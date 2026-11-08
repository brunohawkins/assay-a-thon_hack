"""The small HTTP plumbing the routes are written against.

Requests and responses, the JSON and error shapes from web/README.md
(`{"error": ..., "detail": ...}` with a real status), the body size caps, and a
multipart/form-data reader. `cgi` is gone from modern Pythons, so the upload parser is
`email`-based, which is standard library and has no size surprises because the body is
already capped before it is parsed.
"""

from __future__ import annotations

import json
import time as _time
from email import policy
from email.parser import BytesParser
from typing import Callable, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlsplit

from . import paths

JSON_TYPE = "application/json; charset=utf-8"
SSE_HEADERS = [
    ("Content-Type", "text/event-stream; charset=utf-8"),
    ("Cache-Control", "no-cache, no-store, must-revalidate"),
    ("X-Accel-Buffering", "no"),       # tell a reverse proxy not to buffer the stream
    ("Connection", "close"),
]


class ApiError(Exception):
    """An error with the status the client should see. Never leaks a traceback."""

    def __init__(self, status: int, error: str, detail: str = "", **extra):
        super().__init__("%s %s: %s" % (status, error, detail))
        self.status = status
        self.error = error
        self.detail = detail
        self.extra = extra

    def payload(self) -> dict:
        body = {"error": self.error, "detail": self.detail}
        body.update(self.extra)
        return body


#: When a body is refused for being too big, this much of it is read and thrown away
#: first, so the client can still read the 413 instead of a broken pipe.
DRAIN_LIMIT_BYTES = 64 * 1024 * 1024
DRAIN_SECONDS = 3.0


class Request:
    """One HTTP request, with the body still unread."""

    def __init__(self, app, method, raw_path, headers, rfile, client, connection=None):
        self.app = app
        self.method = method
        self.raw_path = raw_path
        split = urlsplit(raw_path)
        self.path = unquote(split.path)
        self.raw_query = split.query
        self.query = parse_qs(split.query, keep_blank_values=True)
        self.headers = headers
        self.rfile = rfile
        self.connection = connection
        self.client = client
        self.params: Dict[str, str] = {}
        self._body: Optional[bytes] = None

    def get(self, name: str, default: Optional[str] = None) -> Optional[str]:
        values = self.query.get(name)
        return values[0] if values else default

    @property
    def content_length(self) -> int:
        try:
            return max(0, int(self.headers.get("Content-Length") or 0))
        except (TypeError, ValueError):
            raise ApiError(400, "bad_content_length",
                           "Content-Length is not a number") from None

    @property
    def content_type(self) -> str:
        return (self.headers.get("Content-Type") or "").strip()

    def body(self, limit: int) -> bytes:
        """The whole body, refusing anything over `limit` bytes.

        Both the declared length and the bytes actually read are checked: a client can
        lie in the header, so the read itself stops one byte past the cap.
        """
        if self._body is not None:
            return self._body
        declared = self.content_length
        if declared > limit:
            self._drain(declared)
            raise ApiError(413, "too_large",
                           "body is %d bytes; the limit is %d" % (declared, limit),
                           limit_bytes=limit)
        if (self.headers.get("Transfer-Encoding") or "").lower().strip() == "chunked":
            raise ApiError(411, "length_required",
                           "send a Content-Length; chunked uploads are not accepted")
        data = self.rfile.read(min(declared, limit + 1)) if declared else b""
        if len(data) > limit:
            raise ApiError(413, "too_large",
                           "body is larger than the %d byte limit" % limit,
                           limit_bytes=limit)
        self._body = data
        return data

    def _drain(self, declared: int) -> None:
        """Swallow a refused body so the client can read the 413 it is about to get.

        A client that has already started sending 40 MB cannot read our answer until it
        has finished writing, so refusing without draining shows up at the other end as a
        broken pipe instead of a clear error. Bounded in both bytes and time, because the
        declared length may be a lie.
        """
        remaining = min(declared, DRAIN_LIMIT_BYTES)
        deadline = _time.time() + DRAIN_SECONDS
        previous = None
        try:
            if self.connection is not None:
                previous = self.connection.gettimeout()
                self.connection.settimeout(0.5)
            while remaining > 0 and _time.time() < deadline:
                try:
                    chunk = self.rfile.read(min(65536, remaining))
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                remaining -= len(chunk)
        finally:
            if self.connection is not None:
                try:
                    self.connection.settimeout(previous)
                except OSError:
                    pass

    def json(self, limit: int = paths.MAX_JSON_BYTES) -> dict:
        data = self.body(limit)
        if not data.strip():
            return {}
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ApiError(400, "bad_json", str(error)) from None
        if not isinstance(parsed, dict):
            raise ApiError(400, "bad_json", "the body must be a JSON object")
        return parsed


class Response:
    """A finished response, or a stream the server feeds with a writer callable."""

    def __init__(self, status=200, body=b"", headers=None, stream=None):
        self.status = status
        self.body = body
        self.headers = list(headers or [])
        self.stream: Optional[Callable] = stream


def json_response(data, status: int = 200, headers=None) -> Response:
    body = json.dumps(data, indent=2, sort_keys=False, default=str).encode("utf-8") + b"\n"
    return Response(status, body, [("Content-Type", JSON_TYPE)] + list(headers or []))


def error_response(error: ApiError) -> Response:
    return json_response(error.payload(), error.status)


def file_response(path, *, download: bool = False, range_header=None) -> Response:
    """A file, whole or as one byte range.

    Range matters for video: a <video> element cannot seek in a response that arrives as
    one 200, so an mp4 served without `Accept-Ranges` plays from the start and nothing
    else. One range is supported, which is all a media element asks for; a multi-range
    request is answered in full, which is allowed.
    """
    try:
        size = path.stat().st_size
    except OSError as problem:
        raise ApiError(500, "unreadable", "%s: %s" % (paths.repo_rel(path), problem)) from None
    disposition = "attachment" if download else "inline"
    headers = [
        ("Content-Type", paths.content_type(path)),
        ("Content-Disposition", '%s; filename="%s"' % (disposition, path.name)),
        ("X-Content-Type-Options", "nosniff"),
        ("Accept-Ranges", "bytes"),
    ]
    span = parse_range(range_header, size)
    try:
        if span is None:
            with open(path, "rb") as handle:
                return Response(200, handle.read(), headers)
        if span == "unsatisfiable":
            return Response(416, b"", headers + [("Content-Range", "bytes */%d" % size)])
        start, end = span
        with open(path, "rb") as handle:
            handle.seek(start)
            data = handle.read(end - start + 1)
    except OSError as problem:
        raise ApiError(500, "unreadable", "%s: %s" % (paths.repo_rel(path), problem)) from None
    headers.append(("Content-Range", "bytes %d-%d/%d" % (start, start + len(data) - 1, size)))
    return Response(206, data, headers)


def parse_range(header, size: int):
    """(start, end) inclusive, "unsatisfiable", or None for "send the whole thing"."""
    if not header or size <= 0:
        return None
    text = str(header).strip()
    if not text.lower().startswith("bytes=") or "," in text:
        return None                              # not a byte range, or more than one
    spec = text[6:].strip()
    first, _, last = spec.partition("-")
    try:
        if not first:                            # "bytes=-500": the last 500 bytes
            length = int(last)
            if length <= 0:
                return "unsatisfiable"
            start, end = max(0, size - length), size - 1
        else:
            start = int(first)
            end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size or end < start or start < 0:
        return "unsatisfiable"
    return start, min(end, size - 1)


# --- multipart ---------------------------------------------------------------

def parse_multipart(body: bytes, content_type: str):
    """({field: text}, {field: (filename, bytes)}) from a multipart/form-data body."""
    if "multipart/form-data" not in content_type.lower():
        raise ApiError(415, "unsupported_media_type",
                       "expected multipart/form-data or application/json, got %r"
                       % (content_type or "nothing"))
    header = b"MIME-Version: 1.0\r\nContent-Type: " + content_type.encode("utf-8") + b"\r\n\r\n"
    try:
        message = BytesParser(policy=policy.default).parsebytes(header + body)
    except Exception as problem:                     # a malformed body is the client's fault
        raise ApiError(400, "bad_multipart", str(problem)) from None
    if not message.is_multipart():
        raise ApiError(400, "bad_multipart", "no parts found in the body")
    fields: Dict[str, str] = {}
    files: Dict[str, tuple] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            payload = str(part.get_payload()).encode("utf-8", "replace")
        filename = part.get_filename()
        if filename:
            files[str(name)] = (str(filename), payload)
        else:
            fields[str(name)] = payload.decode("utf-8", "replace")
    return fields, files


# --- routing -----------------------------------------------------------------

class Route:
    def __init__(self, method, pattern, handler, name=""):
        self.method = method
        self.pattern = pattern
        self.handler = handler
        self.name = name or getattr(handler, "__name__", "route")


def match(routes: List[Route], method: str, path: str):
    """(route, params) for the first route that matches, or (None, allowed methods)."""
    allowed = set()
    for route in routes:
        found = route.pattern.match(path)
        if not found:
            continue
        if route.method != method:
            allowed.add(route.method)
            continue
        return route, found.groupdict()
    return None, sorted(allowed)

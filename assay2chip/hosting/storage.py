"""Artefact storage behind one interface, plus HMAC-signed download URLs.

    LocalStore   a directory on this machine (the default)
    S3Store      any S3-compatible bucket, path-style, SigV4 (sigv4.py), stdlib only

Choosing one is configuration (A2C_STORE=local|s3 and the A2C_S3_* variables), not code.

Signed URLs: every download link the queue API hands out is
`/api/queue/dl/<key>?exp=<unix>&sig=<hmac>`, served by the API from whichever store is
configured. The HMAC key lives in the data directory (mode 0600), so a link expires, cannot
be edited to point at another object, and works the same for both stores. (S3Store can
also presign a direct bucket URL with `direct_url`, for when the store is remote.)
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import shutil
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, List, Optional
from urllib.parse import urlsplit

from . import sigv4


class StoreError(RuntimeError):
    pass


@dataclass
class ObjectInfo:
    key: str
    size: int


def check_key(key: str) -> str:
    """Object keys are relative POSIX paths with no `..`, no empty parts, no backslashes."""
    if not key or key.startswith("/") or "\\" in key or "\x00" in key:
        raise StoreError("bad object key %r" % key)
    parts = PurePosixPath(key).parts
    if any(p in ("", ".", "..") for p in parts):
        raise StoreError("bad object key %r" % key)
    return "/".join(parts)


class ObjectStore:
    def put_file(self, key: str, path: Path) -> int:
        raise NotImplementedError

    def put_bytes(self, key: str, data: bytes) -> int:
        raise NotImplementedError

    def get_bytes(self, key: str) -> bytes:
        raise NotImplementedError

    def download(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.get_bytes(key))

    def iter_chunks(self, key: str, size: int = 1 << 20) -> Iterator[bytes]:
        yield self.get_bytes(key)

    def list(self, prefix: str = "") -> List[ObjectInfo]:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def describe(self) -> str:
        raise NotImplementedError


class LocalStore(ObjectStore):
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / check_key(key)).resolve()
        if not str(path).startswith(str(self.root.resolve()) + os.sep):
            raise StoreError("key %r escapes the store" % key)
        return path

    def _atomic(self, key: str, writer) -> int:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(".%s.%s.tmp" % (dest.name, secrets.token_hex(4)))
        try:
            writer(tmp)
            os.replace(str(tmp), str(dest))
        finally:
            if tmp.exists():
                tmp.unlink()
        return dest.stat().st_size

    def put_file(self, key: str, path: Path) -> int:
        return self._atomic(key, lambda tmp: shutil.copyfile(str(path), str(tmp)))

    def put_bytes(self, key: str, data: bytes) -> int:
        return self._atomic(key, lambda tmp: tmp.write_bytes(data))

    def get_bytes(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            raise StoreError("no such object %r" % key) from None

    def iter_chunks(self, key: str, size: int = 1 << 20) -> Iterator[bytes]:
        try:
            handle = open(self._path(key), "rb")
        except FileNotFoundError:
            raise StoreError("no such object %r" % key) from None
        with handle:
            while True:
                chunk = handle.read(size)
                if not chunk:
                    return
                yield chunk

    def list(self, prefix: str = "") -> List[ObjectInfo]:
        out = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and not path.name.startswith("."):
                key = path.relative_to(self.root).as_posix()
                if key.startswith(prefix):
                    out.append(ObjectInfo(key, path.stat().st_size))
        return out

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass

    def describe(self) -> str:
        return "local:%s" % self.root


class S3Store(ObjectStore):
    """Path-style S3 over HTTPS: works with AWS, MinIO, OCI and R2 alike."""

    def __init__(self, endpoint: str, bucket: str, region: str, access_key: str,
                 secret_key: str, prefix: str = "", timeout_s: float = 120.0):
        if not (endpoint and bucket and access_key and secret_key):
            raise StoreError("S3 store needs A2C_S3_ENDPOINT, A2C_S3_BUCKET, "
                             "A2C_S3_ACCESS_KEY_ID and A2C_S3_SECRET_ACCESS_KEY")
        split = urlsplit(endpoint)
        self.scheme, self.host = split.scheme or "https", split.netloc
        self.bucket, self.region, self.prefix = bucket, region, prefix.strip("/")
        self.access_key, self.secret_key = access_key, secret_key
        self.timeout_s = timeout_s

    def _path(self, key: str = "") -> str:
        full = "/".join(p for p in (self.prefix, check_key(key) if key else "") if p)
        return "/%s/%s" % (self.bucket, full) if full else "/%s" % self.bucket

    def _request(self, method: str, key: str = "", body: bytes = b"", query=None):
        path = self._path(key)
        query = dict(query or {})
        headers = sigv4.sign_headers(method, self.host, path, region=self.region,
                                     access_key=self.access_key, secret_key=self.secret_key,
                                     query=query,
                                     payload_hash=hashlib.sha256(body).hexdigest())
        qs = sigv4._canonical_query(query)
        url = "%s://%s%s%s" % (self.scheme, self.host, sigv4.uri_encode(path, True),
                               "?" + qs if qs else "")
        headers.pop("host", None)
        req = urllib.request.Request(url, data=body if method in ("PUT", "POST") else None,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as err:
            if err.code == 404:
                raise StoreError("no such object %r" % key) from None
            raise StoreError("S3 %s %s: HTTP %d %s" % (method, path, err.code,
                                                       err.read()[:300])) from None

    def put_bytes(self, key: str, data: bytes) -> int:
        self._request("PUT", key, data)
        return len(data)

    def put_file(self, key: str, path: Path) -> int:
        return self.put_bytes(key, Path(path).read_bytes())

    def get_bytes(self, key: str) -> bytes:
        return self._request("GET", key)[1]

    def exists(self, key: str) -> bool:
        try:
            self._request("HEAD", key)
            return True
        except StoreError:
            return False

    def delete(self, key: str) -> None:
        self._request("DELETE", key)

    def list(self, prefix: str = "") -> List[ObjectInfo]:
        out: List[ObjectInfo] = []
        full_prefix = "/".join(p for p in (self.prefix, prefix) if p)
        token = None
        while True:
            query = {"list-type": "2", "prefix": full_prefix}
            if token:
                query["continuation-token"] = token
            _, body = self._request("GET", "", query=query)
            root = ET.fromstring(body)
            ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
            for item in root.findall(ns + "Contents"):
                key = item.findtext(ns + "Key") or ""
                if self.prefix:
                    key = key[len(self.prefix) + 1:]
                out.append(ObjectInfo(key, int(item.findtext(ns + "Size") or 0)))
            if (root.findtext(ns + "IsTruncated") or "false") != "true":
                return out
            token = root.findtext(ns + "NextContinuationToken")

    def direct_url(self, key: str, expires_s: int) -> str:
        path = self._path(key)
        qs = sigv4.presign("GET", self.host, path, region=self.region,
                           access_key=self.access_key, secret_key=self.secret_key,
                           expires_s=expires_s)
        return "%s://%s%s?%s" % (self.scheme, self.host, sigv4.uri_encode(path, True), qs)

    def describe(self) -> str:
        return "s3:%s://%s/%s" % (self.scheme, self.host, self.bucket)


def open_store(settings) -> ObjectStore:
    if settings.store == "local":
        return LocalStore(settings.store_dir)
    if settings.store == "s3":
        return S3Store(settings.s3_endpoint or "", settings.s3_bucket or "",
                       settings.s3_region, settings.s3_access_key or "",
                       settings.s3_secret_key or "", prefix=settings.s3_prefix)
    raise StoreError("A2C_STORE=%r: use 'local' or 's3'" % settings.store)


class UrlSigner:
    """HMAC-SHA256 over (key, expiry). Constant-time verification."""

    def __init__(self, secret: bytes):
        if len(secret) < 16:
            raise ValueError("signing secret too short")
        self.secret = secret

    @classmethod
    def from_file(cls, path: Path) -> "UrlSigner":
        path = Path(path)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(secrets.token_hex(32))
        return cls(path.read_text().strip().encode("ascii"))

    def _mac(self, key: str, exp: int) -> str:
        return hmac.new(self.secret, ("%s\n%d" % (key, exp)).encode("utf-8"),
                        hashlib.sha256).hexdigest()

    def sign(self, key: str, ttl_s: int, now: Optional[float] = None) -> dict:
        exp = int((now if now is not None else time.time()) + ttl_s)
        return {"exp": exp, "sig": self._mac(check_key(key), exp)}

    def verify(self, key: str, exp: str, sig: str, now: Optional[float] = None) -> bool:
        try:
            exp_i = int(exp)
            check_key(key)
        except (TypeError, ValueError, StoreError):
            return False
        if exp_i < (now if now is not None else time.time()):
            return False
        return hmac.compare_digest(self._mac(key, exp_i), str(sig or ""))

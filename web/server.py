"""Run the whole pipeline from a browser.

    python web/server.py                      # http://127.0.0.1:8765
    python web/server.py --port 9000
    python web/server.py --host 0.0.0.0       # deliberate, and it says so

Standard library only: http.server's ThreadingHTTPServer plus threads, no dependencies
and no build step. Threads matter for two reasons - an SSE connection holds a thread for
the length of a run, and the browser opens the event stream while still fetching files.

It binds 127.0.0.1 unless told otherwise. This repo is private and a run can hold an
ingested paper, so putting it on a network is an explicit choice, never a default.

Serving:
  /api/*                 the API in web/README.md (web/api/routes.py)
  everything else         web/static/, the front end

Jobs run assay2chip/run.py (or assay2chip/cfd/run.py) as a subprocess in a worker
thread, one at a time by default - see web/api/jobs.py for why.
"""

from __future__ import annotations

import argparse
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __package__ in (None, ""):                      # python web/server.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web.api import http as api_http                                        # noqa: E402
from web.api import jobs, llm, paths, routes, sandbox                       # noqa: E402

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Served when the front end is not in this checkout yet. web/static/ belongs to the
#: front end; the server never writes it, so this is a response, not a file.
NO_FRONTEND = """<!doctype html>
<html lang="en"><meta charset="utf-8"><title>assay2chip</title>
<style>body{font:16px/1.55 system-ui,sans-serif;margin:3rem auto;max-width:44rem;padding:0 1rem}
code{background:#f3f3f3;padding:.1em .35em;border-radius:3px}</style>
<h1>assay2chip</h1>
<p>The API is up. The front end (<code>web/static/index.html</code>) is not in this
checkout yet - it is being built in parallel.</p>
<p>Start here: <a href="/api/health">/api/health</a>,
<a href="/api/stages">/api/stages</a>,
<a href="/api/runs">/api/runs</a>,
<a href="/api/chips">/api/chips</a>.</p>
</html>
"""


class App:
    """What the handlers need: the job queue and the route table."""

    def __init__(self, concurrency: int = 1):
        self.manager = jobs.JobManager(concurrency=concurrency)
        self.routes = routes.ROUTES
        self.pages = routes.PAGE_ROUTES
        self.version = routes.VERSION

    def close(self) -> None:
        self.manager.shutdown()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "assay2chip/" + routes.VERSION
    sys_version = ""

    # --- plumbing -------------------------------------------------------------

    @property
    def app(self) -> App:
        return self.server.app

    def log_message(self, template, *args):
        if self.server.quiet:
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), template % args))

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_HEAD(self):
        self._handle("HEAD")

    def _handle(self, method: str) -> None:
        request = api_http.Request(self.app, method, self.path, self.headers, self.rfile,
                                   self.client_address, connection=self.connection)
        try:
            if request.path.startswith("/api/"):
                response = self._api(request, method)
            elif method in ("GET", "HEAD"):
                response = self._static(request)
            else:
                raise api_http.ApiError(405, "method_not_allowed",
                                        "%s is not allowed here" % method)
        except api_http.ApiError as error:
            response = api_http.error_response(error)
        except sandbox.Denied as denied:
            response = api_http.error_response(
                api_http.ApiError(403, "forbidden_path", denied.message,
                                  hint=denied.detail))
        except BrokenPipeError:
            return
        except Exception as problem:                # never a traceback to the browser
            self.log_error("unhandled %s: %s", type(problem).__name__, problem)
            response = api_http.error_response(api_http.ApiError(
                500, "server_error", "%s: %s" % (type(problem).__name__, problem)))
        self._send(response, head_only=(method == "HEAD"))

    def _api(self, request, method: str):
        lookup_method = "GET" if method == "HEAD" else method
        route, params = api_http.match(self.app.routes, lookup_method, request.path)
        if route is None:
            if params:
                raise api_http.ApiError(405, "method_not_allowed",
                                        "%s %s is not a route; try %s"
                                        % (method, request.path, ", ".join(params)))
            raise api_http.ApiError(404, "no_such_route",
                                    "%s is not an API route; see web/README.md"
                                    % request.path)
        request.params = params
        return route.handler(request)

    def _static(self, request):
        """web/static/ first, then the pages and the repo files the twin and the docs
        routes allow. The front end owns web/static/; everything else is allowlisted."""
        route, params = api_http.match(self.app.pages, "GET", request.path)
        if route is not None:
            request.params = params
            return route.handler(request)
        try:
            path = sandbox.resolve_static(request.path)
        except sandbox.Denied as denied:
            if request.path in ("/", "/index.html"):
                return api_http.Response(
                    200, NO_FRONTEND.encode("utf-8"),
                    [("Content-Type", "text/html; charset=utf-8")])
            if not denied.message.startswith("path rejected"):
                # Not a front-end file: it may be one the twin fetches with ../, or a
                # render the documentation points at. Same allowlists, same checks.
                return routes.root_file(request)
            raise api_http.ApiError(404, "not_found", denied.message) from None
        return api_http.file_response(path, range_header=request.headers.get("Range"))

    def _send(self, response, head_only: bool = False) -> None:
        if response.stream is not None:
            self._send_stream(response)
            return
        body = b"" if head_only else response.body
        if response.status in (411, 413):
            # The body was refused, so some of it may still be in flight: do not try to
            # read another request off this connection.
            self.close_connection = True
        try:
            self.send_response(response.status)
            for name, value in response.headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(response.body)))
            if not any(name.lower() == "cache-control" for name, _ in response.headers):
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _send_stream(self, response) -> None:
        """Server-Sent Events: headers, then whatever the generator writes, flushed."""
        self.close_connection = True
        try:
            self.send_response(response.status)
            for name, value in response.headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

        def write(chunk: bytes) -> None:
            self.wfile.write(chunk)
            self.wfile.flush()

        try:
            response.stream(write)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass                                    # the browser closed the stream


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, app: App, quiet: bool = False):
        self.app = app
        self.quiet = quiet
        super().__init__(address, handler)

    def server_close(self):
        try:
            self.app.close()
        finally:
            super().server_close()


def make_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, *,
                concurrency: int = 1, root=None, quiet: bool = False) -> Server:
    """A ready-to-serve server. `root` repoints the whole API at another checkout."""
    if root is not None:
        paths.configure(root)
    return Server((host, port), Handler, App(concurrency=concurrency), quiet=quiet)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="web/server.py",
        description="Serve web/static/ and the /api/* routes in web/README.md.")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="interface to bind; the default %s is local only, and this "
                             "repo is private" % DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="port (default %d)" % DEFAULT_PORT)
    parser.add_argument("--concurrency", type=int, default=1,
                        help="jobs at once. The default 1 is deliberate: stages S5-S11 "
                             "write into the shared chip/ and protocols/ trees")
    parser.add_argument("--root", default=None,
                        help="repository root to serve (default: this checkout)")
    parser.add_argument("--quiet", action="store_true", help="no per-request logging")
    args = parser.parse_args(argv)

    if args.root:
        paths.configure(args.root)
    try:
        server = make_server(args.host, args.port, concurrency=args.concurrency,
                             quiet=args.quiet)
    except OSError as problem:
        print("web/server.py: cannot bind %s:%d: %s" % (args.host, args.port, problem),
              file=sys.stderr)
        return 2

    host, port = server.server_address[0], server.server_address[1]
    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    print("assay2chip web %s on http://%s:%d" % (routes.VERSION, shown, port))
    print("  root      %s" % paths.ROOT)
    print("  static    %s%s" % (paths.repo_rel(paths.STATIC),
                                "" if paths.STATIC.is_dir() else "  (not built yet)"))
    print("  pipeline  %s%s" % (paths.repo_rel(paths.RUN_SCRIPT),
                                "" if paths.RUN_SCRIPT.is_file() else "  (MISSING: runs "
                                "return 503)"))
    print("  cfd       %s%s" % (paths.repo_rel(paths.CFD_SCRIPT),
                                "" if paths.CFD_SCRIPT.is_file() else "  (MISSING: "
                                "/api/cfd returns 503)"))
    print("  llm       %s" % llm.report()["mode"])
    print("  jobs      %d at a time" % max(1, args.concurrency))
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("  !! bound to %s: this repo is private and a run can hold an ingested "
              "paper" % args.host, file=sys.stderr)
    print("  ctrl-c to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

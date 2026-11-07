"""Serve the repo and open the twin's presenter view: one command for the live demo.

    python tools/demo.py                 # serves http://localhost:8000/ and opens /twin/?present
    python tools/demo.py --step 4        # start at step 4
    python tools/demo.py --port 8080     # another port (if it is taken, the next free one is used)
    python tools/demo.py --no-browser    # only serve; open the printed URL yourself
    python tools/demo.py --twin          # the normal twin instead of the presenter

Needs no network: three.js is vendored in twin/vendor/ and the presenter's AI operator is the
scripted one. In the presenter: Next / Back, or the arrow keys, PageDown / PageUp (what a
presentation clicker sends), Space, Home / End; F toggles full screen. Ctrl+C stops the server.
Standard library only.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Handler(http.server.SimpleHTTPRequestHandler):
    # ES modules must be served as JavaScript; some Windows registries map .js to text/plain.
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript", ".mjs": "text/javascript", ".json": "application/json",
        ".svg": "image/svg+xml", ".stl": "model/stl", ".dxf": "application/dxf", ".webm": "video/webm",
    }

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")  # edits show up on reload
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:  # keep the terminal quiet during the demo
        pass


def make_server(port: int, tries: int = 20) -> tuple[http.server.ThreadingHTTPServer, int]:
    handler = functools.partial(Handler, directory=str(ROOT))
    for p in range(port, port + tries):
        try:
            return http.server.ThreadingHTTPServer(("127.0.0.1", p), handler), p
        except OSError:
            continue
    raise SystemExit(f"No free port between {port} and {port + tries - 1}: try --port.")


def presenter_url(port: int, step: int | None = None, twin: bool = False) -> str:
    url = f"http://localhost:{port}/twin/"
    if twin:
        return url
    return url + "?present" + (f"#step={step}" if step else "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Serve the repo and open the twin's presenter view.")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--step", type=int, help="start at this step (1 = the first)")
    ap.add_argument("--no-browser", action="store_true", help="don't open a browser")
    ap.add_argument("--twin", action="store_true", help="open the normal twin, not the presenter")
    args = ap.parse_args(argv)
    if not (ROOT / "twin" / "vendor" / "three" / "build" / "three.module.js").is_file():
        print("twin/vendor/three/ is missing: the twin can't load three.js offline.", file=sys.stderr)
        return 1
    httpd, port = make_server(args.port)
    url = presenter_url(port, args.step, args.twin)
    print(f"Serving {ROOT} at http://localhost:{port}/")
    print(f"Presenter: {url}" if not args.twin else f"Twin: {url}")
    print("Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.6, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

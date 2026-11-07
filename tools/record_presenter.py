"""Record the presenter sequence as a backup video, plus a screenshot of each step.

    python tools/record_presenter.py                  # -> docs/demo/presenter_backup.webm + docs/demo/step_N_<id>.jpg
    python tools/record_presenter.py --no-video       # screenshots only
    python tools/record_presenter.py --size 1280x720  # another resolution (default 1920x1080)

Needs Playwright, which is NOT part of the repo's stdlib + jsonschema set (the twin and the
other tools don't need it):
    pip install playwright && python -m playwright install chromium

The browser gets every file straight from the repo by request interception: no server and no
network. Any request for something outside the repo is refused and reported, which doubles as
the check that the presenter runs offline. On macOS it renders on the GPU (Metal); elsewhere
Chromium picks. The video is Playwright's own real-time recording (VP8 .webm), paced by each
step's `seconds` in twin/presenter.js; the screenshot is taken part-way through each step.
If ffmpeg is on the PATH, the video is then trimmed to start at step 1 (no loading screen) and
re-encoded as constant-quality VP9, which makes it several times smaller at the same sharpness
(--raw keeps Playwright's file as it is).
Exits 1 if the page logged an error or asked for anything outside the repo.
"""

from __future__ import annotations

import argparse
import html
import mimetypes
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "demo"
BASE = "http://twin.local"


def listing(directory: Path, url_path: str) -> str:
    """A directory listing in the shape python -m http.server writes (the twin reads these)."""
    items = "".join(
        f'<li><a href="{urllib.parse.quote(n + ("/" if (directory / n).is_dir() else ""))}">{html.escape(n)}</a></li>'
        for n in sorted(p.name for p in directory.iterdir()))
    return f"<!DOCTYPE HTML><html><head><title>Directory listing for {url_path}</title></head><body><ul>{items}</ul></body></html>"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record the twin's presenter sequence (video + screenshots).")
    ap.add_argument("--size", default="1920x1080", help="WIDTHxHEIGHT (default 1920x1080)")
    ap.add_argument("--no-video", action="store_true", help="screenshots only")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--raw", action="store_true", help="keep Playwright's video as recorded (no ffmpeg trim / re-encode)")
    args = ap.parse_args(argv)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Needs Playwright: pip install playwright && python -m playwright install chromium", file=sys.stderr)
        return 2
    w, h = (int(x) for x in args.size.lower().split("x"))
    args.out.mkdir(parents=True, exist_ok=True)
    problems: list[str] = []
    refused: list[str] = []

    def handle(route) -> None:
        url = route.request.url
        if not url.startswith(BASE):
            refused.append(url)
            route.abort()
            return
        path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
        target = (ROOT / path.lstrip("/")).resolve()
        if not str(target).startswith(str(ROOT)) or not target.exists():
            route.fulfill(status=404, body="not found")
            return
        if target.is_dir():
            if not path.endswith("/"):
                route.fulfill(status=301, headers={"Location": path + "/"})
                return
            if (target / "index.html").exists():
                target = target / "index.html"
            else:
                route.fulfill(status=200, content_type="text/html; charset=utf-8", body=listing(target, path))
                return
        ctype = "text/javascript" if target.suffix == ".js" else mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        route.fulfill(status=200, content_type=ctype, body=target.read_bytes())

    flags = ["--use-angle=metal", "--enable-gpu", "--ignore-gpu-blocklist"] if sys.platform == "darwin" else ["--ignore-gpu-blocklist"]
    video_tmp = Path(tempfile.mkdtemp(prefix="presenter_video_"))
    with sync_playwright() as p:
        browser = p.chromium.launch(args=flags)
        opts = {"viewport": {"width": w, "height": h}, "device_scale_factor": 1}
        if not args.no_video:
            opts.update(record_video_dir=str(video_tmp), record_video_size={"width": w, "height": h})
        context = browser.new_context(**opts)
        t_video = time.monotonic()  # the recording starts with the context
        page = context.new_page()
        page.route("**/*", handle)
        page.on("console", lambda m: problems.append(f"console.{m.type}: {m.text}")
                if m.type == "error" or (m.type == "warning" and "GPU stall" not in m.text) else None)
        page.on("pageerror", lambda e: problems.append(f"page error: {e}"))
        page.goto(f"{BASE}/twin/?present")
        page.wait_for_function("window.__presenter && window.__presenter.ready && !window.__presenter.busy", timeout=90000)
        steps = page.evaluate("() => window.__presenter.steps")
        head = time.monotonic() - t_video  # loading screen at the start of the recording
        for old in args.out.glob("step_*.jpg"):
            old.unlink()
        t0 = time.monotonic()
        for i, st in enumerate(steps):
            if i:
                page.keyboard.press("PageDown")
            page.wait_for_function(f"() => window.__presenter.index === {i} && !window.__presenter.busy", timeout=90000)
            start = time.monotonic()
            page.wait_for_timeout(int(st["seconds"] * 600))
            shot = args.out / f"step_{i + 1}_{st['id']}.jpg"
            page.screenshot(path=str(shot), type="jpeg", quality=85)
            rest = st["seconds"] - (time.monotonic() - start)
            if rest > 0:
                page.wait_for_timeout(int(rest * 1000))
            print(f"  step {i + 1}/{len(steps)} {st['id']:<9} {st['title']}  -> {shot.relative_to(ROOT)}")
        page.wait_for_timeout(1000)
        total = time.monotonic() - t0
        video = page.video
        context.close()
        browser.close()
        if video and not args.no_video:
            dest = args.out / "presenter_backup.webm"
            raw = Path(video.path())
            ffmpeg = shutil.which("ffmpeg")
            if ffmpeg and not args.raw:
                # Trim the loading screen and re-encode: constant-quality VP9 keeps text sharp at a
                # fraction of the size, since most of each frame is still.
                cmd = [ffmpeg, "-v", "error", "-y", "-ss", f"{max(0.0, head - 0.3):.2f}", "-i", str(raw), "-an",
                       "-c:v", "libvpx-vp9", "-crf", "36", "-b:v", "0", "-row-mt", "1", "-deadline", "good",
                       "-cpu-used", "4", str(dest)]
                if subprocess.run(cmd).returncode != 0:
                    print("ffmpeg failed; keeping the raw recording", file=sys.stderr)
                    shutil.move(raw, dest)
            else:
                shutil.move(raw, dest)
            print(f"video: {dest.relative_to(ROOT)} ({total:.0f} s of steps, {dest.stat().st_size / 1e6:.1f} MB, {w}x{h})")
    shutil.rmtree(video_tmp, ignore_errors=True)
    for x in problems:
        print("PROBLEM", x)
    for u in refused:
        print("REFUSED (outside the repo):", u)
    return 1 if problems or refused else 0


if __name__ == "__main__":
    raise SystemExit(main())

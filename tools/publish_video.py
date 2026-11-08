"""Publish a demo video as a GitHub RELEASE ASSET and record its url in the video manifest.

    python tools/publish_video.py VIDEO --module module1_v1 --yes
    python tools/publish_video.py VIDEO --module "Module 4" --duration 42 --yes
    python tools/publish_video.py VIDEO --module module5_v1 --repo OWNER/REPO --yes

Videos never go in git. This uploads VIDEO with the `gh` CLI to the release tagged
`demo-videos` (creating that release if it is missing), named after the chip
(`module1_v1.mp4`), then fills url / release_tag / recorded / duration_s for that slot in
docs/videos/manifest.json and regenerates docs/modules.md and docs/modules.json.

It uploads only with an explicit --yes; without it, it prints what it would do and stops.
Needs `gh auth login` with write access to the repo. See docs/videos/README.md.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "docs/videos/manifest.json"
TAG = "demo-videos"
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}


def _gh(run, args: list[str], repo: str | None) -> subprocess.CompletedProcess:
    cmd = ["gh", *args] + (["--repo", repo] if repo else [])
    return run(cmd, capture_output=True, text=True)


def find_slot(manifest: dict, key: str) -> dict | None:
    k = key.strip().lower()
    for v in manifest.get("videos", []):
        if k in (str(v.get("chip_id", "")).lower(), str(v.get("module", "")).lower()):
            return v
    return None


def publish(video: Path, key: str, *, yes: bool, root: Path = ROOT, tag: str = TAG,
            repo: str | None = None, duration_s: float | None = None, run=subprocess.run,
            regenerate: bool = True, echo=print) -> int:
    video = Path(video)
    mpath = Path(root) / MANIFEST
    if not mpath.is_file():
        echo(f"no {MANIFEST}: run python tools/module_catalog.py first")
        return 1
    manifest = json.loads(mpath.read_text())
    slot = find_slot(manifest, key)
    if slot is None:
        echo(f"no video slot for {key!r}; slots: {', '.join(v['chip_id'] for v in manifest.get('videos', []))}")
        return 1
    if not video.is_file():
        echo(f"no such file: {video}")
        return 1
    if video.suffix.lower() not in VIDEO_EXTS:
        echo(f"not a video file ({video.suffix}); expected one of {', '.join(sorted(VIDEO_EXTS))}")
        return 1
    asset = f"{slot['chip_id']}{video.suffix.lower()}"
    where = repo or "the current repo"
    echo(f"upload {video} ({video.stat().st_size:,} bytes) as {asset} to release {tag} of {where}; "
         f"then set {slot['module']} url in {MANIFEST}")
    if not yes:
        echo("dry run: pass --yes to upload")
        return 2
    if shutil.which("gh") is None and run is subprocess.run:
        echo("the GitHub CLI (gh) is not installed: https://cli.github.com")
        return 1

    if _gh(run, ["release", "view", tag], repo).returncode != 0:
        made = _gh(run, ["release", "create", tag, "--title", "Demo videos",
                         "--notes", "Recordings of the printed demo modules. Index: docs/videos/manifest.json."], repo)
        if made.returncode != 0:
            echo(f"could not create release {tag}: {made.stderr.strip()}")
            return 1
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / asset
        shutil.copyfile(video, staged)
        up = _gh(run, ["release", "upload", tag, str(staged), "--clobber"], repo)
    if up.returncode != 0:
        echo(f"upload failed: {up.stderr.strip()}")
        return 1
    view = _gh(run, ["release", "view", tag, "--json", "assets"], repo)
    url = None
    if view.returncode == 0:
        try:
            url = next((a.get("url") for a in json.loads(view.stdout).get("assets", []) if a.get("name") == asset), None)
        except json.JSONDecodeError:
            url = None
    if not url:
        echo("uploaded, but could not read the asset url back; manifest not changed")
        return 1

    slot.update({"url": url, "release_tag": tag, "recorded": True})
    if duration_s is not None:
        slot["duration_s"] = duration_s
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    echo(f"{slot['module']}: {url}")
    if regenerate:
        sys.path.insert(0, str(Path(root) / "tools"))
        import module_catalog  # noqa: E402

        module_catalog.main([], root=Path(root))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("video", type=Path)
    ap.add_argument("--module", required=True, help='chip id (module1_v1) or slot ("Module 1")')
    ap.add_argument("--yes", action="store_true", help="really upload (required)")
    ap.add_argument("--tag", default=TAG)
    ap.add_argument("--repo", default=None, help="OWNER/REPO (default: the repo gh sees here)")
    ap.add_argument("--duration", type=float, default=None, help="length in seconds")
    a = ap.parse_args(argv)
    if a.repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", a.repo):
        ap.error("--repo must be OWNER/REPO")
    return publish(a.video, a.module, yes=a.yes, tag=a.tag, repo=a.repo, duration_s=a.duration)


if __name__ == "__main__":
    sys.exit(main())

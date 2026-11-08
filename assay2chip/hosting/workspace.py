"""One isolated copy of the source tree per run, and syncing what a run produces.

WHY A COPY. Stages S5-S11 write into the shared chip/, protocols/ and cad/ trees, which is
why web/server.py runs one pipeline at a time. Giving every run its own copy of the source
tree (about 25 MB; assay2chip/work, .git, .venv and private/ are left out) lets runs and
their OpenFOAM cases proceed side by side, and keeps the checkout itself untouched: a
hosted run never writes a file in the repo.

SYNC. After every job, each file that is new or changed relative to the pristine copy is
uploaded to the object store under runs/<run_id>/<path> and recorded in the artifacts
table. OpenFOAM's bulk (polyMesh, processor*/, time directories, VTK/) stays on local scratch,
the same split the repo's .gitignore makes: summaries, images and video are kept.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

TOP_EXCLUDE = {".git", ".venv", "venv", "private", "node_modules", ".pytest_cache",
               ".mypy_cache"}
ANY_EXCLUDE = {"__pycache__", ".DS_Store"}
BASELINE = ".a2c_baseline.json"
SYNCED = ".a2c_synced.json"
MAX_SYNC_BYTES = int(os.environ.get("A2C_SYNC_MAX_MB", "512")) * 1024 * 1024

KINDS = {".json": "json", ".svg": "svg", ".stl": "stl", ".dxf": "dxf", ".md": "md",
         ".png": "png", ".mp4": "mp4", ".gltf": "gltf", ".glb": "glb", ".csv": "csv",
         ".txt": "txt", ".log": "log", ".pdf": "pdf", ".py": "py", ".step": "step"}


def kind_of(path: str) -> str:
    return KINDS.get(Path(path).suffix.lower(), "file")


def _ignore(source_root: Path):
    work = (source_root / "assay2chip" / "work").resolve()

    def ignore(directory: str, names: List[str]) -> List[str]:
        here = Path(directory).resolve()
        out = [n for n in names if n in ANY_EXCLUDE or n.startswith(".env")
               or n.endswith(".key")]
        if here == source_root.resolve():
            out += [n for n in names if n in TOP_EXCLUDE]
        if here == work:
            out += [n for n in names if n != ".gitkeep"]
        return out

    return ignore


def _is_number(name: str) -> bool:
    try:
        float(name)
        return True
    except ValueError:
        return False


def is_bulk(rel: str) -> bool:
    """OpenFOAM case bulk and bookkeeping that never leaves the machine."""
    parts = rel.split("/")
    if parts[-1].startswith(".a2c_"):
        return True
    if "foam" in parts:
        dirs = parts[parts.index("foam") + 1:-1]
        for d in dirs:
            if d in ("polyMesh", "VTK", "postProcessing") or d.startswith("processor") \
                    or _is_number(d):
                return True
    return False


def _index(root: Path) -> Dict[str, list]:
    out: Dict[str, list] = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = [d for d in dirnames if d not in ANY_EXCLUDE]
        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            try:
                st = full.stat()
            except OSError:
                continue
            out[rel] = [st.st_size, st.st_mtime_ns]
    return out


class Workspaces:
    def __init__(self, base: Path, source_root: Path):
        self.base = Path(base)
        self.source_root = Path(source_root)
        self.base.mkdir(parents=True, exist_ok=True)

    def path(self, run_id: str) -> Path:
        return self.base / run_id

    def ensure(self, run_id: str, restore: Optional[Callable[[Path], None]] = None) -> Path:
        """The run's workspace, created on first use (atomically) or restored."""
        dest = self.path(run_id)
        if (dest / BASELINE).exists():
            return dest
        tmp = self.base / (".%s.%d.tmp" % (run_id, os.getpid()))
        if tmp.exists():
            shutil.rmtree(str(tmp))
        shutil.copytree(str(self.source_root), str(tmp), symlinks=True,
                        ignore=_ignore(self.source_root))
        (tmp / "assay2chip" / "work").mkdir(parents=True, exist_ok=True)
        (tmp / BASELINE).write_text(json.dumps(_index(tmp)))
        if dest.exists():                       # a half-made one from a crash
            shutil.rmtree(str(dest))
        os.replace(str(tmp), str(dest))
        if restore is not None:
            restore(dest)                       # earlier jobs' artefacts, from the store
        return dest

    def changed(self, run_id: str) -> List[str]:
        root = self.path(run_id)
        baseline = json.loads((root / BASELINE).read_text())
        synced_path = root / SYNCED
        synced = json.loads(synced_path.read_text()) if synced_path.exists() else {}
        out = []
        for rel, stat in sorted(_index(root).items()):
            if is_bulk(rel) or stat[0] > MAX_SYNC_BYTES:
                continue
            if baseline.get(rel) == stat or synced.get(rel) == stat:
                continue
            out.append(rel)
        return out

    def sync(self, run_id: str, store, *, job_id: Optional[str] = None) -> List[dict]:
        """Upload new and changed files. Returns the artefact records written."""
        root = self.path(run_id)
        synced_path = root / SYNCED
        synced = json.loads(synced_path.read_text()) if synced_path.exists() else {}
        records = []
        for rel in self.changed(run_id):
            full = root / rel
            try:
                data = full.read_bytes()
                stat = full.stat()
            except OSError:
                continue
            key = "runs/%s/%s" % (run_id, rel)
            store.put_bytes(key, data)
            synced[rel] = [stat.st_size, stat.st_mtime_ns]
            records.append({"path": rel, "key": key, "bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "kind": kind_of(rel), "job_id": job_id})
        synced_path.write_text(json.dumps(synced))
        return records

    def mark_synced(self, run_id: str, rels: List[str]) -> None:
        """Files just restored from the store: already there, do not upload them again."""
        root = self.path(run_id)
        synced_path = root / SYNCED
        synced = json.loads(synced_path.read_text()) if synced_path.exists() else {}
        for rel in rels:
            try:
                st = (root / rel).stat()
            except OSError:
                continue
            synced[rel] = [st.st_size, st.st_mtime_ns]
        synced_path.write_text(json.dumps(synced))

    def remove_older_than(self, days: float, keep: List[str]) -> List[str]:
        """Delete workspaces of finished runs (their artefacts are already in the store)."""
        cutoff = time.time() - days * 86400
        gone = []
        for child in self.base.iterdir():
            if child.name.startswith(".") or child.name in keep:
                continue
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(str(child), ignore_errors=True)
                gone.append(child.name)
        return gone

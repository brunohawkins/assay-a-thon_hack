"""Where everything lives, what a file is called, and what an id is allowed to be.

One module owns the repo layout so the rest of the API never builds a path by hand, and
so the tests can point the whole server at a temporary repo with `configure(root=...)`.

Read these as attributes (`paths.WORK`), never with `from ... import WORK`: `configure()`
rebinds them.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Optional

# --- the repo ----------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]

# Every path below is derived from ROOT by _derive(), which configure() re-runs.
WEB = STATIC = WORK = CHIP_DIR = CAD_DIR = FIGURE_DIR = EXPORT_DIR = PROTOCOL_DIR = ROOT
CONTRACTS = BLOCKS_JSON = RUN_SCRIPT = CFD_SCRIPT = ROOT

#: The pipeline's run manifest, written by assay2chip/run.py (its `manifest` stage kind).
MANIFEST_NAME = "run_manifest.json"
#: Where an uploaded source document goes, under the run's work directory and nowhere else.
UPLOAD_DIRNAME = "upload"
#: CFD findings, written by S10 / assay2chip/cfd/run.py, relative to the work directory.
FINDINGS_REL = "cfd/findings.json"
REPORT_MD = "report.md"
REPORT_JSON = "report.json"

#: Hard cap on an uploaded paper. A protocol PDF with figures is a few MB; this leaves
#: room for a scanned one without letting an unauthenticated local client fill the disk.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
#: Cap on a JSON request body (a pasted protocol is text, so this is generous).
MAX_JSON_BYTES = 4 * 1024 * 1024


def _derive() -> None:
    global WEB, STATIC, WORK, CHIP_DIR, CAD_DIR, FIGURE_DIR, EXPORT_DIR, PROTOCOL_DIR
    global CONTRACTS, BLOCKS_JSON, RUN_SCRIPT, CFD_SCRIPT
    WEB = ROOT / "web"
    STATIC = WEB / "static"
    WORK = ROOT / "assay2chip" / "work"
    CHIP_DIR = ROOT / "chip"
    CAD_DIR = ROOT / "cad"
    FIGURE_DIR = CAD_DIR / "figures"
    EXPORT_DIR = CAD_DIR / "exports"
    PROTOCOL_DIR = ROOT / "protocols"
    CONTRACTS = ROOT / "contracts"
    BLOCKS_JSON = CONTRACTS / "blocks.json"
    RUN_SCRIPT = ROOT / "assay2chip" / "run.py"
    CFD_SCRIPT = ROOT / "assay2chip" / "cfd" / "run.py"


_derive()


def configure(root) -> Path:
    """Point the server at a different repo root (the tests use a temporary one)."""
    global ROOT
    ROOT = Path(root).resolve()
    _derive()
    return ROOT


# --- ids and file names ------------------------------------------------------

#: assay2chip/run.py refuses anything else: snake_case, starting with a letter.
ID_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")
_RESERVED = {"", ".", "..", "con", "nul", "prn", "aux"}

#: A run id that means "no run": the repo-wide part of the /file allowlist only
#: (cad/figures/, chip/ and protocols/ specs), so the Chips view works before any run.
REPO_SCOPE = "repo"


class BadId(ValueError):
    """An id that cannot be used as a directory name."""


def sanitise_id(raw: Optional[str], *, fallback: str = "run") -> str:
    """A run id that is safe as a single directory name and legal for run.py.

    Everything outside [a-z0-9_] becomes an underscore, so no caller can smuggle a
    separator, a dot-dot or a leading dash (which argparse would read as a flag) into a
    path or a command line.
    """
    text = unicodedata.normalize("NFKD", str(raw or "")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    text = re.sub(r"_+", "_", text)[:64].strip("_")
    if text and text[0].isdigit():
        text = "r" + text
    if not text or text in _RESERVED:
        text = fallback
    if not ID_RE.match(text):
        raise BadId(
            f"{raw!r} cannot be turned into a run id; use lowercase letters, digits and "
            f"underscores, starting with a letter"
        )
    return text


def safe_filename(raw: Optional[str], *, fallback: str = "upload.bin") -> str:
    """The base name of an uploaded file, stripped of any path and anything odd.

    Only the name survives: no directory, no leading dot, one extension kept.
    """
    name = os.path.basename(str(raw or "").replace("\\", "/")).strip()
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).lstrip(".-_")
    name = re.sub(r"\.{2,}", ".", name)[:128]
    return name or fallback


# --- content types -----------------------------------------------------------

CONTENT_TYPES = {
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".stl": "model/stl",
    ".dxf": "image/vnd.dxf",
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".bin": "application/octet-stream",
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".ogv": "video/ogg",
    ".pdf": "application/pdf",
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

#: The artifact kinds web/README.md names, plus whatever else turns up, unabbreviated.
KINDS = {".json": "json", ".svg": "svg", ".stl": "stl", ".dxf": "dxf", ".md": "md",
         ".png": "png"}


def content_type(path) -> str:
    return CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def kind_for(path) -> str:
    suffix = Path(path).suffix.lower()
    return KINDS.get(suffix, suffix.lstrip(".") or "file")


def repo_rel(path) -> str:
    """A path as the API spells it: POSIX, relative to the repo root when it is inside."""
    path = Path(path)
    try:
        return path.resolve().relative_to(Path(os.path.realpath(ROOT))).as_posix()
    except ValueError:
        return path.as_posix()

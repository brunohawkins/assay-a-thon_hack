"""The one security boundary every file route goes through.

This repo holds unpublished lab work, and the server answers an unauthenticated local
browser, so a path-traversal hole here is a confidentiality breach rather than a bug.
There are three allowlists, and nothing is served that is not on one of them.

`/api/runs/<id>/file` - the rule in web/README.md:

  * the paths the run's own manifest lists (and the run's own work directory, which is
    where every one of those stage files lives),
  * `cad/figures/`,
  * `cad/exports/<chip_id>/` for the chips THIS run produced,
  * `chip/` and `protocols/` specs - the .json and .md files only, never the build
    scripts that sit beside them.

`/docs/<path>` - documentation and the media it points at:

  * `docs/` (json, md, svg, images, video, glTF),
  * `assay2chip/work/<id>/foam/render/` - the 3D render output,
  * `assay2chip/work/<id>/cfd/figures/` - the CFD heatmaps.

`/twin/` and the repo-root paths the twin fetches - the allowlist is DERIVED FROM THE
TWIN'S OWN CODE by `tools/build_twin_site.discover()`, the same function that decides
what may be published, so the two cannot drift apart. Exact membership, not a prefix.

Every one of them goes through the same four checks, in this order:

  1. the request is syntactically refused if it is absolute, contains a `..` segment, has
     a NUL, or is empty (`%2e%2e` is already decoded by the time it arrives here, so it
     is the same check);
  2. NEVER_PREFIXES and NEVER_SUFFIXES are refused whatever an allowlist says:
     contracts/sources/ and every spreadsheet and Word document, because those are where
     the lab's own confidential material lives;
  3. the path is resolved with realpath, which collapses every symlink, and
  4. the REAL path must sit inside the REAL path of an allowed root, so a symlink
     pointing out of the tree fails at step 4 even though it looked innocent at step 1.

Only regular files are served; a directory or a device never is.
"""

from __future__ import annotations

import importlib.util
import os
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import chips, manifest, paths

#: What may be read out of the repo-wide trees (chip/, protocols/, cad/figures/).
#: Deliberately excludes .py: protocols/build_module1.py is code, not a spec.
SPEC_SUFFIXES = {".json", ".md"}
FIGURE_SUFFIXES = {".svg", ".png", ".json"}
EXPORT_SUFFIXES = {".stl", ".dxf", ".svg", ".png", ".json", ".md", ".gcode", ".step", ".3mf"}

#: Refused by every route, however it was reached. contracts/sources/ holds the material
#: CONFIDENTIALITY.md says never leaves the lab, and a spreadsheet or Word document is
#: never a web asset - if one is ever requested, something is wrong, not missing.
NEVER_PREFIXES = ("contracts/sources/", "private/", ".git/", ".github/", ".venv/",
                  "node_modules/")
NEVER_SUFFIXES = {".xlsx", ".xls", ".xlsm", ".xlsb", ".xltx", ".csv",
                  ".docx", ".doc", ".dotx", ".rtf", ".odt", ".ods",
                  ".ppt", ".pptx", ".pages", ".numbers", ".key",
                  ".env", ".pem", ".p12", ".pfx", ".sqlite", ".db"}

#: /docs/: text and still images, then the media a <video> or a 3D viewer needs.
DOC_SUFFIXES = {".json", ".md", ".txt", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif",
                ".ico"}
MEDIA_SUFFIXES = {".mp4", ".webm", ".mov", ".m4v", ".ogv", ".glb", ".gltf", ".bin"}

#: The directories /docs/<path> may read, as (pattern on the repo-relative path, what
#: may be read there, what it is). The work-directory patterns are anchored at both ends,
#: so `assay2chip/work/<id>/foam/render/...` is reachable and the rest of a run's work
#: directory is NOT - that stays behind /api/runs/<id>/file, which checks the run.
DOC_RULES = (
    (re.compile(r"^docs/(?!.*/\.\.?/).+$"), DOC_SUFFIXES | MEDIA_SUFFIXES, "docs/"),
    (re.compile(r"^assay2chip/work/[^/]+/foam/render/.+$"),
     DOC_SUFFIXES | MEDIA_SUFFIXES, "a run's 3D render output"),
    (re.compile(r"^assay2chip/work/[^/]+/cfd/figures/.+$"),
     {".svg", ".png", ".json"}, "a run's CFD figures"),
)

#: How long a derived twin allowlist is reused before it is worked out again.
TWIN_CACHE_SECONDS = 2.0


class Denied(Exception):
    """The request is outside the sandbox. The message is safe to show a user."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.message = message
        self.detail = detail


def real(path) -> str:
    return os.path.realpath(str(path))


def inside(root, candidate) -> bool:
    """True when `candidate` is `root` or sits under it, both already realpath'd."""
    root, candidate = str(root), str(candidate)
    if candidate == root:
        return True
    return candidate.startswith(root.rstrip(os.sep) + os.sep)


def check_request_path(raw: Optional[str]) -> str:
    """The syntactic half of the check. Returns a clean relative path or raises Denied."""
    if raw is None or str(raw).strip() == "":
        raise Denied("path is required", "give ?path=<repo-relative path>")
    text = str(raw).strip().replace("\\", "/")
    if "\x00" in text:
        raise Denied("path rejected: NUL byte")
    if text.startswith("/") or text.startswith("//"):
        raise Denied("path rejected: absolute paths are not served",
                     "ask for a path relative to the repository root")
    if len(text) > 2 and text[1] == ":":
        raise Denied("path rejected: absolute paths are not served", text[:2])
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise Denied("path rejected: '..' is not allowed",
                     "the sandbox serves only this run's files and the repo's specs")
    if not parts:
        raise Denied("path is required")
    return "/".join(parts)


def forbidden(rel: str) -> Optional[str]:
    """Why this path may never be served, or None. Checked before every allowlist."""
    posix = str(rel).lstrip("/")
    lowered = posix.lower()
    for prefix in NEVER_PREFIXES:
        if lowered.startswith(prefix):
            return "%s is never served by this server" % prefix
    suffix = Path(lowered).suffix
    if suffix in NEVER_SUFFIXES:
        return "%s files are never served by this server" % suffix
    return None


def allowed_roots(run_id: str) -> List[Tuple[str, str]]:
    """[(real root, why)] every directory this run may read from."""
    roots = [
        (real(paths.FIGURE_DIR), "cad/figures"),
        (real(paths.CHIP_DIR), "chip specs"),
        (real(paths.PROTOCOL_DIR), "protocol specs"),
    ]
    if run_id == paths.REPO_SCOPE:
        # The repo scope has no run, so "that run's chips" means the chips /api/chips
        # lists: hand-designed exports are exactly as public as the chip/ specs beside
        # them. A real run still only reaches the exports of the chips it produced.
        owned = chips.repo_chip_ids()
    else:
        roots.insert(0, (real(manifest.work_dir(run_id)), "this run's work directory"))
        owned = manifest.chip_ids(run_id)
    for chip in owned:
        export = paths.EXPORT_DIR / chip
        if export.is_dir():
            roots.append((real(export), "cad/exports/%s" % chip))
    return roots


def _suffix_ok(root_why: str, path: Path) -> bool:
    suffix = path.suffix.lower()
    if root_why in ("chip specs", "protocol specs"):
        return suffix in SPEC_SUFFIXES
    if root_why == "cad/figures":
        return suffix in FIGURE_SUFFIXES
    if root_why.startswith("cad/exports/"):
        return suffix in EXPORT_SUFFIXES
    return True


def resolve(run_id: str, raw: Optional[str]) -> Path:
    """The file /api/runs/<run_id>/file?path=<raw> may serve, or Denied."""
    rel = check_request_path(raw)
    why_not = forbidden(rel)
    if why_not:
        raise Denied("path rejected: %s" % why_not, rel)
    candidate = real(paths.ROOT / rel)

    listed = manifest.listed_paths(run_id) if run_id != paths.REPO_SCOPE else {}
    manifest_hit = rel in listed or paths.repo_rel(candidate) in listed

    why = None
    for root, reason in allowed_roots(run_id):
        if inside(root, candidate):
            if _suffix_ok(reason, Path(candidate)):
                why = reason
                break
            raise Denied(
                "path rejected: %s is not a servable file type under %s" % (
                    Path(rel).suffix or "that file", reason),
                rel)
    if why is None and manifest_hit:
        why = "listed in the run manifest"
    if why is None:
        raise Denied(
            "path rejected: outside this run's sandbox",
            "servable: this run's work directory, the paths its manifest lists, "
            "cad/figures/, cad/exports/<chip_id> for its chips, and chip/ and protocols/ "
            "specs",
        )

    path = Path(candidate)
    if not path.exists():
        raise Denied("not found: %s" % rel, "the sandbox allows it, but there is no such file")
    if not path.is_file():
        raise Denied("path rejected: not a regular file", rel)
    return path


def resolve_static(url_path: str) -> Path:
    """A file under web/static/, with the same realpath containment rule."""
    rel = url_path.lstrip("/")
    if rel in ("", "/"):
        rel = "index.html"
    rel = check_request_path(rel)
    candidate = Path(real(paths.STATIC / rel))
    if not inside(real(paths.STATIC), str(candidate)):
        raise Denied("path rejected: outside web/static", rel)
    if candidate.is_dir():
        candidate = Path(real(candidate / "index.html"))
        if not inside(real(paths.STATIC), str(candidate)):
            raise Denied("path rejected: outside web/static", rel)
    if not candidate.is_file():
        raise Denied("not found: %s" % rel)
    return candidate


# --- /docs/<path> ------------------------------------------------------------

def doc_roots() -> List[Tuple[str, str]]:
    """[(real root, why)] the directories /docs/<path> may read inside."""
    roots = [(real(paths.ROOT / "docs"), "docs/")]
    if paths.WORK.is_dir():
        for entry in sorted(paths.WORK.iterdir()):
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            for tail, why in (("foam/render", "a run's 3D render output"),
                              ("cfd/figures", "a run's CFD figures")):
                directory = entry / tail
                if directory.is_dir():
                    roots.append((real(directory), why))
    return roots


def resolve_doc(raw: Optional[str]) -> Path:
    """The file /docs/<path> may serve, given a repo-relative path, or Denied.

    `docs/modules.json`, `docs/videos/manifest.json` and the markdown under docs/, plus
    the render and CFD-figure files those documents point at - which live in a run's work
    directory and are named with its id, so they are matched by pattern and then proved
    to be inside a real `foam/render` or `cfd/figures` directory.
    """
    rel = check_request_path(raw)
    why_not = forbidden(rel)
    if why_not:
        raise Denied("path rejected: %s" % why_not, rel)

    suffix = Path(rel).suffix.lower()
    rule = None
    for pattern, suffixes, why in DOC_RULES:
        if pattern.match(rel):
            if suffix not in suffixes:
                raise Denied(
                    "path rejected: %s is not a servable file type under %s"
                    % (suffix or "that file", why),
                    "servable there: %s" % ", ".join(sorted(suffixes)))
            rule = why
            break
    if rule is None:
        raise Denied(
            "path rejected: outside the documentation sandbox",
            "servable: docs/, assay2chip/work/<id>/foam/render/ and "
            "assay2chip/work/<id>/cfd/figures/")

    candidate = real(paths.ROOT / rel)
    if not any(inside(root, candidate) for root, _ in doc_roots()):
        raise Denied("path rejected: outside the documentation sandbox",
                     "it resolves outside %s" % rule)
    # The same second look as the twin route: a symlink is followed, and then where it
    # LANDED has to satisfy the rules too, not just the name that was asked for.
    real_rel = paths.repo_rel(candidate)
    if real_rel != rel and not any(pattern.match(real_rel)
                                   for pattern, _, _ in DOC_RULES):
        raise Denied("path rejected: %s is a link out of the documentation sandbox" % rel,
                     "a symlink is followed and then checked again")
    path = Path(candidate)
    if not path.exists():
        raise Denied("not found: %s" % rel, "the sandbox allows it, but there is no such file")
    if not path.is_file():
        raise Denied("path rejected: not a regular file", rel)
    return path


# --- /twin/ and the repo paths the twin fetches ------------------------------

_twin_cache = {"root": None, "at": 0.0, "files": None, "source": None}


def _twin_fallback() -> Dict[str, str]:
    """The allowlist when tools/build_twin_site.py cannot be used.

    Same roots and same extensions as its ALLOWED_PREFIXES / DATA_EXTS, enumerated from
    the disk instead of derived from the twin's code. Narrower is better than wider here,
    so this lists files that exist rather than accepting a prefix.
    """
    found: Dict[str, str] = {}
    twin = paths.ROOT / "twin"
    if twin.is_dir():
        for path in sorted(twin.rglob("*")):
            if path.is_file() and not path.name.startswith("."):
                if path.suffix in {".html", ".js", ".css", ".json", ".md", ""}:
                    found[paths.repo_rel(path)] = "twin code"
    data_suffixes = {".json", ".md", ".dxf", ".stl", ".svg"}
    for pattern in ("runs/*.json", "chip/*.json", "chip/*/*.json", "protocols/*.json",
                    "contracts/blocks.json", "cad/*.json", "cad/library/*",
                    "cad/exports/*/*", "cad/figures/*.svg", "docs/modules.json",
                    "docs/demo/prompt_to_chip/*"):
        for path in sorted(paths.ROOT.glob(pattern)):
            if path.is_file() and path.suffix in data_suffixes:
                found.setdefault(paths.repo_rel(path), "data the twin may fetch")
    return found


_builders: Dict[str, object] = {}


def _load_builder(script: Path):
    """tools/build_twin_site.py, loaded from its file rather than imported by name.

    By path, not `import build_twin_site`: that keeps `tools/` off sys.path, and means a
    server pointed at a different checkout gets THAT checkout's builder instead of a
    cached one from somewhere else.
    """
    script = Path(script)
    stamp = "%s:%s" % (script, script.stat().st_mtime_ns)
    cached = _builders.get(stamp)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(
        "_a2c_twin_builder_%d" % abs(hash(stamp)), script)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "discover", None)):
        raise ImportError("%s has no discover()" % script)
    _builders.clear()                            # one checkout at a time is enough
    _builders[stamp] = module
    return module


def twin_files() -> Dict[str, str]:
    """{repo-relative path: why} - exactly what the twin is allowed to fetch.

    Derived by `tools/build_twin_site.discover()`, which reads the path literals out of
    twin/*.js and follows what the fetched data points to. Reusing it means this route
    and the public build agree by construction; if it cannot be imported (it is another
    agent's module and may be mid-edit) the fallback above is used and /api/health says
    which one is in force.
    """
    now = time.time()
    if (_twin_cache["root"] == str(paths.ROOT) and _twin_cache["files"] is not None
            and now - _twin_cache["at"] < TWIN_CACHE_SECONDS):
        return _twin_cache["files"]
    files, source = None, "tools/build_twin_site.discover()"
    try:
        module = _load_builder(paths.ROOT / "tools" / "build_twin_site.py")
        found = module.discover(paths.ROOT)
        if isinstance(found, dict) and found:
            files = {str(key): str(value) for key, value in found.items()}
    except Exception as problem:                 # another agent's module, mid-edit
        files, source = None, "fallback (%s: %s)" % (type(problem).__name__, problem)
    if files is None:
        files = _twin_fallback()
        if source.startswith("tools/"):
            source = "fallback (discover() returned nothing)"
    # Whatever it derived, the two hard rules still apply.
    files = {rel: why for rel, why in files.items() if not forbidden(rel)}
    _twin_cache.update(root=str(paths.ROOT), at=now, files=files, source=source)
    return files


def twin_source() -> str:
    twin_files()
    return str(_twin_cache["source"])


def resolve_twin(raw: Optional[str]) -> Path:
    """The file the twin may fetch at this repo-relative path, or Denied.

    Membership of the derived allowlist, not a prefix match: a file the twin's code never
    names is not served even if it sits beside one that is.
    """
    rel = check_request_path(raw)
    if rel in ("twin", "twin/"):
        rel = "twin/index.html"
    why_not = forbidden(rel)
    if why_not:
        raise Denied("path rejected: %s" % why_not, rel)
    allowed = twin_files()
    if rel not in allowed:
        raise Denied(
            "path rejected: the digital twin does not fetch %s" % rel,
            "the allowlist is derived from twin/*.js by "
            "tools/build_twin_site.discover(); %d paths are served" % len(allowed))
    candidate = real(paths.ROOT / rel)
    if not inside(real(paths.ROOT), candidate):
        raise Denied("path rejected: it resolves outside the repository", rel)
    # After realpath, the file's OWN path must be on the allowlist too. Without this a
    # symlink at an allowed name (twin/anything.js) would hand out whatever it points
    # at, and pointing it somewhere else in the repo would pass a root check.
    real_rel = paths.repo_rel(candidate)
    if real_rel != rel and real_rel not in allowed:
        raise Denied(
            "path rejected: %s is a link to a file the digital twin does not fetch" % rel,
            "a symlink is followed and then checked again against the allowlist")
    path = Path(candidate)
    if not path.is_file():
        raise Denied("not found: %s" % rel, "it is on the allowlist but not on disk")
    return path


def upload_target(run_id: str, filename: Optional[str]) -> Path:
    """assay2chip/work/<run_id>/upload/<name>, proven to be inside that directory.

    Uploads go here and nowhere else, so the id and the name are both sanitised and the
    result is re-checked with realpath before anything is written.
    """
    safe_id = paths.sanitise_id(run_id)
    base = manifest.work_dir(safe_id) / paths.UPLOAD_DIRNAME
    name = paths.safe_filename(filename)
    base.mkdir(parents=True, exist_ok=True)
    target = Path(real(base / name))
    if not inside(real(base), str(target)) or target.name != name:
        raise Denied("upload rejected: %r is not a usable file name" % filename)
    if target.is_dir():
        raise Denied("upload rejected: %s is a directory" % name)
    return target

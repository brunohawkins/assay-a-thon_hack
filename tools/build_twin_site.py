"""Build the PUBLIC digital-twin site: an allow-listed static bundle in _site/.

    python tools/build_twin_site.py               # scan, print the public manifest, write _site/
    python tools/build_twin_site.py --dry-run     # scan and print the manifest only
    python tools/build_twin_site.py --out DIR
    python tools/build_twin_site.py --videos DIR  # publish the demo videos downloaded into DIR

The repo is private; the twin is public (GitHub Pages). So this never copies the repo. The
bundle is exactly:

  twin/                     the twin's own code (html, js, css, vendored three.js)
  docs/modules.json         the module catalogue (tools/module_catalog.py)
  docs/videos/manifest.json the video slots, urls rewritten to the site copies
  videos/<chip_id>.<ext>    only with --videos DIR: the recorded videos, which CI downloads
                            from the private repo's `demo-videos` release at build time. A
                            private release url is never published: a slot whose file is not
                            in the bundle gets url null.
  + the data files the twin fetches, worked out from its code, not listed by hand:
    - path literals and resolved template literals in twin/*.js (e.g. runs/index.json,
      protocols/pipeline_mvp.json, contracts/blocks.json, cad/exports/module1_v1/module1_v1.dxf)
    - bare file names the code joins onto a directory constant (prompt_to_chip/design.json)
    - what those files point to: every run in runs/index.json and its chip_spec, every
      pipeline's module chips, the library STLs for the blocks those chips use

Fetches that need a directory listing (STL folders, cad/display/) find none on a static host
and the twin already falls back without them, so nothing is added for them.

Before anything is written, a confidentiality scan runs over every file in the bundle and the
build REFUSES (exit 1, nothing written) on: an .xlsx/.xls/.docx/.doc/.pdf/.ppt(x) file,
anything under contracts/sources/ or any text that refers to it, any spreadsheet file NAME
(foo.xlsx) in the text, an absolute or user path, a price or currency amount, a
vendor or catalogue-number pattern, a lab-system name (Labstep, OneDrive) or a cell-line
identifier. The allowed-roots fence below is a second guard: a discovered path outside it
is refused too.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TWIN = "twin"
CATALOG = "docs/modules.json"
VIDEO_MANIFEST = "docs/videos/manifest.json"
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".m4v"}
MARKER = ".twin-site"

# Second fence: nothing outside these is ever published, whatever the code seems to fetch.
ALLOWED_PREFIXES = (
    "twin/", "runs/", "chip/", "protocols/", "cad/exports/", "cad/library/", "cad/",
    "contracts/blocks.json", "docs/modules.json", "docs/videos/manifest.json", "docs/demo/prompt_to_chip/",
    "videos/",
)
NEVER_PREFIXES = ("contracts/sources/", "private/", ".git/", ".github/", "assay2chip/work/")
TWIN_EXTS = {".html", ".js", ".css", ".json", ".md", ""}  # "" for vendor LICENSE
DATA_EXTS = {".json", ".md", ".dxf", ".stl", ".svg"}
BLOCKED_EXTS = {".xlsx", ".xls", ".xlsm", ".docx", ".doc", ".pdf", ".ppt", ".pptx", ".csv"}

# Fetched files the twin reads only a few fields of: published REDUCED to exactly those fields
# (a dotted path; "[]" walks a list), so prose the twin never shows stays private. Each field is
# named in the twin code at the place given (tests/test_twin_site.py checks the names are there).
# A chip spec the twin draws whole ("chip": true) keeps only the top-level keys the twin code
# names anywhere, and its description is cut to the first sentence, which is all the twin shows
# (twin/pipeline3d.js firstSentence).
REDUCE: dict[str, dict] = {
    "contracts/blocks.json": {"keep": ["fabrication_floors.min_port_mm"], "read_by": "twin/module1.js"},
    "docs/demo/prompt_to_chip/design.json": {"keep": ["modules[].steps"], "read_by": "twin/presenter.js"},
    "chip/modules/module3_step1_3.json": {"keep": ["workflow", "interfaces.downstream"], "read_by": "twin/main.js"},
    "chip/module3_v1_translated.json": {"chip": True, "read_by": "twin/*.js"},
}

PATH_RE = re.compile(r"^(?:\.\./)?((?:chip|protocols|runs|contracts|cad|docs)/[\w./-]+\.(?:json|md|dxf|stl|svg))$")

SCAN_RULES: list[tuple[str, re.Pattern]] = [
    ("absolute or user path", re.compile(
        r"(?<![\w.:/])(?:/Users/|/home/\w|/private/var/|/var/folders/|/Volumes/|/mnt/\w)"
        r"|\b[A-Za-z]:\\\\?(?:Users|Documents)|file:///|(?<![\w/])~/\w")),
    ("spreadsheet file name", re.compile(r"[\w.-]+\.(?:xlsx|xlsm|xls|ods|numbers)\b", re.I)),
    ("lab system", re.compile(r"\b(?:labstep|onedrive|sharepoint)\b", re.I)),
    ("price", re.compile(
        r"[£€]\s?\d|\$\s?\d[\d,]*\.\d\d\b|\b\d[\d,.]*\s?(?:USD|EUR|GBP|CHF)\b|\b(?:USD|EUR|GBP|CHF)\s?\d"
        r"|\b(?:price[sd]?|pricing|quotation|unit cost|list cost)\b", re.I)),
    ("vendor", re.compile(
        r"\b(?:thermo ?fisher|fisher scientific|sigma[- ]aldrich|merck|millipore|illumina|new england biolabs"
        r"|qiagen|bio-rad|takara|agilent|beckman|corning|gibco|invitrogen|lonza|stemcell technologies"
        r"|10x genomics|integrated dna technologies|vwr|abcam|promega|roche|miltenyi|dolomite|fluigent"
        r"|elveflow|eppendorf|zymo|lucigen|biolegend|peprotech|r&d systems|cellink|formlabs)\b"
        r"|\bcat(?:alogue|alog)?\.? ?(?:no\.?|number|#)\s*[A-Z0-9-]{3,}", re.I)),
    ("cell line", re.compile(r"\b(?:WTC-?11|KOLF2(?:\.1J)?|HPSI\d{3,}|BIONi\d+|IMR-?90|PGP1|HipSci)\b")),
]


# ---------------------------------------------------------------- discovery


def _strings(js: str):
    """String literals in JS source: ('quote', text). Template literals keep their ${...}."""
    for m in re.finditer(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"|`((?:[^`\\]|\\.)*)`", js):
        if m.group(1) is not None:
            yield m.group(1)
        elif m.group(2) is not None:
            yield m.group(2)
        else:
            yield m.group(3)


def _constants(sources: dict[str, str]) -> dict[str, str]:
    consts: dict[str, str] = {"root": "", "ROOT": ""}  # the twin serves the repo root as '../'
    pat = re.compile(r"\b(?:const|let|var)\s+(\w+)\s*=\s*(?:'([^'\n]*)'|\"([^\"\n]*)\"|`([^`\n]*)`)")
    raw: dict[str, str] = {}
    for js in sources.values():
        for m in pat.finditer(js):
            raw.setdefault(m.group(1), next(g for g in m.groups()[1:] if g is not None))
    for _ in range(5):  # resolve constants built from constants
        for k, v in raw.items():
            r = _resolve(v, consts)
            if r is not None and "${" not in r:
                consts.setdefault(k, r)
    for k in ("root", "ROOT"):
        consts[k] = ""
    return consts


def _resolve(text: str, consts: dict[str, str]) -> str | None:
    out, ok = text, True

    def sub(m):
        nonlocal ok
        name = m.group(1).strip()
        if name in consts:
            return consts[name]
        ok = False
        return m.group(0)
    out = re.sub(r"\$\{\s*([\w.]+)\s*\}", sub, out)
    out = out.replace("../", "", 1) if out.startswith("../") else out
    return out if ok else None


def discover(root: Path = ROOT) -> dict[str, str]:
    """Every file the public bundle holds, mapped to why it is there."""
    root = Path(root)
    twin = root / TWIN
    files: dict[str, str] = {}
    if not twin.is_dir():
        return files
    for p in sorted(twin.rglob("*")):
        if p.is_file() and p.suffix in TWIN_EXTS and not p.name.startswith("."):
            files[p.relative_to(root).as_posix()] = "twin code"
    sources = {p.as_posix(): p.read_text(errors="replace") for p in sorted(twin.glob("*.js"))}
    consts = _constants(sources)
    data: dict[str, str] = {}

    def add(rel: str | None, why: str):
        if not rel:
            return
        rel = rel.lstrip("./") if rel.startswith("./") else rel
        if rel in data or rel in files:
            return
        if (root / rel).is_file() and Path(rel).suffix in DATA_EXTS:
            data[rel] = why

    dirs = sorted({v for v in consts.values() if v.endswith("/") and (root / v).is_dir()})
    for js_path, js in sources.items():
        name = Path(js_path).name
        for s in _strings(js):
            r = _resolve(s, consts) if "${" in s else s
            if r is None:
                continue
            m = PATH_RE.match(r)
            if m:
                add(m.group(1), f"fetched by twin/{name}")
            elif re.fullmatch(r"[\w-]+\.(?:json|md)", r):
                for d in dirs:  # a bare name the code joins onto a directory constant
                    add(f"{d}{r}", f"twin/{name} joins it onto {d}")
    add(CATALOG, "module catalogue (tools/module_catalog.py)")
    add(VIDEO_MANIFEST, "video slots (urls rewritten to the site copies)")

    # Follow what the fetched data points to.
    changed = True
    while changed:
        changed = False
        before = len(data)
        for rel in sorted(data):
            if not rel.endswith(".json"):
                continue
            try:
                doc = json.loads((root / rel).read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if rel == "runs/index.json":
                for r in doc.get("runs") or []:
                    add(f"runs/{r.get('file')}", "run listed in runs/index.json")
                    add(r.get("chip_spec"), "chip_spec of a listed run")
            elif rel.startswith("runs/") and isinstance(doc, dict):
                add(doc.get("chip_spec"), f"chip_spec of {rel}")
            if isinstance(doc, dict) and doc.get("kind") == "pipeline":
                for m in doc.get("modules") or []:
                    add(m.get("chip"), f"module chip of {rel}")
            if rel == "chip/components/catalogue.json":
                blocks = set()
                for other in data:
                    if other.startswith(("chip/", "cad/", "docs/demo/")) and other.endswith(".json"):
                        try:
                            spec = json.loads((root / other).read_text())
                        except (OSError, json.JSONDecodeError):
                            continue
                        if isinstance(spec, dict):
                            blocks |= {c.get("block") for c in spec.get("chambers") or [] if isinstance(c, dict)}
                for part in doc.get("parts") or []:
                    if part.get("part") in blocks:
                        add(part.get("stl"), "library STL for a block a bundled chip uses")
        changed = len(data) != before
    files.update(data)
    return dict(sorted(files.items()))


def discover_videos(root: Path, video_dir: Path | None) -> dict[str, Path]:
    """site path -> downloaded file, for each video slot whose asset is in video_dir. Only
    `<chip_id>.<video ext>` names of real slots are taken; anything else there is ignored."""
    if not video_dir or not Path(video_dir).is_dir():
        return {}
    mpath = Path(root) / VIDEO_MANIFEST
    slots = json.loads(mpath.read_text()).get("videos", []) if mpath.is_file() else []
    out: dict[str, Path] = {}
    for v in slots:
        chip = v.get("chip_id") if isinstance(v, dict) else None
        if not chip:
            continue
        hits = sorted(p for p in Path(video_dir).iterdir()
                      if p.is_file() and p.stem == chip and p.suffix.lower() in VIDEO_EXTS)
        if hits:
            out[f"videos/{chip}{hits[0].suffix.lower()}"] = hits[0]
    return out


def video_urls(videos: dict[str, Path]) -> dict[str, str]:
    return {Path(site).stem: site for site in videos}


# ---------------------------------------------------------------- payload


def _keep(doc, path: list[str]):
    """The part of `doc` on one dotted path, as a nested structure of the same shape."""
    if not path:
        return doc
    head, rest = path[0], path[1:]
    if head.endswith("[]"):
        key = head[:-2]
        items = doc.get(key) if isinstance(doc, dict) else None
        if not isinstance(items, list):
            return None
        return {key: [_keep(x, rest) if rest else x for x in items]}
    if not isinstance(doc, dict) or head not in doc:
        return None
    return {head: _keep(doc[head], rest) if rest else doc[head]}


def _merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _merge(out[k], v) if k in out else v
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [_merge(x, y) for x, y in zip(a, b)]
    return b if b is not None else a


_FIRST_SENTENCE = re.compile(r"^.*?[.!?](?:\s|$)", re.S)  # as twin/pipeline3d.js firstSentence


def _twin_identifiers(root: Path) -> set[str]:
    words: set[str] = set()
    for js in sorted((Path(root) / TWIN).glob("*.js")):
        words |= set(re.findall(r"[A-Za-z_]\w*", js.read_text(errors="replace")))
    return words


def _reduce_chip(root: Path, doc: dict) -> dict:
    names = _twin_identifiers(root)
    out = {k: v for k, v in doc.items() if k in names}
    if isinstance(out.get("description"), str):
        m = _FIRST_SENTENCE.match(out["description"])
        out["description"] = (m.group(0) if m else out["description"]).strip()
    return out


def _video_url(slot: dict, videos: dict[str, str] | None) -> str | None:
    """The site-relative url of a slot's video, or None: never a private release url."""
    return (videos or {}).get(slot.get("chip_id"))


def _rewrite_videos(rel: str, doc, videos: dict[str, str] | None):
    if rel == VIDEO_MANIFEST and isinstance(doc, dict):
        for v in doc.get("videos") or []:
            if isinstance(v, dict):
                v["url"] = _video_url(v, videos)
                v["recorded"] = v["url"] is not None
        doc["about"] = "Video slots of the printed demo chips; url is relative to the site root."
    elif rel == CATALOG and isinstance(doc, dict):
        for s in (doc.get("stages") or []) + (doc.get("not_in_pipeline") or []):
            v = (s.get("module") or {}).get("video") if isinstance(s, dict) else None
            if isinstance(v, dict):
                v["url"] = _video_url(v, videos)
                v["recorded"] = v["url"] is not None
    return doc


def payload(root: Path, rel: str, videos: dict[str, str] | None = None) -> bytes:
    """The bytes that go public for `rel`: the file itself, its REDUCE subset, or (manifest and
    catalogue) the file with every video url rewritten to the site copy or null."""
    raw = (Path(root) / rel).read_bytes()
    if rel in (VIDEO_MANIFEST, CATALOG):
        doc = _rewrite_videos(rel, json.loads(raw), videos)
        return (json.dumps(doc, indent=2, ensure_ascii=False) + "\n").encode()
    rule = REDUCE.get(rel)
    if not rule:
        return raw
    doc = json.loads(raw)
    if rule.get("chip"):
        out = _reduce_chip(root, doc)
        out["_published"] = "reduced by tools/build_twin_site.py to the keys the twin reads"
        return (json.dumps(out, indent=1, sort_keys=True) + "\n").encode()
    out: dict = {}
    for path in rule["keep"]:
        part = _keep(doc, re.split(r"\.(?![^\[]*\])", path))
        if part is not None:
            out = _merge(out, part)
    out["_published"] = f"reduced by tools/build_twin_site.py to the fields {rule['read_by']} reads"
    return (json.dumps(out, indent=1, sort_keys=True) + "\n").encode()


# ---------------------------------------------------------------- scan


def scan(root: Path, files, videos: dict[str, Path] | None = None) -> list[str]:
    """Confidentiality problems in the bundle; empty means it may go public."""
    root = Path(root)
    problems = []
    for site, src in sorted((videos or {}).items()):
        head = Path(src).read_bytes()[:8]
        if not site.startswith("videos/") or Path(site).suffix not in VIDEO_EXTS:
            problems.append(f"{site}: not a video slot")
        elif head[:4] == b"PK\x03\x04" or head[:5] == b"%PDF-" or head == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            problems.append(f"{site}: office/PDF content behind a video extension")
    urls = video_urls(videos or {})
    source_names = sorted(p.name for p in (root / "contracts" / "sources").glob("*") if p.is_file())
    for rel in sorted(files):
        p = Path(rel)
        if p.suffix.lower() in BLOCKED_EXTS:
            problems.append(f"{rel}: blocked file type {p.suffix}")
            continue
        if rel.startswith(NEVER_PREFIXES) or "/contracts/sources/" in f"/{rel}":
            problems.append(f"{rel}: under a never-publish folder")
            continue
        if not rel.startswith(ALLOWED_PREFIXES):
            problems.append(f"{rel}: outside the allowed roots")
            continue
        try:
            raw = payload(root, rel, urls)
        except (OSError, ValueError) as e:
            problems.append(f"{rel}: unreadable ({e})")
            continue
        if raw[:4] == b"PK\x03\x04" or raw[:5] == b"%PDF-" or raw[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            problems.append(f"{rel}: office/PDF content behind a {p.suffix or 'no'} extension")
            continue
        if p.suffix.lower() == ".stl" and not raw[:5].lower() == b"solid":
            continue  # binary STL: triangles only, no text to leak
        text = raw.decode("utf-8", errors="replace")
        if "contracts/sources/" in text:
            problems.append(f"{rel}: refers to contracts/sources/")
        for name in source_names:
            if name in text:
                problems.append(f"{rel}: names {name}, a file kept in contracts/sources/")
        for label, rx in SCAN_RULES:
            m = rx.search(text)
            if m:
                line = text.count("\n", 0, m.start()) + 1
                problems.append(f"{rel}:{line}: {label}: {m.group(0)!r}")
    return problems


def warnings(root: Path, files) -> list[str]:
    """Not refusals, but worth a human look: the bundle names a document file (.docx, .pdf).
    Spreadsheet names and contracts/sources/ file names are refusals (scan)."""
    root = Path(root)
    doc_name = re.compile(r"[\w.-]+\.(?:docx|doc|pdf|pptx)\b", re.I)
    out = []
    for rel in sorted(files):
        if rel.startswith("twin/vendor/") or rel.endswith(".stl"):
            continue
        try:
            text = payload(root, rel).decode("utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        hits = sorted(set(doc_name.findall(text)))
        if hits:
            out.append(f"{rel}: names {', '.join(hits)}")
    return out


# ---------------------------------------------------------------- build


REDIRECT = ('<!doctype html><meta charset="utf-8"><title>Capsule Processor twin</title>'
            '<meta http-equiv="refresh" content="0; url=twin/"><a href="twin/">Open the twin</a>\n')


def manifest_lines(root: Path, files: dict[str, str], videos: dict[str, Path] | None = None) -> list[str]:
    total = 0
    lines = ["PUBLIC (everything below goes on GitHub Pages):"]
    urls = video_urls(videos or {})
    for site, src in sorted((videos or {}).items()):
        size = Path(src).stat().st_size
        total += size
        lines.append(f"  {size:>10,}  {site}    [demo video from the demo-videos release]")
    for rel, why in files.items():
        size = len(payload(root, rel, urls))
        total += size
        rule = REDUCE.get(rel)
        note = "" if not rule else ("; REDUCED to the keys the twin reads, description cut to one sentence"
                                    if rule.get("chip") else f"; REDUCED to {', '.join(rule['keep'])}")
        lines.append(f"  {size:>10,}  {rel}    [{why}{note}]")
    lines.append(f"  {len(REDIRECT):>10,}  index.html    [generated redirect to twin/]")
    lines.append(f"  {0:>10,}  .nojekyll    [generated]")
    total += len(REDIRECT)
    lines.append(f"{len(files) + len(videos or {}) + 2} files, {total:,} bytes ({total / 1e6:.2f} MB)")
    return lines


def build(root: Path = ROOT, out: Path | None = None, dry_run: bool = False, echo=print,
          video_dir: Path | None = None) -> int:
    root = Path(root)
    out = Path(out) if out else root / "_site"
    files = discover(root)
    if not files:
        echo("refused: no twin/ found")
        return 1
    videos = discover_videos(root, video_dir)
    if video_dir and Path(video_dir).resolve().is_relative_to(out.resolve()):
        echo("refused: --videos must not be inside --out (the output folder is wiped)")
        return 1
    problems = scan(root, files, videos)
    for line in manifest_lines(root, files, videos):
        echo(line)
    for w in warnings(root, files):
        echo(f"warning: {w}")
    if problems:
        echo("REFUSED by the confidentiality scan; nothing written:")
        for p in problems:
            echo(f"  {p}")
        return 1
    if dry_run:
        echo("dry run: nothing written")
        return 0
    if out.exists():
        if not (out / MARKER).exists() and any(out.iterdir()):
            echo(f"refused: {out} exists and was not made by this tool")
            return 1
        shutil.rmtree(out)
    urls = video_urls(videos)
    for rel in files:
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(payload(root, rel, urls))
    for site, src in videos.items():
        dst = out / site
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    (out / "index.html").write_text(REDIRECT)
    (out / ".nojekyll").write_text("")
    (out / MARKER).write_text("built by tools/build_twin_site.py\n")
    echo(f"wrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--out", type=Path, default=None, help="output folder (default _site/)")
    ap.add_argument("--dry-run", action="store_true", help="scan and print the manifest only")
    ap.add_argument("--videos", type=Path, default=None,
                    help="folder holding the downloaded demo-videos release assets (CI)")
    args = ap.parse_args(argv)
    return build(ROOT, args.out, args.dry_run, video_dir=args.videos)


if __name__ == "__main__":
    sys.exit(main())

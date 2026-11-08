"""The stage spine, for /api/stages and for knowing what a run is going to do.

The authority is assay2chip/README.md and the SPINE table in assay2chip/run.py. This is
the same list with a one-line description each, kept here rather than imported because
the server is standard-library only (importing assay2chip pulls in jsonschema) and must
keep answering while the stage modules are half written.

`implemented` is read off the disk, so a stage that lands while the server is running is
reported the moment it exists.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from . import paths

# number, name, one-line description, what it writes
SPINE = [
    (0, "ingest", "Read the source (PDF, markdown, text, DOI or URL) into text, tables "
                  "and provenance.", "assay2chip/work/<id>/raw.json"),
    (1, "extract", "Pull the ordered wet-lab steps out of the source, with a confidence "
                   "and the questions it could not answer.", "assay2chip/work/<id>/assay.json"),
    (2, "normalise", "Turn lab language into canonical unit operations with fluids, "
                     "particles, durations and temperatures.", "assay2chip/work/<id>/unitops.json"),
    (3, "triage", "Decide per operation: on chip, off chip, or no part exists for it.",
     "assay2chip/work/<id>/unitops.json (placement)"),
    (4, "partition", "Cut the assay across as many chips as it needs and say what carries "
                     "between them.", "assay2chip/work/<id>/partition.json"),
    (5, "group", "Write each chip's design file in the existing chip/designs format.",
     "chip/designs/<chip_id>.json"),
    (6, "build", "Size every part from physics and place it inside the fabrication "
                 "envelope.", "chip/<chip_id>.json (unrouted)"),
    (7, "route", "Route planar channels with no crossing, at one etch depth.",
     "chip/<chip_id>.json (routed)"),
    (8, "protocol", "Derive the flows and the step schedule from the geometry that was "
                    "actually built.", "protocols/<chip_id>.json"),
    (9, "verify", "Run validate, check_chip_spec and the figures; zero errors or the run "
                  "has failed.", "assay2chip/work/<id>/verify.json, cad/figures/"),
    (10, "cfd", "Simulate the whole chip set end to end, find leaks, traps and shear, and "
                "propose fixes.", "assay2chip/work/<id>/cfd/"),
    (11, "fabricate", "Export the STLs, the mould, the closed DXF and the renders.",
     "cad/exports/<chip_id>/, print/"),
    (12, "report", "Write up the problems, the fixes and every artefact the run produced.",
     "assay2chip/work/<id>/report.md, report.json"),
]

#: run.py's stage statuses mapped onto the API's vocabulary (web/README.md).
#: Anything unknown is reported as failed rather than quietly passing as done.
STATUS_MAP = {
    "ok": "done",
    "done": "done",
    "not_implemented": "not_implemented",
    "skipped": "skipped",
    "pending": "pending",
    "running": "running",
    "error": "failed",
    "refused": "failed",
    "wrote_nothing": "failed",
    "failed": "failed",
}
API_STATUSES = ("pending", "running", "done", "failed", "skipped", "not_implemented")
TERMINAL = ("done", "failed", "skipped", "not_implemented")


def api_status(raw: Optional[str]) -> str:
    """run.py's status word as one of web/README.md's six."""
    return STATUS_MAP.get(str(raw or "pending").strip().lower(), "failed")


def token(number: int) -> str:
    return "s%d" % number


def label(number: int, name: str) -> str:
    return "s%d_%s" % (number, name)


def modules_on_disk() -> Dict[int, str]:
    """{stage number: module file name} for the stage modules that exist right now."""
    found = {}
    stage_dir = paths.ROOT / "assay2chip" / "stages"
    if not stage_dir.is_dir():
        return found
    for path in sorted(stage_dir.glob("s*.py")):
        match = re.match(r"^s(\d+)_[a-z0-9_]+$", path.stem)
        if match and not path.stem.startswith("_"):
            found.setdefault(int(match.group(1)), path.name)
    return found


def listing() -> List[dict]:
    """The ordered stage list /api/stages returns."""
    on_disk = modules_on_disk()
    out = []
    for number, name, description, writes in SPINE:
        module = on_disk.get(number)
        out.append({
            "stage": token(number),
            "number": number,
            "name": name,
            "label": label(number, name),
            "description": description,
            "writes": writes,
            "implemented": module is not None,
            "module": ("assay2chip/stages/%s" % module) if module else None,
        })
    return out


def tokens() -> List[str]:
    return [token(n) for n, _, _, _ in SPINE]


def labels() -> List[str]:
    return [label(n, name) for n, name, _, _ in SPINE]


def resolve(value: Optional[str]) -> Optional[int]:
    """The index in SPINE a --from / --to value names: 's6', '6', 's6_build' or 'build'."""
    if value is None or str(value).strip() == "":
        return None
    want = str(value).strip().lower()
    if want.endswith(".py"):
        want = want[:-3]
    for index, (number, name, _, _) in enumerate(SPINE):
        if want in (token(number), str(number), label(number, name), name):
            return index
    return None


def plan(from_value: Optional[str] = None, to_value: Optional[str] = None) -> List[dict]:
    """The stages a run with these --from/--to values will execute, in numeric order."""
    start = resolve(from_value) or 0
    stop = resolve(to_value)
    stop = len(SPINE) - 1 if stop is None else stop
    rows = listing()
    if stop < start:
        return []
    return rows[start:stop + 1]

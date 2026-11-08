"""Every chip in the repo, with its footprint and which scale it is.

Chips are discovered the way tools/check_chip_spec.py discovers them - `chip/*.json` and
`cad/*_spec.json` - so a chip somebody adds shows up without touching this file.

The scale comes from `contracts/blocks.json`, never from a literal here: DEMO is
`envelope.max_x_mm/max_y_mm`, TARGET is `target_envelope.x_mm/y_mm` and the agreed
ceiling is `target_envelope.max_allowed_x_mm/max_allowed_y_mm`. A duplicated contract
number is a bug this repo has been bitten by, and a demo chip shown without its DEMO
SCALE label is how a 30 x 20 mm test coupon gets mistaken for the real device.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from . import manifest, paths

#: Floating point slack when comparing a footprint with an envelope, in mm.
TOLERANCE = 1e-6

_cache = {"stamp": None, "envelopes": None}


def envelopes() -> dict:
    """{demo, target, ceiling} in mm, read from contracts/blocks.json and cached."""
    path = paths.BLOCKS_JSON
    try:
        stamp = (str(path), path.stat().st_mtime_ns)
    except OSError:
        stamp = (str(path), None)
    if _cache["stamp"] == stamp and _cache["envelopes"] is not None:
        return _cache["envelopes"]
    blocks = manifest.read_json(path) or {}
    envelope = blocks.get("envelope") or {}
    target = blocks.get("target_envelope") or {}
    found = {
        "demo": _xy(envelope.get("max_x_mm"), envelope.get("max_y_mm")),
        "target": _xy(target.get("x_mm"), target.get("y_mm")),
        "ceiling": _xy(target.get("max_allowed_x_mm"), target.get("max_allowed_y_mm")),
        "source": paths.repo_rel(path),
        "available": bool(envelope or target),
    }
    _cache.update(stamp=stamp, envelopes=found)
    return found


def _xy(x, y) -> Optional[dict]:
    try:
        return {"x_mm": float(x), "y_mm": float(y)}
    except (TypeError, ValueError):
        return None


def footprint_of(spec: dict) -> Optional[dict]:
    value = spec.get("footprint")
    if isinstance(value, dict):
        return _xy(value.get("x", value.get("x_mm")), value.get("y", value.get("y_mm")))
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return _xy(value[0], value[1])
    return None


def _fits(footprint: dict, envelope: Optional[dict]) -> bool:
    if not envelope:
        return False
    return (footprint["x_mm"] <= envelope["x_mm"] + TOLERANCE
            and footprint["y_mm"] <= envelope["y_mm"] + TOLERANCE)


def classify(footprint: Optional[dict]) -> dict:
    """Which of the three scales a footprint is, and the label it must be shown with."""
    limits = envelopes()
    if not footprint:
        return {"scale": "unknown", "demo": False, "label": "SCALE UNKNOWN",
                "note": "the spec has no footprint"}
    if not limits["available"]:
        return {"scale": "unknown", "demo": False, "label": "SCALE UNKNOWN",
                "note": "%s is missing, so no scale can be decided"
                        % paths.repo_rel(paths.BLOCKS_JSON)}
    if _fits(footprint, limits["demo"]):
        return {"scale": "demo", "demo": True, "label": "DEMO SCALE",
                "note": "fits the %g x %g mm demo envelope: a test coupon, not the real "
                        "device" % (limits["demo"]["x_mm"], limits["demo"]["y_mm"])}
    if _fits(footprint, limits["target"]):
        return {"scale": "target", "demo": False, "label": "TARGET SCALE",
                "note": "fits the ANSI/SLAS microplate footprint %g x %g mm"
                        % (limits["target"]["x_mm"], limits["target"]["y_mm"])}
    if _fits(footprint, limits["ceiling"]):
        return {"scale": "over_target", "demo": False, "label": "OVER TARGET",
                "note": "bigger than the microplate footprint but inside the agreed "
                        "ceiling"}
    return {"scale": "oversize", "demo": False, "label": "OVER THE CEILING",
            "note": "outside the agreed maximum chip size: this cannot be fabricated"}


def spec_paths() -> List[Path]:
    """chip/*.json then cad/*_spec.json, the same set tools/check_chip_spec.py uses."""
    found = []
    if paths.CHIP_DIR.is_dir():
        found += sorted(paths.CHIP_DIR.glob("*.json"))
    if paths.CAD_DIR.is_dir():
        found += sorted(paths.CAD_DIR.glob("*_spec.json"))
    return found


def figures_for(chip_id: str) -> List[str]:
    """The plan-view and stack SVGs cad/make_figures.py writes for a chip."""
    if not paths.FIGURE_DIR.is_dir() or not chip_id:
        return []
    found = set()
    for pattern in ("%s_*.svg" % chip_id, "%s.svg" % chip_id, "%s_*.png" % chip_id):
        for path in paths.FIGURE_DIR.glob(pattern):
            if path.is_file():
                found.add(paths.repo_rel(path))
    return sorted(found)


def exports_for(chip_id: str) -> List[str]:
    directory = paths.EXPORT_DIR / chip_id
    if not chip_id or not directory.is_dir():
        return []
    return sorted(paths.repo_rel(path) for path in directory.rglob("*") if path.is_file())


def row_for(path: Path) -> Optional[dict]:
    spec = manifest.read_json(path)
    if not isinstance(spec, dict):
        return None
    chip_id = str(spec.get("id") or path.stem)
    footprint = footprint_of(spec)
    scale = classify(footprint)
    return {
        "id": chip_id,
        "path": paths.repo_rel(path),
        "name": spec.get("name") or chip_id,
        "footprint": footprint or {},
        "units": spec.get("units", "mm"),
        "scale": scale["scale"],
        "demo": scale["demo"],
        "scale_label": scale["label"],
        "scale_note": scale["note"],
        "placeholder": bool(spec.get("placeholder")),
        "source": "chip" if paths.repo_rel(path).startswith("chip/") else "cad",
        "counts": {
            "chambers": len(spec.get("chambers") or []),
            "channels": len(spec.get("channels") or []),
            "ports": len(spec.get("ports") or []),
        },
        "figures": figures_for(chip_id),
        "exports": exports_for(chip_id),
    }


def listing() -> List[dict]:
    rows = []
    seen = set()
    for path in spec_paths():
        row = row_for(path)
        if row is None:
            continue
        key = (row["id"], row["path"])
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    rows.sort(key=lambda item: (item["source"] != "chip", item["id"]))
    return rows


def by_id() -> Dict[str, dict]:
    return {row["id"]: row for row in listing()}


def repo_chip_ids() -> List[str]:
    return sorted({row["id"] for row in listing()})


def find(chip_id: str) -> Optional[dict]:
    """(row, spec) for a chip id, by id and then by file stem."""
    wanted = str(chip_id or "").strip()
    for path in spec_paths():
        spec = manifest.read_json(path)
        if not isinstance(spec, dict):
            continue
        if str(spec.get("id") or "") == wanted or path.stem == wanted:
            row = row_for(path)
            if row is not None:
                return {"row": row, "spec": spec, "path": path}
    return None

"""manifest.json: what is in the bundle, what passed, and what scale the chip is.

Deterministic on purpose - no timestamps, no paths outside the repo - so the same spec
gives byte-identical bytes and a diff always means the geometry changed. That is the rule
S0 already follows for raw.json.

The first key is `scale`, and it is first because it is the one that can do damage. A
30 x 20 mm demo chip and the 127.76 x 85.48 mm device share every file name, every layer
name and most of their numbers; a manifest that does not shout which one it is describing
is how a test article ends up quoted as the product. `checks.scale_of()` derives it from
contracts/blocks.json and the warning text is carried into the Fusion script and onto both
renders, so the files say it too.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402
from assay2chip.lib.io import sha256_file  # noqa: E402

FABRICATION = ROOT / "cad" / "fabrication.json"


def _rel(path: Path) -> str:
    """Repo-relative when it is in the repo, absolute when a test points it elsewhere."""
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path.resolve())


def file_records(files: list) -> list:
    """One record per written file: name, role, size and sha256, so a bundle can be
    checked against what S11 says it wrote."""
    out = []
    for path, role in files:
        path = Path(path)
        out.append({
            "file": path.name,
            "role": role,
            "bytes": path.stat().st_size if path.exists() else None,
            "sha256": sha256_file(path),
        })
    return out


def routes(spec: dict, body: dict, mould_info: dict) -> dict:
    """The two fabrication routes this project uses, and which file serves each.

    cad/fabrication.json is the authority on the process; the tooling limits at the
    largest agreed size come from contracts/blocks.json
    target_envelope.fabrication_routes_at_ceiling.
    """
    import json as _json
    fab = _json.loads(FABRICATION.read_text())
    at_ceiling = contracts.block("target_envelope.fabrication_routes_at_ceiling")
    chip_id = spec["id"]
    return {
        "process": fab["process"],
        "source": "cad/fabrication.json",
        "resin_print": {
            "makes": "the tool, and the chip directly for a one-off",
            "files": [f"{chip_id}_mould.stl", f"{chip_id}_body.stl"],
            "min_feature_um": fab["min_embossed_feature_um"],
            "tooling_at_the_agreed_ceiling": at_ceiling["resin_prototype"],
        },
        "hot_emboss": {
            "makes": "the production chip, pressed from the resin tool into COC/COP sheet",
            "files": [f"{chip_id}_mould.stl"],
            "mould": mould_info["mirror_note"],
            "plate_mm": mould_info["plate_mm"],
            "relief_um": round(mould_info["relief_mm"] * 1000, 1),
            "tooling_at_the_agreed_ceiling": at_ceiling["embossing"],
            "not_usable": at_ceiling["not_usable"],
        },
        "seal": body["stack"]["seal"],
        "cam_2d": {
            "makes": "the cut outline, for any 2D route",
            "files": [f"{chip_id}.dxf"],
            "units_declared": "$INSUNITS = 4 (mm) in the DXF HEADER",
        },
    }


def build(spec: dict, scale: dict, geom: dict, body: dict, mould_info: dict,
          all_checks: list, stl_reports: list, files: list, extras: dict) -> dict:
    """The manifest, assembled. `all_checks` are checks.run_all() plus the STL reports."""
    layers = spec.get("layers", [])
    n_path = sum(1 for c in spec.get("channels", []) if c.get("path"))
    # An advisory that fails is a note about the DESIGN; it is recorded but it does not
    # make a printable part unprintable, so it is kept out of `checks_failed`.
    failed = [c["check"] for c in all_checks if not c["passed"] and not c.get("advisory")]
    advisories = [c["check"] for c in all_checks if not c["passed"] and c.get("advisory")]

    return {
        "schema": "assay2chip/fabricate manifest v1",
        "chip_id": spec["id"],
        "generated_by": "assay2chip/stages/s11_fabricate.py",
        "scale": scale,
        "units": "mm",
        "origin": ("the chip-spec origin: (0, 0) at the lower-left corner of the footprint, "
                   "z = 0 at the underside of the chip. Every file in this bundle shares it."),
        "footprint_mm": {"x": float(spec["footprint"]["x"]),
                         "y": float(spec["footprint"]["y"])},
        "depth": {
            "etch_depth_um": round(geom["max_depth_mm"] * 1000, 3),
            "depths_present_um": [round(d * 1000, 3) for d in geom["depths_seen_mm"]],
            "max_allowed_um": round(contracts.max_depth_mm() * 1000, 3),
            "body_thickness_mm": body["z_top_mm"],
            "recess_floor_mm": body["recess_floor_mm"],
        },
        "material": {
            "chip": body["stack"]["material"],
            "by_layer": [{"id": l["id"], "material": l.get("material", "unknown"),
                          "thickness_mm": l["thickness_mm"]} for l in layers],
            "fluidic_layer_mm": body["stack"]["fluidic_mm"],
            "base_mm": body["stack"]["base_mm"],
            "lid": body["stack"]["lid"],
        },
        "features": {
            "chambers": len(spec.get("chambers", [])),
            "channels": len(spec.get("channels", [])),
            "channels_with_explicit_path": n_path,
            "ports": len(spec.get("ports", [])),
            "solids_built": len(geom["plan"]["features"]),
            "cut_area_mm2": round(geom["cut_area_mm2"], 3),
            "not_modelled": mould_info["skipped"],
        },
        "geometry_method": {
            "grid": "rectilinear, lines on the feature edges",
            "cells": geom["cells"],
            "sampling_pitch_mm": geom["pitch_mm"],
            "sampled_features": geom["sampled_features"],
            "corner_repairs": geom["corner_repairs"],
            "note": ("axis-aligned features are exact; round port pads and diagonal runs "
                     "are stepped at the sampling pitch, a quarter of the in-plane floor"),
        },
        "fabrication_routes": routes(spec, body, mould_info),
        "checks": all_checks,
        "checks_passed": sum(1 for c in all_checks if c["passed"]),
        "checks_failed": failed,
        "advisories_raised": advisories,
        "all_checks_passed": not failed,
        "stl_validation": stl_reports,
        "files": file_records(files),
        "contracts": contracts.fingerprint(),
        **extras,
    }

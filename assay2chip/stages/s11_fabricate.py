"""S11 fabricate: verified chips out to 3D rendering, printing and the press.

    python assay2chip/stages/s11_fabricate.py --chip chip/module4_v1.json
    python assay2chip/stages/s11_fabricate.py --id my_assay          # every chip of a run
    python assay2chip/stages/s11_fabricate.py --chip chip/module6_v1.json --force

For each chip it writes ``cad/exports/<chip_id>/print/`` holding the geometry for BOTH
fabrication routes this project uses (``cad/fabrication.json``:
``resin_3d_print_plus_hot_emboss``), a closed DXF that declares its units, a Fusion 360
rebuild script, two renders and a manifest. ``assay2chip/fabricate/bundle.py`` documents
the contents file by file; this module is the stage around it - which chips, verified or
not, and what to do when a fabrication check fails.

WHICH CHIPS. With ``--id`` the chip list and each chip's intended scale come from S4's
``partition.json`` through ``assay2chip.lib.io.read_stage``. With ``--chip`` the stage
runs against spec files directly, which is how it is developed and tested against the
chips already in the repo.

VERIFIED MEANS VERIFIED. S9's record is read from ``assay2chip/work/<id>/verify.json`` if
it is there; a chip it reports with errors is skipped unless ``--force``. That file is
S9's to define and it is not written yet, so this reads it defensively and says which
branch it took - it never silently treats "no record" as "passed".

    TODO(S9): agree the shape of verify.json. Read here as
    {"chips": [{"chip_id": ..., "ok": bool, "errors": [...]}]}, with a bare list of
    chip ids and a {chip_id: bool} mapping also accepted. When S9 lands, drop the
    tolerant reader and use the real one.

A chip whose own fabrication checks fail is NOT written unless ``--force``: an STL that
breaks a floor is worse than no STL, because it looks finished. The STLs themselves can
never be written broken - ``mesh.write_validated`` checks watertightness, winding,
outward normals, footprint and origin before anything touches the disk.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.fabricate import bundle, checks  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402
from assay2chip.lib.io import StageFileMissing, read_stage, work_dir  # noqa: E402

WRITES = [
    "cad/exports/{chip_id}/print/{chip_id}_body.stl",
    "cad/exports/{chip_id}/print/{chip_id}_mould.stl",
    "cad/exports/{chip_id}/print/{chip_id}.dxf",
    "cad/exports/{chip_id}/print/{chip_id}_fusion_rebuild.py",
    "cad/exports/{chip_id}/print/render_iso.svg",
    "cad/exports/{chip_id}/print/render_top.svg",
    "cad/exports/{chip_id}/print/manifest.json",
    "cad/exports/{chip_id}/print/README.md",
]


class FabricateError(Exception):
    """S11 could not produce a bundle, and says which chip and why."""


# --- what S9 said ------------------------------------------------------------

def verification(assay_id: str) -> dict:
    """S9's verdict per chip. See the TODO in this module's docstring."""
    if not assay_id:
        return {}
    path = work_dir(assay_id) / "verify.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise FabricateError(f"{path} is not readable JSON: {e}")

    out: dict[str, dict] = {}
    rows = data.get("chips", data) if isinstance(data, dict) else data
    if isinstance(rows, dict):
        for chip_id, value in rows.items():
            ok = value if isinstance(value, bool) else not (value or {}).get("errors")
            out[chip_id] = {"ok": bool(ok), "errors": [] if ok else ["see verify.json"]}
    elif isinstance(rows, list):
        for row in rows:
            if isinstance(row, str):
                out[row] = {"ok": True, "errors": []}
            elif isinstance(row, dict) and row.get("chip_id"):
                errs = list(row.get("errors") or [])
                out[row["chip_id"]] = {"ok": bool(row.get("ok", not errs)), "errors": errs}
    return out


def spec_path_for(chip_id: str) -> Path:
    path = ROOT / "chip" / f"{chip_id}.json"
    if not path.exists():
        raise FabricateError(
            f"chip '{chip_id}' has no spec at chip/{chip_id}.json: S6/S7 have not built it "
            f"yet, so there is nothing to fabricate")
    return path


def chips_for(assay_id: str) -> list:
    """[(chip_id, spec path, intended scale)] from S4's partition."""
    partition = read_stage(assay_id, "partition")
    return [(c["chip_id"], spec_path_for(c["chip_id"]), c.get("scale"))
            for c in partition.get("chips", [])]


# --- the stage ---------------------------------------------------------------

def fabricate_one(spec_path: Path, intended_scale: str = None, force: bool = False,
                  exports_root: Path = None) -> dict:
    """One chip: build the bundle, then report what it says about itself."""
    spec = json.loads(Path(spec_path).read_text())
    for key in ("id", "footprint", "layers", "chambers", "channels", "ports"):
        if key not in spec:
            raise FabricateError(f"{spec_path}: not a chip spec (no '{key}')")

    scale = checks.scale_of(spec["footprint"])
    scale_mismatch = None
    if intended_scale and intended_scale != scale["name"]:
        scale_mismatch = (
            f"S4 planned chip '{spec['id']}' at {intended_scale} scale, but its footprint "
            f"{spec['footprint']['x']:g} x {spec['footprint']['y']:g} mm is {scale['name']} "
            f"scale. The geometry, not the plan, is what gets printed.")

    doc = bundle.write(spec, exports_root=exports_root)
    if scale_mismatch:
        doc["scale"]["planned_as"] = intended_scale
        doc["scale"]["mismatch"] = scale_mismatch

    failed = doc["checks_failed"]
    if failed and not force:
        # The files stay on disk: a manifest that names the failing check is exactly what
        # whoever has to fix the chip needs. What they must not do is print them.
        raise FabricateError(
            f"chip '{spec['id']}': {len(failed)} fabrication check(s) failed "
            f"({', '.join(failed)}). The bundle was written so you can see why - read "
            f"{doc['out_dir']}/manifest.json - but it is NOT fit to print. Fix the chip, "
            f"or re-run with --force if you mean to print it anyway.")
    return doc


def run(ctx=None, assay_id: str = None, chip: list = None, force: bool = False,
        exports_root: Path = None, **_) -> dict:
    """Stage entry point. Returns a summary; S12 reports it."""
    targets = []
    if chip:
        for path in chip:
            path = Path(path)
            targets.append((json.loads(path.read_text()).get("id", path.stem), path, None))
    elif assay_id:
        try:
            targets = chips_for(assay_id)
        except StageFileMissing as e:
            raise FabricateError(str(e))
    else:
        raise FabricateError("give assay_id (S4's chips) or chip (spec files)")

    verdicts = verification(assay_id)
    bundles, skipped, problems = [], [], []
    for chip_id, path, scale in targets:
        verdict = verdicts.get(chip_id)
        if verdict and not verdict["ok"] and not force:
            skipped.append({"chip_id": chip_id, "why": "S9 reports errors on this chip",
                            "errors": verdict["errors"][:4]})
            continue
        try:
            doc = fabricate_one(path, scale, force=force,
                                exports_root=exports_root)
        except FabricateError as e:
            problems.append({"chip_id": chip_id, "why": str(e)})
            continue
        bundles.append({
            "chip_id": chip_id,
            "out_dir": doc["out_dir"],
            "scale": doc["scale"]["name"],
            "is_target_device": doc["scale"]["is_target_device"],
            "checks_passed": doc["checks_passed"],
            "checks_failed": doc["checks_failed"],
            "advisories_raised": doc["advisories_raised"],
            "files": [f["file"] for f in doc["files"]],
            "verified_by_s9": bool(verdict and verdict["ok"]),
        })

    return {
        "stage": "s11_fabricate",
        "bundles": bundles,
        "skipped": skipped,
        "problems": problems,
        "verification_source": ("assay2chip/work/%s/verify.json" % assay_id if verdicts
                                else "none: S9 has written no verify.json, so no chip was "
                                     "treated as verified or unverified on its authority"),
        "contracts": contracts.fingerprint(),
    }


def main(argv: list = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--id", dest="assay_id", help="work id: fabricate S4's chips for it")
    ap.add_argument("--chip", action="append", type=Path,
                    help="a chip spec to fabricate (repeatable)")
    ap.add_argument("--out", type=Path, metavar="DIR",
                    help="use DIR instead of cad/exports/ as the exports root; each chip "
                         "still gets DIR/<chip_id>/print/")
    ap.add_argument("--force", action="store_true",
                    help="write the bundle even when a fabrication check fails")
    args = ap.parse_args(argv)

    try:
        result = run(assay_id=args.assay_id, chip=args.chip, force=args.force,
                     exports_root=args.out)
    except FabricateError as e:
        print(f"s11_fabricate: {e}", file=sys.stderr)
        return 1

    for b in result["bundles"]:
        flag = "" if b["is_target_device"] else f"  [{b['scale'].upper()} SCALE]"
        print(f"chip {b['chip_id']}: {b['checks_passed']} checks passed{flag}")
        print(f"  wrote {b['out_dir']}/ ({', '.join(b['files'])})")
        for name in b["checks_failed"]:
            print(f"  FAILED CHECK: {name}")
        for name in b["advisories_raised"]:
            print(f"  advisory: {name} (recorded, does not block printing)")
    for s in result["skipped"]:
        print(f"skipped {s['chip_id']}: {s['why']}")
    for p in result["problems"]:
        print(f"s11_fabricate: {p['why']}", file=sys.stderr)
    if not result["bundles"]:
        print("s11_fabricate: nothing fabricated", file=sys.stderr)
    return 1 if result["problems"] or not result["bundles"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

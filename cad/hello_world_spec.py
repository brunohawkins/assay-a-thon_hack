"""Hello world plate: dimensions -> chip spec, plus a check against Ayrton's STL.

    python cad/hello_world_spec.py

1. Reads cad/hello_world_dims.json (the single place to edit dimensions).
2. Writes cad/hello_world_plate_spec.json: two open-top wells (well_1, well_2) joined by
   an open-top channel (channel_1). Millimetres, origin at the bottom-left corner of the
   footprint, z up. Don't edit the spec by hand; edit the dims file and re-run.
3. If cad/exports/ holds hello_world_plate.stl, or well_1.stl / channel_1.stl /
   well_2.stl, reads each STL's bounding box and WARNS if it differs from the dims by
   more than 10%. It never changes the STL or the dims file.

Layout (top view, y up the page):

    +--------------------------------------------+
    |   ___                         ___          |
    |  /   \\ ===== channel_1 ===== /   \\         |   footprint y = 2t + d
    |  \\___/                       \\___/         |
    |  well_1                       well_2       |
    +--------------------------------------------+
       footprint x = 2t + 2d + L

    d = well diameter, L = channel length (well edge to well edge), t = wall thickness.
    The floor under the wells is also t thick (assumption until Ayrton confirms).
"""

from __future__ import annotations

import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))

import schema_io  # noqa: E402

CAD = ROOT / "cad"
DIMS = CAD / "hello_world_dims.json"
SPEC = CAD / "hello_world_plate_spec.json"
EXPORTS = CAD / "exports"
CHIP_ID = "hello_world_plate"
TOLERANCE = 0.10  # 10%

REQUIRED = ("well_diameter_mm", "well_depth_mm", "channel_width_mm", "channel_depth_mm",
            "channel_length_mm", "wall_thickness_mm", "min_feature_mm")


# --- geometry -------------------------------------------------------------------------


def load_dims(path: Path = DIMS) -> dict:
    dims = schema_io.load_json(path)
    missing = [k for k in REQUIRED if not isinstance(dims.get(k), (int, float)) or dims[k] <= 0]
    if missing:
        raise ValueError(f"{path.name}: these must be positive numbers (mm): {', '.join(missing)}")
    return dims


def layout(dims: dict) -> dict:
    """Derived positions and sizes, in mm, chip coordinates."""
    d, hw = dims["well_diameter_mm"], dims["well_depth_mm"]
    L, t = dims["channel_length_mm"], dims["wall_thickness_mm"]
    fx, fy = 2 * t + 2 * d + L, 2 * t + d
    height = t + hw
    y = t + d / 2
    z = t + hw / 2
    return {
        "footprint": (fx, fy),
        "height": height,
        "well_1_center": (t + d / 2, y, z),
        "well_2_center": (t + d + L + d / 2, y, z),
        "channel_x": (t + d, t + d + L),
    }


def build_spec(dims: dict) -> dict:
    lay = layout(dims)
    d, hw, t = dims["well_diameter_mm"], dims["well_depth_mm"], dims["wall_thickness_mm"]
    material = str(dims.get("material") or "pla")
    well_volume_ul = round(math.pi * (d / 2) ** 2 * hw, 1)  # 1 mm^3 = 1 uL
    fx, fy = lay["footprint"]

    def well(cid: str, center: tuple) -> dict:
        return {"id": cid, "type": "well", "center": [round(v, 3) for v in center],
                "size": [d, d, hw], "mesh_pore_um": None, "volume_ul": well_volume_ul, "open_top": True}

    return {
        "id": CHIP_ID,
        "name": "Hello world plate",
        "description": ("Two open-top wells joined by one open-top channel; Ayrton's first 3D print. "
                        "GENERATED from cad/hello_world_dims.json by cad/hello_world_spec.py; edit the dims, not this file."),
        "placeholder": bool(dims.get("placeholder", True)),
        "units": "mm",
        "footprint": {"x": round(fx, 3), "y": round(fy, 3)},
        "layers": [
            {"id": "floor", "material": material, "thickness_mm": t},
            {"id": "body", "material": material, "thickness_mm": hw},
        ],
        "chambers": [well("well_1", lay["well_1_center"]), well("well_2", lay["well_2_center"])],
        "channels": [{"id": "channel_1", "from": "well_1", "to": "well_2",
                      "width_mm": dims["channel_width_mm"], "depth_mm": dims["channel_depth_mm"],
                      "open_top": True}],
        "ports": [],
    }


def design_warnings(dims: dict) -> list[str]:
    out = []
    smallest = dims["min_feature_mm"]
    for key in ("channel_width_mm", "channel_depth_mm", "wall_thickness_mm"):
        if dims[key] < smallest:
            out.append(f"{key} = {dims[key]} mm is below the smallest clean printed feature ({smallest} mm)")
    if dims["channel_depth_mm"] > dims["well_depth_mm"]:
        out.append("channel_depth_mm is deeper than the wells")
    if dims["channel_width_mm"] >= dims["well_diameter_mm"]:
        out.append("channel_width_mm is as wide as the wells")
    return out


# --- STL check ------------------------------------------------------------------------


def stl_bbox(path: Path) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """(min_xyz, max_xyz) of an ASCII or binary STL. Standard library only."""
    data = path.read_bytes()
    if len(data) >= 84:
        (n,) = struct.unpack_from("<I", data, 80)
        if len(data) == 84 + 50 * n and n > 0:  # binary
            xs, ys, zs = [], [], []
            for i in range(n):
                v = struct.unpack_from("<9f", data, 84 + 50 * i + 12)
                xs += v[0::3]
                ys += v[1::3]
                zs += v[2::3]
            return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))
    pts = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0].lower() == "vertex":
            pts.append(tuple(float(p) for p in parts[1:]))
    if not pts:
        raise ValueError("no vertices found (not a valid STL?)")
    return tuple(min(p[i] for p in pts) for i in range(3)), tuple(max(p[i] for p in pts) for i in range(3))


def expected_boxes(dims: dict) -> dict[str, tuple[tuple, tuple]]:
    """Expected (min corner, size) per STL name. Separate parts are assumed to be the solid
    around each feature, walls included, exported in place."""
    lay = layout(dims)
    d, t, w = dims["well_diameter_mm"], dims["wall_thickness_mm"], dims["channel_width_mm"]
    H = lay["height"]
    fx, fy = lay["footprint"]
    x0, x1 = lay["channel_x"]
    boxes = {"hello_world_plate": ((0, 0, 0), (fx, fy, H))}
    for cid in ("well_1", "well_2"):
        cx, cy, _ = lay[f"{cid}_center"]
        boxes[cid] = ((cx - d / 2 - t, cy - d / 2 - t, 0), (d + 2 * t, d + 2 * t, H))
    cy = lay["well_1_center"][1]
    boxes["channel_1"] = ((x0, cy - w / 2 - t, 0), (x1 - x0, w + 2 * t, H))
    return boxes


def _off(a: float, b: float) -> float:
    return abs(a - b) / b if b else abs(a)


def check_stls(dims: dict, exports: Path = EXPORTS) -> list[str]:
    """Compare any hello world STLs in cad/exports/ with the dims. Returns warning lines."""
    warnings = []
    found = 0
    for name, (exp_min, exp_size) in expected_boxes(dims).items():
        path = exports / f"{name}.stl"
        if not path.exists():
            continue
        found += 1
        try:
            lo, hi = stl_bbox(path)
        except (ValueError, struct.error) as e:
            warnings.append(f"{path.name}: could not read ({e})")
            continue
        size = tuple(h - l for h, l in zip(hi, lo))
        sz = ", ".join(f"{v:.2f}" for v in size)
        ex = ", ".join(f"{v:.2f}" for v in exp_size)
        print(f"  {path.name}: bounding box {sz} mm (expected {ex} mm)")
        bad_axes = [ax for ax, g, e in zip("xyz", size, exp_size) if _off(g, e) > TOLERANCE]
        if bad_axes:
            hint = ""
            if all(_off(g, e) <= TOLERANCE for g, e in zip(sorted(size), sorted(exp_size))):
                hint = " It matches if rotated: export with z up and x along the long side."
            elif all(_off(g * 25.4, e) <= TOLERANCE for g, e in zip(size, exp_size)):
                hint = " It looks like inches: export in mm."
            elif all(_off(g / 1000, e) <= TOLERANCE or _off(g * 1000, e) <= TOLERANCE for g, e in zip(size, exp_size)):
                hint = " It looks off by 1000x: check the export units (mm)."
            warnings.append(f"{path.name}: size differs from hello_world_dims.json by more than 10% "
                            f"on {', '.join(bad_axes)} (got {sz}, expected {ex} mm).{hint}")
        tol = [max(0.5, TOLERANCE * s) for s in exp_size]
        if any(abs(g - e) > t for g, e, t in zip(lo, exp_min, tol)):
            got = ", ".join(f"{v:.2f}" for v in lo)
            want = ", ".join(f"{v:.2f}" for v in exp_min)
            warnings.append(f"{path.name}: min corner is at ({got}), expected about ({want}). "
                            f"Export in place, with the plate's bottom-left corner at 0,0,0.")
    if not found:
        print("  no hello world STL in cad/exports/ yet (hello_world_plate.stl or well_1/channel_1/well_2.stl)")
    return warnings


# --- main -----------------------------------------------------------------------------


def main() -> int:
    try:
        dims = load_dims()
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    spec = build_spec(dims)
    errors = schema_io.schema_errors("chip_spec", spec)
    if errors:
        for e in errors:
            print(f"ERROR spec {schema_io.path_str(e)}: {schema_io.describe(e)}", file=sys.stderr)
        return 1
    schema_io.write_json(SPEC, spec)
    fx, fy = spec["footprint"]["x"], spec["footprint"]["y"]
    print(f"wrote cad/{SPEC.name}  ({fx} x {fy} x {layout(dims)['height']} mm, "
          f"2 wells of {spec['chambers'][0]['volume_ul']} uL, channel {dims['channel_length_mm']} mm)")

    todo = dims.get("todo") or []
    if todo:
        print(f"TODO  placeholder values, not yet confirmed by Ayrton: {', '.join(todo)}")

    print("STL check:")
    warnings = design_warnings(dims) + check_stls(dims)
    for w in warnings:
        print(f"WARN  {w}")
    print(f"done: {len(warnings)} warning(s); nothing was changed except cad/{SPEC.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

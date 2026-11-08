"""Hello world plate: dimensions -> chip spec, plus a check against Ayrton's STL.

    python cad/hello_world_spec.py

1. Reads cad/hello_world_dims.json (the single place to edit dimensions).
2. Writes cad/hello_world_plate_spec.json: two open-top wells (well_1, well_2) joined by
   an open-top channel (channel_1). Millimetres, origin at the bottom-left corner of the
   footprint, z up. Don't edit the spec by hand; edit the dims file and re-run.
3. If cad/exports/ holds hello_world_plate.stl, or well_1.stl / channel_1.stl /
   well_2.stl, reads each STL's bounding box and WARNS if it differs from the dims by
   more than 10%. It never changes the STL or the dims file.

The MVP is a positive mould: a flat base with the wells and channel raised on top.
Casting against it gives the open-top device, so the raised shapes ARE the wells and
channel. Top view, y up the page:

    +---------------------------------------------+
    |                                             |
    |      ( )=============( )                    |   plate_y_mm
    |    well_1  channel_1  well_2                |
    |                                             |
    +---------------------------------------------+
                      plate_x_mm

    The well-channel-well group is centred on the plate, shifted by features_offset_x_mm.
    channel_length_mm runs well edge to well edge.

STL conventions: stl_units says what one STL unit is (Ayrton's exporter writes um), and
stl_origin says where the exporter puts 0,0 ("center" of the plate, or "corner" for our
usual bottom-left convention). The check converts before comparing.
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

REQUIRED = ("plate_x_mm", "plate_y_mm", "base_thickness_mm", "well_diameter_mm", "well_depth_mm",
            "channel_width_mm", "channel_depth_mm", "channel_length_mm")
STL_UNITS_TO_MM = {"mm": 1.0, "um": 0.001, "cm": 10.0, "m": 1000.0, "in": 25.4}


# --- geometry -------------------------------------------------------------------------


def load_dims(path: Path = DIMS) -> dict:
    dims = schema_io.load_json(path)
    missing = [k for k in REQUIRED if not isinstance(dims.get(k), (int, float)) or dims[k] <= 0]
    if missing:
        raise ValueError(f"{path.name}: these must be positive numbers (mm): {', '.join(missing)}")
    if dims.get("stl_units", "mm") not in STL_UNITS_TO_MM:
        raise ValueError(f"{path.name}: stl_units must be one of {', '.join(STL_UNITS_TO_MM)}")
    if dims.get("stl_origin", "corner") not in ("corner", "center"):
        raise ValueError(f"{path.name}: stl_origin must be \"corner\" or \"center\"")
    return dims


def layout(dims: dict) -> dict:
    """Derived positions and sizes, in mm, chip coordinates (bottom-left origin, z up)."""
    fx, fy = dims["plate_x_mm"], dims["plate_y_mm"]
    d, L = dims["well_diameter_mm"], dims["channel_length_mm"]
    base = dims["base_thickness_mm"]
    relief = max(dims["well_depth_mm"], dims["channel_depth_mm"])
    mid_x = fx / 2 + (dims.get("features_offset_x_mm") or 0)
    y = fy / 2
    half = L / 2 + d / 2
    return {
        "footprint": (fx, fy),
        "base": base,
        "relief": relief,
        "height": base + relief,
        "well_1_center": (mid_x - half, y, base + dims["well_depth_mm"] / 2),
        "well_2_center": (mid_x + half, y, base + dims["well_depth_mm"] / 2),
        "channel_x": (mid_x - L / 2, mid_x + L / 2),
    }


def build_spec(dims: dict) -> dict:
    lay = layout(dims)
    d, hw = dims["well_diameter_mm"], dims["well_depth_mm"]
    material = str(dims.get("material") or "unknown")
    if material.lower() in ("unknown", "tbc", ""):
        material = "printed_resin"  # a printed resin test coupon; the id in contracts/materials.json
    well_volume_ul = round(math.pi * (d / 2) ** 2 * hw, 4)  # 1 mm^3 = 1 uL
    fx, fy = lay["footprint"]

    def well(cid: str, center: tuple) -> dict:
        return {"id": cid, "type": "well", "center": [round(v, 4) for v in center],
                "size": [d, d, hw], "mesh_pore_um": None, "volume_ul": well_volume_ul, "open_top": True,
                "block": "polygon_chamber"}

    return {
        "id": CHIP_ID,
        "name": "Hello world plate",
        "description": ("Hello world MVP: two open-top wells joined by one open-top channel, from Ayrton's "
                        "positive mould (cad/exports/hello_world_plate.stl). GENERATED from "
                        "cad/hello_world_dims.json by cad/hello_world_spec.py; edit the dims, not this file."),
        "placeholder": bool(dims.get("placeholder", True)),
        "units": "mm",
        "footprint": {"x": fx, "y": fy},
        "layers": [
            {"id": "mould_base", "material": material, "thickness_mm": lay["base"]},
            {"id": "relief", "material": material, "thickness_mm": lay["relief"]},
        ],
        "chambers": [well("well_1", lay["well_1_center"]), well("well_2", lay["well_2_center"])],
        "channels": [{"id": "channel_1", "from": "well_1", "to": "well_2",
                      "width_mm": dims["channel_width_mm"], "depth_mm": dims["channel_depth_mm"],
                      "open_top": True, "block": "straight_channel"}],
        "ports": [],
    }


def design_warnings(dims: dict) -> list[str]:
    out = []
    smallest = dims.get("min_feature_mm")
    if isinstance(smallest, (int, float)) and smallest > 0:
        for key in ("channel_width_mm", "channel_depth_mm", "well_depth_mm"):
            if dims[key] < smallest:
                out.append(f"{key} = {dims[key]} mm is below the smallest clean printed feature ({smallest} mm)")
    if dims["channel_width_mm"] >= dims["well_diameter_mm"]:
        out.append("channel_width_mm is as wide as the wells")
    lay = layout(dims)
    x0 = lay["well_1_center"][0] - dims["well_diameter_mm"] / 2
    x1 = lay["well_2_center"][0] + dims["well_diameter_mm"] / 2
    if x0 < 0 or x1 > dims["plate_x_mm"] or dims["well_diameter_mm"] > dims["plate_y_mm"]:
        out.append("the wells and channel don't fit on the plate")
    return out


# --- STL check ------------------------------------------------------------------------


def stl_vertices(path: Path) -> list[tuple[float, float, float]]:
    """Every vertex of an ASCII or binary STL, in STL units. Standard library only."""
    data = path.read_bytes()
    if len(data) >= 84:
        (n,) = struct.unpack_from("<I", data, 80)
        if len(data) == 84 + 50 * n and n > 0:  # binary (even if the header says "solid")
            pts = []
            for i in range(n):
                v = struct.unpack_from("<9f", data, 84 + 50 * i + 12)
                pts += [v[0:3], v[3:6], v[6:9]]
            return pts
    pts = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0].lower() == "vertex":
            pts.append(tuple(float(p) for p in parts[1:]))
    if not pts:
        raise ValueError("no vertices found (not a valid STL?)")
    return pts


def bbox(pts: list) -> tuple[tuple, tuple]:
    return tuple(min(p[i] for p in pts) for i in range(3)), tuple(max(p[i] for p in pts) for i in range(3))


def stl_bbox(path: Path) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """(min_xyz, max_xyz) of an STL, in STL units."""
    return bbox(stl_vertices(path))


def expected_boxes(dims: dict) -> dict[str, tuple[tuple, tuple]]:
    """Expected (min corner, size) in mm, chip coordinates, per STL name."""
    lay = layout(dims)
    d, w = dims["well_diameter_mm"], dims["channel_width_mm"]
    fx, fy = lay["footprint"]
    base = lay["base"]
    x0, x1 = lay["channel_x"]
    boxes = {"hello_world_plate": ((0, 0, 0), (fx, fy, lay["height"]))}
    for cid in ("well_1", "well_2"):
        cx, cy, _ = lay[f"{cid}_center"]
        boxes[cid] = ((cx - d / 2, cy - d / 2, base), (d, d, dims["well_depth_mm"]))
    cy = lay["well_1_center"][1]
    boxes["channel_1"] = ((x0, cy - w / 2, base), (x1 - x0, w, dims["channel_depth_mm"]))
    return boxes


def expected_relief(dims: dict) -> tuple[tuple, tuple]:
    """(min corner, size) of the raised well-channel-well group on a positive mould."""
    lay = layout(dims)
    d = dims["well_diameter_mm"]
    left = lay["well_1_center"][0] - d / 2
    right = lay["well_2_center"][0] + d / 2
    return (left, lay["well_1_center"][1] - d / 2, lay["base"]), (right - left, d, lay["relief"])


def to_chip_mm(pts: list, dims: dict) -> list[tuple]:
    """Convert STL points to mm with the bottom-left-corner origin."""
    s = STL_UNITS_TO_MM[dims.get("stl_units", "mm")]
    shift = (dims["plate_x_mm"] / 2, dims["plate_y_mm"] / 2, 0) if dims.get("stl_origin") == "center" else (0, 0, 0)
    return [tuple(v * s + o for v, o in zip(p, shift)) for p in pts]


def compare_box(label: str, lo: tuple, hi: tuple, exp_min: tuple, exp_size: tuple) -> list[str]:
    """Warnings if a box's size or position is more than 10% off."""
    out = []
    size = tuple(h - l for h, l in zip(hi, lo))
    sz = ", ".join(f"{v:.3f}" for v in size)
    ex = ", ".join(f"{v:.3f}" for v in exp_size)
    bad_axes = [ax for ax, g, e in zip("xyz", size, exp_size) if _off(g, e) > TOLERANCE]
    if bad_axes:
        hint = ""
        if all(_off(g, e) <= TOLERANCE for g, e in zip(sorted(size), sorted(exp_size))):
            hint = " It matches if rotated: export with z up and x along the long side."
        elif all(_off(g * 25.4, e) <= TOLERANCE for g, e in zip(size, exp_size)):
            hint = " It looks like inches: check stl_units in hello_world_dims.json."
        elif all(_off(g / 1000, e) <= TOLERANCE or _off(g * 1000, e) <= TOLERANCE for g, e in zip(size, exp_size)):
            hint = " It looks off by 1000x: check stl_units in hello_world_dims.json."
        out.append(f"{label}: size differs from hello_world_dims.json by more than 10% "
                   f"on {', '.join(bad_axes)} (got {sz}, expected {ex} mm).{hint}")
    tol = [max(0.05, TOLERANCE * s) for s in exp_size]
    if any(abs(g - e) > t for g, e, t in zip(lo, exp_min, tol)):
        got = ", ".join(f"{v:.3f}" for v in lo)
        want = ", ".join(f"{v:.3f}" for v in exp_min)
        out.append(f"{label}: min corner is at ({got}) mm, expected about ({want}). "
                   f"Check stl_origin / features_offset_x_mm in hello_world_dims.json, or export in place.")
    return out


def _off(a: float, b: float) -> float:
    return abs(a - b) / b if b else abs(a)


def check_stls(dims: dict, exports: Path = EXPORTS) -> list[str]:
    """Compare any hello world STLs in cad/exports/ with the dims. Returns warning lines."""
    warnings = []
    found = 0
    units = dims.get("stl_units", "mm")
    origin = dims.get("stl_origin", "corner")
    for name, (exp_min, exp_size) in expected_boxes(dims).items():
        path = exports / f"{name}.stl"
        if not path.exists():
            continue
        found += 1
        try:
            pts = to_chip_mm(stl_vertices(path), dims)
        except (ValueError, struct.error) as e:
            warnings.append(f"{path.name}: could not read ({e})")
            continue
        lo, hi = bbox(pts)
        sz = ", ".join(f"{h - l:.3f}" for h, l in zip(hi, lo))
        ex = ", ".join(f"{v:.3f}" for v in exp_size)
        print(f"  {path.name} (read as {units}, origin at {origin}): {sz} mm (expected {ex} mm)")
        warnings += compare_box(path.name, lo, hi, exp_min, exp_size)
        # One-piece positive mould: everything above the base is the wells + channel.
        if name == "hello_world_plate" and dims.get("positive_mould"):
            base = layout(dims)["base"]
            raised = [p for p in pts if p[2] > base + 1e-6]
            if not raised:
                warnings.append(f"{path.name}: nothing raised above the {base} mm base, but positive_mould is true")
            else:
                # The relief stands on the base surface: x/y from the raised vertices,
                # z from the base up to their top.
                (x0, y0, _), (x1, y1, z1) = bbox(raised)
                rlo, rhi = (x0, y0, base), (x1, y1, z1)
                r_min, r_size = expected_relief(dims)
                rsz = ", ".join(f"{h - l:.3f}" for h, l in zip(rhi, rlo))
                rex = ", ".join(f"{v:.3f}" for v in r_size)
                print(f"    raised wells + channel: {rsz} mm (expected {rex} mm)")
                warnings += compare_box(f"{path.name} raised wells + channel", rlo, rhi, r_min, r_size)
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
    lay = layout(dims)
    fx, fy = lay["footprint"]
    print(f"wrote cad/{SPEC.name}  ({fx} x {fy} x {lay['height']:g} mm; wells {dims['well_diameter_mm']} mm, "
          f"{spec['chambers'][0]['volume_ul']} uL each; channel {dims['channel_width_mm']} x "
          f"{dims['channel_depth_mm']} x {dims['channel_length_mm']} mm)")

    todo = dims.get("todo") or []
    if todo:
        print(f"TODO  still unknown: {', '.join(todo)}")

    print("STL check:")
    warnings = design_warnings(dims) + check_stls(dims)
    for w in warnings:
        print(f"WARN  {w}")
    print(f"done: {len(warnings)} warning(s); nothing was changed except cad/{SPEC.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

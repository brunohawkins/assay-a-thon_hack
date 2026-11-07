"""Module 3, capsule wash (REAL scale, PARAMETRIC DRAFT), from the wash MVP work.

    python cad/generators/m3_wash.py                       # defaults
    python cad/generators/m3_wash.py --capsule-count 25000 --inlets 2
    python cad/generators/m3_wash.py --help                # every parameter

Writes cad/m3_wash_spec.json, protocols/m3_wash.json (timings from operator/flow_calc.py)
and refreshes the predictions in cad/m3_wash_dye_test.json (measured values are kept).
Parameters: contracts/schemas/generators/m3_wash.schema.json.

One trap chamber holds the capsules as a single layer on a bought mesh (area from
operator/sizing.py). Liquid comes in from above, leaves through a waste outlet below the
mesh (capsules stay on top), a back-flush inlet below the mesh lifts the capsules, and a
release outlet at the side lets them out. Side view (not to scale):

      m3_inlet_1..n               m3_release_1 (side)
          |                            |
    lid   |                            |
    ======|===========+       +========|=====
    trap  |  o o o o o o o o o o o  ---+        <- chamber_height_um, capsules on the mesh
    ......:...........mesh..................
    base  ---+                    +---
             |                    |
     m3_backflush_1           m3_waste_1        (bottom ports)

Design rules the generator refuses to break: mesh_opening_um < 0.4 x capsule_diameter_um,
chamber_height_um >= swollen_diameter_um, channels 1.5-2 x capsule diameter (schema).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genlib  # noqa: E402
from genlib import ParamError, r  # noqa: E402

import flow_calc  # noqa: E402
import schema_io  # noqa: E402
import sizing  # noqa: E402

NAME = "m3_wash"
OUT = genlib.ROOT / "cad" / "m3_wash_spec.json"
PROTOCOL_OUT = genlib.ROOT / "protocols" / "m3_wash.json"
DYE_TEST = genlib.ROOT / "cad" / "m3_wash_dye_test.json"
BASE_MM, LID_MM = 1.0, 1.0
TRAP_MAX_FACTOR = 0.4


def _param(key: str) -> float:
    return schema_io.load_json(genlib.ROOT / "contracts" / "parameters.json")["parameters"][key]["value"]


def check_rules(p: dict) -> None:
    d = p["capsule_diameter_um"]
    problems = []
    if p["mesh_opening_um"] >= TRAP_MAX_FACTOR * d:
        problems.append(f"mesh_opening_um {p['mesh_opening_um']:g} must be < 0.4 x capsule_diameter_um = "
                        f"{TRAP_MAX_FACTOR * d:g} um, or capsules slip through the mesh")
    if p["chamber_height_um"] < p["swollen_diameter_um"]:
        problems.append(f"chamber_height_um {p['chamber_height_um']:g} must be >= swollen_diameter_um "
                        f"{p['swollen_diameter_um']:g}")
    if problems:
        raise ParamError("; ".join(problems))


def trap_side_mm(p: dict) -> float:
    s = sizing.compute({"capsule_count": p["capsule_count"], "capsule_diameter_um": p["capsule_diameter_um"]})
    return round(math.sqrt(s["monolayer_area_cm2"] * 100), 3)


def hydraulics(p: dict) -> dict:
    """flow_calc: mesh + full capsule bed resistance, the safe wash flow, and its pressure."""
    side = trap_side_mm(p)
    mu = _param("fluid.viscosity_pa_s")
    clean = flow_calc.mesh_clean_resistance(p["mesh_opening_um"], side * side, _param("mesh.open_fraction"), mu)
    bed = _param("mesh.bed_resistance_kpa_per_ul_min")
    limit = _param("control.pressure_margin") * _param("capsule.escape_pressure_kpa")
    r_total = clean + bed
    q_safe = limit / r_total
    flow = min(p["max_flow_ul_min"], max(1.0, math.floor(q_safe)))
    return {"resistance_kpa_per_ul_min": r_total, "safe_flow_ul_min": q_safe, "flow_ul_min": flow,
            "pressure_kpa": flow_calc.pressure_kpa(r_total, flow), "limit_kpa": limit}


def build(p: dict) -> dict:
    check_rules(p)
    d = p["capsule_diameter_um"]
    side = trap_side_mm(p)
    h = p["chamber_height_um"] / 1000
    margin = p["margin_mm"]
    fx, fy = round(side + 2 * margin, 3), round(side + 2 * 2.5, 3)
    cx, cy = fx / 2, fy / 2
    top = BASE_MM + h + LID_MM
    w = round(p["channel_width_factor"] * d / 1000, 4)
    s = sizing.compute({"capsule_count": p["capsule_count"], "capsule_diameter_um": d})

    def port(pid, typ, x, y, z, role, passable):
        return {"id": pid, "type": typ, "position": [round(x, 3), round(y, 3), round(z, 3)], "role": role,
                "capsule_passable": passable, "width_um": p["port_width_um"]}

    n = p["inlets"]
    xs = [cx] if n == 1 else [round(cx - side / 2 + side * (i + 0.5) / n, 3) for i in range(n)]
    ports = [port(f"m3_inlet_{i + 1}", "inlet", x, cy, top, "load", True) for i, x in enumerate(xs)]          # above the trap
    channels = [{"id": f"m3_ch_load_{i + 1}" if n > 1 else "m3_ch_load", "from": f"m3_inlet_{i + 1}", "to": "m3_trap_1",
                 "width_mm": w, "depth_mm": h, "carries": "capsules"} for i in range(n)]
    ports += [port("m3_waste_1", "outlet", fx - margin / 2, cy, 0, "waste", False),                           # below the mesh
              port("m3_release_1", "outlet", cx, fy - 1.25, top, "release", True)]                            # at the side
    channels += [{"id": "m3_ch_waste", "from": "m3_trap_1", "to": "m3_waste_1", "width_mm": w, "depth_mm": h, "carries": "liquid"},
                 {"id": "m3_ch_release", "from": "m3_trap_1", "to": "m3_release_1", "width_mm": w, "depth_mm": h, "carries": "capsules"}]
    if p["backflush"]:
        ports.insert(n, port("m3_backflush_1", "inlet", margin / 2, cy, 0, "backflush", False))                # below the mesh
        channels.insert(n + 1, {"id": "m3_ch_backflush", "from": "m3_backflush_1", "to": "m3_trap_1", "width_mm": w,
                                "depth_mm": h, "carries": "liquid"})
    return {
        "id": "m3_wash_v1",
        "name": "Module 3: capsule wash (real scale, draft)",
        "description": (f"One capsule trap for {p['capsule_count']:,} capsules as a single layer on a bought "
                        f"{p['mesh_opening_um']:g} um mesh: load from above, wash down to waste below the mesh, "
                        + ("back-flush from below to resuspend, " if p["backflush"] else "")
                        + "release at the side. GENERATED by cad/generators/m3_wash.py; edit the parameters, not this file."),
        "placeholder": True,
        "module": {"id": "m3", "name": "Capsule wash", "status": "draft", "printed": True},
        "generator": {"script": "cad/generators/m3_wash.py",
                      "params_schema": "contracts/schemas/generators/m3_wash.schema.json", "params": p},
        "units": "mm",
        "footprint": {"x": fx, "y": fy},
        "layers": [
            {"id": "m3_base", "material": "pmma", "thickness_mm": BASE_MM},
            {"id": "m3_fluidic", "material": "pmma", "thickness_mm": h},
            {"id": "m3_lid", "material": "pmma", "thickness_mm": LID_MM},
        ],
        "capsule": {"diameter_um": d, "swell_factor": r(p["swollen_diameter_um"] / d), "count": p["capsule_count"]},
        "trap": {"gap_um": p["mesh_opening_um"]},
        "chambers": [{
            "id": "m3_trap_1", "type": "exchange",
            "center": [cx, cy, round(BASE_MM + h / 2, 4)], "size": [side, side, h],
            "height_um": p["chamber_height_um"],
            "volume_ul": round(side * side * h, 3),
            "retention": {"type": "mesh", "opening_um": p["mesh_opening_um"], "source": "bought"},
            "note": f"Single layer of {p['capsule_count']:,} capsules needs {s['monolayer_area_cm2'] * 100:.1f} mm2 (sizing.py).",
        }],
        "channels": channels,
        "ports": ports,
    }


def protocol(p: dict, spec: dict) -> dict:
    hy = hydraulics(p)
    q, v = hy["flow_ul_min"], p["wash_volume_ul"]
    t = lambda washes: round(washes * v / q * 60, 1)  # noqa: E731
    steps = [
        {"step": 1, "operation": "load", "target": "m3_trap_1", "reagent": None, "volume_ul": v, "duration_s": t(1),
         "temp_c": None, "washes": None, "flow_ul_min": None,
         "note": f"Load {p['capsule_count']:,} capsules through the top inlet; they settle in a single layer on the mesh."},
        {"step": 2, "operation": "exchange", "target": "m3_trap_1", "reagent": "wash_buffer", "volume_ul": v, "duration_s": t(3),
         "temp_c": None, "washes": 3, "flow_ul_min": q, "direction": "forward",
         "note": "Wash three times: buffer flows down through the mesh to waste while the capsules stay on top."},
    ]
    if p["backflush"]:
        steps.append({"step": 3, "operation": "exchange", "target": "m3_trap_1", "reagent": "resuspension_buffer", "volume_ul": v,
                      "duration_s": t(1), "temp_c": None, "washes": 1, "flow_ul_min": q, "direction": "backflush",
                      "note": "Back-flush from below to lift the capsules off the mesh and resuspend them."})
    steps.append({"step": len(steps) + 1, "operation": "release", "target": "m3_trap_1", "reagent": "resuspension_buffer",
                  "volume_ul": v, "duration_s": t(1), "temp_c": None, "washes": None, "flow_ul_min": None,
                  "note": "Flush the washed capsules out through the release outlet."})
    return {
        "id": "m3_wash",
        "name": "Module 3: capsule wash",
        "source": ("SPC-NAR (Leonaviciene & Mazutis, NAR 2023): capsule washes at 2000 g, 2 min, replaced here by a mesh trap "
                   "with back-flush; CapSeq (Baronas et al., Science 2026): repeated washes between steps. Timings from "
                   "operator/flow_calc.py. GENERATED by cad/generators/m3_wash.py."),
        "placeholder": True,
        "description": (f"Capsule wash on the Module 3 chip: load, three washes down through the mesh to waste, "
                        f"{'back-flush to resuspend, ' if p['backflush'] else ''}release. {q:g} uL/min keeps the predicted "
                        f"pressure on the capsule bed at {hy['pressure_kpa']:.2f} kPa (limit {hy['limit_kpa']:.2f} kPa). "
                        f"Volumes are placeholders."),
        "steps": steps,
    }


def dye_test(p: dict, spec: dict) -> dict:
    """Predicted dye left in the trap after washing V uL of clear buffer (well-mixed: e^(-V/Vc),
    the same model as operator/model.py). Measured values in an existing file are kept."""
    vc = spec["chambers"][0]["volume_ul"]
    q = hydraulics(p)["flow_ul_min"]
    old = {}
    if DYE_TEST.exists():
        prev = schema_io.load_json(DYE_TEST)
        old = {pt["volume_ul"]: pt for pt in prev.get("points", [])}
    points = []
    for k in range(0, 11):
        vol = round(vc * k * 0.5, 2)                       # 0 to 5 chamber volumes, in half steps
        keep = old.get(vol, {})
        points.append({"volume_ul": vol, "time_s": round(vol / q * 60, 1),
                       "predicted_dye_fraction": round(math.exp(-vol / vc), 4),
                       "measured_dye_fraction": keep.get("measured_dye_fraction"),
                       "note": keep.get("note", "")})
    measured = [pt for pt in points if pt["measured_dye_fraction"] is not None]
    return {
        "_about": ("Module 3 dye test: measured vs predicted. Fill the trap with dye, then wash with clear buffer at "
                   "flow_ul_min and read the outlet (or the trap) after each volume. Put readings in "
                   "measured_dye_fraction (1 = all dye left, 0 = clean). cad/generators/m3_wash.py rewrites the "
                   "predictions and keeps the measurements; operator/model.py uses the same well-mixed model."),
        "chip_id": spec["id"], "chamber": "m3_trap_1", "chamber_volume_ul": vc, "flow_ul_min": q,
        "model": "well-mixed: dye fraction = exp(-V / Vc)",
        "measured_points": len(measured),
        "points": points,
    }


def summary(p: dict, spec: dict) -> list[str]:
    d = p["capsule_diameter_um"]
    s = sizing.compute({"capsule_count": p["capsule_count"], "capsule_diameter_um": d})
    hy = hydraulics(p)
    trap = spec["chambers"][0]
    w = spec["channels"][0]["width_mm"] * 1000
    return [
        f"Module 3 sizing (operator/sizing.py + flow_calc.py) for {p['capsule_count']:,} capsules of {d:g} um:",
        f"  trap                        {trap['size'][0]:g} x {trap['size'][1]:g} mm single layer ({s['monolayer_area_cm2'] * 100:.1f} mm2), "
        f"{trap['height_um']:g} um high (swollen capsules {p['swollen_diameter_um']:g} um), {trap['volume_ul']:g} uL",
        f"  packed capsules             {s['packed_volume_ul']:.2f} uL",
        f"  mesh                        {p['mesh_opening_um']:g} um (< 0.4 x {d:g} = {0.4 * d:g} um; sheet 0.3-0.4 x = "
        f"{s['trap_gap_low_um']:g}-{s['trap_gap_high_um']:g} um)",
        f"  channels                    {w:g} um ({p['channel_width_factor']:g} x diameter; sheet 1.5-2 x = "
        f"{s['channel_width_low_um']:g}-{s['channel_width_high_um']:g} um)",
        f"  inlets / back-flush         {p['inlets']} / {'yes' if p['backflush'] else 'no'};  footprint "
        f"{spec['footprint']['x']:g} x {spec['footprint']['y']:g} mm",
        f"  wash flow                   {hy['flow_ul_min']:g} uL/min -> {hy['pressure_kpa']:.2f} kPa on a full capsule bed "
        f"(limit {hy['limit_kpa']:.2f} kPa; safe up to {hy['safe_flow_ul_min']:.0f} uL/min)",
        f"  one wash of {p['wash_volume_ul']:g} uL             {p['wash_volume_ul'] / hy['flow_ul_min'] * 60:.0f} s "
        f"({p['wash_volume_ul'] / trap['volume_ul']:.1f} chamber volumes; ~{1 - math.exp(-p['wash_volume_ul'] / trap['volume_ul']):.0%} exchanged)",
    ]


def main(argv: list[str] | None = None) -> int:
    try:
        params, args = genlib.parse_params(NAME, argv, "Module 3 capsule wash (parametric draft) -> cad/m3_wash_spec.json")
        spec = build(params)
        out = args.out or OUT
        genlib.write_spec(spec, out)
        if not args.out:
            proto = protocol(params, spec)
            errors = schema_io.schema_errors("protocol", proto)
            if errors:
                raise ParamError("generated protocol breaks the protocol schema: " + "; ".join(schema_io.describe(e) for e in errors[:5]))
            schema_io.write_json(PROTOCOL_OUT, proto)
            schema_io.write_json(DYE_TEST, dye_test(params, spec))
    except ParamError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"wrote {genlib.rel(out)}" + ("" if args.out else f", {genlib.rel(PROTOCOL_OUT)} and {genlib.rel(DYE_TEST)}"))
    return genlib.finish(out, summary(params, spec), args, [(PROTOCOL_OUT, OUT)])


if __name__ == "__main__":
    raise SystemExit(main())

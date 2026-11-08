"""Module 3, capsule wash (REAL scale, PARAMETRIC DRAFT), from the wash MVP work.

    python cad/generators/m3_wash.py                       # defaults
    python cad/generators/m3_wash.py --capsule-count 25000 --inlets 2
    python cad/generators/m3_wash.py --help                # every parameter

Writes cad/m3_wash_spec.json, protocols/m3_wash.json (timings from operator/flow_calc.py)
and refreshes the predictions in cad/m3_wash_dye_test.json (measured values are kept).
Parameters: contracts/schemas/generators/m3_wash.schema.json.

One trap chamber holds the capsules as a single layer behind a DEPTH WEIR (area from
operator/sizing.py). A 25 um mesh cannot be printed (the floor is 200 um IN PLANE, z is
free), so the mesh_opening_um parameter is converted to the weir of contracts/blocks.json
("retention"): a crest leaving a 60 um gap under the lid, block trap_chamber. Everything is
ONE etch depth (0.3 mm) in one fluidic layer inside the 30 x 20 mm envelope. Liquid comes in
at the top, leaves through the waste outlet past the weir (capsules stay behind it), a
back-flush inlet on the far side lifts the capsules, and a release outlet lets them out.
Side view (not to scale; the real ports sit beside the chamber in plan view):

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
chamber_height_um >= swollen_diameter_um, channels 1.5-2 x capsule diameter (schema). Build
floors it enforces by clamping (listed in the spec description): channels >= 200 um, ports
>= 0.8 mm, depth 0.3 mm, footprint inside 30 x 20 mm, wash flow under the wall-shear ceiling.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genlib  # noqa: E402
from genlib import ParamError, r  # noqa: E402

import flow_calc  # noqa: E402
import schema_io  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
import sizing  # noqa: E402

NAME = "m3_wash"
OUT = genlib.ROOT / "cad" / "m3_wash_spec.json"
PROTOCOL_OUT = genlib.ROOT / "protocols" / "m3_wash.json"
DYE_TEST = genlib.ROOT / "cad" / "m3_wash_dye_test.json"
BASE_MM, LID_MM = 1.0, 1.0
TRAP_MAX_FACTOR = 0.4

_BLOCKS = json.loads((genlib.ROOT / "contracts" / "blocks.json").read_text(encoding="utf-8"))
_ENV, _FLOORS, _WEIR = _BLOCKS["envelope"], _BLOCKS["fabrication_floors"], _BLOCKS["retention"]["weir_geometry"]
DEPTH_MM = _ENV["max_depth_mm"]                       # the ONE etch depth
DEPTH_UM = DEPTH_MM * 1000
WEIR_GAP_UM = _WEIR["gap_um"]
WEIR_CREST_UM = DEPTH_UM - WEIR_GAP_UM                # crest = this chip's etch depth minus the gap
MIN_OPEN_UM = _FLOORS["min_open_channel_mm"] * 1000   # in-plane floor
MIN_PORT_UM = _FLOORS["min_port_mm"] * 1000
PITCH_MM = _FLOORS["min_port_pitch_mm"]
MAX_PORTS = _FLOORS["max_ports_per_chip"]
MU_PA_S = 0.8e-3                # buffer viscosity for the wall-shear check
SHEAR_MAX_DYN_CM2 = 0.01        # capsules on the trap: wall shear ceiling
GAP_MM = 0.6                    # clear space between neighbouring features (floor is 0.4 mm)
PORT_ROW_MM = 1.5               # top / bottom ports sit this far beyond the trap


def shear_dyn_cm2(flow_ul_min: float, w_mm: float, h_mm: float) -> float:
    """Wall shear tau = 6 mu Q / (w h^2) in a wide slit, in dyn/cm2 (1 Pa = 10 dyn/cm2)."""
    q = flow_ul_min * 1e-9 / 60
    return 10 * 6 * MU_PA_S * q / ((w_mm / 1000) * (h_mm / 1000) ** 2)


def max_flow_ul_min(w_mm: float, h_mm: float) -> float:
    """Highest flow that keeps shear_dyn_cm2() at or under SHEAR_MAX_DYN_CM2 for a w x h section."""
    return SHEAR_MAX_DYN_CM2 / 10 * (w_mm / 1000) * (h_mm / 1000) ** 2 / (6 * MU_PA_S) * 60e9


def retention_is_weir(p: dict) -> bool:
    """A mesh finer than the in-plane floor cannot be printed: it becomes a depth weir."""
    return p["mesh_opening_um"] < MIN_OPEN_UM


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
    opening = WEIR_GAP_UM if retention_is_weir(p) else p["mesh_opening_um"]
    clean = flow_calc.mesh_clean_resistance(opening, side * side, _param("mesh.open_fraction"), mu)
    bed = _param("mesh.bed_resistance_kpa_per_ul_min")
    limit = _param("control.pressure_margin") * _param("capsule.escape_pressure_kpa")
    r_total = clean + bed
    q_safe = limit / r_total
    shear_cap = math.floor(max_flow_ul_min(side, DEPTH_MM))      # wall shear over the trap: w = side, h = depth
    flow = min(p["max_flow_ul_min"], max(1.0, math.floor(q_safe)), shear_cap)
    return {"resistance_kpa_per_ul_min": r_total, "safe_flow_ul_min": q_safe, "flow_ul_min": flow,
            "shear_cap_ul_min": shear_cap, "shear_dyn_cm2": shear_dyn_cm2(flow, side, DEPTH_MM),
            "pressure_kpa": flow_calc.pressure_kpa(r_total, flow), "limit_kpa": limit}


def build(p: dict) -> dict:
    check_rules(p)
    d = p["capsule_diameter_um"]
    side = trap_side_mm(p)
    h = DEPTH_MM
    margin = p["margin_mm"]
    n = p["inlets"]
    clamps: list[str] = []
    port_um = max(p["port_width_um"], MIN_PORT_UM)
    if port_um != p["port_width_um"]:
        clamps.append(f"port {p['port_width_um']:g} -> {port_um:g} um")
    if abs(p["chamber_height_um"] - DEPTH_UM) > 1e-9:
        clamps.append(f"chamber height {p['chamber_height_um']:g} -> {DEPTH_UM:g} um (one etch depth)")
    half_port = port_um / 2000
    inlet_pitch = max(PITCH_MM, side / n)
    fx = round(max(side + 2 * margin, (n - 1) * inlet_pitch + 2 * (half_port + GAP_MM)), 3)
    fy = round(side + 2 * 2.5, 3)
    if fx > _ENV["max_x_mm"] or fy > _ENV["max_y_mm"]:
        raise ParamError(f"layout doesn't fit: a {side:g} mm trap needs a {fx:g} x {fy:g} mm board, over the "
                         f"{_ENV['max_x_mm']:g} x {_ENV['max_y_mm']:g} mm build envelope")
    cx, cy = fx / 2, fy / 2
    top = BASE_MM + h + LID_MM
    # The capsule rule says 1.5-2 x diameter, but we also cannot PRINT below the in-plane
    # floor in contracts/blocks.json. Take whichever is larger: a channel narrower than the
    # floor is not manufacturable, however good the fluidics are.
    w = round(p["channel_width_factor"] * d / 1000, 4)
    if w * 1000 < MIN_OPEN_UM:
        clamps.append(f"channel width {w * 1000:g} -> {MIN_OPEN_UM:g} um")
        w = MIN_OPEN_UM / 1000
    weir = retention_is_weir(p)
    if weir:
        clamps.append(f"mesh {p['mesh_opening_um']:g} um -> depth weir, {WEIR_GAP_UM:g} um gap under the lid")
    s = sizing.compute({"capsule_count": p["capsule_count"], "capsule_diameter_um": d})

    def port(pid, typ, x, y, z, role, passable):
        return {"id": pid, "type": typ, "position": [round(x, 3), round(y, 3), round(z, 3)], "role": role,
                "capsule_passable": passable, "width_um": port_um}

    # Ports sit BESIDE the trap in plan view (a through-hole over the chamber would be drilled into it):
    # load inlets along the top, release below, back-flush left and waste right, level with the chamber.
    row_up, row_down = cy + side / 2 + PORT_ROW_MM, cy - side / 2 - PORT_ROW_MM
    xs = [round(cx + (i - (n - 1) / 2) * inlet_pitch, 3) for i in range(n)]
    ports = [port(f"m3_inlet_{i + 1}", "inlet", x, row_up, top, "load", True) for i, x in enumerate(xs)]
    chan = lambda cid, a, b, carries: {"id": cid, "from": a, "to": b, "width_mm": w, "depth_mm": h, "carries": carries,  # noqa: E731
                                       "block": "straight_channel"}
    channels = [chan(f"m3_ch_load_{i + 1}" if n > 1 else "m3_ch_load", f"m3_inlet_{i + 1}", "m3_trap_1", "capsules") for i in range(n)]
    ports += [port("m3_waste_1", "outlet", fx - margin / 2, cy, 0, "waste", False),                           # past the weir
              port("m3_release_1", "outlet", cx, row_down, top, "release", True)]
    channels += [chan("m3_ch_waste", "m3_trap_1", "m3_waste_1", "liquid"), chan("m3_ch_release", "m3_trap_1", "m3_release_1", "capsules")]
    if p["backflush"]:
        ports.insert(n, port("m3_backflush_1", "inlet", margin / 2, cy, 0, "backflush", False))
        channels.insert(n + 1, chan("m3_ch_backflush", "m3_backflush_1", "m3_trap_1", "liquid"))
    if len(ports) > MAX_PORTS:
        raise ParamError(f"{len(ports)} ports > the {MAX_PORTS} allowed per chip")
    retention = (f"a DEPTH WEIR at the waste side (block trap_chamber): the {MIN_OPEN_UM:g} um floor is in plane and z is free, so a "
                 f"{WEIR_CREST_UM:g} um crest, {_WEIR['crest_thickness_um']:g} um thick, leaves a {WEIR_GAP_UM:g} um gap "
                 f"under the lid, still one etch depth and one fluidic layer; the {p['mesh_opening_um']:g} um mesh of the original "
                 f"design cannot be printed. The weir is untested: z tolerance on a gap this small is the biggest unknown, so run a gap coupon first"
                 if weir else f"a bought {p['mesh_opening_um']:g} um mesh")
    chamber = {
        "id": "m3_trap_1", "type": "exchange", "center": [cx, cy, round(BASE_MM + h / 2, 4)], "size": [side, side, h],
        "height_um": DEPTH_UM, "volume_ul": round(side * side * h, 3), "block": "trap_chamber",
        "note": f"Single layer of {p['capsule_count']:,} capsules needs {s['monolayer_area_cm2'] * 100:.1f} mm2 (sizing.py).",
    }
    if weir:
        chamber["mesh_pore_um"] = None
        chamber["retention"] = {"type": "weir", "opening_um": WEIR_GAP_UM, "source": "fabricated"}
        chamber["note"] += f" Retention is a depth weir ({WEIR_CREST_UM:g} um crest, {WEIR_GAP_UM:g} um gap), not a mesh."
    else:
        chamber["retention"] = {"type": "mesh", "opening_um": p["mesh_opening_um"], "source": "bought"}
    spec = {
        "id": "m3_wash_v1",
        "name": "Module 3: capsule wash (real scale, draft)",
        "description": (f"One capsule trap for {p['capsule_count']:,} capsules as a single layer, held by {retention}: load at the top, "
                        "wash past the retention to waste, "
                        + ("back-flush to resuspend, " if p["backflush"] else "")
                        + f"release at the bottom. Build: {fx:g} x {fy:g} mm inside the 30 x 20 mm envelope, ONE fluidic layer at ONE "
                        f"etch depth of {DEPTH_MM:g} mm, channels {w * 1000:g} um (floor {MIN_OPEN_UM:g} um), ports {port_um:g} um, "
                        f"{len(ports)} ports, every chamber and channel a block from contracts/blocks.json. Superseded and kept for "
                        "reference. CLAMPS: " + ("; ".join(clamps) if clamps else "none") + ". "
                        "GENERATED by cad/generators/m3_wash.py; edit the parameters, not this file."),
        "placeholder": True,
        "module": {"id": "m3", "name": "Capsule wash (draft)", "status": "superseded", "printed": False,
                   "superseded_by": "chip/module3_v1_translated.json",
                   "note": "Superseded by Szonja's Module 3 (load, lysis, wash); kept for reference, out of the pipeline."},
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
        "chambers": [chamber],
        "channels": channels,
        "ports": ports,
    }
    if not weir:
        spec["trap"] = {"gap_um": p["mesh_opening_um"]}
    return spec


def protocol(p: dict, spec: dict) -> dict:
    hy = hydraulics(p)
    trap = spec["chambers"][0]
    q = hy["flow_ul_min"]
    # A LOAD fills the chamber, so it is capped at the chamber volume. An EXCHANGE flushes
    # THROUGH it and is meant to be several chamber volumes - capping it at one was wrong and
    # left the planner with no way to reach its exchange target ("no safe plan meets the goal").
    v_load = math.floor(trap["volume_ul"] * 10) / 10
    # An exchange must SWEEP the chamber, not fill it. Below about 2.7 chamber volumes the
    # planner cannot reach its exchange target and stops with "no safe plan meets the goal".
    # tests/test_operators.py had been overriding the shipped volume to 2.7 x for exactly this
    # reason, which hid the fact that the shipped protocol could not actually run.
    v = round(max(p["wash_volume_ul"], 2.7 * trap["volume_ul"]), 1)
    q_lift = max(q, _param("backflush.lift_flow_ul_min"))   # below this the bed does not lift
    v_release = round(max(v, 3 * trap["volume_ul"]), 1)      # sweep the trap out, not just fill it
    n = lambda washes: math.ceil(washes * p["wash_volume_ul"] / v - 1e-9)    # noqa: E731  same total volume as the nominal washes
    t = lambda washes: math.ceil(washes * v / q * 600 - 1e-9) / 10           # noqa: E731
    steps = [
        {"step": 1, "operation": "load", "target": "m3_trap_1", "reagent": None, "volume_ul": v_load, "duration_s": t(1),
         "temp_c": None, "washes": None, "flow_ul_min": q,
         "note": f"Load {p['capsule_count']:,} capsules through the top inlet; they settle in a single layer behind the weir."},
        {"step": 2, "operation": "exchange", "target": "m3_trap_1", "reagent": "wash_buffer", "volume_ul": v, "duration_s": t(n(3)),
         "temp_c": None, "washes": n(3), "flow_ul_min": q, "direction": "forward",
         "note": f"Wash {n(3)} x {v:g} uL (the volume of three {p['wash_volume_ul']:g} uL washes, cut to fit the chamber): buffer flows "
                 f"past the weir to waste while the capsules stay behind it."},
    ]
    if p["backflush"]:
        steps.append({"step": 3, "operation": "exchange", "target": "m3_trap_1", "reagent": "resuspension_buffer", "volume_ul": v,
                      "duration_s": t(n(1)), "temp_c": None, "washes": n(1), "flow_ul_min": q_lift, "direction": "backflush",
                      "note": "Back-flush from the far side to lift the capsules off the weir and resuspend them."})
    steps.append({"step": len(steps) + 1, "operation": "release", "target": "m3_trap_1", "reagent": "resuspension_buffer",
                  "volume_ul": v_release, "duration_s": t(v_release / v), "temp_c": None, "washes": None,
                  "flow_ul_min": q_lift,
                  "note": f"Flush the washed capsules out through the release outlet: {v_release:g} uL at "
                          f"{q_lift:g} uL/min. The volume is about three trap volumes because a release has to "
                          f"SWEEP the chamber, not just fill it, and the flow is the lift flow because below it "
                          f"the capsule bed does not come off the weir."})
    return {
        "id": "m3_wash",
        "name": "Module 3: capsule wash",
        "source": ("SPC-NAR (Leonaviciene & Mazutis, NAR 2023): capsule washes at 2000 g, 2 min, replaced here by a depth-weir trap "
                   "with back-flush; CapSeq (Baronas et al., Science 2026): repeated washes between steps. Timings from "
                   "operator/flow_calc.py. GENERATED by cad/generators/m3_wash.py."),
        "placeholder": True,
        "description": (f"Capsule wash on the Module 3 chip: load, {n(3)} washes past the depth weir to waste, "
                        f"{'back-flush to resuspend, ' if p['backflush'] else ''}release. {q:g} uL/min keeps the predicted "
                        f"pressure on the capsule bed at {hy['pressure_kpa']:.2f} kPa (limit {hy['limit_kpa']:.2f} kPa) and the wall "
                        f"shear at {hy['shear_dyn_cm2']:.4f} dyn/cm2 (limit {SHEAR_MAX_DYN_CM2:g}). Each wash is {v:g} uL, "
                        f"at most the chamber's {trap['volume_ul']:g} uL. Volumes are placeholders."),
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
    wash = min(p["wash_volume_ul"], math.floor(trap["volume_ul"] * 10) / 10)
    w = spec["channels"][0]["width_mm"] * 1000
    return [
        f"Module 3 sizing (operator/sizing.py + flow_calc.py) for {p['capsule_count']:,} capsules of {d:g} um:",
        f"  trap                        {trap['size'][0]:g} x {trap['size'][1]:g} mm single layer ({s['monolayer_area_cm2'] * 100:.1f} mm2), "
        f"{trap['height_um']:g} um high (swollen capsules {p['swollen_diameter_um']:g} um), {trap['volume_ul']:g} uL",
        f"  packed capsules             {s['packed_volume_ul']:.2f} uL",
        f"  retention                   " + (f"DEPTH WEIR, {WEIR_GAP_UM:g} um gap (design mesh {p['mesh_opening_um']:g} um cannot be printed)"
                                            if retention_is_weir(p) else f"bought mesh, {p['mesh_opening_um']:g} um")
        + f"; capsules {d:g} um, sheet 0.3-0.4 x = {s['trap_gap_low_um']:g}-{s['trap_gap_high_um']:g} um",
        f"  channels                    {w:g} um ({p['channel_width_factor']:g} x diameter; sheet 1.5-2 x = "
        f"{s['channel_width_low_um']:g}-{s['channel_width_high_um']:g} um)",
        f"  inlets / back-flush         {p['inlets']} / {'yes' if p['backflush'] else 'no'};  footprint "
        f"{spec['footprint']['x']:g} x {spec['footprint']['y']:g} mm",
        f"  wash flow                   {hy['flow_ul_min']:g} uL/min -> {hy['pressure_kpa']:.2f} kPa on a full capsule bed "
        f"(limit {hy['limit_kpa']:.2f} kPa; safe up to {hy['safe_flow_ul_min']:.0f} uL/min)",
        f"  one wash of {wash:g} uL             {wash / hy['flow_ul_min'] * 60:.0f} s "
        f"({wash / trap['volume_ul']:.1f} chamber volumes; ~{1 - math.exp(-wash / trap['volume_ul']):.0%} exchanged; "
        f"shear {hy['shear_dyn_cm2']:.4f} dyn/cm2, ceiling {SHEAR_MAX_DYN_CM2:g})",
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

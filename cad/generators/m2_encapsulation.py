"""Module 2, encapsulation (REAL scale, PARAMETRIC DRAFT), after CapSeq Fig. 1B and the
SPC methods paper (values via contracts/sources/chip_geometry_requirements.xlsx).

    python cad/generators/m2_encapsulation.py                      # defaults from the sheet
    python cad/generators/m2_encapsulation.py --nozzle-width-um 50 --oil-flow-ul-h 800
    python cad/generators/m2_encapsulation.py --help               # every parameter

Writes cad/m2_encapsulation_spec.json and protocols/m2_encapsulation.json, and prints the
sizing summary (operator/sizing.py). Parameters: contracts/schemas/generators/m2_encapsulation.schema.json.

Top view (75 x 25 mm; not to scale):

             oil branch (top) ----------------------+
             |                                      |
  core_in -[filter]-\\                               v
  oil_in-[filter]-split   Y ==== co-flow ==== NOZZLE ===== phase separation =====> emulsion_out
  shell_in-[filter]-/                               ^            (droplets -> core-shell capsules)
             |                                      |
             oil branch (bottom) -------------------+

     emulsion_out ~~> m2_cold_delay_1 (OFF-CHIP tubing on a cold block, 4 C, 15-30 min) ~~> emulsion_in

  Finishing trap (one capsule layer on a mesh, under a 405 nm curing window):
     emulsion_in (above) + breaker_in / buffer_in / pi_in (top edge)
     -> oil to m2_oil_waste_1 (below the mesh), aqueous to m2_waste_1 (below the mesh)
     -> back-flush (m2_backflush_1, below) -> m2_agg_filter_1 (100 um) -> m2_capsule_out_1 -> Module 3

The single oil inlet sits on the left edge and splits into two equal branches that pass
around the core and shell inlets, so oil reaches both sides of the nozzle without crossing
the aqueous line.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genlib  # noqa: E402
from genlib import ParamError, r  # noqa: E402

import schema_io  # noqa: E402
import sizing  # noqa: E402

NAME = "m2_encapsulation"
OUT = genlib.ROOT / "cad" / "m2_encapsulation_spec.json"
PROTOCOL_OUT = genlib.ROOT / "protocols" / "m2_encapsulation.json"

BASE_MM, LID_MM = 1.0, 1.0
PORT_UM = 500
OIL_PORT_X, OIL_SPLIT_X, AQ_PORT_X, ARM_X = 1.5, 4.0, 5.0, 8.0
ARM_DY = 3.0          # core / shell arms start 3 mm above / below the centre line
OIL_DY = 7.5          # oil branches run 7.5 mm above / below it
FILTER_LEN, FILTER_W = 1.4, 0.8


def sizing_for(p: dict) -> dict:
    """operator/sizing.py with this module's parameters."""
    return sizing.compute({
        "capsule_diameter_um": p["capsule_diameter_um"], "capsule_count": p["capsule_count"],
        "gelma_flow_ul_h": p["shell_flow_ul_h"], "cell_flow_ul_h": p["core_flow_ul_h"],
        "oil_flow_ul_h": p["oil_flow_ul_h"], "cell_conc_per_ul": p["cell_conc_per_ul"],
        "nozzle_width_um": p["nozzle_width_um"], "cell_filter_gap_um": p["cell_filter_gap_um"],
    })


def cold_delay_volume_ul(p: dict) -> tuple[float, float]:
    total = p["core_flow_ul_h"] + p["shell_flow_ul_h"] + p["oil_flow_ul_h"]
    return total * p["gelation_time_min_low"] / 60, total * p["gelation_time_min_high"] / 60


def build(p: dict) -> dict:
    if p["gelation_time_min_low"] > p["gelation_time_min_high"]:
        raise ParamError("gelation_time_min_low is longer than gelation_time_min_high")
    s = sizing_for(p)
    d = p["capsule_diameter_um"]
    fx, fy = p["footprint_x_mm"], p["footprint_y_mm"]
    cy = fy / 2
    nz_d = p["nozzle_depth_um"] / 1000
    h = p["trap_height_um"] / 1000
    top = BASE_MM + h + LID_MM
    jz = r(BASE_MM + nz_d / 2)
    tz = r(BASE_MM + h / 2)
    w_in, w_co = p["inlet_width_um"] / 1000, p["coflow_width_um"] / 1000
    yj_x = ARM_X + ARM_DY / math.tan(math.radians(p["y_junction_angle_deg"]) / 2)
    nz_x = yj_x + p["coflow_length_mm"]
    sep_end = nz_x + p["separation_length_mm"]
    side = math.sqrt(s["monolayer_area_cm2"] * 100)
    tx = fx - 9.0 - side / 2
    if tx - side / 2 - 3.0 < sep_end + 2.0:
        raise ParamError(f"layout doesn't fit: the separation channel ends at x = {sep_end:.1f} mm but the finishing "
                         f"trap ({side:.1f} mm for {p['capsule_count']:,} capsules) starts at x = {tx - side / 2 - 3:.1f} mm; "
                         f"increase footprint_x_mm, or shorten separation_length_mm / coflow_length_mm")
    if side + 9.0 > fy or OIL_DY + 1.0 > cy:
        raise ParamError(f"layout doesn't fit: the {side:.1f} mm trap needs footprint_y_mm >= {side + 9:.1f}")
    filters = p["inlet_filters"]

    def port(pid, typ, xy, z, role, passable=None, note=None):
        out = {"id": pid, "type": typ, "position": [r(xy[0], 3), r(xy[1], 3), r(z)], "role": role, "width_um": PORT_UM}
        if passable is not None:
            out["capsule_passable"] = passable
        if note:
            out["note"] = note
        return out

    def chan(cid, a, b, carries, path, w, dd, **extra):
        out = {"id": cid, "from": a, "to": b, "width_mm": r(w), "depth_mm": r(dd), "carries": carries}
        if path:
            out["path"] = [[r(x, 3), r(y, 3)] for x, y in path]
        out.update(extra)
        return out

    def junction(cid, xy, size_mm, note):
        return {"id": cid, "type": "junction", "center": [r(xy[0], 3), r(xy[1], 3), jz], "size": [size_mm, size_mm, r(nz_d)],
                "volume_ul": r(size_mm * size_mm * nz_d, 6), "note": note}

    def filt(cid, xy, purpose, gap_um, depth, z, note):
        return {"id": cid, "type": "filter", "center": [r(xy[0], 3), r(xy[1], 3), z], "size": [FILTER_LEN, FILTER_W, r(depth)],
                "volume_ul": r(FILTER_LEN * FILTER_W * depth, 5),
                "filter": {"type": "pillars", "gap_um": gap_um, "purpose": purpose}, "note": note}

    chambers = [
        junction("m2_yjunction_1", (yj_x, cy), 0.2, f"Y-junction: core and shell meet at {p['y_junction_angle_deg']:g} degrees "
                                                     f"and flow on side by side."),
        junction("m2_nozzle_1", (nz_x, cy), r(p["nozzle_width_um"] / 1000),
                 f"Flow-focusing nozzle, {p['nozzle_width_um']:g} x {p['nozzle_depth_um']:g} um: oil from both sides pinches "
                 f"the co-flow into droplets."),
        junction("m2_oil_split_1", (OIL_SPLIT_X, cy), 0.2, "Oil splits into two equal branches, one to each side of the nozzle."),
    ]
    ports = [port("m2_core_in_1", "inlet", (AQ_PORT_X, cy + ARM_DY), top, "cells", note="Cells in dextran, from Module 1b."),
             port("m2_shell_in_1", "inlet", (AQ_PORT_X, cy - ARM_DY), top, "reagent", note="GelMA."),
             port("m2_oil_in_1", "inlet", (OIL_PORT_X, cy), top, "oil")]
    channels = []
    for name, sign, carries in (("core", 1, "cells"), ("shell", -1, "liquid")):
        y = cy + sign * ARM_DY
        arm = [(ARM_X, y), (yj_x, cy)]
        if filters:
            fid = f"m2_filter_{name}_1"
            fxc = AQ_PORT_X + 0.8 + FILTER_LEN / 2
            chambers.append(filt(fid, (fxc, y), "cells" if name == "core" else "particles", p["cell_filter_gap_um"], nz_d, jz,
                                 f"Passive pillar filter on the {name} inlet."))
            channels += [chan(f"m2_ch_{name}_in", f"m2_{name}_in_1", fid, carries, [(AQ_PORT_X, y), (fxc - FILTER_LEN / 2, y)], w_in, nz_d),
                         chan(f"m2_ch_{name}", fid, "m2_yjunction_1", carries, [(fxc + FILTER_LEN / 2, y)] + arm, w_in, nz_d)]
        else:
            channels.append(chan(f"m2_ch_{name}", f"m2_{name}_in_1", "m2_yjunction_1", carries, [(AQ_PORT_X, y)] + arm, w_in, nz_d))
    if filters:
        fxo = (OIL_PORT_X + OIL_SPLIT_X) / 2
        chambers.append(filt("m2_filter_oil_1", (fxo, cy), "particles", p["cell_filter_gap_um"], nz_d, jz, "Passive pillar filter on the oil inlet."))
        channels += [chan("m2_ch_oil_in", "m2_oil_in_1", "m2_filter_oil_1", "oil", [(OIL_PORT_X, cy), (fxo - FILTER_LEN / 2, cy)], w_in, nz_d),
                     chan("m2_ch_oil_feed", "m2_filter_oil_1", "m2_oil_split_1", "oil", [(fxo + FILTER_LEN / 2, cy), (OIL_SPLIT_X, cy)], w_in, nz_d)]
    else:
        channels.append(chan("m2_ch_oil_in", "m2_oil_in_1", "m2_oil_split_1", "oil", [(OIL_PORT_X, cy), (OIL_SPLIT_X, cy)], w_in, nz_d))
    for n, sign in ((1, 1), (2, -1)):
        yb = cy + sign * OIL_DY
        channels.append(chan(f"m2_ch_oil_{n}", "m2_oil_split_1", "m2_nozzle_1", "oil",
                             [(OIL_SPLIT_X, cy), (OIL_SPLIT_X, yb), (nz_x, yb), (nz_x, cy)], w_in, nz_d,
                             note=f"Oil branch {'above' if sign > 0 else 'below'} the aqueous line (same length as the other)."))
    channels += [
        chan("m2_ch_coflow_1", "m2_yjunction_1", "m2_nozzle_1", "cells", [(yj_x, cy), (nz_x, cy)], w_co, nz_d,
             note="Co-flow: core and shell side by side, not mixing (laminar)."),
        chan("m2_ch_separation_1", "m2_nozzle_1", "m2_emulsion_out_1", "droplets", [(nz_x, cy), (sep_end, cy)],
             p["separation_width_um"] / 1000, p["separation_depth_um"] / 1000,
             note="Straight phase-separation channel: each droplet separates into a dextran core and a GelMA shell."),
    ]
    ports.append(port("m2_emulsion_out_1", "outlet", (sep_end, cy), top, "transfer", True, "Emulsion out to the off-chip cold delay."))

    # --- finishing trap -------------------------------------------------------------------
    y_top, y_bot = cy + side / 2, cy - side / 2
    reagent_y = y_top + 2.0
    bottom_y = y_bot - 2.0
    chambers.append({"id": "m2_trap_1", "type": "exchange", "center": [r(tx, 3), r(cy, 3), tz], "size": [r(side, 3), r(side, 3), r(h)],
                     "height_um": p["trap_height_um"], "volume_ul": r(side * side * h, 3),
                     "retention": {"type": "mesh", "opening_um": p["trap_mesh_um"], "source": "bought"},
                     "note": "Finishing trap: one layer of capsules on a mesh, under the curing window."})
    fax = tx + side / 2 + 2.0
    chambers.append(filt("m2_agg_filter_1", (fax, cy), "capsule_aggregates", p["aggregate_filter_gap_um"], h, tz,
                         "Capsule-aggregate filter: single capsules pass, clumps stay."))
    ports += [
        port("m2_emulsion_in_1", "inlet", (tx, cy), top, "load", True, "Emulsion back from the cold delay, above the trap."),
        port("m2_breaker_in_1", "inlet", (tx - 2.5, reagent_y), top, "reagent", note="Emulsion breaker."),
        port("m2_buffer_in_1", "inlet", (tx, reagent_y), top, "reagent", note="Rinse / wash buffer."),
        port("m2_pi_in_1", "inlet", (tx + 2.5, reagent_y), top, "reagent", note="Photoinitiator."),
        port("m2_backflush_1", "inlet", (tx - side / 2 - 2.0, cy), 0, "backflush", False),
        port("m2_oil_waste_1", "outlet", (tx - 1.5, bottom_y), 0, "waste", False, "Dedicated oil waste."),
        port("m2_waste_1", "outlet", (tx + 1.5, bottom_y), 0, "waste", False),
        port("m2_capsule_out_1", "outlet", (tx + side / 2 + 4.0, cy), top, "release", True, "Cured, washed capsules out to Module 3."),
    ]
    ww = 0.2  # release path: >= 1.5 x capsule diameter and >= the 150 um release outlet minimum
    channels += [
        chan("m2_ch_emulsion_in", "m2_emulsion_in_1", "m2_trap_1", "droplets", None, 0.3, h),
        chan("m2_ch_breaker", "m2_breaker_in_1", "m2_trap_1", "liquid", [(tx - 2.5, reagent_y), (tx - 2.5, y_top)], ww, h),
        chan("m2_ch_buffer", "m2_buffer_in_1", "m2_trap_1", "liquid", [(tx, reagent_y), (tx, y_top)], ww, h),
        chan("m2_ch_pi", "m2_pi_in_1", "m2_trap_1", "liquid", [(tx + 2.5, reagent_y), (tx + 2.5, y_top)], ww, h),
        chan("m2_ch_backflush", "m2_backflush_1", "m2_trap_1", "liquid", [(tx - side / 2 - 2.0, cy), (tx - side / 2, cy)], ww, h),
        chan("m2_ch_oil_waste", "m2_trap_1", "m2_oil_waste_1", "oil", [(tx - 1.5, y_bot), (tx - 1.5, bottom_y)], ww, h),
        chan("m2_ch_waste", "m2_trap_1", "m2_waste_1", "liquid", [(tx + 1.5, y_bot), (tx + 1.5, bottom_y)], ww, h),
        chan("m2_ch_release", "m2_trap_1", "m2_agg_filter_1", "capsules", [(tx + side / 2, cy), (fax - FILTER_LEN / 2, cy)], ww, h),
        chan("m2_ch_capsule_out", "m2_agg_filter_1", "m2_capsule_out_1", "capsules", [(fax + FILTER_LEN / 2, cy), (tx + side / 2 + 4.0, cy)], ww, h),
    ]
    lo, hi = cold_delay_volume_ul(p)
    tube_area = math.pi * (p["delay_tubing_id_mm"] / 2) ** 2   # mm2 = uL per mm
    spec = {
        "id": "m2_encapsulation_v1",
        "name": "Module 2: encapsulation (real scale, draft)",
        "description": ("Core (cells in dextran) and shell (GelMA) meet at a Y-junction and co-flow into a flow-focusing "
                        "nozzle with oil from both sides; droplets phase-separate into core-shell capsules along a straight "
                        "channel, gel in an off-chip cold delay, and come back to a finishing trap: emulsion breaker, oil "
                        "to its own waste, rinse x2, photoinitiator, 405 nm cure over one capsule layer, washes, back-flush "
                        "out through a 100 um aggregate filter. GENERATED by cad/generators/m2_encapsulation.py."),
        "placeholder": True,
        "module": {"id": "m2", "name": "Encapsulation", "status": "draft", "printed": True},
        "generator": {"script": "cad/generators/m2_encapsulation.py",
                      "params_schema": "contracts/schemas/generators/m2_encapsulation.schema.json", "params": p},
        "units": "mm",
        "footprint": {"x": fx, "y": fy},
        "layers": [
            {"id": "m2_base", "material": "pmma", "thickness_mm": BASE_MM},
            {"id": "m2_fluidic", "material": "pmma", "thickness_mm": r(h)},
            {"id": "m2_lid", "material": "pmma", "thickness_mm": LID_MM},
        ],
        "capsule": {"diameter_um": d, "swell_factor": s["inputs"]["swell_factor"], "count": p["capsule_count"]},
        "trap": {"gap_um": p["trap_mesh_um"]},
        "capsule_filter": {"gap_um": p["aggregate_filter_gap_um"]},
        "encapsulation": {"nozzle_um": p["nozzle_width_um"], "nozzle_depth_um": p["nozzle_depth_um"], "nozzle": "m2_nozzle_1",
                          "core_flow_ul_h": p["core_flow_ul_h"], "shell_flow_ul_h": p["shell_flow_ul_h"],
                          "oil_flow_ul_h": p["oil_flow_ul_h"], "core_inlet": "m2_core_in_1"},
        "chambers": chambers,
        "channels": channels,
        "ports": ports,
        "features": [{"id": "m2_curing_window_1", "type": "curing_window", "center": [r(tx, 3), r(cy, 3), r(top - 0.05)],
                      "size": [r(side + 1, 3), r(side + 1, 3), 0.1], "wavelength_nm": p["curing_wavelength_nm"],
                      "covers": ["m2_trap_1"],
                      "note": f"{p['curing_wavelength_nm']:g} nm-transparent window over a single capsule layer (lid: glass or "
                              f"clear thermoplastic); LED mount above."}],
        "stages": [{"id": "m2_cold_delay_1", "type": "cold_delay", "from_port": "m2_emulsion_out_1", "to_port": "m2_emulsion_in_1",
                    "location": "off_chip", "temp_c": p["gelation_temp_c"],
                    "time_min": [p["gelation_time_min_low"], p["gelation_time_min_high"]], "volume_ul": [r(lo, 1), r(hi, 1)],
                    "note": (f"At {p['core_flow_ul_h'] + p['shell_flow_ul_h'] + p['oil_flow_ul_h']:g} uL/h, "
                             f"{p['gelation_time_min_low']:g}-{p['gelation_time_min_high']:g} min of residence is "
                             f"{lo:.0f}-{hi:.0f} uL: far too much for the chip. Use off-chip tubing on a cold block, e.g. "
                             f"{lo / tube_area / 1000:.1f}-{hi / tube_area / 1000:.1f} m of {p['delay_tubing_id_mm']:g} mm ID tubing.")}],
    }
    if filters:
        spec["cell_filter"] = {"gap_um": p["cell_filter_gap_um"]}
    return spec


def protocol(p: dict) -> dict:
    s = sizing_for(p)
    t_enc = math.ceil(s["hours_to_encapsulate"] * 3600 * 10) / 10
    emulsion_ul = (p["core_flow_ul_h"] + p["shell_flow_ul_h"] + p["oil_flow_ul_h"]) * s["hours_to_encapsulate"]
    flow, vol = 20, 20
    step = lambda n, op, target, note, **kw: {  # noqa: E731
        "step": n, "operation": op, "target": target, "reagent": kw.pop("reagent", None), "volume_ul": kw.pop("volume_ul", None),
        "duration_s": kw.pop("duration_s"), "temp_c": kw.pop("temp_c", None), "washes": kw.pop("washes", None),
        "flow_ul_min": kw.pop("flow_ul_min", None), "note": note, **kw}
    wash = lambda n, reagent, washes, note, **kw: step(  # noqa: E731
        n, "exchange", "m2_trap_1", note, reagent=reagent, volume_ul=vol, washes=washes, flow_ul_min=flow,
        duration_s=washes * vol / flow * 60, **kw)
    return {
        "id": "m2_encapsulation",
        "name": "Module 2: encapsulation",
        "source": ("SPC-NAR (Leonaviciene & Mazutis, NAR 2023) and CapSeq (Baronas et al., Science 2026) via "
                   "contracts/sources/chip_geometry_requirements.xlsx: 40 um nozzle, 100 / 250 / 700 uL/h, gelation at 4 C "
                   "for 15-30 min, emulsion breaker + rinse x2, 0.1% LAP, 405 nm for 20 s, 100 um aggregate filter. "
                   "Wash volumes and flows are placeholders. GENERATED by cad/generators/m2_encapsulation.py."),
        "placeholder": True,
        "description": "Encapsulate, gel in the cold delay, break the emulsion, rinse, cure, wash, send the capsules on to Module 3.",
        "steps": [
            step(1, "encapsulate", "m2_nozzle_1",
                 f"Encapsulate {p['capsule_count']:,} capsules: about {s['generation_rate_hz']:.0f} droplets a second, "
                 f"~{s['mean_cells_per_capsule']:.2f} cells per capsule, so most capsules are empty and almost no occupied "
                 f"capsule holds two cells.",
                 duration_s=t_enc, flows_ul_h={"core": p["core_flow_ul_h"], "shell": p["shell_flow_ul_h"], "oil": p["oil_flow_ul_h"]},
                 capsules=p["capsule_count"]),
            step(2, "hold_temp", "m2_cold_delay_1",
                 f"Cold delay: {p['gelation_temp_c']:g} C for {p['gelation_time_min_low']:g} min in tubing on a cold block, "
                 f"so the GelMA shells set.", temp_c=p["gelation_temp_c"], duration_s=p["gelation_time_min_low"] * 60),
            step(3, "load", "m2_trap_1", f"The emulsion (~{emulsion_ul:.1f} uL plus a chase) comes back onto the chip; "
                 f"the capsules settle in one layer on the trap mesh.", volume_ul=vol, duration_s=60),
            wash(4, "emulsion_breaker", 1, "Break the emulsion: the oil drains to its own waste, the capsules stay on the mesh.",
                 waste="m2_oil_waste_1"),
            wash(5, "wash_buffer", 2, "Rinse twice with buffer, still to the oil waste.", waste="m2_oil_waste_1"),
            wash(6, "photoinitiator", 1, "Photoinitiator in (0.1% LAP in the paper)."),
            step(7, "cure", "m2_trap_1", f"Cure: {p['curing_wavelength_nm']:g} nm light for {p['curing_time_s']:g} s through the "
                 f"window over the single capsule layer crosslinks the shells.",
                 duration_s=p["curing_time_s"], wavelength_nm=p["curing_wavelength_nm"]),
            wash(8, "wash_buffer", 3, "Wash three times to clear the photoinitiator."),
            wash(9, "resuspension_buffer", 1, "Back-flush from below to lift the capsules off the mesh.", direction="backflush"),
            step(10, "release", "m2_trap_1", "Capsules out through the 100 um aggregate filter to m2_capsule_out_1, on to Module 3.",
                 reagent="resuspension_buffer", volume_ul=vol, duration_s=60),
        ],
    }


def summary(p: dict, spec: dict) -> list[str]:
    s = sizing_for(p)
    trap = next(c for c in spec["chambers"] if c["id"] == "m2_trap_1")
    lo, hi = cold_delay_volume_ul(p)
    mean = s["mean_cells_per_capsule"]
    return [
        f"Module 2 sizing (operator/sizing.py) for N = {p['capsule_count']:,} capsules of {p['capsule_diameter_um']:g} um:",
        f"  droplet rate                {s['generation_rate_hz']:,.0f} Hz",
        f"  capsules per hour           {s['capsules_per_hour']:,.0f}",
        f"  mean cells per capsule      {mean:.3f} (Poisson: {math.exp(-mean):.1%} empty, {s['fraction_single_cell']:.1%} with one cell, "
        f"{s['fraction_multi_of_occupied']:.1%} of occupied ones with >1)  at {p['cell_conc_per_ul']:g} cells/uL",
        f"  time to make N capsules     {s['hours_to_encapsulate'] * 3600:.1f} s ({s['hours_to_encapsulate'] * 60:.2f} min)",
        f"  finishing trap              {trap['size'][0]:g} x {trap['size'][1]:g} mm single layer ({s['monolayer_area_cm2'] * 100:.1f} mm2), "
        f"{trap['height_um']:g} um high, {trap['volume_ul']:g} uL; packed capsules {s['packed_volume_ul']:.2f} uL",
        f"  cold delay                  {p['gelation_time_min_low']:g}-{p['gelation_time_min_high']:g} min at "
        f"{p['core_flow_ul_h'] + p['shell_flow_ul_h'] + p['oil_flow_ul_h']:g} uL/h = {lo:.0f}-{hi:.0f} uL -> OFF-CHIP tubing on a cold block",
    ]


def main(argv: list[str] | None = None) -> int:
    try:
        params, args = genlib.parse_params(NAME, argv, "Module 2 encapsulation (parametric draft) -> cad/m2_encapsulation_spec.json")
        spec = build(params)
        out = args.out or OUT
        genlib.write_spec(spec, out)
        proto = protocol(params)
        errors = schema_io.schema_errors("protocol", proto)
        if errors:
            raise ParamError("generated protocol breaks the protocol schema: " + "; ".join(schema_io.describe(e) for e in errors[:5]))
        if not args.out:
            schema_io.write_json(PROTOCOL_OUT, proto)
    except ParamError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"wrote {genlib.rel(out)}" + ("" if args.out else f" and {genlib.rel(PROTOCOL_OUT)}"))
    return genlib.finish(out, summary(params, spec), args, [(PROTOCOL_OUT, OUT)])


if __name__ == "__main__":
    raise SystemExit(main())

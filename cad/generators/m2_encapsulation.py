"""Module 2, encapsulation (REFERENCE ONLY, buildable stand-in), after CapSeq Fig. 1B and the
SPC methods paper (values via contracts/sources/chip_geometry_requirements.xlsx).

    python cad/generators/m2_encapsulation.py                      # defaults from the sheet
    python cad/generators/m2_encapsulation.py --nozzle-width-um 50 --oil-flow-ul-h 800
    python cad/generators/m2_encapsulation.py --help               # every parameter

Writes cad/m2_encapsulation_spec.json and protocols/m2_encapsulation.json, and prints the
sizing summary (operator/sizing.py). Parameters: contracts/schemas/generators/m2_encapsulation.schema.json.

HONEST FINDING. This is a flow-focusing droplet generator whose nozzle must be about 40 um
wide to make 70 um capsules. contracts/blocks.json gives a 200 um in-plane floor, one fluidic
layer and ONE depth (0.3 mm), so the generator CLAMPS the nozzle (and every other narrow
channel) to 200 um and drops every shallow depth to 0.3 mm. That makes the chip buildable
and it passes every check, but it does NOT preserve the function: a 200 um nozzle makes far
larger droplets, if any. Droplet generation at this scale is not achievable by resin
printing or hot embossing. The pipeline has retired this chip in favour of spc_demo_v1
(encapsulation off-chip); this one is kept for reference only. Every clamp is listed in the
spec description.

Everything lives in the 30 x 20 mm envelope (footprint parameters above it are clamped),
one depth, blocks from the library only, six ports (merged by off-chip selector valves).
Top view (not to scale):

              oil branch (top) -----------------+
              |                                 |
   core_in -[weir]-\\                            v
   oil_in -[weir]-split  Y == co-flow == NOZZLE == separation == capsule_out ==[weir]== trap_in
   shell_in-[weir]-/                             ^                 (port shared with the     |  (top)
              |                                 |                  trap release)          trap
              oil branch (bottom) --------------+                                           |
                                                                                    under_mesh (bottom)
     capsule_out ~~> m2_cold_delay_1 (OFF-CHIP tubing on a cold block, 4 C, 15-30 min) ~~> trap_in

The finishing trap is one capsule layer held by a DEPTH WEIR (trap_chamber block), not a
mesh: the 200 um floor is in plane, z is free (contracts/blocks.json, "retention"). Breaker,
buffer and photoinitiator share trap_in; waste, oil waste and back-flush share under_mesh.
The single oil inlet splits into two equal branches that pass around the core and shell
inlets, so oil reaches both sides of the nozzle without crossing the aqueous line.
"""

from __future__ import annotations

import json
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

_BLOCKS = json.loads((genlib.ROOT / "contracts" / "blocks.json").read_text(encoding="utf-8"))
_ENV, _FLOORS, _WEIR = _BLOCKS["envelope"], _BLOCKS["fabrication_floors"], _BLOCKS["retention"]["weir_geometry"]
DEPTH_MM = _ENV["max_depth_mm"]                       # the ONE etch depth
DEPTH_UM = DEPTH_MM * 1000
WEIR_GAP_UM = _WEIR["gap_um"]
WEIR_CREST_UM = DEPTH_UM - WEIR_GAP_UM                # crest = this chip's etch depth minus the gap
WEIR_CREST_THICK_UM = _WEIR["crest_thickness_um"]     # in plane, so it IS the in-plane floor
MIN_OPEN_UM = _FLOORS["min_open_channel_mm"] * 1000   # in-plane floor
PORT_UM = _FLOORS["min_port_mm"] * 1000
PITCH_MM = _FLOORS["min_port_pitch_mm"]
MAX_PORTS = _FLOORS["max_ports_per_chip"]
# The weir as DATA: the gap is in z (no in-plane floor applies), the crest height is derived
# from THIS chip's etch depth, the thickness is the in-plane floor. retains_um is added in
# build(), where the capsule diameter is known.
RETENTION = {"type": "weir", "opening_um": WEIR_GAP_UM, "source": "fabricated",
             "crest_height_um": WEIR_CREST_UM, "crest_thickness_um": WEIR_CREST_THICK_UM}
MU_PA_S = genlib.parameter("fluid.viscosity_37c_pa_s")   # 37 C medium, not water at 20 C
SHEAR_MAX_DYN_CM2 = 0.01        # capsules on the trap: wall shear ceiling

BASE_MM, LID_MM = 1.0, 1.0
GAP_MM = 0.6                    # clear space between neighbouring features (floor is 0.4 mm)
EDGE_MM = 1.0                   # clear space to the board edge
FILTER_LEN, FILTER_W = 1.2, 0.8
OIL_PORT_X, AQ_PORT_X = 1.5, 6.0
AQ_ROW_MM = 3.5                 # core / shell ports sit this far inside the top / bottom edge
ARM_DY = 1.6                    # core / shell arms start 1.6 mm above / below the centre line
PORT_ROW_MM = 1.5               # trap ports sit this far beyond the trap's top / bottom
MIN_TRAP_W_MM = 2.0
FLOW_MAX_UL_MIN = 20            # placeholder wash flow, before the shear ceiling


def shear_dyn_cm2(flow_ul_min: float, w_mm: float, h_mm: float) -> float:
    """Wall shear tau = 6 mu Q / (w h^2) in a wide slit, in dyn/cm2 (1 Pa = 10 dyn/cm2)."""
    q = flow_ul_min * 1e-9 / 60
    return 10 * 6 * MU_PA_S * q / ((w_mm / 1000) * (h_mm / 1000) ** 2)


def max_flow_ul_min(w_mm: float, h_mm: float) -> float:
    """Highest flow that keeps shear_dyn_cm2() at or under SHEAR_MAX_DYN_CM2 for a w x h section."""
    return SHEAR_MAX_DYN_CM2 / 10 * (w_mm / 1000) * (h_mm / 1000) ** 2 / (6 * MU_PA_S) * 60e9


def footprint(p: dict) -> tuple[float, float]:
    return min(p["footprint_x_mm"], _ENV["max_x_mm"]), min(p["footprint_y_mm"], _ENV["max_y_mm"])


def trap_size(p: dict) -> tuple[float, float, float]:
    """(x, y, volume_ul) of the finishing trap from the monolayer area, one depth, as tall as the board allows."""
    area = sizing.compute({"capsule_count": p["capsule_count"], "capsule_diameter_um": p["capsule_diameter_um"]})["monolayer_area_cm2"] * 100
    th = max(PITCH_MM, min(footprint(p)[1] - 2 * (PORT_ROW_MM + 1.5), area / MIN_TRAP_W_MM))
    tw = area / th
    return r(tw, 3), r(th, 3), r(tw * th * DEPTH_MM, 3)


def wash_flow_ul_min(p: dict) -> float:
    tw, th, _ = trap_size(p)
    return float(math.floor(min(FLOW_MAX_UL_MIN, max_flow_ul_min(th, DEPTH_MM))))


def sizing_for(p: dict) -> dict:
    """operator/sizing.py with this module's parameters."""
    return sizing.compute({
        "capsule_diameter_um": p["capsule_diameter_um"], "capsule_count": p["capsule_count"],
        "shell_flow_ul_h": p["shell_flow_ul_h"], "cell_flow_ul_h": p["core_flow_ul_h"],
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
    clamps: list[str] = []

    def floor_um(label: str, value_um: float) -> float:
        """In-plane features cannot be printed below the open-channel floor: clamp, and say so."""
        if value_um < MIN_OPEN_UM:
            clamps.append(f"{label} {value_um:g} -> {MIN_OPEN_UM:g} um")
            return MIN_OPEN_UM
        return value_um

    fx, fy = footprint(p)
    if (fx, fy) != (p["footprint_x_mm"], p["footprint_y_mm"]):
        clamps.append(f"footprint {p['footprint_x_mm']:g} x {p['footprint_y_mm']:g} -> {fx:g} x {fy:g} mm")
    for label, key in (("nozzle depth", "nozzle_depth_um"), ("separation depth", "separation_depth_um"),
                       ("trap height", "trap_height_um")):
        if abs(p[key] - DEPTH_UM) > 1e-9:
            clamps.append(f"{label} {p[key]:g} -> {DEPTH_UM:g} um (one etch depth)")
    nozzle_um = floor_um("nozzle width", p["nozzle_width_um"])
    w_in = floor_um("inlet channel width", p["inlet_width_um"]) / 1000
    w_co = floor_um("co-flow channel width", p["coflow_width_um"]) / 1000
    w_sep = floor_um("separation channel width", p["separation_width_um"]) / 1000
    ww = MIN_OPEN_UM / 1000   # release path: >= 1.5 x capsule diameter and the 150 um release outlet minimum
    if PORT_UM > 500:
        clamps.append(f"port 500 -> {PORT_UM:g} um")
    if p["trap_mesh_um"] < MIN_OPEN_UM:
        clamps.append(f"trap mesh {p['trap_mesh_um']:g} um -> depth weir, {WEIR_GAP_UM:g} um gap under the lid")
    for label, key in (("inlet filter gap", "cell_filter_gap_um"), ("aggregate filter gap", "aggregate_filter_gap_um")):
        if p[key] < MIN_OPEN_UM:
            clamps.append(f"{label} {p[key]:g} um -> depth weir (gap is in z, not in plane)")

    cy = fy / 2
    h = DEPTH_MM
    top = BASE_MM + h + LID_MM
    z = r(BASE_MM + h / 2)
    half_port = PORT_UM / 2000
    yj_x_arm = AQ_PORT_X + half_port + GAP_MM + FILTER_LEN + GAP_MM + 0.2   # arms turn just past the inlet filters
    yj_x = yj_x_arm + ARM_DY / math.tan(math.radians(p["y_junction_angle_deg"]) / 2)
    nz_x = yj_x + p["coflow_length_mm"]
    oil_split_x = OIL_PORT_X + half_port + GAP_MM + FILTER_LEN + 0.8
    tw, th, trap_ul = trap_size(p)
    if p["separation_length_mm"] > fx:
        raise ParamError(f"layout doesn't fit: separation_length_mm {p['separation_length_mm']:g} is longer than the whole "
                         f"{fx:g} mm board")
    release_len = half_port + GAP_MM + FILTER_LEN + 0.8                       # out port to trap, through the aggregate filter
    max_sep = fx - EDGE_MM - tw - release_len - nz_x
    sep_len = min(p["separation_length_mm"], max_sep)
    if sep_len < 1.0:
        raise ParamError(f"layout doesn't fit: the finishing trap ({tw:.1f} x {th:.1f} mm for {p['capsule_count']:,} capsules) "
                         f"leaves {max_sep:.1f} mm for the separation channel on a {fx:g} mm board")
    if sep_len < p["separation_length_mm"]:
        clamps.append(f"separation channel {p['separation_length_mm']:g} -> {sep_len:.1f} mm (board length)")
    if th + 2 * (PORT_ROW_MM + half_port) > fy or cy - AQ_ROW_MM < 1.0:
        raise ParamError(f"layout doesn't fit: the {th:.1f} mm trap needs footprint_y_mm >= {th + 2 * (PORT_ROW_MM + half_port):.1f}")
    out_x = nz_x + sep_len
    trap_left = out_x + release_len
    tx = trap_left + tw / 2
    fa_x = out_x + half_port + GAP_MM + FILTER_LEN / 2
    filters = p["inlet_filters"]
    weir = f"depth weir: {WEIR_CREST_UM:g} um crest, {WEIR_GAP_UM:g} um gap under the lid"

    def port(pid, typ, xy, zz, role, passable=None, note=None):
        out = {"id": pid, "type": typ, "position": [r(xy[0], 3), r(xy[1], 3), r(zz)], "role": role, "width_um": PORT_UM}
        if passable is not None:
            out["capsule_passable"] = passable
        if note:
            out["note"] = note
        return out

    links: list[tuple] = []

    def chan(cid, a, b, carries, w, **extra):
        """Every channel is a straight run between two anchors (port, chamber or corner): bends are corner chambers."""
        links.append((cid, a, b, carries, w, extra))

    def junction(cid, xy, size_mm, block, note):
        return {"id": cid, "type": "junction", "center": [r(xy[0], 3), r(xy[1], 3), z], "size": [r(size_mm, 4), r(size_mm, 4), r(h)],
                "volume_ul": r(size_mm * size_mm * h, 6), "block": block, "note": note}

    def filt(cid, xy, purpose, gap_um, note):
        # An opening quoted below the in-plane floor must say so: it is NOT a buildable in-plane
        # gap, and the only reason this chamber exists is that the same number works in z.
        why = (f"Its {gap_um:g} um opening is BELOW THE {MIN_OPEN_UM:g} um IN-PLANE FLOOR, so it cannot be built as a "
               f"pillar gap or an in-plane slit at all. " if gap_um < MIN_OPEN_UM else "")
        return {"id": cid, "type": "filter", "center": [r(xy[0], 3), r(xy[1], 3), z], "size": [FILTER_LEN, FILTER_W, r(h)],
                "volume_ul": r(FILTER_LEN * FILTER_W * h, 6), "block": "trap_chamber", "mesh_pore_um": None,
                "filter": {"type": "mesh", "gap_um": gap_um, "purpose": purpose},
                "note": f"{note} {why}It is built as a DEPTH WEIR instead: a {DEPTH_UM - gap_um:g} um tall x "
                        f"{WEIR_CREST_THICK_UM:g} um thick crest (this chip's {DEPTH_UM:g} um etch depth minus the gap, "
                        f"thickness = the in-plane floor) leaves the {gap_um:g} um gap under the lid in Z, where the "
                        f"in-plane floor does not apply. Not pillars, not a mesh, and not a capsule trap."}

    coflow_clamped = ("" if p["coflow_width_um"] >= MIN_OPEN_UM else
                      f" BUILT {MIN_OPEN_UM:g} um WIDE, not the {p['coflow_width_um']:g} um this design asks for: that is below "
                      f"the {MIN_OPEN_UM:g} um in-plane floor, so the junction and the co-flow channel are clamped UP and the two "
                      f"streams meet {MIN_OPEN_UM / p['coflow_width_um']:.1f}x wider than designed.")
    chambers = [
        junction("m2_yjunction_1", (yj_x, cy), MIN_OPEN_UM / 1000, "y_junction",
                 f"Y-junction: core and shell meet at {p['y_junction_angle_deg']:g} degrees and flow on side by side."
                 + coflow_clamped),
        junction("m2_nozzle_1", (nz_x, cy), nozzle_um / 1000, "flow_focusing",
                 f"Flow-focusing nozzle, {nozzle_um:g} x {DEPTH_UM:g} um as BUILT (design intent {p['nozzle_width_um']:g} um, clamped to "
                 f"the {MIN_OPEN_UM:g} um floor): oil from both sides pinches the co-flow. Will not make 70 um capsules."),
        junction("m2_oil_split_1", (oil_split_x, cy), MIN_OPEN_UM / 1000, "fork_junction",
                 "Oil splits into two equal branches, one to each side of the nozzle."),
    ]
    ports = [port("m2_core_in_1", "inlet", (AQ_PORT_X, cy + (cy - AQ_ROW_MM)), top, "cells", note="Cells in dextran, from Module 1b."),
             port("m2_shell_in_1", "inlet", (AQ_PORT_X, AQ_ROW_MM), top, "reagent", note="GelMA."),
             port("m2_oil_in_1", "inlet", (OIL_PORT_X, cy), top, "oil")]
    fxc = AQ_PORT_X + half_port + GAP_MM + FILTER_LEN / 2
    for name, sign, carries in (("core", 1, "cells"), ("shell", -1, "liquid")):
        y = cy + sign * (cy - AQ_ROW_MM)
        ya = cy + sign * ARM_DY
        chambers += [junction(f"m2_corner_{name}_a", (yj_x_arm, y), MIN_OPEN_UM / 1000, "channel_corner",
                              f"Corner: the {name} line turns toward the centre line."),
                     junction(f"m2_corner_{name}_b", (yj_x_arm, ya), MIN_OPEN_UM / 1000, "channel_corner",
                              f"Corner: the {name} arm turns toward the Y-junction.")]
        if filters:
            fid = f"m2_filter_{name}_1"
            chambers.append(filt(fid, (fxc, y), "cells" if name == "core" else "particles", p["cell_filter_gap_um"],
                                 f"Passive filter on the {name} inlet."))
            chan(f"m2_ch_{name}_in", f"m2_{name}_in_1", fid, carries, w_in)
            chan(f"m2_ch_{name}_f", fid, f"m2_corner_{name}_a", carries, w_in)
        else:
            chan(f"m2_ch_{name}_f", f"m2_{name}_in_1", f"m2_corner_{name}_a", carries, w_in)
        chan(f"m2_ch_{name}_v", f"m2_corner_{name}_a", f"m2_corner_{name}_b", carries, w_in)
        chan(f"m2_ch_{name}", f"m2_corner_{name}_b", "m2_yjunction_1", carries, w_in)
    if filters:
        fxo = OIL_PORT_X + half_port + GAP_MM + FILTER_LEN / 2
        chambers.append(filt("m2_filter_oil_1", (fxo, cy), "particles", p["cell_filter_gap_um"], "Passive filter on the oil inlet."))
        chan("m2_ch_oil_in", "m2_oil_in_1", "m2_filter_oil_1", "oil", w_in)
        chan("m2_ch_oil_feed", "m2_filter_oil_1", "m2_oil_split_1", "oil", w_in)
    else:
        chan("m2_ch_oil_in", "m2_oil_in_1", "m2_oil_split_1", "oil", w_in)
    for n, sign in ((1, 1), (2, -1)):
        yb = cy + sign * (cy - EDGE_MM)
        side = "above" if sign > 0 else "below"
        chambers += [junction(f"m2_corner_oil_{n}a", (oil_split_x, yb), MIN_OPEN_UM / 1000, "channel_corner", f"Corner: oil branch {side} the aqueous line."),
                     junction(f"m2_corner_oil_{n}b", (nz_x, yb), MIN_OPEN_UM / 1000, "channel_corner", f"Corner: oil branch {side} turns into the nozzle.")]
        chan(f"m2_ch_oil_{n}_up", "m2_oil_split_1", f"m2_corner_oil_{n}a", "oil", w_in)
        chan(f"m2_ch_oil_{n}_across", f"m2_corner_oil_{n}a", f"m2_corner_oil_{n}b", "oil", w_in,
             note=f"Oil branch {side} the aqueous line (same length as the other).")
        chan(f"m2_ch_oil_{n}", f"m2_corner_oil_{n}b", "m2_nozzle_1", "oil", w_in)
    chan("m2_ch_coflow_1", "m2_yjunction_1", "m2_nozzle_1", "cells", w_co,
         note="Co-flow: core and shell side by side, not mixing (laminar).")
    chan("m2_ch_separation_1", "m2_nozzle_1", "m2_capsule_out_1", "droplets", w_sep,
         note="Straight phase-separation channel: each droplet separates into a dextran core and a GelMA shell.")

    # --- finishing trap -------------------------------------------------------------------
    y_top, y_bot = cy + th / 2, cy - th / 2
    in_y, under_y = y_top + PORT_ROW_MM, y_bot - PORT_ROW_MM
    chambers.append({"id": "m2_trap_1", "type": "exchange", "center": [r(tx, 3), r(cy, 3), z], "size": [tw, th, r(h)],
                     "height_um": DEPTH_UM, "volume_ul": trap_ul, "mesh_pore_um": None, "block": "trap_chamber",
                     "retention": {**RETENTION, "retains_um": d},
                     "note": f"Finishing trap: one layer of capsules held by a {weir} at the outlet, under the curing window. "
                             f"Not a mesh (a {p['trap_mesh_um']:g} um mesh cannot be printed). The crest is "
                             f"{WEIR_CREST_THICK_UM:g} um thick in plane (the floor) and {WEIR_CREST_UM:g} um tall in z "
                             f"(this chip's {DEPTH_UM:g} um etch depth minus the gap); all of it is in retention "
                             f"(crest_height_um, crest_thickness_um, opening_um, retains_um {d:g} um), not just in this note."})
    chambers.append(filt("m2_agg_filter_1", (fa_x, cy), "capsule_aggregates", p["aggregate_filter_gap_um"],
                         "Capsule-aggregate filter: single capsules pass, clumps stay."))
    ports += [
        port("m2_capsule_out_1", "outlet", (out_x, cy), top, "transfer", True,
             "Emulsion out to the off-chip cold delay, and later the cured capsules out to Module 3 (off-chip selector valve)."),
        port("m2_trap_in_1", "inlet", (tx, in_y), top, "load", True,
             "Emulsion back from the cold delay; breaker, buffer and photoinitiator come in here too (selector valve)."),
        port("m2_under_mesh_1", "outlet", (tx, under_y), 0, "waste", False,
             "Under the weir: waste, oil waste and back-flush share this port (selector valve)."),
    ]
    chan("m2_ch_trap_in", "m2_trap_in_1", "m2_trap_1", "droplets", 1.5 * ww)
    chan("m2_ch_under_mesh", "m2_trap_1", "m2_under_mesh_1", "liquid", ww)
    chan("m2_ch_release", "m2_trap_1", "m2_agg_filter_1", "capsules", ww)
    chan("m2_ch_capsule_out", "m2_agg_filter_1", "m2_capsule_out_1", "capsules", ww)
    pos = {c["id"]: c["center"][:2] for c in chambers} | {q["id"]: q["position"][:2] for q in ports}
    channels = [{"id": cid, "from": a, "to": b, "width_mm": r(w), "depth_mm": r(h), "carries": carries,
                 "block": "straight_channel", "path": [[r(pos[a][0], 3), r(pos[a][1], 3)], [r(pos[b][0], 3), r(pos[b][1], 3)]], **extra}
                for cid, a, b, carries, w, extra in links]
    if len(ports) > MAX_PORTS:
        raise ParamError(f"{len(ports)} ports > the {MAX_PORTS} allowed per chip")
    lo, hi = cold_delay_volume_ul(p)
    tube_area = math.pi * (p["delay_tubing_id_mm"] / 2) ** 2   # mm2 = uL per mm
    spec = {
        "id": "m2_encapsulation_v1",
        "name": "Module 2: encapsulation (REFERENCE ONLY, 30 x 20 mm; droplet generation NOT achievable)",
        "description": (
            "*** REFERENCE ONLY. THIS CHIP CANNOT MAKE CAPSULES. *** m2 is a flow-focusing droplet generator whose nozzle must be "
            f"about {p['nozzle_width_um']:g} um wide to make {d:g} um capsules. The fabrication floor is {MIN_OPEN_UM:g} um in plane, so the "
            f"generator CLAMPS the nozzle to {nozzle_um:g} um: that makes the chip buildable and it passes every check, but it does not "
            "preserve the function. A 200 um nozzle makes far larger droplets, if any. Droplet generation at this scale is not "
            "achievable by resin printing or hot embossing. The pipeline has retired this chip in favour of spc_demo_v1 and moved "
            "encapsulation off-chip; this spec is kept for reference only, to show what the chip would be. "
            f"BUILD: 30 x 20 mm envelope, ONE fluidic layer at ONE etch depth of {DEPTH_MM:g} mm everywhere (the old 0.04 / 0.08 / "
            f"0.15 mm depths would be second processes), every channel at or above {MIN_OPEN_UM:g} um, ports {PORT_UM:g} um, "
            f"{len(ports)} ports (limit {MAX_PORTS}, pitch >= {PITCH_MM:g} mm) merged by off-chip selector valves: emulsion out and "
            "capsule out share m2_capsule_out_1; emulsion in, breaker, buffer and photoinitiator share m2_trap_in_1; waste, oil "
            "waste and back-flush share m2_under_mesh_1, so carry-over and dead volume are real. Every chamber and channel carries a "
            "block from contracts/blocks.json; chamber volumes are computed from their boxes. "
            f"RETENTION: the finishing trap and the three filters are DEPTH WEIRS (trap_chamber block), not meshes or pillar arrays: "
            f"the {MIN_OPEN_UM:g} um floor is in plane and z is free, so a {WEIR_CREST_UM:g} um tall, {WEIR_CREST_THICK_UM:g} um "
            f"thick crest leaves a {WEIR_GAP_UM:g} um gap under the lid, still one layer. That crest height is not quoted from "
            f"anywhere: it is this chip's {DEPTH_UM:g} um etch depth minus the gap, and the trap carries it as data "
            f"(m2_trap_1.retention.crest_height_um / crest_thickness_um / retains_um), so the weir can be machined from this spec. "
            f"The weir is untested: coupon 21 / 24 / 27 um gaps, which span the legal band around {WEIR_GAP_UM:g} um (a 40-80 um "
            f"gap would simply pass a {d:g} um capsule). "
            "Core (cells in dextran) and shell (GelMA) meet at a Y-junction and co-flow into the nozzle with oil from both sides; "
            "droplets would phase-separate along a straight channel, gel in an off-chip cold delay, and come back to the trap: "
            "emulsion breaker, rinse x2, photoinitiator, 405 nm cure over one capsule layer, washes, back-flush out through the "
            "aggregate filter. CLAMPS: " + ("; ".join(clamps) if clamps else "none") + ". "
            "GENERATED by cad/generators/m2_encapsulation.py; edit the generator, not this file."),
        "placeholder": True,
        "module": {"id": "m2", "name": "Encapsulation (retired)", "status": "superseded", "printed": False,
                   "superseded_by": "chip/spc_demo_v1.json",
                   "note": "Superseded, not draft: the nozzle clamps to the 200 um in-plane floor, so this board "
                           "cannot make the pipeline's 70 um capsule, and protocols/pipeline_mvp.json has already "
                           "replaced it with spc_demo_v1 for the flow-focusing geometry and the commercial droplet "
                           "kit for the capsules themselves. It stayed 'draft' long after that decision, which read "
                           "as work in progress rather than a design that lost. Kept for reference, out of the "
                           "pipeline."},
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
        "capsule_filter": {"gap_um": p["aggregate_filter_gap_um"]},
        "encapsulation": {"nozzle_um": nozzle_um, "nozzle_depth_um": DEPTH_UM, "nozzle": "m2_nozzle_1",
                          "core_flow_ul_h": p["core_flow_ul_h"], "shell_flow_ul_h": p["shell_flow_ul_h"],
                          "oil_flow_ul_h": p["oil_flow_ul_h"], "core_inlet": "m2_core_in_1"},
        "chambers": chambers,
        "channels": channels,
        "ports": ports,
        "features": [{"id": "m2_curing_window_1", "type": "curing_window", "center": [r(tx, 3), r(cy, 3), r(top - 0.05)],
                      "size": [r(tw + 1, 3), r(th + 1, 3), 0.1], "wavelength_nm": p["curing_wavelength_nm"],
                      "covers": ["m2_trap_1"],
                      "note": f"{p['curing_wavelength_nm']:g} nm-transparent window over a single capsule layer (lid: glass or "
                              f"clear thermoplastic); LED mount above."}],
        "stages": [{"id": "m2_cold_delay_1", "type": "cold_delay", "from_port": "m2_capsule_out_1", "to_port": "m2_trap_in_1",
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
    tw, th, trap_ul = trap_size(p)
    t_enc = math.ceil(s["hours_to_encapsulate"] * 3600 * 10) / 10
    emulsion_ul = (p["core_flow_ul_h"] + p["shell_flow_ul_h"] + p["oil_flow_ul_h"]) * s["hours_to_encapsulate"]
    flow = wash_flow_ul_min(p)                          # shear on the capsules <= SHEAR_MAX_DYN_CM2 over the trap
    vol = math.floor(trap_ul * 10) / 10                 # one trap volume per wash: never more than the chamber holds
    step = lambda n, op, target, note, **kw: {  # noqa: E731
        "step": n, "operation": op, "target": target, "reagent": kw.pop("reagent", None), "volume_ul": kw.pop("volume_ul", None),
        "duration_s": kw.pop("duration_s"), "temp_c": kw.pop("temp_c", None), "washes": kw.pop("washes", None),
        "flow_ul_min": kw.pop("flow_ul_min", None), "note": note, **kw}
    wash = lambda n, reagent, washes, note, **kw: step(  # noqa: E731
        n, "exchange", "m2_trap_1", note, reagent=reagent, volume_ul=vol, washes=washes, flow_ul_min=flow,
        duration_s=washes * vol / flow * 60, **kw)
    return {
        "id": "m2_encapsulation",
        "name": "Module 2: encapsulation (reference only)",
        "source": ("SPC-NAR (Leonaviciene & Mazutis, NAR 2023) and CapSeq (Baronas et al., Science 2026) via "
                   "contracts/sources/chip_geometry_requirements.xlsx: 40 um nozzle, 100 / 250 / 700 uL/h, gelation at 4 C "
                   "for 15-30 min, emulsion breaker + rinse x2, 0.1% LAP, 405 nm for 20 s, 100 um aggregate filter. "
                   "Wash volumes and flows are placeholders. GENERATED by cad/generators/m2_encapsulation.py."),
        "placeholder": True,
        "description": ("REFERENCE ONLY: the nozzle of this chip is clamped to 200 um (design 40 um), so droplet generation "
                        "(step 1) is not achievable by resin printing or hot embossing; the pipeline encapsulates off-chip. "
                        "Encapsulate, gel in the cold delay, break the emulsion, rinse, cure, wash, send the capsules on to Module 3. "
                        f"Washes are one trap volume ({vol:g} uL) at {flow:g} uL/min, which keeps wall shear on the capsules under "
                        f"{SHEAR_MAX_DYN_CM2:g} dyn/cm2."),
        "steps": [
            step(1, "encapsulate", "m2_nozzle_1",
                 f"Encapsulate {p['capsule_count']:,} capsules: about {s['generation_rate_hz']:.0f} droplets a second, "
                 f"~{s['mean_cells_per_capsule']:.2f} cells per capsule, so most capsules are empty and almost no occupied "
                 f"capsule holds two cells. (Design intent; the clamped 200 um nozzle will not do this.)",
                 duration_s=t_enc, flows_ul_h={"core": p["core_flow_ul_h"], "shell": p["shell_flow_ul_h"], "oil": p["oil_flow_ul_h"]},
                 capsules=p["capsule_count"]),
            step(2, "hold_temp", "m2_cold_delay_1",
                 f"Cold delay: {p['gelation_temp_c']:g} C for {p['gelation_time_min_low']:g} min in tubing on a cold block, "
                 f"so the GelMA shells set.", temp_c=p["gelation_temp_c"], duration_s=p["gelation_time_min_low"] * 60),
            step(3, "load", "m2_trap_1", f"The emulsion (~{emulsion_ul:.1f} uL plus a chase) comes back onto the chip through "
                 f"m2_trap_in_1; the capsules settle in one layer behind the weir.", volume_ul=vol,
                 flow_ul_min=flow, duration_s=vol / flow * 60),
            wash(4, "emulsion_breaker", 1, "Break the emulsion: the oil drains to the shared waste under the weir, the capsules stay.",
                 waste="m2_under_mesh_1"),
            wash(5, "wash_buffer", 2, "Rinse twice with buffer, still to the shared waste.", waste="m2_under_mesh_1"),
            wash(6, "photoinitiator", 1, "Photoinitiator in (0.1% LAP in the paper)."),
            step(7, "cure", "m2_trap_1", f"Cure: {p['curing_wavelength_nm']:g} nm light for {p['curing_time_s']:g} s through the "
                 f"window over the single capsule layer crosslinks the shells.",
                 duration_s=p["curing_time_s"], wavelength_nm=p["curing_wavelength_nm"]),
            wash(8, "wash_buffer", 3, "Wash three times to clear the photoinitiator."),
            wash(9, "resuspension_buffer", 1, "Back-flush from m2_under_mesh_1 to lift the capsules off the weir.", direction="backflush"),
            step(10, "release", "m2_trap_1", "Capsules out through the aggregate filter to m2_capsule_out_1, on to Module 3.",
                 reagent="resuspension_buffer", volume_ul=vol, flow_ul_min=flow, duration_s=vol / flow * 60),
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

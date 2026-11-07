"""Module 1b, automated cell prep (DEMO SCALE: 30 x 20 mm, one fluidic layer, one depth; SIMULATED, not printed).

    python cad/generators/m1b_cellprep.py          # writes cad/m1b_cellprep_spec.json and protocols/m1b_cellprep.json

The bridge between Module 1 and Module 2: cells harvested from m1_out_1 are quenched, washed,
filtered, counted and resuspended in dextran buffer at the concentration Module 2 needs, then
leave for Module 2's core inlet.

Top view (30 x 20 mm; stage x positions are the design brief's, scaled to the footprint):

                 quench_in        wash_in                           buffer_in
                     |               |                                  |
  cells_in --> [T + herringbone] -> [cell wash] -> [weir filter] -> [QC] -> [resuspend] --> to_module2
                                     |
                                   waste

Fabrication rules come from contracts/blocks.json: every chamber and channel is the envelope
depth (0.3 mm, ONE etch depth), channels are at least the open-channel floor wide (clamped UP),
the folded mixer keeps the minimum wall between its passes, ports are the minimum size at the
minimum pitch (six ports), and every chamber and channel names its CAD block.

MIXING IS ADVECTIVE, NOT A PLAIN FOLD. Both mixers (quench, resuspension) are herringbone_mixer
blocks. A serpentine_channel or a wide chamber only stacks the two streams and then waits for
diffusion, which at Re far below 1 is the whole mixing time; the chevrons drive chaotic
advection instead. They are relief inside the one 0.3 mm layer, like the weir crest, so this is
still one fluidic layer at one etch depth, and the channel cross-section is unchanged, so the
wall-shear-limited flows below are unchanged too.

The cell filter is a DEPTH WEIR (blocks.json "retention"): a crest across the chamber leaves a
gap of tens of microns under the lid, so the 200 um in-plane floor does not apply (it limits
width, not z). Single cells (10-20 um) pass under the crest and clumps stay behind. The gap is
the filter_gap_um parameter, kept below the capsule-retention limit the design checker applies
to any retention (trap.gap_factor_high x capsule diameter) and below Module 2's nozzle. ONE
feature, ONE number: retention.opening_um and filter.gap_um are the same gap by construction,
and the crest it implies is written out as retention.crest_height_um (this chip's etch depth
minus the gap), crest_thickness_um and retains_um, so the weir is buildable from the spec.

retains_um IS DERIVED FROM THE GAP (retains_um()), never written down beside it. The retention band in
contracts/blocks.json holds a particle only while gap_factor_low x D <= gap < gap_factor_high x D;
read for D, that fixes the smallest retained clump to gap/gap_factor_high .. gap/gap_factor_low, and
the middle of that interval is what this chip claims. The file used to declare a 27 um gap next to
retains_um 30, which the band contradicts outright - 27 um is more than twice 0.4 x 30 um, so that
weir passed 30 um clumps straight through while claiming to stop them. Catching the brief's 30 um
clumps needs a 9-12 um gap, and that is not a deliverable weir either: the gap is a Z dimension with
about +/-20 um of process tolerance, so a 12 um target can come out at zero (sealed shut) or negative
(no crest at all). The gap therefore stays where it is buildable and retains_um states the clump the
gap really catches; the 30-40 um clumps the brief wanted are recorded as NOT caught.

Step volumes, flows and durations of protocols/m1b_cellprep.json come from this geometry (see
derive_protocol); wall shear in every targeted chamber stays at or below SHEAR_USE x 0.01 dyn/cm2.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genlib  # noqa: E402
import schema_io  # noqa: E402
from genlib import r  # noqa: E402

NAME = "m1b_cellprep"
OUT = genlib.ROOT / "cad" / "m1b_cellprep_spec.json"
PROTOCOL = genlib.ROOT / "protocols" / "m1b_cellprep.json"

BLOCKS = schema_io.load_json(genlib.ROOT / "contracts" / "blocks.json")
ENV, FLOORS = BLOCKS["envelope"], BLOCKS["fabrication_floors"]
DEPTH_MM = ENV["max_depth_mm"]                  # the ONE etch depth of every chamber and channel
MIN_CHANNEL_MM = FLOORS["min_open_channel_mm"]
WALL_MM = FLOORS["min_wall_mm"]
PORT_UM = FLOORS["min_port_mm"] * 1000
PORT_PITCH_MM = FLOORS["min_port_pitch_mm"]
MAX_PORTS = FLOORS["max_ports_per_chip"]
CREST_UM = BLOCKS["retention"]["weir_geometry"]["crest_thickness_um"]   # in plane: the in-plane floor
MIN_OPEN_UM = FLOORS["min_open_channel_mm"] * 1000
BRIEF_CLUMP_UM = 30                             # the clump the design brief ASKED for (sheet: 30-40 um clumps).
#                                                 NOT what this weir delivers: see retains_um() and WHAT_IT_CATCHES.

BASE_MM, LID_MM = 1.0, 1.0                      # PMMA base and lid around the one fluidic layer
MU_PA_S = 0.8e-3                                # buffer viscosity
SHEAR_LIMIT_DYN_CM2 = 0.01
SHEAR_USE = 0.8                                 # run at this fraction of the limit
BRIEF_X_MM = 75.0                               # the design brief's drawing width
CY = 10.0                                       # the cell line runs along y = CY (set from the footprint below)
STAGES = {                                      # id: (x0, x1) on the 75 mm brief drawing
    "m1b_mix_1": (10.0, 22.0), "m1b_cellwash_1": (25.0, 37.0), "m1b_filter_1": (40.0, 48.0),
    "m1b_qc_window_1": (51.0, 57.0), "m1b_resuspend_1": (60.0, 67.0),
}
HEIGHT_MM = {"m1b_mix_1": 4.0, "m1b_cellwash_1": 6.0, "m1b_filter_1": 3.0, "m1b_qc_window_1": 2.0}
PORT_STANDOFF_MM = {"quench": 3.0, "wash": 3.5, "waste": 3.5, "buffer": 4.25}   # port centre beyond the chamber edge
# Hold times that are not set by flow. A step takes the longer of this and the time to push its volume.
NOMINAL_S = {1: 180, 2: 180, 3: 300, 4: 120, 5: 30, 6: 120, 7: 60}


def shear_dyn_cm2(flow_ul_min: float, width_mm: float, depth_mm: float) -> float:
    """tau = 6 mu Q / (w h^2) of a wide slot, in dyn/cm2 (1 Pa = 10 dyn/cm2)."""
    q = flow_ul_min * 1e-9 / 60
    return 10 * 6 * MU_PA_S * q / (width_mm * 1e-3 * (depth_mm * 1e-3) ** 2)


def flow_limit_ul_min(chamber: dict) -> float:
    """Flow at which the chamber's wall shear reaches SHEAR_USE x the limit (serpentines: their own channel)."""
    widths = [chamber["size"][1]] + ([chamber["mixer"]["channel_width_mm"]] if chamber.get("mixer") else [])
    h = chamber["size"][2]
    q = SHEAR_USE * SHEAR_LIMIT_DYN_CM2 / 10 * min(widths) * 1e-3 * (h * 1e-3) ** 2 / (6 * MU_PA_S)
    return math.floor(q * 1e9 * 60 * 1000) / 1000


def gap_factors() -> tuple[float, float]:
    """(low, high) from contracts/parameters.json: the retention band every gap on the chip obeys."""
    params = schema_io.load_json(genlib.ROOT / "contracts" / "parameters.json")["parameters"]
    return params["trap.gap_factor_low"]["value"], params["trap.gap_factor_high"]["value"]


def weir_gap_um(p: dict) -> float:
    """Filter gap under the weir crest: the parameter, kept strictly below the retention limit the checker applies.

    This is the ONLY gap on the feature. retention.opening_um and filter.gap_um are both set from
    it, so the chamber cannot carry two different numbers for one crest.
    """
    params = schema_io.load_json(genlib.ROOT / "contracts" / "parameters.json")["parameters"]
    limit = params["trap.gap_factor_high"]["value"] * params["capsule.diameter_um"]["value"]
    return min(p["filter_gap_um"], limit - 1)


def retains_um(gap: float) -> float:
    """What a gap this size ACTUALLY holds back, DERIVED from the gap - never asserted beside it.

    contracts/blocks.json retention.weir_geometry.rule: a gap retains a particle of diameter D
    only while gap_factor_low x D <= gap < gap_factor_high x D. Read that for D instead of for
    the gap and it says a gap of g holds back everything from g / gap_factor_high up to
    g / gap_factor_low, and NOTHING below g / gap_factor_high. So the retained diameter is not a
    free number: once the gap is fixed, D is fixed to that interval. Take its middle and round to
    5 um, which leaves the most room on both sides for the +/-20 um z tolerance on the gap, then
    refuse any rounding that would leave the interval.

    This is the fix for the contradiction this file used to ship: it declared a 27 um gap and
    retains_um 30, and 27 is not below gap_factor_high x 30 = 12 um, so the stated pair said the
    weir passes exactly what it claims to catch.
    """
    low, high = gap_factors()
    d = 5 * round(gap / 2 * (1 / low + 1 / high) / 5)
    if not (low * d <= gap < high * d):
        d = 5 * math.ceil(gap / high / 5)
    return float(d)


def build(p: dict) -> dict:
    fx, fy = min(p["footprint_x_mm"], ENV["max_x_mm"]), min(p["footprint_y_mm"], ENV["max_y_mm"])
    cy = fy / 2
    w = max(p["channel_width_mm"], MIN_CHANNEL_MM)
    mw = max(p["mixer_width_mm"], MIN_CHANNEL_MM)
    rw = p["resuspend_width_mm"]
    gap = weir_gap_um(p)
    crest_um = DEPTH_MM * 1000 - gap
    top = BASE_MM + DEPTH_MM + LID_MM
    cz = r(BASE_MM + DEPTH_MM / 2)
    margin = WALL_MM
    s = (fx - 2 * margin) / BRIEF_X_MM

    def px(v):
        return margin + v * s

    box = {cid: (px(x0), px(x1)) for cid, (x0, x1) in STAGES.items()}
    heights = {**HEIGHT_MM, "m1b_resuspend_1": rw}
    cx = {cid: (a + b) / 2 for cid, (a, b) in box.items()}

    def stage(cid, typ, block, volume, note, **extra):
        a, b = box[cid]
        out = {"id": cid, "type": typ, "center": [r((a + b) / 2, 3), r(cy, 3), cz],
               "size": [r(b - a, 3), r(heights[cid], 3), DEPTH_MM], "volume_ul": r(volume, 3), "block": block}
        out.update(extra)
        out["note"] = note
        return out

    def port(pid, typ, x, y, role, note=None):
        out = {"id": pid, "type": typ, "position": [r(x, 3), r(y, 3), r(top)], "role": role, "width_um": PORT_UM}
        if note:
            out["note"] = note
        return out

    def chan(cid, a, b, carries, path):
        return {"id": cid, "from": a, "to": b, "width_mm": w, "depth_mm": DEPTH_MM, "carries": carries,
                "path": [[r(x, 3), r(y, 3)] for x, y in path], "block": "straight_channel"}

    def area(cid):
        a, b = box[cid]
        return (b - a) * heights[cid]

    # serpentine: as many passes as the box height holds at the wall floor, never more than the turns asked for
    mx0, mx1 = box["m1b_mix_1"]
    passes = min(p["mixer_turns"] + 1, int((heights["m1b_mix_1"] - mw) / (mw + WALL_MM)) + 1)
    serp_len = passes * (mx1 - mx0) + (passes - 1) * (mw + WALL_MM)
    fa, fb = box["m1b_filter_1"]
    # What the weir is FOR: it holds cell clumps, not capsules, and never the single cells that
    # pass under the crest. The number is DERIVED from the gap we can actually build (retains_um),
    # so the feature cannot claim a retention its gap does not give.
    retains = retains_um(gap)
    brief_gap_lo, brief_gap_hi = (f * BRIEF_CLUMP_UM for f in gap_factors())
    crest_mm = CREST_UM / 1000
    filter_vol = (fb - fa - crest_mm) * heights["m1b_filter_1"] * DEPTH_MM + crest_mm * heights["m1b_filter_1"] * gap / 1000

    chambers = [
        stage("m1b_mix_1", "mixer", "herringbone_mixer", serp_len * mw * DEPTH_MM,
              "Quench: the quench medium joins at a T-junction, then a HERRINGBONE MIXER mixes it in. Mixing has to be "
              "ADVECTIVE: at Re far below 1 a plain serpentine fold only stacks the two streams side by side and leaves "
              "diffusion to do the work, so the chevrons (relief inside the one 0.3 mm layer, like the weir crest) drive "
              f"chaotic advection instead. The folded path gives the length: {passes - 1} turns, {serp_len:.1f} mm of "
              f"{mw:g} mm channel with {WALL_MM:g} mm walls. The cross-section is unchanged, so the shear-limited flow is "
              "the same as for a plain fold.",
              mixer={"kind": "serpentine", "channel_width_mm": mw, "turns": passes - 1, "length_mm": r(serp_len, 2)}),
        stage("m1b_cellwash_1", "cell_wash", "spiral_channel", area("m1b_cellwash_1") * DEPTH_MM,
              "Cell wash: a SPIRAL CHANNEL used as a LONG PATH (blocks.json spiral_channel: 'inertial focusing or long "
              "path'), with clean buffer co-flowed from m1b_wash_in_1 and the old liquid taken off at m1b_waste_1. The "
              "method is named, not TBC, and the two methods it is not are ruled out on numbers: NOT a dld_array, "
              f"because at the {MIN_OPEN_UM:g} um in-plane floor the DLD critical diameter is about 105 um, five times a "
              "10-20 um cell; and NOT inertial focusing in this spiral, because the Reynolds number at the cell-safe "
              "shear-limited flow below is orders of magnitude too low. Exchange here is co-flow plus diffusion across "
              "the lane, so it is slow and incomplete: that is the honest limit of this stage."),
        stage("m1b_filter_1", "filter", "trap_chamber", filter_vol,
              f"Clump filter: a DEPTH WEIR, a {crest_um:g} um tall x {CREST_UM:g} um thick crest across the chamber "
              f"leaves a {gap:g} um gap under the lid (z only: the {MIN_OPEN_UM:g} um floor limits width, not height). "
              f"Single cells of 10-20 um pass under it and clumps from about {retains:g} um stay behind. THAT "
              f"{retains:g} um IS DERIVED FROM THE {gap:g} um GAP, NOT CHOSEN: the retention rule in "
              "contracts/blocks.json holds a particle only while the gap is at or above gap_factor_low x its diameter "
              f"and strictly below gap_factor_high x it, which for a {gap:g} um gap puts the smallest retained clump in "
              f"{gap / gap_factors()[1]:.0f}-{gap / gap_factors()[0]:.0f} um; {retains:g} um is the middle of that, so "
              f"the +/-20 um z tolerance on the gap has the most room on either side. WHAT IT DOES NOT CATCH: the "
              f"design brief wanted {BRIEF_CLUMP_UM:g}-40 um clumps stopped, and this weir does not stop them. A "
              f"{BRIEF_CLUMP_UM:g} um clump needs a {brief_gap_lo:.0f}-{brief_gap_hi:.0f} um gap, which is "
              f"undeliverable here: the gap is a Z dimension at +/-20 um, so a {brief_gap_hi:.0f} um target could come "
              "out at zero or negative and seal the chamber shut. The gap stays at the buildable end and this field "
              "says what that buys instead of what was asked for. ONE gap, ONE number: retention.opening_um and "
              "filter.gap_um are the same value, and the crest is carried as data (retention.crest_height_um = this "
              "chip's etch depth minus the gap, crest_thickness_um, retains_um), so it can be machined from the spec. "
              "Untested: z tolerance is a large fraction of a gap this small, so run a coupon first.",
              filter={"type": "mesh", "gap_um": gap, "purpose": "cells"},
              retention={"type": "weir", "opening_um": gap, "source": "fabricated",
                         "crest_height_um": r(crest_um, 3), "crest_thickness_um": CREST_UM, "retains_um": retains}),
        stage("m1b_qc_window_1", "qc_window", "polygon_chamber", area("m1b_qc_window_1") * DEPTH_MM,
              "Optical window: a counter reads cell concentration and viability (simulated: operator/cellprep.py)."),
        stage("m1b_resuspend_1", "mixer", "herringbone_mixer", area("m1b_resuspend_1") * DEPTH_MM,
              f"Wide ({rw:g} mm), short HERRINGBONE MIXER: resuspends the cells in dextran buffer at the dilution the "
              "operator sets. Wide and short keeps the pressure drop low in a viscous buffer, but a plain wide chamber "
              "does not mix at Re far below 1 - it just carries the two liquids side by side - so the floor carries "
              "chevron relief (same layer, same etch depth) for chaotic advection. The cross-section is unchanged, so "
              "the shear-limited flow is still set by the width.",
              mixer={"kind": "wide_short", "channel_width_mm": rw}),
    ]
    x_in, x_out = px(4.0), px(71.0)
    top_of = lambda cid: cy + heights[cid] / 2  # noqa: E731
    bot_of = lambda cid: cy - heights[cid] / 2  # noqa: E731
    qx, wx, bx = cx["m1b_mix_1"], cx["m1b_cellwash_1"], cx["m1b_resuspend_1"]
    qy = top_of("m1b_mix_1") + PORT_STANDOFF_MM["quench"]
    wy = top_of("m1b_cellwash_1") + PORT_STANDOFF_MM["wash"]
    ry = bot_of("m1b_cellwash_1") - PORT_STANDOFF_MM["waste"]
    by = top_of("m1b_resuspend_1") + PORT_STANDOFF_MM["buffer"]
    ports = [
        port("m1b_cells_in_1", "inlet", x_in, cy, "cells", "From m1_out_1 (Module 1's one merged outlet)."),
        port("m1b_quench_in_1", "inlet", qx, qy, "reagent", "Quench medium (neutralises the detach reagent)."),
        port("m1b_wash_in_1", "inlet", wx, wy, "reagent", "Cell wash buffer."),
        port("m1b_waste_1", "outlet", wx, ry, "waste"),
        port("m1b_buffer_in_1", "inlet", bx, by, "reagent", "Dextran buffer (encapsulation buffer)."),
        port("m1b_to_module2_1", "outlet", x_out, cy, "transfer", "To Module 2's core inlet (m2_core_in_1)."),
    ]
    chain = ["m1b_mix_1", "m1b_cellwash_1", "m1b_filter_1", "m1b_qc_window_1", "m1b_resuspend_1"]
    names = ["cellwash", "filter", "qc", "resuspend"]
    channels = [chan("m1b_ch_in_1", "m1b_cells_in_1", "m1b_mix_1", "cells", [(x_in, cy), (box["m1b_mix_1"][0], cy)])]
    for (a, b), nm in zip(zip(chain[:-1], chain[1:]), names):
        channels.append(chan(f"m1b_ch_to_{nm}", a, b, "cells", [(box[a][1], cy), (box[b][0], cy)]))
    channels += [
        chan("m1b_ch_out", "m1b_resuspend_1", "m1b_to_module2_1", "cells", [(box["m1b_resuspend_1"][1], cy), (x_out, cy)]),
        chan("m1b_ch_quench", "m1b_quench_in_1", "m1b_mix_1", "liquid", [(qx, qy), (qx, top_of("m1b_mix_1"))]),
        chan("m1b_ch_wash_in", "m1b_wash_in_1", "m1b_cellwash_1", "liquid", [(wx, wy), (wx, top_of("m1b_cellwash_1"))]),
        chan("m1b_ch_waste", "m1b_cellwash_1", "m1b_waste_1", "liquid", [(wx, bot_of("m1b_cellwash_1")), (wx, ry)]),
        chan("m1b_ch_buffer", "m1b_buffer_in_1", "m1b_resuspend_1", "liquid", [(bx, by), (bx, top_of("m1b_resuspend_1"))]),
    ]
    if len(ports) > MAX_PORTS:
        raise genlib.ParamError(f"{len(ports)} ports > {MAX_PORTS}")
    for i, a in enumerate(ports):
        for b in ports[i + 1:]:
            if math.dist(a["position"][:2], b["position"][:2]) < PORT_PITCH_MM:
                raise genlib.ParamError(f"ports {a['id']} and {b['id']} are closer than {PORT_PITCH_MM:g} mm")
    for pt in ports:
        if not (PORT_UM / 2000 + WALL_MM <= pt["position"][0] <= fx - PORT_UM / 2000 - WALL_MM
                and PORT_UM / 2000 + WALL_MM <= pt["position"][1] <= fy - PORT_UM / 2000 - WALL_MM):
            raise genlib.ParamError(f"port {pt['id']} is too close to the edge of the {fx:g} x {fy:g} mm footprint")
    return {
        "id": "m1b_cellprep_v1",
        "name": "Module 1b: cell prep (demo scale, simulated)",
        "description": (f"DEMO SCALE: {fx:g} x {fy:g} mm, one fluidic layer at ONE depth ({DEPTH_MM:g} mm: every chamber "
                        f"and channel), resin print + hot emboss. Channels are {w:g} mm wide (clamped up to the "
                        f"{MIN_CHANNEL_MM:g} mm open-channel floor), the quench mixer is a folded {mw:g} mm channel with "
                        f"{WALL_MM:g} mm walls, ports are {PORT_UM:g} um at >= {PORT_PITCH_MM:g} mm pitch "
                        f"({len(ports)} of {MAX_PORTS}), and every chamber and channel names its CAD block "
                        "(contracts/blocks.json). RETENTION: the cell filter is a depth weir (block trap_chamber): a "
                        f"{crest_um:g} um tall x {CREST_UM:g} um thick crest leaves a {gap:g} um gap under the lid, "
                        f"printable because the {MIN_OPEN_UM:g} um floor limits width, not z; it strains clumps from about "
                        f"{retains:g} um and single cells pass. THAT RETENTION IS DERIVED FROM THE GAP: a {gap:g} um gap "
                        f"holds a clump of {gap / gap_factors()[1]:.0f}-{gap / gap_factors()[0]:.0f} um and upwards by the "
                        f"band in contracts/blocks.json, so retains_um is {retains:g}, the middle of it. The brief's "
                        f"{BRIEF_CLUMP_UM:g}-40 um clumps are NOT caught and cannot be: they would need a "
                        f"{brief_gap_lo:.0f}-{brief_gap_hi:.0f} um gap, and at the +/-20 um z tolerance of a weir that "
                        "target could come out negative. All of that is DATA on m1b_filter_1.retention "
                        "(crest_height_um, crest_thickness_um, opening_um, retains_um), and filter.gap_um is the same gap, "
                        "not a second one. There is no mesh. MIXING IS ADVECTIVE: both mixers are herringbone_mixer "
                        "blocks (chevron relief in the same layer), because a plain fold or a wide chamber does not mix at "
                        "Re far below 1. The cell wash is a spiral_channel used as a long co-flow path, which is slow and "
                        f"incomplete but is a named method: a DLD array cannot sort cells at the {MIN_OPEN_UM:g} um floor "
                        "(critical diameter about 105 um) and inertial focusing needs flows far above the cell-safe shear "
                        "limit. Automated bridge from Module 1 to Module 2: quench (T-junction + herringbone), wash, "
                        "filter, count at a QC window, resuspend in dextran buffer, send to Module 2's core inlet. "
                        "Designed and simulated; not printed this weekend. GENERATED by cad/generators/m1b_cellprep.py."),
        "placeholder": True,
        "module": {"id": "m1b", "name": "Cell prep (draft)", "status": "superseded", "printed": False,
                   "superseded_by": "chip/module1_v1.json",
                   "note": "Superseded: Module 1 v1 delivers one counted suspension for encapsulation itself; kept for reference, out of the pipeline."},
        "generator": {"script": "cad/generators/m1b_cellprep.py",
                      "params_schema": "contracts/schemas/generators/m1b_cellprep.schema.json", "params": p},
        "units": "mm",
        "footprint": {"x": fx, "y": fy},
        "layers": [
            {"id": "m1b_base", "material": "pmma", "thickness_mm": BASE_MM},
            {"id": "m1b_fluidic", "material": "pmma", "thickness_mm": DEPTH_MM},
            {"id": "m1b_lid", "material": "pmma", "thickness_mm": LID_MM},
        ],
        "cell_filter": {"gap_um": gap},
        "chambers": chambers,
        "channels": channels,
        "ports": ports,
    }


def targets_of(step: dict) -> list[str]:
    return ([step["target"]] if step.get("target") else []) + list(step.get("targets") or [])


def derive_protocol(protocol: dict, spec: dict) -> dict:
    """Volumes, flows and durations of every step from the chip geometry. Cells are loaded and quenched in equal
    halves of the serpentine volume; the wash and resuspension use one chamber volume; the flow is the
    wall-shear-limited flow of the target; time is the longer of the nominal hold and the time to push the volume."""
    chambers = {c["id"]: c for c in spec["chambers"]}
    ports = {pt["id"] for pt in spec["ports"]}
    gap = spec["cell_filter"]["gap_um"]
    for step in protocol["steps"]:
        if step["step"] == 1:
            step["target"] = "m1b_mix_1"
            step["note"] = ("Cells arrive at m1b_cells_in_1 from Module 1's one outlet m1_out_1, already merged "
                            "(labelled per chamber in Module 1, so they share one line), and fill half of the quench "
                            "serpentine m1b_mix_1; m1b_waste_1 is vented.")
        if step["step"] == 4:
            step["note"] = (f"Through the depth-weir filter (a {gap:g} um gap under the crest) to the QC window: clumps "
                            "stay behind, single cells go on. m1b_waste_1 is capped and m1b_buffer_in_1 is vented as "
                            "the drain; m1b_to_module2_1 stays capped.")
        every = targets_of(step) + ([step["source"]] if step.get("source") else [])
        missing = [t for t in every if t not in chambers and t not in ports]
        if missing:
            raise genlib.ParamError(f"step {step['step']} targets {', '.join(missing)}, not on the chip")
        targets = [t for t in targets_of(step) if t in chambers]
        if not targets:
            continue
        vol = min(chambers[t]["volume_ul"] for t in targets)
        if step["step"] in (1, 2):
            vol = r(vol / 2, 3)
        if step.get("volume_ul") is not None:
            step["volume_ul"] = vol
        if step["step"] == 1:
            step["flow_ul_min"] = flow_limit_ul_min(chambers["m1b_mix_1"])
        if step.get("flow_ul_min") is not None:
            q = min(flow_limit_ul_min(chambers[t]) for t in targets)
            step["flow_ul_min"] = q
            push_s = math.ceil((step.get("washes") or 1) * vol / q * 60)
            step["duration_s"] = max(NOMINAL_S[step["step"]], push_s)
    return protocol


def main(argv: list[str] | None = None) -> int:
    try:
        params, args = genlib.parse_params(NAME, argv, "Module 1b cell prep -> cad/m1b_cellprep_spec.json")
        spec = build(params)
        out = args.out or OUT
        genlib.write_spec(spec, out)
        if not args.out:
            schema_io.write_json(PROTOCOL, derive_protocol(schema_io.load_json(PROTOCOL), spec))
    except genlib.ParamError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"wrote {genlib.rel(out)}: {len(spec['chambers'])} stages, {len(spec['channels'])} channels, {len(spec['ports'])} ports")
    return genlib.finish(out, summary(spec), args)


def summary(spec: dict) -> list[str]:
    import safety
    import schema_io

    m2 = schema_io.load_json(genlib.ROOT / "cad" / "m2_encapsulation_spec.json")
    t = safety.cell_prep_target(m2)
    return [
        "Module 1b summary (cells, not capsules):",
        "  stages                      " + " -> ".join(c["id"].removeprefix("m1b_") for c in spec["chambers"]),
        f"  cell filter                 {spec['cell_filter']['gap_um']:g} um (Module 2 nozzle: "
        f"{m2['encapsulation']['nozzle_um']:g} um)",
        f"  Module 2 needs              ~{t['target_cells_per_ul']:,.0f} cells/uL for {t['target_mean']:g} cells per capsule "
        f"({t['capsules_per_hour']:,.0f} capsules/h from {t['core_flow_ul_h']:g} uL/h of core stream); accepted "
        f"{t['mean_low']:.3f}-{t['mean_high']:.3f}; viability >= {t['viability_min']:.0%}",
        "  runs                        python operator/run.py --scenario scenarios/m1b_cellprep_demo.json (simulated counter)",
    ]


if __name__ == "__main__":
    raise SystemExit(main())

"""Build chip/module1_target_v1.json and protocols/module1_target_v1.json.

    python protocols/build_module1_target.py            # write both files
    python protocols/build_module1_target.py --report   # the physics tables

MODULE 1 AT TARGET SCALE. The shipped demo (chip/module1_v1.json, kept alongside this and
untouched) cannot host its own experiment, and the cause is scale, not architecture: its
three chambers are 0.105 cm2 only because the board is at the 30 x 20 mm DEMO envelope. The
biology audit found four fatal consequences, and all four are area or perfusion problems:

  1 MEDIUM     1.6 uL every 48 h over 0.105 cm2 is ~7.6 uL/cm2/day against a standard
               300-600. Here: continuous perfusion, 480 uL/cm2/day (see PERFUSION below).
  2 OXYGEN     every demo hold is "all ports capped, 24 h", and a sealed chamber holds
               about 3.5 h of oxygen for its cells. Here: NO capped hold exists. Even
               attachment is perfused, at a flow 780x below the wash-out velocity, and the
               loop runs through a VENTED GAS-EXCHANGE RESERVOIR (off-chip, one per lane,
               declared in the chip spec's stages[]).
  3 LANE AREA  0.105 cm2 gives 0.5-1 colony where the route needs 16-40. Here: 13.2, 13.2
               and 9.2 cm2, as six replicated fields per lane.
  4 WASH-OUT   the demo vents its non-adherent hemogenic progenitors past a 25 um weir.
               Here there is NO retention feature at all: a wide slow field retains a 12 um
               cell by SEDIMENTATION, with an 88x margin (see RETENTION below). That is the
               strongest result in this file and it deletes a part rather than adding one.

Everything that is a real process floor still binds and is read from contracts/blocks.json:
one fluidic layer, one 0.3 mm etch depth, 0.2 mm min open channel, 0.4 mm min wall, 0.8 mm
min port, 5 mm min port pitch, geometry only from the block library, no channel crossings in
plan view. What LIFTS at this scale is the 6-port limit, which blocks.json says twice is a
demo-envelope limit and not a process floor; this chip uses 17 ports and every one is
justified in protocols/module1_target_design.md.

VISCOSITY. Every flow and shear number here uses fluid.viscosity_37c_pa_s (0.0008 Pa s:
medium at 37 C), NOT fluid.viscosity_pa_s (0.001, water at 20 C, the dye-demo value).
Nothing is hard-coded; both are read from contracts/parameters.json.

THE FOUR NUMBERS THAT DECIDE THE DESIGN, all derived in report():
  PERFUSION  13.23 cm2 x 480 uL/cm2/day = 6.35 mL/day = 4.41 uL/min per lane, which is 16
             chamber volumes a day (the 10-20/day the 300-600 band implies).
  SHEAR      tau = 6 mu Q / (w h^2). Because every lane keeps the SAME field length along
             the flow (a blocks.json design rule) and the same perfusion per unit area,
             tau = 6 mu q L / h^2 is INDEPENDENT OF LANE WIDTH: all three lanes sit at
             4.2e-4 dyn/cm2, 24x under the 0.01 adherent ceiling. Continuous perfusion and
             the shear ceiling are compatible at target scale. Oxygen, not shear, is what
             sets the flow.
  RETENTION  a 12 um cell settles at v_s = dp g d^2 / (18 mu) = 4.9 um/s, so it leaves a
             field only if U > L v_s / h = 0.229 mm/s. The design point is U = 0.0026 mm/s:
             88x below it. No mesh, no weir, no sub-floor gap.
  OXYGEN     9 amol/cell/s x 86400 = 0.78 pmol/cell/day against 0.19 nmol/uL in air-
             saturated medium: at 50% extraction the perfusion supports ~59,000 cells/cm2.
             That is the cap the lane densities are set from.

Sources (DOIs only, no compositions, concentrations, vendors or cell lines: CONFIDENTIALITY.md):
  neural lane      Shi, Kirwan, Livesey, Nat Protoc 2012;7:1836, doi:10.1038/nprot.2012.116
  microglia lane   McQuade et al., Mol Neurodegener 2018;13:67, doi:10.1186/s13024-018-0297-x
                   Washer et al., Sci Rep 2022, doi:10.1038/s41598-022-23477-2
                   Lanfer et al., IJMS 2022, doi:10.3390/ijms23094526
  astrocyte lane   Schuurmans et al., Glia 2026, doi:10.1002/glia.70202
  O2 consumption   Wagner et al., Free Radic Biol Med 2011, doi:10.1016/j.freeradbiomed.2011.05.024
  activating shear Park et al., Biophys J 2023, doi:10.1016/j.bpj.2023.03.037
  one extract,     SIMPLEX, doi:10.1074/mcp.M115.053702; MPLEx, doi:10.1128/mSystems.00043-16
  two omics        Sapcariu et al., doi:10.1016/j.mex.2014.07.002
  per-lane label   McGinnis et al., Nat Methods 2019;16:619, doi:10.1038/s41592-019-0433-8
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHIP_OUT = ROOT / "chip" / "module1_target_v1.json"
PROTO_OUT = ROOT / "protocols" / "module1_target_v1.json"
CONTRACT = ROOT / "contracts" / "parameters.json"
BLOCKS = ROOT / "contracts" / "blocks.json"

DAY = 86400.0


def parameter(name: str) -> float:
    """One value from contracts/parameters.json. No fallback: a literal copied here is how
    the viscosity drifted 20% last time."""
    return float(json.loads(CONTRACT.read_text())["parameters"][name]["value"])


MU = parameter("fluid.viscosity_37c_pa_s")        # medium at 37 C, NOT water at 20 C
MU_DYE = parameter("fluid.viscosity_pa_s")        # quoted in the report only, for contrast
SHEAR_CEILING = parameter("shear.culture_ceiling_dyn_cm2")
RINSE_CEILING = parameter("shear.prelysis_rinse_ceiling_dyn_cm2")
CULTURE_TEMP = parameter("culture.temp_c")
CELL_CONC = parameter("encapsulation.cell_conc_per_ul")

# --- biology inputs (every one cited in the module docstring) -------------------------
CELL_UM = 12.0            # a dissociated cell / hemogenic progenitor, mid of the 10-15 um range
DRHO = 50.0               # kg/m3, cell vs medium. The CONSERVATIVE end (1.05 g/mL): a denser
                          # cell settles faster, so this understates retention on purpose.
G = 9.81
O2_AMOL_CELL_S = 9.0      # amol/cell/s, the TOP of the published 6-9 macrophage-lineage band
O2_NMOL_PER_UL = 0.19     # air-saturated medium at 37 C (~0.2 mM)
O2_EXTRACTION = 0.5       # medium may leave the lane no more than half depleted
D_SMALL = parameter("dye.diffusivity_m2_s")   # small molecule in water; the FAST case
MEDIUM_PER_CM2_DAY = 480.0                    # uL/cm2/day, mid of the standard 300-600
COLONIES_PER_CM2_PUBLISHED = (5.0, 10.0)      # the hemogenic route's plating density
COLONY_YIELD_PER_CM2 = 225_000.0              # progenitors/cm2 at that density (4 cm2 -> ~900k)
MATURATION_DENSITY = (9_000.0, 90_000.0)      # sparse-to-dense published maturation window

# --- geometry (mm). Every number here is checked by tools/check_chip_spec.py, ----------
# --- cad/make_figures.py and tests/test_design_rules.py -------------------------------
FOOT_X, FOOT_Y = 127.76, 85.48     # ANSI/SLAS 1-2004, from blocks.json target_envelope
DEPTH = 0.3                        # the ONE etch depth: chambers and channels alike
FIELD_L = 14.0                     # field length ALONG the flow, the same in every lane
FIELD_W = {"n": 15.75, "g": 15.75, "a": 11.0}
FIELD_X0 = 9.0
FIELD_PITCH = 18.15
N_FIELDS = 6
QUENCH_FIELDS = 2                  # f1-f2: quenched in place for metabolome + proteome
SPINE_H = 1.5                      # spine y-extent
RISER = 3.0                        # spine edge to field edge
BAND_H = 2 * SPINE_H + 2 * RISER + FIELD_L
CORRIDOR = 4.0
PORT_DY = 1.3        # corridor port centre above the band: leaves 0.9 mm to the spine tees
LINK_DY = 2.9        # corridor link centre above the band: 0.7 mm to the ports, 0.6 to the next band
Y0 = 3.0                           # bottom margin
LEFT_PORT_X = 4.0
RIGHT_PORT_X = 118.0
SUP_X, RET_X = 60.0, 100.0
JL_X = 20.0
BUS_X, BUS_W = 121.0, 1.5
BUS_PORT_X = 124.5
FEED_RISER_W = 0.2                 # the restrictor: at the min_open_channel floor
FEED_RISER_FOLDED = 30.0           # folded length (serpentine_channel) in the riser band
DRAIN_RISER_W = 0.8
SPINE_LINK_W = 1.0
PORT_BORE_UM = 800.0               # fabrication_floors.min_port_mm

SOURCE = (
    "Neural lane: Shi Y, Kirwan P, Livesey FJ. Nature Protocols 2012;7:1836, "
    "doi:10.1038/nprot.2012.116 (dual-SMAD cortical induction, 80 days). "
    "Microglia lane: McQuade A et al. Molecular Neurodegeneration 2018;13:67, "
    "doi:10.1186/s13024-018-0297-x (38-40 days, monolayer, sort-free), with adherent "
    "maturation after Washer S et al. Scientific Reports 2022, doi:10.1038/s41598-022-23477-2 "
    "and Lanfer J et al. IJMS 2022, doi:10.3390/ijms23094526. "
    "Astrocyte lane: Schuurmans IME et al. Glia 2026, doi:10.1002/glia.70202 (35 days). "
    "Oxygen consumption: Wagner BA et al. Free Radical Biology and Medicine 2011, "
    "doi:10.1016/j.freeradbiomed.2011.05.024 (6-9 amol/cell/s). "
    "Activating shear for this lineage: Park J et al. Biophysical Journal 2023, "
    "doi:10.1016/j.bpj.2023.03.037 (0.017 dyn/cm2 over 6 h). "
    "One extraction, two omics: SIMPLEX, doi:10.1074/mcp.M115.053702; MPLEx, "
    "doi:10.1128/mSystems.00043-16; Sapcariu SC et al., doi:10.1016/j.mex.2014.07.002. "
    "Per-lane label before pooling: McGinnis CS et al. Nature Methods 2019;16:619, "
    "doi:10.1038/s41592-019-0433-8."
)

LANES = {
    "n": dict(name="neural", band=0, seed=0, switch=12,
              media=("neural_induction_medium", "neural_maturation_medium")),
    "g": dict(name="glia", band=1, seed=40, switch=52,
              media=("hematopoietic_medium", "microglia_maturation_medium")),
    "a": dict(name="astrocyte", band=2, seed=45, switch=None,
              media=("astro_medium", "astro_medium")),
}
ORDER = ["n", "g", "a"]
MATURE_DAY = 80
INTERACT = [("n", 81), ("g", 83), ("a", 85)]
ENDPOINT_DAY = 87
PERFUSION_BLOCK_DAYS = 2.0
ATTACH_FLOW_PER_LANE = 0.5         # uL/min, continuous: attachment is NOT a capped hold
SEED_MINUTES = 10.0
SEED_MARGIN = 1.5                  # seed at 1.5x the sedimentation criterion, not at it
INTERACT_HOURS = 6.0
SAMPLE_UL = 100.0                  # one supernatant draw


# --- derived geometry -----------------------------------------------------------------

def band_y0(i: int) -> float:
    return Y0 + i * (BAND_H + CORRIDOR)


def field_centre_x(i: int) -> float:
    return FIELD_X0 + FIELD_W["n"] / 2 + i * FIELD_PITCH


GROUPS = {1: list(range(QUENCH_FIELDS)), 2: list(range(QUENCH_FIELDS, N_FIELDS))}
SPINE_X = {
    1: (FIELD_X0, field_centre_x(QUENCH_FIELDS - 1) + FIELD_W["n"] / 2),
    2: (field_centre_x(QUENCH_FIELDS) - FIELD_W["n"] / 2, field_centre_x(N_FIELDS - 1) + FIELD_W["n"] / 2),
}


def spine_box(group: int) -> tuple[float, float]:
    x0, x1 = SPINE_X[group]
    return (x0 + x1) / 2, x1 - x0


def area_cm2(lane: str) -> float:
    return N_FIELDS * FIELD_W[lane] * FIELD_L / 100.0


def group_area_cm2(lane: str, group: int) -> float:
    return len(GROUPS[group]) * FIELD_W[lane] * FIELD_L / 100.0


def field_volume_ul(lane: str) -> float:
    return FIELD_W[lane] * FIELD_L * DEPTH


def bus_id(lane: str, group: int) -> str:
    return f"bus_{lane}{group}"


def feed_tee(lane: str, group: int) -> str:
    """The tee a group's feed port lands on: group 1 is fed from the left end of its ladder,
    group 2 from the right end, so a perfusion step addresses the group by naming it."""
    i = GROUPS[group][0] if group == 1 else GROUPS[group][-1]
    return f"{lane}_dn{i + 1}"


def bus_y(lane: str, group: int) -> float:
    """The y a lane's spoke meets the bus: the collection spine's own y for group 2, and the
    corridor link's y for group 1. Each spoke therefore runs horizontally into its own node."""
    yb = band_y0(LANES[lane]["band"])
    return yb + BAND_H - SPINE_H / 2 if group == 2 else yb + BAND_H + LINK_DY


# --- physics --------------------------------------------------------------------------

def settling_um_s(diameter_um: float = CELL_UM) -> float:
    """Stokes settling velocity, um/s. Lower viscosity at 37 C makes this FASTER than the
    20 C figure, so retention improves; the conservative choice here is the density step."""
    d = diameter_um * 1e-6
    return DRHO * G * d * d / (18.0 * MU) * 1e6


def washout_mm_s(length_mm: float = FIELD_L, depth_mm: float = DEPTH) -> float:
    """Mean velocity above which a cell crosses the field before it can settle: U > L v_s / h."""
    return length_mm * (settling_um_s() * 1e-3) / depth_mm


def mean_velocity_mm_s(flow_ul_min: float, width_mm: float, depth_mm: float = DEPTH) -> float:
    return (flow_ul_min / 60.0) / (width_mm * depth_mm)


def shear_dyn_cm2(flow_ul_min: float, width_mm: float, depth_mm: float = DEPTH, mu: float = MU) -> float:
    """tau = 6 mu Q / (w h^2) for a wide shallow chamber, x10 for dyn/cm2."""
    q = flow_ul_min * 1e-9 / 60.0
    return 10.0 * 6.0 * mu * q / ((width_mm * 1e-3) * (depth_mm * 1e-3) ** 2)


def flow_at_ceiling(width_mm: float, ceiling: float = SHEAR_CEILING) -> float:
    return ceiling / 10.0 * (width_mm * 1e-3) * (DEPTH * 1e-3) ** 2 / (6.0 * MU) * 60.0 * 1e9


def lane_flow_ul_min(lane: str) -> float:
    return area_cm2(lane) * MEDIUM_PER_CM2_DAY / 1440.0


def group_flow_ul_min(lane: str, group: int) -> float:
    return group_area_cm2(lane, group) * MEDIUM_PER_CM2_DAY / 1440.0


def o2_supported_cells_per_cm2() -> float:
    """Cells/cm2 the perfusion feeds, from the medium's O2 content and the per-cell demand."""
    per_cell_day = O2_AMOL_CELL_S * 86400 * 1e-6          # amol/s -> pmol/day
    delivered_pmol_cm2_day = MEDIUM_PER_CM2_DAY * O2_NMOL_PER_UL * 1000.0 * O2_EXTRACTION
    return delivered_pmol_cm2_day / per_cell_day


def sealed_reserve_min(lane: str, cells_per_cm2: float) -> float:
    """Minutes of oxygen in ONE sealed field: why no hold in this protocol caps the ports."""
    nmol = field_volume_ul(lane) * O2_NMOL_PER_UL
    cells = cells_per_cm2 * FIELD_W[lane] * FIELD_L / 100.0
    per_min = cells * O2_AMOL_CELL_S * 60 * 1e-9          # amol/s -> nmol/min
    return nmol / per_min


def rect_resistance(length_mm: float, width_mm: float, depth_mm: float = DEPTH) -> float:
    """Hydraulic resistance of a rectangular duct, Pa s / m3: R = 12 mu L / (a b^3 (1 - 0.63 b/a)),
    a and b the long and short sides. Used for the ratio that decides flow distribution, so the
    "the restrictor dominates" claim is derived rather than asserted."""
    a, b = max(width_mm, depth_mm) * 1e-3, min(width_mm, depth_mm) * 1e-3
    return 12.0 * MU * (length_mm * 1e-3) / (a * b ** 3 * (1.0 - 0.63 * b / a))


def restrictor_dominance() -> tuple[float, float, float]:
    """(restrictor R, one spine span R, ratio). The ratio is what makes six parallel fields
    split the lane flow evenly: the shared spine's resistance is a small correction to each
    field's own."""
    r_restrictor = rect_resistance(FEED_RISER_FOLDED, FEED_RISER_W)
    r_span = rect_resistance(FIELD_PITCH - SPINE_H, SPINE_LINK_W)
    return r_restrictor, r_span, r_restrictor / r_span


def diffusion_time_days(distance_mm: float) -> float:
    """x = 2 sqrt(D t), the convention protocols/module1_design.md used, so the demo's
    '33 mm in 7 days' and these numbers are the same arithmetic."""
    x = distance_mm * 1e-3
    return x * x / (4.0 * D_SMALL) / DAY


def diffusion_reach_mm(seconds: float) -> float:
    return 2.0 * math.sqrt(D_SMALL * seconds) * 1e3


def seed_flow_ul_min(group: int) -> float:
    """Lowest seeding flow that carries cells to the FAR END of a group's spine instead of
    letting them settle in it: the same U > L v_s / h criterion, applied to the spine."""
    x0, x1 = SPINE_X[group]
    u = washout_mm_s(length_mm=x1 - x0)                   # mm/s needed along the spine
    return SEED_MARGIN * u * SPINE_LINK_W * DEPTH * 60.0  # mm/s x mm2 -> uL/min


# Path length from the CELLS to the shared bus: up the drain riser of the field nearest the
# bus, along the collection spine to its right end, then the spoke. And from there across the
# bus to the next band's spine, which is the shortest route from one lane's cells to another's.
TO_BUS_MM = RISER + (SPINE_X[2][1] - field_centre_x(N_FIELDS - 1)) + (BUS_X - BUS_W / 2 - SPINE_X[2][1])
FIELD_TO_FIELD_MM = 2 * TO_BUS_MM + (BAND_H + CORRIDOR)


# --- the chip -------------------------------------------------------------------------

def chamber(cid: str, ctype: str, block: str, cx: float, cy: float, sx: float, sy: float,
            note: str) -> dict:
    return {"id": cid, "type": ctype, "block": block,
            "center": [round(cx, 3), round(cy, 3), round(DEPTH / 2, 3)],
            "size": [round(sx, 3), round(sy, 3), DEPTH],
            "mesh_pore_um": None,
            "volume_ul": round(sx * sy * DEPTH, 3),
            "note": note}


def channel(cid: str, src: str, dst: str, width: float, block: str, carries: str, note: str) -> dict:
    return {"id": cid, "from": src, "to": dst, "width_mm": width, "depth_mm": DEPTH,
            "block": block, "carries": carries, "note": note}


def port(pid: str, ptype: str, x: float, y: float, role: str, note: str) -> dict:
    return {"id": pid, "type": ptype, "position": [round(x, 3), round(y, 3), round(DEPTH + 3.0, 3)],
            "role": role, "width_um": PORT_BORE_UM, "note": note}


def build_chip() -> dict:
    chambers, channels, ports, stages, zones = [], [], [], [], []
    lane_fields: list[str] = []

    # THE BUS. One tall bar would be geometrically wrong, and the crossing check proved it: a
    # channel is cut as a straight run to the chamber's CENTRE, so six spokes into one tall bar
    # are six diagonals that slice through the other lanes' bands. The bus is therefore a
    # vertical CHAIN of small routing nodes, one at each spoke's own y, joined by vertical
    # links: every spoke enters its node horizontally and nothing crosses anything. It is the
    # same thing hydraulically - a shared manifold with six taps - and it is buildable.
    bus_nodes = [(lane, group, bus_y(lane, group)) for lane in ORDER for group in (2, 1)]
    bus_nodes.sort(key=lambda t: t[2])
    for lane, group, y in bus_nodes:
        chambers.append(chamber(
            bus_id(lane, group), "junction", "network_node", BUS_X, y, BUS_W, BUS_W,
            f"Shared-bus node for the {LANES[lane]['name']} lane's group {group}. Three arms: the lane's "
            f"spoke and the two vertical links to its neighbours on the bus. The bus is the ONLY place the "
            f"three lanes can meet, and every lane reaches it through its own spoke, so no lane's medium "
            f"crosses another lane's fields to get there - the mistake the demo chip's first version made. "
            f"Dead and capped for all {MATURE_DAY} days of differentiation."))
    for i, ((la, ga, ya), (lb, gb, yb_)) in enumerate(zip(bus_nodes[:-1], bus_nodes[1:]), start=1):
        channels.append(channel(f"c_bus{i}", bus_id(la, ga), bus_id(lb, gb), SPINE_LINK_W,
                                "straight_channel", "cells",
                                "Bus link: vertical, so it crosses nothing. The chain of links is what makes "
                                "the six taps one shared node."))

    for lane in ORDER:
        L = LANES[lane]
        yb = band_y0(L["band"])
        w = FIELD_W[lane]
        y_d = yb + SPINE_H / 2
        y_c = yb + BAND_H - SPINE_H / 2
        y_field = yb + SPINE_H + RISER + FIELD_L / 2
        y_cport = yb + BAND_H + PORT_DY
        y_link = yb + BAND_H + LINK_DY
        pitches = ", ".join(f"{p:g}" for p in (5, 10))

        # THE TWO SPINES ARE LADDERS, NOT BARS, for the same reason the bus is a chain: a riser
        # is cut as a straight run between the two features' CENTRES, so one wide spine chamber
        # makes six splayed risers that cut through neighbouring fields (the planar checker
        # caught exactly that). A tee at each field's own x gives vertical risers and is the
        # honest drawing of a distribution ladder anyway.
        for i in range(N_FIELDS):
            group = 1 if i in GROUPS[1] else 2
            chambers.append(chamber(
                f"{lane}_dn{i + 1}", "junction", "t_junction", field_centre_x(i), y_d, SPINE_H, SPINE_H,
                f"Feed tee for field {i + 1} (group {group}): the group's spine comes in, carries on to the "
                f"next tee, and the field hangs off it through its own {FEED_RISER_W * 1000:g} um restrictor "
                f"riser. The restrictor is {restrictor_dominance()[2]:.0f}x the resistance of the spine run "
                f"beside it, which is what "
                f"makes {len(GROUPS[group])} parallel fields split the flow evenly - flow distribution "
                f"across a wide chamber is explicitly NOT scale-free (blocks.json)."))
            chambers.append(chamber(
                f"{lane}_cn{i + 1}", "junction", "t_junction", field_centre_x(i), y_c, SPINE_H, SPINE_H,
                f"Collection tee for field {i + 1} (group {group}). The group's collection spine carries "
                f"its effluent, which is also the supernatant the secretome arm samples."))
            if i - 1 in GROUPS[group]:
                for which, yy, carries in (("d", y_d, "cells"), ("c", y_c, "cells")):
                    channels.append(channel(
                        f"{lane}_{which}s{i}", f"{lane}_{which}n{i}", f"{lane}_{which}n{i + 1}",
                        SPINE_LINK_W, "straight_channel", carries,
                        f"Group-{group} {'feed' if which == 'd' else 'collection'} spine, tee {i} to tee "
                        f"{i + 1}. {SPINE_LINK_W:g} mm wide on purpose: narrow enough that the seeding flow "
                        f"keeps cells in suspension over the whole run instead of letting them settle out "
                        f"before the far field, which is the demo chip's seeding failure at target scale."))

        chambers.append(chamber(
            f"{lane}_jl", "junction", "network_node", field_centre_x(QUENCH_FIELDS - 1), y_link, 1.0, 1.0,
            "Routing node that lifts the group-1 spoke out of the band into the corridor, so it can run to "
            "the bus without crossing the group-2 spine. Pure geometry: two arms, no process function. "
            "Group 1 has to be on the bus, because fields quenched for metabolome and proteome must have "
            "the same interaction history as the fields that get sequenced."))

        for i in range(N_FIELDS):
            group = 1 if i in GROUPS[1] else 2
            fid = f"{lane}_f{i + 1}"
            lane_fields.append(fid)
            purpose = ("QUENCHED IN PLACE for metabolome + proteome" if group == 1
                       else "DISSOCIATED for the single-cell transcriptome")
            chambers.append(chamber(
                fid, "culture", "pillar_array", field_centre_x(i), y_field, w, FIELD_L,
                f"Culture field {i + 1} of {N_FIELDS} in the {L['name']} lane, {w * FIELD_L / 100:.2f} cm2: "
                f"{purpose}. Replicated fields, not one big chamber (blocks.json: 'replicated fields and "
                f"more lanes, not bigger fields'), which is what lets one subset be quenched while the "
                f"other is harvested. block pillar_array: a {DEPTH:g} mm cavity {w:g} mm across would "
                f"bow under bonding and operating pressure, so the lid stands on a pillar grid at a "
                f"{pitches} mm pitch - the block's first listed purpose - costing about 2% of the floor. "
                f"NO retention feature of any kind: at the design flow a {CELL_UM:g} um cell settles "
                f"{washout_mm_s() / mean_velocity_mm_s(group_flow_ul_min(lane, group) / len(GROUPS[group]), w):.0f}x "
                f"faster than the liquid can carry it out, so even the non-adherent hemogenic "
                f"progenitors stay where they are born."))
            channels.append(channel(
                f"{lane}_fr{i + 1}", f"{lane}_dn{i + 1}", fid, FEED_RISER_W, "serpentine_channel", "cells",
                f"Feed riser AND flow restrictor: {FEED_RISER_W * 1000:g} x {DEPTH * 1000:g} um, folded to "
                f"~{FEED_RISER_FOLDED:g} mm inside the {RISER:g} mm riser band (about 4 folds at the "
                f"0.4 mm wall floor). It does two jobs: it makes the six fields split the lane flow "
                f"evenly, and it is wide enough (200 um vs a 12 um cell) to seed through. Drawn as one "
                f"straight run between its two ends, which is what the planar checker sees; the folds "
                f"live in the reserved band and nothing else is placed there."))
            channels.append(channel(
                f"{lane}_dr{i + 1}", fid, f"{lane}_cn{i + 1}", DRAIN_RISER_W, "straight_channel", "cells",
                "Drain riser: short and wide, so the pressure drop on the way out is negligible and a "
                "harvested cell suspension does not have to pass the restrictor."))

        ports.append(port(f"p_{lane}_in1", "inlet", LEFT_PORT_X, y_d, "medium",
                          f"Group-1 feed (fields 1-{QUENCH_FIELDS}). During culture it perfuses them from the "
                          f"lane's own vented reservoir, in proportion to their area. At the endpoint the SAME "
                          f"line delivers the rinse and then the cold solvent quench, which is why group 1 "
                          f"needs no extra port: the group has its own hydraulic path, so the quench cannot "
                          f"reach the harvest fields."))
        ports.append(port(f"p_{lane}_ex", "outlet", LEFT_PORT_X, y_c, "harvest",
                          "Group-1 outlet: its effluent during culture (so it doubles as a second "
                          "supernatant tap) and the metabolome + proteome EXTRACT at the endpoint. At the "
                          "left end of the collection spine, so quench flow runs away from fields 3-6."))
        ports.append(port(f"p_{lane}_in2", "inlet", RIGHT_PORT_X, y_d, "medium",
                          f"Group-2 feed (fields {QUENCH_FIELDS + 1}-{N_FIELDS}), from the same reservoir and "
                          f"the same pump as group 1, split off-chip with matched line lengths. Same medium, "
                          f"same age, same gas: that is what makes all six fields ONE sample population."))
        ports.append(port(f"p_{lane}_sup", "outlet", SUP_X, y_cport, "waste",
                          "SUPERNATANT SAMPLING. A dedicated tap on the group-2 collection spine for repeated "
                          "conditioned-medium draws (secretome / exometabolome), the arm the multi-omic design "
                          "says answers the interaction question and that the demo chip has no port for at "
                          "all. Drawing here pulls the lane's own effluent, so a draw does not break "
                          "isolation; it is separate from the recirculation return so the sterile loop is "
                          "never opened to sample it."))
        ports.append(port(f"p_{lane}_ret", "outlet", RET_X, y_cport, "waste",
                          "Recirculation return: group 2's effluent back to this lane's vented gas-exchange "
                          "reservoir (stages[]). The return is what makes the lane a CLOSED LOOP during "
                          "differentiation, so the shared bus is capped and dead and the lanes are "
                          "hydraulically separate - a stronger isolation than the demo's balanced flows."))

        first1, last1 = GROUPS[1][0] + 1, GROUPS[1][-1] + 1
        first2, last2 = GROUPS[2][0] + 1, GROUPS[2][-1] + 1
        channels.append(channel(f"{lane}_cin1", f"p_{lane}_in1", f"{lane}_dn{first1}", SPINE_LINK_W,
                                "straight_channel", "cells",
                                "Group-1 feed line onto the left end of its ladder."))
        channels.append(channel(f"{lane}_cex", f"{lane}_cn{first1}", f"p_{lane}_ex", SPINE_LINK_W,
                                "straight_channel", "cells",
                                "Group-1 outlet line, off the LEFT end of its collection ladder: at the "
                                "endpoint the quench therefore flows away from the harvest fields, which is "
                                "half of why one subset can be quenched without disturbing the other."))
        channels.append(channel(f"{lane}_cin2", f"p_{lane}_in2", f"{lane}_dn{last2}", SPINE_LINK_W,
                                "straight_channel", "cells",
                                "Group-2 feed line onto the right end of its ladder."))
        channels.append(channel(f"{lane}_csup", f"{lane}_cn{first2}", f"p_{lane}_sup", SPINE_LINK_W,
                                "straight_channel", "liquid",
                                "Supernatant sampling tap: straight up out of a group-2 collection tee."))
        channels.append(channel(f"{lane}_cret", f"{lane}_cn{last2}", f"p_{lane}_ret", SPINE_LINK_W,
                                "straight_channel", "cells", "Recirculation return to the reservoir."))
        channels.append(channel(f"{lane}_c2bus", f"{lane}_cn{last2}", bus_id(lane, 2), SPINE_LINK_W,
                                "straight_channel", "cells",
                                "Group-2 spoke to the shared bus: the interaction and pooling route. Capped "
                                "during differentiation."))
        channels.append(channel(f"{lane}_c1jl", f"{lane}_cn{last1}", f"{lane}_jl", SPINE_LINK_W,
                                "straight_channel", "liquid",
                                "Group-1 spoke, part 1: straight up out of the band into the corridor."))
        channels.append(channel(f"{lane}_jlbus", f"{lane}_jl", bus_id(lane, 1), SPINE_LINK_W,
                                "straight_channel", "liquid",
                                "Group-1 spoke, part 2: along the corridor to the bus. Group 1 is on the "
                                "interaction route too, so the fields that get quenched for metabolome and "
                                "proteome have exactly the same interaction history as the fields that get "
                                "sequenced. Without this link the multi-omic claim would be false."))

        stages.append({
            "id": f"{lane}_gasx", "type": "delay", "from_port": f"p_{lane}_ret",
            "to_port": f"p_{lane}_in2", "location": "off_chip",
            "temp_c": CULTURE_TEMP, "time_min": [120.0, 3600.0], "volume_ul": [5000.0, 10000.0],
            "note": "VENTED GAS-EXCHANGE RESERVOIR, off-chip, one per lane, in the incubator. This is the "
                    "part the demo Module 1 lacks and the reason its cells are anoxic two thirds of every "
                    "day: the chip is deliberately NOT gas-permeable, so all gas exchange happens here and "
                    "none of it in the chambers. It re-saturates the medium in minutes against a loop "
                    "turnover of 21-42 h, so it is never the limiting step; it also reproduces the "
                    "published 'top up rather than replace' regime, because the lane's own secreted factors "
                    "stay in the loop. Half the reservoir is replaced with fresh medium every 24 h, which "
                    "is the 300-600 uL/cm2/day of FRESH medium; the recirculation rate is a separate, "
                    "oxygen-driven number. The group-1 line is fed from and returns to this same reservoir."},
        )

    ports.append(port("p_bus", "outlet", BUS_PORT_X, bus_y(ORDER[0], 2), "harvest",
                      "The bus drain: vented when the three lanes are deliberately connected (harvest and "
                      "pooling), CAPPED for all 80 days of differentiation. Only a capped port is a true "
                      "block, so this one cap is what holds the three lanes apart."))
    ports.append(port("p_veh", "inlet", BUS_PORT_X, bus_y(ORDER[-1], 1), "reagent",
                      "VEHICLE CONTROL inlet, straight into the bus. The interaction arm pushes a donor "
                      "lane's conditioned medium across the bus into the other two; this port pushes the "
                      "same medium, from an identical vented reservoir that never held cells, along exactly "
                      "the same route at the same flow for the same time. Without it a recipient's response "
                      "is confounded by the medium itself, which is what the audit found in the demo "
                      "protocol. One port's state is the only difference between the two arms."))
    channels.append(channel("c_bus_out", bus_id(ORDER[0], 2), "p_bus", SPINE_LINK_W, "straight_channel",
                            "cells", "Bus drain line, off the node at one end of the chain."))
    channels.append(channel("c_veh_in", "p_veh", bus_id(ORDER[-1], 1), SPINE_LINK_W, "straight_channel",
                            "liquid", "Vehicle-control feed line, off the node at the other end."))

    zones.append({
        "id": "tz_culture", "chambers": lane_fields, "max_temp_c": CULTURE_TEMP,
        "min_temp_c": CULTURE_TEMP, "purpose": "culture",
        "note": "All 18 culture fields sit in one 37 C zone: the whole board is in an incubator, which is "
                "also why the gas-exchange reservoir can be off-chip and still be in the same environment.",
    })

    return {
        "id": "module1_target_v1",
        "name": "Module 1 at target scale: three perfused lanes, replicated fields, one sample for every omic arm",
        "scale": "target",
        "description": (
            "TARGET-SCALE Module 1 on the ANSI/SLAS 127.76 x 85.48 mm footprint "
            "(contracts/blocks.json target_envelope). The demo chip/module1_v1.json is kept alongside it, "
            "unchanged; this is not a replacement of its architecture but of its SCALE, which is what the "
            "biology audit found fatal. Three lanes, one lineage each, sized from each route's own published "
            "needs: neural 13.23 cm2, glia 13.23 cm2, astrocyte 9.24 cm2, 35.7 cm2 of culture in total, 33% "
            "of the plate. Each lane is SIX replicated fields of 2.2 cm2 (1.5 cm2 astrocyte) on one etch "
            "depth, fed by a distribution spine through per-field restrictor risers and drained by a "
            "collection spine. The fields are split into two hydraulic groups: fields 1-2 are quenched in "
            "place for metabolome and proteome while fields 3-6 of the SAME lane are dissociated for the "
            "single-cell transcriptome, and the lane's effluent is sampled repeatedly for the secretome. "
            "Same lane, same medium, same reservoir, same differentiation, same interaction history, so "
            "'multi-omic from one sample' is true across arms rather than true within one arm. "
            "There are no valves, no barriers and NO RETENTION FEATURES: routing is by which port is FEED, "
            "VENT or CAP, as on the demo chip, and non-adherent cells are held by sedimentation (an 88x "
            "margin) rather than by a weir that would vent them. Perfusion is CONTINUOUS at "
            "480 uL/cm2/day through an off-chip vented gas-exchange reservoir per lane (stages[]); no step "
            "in the protocol caps a lane. 17 ports: 5 per lane plus a bus drain and a vehicle-control "
            "inlet. The 6-port limit is a demo-envelope constraint that blocks.json says lifts at this "
            "scale; every process floor still binds (0.2 mm open channel, 0.4 mm wall, 0.8 mm port, 5 mm "
            "port pitch, 0.3 mm depth, one fluidic layer, no crossings, library blocks only). "
            "Harvest chemistry is deliberately NOT designed here: the day-87 lift belongs to the cell-prep "
            "stage, and this chip hands over adherent, lane-labelled cells in place. "
            "Derivation and arithmetic: protocols/module1_target_design.md."
        ),
        "placeholder": False,
        "units": "mm",
        "generator": {"script": "protocols/build_module1_target.py",
                      "params": {"field_length_mm": FIELD_L, "field_widths_mm": FIELD_W,
                                 "fields_per_lane": N_FIELDS, "quench_fields_per_lane": QUENCH_FIELDS,
                                 "depth_mm": DEPTH, "medium_ul_cm2_day": MEDIUM_PER_CM2_DAY}},
        "footprint": {"x": FOOT_X, "y": FOOT_Y,
                      "note": "ANSI/SLAS 1-2004 microplate footprint, so plate handlers, readers, imagers, "
                              "liquid handlers and incubator shelves all take it (blocks.json _why_sbs)."},
        "layers": [
            {"id": "base", "material": "coc", "thickness_mm": 3.0,
             "note": "Stock sheet, bonded under the fluidic layer."},
            {"id": "fluidic", "material": "coc", "thickness_mm": DEPTH,
             "note": "THE one fluidic layer and THE one etch depth: every chamber and channel on this chip "
                     "is 0.3 mm deep, hot-embossed from a resin-printed tool."},
            {"id": "lid", "material": "coc", "thickness_mm": 3.0,
             "note": "Sealing lid, ports drilled through. It is what seals the sub-0.5 mm channels: they "
                     "are printed OPEN and closed by this layer, which is the route blocks.json's "
                     "min_enclosed_printed_channel_mm assumes. Optically clear for the imaging gate."},
        ],
        "chambers": chambers,
        "channels": channels,
        "ports": ports,
        "stages": stages,
        "thermal_zones": zones,
        "features": [{
            "id": "optical_window", "type": "optical_window",
            "center": [round((FIELD_X0 + field_centre_x(N_FIELDS - 1) + FIELD_W["n"] / 2) / 2, 3),
                       round((band_y0(0) + band_y0(2) + BAND_H) / 2, 3), round(DEPTH + 3.0, 3)],
            "size": [round(field_centre_x(N_FIELDS - 1) + FIELD_W["n"] / 2 - FIELD_X0, 3),
                     round(band_y0(2) + BAND_H - band_y0(0), 3), 3.0],
            "covers": lane_fields,
            "note": "The lid is clear over every field. Imaging is not decoration here: colony count at "
                    "day 0 is the seeding gate, and the imaged density is what sets each lane's perfusion "
                    "rate, because oxygen demand scales with cells and not with area.",
        }],
        "run_parameters": {
            "variant": {
                "values": ["interaction", "vehicle"],
                "default": "interaction",
                "description": "Which arm this board runs. interaction: a donor lane's conditioned medium "
                               "crosses the bus to the other two. vehicle: the SAME medium, from an "
                               "identical vented reservoir that never held cells, crosses the same route "
                               "at the same flow through p_veh. Two boards, one spec; the comparison is "
                               "what separates a secretome response from a medium-change response.",
            },
        },
        # Three per-lane labels, applied here. That is DEMULTIPLEXING metadata only - which
        # lineage a read came from - and it is deliberately all this chip claims: the cell's
        # identity is the random barcode made per capsule downstream (4^barcoding.random_bases),
        # which is what makes the assay single-cell. The two are different things and conflating
        # them is what made this repo wrongly conclude the data was lane-level.
        "barcoding": {"lanes": len(ORDER)},
    }


# --- the protocol ---------------------------------------------------------------------

def _floor1(value: float) -> float:
    """Round a volume DOWN to 0.1 uL. Rounding it UP makes the declared duration a second
    short of what the declared flow needs, which tools/validate.py rightly warns about."""
    return math.floor(value * 10.0) / 10.0


def port_states(mode: str, lane: str | None = None) -> dict:
    """FEED / VENT / CAP for every port, by operating mode. Only a CAP is a true block."""
    all_ports = [f"p_{k}_{s}" for k in ORDER for s in ("in1", "in2", "ex", "sup", "ret")] + ["p_bus", "p_veh"]
    feed: list[str] = []
    vent: list[str] = []
    if mode == "grow":                      # three closed loops, bus dead
        feed = [f"p_{k}_in{g}" for k in ORDER for g in (1, 2)]
        vent = [f"p_{k}_{s}" for k in ORDER for s in ("ex", "ret")]
    elif mode == "lane":                    # one lane only (seeding, attachment, its own schedule)
        feed = [f"p_{lane}_in1", f"p_{lane}_in2"]
        vent = [f"p_{lane}_ex", f"p_{lane}_ret"]
    elif mode == "sample":                  # draw each lane's own effluent, bus still capped
        feed = [f"p_{k}_in{g}" for k in ORDER for g in (1, 2)]
        vent = [f"p_{k}_{s}" for k in ORDER for s in ("sup", "ex")]
    elif mode == "interact":                # donor -> bus -> the other two, backwards through their fields
        feed = [f"p_{lane}_in1", f"p_{lane}_in2"]
        vent = [f"p_{k}_in{g}" for k in ORDER if k != lane for g in (1, 2)]
    elif mode == "vehicle":                 # same, but from the cell-free reservoir
        feed = ["p_veh"] + [f"p_{lane}_in1", f"p_{lane}_in2"]
        vent = [f"p_{k}_in{g}" for k in ORDER if k != lane for g in (1, 2)] + \
               [f"p_{lane}_ex", f"p_{lane}_ret"]
    elif mode == "quench":                  # group 1 of one lane; group 2 untouched
        feed = [f"p_{lane}_in1"]
        vent = [f"p_{lane}_ex"]
    elif mode == "pool":                    # the deliberate connection: everything to the bus
        feed = [f"p_{k}_in2" for k in ORDER]
        vent = ["p_bus"]
    cap = [p for p in all_ports if p not in feed and p not in vent]
    return {"feed": feed, "vent": vent, "cap": cap}


MODE_TAG = {
    "grow": "GROW (three independent loops, bus capped)",
    "lane": "LANE (one lane on its own loop)",
    "sample": "SAMPLE (per-lane supernatant draw, bus capped)",
    "interact": "INTERACT (donor -> bus -> the other two)",
    "vehicle": "VEHICLE CONTROL (cell-free medium -> bus -> the same two)",
    "quench": "QUENCH (group 1 only; group 2 stays wet and untouched)",
    "pool": "POOL (lanes deliberately connected at the bus)",
}


def build_protocol(chip: dict) -> dict:
    vols = {c["id"]: c["volume_ul"] for c in chip["chambers"]}
    steps: list[dict] = []

    def add(day, operation, target, note, mode, lane=None, reagent=None, volume_ul=None,
            duration_s=None, washes=None, flow_ul_min=None, temp_c=None, variant=None,
            targets=None):
        step = {"step": len(steps) + 1, "operation": operation,
                "reagent": reagent, "volume_ul": volume_ul,
                "duration_s": round(duration_s or 0, 3), "temp_c": temp_c,
                "washes": washes, "flow_ul_min": flow_ul_min,
                "ports": port_states(mode, lane),
                "note": f"Day {day:g}. {MODE_TAG[mode]}: {note}"}
        if targets is not None:
            step["targets"] = targets
        else:
            step["target"] = target
        if variant:
            step["variant"] = variant
        steps.append(step)

    def perfuse(day, lane, group, days, reagent, note, mode="grow", flow=None):
        q = round(flow if flow is not None else group_flow_ul_min(lane, group), 3)
        minutes = days * 1440.0
        add(day, "exchange", feed_tee(lane, group), note, mode, lane=lane, reagent=reagent,
            volume_ul=_floor1(q * minutes), washes=1, flow_ul_min=q,
            duration_s=minutes * 60.0, temp_c=CULTURE_TEMP)

    # --- per lane: coat, seed, attach, then continuous perfusion to day 80 -------------
    for lane in ORDER:
        L = LANES[lane]
        seed, switch = L["seed"], L["switch"]
        med1, med2 = L["media"]
        field_v = field_volume_ul(lane)
        for group in (1, 2):
            wetted = len(GROUPS[group]) * field_v + 2 * sum(vols[f"{lane}_{w}n{i + 1}"]
                                                           for i in GROUPS[group] for w in "dc")
            add(seed - 0.5, "exchange", feed_tee(lane, group),
                f"Coat group {group} of the {L['name']} lane through p_{lane}_in{group} alone. One wetted "
                f"volume ({wetted:.0f} uL) so every field and both spines see the coating; the other two "
                f"lanes are on their own loops and are not touched.",
                "lane", lane=lane, reagent="coating_solution", volume_ul=_floor1(wetted), washes=1,
                flow_ul_min=round(seed_flow_ul_min(group), 2),
                duration_s=round(_floor1(wetted) / round(seed_flow_ul_min(group), 2) * 60.0 + 1, 1),
                temp_c=CULTURE_TEMP)
        for i in range(N_FIELDS):
            group = 1 if i in GROUPS[1] else 2
            q = seed_flow_ul_min(group)
            per_field = q / len(GROUPS[group])
            add(seed, "load", f"{lane}_f{i + 1}",
                f"Seed the {L['name']} lane's field {i + 1}: one field volume of suspension, delivered "
                f"through p_{lane}_in{group} with the spine flow at {q:.0f} uL/min. That flow is set by "
                f"sedimentation, not by habit: below about {q:.0f} uL/min cells settle in the spine before "
                f"they reach the far field and the six fields seed unequally, which is the demo chip's "
                f"seeding failure re-created at target scale. At {per_field:.1f} uL/min per field the wall "
                f"shear is {shear_dyn_cm2(per_field, FIELD_W[lane]):.4f} dyn/cm2, still under the "
                f"{SHEAR_CEILING:g} ceiling, and the cells are not attached yet.",
                "lane", lane=lane, volume_ul=_floor1(field_v),
                duration_s=round(field_v / per_field * 60.0, 1), temp_c=CULTURE_TEMP)
        for group in (1, 2):
            q = ATTACH_FLOW_PER_LANE * len(GROUPS[group]) / N_FIELDS
            per_field = q / len(GROUPS[group])
            u = mean_velocity_mm_s(per_field, FIELD_W[lane])
            perfuse(seed + 0.02, lane, group, 1.0, med1,
                    f"ATTACHMENT, 24 h, PERFUSED at {q:.2f} uL/min - NOT a capped hold. A sealed field holds "
                    f"about {sealed_reserve_min(lane, 50_000):.0f} min of oxygen for its cells, so the demo's "
                    f"'all ports capped, 24 h' starves them for two thirds of every day; this is the single "
                    f"change that fixes audit finding 2. At this flow U = {u:.5f} mm/s against the "
                    f"{washout_mm_s():.3f} mm/s a cell needs to be carried out, a factor of "
                    f"{washout_mm_s() / u:.0f}, so unattached cells cannot be washed away either.",
                    mode="lane", flow=q)
        day = seed + 1.02
        switched = False
        while day < MATURE_DAY:
            block = min(PERFUSION_BLOCK_DAYS, MATURE_DAY - day)
            use2 = switch is not None and day >= switch
            medium = med2 if use2 else med1
            first_switch = use2 and not switched and med1 != med2
            switched = switched or first_switch
            for group in (1, 2):
                q = group_flow_ul_min(lane, group)
                perfuse(day, lane, group, block, medium,
                        f"Day {day - seed:g} of the {L['name']} lane, group {group}: continuous perfusion with "
                        f"{medium} at {q:.2f} uL/min for {block:g} days"
                        + (" (first block on the second-stage medium; the loop reaches >99% of the new medium "
                           "within one reservoir exchange, so the stage switch does not need a sweep step)"
                           if first_switch else "")
                        + f". That is {MEDIUM_PER_CM2_DAY:g} uL/cm2/day of medium over "
                          f"{group_area_cm2(lane, group):.2f} cm2, inside the standard 300-600, and "
                          f"{group_flow_ul_min(lane, group) * 1440 / (len(GROUPS[group]) * field_volume_ul(lane)):.0f} "
                          f"chamber volumes a day. Half the reservoir is replaced with fresh medium every 24 h; "
                          f"the rest recirculates through the vented reservoir, which is what keeps the lane's "
                          f"own secreted factors in the loop. Wall shear "
                          f"{shear_dyn_cm2(q / len(GROUPS[group]), FIELD_W[lane]):.5f} dyn/cm2, "
                          f"{SHEAR_CEILING / shear_dyn_cm2(q / len(GROUPS[group]), FIELD_W[lane]):.0f}x under the "
                          f"ceiling.")
            day += block

    # --- interaction window -----------------------------------------------------------
    for lane, day in INTERACT:
        L = LANES[lane]
        others = [LANES[k]["name"] for k in ORDER if k != lane]
        medium = L["media"][1]
        donor_q = 2 * lane_flow_ul_min(lane)
        for k in ORDER:
            add(day - 0.1, "collect", None,
                f"Baseline supernatant draw from the {LANES[k]['name']} lane before the "
                f"{L['name']}-source direction: {SAMPLE_UL:g} uL from p_{k}_sup (group 2) and p_{k}_ex "
                f"(group 1). Non-destructive, repeatable, and the only arm that can be sampled over time. "
                f"All three lanes draw their own effluent at once with the bus capped, so sampling is MORE "
                f"isolated than growth, not less.",
                "sample", lane=k, volume_ul=SAMPLE_UL,
                targets=[f"{k}_cn{GROUPS[1][0] + 1}", f"{k}_cn{GROUPS[2][0] + 1}"],
                duration_s=round(SAMPLE_UL / group_flow_ul_min(k, 2) * 60.0, 1), temp_c=CULTURE_TEMP)
        for variant, mode in (("interaction", "interact"), ("vehicle", "vehicle")):
            src = (f"the {L['name']} lane's own conditioned medium, pushed from its reservoir"
                   if variant == "interaction" else
                   f"{medium} from an identical vented reservoir that has never held cells, pushed through "
                   f"p_veh straight into the bus")
            add(day, "exchange", bus_id(lane, 2),
                f"{INTERACT_HOURS:g} h interaction window, {L['name']} -> {others[0]} + {others[1]}: "
                f"{src}, through the bus and BACKWARDS down each recipient's collection spine into its six "
                f"fields, leaving at their feed ports. Donor feed {donor_q:.1f} uL/min so each recipient "
                f"gets {donor_q / 2:.1f} uL/min, its normal perfusion rate - a half-rate interaction would "
                f"halve its oxygen supply for 6 h. Each recipient receives "
                f"{donor_q / 2 * INTERACT_HOURS * 60 / (N_FIELDS * field_volume_ul(lane)):.1f} lane volumes. "
                f"The recipients' own outlets are CAPPED, so the medium cannot short-circuit along their "
                f"spines without crossing the fields. Recipients' group 1 is on the bus too, so the fields "
                f"that will be quenched for metabolome and proteome see the same exposure as the fields "
                f"that will be sequenced.",
                mode, lane=lane, reagent=medium, volume_ul=_floor1(round(donor_q, 2) * INTERACT_HOURS * 60),
                washes=1, flow_ul_min=round(donor_q, 2), duration_s=INTERACT_HOURS * 3600.0,
                temp_c=CULTURE_TEMP, variant=variant)
        for k in ORDER:
            add(day + 0.3, "collect", None,
                f"Response draw from the {LANES[k]['name']} lane after the {L['name']}-source window, same "
                f"volumes and ports as the baseline. The interaction arm and the vehicle arm are two boards "
                f"of the same spec (run_parameters.variant), so this pair of draws is the comparison that "
                f"separates a secretome response from a medium-change response.",
                "sample", lane=k, volume_ul=SAMPLE_UL,
                targets=[f"{k}_cn{GROUPS[1][0] + 1}", f"{k}_cn{GROUPS[2][0] + 1}"],
                duration_s=round(SAMPLE_UL / group_flow_ul_min(k, 2) * 60.0, 1), temp_c=CULTURE_TEMP)
        for k in ORDER:
            for group in (1, 2):
                perfuse(day + 0.4, k, group, 0.6, LANES[k]["media"][1],
                        f"Reset the {LANES[k]['name']} lane to its own medium on its own closed loop for "
                        f"{0.6 * 24:.0f} h before the next direction, so no lane starts a direction carrying "
                        f"another lane's secretome.")

    # --- endpoint: quench group 1 in place, hand group 2 over -------------------------
    for lane in ORDER:
        L = LANES[lane]
        field_v = field_volume_ul(lane)
        rinse_q = 60.0
        for i in GROUPS[1]:
            fid = f"{lane}_f{i + 1}"
            add(ENDPOINT_DAY, "exchange", fid,
                f"Mandatory rinse of {fid} before the quench: residual medium is about 150:1 against "
                f"intracellular volume and would swamp the metabolome. One field volume at {rinse_q:g} uL/min "
                f"is {shear_dyn_cm2(rinse_q, FIELD_W[lane]):.4f} dyn/cm2, "
                f"{RINSE_CEILING / shear_dyn_cm2(rinse_q, FIELD_W[lane]):.0f}x under the "
                f"{RINSE_CEILING:g} dyn/cm2 pre-lyse rinse ceiling - the culture ceiling is the wrong bound "
                f"for cells that are about to be killed on purpose, and holding this rinse to it would take "
                f"minutes and perturb the measurement more than it protects it.",
                "quench", lane=lane, reagent="cold_volatile_rinse", volume_ul=_floor1(field_v),
                washes=1, flow_ul_min=rinse_q, duration_s=round(field_v / rinse_q * 60.0 + 1, 1), temp_c=4)
        for i in GROUPS[1]:
            fid = f"{lane}_f{i + 1}"
            add(ENDPOINT_DAY + 0.01, "exchange", fid,
                f"QUENCH {fid} IN PLACE: one field volume of cold acidified organic quench in through "
                f"p_{lane}_in1, extract out through p_{lane}_ex. One extraction gives the metabolome in the "
                f"supernatant and the proteome in the pellet (SIMPLEX / MPLEx), on ADHERENT cells, which is "
                f"7x better than scrape-in-buffer and avoids measuring capsule protein instead of cell "
                f"protein. Fields {GROUPS[1][0] + 1} and {GROUPS[1][-1] + 1} are a technical duplicate of "
                f"about {50_000 * group_area_cm2(lane, 1):,.0f} cells. Group 2 is CAPPED at both ends and sees "
                f"no flow; the quench front travels away from it, and in the "
                f"{field_v / rinse_q * 60:.0f} s this takes a small molecule diffuses "
                f"{diffusion_reach_mm(field_v / rinse_q * 60):.2f} mm against the "
                f"{FIELD_PITCH + RISER:.0f} mm to the nearest harvest field. Group 2's "
                f"{sealed_reserve_min(lane, 50_000):.0f} min of sealed oxygen covers this stop with "
                f"{sealed_reserve_min(lane, 50_000) / (2 * field_v / rinse_q):.0f}x to spare.",
                "quench", lane=lane, reagent="cold_acidified_organic_quench", volume_ul=_floor1(field_v),
                washes=1, flow_ul_min=rinse_q, duration_s=round(field_v / rinse_q * 60.0 + 1, 1), temp_c=-20)
        for group in (2,):
            perfuse(ENDPOINT_DAY + 0.05, lane, group, 0.25, L["media"][1],
                    f"HANDOFF HOLD for the {L['name']} lane's transcriptome fields "
                    f"(f{GROUPS[2][0] + 1}-f{GROUPS[2][-1] + 1}, {group_area_cm2(lane, 2):.2f} cm2, about "
                    f"{50_000 * group_area_cm2(lane, 2):,.0f} cells): still perfused at the culture rate, "
                    f"still adherent, still carrying this lane's label. THIS PROTOCOL STOPS HERE ON PURPOSE. "
                    f"The day-87 lift is NOT designed into it: the audit is clear that 80-day cortical "
                    f"neurons do not release with an enzyme-free reagent at 4 C, and harvest chemistry "
                    f"belongs to the cell-prep stage. What is handed over: adherent cells in place, per "
                    f"lane and per field, with an imaged density and a viability gate, reachable through "
                    f"p_{lane}_in2 (feed) and the bus (pooling route, p_bus vented). What the cell-prep "
                    f"stage owes back: a lift chemistry validated on cells of this age, and the "
                    f"{CELL_CONC:g} cells/uL interface the encapsulation module needs.")

    add(ENDPOINT_DAY + 0.3, "collect", None,
        "Final supernatant draw from all three lanes, then the board is handed over. The secretome arm is "
        "the only non-destructive one, so this series - baseline and response around each of the three "
        "directions, plus this endpoint - is the time course the interaction question is actually answered "
        "from.",
        "sample", volume_ul=SAMPLE_UL, targets=[f"{k}_cn{GROUPS[2][0] + 1}" for k in ORDER],
        duration_s=round(SAMPLE_UL / group_flow_ul_min("n", 2) * 60.0, 1), temp_c=CULTURE_TEMP)

    return {
        "id": "module1_target_v1",
        "name": "Three-lineage differentiation at target scale: continuous perfusion, interaction with a vehicle control, and one sample for every omic arm",
        "source": SOURCE,
        "placeholder": False,
        "description": (
            f"Runs chip/module1_target_v1.json for {ENDPOINT_DAY} days. Staggered seeding (day 0 neural, 40 "
            f"glia, 45 astrocyte) so all three lineages mature together on day {MATURE_DAY}; CONTINUOUS "
            f"perfusion throughout at {MEDIUM_PER_CM2_DAY:g} uL/cm2/day through each lane's own off-chip "
            f"vented gas-exchange reservoir, with NO capped hold anywhere - attachment is perfused at a flow "
            f"{washout_mm_s() / mean_velocity_mm_s(ATTACH_FLOW_PER_LANE / N_FIELDS, FIELD_W['n']):.0f}x below "
            f"the velocity that could wash an unattached cell out. Each lane is a closed loop while it "
            f"differentiates and the shared bus is capped, so the lanes are hydraulically separate, not "
            f"merely flow-balanced. Day {MATURE_DAY}-{MATURE_DAY + 6}: one interaction direction at a time "
            f"across the bus, each with a VEHICLE CONTROL arm (the same medium from a reservoir that never "
            f"held cells, same route, same flow, same {INTERACT_HOURS:g} h), selected by the chip's "
            f"run_parameters.variant, with per-lane supernatant draws before and after every direction. "
            f"Day {ENDPOINT_DAY}: fields 1-2 of each lane are rinsed and quenched IN PLACE for metabolome "
            f"and proteome while fields 3-6 of the same lane stay perfused and are handed to the cell-prep "
            f"stage for the transcriptome. Every flow is computed from chamber geometry with "
            f"mu = {MU:g} Pa s (fluid.viscosity_37c_pa_s, medium at 37 C - NOT the {MU_DYE:g} Pa s dye-demo "
            f"value for water at 20 C) and kept under the ceiling that belongs to the step: "
            f"{SHEAR_CEILING:g} dyn/cm2 for living adherent cells, {RINSE_CEILING:g} for the rinse before a "
            f"deliberate quench. Steps are in calendar order, but the three lanes run CONCURRENTLY on "
            f"independent loops, so the durations sum to more than the {ENDPOINT_DAY}-day calendar; a serial "
            f"player will stretch it. Reagent names are generic (CONFIDENTIALITY.md)."
        ),
        "steps": steps,
    }


# --- report ---------------------------------------------------------------------------

def report() -> None:
    v_s = settling_um_s()
    print("TARGET-SCALE MODULE 1: the arithmetic\n")
    print(f"viscosity   mu = {MU:g} Pa s (fluid.viscosity_37c_pa_s, medium at 37 C). The dye-demo value "
          f"{MU_DYE:g} would make every flow below 20% more conservative.\n")

    print("LANE AREA")
    print(f"{'lane':<11}{'fields':>8}{'field cm2':>11}{'lane cm2':>10}{'lane uL':>9}  why this area")
    why = {
        "n": "80-day cortical route, no passage: seeded sparse, 13.2 cm2 gives >1e5 cells for the "
             "destructive arms at any plating density in the published range",
        "g": "needs BOTH 16-40 colonies and in-place maturation: see the colony block below",
        "a": "astrocytes plate ~2x denser, so the 1e5-cell omic floor and the secretome volume are met "
             "in two thirds of the area; the freed board carries the bus corridor",
    }
    for lane in ORDER:
        print(f"{LANES[lane]['name']:<11}{N_FIELDS:>8}{FIELD_W[lane] * FIELD_L / 100:>11.2f}"
              f"{area_cm2(lane):>10.2f}{N_FIELDS * field_volume_ul(lane):>9.0f}  {why[lane]}")
    total = sum(area_cm2(k) for k in ORDER)
    plate = FOOT_X * FOOT_Y / 100
    print(f"{'TOTAL':<11}{3 * N_FIELDS:>8}{'':>11}{total:>10.2f}{'':>9}  {total / plate:.0%} of the "
          f"{plate:.0f} cm2 plate; demo chip was 3 x 0.105 cm2\n")

    print("GLIA LANE COLONY DENSITY (the one lane with two competing published constraints)")
    lo, hi = COLONIES_PER_CM2_PUBLISHED
    per_colony = (COLONY_YIELD_PER_CM2 / hi, COLONY_YIELD_PER_CM2 / lo)
    o2_cap = o2_supported_cells_per_cm2()
    target_density = 50_000.0
    need = (target_density / per_colony[1], target_density / per_colony[0])
    print(f"  published plating                 {lo:g}-{hi:g} colonies/cm2, 16-40 colonies per vessel")
    print(f"  progenitor yield at that density  {COLONY_YIELD_PER_CM2:,.0f}/cm2 = {per_colony[0]:,.0f}-"
          f"{per_colony[1]:,.0f} per colony")
    print(f"  in-place maturation window        {MATURATION_DENSITY[0]:,.0f}-{MATURATION_DENSITY[1]:,.0f} "
          f"cells/cm2 (sparse to dense)")
    print(f"  oxygen cap at this perfusion      {o2_cap:,.0f} cells/cm2  <-- binds first")
    print(f"  so plate at                       {need[0]:.1f}-{need[1]:.1f} colonies/cm2 for "
          f"{target_density:,.0f}/cm2 in place")
    print(f"  which over {area_cm2('g'):.1f} cm2 is          "
          f"{need[0] * area_cm2('g'):.0f}-{need[1] * area_cm2('g'):.0f} colonies, inside the published "
          f"16-40 count\n")
    print("  At the demo's 0.105 cm2 the same sum gives 0.2-1.0 colonies, which is audit finding 3.")
    print(f"  Keeping the published 5-10/cm2 instead would need {COLONY_YIELD_PER_CM2 / target_density * area_cm2('g'):.0f} "
          f"cm2 of maturation area, which no plate holds: that is why the density, not the area, is the lever.\n")

    print("PERFUSION, SHEAR AND VELOCITY (per lane, continuous)")
    print(f"{'lane':<11}{'uL/min':>8}{'mL/day':>8}{'uL/cm2/d':>10}{'CV/day':>8}{'per field':>11}"
          f"{'tau':>10}{'x under':>9}{'U mm/s':>9}{'washout':>9}")
    for lane in ORDER:
        q = lane_flow_ul_min(lane)
        qf = group_flow_ul_min(lane, 2) / len(GROUPS[2])
        tau = shear_dyn_cm2(qf, FIELD_W[lane])
        u = mean_velocity_mm_s(qf, FIELD_W[lane])
        cv = q * 1440 / (N_FIELDS * field_volume_ul(lane))
        print(f"{LANES[lane]['name']:<11}{q:>8.2f}{q * 1440 / 1000:>8.2f}{MEDIUM_PER_CM2_DAY:>10.0f}"
              f"{cv:>8.0f}{qf:>11.3f}{tau:>10.5f}{SHEAR_CEILING / tau:>9.0f}{u:>9.5f}"
              f"{washout_mm_s() / u:>8.0f}x")
    print(f"  tau = 6 mu Q / (w h^2) = 6 mu q L / h^2 with q the flow per unit area: it does NOT contain the")
    print(f"  lane width, so all three lanes sit at the same shear even though they are different sizes.")
    print(f"  Whole lane as ONE 30 mm chamber at 5 uL/min for comparison: "
          f"{shear_dyn_cm2(5.0, 30.0):.4f} dyn/cm2 ({SHEAR_CEILING / shear_dyn_cm2(5.0, 30.0):.0f}x under).")
    print(f"  Flow a {FIELD_W['n']:g} mm field could take at the ceiling: {flow_at_ceiling(FIELD_W['n']):.1f} uL/min "
          f"per field = {flow_at_ceiling(FIELD_W['n']) * N_FIELDS:.0f} uL/min per lane, "
          f"{flow_at_ceiling(FIELD_W['n']) * N_FIELDS / lane_flow_ul_min('n'):.0f}x the design flow. "
          f"SHEAR IS NOT THE BINDING CONSTRAINT AT THIS SCALE; oxygen is.\n")

    print("SEDIMENTATION: WHY THERE IS NO MESH AND NO WEIR")
    print(f"  v_s = dp g d^2 / (18 mu) = {DRHO:g} x {G:g} x ({CELL_UM:g}e-6)^2 / (18 x {MU:g}) = {v_s:.2f} um/s")
    print(f"       (at the denser 1.075 g/mL end of the published range it is "
          f"{settling_um_s() * 75 / DRHO:.1f} um/s; the slower figure is used throughout)")
    print(f"  a cell leaves a field only if U > L v_s / h = {FIELD_L:g} x {v_s:.2f}e-3 / {DEPTH:g} = "
          f"{washout_mm_s():.3f} mm/s")
    qf = group_flow_ul_min("n", 2) / len(GROUPS[2])
    u = mean_velocity_mm_s(qf, FIELD_W["n"])
    print(f"  design point U = {u:.5f} mm/s  ->  margin {washout_mm_s() / u:.0f}x")
    print(f"  settling time h / v_s = {DEPTH * 1000 / v_s:.0f} s against a transit time of "
          f"{FIELD_L / u / 60:.0f} min")
    print(f"  even at the shear ceiling ({flow_at_ceiling(FIELD_W['n']):.1f} uL/min per field) the margin is "
          f"{washout_mm_s() / mean_velocity_mm_s(flow_at_ceiling(FIELD_W['n']), FIELD_W['n']):.1f}x")
    print(f"  SO: the 25 um outlet weir that vents the demo's 10-15 um progenitors is not needed and not "
          f"present. Audit finding 4 is solved by geometry, not by a part.\n")

    print("OXYGEN")
    per_cell_day = O2_AMOL_CELL_S * 86400 * 1e-6
    print(f"  demand      {O2_AMOL_CELL_S:g} amol/cell/s x 86400 = {per_cell_day:.2f} pmol/cell/day")
    print(f"  supply      {MEDIUM_PER_CM2_DAY:g} uL/cm2/day x {O2_NMOL_PER_UL:g} nmol/uL x "
          f"{O2_EXTRACTION:g} extraction = {MEDIUM_PER_CM2_DAY * O2_NMOL_PER_UL * O2_EXTRACTION:.0f} nmol/cm2/day")
    print(f"  so          {o2_supported_cells_per_cm2():,.0f} cells/cm2 supported; lanes are planned at "
          f"{target_density:,.0f}/cm2")
    for lane in ORDER:
        print(f"  {LANES[lane]['name']:<11} lane at {target_density:,.0f}/cm2 = "
              f"{target_density * area_cm2(lane):,.0f} cells needs "
              f"{target_density * area_cm2(lane) * per_cell_day / 1000:.0f} nmol/day; perfusion delivers "
              f"{lane_flow_ul_min(lane) * 1440 * O2_NMOL_PER_UL * O2_EXTRACTION:.0f} nmol/day")
    print(f"  ONE SEALED FIELD holds {field_volume_ul('n') * O2_NMOL_PER_UL:.1f} nmol = "
          f"{sealed_reserve_min('n', target_density):.0f} min for its cells. That is why no step in this "
          f"protocol caps a lane: the demo's 24 h capped holds are ~{24 * 60 / sealed_reserve_min('n', target_density):.0f}x "
          f"longer than the reserve. The longest no-flow interval here is the "
          f"{2 * field_volume_ul('n') / 60:.1f} min quench, {sealed_reserve_min('n', target_density) / (2 * field_volume_ul('n') / 60):.0f}x inside it.")
    print(f"  Gas exchange happens OFF CHIP in a vented reservoir per lane (chip stages[]), because the "
          f"board is deliberately not gas-permeable.\n")

    print("ISOLATION: DIFFUSION OVER THIS CHIP'S OWN BRANCH LENGTHS")
    print(f"  x = 2 sqrt(D t), D = {D_SMALL:g} m2/s (small molecule, the fast case)")
    print(f"  nearest field to the bus          {TO_BUS_MM:.0f} mm -> {diffusion_time_days(TO_BUS_MM):.1f} days")
    print(f"  field to field across the bus     {FIELD_TO_FIELD_MM:.0f} mm -> "
          f"{diffusion_time_days(FIELD_TO_FIELD_MM):.0f} days")
    print(f"  group-1 spoke (corridor route)    ~{BUS_X - JL_X:.0f} mm -> "
          f"{diffusion_time_days(BUS_X - JL_X):.0f} days")
    print(f"  reach in 24 h of stop-flow        {diffusion_reach_mm(DAY):.1f} mm (demo: 8.3 mm in 12 h over "
          f"36 mm branches, re-derived here because the demo's numbers were computed for a 140 x 75 mm board "
          f"and never re-derived after the 0.209 shrink)")
    print(f"  Total stop-flow in the schedule is the {2 * field_volume_ul('n') / 60:.1f} min quench per lane, "
          f"against the {diffusion_time_days(FIELD_TO_FIELD_MM):.0f} days a molecule needs to reach another "
          f"lane's cells. And during all 80 days of differentiation the bus is CAPPED and each lane is a "
          f"closed loop, so there is no advective path at all: this chip's isolation does not depend on "
          f"balancing three flows the way the demo's does.\n")

    print("MULTI-OMIC FIELD ALLOCATION (per lane, same medium, same reservoir, same history)")
    print(f"{'fields':<10}{'cm2':>7}{'cells @50k/cm2':>16}  arm")
    print(f"{'f1-f2':<10}{group_area_cm2('n', 1):>7.2f}{target_density * group_area_cm2('n', 1):>16,.0f}  "
          f"metabolome + proteome: quenched IN PLACE, one cold organic extraction, two fractions. "
          f"n=2 technical duplicate")
    print(f"{'f3-f6':<10}{group_area_cm2('n', 2):>7.2f}{target_density * group_area_cm2('n', 2):>16,.0f}  "
          f"single-cell transcriptome: dissociated by the cell-prep stage, matches the "
          f"400-600k interface at {CELL_CONC:g} cells/uL")
    print(f"{'all six':<10}{area_cm2('n'):>7.2f}{'-':>16}  secretome + exometabolome: repeated draws at "
          f"p_*_sup (group 2) and p_*_ex (group 1)")
    print("  The two groups are separate hydraulic paths, each with its own feed and its own outlet, which")
    print("  is what lets one be quenched while the other keeps growing; they share the lane's reservoir and")
    print("  both sit on the interaction bus, which is what keeps them one sample. chip/module6_v1.json")
    print("  currently seeds its own three lanes from an unnamed source after a 1-day culture, so the")
    print("  repo's 'multi-omic from one sample' claim is false ACROSS arms today; this is the fix.\n")

    print("SEEDING AND FLOW DISTRIBUTION")
    for group in (1, 2):
        x0, x1 = SPINE_X[group]
        q = seed_flow_ul_min(group)
        qf = q / len(GROUPS[group])
        print(f"  group {group}: spine {x1 - x0:.0f} mm, {len(GROUPS[group])} fields. Cells settle out before "
              f"the far field below {q / SEED_MARGIN:.0f} uL/min (U > L v_s / h along the SPINE), so seeding "
              f"runs at {SEED_MARGIN:g}x that, {q:.0f} uL/min; a field then sees "
              f"{shear_dyn_cm2(qf, FIELD_W['n']):.4f} dyn/cm2, "
              f"{SHEAR_CEILING / shear_dyn_cm2(qf, FIELD_W['n']):.1f}x under the ceiling, and the restrictor "
              f"riser is {qf / (washout_mm_s(FEED_RISER_FOLDED) * FEED_RISER_W * DEPTH * 60):.1f}x above its own "
              f"settling limit.")
    r_res, r_span, ratio = restrictor_dominance()
    q_field = group_flow_ul_min("n", 2) / len(GROUPS[2])
    print(f"  Flow distribution: R = 12 mu L / (a b^3 (1 - 0.63 b/a)) gives the {FEED_RISER_W * 1000:g} x "
          f"{DEPTH * 1000:g} um, {FEED_RISER_FOLDED:g} mm restrictor {r_res:.3g} Pa s/m3 against "
          f"{r_span:.3g} for one {FIELD_PITCH - SPINE_H:.1f} mm span of spine: a ratio of {ratio:.0f}.")
    print(f"  The worst-case path difference across {len(GROUPS[2])} fields is "
          f"{(len(GROUPS[2]) - 1) * 2 * r_span / r_res:.0%} of a field's own resistance, so the split is even "
          f"to within that; trimming the riser lengths per field closes the rest.")
    print(f"  Operating pressure at the design flow: {r_res * q_field * 1e-9 / 60:.1f} Pa across a restrictor "
          f"({r_res * q_field * 1e-9 / 60 / 100:.4f} mbar). That is far below what a pressure controller can "
          f"hold, so this board is driven by SYRINGE PUMPS, exactly as the demo review concluded.\n")

    chip = build_chip()
    proto = build_protocol(chip)
    print(f"CHIP      {len(chip['chambers'])} chambers, {len(chip['channels'])} channels, "
          f"{len(chip['ports'])} ports, {len(chip['stages'])} off-chip stages, "
          f"{sum(c['volume_ul'] for c in chip['chambers']):.0f} uL wetted")
    print(f"PROTOCOL  {len(proto['steps'])} steps, "
          f"{sum(s['duration_s'] for s in proto['steps']) / DAY:.0f} player-days "
          f"({ENDPOINT_DAY} calendar days; three lanes run concurrently)")
    medium = sum((s["volume_ul"] or 0) * (s["washes"] or 1) for s in proto["steps"]
                 if s["operation"] == "exchange")
    print(f"MEDIUM    {medium / 1000:.1f} mL through the three loops over {ENDPOINT_DAY} days "
          f"({medium / 1000 / 3:.1f} mL per lane). contracts/parameters.json reservoir.default_ul is "
          f"{parameter('reservoir.default_ul'):g} uL and waste.capacity_ul is "
          f"{parameter('waste.capacity_ul'):g} uL: both are DEMO-BENCH numbers and a target-scale run needs "
          f"litre-scale reservoirs. Flagged, not edited: contracts/parameters.json is not this agent's file.")


def main() -> None:
    chip = build_chip()
    proto = build_protocol(chip)
    if "--report" in sys.argv:
        report()
    if "--dry-run" not in sys.argv:
        CHIP_OUT.write_text(json.dumps(chip, indent=2) + "\n")
        PROTO_OUT.write_text(json.dumps(proto, indent=2) + "\n")
        print(f"\nwrote {CHIP_OUT.relative_to(ROOT)}: {len(chip['chambers'])} chambers, "
              f"{len(chip['channels'])} channels, {len(chip['ports'])} ports")
        print(f"wrote {PROTO_OUT.relative_to(ROOT)}: {len(proto['steps'])} steps")


if __name__ == "__main__":
    main()

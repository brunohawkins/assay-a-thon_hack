"""Build protocols/module6_v1.json.

    python protocols/build_module6.py           # write the protocol
    python protocols/build_module6.py --report  # lane volumes, flows, shear, Reynolds, timings

protocols/module6_v1.json was the last live protocol in the repo with NO builder. It was
hand-maintained, and it drifted: loads of 11.444 uL into a 2.88 uL chamber (3.97x), a
pre-quench rinse held to the CULTURE shear ceiling (712 s of ice-cold rinse before a 1 s
quench), six collect steps with duration_s = 0, and a comb volume that existed only as
prose in the chip description. Nothing numeric is typed in here any more:

  volumes   from chip/module6_v1.json geometry (chamber size, channel path/width/depth,
            port width_um, lid layer thickness) plus the fabrication floors in
            contracts/blocks.json, which set the comb tooth width and rib.
  flows     from the chamber cross-section and the ceiling that belongs to the STEP:
            shear.culture_ceiling_dyn_cm2 while the cells are alive and being grown,
            shear.prelysis_rinse_ceiling_dyn_cm2 for the rinse that immediately precedes
            the quench, and no shear ceiling at all after the cells are dead.
  durations volume / flow, or a thermal ramp from thermal.max_ramp_c_per_s and thermal.tau_s.

THREE FLUIDS, THREE VISCOSITIES, AND THE CONTRACT NOW DISTINGUISHES TWO OF THEM.
contracts/parameters.json carries fluid.viscosity_37c_pa_s (culture medium at 37 C, 0.8 mPa s)
AND fluid.viscosity_pa_s (water at 20 C, 1.0 mPa s, explicitly the dye-demo value). They are
not interchangeable and this script uses each where it belongs: 37 C steps take the 37 C
medium key directly, the ice-cold aqueous rinse takes the water key carried to 4 C by the
textbook water ratio, and the cold organic quench solvent - which is neither, and has no
contract value - is a flagged ASSUMPTION used only for flood pressure and Reynolds number,
never for a shear ceiling. No viscosity is copied as a bare literal: that is how
build_module1.py's mu drifted 20% below contract. See the table at MU, below.

WHY THE LANE, NOT THE CHAMBER, IS THE VOLUME UNIT. The field is in SERIES with an inlet
comb, an outlet comb and two port stubs. One "chamber volume" does not displace the lane,
so every exchange here is a multiple of the LANE WETTED VOLUME. The load is the exception
and is now capped at exactly one field volume: see LOAD, below.

WHAT EVERY EXCHANGE IS FOR (exchange_kind), BECAUSE ABSENCE MEANS SWEEP. Each of the twelve
exchanges declares its job, and the job sets the volume rule. Nothing is left undeclared:
add() raises if an exchange arrives without a kind, or if a non-exchange carries one.

  fill    3  the PRIME of each lane's upstream dead volume. The lane is dry before seeding,
             so there is nothing to clear. A fill is capped ABOVE at one chamber volume, and
             the dead volume is 1.45 of one, so it is written as 2 washes of half of it; in
             the instrument it is one continuous push with no drain in between.
  sweep   9  the pre-quench RINSE (its job is to clear medium so it cannot contaminate the
             extract), the QUENCH FLOOD (its job is displacement: solvent everywhere a cell
             sits) and the solubilisation FLUSH (it decides fraction-to-fraction carry-over).
             All three are sized from contracts exchange.target_fraction.
  partial 6  the four elution CHASES and nothing else. A chase takes a DEFINED fraction - the
             field plus everything downstream of it - because the extract volume is the whole
             lever for metabolomics here. Sweeping one to target would dilute the fraction
             about 2.5x and break the <=10 uL rule, so taking a fraction IS the point. Each
             says how many chamber volumes it replaces, which is what a partial owes.

A SWEEP ON THIS CHIP COSTS MORE THAN THE CONTRACT FLOOR. -ln(1 - target) chamber volumes
assumes clean reagent reaches the chamber from the first microlitre. Here the inlet stub and
comb hold the SAME old liquid, so nothing clean arrives until that dead volume has passed.
Every sweep is therefore sized as dead volume + the contract's chamber volumes, which is
above the floor the test enforces, not at it.

LOAD. Steps 1-3 of the old file pushed one whole lane wetted volume of cell suspension and
declared it as a load into a 2.88 uL chamber. Both halves of that were wrong. Reconciled by
CAPPING THE LOAD AT WHAT THE CHAMBER HOLDS and declaring the upstream dead volume as its
own step:

  prime   inlet stub + inlet comb. This liquid never reaches the field; it is the dead
          volume the suspension has to cross first, and it is now named and costed.
  load    exactly one field volume, which is the suspension that ends up on the floor
          where the cells are wanted.

Total pushed is 7.07 uL per lane, not 11.44, because the OUTLET comb is deliberately left
unfilled with cell suspension: stopping the front at the far field edge is the only way the
"cells must not settle in the comb" risk is addressed by the protocol rather than hoped
away by an adhesion coat. The comb volume is no longer prose: it is derived here from the
channel path, width and depth in the chip spec plus the tooth band, whose tooth width
(min_open_channel_mm), rib (min_wall_mm) and length (spine edge to field edge) all come
from data. The chip_spec schema has no volume field for a channel, so the derivation lives
in this script and its result is written into the chip's notes.

THE RINSE. contracts/parameters.json has carried shear.prelysis_rinse_ceiling_dyn_cm2 = 0.3
specifically to stop a pre-quench rinse being held to the 0.01 culture ceiling, and nothing
read it. This script does. The rinse is 30x faster and takes about 23 s instead of 712 s.
The biology points the same way and the repo had it backwards: Kapoore 2017 found that
WASHING ITSELF causes metabolite leakage, so it is an argument for the SHORTEST defensible
rinse, not for a careful slow one. Lorenz 2011 is the paper that says a rinse is needed at
all (a quick rinse roughly doubles detected features). Fast and short satisfies both.

ONE THERMAL STAGE, SO THE PROTOCOL IS PHASE-ORDERED. The chip declares one thermal zone
over all three fields, because three independently controlled -20/+37 C stages on 10 mm
centres through a 0.5 mm COC base is not a thing we can build. One stage has a consequence
the old lane-by-lane order broke: it cannot hold lane N at -20 C while lanes G and A are
still alive at 37 C. So the steps run phase by phase (seed, culture, rinse+flood, soak,
fraction 1, flush, resolubilise, fraction 2) and every lane crosses each setpoint together.
Two ordering rules fall out of that and are enforced by the order below:

  - the stage stays at 4 C through the quench flood, and only goes to -20 C once every lane
    holds organic solvent. Ramping to -20 C with aqueous rinse buffer in a lane would FREEZE
    it, which is the slow freeze-quench the comb geometry exists to avoid.
  - the aqueous solubilisation buffer is not pushed into a -20 C lane either, so the stage
    warms to ambient BEFORE the flush.

Reagent names are generic and nothing here is a recipe. Solvents are named by class only.
See CONFIDENTIALITY.md.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent / "module6_v1.json"
CHIP = ROOT / "chip" / "module6_v1.json"
CONTRACT = ROOT / "contracts" / "parameters.json"
BLOCKS = ROOT / "contracts" / "blocks.json"

SOURCE = (
    "Chip geometry, volumes, flows and durations are ours, derived from chip/module6_v1.json "
    "and contracts/. ONE EXTRACTION, TWO FRACTIONS: Coman et al. 2016 (SIMPLEX), "
    "doi:10.1074/mcp.M115.053702, and Sapcariu et al. 2014, doi:10.1016/j.mex.2014.07.002 - "
    "cited for the PRINCIPLE (metabolites in the supernatant, protein in the pellet) and for "
    "the >=5 min cold soak, NOT for depth or CV figures: Sapcariu is a chlorinated-solvent "
    "(Folch-type) biphasic partition and SIMPLEX an ether-type one, while this chip runs a single-phase "
    "acidified polar-organic precipitation with no partition solvent at all, so quantitative "
    "transfer from either is an ASSUMPTION. EXTRACTION ON ADHERENT CELLS: Dettmer et al. 2011, "
    "doi:10.1007/s00216-010-4425-x (direct cold solvent on adherent cells beat scrape-in-buffer "
    "7x). THE RINSE: Lorenz et al. 2011, doi:10.1021/ac103313x (a quick rinse roughly doubles "
    "detected features) says rinse; Kapoore et al. 2017, doi:10.1039/C7AN00207F (washing causes "
    "metabolite leakage) says rinse as briefly as possible. This repo previously cited Kapoore "
    "in favour of a careful slow wash, which is backwards. PROTEOMIC INPUT: Mueller et al. 2020, "
    "doi:10.15252/msb.20199111 (1,000 cells give 500-1,000 proteins)."
)


# --- contracts ---------------------------------------------------------------------

def parameter(name: str) -> float:
    """One value from contracts/parameters.json. No fallback: a missing contract is a bug."""
    return float(json.loads(CONTRACT.read_text())["parameters"][name]["value"])


chip = json.loads(CHIP.read_text())
floors = json.loads(BLOCKS.read_text())["fabrication_floors"]

MU_WATER_20C = parameter("fluid.viscosity_pa_s")      # water at 20 C: the DYE DEMO value
MU_MEDIUM_37C = parameter("fluid.viscosity_37c_pa_s")  # culture medium at 37 C: protocol flows
CULTURE_CEILING = parameter("shear.culture_ceiling_dyn_cm2")
RINSE_CEILING = parameter("shear.prelysis_rinse_ceiling_dyn_cm2")
AMBIENT_C = parameter("thermal.ambient_c")
RAMP_C_PER_S = parameter("thermal.max_ramp_c_per_s")
TAU_S = parameter("thermal.tau_s")
CULTURE_TEMP_C = parameter("culture.temp_c")
DIFFUSIVITY_M2_S = parameter("dye.diffusivity_m2_s")           # small molecule in water at ~20 C
CAPSULE_D_UM = parameter("capsule.diameter_um")

MIN_OPEN_MM = floors["min_open_channel_mm"]                    # comb tooth width
MIN_WALL_MM = floors["min_wall_mm"]                            # rib between teeth

# --- which viscosity each step runs at, and why ------------------------------------------
# contracts/parameters.json now carries TWO water-like viscosities and they are not
# interchangeable. This chip needs a THIRD fluid that is neither, so each step says which:
#
#   culture / prime / load   fluid.viscosity_37c_pa_s. Culture medium at 37 C, which is what
#                            these steps actually push. Read straight from the contract: no
#                            ratio, no literal. Using the 20 C water value here would make
#                            every culture flow 20% too conservative.
#   pre-quench rinse         an AQUEOUS volatile buffer on ice, so water IS the right base:
#                            fluid.viscosity_pa_s (explicitly water, at 20 C) scaled by the
#                            textbook water viscosity ratio 4 C / 20 C. Not the 37 C medium
#                            key, because nothing here is at 37 C and nothing is medium.
#   quench / chase / flush   NEITHER. A cold acidified polar organic at -20 C is not water at
#                            any temperature, and the repo has no contract value for it, so
#                            it is an ASSUMPTION, written as a multiple of water at 20 C and
#                            flagged. It only sets the flood pressure drop and Reynolds
#                            number - no shear ceiling applies after the cells are dead - so
#                            a wrong factor here cannot hurt a living cell. Measure it.
WATER_4C_OVER_20C = 1.5674 / 1.0016   # textbook water viscosity ratio
SOLVENT_OVER_WATER_20C = 2.5          # ASSUMPTION: cold acidified polar organic at -20 C
SOLVENT_DENSITY_KG_M3 = 850.0        # ASSUMPTION: cold polar organic, class figure, for Re only
VALVE_STUB_UL = 0.5                  # ASSUMPTION: off-chip selector dead volume per port
VALVE_SWITCH_S = 1.0                 # ASSUMPTION: rotary selector actuation
VIAL_HANDLING_S = 20.0               # ASSUMPTION: cap the pre-charged vial, move it to the cold block
FEED_LINE_BORE_MM = 0.5              # ASSUMPTION: the line from the selector to the port
FEED_LINE_LENGTH_MM = 100.0          # ASSUMPTION
TOOTH_PEAK_FACTOR = 2.02             # resistor-network model of this lane: hottest tooth / mean
HEADROOM = 0.85                      # 15% under whichever shear ceiling applies
CELL_DIAMETER_UM = 12.0              # ASSUMPTION, as build_module1.py
CELLS_PER_MM2 = 1000.0               # ASSUMPTION: dense monolayer

# --- what each exchange is FOR (exchange_kind), and the volume rule that follows ---------
# contracts/schemas/protocol.schema.json makes the job DATA instead of English in a note:
#
#   fill      the first filling of an empty or air-filled chamber. Bounded ABOVE by the
#             chamber volume, no lower bound. It is not clearing anything.
#   sweep     clearing the chamber's contents. Must move at least one chamber volume, and
#             washes x volume must total SWEEP_CV chamber volumes, because a well-mixed
#             chamber keeps exp(-V) of the old liquid after V chamber volumes.
#   partial   a deliberate fractional change. Escapes the sweep floor BECAUSE it is
#             deliberate, so its note has to say what fraction it replaces.
#
# ABSENCE MEANS SWEEP, so every exchange here declares itself. SWEEP_CV is derived from
# contracts exchange.target_fraction, never stated.
EXCHANGE_TARGET = parameter("exchange.target_fraction")
SWEEP_CV = -math.log(1.0 - EXCHANGE_TARGET)

FLOOD_LV = 2.0            # lane wetted volumes of quench: 1 to displace, 1 to be sure
FLUSH_LV = 3.0            # lane wetted volumes of solubilisation buffer, exp(-3) = 5% left
QUENCH_TARGET_S = 1.0     # the design requirement the comb geometry exists to meet
SOAK_S = 300.0            # published floor for a cold sequential extraction: >=5 min at -20 C
SOAK_TEMP_C = -20.0
RINSE_TEMP_C = 4.0
RESOLUBILISE_TEMP_C = AMBIENT_C   # NOT 37 C: see RESOLUBILISATION in build()
RESOLUBILISE_S = 600.0            # PLACEHOLDER to fit
ATTACH_S = 14400.0                # PLACEHOLDER to fit
CULTURE_S = 86400.0               # PLACEHOLDER to fit
CULTURE_INTERVAL_S = 21600.0      # PLACEHOLDER to fit

LANES = {
    "n": "neurons",
    "g": "glia",
    "a": "astrocytes",
}
PORTS = [p["id"] for p in chip["ports"]]
CHAMBERS = {c["id"]: c for c in chip["chambers"]}
CHANNELS = {c["id"]: c for c in chip["channels"]}
CHIP_PORTS = {p["id"]: p for p in chip["ports"]}
FIELDS = [f"cx_{k}" for k in LANES]


# --- geometry: every volume in this protocol comes from here ---------------------------

def lid_thickness_mm() -> float:
    """Material above the fluidic layer, which is what a port hole is drilled through."""
    depth = max([c["size"][2] for c in chip["chambers"]] + [c["depth_mm"] for c in chip["channels"]])
    layers = chip["layers"]
    fluidic = next(i for i, la in enumerate(layers) if abs(la["thickness_mm"] - depth) < 1e-9)
    return sum(la["thickness_mm"] for la in layers[fluidic + 1:])


def stub_ul(port_id: str) -> float:
    """Port hole through the lid, plus the off-chip valve stub behind it."""
    d_mm = CHIP_PORTS[port_id]["width_um"] / 1000.0
    return math.pi / 4.0 * d_mm * d_mm * lid_thickness_mm() + VALVE_STUB_UL


def path_length_mm(channel: dict) -> float:
    return sum(math.dist(a, b) for a, b in zip(channel["path"][:-1], channel["path"][1:]))


def tooth_count(field: dict) -> int:
    """Teeth that fit along the field's long edge at the one pitch the floors allow."""
    return int(field["size"][1] // (MIN_OPEN_MM + MIN_WALL_MM))


def tooth_length_mm(channel: dict, field: dict) -> float:
    """Spine edge to field edge: the gap the teeth have to bridge, from the two x positions."""
    spine_x = channel["path"][0][0]
    half_spine, half_field = channel["width_mm"] / 2.0, field["size"][0] / 2.0
    gap = abs(spine_x - field["center"][0]) - half_spine - half_field
    if gap <= 0:
        raise SystemExit(f"channel '{channel['id']}' overlaps field '{field['id']}': no room for teeth")
    return gap


def comb_ul(channel_id: str, field_id: str) -> float:
    """A centipede_channel's volume: spine plus tooth band. The schema has no channel
    volume_ul field, so this is where the comb volume lives."""
    ch, field = CHANNELS[channel_id], CHAMBERS[field_id]
    spine = path_length_mm(ch) * ch["width_mm"] * ch["depth_mm"]
    teeth = tooth_count(field) * MIN_OPEN_MM * tooth_length_mm(ch, field) * ch["depth_mm"]
    return spine + teeth


def lane(key: str) -> dict:
    """Every volume of one lane, in uL, split by where the liquid actually goes.

    Rounded here, once, to the precision the protocol declares: a duration derived from a
    volume the file does not state is a duration the file cannot be checked against."""
    field_id = f"cx_{key}"
    field = round(CHAMBERS[field_id]["volume_ul"], 3)
    up = round(stub_ul(f"in_{key}") + comb_ul(f"comb_in_{key}", field_id), 3)
    down = round(comb_ul(f"comb_out_{key}", field_id) + stub_ul(f"out_{key}"), 3)
    return {
        "field": field,
        "upstream": up,            # dead volume the load has to cross first
        "downstream": down,        # dead volume between the cells and the vial
        "wetted": round(up + field + down, 3),
        "extract": round(field + down, 3),   # what a chase pushes into the vial
    }


def floor_area_mm2() -> float:
    f = CHAMBERS["cx_n"]
    return f["size"][0] * f["size"][1]


CELLS_PER_LANE = floor_area_mm2() * CELLS_PER_MM2
LANE = {k: lane(k) for k in LANES}
WETTED = LANE["n"]["wetted"]        # the three lanes are identical by construction
EXTRACT = LANE["n"]["extract"]
FIELD_UL = LANE["n"]["field"]
assert all(abs(v["wetted"] - WETTED) < 1e-9 for v in LANE.values()), "lanes differ: size each one separately"

# A SWEEP ON THIS CHIP COSTS MORE THAN THE CONTRACT FLOOR, AND THE DIFFERENCE IS THE DEAD
# VOLUME. The contract rule is SWEEP_CV chamber volumes (SWEEP_FLOOR_UL below), which assumes
# clean reagent arrives at the chamber from the first microlitre. Here the first
# LANE['upstream'] uL only displaces the inlet stub and comb, which hold the SAME old liquid,
# so nothing clean reaches the field until that has passed. The honest sweep is therefore the
# upstream dead volume PLUS the contract's chamber volumes, and that is what this script
# sizes rinses, floods and flushes from. It is deliberately above the test floor, not at it.
SWEEP_FLOOR_UL = round(SWEEP_CV * FIELD_UL, 3)                      # what the contract asks
SWEEP_UL = round(LANE["n"]["upstream"] + SWEEP_CV * FIELD_UL, 3)    # what the lane really needs


# --- flow, shear and the regime the flood really runs in --------------------------------

MU = {
    "culture": MU_MEDIUM_37C,
    "rinse": MU_WATER_20C * WATER_4C_OVER_20C,
    "solvent": MU_WATER_20C * SOLVENT_OVER_WATER_20C,
}
MU_WHY = {
    "culture": "contracts fluid.viscosity_37c_pa_s (culture medium at 37 C)",
    "rinse": "contracts fluid.viscosity_pa_s (water, 20 C) x textbook 4 C/20 C water ratio",
    "solvent": "ASSUMPTION: cold acidified polar organic, x water at 20 C",
}


def mu(kind: str) -> float:
    return MU[kind]


def max_flow(ceiling: float, kind: str, field: dict) -> float:
    """Highest flow (uL/min) keeping wall shear on the field floor at or below the ceiling.
    tau = 6 mu Q / (w h^2); flow crosses the SHORT in-plane dimension, so w is the long one."""
    w, h = field["size"][1] * 1e-3, field["size"][2] * 1e-3
    return (ceiling / 10.0) * w * h * h / (6.0 * mu(kind)) * 60.0 * 1e9


def shear(flow_ul_min: float, kind: str, field: dict) -> float:
    q = flow_ul_min * 1e-9 / 60.0
    w, h = field["size"][1] * 1e-3, field["size"][2] * 1e-3
    return 10.0 * 6.0 * mu(kind) * q / (w * h * h)


def held_flow(ceiling: float, kind: str) -> float:
    """Flow with the HOTTEST TOOTH REGION, not the mean, held under the ceiling."""
    f = CHAMBERS["cx_n"]
    flow = max_flow(ceiling, kind, f) * HEADROOM / TOOTH_PEAK_FACTOR
    if flow <= 0:
        raise SystemExit("field geometry leaves no flow under the shear ceiling")
    return math.floor(flow * 100) / 100


CULTURE_FLOW = held_flow(CULTURE_CEILING, "culture")
RINSE_FLOW = held_flow(RINSE_CEILING, "rinse")
FLOOD_FLOW = round(FLOOD_LV * WETTED / QUENCH_TARGET_S * 60.0, 1)   # no ceiling: cells are dead


def reynolds(area_mm2: float, perimeter_mm: float, flow_ul_min: float) -> float:
    """Re = rho U Dh / mu for a duct, at the flood flow."""
    q = flow_ul_min * 1e-9 / 60.0
    area, d_h = area_mm2 * 1e-6, 4.0 * area_mm2 / perimeter_mm * 1e-3
    return SOLVENT_DENSITY_KG_M3 * (q / area) * d_h / mu("solvent")


def rect_re(w_mm: float, h_mm: float, flow_ul_min: float) -> float:
    return reynolds(w_mm * h_mm, 2.0 * (w_mm + h_mm), flow_ul_min)


def regime() -> dict:
    """The flood's Reynolds numbers and the two diffusion times that bound the quench."""
    field, spine = CHAMBERS["cx_n"], CHANNELS["comb_in_n"]
    teeth = tooth_count(field)
    bore = FEED_LINE_BORE_MM
    u_field = (FLOOD_FLOW * 1e-9 / 60.0) / (field["size"][1] * field["size"][2] * 1e-6)
    # Stokes-Einstein: D goes as T / mu, so the contract's room-temperature small-molecule
    # D has to be carried to the cold, more viscous solvent before it means anything here.
    d_cold = DIFFUSIVITY_M2_S * ((SOAK_TEMP_C + 273.15) / 293.15) * (MU_WATER_20C / mu("solvent"))
    rib_mm = MIN_WALL_MM
    return {
        "re_line": reynolds(math.pi / 4 * bore * bore, math.pi * bore, FLOOD_FLOW),
        "re_spine": rect_re(spine["width_mm"], spine["depth_mm"], FLOOD_FLOW),
        "re_tooth": rect_re(MIN_OPEN_MM, spine["depth_mm"], FLOOD_FLOW / teeth),
        "re_field": rect_re(field["size"][1], field["size"][2], FLOOD_FLOW),
        "sweep_s": (field["size"][0] * 1e-3) / u_field,
        "cell_diffusion_s": (CELL_DIAMETER_UM * 1e-6) ** 2 / (2.0 * d_cold),
        "cell_diffusion_warm_s": (CELL_DIAMETER_UM * 1e-6) ** 2 / (2.0 * DIFFUSIVITY_M2_S),
        "dead_zone_s": [(x * 1e-3) ** 2 / (2.0 * d_cold) for x in (rib_mm * 0.75, rib_mm * 1.5)],
        "entrance_mm": 0.06 * rect_re(spine["width_mm"], spine["depth_mm"], FLOOD_FLOW)
        * (4.0 * spine["width_mm"] * spine["depth_mm"] / (2.0 * (spine["width_mm"] + spine["depth_mm"]))),
        "lane_kpa": 1.37e10 * (FLOOD_FLOW * 1e-9 / 60.0) / 1000.0,
        "line_kpa": (128.0 * mu("solvent") * FEED_LINE_LENGTH_MM * 1e-3
                     / (math.pi * (bore * 1e-3) ** 4)) * (FLOOD_FLOW * 1e-9 / 60.0) / 1000.0,
    }


RE = regime()


def gelatin_ratio() -> tuple[float, float, float]:
    """Capsule gelatin against the protein of the cell inside it, for a CORE-SHELL capsule.

    The old 30-45x was a SOLID sphere of gelatin. These capsules have a liquid core, so only
    the shell is gelatin. Returns (shell volume fraction, low ratio, high ratio)."""
    shell_um = 3.0                   # ASSUMPTION: a few microns, unmeasured
    r = CAPSULE_D_UM / 2.0
    frac = 1.0 - ((r - shell_um) / r) ** 3
    solid_ng, cell_ng = 9.0, (0.2, 0.3)
    return frac, frac * solid_ng / cell_ng[1], frac * solid_ng / cell_ng[0]


SHELL_FRACTION, GEL_LOW, GEL_HIGH = gelatin_ratio()


# --- durations --------------------------------------------------------------------------

def push_s(volume_ul: float, flow_ul_min: float) -> float:
    """Rounded UP, so the declared duration is never shorter than the declared volume needs."""
    return math.ceil(volume_ul / flow_ul_min * 6000.0) / 100.0


def settle_s(from_c: float, to_c: float) -> float:
    """Ramp at the contract rate plus three time constants to settle: a hold that only
    covers the setpoint has not reached it."""
    return round(abs(to_c - from_c) / RAMP_C_PER_S + 3.0 * TAU_S, 1)


def collect_s() -> float:
    """A physical fraction collection: switch to the vial, clear the outlet stub into it,
    switch away, cap it and get it onto the cold block. The old file said 0.0 s."""
    return round(2.0 * VALVE_SWITCH_S + push_s(stub_ul("out_n"), FLOOD_FLOW) + VIAL_HANDLING_S, 2)


# --- build ------------------------------------------------------------------------------

def ports(feed: list[str], vent: list[str]) -> dict:
    """Port state for a step; every port not fed or vented is capped shut."""
    return {"feed": feed, "vent": vent, "cap": [p for p in PORTS if p not in feed + vent]}


def build() -> dict:
    steps: list[dict] = []

    def add(operation, note, duration_s, target=None, targets=None, reagent=None, reagents=None,
            volume_ul=None, temp_c=None, washes=None, flow_ul_min=None, waste=None,
            interval_s=None, port_state=None, kind=None):
        step = {"step": len(steps) + 1, "operation": operation}
        if targets is not None:
            step["targets"] = targets
        else:
            step["target"] = target
        step.update({"reagent": reagent, "volume_ul": volume_ul, "duration_s": duration_s,
                     "temp_c": temp_c, "washes": washes, "flow_ul_min": flow_ul_min})
        if kind is not None:
            if operation != "exchange":
                raise SystemExit(f"exchange_kind on a {operation} step: the schema forbids it")
            step["exchange_kind"] = kind
        elif operation == "exchange":
            raise SystemExit("every exchange must declare its exchange_kind: absence means sweep")
        if reagents is not None:
            step["reagents"] = reagents
        if interval_s is not None:
            step["exchange_interval_s"] = interval_s
        if waste:
            step["waste"] = waste
        step["note"] = note
        step["ports"] = port_state
        steps.append(step)

    def lane_ports(key: str) -> dict:
        return ports([f"in_{key}"], [f"out_{key}"])

    # --- PHASE 0: seed, stage at 37 C -----------------------------------------------
    prime_washes = math.ceil(LANE["n"]["upstream"] / FIELD_UL)
    prime_per_wash = round(LANE["n"]["upstream"] / prime_washes, 3)
    for key, name in LANES.items():
        L = LANE[key]
        add("exchange",
            f"{name.capitalize()}: PRIME the lane. A FILL, not a sweep: the lane is dry before seeding, "
            f"so there is nothing to clear, and {L['upstream']:.3f} uL of cell suspension simply fills "
            f"the inlet stub and the inlet comb - the dead volume UPSTREAM of the field, which the "
            f"suspension has to cross before a single cell reaches the floor. It is declared here "
            f"instead of being hidden inside the load, which is how the old file came to claim it was "
            f"pushing 3.97 chamber volumes into a chamber. Declared as {prime_washes} x "
            f"{prime_per_wash:g} uL because the contract defines an exchange's volume_ul as the volume "
            f"PER WASH and caps a FILL at one chamber volume ({FIELD_UL:g} uL), while the dead volume is "
            f"{L['upstream'] / FIELD_UL:.2f} of one; in the instrument it is a single continuous push and "
            f"nothing drains in between. Held to the {CULTURE_CEILING:g} dyn/cm2 culture ceiling: these "
            f"cells have to survive.",
            push_s(prime_per_wash * prime_washes, CULTURE_FLOW), target=f"cx_{key}",
            reagent=f"cell_suspension_{key}", volume_ul=prime_per_wash, temp_c=CULTURE_TEMP_C,
            washes=prime_washes, flow_ul_min=CULTURE_FLOW, waste=f"out_{key}",
            port_state=lane_ports(key), kind="fill")
        add("load",
            f"{name.capitalize()}: LOAD exactly one field volume ({L['field']:g} uL) of cell suspension, "
            f"then stop flow so the cells settle onto the coated floor. One field volume is the cap: the "
            f"suspension front stops at the far field edge and the OUTLET comb is never filled with cells, "
            f"so cells stranded in the outlet comb are prevented by the volume rather than hoped away by "
            f"the field-only adhesion coat (which stays untested). Total suspension spent per lane is "
            f"{L['upstream'] + L['field']:.3f} uL, down from one whole wetted volume.",
            push_s(L["field"], CULTURE_FLOW), target=f"cx_{key}", reagent=f"cell_suspension_{key}",
            volume_ul=L["field"], temp_c=CULTURE_TEMP_C, flow_ul_min=CULTURE_FLOW,
            port_state=lane_ports(key))

    add("hold_temp",
        f"Stop-flow attachment, every port capped, the one thermal stage at {CULTURE_TEMP_C:g} C under all "
        f"three fields. PLACEHOLDER duration.",
        ATTACH_S, target="cx_n", temp_c=CULTURE_TEMP_C, port_state=ports([], []))

    # --- PHASE 1: culture, stage at 37 C --------------------------------------------
    add("culture",
        f"All three lanes culture in parallel, each through its own port pair: one lane wetted volume "
        f"({WETTED:.3f} uL) of fresh medium every interval at the culture-ceiling flow, stop-flow in "
        f"between. A lane volume, not a field volume, because the comb and the stubs are in series with "
        f"the field and hold {WETTED - LANE['n']['field']:.3f} uL of the medium. Duration and interval are "
        f"PLACEHOLDERS. This chip is the extraction unit cell, not a culture chip: at target scale the "
        f"cells arrive from the module 1 lanes.",
        CULTURE_S, targets=FIELDS, reagents=["culture_medium"] * len(FIELDS), volume_ul=round(WETTED, 3),
        temp_c=CULTURE_TEMP_C, flow_ul_min=CULTURE_FLOW, interval_s=CULTURE_INTERVAL_S,
        port_state=ports([f"in_{k}" for k in LANES], [f"out_{k}" for k in LANES]))

    # --- PHASE 2: rinse and flood, stage at 4 C -------------------------------------
    add("hold_temp",
        f"Cool the one stage from {CULTURE_TEMP_C:g} C to {RINSE_TEMP_C:g} C, every port capped. Derived from "
        f"thermal.max_ramp_c_per_s and three thermal.tau_s, not guessed. The stage STAYS at "
        f"{RINSE_TEMP_C:g} C through the quench floods below: taking it to {SOAK_TEMP_C:g} C now would "
        f"freeze the aqueous rinse buffer in the lanes, which is exactly the slow freeze-quench the comb "
        f"geometry exists to avoid.",
        settle_s(CULTURE_TEMP_C, RINSE_TEMP_C), target="cx_n", temp_c=RINSE_TEMP_C,
        port_state=ports([], []))

    wrong_flow = held_flow(CULTURE_CEILING, "rinse")      # what the old file used, for the contrast
    rinse_ul = SWEEP_UL
    for key, name in LANES.items():
        add("exchange",
            f"{name.capitalize()}: pre-quench rinse. A SWEEP - its whole job is to clear the medium so it "
            f"does not contaminate the extract - so it is sized from contracts exchange.target_fraction "
            f"and not from habit: {SWEEP_CV:.2f} chamber volumes reaches {EXCHANGE_TARGET:.0%} in a "
            f"well-mixed chamber ({SWEEP_FLOOR_UL:g} uL), and the lane's {LANE[key]['upstream']:.3f} uL of "
            f"upstream dead volume holds the SAME medium, so nothing clean reaches the field until that "
            f"has passed. {rinse_ul:g} uL total, about {push_s(rinse_ul, RINSE_FLOW):.0f} s - shorter than "
            f"the {FLOOD_LV:g} lane volumes this step used to push, because the target, not a round number "
            f"of lane volumes, now sets it. Held to "
            f"shear.prelysis_rinse_ceiling_dyn_cm2 ({RINSE_CEILING:g} dyn/cm2), NOT to the "
            f"{CULTURE_CEILING:g} culture ceiling: that ceiling exists to keep adherent cells attached for "
            f"weeks, and against it the same rinse runs at {wrong_flow:g} uL/min and takes "
            f"{push_s(rinse_ul, wrong_flow):.0f} s - about "
            f"{push_s(rinse_ul, wrong_flow) / 60:.0f} minutes of cold perturbation before a quench "
            f"engineered to take about {QUENCH_TARGET_S:g} s (the old file's larger rinse took 700 s "
            f"there). These cells are about to be deliberately killed; losing a few to shear costs "
            f"nothing, a ten-minute perturbation costs the measurement. SHORT IS ALSO THE BIOLOGY: "
            f"Kapoore 2017 found washing "
            f"itself leaks metabolites, so the rinse should be as brief as it can be while still clearing "
            f"the medium (Lorenz 2011: a quick rinse roughly doubles detected features).",
            push_s(rinse_ul, RINSE_FLOW), target=f"cx_{key}", reagent="cold_volatile_rinse",
            volume_ul=round(rinse_ul, 3), temp_c=RINSE_TEMP_C, washes=1, flow_ul_min=RINSE_FLOW,
            waste=f"out_{key}", port_state=lane_ports(key), kind="sweep")
        add("exchange",
            f"{name.capitalize()}: QUENCH FLOOD in place. ALSO A SWEEP, and deliberately classified as "
            f"one: its purpose is displacement - replacing medium-free rinse buffer with solvent "
            f"everywhere a cell sits - so it is held to the same {EXCHANGE_TARGET:.0%} target as the "
            f"rinse. It costs nothing to honour: at {FLOOD_FLOW:g} uL/min the "
            f"{SWEEP_UL:g} uL the target needs would take {push_s(SWEEP_UL, FLOOD_FLOW):.2f} s, so the "
            f"step stays at {FLOOD_LV:g} lane wetted volumes ({FLOOD_LV * WETTED:.3f} uL = "
            f"{FLOOD_LV * WETTED / FIELD_UL:.2f} chamber volumes, {FLOOD_LV * WETTED / SWEEP_UL:.1f}x the "
            f"target) in about {QUENCH_TARGET_S:g} s. The extra over the target is not waste: the flood "
            f"also has to fill the {LANE[key]['downstream']:.3f} uL DOWNSTREAM of the field, which the "
            f"well-mixed-chamber rule knows nothing about, or the extract chase would push rinse buffer "
            f"into vial M. Cold acidified organic solvent, free of chlorinated solvents; it arrives cold "
            f"from an off-chip reservoir while the STAGE is still at {RINSE_TEMP_C:g} C. Cells lyse where "
            f"they sit and the protein precipitates on the floor. No shear ceiling applies from here on. "
            f"INERTIAL, NOT STOKES: at this flow the feed line runs Re ~ "
            f"{RE['re_line']:.0f} and the comb spine Re ~ {RE['re_spine']:.0f}, so entrance effects and "
            f"minor losses at the tooth branches are real; only the teeth (Re ~ {RE['re_tooth']:.1f}) and "
            f"the field (Re ~ {RE['re_field']:.1f}) are near-creeping. Uniformity rests on TOOTH "
            f"CONVECTION, not on Re << 1. Outlet at waste; flow then stops.",
            QUENCH_TARGET_S, target=f"cx_{key}", reagent="cold_acidified_organic_quench",
            volume_ul=round(FLOOD_LV * WETTED, 3), temp_c=SOAK_TEMP_C, washes=1,
            flow_ul_min=FLOOD_FLOW, waste=f"out_{key}", port_state=lane_ports(key), kind="sweep")

    # --- PHASE 3: cold soak, stage at -20 C -----------------------------------------
    add("hold_temp",
        f"Every lane now holds organic solvent, so the stage can go sub-ambient without freezing "
        f"anything: ramp to {SOAK_TEMP_C:g} C, ports capped. THE RIG NEEDS A SUB-AMBIENT SOURCE for this "
        f"and protocols/thermal_map.md used to say it had none; that contradiction is resolved there.",
        settle_s(RINSE_TEMP_C, SOAK_TEMP_C), target="cx_n", temp_c=SOAK_TEMP_C, port_state=ports([], []))
    add("hold_temp",
        f"EXTRACTION SOAK: {SOAK_S:g} s stop-flow at {SOAK_TEMP_C:g} C, all three lanes together on the one "
        f"stage. The old file soaked {30:g} s at {RINSE_TEMP_C:g} C, which under-extracts and may not form "
        f"the protein film the resolubilisation step depends on. Published sequential extractions hold the "
        f"cold organic step for at least 5 min at {SOAK_TEMP_C:g} C (Sapcariu 2014, "
        f"doi:10.1016/j.mex.2014.07.002; Coman 2016 SIMPLEX, doi:10.1074/mcp.M115.053702); this is that "
        f"floor, not an optimum. Not a freeze-quench: the quench already happened in the flood.",
        SOAK_S, target="cx_n", temp_c=SOAK_TEMP_C, port_state=ports([], []))

    # --- PHASE 4: fraction 1, stage at -20 C ----------------------------------------
    for key, name in LANES.items():
        L = LANE[key]
        add("exchange",
            f"{name.capitalize()}: ELUTION CHASE, and a PARTIAL on purpose. The outlet valve turns to "
            f"vial M and fresh quench solvent pushes the extract out of out_{key}. "
            f"{L['extract']:.3f} uL = {L['extract'] / FIELD_UL:.2f} chamber volumes = the field plus "
            f"everything downstream of it (outlet comb + stub), which is exactly what lies between the "
            f"cells and the vial. It is NOT a sweep and must not be sized like one: the "
            f"{SWEEP_UL:g} uL a {EXCHANGE_TARGET:.0%} sweep would push would dilute the metabolome "
            f"{SWEEP_UL / L['extract']:.1f}x and break the <=10 uL extract-volume design rule, which is "
            f"the whole lever for metabolomics on this chip. Taking a defined fraction IS the point. "
            f"About {CELLS_PER_LANE:.0f} cells land in {L['extract']:.2f} uL, about "
            f"{CELLS_PER_LANE / L['extract']:.0f} cells/uL (ASSUMED {CELLS_PER_MM2:g} cells/mm2 on the "
            f"{floor_area_mm2():g} mm2 floor). What stays behind is a fresh charge of the same solvent, "
            f"not old extract, because the flood that preceded this already swept the lane.",
            push_s(L["extract"], FLOOD_FLOW), target=f"cx_{key}", reagent="cold_acidified_organic_quench",
            volume_ul=round(L["extract"], 3), temp_c=SOAK_TEMP_C, washes=1, flow_ul_min=FLOOD_FLOW,
            port_state=lane_ports(key), kind="partial")
        add("collect",
            f"{name.capitalize()}: FRACTION 1, the metabolome, plus peptides under about 10 kDa, which do "
            f"not precipitate. Neutralised on arrival (the vial is pre-charged off chip) and kept on the "
            f"cold block. Duration is {collect_s():g} s, not 0: two valve actuations, clearing the "
            f"{stub_ul(f'out_{key}'):.3f} uL outlet stub into the vial, then capping it and moving it. A "
            f"physical fraction collection takes time, and six steps of this protocol used to claim it "
            f"took none.",
            collect_s(), targets=[f"cx_{key}"], volume_ul=round(L["extract"], 3),
            port_state=lane_ports(key))

    # --- PHASE 5: flush and resolubilise, stage at ambient --------------------------
    add("hold_temp",
        f"Warm the stage to ambient ({AMBIENT_C:g} C), ports capped, BEFORE any aqueous buffer goes in: an "
        f"aqueous solubilisation buffer pushed into a {SOAK_TEMP_C:g} C lane would freeze in the comb. The "
        f"protein film is already precipitated, so warming the lane costs nothing.",
        settle_s(SOAK_TEMP_C, AMBIENT_C), target="cx_n", temp_c=AMBIENT_C, port_state=ports([], []))
    for key, name in LANES.items():
        add("exchange",
            f"{name.capitalize()}: SWEEP. The outlet valve returns to waste and {FLUSH_LV:g} lane wetted "
            f"volumes ({FLUSH_LV * WETTED:.3f} uL = {FLUSH_LV * WETTED / FIELD_UL:.2f} chamber volumes, "
            f"{FLUSH_LV * WETTED / SWEEP_UL:.1f}x the {SWEEP_UL:g} uL the {EXCHANGE_TARGET:.0%} target "
            f"needs) of solubilisation buffer clear the lane, the port hole and the stub of fraction 1 and "
            f"displace the organic solvent. This is the one step where carry-over between the two "
            f"fractions is decided, so it is sized well above the target rather than at it: ideal mixing "
            f"leaves exp(-{FLUSH_LV:g}) = {math.exp(-FLUSH_LV):.0%} of the old solvent in the LANE (a "
            f"laminar slot displaces better; unmeasured, to be shown with a dye test). The precipitated "
            f"protein stays on the floor.",
            push_s(FLUSH_LV * WETTED, FLOOD_FLOW), target=f"cx_{key}",
            reagent="denaturing_solubilisation_buffer", volume_ul=round(FLUSH_LV * WETTED, 3),
            temp_c=AMBIENT_C, washes=1, flow_ul_min=FLOOD_FLOW, waste=f"out_{key}",
            port_state=lane_ports(key), kind="sweep")
    add("hold_temp",
        f"RESOLUBILISATION: stop-flow, {RESOLUBILISE_S:g} s at ambient ({AMBIENT_C:g} C), all three lanes. "
        f"NOT at 37 C, which is what the old file did: a warm hold in a carbamylating chaotrope modifies "
        f"peptides and blocks identification, so the denaturant class here must be non-carbamylating and "
        f"the hold is not heated. The '>90% membrane protein recovery after organic precipitation' figure "
        f"in the literature is specific to COLD ACID resolubilisation, which this is not, so that number "
        f"is NOT claimed here. The protein is denatured by design: native complexes and enzyme activity "
        f"are gone, bottom-up identification and quantification are not. PLACEHOLDER duration.",
        RESOLUBILISE_S, target="cx_n", temp_c=RESOLUBILISE_TEMP_C, port_state=ports([], []))

    # --- PHASE 6: fraction 2 ---------------------------------------------------------
    for key, name in LANES.items():
        L = LANE[key]
        add("exchange",
            f"{name.capitalize()}: ELUTION CHASE, a PARTIAL for the same reason as fraction 1's. The "
            f"outlet valve turns to vial P and fresh buffer pushes the dissolved protein "
            f"({L['extract']:.3f} uL = {L['extract'] / FIELD_UL:.2f} chamber volumes, the field plus "
            f"everything downstream of it) out through the SAME port out_{key}, at a later time. Taking "
            f"that defined fraction is the point: sweeping to {EXCHANGE_TARGET:.0%} would hand the mass "
            f"spectrometer {SWEEP_UL:g} uL of dilute protein instead of {L['extract']:.3f} uL. Separate "
            f"vials and separate valve positions mean the two fractions never share a vial.",
            push_s(L["extract"], FLOOD_FLOW), target=f"cx_{key}",
            reagent="denaturing_solubilisation_buffer", volume_ul=round(L["extract"], 3),
            temp_c=AMBIENT_C, washes=1, flow_ul_min=FLOOD_FLOW, port_state=lane_ports(key),
            kind="partial")
        add("collect",
            f"{name.capitalize()}: FRACTION 2, the proteome, ready for bottom-up digestion. Spin vial M and "
            f"add its pellet here: the flood may strip fines off the floor film (UNTESTED). The lane is "
            f"spent. The single-cell transcriptome arm is a different sample path - on this demo chip it is "
            f"module 4 on capsules; at target scale it is the dissociated fields of the SAME lane.",
            collect_s(), targets=[f"cx_{key}"], volume_ul=round(L["extract"], 3),
            port_state=lane_ports(key))

    return {
        "id": "module6_v1",
        "name": "Bulk omics from adherent cells: one cold acidified organic extraction, metabolome then proteome - module6_v1",
        "source": SOURCE,
        "placeholder": True,
        "description": (
            f"Runs on chip/module6_v1.json, the PROVEN UNIT CELL for one extraction lane at demo scale. "
            f"Generated by protocols/build_module6.py: do not hand-edit this file. Every volume is derived "
            f"from the chip's geometry (chamber size, comb path/width/depth, port width and lid thickness) "
            f"plus the fabrication floors in contracts/blocks.json, and every flow and duration from "
            f"contracts/parameters.json. "
            f"THE UNIT IS THE LANE, NOT THE CHAMBER: the field ({LANE['n']['field']:g} uL) sits in series "
            f"with {LANE['n']['upstream']:.3f} uL upstream (inlet stub + inlet comb) and "
            f"{LANE['n']['downstream']:.3f} uL downstream (outlet comb + stub), so a lane wetted volume is "
            f"{WETTED:.3f} uL and one 'chamber volume' displaces nothing. Exchanges are multiples of the "
            f"lane volume; the extract that reaches a vial is {EXTRACT:.3f} uL (field + downstream). "
            f"THE LOAD IS CAPPED AT ONE FIELD VOLUME ({LANE['n']['field']:g} uL) and the upstream dead "
            f"volume is its own PRIME step, which is how the old hand-maintained file's 11.444 uL 'load' "
            f"into a {LANE['n']['field']:g} uL chamber (3.97x) is reconciled. The outlet comb is never "
            f"filled with cell suspension, so cells cannot be stranded past the field. "
            f"THE PRE-QUENCH RINSE is held to shear.prelysis_rinse_ceiling_dyn_cm2 "
            f"({RINSE_CEILING:g} dyn/cm2), the contract value nothing used to read: {RINSE_FLOW:g} uL/min, "
            f"so the {SWEEP_UL:g} uL the exchange target needs takes about "
            f"{push_s(SWEEP_UL, RINSE_FLOW):.0f} s, against "
            f"{held_flow(CULTURE_CEILING, 'rinse'):g} uL/min and "
            f"{push_s(SWEEP_UL, held_flow(CULTURE_CEILING, 'rinse')):.0f} s on the "
            f"culture ceiling. Kapoore 2017 found washing itself leaks metabolites, so the shortest "
            f"defensible rinse is also the right biology; this repo used to cite that paper for a long "
            f"careful wash, which is backwards. "
            f"ORDER IS BY PHASE, NOT BY LANE, because the chip has ONE thermal stage over all three "
            f"fields: seed and culture at {CULTURE_TEMP_C:g} C, rinse and flood at {RINSE_TEMP_C:g} C, soak "
            f"at {SOAK_TEMP_C:g} C, flush and resolubilise at ambient ({AMBIENT_C:g} C). One stage cannot "
            f"hold one lane at {SOAK_TEMP_C:g} C while the others are alive, the stage must not go "
            f"sub-ambient while a lane holds aqueous rinse buffer, and aqueous solubilisation buffer must "
            f"not enter a {SOAK_TEMP_C:g} C lane. The old lane-by-lane order broke all three. "
            f"The soak is {SOAK_S:g} s at {SOAK_TEMP_C:g} C (published floor), not 30 s at "
            f"{RINSE_TEMP_C:g} C; resolubilisation is at ambient, not 37 C, so a carbamylating chaotrope "
            f"cannot modify peptides during a warm hold. Collect steps carry real durations "
            f"({collect_s():g} s) instead of 0. "
            f"EVERY EXCHANGE DECLARES WHAT IT IS FOR in exchange_kind, because absence means sweep: "
            f"{sum(1 for s in steps if s.get('exchange_kind') == 'fill')} FILL (the prime of each lane's "
            f"dry upstream dead volume - nothing to clear, and capped above at one chamber volume, so it "
            f"is written as {prime_washes} x {prime_per_wash:g} uL), "
            f"{sum(1 for s in steps if s.get('exchange_kind') == 'sweep')} SWEEP (rinse, quench flood, "
            f"solubilisation flush) and {sum(1 for s in steps if s.get('exchange_kind') == 'partial')} "
            f"PARTIAL (the elution chases, which take a DEFINED fraction - the field plus everything "
            f"downstream of it - because extract volume is the whole lever for metabolomics; sweeping one "
            f"to {EXCHANGE_TARGET:.0%} would dilute the fraction about "
            f"{SWEEP_UL / LANE['n']['extract']:.1f}x and break the <=10 uL rule). A SWEEP HERE COSTS MORE "
            f"THAN THE CONTRACT FLOOR: {SWEEP_CV:.2f} chamber volumes ({SWEEP_FLOOR_UL:g} uL) assumes "
            f"clean reagent reaches the field from the first microlitre, but the inlet stub and comb hold "
            f"the same old liquid, so each sweep is sized as that {LANE['n']['upstream']:.3f} uL of dead "
            f"volume PLUS the contract's chamber volumes = {SWEEP_UL:g} uL. "
            f"THREE VISCOSITIES, EACH NAMED: the culture, prime and load flows use "
            f"contracts fluid.viscosity_37c_pa_s ({MU['culture'] * 1e3:g} mPa s, culture medium at 37 C, "
            f"which is what they push); the rinse uses fluid.viscosity_pa_s (water, 20 C) carried to 4 C by "
            f"the textbook water ratio ({MU['rinse'] * 1e3:.2f} mPa s), because the rinse is an aqueous "
            f"buffer on ice and not medium at 37 C; the cold organic quench is neither water nor medium and "
            f"has no contract value, so {MU['solvent'] * 1e3:g} mPa s is an ASSUMPTION - it sets only the "
            f"flood pressure drop and Reynolds number, never a shear ceiling, because the cells are already "
            f"dead by then. "
            f"PLACEHOLDERS still to fit: attachment, culture duration and interval, resolubilisation time. "
            f"Reagent names are generic and solvents are named by class; see CONFIDENTIALITY.md."
        ),
        "steps": steps,
    }


def report() -> None:
    f = CHAMBERS["cx_n"]
    print(f"field {f['size'][0]} x {f['size'][1]} x {f['size'][2]} mm = {f['volume_ul']:g} uL, "
          f"floor {f['size'][0] * f['size'][1]:g} mm2")
    print(f"comb: spine {path_length_mm(CHANNELS['comb_in_n']):g} x {CHANNELS['comb_in_n']['width_mm']:g} mm, "
          f"{tooth_count(f)} teeth of {MIN_OPEN_MM:g} x {tooth_length_mm(CHANNELS['comb_in_n'], f):g} mm "
          f"(rib {MIN_WALL_MM:g} mm, pitch {MIN_OPEN_MM + MIN_WALL_MM:g} mm) "
          f"= {comb_ul('comb_in_n', 'cx_n'):.3f} uL")
    print(f"stub: {CHIP_PORTS['in_n']['width_um']:g} um hole through {lid_thickness_mm():g} mm of lid "
          f"+ {VALVE_STUB_UL:g} uL valve = {stub_ul('in_n'):.3f} uL")
    print("\nlane volumes (uL):")
    for k, v in LANE.items():
        print(f"  cx_{k}  upstream {v['upstream']:.3f}  field {v['field']:.3f}  downstream "
              f"{v['downstream']:.3f}  wetted {v['wetted']:.3f}  extract {v['extract']:.3f}")

    print("\nwhich viscosity each step runs at:")
    for kind in ("culture", "rinse", "solvent"):
        print(f"  {kind:<9} {mu(kind) * 1e3:.3f} mPa s   {MU_WHY[kind]}")

    print(f"\nflows (tau = 6 mu Q / (w h^2), w = {f['size'][1]:g} mm, h = {f['size'][2]:g} mm, "
          f"{HEADROOM:.0%} of the ceiling, hottest tooth {TOOTH_PEAK_FACTOR:g}x the mean):")
    for label, ceiling, kind, flow in (
            ("culture / prime / load", CULTURE_CEILING, "culture", CULTURE_FLOW),
            ("pre-quench rinse", RINSE_CEILING, "rinse", RINSE_FLOW),
            ("rinse on the WRONG (culture) ceiling", CULTURE_CEILING, "rinse",
             held_flow(CULTURE_CEILING, "rinse"))):
        print(f"  {label:<38} ceiling {ceiling:<5g} mu {mu(kind) * 1e3:.2f} mPa s  "
              f"{flow:>7.2f} uL/min  field {shear(flow, kind, f):.4f} dyn/cm2  "
              f"tooth {shear(flow, kind, f) * TOOTH_PEAK_FACTOR:.4f}  "
              f"{FLOOD_LV:g} lane volumes in {push_s(FLOOD_LV * WETTED, flow):.0f} s")
    print(f"  {'quench flood / chase / flush':<38} no ceiling (cells dead)        "
          f"{FLOOD_FLOW:>7.1f} uL/min  {FLOOD_LV:g} lane volumes in "
          f"{push_s(FLOOD_LV * WETTED, FLOOD_FLOW):.2f} s")

    print(f"\nthe flood is NOT Stokes flow (rho = {SOLVENT_DENSITY_KG_M3:g} kg/m3 ASSUMED, "
          f"mu = {mu('solvent') * 1e3:g} mPa s):")
    print(f"  feed line Re {RE['re_line']:.0f}   comb spine Re {RE['re_spine']:.0f}   "
          f"tooth Re {RE['re_tooth']:.1f}   field Re {RE['re_field']:.1f}")
    print(f"  entrance length in the spine about {RE['entrance_mm']:.2f} mm; tooth branches are "
          f"entrance-effect features, so uniformity rests on tooth convection, not on creeping flow")
    print(f"  quench time still checks out: {RE['sweep_s']:.2f} s convective sweep across the "
          f"{f['size'][0]:g} mm field + {RE['cell_diffusion_warm_s']:.2f} s diffusion over a "
          f"{CELL_DIAMETER_UM:g} um cell at the contract D, or {RE['cell_diffusion_s']:.2f} s with D cut "
          f"by the cold viscous solvent = {RE['sweep_s'] + RE['cell_diffusion_warm_s']:.2f}-"
          f"{RE['sweep_s'] + RE['cell_diffusion_s']:.2f} s, still about 1 s")
    print(f"  RISK: the {MIN_WALL_MM:g} mm ribs leave dead zones that would take "
          f"{RE['dead_zone_s'][0]:.0f}-{RE['dead_zone_s'][1]:.0f} s to clear by diffusion alone (the "
          f"physics audit put it at 450-1800 s), so tooth convection is load-bearing, not a nicety")
    print(f"  pressure: {RE['lane_kpa']:.2f} kPa across the lane + {RE['line_kpa']:.2f} kPa across "
          f"{FEED_LINE_LENGTH_MM:g} mm of {FEED_LINE_BORE_MM:g} mm bore = "
          f"{RE['lane_kpa'] + RE['line_kpa']:.2f} kPa, well under 10 kPa")

    print(f"\ngelatin interference, CORE-SHELL not solid: a {3:g} um shell on a {CAPSULE_D_UM:g} um capsule "
          f"is {SHELL_FRACTION:.0%} of its volume,")
    print(f"  so capsule gelatin outweighs the cell's protein about {GEL_LOW:.0f}-{GEL_HIGH:.0f}x, "
          f"not 30-45x (which assumed a solid sphere).")
    print("  The conclusion is unchanged: extraction belongs on adherent cells.")

    print(f"\nthermal: ramp {RAMP_C_PER_S:g} C/s, tau {TAU_S:g} s, so a setpoint change settles in "
          f"ramp + 3 tau")
    for a, b in ((CULTURE_TEMP_C, RINSE_TEMP_C), (RINSE_TEMP_C, SOAK_TEMP_C), (SOAK_TEMP_C, AMBIENT_C)):
        print(f"  {a:>3g} C -> {b:>3g} C: {settle_s(a, b):.0f} s")

    proto = build()
    print(f"\nexchange_kind (absence means sweep, so nothing is left undeclared):")
    for kind in ("fill", "sweep", "partial"):
        rows = [s for s in proto["steps"] if s.get("exchange_kind") == kind]
        print(f"  {kind:<8} {len(rows)}  " + ", ".join(
            f"{s['volume_ul']:g}x{s['washes']} uL = {s['volume_ul'] * s['washes'] / FIELD_UL:.2f} CV"
            for s in rows[:2]) + (" ..." if len(rows) > 2 else ""))
    print(f"  sweep floor: {SWEEP_CV:.2f} CV = {SWEEP_FLOOR_UL:g} uL (contract); sized at "
          f"{SWEEP_UL:g} uL = {SWEEP_UL / FIELD_UL:.2f} CV (+ the {LANE['n']['upstream']:.3f} uL of "
          f"upstream dead volume that holds the same old liquid)")
    print(f"  rinse  {SWEEP_UL:g} uL at {RINSE_FLOW:g} uL/min = {push_s(SWEEP_UL, RINSE_FLOW):.2f} s "
          f"(contract floor alone would be {push_s(SWEEP_FLOOR_UL, RINSE_FLOW):.2f} s)")
    print(f"  flood  {FLOOD_LV * WETTED:.3f} uL at {FLOOD_FLOW:g} uL/min = {QUENCH_TARGET_S:g} s "
          f"({FLOOD_LV * WETTED / SWEEP_UL:.1f}x the target; the target alone would take "
          f"{push_s(SWEEP_UL, FLOOD_FLOW):.2f} s)")
    print(f"  ATP/ADP caveat: {push_s(SWEEP_UL, RINSE_FLOW):.0f} s of cold rinse is still many times the "
          f"~1 s turnover, so it does NOT move - still INVALID")
    print(f"\n{len(proto['steps'])} steps, "
          f"{sum(s['duration_s'] for s in proto['steps']) / 3600:.1f} h of protocol time")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    protocol = build()
    OUT.write_text(json.dumps(protocol, indent=2) + "\n")
    total = sum(s["duration_s"] for s in protocol["steps"])
    print(f"wrote {OUT.name}: {len(protocol['steps'])} steps, {total / 3600:.1f} hours")

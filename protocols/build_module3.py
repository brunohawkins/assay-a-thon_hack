"""Build protocols/module3_v1.json.

    python protocols/build_module3.py           # write the protocol
    python protocols/build_module3.py --report  # volumes, flows and shear in the trap

Runs on chip/module3_v1_translated.json: load capsules, lysis, washes, reverse
transcription, wash, back-flush, release. Plate-based split-and-pool barcoding is dropped,
so the only step after lysis and the washes is reverse transcription with a plain poly(dT)
primer.

NO BARCODE IS ADDED HERE, AND THAT IS NOT THE SAME AS THERE BEING NO BARCODE. Reverse
transcription makes one cell's cDNA inside each capsule and leaves it unlabelled. The
per-CELL barcode is added in the NEXT stage, protocols/sctip_spc_v1.json: each capsule is
re-injected into its own oil droplet, where a single barcode molecule is clonally amplified
and overlap-extended onto that capsule's cDNA. One capsule holds one cell, so the data is
single-cell. The per-lane label the cells carry from module 1 and module 4's four
sublibrary indices (about twelve groups) are for DEMULTIPLEXING, not resolution.

Nothing numeric about liquid is typed in here. Every volume is the trap's volume_ul
times a number of chamber volumes, every flow is derived from the trap's width and depth,
and the port states come from the chip's port list, so rescaling the chip and rerunning
this script is all it takes.

WHAT AN EXCHANGE IS FOR IS A FIELD, NOT PROSE. Every exchange carries `exchange_kind`:
"sweep" where the job is to clear the trap (lysis, the two washes, the back-flush that
lifts the bed) and "fill" where it is to put a reagent in (the RT mix). A sweep is sized
from exchange.target_fraction: a well-mixed chamber keeps exp(-V) of the old liquid after
V chamber volumes, so SWEEP_CV = -ln(1 - target) chamber volumes reach the goal. The sweeps
here used to be round numbers (5 chamber volumes of lysis buffer, 15 of wash buffer) that
no contract value implied; 15 put the wash-buffer demand at 1153 uL against a 1000 uL
reservoir, i.e. a step that cannot be run as written. They are now derived.

THE TRAP HOLDS CAPSULES, SO THE CULTURE SHEAR CEILING IS THE WRONG BOUND. This file used
to take Q as 85% of the flow giving shear.culture_ceiling_dyn_cm2 in main_trap, which
contracts/parameters.json explicitly forbids: that ceiling "applies to chambers holding
ADHERENT CELLS... It does NOT apply to chambers holding CAPSULES: a capsule is not attached
to anything and the limit there is capsule.escape_pressure_kpa". main_trap never holds a
cell attached to anything - it holds capsules on a weir - so the bound is the pressure
across that weir with a full capsule bed, kept under control.pressure_margin of the escape
pressure. That is 70 uL/min rather than 10.8, and it is why these steps took hours.
--report prints both numbers side by side.

mu is READ FROM contracts/parameters.json (fluid.viscosity_37c_pa_s, the 37 C culture-medium
value the contract tells every protocol calculation to use), not hard-coded. This file used
to carry a literal 0.8e-3, which was the right physics with no provenance.

Reagent names are generic and nothing here is a recipe. See CONFIDENTIALITY.md.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).resolve().parent / "module3_v1.json"
CHIP = ROOT / "chip" / "module3_v1_translated.json"
CONTRACT = ROOT / "contracts" / "parameters.json"
sys.path.insert(0, str(ROOT / "operator"))

import flow_calc  # noqa: E402  the repo's one mesh/channel resistance implementation

SOURCE = (
    "Capsule workflow: Baronas et al., Science 391, 1138 (2026), doi:10.1126/science.ady7227 "
    "(CapSeq). Chip geometry, volumes and flows are ours."
)


def parameter(name: str) -> float:
    """One value from contracts/parameters.json. No fallback: a missing contract is a bug,
    and a literal copied here is how mu lost its provenance last time."""
    return float(json.loads(CONTRACT.read_text())["parameters"][name]["value"])


MU_PA_S = parameter("fluid.viscosity_37c_pa_s")
SHEAR_CEILING = parameter("shear.culture_ceiling_dyn_cm2")   # ADHERENT CELLS ONLY; see the docstring
ESCAPE_KPA = parameter("capsule.escape_pressure_kpa")
PRESSURE_MARGIN = parameter("control.pressure_margin")
BED_RESISTANCE = parameter("mesh.bed_resistance_kpa_per_ul_min")
MESH_OPEN_FRACTION = parameter("mesh.open_fraction")
LIFT_FLOW_UL_MIN = parameter("backflush.lift_flow_ul_min")
BACKFLUSH_TARGET = parameter("backflush.target_fraction")
EXCHANGE_TARGET = parameter("exchange.target_fraction")
SWEEP_CV = -math.log(1.0 - EXCHANGE_TARGET)   # chamber volumes a sweep needs to hit the goal
FLOW_FRACTION = 0.85       # 15% headroom under whichever bound applies

# How much liquid each step intends, in chamber volumes. A fill is one chamber volume; a
# sweep is SWEEP_CV, derived from exchange.target_fraction and not chosen.
LOAD_CV = 1.0
RT_MIX_CV = 1.0
RELEASE_CV = 2             # carries the bed out; the step records no per-wash volume

LYSIS_DWELL_S = 600        # round placeholder
LYSIS_TEMP_C = 22
RT_HOLD_S = 3600           # round placeholder
RT_TEMP_C = 42

chip = json.loads(CHIP.read_text())
TRAP = next(c for c in chip["chambers"] if c["id"] == "main_trap")
PORTS = [p["id"] for p in chip["ports"]]
ZONE = next(z for z in chip["thermal_zones"] if "main_trap" in z["chambers"])
assert ZONE["min_temp_c"] <= RT_TEMP_C <= ZONE["max_temp_c"], "RT hold is outside the trap's thermal zone"

V = TRAP["volume_ul"]


def max_flow(width_mm: float, height_mm: float, ceiling: float = SHEAR_CEILING) -> float:
    """Highest flow in uL/min that keeps wall shear at or below the ceiling."""
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (ceiling / 10.0) * w * h * h / (6.0 * MU_PA_S) * 60.0 * 1e9


def shear(flow_ul_min: float, width_mm: float, height_mm: float) -> float:
    q = flow_ul_min * 1e-9 / 60.0
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (6.0 * MU_PA_S * q / (w * h * h)) * 10.0


def escape_flow(chamber: dict) -> float:
    """Highest flow in uL/min that keeps the pressure across this chamber's retention under
    control.pressure_margin of capsule.escape_pressure_kpa with a FULL capsule bed.

    This, not the culture shear ceiling, is the bound for a chamber holding capsules: the
    risk is a soft capsule squeezing through the weir, not a cell being detached from a
    surface it is stuck to. Resistance is the clean weir (negligible) plus the capsule bed
    at full cover, both from contracts/parameters.json via operator/flow_calc.py, which is
    the same resistance operator/model.py and operator/safety.py judge the run against.
    """
    area_mm2 = chamber["size"][0] * chamber["size"][1]
    clean = flow_calc.mesh_clean_resistance(chamber["retention"]["opening_um"], area_mm2,
                                            MESH_OPEN_FRACTION, MU_PA_S)
    return PRESSURE_MARGIN * ESCAPE_KPA / (clean + BED_RESISTANCE)


# main_trap holds capsules on a weir, so the escape-pressure bound applies and the culture
# shear ceiling does not. Rounded down to 0.1 uL/min, never up.
FLOW = math.floor(FLOW_FRACTION * escape_flow(TRAP) * 10) / 10
CULTURE_CEILING_FLOW = FLOW_FRACTION * max_flow(TRAP["size"][1], TRAP["size"][2])
assert FLOW >= LIFT_FLOW_UL_MIN, (
    f"{FLOW} uL/min is below backflush.lift_flow_ul_min ({LIFT_FLOW_UL_MIN} uL/min): the "
    f"capsule bed would not come off the weir")


def seconds(volume_ul: float, washes: int = 1) -> int:
    return math.ceil(washes * volume_ul / FLOW * 60.0)


def ports(feed: list[str], vent: list[str]) -> dict:
    """Port state for a step; every port not fed or vented is capped."""
    return {"feed": feed, "vent": vent, "cap": [p for p in PORTS if p not in feed + vent]}


def build() -> dict:
    steps: list[dict] = []

    def add(operation, note, duration_s, reagent=None, volume_ul=None, temp_c=None, washes=None,
            flow_ul_min=None, waste=None, direction=None, port_state=None, exchange_kind=None):
        if operation == "exchange" and exchange_kind is None:
            raise ValueError("every exchange must declare exchange_kind ('sweep', 'partial' or "
                             "'fill'); an absent one means the strict sweep rule, so it is "
                             "stated here rather than left to a default")
        step = {"step": len(steps) + 1, "operation": operation, "target": "main_trap",
                "reagent": reagent, "volume_ul": volume_ul, "duration_s": duration_s,
                "temp_c": temp_c, "washes": washes, "flow_ul_min": flow_ul_min, "note": note}
        if waste:
            step["waste"] = waste
        if direction:
            step["direction"] = direction
        if exchange_kind:
            step["exchange_kind"] = exchange_kind
        step["ports"] = port_state
        steps.append(step)

    sweep_ul = round(V * SWEEP_CV, 3)

    load_ul = round(V * LOAD_CV, 3)
    add("load",
        "Load the capsule suspension into the trap through the pre-filter. Capsules settle on the "
        "retention gap and the carrier leaves through the wash waste; the load is one chamber volume.",
        seconds(load_ul), reagent="capsule_suspension", volume_ul=load_ul, flow_ul_min=FLOW,
        port_state=ports(["capsule_in"], ["waste"]))
    add("exchange",
        f"Lyse the cells in the capsules: push lysis buffer through the bed to its own dedicated waste "
        f"line, which never meets the other waste. SWEEP: {SWEEP_CV:.1f} chamber volumes, which is what "
        f"the {EXCHANGE_TARGET:.0%} exchange goal needs in a well-mixed chamber (what is left after V "
        f"chamber volumes is exp(-V)), so the bed is fully exchanged.",
        seconds(sweep_ul), reagent="lysis_buffer", volume_ul=sweep_ul, washes=1, flow_ul_min=FLOW,
        waste="lysis_waste", exchange_kind="sweep", port_state=ports(["reagent_in"], ["lysis_waste"]))
    add("hold_temp",
        "Lysis dwell with every port capped, at room temperature, so the lysis buffer reaches the cells "
        "inside the capsules.",
        LYSIS_DWELL_S, temp_c=LYSIS_TEMP_C, port_state=ports([], []))
    add("exchange",
        f"Wash out the lysis buffer through the retention gap to the wash waste, since carryover would "
        f"inhibit the next step. SWEEP: {SWEEP_CV:.1f} chamber volumes, the {EXCHANGE_TARGET:.0%} exchange "
        f"goal for a well-mixed chamber. This used to ask for 15 chamber volumes, which no contract value "
        f"implied and which needed more wash buffer than a reservoir holds.",
        seconds(sweep_ul), reagent="wash_buffer", volume_ul=sweep_ul, washes=1, flow_ul_min=FLOW,
        waste="waste", exchange_kind="sweep", port_state=ports(["reagent_in"], ["waste"]))
    rt_ul = round(V * RT_MIX_CV, 3)
    add("exchange",
        "Fill the trap with the reverse-transcription mix, which carries a plain poly(dT) primer: no "
        "barcode and no UMI are added by reverse transcription itself. The per-cell barcode comes from "
        "the NEXT stage (protocols/sctip_spc_v1.json), where each capsule is re-injected into its own "
        "oil droplet and a single barcode molecule is clonally amplified there and overlap-extended "
        "onto this cDNA, so the data is single-cell. The per-lane label from module 1 and module 4's "
        "sublibrary index are for demultiplexing, not resolution. FILL: one chamber volume, so the bed "
        "is covered and no more is spent. It is a fill and not a sweep - the wash before it already "
        "cleared the trap - so it is bounded above by the chamber, not below by the sweep rule.",
        seconds(rt_ul), reagent="rt_mix", volume_ul=rt_ul, washes=1, flow_ul_min=FLOW, waste="waste",
        exchange_kind="fill", port_state=ports(["reagent_in"], ["waste"]))
    add("hold_temp",
        f"Reverse transcription: one isothermal hold in the trap, in the heated zone under the chamber, "
        f"with every port capped. The only thermal step on this chip; the chamber is held between "
        f"{ZONE['min_temp_c']:g} and {ZONE['max_temp_c']:g} C by its thermal zone.",
        RT_HOLD_S, temp_c=RT_TEMP_C, port_state=ports([], []))
    add("exchange",
        f"Wash the reverse-transcription mix out through the retention gap to the wash waste, leaving "
        f"cDNA inside the capsules. SWEEP: {SWEEP_CV:.1f} chamber volumes, the {EXCHANGE_TARGET:.0%} "
        f"exchange goal for a well-mixed chamber.",
        seconds(sweep_ul), reagent="wash_buffer", volume_ul=sweep_ul, washes=1, flow_ul_min=FLOW,
        waste="waste", exchange_kind="sweep", port_state=ports(["reagent_in"], ["waste"]))
    add("exchange",
        f"Back-flush the trap through backflush_in: liquid pushed up through the retention gap lifts the "
        f"capsules off it and sweeps them towards the capsule outlet. SWEEP, and sized like one: the bed "
        f"comes off as exp(-2V) in chamber volumes V, so one chamber volume lifts only 86% against the "
        f"{BACKFLUSH_TARGET:.0%} back-flush target, which is what this step used to ask for. "
        f"{SWEEP_CV:.1f} chamber volumes at {FLOW:g} uL/min, which is above the "
        f"{LIFT_FLOW_UL_MIN:g} uL/min lift flow the bed needs to leave the weir at all.",
        seconds(sweep_ul), reagent="resuspension_buffer", volume_ul=sweep_ul, washes=1, flow_ul_min=FLOW,
        direction="backflush", exchange_kind="sweep",
        port_state=ports(["backflush_in"], ["to_split_tree"]))
    add("release",
        f"Release the capsules to module 4 as one counted suspension through to_split_tree. {RELEASE_CV} "
        f"chamber volumes of buffer carry the bed out; module 4 then splits it into the sublibraries and "
        f"adds the index.",
        seconds(V * RELEASE_CV), reagent="resuspension_buffer", flow_ul_min=FLOW,
        port_state=ports(["backflush_in"], ["to_split_tree"]))

    return {
        "id": "module3_v1",
        "name": "Module 3: load capsules, lysis, washes, reverse transcription, wash, release - module3_v1",
        "source": SOURCE,
        "placeholder": True,
        "description": (
            "Runs on chip/module3_v1_translated.json. Plate-based split-and-pool barcoding is dropped, so "
            "the only step after lysis and the washes is reverse transcription with a plain poly(dT) "
            "primer: no barcode and no UMI are added by reverse transcription itself. The PER-CELL "
            "barcode is added in the next stage (protocols/sctip_spc_v1.json), where every capsule is "
            "re-injected into its own oil droplet and a single barcode molecule is clonally amplified "
            "there and overlap-extended onto this cDNA; one capsule holds one cell, so the data is "
            "single-cell. The per-lane label from module 1 and module 4's four sublibrary indices are "
            "for demultiplexing, not resolution. "
            "Reverse transcription is one isothermal hold, so it runs in main_trap itself and no new chip "
            "is needed. Every volume is a multiple of the trap's volume_ul, and every exchange declares "
            "what it is FOR in its exchange_kind field: a SWEEP (lysis, the two washes, the back-flush) "
            f"moves {SWEEP_CV:.1f} chamber volumes, which is what the {EXCHANGE_TARGET:.0%} exchange goal "
            f"needs in a well-mixed chamber, and a FILL (the RT mix) moves one volume to put the reagent "
            f"in. main_trap holds CAPSULES, so the bound on its flow is capsule.escape_pressure_kpa across "
            f"the weir with a full bed and NOT shear.culture_ceiling_dyn_cm2, which contracts/parameters.json "
            f"reserves for chambers holding adherent cells: {FLOW:g} uL/min, "
            f"{PRESSURE_MARGIN:.0%} of the {ESCAPE_KPA:g} kPa escape pressure with 15% headroom, against "
            f"the {CULTURE_CEILING_FLOW:.1f} uL/min the culture ceiling would have allowed. mu is read "
            f"from contracts/parameters.json (fluid.viscosity_37c_pa_s = {MU_PA_S:g} Pa s), not hard-coded. "
            "Reagent names are "
            "generic; the dwell and hold durations are round placeholders to be fitted. lysis, wash and "
            "the RT mix share reagent_in through an off-chip rotary valve, so the line needs flushing "
            "between them. Generated by protocols/build_module3.py."
        ),
        "steps": steps,
    }


def report() -> None:
    w, h = TRAP["size"][1], TRAP["size"][2]
    print(f"trap {TRAP['size'][0]} x {w} x {h} mm = {V} uL, mu = {MU_PA_S:g} Pa s "
          f"(contracts/parameters.json fluid.viscosity_37c_pa_s)\n")
    print("Which ceiling applies to main_trap, and what it allows:")
    print(f"  adherent-cell culture ceiling  {SHEAR_CEILING:g} dyn/cm2   "
          f"{CULTURE_CEILING_FLOW:>7.1f} uL/min   DOES NOT APPLY: the trap holds capsules, not cells "
          f"stuck to a surface")
    print(f"  capsule escape pressure        {ESCAPE_KPA:g} kPa         "
          f"{FLOW:>7.1f} uL/min   APPLIES: {PRESSURE_MARGIN:.0%} of it across a full bed, 15% headroom")
    print(f"  back-flush lift flow           {LIFT_FLOW_UL_MIN:g} uL/min     "
          f"{'':>7}            the floor the bed needs to leave the weir; "
          f"{'met' if FLOW >= LIFT_FLOW_UL_MIN else 'NOT MET'}")
    print(f"  wall shear at {FLOW:g} uL/min: {shear(FLOW, w, h):.4f} dyn/cm2 - above the culture ceiling "
          f"on purpose, with no cell in the chamber to detach.\n")
    proto = build()
    print(f"Exchange sizing, goal {EXCHANGE_TARGET:.0%} (sweep = {SWEEP_CV:.2f} chamber volumes):")
    need: dict[str, float] = {}
    for s in proto["steps"]:
        if s["volume_ul"] is None:
            continue
        total = s["volume_ul"] * (s["washes"] or 1)
        need[s["reagent"]] = need.get(s["reagent"], 0.0) + total
        if s["operation"] == "exchange":
            left = math.exp(-total / V)
            print(f"  step {s['step']:>2}  {s['exchange_kind']:<8}{s['reagent']:<22}"
                  f"{total / V:>5.2f} CV  leaves {left:>7.2%}  {s['duration_s'] / 60:>6.1f} min")
    print("\nReagent demand against reservoir.default_ul:")
    for r, v in sorted(need.items()):
        print(f"  {r:<24}{v:>8.0f} uL")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    protocol = build()
    OUT.write_text(json.dumps(protocol, indent=2) + "\n")
    total = sum(s["duration_s"] for s in protocol["steps"])
    print(f"wrote {OUT.name}: {len(protocol['steps'])} steps, {total / 3600:.1f} hours")

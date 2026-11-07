"""Build protocols/module3_v1.json.

    python protocols/build_module3.py           # write the protocol
    python protocols/build_module3.py --report  # volumes, flows and shear in the trap

Runs on chip/module3_v1_translated.json: load capsules, lysis, washes, reverse
transcription, wash, back-flush, release. Split-and-pool barcoding is dropped, so the
only step after lysis and the washes is reverse transcription with a plain poly(dT)
primer.

Nothing numeric about liquid is typed in here. Every volume is the trap's volume_ul
times a number of chamber volumes, every flow is derived from the trap's width and depth,
and the port states come from the chip's port list, so rescaling the chip and rerunning
this script is all it takes.

The contract defines `volume_ul` of an exchange as the volume PER WASH and a wash as one
fill-and-drain cycle, so a step can never move more than the chamber holds in one go: a
step that wants five chamber volumes is five washes of one. Wall shear is
tau = 6 * mu * Q / (w * h^2), with w = size[1] and h = size[2] of the trap; Q is 85% of
the flow that gives 0.01 dyn/cm2, rounded down to 0.1 uL/min. Narrow channels and the
retention gap see higher shear at that flow; only the chamber is held to the ceiling.

Reagent names are generic and nothing here is a recipe. See CONFIDENTIALITY.md.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "module3_v1.json"
CHIP = Path(__file__).resolve().parents[1] / "chip" / "module3_v1_translated.json"

SOURCE = (
    "Capsule workflow: Baronas et al., Science 391, 1138 (2026), doi:10.1126/science.ady7227 "
    "(CapSeq). Chip geometry, volumes and flows are ours."
)

MU_PA_S = 0.8e-3
SHEAR_CEILING = 0.01       # dyn/cm2
FLOW_FRACTION = 0.85       # 15% headroom under the ceiling

# How much liquid each step intends, in chamber volumes. These are process choices,
# not geometry: the geometry sets what a chamber volume is.
LOAD_CV = 1.0
LYSIS_CV = 5               # whole chamber volumes, so each is one wash
WASH_ROUNDS, WASH_CV = 3, 5
RT_MIX_CV = 1.0
BACKFLUSH_CV = 1.0
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


FLOW = math.floor(FLOW_FRACTION * max_flow(TRAP["size"][1], TRAP["size"][2]) * 10) / 10


def seconds(volume_ul: float, washes: int = 1) -> int:
    return math.ceil(washes * volume_ul / FLOW * 60.0)


def ports(feed: list[str], vent: list[str]) -> dict:
    """Port state for a step; every port not fed or vented is capped."""
    return {"feed": feed, "vent": vent, "cap": [p for p in PORTS if p not in feed + vent]}


def build() -> dict:
    steps: list[dict] = []

    def add(operation, note, duration_s, reagent=None, volume_ul=None, temp_c=None, washes=None,
            flow_ul_min=None, waste=None, direction=None, port_state=None):
        step = {"step": len(steps) + 1, "operation": operation, "target": "main_trap",
                "reagent": reagent, "volume_ul": volume_ul, "duration_s": duration_s,
                "temp_c": temp_c, "washes": washes, "flow_ul_min": flow_ul_min, "note": note}
        if waste:
            step["waste"] = waste
        if direction:
            step["direction"] = direction
        step["ports"] = port_state
        steps.append(step)

    load_ul = round(V * LOAD_CV, 3)
    add("load",
        "Load the capsule suspension into the trap through the pre-filter. Capsules settle on the "
        "retention gap and the carrier leaves through the wash waste; the load is one chamber volume.",
        seconds(load_ul), reagent="capsule_suspension", volume_ul=load_ul, flow_ul_min=FLOW,
        port_state=ports(["capsule_in"], ["waste"]))
    add("exchange",
        f"Lyse the cells in the capsules: push lysis buffer through the bed to its own dedicated waste "
        f"line, which never meets the other waste. {LYSIS_CV} fills of one chamber volume each, so the "
        f"bed is fully exchanged.",
        seconds(V, LYSIS_CV), reagent="lysis_buffer", volume_ul=V, washes=LYSIS_CV, flow_ul_min=FLOW,
        waste="lysis_waste", port_state=ports(["reagent_in"], ["lysis_waste"]))
    add("hold_temp",
        "Lysis dwell with every port capped, at room temperature, so the lysis buffer reaches the cells "
        "inside the capsules.",
        LYSIS_DWELL_S, temp_c=LYSIS_TEMP_C, port_state=ports([], []))
    add("exchange",
        f"Wash out the lysis buffer through the retention gap to the wash waste: {WASH_ROUNDS} rounds of "
        f"{WASH_CV} fills of one chamber volume each, since carryover would inhibit the next step.",
        seconds(V, WASH_ROUNDS * WASH_CV), reagent="wash_buffer", volume_ul=V,
        washes=WASH_ROUNDS * WASH_CV, flow_ul_min=FLOW, waste="waste",
        port_state=ports(["reagent_in"], ["waste"]))
    rt_ul = round(V * RT_MIX_CV, 3)
    add("exchange",
        "Fill the trap with the reverse-transcription mix, which carries a plain poly(dT) primer: no "
        "barcode and no UMI are added here, because the cells already carry their per-lane barcode from "
        "module 1. One chamber volume, so the bed is covered and no more is spent.",
        seconds(rt_ul), reagent="rt_mix", volume_ul=rt_ul, washes=1, flow_ul_min=FLOW, waste="waste",
        port_state=ports(["reagent_in"], ["waste"]))
    add("hold_temp",
        f"Reverse transcription: one isothermal hold in the trap, in the heated zone under the chamber, "
        f"with every port capped. The only thermal step on this chip; the chamber is held between "
        f"{ZONE['min_temp_c']:g} and {ZONE['max_temp_c']:g} C by its thermal zone.",
        RT_HOLD_S, temp_c=RT_TEMP_C, port_state=ports([], []))
    add("exchange",
        f"Wash the reverse-transcription mix out through the retention gap to the wash waste, leaving "
        f"cDNA inside the capsules: {WASH_ROUNDS} rounds of {WASH_CV} fills of one chamber volume each.",
        seconds(V, WASH_ROUNDS * WASH_CV), reagent="wash_buffer", volume_ul=V,
        washes=WASH_ROUNDS * WASH_CV, flow_ul_min=FLOW, waste="waste",
        port_state=ports(["reagent_in"], ["waste"]))
    back_ul = round(V * BACKFLUSH_CV, 3)
    add("exchange",
        "Back-flush the trap through backflush_in: liquid pushed up through the retention gap lifts the "
        "capsules off it and sweeps them towards the capsule outlet.",
        seconds(back_ul), reagent="resuspension_buffer", volume_ul=back_ul, washes=1, flow_ul_min=FLOW,
        direction="backflush", port_state=ports(["backflush_in"], ["to_split_tree"]))
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
            "Runs on chip/module3_v1_translated.json. Split-and-pool barcoding is dropped, so the only "
            "step after lysis and the washes is reverse transcription with a plain poly(dT) primer: cells "
            "already carry a per-lane lipid-anchored barcode from module 1 and nothing is added here. "
            "Reverse transcription is one isothermal hold, so it runs in main_trap itself and no new chip "
            "is needed. Every volume is a multiple of the trap's volume_ul, and an exchange moves at most "
            "one chamber volume per wash; every flow is the highest value that keeps wall shear in the "
            "trap at or below 0.01 dyn/cm2 with 15% headroom (tau = 6*mu*Q/(w*h^2), mu = 0.8e-3 Pa s, w "
            "and h from the trap size), rounded down to 0.1 uL/min. Narrow channels and the retention gap "
            "see higher shear at that flow; only the chamber is held to the ceiling. Reagent names are "
            "generic; the dwell and hold durations are round placeholders to be fitted. lysis, wash and "
            "the RT mix share reagent_in through an off-chip rotary valve, so the line needs flushing "
            "between them. Generated by protocols/build_module3.py."
        ),
        "steps": steps,
    }


def report() -> None:
    w, h = TRAP["size"][1], TRAP["size"][2]
    print(f"trap {TRAP['size'][0]} x {w} x {h} mm = {V} uL")
    print(f"shear ceiling flow {max_flow(w, h):.3f} uL/min; used {FLOW} uL/min "
          f"({shear(FLOW, w, h):.4f} dyn/cm2)")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    protocol = build()
    OUT.write_text(json.dumps(protocol, indent=2) + "\n")
    total = sum(s["duration_s"] for s in protocol["steps"])
    print(f"wrote {OUT.name}: {len(protocol['steps'])} steps, {total / 3600:.1f} hours")

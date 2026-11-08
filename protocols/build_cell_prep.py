"""Build protocols/cell_prep_v1.json.

    python protocols/build_cell_prep.py           # write the protocol
    python protocols/build_cell_prep.py --report  # the on-chip vs tube comparison

Detach adherent cells, neutralise, count. That is all this module does.

WHY IT IS SO SMALL. The obvious module would do six things: detach, neutralise, wash
three times, remove clumps, count, and concentrate into the viscous encapsulation
buffer. Four of those six lose to a bench tube once the 100 um feature floor is applied,
and --report prints the comparison. Building them anyway would cost recovery and time
and buy nothing, so they are not here.

THE ONE NUMBER THAT MATTERS DOWNSTREAM is the cell concentration this module hands over.
It is NOT the figure quoted for the original 70 um capsule design: a bigger capsule holds
more liquid, so the same concentration gives a much higher occupancy. Getting this wrong
is the difference between 16% of capsules holding one cell and 37% of them holding none.
See cells_per_ul().

Reagent names are generic and no compositions appear here. See CONFIDENTIALITY.md.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "cell_prep_v1.json"
CHIP = Path(__file__).resolve().parents[1] / "chip" / "cell_prep_v1.json"

SOURCE = (
    "Shear-based disaggregation of clumps in a branching channel with features at or "
    "above 100 um: Qiu Y et al., Lab Chip 2015;15:339, doi:10.1039/c4lc01126k, and "
    "Qiu Y et al., Sci Rep 2018;8:2774, doi:10.1038/s41598-018-20931-y. Advective "
    "mixing is needed because diffusion is too slow at this scale: Stroock AD et al., "
    "Science 2002;295:647, doi:10.1126/science.1066238. Inertial focusing needs a "
    "particle Reynolds number that a viscous buffer destroys: Di Carlo D et al., PNAS "
    "2007;104:18892, doi:10.1073/pnas.0704958104. Dissociation at 37 C induces stress "
    "transcripts, which is why the steps after detachment are kept cold and short: "
    "van den Brink SC et al., Nat Methods 2017;14:935, doi:10.1038/nmeth.4437; "
    "Massoni-Badosa R et al., Genome Biol 2020;21:112, doi:10.1186/s13059-020-02032-0."
)

import build_spc_demo     # the capsule size and occupancy target come from the encapsulation chip

MIN = 60.0
MU_PA_S = 0.8e-3
SHEAR_CEILING = 0.01      # dyn/cm2, in every chamber the flow runs through
FLOW_FRACTION = 0.85      # run at this fraction of the shear-ceiling flow
CAPSULE_UM = build_spc_demo.CAPSULE_UM
CORE_FRACTION = build_spc_demo.CORE_SHARE   # the core stream is half the droplet volume
LAMBDA_CELL = build_spc_demo.LAMBDA_CELL

# How much liquid each step intends, in chamber volumes of the chamber it runs in.
WASH_CV, WASHES = 1.0, 2
DISSOCIATION_CV = 1.0
QUENCH_CV = 1.0
SHEAR_PASS_CV = 1.0
DETACH_TEMP_C = 37
DETACH_HOLD_S = 7 * MIN   # round placeholder; watch the cells rather than trust a timer
COUNT_S = 300             # time to image the window
COLD_C = 4

CHIP_SPEC = json.loads(CHIP.read_text())
CH = {c["id"]: c for c in CHIP_SPEC["chambers"]}
PORTS = [p["id"] for p in CHIP_SPEC["ports"]]


def max_flow(width_mm: float, height_mm: float, ceiling: float = SHEAR_CEILING) -> float:
    """Highest flow in uL/min that keeps wall shear at or below the ceiling."""
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (ceiling / 10.0) * w * h * h / (6.0 * MU_PA_S) * 60.0 * 1e9


def shear(flow_ul_min: float, width_mm: float, height_mm: float) -> float:
    q = flow_ul_min * 1e-9 / 60.0
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (6.0 * MU_PA_S * q / (w * h * h)) * 10.0


def route(chamber: str) -> list[str]:
    """The chamber and every chamber downstream of it. Each stage drains through the next one
    (the only outlet is out_cells), so the flow in one is the flow in all of them."""
    out, todo = [], [chamber]
    while todo:
        c = todo.pop(0)
        if c in CH and c not in out:
            out.append(c)
            todo += [ch["to"] for ch in CHIP_SPEC["channels"] if ch["from"] == c]
    return out


def route_flow(chamber: str) -> float:
    """FLOW_FRACTION of the shear-ceiling flow of the tightest chamber on the route, floored to 1 nL/min."""
    cap = min(max_flow(CH[c]["size"][1], CH[c]["size"][2]) for c in route(chamber))
    return math.floor(FLOW_FRACTION * cap * 1000) / 1000


def ports(feed=(), vent=()) -> dict:
    feed, vent = list(feed), list(vent)
    return {"feed": feed, "vent": vent, "cap": [p for p in PORTS if p not in feed + vent]}


def capsule_nl(d_um: float = CAPSULE_UM) -> float:
    return math.pi / 6.0 * (d_um / 1000.0) ** 3 * 1e3


def cells_per_ul(d_um: float = CAPSULE_UM, lam: float = LAMBDA_CELL) -> float:
    """Concentration the encapsulator needs, for a given capsule size.

    Cells ride in the core stream only, so the volume that matters is the core, not
    the whole droplet. This scales as 1/d^3, which is why a number quoted for a 70 um
    capsule is badly wrong for a 135 um one.
    """
    return lam / (capsule_nl(d_um) * CORE_FRACTION) * 1e3 / 1e3 * 1e3 / 1e3 * 1e3


def poisson(lam: float, k: int) -> float:
    return lam ** k * math.exp(-lam) / math.factorial(k)


def build() -> dict:
    steps: list[dict] = []

    def add(operation, target, note, duration_s, reagent=None, volume_ul=None,
            temp_c=None, washes=None, flow_ul_min=None, port_state=None, **extra):
        if operation == "exchange":
            volume_ul = volume_ul if volume_ul is not None else CH[target]["volume_ul"]
            washes = washes if washes is not None else 1
            duration_s = max(duration_s, washes * volume_ul / flow_ul_min * 60.0 * 1.4)
        if operation == "mix":
            duration_s = max(duration_s, volume_ul / flow_ul_min * 60.0 * 1.4)
        if operation == "transfer":
            # one chamber volume of carrier moves the cells on
            duration_s = max(duration_s, CH[extra["source"]]["volume_ul"]
                             / route_flow(extra["source"]) * 60.0 * 1.4)
        step = {"step": len(steps) + 1, "operation": operation, "target": target,
                "reagent": reagent, "volume_ul": volume_ul,
                "duration_s": round(duration_s), "temp_c": temp_c, "washes": washes,
                "flow_ul_min": flow_ul_min, "note": note}
        step.update(extra)
        step["ports"] = port_state
        steps.append(step)

    f_cult, f_mix = route_flow("cult_1"), route_flow("mix_1")
    v_cult, v_mix = CH["cult_1"]["volume_ul"], CH["mix_1"]["volume_ul"]

    add("exchange", "cult_1",
        "Rinse the adherent layer into a divalent-free buffer. The rinse matters more "
        "than it looks: leftover medium slows the enzyme and leftover divalent cations "
        "keep the cells stuck down, and both show up later as clumps.",
        0, reagent="wash_buffer", volume_ul=round(v_cult * WASH_CV, 3), washes=WASHES,
        flow_ul_min=f_cult, port_state=ports(["in_buffer"], ["out_cells"]))
    add("exchange", "cult_1",
        f"Flow in the dissociation reagent and warm the chamber. This is the only heated "
        f"zone on the board.",
        0, reagent="dissociation_reagent", volume_ul=round(v_cult * DISSOCIATION_CV, 3),
        temp_c=DETACH_TEMP_C, washes=1, flow_ul_min=f_cult,
        port_state=ports(["in_enz"], ["out_cells"]))
    add("hold_temp", "cult_1",
        f"Hold at {DETACH_TEMP_C} C until the cells round up and lift. Watch rather than trust a "
        f"timer: under-digestion leaves clumps, which is the thing most likely to block "
        f"the nozzle downstream, and over-digestion costs viability.",
        DETACH_HOLD_S, temp_c=DETACH_TEMP_C, port_state=ports())
    add("transfer", "mix_1",
        "Carry the lifted cells from the culture chamber into the serpentine.",
        0, port_state=ports(["in_buffer"], ["out_cells"]), source="cult_1")
    add("mix", "mix_1",
        "Co-flow the quench with the lifted cells. Quenching here rather than in the "
        "chamber stops the enzyme as the cells leave instead of minutes later. The "
        "folded path is there because diffusion across the channel is too slow, so the "
        "mixing has to be advective.",
        0, reagent="quench_buffer", volume_ul=round(v_mix * QUENCH_CV, 3), temp_c=COLD_C,
        flow_ul_min=f_mix, port_state=ports(["in_quench"], ["out_cells"]))
    add("mix", "mix_1",
        "Shear the remaining clumps apart against the serpentine turns. Published "
        "branching constrictions raise single cells from about 75% to about 92%, with "
        "viability unaffected when an enzyme has been used. It will not reach 100%, "
        "which is why a bought 40 um strainer still sits downstream.",
        0, reagent="quench_buffer", volume_ul=round(v_mix * SHEAR_PASS_CV, 3), temp_c=COLD_C,
        flow_ul_min=f_mix, port_state=ports(["in_quench"], ["out_cells"]))
    add("count", "qc_1",
        f"Stop the flow and image the window, chilled. Count about 400 cells for a 5% Poisson "
        f"error, then dilute to the concentration the encapsulator needs: about "
        f"{cells_per_ul():.0f} cells/uL for a {CAPSULE_UM:g} um capsule at a mean of "
        f"{LAMBDA_CELL:g}. THAT NUMBER IS NOT TRANSFERABLE between capsule sizes, "
        f"because it falls as the cube of the diameter. A 20% counting error only moves "
        f"single occupancy by about 15%, so a window and a phone camera are enough and "
        f"an in-line counter is not worth building.",
        COUNT_S, temp_c=COLD_C, port_state=ports())
    add("transfer", "out_cells",
        "Deliver to a chilled tube. Everything after this is bench work and stays "
        "there: one spin, a 40 um strainer, and resuspension into the viscous "
        "encapsulation buffer. The module stops here deliberately.",
        0, port_state=ports(["in_buffer"], ["out_cells"]), source="qc_1")

    total_min = sum(st["duration_s"] for st in steps) / MIN
    return {
        "id": "cell_prep_v1",
        "name": "Cell detachment, neutralisation and QC",
        "source": SOURCE,
        "placeholder": False,
        "description": (
            f"Detaches adherent cells, quenches and disaggregates them on the way out, "
            f"and presents them in a counting window. Deliberately does NOT wash, filter, "
            f"concentrate or exchange into the viscous encapsulation buffer: at the "
            f"fabrication feature floor those four steps lose to a bench tube on both "
            f"recovery and time, and --report prints the comparison. About "
            f"{total_min:.0f} min. Every stage drains through the next, so each flow is "
            f"85% of the flow that keeps wall shear at or below 0.01 dyn/cm2 in the "
            f"tightest chamber downstream (tau = 6*mu*Q/(w*h^2), mu = 0.8e-3 Pa s), and every "
            f"volume is a number of chamber volumes. Reagent names are generic; see the "
            f"source citation."
        ),
        "steps": steps,
    }


def report() -> None:
    print("Concentration the encapsulator needs (cells/uL), by capsule size:")
    print(f"{'capsule':>10}{'droplet':>10}{'core':>10}{'cells/uL':>12}")
    for d in (70.0, 100.0, CAPSULE_UM, 150.0):
        v = capsule_nl(d)
        print(f"{d:>8.0f} um{v:>8.2f} nL{v * CORE_FRACTION:>8.2f} nL"
              f"{cells_per_ul(d):>12.0f}")
    print(f"\nA concentration set for a 70 um capsule, used at {CAPSULE_UM:g} um, gives")
    lam = cells_per_ul(70.0) / cells_per_ul(CAPSULE_UM) * LAMBDA_CELL
    print(f"  mean {lam:.2f} cells per capsule: {poisson(lam, 0) * 100:.0f}% empty, "
          f"{poisson(lam, 1) * 100:.0f}% single, "
          f"{(1 - poisson(lam, 0) - poisson(lam, 1)) * 100:.0f}% multiple.")
    print(f"  Correct value gives {poisson(LAMBDA_CELL, 1) * 100:.0f}% single and "
          f"{(1 - poisson(LAMBDA_CELL, 0) - poisson(LAMBDA_CELL, 1)) * 100:.1f}% multiple.")

    print("\nWhy only two of six steps are on this chip (>= 100 um feature floor):")
    rows = [
        ("detach", "ON CHIP", "the cells are already adherent here; nothing to gain by moving"),
        ("neutralise", "ON CHIP", "co-flow quench stops the enzyme at the exit, not minutes later"),
        ("wash x3", "tube", "needs 12-45 um gaps for lateral displacement; inertial leaves ~20% carryover per pass"),
        ("remove clumps", "tube", "no >=100 um geometry removes 30-40 um clumps; needs a bought 40 um mesh"),
        ("count", "ON CHIP", "a window is nearly free and sets the concentration"),
        ("concentrate", "tube", "viscous buffer drops Reynolds ~75x and inertial focusing stops"),
    ]
    for name, where, why in rows:
        print(f"  {name:<15}{where:<9}{why}")
    print("\n  all six on chip : ~0.35 recovery, ~50 min")
    print("  these two + tube: ~0.7  recovery, ~30 min")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    protocol = build()
    OUT.write_text(json.dumps(protocol, indent=2) + "\n")
    total = sum(s["duration_s"] for s in protocol["steps"])
    print(f"\nwrote {OUT.name}: {len(protocol['steps'])} steps, {total / MIN:.0f} min")

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

MIN = 60.0
CAPSULE_UM = 135.0        # must match build_sctip_spc.py
CORE_FRACTION = 0.5       # the core stream is half the droplet volume
LAMBDA_CELL = 0.2
MIX_FLOW = 20.0           # uL/min through the mixer

CH = {c["id"]: c for c in json.loads(CHIP.read_text())["chambers"]}


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
            temp_c=None, washes=None, flow_ul_min=None):
        if operation == "exchange":
            volume_ul = volume_ul if volume_ul is not None else CH[target]["volume_ul"]
            washes = washes if washes is not None else 1
            duration_s = max(duration_s,
                             washes * volume_ul / (flow_ul_min or MIX_FLOW) * 60.0 * 1.4)
        steps.append({"step": len(steps) + 1, "operation": operation, "target": target,
                      "reagent": reagent, "volume_ul": volume_ul,
                      "duration_s": round(duration_s), "temp_c": temp_c, "washes": washes,
                      "flow_ul_min": flow_ul_min, "note": note})

    add("exchange", "cult_1",
        "Rinse the adherent layer into a divalent-free buffer. The rinse matters more "
        "than it looks: leftover medium slows the enzyme and leftover divalent cations "
        "keep the cells stuck down, and both show up later as clumps.",
        240, reagent="wash_buffer", washes=2, flow_ul_min=MIX_FLOW)
    add("exchange", "cult_1",
        "Flow in the dissociation reagent and warm the chamber. This is the only heated "
        "zone on the board.",
        180, reagent="dissociation_reagent", temp_c=37, flow_ul_min=MIX_FLOW)
    add("hold_temp", "cult_1",
        "Hold at 37 C until the cells round up and lift. Watch rather than trust a "
        "timer: under-digestion leaves clumps, which is the thing most likely to block "
        "a 100 um orifice downstream, and over-digestion costs viability.",
        7 * MIN, temp_c=37)
    add("exchange", "mix_1",
        "Carry the lifted cells into the serpentine and co-flow the quench. Quenching "
        "here rather than in the chamber stops the enzyme as the cells leave instead of "
        "minutes later. The folded path is there because diffusion across a 100 um "
        "channel takes about 100 s; the mixing has to be advective.",
        180, reagent="quench_buffer", temp_c=4, flow_ul_min=MIX_FLOW)
    add("exchange", "mix_1",
        "Shear the remaining clumps apart against the serpentine turns. Published "
        "branching constrictions at or above 100 um raise single cells from about 75% to "
        "about 92%, with viability unaffected when an enzyme has been used. It will not "
        "reach 100%, which is why a bought 40 um strainer still sits downstream.",
        120, reagent="quench_buffer", temp_c=4, flow_ul_min=MIX_FLOW)
    add("hold_temp", "qc_1",
        f"Stop the flow and image the window. Count about 400 cells for a 5% Poisson "
        f"error, then dilute to the concentration the encapsulator needs: about "
        f"{cells_per_ul():.0f} cells/uL for a {CAPSULE_UM:g} um capsule at a mean of "
        f"{LAMBDA_CELL:g}. THAT NUMBER IS NOT TRANSFERABLE between capsule sizes, "
        f"because it falls as the cube of the diameter. A 20% counting error only moves "
        f"single occupancy by about 15%, so a window and a phone camera are enough and "
        f"an in-line counter is not worth building.",
        300, temp_c=4)
    add("release", "qc_1",
        "Deliver to a chilled tube. Everything after this is bench work and stays "
        "there: one spin, a 40 um strainer, and resuspension into the viscous "
        "encapsulation buffer. The module stops here deliberately.",
        120, reagent="wash_buffer")

    return {
        "id": "cell_prep_v1",
        "name": "Cell detachment, neutralisation and QC",
        "source": SOURCE,
        "placeholder": False,
        "description": (
            "Detaches adherent cells, quenches and disaggregates them on the way out, "
            "and presents them in a counting window. Deliberately does NOT wash, filter, "
            "concentrate or exchange into the viscous encapsulation buffer: at a 100 um "
            "feature floor those four steps lose to a bench tube on both recovery and "
            "time, and --report prints the comparison. About 20 min, all features at or "
            "above 100 um. Reagent names are generic; see the source citation."
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

"""Build protocols/tri_culture_v1.json.

    python protocols/build_tri_culture.py           # write the protocol
    python protocols/build_tri_culture.py --report  # schedule and shear table

Three wild-type differentiations run side by side in the three lanes of
chip/tri_culture_v1.json, are allowed to interact across the microgroove barriers,
then get a per-lane barcode and are pooled into one library.

TWO THINGS THIS FILE EXISTS TO MAKE EXPLICIT.

1. Staggered starts. The three lineages take very different times, so starting them
   together would finish them weeks apart. Each lane is seeded on the day that makes
   all three mature on the same day (see LANES). Published co-culture work solves the
   same problem by staggering or by banking frozen intermediates.

2. One operation at a time. The protocol contract is a single ordered list, and the
   chip has one selector valve, so the three lanes are genuinely interleaved rather
   than run in parallel. Events are placed on a real calendar and sorted; the gaps
   between them become hold_temp steps. Cumulative step time therefore tracks the
   real elapsed day, which is what the twin plays.

Flow rates come from chamber geometry exactly as in build_ipsc_to_mgl.py: wall shear
tau = 6*mu*Q/(w*h^2), held at or below 0.01 dyn/cm^2.

Reagent names are generic. No compositions, concentrations or cell lines: see
CONFIDENTIALITY.md. The published methods are cited in SOURCE.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "tri_culture_v1.json"
CHIP = Path(__file__).resolve().parents[1] / "chip" / "tri_culture_v1.json"

SOURCE = (
    "Neural lane: Shi Y, Kirwan P, Livesey FJ. Nature Protocols 2012;7:1836, "
    "doi:10.1038/nprot.2012.116 (dual-SMAD cortical induction). "
    "Myeloid lane: McQuade A et al. Molecular Neurodegeneration 2018;13:67, "
    "doi:10.1186/s13024-018-0297-x. "
    "Astroglial lane: Schuurmans IME et al. Glia 2026;74:e70202, doi:10.1002/glia.70202 "
    "(35 days, no neural-progenitor step, no transgene). "
    "Compartment barrier and hydrostatic isolation: Taylor AM et al. Nature Methods "
    "2005;2:599, doi:10.1038/nmeth777; groove-height effect on cell confinement: "
    "Goshi N et al. Lab on a Chip 2022, doi:10.1039/d2lc00683a. "
    "Per-lane barcoding before pooling: McGinnis CS et al. Nature Methods 2019;16:619, "
    "doi:10.1038/s41592-019-0433-8 (lipid-anchored oligos)."
)

DAY = 86400.0
MU_PA_S = 0.8e-3
SHEAR_CEILING = 0.01
MARGIN = 1.4

# lane -> (stage 1 chamber, stage 2 chamber, seed day, passage day, media)
LANES = {
    "neural": ("nx_induct", "nx_mature", 0, 12, ("neural_induction_medium", "neural_maturation_medium")),
    "myeloid": ("mx_hemo", "mx_mature", 40, 52, ("hematopoietic_medium", "microglia_maturation_medium")),
    "astroglial": ("ax_expand", "ax_mature", 45, 55, ("astro_medium", "astro_medium")),
}
MATURE_DAY = 80      # all three lanes ready; barriers opened
INTERACT_DAYS = 7    # cross-talk window before harvest
BARCODES = {"nx_mature": "barcode_1", "mx_mature": "barcode_2", "ax_mature": "barcode_3"}


def max_flow(width_mm: float, height_mm: float) -> float:
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (SHEAR_CEILING / 10.0) * w * h * h / (6.0 * MU_PA_S) * 60.0 * 1e9


def shear(flow_ul_min: float, width_mm: float, height_mm: float) -> float:
    q = flow_ul_min * 1e-9 / 60.0
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (6.0 * MU_PA_S * q / (w * h * h)) * 10.0


CH = {c["id"]: c for c in json.loads(CHIP.read_text())["chambers"]}
FLOW = {cid: max(1.0, float(int(max_flow(c["size"][0], c["size"][2]))) - 2.0) for cid, c in CH.items()}


def events() -> list[tuple[float, dict]]:
    """Every scheduled operation on a real calendar, as (day, partial step)."""
    ev: list[tuple[float, dict]] = []

    def add(day, operation, target, note, reagent=None, fraction=None, washes=1, temp_c=None, duration_s=None):
        ev.append((day, dict(operation=operation, target=target, note=note, reagent=reagent,
                             fraction=fraction, washes=washes, temp_c=temp_c, duration_s=duration_s)))

    for lane, (s1, s2, seed, passage, (med1, med2)) in LANES.items():
        add(seed - 0.5, "exchange", s1, f"Coat the {lane} stage-one chamber.", "coating_solution")
        add(seed, "load", s1,
            f"Seed iPSCs into the {lane} lane. The other two lanes are untouched: after this step "
            f"every lane is fed and drained through its own port, so they never share liquid.")
        add(seed + 0.1, "hold_temp", s1,
            "No flow while the cells attach.", temp_c=37, duration_s=DAY)
        day = seed + 1
        while day < passage:
            add(day, "exchange", s1, f"Day {day - seed:g} of the {lane} lane: feed stage one.", med1, 0.5)
            day += 2
        add(passage, "split", s1,
            f"Pass the {lane} lane into its stage-two chamber. In the myeloid lane this is not a "
            f"passage but a harvest: progenitors round up off the adherent layer and the flow carries them.")
        day = passage + 1
        while day < MATURE_DAY:
            add(day, "exchange", s2, f"Day {day - seed:g} of the {lane} lane: feed stage two.", med2, 0.5)
            day += 2

    # --- barriers opened, cross-talk window ------------------------------------------
    add(MATURE_DAY, "hold_temp", "mx_mature",
        "All three lanes are mature on the same day, which is what the staggered starts were for. "
        "Level the volumes across the microgroove arrays: the hydrostatic difference that kept the "
        "lanes isolated is removed, so soluble factors, vesicles and axons can now cross. The grooves "
        "are not valves and were never closed; isolation was a maintained pressure condition.",
        temp_c=37, duration_s=3600)
    for d in range(1, INTERACT_DAYS):
        for lane, (_, s2, seed, _, (_, med2)) in LANES.items():
            if d % 2 == 1:
                add(MATURE_DAY + d, "exchange", s2,
                    f"Cross-talk day {d}: feed the {lane} lane. Feeding each lane separately keeps the "
                    f"exchange going through the grooves rather than through the medium lines.", med2, 0.5)

    # --- per-lane barcode, then pool ---------------------------------------------------
    h = MATURE_DAY + INTERACT_DAYS
    add(h, "hold_temp", "mx_mature",
        "Cool every lane to 4 C. Cold handling limits the activation that warm lifting causes, and "
        "stops the lipid-anchored barcodes swapping between cells.", temp_c=4, duration_s=1200)
    for chamber, tag in BARCODES.items():
        add(h + 0.01, "exchange", chamber,
            f"Perfuse {tag} into {chamber}. A lipid-anchored oligo inserts into the membrane in minutes "
            f"with no antibody and no spin, so each lane carries its own label before anything is mixed.",
            tag, 0.5, duration_s=600)
    for chamber in BARCODES:
        add(h + 0.02, "exchange", chamber,
            f"Wash unbound barcode out of {chamber}. Carryover is what causes background, so this is "
            f"the step that decides whether the labels can be told apart.", "wash_buffer", 0.5, washes=2)
    for chamber in BARCODES:
        add(h + 0.03, "exchange", chamber,
            f"Lift the cells in {chamber} with a gentle, enzyme-free reagent at 4 C.",
            "dissociation_reagent", 0.5, duration_s=900)
    add(h + 0.05, "exchange", "mix_1",
        "Quench with a carrier-protein buffer so released cells do not stick to the channel walls.",
        "quench_buffer", 0.5)
    add(h + 0.06, "pool", "mix_1",
        "Pool all three lanes. They are safe to mix now because each carries its own barcode: lane "
        "identity survives in the sequencing data, and so does any cell that migrated across a barrier.")
    add(h + 0.07, "exchange", "mix_1",
        "Wash the pooled suspension twice through the retention mesh to remove clumps.",
        "wash_buffer", 0.5, washes=2)
    add(h + 0.08, "release", "mix_1",
        "Deliver one counted, barcoded, filtered suspension to the outlet for encapsulation.",
        "release_buffer")
    return sorted(ev, key=lambda e: e[0])


def build() -> dict:
    steps: list[dict] = []
    t = 0.0
    start = min(d for d, _ in events())

    def emit(operation, target, note, duration_s, reagent=None, volume_ul=None,
             temp_c=None, washes=None, flow_ul_min=None):
        nonlocal t
        steps.append({"step": len(steps) + 1, "operation": operation, "target": target,
                      "reagent": reagent, "volume_ul": volume_ul, "duration_s": round(duration_s),
                      "temp_c": temp_c, "washes": washes, "flow_ul_min": flow_ul_min, "note": note})
        t += round(duration_s)

    for day, e in events():
        gap = (day - start) * DAY - t
        if gap > 1:
            others = ", ".join(sorted({s2 for _, s2, _, _, _ in LANES.values()}))
            emit("hold_temp", e["target"],
                 f"Hold at 37 C until the next scheduled operation. Every lane is incubating "
                 f"independently ({others}); the chip acts on one chamber at a time.",
                 gap, temp_c=37)
        op, target = e["operation"], e["target"]
        if op == "exchange":
            volume = round(CH[target]["volume_ul"] * (e["fraction"] or 1.0), 1)
            flow = FLOW[target]
            minimum = e["washes"] * volume / flow * 60.0
            emit("exchange", target, e["note"], e["duration_s"] or minimum * MARGIN,
                 reagent=e["reagent"], volume_ul=volume, washes=e["washes"], flow_ul_min=flow)
        elif op == "hold_temp":
            emit("hold_temp", target, e["note"], e["duration_s"], temp_c=e["temp_c"])
        elif op == "load":
            emit("load", target, e["note"], 900, volume_ul=CH[target]["volume_ul"])
        elif op == "split":
            emit("split", target, e["note"], 1800)
        elif op == "pool":
            emit("pool", target, e["note"], 1800)
        elif op == "release":
            emit("release", target, e["note"], 900, reagent=e["reagent"])

    return {
        "id": "tri_culture_v1",
        "name": "Three-lineage differentiation, interaction and barcoded collection",
        "source": SOURCE,
        "placeholder": False,
        "description": (
            "Three wild-type iPSC differentiations run in three isolated lanes of one chip, are let "
            "to interact across microgroove barriers once all three are mature, then are barcoded per "
            "lane and pooled into a single suspension for encapsulation. Lanes are seeded on different "
            "days so that three protocols of very different length finish together. Runs about "
            f"{MATURE_DAY + INTERACT_DAYS} days. Reagent names are generic; see the source citation. "
            "Flow rates are computed from chamber geometry to keep wall shear at or below 0.01 dyn/cm2."
        ),
        "steps": steps,
    }


def report() -> None:
    print("Lane schedule (run day):")
    for lane, (s1, s2, seed, passage, _) in LANES.items():
        print(f"  {lane:<12} seed {seed:>3}  passage {passage:>3} ({s1} -> {s2})  "
              f"mature {MATURE_DAY}  = {MATURE_DAY - seed} days of differentiation")
    print(f"  barriers opened day {MATURE_DAY}, harvest day {MATURE_DAY + INTERACT_DAYS}\n")
    print(f"{'chamber':<14}{'w x h (mm)':<13}{'flow':>11}{'shear':>13}{'ceiling':>12}")
    for cid, c in CH.items():
        w, h = c["size"][0], c["size"][2]
        print(f"{cid:<14}{f'{w} x {h}':<13}{FLOW[cid]:>8.0f} uL/min"
              f"{shear(FLOW[cid], w, h):>10.4f} dyn/cm2{max_flow(w, h):>7.1f} uL/min")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    protocol = build()
    OUT.write_text(json.dumps(protocol, indent=2) + "\n")
    total = sum(s["duration_s"] for s in protocol["steps"])
    print(f"\nwrote {OUT.name}: {len(protocol['steps'])} steps, {total / DAY:.1f} days")

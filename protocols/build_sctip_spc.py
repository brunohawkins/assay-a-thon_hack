"""Build protocols/sctip_spc_v1.json.

    python protocols/build_sctip_spc.py           # write the protocol
    python protocols/build_sctip_spc.py --report  # timing and flow table

Capsule generation plus single-copy barcoding, on chip/sctip_spc_v1.json.

WHAT THIS REPLACES. A published single-cell chromatin workflow (TIP-seq, Bartlett et al.
2021) barcodes cells by splitting them across plates: one barcode on the transposon
adapter, then further barcodes added in separate PCR rounds. That needs plate handling
and a lot of centrifugation. Here each capsule instead gets ONE random barcode molecule
from a large pool, which is clonally amplified inside an oil droplet and joined to that
cell's own tagmented fragments, after AmpliDrop (Garcia-Bassets et al. 2024). One step,
no plates.

THE THING THAT MAKES IT WORK. The amplified barcode copies are short, and the capsule
shell only retains DNA above about 300 bp, so loose copies would leak and cross-seed
every other capsule. They never get the chance: amplification happens while the capsule
is sealed inside an oil droplet, and the same reaction joins the barcode onto the
fragment by overlap extension. By the time the emulsion is broken the barcode is part of
a long molecule the capsule holds on to. The emulsion is the containment, not the shell,
and not an anchor molecule.

WHY THE CAPSULES ARE BIG. Close-packed re-injection is what beats Poisson on the capsule
side, and it needs an entrance about 0.85x the capsule diameter. At 70 um that entrance
is 60 um, which we cannot make. At 135 um it is 115 um, which we can. Capsule size is set
by the fabrication limit, not by biology.

TEMPERATURE. Nothing above 72 C ever sees an intact capsule in this workflow: the cells
are permeabilised rather than live, tagmentation runs at room temperature, and the
amplification happens on free DNA. Thermocycling is done in a tube, because on-chip is no
faster and invites evaporation and bubbles.

Reagent names are generic and no compositions appear here. See CONFIDENTIALITY.md. The
published methods are cited in SOURCE.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "sctip_spc_v1.json"
CHIP = Path(__file__).resolve().parents[1] / "chip" / "sctip_spc_v1.json"

SOURCE = (
    "Chromatin workflow and the T7-promoter transposon: Bartlett DA et al., "
    "J Cell Biol 2021;220:e202103078, doi:10.1083/jcb.202103078 (TIP-seq; in-situ "
    "tagmentation in permeabilised cells, then T7 linear amplification). "
    "Single-copy barcode clonally amplified in a compartment and joined by overlap "
    "extension: Garcia-Bassets I et al., bioRxiv 2024, doi:10.1101/2024.08.13.607508 "
    "(AmpliDrop). Semipermeable core-shell capsules, the ~300 bp retention cutoff and "
    "capsule survival of thermocycling: Leonaviciene G & Mazutis L, Nucleic Acids Res "
    "2023;51:e2, doi:10.1093/nar/gkac918, and Baronas D et al., Science 2026;391:1138, "
    "doi:10.1126/science.ady7227. Close-packed re-injection of hydrogel particles for "
    "single occupancy: Du X et al., Anal Chem 2025, doi:10.1021/acs.analchem.5c05488; "
    "Clark IC & Abate AR, Lab Chip 2018, doi:10.1039/C8LC00514A. Overlap extension: "
    "Horton RM et al., Gene 1989;77:61, doi:10.1016/0378-1119(89)90359-4."
)

MIN = 60.0
HOUR = 3600.0

# Stage A: scaled from a documented 70 um operating point by orifice area, which keeps
# the oil-to-aqueous ratio at 3.67 and the junction in the dripping regime.
FLOW_A = {"shell": 1.5, "core": 1.5, "oil": 11.0}      # uL/min
# Stage B: capsule stream, reagent-plus-barcode stream, oil.
FLOW_B = {"caps": 4.2, "mix": 6.7, "oil": 16.7}        # uL/min

CAPSULE_UM = 135.0
CAPSULE_NL = 3.14159265 / 6.0 * (CAPSULE_UM / 1000.0) ** 3 * 1e3   # nL
LAMBDA_CELL = 0.2        # mean cells per capsule
LAMBDA_BARCODE = 3.0     # mean barcode molecules per droplet; see note in build()
TARGET_CAPSULES = 50000

CH = {c["id"]: c for c in json.loads(CHIP.read_text())["chambers"]}
# Packed volume of the whole batch: capsule volume x count, over random close packing.
PACKED_UL = round(TARGET_CAPSULES * CAPSULE_NL / 1e3 / 0.64, 1)


def rate_hz() -> float:
    """Droplet frequency from the aqueous flow and the capsule volume."""
    aqueous_nl_s = (FLOW_A["shell"] + FLOW_A["core"]) * 1e3 / MIN
    return aqueous_nl_s / CAPSULE_NL


def poisson(lam: float, k: int) -> float:
    import math
    return lam ** k * math.exp(-lam) / math.factorial(k)


def build() -> dict:
    steps: list[dict] = []

    def add(operation, target, note, duration_s, reagent=None, volume_ul=None,
            temp_c=None, washes=None, flow_ul_min=None):
        if operation == "exchange":
            volume_ul = volume_ul if volume_ul is not None else CH[target]["volume_ul"]
            washes = washes if washes is not None else 1
            # never promise an exchange faster than the pump can deliver it
            minimum = washes * volume_ul / (flow_ul_min or 4.0) * 60.0 * 1.4
            duration_s = max(duration_s, minimum)
        steps.append({"step": len(steps) + 1, "operation": operation, "target": target,
                      "reagent": reagent, "volume_ul": volume_ul,
                      "duration_s": round(duration_s), "temp_c": temp_c, "washes": washes,
                      "flow_ul_min": flow_ul_min, "note": note})

    gen_s = TARGET_CAPSULES / rate_hz()

    # --- stage A: make the capsules --------------------------------------------------
    add("load", "filt_core",
        "Load the prepared cell suspension. Cells arrive live from culture and are "
        "permeabilised, antibody-bound and transposase-bound in a tube before they reach "
        "the chip: those are bulk steps that gain nothing from being on a chip. They are "
        "permeabilised, NOT fixed and NOT live, which is what the published chromatin "
        "method needs and which removes the fixation conflict entirely.",
        300, reagent="cell_suspension", volume_ul=130, flow_ul_min=FLOW_A["core"])
    add("exchange", "filt_shell",
        "Prime the shell line. The guard filter has 100 um gaps and exists only to stop "
        "gel lumps reaching a 100 um orifice. It cannot strain cell clumps: a 100 um gap "
        "passes a clump of five cells, so cells are strained off-chip at 40 um first.",
        180, reagent="shell_polymer", volume_ul=20, flow_ul_min=FLOW_A["shell"])
    add("exchange", "nozzle_1",
        f"Run the flow-focusing cross. Shell and core meet, oil cuts them into droplets "
        f"at about {rate_hz():.0f} Hz, and the two aqueous polymers demix inside each "
        f"droplet so the shell polymer wets the oil interface and the core stays liquid. "
        f"{TARGET_CAPSULES:,} capsules of about {CAPSULE_UM:g} um take "
        f"{gen_s / MIN:.0f} min.",
        gen_s, reagent="carrier_oil", volume_ul=FLOW_A["oil"] * gen_s / MIN,
        flow_ul_min=FLOW_A["oil"])
    add("hold_temp", "delay_1",
        "Dark residence line. It is NOT a cure window. The phase separation that makes "
        "the shell takes minutes, and a serpentine that holds minutes at this flow would "
        "be metres long, so curing is done off-chip in the collection tube.",
        120, temp_c=22)
    add("release", "delay_1",
        "Release the emulsion to a sealed collection tube. Let it demix, then flood-cure "
        "at 405 nm. Keep the dose low and the oil degassed: dissolved oxygen in "
        "fluorinated oil inhibits the cure, and a high dose kills cells.",
        1200, reagent="photoinitiator")

    # --- stage B: one capsule, one barcode, one droplet ------------------------------
    add("load", "pack_1",
        "Re-inject the cured capsules close-packed. The entrance is 115 um against a "
        "135 um capsule, so capsules deform slightly and arrive in single file at a "
        "steady rate. This is the step that beats Poisson on the capsule: published "
        "work gets 93-97% single occupancy this way, against about 30% by chance.",
        300, reagent="capsule_suspension", volume_ul=PACKED_UL,
        flow_ul_min=FLOW_B["caps"])
    add("exchange", "pack_1",
        f"Meet the amplification mix carrying the barcode pool, diluted to about "
        f"{LAMBDA_BARCODE:g} molecules per droplet. One molecule per droplet would be "
        f"ideal but is unreachable: a free molecule cannot be ordered, so at a mean of 1 "
        f"about 37% of droplets would get none. At {LAMBDA_BARCODE:g} only "
        f"{poisson(LAMBDA_BARCODE, 0) * 100:.0f}% get none, and the droplets that get "
        f"several are resolved afterwards by merging barcodes that share fragments, "
        f"which is what the source method does.",
        240, reagent="amplification_mix", flow_ul_min=FLOW_B["mix"])
    add("exchange", "nozzle_2",
        "Cut each capsule plus its barcode back into its own oil droplet. From here "
        "until the emulsion is broken, every capsule is chemically alone.",
        gen_s, reagent="carrier_oil", flow_ul_min=FLOW_B["oil"])
    add("release", "nozzle_2",
        "Release the droplets to a tube for thermocycling. On-chip cycling is not faster "
        "than a tube at this scale and invites evaporation and bubbles, so only the steps "
        "that genuinely need a chip are on it.",
        600, reagent="carrier_oil")
    add("hold_temp", "nozzle_2",
        "Thermocycle in the tube. Each droplet clonally amplifies its own barcode and, in "
        "the same reaction, overlap-extends it onto that cell's tagmented fragments. "
        "THIS IS THE STEP THE WHOLE DESIGN EXISTS TO PROTECT: the barcode copies are "
        "short enough to leak through the shell, so they are kept inside an oil droplet "
        "until they are part of a long molecule the capsule retains.",
        2.0 * HOUR, temp_c=72)

    # --- stage C: break, trap, wash, transcribe --------------------------------------
    add("load", "brk_1",
        "Return the emulsion to the chip. The bench version of this step is where the "
        "published workflow loses most of its capsules, which is the single biggest "
        "reason this module exists.",
        300, reagent="emulsion", volume_ul=PACKED_UL * 2, flow_ul_min=4.0)
    add("exchange", "brk_1",
        "Separate the phases. Oil leaves through its own line; the capsules go on in the "
        "aqueous stream. Driven by a pressure difference across a wetted slot array, so "
        "it needs a controller rather than a fixed flow.",
        600, reagent="emulsion_breaker", volume_ul=CH["brk_1"]["volume_ul"], washes=1,
        flow_ul_min=2.0)
    add("exchange", "trap_1",
        "Settle the capsules into a packed bed. Retention is by chamber geometry and "
        "downward flow, not by a size gap: a liquid-core capsule deforms, and the gap "
        "that would reliably hold one is 70-80 um, below what we can make.",
        300, reagent="wash_buffer", volume_ul=CH["trap_1"]["volume_ul"], washes=1,
        flow_ul_min=4.0)
    for label in ("after the emulsion break", "before transcription"):
        add("exchange", "trap_1",
            f"Flow-through wash {label}, replacing a centrifugation. Exchange is limited "
            f"by diffusion into the capsule rather than by flushing, so each one takes a "
            f"few minutes however fast the pump runs.",
            300, reagent="wash_buffer", volume_ul=CH["trap_1"]["volume_ul"], washes=3,
            flow_ul_min=4.0)
    add("hold_temp", "trap_1",
        "Gap-fill. The highest temperature any capsule sees in the whole workflow, and it "
        "is well inside what the shell and the board tolerate.",
        3 * MIN, temp_c=72)
    add("exchange", "trap_1",
        "Load the transcription mix. The polymerase is small enough to cross the shell; "
        "the barcoded template is far too large to leave it.",
        300, reagent="transcription_mix", volume_ul=CH["trap_1"]["volume_ul"],
        flow_ul_min=4.0)
    add("hold_temp", "trap_1",
        "Linear amplification in place, overnight. Linear rather than exponential because "
        "a single transposon insertion is enough, errors do not propagate and coverage "
        "stays even. Held in an incubator with the ports sealed, not on a chip heater.",
        17 * HOUR, temp_c=37)
    add("exchange", "trap_1",
        "Final wash before recovery.",
        300, reagent="wash_buffer", volume_ul=CH["trap_1"]["volume_ul"], washes=3,
        flow_ul_min=4.0)
    add("release", "trap_1",
        "Back-flush the bed out of the recovery port. Reverse the flow on a slow ramp: "
        "the bed lifts at well under a pascal, so a sharp start throws capsules at the "
        "walls. Downstream library steps are bulk and off-chip.",
        600, reagent="release_buffer")

    return {
        "id": "sctip_spc_v1",
        "name": "Capsule generation and single-copy barcoding",
        "source": SOURCE,
        "placeholder": False,
        "description": (
            f"Makes about {TARGET_CAPSULES:,} core-shell capsules of {CAPSULE_UM:g} um "
            f"from permeabilised cells, gives each one a single-copy barcode from a large "
            f"pool, clonally amplifies and attaches that barcode inside an oil droplet, "
            f"then traps and washes the capsules on chip in place of about twenty "
            f"centrifugation steps. Replaces plate-based split-and-pool barcoding with "
            f"one step. Every feature on the chip is at or above 100 um, so the board is "
            f"hot-embossed in a single depth with no photolithography. Reagent names are "
            f"generic; see the source citation."
        ),
        "steps": steps,
    }


def report() -> None:
    import math
    print(f"capsule {CAPSULE_UM:g} um = {CAPSULE_NL:.2f} nL   "
          f"droplet rate {rate_hz():.0f} Hz = {rate_hz() * HOUR / 1000:.0f}k/h")
    print(f"{TARGET_CAPSULES:,} capsules in {TARGET_CAPSULES / rate_hz() / MIN:.0f} min\n")
    print(f"{'stream':<10}{'stage A':>12}{'stage B':>12}  (uL/min)")
    for k in ("shell", "core", "oil"):
        b = {"shell": "caps", "core": "mix", "oil": "oil"}[k]
        print(f"{k:<10}{FLOW_A[k]:>12.1f}{FLOW_B[b]:>12.1f}")
    print(f"\nloading, mean cells per capsule = {LAMBDA_CELL:g}")
    for k in (0, 1, 2):
        print(f"  exactly {k} cell(s): {poisson(LAMBDA_CELL, k) * 100:5.1f}%")
    occupied = 1 - poisson(LAMBDA_CELL, 0)
    print(f"  of occupied capsules, exactly one: "
          f"{poisson(LAMBDA_CELL, 1) / occupied * 100:.0f}%")
    print(f"\nbarcode, mean per droplet = {LAMBDA_BARCODE:g}")
    print(f"  none: {poisson(LAMBDA_BARCODE, 0) * 100:.1f}%   "
          f"one or more: {(1 - poisson(LAMBDA_BARCODE, 0)) * 100:.1f}%")
    n = 4 ** 16
    print(f"\nbarcode pool 16 random bases = {n:.2e}; sharing a barcode at "
          f"{TARGET_CAPSULES:,} capsules: {(1 - math.exp(-(TARGET_CAPSULES - 1) / n)) * 100:.4f}%")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    protocol = build()
    OUT.write_text(json.dumps(protocol, indent=2) + "\n")
    total = sum(s["duration_s"] for s in protocol["steps"])
    print(f"\nwrote {OUT.name}: {len(protocol['steps'])} steps, {total / HOUR:.1f} h")

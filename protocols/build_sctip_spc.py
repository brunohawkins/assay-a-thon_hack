"""Build protocols/sctip_spc_v1.json.

    python protocols/build_sctip_spc.py           # write the protocol
    python protocols/build_sctip_spc.py --report  # timing and flow table

Single-copy barcoding and capsule handling, on chip/sctip_spc_v1.json.

THIS IS THE PROJECT'S SINGLE-CELL BARCODING STAGE. It is not a duplicate of module 3 and
it is not a side experiment. It is the one place a per-CELL barcode is created, and it
sits between module 3 and module 4 in protocols/pipeline_mvp.json:

    module 3 (load, lysis, wash, reverse transcription, wash, release)
        -> IN:  washed capsules, each holding one cell's cDNA, no barcode on it yet
    barcoding (this file)
        -> OUT: washed capsules whose molecules each carry one clonal, per-capsule barcode
    module 4 (sublibrary split + index PCR)

The lane label the cells carry from module 1 and module 4's four sublibrary indices are
DEMULTIPLEXING conveniences, about twelve groups. They are not the resolution of the data.
The resolution comes from here: one capsule, one droplet, one clonal barcode.

WHAT IS ON THE CHIP. Stage A (making the capsules) is retired: spc_demo_v1 makes them, and
the capsules arrive here already made. What remains is stage B (re-inject the capsules,
meet the barcode mix, cut each capsule plus its barcode back into oil) and stage C (break
the emulsion, trap and wash the capsules). Nothing numeric about liquid is typed in:
the batch size is what the packed bed in trap_1 holds, volumes are chamber volumes, and
every flow is derived from chamber geometry. A step never moves more than its chamber
holds in one go, so a larger volume is several fills or several passes. The nozzle flows
are the spc_demo_v1 operating point scaled by the ratio of the two nozzles' cross sections.

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
is 60 um, which we cannot make. Capsule size is set by the fabrication limit, not by
biology: here it is whatever spc_demo_v1's nozzle makes (build_spc_demo.CAPSULE_UM).

TEMPERATURE. Nothing above 72 C ever sees an intact capsule in this workflow: the cells
are permeabilised rather than live, tagmentation runs at room temperature, and the
amplification happens on free DNA. Thermocycling is done in a tube, because on-chip is no
faster and invites evaporation and bubbles.

TWO WORKFLOWS IN ONE FILE, AND WHICH STEPS BELONG TO WHICH. These steps are NOT all one
workflow and they are deliberately not merged:

  * SHARED SINGLE-CELL BARCODING STAGE - steps 1-16 and 20-21. Stage B (steps 1-3: re-inject
    close-packed, pair each capsule with the barcode pool, cut into oil, thermocycle in a
    tube) plus the first half of stage C (steps 4-16: break the emulsion pass by pass,
    settle the bed, two flow-through washes) and its tail (steps 20-21: final wash,
    back-flush release). Every workflow runs these, because this is what puts a per-cell
    barcode on the molecules.
  * CHROMATIN / TRANSPOSASE VARIANT TAIL - steps 17-19 only (gap-fill at 72 C, load the
    transcription mix, then the overnight 37 C LINEAR amplification in the trap). These
    finish a transposon-tagmented DNA library in place and belong to the TIP-seq flavour.
    The RNA pipeline in protocols/pipeline_mvp.json does NOT run them: module 3's reverse
    transcription has already made the template, and module 4's index PCR does the
    amplifying. They are kept here, labelled, rather than silently folded into the shared
    stage or deleted.

FABRICATION, PLAINLY. None of the droplet handling is printable. Stage B needs a 115 um
close-packed entrance (0.85 x a 135 um capsule) and stage C needs 100 x 150 um slots, and
our in-plane floor is 200 um for open channels. So, exactly as with encapsulation, the
droplet-handling steps run on a COMMERCIAL DROPLET KIT and chip/sctip_spc_v1.json is a
demo-scale layout, not a printable part: its pack_1 is clamped to 200 um, which is wider
than a capsule and therefore cannot order them single file. The split is coherent and is
the position we take: the barcoding CHEMISTRY is ours and is the single-cell mechanism;
the droplet HARDWARE is bought. The only piece we do fabricate ourselves is capsule
retention, as a depth weir with a 25 um gap under the lid (crest = the chip's etch depth
minus the gap) - the 200 um floor is IN-PLANE and z is free.

Reagent names are generic and no compositions appear here. See CONFIDENTIALITY.md. The
published methods are cited in SOURCE.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import build_spc_demo     # capsule size and the reference nozzle operating point

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
MU_PA_S = build_spc_demo.MU_PA_S          # 37 C medium, from contracts/parameters.json
SHEAR_CEILING = 0.01       # dyn/cm2, in every chamber the flow runs through
FLOW_FRACTION = 0.85       # run at this fraction of the shear-ceiling flow
PACKING = 0.64             # random close packing
CAPS_SHARE = 0.4           # share of the nozzle's aqueous flow carried by the capsule stream
EMULSION_FACTOR = 2.0      # emulsion volume re-injected, in packed capsule volumes (assumption)
LAMBDA_BARCODE = 3.0       # mean barcode molecules per droplet; see note in build()
SETTLE_WASHES = 1
WASH_WASHES = 3            # fills per flow-through wash
COLLECT_S = 600            # handling time to the tube
THERMOCYCLE_S = 2.0 * HOUR # round placeholder, in a tube
GAP_FILL_S = 3 * MIN
GAP_FILL_C = 72
TRANSCRIPTION_S = 17 * HOUR
TRANSCRIPTION_C = 37

CHIP_SPEC = json.loads(CHIP.read_text())
CH = {c["id"]: c for c in CHIP_SPEC["chambers"]}
PORTS = [p["id"] for p in CHIP_SPEC["ports"]]
NOZZLE, PACK, BREAK, TRAP = "nozzle_2", "pack_1", "brk_1", "trap_1"

# The capsules that arrive here are the PIPELINE's capsules, made off-chip on the bought
# droplet kit, not the ones spc_demo_v1's own geometry demo would make. Fall back through the
# names build_spc_demo has used so a rename upstream cannot silently change the batch size.
CAPSULE_UM = float(getattr(build_spc_demo, "PIPELINE_CAPSULE_UM", None)
                   or getattr(build_spc_demo, "CAPSULE_UM", None)
                   or build_spc_demo.DROPLET_UM)
CAPSULE_NL = math.pi / 6.0 * (CAPSULE_UM / 1000.0) ** 3 * 1e3   # nL
LAMBDA_CELL = 0.2          # mean cells per capsule: deliberately low, so doublets are rare
# Cells ride in the core stream only, which is half the droplet, so the concentration
# the nozzle needs falls as the CUBE of capsule diameter. build_cell_prep.py --report has
# the table; this is the number that module has to hand over.
CELLS_PER_UL = round(LAMBDA_CELL / (CAPSULE_NL * getattr(build_spc_demo, "CORE_SHARE", 0.5)) * 1e3)


def max_flow(width_mm: float, height_mm: float, ceiling: float = SHEAR_CEILING) -> float:
    """Highest flow in uL/min that keeps wall shear at or below the ceiling."""
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (ceiling / 10.0) * w * h * h / (6.0 * MU_PA_S) * 60.0 * 1e9


def shear(flow_ul_min: float, width_mm: float, height_mm: float) -> float:
    q = flow_ul_min * 1e-9 / 60.0
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (6.0 * MU_PA_S * q / (w * h * h)) * 10.0


def route(chamber: str) -> list[str]:
    """The chamber and every chamber downstream of it (what the same flow also passes through)."""
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


# Batch: what the packed bed in the trap holds.
BATCH = int(PACKING * CH[TRAP]["volume_ul"] * 1e3 / CAPSULE_NL)
PACKED_UL = BATCH * CAPSULE_NL / 1e3 / PACKING

# Nozzle: the reference operating point of spc_demo_v1, scaled by orifice cross section.
_nz, _ref = CH[NOZZLE]["size"], build_spc_demo.CH[build_spc_demo.NOZZLE]["size"]
NOZZLE_SCALE = (_nz[1] * _nz[2]) / (_ref[1] * _ref[2])
_aq = (build_spc_demo.FLOW["core"] + build_spc_demo.FLOW["shell"]) * NOZZLE_SCALE
FLOW_B = {"caps": _aq * CAPS_SHARE, "mix": _aq * (1 - CAPS_SHARE),
          "oil": build_spc_demo.FLOW["oil"] * NOZZLE_SCALE}   # uL/min


def rate_hz() -> float:
    """Droplet frequency: one capsule per droplet, from the capsule stream at packed density."""
    return FLOW_B["caps"] * PACKING * 1e3 / MIN / CAPSULE_NL


def poisson(lam: float, k: int) -> float:
    return lam ** k * math.exp(-lam) / math.factorial(k)


def build() -> dict:
    steps: list[dict] = []

    def add(operation, target, note, duration_s, reagent=None, volume_ul=None,
            temp_c=None, washes=None, flow_ul_min=None, port_state=None, **extra):
        if operation in ("exchange", "load") and volume_ul is not None:
            # never promise an exchange faster than the pump can deliver it
            duration_s = max(duration_s, (washes or 1) * volume_ul / flow_ul_min * 60.0 * 1.4)
        step = {"step": len(steps) + 1, "operation": operation, "target": target,
                "reagent": reagent, "volume_ul": volume_ul,
                "duration_s": round(duration_s), "temp_c": temp_c, "washes": washes,
                "flow_ul_min": flow_ul_min, "note": note}
        step.update(extra)
        step["ports"] = port_state
        steps.append(step)

    gen_s = BATCH / rate_hz()
    v_brk, v_trap = CH[BREAK]["volume_ul"], CH[TRAP]["volume_ul"]
    f_brk, f_trap = route_flow(BREAK), route_flow(TRAP)
    emulsion_ul = EMULSION_FACTOR * PACKED_UL
    passes = math.ceil(emulsion_ul / v_brk)

    # --- stage B: one capsule, one barcode, one droplet ------------------------------
    add("encapsulate", NOZZLE,
        f"SHARED BARCODING STAGE, and the step that makes this a single-cell assay. "
        f"Re-inject the cured capsules close-packed through {PACK} and meet the amplification "
        f"mix carrying the barcode pool, diluted to about {LAMBDA_BARCODE:g} molecules per "
        f"droplet. The capsule stream goes in as the core and the mix as the shell. The "
        f"entrance is narrower than a capsule, so capsules deform slightly and arrive in single "
        f"file: this is what beats Poisson on the capsule, where published work gets 93-97% "
        f"single occupancy against about 30% by chance. That entrance has to be about 0.85 x the "
        f"capsule, so {0.85 * CAPSULE_UM:.0f} um here, which is far below our 200 um in-plane "
        f"floor: THIS STEP RUNS ON A BOUGHT DROPLET KIT, as encapsulation does. The chemistry is "
        f"ours; the droplet hardware is not. One barcode molecule per droplet "
        f"would be ideal but is unreachable: a free molecule cannot be ordered, so at a mean "
        f"of 1 about 37% of droplets would get none. At {LAMBDA_BARCODE:g} about "
        f"{poisson(LAMBDA_BARCODE, 0) * 100:.0f}% of droplets still get no barcode at all and "
        f"those cells are simply lost from the data, while most droplets get several barcodes "
        f"and have to be merged computationally afterwards by spotting barcodes that share "
        f"fragments, which is what the source method does. Oil then cuts each capsule plus its barcode into its own "
        f"droplet at about {rate_hz():.2f} Hz: {BATCH:,} capsules, the packed bed {TRAP} holds. "
        f"From here until the emulsion is broken, every capsule is chemically alone.",
        gen_s,
        flows_ul_h={"core": round(FLOW_B["caps"] * 60, 1), "shell": round(FLOW_B["mix"] * 60, 1),
                    "oil": round(FLOW_B["oil"] * 60, 1)},
        capsules=BATCH,
        port_state=ports(["in_load", "in_reagent", "in_oil"], ["out_drop"]))
    add("transfer", "out_drop",
        "SHARED BARCODING STAGE. Send the droplets to a tube for thermocycling. On-chip cycling is not faster "
        "than a tube at this scale and invites evaporation and bubbles, so only the steps "
        "that genuinely need a chip are on it.",
        COLLECT_S, port_state=ports(["in_oil"], ["out_drop"]), source=NOZZLE)
    add("hold_temp", NOZZLE,
        "SHARED BARCODING STAGE. Thermocycle in the tube. Each droplet clonally amplifies its own barcode and, in "
        "the same reaction, overlap-extends it onto that cell's own molecules - the cDNA made in "
        "module 3's trap in the RNA pipeline, or tagmented fragments in the chromatin variant. "
        "THIS IS THE STEP THE WHOLE DESIGN EXISTS TO PROTECT, and the step that makes the data "
        "single-cell rather than lane-level: the barcode copies are "
        "short enough to leak through the shell, so they are kept inside an oil droplet "
        "until they are part of a long molecule the capsule retains. The barcode is generated "
        "INSIDE the capsule's own surrounding oil droplet, so the emulsion is the containment "
        "and every capsule - and so every cell - ends up with its own clonal barcode.",
        THERMOCYCLE_S, temp_c=72, port_state=ports())

    # --- stage C: break, trap, wash (shared), then the chromatin variant tail --------
    chunk = round(v_brk, 3)
    remaining = emulsion_ul
    for i in range(1, passes + 1):
        vol = round(min(chunk, remaining), 3)
        remaining -= vol
        add("load", BREAK,
            f"SHARED BARCODING STAGE. Return the emulsion to the chip, pass {i} of {passes}: {BREAK} holds {v_brk:g} uL, "
            f"so the {emulsion_ul:.2f} uL batch goes in as {passes} fills. The bench version of "
            f"this step is where the published workflow loses most of its capsules, which is the "
            f"single biggest reason this module exists.",
            0, reagent="emulsion", volume_ul=vol, flow_ul_min=f_brk,
            port_state=ports(["in_load"], ["out_waste"]))
        add("exchange", BREAK,
            f"SHARED BARCODING STAGE. Separate the phases, pass {i} of {passes}. Oil leaves through the waste line; the "
            f"capsules go on to the trap in the aqueous stream. Driven by a pressure difference, "
            f"so it needs a controller rather than a fixed flow.",
            0, reagent="emulsion_breaker", volume_ul=v_brk, washes=1, flow_ul_min=f_brk,
            port_state=ports(["in_reagent"], ["out_waste"]))
    add("exchange", TRAP,
        "SHARED BARCODING STAGE. Settle the capsules into a packed bed. The capsules are held by a "
        "fabricated depth weir: the 200 um fabrication floor is IN-PLANE and z is free, so the "
        "crest is this chip's etch depth minus a 25 um gap under the lid. No bought mesh insert "
        "and no sedimentation step. Packed-bed geometry and downward flow do the rest, because a "
        "liquid-core capsule deforms and an in-plane gap that would reliably hold one is below "
        "what we can make.",
        0, reagent="wash_buffer", volume_ul=v_trap, washes=SETTLE_WASHES, flow_ul_min=f_trap,
        port_state=ports(["in_reagent"], ["out_waste"]))
    for label in ("after the emulsion break", "before the bed moves on"):
        add("exchange", TRAP,
            f"SHARED BARCODING STAGE. Flow-through wash {label}, replacing a centrifugation. Exchange is limited "
            f"by diffusion into the capsule rather than by flushing, so each one takes a "
            f"few minutes however fast the pump runs.",
            0, reagent="wash_buffer", volume_ul=v_trap, washes=WASH_WASHES, flow_ul_min=f_trap,
            port_state=ports(["in_reagent"], ["out_waste"]))
    add("hold_temp", TRAP,
        "VARIANT WORKFLOW, chromatin/transposase flavour: NOT part of the shared barcoding stage "
        "and NOT run by the pipeline in protocols/pipeline_mvp.json, where module 3's reverse "
        "transcription has already made the template. Gap-fill. The highest temperature any "
        "capsule sees in the whole workflow, and it "
        "is well inside what the shell and the board tolerate.",
        GAP_FILL_S, temp_c=GAP_FILL_C, port_state=ports())
    add("exchange", TRAP,
        "VARIANT WORKFLOW, chromatin/transposase flavour: skipped in the RNA pipeline. "
        "Load the transcription mix. The polymerase is small enough to cross the shell; "
        "the barcoded template is far too large to leave it.",
        0, reagent="transcription_mix", volume_ul=v_trap, washes=1, flow_ul_min=f_trap,
        port_state=ports(["in_reagent"], ["out_waste"]))
    add("hold_temp", TRAP,
        "VARIANT WORKFLOW, chromatin/transposase flavour: skipped in the RNA pipeline, where "
        "module 4's index PCR does the amplifying. "
        "Linear amplification in place, overnight. Linear rather than exponential because "
        "a single transposon insertion is enough, errors do not propagate and coverage "
        "stays even. Held in an incubator with the ports sealed, not on a chip heater.",
        TRANSCRIPTION_S, temp_c=TRANSCRIPTION_C, port_state=ports())
    add("exchange", TRAP,
        "SHARED BARCODING STAGE. Final wash before recovery.",
        0, reagent="wash_buffer", volume_ul=v_trap, washes=WASH_WASHES, flow_ul_min=f_trap,
        port_state=ports(["in_reagent"], ["out_waste"]))
    add("release", TRAP,
        "SHARED BARCODING STAGE. Back-flush the bed out through out_waste, which the off-chip selector valve routes to "
        "the recovery tube for this step. Reverse the flow on a slow ramp: the bed lifts at "
        "well under a pascal, so a sharp start throws capsules at the walls. In the pipeline the "
        "recovered, now barcoded capsules go on to module 4 for the sublibrary split and index "
        "PCR; the remaining library steps are bulk and off-chip.",
        round(v_trap / f_trap * 60.0), reagent="release_buffer",
        port_state=ports(["in_reagent"], ["out_waste"]))

    total_h = sum(st["duration_s"] for st in steps) / HOUR
    return {
        "id": "sctip_spc_v1",
        "name": "Single-cell barcoding: one clonal barcode per capsule, made in its own oil droplet",
        "source": SOURCE,
        "placeholder": False,
        "description": (
            f"THE PIPELINE'S SINGLE-CELL BARCODING STAGE, sitting between module 3 and module 4 "
            f"in protocols/pipeline_mvp.json. IN: washed capsules from module 3, each holding one "
            f"cell's cDNA with no barcode on it. OUT: the same capsules, with every molecule "
            f"carrying one clonal, per-capsule barcode. Gives each of about {BATCH:,} capsules of "
            f"{CAPSULE_UM:g} um (the packed bed the trap holds) a single-copy barcode from a pool "
            f"of 16 random bases (4.3e9, so under 0.002% of capsules collide), clonally amplifies "
            f"and attaches that barcode INSIDE the capsule's own surrounding oil droplet - the "
            f"emulsion is the containment, there is no anchor molecule and no cleavage step - then "
            f"breaks the emulsion and traps and washes the capsules on chip in place of about "
            f"twenty centrifugation steps. One capsule holds one cell, so this is what makes the "
            f"data single-cell; the per-lane label from module 1 and module 4's four sublibrary "
            f"indices (about twelve groups) are for demultiplexing, not resolution. Barcode "
            f"loading is Poisson at a mean of {LAMBDA_BARCODE:g}: about "
            f"{poisson(LAMBDA_BARCODE, 0) * 100:.0f}% of droplets get no barcode and those cells "
            f"are lost, and most get several and must be merged computationally. Steps 1-16 and "
            f"20-21 are the shared barcoding stage that every workflow runs; steps 17-19 (gap-fill, "
            f"transcription mix, overnight linear amplification) are the chromatin/transposase "
            f"VARIANT tail and are skipped in the RNA pipeline - they are kept here labelled, not "
            f"merged in. Capsules arrive from spc_demo_v1 via module 3; stage A is retired. "
            f"FABRICATION: the droplet handling is NOT printable - stage B needs a 115 um "
            f"close-packed entrance and stage C 100 x 150 um slots against a 200 um in-plane "
            f"floor - so re-injection and droplet making run on a bought droplet kit, exactly as "
            f"encapsulation does; the chemistry is ours, the droplet hardware is bought. Capsule "
            f"retention IS ours: a fabricated depth weir with a 25 um gap under the lid, since the "
            f"floor is in-plane and z is free. Runs about {total_h:.0f} h, "
            f"mostly the overnight transcription hold. Every volume is a number of chamber "
            f"volumes and every flow keeps wall shear in its chambers at or below 0.01 "
            f"dyn/cm2 (nozzle streams excepted: a droplet junction is high shear by design). "
            f"Reagent names are generic; see the source citation."
        ),
        "steps": steps,
    }


def report() -> None:
    print(f"capsule {CAPSULE_UM:g} um = {CAPSULE_NL:.2f} nL   "
          f"droplet rate {rate_hz():.2f} Hz = {rate_hz() * HOUR / 1000:.1f}k/h")
    print(f"batch {BATCH:,} capsules (packed bed in {TRAP}, {CH[TRAP]['volume_ul']:g} uL) "
          f"in {BATCH / rate_hz() / MIN:.1f} min")
    depth = CH[TRAP]["size"][2] * 1000
    if CAPSULE_UM > depth:
        print(f"WARNING: capsule {CAPSULE_UM:g} um is larger than the {depth:g} um chamber depth; "
              f"this needs a chip change")
    print(f"\nnozzle scale {NOZZLE_SCALE:.3f} x the spc_demo_v1 operating point")
    print(f"{'stream':<10}{'uL/min':>10}")
    for k, v in FLOW_B.items():
        print(f"{k:<10}{v:>10.3f}")
    print(f"\nchamber flows (uL/min) and shear (dyn/cm2):")
    for cid in (BREAK, TRAP):
        q = route_flow(cid)
        c = CH[cid]
        print(f"  {cid:<8}{q:>8.3f}   {shear(q, c['size'][1], c['size'][2]):.4f}")
    print(f"\nloading, mean cells per capsule = {LAMBDA_CELL:g} "
          f"at {CELLS_PER_UL} cells/uL into the core stream")
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
          f"{BATCH:,} capsules: {(1 - math.exp(-(BATCH - 1) / n)) * 100:.4f}%")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    protocol = build()
    OUT.write_text(json.dumps(protocol, indent=2) + "\n")
    total = sum(s["duration_s"] for s in protocol["steps"])
    print(f"\nwrote {OUT.name}: {len(protocol['steps'])} steps, {total / HOUR:.1f} h")

"""Build protocols/sctip_spc_v1.json.

    python protocols/build_sctip_spc.py           # write the protocol
    python protocols/build_sctip_spc.py --report  # timing and flow table

Single-copy barcoding and capsule handling, on chip/sctip_spc_v1.json.

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
MU_PA_S = 0.8e-3
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

CAPSULE_UM = build_spc_demo.CAPSULE_UM
CAPSULE_NL = math.pi / 6.0 * (CAPSULE_UM / 1000.0) ** 3 * 1e3   # nL
LAMBDA_CELL = build_spc_demo.LAMBDA_CELL
# Cells ride in the core stream only, which is half the droplet, so the concentration
# the nozzle needs falls as the CUBE of capsule diameter. build_cell_prep.py --report has
# the table; this is the number that module has to hand over.
CELLS_PER_UL = build_spc_demo.CELLS_PER_UL


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
        f"Re-inject the cured capsules close-packed through {PACK} and meet the amplification "
        f"mix carrying the barcode pool, diluted to about {LAMBDA_BARCODE:g} molecules per "
        f"droplet. The capsule stream goes in as the core and the mix as the shell. The "
        f"entrance is narrower than a capsule, so capsules deform slightly and arrive in single "
        f"file: this is what beats Poisson on the capsule, where published work gets 93-97% "
        f"single occupancy against about 30% by chance. One barcode molecule per droplet "
        f"would be ideal but is unreachable: a free molecule cannot be ordered, so at a mean "
        f"of 1 about 37% of droplets would get none. At {LAMBDA_BARCODE:g} only "
        f"{poisson(LAMBDA_BARCODE, 0) * 100:.0f}% get none, and the droplets that get several "
        f"are resolved afterwards by merging barcodes that share fragments, which is what the "
        f"source method does. Oil then cuts each capsule plus its barcode into its own "
        f"droplet at about {rate_hz():.2f} Hz: {BATCH:,} capsules, the packed bed {TRAP} holds. "
        f"From here until the emulsion is broken, every capsule is chemically alone.",
        gen_s,
        flows_ul_h={"core": round(FLOW_B["caps"] * 60, 1), "shell": round(FLOW_B["mix"] * 60, 1),
                    "oil": round(FLOW_B["oil"] * 60, 1)},
        capsules=BATCH,
        port_state=ports(["in_load", "in_reagent", "in_oil"], ["out_drop"]))
    add("transfer", "out_drop",
        "Send the droplets to a tube for thermocycling. On-chip cycling is not faster "
        "than a tube at this scale and invites evaporation and bubbles, so only the steps "
        "that genuinely need a chip are on it.",
        COLLECT_S, port_state=ports(["in_oil"], ["out_drop"]), source=NOZZLE)
    add("hold_temp", NOZZLE,
        "Thermocycle in the tube. Each droplet clonally amplifies its own barcode and, in "
        "the same reaction, overlap-extends it onto that cell's tagmented fragments. "
        "THIS IS THE STEP THE WHOLE DESIGN EXISTS TO PROTECT: the barcode copies are "
        "short enough to leak through the shell, so they are kept inside an oil droplet "
        "until they are part of a long molecule the capsule retains.",
        THERMOCYCLE_S, temp_c=72, port_state=ports())

    # --- stage C: break, trap, wash, transcribe --------------------------------------
    chunk = round(v_brk, 3)
    remaining = emulsion_ul
    for i in range(1, passes + 1):
        vol = round(min(chunk, remaining), 3)
        remaining -= vol
        add("load", BREAK,
            f"Return the emulsion to the chip, pass {i} of {passes}: {BREAK} holds {v_brk:g} uL, "
            f"so the {emulsion_ul:.2f} uL batch goes in as {passes} fills. The bench version of "
            f"this step is where the published workflow loses most of its capsules, which is the "
            f"single biggest reason this module exists.",
            0, reagent="emulsion", volume_ul=vol, flow_ul_min=f_brk,
            port_state=ports(["in_load"], ["out_waste"]))
        add("exchange", BREAK,
            f"Separate the phases, pass {i} of {passes}. Oil leaves through the waste line; the "
            f"capsules go on to the trap in the aqueous stream. Driven by a pressure difference, "
            f"so it needs a controller rather than a fixed flow.",
            0, reagent="emulsion_breaker", volume_ul=v_brk, washes=1, flow_ul_min=f_brk,
            port_state=ports(["in_reagent"], ["out_waste"]))
    add("exchange", TRAP,
        "Settle the capsules into a packed bed. Retention is by chamber geometry and "
        "downward flow, not by a size gap: a liquid-core capsule deforms, and the gap "
        "that would reliably hold one is below what we can make.",
        0, reagent="wash_buffer", volume_ul=v_trap, washes=SETTLE_WASHES, flow_ul_min=f_trap,
        port_state=ports(["in_reagent"], ["out_waste"]))
    for label in ("after the emulsion break", "before transcription"):
        add("exchange", TRAP,
            f"Flow-through wash {label}, replacing a centrifugation. Exchange is limited "
            f"by diffusion into the capsule rather than by flushing, so each one takes a "
            f"few minutes however fast the pump runs.",
            0, reagent="wash_buffer", volume_ul=v_trap, washes=WASH_WASHES, flow_ul_min=f_trap,
            port_state=ports(["in_reagent"], ["out_waste"]))
    add("hold_temp", TRAP,
        "Gap-fill. The highest temperature any capsule sees in the whole workflow, and it "
        "is well inside what the shell and the board tolerate.",
        GAP_FILL_S, temp_c=GAP_FILL_C, port_state=ports())
    add("exchange", TRAP,
        "Load the transcription mix. The polymerase is small enough to cross the shell; "
        "the barcoded template is far too large to leave it.",
        0, reagent="transcription_mix", volume_ul=v_trap, washes=1, flow_ul_min=f_trap,
        port_state=ports(["in_reagent"], ["out_waste"]))
    add("hold_temp", TRAP,
        "Linear amplification in place, overnight. Linear rather than exponential because "
        "a single transposon insertion is enough, errors do not propagate and coverage "
        "stays even. Held in an incubator with the ports sealed, not on a chip heater.",
        TRANSCRIPTION_S, temp_c=TRANSCRIPTION_C, port_state=ports())
    add("exchange", TRAP,
        "Final wash before recovery.",
        0, reagent="wash_buffer", volume_ul=v_trap, washes=WASH_WASHES, flow_ul_min=f_trap,
        port_state=ports(["in_reagent"], ["out_waste"]))
    add("release", TRAP,
        "Back-flush the bed out through out_waste, which the off-chip selector valve routes to "
        "the recovery tube for this step. Reverse the flow on a slow ramp: the bed lifts at "
        "well under a pascal, so a sharp start throws capsules at the walls. Downstream "
        "library steps are bulk and off-chip.",
        round(v_trap / f_trap * 60.0), reagent="release_buffer",
        port_state=ports(["in_reagent"], ["out_waste"]))

    total_h = sum(st["duration_s"] for st in steps) / HOUR
    return {
        "id": "sctip_spc_v1",
        "name": "Capsule generation and single-copy barcoding",
        "source": SOURCE,
        "placeholder": False,
        "description": (
            f"Gives each of about {BATCH:,} capsules of {CAPSULE_UM:g} um (the packed bed the "
            f"trap holds) a single-copy barcode from a large pool, clonally amplifies and "
            f"attaches that barcode inside an oil droplet, then breaks the emulsion and "
            f"traps and washes the capsules on chip in place of about twenty centrifugation "
            f"steps. Capsules arrive from spc_demo_v1; stage A is retired. Replaces "
            f"plate-based split-and-pool barcoding with one step. Runs about {total_h:.0f} h, "
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

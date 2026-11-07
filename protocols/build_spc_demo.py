"""Build protocols/spc_demo_v1.json.

    python protocols/build_spc_demo.py           # write the protocol
    python protocols/build_spc_demo.py --report  # geometry, flows, rate and run time

THE DYE DEMO on chip/spc_demo_v1.json: prime oil-first, start the two dye streams, let the
junction settle, film the droplet train down the serpentine, collect into a tube and look at
it. That is the whole run.

THIS IS NOT THE PIPELINE'S ENCAPSULATOR AND IT DOES NOT SUPPLY THE PIPELINE. The pipeline's
capsule is 70 um (contracts/parameters.json capsule.diameter_um), which needs about a 40 um
orifice (encapsulation.nozzle_um, the existing kit's 40 x 40 um nozzle). Our in-plane
fabrication floor is 200 um for both routes we have, resin printing and hot embossing
(cad/fabrication.json, from contracts/blocks.json fabrication_floors.min_open_channel_mm), so a
40 um orifice is five times below the floor and ON-CHIP ENCAPSULATION IS NOT ACHIEVABLE here by
either process. Encapsulation is therefore OFF-CHIP, on the existing commercial droplet kit, at
70 um - which is what module3, module4, module6 and contracts/blocks.json all already assume.
This board is the geometry demo and reference article: it shows flow focusing, planar routing,
the wetting order and whether a train comes out even.

DROPLET SIZE IS INDICATIVE. The orifice as drawn is 300 um wide by 150 um deep, one etch depth.
The usual rule of thumb (a drop is about 1.3x the nozzle) assumes an AXISYMMETRIC nozzle; applied
to 300 um it gives 390 um, and a 390 um sphere cannot form in, or be carried through, a 150 um
deep channel. In a shallow rectangular junction the drop is confined by the DEPTH, so the number
this file carries is the 150 um etch depth, and it is a bound rather than a prediction. On the
demo, measure the SPREAD, not the diameter.

NO CELLS. Both aqueous streams are dye. Loading statistics (about 0.1 cells per capsule) belong
to the off-chip kit at 70 um, not to this board.

This is the scaled-down build. The full module (chip/sctip_spc_v1.json) is a 150 x 100
mm board with three stages; the real fabrication envelope is 30 x 20 mm, single layer,
one depth, so only the first stage fits. That stage is the right one to keep as a reference,
because it is the only one you can watch happen.

Reagent names are generic. See CONFIDENTIALITY.md.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "spc_demo_v1.json"
CHIP = Path(__file__).resolve().parents[1] / "chip" / "spc_demo_v1.json"

SOURCE = (
    "Flow focusing: Anna SL, Bontoux N, Stone HA. Appl Phys Lett 2003;82:364, "
    "doi:10.1063/1.1537519. Design rules relating orifice, flows and droplet size: "
    "Lashkaripour A et al., Nat Commun 2021;12:25, doi:10.1038/s41467-020-20284-z. "
    "Core-shell semipermeable capsules by aqueous two-phase separation inside the "
    "droplet: Leonaviciene G & Mazutis L, Nucleic Acids Res 2023;51:e2, "
    "doi:10.1093/nar/gkac918."
)

MIN = 60.0
SHEAR_CEILING = 0.01          # dyn/cm2, in the chamber the flow runs through
FLOW_FRACTION = 0.85          # run at this fraction of the shear-ceiling flow
OIL_TO_AQUEOUS = 11.0 / 3.0   # design ratio of the full module (oil 11, shell 1.5, core 1.5)
CORE_SHARE = 0.5              # shell and core are equal streams
DROPLET_TO_NOZZLE = 1.3       # rule of thumb for an AXISYMMETRIC nozzle; not what this board has
SETTLE_S = 180                # time for the junction to lock into a steady drip
RUN_S = 600                   # demo batch length: long enough to film and to see under a scope
FILM_S = 120                  # the film-this hold on the serpentine
COLLECT_S = 600

# The two numbers the whole repo has to agree on, and where they come from.
PARAMS = json.loads((Path(__file__).resolve().parents[1] / "contracts" / "parameters.json").read_text())
PIPELINE_CAPSULE_UM = PARAMS["parameters"]["capsule.diameter_um"]["value"]     # 70, off-chip kit
# 37 C medium. NOT fluid.viscosity_pa_s, which is water at 20 C and is the dye-demo value.
MU_PA_S = float(PARAMS["parameters"]["fluid.viscosity_37c_pa_s"]["value"])
KIT_NOZZLE_UM = PARAMS["parameters"]["encapsulation.nozzle_um"]["value"]       # 40, off-chip kit
FAB_FLOOR_UM = json.loads(
    (Path(__file__).resolve().parents[1] / "contracts" / "blocks.json").read_text()
)["fabrication_floors"]["min_open_channel_mm"] * 1000.0                       # 200, in-plane

CHIP_SPEC = json.loads(CHIP.read_text())
CH = {c["id"]: c for c in CHIP_SPEC["chambers"]}
PORTS = [p["id"] for p in CHIP_SPEC["ports"]]
NOZZLE = CHIP_SPEC["encapsulation"]["nozzle"]
DELAY = "delay_1"
ORIFICE_UM = float(CHIP_SPEC["encapsulation"]["nozzle_um"])
ORIFICE_DEPTH_UM = float(CHIP_SPEC["encapsulation"]["nozzle_depth_um"])
# A sphere larger than the etch depth cannot form at the junction or be carried to the outlet, so
# the depth, not the 1.3x rule, sets the figure on a board whose orifice is wider than it is deep.
UNCONFINED_UM = round(DROPLET_TO_NOZZLE * ORIFICE_UM, 1)
DROPLET_UM = round(min(UNCONFINED_UM, ORIFICE_DEPTH_UM), 1)
DEPTH_LIMITED = DROPLET_UM < UNCONFINED_UM
DROPLET_NL = math.pi / 6.0 * (DROPLET_UM / 1000.0) ** 3 * 1e3
assert DROPLET_UM <= CH[NOZZLE]["size"][2] * 1000 + 1e-9, (
    "the stated droplet is larger than the channel depth: it could not form or be carried"
)


def max_flow(width_mm: float, height_mm: float, ceiling: float = SHEAR_CEILING) -> float:
    """Highest flow in uL/min that keeps wall shear at or below the ceiling."""
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (ceiling / 10.0) * w * h * h / (6.0 * MU_PA_S) * 60.0 * 1e9


def shear(flow_ul_min: float, width_mm: float, height_mm: float) -> float:
    q = flow_ul_min * 1e-9 / 60.0
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (6.0 * MU_PA_S * q / (w * h * h)) * 10.0


# Every stream leaves through the delay line, so the delay line's shear ceiling caps the
# total flow; the full module's oil-to-aqueous ratio and equal shell and core streams set
# how it is shared. This keeps the droplet ratio and gives up speed.
TOTAL_FLOW = math.floor(FLOW_FRACTION * max_flow(CH[DELAY]["size"][1], CH[DELAY]["size"][2]) * 1000) / 1000
_AQUEOUS = TOTAL_FLOW / (1.0 + OIL_TO_AQUEOUS)
FLOW = {"shell": math.floor(_AQUEOUS * (1 - CORE_SHARE) * 1000) / 1000,
        "core": math.floor(_AQUEOUS * CORE_SHARE * 1000) / 1000,
        "oil": math.floor(_AQUEOUS * OIL_TO_AQUEOUS * 1000) / 1000}   # uL/min


def rate_hz() -> float:
    return (FLOW["shell"] + FLOW["core"]) * 1e3 / MIN / DROPLET_NL


def demo_droplets() -> int:
    return int(rate_hz() * RUN_S)


def ports(feed=(), vent=()) -> dict:
    feed, vent = list(feed), list(vent)
    return {"feed": feed, "vent": vent, "cap": [p for p in PORTS if p not in feed + vent]}


def build() -> dict:
    steps: list[dict] = []

    def add(operation, target, note, duration_s, reagent=None, volume_ul=None,
            temp_c=None, washes=None, flow_ul_min=None, port_state=None, **extra):
        if operation == "exchange":
            volume_ul = volume_ul if volume_ul is not None else CH[target]["volume_ul"]
            washes = washes if washes is not None else 1
            duration_s = max(duration_s,
                             washes * volume_ul / (flow_ul_min or TOTAL_FLOW) * 60.0 * 1.4)
        step = {"step": len(steps) + 1, "operation": operation, "target": target,
                "reagent": reagent, "volume_ul": volume_ul,
                "duration_s": round(duration_s), "temp_c": temp_c, "washes": washes,
                "flow_ul_min": flow_ul_min, "note": note}
        step.update(extra)
        step["ports"] = port_state
        steps.append(step)

    n_drops = demo_droplets()
    run_s = n_drops / rate_hz()

    add("exchange", DELAY,
        "Prime the board with oil before anything aqueous goes in. Do this first and "
        "always: the walls have to be oil-wetted for water-in-oil to work at all, and an "
        "aqueous stream that touches a dry wall first will wet it and keep wetting it. "
        "Getting this order right, and seeing what happens when you do not, is half the "
        "point of running the board.",
        0, reagent="carrier_oil", flow_ul_min=TOTAL_FLOW,
        port_state=ports(["in_oil"], ["out_caps"]))
    add("encapsulate", NOZZLE,
        f"Start the two dye streams and the oil, and let the junction settle. Expect a "
        f"minute or two of ragged, uneven droplets before it locks into a steady drip: that "
        f"is normal, and it is the syringe pump and the tubing settling rather than the "
        f"chip. NO CELLS GO ON THIS BOARD - both aqueous streams are dye, because this is a "
        f"geometry demo and not the pipeline's encapsulator. The orifice is "
        f"{ORIFICE_UM:g} um wide by {ORIFICE_DEPTH_UM:g} um deep, so droplets are confined by "
        f"the depth: treat {DROPLET_UM:g} um as an INDICATIVE bound, not a prediction. At "
        f"about {rate_hz():.2f} droplets per second the demo batch of {n_drops:,} droplets "
        f"takes about {run_s / MIN:.0f} min.",
        SETTLE_S + run_s,
        flows_ul_h={"core": round(FLOW["core"] * 60, 1), "shell": round(FLOW["shell"] * 60, 1),
                    "oil": round(FLOW["oil"] * 60, 1)},
        capsules=n_drops,
        port_state=ports(["in_oil", "in_shell", "in_core"], ["out_caps"]))
    add("hold_temp", DELAY,
        f"FILM THIS. The serpentine shows the droplet train. MEASURE THE SPREAD, NOT THE "
        f"DIAMETER: even spacing and even size is the whole result, and the {DROPLET_UM:g} um "
        f"figure is only the depth bound. If droplets vary in size the flow ratio is off; if "
        f"they merge the surfactant or the surface treatment is wrong.",
        FILM_S, temp_c=22, port_state=ports(["in_oil", "in_shell", "in_core"], ["out_caps"]))
    add("release", DELAY,
        f"Collect into a tube and look at the droplets under a scope. NOTHING IS CURED AND "
        f"NOTHING GOES DOWNSTREAM: these are dye droplets of order {DROPLET_UM:g} um from a "
        f"geometry demo, not the pipeline's {PIPELINE_CAPSULE_UM:g} um capsules. Those come "
        f"from the off-chip commercial droplet kit and its {KIT_NOZZLE_UM:g} um nozzle, "
        f"because a {KIT_NOZZLE_UM:g} um orifice is {FAB_FLOOR_UM / KIT_NOZZLE_UM:.0f}x below "
        f"our {FAB_FLOOR_UM:g} um in-plane fabrication floor and cannot be made here.",
        COLLECT_S, reagent="dye_solution", port_state=ports([], ["out_caps"]))

    total_min = sum(st["duration_s"] for st in steps) / MIN
    return {
        "id": "spc_demo_v1",
        "name": "Flow-focusing dye demo (reference board, not the pipeline's encapsulator)",
        "source": SOURCE,
        "placeholder": False,
        "description": (
            f"A DYE DEMO, NOT A PIPELINE STAGE. Prime oil-first, start the two dye streams, "
            f"settle the junction, film the droplet train, collect and look at it: about "
            f"{n_drops:,} droplets in roughly {total_min:.0f} min on a 30 x 20 mm "
            f"single-depth chip. THIS BOARD DOES NOT SUPPLY THE PIPELINE AND CANNOT MAKE ITS "
            f"{PIPELINE_CAPSULE_UM:g} um CAPSULES: that needs about a {KIT_NOZZLE_UM:g} um "
            f"orifice, which is {FAB_FLOOR_UM / KIT_NOZZLE_UM:.0f}x below the {FAB_FLOOR_UM:g} "
            f"um in-plane floor of both processes we have, so encapsulation is off-chip on the "
            f"existing commercial droplet kit at {PIPELINE_CAPSULE_UM:g} um. The orifice as "
            f"drawn is {ORIFICE_UM:g} um wide by {ORIFICE_DEPTH_UM:g} um deep; droplets are "
            f"confined by the depth, so {DROPLET_UM:g} um is an INDICATIVE bound and the figure "
            f"to measure on the demo is the spread, not the diameter. The 1.3x-the-nozzle rule "
            f"would say {UNCONFINED_UM:g} um, which is impossible in a {ORIFICE_DEPTH_UM:g} um "
            f"deep channel and is why that number is gone. No cells: both aqueous streams are "
            f"dye and nothing is cured. The streams keep the full module's oil-to-aqueous ratio, "
            f"but their total is set by the delay line's shear ceiling (0.01 dyn/cm2 in "
            f"{DELAY}), so it runs slower than the full module. The batch is what fits in "
            f"{RUN_S / MIN:.0f} min at that rate. Reagent names are generic; see the source "
            f"citation."
        ),
        "steps": steps,
    }


def report() -> None:
    depth = CH[NOZZLE]["size"][2] * 1000
    print(f"orifice as drawn at {NOZZLE}: {ORIFICE_UM:g} um wide x {ORIFICE_DEPTH_UM:g} um deep "
          f"(channel depth {depth:g} um, one etch depth)")
    print(f"  1.3x-the-nozzle rule (axisymmetric nozzle): {UNCONFINED_UM:g} um "
          f"-- IMPOSSIBLE here, a sphere cannot exceed the {depth:g} um depth")
    print(f"  depth-confined bound, what this file states: {DROPLET_UM:g} um "
          f"({DROPLET_NL:.2f} nL){', indicative only' if DEPTH_LIMITED else ''}")
    print(f"  pipeline capsule {PIPELINE_CAPSULE_UM:g} um needs a ~{KIT_NOZZLE_UM:g} um orifice; "
          f"in-plane floor is {FAB_FLOOR_UM:g} um ({FAB_FLOOR_UM / KIT_NOZZLE_UM:.0f}x), so "
          f"ON-CHIP ENCAPSULATION IS NOT ACHIEVABLE")
    print("  -> capsules come from the off-chip commercial droplet kit; this board is a dye demo\n")
    print(f"rate {rate_hz():.2f} Hz = {rate_hz() * 3600 / 1000:.1f}k/h")
    print(f"demo batch {demo_droplets():,} droplets in {RUN_S / MIN:.0f} min\n")
    c = CH[DELAY]
    print(f"{'stream':<10}{'uL/min':>9}{'uL/h':>9}")
    for k, v in FLOW.items():
        print(f"{k:<10}{v:>9.3f}{v * 60:>9.1f}")
    print(f"\nshear in {DELAY}: {shear(sum(FLOW.values()), c['size'][1], c['size'][2]):.4f} dyn/cm2 "
          f"(ceiling {SHEAR_CEILING:g}) at {sum(FLOW.values()):.3f} uL/min")
    print(f"oil : aqueous = {FLOW['oil'] / (FLOW['shell'] + FLOW['core']):.2f} : 1")
    print("no cells: both aqueous streams are dye. Loading statistics belong to the off-chip kit "
          "at 70 um, not to this board.")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    protocol = build()
    OUT.write_text(json.dumps(protocol, indent=2) + "\n")
    total = sum(s["duration_s"] for s in protocol["steps"])
    print(f"\nwrote {OUT.name}: {len(protocol['steps'])} steps, {total / MIN:.0f} min")

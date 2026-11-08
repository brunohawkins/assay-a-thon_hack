"""Build protocols/spc_demo_v1.json.

    python protocols/build_spc_demo.py           # write the protocol
    python protocols/build_spc_demo.py --report  # flows, rate and run time

The demo run on chip/spc_demo_v1.json: make capsules, film them, collect, cure.

This is the scaled-down build. The full module (chip/sctip_spc_v1.json) is a 150 x 100
mm board with three stages; the real fabrication envelope is 30 x 20 mm, single layer,
one depth, so only the first stage fits. That stage is the right one to keep, because
it is the only one you can watch happen.

Flows are unchanged from the full module: droplet size is set by the orifice and the
flow ratio, not by how big the board is.

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
MU_PA_S = 0.8e-3
SHEAR_CEILING = 0.01          # dyn/cm2, in the chamber the flow runs through
FLOW_FRACTION = 0.85          # run at this fraction of the shear-ceiling flow
OIL_TO_AQUEOUS = 11.0 / 3.0   # design ratio of the full module (oil 11, shell 1.5, core 1.5)
CORE_SHARE = 0.5              # shell and core are equal streams
DROPLET_TO_NOZZLE = 1.3       # droplets come out at about 1.3x the nozzle width (chip note on xj_1)
LAMBDA_CELL = 0.2
SETTLE_S = 180                # time for the junction to lock into a steady drip
RUN_S = 600                   # demo batch length: long enough to film and to see under a scope
FILM_S = 120                  # the film-this hold on the serpentine
COLLECT_S = 600

CHIP_SPEC = json.loads(CHIP.read_text())
CH = {c["id"]: c for c in CHIP_SPEC["chambers"]}
PORTS = [p["id"] for p in CHIP_SPEC["ports"]]
NOZZLE = "xj_1"
DELAY = "delay_1"
ORIFICE_UM = float(CHIP_SPEC["encapsulation"]["nozzle_um"])
CAPSULE_UM = round(DROPLET_TO_NOZZLE * ORIFICE_UM, 1)
CAPSULE_NL = math.pi / 6.0 * (CAPSULE_UM / 1000.0) ** 3 * 1e3
CELLS_PER_UL = round(LAMBDA_CELL / (CAPSULE_NL * CORE_SHARE) * 1e3)


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
    return (FLOW["shell"] + FLOW["core"]) * 1e3 / MIN / CAPSULE_NL


def demo_capsules() -> int:
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

    n_caps = demo_capsules()
    run_s = n_caps / rate_hz()

    add("exchange", DELAY,
        "Prime the board with oil before anything aqueous goes in. Do this first and "
        "always: the walls have to be oil-wetted for water-in-oil to work at all, and an "
        "aqueous stream that touches a dry wall first will wet it and keep wetting it.",
        0, reagent="carrier_oil", flow_ul_min=TOTAL_FLOW,
        port_state=ports(["in_oil"], ["out_caps"]))
    add("encapsulate", NOZZLE,
        f"Start the two aqueous streams and the oil, and let the junction settle. Expect a "
        f"minute or two of ragged, uneven droplets before it locks into a steady drip: that "
        f"is normal, and it is the syringe pump and the tubing settling rather than the "
        f"chip. Cells ride in the core stream at about {CELLS_PER_UL} cells/uL, which gives "
        f"a mean of {LAMBDA_CELL:g} cells per capsule. That figure belongs to a "
        f"{CAPSULE_UM:g} um capsule and falls as the cube of diameter, so do not reuse it if "
        f"the nozzle changes. At about {rate_hz():.2f} droplets per second the demo batch of "
        f"{n_caps:,} capsules takes about {run_s / MIN:.0f} min.",
        SETTLE_S + run_s,
        flows_ul_h={"core": round(FLOW["core"] * 60, 1), "shell": round(FLOW["shell"] * 60, 1),
                    "oil": round(FLOW["oil"] * 60, 1)},
        capsules=n_caps,
        port_state=ports(["in_oil", "in_shell", "in_core"], ["out_caps"]))
    add("hold_temp", DELAY,
        f"FILM THIS. The serpentine shows a regular train of {CAPSULE_UM:g} um capsules. "
        f"Even spacing and even size is the whole result: if droplets vary in size the flow "
        f"ratio is off, and if they merge the surfactant or the surface treatment is wrong.",
        FILM_S, temp_c=22, port_state=ports(["in_oil", "in_shell", "in_core"], ["out_caps"]))
    add("release", DELAY,
        "Collect into a tube on ice. Let the two aqueous polymers demix for a few "
        "minutes, which is what puts the shell on the outside, then flood-cure at 405 nm. "
        "Curing is off-chip on purpose: demixing takes minutes, and a serpentine holding "
        "minutes at this flow would be longer than the board.",
        COLLECT_S, reagent="photoinitiator", port_state=ports([], ["out_caps"]))

    total_min = sum(st["duration_s"] for st in steps) / MIN
    return {
        "id": "spc_demo_v1",
        "name": "Capsule generation, demo run",
        "source": SOURCE,
        "placeholder": False,
        "description": (
            f"The demo: prime, start the streams, settle the junction, film the droplet "
            f"train, collect and cure. About {n_caps:,} capsules of {CAPSULE_UM:g} um from a "
            f"{ORIFICE_UM:g} um nozzle in roughly {total_min:.0f} min on a 30 x 20 mm "
            f"single-depth chip. The streams keep the full module's oil-to-aqueous ratio, but "
            f"their total is set by the delay line's shear ceiling (0.01 dyn/cm2 in "
            f"{DELAY}), so it runs slower than the full module; droplet size follows the "
            f"nozzle and the flow ratio rather than the size of the board. The batch is what "
            f"fits in {RUN_S / MIN:.0f} min at that rate. Reagent names are generic; see the "
            f"source citation."
        ),
        "steps": steps,
    }


def report() -> None:
    print(f"nozzle {ORIFICE_UM:g} um -> capsule {CAPSULE_UM:g} um "
          f"({CAPSULE_NL:.2f} nL), {DROPLET_TO_NOZZLE:g}x the nozzle")
    depth = CH[NOZZLE]["size"][2] * 1000
    if CAPSULE_UM > depth:
        print(f"WARNING: capsule {CAPSULE_UM:g} um is larger than the {depth:g} um channel depth; "
              f"droplets would be squeezed flat, so this needs a chip change")
    print(f"rate {rate_hz():.2f} Hz = {rate_hz() * 3600 / 1000:.1f}k/h")
    print(f"demo batch {demo_capsules():,} in {RUN_S / MIN:.0f} min\n")
    c = CH[DELAY]
    print(f"{'stream':<10}{'uL/min':>9}{'uL/h':>9}")
    for k, v in FLOW.items():
        print(f"{k:<10}{v:>9.3f}{v * 60:>9.1f}")
    print(f"\nshear in {DELAY}: {shear(sum(FLOW.values()), c['size'][1], c['size'][2]):.4f} dyn/cm2 "
          f"(ceiling {SHEAR_CEILING:g}) at {sum(FLOW.values()):.3f} uL/min")
    print(f"oil : aqueous = {FLOW['oil'] / (FLOW['shell'] + FLOW['core']):.2f} : 1")
    print(f"cells into the core stream: {CELLS_PER_UL} cells/uL "
          f"for a mean of {LAMBDA_CELL:g} per capsule")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    protocol = build()
    OUT.write_text(json.dumps(protocol, indent=2) + "\n")
    total = sum(s["duration_s"] for s in protocol["steps"])
    print(f"\nwrote {OUT.name}: {len(protocol['steps'])} steps, {total / MIN:.0f} min")

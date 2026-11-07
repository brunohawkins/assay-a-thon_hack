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
ORIFICE_UM = 100.0
CAPSULE_UM = 135.0
FLOW = {"shell": 1.5, "core": 1.5, "oil": 11.0}   # uL/min, as the full module
LAMBDA_CELL = 0.2
DEMO_CAPSULES = 5000     # a demo, not a run: enough to film and to see under a scope

CH = {c["id"]: c for c in json.loads(CHIP.read_text())["chambers"]}
CAPSULE_NL = math.pi / 6.0 * (CAPSULE_UM / 1000.0) ** 3 * 1e3
CELLS_PER_UL = round(LAMBDA_CELL / (CAPSULE_NL * 0.5) * 1e3)


def rate_hz() -> float:
    return (FLOW["shell"] + FLOW["core"]) * 1e3 / MIN / CAPSULE_NL


def build() -> dict:
    steps: list[dict] = []

    def add(operation, target, note, duration_s, reagent=None, volume_ul=None,
            temp_c=None, washes=None, flow_ul_min=None):
        if operation == "exchange":
            volume_ul = volume_ul if volume_ul is not None else CH[target]["volume_ul"]
            washes = washes if washes is not None else 1
            duration_s = max(duration_s,
                             washes * volume_ul / (flow_ul_min or 4.0) * 60.0 * 1.4)
        steps.append({"step": len(steps) + 1, "operation": operation, "target": target,
                      "reagent": reagent, "volume_ul": volume_ul,
                      "duration_s": round(duration_s), "temp_c": temp_c, "washes": washes,
                      "flow_ul_min": flow_ul_min, "note": note})

    run_s = DEMO_CAPSULES / rate_hz()

    add("exchange", "delay_1",
        "Prime the board with oil before anything aqueous goes in. Do this first and "
        "always: the walls have to be oil-wetted for water-in-oil to work at all, and an "
        "aqueous stream that touches a dry wall first will wet it and keep wetting it.",
        300, reagent="carrier_oil", flow_ul_min=FLOW["oil"])
    add("load", "aq_merge",
        f"Start the two aqueous streams. Cells ride in the core stream at about "
        f"{CELLS_PER_UL} cells/uL, which gives a mean of {LAMBDA_CELL:g} cells per "
        f"capsule. That figure belongs to a {CAPSULE_UM:g} um capsule and falls as the "
        f"cube of diameter, so do not reuse it if the orifice changes.",
        180, reagent="cell_suspension", volume_ul=20, flow_ul_min=FLOW["core"])
    add("exchange", "xj_1",
        f"Bring the oil up to rate and let the junction settle. Expect a minute or two of "
        f"ragged, uneven droplets before it locks into a steady drip: that is normal, and "
        f"it is the syringe pump and the tubing settling rather than the chip.",
        180, reagent="carrier_oil", volume_ul=20, flow_ul_min=FLOW["oil"])
    add("hold_temp", "delay_1",
        f"FILM THIS. At about {rate_hz():.0f} droplets per second the serpentine shows a "
        f"regular train of {CAPSULE_UM:g} um capsules. Even spacing and even size is the "
        f"whole result: if droplets vary in size the flow ratio is off, and if they merge "
        f"the surfactant or the surface treatment is wrong.",
        120, temp_c=22)
    add("exchange", "delay_1",
        f"Run for the demo batch: {DEMO_CAPSULES:,} capsules in about "
        f"{run_s / MIN:.0f} min. A real run is ten times this and changes nothing about "
        f"the chip.",
        run_s, reagent="carrier_oil", volume_ul=FLOW["oil"] * run_s / MIN,
        flow_ul_min=FLOW["oil"])
    add("release", "delay_1",
        "Collect into a tube on ice. Let the two aqueous polymers demix for a few "
        "minutes, which is what puts the shell on the outside, then flood-cure at 405 nm. "
        "Curing is off-chip on purpose: demixing takes minutes, and a serpentine holding "
        "minutes at this flow would be longer than the board.",
        600, reagent="photoinitiator")

    return {
        "id": "spc_demo_v1",
        "name": "Capsule generation, demo run",
        "source": SOURCE,
        "placeholder": False,
        "description": (
            f"The demo: prime, start the streams, settle the junction, film the droplet "
            f"train, collect and cure. About {DEMO_CAPSULES:,} capsules of "
            f"{CAPSULE_UM:g} um from a {ORIFICE_UM:g} um orifice in roughly "
            f"{(run_s + 1380) / MIN:.0f} min on a 30 x 20 mm single-depth chip. Flows are "
            f"identical to the full module, because droplet size follows the orifice and "
            f"the flow ratio rather than the size of the board. Reagent names are "
            f"generic; see the source citation."
        ),
        "steps": steps,
    }


def report() -> None:
    print(f"orifice {ORIFICE_UM:g} um -> capsule {CAPSULE_UM:g} um "
          f"({CAPSULE_NL:.2f} nL), about 1.3x the orifice")
    print(f"rate {rate_hz():.0f} Hz = {rate_hz() * 3600 / 1000:.0f}k/h")
    print(f"demo batch {DEMO_CAPSULES:,} in {DEMO_CAPSULES / rate_hz() / MIN:.0f} min\n")
    print(f"{'stream':<10}{'uL/min':>9}{'uL/h':>9}")
    for k, v in FLOW.items():
        print(f"{k:<10}{v:>9.1f}{v * 60:>9.0f}")
    print(f"\noil : aqueous = {FLOW['oil'] / (FLOW['shell'] + FLOW['core']):.2f} : 1")
    print(f"cells into the core stream: {CELLS_PER_UL} cells/uL "
          f"for a mean of {LAMBDA_CELL:g} per capsule")
    print(f"\nboard 30 x 20 mm, single depth 0.15 mm, smallest feature "
          f"{ORIFICE_UM:g} um at the orifice")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    protocol = build()
    OUT.write_text(json.dumps(protocol, indent=2) + "\n")
    total = sum(s["duration_s"] for s in protocol["steps"])
    print(f"\nwrote {OUT.name}: {len(protocol['steps'])} steps, {total / MIN:.0f} min")

"""Build protocols/ipsc_to_mgl_v1.json.

    python protocols/build_ipsc_to_mgl.py

The protocol runs a published monolayer iPSC-to-microglia differentiation across the
five modules of chip/ipsc_to_mgl_chip_v1.json, with nothing handled off-chip. This
script exists so the numbers can be checked rather than taken on trust: the day
schedule comes from the published protocol cited in SOURCE, and every flow rate and
duration is computed here from chamber geometry.

Two calculations set the flow rates.

1. Wall shear in a wide shallow chamber is tau = 6 * mu * Q / (w * h^2).
   Park et al. 2023 (doi 10.1016/j.bpj.2023.03.037) saw microglial activation at
   0.017 dyn/cm^2, which is the lowest activating shear in the literature. Every
   exchange here is held at or below 0.01 dyn/cm^2, with the ceiling per chamber
   printed by --report.

2. Exchange duration must cover the liquid actually moved:
   duration >= washes * volume_ul / flow_ul_min * 60. The repo validator checks this,
   and we add a margin for valve switching and settling.

Reagent names are generic on purpose. No compositions, concentrations or cell lines
appear here or in the generated file: see CONFIDENTIALITY.md.
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent / "ipsc_to_mgl_v1.json"
CHIP = Path(__file__).resolve().parents[1] / "chip" / "ipsc_to_mgl_chip_v1.json"

SOURCE = (
    "McQuade A, Coburn M, Tu CH, Hasselmann J, Davtyan H, Blurton-Jones M. "
    "Development and validation of a simplified method to generate human microglia "
    "from pluripotent stem cells. Molecular Neurodegeneration 2018;13:67. "
    "doi:10.1186/s13024-018-0297-x. Monolayer, embryoid-body-free, sorting-free. "
    "Maturation-stage adherence follows Washer et al., Scientific Reports 2022;12:19454, "
    "doi:10.1038/s41598-022-23477-2."
)

DAY = 86400.0
MU_PA_S = 0.8e-3          # culture medium at 37 C
SHEAR_CEILING = 0.01      # dyn/cm^2, below the 0.017 activation threshold
MARGIN = 1.4              # exchange duration margin over the liquid-moved minimum


def shear_dyn_cm2(flow_ul_min: float, width_mm: float, height_mm: float) -> float:
    """Wall shear for a wide shallow rectangular chamber, in dyn/cm^2."""
    q = flow_ul_min * 1e-9 / 60.0          # uL/min -> m^3/s
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (6.0 * MU_PA_S * q / (w * h * h)) * 10.0   # Pa -> dyn/cm^2


def max_flow(width_mm: float, height_mm: float, ceiling: float = SHEAR_CEILING) -> float:
    """Highest flow in uL/min that keeps wall shear at or below the ceiling."""
    w, h = width_mm * 1e-3, height_mm * 1e-3
    q = (ceiling / 10.0) * w * h * h / (6.0 * MU_PA_S)
    return q * 60.0 * 1e9


def load_chip() -> dict[str, dict]:
    chip = json.loads(CHIP.read_text())
    return {c["id"]: c for c in chip["chambers"]}


CH = load_chip()
# Flow per chamber: rounded down from the shear ceiling to a tidy number.
FLOW = {cid: float(int(max_flow(c["size"][0], c["size"][2]))) for cid, c in CH.items()}
for cid in FLOW:                     # keep a little headroom under the ceiling
    FLOW[cid] = max(1.0, FLOW[cid] - 2.0)

MATURATION = ["mat_1", "mat_2", "mat_3", "mat_4"]


class Protocol:
    """Accumulates steps and tracks elapsed protocol time, so holds fill the gaps."""

    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.t = 0.0

    def _add(self, operation: str, target: str, note: str, duration_s: float,
             reagent=None, volume_ul=None, temp_c=None, washes=None, flow_ul_min=None) -> None:
        self.steps.append({
            "step": len(self.steps) + 1,
            "operation": operation,
            "target": target,
            "reagent": reagent,
            "volume_ul": volume_ul,
            "duration_s": round(duration_s),
            "temp_c": temp_c,
            "washes": washes,
            "flow_ul_min": flow_ul_min,
            "note": note,
        })
        self.t += round(duration_s)

    def load(self, target: str, volume_ul: float, note: str, duration_s: float = 600) -> None:
        self._add("load", target, note, duration_s, volume_ul=volume_ul)

    def exchange(self, target: str, reagent: str, note: str, fraction: float = 1.0,
                 washes: int = 1, duration_s: float | None = None) -> None:
        """Move `fraction` of the chamber volume through it, `washes` times."""
        volume = round(CH[target]["volume_ul"] * fraction, 1)
        flow = FLOW[target]
        minimum = washes * volume / flow * 60.0
        self._add("exchange", target, note, duration_s or minimum * MARGIN,
                  reagent=reagent, volume_ul=volume, washes=washes, flow_ul_min=flow)

    def hold(self, target: str, temp_c: float, duration_s: float, note: str) -> None:
        if duration_s <= 0:
            raise ValueError(f"hold on {target} would be {duration_s} s; schedule is out of order")
        self._add("hold_temp", target, note, duration_s, temp_c=temp_c)

    def hold_until(self, target: str, day: float, temp_c: float, note: str) -> None:
        self.hold(target, temp_c, day * DAY - self.t, note)

    def split(self, target: str, note: str, duration_s: float = 900) -> None:
        self._add("split", target, note, duration_s)

    def pool(self, target: str, note: str, duration_s: float = 900) -> None:
        self._add("pool", target, note, duration_s)

    def release(self, target: str, reagent: str, note: str, duration_s: float = 600) -> None:
        self._add("release", target, note, duration_s, reagent=reagent)


def build() -> dict:
    p = Protocol()

    # --- M1: iPSC expansion, days -7 to -1 -------------------------------------------
    # Time starts at day -7, so day 0 of the published schedule is at t = 7 days.
    p.exchange("m1_expand", "coating_solution",
               "Fill the expansion chamber with coating solution so the iPSCs have a surface to attach to.")
    p.hold("m1_expand", 37, 2 * 3600,
           "Hold the coating in place at 37 C, then it is displaced by the first medium change.")
    p.load("m1_expand", 120,
           "Load iPSCs into the expansion chamber. Flow stops so they can settle.")
    p.hold("m1_expand", 37, 1 * DAY,
           "No flow for 24 hours while the cells attach. Oxygen comes from the recirculating reservoir, not from fresh medium.")
    for day in range(-6, -1):
        p.exchange("m1_expand", "ipsc_medium",
                   f"Daily feed on day {day}: exchange one chamber volume of iPSC medium.")
        p.hold_until("m1_expand", day + 7 + 1, 37, f"Grow on through day {day}.")
    p.exchange("m1_expand", "dissociation_reagent",
               "Lift the iPSCs as small aggregates with an enzyme-free reagent. Aggregates, not single cells, are what the next stage needs.",
               fraction=0.5)
    p.split("m1_expand",
            "Carry the aggregates downstream into the hemogenic chamber. This replaces a pipette transfer between flasks.")

    # --- M2: hemogenic endothelium, days -1 to 12 -------------------------------------
    p.hold_until("m2_hemogenic", 7.0, 37,
                 "No flow for 24 hours while the aggregates attach and spread. The run stops here if the day-0 colony count is out of range.")
    p.exchange("m2_hemogenic", "mesoderm_medium",
               "Day 0: full exchange into mesoderm medium. Imaging has counted the colonies and confirmed the seeding density is in range.")
    p.hold_until("m2_hemogenic", 9.0, 37, "Days 0 to 2: mesoderm specification.")
    p.exchange("m2_hemogenic", "mesoderm_medium",
               "Day 2: half exchange, leaving half the conditioned medium behind. The cells depend on their own secreted factors, so this is never a full change.",
               fraction=0.5)
    p.hold_until("m2_hemogenic", 10.0, 37, "Day 2 to 3.")
    p.exchange("m2_hemogenic", "hematopoietic_medium",
               "Day 3: full exchange into hematopoietic medium, which starts the switch from mesoderm to hemogenic endothelium.")
    for day in (5, 7, 9):
        p.hold_until("m2_hemogenic", day + 7.0, 37,
                     f"Growing on to day {day}. From about day 7 the first progenitors round up and lift off the endothelial layer.")
        p.exchange("m2_hemogenic", "hematopoietic_medium",
                   f"Day {day}: half exchange. Spent medium leaves through the waste port and is kept as a supernatant sample.",
                   fraction=0.5)
    p.hold_until("m2_hemogenic", 17.0, 37,
                 "Day 9 to 10: progenitors keep accumulating above the adherent layer.")

    # --- M3 and M4: two progenitor harvests, days 10 and 12 ---------------------------
    for n, day in ((1, 10), (2, 12)):
        p.split("m2_hemogenic",
                f"Harvest {n}, day {day}: carry the floating progenitors downstream. The adherent layer stays put, so it keeps producing.")
        p.exchange("m3_concentrate", "wash_buffer",
                   "Pass the progenitors through the 30 um filter, which blocks any detached endothelial sheet, then concentrate them. This replaces a centrifugation step.",
                   washes=2)
        p.split("m3_concentrate",
                "Distribute the progenitors across the four maturation chambers.")
        if day == 10:
            p.hold_until("m2_hemogenic", 19.0, 37, "Day 10 to 12: the endothelium produces a second batch.")

    # --- M4: maturation, days 12 to 40 -------------------------------------------------
    p.hold_until("mat_1", 20.0, 37,
                 "Progenitors settle and begin to adhere in maturation medium. Flow stays off while they attach.")
    for day in range(14, 39, 2):
        for chamber in MATURATION:
            p.exchange(chamber, "maturation_medium",
                       f"Day {day}: half exchange in {chamber}. The four chambers are fed one at a time through the selector valve.",
                       fraction=0.5)
        p.hold_until("mat_1", day + 7 + 2.0, 37,
                     f"Day {day} to {day + 2}: maturation continues. Imaging tracks ramification and the supernatant is sampled for stress markers.")

    # --- M5: harvest and processing, day 40 -------------------------------------------
    p.pool("m5_harvest",
           "Day 40: bring the cells from all four maturation chambers together in the harvest module.")
    p.hold("m5_harvest", 4, 600,
           "Cool to 4 C before dissociation. Cold handling limits the activation that warm enzymatic lifting causes in this cell type.")
    p.exchange("m5_harvest", "dissociation_reagent",
               "Fill with a gentle, enzyme-free dissociation reagent.", fraction=0.5)
    p.hold("m5_harvest", 4, 900,
           "Hold cold while the cells release from the surface.")
    p.exchange("m5_harvest", "quench_buffer",
               "Quench with a carrier-protein buffer so the released cells do not stick to the channel walls.",
               fraction=0.5)
    p.exchange("m5_harvest", "wash_buffer",
               "Wash twice and pass the suspension through the retention mesh twice, which removes clumps above 30 um.",
               fraction=0.5, washes=2)
    p.release("m5_harvest", "release_buffer",
              "Deliver a counted, filtered single-cell suspension to the outlet, ready for the encapsulation chip.")

    return {
        "id": "ipsc_to_mgl_v1",
        "name": "iPSC to microglia-like cells, end to end on chip",
        "source": SOURCE,
        "placeholder": False,
        "description": (
            "A published monolayer, embryoid-body-free, sorting-free differentiation from iPSCs to "
            "microglia-like cells, laid out across five linked modules so that nothing is handled off-chip. "
            "The one vessel transfer in the published method becomes a channel, and each centrifugation "
            "becomes a retention mesh with liquid flowing past it. Runs about 47 days end to end, of which "
            "the published schedule accounts for 40. Reagent names are generic; see the source citation for "
            "compositions. Flow rates are computed from chamber geometry to keep wall shear at or below "
            "0.01 dyn/cm^2, which is below the lowest shear reported to activate this cell type."
        ),
        "steps": p.steps,
    }


def report() -> None:
    print(f"{'chamber':<16}{'w x h (mm)':<14}{'flow':>10}{'shear':>12}{'ceiling flow':>14}")
    for cid, c in CH.items():
        w, h = c["size"][0], c["size"][2]
        print(f"{cid:<16}{f'{w} x {h}':<14}{FLOW[cid]:>7.0f} uL/min"
              f"{shear_dyn_cm2(FLOW[cid], w, h):>9.4f} dyn/cm2{max_flow(w, h):>9.1f} uL/min")


if __name__ == "__main__":
    import sys

    if "--report" in sys.argv:
        report()
    protocol = build()
    OUT.write_text(json.dumps(protocol, indent=2) + "\n")
    total = sum(s["duration_s"] for s in protocol["steps"])
    print(f"wrote {OUT.name}: {len(protocol['steps'])} steps, {total / DAY:.1f} days of protocol time")

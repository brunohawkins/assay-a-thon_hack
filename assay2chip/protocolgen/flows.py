"""Where every flow rate in a generated protocol comes from.

    tau = 6 * mu * Q / (w * h^2)        SI in, Pa out; x10 for dyn/cm2

`w` is the width ACROSS the flow of the real channel and `h` the etch depth, both from
`geometry.cross_section`, never from a bounding box.

THREE CEILINGS, AND THEY ARE NOT INTERCHANGEABLE. `assay2chip/lib/contracts.py` owns the
numbers and the scoping; this module only applies whichever one the op's `shear_class`
selects:

  adherent_cells   wall shear <= shear.culture_ceiling_dyn_cm2 (0.01). ADHERENT CELLS
                   ONLY - the risk being priced is detaching them.
  prelysis_rinse   wall shear <= shear.prelysis_rinse_ceiling_dyn_cm2 (0.3). A rinse
                   immediately before a deliberate lyse. Held to the culture ceiling
                   instead, that rinse takes about 12 minutes and perturbs the cells for
                   700x longer than the quench it is meant to serve.
  capsules         NO wall-shear ceiling at all. A capsule is not stuck to anything; the
                   limit is pressure across the retention feature,
                   capsule.escape_pressure_kpa x control.pressure_margin.

The pressure limit is a DEVICE limit, so it applies to every step whatever the contents,
and a flow is the lowest of the bounds that apply. A back-flush or release of a capsule
bed is a third case: below backflush.lift_flow_ul_min the bed does not come off the weir
at all, so that contract value is the flow, bounded above by pressure - lowering it to
look gentle would simply fail to move the capsules.

Viscosity comes from `contracts.viscosity_for(temp_c)`: 0.8 mPa s at 37 C, and water at
20 C only for the dye demo.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "operator") not in sys.path:
    sys.path.insert(0, str(ROOT / "operator"))

import flow_calc  # noqa: E402  the repo's rectangular-channel and mesh resistance maths

from assay2chip.lib import contracts  # noqa: E402

from .geometry import Chip, CrossSection, Segment  # noqa: E402

# Fraction of a wall-shear ceiling a step is allowed to use. A process choice, not a
# contract value: there is no contract key for it, so it is named here, stated in every
# note it affects, and kept at the 15% headroom protocols/build_module3.py already uses.
SHEAR_HEADROOM = 0.85

# Pumps are set in finite increments, so a flow is rounded DOWN onto a settable grid:
# rounding up would put the step over the ceiling it was derived from.
FLOW_GRID = (0.1, 0.01, 0.001, 1e-4, 1e-5, 1e-6)

LIFT_FLOW_KEY = "backflush.lift_flow_ul_min"
MESH_OPEN_FRACTION_KEY = "mesh.open_fraction"
BED_RESISTANCE_KEY = "mesh.bed_resistance_kpa_per_ul_min"

PARTICLE_BED_KINDS = ("capsules", "beads")


class FlowError(ValueError):
    """A flow cannot be derived from what the chip and the contracts actually say."""


@dataclass(frozen=True)
class Flow:
    """One derived flow, with everything a reviewer needs to check it."""

    flow_ul_min: float
    bound_by: str              # 'wall_shear' | 'escape_pressure' | 'lift_flow'
    ceiling: str               # the ceiling in words, with its contract key
    fraction: float            # the fraction of that ceiling this flow uses
    shear_dyn_cm2: float       # wall shear in the target chamber at this flow
    pressure_kpa: float        # predicted drop over the whole route at this flow
    limit_kpa: float
    section: CrossSection
    why: str                   # one sentence, goes into the step's note
    assumptions: tuple = ()    # contract keys the contract itself marks as assumptions


def ceiling_key(entry) -> str:
    """The contract key behind a ShearCeiling, for quoting in a note.

    `contracts.ShearCeiling.source` is 'contracts/parameters.json <key>', so the key is
    its last word. Quoting the key rather than the number is the point: a reader can look
    it up, and a stale copy of the number cannot hide in the prose.
    """
    return entry.source.split()[-1]


def wall_shear_dyn_cm2(flow_ul_min: float, width_mm: float, height_mm: float,
                       viscosity_pa_s: float) -> float:
    """tau = 6 mu Q / (w h^2), in dyn/cm2 (1 Pa = 10 dyn/cm2)."""
    q = flow_ul_min * 1e-9 / 60.0
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (6.0 * viscosity_pa_s * q / (w * h * h)) * 10.0


def flow_at_shear(ceiling_dyn_cm2: float, width_mm: float, height_mm: float,
                  viscosity_pa_s: float) -> float:
    """The flow in uL/min at which wall shear reaches the ceiling. Inverse of the above."""
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (ceiling_dyn_cm2 / 10.0) * w * h * h / (6.0 * viscosity_pa_s) * 60.0 * 1e9


def quantise(flow_ul_min: float) -> float:
    """The largest pump-settable flow at or below this one. Never zero."""
    for step in FLOW_GRID:
        value = math.floor(flow_ul_min / step) * step
        if value > 0:
            return round(value, 6)
    return flow_ul_min


def segment_resistance(segment: Segment, viscosity_pa_s: float, footprint_mm2: float,
                       particles: str) -> float:
    """kPa per uL/min for one piece of the route.

    A retention feature is a mesh or weir, not a channel: it is treated as parallel
    openings (operator/flow_calc.mesh_clean_resistance), and when particles are held on
    it the bed on top dominates - which is the whole reason the pressure limit exists.
    """
    if segment.kind == "retention":
        gap_um = segment.section.height_mm * 1000.0
        clean = flow_calc.mesh_clean_resistance(
            gap_um, footprint_mm2, float(contracts.param(MESH_OPEN_FRACTION_KEY)), viscosity_pa_s)
        bed = float(contracts.param(BED_RESISTANCE_KEY)) if particles in PARTICLE_BED_KINDS else 0.0
        return clean + bed
    if segment.length_mm <= 0:
        return 0.0
    return flow_calc.channel_resistance(segment.length_mm, segment.section.width_mm,
                                        segment.section.height_mm, viscosity_pa_s)


def route_resistance(segments: list, viscosity_pa_s: float, footprint_mm2: float,
                     particles: str = "none") -> float:
    return sum(segment_resistance(s, viscosity_pa_s, footprint_mm2, particles) for s in segments)


def derive(chip: Chip, target: str, shear_class: str, feed: str, vent: str,
           temp_c: float = None, particles: str = "none", purpose: str = "forward",
           also_bounded: list = None) -> Flow:
    """The flow for one step, from the chip's geometry and the ceiling its contents select.

    `also_bounded` is [(chamber_id, shear_class), ...] for chambers further down the route
    that the same flow passes through: the liquid leaving one chamber arrives in the next
    at the same Q, so the step is held to the tightest of them. Channels are NOT held to a
    wall-shear ceiling - a narrow channel and a retention gap always see more shear than
    the chamber, and holding a channel to a culture ceiling would stop the chip working.
    """
    mu = contracts.viscosity_for(temp_c)
    section = chip.cross_section(target)
    chamber = chip.chamber(target)
    footprint = float(chamber["size"][0]) * float(chamber["size"][1])
    segments = chip.segments(feed, target, vent)
    resistance = route_resistance(segments, mu.pa_s, footprint, particles)
    limit_kpa = contracts.max_working_pressure_kpa()

    candidates = []           # (flow, bound_by, ceiling text, fraction, assumption keys)
    pressure_cap = limit_kpa / resistance if resistance > 0 else float("inf")
    candidates.append((pressure_cap, "escape_pressure",
                       f"{limit_kpa:g} kPa across the route "
                       f"(capsule.escape_pressure_kpa x control.pressure_margin)",
                       contracts.pressure_margin(),
                       ("capsule.escape_pressure_kpa", "control.pressure_margin")))

    ceiling = contracts.shear_ceiling_for(shear_class)
    for chamber_id, klass in [(target, shear_class)] + list(also_bounded or []):
        entry = contracts.shear_ceiling_for(klass)
        if entry.ceiling_dyn_cm2 is None:
            continue
        here = chip.cross_section(chamber_id)
        cap = flow_at_shear(entry.ceiling_dyn_cm2, here.width_mm, here.height_mm, mu.pa_s)
        where = "" if chamber_id == target else f" in '{chamber_id}' downstream"
        candidates.append((cap * SHEAR_HEADROOM, "wall_shear",
                           f"{entry.ceiling_dyn_cm2:g} dyn/cm2 wall shear{where} "
                           f"({ceiling_key(entry)})",
                           SHEAR_HEADROOM, (ceiling_key(entry),)))

    if purpose == "lift":
        lift = float(contracts.param(LIFT_FLOW_KEY))
        candidates.append((lift, "lift_flow",
                           f"the {lift:g} uL/min that lifts a retained bed off the "
                           f"retention feature ({LIFT_FLOW_KEY})", 1.0, (LIFT_FLOW_KEY,)))

    chosen = min(candidates, key=lambda c: c[0])
    flow = quantise(chosen[0])
    if flow <= 0:
        raise FlowError(
            f"no positive flow satisfies {chosen[2]} in chamber '{target}' of chip {chip.id}: "
            f"the geometry or the ceiling has to change, not the flow"
        )
    shear = wall_shear_dyn_cm2(flow, section.width_mm, section.height_mm, mu.pa_s)
    pressure = flow_calc.pressure_kpa(resistance, flow)

    if chosen[1] == "lift_flow":
        why = (f"{flow:g} uL/min is the lift flow for a retained bed ({LIFT_FLOW_KEY}); "
               f"below it the bed does not come off the retention feature. It stays under "
               f"the {limit_kpa:g} kPa pressure limit ({pressure:.2f} kPa predicted)")
    elif chosen[1] == "wall_shear":
        why = (f"{flow:g} uL/min is {SHEAR_HEADROOM:.0%} of the flow that would reach "
               f"{chosen[2]}, using {section.why} and {mu.pa_s * 1000:g} mPa s at "
               f"{mu.temp_c:g} C ({mu.key}); wall shear here is {shear:.4f} dyn/cm2")
    else:
        why = (f"{flow:g} uL/min keeps the predicted drop over the whole route at or below "
               f"{chosen[2]} ({pressure:.2f} kPa at this flow, resistance "
               f"{resistance:.3f} kPa per uL/min), using {section.why}")
    if ceiling.ceiling_dyn_cm2 is None and shear_class == "capsules":
        why += ("; capsule handling has no wall-shear ceiling - the capsules are not "
                "attached to anything, so pressure is the limit")

    keys = set(chosen[4]) | {mu.key}
    return Flow(flow, chosen[1], chosen[2], chosen[3], shear, pressure, limit_kpa, section, why,
                tuple(sorted(k for k in keys if _is_assumption(k))))


def _is_assumption(key: str) -> bool:
    try:
        return contracts.is_assumption(key)
    except contracts.ContractError:
        return False


def duration_s(volume_ul: float, washes: int, flow_ul_min: float, margin: float,
               repeats: int = 1) -> int:
    """volume x washes / flow, plus the stated margin, in whole seconds (rounded up).

    `repeats` is the number of targets a step runs over: the contract says an exchange
    with `targets` runs once per target, so its duration has to cover all of them.
    """
    if flow_ul_min <= 0:
        raise FlowError("duration needs a positive flow")
    return int(math.ceil(volume_ul * washes * repeats / flow_ul_min * 60.0 * margin))

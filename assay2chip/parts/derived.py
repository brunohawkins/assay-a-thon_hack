"""Contract values that contracts/blocks.json states in PROSE, extracted not retyped.

Three numbers this registry needs are written into sentences rather than into keys:

  z tolerance          retention.weir_geometry.z_tolerance_warning  "around +/-20 um"
  lid support pitch    target_envelope.design_rules_at_this_scale   "about 5-10 mm pitch"
  DLD reference point  retention.alternatives_ranked[0]             "gap 200 um and eps 0.125
                                                                     ... ~105 um"

Retyping them here would be exactly the duplication assay2chip/README.md forbids, so this
module READS the sentence through assay2chip.lib.contracts and pulls the number out of it.
If the wording changes so a number can no longer be found, that is a ContractError naming
the key - never a fallback. When these graduate to real keys in contracts/blocks.json,
delete the regex and read the key.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402


def _number(text: str, pattern: str, key: str, what: str) -> tuple[float, ...]:
    m = re.search(pattern, text)
    if not m:
        raise contracts.ContractError(
            f"contracts/blocks.json {key} no longer states {what} in a form this can read "
            f"(looked for /{pattern}/). Either restore the wording or promote it to a real "
            f"key and read the key - do not pick a number in assay2chip/parts/."
        )
    return tuple(float(g) for g in m.groups())


def z_tolerance_um() -> float:
    """Process z tolerance, the number that makes every depth feature a risk.

    On a 25 um weir gap it is +/-80%, which blocks.json calls the biggest unknown in the
    retention scheme; on a metered volume at 0.3 mm it is +/-6.7%.
    """
    key = "retention.weir_geometry.z_tolerance_warning"
    (um,) = _number(str(contracts.block(key)), r"\+/-\s*(\d+(?:\.\d+)?)\s*um", key, "a z tolerance")
    return um


def lid_support_pitch_mm() -> tuple[float, float]:
    """(loosest, tightest) pillar_array pitch that holds a 0.3 mm lid flat.

    Returned as (pitch_mm_tight, pitch_mm_loose) = (5, 10): a span wider than the LOOSE
    end needs support at all, and when support is needed this registry uses the TIGHT end,
    which is the conservative choice inside the contract's own band.
    """
    key = "target_envelope.design_rules_at_this_scale"
    rules = contracts.block(key)
    text = next((r for r in rules if "pillar_array" in r), "")
    lo, hi = _number(text, r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*mm pitch", key, "a pitch band")
    return (min(lo, hi), max(lo, hi))


@dataclass(frozen=True)
class DldReference:
    """The one worked DLD point blocks.json gives, used to check our correlation."""

    gap_um: float
    epsilon: float
    critical_diameter_um: float
    gap_tolerance_um: float
    source: str


def dld_reference() -> DldReference:
    key = "retention.alternatives_ranked"
    text = next((a for a in contracts.block(key) if a.startswith("dld_array")), "")
    (gap,) = _number(text, r"gap (\d+(?:\.\d+)?) um", key, "a DLD gap")
    (eps,) = _number(text, r"eps (\d+(?:\.\d+)?)", key, "a DLD row shift")
    (dc,) = _number(text, r"~(\d+(?:\.\d+)?) um", key, "a DLD critical diameter")
    (tol,) = _number(text, r"\+/-(\d+(?:\.\d+)?) um gap tolerance", key, "a DLD gap tolerance")
    return DldReference(gap, eps, dc, tol, "contracts/blocks.json retention.alternatives_ranked[0]")


EXCESS_DENSITY_KEYS = {
    "cells": "cell.excess_density_kg_m3",
    "capsules": "capsule.excess_density_kg_m3",
    "beads": "bead.excess_density_kg_m3",
}
FLUID_DENSITY_KEY = "fluid.density_37c_kg_m3"

# Which end of an excess-density band is the conservative one depends entirely on the
# question being asked, because Stokes settling is LINEAR in excess density:
#
#   retain  a sizer RELYING on settling to hold particles takes MIN, the slowest settling,
#           so it never promises retention the slow end of the band would not deliver.
#   loss    a sizer CHECKING FOR LOSS by settling - a delay line, a feed line, a mixer -
#           takes MAX, the fastest settling, so it never misses a line that deposits.
#
# Taking the nominal value in both cases would be optimistic in both directions at once.
PURPOSE_END = {"retain": "min", "loss": "max"}


@dataclass(frozen=True)
class Density:
    """One excess density, the end of the band it came from, and why that end."""

    key: str
    value: float
    end: str
    band: tuple[float, float]
    nominal: float
    purpose: str
    assumption: str
    source: str


def fluid_density_kg_m3() -> float:
    """Medium density at 37 C (fluid.density_37c_kg_m3).

    Only ever feeds a Reynolds number here, and the band is within 1% either way, so it
    never decides a verdict on its own - which is why this takes the nominal value.
    """
    return float(contracts.param(FLUID_DENSITY_KEY))


def fluid_density_assumption() -> str:
    lo, hi = contracts.param_range(FLUID_DENSITY_KEY)
    return (f"{FLUID_DENSITY_KEY} = {fluid_density_kg_m3():g} kg/m3 (band {lo:g}-{hi:g}), "
            f"marked assumption: true in contracts/parameters.json. Used only for the "
            f"Reynolds number, never for a dimension, and the band is within 1% either "
            f"way.")


def excess_density(particles: str, purpose: str) -> Density:
    """Particle-minus-medium density, at the conservative end for the question asked.

    `purpose` is 'retain' (relying on settling, so MIN) or 'loss' (checking for settling,
    so MAX). One key per particle kind, because a single shared particle density made
    cells, capsules and beads settle identically - and beads settle 12x faster than a
    capsule of the same size.
    """
    if particles not in EXCESS_DENSITY_KEYS:
        raise contracts.ContractError(
            f"no excess density for particles={particles!r}: the contract has one per kind "
            f"({', '.join(sorted(EXCESS_DENSITY_KEYS))})")
    if purpose not in PURPOSE_END:
        raise contracts.ContractError(
            f"purpose must be 'retain' (use the min of the band) or 'loss' (use the max), "
            f"not {purpose!r}")
    key = EXCESS_DENSITY_KEYS[particles]
    end = PURPOSE_END[purpose]
    lo, hi = contracts.param_range(key)
    if lo is None or hi is None:
        raise contracts.ContractError(
            f"contracts/parameters.json {key} has no min/max band, and the whole point of "
            f"this accessor is picking the conservative end of one")
    nominal = float(contracts.param(key))
    value = float(lo if end == "min" else hi)
    which = "slowest settling" if end == "min" else "fastest settling"
    assumption = (
        f"{key} = {value:g} kg/m3, the {end} of the contract's {lo:g}-{hi:g} band "
        f"(nominal {nominal:g}), marked assumption: true in contracts/parameters.json. "
        f"Settling is LINEAR in it, so this part took the {which} end because it is "
        f"{'relying on settling to retain' if purpose == 'retain' else 'checking for loss by settling'}.")
    return Density(key, value, end, (float(lo), float(hi)), nominal, purpose, assumption,
                   f"contracts/parameters.json {key}")


def capsule_settling_band_um_s() -> tuple[float, float]:
    """(low, high) capsule settling speed the contract states, 50-210 um/s.

    From retention.alternatives_ranked, the sedimentation-sump entry. It is the only
    measured-ish settling figure anywhere in the contracts, so the Stokes estimate in
    physics.py is checked against it rather than trusted on its own.
    """
    key = "retention.alternatives_ranked"
    text = next((a for a in contracts.block(key) if "Sedimentation sump" in a), "")
    lo, hi = _number(text, r"~(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\s*um/s", key,
                     "a settling-speed band")
    return (min(lo, hi), max(lo, hi))


def weir_gap_um() -> float:
    """The agreed weir gap (retention.weir_geometry.gap_um): the only figure to quote."""
    return float(contracts.block("retention.weir_geometry.gap_um"))


def weir_crest_thickness_um() -> float:
    """IN-PLANE thickness of the crest. It is the 200 um floor, not a height."""
    return float(contracts.block("retention.weir_geometry.crest_thickness_um"))


def weir_retains_um() -> float:
    """What the agreed weir is dimensioned to hold back (70 um, a capsule)."""
    return float(contracts.block("retention.weir_geometry.retains_um"))


def weir_pressure_drop_pa_at_100_ul_min() -> float:
    return float(contracts.block("retention.weir_geometry.pressure_drop_pa_at_100_ul_min"))


def weir_gap_band_um(diameter_um: float) -> tuple[float, float]:
    """[low, high) legal gap band for a particle of this diameter.

    From trap.gap_factor_low / trap.gap_factor_high. The upper bound is STRICT: both
    tools/check_chip_spec.py and blocks.json's own rule fail a gap equal to it.
    """
    return (
        float(contracts.param("trap.gap_factor_low")) * diameter_um,
        float(contracts.param("trap.gap_factor_high")) * diameter_um,
    )

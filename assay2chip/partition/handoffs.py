"""What moves between two chips, and what can go wrong in the line.

The risks are **computed from this handoff's own numbers**, not picked from a list of
things that generally go wrong with tubing. Three of them, and each one has already bitten
this repo or is one division away from doing so:

**Dead volume.** The line is ``tubing.inner_diameter_mm`` by ``tubing.length_mm`` plus
``selector_valve.internal_volume_ul`` of swept valve, all three from
``contracts/parameters.json``, and widened when the largest thing in the line needs more
than that bore (``capsule_channel.width_factor_low`` x the swollen particle). Bore and
length give the volume left behind, and that is compared with the volume being carried.
Moving 9 uL through a line holding 6 uL is a different proposition from moving 900 uL
through it, and the ratio says which one this is.

**Stokes settling.** Particles fall at ``v = dRho g d^2 / 18 mu`` with ``mu`` from
``contracts.viscosity_for(temp_c)`` - the 37 C value, 0.0008 Pa s, not the 20 C water of
the dye demo, which would overstate the resistance by 25% and understate the settling.
The excess density is per particle kind - ``cell.excess_density_kg_m3``,
``capsule.excess_density_kg_m3``, ``bead.excess_density_kg_m3`` - against
``fluid.density_37c_kg_m3``.

Each of those is a band, and the contract is explicit about which end to take: "use max
when predicting cells LOST by settling in a line or mixer, min when RELYING on settling to
retain cells in a chamber, and report which end you used". A handoff risk is a loss
question, so this module takes the **maximum** of the band every time and says so, in the
risk sentence and in ``unresolved``. The useful form is not the velocity but the **time to
fall across the bore**: once that is known, the transfer either finishes first or the
particles bed in the line, and the minimum flow rate that beats it is a number an operator
can set. As a sanity check, the same expression at the band's *central* value reproduces
the 50-210 um/s ``blocks.json retention.alternatives_ranked`` quotes for its sedimentation
sump.

**Volume mismatch.** The upstream chip releases one volume and the downstream chip draws
another. This repo's own chain does exactly this and the gap is big:
``protocols/pipeline_status.md`` records "Module3 releases 76.9 uL; module4 draws 9.6 uL
at near-packed density. Concentration step undocumented." So the two volumes are compared
against ``control.error_threshold`` (0.15), the relative error the control loop already
uses, and a mismatch that needs concentrating makes the handoff **not automated**, because
the missing step is a spin at the bench.

``automated`` is never set true to improve the headline. An off-chip op in the gap makes
it false unless the op's own prose names a machine that genuinely does the work, and a
pipette, a tube, a spin or a strainer in that prose keeps it false whatever else it says.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from assay2chip.lib import contracts
from assay2chip.partition.ops import Op

#: Standard gravity, m/s2. A physical constant, not a contract value.
G_M_S2 = 9.80665

#: Excess density over the carrier, per particle kind, by contract key. One key per kind:
#: a cell, a hydrogel capsule and a magnetic bead do not settle alike, and a single shared
#: "particle" density used to make them settle identically. `library` travels on the
#: clean-up beads, so it reads the bead key.
EXCESS_DENSITY_KEYS: Dict[str, str] = {
    "cells": "cell.excess_density_kg_m3",
    "capsules": "capsule.excess_density_kg_m3",
    "beads": "bead.excess_density_kg_m3",
    "library": "bead.excess_density_kg_m3",
}

CARRIER_DENSITY_KEY = "fluid.density_37c_kg_m3"
TUBING_BORE_KEY = "tubing.inner_diameter_mm"
TUBING_LENGTH_KEY = "tubing.length_mm"
VALVE_VOLUME_KEY = "selector_valve.internal_volume_ul"

#: Which end of a banded contract value a loss question takes. The contract's own note on
#: cell.excess_density_kg_m3 sets the rule: max when predicting what is LOST by settling,
#: min when RELYING on settling to retain something. A handoff is always the former.
LOSS_END = "max"

#: Prose that means a machine does it unattended.
AUTOMATABLE = ("selector valve", "rotary valve", "syringe pump", "peristaltic",
               "liquid handler", "autosampler", "robot", "thermocycler", "incubator",
               "pressure controller")

#: Prose that means a human does it, whatever else the sentence says.
MANUAL = ("pipette", "pipetting", "by hand", "manual", "bench", "tube", "spin",
          "centrifuge", "vortex", "strainer", "mesh insert", "carry", "plate reader",
          "qubit", "tapestation", "ice", "fridge")


@dataclass(frozen=True)
class LineGeometry:
    bore_mm: float
    length_mm: float
    valve_ul: float
    why: str
    widened: bool = False

    @property
    def dead_volume_ul(self) -> float:
        """Swept volume of the tube plus the selector valve. 1 mm3 == 1 uL."""
        return math.pi / 4.0 * self.bore_mm ** 2 * self.length_mm + self.valve_ul


def line_for(particle_um: Optional[float]) -> LineGeometry:
    """The off-chip line between two chips, from the contract's own tubing keys.

    The bore is ``tubing.inner_diameter_mm`` unless the largest thing in the line needs
    more than that, in which case it is widened to
    ``capsule_channel.width_factor_low`` x the particle and the caller is told, because a
    bore the contract does not have is a procurement question.
    """
    bore = float(contracts.param(TUBING_BORE_KEY))
    length = float(contracts.param(TUBING_LENGTH_KEY))
    valve = float(contracts.param(VALVE_VOLUME_KEY))
    if particle_um is None:
        return LineGeometry(
            bore, length, valve,
            "%g mm bore over %g mm plus %g uL of valve (%s, %s, %s); no particle stated"
            % (bore, length, valve, TUBING_BORE_KEY, TUBING_LENGTH_KEY, VALVE_VOLUME_KEY))
    factor = contracts.param("capsule_channel.width_factor_low")
    needed = factor * particle_um / 1000.0
    if needed <= bore:
        return LineGeometry(
            bore, length, valve,
            "%g mm bore (%s) over %g mm (%s) plus %g uL of valve (%s); it passes a %g um "
            "particle, which needs %g x its diameter = %.2f mm"
            % (bore, TUBING_BORE_KEY, length, TUBING_LENGTH_KEY, valve, VALVE_VOLUME_KEY,
               particle_um, factor, needed))
    return LineGeometry(
        needed, length, valve,
        "a %g um particle needs %g x its diameter (capsule_channel.width_factor_low) = "
        "%.2f mm of bore, wider than the %g mm in %s"
        % (particle_um, factor, needed, bore, TUBING_BORE_KEY),
        widened=True)


def excess_density(carries: str) -> Tuple[float, str, str]:
    """(kg/m3, contract key, which end of the band) for what the line carries.

    Always the MAX of the band: a handoff asks what is LOST in the line, settling is
    linear in this value, and the contract's note on ``cell.excess_density_kg_m3`` says to
    take the conservative end for the question being asked and to report which end it was.
    """
    key = EXCESS_DENSITY_KEYS.get(carries, EXCESS_DENSITY_KEYS["capsules"])
    low, high = contracts.param_range(key)
    if high is not None:
        return float(high), key, LOSS_END
    return float(contracts.param(key)), key, "value"


def settling_velocity_m_s(particle_um: float, carries: str,
                          temp_c: Optional[float]) -> Tuple[float, str]:
    """Stokes terminal velocity, and the viscosity key it used.

    Worst case by design: see ``excess_density``.
    """
    viscosity = contracts.viscosity_for(temp_c)
    delta, _key, _end = excess_density(carries)
    diameter_m = particle_um * 1e-6
    velocity = delta * G_M_S2 * diameter_m ** 2 / (18.0 * viscosity.pa_s)
    return velocity, viscosity.key


@dataclass
class Handoff:
    """One partition.json handoff entry, plus the gaps computing it opened."""

    data: Dict[str, object]
    gaps: List[Dict[str, object]] = field(default_factory=list)

    @property
    def automated(self) -> bool:
        return bool(self.data.get("automated"))


def carries_for(upstream: Sequence[Op], downstream: Sequence[Op]) -> str:
    """One of partition.schema.json's four: cells, capsules, library, supernatant."""
    amplified = any(op.kind == "thermocycle" for op in upstream)
    last_particles = [op.particle_kind for op in upstream if op.has_particles]
    kind = last_particles[-1] if last_particles else "none"
    if kind == "capsules" and not amplified:
        return "capsules"
    if kind == "cells":
        return "cells"
    if amplified or kind == "beads":
        return "library"
    return "supernatant"


def _automation(gap_ops: Sequence[Op], carries: str, line: LineGeometry,
                mismatch: Optional[Tuple[float, float]],
                transfer: Optional[Op] = None) -> Tuple[bool, str]:
    """Can a machine do this unattended, and by what mechanism.

    `transfer` is an on-chip ``transfer`` op sitting on this boundary. The assay asked for
    the move explicitly, so its own prose decides: a named machine makes it automated, and
    a pipette, tube or spin in that prose keeps it manual however it is phrased.
    """
    if mismatch is not None:
        release, draw = mismatch
        return False, ("manual concentration step: the upstream chip releases %g uL and "
                       "the next draws %g uL" % (release, draw))
    for op in list(gap_ops) + ([transfer] if transfer is not None else []):
        manual = [word for word in MANUAL if word in op.text]
        if manual:
            return False, ("%s is done at the bench (%s says %s)"
                           % (op.label(), "the op's own note", manual[0]))
    if gap_ops:
        texts = " ".join(op.text for op in gap_ops)
        machines = [word for word in AUTOMATABLE if word in texts]
        if not machines:
            return False, ("off-chip work between the chips (%s) with no machine named "
                           "for it" % ", ".join(op.label() for op in gap_ops))
        return True, ("%s, driven by the %s named in the op"
                      % (", ".join(op.label() for op in gap_ops), machines[0]))
    if transfer is not None:
        machines = [word for word in AUTOMATABLE if word in transfer.text]
        return True, ("%s across the boundary: %s"
                      % (transfer.label(),
                         "driven by the %s named in the op" % machines[0] if machines
                         else "tubing + selector valve, driven by a syringe pump"))
    if carries in ("capsules", "cells", "library", "supernatant"):
        return True, "tubing + selector valve, driven by a syringe pump"
    return False, "mechanism not established"


def build(from_chip: str, from_port: str, to_chip: str, to_port: str,
          upstream: Sequence[Op], downstream: Sequence[Op],
          gap_ops: Sequence[Op] = ()) -> Handoff:
    """One handoff with computed volumes, particle size, risks and automation verdict.

    `gap_ops` are the off-chip ops that fall between the two chips. A ``transfer`` op on
    either side of the boundary is picked up automatically: the assay asked for the move,
    so it is this handoff rather than work either board does.
    """
    carries = carries_for(upstream, downstream)
    gaps: List[Dict[str, object]] = []
    risks: List[str] = []

    release = next((op.volume_ul for op in reversed(list(upstream))
                    if op.volume_ul is not None), None)
    draw = next((op.volume_ul for op in downstream if op.volume_ul is not None), None)

    particle = max([op.swollen_um for op in list(upstream) + list(downstream)
                    if op.swollen_um is not None] or [0.0]) or None

    line = line_for(particle)
    for key in (TUBING_BORE_KEY, TUBING_LENGTH_KEY, VALVE_VOLUME_KEY):
        if contracts.is_assumption(key):
            low, high = contracts.param_range(key)
            gaps.append({
                "field": key,
                "question": "the line between %s and %s uses %s, which the contract marks "
                            "an assumption%s - no rig layout exists yet, and dead volume "
                            "and transit time both depend on it"
                            % (from_chip, to_chip, key,
                               " (band %g-%g)" % (low, high) if low is not None else ""),
                "assumed": contracts.param(key), "assumed_from": key,
            })
    if line.widened:
        gaps.append({
            "field": TUBING_BORE_KEY,
            "question": "a %g um particle needs a %.2f mm bore, wider than the %g mm in "
                        "%s: which tubing is actually used between %s and %s?"
                        % (particle, line.bore_mm, contracts.param(TUBING_BORE_KEY),
                           TUBING_BORE_KEY, from_chip, to_chip),
            "assumed": line.bore_mm,
            "assumed_from": "contracts/parameters.json capsule_channel.width_factor_low",
        })

    # --- dead volume ---------------------------------------------------------
    dead = line.dead_volume_ul
    threshold = contracts.param("control.error_threshold")
    if release is not None and release > 0:
        share = dead / release
        if share > threshold:
            risks.append(
                "dead volume: %.1f uL sits in the %.2f mm bore over %g mm of line "
                "(%s, %s) plus %g uL of selector valve (%s), %.0f%% of the %g uL "
                "carried, which is above the %g relative error control.error_threshold "
                "allows - prime the line, shorten it or take the %g mm end of the band"
                % (dead, line.bore_mm, line.length_mm, TUBING_BORE_KEY,
                   TUBING_LENGTH_KEY, line.valve_ul, VALVE_VOLUME_KEY, 100 * share,
                   release, threshold, contracts.param_range(TUBING_LENGTH_KEY)[0]))
        else:
            risks.append("dead volume %.1f uL (%.2f mm x %g mm of tubing plus %g uL of "
                         "valve, from %s, %s and %s) is %.0f%% of the %g uL carried, "
                         "inside control.error_threshold (%g)"
                         % (dead, line.bore_mm, line.length_mm, line.valve_ul,
                            TUBING_BORE_KEY, TUBING_LENGTH_KEY, VALVE_VOLUME_KEY,
                            100 * share, release, threshold))
    else:
        risks.append("dead volume %.1f uL in the line (%s, %s, %s), but the volume "
                     "carried is not stated, so the loss cannot be bounded"
                     % (dead, TUBING_BORE_KEY, TUBING_LENGTH_KEY, VALVE_VOLUME_KEY))

    # --- settling ------------------------------------------------------------
    if particle:
        delta, density_key, end = excess_density(carries)
        carrier = contracts.param(CARRIER_DENSITY_KEY)
        velocity, key = settling_velocity_m_s(particle, carries, _temp_of(upstream))
        if velocity > 0:
            fall_s = (line.bore_mm / 1000.0) / velocity
            risks.append(
                "sedimentation: a %g um %s, %g kg/m3 denser than the %g kg/m3 carrier "
                "(%s, %s of its band, and %s), settles at %.0f um/s by Stokes at %s and "
                "crosses the %.2f mm bore in %.0f s%s"
                % (particle, carries, delta, carrier, density_key, end,
                   CARRIER_DENSITY_KEY, velocity * 1e6, key, line.bore_mm, fall_s,
                   _flow_clause(release, fall_s)))
            low, high = contracts.param_range(density_key)
            gaps.append({
                "field": density_key,
                "question": "%s is an assumption in the contract (band %s-%s kg/m3) and "
                            "settling is linear in it; this handoff asks what is LOST in "
                            "the line, so it used the %s end, per the contract's own note"
                            % (density_key, low, high, end),
                "assumed": delta, "assumed_from": "%s (%s of the band)"
                                                  % (density_key, end),
            })
        port = contracts.floor("min_port_mm")
        if line.bore_mm > port:
            risks.append(
                "the %.2f mm bore this particle needs is wider than the %g mm "
                "fabrication_floors.min_port_mm port it mates, so both ports need "
                "enlarging or the particle needs shrinking" % (line.bore_mm, port))

    # --- volume mismatch -----------------------------------------------------
    # Only a SHORTFALL is a mismatch. A downstream step that handles more than arrives is
    # just adding its own reagents, which is what its feed ports are for; a downstream
    # step that handles less leaves material behind, and something has to concentrate it.
    mismatch: Optional[Tuple[float, float]] = None
    if release is not None and draw is not None and release > 0:
        shortfall = (release - draw) / release
        if shortfall > threshold:
            risks.append(
                "volume mismatch: %s releases %g uL and %s draws only %g uL, leaving "
                "%.0f%% behind against the %g relative error control.error_threshold "
                "allows; the concentration step between them is not in the source"
                % (from_chip, release, to_chip, draw, 100 * shortfall, threshold))
            mismatch = (release, draw)
            gaps.append({
                "field": "volume_ul",
                "question": "%s releases %g uL but %s draws %g uL: what concentration "
                            "step closes the gap?" % (from_chip, release, to_chip, draw),
                "assumed_from": "contracts/parameters.json control.error_threshold",
            })
    elif release is None or draw is None:
        gaps.append({
            "field": "volume_ul",
            "question": "the volume moving from %s to %s is not stated on both sides, so "
                        "the two chips cannot be checked against each other"
                        % (from_chip, to_chip),
        })

    boundary_transfer = next(
        (op for op in (list(upstream)[-1:] + list(downstream)[:1]) if op.is_transfer),
        None)
    automated, how = _automation(gap_ops, carries, line, mismatch, boundary_transfer)

    data: Dict[str, object] = {
        "from": from_chip, "from_port": from_port,
        "to": to_chip, "to_port": to_port,
        "carries": carries,
        "volume_ul": release,
        "particle_um": particle,
        "automated": automated,
        "how": how,
        "risks": risks,
        "dead_volume_ul": round(dead, 2),
    }
    notes = []
    if gap_ops:
        notes.append("off-chip between the two chips: %s" % "; ".join(
            "%s - %s" % (op.label(), op.why or "no reason given") for op in gap_ops))
    if boundary_transfer is not None:
        notes.append("the assay's own %s crosses this cut, so it is this handoff"
                     % boundary_transfer.label())
    if notes:
        data["note"] = " | ".join(notes)
    return Handoff(data, gaps)


def _temp_of(ops: Sequence[Op]) -> Optional[float]:
    for op in reversed(list(ops)):
        if op.temp_c is not None:
            return op.temp_c
    return None


def _flow_clause(volume_ul: Optional[float], fall_s: float) -> str:
    if not volume_ul or fall_s <= 0:
        return ", so the line must be flushed promptly or kept agitated"
    needed = volume_ul / fall_s * 60.0
    return (", so the %g uL has to move in under that - at least %.0f uL/min - or the "
            "particles bed in the line" % (volume_ul, needed))


def count_manual(handoffs: Sequence[Dict[str, object]]) -> int:
    """The headline number: how many handoffs still need a human."""
    return sum(1 for h in handoffs if not h.get("automated"))

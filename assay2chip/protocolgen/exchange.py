"""What an exchange is FOR, and the volume that follows from it.

`exchange_kind` exists because the distinction used to live only in English inside `note`,
where no tool could read it: one rule could not tell a deliberate half medium change from
a wash that silently failed to clear the chamber. Three kinds, from the schema:

  fill     the first liquid into an empty or air-filled chamber. Bounded ABOVE by the
           chamber's volume_ul - you cannot put in more than it holds - and no lower bound.
  sweep    clearing the chamber: a wash, a rinse, a reagent change. A well-mixed chamber
           keeps exp(-V) of the old liquid after V chamber volumes, so reaching
           exchange.target_fraction needs -ln(1 - target) chamber volumes IN TOTAL, and
           below one chamber volume the step cannot clear the chamber however often it is
           repeated.
  partial  a deliberate fractional change - a culture medium half-change, or a back-flush
           whose job is lifting a bed rather than clearing liquid. The conditioned medium
           left behind is the point, so the sweep floor does not apply, and the note must
           say what fraction is replaced.

ABSENCE IS NOT A LOOPHOLE: a step with no `exchange_kind` is held to the sweep rule, so
this module always writes the key explicitly and `classify` falls back to sweep, the
strict case, whenever the evidence is thin.

An exchange's `volume_ul` is the volume PER WASH and a wash is one fill-and-drain cycle,
so a step can never move more than the chamber holds in one go: five chamber volumes is
five washes of one, not one wash of five.

BENCH VOLUMES DO NOT TRANSFER (assay2chip/README.md). The paper's "wash in 500 uL" or
"lyse in 200 uL" describes a TUBE. `unitops.json` keeps it in `fluid.volume_ul` for
traceability only, and NOTHING here reads it. What survives is:

  a wash or rinse        the volume is dropped: it becomes a sweep of the built chamber.
  a reagent added to a
  sample                 the RATIO survives (`ratio.reagent_to_sample`), so the final
                         concentration matches the paper whatever the chamber holds. A
                         well-mixed chamber of volume V that takes V_in of reagent ends up
                         1 - exp(-V_in/V) reagent, so matching a reagent:sample ratio r
                         needs V_in = ln(1 + r) x V - 0.69 chamber volumes for 1:1. Past
                         r = e - 1 that reaches a whole chamber volume and the step is a
                         sweep instead.
  a first fill           one chamber volume, which is also its upper bound.

A ratio the paper does not give is NOT invented: `ratio_question` returns the open question
for the step's note, and the step is held to the sweep rule meanwhile.

Diffusion sets a floor on time, not volume: reagent has to get INSIDE a capsule or a gel,
which flushing faster does not help. `interior_soak_s` derives that floor from the
contracts (exchange.interior_target_fraction and capsule.diffusion_time_s) - the precedent
being protocols/build_sctip_spc.py, whose trap washes "take a few minutes however fast the
pump runs".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from assay2chip.lib import contracts

TARGET_FRACTION_KEY = "exchange.target_fraction"
INTERIOR_FRACTION_KEY = "exchange.interior_target_fraction"
DIFFUSION_TIME_KEY = "capsule.diffusion_time_s"

# Particles whose INSIDE a reagent has to reach, so time is set by diffusion and not by
# the pump. A gel-cored capsule and a bead bed behave the same way here.
DIFFUSION_LIMITED = ("capsules", "beads")

FILL, SWEEP, PARTIAL = "fill", "sweep", "partial"
KINDS = (PARTIAL, SWEEP, FILL)

# Op kinds whose point is getting the chamber's current contents OUT. Anything here is a
# sweep unless the chamber is empty (then it is the first fill).
CLEARING_KINDS = ("wash", "lyse", "separate", "collect", "pool")

# Op kinds that ADD a reagent to a sample (lysis, enzyme, master mix, label). These are the
# ones whose ratio survives from the paper, and the only ones that can come out `partial`.
# A `lyse` is in both lists: it is a reagent addition when the chamber's liquid is the
# sample, and a sweep when the sample is retained and the liquid is only carrier - which
# the chip decides, not the kind (see build.Plan.exchange).
RATIO_KINDS = ("lyse", "load", "merge", "mix", "incubate", "thermocycle", "cure")


class ExchangeError(ValueError):
    """An exchange cannot be sized from what the chip and the op actually say."""


@dataclass(frozen=True)
class Sizing:
    """One sized exchange: what to write in the step, and why."""

    kind: str
    volume_ul: float          # PER WASH
    washes: int
    chamber_volumes: float    # washes x volume / chamber volume
    fraction: float           # the fraction of the chamber's liquid this replaces
    why: str                  # the reason for the KIND, for the stage log
    note: str                 # the sentence the step's note must carry

    @property
    def total_ul(self) -> float:
        return self.volume_ul * self.washes


def target_fraction() -> float:
    """exchange.target_fraction: how much of the old liquid a sweep has to remove."""
    return float(contracts.param(TARGET_FRACTION_KEY))


def sweep_chamber_volumes(fraction: float = None) -> float:
    """-ln(1 - target): the chamber volumes a sweep needs to reach the target fraction.

    0.99 -> 4.605 chamber volumes. The floor is one chamber volume whatever the target,
    because below that the chamber cannot be cleared however many times the step repeats.
    """
    fraction = target_fraction() if fraction is None else fraction
    if not 0 < fraction < 1:
        raise ExchangeError(
            f"{TARGET_FRACTION_KEY} must be strictly between 0 and 1, not {fraction!r}: "
            f"1.0 would need infinite liquid"
        )
    return max(1.0, -math.log(1.0 - fraction))


def replaced_fraction(chamber_volumes: float) -> float:
    """The fraction of the old liquid gone after this many chamber volumes: 1 - exp(-V)."""
    return 1.0 - math.exp(-max(0.0, chamber_volumes))


def chamber_volumes_for_ratio(ratio: float) -> float:
    """Chamber volumes of reagent that leave the paper's reagent:sample ratio behind.

    A well-mixed chamber that takes V_in of reagent ends up 1 - exp(-V_in/V) reagent, so
    matching a ratio r means 1 - exp(-V_in/V) = r / (1 + r), i.e. V_in = ln(1 + r) x V.
    1:1 is 0.69 chamber volumes; 1:9 is 0.11; anything at or above e - 1 reaches a whole
    chamber volume, where the reagent has effectively displaced the sample and the step is
    a sweep instead of a partial.
    """
    if ratio is None or ratio <= 0:
        raise ExchangeError(
            f"a reagent:sample ratio must be a positive number, not {ratio!r}: a null ratio "
            f"is an open question for the protocol to carry, not a number to pick"
        )
    return min(math.log(1.0 + float(ratio)), 1.0)


def ratio_question(op_kind: str, chamber_volume_ul: float) -> str:
    """The open question a step has to carry when the paper's ratio is unknown.

    The paper's absolute volume cannot answer it: that volume describes a tube. So the
    step is sized as a sweep, which is the strict reading, and says what is missing.
    """
    return (f"OPEN QUESTION: the source never gives a reagent-to-sample ratio for this "
            f"'{op_kind}', and its tube volume cannot supply one - it describes a tube, not "
            f"this {chamber_volume_ul:g} uL chamber. The step is therefore sized as a full "
            f"sweep of the chamber, which is the strict reading; if the paper's intent was a "
            f"fractional addition, the ratio has to be read off the source and recorded in "
            f"unitops.json as ratio.reagent_to_sample")


def interior_soak_s() -> float:
    """Seconds a reagent needs to reach the INSIDE of a capsule or bead, per exposure.

    Derived, not typed in: a first-order interior lag of capsule.diffusion_time_s reaches
    exchange.interior_target_fraction after -ln(1 - target) time constants. Flushing faster
    does not help, which is why protocols/build_sctip_spc.py says its trap washes take a
    few minutes however fast the pump runs.
    """
    tau = float(contracts.param(DIFFUSION_TIME_KEY))
    target = float(contracts.param(INTERIOR_FRACTION_KEY))
    if not 0 < target < 1:
        raise ExchangeError(f"{INTERIOR_FRACTION_KEY} must be between 0 and 1, not {target!r}")
    return -math.log(1.0 - target) * tau


def classify(op_kind: str, chamber_is_empty: bool, ratio: float,
             chamber_volume_ul: float, direction: str = "forward") -> tuple:
    """(kind, why) for one exchange. Deterministic, and documented in this order.

    The paper's absolute volume is not an input here at all: see this module's docstring.

    1. The chamber holds nothing yet            -> fill    (there is nothing to clear)
    2. The liquid is pushed back through the
       retention feature to lift a bed          -> partial (sized by lift flow, not clearing)
    3. A reagent added to the sample at a ratio
       the paper states                         -> partial, or sweep when the ratio needs a
                                                  whole chamber volume anyway
    4. The op's point is clearing the chamber   -> sweep
    5. Anything else, including a ratio the
       source never gave                        -> sweep   (the strict default)
    """
    if chamber_is_empty:
        return FILL, (f"'{op_kind}' is the first liquid into the chamber, so there is "
                      f"nothing to clear: a fill, bounded above by the chamber volume")
    if direction == "backflush":
        return PARTIAL, ("a back-flush is sized by the flow that lifts the bed off the "
                         "retention feature, not by clearing the chamber's liquid, so it "
                         "is a partial that states the fraction it happens to replace")
    if op_kind in RATIO_KINDS and ratio:
        volumes = chamber_volumes_for_ratio(ratio)
        if volumes < 1.0:
            return PARTIAL, (f"'{op_kind}' adds a reagent at {ratio:g} reagent per sample, "
                             f"which is {volumes:.2f} chamber volumes: part of the sample "
                             f"stays, by design, so calling this a wash would be a lie")
        return SWEEP, (f"'{op_kind}' adds a reagent at {ratio:g} reagent per sample, which "
                       f"needs a whole chamber volume or more: the reagent displaces the "
                       f"sample, so the step is sized as a sweep")
    if op_kind in CLEARING_KINDS:
        return SWEEP, (f"'{op_kind}' exists to get the chamber's current contents out, so "
                       f"it must reach {TARGET_FRACTION_KEY}")
    return SWEEP, (f"nothing says '{op_kind}' is a deliberate fractional change, and an "
                   f"unclassified exchange is held to the sweep rule")


def size(kind: str, chamber_volume_ul: float, ratio: float = None,
         fraction: float = None) -> Sizing:
    """Volume per wash and wash count for an exchange of this kind, FROM THE CHAMBER.

    The only volume that enters is the built chamber's `volume_ul`. `ratio` is the paper's
    reagent:sample ratio, used by a partial to match the final concentration; no absolute
    volume from the source is read anywhere.

    A sweep is always whole chamber volumes, one wash each, because that is the largest a
    wash may be and it makes the arithmetic checkable at a glance.
    """
    if chamber_volume_ul <= 0:
        raise ExchangeError("an exchange needs a chamber with a positive volume_ul")
    volume = round(chamber_volume_ul, 3)

    if kind == FILL:
        return Sizing(FILL, volume, 1, 1.0, replaced_fraction(1.0),
                      "fill", f"one chamber volume, {volume:g} uL: a fill cannot put in more "
                              f"than the chamber holds, and the source's own volume is a tube "
                              f"volume that does not transfer")
    if kind == SWEEP:
        needed = sweep_chamber_volumes(fraction)
        washes = int(math.ceil(needed))
        cv = float(washes)
        return Sizing(SWEEP, volume, washes, cv, replaced_fraction(cv),
                      "sweep", f"{washes} fills of one {volume:g} uL chamber volume each, "
                               f"{cv:.0f} chamber volumes in total: reaching "
                               f"{target_fraction() if fraction is None else fraction:.0%} "
                               f"needs {needed:.2f}, since a well-mixed chamber keeps "
                               f"exp(-V) of the old liquid after V chamber volumes")
    if kind == PARTIAL:
        if not ratio:
            raise ExchangeError(
                "a partial exchange has to say what fraction it replaces, and the only "
                "honest source for that is the paper's reagent-to-sample ratio: give "
                "`ratio`, or classify the step as a sweep"
            )
        cv = chamber_volumes_for_ratio(ratio)
        volume = round(cv * chamber_volume_ul, 3)
        replaced = replaced_fraction(cv)
        return Sizing(PARTIAL, volume, 1, cv, replaced,
                      "partial", f"{volume:g} uL is {cv:.2f} chamber volumes and leaves the "
                                 f"chamber about {replaced:.0%} reagent, which is the "
                                 f"{ratio:g}:1 reagent-to-sample ratio the source states; a "
                                 f"deliberate fractional change, not an attempt to clear the "
                                 f"chamber. The source's absolute volume is a tube volume and "
                                 f"does not transfer")
    raise ExchangeError(f"exchange_kind must be one of {', '.join(KINDS)}, not {kind!r}")


def problems(step: dict, chamber_volume_ul: float, fraction: float = None) -> list:
    """Why this exchange step breaks its own `exchange_kind`. Empty list means it is sound.

    Runs on anybody's protocol, not just ours, which is how the hand-written protocols in
    protocols/ get checked against the same rule.
    """
    out = []
    kind = step.get("exchange_kind", SWEEP)
    volume, washes = step.get("volume_ul"), step.get("washes")
    if volume is None or washes is None:
        return [f"step {step.get('step')}: an exchange needs volume_ul and washes"]
    total = volume * washes
    cv = total / chamber_volume_ul
    if kind not in KINDS:
        return [f"step {step.get('step')}: exchange_kind {kind!r} is not one of "
                f"{', '.join(KINDS)}"]
    if volume > chamber_volume_ul + 1e-6:
        out.append(f"step {step.get('step')}: {volume:g} uL per wash is more than the "
                   f"{chamber_volume_ul:g} uL the chamber holds, and a wash is one "
                   f"fill-and-drain cycle")
    if kind == FILL and total > chamber_volume_ul + 1e-6:
        out.append(f"step {step.get('step')}: a fill is bounded above by the chamber volume, "
                   f"but this one puts in {total:g} uL into {chamber_volume_ul:g} uL")
    if kind == SWEEP:
        needed = sweep_chamber_volumes(fraction)
        if cv + 1e-9 < needed:
            out.append(f"step {step.get('step')}: a sweep must reach "
                       f"{target_fraction() if fraction is None else fraction:.0%} "
                       f"({needed:.2f} chamber volumes), but {washes} x {volume:g} uL is "
                       f"{cv:.2f} chamber volumes of a {chamber_volume_ul:g} uL chamber"
                       + ("; below one chamber volume no number of repeats can clear it"
                          if cv < 1 else ""))
    if kind == PARTIAL:
        note = str(step.get("note", ""))
        if "%" not in note and "fraction" not in note.lower() and "half" not in note.lower():
            out.append(f"step {step.get('step')}: a partial exchange must say in its note "
                       f"what fraction of the chamber it replaces")
    return out

"""S10 analyse: everything the CFD modules produced -> `cfd/findings.json`.

One finding per REAL problem. A finding is a disagreement between two numbers, or an
honest statement that a number we need does not exist. It is never a vibe, and it is
never a silent pass.

    from assay2chip.cfd import analyse

    bundle = analyse.Bundle(chip=chip_spec, protocol=proto, mesh=mesh, steps=[...])
    findings = analyse.analyse(bundle)
    analyse.write_findings(findings, work / "cfd" / "findings.json")

WHAT EACH FINDING CARRIES
    id class severity chip_id feature_id step metric value limit units message fix_hint
plus `untrusted`, `assumptions` and `evidence`, because a number that rests on an
assumption is not the same as one that rests on a measurement, and the report has to be
able to say which.


SEVERITY - the rules, in one place
---------------------------------
Severity is a function of (a) how far past the limit we are and (b) what it costs. Both
halves matter: 3x over a limit that only slows a wash down is not a blocker, and 1.05x
over a limit that loses the sample is not a nag.

The ladder, on `ratio = value / limit`:

    ratio >= 2.00  blocker      ratio >= 1.25  high
    ratio >= 1.00  medium       ratio >= 0.90  low      (approaching; reported, not a pass)

Then per class:

    leak            ladder, escalated one rung when the breach is at a bond or seal
                    (a delaminated lid loses the chip and the sample, not just the step).
                    `bond_strength_unknown` is a flat medium: we cannot grade what we
                    cannot look up.
    trap/dead_zone  by affected volume fraction: >=0.25 high, >=0.10 medium, >=0.02 low;
                    escalated one rung when the step actually needs that chamber
                    exchanged (a stagnant corner in a chamber nobody is washing is a
                    curiosity; the same corner during a 15x wash is carryover).
    dead_end        blocker when it isolates a port the step drives or vents through;
                    high when a region simply cannot be reached or drained.
    bubble          high for a dead-end branch with no vent at priming, ladder for a
                    pinning expansion. Capped at high: it rests on a contact angle the
                    contracts do not hold (see ASSUMPTIONS).
    shear           ladder against the ceiling FOR THAT CHAMBER'S CONTENTS, see below.
                    `shear_scope_unknown` is a flat medium.
    sediment        ladder on settling time vs transit time.
    clog            blocker when the lane is narrower than a swollen capsule (it will
                    jam, not maybe jam); otherwise the ladder on the 1.5x rule.
    weir_escape     ladder on dP across the retention feature vs escape x margin.
    exchange        ladder on the shortfall against the step's exchange target.
    handoff         blocker for a particle-size mismatch (the capsules do not arrive);
                    ladder for volume and dead-volume mismatches.
    unmodelled      Re > 10 high, Re > 1 medium. NEVER a blocker: it does not say the
                    chip fails, it says this model may not be used to decide.

Two global modifiers:

    * A finding resting on a LOCAL assumption (one this module had to invent because no
      contract holds it) is capped at `high`. We do not block a build on a guess.
    * If validate.py reports a failed benchmark, every finding is marked `untrusted` and
      its message says so. Severities are kept - an untrusted blocker still needs looking
      at - but nothing here may be quoted as a number.


WHAT A CHAMBER HOLDS - the shear scoping, which this repo has got wrong at least six times
-----------------------------------------------------------------------------------------
There are three ceilings and they are NOT interchangeable
(`contracts.shear_ceilings()`):

    adherent_cells    0.01 dyn/cm2   ADHERENT CELLS ONLY. The risk is detaching them.
    prelysis_rinse    0.3  dyn/cm2   a rinse IMMEDIATELY BEFORE a deliberate lyse.
    capsules          no shear ceiling at all - bounded by escape pressure.

So the scope is not a property of the chamber, it is a property of the PAIR
(chamber, step): the same chamber is adherent-cell-bounded on day 3 and
rinse-bounded in the step before the quench. `shear_scope()` decides from DATA:

    1. an explicit `contents` / `shear_class` / `holds` on the chamber wins;
    2. CAPSULES when the chip carries positive capsule evidence - a `capsule` section, a
       channel with `carries: capsules` or `capsule_passable`, a capsule reagent - AND
       this chamber is the retention feature holding them. Judged by escape pressure,
       never by the culture ceiling;
    2b. CAPSULES when THIS step's own reagents name capsules, whatever the chamber is.
       Rule 2 needs a retention gap, so a chamber capsules are only loaded INTO - module4's
       sublibrary wells, module5's bead chamber - used to fall through to rule 3 and get
       judged at 0.01, which produced four 990x blockers and one 1800x one. A reagent
       called `pooled_capsule_suspension` is not ambiguous the way a bare weir is;
    3. ADHERENT CELLS when the chamber is a culture block or type, or the protocol coats
       it and loads a cell suspension into it and then holds it warm;
    3a. NONE when the cells are not in there YET. Coating is evidence of adherent culture
       and also happens before seeding, so the coat step itself has nothing to detach.
       Only fires when the protocol does seed this chamber later - if it never seeds it,
       the cells may arrive pre-loaded and assuming empty would hide real blockers;
    3b. UNKNOWN on a deliberate dissociation (trypsin, Accutase, collagenase...): the
       0.01 ceiling exists to stop unwanted detachment, and detachment is the point here.
       The cells are the product, so a limit exists, but contracts carry no
       suspended-cell ceiling - so this reports unresolved rather than borrowing 0.3,
       which means something else;
    4. PRE-LYSIS RINSE when this step is a rinse/exchange on a chamber that held adherent
       cells and the next flowing step on it is a deliberate lyse;
    5. NONE once the cells in it have been deliberately lysed - there is nothing left to
       protect, and holding a quench to a culture ceiling is how you get a 12-minute
       "1-second" flood;
    5b. UNKNOWN once they have been deliberately DISSOCIATED: from then on they are in
       suspension, so the adherent ceiling cannot apply, but they are still the product.
       Same missing contract value as 3b. Without this the report contradicted itself -
       "no ceiling applies" on the dissociation step, then a 90x blocker two steps later
       on the transfer;
    6. otherwise UNKNOWN -> a `shear_scope_unknown` finding saying exactly what we
       looked at and failed to resolve. We do NOT pick the strictest ceiling (it invents
       blockers on waste lines) and we do NOT pick the loosest (it hides real ones).

A weir is NOT capsule evidence on its own: module1's trap chambers have 25 um weirs and
hold adherent cells - the weir is there to keep cell clusters in. That distinction is why
rule 2 needs the chip-level evidence as well as the retention feature.

SI internally; kPa, dyn/cm2, mm, uL/min and fractions at the JSON boundary, with the unit
in the key name.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field, fields as dataclass_fields, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts, io  # noqa: E402

# contracts.py is the ONE reader for parameters.json and blocks.json - every number here
# comes through it. materials.json has no accessor there (it is not a parameter table), so
# the bond-strength lookup below reads it directly. It looks for a key and reports its
# ABSENCE; it never substitutes a number.
MATERIALS_PATH = ROOT / "contracts" / "materials.json"

SEVERITIES = ("blocker", "high", "medium", "low")
_SEVERITY_RANK = {name: i for i, name in enumerate(SEVERITIES)}

CLASSES = (
    "leak",
    "dead_end",
    "bubble",
    "clog",
    "weir_escape",
    "handoff",
    "shear",
    "trap",
    "exchange",
    "sediment",
    "unmodelled",
)

# Ladder breakpoints on value/limit. Not contract numbers - they are this module's
# grading policy, stated once so the report can quote them.
LADDER = ((2.00, "blocker"), (1.25, "high"), (1.00, "medium"), (0.90, "low"))


# --- the Finding shape ------------------------------------------------------------

from assay2chip.cfd.model import Finding  # noqa: E402

# model.Finding is owned by the mesh module and is the shared shape, so we use it rather
# than a local copy. Two things it does that this module has to respect:
#   * the JSON key is `class`, which Python will not allow as a field name, so the
#     attribute is `cls` and `Finding.to_json()` writes `class`;
#   * it has no `assumptions` / `evidence` / `untrusted` fields, but it does have the
#     repo's open `audit` pocket, which is exactly what provenance like that is for.
# `finding_assumptions()` and friends below are the only way this module reads them back,
# so none of it depends on the field layout.

AUDIT_ASSUMPTIONS = "assumptions"
AUDIT_EVIDENCE = "evidence"
AUDIT_UNTRUSTED = "untrusted"
AUDIT_STEPS = "also_in_steps"


def make_finding(
    *,
    id: str,
    finding_class: str,
    severity: str,
    chip_id: str,
    feature_id: Any,
    step: Any,
    metric: str,
    value: Any,
    limit: Any,
    units: str,
    message: str,
    fix_hint: str = "",
    source: str = "",
    assumptions: Sequence[str] = (),
    evidence: Sequence[str] = (),
) -> Finding:
    """Build a `model.Finding`, putting our extras in its `audit` pocket."""
    audit: dict[str, Any] = {}
    if assumptions:
        audit[AUDIT_ASSUMPTIONS] = list(assumptions)
    if evidence:
        audit[AUDIT_EVIDENCE] = list(evidence)
    return Finding(
        id=id,
        cls=finding_class,
        severity=severity,
        chip_id=chip_id,
        feature_id="" if feature_id is None else str(feature_id),
        step=step,
        metric=metric,
        value=_num(value),
        limit=_num(limit),
        units=units,
        message=message,
        fix_hint=fix_hint,
        source=source,
        assumption=bool(assumptions),
        audit=audit,
    )


def finding_class_of(finding: Any) -> str:
    """The JSON's `class`, whatever the object calls it."""
    return str(_get(finding, "cls", "finding_class", "class", default=""))


def _audit(finding: Any) -> dict:
    pocket = _get(finding, "audit", default=None)
    return dict(pocket) if isinstance(pocket, Mapping) else {}


def finding_assumptions(finding: Any) -> list[str]:
    return list(_audit(finding).get(AUDIT_ASSUMPTIONS, ()))


def finding_evidence(finding: Any) -> list[str]:
    return list(_audit(finding).get(AUDIT_EVIDENCE, ()))


def finding_untrusted(finding: Any) -> bool:
    return bool(_audit(finding).get(AUDIT_UNTRUSTED, False))


def _set_audit(finding: Any, key: str, value: Any) -> None:
    pocket = _get(finding, "audit", default=None)
    if isinstance(pocket, dict):
        pocket[key] = value
    else:  # pragma: no cover - Finding always has one
        try:
            setattr(finding, "audit", {key: value})
        except Exception:
            pass


def finding_to_json(finding: Any) -> dict:
    """One finding as `findings.json` - `class`, not `cls`, and rounded for reading."""
    to_json = getattr(finding, "to_json", None)
    if callable(to_json):
        out = dict(to_json())
    else:  # a plain dict from a sibling that already emitted finding shape
        out = {k: v for k, v in dict(finding).items()}
        out.setdefault("class", finding_class_of(finding))
        out.pop("cls", None)
    out["class"] = finding_class_of(finding)
    for key in ("value", "limit"):
        if out.get(key) is not None:
            out[key] = _round(out[key])
    out.setdefault("feature_id", "")
    out.setdefault("step", None)
    out.setdefault("fix_hint", "")
    out.setdefault("units", "-")
    if finding_untrusted(finding):
        out["untrusted"] = True   # top level: a reader must not have to dig for this
    return out


# --- local assumptions: numbers no contract holds ---------------------------------

@dataclass(frozen=True)
class Assumption:
    """A number this module needs that the contracts do not hold.

    Every finding that uses one names it, so nothing rests on an invented number
    silently. TODO(contracts): these belong in contracts/parameters.json with a source.
    """

    key: str
    value: float
    units: str
    why: str


ASSUMPTIONS: dict[str, Assumption] = {
    "contact_angle_deg": Assumption(
        "contact_angle_deg",
        70.0,
        "degrees",
        "Water/COC-PMMA advancing contact angle. The contracts hold no wetting data at "
        "all, and capillary pinning cannot be computed without it. 70 deg is a mid "
        "unoxidised-thermoplastic value; a plasma-treated surface would be far lower and "
        "would not pin.",
    ),
    "surface_tension_n_per_m": Assumption(
        "surface_tension_n_per_m",
        0.072,
        "N/m",
        "Aqueous buffer/air surface tension at room temperature. Culture medium with "
        "protein is lower (~0.05); organic quench solvent lower still.",
    ),
    "expansion_pin_factor": Assumption(
        "expansion_pin_factor",
        2.0,
        "x width",
        "Area ratio at which a filling meniscus is treated as meeting a sudden "
        "expansion that it can pin on, rather than a taper it sweeps through.",
    ),
    "dead_zone_velocity_fraction": Assumption(
        "dead_zone_velocity_fraction",
        0.10,
        "fraction of chamber mean",
        "A cell below this fraction of its chamber's mean speed counts as a low-velocity "
        "zone. Hele-Shaw flow has no closed streamlines, so this - not a recirculation "
        "test - is what finds a trap. Used only when transport.py reports no zones of "
        "its own.",
    ),
    "residence_outlier_factor": Assumption(
        "residence_outlier_factor",
        3.0,
        "x chamber median",
        "Residence time above this multiple of the chamber median is a flushing outlier.",
    ),
    "dead_zone_report_floor": Assumption(
        "dead_zone_report_floor",
        0.02,
        "volume fraction",
        "Below 2% of chamber volume a slow corner is noise, not a finding.",
    ),
    "re_validity_ceiling": Assumption(
        "re_validity_ceiling",
        1.0,
        "Reynolds number",
        "Stated in assay2chip/cfd/README.md rather than in a contract: above Re 1 the "
        "Stokes/Hele-Shaw network form is outside its validity.",
    ),
    "re_high_factor": Assumption(
        "re_high_factor",
        10.0,
        "Reynolds number",
        "An order of magnitude past validity - inertia dominates and the model's numbers "
        "are not merely imprecise, they are the wrong equations.",
    ),
    "handoff_volume_tolerance": Assumption(
        "handoff_volume_tolerance",
        0.10,
        "fraction",
        "Volume mismatch across a handoff we treat as accounted for by priming.",
    ),
}


# --- densities: which end of the band, and why -----------------------------------

GRAVITY_M_S2 = 9.80665

# Particle class -> the contract key holding its excess density over the medium.
EXCESS_DENSITY_KEYS = {
    "cells": "cell.excess_density_kg_m3",
    "cell": "cell.excess_density_kg_m3",
    "capsules": "capsule.excess_density_kg_m3",
    "capsule": "capsule.excess_density_kg_m3",
    "beads": "bead.excess_density_kg_m3",
    "bead": "bead.excess_density_kg_m3",
}


@dataclass(frozen=True)
class DensityChoice:
    """One end of a contract density band, and the reason that end was taken."""

    key: str
    kg_m3: float
    end: str          # "max" | "min" | "value"
    purpose: str      # "loss" | "retention"
    why: str

    @property
    def note(self) -> str:
        return f"{self.key} {self.end} = {self.kg_m3:g} kg/m3 ({self.why})"


def excess_density(particle_class: str, purpose: str) -> DensityChoice:
    """Excess density for a settling judgement, from the right end of the band.

    Stokes settling is LINEAR in excess density, so which end of the contract's band you
    take decides the verdict, and the contract's own notes set the rule:

      purpose="loss"       checking whether particles are LOST by settling - dropping out
                           in a feed line, a chamber or a tube. Take the MAX: the densest
                           plausible particle settles fastest, so if even that one stays
                           up, none are lost.
      purpose="retention"  RELYING on settling to hold something where you want it - a
                           packed bed staying put, a particle reaching a trap. Take the
                           MIN: the lightest plausible particle settles slowest, so if
                           even that one lands in time, retention holds.

    Taking the wrong end turns a safety check into wishful thinking in both directions,
    which is why every finding built from this quotes `DensityChoice.note`.
    """
    key = EXCESS_DENSITY_KEYS.get(str(particle_class).lower())
    if key is None:
        raise contracts.ContractError(
            f"no excess-density contract for particle class {particle_class!r}: "
            f"known classes are {', '.join(sorted(set(EXCESS_DENSITY_KEYS)))}"
        )
    entry = contracts.param_entry(key)
    low, high = contracts.param_range(key)
    if purpose == "loss":
        chosen, end = (high if high is not None else entry.get("value")), "max"
        why = "max of the band: the densest plausible particle settles fastest, so this is the conservative end for a LOSS check"
    elif purpose == "retention":
        chosen, end = (low if low is not None else entry.get("value")), "min"
        why = "min of the band: the lightest plausible particle settles slowest, so this is the conservative end when RELYING on settling"
    else:
        raise ValueError(f"purpose must be 'loss' or 'retention', not {purpose!r}")
    if chosen is None:
        raise contracts.ContractError(f"contracts/parameters.json '{key}' has no usable value")
    return DensityChoice(key, float(chosen), end, purpose, why)


def stokes_settling_mm_s(excess_kg_m3: float, diameter_um: float, mu_pa_s: float) -> float:
    """Terminal settling speed, mm/s. v = dRho g d^2 / (18 mu)."""
    d_m = diameter_um / 1e6
    return (excess_kg_m3 * GRAVITY_M_S2 * d_m * d_m) / (18.0 * mu_pa_s) * 1000.0


def assumption(key: str) -> Assumption:
    if key not in ASSUMPTIONS:
        raise KeyError(f"no local assumption {key!r}: have {', '.join(sorted(ASSUMPTIONS))}")
    return ASSUMPTIONS[key]


def assumed(key: str) -> float:
    return assumption(key).value


# --- generic accessors: siblings may hand us dataclasses or plain dicts -----------

_MISSING = object()


def _get(obj: Any, *names: str, default: Any = _MISSING) -> Any:
    """First present attribute or key among `names`. Siblings serialise both ways."""
    for name in names:
        if isinstance(obj, Mapping):
            if name in obj:
                return obj[name]
        elif hasattr(obj, name):
            return getattr(obj, name)
    if default is _MISSING:
        raise KeyError(f"none of {names} on {type(obj).__name__}")
    return default


def _is_record(rec: Any) -> bool:
    """Can we read named fields off this?

    `mesh.Mesh` holds `Node`/`Edge` DATACLASSES, not dicts, and so do `couple.Handoff`
    and the particle verdicts - while the same code has to read the plain dicts that come
    back from JSON and from tests. Guarding on `isinstance(rec, Mapping)` quietly drops
    every dataclass, which once left the graph empty and the dead-end check silently
    doing nothing on every real chip. So: anything that is not a scalar is a record.
    """
    return rec is not None and not isinstance(rec, (str, bytes, int, float, bool))


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(float(value)) else None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _round(value: Any, places: int = 6) -> Any:
    n = _num(value)
    if n is None:
        return value if not isinstance(value, float) else None
    if n == 0:
        return 0.0
    magnitude = math.floor(math.log10(abs(n)))
    return round(n, max(places - 1 - magnitude, 0)) if magnitude < places else round(n, 0)


def _ratio(value: Any, limit: Any) -> float | None:
    v, l = _num(value), _num(limit)
    if v is None or l is None or l == 0:
        return None
    return abs(v) / abs(l)


def _iter_records(blob: Any, *names: str) -> list[Any]:
    """A sibling's list-of-records, whether it came as a list or a dict keyed by id."""
    if blob is None:
        return []
    if isinstance(blob, Mapping):
        for name in names:
            if name in blob:
                return _iter_records(blob[name])
        out = []
        for key, value in blob.items():
            if isinstance(value, Mapping):
                merged = dict(value)
                merged.setdefault("id", key)
                merged.setdefault("feature_id", value.get("feature_id", key))
                out.append(merged)
            else:
                out.append({"id": key, "value": value})
        return out
    if isinstance(blob, Sequence) and not isinstance(blob, (str, bytes)):
        return list(blob)
    return [blob]


def _mapping(blob: Any, *names: str) -> dict:
    if blob is None:
        return {}
    if isinstance(blob, Mapping):
        for name in names:
            if name in blob and isinstance(blob[name], Mapping):
                return dict(blob[name])
        if not names:
            return dict(blob)
        return dict(blob)
    for name in names:
        inner = getattr(blob, name, None)
        if isinstance(inner, Mapping):
            return dict(inner)
    return {}


def _vector_magnitude(value: Any) -> float | None:
    """`u_mm_s[cell]` is a vector; accept a scalar, a pair, or an {x,y} mapping."""
    if isinstance(value, Mapping):
        parts = [_num(value.get(k)) for k in ("x", "y", "z", "u", "v")]
        parts = [p for p in parts if p is not None]
        if parts:
            return math.sqrt(sum(p * p for p in parts))
        return _num(value.get("magnitude") or value.get("mag"))
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        parts = [p for p in (_num(v) for v in value) if p is not None]
        return math.sqrt(sum(p * p for p in parts)) if parts else None
    return _num(value)


# --- protocol / chip reading ------------------------------------------------------

# Words that mean the cells in this chamber are being destroyed ON PURPOSE. Matched
# against the operation and the reagent. "quench" alone is deliberately NOT here:
# module1 quenches a barcoding reaction and keeps culturing the same cells afterwards.
_LYSE_TERMS = (
    "lysis", "lyse", "lysing", "solubilis", "solubiliz", "denatur", "guanidin",
    "gitc", "gusc", "trizol", "chaotrop", "sds", "detergent", "ripa", "extract",
)
# Words that destroy the cells only if the chamber is finished with afterwards.
_TERMINAL_TERMS = ("quench", "extract", "fix", "precipitat")
_RINSE_TERMS = ("rinse", "wash", "exchange", "flush")
_CELL_REAGENT_TERMS = ("cell_suspension", "cells", "suspension", "seed", "inoculum")
_CAPSULE_TERMS = ("capsule", "bead", "hydrogel", "gelma")
_COATING_TERMS = ("coating", "matrigel", "laminin", "poly_l_lysine", "pll", "fibronectin")
# Reagents whose PURPOSE is to detach adherent cells, which makes the 0.01 "do not detach
# them" ceiling self-contradictory on that step.
_DISSOCIATION_TERMS = (
    "dissociat", "trypsin", "accutase", "tryple", "collagenase", "papain", "versene",
)
_CULTURE_OPS = ("culture", "incubate", "grow", "hold_temp", "hold")
_NO_FLOW_OPS = ("hold_temp", "hold", "incubate", "collect", "soak", "dwell", "image")


@dataclass(frozen=True)
class StepView:
    """One protocol step, normalised. Index is the key the findings quote as `step`."""

    index: int
    label: Any
    operation: str
    targets: tuple[str, ...]
    reagents: tuple[str, ...]
    volume_ul: float | None
    flow_ul_min: float | None
    duration_s: float | None
    temp_c: float | None
    washes: float | None
    feeds: tuple[str, ...]
    vents: tuple[str, ...]
    closed: tuple[str, ...]
    raw: Mapping

    @property
    def text(self) -> str:
        return " ".join([self.operation, *self.reagents, str(_get(self.raw, "note", default=""))]).lower()

    @property
    def has_flow(self) -> bool:
        """Does liquid move? A capped hold is not a dead end, it is an idle chamber."""
        if self.operation in _NO_FLOW_OPS:
            return False
        flow = self.flow_ul_min
        if flow is not None and flow <= 0:
            return False
        return bool(self.feeds) or flow is not None

    def targets_chamber(self, chamber_id: str) -> bool:
        return chamber_id in self.targets


def step_views(protocol: Any) -> list[StepView]:
    raw_steps = _iter_records(_get(protocol, "steps", default=[]) if protocol is not None else [])
    out: list[StepView] = []
    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, Mapping):
            raw = {"operation": str(raw)}
        ports = _mapping(_get(raw, "ports", default={}) or {})
        targets = _get(raw, "targets", default=None)
        if targets is None:
            single = _get(raw, "target", default=None)
            targets = [single] if single else []
        reagents = _get(raw, "reagents", default=None)
        if reagents is None:
            single = _get(raw, "reagent", default=None)
            reagents = [single] if single else []
        out.append(
            StepView(
                index=i,
                label=_get(raw, "step", default=i + 1),
                operation=str(_get(raw, "operation", default="") or "").lower(),
                targets=tuple(str(t) for t in targets if t),
                reagents=tuple(str(r) for r in reagents if r),
                volume_ul=_num(_get(raw, "volume_ul", default=None)),
                flow_ul_min=_num(_get(raw, "flow_ul_min", default=None)),
                duration_s=_num(_get(raw, "duration_s", default=None)),
                temp_c=_num(_get(raw, "temp_c", default=None)),
                washes=_num(_get(raw, "washes", default=None)),
                feeds=tuple(str(p) for p in _as_list(ports.get("feed"))),
                vents=tuple(str(p) for p in _as_list(ports.get("vent")) + _as_list(ports.get("waste"))),
                closed=tuple(str(p) for p in _as_list(ports.get("cap")) + _as_list(ports.get("closed"))),
                raw=raw,
            )
        )
    return out


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    if isinstance(value, Mapping):
        return list(value)
    if isinstance(value, Sequence):
        return list(value)
    return [value]


def _matches(text: str, terms: Iterable[str]) -> bool:
    low = text.lower()
    return any(term in low for term in terms)


@dataclass(frozen=True)
class ChamberView:
    """A chamber as the chip spec states it, with the keys this module reads."""

    id: str
    kind: str
    block: str
    volume_ul: float | None
    retention: Mapping
    explicit_contents: str | None
    note: str
    raw: Mapping


def chamber_views(chip: Any) -> dict[str, ChamberView]:
    out: dict[str, ChamberView] = {}
    for raw in _iter_records(_get(chip, "chambers", default=[]) if chip is not None else []):
        if not isinstance(raw, Mapping):
            continue
        cid = str(_get(raw, "id", default=""))
        if not cid:
            continue
        explicit = _get(raw, "contents", "shear_class", "holds", default=None)
        out[cid] = ChamberView(
            id=cid,
            kind=str(_get(raw, "type", default="") or ""),
            block=str(_get(raw, "block", default="") or ""),
            volume_ul=_num(_get(raw, "volume_ul", default=None)),
            retention=_mapping(_get(raw, "retention", default={}) or {}),
            explicit_contents=str(explicit) if explicit else None,
            note=str(_get(raw, "note", default="") or ""),
            raw=raw,
        )
    return out


def chip_id_of(chip: Any, mesh: Any = None, fallback: str = "chip") -> str:
    for source in (chip, mesh):
        if source is None:
            continue
        cid = _get(source, "chip_id", "id", default=None)
        if cid:
            return str(cid)
        meta = _mapping(_get(source, "meta", default={}) or {})
        if meta.get("chip_id"):
            return str(meta["chip_id"])
    return fallback


def is_capsule_chip(chip: Any, protocol: Any = None) -> tuple[bool, list[str]]:
    """Positive capsule evidence only. A weir alone does NOT make a capsule chip.

    module1_v1 has 25 um weirs on two chambers and carries no capsules anywhere - its
    weirs retain adherent cell clusters. Judging those chambers by escape pressure, or
    by the culture ceiling because of the weir, are both wrong for the same reason:
    nobody looked at what is actually in them.
    """
    evidence: list[str] = []
    if chip is not None:
        if _get(chip, "capsule", default=None):
            evidence.append("chip spec has a `capsule` section")
        for coll in ("channels", "ports"):
            for rec in _iter_records(_get(chip, coll, default=[]) or []):
                if not _is_record(rec):
                    continue
                if str(_get(rec, "carries", default="")).lower() in _CAPSULE_TERMS:
                    evidence.append(f"{coll[:-1]} {_get(rec, 'id', default='?')} carries capsules")
                elif _get(rec, "capsule_passable", default=False) is True:
                    evidence.append(f"{coll[:-1]} {_get(rec, 'id', default='?')} is capsule_passable")
    for view in step_views(protocol):
        if _matches(" ".join(view.reagents), _CAPSULE_TERMS):
            evidence.append(f"step {view.label} reagent {view.reagents[0]}")
    # Dedupe, keep order, keep it short enough to read in a finding.
    seen, kept = set(), []
    for item in evidence:
        if item not in seen:
            seen.add(item)
            kept.append(item)
    return bool(kept), kept[:4]


# --- what does this chamber hold, in this step -----------------------------------

@dataclass(frozen=True)
class ScopeCall:
    """The answer to "what bounds the flow in this chamber, in this step?"."""

    shear_class: str           # adherent_cells | prelysis_rinse | capsules | none | unknown
    ceiling_dyn_cm2: float | None
    pressure_ceiling_kpa: float | None
    bound_by: str
    evidence: tuple[str, ...]
    resolved: bool

    @property
    def is_unknown(self) -> bool:
        return not self.resolved


def _deliberate_lyse(view: StepView, later: Sequence[StepView], chamber_id: str) -> tuple[bool, str]:
    """Is this step destroying the cells in `chamber_id` on purpose?

    Unambiguous when the reagent or operation names a lysis chemistry. When it only
    names a quench or an extraction, it counts only if the chamber is FINISHED with
    afterwards - no later load, culture or warm hold on it. That is the difference
    between module6's -20 C organic quench (terminal, the lane is spent) and module1's
    barcode quench_buffer (the same cells are cultured for days afterwards).
    """
    blob = " ".join((view.operation, *view.reagents))
    if _matches(blob, _LYSE_TERMS):
        return True, f"step {view.label} {view.operation} with {', '.join(view.reagents) or 'no reagent'}"
    if not _matches(blob, _TERMINAL_TERMS):
        return False, ""
    for nxt in sorted((s for s in later if chamber_id in s.targets), key=lambda s: s.index):
        if _matches(" ".join(nxt.reagents), _LYSE_TERMS):
            # Unambiguous lysis chemistry downstream: from here on the chamber certainly
            # holds no live cells, so nothing past this point can argue they survived.
            break
        if nxt.operation in ("load", "seed") or _matches(" ".join(nxt.reagents), _CELL_REAGENT_TERMS):
            return False, ""
        if nxt.operation in ("culture", "incubate", "grow"):
            return False, ""
        # A fed MEDIUM is the signal that cells are still being kept, not a warm hold on
        # its own: module6 holds a spent lane at 37 C to RESOLUBILISE precipitated
        # protein, and reading that as culture is what made its quench look survivable.
        if _matches(" ".join(nxt.reagents), ("medium", "media")):
            return False, ""
    return True, (
        f"step {view.label} is a terminal {view.operation}"
        f"{' with ' + view.reagents[0] if view.reagents else ''}: afterwards {chamber_id} is "
        f"never loaded, cultured or fed medium again"
    )


def _held_adherent_cells(chamber: ChamberView, steps: Sequence[StepView]) -> tuple[bool, list[str]]:
    evidence: list[str] = []
    if chamber.block == "culture_chamber":
        evidence.append("block culture_chamber")
    if chamber.kind in ("culture", "organoid"):
        evidence.append(f"chamber type {chamber.kind}")
    if "adherent" in chamber.note.lower():
        evidence.append("chip spec note says ADHERENT")
    loaded_cells = coated = warm_hold = False
    for view in steps:
        if chamber.id not in view.targets:
            continue
        reagents = " ".join(view.reagents)
        if view.operation in ("load", "seed") and (
            not view.reagents or _matches(reagents, _CELL_REAGENT_TERMS)
        ):
            loaded_cells = True
        if _matches(reagents, _COATING_TERMS):
            coated = True
        if view.operation in _CULTURE_OPS and (view.temp_c is not None and view.temp_c >= 30):
            warm_hold = True
    if coated:
        evidence.append("protocol coats it before loading - coating is for attachment")
    if loaded_cells and warm_hold:
        evidence.append("protocol loads a cell suspension then holds it at >=30 C")
    elif loaded_cells:
        evidence.append("protocol loads a cell suspension into it")
    return bool(evidence), evidence


def shear_scope(
    chamber: ChamberView,
    view: StepView,
    steps: Sequence[StepView],
    *,
    capsule_chip: bool,
    capsule_evidence: Sequence[str] = (),
) -> ScopeCall:
    """Which of the three ceilings governs (chamber, step). See the module docstring.

    Returns `shear_class == "unknown"` rather than guessing. Picking the strictest
    ceiling invents blockers on waste lines; picking the loosest hides real ones.
    """
    ceilings = contracts.shear_ceilings()

    def call(name: str, evidence: Sequence[str], resolved: bool = True) -> ScopeCall:
        ceiling = ceilings[name] if name in ceilings else ceilings["none"]
        return ScopeCall(
            name,
            ceiling.ceiling_dyn_cm2,
            ceiling.pressure_ceiling_kpa,
            ceiling.bound_by,
            tuple(evidence),
            resolved,
        )

    # 1. the chip spec said so.
    if chamber.explicit_contents:
        stated = chamber.explicit_contents.lower()
        for name in ("capsules", "prelysis_rinse", "adherent_cells", "none"):
            if name in stated or stated in name:
                return call(name, [f"chamber states contents/shear_class {chamber.explicit_contents!r}"])
        if _matches(stated, _CAPSULE_TERMS):
            return call("capsules", [f"chamber states contents {chamber.explicit_contents!r}"])

    retention = chamber.retention or {}
    gap_um = _num(retention.get("opening_um") or retention.get("gap_um"))

    # 2. capsules: needs chip-level capsule evidence AND a retention feature here.
    if capsule_chip and gap_um is not None:
        try:
            diameter = contracts.capsule_diameter_um()
            low = diameter * float(contracts.param("trap.gap_factor_low"))
            high = diameter * float(contracts.param("trap.gap_factor_high"))
            in_band = low <= gap_um < high
        except contracts.ContractError:
            in_band = True
        if in_band:
            return call(
                "capsules",
                [
                    f"{retention.get('type', 'retention')} gap {gap_um:g} um retains a "
                    f"{contracts.capsule_diameter_um():g} um capsule",
                    *list(capsule_evidence)[:2],
                    "bounded by escape pressure, NOT the culture shear ceiling",
                ],
            )

    step_text = " ".join((view.operation, *view.reagents)).lower()

    # 2b. capsules named in THIS step's reagents. Rule 2 above needs a retention gap, so a
    # chamber capsules are merely LOADED into - module4's sublibrary wells, module5's bead
    # chamber - fell straight through to the adherent-cell ceiling and reported 10-18
    # dyn/cm2 against 0.01 as blockers. A reagent called `pooled_capsule_suspension` is not
    # ambiguous the way a bare 25 um weir is, so this needs no chip-level corroboration.
    if _matches(step_text, _CAPSULE_TERMS):
        named = [r for r in view.reagents if _matches(r.lower(), _CAPSULE_TERMS)]
        return call(
            "capsules",
            [
                f"this step puts {', '.join(named) or 'a capsule reagent'} into {chamber.id}",
                "what is in it during this step is capsules, not adherent cells",
                "bounded by escape pressure, NOT the culture shear ceiling",
            ],
        )

    # 2c. capsules put in here by an EARLIER step are still in here. module5's bead_cx
    # takes capsules at step 1 and a dilution buffer at step 5; without this, step 5 has no
    # capsule word in its reagents and falls through to the 0.01 culture ceiling on a
    # chamber that demonstrably holds capsules. Mirrors rule 5's backward look.
    for past in steps:
        if past.index >= view.index or chamber.id not in past.targets:
            continue
        if _matches(" ".join((past.operation, *past.reagents)).lower(), _CAPSULE_TERMS):
            return call(
                "capsules",
                [
                    f"{past.label} (step {past.index}) put "
                    f"{', '.join(past.reagents) or 'capsules'} into {chamber.id}",
                    "nothing since has emptied it, so it still holds capsules",
                    "bounded by escape pressure, NOT the culture shear ceiling",
                ],
            )

    adherent, adherent_evidence = _held_adherent_cells(chamber, steps)

    if adherent:
        later = [s for s in steps if s.index > view.index]
        # 3a-i. a COATING step. You coat a surface before cells go onto it - coating under
        # adherent cells is not a thing - so the coating reagent itself says the cells are
        # not on yet, even when the protocol models no seeding step at all (module1's does
        # not: its reagents are media, buffers, coat, dissociate and barcodes). Stated as an
        # inference in the evidence so it can be audited.
        if _matches(step_text, _COATING_TERMS):
            return call(
                "none",
                [
                    f"this step coats {chamber.id}: "
                    f"{', '.join(view.reagents) or view.operation}",
                    "a surface is coated BEFORE cells are seeded onto it, so there are no "
                    "adherent cells here yet and nothing to detach",
                    "inferred from the coating reagent: this protocol models no seeding step",
                ],
            )
        # 3a. the cells are not in there YET. Rule 3 reads a coating step as evidence of
        # adherent culture and is right to - but coating happens BEFORE seeding, so there
        # is nothing in the chamber to detach. Guarded: this only fires when the protocol
        # really does seed this chamber later. If it never seeds it, the cells may arrive
        # already loaded and assuming an empty chamber would hide real blockers.
        seeds = [
            s for s in steps
            if chamber.id in s.targets
            and _matches(" ".join((s.operation, *s.reagents)).lower(), _CELL_REAGENT_TERMS)
        ]
        if seeds and all(s.index > view.index for s in seeds):
            first = min(seeds, key=lambda s: s.index)
            return call(
                "none",
                [
                    f"{chamber.id} is not seeded until {first.label} (step {first.index})",
                    f"this is step {view.index}: no cells in it yet, so none to detach",
                    *adherent_evidence[:1],
                ],
            )
        # 5. already lysed? nothing left to protect.
        for past in steps:
            if past.index >= view.index or chamber.id not in past.targets:
                continue
            lysed, why = _deliberate_lyse(past, [s for s in steps if s.index > past.index], chamber.id)
            if lysed:
                return call(
                    "none",
                    [f"cells in {chamber.id} were deliberately lysed at {why}", "nothing shear-sensitive left"],
                )
        # 5b. already DISSOCIATED? Then nothing in here is adherent any more - the cells
        # are in suspension. Without this, cell_prep's `transfer` two steps after its
        # dissociation was judged at 0.01 and reported a 90x blocker, while the
        # dissociation step itself said "no ceiling applies" - the report contradicted
        # itself. Not "none" (the cells are the product), not 0.01 (nothing is stuck down),
        # and the contract has no suspended-cell number, so unresolved - as in rule 3b.
        for past in steps:
            if past.index >= view.index or chamber.id not in past.targets:
                continue
            if _matches(" ".join((past.operation, *past.reagents)).lower(), _DISSOCIATION_TERMS):
                return ScopeCall(
                    "unknown", None, None, "undetermined",
                    (
                        f"step {past.index} dissociated {chamber.id}: "
                        f"{', '.join(past.reagents) or past.operation}",
                        "from there on the cells here are in SUSPENSION, so "
                        "shear.culture_ceiling_dyn_cm2 (0.01, adherent only) cannot apply",
                        "contracts hold no suspended-cell ceiling and picking one would "
                        "invent it",
                        "wanted in contracts/parameters.json: shear.suspension_ceiling_dyn_cm2",
                    ),
                    False,
                )
        # is THIS step the lyse?
        lysed_now, why_now = _deliberate_lyse(view, later, chamber.id)
        if lysed_now:
            return call("none", [f"this step IS the deliberate lyse: {why_now}"])
        # 4. a rinse immediately before the lyse.
        if _matches(" ".join((view.operation, *view.reagents)), _RINSE_TERMS):
            nxt = next((s for s in later if chamber.id in s.targets and s.has_flow), None)
            if nxt is not None:
                lysed_next, why_next = _deliberate_lyse(nxt, [s for s in later if s.index > nxt.index], chamber.id)
                # A deliberate DISSOCIATION counts the same way a lyse does: in both cases
                # the cells are about to be released on purpose, so detachment during the
                # rinse before it is no longer the risk the 0.01 ceiling guards against.
                nxt_text = " ".join((nxt.operation, *nxt.reagents)).lower()
                dissociated_next = _matches(nxt_text, _DISSOCIATION_TERMS)
                if lysed_next or dissociated_next:
                    why_next = why_next if lysed_next else (
                        f"deliberate dissociation: {', '.join(nxt.reagents) or nxt.operation}")
                    return call(
                        "prelysis_rinse",
                        [
                            f"this step is a rinse on {chamber.id}; the next flowing step on it is a "
                            f"deliberate {'lyse' if lysed_next else 'dissociation'} ({why_next})",
                            "detachment no longer matters, so the 0.01 culture ceiling does not apply",
                            *adherent_evidence[:1],
                        ],
                    )
        # 3b. a deliberate dissociation. Detaching the cells IS the goal, so holding this
        # step to the 0.01 "do not detach them" ceiling contradicts itself - the same
        # reasoning as rule 5's lyse. But the cells here are the PRODUCT, not waste, so a
        # limit does exist; the contracts just do not carry one for cells in suspension.
        # Picking 0.3 would be borrowing a ceiling that means something else, so report it
        # unresolved (a flat medium) and name the number we want.
        if _matches(step_text, _DISSOCIATION_TERMS):
            return ScopeCall(
                "unknown", None, None, "undetermined",
                (
                    f"this step dissociates {chamber.id}: "
                    f"{', '.join(view.reagents) or view.operation}",
                    "detachment is the intent, so shear.culture_ceiling_dyn_cm2 (0.01) "
                    "cannot be the limit on this step",
                    "the cells are the product, so a limit exists - but contracts hold no "
                    "suspended-cell ceiling and choosing one here would invent it",
                    "wanted in contracts/parameters.json: shear.suspension_ceiling_dyn_cm2",
                ),
                False,
            )
        # 3. plain adherent culture.
        return call("adherent_cells", adherent_evidence)

    # Nothing shear-sensitive we can name: a pool, a junction, a waste line.
    if chamber.kind in ("pool", "waste", "collection", "filter") or chamber.block in (
        "cross_junction", "t_junction", "y_junction", "network_node", "flow_manifold",
    ):
        return call("none", [f"{chamber.id} is a {chamber.kind or chamber.block}: no cells, no capsules"])

    return ScopeCall(
        "unknown",
        None,
        None,
        "undetermined",
        (
            f"chamber type {chamber.kind or '(none)'}, block {chamber.block or '(none)'}",
            f"retention {json.dumps(dict(retention)) if retention else '(none)'}",
            f"step {view.label} {view.operation} with {', '.join(view.reagents) or 'no reagent'}",
            "no capsule evidence on this chip" if not capsule_chip else "capsule chip, but no retention here",
        ),
        False,
    )


# --- materials: the hot-embossed bond -------------------------------------------

_BOND_KEYS = (
    "bond_strength_kpa", "bond_strength_mpa", "bond_strength_pa", "bond_strength",
    "hot_embossed_bond_strength_kpa", "bond_burst_pressure_kpa", "burst_pressure_kpa",
    "seal_strength_kpa", "lap_shear_strength_mpa", "bond_pressure_kpa",
)
_KPA_PER = {"kpa": 1.0, "mpa": 1000.0, "pa": 0.001, "bar": 100.0, "psi": 6.894757}


def load_materials(path: Path | None = None) -> dict:
    data = json.loads((path or MATERIALS_PATH).read_text())
    mats = data.get("materials", data)
    return mats if isinstance(mats, Mapping) else {}


def bond_strength_kpa(chip: Any, materials: Mapping | None = None) -> tuple[float | None, str, str]:
    """What a hot-embossed bond on this chip's material holds, in kPa.

    Returns (kPa or None, material id, note). The contracts hold temperatures, optical
    and chemical properties for every material and NO bond strength for any of them, so
    the honest answer today is None - which is itself a medium finding, not a pass.
    """
    mats = materials if materials is not None else load_materials()
    wanted: list[str] = []
    for layer in _iter_records(_get(chip, "layers", default=[]) if chip is not None else []):
        if _is_record(layer):
            mat = _get(layer, "material", default=None)
            if mat:
                wanted.append(str(mat).lower())
        elif isinstance(layer, str):
            wanted.append(layer.lower())
    matched = ""
    for name in wanted:
        for mid, entry in mats.items():
            aliases = {str(mid).lower()} | {str(a).lower() for a in (entry or {}).get("aliases", [])}
            if name in aliases:
                matched = str(mid)
                break
        if matched:
            break
    candidates = [mats.get(matched, {})] if matched else list(mats.values())
    for entry in candidates:
        if not isinstance(entry, Mapping):
            continue
        for key in _BOND_KEYS:
            raw = _num(entry.get(key))
            if raw is None:
                continue
            unit = key.rsplit("_", 1)[-1].lower()
            return raw * _KPA_PER.get(unit, 1.0), matched or "(any material)", f"{key} from materials.json"
    return None, matched or "(unmatched)", "no bond-strength key on any material in contracts/materials.json"


# --- the inputs ------------------------------------------------------------------

@dataclass
class StepBundle:
    """One protocol step's solved state, as the sibling modules hand it over.

    Everything but `view` may be None: analyse runs on whatever exists and simply
    produces no findings of the classes it has no input for.
    """

    view: StepView
    field: Any = None            # solve.py  Field
    scenario: Any = None         # scenario.py Scenario
    transport: Any = None        # transport.py
    particles: Any = None        # particles.py


@dataclass
class Bundle:
    """Everything analyse reads for ONE chip, plus the pipeline-level inputs."""

    chip: Any = None
    protocol: Any = None
    mesh: Any = None
    steps: list[StepBundle] = field(default_factory=list)
    couple: Any = None           # couple.py pipeline field (handoffs)
    validation: Any = None       # validate.py benchmark report
    chip_id: str | None = None
    materials: Mapping | None = None
    _views: list[StepView] | None = field(default=None, repr=False, compare=False)
    _aligned: dict[int, StepView] = field(default_factory=dict, repr=False, compare=False)

    def resolved_chip_id(self) -> str:
        return self.chip_id or chip_id_of(self.chip, self.mesh)

    def all_step_views(self) -> list[StepView]:
        """EVERY protocol step, not just the solved ones.

        Scoping needs the whole protocol: deciding that a rinse is a pre-lysis rinse
        means looking ahead to the lyse, and the lyse may well be a step nobody solved a
        field for (a capped soak solves to nothing). Judging a rinse by the culture
        ceiling because the quench after it was not in the bundle is exactly the
        mis-scoping this module exists to prevent.
        """
        if self._views is None:
            views: list[StepView] = []
            if self.protocol is not None:
                views = step_views(self.protocol)
            self._views = views or [sb.view for sb in self.steps]
        return self._views

    def aligned(self, view: StepView) -> StepView:
        """`view` as it appears in the full protocol list.

        `shear_scope` walks the protocol by `index`, so a solved step has to be the SAME
        object the full list holds, or "the next step on this chamber" means nothing.
        Matched on the step label first, because a bundle may hold a subset of steps.
        """
        cached = self._aligned.get(id(view))
        if cached is not None:
            return cached
        resolved = self._align(view)
        self._aligned[id(view)] = resolved
        return resolved

    def _align(self, view: StepView) -> StepView:
        full = self.all_step_views()
        for candidate in full:
            if candidate is view:
                return candidate
        if view.label is not None:
            matches = [c for c in full if c.label == view.label]
            if len(matches) == 1:
                return matches[0]
        if 0 <= view.index < len(full):
            candidate = full[view.index]
            if candidate.operation == view.operation and candidate.targets == view.targets:
                return candidate
        return view


# --- severity -------------------------------------------------------------------

def severity_for_ratio(ratio: float | None, *, floor: str = "low") -> str:
    """The ladder. `floor` is the mildest rung this call may return."""
    if ratio is None:
        return floor
    chosen = None
    for threshold, name in LADDER:
        if ratio >= threshold:
            chosen = name
            break
    if chosen is None:
        return floor
    return chosen if _SEVERITY_RANK[chosen] <= _SEVERITY_RANK[floor] else floor


def escalate(severity: str, rungs: int = 1) -> str:
    return SEVERITIES[max(0, _SEVERITY_RANK[severity] - rungs)]


def cap(severity: str, ceiling: str) -> str:
    return severity if _SEVERITY_RANK[severity] >= _SEVERITY_RANK[ceiling] else ceiling


def severity_for_fraction(fraction: float | None, *, needed: bool) -> str:
    """Trap / dead-zone severity: how much of the chamber, and does the step need it.

    A stagnant tenth of a chamber nobody is washing is a note. The same tenth during a
    15-volume pre-RT wash is carryover into the next reagent.
    """
    frac = _num(fraction) or 0.0
    if frac >= 0.25:
        base = "high"
    elif frac >= 0.10:
        base = "medium"
    else:
        base = "low"
    return escalate(base) if needed else base


# --- finding construction and bookkeeping ---------------------------------------

def _finding_id(chip_id: str, cls: str, feature_id: Any, step: Any, metric: str) -> str:
    """Stable, readable, deterministic - and it does not shift when a sibling appears."""
    parts = [str(p) for p in (chip_id, cls, feature_id if feature_id else "-", step if step is not None else "-")]
    slug = "-".join(re.sub(r"[^A-Za-z0-9]+", "_", p).strip("_") or "_" for p in parts)
    digest = hashlib.sha1("|".join(parts + [metric]).encode()).hexdigest()[:6]
    return f"{slug}-{digest}"


class _Collector:
    """Accumulates findings for one chip, with the local-assumption cap applied."""

    def __init__(self, chip_id: str) -> None:
        self.chip_id = chip_id
        self.items: list[Any] = []

    def add(
        self,
        cls: str,
        severity: str,
        *,
        metric: str,
        message: str,
        feature_id: Any = None,
        step: Any = None,
        value: Any = None,
        limit: Any = None,
        units: str = "-",
        fix_hint: str = "",
        source: str = "",
        assumptions: Sequence[str] = (),
        evidence: Sequence[str] = (),
    ) -> Any:
        if cls not in CLASSES:
            raise ValueError(f"unknown finding class {cls!r}: {', '.join(CLASSES)}")
        if severity not in _SEVERITY_RANK:
            raise ValueError(f"unknown severity {severity!r}: {', '.join(SEVERITIES)}")
        local = [a for a in assumptions if a in ASSUMPTIONS]
        if local:
            # Never block a build on a number we invented.
            severity = cap(severity, "high")
        notes = []
        for key in assumptions:
            if key in ASSUMPTIONS:
                a = ASSUMPTIONS[key]
                notes.append(f"local assumption {a.key}={a.value:g} {a.units} ({a.why.split('.')[0]})")
            else:
                notes.append(f"contract assumption {key}")
        finding = make_finding(
            id=_finding_id(self.chip_id, cls, feature_id, step, metric),
            finding_class=cls,
            severity=severity,
            chip_id=self.chip_id,
            feature_id=feature_id,
            step=step,
            metric=metric,
            value=value,
            limit=limit,
            units=units,
            message=message if not notes else f"{message} [{'; '.join(notes)}]",
            fix_hint=fix_hint,
            source=source,
            assumptions=tuple(assumptions),
            evidence=tuple(evidence),
        )
        self.items.append(finding)
        return finding


def _contract_assumptions(*keys: str) -> list[str]:
    """Contract keys that the contract itself flags as an assumption, not a measurement."""
    out = []
    for key in keys:
        try:
            if contracts.is_assumption(key):
                out.append(key)
        except contracts.ContractError:
            continue
    return out


# --- LEAK -------------------------------------------------------------------------

def _peak_pressure_kpa(field: Any) -> tuple[float | None, str | None, str]:
    """Worst gauge pressure a seal sees, and where.

    Prefer a stated atmospheric reference; otherwise take the lowest nodal pressure as
    the open end, which is what the solver's outlet boundary is.
    """
    pressures = _mapping(_get(field, "p_pa", "pressure_pa", default={}) or {})
    numeric = {k: _num(v) for k, v in pressures.items()}
    numeric = {k: v for k, v in numeric.items() if v is not None}
    if not numeric:
        return None, None, "field has no p_pa"
    node, peak = max(numeric.items(), key=lambda kv: kv[1])
    reference = min(numeric.values())
    how = "gauge against the lowest nodal pressure (the open/atmospheric end)"
    if abs(reference) < 1e-9:
        how = "gauge against the solver's 0 Pa outlet"
    return (peak - reference) / 1000.0, node, how


def check_leak(bundle: Bundle, collector: _Collector) -> None:
    chip = bundle.chip
    capsule_chip, capsule_evidence = is_capsule_chip(chip, bundle.protocol)

    worst: tuple[float, StepBundle, str | None, str] | None = None
    for sb in bundle.steps:
        if sb.field is None:
            continue
        peak, node, how = _peak_pressure_kpa(sb.field)
        if peak is None:
            continue
        if worst is None or peak > worst[0]:
            worst = (peak, sb, node, how)
    if worst is None:
        return
    peak_kpa, sb, node, how = worst
    view = sb.view

    # a) capsule chips: escape pressure x margin.
    if capsule_chip:
        try:
            limit = contracts.max_working_pressure_kpa()
            escape = contracts.escape_pressure_kpa()
            margin = contracts.pressure_margin()
        except contracts.ContractError as exc:
            collector.add(
                "leak", "medium",
                metric="escape_pressure_contract",
                message=f"cannot check capsule escape pressure: {exc}",
                units="-",
                fix_hint="add the key to contracts/parameters.json with its units and source",
            )
        else:
            ratio = _ratio(peak_kpa, limit)
            if ratio is not None and ratio >= LADDER[-1][0]:
                collector.add(
                    "leak",
                    escalate(severity_for_ratio(ratio)) if ratio >= 1.0 else severity_for_ratio(ratio),
                    metric="peak_pressure_vs_escape_margin",
                    message=(
                        f"peak pressure {peak_kpa:.3g} kPa at {node} in step {view.label} "
                        f"({view.operation}) against the safe working limit {limit:.3g} kPa "
                        f"= {escape:g} kPa escape x {margin:g} margin. Capsules squeeze through "
                        f"the retention gap above this and the bed is lost, not just the step. "
                        f"Pressure read as {how}."
                    ),
                    feature_id=node,
                    step=view.label,
                    value=peak_kpa,
                    limit=limit,
                    units="kPa",
                    fix_hint=(
                        f"drop the step flow below {limit / max(_ratio(peak_kpa, 1.0) or 1.0, 1e-9):.3g} "
                        "uL/min, or widen the feed line / shorten the path to cut resistance"
                    ),
                    assumptions=_contract_assumptions("capsule.escape_pressure_kpa", "control.pressure_margin"),
                    evidence=tuple(capsule_evidence),
                )

    # b) every chip: what the hot-embossed 0.3 mm bond holds.
    bond_kpa, material, bond_note = bond_strength_kpa(chip, bundle.materials)
    if bond_kpa is None:
        collector.add(
            "leak", "medium",
            metric="bond_strength_known",
            message=(
                f"bond strength unknown: {bond_note}. The chip is one hot-embossed "
                f"{material} lid over a 0.3 mm channel layer and the peak pressure in the "
                f"protocol is {peak_kpa:.3g} kPa (step {view.label}, {view.operation}, at "
                f"{node}), so whether the seal holds is UNDECIDED - not passed. "
                f"contracts/materials.json carries working temperatures, optical and "
                f"chemical properties for every material and no bond or burst strength for "
                f"any of them."
            ),
            feature_id=node,
            step=view.label,
            value=peak_kpa,
            limit=None,
            units="kPa",
            fix_hint=(
                "add bond_strength_kpa (or burst_pressure_kpa) per material to "
                "contracts/materials.json from a burst test on the real emboss-and-seal "
                "process, then re-run; until then treat the peak pressure as unbounded risk"
            ),
        )
    else:
        ratio = _ratio(peak_kpa, bond_kpa)
        if ratio is not None and ratio >= LADDER[-1][0]:
            collector.add(
                "leak", escalate(severity_for_ratio(ratio)),
                metric="peak_pressure_vs_bond_strength",
                message=(
                    f"peak pressure {peak_kpa:.3g} kPa at {node} in step {view.label} against "
                    f"the {material} hot-embossed bond at {bond_kpa:.3g} kPa ({bond_note}). "
                    f"A delaminated lid loses the chip and the sample."
                ),
                feature_id=node,
                step=view.label,
                value=peak_kpa,
                limit=bond_kpa,
                units="kPa",
                fix_hint="cut the drive pressure, or widen/shorten the highest-resistance edge upstream",
            )


# --- TRAP / DEAD ZONE and EXCHANGE ------------------------------------------------

def _node_features(mesh: Any) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for node in _iter_records(_get(mesh, "nodes", default=[]) if mesh is not None else []):
        nid = _get(node, "id", default=None) if _is_record(node) else None
        if nid:
            feature = _get(node, "feature_id", default=None)
            out[str(nid)] = str(feature) if feature else None
    return out


def _transport_chambers(transport: Any) -> dict[str, dict]:
    """transport.analyse() returns `{chambers: {cid: {dead_zones: {...}, exchange: {...}}}}`."""
    if transport is None:
        return {}
    chambers = _get(transport, "chambers", default=None)
    if isinstance(chambers, Mapping):
        return {str(k): dict(v) for k, v in chambers.items() if isinstance(v, Mapping)}
    return {}


def _dead_zones_from_transport(transport: Any) -> list[dict]:
    """Dead-zone records, from transport.analyse() output or a bare list of DeadZones.

    transport.py reports several fractions and they mean different things:
    `low_speed_volume_fraction`, `long_age_volume_fraction`, `either_volume_fraction`
    (slow OR old) and `both_volume_fraction`. `either` is the right one here - a cell
    that is slow and a cell that is old both fail to be exchanged, and the README asks
    for a low-velocity OR long-residence-time test.
    """
    if transport is None:
        return []
    out: list[dict] = []
    for cid, entry in sorted(_transport_chambers(transport).items()):
        zone = entry.get("dead_zones")
        if isinstance(zone, Mapping):
            rec = dict(zone)
            rec.setdefault("chamber_id", cid)
            out.append(rec)
    if out:
        return out
    zones = _get(transport, "dead_zones", "low_velocity_zones", "traps", default=None)
    for rec in _iter_records(zones):
        if isinstance(rec, Mapping):
            out.append(dict(rec))
        elif hasattr(rec, "chamber_id"):
            as_json = getattr(rec, "to_json", None)
            out.append(dict(as_json()) if callable(as_json) else {
                "chamber_id": rec.chamber_id,
                "either_volume_fraction": getattr(rec, "either_volume_fraction", None),
            })
    return out


def _zone_fraction(zone: Mapping) -> float | None:
    for key in ("either_volume_fraction", "volume_fraction", "affected_fraction",
                "dead_volume_fraction", "low_speed_volume_fraction",
                "long_age_volume_fraction"):
        value = _num(zone.get(key))
        if value is not None:
            return value
    cells = zone.get("cells")
    if isinstance(cells, (list, tuple)) and cells:
        total = _num(zone.get("chamber_cells") or zone.get("cell_count"))
        if total:
            return len(cells) / total
    return _num(zone.get("cell_fraction"))


def _exchange_from_transport(transport: Any) -> dict[str, dict]:
    """`{chamber_id: {reached, target, shortfall}}` from transport.check_exchange output."""
    out: dict[str, dict] = {}
    for cid, entry in _transport_chambers(transport).items():
        check = entry.get("exchange")
        if not isinstance(check, Mapping):
            continue
        washout = check.get("washout") if isinstance(check.get("washout"), Mapping) else {}
        intent = check.get("intent") if isinstance(check.get("intent"), Mapping) else {}
        out[cid] = {
            "reached": _num(washout.get("fraction_reached")),
            "target": _num(washout.get("target_fraction") or intent.get("target_fraction")),
            "shortfall": _num(check.get("shortfall_fraction")),
            "extra_chamber_volumes_needed": _num(check.get("extra_chamber_volumes_needed")),
            "target_source": str(intent.get("target_source") or ""),
            "kind": str(intent.get("kind") or ""),
        }
    if out:
        return out
    for cid, raw in _mapping(
        _get(transport, "exchange_fraction", "exchange_reached", default={}) or {}
    ).items():
        reached = _num(raw if not isinstance(raw, Mapping) else raw.get("fraction"))
        if reached is not None:
            out[str(cid)] = {"reached": reached, "target": None, "shortfall": None,
                             "target_source": "", "kind": ""}
    return out


def _dead_zones_from_field(mesh: Any, field: Any) -> list[dict]:
    """Fallback: low-velocity cells grouped per chamber, from the velocity field alone.

    Hele-Shaw has no closed streamlines, so this is a velocity-magnitude test and never
    a recirculation test. Uses a flagged local threshold.
    """
    speeds = _mapping(_get(field, "u_mm_s", default={}) or {})
    if not speeds:
        return []
    feature_of = _node_features(mesh)
    per_chamber: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for cell, raw in speeds.items():
        mag = _vector_magnitude(raw)
        if mag is None:
            continue
        chamber = feature_of.get(str(cell)) or "(unassigned)"
        per_chamber[chamber].append((str(cell), mag))
    threshold = assumed("dead_zone_velocity_fraction")
    floor = assumed("dead_zone_report_floor")
    out = []
    for chamber, cells in per_chamber.items():
        if len(cells) < 2:
            continue
        mean = sum(m for _, m in cells) / len(cells)
        if mean <= 0:
            continue
        slow = [(c, m) for c, m in cells if m < threshold * mean]
        if not slow:
            continue
        fraction = len(slow) / len(cells)
        if fraction < floor:
            continue
        out.append(
            {
                "chamber_id": chamber,
                "volume_fraction": fraction,
                "cells": [c for c, _ in sorted(slow)],
                "mean_velocity_mm_s": sum(m for _, m in slow) / len(slow),
                "chamber_mean_mm_s": mean,
                "_from_field": True,
            }
        )
    return sorted(out, key=lambda z: (-z["volume_fraction"], z["chamber_id"]))


def _step_needs_exchange(view: StepView, chamber_id: str) -> bool:
    if chamber_id not in view.targets:
        return False
    return view.operation in ("exchange", "wash", "rinse", "flush", "release", "backflush") or bool(
        view.washes
    )


def check_traps(bundle: Bundle, collector: _Collector, dead_regions: Mapping[Any, set[str]]) -> None:
    for sb in bundle.steps:
        view = sb.view
        if not view.has_flow:
            continue  # a capped hold has no flow; a stagnant chamber is the point of it.
        zones = _dead_zones_from_transport(sb.transport)
        from_field = False
        if not zones and sb.field is not None:
            zones = _dead_zones_from_field(bundle.mesh, sb.field)
            from_field = True
        dead_here = dead_regions.get(view.label, set())
        for zone in zones:
            chamber = str(zone.get("chamber_id") or zone.get("feature_id") or "(unknown)")
            if chamber in dead_here:
                continue  # root cause is the dead end, already reported.
            fraction = _zone_fraction(zone)
            if fraction is None or fraction < assumed("dead_zone_report_floor"):
                continue
            needed = _step_needs_exchange(view, chamber)
            severity = severity_for_fraction(fraction, needed=needed)
            slow = _num(zone.get("mean_speed_mm_s") or zone.get("mean_velocity_mm_s"))
            mean = _num(zone.get("speed_threshold_mm_s") or zone.get("chamber_mean_mm_s"))
            residence = _num(zone.get("max_age_s") or zone.get("residence_s")
                             or zone.get("max_residence_s"))
            clearance = _num(zone.get("diffusive_clearance_max_s"))
            detail = []
            if slow is not None and mean:
                detail.append(f"{slow:.3g} mm/s against a {mean:.3g} mm/s threshold")
            if residence is not None:
                detail.append(f"oldest liquid {residence:.3g} s")
            if clearance is not None:
                detail.append(f"diffusive clearance alone would take {clearance:.3g} s")
            collector.add(
                "trap", severity,
                metric="dead_zone_volume_fraction",
                message=(
                    f"{fraction * 100:.3g}% of {chamber} is a low-velocity zone in step "
                    f"{view.label} ({view.operation})"
                    + (f": {', '.join(detail)}" if detail else "")
                    + (
                        ". This step has to exchange that chamber, so the slow fraction "
                        "carries the old reagent into the next one."
                        if needed
                        else ". The step does not exchange this chamber, so it is a note, not carryover."
                    )
                    + " Found by velocity and residence time: Hele-Shaw flow has no closed "
                    "streamlines, so a recirculation test would never fire here."
                ),
                feature_id=chamber,
                step=view.label,
                value=fraction,
                limit=assumed("dead_zone_report_floor"),
                units="fraction of chamber volume",
                fix_hint=(
                    "feed the chamber from a distributor rather than one point, add a rib to "
                    "steer flow into the slow corner, or split the chamber"
                ),
                assumptions=(
                    ["dead_zone_velocity_fraction", "dead_zone_report_floor"] if from_field else []
                ),
            )

        # EXCHANGE: did the sweep actually reach its target?
        for chamber, check in sorted(_exchange_from_transport(sb.transport).items()):
            reached = check.get("reached")
            if reached is None or not _step_needs_exchange(view, str(chamber)):
                continue
            target = check.get("target")
            source = check.get("target_source") or "contracts/parameters.json"
            if target is None:
                # transport.py did not state one (the step may have no exchange_kind), so
                # fall back to the contract's default goal for an exchange step.
                try:
                    target = float(contracts.param("exchange.target_fraction"))
                    source = "contracts/parameters.json exchange.target_fraction"
                except contracts.ContractError as exc:
                    collector.add(
                        "exchange", "medium",
                        metric="exchange_target_contract",
                        message=f"cannot judge exchange of {chamber}: {exc}",
                        feature_id=str(chamber), step=view.label, units="-",
                    )
                    continue
            if reached >= target:
                continue
            shortfall = (1.0 - reached) / max(1.0 - target, 1e-9)
            extra = check.get("extra_chamber_volumes_needed")
            collector.add(
                "exchange", severity_for_ratio(shortfall, floor="low"),
                metric="exchange_fraction_reached",
                message=(
                    f"step {view.label} ({view.operation}"
                    f"{', ' + view.reagents[0] if view.reagents else ''}) sweeps {chamber} to "
                    f"{reached * 100:.3g}% against the {target * 100:.3g}% target: "
                    f"{(1 - reached) * 100:.3g}% of the old fluid is still there when the next "
                    f"reagent arrives, {shortfall:.3g}x the allowed residual."
                ),
                feature_id=str(chamber),
                step=view.label,
                value=reached,
                limit=target,
                units="fraction",
                source=source,
                fix_hint=(
                    (f"sweep about {extra:.3g} more chamber volumes"
                     if extra else
                     f"raise the swept volume (currently {view.volume_ul or '?'} uL at "
                     f"{view.washes or 1:g} wash(es))")
                    + ", or fix the dead zone that is holding the residual"
                ),
                assumptions=_contract_assumptions("exchange.target_fraction"),
            )


# --- DEAD END: graph reachability -------------------------------------------------

def _graph(mesh: Any) -> tuple[dict[str, set[str]], dict[str, str | None], dict[str, str]]:
    """adjacency, node -> feature_id, port_id -> node_id."""
    adjacency: dict[str, set[str]] = defaultdict(set)
    feature_of: dict[str, str | None] = {}
    for node in _iter_records(_get(mesh, "nodes", default=[]) if mesh is not None else []):
        if not _is_record(node):
            continue
        nid = str(_get(node, "id", default="") or "")
        if nid:
            adjacency.setdefault(nid, set())
            fid = _get(node, "feature_id", default=None)
            feature_of[nid] = str(fid) if fid else None
    for edge in _iter_records(_get(mesh, "edges", default=[]) if mesh is not None else []):
        if not _is_record(edge):
            continue
        a, b = _get(edge, "a", default=None), _get(edge, "b", default=None)
        if a is None or b is None:
            continue
        adjacency[str(a)].add(str(b))
        adjacency[str(b)].add(str(a))
    ports = {str(k): str(v) for k, v in _mapping(_get(mesh, "ports", default={}) or {}).items()}
    return adjacency, feature_of, ports


def _reach(adjacency: Mapping[str, set[str]], seeds: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    stack = [s for s in seeds if s in adjacency]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(n for n in adjacency.get(node, ()) if n not in seen)
    return seen


def _components(adjacency: Mapping[str, Iterable[str]]) -> list[set[str]]:
    out: list[set[str]] = []
    unseen = set(adjacency)
    while unseen:
        comp = _reach(adjacency, [min(unseen)])  # type: ignore[arg-type]
        comp &= set(adjacency)
        out.append(comp)
        unseen -= comp
    return out


_VIRTUAL_TERMINAL = "\x00*feeds_and_vents*"


def flow_carrying_nodes(
    adjacency: Mapping[str, set[str]], feeds: Iterable[str], vents: Iterable[str]
) -> set[str]:
    """Nodes that carry flow from some feed to some vent.

    A plain BFS from the feeds is NOT the test, and getting that wrong hides every dead
    end there is: the graph is undirected, so a blind branch off a live chamber is
    "reachable" from both the inlet and the outlet while carrying no flow at all.

    The actual criterion is a cycle. Join one VIRTUAL node to every feed and every vent;
    then a node carries flow exactly when it lies on a cycle through that virtual node,
    because such a cycle IS a feed-to-vent path plus the return through the virtual node.
    Two nodes lie on a common cycle exactly when they share a biconnected component, so
    the answer is the union of the biconnected components containing the virtual node -
    one Tarjan pass, O(V+E).

    That single rule covers all three ways a region goes stagnant: a component with no
    feed or no vent, a pendant branch or tree with one mouth, and a loop hanging off a
    single junction (which matters, because `ring_junction` is in the block catalogue).
    Stokes/Hele-Shaw has no recirculation, so zero net flow means genuinely stagnant -
    there is no eddy keeping it stirred.
    """
    adj: dict[str, set[str]] = {n: set(v) for n, v in adjacency.items()}
    adj[_VIRTUAL_TERMINAL] = set()
    for terminal in set(feeds) | set(vents):
        if terminal in adjacency:
            adj[_VIRTUAL_TERMINAL].add(terminal)
            adj[terminal].add(_VIRTUAL_TERMINAL)
    if not adj[_VIRTUAL_TERMINAL]:
        return set()

    disc: dict[str, int] = {}
    low: dict[str, int] = {}
    parent: dict[str, str | None] = {}
    carrying: set[str] = set()
    edge_stack: list[tuple[str, str]] = []
    timer = 0

    for root in sorted(adj):
        if root in disc:
            continue
        parent[root] = None
        disc[root] = low[root] = timer
        timer += 1
        stack: list[tuple[str, Any]] = [(root, iter(sorted(adj[root])))]
        while stack:
            node, neighbours = stack[-1]
            advanced = False
            for nbr in neighbours:
                if nbr == parent.get(node) or nbr not in adj:
                    continue
                if nbr not in disc:
                    parent[nbr] = node
                    disc[nbr] = low[nbr] = timer
                    timer += 1
                    edge_stack.append((node, nbr))
                    stack.append((nbr, iter(sorted(adj[nbr]))))
                    advanced = True
                    break
                if disc[nbr] < disc[node]:
                    edge_stack.append((node, nbr))
                    low[node] = min(low[node], disc[nbr])
            if advanced:
                continue
            stack.pop()
            if not stack:
                break
            p = stack[-1][0]
            low[p] = min(low[p], low[node])
            if low[node] < disc[p]:
                continue
            # p closes a biconnected component: pop its edges off the stack.
            component: set[str] = set()
            while edge_stack and disc[edge_stack[-1][0]] >= disc[node]:
                u, v = edge_stack.pop()
                component.update((u, v))
            if edge_stack and edge_stack[-1] == (p, node):
                u, v = edge_stack.pop()
                component.update((u, v))
            # A two-node component is a single BRIDGE, not a cycle. One involving the
            # virtual node means that terminal's only route to any other terminal is the
            # virtual edge itself - an inlet joined to nothing, which carries no flow.
            if _VIRTUAL_TERMINAL in component and len(component) > 2:
                carrying |= component
    carrying.discard(_VIRTUAL_TERMINAL)
    return carrying


def stagnant_nodes(
    adjacency: Mapping[str, set[str]], feeds: Iterable[str], vents: Iterable[str]
) -> set[str]:
    """Nodes carrying no flow between the feeds and the vents. See flow_carrying_nodes."""
    return set(adjacency) - flow_carrying_nodes(adjacency, feeds, vents)


def _open_ports(sb: StepBundle, ports: Mapping[str, str]) -> tuple[set[str], set[str]]:
    """(feed nodes, vent nodes) for this step, from the Scenario or the protocol step."""
    feeds: set[str] = set()
    vents: set[str] = set()
    scenario_ports = _mapping(_get(sb.scenario, "ports", default={}) or {}) if sb.scenario is not None else {}
    for port_id, spec in scenario_ports.items():
        node = ports.get(str(port_id), str(port_id))
        if isinstance(spec, str):
            if spec.lower() == "pressure":
                vents.add(node)
            continue
        # scenario.PortBC states its role outright; prefer that over guessing from sign.
        role = str(_get(spec, "role", default="") or "").lower()
        kind = str(_get(spec, "type", default="") or "").lower()
        if kind == "closed" or role == "cap":
            continue
        if role == "feed":
            feeds.add(node)
        elif role in ("vent", "waste"):
            vents.add(node)
        elif kind == "flow":
            q = _num(_get(spec, "q_ul_min", default=None))
            (feeds if (q is None or q > 0) else vents).add(node)
        elif kind == "pressure":
            vents.add(node)
    if not scenario_ports:
        for port_id in sb.view.feeds:
            feeds.add(ports.get(port_id, port_id))
        for port_id in sb.view.vents:
            vents.add(ports.get(port_id, port_id))
    return feeds, vents


def check_dead_ends(bundle: Bundle, collector: _Collector) -> dict[Any, set[str]]:
    """Dead ends, and which features are stagnant in which step.

    TWO different questions, and conflating them produces nothing but false positives:

      1. DESIGN. Is there a region with no path from any inlet to any outlet, with every
         port of the chip available? That is the README's dead_end: a geometry fault, a
         blind branch nobody can fill or drain. It does not depend on the protocol.
      2. THIS STEP. Is a chamber the step actually TARGETS cut off by the port states the
         step itself asks for? That is a blocker, because the step cannot do what it says.

    What is NOT a finding is an idle lane. module1 grows its three lanes on separate days
    and module6 loads one lane at a time, so at any moment most of the chip is
    deliberately capped and therefore stagnant. Reporting that as a dead end would bury
    the real ones under hundreds of entries - and the chip would be condemned for working
    exactly as designed.

    Returns `{step_label: {stagnant feature ids}}`, which the trap and shear checks use to
    stay quiet about regions that carry no flow in that step.
    """
    adjacency, feature_of, ports = _graph(bundle.mesh)
    dead_by_step: dict[Any, set[str]] = {}
    if not adjacency:
        return dead_by_step

    port_nodes = {node for node in ports.values() if node in adjacency}
    # How many mesh nodes each chip feature owns, so we can tell a feature that is
    # ENTIRELY cut off from one with a stagnant corner. A chamber is discretised across
    # its width, so a couple of slow cells in a corner is a dead ZONE - the trap finding -
    # and calling that an isolated chamber would condemn every wide chamber on the chip.
    nodes_per_feature: dict[str, int] = defaultdict(int)
    for node, feature in feature_of.items():
        nodes_per_feature[feature or node] += 1

    def split_features(stranded: set[str]) -> tuple[set[str], set[str]]:
        """(features entirely stagnant, features only partly stagnant)."""
        counts: dict[str, int] = defaultdict(int)
        for node in stranded:
            counts[feature_of.get(node) or node] += 1
        whole = {f for f, n in counts.items() if n >= nodes_per_feature.get(f, n)}
        return whole, set(counts) - whole

    # --- 1. the design question: every port open at once.
    if port_nodes:
        stranded = stagnant_nodes(adjacency, port_nodes, port_nodes)
        whole, partial = split_features(stranded)
        sub = {n: {m for m in adjacency[n] if m in stranded} for n in stranded}
        for region in sorted(_components(sub), key=lambda r: (-len(r), min(r))):
            features = sorted({feature_of.get(n) or n for n in region})
            entire = [f for f in features if f in whole]
            if not entire:
                # A pocket inside a feature that otherwise flows is a dead ZONE, and the
                # trap class already reports it from transport.py with a volume fraction
                # and a diffusive clearance time - far more use than a node count. A
                # dead_end is a BRANCH nobody can reach. One root cause, one finding.
                continue
            collector.add(
                "dead_end", "high",
                metric="unreachable_nodes",
                message=(
                    f"{len(region)} mesh node(s) covering {', '.join(features[:6])}"
                    f"{' and more' if len(features) > 6 else ''} have no path from any inlet to "
                    f"any outlet even with EVERY port of the chip open, so no protocol can "
                    f"reach them. {', '.join(entire[:4])} is cut off entirely: it cannot be "
                    f"filled, exchanged or drained."
                ),
                feature_id=entire[0],
                step=None,
                value=float(len(region)),
                limit=0.0,
                units="nodes",
                source="graph reachability over the whole mesh",
                fix_hint=(
                    "add a vent port on that branch or a bypass channel to an outlet; if it is "
                    "meant to be a blind pocket, it still has to vent to prime"
                ),
            )

    # --- 2. the per-step question, and the stagnant map the other checks need.
    cache: dict[tuple[frozenset, frozenset], set[str]] = {}
    for sb in bundle.steps:
        view = sb.view
        if not view.has_flow:
            continue
        feeds, vents = _open_ports(sb, ports)
        if not feeds or not vents:
            # A step that drives nothing. scenario.py owns that complaint.
            continue
        if not (feeds & set(adjacency)) or not (vents & set(adjacency)):
            # The step names ports this mesh has no node for, so reachability is
            # undecidable here. That is a mesh/scenario disagreement, which mesh.py and
            # scenario.py own - inventing a chip-wide dead end from it would be wrong.
            continue
        key = (frozenset(feeds), frozenset(vents))
        if key not in cache:
            cache[key] = stagnant_nodes(adjacency, feeds, vents)
        stranded = cache[key]
        fully_stagnant, _partly = split_features(stranded)
        # Only a feature with NO flowing node is "dead" for the purposes of silencing the
        # shear and trap checks. A chamber with a stagnant corner must still get its trap
        # finding - that corner is the whole point of the trap class.
        dead_by_step[view.label] = fully_stagnant

        # A port this step actually drives or vents through, that carries no flow. The
        # step names it, so the step is asking for something the plumbing cannot do.
        for port_id in sorted(set(view.feeds) | set(view.vents)):
            node = ports.get(port_id, port_id)
            if node not in stranded:
                continue
            role = "feeds through" if port_id in view.feeds else "vents through"
            collector.add(
                "dead_end", "blocker",
                metric="step_port_stranded",
                message=(
                    f"step {view.label} ({view.operation}"
                    f"{', ' + view.reagents[0] if view.reagents else ''}) {role} port "
                    f"{port_id}, but no flow can pass it: there is no feed-to-vent path "
                    f"through {port_id} under this step's own port states. The step cannot "
                    f"run as written."
                ),
                feature_id=feature_of.get(node) or port_id,
                step=view.label,
                value=0.0,
                limit=None,
                units="feed-to-vent paths through the port",
                source="graph reachability under this step's port states",
                fix_hint=(
                    f"connect {port_id} to the network, or open a matching vent so the step "
                    f"has somewhere to push to"
                ),
            )

        for target in sorted(set(view.targets) & fully_stagnant):
            collector.add(
                "dead_end", "blocker",
                metric="target_isolated_by_step_ports",
                message=(
                    f"step {view.label} ({view.operation}"
                    f"{', ' + view.reagents[0] if view.reagents else ''}) acts on {target}, but "
                    f"with the port states this step asks for - feeding "
                    f"{', '.join(sorted(view.feeds)) or 'nothing'} and venting "
                    f"{', '.join(sorted(view.vents)) or 'nothing'} - there is no path through "
                    f"{target} from a feed to a vent. The step cannot move liquid through its "
                    f"own target, so it cannot do what it says."
                ),
                feature_id=target,
                step=view.label,
                value=0.0,
                limit=None,
                units="feed-to-vent paths through the target",
                source="graph reachability under this step's port states",
                fix_hint=(
                    f"open a vent downstream of {target} for this step, or correct the step's "
                    f"feed/vent ports in the protocol"
                ),
            )
    return dead_by_step


# --- BUBBLE ----------------------------------------------------------------------

def _priming_step(bundle: Bundle) -> StepBundle | None:
    """The step that first wets the chip. Bubbles are trapped then or never."""
    for sb in bundle.steps:
        if sb.view.operation in ("prime", "wet", "fill", "flush") or "prim" in sb.view.text:
            return sb
    return next((sb for sb in bundle.steps if sb.view.has_flow), None)


def capillary_pressure_pa(width_mm: float, depth_mm: float) -> float:
    """Laplace pressure of a meniscus in a rectangular duct, from flagged assumptions.

    dP = 2 gamma cos(theta) (1/w + 1/h). Positive means the surface pulls the liquid in.
    """
    gamma = assumed("surface_tension_n_per_m")
    theta = math.radians(assumed("contact_angle_deg"))
    w, h = width_mm / 1000.0, depth_mm / 1000.0
    if w <= 0 or h <= 0:
        return 0.0
    return 2.0 * gamma * math.cos(theta) * (1.0 / w + 1.0 / h)


def check_bubbles(bundle: Bundle, collector: _Collector) -> None:
    sb = _priming_step(bundle)
    if sb is None:
        return
    view = sb.view
    adjacency, feature_of, ports = _graph(bundle.mesh)
    port_nodes = set(ports.values())

    # a) dead-end branches with no vent at priming: air has nowhere to go.
    if adjacency:
        feeds, vents = _open_ports(sb, ports)
        if feeds & set(adjacency) and vents & set(adjacency):
            # Only branches the liquid actually REACHES can trap air: an isolated region
            # never wets at all, which is the dead_end finding, not a bubble.
            wetted = _reach(adjacency, feeds)
            for node in sorted(stagnant_nodes(adjacency, feeds, vents) & wetted):
                degree = len(adjacency.get(node, ()))
                if degree > 1 or node in port_nodes:
                    continue
                feature = feature_of.get(node) or node
                collector.add(
                    "bubble", "high",
                    metric="unvented_dead_end_at_priming",
                    message=(
                        f"{feature} is a dead-end branch reached by the priming step "
                        f"{view.label} ({view.operation}) with no vent beyond it. The liquid "
                        f"front seals the mouth and the air in the branch has nowhere to go, so "
                        f"it stays as a bubble - compressible, and it will move later and shed "
                        f"the trapped volume into the sample."
                    ),
                    feature_id=feature,
                    step=view.label,
                    value=1.0,
                    limit=0.0,
                    units="unvented dead ends",
                    fix_hint="add a vent port or a hydrophobic membrane vent at the branch tip, or prime through it",
                )

    # b) sudden expansions where the meniscus pins.
    edges = {}
    for edge in _iter_records(_get(bundle.mesh, "edges", default=[]) if bundle.mesh is not None else []):
        eid = _get(edge, "id", default=None) if _is_record(edge) else None
        if eid:
            edges[str(eid)] = edge
    pin_factor = assumed("expansion_pin_factor")
    by_node: dict[str, list[Any]] = defaultdict(list)
    for edge in edges.values():
        for end in ("a", "b"):
            node = _get(edge, end, default=None)
            if node is not None:
                by_node[str(node)].append(edge)
    drive = None
    if sb.field is not None:
        peak, _, _ = _peak_pressure_kpa(sb.field)
        drive = peak * 1000.0 if peak is not None else None
    reported: set[tuple[str, str]] = set()
    for node, incident in sorted(by_node.items()):
        widths = [(str(_get(e, "id", default="")), _num(_get(e, "width_mm", default=None)),
                   _num(_get(e, "depth_mm", default=None))) for e in incident]
        widths = [(i, w, d) for i, w, d in widths if w and d]
        if len(widths) < 2:
            continue
        narrow = min(widths, key=lambda t: t[1])
        wide = max(widths, key=lambda t: t[1])
        if wide[1] < pin_factor * narrow[1]:
            continue
        key = (narrow[0], wide[0])
        if key in reported:
            continue
        reported.add(key)
        needed = capillary_pressure_pa(narrow[1], narrow[2]) - capillary_pressure_pa(wide[1], wide[2])
        if needed <= 0:
            continue  # the wide side pulls harder; the front crosses on its own.
        ratio = _ratio(needed, drive) if drive else None
        severity = severity_for_ratio(ratio, floor="low") if ratio is not None else "medium"
        feature = feature_of.get(node) or str(_get(incident[0], "feature_id", default=node))
        collector.add(
            "bubble", severity,
            metric="capillary_pin_pressure_pa",
            message=(
                f"{wide[1]:.3g} mm opens off {narrow[1]:.3g} mm at {feature} "
                f"({wide[1] / narrow[1]:.3g}x): a filling meniscus pins at the step and the "
                f"corners fill last, trapping air. Crossing it needs {needed:.3g} Pa"
                + (f" against {drive:.3g} Pa of drive pressure in step {view.label}." if drive else
                   ", and no field was solved for the priming step to compare it with.")
                + " Contact angle and surface tension are NOT in the contracts, so this is an "
                "order-of-magnitude check, not a number to trust."
            ),
            feature_id=feature,
            step=view.label,
            value=needed,
            limit=drive,
            units="Pa",
            fix_hint=(
                "taper the expansion instead of stepping it, add a phase_guide to pin the front "
                "on a line you chose, or prime from the wide side"
            ),
            assumptions=["contact_angle_deg", "surface_tension_n_per_m", "expansion_pin_factor"],
        )


# --- SHEAR -----------------------------------------------------------------------

def _edge_features(mesh: Any) -> dict[str, str]:
    out = {}
    for coll in ("edges", "nodes"):
        for rec in _iter_records(_get(mesh, coll, default=[]) if mesh is not None else []):
            rid = _get(rec, "id", default=None) if _is_record(rec) else None
            if rid:
                fid = _get(rec, "feature_id", default=None)
                out[str(rid)] = str(fid) if fid else str(rid)
    return out


def check_shear(bundle: Bundle, collector: _Collector, dead_regions: Mapping[Any, set[str]]) -> None:
    chambers = chamber_views(bundle.chip)
    steps = bundle.all_step_views()
    capsule_chip, capsule_evidence = is_capsule_chip(bundle.chip, bundle.protocol)
    feature_of = _edge_features(bundle.mesh)
    unknown_seen: set[str] = set()
    # The scope depends on (chamber, step), never on the individual edge, so it is
    # resolved once per pair: a 7000-edge mesh would otherwise re-walk the protocol
    # once per edge per step.
    scopes: dict[tuple[str, int], ScopeCall] = {}

    def scope_for(chamber: ChamberView, view: StepView) -> ScopeCall:
        key = (chamber.id, view.index)
        if key not in scopes:
            scopes[key] = shear_scope(
                chamber, bundle.aligned(view), steps,
                capsule_chip=capsule_chip, capsule_evidence=capsule_evidence,
            )
        return scopes[key]

    for sb in bundle.steps:
        view = sb.view
        if sb.field is None or not view.has_flow:
            continue
        shear = _mapping(_get(sb.field, "shear_dyn_cm2", default={}) or {})
        if not shear:
            continue
        # worst shear per chip feature, so one over-shear channel is one finding
        worst: dict[str, tuple[float, str]] = {}
        for key, raw in shear.items():
            value = _num(raw if not isinstance(raw, Mapping) else raw.get("value"))
            if value is None:
                continue
            feature = feature_of.get(str(key), str(key))
            if feature not in worst or abs(value) > worst[feature][0]:
                worst[feature] = (abs(value), str(key))

        for feature, (value, where) in sorted(worst.items()):
            chamber = chambers.get(feature)
            if chamber is None:
                # A DUCT, not a chamber - and the three ceilings are properties of what a
                # chamber HOLDS, not of what flows past. The contract is explicit that
                # 0.01 "applies to chambers holding ADHERENT CELLS, where the risk is
                # detaching them": nothing is attached inside a feed line, so holding a
                # feed comb or an outlet channel to the culture ceiling is the same
                # scoping error as judging a capsule bed by it, pointing the other way.
                # It produced ten spurious blockers on module1 and six on module6, which
                # buried the one chamber that is genuinely over its ceiling.
                #
                # Cells in SUSPENSION crossing a duct are a real question and the
                # contracts hold no ceiling for it, so we do not invent one here.
                continue
            scope = scope_for(chamber, view)
            if scope.is_unknown:
                if chamber.id in unknown_seen:
                    continue
                unknown_seen.add(chamber.id)
                collector.add(
                    "shear", "medium",
                    metric="shear_scope_unknown",
                    message=(
                        f"cannot tell what {chamber.id} holds, so no shear ceiling applies to it "
                        f"and the peak wall shear of {value:.3g} dyn/cm2 (step {view.label}, at "
                        f"{where}) is UNJUDGED. Looked at: {'; '.join(scope.evidence)}. The three "
                        f"ceilings are not interchangeable - 0.01 is for adherent cells only, 0.3 "
                        f"for a rinse before a deliberate lyse, and capsules have no shear ceiling "
                        f"at all - so picking the strictest would invent a blocker and picking the "
                        f"loosest would hide one."
                    ),
                    feature_id=chamber.id,
                    step=view.label,
                    value=value,
                    limit=None,
                    units="dyn/cm2",
                    fix_hint=(
                        "state `contents` (adherent_cells | capsules | none) on the chamber in the "
                        "chip spec, or name the reagent and operation in the protocol so the scope "
                        "is derivable"
                    ),
                    evidence=scope.evidence,
                )
                continue
            if scope.ceiling_dyn_cm2 is None:
                # capsules, or nothing shear-sensitive. Pressure governs, and the leak and
                # weir_escape checks own that - reporting shear here would be the exact
                # scoping error this module exists to avoid.
                continue
            if feature in dead_regions.get(view.label, set()):
                continue
            ratio = _ratio(value, scope.ceiling_dyn_cm2)
            if ratio is None or ratio < LADDER[-1][0]:
                continue
            collector.add(
                "shear", severity_for_ratio(ratio),
                metric=f"wall_shear_vs_{scope.shear_class}_ceiling",
                message=(
                    f"wall shear {value:.3g} dyn/cm2 at {feature} in step {view.label} "
                    f"({view.operation}{', ' + view.reagents[0] if view.reagents else ''}) against "
                    f"the {scope.shear_class} ceiling {scope.ceiling_dyn_cm2:g} dyn/cm2, "
                    f"{ratio:.3g}x over. Scope: {'; '.join(scope.evidence[:2])}."
                ),
                feature_id=feature,
                step=view.label,
                value=value,
                limit=scope.ceiling_dyn_cm2,
                units="dyn/cm2",
                fix_hint=(
                    f"shear is linear in Q and goes as 1/(w h^2): drop the step flow to "
                    f"{(view.flow_ul_min / ratio):.3g} uL/min"
                    if view.flow_ul_min
                    else "drop the step flow or widen the lane"
                )
                + (
                    f", or widen the lane by {ratio:.3g}x. If this is really a rinse before a "
                    f"deliberate lyse, say so in the protocol and the 0.3 ceiling applies instead."
                    if scope.shear_class == "adherent_cells"
                    else ""
                ),
                assumptions=_contract_assumptions(
                    "shear.culture_ceiling_dyn_cm2" if scope.shear_class == "adherent_cells"
                    else "shear.prelysis_rinse_ceiling_dyn_cm2"
                ),
                evidence=scope.evidence,
            )


# --- particles: SEDIMENT, CLOG, WEIR ESCAPE ---------------------------------------

def _particle_records(particles: Any, *names: str) -> list[Mapping]:
    """One kind of particle verdict, as mappings.

    particles.py hands over dataclass verdicts (`ClogVerdict`, `SedimentVerdict`,
    `WeirBand`) with an `as_json()`, or a dict of lists. Both are accepted.
    """
    if particles is None:
        return []
    blob = _get(particles, *names, default=None)
    out: list[Mapping] = []
    for rec in _iter_records(blob):
        if isinstance(rec, Mapping):
            out.append(rec)
            continue
        as_json = getattr(rec, "as_json", None) or getattr(rec, "to_json", None)
        if callable(as_json):
            out.append(as_json())
        elif hasattr(rec, "feature_id"):
            out.append({k: v for k, v in vars(rec).items()})
    return out


_SETTLING_IS_FINE = frozenset({"desired", "retention", "parked", "intended", "wanted"})
_SETTLING_OK_VERDICTS = frozenset({"captured", "parked", "ok", "passes", "retained", "none"})


def _settling_is_a_loss(rec: Any) -> bool:
    """Is this settling record a PROBLEM, or the chip doing its job?

    particles.py works out the intent from the geometry and the step - whether a particle
    settling here is a loss or the entire point - and its `intent` / `verdict` are
    authoritative. Ignoring them means reporting module3's capsule bed settling onto its
    own retention weir as a blocker, which is the chip working exactly as designed.

    Only when no intent is stated do we fall back to the raw time comparison.
    """
    intent = str(_get(rec, "intent", default="") or "").lower()
    verdict = str(_get(rec, "verdict", default="") or "").lower()
    if intent in _SETTLING_IS_FINE:
        return False
    if verdict in _SETTLING_OK_VERDICTS:
        return False
    return True


def check_particles(bundle: Bundle, collector: _Collector) -> None:
    clog_blockers: set[str] = set()
    worst_clog: dict[str, tuple[float, StepView, Mapping]] = {}
    worst_settle: dict[str, tuple[float, StepView, Mapping]] = {}
    worst_weir: dict[str, tuple[float, StepView, Mapping]] = {}

    for sb in bundle.steps:
        if sb.particles is None or not sb.view.has_flow:
            continue
        for rec in _particle_records(sb.particles, "clog", "clogs", "clog_margin"):
            feature = str(_get(rec, "feature_id", "edge_id", "id", default="?"))
            # particles.py reports min(width, depth) in um, which is the right dimension:
            # a 0.6 mm wide, 0.3 mm deep duct passes what a 0.3 mm slot passes.
            width = _num(_get(rec, "min_dimension_um", default=None))
            width = width / 1000.0 if width is not None else _num(
                _get(rec, "width_mm", "width", default=None))
            if width is None:
                continue
            if feature not in worst_clog or width < worst_clog[feature][0]:
                worst_clog[feature] = (width, sb.view, rec)
        for rec in _particle_records(sb.particles, "settling", "sediment", "sediments"):
            feature = str(_get(rec, "feature_id", "edge_id", "id", default="?"))
            if not _settling_is_a_loss(rec):
                continue
            settle = _num(_get(rec, "fall_time_s", "settle_s", "settling_s", default=None))
            transit = _num(_get(rec, "transit_time_s", "transit_s", default=None))
            ratio = _num(_get(rec, "ratio", default=None))
            if ratio is None:
                if settle is None or transit is None or transit <= 0:
                    continue
                ratio = transit / settle if settle > 0 else math.inf
            if feature not in worst_settle or ratio > worst_settle[feature][0]:
                worst_settle[feature] = (ratio, sb.view, rec)
        for rec in _particle_records(sb.particles, "weir_escape", "weir", "weir_bands"):
            feature = str(_get(rec, "feature_id", "chamber_id", "id", default="?"))
            dp = _num(_get(rec, "nominal_dp_kpa", "dp_kpa", "dp_across_weir_kpa", "dp",
                           default=None))
            if dp is None:
                continue
            if feature not in worst_weir or dp > worst_weir[feature][0]:
                worst_weir[feature] = (dp, sb.view, rec)

    # CLOG
    try:
        swollen = contracts.swollen_capsule_diameter_um()
        factor = float(contracts.param("capsule_channel.width_factor_low"))
    except contracts.ContractError as exc:
        if worst_clog:
            collector.add("clog", "medium", metric="clog_contract",
                          message=f"cannot judge clog margin: {exc}", units="-")
        swollen = factor = None  # type: ignore[assignment]
    if swollen is not None:
        required_mm = swollen * factor / 1000.0
        for feature, (width, view, rec) in sorted(worst_clog.items()):
            required_um = _num(_get(rec, "required_um", default=None))
            required = (required_um / 1000.0 if required_um is not None
                        else _num(_get(rec, "required_mm", default=None)) or required_mm)
            ratio = _ratio(required, width)
            if ratio is None or ratio < LADDER[-1][0]:
                continue
            jams = width * 1000.0 < swollen
            severity = "blocker" if jams else severity_for_ratio(ratio)
            if severity == "blocker":
                clog_blockers.add(feature)
            collector.add(
                "clog", severity,
                metric="lane_width_vs_swollen_capsule",
                message=(
                    f"{feature} is {width:.3g} mm wide and has to carry capsules that swell to "
                    f"{swollen:g} um, needing {required:.3g} mm at the {factor:g}x rule"
                    + (
                        ". It is narrower than a single swollen capsule, so it WILL jam, not "
                        "might: the bed backs up and the pressure then runs at the retention gap."
                        if jams
                        else f" - {ratio:.3g}x short, so aggregates bridge the lane."
                    )
                    + f" Worst in step {view.label} ({view.operation})."
                ),
                feature_id=feature,
                step=view.label,
                value=width,
                limit=required,
                units="mm",
                fix_hint=f"widen {feature} to at least {required:.3g} mm, or filter aggregates upstream",
                assumptions=_contract_assumptions("capsule_channel.width_factor_low", "capsule.swell_factor"),
            )

    # SEDIMENT - suppressed where a clog blocker already explains it.
    for feature, (ratio, view, rec) in sorted(worst_settle.items()):
        if feature in clog_blockers or ratio is None or ratio < LADDER[-1][0]:
            continue
        settle = _num(_get(rec, "fall_time_s", "settle_s", "settling_s", default=None))
        transit = _num(_get(rec, "transit_time_s", "transit_s", default=None))
        # Settling is linear in excess density, so a finding is meaningless without
        # saying which end of the band it rests on. This is a LOSS check: particles
        # dropping out before they cross, so the conservative end is the MAX.
        particle_name = str(_get(rec, "particle", default="") or "").lower()
        density_note = ""
        density_key = ""
        try:
            choice = excess_density(particle_name, "loss")
            density_note = f" Density end required for a loss check: {choice.note}."
            density_key = choice.key
        except (contracts.ContractError, ValueError):
            density_note = (
                f" Settling velocity came from particles.py; this finding does not restate "
                f"which end of the density band it used."
            )
        collector.add(
            "sediment", severity_for_ratio(ratio),
            metric="transit_time_over_settling_time",
            message=(
                f"particles drop out in {feature} during step {view.label} ({view.operation}): "
                + (
                    f"settling takes {settle:.3g} s and the transit takes {transit:.3g} s, "
                    if settle is not None and transit is not None
                    else ""
                )
                + f"{ratio:.3g}x longer than it takes to reach the floor, so they land before "
                f"they cross and are left behind."
                + (f" Why that is a loss here: {why}." if (why := str(
                    _get(rec, "intent_why", default="") or "")) else "")
                + density_note
            ),
            feature_id=feature,
            step=view.label,
            value=ratio,
            limit=1.0,
            units="transit/settling",
            source=density_key,
            fix_hint=(
                f"raise the local velocity {ratio:.3g}x (higher flow or a narrower lane), shorten "
                f"the path, or accept the loss and state it"
            ),
            assumptions=_contract_assumptions(density_key) if density_key else (),
        )

    # WEIR ESCAPE
    for feature, (dp, view, rec) in sorted(worst_weir.items()):
        try:
            limit = contracts.max_working_pressure_kpa()
        except contracts.ContractError:
            continue
        stated = _num(_get(rec, "pressure_limit_kpa", "limit_kpa", default=None))
        limit = stated if stated is not None else limit
        ratio = _ratio(dp, limit)
        if ratio is None or ratio < LADDER[-1][0]:
            continue
        collector.add(
            "weir_escape", severity_for_ratio(ratio),
            metric="dp_across_retention_vs_escape_margin",
            message=(
                f"{dp:.3g} kPa across the retention feature at {feature} in step {view.label} "
                f"({view.operation}) against the {limit:.3g} kPa safe limit "
                f"(capsule.escape_pressure_kpa x control.pressure_margin), {ratio:.3g}x over. "
                f"Soft capsules extrude through the gap and the bed walks downstream - this is "
                f"the pressure limit that governs a capsule chamber, in place of a shear ceiling."
            ),
            feature_id=feature,
            step=view.label,
            value=dp,
            limit=limit,
            units="kPa",
            fix_hint=(
                f"drop the step flow to {(view.flow_ul_min / ratio):.3g} uL/min"
                if view.flow_ul_min else "drop the step flow"
            ) + ", widen the weir slit across the full chamber width, or split the wash into more passes",
            assumptions=_contract_assumptions("capsule.escape_pressure_kpa", "control.pressure_margin"),
        )


# --- HANDOFF ---------------------------------------------------------------------

_CARRIES_TO_PARTICLE = {
    "cells": "cells", "cell": "cells", "capsules": "capsules", "capsule": "capsules",
    "beads": "beads", "bead": "beads",
}


def _check_handoff_settling(
    rec: Any, src: str, dst: str, feature: str, step: Any, collector: _Collector
) -> None:
    """Do the particles drop out in the off-chip line before they arrive?

    A LOSS check, so it takes the MAX of the excess-density band (see `excess_density`):
    if even the densest plausible particle stays suspended, none are lost. The line
    geometry and the valve volume are contract assumptions, not numbers invented here.
    """
    carries = str(_get(rec, "carries", default="") or "").lower()
    particle = _CARRIES_TO_PARTICLE.get(carries)
    if particle is None:
        return  # a library or a supernatant has nothing to settle
    diameter_um = _num(_get(rec, "particle_um", "stated_particle_um", "particle_in_um",
                            default=None))
    if diameter_um is None:
        diameter_um = (contracts.swollen_capsule_diameter_um()
                       if particle == "capsules" else None)
        if diameter_um is None:
            return
    try:
        density = excess_density(particle, "loss")
        bore_mm = float(contracts.param("tubing.inner_diameter_mm"))
        length_mm = float(contracts.param("tubing.length_mm"))
        valve_ul = float(contracts.param("selector_valve.internal_volume_ul"))
    except (contracts.ContractError, ValueError) as exc:
        collector.add(
            "handoff", "medium",
            metric="settling_contract",
            message=f"cannot judge settling in the {src}->{dst} line: {exc}",
            feature_id=feature, step=step, units="-",
            fix_hint="add the key to contracts/parameters.json with its units and source",
        )
        return

    # The line carries no temperature, and the real device runs warm.
    mu = contracts.viscosity_for(None)
    v_mm_s = stokes_settling_mm_s(density.kg_m3, diameter_um, mu.pa_s)
    if v_mm_s <= 0:
        return
    # A particle entering mid-bore has to fall half the bore to reach the wall and stall.
    fall_mm = bore_mm / 2.0
    settle_s = fall_mm / v_mm_s
    line_ul = math.pi * (bore_mm / 2.0) ** 2 * length_mm / 1000.0 + valve_ul
    flow_ul_min = _num(_get(rec, "flow_ul_min", "transfer_flow_ul_min", default=None))
    if flow_ul_min is None or flow_ul_min <= 0:
        volume_ul = _num(_get(rec, "volume_out_ul", "released_volume_ul", "volume_ul",
                              "stated_volume_ul", default=None))
        duration_s = _num(_get(rec, "duration_s", default=None))
        if volume_ul and duration_s and duration_s > 0:
            flow_ul_min = volume_ul / duration_s * 60.0
    if flow_ul_min is None or flow_ul_min <= 0:
        collector.add(
            "handoff", "medium",
            metric="line_settling_undecidable",
            message=(
                f"{diameter_um:g} um {particle} cross the {src}->{dst} line and reach the "
                f"bore wall in {settle_s:.3g} s, but no transfer flow rate is stated for the "
                f"handoff, so whether they arrive is UNDECIDED. Line as the contract assumes "
                f"it: {bore_mm:g} mm bore, {length_mm:g} mm long, plus a {valve_ul:g} uL valve "
                f"= {line_ul:.3g} uL. Density end used: {density.note}."
            ),
            feature_id=feature, step=step, value=settle_s, limit=None, units="s",
            source=f"{density.key}, tubing.*, selector_valve.internal_volume_ul",
            fix_hint="state the transfer flow rate on the handoff, then re-run",
            assumptions=_contract_assumptions(
                density.key, "tubing.inner_diameter_mm", "tubing.length_mm",
                "selector_valve.internal_volume_ul"),
        )
        return
    transit_s = line_ul / flow_ul_min * 60.0
    ratio = _ratio(transit_s, settle_s)
    if ratio is None or ratio < LADDER[-1][0]:
        return
    collector.add(
        "handoff", severity_for_ratio(ratio),
        metric="line_transit_over_settling_time",
        message=(
            f"{diameter_um:g} um {particle} settle out in the {src}->{dst} line before they "
            f"arrive: reaching the bore wall takes {settle_s:.3g} s and the transit takes "
            f"{transit_s:.3g} s at {flow_ul_min:.3g} uL/min, {ratio:.3g}x longer. They stall "
            f"on the tubing wall and the receiving chip gets fewer than were sent. Line as "
            f"the contract assumes it: {bore_mm:g} mm bore, {length_mm:g} mm long, plus a "
            f"{valve_ul:g} uL valve = {line_ul:.3g} uL. This is a LOSS check, so it uses "
            f"{density.note}."
        ),
        feature_id=feature, step=step, value=ratio, limit=1.0, units="transit/settling",
        source=f"{density.key}, tubing.*, selector_valve.internal_volume_ul",
        fix_hint=(
            f"push the transfer faster than {line_ul / settle_s * 60.0:.3g} uL/min, shorten or "
            f"narrow the line, or keep it vertical so settling does not cross the flow"
        ),
        assumptions=_contract_assumptions(
            density.key, "tubing.inner_diameter_mm", "tubing.length_mm",
            "selector_valve.internal_volume_ul"),
    )


def check_handoffs(bundle: Bundle, collector: _Collector) -> None:
    if bundle.couple is None:
        return
    chip_id = bundle.resolved_chip_id()
    for rec in _iter_records(_get(bundle.couple, "handoffs", default=None)):
        if not _is_record(rec):
            continue
        src = str(_get(rec, "from_chip", "from_stage", "from", default="?"))
        dst = str(_get(rec, "to_chip", "to_stage", "to", default="?"))
        if chip_id not in (src, dst):
            continue
        feature = str(_get(rec, "id", "line_id", default=f"{src}->{dst}"))
        step = _get(rec, "step", "after_step", default=None)

        passable = _num(_get(rec, "particle_pass_um", "min_passable_um",
                             "narrowest_passable_um", default=None))
        incoming = _num(_get(rec, "particle_in_um", "particle_um", "stated_particle_um",
                             default=None))
        if passable is not None and incoming is not None and incoming > passable:
            collector.add(
                "handoff", "blocker",
                metric="particle_size_vs_line_passage",
                message=(
                    f"{src} hands {incoming:g} um particles to {dst} through {feature}, which "
                    f"passes {passable:g} um. They do not arrive: the line blocks and the run "
                    f"stops with the sample in the tubing."
                ),
                feature_id=feature, step=step, value=incoming, limit=passable, units="um",
                fix_hint=f"widen the line/port to above {incoming:g} um, or size-reduce before the handoff",
            )

        _check_handoff_settling(rec, src, dst, feature, step, collector)

        out_ul = _num(_get(rec, "volume_out_ul", "released_volume_ul", "volume_ul",
                           "stated_volume_ul", default=None))
        in_ul = _num(_get(rec, "volume_in_ul", "drawn_volume_ul", "accepts_ul",
                          "capacity_ul", default=None))
        if out_ul is not None and in_ul is not None and in_ul > 0:
            tol = assumed("handoff_volume_tolerance")
            mismatch = abs(out_ul - in_ul) / in_ul
            if mismatch > tol:
                collector.add(
                    "handoff", severity_for_ratio(mismatch / tol, floor="low"),
                    metric="handoff_volume_mismatch",
                    message=(
                        f"{src} delivers {out_ul:.3g} uL into {dst}, which expects {in_ul:.3g} uL "
                        f"- {mismatch * 100:.3g}% apart. "
                        + ("The receiving chip overflows into its vent." if out_ul > in_ul
                           else "The receiving chip is underfilled and part of its chamber never wets.")
                    ),
                    feature_id=feature, step=step, value=out_ul, limit=in_ul, units="uL",
                    fix_hint="re-meter the release volume, or resize the receiving chamber",
                    assumptions=["handoff_volume_tolerance"],
                )

        dead_ul = _num(_get(rec, "dead_volume_ul", "stated_dead_volume_ul", default=None))
        if dead_ul is not None and out_ul and dead_ul > 0:
            ratio = _ratio(dead_ul, out_ul * assumed("handoff_volume_tolerance"))
            if ratio is not None and ratio >= LADDER[-1][0]:
                collector.add(
                    "handoff", severity_for_ratio(ratio, floor="low"),
                    metric="dead_volume_fraction_of_transfer",
                    message=(
                        f"{dead_ul:.3g} uL of dead volume in {feature} against a {out_ul:.3g} uL "
                        f"transfer from {src} to {dst}: {dead_ul / out_ul * 100:.3g}% of the sample "
                        f"stays in the line."
                    ),
                    feature_id=feature, step=step, value=dead_ul,
                    limit=out_ul * assumed("handoff_volume_tolerance"), units="uL",
                    fix_hint="shorten or narrow the tubing, or chase the line with buffer and accept the dilution",
                    assumptions=["handoff_volume_tolerance"],
                )

        budget = _num(_get(rec, "pressure_budget_kpa", default=None))
        required = _num(_get(rec, "pressure_required_kpa", "dp_kpa", default=None))
        if budget is not None and required is not None and budget > 0:
            ratio = _ratio(required, budget)
            if ratio is not None and ratio >= 1.0:
                collector.add(
                    "handoff", severity_for_ratio(ratio),
                    metric="handoff_pressure_vs_budget",
                    message=(
                        f"driving the {src}->{dst} handoff through {feature} needs {required:.3g} kPa "
                        f"against a {budget:.3g} kPa budget, {ratio:.3g}x over."
                    ),
                    feature_id=feature, step=step, value=required, limit=budget, units="kPa",
                    fix_hint="shorten/widen the tubing, or slow the transfer",
                )


# --- UNMODELLED ------------------------------------------------------------------

def check_unmodelled(bundle: Bundle, collector: _Collector) -> None:
    """Every edge above Re 1. Not a number to trust - a statement that we must not.

    Module 6's quench flood runs Re 13-23 in the comb spine and feed line; a droplet
    nozzle runs Re ~9. There the Hele-Shaw network form is not imprecise, it is the
    wrong equations, so no shear or pressure number from those edges may be quoted.
    """
    ceiling = assumed("re_validity_ceiling")
    high = assumed("re_high_factor")
    feature_of = _edge_features(bundle.mesh)
    worst: dict[str, tuple[float, StepView, str]] = {}
    for sb in bundle.steps:
        if sb.field is None:
            continue
        for key, raw in _mapping(_get(sb.field, "re", "reynolds", default={}) or {}).items():
            value = _num(raw if not isinstance(raw, Mapping) else raw.get("value"))
            if value is None or abs(value) <= ceiling:
                continue
            feature = feature_of.get(str(key), str(key))
            if feature not in worst or abs(value) > worst[feature][0]:
                worst[feature] = (abs(value), sb.view, str(key))
    for feature, (value, view, where) in sorted(worst.items()):
        severity = "high" if value >= high else "medium"
        collector.add(
            "unmodelled", severity,
            metric="reynolds_number",
            message=(
                f"Re {value:.3g} at {feature} in step {view.label} ({view.operation}"
                f"{', ' + view.reagents[0] if view.reagents else ''}"
                f"{f', {view.flow_ul_min:g} uL/min' if view.flow_ul_min else ''}) is above the "
                f"Re {ceiling:g} validity limit of the depth-averaged Stokes/Hele-Shaw network "
                f"this solver uses"
                + (
                    ". An order of magnitude past it: inertia dominates, real 3D recirculation is "
                    "possible, and the pressure and shear numbers for this edge are not merely "
                    "imprecise - they come from the wrong equations. Treat every finding that "
                    "quotes this edge as out of scope, not as a pass."
                    if value >= high
                    else ". Outside the model's validity; the numbers on this edge are indicative only."
                )
            ),
            feature_id=feature,
            step=view.label,
            value=value,
            limit=ceiling,
            units="Reynolds number",
            fix_hint=(
                f"slow the step to about {view.flow_ul_min / value * ceiling:.3g} uL/min to come "
                f"back inside Stokes, or accept it and check this edge with a 3D solver - the "
                f"model cannot decide it"
                if view.flow_ul_min
                else "re-check this edge with a solver valid above Re 1; this one cannot decide it"
            ),
            assumptions=["re_validity_ceiling"] + (["re_high_factor"] if value >= high else []),
        )


# --- validation gate -------------------------------------------------------------

def validation_failures(validation: Any) -> list[str]:
    """Benchmarks validate.py reports as failed. Non-empty means nothing here is trusted.

    validate.py's report is `{status: pass|fail|incomplete, trusted: bool, counts: {...},
    benchmarks: [{id, name, status: pass|fail|skipped, ...}]}`. A benchmark it SKIPPED is
    not a failure - it is an unchecked claim, and validate.py's own `trusted` flag already
    accounts for it, so we honour that flag too.
    """
    if validation is None:
        return []
    failed: list[str] = []
    for rec in _iter_records(_get(validation, "benchmarks", "checks", "results", default=None)):
        if not isinstance(rec, Mapping) and not hasattr(rec, "status"):
            continue
        status = str(_get(rec, "status", default="") or "").lower()
        ok = _get(rec, "passed", "ok", default=None)
        if status == "fail" or ok is False:
            failed.append(str(_get(rec, "name", "id", default="(unnamed benchmark)")))
    if failed:
        return failed
    overall = str(_get(validation, "status", default="") or "").lower()
    if overall == "fail":
        failed.append(str(_get(validation, "reason", default="validate.py reported status=fail")))
    elif _get(validation, "trusted", default=None) is False:
        failed.append(
            "validate.py reports trusted=false"
            + (f" (status {overall})" if overall else "")
        )
    elif _get(validation, "passed", "ok", default=None) is False:
        failed.append(str(_get(validation, "reason", default="validation reported failure")))
    return failed


def _mark_untrusted(findings: Sequence[Any], failures: Sequence[str]) -> None:
    if not failures:
        return
    seen: list[str] = []
    for name in failures:
        if name not in seen:
            seen.append(name)
    names = ", ".join(seen[:3]) + (" and more" if len(seen) > 3 else "")
    prefix = f"UNTRUSTED (solver failed benchmark: {names}) - "
    for finding in findings:
        _set_audit(finding, AUDIT_UNTRUSTED, True)
        message = str(_get(finding, "message", default=""))
        if not message.startswith("UNTRUSTED"):
            try:
                setattr(finding, "message", prefix + message)
            except Exception:  # pragma: no cover
                pass


# --- dedup, rank, order ----------------------------------------------------------

# Classes where the METRIC is not part of the identity, because every metric in them is
# a different way of saying the same root cause. A region with no feed-to-vent path makes
# the design check fire AND makes every step that touches it fire; that is one broken
# branch, not several. Other classes keep the metric: a `leak` with no bond strength in
# the contract and a `leak` over the escape margin at the same port are genuinely two
# problems, and fixing one leaves the other.
_METRIC_AGNOSTIC_CLASSES = frozenset({"dead_end"})


def _root_cause(finding: Any) -> tuple:
    """One root cause, one finding: class + chip + feature (+ metric, for most classes).

    The step is deliberately NOT part of the key. module1 exchanges the same chamber 40
    times; that is one over-shear channel, not 40 findings. The worst occurrence survives
    and the message says how many shared it.
    """
    cls = finding_class_of(finding)
    return (
        cls,
        str(_get(finding, "chip_id", default="")),
        str(_get(finding, "feature_id", default="") or ""),
        "" if cls in _METRIC_AGNOSTIC_CLASSES else str(_get(finding, "metric", default="")),
    )


def deduplicate(findings: Sequence[Any]) -> list[Any]:
    best: dict[tuple, Any] = {}
    counts: dict[tuple, int] = defaultdict(int)
    steps: dict[tuple, list] = defaultdict(list)
    for finding in findings:
        key = _root_cause(finding)
        counts[key] += 1
        step = _get(finding, "step", default=None)
        if step is not None:
            steps[key].append(step)
        incumbent = best.get(key)
        if incumbent is None or _worse(finding, incumbent):
            best[key] = finding
    out = []
    for key, finding in best.items():
        if counts[key] > 1:
            labels = sorted({str(s) for s in steps[key]}, key=_natural)
            message = str(_get(finding, "message", default=""))
            if len(labels) > 1:
                shown = ", ".join(labels[:6]) + (" ..." if len(labels) > 6 else "")
                note = (f"same root cause in {counts[key]} places, steps {shown}; "
                        f"the worst is shown")
            else:
                note = (f"{counts[key]} checks found this one root cause; "
                        f"the most serious is shown")
            try:
                setattr(finding, "message", f"{message} ({note})")
            except Exception:  # pragma: no cover
                pass
            if labels:
                _set_audit(finding, AUDIT_STEPS, labels)
        out.append(finding)
    return out


def _natural(label: str) -> tuple:
    """Sort step labels so 2 comes before 10."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", label) if part
    )


def _worse(a: Any, b: Any) -> bool:
    ra = _SEVERITY_RANK.get(str(_get(a, "severity", default="low")), 3)
    rb = _SEVERITY_RANK.get(str(_get(b, "severity", default="low")), 3)
    if ra != rb:
        return ra < rb
    ratio_a = _ratio(_get(a, "value", default=None), _get(a, "limit", default=None)) or 0.0
    ratio_b = _ratio(_get(b, "value", default=None), _get(b, "limit", default=None)) or 0.0
    if ratio_a != ratio_b:
        return ratio_a > ratio_b
    return str(_get(a, "id", default="")) < str(_get(b, "id", default=""))


def _sort_key(finding: Any) -> tuple:
    cls = finding_class_of(finding)
    ratio = _ratio(_get(finding, "value", default=None), _get(finding, "limit", default=None))
    return (
        _SEVERITY_RANK.get(str(_get(finding, "severity", default="low")), 3),
        -(ratio if ratio is not None else 0.0),
        CLASSES.index(cls) if cls in CLASSES else len(CLASSES),
        str(_get(finding, "chip_id", default="")),
        str(_get(finding, "feature_id", default="") or ""),
        str(_get(finding, "step", default="")),
        str(_get(finding, "id", default="")),
    )


def rank(findings: Sequence[Any]) -> list[Any]:
    """Deterministic: severity, then how far over, then class, chip, feature, step, id."""
    return sorted(findings, key=_sort_key)


# --- the entry points ------------------------------------------------------------

def analyse_chip(bundle: Bundle) -> list[Any]:
    """Every finding for ONE chip, deduplicated and ranked."""
    collector = _Collector(bundle.resolved_chip_id())
    dead_regions = check_dead_ends(bundle, collector)
    check_leak(bundle, collector)
    check_bubbles(bundle, collector)
    check_shear(bundle, collector, dead_regions)
    check_traps(bundle, collector, dead_regions)
    check_particles(bundle, collector)
    check_handoffs(bundle, collector)
    check_unmodelled(bundle, collector)
    findings = rank(deduplicate(collector.items))
    _mark_untrusted(findings, validation_failures(bundle.validation))
    return findings


def analyse(bundles: Bundle | Sequence[Bundle]) -> list[Any]:
    """Every finding across the whole pipeline, deduplicated and ranked."""
    many = [bundles] if isinstance(bundles, Bundle) else list(bundles)
    out: list[Any] = []
    for bundle in many:
        collector = _Collector(bundle.resolved_chip_id())
        dead_regions = check_dead_ends(bundle, collector)
        check_leak(bundle, collector)
        check_bubbles(bundle, collector)
        check_shear(bundle, collector, dead_regions)
        check_traps(bundle, collector, dead_regions)
        check_particles(bundle, collector)
        check_handoffs(bundle, collector)
        check_unmodelled(bundle, collector)
        out.extend(collector.items)
    findings = rank(deduplicate(out))
    failures: list[str] = []
    for bundle in many:
        failures.extend(validation_failures(bundle.validation))
    _mark_untrusted(findings, failures)
    return findings


def summary(findings: Sequence[Any]) -> dict:
    by_severity = {name: 0 for name in SEVERITIES}
    by_class: dict[str, int] = defaultdict(int)
    for finding in findings:
        by_severity[str(_get(finding, "severity", default="low"))] += 1
        by_class[finding_class_of(finding)] += 1
    return {
        "total": len(findings),
        "by_severity": by_severity,
        "by_class": dict(sorted(by_class.items())),
        "untrusted": any(finding_untrusted(f) for f in findings),
        "worst": str(_get(findings[0], "severity", default="low")) if findings else None,
    }


def findings_json(findings: Sequence[Any]) -> list[dict]:
    return [finding_to_json(f) for f in findings]


def write_findings(findings: Sequence[Any], path: Path | str) -> dict:
    """Write `cfd/findings.json` and return the summary that goes in the report."""
    payload = {
        "_about": (
            "S10 CFD findings. One finding per real problem. `severity` grading and the "
            "chamber-contents scoping rules are documented in assay2chip/cfd/analyse.py. "
            "`untrusted: true` means validate.py reported a failed benchmark and no number "
            "here may be quoted."
        ),
        "summary": summary(findings),
        "assumptions": {
            key: {"value": a.value, "units": a.units, "why": a.why}
            for key, a in sorted(ASSUMPTIONS.items())
        },
        "findings": findings_json(findings),
    }
    io.write_json(path, payload)  # atomic, via the foundation
    return payload["summary"]

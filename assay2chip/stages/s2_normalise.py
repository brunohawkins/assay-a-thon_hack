"""S2 normalise: assay.json -> unitops.json, the canonical unit operations.

S1 gives ordered wet-lab sentences in lab language. Everything downstream (S3 triage,
S4 partition, S5 group) reasons about *unit operations*, so this stage is where the
vocabulary is fixed. Two things matter more than anything else here:

  1. No step may vanish. check_coverage() fails loudly if one does.
  2. The physics classes (particles, shear_class, viscosity_class) must be rule-based,
     because S3/S4/S5 size geometry and flow from them. A model may help with the
     linguistic judgement (does this sentence contain two actions?); it never decides
     physics, and it never raises a confidence.

Every number comes from `assay2chip.lib.contracts`, every file through
`assay2chip.lib.io`, and the output matches contracts/schemas/unitops.schema.json.

THE VOCABULARY
--------------
Sixteen kinds, and `kind` in contracts/schemas/unitops.schema.json is exactly these
sixteen. The spine first proposed thirteen - merge, mix, incubate, wash, separate, lyse,
thermocycle, split, pool, collect, image, encapsulate, cure - and three operations the
chips already run had nowhere to go, so the contract gained them:

  * `load`    - a one-shot fill of a vessel with a population or a reagent. `merge` is a
                reagent joining a *flowing* stream (designs rule 2); a fill is different
                physics (settling, no mixer, its own inlet port) and `load` is the single
                most used operation in every protocol in protocols/.
  * `culture` - days-to-weeks in medium with periodic batch exchanges. `incubate` is one
                timed hold; the contract has a dedicated `culture` operation with
                exchange_interval_s, and this is the one case where the adherent-cell
                shear ceiling applies. Overloading `incubate` loses both facts.
  * `transfer`- moving a population between vessels, which is what becomes an S4 handoff.
                Neither `merge` nor `collect` covers "move the cells from A to B through
                whatever lies between", and the contract's `transfer` needs source+target.

Every kind is emitted as it is. There is no nearest-kind substitution: check_vocabulary()
reads the enum out of the schema and raises if a kind is not in it, because a kind that is
merely close is the plausible-but-wrong value this pipeline exists to avoid.

Deliberately NOT in the vocabulary: `valve`. Valve actuation is never a lab step; it is
plumbing S5 and operator/planner.py add. It is the only protocol operation no kind reaches.

PLACEMENT AND CONFIDENCE
------------------------
Every op leaves here as `placement.where: "pending"` - the contract's explicit "no
decision". S2 does not triage: S3 must replace every pending one, and S4 onward refuses
any that survive. `placement.part`, when S3 fills it, is a contracts/blocks.json block id
(the same vocabulary as a chip spec's `block`), with `catalogue_part` for display only.

`confidence` is an op-level field and is the LOWEST confidence of the steps the op came
from. Merging steps never raises it (_absorb), so an op is never more certain than its
source; anything below 0.5 also lands in `unresolved`.

MAPPING ONTO contracts/schemas/protocol.schema.json
---------------------------------------------------
S5 has to emit a design file against a fixed protocol contract, so every kind maps onto
that enum. check_vocabulary() reads both enums out of the schemas and refuses to run if a
mapping drifts off either; op `audit.protocol` carries the chosen operation forward.
Two kinds have no operation of their own and come out `audit.protocol.approximate`:

  * `separate` -> `exchange`. A centrifuge wash is a chamber flush or a spiral (designs
    rule 3), i.e. an exchange on a retained population. A *size-sorting* separation
    (weir, DLD, pillar filter) has no protocol operation at all: it is part geometry with
    flow through it, so S3/S5 should expect missing_part, not a step.
  * `lyse` -> `exchange` (+ `hold_temp` for the dwell), exactly the idiom in
    protocols/module3_v1.json steps 2-3 and module4_v1.json.

An op with `audit.protocol.unmappable` has no protocol step at all (a size-sorting
separation, or imaging that is not a count): S3 should read that as off_chip/missing_part.

CLASSIFYING SHEAR
-----------------
contracts/parameters.json is explicit that this has been got wrong before (the notes on
shear.culture_ceiling_dyn_cm2 and shear.prelysis_rinse_ceiling_dyn_cm2) and
contracts.shear_ceiling_for() holds the three ceilings. Rules, first match wins:

  1. capsules present                -> "capsules"          bounded by pressure, not shear
  2. cells, rinse right before lyse  -> "prelysis_rinse"    0.3 dyn/cm2
  3. cells, attached AND alive       -> "adherent_cells"    0.01 dyn/cm2
  4. anything else                   -> "none"

Rule 1 outranks rule 3 on purpose: cells inside a capsule are not attached to anything,
the shell takes the shear, and the limit is escape pressure x margin. The audit found the
culture ceiling applied to capsule chambers in six places, which only made them slow.
Rule 2 outranks rule 3 on purpose: cells about to be deliberately killed do not need a
12-minute rinse to stay attached, and that rinse destroyed the metabolites it existed to
preserve. A deliberate dissociation is not held to a don't-detach ceiling either.
"Attached" is not guessed from adjectives: it is inferred from the assay itself, because a
later enzymatic dissociation of the same target proves the cells were attached until then
(_attachment_map).

WHAT THE CHAMBER MUST HOLD (`load` and `ratio`)
-----------------------------------------------
assay2chip/README.md, "Bench volumes do not transfer": a paper's "wash in 500 uL"
describes a TUBE. The first end-to-end run failed at S6 because a 500 uL tube became a
500 uL chamber and a 0.012 uL step was refused as unbuildable. So `fluid.volume_ul` stays
as the paper's number FOR TRACEABILITY ONLY, and what S6 sizes from is `load`:

  adherent_area     cells attached to the floor. area_mm2 = cells / surface density. The
                    paper's seeding density when it states one, else
                    culture.surface_density_per_mm2 (an assumption in the contract, so it
                    is flagged in `unresolved` and `load.source` says contract_default).
  packed_volume     capsules or beads in a bed. count x (pi/6) d^3 / capsule.packing_fraction,
                    with the SWOLLEN diameter once they have been cultured, because that
                    is the bed the chamber has to hold.
  suspension_volume cells held in suspension at a density: cells / density per uL.
  flow_through      nothing is held - a mixer, a junction, a nozzle, or an incubation in
                    flow (designs rule 4). Sized from flow and residence_s, not volume.
  none              nothing to hold.

`ratio` is for a reagent ADDED to a sample (lysis buffer, enzyme, master mix, label), so
the concentration on chip matches the paper whatever the chamber turns out to be:
reagent_to_sample = the paper's reagent volume / the sample volume it went into (tracked
per vessel). A wash or rinse gets NO ratio - it becomes a sweep of
-ln(1 - exchange.target_fraction) chamber volumes, recorded in `audit.load`.

A count, density or sample volume the paper never states is null plus an `unresolved`
question; `load.source` is paper, derived or contract_default, honestly, every time.

NUMBERS
-------
Capsule sizes come from contracts.capsule_diameter_um() /
swollen_capsule_diameter_um(); capsules swell in culture, so `swollen_diameter_um` is set
from the first culture op onward, which is why culture.chamber_height_min_um is 150 um and
not 70. Viscosity comes from contracts.viscosity_for(temp_c). The contracts hold no cell
diameter, no bead diameter and no viscosity for a viscous, organic or oil phase: those
come out null with an entry in `unresolved` rather than as an invented number.

Entry points: `run(**kwargs)` (the pipeline's), normalise(assay), check_coverage(), and

    python assay2chip/stages/s2_normalise.py --id my_assay
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts, llm                      # noqa: E402
from assay2chip.lib.io import (                                # noqa: E402
    SCHEMA_DIR,
    StageFileMissing,
    load_json,
    read_stage,
    write_json,
    write_stage,
)

STAGE = "s2_normalise"
# Nothing outside assay2chip/work/<id>/: S2 writes unitops.json and nothing else.
WRITES: list[str] = []

# --------------------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------------------

KINDS: tuple[str, ...] = (
    "load",
    "merge",
    "mix",
    "incubate",
    "culture",
    "wash",
    "separate",
    "lyse",
    "thermocycle",
    "split",
    "pool",
    "transfer",
    "collect",
    "image",
    "encapsulate",
    "cure",
)

# The three kinds added to the spine's thirteen, and why.
KINDS_ADDED_TO_SPINE: dict[str, str] = {
    "load": "a one-shot fill of a vessel; `merge` is a reagent joining a flowing stream, "
            "and without `load` the protocol contract's `load` - the commonest operation "
            "in protocols/ - is unreachable",
    "culture": "days-weeks with periodic medium exchange; the protocol contract has a "
               "dedicated `culture` operation with exchange_interval_s, and this is the "
               "only case where the adherent-cell shear ceiling applies",
    "transfer": "moving a population between vessels, which becomes an S4 handoff; the "
                "contract's `transfer` (source + target) had no kind mapping to it",
}
KINDS_NOT_IN_VOCABULARY: dict[str, str] = {
    "valve": "valve actuation is never a lab step; S5 and operator/planner.py add it as "
             "plumbing. It is the only protocol operation no kind reaches.",
}


@dataclass(frozen=True)
class ProtocolMapping:
    """How one canonical kind becomes steps in contracts/schemas/protocol.schema.json."""

    operation: str                      # the operation S5 emits by default
    alternates: tuple[str, ...] = ()    # other operations this kind can become, by rule
    also: tuple[str, ...] = ()          # companion steps S5 must emit alongside
    approximate: bool = False           # the contract has no operation for this kind
    rule: str = ""                      # when an alternate is used instead

    def to_dict(self) -> dict:
        return {
            "operation": self.operation,
            "alternates": list(self.alternates),
            "also": list(self.also),
            "approximate": self.approximate,
            "rule": self.rule,
        }


KIND_TO_PROTOCOL: dict[str, ProtocolMapping] = {
    "load": ProtocolMapping(
        "load",
        rule="a fill with nothing retained upstream; keeps volume_ul, no flow rate",
    ),
    "merge": ProtocolMapping(
        "mix",
        alternates=("load",),
        rule="mix (reagent into the flowing stream at a junction, designs rule 2); load "
             "when the target vessel is being filled rather than joined",
    ),
    "mix": ProtocolMapping(
        "mix",
        rule="the mixer half of designs rule 2 (serpentine_channel, or snake_channel when "
             "viscosity_class is viscous)",
    ),
    "incubate": ProtocolMapping(
        "hold_temp",
        rule="one timed hold; in flow it is a delay line instead (designs rule 4), which "
             "is still hold_temp in the protocol",
    ),
    "culture": ProtocolMapping(
        "culture",
        alternates=("exchange", "hold_temp"),
        rule="culture with exchange_interval_s when the chip has culture lanes; the older "
             "alternating exchange/hold_temp spelling (protocols/ipsc_to_mgl_v1.json) when "
             "it does not",
    ),
    "wash": ProtocolMapping(
        "exchange",
        rule="washes = repeat count; direction=backflush when capsules must come off a mesh",
    ),
    "separate": ProtocolMapping(
        "exchange",
        alternates=("transfer", "collect"),
        approximate=True,
        rule="exchange for a centrifuge-wash equivalent (designs rule 3); collect when the "
             "liquid phase is the product. A size-sorting separation (weir/DLD/filter) has "
             "NO protocol operation: it is part geometry, so expect missing_part",
    ),
    "lyse": ProtocolMapping(
        "exchange",
        also=("hold_temp",),
        approximate=True,
        rule="deliver lysis buffer as an exchange, then hold for the dwell "
             "(protocols/module3_v1.json steps 2-3). The contract has no `lyse`",
    ),
    "thermocycle": ProtocolMapping(
        "hold_temp",
        rule="hold_temp with `cycles` (count, ramp_c_s, profile); the schema allows "
             "`cycles` on hold_temp only",
    ),
    "split": ProtocolMapping(
        "split",
        rule="designs rule 5: flow_manifold / flow_distributor / fork_junction",
    ),
    "pool": ProtocolMapping("pool", rule="designs rule 5: ring_junction"),
    "transfer": ProtocolMapping(
        "transfer",
        rule="needs source and target; off chip it is an S4 handoff with automated=false",
    ),
    "collect": ProtocolMapping(
        "collect",
        alternates=("harvest", "release"),
        rule="collect when the liquid phase leaves and the particles stay; harvest when "
             "the cells leave; release when a retained bed (capsules on a mesh) must be "
             "freed, which operator/safety.py requires a back-flush before",
    ),
    "image": ProtocolMapping(
        "count",
        rule="count when the readout is cell count / viability at a QC window; any other "
             "imaging has no protocol operation at all and is off_chip by designs rule 6",
    ),
    "encapsulate": ProtocolMapping(
        "encapsulate",
        rule="needs flows_ul_h (core/shell/oil) and capsules",
    ),
    "cure": ProtocolMapping("cure", rule="needs wavelength_nm (crosslink.wavelength_nm)"),
}


class VocabularyError(RuntimeError):
    """A kind does not fit the protocol contract or the unitops schema."""


class CoverageError(RuntimeError):
    """An assay step reached no unit operation. The worst outcome, so it raises."""


def _enum(schema_file: str, *path: str) -> tuple[str, ...]:
    """An enum read out of a contract schema, so no vocabulary is restated in code."""
    node: Any = load_json(SCHEMA_DIR / schema_file)
    for part in path:
        node = node[part]
    return tuple(node)


def protocol_operations() -> tuple[str, ...]:
    return _enum("protocol.schema.json", "$defs", "step", "properties", "operation", "enum")


def schema_kinds() -> tuple[str, ...]:
    return _enum("unitops.schema.json", "properties", "ops", "items", "properties",
                 "kind", "enum")


def check_vocabulary() -> dict:
    """Fail unless every kind fits BOTH contracts. Called by normalise().

    The kinds are emitted as they are: no nearest-kind substitution, because a kind that
    is merely close is exactly the plausible-but-wrong value this pipeline exists to
    avoid. If a kind is not in the schema's enum, this raises instead.
    """
    operations = set(protocol_operations())
    allowed_kinds = set(schema_kinds())
    bad: list[str] = []
    used: set[str] = set()

    for kind in KINDS:
        mapping = KIND_TO_PROTOCOL.get(kind)
        if mapping is None:
            bad.append(f"kind {kind} has no protocol mapping")
            continue
        for operation in (mapping.operation, *mapping.alternates, *mapping.also):
            used.add(operation)
            if operation not in operations:
                bad.append(f"{kind} -> protocol operation {operation!r} does not exist")
        if kind not in allowed_kinds:
            bad.append(
                f"kind {kind!r} is not in the unitops schema's enum "
                f"({', '.join(sorted(allowed_kinds))}): add it to the contract rather than "
                f"emitting a near-miss kind"
            )
    if bad:
        raise VocabularyError("vocabulary does not fit the contracts: " + "; ".join(bad))

    return {
        "kinds": list(KINDS),
        "added_to_spine": dict(KINDS_ADDED_TO_SPINE),
        "not_in_vocabulary": dict(KINDS_NOT_IN_VOCABULARY),
        "schema_kind_enum": list(schema_kinds()),
        "kind_to_protocol": {k: KIND_TO_PROTOCOL[k].to_dict() for k in KINDS},
        "protocol_operations_unused": sorted(operations - used),
    }


# --------------------------------------------------------------------------------------
# lexicons (language, not contract values: literals belong here)
# --------------------------------------------------------------------------------------

_OIL = ("oil", "hfe", "novec", "fc-40", "fc40", "fluorinated", "pico-surf", "picosurf",
        "span 80", "abil em", "squalane")
_ORGANIC = ("ethanol", "methanol", "isopropanol", "isopropyl", "acetone", "chloroform",
            "phenol", "trizol", "tri reagent", "acetonitrile", "xylene", "hexane",
            "dmso", "formamide", "dmf")
_VISCOUS = ("dextran", "glycerol", "gelma", "gelatin", "alginate", "agarose", "matrigel",
            "methylcellulose", "peg", "polyethylene glycol", "ficoll", "optiprep",
            "iodixanol", "collagen", "hydrogel", "viscous", "sucrose cushion", "glycerin")
_DISSOCIATION = ("trypsin", "accutase", "tryple", "collagenase", "dispase", "papain",
                 "liberase", "dissociation", "versene")
_LYTIC = ("lysis", "lyse", "guanidin", "gitc", "ripa", "triton", "sds", "nonidet",
          "proteinase k")
_FIXATIVE = ("formaldehyde", "paraformaldehyde", "pfa", "glutaraldehyde")
_BEADS = ("bead", "dynabead", "ampure", "resin", "magnetic particle", "microsphere")
_CELLS = ("cell", "ipsc", "psc", "spheroid", "organoid", "monocyte", "pbmc", "nuclei",
          "nucleus")
_CAPSULES = ("capsule", "droplet", "microgel", "emulsion")

_CUE = {
    "mix": ("mix", "vortex", "triturate", "pipette up and down", "pipetting up and down",
            "invert", "agitate", "resuspend by pipetting"),
    "incubate": ("incubate", "incubation", "leave for", "stand for", "rest for", "dwell",
                 "overnight", "attach", "let them settle", "allow to settle"),
    "separate": ("centrifuge", "spin ", "spun", "pellet", " g for", "magnetic separation",
                 "magnetic rack", "on the magnet", "filter", "strain", "sort", "weir",
                 "dld"),
    # Narrower: enough to add a SECOND op to a step that already does something else.
    # "magnetic beads" in a reagent name must not conjure a magnetic separation.
    "separate_action": ("centrifuge", "spin ", "spun", "pellet", " g for", "magnetic rack",
                        "on the magnet", "magnetic separation"),
    "wash": ("wash", "rinse", "medium exchange", "exchange the medium",
             "change the medium", "replace the medium"),
    "collect": ("collect", "harvest", "recover", "elute", "take the supernatant",
                "transfer the supernatant", "aspirate the supernatant"),
    "resuspend": ("resuspend in", "resuspended in", "resuspend the pellet", "take up in",
                  "reconstitute in"),
    "culture": ("cultur", "expansion", "differentiat", "passage"),
    "fed": ("feed", "fed", "daily", "every", "medium exchange", "exchanging",
            "change the medium", "passage", "refresh", "replace the medium"),
    "lyse": _LYTIC,
    "thermocycle": ("thermocycl", "cycles of", "pcr", "denature", "annealing", "cycling",
                    "cycles between"),
    "encapsulate": ("encapsulat", "flow-focus", "flow focus", "nozzle", "emulsif",
                    "droplet generat", "co-flow"),
    "cure": ("cure", "crosslink", "cross-link", "photo-cross", "uv ", "405"),
    "split": ("split", "aliquot", "divide", "distribute into", "sublibrar", "into wells"),
    "pool": ("pool", "combine", "recombine"),
    "load": ("seed", "plate the", "load", "fill the", "introduce into"),
    "image": ("image", "imaging", "microscop", "photograph", "brightfield", "fluorescen"),
    "count": ("count", "haemocytometer", "hemocytometer", "viability", "trypan"),
    "isothermal": ("isothermal", "reverse transcription", " rt ", "ivt",
                   "in vitro transcription", "ligation"),
    "long": ("day", "days", "week", "weeks"),
    "adherent": ("adherent", "monolayer", "attach", "coating", "coated", "flask",
                 "well plate", "confluen"),
    "suspension": ("suspension culture", "in suspension", "non-adherent", "suspension cells"),
}

_REPEAT_WORDS = {"once": 1, "twice": 2, "two times": 2, "three times": 3, "four times": 4,
                 "five times": 5}

_ACTION_KIND = {
    "add": "merge",
    "seed": "load",
    "incubate": "incubate",
    "wash": "wash",
    "centrifuge": "separate",
    "separate": "separate",
    "mix": "mix",
    "transfer": "transfer",
    "image": "image",
    "lyse": "lyse",
    "amplify": "thermocycle",
    "harvest": "collect",
}

# S1's action is extracted data; the cue tables are heuristics. A cue may only override
# the action when the action is vague about which operation it is. Without this, "wash the
# capsule bed BEFORE LYSIS" reads as a lyse - and that is the rinse the audit found held
# to the wrong shear ceiling.
_VAGUE_ACTIONS = {"", "add", "incubate", "transfer", "separate"}


def _has(text: str, needles: Iterable[str]) -> bool:
    """Prefix-at-a-word-boundary match, so 'abil em' cannot hide inside 'viability'."""
    return any(re.search(r"(?<![a-z0-9])" + re.escape(n), text) for n in needles)


def _blob(step: dict) -> str:
    """Everything the step says, lowercased: used for the physics cues."""
    parts = [str(step.get("text") or ""), str(step.get("notes") or ""),
             str(step.get("note") or ""), str(step.get("target") or ""),
             str(step.get("action") or "")]
    for reagent in step.get("reagents") or []:
        parts.append(str(reagent.get("name") or ""))
        parts.append(str(reagent.get("role") or ""))
    return " " + " ".join(parts).lower() + " "


def _kind_blob(step: dict) -> str:
    """What the step says, without the target id: used to choose the kind.

    A target called culture_chamber must not make every step in it a culture.
    """
    parts = [str(step.get("text") or ""), str(step.get("notes") or ""),
             str(step.get("note") or "")]
    for reagent in step.get("reagents") or []:
        parts.append(str(reagent.get("name") or ""))
        parts.append(str(reagent.get("role") or ""))
    return " " + " ".join(parts).lower() + " "


def _prose(step: dict) -> str:
    """The sentence only, for the places a reagent name would otherwise decide the kind."""
    return " " + f"{step.get('text') or ''} {step.get('notes') or ''}".lower() + " "


def _repeats(text: str) -> int:
    match = re.search(r"(\d+)\s*(?:x\b|times)", text)
    if match:
        return max(1, int(match.group(1)))
    for word, count in _REPEAT_WORDS.items():
        if word in text:
            return count
    return 1


# --------------------------------------------------------------------------------------
# atoms: one lab step -> one or more candidate operations (one-to-many)
# --------------------------------------------------------------------------------------

@dataclass
class Atom:
    kind: str
    steps: list[int]
    target: str
    reagents: list[dict]
    duration_s: float | None
    temp_c: float | None
    confidence: float
    why: str
    repeats: int = 1
    cues: dict = field(default_factory=dict)

    @property
    def reagent_names(self) -> tuple[str, ...]:
        return tuple(sorted(str(r.get("name") or "") for r in self.reagents))


def _primary_kind(step: dict, text: str, prose: str) -> tuple[str, str]:
    """The one kind a step is mostly about. S1's action wins unless it is vague."""
    action = str(step.get("action") or "").strip().lower()
    base = _ACTION_KIND.get(action)

    if base and action not in _VAGUE_ACTIONS:
        if action == "amplify":
            if _has(text, _CUE["thermocycle"]) and not _has(text, _CUE["isothermal"]):
                return "thermocycle", "amplification with temperature cycling"
            return "incubate", "isothermal amplification: one hold, not a cycle"
        return base, f"S1 action {action!r}"

    # Cues specific enough to beat a vague action.
    if _has(text, _CUE["encapsulate"]):
        return "encapsulate", "encapsulation cue in the step"
    if _has(text, _CUE["cure"]):
        return "cure", "curing / crosslinking cue"
    if _has(text, _CUE["lyse"]):
        return "lyse", "lysis reagent or lysis cue"
    if _has(text, _CUE["thermocycle"]) and not _has(text, _CUE["isothermal"]):
        return "thermocycle", "repeated temperature cycling cue"
    if _has(text, _CUE["split"]) and base != "mix":
        return "split", "split / aliquot cue"
    if _has(text, _CUE["pool"]):
        return "pool", "pool / combine cue"
    if _is_culture(step, text):
        return "culture", "a long, fed run in medium"

    # Refinements of a vague action, from the sentence only.
    if base:
        if action == "transfer" and _has(prose, _CUE["collect"]):
            return "collect", "S1 action 'transfer', but the sentence takes a product out"
        if action == "add" and _has(prose, _CUE["load"]):
            return "load", "S1 action 'add', but the sentence fills a vessel"
        if action in ("add", "incubate") and _has(prose, _CUE["wash"]):
            return "wash", f"S1 action {action!r}, but the sentence washes / exchanges"
        return base, f"S1 action {action!r}"

    if _has(text, _CUE["wash"]):
        return "wash", "wash / rinse cue"
    if _has(text, _CUE["separate"]):
        return "separate", "centrifuge / filter / sort cue"
    if _has(text, _CUE["collect"]):
        return "collect", "collect / harvest cue"
    if _has(text, _CUE["image"]) or _has(text, _CUE["count"]):
        return "image", "readout cue"
    if _has(text, _CUE["load"]):
        return "load", "seed / fill cue"
    if _has(text, _CUE["incubate"]):
        return "incubate", "timed hold cue"
    return "transfer", "no stronger cue: read as moving material, and flagged"


def _is_culture(step: dict, text: str) -> bool:
    """`culture`, not `incubate`: a long run in medium that is FED.

    All three are required, because the protocol contract's culture operation is defined by
    exchange_interval_s. A long unfed hold is an incubate (a sealed IVT, ivt.time_h 16-19 h,
    is not a culture); a seeding that merely mentions a culture chamber is a load.
    """
    if not _has(text, _CUE["culture"]):
        return False
    duration = step.get("duration_s")
    long_run = isinstance(duration, (int, float)) and duration >= 12 * 3600
    return bool((long_run or _has(text, _CUE["long"])) and _has(text, _CUE["fed"]))


def _atoms_for_step(step: dict, model_enabled: bool = False) -> list[Atom]:
    """One step -> one or more atoms. Many-to-one happens later, in _merge_atoms()."""
    text = _kind_blob(step)
    prose = _prose(step)
    number = int(step["step"])
    target = str(step.get("target") or "").strip() or "unspecified"
    confidence = _confidence(step)
    kind, why = _primary_kind(step, text, prose)
    duration = step.get("duration_s")
    temp = step.get("temp_c")
    repeats = _repeats(text)

    # Dissociation is the headline many-to-one: "add enzyme", "incubate", "pipette to
    # detach" is ONE separate op, so the enzyme and its dwell stay together here.
    if kind in ("merge", "load", "incubate", "separate", "mix") and _has(text, _DISSOCIATION):
        return [Atom("separate", [number], target, list(step.get("reagents") or []),
                     duration, temp, confidence,
                     "enzymatic dissociation: the reagent and its dwell are one op",
                     repeats, {"dissociation": True})]

    primary = Atom(kind, [number], target, list(step.get("reagents") or []), duration, temp,
                   confidence, why, repeats)
    before: list[Atom] = []
    after: list[Atom] = []

    # One-to-many: a sentence that does two things. Cue-based and deterministic; a model
    # may only ADD splits, never touch the physics (see _ask_model).
    extra = _split_cues(kind, text)
    if model_enabled:
        extra = _ask_model(step, kind, extra)

    for sub_kind, sub_why, takes_time, takes_reagents, position in extra:
        if sub_kind == kind:
            continue
        atom = Atom(sub_kind, [number], target,
                    list(step.get("reagents") or []) if takes_reagents else [],
                    duration if takes_time else None,
                    temp if takes_time else None,
                    confidence, sub_why, 1, {"from_one_to_many": True})
        (before if position == "before" else after).append(atom)
        if takes_time:
            # The source stated ONE duration, so it moves rather than being copied:
            # copying it would double the time the chip spends on this step.
            primary.duration_s = None
            primary.cues["duration_moved_to"] = sub_kind

    return [*before, primary, *after]


def _split_cues(kind: str, text: str) -> list[tuple[str, str, bool, bool, str]]:
    """(kind, why, takes_the_duration, takes_the_reagents, before|after) inside one step."""
    out: list[tuple[str, str, bool, bool, str]] = []
    if kind in ("collect", "transfer") and _has(text, _CUE["pool"]):
        out.append(("pool", "the step pools the fractions first (designs rule 5)",
                    False, False, "before"))
    if kind in ("merge", "load", "wash", "separate", "lyse", "transfer", "pool") \
            and _has(text, _CUE["mix"]):
        out.append(("mix", "the step also says to mix: merge then mixer (designs rule 2)",
                    False, False, "after"))
    if kind in ("merge", "load", "lyse", "cure", "encapsulate", "split") \
            and _has(text, _CUE["incubate"]):
        out.append(("incubate", "the step also states a timed hold", True, False, "after"))
    if kind in ("merge", "wash", "collect", "transfer") and _has(text, _CUE["separate_action"]):
        out.append(("separate", "the step also centrifuges / separates", False, False, "after"))
    if kind == "separate" and _has(text, _CUE["resuspend"]):
        out.append(("merge", "the step also resuspends in fresh liquid", False, True, "after"))
    if kind in ("separate", "wash") and _has(text, _CUE["collect"]):
        out.append(("collect", "the step also takes the liquid phase out", False, False, "after"))
    if kind in ("collect", "transfer", "separate") and _has(text, _CUE["count"]):
        out.append(("image", "the step also reads a count or viability", False, False, "after"))
    return out


def _ask_model(step: dict, kind: str, deterministic: list) -> list:
    """Optional, linguistic only: may ADD splits the cue table missed.

    The model may propose kinds; it may not touch particles, shear_class, viscosity_class,
    durations or confidence, and anything off-vocabulary is dropped. In stub mode
    llm.complete_json returns the deterministic answer unchanged, so the offline path is
    exactly the rule-based one.
    """
    prompt = (
        "A wet-lab sentence may describe more than one physical operation.\n"
        f"Vocabulary: {', '.join(KINDS)}.\n"
        f"Already found: {', '.join([kind] + [k for k, *_ in deterministic])}.\n"
        "Reply with JSON {\"extra\": [{\"kind\": ..., \"why\": ...}]} listing only "
        "operations present in the sentence and not already found. Do not comment on "
        "volumes, temperatures, particles or shear.\n"
        f"Sentence: {step.get('text')!r}"
    )
    answer = llm.complete_json(prompt, task="s2_split_step", stub={"extra": []})
    if not isinstance(answer, dict):
        return deterministic
    found = {k for k, *_ in deterministic} | {kind}
    out = list(deterministic)
    for item in answer.get("extra") or []:
        sub = str((item or {}).get("kind") or "")
        if sub in KINDS and sub not in found:
            found.add(sub)
            why = str((item or {}).get("why") or "second action in the sentence")[:120]
            out.append((sub, f"model: {why}", False, False, "after"))
    return out


def _confidence(step: dict) -> float:
    value = step.get("confidence")
    return float(value) if isinstance(value, (int, float)) else 0.0


# --------------------------------------------------------------------------------------
# many-to-one: adjacent atoms that are really one operation
# --------------------------------------------------------------------------------------

# An incubation right after one of these, on the same target, is that op's dwell.
_FOLD_INTO = {"lyse", "separate", "cure", "encapsulate", "culture", "wash"}
# Repeats of the same kind on the same target with the same reagents collapse.
_RUN_MERGEABLE = {"wash", "culture", "load", "collect", "mix", "image", "separate",
                  "incubate", "merge"}


def _temps_compatible(a: float | None, b: float | None) -> bool:
    return a is None or b is None or a == b


def _merge_atoms(atoms: list[Atom]) -> list[Atom]:
    out: list[Atom] = []
    for atom in atoms:
        previous = out[-1] if out else None
        if previous is not None and previous.target == atom.target and _mergeable(previous, atom):
            _absorb(previous, atom)
            continue
        out.append(atom)
    return out


def _mergeable(previous: Atom, atom: Atom) -> bool:
    if atom.cues.get("from_one_to_many") or previous.cues.get("from_one_to_many"):
        return False
    if atom.kind == "incubate" and previous.kind in _FOLD_INTO:
        return _temps_compatible(previous.temp_c, atom.temp_c)
    if previous.kind == atom.kind and atom.kind in _RUN_MERGEABLE:
        same_reagents = (previous.reagent_names == atom.reagent_names
                         or not previous.reagent_names or not atom.reagent_names)
        return same_reagents and _temps_compatible(previous.temp_c, atom.temp_c)
    return False


def _absorb(previous: Atom, atom: Atom) -> None:
    for number in atom.steps:
        if number not in previous.steps:
            previous.steps.append(number)
    previous.duration_s = _sum(previous.duration_s, atom.duration_s)
    if previous.temp_c is None:
        previous.temp_c = atom.temp_c
    for reagent in atom.reagents:
        if reagent not in previous.reagents:
            previous.reagents.append(reagent)
    if previous.kind == atom.kind:
        previous.repeats += atom.repeats
    # Never upgrade certainty by aggregating (assay2chip/README.md).
    previous.confidence = min(previous.confidence, atom.confidence)
    previous.cues.setdefault("merged", []).append(
        f"step {atom.steps[0]}: {atom.why}" if atom.steps else atom.why
    )


def _sum(a: float | None, b: float | None) -> float | None:
    if a is None:
        return b
    if b is None:
        return a
    return a + b


# --------------------------------------------------------------------------------------
# physics: particles, shear, fluid - every number through assay2chip.lib.contracts
# --------------------------------------------------------------------------------------

class Gaps:
    """Values this stage needed that no source and no contract provides.

    Deduplicated, so a missing cell diameter is one `unresolved` entry rather than one per
    op, and each entry names the ops it affects.
    """

    def __init__(self) -> None:
        self.entries: dict[str, dict] = {}
        self.ops: dict[str, list[int]] = {}

    def add(self, field_name: str, question: str, op: int | None = None,
            assumed: Any = None, assumed_from: str | None = None,
            dedupe: str | None = None) -> None:
        key = dedupe or f"{field_name}|{assumed_from or ''}"
        if key not in self.entries:
            entry = {"field": field_name, "question": question}
            if assumed is not None:
                entry["assumed"] = assumed
            if assumed_from:
                entry["assumed_from"] = assumed_from
            self.entries[key] = entry
            self.ops[key] = []
        if op is not None and op not in self.ops[key]:
            self.ops[key].append(op)

    def unresolved(self) -> list[dict]:
        out = []
        for key, entry in self.entries.items():
            item = dict(entry)
            ops = self.ops[key]
            if ops:
                item["op"] = ops[0]
                if len(ops) > 1:
                    item["note"] = (f"the same gap affects ops "
                                    f"{', '.join(str(o) for o in ops)}")
            out.append(item)
        return out


def _contract_value(key: str) -> tuple[Any, str | None]:
    """(value, None) or (None, why) - a contract gap is reported, never defaulted."""
    try:
        return contracts.param(key), None
    except contracts.ContractError as e:
        return None, str(e)


def _attachment_map(atoms: list[Atom], steps_by_number: dict[int, dict]) -> dict[str, int | None]:
    """Per target, the atom index at which cells stop being attached.

    Attachment is not read off adjectives. A later enzymatic dissociation of the same
    target is what proves the cells were attached until then; an assay that never detaches
    anything was never adherent, and cells in suspension cannot be detached by shear.
    """
    detach: dict[str, int | None] = {}
    seeded: dict[str, bool] = {}
    for index, atom in enumerate(atoms):
        text = " ".join(_blob(steps_by_number[n]) for n in atom.steps if n in steps_by_number)
        if atom.kind == "load" and _has(text, _CELLS):
            seeded.setdefault(atom.target, True)
        if _has(text, _CUE["adherent"]):
            seeded[atom.target] = True
        if atom.target in detach:
            continue
        if atom.cues.get("dissociation"):
            detach[atom.target] = index
        elif _has(text, _CUE["suspension"]):
            detach[atom.target] = -1              # never attached
    for target in list(seeded):
        detach.setdefault(target, None)           # attached, never released in this assay
    return detach


def _particle_walk(atoms: list[Atom], steps_by_number: dict[int, dict]) -> list[dict]:
    """Particle inventory per op, walked forward so the scoping is explicit."""
    detach = _attachment_map(atoms, steps_by_number)
    state: dict[str, dict] = {}
    cultured_capsules = False
    out: list[dict] = []

    for index, atom in enumerate(atoms):
        text = " ".join(_blob(steps_by_number[n]) for n in atom.steps if n in steps_by_number)
        here = state.setdefault(atom.target, {"kind": "none", "alive": True})

        if atom.kind == "encapsulate" or _has(text, _CAPSULES):
            here["kind"] = "capsules"
        elif _has(text, _CELLS) and here["kind"] in ("none", "cells", "beads"):
            # Cells win over beads in a mixed stream: they are what the shear rules care
            # about. The beads are recorded in the op's audit.
            here["kind"] = "cells"
        elif _has(text, _BEADS) and here["kind"] == "none":
            here["kind"] = "beads"

        if atom.kind == "lyse" or _has(text, _LYTIC) or _has(text, _FIXATIVE):
            here["alive"] = False
        if atom.kind == "culture" and here["kind"] == "capsules":
            cultured_capsules = True

        # The population travels: an op on a fresh vessel that is not a fill inherits what
        # the previous op was working on, so a chamber downstream of an encapsulation holds
        # capsules even when its own sentence never says the word.
        inherited = False
        if here["kind"] == "none" and atom.kind != "load" and out:
            previous = out[-1]
            if previous["kind"] != "none":
                here["kind"] = previous["kind"]
                here["alive"] = previous["alive"]
                inherited = True
                here["inherited"] = True

        attached = False
        if here["kind"] == "cells" and here["alive"] and atom.target in detach \
                and not here.get("inherited"):
            cut = detach[atom.target]
            if cut is None:
                attached = True                   # attached for the whole assay
            elif cut >= 0:
                # Strictly BEFORE the dissociation: that op exists to detach them, so
                # holding it to a don't-detach ceiling would be self-defeating.
                attached = index < cut

        out.append({
            "kind": here["kind"],
            "alive": here["alive"],
            "attached": attached,
            "dissociating": bool(atom.cues.get("dissociation")),
            "capsule_swollen": here["kind"] == "capsules" and cultured_capsules,
            "inherited": inherited,
            "beads_present": _has(text, _BEADS),
        })
    return out


def _particles(info: dict, op: int, gaps: Gaps) -> tuple[dict, dict]:
    """The op's `particles` object plus its derivation. Never invents a diameter."""
    kind = info["kind"]
    audit: dict = {
        "state": {k: info[k] for k in ("alive", "attached", "dissociating",
                                       "capsule_swollen", "inherited")},
    }
    if info["beads_present"] and kind != "beads":
        audit["also_present"] = "beads"

    if kind == "none":
        return {"kind": "none"}, audit

    if kind == "capsules":
        diameter = contracts.capsule_diameter_um()
        swollen = contracts.swollen_capsule_diameter_um()
        particles: dict = {"kind": "capsules", "diameter_um": diameter}
        audit["diameter_from"] = "capsule.diameter_um"
        audit["swell_factor_from"] = "capsule.swell_factor"
        gaps.add("particles.diameter_um",
                 "the source does not state a capsule diameter; the contract's value is used",
                 op, assumed=diameter, assumed_from="capsule.diameter_um")
        if info["capsule_swollen"]:
            particles["swollen_diameter_um"] = swollen
            particles["note"] = ("cultured: occupied capsules swell, so channels, traps and "
                                 "ports here must pass the swollen diameter")
            audit["swollen_applies"] = True
        else:
            audit["swollen_applies"] = False
        return particles, audit

    if kind == "cells":
        value, why = _contract_value("cell.diameter_um")
        if value is not None:
            audit["diameter_from"] = "cell.diameter_um"
            gaps.add("particles.diameter_um",
                     "the source does not state a cell diameter; the contract's value is used",
                     op, assumed=value, assumed_from="cell.diameter_um")
            return {"kind": "cells", "diameter_um": value}, audit
        gaps.add("particles.diameter_um",
                 "no cell diameter: the source does not give one and " + why
                 + " cell_filter.gap_um (a filter gap) and cell_wash.dld_critical_diameter_um "
                   "(a DLD cut point) are NOT cell sizes, so this stays null rather than "
                   "being invented", op)
        audit["diameter_from"] = None
        audit["nearest_contract_handles"] = ["cell_filter.gap_um",
                                             "cell_wash.dld_critical_diameter_um",
                                             "dissociation.outlet_min_width_um"]
        return {"kind": "cells", "diameter_um": None}, audit

    value, why = _contract_value("bead.diameter_um")
    if value is not None:
        audit["diameter_from"] = "bead.diameter_um"
        return {"kind": "beads", "diameter_um": value}, audit
    gaps.add("particles.diameter_um",
             "no bead diameter: the source does not give one and " + why, op)
    audit["diameter_from"] = None
    return {"kind": "beads", "diameter_um": None}, audit


def _shear_class(kind: str, info: dict, next_kind: str | None) -> tuple[str, dict]:
    """Precedence: capsules > prelysis_rinse > adherent_cells > none."""
    if info["kind"] == "capsules":
        shear_class = "capsules"
        why = ("capsules are not attached to anything, so the culture ceiling does not "
               "apply even when they hold live cells: the limit is escape pressure")
    elif info["kind"] == "cells" and kind in ("wash", "separate", "merge") and next_kind == "lyse":
        shear_class = "prelysis_rinse"
        why = ("rinse immediately before a deliberate lyse; held to the culture ceiling it "
               "takes about 12 minutes and destroys what it exists to preserve")
    elif info["kind"] == "cells" and info["attached"] and info["alive"]:
        shear_class = "adherent_cells"
        why = "cells attached and alive here: the risk is detaching them"
    elif info["dissociating"]:
        shear_class = "none"
        why = "deliberate dissociation: detaching the cells is the point of this op"
    else:
        shear_class = "none"
        why = {
            "none": "nothing shear-sensitive in this op",
            "beads": "beads are not shear-sensitive",
            "cells": "cells in suspension, or already killed: nothing to detach",
            "capsules": "",
        }[info["kind"]]

    ceiling = contracts.shear_ceiling_for(shear_class)
    return shear_class, {
        "why": why,
        "bound_by": ceiling.bound_by,
        "ceiling_dyn_cm2": ceiling.ceiling_dyn_cm2,
        "pressure_ceiling_kpa": ceiling.pressure_ceiling_kpa,
        "from": ceiling.source,
    }


def _classify_fluid(blob: str, roles: set[str]) -> tuple[str, str] | None:
    if "oil" in roles or _has(blob, _OIL):
        return "oil", "oil phase (reagent role or name)"
    if _has(blob, _ORGANIC):
        return "organic", "organic solvent named"
    if "solvent" in roles:
        return "organic", "reagent role 'solvent'"
    if _has(blob, _VISCOUS):
        return "viscous", "polymer / crowding agent named (not water)"
    return None


def _fluid(atom: Atom, text: str, op: int, gaps: Gaps) -> tuple[dict, dict]:
    """The op's `fluid` object: which phase it pushes, and how much.

    The reagents decide the class; the sentence is consulted only when the op names none.
    Washing oil OUT of a trap with wash buffer pushes an aqueous phase, so it must not come
    out "oil" just because the word appears.
    """
    names = " ".join(f"{r.get('name') or ''} {r.get('role') or ''}".lower()
                     for r in atom.reagents)
    roles = {str(r.get("role") or "").lower() for r in atom.reagents}
    from_reagents = _classify_fluid(" " + names + " ", roles)
    from_text = _classify_fluid(text, set())

    if from_reagents is not None:
        viscosity_class, why = from_reagents
    elif not atom.reagents and from_text is not None:
        viscosity_class = from_text[0]
        why = from_text[1] + " in the step text (this op names no reagent)"
    else:
        viscosity_class, why = "aqueous", "no non-aqueous reagent named"

    audit: dict = {"viscosity_class_why": why}
    if from_text is not None and from_text[0] != viscosity_class:
        # An aqueous wash that displaces oil, or an encapsulation whose core is viscous
        # while the carrier is oil: S5 needs to know both are in the part.
        audit["other_phase_present"] = from_text[0]

    if viscosity_class == "aqueous":
        viscosity = contracts.viscosity_for(atom.temp_c)
        audit["viscosity_pa_s"] = viscosity.pa_s
        audit["viscosity_from"] = viscosity.key
        audit["viscosity_scope"] = viscosity.scope
        if atom.temp_c is None:
            gaps.add("temp_c", "the source states no temperature for this op; the 37 C "
                               "viscosity is used because the device runs warm", op,
                     assumed=viscosity.pa_s, assumed_from=viscosity.key)
    else:
        key = f"fluid.viscosity_{viscosity_class}_pa_s"
        value, why_missing = _contract_value(key)
        audit["viscosity_pa_s"] = value
        audit["viscosity_from"] = key if value is not None else None
        if value is None:
            gaps.add(
                "fluid.viscosity_pa_s",
                f"a {viscosity_class} phase is not water and the contracts hold no "
                f"viscosity for one ({why_missing}). fluid.viscosity_37c_pa_s and "
                f"fluid.viscosity_pa_s are both water, so flow, pressure and shear for "
                f"these ops cannot be computed until the contract has a {viscosity_class} "
                f"value - do not substitute the aqueous one", op, dedupe=key)

    volume_ul, volume_missing = _volume_ul(atom.reagents, atom.repeats)
    if volume_missing:
        gaps.add("fluid.volume_ul", "a reagent in this op has no stated volume", op)
    fluid = {"volume_ul": volume_ul, "viscosity_class": viscosity_class}
    note = "the paper's volume, for traceability only: it describes a tube, not the chip"
    if atom.repeats > 1 and volume_ul is not None:
        note = f"{atom.repeats} repeats in the source, total; " + note
    fluid["note"] = note
    return fluid, audit


# --------------------------------------------------------------------------------------
# what the place has to hold: `load` and `ratio`
# --------------------------------------------------------------------------------------

# Parts that only CARRY a stream: a junction, a mixer, a nozzle (designs rules 2 and 5).
FLOW_KINDS = {"merge", "mix", "split", "pool", "transfer", "encapsulate"}
# Parts that HOLD something while it happens.
HOLD_KINDS = {"load", "incubate", "culture", "wash", "separate", "lyse", "thermocycle",
              "cure", "collect", "image"}
# An addition of a reagent INTO a sample, which is what a ratio is for. A wash is not one:
# it is a sweep, and its reagent volume is dropped entirely.
ADDITION_KINDS = {"merge", "mix", "lyse"}

_SUPERSCRIPT = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_NUM = r"\d+(?:\.\d+)?(?:\s*(?:[eE]|x\s*10\s*\^?)\s*\d+)?"
_PARTICLE_WORD = r"(?:cells|cell|nuclei|nucleus|capsules|beads|spheroids|organoids)"
_AREA_UNITS = {"cm2": 0.01, "mm2": 1.0}        # multiply a per-unit-area density by this
_VOLUME_UNITS = {"ul": 1.0, "nl": 1000.0, "ml": 0.001, "l": 0.000001}


def _normalise_numbers(text: str) -> str:
    """Superscripts, unicode times signs and thousands separators out of the way."""
    out = text.translate(_SUPERSCRIPT).replace("×", "x").replace("·", ".")
    # Lowercased so 'cells/uL' and 'cells/mL' match the unit table; the callers in this
    # module pass lowercased text already, but a direct caller might not.
    out = out.lower().replace("µ", "u").replace("μ", "u")
    return re.sub(r"(\d),(\d\d\d)\b", r"\1\2", out)


def _to_float(token: str) -> float | None:
    """'1e6', '1 x 10^6', '2000' -> a float. None when it is not a number after all."""
    token = token.strip().replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(?:(?:[eE]|x10\^?)(\d+))?", token)
    if not match:
        return None
    value = float(match.group(1))
    if match.group(2):
        value *= 10 ** int(match.group(2))
    return value


def _parse_quantities(text: str) -> dict:
    """Counts and densities the SOURCE states. Nothing here is inferred or defaulted."""
    blob = _normalise_numbers(text)
    found: dict[str, float] = {}

    # Densities first, so '5 x 10^4 cells/cm2' is not also read as a count.
    for match in re.finditer(
        rf"({_NUM})\s*{_PARTICLE_WORD}?\s*(?:/|per\s+)\s*(cm2|cm\^2|mm2|mm\^2|ul|nl|ml|l)\b",
        blob,
    ):
        value = _to_float(match.group(1))
        unit = match.group(2).replace("^", "")
        context = blob[max(0, match.start() - 30):match.end()]
        if value is None or not _has(context, ("cell", "nuclei", "capsule", "bead")):
            continue
        if unit in _AREA_UNITS:
            found["surface_density_per_mm2"] = value * _AREA_UNITS[unit]
        else:
            found["suspension_density_per_ul"] = value * _VOLUME_UNITS[unit]
        blob = blob[:match.start()] + " " * (match.end() - match.start()) + blob[match.end():]

    for match in re.finditer(rf"({_NUM})\s*({_PARTICLE_WORD})\b", blob):
        value = _to_float(match.group(1))
        if value is None or value <= 0:
            continue
        key = "particles" if match.group(2).startswith(("capsule", "bead")) else "cells"
        found.setdefault(key, value)
    return found


def _sphere_ul(diameter_um: float) -> float:
    """Volume of one sphere in microlitres (1 uL = 1e9 um^3)."""
    import math
    return (math.pi / 6.0) * diameter_um ** 3 / 1e9


class Population:
    """Counts and densities the source stated, carried with the population that moves.

    The paper states a seeding density once and then says "the cells" for twenty steps;
    the capsule count is stated at the nozzle and the trap downstream holds those same
    capsules. Without this, every later op falls back to a contract default and the SAME
    chamber comes out 8000 mm2 in one op and 4 mm2 in the next - which is how a chip gets
    sized from nothing. Keyed by particle kind, because cells becoming capsules is a new
    population with its own count.
    """

    KEYS = ("cells", "particles", "surface_density_per_mm2", "suspension_density_per_ul")

    def __init__(self) -> None:
        self.by_kind: dict[str, dict[str, tuple[float, int]]] = {}

    def update(self, particle_kind: str, stated: dict, op: int) -> None:
        here = self.by_kind.setdefault(particle_kind, {})
        for key in self.KEYS:
            value = stated.get(key)
            if value is not None:
                here[key] = (value, op)        # a later statement supersedes an earlier one

    def get(self, particle_kind: str, key: str) -> tuple[float | None, int | None]:
        value = self.by_kind.get(particle_kind, {}).get(key)
        return value if value else (None, None)


def _load_basis(kind: str, particles: dict, info: dict, established: str | None) -> tuple[str, str]:
    """Which `load.basis` applies, and why. A part either carries a stream or holds something.

    The basis is STICKY per vessel: a chamber that holds a monolayer still holds that
    monolayer while the cells are being lysed in it, even though they are no longer alive
    and `attached` has gone false. Only a dissociation (or the population leaving) resets
    it, which is the difference between "the cells are in suspension" and "the cells have
    just been killed where they sat".
    """
    if kind in FLOW_KINDS:
        return "flow_through", ("the part only has to carry the stream: a junction, a mixer "
                                "or a nozzle (designs rules 2 and 5)")
    if established and not info["dissociating"] and particles["kind"] != "none":
        return established, f"the vessel already holds this population on a {established} basis"
    if particles["kind"] in ("capsules", "beads"):
        return "packed_volume", (f"a bed of {particles['kind']}: the chamber holds their "
                                 f"PACKED volume, not a tube's worth of liquid")
    if particles["kind"] == "cells":
        if info["attached"] or (info["alive"] is False and info["dissociating"] is False
                                and established == "adherent_area"):
            return "adherent_area", ("cells attached to the floor: the chamber is sized by "
                                     "FLOOR AREA at the seeding density")
        return "suspension_volume", ("cells in suspension: the chamber is sized by the "
                                     "volume that holds them at their density")
    # Nothing is held. A timed hold with nothing in it is a delay line (designs rule 4);
    # anything else holds nothing at all.
    if kind in ("incubate", "thermocycle", "cure"):
        return "flow_through", ("a timed hold with nothing retained is a delay line "
                                "(designs rule 4), sized from residence time")
    return "none", f"a {kind} op with no particles holds nothing to size a part from"


def _envelope_fit(what: str, value: float, unit: str, op: int, gaps: Gaps) -> str | None:
    """Flag a load that no allowed chip can hold. Not clamped here: S4 splits, S6 sizes."""
    try:
        demo = contracts.envelope("demo")
        ceiling = contracts.envelope("ceiling")
    except contracts.ContractError:
        return None
    if unit == "mm2":
        budgets = [("demo", demo.area_mm2), ("ceiling", ceiling.area_mm2)]
    else:                                  # uL, against the one-layer etched volume
        budgets = [("demo", demo.area_mm2 * demo.depth_mm),
                   ("ceiling", ceiling.area_mm2 * ceiling.depth_mm)]
    too_big = [name for name, budget in budgets if value > budget]
    if not too_big:
        return None
    worst = budgets[-1]
    message = (f"the {what} this op needs ({value:.3g} {unit}) does not fit the "
               f"{', '.join(too_big)} envelope"
               + (f" - even the ceiling holds only {worst[1]:.3g} {unit} on one layer"
                  if "ceiling" in too_big else ""))
    if "ceiling" in too_big:
        gaps.add(f"load.{what}",
                 message + ": the assay must be split across chips (S4) or run at a lower "
                           "count than the paper's, which is a decision, not a rounding", op)
    return message


def _load(atom: Atom, particles: dict, info: dict, stated: dict, population: Population,
          established: str | None, op: int, gaps: Gaps) -> tuple[dict, dict, str]:
    """The op's `load`: what the place it happens in must HOLD, never the tube volume."""
    basis, basis_why = _load_basis(atom.kind, particles, info, established)
    load: dict[str, Any] = {"basis": basis, "source": "paper"}
    audit: dict[str, Any] = {"basis_why": basis_why, "stated_in_source": dict(stated)}
    notes: list[str] = []
    sources: set[str] = set()
    particle_kind = particles["kind"]

    def quantity(key: str, what: str, default_key: str) -> float | None:
        """The paper's number: stated here, else carried with this population, else a
        flagged contract default. Every outcome is recorded."""
        value = stated.get(key)
        if value is not None:
            sources.add("paper")
            return value
        carried, from_op = population.get(particle_kind, key)
        if carried is not None:
            sources.add("paper")
            audit.setdefault("carried_from_op", {})[what] = from_op
            notes.append(f"{what} carried from op {from_op} (the same population; the "
                         f"source states it once)")
            return carried
        value, why = _contract_value(default_key)
        if value is None:
            gaps.add(f"load.{what}", f"no {what} for this op, and " + why, op)
            return None
        low, high = contracts.param_range(default_key)
        gaps.add(f"load.{what}",
                 f"the source never states {what}; {default_key} is used"
                 + (f" (range {low}-{high})" if low is not None else "")
                 + (", and the contract marks it an assumption rather than a published value"
                    if contracts.is_assumption(default_key) else ""),
                 op, assumed=value, assumed_from=default_key)
        audit.setdefault("contract_defaults", []).append(default_key)
        sources.add("contract_default")
        notes.append(f"{what} not in the source: {default_key} used, provisional")
        return value

    if basis == "none":
        load["source"] = "derived"
        return load, audit, basis

    if basis == "flow_through":
        load["residence_s"] = atom.duration_s
        load["source"] = "paper" if atom.duration_s is not None else "derived"
        if atom.duration_s is None:
            gaps.add("load.residence_s",
                     "this op is sized from residence time and the source states no "
                     "duration, so S6 has nothing to set the delay volume from", op)
            notes.append("no residence time stated")
        else:
            notes.append(f"residence {atom.duration_s:g} s, sized with the flow rate S6 picks")
        load["note"] = "; ".join(notes)
        return load, audit, basis

    if basis == "adherent_area":
        count = quantity("cells", "cells", "target_cells")
        density = quantity("surface_density_per_mm2", "surface_density_per_mm2",
                           "culture.surface_density_per_mm2")
        load["cells"] = stated.get("cells") if stated.get("cells") is not None else count
        load["surface_density_per_mm2"] = density
        if count is not None and density:
            load["area_mm2"] = count / density
            sources.add("derived")
            notes.append(f"area = {count:g} cells / {density:g} per mm2 "
                         f"= {count / density:.4g} mm2")
            warning = _envelope_fit("area_mm2", count / density, "mm2", op, gaps)
            if warning:
                notes.append(warning)
                audit["envelope"] = warning
        else:
            load["area_mm2"] = None

    elif basis == "packed_volume":
        count = quantity("particles" if particle_kind != "cells" else "cells", "cells",
                         "capsule.count" if particle_kind == "capsules" else "target_cells")
        diameter = particles.get("swollen_diameter_um") or particles.get("diameter_um")
        load["cells"] = count
        if particles.get("swollen_diameter_um"):
            notes.append("swollen diameter: that is the bed the chamber has to hold")
        packing, why = _contract_value("capsule.packing_fraction")
        if packing is None:
            gaps.add("load.packed_volume_ul", "cannot work out a bed volume: " + why, op)
            load["packed_volume_ul"] = None
        elif count is not None and diameter:
            audit.setdefault("contract_defaults", []).append("capsule.packing_fraction")
            volume = count * _sphere_ul(diameter) / packing
            load["packed_volume_ul"] = volume
            sources.add("derived")
            notes.append(f"bed = {count:g} x (pi/6)({diameter:g} um)^3 / packing fraction "
                         f"{packing:g} = {volume:.4g} uL")
            warning = _envelope_fit("packed_volume_ul", volume, "uL", op, gaps)
            if warning:
                notes.append(warning)
                audit["envelope"] = warning
        else:
            load["packed_volume_ul"] = None
            if not diameter:
                notes.append("no particle diameter, so no bed volume")

    else:  # suspension_volume
        count = quantity("cells", "cells", "target_cells")
        density = quantity("suspension_density_per_ul", "suspension_density_per_ul",
                           "encapsulation.cell_conc_per_ul")
        load["cells"] = count
        load["suspension_density_per_ul"] = density
        if count is not None and density:
            volume = count / density
            sources.add("derived")
            audit["suspension_volume_ul"] = volume
            notes.append(f"volume = {count:g} cells / {density:g} per uL = {volume:.4g} uL "
                         f"(S6 rounds UP to the smallest buildable chamber rather than "
                         f"refusing it)")
            warning = _envelope_fit("suspension_volume_ul", volume, "uL", op, gaps)
            if warning:
                notes.append(warning)
                audit["envelope"] = warning

    # Honest provenance: `paper` only when nothing was computed or defaulted.
    if "contract_default" in sources:
        load["source"] = "contract_default"
    elif "derived" in sources:
        load["source"] = "derived"
    elif "paper" in sources:
        load["source"] = "paper"
    else:
        load["source"] = "derived"
    if notes:
        load["note"] = "; ".join(notes)
    return load, audit, basis


def _ratio(atom: Atom, kind: str, sample_ul: float | None, op: int,
           gaps: Gaps) -> dict | None:
    """The reagent:sample ratio for an ADDITION, so the concentration survives rescaling.

    A wash or rinse returns None on purpose: its volume is dropped and becomes a sweep.
    """
    if kind not in ADDITION_KINDS or not atom.reagents:
        return None
    reagent_ul, _ = _volume_ul(atom.reagents, 1)
    ratio: dict[str, Any] = {
        "paper_reagent_ul": reagent_ul,
        "paper_sample_ul": sample_ul,
        "reagent_to_sample": None,
    }
    if reagent_ul is not None and sample_ul:
        ratio["reagent_to_sample"] = reagent_ul / sample_ul
        ratio["note"] = (f"{reagent_ul:g} uL into {sample_ul:g} uL in the source: S8 keeps "
                         f"this ratio so the on-chip concentration matches the paper")
    else:
        missing = "the reagent volume" if reagent_ul is None else "the sample volume it goes into"
        gaps.add("ratio.reagent_to_sample",
                 f"this op adds a reagent but the source does not give {missing}, so the "
                 f"concentration on chip cannot be matched to the paper", op)
        ratio["note"] = f"no ratio: {missing} is not in the source"
    return ratio


def _sweep_note() -> str:
    """Why a wash has no ratio, with the sweep the contract implies instead."""
    import math
    fraction, why = _contract_value("exchange.target_fraction")
    if fraction is None:
        return "no ratio: a wash becomes a sweep of the chamber, but " + why
    volumes = -math.log(1 - float(fraction))
    return (f"no ratio: a wash's reagent volume is dropped and becomes a sweep of "
            f"{volumes:.2f} chamber volumes (-ln(1 - exchange.target_fraction), "
            f"target {fraction})")


def _track_sample_volume(atom: Atom, fluid: dict, sample_ul: dict[str, float | None]) -> None:
    """Keep the paper's standing volume per vessel, for the next addition's ratio."""
    volume = fluid.get("volume_ul")
    if volume is None:
        return
    if atom.kind in ("load", "transfer", "pool", "split"):
        sample_ul[atom.target] = volume            # the vessel is filled with the sample
    elif atom.kind in ADDITION_KINDS:
        standing = sample_ul.get(atom.target)
        sample_ul[atom.target] = volume if standing is None else standing + volume
    elif atom.kind == "separate" and atom.reagents:
        sample_ul[atom.target] = volume            # resuspended in a fresh volume
    # A wash replaces liquid rather than adding to it, so the standing volume is unchanged.


def _volume_ul(reagents: list[dict], repeats: int) -> tuple[float | None, bool]:
    """Total liquid the op moves, and whether any named reagent had no volume."""
    total = 0.0
    known = False
    missing = False
    for reagent in reagents:
        value = reagent.get("volume_ul")
        if isinstance(value, (int, float)):
            total += float(value)
            known = True
        else:
            missing = True
    if not known:
        return None, bool(reagents)
    return total * max(1, repeats), missing


# --------------------------------------------------------------------------------------
# protocol choice
# --------------------------------------------------------------------------------------

def _protocol_choice(kind: str, particles: dict, text: str) -> dict:
    """Which contract operation S5 should emit, applying the mapping's `rule`."""
    mapping = KIND_TO_PROTOCOL[kind]
    operation: str | None = mapping.operation
    why = mapping.rule

    if kind == "merge" and _has(text, _CUE["load"]):
        operation, why = "load", "fills the vessel rather than joining a flowing stream"
    elif kind == "collect":
        if particles["kind"] == "capsules":
            operation = "release"
            why = ("a retained capsule bed must be freed; operator/safety.py requires a "
                   "back-flush first")
        elif particles["kind"] == "cells" and not _has(text, ("supernatant", "medium only")):
            operation, why = "harvest", "the cells themselves leave through the outlet"
    elif kind == "image":
        if _has(text, _CUE["count"]):
            operation, why = "count", "a count / viability readout at a QC window"
        else:
            operation = None
            why = "no protocol operation for imaging: off_chip by designs rule 6"
    elif kind == "separate" and _has(text, ("filter", "strain", "sort", "weir", "dld",
                                            "size-sort", "size sort")):
        operation = None
        why = ("a size-sorting separation is part geometry, not a protocol step: expect "
               "missing_part")

    return {
        "operation": operation,
        "also": list(mapping.also),
        # approximate: the contract has no operation for this kind, so S5 spells it as
        # something else. unmappable: there is no protocol step at all.
        "approximate": mapping.approximate and operation is not None,
        "unmappable": operation is None,
        "why": why,
    }


# --------------------------------------------------------------------------------------
# the stage
# --------------------------------------------------------------------------------------

# `placement` is required on every op, but placing them is S3's job. `pending` is the
# contract's explicit "no decision": it cannot be read as a verdict the way a fabricated
# missing_part could, S3 must replace every one, and S4+ refuses any that survive.
PLACEMENT_PENDING = {
    "where": "pending",
    "why": "S2 does not place ops; S3 decides",
}


def normalise(assay: dict, *, use_llm: bool = False, strict: bool = True) -> dict:
    """assay.json (as a dict) -> unitops.json (as a dict), schema-shaped.

    Deterministic and offline unless `use_llm`, and even then the model only proposes
    extra step splits: the physics classes are rule-based either way.
    """
    vocabulary = check_vocabulary()
    steps = list(assay.get("steps") or [])
    steps_by_number = {int(s["step"]): s for s in steps}

    atoms: list[Atom] = []
    for step in sorted(steps, key=lambda s: int(s["step"])):
        atoms.extend(_atoms_for_step(step, use_llm))
    atoms = _merge_atoms(atoms)

    inventory = _particle_walk(atoms, steps_by_number)
    gaps = Gaps()
    ops: list[dict] = []
    # The sample volume standing in each vessel, so an addition's ratio is reagent : what
    # it went INTO rather than reagent : itself. The paper's numbers, for the ratio only.
    sample_ul: dict[str, float | None] = {}
    # Counts and densities travel with the population; the load basis sticks to a vessel.
    population = Population()
    basis_by_target: dict[str, str] = {}

    for index, atom in enumerate(atoms):
        op_number = index + 1
        text = " ".join(_blob(steps_by_number[n]) for n in atom.steps if n in steps_by_number)
        info = inventory[index]
        next_kind = atoms[index + 1].kind if index + 1 < len(atoms) else None

        particles, particle_audit = _particles(info, op_number, gaps)
        shear_class, shear_audit = _shear_class(atom.kind, info, next_kind)
        fluid, fluid_audit = _fluid(atom, text, op_number, gaps)
        protocol = _protocol_choice(atom.kind, particles, text)

        stated = _parse_quantities(text)
        population.update(particles["kind"], stated, op_number)
        load, load_audit, basis = _load(atom, particles, info, stated, population,
                                        basis_by_target.get(atom.target), op_number, gaps)
        if basis in ("adherent_area", "packed_volume", "suspension_volume"):
            basis_by_target[atom.target] = basis
        if info["dissociating"]:
            # The cells came off the floor here: this vessel no longer holds a monolayer.
            basis_by_target[atom.target] = "suspension_volume"
        if atom.target not in sample_ul and index > 0:
            # A population that moved brings its sample volume with it.
            sample_ul[atom.target] = sample_ul.get(atoms[index - 1].target)
        ratio = _ratio(atom, atom.kind, sample_ul.get(atom.target), op_number, gaps)
        _track_sample_volume(atom, fluid, sample_ul)
        if atom.kind in ("wash", "separate") and not ratio:
            load_audit["no_ratio"] = _sweep_note()

        from_steps = sorted(set(atom.steps))
        notes = [atom.why, *atom.cues.get("merged", [])]
        if atom.cues.get("duration_moved_to"):
            notes.append(f"the step's one stated duration went to the "
                         f"{atom.cues['duration_moved_to']} op rather than being copied")
        if atom.confidence < 0.5:
            notes.append(f"low confidence ({atom.confidence}) inherited from the source step")

        audit = {
            "stage": STAGE,
            "confidence_basis": "lowest confidence of the source steps; never aggregated upward",
            "target": atom.target,
            "repeats": atom.repeats,
            "reagents": [{"name": r.get("name"), "role": r.get("role"),
                          "volume_ul": r.get("volume_ul")} for r in atom.reagents],
            "protocol": protocol,
            "particles": particle_audit,
            "fluid": fluid_audit,
            "load": load_audit,
            "shear": shear_audit,
            "why": atom.why,
        }

        ops.append({
            "op": op_number,
            "from_steps": from_steps,
            "kind": atom.kind,
            "confidence": atom.confidence,
            "load": load,
            **({"ratio": ratio} if ratio else {}),
            "fluid": fluid,
            "particles": particles,
            "duration_s": atom.duration_s,
            "temp_c": atom.temp_c,
            "shear_class": shear_class,
            "placement": dict(PLACEMENT_PENDING),
            "note": "; ".join(n for n in notes if n),
            "audit": audit,
        })

    coverage = check_coverage(assay, ops, strict=strict)

    unresolved = [dict(u) for u in (assay.get("unresolved") or [])]
    unresolved.extend(gaps.unresolved())
    for op in ops:
        confidence = op["confidence"]
        if confidence < 0.5:
            unresolved.append({
                "op": op["op"],
                "field": "confidence",
                "question": (f"op {op['op']} ({op['kind']}) rests on low-confidence "
                             f"step(s) {op['from_steps']} (confidence {confidence}): the op "
                             f"is no more certain than the step it came from"),
            })

    out: dict[str, Any] = {
        "id": assay.get("id"),
        "ops": ops,
        "unresolved": unresolved,
        "llm": llm.provenance(),
        "audit": {
            "stage": STAGE,
            "vocabulary": vocabulary,
            "coverage": coverage,
            "contracts": contracts.fingerprint(),
            "placement": "every op is placement.where 'pending': S3 decides, and must "
                         "replace every one of them",
        },
        "note": (f"{coverage['steps']} assay steps became {coverage['ops']} canonical unit "
                 f"operations ({len(coverage['many_to_one'])} ops cover several steps, "
                 f"{len(coverage['one_to_many'])} steps became several ops)"),
    }
    if assay.get("source") is not None:
        out["source"] = assay["source"]
    if assay.get("extraction") is not None:
        out["extraction"] = assay["extraction"]
    if assay.get("placeholder"):
        out["placeholder"] = True
    return out


def check_coverage(assay: dict, ops: list[dict], *, strict: bool = True) -> dict:
    """Every assay step must appear in at least one op's from_steps. A gap raises.

    A silently dropped step is the worst outcome this stage can produce, so the default is
    to raise; strict=False returns the report instead, for a caller that wants to show it.
    """
    wanted = sorted(int(s["step"]) for s in (assay.get("steps") or []))
    seen: dict[int, list[int]] = {}
    problems: list[dict] = []
    for op in ops:
        if not op["from_steps"]:
            problems.append({"op": op["op"], "problem": "no from_steps: it traces to nothing"})
        for number in op["from_steps"]:
            seen.setdefault(number, []).append(op["op"])
            if number not in wanted:
                problems.append({"op": op["op"],
                                 "problem": f"from_steps cites step {number}, which is not "
                                            f"in the assay"})
    missing = [n for n in wanted if n not in seen]
    report = {
        "steps": len(wanted),
        "ops": len(ops),
        "missing_steps": missing,
        "problems": problems,
        "ok": not missing and not problems,
        "steps_per_op": {str(op["op"]): op["from_steps"] for op in ops},
        "ops_per_step": {str(n): seen.get(n, []) for n in wanted},
        "many_to_one": [op["op"] for op in ops if len(op["from_steps"]) > 1],
        "one_to_many": sorted(n for n, o in seen.items() if len(o) > 1),
    }
    if strict and not report["ok"]:
        raise CoverageError(
            f"S2 coverage failed: {len(missing)} assay step(s) reached no unit operation: "
            f"{missing}" + (f"; {problems}" if problems else "")
            + ". Every step must land in at least one op (assay2chip/README.md): a dropped "
              "step is a silently missing piece of the assay."
        )
    return report


def run(assay_id: str | None = None, ctx: Any = None, llm_mode: str | None = None,
        verbose: bool = False, **kwargs: Any) -> dict | None:
    """Pipeline entry point: read assay.json, write unitops.json.

    run.py passes only the arguments named here (assay2chip/stages/__init__.py); the rest
    are ignored. Returns None: S2 never asks for a re-run, because nothing downstream can
    tell it something it did not already know from assay.json.
    """
    if not assay_id:
        assay_id = getattr(ctx, "id", None) or getattr(ctx, "assay_id", None)
    if not assay_id:
        raise ValueError("s2_normalise.run needs assay_id, or a ctx carrying one")

    if llm_mode:
        llm.configure(llm_mode)
    # The model is only ever consulted in real mode: in stub mode it would return the
    # deterministic answer anyway, and the file would carry a misleading `llm` block.
    unitops = normalise(read_stage(assay_id, "assay"), use_llm=llm_mode == "real",
                        strict=True)
    if not unitops["ops"]:
        raise CoverageError(
            f"S2 produced no unit operations for '{assay_id}': assay.json has no steps, so "
            f"there is nothing to put on a chip. Re-run S1 (and check S0's raw.json)."
        )
    path = write_stage(assay_id, "unitops", unitops)
    if verbose:
        _report(unitops)
        print(f"  wrote {path}")
    return None


def _report(unitops: dict) -> None:
    coverage = unitops["audit"]["coverage"]
    print(f"s2_normalise: {coverage['steps']} steps -> {coverage['ops']} ops "
          f"(many-to-one: {len(coverage['many_to_one'])}, "
          f"one-to-many: {len(coverage['one_to_many'])})")
    for op in unitops["ops"]:
        particles = op["particles"]
        diameter = particles.get("swollen_diameter_um") or particles.get("diameter_um")
        load = op["load"]
        held = (load.get("area_mm2") and f"{load['area_mm2']:.3g} mm2") or \
               (load.get("packed_volume_ul") and f"{load['packed_volume_ul']:.3g} uL bed") or \
               (load.get("residence_s") is not None and f"{load['residence_s']:g} s") or \
               (op["audit"]["load"].get("suspension_volume_ul")
                and f"{op['audit']['load']['suspension_volume_ul']:.3g} uL") or "-"
        ratio = op.get("ratio", {}).get("reagent_to_sample")
        print(f"  op {op['op']:>3} {op['kind']:<12} steps {str(op['from_steps']):<9}"
              f" {particles['kind']:<8} {str(diameter or '-'):>6} um"
              f" shear={op['shear_class']:<15} {op['fluid']['viscosity_class']:<8}"
              f" load={load['basis']:<17} {held:>12} [{load['source']}]"
              f" ratio={('%.3g' % ratio) if ratio else '-':<7}"
              f" conf={op['confidence']} {op['placement']['where']}")
    if unitops["unresolved"]:
        print(f"  unresolved ({len(unitops['unresolved'])}):")
        for item in unitops["unresolved"]:
            where = f"step {item['step']}" if item.get("step") else (
                f"op {item['op']}" if item.get("op") else "-")
            print(f"    {where} {item.get('field', '')}: {item['question'][:120]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S2 normalise: assay.json -> unitops.json")
    parser.add_argument("--id", help="work id: assay2chip/work/<id>/")
    parser.add_argument("--in", dest="in_path",
                        help="read this assay.json instead of the work directory's")
    parser.add_argument("--out", dest="out_path", help="write unitops.json here as well")
    parser.add_argument("--llm", choices=("stub", "real"), default=None,
                        help="let the model propose extra step splits (linguistic only)")
    parser.add_argument("--no-strict", action="store_true",
                        help="report a coverage failure instead of raising")
    args = parser.parse_args(argv)

    if args.llm:
        llm.configure(args.llm)
    if not args.id and not args.in_path:
        parser.error("give --id or --in")

    try:
        assay = load_json(args.in_path) if args.in_path else read_stage(args.id, "assay")
    except StageFileMissing as e:
        print(f"s2_normalise: {e}", file=sys.stderr)
        return 1

    try:
        unitops = normalise(assay, use_llm=args.llm == "real", strict=not args.no_strict)
    except CoverageError as e:
        print(f"s2_normalise: {e}", file=sys.stderr)
        return 1

    _report(unitops)
    if args.id:
        print(f"  wrote {write_stage(args.id, 'unitops', unitops)}")
    if args.out_path:
        print(f"  wrote {write_json(args.out_path, unitops)}")
    return 0 if unitops["audit"]["coverage"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

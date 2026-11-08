"""S1 extract: raw.json -> assay.json. What the source SAYS, still in lab language.

    python assay2chip/stages/s1_extract.py --id messy_wash
    python assay2chip/stages/s1_extract.py --id messy_wash --print --no-write
    python assay2chip/stages/s1_extract.py --selftest        # unit tables, offline

Everything downstream is derived from this file, so this is the stage where a guessed
number does the most damage. An audit of this repo found four different capsule diameters
living side by side and a citation whose paper never mentioned the thing it was cited for.
The three rules that follow exist because of that:

  1. `text` is VERBATIM: a contiguous substring of one `raw.json` block - a sentence, or
     the table row as the source printed it - with only the source's own step label
     ("3. ") stripped, which goes to `audit.source_label`. Nothing is ever paraphrased
     into `text`. Model output is checked against the source and demoted when it does not
     match (`_verify`).
  2. Every step carries `confidence` and `quote_page`, and `audit.confidence_reasons`
     says how the confidence was arrived at, so a reader can disagree with it.
  3. Anything the source does not state is `null` PLUS a specific question in
     `unresolved`. Never a plausible default: a missing volume is `null`, not "probably
     100 uL". Picking a value is a later stage's job, and the README makes it flag the
     choice against a contract key.

Model and fallback, and how to tell which one ran
-------------------------------------------------
Prose to structured steps is a language-model job, so in `--llm real` this stage asks
`assay2chip/lib/llm.py` and then CHECKS the answer against raw.json: every quote must
appear verbatim in a block, every number is re-parsed from that quote, the page is
corrected to the block the text really came from, and the source wins any disagreement.
In the default stub mode no model is called at all and the deterministic extractor runs:
regex over numbered steps, parameter tables, units and a verb lexicon. It is honestly
worse - fewer reagents, more null targets, more questions, confidence capped at
DETERMINISTIC_CEILING. `extraction.mode`, `extraction.fallback_reason`, the `llm` block
and `audit.extracted_by` on every step all record which path produced what.

Units
-----
mL/uL/nL, ms/s/min/h/d and degC are normalised to uL, seconds and degrees C through the
tables at the top of this module, and the ORIGINAL string is kept beside the parsed
number (`audit.duration.text`, `audit.reagents[].volume_text`, `audit.table.cells`) so a
factor-of-ten slip is auditable instead of invisible. For a table the unit comes from the
COLUMN HEADER ("Volume (uL)", "Time (min)"), and a column with no unit in its header is a
question, not a guess. `--selftest` checks the conversions, including the traps: "0.1 mL"
is 100 uL and "1,000 nL" is 1 uL.

Messiness this handles, and says it handled
-------------------------------------------
A step split over two sentences (merged, `audit.sentence_count`); "repeat steps 3-5 three
times" (expanded, `audit.repeat`, plus a question because "three times" does not say
whether the first pass counts); ranges ("5-10 min" -> `duration_s` null and
`audit.duration.range_s` [300, 600], never silently one end); "overnight", "on ice",
"room temperature" (qualitative: null plus a question); a bare "RT", which is read as
NEITHER room temperature nor reverse transcription; parenthetical asides; one sentence
that is really two actions (split, both halves keep the whole verbatim sentence); step
tables whose numbers belong to numbered prose steps; a table that IS the method; a cell
whose value is only in a footnote; "not stated" cells; irregular source numbering
(1, 2, 3, 3b, 5, then 4); and blocks under an "Abstract", "Materials" or "Notes" heading,
which are context and not steps.

Anything S0 flagged with `needs_attention` is low confidence here by construction, and
S0's own note is carried into `audit` rather than restated.

CONFIDENTIALITY (CONFIDENTIALITY.md) - the boundary runs through this file
-------------------------------------------------------------------------
`assay.json` is a working artefact that cites its source, so it may hold the source's OWN
reagent names: an ingested published paper legitimately contains them. THAT STOPS HERE.
Anything written into a committed chip spec, protocol or doc must use generic reagent
names with no concentrations, vendors or prices, which is why every reagent carries a
`role` for a later stage to genericise by, and why `audit.reagents[].generic` offers a
snake_case, number-free form of each name for whoever writes those files. Nothing from the
lab's Labstep or OneDrive may enter this pipeline at all. This module hard-codes no
reagent names: the role lexicon is category words only ("buffer", "medium", "oil") and
every name comes from the text at run time.

The artefact is deterministic: steps in document order, `unresolved` sorted, no
timestamps, same bytes for the same raw.json on any machine.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import io as stage_io  # noqa: E402
from assay2chip.lib import llm  # noqa: E402

STAGE = "s1_extract"
INPUT_KIND = "raw"
OUTPUT_KIND = "assay"
TASK = "s1_extract_steps"

# Nothing outside assay2chip/work/<id>/. The orchestrator checks this.
WRITES: list[str] = []

DETERMINISTIC_CEILING = 0.6   # the no-model path never claims more than this
MODEL_CEILING = 0.9           # nor does the model path claim certainty
FLAGGED_CEILING = 0.3         # anything S0 flagged needs_attention
TABLE_CEILING = 0.5           # a number read out of a table S0 parsed cleanly
MIN_CONFIDENCE = 0.05


# --- units --------------------------------------------------------------------------
# One factor per unit, in one place, checked by --selftest. A wrong entry here is the
# invisible catastrophe, so nothing else in this module converts units by hand.

VOLUME_UL = {"l": 1_000_000.0, "ml": 1_000.0, "ul": 1.0, "nl": 0.001, "pl": 0.000_001}
VOLUME_ALIASES = {
    "l": "l", "litre": "l", "litres": "l", "liter": "l", "liters": "l",
    "ml": "ml", "mls": "ml", "millilitre": "ml", "millilitres": "ml",
    "milliliter": "ml", "milliliters": "ml", "cc": "ml",
    "ul": "ul", "uls": "ul", "\u00b5l": "ul", "\u03bcl": "ul",
    "microlitre": "ul", "microlitres": "ul", "microliter": "ul", "microliters": "ul",
    "nl": "nl", "nanolitre": "nl", "nanolitres": "nl", "nanoliter": "nl", "nanoliters": "nl",
    "pl": "pl", "picolitre": "pl", "picolitres": "pl", "picoliter": "pl", "picoliters": "pl",
}
TIME_S = {"ms": 0.001, "s": 1.0, "min": 60.0, "h": 3_600.0, "d": 86_400.0}
TIME_ALIASES = {
    "ms": "ms", "msec": "ms", "msecs": "ms", "millisecond": "ms", "milliseconds": "ms",
    "s": "s", "sec": "s", "secs": "s", "second": "s", "seconds": "s",
    "min": "min", "mins": "min", "minute": "min", "minutes": "min",
    "h": "h", "hr": "h", "hrs": "h", "hour": "h", "hours": "h",
    "d": "d", "day": "d", "days": "d",
}
# "m" for minutes and "M" for molar are the same token, so bare "m" is deliberately NOT
# a time unit: "a 5 m length of tubing" must not become 300 seconds. "5 m" of time stays
# unparsed and raises a question instead.

NUMBER = r"\d[\d,]*(?:\.\d+)?"
RANGE_SEP = r"\s*(?:-|\u2013|\u2014|to|or)\s*"


def _unit_group(aliases: dict) -> str:
    return "|".join(re.escape(k) for k in sorted(aliases, key=len, reverse=True))


VOLUME_ONE = re.compile(rf"(?P<num>{NUMBER})\s*(?P<unit>{_unit_group(VOLUME_ALIASES)})\b", re.I)
VOLUME_RANGE = re.compile(
    rf"(?P<lo>{NUMBER})\s*(?:(?P<lounit>{_unit_group(VOLUME_ALIASES)})\b)?{RANGE_SEP}"
    rf"(?P<hi>{NUMBER})\s*(?P<unit>{_unit_group(VOLUME_ALIASES)})\b", re.I)
TIME_ONE = re.compile(rf"(?P<num>{NUMBER})\s*(?P<unit>{_unit_group(TIME_ALIASES)})\b")
TIME_RANGE = re.compile(
    rf"(?P<lo>{NUMBER})\s*(?:(?P<lounit>{_unit_group(TIME_ALIASES)})\b)?{RANGE_SEP}"
    rf"(?P<hi>{NUMBER})\s*(?P<unit>{_unit_group(TIME_ALIASES)})\b")
TEMP_DEG = r"(?:\s*(?:\u00b0|\u00ba|deg(?:rees)?\.?)\s*)"
TEMP_ONE = re.compile(rf"(?P<num>-?{NUMBER})(?:{TEMP_DEG}|\s*)C(?![a-zA-Z])")
TEMP_RANGE = re.compile(
    rf"(?P<lo>-?{NUMBER})(?:{TEMP_DEG}|\s*)?(?:C(?![a-zA-Z]))?{RANGE_SEP}"
    rf"(?P<hi>-?{NUMBER})(?:{TEMP_DEG}|\s*)C(?![a-zA-Z])")
OTHER_TEMP_UNIT = re.compile(r"-?\d[\d.]*\s*(?:\u00b0\s*F|deg(?:rees)?\s*F\b|K\b)")

# Qualitative conditions: a phrase where a measurement should be. Each becomes null plus
# a question, because "overnight" and "room temperature" are not numbers.
QUALITATIVE_TIME = (
    (r"overnight", "overnight"),
    (r"\bo/n\b", "O/N"),
    # None means "use the matched words": "until sequencing" is not "until the cells lift".
    (r"until\s+[\w-]+(?:\s+[\w-]+){0,3}", None),
    (r"as (?:quickly|fast|soon) as possible", None),
)
QUALITATIVE_TEMP = (
    (r"room temperature", "room temperature"),
    (r"\bambient\b", "ambient"),
    (r"on ice\b", "on ice"),
    (r"ice[- ]cold", "ice-cold"),
    (r"\bchilled\b", "chilled"),
    (r"\bpre-?warmed\b", "pre-warmed"),
    (r"body temperature", "body temperature"),
)
# "RT" is reverse transcription at least as often as it is room temperature, so it is
# NEVER read as either here; it becomes a question. See `_ambiguous_rt`.
RT_TOKEN = re.compile(r"(?<![A-Za-z])RT(?![A-Za-z])")


# --- lexicons -----------------------------------------------------------------------
# Actions are the schema's enum and nothing else. A clause whose verb matches no entry is
# not given a plausible action; prose is left out of `steps` and listed in `unresolved`.

ACTIONS = (
    # (surface forms, action, weak)   weak => the mapping is a judgement call, and it is
    # written into `notes` and costs confidence.
    (r"lys(?:e|es|ed|ing|is)|permeabilis|permeabiliz", "lyse", False),
    (r"centrifug|spin(?:s|ning)?\b|spun\b|pellet(?:s|ed|ing)?\b", "centrifuge", False),
    (r"wash(?:es|ed|ing)?\b|rins(?:e|es|ed|ing)\b", "wash", False),
    (r"incubat|hold(?:s|ing)?\b|held\b|stand(?:s|ing)?\b|rest(?:s|ed|ing)?\b|settle", "incubate",
     False),
    # Stopping the flow and letting something sit is a hold; the enum has no "stop".
    (r"stop(?:s|ped|ping)?\b|halt(?:s|ed|ing)?\b|paus(?:e|es|ed|ing)|switch off|turn off",
     "incubate", True),
    (r"amplif(?:y|ies|ied|ying)|thermocycl|pcr\b|reverse[- ]transcrib|anneal|denatur",
     "amplify", False),
    (r"mix(?:es|ed|ing)?\b|vortex|triturat|resuspend|resuspension|agitat|"
     r"pipett?e up and down|invert", "mix", False),
    (r"add(?:s|ed|ing)?\b|dispens|pipett?e(?:s|d)?\b|pipetting\b|appl(?:y|ies|ied|ying)|"
     r"introduc|supplement|overlay|overlaid|top up|inject", "add", False),
    (r"separat|filter(?:s|ed|ing)?\b|strain(?:s|ed|ing)?\b|sort(?:s|ed|ing)?\b|"
     r"elut(?:e|es|ed|ing)|magnet(?:ic)?ally|size[- ]select|"
     r"break(?:s|ing)?\s+(?:the\s+)?emulsion|demulsif|decant", "separate", False),
    (r"imag(?:e|es|ed|ing)|photograph|micrograph|count(?:s|ed|ing)?\b|"
     r"visualis|visualiz|observ|inspect|read (?:the )?(?:plate|absorbance)", "image", False),
    (r"seed(?:s|ed|ing)?\b|plat(?:e|es|ed|ing)\b|cultur(?:e|es|ed|ing)|grow\b|grown\b",
     "seed", False),
    (r"harvest|detach|dissociat|trypsinis|trypsiniz|lift(?:s|ed|ing)?\b", "harvest", False),
    # Loading, co-flowing and perfusing all move liquid in; they are one action, so a
    # sentence that says "loaded ... and co-flowed" is not two steps.
    (r"transfer|load(?:s|ed|ing)?\b|co-?flow\w*|perfus\w*|flow(?:s|ed|ing)?\b|deliver|"
     r"draw off|carry|carried|recover", "transfer", False),
    # Weak mappings: the source's word does not name an action in the enum.
    (r"collect(?:s|ed|ing)?\b|aspirat|remov(?:e|es|ed|ing)|discard", "transfer", True),
    (r"place(?:s|d)?\b|leav(?:e|es|ing)\b|left\b|keep(?:s|ing)?\b|kept\b|stor(?:e|es|ed|ing)",
     "incubate", True),
    (r"quench|neutralis|neutraliz|dilut|replac(?:e|es|ed|ing)", "add", True),
    (r"concentrat\b", "separate", True),
)
ACTION_NAMES = ("add", "incubate", "wash", "centrifuge", "mix", "transfer", "image", "lyse",
                "amplify", "separate", "seed", "harvest")
ROLE_NAMES = ("medium", "buffer", "enzyme", "solvent", "oil", "label")

# Role keywords for reagents: CATEGORY words only, never a product or an enzyme name.
# Matched against the noun phrase head first (English heads sit last), so "staining
# buffer" is a buffer and "antibody solution" is a label.
ROLE_KEYWORDS = (
    (r"^(?:buffer|buffers|saline|pbs|tbs|dpbs|hbss)$", "buffer"),
    (r"^(?:medium|media|serum|fbs|fcs)$", "medium"),
    # "-ase" means an enzyme, except for the English words that happen to end that way:
    # "oil_phase" classified as an enzyme is exactly the sort of silent error that
    # reaches a chip as the wrong viscosity.
    (r"^(?:enzyme|enzymes|(?!phase|base|case|grease|release|increase|decrease|purchase|"
     r"disease|please|cease)\w{4,}ases?)$", "enzyme"),
    (r"^(?:oil|oils|fluorinated|fluorocarbon)$", "oil"),
    (r"^(?:ethanol|methanol|isopropanol|isopropyl|acetone|dmso|chloroform|water)$", "solvent"),
    (r"^(?:dye|dyes|stain|stains|staining|antibody|antibodies|fluorophore|probe|label|"
     r"tracer|fluorescent)$", "label"),
)
# Words that end a noun phrase: a reagent name never runs through one of these.
STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "to", "into", "onto", "in", "on", "at", "for",
    "with", "without", "from", "per", "then", "while", "until", "before", "after", "by",
    "through", "over", "under", "using", "use", "each", "all", "every", "this", "that",
    "these", "those", "is", "are", "was", "were", "be", "been", "it", "its", "as", "so",
    "if", "when", "which", "containing", "pre", "about", "approximately", "approx",
    "times", "x", "min", "mins", "minute", "minutes", "h", "hr", "hrs", "hour", "hours",
    "s", "sec", "secs", "second", "seconds", "overnight", "ice", "room", "temperature",
    "step", "steps", "table", "nominal", "given", "not", "stated", "none", "added",
    "because", "since", "although", "whereas", "both", "immediately", "fresh",
}
# Generic nouns a reagent name may end on, so "lysis buffer", "wash_buffer" and
# "capture_beads" survive while "the cells" does not.
REAGENT_TAILS = {
    "buffer", "solution", "medium", "media", "mix", "mixture", "reagent", "oil",
    "enzyme", "serum", "water", "dye", "stain", "antibody", "saline", "phase",
    "suspension", "cocktail", "mastermix", "supplement", "diluent", "beads", "bead",
}
# Things a step is done TO. Lab language, kept as the source wrote it.
TARGET_NOUNS = (
    "cell suspension", "cell pellet", "cell monolayer", "adherent cells", "cells",
    "capsules", "capsule suspension", "droplets", "emulsion", "beads", "bead pool",
    "nuclei", "pellet", "supernatant", "lysate", "library", "cDNA", "mRNA", "RNA", "DNA",
    "monolayer", "hydrogel", "gel", "column", "filter", "strainer", "membrane",
    "well", "wells", "plate", "dish", "flask", "tube", "vessel", "chamber", "trap",
    "device", "slide", "reaction", "sample", "suspension", "aqueous phase", "oil phase",
    "outlet", "inlet", "port",
)
WORD_NUMBERS = {
    "once": 1, "twice": 2, "thrice": 3, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12,
}

# "repeat steps 3-5 three times", "steps 3 to 5 are repeated twice", "repeat step 4 x3"
REPEAT_DIRECTIVE = re.compile(
    r"(?:repeat|repeated|repeating)\b[^.]*?\bsteps?\s*(?P<lo>\d{1,3})"
    rf"(?:{RANGE_SEP}|\s*(?:and|,)\s*)(?P<hi>\d{{1,3}})?[^.]*?"
    r"(?:(?P<count>\d{1,3}|" + "|".join(WORD_NUMBERS) + r")\s*(?:times|x|\u00d7)|"
    r"(?:times|x|\u00d7)\s*(?P<count2>\d{1,3}))", re.I)
REPEAT_DIRECTIVE_LOOSE = re.compile(
    r"\bsteps?\s*(?P<lo>\d{1,3})(?:" + RANGE_SEP + r")(?P<hi>\d{1,3})\b[^.]*?"
    r"\b(?:repeat|repeated)\b", re.I)
REPEAT_IN_STEP = re.compile(
    r"\b(?P<count>\d{1,3}|" + "|".join(WORD_NUMBERS) + r")\s*"
    r"(?:times|x|\u00d7|passes|cycles|rounds|repeats)\b"
    r"|\b(?:in\s+)?(?P<word>triplicate|duplicate|quadruplicate)\b", re.I)
REPLICATES = {"duplicate": 2, "triplicate": 3, "quadruplicate": 4}
STEP_REFERENCE = re.compile(r"\bstep\s*(?P<n>\d{1,3}[a-z]?)\b", re.I)

# The source's own step numbering, found on the WHOLE block before any sentence
# splitting: "3. Collect the cells" splits after "3." on sentence rules and the label
# would be lost - which is exactly how a "repeat steps 3-5" directive silently fails to
# expand. A letter suffix is kept ("3b"), because the source's irregularity is a fact.
STEP_LABEL = re.compile(
    r"(?:^|(?<=\n)|(?<=[.!?;]\s))\s*(?:step\s*)?\(?(?P<n>\d{1,3}[a-z]?)\)?\s*"
    r"[.):\]\u2013-]\s+(?=[A-Za-z(])", re.I)
# A bare number as a label, allowed only at the start of a line and only before a capital:
# "5 Apply lysis_buffer" is a label, "500 uL of buffer" and "2 mL" are not.
STEP_LABEL_BARE = re.compile(r"(?:^|(?<=\n))\s*(?P<n>\d{1,3}[a-z]?)\s+(?=[A-Z])")
# "Fig. 2). The" looks exactly like a step label, so these never start one.
NOT_A_LABEL = re.compile(r"(?:fig|figs|figure|table|tbl|eq|eqn|ref|no|section|sec)\.?\s*\(?$",
                         re.I)
CAPTION = re.compile(r"^\s*(?:table|fig(?:ure)?|scheme)\s*[A-Za-z]?\d{0,3}[.:]?\s", re.I)
# Headings whose section is context, not method. A block under one of these is not a step
# source, and the skip is recorded rather than silent.
NON_STEP_HEADING = re.compile(
    r"^\s*(?:abstract|summary|introduction|background|materials?|reagents?|equipment|"
    r"supplies|results?|discussion|notes?|notes on \w+|troubleshooting|expected outcome|"
    r"outcome|references?|acknowledge?ments?|author|funding|data availability|"
    r"conclusions?|limitations?)\b", re.I)
CLAUSE_SPLIT = re.compile(r",\s+then\s+|\s+and\s+then\s+|;\s*then\s+|;\s+|,\s+followed by\s+|"
                          r"\.\s+Then\s+|\s+and\s+", re.I)
# A sentence that tells you what was NOT measured, or what not to do. Neither is a step,
# and neither may be dropped silently: both go to `unresolved`, because "the delivered
# rate was not measured" is exactly the sort of thing a later stage must not assume.
CAVEAT = re.compile(
    r"\b(?:not|never|without being)\s+(?:been\s+)?(?:measured|reported|quantified|"
    r"characteris\w*|characteriz\w*|recorded|instrumented|stated|given|verified|"
    r"validated|calibrated|confirmed)\b|\bin appearance only\b|\bwas pasted\b|"
    r"\bno(?:t)? (?:instrumented|data)\b", re.I)
PROHIBITION = re.compile(r"^(?:do not|don'?t|never|avoid|take care not)\b", re.I)
# A sentence ABOUT the parameters rather than an instruction: "Volumes are the volume
# delivered per pass and were set at the pump."
DESCRIPTIVE = re.compile(
    r"^(?:volumes?|flow rates?|rates?|temperatures?|times?|durations?|parameters?|"
    r"values?|concentrations?|set ?points?|the (?:volumes?|times?|temperatures?|"
    r"parameters?|flow rates?))\b[^.]*?\b(?:are|is|were|was)\b", re.I)
# A sentence that finishes the previous one rather than starting a new step: passive or
# nominalised ("Resuspension is done by...", "Incubation was carried out at 4 C").
CONTINUATION_START = re.compile(
    r"^(?:this|that|these|those|the (?:reaction|mixture|incubation|suspension|step|sample|"
    r"pellet|cells|capsules|solution|beads|second|first|third)|all steps?|both steps?|"
    r"[A-Za-z]+(?:ation|ing|sion|ment))\b", re.I)



# --- small helpers ------------------------------------------------------------------


def _norm(text: Any) -> str:
    """Whitespace-normalised, for verbatim substring checks."""
    return re.sub(r"\s+", " ", str(text or "").replace("\u00a0", " ")).strip()


def _num(value: Optional[float]) -> Optional[float]:
    """Round for a stable artefact, and emit an int when the value is integral."""
    if value is None:
        return None
    rounded = round(float(value), 6)
    return int(rounded) if rounded == int(rounded) and abs(rounded) < 1e15 else rounded


def _float(raw: str) -> float:
    return float(str(raw).replace(",", ""))


def _word_count(token: Any) -> Optional[int]:
    token = str(token or "").strip().lower()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    return WORD_NUMBERS.get(token)


def _depunct(text: str) -> str:
    """Parenthetical asides removed, for verb and reagent detection only. `text` in the
    artefact always keeps them."""
    return re.sub(r"\s+", " ", re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", text)).strip()


def _label_sort_key(label: str) -> tuple:
    m = re.match(r"(\d+)([a-z]?)", str(label))
    return (int(m.group(1)), m.group(2)) if m else (10 ** 6, str(label))


_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.", "cf.", "approx.", "ca.", "Fig.", "fig.",
                  "Figs.", "ref.", "No.", "no.", "min.", "sec.", "al.", "Dr.", "wt.", "vol.")


def _sentences(text: str) -> list[str]:
    """Split on sentence ends without being fooled by "0.3 mm" or "e.g."."""
    masked = text
    restore = {}
    for index, abbrev in enumerate(_ABBREVIATIONS):
        if abbrev in masked:
            key = f"\x00{index}\x00"
            masked = masked.replace(abbrev, key)
            restore[key] = abbrev
    masked = re.sub(r"(\d)\.(\d)", "\\1\x01\\2", masked)
    out = []
    for part in re.split(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])", masked):
        part = part.replace("\x01", ".")
        for key, abbrev in restore.items():
            part = part.replace(key, abbrev)
        part = part.strip()
        if part:
            out.append(part)
    return out


def _generic_name(name: str) -> str:
    """A snake_case, number-free form of a source reagent name, for whoever writes a
    COMMITTED chip spec, protocol or doc - those may not carry the source's names,
    concentrations or vendors (CONFIDENTIALITY.md). Offered in `audit`, never substituted
    for what the source actually said."""
    cleaned = re.sub(r"\([^)]*\)", " ", name)
    cleaned = re.sub(r"\b\d[\d.,]*\s*[a-z%/]*\b", " ", cleaned, flags=re.I)
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", cleaned).strip("_").lower()
    return re.sub(r"_+", "_", cleaned)


# --- unit parsing -------------------------------------------------------------------
# Every parser returns the ORIGINAL string beside the parsed number, and a range rather
# than a silently chosen end.


def _inside(pos: int, spans: list) -> bool:
    return any(lo <= pos < hi for lo, hi in spans)


def parse_volumes(text: str) -> list[dict]:
    """Every volume in `text`, in source order, normalised to uL.

    Each entry: {text, volume_ul, range_ul, start, end}. A range keeps both ends and
    leaves `volume_ul` None: picking one end quietly is how a chip ends up sized for a
    number the paper never gave.
    """
    out = []
    ranges = list(VOLUME_RANGE.finditer(text))
    for m in ranges:
        unit = VOLUME_ALIASES[m.group("unit").lower()]
        lo_unit = VOLUME_ALIASES[m.group("lounit").lower()] if m.group("lounit") else unit
        lo = _float(m.group("lo")) * VOLUME_UL[lo_unit]
        hi = _float(m.group("hi")) * VOLUME_UL[unit]
        out.append({"text": m.group(0).strip(), "volume_ul": None,
                    "range_ul": [_num(min(lo, hi)), _num(max(lo, hi))],
                    "start": m.start(), "end": m.end()})
    taken = [(m.start(), m.end()) for m in ranges]
    for m in VOLUME_ONE.finditer(text):
        if _inside(m.start(), taken):
            continue
        unit = VOLUME_ALIASES[m.group("unit").lower()]
        out.append({"text": m.group(0).strip(),
                    "volume_ul": _num(_float(m.group("num")) * VOLUME_UL[unit]),
                    "range_ul": None, "start": m.start(), "end": m.end()})
    return sorted(out, key=lambda v: v["start"])


def parse_duration(text: str) -> dict:
    """The duration in `text`: {text, duration_s, range_s, qualitative}.

    A range and a qualitative phrase both leave `duration_s` None.
    """
    blank = {"text": None, "duration_s": None, "range_s": None, "qualitative": None}
    plain = _depunct(text)
    m = TIME_RANGE.search(plain)
    if m:
        unit = TIME_ALIASES[m.group("unit").lower()]
        lo_unit = TIME_ALIASES[m.group("lounit").lower()] if m.group("lounit") else unit
        lo = _float(m.group("lo")) * TIME_S[lo_unit]
        hi = _float(m.group("hi")) * TIME_S[unit]
        return {"text": m.group(0).strip(), "duration_s": None,
                "range_s": [_num(min(lo, hi)), _num(max(lo, hi))], "qualitative": None}
    hits = list(TIME_ONE.finditer(plain))
    preferred = None
    for m in hits:
        # A "for X" duration beats a bare number ("centrifuge at 300 g for 5 min").
        if re.search(r"\b(?:for|over)\s*$", plain[max(0, m.start() - 8):m.start()], re.I):
            preferred = m
            break
    chosen = preferred or (hits[0] if hits else None)
    if chosen:
        unit = TIME_ALIASES[chosen.group("unit").lower()]
        return {"text": chosen.group(0).strip(),
                "duration_s": _num(_float(chosen.group("num")) * TIME_S[unit]),
                "range_s": None, "qualitative": None}
    for pattern, label in QUALITATIVE_TIME:
        m = re.search(pattern, plain, re.I)
        if m:
            return {"text": m.group(0).strip(), "duration_s": None, "range_s": None,
                    "qualitative": label or _norm(m.group(0)).lower()}
    return blank


def parse_temperature(text: str) -> dict:
    """The temperature in `text`: {text, temp_c, range_c, qualitative}."""
    plain = _depunct(text)
    m = TEMP_RANGE.search(plain)
    if m:
        lo, hi = _float(m.group("lo")), _float(m.group("hi"))
        return {"text": m.group(0).strip(), "temp_c": None,
                "range_c": [_num(min(lo, hi)), _num(max(lo, hi))], "qualitative": None}
    m = TEMP_ONE.search(plain)
    if m:
        return {"text": m.group(0).strip(), "temp_c": _num(_float(m.group("num"))),
                "range_c": None, "qualitative": None}
    for pattern, label in QUALITATIVE_TEMP:
        m = re.search(pattern, plain, re.I)
        if m:
            return {"text": m.group(0).strip(), "temp_c": None, "range_c": None,
                    "qualitative": label}
    return {"text": None, "temp_c": None, "range_c": None, "qualitative": None}


def _ambiguous_rt(text: str) -> bool:
    """A bare "RT": room temperature, or reverse transcription? Never guessed."""
    return bool(RT_TOKEN.search(_depunct(text))) and not re.search(
        r"room temperature|reverse[- ]transcri", text, re.I)


# --- action, target, reagents -------------------------------------------------------


def classify_action(text: str) -> tuple[Optional[str], bool, Optional[str]]:
    """(action, weak, the source's word). The earliest verb wins, so "Add 100 uL lysis
    buffer" is `add` and not `lyse`; ties go to the more specific lexicon entry."""
    plain = _depunct(text)
    best = None
    for order, (pattern, action, weak) in enumerate(ACTIONS):
        m = re.search(pattern, plain, re.I)
        if m and (best is None or (m.start(), order) < (best[0], best[1])):
            best = (m.start(), order, action, weak, m.group(0))
    if best is None:
        return None, False, None
    return best[2], best[3], best[4]


def _verb_position(text: str) -> int:
    """Where the action verb sits. An instruction opens with it; a description does not."""
    plain = _depunct(text)
    positions = [m.start() for pattern, _, _ in ACTIONS
                 for m in [re.search(pattern, plain, re.I)] if m]
    return min(positions) if positions else len(plain)


def find_target(text: str) -> Optional[str]:
    """What the step is done to, in the source's words. None when it is not stated."""
    plain = _depunct(text)
    candidates = []
    after_prep = re.search(
        r"\b(?:to|into|onto|from|in|on|through|of)\s+the\s+([A-Za-z][\w-]*(?:\s+[a-z][\w-]*)?)",
        plain)
    if after_prep:
        candidates.append((after_prep.start(1), after_prep.group(1)))
    for noun in TARGET_NOUNS:
        m = re.search(rf"\b{re.escape(noun)}\b", plain, re.I)
        if m:
            candidates.append((m.start(), m.group(0)))
    for _, phrase in sorted(candidates):
        low = phrase.lower().strip()
        if low in STOPWORDS or low in REAGENT_TAILS:
            continue
        for noun in TARGET_NOUNS:
            if re.fullmatch(rf"{re.escape(noun)}s?", low, re.I) or noun.lower() in low:
                return phrase.strip()
    return None


def _role(name: str) -> tuple[Optional[str], Optional[str]]:
    """(role, the word it was decided by). Scans the noun phrase head first, and splits
    snake_case, so "wash_buffer" is a buffer."""
    tokens = [t for t in re.split(r"[\s/()\-_+]+", name.lower()) if t]
    for token in reversed(tokens):
        for pattern, role in ROLE_KEYWORDS:
            if re.match(pattern, token):
                return role, token
    return None, None


def _is_reagent_word(word: str) -> bool:
    """A single token that names a reagent: "wash_buffer", "oil_phase", "lysis_buffer"."""
    parts = [p for p in re.split(r"[\-_]+", word.lower()) if p]
    if len(parts) < 2:
        return False
    return parts[-1] in REAGENT_TAILS or _role(parts[-1])[0] is not None


def _noun_phrase(text: str, start: int) -> Optional[str]:
    """Up to four tokens of a reagent name from `start`, stopping at a stopword."""
    tail = text[start:]
    tokens: list[tuple[str, int]] = []
    for m in re.finditer(r"[A-Za-z0-9][\w\-+%.']*", tail):
        if m.start() > (tokens[-1][1] + 2 if tokens else 1):
            break
        word = m.group(0)
        if word.lower() in STOPWORDS:
            break
        tokens.append((word, m.end()))
        if len(tokens) >= 4 or word.lower() in REAGENT_TAILS or _is_reagent_word(word):
            break
    if not tokens:
        return None
    return " ".join(word for word, _ in tokens)


def _split_reagent_cell(cell: str) -> list[str]:
    """A table cell naming several reagents -> one name each.

    "capture_beads in bead_buffer" and "cell_suspension + capture_beads + oil_phase" are
    three reagents co-flowed, not one reagent with a long name.
    """
    parts = [p.strip(" ,;") for p in re.split(r"\s*\+\s*|\s+(?:in|and|with|plus)\s+", cell)]
    names = [p for p in parts if p and p.lower() not in STOPWORDS]
    return names or [cell]


def find_reagents(text: str) -> list[dict]:
    """Reagents in the SOURCE'S OWN words, each with the volume written beside it.

    Three honest heuristics and no product list: a noun phrase after a volume ("100 uL of
    X"), a single snake_case token whose head is a category word ("wash_buffer"), and a
    phrase whose last word is one ("lysis buffer"). See the confidentiality note in the
    module docstring for why there is no lexicon of names here.
    """
    plain = _depunct(text)
    found: list[dict] = []

    def add(name: str, pos: int, volume: Optional[dict]) -> None:
        name = name.strip(" ,.;:|")
        if not name or name.lower() in STOPWORDS:
            return
        # A name that starts with a verb is the instruction, not the reagent: "Resuspend
        # beads" and "Wash bead pool" are operations, and letting them through fills
        # `unresolved` with questions about reagents that do not exist.
        words = name.split()
        if len(words) > 4:
            return
        # Strip a leading verb: "diluted in medium" is the instruction plus the reagent,
        # and the reagent is "medium". Never applied to a one-token name, because
        # "lysis_buffer" is a reagent and not the verb "lyse".
        while len(words) > 1 and classify_action(words[0])[0] is not None:
            words = words[1:]
        name = " ".join(words)
        role = _role(name)[0]
        if len(words) == 1 and role is None and not _is_reagent_word(name) and \
                name.lower() in REAGENT_TAILS and volume is None:
            # A bare category word with no volume and no role ("beads", "phase") is not a
            # reagent this stage can honestly name; S2 handles particles.
            return
        for existing in found:
            if existing["name"].lower() == name.lower():
                if volume and existing["volume_ul"] is None and existing["range_ul"] is None:
                    existing.update(volume_ul=volume["volume_ul"], range_ul=volume["range_ul"],
                                    volume_text=volume["text"])
                return
        role, word = _role(name)
        found.append({"name": name, "pos": pos, "role": role, "role_word": word,
                      "volume_ul": volume["volume_ul"] if volume else None,
                      "range_ul": volume["range_ul"] if volume else None,
                      "volume_text": volume["text"] if volume else None,
                      "from": "prose"})

    for vol in parse_volumes(plain):
        lead = re.match(r"\s*(?:of\s+)?", plain[vol["end"]:])
        offset = vol["end"] + (lead.end() if lead else 0)
        name = _noun_phrase(plain, offset)
        if name:
            add(name, offset, vol)
    for m in re.finditer(r"[A-Za-z][\w\-]*", plain):
        if _is_reagent_word(m.group(0)):
            add(m.group(0), m.start(), None)
    for tail in sorted(REAGENT_TAILS):
        for m in re.finditer(rf"\b([\w-]+\s+)?([\w-]+\s+)?{re.escape(tail)}\b", plain, re.I):
            words = [w for w in _norm(m.group(0)).split() if w.lower() not in STOPWORDS
                     or w.lower() == tail]
            if words:
                add(" ".join(words[-3:]), m.start(), None)
    return _dedupe_reagents(found)


def _dedupe_reagents(found: list[dict]) -> list[dict]:
    """One reagent, one entry. "oil" inside "oil phase", or "buffer" beside the
    "wash_buffer" a table or a model named, is the same thing said shorter - and two
    entries for it would be two spurious questions about a missing volume."""
    kept = []
    for reagent in sorted(found, key=lambda r: r["pos"]):
        name = reagent["name"].lower()
        longer = next((other for other in found
                       if other is not reagent
                       and (re.search(rf"\b{re.escape(name)}\b", other["name"].lower())
                            or name in re.split(r"[\s/()\-_+]+", other["name"].lower()))
                       and len(other["name"]) > len(reagent["name"])), None)
        if longer is not None:
            if reagent["volume_ul"] is not None and longer["volume_ul"] is None:
                longer.update(volume_ul=reagent["volume_ul"], range_ul=reagent["range_ul"],
                              volume_text=reagent["volume_text"])
            continue
        kept.append(reagent)
    return kept


# --- raw.json -----------------------------------------------------------------------


def _block_text(block: dict) -> str:
    text = block.get("text")
    if isinstance(text, str) and text.strip():
        return text
    rows = block.get("rows")
    if isinstance(rows, list):
        return "\n".join(" | ".join(str(cell) for cell in row)
                         if isinstance(row, (list, tuple)) else str(row) for row in rows)
    return text if isinstance(text, str) else ""


def _blocks(raw: dict) -> list[dict]:
    blocks = raw.get("blocks")
    if not isinstance(blocks, list):
        raise ValueError("raw.json has no 'blocks' list; S0 ingest has to run first")
    return [b for b in blocks if isinstance(b, dict)]


def block_flagged(block: dict) -> bool:
    """Did S0 flag this block as one a human has to check?

    S0's own convention is `needs_attention` plus a `note`; the other spellings are
    accepted because a flag that is missed here becomes a number that reads as fact.
    """
    if block.get("needs_attention") or block.get("unparsed") or block.get("parse_failed") \
            or block.get("parse_error") or block.get("truncated"):
        return True
    if block.get("parsed") is False or block.get("ok") is False:
        return True
    flags = block.get("flags") or block.get("warnings") or []
    if isinstance(flags, str):
        flags = [flags]
    if isinstance(flags, (list, tuple)) and any(
            re.search(r"unparsed|could not parse|parse fail|unreadable|check", str(f), re.I)
            for f in flags):
        return True
    note = block.get("note")
    if isinstance(note, str) and re.search(r"unparsed|could not parse|needs checking|"
                                           r"unverified|irregular", note, re.I):
        return True
    return block.get("kind") == "table" and not _block_text(block).strip()


def block_locator(block: dict, index: int) -> Any:
    """`quote_page` for a block: the page number, or a locator string. Never null - the
    schema forbids it, and a quote nobody can find is not a citation."""
    page = block.get("page")
    if isinstance(page, int):
        return page
    if isinstance(page, str) and page.strip():
        return page.strip()
    marker = block.get("block", index)
    line = block.get("line")
    return f"block {marker}" + (f", line {line}" if isinstance(line, int) else "")


def _where(locator: Any) -> str:
    """A locator as English: "page 12", or the locator string for a source with no pages."""
    return f"page {locator}" if isinstance(locator, int) else str(locator)


def _is_caption(text: str) -> bool:
    return bool(CAPTION.match(_norm(text)))


def _is_pseudo_heading(block: dict) -> bool:
    """A short bare line acting as a heading ("Notes", "Device operation"), which S0
    emits as a paragraph because the source gave it no markup."""
    if block.get("kind") not in ("paragraph", None):
        return False
    text = _norm(_block_text(block))
    return bool(text) and len(text.split()) <= 4 and not text.endswith((".", ":", ";")) \
        and not _is_caption(text)


def _context(blocks: list[dict]) -> dict:
    """Document-level facts a single step needs."""
    flagged = [index for index, block in enumerate(blocks) if block_flagged(block)]
    tables = [index for index, block in enumerate(blocks)
              if block.get("kind") == "table" and block_flagged(block)]
    return {
        "flagged_blocks": len(flagged),
        "flagged_tables": len(tables),
        "flagged_table_pages": sorted({str(blocks[i].get("page")) for i in tables}),
    }


# --- parameter tables ---------------------------------------------------------------
# The fixtures keep their numbers in tables, keyed by step. The unit comes from the
# COLUMN HEADER ("Volume (uL)", "Time (min)"); a column with no unit is a question.

UNIT_IN_HEADER = re.compile(r"[(\[]\s*([^)\]]+?)\s*[)\]]")
# A footnote marker is a symbol or a lone letter. NOT a bare digit: "2" in a Time column
# is two minutes, and reading it as a marker would quietly delete the value.
FOOTNOTE_MARKER = re.compile(r"^(?:\*{1,3}|\u2020{1,2}|\u2021|\u00a7|\u00b6|[a-z]|\(\w\)|[a-z]\))$")
MISSING_CELL = re.compile(r"^(?:|-+|\u2013|\u2014|n/?a|nd|not stated|not given|not recorded|"
                          r"unknown|\?)$", re.I)
NO_REAGENT_CELL = re.compile(r"^(?:none|none added|no reagent|-+|n/?a)$", re.I)


def _column_kind(header: str) -> str:
    text = _norm(header).lower()
    if re.match(r"^(?:step|pass|stage|phase|no\.?|#|cycle)\b", text):
        return "key"
    if re.search(r"operation|action|description|procedure|what", text):
        return "operation"
    if re.search(r"reagent|solution|buffer|fluid|medium", text):
        return "reagent"
    if re.search(r"volume|\bvol\b", text):
        return "volume"
    if re.search(r"time|duration|dwell|hold", text):
        return "duration"
    if re.search(r"temp", text):
        return "temperature"
    if re.search(r"flow|rate", text):
        return "flow"
    if re.search(r"note|comment", text):
        return "note"
    return "other"


def _header_unit(header: str) -> Optional[str]:
    m = UNIT_IN_HEADER.search(header or "")
    return _norm(m.group(1)) if m else None


def _footnote_for(marker: str, footnotes: list) -> Optional[str]:
    for note in footnotes or []:
        body = _norm(note)
        stripped = re.sub(r"^footnote:\s*", "", body, flags=re.I)
        if stripped.startswith(marker):
            return body
    return None


def _cell_number(kind: str, header: str, cell: str) -> dict:
    """One table cell -> {state, value, text, unit, unit_from}.

    `state` is "value", "missing", "footnote" or "no_unit". The cell's own unit wins over
    the header's, and both are recorded.
    """
    raw = _norm(cell)
    out = {"state": "value", "value": None, "text": raw, "unit": None, "unit_from": None}
    if MISSING_CELL.match(raw):
        out["state"] = "missing"
        return out
    if FOOTNOTE_MARKER.match(raw) and kind in ("volume", "duration", "temperature", "flow"):
        out["state"] = "footnote"
        return out
    own = {"volume": parse_volumes(raw), "duration": [parse_duration(raw)],
           "temperature": [parse_temperature(raw)]}.get(kind) or []
    if kind == "volume" and own and own[0]["volume_ul"] is not None:
        out.update(value=own[0]["volume_ul"], unit="uL", unit_from="cell")
        return out
    if kind == "duration" and own and own[0]["duration_s"] is not None:
        out.update(value=own[0]["duration_s"], unit="s", unit_from="cell")
        return out
    if kind == "temperature" and own and own[0]["temp_c"] is not None:
        out.update(value=own[0]["temp_c"], unit="C", unit_from="cell")
        return out
    number = re.match(rf"^({NUMBER})$", raw)
    if not number:
        out["state"] = "missing"
        return out
    unit = (_header_unit(header) or "").lower().split("/")[0].strip()
    out["unit_from"] = f"header {header!r}"
    if kind == "volume":
        key = VOLUME_ALIASES.get(unit)
        if key is None:
            out["state"] = "no_unit"
            return out
        out.update(value=_num(_float(number.group(1)) * VOLUME_UL[key]), unit=unit)
    elif kind == "duration":
        key = TIME_ALIASES.get(unit)
        if key is None:
            out["state"] = "no_unit"
            return out
        out.update(value=_num(_float(number.group(1)) * TIME_S[key]), unit=unit)
    elif kind == "temperature":
        if not re.fullmatch(r"\u00b0?\s*c|deg\s*c|celsius", unit, re.I):
            out["state"] = "no_unit"
            return out
        out.update(value=_num(_float(number.group(1))), unit=unit)
    else:
        out.update(value=_num(_float(number.group(1))), unit=unit or None)
    return out


def parameter_tables(blocks: list[dict]) -> list[dict]:
    """Every table that enumerates steps or passes, parsed into rows keyed by label.

    Returns [{block_index, page, locator, key_header, key_kind, caption, flagged, note,
              footnotes, rows: {label: {kind: cell, ...}}, order: [labels], headers}].
    """
    out = []
    for index, block in enumerate(blocks):
        if block.get("kind") != "table":
            continue
        rows = block.get("rows")
        if not isinstance(rows, list) or not rows:
            continue
        headers = block.get("columns")
        body = rows
        if not isinstance(headers, list) or not headers:
            headers, body = rows[0], rows[1:]
        if not isinstance(headers, list) or not body:
            continue
        kinds = [_column_kind(str(h)) for h in headers]
        if "key" not in kinds:
            continue
        key_at = kinds.index("key")
        caption = None
        for back in range(index - 1, max(-1, index - 3), -1):
            text = _norm(_block_text(blocks[back]))
            if _is_caption(text):
                caption = text
                break
        parsed: dict[str, dict] = {}
        order: list[str] = []
        lines = _block_text(block).split("\n")
        for position, row in enumerate(body):
            if not isinstance(row, (list, tuple)) or not row:
                continue
            label = _norm(row[key_at])
            if not label or not re.match(r"^\d{1,3}[a-z]?$", label):
                continue
            cells: dict[str, Any] = {}
            for column, (header, kind) in enumerate(zip(headers, kinds)):
                if column >= len(row):
                    continue
                value = _norm(row[column])
                if kind in ("volume", "duration", "temperature", "flow"):
                    cells[kind] = dict(_cell_number(kind, str(header), value),
                                       header=_norm(header))
                elif kind in ("operation", "reagent", "note"):
                    cells[kind] = {"text": value, "header": _norm(header)}
            verbatim = next((line.strip() for line in lines
                             if re.match(rf"^\s*\|?\s*{re.escape(label)}\s*\|", line)), None)
            parsed[label] = {"label": label, "cells": cells, "row": list(row),
                             "verbatim": verbatim, "position": position}
            order.append(label)
        if not parsed:
            continue
        out.append({"block_index": index, "page": block.get("page"),
                    "locator": block_locator(block, index), "headers": [_norm(h) for h in headers],
                    "key_header": _norm(headers[key_at]), "key_kind": _column_kind(
                        str(headers[key_at])),
                    "keys_are_steps": bool(re.match(r"^step", _norm(headers[key_at]), re.I)),
                    "caption": caption, "flagged": block_flagged(block),
                    "note": block.get("note"), "footnotes": block.get("footnotes") or [],
                    "rows": parsed, "order": order})
    return out


# --- candidates ---------------------------------------------------------------------


class Candidate:
    """One verbatim chunk of source that looks like a step, before classification."""

    __slots__ = ("text", "locator", "block_index", "block_kind", "source_label", "order",
                 "sentence_count", "flagged", "s0_note", "clause", "split", "repeat",
                 "action", "weak", "verb", "table", "model")

    def __init__(self, text: str, locator: Any, block_index: int, block_kind: str,
                 source_label: Optional[str] = None, flagged: bool = False,
                 s0_note: Optional[str] = None, order: tuple = (0, 0)):
        self.text = text
        self.locator = locator
        self.block_index = block_index
        self.block_kind = block_kind
        self.source_label = source_label
        self.order = order
        self.sentence_count = 1
        self.flagged = flagged
        self.s0_note = s0_note
        self.clause = None
        self.split = None
        self.repeat = None
        self.action = None
        self.weak = False
        self.verb = None
        self.table = None
        self.model = None

    def copy(self) -> "Candidate":
        other = Candidate(self.text, self.locator, self.block_index, self.block_kind,
                          self.source_label, self.flagged, self.s0_note, self.order)
        other.sentence_count = self.sentence_count
        other.clause = self.clause
        other.split = dict(self.split) if self.split else None
        other.repeat = dict(self.repeat) if self.repeat else None
        other.action, other.weak, other.verb = self.action, self.weak, self.verb
        other.table = dict(self.table) if self.table else None
        other.model = dict(self.model) if self.model else None
        return other


def _label_chunks(text: str) -> list[tuple[Optional[str], str]]:
    """Split a block on the source's own step numbering: [(label, verbatim text), ...].

    Done before sentence splitting, and never on a figure or table cross-reference.
    """
    marks = [m for m in STEP_LABEL.finditer(text)
             if not NOT_A_LABEL.search(text[max(0, m.start() - 10):m.start()])]
    if not marks:
        marks = [m for m in STEP_LABEL_BARE.finditer(text)
                 if not NOT_A_LABEL.search(text[max(0, m.start() - 10):m.start()])]
    if not marks:
        return [(None, text)]
    out: list[tuple[Optional[str], str]] = []
    head = text[:marks[0].start()].strip()
    if head:
        out.append((None, head))
    for position, mark in enumerate(marks):
        end = marks[position + 1].start() if position + 1 < len(marks) else len(text)
        body = text[mark.end():end].strip()
        if body:
            out.append((mark.group("n"), body))
    return out


def _segment(blocks: list[dict],
             title: str = "") -> tuple[list[Candidate], list[dict], list[str], list[dict]]:
    """Source blocks -> candidate steps, repeat directives, notes and questions.

    Headings and captions are context, not steps, and so is everything under a heading
    like "Abstract", "Materials" or "Notes on operation" - that skip is recorded in
    `extraction.notes` rather than being silent. Sentences that continue the previous one
    are merged into it, still verbatim, and `sentence_count` records the join.
    """
    candidates: list[Candidate] = []
    directives: list[dict] = []
    notes: list[str] = []
    questions: list[dict] = []
    section: Optional[str] = None
    skipped: dict[str, int] = {}
    for index, block in enumerate(blocks):
        kind = (block.get("kind") or "paragraph").lower()
        text = _block_text(block)
        locator = block_locator(block, index)
        flagged = block_flagged(block)
        if flagged and block.get("note"):
            notes.append(f"S0 flagged block {block.get('block', index)} "
                         f"({kind}, {_where(locator)}): {_norm(block['note'])}")
            questions.append({"step": None, "field": "confidence", "question":
                              f"S0 flagged the {kind} at {_where(locator)} as needing attention "
                              f"({_norm(block['note'])[:160]}); everything this stage took from "
                              f"it is capped at confidence {FLAGGED_CEILING}"})
        if kind == "heading" or _is_pseudo_heading(block):
            section = _norm(text)
            continue
        if not text.strip():
            continue
        if title and _norm(text) == _norm(title):
            # A PDF's title line arrives as an ordinary paragraph; it is not a step, even
            # though "wash of encapsulated cells" parses as one.
            continue
        if kind == "table":
            continue  # tables are read by parameter_tables(), not sentence by sentence
        if _is_caption(text):
            continue
        if section and NON_STEP_HEADING.match(section):
            skipped[section] = skipped.get(section, 0) + 1
            continue
        chunks = _label_chunks(text)
        if isinstance(block.get("items"), list) and len(block["items"]) > 1 and len(chunks) <= 1:
            chunks = [(None, _norm(item)) for item in block["items"] if _norm(item)]
        for chunk_at, (label, chunk) in enumerate(chunks):
            for position, piece in enumerate(_sentences(chunk)):
                piece = piece.strip()
                if not piece:
                    continue
                piece_label = label if position == 0 else None
                directive = REPEAT_DIRECTIVE.search(piece) or REPEAT_DIRECTIVE_LOOSE.search(piece)
                if directive and re.search(r"\brepeat", piece, re.I):
                    count = _word_count(directive.groupdict().get("count")) or \
                        _word_count(directive.groupdict().get("count2"))
                    directives.append({"text": piece, "locator": locator,
                                       "block_index": index, "lo": directive.group("lo"),
                                       "hi": directive.groupdict().get("hi")
                                       or directive.group("lo"), "count": count})
                    continue
                action, weak, verb = classify_action(piece)
                if PROHIBITION.match(piece):
                    notes.append(f"the source warns {_norm(piece)[:70]!r} ({_where(locator)}); S1 has no "
                                 f"field for a constraint, so it is not a step")
                    questions.append({"step": None, "question":
                                      f"the source warns: {_norm(piece)!r} ({_where(locator)}). It is "
                                      f"NOT a step and S1 has nowhere to put it - a later stage "
                                      f"has to honour it some other way"})
                    continue
                if DESCRIPTIVE.match(piece) and not CAVEAT.search(piece):
                    notes.append(f"a sentence about the parameters rather than an instruction was "
                                 f"read as context, not a step ({_where(locator)}): "
                                 f"{_norm(piece)[:70]!r}")
                    continue
                if CAVEAT.search(piece) and not parse_volumes(_depunct(piece)):
                    notes.append(f"the source says something was not measured or not reported "
                                 f"({_where(locator)}); recorded as a gap, not a step")
                    questions.append({"step": None, "question":
                                      f"the source states a limit on its own data: "
                                      f"{_norm(piece)!r} ({_where(locator)}); no later stage may treat "
                                      f"that quantity as known"})
                    continue
                merge = False
                if candidates and candidates[-1].block_index == index:
                    if action is None and (CONTINUATION_START.match(piece)
                                           or parse_duration(piece)["text"]
                                           or parse_temperature(piece)["text"]):
                        merge = True
                    elif (action is not None and piece_label is None
                          and CONTINUATION_START.match(piece)
                          and action == candidates[-1].action
                          and classify_action(piece.split()[0])[0] is None):
                        # Described, not instructed: it continues the previous step, does
                        # not open with a verb, and names the same action.
                        merge = True
                if merge:
                    previous = candidates[-1]
                    previous.text = f"{previous.text} {piece}".strip()
                    previous.sentence_count += 1
                    continue
                candidate = Candidate(piece, locator, index, kind, piece_label, flagged,
                                      _norm(block.get("note")) or None,
                                      (index, chunk_at, position))
                candidate.action, candidate.weak, candidate.verb = action, weak, verb
                candidates.append(candidate)
    for heading, count in sorted(skipped.items()):
        notes.append(f"{count} block(s) under {heading!r} were read as context, not steps")
        questions.append({"step": None, "question":
                          f"{count} block(s) under the heading {heading!r} were NOT turned into "
                          f"steps because the heading says they are not method; if any of them "
                          f"is a step, it is missing from this file"})
    return candidates, directives, notes, questions


def _split_two_actions(candidates: list[Candidate]) -> tuple[list[Candidate], list[str]]:
    """One sentence, two actions -> two steps. Both keep the WHOLE verbatim sentence as
    `text`; the clause each covers goes to `audit.split.clause`."""
    out: list[Candidate] = []
    notes: list[str] = []
    for candidate in candidates:
        if candidate.action is None or candidate.table:
            out.append(candidate)
            continue
        classified: list[tuple] = []
        for clause in [c.strip() for c in CLAUSE_SPLIT.split(candidate.text) if c and c.strip()]:
            action, weak, verb = classify_action(clause)
            if not action:
                continue
            if classified and classified[-1][1] == action:
                # Neighbouring clauses that name the SAME action are one step. "washed
                # three times ...; each wash is one cycle of step 7" is one wash, and
                # emitting a second empty wash step would overstate the protocol.
                previous = classified[-1]
                classified[-1] = (f"{previous[0]} {clause}", action, previous[2] and weak,
                                  previous[3])
                continue
            classified.append((clause, action, weak, verb))
        if len(classified) < 2:
            out.append(candidate)
            continue
        for position, (clause, action, weak, verb) in enumerate(classified, start=1):
            piece = candidate.copy()
            piece.action, piece.weak, piece.verb = action, weak, verb
            piece.clause = clause
            piece.split = {"of": len(classified), "index": position, "clause": clause}
            out.append(piece)
        notes.append(f"one source sentence describing {len(classified)} actions was split into "
                     f"{len(classified)} steps: {_norm(candidate.text)[:70]}...")
    return out, notes


def _expand_repeats(candidates: list[Candidate],
                    directives: list[dict]) -> tuple[list[Candidate], list[str], list[dict]]:
    """Expand "repeat steps 3-5 three times" into real steps.

    "three times" does not say whether the first pass counts, so this expands to a TOTAL
    of three passes (the common reading) and raises a question naming the other reading.
    Every copy says in `audit.repeat` and `notes` that it was expanded, so nobody mistakes
    it for a sentence the source wrote three times.
    """
    notes: list[str] = []
    questions: list[dict] = []
    for directive in directives:
        lo, hi, count = directive["lo"], directive["hi"], directive["count"]
        try:
            lo_i, hi_i = int(lo), int(hi)
        except (TypeError, ValueError):
            continue
        labels = {str(n) for n in range(min(lo_i, hi_i), max(lo_i, hi_i) + 1)}
        block = [c for c in candidates if c.source_label in labels]
        if not block or not count:
            why = ("they are not numbered steps in this source" if not block
                   else "it gives no count")
            notes.append(f"a repeat directive at {_where(directive['locator'])} names steps {lo}-{hi} "
                         f"but {why}; it was NOT expanded")
            questions.append({"step": None, "question":
                              f"repeat directive {_norm(directive['text'])!r}: {why}, so the "
                              f"repeats are NOT in this file - which steps repeat, how often?"})
            continue
        last = max(candidates.index(c) for c in block)
        expanded: list[Candidate] = []
        for pass_index in range(2, count + 1):
            for source in block:
                copy = source.copy()
                copy.repeat = {"kind": "expanded_directive", "passes": count,
                               "pass_index": pass_index,
                               "source_steps": sorted(labels, key=_label_sort_key),
                               "directive": _norm(directive["text"]),
                               "directive_at": directive["locator"], "expanded": True}
                expanded.append(copy)
        for source in block:
            source.repeat = {"kind": "expanded_directive", "passes": count, "pass_index": 1,
                             "source_steps": sorted(labels, key=_label_sort_key),
                             "directive": _norm(directive["text"]),
                             "directive_at": directive["locator"], "expanded": True}
        candidates = candidates[:last + 1] + expanded + candidates[last + 1:]
        notes.append(f"expanded {_norm(directive['text'])!r} ({_where(directive['locator'])}): source "
                     f"steps {lo}-{hi} repeated to {count} total passes, adding "
                     f"{len(expanded)} steps")
        questions.append({"step": None, "question":
                          f"{_norm(directive['text'])!r} was expanded as {count} TOTAL passes; "
                          f"if it means {count} passes IN ADDITION to the first, every repeated "
                          f"step is one pass short - which reading is right?"})
    return candidates, notes, questions


# --- tables onto steps --------------------------------------------------------------


def _table_rows_as_candidates(tables: list[dict], used: dict) -> tuple[list[Candidate], list[str],
                                                                      list[dict]]:
    """Rows of a table that IS the method -> steps.

    Only for a table whose rows were not matched to prose steps. The action comes from an
    Operation column, or from the table's caption ("Table 2. Wash passes"), or from the
    row's own shape - and in the last two cases it is weak, noted and questioned, because
    a table does not contain a verb.
    """
    out: list[Candidate] = []
    notes: list[str] = []
    questions: list[dict] = []
    for table in tables:
        matched = used.get(table["block_index"], set())
        unmatched = [label for label in table["order"] if label not in matched]
        if not unmatched:
            continue
        if matched:
            # The table parameterises prose steps; a row with no prose step is a gap in
            # the source, not licence to invent a step.
            for label in unmatched:
                row = table["rows"][label]
                questions.append({"step": None, "question":
                                  f"the table at {_where(table['locator'])} has a row for step {label} "
                                  f"({_norm(row['verbatim'] or row['row'])[:90]}) but no prose "
                                  f"step {label} was found; is it a step?"})
            notes.append(f"{len(unmatched)} row(s) of the table at {_where(table['locator'])} name steps "
                         f"with no prose step; they are questions, not steps")
            continue
        caption_action = classify_action(table["caption"] or "")[0]
        for label in unmatched:
            row = table["rows"][label]
            cells = row["cells"]
            operation = _norm((cells.get("operation") or {}).get("text"))
            reagent_text = _norm((cells.get("reagent") or {}).get("text"))
            action, weak, verb = (None, False, None)
            source_of_action = None
            if operation:
                action, weak, verb = classify_action(operation)
                source_of_action = f"the {cells['operation']['header']!r} column"
            if action is None and caption_action:
                action, weak, verb = caption_action, True, classify_action(
                    table["caption"])[2]
                source_of_action = f"the table caption {table['caption']!r}"
            if action is None:
                has_time = (cells.get("duration") or {}).get("value") is not None
                has_reagent = bool(reagent_text) and not NO_REAGENT_CELL.match(reagent_text)
                if has_reagent:
                    action, weak, source_of_action = "add", True, "the row having a reagent"
                elif has_time:
                    action, weak, source_of_action = "incubate", True, \
                        "the row being a timed hold with no reagent"
            if action is None:
                questions.append({"step": None, "field": "action", "question":
                                  f"row {label} of the table at {_where(table['locator'])} "
                                  f"({_norm(row['verbatim'] or row['row'])[:90]}) names no action "
                                  f"this stage can map to the enum; it is NOT in `steps`"})
                continue
            text = row["verbatim"] or " | ".join(str(c) for c in row["row"])
            candidate = Candidate(_norm(text), table["locator"], table["block_index"], "table",
                                  label, table["flagged"], _norm(table["note"]) or None,
                                  (table["block_index"], 0, row["position"]))
            candidate.action, candidate.weak, candidate.verb = action, weak, verb
            candidate.table = dict(table_ref(table), row=label, cells=cells,
                                   action_from=source_of_action, is_the_method=True)
            out.append(candidate)
        if out:
            notes.append(f"the table at {_where(table['locator'])} IS the method here: "
                         f"{len(unmatched)} row(s) became steps, with the action taken from "
                         f"the Operation column, the caption or the row's own shape")
            questions.append({"step": None, "field": "action", "question":
                              f"steps from the table at {_where(table['locator'])} have no verb in the "
                              f"source: the action was read from the Operation column, "
                              f"the caption or the row's shape - confirm each one"})
    return out, notes, questions


def table_ref(table: dict) -> dict:
    """The provenance of a table, without its rows. The footnotes travel with it: one
    fixture hides a rinse volume in a footnote, and dropping them here loses it."""
    return {"block_index": table["block_index"], "at": table["locator"],
            "caption": table["caption"], "key_header": table["key_header"],
            "headers": table["headers"], "flagged": table["flagged"],
            "footnotes": list(table["footnotes"]),
            "s0_note": _norm(table["note"]) or None}


def _attach_tables(candidates: list[Candidate],
                   tables: list[dict]) -> tuple[dict, list[str], list[dict]]:
    """Attach table rows to the prose steps they parameterise, by the source's own label.

    Only a table whose key column is literally "Step" is matched to prose: the fixture
    with a "Pass" column has rows 1-3 and a prose list numbered 1-3 that mean entirely
    different things, and merging those would be exactly the invisible error this stage
    exists to prevent.
    """
    used: dict[int, set] = {}
    notes: list[str] = []
    questions: list[dict] = []
    for table in tables:
        if not table["keys_are_steps"]:
            labelled = [c.source_label for c in candidates if c.source_label]
            if labelled and set(table["order"]) & set(labelled):
                notes.append(f"the table at {_where(table['locator'])} is keyed by "
                             f"{table['key_header']!r}, not by step, so its rows were NOT "
                             f"merged into the prose steps even though the numbers overlap")
                questions.append({"step": None, "question":
                                  f"the table at {_where(table['locator'])} is keyed by "
                                  f"{table['key_header']!r} and its numbers overlap the prose "
                                  f"step numbers; do they refer to the same things?"})
            continue
        for candidate in candidates:
            label = candidate.source_label
            if label is None or label not in table["rows"] or candidate.table:
                continue
            row = table["rows"][label]
            candidate.table = dict(table_ref(table), row=label, cells=row["cells"],
                                   verbatim=row["verbatim"], is_the_method=False)
            used.setdefault(table["block_index"], set()).add(label)
    return used, notes, questions


# --- confidence ---------------------------------------------------------------------


def _confidence(step: dict, audit: dict, mode: str, base: Optional[float] = None,
                questions: int = 0) -> tuple[float, list[str]]:
    """A number with its reasons. The reasons are the point: a bare score cannot be
    argued with, and numbers nobody can argue with are this repo's failure mode."""
    reasons: list[str] = []
    if mode == "model":
        value = MODEL_CEILING / 2 if base is None else max(0.0, min(float(base), MODEL_CEILING))
        reasons.append(f"model reported {base}, clamped to <= {MODEL_CEILING}"
                       if base is not None else "model gave no confidence; assumed mid")
    else:
        value = 0.5 if audit.get("source_label") else 0.4
        reasons.append("numbered step in the source" if audit.get("source_label")
                       else "sentence with an action verb, not a numbered step")
    table = audit.get("table") or {}
    if table:
        if table.get("is_the_method"):
            value -= 0.1
            reasons.append(f"the step is a table row, and its action came from "
                           f"{table.get('action_from')} rather than a verb in the source")
        else:
            reasons.append(f"numbers filled from the table at {table.get('at')}")
    if audit.get("weak_verb"):
        value -= 0.1
        reasons.append(f"action inferred from {audit['weak_verb']!r}, which is not an action name")
    if step.get("target") is None:
        value -= 0.1
        reasons.append("target not stated")
    if audit.get("split"):
        value -= 0.05
        reasons.append("one sentence split into several actions")
    if (audit.get("repeat") or {}).get("expanded") and \
            (audit["repeat"].get("pass_index") or 1) > 1:
        value -= 0.05
        reasons.append("step added by expanding a repeat directive")
    if questions:
        value -= min(0.05 * questions, 0.15)
        reasons.append(f"{questions} unresolved question(s) on this step")
    if table and not table.get("flagged"):
        value = min(value, TABLE_CEILING)
        reasons.append(f"a number read out of a table is capped at {TABLE_CEILING}")
    if audit.get("flagged_by_s0"):
        value = min(value, FLAGGED_CEILING)
        reasons.append(f"S0 flagged this block needs_attention, so it is capped at "
                       f"{FLAGGED_CEILING}")
    if not audit.get("verbatim", True):
        value = min(value, 0.2)
        reasons.append("text was NOT found verbatim in the source; capped at 0.2")
    ceiling = MODEL_CEILING if mode == "model" else DETERMINISTIC_CEILING
    if value > ceiling:
        value = ceiling
        reasons.append(f"the {mode} path never claims more than {ceiling}")
    return round(max(MIN_CONFIDENCE, min(value, ceiling)), 2), reasons


# --- step assembly ------------------------------------------------------------------


def _build_step(number: int, candidate: Candidate, mode: str,
                context: Optional[dict] = None) -> tuple[dict, list[dict]]:
    """One candidate -> one schema-shaped step plus its unresolved questions."""
    context = context or {}
    model_fields = candidate.model or None
    text = _norm(candidate.text)
    scope = candidate.clause or text
    questions: list[dict] = []
    flags: list[str] = []
    notes: list[str] = []

    def ask(field: Optional[str], question: str) -> None:
        entry = {"step": number, "question": question}
        if field:
            entry["field"] = field
        questions.append(entry)

    table = dict(candidate.table) if candidate.table else None
    # For a step that IS a table row, the prose heuristics are read on the Operation cell
    # only: running them over "| 5 | Capture on bead | none added | 0 | 30 | 25 |" invents
    # a reagent called "Capture bead". The numbers come from the cells, below.
    row_is_the_step = bool(table and table.get("is_the_method"))
    detect = scope
    if row_is_the_step:
        detect = _norm(((table.get("cells") or {}).get("operation") or {}).get("text"))
    duration = parse_duration(detect)
    temperature = parse_temperature(detect)
    reagents = [] if row_is_the_step else find_reagents(detect)
    target = find_target(detect)
    action = candidate.action

    # --- the table, where there is one. Prose wins any disagreement, because `text` is
    # the citation; the table's value is still recorded and the conflict is a question.
    if table:
        cells = table.get("cells") or {}
        for kind, parsed in (("duration", duration), ("temperature", temperature)):
            cell = cells.get(kind)
            if not cell:
                continue
            spine_key = "duration_s" if kind == "duration" else "temp_c"
            mine = parsed["duration_s"] if kind == "duration" else parsed["temp_c"]
            if cell["state"] == "value" and mine is None and not parsed["range_s" if
                                                                       kind == "duration"
                                                                       else "range_c"]:
                if kind == "duration":
                    duration = dict(duration, duration_s=cell["value"], text=cell["text"],
                                    qualitative=None)
                else:
                    temperature = dict(temperature, temp_c=cell["value"], text=cell["text"],
                                       qualitative=None)
                notes.append(f"{spine_key} came from the {cell['header']!r} column of the table "
                             f"at {_where(table['at'])} (cell {cell['text']!r}, unit from "
                             f"{cell['unit_from']})")
                flags.append(f"{spine_key}_from_table")
            elif cell["state"] == "value" and mine is not None and \
                    abs(float(cell["value"]) - float(mine)) > 1e-6:
                flags.append(f"{spine_key}_conflict")
                notes.append(f"the sentence says {parsed['text']!r} = {mine} but the table at "
                             f"{_where(table['at'])} says {cell['text']!r} = {cell['value']}; the "
                             f"sentence is kept because it is the quote")
                ask(spine_key, f"the source contradicts itself: the sentence gives {mine} and the "
                               f"table at {_where(table['at'])} gives {cell['value']} for {spine_key} - "
                               f"which is right?")
            elif cell["state"] == "no_unit":
                flags.append("table_column_has_no_unit")
                ask(spine_key, f"the {cell['header']!r} column of the table at {_where(table['at'])} "
                               f"gives no unit, so its value {cell['text']!r} was NOT converted")
            elif cell["state"] == "footnote":
                note_text = _footnote_for(cell["text"], table.get("footnotes") or [])
                taken = _footnote_value(kind, note_text) if note_text else None
                if taken is not None:
                    if kind == "duration":
                        duration = dict(duration, duration_s=taken, text=note_text,
                                        qualitative=None)
                    else:
                        temperature = dict(temperature, temp_c=taken, text=note_text,
                                           qualitative=None)
                    flags.append(f"{spine_key}_from_footnote")
                    notes.append(f"the {cell['header']!r} cell is {cell['text']!r}, a footnote "
                                 f"marker; the value was read from {note_text!r}")
                    ask(spine_key, f"{spine_key} was read from the footnote {note_text!r} because "
                                   f"the cell holds only {cell['text']!r}; confirm the footnote "
                                   f"belongs to this row")
                else:
                    flags.append("table_cell_is_a_footnote_marker")
                    ask(spine_key, f"the {cell['header']!r} cell for step {table['row']} is "
                                   f"{cell['text']!r}, a footnote marker, and no footnote with "
                                   f"that marker carries a {kind}; what is the value?")
            elif cell["state"] == "missing":
                flags.append(f"{spine_key}_not_in_table")
                ask(spine_key, f"the {cell['header']!r} cell for step {table['row']} says "
                               f"{cell['text'] or '(empty)'!r}, so {spine_key} is null")
        reagent_cell = cells.get("reagent")
        volume_cell = cells.get("volume")
        if reagent_cell and _norm(reagent_cell["text"]) and \
                not NO_REAGENT_CELL.match(_norm(reagent_cell["text"])):
            names = _split_reagent_cell(_norm(reagent_cell["text"]))
            name = names[0]
            volume = None
            if volume_cell and volume_cell["state"] == "value":
                volume = volume_cell["value"]
            elif volume_cell and volume_cell["state"] == "footnote":
                note_text = _footnote_for(volume_cell["text"], table.get("footnotes") or [])
                volume = _footnote_value("volume", note_text) if note_text else None
                if volume is not None:
                    flags.append("volume_from_footnote")
                    notes.append(f"the volume cell is {volume_cell['text']!r}, a footnote marker; "
                                 f"{volume} uL was read from {note_text!r}")
                    ask("volume_ul", f"the volume for step {table['row']} was read from the "
                                     f"footnote {note_text!r} because the cell holds only "
                                     f"{volume_cell['text']!r}; confirm it belongs to this row")
                else:
                    ask("volume_ul", f"the volume cell for step {table['row']} is "
                                     f"{volume_cell['text']!r}, a footnote marker, and no "
                                     f"footnote with that marker gives a volume")
            elif volume_cell and volume_cell["state"] == "no_unit":
                flags.append("table_column_has_no_unit")
                ask("volume_ul", f"the {volume_cell['header']!r} column of the table at "
                                 f"{_where(table['at'])} gives no unit, so {volume_cell['text']!r} was "
                                 f"NOT converted")
            elif volume_cell:
                ask("volume_ul", f"the volume cell for step {table['row']} says "
                                 f"{volume_cell['text'] or '(empty)'!r}, so no volume is recorded")
            if len(names) > 1 and volume is not None:
                # One volume for several reagents in one cell: how it splits is not stated.
                flags.append("one_volume_for_several_reagents")
                notes.append(f"the {reagent_cell['header']!r} cell names {len(names)} reagents "
                             f"({', '.join(names)}) and the volume column gives ONE value "
                             f"({volume} uL); it is recorded against none of them")
                ask("volume_ul", f"the table gives one volume ({volume} uL) for {len(names)} "
                                 f"reagents together ({', '.join(names)}); how does it split?")
                volume = None
            for position, name in enumerate(names):
                existing = next((r for r in reagents if r["name"].lower() == name.lower()), None)
                if existing is None:
                    role, word = _role(name)
                    reagents.append({
                        "name": name, "pos": -1 + position / 100.0, "role": role,
                        "role_word": word, "volume_ul": volume, "range_ul": None,
                        "from": "table",
                        "volume_text": volume_cell["text"] if volume_cell else None})
                elif existing["volume_ul"] is None and volume is not None:
                    existing["volume_ul"] = volume
                    existing["from"] = "table"
                    existing["volume_text"] = volume_cell["text"] if volume_cell else None
            reagents.sort(key=lambda r: r["pos"])
        if cells.get("flow") and cells["flow"].get("value") is not None:
            notes.append(f"the table also gives {cells['flow']['header']!r} = "
                         f"{cells['flow']['text']!r} for this step; it is in `audit`, since the "
                         f"S1 contract has no flow field")
        if table.get("flagged"):
            flags.append("s0_flagged_the_table")
            notes.append(f"S0 could not parse that table with certainty, so every number taken "
                         f"from it here is unverified")

    # --- the model's reading, where there is one. It may name a target or a role the
    # regexes cannot see; it may NOT overrule a number parsed from the quote.
    if model_fields:
        if model_fields.get("target"):
            target = model_fields["target"]
        if model_fields.get("action") in ACTION_NAMES:
            if action and model_fields["action"] != action:
                notes.append(f"the model called this {model_fields['action']!r} and the verb "
                             f"lexicon called it {action!r}; the model's reading is used")
            action = model_fields["action"]
        for proposed in model_fields.get("reagents") or []:
            if not isinstance(proposed, dict):
                continue
            name = _norm(proposed.get("name"))
            if not name:
                continue
            existing = next((r for r in reagents if r["name"].lower() == name.lower()), None)
            if existing is None:
                role, word = _role(name)
                if not role and proposed.get("role") in ROLE_NAMES:
                    role, word = proposed["role"], "model"
                reagents.append({"name": name, "pos": 10 ** 6, "role": role, "role_word": word,
                                 "volume_ul": None, "range_ul": None, "volume_text": None,
                                 "from": "model"})
                existing = reagents[-1]
            if existing["role"] is None and proposed.get("role") in ROLE_NAMES:
                existing["role"], existing["role_word"] = proposed["role"], "model"
            claimed = proposed.get("volume_ul")
            if claimed is None:
                continue
            if existing["volume_ul"] is not None and \
                    abs(float(claimed) - float(existing["volume_ul"])) > 1e-6:
                flags.append("model_volume_disagreed")
                notes.append(f"the model said {name} is {claimed} uL; the source says "
                             f"{existing['volume_text']!r} = {existing['volume_ul']} uL, which "
                             f"is kept")
            elif existing["volume_ul"] is None:
                flags.append("model_volume_unsourced")
                ask("volume_ul", f"the model volunteered {claimed} uL for {name!r}, but no volume "
                                 f"for it appears in the quoted text; where is it from?")
        for field, parsed, key in (("duration_s", duration, "duration_s"),
                                   ("temp_c", temperature, "temp_c")):
            claimed = model_fields.get(field)
            if claimed is None:
                continue
            if parsed[key] is not None and abs(float(claimed) - float(parsed[key])) > 1e-6:
                flags.append(f"model_{field}_disagreed")
                notes.append(f"the model said {field}={claimed}; the source says "
                             f"{parsed['text']!r} = {parsed[key]}, which is kept")
            elif parsed[key] is None:
                flags.append(f"model_{field}_unsourced")
                ask(field, f"the model volunteered {field}={claimed}, which is not stated in the "
                           f"quoted text; where is it from?")

    reagents = _dedupe_reagents(reagents)

    # --- ranges and qualitative conditions: null in the spine field, kept in `audit`
    if duration["range_s"]:
        flags.append("duration_is_a_range")
        notes.append(f"the source gives a range, {duration['text']!r}; duration_s is null and "
                     f"audit.duration.range_s holds {duration['range_s']} - no end was picked")
        ask("duration_s", f"duration is a range ({duration['text']}) = {duration['range_s']} s; "
                          f"which value should a later stage use, and on what rule?")
    if duration["qualitative"]:
        flags.append("duration_is_qualitative")
        notes.append(f"the duration is given as {duration['qualitative']!r}, not a measurement")
        ask("duration_s", f"duration is {duration['qualitative']!r}, not a number; how many "
                          f"seconds is it?")
    if temperature["range_c"]:
        flags.append("temperature_is_a_range")
        notes.append(f"the source gives a temperature range, {temperature['text']!r}; temp_c is "
                     f"null and audit.temperature.range_c holds {temperature['range_c']}")
        ask("temp_c", f"temperature is a range ({temperature['text']}) = "
                      f"{temperature['range_c']} C; which value should a later stage use?")
    if temperature["qualitative"]:
        flags.append("temperature_is_qualitative")
        notes.append(f"the temperature is given as {temperature['qualitative']!r}, not a number")
        ask("temp_c", f"temperature is {temperature['qualitative']!r} and the source gives no "
                      f"number; what is it in C?")
    if _ambiguous_rt(scope):
        flags.append("rt_ambiguous")
        notes.append("'RT' here could be room temperature or reverse transcription, so it was "
                     "read as NEITHER")
        ask("temp_c", "'RT' in this text: room temperature, or reverse transcription?")
    if OTHER_TEMP_UNIT.search(scope):
        flags.append("temperature_unit_not_converted")
        ask("temp_c", "a temperature appears in F or K, which this stage does not convert; what "
                      "is it in C?")

    # --- repeats written into the step itself are recorded, NOT expanded: one source
    # sentence stays one step, and the count travels for S2 to turn into washes.
    in_step = None
    if candidate.repeat is None:
        m = REPEAT_IN_STEP.search(_depunct(scope))
        if m:
            count = _word_count(m.group("count")) or REPLICATES.get(
                (m.group("word") or "").lower())
            if count and count > 1:
                in_step = {"kind": "in_step_count", "count": count, "expanded": False,
                           "text": m.group(0).strip()}
                flags.append("repeat_count_in_step")
                notes.append(f"the step says {m.group(0).strip()!r}: recorded as "
                             f"audit.repeat.count = {count} and NOT expanded into {count} steps")
                ask(None, f"this step says {m.group(0).strip()!r}; it is ONE step here with "
                          f"audit.repeat.count = {count}. Should it be {count} steps?")
    reference = STEP_REFERENCE.search(scope)
    if reference and reference.group("n") != (candidate.source_label or ""):
        flags.append("references_another_step")
        ask(None, f"this step points at step {reference.group('n')} of the source; is it a repeat "
                  f"of that step, and if so how many times?")
    if re.search(r"\btable\b", scope, re.I) and context.get("flagged_tables"):
        flags.append("values_are_in_a_table")
        notes.append(f"the step points at a table for its values, and S0 flagged "
                     f"{context['flagged_tables']} table(s) here, so anything only that table "
                     f"said is absent rather than estimated")
        ask(None, f"this step's values are in a table S0 could not parse with certainty "
                  f"(page(s) {', '.join(context.get('flagged_table_pages') or ['?'])}); what does it say?")

    # --- what is simply not there
    if duration["duration_s"] is None and not duration["range_s"] and \
            not duration["qualitative"] and action in ("incubate", "centrifuge", "amplify",
                                                       "wash", "lyse"):
        ask("duration_s", f"no duration stated for this {action} step")
    if temperature["temp_c"] is None and not temperature["range_c"] and \
            not temperature["qualitative"] and action in ("incubate", "amplify", "lyse"):
        ask("temp_c", f"no temperature stated for this {action} step")
    if not reagents and action in ("add", "wash", "mix"):
        ask("reagents", f"this {action} step names no reagent this stage could identify")
    if target is None:
        ask("target", "what the step is done to is not stated in the quoted text")
    for reagent in reagents:
        if reagent["volume_ul"] is None and reagent["range_ul"] is None:
            ask("volume_ul", f"no volume stated for {reagent['name']!r}")
        if reagent["range_ul"]:
            ask("volume_ul", f"the volume for {reagent['name']!r} is a range "
                             f"({reagent['volume_text']}) = {reagent['range_ul']} uL; which value "
                             f"should a later stage use?")
        if reagent["role"] is None:
            ask("role", f"the role of {reagent['name']!r} "
                        f"({'/'.join(ROLE_NAMES)}) is not inferable from the source")
    if candidate.weak:
        notes.append(f"the source says {candidate.verb!r}, which is not one of "
                     f"{'/'.join(ACTION_NAMES)}; it was read as {action!r}")
    if candidate.sentence_count > 1:
        flags.append("sentences_joined")
        notes.append(f"{candidate.sentence_count} consecutive source sentences describe this one "
                     f"step and are quoted together")
    if candidate.split:
        notes.append(f"this is action {candidate.split['index']} of "
                     f"{candidate.split['of']} in one sentence; the clause it covers is "
                     f"{candidate.split['clause']!r}")
    if candidate.s0_note:
        notes.append(f"S0 on this block: {candidate.s0_note}")

    step: dict = {
        "step": number,
        "text": text,
        "action": action,
        "reagents": [_reagent_out(r) for r in reagents],
        "target": target,
        "duration_s": duration["duration_s"],
        "temp_c": temperature["temp_c"],
        "confidence": 0.0,
        "quote_page": candidate.locator,
        "notes": "; ".join(notes),
    }
    if not step["notes"]:
        del step["notes"]
    audit: dict = {
        "extracted_by": mode,
        "source_label": candidate.source_label,
        "block_index": candidate.block_index,
        "block_kind": candidate.block_kind,
        "sentence_count": candidate.sentence_count,
        "verb": candidate.verb,
        "weak_verb": candidate.verb if candidate.weak else None,
        "verbatim": bool((model_fields or {}).get("verbatim", True)),
        "flagged_by_s0": candidate.flagged,
        "s0_note": candidate.s0_note,
        "duration": {k: duration[k] for k in ("text", "duration_s", "range_s", "qualitative")},
        "temperature": {k: temperature[k] for k in ("text", "temp_c", "range_c", "qualitative")},
        "reagents": [{"name": r["name"], "generic": _generic_name(r["name"]),
                      "volume_text": r["volume_text"], "volume_ul": r["volume_ul"],
                      "volume_ul_range": r["range_ul"], "role": r["role"],
                      "role_from": r["role_word"], "found_in": r.get("from", "prose")}
                     for r in reagents],
        "repeat": candidate.repeat or in_step,
        "split": candidate.split,
        "table": table,
        "flags": sorted(set(flags)),
        "confidence_reasons": [],
    }
    step["audit"] = audit
    step["confidence"], audit["confidence_reasons"] = _confidence(
        step, audit, mode, base=(model_fields or {}).get("confidence"), questions=len(questions))
    return step, questions


def _reagent_out(reagent: dict) -> dict:
    """A reagent in the schema's shape. `role` is OMITTED when the source does not say,
    because the enum has no null and a guessed role is a guessed viscosity in S2."""
    out = {"name": reagent["name"], "volume_ul": reagent["volume_ul"]}
    if reagent.get("role") in ROLE_NAMES:
        out["role"] = reagent["role"]
    return out


def _footnote_value(kind: str, note: Optional[str]) -> Optional[float]:
    """The one number a footnote carries, in canonical units, or None."""
    if not note:
        return None
    if kind == "volume":
        hits = parse_volumes(note)
        return hits[0]["volume_ul"] if hits and hits[0]["volume_ul"] is not None else None
    if kind == "duration":
        return parse_duration(note)["duration_s"]
    if kind == "temperature":
        return parse_temperature(note)["temp_c"]
    return None


# --- the deterministic path ---------------------------------------------------------


def extract_deterministic(raw: dict) -> dict:
    """raw.json -> steps with no model at all: regex, parameter tables, units, lexicon.

    Honestly worse than the model path and says so: fewer reagents, more null targets,
    more questions, confidence capped at DETERMINISTIC_CEILING.
    """
    blocks = _blocks(raw)
    context = _context(blocks)
    candidates, directives, notes, questions = _segment(blocks, _norm(raw.get("title")))
    tables = parameter_tables(blocks)
    unclassified = [c for c in candidates if c.action is None]
    candidates = [c for c in candidates if c.action is not None]
    used, table_notes, table_questions = _attach_tables(candidates, tables)
    from_tables, method_notes, method_questions = _table_rows_as_candidates(tables, used)
    candidates = sorted(candidates + from_tables, key=lambda c: c.order)
    candidates, split_notes = _split_two_actions(candidates)
    candidates, repeat_notes, repeat_questions = _expand_repeats(candidates, directives)

    steps: list[dict] = []
    unresolved = list(questions) + table_questions + method_questions + repeat_questions
    for number, candidate in enumerate(candidates, start=1):
        step, step_questions = _build_step(number, candidate, "deterministic", context)
        steps.append(step)
        unresolved.extend(step_questions)
    for candidate in unclassified:
        where = f"source step {candidate.source_label}" if candidate.source_label \
            else f"a sentence at {_where(candidate.locator)}"
        unresolved.append({"step": None, "field": "action", "question":
                           f"{where} was not emitted as a step: no verb in it maps to an action "
                           f"({', '.join(ACTION_NAMES)}). It reads "
                           f"{_norm(candidate.text)[:90]!r}"})
    unresolved.extend(_numbering_questions(candidates))
    return {"steps": steps, "unresolved": unresolved,
            "notes": notes + table_notes + method_notes + split_notes + repeat_notes,
            "counts": dict(context, source_blocks=len(blocks), parameter_tables=len(tables),
                           unclassified_sentences=len(unclassified))}


def _numbering_questions(candidates: list[Candidate]) -> list[dict]:
    """The source's own numbering, when it does not agree with document order.

    The messy fixture runs 1, 2, 3, 3b, 5 and then 4 in a later block. `step` here is
    document order; if the source meant numeric order, the sequence is wrong, and only the
    source can say which.
    """
    labels = [(number, c.source_label) for number, c in enumerate(candidates, start=1)
              if c.source_label]
    if len(labels) < 2:
        return []
    keys = [_label_sort_key(label) for _, label in labels]
    if keys == sorted(keys) and len({label for _, label in labels}) == len(labels):
        return []
    shown = ", ".join(label for _, label in labels)
    return [{"step": None, "question":
             f"the source's own step numbers run {shown}, which is not ascending order; `step` "
             f"here is DOCUMENT order, not the source's numbering - which order is the assay "
             f"actually carried out in?"}]


# --- the model path -----------------------------------------------------------------

SYSTEM_PROMPT = """\
You read a wet-lab method and return its steps as JSON. You are one stage of a pipeline \
that etches real microfluidic chips from the numbers you return, so a number you invent \
becomes a channel that is the wrong size.

Hard rules:
- "text" must be a VERBATIM contiguous quote from one block: a sentence, or a table row \
as printed. Never reword it, never join text from two blocks, never tidy it up. It is \
checked against the source and your step is demoted if it does not match.
- If the source does not state a value, use null AND add an entry to "unresolved" saying \
exactly what is missing. Never a plausible default, never a typical value.
- A range ("5-10 min") is not a value: leave the field null and say so in "unresolved".
- "overnight", "room temperature" and "on ice" are not numbers: null, plus a question. \
A bare "RT" is ambiguous (room temperature or reverse transcription): never resolve it.
- "action" is one of: add, incubate, wash, centrifuge, mix, transfer, image, lyse, \
amplify, separate, seed, harvest. If none fits, leave the sentence out of "steps" and say \
why in "unresolved".
- Reagents keep the SOURCE'S own names. Give each a "role": medium, buffer, enzyme, \
solvent, oil or label, or omit the role when you cannot tell from the source.
- Units: volumes in uL, durations in seconds, temperatures in C. Take a table's unit from \
its column header.
- A step table keyed by step number gives the numbers for the prose steps with those \
numbers. A table keyed by anything else (pass, phase, port) is NOT the prose numbering.
- "confidence" is 0-1 and is about the STEP, not your fluency: 0.8 means the source \
states the action, the target and the numbers plainly. Anything S0 marked \
needs_attention must be low.
- One sentence describing two actions becomes two steps, both quoting the whole sentence. \
A step spread over two sentences becomes one step quoting both.
- "quote_page" is the page of the block you quoted.
- Do not include anything under an Abstract, Materials, Notes or Expected outcome heading.

Return ONLY JSON: {"steps": [...], "unresolved": [{"step": n|null, "question": "..."}]}"""

# The real fallback is `extract_deterministic`, which produces far more than this prompt's
# JSON shape, so the stub handed to llm.complete_json is a marker and the extractor runs
# on seeing it. llm.py requires a stub precisely so that no stage can be model-only.
_STUB_MARKER = {"s1_fallback": "deterministic"}


def _prompt(blocks: list[dict]) -> str:
    lines = []
    for index, block in enumerate(blocks):
        kind = (block.get("kind") or "paragraph").lower()
        marker = ""
        if block_flagged(block):
            marker = (f" [S0 FLAGGED needs_attention: {_norm(block.get('note'))} - treat every "
                      f"number in it as unverified and give it low confidence]")
        lines.append(f"<block index=\"{index}\" kind=\"{kind}\" page=\"{block.get('page')}\">"
                     f"{marker}\n{_block_text(block)}\n</block>")
    return ("Method text, block by block:\n\n" + "\n".join(lines)
            + "\n\nReturn the steps as JSON.")


def _as_payload(reply: Any) -> dict:
    """Whatever came back -> {"steps": [...], ...}. Raises ValueError if it cannot be."""
    if isinstance(reply, dict) and ("steps" in reply or "unresolved" in reply):
        return reply
    if isinstance(reply, list):
        return {"steps": reply, "unresolved": []}
    if isinstance(reply, dict):
        for key in ("json", "data", "content", "text", "output", "result", "reply"):
            if key in reply:
                return _as_payload(reply[key])
        raise ValueError(f"model reply has no steps and no known payload key: {sorted(reply)[:8]}")
    if isinstance(reply, str):
        text = reply.strip()
        fence = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
        if fence:
            text = fence.group(1).strip()
        start = min([i for i in (text.find("{"), text.find("[")) if i != -1] or [-1])
        if start > 0:
            text = text[start:]
        return _as_payload(json.loads(text))
    raise ValueError(f"model reply is a {type(reply).__name__}, not JSON")


def _verify(model_steps: list, blocks: list[dict]) -> tuple[list[Candidate], list[str]]:
    """Check every model step against the source before trusting a word of it.

    This is the guard against the defect that bit this repo: text that reads like a quote
    but is not, and a page citation for something that page never said. A step whose text
    is not verbatim keeps its text but is flagged, demoted to at most 0.2 and questioned;
    its page is corrected to the block the text actually came from.
    """
    normalised = [(index, _norm(_block_text(block)), block) for index, block in enumerate(blocks)]
    candidates: list[Candidate] = []
    repairs: list[str] = []
    for position, raw_step in enumerate(model_steps, start=1):
        if not isinstance(raw_step, dict):
            repairs.append(f"model step {position} is not an object; dropped")
            continue
        text = _norm(raw_step.get("text"))
        if not text:
            repairs.append(f"model step {position} has no text; dropped")
            continue
        action = raw_step.get("action")
        if action not in ACTION_NAMES:
            guessed = classify_action(text)[0]
            if guessed is None:
                repairs.append(f"model step {position} has action {action!r}, not in the enum, "
                               f"and no verb in its text maps to one; dropped")
                continue
            repairs.append(f"model step {position}: action {action!r} is not in the enum; the "
                           f"verb lexicon read it as {guessed!r}")
            action = guessed
        hit = next((entry for entry in normalised if text in entry[1]), None)
        if hit is None:
            repairs.append(f"model step {position} text is NOT verbatim in any block "
                           f"({text[:60]!r}); kept, flagged and capped at 0.2")
        block = hit[2] if hit else {}
        locator = block_locator(block, hit[0]) if hit else raw_step.get("quote_page")
        claimed = raw_step.get("quote_page")
        if hit and claimed is not None and _norm(claimed) != _norm(locator):
            repairs.append(f"model step {position} cited {claimed!r}; the quoted text is at "
                           f"{locator!r} (block {hit[0]}); the citation was corrected")
        if not hit and not (isinstance(locator, (int, str)) and _norm(locator)):
            locator = f"model step {position}, not located in raw.json"
        candidate = Candidate(text, locator, hit[0] if hit else -1,
                              (block.get("kind") or "paragraph").lower() if hit else "unknown",
                              None, bool(hit) and block_flagged(block),
                              _norm(block.get("note")) or None if hit else None,
                              (hit[0] if hit else 10 ** 6, 0, position))
        candidate.action = action
        candidate.weak, candidate.verb = classify_action(text)[1:]
        candidate.model = {
            "target": raw_step.get("target"), "action": action,
            "reagents": raw_step.get("reagents"), "duration_s": raw_step.get("duration_s"),
            "temp_c": raw_step.get("temp_c"), "confidence": raw_step.get("confidence"),
            "verbatim": hit is not None,
        }
        candidates.append(candidate)
    return candidates, repairs


def extract_with_model(raw: dict) -> dict:
    """Ask the model, then verify. Returns {"failed": why} instead of raising."""
    blocks = _blocks(raw)
    context = _context(blocks)
    reply = llm.complete_json(_prompt(blocks), task=TASK, system=SYSTEM_PROMPT,
                              stub=_STUB_MARKER)
    if isinstance(reply, dict) and reply.get("s1_fallback"):
        return {"failed": "llm is in stub mode, so no model was called"}
    try:
        payload = _as_payload(reply)
    except (ValueError, json.JSONDecodeError) as exc:
        return {"failed": f"the model's answer could not be read as JSON ({exc})"}
    model_steps = payload.get("steps")
    if not isinstance(model_steps, list) or not model_steps:
        return {"failed": "the model returned no steps"}
    candidates, repairs = _verify(model_steps, blocks)
    if not candidates:
        return {"failed": "no model step survived the verbatim and enum checks"}
    candidates = sorted(candidates, key=lambda c: c.order)
    candidates, repeat_notes, repeat_questions = _expand_repeats(
        candidates, _segment(blocks, _norm(raw.get("title")))[1])
    steps: list[dict] = []
    unresolved: list[dict] = list(repeat_questions)
    for number, candidate in enumerate(candidates, start=1):
        step, questions = _build_step(number, candidate, "model", context)
        if not (candidate.model or {}).get("verbatim", True):
            questions.append({"step": number, "question":
                              "the quoted text could not be found verbatim in raw.json; confirm "
                              "it against the source before any number from it is used"})
            step["confidence"], step["audit"]["confidence_reasons"] = _confidence(
                step, step["audit"], "model",
                base=(candidate.model or {}).get("confidence"), questions=len(questions))
        steps.append(step)
        unresolved.extend(questions)
    for entry in payload.get("unresolved") or []:
        if isinstance(entry, dict) and entry.get("question"):
            number = entry.get("step")
            unresolved.append({"step": number if isinstance(number, int) and number >= 1 else None,
                               "question": f"(model) {_norm(entry['question'])}"})
    return {"steps": steps, "unresolved": unresolved, "notes": repairs + repeat_notes,
            "repairs": repairs,
            "counts": dict(context, source_blocks=len(blocks),
                           model_steps=len(model_steps), kept=len(candidates))}


# --- the stage ----------------------------------------------------------------------

SOURCE_KEYS = ("kind", "ref", "sha256", "retrieved_at", "note")


def _dedupe(unresolved: list[dict]) -> list[dict]:
    """Deterministic order, no duplicates: by step (nulls last), field, then question."""
    seen = set()
    out = []
    for entry in sorted(unresolved, key=lambda e: (e.get("step") is None, e.get("step") or 0,
                                                   e.get("field") or "", e.get("question", ""))):
        question = _norm(entry.get("question"))
        if not question:
            continue
        key = (entry.get("step"), question)
        if key in seen:
            continue
        seen.add(key)
        item = {"step": entry.get("step"), "question": question}
        if entry.get("field"):
            item["field"] = entry["field"]
        out.append(item)
    return out


def extract(raw: dict, use_model: Optional[bool] = None) -> dict:
    """raw.json (dict) -> assay.json (dict). The entry point for callers and tests.

    `use_model` None follows assay2chip/lib/llm.py's mode: a model is asked only in
    `--llm real`. The mode that ran, and why it fell back if it did, are recorded in
    `extraction` and in the `llm` block - a stubbed file can never read as a real one.
    """
    mode = "deterministic"
    fallback_reason = None
    repairs: list[str] = []
    result = None
    wanted = llm.current_mode() == llm.REAL if use_model is None else bool(use_model)
    if not wanted:
        fallback_reason = (f"no model was asked for (llm mode {llm.current_mode()!r}); the "
                           f"deterministic extractor ran")
    else:
        attempt = extract_with_model(raw)
        if "failed" in attempt:
            fallback_reason = f"{attempt['failed']}; the deterministic extractor ran instead"
        else:
            result, mode = attempt, "model"
            repairs = attempt.get("repairs", [])
    if result is None:
        result = extract_deterministic(raw)

    notes = list(result.get("notes") or [])
    if mode == "deterministic":
        notes.insert(0, "extracted WITHOUT a language model: regex over the source's own "
                        "numbering, its parameter tables, units and a verb lexicon. Expect "
                        "missed reagents, null targets and more unresolved questions, and "
                        f"confidence no higher than {DETERMINISTIC_CEILING}.")
    else:
        notes.insert(0, "extracted with a language model, then checked against raw.json: every "
                        "quote must appear verbatim in a block, every number is re-parsed from "
                        "that quote, citations are corrected to the block the text came from, "
                        "and the source wins any disagreement.")
    unresolved = _dedupe(result["unresolved"])
    if not result["steps"]:
        raise ValueError(
            "no step could be extracted from raw.json, so nothing was written: the schema needs "
            "at least one step and an empty assay.json would read like an assay with no steps. "
            + (f"The first gaps found were: {'; '.join(q['question'] for q in unresolved[:3])}"
               if unresolved else "No candidate sentences were found at all."))

    source = raw.get("source")
    dropped: list[str] = []
    if isinstance(source, dict):
        dropped = sorted(k for k in source if k not in SOURCE_KEYS)
        source = {k: v for k, v in source.items() if k in SOURCE_KEYS}
    if dropped:
        notes.append(f"S0's source block carried {', '.join(dropped)}, which the assay contract "
                     f"does not allow; they were left behind, not altered")

    assay: dict = {
        "id": raw.get("id"),
        "source": source,
        "extraction": {
            "stage": STAGE,
            "contract": "assay2chip/README.md, S1 assay.json",
            "mode": mode,
            "fallback_reason": fallback_reason,
            "confidence_ceiling": MODEL_CEILING if mode == "model" else DETERMINISTIC_CEILING,
            "repairs": repairs,
            "notes": notes,
            "counts": dict(result.get("counts") or {}, steps=len(result["steps"]),
                           unresolved=len(unresolved),
                           low_confidence_steps=sum(1 for s in result["steps"]
                                                    if s["confidence"] < 0.3)),
        },
        "llm": llm.provenance(note=(
            "No model was called. The numbers here were parsed by S1's own deterministic "
            "extractor from verbatim source text, not invented - but it reads prose with "
            "regexes, so see `extraction` and every step's `audit.confidence_reasons`.")
            if mode == "deterministic" else None),
        "steps": result["steps"],
        "unresolved": unresolved,
    }
    if raw.get("title"):
        assay["title"] = _norm(raw["title"])
    return assay


def run(assay_id: str = "", raw_path: Optional[Any] = None, out_path: Optional[Any] = None,
        llm_mode: Optional[str] = None, write: bool = True, **kwargs) -> dict:
    """The stage entry point. Reads raw.json, extracts, writes assay.json.

    Called by assay2chip/run.py with `assay_id` (and `llm_mode`, already configured on
    the llm module); `raw_path`/`out_path` are for fixtures and throwaway inputs outside
    the work directory. Returns a summary; S1 never asks for a re-run.
    """
    _ = kwargs
    if llm_mode in (llm.STUB, llm.REAL):
        llm.configure(llm_mode)
    if raw_path is not None:
        raw = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    elif assay_id:
        raw = stage_io.read_stage(assay_id, INPUT_KIND)
    else:
        raise ValueError("s1_extract.run() needs an assay id or a raw_path")
    assay = extract(raw)
    if not assay.get("id"):
        assay["id"] = assay_id or None
    written = None
    if write and out_path is not None:
        written = stage_io.write_json(out_path, assay)
    elif write:
        written = stage_io.write_stage(assay["id"] or assay_id, OUTPUT_KIND, assay)
    return {"steps": len(assay["steps"]), "unresolved": len(assay["unresolved"]),
            "mode": assay["extraction"]["mode"], "llm": assay["llm"]["mode"],
            "path": str(written) if written else None, "assay": assay}


# --- selftest -----------------------------------------------------------------------


def selftest() -> list[str]:
    """The unit tables and the messy cases, offline. A factor-of-ten slip in the tables at
    the top of this file is invisible in the artefact, so it is checked here."""
    problems: list[str] = []

    def check(label: str, got: Any, want: Any) -> None:
        if got != want:
            problems.append(f"{label}: got {got!r}, want {want!r}")

    for text, want in (("100 uL", 100), ("100 \u00b5L", 100), ("100 \u03bcL", 100),
                       ("0.1 mL", 100), ("1 mL", 1000), ("1.5 mL", 1500), ("1 L", 1000000),
                       ("1,000 nL", 1), ("500 nL", 0.5), ("2 pL", 0.000002),
                       ("20 microlitres", 20), ("2 cc", 2000)):
        hits = parse_volumes(text)
        check(f"volume {text}", hits[0]["volume_ul"] if hits else None, want)
    check("volume range 5-10 uL", parse_volumes("5-10 uL")[0]["range_ul"], [5, 10])
    check("volume range 0.5 to 1 mL", parse_volumes("0.5 to 1 mL")[0]["range_ul"], [500, 1000])
    check("'5 mM' is not a volume", parse_volumes("5 mM EDTA"), [])
    check("'5 mm' is not a volume", parse_volumes("a 5 mm channel"), [])

    for text, want in (("for 90 s", 90), ("for 90 seconds", 90), ("for 5 min", 300),
                       ("for 5 minutes", 300), ("for 1.5 h", 5400), ("for 2 hours", 7200),
                       ("for 1 d", 86400), ("for 250 ms", 0.25)):
        check(f"duration {text}", parse_duration(text)["duration_s"], want)
    check("duration range", parse_duration("incubate for 5-10 min")["range_s"], [300, 600])
    check("a range picks neither end", parse_duration("for 5-10 min")["duration_s"], None)
    check("overnight", parse_duration("incubate overnight")["qualitative"], "overnight")
    check("overnight has no number", parse_duration("incubate overnight")["duration_s"], None)
    check("'for' beats a bare number",
          parse_duration("centrifuge at 300 g for 5 min")["duration_s"], 300)
    check("'5 m' is not minutes", parse_duration("a 5 m length of tubing")["duration_s"], None)

    for text, want in (("at 37 C", 37), ("at 37\u00b0C", 37), ("at 37 \u00b0C", 37),
                       ("at 4C", 4), ("at -20 degC", -20), ("at 98 degrees C", 98)):
        check(f"temp {text}", parse_temperature(text)["temp_c"], want)
    check("temp range", parse_temperature("hold at 20-25 C")["range_c"], [20, 25])
    check("temp range picks neither end", parse_temperature("hold at 20-25 C")["temp_c"], None)
    check("room temperature", parse_temperature("at room temperature")["qualitative"],
          "room temperature")
    check("room temperature is not 22", parse_temperature("at room temperature")["temp_c"], None)
    check("on ice", parse_temperature("keep on ice")["qualitative"], "on ice")
    check("'RT' is not a temperature", parse_temperature("incubate at RT")["temp_c"], None)
    check("'RT' is flagged", _ambiguous_rt("incubate at RT for 10 min"), True)
    check("'RT' with the words is not flagged",
          _ambiguous_rt("at room temperature (RT)"), False)
    check("'300 Cells' is not a temperature", parse_temperature("count 300 Cells")["temp_c"], None)

    check("header unit uL", _cell_number("volume", "Volume (uL)", "120")["value"], 120)
    check("header unit mL", _cell_number("volume", "Volume (mL)", "0.12")["value"], 120)
    check("header unit min", _cell_number("duration", "Time (min)", "2")["value"], 120)
    check("header unit C", _cell_number("temperature", "Temp (C)", "37")["value"], 37)
    check("cell unit beats header",
          _cell_number("volume", "Volume (uL)", "60 nL")["value"], 0.06)
    check("no unit in header is not a guess",
          _cell_number("volume", "Volume", "120")["state"], "no_unit")
    check("'not stated' cell", _cell_number("volume", "Volume (uL)", "not stated")["state"],
          "missing")
    check("footnote marker cell", _cell_number("volume", "Volume (uL)", "*")["state"], "footnote")
    check("footnote lookup", _footnote_value(
        "volume", "Footnote: * The second rinse uses 60 uL, half the volume of the first."), 60)

    check("action add", classify_action("Add 100 uL of lysis buffer to the cells")[0], "add")
    check("action wash", classify_action("Wash the beads three times with wash_buffer")[0], "wash")
    check("action centrifuge", classify_action("Spin at 300 g for 5 min")[0], "centrifuge")
    check("action incubate", classify_action("Incubate at 37 C for 30 min")[0], "incubate")
    check("action separate", classify_action("Break the emulsion with break_solution")[0],
          "separate")
    check("weak verb flagged", classify_action("Discard the supernatant")[1], True)
    check("no action", classify_action("The chip was fabricated in PDMS.")[0], None)

    check("role from head noun", _role("staining buffer")[0], "buffer")
    check("role through snake_case", _role("wash_buffer")[0], "buffer")
    check("role label", _role("antibody solution")[0], "label")
    check("role oil", _role("oil_phase")[0], "oil")
    check("role enzyme by -ase", _role("proteinase")[0], "enzyme")
    check("unknown role stays null", _role("widget XR-7")[0], None)
    check("reagent word", _is_reagent_word("lysis_buffer"), True)
    check("not a reagent word", _is_reagent_word("chamber"), False)
    check("generic name strips numbers and vendors",
          _generic_name("Lysis Buffer (10 mM, AcmeBio)"), "lysis_buffer")

    check("snake_case reagent found",
          [r["name"] for r in find_reagents("Apply lysis_buffer and hold without flow.")],
          ["lysis_buffer"])
    check("volume attaches to its reagent",
          [(r["name"], r["volume_ul"]) for r in
           find_reagents("Resuspend in 100 uL of wash_buffer.")],
          [("wash_buffer", 100)])

    check("word number", _word_count("three"), 3)
    check("digit number", _word_count("3"), 3)
    check("sentence split keeps decimals", _sentences("Use 0.3 mm channels. Then wash."),
          ["Use 0.3 mm channels.", "Then wash."])
    check("step label survives sentence rules",
          _label_chunks("1. Rinse the chamber.\n2. Repeat the rinse."),
          [("1", "Rinse the chamber."), ("2", "Repeat the rinse.")])
    check("letter-suffixed label kept",
          [label for label, _ in _label_chunks("3. Do this.\n3b. Do that.")], ["3", "3b"])
    check("figure reference is not a label",
          _label_chunks("As in Fig. 2). The cells were counted."),
          [(None, "As in Fig. 2). The cells were counted.")])
    check("repeat directive", bool(REPEAT_DIRECTIVE.search(
        "Repeat steps 3-5 three times to remove residual enzyme.")), True)
    check("caption spotted", _is_caption("Table S2. Nominal parameters for the wash"), True)
    check("non-step heading spotted", bool(NON_STEP_HEADING.match("Notes on operation")), True)
    check("method heading is not skipped", bool(NON_STEP_HEADING.match("Wash and lysis sequence")),
          False)
    return problems


# --- CLI ----------------------------------------------------------------------------


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        description="S1 extract: raw.json -> assay.json, the source's own wet-lab steps.")
    parser.add_argument("--id", dest="assay_id", default="",
                        help="assay id; reads and writes assay2chip/work/<id>/")
    parser.add_argument("--raw", help="a raw.json to read instead of the work directory")
    parser.add_argument("--out", help="where to write assay.json instead")
    parser.add_argument("--llm", choices=(llm.STUB, llm.REAL),
                        help=f"{llm.STUB} (default, offline) or {llm.REAL}")
    parser.add_argument("--no-write", action="store_true", help="extract but write nothing")
    parser.add_argument("--print", action="store_true", help="print the artefact to stdout")
    parser.add_argument("--selftest", action="store_true",
                        help="check the unit tables and the messy cases, offline")
    args = parser.parse_args(argv)

    if args.selftest:
        problems = selftest()
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        print(f"{STAGE} selftest: {'FAILED' if problems else 'OK'} ({len(problems)} problem(s))")
        return 1 if problems else 0

    if not args.assay_id and not args.raw:
        parser.error("give --id, or --raw (and usually --out)")
    try:
        summary = run(assay_id=args.assay_id, raw_path=args.raw, out_path=args.out,
                      llm_mode=args.llm, write=not args.no_write)
    except llm.LLMUnavailable as exc:
        # --llm real asked for a model and there is none. Deliberately NOT a quiet fall
        # back to the deterministic path: that would label a regex extraction 'real'.
        print(f"{STAGE}: {exc}\nRun without --llm real to use the deterministic extractor, "
              f"which says so in `extraction.mode`.", file=sys.stderr)
        return 1
    except (ValueError, OSError, stage_io.StageError) as exc:
        print(f"{STAGE}: {exc}", file=sys.stderr)
        return 1
    assay = summary.pop("assay")
    if args.print:
        print(json.dumps(assay, indent=2, ensure_ascii=False))
    low = [s["step"] for s in assay["steps"] if s["confidence"] < 0.3]
    print(f"{STAGE}: {summary['steps']} steps, {summary['unresolved']} unresolved, "
          f"mode={summary['mode']}, llm={summary['llm']}"
          f"{', low-confidence steps ' + str(low) if low else ''}"
          f"{' -> ' + summary['path'] if summary['path'] else ' (nothing written)'}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

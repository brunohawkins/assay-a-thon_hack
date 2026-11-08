"""Generic reagent names, and the screen that keeps the source paper out of a protocol.

CONFIDENTIALITY.md: `raw.json` and `assay.json` may hold what a published source said,
cited. A committed protocol may not. It uses generic reagent names with no concentrations,
no vendors, no enzyme names and no compositions.

So S8 never carries a name across. `unitops.json` has no reagent-name field at all, and a
reagent here is chosen from a CLOSED vocabulary by what the op does - which means a
paper's wording cannot reach a protocol even if someone later adds a name field upstream.
Every note in a generated protocol is built from this package's own sentences for the same
reason.

`screen()` is the belt to that braces: it reads a finished protocol and reports
concentrations, molarities, vendor marks, enzyme names, buffer compositions, oligo
sequences and lab file paths. It runs on hand-written protocols too, and S8 refuses to
write a file that fails it.
"""

from __future__ import annotations

import re

# The whole vocabulary. A name not in here cannot be written into a protocol.
VOCABULARY = (
    "capsule_suspension",
    "cell_suspension",
    "bead_suspension",
    "wash_buffer",
    "rinse_buffer",
    "lysis_buffer",
    "culture_medium",
    "reaction_mix",
    "co_flow_reagent",
    "dissociation_reagent",
    "quench_buffer",
    "resuspension_buffer",
    "shell_polymer",
    "carrier_oil",
    "stain",
    "reagent",
)

# What each unit op calls for. Keyed by (op kind, particles kind); ('*') matches any
# particles. The fallback is the bare name 'reagent' plus a recorded gap, never a guess at
# what the chemistry was.
BY_OP = {
    ("load", "capsules"): "capsule_suspension",
    ("load", "cells"): "cell_suspension",
    ("load", "beads"): "bead_suspension",
    ("load", "none"): "reagent",
    ("culture", "*"): "culture_medium",
    ("transfer", "capsules"): "capsule_suspension",
    ("transfer", "cells"): "cell_suspension",
    ("transfer", "*"): "resuspension_buffer",
    ("separate", "capsules"): "capsule_suspension",
    ("separate", "cells"): "cell_suspension",
    ("separate", "beads"): "bead_suspension",
    ("separate", "none"): "wash_buffer",
    ("wash", "*"): "wash_buffer",
    ("lyse", "*"): "lysis_buffer",
    ("incubate", "cells"): "culture_medium",
    ("incubate", "*"): "reaction_mix",
    ("thermocycle", "*"): "reaction_mix",
    ("mix", "*"): "co_flow_reagent",
    ("merge", "*"): "co_flow_reagent",
    ("encapsulate", "*"): "shell_polymer",
    ("cure", "*"): "reagent",
    ("collect", "*"): "wash_buffer",
    ("pool", "*"): "resuspension_buffer",
    ("split", "*"): "resuspension_buffer",
    ("image", "*"): "stain",
}

# A rinse immediately before a deliberate lyse is its own reagent: it is not a culture
# wash, and the protocol should not read as though it were.
PRELYSIS_REAGENT = "rinse_buffer"
RELEASE_REAGENT = "resuspension_buffer"

_UNSAFE = (
    ("a molar concentration",
     re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*(?:M|mM|uM|nM|pM)(?![A-Za-z])")),
    ("a mass concentration",
     re.compile(r"\d+(?:\.\d+)?\s*(?:[mun\u00b5]?g\s*/\s*[mun\u00b5]?[lL])", re.I)),
    ("an activity concentration", re.compile(r"\bU\s*/\s*[mun\u00b5]?L\b")),
    ("a w/v or v/v percentage", re.compile(r"%\s*\(?\s*[wv]\s*/\s*[wv]", re.I)),
    # "10x stock" is a concentration; "3.2 x 3.2 mm" is a rectangle. The difference is
    # what follows the x, so a dimension and a scale factor are both let through.
    ("a stock multiple",
     re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)?\s?x\b(?!\s*(?:\d|capsule|diameter|the\b))", re.I)),
    ("a vendor or catalogue reference",
     re.compile(r"\u2122|\u00ae|\bcat\.?\s?(?:no|#)|\bcatalog|\blot\s?(?:no|#)|\bpart\s?no\b", re.I)),
    ("a named vendor",
     re.compile(r"\b(?:thermo\s?fisher|invitrogen|sigma|merck|qiagen|illumina|takara|roche|"
                r"bio-?rad|promega|lonza|miltenyi|corning|gibco|new\s?england\s?biolabs|"
                r"10x\s?genomics|agilent|eppendorf)\b", re.I)),
    ("a named enzyme or kit",
     re.compile(r"\b(?:trypsin|accutase|tryple|collagenase|dispase|liberase|proteinase|"
                r"protease|rnase|dnase|superscript|maxima|tn5|tagmentase|transposase|"
                r"polymerase|ligase|exonuclease|reverse\s?transcriptase)\b", re.I)),
    ("a buffer composition",
     re.compile(r"\b(?:guanidinium|gitc|thiocyanate|triton|tween|sds|edta|egta|hepes|tris|"
                r"dtt|beta-?mercaptoethanol|pbs|dmem|rpmi|fbs|bsa|glycerol|formamide)\b", re.I)),
    ("an oligo or barcode sequence", re.compile(r"\b[ACGTU]{8,}\b")),
    ("a lab file store",
     re.compile(r"\b(?:labstep|onedrive|sharepoint|dropbox|google\s?drive)\b|/Users/|[A-Z]:\\", re.I)),
)

# Where a string may legitimately look risky: a chamber id is the chip's, not ours.
SCREENED_FIELDS = ("name", "source", "description", "note", "reagent")


class ReagentError(ValueError):
    """A reagent name is not in the generic vocabulary."""


def name_for(op: dict, purpose: str = "", chamber_is_empty: bool = True,
             next_kind: str = "") -> tuple:
    """(generic reagent name, gap or '') for a unit op.

    `purpose` overrides the two cases the op kind alone does not carry: a pre-lysis rinse
    and a release. `chamber_is_empty` and `next_kind` settle one real ambiguity: a `load`
    into an EMPTY chamber brings in what is being processed (the capsule or cell
    suspension), while a `load` into a chamber that already holds liquid is delivering a
    reagent, and what that reagent is for is said by the op that follows it - a timed hold
    is a reaction, a culture is medium. Nothing here reads the source's words.
    """
    if purpose == "prelysis_rinse":
        return PRELYSIS_REAGENT, ""
    if purpose in ("release", "lift"):
        return RELEASE_REAGENT, ""
    kind = str(op.get("kind", ""))
    particles = str((op.get("particles") or {}).get("kind", "none"))
    if op.get("shear_class") == "prelysis_rinse" and kind == "wash":
        return PRELYSIS_REAGENT, ""
    if kind in ("load", "merge") and not chamber_is_empty:
        if next_kind in ("incubate", "thermocycle"):
            return "reaction_mix", ""
        if next_kind == "culture":
            return "culture_medium", ""
        if next_kind == "lyse":
            return "lysis_buffer", ""
        return "reagent", (f"op {op.get('op')} ({kind}) delivers a reagent into a chamber that "
                           f"already holds liquid, and neither it nor the op after it says "
                           f"what the reagent is for, so the step names a bare 'reagent'")
    for key in ((kind, particles), (kind, "*")):
        if key in BY_OP:
            return BY_OP[key], ""
    return "reagent", (f"op {op.get('op')} ({kind}) has no generic reagent in "
                       f"protocolgen/reagents.py, so the step names a bare 'reagent': "
                       f"add the mapping rather than let a source's wording through")


def check_name(name: str) -> None:
    if name not in VOCABULARY:
        raise ReagentError(
            f"reagent {name!r} is not in the generic vocabulary "
            f"({', '.join(VOCABULARY)}). CONFIDENTIALITY.md: a committed protocol uses "
            f"generic names only, so add one here deliberately instead of passing a "
            f"source's wording through"
        )


def screen_text(text: str, where: str) -> list:
    """Every confidentiality problem in one string."""
    out = []
    for label, pattern in _UNSAFE:
        match = pattern.search(text or "")
        if match:
            out.append(f"{where}: {label} ({match.group(0).strip()!r}) must not reach a "
                       f"committed protocol (CONFIDENTIALITY.md)")
    return out


def screen(protocol: dict) -> list:
    """Every confidentiality problem in a finished protocol. Empty list means it is clean."""
    out = []
    for field in SCREENED_FIELDS:
        if isinstance(protocol.get(field), str):
            out += screen_text(protocol[field], field)
    for step in protocol.get("steps", []) or []:
        label = f"step {step.get('step')}"
        for field in SCREENED_FIELDS:
            value = step.get(field)
            if isinstance(value, str):
                out += screen_text(value, f"{label} {field}")
        for name in [step.get("reagent")] + list(step.get("reagents") or []):
            if isinstance(name, str) and name not in VOCABULARY:
                out.append(f"{label}: reagent {name!r} is not in the generic vocabulary "
                           f"(protocolgen/reagents.py VOCABULARY)")
    return out

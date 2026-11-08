"""Stage 2: source text -> protocol IR (contracts/schemas/protocol_ir.schema.json).

run(source: dict, constraints: dict, agent: autochip.llm.Agent, out_dir: Path) -> dict

The agent reads the source with prompts/extract.md as its system prompt and returns typed steps.
validate() checks the answer (schema + the semantic rules below) and llm.Agent sends any problems
back for a repair. With the rules backend agent.ask_json() returns None and rules_extract() does the
same job deterministically: numbered lines / bullets / table rows / Methods sentences become steps,
keywords pick the action, regexes pull out volumes, times, temperatures, repeats, light, cycles and g.

Semantic rules (validate):
  - step n runs 1..N in order; every reagent id a step uses is declared in `reagents`
  - a step's input_material matches the previous step's output_material (or one of them is none/null)
  - specific actions are backed by words in the step text (a "centrifuge" step mentions a spin, ...)
  - durations, volumes and flows are not negative; repeats set when the text says twice / N times
  - on_chip_hint on every step; source_ref on every step of a protocol or paper
  - prompt-only sources: placeholder true, assumptions listed, every step says whether it is assumed
  - no nucleotide sequences anywhere
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import sys

from autochip import constraints as C
from autochip.llm import AgentError, schema_errors

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "contracts" / "schemas" / "protocol_ir.schema.json"
PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "extract.md"
MAX_PROMPT_TEXT = 110_000
FALLBACK_PREFIX = "FALLBACK:"   # first assumption when a live agent failed and the rules were used instead

_SCHEMA: dict | None = None


def schema() -> dict:
    global _SCHEMA
    if _SCHEMA is None:
        _SCHEMA = json.loads(SCHEMA_PATH.read_text())
    return _SCHEMA


# ============================================================================================ entry point
def run(source: dict, constraints: dict, agent, out_dir: Path | None = None) -> dict:
    """Protocol IR for the source: from the agent if there is one, else from the built-in rules."""
    obj = None
    if agent is not None:
        try:
            obj = agent.ask_json("extract", PROMPT_PATH.read_text(), user_prompt(source, constraints),
                                 validate=lambda o: validate(o, source))
        except AgentError as e:
            if not getattr(agent, "live", False):
                raise
            print(f"WARNING: extract: the agent ({agent.describe()}) gave no valid protocol IR ({str(e)[:200]}). "
                  f"FALLING BACK to the built-in keyword rules: the steps are rough, check them by hand.",
                  file=sys.stderr, flush=True)
            ir = rules_extract(source)
            ir["assumptions"].insert(0, f"{FALLBACK_PREFIX} the agent could not produce a valid protocol IR "
                                        f"({str(e)[:200]}), so the built-in keyword rules were used instead; "
                                        "check the steps by hand.")
            return ir
    if obj is not None:
        src = obj.setdefault("source", {"kind": source["kind"]})
        if not src.get("file") and source.get("file"):
            src["file"] = source["file"]
        if not src.get("title") and source.get("title"):
            src["title"] = source["title"]
        return obj
    ir = rules_extract(source)
    errs = validate(ir, source)
    hard = schema_errors(ir, schema())
    if hard:
        raise RuntimeError("the rules extractor produced an IR that breaks the schema (a bug): " + "; ".join(hard[:5]))
    if errs:
        print(f"WARNING: extract: rules IR has {len(errs)} problem(s): {errs[0][:160]}", file=sys.stderr, flush=True)
    return ir


def user_prompt(source: dict, constraints: dict | None) -> str:
    """The request sent with prompts/extract.md: source, user's goal, constraints summary, schema."""
    kind = source["kind"]
    text = source.get("text") or ""
    if len(text) > MAX_PROMPT_TEXT:
        text = text[:MAX_PROMPT_TEXT] + "\n[... truncated]"
    parts = ["Turn the SOURCE below into one protocol IR JSON object that follows the JSON schema at the end.",
             f"## Source\nkind: {kind}\nfile: {source.get('file')}\ntitle: {source.get('title')}"]
    if kind == "prompt":
        parts.append("## The user's goal (prompt-only source)\nThere is no written protocol. Expand this goal into a "
                     "complete, plausible protocol based on standard published methods. Mark every step and value you "
                     "add as assumed, list them in `assumptions`, set `placeholder: true` and `source_ref: \"prompt\"`. "
                     "Confidentiality: do NOT fill in cycle numbers, or times/temperatures for lysis, reverse "
                     "transcription, enzymatic digestion/release or PCR: leave those fields null and write "
                     "'placeholder' in the text, so the lab sets them.")
    elif source.get("prompt"):
        parts.append("## The user's goal / focus\n" + source["prompt"].strip())
    if kind == "paper" and source.get("methods_chars"):
        parts.append("The paper's Methods section comes first in the text below; take the steps from it.")
    if constraints:
        try:
            parts.append("## Constraints (context only: what the chips can be made with; do not drop steps because of them)\n"
                         + C.summary(constraints))
        except (KeyError, TypeError):
            pass
    parts.append("## Source text\n<<<SOURCE\n" + text.strip() + "\nSOURCE>>>")
    parts.append("## JSON schema (protocol_ir.schema.json)\n" + json.dumps(schema(), separators=(",", ":")))
    return "\n\n".join(parts)


# ============================================================================================ validation
SOLUBLE = {"liquid", "lysate", "nucleic_acid", "library"}

# action -> words its step text should contain (only for actions with a clear vocabulary)
ACTION_HINTS = {
    "centrifuge": r"centrifug|spin|spun|pellet|\d\s*(?:x\s*)?g\b|rcf|rpm",
    "encapsulate": r"encapsul|capsul|droplet|co-?flow|flow[- ]?focus|wrap|compartment|emulsi|junction|bead",
    "emulsify": r"emulsi|droplet",
    "cure": r"cur|cross-?link|polymeri|light|uv\b|\dnm\b|\d nm\b|photo|expos",
    "break_emulsion": r"emulsion|break|oil|demulsif|perfluoro",
    "lyse": r"lys",
    "reverse_transcribe": r"revers|\brt\b|cdna|transcri|first[- ]strand",
    "amplify": r"pcr|amplif|cycl",
    "magnetic_separate": r"magnet",
    "elute": r"elut|eluate|off the beads|from the beads",
    "bind_beads": r"bead|bind|bound|captur",
    "wash_cells": r"wash|rins|exchang|replac|buffer|remov",
    "wash_particles": r"wash|rins|exchang|replac|buffer|remov",
    "count": r"count|viab|number|density|concentration",
    "image": r"imag|microscop|photo|inspect|observ|visual|check",
    "filter": r"filter|strain|mesh|siev|µm|um\b|micron",
    "split": r"split|divid|aliquot|portion|distribut|partition|separat|fraction|sublibrar",
    "pool": r"pool|combin|merg",
    "culture": r"cultur|grow|expand|different|maintain|incubat",
    "dissociate": r"dissociat|detach|single[- ]cell|enzym|trypsin|digest|lift",
    "quench": r"quench|stop|neutrali|inactivat",
    "fragment": r"fragment|tagment|shear",
    "ligate": r"ligat|adapt",
}
# first word of the step text -> actions it allows
FIRST_WORD = {
    "centrifuge": {"centrifuge", "wash_cells", "wash_particles"}, "spin": {"centrifuge", "wash_cells", "wash_particles"},
    "lyse": {"lyse"}, "elute": {"elute"}, "encapsulate": {"encapsulate", "emulsify"},
    "count": {"count", "measure", "image"}, "image": {"image", "measure", "count"}, "filter": {"filter"},
    "wash": {"wash_cells", "wash_particles", "magnetic_separate"}, "rinse": {"wash_cells", "wash_particles"},
    "amplify": {"amplify"}, "pool": {"pool"}, "split": {"split"}, "photocure": {"cure"}, "cure": {"cure"},
}
_LIGHT_RX = re.compile(r"cross-?link|polymeri|light|\buv\b|photo|\d\s*nm\b|expos")
_COLD_RX = re.compile(r"\bon ice\b|\bice[- ]cold\b|\bice\b|\bcold\b|\bchill\w*|\b4\s*°?\s*c\b")
_SEQ_RX = re.compile(r"(?<![A-Za-z])[ACGTUN]{12,}(?![A-Za-z])")


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def _has_numbers(step: dict) -> bool:
    cond = step.get("conditions") or {}
    return any(isinstance(step.get(k), (int, float)) for k in ("volume_ul", "duration_s", "temp_c", "flow_ul_min")) \
        or (step.get("repeats") or 1) > 1 or bool(cond.get("light_nm")) or bool(cond.get("thermal_cycles"))


def _materials_agree(prev_out, inp) -> bool:
    return inp in (None, "none") or prev_out == inp or (prev_out in SOLUBLE and inp in SOLUBLE)


_LAB_SET_ACTIONS = {"lyse", "reverse_transcribe", "amplify", "release", "fragment", "ligate"}
_CYCLES_RX = re.compile(r"\b\d+\s*(?:x\s*)?(?:thermal\s+|pcr\s+)?cycles\b", re.I)


def validate(ir: dict, source: dict | None = None) -> list[str]:
    """Schema errors plus the semantic rules in the module docstring, as readable strings."""
    errs = schema_errors(ir, schema())
    if not isinstance(ir, dict) or not isinstance(ir.get("steps"), list):
        return errs or ["steps: missing"]
    steps = [s for s in ir["steps"] if isinstance(s, dict)]
    kind = (source or {}).get("kind")

    ns = [s.get("n") for s in steps]
    if ns != list(range(1, len(steps) + 1)):
        errs.append(f"steps: n must run 1..{len(steps)} in order with no gaps (got {ns[:40]})")

    declared = [r.get("id") for r in ir.get("reagents") or [] if isinstance(r, dict)]
    dup = sorted({d for d in declared if declared.count(d) > 1})
    if dup:
        errs.append(f"reagents: ids declared more than once: {dup}")
    missing: dict[str, list] = {}
    for s in steps:
        for r in s.get("reagents") or []:
            if r not in declared:
                missing.setdefault(r, []).append(s.get("n"))
    for r, where in missing.items():
        errs.append(f"reagent '{r}' is used in step(s) {where} but not declared in the top-level reagents list; "
                    f"declare it ({{\"id\": \"{r}\", \"role\": ...}}) or use a declared id")

    prev = None
    for s in steps:
        n, inp, out = s.get("n"), s.get("input_material"), s.get("output_material")
        if prev is not None and not _materials_agree(prev[1], inp):
            errs.append(f"step {n}: input_material '{inp}' but step {prev[0]} before it outputs '{prev[1]}'; make the "
                        f"input equal the previous output (use 'none' only for a side step that does not touch the main material)")
        if out not in (None, "none"):
            prev = (n, out)

    for s in steps:
        n = s.get("n")
        text = str(s.get("text") or "")
        low = text.lower()
        act = s.get("action")
        hint = ACTION_HINTS.get(act)
        if hint and not re.search(hint, low):
            errs.append(f"step {n}: action '{act}' but the text does not say so ({text[:80]!r}); fix the action or say in "
                        f"the text what happens")
        first = re.match(r"[a-z-]+", low)
        if first and first.group(0) in FIRST_WORD and act not in FIRST_WORD[first.group(0)]:
            errs.append(f"step {n}: text starts with '{first.group(0)}' but action is '{act}' "
                        f"(expected one of {sorted(FIRST_WORD[first.group(0)])})")
        for key in ("volume_ul", "duration_s", "flow_ul_min"):
            v = s.get(key)
            if isinstance(v, (int, float)) and v < 0:
                errs.append(f"step {n}: {key} is negative ({v})")
        t = s.get("temp_c")
        if isinstance(t, (int, float)) and not -200 <= t <= 150:
            errs.append(f"step {n}: temp_c {t} is not a plausible temperature in C")
        cond = s.get("conditions") or {}
        if isinstance(cond.get("light_nm"), (int, float)) and cond["light_nm"] <= 0:
            errs.append(f"step {n}: conditions.light_nm must be positive")
        if isinstance(cond.get("thermal_cycles"), int) and cond["thermal_cycles"] < 1:
            errs.append(f"step {n}: conditions.thermal_cycles must be at least 1")
        if act == "cure" and not cond.get("light_nm") and not _LIGHT_RX.search(low) and \
                (cond.get("cold") or _COLD_RX.search(low)):
            errs.append(f"step {n}: a cold hold (on ice / 4 C, e.g. to let a shell gel) is action 'incubate' with "
                        f"conditions.cold true and temp_c 4; 'cure' is only for light, UV or chemical crosslinking")
        if re.search(r"\bon ice\b|\bice[- ]cold\b", low) and not cond.get("cold"):
            errs.append(f"step {n}: the text says on ice but conditions.cold is not true")
        if _repeats(text) and not s.get("repeats"):
            errs.append(f"step {n}: the text says it is repeated ({text[:60]!r}) but repeats is not set")
        if "on_chip_hint" not in s:
            errs.append(f"step {n}: on_chip_hint missing (required / possible / off_chip)")
        if kind in ("protocol", "paper") and not s.get("source_ref"):
            errs.append(f"step {n}: source_ref missing (where in the source the step comes from)")
        if kind == "prompt" and "assumed" not in s:
            errs.append(f"step {n}: say whether the step is assumed (assumed: true for steps or values you added)")
        if kind == "prompt" and s.get("assumed") is False and not re.search(r"\d", (source or {}).get("text") or "") \
                and _has_numbers(s):
            errs.append(f"step {n}: the goal gives no numbers, so the values in this step were filled in: set assumed: true")

    if kind and isinstance(ir.get("source"), dict) and ir["source"].get("kind") != kind:
        errs.append(f"source.kind must be '{kind}' (the kind of the source you were given)")
    if kind == "prompt":
        if ir.get("placeholder") is not True:
            errs.append("placeholder must be true for a prompt-only source (the values are assumed)")
        if not ir.get("assumptions"):
            errs.append("assumptions must list what you filled in (a prompt-only source states almost nothing)")
    if kind == "prompt":
        # Confidentiality: these conditions are never invented for a prompt-only source (the lab sets them).
        for s_ in steps:
            act, cond = s_.get("action"), s_.get("conditions") or {}
            if cond.get("thermal_cycles") is not None:
                errs.append(f"step {s_.get('n')}: leave conditions.thermal_cycles null (cycle numbers are set by the lab)")
            if act in _LAB_SET_ACTIONS and (s_.get("temp_c") is not None or s_.get("duration_s") is not None):
                errs.append(f"step {s_.get('n')}: leave temp_c and duration_s null for {act} (placeholder: the lab sets them)")
        if any(_CYCLES_RX.search(x) for x in _strings(ir)):
            errs.append("don't write a cycle number anywhere (text, notes or assumptions): say 'placeholder number of cycles'")
    if any(_SEQ_RX.search(x) for x in _strings(ir)):
        errs.append("remove nucleotide sequences: refer to primers, barcodes and oligos by generic names only")
    return errs


# ============================================================================================ rules: vocab
_STRONG = [  # (action, regex); in a clause the earliest match wins
    ("reverse_transcribe", r"\breverse[- ]transcri\w*|\brt[- ](?:mix|reaction|master|enzyme|step)\b|\bfirst[- ]strand|\bcdna synthesis"),
    ("break_emulsion", r"\bbreak\w*\s+(?:of\s+)?(?:the\s+)?emulsions?|\bemulsions?\s+(?:was\s+|were\s+|is\s+)?broken|"
                       r"\bemulsion[- ]break\w*|\bde-?emulsif\w*|\bdemulsif\w*"),
    ("encapsulate", r"\bencapsulat\w*|\bco-?flow\w*|\bflow[- ]focus\w*|\bwrap(?:s|ped|ping)?\b|\bcompartmentali[sz]\w*|"
                    r"\bdroplet generation|\b(?:make|generate|form)\w*\s+(?:the\s+)?(?:droplets|capsules)\b"),
    ("emulsify", r"\bemulsif\w*"),
    # cure = light / UV / chemical crosslinking only; a cold hold that gels a shell is incubate (cold)
    ("cure", r"\b(?:photo-?)?cur(?:e|ed|es|ing)\b|\bcross-?link\w*|\bpolymeri[sz](?:e|ed|es|ing|ation)\b|"
             r"\bexpos\w*\b[^.;]{0,30}\b(?:light|uv|\d{3}\s*nm)\b"),
    ("lyse", r"\blys(?:e|ed|es|ing|is)\b"),
    ("amplify", r"\bamplif\w*|\bpcr\b|\bthermal[- ]cycl\w*|\b\d+\s*(?:pcr\s+)?cycles\b"),
    ("fragment", r"\bfragment(?:ed|ing|ation)\b|\btagment\w*|\bshear(?:ed|ing)?\b"),
    ("ligate", r"\bligat(?:e|ed|es|ing|ion)\b"),
    ("elute", r"\belut(?:e|ed|es|ing|ion)\b"),
    ("bind_beads", r"\bbind(?:s|ing)?\b|\bbound\b|\bcaptur(?:e|ed|es|ing)\b|\bbead suspension\b|"
                   r"\b(?:add|mix)(?:ed|es|ing)?\b[^.;]{0,60}\bbeads?\b"),
    ("magnetic_separate", r"\bmagnet\b|\bmagnetic (?:stand|rack|separat\w*|field)"),
    ("dissociate", r"\bdissociat\w*|\bdetach\w*|\btrypsini[sz]\w*"),
    ("quench", r"\bquench\w*|\bneutrali[sz]\w*|\bstop (?:the )?(?:enzyme|reaction|digestion|dissociation)"),
    ("wash", r"\bwash(?:ed|es|ing)?\b|\brins(?:e|ed|es|ing)\b"),
    ("centrifuge", r"\bcentrifug\w*|\bspin(?:s|ning)?\b(?! ?coat)|\bspun\b|\bpellet(?:ed|ing)\b|\bpellet (?:the )?cells"),
    ("filter", r"\bfilter(?:ed|s|ing)?\b|\bstrain(?:ed|ing)\b|\bstrainer\b|\bsiev(?:e|ed|ing)\b|\bmesh\b"),
    ("count", r"\bcount(?:s|ed|ing)?\b|\bviabilit\w*|\bhae?mocytometer"),
    ("image", r"\bimag(?:e|es|ed|ing)\b|\bmicroscop\w*|\bphotograph\w*"),
    ("measure", r"\bmeasur\w*|\bquantif\w*|\bassess\w*|\bdetermin(?:e|ed|ing)\b|\bcheck(?:s|ed|ing)?\b|\bqc\b"),
    ("culture", r"\bcultur(?:e|ed|es|ing)\b(?!\s+(?:medium|media|vessel|plate|dish|flask|well|chamber|insert|surface))|"
                r"\bgrow(?:s|n|ing)?\b|\bexpand(?:ed|ing)?\b|\bdifferentiat\w*|\bmaintain(?:ed|ing)?\b"),
    ("seed", r"\bseed(?:s|ed|ing)?\b|\bplated\b|\bplate (?:the )?cells\b"),
    ("feed", r"\bfeed(?:s|ing)?\b|\bfed\b|\bmedi(?:um|a) (?:change|exchange|replacement)|"
             r"\b(?:chang|replac|refresh)\w* (?:the )?medi(?:um|a)\b"),
    ("split", r"\bsplit(?:s|ting)?\b|\bdivid(?:e|ed|es|ing)\b|\baliquot\w*|\bdistribut(?:e|ed|ing)\b|"
              r"\binto (?:\w+ )?(?:equal )?portions\b"),
    ("pool", r"\bpool(?:s|ed|ing)?\b|\bcombin(?:e|ed|es|ing)\b|\bmerg(?:e|ed|es|ing)\b"),
    ("release", r"\breleas(?:e|ed|es|ing)\b|\bdigest(?:s|ed|ing)?\b|\bdissolv\w*|\bdegrad(?:e|ed|ing)\b"),
    ("purify", r"\bpurif\w*|\bclean(?:ed|ing)?[- ]?up\b|\bcleanup\b|\bclean(?:ed|ing)?\b(?=(?:\s+\w+){0,3}\s+up\b)|\bdesalt\w*"),
    ("store", r"\bstor(?:e|ed|es|ing|age)\b|\bfreez(?:e|er|ing)\b|\bfrozen\b"),
]
_WEAK = [  # used only when no strong word is in the clause
    ("resuspend", r"\bre-?suspen\w*"),
    ("add_reagent", r"\badd(?:s|ed|ing)?\b|\bsupplement\w*|\bexchang\w*|\breplac\w*|\bintroduc\w*"),
    ("mix", r"\bmix(?:es|ed|ing)?\b|\bpipett\w* up and down|\bvortex\w*|\binvert\w*|\btriturat\w*"),
    ("incubate", r"\bincubat\w*|\bair[- ]?dr(?:y|ied|ying)\b|\bdr(?:y|ied)\b|\bkeep\b|\bkept\b|\bhold\b|\bheld\b|"
                 r"\bleave\b|\bwait\w*|\bon ice\b"),
    ("collect", r"\bcollect\w*|\bharvest\w*|\bretriev\w*|\brecover\w*"),
    ("transfer", r"\btransfer\w*|\bmov(?:e|ed|es|ing)\b|\bload(?:s|ed|ing)?\b|\binject\w*"),
]
_STRONG_RX = [(a, re.compile(r, re.I)) for a, r in _STRONG]
_WEAK_RX = [(a, re.compile(r, re.I)) for a, r in _WEAK]
_MINOR_LEAD = re.compile(r"^(?:then\s+|and\s+)?(?:remove|discard|aspirate|decant|pour off|take off|repeat)\b", re.I)
_MINOR_ANY = re.compile(r"\b(?:remov|discard|aspirat|decant)\w*|\brepeat\w*", re.I)

_VERBS = (r"(?:add|mix|incubat|wash|rins|centrifug|spin|spun|pellet|re-?suspen|filter|pass|strain|count|check|imag|measur|"
          r"quantif|encapsulat|co-?flow|wrap|emulsif|cur|photocur|cross-?link|polymeri|break|lys|revers|amplif|run|"
          r"fragment|tagment|ligat|bind|bound|captur|place|elut|purif|clean|split|divid|aliquot|pool|combin|releas|"
          r"digest|collect|harvest|recover|transfer|mov|load|stor|freez|keep|kept|remov|discard|aspirat|repeat|"
          r"detach|dissociat|quench|cultur|grow|seed|feed|exchang|air-dr|dry|dried|transcrib|stain|heat|cool|chill|"
          r"wait|leave|hold|stop|sort)\w*")
_SPLIT_RX = re.compile(
    r"\s*;\s*|,\s*(?:and\s+)?then\s+|\s+and\s+then\s+|,\s*followed\s+by\s+|,\s+and\s+|"
    r"(?:,\s*|\s+and\s+|\s+then\s+)(?=(?:(?:was|were|is|are)\s+)?(?:then\s+)?(?:gently\s+|briefly\s+)?" + _VERBS + r"\b)",
    re.I)

# (regex, id, role, extra); matched on the text without parentheses, with a context word just before
_REAGENTS = [
    (r"dissociation (?:enzyme|reagent|solution)|\btrypsin\w*|\baccutase\b", "dissociation_enzyme", "detach the cells", {}),
    (r"quench(?:ing)? (?:medium|media|buffer|solution)", "quench_medium", "stop the dissociation enzyme", {}),
    (r"lysis (?:buffer|solution|mix|reagent)", "lysis_buffer", "lyse the cells", {}),
    (r"reverse[- ]transcription (?:mix|master mix|reagents?|buffer)|\brt (?:mix|master mix|reagents?)", "rt_mix",
     "reverse transcription", {}),
    (r"(?:index )?primer mix|\bpcr (?:mix|master mix|reagents?)|\bmaster mix|\bpolymerase mix", "pcr_mix", "amplification", {}),
    (r"(?:emulsion[- ]breaking|demulsifying) (?:agent|reagent)|\bdemulsifier|\bperfluoro-?octanol|\bpfo\b", "emulsion_breaker",
     "break the emulsion", {"immiscible": True}),
    (r"shell (?:polymer|solution|phase|precursor)(?: solution)?|\bgelma\b|\bhydrogel (?:precursor|solution)", "shell_polymer",
     "capsule shell", {"viscous": True}),
    (r"core (?:solution|phase|buffer)|\bdextran\b", "core_solution", "capsule core", {"viscous": True}),
    (r"(?:fluorinated|fluorous|carrier|mineral) oil(?: (?:with|containing) surfactant)?|\boil\b(?: (?:with|containing) surfactant)?",
     "OIL", "continuous (oil) phase", {"immiscible": True}),
    (r"photo-?initiator", "photoinitiator", "start the photocuring", {}),
    (r"elution buffer", "elution_buffer", "elute from the beads", {}),
    (r"nuclease[- ]free water|\bwater\b", "water", "elution / dilution", {}),
    (r"\d+\s*%\s*ethanol|\bethanol\b", "ethanol_wash", "wash the beads", {}),
    (r"bead suspension|(?:para)?magnetic beads?|\bspri beads?|purification beads?|\bbeads\b", "magnetic_beads",
     "bind nucleic acid", {}),
    (r"(?:release|digestion|shell[- ]digest\w*) enzyme|\bcollagenase\b|\bprotease\b", "digestion_enzyme",
     "digest the capsule shells", {}),
    (r"viability (?:stain|dye)|\btrypan blue|\bstain\b|\bdye\b", "viability_stain", "mark live / dead cells", {}),
    (r"ligation (?:mix|master mix|buffer)|\bligase\b|\badapters?\b", "ligation_mix", "ligate adapters", {}),
    (r"(?:fragmentation|tagmentation) (?:mix|enzyme|buffer)|\btransposase\b", "fragmentation_mix", "fragment the DNA", {}),
    (r"wash buffer", "wash_buffer", "wash", {}),
    (r"(?:culture |growth |fresh |complete )?medi(?:um|a)\b", "culture_medium", "cell culture medium", {}),
    (r"\bpbs\b|\bsaline\b", "pbs", "rinse", {}),
    (r"\bbuffer\b", "wash_buffer", "wash", {"strict": True}),
]
_REAGENTS_RX = [(re.compile(r, re.I), i, role, extra) for r, i, role, extra in _REAGENTS]
_CTX = {"add", "adds", "added", "adding", "with", "in", "into", "using", "containing", "to", "of", "for", "by", "plus",
        "exchange", "exchanged", "supplemented", "co-flow", "co-flowed", "flow", "flowed", "load", "loaded", "inject",
        "injected", "reagent:"}
_NEG = {"remove", "removed", "removes", "removing", "discard", "discarded", "aspirate", "aspirated"}

_DEFAULT_REAGENTS = {
    "culture": ["culture_medium"], "feed": ["culture_medium"], "dissociate": ["dissociation_enzyme"],
    "quench": ["quench_medium"], "wash_cells": ["wash_buffer"], "wash_particles": ["wash_buffer"],
    "lyse": ["lysis_buffer"], "reverse_transcribe": ["rt_mix"], "amplify": ["pcr_mix"],
    "break_emulsion": ["emulsion_breaker"], "bind_beads": ["magnetic_beads"], "elute": ["elution_buffer"],
    "release": ["digestion_enzyme"], "fragment": ["fragmentation_mix"], "ligate": ["ligation_mix"],
}
_ROLES = {i: (role, extra) for _, i, role, extra in _REAGENTS}
_ROLES["fluorinated_oil"] = _ROLES["oil"] = ("continuous (oil) phase", {"immiscible": True})

_ON_CHIP = {"encapsulate": "required", "emulsify": "required", "centrifuge": "off_chip", "store": "off_chip",
            "measure": "off_chip"}

_NUM = r"(\d+(?:\.\d+)?)"
_RANGE = _NUM + r"(?:\s*(?:-|–|—|to)\s*" + _NUM + r")?"
_VOL_RX = re.compile(r"(?<![\w.])" + _RANGE + r"\s*(µl|μl|ul|nl|ml|l|microlit(?:re|er)s?|millilit(?:re|er)s?)(?![\w/])", re.I)
_VOL_MULT = {"µl": 1, "μl": 1, "ul": 1, "nl": 0.001, "ml": 1000, "l": 1e6}
_DUR_RX = re.compile(r"(?<![\w.])" + _RANGE + r"\s*(seconds?|secs?|s|minutes?|mins?|hours?|hrs?|h|days?|weeks?)(?![\w/])", re.I)
_TEMP_RX = re.compile(r"(?<![\w.])([-–−]?\d+(?:\.\d+)?)(?:\s*(?:-|–|to)\s*(\d+(?:\.\d+)?))?\s*(?:°|º|˚)\s*C\b|"
                      r"(?<![\w.])([-–−]?\d+(?:\.\d+)?)\s*(?:deg(?:rees)?\s*C?|C)(?![A-Za-z0-9])")
_FLOW_RX = re.compile(r"(?:(\w+)\s+)?(?<![\w.])(\d+(?:\.\d+)?)\s*(µl|μl|ul|nl|ml)\s*/\s*(min|h|hr|s)\b", re.I)
_NM_RX = re.compile(r"(?<![\w.])(\d{3})\s*nm\b", re.I)
_CYC_RX = re.compile(r"(?<![\w.])(\d+)\s*(?:pcr\s+|thermal\s+|amplification\s+)?cycles\b", re.I)
_G_RX = re.compile(r"(?<![\w.])(\d[\d,]*)\s*(?:x\s*|×\s*)?(g|rpm|rcf)\b(?![\w/])", re.I)
_SIZE_RX = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(?:µm|μm|um|microns?|micromet(?:re|er)s?)\b", re.I)
_WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8}
_MAGNET_RX = re.compile(r"\b(?:on|onto|to|against|using|with|in)\s+(?:a\s+|the\s+)?magnet(?:ic\s+(?:stand|rack))?\b|"
                        r"\bmagnetic (?:stand|rack|separation|field)\b|\bplace\w*\b[^.;]{0,25}\bmagnet\b", re.I)


# ============================================================================================ rules: helpers
def _slug(s: str, limit: int = 48) -> str:
    words = [w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if w]
    out = ""
    for w in words:
        if out and len(out) + 1 + len(w) > limit:
            break
        out = f"{out}_{w}" if out else w[:limit]
    return out or "protocol"


def _scrub(text: str) -> str:
    return _SEQ_RX.sub("[sequence removed]", text)


def _mask_parens(text: str) -> tuple[str, list[str]]:
    saved: list[str] = []

    def keep(m):
        saved.append(m.group(0))
        return f"\x00{len(saved) - 1}\x00"
    prev = None
    while prev != text:
        prev, text = text, re.sub(r"\([^()]*\)", keep, text)
    return text, saved


def _unmask(text: str, saved: list[str]) -> str:
    while "\x00" in text:
        new = re.sub(r"\x00(\d+)\x00", lambda m: saved[int(m.group(1))], text)
        if new == text:
            break
        text = new
    return text


def _no_parens(text: str) -> str:
    prev = None
    while prev != text:
        prev, text = text, re.sub(r"\([^()]*\)", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _repeats(text: str) -> int | None:
    """How many times a step is done in total, from words like twice / three times / repeat once / x3."""
    t = text.lower()
    w = r"(\d+|one|two|three|four|five|six|seven|eight)"
    m = re.search(r"\b" + w + r"\s+(?:washes|rinses|rounds|repeats|exchanges)\b", t)
    if m:
        n = _WORDNUM.get(m.group(1)) or int(m.group(1))
        return n if n > 1 else None
    m = re.search(r"\brepeat(?:ed|ing)?\b[^.;]{0,40}?\b(once|twice|" + w + r"\s+(?:more\s+)?times)\b", t)
    if m:
        if m.group(1) == "once":
            return 2
        if m.group(1) == "twice":
            return 3
        n = _WORDNUM.get(m.group(2)) or int(m.group(2))
        return n if n > 1 else None
    m = re.search(r"\b(twice|thrice)\b", t)
    if m:
        return 2 if m.group(1) == "twice" else 3
    m = re.search(r"\b" + w + r"\s+times\b", t)
    if m:
        n = _WORDNUM.get(m.group(1)) or int(m.group(1))
        return n if n > 1 else None
    m = re.search(r"(?<![\d.])(\d+)\s*[x×](?![\w.])(?!\s*\d)", t)
    if m and int(m.group(1)) > 1:
        return int(m.group(1))
    m = re.search(r"(?:^|[\s(,])[x×]\s?(\d+)\b", t)
    if m and not re.search(r"\d\s*$", t[:m.start() + 1]) and int(m.group(1)) > 1:
        return int(m.group(1))
    return None


def _quantities(text: str, action: str) -> dict:
    """Numbers in a step's text, in IR units, plus notes on what was simplified."""
    q: dict = {"notes": []}
    t = text.replace("−", "-")
    m = _VOL_RX.search(t)
    if m:
        unit = m.group(3).lower()
        unit = "µl" if unit.startswith("micro") else "ml" if unit.startswith("milli") else unit
        q["volume_ul"] = round(float(m.group(2) or m.group(1)) * _VOL_MULT.get(unit, 1), 3)
    if re.search(r"\bovernight\b", t, re.I):
        q["duration_s"] = 16 * 3600
        q["notes"].append("overnight taken as 16 h")
    else:
        m = _DUR_RX.search(t)
        if m:
            u = m.group(3).lower()
            mult = 1 if u.startswith("s") else 60 if u.startswith("m") else 3600 if u.startswith("h") else \
                86400 if u.startswith("d") else 604800
            q["duration_s"] = round(float(m.group(2) or m.group(1)) * mult, 3)
            if m.group(2):
                q["notes"].append(f"time range {m.group(0).strip()}: upper value used")
    m = _TEMP_RX.search(t)
    if m:
        v = (m.group(1) or m.group(3)).replace("–", "-")
        q["temp_c"] = float(m.group(2)) if m.group(2) else float(v)
    elif re.search(r"room temperature|\bambient\b", t, re.I):
        q["temp_c"] = 22.0
    elif re.search(r"\bon ice\b|\bice[- ]cold\b|\bice\b", t, re.I):
        q["temp_c"] = 4.0
    flows = []
    for fm in _FLOW_RX.finditer(t):
        per = {"min": 1, "h": 1 / 60, "hr": 1 / 60, "s": 60}[fm.group(4).lower()]
        val = float(fm.group(2)) * _VOL_MULT.get(fm.group(3).lower().replace("μ", "µ"), 1) * per
        flows.append((fm.group(1), round(val, 4)))
    if flows:
        q["flow_ul_min"] = flows[0][1]
        if len(flows) > 1:
            q["notes"].append("flows (uL/min): " + ", ".join(f"{name or 'stream'} {v:g}" for name, v in flows))
    m = _NM_RX.search(t)
    if m and 200 <= int(m.group(1)) <= 1000 and action in ("cure", "image", "other", "release"):
        q["light_nm"] = int(m.group(1))
    m = _CYC_RX.search(t)
    if m:
        q["cycles"] = int(m.group(1))
    if action == "centrifuge" or re.search(r"centrifug|\bspin", t, re.I):
        m = _G_RX.search(t)
        if m:
            q["notes"].append(f"{m.group(1)} {m.group(2)}")
    if action in ("encapsulate", "emulsify"):
        m = _SIZE_RX.search(t)
        if m:
            q["size_um"] = float(m.group(1))
    r = _repeats(t)
    if r:
        q["repeats"] = r
    return q


def _reagents_in(text: str, fluorinated: bool) -> list[str]:
    """Generic reagent ids named in the text next to add / with / in / into / ... ."""
    t = _no_parens(text).lower()
    found: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for rx, rid, _role, extra in _REAGENTS_RX:
        for m in rx.finditer(t):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            before = [w for w in re.split(r"[\s/]+", t[:m.start()]) if w][-3:]
            if any(w in _NEG for w in before):
                continue
            if extra.get("strict"):
                if not before or before[-1] not in ("with", "in", "into", "using"):
                    continue
            elif not any(w in _CTX for w in before):
                continue
            taken.append((m.start(), m.end()))
            if rid == "OIL":
                rid = "fluorinated_oil" if fluorinated else "oil"
            found.append((m.start(), rid))
    out: list[str] = []
    for _, rid in sorted(found):
        if rid not in out:
            out.append(rid)
    return out


def _equipment(action: str, text: str, q: dict, cond: dict) -> list[str]:
    t = text.lower()
    eq = []
    if action == "centrifuge" or re.search(r"centrifug|\bspin\b", t):
        eq.append("centrifuge")
    if cond.get("magnet") or action == "magnetic_separate":
        eq.append("magnetic_stand")
    if action == "amplify" or cond.get("thermal_cycles"):
        eq.append("thermocycler")
    if action == "image" or re.search(r"microscop", t):
        eq.append("microscope")
    if cond.get("light_nm"):
        eq.append(f"light_source_{cond['light_nm']}nm")
    elif action == "cure" and re.search(r"\blight\b|\buv\b|\bled\b|photo", t):
        eq.append("light_source")
    if re.search(r"\bice\b", t):
        eq.append("ice_bath")
    if cond.get("co2"):
        eq.append("co2_incubator")
    elif q.get("temp_c") is not None and q["temp_c"] > 30 and action not in ("amplify", "culture"):
        eq.append("heat_block")
    if re.search(r"strainer", t):
        eq.append("cell_strainer")
    elif action == "filter":
        eq.append("filter")
    if action == "count":
        eq.append("cell_counter")
    if action in ("encapsulate", "emulsify") or q.get("flow_ul_min"):
        eq.append("syringe_pumps")
    if action == "measure":
        if re.search(r"concentration|quantif|yield", t):
            eq.append("fluorometer")
        if re.search(r"fragment size|size distribution|fragment analy", t):
            eq.append("fragment_analyzer")
    if cond.get("shaking"):
        eq.append("shaker")
    if action == "store":
        eq.append("freezer" if (q.get("temp_c") or 0) < 0 else "fridge")
    return list(dict.fromkeys(eq))


# ---------------------------------------------------------------------------------------- items -> clauses
def _classify(clause: str) -> tuple[str, str]:
    """(action, strength) with strength strong / weak / minor / none."""
    c = _no_parens(clause).lower().strip(" ,.;:")
    c = re.sub(r"^(?:and|then|and then|next|finally|first)\s+", "", c)
    if _MINOR_LEAD.match(c):
        return "minor", "minor"
    for table, strength in ((_STRONG_RX, "strong"), (_WEAK_RX, "weak")):
        best = None
        for act, rx in table:
            m = rx.search(c)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), act)
        if best:
            return best[1], strength
    if _MINOR_ANY.search(c):
        return "minor", "minor"
    return "other", "none"


def _sentences(text: str) -> list[str]:
    masked, saved = _mask_parens(text)
    protected = re.sub(r"\b(e\.g|i\.e|et al|approx|ca|cf|vs|Fig|Figs|No|Ref|Eq)\.", lambda m: m.group(0).replace(".", "\x01"),
                       masked)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\x00])", protected)
    return [_unmask(p.replace("\x01", "."), saved).strip() for p in parts if p.strip()]


def _clauses(sentence: str) -> list[str]:
    masked, saved = _mask_parens(sentence)
    out, last = [], 0
    for m in _SPLIT_RX.finditer(masked):
        if m.start() == 0:
            continue
        out.append(masked[last:m.start()])
        last = m.start()
    out.append(masked[last:])
    return [_unmask(c, saved) for c in out if c.strip(" ,;")]


_QC = {"count", "measure", "image"}
# "... and incubate 5 min" is part of these steps; after collect / filter / centrifuge / ... it is a step of its own
_INCUBATE_INTO = {"add_reagent", "mix", "bind_beads", "dissociate", "quench", "lyse", "reverse_transcribe", "amplify",
                  "fragment", "ligate", "release", "elute", "resuspend", "seed", "feed", "culture", "incubate",
                  "break_emulsion", "wash", "cure"}


def _groups(text: str, mode: str) -> list[dict]:
    """Split an item (a numbered step, a sentence, ...) into one group of clauses per action."""
    groups: list[dict] = []
    pending: list[str] = []
    for sent in _sentences(text):
        for cl in _clauses(sent):
            act, strength = _classify(cl)
            if strength in ("minor", "none"):
                if groups:
                    groups[-1]["parts"].append(cl)
                else:
                    pending.append(cl)
                continue
            if groups:
                g = groups[-1]
                if act == g["action"] or (strength == "weak" and act == "mix") or \
                        (strength == "weak" and act == "incubate" and g["action"] in _INCUBATE_INTO) or \
                        (act in _QC and g["action"] in _QC):
                    g["parts"].append(cl)
                    continue
            groups.append({"action": act, "strength": strength, "parts": pending + [cl]})
            pending = []
    if pending:
        if groups:
            groups[-1]["parts"] += pending
        elif mode != "prose":
            groups.append({"action": "other", "strength": "none", "parts": pending})
    for g in groups:
        raw = " ".join(p.strip() for p in g["parts"])
        raw = re.sub(r"\s+([,;.])", r"\1", re.sub(r"\s+", " ", raw)).strip(" ,;")
        raw = re.sub(r"^(?:[,;]\s*)?(?:and then|and|then|followed by)\s+", "", raw, flags=re.I)
        g["text"] = (raw[:1].upper() + raw[1:]).rstrip(" ,;") + ("" if raw.endswith((".", "!", "?")) else ".")
    return groups


_NUMBERED = re.compile(r"^\s*(?:step\s*)?(\d{1,3})\s*[.):\]]\s+(\S.*)$", re.I)
_BULLET = re.compile(r"^\s*[-*•·]\s+(\S.*)$")
_MD_HEAD = re.compile(r"^\s*#{1,6}\s+(.+?)\s*#*\s*$")
_NUM_HEAD = re.compile(r"^\s*(\d+(?:\.\d+)+\.?|\d+\.)\s+([A-Z][^.]{0,70})$")
_SKIP_SECTIONS = re.compile(r"material|reagent|equipment|note|reference|safety|abbreviation|suppl|consumable|tip|"
                            r"troubleshoot|recipe|stock|setup|set-up|before you begin|background|abstract", re.I)


def _heading(line: str, prev_blank: bool) -> str | None:
    s = line.strip()
    m = _MD_HEAD.match(s)
    if m:
        return m.group(1).strip()
    m = _NUM_HEAD.match(s)
    if m and len(s.split()) <= 9:
        return s
    if prev_blank and s and len(s) <= 60 and s[0].isupper() and not s.endswith((".", ";", ",")) and \
            len(s.split()) <= 8 and not s.startswith("|") and not re.search(r":\s+\S", s):
        return s
    return None


def _scan(text: str) -> dict:
    """Numbered items, bullets, table rows and prose paragraphs, each with the section it sits in."""
    from autochip.ingest import PAPER_HEADINGS, heading_of
    numbered, bullets, rows, paras = [], [], [], []
    section = None
    cur = None          # the list item being continued
    para: list[str] = []
    table: list[list[str]] = []
    prev_blank = True
    seen_heading = False

    def flush_para():
        nonlocal para
        if para:
            paras.append({"section": section, "text": " ".join(x.strip() for x in para)})
        para = []

    def flush_table():
        nonlocal table
        if len(table) >= 2:
            head = [h.strip().lower() for h in table[0]]
            for i, r in enumerate(table[1:], 1):
                rows.append({"section": section, "header": head, "cells": r, "row": i})
        table = []

    for raw in text.split("\n"):
        line = raw.rstrip()
        s = line.strip()
        if not s:
            flush_para()
            cur = None
            prev_blank = True
            continue
        if s.startswith("[") and s.endswith("]"):
            continue
        if s.startswith("|"):
            flush_para()
            cur = None
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c) or not c for c in cells):
                table.append(cells)
            prev_blank = False
            continue
        flush_table()
        m = _NUMBERED.match(line)
        if m and not (heading_of(m.group(2)) in PAPER_HEADINGS):
            flush_para()
            cur = {"section": section, "num": int(m.group(1)), "text": m.group(2).strip()}
            numbered.append(cur)
            prev_blank = False
            continue
        b = _BULLET.match(line)
        if b:
            flush_para()
            if cur is not None and cur in numbered and len(line) - len(line.lstrip()) >= 2:
                cur["text"] += " " + b.group(1).strip()
            else:
                cur = {"section": section, "text": b.group(1).strip()}
                bullets.append(cur)
            prev_blank = False
            continue
        h = _heading(line, prev_blank)
        if h and cur is None and not para:
            # the document title (a first "# " heading) is not a section
            if not (re.match(r"^\s*#\s", line) and not seen_heading):
                section = h
            seen_heading = True
            prev_blank = True        # a heading behaves like a paragraph break
            continue
        if cur is not None:
            cur["text"] += " " + s
        else:
            para.append(s)
        prev_blank = False
    flush_para()
    flush_table()
    return {"numbered": numbered, "bullets": bullets, "rows": rows, "paras": paras}


_TEXT_COLS = re.compile(r"step|description|action|procedure|instruction|operation|task|what|details|text|note", re.I)
_UNIT_IN_HEAD = re.compile(r"\(([^)]+)\)|\[([^\]]+)\]")


def _row_text(header: list[str], cells: list[str]) -> str:
    cols = list(range(len(cells)))
    text_cols = [j for j in cols if j < len(header) and _TEXT_COLS.search(header[j])
                 and not re.fullmatch(r"\s*\d+\s*", cells[j] or "")]
    if not text_cols:
        text_cols = [max(cols, key=lambda j: len(cells[j] or ""))] if cols else []
    main = " ".join(cells[j] for j in text_cols if cells[j])
    extra = []
    for j in cols:
        if j in text_cols or not cells[j] or (j < len(header) and re.fullmatch(r"(?:step|#|no\.?|n)", header[j] or "")):
            continue
        head = header[j] if j < len(header) else ""
        val = cells[j]
        um = _UNIT_IN_HEAD.search(head)
        if um and re.fullmatch(r"-?\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?", val):
            val = f"{val} {um.group(1) or um.group(2)}"
            head = _UNIT_IN_HEAD.sub("", head).strip()
        extra.append(f"{head}: {val}" if head else val)
    return main.rstrip(".") + "." + (f" ({'; '.join(extra)})" if extra else "")


def _items(source: dict) -> tuple[list[dict], str]:
    """The step candidates in the source and the mode they came from."""
    text = _scrub(source.get("text") or "")
    if source["kind"] == "prompt":
        return [{"ref": "prompt", "text": s} for s in _sentences(re.sub(r"\s+", " ", text))], "prompt"
    if source.get("methods_chars"):
        text = text[:source["methods_chars"]]
    sc = _scan(text)
    numbered = sc["numbered"]
    if len(numbered) >= 2:
        return [{"ref": (f"{it['section']}, step {it['num']}" if it["section"] else f"step {it['num']}"),
                 "text": it["text"]} for it in numbered], "list"
    bullets = [b for b in sc["bullets"] if not (b["section"] and _SKIP_SECTIONS.search(b["section"]))]
    if len(bullets) >= 2:
        return [{"ref": (f"{b['section']}, item {i}" if b["section"] else f"item {i}"), "text": b["text"]}
                for i, b in enumerate(bullets, 1)], "list"
    if len(sc["rows"]) >= 2:
        return [{"ref": (f"{r['section']}, table row {r['row']}" if r["section"] else f"table row {r['row']}"),
                 "text": _row_text(r["header"], r["cells"])} for r in sc["rows"]], "list"
    items = []
    for p in sc["paras"]:
        if p["section"] and _SKIP_SECTIONS.search(p["section"]) and not re.search(r"method|protocol|procedure", p["section"], re.I):
            continue
        for i, s in enumerate(_sentences(p["text"]), 1):
            items.append({"ref": (f"{p['section']}, sentence {i}" if p["section"] else f"sentence {i}"), "text": s})
    return items, "prose"


# ---------------------------------------------------------------------------------------- materials
_COMPARTMENTS = {"capsules", "droplets", "gel", "beads"}


def _start_material(text: str, action: str = "") -> str:
    t = text.lower()
    if action in ("culture", "seed", "feed", "dissociate", "quench", "encapsulate", "count", "wash_cells"):
        return "cells"
    if re.search(r"\bcapsules?\b", t):
        return "capsules"
    if re.search(r"\bdroplets?\b|\bemulsion\b", t):
        return "droplets"
    if re.search(r"\bcells?\b|cultur|tissue|organoid", t):
        return "cells"
    if re.search(r"\blibrary\b", t):
        return "library"
    if re.search(r"\bdna\b|\brna\b|cdna|nucleic|\bsample\b|amplicon", t):
        return "nucleic_acid"
    if re.search(r"\bbeads?\b", t):
        return "beads"
    return "liquid"


class _Tracker:
    """Follows the main material through the steps (cells -> droplets -> capsules -> nucleic acid -> library)."""

    def __init__(self, source_text: str):
        t = source_text.lower()
        self.capsules = bool(re.search(r"capsul", t))
        self.emulsion = bool(re.search(r"emulsion|droplet", t))
        self.library = False
        self.cur: str | None = None

    def _soluble(self) -> str:
        return "library" if self.library else "nucleic_acid"

    def step(self, action: str, text: str) -> tuple[str, str]:
        t = text.lower()
        cur = self.cur or _start_material(text, action)
        if action in ("amplify", "ligate", "fragment") and re.search(r"index|library|adapter|sequenc", t) or action == "ligate":
            self.library = True
        out = cur
        if action in ("culture", "seed", "feed", "dissociate"):
            out = "cells"
        elif action == "encapsulate":
            out = "droplets" if self.emulsion else "capsules"
        elif action == "emulsify":
            out = "droplets"
        elif action == "cure":
            out = "capsules" if (cur in ("droplets", "capsules") and self.capsules) else ("gel" if cur in ("liquid",) else cur)
        elif action == "break_emulsion":
            out = "capsules" if (self.capsules or cur == "capsules") else ("beads" if cur == "beads" else "liquid")
        elif action == "lyse":
            out = cur if cur in _COMPARTMENTS else "lysate"
        elif action in ("reverse_transcribe", "amplify", "fragment", "ligate"):
            out = cur if cur in _COMPARTMENTS else self._soluble()
        elif action == "release":
            out = self._soluble() if cur in _COMPARTMENTS else cur
        elif action == "bind_beads":
            out = "beads"
        elif action in ("elute", "purify"):
            out = self._soluble() if cur in _COMPARTMENTS | SOLUBLE else cur
        if action == "measure" and "library" in t and out in SOLUBLE:
            out = "library"
            self.library = True
        self.cur = out
        return cur, out


# ---------------------------------------------------------------------------------------- the extractor
def _citation(text: str) -> str | None:
    head = re.sub(r"\s+", " ", text[:6000])
    found = re.findall(r"((?:[A-Z][A-Za-z'\-]+(?: et al\.?| (?:&|and) [A-Z][A-Za-z'\-]+)),?\s+[^;\n()]{0,80}?(?:19|20)\d{2})", head)
    out = []
    for f in found:
        f = re.sub(r"\s+", " ", f).strip()
        if f not in out:
            out.append(f)
    return "; ".join(out[:4]) or None


def _goal(source: dict) -> str:
    if source["kind"] == "prompt":
        return re.sub(r"\s+", " ", source["text"]).strip()[:600]
    if source.get("prompt"):
        return source["prompt"].strip()[:600]
    return f"Run the protocol '{source.get('title') or 'from the source'}'."


def rules_extract(source: dict) -> dict:
    """Deterministic protocol IR from keyword rules (no agent)."""
    items, mode = _items(source)
    text_all = source.get("text") or ""
    tracker = _Tracker(text_all)
    fluorinated = bool(re.search(r"fluorinat|fluorous|hfe", text_all, re.I))
    prompt_mode = mode == "prompt"
    assumptions: list[str] = []
    steps: list[dict] = []
    used: list[str] = []
    sizes: dict[str, float] = {}
    defaulted: set[str] = set()

    raw_steps: list[dict] = []
    for k, it in enumerate(items, 1):
        groups = _groups(it["text"], mode)
        for j, g in enumerate(groups, 1):
            ref = it["ref"] if it["ref"] != "prompt" else "prompt"
            if len(groups) > 1 and ref != "prompt":
                ref += f" (part {j})"
            raw_steps.append({"action": g["action"], "text": g["text"], "ref": ref, "assumed": prompt_mode, "item": k})
            # "split ... for index PCR": the purpose is a step of its own
            m = re.search(r"\bfor\s+(?:\w+\s+){0,2}?(pcr|amplification|reverse transcription|lysis|ligation|"
                          r"fragmentation|tagmentation)\b", g["text"], re.I)
            if m and g["action"] in ("split", "collect", "transfer", "add_reagent", "pool", "other"):
                purpose = m.group(1).lower()
                act = {"pcr": "amplify", "amplification": "amplify", "reverse transcription": "reverse_transcribe",
                       "lysis": "lyse", "ligation": "ligate", "fragmentation": "fragment", "tagmentation": "fragment"}[purpose]
                phrase = m.group(0)[4:].strip()
                raw_steps.append({"action": act, "text": f"Run {phrase} on each portion (implied by '{m.group(0)}').",
                                  "ref": ref, "assumed": True, "item": k})
                assumptions.append(f"Added a {act} step after '{g['text'][:60]}' because the source says '{m.group(0)}'.")
            # prompt-only: a bead clean-up is written out as bind / wash on a magnet / elute
            if prompt_mode and g["action"] == "purify" and re.search(r"\bbeads?\b", g["text"], re.I):
                raw_steps.pop()
                raw_steps += [
                    {"action": "bind_beads", "text": "Bind the product to magnetic beads.", "ref": ref, "assumed": True, "item": k},
                    {"action": "wash", "text": "Wash the beads on a magnet.", "ref": ref, "assumed": True, "item": k},
                    {"action": "elute", "text": "Elute the clean product from the beads.", "ref": ref, "assumed": True, "item": k}]
                assumptions.append("The bead clean-up was written out as the standard bind / wash on a magnet / elute steps.")

    if not raw_steps:
        snippet = re.sub(r"\s+", " ", _scrub(text_all)).strip()[:300] or "No steps found."
        raw_steps.append({"action": "other", "text": snippet, "ref": "whole source", "assumed": True, "item": 1})
        assumptions.append("No steps could be found in the source by the keyword rules; the whole text is one placeholder step.")

    for n, rs in enumerate(raw_steps, 1):
        action = rs["action"]
        text = rs["text"]
        if action == "wash":
            cur = tracker.cur or _start_material(text)
            action = "wash_cells" if cur == "cells" else "wash_particles"
        if action == "cure" and _COLD_RX.search(text.lower()) and not _LIGHT_RX.search(text.lower()):
            action = "incubate"                      # a cold hold, not light / chemical crosslinking
        if action == "resuspend" and re.search(r"\bwash", text, re.I):
            action = "wash_cells" if (tracker.cur or "cells") == "cells" else "wash_particles"
        inp, out = tracker.step(action, text)
        q = _quantities(text, action)
        cond: dict = {}
        if q.get("light_nm"):
            cond["light_nm"] = q["light_nm"]
        if _MAGNET_RX.search(text) or action == "magnetic_separate":
            cond["magnet"] = True
        if re.search(r"\bshak\w*|\brotat(?:e|ing|or)\b|\borbital\b|\bagitat\w*|\brocking\b", text, re.I):
            cond["shaking"] = True
        if re.search(r"\bco2\b|co₂", text, re.I):
            cond["co2"] = True
        if q.get("cycles") and action == "amplify":
            cond["thermal_cycles"] = q["cycles"]
        if (q.get("temp_c") is not None and q["temp_c"] <= 8) or re.search(r"\bice\b|\bcold\b|\bchill", text, re.I):
            cond["cold"] = True
        reagents = _reagents_in(text, fluorinated)
        if action == "encapsulate":
            need = (["core_solution", "shell_polymer"] if tracker.capsules else []) + ["fluorinated_oil" if fluorinated else "oil"]
            if any(r not in reagents for r in need):
                reagents = reagents + [r for r in need if r not in reagents]
                defaulted.add(action)
        elif not reagents and action in _DEFAULT_REAGENTS:
            reagents = list(_DEFAULT_REAGENTS[action])
            defaulted.add(action)
        for r in reagents:
            if r not in used:
                used.append(r)
        if q.get("size_um") and out in ("droplets", "capsules"):
            sizes.setdefault(out, q["size_um"])
            sizes.setdefault("capsules", q["size_um"])
        note = "; ".join(q["notes"])
        step = {"n": n, "text": text, "action": action, "input_material": inp, "output_material": out,
                "reagents": reagents, "volume_ul": q.get("volume_ul"), "duration_s": q.get("duration_s"),
                "temp_c": q.get("temp_c"), "repeats": q.get("repeats"), "flow_ul_min": q.get("flow_ul_min"),
                "conditions": cond, "equipment": _equipment(action, text, q, cond),
                "on_chip_hint": _ON_CHIP.get(action, "possible"), "source_ref": rs["ref"], "assumed": rs["assumed"]}
        if action == "measure" and not re.search(r"concentration|size|yield|quantif|qc", text, re.I):
            step["on_chip_hint"] = "possible"
        if note:
            step["note"] = note
        steps.append(step)

    reagents = []
    for rid in used:
        role, extra = _ROLES.get(rid, ("reagent named in the source", {}))
        r = {"id": rid, "role": role}
        if extra.get("viscous"):
            r["viscous"] = True
        if extra.get("immiscible"):
            r["immiscible"] = True
        reagents.append(r)

    kinds = []
    for s in steps:
        for k in (s["input_material"], s["output_material"]):
            if k and k != "none" and k not in kinds:
                kinds.append(k)
    materials = [{"id": k, "kind": k, **({"size_um": sizes[k]} if k in sizes else {})} for k in kinds]

    if prompt_mode:
        assumptions.insert(0, "Prompt-only source: the steps were split from the goal by keyword rules (no agent); "
                              "every step is assumed and no volumes, times or temperatures are given.")
    if defaulted:
        assumptions.append("Generic reagent names were assumed for steps whose reagent the source does not name: "
                           + ", ".join(sorted(defaulted)) + ".")
    if mode == "prose":
        assumptions.append("Steps were taken sentence by sentence from the "
                           + ("Methods section" if source.get("methods_chars") else "text") + " by keyword rules.")
    title = source.get("title") or (Path(source["file"]).stem if source.get("file") else "protocol")
    ident = _slug(Path(source["file"]).stem) if source.get("file") else _slug(title)
    placeholder = prompt_mode or bool(re.search(r"placeholder|illustrat|typical|generic", text_all, re.I)) or \
        any(s["assumed"] for s in steps)
    return {
        "kind": "protocol_ir",
        "id": ident,
        "name": title,
        "goal": _goal(source),
        "source": {"kind": source["kind"], "file": source.get("file"), "title": source.get("title"),
                   "citation": _citation(text_all) if source["kind"] != "prompt" else None},
        "placeholder": placeholder,
        "assumptions": assumptions,
        "materials": materials,
        "reagents": reagents,
        "steps": steps,
    }

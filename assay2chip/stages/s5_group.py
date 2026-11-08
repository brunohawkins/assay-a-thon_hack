"""S5 group: ``partition.json`` -> ``chip/designs/<chip_id>.json``, one per chip.

    python assay2chip/stages/s5_group.py --id my_assay
    python assay2chip/stages/s5_group.py --unitops <unitops.json> --partition <partition.json> \
        --chip-id my_assay_c1 --stdout
    python assay2chip/stages/s5_group.py --id my_assay --coverage   # the step-by-step table

The output is a design file in the format that **already exists** in this repo
(``chip/designs/README.md``), so ``chip/components/build_chip.py`` stays usable on it
unchanged: modules in flow order, one library part each, the protocol steps each module
does, and its reagent inlets. S6 sizes parts from physics instead, but the design file is
the shared, reviewable statement of WHAT goes on the chip, and keeping it in the existing
format means the hand path and the generated path produce the same artefact.

TWO PART VOCABULARIES, AND WHICH ONE IS AUTHORITATIVE. S3 places an op on a **block id**
from ``contracts/blocks.json`` - the vocabulary every chip spec's ``block`` field already
uses, and the one S6 sizes parts from through ``assay2chip/parts/``. Yuvraj's design format
predates that: its ``part`` is a **Flowmatter name** from
``chip/components/catalogue.json``. So each module here carries BOTH:

  ``block``  the block id, authoritative, straight from S3. Never guessed, never dropped.
  ``part``   the Flowmatter name whose SHAPE corresponds to that block, from S3's
             ``placement.catalogue_part`` or from ``assay2chip/parts/flowmatter.py``'s
             mapping - and ``null`` when the block has no Flowmatter shape at all.

A block with no Flowmatter shape is not given an invented name. It is recorded as
``"part": null`` with ``why_no_part``, and the design says
``"build_chip_compatible": false``: ``chip/components/build_chip.py`` can only place a part
it has a body for, so it genuinely cannot represent such a module. That is a limit of the
compatibility path, not of the design - S6 reads ``block`` and sizes the part from physics.
Every block that has a shape still goes through Yuvraj's own ``check_design`` and
``layout``, so the compatibility claim is tested rather than asserted.

GROUPING (``chip/designs/README.md`` rules 1-6, applied in this order):

  1  One module per place the cells or capsules SIT. Everything done to the same material
     in the same part is one module, so a load, the washes after it, a hold and the
     collection all land in one chamber - the way `capsule_trap` in
     chip/designs/capsule_prep_v1.json covers three steps.
  2  A new module starts when the material moves (a `transfer`), when the part changes, or
     when another stream joins it (`merge`, `mix`, `encapsulate`, `split`, `pool`).
  6  `off_chip` and `missing_parts` are copied from S3's placement, with its reason.

EVERY ASSAY STEP IS ACCOUNTED FOR. A step that reaches no module, no `off_chip` entry and
no `missing_parts` entry is a silently dropped step, which is the failure this stage exists
to make impossible: it raises (``--allow-gaps`` writes the file anyway and records them).

The reagent inlets, and the generic names they are given, come from the same closed
vocabulary S8 uses (``assay2chip/protocolgen/reagents.py``), so a design file and the
protocol that runs on it name the same reagent the same way - and no concentration, vendor
or enzyme name from the source document can reach either (CONFIDENTIALITY.md).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts, llm  # noqa: E402
from assay2chip.lib.io import StageFileMissing, load_json, read_stage, write_json  # noqa: E402
from assay2chip.parts import flowmatter  # noqa: E402
from assay2chip.protocolgen import reagents  # noqa: E402

STAGE = "s5_group"
WRITES = ["chip/designs/{chip_id}.json"]

DESIGN_DIR = ROOT / "chip" / "designs"
CATALOGUE = ROOT / "chip" / "components" / "catalogue.json"

# Rule 2: these kinds mean the material has moved or another stream has joined it, so a
# new module starts. Everything else happens where the material already sits (rule 1).
NEW_PLACE_KINDS = ("merge", "mix", "encapsulate", "split", "pool", "transfer")

# Particle suspensions travel the chain (inlet_1 -> module -> ... -> outlet_1), so they are
# never a reagent inlet: an inlet is a line that feeds this module from outside.
CARRIED = ("capsule_suspension", "cell_suspension", "bead_suspension")

# What each module does, in our own words, by op kind. Never the source's sentence.
DOES = {
    "load": "takes in what is to be processed",
    "wash": "is flushed to clear its contents",
    "lyse": "receives the lysis reagent and holds",
    "incubate": "is held at temperature",
    "culture": "keeps the cells in culture with batch medium changes",
    "separate": "separates what is carried from the liquid",
    "thermocycle": "is cycled through a temperature profile",
    "mix": "mixes a reagent into the stream",
    "merge": "joins a reagent to the stream",
    "split": "shares its contents between the chambers downstream",
    "pool": "brings several streams together",
    "collect": "sends its contents out",
    "image": "is imaged",
    "encapsulate": "makes the capsules",
    "cure": "cures them in place",
    "transfer": "passes the material on",
}


class GroupError(RuntimeError):
    """The design cannot be written from what the earlier stages actually said."""


# --------------------------------------------------------------------------------------
# inputs
# --------------------------------------------------------------------------------------

def catalogue_parts() -> dict:
    """{part name: part} from Yuvraj's library, or {} when it has not been built yet."""
    if not CATALOGUE.exists():
        return {}
    return {p["part"]: p for p in load_json(CATALOGUE).get("parts", [])}


def block_to_flowmatter() -> dict:
    """{block id: Flowmatter part name}, from assay2chip/parts/flowmatter.py.

    That module owns the correspondence and says which names the catalogue itself grades
    as guesses; this stage only reads it. A block that maps to nothing is absent here, and
    absent means absent - no nearest-name substitution.
    """
    out = {}
    for part in flowmatter.catalogue():
        if part.block and part.block not in out:
            out[part.block] = part.part
    return out


def block_of(op: dict, log) -> str:
    """The block id S3 placed this op on. The authoritative identity of the part."""
    placement = op.get("placement") or {}
    library = contracts.library()
    named = placement.get("part")
    if named in library:
        return named
    for candidate in placement.get("blocks") or []:
        if candidate in library:
            log(f"op {op.get('op')}: placement.part {named!r} is not a block id, so the "
                f"block comes from placement.blocks ({candidate})")
            return candidate
    raise GroupError(
        f"op {op.get('op')}: placement.part {named!r} is not a block id in "
        f"contracts/blocks.json, and placement.blocks names none either. S3 places ops on "
        f"blocks (the vocabulary every chip spec's `block` field uses and the one S6 sizes "
        f"from), so either the id is wrong or the op is a missing_part."
    )


def flowmatter_part(block: str, op: dict, parts: dict, mapping: dict, log) -> tuple:
    """(Flowmatter part name or None, why it is None).

    From S3's `placement.catalogue_part` when it wrote one, else the block -> part map.
    Never a guess: a block with no Flowmatter shape stays None, and the design file says so.
    """
    stated = (op.get("placement") or {}).get("catalogue_part")
    if stated:
        if not parts or stated in parts:
            return stated, ""
        log(f"op {op.get('op')}: placement.catalogue_part {stated!r} is not in "
            f"chip/components/catalogue.json, so the block -> part map is used instead")
    name = mapping.get(block)
    if name and (not parts or name in parts):
        return name, ""
    if name:
        return None, (f"block '{block}' maps to Flowmatter part '{name}', which is not in the "
                      f"catalogue that is built here")
    return None, (f"no Flowmatter part in chip/components/catalogue.json has the shape of "
                  f"block '{block}' (assay2chip/parts/flowmatter.py); S6 sizes it from "
                  f"physics instead, so this module has a block but no catalogue shape")


def chip_steps(ops: list) -> list:
    """The assay steps THIS chip's ops cover, which is what its design must account for.

    Not the whole assay: with several chips, another chip's steps are not this design's to
    explain. The whole-assay accounting is `missing_from_every_chip`.
    """
    return sorted({int(s) for op in ops for s in op.get("from_steps", [])})


def assay_steps(assay: dict, ops: list = None) -> list:
    """Every step in the source assay, when assay.json is there; else what the ops cite."""
    if assay:
        return sorted({int(step["step"]) for step in assay.get("steps", [])})
    return chip_steps(ops or [])


def elsewhere_by_chip(unitops: dict, partition: dict) -> dict:
    """Off-chip and missing-part ops that no chip claims, given to a chip to record.

    S4 lists the ops a chip PERFORMS, so an op that happens on the bench usually appears on
    no chip at all - and then nobody would write it down. Each one is attached to the chip
    it falls after in process order, which is where a reader looks for it, so every op in
    unitops.json reaches exactly one design file.
    """
    chips = partition.get("chips", [])
    claimed = {int(n) for chip in chips for n in chip.get("ops", [])}
    out = {chip["chip_id"]: [] for chip in chips}
    if not chips:
        return out
    for op in unitops.get("ops", []):
        number = int(op["op"])
        if number in claimed or (op.get("placement") or {}).get("where") == "on_chip":
            continue
        owner = chips[0]["chip_id"]
        for chip in chips:
            if any(int(n) < number for n in chip.get("ops", [])):
                owner = chip["chip_id"]
        out[owner].append(op)
    return out


def missing_from_every_chip(designs: list, steps: list) -> list:
    """Assay steps that reached no design at all: an S2/S3/S4 gap, not a grouping one."""
    seen = set()
    for design in designs:
        seen |= {int(s) for module in design.get("modules", []) for s in module.get("steps", [])}
        for key in ("off_chip", "missing_parts"):
            seen |= {int(s) for item in design.get(key, []) for s in item.get("steps", [])}
    return [s for s in steps if s not in seen]


def chip_entry(partition: dict, chip_id: str) -> dict:
    for entry in partition.get("chips", []):
        if entry.get("chip_id") == chip_id:
            return entry
    raise GroupError(
        f"partition.json has no chip '{chip_id}' (it has: "
        f"{', '.join(c.get('chip_id', '?') for c in partition.get('chips', []))})"
    )


def ops_for(unitops: dict, entry: dict) -> list:
    by_number = {int(op["op"]): op for op in unitops.get("ops", [])}
    missing = [n for n in entry.get("ops", []) if int(n) not in by_number]
    if missing:
        raise GroupError(
            f"partition.json puts ops {missing} on chip {entry['chip_id']}, and unitops.json "
            f"has no such op: S4 has to be re-run against this unitops.json"
        )
    return [by_number[int(n)] for n in entry.get("ops", [])]


# --------------------------------------------------------------------------------------
# grouping
# --------------------------------------------------------------------------------------

def module_id(block: str, used: dict) -> str:
    """culture_chamber -> culture_1, culture_2 ...; t_junction -> t_junction_1."""
    base = block[:-len("_chamber")] if block.endswith("_chamber") else block
    used[base] = used.get(base, 0) + 1
    return f"{base}_{used[base]}"


def starts_new_module(op: dict, current: dict, block: str) -> bool:
    kind = str(op.get("kind", ""))
    if current is None or current["block"] != block:
        return True
    return kind in NEW_PLACE_KINDS


def inlets_for(ops: list) -> list:
    """The reagent lines this module needs, in order, with no duplicates."""
    out = []
    for index, op in enumerate(ops):
        following = ops[index + 1] if index + 1 < len(ops) else {}
        name, _ = reagents.name_for(
            op, chamber_is_empty=index == 0 and str(op.get("kind")) in ("load", "merge"),
            next_kind=str(following.get("kind", "")))
        if name in CARRIED or name in out:
            continue
        out.append(name)
    return out


def mesh_pore_um(ops: list) -> float:
    """A retention opening, when this module has to hold particles while liquid leaves.

    From the contract: trap.gap_factor_low x the particle diameter, which is the lower end
    of the agreed 0.3-0.4 x range (check_chip_spec.py errors at or above 0.4). S6 sizes the
    real feature; this is the design file's statement that one is needed at all.
    """
    holds = any(str(op.get("kind")) in ("wash", "collect", "separate", "lyse") for op in ops)
    diameters = [float((op.get("particles") or {}).get("diameter_um") or 0)
                 for op in ops if (op.get("particles") or {}).get("kind") in ("capsules", "beads")]
    if not holds or not any(diameters):
        return 0.0
    return round(max(diameters) * float(contracts.param("trap.gap_factor_low")), 1)


def does_sentence(block: str, ops: list) -> str:
    """One sentence, in our words, built from the op kinds. Never the source's prose."""
    actions = []
    for op in ops:
        text = DOES.get(str(op.get("kind")), f"performs {op.get('kind')}")
        if text not in actions:
            actions.append(text)
    joined = actions[0] if len(actions) == 1 else ", ".join(actions[:-1]) + " and " + actions[-1]
    return f"The {block.replace('_', ' ')} {joined}."


def safe(text: str, where: str, log) -> str:
    """Prose from an earlier stage, screened before it lands in a committed file."""
    problems = reagents.screen_text(text or "", where)
    if not problems:
        return (text or "").strip()
    log(f"replaced {where}: {problems[0]}")
    return ("Reason withheld: the sentence S3 wrote for this would have put a "
            "concentration, vendor or composition into a committed file "
            "(CONFIDENTIALITY.md); see the work directory.")


def elsewhere(ops: list, where: str, log) -> list:
    """`off_chip` or `missing_parts` entries, merging ops that give the same reason."""
    out = []
    for op in ops:
        placement = op.get("placement") or {}
        if placement.get("where") != where:
            continue
        reason = safe(placement.get("why", ""), f"op {op['op']} placement.why", log)
        need = safe(placement.get("need", ""), f"op {op['op']} placement.need", log)
        steps = sorted(int(s) for s in op.get("from_steps", []))
        for entry in out:
            if entry["why"] == reason and entry.get("need", "") == need:
                entry["steps"] = sorted(set(entry["steps"]) | set(steps))
                break
        else:
            entry = {"steps": steps, "why": reason}
            if where == "missing_part":
                entry = {"steps": steps, "need": need or "a part no block in the library covers",
                         "why": reason}
            out.append(entry)
    return out


def group(entry: dict, ops: list, unitops: dict, assay: dict = None, parts: dict = None,
          log=None) -> dict:
    """One design file for one chip of the partition."""
    log = log or (lambda message: None)
    parts = parts if parts is not None else catalogue_parts()
    mapping = block_to_flowmatter()
    chip_id = entry["chip_id"]
    on_chip = [op for op in ops if (op.get("placement") or {}).get("where") == "on_chip"]

    modules, used, current = [], {}, None
    for op in on_chip:
        block = block_of(op, log)
        if starts_new_module(op, current, block):
            current = {"id": module_id(block, used), "block": block, "ops": [op]}
            modules.append(current)
        else:
            current["ops"].append(op)

    out_modules, shapeless = [], []
    for module in modules:
        steps = sorted({int(s) for op in module["ops"] for s in op.get("from_steps", [])})
        part, why = flowmatter_part(module["block"], module["ops"][0], parts, mapping, log)
        built = {"id": module["id"], "block": module["block"], "part": part, "steps": steps,
                 "inlets": inlets_for(module["ops"]),
                 "does": does_sentence(module["block"], module["ops"])}
        if part is None:
            built["why_no_part"] = why
            shapeless.append(module["id"])
            log(f"{module['id']}: {why}")
        pore = mesh_pore_um(module["ops"])
        if pore:
            built["mesh_pore_um"] = pore
        out_modules.append(built)

    design = {
        "id": chip_id,
        "name": f"{unitops.get('id', chip_id)} on {chip_id}",
        "source": source_line(unitops),
        "placeholder": True,
        "description": (
            f"Generated by assay2chip S5 ({STAGE}) from the unit operations of "
            f"'{unitops.get('id', '?')}' and the chip plan in partition.json: "
            f"{entry.get('reason', '')} Built to the {entry.get('scale', 'demo')} envelope "
            f"({_envelope(entry.get('scale', 'demo'))}). Modules are in flow order, one "
            f"library part each, in the format chip/designs/README.md defines, so "
            f"chip/components/build_chip.py runs on this file unchanged."
        ),
        "modules": out_modules,
        "off_chip": elsewhere(ops, "off_chip", log),
        "missing_parts": elsewhere(ops, "missing_part", log),
        "build_chip_compatible": not shapeless,
    }
    if shapeless:
        design["note"] = (
            f"chip/components/build_chip.py cannot build this design: module(s) "
            f"{', '.join(shapeless)} sit on blocks that no Flowmatter part in "
            f"chip/components/catalogue.json has a shape for, and that builder places a "
            f"library STL per module. Each module's `block` is the authoritative identity "
            f"and S6 sizes the part from physics through assay2chip/parts/, so the chip is "
            f"built from this design either way; what is lost is the Flowmatter display "
            f"model. No part name has been invented to paper over it."
        )

    budget = contracts.max_ports_per_chip(entry.get("scale", "demo"))
    ports = sum(len(m["inlets"]) for m in out_modules) + 2
    if budget is not None and ports > budget:
        log(f"{chip_id} needs about {ports} ports (every inlet plus inlet_1 and outlet_1) and "
            f"the {entry.get('scale', 'demo')} limit is {budget}: reagent lines have to share "
            f"an off-chip selector valve, or S4 has to cut the assay again")
        design["note"] = (
            f"About {ports} ports are needed (one per inlet, plus inlet_1 and outlet_1) "
            f"against a limit of {budget} at this scale (contracts/blocks.json "
            f"fabrication_floors.max_ports_per_chip). Reagent lines that are never used at "
            f"the same time have to share one port through an off-chip selector valve, as "
            f"chip/module3_v1_translated.json does, or S4 has to cut the assay again."
        )
    return design


def _envelope(scale: str) -> str:
    env = contracts.envelope(scale)
    return f"{env.x_mm:g} x {env.y_mm:g} mm, {env.depth_mm:g} mm deep, {env.layers} layer"


def source_line(unitops: dict) -> str:
    source = unitops.get("source") or {}
    ref, kind = str(source.get("ref", "")).strip(), str(source.get("kind", "")).strip()
    if not ref:
        return "No source document recorded by S0/S1; see the assay2chip work directory."
    sha = str(source.get("sha256") or "")[:12]
    return f"{kind}: {ref}" + (f" (sha256 {sha})" if sha else "")


# --------------------------------------------------------------------------------------
# accounting
# --------------------------------------------------------------------------------------

def coverage(design: dict, steps: list) -> list:
    """One line per assay step saying where it went. Same shape build_chip.py prints."""
    where = {}
    for module in design.get("modules", []):
        # The block id is the identity; the Flowmatter name is shown only when there is one.
        label = f"{module['id']} ({module.get('block')}"
        label += f", {module['part']})" if module.get("part") else ", no Flowmatter shape)"
        for step in module.get("steps", []):
            where.setdefault(int(step), []).append(label)
    for item in design.get("off_chip", []):
        for step in item.get("steps", []):
            where.setdefault(int(step), []).append("OFF CHIP")
    for item in design.get("missing_parts", []):
        for step in item.get("steps", []):
            where.setdefault(int(step), []).append(f"NO PART: needs {item.get('need', '?')}")
    lines = []
    for step in steps or sorted(where):
        lines.append(f"  step {step:>2}: {' + '.join(where.get(step, ['NOT ACCOUNTED FOR']))}")
    return lines


def unaccounted(design: dict, steps: list) -> list:
    """Assay steps that reached no module, no off_chip entry and no missing_parts entry."""
    seen = {int(s) for module in design.get("modules", []) for s in module.get("steps", [])}
    for key in ("off_chip", "missing_parts"):
        seen |= {int(s) for item in design.get(key, []) for s in item.get("steps", [])}
    return [s for s in steps if s not in seen]


def check_buildable(design: dict, parts: dict = None) -> list:
    """What chip/components/build_chip.py would refuse about this design.

    Calls Yuvraj's own `check_design` and `layout`, so this is his rule and not a second
    copy of it. Skipped - with nothing claimed - when the catalogue has not been built, or
    when a module sits on a block that has no Flowmatter shape: his builder places one
    library STL per module, so it cannot represent that module at all, and the design file
    says so in `build_chip_compatible` and `note` instead of pretending otherwise.
    """
    if parts is None:
        parts = catalogue_parts()
    if not parts or not design.get("build_chip_compatible", True):
        return []
    sys.path.insert(0, str(ROOT / "chip" / "components"))
    sys.path.insert(0, str(ROOT / "operator"))
    import build_chip  # noqa: E402

    problems = list(build_chip.check_design(design, parts))
    try:
        spec, _ = build_chip.layout(design, parts)
    except Exception as error:                       # a layout failure is a design failure
        return problems + [f"build_chip.layout() failed: {error}"]
    import schema_io  # noqa: E402

    problems += [f"chip spec {schema_io.path_str(e) or '(top level)'}: {schema_io.describe(e)}"
                 for e in schema_io.schema_errors("chip_spec", spec)]
    return problems


# --------------------------------------------------------------------------------------
# the stage
# --------------------------------------------------------------------------------------

def write(design: dict, out: Path = None) -> Path:
    return write_json(Path(out) if out else DESIGN_DIR / f"{design['id']}.json", design)


def polish(design: dict, llm_mode: str = "", log=None) -> dict:
    """Optionally let a model rewrite the `does` sentences. Deterministic by default.

    The model is given the module ids, the part names and the op KINDS only - never the
    source document - and anything it returns is screened and dropped unless it is clean.
    A stubbed call returns the deterministic sentences unchanged, which is what tests run.
    """
    log = log or (lambda message: None)
    if llm_mode:
        llm.configure(llm_mode)
    stub = {m["id"]: m["does"] for m in design["modules"]}
    prompt = (
        "Rewrite each module's one-sentence description of what it does on a microfluidic "
        "chip. Keep it factual, generic and under 25 words. Use no reagent names, no "
        "concentrations and no vendor names. Return JSON: {module_id: sentence}.\n"
        + json.dumps([{"id": m["id"], "part": m["part"], "does": m["does"]}
                      for m in design["modules"]], indent=2)
    )
    answer = llm.complete_json(prompt, task="s5_group_does", stub=stub)
    for module in design["modules"]:
        text = str((answer or {}).get(module["id"], "")).strip()
        if not text:
            continue
        problems = reagents.screen_text(text, f"module {module['id']} does")
        if problems:
            log(f"kept the deterministic sentence for {module['id']}: {problems[0]}")
            continue
        module["does"] = text
    return design


def run(assay_id: str = "", id: str = "", ctx=None, chip_id: str = "", llm_mode: str = "",
        allow_gaps: bool = False, write_files: bool = True, log=None, **_) -> dict:
    """Stage entry point: one design file per chip in partition.json."""
    assay_id = assay_id or id or (getattr(ctx, "id", "") if ctx else "")
    if not assay_id:
        raise GroupError("s5_group needs an assay id to read partition.json")
    say = log or (ctx.log if ctx else (lambda message: None))
    partition = read_stage(assay_id, "partition")
    unitops = read_stage(assay_id, "unitops")
    try:
        assay = read_stage(assay_id, "assay")
    except StageFileMissing:
        assay = {}
        say("no assay.json, so step accounting can only use the steps the ops cite")

    parts = catalogue_parts()
    if not parts:
        say("chip/components/catalogue.json is not built, so part names are unchecked: run "
            "python chip/components/build_library.py")
    written, designs = [], []
    wanted = [chip_id] if chip_id else [c["chip_id"] for c in partition.get("chips", [])]
    spare = elsewhere_by_chip(unitops, partition)
    for one in wanted:
        entry = chip_entry(partition, one)
        ops = ops_for(unitops, entry) + spare.get(one, [])
        design = polish(group(entry, ops, unitops, assay, parts, say), llm_mode, say)
        steps = chip_steps(ops)
        gaps = unaccounted(design, steps)
        if gaps and not allow_gaps:
            raise GroupError(
                f"chip {one}: step(s) {', '.join(str(s) for s in gaps)} are covered by an op "
                f"on this chip but reached no module, no off_chip entry and no "
                f"missing_parts entry, so the grouping dropped them:\n"
                + "\n".join(coverage(design, steps))
            )
        if gaps:
            design["note"] = _add_note(design, (
                f"NOT ACCOUNTED FOR: step(s) {', '.join(str(s) for s in gaps)} are covered by "
                f"an op on this chip but reached no module, no off_chip entry and no "
                f"missing_parts entry."))
        problems = check_buildable(design, parts)
        if problems:
            raise GroupError(
                f"chip {one}: chip/components/build_chip.py would refuse this design:\n  - "
                + "\n  - ".join(problems)
            )
        say(f"{one}: {len(design['modules'])} module(s), "
            f"{len(design['off_chip'])} off chip, {len(design['missing_parts'])} missing part(s), "
            f"{len(gaps)} step(s) unaccounted for"
            + ("" if design.get("build_chip_compatible", True)
               else "; build_chip.py cannot place every module (see note)"))
        designs.append({"chip_id": one, "design": design, "steps": steps, "gaps": gaps})

    # Across every chip: a step that reached no design at all never became a unit op, which
    # is an S2/S3/S4 gap rather than a grouping one. Only checkable with the whole partition.
    everywhere = []
    if assay and not chip_id:
        everywhere = missing_from_every_chip([d["design"] for d in designs],
                                             assay_steps(assay))
        if everywhere and not allow_gaps:
            raise GroupError(
                f"assay step(s) {', '.join(str(s) for s in everywhere)} reached no chip's "
                f"design at all: no unit operation covers them, so S2/S3 dropped them before "
                f"S5 saw them. Re-run those stages, or pass --allow-gaps to write the designs "
                f"with the gap recorded."
            )
        if everywhere and designs:
            designs[0]["design"]["note"] = _add_note(designs[0]["design"], (
                f"NOT ACCOUNTED FOR ANYWHERE: assay step(s) "
                f"{', '.join(str(s) for s in everywhere)} are covered by no unit operation on "
                f"any chip of this partition. They are missing from the pipeline, not from the "
                f"assay."))
    for entry in designs:
        if write_files:
            written.append(write(entry["design"]).relative_to(ROOT).as_posix())
    return {"written": written, "designs": designs, "unaccounted_anywhere": everywhere}


def _add_note(design: dict, text: str) -> str:
    return ((design.get("note", "") + " ") if design.get("note") else "") + text


def main(argv: list = None) -> int:
    parser = argparse.ArgumentParser(
        prog=STAGE, description="S5 group: partition.json -> chip/designs/<chip_id>.json")
    parser.add_argument("--id", help="work id: read assay2chip/work/<id>/")
    parser.add_argument("--chip-id", default="", help="only this chip of the partition")
    parser.add_argument("--unitops", help="unitops.json path, instead of a work directory")
    parser.add_argument("--partition", help="partition.json path, instead of a work directory")
    parser.add_argument("--assay", help="assay.json path, for the full step list")
    parser.add_argument("--out", help="write here instead of chip/designs/<chip_id>.json")
    parser.add_argument("--stdout", action="store_true", help="print the design, write nothing")
    parser.add_argument("--coverage", action="store_true", help="print the step-by-step table")
    parser.add_argument("--llm", default="", choices=["", "stub", "real"],
                        help="let a model rewrite the module sentences (default: deterministic)")
    parser.add_argument("--allow-gaps", action="store_true",
                        help="write the design even when a step is unaccounted for")
    args = parser.parse_args(argv)

    try:
        if args.unitops and args.partition:
            unitops = load_json(Path(args.unitops))
            partition = load_json(Path(args.partition))
            assay = load_json(Path(args.assay)) if args.assay else {}
            wanted = [args.chip_id] if args.chip_id else [c["chip_id"] for c in partition["chips"]]
            spare = elsewhere_by_chip(unitops, partition)
            for one in wanted:
                entry = chip_entry(partition, one)
                ops = ops_for(unitops, entry) + spare.get(one, [])
                design = polish(group(entry, ops, unitops, assay, log=_printer),
                                args.llm, _printer)
                steps = chip_steps(ops)
                gaps = unaccounted(design, steps)
                problems = check_buildable(design)
                for line in problems:
                    print(f"PROBLEM  {line}", file=sys.stderr)
                if args.coverage:
                    print("\n".join(coverage(design, steps)))
                if gaps and not args.allow_gaps:
                    raise GroupError(
                        f"chip {one}: assay step(s) {', '.join(str(s) for s in gaps)} are not "
                        f"accounted for anywhere (--allow-gaps to write anyway)")
                if problems:
                    return 1
                if args.stdout:
                    print(json.dumps(design, indent=2, ensure_ascii=False))
                else:
                    print(f"{STAGE}: wrote {write(design, Path(args.out) if args.out else None)}")
            return 0

        if not args.id:
            parser.error("give --id, or --unitops with --partition")
        out = run(assay_id=args.id, chip_id=args.chip_id, llm_mode=args.llm,
                  allow_gaps=args.allow_gaps, write_files=not args.stdout, log=_printer)
        for entry in out["designs"]:
            if args.coverage:
                print("\n".join(coverage(entry["design"], entry["steps"])))
            if args.stdout:
                print(json.dumps(entry["design"], indent=2, ensure_ascii=False))
        for path in out["written"]:
            print(f"{STAGE}: wrote {path}")
        return 0
    except GroupError as error:
        print(f"{STAGE}: {error}", file=sys.stderr)
        return 1


def _printer(message: str) -> None:
    print(f"  {message}")


if __name__ == "__main__":
    raise SystemExit(main())

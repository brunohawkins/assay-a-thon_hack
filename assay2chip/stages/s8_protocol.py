"""S8 protocol: a built and routed chip plus its unit ops -> ``protocols/<chip_id>.json``.

    python assay2chip/stages/s8_protocol.py --id my_assay
    python assay2chip/stages/s8_protocol.py --chip chip/module3_v1_translated.json \
        --unitops assay2chip/protocolgen/fixtures/module3_v1_translated_unitops.json --stdout
    python assay2chip/stages/s8_protocol.py --audit protocols/module3_v1.json \
        --chip chip/module3_v1_translated.json          # check anybody's protocol
    python assay2chip/stages/s8_protocol.py --id my_assay --report

The output is a protocol in the same contract the hand-written ones use
(``contracts/schemas/protocol.schema.json``), generated the way
``protocols/build_module3.py`` and ``protocols/build_cell_prep.py`` generate theirs:
nothing numeric about liquid is typed in. Every volume is a number of chamber volumes of
a chamber in the chip spec, every flow is derived from that chamber's real cross-section
and the ceiling scoped to what it holds, and every duration is volume x washes / flow plus
a stated margin. Rescale the chip, re-run this, and the protocol follows.

Each of these was a defect an audit of the hand-written protocols here actually found, so
each one is checked on the way out and the file is NOT written if any of them fails
(``assay2chip/protocolgen/audit.py``):

  - every step's target exists on the chip, and the numbering is contiguous from 1
  - a load fills: volume <= the chamber's volume_ul
  - every exchange declares ``exchange_kind``; a sweep reaches ``exchange.target_fraction``,
    a partial states its fraction, a fill is bounded above by the chamber volume
  - flows come from the correct scoped ceiling for what that chamber holds, on its real
    cross-section, at a stated fraction of the ceiling
  - duration = volume x washes / flow plus a stated margin
  - ports.feed / vent / cap on every flow step, with every port on the chip accounted for
  - every temperature step names a declared thermal zone that can hold it
  - generic reagent names only: no concentrations, vendors, enzymes or compositions ever
    reach a committed protocol, whatever the source document said (CONFIDENTIALITY.md)
  - deterministic: no timestamps, so the same inputs give byte-identical output

The unit-op kind to protocol operation mapping is S2's
(``assay2chip/stages/s2_normalise.py`` ``KIND_TO_PROTOCOL``, checked against both schema
enums by ``check_vocabulary()``); S8 only chooses between the alternates that mapping
already authorises. Which chamber an op runs in comes from ``chambers[].from_ops`` in the
chip spec, then an explicit ``--map``, then the chip's only usable chamber, then a match on
chamber type - and if that is still ambiguous this stage refuses rather than guess.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "operator") not in sys.path:
    sys.path.insert(0, str(ROOT / "operator"))

import schema_io  # noqa: E402

from assay2chip.lib import contracts  # noqa: E402
from assay2chip.lib.io import load_json, read_stage, write_json  # noqa: E402
from assay2chip.protocolgen import audit, build, exchange, flows, reagents  # noqa: E402
from assay2chip.protocolgen.geometry import Chip  # noqa: E402

STAGE = "s8_protocol"
WRITES = ["protocols/{chip_id}.json"]

CHIP_DIR = ROOT / "chip"
PROTOCOL_DIR = ROOT / "protocols"


class ProtocolError(RuntimeError):
    """S8 cannot write a protocol it would not pass itself."""


# --------------------------------------------------------------------------------------
# building
# --------------------------------------------------------------------------------------

def source_line(unitops: dict, chip_spec: dict) -> str:
    """The protocol's `source`: the citation, and whose numbers the geometry is.

    Screened like everything else, because the one place a source document's wording can
    legitimately sit in a stage file is also the one place it could leak from.
    """
    source = unitops.get("source") or {}
    ref, kind = str(source.get("ref", "")).strip(), str(source.get("kind", "")).strip()
    sha = str(source.get("sha256") or "")[:12]
    cited = f"{kind}: {ref}" if ref else "no source document recorded by S0/S1"
    if sha:
        cited += f" (sha256 {sha})"
    return (f"Assay from {cited}; grouped into unit operations by assay2chip S1-S4. Chip "
            f"geometry, volumes and flows are ours, from {chip_spec.get('id')}.")


def op_lookup(unitops: dict) -> dict:
    return {int(op["op"]): op for op in unitops.get("ops", [])}


def ops_for_chip(unitops: dict, partition: dict, chip_id: str) -> list:
    """The ops this chip performs, in process order, from partition.json."""
    by_number = op_lookup(unitops)
    for entry in (partition or {}).get("chips", []):
        if entry.get("chip_id") != chip_id:
            continue
        missing = [n for n in entry.get("ops", []) if int(n) not in by_number]
        if missing:
            raise ProtocolError(
                f"partition.json puts ops {missing} on chip {chip_id}, and unitops.json has "
                f"no such op: the two files disagree, so S4 has to be re-run"
            )
        return [by_number[int(n)] for n in entry.get("ops", [])]
    raise ProtocolError(
        f"partition.json has no chip '{chip_id}' "
        f"(it has: {', '.join(c.get('chip_id', '?') for c in (partition or {}).get('chips', []))})"
    )


def build_protocol(chip_spec: dict, unitops: dict, ops: list = None, mapping: dict = None,
                   assay_id: str = "") -> tuple:
    """(protocol, plan). Raises BuildError or ProtocolError rather than write a bad file."""
    chip = Chip(chip_spec)
    if ops is None:
        # Without a partition to say which ops are this chip's, the chip itself says: S6
        # records them (`ops_per_part`, `ops_realised_as_channels`, `ops`). Only when the
        # chip records nothing - a hand-made chip - is every op taken as its own.
        claimed = (set(build.declared_ops(chip)) | build.inline_ops(chip)
                   | {int(n) for n in build.generator_params(chip).get("ops") or []})
        ops = [op for op in unitops.get("ops", [])
               if not claimed or int(op["op"]) in claimed]
    ops = list(ops)
    on_chip = [op for op in ops if (op.get("placement") or {}).get("where") == "on_chip"]
    elsewhere = [op for op in ops if op not in on_chip]
    if not on_chip:
        raise ProtocolError(
            f"none of the {len(ops)} ops given for chip {chip_spec.get('id')} is placed "
            f"on_chip, so there is no protocol to write"
        )

    assignment = build.assign(chip, on_chip, mapping)
    if not [t for t in assignment.values() if t != build.AS_A_CHANNEL]:
        raise ProtocolError(
            f"every op on chip {chip_spec.get('id')} was realised as a channel rather than "
            f"as a part, so there is no chamber for a step to target: the chip is a conduit, "
            f"and whatever drives it belongs to the chip either side of it"
        )
    plan = build.Plan(chip, assay_id or unitops.get("id", ""))
    # Whoever built the protocol also knows which ops went where, so no caller has to
    # work the assignment out again - and they cannot disagree about it.
    plan.ops, plan.assignment = ops, assignment
    plan.classes = build.classes_for(on_chip, assignment)
    previous = None
    for index, op in enumerate(on_chip):
        target = assignment[int(op["op"])]
        following = on_chip[index + 1] if index + 1 < len(on_chip) else {}
        later = [assignment.get(int(o["op"])) for o in on_chip[index + 1:]]
        build.steps_for_op(plan, op, target, previous, str(following.get("kind", "")),
                           last_in_chamber=target not in later)
        if target != build.AS_A_CHANNEL:
            previous = target
    build.finish(plan)
    for op in elsewhere:
        placement = op.get("placement") or {}
        plan.gap(op=int(op["op"]), field="placement",
                 question=(f"op {op['op']} ({op.get('kind')}) is {placement.get('where')}, so "
                           f"this protocol has no step for it: {placement.get('why', '')}"))

    if not plan.steps:
        # Nothing the ops asked for could be written. The chip is still in the chain, so
        # it is driven as a pass-through and every reason is carried in the steps.
        chambers = [t for t in assignment.values() if t != build.AS_A_CHANNEL]
        if not chambers:
            raise ProtocolError(
                f"chip {chip_spec.get('id')} has no chamber any op runs in, so there is "
                f"nothing for a protocol to target"
            )
        build.passthrough(plan, chambers[0],
                          " ".join(gap.question + "." for gap in plan.gaps[:3]))

    chip_id = chip_spec["id"]
    kinds = []
    for op in on_chip:
        if op.get("kind") not in kinds:
            kinds.append(str(op.get("kind")))
    protocol = {
        "id": chip_id,
        "name": f"{', '.join(kinds)} on {chip_id}",
        "source": source_line(unitops, chip_spec),
        "placeholder": bool(chip_spec.get("placeholder") or unitops.get("placeholder")
                            or plan.gaps or plan.assumptions),
        "description": description(plan, chip_spec, unitops, kinds),
        "steps": plan.steps,
    }
    return protocol, plan


def description(plan: build.Plan, chip_spec: dict, unitops: dict, kinds: list) -> str:
    """How every number in this file was derived, and what is still missing."""
    total = sum(step["duration_s"] for step in plan.steps)
    sweep_cv = exchange.sweep_chamber_volumes()
    chambers = sorted({step.get("target") for step in plan.steps if step.get("target")}
                      | {t for step in plan.steps for t in step.get("targets") or []})
    text = [
        f"Runs on chip/{chip_spec['id']}.json: {', '.join(kinds)}, in "
        f"{len(plan.steps)} steps and about {total / 3600:.1f} h.",
        f"Generated by assay2chip S8 ({STAGE}) from the unit operations of "
        f"'{unitops.get('id', '?')}'; nothing numeric about liquid is typed into the "
        f"generator.",
        f"VOLUMES are multiples of the volume_ul of the chamber each step runs in "
        f"({', '.join(f'{c} {plan.chip.volume_ul(c):g} uL' for c in chambers if c in plan.chip.chambers)}), "
        f"and an exchange moves at most one chamber volume per wash.",
        f"EXCHANGES declare what they are for: a sweep moves "
        f"{sweep_cv:.2f} chamber volumes, which is -ln(1 - exchange.target_fraction) at "
        f"{exchange.target_fraction():.0%}, because a well-mixed chamber keeps exp(-V) of "
        f"the old liquid after V chamber volumes; a fill is bounded above by the chamber "
        f"volume; a partial states the fraction it replaces.",
        f"FLOWS are derived per chamber from tau = 6*mu*Q/(w*h^2) on the real cross-section "
        f"of the channel the liquid crosses - never a bounding box - at "
        f"{flows.SHEAR_HEADROOM:.0%} of whichever ceiling is scoped to what that chamber "
        f"holds, and every flow is also held under "
        f"capsule.escape_pressure_kpa x control.pressure_margin over the whole route. "
        f"Capsule handling has no wall-shear ceiling at all: pressure is its limit.",
        f"DURATIONS are volume x washes / flow plus {build.DURATION_MARGIN - 1:.0%} for pump "
        f"ramp, line dead volume and settling.",
        f"REAGENT NAMES are generic placeholders from a closed vocabulary and are not a "
        f"recipe; no concentration, vendor or enzyme name from the source reaches this "
        f"file (CONFIDENTIALITY.md).",
    ]
    if plan.assumptions:
        text.append(f"RESTS ON CONTRACT ASSUMPTIONS (not published numbers): "
                    f"{', '.join(sorted(plan.assumptions))}.")
    if plan.gaps:
        text.append("GAPS, carried from the ops rather than filled in: "
                    + "; ".join(gap.line() for gap in plan.gaps) + ".")
    return " ".join(text)


# --------------------------------------------------------------------------------------
# checking and writing
# --------------------------------------------------------------------------------------

def classes_from_ops(ops: list, assignment: dict) -> dict:
    """{chamber: shear_class}, the strictest per chamber. One ordering, in protocolgen."""
    return build.classes_for(ops, assignment)


def check(protocol: dict, chip_spec: dict, classes: dict = None) -> list:
    """Schema errors, confidentiality problems and audit findings, as readable lines."""
    problems = [f"{schema_io.path_str(e) or '(top level)'}: {schema_io.describe(e)}"
                for e in schema_io.schema_errors("protocol", protocol)]
    problems += reagents.screen(protocol)
    problems += audit.problems(protocol, chip_spec, classes)
    seen, unique = set(), []
    for line in problems:
        if line not in seen:
            seen.add(line)
            unique.append(line)
    return unique


def write(protocol: dict, out: Path = None) -> Path:
    """Write protocols/<id>.json atomically, through the foundation's writer."""
    path = Path(out) if out else PROTOCOL_DIR / f"{protocol['id']}.json"
    return write_json(path, protocol)


def report(protocol: dict, plan: build.Plan) -> str:
    """Volumes, flows, shear and pressure per step, for --report."""
    lines = [f"{protocol['id']}: {len(protocol['steps'])} steps, "
             f"{sum(s['duration_s'] for s in protocol['steps']) / 3600:.2f} h",
             f"{'step':>4} {'operation':<12} {'target':<16} {'kind':<8} {'uL':>8} {'x':>3} "
             f"{'uL/min':>8} {'dyn/cm2':>9} {'kPa':>6} {'s':>7}"]
    by_step = dict(plan.flows)
    for step in protocol["steps"]:
        flow = by_step.get(step["step"])
        target = step.get("target") or ", ".join(step.get("targets") or [])
        lines.append(
            f"{step['step']:>4} {step['operation']:<12} {target:<16} "
            f"{step.get('exchange_kind', '-'):<8} "
            f"{(step.get('volume_ul') or 0):>8.3f} {(step.get('washes') or 0):>3} "
            f"{(step.get('flow_ul_min') or 0):>8.3f} "
            f"{(flow.shear_dyn_cm2 if flow else 0):>9.4f} "
            f"{(flow.pressure_kpa if flow else 0):>6.2f} {step['duration_s']:>7}")
    limit = contracts.max_working_pressure_kpa()
    lines.append(f"pressure limit {limit:g} kPa "
                 f"(capsule.escape_pressure_kpa x control.pressure_margin); shear headroom "
                 f"{flows.SHEAR_HEADROOM:.0%}; duration margin "
                 f"{build.DURATION_MARGIN - 1:.0%}")
    for gap in plan.gaps:
        lines.append(f"GAP  {gap.line()}")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------
# the stage
# --------------------------------------------------------------------------------------

def chip_path(chip_id: str) -> Path:
    path = CHIP_DIR / f"{chip_id}.json"
    if not path.exists():
        raise ProtocolError(
            f"chip/{chip_id}.json is not there: S6 (build) and S7 (route) have to run before "
            f"S8 can derive flows from the geometry"
        )
    return path


def run(assay_id: str = "", id: str = "", ctx=None, force: bool = False, log=None,
        chip_id: str = "", mapping: dict = None, write_files: bool = True, **_) -> dict:
    """Stage entry point: one protocol per chip in partition.json."""
    assay_id = assay_id or id or (getattr(ctx, "id", "") if ctx else "")
    if not assay_id:
        raise ProtocolError("s8_protocol needs an assay id to read partition.json")
    partition = read_stage(assay_id, "partition")
    unitops = read_stage(assay_id, "unitops")
    say = log or (ctx.log if ctx else (lambda message: None))

    written, results, skipped = [], [], []
    wanted = [chip_id] if chip_id else [c["chip_id"] for c in partition.get("chips", [])]
    for one in wanted:
        spec = load_json(chip_path(one))
        ops = ops_for_chip(unitops, partition, one)
        try:
            protocol, plan = build_protocol(spec, unitops, ops, mapping, assay_id)
        except ProtocolError as error:
            # One chip with nothing steppable does not invalidate the others, and the
            # reason is upstream: a hold whose time the source never gave, or a chip that
            # is only a conduit. Said out loud, and carried into the result.
            skipped.append({"chip_id": one, "why": str(error)})
            say(f"no protocol for {one}: {error}")
            continue
        problems = check(protocol, spec, classes_from_ops(plan.ops, plan.assignment))
        if problems:
            raise ProtocolError(
                f"the protocol S8 built for {one} breaks its own rules, so it was not "
                f"written:\n  - " + "\n  - ".join(problems)
            )
        if write_files:
            written.append(str(write(protocol).relative_to(ROOT).as_posix()))
        for gap in plan.gaps:
            say(f"gap: {gap.line()}")
        say(f"{one}: {len(protocol['steps'])} steps, "
            f"{sum(s['duration_s'] for s in protocol['steps']) / 3600:.1f} h, "
            f"{len(plan.gaps)} gap(s)")
        results.append({"chip_id": one, "protocol": protocol, "plan": plan})
    if skipped and not results:
        raise ProtocolError(
            f"no chip of '{assay_id}' has a protocol:\n  - "
            + "\n  - ".join(f"{s['chip_id']}: {s['why']}" for s in skipped))
    return {"written": written, "protocols": results, "skipped": skipped}


def main(argv: list = None) -> int:
    parser = argparse.ArgumentParser(
        prog=STAGE, description="S8: built chip + unit ops -> protocols/<chip_id>.json")
    parser.add_argument("--id", help="work id: read assay2chip/work/<id>/{partition,unitops}.json")
    parser.add_argument("--chip-id", default="", help="only this chip of the partition")
    parser.add_argument("--chip", help="chip spec path (with --unitops, no work dir needed)")
    parser.add_argument("--unitops", help="unitops.json path, for developing against a chip")
    parser.add_argument("--map", action="append", default=[], metavar="OP=CHAMBER",
                        help="which chamber an op runs in, when the chip does not say")
    parser.add_argument("--out", help="write here instead of protocols/<chip_id>.json")
    parser.add_argument("--stdout", action="store_true", help="print the protocol, write nothing")
    parser.add_argument("--report", action="store_true",
                        help="volumes, flows, shear and pressure per step")
    parser.add_argument("--audit", metavar="PROTOCOL",
                        help="check an existing protocol against the same rules (needs --chip)")
    args = parser.parse_args(argv)

    mapping = {}
    for item in args.map:
        number, _, chamber = item.partition("=")
        if not chamber.strip():
            parser.error(f"--map wants OP=CHAMBER, not {item!r}")
        mapping[int(number)] = chamber.strip()

    try:
        if args.audit:
            if not args.chip:
                parser.error("--audit needs --chip, since every rule is about the geometry")
            protocol = load_json(Path(args.audit))
            spec = load_json(Path(args.chip))
            problems = check(protocol, spec)
            print(f"{Path(args.audit).name} on {spec['id']}: "
                  f"{len(protocol.get('steps') or [])} steps, {len(problems)} problem(s)")
            for line in problems:
                print(f"  - {line}")
            return 1 if problems else 0

        if args.chip and args.unitops:
            spec = load_json(Path(args.chip))
            unitops = load_json(Path(args.unitops))
            protocol, plan = build_protocol(spec, unitops, None, mapping)
            problems = check(protocol, spec, classes_from_ops(plan.ops, plan.assignment))
            if args.report:
                print(report(protocol, plan))
            for line in problems:
                print(f"PROBLEM  {line}", file=sys.stderr)
            if problems:
                return 1
            if args.stdout:
                print(_dumps(protocol))
            else:
                path = write(protocol, Path(args.out) if args.out else None)
                print(f"{STAGE}: wrote {path} ({len(protocol['steps'])} steps, "
                      f"{len(plan.gaps)} gap(s))")
            return 0

        if not args.id:
            parser.error("give --id, or --chip with --unitops, or --audit with --chip")
        out = run(assay_id=args.id, chip_id=args.chip_id, mapping=mapping,
                  write_files=not args.stdout, log=lambda m: print(f"  {m}"))
        for entry in out["protocols"]:
            if args.report:
                print(report(entry["protocol"], entry["plan"]))
            if args.stdout:
                print(_dumps(entry["protocol"]))
        for path in out["written"]:
            print(f"{STAGE}: wrote {path}")
        return 0
    except (ProtocolError, build.BuildError, exchange.ExchangeError, flows.FlowError,
            reagents.ReagentError) as error:
        print(f"{STAGE}: {error}", file=sys.stderr)
        return 1


def _dumps(data) -> str:
    import json

    return json.dumps(data, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    raise SystemExit(main())

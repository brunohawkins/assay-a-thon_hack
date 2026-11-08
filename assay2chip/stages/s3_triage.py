"""S3 triage: every unit operation gets a `placement`.

    python assay2chip/stages/s3_triage.py --id my_assay
    python assay2chip/stages/s3_triage.py --id my_assay --stdout      # decide, write nothing
    python assay2chip/stages/s3_triage.py --rules                     # print the rule table

Reads ``assay2chip/work/<id>/unitops.json`` (S2) and writes the same file back with

    "placement": {"where": "on_chip|off_chip|missing_part", "part": ..., "why": ...,
                  "need": ..., "blocks": [...], "confidence": 0.95, "note": ...}

on every op. Nothing else in the file changes except `unresolved`, which grows: a contract
default this stage had to fall back on, and every flag that is a real open question, is
recorded there rather than left implicit.

**It is a rule engine.** The decision for an op comes from the ordered table in
``assay2chip/triage/rules.py``; the geometry it rests on comes from
``assay2chip/triage/facts.py``, which is the only place a contract value enters S3; and the
block ids it is allowed to name are guarded by ``assay2chip/triage/library.py``. Same input,
same output - no clock, no randomness, no network. A language model is consulted in exactly
one place (``assay2chip/triage/tiebreak.py``) and only when two rules of equal rank disagree
about what a word means. Physics is never put to a model.

``--expect`` checks the result against a ground-truth file, which is how the decisions
already encoded in ``protocols/pipeline_mvp.json`` are kept honest:

    {"ops": {"5": ["on_chip", "trap_chamber"], "7": ["off_chip", null]}}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import io as stage_io  # noqa: E402
from assay2chip.lib import llm  # noqa: E402
from assay2chip.triage import engine, rules  # noqa: E402

#: Nothing outside the work directory. S3 rewrites the file S2 wrote.
WRITES = []

STAGE = "s3"


def run(assay_id="", ctx=None, llm_mode=None, force=False, write=True, unitops=None,
        expect=None, **_ignored):
    """Stage entry point for assay2chip/run.py.

    assay_id  the work directory name. Taken from `ctx` when run.py passes one.
    unitops   an in-memory payload instead of reading the file (used by tests).
    expect    {op number: (where, part or None)} ground truth; disagreements are returned
              in the result summary as `expected_disagreements` and make the CLI exit 1.
    """
    if ctx is not None and not assay_id:
        assay_id = getattr(ctx, "assay_id", None) or getattr(ctx, "id", None) or ""
    if llm_mode:
        llm.configure(llm_mode)
    llm.reset()

    payload = unitops if unitops is not None else stage_io.read_stage(assay_id, "unitops")
    result = engine.triage(payload)

    if expect:
        result.summary["expected_disagreements"] = engine.compare(result, expect)

    if write and unitops is None:
        _write(assay_id, result)
    return result


def _write(assay_id, result):
    """Write unitops.json back, schema-validated.

    If the payload S2 handed us already broke the schema, say so and do not silently
    rewrite it as if it were fine: the problems are listed, the ones S3 could have caused
    are separated out, and only a file whose remaining problems all came in with it is
    written unvalidated.
    """
    try:
        return stage_io.write_stage(assay_id, "unitops", result.unitops)
    except stage_io.StageValidationError as error:
        inherited = stage_io.problems("unitops", _without_placements(result.unitops))
        mine = [p for p in stage_io.problems("unitops", result.unitops) if p not in inherited]
        result.summary["schema_problems"] = {"inherited_from_s2": inherited, "added_by_s3": mine}
        if mine:
            raise
        print("s3_triage: unitops.json does not match its schema, and every problem came in "
              "from S2 rather than from this stage, so the placements were written anyway:",
              file=sys.stderr)
        for problem in inherited:
            print("  - %s" % problem, file=sys.stderr)
        print("  (%s)" % str(error).splitlines()[0], file=sys.stderr)
        return stage_io.write_json(stage_io.stage_path(assay_id, "unitops"), result.unitops)


def _without_placements(unitops):
    """The same payload with S3's own work replaced by a minimal valid placement.

    Anything the schema still complains about came in from S2, not from this stage.
    """
    copy = {k: v for k, v in unitops.items() if k != "ops"}
    copy["ops"] = [_neutral_placement(op) for op in unitops.get("ops", [])]
    return copy


def _neutral_placement(op):
    out = {k: v for k, v in op.items() if k != "placement"}
    out["placement"] = {"where": "off_chip", "why": "placeholder for the comparison only"}
    return out


def rule_table():
    """The rule table as text: rank, id, physics, and what it decides."""
    lines = ["%-5s %-28s %-9s %-8s %s" % ("rank", "rule", "evidence", "physics", "decides")]
    for r in sorted(rules.RULES, key=lambda r: (r.rank, r.id)):
        lines.append("%-5d %-28s %-9s %-8s %s"
                     % (r.rank, r.id, r.evidence, "yes" if r.physics else "no", r.about))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="s3_triage",
        description="S3: decide on_chip / off_chip / missing_part for every unit operation")
    parser.add_argument("--id", default="", help="assay id (the work directory name)")
    parser.add_argument("--in", dest="infile", help="read this unitops.json instead")
    parser.add_argument("--stdout", action="store_true",
                        help="print the triaged file, write nothing")
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    parser.add_argument("--rules", action="store_true", help="print the rule table and exit")
    parser.add_argument("--expect", help="ground-truth JSON: {\"ops\": {\"5\": [\"on_chip\", "
                                         "\"trap_chamber\"]}}")
    parser.add_argument("--llm", dest="llm_mode", choices=("stub", "real"),
                        help="model mode for the wording tie-break only (default stub)")
    args = parser.parse_args(argv)

    if args.rules:
        print(rule_table())
        return 0
    if not args.id and not args.infile:
        parser.error("give --id (or --in path/to/unitops.json)")

    expect = None
    if args.expect:
        raw = json.loads(Path(args.expect).read_text(encoding="utf-8"))
        expect = {int(k): tuple(v) for k, v in (raw.get("ops") or raw).items()}

    payload = None
    if args.infile:
        payload = json.loads(Path(args.infile).read_text(encoding="utf-8"))

    try:
        result = run(assay_id=args.id, unitops=payload, expect=expect,
                     llm_mode=args.llm_mode,
                     write=not args.stdout and payload is None)
    except stage_io.StageError as error:
        print("s3_triage: %s" % error, file=sys.stderr)
        return 2

    if args.stdout:
        json.dump(result.unitops, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    elif args.json:
        json.dump(result.summary, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
    else:
        print(result.report())

    disagreements = result.summary.get("expected_disagreements") or []
    if disagreements:
        print("", file=sys.stderr)
        print("GROUND TRUTH: %d disagreement(s)" % len(disagreements), file=sys.stderr)
        for line in disagreements:
            print("  - %s" % line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

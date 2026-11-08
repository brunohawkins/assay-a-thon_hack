"""Run the rule table over every unit operation and report what came out.

    from assay2chip.triage import engine

    result = engine.triage(unitops)
    result.unitops          # a NEW dict; `placement` set on every op
    result.summary          # counts, flags, the parts the library lacks, disagreements
    print(result.report())  # the same thing as text, for the CLI and the S12 report

Three promises:

  * every op gets a decision. S2 writes `placement.where: "pending"` as an explicit "no
    decision"; S3 replaces every one of them, and finishing with a single `pending` left
    raises `PendingLeft` rather than returning a half-triaged file. The table ends in a
    fallback that says `missing_part` rather than guessing a block;
  * every placement carries a `why` that names a contract key or a repo file;
  * the same input gives the same output - no clock, no randomness, no network.

What the engine adds on top of the rules: it records the contract defaults the facts had
to fall back on in `unresolved`, turns a rule's flags into entries in `unresolved` where
they are real open questions (the oxygen budget, the untested weir gap), and collects the
`need` strings into one list - the parts the library lacks.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts, llm  # noqa: E402

from . import rules as rules_mod  # noqa: E402
from . import tiebreak  # noqa: E402
from .facts import facts_for  # noqa: E402
from .rules import DECIDED, MISSING, OFF, ON  # noqa: E402

STAGE = "s3_triage"

#: An op this uncertain gets said so in its `why`: a placement is only ever as good as the
#: step it came from. Same threshold S2 uses to raise an `unresolved` entry.
LOW_CONFIDENCE = 0.5

#: A flag that is a real open question, and the question it raises in `unresolved`.
FLAG_QUESTIONS = {
    "weir_gap_untested": "no published capture efficiency for a weir retaining particles "
                         "above 100 um: run the 21/24/27 um coupon before committing "
                         "(contracts/blocks.json retention.weir_geometry.untested)",
    "oxygen_budget_unresolved": "oxygen and medium budget for on-chip culture: cell count x "
                                "consumption against perfusion rate is not computed anywhere "
                                "yet, and a sealed chamber goes anoxic in about 8 h",
    "needs_engineered_cell_line": "this step needs a labelled or reporter cell line and the "
                                  "project is wild-type iPSCs only: does the assay really "
                                  "require it, or is there a label-free substitute?",
    "live_cells_above_culture_temp": "the source puts live cells above culture.temp_c, which "
                                     "nothing can do: check the source, then cut the assay "
                                     "here (S4)",
    "shear_class_mis_scoped": "S2 applied the adherent-cell shear ceiling to a capsule op; "
                              "contracts/parameters.json scopes it to adherent cells only",
    "physics_tie_not_arbitrated": "two physics rules matched at the same rank and the tie was "
                                  "resolved by rule id: give them distinct ranks",
}


class PendingLeft(AssertionError):
    """S3 finished and an op still says `pending`. That is a failure, never a pass."""


def pending(unitops):
    """Op numbers still carrying `placement.where: pending` (or no placement at all).

    S2 writes `pending` on every op as an explicit "no decision". S3 must replace all of
    them, and S4 onwards must refuse a file that still holds one, so this is the check both
    sides use.
    """
    out = []
    for op in unitops.get("ops") or []:
        placement = op.get("placement")
        if not isinstance(placement, dict) or placement.get("where") not in DECIDED:
            out.append(op.get("op"))
    return out


@dataclass
class Result:
    unitops: dict
    summary: Dict[str, Any] = field(default_factory=dict)
    decisions: List[Dict[str, Any]] = field(default_factory=list)

    def where(self, op_number):
        """Where op `op_number` landed, for a quick assertion."""
        for op in self.unitops.get("ops", []):
            if op.get("op") == op_number:
                return (op["placement"]["where"], op["placement"].get("part"))
        raise KeyError("no op %r in this result" % (op_number,))

    def report(self):
        return report(self)


def _append_unresolved(unitops, entry):
    """Add an `unresolved` entry, skipping an exact duplicate."""
    bucket = unitops.setdefault("unresolved", [])
    if entry not in bucket:
        bucket.append(entry)


def triage(unitops, rule_table=None):
    """Attach `placement` to every op in a unitops payload. Returns a Result.

    `unitops` is not modified: the payload in the Result is a fresh structure, so a caller
    can diff the two or throw the result away.
    """
    table = sorted(rule_table or rules_mod.RULES, key=lambda r: (r.rank, r.id))
    problems = rules_mod.audit()
    if problems:
        raise ValueError(
            "the S3 rule table is not well formed, so no op was triaged:\n  - "
            + "\n  - ".join(problems))

    out = _copy(unitops)
    ops = out.get("ops") or []
    counts = {ON: 0, OFF: 0, MISSING: 0}
    by_rule = {}  # type: Dict[str, int]
    flags = {}  # type: Dict[str, List[int]]
    needs = []  # type: List[Dict[str, Any]]
    decisions = []  # type: List[Dict[str, Any]]
    tie_notes = []  # type: List[Dict[str, Any]]
    gaps = []  # type: List[Dict[str, Any]]
    llm_calls_before = _llm_calls()

    for op in ops:
        f = facts_for(op, gaps)
        matches = []
        for r in table:
            verdict = r.test(f)
            if verdict is not None:
                matches.append((r, verdict))
        if not matches:
            # The fallback rule makes this unreachable with the real table. If a caller
            # passes a table without one, the op keeps the `pending` S2 gave it and the
            # check at the end of this function refuses the whole file - which is the point
            # of `pending`: a missing decision stays visible instead of being invented.
            continue
        top = min(r.rank for r, _ in matches)
        chosen_rule, verdict, notes = tiebreak.resolve(
            f, [pair for pair in matches if pair[0].rank == top])
        tie_notes.extend(notes)

        if f.confidence is not None and float(f.confidence) < LOW_CONFIDENCE:
            verdict.why += (
                " - and this rests on a LOW-CONFIDENCE op (confidence %g): the placement is "
                "only as good as the step it came from, so treat it as provisional"
                % float(f.confidence))
            verdict.confidence = min(
                float(f.confidence),
                verdict.confidence if verdict.confidence is not None
                else rules_mod.CONFIDENCE[verdict.evidence])
            verdict.flags = list(verdict.flags) + ["low_confidence_op"]

        op["placement"] = verdict.placement()
        counts[verdict.where] = counts.get(verdict.where, 0) + 1
        by_rule[chosen_rule.id] = by_rule.get(chosen_rule.id, 0) + 1
        for flag in verdict.flags:
            flags.setdefault(flag, []).append(f.op)
            question = FLAG_QUESTIONS.get(flag)
            if question:
                _append_unresolved(out, {"op": f.op, "field": "placement",
                                         "question": question})
        if verdict.need:
            needs.append({"op": f.op, "where": verdict.where, "need": verdict.need,
                          "rule": chosen_rule.id})
        decisions.append({
            "op": f.op,
            "kind": f.kind,
            "particles": f.particles,
            "rule": chosen_rule.id,
            "rank": chosen_rule.rank,
            "where": verdict.where,
            "part": verdict.placement().get("part"),
            "evidence": verdict.evidence,
            "also_matched": sorted(r.id for r, _ in matches if r.id != chosen_rule.id),
        })

    for gap in gaps:
        _append_unresolved(out, {k: v for k, v in gap.items() if v is not None})

    left = pending(out)
    if left:
        raise PendingLeft(
            "S3 finished with %d op(s) still 'pending' (%s). S2 writes 'pending' as an "
            "explicit no-decision and S3 exists to replace every one of them, so this is a "
            "failure, not a partial pass."
            % (len(left), ", ".join(str(n) for n in left)))

    fired = set(by_rule)
    disagreements = [d for d in rules_mod.DISAGREEMENTS if d["rule"] in fired]

    if _llm_calls() > llm_calls_before:
        out["llm"] = llm.provenance(
            note="S3 called the model only to break a tie over wording "
                 "(assay2chip/triage/tiebreak.py); no physics was put to it")

    summary = {
        "stage": STAGE,
        "ops": len(ops),
        "counts": {"on_chip": counts.get(ON, 0), "off_chip": counts.get(OFF, 0),
                   "missing_part": counts.get(MISSING, 0)},
        "by_rule": dict(sorted(by_rule.items())),
        "pending_left": len(pending(out)),
        "parts_used": sorted({d["part"] for d in decisions if d["part"]}),
        "library_lacks": sorted({n["need"] for n in needs if n["where"] == MISSING}),
        "off_chip_kits": sorted({n["need"] for n in needs if n["where"] == OFF}),
        "needs": needs,
        "flags": {k: sorted(v) for k, v in sorted(flags.items())},
        "tiebreaks": tie_notes,
        "disagreements": disagreements,
        "unresolved": len(out.get("unresolved") or []),
        "contracts": contracts.fingerprint(),
    }
    return Result(out, summary, decisions)


def _llm_calls():
    try:
        return llm.provenance().get("calls", 0)
    except Exception:  # pragma: no cover - provenance is pure, but never block triage on it
        return 0


def _copy(value):
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


def compare(result, expected):
    """Ground truth check: `expected` is {op number: (where, part or None)}.

    Returns a list of readable disagreements, empty when triage reproduced every one. Used
    by tests/test_assay2chip_triage.py against the decisions protocols/pipeline_mvp.json
    already encodes, and by `--expect` on the stage CLI.
    """
    out = []
    for number in sorted(expected):
        want_where, want_part = expected[number]
        try:
            got_where, got_part = result.where(number)
        except KeyError:
            out.append("op %d: expected %s but there is no such op" % (number, want_where))
            continue
        if got_where != want_where or (want_part is not None and got_part != want_part):
            decision = next((d for d in result.decisions if d["op"] == number), {})
            out.append(
                "op %d (%s): expected %s%s, triage said %s%s by rule %s"
                % (number, decision.get("kind", "?"), want_where,
                   " on " + want_part if want_part else "",
                   got_where, " on " + got_part if got_part else "",
                   decision.get("rule", "?")))
    return out


def report(result):
    """The summary as text. Printed by the stage and reused by the S12 report."""
    s = result.summary
    lines = [
        "S3 triage: %d op(s)" % s["ops"],
        "  on_chip      %d" % s["counts"]["on_chip"],
        "  off_chip     %d" % s["counts"]["off_chip"],
        "  missing_part %d" % s["counts"]["missing_part"],
        "  pending      %d  (S2 writes pending; S3 must leave none)" % s["pending_left"],
    ]
    if s["parts_used"]:
        lines.append("  blocks used: " + ", ".join(s["parts_used"]))
    lines.append("")
    lines.append("decisions")
    for d in result.decisions:
        lines.append("  op %-3d %-12s %-9s -> %-12s %-18s %s"
                     % (d["op"], d["kind"], d["particles"], d["where"],
                        d["part"] or "-", d["rule"]))
    if s["library_lacks"]:
        lines.append("")
        lines.append("PARTS THE LIBRARY LACKS (%d)" % len(s["library_lacks"]))
        for need in s["library_lacks"]:
            lines.append("  - " + need)
    if s["off_chip_kits"]:
        lines.append("")
        lines.append("BOUGHT / OFF-CHIP EQUIPMENT (%d)" % len(s["off_chip_kits"]))
        for need in s["off_chip_kits"]:
            lines.append("  - " + need)
    if s["flags"]:
        lines.append("")
        lines.append("FLAGS")
        for flag, where in s["flags"].items():
            lines.append("  %-38s ops %s" % (flag, ", ".join(str(n) for n in where)))
    if s["tiebreaks"]:
        lines.append("")
        lines.append("TIE-BREAKS")
        for note in s["tiebreaks"]:
            lines.append("  op %s: chose %s over %s (%s)"
                         % (note["op"], note["chose"], note["over"], note["model"]))
    if s["disagreements"]:
        lines.append("")
        lines.append("DELIBERATE DISAGREEMENTS WITH THE REPO")
        for d in s["disagreements"]:
            lines.append("  %s" % d["rule"])
            lines.append("    repo:   %s" % d["repo"])
            lines.append("    triage: %s" % d["triage"])
            lines.append("    wrong:  %s" % d["who_is_wrong"])
    lines.append("")
    lines.append("unresolved entries carried forward: %d" % s["unresolved"])
    return "\n".join(lines)

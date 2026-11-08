"""The one place S3 is allowed to ask a language model anything.

It is allowed for exactly one job: two rules of the SAME rank both matched, both of them
decided on the meaning of a word rather than on a number, and something has to choose.
That is a question about English.

It is never allowed for physics. If any tied rule declares `physics=True` the model is not
consulted at all: the tie is resolved deterministically (lowest rule id) and the op is
flagged `physics_tie_not_arbitrated` so a human sees it. A model cannot be allowed to
decide whether a 25 um gap holds a 70 um capsule, and this module is where that line is
drawn and enforced.

The model path runs through assay2chip/lib/llm.py, so it is stubbed offline by default and
whichever mode ran is recorded in the file's `llm` block. The stub returns the same answer
as the deterministic path, which means tests - and any offline run - are reproducible.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import llm  # noqa: E402

TASK = "s3_triage_tiebreak"

SYSTEM = (
    "You are breaking a tie between two microfluidic triage rules that both matched one "
    "unit operation. Decide ONLY which reading of the wording is right. Do not reason "
    "about fluid mechanics, fabrication limits or temperatures: those are already decided "
    "and are not yours to change. Answer with JSON: {\"rule\": \"<rule id>\", \"why\": "
    "\"<one short sentence about the wording>\"}."
)


def deterministic_choice(candidates):
    """Lowest rule id wins. Stable, explainable and the same on every machine."""
    return sorted(candidates, key=lambda pair: pair[0].id)[0]


def _prompt(f, candidates):
    lines = [
        "A unit operation matched %d rules of equal rank." % len(candidates),
        "",
        "Operation %d: kind=%s particles=%s diameter_um=%s temp_c=%s duration_s=%s"
        % (f.op, f.kind, f.particles, f.diameter_um, f.temp_c, f.duration_s),
        "Wording attached to it: %s" % (f.text or "(none)"),
        "",
        "Candidate readings:",
    ]
    for r, verdict in sorted(candidates, key=lambda pair: pair[0].id):
        lines.append("- %s -> %s: %s" % (r.id, verdict.where, r.about))
    lines.append("")
    lines.append("Which reading of the wording is right?")
    return "\n".join(lines)


def resolve(f, candidates):
    """-> (rule, verdict, notes). `candidates` is [(Rule, Verdict)] sharing the top rank.

    One candidate passes straight through untouched. Two or more are resolved here, and
    the chosen verdict is marked down in confidence and flagged, because a tie means the
    evidence was weaker than a single rule's match.
    """
    notes = []
    if len(candidates) == 1:
        return candidates[0][0], candidates[0][1], notes

    physics = [r.id for r, _ in candidates if r.physics]
    chosen_rule, chosen = deterministic_choice(candidates)
    others = ", ".join(sorted(r.id for r, _ in candidates if r.id != chosen_rule.id))

    if physics:
        chosen.flags = list(chosen.flags) + ["physics_tie_not_arbitrated"]
        chosen.note = "; ".join(x for x in [
            chosen.note,
            "TIE with %s, resolved deterministically by rule id and NOT put to a model, "
            "because %s is physics (assay2chip/triage/tiebreak.py)" % (others, ", ".join(physics)),
        ] if x)
        chosen.confidence = 0.5
        notes.append({"op": f.op, "kind": "physics_tie", "chose": chosen_rule.id,
                      "over": others, "model": "not consulted"})
        return chosen_rule, chosen, notes

    answer = llm.complete_json(
        _prompt(f, candidates),
        task=TASK,
        system=SYSTEM,
        stub={"rule": chosen_rule.id,
              "why": "offline stub: lowest rule id, the deterministic fallback"},
    )
    picked_id = answer.get("rule") if isinstance(answer, dict) else None
    picked = next((pair for pair in candidates if pair[0].id == picked_id), None)
    if picked is None:
        notes.append({"op": f.op, "kind": "linguistic_tie", "chose": chosen_rule.id,
                      "over": others,
                      "model": "answer %r is not one of the tied rules; fell back to the "
                               "deterministic choice" % (picked_id,)})
    else:
        chosen_rule, chosen = picked
        notes.append({"op": f.op, "kind": "linguistic_tie", "chose": chosen_rule.id,
                      "over": others,
                      "model": (answer.get("why") or "")[:200]})
    chosen.flags = list(chosen.flags) + ["resolved_by_tiebreak"]
    chosen.note = "; ".join(x for x in [
        chosen.note,
        "TIE with %s on what the wording means, resolved in "
        "assay2chip/triage/tiebreak.py (llm mode %s); no physics was put to the model"
        % (others, llm.current_mode()),
    ] if x)
    chosen.confidence = 0.5
    return chosen_rule, chosen, notes

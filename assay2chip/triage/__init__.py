"""S3 triage: the rule engine that decides on_chip / off_chip / missing_part.

Four modules, and the split is deliberate:

  facts     everything a rule may reason from, derived from the op plus the contracts.
            Every number in here comes from contracts/blocks.json,
            contracts/parameters.json or contracts/materials.json through
            assay2chip/lib/contracts.py - never from a literal in this package.
  library   the block vocabulary guard. A part is a block id from contracts/blocks.json
            `basic` or `advanced`, or it is not a part. Nothing else may be named.
  rules     the ordered rule table. One rule, one decision, one citation.
  engine    runs the table over every op, attaches `placement`, and reports counts,
            flags and the parts the library lacks.

    from assay2chip.triage import engine

    result = engine.triage(unitops)          # new dict, `placement` on every op
    print(result.summary["counts"])          # {'on_chip': 4, 'off_chip': 5, ...}

The engine is rule-based and reproducible: same input, same output, no clock, no
randomness, no network. A language-model call is allowed in exactly one place
(`tiebreak.py`) and only to break a tie between two rules that disagree on the meaning
of a word. Physics is never put to a model.
"""

from __future__ import annotations

from . import facts, library, rules, tiebreak, engine  # noqa: F401

triage = engine.triage

__all__ = ["facts", "library", "rules", "tiebreak", "engine", "triage"]

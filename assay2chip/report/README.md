# The report (S12)

`assay2chip/stages/s12_report.py` writes two files into `assay2chip/work/<id>/`:

| File | What it is |
|---|---|
| `report.json` | every number, gathered by `model.collect()`. The machine-readable one |
| `report.md` | the same numbers, rendered by `markdown.render()`. The one people read |

Both come from one `collect()` call, so they cannot disagree. **No number is computed in
`markdown.py`.** If a figure is not in `report.json` it does not go in `report.md`.

```
assay.json  unitops.json  partition.json  verify.json  cfd/findings.json  cfd/fixes.json
cad/exports/<chip_id>/print/
        │
  model.collect()   ──►  report.json
        │
  markdown.render() ──►  report.md
```

## It is deliberately short

Tables, not prose. `protocols/pipeline_status.md` is the house style: one sentence of
context, then a table, then blunt notes. Long tables are capped (`markdown.CAPS`) with a
footnote pointing at `report.json` for the rest, so brevity never means hiding anything.

## The headline numbers

| Number | Why it is a headline |
|---|---|
| chips | one assay does not fit on one chip; how many it took is the first question |
| on-chip fraction of steps | how much of the paper actually became geometry |
| **hand-offs needing a human** | "no human intervention" is the stated goal, so this is the honest answer to whether it was met. `automated: false` in `partition.json` |
| unresolved questions from S1 | what the source never stated. A run with none is suspicious |
| CFD findings by severity, fixes applied vs proposed | what would stop it flowing, and what was changed automatically rather than by a person |
| fabrication bundle paths | `cad/exports/<chip_id>/print/` - what you can actually send to a printer |

## Where every assay step ended up

Four outcomes, and the difference between the first two matters:

| Outcome | Meaning |
|---|---|
| `chip` | the op is on a chip and names a part that chip has. Geometry does the work |
| `op` | on a chip, but as a protocol action through parts that already exist. Still automated - but an assay that is *all* `op` has not really been put on a chip |
| `off_chip` | a tube, a spin, an instrument. S3 said so |
| `missing` | S3 found no part (`missing_part`), or no unit op covers the step at all. The second is worse and `why` says which |

## Honest limits

Always present, never trimmed. Nothing in this repo has been printed or run: every number
is a contract value, a geometric calculation or a simulation. The section exists because
a simulated number that reads as a measurement is the failure mode this whole repo has
been fighting - see `model.HONEST_LIMITS`.

## Modules

| Module | Job |
|---|---|
| `findings.py` | the finding shape S9 and S10 both produce, plus a tolerant reader for their files |
| `model.py` | gathers the stage files into `report.json`. Counting only |
| `markdown.py` | renders `report.json`. Formatting only |

A missing stage is recorded in `report["missing"]` and shown in the document. The stage
never fails a run: the report is how you find out what did not run.

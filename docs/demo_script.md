# Demo script: protocol in, chips out, and the faults the CFD finds

Every command runs from the repo root with `./.venv/bin/python` (written `py` below).
All of it is offline: no model key, no network.

**Before you start (5 min, the day before and again an hour before):**

1. Run the whole script once on the demo machine. Close everything else: the timings
   below are CPU-bound, and a loaded laptop runs them several times slower (they were
   measured under a load average of ~100 on 8 cores, where the demo took 4-8 min wall
   for ~75 s of CPU).
2. `py -m unittest tests.test_demo_profile` - nine fast checks, should say `OK`.
3. Open `assay2chip/work/pipeline_mvp/cfd/report.md` in a tab: the full-chain CFD takes
   ~1 h, so it is shown precomputed, never run live.
4. Have a terminal with a large font, and a browser.

---

## 1. The whole chain on a small protocol  (~2 min on a quiet machine; ~75 s CPU)

```
py tools/demo_pipeline.py
```

**What it shows.** A three-step bead-wash protocol (`tests/fixtures/demo_bead_wash.md`,
synthetic) goes through all 13 stages, S0 ingest to S12 report, through the real
orchestrator. The first line states the profile. The last block reads back from disk:
how many chips, chip specs, protocols, the S9 verdict, the S10 flow findings, the S11 print
files (STL body and mould, closed DXF, Fusion rebuild script, renders) and the time.

**What to say.**
- "This is the real pipeline on a smaller problem. The demo profile changes only how much
  work there is: 2 flow steps per chip instead of 6, 8 tracked particles instead of 24,
  and validation scoped to these chips. The routing grid is the same 0.05 mm as a real
  run; the router refuses to go coarser."
- "Three chips came out because the protocol needs a wash and a recovery that the
  partitioner will not put on one board."
- "S9 runs the same four checks a hand-designed chip has to pass."
- "S10 found a 45% dead zone in a wash chamber of the chip it just designed. It proposes
  a fix. It does not apply it automatically yet - see the limits below."
- Open `assay2chip/work/demo_small/report.md` and one render,
  `cad/exports/demo_small_c1/print/render_iso.svg`.

`py tools/demo_pipeline.py --profile full` runs the same chain at full fidelity
(several minutes).

## 2. The CFD on the hand-designed chain - the strongest evidence  (settling ~12 s CPU, retention ~50 s CPU)

These are faults a human review of these chips missed. Each command recomputes the finding
on the chip spec in this checkout and prints the two numbers that disagree.

```
py tools/demo_findings.py settling
py tools/demo_findings.py retention
```

(`sweep` and `all` exist but are NOT for live use: `sweep` re-simulates all of Module 3,
~3 min CPU, and today it does not reproduce - see the limits.)

| Command | Chip | The two numbers |
|---|---|---|
| `settling` | Module 4, feed line `ch_in_1` | swollen capsules reach the floor in **0.38 s**; crossing the line takes **1.02 s** -> 100% settle before they arrive |
| `retention` | Module 3, `main_trap` | **+1.8 um** of headroom before capsules escape the 25 um gap, against a **+/-20 um** fabrication z tolerance -> it works over 55% of the window |
| `sweep` | Module 3 (any chip with `--chip`) | the exchange each wash reaches against the 99% target - **currently prints NOT REPRODUCED; do not use live** |

**What to say.** "The capsules are denser than the buffer. At the flow the protocol uses,
they fall 300 um in under half a second, and the line takes a second to cross, so they
never arrive. Nobody caught that reading the drawing." For retention: "The gap is right
on paper. But a print that is 2 um too deep lets every capsule through, and the process
holds depth to 20 um."

Then show the whole-chain result, precomputed:
`assay2chip/work/pipeline_mvp/cfd/report.md` (six chips, 57 findings, figures under
`figures/`). To regenerate it: `py assay2chip/cfd/run.py --pipeline protocols/pipeline_mvp.json`
(~1 h; never live).

## 3. The web GUI  (~1 min to show)

```
ASSAY2CHIP_PROFILE=demo py web/server.py        # http://127.0.0.1:8765
```

Upload `tests/fixtures/demo_bead_wash.md` (or paste its text), start a run, and watch the
stages tick through on the live event stream; then open the chips, the report and the
artefacts. The env var puts the GUI's runs on the demo profile; without it they run at
full fidelity. The twin presenter is `py tools/demo.py`.

---

## If something fails mid-demo

| Symptom | What to do and say |
|---|---|
| `demo_pipeline.py` is slow | It is CPU-bound; something else is running. Keep talking through the stage lines as they appear, or switch to the previous run: `assay2chip/work/demo_small/report.md` stays from the rehearsal. |
| a stage prints `ERROR` | The orchestrator stops and names the stage. Say so - that is the system refusing to emit a bad chip - and show `assay2chip/work/demo_small/run_manifest.json`. Re-run with `--keep` to resume is not supported; just re-run the command (it cleans its own outputs). |
| "would overwrite N file(s) this pipeline did not produce" | Another run left files with the same id. Use `--id demo_small2`. |
| `demo_findings.py` says NOT REPRODUCED | The chip or the model changed since this script was written. Do not improvise numbers: show `assay2chip/work/pipeline_mvp/cfd/findings.json` and say the live check disagrees. |
| web GUI port taken | `py web/server.py --port 8766`. |
| anything else | Fall back to the precomputed reports above, and the slides. |

## What cannot be demonstrated today - read before presenting

- **Every CFD finding is labelled UNTRUSTED.** The solver's validation suite currently
  fails `duct_resistance` (and, over the whole repo, `mass_conservation` at 9.7e-9 against
  a 1e-9 tolerance and `hele_shaw_validity`), so `analyse.py` marks every finding
  untrusted. The analytic benchmarks themselves (series/parallel, Hele-Shaw uniform and
  point source, wall shear, grid convergence, operator cross-check) pass. Say "validated
  against analytic solutions" only with that qualifier.
- **The CFD's automatic fix loop does not close.** S10 proposes geometry fixes, but S6
  does not read them, so a re-run would rebuild the same chip. S10 now says so and stops
  instead of looping three times; the fix is a proposal for a human.
- **The 38%-against-99% sweep finding does not reproduce** on the current Module 3
  protocol, which now sweeps 4.61 chamber volumes: neither the full Module 3 CFD run nor
  `tools/demo_findings.py sweep` finds an exchange shortfall, and no record of the 38%
  number exists in the repo. Do not quote 38% until someone finds the chip and protocol
  version it came from.
- **The demo chip itself has an open CFD blocker** (a 45% dead zone in `wash_2` on
  `demo_small_c1`). That is a true result about a generated chip - say so - but it means
  "printable chips come out" is true while "chips that pass the flow check come out" is
  not, yet.
- **A transport bug shows up in the demo run:** `transport.analyse()` raises
  `target_fraction must be in [0, 1): got 1.0` on `demo_small_c2`'s lyse step, so that
  step's exchange is not checked. It is in `assay2chip/cfd/transport.py`.
- **The full end-to-end test still exceeds 15 minutes** (14 chips from the bead-capture
  fixture, all simulated); only the demo profile is watchable.
- **Nothing is fabricated or wet-tested by this software.** Print files are produced; no
  biology has run.

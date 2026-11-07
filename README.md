# assay-a-thon_hack: capsule processor

Describe a single-cell assay protocol; AI designs a microfluidic **capsule processor**
chip for it, and an AI operator runs the protocol on that chip, shown live as a 3D
digital twin. Cells sit in capsules that stay put on mesh floors while liquids are
exchanged around them, split into chambers and pooled back together. This weekend
there are no pumps, cameras or wet-lab data: everything runs in software, with
resin-printed, hot-embossed parts for the physical chip (30 × 20 mm, 0.3 mm deep, one
fluidic layer). Retention features cannot be printed at this floor, so capsule
retention needs a bought mesh insert or sedimentation. Stage map: [protocols/pipeline_status.md](protocols/pipeline_status.md). Closed-loop runs use a
**virtual chip** and are labelled **SIMULATED HARDWARE** everywhere; what to buy to
run them for real is in [docs/hardware_roadmap.md](docs/hardware_roadmap.md).

## Pipeline

```
 protocols/              chip/                     cad/
 MJ + Kilian             Yuvraj + Szonja           Ayrton
 protocol.json           NL prompt                 CAD
 (steps + physics)          │                       │
       │                    ▼                       ▼
       │              chip_spec.json  ──────▶  exports/<part_id>.stl
       │                    │                       │
       └─────────┬──────────┘                       │
                 ▼                                  │
           operator/  (Bruno)                       │
   optimizer ─▶ safety ─▶ closed-loop controller    │
   driven by the planner, Claude (llm_operator.py)  │
   or a replayed transcript; scenarios/ inject      │
   faults on the virtual chip                       │
                 │                                  │
                 ▼                                  │
  runs/<protocol>__<chip>[__<scenario>].json        │
                 │                                  │
                 └────────────────┬─────────────────┘
                                  ▼
                            twin/  (Bruno)
                  3D replay in the browser (three.js)
```

The **contracts** (`contracts/`) are the shared file formats between these steps.
Each pair works against placeholder files until the real ones are ready. Start
with [contracts/README.md](contracts/README.md).

## Who owns what

| Folder | Owner | Produces |
|---|---|---|
| `protocols/` | MJ + Kilian | `protocol.json`: steps and physics numbers |
| `chip/` | Yuvraj + Szonja | `chip_spec.json`: NL → chip geometry |
| `cad/` | Ayrton | STL per part, named by chamber ID |
| `operator/` | Bruno | run files: planner + LLM operator |
| `twin/` | Bruno | the 3D digital twin |
| `contracts/`, `tools/`, integration | Bruno | schemas, validator, CI |

## Quickstart

Needs Python 3.10+.

```bash
git clone https://github.com/brunohawkins/assay-a-thon_hack.git
cd assay-a-thon_hack
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt     # just jsonschema

python tools/validate.py            # 1. check every file against the contracts
python tools/check_chip_spec.py     #    and every chip design against the capsule rules
python operator/run.py --all        # 2. regenerate every run (simple + closed-loop scenarios)
python operator/run.py --scenario scenarios/m3_wash_clog.json   #    or one closed-loop scenario
python -m http.server 8000          # 3. serve the repo root...
# ...and open http://localhost:8000/twin/
```

In the twin: pick a protocol, then Play/Pause (Space), Step (→), speed 1x/5x/20x
(keys 1/2/3), click the timeline to jump, IDs (L) to toggle part labels, drag to orbit.
Switching protocol replays on the same chip. `#run=<file>` in the URL opens a specific run.
**Real scale / Display model** switches between the working chip (true sizes) and the
printed show piece; the scale bar works in both. The **Chip
sizing** panel shows what the sizing calculator says the chip needs next to what it has.
The twin opens on the **pipeline view**: every current module left to right (from
`protocols/pipeline_mvp.json`): Module 1 v1 (culture) -> cell prep -> Module 2 (encapsulation, `spc_demo_v1`) ->
Module 3 (load, lysis, wash; translated from Szonja's file) -> barcoding (a HOLE: no chip, no protocol) ->
Module 4 (sublibrary split + index PCR) -> Module 5 (release + cleanup) -> SPRI and QC (off-chip) -> library out. Each card
has a status badge (FINAL, DRAFT, TRANSLATED, MISSING), says whether it is **built for the
dye demo** or **twin only**, and shows its hand-off ports (TBC where not named) with
a ⚠ where the two chips' ports differ (the demo needs an adapter). Cells, capsules and library
move between the cards; click a module for its own view (tabs along the top). Module 1 v1 is a
flow twin (see below); Module 3 plays Szonja's workflow on the translated chip; Modules 4 and 5
play MJ's protocols with a design-note panel (fill order ABCD DCBA and thermocycle on the whole
chip; pass k of 4, magnet on/off). The superseded drafts (Module 1 brief, 1b, 3 wash) are under
**Old drafts**. Generated chips' views show their animations (valves, a day counter while cells
differentiate, Module 2's nozzle close-up with droplets forming core-shell capsules, the UV
flash) and a **Parameters** panel with the generator values used.
Closed-loop runs add the **control loop** panel (believed vs true state, prediction vs
measurement charts with model-update markers, the optimizer's choices, the event log
with safety tags), an alert banner, and an **Open loop / Closed loop** toggle that
replays the same protocol and faults the other way. How the closed loop works:
[operator/README.md](operator/README.md).

Other tools: `python operator/sizing.py mvp cells:100000` (chip sizing, port of Szonja's
sheet), `python cad/export_display_model.py cad/m3_wash_spec.json` (printable display model),
`python -m unittest discover -s tests` (tests).

The twin only reads run files, so a recorded run always works offline. That's the
demo fallback: if the live LLM run misbehaves, play the committed run.

## Swapping a placeholder for real work

Every placeholder file says `"placeholder": true`. To replace one:

| You have | Do this |
|---|---|
| A real protocol | New file in `protocols/` (copy a placeholder), new `id`, fill in steps → `python tools/validate.py` → `python operator/run.py --protocol protocols/<file>.json --chip chip/<spec>.json`. MJ reviews it first. |
| A real chip spec | New file in `chip/`, new `id`, **keep the IDs** (`exchange_1`, `split_1`…) → validate → `python operator/run.py --all --chip chip/<file>.json` |
| CAD parts | `cad/exports/<id>.stl` in mm, same origin as the chip spec → reload the twin; matching parts replace the boxes |
| An LLM run | `export ANTHROPIC_API_KEY=...` then `python operator/run.py --scenario scenarios/m3_wash_clog.json --operator llm`; commit the run and its transcript |
| Real hardware | See [docs/hardware_roadmap.md](docs/hardware_roadmap.md); pick an adapter with `--adapter` (`dryrun` prints what the hardware would do) |

Set `"placeholder": false` only when a file is real. Read [CONFIDENTIALITY.md](CONFIDENTIALITY.md)
before committing anything real, and [CONTRIBUTING.md](CONTRIBUTING.md) for how we work.

## Repo layout

```
contracts/          the shared formats: plain-English README + JSON Schemas
protocols/          protocol.json files (MJ + Kilian)
chip/               chip specs + nl_to_spec/ (Yuvraj + Szonja)
cad/exports/        STL per part, named by ID (Ayrton)
operator/           model, optimizer, safety, closed-loop controller, operators, adapters, run.py (Bruno)
scenarios/          closed-loop test scenarios: sensors on/off, model-vs-reality gaps, injected faults
runs/               generated run files, audit reports, operator transcripts + index.json (the twin reads these)
twin/               three.js digital twin, no build step (Bruno)
tools/validate.py   validates everything and checks IDs line up (also runs in CI)
tools/check_chip_spec.py  real-scale design checks: trap gaps, channel widths, heights, temperatures
tests/              unit tests (sizing, design checks, safety rules, operators)
docs/               hardware roadmap
```

Rules: Python 3.10+ with the standard library plus `jsonschema` only. The twin is
plain HTML + ES modules with three.js pinned from a CDN: no build step, no npm.

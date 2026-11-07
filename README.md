# assay-a-thon_hack: capsule processor

Describe a single-cell assay protocol; AI designs a microfluidic **capsule processor**
chip for it, and an AI operator runs the protocol on that chip, shown live as a 3D
digital twin. Cells sit in capsules that stay put on mesh floors while liquids are
exchanged around them, split into chambers and pooled back together. This weekend
there are no pumps, cameras or wet-lab data: everything runs in software, with
laser-cut and 3D-printed parts for the physical chip.

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
   planner.py (deterministic) or                    │
   llm_operator.py (Claude, 6 commands as tools)    │
                 │                                  │
                 ▼                                  │
     runs/<protocol>__<chip>.json                   │
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
python operator/run.py --all        # 2. generate runs for every protocol
python -m http.server 8000          # 3. serve the repo root...
# ...and open http://localhost:8000/twin/
```

In the twin: pick a protocol, then Play/Pause (Space), Step (→), speed 1x/5x/20x
(keys 1/2/3), click the timeline to jump, IDs (L) to toggle part labels, drag to orbit.
Switching protocol replays on the same chip. `#run=<file>` in the URL opens a specific run.

The twin only reads run files, so a recorded run always works offline. That's the
demo fallback: if the live LLM run misbehaves, play the committed run.

## Swapping a placeholder for real work

Every placeholder file says `"placeholder": true`. To replace one:

| You have | Do this |
|---|---|
| A real protocol | New file in `protocols/` (copy a placeholder), new `id`, fill in steps → `python tools/validate.py` → `python operator/run.py --protocol protocols/<file>.json --chip chip/<spec>.json`. MJ reviews it first. |
| A real chip spec | New file in `chip/`, new `id`, **keep the IDs** (`exchange_1`, `split_1`…) → validate → `python operator/run.py --all --chip chip/<file>.json` |
| CAD parts | `cad/exports/<id>.stl` in mm, same origin as the chip spec → reload the twin; matching parts replace the boxes |
| An LLM run | `export ANTHROPIC_API_KEY=...` then add `--llm` to `run.py`; commit the good run |

Set `"placeholder": false` only when a file is real. Read [CONFIDENTIALITY.md](CONFIDENTIALITY.md)
before committing anything real, and [CONTRIBUTING.md](CONTRIBUTING.md) for how we work.

## Repo layout

```
contracts/          the shared formats: plain-English README + JSON Schemas
protocols/          protocol.json files (MJ + Kilian)
chip/               chip specs + nl_to_spec/ (Yuvraj + Szonja)
cad/exports/        STL per part, named by ID (Ayrton)
operator/           planner, LLM operator, adapters, run.py (Bruno)
runs/               generated run files + index.json (the twin reads these)
twin/               three.js digital twin, no build step (Bruno)
tools/validate.py   validates everything and checks IDs line up (also runs in CI)
```

Rules: Python 3.10+ with the standard library plus `jsonschema` only. The twin is
plain HTML + ES modules with three.js pinned from a CDN: no build step, no npm.

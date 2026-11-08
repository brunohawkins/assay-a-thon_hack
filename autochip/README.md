# autochip: protocol → chips

A protocol compiler. Give it a wet-lab protocol, a published paper or a plain-English goal, plus the
constraints of whatever you can fabricate, and it gives back:

- the protocol broken down **chip by chip** (which steps run on which chip, in which module, what stays at
  the bench, which parts are missing),
- a **chip design** for each chip (chip spec in the team contract format, CAD: SVG/PNG drawing, DXF, STLs,
  Fusion 360 script, and a step-by-step build guide),
- a **run protocol** for each chip (which port feeds, vents or is capped at each step, rates, volumes,
  temperatures), checked by the hydraulic solver, and a **run sheet** for the whole pipeline that a bench
  scientist can follow,
- a **report** (`report.html`) tying it together.

The breakdown is done by an agent (Claude), checked by code at every stage, and sent back to the agent to
fix when a check fails. The same stages have built-in rules, so the whole thing also runs offline.

```
protocol / paper / prompt ──► 1 ingest ──► 2 extract ──► 3 partition ──► 4 layout ──► 5 runplan ──► 6 report
                                  │            (agent)        (agent)       │  ▲
                                  │                              ▲          │  │ constraint errors
                                  │                              └──────────┘  │ go back to partition
                           constraints.json ─────────── every stage ───────────┘
```

## Run it

```bash
# A protocol file (md, txt, docx, xlsx, csv, protocol json)
python -m autochip run autochip/examples/spc_capsule_protocol.md

# A paper (PDF); the methods section is found and read first
python -m autochip run autochip/examples/spc_methods_paper.pdf --constraints resin_print_multilayer

# Just a goal in plain English: the agent writes the protocol first (every assumption is flagged)
python -m autochip run --prompt "$(cat autochip/examples/capsule_scrnaseq_prompt.txt)" --constraints assayathon_demo

# Your own constraints file
python -m autochip run my_protocol.md --constraints my_lab.json

# Offline (no agent): built-in rules
python -m autochip run my_protocol.md --backend rules

# Replay a saved agent run (demo, no network)
python -m autochip run autochip/examples/spc_capsule_protocol.md --backend replay --replay <earlier out>/agent

# Re-run from a stage after editing a stage file by hand (e.g. you moved a module in chip_plan.json)
python -m autochip run --resume autochip/out/spc_capsule_protocol --from-stage layout

# Continue an interrupted run: starts at the first stage whose output is missing
python -m autochip run --resume autochip/out/spc_capsule_protocol

python -m autochip constraints --list
python -m autochip constraints --show assayathon_demo
```

Agent backends (`--backend`): `auto` (default: API key if set, else the Claude Code CLI, else rules),
`api` (`ANTHROPIC_API_KEY`, model from `--model` / `AUTOCHIP_MODEL`), `claude-cli` (`claude -p`, uses your
Claude Code login: no key needed; model from `--model` / `AUTOCHIP_CLI_MODEL`, else the CLI's default; it runs
with the stage's system prompt in place of Claude Code's own, no tools, from an empty folder), `rules`, `replay`
(`--replay <folder>`; with `--resume` it defaults to `<resume>/agent`).

Only **extract** and **partition** ask the agent; the other stages are code. When a chip breaks an error-level
constraint, the errors go back to partition (the agent, or with `--backend rules` the rules' re-split) for up to
`--rounds` (default 2) more rounds; a round that gives the same plan, or leaves the same errors, ends the loop.
A replay re-splits with the saved answers: layout is deterministic, so replaying a live run hits the same
constraint errors and finds the re-split answer the agent gave for them (`partition__resplit_<hash>.json`).

## Output folder

| File | Stage | What it is |
|---|---|---|
| `source.json` | 1 ingest | the text that was read |
| `constraints.json` | – | the resolved constraints used |
| `run_meta.json` | – | the backend (and replay folder) the run used; `--resume` keeps it unless you pass `--backend` |
| `protocol_ir.json` | 2 extract | typed steps ([schema](../contracts/schemas/protocol_ir.schema.json)) |
| `chip_plan.json` | 3 partition | chips, modules, hand-offs, off-chip steps, missing parts, step coverage ([schema](../contracts/schemas/chip_plan.schema.json)) |
| `designs/<chip>.json` | 3 partition | each chip as a hand-editable design file ([chip/designs](../chip/designs/README.md) format) |
| `chips/<chip>.json` | 4 layout | chip spec ([contract](../contracts/schemas/chip_spec.schema.json)): twin, operator and checker read it |
| `layout_findings.json` | 4 layout | each chip's constraint findings; a resume from `runplan` / `report` reads it (a spec edited since is re-checked) |
| `cad/<chip>/` | 4 layout | drawing (SVG/PNG), DXF, STLs, Fusion 360 script, `BUILD.md` |
| `runs/<chip>.protocol.json` | 5 runplan | run protocol ([contract](../contracts/schemas/protocol.schema.json)) |
| `runs/<chip>.run.json`, `runs/<chip>.run_sheet.md` | 5 runplan | port states per step + flow-solver checks; the bench version |
| `runs/<chip>.twin_run.json` | 5 runplan | the run protocol as the operator's planner turns it into twin events (when it accepts it) |
| `runs/pipeline_run_sheet.md`, `runs/pipeline_run.json` | 5 runplan | the whole protocol as run in the lab, chips + bench steps + hand-offs in order |
| `pipeline.json`, `report.md`, `report.html`, `overview.svg`, `overview.png` | 6 report | the result in one place |
| `agent/` | 2, 3 | every agent exchange (`<stage>[__<key>].log.json`, e.g. `partition__resplit_<hash>` for a re-split round) and accepted answer (`<stage>[__<key>].json`: what `--backend replay` reads; removed when a later run of that stage gets no valid answer) |

When a re-split or a hand-edited `chip_plan.json` drops a chip, its files in `chips/`, `designs/`, `cad/` and
`runs/` are removed. Re-running from `layout` writes design files only for chips that have none, so your edits
in `designs/` are kept (and are not updated from an edited `chip_plan.json`).

## Constraints

One JSON file ([schema](../contracts/schemas/constraints.schema.json)). Everything is optional except `id` and
`fabrication.method`; missing or `null` means no limit (except where a default is stated: crossings follow
`multilayer.allowed`, `max_chip_mm` falls back to `printable_area_mm`).

`extends` starts from a preset id, or another constraints file (a relative path is relative to the file that
extends it). Objects merge key by key; any other value (a list, a number, a string, `null`) replaces the
preset's. So `"equipment": [...]` and `"notes": [...]` replace the preset's lists rather than adding to them,
and `"max_per_chip": null` removes a preset's limit. Settings that contradict each other are refused when the
file is loaded: channel crossings without multilayer, `depth.min_mm` > `max_mm`, `allowed_mm` outside them,
`max_chip_mm` bigger than the printable area, a depth not below the build height, edge margins that eat the
chip, `min_pitch_mm` below `min_diameter_mm`, and ranges given high to low.

| Group | Fields |
|---|---|
| `fabrication` | `method` (resin_print_hot_emboss, resin_print, hot_embossing, fdm_print, two_photon_print, laser_cut_laminate, cnc_milling, soft_lithography_pdms, glass_wet_etch, glass_laser_3d_etch, injection_molding, other), `method_note`, `machine`, `material`, `sealing`, `multilayer.allowed / max_fluidic_layers / vias_allowed`, `channel_crossings_allowed`, `on_chip_valves.allowed / type`, `depth.max_mm / min_mm / single_depth / allowed_mm`, `min_feature.open_channel_width_mm / enclosed_channel_width_mm / wall_mm / gap_mm / max_aspect_ratio`, `thermal_zoning_allowed` (unset: steps at clashing temperatures go on separate chips) |
| `envelope` | `printable_area_mm {x, y, z}`, `max_chip_mm {x, y}`, `max_chips`, `edge_margin_mm` |
| `ports` | `max_per_chip`, `min_diameter_mm`, `min_pitch_mm`, `connector`, `placement` |
| `parts` | `source` (library_only, library_and_parametric, parametric_only), `library`, `allow_scaling`, `allowed`, `forbidden` |
| `off_chip` | `equipment` (syringe_pump, thermocycler, magnet, led_405nm, ...), `pump_channels`, `flow_range_ul_min`, `manual_steps_allowed` |
| `operating` | `temp_range_c`, `max_pressure_kpa`, `biocompatible_required`, `optical_window_required`, `sterilisable` |
| `objectives` | `prefer` (fewest_chips, most_on_chip, simplest_to_make, one_chip_per_module), `notes` |
| `notes` | anything else in plain English; passed word for word to the agents |

Every field is in the constraints summary the agents read, but some are only advice to the agents: no code
checks them, so the rules backend ignores them. These are `fabrication.machine`, `method_note`,
`multilayer.vias_allowed`, `on_chip_valves.type`, `envelope.printable_area_mm.z` (only compared with
`depth.max_mm` on load), `ports.connector` (named in the report), and `operating.biocompatible_required`,
`optical_window_required`, `sterilisable`.

Presets in `autochip/constraints/`: `unconstrained` (default), `assayathon_demo` (Form 3B resin + hot
embossing, 30 × 20 mm, one layer, 6 ports, Flowmatter blocks only), `resin_print_multilayer` (Form 3B, printed
closed, 3 layers), `pdms_soft_lithography` (one flow layer + a pneumatic valve layer: planar flow routing),
`glass_laser_3d_etch` (true 3D in fused silica, clamped manifold), `laser_cut_laminate` (PMMA/PSA stack,
Luer ports at 10 mm pitch).

Example of your own:

```json
{
  "id": "my_lab",
  "extends": "resin_print_multilayer",
  "envelope": {"max_chip_mm": {"x": 50, "y": 25}},
  "ports": {"max_per_chip": 8},
  "off_chip": {"equipment": ["syringe_pump", "heater", "thermocycler", "magnet"], "pump_channels": 4},
  "notes": ["Keep anything with oil off the culture chip."]
}
```

## Manual route

The automated pipeline doesn't replace the manual builder. Every chip in a plan is also written to
`designs/<chip>.json` in the [chip/designs](../chip/designs/README.md) format, so you can edit it and build it
with `chip/components/build_chip.py`, or edit `chip_plan.json` and re-run from `--from-stage layout`.

## Confidentiality

Agents are told to paraphrase and use generic reagent names, and never to write sequences or exact recipes.
Only feed in published protocols or placeholder versions; follow [CONFIDENTIALITY.md](../CONFIDENTIALITY.md).
Everything the agent saw and said is in `agent/` so it can be reviewed before anything leaves the team.

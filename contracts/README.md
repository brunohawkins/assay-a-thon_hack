# The contracts

Four shared file formats connect our four workstreams. If your file follows its
contract, the other pairs can use it without asking you anything, and you can work
against their placeholder files until the real ones arrive.

```
protocol.json  (MJ + Kilian)  ─┐
                               ├─▶  operator  ─▶  run.json  ─▶  twin
chip_spec.json (Yuvraj+Szonja) ┘                    ▲
<part_id>.stl  (Ayrton)  ──────────────────────────┘ (twin swaps STLs in by id)
```

The exact rules live in `schemas/*.schema.json` (JSON Schema). You don't need to
read those: this page says the same thing in plain English, and
`python tools/validate.py` tells you exactly what's wrong if a file breaks a rule.

Tip: in VS Code, these files get autocomplete and red underlines for mistakes
automatically (set up in `.vscode/settings.json`).

---

## Units

Every number carries its unit in its field name, so nobody has to guess.

| What | Unit | Field names end in | Example |
|---|---|---|---|
| Length, position, size | millimetres | `_mm`, or the whole chip spec (`"units": "mm"`) | `"thickness_mm": 3` |
| Volume | microlitres | `_ul` | `"volume_ul": 100` |
| Time | seconds | `_s` | `"duration_s": 600` (10 minutes) |
| Temperature | degrees Celsius | `_c` | `"temp_c": 40` |
| Flow rate | microlitres per minute | `_ul_min` | `"flow_ul_min": 100` |
| Mesh pore size | micrometres | `_um` | `"mesh_pore_um": 20` |

Handy: 1 mm³ = 1 µL, so a 4 × 3 × 0.3 mm chamber holds 3.6 µL. (Chambers really are that
shallow: the envelope allows one 0.3 mm etch depth. See Fabrication, below.)

## IDs: the join key

Every chamber and port has an ID, and **that ID is how all the files refer to it**.
The protocol says "exchange in `exchange_1`", the chip spec says where `exchange_1`
is, Ayrton's file for it is `exchange_1.stl`, and the twin lights up `exchange_1`.

Rules:
- lowercase letters, digits and underscores only: `exchange_1`, `split_3`, `pool_1`
- no spaces, capitals or dashes (`Exchange 1`, `split-3` are wrong)
- the same ID never means two things in one chip
- reagent names follow the same style: `wash_buffer`, `lysis_buffer`, `rt_mix`

The placeholder chip uses: `inlet_1`, `exchange_1`, `split_1` … `split_4`, `pool_1`, `outlet_1`.

### The current modules (the pipeline)

`protocols/pipeline_mvp.json` lists the modules in process order, who owns each one, its
status and how it is built:

| Module | Chip spec | Protocol | Owner | Status | Build |
|---|---|---|---|---|---|
| 1. Culture (v1) | `chip/module1_v1.json` | `protocols/module1_v1.json` | MJ | final | resin print + hot emboss, 0.8 mm drilled ports |
| Cell prep | `chip/cell_prep_v1.json` | `protocols/cell_prep_v1.json` | TBC | final | resin print + hot emboss, 0.8 mm drilled ports |
| 2. Encapsulation | `chip/spc_demo_v1.json` | `protocols/spc_demo_v1.json` | TBC | final; the only fabricable encapsulation chip | resin print + hot emboss, 0.8 mm drilled ports |
| 3. Load, lysis, wash | `chip/module3_v1_translated.json` (from Szonja's `chip/modules/module3_step1_3.json`) | steps in her file | Szonja | translated | resin print + hot emboss; retention gap is a fabricated depth weir (25 µm gap) |
| Reverse transcription (un-barcoded) | `module3_v1_translated` | `module3_v1.json` | ~42–50 °C hold | Runs in the existing capsule trap after lysis and wash. Split-and-pool (BC1/BC2/BC3) and the hairpin ligation are dropped: cells already carry a per-lane barcode from module 1, so the RT primer is a plain poly(dT) | MJ |
| 4. Sublibrary + index PCR | `chip/module4_v1.json` | `protocols/module4_v1.json` | MJ | final | hot-embossed COC, 0.8 mm drilled ports |
| 5. Release + cleanup | `chip/module5_v1.json` | `protocols/module5_v1.json` | MJ | final | hot-embossed COC, 0.8 mm drilled ports |

**Superseded drafts**, kept for reference and out of the pipeline: `cad/m1_culture_spec.json`
(replaced by Module 1 v1), `cad/m1b_cellprep_spec.json` (replaced by `cell_prep_v1`) and
`cad/m3_wash_spec.json` (replaced by Module 3). Their `module` block says
`"status": "superseded"` and `superseded_by`; `tools/validate.py` fails if one is put back in
the pipeline. `cad/m2_encapsulation_spec.json` is retired too (it cannot make capsules at
30 × 20 mm; `spc_demo_v1` replaces it), and so is stage A of `chip/sctip_spc_v1.json`
(150 × 100 mm). Stage map: `protocols/pipeline_status.md`.

**Fabrication.** Every chip is 30 × 20 mm, 0.3 mm deep, one fluidic layer, resin-printed and
hot-embossed, using only the blocks in `contracts/blocks.json`. On-chip valves need a second
layer and are not allowed: use off-chip rotary or selector valves.

**Retention is a fabricated depth weir.** The 200 µm fabrication floor is **in-plane only**:
z is free, so a crest that stops 25 µm short of the lid retains a 70 µm capsule while liquid
passes over it. That is still one fluidic layer, because the crest is relief in the same print
or mould. So capsule retention **is** fabricated, and every chip on disk records
`"retention": {"type": "weir", "opening_um": 25, "source": "fabricated"}`. A bought mesh
insert and a sedimentation sump are the ranked *fallbacks*, not the plan — they are only
needed for retention below 50 µm, or for bare cells (10–15 µm), which this weir does **not**
retain. The crest **height** is never a fixed number: it is that chip's etch depth minus the
25 µm gap. Full reasoning, the legal 21–28 µm gap band and the open z-tolerance risk are in
[`contracts/blocks.json`](blocks.json) (`retention`).

**Translated specs.** Szonja's Module 3 file is in its own format. `tools/convert_module3.py`
writes it as a chip spec, keeping her ids and numbers; whatever it had to infer (footprint,
layers, positions, routes, valve construction) is marked `"placeholder": true`, and the
spec's `translated_from` names the source. Edit her file, not the translation, and re-run
the converter (a test fails if the committed translation is out of date).

### Module prefixes (generated chips)

On a chip with a `module` block (the generated drafts), **every id starts with its module
prefix** (`m1_`, `m1b_`, `m2_`, `m3_`): part ids, STL names, spec files, protocol files. The
generated spec records the script and parameter values that made it in its `generator`
block. Specs written by hand by their owners (`chip/module*_v1.json`) have no `module` block
and keep their own ids.

### How to design Module 2 or 3

The generators are the design tool: **don't hand-edit `cad/m*_spec.json`.**

1. **Change parameters.** Every parameter, its default and where the default comes from is
   in `contracts/schemas/generators/m2_encapsulation.schema.json` (or `m3_wash.schema.json`).
   `--help` lists them too. The ones you'll touch most:
   - Module 2: `--nozzle-width-um`, `--core-flow-ul-h` / `--shell-flow-ul-h` / `--oil-flow-ul-h`,
     `--y-junction-angle-deg`, `--coflow-length-mm`, `--separation-length-mm`, `--capsule-count`,
     `--inlet-filters`, `--cell-filter-gap-um`, `--footprint-x-mm`.
   - Module 3: `--capsule-count`, `--capsule-diameter-um`, `--swollen-diameter-um`,
     `--mesh-opening-um`, `--chamber-height-um`, `--channel-width-factor`, `--inlets`, `--backflush`.
2. **Run one command:**
   ```bash
   python cad/generators/m2_encapsulation.py --nozzle-width-um 50 --oil-flow-ul-h 800
   python cad/generators/m3_wash.py --capsule-count 25000 --inlets 2
   ```
   It writes the spec (and the module's protocol), runs `tools/check_chip_spec.py`, prints the
   sizing summary, exports the display-model STLs to `cad/display/<chip_id>/` and regenerates
   the module's runs. A design that breaks a hard rule (e.g. a mesh >= 0.4 x capsule diameter)
   is refused with the reason; anything the checker flags is printed. `--out path.json` tries a
   design without touching the repo's files.
3. **Look at the twin** (`python -m http.server 8000`, then `http://localhost:8000/twin/`): the
   module's view shows the new chip and its parameters panel shows the values used.

The natural-language tool calls the same generators: `chip/nl_to_spec/generators.py`.

## `placeholder: true`

Every file has a `"placeholder"` field. Set it to `true` while the file holds fake
numbers, and to `false` only when it holds the real thing. The twin shows a
"placeholder data" badge whenever it's true.

---

## 1. protocol.json (MJ + Kilian)

**What it is:** the assay as an ordered list of steps. Lives in `protocols/`.

**Top-level fields**

| Field | Meaning |
|---|---|
| `id` | Unique name, snake_case. Becomes part of run file names. |
| `name` | Human name, shown in the twin's dropdown. |
| `source` | Citation of the published protocol it's based on. |
| `placeholder` | `true` or `false` (see above). |
| `description` | Optional summary. |
| `steps` | The list of steps, in order. |

**A step can carry 25 fields in all, and 9 of them are required on every step**
(`schemas/protocol.schema.json`): `step`, `operation`, `reagent`, `volume_ul`, `duration_s`,
`temp_c`, `washes`, `flow_ul_min` and `note`. Those 9 must always be *present* — use `null`
for the ones that don't apply. Everything else is optional and only appears on the
operations that need it (the table below marks them, and the per-operation table after it
says which are mandatory for which operation).

| Field | Meaning |
|---|---|
| `step` | 1, 2, 3 … in order |
| `operation` | one of `load`, `exchange`, `hold_temp`, `split`, `pool`, `release`, `valve`, `culture`, `collect`, `harvest`, `encapsulate`, `cure`, `mix`, `transfer`, `count` |
| `target` | the chamber ID the step acts on (see below for split and pool). Or give `targets`, a list, to act on several at once (an `exchange` with `targets` runs once per target; `reagents` gives one reagent per target) |
| `reagent` | generic reagent name, or `null` |
| `volume_ul` | µL, or `null` |
| `duration_s` | seconds the step takes (always a number) |
| `temp_c` | °C, or `null` |
| `washes` | how many fill-and-drain cycles, or `null` |
| `flow_ul_min` | µL/min, or `null` |
| `note` | one plain-English sentence; **this is what the audience reads** in the twin |
| `ports` | optional. Port state for the step: `{"feed": [port ids], "vent": [port ids], "cap": [port ids]}`, each list optional. A port appears in at most one list. A fed (driven) port is a pressure source, not a block; a vented port is the drain; only a capped port blocks. For chips that route by port, not by valve |
| *operation-specific* | the remaining optional fields, each used by the operations in the next table: `targets`, `reagents`, `source`, `valve_state`, `exchange_interval_s`, `factors`, `flows_ul_h`, `capsules`, `wavelength_nm`, `dilution`, `direction`, `waste`, `variant` |
| `cycles` | optional, `hold_temp` only. Thermocycling: `{"count": int ≥ 1, "ramp_c_s": number > 0, "profile": [{"temp_c", "duration_s"}, …]}` (non-empty). The profile repeats `count` times, ramping at `ramp_c_s` between holds |

**Which fields each operation needs** (the rest can be `null`):

| Operation | What happens | Must fill in |
|---|---|---|
| `load` | capsules come in through the inlet and settle in `target` | `volume_ul` |
| `exchange` | a reagent flows through `target` and drains, `washes` times; capsules stay on the mesh. Optional `"direction": "backflush"` pushes liquid up through the mesh instead, lifting the capsules off it | `reagent`, `volume_ul`, `washes`, `flow_ul_min` |
| `hold_temp` | `target` is held at a temperature | `temp_c`, `duration_s` > 0 |
| `split` | capsules leave `target` and are shared between the split chambers it connects to | nothing extra |
| `pool` | capsules from every chamber connected into `target` come together in `target` | nothing extra |
| `release` | the product leaves the capsules in `target` and flows to the outlet | `reagent` |
| `valve` | open or close the valves listed in `targets` | `targets`, `valve_state` (`open`/`closed`) |
| `culture` | the chambers in `targets` stay in culture: every `exchange_interval_s` a batch of fresh medium (`volume_ul` at `flow_ul_min`) flows through each, then the flow stops until the next change | `targets`, `reagents` (one medium per target), `exchange_interval_s`, `duration_s`, `temp_c`; optional `factors` (one per target, through each lane's factor port) |
| `collect` | supernatant leaves each chamber in `targets` through its outlet; the cells stay | `targets`, `volume_ul` |
| `harvest` | the cells leave each chamber in `targets` through its outlet | `targets`, `reagent` |
| `encapsulate` | core and shell streams co-flow into the nozzle `target`; oil pinches off droplets that become core-shell capsules | `flows_ul_h` (`{core, shell, oil}`), `capsules` |
| `cure` | light-cures the capsules in `target` through a curing window | `wavelength_nm`, `duration_s` > 0 |
| `mix` | a reagent is mixed into the cell stream in the mixer `target`, optionally diluting it | `reagent`; optional `dilution` (a number, or `"auto"`: the Module 1b operator sets it from the last count) |
| `transfer` | the cells move from `source` to `target` (a chamber or an outlet port), through any filter or window between | `source`, `target` |
| `count` | the QC window `target` reads the cell concentration and viability | nothing extra |

An `exchange` can name the port it drains to with `"waste"` (e.g. Module 2's dedicated oil
waste). `hold_temp` can target an off-chip stage (Module 2's cold delay, `m2_cold_delay_1`).

A step with `"variant": "interaction"` runs only when the run's `variant` is `interaction`
(Module 1 is one chip spec run twice, as `control` and `interaction`; see `run_parameters`).
Skipped steps are logged in the run file.

**Worked example:** step 2 of `placeholder_scrnaseq.json`:

```json
{ "step": 2, "operation": "exchange", "target": "exchange_1", "reagent": "wash_buffer",
  "volume_ul": 100, "duration_s": 180, "temp_c": null, "washes": 3, "flow_ul_min": 100,
  "note": "Wash the capsules three times with wash buffer to clear the loading liquid." }
```

Read it as: "In chamber `exchange_1`, wash with 100 µL of wash buffer, three times, at
100 µL/min; this takes 180 s." The validator checks the physics: 3 × 100 µL at
100 µL/min needs at least 180 s, so a `duration_s` of 60 would get a warning.

## 2. chip_spec.json (Yuvraj + Szonja)

**What it is:** the chip's geometry. Lives in `chip/`.

**Coordinates:** millimetres, origin at the **bottom-left corner** of the chip
footprint, **x** to the right along the chip's long edge, **y** across it, **z** up.
Ayrton's CAD uses the same origin, so his parts drop straight in.

| Field | Meaning |
|---|---|
| `id` | e.g. `placeholder_chip_v1` |
| `units` | always `"mm"` |
| `footprint` | `{"x": 30, "y": 20}`: the build envelope. Anything larger is an **error**, not a warning (`tools/check_chip_spec.py`). |
| `layers` | material layers **bottom to top**, each `{id, material, thickness_mm}`. The first starts at z = 0, each next one sits on top of the last. |
| `chambers` | each `{id, type, center: [x,y,z], size: [x,y,z], block, mesh_pore_um, volume_ul, open_top}`; `type` is `inlet`, `exchange`, `split`, `pool`, `outlet` or `well`. `size[2]` (the depth) may not exceed 0.3 mm. |
| `channels` | each `{id, from, to, width_mm, depth_mm, block, open_top}`: liquid flows **from → to**. `width_mm` ≥ 0.2, `depth_mm` ≤ 0.3. |
| `ports` | where tubing connects: each `{id, type: inlet or outlet, position: [x,y,z]}` on the top surface. At most **6 per chip**, at least 5 mm apart. |

### `block`: the project's central constraint

**Every chamber and every channel must name a `block`**, and that block must exist in
[`contracts/blocks.json`](blocks.json). This is the one rule most likely to reject your
design, so read it first.

The CAD tool offers a **fixed library of geometry primitives** and we can place nothing else.
So a chip spec is not free-form geometry: it is an assembly of named blocks. `blocks.json`
lists them under `basic` (channels, junctions, chambers, ports, manifolds) and `advanced`
(pillar arrays, traps, mixers, sorters, droplet and gradient features).

```json
{ "id": "ch_1", "from": "inlet_1", "to": "exchange_1",
  "width_mm": 0.2, "depth_mm": 0.3, "block": "straight_channel" }
```

- A `block` **not** in `basic` or `advanced` is a hard **error**.
- A chamber or channel with **no** `block` field is a **warning**: "we cannot fabricate what
  we cannot name". Older specs stay valid, but new ones should name it.
- `block` says *what shape to build*; `type` says *what the protocol does with it*. They are
  independent: `exchange_1` has `"type": "exchange"` and `"block": "trap_chamber"`.
- The `excluded` section matters as much as the included ones: **all 15 multilayer blocks are
  unavailable**, because we print one fluidic layer. That is why no chip may route two
  channels across each other, and why on-chip valves are out.
- A few listed blocks carry warnings — e.g. `tesla_valve` is useless below Re 1 (diodicity is
  exactly 1.0 in Stokes flow). Read the block's one-line description before using it.

`blocks.json` also holds the build envelope, the fabrication floors and the retention rules;
`tools/check_chip_spec.py` and `tests/test_envelope.py` enforce all of it. It is the source of
truth — change a limit there, not in a chip spec.

**Worked example:** the exchange chamber of the placeholder chip:

```json
{ "id": "exchange_1", "type": "exchange", "center": [7.019, 5.267, 3.5],
  "size": [3.893, 3.115, 0.3], "block": "trap_chamber",
  "mesh_pore_um": null, "volume_ul": 3.638,
  "retention": {"type": "weir", "opening_um": 25, "source": "fabricated"} }
```

Read it as: "A 3.893 × 3.115 mm box, **0.3 mm deep**, centred 7.019 mm from the left edge and
5.267 mm up from the bottom edge, sitting 3.5 mm up in the fluidic layer; it is built from the
`trap_chamber` block and holds 3.638 µL. Capsules are held by a fabricated depth weir with a
25 µm gap under the lid."

Note what the numbers are *not*: there is no 2 mm-deep chamber and no 75 × 25 mm slide
anywhere. One 0.3 mm etch depth across the whole chip is the envelope, so a 2 mm chamber is
rejected outright; `mesh_pore_um` is `null` because retention is a weir, not a mesh.

**Open-top features.** Chambers and channels can set `"open_top": true` when they have no lid:
a well you pipette into, or a groove printed into the top surface. Leave it out (it defaults
to `false`) for anything sealed under a lid. Use chamber type `well` for open wells; for a
round well, `size` is its bounding box (diameter × diameter × depth) and `volume_ul` is the
real cylinder volume. Liquid in open features has a free surface, so flow there is driven by
level differences and surface tension rather than a pump. Any flow numbers we compute for
open features are **approximate**.

**Channels decide split and pool.** The operator works out where `split` sends capsules
by following the channels out of the protocol's target chamber, and where `pool`
collects from by following the channels into the pool chamber. So if you add
`split_5`, connect it with channels and it'll be used automatically.

### Design fields (optional)

These come from Szonja's sheet (`contracts/sources/chip_geometry_requirements.xlsx`).
All are optional, so older specs stay valid. **Yuvraj's design tool should target these
fields**: they're what `tools/check_chip_spec.py` checks the real-scale design against.

| Field | Meaning |
|---|---|
| `capsule.diameter_um`, `.swell_factor`, `.count` | The capsules the chip is designed for. `count` is **capsules, not cells**; use `operator/sizing.py --cells` to convert. |
| `trap.gap_um` | Target trap gap; must stay **strictly below** 0.4 × capsule diameter and at or above 0.3 × it (21–28 µm for 70 µm capsules; we use 25 µm). Unprintable *in plane*, but fabricable as a depth weir in z. |
| `capsule_filter.gap_um`, `cell_filter.gap_um` | Pre-filter gaps. The cell filter must be ≤ the encapsulation nozzle (`encapsulation.nozzle_um`). |
| `thermal_zones[]` | `{id, chambers, max_temp_c, min_temp_c, purpose}`; `purpose: "culture"` means capsules swell there. |
| `barcoding.lanes`, `.sublibraries` | Barcoding scheme; used for the barcode-collision check. Split-and-pool is dropped, so `.rounds` and `.chambers_per_round` are legacy and ignored: cells arrive carrying a per-lane barcode and reverse transcription adds none. |
| `chambers[].height_um`, `.sealed` | Internal height in µm; sealed against evaporation. |
| `chambers[].retention` | `{type: mesh/pillars/weir, opening_um, source: bought/fabricated}`: what holds the capsules. Current chips all use `{weir, 25, fabricated}`: the 200 µm floor is in-plane, and a weir's gap is in z, so it **is** fabricable. `pillars` cannot retain at a 200 µm throat. |
| `ports[].role`, `.capsule_passable`, `.width_um` | The sheet's `outlets[]`: which ports capsules can pass (a waste line below a mesh can't), and the opening width. |
| `module`, `generator` | Which MVP module the chip is (`{id: m1, name, status: designed/draft, printed}`), and the script + parameter values that generated it. |
| `run_parameters` | Named run parameters, e.g. `{"variant": {"values": ["control", "interaction"], "default": "control"}}`: one spec, several instances. |
| `chambers[].shape` | `box` (default) or `cylinder` (round chamber: `size` is `[diameter, diameter, depth]`). Types also include `culture`, `junction` (where channels meet), `bubble_trap`. |
| `channels[].carries` | `capsules`, `droplets`, `cells`, `liquid`, `oil` or `signals`: picks the design rule (capsules: ≥ 1.5 × diameter; cells: ≥ 50 µm). |
| `channels[].path` | Centreline waypoints `[[x, y], …]`; without it, channels are routed automatically. Channels with paths are checked for crossings. |
| `channels[].capsule_passable`, `.fabricable` | `capsule_passable: false` (MJ's proposal): deliberately too small for capsules or cells (microgrooves were this; they cannot be printed here). `fabricable: false`: drawn, but too small to make here. |
| `valves[]` | `{id, type: normally_closed/normally_open, kind: branch/pair/isolation, channel, position, control_port, connects}`. A `pair` valve can't be opened in a `control` run. Control ports are `ports` with `type: control`. Pneumatic on-chip valves are removed from current chips (they need a second layer). |
| `chambers[].mixer` | For `type: mixer`: `{kind: serpentine/wide_short, channel_width_mm, turns, length_mm}`. Types `cell_wash` (method TBC) and `qc_window` are Module 1b's other stages. |
| `chambers[].filter` | For `type: filter`: `{type: pillars/mesh, gap_um, purpose: cells/particles/capsule_aggregates}`. A filter whose purpose is `cells` must have a gap **smaller** than the encapsulation nozzle. |
| `encapsulation` | `{nozzle_um, nozzle_depth_um, nozzle, core_flow_ul_h, shell_flow_ul_h, oil_flow_ul_h, core_inlet}`: Module 1b uses these flows to set the cell concentration. |
| `stages[]` | Steps that happen off the chip between two of its ports: `{id, type: cold_delay, from_port, to_port, location: off_chip, temp_c, time_min, volume_ul, note}`. |
| `features[]` | Parts that aren't chambers, channels or ports: `{id, type: gas_window/heat_zone/cold_zone/curing_window/optical_window, center, size, covers, temp_c, wavelength_nm}`. |

**`contracts/parameters.json`** holds every number from the sheet with its units,
source keys (CapSeq, SPC-NAR, 10x, inDrops, TIP-seq) and an `assumption` flag for
rules of thumb that aren't from a paper. The sources table is in the same file.
**`contracts/materials.json`** lists **seven** chip materials — PMMA, printed resin, COC,
two grades of COP (high-Tg 1420R and low-Tg 1060R), PDMS and glass — each with aliases, max working
temperature, glass transition, optical transparency, `pcr_98c_status`, DNA adsorption and a
chemical-compatibility map over ~28 reagents. It is **mostly filled in**: COC, both COPs,
PDMS and glass each have only 8 reagents left as `TODO`. The gaps are concentrated in the two
materials we actually build with — **PMMA (all 27 `TODO`) and printed resin (26 of 27)** —
which is exactly why `tools/check_chip_spec.py` prints "compatibility TODO: PMMA with …"
against most chips. Those warnings are the remaining work, not a broken file.

### Pipelines: `protocols/pipeline_*.json`

A pipeline chains the modules (`"kind": "pipeline"`, schema `schemas/pipeline.schema.json`).
Each entry in `modules` has a `status` (`final`, `draft`, `translated`, `missing` or `bench`;
the last two are placeholders with no chip, every port `"TBC"`), an `owner`, its `chip` and
`protocol`, `build` (`method` and a `connector` id for its port style), `built_for_demo`, and
optionally `prefer` (`{"glob": ...}`: switch to that file as soon as it exists). `handoffs` say
which outlet feeds which inlet; `ports` lists several inlets at once (Module 4's four). Each
hand-off between two modules carries a `connector` block (`match`, `mismatch`, or `unknown`
when either side is TBC, with a `warning`: where the demo needs an adapter).

**Two assays.** `pipeline_capseq.json` is the plan: CapSeq, RNA, MJ's chain as it is — culture,
cell prep, encapsulation, lysis and RT in Module 3's trap, **per-cell barcoding in oil** (stage 4b,
`sctip_spc_v1`: each capsule goes into its own oil droplet, where one clonal 16-base barcode is
amplified and overlap-extended onto that capsule's cDNA), index PCR, release. Plate-based
split-and-pool was dropped (fcc6ef2) and its hairpin ligation with it, so there is **no
split-and-pool and no hairpin ligation** — but per-cell barcoding was not dropped, and stage 4b is
what makes the data single-cell; the lane barcode from Module 1 and Module 4's sublibrary index are
demultiplexing on top. `tools/build_assay_variants.py` refuses a base chain that has the dropped
placeholders back, and refuses one that has lost stage 4b. `pipeline_tipseq.json` is an alternative
kept for comparison (TIP-seq, chromatin DNA: a bench permeabilise + tagment placeholder,
`sctip_spc_v1` stages B and C, release before index PCR). Both are
built from MJ's chain by `tools/build_assay_variants.py --base <MJ's pipeline file>`.
`pipeline_mvp.json` is a copy of the chosen one (`"same_as"`, `"variant"`):

```
python tools/choose_assay.py tipseq     # switch; capseq is the default
```

Edit the chain, not the copy: `tools/validate.py` fails if `pipeline_mvp.json` drifts from its
variant. It also checks the files exist, no superseded chip is used, every named port exists
and runs outlet -> inlet, and the connector blocks agree with the modules' `build`; it lists TBC
ports and connector warnings.

**Port overlap** (`tools/check_chip_spec.py`, an error): a port's lid hole (its `width_um`, else
`blocks.json`'s `min_port_mm` bore) must open onto the cut channel or chamber under it by at
least 1 mm², or a quarter of the hole when the hole is under 2 × 2 mm (a 0.8 mm bore holds only
0.64 mm²). It measures the exported DXF when there is one, else the spec. Fix a failure by
running the channel across the hole or adding a pad under it.

## 3. commands: the chip's instruction set

The operator (planner or AI) turns each protocol step into commands.
This is everything the chips can do:

| Command | Arguments |
|---|---|
| `load` | `target`, `volume_ul` |
| `exchange` | `target`, `reagent`, `volume_ul`, `washes`, `flow_ul_min`, optional `direction` (`forward`/`backflush`) |
| `hold_temp` | `target`, `temp_c`, `duration_s` |
| `split` | `source`, `targets` (a list) |
| `pool` | `sources` (a list), `target` |
| `release` | `target`, `reagent` |
| `valve` | `valves` (a list), `state` (`open`/`closed`) |
| `culture` | `targets`, `reagents`, `duration_s`, `exchange_interval_s`, `volume_ul`, `flow_ul_min`, `temp_c`, optional `factors` |
| `collect` | `targets`, `volume_ul` |
| `harvest` | `targets`, `reagent` |
| `encapsulate` | `target` (nozzle), `core_flow_ul_h`, `shell_flow_ul_h`, `oil_flow_ul_h`, `capsules` |
| `cure` | `target`, `wavelength_nm`, `duration_s` |
| `mix` | `target`, `reagent`, optional `volume_ul`, `flow_ul_min`, `dilution` |
| `transfer` | `source`, `target` (chamber or outlet port) |
| `count` | `target` (a QC window) |

Example: `{"command": "split", "args": {"source": "exchange_1", "targets": ["split_1", "split_2", "split_3", "split_4"]}}`

Adding a command means changing `schemas/commands.schema.json`, the operator and the
twin together. Talk to Bruno first.

## 4. run.json: what the twin plays

**What it is:** a recording of the commands an operator sent, in time order. Lives in
`runs/`, named `<protocol_id>__<chip_id>.json`. Nobody writes these by hand:
`python operator/run.py` makes them. The twin **only** reads run files, so a recorded
run always works offline. That's our fallback for the live demo.

| Field | Meaning |
|---|---|
| `protocol_id`, `protocol_name` | which protocol |
| `chip_id`, `chip_spec` | which chip, and the path of its spec file |
| `operator` | `planner` (deterministic) or `llm` (Claude) |
| `created_at` | when it was made |
| `placeholder` | `true` if made from any placeholder file |
| `total_duration_s` | total protocol time |
| `events` | the commands, each with `t_s` (start time), `duration_s`, `command`, `args`, `narration`, `protocol_step` |

Example event:

```json
{ "t_s": 60, "duration_s": 180, "command": "exchange",
  "args": {"target": "exchange_1", "reagent": "wash_buffer", "volume_ul": 100, "washes": 3, "flow_ul_min": 100},
  "narration": "Wash the capsules three times with wash buffer to clear the loading liquid.",
  "protocol_step": 2 }
```

`runs/index.json` lists every run for the twin's dropdown; `run.py` keeps it up to date.

## 5. STL files (Ayrton)

One STL per part, **named exactly by its ID**: `exchange_1.stl`, `split_1.stl` …, in
millimetres, using the chip spec's origin. Details in `cad/README.md`.

---

## Changing a contract

The contracts are what let six people work in parallel, so change them deliberately:
1. Talk to Bruno and whoever uses the file.
2. Change the schema in `contracts/schemas/` and this README in the same commit.
3. Update the placeholders so `python tools/validate.py` still passes.

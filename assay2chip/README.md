# assay2chip: wet-lab assay in, printable chips out

The repo already does the **second half**: a design file that groups protocol steps into
library parts becomes a chip spec, placed STLs and a Fusion script
(`chip/designs/README.md`, `chip/components/build_chip.py`).

This directory is the **first half plus the verification tail**, so the whole thing runs
end to end from a paper or a protocol document with no human in the middle.

```
  paper / protocol  (PDF, markdown, text, DOI)
        │
  S0 ingest      ──►  assay2chip/work/<id>/raw.json          text + tables + provenance
        │
  S1 extract     ──►  assay2chip/work/<id>/assay.json        ordered wet-lab steps
        │
  S2 normalise   ──►  assay2chip/work/<id>/unitops.json      canonical unit operations
        │
  S3 triage      ──►  (same file, `placement` per op)        on_chip | off_chip | missing_part
        │
  S4 partition   ──►  assay2chip/work/<id>/partition.json    which ops go on which chip
        │
  S5 group       ──►  chip/designs/<chip_id>.json            EXISTING format, one per chip
        │
  S6 build       ──►  chip/<chip_id>.json (unrouted)         parts SIZED FROM PHYSICS, placed
        │
  S7 route       ──►  chip/<chip_id>.json (routed)           planar channels, no crossings
        │
  S8 protocol    ──►  protocols/<chip_id>.json               flows from the BUILT geometry
        │
  S9 verify      ──►  validate + check_chip_spec + figures   must be 0 errors
        │
  S10 cfd        ──►  assay2chip/work/<id>/cfd/…             end-to-end flow simulation + fixes
        │
  S11 fabricate  ──►  cad/exports/<chip_id>/ + print/       STL, mould, closed DXF, renders
        │
  S12 report     ──►  assay2chip/work/<id>/report.md + .json problems, fixes, artefacts
```

Stage files are `assay2chip/stages/s<N>_<name>.py`, run in numeric order. S10 can send a
chip back to S6/S7 when an automatic fix changes geometry; that loop is bounded and logged.

### Bench volumes do not transfer - translate what scales, not what was written

A paper says "wash the pellet in 500 uL" or "lyse in 200 uL". Those numbers describe a
TUBE, not the assay. Building a chamber that holds 500 uL is wrong, and so is refusing a
0.012 uL step as unbuildable. The first end-to-end run failed at S6 on exactly this.
The translation rules, applied in S2 (record them), S6 (size from them) and S8 (protocol
volumes from them):

| Quantity in the paper | What survives onto the chip |
|---|---|
| cell number, cells per area, cells per volume | **kept** - it sets the chamber: adherent cells -> floor area at the paper's surface density; suspended cells, capsules or beads -> packed volume (contracts: packing fraction, capsule diameter) |
| reagent volume for a wash or rinse | **dropped** - becomes a SWEEP of the chamber: `-ln(1 - exchange.target_fraction)` chamber volumes |
| reagent volume added to a sample (lysis, enzyme, master mix) | **the RATIO is kept** (reagent : sample, so the final concentration matches the paper); the absolute volume follows from the chamber |
| incubation time, temperature | **kept** - unless diffusion into a gel or capsule needs longer, which then wins (precedent: the barcoding trap budgets 5 min of soak per wash) |
| centrifuge speed and time, pipetting, vortexing | **dropped** - replaced by the on-chip mechanism S3 chose (weir flush, sedimentation, herringbone mixing) |
| a volume below the smallest buildable chamber | **rounded UP** to the smallest chamber the floors allow, with a note; never refused |

`unitops.json` should therefore carry the LOAD (count, area or packed volume) and the
RATIOS, with the paper's absolute volume kept only in `audit` for traceability. A step
whose cell number the paper never states is an `unresolved` question, and the chamber is
sized from a stated contract default, flagged - never from the tube volume.

### Parts are sized from physics, not copied from the Flowmatter STLs

`chip/components/catalogue.json` (Yuvraj's Flowmatter library) is a REFERENCE: its parts are
1.0 mm deep and up to 105 x 100 mm, so placing them as-is cannot satisfy the envelope -
that is exactly why the first example failed. S6 therefore sizes every part parametrically
from what the op needs (volume, residence time, particle size, shear ceiling) using the
**block ids in `contracts/blocks.json`**, which is the contract vocabulary every chip spec's
`block` field already uses. `assay2chip/parts/` holds that registry. The Flowmatter STLs
remain useful for display and as the shape reference for each block.

The mature path in this repo - chip spec → `tools/validate.py`, `tools/check_chip_spec.py`,
`cad/make_figures.py`, `cad/make_train_cad.py` - is the one S6-S11 must produce for. A chip
this pipeline makes must pass exactly the checks a hand-designed chip passes. Note
`check_chip_spec.py` currently reports 60 pre-existing "port overlap" errors on main (a new
rule: a 0.8 mm port hole must open onto enough cut channel, fixed with a port pad under
it). Those are not ours, but every chip the pipeline generates must pass that rule too.

Run it:

```
python assay2chip/run.py --paper path/to/protocol.pdf --id my_assay
python assay2chip/run.py --id my_assay --from s4        # resume at a stage
```

Every stage is a module in `assay2chip/stages/`, reads the previous stage's file, writes
its own, and is independently runnable. No stage mutates an earlier stage's output.

## Why this is not just "ask a model to design a chip"

Three hard constraints make this an engineering problem rather than a prompt, and every
stage has to respect them. They come from `contracts/blocks.json`, which is the authority.

1. **The fabrication envelope.** Demo is 30 x 20 mm, the design target is the ANSI/SLAS
   microplate footprint 127.76 x 85.48 mm, and the agreed ceiling is 290 x 190 mm. Max
   depth 0.3 mm. ONE fluidic layer at ONE etch depth, so **no channel may cross another
   in plan view** - a crossing is a short circuit. In-plane features >= 200 um, walls
   >= 0.4 mm, ports >= 0.8 mm at >= 5 mm pitch.
2. **The library parts do not currently fit.** `chip/components/catalogue.json` parts are
   1.0 mm deep and some are enormous - `spiral_channel` is 104.8 x 99.8 mm. That is why
   the first example came out 317 x 116 mm, 1 mm deep, 12 ports, and failed the contract.
   Parts must be re-cut to the single etch depth and fitted to the envelope (see
   `tools/fit_chip.py`), or the design must be split across more chips.
3. **One assay does not fit on one chip.** Port count, area, incompatible temperatures and
   cross-contamination all force a split. Deciding *where* to cut, and what carries
   between the pieces, is stage S4 and is the heart of this pipeline.

## Foundation API (built - use it, do not open stage files yourself)

```python
from assay2chip.lib.io import read_stage, write_stage, StageValidationError
data = read_stage(assay_id, "assay")          # kinds: raw assay unitops partition report
path = write_stage(assay_id, "unitops", data) # validates against the schema, atomic write

from assay2chip.lib import contracts          # missing key -> ContractError, never a default
contracts.envelope("demo" | "target" | "ceiling"); contracts.max_depth_mm()
contracts.fabrication_floors(); contracts.floor(name); contracts.max_ports_per_chip(scale)
contracts.shear_ceiling_for(shear_class)      # adherent_cells | prelysis_rinse | capsules | none
contracts.capsule_diameter_um(); contracts.swollen_capsule_diameter_um()
contracts.escape_pressure_kpa(); contracts.max_working_pressure_kpa()
contracts.viscosity_for(temp_c)               # 37 C -> 0.0008; 20 C water is the dye demo only
contracts.barcode_random_bases(); contracts.param(key)

from assay2chip.lib import llm                # llm.complete_json(..., task=, stub=); llm.provenance()
```

A stage is `assay2chip/stages/s<N>_<name>.py` exposing `run(**kwargs)` (accepts any of
`ctx, assay_id, paper, llm_mode, force`), a `WRITES = ["chip/designs/{chip_id}.json", ...]`
list for anything outside the work dir, and may return
`{"rerun_from": "s6", "reason": "..."}` for a bounded re-run (cap 3, logged).
Every schema object accepts an optional `note`, and items carry an open `audit` pocket for
provenance. `quote_page` is a page number or a locator string, never null.

## Stage contracts

These shapes are authoritative. `contracts/schemas/` holds the formal JSON Schema for each.

### S0 `raw.json`
```json
{ "id": "my_assay", "source": {"kind": "pdf|md|txt|doi|url", "ref": "...", "sha256": "..."},
  "title": "...", "blocks": [ {"kind": "heading|paragraph|table|list", "text": "...", "page": 3} ] }
```

### S1 `assay.json` - what the paper says, still in lab language
```json
{ "id": "my_assay", "source": {...},
  "steps": [ { "step": 1, "text": "verbatim sentence from the source",
               "action": "add|incubate|wash|centrifuge|mix|transfer|image|lyse|amplify|separate|seed|harvest",
               "reagents": [ {"name": "generic name only", "volume_ul": 100, "role": "medium|buffer|enzyme|solvent|oil|label"} ],
               "target": "what it is done to", "duration_s": 600, "temp_c": 37,
               "confidence": 0.0, "quote_page": 3, "notes": "ambiguity, if any" } ],
  "unresolved": [ {"step": 4, "question": "volume not stated"} ] }
```
Confidence and `unresolved` are mandatory. A guessed number that reads as fact is the
failure mode this whole repo has been fighting; never emit one.

### S2/S3 `unitops.json` - canonical operations plus the on-chip decision
```json
{ "id": "my_assay",
  "ops": [ { "op": 1, "from_steps": [1,2], "kind": "merge|mix|incubate|wash|separate|lyse|thermocycle|split|pool|collect|image|encapsulate|cure",
             "fluid": {"volume_ul": 100, "viscosity_class": "aqueous|viscous|organic|oil"},
             "particles": {"kind": "none|cells|capsules|beads", "diameter_um": 12},
             "duration_s": 600, "temp_c": 37, "shear_class": "adherent_cells|capsules|prelysis_rinse|none",
             "placement": { "where": "on_chip|off_chip|missing_part",
                            "part": "catalogue part name, when on_chip",
                            "why": "one sentence, always",
                            "need": "what part would be required, when missing_part" } } ] }
```

### S4 `partition.json` - the multi-chip plan
```json
{ "id": "my_assay",
  "chips": [ { "chip_id": "my_assay_c1", "ops": [1,2,3], "scale": "demo|target",
               "reason": "why these ops are together" } ],
  "handoffs": [ { "from": "my_assay_c1", "from_port": "outlet_1",
                  "to": "my_assay_c2", "to_port": "inlet_1",
                  "carries": "cells|capsules|library|supernatant",
                  "volume_ul": 40, "particle_um": 70,
                  "automated": true, "how": "tubing + selector valve",
                  "risks": ["dead volume", "sedimentation in the line"] } ],
  "cuts": [ {"between": [3,4], "why": "98 C thermocycling cannot share a board with live cells"} ] }
```
A handoff is only `automated: true` if a machine can do it. Pipetting is `false`, and the
report must count them, because "no human intervention" is the stated goal.

### S9 `cfd/*.json` - see `assay2chip/cfd/README.md`

## Rules every stage obeys

- **Never invent a number.** Carry `confidence` and `unresolved` forward. If a stage needs
  a value the source never gave, it records the gap and uses a contract default, flagged.
- **Read contracts, do not restate them.** Envelopes, floors, shear ceilings, viscosity and
  capsule sizes come from `contracts/blocks.json` and `contracts/parameters.json`. A literal
  that duplicates a contract value is a bug; this repo has been bitten by it repeatedly.
  Viscosity: use `fluid.viscosity_37c_pa_s` for anything at 37 C, not `fluid.viscosity_pa_s`,
  which is water at 20 C and belongs to the dye demo.
- **Confidentiality** (`CONFIDENTIALITY.md`): an ingested paper may legitimately contain
  concentrations and product names, so `raw.json` and `assay.json` may hold what the source
  said, cited. But **nothing from the lab's Labstep or OneDrive** may enter the repo, and
  anything written into a committed chip spec, protocol or doc uses generic reagent names
  with no concentrations, vendors or prices.
- **Model calls are optional.** S1 and S5 are better with a language model, but each must
  have a deterministic fallback and must run offline for tests. Keep the model behind one
  interface so it can be stubbed.

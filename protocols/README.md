# protocols/ (owners: MJ + Kilian)

Each assay protocol as a `protocol.json` file: an ordered list of steps, plus the physics
numbers: volumes, flow rates, times, temperatures.

`contracts/schemas/protocol.schema.json` allows **fifteen** operations, not six:
`load`, `exchange`, `hold_temp`, `split`, `pool`, `release`, `valve`, `culture`,
`collect`, `harvest`, `encapsulate`, `cure`, `mix`, `transfer`, `count`. The schema is
the authority; if you need an operation that isn't in that list, add it there first.

The format is explained, with examples, in [`contracts/README.md`](../contracts/README.md#1-protocoljson-mj--kilian).

## Files

| File | What |
|---|---|
| [`ipsc_to_mgl_v1.json`](./ipsc_to_mgl_v1.json) | **Real.** iPSC to microglia-like cells, 109 steps, 47 days, end to end on `chip/ipsc_to_mgl_chip_v1.json` with nothing handled off-chip |
| [`build_ipsc_to_mgl.py`](./build_ipsc_to_mgl.py) | Generates the above. Computes every flow rate from chamber geometry; `--report` prints the shear table |
| [`ipsc_to_mgl_design.md`](./ipsc_to_mgl_design.md) | Why that protocol, why those chamber sizes, the physics, and the honest limits |
| [`module1_v1.json`](./module1_v1.json) | **Real.** Three lineages (neurons, glia, astrocytes) on `chip/module1_v1.json`, routed only by which port is pressurised: no valves, no barriers |
| [`build_module1.py`](./build_module1.py) | Generates the above |
| [`module1_design.md`](./module1_design.md) | Why this design and the honest limits |
| [`cell_prep_v1.json`](./cell_prep_v1.json) | **Real.** Pipeline stage 2 on `chip/cell_prep_v1.json` |
| [`build_cell_prep.py`](./build_cell_prep.py) | Generates the above |
| [`cell_prep_design.md`](./cell_prep_design.md) | Why four of the six cell-prep steps are *not* on the chip |
| [`spc_demo_v1.json`](./spc_demo_v1.json) | **Real.** The dye droplet demo on `chip/spc_demo_v1.json`. Read its design note: it is **not** the pipeline's encapsulator and does not supply the pipeline |
| [`build_spc_demo.py`](./build_spc_demo.py) | Generates the above; `--report` prints geometry, flows, rate and run time |
| [`module3_v1.json`](./module3_v1.json) | **Real.** Stage 4 (load, lysis, wash, RT, wash, release) on `chip/module3_v1_translated.json` |
| [`build_module3.py`](./build_module3.py) | Generates the above |
| [`module4_v1.json`](./module4_v1.json), [`module5_v1.json`](./module5_v1.json) | **Real.** Stages 5 and 6: sublibrary split and index PCR, then release, bead cleanup and library out |
| [`build_library_out.py`](./build_library_out.py) | Generates **both** of the above from one set of numbers |
| [`module5_design.md`](./module5_design.md) | Design note for modules 4 and 5 (steps 15–17), both chips in one file |
| [`module6_v1.json`](./module6_v1.json) | **Real.** The bulk arm: one cold extraction of adherent cells giving metabolome and proteome |
| [`multiomic_design.md`](./multiomic_design.md) | Why the bulk arm extracts from adherent cells and not capsules, and what the transcriptome arm keeps |
| [`sctip_spc_v1.json`](./sctip_spc_v1.json) | Capsule generation and single-copy barcoding. **Partly retired and not fabricable as drawn** |
| [`build_sctip_spc.py`](./build_sctip_spc.py) | Generates the above |
| [`sctip_spc_design.md`](./sctip_spc_design.md) | That design, what it fixes, and why it is not fabricable as drawn |
| [`retention_audit.md`](./retention_audit.md) | What the weir actually holds: capsules yes, bare cells no. Three suspension-cell steps are unsolved |
| [`thermal_map.md`](./thermal_map.md) | Every thermal step from cell prep to finished library, and what hardware each needs |
| [`pipeline_status.md`](./pipeline_status.md) | One table of every pipeline stage: which chip wins, which duplicate is retired, which stage is RT only (no barcoding) or off-chip. Read this first. |
| [`pipeline_mvp.json`](./pipeline_mvp.json) | The pipeline manifest: stages and hand-offs, checked by `tools/validate.py` |
| [`m1_culture.json`](./m1_culture.json), [`m1b_cellprep.json`](./m1b_cellprep.json), [`m2_encapsulation.json`](./m2_encapsulation.json), [`m3_wash.json`](./m3_wash.json) | **Retired.** Protocols for the `cad/generators/` chips, superseded by the `module*` and `cell_prep` files above. None is marked retired in the file itself — see `pipeline_status.md` |
| [`placeholder_scrnaseq.json`](./placeholder_scrnaseq.json) | Generic scRNA-seq shape, 14 steps |
| [`placeholder_scdnaseq.json`](./placeholder_scdnaseq.json) | Generic scDNA-seq shape with two heat steps, 15 steps |
| [`placeholder_culture_then_scrnaseq.json`](./placeholder_culture_then_scrnaseq.json) | Long media perfusion and culture first, then scRNA-seq, 15 steps |

Every chip is **demo scale**, 30 × 20 mm, one fluidic layer, resin-printed and hot-embossed
(the design target is the 127.76 × 85.48 mm microplate footprint — see
[`chip/README.md`](../chip/README.md#scale-these-are-demo-chips-not-the-real-device)).

Capsule retention is a **depth weir fabricated on the same single layer**. The 200 µm
fabrication floor is **in plane only** and z is unconstrained, so a crest about 275 µm tall
under a 300 µm lid leaves a 25 µm gap in z; every chip that retains capsules records
`retention: {type: weir, opening_um: 25, source: fabricated}`. A bought mesh insert and
sedimentation are fallbacks if the weir coupon fails, not the answer.

Values in the `placeholder_*` files are fake. They exist so the operator and twin can
be built before the real protocols are ready.

**Don't hand-edit any generated protocol** — anything with a matching `build_*.py` above.
Change the builder and re-run it, so the physics and the file stay in step.

## Replacing a placeholder with the real protocol

1. Copy a placeholder to a new file, e.g. `protocols/scrnaseq_v1.json`, and give it a
   new `id` (same as the file name is easiest).
2. Fill in the steps. Keep reagent names **generic** (`lysis_buffer`, not a product
   name). Read [CONFIDENTIALITY.md](../CONFIDENTIALITY.md) first: no recipes,
   primers, barcodes, enzymes, lab conditions or cell lines in this repo.
3. Set `"placeholder": false` only when every number is real and cleared.
4. Check it: `python tools/validate.py`
5. Make a run so the twin can show it:
   `python operator/run.py --protocol protocols/scrnaseq_v1.json --chip chip/placeholder_chip_spec.json`
6. **MJ reviews anything that touches the protocol before it's pushed.**

Hand-offs: `protocol.json` **Saturday 14:30**, physics numbers **Saturday 16:30**.

# protocols/ (owners: MJ + Kilian)

Each assay protocol as a `protocol.json` file: an ordered list of steps using the
six operations (load, exchange, hold_temp, split, pool, release), plus the physics
numbers: volumes, flow rates, times, temperatures.

The format is explained, with examples, in [`contracts/README.md`](../contracts/README.md#1-protocoljson-mj--kilian).

## Files

| File | What |
|---|---|
| `ipsc_to_mgl_v1.json` | **Real.** iPSC to microglia-like cells, 109 steps, 47 days, end to end on `chip/ipsc_to_mgl_chip_v1.json` with nothing handled off-chip |
| `build_ipsc_to_mgl.py` | Generates the above. Computes every flow rate from chamber geometry; `--report` prints the shear table |
| `ipsc_to_mgl_design.md` | Why that protocol, why those chamber sizes, the physics, and the honest limits |
| `tri_culture_v1.json` | **Real.** Three lineages in three isolated lanes of `chip/tri_culture_v1.json`, linked by microgroove barriers, barcoded per lane and pooled. 180 steps, 87 days |
| `build_tri_culture.py` | Generates the above. Staggers the three seeding days so lineages of 80, 40 and 35 days finish together; `--report` prints the schedule |
| `tri_culture_design.md` | What changed from the hand sketch and why: Tesla valves can't rectify at Re < 1, the barriers are microgrooves rather than valves, and the three lineages don't finish together |
| `placeholder_scrnaseq.json` | Generic scRNA-seq shape, 14 steps |
| `placeholder_scdnaseq.json` | Generic scDNA-seq shape with two heat steps, 15 steps |
| `placeholder_culture_then_scrnaseq.json` | Long media perfusion and culture first, then scRNA-seq, 15 steps |

Values in the `placeholder_*` files are fake. They exist so the operator and twin can
be built before the real protocols are ready.

Don't hand-edit `ipsc_to_mgl_v1.json`: change `build_ipsc_to_mgl.py` and re-run it, so
the physics and the file stay in step.

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

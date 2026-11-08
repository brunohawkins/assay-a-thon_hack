# Notes for coding agents

Read this first. It is short on purpose; the details are in the files it points to.

## Barcoding: no split-and-pool, no hairpin ligation - here is why

Bruno, this one is for your agent first: the split-and-pool barcoding and hairpin-ligation
modules were removed on purpose (agreed by Bruno and MJ). Do not add them back, not even as
placeholders.

- Only **plate-based** split-and-pool was dropped. **Per-cell** barcoding was not.
- Each capsule goes back into its **own oil droplet**. There, one random barcode from a pool of
  16 bases (4^16, about 4.3e9) is copied many times **inside that droplet** and joined onto
  that capsule's cDNA by overlap extension (AmpliDrop, doi:10.1101/2024.08.13.607508, on
  semipermeable capsules). The oil keeps the barcode from reaching other capsules.
- One capsule = one cell, so the data **is** single-cell.
- This is **stage 4b**, `protocols/sctip_spc_v1.json`, between Module 3 (RT) and Module 4
  (index PCR). It has to come after RT, because overlap extension needs cDNA already in the
  capsule.
- Hairpin ligation only existed to close the split-and-pool barcode. Overlap extension has no
  ligation step, so there is nothing for it to do.
- Lane labels and Module 4's four sublibrary indices are for demultiplexing. They do not give
  single-cell resolution, so never count them as the barcode space.
- `tools/build_assay_variants.py` raises an error if the base chain has no real barcoding stage
  instead of putting placeholders back. `tests/test_pipeline_variants.py` checks this.

Sources: `protocols/sctip_spc_design.md`, the `barcoding.*` entries in
`contracts/parameters.json` (start with `barcoding.scheme`), `protocols/pipeline_status.md`
stage 4b.

## The chain: never rebuild it from an older branch

A rebuild of CapSeq from an older branch once deleted three things that are deliberate and
current. Keep all three. If a source you are working from lacks one of them, that source is
stale.

- **Stage 4b**, the in-droplet barcoding above. Without it the assay is not single-cell. The
  data is **single-cell**, never "lane-level".
- **Module 6, the bulk-omics arm** (`module6_bulk_omics`, `chip/module6_v1.json`): metabolome
  and proteome from one cold extraction of adherent cells.
- **Module 4's outlet `out_sub`.** Without it the indexed library is sealed inside the chip.

`protocols/pipeline_status.md` is the current map of the chain. When it disagrees with
something else, it wins - or ask MJ.

## Hard rules

- **Confidentiality** (`CONFIDENTIALITY.md`): nothing from the lab's Labstep or OneDrive goes
  in this repo, whether copied, summarised or paraphrased. If in doubt, leave it out and ask MJ.
- **Contract values come from `contracts/` only.** Read them through `contracts/parameters.json`
  and the loaders. Never write a number in as a literal.
- **Anything at 37 °C uses `fluid.viscosity_37c_pa_s`.** `fluid.viscosity_pa_s` is water at
  about 20 °C, for the dye demo only.
- **The shear ceilings have scopes** (see `operator/safety.py`): `shear.culture_ceiling_dyn_cm2`
  for adherent cells, `shear.prelysis_rinse_ceiling_dyn_cm2` for a rinse right before
  lysis or quench, and for capsules the limit is the escape pressure
  (`shear.capsule_handling_note`). Using the wrong one is wrong in both directions.
- **Chips printed for the demo are frozen.** Six are being printed: `module1_v1`,
  `spc_demo_v1` (Module 2), `module3_v1_translated`, `module4_v1`, `module5_v1` and
  `module6_v1`. Their geometry and CAD exports must not change: don't edit or regenerate their
  specs or exports. Design improvements go into a new full-spec (TARGET) versioned file
  instead. `cell_prep_v1` and `sctip_spc_v1` are not printed.
- Before you finish, run `./.venv/bin/python -m unittest discover -s tests`,
  `tools/validate.py` and `cad/make_figures.py --all`.

# Pipeline status

Every chip: 30 × 20 mm, 0.3 mm deep, one fluidic layer, resin print and hot embossing, blocks from `contracts/blocks.json`. BUILT means a chip spec exists in that envelope, not that anything has been printed or run with cells.

| Stage | Chip | Protocol | Status |
|---|---|---|---|
| Culture | `module1_v1` | `module1_v1.json` | BUILT, wins (rescaled from 140 × 75 mm) |
| Culture | `m1_culture_v1` | `m1_culture.json` | DUPLICATE-RETIRE |
| Culture (separate iPSC-to-microglia experiment) | `ipsc_to_mgl_chip_v1` | `ipsc_to_mgl_v1.json` | BUILT |
| Cell prep | `cell_prep_v1` | `cell_prep_v1.json` | BUILT, wins |
| Cell prep | `m1b_cellprep_v1` | `m1b_cellprep.json` | DUPLICATE-RETIRE |
| Cell prep (dissociation in module1, steps 179-182) | `module1_v1` | `module1_v1.json` | DUPLICATE-RETIRE |
| Wash, clump filter, concentrate | tube + bought mesh | `cell_prep_design.md` | OFF-CHIP |
| Encapsulation | `spc_demo_v1` | `spc_demo_v1.json` | BUILT, wins (capsules about 260 µm) |
| Encapsulation | `m2_encapsulation_v1` | `m2_encapsulation.json` | DUPLICATE-RETIRE |
| Encapsulation, stage A | `sctip_spc_v1` | `sctip_spc_v1.json` | DUPLICATE-RETIRE (150 × 100 mm) |
| Cure | tube | `spc_demo_v1.json` step 6 | OFF-CHIP |
| Capsule trap, lysis, wash | `module3_v1_translated` | `module3_step1_3.json` (Szonja) | BUILT, wins (needs bought mesh) |
| Capsule trap | `m2_trap_1` | none | DUPLICATE-RETIRE |
| Capsule trap | `m3_wash_v1` | `m3_wash.json` | DUPLICATE-RETIRE |
| Reverse transcription (plain poly(dT), no barcode or UMI) | `module3_v1_translated` | `module3_v1.json` (in progress) | RT ONLY, runs in the capsule trap |
| Sublibrary split, index PCR | `module4_v1` | `module4_v1.json` | BUILT |
| Release, bead cleanup | `module5_v1` | `module5_v1.json` | BUILT |
| SPRI size selection | tube | none | OFF-CHIP, undocumented |
| QC (Qubit, TapeStation, qPCR) | instruments | none | OFF-CHIP, undocumented |

## Blunt notes

- **Split-and-pool barcoding is dropped.** Reverse transcription is the only step between Module 3 and Module 4: one isothermal hold at about 42-50 °C in the existing capsule trap. Index PCR in Module 4 still adds the sublibrary index (BC4).
- **CAVEAT, QUESTION FOR THE TEAM: the data is lane-level, not single-cell.** The barcodes the cells now carry are per lane (three, one per lineage), not per cell. With four sublibrary indices that gives about twelve distinguishable groups. If single-cell resolution is still wanted, something must still give each cell its own barcode. Decision needed.
- **Retention cannot be printed.** The floor is 200 µm, so capsule retention needs a bought mesh insert or sedimentation.
- **`spc_demo_v1` has a 0.2 mm orifice, not 0.1 mm.** Capsules are larger than the 135 µm downstream sizing assumes.
- **Module 1's protocol still contains dissociation steps** that duplicate `cell_prep_v1`. MJ owns that change.

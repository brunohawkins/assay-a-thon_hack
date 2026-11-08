# Pipeline status

Checked 7 Nov 2026. Every chip: 30 × 20 mm, one layer. BUILT means a spec and protocol exist, not that anything was printed or run. RT is un-barcoded and runs inside module3's trap; there is no barcoding module.

## Chain

| # | Stage | Chip | Protocol | Status |
|---|---|---|---|---|
| 1 | Culture, lane barcode, harvest | `module1_v1` | `module1_v1.json` | BUILT |
| 2 | Cell prep | `cell_prep_v1` | `cell_prep_v1.json` | BUILT |
| 2b | Spin, 40 µm strainer, resuspend | tube, mesh | `cell_prep_design.md` | OFF-CHIP |
| 3 | Encapsulation | `spc_demo_v1` | `spc_demo_v1.json` | BUILT |
| 3b | Break emulsion, wash oil, cure | tube | step 6 covers cure only | OFF-CHIP, HOLE |
| 4 | Load, lysis, wash, RT, wash, release | `module3_v1_translated` | `module3_v1.json` | BUILT (bought mesh) |
| 5 | Sublibrary split, index PCR | `module4_v1` | `module4_v1.json` | BUILT |
| 6 | Release, bead cleanup | `module5_v1` | `module5_v1.json` | BUILT |
| 7 | Pool, SPRI, QC | tube, instruments | none | OFF-CHIP, HOLE |

## Retired and duplicates

| Item | Replaced by | Retirement recorded? |
|---|---|---|
| `m1_culture_v1` | `module1_v1` | Yes, `superseded` |
| `m1b_cellprep_v1` | `cell_prep_v1` | Spec says `module1_v1`: wrong |
| `m3_wash_v1` | `module3_v1_translated` | Yes |
| `m2_trap_1` | `module3_v1_translated` | Chamber inside m2, no record of its own |
| `m2_encapsulation_v1` | `spc_demo_v1` | **No.** Spec says `draft`; READMEs only |
| `sctip_spc_v1` stage A | `spc_demo_v1` | Prose only |
| `sctip_spc_v1` stages B, C | `module3_v1_translated` | **No.** DUPLICATE trap, and the only chip claiming emulsion breaking |
| `module1_v1` dissociation (175-178) | `cell_prep_v1` | **No.** DUPLICATE, still in protocol. Steps 179-182 are still needed |

The pipeline points at no retired chip. Still pointing at them: 5 `m3_wash_*` and 3 `m1b_cellprep_*` scenarios, 15 runs, and five protocol files (`m1_culture`, `m1b_cellprep`, `m2_encapsulation`, `m3_wash`, `sctip_spc_v1`), none marked retired.

## Broken joins

| Join | Problem |
|---|---|
| 1 → 2 | Module 1 already lifts and quenches cells. `cell_prep_v1` steps 1-3 expect an adherent layer and dissociate again |
| 2 → 3 | Hand spin, strainer, resuspension (documented). Cell count is for 135 µm capsules, not 260 µm |
| 3 → 4 | Emulsion break and oil wash undocumented. Capsules about 260 µm; module3 built for 70 µm, 100 µm pre-filter gap. About 5,000 capsules made, trap sized for 650,000. `spc_demo_v1` is 0.15 mm deep, too shallow for a 260 µm sphere |
| 4 → 5 | Module3 releases 76.9 µL; module4 draws 9.6 µL at near-packed density. Concentration step undocumented. Valve and tubing off-chip |
| 5 → 6 | **Module4 has no capsule outlet**: `in_s1`-`in_s4` are inlets, `p_vent` blocks capsules. Port stays TBC. Reverse flow through the inlets and tubing undocumented |
| 6 → 7 | Pooling, ethanol size selection and QC by hand, no protocol |

**Holes:** no protocol for 3b and 7; no outlet port on `module4_v1`.

## Blunt notes

- **CAVEAT, QUESTION FOR THE TEAM: the data is lane-level, not single-cell.** The surviving barcodes are per lane (three, one per lineage). With four sublibrary indices that gives about twelve groups. If single-cell resolution is still wanted, something must give each cell its own barcode.
- **Retention cannot be printed** (200 µm floor): bought mesh or sedimentation. `ipsc_to_mgl_chip_v1` is a separate experiment, not in the chain.
- **`tests/test_pipeline.py`** needs a TBC port on a missing module. None remains, so one test errors.

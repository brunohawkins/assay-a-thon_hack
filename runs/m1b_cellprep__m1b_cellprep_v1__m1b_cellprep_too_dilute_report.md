# Run report: Module 1b: cell prep on m1b_cellprep_v1

> **SIMULATED HARDWARE.** The cell counter at the QC window is simulated; no chip was printed for Module 1b.

- Scenario: `m1b_cellprep_too_dilute`: Too few cells per uL: even undiluted they give well under 0.1 cells per capsule, so the safety checker blocks the hand-off and says how much to concentrate.
- Status: **stopped**
- Created: 2026-11-07T23:06:32Z

## Outcome

| | |
|---|---|
| Counted at the QC window | 1,086 cells/uL, 94.3% viable |
| Dilution set by the operator | 1x |
| Sent to Module 2 | 1,086 cells/uL -> 0.056 cells per capsule (target 0.1, i.e. ~1,949 cells/uL) |
| Safety checker | **blocked**: too dilute - concentrate 1.8x: 1,086 cells/uL gives 0.056 cells per capsule at Module 2's flows (target 0.1) |

## Steps

| Start | Step | Command | Duration | What happened |
|---|---|---|---|---|
| 0 s | 1 | load | 366 s | target=m1b_mix_1, volume_ul=1.089 |
| 366 s | 2 | mix | 366 s | target=m1b_mix_1, reagent=quench_medium, volume_ul=1.089, flow_ul_min=0.179 |
| 732 s | 3 | exchange | 300 s | target=m1b_cellwash_1, reagent=cell_wash_buffer, volume_ul=8.41, washes=2, flow_ul_min=5.399, waste=m1b_waste_1 |
| 1032 s | 4 | transfer | 120 s | source=m1b_cellwash_1, target=m1b_qc_window_1 |
| 1152 s | 5 | count | 30 s | target=m1b_qc_window_1 -> measured 1,086 cells/uL, 94.3% viable |
| 1182 s | 6 | mix | 120 s | target=m1b_resuspend_1, reagent=dextran_buffer, flow_ul_min=1.349, dilution=1.0, volume_ul=0.1 |

## Operator log (measurements, decisions, safety)

- `   1182 s` **measurement** (step 5): simulated counter at m1b_qc_window_1: 1,086 cells/uL, 94.3% viable
- `   1182 s` **decision** (step 6): m1b_resuspend_1 dilution set to 1x: counted 1,086 cells/uL, below the ~1,949 cells/uL Module 2 needs for 0.1 cells per capsule; dilution can't fix that, so no extra dilution (minimum buffer only)
- `   1302 s` **blocked** (step 7): cells to Module 2: blocked: too dilute - concentrate 1.8x: 1,086 cells/uL gives 0.056 cells per capsule at Module 2's flows (target 0.1)
- `   1302 s` **stop** (step 7): stopped before Module 2: the cells stay in m1b_resuspend_1 for a person to fix (too dilute - concentrate 1.8x: 1,086 cells/uL gives 0.056 cells per capsule at Module 2's flows (target 0.1))

# Run report: Module 1b: cell prep on m1b_cellprep_v1

> **SIMULATED HARDWARE.** The cell counter at the QC window is simulated; no chip was printed for Module 1b.

- Scenario: `m1b_cellprep_low_viability`: Too many dead cells: viability under 90%. Dilution can't fix that, so the safety checker blocks the hand-off to Module 2.
- Status: **stopped**
- Created: 2026-11-07T19:16:22Z

## Outcome

| | |
|---|---|
| Counted at the QC window | 3,554 cells/uL, 84.3% viable |
| Dilution set by the operator | 1.82x |
| Sent to Module 2 | 1,953 cells/uL -> 0.100 cells per capsule (target 0.1, i.e. ~1,949 cells/uL) |
| Safety checker | **blocked**: viability 84% is below 90% (10x guidance): don't encapsulate these cells |

## Steps

| Start | Step | Command | Duration | What happened |
|---|---|---|---|---|
| 0 s | 1 | load | 366 s | target=m1b_mix_1, volume_ul=1.089 |
| 366 s | 2 | mix | 366 s | target=m1b_mix_1, reagent=quench_medium, volume_ul=1.089, flow_ul_min=0.179 |
| 732 s | 3 | exchange | 300 s | target=m1b_cellwash_1, reagent=cell_wash_buffer, volume_ul=8.41, washes=2, flow_ul_min=5.399, waste=m1b_waste_1 |
| 1032 s | 4 | transfer | 120 s | source=m1b_cellwash_1, target=m1b_qc_window_1 |
| 1152 s | 5 | count | 30 s | target=m1b_qc_window_1 -> measured 3,554 cells/uL, 84.3% viable |
| 1182 s | 6 | mix | 120 s | target=m1b_resuspend_1, reagent=dextran_buffer, flow_ul_min=1.349, dilution=1.82, volume_ul=41.0 |

## Operator log (measurements, decisions, safety)

- `   1182 s` **measurement** (step 5): simulated counter at m1b_qc_window_1: 3,554 cells/uL, 84.3% viable
- `   1182 s` **decision** (step 6): m1b_resuspend_1 dilution set to 1.82x: counted 3,554 cells/uL at 84% viability; Module 2 makes 1,948,836 capsules/h from 100 uL/h of core stream, so 0.1 cells per capsule needs ~1,949 cells/uL: diluting 1.82x with dextran buffer
- `   1302 s` **blocked** (step 7): cells to Module 2: blocked: viability 84% is below 90% (10x guidance): don't encapsulate these cells
- `   1302 s` **stop** (step 7): stopped before Module 2: the cells stay in m1b_resuspend_1 for a person to fix (viability 84% is below 90% (10x guidance): don't encapsulate these cells)

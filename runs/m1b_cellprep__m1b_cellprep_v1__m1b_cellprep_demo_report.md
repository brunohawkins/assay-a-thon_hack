# Run report: Module 1b: cell prep on m1b_cellprep_v1

> **SIMULATED HARDWARE.** The cell counter at the QC window is simulated; no chip was printed for Module 1b.

- Scenario: `m1b_cellprep_demo`: Healthy but concentrated cells: the operator dilutes them at m1b_resuspend_1 to ~0.1 cells per capsule and the safety checker lets them through to Module 2.
- Status: **done**
- Created: 2026-11-07T15:13:10Z

## Outcome

| | |
|---|---|
| Counted at the QC window | 4,048 cells/uL, 93.3% viable |
| Dilution set by the operator | 2.08x |
| Sent to Module 2 | 1,946 cells/uL -> 0.100 cells per capsule (target 0.1, i.e. ~1,949 cells/uL) |
| Safety checker | **approved**: 1,946 cells/uL -> 0.100 cells per capsule at Module 2's flows (target 0.1); viability 93% |

## Steps

| Start | Step | Command | Duration | What happened |
|---|---|---|---|---|
| 0 s | 1 | load | 180 s | target=m1b_merge_1, volume_ul=60 |
| 180 s | 2 | mix | 180 s | target=m1b_mix_1, reagent=quench_medium, volume_ul=60, flow_ul_min=20 |
| 360 s | 3 | exchange | 300 s | target=m1b_cellwash_1, reagent=cell_wash_buffer, volume_ul=50, washes=2, flow_ul_min=20, waste=m1b_waste_1 |
| 660 s | 4 | transfer | 120 s | source=m1b_cellwash_1, target=m1b_qc_window_1 |
| 780 s | 5 | count | 30 s | target=m1b_qc_window_1 -> measured 4,048 cells/uL, 93.3% viable |
| 810 s | 6 | mix | 120 s | target=m1b_resuspend_1, reagent=dextran_buffer, flow_ul_min=20, dilution=2.08, volume_ul=54.0 |
| 930 s | 7 | transfer | 60 s | source=m1b_resuspend_1, target=m1b_to_module2_1 |

## Operator log (measurements, decisions, safety)

- `    810 s` **measurement** (step 5): simulated counter at m1b_qc_window_1: 4,048 cells/uL, 93.3% viable
- `    810 s` **decision** (step 6): m1b_resuspend_1 dilution set to 2.08x: counted 4,048 cells/uL at 93% viability; Module 2 makes 1,948,836 capsules/h from 100 uL/h of core stream, so 0.1 cells per capsule needs ~1,949 cells/uL: diluting 2.08x with dextran buffer
- `    930 s` **safety** (step 7): cells to Module 2: approved: 1,946 cells/uL -> 0.100 cells per capsule at Module 2's flows (target 0.1); viability 93%

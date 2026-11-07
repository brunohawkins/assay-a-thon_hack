# Run report: Module 3: capsule wash on m3_wash_v1

> **SIMULATED HARDWARE.** This run used the virtual chip: no physical pump, valves or sensors. Values marked TRUE come from the simulation and could never be measured directly on real hardware.

- Scenario: `m3_wash_closed_loop`: Pressure sensor at the mesh and outlet absorbance on: the model is corrected as the run goes.
- Operator: **planner** · Adapter: `virtual_chip` · Loop: **closed**
- Sensors: pressure_kpa, outlet_absorbance
- Faults injected: none
- Status: **done** · Protocol time: 281.15 s
- Created: 2026-11-07T14:49:56Z

## Outcome

| | Believed (model + sensors) | TRUE (simulation) |
|---|---|---|
| Final exchange % (step 2) | 99.49% | 100.0% |
| Capsule recovery | 97.83% | 98.0% |
| Capsules collected | 9783 | 9800 |
| Capsules lost to waste | 0 | 0 |
| Capsules left on the mesh | 33 | 24 |
| Reagent used: capsule_suspension | 20.0 uL | 20.0 uL |
| Reagent used: resuspension_buffer | 46.7 uL | 46.7 uL |
| Reagent used: wash_buffer | 76.0 uL | 76.0 uL |
| Waste | 116.0 uL | 116.0 uL |

Alerts: **0** (none) · Recoveries: **0** · Model updates: **4**

## Belief vs truth drift

How far the operator's belief was from the simulated truth at the end of the run.

- Mesh resistance (model vs true): -23.1% at the start -> -5.2% at the end
- Chamber volume (model vs true): +11.1% at the start -> +1.1% at the end
- Final exchange %: off by 0.51 points
- Capsules collected: off by 17 capsules
- Capsules lost: off by 0 capsules
- Waste volume: off by 0.0 uL

## Steps

| Start | Step | Command as executed | Duration | Safety | Operator's explanation |
|---|---|---|---|---|---|
| 0.0 s | 1 | load 20 uL at 40 uL/min | 30.0 s | approved | Fastest safe option of 3 simulated (0 rejected): load 20 uL at 40 uL/min, predicted 30 s, 93.4% exchanged, peak 2.00 kPa. |
| 30.0 s | 2 | exchange 1 x 16 uL wash_buffer at 40 uL/min + 72 s soak | 24.0 s | approved | Fastest safe option of 16 simulated (7 rejected): exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak, predicted 192 s, 100.0% exchanged, peak 2.00 kPa. |
| 54.0 s | 2 | exchange 3 x 20 uL wash_buffer at 40 uL/min + 77 s soak | 167.1 s | approved | Re-planned after a model update: Fastest safe option of 16 simulated (4 rejected): exchange 3 x 20 uL wash_buffer at 40 uL/min + 77 s soak, predicted 167 s, 100.0% exchanged, peak 2.46 kPa. |
| 221.1 s | 3 | back-flush 1 x 20 uL resuspension_buffer at 60 uL/min | 20.0 s | approved | Fastest safe option of 8 simulated (5 rejected): exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min, predicted 20 s, 94.2% exchanged, peak -3.70 kPa. |
| 241.1 s | 4 | release 26.7 uL at 40 uL/min | 40.0 s | approved | Fastest safe option of 9 simulated (3 rejected): release 26.7 uL at 40 uL/min, predicted 40 s, 99.9% exchanged. |

## Alerts

None.

## Recoveries and operator interventions

None.

## Model updates

| t | Parameter | Old -> new | Why |
|---|---|---|---|
| 30.0 s | `mesh_resistance[m3_trap_1]` | 0.05 -> 0.0616 kPa per uL/min | pressure 2.58 kPa vs predicted 2.00 (+29%) |
| 48.0 s | `chamber_volume_ul[m3_trap_1]` | 7.35 -> 6.79 uL | outlet absorbance 0.913 after 16 uL: leftover old liquid -23% vs the model since the exchange began |
| 60.0 s | `chamber_volume_ul[m3_trap_1]` | 6.79 -> 7.001 uL | outlet absorbance 0.966 after 24 uL: leftover old liquid +16% vs the model since the exchange began |
| 237.1 s | `chamber_volume_ul[m3_trap_1]` | 7.001 -> 6.685 uL | outlet absorbance 0.953 after 20 uL: leftover old liquid -18% vs the model since the exchange began |

## Optimizer decisions

- t = 0.0 s, step 1 (plan): step 1: chose load 20 uL at 40 uL/min; predicted {'time_s': 30.0, 'reagent_ul': 20.0, 'final_pct': 93.42, 'interior_pct': 26.82, 'peak_kpa': 2.0}. 3 candidates, 0 rejected.
- t = 30.0 s, step 2 (plan): step 2: chose exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak; predicted {'time_s': 192.3, 'reagent_ul': 80.0, 'final_pct': 100.0, 'interior_pct': 95.0, 'peak_kpa': 2.0}. 16 candidates, 7 rejected (4 x predicted …, 3 x peak …).
- t = 54.0 s, step 2 (replan): step 2: re-planned the rest after a model update: exchange 3 x 20 uL wash_buffer at 40 uL/min + 77 s soak. 16 candidates, 4 rejected (4 x peak …).
- t = 221.1 s, step 3 (plan): step 3: chose exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min; predicted {'time_s': 20.0, 'reagent_ul': 20.0, 'final_pct': 94.25, 'interior_pct': 19.38, 'peak_kpa': -3.696}. 8 candidates, 5 rejected (5 x lifts …).
- t = 241.1 s, step 4 (plan): step 4: chose release 26.7 uL at 40 uL/min; predicted {'time_s': 40.0, 'reagent_ul': 26.7, 'final_pct': 99.91, 'interior_pct': 58.31, 'peak_kpa': 0.0}. 9 candidates, 3 rejected (3 x recovers …).

## Full operator log

- `    0.0 s` **plan**: step 1: chose load 20 uL at 40 uL/min; predicted {'time_s': 30.0, 'reagent_ul': 20.0, 'final_pct': 93.42, 'interior_pct': 26.82, 'peak_kpa': 2.0}
- `    0.0 s` **safety**: approved: load 20 uL at 40 uL/min
- `   30.0 s` **plan**: step 2: chose exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak; predicted {'time_s': 192.3, 'reagent_ul': 80.0, 'final_pct': 100.0, 'interior_pct': 95.0, 'peak_kpa': 2.0}
- `   30.0 s` **safety**: approved: exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak
- `   30.0 s` **model_update**: mesh_resistance[m3_trap_1]: 0.05 -> 0.0616 kPa per uL/min (pressure 2.58 kPa vs predicted 2.00 (+29%))
- `   48.0 s` **model_update**: chamber_volume_ul[m3_trap_1]: 7.35 -> 6.79 uL (outlet absorbance 0.913 after 16 uL: leftover old liquid -23% vs the model since the exchange began)
- `   54.0 s` **replan**: step 2: re-planned the rest after a model update: exchange 3 x 20 uL wash_buffer at 40 uL/min + 77 s soak
- `   60.0 s` **model_update**: chamber_volume_ul[m3_trap_1]: 6.79 -> 7.001 uL (outlet absorbance 0.966 after 24 uL: leftover old liquid +16% vs the model since the exchange began)
- `  221.1 s` **plan**: step 3: chose exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min; predicted {'time_s': 20.0, 'reagent_ul': 20.0, 'final_pct': 94.25, 'interior_pct': 19.38, 'peak_kpa': -3.696}
- `  221.1 s` **safety**: approved: exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min
- `  237.1 s` **model_update**: chamber_volume_ul[m3_trap_1]: 7.001 -> 6.685 uL (outlet absorbance 0.953 after 20 uL: leftover old liquid -18% vs the model since the exchange began)
- `  241.1 s` **plan**: step 4: chose release 26.7 uL at 40 uL/min; predicted {'time_s': 40.0, 'reagent_ul': 26.7, 'final_pct': 99.91, 'interior_pct': 58.31, 'peak_kpa': 0.0}
- `  241.1 s` **safety**: approved: release 26.7 uL at 40 uL/min

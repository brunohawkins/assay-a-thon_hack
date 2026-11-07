# Run report: Placeholder capsule wash (MVP) on wash_mvp_v1

> **SIMULATED HARDWARE.** This run used the virtual chip: no physical pump, valves or sensors. Values marked TRUE come from the simulation and could never be measured directly on real hardware.

- Scenario: `wash_mvp_sensor_fault`: Closed loop, but the pressure sensor sticks mid-run: detected as sensor_disagreement, distrusted, model takes over.
- Operator: **planner** · Adapter: `virtual_chip` · Loop: **closed**
- Sensors: pressure_kpa, outlet_absorbance
- Faults injected: sensor_fault {'sensor': 'pressure_kpa', 'mode': 'stuck', 'start_t_s': 90}
- Status: **done** · Protocol time: 283.4 s
- Created: 2026-11-07T13:08:19Z

## Outcome

| | Believed (model + sensors) | TRUE (simulation) |
|---|---|---|
| Final exchange % (step 2) | 100.0% | 100.0% |
| Capsule recovery | 97.84% | 98.32% |
| Capsules collected | 9784 | 9832 |
| Capsules lost to waste | 0 | 0 |
| Capsules left on the mesh | 33 | 24 |
| Reagent used: capsule_suspension | 20.0 uL | 20.0 uL |
| Reagent used: resuspension_buffer | 48.0 uL | 48.0 uL |
| Reagent used: wash_buffer | 68.0 uL | 68.0 uL |
| Waste | 108.0 uL | 108.0 uL |

Alerts: **1** (sensor_disagreement) · Recoveries: **0** · Model updates: **3**

## Belief vs truth drift

How far the operator's belief was from the simulated truth at the end of the run.

- Mesh resistance (model vs true): -23.1% at the start -> -5.2% at the end
- Chamber volume (model vs true): +11.1% at the start -> +5.8% at the end
- Final exchange %: off by 0.0 points
- Capsules collected: off by 48 capsules
- Capsules lost: off by 0 capsules
- Waste volume: off by 0.0 uL

## Steps

| Start | Step | Command as executed | Duration | Safety | Operator's explanation |
|---|---|---|---|---|---|
| 0.0 s | 1 | load 20 uL at 40 uL/min | 30.0 s | approved | Fastest safe option of 3 simulated (0 rejected): load 20 uL at 40 uL/min, predicted 30 s, 93.4% exchanged, peak 2.00 kPa. |
| 30.0 s | 2 | exchange 1 x 16 uL wash_buffer at 40 uL/min + 72 s soak | 24.0 s | approved | Fastest safe option of 16 simulated (7 rejected): exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak, predicted 192 s, 100.0% exchanged, peak 2.00 kPa. |
| 54.0 s | 2 | exchange 2 x 16 uL wash_buffer at 40 uL/min + 77 s soak | 48.0 s | approved | Re-planned after a model update: Fastest safe option of 16 simulated (4 rejected): exchange 3 x 20 uL wash_buffer at 40 uL/min + 77 s soak, predicted 167 s, 100.0% exchanged, peak 2.46 kPa. |
| 102.0 s | 2 | exchange 1 x 20 uL wash_buffer at 20 uL/min + 59 s soak | 119.4 s | approved | Fastest safe option of 16 simulated (4 rejected): exchange 1 x 20 uL wash_buffer at 20 uL/min + 59 s soak, predicted 119 s, 100.0% exchanged, peak 1.23 kPa. |
| 221.4 s | 3 | back-flush 1 x 20 uL resuspension_buffer at 60 uL/min | 20.0 s | approved | Fastest safe option of 8 simulated (5 rejected): exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min, predicted 20 s, 94.2% exchanged, peak -3.70 kPa. |
| 241.4 s | 4 | release 28 uL at 40 uL/min | 42.0 s | approved | Fastest safe option of 9 simulated (3 rejected): release 28 uL at 40 uL/min, predicted 42 s, 99.9% exchanged. |

## Alerts

- **t = 102.0 s, step 2: `sensor_disagreement`** (critical): pressure sensor reading is frozen (identical values, no noise). Evidence: `{'sensor': 'pressure_kpa', 'readings': [2.6174, 2.6174, 2.6174], 'predicted': [2.4642, 2.4642, 2.4642], 'mode': 'stuck'}`

## Recoveries and operator interventions

- t = 102.0 s: distrusting sensor pressure_kpa; falling back to the model for it (The pressure_kpa sensor disagrees with the model; ignore it and fall back to the model for that quantity.)

## Model updates

| t | Parameter | Old -> new | Why |
|---|---|---|---|
| 30.0 s | `mesh_resistance[trap_1]` | 0.05 -> 0.0616 kPa per uL/min | pressure 2.58 kPa vs predicted 2.00 (+29%) |
| 48.0 s | `chamber_volume_ul[trap_1]` | 7.35 -> 6.79 uL | outlet absorbance 0.913 after 16 uL: leftover old liquid -23% vs the model since the exchange began |
| 60.0 s | `chamber_volume_ul[trap_1]` | 6.79 -> 7.001 uL | outlet absorbance 0.966 after 24 uL: leftover old liquid +16% vs the model since the exchange began |

## Optimizer decisions

- t = 0.0 s, step 1 (plan): step 1: chose load 20 uL at 40 uL/min; predicted {'time_s': 30.0, 'reagent_ul': 20.0, 'final_pct': 93.42, 'interior_pct': 26.82, 'peak_kpa': 2.0}. 3 candidates, 0 rejected.
- t = 30.0 s, step 2 (plan): step 2: chose exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak; predicted {'time_s': 192.3, 'reagent_ul': 80.0, 'final_pct': 100.0, 'interior_pct': 95.0, 'peak_kpa': 2.0}. 16 candidates, 7 rejected (4 x predicted …, 3 x peak …).
- t = 54.0 s, step 2 (replan): step 2: re-planned the rest after a model update: exchange 3 x 20 uL wash_buffer at 40 uL/min + 77 s soak. 16 candidates, 4 rejected (4 x peak …).
- t = 102.0 s, step 2 (plan): step 2: chose exchange 1 x 20 uL wash_buffer at 20 uL/min + 59 s soak; predicted {'time_s': 119.4, 'reagent_ul': 20.0, 'final_pct': 100.0, 'interior_pct': 95.0, 'peak_kpa': 1.232}. 16 candidates, 4 rejected (4 x peak …).
- t = 221.4 s, step 3 (plan): step 3: chose exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min; predicted {'time_s': 20.0, 'reagent_ul': 20.0, 'final_pct': 94.25, 'interior_pct': 19.38, 'peak_kpa': -3.696}. 8 candidates, 5 rejected (5 x lifts …).
- t = 241.4 s, step 4 (plan): step 4: chose release 28 uL at 40 uL/min; predicted {'time_s': 42.0, 'reagent_ul': 28.0, 'final_pct': 99.91, 'interior_pct': 59.58, 'peak_kpa': 0.0}. 9 candidates, 3 rejected (3 x recovers …).

## Full operator log

- `    0.0 s` **plan**: step 1: chose load 20 uL at 40 uL/min; predicted {'time_s': 30.0, 'reagent_ul': 20.0, 'final_pct': 93.42, 'interior_pct': 26.82, 'peak_kpa': 2.0}
- `    0.0 s` **safety**: approved: load 20 uL at 40 uL/min
- `   30.0 s` **plan**: step 2: chose exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak; predicted {'time_s': 192.3, 'reagent_ul': 80.0, 'final_pct': 100.0, 'interior_pct': 95.0, 'peak_kpa': 2.0}
- `   30.0 s` **safety**: approved: exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak
- `   30.0 s` **model_update**: mesh_resistance[trap_1]: 0.05 -> 0.0616 kPa per uL/min (pressure 2.58 kPa vs predicted 2.00 (+29%))
- `   48.0 s` **model_update**: chamber_volume_ul[trap_1]: 7.35 -> 6.79 uL (outlet absorbance 0.913 after 16 uL: leftover old liquid -23% vs the model since the exchange began)
- `   54.0 s` **replan**: step 2: re-planned the rest after a model update: exchange 3 x 20 uL wash_buffer at 40 uL/min + 77 s soak
- `   60.0 s` **model_update**: chamber_volume_ul[trap_1]: 6.79 -> 7.001 uL (outlet absorbance 0.966 after 24 uL: leftover old liquid +16% vs the model since the exchange began)
- `  102.0 s` **alert**: sensor_disagreement: pressure sensor reading is frozen (identical values, no noise)
- `  102.0 s` **model_update**: distrusting sensor pressure_kpa; falling back to the model for it (The pressure_kpa sensor disagrees with the model; ignore it and fall back to the model for that quantity.)
- `  102.0 s` **plan**: step 2: chose exchange 1 x 20 uL wash_buffer at 20 uL/min + 59 s soak; predicted {'time_s': 119.4, 'reagent_ul': 20.0, 'final_pct': 100.0, 'interior_pct': 95.0, 'peak_kpa': 1.232}
- `  102.0 s` **safety**: approved: exchange 1 x 20 uL wash_buffer at 20 uL/min + 59 s soak
- `  221.4 s` **plan**: step 3: chose exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min; predicted {'time_s': 20.0, 'reagent_ul': 20.0, 'final_pct': 94.25, 'interior_pct': 19.38, 'peak_kpa': -3.696}
- `  221.4 s` **safety**: approved: exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min
- `  241.4 s` **plan**: step 4: chose release 28 uL at 40 uL/min; predicted {'time_s': 42.0, 'reagent_ul': 28.0, 'final_pct': 99.91, 'interior_pct': 59.58, 'peak_kpa': 0.0}
- `  241.4 s` **safety**: approved: release 28 uL at 40 uL/min

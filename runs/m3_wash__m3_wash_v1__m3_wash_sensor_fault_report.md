# Run report: Module 3: capsule wash on m3_wash_v1

> **SIMULATED HARDWARE.** This run used the virtual chip: no physical pump, valves or sensors. Values marked TRUE come from the simulation and could never be measured directly on real hardware.

- Scenario: `m3_wash_sensor_fault`: Closed loop, but the pressure sensor sticks mid-run: detected as sensor_disagreement, distrusted, model takes over.
- Operator: **planner** · Adapter: `virtual_chip` · Loop: **closed**
- Sensors: pressure_kpa, outlet_absorbance
- Faults injected: sensor_fault {'sensor': 'pressure_kpa', 'mode': 'stuck', 'start_t_s': 90}
- Status: **done** · Protocol time: 351.33 s
- Created: 2026-11-07T17:59:31Z

## Outcome

| | Believed (model + sensors) | TRUE (simulation) |
|---|---|---|
| Final exchange % (step 2) | 100.0% | 99.96% |
| Capsule recovery | 97.83% | 98.29% |
| Capsules collected | 9783 | 9829 |
| Capsules lost to waste | 0 | 0 |
| Capsules left on the mesh | 34 | 25 |
| Reagent used: capsule_suspension | 14.7 uL | 14.7 uL |
| Reagent used: resuspension_buffer | 95.6 uL | 95.6 uL |
| Reagent used: wash_buffer | 103.22 uL | 103.22 uL |
| Waste | 157.62 uL | 157.62 uL |

Alerts: **1** (sensor_disagreement) · Recoveries: **0** · Model updates: **3**

## Belief vs truth drift

How far the operator's belief was from the simulated truth at the end of the run.

- Mesh resistance (model vs true): -23.1% at the start -> -5.2% at the end
- Chamber volume (model vs true): +11.1% at the start -> +5.7% at the end
- Final exchange %: off by 0.04 points
- Capsules collected: off by 46 capsules
- Capsules lost: off by 0 capsules
- Waste volume: off by 0.0 uL

## Steps

| Start | Step | Command as executed | Duration | Safety | Operator's explanation |
|---|---|---|---|---|---|
| 0.0 s | 1 | load 14.7 uL at 40 uL/min | 22.1 s | approved | Fastest safe option of 3 simulated (0 rejected): load 14.7 uL at 40 uL/min, predicted 22 s, 63.2% exchanged, peak 2.00 kPa. |
| 22.1 s | 2 | exchange 1 x 31.76 uL wash_buffer at 40 uL/min + 29 s soak | 47.6 s | approved | Fastest safe option of 16 simulated (7 rejected): exchange 3 x 39.7 uL wash_buffer at 40 uL/min + 29 s soak, predicted 208 s, 100.0% exchanged, peak 2.00 kPa. |
| 69.7 s | 2 | exchange 1 x 31.76 uL wash_buffer at 40 uL/min + 38 s soak | 47.6 s | approved | Re-planned after a model update: Fastest safe option of 16 simulated (4 rejected): exchange 2 x 39.7 uL wash_buffer at 40 uL/min + 38 s soak, predicted 158 s, 100.0% exchanged, peak 2.46 kPa. |
| 117.3 s | 2 | exchange 1 x 39.7 uL wash_buffer at 40 uL/min + 51 s soak | 110.5 s | approved | Fastest safe option of 16 simulated (4 rejected): exchange 1 x 39.7 uL wash_buffer at 40 uL/min + 51 s soak, predicted 110 s, 99.9% exchanged, peak 2.46 kPa. |
| 227.8 s | 3 | back-flush 1 x 39.7 uL resuspension_buffer at 60 uL/min | 39.7 s | approved | Fastest safe option of 8 simulated (5 rejected): exchange back-flush 1 x 39.7 uL resuspension_buffer at 60 uL/min, predicted 40 s, 94.2% exchanged, peak -3.70 kPa. |
| 267.5 s | 4 | release 55.9 uL at 40 uL/min | 83.8 s | approved | Fastest safe option of 9 simulated (3 rejected): release 55.9 uL at 40 uL/min, predicted 84 s, 99.9% exchanged. |

## Alerts

- **t = 117.3 s, step 2: `sensor_disagreement`** (critical): pressure sensor reading is frozen (identical values, no noise). Evidence: `{'sensor': 'pressure_kpa', 'readings': [2.5982, 2.5982, 2.5982], 'predicted': [2.4642, 2.4642, 2.4642], 'mode': 'stuck'}`

## Recoveries and operator interventions

- t = 117.3 s: distrusting sensor pressure_kpa; falling back to the model for it (The pressure_kpa sensor disagrees with the model; ignore it and fall back to the model for that quantity.)

## Model updates

| t | Parameter | Old -> new | Why |
|---|---|---|---|
| 22.1 s | `mesh_resistance[m3_trap_1]` | 0.05 -> 0.0616 kPa per uL/min | pressure 2.58 kPa vs predicted 2.00 (+29%) |
| 57.8 s | `chamber_volume_ul[m3_trap_1]` | 14.7 -> 13.58 uL | outlet absorbance 0.911 after 32 uL: leftover old liquid -23% vs the model since the exchange began |
| 81.6 s | `chamber_volume_ul[m3_trap_1]` | 13.58 -> 13.99 uL | outlet absorbance 0.965 after 48 uL: leftover old liquid +16% vs the model since the exchange began |

## Optimizer decisions

- t = 0.0 s, step 1 (plan): step 1: chose load 14.7 uL at 40 uL/min; predicted {'time_s': 22.1, 'reagent_ul': 14.7, 'final_pct': 63.21, 'interior_pct': 11.81, 'peak_kpa': 2.0}. 3 candidates, 0 rejected.
- t = 22.1 s, step 2 (plan): step 2: chose exchange 3 x 39.7 uL wash_buffer at 40 uL/min + 29 s soak; predicted {'time_s': 207.9, 'reagent_ul': 119.1, 'final_pct': 99.97, 'interior_pct': 95.0, 'peak_kpa': 2.0}. 16 candidates, 7 rejected (4 x predicted …, 3 x peak …).
- t = 69.7 s, step 2 (replan): step 2: re-planned the rest after a model update: exchange 2 x 39.7 uL wash_buffer at 40 uL/min + 38 s soak. 16 candidates, 4 rejected (4 x peak …).
- t = 117.3 s, step 2 (plan): step 2: chose exchange 1 x 39.7 uL wash_buffer at 40 uL/min + 51 s soak; predicted {'time_s': 110.4, 'reagent_ul': 39.7, 'final_pct': 99.94, 'interior_pct': 95.0, 'peak_kpa': 2.464}. 16 candidates, 4 rejected (4 x peak …).
- t = 227.8 s, step 3 (plan): step 3: chose exchange back-flush 1 x 39.7 uL resuspension_buffer at 60 uL/min; predicted {'time_s': 39.7, 'reagent_ul': 39.7, 'final_pct': 94.15, 'interior_pct': 34.03, 'peak_kpa': -3.696}. 8 candidates, 5 rejected (5 x lifts …).
- t = 267.5 s, step 4 (plan): step 4: chose release 55.9 uL at 40 uL/min; predicted {'time_s': 83.8, 'reagent_ul': 55.9, 'final_pct': 99.9, 'interior_pct': 83.13, 'peak_kpa': 0.0}. 9 candidates, 3 rejected (3 x recovers …).

## Full operator log

- `    0.0 s` **plan**: step 1: chose load 14.7 uL at 40 uL/min; predicted {'time_s': 22.1, 'reagent_ul': 14.7, 'final_pct': 63.21, 'interior_pct': 11.81, 'peak_kpa': 2.0}
- `    0.0 s` **safety**: approved: load 14.7 uL at 40 uL/min
- `   22.1 s` **plan**: step 2: chose exchange 3 x 39.7 uL wash_buffer at 40 uL/min + 29 s soak; predicted {'time_s': 207.9, 'reagent_ul': 119.1, 'final_pct': 99.97, 'interior_pct': 95.0, 'peak_kpa': 2.0}
- `   22.1 s` **safety**: approved: exchange 3 x 39.7 uL wash_buffer at 40 uL/min + 29 s soak
- `   22.1 s` **model_update**: mesh_resistance[m3_trap_1]: 0.05 -> 0.0616 kPa per uL/min (pressure 2.58 kPa vs predicted 2.00 (+29%))
- `   57.8 s` **model_update**: chamber_volume_ul[m3_trap_1]: 14.7 -> 13.58 uL (outlet absorbance 0.911 after 32 uL: leftover old liquid -23% vs the model since the exchange began)
- `   69.7 s` **replan**: step 2: re-planned the rest after a model update: exchange 2 x 39.7 uL wash_buffer at 40 uL/min + 38 s soak
- `   81.6 s` **model_update**: chamber_volume_ul[m3_trap_1]: 13.58 -> 13.99 uL (outlet absorbance 0.965 after 48 uL: leftover old liquid +16% vs the model since the exchange began)
- `  117.3 s` **alert**: sensor_disagreement: pressure sensor reading is frozen (identical values, no noise)
- `  117.3 s` **model_update**: distrusting sensor pressure_kpa; falling back to the model for it (The pressure_kpa sensor disagrees with the model; ignore it and fall back to the model for that quantity.)
- `  117.3 s` **plan**: step 2: chose exchange 1 x 39.7 uL wash_buffer at 40 uL/min + 51 s soak; predicted {'time_s': 110.4, 'reagent_ul': 39.7, 'final_pct': 99.94, 'interior_pct': 95.0, 'peak_kpa': 2.464}
- `  117.3 s` **safety**: approved: exchange 1 x 39.7 uL wash_buffer at 40 uL/min + 51 s soak
- `  227.8 s` **plan**: step 3: chose exchange back-flush 1 x 39.7 uL resuspension_buffer at 60 uL/min; predicted {'time_s': 39.7, 'reagent_ul': 39.7, 'final_pct': 94.15, 'interior_pct': 34.03, 'peak_kpa': -3.696}
- `  227.8 s` **safety**: approved: exchange back-flush 1 x 39.7 uL resuspension_buffer at 60 uL/min
- `  267.5 s` **plan**: step 4: chose release 55.9 uL at 40 uL/min; predicted {'time_s': 83.8, 'reagent_ul': 55.9, 'final_pct': 99.9, 'interior_pct': 83.13, 'peak_kpa': 0.0}
- `  267.5 s` **safety**: approved: release 55.9 uL at 40 uL/min

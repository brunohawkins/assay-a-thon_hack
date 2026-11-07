# Run report: Module 3: capsule wash on m3_wash_v1

> **SIMULATED HARDWARE.** This run used the virtual chip: no physical pump, routing or sensors. Values marked TRUE come from the simulation and could never be measured directly on real hardware.

- Scenario: `m3_wash_clog`: Closed loop with the mesh clogging from wash 2: pressure climbs, alert, back-flush, re-plan, recovery. Clog growth rescaled by 0.50 when the trap went to a single 0.3 mm etch depth and its volume changed from 7.35 to 14.7 uL; the fault is per microlitre pushed.
- Operator: **planner** · Adapter: `virtual_chip` · Loop: **closed**
- Sensors: outlet_absorbance, pressure_kpa
- Faults injected: clog {'start_wash': 2, 'growth_per_ul': 0.0175, 'backflush_clears': 0.8}
- Status: **done** · Protocol time: 514.18 s
- Created: 2026-11-07T23:22:20Z

## Outcome

| | Believed (model + outlet_absorbance, pressure_kpa) | TRUE (simulation) |
|---|---|---|
| Where these come from | model corrected by outlet_absorbance, pressure_kpa | the virtual chip's own state |
| Final exchange % (step 2) | 100.0% | 100.0% |
| Capsule recovery (believed value is ASSUMED) | 98.12% | 97.95% |
| Capsules collected | 9812 | 9795 |
| Capsules lost to waste | 0 | 0 |
| Capsules left on the mesh | 5 | 4 |
| Reagent used: capsule_suspension | 14.7 uL | 14.7 uL |
| Reagent used: resuspension_buffer | 103.36 uL | 103.36 uL |
| Reagent used: wash_buffer | 162.98 uL | 162.98 uL |
| Waste | 229.34 uL | 229.34 uL |

Capsule recovery: ASSUMED: the denominator is the capsules the chip spec says were loaded (capsule.count - they were never counted), and the numerator is the MODEL's prediction of how many came out (no capsule counter was on).

Capsule conservation (capsules_loaded == on a mesh + suspended + collected + lost): every believed count added up; 0.0 capsules unaccounted for at the end.

Alerts: **1** (pressure_high) · Recoveries: **1** · Model updates: **12**

## Belief vs truth drift

How far the operator's belief was from the simulated truth at the end of the run.

- Mesh resistance (model vs true): -23.1% at the start -> +91.3% at the end
- Chamber volume (model vs true): +11.1% at the start -> -2.2% at the end
- Final exchange %: off by 0.0 points
- Capsules collected: off by 17 capsules
- Capsules lost: off by 0 capsules
- Waste volume: off by 0.0 uL

## Steps

| Start | Step | Command as executed | Duration | Safety | Operator's explanation |
|---|---|---|---|---|---|
| 0.0 s | 1 | load 14.7 uL at 40 uL/min | 22.1 s | approved | Fastest safe option of 3 simulated (0 rejected): load 14.7 uL at 40 uL/min, predicted 22 s, 63.2% exchanged, peak 2.00 kPa. |
| 22.1 s | 2 | exchange 1 x 31.76 uL wash_buffer at 40 uL/min + 29 s soak | 47.6 s | approved | Fastest safe option of 20 simulated (8 rejected): exchange 3 x 39.7 uL wash_buffer at 40 uL/min + 29 s soak, predicted 208 s, 100.0% exchanged, peak 2.00 kPa. |
| 69.7 s | 2 | exchange 1 x 39.7 uL wash_buffer at 40 uL/min + 38 s soak | 59.5 s | approved | Re-planned after a model update: Fastest safe option of 20 simulated (4 rejected): exchange 2 x 39.7 uL wash_buffer at 40 uL/min + 38 s soak, predicted 158 s, 100.0% exchanged, peak 2.46 kPa. |
| 129.2 s | 2 | back-flush 1 x 28 uL wash_buffer at 45 uL/min | 37.3 s | warning · recovery | Pressure is near the escape limit, so back-flush to lift the capsules and clear the mesh before continuing. |
| 166.6 s | 2 | exchange 1 x 7.94 uL wash_buffer at 20 uL/min | 23.8 s | approved | Fastest safe option of 20 simulated (8 rejected): exchange 1 x 39.7 uL wash_buffer at 20 uL/min, predicted 119 s, 100.0% exchanged, peak 2.14 kPa. |
| 190.4 s | 2 | exchange 1 x 15.88 uL wash_buffer at 40 uL/min | 23.8 s | approved | Re-planned after a model update: Fastest safe option of 20 simulated (4 rejected): exchange 1 x 39.7 uL wash_buffer at 40 uL/min, predicted 60 s, 100.0% exchanged, peak 3.16 kPa. |
| 214.2 s | 2 | exchange 1 x 39.7 uL wash_buffer at 20 uL/min | 119.1 s | approved | Re-planned after a model update: Fastest safe option of 20 simulated (8 rejected): exchange 1 x 39.7 uL wash_buffer at 20 uL/min, predicted 119 s, 100.0% exchanged, peak 1.80 kPa. |
| 333.3 s | 3 | back-flush 1 x 31.76 uL resuspension_buffer at 30 uL/min | 63.5 s | warning | Fastest safe option of 8 simulated (7 rejected): exchange back-flush 1 x 39.7 uL resuspension_buffer at 30 uL/min, predicted 79 s, 94.2% exchanged, peak -3.73 kPa. |
| 396.8 s | 3 | back-flush 1 x 19.9 uL resuspension_buffer at 30 uL/min | 39.8 s | warning | Re-planned after a model update: Fastest safe option of 8 simulated (4 rejected): exchange back-flush 1 x 19.9 uL resuspension_buffer at 30 uL/min, predicted 40 s, 98.3% exchanged, peak -3.73 kPa. |
| 436.6 s | 4 | release 51.7 uL at 40 uL/min | 77.5 s | approved | Fastest safe option of 9 simulated (3 rejected): release 51.7 uL at 40 uL/min, predicted 78 s, 100.0% exchanged. |

## Alerts

- **t = 129.2 s, step 2: `pressure_high`** (critical): 4.45 kPa measured at the mesh (alert at 4.25, capsules escape at 5). Evidence: `{'estimate_kpa': 4.445, 'source': 'sensor', 'measured_kpa': 4.445, 'predicted_kpa': 3.626, 'limit_kpa': 5.0, 'flow_ul_min': 40}`

## Recoveries and operator interventions

- t = 129.2 s: recovery back-flush of m3_trap_1: exchange back-flush 1 x 28 uL wash_buffer at 45 uL/min

## Model updates

| t | Parameter | Old -> new | Why |
|---|---|---|---|
| 34.0 s | `mesh_resistance[m3_trap_1]` | 0.05 -> 0.0616 kPa per uL/min | pressure 2.58 kPa vs predicted 2.00 (+29%) |
| 69.7 s | `chamber_volume_ul[m3_trap_1]` | 14.7 -> 13.58 uL | outlet absorbance 0.911 after 32 uL: leftover old liquid -23% vs the model since the exchange began |
| 81.6 s | `mesh_resistance[m3_trap_1]` | 0.0616 -> 0.07034 kPa per uL/min | pressure 2.90 kPa vs predicted 2.46 (+18%) |
| 93.5 s | `mesh_resistance[m3_trap_1]` | 0.07034 -> 0.08048 kPa per uL/min | pressure 3.32 kPa vs predicted 2.81 (+18%) |
| 93.5 s | `chamber_volume_ul[m3_trap_1]` | 13.58 -> 13.99 uL | outlet absorbance 0.965 after 48 uL: leftover old liquid +16% vs the model since the exchange began |
| 105.4 s | `mesh_resistance[m3_trap_1]` | 0.08048 -> 0.09064 kPa per uL/min | pressure 3.73 kPa vs predicted 3.22 (+16%) |
| 129.2 s | `mesh_resistance[m3_trap_1]` | 0.09064 -> 0.107 kPa per uL/min | pressure 4.45 kPa vs predicted 3.63 (+23%) |
| 190.4 s | `mesh_resistance[m3_trap_1]` | 0.107 -> 0.07907 kPa per uL/min | pressure 1.44 kPa vs predicted 2.14 (-33%) |
| 214.2 s | `mesh_resistance[m3_trap_1]` | 0.07907 -> 0.09013 kPa per uL/min | pressure 3.72 kPa vs predicted 3.16 (+17%) |
| 261.9 s | `mesh_resistance[m3_trap_1]` | 0.09013 -> 0.1079 kPa per uL/min | pressure 2.25 kPa vs predicted 1.80 (+25%) |
| 309.5 s | `mesh_resistance[m3_trap_1]` | 0.1079 -> 0.1243 kPa per uL/min | pressure 2.57 kPa vs predicted 2.16 (+19%) |
| 396.8 s | `chamber_volume_ul[m3_trap_1]` | 13.99 -> 12.93 uL | outlet absorbance 0.921 after 32 uL: leftover old liquid -24% vs the model since the exchange began |

## Optimizer decisions

- t = 0.0 s, step 1 (plan): step 1: chose load 14.7 uL at 40 uL/min; predicted {'time_s': 22.1, 'reagent_ul': 14.7, 'final_pct': 63.21, 'interior_pct': 11.81, 'peak_kpa': 2.0}. 3 candidates, 0 rejected.
- t = 22.1 s, step 2 (plan): step 2: chose exchange 3 x 39.7 uL wash_buffer at 40 uL/min + 29 s soak; predicted {'time_s': 208.0, 'reagent_ul': 119.1, 'final_pct': 99.97, 'interior_pct': 95.01, 'peak_kpa': 2.0}. 20 candidates, 8 rejected (5 x predicted …, 3 x peak …).
- t = 69.7 s, step 2 (replan): step 2: re-planned the rest after a model update: exchange 2 x 39.7 uL wash_buffer at 40 uL/min + 38 s soak. 20 candidates, 4 rejected (4 x peak …).
- t = 129.2 s, step 2 (plan): recovery back-flush of m3_trap_1: exchange back-flush 1 x 28 uL wash_buffer at 45 uL/min. 8 candidates, 6 rejected (4 x lifts …, 2 x safety).
- t = 166.6 s, step 2 (plan): step 2: chose exchange 1 x 39.7 uL wash_buffer at 20 uL/min; predicted {'time_s': 119.1, 'reagent_ul': 39.7, 'final_pct': 100.0, 'interior_pct': 98.11, 'peak_kpa': 2.141}. 20 candidates, 8 rejected (4 x peak …, 4 x safety).
- t = 190.4 s, step 2 (replan): step 2: re-planned the rest after a model update: exchange 1 x 39.7 uL wash_buffer at 40 uL/min. 20 candidates, 4 rejected (4 x safety).
- t = 214.2 s, step 2 (replan): step 2: re-planned the rest after a model update: exchange 1 x 39.7 uL wash_buffer at 20 uL/min. 20 candidates, 8 rejected (4 x peak …, 4 x safety).
- t = 333.3 s, step 3 (plan): step 3: chose exchange back-flush 1 x 39.7 uL resuspension_buffer at 30 uL/min; predicted {'time_s': 79.4, 'reagent_ul': 39.7, 'final_pct': 94.15, 'interior_pct': 54.51, 'peak_kpa': -3.73}. 8 candidates, 7 rejected (3 x lifts …, 4 x safety).
- t = 396.8 s, step 3 (replan): step 3: re-planned the rest after a model update: exchange back-flush 1 x 19.9 uL resuspension_buffer at 30 uL/min. 8 candidates, 4 rejected (4 x safety).
- t = 436.6 s, step 4 (plan): step 4: chose release 51.7 uL at 40 uL/min; predicted {'time_s': 77.6, 'reagent_ul': 51.7, 'final_pct': 99.97, 'interior_pct': 91.36, 'peak_kpa': 0.0}. 9 candidates, 3 rejected (3 x recovers …).

## Full operator log

- `    0.0 s` **plan**: step 1: chose load 14.7 uL at 40 uL/min; predicted {'time_s': 22.1, 'reagent_ul': 14.7, 'final_pct': 63.21, 'interior_pct': 11.81, 'peak_kpa': 2.0}
- `    0.0 s` **safety**: approved: load 14.7 uL at 40 uL/min
- `   22.1 s` **plan**: step 2: chose exchange 3 x 39.7 uL wash_buffer at 40 uL/min + 29 s soak; predicted {'time_s': 208.0, 'reagent_ul': 119.1, 'final_pct': 99.97, 'interior_pct': 95.01, 'peak_kpa': 2.0}
- `   22.1 s` **safety**: approved: exchange 3 x 39.7 uL wash_buffer at 40 uL/min + 29 s soak
- `   34.0 s` **model_update**: mesh_resistance[m3_trap_1]: 0.05 -> 0.0616 kPa per uL/min (pressure 2.58 kPa vs predicted 2.00 (+29%))
- `   69.7 s` **model_update**: chamber_volume_ul[m3_trap_1]: 14.7 -> 13.58 uL (outlet absorbance 0.911 after 32 uL: leftover old liquid -23% vs the model since the exchange began)
- `   69.7 s` **replan**: step 2: re-planned the rest after a model update: exchange 2 x 39.7 uL wash_buffer at 40 uL/min + 38 s soak
- `   81.6 s` **model_update**: mesh_resistance[m3_trap_1]: 0.0616 -> 0.07034 kPa per uL/min (pressure 2.90 kPa vs predicted 2.46 (+18%))
- `   93.5 s` **model_update**: mesh_resistance[m3_trap_1]: 0.07034 -> 0.08048 kPa per uL/min (pressure 3.32 kPa vs predicted 2.81 (+18%))
- `   93.5 s` **model_update**: chamber_volume_ul[m3_trap_1]: 13.58 -> 13.99 uL (outlet absorbance 0.965 after 48 uL: leftover old liquid +16% vs the model since the exchange began)
- `  105.4 s` **model_update**: mesh_resistance[m3_trap_1]: 0.08048 -> 0.09064 kPa per uL/min (pressure 3.73 kPa vs predicted 3.22 (+16%))
- `  129.2 s` **alert**: pressure_high: 4.45 kPa measured at the mesh (alert at 4.25, capsules escape at 5)
- `  129.2 s` **model_update**: mesh_resistance[m3_trap_1]: 0.09064 -> 0.107 kPa per uL/min (pressure 4.45 kPa vs predicted 3.63 (+23%))
- `  129.2 s` **plan**: recovery back-flush of m3_trap_1: exchange back-flush 1 x 28 uL wash_buffer at 45 uL/min
- `  129.2 s` **safety**: warning: recovery back-flush
- `  166.6 s` **plan**: step 2: chose exchange 1 x 39.7 uL wash_buffer at 20 uL/min; predicted {'time_s': 119.1, 'reagent_ul': 39.7, 'final_pct': 100.0, 'interior_pct': 98.11, 'peak_kpa': 2.141}
- `  166.6 s` **safety**: approved: exchange 1 x 39.7 uL wash_buffer at 20 uL/min
- `  190.4 s` **model_update**: mesh_resistance[m3_trap_1]: 0.107 -> 0.07907 kPa per uL/min (pressure 1.44 kPa vs predicted 2.14 (-33%))
- `  190.4 s` **replan**: step 2: re-planned the rest after a model update: exchange 1 x 39.7 uL wash_buffer at 40 uL/min
- `  214.2 s` **model_update**: mesh_resistance[m3_trap_1]: 0.07907 -> 0.09013 kPa per uL/min (pressure 3.72 kPa vs predicted 3.16 (+17%))
- `  214.2 s` **replan**: step 2: re-planned the rest after a model update: exchange 1 x 39.7 uL wash_buffer at 20 uL/min
- `  261.9 s` **model_update**: mesh_resistance[m3_trap_1]: 0.09013 -> 0.1078 kPa per uL/min (pressure 2.25 kPa vs predicted 1.80 (+25%))
- `  309.5 s` **model_update**: mesh_resistance[m3_trap_1]: 0.1078 -> 0.1243 kPa per uL/min (pressure 2.57 kPa vs predicted 2.16 (+19%))
- `  333.3 s` **plan**: step 3: chose exchange back-flush 1 x 39.7 uL resuspension_buffer at 30 uL/min; predicted {'time_s': 79.4, 'reagent_ul': 39.7, 'final_pct': 94.15, 'interior_pct': 54.51, 'peak_kpa': -3.73}
- `  333.3 s` **safety**: warning: exchange back-flush 1 x 39.7 uL resuspension_buffer at 30 uL/min (predicted 3.73 kPa lifting off the retention is above 70% of the 5 kPa escape pressure)
- `  396.8 s` **model_update**: chamber_volume_ul[m3_trap_1]: 13.99 -> 12.93 uL (outlet absorbance 0.921 after 32 uL: leftover old liquid -24% vs the model since the exchange began)
- `  396.8 s` **replan**: step 3: re-planned the rest after a model update: exchange back-flush 1 x 19.9 uL resuspension_buffer at 30 uL/min
- `  436.6 s` **plan**: step 4: chose release 51.7 uL at 40 uL/min; predicted {'time_s': 77.6, 'reagent_ul': 51.7, 'final_pct': 99.97, 'interior_pct': 91.36, 'peak_kpa': 0.0}
- `  436.6 s` **safety**: approved: release 51.7 uL at 40 uL/min

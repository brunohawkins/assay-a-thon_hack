# Run report: Module 3: capsule wash on m3_wash_v1

> **SIMULATED HARDWARE.** This run used the virtual chip: no physical pump, valves or sensors. Values marked TRUE come from the simulation and could never be measured directly on real hardware.

- Scenario: `m3_wash_clog`: Closed loop with the mesh clogging from wash 2: pressure climbs, alert, back-flush, re-plan, recovery.
- Operator: **planner** · Adapter: `virtual_chip` · Loop: **closed**
- Sensors: pressure_kpa, outlet_absorbance
- Faults injected: clog {'start_wash': 2, 'growth_per_ul': 0.035, 'backflush_clears': 0.8}
- Status: **done** · Protocol time: 283.2 s
- Created: 2026-11-07T14:49:57Z

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
| Reagent used: wash_buffer | 74.0 uL | 74.0 uL |
| Waste | 114.0 uL | 114.0 uL |

Alerts: **1** (pressure_high) · Recoveries: **1** · Model updates: **10**

## Belief vs truth drift

How far the operator's belief was from the simulated truth at the end of the run.

- Mesh resistance (model vs true): -23.1% at the start -> +66.5% at the end
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
| 54.0 s | 2 | exchange 1 x 20 uL wash_buffer at 40 uL/min + 77 s soak | 30.0 s | approved | Re-planned after a model update: Fastest safe option of 16 simulated (4 rejected): exchange 3 x 20 uL wash_buffer at 40 uL/min + 77 s soak, predicted 167 s, 100.0% exchanged, peak 2.46 kPa. |
| 84.0 s | 2 | back-flush 1 x 14 uL wash_buffer at 60 uL/min | 14.0 s | approved · recovery | Pressure is near the escape limit, so back-flush to lift the capsules and clear the mesh before continuing. |
| 98.0 s | 2 | exchange 1 x 4 uL wash_buffer at 10 uL/min + 3 s soak | 24.0 s | approved | Fastest safe option of 16 simulated (8 rejected): exchange 1 x 20 uL wash_buffer at 10 uL/min + 3 s soak, predicted 123 s, 100.0% exchanged, peak 1.07 kPa. |
| 122.0 s | 2 | exchange 1 x 20 uL wash_buffer at 20 uL/min + 39 s soak | 99.2 s | approved | Re-planned after a model update: Fastest safe option of 16 simulated (4 rejected): exchange 1 x 20 uL wash_buffer at 20 uL/min + 39 s soak, predicted 99 s, 100.0% exchanged, peak 1.55 kPa. |
| 221.2 s | 3 | back-flush 1 x 20 uL resuspension_buffer at 60 uL/min | 20.0 s | approved | Fastest safe option of 8 simulated (5 rejected): exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min, predicted 20 s, 94.2% exchanged, peak -6.49 kPa. |
| 241.2 s | 4 | release 28 uL at 40 uL/min | 42.0 s | approved | Fastest safe option of 9 simulated (3 rejected): release 28 uL at 40 uL/min, predicted 42 s, 99.9% exchanged. |

## Alerts

- **t = 84.0 s, step 2: `pressure_high`** (critical): 4.46 kPa at the mesh (alert at 4.25, capsules escape at 5). Evidence: `{'measured_kpa': 4.4587, 'predicted_kpa': 3.633, 'limit_kpa': 5.0, 'flow_ul_min': 40}`

## Recoveries and operator interventions

- t = 84.0 s: recovery back-flush of m3_trap_1: exchange back-flush 1 x 14 uL wash_buffer at 60 uL/min

## Model updates

| t | Parameter | Old -> new | Why |
|---|---|---|---|
| 30.0 s | `mesh_resistance[m3_trap_1]` | 0.05 -> 0.0616 kPa per uL/min | pressure 2.58 kPa vs predicted 2.00 (+29%) |
| 48.0 s | `chamber_volume_ul[m3_trap_1]` | 7.35 -> 6.79 uL | outlet absorbance 0.913 after 16 uL: leftover old liquid -23% vs the model since the exchange began |
| 54.0 s | `mesh_resistance[m3_trap_1]` | 0.0616 -> 0.07039 kPa per uL/min | pressure 2.90 kPa vs predicted 2.46 (+18%) |
| 60.0 s | `mesh_resistance[m3_trap_1]` | 0.07039 -> 0.0806 kPa per uL/min | pressure 3.33 kPa vs predicted 2.82 (+18%) |
| 60.0 s | `chamber_volume_ul[m3_trap_1]` | 6.79 -> 7.001 uL | outlet absorbance 0.966 after 24 uL: leftover old liquid +16% vs the model since the exchange began |
| 66.0 s | `mesh_resistance[m3_trap_1]` | 0.0806 -> 0.09083 kPa per uL/min | pressure 3.74 kPa vs predicted 3.22 (+16%) |
| 78.0 s | `mesh_resistance[m3_trap_1]` | 0.09083 -> 0.1073 kPa per uL/min | pressure 4.46 kPa vs predicted 3.63 (+23%) |
| 98.0 s | `mesh_resistance[m3_trap_1]` | 0.1073 -> 0.07761 kPa per uL/min | pressure 0.70 kPa vs predicted 1.07 (-35%) |
| 134.0 s | `mesh_resistance[m3_trap_1]` | 0.07761 -> 0.09063 kPa per uL/min | pressure 1.88 kPa vs predicted 1.55 (+21%) |
| 158.0 s | `mesh_resistance[m3_trap_1]` | 0.09063 -> 0.1082 kPa per uL/min | pressure 2.25 kPa vs predicted 1.81 (+24%) |

## Optimizer decisions

- t = 0.0 s, step 1 (plan): step 1: chose load 20 uL at 40 uL/min; predicted {'time_s': 30.0, 'reagent_ul': 20.0, 'final_pct': 93.42, 'interior_pct': 26.82, 'peak_kpa': 2.0}. 3 candidates, 0 rejected.
- t = 30.0 s, step 2 (plan): step 2: chose exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak; predicted {'time_s': 192.3, 'reagent_ul': 80.0, 'final_pct': 100.0, 'interior_pct': 95.0, 'peak_kpa': 2.0}. 16 candidates, 7 rejected (4 x predicted …, 3 x peak …).
- t = 54.0 s, step 2 (replan): step 2: re-planned the rest after a model update: exchange 3 x 20 uL wash_buffer at 40 uL/min + 77 s soak. 16 candidates, 4 rejected (4 x peak …).
- t = 84.0 s, step 2 (plan): recovery back-flush of m3_trap_1: exchange back-flush 1 x 14 uL wash_buffer at 60 uL/min. 8 candidates, 5 rejected (5 x lifts …).
- t = 98.0 s, step 2 (plan): step 2: chose exchange 1 x 20 uL wash_buffer at 10 uL/min + 3 s soak; predicted {'time_s': 123.2, 'reagent_ul': 20.0, 'final_pct': 100.0, 'interior_pct': 95.0, 'peak_kpa': 1.073}. 16 candidates, 8 rejected (4 x peak …, 4 x safety).
- t = 122.0 s, step 2 (replan): step 2: re-planned the rest after a model update: exchange 1 x 20 uL wash_buffer at 20 uL/min + 39 s soak. 16 candidates, 4 rejected (4 x safety).
- t = 221.2 s, step 3 (plan): step 3: chose exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min; predicted {'time_s': 20.0, 'reagent_ul': 20.0, 'final_pct': 94.25, 'interior_pct': 19.38, 'peak_kpa': -6.494}. 8 candidates, 5 rejected (5 x lifts …).
- t = 241.2 s, step 4 (plan): step 4: chose release 28 uL at 40 uL/min; predicted {'time_s': 42.0, 'reagent_ul': 28.0, 'final_pct': 99.91, 'interior_pct': 59.6, 'peak_kpa': 0.0}. 9 candidates, 3 rejected (3 x recovers …).

## Full operator log

- `    0.0 s` **plan**: step 1: chose load 20 uL at 40 uL/min; predicted {'time_s': 30.0, 'reagent_ul': 20.0, 'final_pct': 93.42, 'interior_pct': 26.82, 'peak_kpa': 2.0}
- `    0.0 s` **safety**: approved: load 20 uL at 40 uL/min
- `   30.0 s` **plan**: step 2: chose exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak; predicted {'time_s': 192.3, 'reagent_ul': 80.0, 'final_pct': 100.0, 'interior_pct': 95.0, 'peak_kpa': 2.0}
- `   30.0 s` **safety**: approved: exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak
- `   30.0 s` **model_update**: mesh_resistance[m3_trap_1]: 0.05 -> 0.0616 kPa per uL/min (pressure 2.58 kPa vs predicted 2.00 (+29%))
- `   48.0 s` **model_update**: chamber_volume_ul[m3_trap_1]: 7.35 -> 6.79 uL (outlet absorbance 0.913 after 16 uL: leftover old liquid -23% vs the model since the exchange began)
- `   54.0 s` **replan**: step 2: re-planned the rest after a model update: exchange 3 x 20 uL wash_buffer at 40 uL/min + 77 s soak
- `   54.0 s` **model_update**: mesh_resistance[m3_trap_1]: 0.0616 -> 0.07039 kPa per uL/min (pressure 2.90 kPa vs predicted 2.46 (+18%))
- `   60.0 s` **model_update**: mesh_resistance[m3_trap_1]: 0.07039 -> 0.0806 kPa per uL/min (pressure 3.33 kPa vs predicted 2.82 (+18%))
- `   60.0 s` **model_update**: chamber_volume_ul[m3_trap_1]: 6.79 -> 7.001 uL (outlet absorbance 0.966 after 24 uL: leftover old liquid +16% vs the model since the exchange began)
- `   66.0 s` **model_update**: mesh_resistance[m3_trap_1]: 0.0806 -> 0.09083 kPa per uL/min (pressure 3.74 kPa vs predicted 3.22 (+16%))
- `   78.0 s` **model_update**: mesh_resistance[m3_trap_1]: 0.09083 -> 0.1073 kPa per uL/min (pressure 4.46 kPa vs predicted 3.63 (+23%))
- `   84.0 s` **alert**: pressure_high: 4.46 kPa at the mesh (alert at 4.25, capsules escape at 5)
- `   84.0 s` **plan**: recovery back-flush of m3_trap_1: exchange back-flush 1 x 14 uL wash_buffer at 60 uL/min
- `   84.0 s` **safety**: approved: recovery back-flush
- `   98.0 s` **plan**: step 2: chose exchange 1 x 20 uL wash_buffer at 10 uL/min + 3 s soak; predicted {'time_s': 123.2, 'reagent_ul': 20.0, 'final_pct': 100.0, 'interior_pct': 95.0, 'peak_kpa': 1.073}
- `   98.0 s` **safety**: approved: exchange 1 x 20 uL wash_buffer at 10 uL/min + 3 s soak
- `   98.0 s` **model_update**: mesh_resistance[m3_trap_1]: 0.1073 -> 0.07761 kPa per uL/min (pressure 0.70 kPa vs predicted 1.07 (-35%))
- `  122.0 s` **replan**: step 2: re-planned the rest after a model update: exchange 1 x 20 uL wash_buffer at 20 uL/min + 39 s soak
- `  134.0 s` **model_update**: mesh_resistance[m3_trap_1]: 0.07761 -> 0.09063 kPa per uL/min (pressure 1.88 kPa vs predicted 1.55 (+21%))
- `  158.0 s` **model_update**: mesh_resistance[m3_trap_1]: 0.09063 -> 0.1082 kPa per uL/min (pressure 2.25 kPa vs predicted 1.81 (+24%))
- `  221.2 s` **plan**: step 3: chose exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min; predicted {'time_s': 20.0, 'reagent_ul': 20.0, 'final_pct': 94.25, 'interior_pct': 19.38, 'peak_kpa': -6.494}
- `  221.2 s` **safety**: approved: exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min
- `  241.2 s` **plan**: step 4: chose release 28 uL at 40 uL/min; predicted {'time_s': 42.0, 'reagent_ul': 28.0, 'final_pct': 99.91, 'interior_pct': 59.6, 'peak_kpa': 0.0}
- `  241.2 s` **safety**: approved: release 28 uL at 40 uL/min

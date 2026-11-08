# Run report: Module 3: capsule wash on m3_wash_v1

> **SIMULATED HARDWARE - DRY RUN.** Nothing moved and nothing was measured: no pump, no routing, no heater, no sensors. Every number below is the model's own prediction of what would happen. The model cannot check itself, so a dry run has no measurements and no 'true' column, and the closed loop has nothing to correct it with.

- Scenario: `m3_wash_closed_loop`: Pressure sensor at the mesh and outlet absorbance on: the model is corrected as the run goes.
- Operator: **planner** · Adapter: `dryrun` · Loop: **open**
- Sensors: none (open loop) · asked for but not available on `dryrun`: pressure_kpa, outlet_absorbance
- Faults injected: none
- Status: **done** · Protocol time: 358.0 s
- Created: 2026-11-07T23:17:14Z

## Outcome

| | Believed (model only: NO sensors) | TRUE (simulation) |
|---|---|---|
| Where these come from | model only: no sensor was on, so every believed number is a PREDICTION | the virtual chip's own state |
| Final exchange % (step 2) | 99.97% | – |
| Capsule recovery (believed value is ASSUMED) | 97.73% | – |
| Capsules collected | 9773 | – |
| Capsules lost to waste | 0 | – |
| Capsules left on the mesh | 45 | – |
| Reagent used: capsule_suspension | 14.7 uL | – |
| Reagent used: resuspension_buffer | 98.5 uL | – |
| Reagent used: wash_buffer | 119.1 uL | – |
| Waste | 173.5 uL | – |

Capsule recovery: ASSUMED: the denominator is the capsules the chip spec says were loaded (capsule.count - they were never counted), and the numerator is the MODEL's prediction of how many came out (no capsule counter was on).

Capsule conservation (capsules_loaded == on a mesh + suspended + collected + lost): every believed count added up; 0.0 capsules unaccounted for at the end.

Alerts: **0** (none) · Recoveries: **0** · Model updates: **0**

## Steps

| Start | Step | Command as executed | Duration | Safety | Operator's explanation |
|---|---|---|---|---|---|
| 0.0 s | 1 | load 14.7 uL at 40 uL/min | 22.1 s | approved | Fastest safe option of 3 simulated (0 rejected): load 14.7 uL at 40 uL/min, predicted 22 s, 63.2% exchanged, peak 2.00 kPa. |
| 22.1 s | 2 | exchange 3 x 39.7 uL wash_buffer at 40 uL/min + 29 s soak | 208.1 s | approved | Fastest safe option of 16 simulated (7 rejected): exchange 3 x 39.7 uL wash_buffer at 40 uL/min + 29 s soak, predicted 208 s, 100.0% exchanged, peak 2.00 kPa. |
| 230.1 s | 3 | back-flush 1 x 39.7 uL resuspension_buffer at 60 uL/min | 39.7 s | approved | Fastest safe option of 8 simulated (5 rejected): exchange back-flush 1 x 39.7 uL resuspension_buffer at 60 uL/min, predicted 40 s, 93.3% exchanged, peak -3.00 kPa. |
| 269.8 s | 4 | release 58.8 uL at 40 uL/min | 88.2 s | approved | Fastest safe option of 9 simulated (3 rejected): release 58.8 uL at 40 uL/min, predicted 88 s, 99.9% exchanged. |

## Alerts

None. (No sensors were on, so nothing could be detected.)

## Recoveries and operator interventions

None.

## Model updates

None (open loop: nothing to correct the model with).

## Optimizer decisions

- t = 0.0 s, step 1 (plan): step 1: chose load 14.7 uL at 40 uL/min; predicted {'time_s': 22.1, 'reagent_ul': 14.7, 'final_pct': 63.21, 'interior_pct': 11.81, 'peak_kpa': 2.0}. 3 candidates, 0 rejected.
- t = 22.1 s, step 2 (plan): step 2: chose exchange 3 x 39.7 uL wash_buffer at 40 uL/min + 29 s soak; predicted {'time_s': 208.0, 'reagent_ul': 119.1, 'final_pct': 99.97, 'interior_pct': 95.01, 'peak_kpa': 2.0}. 16 candidates, 7 rejected (4 x predicted …, 3 x peak …).
- t = 230.1 s, step 3 (plan): step 3: chose exchange back-flush 1 x 39.7 uL resuspension_buffer at 60 uL/min; predicted {'time_s': 39.7, 'reagent_ul': 39.7, 'final_pct': 93.28, 'interior_pct': 33.4, 'peak_kpa': -3.0}. 8 candidates, 5 rejected (5 x lifts …).
- t = 269.8 s, step 4 (plan): step 4: chose release 58.8 uL at 40 uL/min; predicted {'time_s': 88.2, 'reagent_ul': 58.8, 'final_pct': 99.88, 'interior_pct': 83.8, 'peak_kpa': 0.0}. 9 candidates, 3 rejected (3 x recovers …).

## Full operator log

- `    0.0 s` **plan**: step 1: chose load 14.7 uL at 40 uL/min; predicted {'time_s': 22.1, 'reagent_ul': 14.7, 'final_pct': 63.21, 'interior_pct': 11.81, 'peak_kpa': 2.0}
- `    0.0 s` **safety**: approved: load 14.7 uL at 40 uL/min
- `   22.1 s` **plan**: step 2: chose exchange 3 x 39.7 uL wash_buffer at 40 uL/min + 29 s soak; predicted {'time_s': 208.0, 'reagent_ul': 119.1, 'final_pct': 99.97, 'interior_pct': 95.01, 'peak_kpa': 2.0}
- `   22.1 s` **safety**: approved: exchange 3 x 39.7 uL wash_buffer at 40 uL/min + 29 s soak
- `  230.1 s` **plan**: step 3: chose exchange back-flush 1 x 39.7 uL resuspension_buffer at 60 uL/min; predicted {'time_s': 39.7, 'reagent_ul': 39.7, 'final_pct': 93.28, 'interior_pct': 33.4, 'peak_kpa': -3.0}
- `  230.1 s` **safety**: approved: exchange back-flush 1 x 39.7 uL resuspension_buffer at 60 uL/min
- `  269.8 s` **plan**: step 4: chose release 58.8 uL at 40 uL/min; predicted {'time_s': 88.2, 'reagent_ul': 58.8, 'final_pct': 99.88, 'interior_pct': 83.8, 'peak_kpa': 0.0}
- `  269.8 s` **safety**: approved: release 58.8 uL at 40 uL/min

# Run report: Placeholder capsule wash (MVP) on wash_mvp_v1

> **SIMULATED HARDWARE.** This run used the virtual chip: no physical pump, valves or sensors. Values marked TRUE come from the simulation and could never be measured directly on real hardware.

- Scenario: `wash_mvp_open_loop`: No sensors: the operator runs on the model alone (open loop). Shows how far belief drifts from the truth.
- Operator: **planner** · Adapter: `virtual_chip` · Loop: **open**
- Sensors: none (open loop)
- Faults injected: none
- Status: **done** · Protocol time: 286.4 s
- Created: 2026-11-07T13:08:19Z

## Outcome

| | Believed (model + sensors) | TRUE (simulation) |
|---|---|---|
| Final exchange % (step 2) | 100.0% | 100.0% |
| Capsule recovery | 97.74% | 98.59% |
| Capsules collected | 9774 | 9859 |
| Capsules lost to waste | 0 | 0 |
| Capsules left on the mesh | 43 | 24 |
| Reagent used: capsule_suspension | 20.0 uL | 20.0 uL |
| Reagent used: resuspension_buffer | 49.4 uL | 49.4 uL |
| Reagent used: wash_buffer | 80.0 uL | 80.0 uL |
| Waste | 120.0 uL | 120.0 uL |

Alerts: **0** (none) · Recoveries: **0** · Model updates: **0**

## Belief vs truth drift

How far the operator's belief was from the simulated truth at the end of the run.

- Mesh resistance (model vs true): -23.1% at the start -> -23.1% at the end
- Chamber volume (model vs true): +11.1% at the start -> +11.1% at the end
- Final exchange %: off by 0.0 points
- Capsules collected: off by 85 capsules
- Capsules lost: off by 0 capsules
- Waste volume: off by 0.0 uL

## Steps

| Start | Step | Command as executed | Duration | Safety | Operator's explanation |
|---|---|---|---|---|---|
| 0.0 s | 1 | load 20 uL at 40 uL/min | 30.0 s | approved | Fastest safe option of 3 simulated (0 rejected): load 20 uL at 40 uL/min, predicted 30 s, 93.4% exchanged, peak 2.00 kPa. |
| 30.0 s | 2 | exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak | 192.3 s | approved | Fastest safe option of 16 simulated (7 rejected): exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak, predicted 192 s, 100.0% exchanged, peak 2.00 kPa. |
| 222.3 s | 3 | back-flush 1 x 20 uL resuspension_buffer at 60 uL/min | 20.0 s | approved | Fastest safe option of 8 simulated (5 rejected): exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min, predicted 20 s, 93.4% exchanged, peak -3.00 kPa. |
| 242.3 s | 4 | release 29.4 uL at 40 uL/min | 44.1 s | approved | Fastest safe option of 9 simulated (3 rejected): release 29.4 uL at 40 uL/min, predicted 44 s, 99.9% exchanged. |

## Alerts

None. (No sensors were on, so nothing could be detected.)

## Recoveries and operator interventions

None.

## Model updates

None (open loop: nothing to correct the model with).

## Optimizer decisions

- t = 0.0 s, step 1 (plan): step 1: chose load 20 uL at 40 uL/min; predicted {'time_s': 30.0, 'reagent_ul': 20.0, 'final_pct': 93.42, 'interior_pct': 26.82, 'peak_kpa': 2.0}. 3 candidates, 0 rejected.
- t = 30.0 s, step 2 (plan): step 2: chose exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak; predicted {'time_s': 192.3, 'reagent_ul': 80.0, 'final_pct': 100.0, 'interior_pct': 95.0, 'peak_kpa': 2.0}. 16 candidates, 7 rejected (4 x predicted …, 3 x peak …).
- t = 222.3 s, step 3 (plan): step 3: chose exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min; predicted {'time_s': 20.0, 'reagent_ul': 20.0, 'final_pct': 93.42, 'interior_pct': 19.01, 'peak_kpa': -3.0}. 8 candidates, 5 rejected (5 x lifts …).
- t = 242.3 s, step 4 (plan): step 4: chose release 29.4 uL at 40 uL/min; predicted {'time_s': 44.1, 'reagent_ul': 29.4, 'final_pct': 99.88, 'interior_pct': 60.44, 'peak_kpa': 0.0}. 9 candidates, 3 rejected (3 x recovers …).

## Full operator log

- `    0.0 s` **plan**: step 1: chose load 20 uL at 40 uL/min; predicted {'time_s': 30.0, 'reagent_ul': 20.0, 'final_pct': 93.42, 'interior_pct': 26.82, 'peak_kpa': 2.0}
- `    0.0 s` **safety**: approved: load 20 uL at 40 uL/min
- `   30.0 s` **plan**: step 2: chose exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak; predicted {'time_s': 192.3, 'reagent_ul': 80.0, 'final_pct': 100.0, 'interior_pct': 95.0, 'peak_kpa': 2.0}
- `   30.0 s` **safety**: approved: exchange 4 x 20 uL wash_buffer at 40 uL/min + 72 s soak
- `  222.3 s` **plan**: step 3: chose exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min; predicted {'time_s': 20.0, 'reagent_ul': 20.0, 'final_pct': 93.42, 'interior_pct': 19.01, 'peak_kpa': -3.0}
- `  222.3 s` **safety**: approved: exchange back-flush 1 x 20 uL resuspension_buffer at 60 uL/min
- `  242.3 s` **plan**: step 4: chose release 29.4 uL at 40 uL/min; predicted {'time_s': 44.1, 'reagent_ul': 29.4, 'final_pct': 99.88, 'interior_pct': 60.44, 'peak_kpa': 0.0}
- `  242.3 s` **safety**: approved: release 29.4 uL at 40 uL/min

# Run sheet: Capsule filter, lysis, reverse transcription and wash chip (`chip3_filter_lysis_rt_wash`)

**Purpose:** Removes aggregates from the capsules, then lyses the cells, washes, reverse transcribes and washes again in a trap.  
**Total time:** 2.1 h  
**Status:** generated; placeholder values: check flows, volumes and times before a real run.

Port states: **FEED** = a syringe pump pushes into that port; **VENT** = open outlet into a tube; **CAP** = plugged. Ports not named in a step are capped.

## Setup

### Ports and syringes

| Port | Type | Connect | Load (µL) | Syringe | Used in steps |
|---|---|---|---|---|---|
| `inlet_1` | inlet | syringe: capsules_suspension (from step 1) | 138 | 1 mL | 1 |
| `outlet_1` | outlet | tubing to tube labelled 'chip3_filter_lysis_rt_wash: capsules after step 19, for chip4_shell_release_bead_capture_wash_elution inlet_1' | | | 8 |
| `reagent_line_in` | inlet | syringe: lysis_buffer (from step 2), then wash_buffer (from step 4), then rt_mix (from step 5), then collection_buffer (from step 8); swap the syringe and prime the line between reagents | 90 / 900 / 90 / 120 | 1 mL / 1 mL / 1 mL / 1 mL | 2, 4, 7, 5, 8 |
| `reaction_trap_waste_out` | outlet | tubing to waste | | | 1, 2, 4, 5, 7 |

### Equipment

- syringe pump with 1 independent line
- heater / stage-top incubator (42 °C)
- tubing for every used port, plugs for capped ports, waste and collection tubes

Before step 1: fill every syringe, prime each feed line up to its port (no air), connect the outlet tubing, plug every port, then open only the ports step 1 needs.

## Steps

| # | IR step | Start | Module | What | Port states | Volume | Time | Temp | Watch |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 15 | 0:00:00 | aggregate_filter | Pass through | FEED inlet_1 20 µL/min (capsules_suspension) / VENT reaction_trap_waste_out / rest capped | 115 µL | 5.75 min |  | material moves from the inlet through aggregate_filter towards reaction_trap_waste_out; streams merge evenly; no clogging; stop the pumps once the material has reached reaction_trap (its next step) |
| 2 | 16 | 0:05:45 | reaction_trap | Exchange: lysis_buffer | FEED reagent_line_in 20 µL/min (lysis_buffer) / VENT reaction_trap_waste_out / rest capped | 75 µL | 3.75 min |  | lysis_buffer reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 3 | 16 | 0:09:30 | reaction_trap | Incubate | all capped |  | 10 min | 22 °C | temperature steady at 22 °C; nothing moving; capped ports not leaking |
| 4 | 17 | 0:19:30 | reaction_trap | Exchange: wash_buffer x3 | FEED reagent_line_in 20 µL/min (wash_buffer) / VENT reaction_trap_waste_out / rest capped | 450 µL | 22.5 min |  | wash_buffer reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 5 | 18 | 0:42:00 | reaction_trap | Exchange: rt_mix | FEED reagent_line_in 20 µL/min (rt_mix) / VENT reaction_trap_waste_out / rest capped | 75 µL | 3.75 min | 42 °C | rt_mix reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 6 | 18 | 0:45:45 | reaction_trap | Incubate | all capped |  | 1 h | 42 °C | temperature steady at 42 °C; nothing moving; capped ports not leaking |
| 7 | 19 | 1:45:45 | reaction_trap | Exchange: wash_buffer x2 | FEED reagent_line_in 20 µL/min (wash_buffer) / VENT reaction_trap_waste_out / rest capped | 300 µL | 15 min |  | wash_buffer reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 8 | 19 | 2:00:45 | reaction_trap | Collect the capsules (outlet_1) | FEED reagent_line_in 20 µL/min (collection_buffer) / VENT outlet_1 / rest capped | 100 µL | 5 min |  | the capsules leaves reaction_trap through outlet_1 into the tube labelled 'chip3_filter_lysis_rt_wash: capsules after step 19, for chip4_shell_release_bead_capture_wash_elution inlet_1'; never to waste |

## Flow check (operator/network.py)

| # | Through target (µL/min) | Max pressure (kPa) | Target shear (dyn/cm2) | Result |
|---|---|---|---|---|
| 1 | 20 | 0.0027 | 0.0178 | OK |
| 2 | 20 | 0.0027 | 0.00905 | OK |
| 3 | | | | no flow: every port capped |
| 4 | 20 | 0.0027 | 0.00905 | OK |
| 5 | 20 | 0.0027 | 0.00905 | OK |
| 6 | | | | no flow: every port capped |
| 7 | 20 | 0.0027 | 0.00905 | OK |
| 8 | 20 | 0.0023 | 0.00905 | OK |

## Digital twin replay

`runs/chip3_filter_lysis_rt_wash.twin_run.json` (8 events) stores the chip as the repo-relative path `autochip/examples/live/protocol_resin_multilayer/chips/chip3_filter_lysis_rt_wash.json`, so the twin can load and replay it.

## Checks

- **warning**: step 16: module reaction_trap has no inlet named for lysis_buffer; it goes in through reagent_line_in (swap that syringe and prime the line first)
- **warning**: step 17: module reaction_trap has no inlet named for wash_buffer; it goes in through reagent_line_in (swap that syringe and prime the line first)
- **warning**: step 18: module reaction_trap has no inlet named for rt_mix; it goes in through reagent_line_in (swap that syringe and prime the line first)
- **warning**: step 19: module reaction_trap has no inlet named for wash_buffer; it goes in through reagent_line_in (swap that syringe and prime the line first)
- **info**: step 15 (Pass through): the outflow passes reaction_trap on its way to reaction_trap_waste_out
- **info**: the operator's planner (operator/planner.py) accepts this protocol: 8 events in runs/chip3_filter_lysis_rt_wash.twin_run.json, chip stored as autochip/examples/live/protocol_resin_multilayer/chips/chip3_filter_lysis_rt_wash.json

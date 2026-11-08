# Run sheet: Lysis, wash and reverse transcription chip (`chip3_lysis_wash_rt`)

**Purpose:** Lyses the cells, washes, and runs reverse transcription inside the capsules held in one trap.  
**Total time:** 2.06 h  
**Status:** generated; placeholder values: check flows, volumes and times before a real run.

Port states: **FEED** = a syringe pump pushes into that port; **VENT** = open outlet into a tube; **CAP** = plugged. Ports not named in a step are capped.

## Setup

### Ports and syringes

| Port | Type | Connect | Load (µL) | Syringe | Used in steps |
|---|---|---|---|---|---|
| `inlet_1` | inlet | syringe: capsules_sample (from step 1), then lysis_buffer (from step 2), then wash_buffer (from step 4), then rt_mix (from step 5); swap the syringe and prime the line between reagents | 90 / 90 / 900 / 90 | 1 mL / 1 mL / 1 mL / 1 mL | 1, 2, 4, 7, 5 |
| `outlet_1` | outlet | tubing to tube labelled 'chip3_lysis_wash_rt: capsules after step 19' | | | 8 |
| `reagent_line_in` | inlet | syringe: collection_buffer (from step 8) | 120 | 1 mL | 8 |
| `trap_waste_out` | outlet | tubing to waste | | | 1, 2, 4, 5, 7 |

### Equipment

- syringe pump with 1 independent line
- heater / stage-top incubator (42 °C)
- tubing for every used port, plugs for capped ports, waste and collection tubes

Before step 1: fill every syringe, prime each feed line up to its port (no air), connect the outlet tubing, plug every port, then open only the ports step 1 needs.

## Steps

| # | IR step | Start | Module | What | Port states | Volume | Time | Temp | Watch |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 16 | 0:00:00 | lysis_rt_trap | Load capsules from the hand-off from chip2_emulsion_break_curing_wash_filter | FEED inlet_1 20 µL/min (capsules_sample) / VENT trap_waste_out / rest capped | 75 µL | 3.75 min |  | the capsules arrives in lysis_rt_trap and stays there; liquid leaves at trap_waste_out |
| 2 | 16 | 0:03:45 | lysis_rt_trap | Exchange: lysis_buffer | FEED inlet_1 20 µL/min (lysis_buffer) / VENT trap_waste_out / rest capped | 75 µL | 3.75 min |  | lysis_buffer reaches lysis_rt_trap and leaves at trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 3 | 16 | 0:07:30 | lysis_rt_trap | Incubate | all capped |  | 10 min | 22 °C | temperature steady at 22 °C; nothing moving; capped ports not leaking |
| 4 | 17 | 0:17:30 | lysis_rt_trap | Exchange: wash_buffer x3 | FEED inlet_1 20 µL/min (wash_buffer) / VENT trap_waste_out / rest capped | 450 µL | 22.5 min |  | wash_buffer reaches lysis_rt_trap and leaves at trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 5 | 18 | 0:40:00 | lysis_rt_trap | Exchange: rt_mix | FEED inlet_1 20 µL/min (rt_mix) / VENT trap_waste_out / rest capped | 75 µL | 3.75 min | 42 °C | rt_mix reaches lysis_rt_trap and leaves at trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 6 | 18 | 0:43:45 | lysis_rt_trap | Incubate | all capped |  | 1 h | 42 °C | temperature steady at 42 °C; nothing moving; capped ports not leaking |
| 7 | 19 | 1:43:45 | lysis_rt_trap | Exchange: wash_buffer x2 | FEED inlet_1 20 µL/min (wash_buffer) / VENT trap_waste_out / rest capped | 300 µL | 15 min |  | wash_buffer reaches lysis_rt_trap and leaves at trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 8 | 19 | 1:58:45 | lysis_rt_trap | Collect the capsules (outlet_1) | FEED reagent_line_in 20 µL/min (collection_buffer) / VENT outlet_1 / rest capped | 100 µL | 5 min |  | the capsules leaves lysis_rt_trap through outlet_1 into the tube labelled 'chip3_lysis_wash_rt: capsules after step 19'; never to waste |

## Flow check (operator/network.py)

| # | Through target (µL/min) | Max pressure (kPa) | Target shear (dyn/cm2) | Result |
|---|---|---|---|---|
| 1 | 20 | 0.0044 | 0.0195 | OK |
| 2 | 20 | 0.0044 | 0.0195 | OK |
| 3 | | | | no flow: every port capped |
| 4 | 20 | 0.0044 | 0.0195 | OK |
| 5 | 20 | 0.0044 | 0.0195 | OK |
| 6 | | | | no flow: every port capped |
| 7 | 20 | 0.0044 | 0.0195 | OK |
| 8 | 20 | 0.0045 | 0.0195 | OK |

## Digital twin replay

`runs/chip3_lysis_wash_rt.twin_run.json` (8 events) stores the chip as the repo-relative path `autochip/examples/live/paper_assayathon_demo/chips/chip3_lysis_wash_rt.json`, so the twin can load and replay it.

## Checks

- **info**: step 16: module lysis_rt_trap has no inlet named for lysis_buffer; it goes in through inlet_1 (swap that syringe and prime the line first)
- **info**: step 17: module lysis_rt_trap has no inlet named for wash_buffer; it goes in through inlet_1 (swap that syringe and prime the line first)
- **info**: step 18: module lysis_rt_trap has no inlet named for rt_mix; it goes in through inlet_1 (swap that syringe and prime the line first)
- **info**: step 19: module lysis_rt_trap has no inlet named for wash_buffer; it goes in through inlet_1 (swap that syringe and prime the line first)
- **info**: the operator's planner (operator/planner.py) accepts this protocol: 8 events in runs/chip3_lysis_wash_rt.twin_run.json, chip stored as autochip/examples/live/paper_assayathon_demo/chips/chip3_lysis_wash_rt.json

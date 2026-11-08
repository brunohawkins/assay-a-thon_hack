# Run sheet: Emulsion break, curing, wash and capsule filter chip (`chip2_emulsion_break_curing_wash_filter`)

**Purpose:** Breaks the emulsion, photocures the shells, washes the capsules and removes aggregates.  
**Total time:** 1.29 h  
**Status:** generated; placeholder values: check flows, volumes and times before a real run.

Port states: **FEED** = a syringe pump pushes into that port; **VENT** = open outlet into a tube; **CAP** = plugged. Ports not named in a step are capped.

## Setup

### Ports and syringes

| Port | Type | Connect | Load (µL) | Syringe | Used in steps |
|---|---|---|---|---|---|
| `inlet_1` | inlet | syringe: droplets_sample (from step 1), then photoinitiator (from step 3), then wash_buffer (from step 5), then carrier_buffer (from step 6); swap the syringe and prime the line between reagents | 90 / 90 / 540 / 138 | 1 mL / 1 mL / 1 mL / 1 mL | 1, 3, 5, 6 |
| `outlet_1` | outlet | tubing to tube labelled 'chip2_emulsion_break_curing_wash_filter: capsules after step 15, for chip3_lysis_wash_rt inlet_1' | | | 6 |
| `emulsion_breaking_agent_in` | inlet | syringe: emulsion_breaking_agent (from step 2) | 180 | 1 mL | 2 |
| `reagent_line_in` | inlet | plug (not used) | | | |
| `trap_waste_out` | outlet | tubing to waste | | | 1, 2, 3, 5 |

### Equipment

- syringe pump with 1 independent line
- LED / lamp at 405 nm
- tubing for every used port, plugs for capped ports, waste and collection tubes

Before step 1: fill every syringe, prime each feed line up to its port (no air), connect the outlet tubing, plug every port, then open only the ports step 1 needs.

## Steps

| # | IR step | Start | Module | What | Port states | Volume | Time | Temp | Watch |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 12 | 0:00:00 | capsule_trap | Load droplets from the hand-off from chip1_encapsulation | FEED inlet_1 5 µL/min (droplets_sample) / VENT trap_waste_out / rest capped | 75 µL | 15 min |  | the droplets arrives in capsule_trap and stays there; liquid leaves at trap_waste_out |
| 2 | 12 | 0:15:00 | capsule_trap | Exchange: emulsion_breaking_agent | FEED emulsion_breaking_agent_in 5 µL/min (emulsion_breaking_agent) / VENT trap_waste_out / rest capped | 150 µL | 30 min |  | emulsion_breaking_agent reaches capsule_trap and leaves at trap_waste_out; no bubbles in the chamber |
| 3 | 13 | 0:45:00 | capsule_trap | Exchange: photoinitiator | FEED inlet_1 20 µL/min (photoinitiator) / VENT trap_waste_out / rest capped | 75 µL | 3.75 min |  | photoinitiator reaches capsule_trap and leaves at trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 4 | 13 | 0:48:45 | capsule_trap | Cure (405 nm) (light 405 nm) | all capped |  | 30 s |  | 405 nm light over capsule_trap for 30 s; nothing flows |
| 5 | 14 | 0:49:15 | capsule_trap | Exchange: wash_buffer x3 | FEED inlet_1 20 µL/min (wash_buffer) / VENT trap_waste_out / rest capped | 450 µL | 22.5 min |  | wash_buffer reaches capsule_trap and leaves at trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 6 | 15 | 1:11:45 | aggregate_filter | Pass through | FEED inlet_1 20 µL/min (carrier_buffer) / VENT outlet_1 / rest capped | 115 µL | 5.75 min |  | material moves from capsule_trap through aggregate_filter towards outlet_1; streams merge evenly; no clogging; collect into the tube labelled 'chip2_emulsion_break_curing_wash_filter: capsules after step 15, for chip3_lysis_wash_rt inlet_1' |

## Flow check (operator/network.py)

| # | Through target (µL/min) | Max pressure (kPa) | Target shear (dyn/cm2) | Result |
|---|---|---|---|---|
| 1 | 5 | 0.0012 | 0.00344 | OK |
| 2 | 5 | 0.0018 | 0.00344 | OK |
| 3 | 20 | 0.0046 | 0.0138 | OK |
| 4 | | | | no flow: every port capped |
| 5 | 20 | 0.0046 | 0.0138 | OK |
| 6 | 20 | 0.0027 | 0.0178 | OK |

## Digital twin replay

`runs/chip2_emulsion_break_curing_wash_filter.twin_run.json` (6 events) stores the chip as the repo-relative path `autochip/examples/live/paper_assayathon_demo/chips/chip2_emulsion_break_curing_wash_filter.json`, so the twin can load and replay it.

## Checks

- **info**: step 13: module capsule_trap has no inlet named for photoinitiator; it goes in through inlet_1 (swap that syringe and prime the line first)
- **info**: step 14: module capsule_trap has no inlet named for wash_buffer; it goes in through inlet_1 (swap that syringe and prime the line first)
- **info**: the operator's planner (operator/planner.py) accepts this protocol: 6 events in runs/chip2_emulsion_break_curing_wash_filter.twin_run.json, chip stored as autochip/examples/live/paper_assayathon_demo/chips/chip2_emulsion_break_curing_wash_filter.json

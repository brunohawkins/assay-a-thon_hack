# Run sheet: Gelation, emulsion breaking, curing and capsule wash chip (`chip2_gelation_emulsion_break_cure_wash`)

**Purpose:** Gels the shells in a cold delay line, then breaks the emulsion, photocures and washes the capsules in a trap.  
**Total time:** 3.61 h  
**Status:** generated; placeholder values: check flows, volumes and times before a real run.

Port states: **FEED** = a syringe pump pushes into that port; **VENT** = open outlet into a tube; **CAP** = plugged. Ports not named in a step are capped.

## Setup

### Ports and syringes

| Port | Type | Connect | Load (µL) | Syringe | Used in steps |
|---|---|---|---|---|---|
| `inlet_1` | inlet | syringe: droplets_sample (from step 1), then carrier_buffer (from step 3); swap the syringe and prime the line between reagents | 270 / 480 | 1 mL / 1 mL | 1, 3 |
| `outlet_1` | outlet | tubing to tube labelled 'chip2_gelation_emulsion_break_cure_wash: capsules after step 14, for chip3_filter_lysis_rt_wash inlet_1' | | | 8 |
| `emulsion_breaking_agent_in` | inlet | syringe: emulsion_breaking_agent (from step 4) | 180 | 1 mL | 4 |
| `photoinitiator_in` | inlet | syringe: photoinitiator (from step 5) | 90 | 1 mL | 5 |
| `capsule_trap_waste_out` | outlet | tubing to waste | | | 1, 3, 4, 5, 7 |
| `wash_buffer_in` | inlet | syringe: wash_buffer (from step 7), then collection_buffer (from step 8); swap the syringe and prime the line between reagents | 540 / 120 | 1 mL / 1 mL | 7, 8 |

### Equipment

- syringe pump with 1 independent line
- cold plate (4 °C)
- LED / lamp at 405 nm
- tubing for every used port, plugs for capped ports, waste and collection tubes

Before step 1: fill every syringe, prime each feed line up to its port (no air), connect the outlet tubing, plug every port, then open only the ports step 1 needs.

## Steps

| # | IR step | Start | Module | What | Port states | Volume | Time | Temp | Watch |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 11 | 0:00:00 | gelation_delay | Load droplets from the hand-off from chip1_rinse_dissociation_quench_wash_encapsulation | FEED inlet_1 5 µL/min (droplets_sample) / VENT capsule_trap_waste_out / rest capped | 225 µL | 45 min |  | the droplets arrives in gelation_delay; stop the pump as soon as it is there, before it runs on towards capsule_trap_waste_out |
| 2 | 11 | 0:45:00 | gelation_delay | Hold | all capped |  | 30 min | 4 °C | temperature steady at 4 °C; nothing moving; capped ports not leaking |
| 3 | 12 | 1:15:00 | capsule_trap | Move droplets: gelation_delay → capsule_trap | FEED inlet_1 5 µL/min (carrier_buffer) / VENT capsule_trap_waste_out / rest capped | 400 µL | 1.33 h |  | the droplets arrives in capsule_trap and stays there; liquid leaves at capsule_trap_waste_out |
| 4 | 12 | 2:35:00 | capsule_trap | Exchange: emulsion_breaking_agent | FEED emulsion_breaking_agent_in 5 µL/min (emulsion_breaking_agent) / VENT capsule_trap_waste_out / rest capped | 150 µL | 30 min |  | emulsion_breaking_agent reaches capsule_trap and leaves at capsule_trap_waste_out; no bubbles in the chamber |
| 5 | 13 | 3:05:00 | capsule_trap | Exchange: photoinitiator | FEED photoinitiator_in 20 µL/min (photoinitiator) / VENT capsule_trap_waste_out / rest capped | 75 µL | 3.75 min |  | photoinitiator reaches capsule_trap and leaves at capsule_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 6 | 13 | 3:08:45 | capsule_trap | Cure (405 nm) (light 405 nm) | all capped |  | 30 s | 4 °C | 405 nm light over capsule_trap for 30 s; nothing flows |
| 7 | 14 | 3:09:15 | capsule_trap | Exchange: wash_buffer x3 | FEED wash_buffer_in 20 µL/min (wash_buffer) / VENT capsule_trap_waste_out / rest capped | 450 µL | 22.5 min |  | wash_buffer reaches capsule_trap and leaves at capsule_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 8 | 14 | 3:31:45 | capsule_trap | Collect the capsules (outlet_1) | FEED wash_buffer_in 20 µL/min (collection_buffer) / VENT outlet_1 / rest capped | 100 µL | 5 min |  | the capsules leaves capsule_trap through outlet_1 into the tube labelled 'chip2_gelation_emulsion_break_cure_wash: capsules after step 14, for chip3_filter_lysis_rt_wash inlet_1'; never to waste |

## Flow check (operator/network.py)

| # | Through target (µL/min) | Max pressure (kPa) | Target shear (dyn/cm2) | Result |
|---|---|---|---|---|
| 1 | 5 | 0.002 | 0.00129 | OK |
| 2 | | | | no flow: every port capped |
| 3 | 5 | 0.002 | 0.00226 | OK |
| 4 | 5 | 0.0031 | 0.00226 | OK |
| 5 | 20 | 0.0124 | 0.00905 | OK |
| 6 | | | | no flow: every port capped |
| 7 | 20 | 0.0124 | 0.00905 | OK |
| 8 | 20 | 0.0074 | 0.00905 | OK |

## Digital twin replay

`runs/chip2_gelation_emulsion_break_cure_wash.twin_run.json` (8 events) stores the chip as the repo-relative path `autochip/examples/live/protocol_resin_multilayer/chips/chip2_gelation_emulsion_break_cure_wash.json`, so the twin can load and replay it.

## Checks

- **info**: step 11 (Load droplets from the hand-off from chip1_rinse_dissociation_quench_wash_encapsulation): the outflow passes capsule_trap on its way to capsule_trap_waste_out
- **info**: the operator's planner (operator/planner.py) accepts this protocol: 8 events in runs/chip2_gelation_emulsion_break_cure_wash.twin_run.json, chip stored as autochip/examples/live/protocol_resin_multilayer/chips/chip2_gelation_emulsion_break_cure_wash.json

# Run sheet: Tri-culture, dissociation, quench, cell wash and filter chip (`chip1_triculture_dissociation_quench_cell_wash_filter`)

**Purpose:** Grows three iPSC-derived populations in separate chambers (apart, then sharing medium through the central junction), detaches them, quenches the enzyme, moves the cells into clean buffer and filters out clumps.  
**Total time:** 9.11 d  
**Status:** generated; placeholder values: check flows, volumes and times before a real run.

Port states: **FEED** = a syringe pump pushes into that port; **VENT** = open outlet into a tube; **CAP** = plugged. Ports not named in a step are capped.

## Setup

### Ports and syringes

| Port | Type | Connect | Load (µL) | Syringe | Used in steps |
|---|---|---|---|---|---|
| `inlet_1` | inlet | syringe: carrier_buffer (from step 11) | 440 | 1 mL | 11, 15 |
| `outlet_1` | outlet | tubing to tube labelled 'chip1_triculture_dissociation_quench_cell_wash_filter: cells after step 7, for chip2_encapsulation_emulsion_break_capsule_wash_curing inlet_1' | | | 15 |
| `culture_medium_neurons_in` | inlet | syringe: cells_for_culture_neurons (from step 1), then culture_medium_neurons (from step 4), then dissociation_enzyme (from step 7); swap the syringe and prime the line between reagents | 180 / 1980 / 180 | 1 mL / 3 mL / 1 mL | 1, 4, 5, 6, 7 |
| `culture_neurons_vent_out` | outlet | tubing to waste | | | 1, 4, 5, 7 |
| `culture_medium_microglia_in` | inlet | syringe: cells_for_culture_microglia (from step 2), then culture_medium_microglia (from step 4), then dissociation_enzyme (from step 8); swap the syringe and prime the line between reagents | 180 / 1620 / 180 | 1 mL / 3 mL / 1 mL | 2, 4, 5, 8 |
| `microglia_vent_out` | outlet | tubing to waste | | | 2, 4, 5, 6, 8 |
| `culture_microglia_vent_out` | outlet | plug (not used) | | | |
| `culture_medium_astrocytes_in` | inlet | syringe: cells_for_culture_astrocytes (from step 3), then culture_medium_astrocytes (from step 4), then dissociation_enzyme (from step 9); swap the syringe and prime the line between reagents | 180 / 1620 / 180 | 1 mL / 3 mL / 1 mL | 3, 4, 5, 9 |
| `astrocytes_vent_out` | outlet | tubing to waste | | | 3, 4, 5, 6, 9 |
| `culture_astrocytes_vent_out` | outlet | plug (not used) | | | |
| `quench_solution_in` | inlet | syringe: quench_solution (from step 11) | 341 | 1 mL | 11 |
| `wash_buffer_in` | inlet | syringe: wash_buffer (from step 12) | 221 | 1 mL | 12, 14 |
| `cell_wash_waste_out` | outlet | tubing to waste | | | 11, 12, 14 |

### Equipment

- syringe pump with 3 independent lines
- heater / stage-top incubator (37 °C)
- microscope
- tubing for every used port, plugs for capped ports, waste and collection tubes

Before step 1: fill every syringe, prime each feed line up to its port (no air), connect the outlet tubing, plug every port, then open only the ports step 1 needs.

## Steps

| # | IR step | Start | Module | What | Port states | Volume | Time | Temp | Watch |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 1 | 0:00:00 | culture_neurons | Load cells from the bench | FEED culture_medium_neurons_in 10 µL/min (cells_for_culture_neurons) / VENT culture_neurons_vent_out / rest capped | 150 µL | 15 min |  | the cells arrives in culture_neurons and stays there; liquid leaves at culture_neurons_vent_out |
| 2 | 1 | 0:15:00 | culture_microglia | Load cells from the bench | FEED culture_medium_microglia_in 10 µL/min (cells_for_culture_microglia) / VENT microglia_vent_out / rest capped | 150 µL | 15 min |  | the cells arrives in culture_microglia and stays there; liquid leaves at microglia_vent_out |
| 3 | 1 | 0:30:00 | culture_astrocytes | Load cells from the bench | FEED culture_medium_astrocytes_in 10 µL/min (cells_for_culture_astrocytes) / VENT astrocytes_vent_out / rest capped | 150 µL | 15 min |  | the cells arrives in culture_astrocytes and stays there; liquid leaves at astrocytes_vent_out |
| 4 | 1 | 0:45:00 | culture_neurons | Culture apart 7 d (3 chambers, own medium each, change every 1 d) (exchange every 1 d) | FEED culture_medium_neurons_in 10 µL/min (culture_medium_neurons); culture_medium_microglia_in 10 µL/min (culture_medium_microglia); culture_medium_astrocytes_in 10 µL/min (culture_medium_astrocytes) / VENT culture_neurons_vent_out, microglia_vent_out, astrocytes_vent_out / rest capped | 3150 µL | 7 d | 37 °C | every 1 d, all 3 pumps together: each chamber's medium leaves through its own vent and nothing crosses the junction; check each culture on the microscope |
| 5 | 2 | 7 d 00:45 | culture_neurons | Culture apart 1 d (3 chambers, own medium each, change every 12 h) (exchange every 12 h) | FEED culture_medium_neurons_in 10 µL/min (culture_medium_neurons); culture_medium_microglia_in 10 µL/min (culture_medium_microglia); culture_medium_astrocytes_in 10 µL/min (culture_medium_astrocytes) / VENT culture_neurons_vent_out, microglia_vent_out, astrocytes_vent_out / rest capped | 900 µL | 1 d | 37 °C | every 12 h, all 3 pumps together: each chamber's medium leaves through its own vent and nothing crosses the junction; check each culture on the microscope |
| 6 | 2 | 8 d 00:45 | culture_neurons | Culture sharing medium 1 d (feed culture_neurons, vent the others) (exchange every 12 h) | FEED culture_medium_neurons_in 10 µL/min (culture_medium_neurons) / VENT microglia_vent_out, astrocytes_vent_out / rest capped | 300 µL | 1 d | 37 °C | medium from culture_neurons reaches the other chambers through the junction and leaves at microglia_vent_out, astrocytes_vent_out |
| 7 | 3 | 9 d 00:45 | culture_neurons | Exchange: dissociation_enzyme | FEED culture_medium_neurons_in 10 µL/min (dissociation_enzyme) / VENT culture_neurons_vent_out / rest capped | 150 µL | 15 min | 37 °C | dissociation_enzyme reaches culture_neurons and leaves at culture_neurons_vent_out; no bubbles in the chamber |
| 8 | 3 | 9 d 01:00 | culture_microglia | Exchange: dissociation_enzyme | FEED culture_medium_microglia_in 10 µL/min (dissociation_enzyme) / VENT microglia_vent_out / rest capped | 150 µL | 15 min | 37 °C | dissociation_enzyme reaches culture_microglia and leaves at microglia_vent_out; no bubbles in the chamber |
| 9 | 3 | 9 d 01:15 | culture_astrocytes | Exchange: dissociation_enzyme | FEED culture_medium_astrocytes_in 10 µL/min (dissociation_enzyme) / VENT astrocytes_vent_out / rest capped | 150 µL | 15 min | 37 °C | dissociation_enzyme reaches culture_astrocytes and leaves at astrocytes_vent_out; no bubbles in the chamber |
| 10 | 3 | 9 d 01:30 | culture_astrocytes | Incubate | all capped |  | 10 min | 37 °C | temperature steady at 37 °C; nothing moving; capped ports not leaking |
| 11 | 4 | 9 d 01:40 | quench_join | Mix quench_solution into the stream | FEED inlet_1 10 µL/min (carrier_buffer); quench_solution_in 10 µL/min (quench_solution) / VENT cell_wash_waste_out / rest capped | 567.8 µL | 28.4 min |  | material moves from culture_neurons through quench_join towards cell_wash_waste_out; streams merge evenly; no clogging; stop the pumps once the material has reached cell_wash (its next step) |
| 12 | 5 | 9 d 02:08 | cell_wash | Exchange: wash_buffer | FEED wash_buffer_in 10 µL/min (wash_buffer) / VENT cell_wash_waste_out / rest capped | 61.4 µL | 6.14 min |  | wash_buffer reaches cell_wash and leaves at cell_wash_waste_out; no bubbles in the chamber; cells stay in place |
| 13 | 5 | 9 d 02:14 | cell_wash | Incubate | all capped |  | 5 min | 22 °C | temperature steady at 22 °C; nothing moving; capped ports not leaking |
| 14 | 6 | 9 d 02:19 | cell_wash | Exchange: wash_buffer | FEED wash_buffer_in 10 µL/min (wash_buffer) / VENT cell_wash_waste_out / rest capped | 122.9 µL | 12.3 min |  | wash_buffer reaches cell_wash and leaves at cell_wash_waste_out; no bubbles in the chamber; cells stay in place |
| 15 | 7 | 9 d 02:31 | cell_filter | Pass through | FEED inlet_1 10 µL/min (carrier_buffer) / VENT outlet_1 / rest capped | 83.1 µL | 8.31 min |  | material moves from cell_wash through cell_filter towards outlet_1; streams merge evenly; no clogging; collect into the tube labelled 'chip1_triculture_dissociation_quench_cell_wash_filter: cells after step 7, for chip2_encapsulation_emulsion_break_capsule_wash_curing inlet_1' |

## Flow check (operator/network.py)

| # | Through target (µL/min) | Max pressure (kPa) | Target shear (dyn/cm2) | Result |
|---|---|---|---|---|
| 1 | 10 | 0.0022 | 0.0032 | OK |
| 2 | 10 | 0.0022 | 0.0032 | OK |
| 3 | 10 | 0.0022 | 0.0032 | OK |
| 4 | 10 | 0.0022 | 0.0032 | OK |
| 5 | 10 | 0.0022 | 0.0032 | OK |
| 6 | 10 | 0.0022 | 0.0032 | OK |
| 7 | 10 | 0.0022 | 0.0032 | OK |
| 8 | 10 | 0.0022 | 0.0032 | OK |
| 9 | 10 | 0.0022 | 0.0032 | OK |
| 10 | | | | no flow: every port capped |
| 11 | 20 | 0.0428 | 0.0988 | OK |
| 12 | 10 | 0.0239 | 0.0025 | OK |
| 13 | | | | no flow: every port capped |
| 14 | 10 | 0.0239 | 0.0025 | OK |
| 15 | 10 | 0.0064 | 0.0444 | OK |

## Digital twin replay

`runs/chip1_triculture_dissociation_quench_cell_wash_filter.twin_run.json` (15 events) stores the chip as the repo-relative path `autochip/examples/live/prompt_unconstrained/chips/chip1_triculture_dissociation_quench_cell_wash_filter.json`, so the twin can load and replay it.

## Checks

- **warning**: step 3: module culture_neurons has no inlet named for dissociation_enzyme; it goes in through culture_medium_neurons_in (swap that syringe and prime the line first)
- **warning**: step 3: module culture_microglia has no inlet named for dissociation_enzyme; it goes in through culture_medium_microglia_in (swap that syringe and prime the line first)
- **warning**: step 3: module culture_astrocytes has no inlet named for dissociation_enzyme; it goes in through culture_medium_astrocytes_in (swap that syringe and prime the line first)
- **info**: step 4 (Mix quench_solution into the stream): the outflow passes quench_mix, cell_wash on its way to cell_wash_waste_out
- **info**: the operator's planner (operator/planner.py) accepts this protocol: 15 events in runs/chip1_triculture_dissociation_quench_cell_wash_filter.twin_run.json, chip stored as autochip/examples/live/prompt_unconstrained/chips/chip1_triculture_dissociation_quench_cell_wash_filter.json

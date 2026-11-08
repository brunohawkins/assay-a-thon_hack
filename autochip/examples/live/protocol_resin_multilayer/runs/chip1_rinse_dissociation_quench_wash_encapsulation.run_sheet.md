# Run sheet: Cell rinse, dissociation, quench, wash and encapsulation chip (`chip1_rinse_dissociation_quench_wash_encapsulation`)

**Purpose:** Rinses and detaches the cells, quenches the enzyme, washes the cells in a spiral, mixes in core solution and forms ~70 um droplets at the flow-focusing junction.  
**Total time:** 4.22 h  
**Status:** generated; placeholder values: check flows, volumes and times before a real run.

Port states: **FEED** = a syringe pump pushes into that port; **VENT** = open outlet into a tube; **CAP** = plugged. Ports not named in a step are capped.

## Setup

### Ports and syringes

| Port | Type | Connect | Load (µL) | Syringe | Used in steps |
|---|---|---|---|---|---|
| `inlet_1` | inlet | syringe: cells_sample (from step 1), then wash_buffer (from step 2), then dissociation_enzyme (from step 3), then carrier_buffer (from step 5), then core_solution (from step 10); swap the syringe and prime the line between reagents | 180 / 360 / 180 / 1080 / 72 | 1 mL / 1 mL / 1 mL / 3 mL / 1 mL | 1, 2, 3, 5, 9, 10 |
| `outlet_1` | outlet | tubing to tube labelled 'chip1_rinse_dissociation_quench_wash_encapsulation: droplets after step 10, for chip2_gelation_emulsion_break_cure_wash inlet_1'; waste tube | | | 9, 10 |
| `reagent_line_in` | inlet | plug (not used) | | | |
| `culture_1_waste_out` | outlet | tubing to waste | | | 1, 2, 3 |
| `quench_medium_in` | inlet | syringe: quench_medium (from step 5) | 450 | 1 mL | 5 |
| `wash_buffer_in` | inlet | syringe: wash_buffer (from step 6) | 762 | 1 mL | 6, 8 |
| `cell_wash_waste_out` | outlet | tubing to waste | | | 5, 6, 8 |
| `core_solution_in` | inlet | syringe: core_solution (from step 9) | 630 | 1 mL | 9 |
| `shell_polymer_in` | inlet | syringe: shell_polymer (from step 10) | 72 | 1 mL | 10 |
| `fluorinated_oil_in` | inlet | syringe: fluorinated_oil (from step 10) | 360 | 1 mL | 10 |

### Equipment

- syringe pump with 3 independent lines
- heater / stage-top incubator (37 °C)
- syringe heater / warm jacket on the shell_polymer syringe and line (37 °C)
- microscope
- tubing for every used port, plugs for capped ports, waste and collection tubes

Before step 1: fill every syringe, prime each feed line up to its port (no air), connect the outlet tubing, plug every port, then open only the ports step 1 needs.

## Steps

| # | IR step | Start | Module | What | Port states | Volume | Time | Temp | Watch |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 2 | 0:00:00 | culture_1 | Load cells from the bench | FEED inlet_1 10 µL/min (cells_sample) / VENT culture_1_waste_out / rest capped | 150 µL | 15 min |  | the cells arrives in culture_1 and stays there; liquid leaves at culture_1_waste_out |
| 2 | 2 | 0:15:00 | culture_1 | Exchange: wash_buffer | FEED inlet_1 10 µL/min (wash_buffer) / VENT culture_1_waste_out / rest capped | 300 µL | 30 min |  | wash_buffer reaches culture_1 and leaves at culture_1_waste_out; no bubbles in the chamber; cells stay in place |
| 3 | 3 | 0:45:00 | culture_1 | Exchange: dissociation_enzyme | FEED inlet_1 10 µL/min (dissociation_enzyme) / VENT culture_1_waste_out / rest capped | 150 µL | 15 min | 37 °C | dissociation_enzyme reaches culture_1 and leaves at culture_1_waste_out; no bubbles in the chamber |
| 4 | 3 | 1:00:00 | culture_1 | Incubate | all capped |  | 5 min | 37 °C | temperature steady at 37 °C; nothing moving; capped ports not leaking |
| 5 | 4 | 1:05:00 | quench_join | Mix quench_medium into the stream | FEED inlet_1 10 µL/min (carrier_buffer); quench_medium_in 10 µL/min (quench_medium) / VENT cell_wash_waste_out / rest capped | 749.4 µL | 37.5 min |  | material moves from culture_1 through quench_join towards cell_wash_waste_out; streams merge evenly; no clogging; stop the pumps once the material has reached cell_wash (its next step) |
| 6 | 5 | 1:42:28 | cell_wash | Exchange: wash_buffer | FEED wash_buffer_in 10 µL/min (wash_buffer) / VENT cell_wash_waste_out / rest capped | 127 µL | 12.7 min |  | wash_buffer reaches cell_wash and leaves at cell_wash_waste_out; no bubbles in the chamber; cells stay in place |
| 7 | 5 | 1:55:10 | cell_wash | Incubate | all capped |  | 5 min | 22 °C | temperature steady at 22 °C; nothing moving; capped ports not leaking |
| 8 | 6 | 2:00:10 | cell_wash | Exchange: wash_buffer x2 | FEED wash_buffer_in 10 µL/min (wash_buffer) / VENT cell_wash_waste_out / rest capped | 507.8 µL | 50.8 min |  | wash_buffer reaches cell_wash and leaves at cell_wash_waste_out; no bubbles in the chamber; cells stay in place |
| 9 | 9 | 2:50:57 | core_join | Mix core_solution into the stream | FEED inlet_1 10 µL/min (carrier_buffer); core_solution_in 10 µL/min (core_solution) / VENT outlet_1 / rest capped | 1050.2 µL | 52.5 min |  | material moves from cell_wash through core_join towards outlet_1; streams merge evenly; no clogging; stop the pumps once the material has reached droplets_1 (its next step) |
| 10 | 10 | 3:43:28 | droplets_1 | Encapsulate (co-flow) | FEED inlet_1 2 µL/min (core_solution); shell_polymer_in 2 µL/min (shell_polymer); fluorinated_oil_in 10 µL/min (fluorinated_oil) / VENT outlet_1 / rest capped | 420 µL | 30 min |  | steady droplet break-up at droplets_1 (microscope); droplets travel on towards outlet_1; no oil backing up into the core line; keep shell_polymer_in (shell_polymer) warm (37 °C syringe heater) up to the chip; the cold zone starts downstream; collect into the tube labelled 'chip1_rinse_dissociation_quench_wash_encapsulation: droplets after step 10, for chip2_gelation_emulsion_break_cure_wash inlet_1' |

## Flow check (operator/network.py)

| # | Through target (µL/min) | Max pressure (kPa) | Target shear (dyn/cm2) | Result |
|---|---|---|---|---|
| 1 | 10 | 0.0008 | 0.0032 | OK |
| 2 | 10 | 0.0008 | 0.0032 | OK |
| 3 | 10 | 0.0008 | 0.0032 | OK |
| 4 | | | | no flow: every port capped |
| 5 | 20 | 0.0115 | 0.0593 | OK |
| 6 | 10 | 0.0007 | 0.00174 | OK |
| 7 | | | | no flow: every port capped |
| 8 | 10 | 0.0007 | 0.00174 | OK |
| 9 | 20 | 0.0248 | 0.0593 | OK |
| 10 | 14 | 0.007 | 0.00056 | OK |

## Digital twin replay

`runs/chip1_rinse_dissociation_quench_wash_encapsulation.twin_run.json` (10 events) stores the chip as the repo-relative path `autochip/examples/live/protocol_resin_multilayer/chips/chip1_rinse_dissociation_quench_wash_encapsulation.json`, so the twin can load and replay it.

## Checks

- **info**: step 2: module culture_1 has no inlet named for wash_buffer; it goes in through inlet_1 (swap that syringe and prime the line first)
- **info**: step 3: module culture_1 has no inlet named for dissociation_enzyme; it goes in through inlet_1 (swap that syringe and prime the line first)
- **info**: step 4 (Mix quench_medium into the stream): the outflow passes quench_mix, cell_wash on its way to cell_wash_waste_out
- **info**: step 9 (Mix core_solution into the stream): the outflow passes core_mix, droplets_1 on its way to outlet_1
- **info**: the operator's planner (operator/planner.py) accepts this protocol: 10 events in runs/chip1_rinse_dissociation_quench_wash_encapsulation.twin_run.json, chip stored as autochip/examples/live/protocol_resin_multilayer/chips/chip1_rinse_dissociation_quench_wash_encapsulation.json

# Run sheet: Encapsulation chip (`chip1_encapsulation`)

**Purpose:** Cells in core solution are pinched into shell-polymer droplets by fluorinated oil at a flow-focusing junction.  
**Total time:** 30 min  
**Status:** generated; placeholder values: check flows, volumes and times before a real run.

Port states: **FEED** = a syringe pump pushes into that port; **VENT** = open outlet into a tube; **CAP** = plugged. Ports not named in a step are capped.

## Setup

### Ports and syringes

| Port | Type | Connect | Load (µL) | Syringe | Used in steps |
|---|---|---|---|---|---|
| `inlet_1` | inlet | syringe: cells_in_core_solution (from step 1) | 72 | 1 mL | 1 |
| `outlet_1` | outlet | tubing to tube labelled 'chip1_encapsulation: droplets after step 10, for chip2_emulsion_break_curing_wash_filter inlet_1' | | | 1 |
| `shell_polymer_in` | inlet | syringe: shell_polymer (from step 1) | 72 | 1 mL | 1 |
| `fluorinated_oil_in` | inlet | syringe: fluorinated_oil (from step 1) | 360 | 1 mL | 1 |

### Equipment

- syringe pump with 3 independent lines
- syringe heater / warm jacket on the shell_polymer syringe and line (37 °C)
- microscope
- tubing for every used port, plugs for capped ports, waste and collection tubes

Before step 1: fill every syringe, prime each feed line up to its port (no air), connect the outlet tubing, plug every port, then open only the ports step 1 needs.

## Steps

| # | IR step | Start | Module | What | Port states | Volume | Time | Temp | Watch |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 10 | 0:00:00 | droplets_1 | Encapsulate (co-flow) | FEED inlet_1 2 µL/min (cells_in_core_solution); shell_polymer_in 2 µL/min (shell_polymer); fluorinated_oil_in 10 µL/min (fluorinated_oil) / VENT outlet_1 / rest capped | 420 µL | 30 min |  | steady droplet break-up at droplets_1 (microscope); droplets travel on towards outlet_1; no oil backing up into the core line; keep shell_polymer_in (shell_polymer) warm (37 °C syringe heater) up to the chip; the cold zone starts downstream; collect into the tube labelled 'chip1_encapsulation: droplets after step 10, for chip2_emulsion_break_curing_wash_filter inlet_1' |

## Flow check (operator/network.py)

| # | Through target (µL/min) | Max pressure (kPa) | Target shear (dyn/cm2) | Result |
|---|---|---|---|---|
| 1 | 14 | 0.0029 | 0.0415 | OK |

## Digital twin replay

`runs/chip1_encapsulation.twin_run.json` (1 events) stores the chip as the repo-relative path `autochip/examples/live/paper_assayathon_demo/chips/chip1_encapsulation.json`, so the twin can load and replay it.

## Checks

- **info**: the operator's planner (operator/planner.py) accepts this protocol: 1 events in runs/chip1_encapsulation.twin_run.json, chip stored as autochip/examples/live/paper_assayathon_demo/chips/chip1_encapsulation.json

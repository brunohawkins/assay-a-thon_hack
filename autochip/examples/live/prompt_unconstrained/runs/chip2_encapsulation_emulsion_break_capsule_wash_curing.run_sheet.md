# Run sheet: Encapsulation, emulsion break, capsule wash and curing chip (`chip2_encapsulation_emulsion_break_capsule_wash_curing`)

**Purpose:** Merges cells into core solution, makes single-cell shell/core droplets in fluorinated oil, then breaks the emulsion, washes, cures and washes the capsules in a trap.  
**Total time:** 1.73 h  
**Status:** generated; placeholder values: check flows, volumes and times before a real run.

Port states: **FEED** = a syringe pump pushes into that port; **VENT** = open outlet into a tube; **CAP** = plugged. Ports not named in a step are capped.

## Setup

### Ports and syringes

| Port | Type | Connect | Load (µL) | Syringe | Used in steps |
|---|---|---|---|---|---|
| `inlet_1` | inlet | syringe: cells_suspension (from step 1) | 36 | 1 mL | 1 |
| `outlet_1` | outlet | tubing to tube labelled 'chip2_encapsulation_emulsion_break_capsule_wash_curing: capsules after step 13, for chip3_lysis_rt_split_index_pcr_pool_release inlet_1' | | | 6 |
| `core_solution_in` | inlet | syringe: core_solution (from step 1) | 36 | 1 mL | 1 |
| `shell_polymer_in` | inlet | syringe: shell_polymer (from step 1) | 72 | 1 mL | 1 |
| `fluorinated_oil_in` | inlet | syringe: fluorinated_oil (from step 1) | 360 | 1 mL | 1 |
| `emulsion_breaker_in` | inlet | syringe: emulsion_breaker (from step 2) | 180 | 1 mL | 2 |
| `wash_buffer_in` | inlet | syringe: wash_buffer (from step 3), then collection_buffer (from step 6); swap the syringe and prime the line between reagents | 900 / 120 | 1 mL / 1 mL | 3, 5, 6 |
| `capsule_trap_waste_out` | outlet | tubing to waste | | | 1, 2, 3, 5 |

### Equipment

- syringe pump with 4 independent lines
- LED / lamp at 405 nm
- microscope
- tubing for every used port, plugs for capped ports, waste and collection tubes

Before step 1: fill every syringe, prime each feed line up to its port (no air), connect the outlet tubing, plug every port, then open only the ports step 1 needs.

## Steps

| # | IR step | Start | Module | What | Port states | Volume | Time | Temp | Watch |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 9 | 0:00:00 | encap_junction | Encapsulate (co-flow) | FEED inlet_1 1 µL/min (cells_suspension); core_solution_in 1 µL/min (core_solution); shell_polymer_in 2 µL/min (shell_polymer); fluorinated_oil_in 10 µL/min (fluorinated_oil) / VENT capsule_trap_waste_out / rest capped | 420 µL | 30 min |  | steady droplet break-up at encap_junction (microscope); droplets travel on towards capsule_trap_waste_out; no oil backing up into the core line; stop the pumps once the material has reached capsule_trap (its next step) |
| 2 | 10 | 0:30:00 | capsule_trap | Exchange: emulsion_breaker | FEED emulsion_breaker_in 5 µL/min (emulsion_breaker) / VENT capsule_trap_waste_out / rest capped | 150 µL | 30 min |  | emulsion_breaker reaches capsule_trap and leaves at capsule_trap_waste_out; no bubbles in the chamber |
| 3 | 11 | 1:00:00 | capsule_trap | Exchange: wash_buffer x3 | FEED wash_buffer_in 20 µL/min (wash_buffer) / VENT capsule_trap_waste_out / rest capped | 450 µL | 22.5 min |  | wash_buffer reaches capsule_trap and leaves at capsule_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 4 | 12 | 1:22:30 | capsule_trap | Cure (405 nm) (light 405 nm) | all capped |  | 1 min |  | 405 nm light over capsule_trap for 1 min; nothing flows |
| 5 | 13 | 1:23:30 | capsule_trap | Exchange: wash_buffer x2 | FEED wash_buffer_in 20 µL/min (wash_buffer) / VENT capsule_trap_waste_out / rest capped | 300 µL | 15 min |  | wash_buffer reaches capsule_trap and leaves at capsule_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 6 | 13 | 1:38:30 | capsule_trap | Collect the capsules (outlet_1) | FEED wash_buffer_in 20 µL/min (collection_buffer) / VENT outlet_1 / rest capped | 100 µL | 5 min |  | the capsules leaves capsule_trap through outlet_1 into the tube labelled 'chip2_encapsulation_emulsion_break_capsule_wash_curing: capsules after step 13, for chip3_lysis_rt_split_index_pcr_pool_release inlet_1'; never to waste |

## Flow check (operator/network.py)

| # | Through target (µL/min) | Max pressure (kPa) | Target shear (dyn/cm2) | Result |
|---|---|---|---|---|
| 1 | 14 | 0.0133 | 0.0415 | OK |
| 2 | 5 | 0.0011 | 0.00226 | OK |
| 3 | 20 | 0.0043 | 0.00905 | OK |
| 4 | | | | no flow: every port capped |
| 5 | 20 | 0.0043 | 0.00905 | OK |
| 6 | 20 | 0.0031 | 0.00905 | OK |

## Digital twin replay

`runs/chip2_encapsulation_emulsion_break_capsule_wash_curing.twin_run.json` (6 events) stores the chip as the repo-relative path `autochip/examples/live/prompt_unconstrained/chips/chip2_encapsulation_emulsion_break_capsule_wash_curing.json`, so the twin can load and replay it.

## Checks

- **info**: step 9 (Encapsulate (co-flow)): the outflow passes capsule_trap on its way to capsule_trap_waste_out
- **info**: the operator's planner (operator/planner.py) accepts this protocol: 6 events in runs/chip2_encapsulation_emulsion_break_capsule_wash_curing.twin_run.json, chip stored as autochip/examples/live/prompt_unconstrained/chips/chip2_encapsulation_emulsion_break_capsule_wash_curing.json

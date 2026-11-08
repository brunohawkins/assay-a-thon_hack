# Run sheet: Shell release, bead capture, ethanol wash and elution chip (`chip4_shell_release_bead_capture_wash_elution`)

**Purpose:** Digests the capsule shells to release cDNA, then binds the library to magnetic beads, washes them with ethanol on the magnet and elutes in water.  
**Total time:** 47 min  
**Status:** generated; placeholder values: check flows, volumes and times before a real run.

Port states: **FEED** = a syringe pump pushes into that port; **VENT** = open outlet into a tube; **CAP** = plugged. Ports not named in a step are capped.

## Setup

### Ports and syringes

| Port | Type | Connect | Load (µL) | Syringe | Used in steps |
|---|---|---|---|---|---|
| `inlet_1` | inlet | syringe: capsules_sample (from step 1), then nucleic_acid_sample (from step 4); swap the syringe and prime the line between reagents | 90 / 90 | 1 mL / 1 mL | 1, 4 |
| `outlet_1` | outlet | tubing to tube labelled 'chip4_shell_release_bead_capture_wash_elution: library after step 25' | | | 9 |
| `shell_digesting_enzyme_in` | inlet | syringe: shell_digesting_enzyme (from step 2) | 120 | 1 mL | 2 |
| `release_trap_waste_out` | outlet | tubing to waste | | | 1, 2 |
| `magnetic_beads_in` | inlet | syringe: magnetic_beads (from step 3) | 90 | 1 mL | 3 |
| `ethanol_wash_in` | inlet | syringe: ethanol_wash (from step 6) | 360 | 1 mL | 6 |
| `beads_1_waste_out` | outlet | tubing to waste | | | 3, 4, 6, 7 |
| `elution_buffer_in` | inlet | syringe: elution_buffer (from step 7), then collection_buffer (from step 9); swap the syringe and prime the line between reagents | 90 / 120 | 1 mL / 1 mL | 7, 9 |

### Equipment

- syringe pump with 1 independent line
- magnet under the chip
- tubing for every used port, plugs for capped ports, waste and collection tubes

Before step 1: fill every syringe, prime each feed line up to its port (no air), connect the outlet tubing, plug every port, then open only the ports step 1 needs.

## Steps

| # | IR step | Start | Module | What | Port states | Volume | Time | Temp | Watch |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 22 | 0:00:00 | release_trap | Load capsules from the hand-off from chip3_filter_lysis_rt_wash | FEED inlet_1 20 µL/min (capsules_sample) / VENT release_trap_waste_out / rest capped | 75 µL | 3.75 min |  | the capsules arrives in release_trap and stays there; liquid leaves at release_trap_waste_out |
| 2 | 22 | 0:03:45 | release_trap | Release with shell_digesting_enzyme | FEED shell_digesting_enzyme_in 20 µL/min (shell_digesting_enzyme) / VENT release_trap_waste_out / rest capped | 100 µL | 5 min |  | material leaves through release_trap_waste_out into a collection tube; stop when release_trap is clear |
| 3 | 23 | 0:08:45 | beads_1 | Exchange: magnetic_beads | FEED magnetic_beads_in 20 µL/min (magnetic_beads) / VENT beads_1_waste_out / rest capped | 75 µL | 3.75 min |  | magnetic_beads reaches beads_1 and leaves at beads_1_waste_out; no bubbles in the chamber |
| 4 | 23 | 0:12:30 | beads_1 | Load nucleic_acid from the hand-off from chip3_filter_lysis_rt_wash | FEED inlet_1 20 µL/min (nucleic_acid_sample) / VENT beads_1_waste_out / rest capped | 75 µL | 3.75 min |  | the nucleic_acid arrives in beads_1; stop the pump as soon as it is there, before it runs on towards beads_1_waste_out |
| 5 | 23 | 0:16:15 | beads_1 | Incubate | all capped |  | 5 min | 22 °C | temperature steady at 22 °C; nothing moving; capped ports not leaking |
| 6 | 24 | 0:21:15 | beads_1 | Exchange: ethanol_wash x2 (magnet on) | FEED ethanol_wash_in 20 µL/min (ethanol_wash) / VENT beads_1_waste_out / rest capped | 300 µL | 15 min |  | ethanol_wash reaches beads_1 and leaves at beads_1_waste_out; no bubbles in the chamber; magnet on, beads stay put |
| 7 | 25 | 0:36:15 | beads_1 | Exchange: elution_buffer | FEED elution_buffer_in 20 µL/min (elution_buffer) / VENT beads_1_waste_out / rest capped | 75 µL | 3.75 min |  | elution_buffer reaches beads_1 and leaves at beads_1_waste_out; no bubbles in the chamber |
| 8 | 25 | 0:40:00 | beads_1 | Incubate | all capped |  | 2 min | 22 °C | temperature steady at 22 °C; nothing moving; capped ports not leaking |
| 9 | 25 | 0:42:00 | beads_1 | Collect the library (outlet_1) (magnet on) | FEED elution_buffer_in 20 µL/min (collection_buffer) / VENT outlet_1 / rest capped | 100 µL | 5 min |  | the library leaves beads_1 through outlet_1 into the tube labelled 'chip4_shell_release_bead_capture_wash_elution: library after step 25'; magnet on so the beads stay behind; never to waste |

## Flow check (operator/network.py)

| # | Through target (µL/min) | Max pressure (kPa) | Target shear (dyn/cm2) | Result |
|---|---|---|---|---|
| 1 | 20 | 0.002 | 0.00905 | OK |
| 2 | 20 | 0.0019 | 0.00905 | OK |
| 3 | 20 | 0.0019 | 0.00905 | OK |
| 4 | 20 | 0.0025 | 0.00905 | OK |
| 5 | | | | no flow: every port capped |
| 6 | 20 | 0.0019 | 0.00905 | OK |
| 7 | 20 | 0.0019 | 0.00905 | OK |
| 8 | | | | no flow: every port capped |
| 9 | 20 | 0.002 | 0.00905 | OK |

## Digital twin replay

`runs/chip4_shell_release_bead_capture_wash_elution.twin_run.json` (9 events) stores the chip as the repo-relative path `autochip/examples/live/protocol_resin_multilayer/chips/chip4_shell_release_bead_capture_wash_elution.json`, so the twin can load and replay it.

## Checks

- **info**: the operator's planner (operator/planner.py) accepts this protocol: 9 events in runs/chip4_shell_release_bead_capture_wash_elution.twin_run.json, chip stored as autochip/examples/live/protocol_resin_multilayer/chips/chip4_shell_release_bead_capture_wash_elution.json

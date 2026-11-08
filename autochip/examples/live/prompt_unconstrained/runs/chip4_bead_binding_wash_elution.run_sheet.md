# Run sheet: Bead binding, ethanol wash and elution chip (`chip4_bead_binding_wash_elution`)

**Purpose:** Binds cDNA to magnetic beads, washes on the magnet and elutes the library.  
**Total time:** 38.2 min  
**Status:** generated; placeholder values: check flows, volumes and times before a real run.

Port states: **FEED** = a syringe pump pushes into that port; **VENT** = open outlet into a tube; **CAP** = plugged. Ports not named in a step are capped.

## Setup

### Ports and syringes

| Port | Type | Connect | Load (µL) | Syringe | Used in steps |
|---|---|---|---|---|---|
| `inlet_1` | inlet | syringe: nucleic_acid_sample (from step 2) | 90 | 1 mL | 2 |
| `outlet_1` | outlet | tubing to tube labelled 'chip4_bead_binding_wash_elution: library after step 23' | | | 7 |
| `magnetic_beads_in` | inlet | syringe: magnetic_beads (from step 1) | 90 | 1 mL | 1 |
| `ethanol_wash_in` | inlet | syringe: ethanol_wash (from step 4) | 360 | 1 mL | 4 |
| `bead_waste_out` | outlet | tubing to waste | | | 1, 2, 4, 5 |
| `elution_buffer_in` | inlet | syringe: elution_buffer (from step 5), then collection_buffer (from step 7); swap the syringe and prime the line between reagents | 90 / 120 | 1 mL / 1 mL | 5, 7 |

### Equipment

- syringe pump with 1 independent line
- magnet under the chip
- tubing for every used port, plugs for capped ports, waste and collection tubes

Before step 1: fill every syringe, prime each feed line up to its port (no air), connect the outlet tubing, plug every port, then open only the ports step 1 needs.

## Steps

| # | IR step | Start | Module | What | Port states | Volume | Time | Temp | Watch |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 21 | 0:00:00 | bead_chamber | Exchange: magnetic_beads | FEED magnetic_beads_in 20 µL/min (magnetic_beads) / VENT bead_waste_out / rest capped | 75 µL | 3.75 min | 22 °C | magnetic_beads reaches bead_chamber and leaves at bead_waste_out; no bubbles in the chamber |
| 2 | 21 | 0:03:45 | bead_chamber | Load nucleic_acid from the hand-off from chip3_lysis_rt_split_index_pcr_pool_release | FEED inlet_1 20 µL/min (nucleic_acid_sample) / VENT bead_waste_out / rest capped | 75 µL | 3.75 min |  | the nucleic_acid arrives in bead_chamber; stop the pump as soon as it is there, before it runs on towards bead_waste_out |
| 3 | 21 | 0:07:30 | bead_chamber | Incubate | all capped |  | 5 min | 22 °C | temperature steady at 22 °C; nothing moving; capped ports not leaking |
| 4 | 22 | 0:12:30 | bead_chamber | Exchange: ethanol_wash x2 (magnet on) | FEED ethanol_wash_in 20 µL/min (ethanol_wash) / VENT bead_waste_out / rest capped | 300 µL | 15 min |  | ethanol_wash reaches bead_chamber and leaves at bead_waste_out; no bubbles in the chamber; magnet on, beads stay put |
| 5 | 23 | 0:27:30 | bead_chamber | Exchange: elution_buffer (magnet on) | FEED elution_buffer_in 20 µL/min (elution_buffer) / VENT bead_waste_out / rest capped | 75 µL | 3.75 min | 22 °C | elution_buffer reaches bead_chamber and leaves at bead_waste_out; no bubbles in the chamber; magnet on, beads stay put |
| 6 | 23 | 0:31:15 | bead_chamber | Incubate (magnet on) | all capped |  | 2 min | 22 °C | temperature steady at 22 °C; nothing moving; capped ports not leaking |
| 7 | 23 | 0:33:15 | bead_chamber | Collect the library (outlet_1) (magnet on) | FEED elution_buffer_in 20 µL/min (collection_buffer) / VENT outlet_1 / rest capped | 100 µL | 5 min |  | the library leaves bead_chamber through outlet_1 into the tube labelled 'chip4_bead_binding_wash_elution: library after step 23'; magnet on so the beads stay behind; never to waste |

## Flow check (operator/network.py)

| # | Through target (µL/min) | Max pressure (kPa) | Target shear (dyn/cm2) | Result |
|---|---|---|---|---|
| 1 | 20 | 0.0044 | 0.00905 | OK |
| 2 | 20 | 0.0062 | 0.00905 | OK |
| 3 | | | | no flow: every port capped |
| 4 | 20 | 0.0044 | 0.00905 | OK |
| 5 | 20 | 0.0044 | 0.00905 | OK |
| 6 | | | | no flow: every port capped |
| 7 | 20 | 0.0072 | 0.00905 | OK |

## Digital twin replay

`runs/chip4_bead_binding_wash_elution.twin_run.json` (7 events) stores the chip as the repo-relative path `autochip/examples/live/prompt_unconstrained/chips/chip4_bead_binding_wash_elution.json`, so the twin can load and replay it.

## Checks

- **info**: the operator's planner (operator/planner.py) accepts this protocol: 7 events in runs/chip4_bead_binding_wash_elution.twin_run.json, chip stored as autochip/examples/live/prompt_unconstrained/chips/chip4_bead_binding_wash_elution.json

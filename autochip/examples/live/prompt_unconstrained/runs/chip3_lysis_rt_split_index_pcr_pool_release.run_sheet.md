# Run sheet: Lysis, RT, split, index PCR, pool and release chip (`chip3_lysis_rt_split_index_pcr_pool_release`)

**Purpose:** Lyses cells in capsules, reverse transcribes, splits into four portions for index PCR, pools them and releases the cDNA.  
**Total time:** 3.16 h  
**Status:** generated; placeholder values: check flows, volumes and times before a real run.

Port states: **FEED** = a syringe pump pushes into that port; **VENT** = open outlet into a tube; **CAP** = plugged. Ports not named in a step are capped.

## Setup

### Ports and syringes

| Port | Type | Connect | Load (µL) | Syringe | Used in steps |
|---|---|---|---|---|---|
| `inlet_1` | inlet | syringe: capsules_sample (from step 1), then rt_mix (from step 5), then carrier_buffer (from step 8), then library_suspension (from step 16), then collection_buffer (from step 17); swap the syringe and prime the line between reagents | 90 / 90 / 400 / 40 / 40 | 1 mL / 1 mL / 1 mL / 1 mL / 1 mL | 1, 5, 8, 14, 16, 17 |
| `outlet_1` | outlet | tubing to tube labelled 'chip3_lysis_rt_split_index_pcr_pool_release: library after step 24, for chip4_bead_binding_wash_elution inlet_1'; tube labelled 'chip3_lysis_rt_split_index_pcr_pool_release: nucleic_acid after step 20, for chip4_bead_binding_wash_elution inlet_1'; waste tube | | | 15, 16, 17 |
| `reagent_line_in` | inlet | plug (not used) | | | |
| `wash_buffer_in` | inlet | syringe: lysis_buffer (from step 2), then wash_buffer (from step 4); swap the syringe and prime the line between reagents | 90 / 900 | 1 mL / 1 mL | 2, 4, 7 |
| `reaction_trap_waste_out` | outlet | tubing to waste | | | 1, 2, 4, 5, 7 |
| `index_mix_1_in` | inlet | syringe: index_mix_1 (from step 9) | 45 | 1 mL | 9 |
| `pcr_1_vent_out` | outlet | tubing to waste | | | 8, 9 |
| `index_mix_2_in` | inlet | syringe: index_mix_2 (from step 10) | 45 | 1 mL | 10 |
| `pcr_2_vent_out` | outlet | tubing to waste | | | 8, 10 |
| `index_mix_3_in` | inlet | syringe: index_mix_3 (from step 11) | 45 | 1 mL | 11 |
| `pcr_3_vent_out` | outlet | tubing to waste | | | 8, 11 |
| `index_mix_4_in` | inlet | syringe: index_mix_4 (from step 12) | 45 | 1 mL | 12 |
| `pcr_4_vent_out` | outlet | tubing to waste | | | 8, 12 |
| `release_enzyme_in` | inlet | syringe: release_enzyme (from step 15) | 120 | 1 mL | 15 |
| `release_trap_waste_out` | outlet | tubing to waste | | | 14 |

### Equipment

- syringe pump with 1 independent line
- heater / stage-top incubator (42, 72, 95 °C)
- thermocycler for thermal cycling (pcr_zone)
- tubing for every used port, plugs for capped ports, waste and collection tubes

Before step 1: fill every syringe, prime each feed line up to its port (no air), connect the outlet tubing, plug every port, then open only the ports step 1 needs.

## Steps

| # | IR step | Start | Module | What | Port states | Volume | Time | Temp | Watch |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 14 | 0:00:00 | reaction_trap | Load capsules from the hand-off from chip2_encapsulation_emulsion_break_capsule_wash_curing | FEED inlet_1 20 µL/min (capsules_sample) / VENT reaction_trap_waste_out / rest capped | 75 µL | 3.75 min |  | the capsules arrives in reaction_trap and stays there; liquid leaves at reaction_trap_waste_out |
| 2 | 14 | 0:03:45 | reaction_trap | Exchange: lysis_buffer | FEED wash_buffer_in 20 µL/min (lysis_buffer) / VENT reaction_trap_waste_out / rest capped | 75 µL | 3.75 min |  | lysis_buffer reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 3 | 14 | 0:07:30 | reaction_trap | Incubate | all capped |  | 10 min | 22 °C | temperature steady at 22 °C; nothing moving; capped ports not leaking |
| 4 | 15 | 0:17:30 | reaction_trap | Exchange: wash_buffer x3 | FEED wash_buffer_in 20 µL/min (wash_buffer) / VENT reaction_trap_waste_out / rest capped | 450 µL | 22.5 min |  | wash_buffer reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 5 | 16 | 0:40:00 | reaction_trap | Exchange: rt_mix | FEED inlet_1 20 µL/min (rt_mix) / VENT reaction_trap_waste_out / rest capped | 75 µL | 3.75 min | 42 °C | rt_mix reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 6 | 16 | 0:43:45 | reaction_trap | Incubate | all capped |  | 1 h | 42 °C | temperature steady at 42 °C; nothing moving; capped ports not leaking |
| 7 | 17 | 1:43:45 | reaction_trap | Exchange: wash_buffer x2 | FEED wash_buffer_in 20 µL/min (wash_buffer) / VENT reaction_trap_waste_out / rest capped | 300 µL | 15 min |  | wash_buffer reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh |
| 8 | 18 | 1:58:45 | split_2 | Split | FEED inlet_1 20 µL/min (carrier_buffer) / VENT pcr_1_vent_out, pcr_2_vent_out, pcr_3_vent_out, pcr_4_vent_out / rest capped | 183 µL | 9.15 min |  | material moves from reaction_trap through split_2 towards pcr_1_vent_out, pcr_2_vent_out, pcr_3_vent_out, pcr_4_vent_out; streams merge evenly; no clogging; stop the pumps once the material is spread over pcr_1, pcr_2, pcr_3, pcr_4 |
| 9 | 19 | 2:07:54 | pcr_1 | Exchange: index_mix_1 | FEED index_mix_1_in 20 µL/min (index_mix_1) / VENT pcr_1_vent_out / rest capped | 37.5 µL | 1.88 min | 72 °C | index_mix_1 reaches pcr_1 and leaves at pcr_1_vent_out; no bubbles in the chamber; capsules stay on the mesh |
| 10 | 19 | 2:09:46 | pcr_2 | Exchange: index_mix_2 | FEED index_mix_2_in 20 µL/min (index_mix_2) / VENT pcr_2_vent_out / rest capped | 37.5 µL | 1.88 min | 72 °C | index_mix_2 reaches pcr_2 and leaves at pcr_2_vent_out; no bubbles in the chamber; capsules stay on the mesh |
| 11 | 19 | 2:11:39 | pcr_3 | Exchange: index_mix_3 | FEED index_mix_3_in 20 µL/min (index_mix_3) / VENT pcr_3_vent_out / rest capped | 37.5 µL | 1.88 min | 72 °C | index_mix_3 reaches pcr_3 and leaves at pcr_3_vent_out; no bubbles in the chamber; capsules stay on the mesh |
| 12 | 19 | 2:13:32 | pcr_4 | Exchange: index_mix_4 | FEED index_mix_4_in 20 µL/min (index_mix_4) / VENT pcr_4_vent_out / rest capped | 37.5 µL | 1.88 min | 72 °C | index_mix_4 reaches pcr_4 and leaves at pcr_4_vent_out; no bubbles in the chamber; capsules stay on the mesh |
| 13 | 19 | 2:15:24 | pcr_4 | Thermal cycling: N cycles | all capped |  | 38.2 min | 95 °C | block runs the program on thermal zone pcr_zone; ports stay capped; no bubbles or evaporation in pcr_4 |
| 14 | 20 | 2:53:39 | release_trap | Move capsules: pcr_1 → release_trap | FEED inlet_1 20 µL/min (carrier_buffer) / VENT release_trap_waste_out / rest capped | 150 µL | 7.5 min |  | the capsules arrives in release_trap and stays there; liquid leaves at release_trap_waste_out |
| 15 | 20 | 3:01:09 | release_trap | Release with release_enzyme | FEED release_enzyme_in 20 µL/min (release_enzyme) / VENT outlet_1 / rest capped | 100 µL | 5 min |  | material leaves through outlet_1 into a collection tube; stop when release_trap is clear; collect into the tube labelled 'chip3_lysis_rt_split_index_pcr_pool_release: nucleic_acid after step 20, for chip4_bead_binding_wash_elution inlet_1' |
| 16 | 24 | 3:06:09 | pool_2 | Pool | FEED inlet_1 20 µL/min (library_suspension) / VENT outlet_1 / rest capped | 33 µL | 1.65 min |  | material moves from the inlet through pool_2 towards outlet_1; streams merge evenly; no clogging |
| 17 | 24 | 3:07:48 | pool_2 | Collect the library (outlet_1) | FEED inlet_1 20 µL/min (collection_buffer) / VENT outlet_1 / rest capped | 33 µL | 1.65 min |  | the library leaves pool_2 through outlet_1 into the tube labelled 'chip3_lysis_rt_split_index_pcr_pool_release: library after step 24, for chip4_bead_binding_wash_elution inlet_1'; never to waste |

## Flow check (operator/network.py)

| # | Through target (µL/min) | Max pressure (kPa) | Target shear (dyn/cm2) | Result |
|---|---|---|---|---|
| 1 | 20 | 0.0375 | 0.00905 | OK |
| 2 | 20 | 0.0734 | 0.00905 | OK |
| 3 | | | | no flow: every port capped |
| 4 | 20 | 0.0734 | 0.00905 | OK |
| 5 | 20 | 0.0375 | 0.00905 | OK |
| 6 | | | | no flow: every port capped |
| 7 | 20 | 0.0734 | 0.00905 | OK |
| 8 | 20 | 0.0018 | 0.00213 | OK |
| 9 | 20 | 0.0042 | 0.0128 | OK |
| 10 | 20 | 0.0042 | 0.0128 | OK |
| 11 | 20 | 0.0042 | 0.0128 | OK |
| 12 | 20 | 0.0042 | 0.0128 | OK |
| 13 | | | | no flow: every port capped |
| 14 | 20 | 0.0404 | 0.00905 | OK |
| 15 | 20 | 0.0418 | 0.00905 | OK |
| 16 | 20 | 0.0089 | 0.00213 | OK |
| 17 | 20 | 0.0089 | 0.00213 | OK |

## Digital twin replay

`runs/chip3_lysis_rt_split_index_pcr_pool_release.twin_run.json` (17 events) stores the chip as the repo-relative path `autochip/examples/live/prompt_unconstrained/chips/chip3_lysis_rt_split_index_pcr_pool_release.json`, so the twin can load and replay it.

## Checks

- **info**: step 16: module reaction_trap has no inlet named for rt_mix; it goes in through inlet_1 (swap that syringe and prime the line first)
- **info**: step 24 (Pool): the outflow passes release_trap on its way to outlet_1
- **info**: step 24 (Collect the library (outlet_1)): the outflow passes release_trap on its way to outlet_1
- **info**: the operator's planner (operator/planner.py) accepts this protocol: 17 events in runs/chip3_lysis_rt_split_index_pcr_pool_release.twin_run.json, chip stored as autochip/examples/live/prompt_unconstrained/chips/chip3_lysis_rt_split_index_pcr_pool_release.json

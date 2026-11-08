# Lab run sheet: Single-cell processing inside semi-permeable capsules

**Goal:** Take adherent cells through dissociation, capsule generation, photocuring, lysis, reverse transcription and index PCR to a cleaned-up, quality-checked sequencing library.  
**Chips:** chip1_encapsulation (Encapsulation chip), chip2_emulsion_break_curing_wash_filter (Emulsion break, curing, wash and capsule filter chip), chip3_lysis_wash_rt (Lysis, wash and reverse transcription chip)  
**Steps:** 26: 9 on chip, 16 at the bench, 1 waiting for a missing part  
**Status:** generated; values the source did not give are placeholders.

Each chip has its own sheet (`<chip>.run_sheet.md`) with the syringe set-up and the full flow check. FEED = the pump pushes into the port, VENT = open to a tube; every port not named is capped.

## Before you start

- **chip1_encapsulation** (Encapsulation chip): syringes inlet_1 ← cells_in_core_solution (72 µL), shell_polymer_in ← shell_polymer (72 µL), fluorinated_oil_in ← fluorinated_oil (360 µL). Equipment: syringe pump with 3 independent lines; syringe heater / warm jacket on the shell_polymer syringe and line (37 °C); microscope; tubing for every used port, plugs for capped ports, waste and collection tubes.
- **chip2_emulsion_break_curing_wash_filter** (Emulsion break, curing, wash and capsule filter chip): syringes inlet_1 ← droplets_sample (90 µL), emulsion_breaking_agent_in ← emulsion_breaking_agent (180 µL), inlet_1 ← photoinitiator (90 µL), inlet_1 ← wash_buffer (540 µL), inlet_1 ← carrier_buffer (138 µL). Equipment: syringe pump with 1 independent line; LED / lamp at 405 nm; tubing for every used port, plugs for capped ports, waste and collection tubes.
- **chip3_lysis_wash_rt** (Lysis, wash and reverse transcription chip): syringes inlet_1 ← capsules_sample (90 µL), inlet_1 ← lysis_buffer (90 µL), inlet_1 ← wash_buffer (900 µL), inlet_1 ← rt_mix (90 µL), reagent_line_in ← collection_buffer (120 µL). Equipment: syringe pump with 1 independent line; heater / stage-top incubator (42 °C); tubing for every used port, plugs for capped ports, waste and collection tubes.

## Protocol

**Step 1** · at the bench: Culture adherent cells in a vessel until confluent. Equipment: pipette. (Off chip because: Culture, rinse and enzyme detachment of adherent cells in a vessel, done off chip.)

**Step 2** · at the bench: Remove the medium and rinse the cells once with buffer. Equipment: pipette. (Off chip because: Culture, rinse and enzyme detachment of adherent cells in a vessel, done off chip.)

**Step 3** · at the bench: Add dissociation enzyme and incubate to detach the cells. Equipment: pipette. (Off chip because: Culture, rinse and enzyme detachment of adherent cells in a vessel, done off chip.)

**Step 4** · at the bench: Add quench medium and mix gently to stop the enzyme. Equipment: centrifuge, pipette. (Off chip because: Quench, centrifuge and wash are done at the bench with the centrifuge.)

**Step 5** · at the bench: Centrifuge the cells and remove the supernatant. Equipment: centrifuge, pipette. (Off chip because: Quench, centrifuge and wash are done at the bench with the centrifuge.)

**Step 6** · at the bench: Resuspend the pellet in wash buffer and wash again (two washes in total). Equipment: centrifuge, pipette. (Off chip because: Quench, centrifuge and wash are done at the bench with the centrifuge.)

**Step 7** · **(!) missing part** (pillar filter with about 30 um gap (0.75 x the encapsulation nozzle) for cells upstream of the junction): Pass the suspension through a cell strainer.
  - Workaround for now: Strain the cells off chip with a 30-40 um cell strainer before loading chip 1.

**Step 8** · at the bench: Count the cells and check viability. Equipment: microscope. (Off chip because: Counting and viability check are done at the bench.)

**Step 9** · at the bench: Resuspend the cells in core solution at the loading density. Equipment: pipette. (Off chip because: Resuspending in core solution is a pipette step before loading the syringe for chip 1.)

**Step 10** · chip **chip1_encapsulation**, module `droplets_1`: Co-flow the cell/core solution with warm shell polymer solution and focus with fluorinated oil at a flow-focusing junction to make droplets.
  - chip1_encapsulation #1 Encapsulate (co-flow): FEED inlet_1 2 µL/min (cells_in_core_solution); shell_polymer_in 2 µL/min (shell_polymer); fluorinated_oil_in 10 µL/min (fluorinated_oil) | VENT outlet_1 | rest capped; 30 min. Watch: steady droplet break-up at droplets_1 (microscope); droplets travel on towards outlet_1; no oil backing up into the core line; keep shell_polymer_in (shell_polymer) warm (37 °C syringe heater) up to the chip; the cold zone starts downstream; collect into the tube labelled 'chip1_encapsulation: droplets after step 10, for chip2_emulsion_break_curing_wash_filter inlet_1'.

> **Hand-off out of chip1_encapsulation (droplets): collect it from outlet_1 of chip1_encapsulation into a tube, then do bench step(s) 11.**

**Step 11** · at the bench: Collect the emulsion and keep it on ice so the shell gels. Equipment: cold_plate, pipette. (Off chip because: The emulsion is gelled on the cold plate off chip. A 30 min delay line would not fit a 30 x 20 mm chip.)

> **Hand-off into chip2_emulsion_break_curing_wash_filter (droplets): load the tube into the syringe on inlet_1 of chip2_emulsion_break_curing_wash_filter (the sample line for chip2_emulsion_break_curing_wash_filter's first step).**

**Step 12** · chip **chip2_emulsion_break_curing_wash_filter**, module `capsule_trap`: Add emulsion-breaking agent and remove the oil phase.
  - chip2_emulsion_break_curing_wash_filter #1 Load droplets from the hand-off from chip1_encapsulation: FEED inlet_1 5 µL/min (droplets_sample) | VENT trap_waste_out | rest capped; 15 min. Watch: the droplets arrives in capsule_trap and stays there; liquid leaves at trap_waste_out.
  - chip2_emulsion_break_curing_wash_filter #2 Exchange: emulsion_breaking_agent: FEED emulsion_breaking_agent_in 5 µL/min (emulsion_breaking_agent) | VENT trap_waste_out | rest capped; 30 min. Watch: emulsion_breaking_agent reaches capsule_trap and leaves at trap_waste_out; no bubbles in the chamber.

**Step 13** · chip **chip2_emulsion_break_curing_wash_filter**, module `capsule_trap`: Photocure the capsule shells with 405 nm light in the presence of photoinitiator.
  - chip2_emulsion_break_curing_wash_filter #3 Exchange: photoinitiator: FEED inlet_1 20 µL/min (photoinitiator) | VENT trap_waste_out | rest capped; 3.75 min. Watch: photoinitiator reaches capsule_trap and leaves at trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip2_emulsion_break_curing_wash_filter #4 Cure (405 nm): all capped; 30 s; light 405 nm. Watch: 405 nm light over capsule_trap for 30 s; nothing flows.

**Step 14** · chip **chip2_emulsion_break_curing_wash_filter**, module `capsule_trap`: Wash the capsules in buffer to remove oil and free reagents.
  - chip2_emulsion_break_curing_wash_filter #5 Exchange: wash_buffer x3: FEED inlet_1 20 µL/min (wash_buffer) | VENT trap_waste_out | rest capped; 22.5 min. Watch: wash_buffer reaches capsule_trap and leaves at trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.

**Step 15** · chip **chip2_emulsion_break_curing_wash_filter**, module `aggregate_filter`: Filter the capsules through a strainer to remove aggregates.
  - chip2_emulsion_break_curing_wash_filter #6 Pass through: FEED inlet_1 20 µL/min (carrier_buffer) | VENT outlet_1 | rest capped; 5.75 min. Watch: material moves from capsule_trap through aggregate_filter towards outlet_1; streams merge evenly; no clogging; collect into the tube labelled 'chip2_emulsion_break_curing_wash_filter: capsules after step 15, for chip3_lysis_wash_rt inlet_1'.

> **Hand-off chip2_emulsion_break_curing_wash_filter → chip3_lysis_wash_rt (capsules): move the outlet_1 tubing of chip2_emulsion_break_curing_wash_filter to inlet_1 of chip3_lysis_wash_rt; keep every other port of both chips capped.**

**Step 16** · chip **chip3_lysis_wash_rt**, module `lysis_rt_trap`: Exchange the buffer for lysis buffer and incubate the capsules.
  - chip3_lysis_wash_rt #1 Load capsules from the hand-off from chip2_emulsion_break_curing_wash_filter: FEED inlet_1 20 µL/min (capsules_sample) | VENT trap_waste_out | rest capped; 3.75 min. Watch: the capsules arrives in lysis_rt_trap and stays there; liquid leaves at trap_waste_out.
  - chip3_lysis_wash_rt #2 Exchange: lysis_buffer: FEED inlet_1 20 µL/min (lysis_buffer) | VENT trap_waste_out | rest capped; 3.75 min. Watch: lysis_buffer reaches lysis_rt_trap and leaves at trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip3_lysis_wash_rt #3 Incubate: all capped; 10 min; 22 °C. Watch: temperature steady at 22 °C; nothing moving; capped ports not leaking.

**Step 17** · chip **chip3_lysis_wash_rt**, module `lysis_rt_trap`: Wash the capsules in wash buffer.
  - chip3_lysis_wash_rt #4 Exchange: wash_buffer x3: FEED inlet_1 20 µL/min (wash_buffer) | VENT trap_waste_out | rest capped; 22.5 min. Watch: wash_buffer reaches lysis_rt_trap and leaves at trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.

**Step 18** · chip **chip3_lysis_wash_rt**, module `lysis_rt_trap`: Exchange the buffer for reverse transcription mix and incubate the capsules.
  - chip3_lysis_wash_rt #5 Exchange: rt_mix: FEED inlet_1 20 µL/min (rt_mix) | VENT trap_waste_out | rest capped; 3.75 min; 42 °C. Watch: rt_mix reaches lysis_rt_trap and leaves at trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip3_lysis_wash_rt #6 Incubate: all capped; 1 h; 42 °C. Watch: temperature steady at 42 °C; nothing moving; capped ports not leaking.

**Step 19** · chip **chip3_lysis_wash_rt**, module `lysis_rt_trap`: Wash the capsules twice.
  - chip3_lysis_wash_rt #7 Exchange: wash_buffer x2: FEED inlet_1 20 µL/min (wash_buffer) | VENT trap_waste_out | rest capped; 15 min. Watch: wash_buffer reaches lysis_rt_trap and leaves at trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip3_lysis_wash_rt #8 Collect the capsules (outlet_1): FEED reagent_line_in 20 µL/min (collection_buffer) | VENT outlet_1 | rest capped; 5 min. Watch: the capsules leaves lysis_rt_trap through outlet_1 into the tube labelled 'chip3_lysis_wash_rt: capsules after step 19'; never to waste.

**Step 20** · at the bench: Split the capsules into four equal portions. Equipment: thermocycler, pipette. (Off chip because: Index PCR reaches 95 °C, above the 50 °C limit of the printed resin. The capsules are split into four portions by pipette and cycled in the thermocycler, each with its own index primer mix.)

**Step 21** · at the bench: Run index PCR on each portion with a different index primer mix. Equipment: thermocycler, pipette. (Off chip because: Index PCR reaches 95 °C, above the 50 °C limit of the printed resin. The capsules are split into four portions by pipette and cycled in the thermocycler, each with its own index primer mix.)

**Step 22** · at the bench: Release the cDNA by digesting the capsule shells with shell-digesting enzyme. Equipment: heater, pipette. (Off chip because: Shell digestion is done at the bench by pipette after PCR.)

**Step 23** · at the bench: Bind the library to magnetic beads. Equipment: pipette. (Off chip because: Bead binding, ethanol washes and elution are done as a bench kit. The lab has no magnet.)

**Step 24** · at the bench: Wash the beads with ethanol wash solution on a magnet. Equipment: pipette. (Off chip because: Bead binding, ethanol washes and elution are done as a bench kit. The lab has no magnet.)

**Step 25** · at the bench: Elute the library from the beads in water. Equipment: pipette. (Off chip because: Bead binding, ethanol washes and elution are done as a bench kit. The lab has no magnet.)

**Step 26** · at the bench: Measure library concentration and fragment size. Equipment: fluorometer, fragment_analyzer. (Off chip because: Library concentration and fragment size are measured on bench instruments.)

## Open problems

- step 7: missing part: pillar filter with about 30 um gap (0.75 x the encapsulation nozzle) for cells upstream of the junction (workaround: Strain the cells off chip with a 30-40 um cell strainer before loading chip 1.)

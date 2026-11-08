# Lab run sheet: Semi-permeable capsule (SPC) workflow

**Goal:** Prepare adherent cells, encapsulate them in semi-permeable capsules, process them inside the capsules (lysis, reverse transcription, indexing PCR), release cDNA and purify and measure a library.  
**Chips:** chip1_rinse_dissociation_quench_wash_encapsulation (Cell rinse, dissociation, quench, wash and encapsulation chip), chip2_gelation_emulsion_break_cure_wash (Gelation, emulsion breaking, curing and capsule wash chip), chip3_filter_lysis_rt_wash (Capsule filter, lysis, reverse transcription and wash chip), chip4_shell_release_bead_capture_wash_elution (Shell release, bead capture, ethanol wash and elution chip)  
**Steps:** 26: 20 on chip, 5 at the bench, 1 waiting for a missing part  
**Status:** generated; values the source did not give are placeholders.

Each chip has its own sheet (`<chip>.run_sheet.md`) with the syringe set-up and the full flow check. FEED = the pump pushes into the port, VENT = open to a tube; every port not named is capped.

## Before you start

- **chip1_rinse_dissociation_quench_wash_encapsulation** (Cell rinse, dissociation, quench, wash and encapsulation chip): syringes inlet_1 ← cells_sample (180 µL), inlet_1 ← wash_buffer (360 µL), inlet_1 ← dissociation_enzyme (180 µL), inlet_1 ← carrier_buffer (1080 µL), quench_medium_in ← quench_medium (450 µL), wash_buffer_in ← wash_buffer (762 µL), core_solution_in ← core_solution (630 µL), inlet_1 ← core_solution (72 µL), shell_polymer_in ← shell_polymer (72 µL), fluorinated_oil_in ← fluorinated_oil (360 µL). Equipment: syringe pump with 3 independent lines; heater / stage-top incubator (37 °C); syringe heater / warm jacket on the shell_polymer syringe and line (37 °C); microscope; tubing for every used port, plugs for capped ports, waste and collection tubes.
- **chip2_gelation_emulsion_break_cure_wash** (Gelation, emulsion breaking, curing and capsule wash chip): syringes inlet_1 ← droplets_sample (270 µL), inlet_1 ← carrier_buffer (480 µL), emulsion_breaking_agent_in ← emulsion_breaking_agent (180 µL), photoinitiator_in ← photoinitiator (90 µL), wash_buffer_in ← wash_buffer (540 µL), wash_buffer_in ← collection_buffer (120 µL). Equipment: syringe pump with 1 independent line; cold plate (4 °C); LED / lamp at 405 nm; tubing for every used port, plugs for capped ports, waste and collection tubes.
- **chip3_filter_lysis_rt_wash** (Capsule filter, lysis, reverse transcription and wash chip): syringes inlet_1 ← capsules_suspension (138 µL), reagent_line_in ← lysis_buffer (90 µL), reagent_line_in ← wash_buffer (900 µL), reagent_line_in ← rt_mix (90 µL), reagent_line_in ← collection_buffer (120 µL). Equipment: syringe pump with 1 independent line; heater / stage-top incubator (42 °C); tubing for every used port, plugs for capped ports, waste and collection tubes.
- **chip4_shell_release_bead_capture_wash_elution** (Shell release, bead capture, ethanol wash and elution chip): syringes inlet_1 ← capsules_sample (90 µL), shell_digesting_enzyme_in ← shell_digesting_enzyme (120 µL), magnetic_beads_in ← magnetic_beads (90 µL), inlet_1 ← nucleic_acid_sample (90 µL), ethanol_wash_in ← ethanol_wash (360 µL), elution_buffer_in ← elution_buffer (90 µL), elution_buffer_in ← collection_buffer (120 µL). Equipment: syringe pump with 1 independent line; magnet under the chip; tubing for every used port, plugs for capped ports, waste and collection tubes.

## Protocol

**Step 1** · at the bench: Culture adherent cells in a vessel at 37 °C with 5% CO2 until confluent, changing the medium daily. Equipment: pipette. (Off chip because: Three-day culture of adherent cells in a CO2 incubator is done in a vessel at the bench.)

**Step 2** · chip **chip1_rinse_dissociation_quench_wash_encapsulation**, module `culture_1`: Remove the medium and rinse the cells once with buffer.
  - chip1_rinse_dissociation_quench_wash_encapsulation #1 Load cells from the bench: FEED inlet_1 10 µL/min (cells_sample) | VENT culture_1_waste_out | rest capped; 15 min. Watch: the cells arrives in culture_1 and stays there; liquid leaves at culture_1_waste_out.
  - chip1_rinse_dissociation_quench_wash_encapsulation #2 Exchange: wash_buffer: FEED inlet_1 10 µL/min (wash_buffer) | VENT culture_1_waste_out | rest capped; 30 min. Watch: wash_buffer reaches culture_1 and leaves at culture_1_waste_out; no bubbles in the chamber; cells stay in place.

**Step 3** · chip **chip1_rinse_dissociation_quench_wash_encapsulation**, module `culture_1`: Add dissociation enzyme and incubate to detach the cells.
  - chip1_rinse_dissociation_quench_wash_encapsulation #3 Exchange: dissociation_enzyme: FEED inlet_1 10 µL/min (dissociation_enzyme) | VENT culture_1_waste_out | rest capped; 15 min; 37 °C. Watch: dissociation_enzyme reaches culture_1 and leaves at culture_1_waste_out; no bubbles in the chamber.
  - chip1_rinse_dissociation_quench_wash_encapsulation #4 Incubate: all capped; 5 min; 37 °C. Watch: temperature steady at 37 °C; nothing moving; capped ports not leaking.

**Step 4** · chip **chip1_rinse_dissociation_quench_wash_encapsulation**, module `quench_join`: Add quench medium and mix gently to stop the enzyme.
  - chip1_rinse_dissociation_quench_wash_encapsulation #5 Mix quench_medium into the stream: FEED inlet_1 10 µL/min (carrier_buffer); quench_medium_in 10 µL/min (quench_medium) | VENT cell_wash_waste_out | rest capped; 37.5 min. Watch: material moves from culture_1 through quench_join towards cell_wash_waste_out; streams merge evenly; no clogging; stop the pumps once the material has reached cell_wash (its next step).

**Step 5** · chip **chip1_rinse_dissociation_quench_wash_encapsulation**, module `cell_wash`: Centrifuge the cells and remove the supernatant.
  - chip1_rinse_dissociation_quench_wash_encapsulation #6 Exchange: wash_buffer: FEED wash_buffer_in 10 µL/min (wash_buffer) | VENT cell_wash_waste_out | rest capped; 12.7 min. Watch: wash_buffer reaches cell_wash and leaves at cell_wash_waste_out; no bubbles in the chamber; cells stay in place.
  - chip1_rinse_dissociation_quench_wash_encapsulation #7 Incubate: all capped; 5 min; 22 °C. Watch: temperature steady at 22 °C; nothing moving; capped ports not leaking.

**Step 6** · chip **chip1_rinse_dissociation_quench_wash_encapsulation**, module `cell_wash`: Resuspend the pellet in wash buffer to wash the cells; repeat the wash once.
  - chip1_rinse_dissociation_quench_wash_encapsulation #8 Exchange: wash_buffer x2: FEED wash_buffer_in 10 µL/min (wash_buffer) | VENT cell_wash_waste_out | rest capped; 50.8 min. Watch: wash_buffer reaches cell_wash and leaves at cell_wash_waste_out; no bubbles in the chamber; cells stay in place.

**Step 7** · **(!) missing part** (Pillar filter with ~30 um gap for cells (0.75 x the 40 um nozzle)): Pass the suspension through a 40 um cell strainer.
  - Workaround for now: Strain the cells off chip through a 30-40 um strainer before loading, or place a bought mesh in a trap.

**Step 8** · at the bench: Count the cells and check viability. Equipment: microscope. (Off chip because: Cell counting and viability check are done on the bench.)

**Step 9** · chip **chip1_rinse_dissociation_quench_wash_encapsulation**, module `core_join`: Resuspend the cells in core solution at the loading density.
  - chip1_rinse_dissociation_quench_wash_encapsulation #9 Mix core_solution into the stream: FEED inlet_1 10 µL/min (carrier_buffer); core_solution_in 10 µL/min (core_solution) | VENT outlet_1 | rest capped; 52.5 min. Watch: material moves from cell_wash through core_join towards outlet_1; streams merge evenly; no clogging; stop the pumps once the material has reached droplets_1 (its next step).

**Step 10** · chip **chip1_rinse_dissociation_quench_wash_encapsulation**, module `droplets_1`: Co-flow the cell/core solution with warm shell polymer solution and focus with fluorinated oil with surfactant at a flow-focusing junction to form droplets of about 70 um.
  - chip1_rinse_dissociation_quench_wash_encapsulation #10 Encapsulate (co-flow): FEED inlet_1 2 µL/min (core_solution); shell_polymer_in 2 µL/min (shell_polymer); fluorinated_oil_in 10 µL/min (fluorinated_oil) | VENT outlet_1 | rest capped; 30 min. Watch: steady droplet break-up at droplets_1 (microscope); droplets travel on towards outlet_1; no oil backing up into the core line; keep shell_polymer_in (shell_polymer) warm (37 °C syringe heater) up to the chip; the cold zone starts downstream; collect into the tube labelled 'chip1_rinse_dissociation_quench_wash_encapsulation: droplets after step 10, for chip2_gelation_emulsion_break_cure_wash inlet_1'.

> **Hand-off chip1_rinse_dissociation_quench_wash_encapsulation → chip2_gelation_emulsion_break_cure_wash (droplets): move the outlet_1 tubing of chip1_rinse_dissociation_quench_wash_encapsulation to inlet_1 of chip2_gelation_emulsion_break_cure_wash; keep every other port of both chips capped.**

**Step 11** · chip **chip2_gelation_emulsion_break_cure_wash**, module `gelation_delay`: Collect the emulsion and keep it on ice so the shell gels.
  - chip2_gelation_emulsion_break_cure_wash #1 Load droplets from the hand-off from chip1_rinse_dissociation_quench_wash_encapsulation: FEED inlet_1 5 µL/min (droplets_sample) | VENT capsule_trap_waste_out | rest capped; 45 min. Watch: the droplets arrives in gelation_delay; stop the pump as soon as it is there, before it runs on towards capsule_trap_waste_out.
  - chip2_gelation_emulsion_break_cure_wash #2 Hold: all capped; 30 min; 4 °C. Watch: temperature steady at 4 °C; nothing moving; capped ports not leaking.

**Step 12** · chip **chip2_gelation_emulsion_break_cure_wash**, module `capsule_trap`: Break the emulsion with an emulsion-breaking agent and remove the oil phase.
  - chip2_gelation_emulsion_break_cure_wash #3 Move droplets: gelation_delay → capsule_trap: FEED inlet_1 5 µL/min (carrier_buffer) | VENT capsule_trap_waste_out | rest capped; 1.33 h. Watch: the droplets arrives in capsule_trap and stays there; liquid leaves at capsule_trap_waste_out.
  - chip2_gelation_emulsion_break_cure_wash #4 Exchange: emulsion_breaking_agent: FEED emulsion_breaking_agent_in 5 µL/min (emulsion_breaking_agent) | VENT capsule_trap_waste_out | rest capped; 30 min. Watch: emulsion_breaking_agent reaches capsule_trap and leaves at capsule_trap_waste_out; no bubbles in the chamber.

**Step 13** · chip **chip2_gelation_emulsion_break_cure_wash**, module `capsule_trap`: Photocure the capsule shells with 405 nm light in the presence of photoinitiator.
  - chip2_gelation_emulsion_break_cure_wash #5 Exchange: photoinitiator: FEED photoinitiator_in 20 µL/min (photoinitiator) | VENT capsule_trap_waste_out | rest capped; 3.75 min. Watch: photoinitiator reaches capsule_trap and leaves at capsule_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip2_gelation_emulsion_break_cure_wash #6 Cure (405 nm): all capped; 30 s; 4 °C; light 405 nm. Watch: 405 nm light over capsule_trap for 30 s; nothing flows.

**Step 14** · chip **chip2_gelation_emulsion_break_cure_wash**, module `capsule_trap`: Wash the capsules in buffer to remove oil and free reagents.
  - chip2_gelation_emulsion_break_cure_wash #7 Exchange: wash_buffer x3: FEED wash_buffer_in 20 µL/min (wash_buffer) | VENT capsule_trap_waste_out | rest capped; 22.5 min. Watch: wash_buffer reaches capsule_trap and leaves at capsule_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip2_gelation_emulsion_break_cure_wash #8 Collect the capsules (outlet_1): FEED wash_buffer_in 20 µL/min (collection_buffer) | VENT outlet_1 | rest capped; 5 min. Watch: the capsules leaves capsule_trap through outlet_1 into the tube labelled 'chip2_gelation_emulsion_break_cure_wash: capsules after step 14, for chip3_filter_lysis_rt_wash inlet_1'; never to waste.

> **Hand-off chip2_gelation_emulsion_break_cure_wash → chip3_filter_lysis_rt_wash (capsules): move the outlet_1 tubing of chip2_gelation_emulsion_break_cure_wash to inlet_1 of chip3_filter_lysis_rt_wash; keep every other port of both chips capped.**

**Step 15** · chip **chip3_filter_lysis_rt_wash**, module `aggregate_filter`: Filter the capsules through a 100 um strainer to remove aggregates.
  - chip3_filter_lysis_rt_wash #1 Pass through: FEED inlet_1 20 µL/min (capsules_suspension) | VENT reaction_trap_waste_out | rest capped; 5.75 min. Watch: material moves from the inlet through aggregate_filter towards reaction_trap_waste_out; streams merge evenly; no clogging; stop the pumps once the material has reached reaction_trap (its next step).

**Step 16** · chip **chip3_filter_lysis_rt_wash**, module `reaction_trap`: Exchange the buffer for lysis buffer and incubate.
  - chip3_filter_lysis_rt_wash #2 Exchange: lysis_buffer: FEED reagent_line_in 20 µL/min (lysis_buffer) | VENT reaction_trap_waste_out | rest capped; 3.75 min. Watch: lysis_buffer reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip3_filter_lysis_rt_wash #3 Incubate: all capped; 10 min; 22 °C. Watch: temperature steady at 22 °C; nothing moving; capped ports not leaking.

**Step 17** · chip **chip3_filter_lysis_rt_wash**, module `reaction_trap`: Wash the capsules in wash buffer.
  - chip3_filter_lysis_rt_wash #4 Exchange: wash_buffer x3: FEED reagent_line_in 20 µL/min (wash_buffer) | VENT reaction_trap_waste_out | rest capped; 22.5 min. Watch: wash_buffer reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.

**Step 18** · chip **chip3_filter_lysis_rt_wash**, module `reaction_trap`: Exchange into reverse transcription mix and incubate.
  - chip3_filter_lysis_rt_wash #5 Exchange: rt_mix: FEED reagent_line_in 20 µL/min (rt_mix) | VENT reaction_trap_waste_out | rest capped; 3.75 min; 42 °C. Watch: rt_mix reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip3_filter_lysis_rt_wash #6 Incubate: all capped; 1 h; 42 °C. Watch: temperature steady at 42 °C; nothing moving; capped ports not leaking.

**Step 19** · chip **chip3_filter_lysis_rt_wash**, module `reaction_trap`: Wash the capsules.
  - chip3_filter_lysis_rt_wash #7 Exchange: wash_buffer x2: FEED reagent_line_in 20 µL/min (wash_buffer) | VENT reaction_trap_waste_out | rest capped; 15 min. Watch: wash_buffer reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip3_filter_lysis_rt_wash #8 Collect the capsules (outlet_1): FEED reagent_line_in 20 µL/min (collection_buffer) | VENT outlet_1 | rest capped; 5 min. Watch: the capsules leaves reaction_trap through outlet_1 into the tube labelled 'chip3_filter_lysis_rt_wash: capsules after step 19, for chip4_shell_release_bead_capture_wash_elution inlet_1'; never to waste.

> **Hand-off out of chip3_filter_lysis_rt_wash (capsules): collect it from outlet_1 of chip3_filter_lysis_rt_wash into a tube, then do bench step(s) 20, 21.**

**Step 20** · at the bench: Split the capsules into four equal portions. Equipment: pipette, thermocycler. (Off chip because: Index PCR reaches 95 °C, above the 50 °C limit of the printed resin. The capsules are split by pipette into four tubes, each gets its own index primer mix, and they cycle on the thermocycler. They are then pooled and loaded onto chip 4.)

**Step 21** · at the bench: Run index PCR on each portion with a different index primer mix. Equipment: pipette, thermocycler. (Off chip because: Index PCR reaches 95 °C, above the 50 °C limit of the printed resin. The capsules are split by pipette into four tubes, each gets its own index primer mix, and they cycle on the thermocycler. They are then pooled and loaded onto chip 4.)

> **Hand-off into chip4_shell_release_bead_capture_wash_elution (capsules): load the tube into the syringe on inlet_1 of chip4_shell_release_bead_capture_wash_elution (the sample line for chip4_shell_release_bead_capture_wash_elution's first step).**

**Step 22** · chip **chip4_shell_release_bead_capture_wash_elution**, module `release_trap`: Release the cDNA by digesting the capsule shells with a shell-digesting enzyme.
  - chip4_shell_release_bead_capture_wash_elution #1 Load capsules from the hand-off from chip3_filter_lysis_rt_wash: FEED inlet_1 20 µL/min (capsules_sample) | VENT release_trap_waste_out | rest capped; 3.75 min. Watch: the capsules arrives in release_trap and stays there; liquid leaves at release_trap_waste_out.
  - chip4_shell_release_bead_capture_wash_elution #2 Release with shell_digesting_enzyme: FEED shell_digesting_enzyme_in 20 µL/min (shell_digesting_enzyme) | VENT release_trap_waste_out | rest capped; 5 min. Watch: material leaves through release_trap_waste_out into a collection tube; stop when release_trap is clear.

**Step 23** · chip **chip4_shell_release_bead_capture_wash_elution**, module `beads_1`: Bind the library to magnetic beads.
  - chip4_shell_release_bead_capture_wash_elution #3 Exchange: magnetic_beads: FEED magnetic_beads_in 20 µL/min (magnetic_beads) | VENT beads_1_waste_out | rest capped; 3.75 min. Watch: magnetic_beads reaches beads_1 and leaves at beads_1_waste_out; no bubbles in the chamber.
  - chip4_shell_release_bead_capture_wash_elution #4 Load nucleic_acid from the hand-off from chip3_filter_lysis_rt_wash: FEED inlet_1 20 µL/min (nucleic_acid_sample) | VENT beads_1_waste_out | rest capped; 3.75 min. Watch: the nucleic_acid arrives in beads_1; stop the pump as soon as it is there, before it runs on towards beads_1_waste_out.
  - chip4_shell_release_bead_capture_wash_elution #5 Incubate: all capped; 5 min; 22 °C. Watch: temperature steady at 22 °C; nothing moving; capped ports not leaking.

**Step 24** · chip **chip4_shell_release_bead_capture_wash_elution**, module `beads_1`: Wash the beads with ethanol wash solution on a magnet.
  - chip4_shell_release_bead_capture_wash_elution #6 Exchange: ethanol_wash x2: FEED ethanol_wash_in 20 µL/min (ethanol_wash) | VENT beads_1_waste_out | rest capped; 15 min; magnet on. Watch: ethanol_wash reaches beads_1 and leaves at beads_1_waste_out; no bubbles in the chamber; magnet on, beads stay put.

**Step 25** · chip **chip4_shell_release_bead_capture_wash_elution**, module `beads_1`: Elute the library from the beads in water.
  - chip4_shell_release_bead_capture_wash_elution #7 Exchange: elution_buffer: FEED elution_buffer_in 20 µL/min (elution_buffer) | VENT beads_1_waste_out | rest capped; 3.75 min. Watch: elution_buffer reaches beads_1 and leaves at beads_1_waste_out; no bubbles in the chamber.
  - chip4_shell_release_bead_capture_wash_elution #8 Incubate: all capped; 2 min; 22 °C. Watch: temperature steady at 22 °C; nothing moving; capped ports not leaking.
  - chip4_shell_release_bead_capture_wash_elution #9 Collect the library (outlet_1): FEED elution_buffer_in 20 µL/min (collection_buffer) | VENT outlet_1 | rest capped; 5 min; magnet on. Watch: the library leaves beads_1 through outlet_1 into the tube labelled 'chip4_shell_release_bead_capture_wash_elution: library after step 25'; magnet on so the beads stay behind; never to waste.

**Step 26** · at the bench: Measure library concentration and fragment size. Equipment: pipette. (Off chip because: Library concentration and size measurement is done on the bench; the lab has no fluorometer in its list, so it is a manual step on the eluted sample.)

## Open problems

- step 7: missing part: Pillar filter with ~30 um gap for cells (0.75 x the 40 um nozzle) (workaround: Strain the cells off chip through a 30-40 um strainer before loading, or place a bought mesh in a trap.)

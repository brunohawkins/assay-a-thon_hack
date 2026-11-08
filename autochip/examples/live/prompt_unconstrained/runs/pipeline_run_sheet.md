# Lab run sheet: iPSC tri-culture to capsule-based single-cell RNA library

**Goal:** Grow iPSC-derived neurons, microglia and astrocytes side by side (first apart, then sharing medium), then encapsulate single cells in semi-permeable hydrogel capsules, lyse, reverse transcribe, split into four index-PCR sublibraries, release the cDNA and clean up the library with magnetic beads for sequencing.  
**Chips:** chip1_triculture_dissociation_quench_cell_wash_filter (Tri-culture, dissociation, quench, cell wash and filter chip), chip2_encapsulation_emulsion_break_capsule_wash_curing (Encapsulation, emulsion break, capsule wash and curing chip), chip3_lysis_rt_split_index_pcr_pool_release (Lysis, RT, split, index PCR, pool and release chip), chip4_bead_binding_wash_elution (Bead binding, ethanol wash and elution chip)  
**Steps:** 25: 23 on chip, 2 at the bench, 0 waiting for a missing part  
**Status:** generated; values the source did not give are placeholders.

Each chip has its own sheet (`<chip>.run_sheet.md`) with the syringe set-up and the full flow check. FEED = the pump pushes into the port, VENT = open to a tube; every port not named is capped.

## Before you start

- **chip1_triculture_dissociation_quench_cell_wash_filter** (Tri-culture, dissociation, quench, cell wash and filter chip): syringes culture_medium_neurons_in ← cells_for_culture_neurons (180 µL), culture_medium_microglia_in ← cells_for_culture_microglia (180 µL), culture_medium_astrocytes_in ← cells_for_culture_astrocytes (180 µL), culture_medium_neurons_in ← culture_medium_neurons (1980 µL), culture_medium_microglia_in ← culture_medium_microglia (1620 µL), culture_medium_astrocytes_in ← culture_medium_astrocytes (1620 µL), culture_medium_neurons_in ← dissociation_enzyme (180 µL), culture_medium_microglia_in ← dissociation_enzyme (180 µL), culture_medium_astrocytes_in ← dissociation_enzyme (180 µL), inlet_1 ← carrier_buffer (440 µL), quench_solution_in ← quench_solution (341 µL), wash_buffer_in ← wash_buffer (221 µL). Equipment: syringe pump with 3 independent lines; heater / stage-top incubator (37 °C); microscope; tubing for every used port, plugs for capped ports, waste and collection tubes.
- **chip2_encapsulation_emulsion_break_capsule_wash_curing** (Encapsulation, emulsion break, capsule wash and curing chip): syringes inlet_1 ← cells_suspension (36 µL), core_solution_in ← core_solution (36 µL), shell_polymer_in ← shell_polymer (72 µL), fluorinated_oil_in ← fluorinated_oil (360 µL), emulsion_breaker_in ← emulsion_breaker (180 µL), wash_buffer_in ← wash_buffer (900 µL), wash_buffer_in ← collection_buffer (120 µL). Equipment: syringe pump with 4 independent lines; LED / lamp at 405 nm; microscope; tubing for every used port, plugs for capped ports, waste and collection tubes.
- **chip3_lysis_rt_split_index_pcr_pool_release** (Lysis, RT, split, index PCR, pool and release chip): syringes inlet_1 ← capsules_sample (90 µL), wash_buffer_in ← lysis_buffer (90 µL), wash_buffer_in ← wash_buffer (900 µL), inlet_1 ← rt_mix (90 µL), inlet_1 ← carrier_buffer (400 µL), index_mix_1_in ← index_mix_1 (45 µL), index_mix_2_in ← index_mix_2 (45 µL), index_mix_3_in ← index_mix_3 (45 µL), index_mix_4_in ← index_mix_4 (45 µL), release_enzyme_in ← release_enzyme (120 µL), inlet_1 ← library_suspension (40 µL), inlet_1 ← collection_buffer (40 µL). Equipment: syringe pump with 1 independent line; heater / stage-top incubator (42, 72, 95 °C); thermocycler for thermal cycling (pcr_zone); tubing for every used port, plugs for capped ports, waste and collection tubes.
- **chip4_bead_binding_wash_elution** (Bead binding, ethanol wash and elution chip): syringes magnetic_beads_in ← magnetic_beads (90 µL), inlet_1 ← nucleic_acid_sample (90 µL), ethanol_wash_in ← ethanol_wash (360 µL), elution_buffer_in ← elution_buffer (90 µL), elution_buffer_in ← collection_buffer (120 µL). Equipment: syringe pump with 1 independent line; magnet under the chip; tubing for every used port, plugs for capped ports, waste and collection tubes.

## Protocol

**Step 1** · chip **chip1_triculture_dissociation_quench_cell_wash_filter**, module `culture_neurons`: Culture neurons, microglia and astrocytes in separate vessels, side by side, each in its own medium.
  - chip1_triculture_dissociation_quench_cell_wash_filter #1 Load cells from the bench: FEED culture_medium_neurons_in 10 µL/min (cells_for_culture_neurons) | VENT culture_neurons_vent_out | rest capped; 15 min. Watch: the cells arrives in culture_neurons and stays there; liquid leaves at culture_neurons_vent_out.
  - chip1_triculture_dissociation_quench_cell_wash_filter #2 Load cells from the bench: FEED culture_medium_microglia_in 10 µL/min (cells_for_culture_microglia) | VENT microglia_vent_out | rest capped; 15 min. Watch: the cells arrives in culture_microglia and stays there; liquid leaves at microglia_vent_out.
  - chip1_triculture_dissociation_quench_cell_wash_filter #3 Load cells from the bench: FEED culture_medium_astrocytes_in 10 µL/min (cells_for_culture_astrocytes) | VENT astrocytes_vent_out | rest capped; 15 min. Watch: the cells arrives in culture_astrocytes and stays there; liquid leaves at astrocytes_vent_out.
  - chip1_triculture_dissociation_quench_cell_wash_filter #4 Culture apart 7 d (3 chambers, own medium each, change every 1 d): FEED culture_medium_neurons_in 10 µL/min (culture_medium_neurons); culture_medium_microglia_in 10 µL/min (culture_medium_microglia); culture_medium_astrocytes_in 10 µL/min (culture_medium_astrocytes) | VENT culture_neurons_vent_out, microglia_vent_out, astrocytes_vent_out | rest capped; 7 d; 37 °C. Watch: every 1 d, all 3 pumps together: each chamber's medium leaves through its own vent and nothing crosses the junction; check each culture on the microscope.

**Step 2** · chip **chip1_triculture_dissociation_quench_cell_wash_filter**, module `culture_neurons`: Continue culture of the three cell types sharing medium (co-culture or shared medium flow).
  - chip1_triculture_dissociation_quench_cell_wash_filter #5 Culture apart 1 d (3 chambers, own medium each, change every 12 h): FEED culture_medium_neurons_in 10 µL/min (culture_medium_neurons); culture_medium_microglia_in 10 µL/min (culture_medium_microglia); culture_medium_astrocytes_in 10 µL/min (culture_medium_astrocytes) | VENT culture_neurons_vent_out, microglia_vent_out, astrocytes_vent_out | rest capped; 1 d; 37 °C. Watch: every 12 h, all 3 pumps together: each chamber's medium leaves through its own vent and nothing crosses the junction; check each culture on the microscope.
  - chip1_triculture_dissociation_quench_cell_wash_filter #6 Culture sharing medium 1 d (feed culture_neurons, vent the others): FEED culture_medium_neurons_in 10 µL/min (culture_medium_neurons) | VENT microglia_vent_out, astrocytes_vent_out | rest capped; 1 d; 37 °C. Watch: medium from culture_neurons reaches the other chambers through the junction and leaves at microglia_vent_out, astrocytes_vent_out.

**Step 3** · chip **chip1_triculture_dissociation_quench_cell_wash_filter**, module `culture_neurons`: Detach the cells with a dissociation enzyme.
  - chip1_triculture_dissociation_quench_cell_wash_filter #7 Exchange: dissociation_enzyme: FEED culture_medium_neurons_in 10 µL/min (dissociation_enzyme) | VENT culture_neurons_vent_out | rest capped; 15 min; 37 °C. Watch: dissociation_enzyme reaches culture_neurons and leaves at culture_neurons_vent_out; no bubbles in the chamber.
  - chip1_triculture_dissociation_quench_cell_wash_filter #8 Exchange: dissociation_enzyme: FEED culture_medium_microglia_in 10 µL/min (dissociation_enzyme) | VENT microglia_vent_out | rest capped; 15 min; 37 °C. Watch: dissociation_enzyme reaches culture_microglia and leaves at microglia_vent_out; no bubbles in the chamber.
  - chip1_triculture_dissociation_quench_cell_wash_filter #9 Exchange: dissociation_enzyme: FEED culture_medium_astrocytes_in 10 µL/min (dissociation_enzyme) | VENT astrocytes_vent_out | rest capped; 15 min; 37 °C. Watch: dissociation_enzyme reaches culture_astrocytes and leaves at astrocytes_vent_out; no bubbles in the chamber.
  - chip1_triculture_dissociation_quench_cell_wash_filter #10 Incubate: all capped; 10 min; 37 °C. Watch: temperature steady at 37 °C; nothing moving; capped ports not leaking.

**Step 4** · chip **chip1_triculture_dissociation_quench_cell_wash_filter**, module `quench_join`: Quench the dissociation enzyme.
  - chip1_triculture_dissociation_quench_cell_wash_filter #11 Mix quench_solution into the stream: FEED inlet_1 10 µL/min (carrier_buffer); quench_solution_in 10 µL/min (quench_solution) | VENT cell_wash_waste_out | rest capped; 28.4 min. Watch: material moves from culture_neurons through quench_join towards cell_wash_waste_out; streams merge evenly; no clogging; stop the pumps once the material has reached cell_wash (its next step).

**Step 5** · chip **chip1_triculture_dissociation_quench_cell_wash_filter**, module `cell_wash`: Centrifuge the cell suspension to pellet the cells.
  - chip1_triculture_dissociation_quench_cell_wash_filter #12 Exchange: wash_buffer: FEED wash_buffer_in 10 µL/min (wash_buffer) | VENT cell_wash_waste_out | rest capped; 6.14 min. Watch: wash_buffer reaches cell_wash and leaves at cell_wash_waste_out; no bubbles in the chamber; cells stay in place.
  - chip1_triculture_dissociation_quench_cell_wash_filter #13 Incubate: all capped; 5 min; 22 °C. Watch: temperature steady at 22 °C; nothing moving; capped ports not leaking.

**Step 6** · chip **chip1_triculture_dissociation_quench_cell_wash_filter**, module `cell_wash`: Resuspend the cells in wash buffer.
  - chip1_triculture_dissociation_quench_cell_wash_filter #14 Exchange: wash_buffer: FEED wash_buffer_in 10 µL/min (wash_buffer) | VENT cell_wash_waste_out | rest capped; 12.3 min. Watch: wash_buffer reaches cell_wash and leaves at cell_wash_waste_out; no bubbles in the chamber; cells stay in place.

**Step 7** · chip **chip1_triculture_dissociation_quench_cell_wash_filter**, module `cell_filter`: Filter the suspension through a cell strainer to remove clumps.
  - chip1_triculture_dissociation_quench_cell_wash_filter #15 Pass through: FEED inlet_1 10 µL/min (carrier_buffer) | VENT outlet_1 | rest capped; 8.31 min. Watch: material moves from cell_wash through cell_filter towards outlet_1; streams merge evenly; no clogging; collect into the tube labelled 'chip1_triculture_dissociation_quench_cell_wash_filter: cells after step 7, for chip2_encapsulation_emulsion_break_capsule_wash_curing inlet_1'.

> **Hand-off out of chip1_triculture_dissociation_quench_cell_wash_filter (filtered single cells (cell count checked on the bench)): collect it from outlet_1 of chip1_triculture_dissociation_quench_cell_wash_filter into a tube, then do bench step(s) 8.**

**Step 8** · at the bench: Count the cells and check viability. Equipment: pipette, microscope. (Off chip because: Cell counting and viability check are bench steps; the cell counter is not in the lab equipment list.)

> **Hand-off into chip2_encapsulation_emulsion_break_capsule_wash_curing (filtered single cells (cell count checked on the bench)): load the tube into the syringe on inlet_1 of chip2_encapsulation_emulsion_break_capsule_wash_curing (the sample line for chip2_encapsulation_emulsion_break_capsule_wash_curing's first step).**

**Step 9** · chip **chip2_encapsulation_emulsion_break_capsule_wash_curing**, module `encap_junction`: Wrap single cells in droplets of shell polymer around an aqueous core, formed in fluorinated oil at a chip junction.
  - chip2_encapsulation_emulsion_break_capsule_wash_curing #1 Encapsulate (co-flow): FEED inlet_1 1 µL/min (cells_suspension); core_solution_in 1 µL/min (core_solution); shell_polymer_in 2 µL/min (shell_polymer); fluorinated_oil_in 10 µL/min (fluorinated_oil) | VENT capsule_trap_waste_out | rest capped; 30 min. Watch: steady droplet break-up at encap_junction (microscope); droplets travel on towards capsule_trap_waste_out; no oil backing up into the core line; stop the pumps once the material has reached capsule_trap (its next step).

**Step 10** · chip **chip2_encapsulation_emulsion_break_capsule_wash_curing**, module `capsule_trap`: Break the emulsion with an emulsion-breaking agent and remove the oil phase.
  - chip2_encapsulation_emulsion_break_capsule_wash_curing #2 Exchange: emulsion_breaker: FEED emulsion_breaker_in 5 µL/min (emulsion_breaker) | VENT capsule_trap_waste_out | rest capped; 30 min. Watch: emulsion_breaker reaches capsule_trap and leaves at capsule_trap_waste_out; no bubbles in the chamber.

**Step 11** · chip **chip2_encapsulation_emulsion_break_capsule_wash_curing**, module `capsule_trap`: Wash the capsules to remove leftover oil.
  - chip2_encapsulation_emulsion_break_capsule_wash_curing #3 Exchange: wash_buffer x3: FEED wash_buffer_in 20 µL/min (wash_buffer) | VENT capsule_trap_waste_out | rest capped; 22.5 min. Watch: wash_buffer reaches capsule_trap and leaves at capsule_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.

**Step 12** · chip **chip2_encapsulation_emulsion_break_capsule_wash_curing**, module `capsule_trap`: Cure the capsule shells with light.
  - chip2_encapsulation_emulsion_break_capsule_wash_curing #4 Cure (405 nm): all capped; 1 min; light 405 nm. Watch: 405 nm light over capsule_trap for 1 min; nothing flows.

**Step 13** · chip **chip2_encapsulation_emulsion_break_capsule_wash_curing**, module `capsule_trap`: Wash the cured capsules.
  - chip2_encapsulation_emulsion_break_capsule_wash_curing #5 Exchange: wash_buffer x2: FEED wash_buffer_in 20 µL/min (wash_buffer) | VENT capsule_trap_waste_out | rest capped; 15 min. Watch: wash_buffer reaches capsule_trap and leaves at capsule_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip2_encapsulation_emulsion_break_capsule_wash_curing #6 Collect the capsules (outlet_1): FEED wash_buffer_in 20 µL/min (collection_buffer) | VENT outlet_1 | rest capped; 5 min. Watch: the capsules leaves capsule_trap through outlet_1 into the tube labelled 'chip2_encapsulation_emulsion_break_capsule_wash_curing: capsules after step 13, for chip3_lysis_rt_split_index_pcr_pool_release inlet_1'; never to waste.

> **Hand-off chip2_encapsulation_emulsion_break_capsule_wash_curing → chip3_lysis_rt_split_index_pcr_pool_release (cured capsules): move the outlet_1 tubing of chip2_encapsulation_emulsion_break_capsule_wash_curing to inlet_1 of chip3_lysis_rt_split_index_pcr_pool_release; keep every other port of both chips capped.**

**Step 14** · chip **chip3_lysis_rt_split_index_pcr_pool_release**, module `reaction_trap`: Lyse the cells inside the capsules (placeholder time and temperature).
  - chip3_lysis_rt_split_index_pcr_pool_release #1 Load capsules from the hand-off from chip2_encapsulation_emulsion_break_capsule_wash_curing: FEED inlet_1 20 µL/min (capsules_sample) | VENT reaction_trap_waste_out | rest capped; 3.75 min. Watch: the capsules arrives in reaction_trap and stays there; liquid leaves at reaction_trap_waste_out.
  - chip3_lysis_rt_split_index_pcr_pool_release #2 Exchange: lysis_buffer: FEED wash_buffer_in 20 µL/min (lysis_buffer) | VENT reaction_trap_waste_out | rest capped; 3.75 min. Watch: lysis_buffer reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip3_lysis_rt_split_index_pcr_pool_release #3 Incubate: all capped; 10 min; 22 °C. Watch: temperature steady at 22 °C; nothing moving; capped ports not leaking.

**Step 15** · chip **chip3_lysis_rt_split_index_pcr_pool_release**, module `reaction_trap`: Wash the capsules after lysis.
  - chip3_lysis_rt_split_index_pcr_pool_release #4 Exchange: wash_buffer x3: FEED wash_buffer_in 20 µL/min (wash_buffer) | VENT reaction_trap_waste_out | rest capped; 22.5 min. Watch: wash_buffer reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.

**Step 16** · chip **chip3_lysis_rt_split_index_pcr_pool_release**, module `reaction_trap`: Reverse transcribe the RNA inside the capsules (placeholder time and temperature).
  - chip3_lysis_rt_split_index_pcr_pool_release #5 Exchange: rt_mix: FEED inlet_1 20 µL/min (rt_mix) | VENT reaction_trap_waste_out | rest capped; 3.75 min; 42 °C. Watch: rt_mix reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip3_lysis_rt_split_index_pcr_pool_release #6 Incubate: all capped; 1 h; 42 °C. Watch: temperature steady at 42 °C; nothing moving; capped ports not leaking.

**Step 17** · chip **chip3_lysis_rt_split_index_pcr_pool_release**, module `reaction_trap`: Wash the capsules after reverse transcription.
  - chip3_lysis_rt_split_index_pcr_pool_release #7 Exchange: wash_buffer x2: FEED wash_buffer_in 20 µL/min (wash_buffer) | VENT reaction_trap_waste_out | rest capped; 15 min. Watch: wash_buffer reaches reaction_trap and leaves at reaction_trap_waste_out; no bubbles in the chamber; capsules stay on the mesh.

**Step 18** · chip **chip3_lysis_rt_split_index_pcr_pool_release**, module `split_2`: Split the capsules into four sublibrary portions.
  - chip3_lysis_rt_split_index_pcr_pool_release #8 Split: FEED inlet_1 20 µL/min (carrier_buffer) | VENT pcr_1_vent_out, pcr_2_vent_out, pcr_3_vent_out, pcr_4_vent_out | rest capped; 9.15 min. Watch: material moves from reaction_trap through split_2 towards pcr_1_vent_out, pcr_2_vent_out, pcr_3_vent_out, pcr_4_vent_out; streams merge evenly; no clogging; stop the pumps once the material is spread over pcr_1, pcr_2, pcr_3, pcr_4.

**Step 19** · chip **chip3_lysis_rt_split_index_pcr_pool_release**, module `pcr_1`: Run index PCR on each sublibrary with its own index (placeholder cycles, times and temperatures).
  - chip3_lysis_rt_split_index_pcr_pool_release #9 Exchange: index_mix_1: FEED index_mix_1_in 20 µL/min (index_mix_1) | VENT pcr_1_vent_out | rest capped; 1.88 min; 72 °C. Watch: index_mix_1 reaches pcr_1 and leaves at pcr_1_vent_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip3_lysis_rt_split_index_pcr_pool_release #10 Exchange: index_mix_2: FEED index_mix_2_in 20 µL/min (index_mix_2) | VENT pcr_2_vent_out | rest capped; 1.88 min; 72 °C. Watch: index_mix_2 reaches pcr_2 and leaves at pcr_2_vent_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip3_lysis_rt_split_index_pcr_pool_release #11 Exchange: index_mix_3: FEED index_mix_3_in 20 µL/min (index_mix_3) | VENT pcr_3_vent_out | rest capped; 1.88 min; 72 °C. Watch: index_mix_3 reaches pcr_3 and leaves at pcr_3_vent_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip3_lysis_rt_split_index_pcr_pool_release #12 Exchange: index_mix_4: FEED index_mix_4_in 20 µL/min (index_mix_4) | VENT pcr_4_vent_out | rest capped; 1.88 min; 72 °C. Watch: index_mix_4 reaches pcr_4 and leaves at pcr_4_vent_out; no bubbles in the chamber; capsules stay on the mesh.
  - chip3_lysis_rt_split_index_pcr_pool_release #13 Thermal cycling: N cycles: all capped; 38.2 min; 95 °C. Watch: block runs the program on thermal zone pcr_zone; ports stay capped; no bubbles or evaporation in pcr_4.

**Step 20** · chip **chip3_lysis_rt_split_index_pcr_pool_release**, module `release_trap`: Release the cDNA from the capsules by digesting the shell with a shell-digesting enzyme (placeholder time and temperature).
  - chip3_lysis_rt_split_index_pcr_pool_release #14 Move capsules: pcr_1 → release_trap: FEED inlet_1 20 µL/min (carrier_buffer) | VENT release_trap_waste_out | rest capped; 7.5 min. Watch: the capsules arrives in release_trap and stays there; liquid leaves at release_trap_waste_out.
  - chip3_lysis_rt_split_index_pcr_pool_release #15 Release with release_enzyme: FEED release_enzyme_in 20 µL/min (release_enzyme) | VENT outlet_1 | rest capped; 5 min. Watch: material leaves through outlet_1 into a collection tube; stop when release_trap is clear; collect into the tube labelled 'chip3_lysis_rt_split_index_pcr_pool_release: nucleic_acid after step 20, for chip4_bead_binding_wash_elution inlet_1'.

> **Hand-off chip3_lysis_rt_split_index_pcr_pool_release → chip4_bead_binding_wash_elution (released cDNA): move the outlet_1 tubing of chip3_lysis_rt_split_index_pcr_pool_release to inlet_1 of chip4_bead_binding_wash_elution; keep every other port of both chips capped.**

**Step 21** · chip **chip4_bead_binding_wash_elution**, module `bead_chamber`: Bind the released cDNA to magnetic beads.
  - chip4_bead_binding_wash_elution #1 Exchange: magnetic_beads: FEED magnetic_beads_in 20 µL/min (magnetic_beads) | VENT bead_waste_out | rest capped; 3.75 min; 22 °C. Watch: magnetic_beads reaches bead_chamber and leaves at bead_waste_out; no bubbles in the chamber.
  - chip4_bead_binding_wash_elution #2 Load nucleic_acid from the hand-off from chip3_lysis_rt_split_index_pcr_pool_release: FEED inlet_1 20 µL/min (nucleic_acid_sample) | VENT bead_waste_out | rest capped; 3.75 min. Watch: the nucleic_acid arrives in bead_chamber; stop the pump as soon as it is there, before it runs on towards bead_waste_out.
  - chip4_bead_binding_wash_elution #3 Incubate: all capped; 5 min; 22 °C. Watch: temperature steady at 22 °C; nothing moving; capped ports not leaking.

**Step 22** · chip **chip4_bead_binding_wash_elution**, module `bead_chamber`: Wash the beads on a magnet.
  - chip4_bead_binding_wash_elution #4 Exchange: ethanol_wash x2: FEED ethanol_wash_in 20 µL/min (ethanol_wash) | VENT bead_waste_out | rest capped; 15 min; magnet on. Watch: ethanol_wash reaches bead_chamber and leaves at bead_waste_out; no bubbles in the chamber; magnet on, beads stay put.

**Step 23** · chip **chip4_bead_binding_wash_elution**, module `bead_chamber`: Elute the library from the beads.
  - chip4_bead_binding_wash_elution #5 Exchange: elution_buffer: FEED elution_buffer_in 20 µL/min (elution_buffer) | VENT bead_waste_out | rest capped; 3.75 min; 22 °C; magnet on. Watch: elution_buffer reaches bead_chamber and leaves at bead_waste_out; no bubbles in the chamber; magnet on, beads stay put.
  - chip4_bead_binding_wash_elution #6 Incubate: all capped; 2 min; 22 °C; magnet on. Watch: temperature steady at 22 °C; nothing moving; capped ports not leaking.
  - chip4_bead_binding_wash_elution #7 Collect the library (outlet_1): FEED elution_buffer_in 20 µL/min (collection_buffer) | VENT outlet_1 | rest capped; 5 min; magnet on. Watch: the library leaves bead_chamber through outlet_1 into the tube labelled 'chip4_bead_binding_wash_elution: library after step 23'; magnet on so the beads stay behind; never to waste.

**Step 24** · chip **chip3_lysis_rt_split_index_pcr_pool_release**, module `pool_2`: Pool the four cleaned-up sublibraries for sequencing.
  - chip3_lysis_rt_split_index_pcr_pool_release #16 Pool: FEED inlet_1 20 µL/min (library_suspension) | VENT outlet_1 | rest capped; 1.65 min. Watch: material moves from the inlet through pool_2 towards outlet_1; streams merge evenly; no clogging.
  - chip3_lysis_rt_split_index_pcr_pool_release #17 Collect the library (outlet_1): FEED inlet_1 20 µL/min (collection_buffer) | VENT outlet_1 | rest capped; 1.65 min. Watch: the library leaves pool_2 through outlet_1 into the tube labelled 'chip3_lysis_rt_split_index_pcr_pool_release: library after step 24, for chip4_bead_binding_wash_elution inlet_1'; never to waste.

**Step 25** · at the bench: Measure library concentration and fragment size. Equipment: pipette. (Off chip because: Library concentration and size measurement are done on the bench (fluorometer is not on the lab list).)

## Open problems

None found.

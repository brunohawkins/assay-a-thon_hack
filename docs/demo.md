# The dye demo

**Generated file. Do not edit.** `python tools/demo_plan.py --write` rebuilds it from the
chip specs and `contracts/parameters.json`. Hand-written timings are exactly how the old
procedure (`cad/module1_dye_demo.md`) went stale when the boards were rescaled.

## What is on the bench

Printed and hot-embossed chips at 30 x 20 mm, **one** syringe pump, food dye in water, and
plugs for every port that is not the feed or the vent. No cells, no reagents, no
thermocycler, no incubator, no beads, no oil.

## How the flow was chosen

Dye has no shear limit. `shear.culture_ceiling_dyn_cm2` is the **adherent-cell** ceiling and
`capsule.escape_pressure_kpa` is the **capsule** limit; neither applies to coloured water in
an empty chip. Holding a dye fill to the cell ceiling is what left `spc_demo_v1`'s protocol
at 0.071 uL/min - over an hour to fill 4.68 uL, which nobody could film. The two bounds that
do apply are a fill a human can watch (`demo.target_fill_s`, 10-60 s per chamber,
aiming for 30 s) and the bonded seal (`demo.max_drive_pressure_kpa` = 20 kPa,
an **assumption**, see below). Pressures are `operator/flow_calc.py` at
`fluid.viscosity_pa_s` = 0.001 Pa s, water at 20 C, which is the one case that value is for.

Volumes are the specs' own `volume_ul` and channel centrelines from `operator/network.py`
(the DXF where there is one, otherwise cut exactly as `cad/make_train_cad.py` cuts it). The
port hole each feed has to fill first is `operator/module1_operator.py port_hole_ul`: the
contract bore `fabrication_floors.min_port_mm` = 0.8 mm squared, through that chip's own
lid layer. The twin computes it the same way, from the same function's rule, so the bench
sheet and the twin cannot disagree.

Every number below is derived. Nothing is typed in.

## Every chip at a glance

| chip                  | feed       | vent          | uL/min | headline fill | filmable | kPa   |
| ---                   | ---        | ---           | ---:   | ---:          | ---:     | ---:  |
| cell_prep_v1          | in_buffer  | out_cells     | 7.9    | 36 s          | 2        | 0.051 |
| ipsc_to_mgl_chip_v1   | inlet_1    | waste_1       | 8.8    | 30 s          | 2        | 0.012 |
| module1_v1            | p_in       | p_mid         | 6.3    | 30 s          | 2        | 0.007 |
| module3_v1_translated | reagent_in | to_split_tree | 77     | 30 s          | 1        | 0.105 |
| module4_v1            | in_s1      | p_vent        | 16     | 30 s          | 1        | 0.009 |
| module5_v1            | in_prot    | out_lib       | 16     | 30 s          | 2        | 0.010 |
| module6_v1            | in_n       | out_n         | 5.8    | 30 s          | 1        | 0.001 |
| placeholder_chip_v1   | inlet_1    | outlet_1      | 7.3    | 30 s          | 3        | 0.064 |
| sctip_spc_v1          | in_reagent | out_waste     | 2.2    | 30 s          | 1        | 0.020 |
| spc_demo_v1           | in_core    | out_caps      | 9.4    | 30 s          | 1        | 0.309 |

`headline fill` is the largest chamber on the route; `filmable` counts the chambers whose fill time lands inside the 10-60 s window.

## Chip by chip

One feed, one vent, everything else plugged. Each chain below is the order the dye arrives in, with how long that part takes to colour. **Bold is what a viewer can read; the rest goes by too fast to film.** The clock starts when dye reaches the port, and the fast centre of the stream shows about a third sooner than the figure given. None of these are stopwatch readings - write those down separately and compare.

### `cell_prep_v1` - feed `in_buffer` at 7.9 uL/min, vent `out_cells`, plug `in_enz`, `in_quench`

`in_buffer` hole 1.0 s -> `ch_buf_cult` 0.7 s -> **`cult_1` 36 s** -> `ch_cult_mix` 0.5 s -> **`mix_1` 10 s** -> `ch_mix_qc` 0.7 s -> `qc_1` 5.8 s -> `ch_qc_out` 0.6 s -> out of `out_cells` at 55 s.

7.287 uL in all (0.128 uL of it lost in the feed port before anything shows). Drive pressure 0.051 kPa, 0.3% of the assumed limit. Clear-water wash-out of `cult_1`: 1/e every 36 s, clear by about 108 s (well-mixed chamber, APPROXIMATE).

Repeat from `in_enz`, `in_quench` one at a time for the rest of the chip.

**Cannot show:** the streams meeting at mix_1: they come from 3 inlets (in_buffer, in_enz, in_quench) and one pump drives one at a time, so they never meet at once; every thermal step protocols/cell_prep_v1.json asks for (4, 37 C): no thermocycler and no incubator on the bench.

### `ipsc_to_mgl_chip_v1` - feed `inlet_1` at 8.8 uL/min, vent `waste_1`, plug `outlet_1`

`inlet_1` hole 13 s -> `ch_in_m1` 0.5 s -> **`m1_expand` 30 s** -> `ch_m1_m2` 0.8 s -> **`m2_hemogenic` 30 s** -> `ch_m2_waste` 2.6 s -> out of `waste_1` at 77 s.

11.342 uL in all (1.92 uL of it lost in the feed port before anything shows). Drive pressure 0.012 kPa, <0.1% of the assumed limit. Clear-water wash-out of `m1_expand`: 1/e every 30 s, clear by about 90 s (well-mixed chamber, APPROXIMATE).

Other vents, same flow: `outlet_1` carries the dye on into `m2_hemogenic`, `m3_concentrate`, `mat_2`, `m5_harvest` (+75 s).

**Cannot show:** retention in m3_concentrate, mat_1, mat_2, mat_3, mat_4, m5_harvest: a weir at 25 um retains NOTHING with no particles in the liquid. Dye passes it either way, so the demo says nothing about whether it would hold a capsule; every thermal step protocols/ipsc_to_mgl_v1.json asks for (4, 37 C): no thermocycler and no incubator on the bench; m3_concentrate, mat_1, mat_2, mat_3, mat_4, m5_harvest: not on the one-pump route, so they stay empty (a second run with a different vent, or a second pump, would reach them).

### `module1_v1` - feed `p_in` at 6.3 uL/min, vent `p_mid`, plug `p_a_in`, `p_a_out`, `p_g_in`, `p_g_out`

`p_in` hole 18 s -> `ch_in_n` 2.5 s -> **`n_cx` 30 s** -> `ch_n_j` 1.3 s -> **`j_mid` 16 s** -> `ch_j_mid` 0.4 s -> out of `p_mid` at 69 s.

7.232 uL in all (1.92 uL of it lost in the feed port before anything shows). Drive pressure 0.007 kPa, <0.1% of the assumed limit. Clear-water wash-out of `n_cx`: 1/e every 30 s, clear by about 89 s (well-mixed chamber, APPROXIMATE).

Other vents, same flow: `p_a_out` carries the dye on into `j_mid`, `a_cx` (+52 s); `p_g_out` carries the dye on into `j_mid`, `g_cx` (+52 s). Repeat from `p_a_in`, `p_g_in` one at a time for the rest of the chip.

**Cannot show:** retention in g_cx, a_cx: a weir at 25 um retains NOTHING with no particles in the liquid. Dye passes it either way, so the demo says nothing about whether it would hold a capsule; the streams meeting at j_mid: they come from 3 inlets (p_a_in, p_g_in, p_in) and one pump drives one at a time, so they never meet at once; every thermal step protocols/module1_v1.json asks for (4, 37 C): no thermocycler and no incubator on the bench; g_cx, a_cx: not on the one-pump route, so they stay empty (a second run with a different vent, or a second pump, would reach them).

### `module3_v1_translated` - feed `reagent_in` at 77 uL/min, vent `to_split_tree`, plug `backflush_in`, `capsule_in`, `lysis_waste`, `waste`

`reagent_in` hole 0.5 s -> `ch_reagent` 0.2 s -> **`main_trap` 30 s** -> `ch_to_split_tree` 0.1 s -> out of `to_split_tree` at 31 s.

39.466 uL in all (0.64 uL of it lost in the feed port before anything shows). Drive pressure 0.105 kPa, 0.5% of the assumed limit. Clear-water wash-out of `main_trap`: 1/e every 30 s, clear by about 90 s (well-mixed chamber, APPROXIMATE).

Repeat from `backflush_in`, `capsule_in` one at a time for the rest of the chip.

**Cannot show:** retention in main_trap: a weir at 25 um retains NOTHING with no particles in the liquid. Dye passes it either way, so the demo says nothing about whether it would hold a capsule; every thermal step protocols/module3_v1.json asks for (42 C): no thermocycler and no incubator on the bench; capsule_prefilter: not on the one-pump route, so it stays empty (a second run with a different vent, or a second pump, would reach it).

### `module4_v1` - feed `in_s1` at 16 uL/min, vent `p_vent`, plug `in_s2`, `in_s3`, `in_s4`, `out_sub`

`in_s1` hole 2.4 s -> `ch_in_1` 3.8 s -> **`sub_1` 30 s** -> `ch_tap_1` 0.5 s -> `node_1` 0.4 s -> `ch_vent` 0.2 s -> out of `p_vent` at 37 s.

9.942 uL in all (0.64 uL of it lost in the feed port before anything shows). Drive pressure 0.009 kPa, <0.1% of the assumed limit. Clear-water wash-out of `sub_1`: 1/e every 30 s, clear by about 90 s (well-mixed chamber, APPROXIMATE).

Other vents, same flow: `out_sub` carries the dye on into `node_1`, `node_2`, `node_3`, `node_4` (+12 s). Repeat from `in_s2`, `in_s3`, `in_s4` one at a time for the rest of the chip.

**Cannot show:** the streams meeting at node_1, node_2, node_3, node_4: they come from 4 inlets (in_s1, in_s2, in_s3, in_s4) and one pump drives one at a time, so they never meet at once; every thermal step protocols/module4_v1.json asks for (4, 60, 72, 98 C): no thermocycler and no incubator on the bench; sub_2, sub_3, sub_4, node_2, node_3, node_4: not on the one-pump route, so they stay empty (a second run with a different vent, or a second pump, would reach them).

### `module5_v1` - feed `in_prot` at 16 uL/min, vent `out_lib`, plug `in_bead`, `in_lib`, `out_waste`

`in_prot` hole 2.4 s -> `ch_in_prot` 1.7 s -> **`react_cx` 30 s** -> `ch_react_bead` 5.0 s -> **`bead_cx` 22 s** -> `ch_out_lib` 2.0 s -> out of `out_lib` at 64 s.

17.057 uL in all (0.64 uL of it lost in the feed port before anything shows). Drive pressure 0.010 kPa, <0.1% of the assumed limit. Clear-water wash-out of `react_cx`: 1/e every 30 s, clear by about 91 s (well-mixed chamber, APPROXIMATE).

Other vents, same flow: `out_waste` carries the dye on into `bead_cx` (+34 s). Repeat from `in_bead`, `in_lib` one at a time for the rest of the chip.

**Cannot show:** every thermal step protocols/module5_v1.json asks for (37 C): no thermocycler and no incubator on the bench.

### `module6_v1` - feed `in_n` at 5.8 uL/min, vent `out_n`, plug `in_a`, `in_g`, `out_a`, `out_g`

`in_n` hole 6.6 s -> `comb_in_n` 20 s -> **`cx_n` 30 s** -> `comb_out_n` 20 s -> out of `out_n` at 76 s.

7.36 uL in all (0.64 uL of it lost in the feed port before anything shows). Drive pressure 0.001 kPa, <0.1% of the assumed limit. Clear-water wash-out of `cx_n`: 1/e every 30 s, clear by about 89 s (well-mixed chamber, APPROXIMATE).

Repeat from `in_a`, `in_g` one at a time for the rest of the chip.

**Cannot show:** every thermal step protocols/module6_v1.json asks for (-20, 4, 37 C): no thermocycler and no incubator on the bench; cx_g, cx_a: not on the one-pump route, so they stay empty (a second run with a different vent, or a second pump, would reach them).

### `placeholder_chip_v1` - feed `inlet_1` at 7.3 uL/min, vent `outlet_1`, plug nothing (every port is in use)

`inlet_1` hole 10 s -> `ch_1` 1.3 s -> **`exchange_1` 30 s** -> `ch_3` 2.1 s -> **`split_2` 10 s** -> `ch_7` 1.6 s -> **`pool_1` 24 s** -> `ch_10` 2.6 s -> out of `outlet_1` at 82 s.

10.024 uL in all (1.28 uL of it lost in the feed port before anything shows). Drive pressure 0.064 kPa, 0.3% of the assumed limit. Clear-water wash-out of `exchange_1`: 1/e every 30 s, clear by about 90 s (well-mixed chamber, APPROXIMATE).

**Cannot show:** retention in exchange_1, split_1, split_2, split_3, split_4, pool_1: a weir at 25 um retains NOTHING with no particles in the liquid. Dye passes it either way, so the demo says nothing about whether it would hold a capsule; every thermal step protocols/placeholder_culture_then_scrnaseq.json, protocols/placeholder_scdnaseq.json, protocols/placeholder_scrnaseq.json asks for (37, 40, 50, 80 C): no thermocycler and no incubator on the bench; split_1, split_3, split_4: not on the one-pump route, so they stay empty (a second run with a different vent, or a second pump, would reach them).

### `sctip_spc_v1` - feed `in_reagent` at 2.2 uL/min, vent `out_waste`, plug `in_load`, `in_oil`, `out_drop`

`in_reagent` hole 3.5 s -> `ch_wash_trap` 10 s -> **`trap_1` 30 s** -> `ch_trap_waste` 3.4 s -> out of `out_waste` at 47 s.

1.716 uL in all (0.128 uL of it lost in the feed port before anything shows). Drive pressure 0.020 kPa, 0.1% of the assumed limit. Clear-water wash-out of `trap_1`: 1/e every 30 s, clear by about 89 s (well-mixed chamber, APPROXIMATE).

Repeat from `in_load`, `in_oil` one at a time for the rest of the chip.

**Cannot show:** anything two-phase at pack_1, nozzle_2, brk_1: droplets need oil and an aqueous stream at once, and there is no oil and one pump; the streams meeting at pack_1, nozzle_2: they come from 3 inlets (in_load, in_oil, in_reagent) and one pump drives one at a time, so they never meet at once; every thermal step protocols/sctip_spc_v1.json asks for (37, 72 C): no thermocycler and no incubator on the bench; pack_1, nozzle_2, brk_1: not on the one-pump route, so they stay empty (a second run with a different vent, or a second pump, would reach them).

### `spc_demo_v1` - feed `in_core` at 9.4 uL/min, vent `out_caps`, plug `in_oil`, `in_shell`

`in_core` hole 0.8 s -> `ch_core_aq` 1.4 s -> `aq_merge` 0.1 s -> `ch_aq_xj` 0.8 s -> `xj_1` 0.1 s -> `ch_xj_delay` 0.2 s -> **`delay_1` 30 s** -> `ch_delay_out` 0.3 s -> out of `out_caps` at 34 s.

5.263 uL in all (0.128 uL of it lost in the feed port before anything shows). Drive pressure 0.309 kPa, 1.5% of the assumed limit. Clear-water wash-out of `delay_1`: 1/e every 30 s, clear by about 90 s (well-mixed chamber, APPROXIMATE).

Repeat from `in_oil`, `in_shell` one at a time for the rest of the chip.

**Cannot show:** anything two-phase at xj_1: droplets need oil and an aqueous stream at once, and there is no oil and one pump; the streams meeting at aq_merge, xj_1: they come from 3 inlets (in_core, in_oil, in_shell) and one pump drives one at a time, so they never meet at once; oil_split, oil_nw, oil_ne, oil_sw, oil_se: not on the one-pump route, so they stay empty (a second run with a different vent, or a second pump, would reach them).

## Pump off: diffusion

Stop the pump, plug every port, and the only thing left is diffusion. The front spreads as sqrt(2 D t) with D = `dye.diffusivity_m2_s` = 4e-10 m2/s (itself a TODO placeholder, so treat these as an order of magnitude):

- 1 min: about 0.22 mm
- 10 min: about 0.69 mm
- 1 hour: about 1.70 mm
- overnight (12 h): about 5.88 mm

So within a demo you see a sharp dye interface at a chamber mouth go soft by a fraction of a millimetre: real, filmable under macro, and nothing more. The old procedure's "8.8 mm in 12 hours" is 1.5x this estimate - the same order, so that one figure survived the rescale. It is still an overnight claim, not something to show on the day.

## The pressure limit is an assumption

`demo.max_drive_pressure_kpa` = 20 kPa is a **deliberately conservative placeholder,
not a measurement**. The contracts held no bond-strength value because nobody has burst a
real chip. Until someone pressurises one to failure and writes the number down, treat this
as a ceiling to stay well inside, never as any chip's strength. The worst route above
(`spc_demo_v1`) sits at 0.309 kPa, 1.5% of the limit, so the limit is not what sets
any flow here - the visible-fill window is. That also means the limit could be badly wrong in
either direction without changing a single number above, which is the honest reason to measure it.

## What this demo does not prove

Food dye in water through an empty chip. It shows that the liquid goes where the drawing
says and that the chip is sealed enough to run at all. It proves **none** of the following,
and no number from the demo should be quoted for any of them:

- **No cell biology.** No cells are loaded. Nothing about attachment, viability,
  differentiation, lineage identity, conditioned medium or any cell response.
- **No thermal step.** Nothing heats or cools the chip: no 37 C culture, no 4 C hold,
  no 42 C reverse transcription, no 98 C index PCR, no ramp rate.
- **No reagent compatibility.** Water and food dye only. Nothing about the chaotropic lysis
  buffer, enzymes, oil, photoinitiator or solvents against the resin, the film or the
  adhesive - and `contracts/materials.json` lists those as TODO for a reason.
- **No weir capture at the real gap.** With no particles in the liquid, a 25 um weir retains
  nothing by definition. The demo cannot distinguish a correctly built weir from a missing one.
- **No weeks-long sealing.** A bond that holds for a ten-minute dye run says nothing about
  evaporation, creep, delamination or sterility over the weeks of culture the design assumes.
- **No DNA adsorption.** Nucleic acid losses to the wetted surfaces are untested and
  untestable without nucleic acid.
- **No droplets.** Flow focusing needs oil plus two aqueous streams at once. One pump and no
  oil means no droplet is formed, so nothing about size, monodispersity or core-shell structure.
- **No single-cell barcoding.** No capsules, no emulsion, no barcode pool. Nothing about
  collision rate, Poisson loading or demultiplexing.
- **No CFD validation.** The times above come from one-dimensional volume/flow arithmetic
  and the rectangular-channel resistance formula. Comparing them with a stopwatch checks the
  geometry and the pump; it does not validate any flow simulation, and the measurement should
  be recorded as a measurement, not folded into the prediction.

## Day-of risks

| Risk | What it looks like | Mitigation |
|---|---|---|
| Connecting tubing to a 0.8 mm port | The bore is `fabrication_floors.min_port_mm` = 0.8 mm. Push-fit tubing either will not enter or will not seal, and forcing it splits the printed boss. | Go in with a blunt dispensing needle rather than bare tubing: the published gauge dimensions put 21G at about 0.82 mm outside diameter and 20G at about 0.91 mm, so 21G is the one that enters a 0.8 mm hole and 20G is an interference fit. Seat it with a drop of adhesive or a short silicone sleeve, test every joint in a scrap chip first, and never push against a plugged port. |
| Tubing dead volume against an ~11 uL chip | A metre of fine tubing holds many times the whole chip. Dye spends minutes in the line while the chamber stays clear, and the demo looks broken. | Keep the lines as short as they can physically be, prefill them with dye to the port face before connecting, and start the stopwatch at the port, not at the pump. The times in this file begin at the port and exclude all tubing. |
| Priming and bubbles | One trapped bubble in a 0.2 mm x 0.3 mm channel stops the flow, splits the stream or parks in the chamber and sits there as a white blob on camera. | Prime the whole chip with plain water first and confirm every chamber is clear before any dye. Fill syringes nose-up and tap them out. Introduce dye only into an already-wet chip. If a bubble lodges, raise the flow briefly rather than rapping the chip. |
| Unused ports not plugged | An open port is a vent. The dye leaves by the nearest hole, the chamber never fills, and it looks like a design fault. A switched-off pump on an open line is also a vent, not a plug. | Plug every port in the plan's plug list, physically, before starting. Count the plugs against the list out loud. A stopped pump still needs its port plugged. |
| Dye staining the resin | Food dye in a printed part can tint the walls, so the "after" state never clears and a second run on the same chip reads wrong. | Treat the first dye run on a chip as the only trustworthy one. Keep one chip per chamber state, run clear water last, and do not try to reuse a stained chip for the clean-fill shot. |
| Hazy printed resin hiding the dye | `contracts/materials.json` says printed resin's clear grades are "translucent to slightly hazy and yellow with age; not a reliable optical window". Front-lit, a 0.3 mm deep channel of dye is nearly invisible. | **Backlight it.** Put a diffuse white light or a phone torch through paper behind the chip and shoot the transmitted image. Pick a dye colour with contrast against the resin's yellow (blue or green beats yellow or red). |
| 30 x 20 mm is too small to see | The audience sees a thumbnail-sized blank rectangle; the whole chip is smaller than a credit card and the chambers are a few millimetres. | Phone on a stand, macro mode, locked focus and exposure, chip taped down so nothing drifts. Put a ruler in the shot for scale, and frame one chamber rather than the whole board. Record, do not rely on anyone leaning in. |
| One chip, one chance | A split boss, a leak, a stained chamber or a blocked channel ends the demo if it is the only chip. | Print spares - at least two of each chip you intend to show - and have a second syringe, a second set of plugs and pre-cut spare tubing on the table. Run every chip through a water prime the day before, not on the day. |

## The printed chips

6 chips are printed for the demo, confirmed by the project lead: `module1_v1`, `module3_v1_translated`, `module4_v1`, `module5_v1`, `module6_v1`, `spc_demo_v1`. `built_for_demo` in the pipeline files is set from that list by `tools/build_assay_variants.py` (`PRINTED_FOR_DEMO`). They are FROZEN: `cad/make_train_cad.py` refuses to rewrite their exports, and a change means a new versioned spec. Each one's procedure, in full in its section above:

| chip | feed | flow | vent | plug | readable fills |
|---|---|---:|---|---|---|
| `module1_v1` | `p_in` | 6.3 uL/min | `p_mid` | `p_a_in`, `p_a_out`, `p_g_in`, `p_g_out` | `n_cx` 30 s, `j_mid` 16 s |
| `module3_v1_translated` | `reagent_in` | 77 uL/min | `to_split_tree` | `backflush_in`, `capsule_in`, `lysis_waste`, `waste` | `main_trap` 30 s |
| `module4_v1` | `in_s1` | 16 uL/min | `p_vent` | `in_s2`, `in_s3`, `in_s4`, `out_sub` | `sub_1` 30 s |
| `module5_v1` | `in_prot` | 16 uL/min | `out_lib` | `in_bead`, `in_lib`, `out_waste` | `react_cx` 30 s, `bead_cx` 22 s |
| `module6_v1` | `in_n` | 5.8 uL/min | `out_n` | `in_a`, `in_g`, `out_a`, `out_g` | `cx_n` 30 s |
| `spc_demo_v1` | `in_core` | 9.4 uL/min | `out_caps` | `in_oil`, `in_shell` | `delay_1` 30 s |

Not printed, so their sections above are for reference only: `cell_prep_v1`, `ipsc_to_mgl_chip_v1`, `placeholder_chip_v1`, `sctip_spc_v1`.

# Where the pipeline needs heating, cooling and thermocycling

Every thermal step from cell prep to finished library. "Typical" means standard lab
practice, not a CapSeq-specific value — the CapSeq supplementary methods are not pulled yet.

> ⚠️ **"RT" is banned from the tables below, because it meant two different things in the
> one table whose job is to stop someone cooking cells.** It was used for *room temperature*
> in some rows and *reverse transcription* in others. From here on:
>
> | Written as | Means |
> |---|---|
> | **ambient (~22 °C)** | room temperature. A setpoint |
> | **RTx** | reverse transcription. A step, which runs at 42–50 °C |
>
> Nothing in this file says "RT" on its own again.

## The short answer

**Only ONE step is true thermocycling: the indexing PCR (step 15).** Everything else is
an isothermal hold. That single fact sets the hardware: you need one thermocycler, not a
thermal system per chip.

| | |
|---|---|
| True thermocycling | Indexing PCR only, a standard indexing PCR cycle count at 98 / 60 / 72 °C |
| Needs cooling below ambient | GelMA gelation (4 °C), emulsion break, enzyme/library storage, **and Module 6's −20 °C extraction soak, which is the one step that needs a SUB-AMBIENT SOURCE and not just ice** |
| Runs in a plain 37 °C incubator | Dissociation, all culture, interaction window, optional IVT |
| Incompatible with living culture, so cannot share a chip with it | Everything from **RTx** (reverse transcription, 42 °C) onward, up to 98 °C |

⚠️ **This section used to end "No chiller, no water jacket", and that is now wrong.** It
was written before Module 6 existed. See *The −20 °C problem*, below, for the decision and
why the alternative was rejected.

## Full map

| # | Step | °C | Duration | Mode | Hardware |
|---|---|---|---|---|---|
| 1a | Dissociation reagent | 37 | 3–10 min | hold | incubator |
| 1b | Wash, filter, viability | ambient / 4 | min | hold | ice optional |
| 2a | GelMA droplet generation | ~37–40 | run | hold | heated syringe + inlet |
| 2b | **GelMA gelation** | **4** | 15–30 min | hold | **Peltier / fridge / ice** |
| 2c | Emulsion break, washes | 4 → ambient | min | hold | cold block |
| 2d | 405 nm LAP crosslink | ambient | s–min | hold | none |
| C | **Culture + interaction + harvest** | **37, 5% CO₂** | days–weeks | hold | **incubator** |
| 3a | Lysis | ambient | per protocol | hold | none |
| 3b | Washes | ambient / 4 | min | hold | none |
| 3c | **RTx** (reverse transcription) | 42–50 | 10–90 min | hold ⚠️ | dry block |
| **15** | **Indexing PCR (BC4)** | **98 / 60 / 72** | ~1 h | **THERMOCYCLED, standard indexing cycle count, 2–4 °C/s** | **thermocycler or Peltier** |
| **16a** | Matrix-digesting enzyme release | 37 | 30 min–h | hold | incubator/block |
| 16b | Heat-kill (only if a thermostable enzyme is used) | ~95 | 5–10 min | hold (single) | block |
| 16c | SPRI cleanup | ambient | ~15 min | hold | none |
| **17** | QC, storage | 4 / −20 | — | — | fridge/freezer |

### Module 6, the bulk metabolome + proteome arm

Module 6 is a separate branch, not a step in the sequence above: it is the extraction
**unit cell** that `chip/module1_target_v1.json` tiles, and it takes adherent cells
*before* encapsulation. It is also the only part of the pipeline that asks for a setpoint
below 0 °C on a chip. All four setpoints land on **one stage under all three lanes**
(`tz_lane_fields` in `chip/module6_v1.json`), which is why
`protocols/module6_v1.json` is ordered by phase and not by lane.

| # | Step | °C | Duration | Mode | Hardware |
|---|---|---|---|---|---|
| 6a | Prime, seed, attach, culture | 37 | days | hold | **same stage**, incubator-equivalent |
| 6b | Pre-quench rinse | 4 | ~23 s | hold | **same stage** |
| 6c | Quench flood | 4 stage, solvent delivered at −20 | ~1 s | hold | **cold reagent reservoir off chip**, stage still at 4 |
| 6d | **Extraction soak** | **−20** | 5 min | hold | **sub-ambient stage — NEW REQUIREMENT** |
| 6e | Flush + resolubilisation | ambient (22) | ~10 min | hold | same stage, no heat |
| 6f | Fraction M / P on the cold block | 4 | min | hold | cold block |

Three ordering rules come straight out of there being one stage, and the protocol obeys
all three:

1. **No lane can be cold while another is alive.** One stage, so every lane crosses each
   setpoint together.
2. **The stage stays at 4 °C through the quench flood.** The solvent arrives cold from an
   off-chip reservoir; the stage only drops to −20 °C once *every* lane holds organic
   solvent. Ramping to −20 °C with aqueous rinse buffer still in a lane would freeze it,
   which is the slow freeze-quench the comb geometry exists to avoid (2–10 K/s is 6–30 s,
   far slower than the ~1 s flood).
3. **Back to ambient before the aqueous solubilisation buffer goes in**, for the same
   reason in reverse.

## The −20 °C problem

Module 6's extraction soak is 5 min at −20 °C. That is the published floor for a cold
sequential extraction ([Sapcariu 2014](https://doi.org/10.1016/j.mex.2014.07.002),
[Coman 2016](https://doi.org/10.1074/mcp.M115.053702)); the 30 s at 4 °C the protocol used
to do under-extracts and may not even form the protein film that fraction 2 depends on.
There were exactly two ways out and they are mutually exclusive.

**Rejected — do the soak off chip.** You cannot. The precipitated protein is a film on the
*chamber floor*; moving the liquid off chip for the soak leaves the substrate for
fraction 2 behind. The whole point of this module is that both fractions come out of one
extraction in one place.

**Chosen — the rig gets a sub-ambient source.** Concretely, one of:

- a **two-stage (cascaded) Peltier** with a water- or fan-cooled hot side. 22 °C ambient to
  −20 °C is a 42 K lift, past what a single stage holds with a plain finned heatsink; or
- a small **recirculating chiller** feeding a cold plate, which the lab does not have today.

Either way: **the rig now needs a sub-ambient source, and the "no chiller, no water jacket"
line below no longer holds.** It is one plate serving one 30 × 20 mm board, not a per-chip
thermal system, so the "one thermocycler, not a thermal system per chip" headline survives.

**Degraded mode, if the sub-ambient source is not built in time.** Run the soak at 4 °C on
the existing cold block and lengthen it. That is off the published method, so the
extraction yield and the metabolome/proteome split become unvalidated and must be labelled
as such — it is a fallback for a dry run, not a result.

**Still unmeasured.** Whether a stage under a 0.5 mm COC base actually pulls the 0.3 mm
lane to −20 °C, and how long it really takes. `contracts/parameters.json` gives
`thermal.max_ramp_c_per_s` = 2 and `thermal.tau_s` = 30 as placeholders, so the protocol
budgets ramp + 3 τ (about 102 s from 4 °C to −20 °C) and both numbers need a thermocouple
on a coupon.

⚠️ **3c caveat.** SPLiT-seq ([10.1126/science.aam8999](https://doi.org/10.1126/science.aam8999))
uses a *stepped* RTx ramp rather than a flat hold. If CapSeq copies it, that step counts as
cycled too. Worth confirming before anyone builds a heater for it.

Note 16a: **a broad-spectrum protease does not open these capsules** — published work has them
stay intact and keep their DNA through an overnight digest. A matrix-digesting enzyme cuts the
gelatin backbone between crosslinks until the network dissolves, and it works at 37 °C, so step
16 needs no heat at all beyond an incubator. A divalent-cation chelator inhibits that enzyme, so
it must be absent from the incoming buffer.

Note 16b: the 95 °C heat-kill is a **single hold**, not cycling, and is avoidable with a
thermolabile enzyme.

## Six setpoints, four pieces of hardware

−20 °C · 4 °C · ambient (~22) · 37 °C · 42–56 °C · 98/60/72 °C cycled — but they collapse onto:

1. **CO₂ incubator (37 °C)** — dissociation, all culture, interaction window, IVT
2. **Thermocycler with heated lid** — covers *every* post-harvest setpoint: ligations,
   RTx, protease, heat-kill, PCR, 4 °C hold
3. **Fridge + ice** — gelation, emulsion break, storage
4. **Sub-ambient stage (−20 °C)** — Module 6's extraction soak only, plus holding its
   collected fractions cold. Two-stage Peltier with a cooled hot side, or a chiller plate.
   See *The −20 °C problem*. This is the item the earlier version of this file said we did
   not need.

Optional: a PID resistive heater for the GelMA inlet (~37–40 °C), and a second dry block
at 37 °C so ligations don't tie up the cycler. A water jacket is still not needed — but a
chiller, or a cascaded Peltier doing the same job, now is.

## Three consequences for chip layout

- **The culture chip must never exceed 37 °C.** Not because 42 °C is instantly lethal — it
  is not; 42 °C is a classic **heat-shock** setpoint, and cells survive it for a while
  while mounting a stress response that would wreck any transcriptome we then measure.
  Above that it does become lethal, and the library steps run to 98 °C. Either way the
  culture board cannot share a thermal stage with library prep, so **the conclusion is
  unchanged: separate boards.** That is why modules 1–3 and the library chips are separate.
- **The hardest thermal requirement is the PCR**, and it is the only one worth agonising
  over. In a tube on a commercial cycler it is trivial. See `module_d_design.md` for
  whether we do it on chip at all.
- **One stage per board, not per lane.** Module 6 wants 37 °C and −20 °C on the same
  30 × 20 mm board at different times, and three lanes sit on 10 mm centres over a 0.5 mm
  COC base. Independently controlled neighbours at those setpoints would fight each other
  through the substrate, so the chip declares **one** zone over all three lanes and the
  protocol is ordered by phase. Anything that wants per-lane temperature control needs
  lanes much further apart, or a thermal break in the base, and neither is designed.

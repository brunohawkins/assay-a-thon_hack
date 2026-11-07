# Where the pipeline needs heating, cooling and thermocycling

Every thermal step from cell prep to finished library. "Typical" means standard lab
practice, not a CapSeq-specific value — the CapSeq supplementary methods are not pulled yet.

## The short answer

**Only ONE step is true thermocycling: the indexing PCR (step 15).** Everything else is
an isothermal hold. That single fact sets the hardware: you need one thermocycler, not a
thermal system per chip.

| | |
|---|---|
| True thermocycling | Indexing PCR only, a standard indexing PCR cycle count at 98 / 60 / 72 °C |
| Needs cooling below ambient | GelMA gelation (4 °C), emulsion break, enzyme/library storage |
| Runs in a plain 37 °C incubator | Dissociation, all culture, interaction window, optional IVT |
| Lethal to cells, so cannot share a chip with culture | Everything from RT onward (42–98 °C) |

## Full map

| # | Step | °C | Duration | Mode | Hardware |
|---|---|---|---|---|---|
| 1a | Dissociation reagent | 37 | 3–10 min | hold | incubator |
| 1b | Wash, filter, viability | RT / 4 | min | hold | ice optional |
| 2a | GelMA droplet generation | ~37–40 | run | hold | heated syringe + inlet |
| 2b | **GelMA gelation** | **4** | 15–30 min | hold | **Peltier / fridge / ice** |
| 2c | Emulsion break, washes | 4 → RT | min | hold | cold block |
| 2d | 405 nm LAP crosslink | RT | s–min | hold | none |
| C | **Culture + interaction + harvest** | **37, 5% CO₂** | days–weeks | hold | **incubator** |
| 3a | Lysis | RT | per protocol | hold | none |
| 3b | Washes | RT / 4 | min | hold | none |
| 3c | Reverse transcription | 42–50 | 10–90 min | hold ⚠️ | dry block |
| **15** | **Indexing PCR (BC4)** | **98 / 60 / 72** | ~1 h | **THERMOCYCLED, standard indexing cycle count, 2–4 °C/s** | **thermocycler or Peltier** |
| **16a** | Matrix-digesting enzyme release | 37 | 30 min–h | hold | incubator/block |
| 16b | Heat-kill (only if a thermostable enzyme is used) | ~95 | 5–10 min | hold (single) | block |
| 16c | SPRI cleanup | RT | ~15 min | hold | none |
| **17** | QC, storage | 4 / −20 | — | — | fridge/freezer |

⚠️ **3c caveat.** SPLiT-seq ([10.1126/science.aam8999](https://doi.org/10.1126/science.aam8999))
uses a *stepped* RT ramp rather than a flat hold. If CapSeq copies it, that step counts as
cycled too. Worth confirming before anyone builds a heater for it.

Note 16a: **a broad-spectrum protease does not open these capsules** — published work has them
stay intact and keep their DNA through an overnight digest. A matrix-digesting enzyme cuts the
gelatin backbone between crosslinks until the network dissolves, and it works at 37 °C, so step
16 needs no heat at all beyond an incubator. A divalent-cation chelator inhibits that enzyme, so
it must be absent from the incoming buffer.

Note 16b: the 95 °C heat-kill is a **single hold**, not cycling, and is avoidable with a
thermolabile enzyme.

## Five setpoints, three pieces of hardware

4 °C · RT · 37 °C · 42–56 °C · 98/60/72 °C cycled — but they collapse onto:

1. **CO₂ incubator (37 °C)** — dissociation, all culture, interaction window, IVT
2. **Thermocycler with heated lid** — covers *every* post-harvest setpoint: ligations,
   RT, protease, heat-kill, PCR, 4 °C hold
3. **Fridge + ice** — gelation, emulsion break, storage

Optional: a PID resistive heater for the GelMA inlet (~37–40 °C), and a second dry block
at 37 °C so ligations don't tie up the cycler. **No chiller, no water jacket.**

## Two consequences for chip layout

- **The culture chip must never exceed 37 °C.** Everything from RT (42 °C) onward kills
  cells, so library prep cannot share a board with culture. That is why modules 1–3 and
  the library chips are separate.
- **The hardest thermal requirement is the PCR**, and it is the only one worth agonising
  over. In a tube on a commercial cycler it is trivial. See `module_d_design.md` for
  whether we do it on chip at all.

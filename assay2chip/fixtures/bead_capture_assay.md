<!-- Synthetic fixture for assay2chip S0/S1. Written for this repo in generic,
     published-style method language. Every reagent name is a placeholder and every
     number is invented. Nothing here comes from any lab's records. -->
<!-- page 1 -->

# Droplet encapsulation and bead capture of single cells

## Abstract

We describe a generic single-cell capture workflow in which a suspension of dissociated
cells is co-flowed with barcoded capture beads and an immiscible carrier oil, encapsulated
in monodisperse droplets, held for lysis and capture, and then broken to recover the bead
pool for downstream library preparation. The workflow is written against placeholder
reagents so that it can be used as a worked example of a protocol with a step table.

## Materials

- cell_suspension, dissociated and filtered to remove aggregates
- capture_beads, nominal diameter 30 um, supplied in bead_buffer
- lysis_buffer
- wash_buffer
- oil_phase with surfactant
- break_solution

<!-- page 2 -->

## Method

Cells are counted and diluted in medium to the working concentration given in Table 1. The
bead suspension is vortexed immediately before use, because settled beads give an uneven
occupancy. Both suspensions and the oil phase are loaded into the device and co-flowed at
the rates in Table 1; droplet formation is checked by eye before collection begins.

### Table 1. Encapsulation and capture steps

| Step | Operation | Reagent | Volume (uL) | Time (min) | Temperature (C) |
|---|---|---|---|---|---|
| 1 | Dilute cells | cell_suspension | 200 | 5 | 20 |
| 2 | Resuspend beads | capture_beads in bead_buffer | 100 | 2 | 20 |
| 3 | Co-flow and encapsulate | cell_suspension + capture_beads + oil_phase | 400 | 15 | 20 |
| 4 | Lyse in droplet | lysis_buffer | 50 | 10 | 25 |
| 5 | Capture on bead | none added | 0 | 30 | 25 |
| 6 | Break emulsion | break_solution | 200 | 5 | 20 |
| 7 | Wash bead pool | wash_buffer | 500 | 10 | 4 |
| 8 | Collect bead pool | wash_buffer | 100 | 5 | 4 |

After the emulsion is broken, the beads are allowed to settle and the oil layer is removed
from below. The bead pool is washed three times with wash_buffer; each wash is one cycle of
step 7 and the supernatant is discarded each time.

<!-- page 3 -->

## Notes on operation

1. Keep the bead suspension moving. Beads that settle in the inlet line change the
   occupancy part way through a run.
2. Collect droplets into a vessel pre-filled with oil_phase so that the collected emulsion
   does not shear at the air interface.
3. Discard the first 30 s of collection, which contains the unstable droplets formed while
   the flows equilibrate.
4. The wash in step 7 is the only cold step; everything before it is at or near room
   temperature.

## Expected outcome

A washed bead pool in a volume of about 100 uL, suitable for off-chip library preparation.
Occupancy and capture efficiency are read out by counting beads before and after capture.

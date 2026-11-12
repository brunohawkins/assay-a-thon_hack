<!-- Synthetic demo fixture for assay2chip. Written for this repo in generic,
     published-style method language. Every reagent name is a placeholder and every
     number is invented. Nothing here comes from any lab's records.
     It is deliberately SMALL (three steps) so the whole S0-S12 chain runs in front of an
     audience; it takes the same code path as the full fixtures. -->
<!-- page 1 -->

# Bead wash and recovery

## Abstract

A pool of capture beads is loaded, washed and recovered for off-chip library
preparation. Written against placeholder reagents as a worked example.

## Materials

- capture_beads, nominal diameter 30 um, supplied in bead_buffer
- wash_buffer

## Method

### Table 1. Steps

| Step | Operation | Reagent | Volume (uL) | Time (min) | Temperature (C) |
|---|---|---|---|---|---|
| 1 | Load bead pool | capture_beads in bead_buffer | 100 | 5 | 20 |
| 2 | Wash bead pool | wash_buffer | 500 | 10 | 20 |
| 3 | Collect bead pool | wash_buffer | 100 | 5 | 20 |

The bead pool is washed three times with wash_buffer; each wash is one cycle of step 2.

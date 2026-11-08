<!-- Synthetic fixture for assay2chip S0/S1: the HARD case. Written for this repo in
     generic, published-style method language, then deliberately degraded the way a
     supplementary methods section degrades when it is lifted out of a PDF. All reagent
     names are placeholders and all numbers are invented. Nothing here comes from any
     lab's records.

     What it is here to test:
       - irregular step numbering (1., 2), Step 3, 3b., 5, then 4)
       - a step whose instruction is split across two sentences and two blocks
       - a volume that exists ONLY in a table footnote
       - a pipe table with no header delimiter row
       - a whitespace-aligned table with a ragged row
       - a table inside a fenced block
       - a value the source simply never gives, which S1 must record as unresolved -->
<!-- page 11 -->

Supplementary Methods: on-chip wash and lysis of adherent cells
==============================================================

Cells were seeded into the culture chamber and left to attach before any flow was applied.
Flow rates below are nominal and were set at the pump; the device was not instrumented, so
the delivered rate was not measured.

Wash and lysis sequence
-----------------------

1. Rinse the chamber with wash_buffer to remove unattached cells.
2) Repeat the rinse. The second rinse uses the reduced volume given in the footnote to
   Table S2, not the volume used for the first rinse.
Step 3 - Replace the medium with fresh medium and return the device to the incubator.
3b. If the cells are to be imaged, do this before the medium is replaced, since the
    replacement disturbs the monolayer.

5. Apply lysis_buffer and hold without flow.

Hold the device still for the lysis time in Table S2. Do not restart flow during the hold;
restarting flow before lysis is complete carries intact cells into the outlet and the
recovered lysate is then a mixture.

4) Collect the lysate from the outlet port into a chilled vessel.

<!-- page 12 -->

Table S2. Nominal parameters for the wash and lysis sequence

Step  Reagent        Volume (uL)  Flow (uL/min)  Time (min)  Temp (C)
1     wash_buffer    120          20             2           37
2     wash_buffer    *            20             2           37
3     medium         200          10             1           37
5     lysis_buffer   40           0              8           25
6     lysate         40           5              2           4
7     wash_buffer    not stated   5              2           4

* The second rinse uses 60 uL, half the volume of the first rinse.

A second table was pasted into the manuscript without its separator row, so the first row
below is a header in appearance only.

| Port | Carries | Nominal diameter (mm) | Note |
| inlet_1 | wash_buffer and medium | 0.8 | shared inlet |
| inlet_2 | lysis_buffer | 0.8 | dedicated |
| outlet_1 | lysate | 0.8 | chilled line |

<!-- page 13 -->

Device operation

The temperatures in Table S2 are chamber set points. The ramp between 37 C and 25 C was
not characterised and is not reported here.

```
Phase            Duration (min)   Shear class
attachment       240              adherent_cells
rinse            2                prelysis_rinse
lysis            8                none
collection       2                none
```

Notes

a) Step 7 is a final rinse of the empty chamber and is optional; its volume was not
   recorded.
b) Where a time is given as a whole number of minutes, it was timed with a bench timer and
   should be read as approximate.

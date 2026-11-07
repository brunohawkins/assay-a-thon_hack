# Retention audit: capsules vs bare cells

Numbers are mine unless a DOI is given. *Assumed* = unverified.

**Headline.** The 60 µm weir holds capsules only. Adherent culture needs no retention. Three suspension-cell steps are unsolved.

| Chip / chamber | Must hold | Current | Verdict |
|---|---|---|---|
| ipsc m1_expand, m2_hemogenic | adherent cells | none | OK (adhesion) |
| ipsc m3_concentrate | suspended progenitors | weir | **FAIL**: cells pass |
| ipsc mat_1–4 | semi-adherent | weir | MARGINAL |
| ipsc m5_harvest | dissociated cells, washes | weir | **FAIL** |
| module1 n_cx, g_cx, a_cx | adherent | none / weir | OK |
| m1_culture lanes | adherent | none | OK |
| cell_prep cult_1 | adherent, then eluted | none | OK |
| m1b cell_wash | bare cells | none (TBC) | **UNSOLVED** |
| placeholder; sctip trap_1 | capsules 70/135 µm | weir | OK pending coupon |
| module3 main_trap | capsules | 25 µm pillars | FIX: weir |
| m2_trap_1, m3_wash | capsules | bought 25 µm mesh | OK |

## Bare-cell options

- **Tighter weir: no.** A 12 µm cell needs about 5–6 µm, a 294 µm crest in a 300 µm chamber: ±1 µm against the ±20 µm contract. Pressure is trivial (about 1.2 kPa at 6 µL/min). Clogging is fatal: one clump blocks the slot, with no bypass.
- **Adhesion: sufficient. The culture chips have no problem.** hESC colonies kept adhesion at 0.6 dyn/cm² (10.1039/b820380f), 60× our 0.01 ceiling. Well-coated cells need about 10³ dyn/cm² (10.1021/ac049837t); poorly coated ones detach at low shear. hiPSC perfusion near 0.01 dyn/cm² has precedent (10.1002/bit.25150). Caveat: the first hours after seeding.
- **Sedimentation plus the weir wall.** A 12 µm cell falls about 6 µm/s at 37 °C, 3 µm/s at 4 °C (Stokes; density 1.065 g/mL *assumed*, range per 10.1073/pnas.1104651108). A 300 µm fall takes 50 s. Flow must stay below v_s × footprint: m3_concentrate 1.3 µL/min, mat 3.4, m5 2.7 (1.4 cold). Settled cells then sit behind a 240 µm wall. Floor shear (0.008 dyn/cm²) is near my resuspension estimate of 0.002–0.007 (*assumed* Shields θ 0.03–0.1), so the wall matters. Dextran defeats it.
- **Bought mesh.** Only an edge-on strip fits one layer: hand-assembled, leak-prone. Face-on membranes need a second layer, which is excluded.
- **Off-chip.** Spin and strainer work today.

## Recommendations, by risk

1. **m3_concentrate**: design change. The step is currently void. Make it settle-then-exchange: stop flow 3 min, then at most 0.65 µL/min (now 0.73).
2. **m5_harvest**: move off-chip, to a chilled tube as cell_prep does. Use bought 30 µm mesh; a 30 µm weir gap has ±67% error.
3. **m1b cell_wash**: move off-chip. DLD is impossible at our floor, and dextran kills sedimentation.
4. **mat_1–4**: cut flow to 1.7 µL/min (now 86% of cap); add a settle hold.
5. **module3 main_trap**: replace the pillars with a weir.
6. **Culture chambers**: no action.

Validate with a 10–15 µm bead coupon before cells.

# Module 1 at target scale

Design note for `chip/module1_target_v1.json` and `protocols/module1_target_v1.json`, both
written by `protocols/build_module1_target.py` (`--report` prints every table below).
The demo `chip/module1_v1.json` is kept alongside and is untouched.

The biology audit's four findings are all **scale**, not architecture. The demo's chambers are
0.105 cm² only because the board is at the 30 × 20 mm demo envelope. This board is the
ANSI/SLAS 127.76 × 85.48 mm footprint (`contracts/blocks.json` `target_envelope`, which until
now no code read).

μ = **0.0008 Pa·s** throughout (`fluid.viscosity_37c_pa_s`, medium at 37 °C), not the 0.001
dye-demo value.

| Audit finding | Demo | Here |
|---|---|---|
| 1 Medium 40–80× short | 1.6 µL / 48 h = 7.6 µL/cm²/day | continuous, **480 µL/cm²/day** |
| 2 Anoxia: "all ports capped, 24 h" | ~3.5 h of O₂ in a sealed chamber | **no capped hold exists**; vented off-chip reservoir per lane (`stages[]`) |
| 3 Lane area 0.105 cm² → 0.5–1 colony | — | **13.2 / 13.2 / 9.2 cm²**, 6 fields each |
| 4 Progenitors vent past a 25 µm weir | weir retains 70 µm, not 12 µm | **no retention feature at all**; sedimentation, 88× margin |

## Lane areas, and why each is what it is

| Lane | Fields | Field | Lane | Why this area |
|---|---|---|---|---|
| neural | 6 | 15.75 × 14.0 mm = 2.21 cm² | **13.23 cm²** | 80 days, no passage, so seeded sparse; ≥1e5 cells for the destructive arms at any published plating density |
| glia | 6 | 2.21 cm² | **13.23 cm²** | two competing published constraints — see below |
| astrocyte | 6 | 11.0 × 14.0 mm = 1.54 cm² | **9.24 cm²** | plates ~2× denser, so the 1e5-cell omic floor and the secretome volume are met in ⅔ of the area; the freed board carries the bus corridor |
| | 18 | | **35.70 cm²** | 33% of the 109 cm² plate |

**Field length is 14.0 mm in every lane** (a `blocks.json` rule: keep chamber length constant,
replicate). Only the width differs — which is why the shear below is identical in all three.

**Glia lane arithmetic** (the only lane where two published numbers fight):

| Step | Number |
|---|---|
| published plating | 5–10 colonies/cm², 16–40 colonies per vessel |
| progenitor yield at that density | 225,000/cm² ⇒ 22,500–45,000 per colony |
| in-place maturation window | 9,000–90,000 cells/cm² |
| **oxygen cap at this perfusion** | **58,642 cells/cm² — binds first** |
| ⇒ plate at | 1.1–2.2 colonies/cm² for 50,000/cm² in place |
| ⇒ over 13.2 cm² | **15–29 colonies**, inside the published 16–40 |

Keeping 5–10 colonies/cm² would need 60 cm² of maturation area. The lever is density, not area.
At the demo's 0.105 cm² the same sum gives 0.2–1.0 colonies: finding 3.

## Perfusion, shear, velocity

τ = 6μQ/(wh²) = 6μqL/h² with q the flow per unit area. **It does not contain the lane width**,
so equal perfusion per cm² gives equal shear in every lane.

| Lane | µL/min | mL/day | CV/day | per field | τ dyn/cm² | × under 0.01 | U mm/s | × under wash-out |
|---|---|---|---|---|---|---|---|---|
| neural | 4.41 | 6.35 | 16 | 0.735 | 0.00041 | 24 | 0.0026 | 88 |
| glia | 4.41 | 6.35 | 16 | 0.735 | 0.00041 | 24 | 0.0026 | 88 |
| astrocyte | 3.08 | 4.44 | 16 | 0.513 | 0.00041 | 24 | 0.0026 | 88 |

Owner's sketch confirmed: a whole lane as one 30 mm chamber at 5 µL/min is **0.0015 dyn/cm²**
(7× under; 0.0019 at the 20 °C viscosity). A field could take **17.7 µL/min** at the ceiling =
106 µL/min per lane, **24× the design flow**. So continuous perfusion and the 0.01 ceiling are
compatible at this scale, and **oxygen, not shear, sets the flow**.

Operating pressure 2.5 Pa (0.025 mbar) across a restrictor ⇒ **syringe pumps, not pressure
control**, exactly as the demo review concluded.

## Retention: sedimentation, so finding 4 deletes a part

| Step | Number |
|---|---|
| v_s = Δρ·g·d²/18μ, Δρ = 50 kg/m³ (conservative), d = 12 µm | **4.90 µm/s** (7.4 at 1.075 g/mL) |
| a cell escapes only if U > L·v_s/h = 14 × 4.9e-3 / 0.3 | **0.229 mm/s** |
| design point U | **0.0026 mm/s → 88×** |
| settling h/v_s vs transit L/U | 61 s vs 90 min |
| margin even at the shear ceiling | 3.7× |

No mesh, no weir, no sub-floor gap, and nothing to back-flush off.

## Oxygen

| Step | Number |
|---|---|
| demand, 9 amol/cell/s (top of published band) | 0.78 pmol/cell/day |
| supply, 480 µL/cm²/day × 0.19 nmol/µL × 50% extraction | 46 nmol/cm²/day |
| ⇒ supported | **58,642 cells/cm²** (lanes planned at 50,000) |
| neural lane: 661,500 cells need 514 nmol/day | perfusion delivers 603 |
| **one sealed field** | 12.6 nmol = **211 min** |

The demo's 24 h capped hold is ~7× that reserve — finding 2. Here the longest no-flow interval
is the **2.2 min quench** (96× inside the reserve); attachment is perfused at 0.5 µL/min, which
is 780× below wash-out. Gas exchange is **off-chip** in a vented reservoir per lane
(`stages[].id = *_gasx`): the board is deliberately not gas-permeable. Half the reservoir is
replaced with fresh medium every 24 h — that is the 300–600 µL/cm²/day of *fresh* medium; the
recirculation rate is the separate, oxygen-driven number, and recirculating is what keeps the
lane's own secreted factors in the loop.

## Isolation, re-derived for these branch lengths

x = 2√(Dt), D = 4e-10 m²/s (small molecule, the fast case) — the same convention as the demo
note, whose numbers were derived for a 140 × 75 mm board and never re-derived after the 0.209
shrink.

| Path | Length | Crossing time |
|---|---|---|
| nearest field → bus | 16 mm | 1.8 days |
| **field → field across the bus** | **58 mm** | **25 days** |
| group-1 spoke (corridor route) | 101 mm | 74 days |
| reach in 24 h of stop-flow | 11.8 mm | — |

Total stop-flow in the schedule is 2.2 min per lane. **The lanes genuinely stay isolated**, and
for a stronger reason than the demo's: during all 80 days each lane is a closed recirculating
loop and the bus is capped, so there is no advective path at all — isolation does not depend on
balancing three flows.

## Multi-omic allocation: same lane, same medium, same history

| Fields | cm² | cells @50k/cm² | Arm |
|---|---|---|---|
| f1–f2 (group 1) | 4.41 | 220,500 | metabolome + proteome, **quenched in place**, one cold organic extraction, two fractions, n = 2 |
| f3–f6 (group 2) | 8.82 | 441,000 | single-cell transcriptome, dissociated downstream; matches the 400–600k @1,500 cells/µL interface |
| all six | 13.23 | — | secretome + exometabolome, repeated draws at `p_*_sup` and `p_*_ex` |

The two groups are **separate hydraulic paths** (own feed, own outlet), which is what lets one
be quenched while the other keeps growing: during a quench every other port is capped, so the
only through-path is group 1, flow runs *away* from f3–f6, and in 2.2 min a small molecule
diffuses 0.1 mm against the 21 mm to the nearest harvest field. Both groups sit on the
interaction bus and share the lane's reservoir, which is what keeps them one sample.
`chip/module6_v1.json` today seeds its own three lanes from an unnamed source after a 1-day
culture, so "multi-omic from one sample" is false *across* arms; this is the architectural fix.

## 17 ports, and why

| Port (× lane) | Job |
|---|---|
| `p_*_in1` | group-1 feed; at the endpoint the same line carries rinse then quench |
| `p_*_in2` | group-2 feed, same reservoir and pump, split off-chip with matched lines |
| `p_*_ex` | group-1 outlet: effluent (second supernatant tap) and the extract |
| `p_*_sup` | **supernatant sampling**, the capability the demo lost: repeated draws without opening the sterile loop |
| `p_*_ret` | recirculation return to the vented reservoir — what makes the lane a closed loop |
| `p_bus` | bus drain: vented only when the lanes are deliberately connected |
| `p_veh` | **vehicle control** inlet straight into the bus |

5 × 3 + 2 = 17. The 6-port limit is a demo-envelope constraint (`blocks.json` says so twice);
at 5 mm pitch this footprint holds 425 positions. Minimum pairwise pitch here is 5.5 mm.

## Schedule

| Day | Step |
|---|---|
| −0.5 / 0, 39.5 / 40, 44.5 / 45 | coat, then seed each lane on its own day (neural, glia, astrocyte) so all mature together |
| seed + 0–1 | attachment, **perfused** at 0.5 µL/min |
| to 80 | continuous perfusion, 48 h blocks; medium switch at lane day 12 / 52 |
| 81, 83, 85 | one interaction direction at a time, 6 h, donor fed at 2× so each recipient keeps its normal rate; **each direction has a vehicle arm** (`run_parameters.variant`) |
| around each | supernatant draw before and after, all three lanes at once, bus capped |
| 87 | rinse + quench f1–f2 in place; f3–f6 handed over still perfused |

Seeding flow is set by sedimentation, not habit: below 10 (group 1) / 21 (group 2) µL/min cells
settle in the spine before reaching the far field — the demo's seeding failure re-created at
target scale — so seeding runs at 1.5× that, 15 / 31 µL/min, still 2.3× under the shear ceiling.

## Geometry and the floors that still bind

| | |
|---|---|
| footprint / depth / layers | 127.76 × 85.48 mm, **one** 0.3 mm etch depth, **one** fluidic layer |
| lid support | `pillar_array` per field, 5–10 mm pitch, ~2% of the floor (new at this size: a 0.3 mm cavity 15.75 mm across would bow) |
| flow distribution | restrictor riser 200 µm × 30 mm = 2.07e11 Pa·s/m³ vs 7.3e9 for one spine span: **28×**, so worst-case spread across 4 fields is 21%, closed by trimming riser lengths |
| spines and bus | ladders of `t_junction` tees and a chain of `network_node`s, **not** wide bars: a channel is cut as a straight run between centres, so one bar makes splayed risers — the promoted crossing check caught exactly that on the first draft |
| blocks used | `pillar_array`, `t_junction`, `network_node`, `serpentine_channel`, `straight_channel` |
| checks | 0 errors from `check_chip_spec.py`, `validate.py` and `make_figures.py`; tightest clearance 0.60 mm against a 0.4 mm min wall |

## Honest limits

- **No cells have been run.** This is an architecture and a physics budget.
- 21% flow spread across a group before the riser lengths are trimmed.
- The restrictor's folds are not in the spec geometry (a channel carries no length field); the
  3 mm riser band is reserved and empty, but the planar checker cannot see the folds.
- 955 mL of medium over 87 days (318 per lane). `reservoir.default_ul` (1,000 µL) and
  `waste.capacity_ul` (2,000 µL) are demo-bench numbers; a target-scale run needs litre-scale
  reservoirs. Flagged, not edited — `contracts/parameters.json` is another owner's file.
- The vehicle arm's medium does not traverse the donor's spine, so ~100 mm of surface
  adsorption differs between arms. Unmeasured.
- Quench speed is adequate for amino acids, TCA intermediates and lipids, **not** for ATP or
  glycolytic intermediates (~1 s turnover).
- One destructive endpoint per board, so the pre-interaction state comes from the secretome
  time course and a second board.
- `cad/make_train_cad.py`'s `in_envelope()` only knows the demo envelope, so the figures
  describe this board as laser-cut rather than embossed. Cosmetic; `cad/` is another owner's.

## Handoff (deliberately not designed here)

The day-87 enzyme-free lift at 4 °C is **not** in this protocol: 80-day cortical neurons will
not release that way, and harvest chemistry belongs to the cell-prep stage. This board hands
over, per lane, fields f3–f6 of adherent cells in place (8.82 cm², ~441,000 cells), still
perfused, with an imaged density and a viability gate, reachable through `p_*_in2` and the bus
(`p_bus` vented). It needs back: a lift chemistry validated on cells of this age, and the
1,500 cells/µL encapsulation interface.

## Rebuilding

```bash
python protocols/build_module1_target.py --report      # chip + protocol + every table above
python tools/check_chip_spec.py
python tools/validate.py
python cad/make_figures.py --chip chip/module1_target_v1.json
```

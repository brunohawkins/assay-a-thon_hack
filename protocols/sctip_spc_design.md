# Capsule generation and single-copy barcoding: the design, and what it fixes

Notes for `sctip_spc_v1.json` and `chip/sctip_spc_v1.json`.

> **Not fabricable as drawn.** The 150 × 100 mm board is far outside the 30 × 20 mm envelope,
> and its 100 µm features are below the 200 µm floor. Stage A is retired in favour of
> `chip/spc_demo_v1.json`; stages B and C have no chip. Retention features cannot be printed,
> so capsule retention needs a bought mesh insert or sedimentation. See
> [pipeline_status.md](pipeline_status.md).

The brief: build our own capsule generator instead of buying one, and replace
plate-based split-and-pool barcoding with a single random barcode per capsule that is
amplified in place — the AmpliDrop idea
([10.1101/2024.08.13.607508](https://doi.org/10.1101/2024.08.13.607508)) applied to
semipermeable capsules, on a chip we can actually make.

It works, but not the way it was first drawn. Two things had to change, and both changes
are forced by numbers rather than taste. Everything below is published work, cited
inline, plus geometry we derived. See [CONFIDENTIALITY.md](../CONFIDENTIALITY.md).

![Plan view of the module](../cad/figures/sctip_spc_v1_layout.svg)

---

## 1. The anchor molecule does not work, and is not needed

The original idea was a barcode tethered to a large PEG so it could not escape the
capsule. The problem is that **it protects the wrong molecule.**

The shell retains DNA above about **300 bp** and passes proteins up to about 160 kDa
([10.1093/nar/gkac918](https://doi.org/10.1093/nar/gkac918),
[10.1126/science.ady7227](https://doi.org/10.1126/science.ady7227)). An anchor holds the
one seed molecule. But clonal amplification produces **~10⁶–10⁸ short copies**, none of
them anchored, all of them below the cutoff. Measured leakage for 100–300 bp fragments
is **5.1% per wash**, with amplicons found in the supernatant at every size from 100 to
1500 bp ([10.1101/2025.03.14.643253](https://doi.org/10.1101/2025.03.14.643253)). Every
capsule would seed every other capsule.

The anchor holds one molecule while a million escape.

**What we do instead:** amplify while the capsule is sealed inside an **oil droplet**,
and in the same reaction join the barcode onto the fragment by overlap extension
([10.1016/0378-1119(89)90359-4](https://doi.org/10.1016/0378-1119(89)90359-4)). By the
time the emulsion is broken the barcode is part of a molecule long enough for the shell
to hold. Loose copies wash away harmlessly.

**The emulsion is the containment.** The shell's job is retaining the finished product
for washing and pooling — not retaining the barcode during amplification. No anchor
chemistry, no cleavage step, no protease.

| | Anchor design | Amplify-in-oil |
|---|---|---|
| Holds the seed | yes | yes (oil) |
| Holds the **amplicons** | **no** | **yes (oil)** |
| Extra chemistry | PEG conjugate + cleavage | none |
| Thermal conflict | anchor must survive cycling | none |

---

## 2. The capsules are 135 µm because of the fabrication limit, not the biology

A 40 µm nozzle is **not achievable** with 3D printing and hot embossing. A desktop DLP
printer floors at about 200 µm for open channels and 500 µm for enclosed ones (`contracts/blocks.json`); hot
embossing replicates whatever its master has, and a 40 µm master needs photolithography
or micromilling, both of which are outside the constraint.

So the orifice is **100 µm**, which gives droplets of roughly 1.3× that — about 135 µm.

That turns out to be the right answer anyway, because of the next step. Close-packed
re-injection is what beats Poisson on the capsule side, and it needs an entrance of about
**0.85 × capsule diameter** ([10.1021/acs.analchem.5c05488](https://doi.org/10.1021/acs.analchem.5c05488)).

| Capsule | Entrance needed | Can we make it? |
|---|---|---|
| 70 µm | 60 µm | **no** |
| 120 µm | 102 µm | just |
| **135 µm** | **115 µm** | **yes** |

A 70 µm capsule cannot be re-injected close-packed on a chip we can build. At 135 µm it
works, and published single occupancy is **93–97%**, against about 30% by chance.

The cost is honest: a 135 µm capsule is **9.8× the volume** of a 70 µm one, so reagent
per cell goes up, and throughput is ~140k capsules/h against the ~835k/h of a bought
instrument. `--report` prints both.

---

## 3. Nothing above 72 °C ever touches an intact capsule

This was the biggest worry and it evaporates on reading the published method. In TIP-seq
([10.1083/jcb.202103078](https://doi.org/10.1083/jcb.202103078)):

| Step | Temp | Cells intact? |
|---|---|---|
| Antibody, transposase binding | 4 °C – RT | yes |
| **Tagmentation** | **room temperature** | yes |
| Lysis | 50 °C | no, lysed here |
| Gap-fill | 72 °C | DNA only |
| Linear amplification | 37 °C, overnight | DNA only |
| Library PCR | 98 °C | DNA only, after release |

**Maximum with capsules present: 72 °C for 3 min.** The amplification that matters is
*linear* and runs at 37 °C — chosen in the source method because one transposon insertion
is enough, errors do not propagate and coverage stays even. Capsules are published as
surviving 98 °C cycling anyway, so there is margin either way.

---

## 4. The live-cell conflict dissolves

AmpliDrop needs cells **fixed and permeabilised**, with the enzyme steps done inside the
cell before encapsulation. We have live cells. That looked like a hard conflict.

It isn't, because the chromatin method needs cells that are **permeabilised but not
fixed and not live** — antibody and transposase have to get in, so a live cell is no good
either way. Cells arrive live from culture, are permeabilised in a tube, and go on chip.
Those upstream steps are bulk and gain nothing from being on a chip.

---

## 5. One barcode per droplet is impossible, and that's fine

A free molecule cannot be ordered the way a close-packed bead can, so barcode loading is
Poisson no matter what.

| Mean per droplet | None | Exactly one | Two or more |
|---|---|---|---|
| 1.0 | **36.8%** | 36.8% | 26.4% |
| **3.0** | **5.0%** | 14.9% | 80.1% |

Aiming for exactly one throws away 37% of cells. So we do what the source method does:
run at a mean of **3**, accept that most droplets get several, and resolve them
afterwards by merging barcodes that share fragments. Only 5% of droplets get none.

Pool size is **16 random bases** (4.3 × 10⁹), so fewer than **0.002%** of 50,000 capsules
share a barcode.

---

## 6. The module, stage by stage

![Feature sizes against the process floors](../cad/figures/sctip_spc_v1_features.svg)

Every feature is at or above 100 µm (below the 200 µm floor in force now), so the whole
board is **one embossing depth, one process** in principle. Compare the three-lineage chip, which needs two.

| Stage | Does | Key dimension |
|---|---|---|
| **A** generation | shell + core meet at a flow-focusing cross, oil cuts droplets | 100 µm orifice |
| **B** barcoding | capsules re-injected close-packed, meet barcode + mix, cut back into oil | 115 µm entrance |
| **C** trap and wash | phase separation, packed-bed trap, flow-through washes, back-flush | 100 × 150 µm slots |

**Stage C is the one that pays for itself.** The bench workflow runs about twenty
centrifugation steps, and its own notes say most capsules are lost at the
emulsion-breaking step. Buffer exchange here is limited by diffusion into the capsule
(~2–5 min), not by flushing, so there is nothing to gain from a faster pump.

![Layer stack and process](../cad/figures/sctip_spc_v1_stack.svg)

### Two things deliberately left off the chip

- **Curing.** The two aqueous polymers need minutes to demix before the shell is in the
  right place. A serpentine holding minutes at this flow would be metres long, so
  capsules are collected and flood-cured in the tube.
- **Thermocycling.** On-chip cycling is no faster than a tube at this scale and invites
  evaporation and bubbles through a 16-hour run. Only the steps that genuinely need a
  chip are on it.

---

## 7. Honest limits

- **No cells have been run.** This is an architecture and a physics budget.
- **Nobody has published capsule re-injection with a single-copy barcode.** The two halves
  are each published; the combination is not.
- **Capsule retention at a 100 µm gap is unverified.** A liquid-core capsule deforms, and
  the gap that would reliably hold a 135 µm capsule is 70–80 µm, which we cannot make.
  Retention here leans on chamber geometry and downward flow. **This is the first thing
  to test on the bench** — a pressure ramp on real capsules through a 100 µm slot.
- **Fragments below ~300 bp are lost** even when barcoded, because the shell will not hold
  them. For a mononucleosomal target that is a real fraction.
- **About half the transcribed RNA may lack the barcode**, because the barcode oligo
  carries its own promoter.
- **Cell sedimentation in the core line** will drift the loading within minutes at
  1.5 µL/min. Test with density-matched beads before using cells.
- **Oil supply:** the standard fluorinated oils are affected by the 3M PFAS phase-out.
  Alternatives exist but need qualifying.
- `coc` is not yet in `contracts/materials.json`, so its compatibility is unchecked.

## Rebuilding

```bash
python protocols/build_sctip_spc.py --report
python cad/make_figures.py --chip chip/sctip_spc_v1.json
python operator/run.py --protocol protocols/sctip_spc_v1.json --chip chip/sctip_spc_v1.json
python tools/validate.py && python tools/check_chip_spec.py
```

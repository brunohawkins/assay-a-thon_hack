# Pipeline status

Checked 7 Nov 2026. Every chip: 30 × 20 mm, one layer. BUILT means a spec and protocol exist, not that anything was printed or run.

**This is a single-cell assay.** RT runs inside module3's trap and adds no barcode *itself*, but the per-cell barcode is added immediately afterwards, in stage 4b: each capsule is re-injected into its own oil droplet, and a single barcode molecule from a pool of 16 random bases (4.3 × 10⁹) is clonally amplified *in that droplet* and overlap-extended onto that capsule's cDNA — AmpliDrop ([10.1101/2024.08.13.607508](https://doi.org/10.1101/2024.08.13.607508)) applied to semipermeable capsules. The emulsion is the containment, so no anchor molecule and no cleavage step. One capsule holds one cell, so every cell gets its own clonal barcode. The lane label and the four sublibrary indices (about twelve groups) are a **demultiplexing** convenience on top, not the resolution of the data. Stage 4b has a design and a protocol; it does **not** have a printable chip — see [sctip_spc_design.md](sctip_spc_design.md) and the Blunt notes below.

## Chain

| # | Stage | Chip | Protocol | Status |
|---|---|---|---|---|
| 1 | Culture, lane barcode, harvest | `module1_v1` | `module1_v1.json` | BUILT |
| 2 | Cell prep | `cell_prep_v1` | `cell_prep_v1.json` | BUILT |
| 2b | Spin, 40 µm strainer, resuspend | tube, mesh | `cell_prep_design.md` | OFF-CHIP |
| 3 | Encapsulation | `spc_demo_v1` | `spc_demo_v1.json` | BUILT |
| 3b | Break emulsion, wash oil, cure | tube | step 6 covers cure only | OFF-CHIP, HOLE |
| 4 | Load, lysis, wash, RT, wash, release | `module3_v1_translated` | `module3_v1.json` | BUILT (bought mesh) |
| 4b | **Single-cell barcoding**: one clonal barcode per capsule, generated in its own oil droplet | `sctip_spc_v1` (demo layout only, stages B + C) | `sctip_spc_v1.json` | DESIGNED + PROTOCOL, **NOT PRINTABLE**: droplet handling on a bought kit |
| 5 | Sublibrary split, index PCR | `module4_v1` | `module4_v1.json` | BUILT |
| 6 | Release, bead cleanup | `module5_v1` | `module5_v1.json` | BUILT |
| 7 | Pool, SPRI, QC | tube, instruments | none | OFF-CHIP, HOLE |

## Retired and duplicates

| Item | Replaced by | Retirement recorded? |
|---|---|---|
| `m1_culture_v1` | `module1_v1` | Yes, `superseded` |
| `m1b_cellprep_v1` | `cell_prep_v1` | Spec says `module1_v1`: wrong |
| `m3_wash_v1` | `module3_v1_translated` | Yes |
| `m2_trap_1` | `module3_v1_translated` | Chamber inside m2, no record of its own |
| `m2_encapsulation_v1` | `spc_demo_v1` | **No.** Spec says `draft`; READMEs only |
| `sctip_spc_v1` stage A | `spc_demo_v1` | Prose only |
| `sctip_spc_v1` stages B, C | **NOT RETIRED.** They are stage 4b | n/a. Filing them as a duplicate of module 3's trap was the mistake that lost single-cell resolution: stage B is the barcoding re-injection and nothing else in the repo does it. Steps 17-19 of its protocol (gap-fill, transcription mix, overnight 37 °C linear amplification) are a chromatin/transposase **variant tail** and are skipped on the RNA path; steps 1-16 and 20-21 are the shared barcoding stage |
| `module1_v1` dissociation (175-178) | `cell_prep_v1` | **No.** DUPLICATE, still in protocol. Steps 179-182 are still needed |

The pipeline points at no retired chip. Still pointing at them: 5 `m3_wash_*` and 3 `m1b_cellprep_*` scenarios, 15 runs, and four retired protocol files (`m1_culture`, `m1b_cellprep`, `m2_encapsulation`, `m3_wash`), none marked retired. `sctip_spc_v1.json` is **not** one of them: it is stage 4b's live protocol.

## Broken joins

| Join | Problem |
|---|---|
| 1 → 2 | Module 1 already lifts and quenches cells. `cell_prep_v1` steps 1-3 expect an adherent layer and dissociate again |
| 2 → 3 | Hand spin, strainer, resuspension (documented). Cell count is for 135 µm capsules, not 260 µm |
| 3 → 4 | Emulsion break and oil wash undocumented. Capsules about 260 µm; module3 built for 70 µm, 100 µm pre-filter gap. About 5,000 capsules made, trap sized for 650,000. `spc_demo_v1` is 0.15 mm deep, too shallow for a 260 µm sphere |
| 4 → 4b | Module3 releases 76.9 µL; stage 4b re-injects capsules **close-packed**, so the suspension has to be concentrated to a packed bed first. Undocumented. The join also crosses onto bought hardware, so it needs an adapter |
| 4b → 5 | Stage 4b back-flushes the bed out through its own waste line (step 21, routed by an off-chip selector valve); module4 draws 9.6 µL at near-packed density. Concentration step undocumented. Valve and tubing off-chip, and the kit-side fitting is unnamed |
| 5 → 6 | **Module4 has no capsule outlet**: `in_s1`-`in_s4` are inlets, `p_vent` blocks capsules. Port stays TBC. Reverse flow through the inlets and tubing undocumented |
| 6 → 7 | Pooling, ethanol size selection and QC by hand, no protocol |

**Holes:** no protocol for 3b and 7; no outlet port on `module4_v1`; no printable chip for 4b (by design — the droplet steps are bought, see below).

## Blunt notes

- **The data is single-cell, and the earlier "lane-level" note was wrong.** Dropping plate-based split-and-pool barcoding did not drop per-cell barcoding. What actually happened is narrower and worse: **the pipeline file lost its barcoding stage and then rationalised the loss as a design choice.** `pipeline_mvp.json` had no barcoding module and `sctip_spc_v1` was filed as a retired duplicate of module 3's trap. It is not a duplicate; it is the missing stage. It is now stage 4b with a position, an input and an output.
- **The droplet hardware is bought; the barcoding chemistry is ours.** Stage B needs a close-packed entrance of about 0.85 × the capsule diameter and stage C needs 100 × 150 µm slots. Our in-plane floor is 200 µm. So, exactly as with encapsulation, re-injection, droplet making and emulsion breaking **cannot be resin-printed or hot-embossed and belong on the commercial droplet kit**. `chip/sctip_spc_v1.json` is a demo-scale layout, not a printable part: its `pack_1` is clamped to 200 µm, wider than a capsule, so it cannot order capsules single file. This is a coherent position, not a failure — the single-cell mechanism is the chemistry, and we own it.
- **Barcoding loading is Poisson, with a cost.** At a mean of 3 barcode molecules per droplet, about **5% of droplets get no barcode and those cells are lost**, and most droplets get **several**, which must be merged computationally afterwards by spotting barcodes that share fragments. Aiming for exactly one would throw away 37%, which is worse.
- **Stage 4b's unverified parts** (from [sctip_spc_design.md](sctip_spc_design.md) §7, and they stand): nobody has published capsule re-injection with a single-copy barcode — both halves are published, the combination is not; fragments below ~300 bp are lost because the shell will not hold them; about half the transcribed RNA may lack the barcode; capsule retention at the gap is unverified; cell sedimentation in the core line drifts loading within minutes.
- **Stage 4b's capsule retention *can* be fabricated — in z.** The 200 µm floor is **in-plane** and z is free, so its trap uses a **depth weir**: a 25 µm gap under the lid, crest = that chip's etch depth minus the gap. Not a bought mesh insert and not sedimentation. The gap itself is untested; z tolerance on 25 µm is the biggest unknown, so run a 21 / 24 / 27 µm coupon first. `ipsc_to_mgl_chip_v1` is a separate experiment, not in the chain.
- **`tests/test_pipeline.py`** passes (17/17): it builds the missing-module violation it needs rather than hunting for a live one. The two new `connector unknown` warnings from `tools/validate.py` on the 4 → 4b and 4b → 5 joins are expected and honest — they are the adapters between our chips and the bought droplet kit.

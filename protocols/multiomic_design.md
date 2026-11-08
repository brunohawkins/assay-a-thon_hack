# Multi-omic design

Checked 7 Nov 2026. Numbers carry a DOI unless they are ours. Capsule-specific steps are *inference*: nobody has published them.

**Headline.** One extraction gives metabolome *and* proteome. Extraction happens on adherent cells before encapsulation. The secretome answers the interaction question. The transcriptome stays on capsules.

**Read this before "multi-omic from one sample".** Today Module 6 kills the cells it extracts, so the metabolome/proteome arm and the single-cell transcriptome arm run on **parallel samples within one experiment** — same lanes, same medium, same timepoint, different cells. That becomes "one sample population" only when the target-scale culture chip (`chip/module1_target_v1.json`, in progress) lands with **replicated fields per lane**, so some fields are quenched in place while other fields in the *same* lane are dissociated for the transcriptome. Both claims are written out below; do not use the stronger one until that chip exists.

## 1. The wrong premise

We first concluded that mild lysis (protein) and cold-solvent quench (metabolites) were mutually exclusive. **False.** It assumed proteomics needs native protein. Bottom-up LC-MS/MS digests everything, so a denaturing quench costs nothing that matters.

One cold acidified organic extraction gives metabolome in the supernatant and proteome in the pellet.

| Source | What it actually supports here |
|---|---|
| SIMPLEX, Coman 2016, 10.1074/mcp.M115.053702 | 3,327 proteins, 75 metabolites, 360 lipids from one extract of about 1e6 cells. Cited for the **principle** and the cold-soak time, not for depth on our chemistry (SIMPLEX is an ether-type biphasic partition; ours is single-phase) |
| MPLEx, Nakayasu 2016, 10.1128/mSystems.00043-16 | **NOT TRANSFERABLE.** MPLEx is a chlorinated, Folch-type biphasic extraction, and this chip excludes chlorinated solvents. Its proteome-depth and CV equivalence (≈1.8% enriched / 1.4% depleted >2-fold) belong to that chemistry; ours is a single-phase acidified polar-organic precipitation with no partition solvent, so **no depth or CV number is claimed from it** |
| Sapcariu 2014, 10.1016/j.mex.2014.07.002 | Cold polar-organic supernatant for metabolites, pellet digested for protein. This is the principle we use |
| Roume 2012, 10.1038/ismej.2012.72 | ⚠️ **Extrapolated.** This is a biomolecular isolation framework for **mixed microbial communities / biofilms**, not adherent mammalian cells. The sequential-fraction *idea* transfers; its RNA-integrity figure (9.68 ± 0.05) and its yields do not. Treat as inference |
| Doucette 2014, 10.1021/pr500864a | Intact membrane proteins resolubilised **with cold acid** after precipitation. ⚠️ Our step resolubilises at **ambient** in a non-carbamylating denaturant class, so the ">90% membrane protein recovery" figure does **not** transfer and is **not claimed**. The paper still supports the principle: precipitated membrane proteins can be brought back for MS |

**Actually lost:** native complexes, enzyme activity, interaction assays, and peptides under about 10 kDa. These do not precipitate and land in the metabolite fraction.

## 2. Why bulk omics left the capsules

GelMA is gelatin, so it is protein. The capsule is **core–shell**, not solid: a ~3 µm shell on a 70 µm capsule is about **24% of the volume**, so the gelatin burden is about **2.1 ng**, not 9 ng. Against 0.2–0.3 ng of cell protein that is **7–10×** more capsule than cell, not 30–45×. **The conclusion survives:** proteomics on capsules still measures mostly capsule.

So extraction runs on **adherent cells before encapsulation**. This is also better practice:

- Direct solvent on adherent cells beat scrape-in-buffer by 7× (Dettmer 2010, 10.1007/s00216-010-4425-x).
- **Rinse, but rinse as briefly as you can.** A quick rinse roughly doubles detected features, which is why you rinse at all (Lorenz 2011, 10.1021/ac103313x). But **washing itself causes metabolite leakage** — Kapoore 2017 (10.1039/C7AN00207F) found a single short wash step gives minimal leakage, so it argues for the **shortest defensible rinse**, not a careful slow one. This repo previously cited it backwards, in favour of a long wash.

The transcriptome arm is unaffected.

## 3. Architecture

| Omic | Module | Sample | Chemistry | Destructive? |
|---|---|---|---|---|
| Secretome + exometabolome | Per lane, culture | Repeated supernatant draws, many timepoints | Collect, store cold | No |
| Transcriptome | Module 3 (RT in its capsule trap), then stage 4b (barcoding), then Modules 4–5 | Capsules, **single-cell** | Existing capsule workflow. Single-cell because each capsule gets its own clonal barcode, amplified inside its own oil droplet in stage 4b ([sctip_spc_design.md](sctip_spc_design.md)); the lane label and the four sublibrary indices only demultiplex | Yes (endpoint) |
| Metabolome + proteome | Module 6 (`chip/module6_v1.json`, `protocols/module6_v1.json`) | Adherent cells, one endpoint | Single-phase acidified polar-organic quench, free of chlorinated solvents and of partition solvents; two fractions from one extraction. **Needs a sub-ambient stage** | Yes |

**The secretome arm answers the interaction question.** What crosses between lanes is extracellular by definition. The transcriptome is the response.

**Module 6 is a branch, not a step in the chain**, and it now carries a hardware cost the rest of the pipeline does not: a **−20 °C extraction soak** plus a 4 °C rinse and flood, all on **one thermal stage under all three lanes**, so the protocol is ordered by phase and not by lane. That is a new rig requirement — see [thermal_map.md](thermal_map.md) and the Module 6 row in [pipeline_status.md](pipeline_status.md).

## 4. Lane-of-origin labelling

Our cells are wild-type, so genetically encoded sensors are out. Instead, pulse-label one lane metabolically before the lanes are connected.

- Proteins and glycans: azide-bearing amino acid or sugar analogues.
- Metabolites: stable-isotope tracers.
- Secreted material is then traced to its source lane.

Precedent:

- **Bioorthogonal labelling of secreted proteins:** 10.1002/glia.70079 (Liu 2025, *Glia*) — unnatural-amino-acid labelling plus a click reaction enriches an iPSC-astrocyte secretome **in the presence of serum**, which is the technique this arm needs.
- **Stable-isotope tracing between neural cell types:** 10.1038/srep33285 (Simão 2016) — labelled tracer followed through the glutamine–glutamate–GABA shuttle in a human neuron–astrocyte co-culture.
- **Dropped:** 10.15252/embj.2020105693 (Tüshaus 2020). It is a secretome-proteomics *enrichment and resource* method for mouse brain; it does not support tracing secreted material back to a source lane, which is the claim here.

## 5. Honest limits

| Limit | Consequence |
|---|---|
| Yield | Proteomics is comfortable: 1,000 cells give 500–1,000 proteins (Mueller 2020, 10.15252/msb.20199111), roughly 10× headroom on ~9,600 cells per lane. **Untargeted metabolomics is now in reach too**, and the old "1e5–1e6 cells, targeted central-carbon only" line was stale: that figure describes conventional wide-column workflows. On nanoflow HILIC or CE-MS, 100 cells gave 1,620 peak pairs (Luo 2017, 10.1021/acs.analchem.7b03100), ~1e3 cells gives 40–100 identified metabolites, **~1e4 gives 100–160**, 1e5 gives 200–300; adenylates are seen from 500 cells by CE-MS (Zhang 2019, 10.1016/j.jchromb.2018.12.004). So ~1e4 cells is usable **untargeted** coverage of roughly 100–160 metabolites, *provided the extract stays at or below about 10 µL*. Synced to `chip/module6_v1.json` |
| Quench speed | Adequate for amino acids, TCA intermediates and lipids. **Not** for ATP/ADP or glycolytic intermediates, which turn over in about 1 s |
| Residual medium | About **593:1** by volume on a **lane** basis (11.258 µL wetted per lane against ~0.019 µL of intracellular volume, 2 pL/cell assumed) — not 150:1. That is why the rinse is not optional: it is what makes medium-borne metabolites (amino acids, glucose) measurable. Metabolites absent from the medium (nucleotides, phosphorylated glycolytic intermediates) are unaffected |
| Rinse cost | The rinse now runs at the **pre-lysis shear ceiling**, not the culture ceiling: about **23 s**, down from about **700 s**. "It costs a few seconds of perturbed metabolism" was out by roughly **200×** against the old rinse; at ~23 s it is the right order — tens of seconds, not minutes |
| Solvents and polymer | Chlorinated solvents are excluded, and the reason has moved on: **this chip is a cyclic-olefin copolymer, not PMMA.** The usual chlorinated-free substitute is an ether-type biphasic partition, and `contracts/materials.json` says COC is attacked by non-polar hydrocarbons, which is that family — so swapping solvents moves the material risk rather than removing it. Our way out is a **single-phase** acidified polar-organic precipitation with no partition solvent. COC compatibility with the actual quench and solubilisation reagents is still a TODO in `contracts/materials.json` |
| Acid-labile metabolites | Acid plus organic is only briefly safe, so fraction 1 is neutralised the moment it is collected, into a pre-charged vial. (The old row said "RNA" — **Module 6 collects no RNA**; the transcriptome arm is on capsules) |
| Endpoints | One per chip for the destructive arm. Repeated sampling only for the secretome. Today that means **parallel samples within one experiment**, not one sample population; replicated fields per lane in `chip/module1_target_v1.json` are what would change that |
| Evidence | Nobody has published this on GelMA capsules. Capsule-specific steps are inference |

## 6. Dropped

| Dropped | Why |
|---|---|
| Repeated in-chip lysis | Destructive. One endpoint is all the chemistry allows |
| Capsule-based omic separation | The capsule is protein and still dominates the signal at 7–10× the cell's own protein |
| Genetically encoded biosensors | Cells must be wild-type |
| SERS | Not a primary readout for this design |
| Raman as primary readout | Same. Not specific enough for our questions |

## What to build first

1. **Secretome sampling per lane.** Cheapest, non-destructive, and answers the interaction question.
2. **Lane-of-origin pulse label.** It makes the secretome data interpretable.
3. **Module 6 cold-solvent quench**, single-phase and free of chlorinated solvents, with the shortest defensible rinse. **Test on a cyclic-olefin-copolymer coupon** — that is what the chip is made of, not PMMA.
4. **Neutralisation step** for fraction 1, so acid-labile metabolites survive. (Not for RNA; there is none in this arm.)
5. **Sub-ambient stage** for the −20 °C extraction soak. This is the new rig item; without it the soak runs at 4 °C and the yield and the metabolome/proteome split are unvalidated.
6. **Untargeted panel** sized to chip yield (~1e4 cells, extract ≤ ~10 µL), with a targeted central-carbon panel as the fallback.
7. **Capsule-specific checks.** Treat as inference until run.

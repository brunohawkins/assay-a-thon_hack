# Multi-omic design

Checked 7 Nov 2026. Numbers carry a DOI unless they are ours. Capsule-specific steps are *inference*: nobody has published them.

**Headline.** One sample can give metabolome and proteome. Extraction happens on adherent cells before encapsulation. The secretome answers the interaction question. The transcriptome stays on capsules.

## 1. The wrong premise

We first concluded that mild lysis (protein) and cold-solvent quench (metabolites) were mutually exclusive. **False.** It assumed proteomics needs native protein. Bottom-up LC-MS/MS digests everything, so a denaturing quench costs nothing that matters.

One cold acidified organic extraction gives metabolome in the supernatant and proteome in the pellet.

| Source | Result |
|---|---|
| SIMPLEX, 10.1074/mcp.M115.053702 | 3,327 proteins, 75 metabolites, 360 lipids from one extract of about 1e6 cells |
| MPLEx, 10.1128/mSystems.00043-16 | Proteome depth and CVs match unextracted control. Only about 1.8% of proteins enriched, 1.4% depleted more than 2-fold |
| Sapcariu, 10.1016/j.mex.2014.07.002 | Cold methanol supernatant for metabolites, pellet digested for protein |
| Roume, 10.1038/ismej.2012.72 | Metabolites first, then DNA, RNA and protein from the same pellet. RNA integrity 9.68 ± 0.05 |
| 10.1021/pr500864a | Membrane proteins recovered above 90% after organic precipitation |

**Actually lost:** native complexes, enzyme activity, interaction assays, and peptides under about 10 kDa. These do not precipitate and land in the metabolite fraction.

## 2. Why bulk omics left the capsules

GelMA is gelatin, so it is protein. A 70 µm capsule holds about 9 ng of gelatin. A cell holds about 0.2–0.3 ng of protein. That is roughly 30–45× more capsule than cell. Proteomics on capsules measures the capsule.

So extraction runs on **adherent cells before encapsulation**. This is also better practice:

- Direct solvent on adherent cells beat scrape-in-buffer by 7× (10.1007/s00216-010-4425-x).
- A brief rinse, then cold methanol, minimises leakage (10.1039/C7AN00207F).

The transcriptome arm is unaffected.

## 3. Architecture

| Omic | Module | Sample | Chemistry | Destructive? |
|---|---|---|---|---|
| Secretome + exometabolome | Per lane, culture | Repeated supernatant draws, many timepoints | Collect, store cold | No |
| Transcriptome | Module 3 (RT in its capsule trap), then stage 4b (barcoding), then Modules 4–5 | Capsules, **single-cell** | Existing capsule workflow. Single-cell because each capsule gets its own clonal barcode, amplified inside its own oil droplet in stage 4b ([sctip_spc_design.md](sctip_spc_design.md)); the lane label and the four sublibrary indices only demultiplex | Yes (endpoint) |
| Metabolome + proteome | Module 6 | Adherent cells, one endpoint | Cold organic solvent quench free of chlorinated solvents, two fractions from one extraction | Yes |

**The secretome arm answers the interaction question.** What crosses between lanes is extracellular by definition. The transcriptome is the response.

## 4. Lane-of-origin labelling

Our cells are wild-type, so genetically encoded sensors are out. Instead, pulse-label one lane metabolically before the lanes are connected.

- Proteins and glycans: azide-bearing amino acid or sugar analogues.
- Metabolites: stable-isotope tracers.
- Secreted material is then traced to its source lane.

Precedent: 10.15252/embj.2020105693, 10.1002/glia.70079, 10.1038/srep33285.

## 5. Honest limits

| Limit | Consequence |
|---|---|
| Yield | Proteomics is comfortable: 1,000 cells give 500–1,000 proteins (10.15252/msb.20199111). Untargeted metabolomics wants 1e5–1e6 cells. Chip scale supports **targeted central-carbon metabolism only** |
| Quench speed | Adequate for amino acids, TCA intermediates and lipids. **Not** for ATP/ADP or glycolytic intermediates, which turn over in about 1 s |
| Residual medium | About 150:1 by volume against intracellular volume. The rinse is mandatory. It costs a few seconds of perturbed metabolism |
| Chlorinated solvents | Excluded: they attack PMMA. The quench must avoid them |
| RNA | Acid plus organic is only briefly safe. Neutralise the eluate immediately |
| Endpoints | One per chip for the destructive arm. Repeated sampling only for the secretome |
| Evidence | Nobody has published this on GelMA capsules. Capsule-specific steps are inference |

## 6. Dropped

| Dropped | Why |
|---|---|
| Repeated in-chip lysis | Destructive. One endpoint is all the chemistry allows |
| Capsule-based omic separation | The capsule is protein and swamps the signal |
| Genetically encoded biosensors | Cells must be wild-type |
| SERS | Not a primary readout for this design |
| Raman as primary readout | Same. Not specific enough for our questions |

## What to build first

1. **Secretome sampling per lane.** Cheapest, non-destructive, and answers the interaction question.
2. **Lane-of-origin pulse label.** It makes the secretome data interpretable.
3. **Module 6 cold-solvent quench**, free of chlorinated solvents, with the mandatory rinse. Test on a PMMA coupon first.
4. **Neutralisation step** for the eluate, so RNA survives.
5. **Targeted central-carbon panel** sized to chip yield.
6. **Capsule-specific checks.** Treat as inference until run.

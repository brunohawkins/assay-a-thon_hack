# Scientific brief: a multiplexed cellular-perturbation platform for human brain cells

## 1. Thesis

This platform imposes a defined cellular context on iPSC-derived brain cells and then reads regulatory state, transcriptome, proteome and metabolome from the same cultures, so it can follow Alzheimer's disease (AD) risk from microglial regulatory DNA to protein and metabolism, which no published method does.

It is modelled on Perturb-seq (Dixit 2016), where the perturbation is a CRISPR edit. Here the perturbation is one cell type acting on another. That is an analogy to Perturb-seq, not a claim to be Perturb-seq. Perturb-seq runs edit first, then reads the cell. This platform runs phenotype first: set the context, then read the molecular state. That is the harder direction, and we found no pipeline for it.

## 2. The problem

Much AD genetic risk sits in microglial regulatory DNA. Sporadic AD variants are "largely confined to microglia enhancers" (Nott 2019). AD heritability is enriched in PU.1 binding sites (Huang 2017) and in microglial accessible chromatin (Corces 2020). A GWAS of 111,326 clinical and proxy cases and 677,663 controls found 75 risk loci, 42 new, and implicated microglia alongside amyloid and tau pathways (Bellenguez 2022).

Microglia cannot hold that state alone. Isolated human microglia rapidly switch off microglia-specific genes, many linked to disease (Gosselin 2017). They need astrocyte-derived factors to survive, and their profile "requires continuous instructive signaling from the intact CNS" (Bohlen 2017). Transcript levels alone do not predict protein levels in many scenarios (Liu 2016). Microglial metabolism is tied to AD genetics (Ulland 2017).

The burden is large. All-cause dementia affected 57.4 million people in 2019 and is projected to reach 152.8 million in 2050. About 6.9 million Americans aged 65+ have Alzheimer's dementia, possibly 13.8 million by 2060. Estimated 2024 payments for health care, long-term care and hospice for people 65+ with dementia are $360 billion. Across 413 AD trials in 2002–2012 the success rate was 0.4%, before the anti-amyloid approvals. The 2024 pipeline held 164 trials of 127 drugs, down from 187 and 141 in 2023.

## 3. The knowledge gap

**No published method measures transcription-factor binding or chromatin state together with the proteome and metabolome from one defined human cell population, and none controls which cell types interact.**

- Three layers is the most any single measurement reaches (chromatin, RNA, antibody protein): TEA-seq, DOGMA-seq, spatial-Mux-seq. None includes metabolites or a mass-spectrometry (MS) proteome.
- Metabolome methods pair with one other layer only, RNA or antibody protein. The MS proteome has been paired only with RNA (nanoSPLITS).
- COSIE (preprint), the only four-layer result, joins separate tissue sections by computation. At bulk, four layers come only from split aliquots run as separate assays.

This is the measurement half of the argument. The perturbation half is that cell-type interaction is imposed and labelled, not found.

## 4. Novelty

1. Regulatory layer plus metabolome from the same population. No published method combines them from the same cells or tissue section.
2. Four layers measured, not computed, from the same lanes. COSIE reaches four only by integrating separate sections.
3. Regulatory layer plus an MS proteome. Three-layer methods use antibody panels; the MS proteome has been paired only with RNA.
4. Cell-cell interaction as the controlled perturbation, multiplexed across conditions, with a no-cell vehicle control. Every method compared measures cells or tissue as found.

## 5. Why it is necessary, and why now

Necessary. Microglia lose much of their risk-carrying gene program outside the brain, and a brain environment largely restores it (Gosselin 2017; Bohlen 2017), as transplantation into mouse brain shows (Hasselmann, Mancuso, Svoboda 2019). One-third of putative AD risk genes lack adequate mouse orthologs (Mancuso 2019), so the system has to be human. Influence runs both ways: in organoids, microglia changed neural stem cell transcription and accelerated network activity (Popova 2021). Transcript and protein are non-redundant readouts (Buccitelli 2020). Amyloid-β shifts microglia from oxidative phosphorylation to glycolysis, then to a tolerant state with weaker cytokine and phagocytic responses (Baik 2019).

Timely. The parts exist. Fine-mapping in primary microglia from 150 donors narrowed 18 of 21 AD loci to a single gene and named SPI1 a key putative regulator (Kosoy 2022). TIP-seq gives single-cell binding profiles with "∼10-fold higher unique reads per single cell compared with other methods" (Bartlett 2021). Semipermeable core-shell capsules carry single cells through multi-step enzymatic reactions (Leonaviciene & Mazutis 2023). AmpliDrop clonally amplifies one barcode inside each droplet (Garcia-Bassets 2024, preprint). Single-cell RNA has been paired with MS proteome (Fulcher 2024) and with metabolome (Mao 2024; Kang 2025, plant cells).

## 6. Scale and architecture

A few thousand cells acting on a few thousand cells are enough. The value is multiplexing conditions, so scale is set by the number of conditions, not the number of cells.

- A commercial droplet single-cell well takes up to about 20,000 cells, and a chip has eight wells (10x Genomics user guide CG000731 Rev B).
- At about 2,000 cells per condition, one well holds about 10 multiplexed conditions, and one commercial chip about 80. One chip's library out equals one well.
- The commercial well is a yardstick for library size, not part of the pipeline.
- Working in microlitres uses less reagent and gives a more concentrated sample.
- Scale by adding condition chambers, never by enlarging them.
- The area needed to differentiate cells (colony scale, done once) is not the area needed to run a condition (a few mm²). Differentiate once, then distribute into small chambers.

The 20,000 cells and eight wells come from the vendor guide, which has no DOI. The 2,000 cells per condition and the totals of 10 and 80 are design arithmetic.

Three cell types are the lab's own use case and a proof of concept, not a limit. The path is 10 compartments and beyond, with richer fluidics so compartments talk to one another. Each added compartment multiplies the combinations.

## 7. What it enables

For this assay, test whether a candidate microglial risk mechanism changes transcript, protein and metabolite output, and whether neurons or astrocytes alter the effect. Beyond it, the same platform can:

- test new differentiation protocols;
- vary factor dose and timing;
- test which cell type perturbs which, alone and in combination;
- compare genetic backgrounds, including iPSC lines of differing background, in one setting;
- screen interventions on matched regulatory, cell-state, protein and metabolite readouts.

## 8. Comparison with the closest approaches

| Approach | R | T | P | M | Same population | Interaction controlled |
|---|---|---|---|---|---|---|
| Perturb-seq (analogy) | No | Yes | No | No | Same cells | No (CRISPR edit) |
| TEA-seq | ATAC | Yes | Antibody | No | Same cells | No |
| DOGMA-seq | ATAC | Yes | Antibody | No | Same cells | No |
| spatial-Mux-seq | Histone + ATAC | Yes | Antibody | No | Same section | No |
| Paired-Tag / scMTR-seq | Histone marks | Yes | No | No | Same cells | No |
| nanoSPLITS | No | Yes | MS | No | Same cells | No |
| scMeT-seq / Kang et al. | No | Yes | No | Yes | Same cells | No |
| Vicari et al. | No | Yes | No | Yes | Same section | No |
| scSpaMet | No | No | Antibody | Yes | Same tissue | No |
| COSIE (preprint) | Yes | Yes | Yes | Yes | No (computed) | No |
| **This assay** | TIP-seq, single cell | Single cell | MS proteome, bulk | Bulk | Same lanes | Yes, with vehicle control |

R = regulatory, T = transcriptome, P = protein, M = metabolome.

## 9. Limits

- Proteome (bottom-up LC-MS/MS) and metabolome are lane-level, not single-cell. Both come from one cold acidified organic extraction of parallel replicated fields of the same lanes. The input an MS readout needs from a chamber of about 2,000 cells is untested.
- The four layers share one population and exposure history, not one single cell.
- The demo is dye-only: chips are printed, no biology has run, and the full-scale design is simulated, not built.
- The demo is single-layer and valveless. Linking cultures on demand with pneumatic valves needs a second layer plus a membrane. Contact-only interaction through microchannels that pass axons and glial processes but not cell bodies needs photolithography. Both are full-spec options on a different fabrication route, not part of the demo.
- A multiplexed run must record each cell's condition. Transcriptomic clustering separates the three cell types, as is standard for heterogeneous populations, but it does not say which condition chamber a cell came from, so it cannot replace a label. The run needs a per-condition label applied before harvest (for example a lipid-anchored oligo, MULTI-seq, McGinnis 2019), or a barcode assigned at encapsulation per chamber. Whether such a label reaches our library is unverified. Perturb-seq has no such problem, because its guide barcode travels with the cell.
- The 20,000-cell well is a figure for one kit generation (GEM-X 3' v4). Other kits differ.
- TIP-seq inside capsules with in-droplet barcoding is unpublished. Its parts are published, each with a gap:
  - TIP-seq (Bartlett 2021) covers histone marks, RNAPII and the TF CTCF. It needs an antibody per target and has not been shown for microglial TFs.
  - Semipermeable core-shell capsules (Leonaviciene & Mazutis 2023) were shown for RNA cytometry of single cells.
  - AmpliDrop (preprint) was shown for transcriptome and chromatin accessibility in emulsion droplets, not in capsules and not with TIP-seq.
- TIP-seq's ~10-fold gain is a cross-study comparison with published single-cell CUT&Tag data (droplet scCUT&Tag, Paired-Tag), not a head-to-head test. The Paired-Tag data came from brain tissue, TIP-seq from cultured cells.
- Restoration of microglial state is shown in mouse brain, not on a chip.
- Open items: 10x Multiome is unverified (no primary paper); the methods search is not exhaustive; COSIE and AmpliDrop are preprints; most claims were checked against abstracts only.

## 10. Evidence

| Claim | DOI |
|---|---|
| Perturb-seq (analogy) | 10.1016/j.cell.2016.11.038 |
| Per-condition lipid label | 10.1038/s41592-019-0433-8 |
| Well capacity (about 20,000 cells) and eight wells per chip | No DOI. 10x Genomics, Chromium GEM-X Single Cell 3' Reagent Kits v4 User Guide, CG000731 Rev B |
| AD risk in microglial regulatory DNA; SPI1 | 10.1126/science.aay0793; 10.1038/nn.4587; 10.1038/s41588-020-00721-x; 10.1038/s41588-022-01149-1 |
| 75 loci, 42 new; microglia implicated | 10.1038/s41588-022-01024-z |
| Microglial state needs brain and astrocyte signals | 10.1126/science.aal3222; 10.1016/j.neuron.2017.04.043 |
| Transplant restoration; one-third lack mouse orthologs | 10.1016/j.neuron.2019.07.002; 10.1038/s41593-019-0525-x; 10.1073/pnas.1913541116 |
| Microglia alter organoid neural cells | 10.1016/j.stem.2021.08.015 |
| Transcript and protein non-redundant | 10.1016/j.cell.2016.03.014; 10.1038/s41576-020-0258-4 |
| Microglial metabolism, TREM2, amyloid-β | 10.1016/j.cell.2017.07.023; 10.1016/j.cmet.2019.06.005 |
| TIP-seq, ∼10-fold | 10.1083/jcb.202103078 |
| Semipermeable capsules; in-droplet barcoding (preprint) | 10.1093/nar/gkac918; 10.1101/2024.08.13.607508 |
| Three-layer maximum | 10.7554/eLife.63632; 10.1038/s41587-021-00927-2; 10.1038/s41592-024-02576-0 |
| MS proteome or metabolome with RNA | 10.1038/s41467-024-54099-z; 10.1002/advs.202411276; 10.1073/pnas.2512828122 |
| Other table rows: Paired-Tag, scMTR-seq, Vicari, scSpaMet | 10.1038/s41592-021-01060-3; 10.1126/sciadv.adu3308; 10.1038/s41587-023-01937-y; 10.1038/s41467-023-43917-5 |
| COSIE (preprint; bioRxiv) | 10.64898/2026.03.03.709303 |
| Scale figures | 10.1016/S2468-2667(21)00249-8; 10.1002/alz.13809; 10.1186/alzrt269; 10.1002/trc2.12465 |

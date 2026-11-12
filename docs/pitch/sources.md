# Sources: merged evidence tables

Built only from `research_methods.md` and `research_context.md`. Every DOI below was resolved against Crossref on 2026-11-08 (all 44 resolve; first author, year and journal match the research files). Two capsule-barcoding DOIs (section E2) and two framing DOIs (Perturb-seq, MULTI-seq; section E3) were added and resolved on the same day. The brief now cites 35 DOIs plus one vendor user guide. The research files' own checks were against Europe PMC abstracts only. A second independent re-check on 2026-11-08 (see `factcheck.md`) re-resolved the brief's DOIs, re-read each claim against its abstract, and read the full text of TIP-seq, Corces 2020, AmpliDrop and the capsule paper.

## A. Disease genetics and microglia (research_context.md §1)

| Source | DOI | What it verifies in the brief |
|---|---|---|
| Nott et al. 2019, *Science* | 10.1126/science.aay0793 | Sporadic AD variants "largely confined to microglia enhancers"; deleting a microglia enhancer removed *BIN1* expression in microglia only |
| Corces et al. 2020, *Nat Genet* | 10.1038/s41588-020-00721-x | Full text: LD score regression shows a significant increase in per-SNP AD heritability in the microglia peak set only. Cited in brief §2 |
| Huang et al. 2017, *Nat Neurosci* | 10.1038/nn.4587 | AD heritability enriched in PU.1 (*SPI1*) binding sites; lower-*SPI1* allele linked to later onset |
| Kosoy et al. 2022, *Nat Genet* | 10.1038/s41588-022-01149-1 | Primary human microglia from 150 donors; mechanisms for 21 AD loci, 18 refined to a single gene; SPI1 a key putative regulator |
| Bellenguez et al. 2022, *Nat Genet* | 10.1038/s41588-022-01024-z | 111,326 clinically diagnosed or proxy cases, 677,663 controls; 75 loci, 42 new; pathway analysis "highlighted microglia implication" |

Caveat carried into the brief: amyloid and tau pathways are also implicated; microglia are not the only cell type.

## B. Microglial state depends on environment (research_context.md §2)

| Source | DOI | What it verifies |
|---|---|---|
| Gosselin et al. 2017, *Science* | 10.1126/science.aal3222 | Microglia-specific genes switched off quickly in culture; many linked to disease or risk variants |
| Bohlen et al. 2017, *Neuron* | 10.1016/j.neuron.2017.04.043 | Survival depends on astrocyte-derived CSF-1/IL-34, TGF-β2, cholesterol; profile "requires continuous instructive signaling from the intact CNS" |
| Lavin et al. 2014, *Cell* | 10.1016/j.cell.2014.11.018 | Macrophage enhancer landscapes shaped by tissue, not origin alone (background; not cited in brief) |
| Hasselmann et al. 2019, *Neuron* | 10.1016/j.neuron.2019.07.002 | Transplanted cells gain ex vivo human microglial signature |
| Mancuso et al. 2019, *Nat Neurosci* | 10.1038/s41593-019-0525-x | Transplanted cells resemble primary microglia; about one-third of putative AD risk genes lack adequate mouse orthologs |
| Svoboda et al. 2019, *PNAS* | 10.1073/pnas.1913541116 | Transplanted iPSC microglia resemble primary human microglia |
| Popova et al. 2021, *Cell Stem Cell* | 10.1016/j.stem.2021.08.015 | States differ across models; microglia altered neural stem cell transcription and accelerated organoid network activity (two-way influence) |

## C. Layers are not interchangeable (research_context.md §3)

| Source | DOI | What it verifies |
|---|---|---|
| Schwanhäusser et al. 2011, *Nature* | 10.1038/nature10098 | Mixed result: mRNA and protein levels correlate better than thought; half-lives do not; translation dominates (not cited in brief; caution against overstating) |
| Liu, Beyer & Aebersold 2016, *Cell* | 10.1016/j.cell.2016.03.014 | Transcript levels "not sufficient to predict protein levels in many scenarios" |
| Buccitelli & Selbach 2020, *Nat Rev Genet* | 10.1038/s41576-020-0258-4 | "Reasonable correlation" yet "non-redundant readouts" |
| O'Neill et al. 2016, *Nat Rev Immunol* | 10.1038/nri.2016.70 | Metabolic changes alter immune-cell function (background) |
| Ulland et al. 2017, *Cell* | 10.1016/j.cell.2017.07.023 | TREM2 deficiency disrupts mTOR, ATP and biosynthetic metabolism in microglia |
| Baik et al. 2019, *Cell Metab* | 10.1016/j.cmet.2019.06.005 | Amyloid-β shifts microglia to glycolysis, then a tolerant state with weaker cytokine and phagocytic responses |

## D. Scale (research_context.md, Scale)

| Source | DOI | What it verifies |
|---|---|---|
| GBD 2019 (Nichols et al.), *Lancet Public Health* 2022 | 10.1016/S2468-2667(21)00249-8 | 57.4 million dementia cases in 2019 to 152.8 million in 2050 (all-cause dementia, not AD alone) |
| Alzheimer's Association 2024 | 10.1002/alz.13809 | About 6.9 million Americans 65+; possibly 13.8 million by 2060; $360 billion: estimated 2024 payments for health care, long-term care and hospice for people 65+ with dementia (all dementia, not Alzheimer's alone) |
| Cummings et al. 2014 | 10.1186/alzrt269 | 413 AD trials 2002–2012; 0.4% success; predates anti-amyloid approvals |
| Cummings et al. 2024 | 10.1002/trc2.12465 | 164 trials of 127 drugs in 2024, down from 187 and 141 in 2023 |

## E. Multi-layer methods (research_methods.md)

R = regulatory, T = transcriptome, P-ab = antibody protein, P-MS = MS proteome, M = MS metabolome.

| Method | Layers | Same cells? | DOI | What it verifies in the brief |
|---|---|---|---|---|
| CUT&Tag | R | n/a | 10.1038/s41467-019-09982-5 | Antibody-based regulatory profiling baseline |
| TIP-seq (Bartlett 2021) | R (histone, RNAPII, CTCF) | n/a | 10.1083/jcb.202103078 | The published regulatory part of the core combination. "∼10-fold higher unique reads per single cell compared with other methods": full text compares with published single-cell CUT&Tag (10x scCUT&Tag, Paired-Tag), cross-study. TF shown only for CTCF; needs an antibody |
| nano-CUT&Tag | R ×3 | Yes | 10.1038/s41587-022-01535-4 | Regulatory-only multi-mark method |
| SNARE-seq | R + T | Yes | 10.1038/s41587-019-0290-0 | Two-layer class; 10x Multiome not verified |
| SHARE-seq | R + T | Yes | 10.1016/j.cell.2020.09.056 | Two-layer |
| Paired-Tag | R + T | Yes | 10.1038/s41592-021-01060-3 | Comparison table row |
| scMTR-seq | R + T | Yes | 10.1126/sciadv.adu3308 | Comparison table row |
| scNMT-seq | R + T | Yes | 10.1038/s41467-018-03149-4 | Two-layer, no TF binding |
| CITE-seq | T + P-ab | Yes | 10.1038/nmeth.4380 | Antibody protein only |
| TEA-seq | T + R + P-ab | Yes | 10.7554/eLife.63632 | Three-layer maximum; no M, no P-MS |
| ASAP-/DOGMA-seq | R + P-ab (± T) | Yes | 10.1038/s41587-021-00927-2 | Three-layer maximum; no M, no P-MS |
| Spatial ATAC–RNA | R + T | Same section | 10.1038/s41586-023-05795-1 | Spatial two-layer |
| spatial-Mux-seq | R + T + P-ab | Same section | 10.1038/s41592-024-02576-0 | Three-layer maximum; no M |
| SCoPE2 | P-MS | RNA on parallel cells | 10.1186/s13059-021-02267-5 | Single-cell MS proteome |
| plexDIA | P-MS | n/a | 10.1038/s41587-022-01389-w | MS proteome throughput |
| nanoSPLITS | T + P-MS | Yes | 10.1038/s41467-024-54099-z | Only pairing of MS proteome (with RNA) |
| SpaceM | M | n/a | 10.1038/s41592-021-01198-0 | Single-cell metabolome only |
| scMeT-seq | M + T | Yes | 10.1002/advs.202411276 | Metabolome paired with RNA only |
| Kang et al. | M + T | Yes (plant) | 10.1073/pnas.2512828122 | Metabolome paired with RNA only (plant cells) |
| Vicari et al. | M + T | Same section | 10.1038/s41587-023-01937-y | Metabolome + RNA at spot resolution |
| scSpaMet | M + P-ab | Same tissue | 10.1038/s41467-023-43917-5 | Metabolome + antibody protein only |
| COSIE (Li 2026, preprint) | R + T + P + M | **No** (computed) | 10.64898/2026.03.03.709303 | Only four-layer result; computational across separate sections. DOI resolves to bioRxiv (openRxiv prefix 10.64898), posted 2026-03-05 |

## E2. Capsule barcoding (project-supplied, verified 2026-11-08)

| Source | DOI | What it verifies in the brief |
|---|---|---|
| Leonaviciene & Mazutis 2023, *Nucleic Acids Res* 51(1):e2 (online Oct 2022) | 10.1093/nar/gkac918 | Single cells in semipermeable microcapsules with a concentric core/shell structure (full text, PMC9841424); reagents exchanged by buffer; sequential multi-step enzymatic reactions; shown for RNA cytometry |
| Garcia-Bassets et al. 2024, bioRxiv preprint (AmpliDrop) | 10.1101/2024.08.13.607508 | "A unique single-molecule barcode is clonally amplified—i.e., within the droplet" (Fig. 1 legend, full text); water-in-oil emulsion droplets; shown for transcriptome and chromatin accessibility, not in capsules or with TIP-seq. Still a preprint (bioRxiv API, 2026-11-08: published = NA) |

The combination (TIP-seq inside capsules with in-droplet barcoding) remains unpublished.

## E3. Platform framing and scale (added 2026-11-08, second revision)

| Source | DOI / reference | What it verifies in the brief |
|---|---|---|
| Dixit et al. 2016, *Cell* | 10.1016/j.cell.2016.11.038 | Perturb-seq: single-cell RNA-seq combined with CRISPR-based perturbations, many assays in a pool. Used only as the analogy; the brief says it is not Perturb-seq. Resolved on Crossref 2026-11-08 |
| McGinnis et al. 2019, *Nat Methods* | 10.1038/s41592-019-0433-8 | MULTI-seq: sample multiplexing by lipid-tagged indices; includes a 96-plex perturbation experiment. Example of a per-condition label. Resolved on Crossref 2026-11-08. Whether such a label reaches the lab's own library is **not verified** |
| 10x Genomics, Chromium GEM-X Single Cell 3' Reagent Kits v4 User Guide, CG000731 Rev B (public PDF, no DOI) | none | p. 12: "profiling 500-20,000 individual cells per sample". p. 19: input 1,300–1,600 cells/µl for a 10,000–20,000 cell recovery target. p. 36: "If loading less than 8 samples/chip", so eight wells per chip. Read from the PDF on 2026-11-08 |

Status of the scale figures in the brief:

| Figure | Status |
|---|---|
| About 20,000 cells per commercial well; eight wells per chip | Verified for one kit generation (GEM-X 3' v4) from the vendor guide above. 20,000 is the upper recovery target per sample; loading about 29,000 cells gives about 20,000 recovered (p. 20). Other kits differ (not checked here), so the brief says "one kit generation" |
| About 2,000 cells per condition | Working design figure, not a cited value. A design target, not a power calculation |
| About 10 conditions per well; about 80 per commercial chip | Arithmetic: 20,000 / 2,000 = 10; 8 × 10 = 80. Matches the project's platform design note |
| Microlitre working means less reagent and a more concentrated sample | Design argument, not a cited value |
| Differentiation area is colony-scale; a condition needs a few mm² | Design position from the project, not a published result |
| Earlier sizing of about 441,000 cells per lane | Superseded. That figure is the supply for differentiation, not the readout. It is not in the brief |

Conclusion verified by these rows: no published method measures R with M from the same cells or section, so none delivers R + T + P + M from one defined population; the maximum is three layers. Caveat: searches were not exhaustive; at bulk, four layers come only from split aliquots run as separate assays.

## F. Claims in the brief without a source in the research files

- **Phenotype-first as the harder direction, with no pipeline.** A framing judgement from the project. No paper states it; "we found no pipeline" rests on the non-exhaustive search.
- **Expansion to 10 compartments and beyond; each added compartment multiplies the combinations.** Project design position, not tested.
- **Valves need a second layer plus a membrane; microchannel barriers need photolithography.** Project fabrication constraints for the demo route, not a literature claim.
- **Controlled interaction as novel.** Inferred: none of the methods in table E includes a controlled cell-cell interaction step. Neither file states this directly.
- **Proteome type.** Confirmed by the project: bottom-up LC-MS/MS, from one cold acidified organic extraction that also yields the metabolome.
- **Assay description and status** come from the project lead's brief, not from a publication.

## G. Open verification items (from the research files)

1. Corces 2020 microglia enrichment: **resolved** (full text, LDSC).
2. 10x Multiome: no primary paper; not verified.
3. TIP-seq ~10×: **resolved**. A cross-study comparison with published single-cell CUT&Tag (10x scCUT&Tag, Paired-Tag), not head-to-head.
4. Methods search: database and web searches, not exhaustive.
5. COSIE and AmpliDrop are preprints.
6. Most claims rest on abstracts (full text read only for TIP-seq, Corces 2020, AmpliDrop and the capsule paper).

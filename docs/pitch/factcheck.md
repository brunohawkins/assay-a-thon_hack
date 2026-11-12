# Fact-check of brief.md (final pass, 2026-11-08)

Method. Every DOI in the brief was resolved through Crossref (title, first author, journal, date) and the doi.org handle service. Each claim was then read against the Europe PMC abstract. Full text was read for TIP-seq (PMC8600797), Corces 2020 (PMC7606627), the capsule paper (PMC9841424) and AmpliDrop (bioRxiv full text). COSIE and AmpliDrop metadata were also checked in the bioRxiv API. All 33 DOIs in the first pass resolve (35 after the second revision, see the end). Abstracts were used for the rest, so those verdicts cover what the abstract says.

Verdicts: correct, corrected, removed, unverified.

| # | Claim in brief | DOI | Verdict | Evidence |
|---|---|---|---|---|
| 1 | Sporadic AD variants "largely confined to microglia enhancers" (Nott 2019) | 10.1126/science.aay0793 | Correct | Abstract, verbatim. |
| 2 | AD heritability enriched in PU.1 binding sites (Huang 2017) | 10.1038/nn.4587 | Correct | "AD heritability was enriched within the PU.1 cistrome". |
| 3 | AD heritability enriched in microglial accessible chromatin (Corces 2020) | 10.1038/s41588-020-00721-x | Correct (previously flagged; resolved) | The abstract does not mention it. The full text does: "cell type-specific LD score regression revealed a significant increase in per-SNP heritability for AD in the microglia peak set". Cited as full-text-supported. |
| 4 | GWAS: 111,326 cases, 677,663 controls, 75 loci, 42 new, microglia, amyloid/tau (Bellenguez 2022) | 10.1038/s41588-022-01024-z | Corrected (wording) | All four numbers match the abstract character for character. The abstract says "clinically diagnosed/'proxy' AD cases"; the brief now says "clinical and proxy cases". "42 were new at the time of analysis". |
| 5 | Isolated microglia switch off microglia-specific genes, many disease-linked (Gosselin 2017) | 10.1126/science.aal3222 | Correct | "rapid and extensive down-regulation of microglia-specific genes"; "Substantial subsets … altered expression in neurodegenerative and behavioral diseases". |
| 6 | Astrocyte-derived factors for survival; "requires continuous instructive signaling from the intact CNS" (Bohlen 2017) | 10.1016/j.neuron.2017.04.043 | Correct | Quote verbatim. |
| 7 | Transcript levels alone do not predict protein levels in many scenarios (Liu 2016) | 10.1016/j.cell.2016.03.014 | Correct | "not sufficient to predict protein levels in many scenarios". |
| 8 | Microglial metabolism tied to AD genetics (Ulland 2017) | 10.1016/j.cell.2017.07.023 | Correct | TREM2 risk variants; defective mTOR signalling, ATP and biosynthetic pathways. |
| 9 | 57.4 million with dementia in 2019, 152.8 million in 2050 | 10.1016/S2468-2667(21)00249-8 | Correct | "57·4 (95% UI 50·4–65·1) million … 2019 to 152·8 (130·8–175·9) million … 2050". Dementia of all causes, not AD alone. |
| 10 | 6.9 million Americans 65+; 13.8 million by 2060 | 10.1002/alz.13809 | Correct | "An estimated 6.9 million Americans age 65 and older are living with Alzheimer's dementia … could grow to 13.8 million by 2060, barring … medical breakthroughs". |
| 11 | $360 billion in 2024 care payments | 10.1002/alz.13809 | Corrected | Source: "Total payments in 2024 for health care, long-term care and hospice services for people age 65 and older with dementia are estimated to be $360 billion." This covers all dementia, not only Alzheimer's, and is not "their dementia care". The brief now says exactly that. |
| 12 | 0.4% success across 413 trials, 2002–2012 | 10.1186/alzrt269 | Correct | "413 AD trials were performed … overall success rate during the 2002 to 2012 period of 0.4% (99.6% failure)". The phrase "before the anti-amyloid approvals" is the brief's own context; the paper dates from 2014. |
| 13 | 164 trials of 127 drugs in 2024 vs 187 and 141 in 2023 | 10.1002/trc2.12465 | Correct | "164 trials assessing 127 drugs"; "fewer trials (164 vs. 187), fewer drugs (127 vs. 141)". |
| 14 | Kosoy 2022: 150 donors | 10.1038/s41588-022-01149-1 | Correct | "primary human microglia from 150 donors". "Primary" added to the brief. |
| 15 | Kosoy 2022: 18 of 21 AD loci refined to a single gene | same | Correct | "putative regulatory mechanisms for 21 AD risk loci, of which 18 were refined to a single gene". |
| 16 | Kosoy 2022: SPI1 a key regulator | same | Corrected (earlier pass) | Source: "SPI1 as a key **putative** regulator". The brief says "key putative regulator". |
| 17 | TIP-seq "∼10-fold higher unique reads per single cell" (Bartlett 2021) | 10.1083/jcb.202103078 | Corrected (comparator stated) | See the TIP-seq section below. |
| 18 | TIP-seq covers histone marks, RNAPII, CTCF | same | Correct | "map histone modifications, RNA polymerase II (RNAPII), and transcription factor CTCF binding sites". |
| 19 | Semipermeable core-shell capsules carry single cells through multi-step enzymatic reactions (Leonaviciene & Mazutis 2023) | 10.1093/nar/gkac918 | Correct, added | Resolves to "RNA cytometry of single-cells using semi-permeable microcapsules", *Nucleic Acids Res* 51(1):e2, online 21 Oct 2022. "Microcapsules support sequential multi-step enzymatic reactions". Full text: "concentric core/shell structure", reagents exchanged by changing buffer. Shown for RNA cytometry. |
| 20 | AmpliDrop clonally amplifies one barcode inside each droplet (preprint) | 10.1101/2024.08.13.607508 | Correct, added | Resolves to bioRxiv, Garcia-Bassets et al., "Single-Molecule Barcoding Technology for Single-Cell Genomics", 13 Aug 2024. bioRxiv API: published = NA, so still a preprint. Fig. 1a legend: "A unique single-molecule barcode is clonally amplified—i.e., within the droplet". The droplets are water-in-oil emulsion droplets. Shown for transcriptome and chromatin accessibility; not in capsules, not with TIP-seq. |
| 21 | TIP-seq inside capsules with in-droplet barcoding is unpublished | none | Kept, unverifiable by nature | None of the three papers combines them. The search was not exhaustive. |
| 22 | COSIE: four layers only by computation across separate sections (preprint) | 10.64898/2026.03.03.709303 | Correct | See the COSIE section below. |
| 23 | TEA-seq, DOGMA-seq, spatial-Mux-seq are three-layer methods | 10.7554/eLife.63632; 10.1038/s41587-021-00927-2; 10.1038/s41592-024-02576-0 | Correct | TEA-seq: transcripts, epitopes, chromatin accessibility. DOGMA-seq: chromatin, RNA, surface protein (Mimitou 2021). Spatial-Mux-seq: two histone marks, accessibility, transcriptome, protein panel. Online Jan 2025, DOI year 2024. None has metabolites or MS proteome. |
| 24 | MS proteome paired only with RNA (nanoSPLITS; Fulcher 2024) | 10.1038/s41467-024-54099-z | Correct | Transcriptome plus "mass spectrometry-based proteomics" from the same single cells. "Only" rests on the non-exhaustive search. |
| 25 | Metabolome paired with RNA (Mao 2024; Kang 2025, plant cells) | 10.1002/advs.202411276; 10.1073/pnas.2512828122 | Correct | scMeT-seq: metabolome then transcriptome in one cell (macrophages). Kang: scRNA-seq and scMS on the same plant (Catharanthus) cell. |
| 26 | Table rows: Paired-Tag, scMTR-seq, Vicari, scSpaMet | 10.1038/s41592-021-01060-3; 10.1126/sciadv.adu3308; 10.1038/s41587-023-01937-y; 10.1038/s41467-023-43917-5 | Correct; DOIs added to §9 | Paired-Tag: histone marks plus transcriptome. scMTR-seq: six histone marks plus transcriptome. Vicari: mass spectrometry imaging plus Visium. scSpaMet: spatial metabolomics plus multiplexed protein imaging. |
| 27 | Transplantation restores state (Hasselmann, Mancuso, Svoboda 2019) | 10.1016/j.neuron.2019.07.002; 10.1038/s41593-019-0525-x; 10.1073/pnas.1913541116 | Correct | Mancuso: "recapitulate transcriptionally human primary microglia ex vivo". Svoboda title: iPSC microglia "assume a primary microglia-like state after transplantation". Mancuso used embryonic stem cell-derived cells. The brief does not say iPSC here. |
| 28 | One-third of putative AD risk genes lack adequate mouse orthologs (Mancuso 2019) | 10.1038/s41593-019-0525-x | Correct | Verbatim. |
| 29 | Microglia changed neural stem cell transcription and accelerated network activity (Popova 2021) | 10.1016/j.stem.2021.08.015 | Correct | "microglia induce transcriptional changes in neural stem cells"; "accelerate the emergence of synchronized oscillatory network activity in brain organoids". |
| 30 | Transcript and protein are non-redundant readouts (Buccitelli 2020) | 10.1038/s41576-020-0258-4 | Correct | "transcriptomics and proteomics provide useful non-redundant readouts". Limited to those two layers. |
| 31 | Amyloid-β: OXPHOS to glycolysis, then tolerant state with weaker cytokine and phagocytic responses (Baik 2019) | 10.1016/j.cmet.2019.06.005 | Correct | Abstract states each step. |
| 32 | Proteome is MS-based | project fact | Closed | Bottom-up LC-MS/MS. The "should be confirmed" sentence is gone. The table cell reads "MS proteome, bulk". |
| 33 | Novelty 4: no compared method controls cell-cell interaction | none | Unverified by citation (inference) | Judged from the methods compared; no paper states it. |
| 34 | 10x Multiome | none | Unverified | No primary paper found. Listed in §8 open items. Not used in a claim. |

## COSIE (10.64898/2026.03.03.709303)

Verdict: the DOI is genuine and resolves; the claim is kept.

- doi.org handle: URL value `http://biorxiv.org/lookup/doi/10.64898/2026.03.03.709303`, registered 2026-03-05.
- Crossref and the bioRxiv API: Li et al. (corresponding author Mingyao Li, University of Pennsylvania), "Toward Computationally Complete Spatial Omics", bioRxiv, genomics, version 1, 5 Mar 2026. Published = NA, so a preprint.
- 10.64898 is the DOI prefix used by openRxiv, the organisation now running bioRxiv. It is unusual only because it is new.
- Abstract: COSIE is "a computational framework that generates high-resolution, multilayered molecular landscapes across tissue sections, individuals, and platforms" and "integrates histology, epigenome, transcriptome, proteome, and metabolome". It "predicts unmeasured modalities". The brief's description holds: four molecular layers by computation across separate sections, not measured together. COSIE also adds histology, which the brief does not count as a layer. "Only four-layer result" rests on a non-exhaustive search.

## TIP-seq comparator (Bartlett 2021)

The abstract says "∼10-fold higher unique reads per single cell compared with other methods". The full text names the comparators. They are published single-cell CUT&Tag datasets, not a head-to-head experiment.

- Droplet (10x) scCUT&Tag, H3K27me3, PBMCs (Wu 2021): 1,110 unique reads per cell against 11,144 for sciTIP-seq, "10-fold higher coverage".
- Paired-Tag (Zhu 2021), reanalysed on shared epitopes: "∼10-fold more unique reads per cell". The authors note the caveat: Paired-Tag used brain tissue, TIP-seq used cultured cells.
- Other comparisons in the same text: 58-fold against a second 10x scCUT&Tag study (Bartosovic 2021) and 25-fold against a combinatorial-indexing study (Carter 2020). The only head-to-head test, CTCF with 5,000 cells, gave ∼23.2-fold more unique reads than bulk CUT&Tag.

The brief quotes the abstract with its own wording ("compared with other methods") and §8 states the real comparator and the caveat.

## Closed items

- Corces 2020 enrichment: resolved from the full text (item 3).
- Capsule and AmpliDrop citations: added to §5, §8 and §9, and to sources.md E2. The brief says the combination is unpublished.
- Proteome type: closed (item 32).

## Corrected or removed in this pass

- Corrected: the $360 billion wording; "clinical and proxy cases" for Bellenguez; "primary" microglia for Kosoy; the TIP-seq comparator (earlier pass).
- Added: four Paired-Tag/scMTR-seq/Vicari/scSpaMet DOIs to §9, because the comparison table cited them without a DOI.
- Removed: the "MS-based should be confirmed" open item, the Corces open item and the TIP-seq comparator open item.
- No claim was dropped as unsupported.

## Second revision: platform reframing (2026-11-08)

The brief was rewritten around a multiplexed cellular-perturbation frame. Every verified claim and citation above was kept. Rows 1–34 stand unchanged. The brief now cites 35 DOIs; the two new ones resolve on Crossref.

| # | New claim in brief | Source | Verdict | Evidence |
|---|---|---|---|---|
| 35 | Model is Perturb-seq, where the perturbation is a CRISPR edit; many assays in a pool (Dixit 2016) | 10.1016/j.cell.2016.11.038 | Correct | Abstract: "combining single-cell RNA sequencing (RNA-seq) and … CRISPR-based perturbations to perform many such assays in a pool". The brief says this is an analogy, not a claim to be Perturb-seq. |
| 36 | Perturb-seq's guide barcode travels with the cell, so it needs no separate label | same | Correct (project note, consistent with the paper) | Each cell carries its guide identity in the pool. Stated in the project's platform design note; not worded in the abstract. |
| 37 | Phenotype-first is the harder direction; we found no pipeline | none | Unverified (judgement) | Framing from the project. No source states it. "Found no" rests on the non-exhaustive search. Worded as "we found". |
| 38 | A commercial droplet well takes up to about 20,000 cells; a chip has eight wells | 10x Genomics user guide CG000731 Rev B (no DOI) | Verified for one kit generation | I downloaded the public PDF and read it. p. 12: "profiling 500-20,000 individual cells per sample". p. 19: 1,300–1,600 cells/µl for a 10,000–20,000 recovery target. p. 36: "If loading less than 8 samples/chip", so eight wells. p. 20: about 29,000 cells loaded for about 20,000 recovered. This is GEM-X 3' v4. Other generations differ, so the brief says so in §9. No DOI exists, none was invented. |
| 39 | About 2,000 cells per condition | none | Working figure | A design target, not a citation and not a power calculation. Rare sub-states could need more. |
| 40 | About 10 conditions per well; about 80 per commercial chip | arithmetic | Correct | 20,000 / 2,000 = 10; 8 × 10 = 80. Identical to `protocols/platform_design.md` (20,000; 8; 2,000; 10; 80; one chip's library out = one well). |
| 41 | The commercial well is a yardstick, not part of the pipeline | project note | Project fact | platform_design.md §1: the pipeline encapsulates, barcodes and builds its own library. |
| 42 | Microlitres: less reagent, more concentrated sample | none | Working claim | Design argument. Not cited. |
| 43 | Differentiation area (colony scale, once) differs from condition area (a few mm²) | none | Project design position | platform_design.md §1 gives 4.0 mm² at 500 cells/mm² for 2,000 cells, under 10 mm² with pessimistic recovery. The brief says "a few mm²". |
| 44 | Three cell types are a use case and proof of concept; expansion to 10 compartments and beyond; each added compartment multiplies the combinations | none | Project design position | platform_design.md §2 gives N(N−1) directed pairs and 2ᴺ−1 subsets (6 and 7 at N=3; 90 and 1,023 at N=10). The brief states the multiplication without the counts. |
| 45 | Five further uses: new differentiation protocols, dose and timing, which cell type perturbs which, genetic backgrounds, screening interventions | none | Project design position | Matches platform_design.md §2 items 1–5. Not tested. |
| 46 | Demo is single-layer and valveless; valves need a second layer plus a membrane; microchannel barriers need photolithography | none | Project constraint | platform_design.md §4a and §4b. Valve and barrier precedents exist (Unger 2000; Taylor 2005) but the brief does not cite them. |
| 47 | Clustering separates the three cell types but not the condition chamber; a label or per-chamber barcode is needed | none | Project design position; sound | platform_design.md §3. Clustering by treatment effect would be circular. |
| 48 | A per-condition lipid-anchored oligo label (MULTI-seq) | 10.1038/s41592-019-0433-8 | Correct | Abstract: lipid-tagged indices; barcodes any cell type; "96-plex perturbation experiment". |
| 49 | Whether such a label reaches the lab's own library | none | **Unverified** | platform_design.md names this as the first coupon to run. Flagged in §9. |
| 50 | MS readout from a ~2,000-cell chamber | none | **Unverified** | Not established anywhere in the files read. The earlier project brief used about 10⁴ cells per arm for the metabolome. Flagged in §9 as untested. |

Number correction. The earlier full-scale sizing, about 441,000 cells per lane, was the wrong target for the readout. It is the supply for differentiation, not the readout. The brief does not state it.

Not checked: the manufacturer's page listing; the 40,000-cell on-chip multiplexing claim from search snippets; Next GEM kit limits. None is used in the brief.

## Length

brief.md is about 1,470 words outside its two tables (headings and bullets included) and about 1,880 with them. That is longer than the first pass because six new points were added. The tables are mostly DOIs.

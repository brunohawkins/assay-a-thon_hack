# Multi-layer measurement methods: what each one captures

Layers: **R** = regulatory (TF binding or chromatin), **T** = transcriptome, **P** = protein (**P-ab** = antibody panel; **P-MS** = mass-spectrometry proteome), **M** = MS metabolome.
DOIs checked against Europe PMC abstracts (Nov 2026).

| Method | Layers | Resolution | Same cells? | Main limitation | DOI |
|---|---|---|---|---|---|
| CUT&Tag | R (histone marks, RNAPII, TFs) | Bulk, low input, single cells | n/a (one layer) | Needs an antibody for each target | 10.1038/s41467-019-09982-5 |
| **TIP-seq** | R (histone marks, RNAPII, CTCF) | Single cell (combinatorial indexing) | n/a (one layer) | Needs an antibody; regulatory layer only | 10.1083/jcb.202103078 |
| nano-CUT&Tag | R ×3 (ATAC, H3K27ac, H3K27me3) | Single cell | Yes | Regulatory layer only; needs 25,000–200,000 input cells | 10.1038/s41587-022-01535-4 |
| SNARE-seq (droplet RNA+ATAC, same design class as 10x Multiome) | R (ATAC) + T | Single nucleus | Yes | No protein or metabolites; 10x Multiome itself not verified (no primary paper) | 10.1038/s41587-019-0290-0 |
| SHARE-seq | R (ATAC) + T | Single cell | Yes | No protein or metabolites | 10.1016/j.cell.2020.09.056 |
| Paired-Tag | R (5 histone marks) + T | Single nucleus | Yes | Antibodies; histone marks only | 10.1038/s41592-021-01060-3 |
| scMTR-seq | R (6 histone marks) + T | Single cell | Yes | Antibodies; no protein or metabolites | 10.1126/sciadv.adu3308 |
| scNMT-seq | R (accessibility, DNA methylation) + T | Single cell | Yes | No TF binding, protein or metabolites | 10.1038/s41467-018-03149-4 |
| CITE-seq | T + P-ab | Single cell | Yes | Antibody panel only | 10.1038/nmeth.4380 |
| TEA-seq | T + R (ATAC) + P-ab | Single cell | Yes | Antibody panel only; no metabolites | 10.7554/eLife.63632 |
| ASAP-seq / DOGMA-seq | R (ATAC) + P-ab (± T in DOGMA) + mtDNA | Single cell | Yes | Sparse ATAC; antibody panel; no metabolites | 10.1038/s41587-021-00927-2 |
| Spatial ATAC–RNA / CUT&Tag–RNA | R (ATAC or one histone mark) + T | Spatial, near single cell | Same tissue section | No protein or metabolites | 10.1038/s41586-023-05795-1 |
| spatial-Mux-seq | R (2 histone marks + ATAC) + T + P-ab | Spatial, "cellular level" | Same section | Antibody panel; no metabolites | 10.1038/s41592-024-02576-0 |
| SCoPE2 | P-MS (>3,000 proteins) | Single cell | RNA measured only on **parallel** cells (10x) | No regulatory layer | 10.1186/s13059-021-02267-5 |
| plexDIA | P-MS (~1,000 proteins per cell) | Single cell or bulk | n/a (one layer) | Lower depth at single-cell level | 10.1038/s41587-022-01389-w |
| nanoSPLITS | T + P-MS | Single cell | Yes (each cell's lysate is split) | No regulatory layer or metabolites | 10.1038/s41467-024-54099-z |
| SpaceM | M (>100 metabolites) + fluorescence | Single cell, in situ | n/a | Metabolites only | 10.1038/s41592-021-01198-0 |
| scMeT-seq | M + T | Single cell | Yes (sub-picolitre sample, then RNA-seq) | No regulatory layer or protein | 10.1002/advs.202411276 |
| Kang et al. (plant protoplasts) | M + T | Single cell | Yes | Plant cells; 96-well throughput | 10.1073/pnas.2512828122 |
| Spatial metabolome + transcriptome (Vicari) | M + T + histology | Spatial (spots) | Same section | Spot resolution, not single cell | 10.1038/s41587-023-01937-y |
| scSpaMet | M + P-ab (imaging) | Spatial, single cell | Same tissue | Antibody protein only; no RNA or R | 10.1038/s41467-023-43917-5 |
| COSIE (preprint) | R + T + P + M (computational) | Spatial | **No**: integrates separate sections computationally | Not a measurement method | 10.64898/2026.03.03.709303 |

## TIP-seq

- **Developers:** Bartlett DA, Dileep V, Handa T, Ohkawa Y, Kimura H, Henikoff S and Gilbert DM, in the Gilbert lab (*J Cell Biol* 2021; DOI 10.1083/jcb.202103078).
- **What it measures:** genome-wide binding of chromatin proteins in single cells. A protein A–Tn5 fusion inserts a T7 promoter next to the antibody-bound target. Shown for histone marks, RNAPII and the TF CTCF.
- **Advantages:** T7 linear amplification gives "∼10-fold higher unique reads per single cell compared with other methods". Combinatorial indexing adds throughput and multiplexes samples.
- **Not verified:** the abstract does not name CUT&Tag as the comparator, so the head-to-head 10× figure against scCUT&Tag comes from the abstract's wording and was not checked in the full text.

## Conclusion

No published method measures the regulatory layer (TF binding or chromatin state) together with the metabolome from the same cells or the same tissue section, so no method delivers all four layers (R + T + P + M) from one defined population. The most in one measurement is three layers (chromatin, RNA, antibody protein) in TEA-seq (10.7554/eLife.63632), DOGMA-seq (10.1038/s41587-021-00927-2) and spatial-Mux-seq (10.1038/s41592-024-02576-0). None of these includes metabolites, and none includes an MS proteome. Metabolite methods pair them with one other layer only: RNA in scMeT-seq (10.1002/advs.202411276), Kang et al. (10.1073/pnas.2512828122) and Vicari et al. (10.1038/s41587-023-01937-y), or antibody protein in scSpaMet (10.1038/s41467-023-43917-5). The MS proteome has been paired only with RNA (nanoSPLITS, 10.1038/s41467-024-54099-z). The only four-layer result, COSIE (10.64898/2026.03.03.709303), combines separate sections by computation. The missing combinations are therefore regulatory + metabolomic, and the full four-layer set. Caveat: based on database and web searches, not exhaustive; at bulk, four layers come only from split aliquots run as separate assays.

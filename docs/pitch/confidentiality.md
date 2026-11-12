# Confidentiality: statements drawing on the unpublished programme

The lab's wider programme is unpublished. Nothing specific to it should appear in public-facing sentences of the pitch or deck.

## The programme (do not disclose)

- Fine-mapping Alzheimer's genetic risk to specific transcription factors acting in microglia.
- Profiling those transcription factors' binding by single-cell TIP-seq.
- Comparing iPSC lines of high and low polygenic risk.
- Building a screening assay with binding and cell-state readouts as an evidence base for therapeutics.
- The framing that this assay supplies what a binding readout alone cannot: cellular context (neurons, astrocytes) and downstream layers (protein, metabolite).

## Statements in brief.md that draw on it

| Section | Statement | How it is handled |
|---|---|---|
| 1. Thesis | "follow Alzheimer's disease (AD) risk from microglial regulatory DNA to protein and metabolism" | Generic; rests on public genetics (Nott, Huang, Kosoy, Bellenguez). Names no TF and no programme step. Safe if kept generic. |
| 5. Why now | Kosoy 2022 named SPI1 a key putative regulator | Public finding. Do not link it to any TF the lab has chosen or fine-mapped. |
| 7. What it enables | "Test whether a candidate microglial risk mechanism changes transcript, protein and metabolite output, and whether neurons or astrocytes alter the effect" | Generic form of the fine-mapping + context aim. Do not name targets. |
| 7. What it enables | "compare genetic backgrounds, including iPSC lines of differing background, in one setting" | Generic form of the high/low polygenic-risk comparison. Do not say "polygenic risk" or "high and low" publicly. |
| 7. What it enables | "screen interventions on matched regulatory, cell-state, protein and metabolite readouts" | Generic form of the therapeutics screening assay. See the higher-risk row below. |
| 9. Limits | "has not been shown for microglial TFs" | Public fact about TIP-seq; do not add which TFs the lab plans to profile. |
| 9. Limits | TIP-seq inside capsules with in-droplet barcoding is unpublished; its three parts are listed one by one (TIP-seq, semipermeable core-shell capsules, AmpliDrop), each with its gap | The lab's own unpublished method combination. Listing the three published parts, also in §5 and §10, makes the design easy to reconstruct. Decide with MJ whether to disclose it before filing or publication. |
| 5. Why now; 10. Evidence | Capsules (Leonaviciene & Mazutis 2023) and AmpliDrop (Garcia-Bassets 2024, preprint) cited as "the parts exist"; two DOI rows | Public papers, but together they point to the unpublished barcoding design. Same decision as above. Remove the capsule and AmpliDrop sentences in §5, the sub-bullets in §9 and the §10 row for a public version. |
| 9. Limits | TIP-seq's ~10-fold gain is cross-study; Paired-Tag data were brain tissue, TIP-seq cultured cells | Published facts from Bartlett 2021. No programme content. |
| 9. Limits | Proteome by bottom-up LC-MS/MS and metabolome from one cold acidified organic extraction of parallel replicated fields | An unpublished assay detail (see below), stated explicitly. Confirm with MJ, or reduce it to "lane-level MS proteome and metabolome" in public versions. |
| 8. Table | "MS proteome, bulk" and "Bulk" metabolome; "Same lanes"; "Yes, with vehicle control" | Generic; low risk. Reveals the lane format and a no-cell control at a high level only. |

## Re-check against the final brief.md (2026-11-08, after the platform revision)

- Programme terms absent from the text: no named TF other than SPI1 (a published finding), no "polygenic risk", no "high and low", no "therapeutics", no target list. A grep of the final brief for "screen" finds one hit, in §7 ("screen interventions"), covered in the higher-risk row below.
- Section numbers above follow the final brief: §6 Scale, §7 What it enables, §8 Table, §9 Limits, §10 Evidence.

## Added in the platform revision (2026-11-08)

| Section | Statement | How it is handled |
|---|---|---|
| 1. Thesis | Multiplexed cellular-perturbation platform; analogy to Perturb-seq; phenotype-first | Framing of the assay, not of the programme. Names no TF or target. The "no pipeline" claim signals the lab's position. Confirm with MJ that this framing may go public. |
| 6. Scale | About 2,000 cells per condition; about 10 conditions per well; about 80 per commercial chip; condition chambers of a few mm²; differentiate once then distribute | Unpublished design arithmetic. Reveals the platform's sizing and architecture. Confirm with MJ. The commercial figures (20,000 cells, eight wells) are public vendor numbers. |
| 6. Scale | Three cell types as proof of concept; 10 compartments and beyond; compartments that talk | Unpublished roadmap. Confirm with MJ. |
| 7. What it enables | "screen interventions", "test new differentiation protocols", "compare genetic backgrounds" | **Higher risk than before.** The earlier text had "Rank candidate interventions", the only trace of the therapeutics-screening aim. The word "screen" now appears, as a generic platform use. No target, no TF, no "polygenic risk", no "high and low". Decide with MJ whether to keep "screen interventions" in a public version. |
| 9. Limits | Demo is single-layer and valveless; valves need a second layer plus a membrane; microchannel barriers need photolithography | Fabrication route of the unpublished assay. Low risk, but it describes the lab's hardware. |
| 9. Limits | Per-condition label (MULTI-seq) or per-chamber barcode at encapsulation; label may not reach the library | Unpublished assay detail. MULTI-seq is a public method. The "unverified" note signals an open technical risk. |
| 9. Limits | MS input from a ~2,000-cell chamber is untested | Unpublished assay detail. |
| 10. Evidence | 10x Genomics user guide; Perturb-seq; MULTI-seq | Public sources. Naming the vendor kit shows how the library size was set. Low risk. |

Not in the brief, by design: the earlier 441,000-cells-per-lane sizing, chamber dimensions, flow rates, block names and any file or parameter key from the design repository.

## Assay details that are also unpublished

- The automated pipeline that turns a written protocol into chips, and the validated flow simulation of each design.
- Hydrogel-capsule per-cell barcoding combined with TIP-seq.
- Proteome and metabolome from one extraction of parallel replicated fields.
- Status (dye-only demo; full-scale design simulated, not built).

These describe the assay rather than the programme, but they are not public; confirm with MJ what may be shown.

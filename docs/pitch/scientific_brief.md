# Scientific brief: measuring the full state of human microglia while they interact with neurons and astrocytes

Draft for MJ to check before the pitch and the 3-slide deck are built. Written 8 Nov 2026.

**How to read this**

- Every factual claim carries a numbered citation; the list is in `references.md`, with what was actually read for each source (abstract only, or full text) and exactly what it supports.
- Two key sources are **preprints that have not been peer reviewed** (refs 21, 22). They are flagged where they are used.
- Statements about the assay come from the design files (`aath/`, `aath-pipeline/`) and describe a **design and a physics budget. No cells have been run.**
- Section 10 is **internal**: it links the assay to the lab's unpublished programme and must not go into public text. Everything else is written so it can be used publicly, but check it against the confidentiality rules first.
- The scope is measurement science and disease biology only. No genome-editing, cloning or cell-engineering methods are described.

---

## 1. The claim in one paragraph

Most genetic risk for Alzheimer's disease (AD) lies in DNA that controls genes in microglia, and it appears to act through a small set of transcription factors, PU.1 (encoded by *SPI1*) above all [3–7]. But microglia only keep the regulatory landscape these variants sit in while they receive signals from neurons and astrocytes. Taken out of that context, they lose about a third of their AD-risk gene expression and more than half of their super-enhancers within days [1, 2, 13]. The effects of polygenic risk on human microglia show up under challenge or inside a brain, not at rest in a dish [21, 22]. They also show up in protein, lipid and metabolic phenotypes that RNA does not predict [17–20, 23–26]. Today these layers are measured in different systems: post-mortem tissue, isolated cell types, mouse hosts, or mixed co-cultures where nobody controls when the interaction starts. No measurement shows **which named transcription factor is bound at a risk enhancer in a human microglial population with a defined neuronal and astrocytic context, together with what that population then expresses, makes, metabolises and secretes.** The assay is designed to make that measurement. Each lineage matures in isolation, the cells interact on a set schedule with a vehicle control, and single-cell transcription-factor binding (TIP-seq) or RNA, bulk proteome and metabolome, and a secretome time series are all read from one culture history.

---

## 2. The problem (disease biology)

**2.1 AD risk sits in microglial regulatory DNA, organised around specific transcription factors.**

- The largest AD genome-wide association study (GWAS) to date: 111,326 cases and 677,663 controls, 75 risk loci (42 new). Pathway analysis "highlighted microglia implication" [3].
- Mapping enhancers by brain cell type: sporadic AD variants "were largely confined to microglia enhancers", while psychiatric-disorder variants mapped to neurons [4].
- AD risk variants are enriched at open chromatin carrying SPI1 and MEF2 motifs in microglia. SPI1-motif sites capture "the majority of the common variant SNP-chip heritability" [5].
- A common haplotype that lowers *SPI1*/PU.1 expression in myeloid cells delays AD onset, and AD heritability is enriched in the PU.1 cistrome [6].
- In primary microglia from 150 donors, fine-mapping refined 21 AD loci (18 to a single gene) and identified SPI1 as a key regulator of microglial expression and AD risk [7].
- Primary-microglia QTL and myeloid-enhancer studies place candidate causal variants in microglia-specific enhancers [8, 9]. Brain single-cell epigenomics nominates functional AD single-nucleotide polymorphisms (SNPs) by cell type [10].

**2.2 The variants' effects depend on context, so measuring them at rest misses most of them.**

- Expression QTLs (eQTLs) explain "only a small fraction of GWAS signals", because GWAS hits and eQTLs are systematically different kinds of variant [11].
- In human macrophages, about 60% of stimulus-specific eQTLs with a detectable chromatin effect already altered accessibility in naive cells ("enhancer priming"), probably by changing binding of lineage factors such as PU.1. The authors conclude that detecting the downstream effects "will be challenging, requiring profiling of large numbers of stimulated cellular states and time points" [12].

**2.3 The microglial regulatory landscape is maintained by the cells around it.**

- Moving human microglia from brain into culture caused >2,000 genes to fall more than 4-fold within 6 h. After 7 days, **31% of genes associated with AD risk variants** had fallen more than 2-fold, **424 of 740 super-enhancers** were lost, and the enhancers that lost activity were enriched for PU.1, IRF, AP-1, SMAD and MEF2 motifs. In the same study, the AD risk variant rs6733839 upstream of *BIN1* sits in a region bound by PU.1 [1].
- In defined-medium cultures of isolated microglia, astrocyte-derived factors (CSF-1/IL-34, TGF-β2 and cholesterol) kept the cells alive. The microglial signature needed "continuous instructive signaling from the intact CNS" and was restored by engraftment [2].
- Human iPSC-derived microglia co-cultured with iPSC neurons turned down pathogen-response pathways, turned up homeostatic pathways and changed their cytokine response compared with monoculture [13].
- iPSC microglia only take on a primary-microglia-like signature after transplantation into mouse brain [14, 15]. Fetal-to-postnatal maturation of human microglia is governed by specific gene regulatory networks that culture does not reproduce on its own [16].

**2.4 Microglial state acts through neighbours, and through protein and metabolism as much as RNA.**

- Microglia → astrocytes: activated microglia secrete IL-1α, TNF and C1q, which induce neurotoxic reactive astrocytes (shown in rodents, with these astrocytes found in human disease tissue) [27].
- Human tri-culture: complement C3 is potentiated in tri-culture and further raised in AD-mutant cultures because "microglia initiat[e] reciprocal signaling with astrocytes" [28].
- Human tri-culture: astrocytes induce disease-associated microglia (DAM) genes (*TREM2*, *SPP1*, *APOE*, *GPNMB*), and familial AD neurons dampen that signature [29].
- Lipid metabolism: an ACSL1⁺ microglial state is most abundant in *APOE4/4* AD brains. In iPSC microglia, fibrillar Aβ induces triglyceride synthesis and lipid droplets, and conditioned medium from these microglia drives tau phosphorylation and neurotoxicity in an APOE-dependent way [23].
- Neuronal cues induce a lipogenic programme in APOE4 iPSC microglia that impairs their surveillance of neuronal network activity [24].
- *APOE4* disrupts the lipidome of human iPSC astrocytes [25]. Lipid-droplet-accumulating microglia are a dysfunctional, pro-inflammatory state in the ageing brain [26].
- The proteome is not a proxy for the transcriptome. In >1,000 brain tissues, "nearly half of the protein co-expression modules … were not observed in RNA networks from the same cohorts", including an AD module tied to MAPK signalling and metabolism that tracks cognitive decline [17]. In >2,000 brains, the module most associated with AD pathology concerns sugar metabolism and is enriched in microglial and astrocytic markers [18]. In general, transcript levels alone "are not sufficient to predict protein levels in many scenarios", especially during dynamic transitions [19].
- In vivo, human microglia adopt several distinct responses to amyloid (DAM, HLA and cytokine-response states), and AD risk genes are regulated differently across these states [20].

---

## 3. What can and cannot be measured today

| Human system | Neuronal / astrocytic context | Layers typically measured | What it cannot tell you |
|---|---|---|---|
| Post-mortem brain atlases: snRNA + chromatin accessibility at scale [7, 10, 30–32] | Native, but end-stage and fixed in time | RNA, accessibility; TF regulators **inferred** from motifs (e.g. MITF [32]) | No perturbation or time course. Bound TF not measured directly. Dissociation and post-mortem artefacts, most prominent in microglia [33] |
| iPSC microglia monoculture, risk-stratified lines (51 lines) [21]\* | **None** | Functional assays, cytokines, bulk proteome | Polygenic-risk effects appeared **only under LPS challenge**. Bulk proteome found no differentially expressed proteins at rest and one under LPS. The authors name monoculture (fetal-like, partially activated) as a limitation |
| iPSC microglia xenografted into mouse brain [14, 15, 20, 22]\* | Restored, but **mouse** neurons and astrocytes | scRNA-seq mainly | Low throughput, not a screening format, host is non-human. Risk-linked MHC-II variation across 31 donors was seen *in vivo* [22]\* |
| Human iPSC tri-cultures / assembloids [28, 29, 34–36] | Human, cells mixed together | RNA, targeted proteins, function | Interaction is present from plating: no defined onset, no direction, no clean "before". Attributing a secreted molecule to its source cell needs extra steps. No TF-binding or untargeted metabolome readout reported in the studies read |
| Single-cell regulatory methods | Applied to any of the above | Accessibility + RNA (e.g. SUM-seq) [37]; histone marks / named TFs (scCUT&Tag: OLIG2, RAD21 in mouse CNS [38]; TIP-seq: CTCF, RNA Pol II [39]) | Accessibility does not name the TF. Footprints miss TFs with short DNA residence times [40]. Named-TF single-cell data in human microglia in a defined interaction context: none found |

\* Preprint, not peer reviewed.

**The counterpoint, stated fairly.** A genome-wide footprinting reference found that context-dependent regulation is "chiefly executed by wholesale modulation of accessibility … rather than by differential transcription factor occupancy within accessible elements". It also found that disease-variant enrichment in regulatory DNA is "almost entirely attributable to variants within footprints", that is, at sites where a TF sits [41]. So accessibility may track context well, while variant interpretation depends on **which factor occupies the site**. That is the case for a named-TF readout. It is also what a reviewer will push on (§8).

---

## 4. The exact knowledge gap

> **In no human system can we currently measure, for one microglial population with a defined neuronal and astrocytic context, whether a named transcription factor is bound at AD-risk enhancers, and link that binding, before and after a controlled interaction, to the same population's transcriptome, proteome, metabolome and secretome.**

The gap has four parts, and each is closed by one feature of the design:

| Sub-gap | Evidence it is real | What closes it in the design |
|---|---|---|
| **G1 Context.** Risk enhancers are measured in microglia that lack the signals maintaining them | [1, 2, 13, 21] | Microglia matured beside, then exposed to, human neurons and astrocytes, with a vehicle arm |
| **G2 Specificity.** Accessibility shows *that* a site is open, not *which* TF binds it | [12, 40, 41] | Antibody-targeted single-cell TF binding (TIP-seq) [39] |
| **G3 Layers.** RNA does not predict protein, and the AD-relevant microglial phenotypes are lipid and metabolic | [17–19, 21, 23–26] | Bulk proteome and metabolome from one cold extraction, quenched in place on fields of the **same lane** |
| **G4 Interaction attribution.** In mixed co-cultures the start, direction and source of signals are undefined | [28, 29, 34] | Lanes isolated during maturation, then connected one direction at a time. Repeated secretome draws before and after. Lane-of-origin metabolic pulse label to trace secreted material |

---

## 5. What the assay measures, as designed

Wild-type human iPSCs are differentiated into neurons, microglia and astrocytes in separate lanes of one microfluidic chip. Each lane is a closed recirculating loop while the cells mature.

**Isolation.** By the design note's diffusion estimate, a small molecule would take about 25 days to cross from field to field across the shared channel.

**Interaction schedule.** The lanes are then connected one direction at a time:

- 6-h windows on days 81, 83 and 85.
- Each direction has its own vehicle arm.
- Supernatant is drawn from all three lanes before and after each window, without opening the loop.

These figures are from `protocols/module1_target_design.md`.

| Layer | Question it answers | How (design) | Resolution | Main design limit |
|---|---|---|---|---|
| **TF binding (TIP-seq)** | Is the named factor bound at the risk enhancer in this cell, in this context? | Antibody-guided insertion and linear amplification [39]. Cells in hydrogel capsules [42, 43], each barcoded clonally in its own oil droplet (AmpliDrop idea [44] applied to capsules) | Single cell | Fragments under ~300 bp are lost through the capsule shell. Single-cell binding of microglial lineage TFs is unproven (§8) |
| **RNA** | Which state programme did the cell adopt (homeostatic, DAM, HLA, cytokine-response, lipid) [20, 30] | Same capsule and barcoding chain, reverse transcription in capsule | Single cell | Run on a **separate capsule aliquot** from TIP-seq, so RNA and TF binding come from different cells of the same lane |
| **Proteome** | Does the programme reach protein? | Pellet from one cold, acidified, single-phase organic extraction of adherent cells, in place | Bulk, same lane | Native complexes and peptides under ~10 kDa lost. 100–1,000 cells give 500–1,000 proteins in a published low-input workflow [45] |
| **Metabolome** | Did metabolic and lipid state change? | Supernatant of the same extraction | Bulk, same lane | ~10⁴ cells per arm. Quench too slow for ATP and glycolytic intermediates (~1 s turnover) |
| **Secretome + exometabolome** | What crossed between the cells: the interaction itself | Repeated supernatant draws, lane-of-origin pulse label (bioorthogonal amino-acid labelling of secreted proteins in iPSC astrocytes, in serum [46]) | Bulk, time series | Soluble signals only |

The proteome and metabolome are quenched in place, so those two layers avoid the dissociation step that can alter microglial expression [33]. "One sample population" across all arms holds only on the target-scale chip with replicated fields per lane (`chip/module1_target_v1.json`). On the 30 × 20 mm demo chips the arms are **parallel samples within one experiment**.

---

## 6. Novelty: what is claimed and what is not

**Claimed.** Each claim is bounded by the literature we searched (searches listed in `references.md`):

1. **Named-TF binding in human microglia under a defined neuronal and astrocytic context.** We found no study that measures the binding of a named transcription factor (bulk or single cell) in microglia while they are exposed to human neurons or astrocytes. Published named-TF binding in human microglia is from ex vivo primary cells [1]. Single-cell named-TF profiling has been shown for other factors and tissues [38, 39].
2. **Four molecular layers plus a secretome time series from one culture history per lane.** In the studies we found, risk-stratified iPSC microglia were measured in monoculture [21]. Human tri-cultures reported RNA and targeted proteins [28, 29]. Astrocyte resources reported RNA and proteome [36]. We found none that combines TF binding, transcriptome, proteome and metabolome around a controlled interaction.
3. **Controlled onset and direction of human neuro-immune interaction, with a vehicle arm and a sampled "before".** Compartmentalised microfluidic tri-culture exists [34]. What is new here is pairing isolation, then a defined, one-direction exposure, with destructive multi-omic endpoints on the same lanes.
4. **Capsule-based single-cell TF binding with in-droplet clonal barcoding.** Capsule processing [42, 43] and single-copy barcodes amplified in droplets [44] are each published. Their combination is not, per the design notes.

**Not claimed:** tri-culture itself [28, 29, 34, 35], iPSC microglia [13, 47], TIP-seq [39], semipermeable capsules [42, 43], low-input proteomics or metabolomics [45, 48], or multi-omics as such [49].

---

## 7. Why now

1. **Genetics has named the factors.** 75 loci [3]; SPI1/PU.1 and MEF2 at the centre of the heritability [5–7]; MITF nominated as an upstream regulator of a disease-associated, risk-enriched microglial subtype across 1,607 donors [32], and shown to drive a DAM-like state in iPSC microglia [47]. The hypotheses are now specific enough to test with a named-TF readout.
2. **Risk-stratified human cell collections exist, and they show that context is the bottleneck.** In 51 iPSC microglia lines from polygenic-risk extremes, risk effects on ATP production (−13%), IL-6 (−42%) and TNF (−38.5%) appeared only under LPS challenge. The bulk proteome barely separated the groups, and the authors cite monoculture as a limitation [21]\*. Across 31 donors xenografted into mouse brain, risk-linked variation in MHC-II appeared in vivo [22]\*. The lines are in hand; what is missing is a human context and readouts that capture it.
3. **Engaging a microglial target is not enough evidence.** The TREM2-agonist antibody AL002 failed its phase 2 primary endpoint (Nov 2024) "despite evidence of sustained target engagement and microglia activation" [50]. Whatever the reasons, this shows the field needs readouts of *which* microglial state a drug produces and what that state does to neighbouring cells, not only of target engagement. This is our interpretation, not a finding of the trial.
4. **The measurement tools have only recently become available.**
   - Single-cell named-TF binding with ~10-fold more unique reads per cell than earlier methods (2021) [39].
   - Semipermeable capsules for multistep single-cell assays (2022), now in *Science* (2026) [42, 43].
   - Low-input proteomics (2020) [45] and metabolomics (2017) [48].
   - Bioorthogonal secretome capture from
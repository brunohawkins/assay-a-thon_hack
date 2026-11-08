"""Build autochip/examples/spc_methods_paper.pdf: a short, generic methods paper (placeholder values). Needs reportlab.

    python3 autochip/examples/make_example_paper.py autochip/examples/spc_methods_paper.pdf
"""
import sys
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

out = sys.argv[1]
ss = getSampleStyleSheet()
title = ParagraphStyle("t", parent=ss["Title"], fontSize=15, leading=19)
auth = ParagraphStyle("a", parent=ss["Normal"], alignment=1, fontSize=9.5, leading=12)
h1 = ParagraphStyle("h1", parent=ss["Heading2"], fontSize=12, spaceBefore=10, spaceAfter=4)
h2 = ParagraphStyle("h2", parent=ss["Heading3"], fontSize=10.5, spaceBefore=6, spaceAfter=2)
body = ParagraphStyle("b", parent=ss["Normal"], fontSize=9.5, leading=12.5, spaceAfter=6)
abst = ParagraphStyle("ab", parent=body, leftIndent=8 * mm, rightIndent=8 * mm)

S = []
S += [Paragraph("Processing single cells inside semi-permeable capsules: a generic microfluidic workflow", title),
      Paragraph("A. Example and B. Placeholder (illustrative document, not a real publication)", auth),
      Spacer(1, 8)]
S += [Paragraph("Abstract", h1),
      Paragraph("Semi-permeable capsules (SPCs) let small molecules and enzymes reach cells and their nucleic "
                "acids while keeping large products inside, so many single cells can be processed in bulk. "
                "Here we describe a generic workflow, paraphrased from published SPC methods, that takes adherent "
                "cells through dissociation, encapsulation, photocuring, lysis, reverse transcription and index "
                "PCR to a sequencing-ready library. All values are placeholders for illustration, not a validated recipe.", abst)]
S += [Paragraph("1. Introduction", h1),
      Paragraph("Droplet microfluidics compartmentalises single cells, but each droplet supports only one reaction "
                "mix. Capsules with a hydrogel shell can be washed and exchanged into new reagents many times "
                "(Leonaviciene &amp; Mazutis, Nucleic Acids Research 2023; Baronas et al., Science 2026). This note "
                "restates the workflow in a generic form for use as a test document.", body)]
S += [Paragraph("2. Methods", h1),
      Paragraph("2.1 Cell preparation", h2),
      Paragraph("Adherent cells were cultured in a culture vessel at 37 °C with 5% CO2 until confluent (placeholder: "
                "3 days, medium changed daily). The medium was removed and the cells were rinsed once with buffer "
                "(200 µL). Dissociation enzyme (100 µL) was added and the cells were incubated for 5 min at 37 °C "
                "to detach them. Quench medium (200 µL) was added and mixed gently to stop the enzyme. The cells "
                "were centrifuged at 300 g for 5 min and the supernatant was removed. The pellet was resuspended "
                "in wash buffer (500 µL) and the wash was repeated once. The suspension was passed through a 40 µm "
                "cell strainer. Cells were counted and viability was checked. Cells were resuspended in core "
                "solution at the loading density (placeholder: 2 x 10^6 cells/mL).", body),
      Paragraph("2.2 Capsule generation", h2),
      Paragraph("The cell/core solution was co-flowed with the shell polymer solution (kept warm) and focused "
                "with fluorinated oil containing surfactant in a flow-focusing junction to make droplets of about "
                "70 µm (placeholder flows: core 2 µL/min, shell 2 µL/min, oil 10 µL/min). The emulsion was "
                "collected and kept on ice for 15-30 min so that the shell gelled. The emulsion was broken by adding "
                "emulsion-breaking agent and the oil phase was removed. Capsule "
                "shells were photocured with 405 nm light (placeholder: 30 s) in the presence of photoinitiator. "
                "Capsules were washed three times in buffer to remove oil and free reagents and filtered through a "
                "100 µm strainer to remove aggregates.", body),
      Paragraph("2.3 Processing inside capsules", h2),
      Paragraph("The buffer was exchanged for lysis buffer and the capsules were incubated (placeholder time and "
                "temperature). Capsules were washed three times in wash buffer. The buffer was then exchanged for "
                "reverse transcription mix and the capsules were incubated (placeholder time and temperature), followed "
                "by two washes. The capsules were split into four equal portions, and index PCR was run on each "
                "portion with a different index primer mix (placeholder number of cycles). The cDNA was released by "
                "digesting the capsule shells with a shell-digesting enzyme (placeholder time and temperature).", body),
      Paragraph("2.4 Library clean-up and quality control", h2),
      Paragraph("The library was bound to magnetic beads, washed twice with ethanol wash solution on a magnet and eluted in "
                "water. Library concentration and fragment size were measured.", body)]
S += [Paragraph("3. Results", h1),
      Paragraph("This is an illustrative document and reports no experimental results. A typical run is expected to "
                "give capsules of about 70 µm with one cell or none per capsule.", body),
      Paragraph("References", h1),
      Paragraph("1. Leonaviciene G., Mazutis L. RNA cytometry of single cells using semi-permeable microcapsules. "
                "Nucleic Acids Research (2023).", body),
      Paragraph("2. Baronas et al. Science (2026).", body)]

doc = SimpleDocTemplate(out, pagesize=A4, leftMargin=22 * mm, rightMargin=22 * mm, topMargin=20 * mm, bottomMargin=20 * mm,
                        title="Processing single cells inside semi-permeable capsules", author="A. Example")
doc.build(S)
print("wrote", out)

# assay2chip fixtures

Test inputs for the pipeline, starting at S0. **Everything in here is synthetic**: written
for this repo in generic, published-style method language, with placeholder reagent names
(`wash_buffer`, `lysis_buffer`, `medium`, `capture_beads`, `oil_phase`) and invented round
numbers. Nothing is copied from any lab's records or from a paper, so these files are safe
to commit under [CONFIDENTIALITY.md](../../CONFIDENTIALITY.md).

Ingest any of them with:

```bash
python assay2chip/stages/s0_ingest.py --paper assay2chip/fixtures/<file> --id <id> --check
```

| File | What it is | What it exercises |
|---|---|---|
| `bead_capture_assay.md` | A clean protocol: abstract, materials list, one 8-step method table, operating notes | the happy path - headings, lists, a GFM table parsed into `rows` + `columns` with nothing flagged |
| `messy_wash_and_lyse_assay.md` | The same kind of document after it has been through a PDF and a copy-paste | irregular numbering (`1.`, `2)`, `Step 3 -`, `3b.`, `5`, then `4)`), a step split across two sentences and two blocks, a volume that exists **only** in a table footnote, a pipe table with no delimiter row, a whitespace-aligned table, a table inside a fenced block, and two values the source never gives |
| `pdf_methods_excerpt.pdf` | A two-page PDF built by `make_pdf_fixture.py` | the PDF path: real page numbers, a whitespace-aligned step table reconstructed from the text layer, and a footnote carrying the volume one cell omits |

`make_pdf_fixture.py` regenerates the PDF with the standard library only and writes no
creation date, so the PDF - and therefore S0's output from it - is byte-identical on every
run.

Page numbers: markdown and text have none, so S0 defaults to page 1 and honours
`<!-- page N -->` on its own line (and a form feed) as a page marker. Both markdown
fixtures use it, because every later stage has to cite a page.

## What a fixture is for

S0 must not clean these up. The messy fixture is only useful if the irregular numbering,
the footnote and the missing volumes survive into `raw.json` - flagged with
`needs_attention` and a `note`, never quietly repaired and never guessed. S1 is where the
irregularities become `confidence` and `unresolved`.

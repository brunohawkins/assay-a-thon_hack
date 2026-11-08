"""The S12 report: `report.json` (the data) and `report.md` (the same data, read by people).

    from assay2chip import report

    data = report.collect("my_assay")        # every stage's output, gathered and counted
    text = report.render(data)               # report.md

Three modules, one job each:

  `findings.py`  the finding shape S9 and S10 both produce, and the reader for their files.
  `model.py`     gathers the stage files into report.json. No formatting, no prose.
  `markdown.py`  renders report.json as report.md. No gathering, no counting.

The split exists because the two outputs must agree. Every number in report.md comes out
of report.json, so a reader who checks the JSON and a reader who reads the markdown can
never be told different things.

The report is deliberately SHORT: tables, not prose, and an HONEST LIMITS section that is
always there. See `assay2chip/report/README.md` for what belongs in it and what does not.
"""

from __future__ import annotations

from . import findings, markdown, model

collect = model.collect
render = markdown.render

__all__ = ["collect", "render", "findings", "markdown", "model"]

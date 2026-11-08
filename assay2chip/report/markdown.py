"""Render report.json as report.md. Formatting only - every number comes from the JSON.

SHORT, and tables rather than prose: the project owner has asked for that repeatedly, and
`protocols/pipeline_status.md` is the house style - one sentence of context, then tables,
then blunt notes. Nothing is computed here. If a number is not in report.json it does not
belong in report.md either, because the two must never be able to disagree.

Long tables are capped (`CAPS`) and say so in a footnote naming report.json, so the
document stays readable without hiding anything.
"""

from __future__ import annotations

from typing import Any, Sequence

from . import findings as F

# How many rows each table shows before it points at report.json for the rest.
CAPS = {"steps": 40, "unresolved": 12, "findings": 15, "fixes": 12, "verify": 10}

WHERE_LABEL = {
    "chip": "on chip, in a part of its own",
    "op": "on chip, as a protocol action through parts already there",
    "off_chip": "off chip: tube, spin or instrument",
    "missing": "nowhere: no part exists, or no unit op covers the step",
}


def render(report: dict) -> str:
    """report.md, start to finish."""
    out: list[str] = []
    head = report["headline"]
    out.append(f"# {report['id']}: assay to chip")
    out.append("")
    out.append(_verdict(report))
    out.append("")
    out += _headline(report, head)
    out += _chips(report)
    out += _placement(report, head)
    out += _handoffs(report)
    out += _unresolved(report)
    out += _cfd(report)
    out += _verify(report)
    out += _fabrication(report)
    out += _limits(report)
    return "\n".join(out).rstrip() + "\n"


# --- sections ------------------------------------------------------------------------


def _verdict(report: dict) -> str:
    head = report["headline"]
    bits = [f"{head['chips']} chip(s)"]
    if head["on_chip_fraction"] is not None:
        bits.append(f"{head['steps_on_chip']} of {head['assay_steps']} assay steps on chip "
                    f"({head['on_chip_fraction']:.0%})")
    bits.append(f"{head['handoffs_not_automated']} of {head['handoffs']} hand-off(s) need a human")
    verify = report["verify"].get("ok")
    bits.append("S9 verify PASSED" if verify else
                ("S9 verify FAILED" if verify is False else "S9 verify has not run"))
    line = "; ".join(bits) + "."
    if report["missing"]:
        line += f" {len(report['missing'])} stage output(s) are not there yet (listed below)."
    if report["llm"]["stubbed_stages"]:
        line += f" **{report['llm']['note']}**"
    return line


def _headline(report: dict, head: dict) -> list[str]:
    cfd = head["cfd_by_severity"]
    rows = [
        ("Chips", str(head["chips"])),
        ("Assay steps", f"{head['assay_steps']} -> {head['unit_ops']} unit op(s)"),
        ("On chip", f"{head['steps_on_chip']} / {head['assay_steps']} steps"
                    + (f" ({head['on_chip_fraction']:.0%})"
                       if head["on_chip_fraction"] is not None else "")),
        ("**Hand-offs needing a human**",
         f"**{head['handoffs_not_automated']} of {head['handoffs']}**"
         + ("  - the stated goal is none" if head["handoffs_not_automated"] else "")),
        ("Unresolved questions (S1)", str(head["unresolved_from_s1"])
         + (f", {head['unresolved_total']} across all stages"
            if head["unresolved_total"] != head["unresolved_from_s1"] else "")),
        ("CFD findings",
         ", ".join(f"{n} {s}" for s, n in cfd.items() if n) or "none"),
        ("CFD fixes", f"{head['cfd_fixes_applied']} applied, "
                      f"{head['cfd_fixes_proposed']} proposed (for a human)"),
        ("S9 verify", _ok(head["verify_ok"])
         + (f", {head['verify_blockers']} blocker(s)" if head["verify_blockers"] else "")),
        ("Print bundles ready", f"{head['bundles_ready']} of {head['chips']}"),
    ]
    return ["## Headline", "", *_table(["", ""], rows), ""]


def _chips(report: dict) -> list[str]:
    if not report["chips"]:
        return []
    rows = [
        (
            f"`{c['chip_id']}`",
            ", ".join(str(o) for o in c["ops"]) or "-",
            str(c["scale"] or "-"),
            f"{c['ports']} ports" if c["ports"] is not None else "-",
            "OK" if c["verify_blockers"] == 0 else f"{c['verify_blockers']} blocker(s)",
            c["cfd_worst"] or "-",
            "ready" if c["bundle"]["present"] else "not built",
        )
        for c in report["chips"]
    ]
    return ["## Chips", "",
            *_table(["Chip", "Ops", "Scale", "Ports", "S9", "CFD worst", "Print"], rows),
            ""]


def _placement(report: dict, head: dict) -> list[str]:
    if not report["steps"]:
        return ["## Where the assay ended up", "",
                "S1 has not run, so there are no assay steps to place.", ""]
    counts = head["steps_by_where"]
    summary = [(f"`{where}`", str(counts[where]), WHERE_LABEL[where])
               for where in counts]
    rows = [
        (
            str(s["step"]),
            s["action"] or "-",
            ", ".join(str(o) for o in s["ops"]) or "-",
            f"`{s['where']}`",
            f"`{s['chip']}`" if s["chip"] else "-",
            _clip(s["why"], 70),
        )
        for s in report["steps"][:CAPS["steps"]]
    ]
    out = ["## Where the assay ended up", "",
           *_table(["Outcome", "Steps", "Meaning"], summary), ""]
    unverified = head.get("steps_unverified_against_a_spec") or 0
    if unverified:
        out += [f"{unverified} row(s) could not be checked against a built chip spec "
                f"(S6/S7 have not written one), so `chip` there is S3's claim, not a "
                f"drawn part.", ""]
    out += _table(["Step", "Action", "Op", "Where", "Chip", "Why"], rows)
    out += _more(len(report["steps"]), CAPS["steps"], "steps")
    return out + [""]


def _handoffs(report: dict) -> list[str]:
    if not report["handoffs"]:
        return []
    rows = [
        (
            f"`{h['from']}` -> `{h['to']}`",
            h["carries"] or "-",
            _num(h["volume_ul"], "uL"),
            _num(h["particle_um"], "um"),
            "yes" if h["automated"] else "**NO**",
            _clip(h["how"], 48),
            _clip("; ".join(h["risks"]), 48) or "-",
        )
        for h in report["handoffs"]
    ]
    out = ["## Hand-offs", "",
           *_table(["From -> To", "Carries", "Volume", "Particle", "Automated", "How",
                    "Risks"], rows)]
    manual = [h for h in report["handoffs"] if not h["automated"]]
    if manual:
        out += ["", f"{len(manual)} hand-off(s) need a person. 'No human intervention' is "
                    f"the stated goal, so each one is a gap, not a detail."]
    if report["cuts"]:
        out += ["", *_table(["Cut between ops", "Forced by", "Why"],
                            [(" / ".join(str(x) for x in c["between"]),
                              c.get("forced_by", "-"), _clip(c["why"], 80))
                             for c in report["cuts"]])]
    return out + [""]


def _unresolved(report: dict) -> list[str]:
    everything = [
        (stage, item) for stage, items in report["unresolved"].items() for item in items
    ]
    if not everything:
        return ["## Unresolved questions", "",
                "None recorded. That is either true or a bug in S1: a source that states "
                "every volume, time and temperature is rare.", ""]
    rows = [
        (
            stage.upper().replace("_", "/"),
            _locator(item),
            _clip(item.get("question", ""), 84),
            _clip(str(item.get("assumed")), 24) if item.get("assumed") is not None else "-",
            _clip(item.get("assumed_from", ""), 36) or "-",
        )
        for stage, item in everything[:CAPS["unresolved"]]
    ]
    out = ["## Unresolved questions", "",
           "What the source never said. Nothing below was filled in.", "",
           *_table(["From", "Where", "Question", "Value used", "From contract"], rows)]
    out += _more(len(everything), CAPS["unresolved"], "questions")
    return out + [""]


def _cfd(report: dict) -> list[str]:
    cfd = report["cfd"]
    if not report["stages_present"].get("s10_cfd"):
        return ["## Flow simulation (S10)", "",
                "**S10 did not run for this id**, so there is no flow result here. An "
                "empty CFD table is not a clean simulation.", ""]
    rows = _finding_rows(cfd["findings"], CAPS["findings"])
    out = ["## Flow simulation (S10)", "",
           ", ".join(f"{n} {s}" for s, n in cfd["by_severity"].items() if n)
           or "No findings.", ""]
    if rows:
        out += _table(["Severity", "Chip", "Feature", "Finding", "Numbers", "Fix"], rows)
        out += _more(len(cfd["findings"]), CAPS["findings"], "findings")
        out += [""]
    applied, proposed = cfd["fixes"]["applied"], cfd["fixes"]["proposed"]
    if applied or proposed:
        rows = [
            ("applied" if fix.get("applied") else "proposed",
             f"`{fix.get('chip') or '-'}`",
             _clip(fix.get("what", ""), 72),
             _clip(str(fix.get("before")), 22) if fix.get("before") is not None else "-",
             _clip(str(fix.get("predicted")), 22) if fix.get("predicted") is not None else "-")
            for fix in (applied + proposed)[:CAPS["fixes"]]
        ]
        out += _table(["Status", "Chip", "Change", "Before", "Predicted"], rows)
        out += _more(len(applied) + len(proposed), CAPS["fixes"], "fixes")
        out += ["", f"{len(applied)} fix(es) were applied automatically (the re-run showed "
                    f"the finding gone and no new blocker); {len(proposed)} need a human."]
    return out + [""]


def _verify(report: dict) -> list[str]:
    verify = report["verify"]
    if verify.get("ok") is None:
        return ["## Checks (S9)", "", "**S9 did not run for this id.**", ""]
    blockers = F.blockers(verify["findings"])
    counts = (verify.get("counts") or {})
    out = ["## Checks (S9)", "",
           f"{_ok(verify['ok'])}. {(verify.get('verdict') or {}).get('reason', '')}".strip(),
           ""]
    pipeline = counts.get("pipeline") or {}
    preexisting = counts.get("preexisting") or {}
    out += _table(
        ["Scope", "blocker", "high", "medium", "low"],
        [("the pipeline's own chips", *[str(pipeline.get(s, 0)) for s in
                                       ("blocker", "high", "medium", "low")]),
         ("pre-existing, on main's hand-made chips",
          *[str(preexisting.get(s, 0)) for s in ("blocker", "high", "medium", "low")])])
    if blockers:
        out += ["", *_table(["Severity", "Chip", "Feature", "Finding", "Numbers", "Fix"],
                            _finding_rows(blockers, CAPS["verify"]))]
        out += _more(len(blockers), CAPS["verify"], "blockers")
    for problem in verify.get("prerequisites_missing", [])[:5]:
        out += ["", f"- MISSING: {problem}"]
    return out + [""]


def _fabrication(report: dict) -> list[str]:
    if not report["fabrication"]:
        return []
    rows = [
        (f"`{b['chip_id']}`", f"`{b['path']}`",
         "ready" if b["present"] else "**not built**",
         f"{len(b['files'])} file(s): " + ", ".join(b["kinds"]) if b["files"] else "-")
        for b in report["fabrication"]
    ]
    return ["## Fabrication bundle", "",
            *_table(["Chip", "Path", "State", "Contents"], rows), ""]


def _limits(report: dict) -> list[str]:
    out = ["## Honest limits", ""]
    out += [f"- {line}" for line in report["limits"]]
    if report["missing"]:
        out += ["", "Not run for this id:"]
        out += [f"- {line}" for line in report["missing"]]
    return out + [""]


# --- helpers -------------------------------------------------------------------------


def _finding_rows(rows: Sequence[dict], cap: int) -> list[tuple[str, ...]]:
    return [
        (
            row.get("severity", "-"),
            f"`{row['chip']}`" if row.get("chip") else "-",
            f"`{row['feature']}`" if row.get("feature") else "-",
            _clip(row.get("what") or row.get("rule", ""), 72),
            F.one_line(row),
            _fix_cell(row.get("fix")),
        )
        for row in sorted(rows, key=lambda r: F.SEVERITIES.index(
            r.get("severity") if r.get("severity") in F.SEVERITIES else "info"))[:cap]
    ]


def _fix_cell(fix: dict | None) -> str:
    if not fix:
        return "-"
    state = {True: "applied", False: "rejected", None: "proposed"}[fix.get("applied")]
    return f"{state}: {_clip(fix.get('what', ''), 48)}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """A GitHub-flavoured markdown table. No padding: the renderer aligns it, we do not."""
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        cells = [str(c).replace("|", "\\|").replace("\n", " ") for c in row]
        cells += [""] * (len(headers) - len(cells))
        out.append("| " + " | ".join(cells[:len(headers)]) + " |")
    return out


def _more(total: int, cap: int, what: str) -> list[str]:
    if total <= cap:
        return []
    return ["", f"_{total - cap} more {what} in report.json._"]


def _clip(text: Any, width: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[: width - 1] + "\u2026"


def _locator(item: dict) -> str:
    """Where an unresolved question belongs: a step, an op, a chip or a field."""
    bits = [f"step {item['step']}" if item.get("step") else "",
            f"op {item['op']}" if item.get("op") else "",
            f"`{item['chip_id']}`" if item.get("chip_id") else "",
            f"`{item['field']}`" if item.get("field") else ""]
    return ", ".join(b for b in bits if b) or "-"


def _num(value: Any, units: str) -> str:
    if value is None:
        return "not stated"
    return f"{value:g} {units}" if isinstance(value, (int, float)) else f"{value} {units}"


def _ok(value: Any) -> str:
    return {True: "PASSED", False: "FAILED", None: "did not run"}[value]

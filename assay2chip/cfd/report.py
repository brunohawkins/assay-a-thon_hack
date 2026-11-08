"""The S10 CFD report: what the simulation found, what was changed, what to distrust.

    from assay2chip.cfd import report
    text = report.render(result)            # the dict assay2chip/cfd/run.py builds
    report.write(result, path)

Short on purpose. The owner reads tables, not prose, so every section is a table with
one line of context above it. The order is fixed because it is the order the numbers
stop being trustworthy in:

  1  VERDICT          and, when validation did not pass, a banner saying every finding
                      below is UNTRUSTED. A solver nobody has checked is worse than no
                      solver (cfd/README.md), so an unvalidated run says so first.
  2  WHAT RAN         which cfd modules exist; NOT YET AVAILABLE is a result, not a gap
                      to paper over.
  3  FINDINGS         by severity, with the two numbers that disagree.
  4  FIXES            each with before and after, and whether it was applied or is a
                      proposal for a human.
  5  HANDOFFS         the chip-to-chip joins, which is the half of the pipeline that no
                      single-chip simulation can see.
  6  VALIDATION       every benchmark, pass or fail.
  7  HONEST LIMITS    where the model does not apply and every flagged assumption,
                      named, with the contract key it came from.
  8  3D RESULTS       a clearly labelled slot for `assay2chip/foam/` (OpenFOAM), which
                      is built in parallel and NOT integrated yet. Section 6 keeps
                      saying this is a depth-averaged model, so the reader has to be
                      told plainly whether a 3D check exists. See FOAM_SECTION.

`render` never raises on a partial result: a missing section becomes a line saying it is
missing. Standard library only.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SEVERITY_ORDER = ["blocker", "high", "medium", "low", "info"]
OK_STATUS = ("ok", "pass", "passed", "available")


def _f(value, default=None):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if out != out else out


def trim(text):
    """Drop trailing zeros only AFTER a decimal point.

    A bare `.rstrip("0")` turns 140 into 14 and 1200 into 12, which is how a 140 um
    capsule was reported as 14 um against a 100 um gap printed as 1 um. The guard is the
    whole point of this function existing.
    """
    return (text.rstrip("0").rstrip(".") or "0") if "." in text else text


def sig(value, digits=3):
    x = _f(value)
    if x is None:
        return str(value) if value not in (None, "") else "-"
    if x == 0:
        return "0"
    if abs(x) >= 1e5 or abs(x) < 1e-3:
        return f"{x:.2e}"
    places = max(0, digits - 1 - int(math.floor(math.log10(abs(x)))))
    return trim(f"{x:.{places}f}")


def cell(value, width=None):
    """A markdown table cell: no pipes, no newlines, nothing that breaks the row."""
    text = "-" if value in (None, "") else str(value)
    text = text.replace("|", "/").replace("\n", " ").strip()
    if width and len(text) > width:
        text = text[: width - 1].rstrip() + "…"
    return text or "-"


def table(headers, rows, empty="_none_"):
    if not rows:
        return [empty, ""]
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(cell(v) for v in row) + " |")
    out.append("")
    return out


def _counts(findings):
    out = {}
    for f in findings or []:
        key = str(f.get("severity") or "info").lower()
        out[key] = out.get(key, 0) + 1
    return out


def _severity_line(findings):
    counts = _counts(findings)
    bits = [f"{counts[s]} {s}" for s in SEVERITY_ORDER if counts.get(s)]
    return ", ".join(bits) if bits else "no findings"


def validation_verdict(result):
    """(verdict, trusted, detail). `trusted` false means every finding is untrusted.

    `validate.py` is the authority: when it states `trusted` (or `passed`/`ok`), that is
    the answer, because it gates on every benchmark having RUN as well as passed - a
    gating benchmark that never appeared is as bad as one that failed. The checks are
    only used to name what went wrong.
    """
    validation = (result or {}).get("validation")
    if not isinstance(validation, dict):
        validation = {"status": "missing"} if validation is None else {
            "status": "unreadable",
            "reason": f"validation is a {type(validation).__name__}, not a report"}
    status = str(validation.get("status") or "missing").lower()
    checks = [c for c in (validation.get("checks") or []) if isinstance(c, dict)]
    failed = [c for c in checks if not c.get("ok")]
    reason = (validation.get("reason") or validation.get("untrusted_reason")
              or validation.get("detail") or "")
    stated = next((validation[key] for key in ("trusted", "passed", "ok")
                   if key in validation), None)
    trusted = bool(stated) if stated is not None else (
        status in OK_STATUS and bool(checks) and not failed)
    if trusted:
        counts = validation.get("counts") or {}
        ran = counts.get("pass", len(checks))
        return "VALIDATED", True, f"{ran} of {len(checks) or ran} benchmark(s) passed"
    if status in ("missing", "not yet available", "not_yet_available", ""):
        return ("NOT VALIDATED", False,
                "assay2chip/cfd/validate.py is NOT YET AVAILABLE, so nothing has "
                "checked the solver")
    if failed:
        return ("VALIDATION FAILED", False,
                reason or (f"{len(failed)} of {len(checks)} benchmark(s) failed: "
                           + "; ".join(cell(c.get("name"), 40) for c in failed[:4])))
    return (f"NOT VALIDATED ({status})", False,
            reason or "the benchmarks did not all run, so nothing has been checked")


# --- the sections ----------------------------------------------------------------------

def _header(result, lines):
    run = result.get("run") or "cfd"
    lines += [f"# S10 CFD: {cell(run)}", ""]
    lines += [f"_{cell(result.get('title') or result.get('mode') or '')}_", ""] \
        if result.get("title") or result.get("mode") else []
    verdict, trusted, detail = validation_verdict(result)
    findings = result.get("findings") or []
    if not trusted:
        lines += [
            f"> ## {verdict} - ALL FINDINGS BELOW ARE UNTRUSTED",
            f"> {str(detail).rstrip('. ')}. Until the benchmarks in `cfd/README.md` pass "
            f"(analytic duct resistance, series and parallel networks, agreement with "
            f"`operator/network.py`, mass conservation) every number in this report is "
            f"the output of an unchecked solver. Read the findings as *where to look*, "
            f"not as results.",
            "",
        ]
    else:
        lines += [f"**{verdict}** - {detail}.", ""]
    lines += table(
        ["", "value"],
        [["mode", result.get("mode")],
         ["generated", result.get("generated_at")],
         ["chips simulated", len(result.get("chips") or [])],
         ["protocol steps simulated", sum(len(c.get("steps") or [])
                                          for c in result.get("chips") or [])],
         ["findings", _severity_line(findings)],
         ["fixes proposed / applied",
          f"{len(result.get('fixes') or [])} / "
          f"{sum(1 for x in result.get('fixes') or [] if x.get('applied'))}"],
         ["fix loops run", result.get("loops")],
         ["figures", len(result.get("figures") or [])],
         ["geometry re-run requested",
          (result.get("rerun") or {}).get("rerun_from") or "no"]],
    )


def _what_ran(result, lines):
    lines += ["## 1. What ran", "",
              "Every `assay2chip/cfd/` module this run looked for. A module that does "
              "not exist yet is skipped and named here; it is never silently replaced "
              "by a guess.", ""]
    rows = []
    for name, info in (result.get("modules") or {}).items():
        status = str(info.get("status") or "?")
        rows.append([f"`{name}.py`", status.upper() if status != "ok" else "ok",
                     cell(info.get("detail"), 86)])
    lines += table(["module", "status", "detail"], rows)
    chips = result.get("chips") or []
    if chips:
        rows = []
        for chip in chips:
            rows.append([
                f"`{chip.get('chip_id')}`",
                chip.get("module") or "-",
                chip.get("scale") or "-",
                f"{(chip.get('mesh') or {}).get('nodes', '-')} / "
                f"{(chip.get('mesh') or {}).get('edges', '-')}",
                len(chip.get("steps") or []),
                _severity_line(chip.get("findings")),
            ])
        lines += ["### Chips", ""]
        lines += table(["chip", "module", "scale", "cells / edges", "steps",
                        "findings"], rows)


def _findings(result, lines):
    findings = result.get("findings") or []
    lines += ["## 2. Findings", ""]
    if not findings:
        lines += ["No findings. That is only meaningful once `analyse.py` exists and "
                  "the validation above passes - see section 1.", ""]
        return
    counts = _counts(findings)
    lines += table(["severity", "count", "what it means"],
                   [[s, counts.get(s, 0), _SEVERITY_MEANING[s]]
                    for s in SEVERITY_ORDER if counts.get(s)])
    for severity in SEVERITY_ORDER:
        group = [f for f in findings
                 if str(f.get("severity") or "info").lower() == severity]
        if not group:
            continue
        lines += [f"### {severity} ({len(group)})", ""]
        rows = []
        for f in group:
            rows.append([
                f"`{cell(f.get('id'), 48)}`",      # the id has to be usable to grep
                cell(f.get("class")),
                cell(f.get("chip_id"), 22),
                cell(f.get("feature_id"), 22),
                cell(f.get("step")),
                cell(f.get("metric"), 22),
                f"{sig(f.get('value'))} vs {sig(f.get('limit'))} "
                f"{cell(f.get('units'), 12) if f.get('units') else ''}".strip(),
                cell(f.get("message") or f.get("fix_hint"), 90),
            ])
        lines += table(["id", "class", "chip", "feature", "step", "metric",
                        "value vs limit", "message"], rows)


_SEVERITY_MEANING = {
    "blocker": "the protocol cannot run as drawn",
    "high": "the step runs but the result is not the intended one",
    "medium": "a real risk with a known fix",
    "low": "worth knowing, not worth re-cutting a chip for",
    "info": "reported for completeness",
}


def _fixes(result, lines):
    fixes = result.get("fixes") or []
    lines += ["## 3. Fixes, before and after", "",
              "A fix is applied automatically only if it keeps every fabrication floor, "
              "introduces no channel crossing, and the re-run shows the finding gone "
              "with no new blocker. Everything else is a proposal for a human. "
              "A hand-designed chip in `chip/` is never modified: in pipeline mode a "
              "fix becomes a `rerun_from` request to S6/S7, and standalone it is a "
              "patched copy in the work directory.", ""]
    if not fixes:
        lines += ["_no fixes proposed_", ""]
        return
    applied = [f for f in fixes if f.get("applied")]
    lines += [f"{len(fixes)} candidate(s) across {len(_by_finding(fixes))} finding(s); "
              f"{len(applied)} applied. Every candidate, with its gates, is in "
              f"`cfd/fixes.json`.", ""]

    lines += ["### Applied", ""]
    lines += table(
        ["finding", "action", "target", "metric", "before", "after", "verified after"],
        [[f"`{cell(f.get('finding_id'), 44)}`", cell(f.get("action"), 26),
          cell(f.get("target"), 22),
          cell((f.get("predicted") or {}).get("metric"), 24),
          sig((f.get("predicted") or {}).get("before")),
          sig((f.get("predicted") or {}).get("after")),
          sig(f.get("verified_after"))] for f in applied],
        empty="_none: every candidate below failed at least one gate and is a proposal._")

    # One row per FINDING, not per candidate: the fix module offers several routes to the
    # same finding and 123 near-duplicate rows is not a short document. The best
    # candidate and what stopped it is the decision a reader has to make.
    rows = []
    for finding_id, group in _by_finding(fixes).items():
        if all(f.get("applied") for f in group):
            continue
        best = min(group, key=_distance)
        predicted = best.get("predicted") or {}
        blocked = sorted({g for f in group for g in (f.get("blocked_by") or [])})
        rows.append([
            f"`{cell(finding_id, 44)}`",
            cell(best.get("action"), 26),
            cell(best.get("target"), 22),
            sig(predicted.get("before")),
            sig(predicted.get("after")),
            sig(predicted.get("limit")),
            len(group),
            ", ".join(blocked) or ("auto-applicable" if best.get("auto_applicable")
                                   else "not re-simulated"),
        ])
    lines += ["### Proposed for a human - best candidate per finding", ""]
    lines += table(["finding", "best action", "target", "before", "after", "limit",
                    "candidates", "blocked by"], rows)
    needs = sorted({str(f.get("needs")) for f in fixes if f.get("needs")})
    if needs:
        lines += [f"Blocked on a part or capability we do not have: "
                  f"{', '.join(needs)}.", ""]


def _by_finding(fixes):
    out = {}
    for fix in fixes:
        out.setdefault(str(fix.get("finding_id")), []).append(fix)
    return out


def _distance(fix):
    """How far the predicted result still is from the limit; unknown sorts last."""
    predicted = fix.get("predicted") or {}
    after, limit = _f(predicted.get("after")), _f(predicted.get("limit"))
    if after is None:
        return float("inf")
    if limit is None or limit == 0:
        return abs(after)
    return abs(after / limit)


def _handoffs(result, lines):
    handoffs = result.get("handoffs") or []
    coupling = result.get("coupling") or {}
    lines += ["## 4. Handoffs between chips", "",
              "The joins no single-chip solve can see: tubing, dead volume and the "
              "pressure budget across each handoff.", ""]
    if not handoffs:
        lines += ["_no handoffs in this run_", ""]
        return
    if not coupling:
        lines += ["`couple.py` produced nothing for this run, so the rows below are "
                  "only what the pipeline file DECLARES: no tubing, no dead volume and "
                  "no cross-chip pressure budget has been computed.", ""]
    rows = []
    for hand in handoffs:
        hid = str(hand.get("id") or "")
        extra = coupling.get(hid) or {}
        source = hand.get("from") or {}
        target = hand.get("to") or {}
        pressure = extra.get("pressure") or {}
        particle = _f(extra.get("particle_um"))
        passable = _f(extra.get("narrowest_passable_um"))
        fits = ("-" if particle is None or passable is None else
                (f"{sig(particle)} / {sig(passable)}"
                 + (" **FAILS**" if particle > passable else "")))
        dp = next((pressure.get(key) for key in
                   ("total_dp_kpa", "dp_kpa", "line_dp_kpa") if pressure.get(key)), None)
        if dp is None:
            dp = _f(pressure.get("line_dp_kpa"), 0.0) + \
                _f(pressure.get("downstream_chip_dp_kpa"), 0.0) or None
        rows.append([
            f"`{cell(hid, 26)}`",
            f"{cell(source.get('module') or source.get('roadmap'), 12)}."
            f"{cell(extra.get('from_port') or source.get('port') or source.get('ports'), 12)}",
            f"{cell(target.get('module') or target.get('roadmap'), 12)}."
            f"{cell(extra.get('to_port') or target.get('port') or target.get('ports'), 12)}",
            cell(extra.get("carries") or hand.get("carries")),
            str(extra.get("severity") or "-"),
            "yes" if extra.get("automated") else
            ("no" if "automated" in extra else "-"),
            fits,
            sig(extra.get("dead_volume_ul")),
            sig(dp),
            sig(pressure.get("limit_kpa")),
        ])
    lines += table(["handoff", "from", "to", "carries", "severity", "automated",
                    "particle / narrowest µm", "dead vol µL", "dP kPa", "limit kPa"],
                   rows)
    notes = []
    for hand in handoffs:
        hid = str(hand.get("id") or "")
        extra = coupling.get(hid) or {}
        warning = ((hand.get("connector") or {}).get("warning")
                   or extra.get("automated_why") if not extra.get("automated") else None)
        if str(extra.get("severity") or "") in ("blocker", "high") or warning:
            notes.append([f"`{cell(hid, 26)}`",
                          cell(extra.get("chain_note") or warning
                               or hand.get("note"), 150)])
    if notes:
        lines += ["Why the worst of them are flagged:", ""]
        lines += table(["handoff", "what the chain file and the coupling say"], notes)
    manual = [h for h in handoffs
              if (coupling.get(str(h.get("id"))) or {}).get("automated") is False]
    if manual:
        lines += [f"**{len(manual)} of {len(handoffs)} handoffs are NOT automated** - "
                  f"a human carries the sample. The stated goal is no human "
                  f"intervention, so these are counted here: "
                  f"{', '.join('`' + str(h.get('id')) + '`' for h in manual)}.", ""]


def _validation(result, lines):
    validation = result.get("validation")
    validation = validation if isinstance(validation, dict) else {}
    verdict, trusted, detail = validation_verdict(result)
    lines += ["## 5. Validation", "", f"**{verdict}** - {detail}.", ""]
    if validation.get("recommended_grid_mm"):
        lines += [f"Grid convergence recommends "
                  f"{sig(validation['recommended_grid_mm'])} mm cells. The grid each "
                  f"chip was actually meshed at is in section 1.", ""]
    checks = [c for c in (validation.get("checks") or []) if isinstance(c, dict)]
    if not checks:
        lines += ["_no benchmark results_. Required before any finding is trusted: "
                  "analytic single straight channel against "
                  "`R = 12 mu L / (w h^3 (1 - 0.63 h/w))`, series and parallel networks "
                  "against hand-computed resistances, agreement with "
                  "`operator/network.py` on Module 1 within a few percent, and mass "
                  "conservation at every node.", ""]
        return
    lines += table(["benchmark", "verdict", "expected", "got", "detail"],
                   [[cell(c.get("name"), 44), "pass" if c.get("ok") else "**FAIL**",
                     sig(c.get("expected")), sig(c.get("got")),
                     cell(c.get("detail"), 70)] for c in checks])


# The assumptions this model stands on that no contract covers, each named so a reader
# can decide whether to trust the number above it. The ones with a contract key are
# flagged from the contract's own `assumption` field, not from a literal here.
STANDING_LIMITS = [
    ("depth-averaged Hele-Shaw",
     "Every field is a 2D potential flow in plane, computed from the exact "
     "rectangular-duct resistance. It has NO recirculation eddies, so a closed-streamline "
     "trap test cannot fire and was not used: a trap here is a low-velocity or "
     "long-residence-time zone."),
    ("Re > 1",
     "Stokes flow is the correct physics below Re ~1, which the audit measured almost "
     "everywhere. Any edge above Re 1 is OUTSIDE the model and is reported as an "
     "`unmodelled` finding rather than as a number."),
    ("3D recirculation",
     "Real recirculation needs Re well above 1. Where this report says there is no eddy, "
     "that is a statement about the model, not a measurement of the chip."),
    ("tubing between chips",
     "Handoff lines are modelled as straight ducts of the stated bore and length. Real "
     "tubing has fittings, compliance and a bubble history that none of this captures; "
     "dead volume is the part that is computed."),
    ("densities and sedimentation",
     "Settling uses Stokes drag on a rigid sphere at an assumed particle-to-fluid density "
     "difference. Capsules are soft, nearly neutrally buoyant and swell, so a sediment "
     "finding is directional, not quantitative."),
    ("contact angles and priming",
     "Capillary pressure at a sudden expansion uses an assumed contact angle for an "
     "unmeasured surface. Every priming and bubble finding inherits that assumption."),
    ("cold-solvent viscosity",
     "Viscosity comes from the contract for 37 C steps and water at 20 C for the dye "
     "demo. A cold organic solvent or a viscous core buffer is NOT in the contract, so "
     "any step using one carries a flagged assumption instead of a silent water value."),
    ("one etch depth",
     "The solve assumes the single fluidic depth the spec declares. A feature drawn at a "
     "second depth is a fabrication problem before it is a flow problem."),
]


def _limits(result, lines):
    lines += ["## 6. Honest limits", "",
              "Where this model does not apply, and what it assumed. Read this before "
              "quoting any number above.", ""]
    lines += table(["limit", "what it means for the numbers above"],
                   [[name, text] for name, text in STANDING_LIMITS])

    unmodelled = result.get("unmodelled") or []
    if unmodelled:
        lines += ["### Edges outside the model in this run (Re > 1)", ""]
        lines += table(["chip", "edge", "step", "Re", "note"],
                       [[cell(u.get("chip_id"), 22), cell(u.get("edge") or
                                                          u.get("feature_id"), 24),
                         cell(u.get("step")), sig(u.get("re")), cell(u.get("note"), 60)]
                        for u in unmodelled[:30]])
        if len(unmodelled) > 30:
            lines += [f"_and {len(unmodelled) - 30} more_", ""]

    assumptions = result.get("assumptions") or []
    if assumptions:
        lines += ["### Flagged assumptions this run depended on", "",
                  "Every number here is a rule of thumb or a contract value marked "
                  "`assumption: true`. A finding that rests on one is not the same as "
                  "one that rests on a measurement.", ""]
        lines += table(["key or subject", "value", "units", "where it was used, and why",
                        "source"],
                       [[f"`{cell(a.get('key'), 44)}`", sig(a.get("value")),
                         cell(a.get("units"), 20), cell(a.get("used_for"), 150),
                         cell(a.get("source"), 50)]
                        for a in assumptions])

    gaps = result.get("gaps") or []
    if gaps:
        lines += ["### Gaps recorded rather than guessed", ""]
        lines += table(["subject", "what is missing"],
                       [[cell(g.get("subject"), 32), cell(g.get("question")
                                                          or g.get("note"), 110)]
                        for g in gaps])


#: Where the 3D results go. `assay2chip/foam/` (OpenFOAM) is being built in parallel with
#: its own CLI and renderer and is NOT integrated here yet. The section is written either
#: way, because the one thing section 6 keeps saying is that this is a depth-averaged
#: model - so a reader has to be told plainly whether a 3D check exists. When foam lands,
#: `result["foam"]` carries {status, cases: [...], figures: [...], disagreements: [...]}
#: and nothing in this report has to be restructured to hold it.
FOAM_SECTION = "## 8. 3D results (OpenFOAM)"


def _foam(result, lines):
    foam = result.get("foam")
    lines += [FOAM_SECTION, ""]
    if not isinstance(foam, dict) or not foam:
        lines += [
            "**NOT YET AVAILABLE.** Every number in this report comes from the "
            "depth-averaged 2D model in section 6. No 3D solve has been run, so nothing "
            "here has checked the places that model is known to be weakest: a region "
            "above Re 1 (section 6), real recirculation, a sudden expansion, or a "
            "chamber whose width is only a few times its depth.",
            "",
            "`assay2chip/foam/` is being built in parallel and has its own CLI and "
            "renderer. When it lands, its cases, its figures and - the useful part - "
            "anywhere it DISAGREES with the 2D result appear here.",
            "",
        ]
        return
    lines += [f"Status: **{cell(foam.get('status'))}**. "
              f"{cell(foam.get('note') or 'A 3D cross-check of the 2D findings above.')}",
              ""]
    cases = [c for c in (foam.get("cases") or []) if isinstance(c, dict)]
    if cases:
        lines += table(["case", "chip", "step", "what it checked", "verdict"],
                       [[cell(c.get("id"), 28), cell(c.get("chip_id"), 22),
                         cell(c.get("step")), cell(c.get("metric") or c.get("what"), 34),
                         cell(c.get("verdict") or c.get("status"), 24)]
                        for c in cases])
    disagreements = [d for d in (foam.get("disagreements") or [])
                     if isinstance(d, dict)]
    lines += ["### Where 3D disagrees with the depth-averaged model", ""]
    lines += table(["chip", "feature", "metric", "2D", "3D", "what it means"],
                   [[cell(d.get("chip_id"), 22), cell(d.get("feature_id"), 22),
                     cell(d.get("metric"), 22), sig(d.get("depth_averaged")),
                     sig(d.get("foam")), cell(d.get("message"), 70)]
                    for d in disagreements],
                   empty="_none reported: the 2D findings above stand as written._")
    for path in foam.get("figures") or []:
        lines += [f"![3D result]({cell(path)})", ""]


def _figures(result, lines):
    figures = result.get("figures") or []
    lines += ["## 7. Figures (2D)", "",
              "All SIMULATED. Four maps per chip per simulated step - pressure, speed, "
              "wall shear as a fraction of that chamber's own ceiling, residence time - "
              "each with the chip outline, ports, a scale bar and a marker on every "
              "finding. Plus one overview of the whole chain.", ""]
    if not figures:
        lines += ["_no figures written_", ""]
        return
    overview = [p for p in figures if "pipeline_overview" in str(p)]
    for path in overview:
        lines += [f"![pipeline overview]({cell(path)})", ""]
    rows = []
    for path in figures:
        name = Path(str(path)).name
        rows.append([f"`{name}`", f"[open]({cell(path)})"])
    lines += table(["figure", "link"], rows)


# --- the whole document ----------------------------------------------------------------

def render(result) -> str:
    """The report as markdown. Never raises on a partial result."""
    result = dict(result or {})
    lines: list[str] = []
    for section in (_header, _what_ran, _findings, _fixes, _handoffs, _validation,
                    _limits, _figures, _foam):
        try:
            section(result, lines)
        except Exception as exc:                       # a bad section is not a dead report
            lines += [f"_section {section.__name__} failed: "
                      f"{type(exc).__name__}: {exc}_", ""]
    verdict, trusted, _ = validation_verdict(result)
    lines += ["---", "",
              f"Generated by `assay2chip/cfd/report.py` (S10). {verdict}. "
              f"{'All findings above are UNTRUSTED.' if not trusted else ''} "
              f"Every value is simulated, not measured.", ""]
    return "\n".join(lines)


def write(result, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(result), encoding="utf-8")
    return path


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Render a CFD report from a run summary.")
    ap.add_argument("summary", help="assay2chip/work/<run>/cfd/cfd.json")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    result = json.loads(Path(args.summary).read_text())
    text = render(result)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(args.out)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""The ONE finding shape S9 and S10 both produce and the report consumes.

`assay2chip/cfd/README.md` fixes it: every finding carries a `severity`, the chip and
feature it is about, **the two numbers that disagree**, and a `fix` where one exists.
S9 (tool verification, cross-chip checks) and S10 (flow simulation) emit the same shape,
so the report renders them with one function instead of two and nothing is lost in
translation. Duplicating a shape is how this repo ended up with four capsule diameters,
which is exactly what S9 now checks for.

    from assay2chip.report import findings

    f = findings.finding("blocker", "tools/check_chip_spec.py", "capsule_channel_width",
                         chip="my_assay_c2", feature="ch_in",
                         what="channel is narrower than a swollen capsule",
                         observed=("channel width", 105.0, "um"),
                         limit=("1.5 x capsule diameter", 135.0, "um"),
                         rerun_from="s6")

    rows = findings.normalise(json.load(open(".../cfd/findings.json")))   # S10's file
    findings.counts(rows)        # {'blocker': 0, 'high': 2, 'medium': 1, 'low': 0, 'info': 0}

`normalise` is deliberately tolerant. S10 is being written in parallel and the README
gives its findings prose, not a schema, so this reads the documented keys and several
obvious spellings of them rather than failing. Anything it cannot place is kept verbatim
under `raw` so the report can still show it - a finding that disappears because the
reader did not recognise it is the worst outcome available.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

# Worst first. The report and S9's verdict both rely on this order.
SEVERITIES = ("blocker", "high", "medium", "low", "info")

# A finding is a plain dict with these keys. Every one is always present, so no consumer
# needs .get() with a default that might differ from another consumer's default.
KEYS = (
    "severity",     # one of SEVERITIES
    "source",       # the tool or check that found it: 'tools/validate.py', 's9_cross_chip', 's10_cfd'
    "rule",         # stable snake_case name, e.g. 'port_overlap', 'volume_continuity'
    "chip",         # chip id, or None when it is about the run as a whole
    "feature",      # channel/chamber/port/handoff id, or None
    "what",         # one sentence, readable, no numbers (they are below)
    "observed",     # {"name", "value", "units"} - what the design does
    "limit",        # {"name", "value", "units"} - what it must do
    "fix",          # {"what", "predicted", "applied"} or None
    "rerun_from",   # 's4'...'s8' when the fix is upstream, else None
    "scope",        # 'pipeline' (our chip) or 'preexisting' (a hand-made chip on main)
    "message",      # the tool's own line, verbatim, so nothing is lost in parsing
)

PIPELINE, PREEXISTING = "pipeline", "preexisting"


class FindingError(ValueError):
    """A finding was built with a severity or scope that does not exist."""


def number(name: str, value: Any, units: str = "") -> dict | None:
    """One of the two numbers. None when there is no number - never 0 as a stand-in."""
    if value is None:
        return None
    return {"name": name, "value": value, "units": units}


def _pair(value: Any) -> dict | None:
    """Accept ('name', 1.0, 'um'), {'name':..,'value':..,'units':..} or None."""
    if value is None:
        return None
    if isinstance(value, dict):
        return {
            "name": value.get("name", ""),
            "value": value.get("value"),
            "units": value.get("units", ""),
        }
    if isinstance(value, (list, tuple)):
        name, val, *rest = list(value) + ["", None]
        return number(str(name), val, str(rest[0]) if rest and rest[0] else "")
    raise FindingError(f"a finding number must be a dict or (name, value, units), not {value!r}")


def finding(
    severity: str,
    source: str,
    rule: str,
    *,
    what: str = "",
    chip: str | None = None,
    feature: str | None = None,
    observed: Any = None,
    limit: Any = None,
    fix: dict | None = None,
    rerun_from: str | None = None,
    scope: str = PIPELINE,
    message: str = "",
) -> dict:
    """Build a finding. Raises rather than inventing a severity it does not know."""
    if severity not in SEVERITIES:
        raise FindingError(
            f"severity {severity!r} is not one of {', '.join(SEVERITIES)} "
            f"(assay2chip/cfd/README.md fixes the list)"
        )
    if scope not in (PIPELINE, PREEXISTING):
        raise FindingError(f"scope must be '{PIPELINE}' or '{PREEXISTING}', not {scope!r}")
    return {
        "severity": severity,
        "source": source,
        "rule": rule,
        "chip": chip,
        "feature": feature,
        "what": what,
        "observed": _pair(observed),
        "limit": _pair(limit),
        "fix": fix,
        "rerun_from": rerun_from,
        "scope": scope,
        "message": message,
    }


def fix(what: str, predicted: Any = None, applied: bool | None = None) -> dict:
    """A fix as the report shows it: what it changes, the number it predicts, applied or not.

    `applied` is None for 'nobody has decided yet' and the report says so rather than
    guessing - assay2chip/cfd/README.md only applies a fix automatically when the re-run
    shows the finding gone.
    """
    return {"what": what, "predicted": predicted, "applied": applied}


# --- reading other stages' files ----------------------------------------------------

# S10's keys, in the order this reads them. The first spelling is the documented one;
# the rest are near-misses seen in the repo's other JSON. TODO(s10): when
# assay2chip/stages/s10_cfd.py lands, drop the aliases and keep the documented key.
_SEVERITY_KEYS = ("severity", "level")
_RULE_KEYS = ("rule", "class", "kind", "type", "check")
_CHIP_KEYS = ("chip", "chip_id", "module")
_FEATURE_KEYS = ("feature", "edge", "cell", "id", "node", "where", "handoff")
_WHAT_KEYS = ("what", "message", "description", "summary", "detail")
_OBSERVED_KEYS = ("observed", "measured", "value", "actual", "is")
_LIMIT_KEYS = ("limit", "ceiling", "threshold", "allowed", "should_be", "target")


def _first(raw: dict, keys: Iterable[str]) -> Any:
    for key in keys:
        if key in raw and raw[key] is not None:
            return raw[key]
    return None


def _as_pair(value: Any, fallback_name: str) -> dict | None:
    """A number that may arrive as a dict, a (name, value, units) triple or a bare float."""
    if value is None:
        return None
    if isinstance(value, dict):
        if "value" in value or "name" in value:
            return _pair(value)
        # {"re": 13.0} - a single-entry dict naming its own quantity
        if len(value) == 1:
            (name, val), = value.items()
            return number(str(name), val)
        return None
    if isinstance(value, (list, tuple)):
        return _pair(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return number(fallback_name, value)
    return None


def normalise_one(raw: Any, source: str = "s10_cfd", scope: str = PIPELINE) -> dict:
    """One finding from S10 (or anywhere) in the shape above. Never raises on odd input."""
    if not isinstance(raw, dict):
        return finding("info", source, "unreadable", what=str(raw)[:200], scope=scope,
                       message=str(raw)[:500])
    severity = str(_first(raw, _SEVERITY_KEYS) or "info").strip().lower()
    if severity not in SEVERITIES:
        severity = "info"
    out = finding(
        severity,
        str(raw.get("source") or source),
        str(_first(raw, _RULE_KEYS) or "unclassified"),
        what=str(_first(raw, _WHAT_KEYS) or "").strip(),
        chip=_str_or_none(_first(raw, _CHIP_KEYS)),
        feature=_str_or_none(_first(raw, _FEATURE_KEYS)),
        observed=_as_pair(_first(raw, _OBSERVED_KEYS), "observed"),
        limit=_as_pair(_first(raw, _LIMIT_KEYS), "limit"),
        fix=_normalise_fix(raw.get("fix")),
        rerun_from=_str_or_none(raw.get("rerun_from")),
        scope=scope if raw.get("scope") not in (PIPELINE, PREEXISTING) else raw["scope"],
        message=str(_first(raw, ("message", "what", "description")) or "")[:1000],
    )
    unused = {k: v for k, v in raw.items() if k not in _CONSUMED}
    if unused:
        out["raw"] = unused
    return out


_CONSUMED = set(KEYS) | set(_SEVERITY_KEYS) | set(_RULE_KEYS) | set(_CHIP_KEYS) \
    | set(_FEATURE_KEYS) | set(_WHAT_KEYS) | set(_OBSERVED_KEYS) | set(_LIMIT_KEYS)


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _normalise_fix(raw: Any) -> dict | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return fix(raw)
    if not isinstance(raw, dict):
        return fix(str(raw))
    applied = raw.get("applied")
    if applied is None and isinstance(raw.get("status"), str):
        applied = {"applied": True, "proposed": False, "rejected": False}.get(
            raw["status"].strip().lower()
        )
    predicted = raw.get("predicted")
    if predicted is None:
        predicted = raw.get("after", raw.get("predicted_value", raw.get("new_value")))
    return {
        "what": str(raw.get("what") or raw.get("change") or raw.get("description") or ""),
        "predicted": predicted,
        "applied": applied,
        **({"before": raw["before"]} if "before" in raw else {}),
        **({"finding": raw["finding"]} if "finding" in raw else {}),
    }


def normalise(data: Any, source: str = "s10_cfd", scope: str = PIPELINE) -> list[dict]:
    """Every finding in an S10 findings.json, whatever container it uses.

    Accepts a bare list, {"findings": [...]}, or {"<chip_id>": [...]} - all three appear
    in this repo's JSON - and keeps the chip id from the key when the finding omits it.
    """
    if data is None:
        return []
    if isinstance(data, list):
        return [normalise_one(x, source, scope) for x in data]
    if not isinstance(data, dict):
        return [normalise_one(data, source, scope)]
    for key in ("findings", "results", "items"):
        if isinstance(data.get(key), list):
            return [normalise_one(x, source, scope) for x in data[key]]
    out: list[dict] = []
    for key, value in data.items():
        if isinstance(value, list):
            for item in value:
                row = normalise_one(item, source, scope)
                if row["chip"] is None and isinstance(key, str):
                    row["chip"] = key
                out.append(row)
    return out


def normalise_fixes(data: Any) -> list[dict]:
    """Every fix in an S10 fixes.json, with `applied` true / false / None (undecided)."""
    if data is None:
        return []
    items: list[Any] = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("fixes", "proposals", "items"):
            if isinstance(data.get(key), list):
                items = data[key]
                break
        else:
            items = [v for v in data.values() if isinstance(v, dict)]
    out = []
    for raw in items:
        row = _normalise_fix(raw) or fix("")
        if isinstance(raw, dict):
            row.setdefault("finding", raw.get("finding") or raw.get("addresses"))
            row["chip"] = _str_or_none(_first(raw, _CHIP_KEYS))
            row["rule"] = _str_or_none(_first(raw, _RULE_KEYS))
        out.append(row)
    return out


# --- summarising --------------------------------------------------------------------


def counts(rows: Sequence[dict]) -> dict[str, int]:
    """How many findings at each severity. Every severity is present, including the zeros,
    so a report never hides 'blocker: 0' by omitting it."""
    out = {s: 0 for s in SEVERITIES}
    for row in rows:
        out[row.get("severity", "info") if row.get("severity") in out else "info"] += 1
    return out


def worst(rows: Sequence[dict]) -> str | None:
    """The worst severity present, or None for an empty list."""
    for severity in SEVERITIES:
        if any(row.get("severity") == severity for row in rows):
            return severity
    return None


def of_scope(rows: Sequence[dict], scope: str = PIPELINE) -> list[dict]:
    return [r for r in rows if r.get("scope", PIPELINE) == scope]


def blockers(rows: Sequence[dict], scope: str | None = PIPELINE) -> list[dict]:
    """Findings that fail a stage: severity 'blocker', in `scope` (None for any scope)."""
    return [
        r for r in rows
        if r.get("severity") == "blocker" and (scope is None or r.get("scope", PIPELINE) == scope)
    ]


def rerun_request(rows: Sequence[dict]) -> dict | None:
    """The earliest stage a blocker asks to re-run from, as run.py's rerun_from dict.

    Earliest, because re-running from s6 also re-runs s7 and s8: fixing the geometry and
    then re-routing is the only order that can work.
    """
    wanted = sorted(
        {r["rerun_from"] for r in blockers(rows) if r.get("rerun_from")},
        key=lambda s: int(s.lstrip("s") or 99),
    )
    if not wanted:
        return None
    first = wanted[0]
    why = [f"{r['rule']} on {r['chip'] or 'the run'}" for r in blockers(rows) if r.get("rerun_from")]
    return {
        "rerun_from": first,
        "reason": f"{len(why)} blocker(s) with an upstream fix: " + "; ".join(sorted(set(why))[:6]),
    }


def one_line(row: dict) -> str:
    """'105 um vs 135 um' - the two numbers, for a table cell. '-' when there are none."""
    obs, lim = row.get("observed"), row.get("limit")
    if not obs and not lim:
        return "-"
    return " vs ".join(
        f"{_fmt(p['value'])}{(' ' + p['units']) if p.get('units') else ''}"
        for p in (obs, lim) if p
    )


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)

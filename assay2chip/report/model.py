"""Gather every stage's output into one report.json. Counting only - no formatting.

The report answers five questions and nothing else:

  1. Where did every assay step end up?        chip | op | off_chip | missing
  2. How much of it is actually automated?     chips, on-chip fraction, NON-AUTOMATED hand-offs
  3. What does nobody know?                    S1's unresolved questions, carried forward
  4. What would stop it running?               S9 and S10 findings by severity, fixes applied
  5. What can be made?                         cad/exports/<chip_id>/print/

Missing stages are recorded, never faked. Every stage this one reads is optional, because
the report has to be useful at every point in the build-out: when S10 has not run, the
report says "S10 did not run" in `missing` and the CFD table is empty, rather than
implying a clean simulation. The same goes for the fabrication bundle.

THE FOUR PLACES A STEP CAN END UP
---------------------------------
  chip      on a chip, and the built spec has a part of its own for it. Geometry does
            the work. Matched on the op's `placement.blocks` / `placement.part` against
            the chambers and channels of `chip/<chip_id>.json`.
  op        on a chip, but with no part of its own: a protocol action through parts that
            are already there (a hold, a flush, a valve move). Still automated and still
            on-chip - but an assay that is *all* `op` has not really been put on a chip,
            which is why the two are not merged.
  off_chip  a tube, a spin, an instrument. S3 said so.
  missing   S3 found no part for it (`missing_part`), or the step reached no op at all.
            Both are holes; the second is worse, and `why` says which.

`placement.where == "on_chip"` always carries a `part` (the schema requires it), so the
claim alone cannot separate `chip` from `op`: only the built spec can. Until S6/S7 have
written one, those rows say `chip` with `"verified": false` and the report counts them,
rather than asserting a part exists that nobody has drawn.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..lib import io as stage_io
from . import findings as F

ROOT = stage_io.ROOT
EXPORTS = ROOT / "cad" / "exports"

# Where S11 puts the print-ready bundle (assay2chip/README.md, S11).
BUNDLE = "cad/exports/{chip_id}/print"

WHERE_CHIP, WHERE_OP, WHERE_OFF, WHERE_MISSING = "chip", "op", "off_chip", "missing"
WHERE_ORDER = (WHERE_CHIP, WHERE_OP, WHERE_OFF, WHERE_MISSING)

# Always present, in every report, whatever ran. The project owner has been handed
# simulated numbers that read as measurements before; this is the fix.
HONEST_LIMITS = [
    "**Nothing here was measured.** No chip in this report has been printed, bonded or "
    "run. Every number comes from a contract, a geometric calculation or a simulation.",
    "**The flow numbers are simulated.** S10 solves depth-averaged Stokes flow on a "
    "resistance network (`assay2chip/cfd/README.md`). It has no recirculation by "
    "construction, so a trap here means a low-velocity or long-residence zone, not an "
    "eddy. Any edge above Re 1 is outside the model and is reported as a finding, not a "
    "number.",
    "**S9 is static.** It runs the repo's own checks - validate.py, check_chip_spec.py, "
    "the planar collision check, operator/run.py - plus the cross-chip checks. A chip "
    "that passes them can still trap, leak or shear.",
    "**Volumes, times and temperatures are the source's**, carried through unchanged "
    "where the source stated them and listed under unresolved questions where it did "
    "not. Nothing was filled in to make a table look complete.",
    "**Fabrication limits are partly unknown.** cad/fabrication.json still has no "
    "measured laser kerf or minimum embossed feature, so features below the in-plane "
    "floor are reported as design-fine / not-makeable-here rather than passed or failed.",
]


def collect(assay_id: str) -> dict:
    """Everything the report needs, as report.json. Reads; never writes, never guesses.

    The work directory is `assay2chip/lib/io`'s, never a local idea of one: that module
    owns where a run's files live and validates them on the way in.
    """
    work = stage_io.work_dir(assay_id)
    missing: list[str] = []

    assay = _stage(assay_id, "assay", missing, "S1 extract")
    unitops = _stage(assay_id, "unitops", missing, "S2 normalise / S3 triage")
    partition = _stage(assay_id, "partition", missing, "S4 partition")
    verify = _json(work / "verify.json", missing, "S9 verify")
    cfd_findings_raw = _json(work / "cfd" / "findings.json", missing, "S10 cfd")
    cfd_fixes_raw = _json(work / "cfd" / "fixes.json", missing, "S10 cfd (fixes)")
    manifest = _json(work / "run_manifest.json", None, "")

    ops = list((unitops or {}).get("ops") or [])
    chips = list((partition or {}).get("chips") or [])
    handoffs = list((partition or {}).get("handoffs") or [])
    chip_of_op = {op_no: chip["chip_id"] for chip in chips for op_no in chip.get("ops", [])}
    parts_of_chip = {chip["chip_id"]: _chip_parts(chip["chip_id"]) for chip in chips}

    steps = _step_rows(assay, ops, chip_of_op, parts_of_chip)
    cfd = F.normalise(cfd_findings_raw, source="s10_cfd") if cfd_findings_raw else []
    fixes = F.normalise_fixes(cfd_fixes_raw) if cfd_fixes_raw else []
    verify_findings = list((verify or {}).get("findings") or [])
    bundles = [_bundle(chip["chip_id"]) for chip in chips]

    where_counts = {w: sum(1 for s in steps if s["where"] == w) for w in WHERE_ORDER}
    unverified = sum(1 for s in steps if s["verified"] is False)
    on_chip_steps = where_counts[WHERE_CHIP] + where_counts[WHERE_OP]
    not_automated = [h for h in handoffs if not h.get("automated")]
    s1_unresolved = list((assay or {}).get("unresolved") or [])

    report = {
        "id": assay_id,
        "title": (assay or {}).get("title") or (partition or {}).get("id") or assay_id,
        "source": (assay or {}).get("source"),
        "generated_by": "assay2chip/stages/s12_report.py",
        "stages_present": _stages_present(assay, unitops, partition, verify,
                                          cfd_findings_raw, bundles),
        "missing": missing,
        "headline": {
            "chips": len(chips),
            "assay_steps": len(steps),
            "unit_ops": len(ops),
            "steps_on_chip": on_chip_steps,
            "on_chip_fraction": round(on_chip_steps / len(steps), 3) if steps else None,
            "steps_by_where": where_counts,
            "steps_unverified_against_a_spec": unverified,
            "handoffs": len(handoffs),
            "handoffs_not_automated": len(not_automated),
            "unresolved_from_s1": len(s1_unresolved),
            "unresolved_total": len(s1_unresolved)
            + len(list((unitops or {}).get("unresolved") or []))
            + len(list((partition or {}).get("unresolved") or [])),
            "cfd_by_severity": F.counts(cfd),
            "cfd_fixes_applied": sum(1 for f in fixes if f.get("applied") is True),
            "cfd_fixes_proposed": sum(1 for f in fixes if f.get("applied") is not True),
            "verify_ok": (verify or {}).get("ok"),
            "verify_blockers": len(F.blockers(verify_findings)),
            "bundles_ready": sum(1 for b in bundles if b["present"]),
        },
        "steps": steps,
        "chips": [
            {
                "chip_id": chip["chip_id"],
                "ops": chip.get("ops", []),
                "scale": chip.get("scale"),
                "reason": chip.get("reason", ""),
                "ports": chip.get("ports"),
                "area_mm2": chip.get("area_mm2"),
                "temp_range_c": chip.get("temp_range_c"),
                "bundle": bundle,
                "verify_blockers": len(F.blockers(
                    [r for r in verify_findings if r.get("chip") == chip["chip_id"]])),
                "cfd_worst": F.worst([r for r in cfd if r.get("chip") == chip["chip_id"]]),
            }
            for chip, bundle in zip(chips, bundles)
        ],
        "handoffs": [
            {
                "from": h.get("from"), "to": h.get("to"),
                "from_port": h.get("from_port"), "to_port": h.get("to_port"),
                "carries": h.get("carries"),
                "volume_ul": h.get("volume_ul"),
                "particle_um": h.get("particle_um"),
                "automated": bool(h.get("automated")),
                "how": h.get("how", ""),
                "risks": h.get("risks", []),
            }
            for h in handoffs
        ],
        "cuts": list((partition or {}).get("cuts") or []),
        "unresolved": {
            "s1": s1_unresolved,
            "s2_s3": list((unitops or {}).get("unresolved") or []),
            "s4": list((partition or {}).get("unresolved") or []),
        },
        "verify": {
            "ok": (verify or {}).get("ok"),
            "verdict": (verify or {}).get("verdict"),
            "counts": (verify or {}).get("counts"),
            "tools": (verify or {}).get("tools", []),
            "prerequisites_missing": (verify or {}).get("prerequisites_missing", []),
            "findings": verify_findings,
        },
        "cfd": {
            "findings": cfd,
            "by_severity": F.counts(cfd),
            "fixes": {
                "applied": [f for f in fixes if f.get("applied") is True],
                "proposed": [f for f in fixes if f.get("applied") is not True],
            },
        },
        "fabrication": bundles,
        "llm": _llm_block(assay, unitops, partition, manifest),
        "limits": HONEST_LIMITS,
    }
    return report


# --- the pieces ----------------------------------------------------------------------


def _stage(assay_id: str, kind: str, missing: list[str], who: str) -> dict | None:
    """A schema-validated stage file, or None with a line in `missing`."""
    try:
        return stage_io.read_stage(assay_id, kind)
    except stage_io.StageFileMissing:
        missing.append(f"{who} has not run: {stage_io.FILES[kind]} is not there")
    except stage_io.StageValidationError as error:
        missing.append(f"{who} wrote a {kind}.json that does not match its schema: "
                       f"{str(error).splitlines()[0]}")
    return None


def _json(path: Path, missing: list[str] | None, who: str) -> Any:
    if not path.exists():
        if missing is not None:
            try:
                where = path.resolve().relative_to(ROOT).as_posix()
            except ValueError:
                where = str(path)
            missing.append(f"{who} has not run: {where} is not there")
        return None
    try:
        return json.loads(path.read_text())
    except ValueError as error:
        if missing is not None:
            missing.append(f"{who} wrote unreadable JSON ({path.name}): {error}")
        return None


def _step_rows(assay: dict | None, ops: list[dict], chip_of_op: dict[int, str],
               parts_of_chip: dict[str, set[str] | None]) -> list[dict]:
    """One row per ASSAY step - the question is where the paper's steps ended up.

    A step covered by several ops takes the best outcome of those ops, because the step
    did make it onto a chip; the row still names every op so the split is visible.
    """
    if not assay:
        return []
    ops_of_step: dict[int, list[dict]] = {}
    for op in ops:
        for step_no in op.get("from_steps") or []:
            ops_of_step.setdefault(step_no, []).append(op)

    rows = []
    for step in assay.get("steps") or []:
        mine = ops_of_step.get(step["step"], [])
        placed = [_where_of(op, chip_of_op, parts_of_chip) for op in mine]
        best = min(placed, key=lambda p: WHERE_ORDER.index(p["where"])) if placed else {
            "where": WHERE_MISSING, "chip": None, "verified": True,
            "why": "no unit operation covers this step: S2 dropped it",
        }
        rows.append({
            "step": step["step"],
            "action": step.get("action"),
            "text": step.get("text", "")[:160],
            "confidence": step.get("confidence"),
            "ops": [op["op"] for op in mine],
            "kinds": sorted({op["kind"] for op in mine}),
            **best,
        })
    return rows


def _where_of(op: dict, chip_of_op: dict[int, str],
              parts_of_chip: dict[str, set[str] | None]) -> dict:
    """One op's outcome. See the four buckets in this module's docstring.

    `verified` is False when the chip spec that would settle `chip` vs `op` has not been
    built yet, so the report can say how much of its own placement table is a claim.
    """
    placement = op.get("placement") or {}
    where = placement.get("where")
    why = placement.get("why") or ""
    chip = chip_of_op.get(op.get("op"))

    def row(bucket: str, chip_id: str | None, reason: str, verified: bool = True) -> dict:
        return {"where": bucket, "chip": chip_id, "why": reason, "verified": verified}

    if where == "on_chip":
        if chip is None:
            return row(WHERE_MISSING, None,
                       "S3 put it on chip but S4 put it on no chip: nothing carries it")
        parts = parts_of_chip.get(chip)
        if parts is None:
            return row(WHERE_CHIP, chip, why, verified=False)
        wanted = {str(x).lower() for x in
                  ([placement.get("part")] + list(placement.get("blocks") or [])) if x}
        if wanted & parts:
            return row(WHERE_CHIP, chip, why)
        return row(WHERE_OP, chip,
                   why + (" " if why else "") + f"(no part of its own on {chip}: it runs "
                   f"through parts that are already there)")
    if where == "off_chip":
        return row(WHERE_OFF, None, why)
    if where == "missing_part":
        return row(WHERE_MISSING, None,
                   placement.get("need") or why or "no part exists for it")
    return row(WHERE_MISSING, chip, why or "S3 recorded no placement for this op")


def _chip_parts(chip_id: str) -> set[str] | None:
    """Every name a part on this chip answers to, lowercased: ids, `block`s and `type`s.

    None when the spec is not there - 'I cannot tell', which is not the same as 'empty'.
    """
    path = ROOT / "chip" / f"{chip_id}.json"
    if not path.is_file():
        return None
    try:
        chip = json.loads(path.read_text())
    except ValueError:
        return None
    names: set[str] = set()
    for group in ("chambers", "channels", "features", "stages", "valves"):
        for item in chip.get(group) or []:
            if isinstance(item, dict):
                names |= {str(item[key]).lower() for key in ("id", "block", "type")
                          if item.get(key)}
    return names


def _bundle(chip_id: str) -> dict:
    """The print-ready bundle for one chip: the path, and what is actually in it."""
    path = ROOT / BUNDLE.format(chip_id=chip_id)
    files = sorted(p.name for p in path.iterdir() if p.is_file()) if path.is_dir() else []
    return {
        "chip_id": chip_id,
        "path": BUNDLE.format(chip_id=chip_id) + "/",
        "present": bool(files),
        "files": files,
        "kinds": sorted({p.rsplit(".", 1)[-1].lower() for p in files}),
    }


def _stages_present(assay, unitops, partition, verify, cfd, bundles) -> dict[str, bool]:
    return {
        "s1_extract": assay is not None,
        "s2_s3_unitops": unitops is not None,
        "s4_partition": partition is not None,
        "s9_verify": verify is not None,
        "s10_cfd": cfd is not None,
        "s11_fabricate": any(b["present"] for b in bundles),
    }


def _llm_block(assay, unitops, partition, manifest) -> dict:
    """Which stages used a model, and whether it was the offline stub.

    A stubbed extraction and a real one must never look the same in a report: the stub's
    numbers are made up by construction.
    """
    modes = {
        "s1_extract": ((assay or {}).get("llm") or {}).get("mode"),
        "s2_s3_unitops": ((unitops or {}).get("llm") or {}).get("mode"),
        "s4_partition": ((partition or {}).get("llm") or {}).get("mode"),
    }
    stubbed = sorted(k for k, v in modes.items() if v == "stub")
    return {
        "modes": modes,
        "stubbed_stages": stubbed,
        "note": (
            "Stages " + ", ".join(stubbed) + " ran the OFFLINE STUB, not a model: treat "
            "their text and any number they introduced as placeholder."
        ) if stubbed else "No stage reported a stubbed model call.",
        "manifest": (manifest or {}).get("llm"),
    }

"""S10: simulate the chips this pipeline built, end to end, and ask for a re-run if a fix
changes geometry.

Reads `partition.json` (S4) for the chip list and the handoffs, drives
`assay2chip/cfd/run.py` over them, and writes into `assay2chip/work/<id>/cfd/`:

    <chip_id>.mesh.json  <chip_id>.field.json  pipeline.field.json
    findings.json  fixes.json  validation.json  cfd.json
    report.md            the S10 section, short; S12 writes the run's own report.md
    figures/*.svg        four maps per chip per step, plus the chain overview

THE RE-RUN. A fix that changes geometry is NOT applied here. `chip/*.json` holds real
hand-designed work, and even a pipeline-made chip has to go back through S7's routing and
S9's checks before anyone trusts it, so S10 returns

    {"rerun_from": "s6", "reason": "..."}

and `assay2chip/run.py` re-runs from there, logs the reason in the manifest and caps the
loop at MAX_RERUN_LOOPS. Flow-only fixes (drop this rate, add a wash) change the protocol,
not the geometry, and come back as a finding plus a fix for a human rather than a loop.
`ctx.loop` is how many times this stage has already asked; at the cap it stops asking and
says so in the report, because a silent loop is worse than an unfixed chip.

This stage writes only inside the work directory, so WRITES is empty: the patched chip
copies a standalone CFD run would make are never written into `chip/` from here.

Runs offline. Needs no language model.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib.io import read_stage  # noqa: E402

# Nothing outside assay2chip/work/<id>/ - see the module docstring.
WRITES: list[str] = []

CFD_RUN = ROOT / "assay2chip" / "cfd" / "run.py"

#: How many times this stage may send the pipeline backwards. The orchestrator caps it
#: too (assay2chip/run.py MAX_RERUN_LOOPS); this is the stage's own guard so the number
#: appears in the report even when the stage is run on its own.
MAX_LOOPS = 3


def _shortest(path):
    """A repo-relative path when it is inside the repo, else the path. Never raises."""
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)


def _load_cfd():
    """assay2chip/cfd/run.py, by path. -> (module, None) or (None, reason)."""
    if not CFD_RUN.exists():
        return None, f"{_shortest(CFD_RUN)} does not exist yet"
    try:
        spec = importlib.util.spec_from_file_location("assay2chip.cfd.run", CFD_RUN)
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault(spec.name, module)
        spec.loader.exec_module(module)
        module.bootstrap()
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return module, None


def _chip_paths(partition):
    """[(chip_id, chip path, protocol path)] for the chips S6-S8 produced."""
    out = []
    for entry in (partition or {}).get("chips") or []:
        chip_id = str(entry.get("chip_id") or "")
        if not chip_id:
            continue
        chip = ROOT / "chip" / f"{chip_id}.json"
        protocol = ROOT / "protocols" / f"{chip_id}.json"
        out.append((chip_id, chip if chip.exists() else None,
                    protocol if protocol.exists() else None))
    return out


def _as_pipeline(partition, pairs):
    """partition.json as the shape cfd/figures.pipeline_overview draws.

    The CFD package's chain view is `protocols/pipeline_mvp.json`-shaped: modules in
    chain order with handoffs between them. A partition is the same graph under different
    key names, so it is translated here rather than teaching the figure two formats.
    """
    modules = []
    for chip_id, chip_path, protocol_path in pairs:
        entry = next((c for c in partition.get("chips") or []
                      if c.get("chip_id") == chip_id), {})
        modules.append({
            "module": chip_id,
            "name": entry.get("reason") or chip_id,
            "status": "final" if chip_path else "missing",
            "status_note": None if chip_path else f"chip/{chip_id}.json was not built",
            "chip": f"chip/{chip_id}.json" if chip_path else None,
            "protocol": f"protocols/{chip_id}.json" if protocol_path else None,
        })
    handoffs = []
    for i, hand in enumerate(partition.get("handoffs") or []):
        handoffs.append({
            "id": hand.get("id") or f"handoff_{i + 1}",
            "carries": hand.get("carries"),
            "volume_ul": hand.get("volume_ul"),
            "particle_um": hand.get("particle_um"),
            "from": {"module": hand.get("from"), "port": hand.get("from_port")},
            "to": {"module": hand.get("to"), "port": hand.get("to_port")},
            "connector": {"status": "match" if hand.get("automated") else "manual",
                          "warning": None if hand.get("automated") else
                          f"not automated: {hand.get('how') or 'pipetting'}"},
            "note": "; ".join(hand.get("risks") or []) or hand.get("how") or "",
        })
    return {"kind": "pipeline", "id": partition.get("id") or "partition",
            "name": f"{partition.get('id', 'partition')}: chips from S4-S8",
            "modules": modules, "handoffs": handoffs}


def run(ctx=None, assay_id=None, verbose=False, **_ignored):
    """Simulate every chip in the partition. May return a rerun_from request."""
    assay_id = assay_id or (ctx.id if ctx is not None else None)
    if not assay_id:
        raise ValueError("s10_cfd needs an assay id")
    log = ctx.log if ctx is not None else (lambda message: None)
    work_dir = (ctx.work_dir if ctx is not None else ROOT / "assay2chip" / "work"
                / assay_id)
    out_dir = Path(work_dir) / "cfd"
    loop = int(getattr(ctx, "loop", 0) or 0)

    cfd, why = _load_cfd()
    if cfd is None:
        log(f"assay2chip/cfd is NOT YET AVAILABLE ({why}): nothing simulated")
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.md").write_text(
            "# S10 CFD\n\nNOT RUN. `assay2chip/cfd/run.py` could not be loaded: "
            f"{why}.\n\nNo chip in this run has been checked for leaks, traps, shear or "
            "handoff mismatches.\n", encoding="utf-8")
        return None

    partition = read_stage(assay_id, "partition")
    pairs = _chip_paths(partition)
    if not pairs:
        log("partition.json names no chips: nothing to simulate")
        return None

    run_obj = cfd.Run(assay_id, out_dir, "pipeline", verbose=bool(verbose),
                      pipeline_mode=True)
    run_obj.log(f"S10 CFD: {assay_id} ({len(pairs)} chip(s) from partition.json)")
    run_obj.validate()
    missing = []
    for chip_id, chip_path, protocol_path in pairs:
        if chip_path is None:
            missing.append(chip_id)
            run_obj.gaps.append({
                "subject": f"chip {chip_id}",
                "question": "partition.json names this chip but chip/<id>.json does not "
                            "exist: S6/S7 has not built it, so there is nothing to "
                            "simulate."})
            continue
        chip = cfd.load_json(chip_path)
        protocol = cfd.load_json(protocol_path) if protocol_path else None
        run_obj.simulate_chip(chip, protocol, chip_path, protocol_path, chip_id)

    pipeline = _as_pipeline(partition, pairs)
    result = run_obj.finish(f"S10 CFD for {assay_id}", pipeline["handoffs"], None,
                            pipeline, {m["module"]: cfd.load_json(ROOT / m["chip"])
                                       for m in pipeline["modules"] if m["chip"]})
    counts = {}
    for finding in result["findings"]:
        key = str(finding.get("severity") or "info")
        counts[key] = counts.get(key, 0) + 1
    log(f"{len(result['chips'])} chip(s), {len(result['findings'])} finding(s) "
        f"{counts or '(none)'}, {len(result['figures'])} figure(s)")
    if missing:
        log(f"not simulated, not built: {', '.join(missing)}")
    validation = result.get("validation") or {}
    if not validation.get("trusted"):
        log(f"validation {validation.get('status')}: every finding is UNTRUSTED "
            f"({validation.get('detail')})")

    rerun = result.get("rerun")
    if rerun:
        if loop >= MAX_LOOPS:
            log(f"a geometry fix wants {rerun['rerun_from']} again, but this is loop "
                f"{loop} of {MAX_LOOPS}: NOT asking. The fix stays a proposal.")
            return None
        log(f"asking for a re-run from {rerun['rerun_from']}: {rerun['reason']}")
        return {"rerun_from": rerun["rerun_from"],
                "reason": f"S10 CFD loop {loop + 1}/{MAX_LOOPS}: {rerun['reason']}"}
    return None


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Run S10 on one work directory.")
    ap.add_argument("--id", required=True)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    out = run(assay_id=args.id, verbose=args.verbose)
    print(out if out else "no re-run requested")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

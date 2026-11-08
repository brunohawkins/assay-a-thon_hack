"""The run as a state machine. `advance(run_id)` is idempotent and safe to call any time.

    ingest -> extract -> plan -> design ─┬─> foam (one job per chip x protocol step) ─┐
                                         └─> fabricate ───────────────────────────────┴─> collect -> report

The chain up to design is strict: a failed segment fails the run. After design everything
is best effort and the report always runs, so it can say what failed; the run ends `done`
when every job was clean, `done_with_errors` otherwise.

Which OpenFOAM cases: the steps of each chip's protocol that actually move fluid
(flow_ul_min > 0), highest flow first (they carry the worst shear and pressure), at most
A2C_FOAM_MAX_STEPS_PER_CHIP of them (default 4); the rest are listed in the `fanout`
event and in foam/summary.json as not simulated, never silently dropped. A run submitted
with options {"foam": false} skips OpenFOAM entirely.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import commands
from .queue import CANCELLED, DONE, DONE_WITH_ERRORS, FAILED, OK, TERMINAL, Queue


def _flow(step: dict) -> float:
    try:
        return float(step.get("flow_ul_min") or 0)
    except (TypeError, ValueError):
        return 0.0


def foam_cases(workspace: Path, run_id: str, max_steps: int) -> Tuple[List[dict], List[dict]]:
    """(cases to run, cases skipped with the reason), from the run's own workspace."""
    cases: List[dict] = []
    skipped: List[dict] = []
    partition = workspace / "assay2chip" / "work" / run_id / "partition.json"
    try:
        chips = [c["chip_id"] for c in json.loads(partition.read_text()).get("chips", [])]
    except (OSError, ValueError, KeyError, TypeError) as problem:
        return [], [{"chip": None, "why": "no readable partition.json: %s" % problem}]
    for chip in chips:
        chip_file = workspace / "chip" / ("%s.json" % chip)
        proto_file = workspace / "protocols" / ("%s.json" % chip)
        if not chip_file.exists():
            skipped.append({"chip": chip, "why": "design did not produce chip/%s.json" % chip})
            continue
        try:
            steps = json.loads(proto_file.read_text()).get("steps") or []
        except (OSError, ValueError):
            skipped.append({"chip": chip, "why": "no protocols/%s.json" % chip})
            continue
        flowing = [s for s in steps if _flow(s) > 0 and s.get("step") is not None]
        if not flowing:
            skipped.append({"chip": chip, "why": "no protocol step moves fluid"})
            continue
        ranked = sorted(flowing, key=lambda s: (-_flow(s), str(s.get("step"))))
        chosen = sorted(ranked[:max(0, max_steps)], key=lambda s: str(s.get("step")))
        for s in ranked[max(0, max_steps):]:
            skipped.append({"chip": chip, "step": s.get("step"),
                            "why": "beyond A2C_FOAM_MAX_STEPS_PER_CHIP=%d" % max_steps})
        for s in chosen:
            cases.append({"chip": chip, "step": s.get("step"), "flow_ul_min": _flow(s),
                          "out": commands.foam_out(run_id, chip, s.get("step"))})
    return cases, skipped


class Orchestrator:
    def __init__(self, queue: Queue, workspaces, settings):
        self.q = queue
        self.ws = workspaces
        self.settings = settings

    def submit_first(self, run_id: str) -> None:
        self.advance(run_id)

    def advance(self, run_id: str) -> str:
        run = self.q.run(run_id)
        if run is None:
            return "missing"
        if run["status"] in TERMINAL:
            return run["status"]
        jobs = {j["key"]: j for j in self.q.jobs(run_id)}

        if run["cancel_requested"]:
            if not any(j["status"] == "running" for j in jobs.values()):
                self.q.set_run_status(run_id, CANCELLED)
                return CANCELLED
            return "cancelling"

        def enqueue(kind: str, key: Optional[str] = None, resource: str = "light",
                    payload: Optional[dict] = None, priority: int = 100) -> None:
            self.q.enqueue(run_id, kind, key or kind, resource=resource, payload=payload,
                           max_attempts=commands.MAX_ATTEMPTS.get(kind, 2),
                           priority=priority)

        # 1. the strict chain
        for kind in commands.CHAIN:
            job = jobs.get(kind)
            if job is None:
                enqueue(kind, priority=10)
                return "running"
            if job["status"] in (FAILED, CANCELLED):
                self.q.set_run_status(run_id, FAILED, "%s %s: %s" % (
                    kind, job["status"], job.get("error") or "see its log"))
                return FAILED
            if job["status"] not in OK:
                return "running"

        # 2. fan out: foam cases + fabricate, in one transaction (fabricate marks it done)
        if "fabricate" not in jobs:
            options = run.get("options") or {}
            specs = [{"kind": "fabricate", "key": "fabricate", "resource": "light",
                      "max_attempts": commands.MAX_ATTEMPTS["fabricate"], "priority": 20}]
            cases, skipped = [], []
            if options.get("foam", True):
                max_steps = int(options.get("foam_max_steps") or
                                self.settings.foam_max_steps_per_chip)
                cases, skipped = foam_cases(self.ws.path(run_id), run_id, max_steps)
            else:
                skipped = [{"chip": None, "why": "run submitted with foam: false"}]
            specs += [{"kind": "foam", "key": "foam:%s:%s" % (c["chip"], c["step"]),
                       "resource": "foam", "payload": c,
                       "max_attempts": commands.MAX_ATTEMPTS["foam"], "priority": 50}
                      for c in cases]
            self.q.enqueue_many(run_id, specs)
            self.q.event(run_id, "fanout", {"foam_jobs": len(cases), "cases": cases,
                                            "skipped": skipped})
            return "running"

        after = [j for k, j in jobs.items() if k == "fabricate" or j["kind"] == "foam"]
        if any(j["status"] not in TERMINAL for j in after):
            return "running"

        # 3. fan in, then the report - both always run
        for kind in ("collect", "report"):
            job = jobs.get(kind)
            if job is None:
                enqueue(kind, priority=10)
                return "running"
            if job["status"] not in TERMINAL:
                return "running"

        # 4. the verdict
        report = jobs["report"]
        if report["status"] not in OK:
            self.q.set_run_status(run_id, FAILED, "report %s: %s" % (
                report["status"], report.get("error") or "see its log"))
            return FAILED
        clean = all(j["status"] == DONE for j in self.q.jobs(run_id))
        final = DONE if clean else DONE_WITH_ERRORS
        self.q.set_run_status(run_id, final)
        return final


def collect(queue: Queue, workspace: Path, run_id: str) -> Dict:
    """Fan-in: one foam/summary.json listing every OpenFOAM case and how it ended."""
    cases = []
    for job in queue.jobs(run_id):
        if job["kind"] != "foam":
            continue
        payload = job.get("payload") or {}
        out = workspace / (payload.get("out") or "")
        renders = sorted(p.relative_to(workspace).as_posix()
                         for p in out.rglob("*") if p.suffix in (".png", ".mp4", ".json")
                         ) if out.exists() else []
        cases.append({"chip": payload.get("chip"), "step": payload.get("step"),
                      "flow_ul_min": payload.get("flow_ul_min"), "status": job["status"],
                      "attempts": job["attempts"], "exit_code": job["exit_code"],
                      "error": job.get("error"), "outputs": renders})
    skipped = []
    for event in queue.events(run_id, 0, limit=100000):
        if event["name"] == "fanout":
            skipped = (event.get("data") or {}).get("skipped", [])
    summary = {"run_id": run_id, "label": "SIMULATED", "cases": cases,
               "not_simulated": skipped,
               "counts": {s: sum(1 for c in cases if c["status"] == s)
                          for s in sorted({c["status"] for c in cases})}}
    dest = workspace / "assay2chip" / "work" / run_id / "foam" / "summary.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(summary, indent=2) + "\n")
    return summary

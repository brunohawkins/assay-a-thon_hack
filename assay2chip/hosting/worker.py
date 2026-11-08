"""The worker pool: slot threads that lease jobs from the queue and run them.

Each job is a subprocess in its own process group, in the run's workspace, so a stage that
crashes or spins cannot take the worker down, and cancelling a job kills everything it
started (mpirun and the solver ranks included).

While a job runs its slot renews the lease every few seconds and checks for a cancel. On
a clean stop (Ctrl-C, SIGTERM, `a2c stop`) running jobs are terminated and handed back to
the queue WITHOUT spending an attempt, so they start again on the next `a2c start`. After
a hard kill the next start finds them still marked running under a dead owner and requeues
them, which does spend an attempt.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import commands
from .orchestrator import Orchestrator, collect
from .queue import CANCELLED, DONE, DONE_WITH_ERRORS, FAILED, Queue

log = logging.getLogger("a2c.worker")

STAGE_LINE = re.compile(r"^ {2}s\d+_")
MAX_LOG_EVENTS = 2000
TERM_GRACE_S = 10.0


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)


class Slot(threading.Thread):
    def __init__(self, worker: "Worker", resource: str, index: int):
        super().__init__(name="slot-%s-%d" % (resource, index), daemon=True)
        self.worker = worker
        self.resource = resource
        self.owner = "%s%s-%d" % (worker.owner_prefix, resource, index)
        self.current: Optional[dict] = None
        self.proc: Optional[subprocess.Popen] = None

    def run(self) -> None:
        w = self.worker
        while not w.stopping.is_set():
            try:
                job = w.q.lease(self.resource, self.owner, w.settings.lease_s)
            except Exception as problem:            # a locked database, say: try again
                log.warning("lease failed: %s", problem)
                job = None
            if job is None:
                w.stopping.wait(w.settings.poll_s)
                continue
            self.current = job
            try:
                self.execute(job)
            except Exception as problem:
                log.exception("job %s crashed the slot", job["id"])
                w.q.finish(job["id"], self.owner, FAILED, error="worker error: %s" % problem,
                           retry=True)
                w.orch.advance(job["run_id"])
            finally:
                self.current = None
                self.proc = None

    # --- one job ------------------------------------------------------------------

    def _restore(self, run_id: str):
        w = self.worker

        def restore(dest: Path) -> None:
            rels = []
            for art in w.q.artifacts(run_id):
                if art["path"].startswith("_"):
                    continue
                w.store.download(art["key"], dest / art["path"])
                rels.append(art["path"])
            w.ws.mark_synced(run_id, rels)
        return restore

    def execute(self, job: dict) -> None:
        w = self.worker
        run = w.q.run(job["run_id"])
        kind = job["kind"]
        workspace = w.ws.ensure(run["id"], self._restore(run["id"]))
        log.info("start %s %s (attempt %d/%d)", run["id"], job["key"], job["attempts"],
                 job["max_attempts"])
        if kind == "collect":
            summary = collect(w.q, workspace, run["id"])
            self._sync(run["id"], job)
            w.q.finish(job["id"], self.owner, DONE, exit_code=0,
                       result={"counts": summary["counts"]})
            w.orch.advance(run["id"])
            return

        argv = commands.build_argv(kind, job, run, w.settings)
        env = commands.build_env(kind, run, w.settings, foam_cores=w.plan.foam_cores_per_case)
        log_path = w.settings.logs / "jobs" / run["id"] / ("%s.log" % _safe(job["key"]))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        started = time.time()
        manifest = workspace / "assay2chip" / "work" / run["id"] / "run_manifest.json"

        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write("\n=== %s attempt %d  %s\n$ %s\n" % (
                job["key"], job["attempts"], time.strftime("%Y-%m-%d %H:%M:%S"),
                " ".join(argv)))
            handle.flush()
            self.proc = subprocess.Popen(argv, cwd=str(workspace), env=env,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, start_new_session=True,
                                         text=True, bufsize=1, errors="replace")
            reader = threading.Thread(target=self._pump, args=(job, handle), daemon=True)
            reader.start()
            outcome = self._watch(job, commands.timeout_for(kind, w.settings))
            reader.join(timeout=30)
            rc = self.proc.returncode
            handle.write("=== exit %s (%s) after %.1fs\n" % (rc, outcome, time.time() - started))

        if outcome == "stopping":
            w.q.release(job["id"], self.owner, "worker stopped; will resume on next start")
            log.info("released %s %s for the next start", run["id"], job["key"])
            return

        result: Dict = {"seconds": round(time.time() - started, 2)}
        if kind in commands.SEGMENT and manifest.exists() and \
                manifest.stat().st_mtime >= started - 1:
            copy = manifest.with_name("manifests") / ("%s.json" % kind)
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_bytes(manifest.read_bytes())
            result["stages"] = self._stage_summary(manifest)
        self._sync(run["id"], job, log_path)

        if outcome == "cancelled":
            w.q.finish(job["id"], self.owner, CANCELLED, exit_code=rc, error="cancelled")
        elif outcome == "timeout":
            w.q.finish(job["id"], self.owner, FAILED, exit_code=rc, retry=True,
                       error="timed out after %ds" % commands.timeout_for(kind, w.settings))
        elif rc == 0:
            w.q.finish(job["id"], self.owner, DONE, exit_code=0, result=result)
        elif rc == 1 and "stages" in result:
            errors = [s for s in result["stages"] if s["status"] not in ("ok", "skipped",
                                                                          "not_implemented")]
            w.q.finish(job["id"], self.owner, DONE_WITH_ERRORS, exit_code=1, result=result,
                       error="; ".join("%s %s" % (s["stage"], s["status"]) for s in errors)
                       or "a stage reported an error")
        else:
            w.q.finish(job["id"], self.owner, FAILED, exit_code=rc, result=result,
                       retry=True, error="exit code %s; see the job log" % rc)
        log.info("end %s %s rc=%s %s", run["id"], job["key"], rc, outcome)
        w.orch.advance(run["id"])

    def _pump(self, job: dict, handle) -> None:
        count = 0
        assert self.proc is not None and self.proc.stdout is not None
        for line in self.proc.stdout:
            handle.write(line)
            handle.flush()
            line = line.rstrip("\n")
            count += 1
            if count <= MAX_LOG_EVENTS or STAGE_LINE.match(line):
                try:
                    self.worker.q.event(job["run_id"], "log", {"job": job["key"], "line": line})
                except Exception:
                    pass

    def _watch(self, job: dict, timeout_s: int) -> str:
        w = self.worker
        deadline = time.time() + timeout_s
        beat = max(1.0, min(5.0, w.settings.lease_s / 4.0))
        last = 0.0
        assert self.proc is not None
        while self.proc.poll() is None:
            if w.stopping.is_set():
                self._kill()
                return "stopping"
            if time.time() > deadline:
                self._kill()
                return "timeout"
            if time.time() - last >= beat:
                last = time.time()
                try:
                    if w.q.heartbeat(job["id"], self.owner, w.settings.lease_s):
                        self._kill()
                        return "cancelled"
                except Exception as problem:
                    log.warning("heartbeat failed: %s", problem)
            time.sleep(0.2)
        return "exited"

    def _kill(self) -> None:
        proc = self.proc
        if proc is None or proc.poll() is not None:
            return
        for sig, wait in ((signal.SIGTERM, TERM_GRACE_S), (signal.SIGKILL, 5.0)):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                proc.wait(timeout=wait)
                return
            except subprocess.TimeoutExpired:
                continue

    def _sync(self, run_id: str, job: dict, log_path: Optional[Path] = None) -> None:
        w = self.worker
        records = w.ws.sync(run_id, w.store, job_id=job["id"])
        if log_path is not None and log_path.exists():
            key = "runs/%s/_logs/%s" % (run_id, log_path.name)
            data = log_path.read_bytes()
            w.store.put_bytes(key, data)
            import hashlib
            records.append({"path": "_logs/%s" % log_path.name, "key": key,
                            "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                            "kind": "log", "job_id": job["id"]})
        w.q.record_artifacts(run_id, records)

    @staticmethod
    def _stage_summary(manifest: Path) -> List[dict]:
        try:
            data = json.loads(manifest.read_text())
        except (OSError, ValueError):
            return []
        return [{"stage": s.get("stage"), "name": s.get("name"), "status": s.get("status"),
                 "seconds": s.get("duration_s")} for s in data.get("stages", [])]


class Worker:
    def __init__(self, settings, queue: Queue, store, workspaces, plan):
        self.settings = settings
        self.q = queue
        self.store = store
        self.ws = workspaces
        self.plan = plan
        self.orch = Orchestrator(queue, workspaces, settings)
        self.owner_prefix = "%s:%d:" % (socket.gethostname(), os.getpid())
        self.stopping = threading.Event()
        self.slots: List[Slot] = []
        self.sweeper: Optional[threading.Thread] = None

    def start(self) -> None:
        back = self.q.reclaim(live_owner_prefix=self.owner_prefix)
        if back:
            log.info("requeued %d job(s) left running by a previous process", len(back))
        for run_id in self.q.active_run_ids():
            self.orch.advance(run_id)
        self.slots = ([Slot(self, "light", i) for i in range(self.plan.light_slots)] +
                      [Slot(self, "foam", i) for i in range(self.plan.foam_slots)])
        for slot in self.slots:
            slot.start()
        self.sweeper = threading.Thread(target=self._sweep, name="sweeper", daemon=True)
        self.sweeper.start()

    def _sweep(self) -> None:
        ttl_days = float(os.environ.get("A2C_WORKSPACE_TTL_DAYS", "7"))
        last_clean = 0.0
        while not self.stopping.wait(5.0):
            try:
                self.q.reclaim()
                for run_id in self.q.active_run_ids():
                    self.orch.advance(run_id)
                if time.time() - last_clean > 3600:
                    last_clean = time.time()
                    gone = self.ws.remove_older_than(ttl_days, self.q.active_run_ids())
                    if gone:
                        log.info("removed %d old workspace(s)", len(gone))
            except Exception:
                log.exception("sweep failed")

    def status(self) -> List[dict]:
        out = []
        for slot in self.slots:
            job = slot.current
            out.append({"slot": slot.name, "resource": slot.resource,
                        "job": None if job is None else {
                            "run_id": job["run_id"], "key": job["key"],
                            "attempt": job["attempts"],
                            "running_s": round(time.time() - (job["started"] or time.time()),
                                               1)}})
        return out

    def stop(self, timeout: float = 30.0) -> None:
        self.stopping.set()
        for slot in self.slots:
            slot.join(timeout=timeout)

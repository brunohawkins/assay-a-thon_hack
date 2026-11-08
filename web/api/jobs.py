"""Running the pipeline as a subprocess, one job at a time, and streaming what it does.

Why a subprocess and not an import: `assay2chip/run.py` is a CLI with its own argument
handling, it is being written by other people while this server runs, and a stage that
crashes or spins must not be able to take the web server with it. The server stays
standard-library only; the pipeline gets the interpreter that is running the server.

Why one at a time: stages S5-S11 write into the shared `chip/`, `protocols/` and `cad/`
trees. Two runs at once would interleave writes into the same files, and run.py's
produced-file ledger would not be able to tell which run owned what. So there is one
worker thread by default; extra submissions queue and their status says `queued` with how
many jobs are ahead (`--concurrency` can raise it, with that caveat).

Live progress comes from two places at once, which is deliberate:

  * the subprocess's stdout, line by line, which is what the `log` events carry and what
    gives a stage transition the moment it happens;
  * `assay2chip/work/<id>/run_manifest.json`, polled, which is the AUTHORITY. run.py
    writes it when the run ends, and the moment it appears every stage status is taken
    from it, overwriting whatever was inferred from stdout.

A stage run.py reports as NOT YET IMPLEMENTED becomes `not_implemented`, never a failure
and never a silent success. A missing script is refused before a job is created at all.
"""

from __future__ import annotations

import itertools
import os
import queue
import re
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional

from . import llm, manifest, paths, stages
from .http import ApiError

#: How often the manifest and the findings file are re-read while a job runs.
POLL_SECONDS = 0.4
#: Lines of a job's output kept for a browser that connects late.
LOG_BUFFER_LINES = 4000
#: Events a single SSE client may fall behind by before events are dropped for it.
SUBSCRIBER_BACKLOG = 10000
#: How long a terminate gets before a kill, when the server shuts down.
TERMINATE_GRACE_S = 5.0

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"

# run.py prints one line per finished stage:
#   "  s0_ingest        ok                  0.12s  +1 file(s)  [llm: stub]"
#   "  s3_triage        NOT YET IMPLEMENTED (skipped)"
#   "  s6_build         ERROR  ValueError: envelope exceeded"
STAGE_LINE = re.compile(r"^ {2}(s\d+)_([a-z0-9_]+)\s{2,}(\S.*)$")
# "assay2chip: id 'my_assay', s0_ingest -> s12_report, llm stub"
HEADER_LINE = re.compile(
    r"^assay2chip: id '([^']+)',\s*(s\d+)_\S+\s*->\s*(s\d+)_\S+,\s*llm\s*(\w+)")
SECONDS = re.compile(r"(\d+(?:\.\d+)?)s(?:\s|$)")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


class Event:
    __slots__ = ("seq", "name", "data")

    def __init__(self, seq: int, name: str, data: dict):
        self.seq = seq
        self.name = name
        self.data = data


class Job:
    """One pipeline or CFD invocation, its output, and whoever is watching it."""

    def __init__(self, job_id, kind, argv, *, run_id=None, title=None,
                 llm_mode=None, scale=None, plan=None, env=None, request=None):
        self.id = job_id
        self.run_id = run_id or job_id
        self.kind = kind                       # "run" | "rerun" | "cfd"
        self.argv = list(argv)
        self.title = title or self.run_id
        self.llm = llm_mode or llm.report()
        self.scale = scale
        self.request = request or {}
        self.env = dict(env or {})
        self.status = QUEUED
        self.created = _now()
        self.started = None
        self.started_epoch = None
        self.finished = None
        self.returncode = None
        self.message = ""
        self.queued_behind = 0
        self.log = deque(maxlen=LOG_BUFFER_LINES)
        self.findings: List[dict] = []
        self._finding_ids = set()
        self._counter = itertools.count(1)
        self._events = deque(maxlen=LOG_BUFFER_LINES)
        self._key_events: List[Event] = []     # stage / finding / done, never dropped
        self._subscribers: Dict[int, queue.Queue] = {}
        self._sub_ids = itertools.count(1)
        self.lock = threading.RLock()
        self.process = None
        self.view = manifest.StageView(plan) if kind in ("run", "rerun") else None
        self._simple = {
            "stage": "cfd", "name": "cfd", "label": "cfd", "status": "pending",
            "started": None, "seconds": None, "outputs": [], "error": None,
            "description": "assay2chip/cfd/run.py over the whole chip set",
        }

    # --- events ---------------------------------------------------------------

    def emit(self, name: str, data: dict) -> Event:
        with self.lock:
            event = Event(next(self._counter), name, dict(data))
            self._events.append(event)
            if name != "log":
                self._key_events.append(event)
            for key, sink in list(self._subscribers.items()):
                try:
                    sink.put_nowait(event)
                except queue.Full:
                    pass                        # this client is too slow; it keeps the rest
        return event

    def emit_log(self, line: str) -> None:
        self.log.append(line)
        self.emit("log", {"line": line, "stream": "stdout", "at": _now()})

    def subscribe(self):
        """(events so far, sink). Registered under the lock so nothing slips between."""
        sink = queue.Queue(maxsize=SUBSCRIBER_BACKLOG)
        with self.lock:
            key = next(self._sub_ids)
            self._subscribers[key] = sink
            backlog = {event.seq: event for event in self._key_events}
            backlog.update({event.seq: event for event in self._events})
            replay = [backlog[seq] for seq in sorted(backlog)]
        return replay, (key, sink)

    def unsubscribe(self, handle) -> None:
        with self.lock:
            self._subscribers.pop(handle[0], None)

    # --- the stage timeline ---------------------------------------------------

    def rows(self) -> List[dict]:
        if self.view is not None:
            return self.view.list()
        return [dict(self._simple)]

    def _emit_rows(self, changed) -> None:
        for row in changed:
            self.emit("stage", row)

    def _simple_status(self, status, seconds=None, error=None) -> None:
        self._simple["status"] = status
        if status == "running" and not self._simple["started"]:
            self._simple["started"] = _now()
        if seconds is not None:
            self._simple["seconds"] = seconds
        self._simple["error"] = error
        self.emit("stage", dict(self._simple))

    # --- what the API reports -------------------------------------------------

    def summary(self) -> dict:
        rows = self.rows()
        counts = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        data = {
            "run_id": self.run_id,
            "job_id": self.id,
            "kind": self.kind,
            "title": self.title,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "status": self.status,
            "returncode": self.returncode,
            "llm": self.llm.get("mode"),
            "llm_detail": self.llm,
            "scale": self.scale,
            "command": " ".join(self.argv),
            "stages": rows,
            "stage_counts": counts,
            "findings_by_severity": manifest.findings_by_severity(self.run_id, self.findings),
            "findings_total": len(self.findings),
            "chips": manifest.chip_ids(self.run_id),
            "message": self.message,
        }
        if self.status == QUEUED:
            data["queued_behind"] = self.queued_behind
            data["message"] = self.message or (
                "queued behind %d job(s): stages S5-S11 write into the shared chip/ and "
                "protocols/ trees, so runs are executed one at a time"
                % self.queued_behind)
        return data


class JobManager:
    """The queue, the worker threads, and the job registry keyed by run id."""

    def __init__(self, concurrency: int = 1):
        self.concurrency = max(1, int(concurrency))
        self._queue: "queue.Queue[Optional[Job]]" = queue.Queue()
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []
        self._pending: List[Job] = []
        self._lock = threading.RLock()
        self._stopping = threading.Event()
        self._workers = []
        self._cfd_seq = itertools.count(1)
        for index in range(self.concurrency):
            worker = threading.Thread(target=self._work, name="job-worker-%d" % index,
                                      daemon=True)
            worker.start()
            self._workers.append(worker)

    # --- registry -------------------------------------------------------------

    def get(self, run_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(run_id)

    def all(self) -> List[Job]:
        with self._lock:
            return [self._jobs[key] for key in reversed(self._order) if key in self._jobs]

    def active(self) -> List[Job]:
        return [job for job in self.all() if job.status in (QUEUED, RUNNING)]

    def queue_state(self) -> dict:
        jobs = self.all()
        return {
            "concurrency": self.concurrency,
            "running": [job.run_id for job in jobs if job.status == RUNNING],
            "queued": [job.run_id for job in jobs if job.status == QUEUED],
            "note": "one run at a time: stages S5-S11 write into the shared chip/ and "
                    "protocols/ trees",
        }

    def cfd_job_id(self, subject: str) -> str:
        return paths.sanitise_id("cfd_%s_%d" % (subject, next(self._cfd_seq)),
                                 fallback="cfd_job")

    # --- submitting -----------------------------------------------------------

    def require_script(self, script: Path, what: str, hint: str) -> Path:
        """503 while the script is still being written, with a message that says which.

        The pipeline and the CFD CLI are built in parallel with this server, so "not
        there yet" is a normal state and must read as one - never a 500 and never a run
        that pretends to have worked.
        """
        if not Path(script).is_file():
            raise ApiError(
                503, "script_not_built",
                "%s is not in this checkout yet, so %s cannot run. %s"
                % (paths.repo_rel(script), what, hint),
                script=paths.repo_rel(script), available=False)
        return Path(script)

    def submit(self, job: Job) -> Job:
        with self._lock:
            existing = self._jobs.get(job.run_id)
            if existing is not None and existing.status in (QUEUED, RUNNING):
                raise ApiError(
                    409, "already_running",
                    "run %r is %s already; watch /api/runs/%s/events instead of starting "
                    "a second one" % (job.run_id, existing.status, job.run_id),
                    run_id=job.run_id, run_status=existing.status)
            self._jobs[job.run_id] = job
            if job.run_id in self._order:
                self._order.remove(job.run_id)
            self._order.append(job.run_id)
            self._pending.append(job)
            ahead = max(0, len(self._pending) - 1 + len(
                [j for j in self._jobs.values() if j.status == RUNNING]) - self.concurrency + 1)
            job.queued_behind = max(0, ahead)
        if job.queued_behind:
            job.message = (
                "queued behind %d job(s): runs are executed one at a time because stages "
                "S5-S11 write into the shared chip/ and protocols/ trees"
                % job.queued_behind)
            job.emit_log(job.message)
        for row in job.rows():          # the whole timeline up front, so a client that
            job.emit("stage", row)      # connects immediately has every stage to draw
        self._queue.put(job)
        return job

    # --- the worker -----------------------------------------------------------

    def _work(self) -> None:
        while not self._stopping.is_set():
            job = self._queue.get()
            if job is None:
                return
            with self._lock:
                if job in self._pending:
                    self._pending.remove(job)
                for other in self._pending:
                    other.queued_behind = max(0, other.queued_behind - 1)
            try:
                self._execute(job)
            except Exception as problem:               # never lose the worker
                job.status = FAILED
                job.finished = _now()
                job.message = "the server failed to run the job: %s: %s" % (
                    type(problem).__name__, problem)
                job.emit_log(job.message)
                job.emit("done", job.summary())
            finally:
                self._queue.task_done()

    def _execute(self, job: Job) -> None:
        job.status = RUNNING
        job.started = _now()
        job.started_epoch = time.time()
        job.queued_behind = 0
        job.emit_log("$ %s" % " ".join(job.argv))
        job.emit_log("llm mode: %s%s" % (job.llm.get("mode"),
                                         " - " + job.llm["note"] if job.llm.get("note") else ""))
        if job.view is not None:
            first = job.view.next_pending()
            if first:
                job._emit_rows(job.view.note_running(first))
        else:
            job._simple_status("running")

        environment = os.environ.copy()
        environment.update({
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": os.pathsep.join(
                [str(paths.ROOT)] + ([environment["PYTHONPATH"]]
                                     if environment.get("PYTHONPATH") else [])),
        })
        environment.update(job.env)

        try:
            job.process = subprocess.Popen(
                job.argv, cwd=str(paths.ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, env=environment, bufsize=1,
                universal_newlines=True,
            )
        except OSError as problem:
            job.status = FAILED
            job.finished = _now()
            job.message = "could not start %s: %s" % (job.argv[1] if len(job.argv) > 1
                                                      else job.argv[0], problem)
            job.emit_log(job.message)
            if job.view is not None:
                job._emit_rows(job.view.note_finished(job.view.next_pending() or "s0",
                                                      "error", None, job.message))
            else:
                job._simple_status(FAILED, error=job.message)
            job.emit("done", job.summary())
            return

        lines: "queue.Queue[Optional[str]]" = queue.Queue()
        reader = threading.Thread(target=_pump, args=(job.process.stdout, lines),
                                  name="job-reader-%s" % job.id, daemon=True)
        reader.start()

        last_stage = None
        manifest_stamp = None
        findings_stamp = {}
        while True:
            try:
                line = lines.get(timeout=POLL_SECONDS)
            except queue.Empty:
                line = ""
            if line is None:
                break
            if line:
                job.emit_log(line)
                last_stage = self._read_line(job, line, last_stage)
            manifest_stamp = self._poll_manifest(job, manifest_stamp)
            findings_stamp = self._poll_findings(job, findings_stamp)

        returncode = job.process.wait()
        reader.join(timeout=2.0)
        job.returncode = returncode
        self._poll_manifest(job, None, force=True)
        self._poll_findings(job, {}, force=True)
        self._finish(job, returncode)

    # --- reading the subprocess ----------------------------------------------

    def _read_line(self, job: Job, line: str, last_stage) -> Optional[str]:
        """Turn one line of run.py's output into stage events. Best effort by design:
        the manifest is the authority and overwrites anything inferred here."""
        if job.view is None:
            return last_stage
        header = HEADER_LINE.match(line)
        if header:
            start, stop = stages.resolve(header.group(2)), stages.resolve(header.group(3))
            if start is not None and stop is not None and stop >= start:
                tokens = [stages.token(number)
                          for number, _, _, _ in stages.SPINE[start:stop + 1]]
                job._emit_rows(job.view.set_plan(tokens))
                nxt = job.view.next_pending()
                if nxt:
                    job._emit_rows(job.view.note_running(nxt))
            return last_stage
        found = STAGE_LINE.match(line)
        if not found:
            return last_stage
        token, rest = found.group(1), found.group(3).strip()
        if rest.startswith("NOT YET IMPLEMENTED"):
            status, message = "not_implemented", (
                "NOT YET IMPLEMENTED: no module for this stage in this checkout yet")
        elif rest.startswith("ERROR"):
            status, message = "error", rest[5:].strip() or "the stage reported an error"
        else:
            status, message = rest.split()[0], None
        seconds = None
        duration = SECONDS.search(rest)
        if duration:
            seconds = float(duration.group(1))
        job._emit_rows(job.view.note_finished(token, status, seconds, message))
        nxt = job.view.next_pending(token)
        if nxt:
            job._emit_rows(job.view.note_running(nxt))
        return token

    def _poll_manifest(self, job: Job, stamp, force: bool = False):
        """Re-read run_manifest.json when it changes and emit every status change."""
        if job.view is None:
            return stamp
        path = manifest.manifest_path(job.run_id)
        try:
            current = path.stat().st_mtime_ns, path.stat().st_size
        except OSError:
            return stamp
        if current == stamp and not force:
            return stamp
        data = manifest.load(job.run_id)
        if data is not None:
            job._emit_rows(job.view.apply_manifest(data))
            job.title = manifest.title(job.run_id, data)
            reported = (data.get("llm") or {}).get("mode")
            if reported and reported != job.llm.get("mode"):
                job.llm = dict(job.llm, mode=reported, reported_by="run_manifest.json")
        return current

    def _poll_findings(self, job: Job, stamps: dict, force: bool = False) -> dict:
        """Emit a `finding` event for every CFD finding that has appeared since last time.

        A pipeline run writes them to its own work directory. A standalone CFD job is not
        given a run id by `assay2chip/cfd/run.py` (its CLI takes --pipeline / --chip), so
        every work directory touched since the job started is checked too.
        """
        stamps = dict(stamps or {})
        for path in self._findings_files(job):
            try:
                current = path.stat().st_mtime_ns, path.stat().st_size
            except OSError:
                continue
            if stamps.get(path) == current and not force:
                continue
            stamps[path] = current
            data = manifest.read_json(path)
            if isinstance(data, dict):
                data = data.get("findings")
            if not isinstance(data, list):
                continue
            for index, item in enumerate(data):
                if not isinstance(item, dict):
                    continue
                key = manifest.finding_id(item, index)
                if key in job._finding_ids:
                    continue
                job._finding_ids.add(key)
                job.findings.append(item)
                job.emit("finding", dict(item, id=item.get("id", key),
                                         source=paths.repo_rel(path)))
        return stamps

    def _findings_files(self, job: Job) -> List[Path]:
        """Where this job's findings could be.

        A pipeline run writes them under its own id. `assay2chip/cfd/run.py` is given a
        --pipeline or a --chip and picks the directory itself, so a standalone job also
        looks at the directory named after its subject and at any other work directory
        whose findings file has been WRITTEN SINCE THE JOB STARTED. The mtime test is the
        point: without it another run's stale findings would be reported as this job's.
        """
        found = [manifest.work_dir(job.run_id) / paths.FINDINGS_REL]
        if job.kind == "cfd":
            hint = job.request.get("chip") or job.request.get("pipeline")
            if hint:
                found.append(manifest.work_dir(Path(str(hint)).stem) / paths.FINDINGS_REL)
            since = (job.started_epoch or time.time()) - 1.0
            if paths.WORK.is_dir():
                for candidate in sorted(paths.WORK.glob("*/" + paths.FINDINGS_REL)):
                    try:
                        fresh = candidate.stat().st_mtime >= since
                    except OSError:
                        fresh = False
                    if fresh and candidate not in found:
                        found.append(candidate)
        return [path for path in dict.fromkeys(found) if path.is_file()]

    def _finish(self, job: Job, returncode: int) -> None:
        rows = job.rows()
        if job.view is not None:
            # Whatever the manifest records is already in the view (source "manifest").
            # These are the stages it says nothing about: either the orchestrator stopped
            # before reaching them, or it died before writing the file at all. Those are
            # different, and only the second is a failure of that stage.
            data = manifest.load(job.run_id)
            recorded = {str(entry.get("stage")) for entry in (data or {}).get("stages") or []}
            stopped_short = ("the run stopped before this stage (it exited with code %d)"
                             % returncode)
            changed = []
            for row in rows:
                planned = job.view.plan is None or row["stage"] in job.view.plan
                if row["status"] == "running":
                    if data is not None and row["stage"] not in recorded:
                        changed += job.view.note_finished(row["stage"], "skipped", None,
                                                          stopped_short)
                    else:
                        changed += job.view.note_finished(
                            row["stage"], "error" if returncode else "ok", None,
                            "the run exited with code %d during this stage; %s has no "
                            "entry for it" % (returncode, paths.MANIFEST_NAME)
                            if returncode else None)
                elif row["status"] == "pending" and planned:
                    changed += job.view.note_finished(row["stage"], "skipped", None,
                                                      stopped_short)
            job._emit_rows(changed)
            rows = job.rows()
            failed = [row for row in rows if row["status"] == "failed"]
            job.status = FAILED if (returncode or failed) else DONE
        else:
            job.status = FAILED if returncode else DONE
            job._simple_status(
                job.status,
                error=("assay2chip/cfd/run.py exited with code %d" % returncode)
                if returncode else None)
        job.finished = _now()
        not_done = [row["stage"] for row in rows if row["status"] == "not_implemented"]
        parts = ["exit code %d" % returncode]
        if not_done:
            parts.append("%d stage(s) NOT YET IMPLEMENTED: %s"
                         % (len(not_done), ", ".join(not_done)))
        if job.kind in ("run", "rerun") and job.llm.get("mode") == llm.STUB:
            parts.append("llm: stub, so no extraction on this run is a real model reading")
        job.message = "; ".join(parts)
        job.emit_log(job.message)
        job.emit("done", job.summary())

    # --- shutdown -------------------------------------------------------------

    def shutdown(self) -> None:
        self._stopping.set()
        for job in self.all():
            process = job.process
            if process is not None and process.poll() is None:
                try:
                    process.terminate()
                    deadline = time.time() + TERMINATE_GRACE_S
                    while process.poll() is None and time.time() < deadline:
                        time.sleep(0.1)
                    if process.poll() is None:
                        process.kill()
                except OSError:
                    pass
        for _ in self._workers:
            self._queue.put(None)


def _pump(stream, sink: "queue.Queue") -> None:
    try:
        for line in iter(stream.readline, ""):
            sink.put(line.rstrip("\n"))
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass
        sink.put(None)


def python_executable() -> str:
    """The interpreter the pipeline is run with: the one running this server."""
    return sys.executable or "python3"

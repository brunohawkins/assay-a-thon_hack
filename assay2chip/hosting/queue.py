"""The durable job queue and run state: one SQLite file in WAL mode.

Why SQLite: everything runs on one machine, and SQLite gives transactional leasing, crash
safety and zero administration. A job survives a restart because it is a row; a job that
was running when the process died is put back in the queue on the next start (or when its
lease expires), and its retry budget says how many more chances it gets.

    runs       one per submitted assay: status, options, cancel flag
    jobs       one per unit of work: kind, resource class (light | foam), status,
               attempts / max_attempts, lease, idempotency key (UNIQUE per run)
    events     append-only log the API streams as Server-Sent Events
    artifacts  every file a run produced, with its object-store key, size and hash

Job states: queued -> running -> done | done_with_errors | failed | cancelled.
`done_with_errors` is a pipeline segment that ran to the end but had a stage report an
error: deterministic, so it is never retried, and the run carries on so the report can say
what went wrong.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

QUEUED, RUNNING, DONE, DONE_WITH_ERRORS, FAILED, CANCELLED = (
    "queued", "running", "done", "done_with_errors", "failed", "cancelled")
TERMINAL = (DONE, DONE_WITH_ERRORS, FAILED, CANCELLED)
OK = (DONE, DONE_WITH_ERRORS)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    title TEXT,
    created REAL NOT NULL,
    updated REAL NOT NULL,
    status TEXT NOT NULL,
    idem_key TEXT UNIQUE,
    paper TEXT,
    options TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id),
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    resource TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    priority INTEGER NOT NULL DEFAULT 100,
    not_before REAL NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_until REAL,
    created REAL NOT NULL,
    started REAL,
    finished REAL,
    exit_code INTEGER,
    error TEXT,
    result TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    UNIQUE (run_id, key)
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (status, resource, priority, created);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    at REAL NOT NULL,
    name TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_run ON events (run_id, seq);
CREATE TABLE IF NOT EXISTS artifacts (
    run_id TEXT NOT NULL,
    path TEXT NOT NULL,
    key TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    job_id TEXT,
    at REAL NOT NULL,
    PRIMARY KEY (run_id, path)
);
"""


def _row(row: Optional[sqlite3.Row]) -> Optional[dict]:
    if row is None:
        return None
    out = dict(row)
    for name in ("payload", "options", "result", "data"):
        if name in out and isinstance(out[name], str):
            try:
                out[name] = json.loads(out[name])
            except ValueError:
                pass
    return out


class Queue:
    """Thread-safe: one connection per thread, every write in BEGIN IMMEDIATE."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self.conn().executescript(SCHEMA)            # idempotent; commits itself

    # --- plumbing -----------------------------------------------------------------

    def conn(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(str(self.path), timeout=30, isolation_level=None,
                                 check_same_thread=False)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA busy_timeout=30000")
            self._local.db = db
        return db

    class _Tx:
        def __init__(self, db):
            self.db = db

        def __enter__(self):
            self.db.execute("BEGIN IMMEDIATE")
            return self.db

        def __exit__(self, kind, value, tb):
            self.db.execute("COMMIT" if kind is None else "ROLLBACK")
            return False

    def tx(self):
        return Queue._Tx(self.conn())

    def close(self) -> None:
        db = getattr(self._local, "db", None)
        if db is not None:
            db.close()
            self._local.db = None

    # --- runs ---------------------------------------------------------------------

    def create_run(self, run_id: str, *, title: str, paper: Optional[str],
                   options: dict, idem_key: Optional[str] = None) -> dict:
        """A new run, or the existing one when `idem_key` was seen before."""
        now = time.time()
        with self.tx() as db:
            if idem_key:
                found = db.execute("SELECT * FROM runs WHERE idem_key=?",
                                   (idem_key,)).fetchone()
                if found:
                    return dict(_row(found), existing=True)
            db.execute("INSERT INTO runs (id, title, created, updated, status, idem_key, "
                       "paper, options) VALUES (?,?,?,?,?,?,?,?)",
                       (run_id, title, now, now, QUEUED, idem_key, paper,
                        json.dumps(options)))
            self._event(db, run_id, "run", {"status": QUEUED})
        return dict(self.run(run_id), existing=False)

    def run(self, run_id: str) -> Optional[dict]:
        return _row(self.conn().execute("SELECT * FROM runs WHERE id=?",
                                        (run_id,)).fetchone())

    def runs(self, limit: int = 200) -> List[dict]:
        rows = self.conn().execute("SELECT * FROM runs ORDER BY created DESC LIMIT ?",
                                   (limit,)).fetchall()
        return [_row(r) for r in rows]

    def active_run_ids(self) -> List[str]:
        rows = self.conn().execute(
            "SELECT id FROM runs WHERE status IN ('queued','running','cancelling')").fetchall()
        return [r["id"] for r in rows]

    def set_run_status(self, run_id: str, status: str, error: Optional[str] = None) -> bool:
        """True when the status actually changed."""
        with self.tx() as db:
            old = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if old is None or old["status"] == status:
                return False
            db.execute("UPDATE runs SET status=?, error=COALESCE(?, error), updated=? "
                       "WHERE id=?", (status, error, time.time(), run_id))
            self._event(db, run_id, "run", {"status": status, "error": error})
        return True

    def cancel_run(self, run_id: str) -> bool:
        with self.tx() as db:
            run = db.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                return False
            if run["status"] in ("done", "done_with_errors", "failed", "cancelled"):
                return True
            now = time.time()
            db.execute("UPDATE runs SET cancel_requested=1, status='cancelling', updated=? "
                       "WHERE id=?", (now, run_id))
            db.execute("UPDATE jobs SET status='cancelled', finished=?, "
                       "error='cancelled before it started' "
                       "WHERE run_id=? AND status='queued'", (now, run_id))
            db.execute("UPDATE jobs SET cancel_requested=1 WHERE run_id=? AND "
                       "status='running'", (run_id,))
            self._event(db, run_id, "run", {"status": "cancelling"})
        return True

    def retry_run(self, run_id: str) -> int:
        """Requeue a run's failed and cancelled jobs with a fresh retry budget."""
        with self.tx() as db:
            n = db.execute("UPDATE jobs SET status='queued', attempts=0, not_before=0, "
                           "cancel_requested=0, error=NULL, finished=NULL WHERE run_id=? "
                           "AND status IN ('failed','cancelled')", (run_id,)).rowcount
            db.execute("UPDATE runs SET status='queued', cancel_requested=0, error=NULL, "
                       "updated=? WHERE id=?", (time.time(), run_id))
            self._event(db, run_id, "run", {"status": QUEUED, "note": "retry: %d job(s)" % n})
        return n

    # --- jobs ---------------------------------------------------------------------

    def enqueue(self, run_id: str, kind: str, key: str, *, resource: str = "light",
                payload: Optional[dict] = None, max_attempts: int = 2,
                priority: int = 100) -> dict:
        """Idempotent: a second enqueue of the same (run, key) returns the first job."""
        with self.tx() as db:
            return self._enqueue(db, run_id, kind, key, resource, payload or {},
                                 max_attempts, priority)

    def _enqueue(self, db, run_id, kind, key, resource, payload, max_attempts, priority):
        found = db.execute("SELECT * FROM jobs WHERE run_id=? AND key=?",
                           (run_id, key)).fetchone()
        if found:
            return _row(found)
        job_id = uuid.uuid4().hex[:12]
        db.execute("INSERT INTO jobs (id, run_id, kind, key, resource, payload, status, "
                   "max_attempts, priority, created) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (job_id, run_id, kind, key, resource, json.dumps(payload), QUEUED,
                    max_attempts, priority, time.time()))
        self._event(db, run_id, "job", {"job_id": job_id, "kind": kind, "key": key,
                                        "status": QUEUED})
        return _row(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def enqueue_many(self, run_id: str, specs: Iterable[dict]) -> List[dict]:
        with self.tx() as db:
            return [self._enqueue(db, run_id, s["kind"], s["key"], s.get("resource", "light"),
                                  s.get("payload") or {}, s.get("max_attempts", 2),
                                  s.get("priority", 100)) for s in specs]

    def job(self, job_id: str) -> Optional[dict]:
        return _row(self.conn().execute("SELECT * FROM jobs WHERE id=?",
                                        (job_id,)).fetchone())

    def jobs(self, run_id: str) -> List[dict]:
        rows = self.conn().execute("SELECT * FROM jobs WHERE run_id=? ORDER BY created, key",
                                   (run_id,)).fetchall()
        return [_row(r) for r in rows]

    def lease(self, resource: str, owner: str, lease_s: float) -> Optional[dict]:
        now = time.time()
        with self.tx() as db:
            row = db.execute(
                "SELECT j.id FROM jobs j JOIN runs r ON r.id=j.run_id "
                "WHERE j.status='queued' AND j.resource=? AND j.not_before<=? "
                "AND r.cancel_requested=0 ORDER BY j.priority, j.created LIMIT 1",
                (resource, now)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running', lease_owner=?, lease_until=?, "
                       "attempts=attempts+1, started=?, error=NULL WHERE id=?",
                       (owner, now + lease_s, now, row["id"]))
            job = _row(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())
            db.execute("UPDATE runs SET status='running', updated=? WHERE id=? "
                       "AND status='queued'", (now, job["run_id"]))
            self._event(db, job["run_id"], "job", {"job_id": job["id"], "kind": job["kind"],
                                                   "key": job["key"], "status": RUNNING,
                                                   "attempt": job["attempts"]})
        return job

    def heartbeat(self, job_id: str, owner: str, lease_s: float) -> bool:
        """Extend the lease. Returns True when the job should be cancelled."""
        with self.tx() as db:
            db.execute("UPDATE jobs SET lease_until=? WHERE id=? AND lease_owner=? "
                       "AND status='running'", (time.time() + lease_s, job_id, owner))
            row = db.execute("SELECT cancel_requested, status FROM jobs WHERE id=?",
                             (job_id,)).fetchone()
        return bool(row and (row["cancel_requested"] or row["status"] != RUNNING))

    def finish(self, job_id: str, owner: str, status: str, *, exit_code: Optional[int] = None,
               result: Optional[dict] = None, error: Optional[str] = None,
               retry: bool = False, backoff_s: float = 5.0) -> dict:
        """Record the outcome. A retryable failure with budget left goes back in the queue."""
        now = time.time()
        with self.tx() as db:
            job = _row(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            if job is None or job["status"] != RUNNING or job["lease_owner"] != owner:
                return job or {}
            if job["cancel_requested"] and status not in OK:
                status, retry = CANCELLED, False
            if status == FAILED and retry and job["attempts"] < job["max_attempts"]:
                delay = backoff_s * (2 ** (job["attempts"] - 1))
                db.execute("UPDATE jobs SET status='queued', lease_owner=NULL, "
                           "lease_until=NULL, not_before=?, exit_code=?, error=? WHERE id=?",
                           (now + delay, exit_code, error, job_id))
                status_out = "retrying"
            else:
                db.execute("UPDATE jobs SET status=?, finished=?, exit_code=?, error=?, "
                           "result=?, lease_owner=NULL, lease_until=NULL WHERE id=?",
                           (status, now, exit_code, error, json.dumps(result or {}), job_id))
                status_out = status
            self._event(db, job["run_id"], "job", {
                "job_id": job_id, "kind": job["kind"], "key": job["key"],
                "status": status_out, "exit_code": exit_code, "error": error,
                "attempt": job["attempts"]})
            return _row(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def release(self, job_id: str, owner: str, reason: str) -> None:
        """Give a job back WITHOUT spending an attempt (a clean shutdown is not its fault)."""
        with self.tx() as db:
            job = db.execute("SELECT run_id, kind, key FROM jobs WHERE id=? AND "
                             "lease_owner=? AND status='running'", (job_id, owner)).fetchone()
            if job is None:
                return
            db.execute("UPDATE jobs SET status='queued', attempts=MAX(0, attempts-1), "
                       "lease_owner=NULL, lease_until=NULL, not_before=0 WHERE id=?",
                       (job_id,))
            self._event(db, job["run_id"], "job", {"job_id": job_id, "kind": job["kind"],
                                                   "key": job["key"], "status": QUEUED,
                                                   "note": reason})

    def reclaim(self, *, live_owner_prefix: Optional[str] = None,
                now: Optional[float] = None) -> List[str]:
        """Requeue running jobs whose lease expired, or - given the prefix every owner id
        of the live process starts with - whose owner is another, dead process."""
        now = time.time() if now is None else now
        back: List[str] = []
        with self.tx() as db:
            rows = db.execute("SELECT * FROM jobs WHERE status='running'").fetchall()
            for row in rows:
                expired = (row["lease_until"] or 0) < now
                orphan = (live_owner_prefix is not None and
                          not str(row["lease_owner"] or "").startswith(live_owner_prefix))
                if not (expired or orphan):
                    continue
                why = "lease expired" if expired else "its worker is gone (restart)"
                if row["cancel_requested"]:
                    db.execute("UPDATE jobs SET status='cancelled', finished=? WHERE id=?",
                               (now, row["id"]))
                elif row["attempts"] >= row["max_attempts"]:
                    db.execute("UPDATE jobs SET status='failed', finished=?, error=? "
                               "WHERE id=?", (now, why + "; no attempts left", row["id"]))
                else:
                    db.execute("UPDATE jobs SET status='queued', lease_owner=NULL, "
                               "lease_until=NULL, not_before=0, error=? WHERE id=?",
                               (why, row["id"]))
                self._event(db, row["run_id"], "job", {"job_id": row["id"],
                                                       "kind": row["kind"], "key": row["key"],
                                                       "status": "reclaimed", "note": why})
                back.append(row["id"])
        return back

    def counts(self) -> Dict[str, Dict[str, int]]:
        out: Dict[str, Dict[str, int]] = {}
        for row in self.conn().execute("SELECT resource, status, COUNT(*) n FROM jobs "
                                       "GROUP BY resource, status"):
            out.setdefault(row["resource"], {})[row["status"]] = row["n"]
        return out

    # --- events and artefacts -----------------------------------------------------

    def _event(self, db, run_id: str, name: str, data: dict) -> None:
        db.execute("INSERT INTO events (run_id, at, name, data) VALUES (?,?,?,?)",
                   (run_id, time.time(), name, json.dumps(data, default=str)))

    def event(self, run_id: str, name: str, data: dict) -> None:
        with self.tx() as db:
            self._event(db, run_id, name, data)

    def events(self, run_id: str, after: int = 0, limit: int = 500) -> List[dict]:
        rows = self.conn().execute("SELECT * FROM events WHERE run_id=? AND seq>? "
                                   "ORDER BY seq LIMIT ?", (run_id, after, limit)).fetchall()
        return [_row(r) for r in rows]

    def record_artifacts(self, run_id: str, items: List[dict]) -> None:
        if not items:
            return
        now = time.time()
        with self.tx() as db:
            db.executemany(
                "INSERT OR REPLACE INTO artifacts (run_id, path, key, bytes, sha256, kind, "
                "job_id, at) VALUES (?,?,?,?,?,?,?,?)",
                [(run_id, i["path"], i["key"], i["bytes"], i["sha256"], i["kind"],
                  i.get("job_id"), now) for i in items])

    def artifacts(self, run_id: str) -> List[dict]:
        rows = self.conn().execute("SELECT * FROM artifacts WHERE run_id=? ORDER BY path",
                                   (run_id,)).fetchall()
        return [_row(r) for r in rows]

    def artifact(self, run_id: str, path: str) -> Optional[dict]:
        return _row(self.conn().execute("SELECT * FROM artifacts WHERE run_id=? AND path=?",
                                        (run_id, path)).fetchone())


def as_json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))

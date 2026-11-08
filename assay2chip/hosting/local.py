"""Everything on this machine, on 127.0.0.1, in one process: `deploy/local/a2c`.

    a2c start [--detach]      the GUI + API (web/server.py) + the queue API + the worker pool
    a2c stop                  graceful: running jobs are handed back to the queue
    a2c status [--json]       up or down, the pool, what each slot is doing, queue depth
    a2c logs [-f] [RUN_ID]    the service log, or one run's job logs
    a2c submit FILE [--wait]  queue an assay (also: --text, --url, --no-foam, --llm real)

Ctrl-C in the foreground does the same as `a2c stop`. Everything lives in the data
directory (A2C_DATA_DIR, default ~/Library/Application Support/assay2chip): the SQLite
queue, the artefact store, one workspace per run, and logs/ - outside the repo, because
an upload may be an unpublished protocol.

PRIVATE BY DEFAULT. It binds 127.0.0.1 and refuses anything else. Because a page on any
website can still make a browser send requests to 127.0.0.1, every request must carry a
Host of 127.0.0.1 / localhost (which defeats DNS rebinding) and every POST from a browser
must come from this origin (which defeats cross-site form posts).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.hosting import config  # noqa: E402

log = logging.getLogger("a2c")
LOOPBACK = ("127.0.0.1", "localhost", "::1")


# --- the service ---------------------------------------------------------------------

def allowed_hosts(port: int) -> set:
    return {"127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port}


def _guarded_handler(web_server, api_http):
    class GuardedHandler(web_server.Handler):
        def _handle(self, method):
            hosts = allowed_hosts(self.server.server_address[1])
            origins = {"http://" + h for h in hosts}
            host = (self.headers.get("Host") or "").strip().lower()
            origin = (self.headers.get("Origin") or "").strip().lower()
            problem = None
            if host not in hosts:
                problem = "Host %r is not this machine's loopback address" % host
            elif method not in ("GET", "HEAD") and origin and origin not in origins:
                problem = "cross-origin %s from %r refused" % (method, origin)
            if problem:
                self._send(api_http.error_response(api_http.ApiError(403, "forbidden",
                                                                     problem)))
                return
            return super()._handle(method)

        def log_message(self, template, *args):
            log.debug("%s %s", self.address_string(), template % args)

    return GuardedHandler


def _setup_logging(settings, foreground: bool) -> None:
    settings.logs.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    fh = logging.handlers.RotatingFileHandler(str(settings.logs / "a2c.log"),
                                              maxBytes=10 * 1024 * 1024, backupCount=5)
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if foreground:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def _lock(settings):
    """Exactly one service per data directory, held for the life of the process."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    handle = open(settings.data_dir / "a2c.lock", "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def serve(settings, *, foreground: bool = True) -> int:
    if settings.host not in LOOPBACK:
        print("a2c: refusing to bind %r - this service is local only (127.0.0.1)"
              % settings.host, file=sys.stderr)
        return 2
    lock = _lock(settings)
    if lock is None:
        print("a2c: already running for %s (see `a2c status`)" % settings.data_dir,
              file=sys.stderr)
        return 1
    os.chmod(str(settings.data_dir), 0o700)
    _setup_logging(settings, foreground)

    from web import server as web_server
    from web.api import http as api_http
    from assay2chip.hosting.api import QueueApi
    from assay2chip.hosting.orchestrator import Orchestrator
    from assay2chip.hosting.queue import Queue
    from assay2chip.hosting.storage import UrlSigner, open_store
    from assay2chip.hosting.worker import Worker
    from assay2chip.hosting.workspace import Workspaces

    machine = config.detect_machine(settings.data_dir)
    plan = settings.plan(machine)
    q = Queue(settings.db_path)
    store = open_store(settings)
    ws = Workspaces(settings.workspaces, settings.source_root)
    worker = Worker(settings, q, store, ws, plan)
    ctx = SimpleNamespace(settings=settings, q=q, store=store, machine=machine, plan=plan,
                          orch=Orchestrator(q, ws, settings), worker=worker,
                          signer=UrlSigner.from_file(settings.data_dir / "url-signing.key"))
    api = QueueApi(ctx)

    app = web_server.App(concurrency=1)
    app.routes = api.routes() + list(app.routes)
    if hasattr(app, "pages"):
        app.pages = api.pages() + list(app.pages)
    handler = _guarded_handler(web_server, api_http)
    try:
        server = web_server.Server((settings.host, settings.port), handler, app, quiet=False)
    except OSError as problem:
        log.error("cannot bind %s:%d: %s", settings.host, settings.port, problem)
        print("a2c: cannot bind %s:%d: %s" % (settings.host, settings.port, problem),
              file=sys.stderr)
        return 2
    port = server.server_address[1]
    url = "http://127.0.0.1:%d/" % port
    runtime = settings.data_dir / "runtime.json"
    runtime.write_text(json.dumps({"pid": os.getpid(), "port": port, "url": url,
                                   "started": time.time(), "plan": plan.to_json()}))

    worker.start()
    http_thread = threading.Thread(target=server.serve_forever, name="http", daemon=True)
    http_thread.start()
    log.info("assay2chip local service on %s  (pid %d)", url, os.getpid())
    log.info("  pool: %d light slot(s), %d OpenFOAM case(s) at once on %d core(s) each "
             "[limited by %s]; machine %d cores, %.0f GB", plan.light_slots, plan.foam_slots,
             plan.foam_cores_per_case, plan.limited_by, machine.cores, machine.mem_gb)
    log.info("  queue page %squeue  data %s  store %s", url, settings.data_dir,
             store.describe())

    done = threading.Event()

    def on_signal(signum, frame):
        log.info("signal %d: stopping", signum)
        done.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    try:
        while not done.wait(1.0):
            pass
    finally:
        log.info("stopping: handing running jobs back to the queue")
        worker.stop()
        server.shutdown()
        server.server_close()
        try:
            runtime.unlink()
        except OSError:
            pass
        log.info("stopped cleanly")
        lock.close()
    return 0


# --- the client commands -------------------------------------------------------------

def _runtime(settings) -> Optional[dict]:
    path = settings.data_dir / "runtime.json"
    try:
        info = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    lock = _lock(settings)
    if lock is not None:                    # nobody holds it: not running (stale file)
        lock.close()
        return None
    return info


def _get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def cmd_start(settings, args) -> int:
    if not args.detach:
        return serve(settings, foreground=True)
    if _runtime(settings):
        print("a2c: already running: %s" % _runtime(settings)["url"])
        return 0
    settings.logs.mkdir(parents=True, exist_ok=True)
    out = open(settings.logs / "a2c.out", "a")
    proc = subprocess.Popen([sys.executable, "-m", "assay2chip.hosting.local", "start"],
                            cwd=str(ROOT), stdout=out, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True,
                            env=dict(os.environ))
    for _ in range(100):
        info = _runtime(settings)
        if info:
            try:
                _get(info["url"] + "api/queue/status")
                print("a2c: running on %s  (pid %d; logs in %s)" % (
                    info["url"], info["pid"], settings.logs))
                return 0
            except Exception:
                pass
        if proc.poll() is not None:
            print("a2c: failed to start; see %s" % (settings.logs / "a2c.out"),
                  file=sys.stderr)
            return 1
        time.sleep(0.2)
    print("a2c: started (pid %d) but not answering yet; see `a2c status`" % proc.pid)
    return 0


def cmd_stop(settings, args) -> int:
    info = _runtime(settings)
    if not info:
        print("a2c: not running")
        return 0
    pid = int(info["pid"])
    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        # The service holds the lock until its very last moment (a zombie still answers
        # kill(pid, 0), so the lock, not the pid, is the truth).
        lock = _lock(settings)
        if lock is not None:
            lock.close()
            print("a2c: stopped (pid %d)" % pid)
            return 0
        time.sleep(0.2)
    print("a2c: pid %d did not stop within %ds" % (pid, args.timeout), file=sys.stderr)
    return 1


def cmd_status(settings, args) -> int:
    info = _runtime(settings)
    if info:
        try:
            status = _get(info["url"] + "api/queue/status")
        except Exception as problem:
            status = {"ok": False, "error": str(problem)}
        status["pid"] = info["pid"]
    else:
        from assay2chip.hosting.queue import Queue
        status = {"ok": False, "running": False, "data_dir": str(settings.data_dir)}
        if settings.db_path.exists():
            q = Queue(settings.db_path)
            status["queue"] = q.counts()
            status["active_runs"] = q.active_run_ids()
    if args.json:
        print(json.dumps(status, indent=2))
        return 0 if info else 3
    if not info:
        print("a2c: not running  (data %s)" % settings.data_dir)
        if status.get("active_runs"):
            print("  %d unfinished run(s) will resume on `a2c start`: %s" % (
                len(status["active_runs"]), ", ".join(status["active_runs"])))
        return 3
    plan = status.get("plan", {})
    print("a2c: running on %s  (pid %s)" % (status.get("url"), info["pid"]))
    print("  pool    %s light, %s OpenFOAM x %s cores   store %s" % (
        plan.get("light_slots"), plan.get("foam_slots"), plan.get("foam_cores_per_case"),
        status.get("store")))
    for slot in status.get("slots", []):
        job = slot.get("job")
        print("  %-14s %s" % (slot["slot"], "idle" if not job else "%s %s (%.0fs)" % (
            job["run_id"], job["key"], job["running_s"])))
    print("  queue   %s" % json.dumps(status.get("queue", {})))
    print("  logs    %s" % (settings.logs / "a2c.log"))
    return 0


def cmd_logs(settings, args) -> int:
    path = settings.logs / "a2c.log"
    if args.run:
        files = sorted((settings.logs / "jobs" / args.run).glob("*.log"))
        if not files:
            print("a2c: no job logs for %r" % args.run, file=sys.stderr)
            return 1
        for f in files:
            print("##### %s" % f)
            sys.stdout.write(f.read_text(errors="replace"))
        return 0
    if args.follow:
        os.execvp("tail", ["tail", "-n", "100", "-F", str(path)])
    sys.stdout.write("".join(path.read_text(errors="replace").splitlines(True)[-200:])
                     if path.exists() else "")
    return 0


def cmd_submit(settings, args) -> int:
    info = _runtime(settings)
    if not info:
        print("a2c: not running; `a2c start --detach` first", file=sys.stderr)
        return 1
    base = info["url"].rstrip("/")
    options = {"llm": args.llm, "foam": not args.no_foam}
    if args.foam_max_steps is not None:
        options["foam_max_steps"] = args.foam_max_steps
    headers = {"Origin": base}
    if args.idempotency_key:
        headers["Idempotency-Key"] = args.idempotency_key
    if args.url or args.text:
        body = {"options": options}
        if args.url:
            body["url"] = args.target
        else:
            body["text"] = Path(args.target).read_text()
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    else:
        path = Path(args.target)
        boundary = "a2c%d" % int(time.time() * 1000)
        data = (("--%s\r\nContent-Disposition: form-data; name=\"options\"\r\n\r\n%s\r\n"
                 "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                 "Content-Type: application/octet-stream\r\n\r\n") % (
                     boundary, json.dumps(options), boundary, path.name)).encode("utf-8") \
            + path.read_bytes() + ("\r\n--%s--\r\n" % boundary).encode("utf-8")
        headers["Content-Type"] = "multipart/form-data; boundary=%s" % boundary
    req = urllib.request.Request(base + "/api/queue/runs", data=data, headers=headers,
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        print("a2c: %s" % err.read().decode("utf-8", "replace"), file=sys.stderr)
        return 1
    run_id = out["run_id"]
    print(run_id)
    if not args.wait:
        return 0
    while True:
        run = _get("%s/api/queue/runs/%s" % (base, run_id))
        if run["status"] in ("done", "done_with_errors", "failed", "cancelled"):
            print("%s %s" % (run_id, run["status"]), file=sys.stderr)
            return 0 if run["status"] in ("done", "done_with_errors") else 1
        time.sleep(2)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="a2c", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    start = sub.add_parser("start", help="start everything on 127.0.0.1")
    start.add_argument("--detach", action="store_true", help="run in the background")
    start.add_argument("--port", type=int)
    start.add_argument("--data", help="data directory (A2C_DATA_DIR)")
    start.add_argument("--light-slots", type=int, help="light jobs at once (default 2)")
    start.add_argument("--foam-parallel", type=int,
                       help="OpenFOAM cases at once (default 1)")
    start.add_argument("--foam-cores", type=int,
                       help="cores per OpenFOAM case (default min(6, cores - light))")
    start.add_argument("--llm", choices=("stub", "real"), help="default LLM mode for runs")
    stop = sub.add_parser("stop", help="stop gracefully")
    stop.add_argument("--timeout", type=int, default=90)
    status = sub.add_parser("status", help="up or down, slots, queue")
    status.add_argument("--json", action="store_true")
    logs = sub.add_parser("logs", help="the service log, or a run's job logs")
    logs.add_argument("run", nargs="?")
    logs.add_argument("-f", "--follow", action="store_true")
    submit = sub.add_parser("submit", help="queue an assay")
    submit.add_argument("target", help="a .pdf/.md/.txt file (or a URL/DOI with --url)")
    submit.add_argument("--url", action="store_true", help="TARGET is a URL or DOI")
    submit.add_argument("--text", action="store_true", help="send the file's text inline")
    submit.add_argument("--llm", choices=("stub", "real"), default="stub")
    submit.add_argument("--no-foam", action="store_true", help="skip the OpenFOAM fan-out")
    submit.add_argument("--foam-max-steps", type=int)
    submit.add_argument("--idempotency-key")
    submit.add_argument("--wait", action="store_true", help="wait until the run finishes")
    for p in (stop, status, logs, submit):
        p.add_argument("--data", help="data directory (A2C_DATA_DIR)")
    args = ap.parse_args(argv)

    if getattr(args, "data", None):
        os.environ["A2C_DATA_DIR"] = args.data
    if args.cmd == "start":
        for flag, var in (("port", "A2C_PORT"), ("light_slots", "A2C_LIGHT_SLOTS"),
                          ("foam_parallel", "A2C_FOAM_MAX_PARALLEL"),
                          ("foam_cores", "A2C_FOAM_CORES_PER_CASE"), ("llm", "A2C_LLM")):
            value = getattr(args, flag)
            if value is not None:
                os.environ[var] = str(value)
    settings = config.load()
    return {"start": cmd_start, "stop": cmd_stop, "status": cmd_status, "logs": cmd_logs,
            "submit": cmd_submit}[args.cmd](settings, args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

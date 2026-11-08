"""End-to-end smoke test of the local deployment, no Docker, plain processes.

    .venv/bin/python deploy/local/smoke.py            # fast: fakes for the slow segments
    .venv/bin/python deploy/local/smoke.py --real     # the real pipeline + OpenFOAM (slow)

It starts the service exactly as `a2c start` does, in a throwaway data directory on a free
port, and checks:

  1. private by default: a foreign Host header and a cross-origin POST are refused
  2. a fixture assay goes in (multipart upload) and every job runs: ingest, extract and
     plan for real; design, the OpenFOAM fan-out (one job per chip x flowing step),
     fabricate, collect and report (faked unless --real)
  3. a foam job that fails once is retried and succeeds; resubmitting with the same
     Idempotency-Key returns the same run
  4. a running run can be cancelled, and its job is killed
  5. artefacts come back through signed URLs; a tampered signature is refused; OpenFOAM
     bulk (polyMesh, time directories) is never uploaded
  6. durability: stopping the service mid-job hands the job back; after a restart it
     resumes and finishes without spending an attempt
  7. `a2c stop` stops it cleanly
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PY = sys.executable
FIXTURE = ROOT / "assay2chip" / "fixtures" / "bead_capture_assay.md"
FAKE = ROOT / "deploy" / "local" / "fake_stage.py"


class Smoke:
    def __init__(self, data: Path, real: bool, verbose: bool):
        self.data = data
        self.real = real
        self.verbose = verbose
        self.port = self._free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self.proc = None
        self.env = dict(os.environ)
        self.env.update({"A2C_DATA_DIR": str(data), "A2C_PORT": str(self.port),
                         "A2C_POLL_S": "0.2", "A2C_FOAM_MAX_PARALLEL": "2",
                         "A2C_LIGHT_SLOTS": "2"})
        if not real:
            overrides = data / "overrides.json"
            data.mkdir(parents=True, exist_ok=True)
            fake = [PY, str(FAKE)]
            overrides.write_text(json.dumps({
                "design": fake + ["design", "{run_id}"],
                "foam": fake + ["foam", "{run_id}", "{chip}", "{step}", "{out}"],
                "fabricate": fake + ["fabricate", "{run_id}"],
                "report": fake + ["report", "{run_id}"],
            }))
            self.env["A2C_COMMAND_OVERRIDES"] = str(overrides)
            self.env["A2C_TEST_FAIL_ONCE"] = "foam"

    @staticmethod
    def _free_port() -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def say(self, text: str) -> None:
        print("  " + text, flush=True)

    # --- http ---------------------------------------------------------------------

    def http(self, method, path, body=None, headers=None, raw=False):
        headers = dict(headers or {})
        headers.setdefault("Origin", self.base)
        req = urllib.request.Request(self.base + path, data=body, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                return resp.status, (data if raw else json.loads(data or b"null"))
        except urllib.error.HTTPError as err:
            data = err.read()
            try:
                return err.code, json.loads(data)
            except ValueError:
                return err.code, data

    def submit_file(self, path: Path, options: dict, key: str = None):
        boundary = "smoke%d" % time.time_ns()
        body = (("--%s\r\nContent-Disposition: form-data; name=\"options\"\r\n\r\n%s\r\n"
                 "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"%s\"\r\n"
                 "\r\n") % (boundary, json.dumps(options), boundary, path.name)).encode() \
            + path.read_bytes() + ("\r\n--%s--\r\n" % boundary).encode()
        headers = {"Content-Type": "multipart/form-data; boundary=%s" % boundary}
        if key:
            headers["Idempotency-Key"] = key
        return self.http("POST", "/api/queue/runs", body, headers)

    def submit_text(self, text: str, options: dict):
        return self.http("POST", "/api/queue/runs",
                         json.dumps({"text": text, "options": options}).encode(),
                         {"Content-Type": "application/json"})

    def run(self, run_id):
        return self.http("GET", "/api/queue/runs/%s" % run_id)[1]

    def wait(self, run_id, until, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            run = self.run(run_id)
            if until(run):
                return run
            time.sleep(0.3)
        raise AssertionError("timed out waiting on %s: %s" % (
            run_id, json.dumps(self.run(run_id), indent=1)[:3000]))

    # --- the service --------------------------------------------------------------

    def a2c(self, *args, check=True):
        out = subprocess.run([PY, "-m", "assay2chip.hosting.local"] + list(args),
                             cwd=str(ROOT), env=self.env, capture_output=True, text=True,
                             timeout=180)
        if self.verbose:
            sys.stdout.write(out.stdout + out.stderr)
        if check and out.returncode != 0:
            raise AssertionError("a2c %s failed: %s %s" % (args, out.stdout, out.stderr))
        return out

    def start(self):
        self.proc = subprocess.Popen([PY, "-m", "assay2chip.hosting.local", "start"],
                                     cwd=str(ROOT), env=self.env,
                                     stdout=subprocess.DEVNULL if not self.verbose else None,
                                     stderr=subprocess.DEVNULL if not self.verbose else None)
        for _ in range(150):
            try:
                if self.http("GET", "/api/queue/status")[0] == 200:
                    return
            except OSError:
                pass
            if self.proc.poll() is not None:
                raise AssertionError("service exited with %s" % self.proc.returncode)
            time.sleep(0.2)
        raise AssertionError("service did not come up")

    def stop(self):
        self.a2c("stop")
        rc = self.proc.wait(timeout=60)
        assert rc == 0, "service exit code %s" % rc

    # --- the checks ---------------------------------------------------------------

    def check(self) -> dict:
        results = {}
        self.start()
        status = self.http("GET", "/api/queue/status")[1]
        self.say("up on %s: %s" % (self.base, json.dumps(status["plan"])))

        # 1. private by default
        code, _ = self.http("GET", "/api/queue/status", headers={"Host": "evil.example"})
        assert code == 403, "foreign Host accepted (%s)" % code
        code, _ = self.http("POST", "/api/queue/runs", b"{}",
                            {"Origin": "http://evil.example",
                             "Content-Type": "application/json"})
        assert code == 403, "cross-origin POST accepted (%s)" % code
        self.say("refuses a foreign Host and a cross-origin POST")

        # 2 + 3. a fixture assay end to end, idempotency
        options = {"llm": "stub", "foam": True}
        code, sub = self.submit_file(FIXTURE, options, key="smoke-a")
        assert code == 202, (code, sub)
        run_a = sub["run_id"]
        code, again = self.submit_file(FIXTURE, options, key="smoke-a")
        assert again["run_id"] == run_a and again["existing"], again
        self.say("submitted %s (same Idempotency-Key -> same run)" % run_a)

        # 4. cancel a second run mid-design (fast mode only: the fake design sleeps)
        if not self.real:
            code, sub_b = self.submit_text(FIXTURE.read_text() + "\nSLOW_FAKE_DESIGN\n",
                                           {"llm": "stub"})
            run_b = sub_b["run_id"]
            self.wait(run_b, lambda r: any(j["key"] == "design" and j["status"] == "running"
                                           for j in r.get("jobs", [])), 60)
            self.http("POST", "/api/queue/runs/%s/cancel" % run_b)
            run = self.wait(run_b, lambda r: r["status"] == "cancelled", 60)
            design = [j for j in run["jobs"] if j["key"] == "design"][0]
            assert design["status"] == "cancelled", design
            results["cancelled"] = run_b
            self.say("cancelled %s while its design job was running" % run_b)

        run = self.wait(run_a, lambda r: r["status"] in ("done", "done_with_errors",
                                                         "failed", "cancelled"),
                        3 * 3600 if self.real else 180)
        jobs = run["jobs"]
        foam = [j for j in jobs if j["kind"] == "foam"]
        self.say("%s finished: %s; %d jobs, %d OpenFOAM" % (run_a, run["status"], len(jobs),
                                                           len(foam)))
        for j in jobs:
            self.say("    %-28s %-17s attempts %d/%d %s" % (
                j["key"], j["status"], j["attempts"], j["max_attempts"], j["error"] or ""))
        results.update(run_a=run_a, status=run["status"], jobs=len(jobs), foam=len(foam))
        if not self.real:
            assert run["status"] == "done", run["status"]
            partition = self.artifact_json(run_a, "assay2chip/work/%s/partition.json" % run_a)
            assert len(foam) == 2 * len(partition["chips"]), (len(foam), partition)
            assert all(j["status"] == "done" and j["attempts"] == 2 for j in foam), \
                "foam jobs should each fail once and then succeed"
            self.say("fan-out: %d chips x 2 flowing steps; each foam job retried once"
                     % len(partition["chips"]))

        # 5. artefacts through signed URLs
        arts = self.http("GET", "/api/queue/runs/%s/artifacts" % run_a)[1]
        paths = {a["path"]: a for a in arts}
        expect = ["assay2chip/work/%s/raw.json" % run_a, "assay2chip/work/%s/assay.json" % run_a,
                  "assay2chip/work/%s/partition.json" % run_a,
                  "assay2chip/work/%s/foam/summary.json" % run_a,
                  "assay2chip/work/%s/report.md" % run_a]
        missing = [p for p in expect if p not in paths]
        assert not missing, "missing artefacts: %s" % missing
        assert not any("polyMesh" in p or "/0.5/" in p for p in paths), \
            "OpenFOAM bulk was uploaded"
        report = paths["assay2chip/work/%s/report.md" % run_a]
        code, body = self.http("GET", report["url"], raw=True, headers={"Origin": ""})
        assert code == 200 and len(body) == report["bytes"], (code, len(body))
        bad = report["url"][:-4] + ("0000" if not report["url"].endswith("0000") else "1111")
        assert self.http("GET", bad, raw=True)[0] == 403, "tampered signature accepted"
        local = Path(tempfile.mkdtemp(prefix="a2c_smoke_out_"))
        for a in arts:
            code, data = self.http("GET", a["url"], raw=True)
            assert code == 200, (a["path"], code)
            dest = local / a["path"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        results.update(artifacts=len(arts), downloaded_to=str(local))
        self.say("%d artefacts downloaded via signed URLs to %s; tampered link -> 403; "
                 "no OpenFOAM bulk uploaded" % (len(arts), local))

        # 6. durability across a stop and a restart
        if not self.real:
            code, sub_c = self.submit_text(FIXTURE.read_text() + "\nSLOWISH_FAKE_DESIGN\n",
                                           {"llm": "stub", "foam": False})
            run_c = sub_c["run_id"]
            self.wait(run_c, lambda r: any(j["key"] == "design" and j["status"] == "running"
                                           for j in r.get("jobs", [])), 60)
            self.stop()
            status = self.a2c("status", "--json", check=False)
            down = json.loads(status.stdout)
            assert status.returncode == 3 and run_c in down.get("active_runs", []), down
            self.say("stopped mid-design: %s is waiting in the durable queue" % run_c)
            self.start()
            run = self.wait(run_c, lambda r: r["status"] in ("done", "failed", "cancelled"),
                            120)
            design = [j for j in run["jobs"] if j["key"] == "design"][0]
            assert run["status"] == "done" and design["attempts"] == 1, (run["status"], design)
            results["resumed"] = run_c
            self.say("restarted: %s resumed and finished (design attempts 1: the clean stop "
                     "did not spend a retry)" % run_c)

        # 7. clean stop
        self.stop()
        log_text = (self.data / "logs" / "a2c.log").read_text()
        assert "stopped cleanly" in log_text
        self.say("stopped cleanly; one log at %s" % (self.data / "logs" / "a2c.log"))
        return results

    def artifact_json(self, run_id, path):
        arts = {a["path"]: a for a in
                self.http("GET", "/api/queue/runs/%s/artifacts" % run_id)[1]}
        return json.loads(self.http("GET", arts[path]["url"], raw=True)[1])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--real", action="store_true",
                    help="run the real pipeline and OpenFOAM (slow)")
    ap.add_argument("--data", help="data directory (default: a new temporary one)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    data = Path(args.data) if args.data else Path(tempfile.mkdtemp(prefix="a2c_smoke_"))
    smoke = Smoke(data, args.real, args.verbose)
    print("deploy smoke test (%s) in %s" % ("REAL pipeline" if args.real else "fast fakes",
                                            data))
    try:
        results = smoke.check()
    finally:
        if smoke.proc is not None and smoke.proc.poll() is None:
            smoke.proc.terminate()
            smoke.proc.wait(timeout=60)
    print(json.dumps(results, indent=2))
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

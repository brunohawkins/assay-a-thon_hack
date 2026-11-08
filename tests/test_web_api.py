"""web/server.py and web/api/: every route's shape, and the boundaries that matter.

    python -m unittest discover -s tests -v

Offline by design. Most tests run the real server against a TEMPORARY repository holding
a fake `assay2chip/run.py`, so nothing here touches the real work directory, no stage
code is needed, and the pipeline being half-written cannot make these tests flap. The
fake script prints the same lines run.py prints and writes the same run manifest, which
is the contract the API is built on.

What is checked, beyond response shapes:

  * path traversal is refused - `..`, percent-encoded `%2e%2e`, absolute paths, and a
    symlink out of the work directory;
  * `chip/` and `protocols/` give up their specs but not the build scripts beside them;
  * an upload over MAX_UPLOAD_BYTES is refused with 413 and nothing is written;
  * a missing assay2chip/run.py or assay2chip/cfd/run.py is 503, never 500;
  * a stage reported NOT YET IMPLEMENTED arrives as `not_implemented`;
  * the SSE stream really streams, and delivers `stage` then `done` for a job.
"""

import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web import server                                                      # noqa: E402
from web.api import paths                                                   # noqa: E402

# A stand-in for assay2chip/run.py. It prints what run.py prints (the header line, one
# line per stage, the summary) and writes the same run_manifest.json, including a stage
# whose module does not exist yet.
FAKE_RUN = '''\
import json, sys, time
from pathlib import Path

args = sys.argv[1:]


def value(flag, default=None):
    return args[args.index(flag) + 1] if flag in args else default


run_id = value("--id", "fake")
work = Path("assay2chip/work") / run_id
work.mkdir(parents=True, exist_ok=True)
(work / "raw.json").write_text(json.dumps({"id": run_id, "title": "A fake protocol"}))
(work / "report.md").write_text("# report\\n\\nsimulated numbers only\\n")
(work / "report.json").write_text(json.dumps({"id": run_id, "problems": []}))
(work / "cfd").mkdir(exist_ok=True)
(work / "cfd" / "findings.json").write_text(json.dumps([
    {"id": "f1", "class": "trap", "severity": "blocker", "chip_id": "demo_chip_v1",
     "feature_id": "cx_1", "metric": "u_mm_s", "value": 0.01, "limit": 0.1,
     "units": "mm/s", "message": "low-velocity zone"},
    {"id": "f2", "class": "shear", "severity": "low", "chip_id": "demo_chip_v1",
     "feature_id": "ch_1", "metric": "shear", "value": 0.2, "limit": 0.3,
     "units": "dyn/cm2", "message": "near the rinse ceiling"},
]))

print("assay2chip: id '%s', s0_ingest -> s2_normalise, llm stub" % run_id)
sys.stdout.flush()
print("  s0_ingest        ok                  0.01s  +1 file(s)  [llm: stub]")
sys.stdout.flush()
time.sleep(0.2)
print("  s1_extract       NOT YET IMPLEMENTED (skipped)")
sys.stdout.flush()
print("  s2_normalise     ok                  0.02s")
sys.stdout.flush()
manifest = {
    "id": run_id,
    "created_at": "2026-01-01T00:00:00+00:00",
    "command": "python assay2chip/run.py " + " ".join(args),
    "args": {"paper": value("--paper"), "from": "s0", "to": "s2"},
    "llm": {"mode": "stub", "model": None},
    "plan": ["s0_ingest", "s1_extract", "s2_normalise"],
    "status": "ok",
    "summary": {"ok": 2, "not_implemented": 1},
    "reruns": [],
    "stages": [
        {"stage": "s0", "name": "ingest", "status": "ok", "started_at":
         "2026-01-01T00:00:00+00:00", "duration_s": 0.01,
         "output": {"path": "assay2chip/work/%s/raw.json" % run_id, "sha256": "x"},
         "llm": {"mode": "none"}},
        {"stage": "s1", "name": "extract", "status": "not_implemented",
         "started_at": "2026-01-01T00:00:01+00:00", "duration_s": 0.0,
         "message": "NOT YET IMPLEMENTED: assay2chip/stages/s1_extract.py does not exist yet"},
        {"stage": "s2", "name": "normalise", "status": "ok", "started_at":
         "2026-01-01T00:00:02+00:00", "duration_s": 0.02,
         "output": {"path": "assay2chip/work/%s/report.md" % run_id, "sha256": "y"},
         "extra_writes": [{"path": "chip/demo_chip_v1.json", "exists": True}]},
    ],
}
(work / "run_manifest.json").write_text(json.dumps(manifest, indent=2))
print("")
print("manifest: assay2chip/work/%s/run_manifest.json" % run_id)
print("  2 ok, 1 not_implemented")
'''

DEMO_CHIP = {
    "id": "demo_chip_v1", "name": "A demo chip", "placeholder": False, "units": "mm",
    "footprint": {"x": 30, "y": 20},
    "chambers": [{"id": "cx_1"}], "channels": [{"id": "ch_1"}], "ports": [{"id": "p_1"}],
}
TARGET_CHIP = {
    "id": "target_chip_v1", "name": "A target-scale chip", "units": "mm",
    "footprint": {"x": 120.0, "y": 80.0}, "chambers": [], "channels": [], "ports": [],
}


#: A 48-byte stand-in for an mp4. Range requests do not care what the bytes mean.
FAKE_MP4 = b"\x00\x00\x00\x18ftypmp42" + bytes(range(40))

TWIN_INDEX = """<!doctype html><html><head><link rel="stylesheet" href="style.css">
</head><body><script src="main.js" type="module"></script></body></html>
"""
# The twin fetches by relative path from the repo root; tools/build_twin_site.discover()
# reads these literals out of the code, which is exactly the allowlist the route uses.
TWIN_MAIN = """\
const root = '../';
const CHIP = 'chip/demo_chip_v1.json';
async function load() {
  await fetch(`${root}runs/index.json`);
  await fetch(`../${CHIP}`);
  await fetch('../contracts/blocks.json');
  await fetch('../docs/modules.json');
}
load();
"""


def build_repo(directory: Path, *, run_script: bool = True, cfd_script: bool = False) -> Path:
    """A minimal repository the API can serve: contracts, two chips, a figure, a script."""
    (directory / "assay2chip" / "work").mkdir(parents=True)
    (directory / "assay2chip" / "stages").mkdir(parents=True)
    (directory / "contracts").mkdir()
    (directory / "chip").mkdir()
    (directory / "cad" / "figures").mkdir(parents=True)
    (directory / "cad" / "exports" / "demo_chip_v1").mkdir(parents=True)
    (directory / "protocols").mkdir()

    shutil.copy(ROOT / "contracts" / "blocks.json", directory / "contracts" / "blocks.json")
    (directory / "chip" / "demo_chip_v1.json").write_text(json.dumps(DEMO_CHIP))
    (directory / "cad" / "target_chip_spec.json").write_text(json.dumps(TARGET_CHIP))
    (directory / "cad" / "figures" / "demo_chip_v1_layout.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"/>')
    (directory / "cad" / "exports" / "demo_chip_v1" / "cx_1.stl").write_bytes(b"solid x\n")
    (directory / "protocols" / "pipeline_mvp.json").write_text(json.dumps({"steps": []}))
    (directory / "protocols" / "build_module1.py").write_text("# a build script, not a spec\n")
    (directory / "assay2chip" / "stages" / "s0_ingest.py").write_text("def run(ctx):\n    pass\n")

    # docs/, the media it points at, and a run's render and CFD-figure output
    (directory / "docs" / "videos").mkdir(parents=True)
    (directory / "docs" / "modules.json").write_text(json.dumps(
        {"modules": [{"id": "m1", "renders_3d":
                      ["assay2chip/work/doc_run/foam/render/m1_turn.mp4"]}]}))
    (directory / "docs" / "modules.md").write_text("# modules\n\nDEMO SCALE.\n")
    (directory / "docs" / "videos" / "manifest.json").write_text(json.dumps(
        {"videos": [{"chip": "demo_chip_v1", "file": "docs/videos/bench.mp4"}]}))
    (directory / "docs" / "videos" / "bench.mp4").write_bytes(FAKE_MP4)
    (directory / "docs" / "secret.xlsx").write_bytes(b"PK\x03\x04 not for the web\n")
    (directory / "docs" / "notes.docx").write_bytes(b"PK\x03\x04 not for the web\n")
    (directory / "contracts" / "sources").mkdir(parents=True, exist_ok=True)
    (directory / "contracts" / "sources" / "quotes.json").write_text('{"private": true}')
    (directory / "contracts" / "sources" / "prices.xlsx").write_bytes(b"PK\x03\x04\n")

    render = directory / "assay2chip" / "work" / "doc_run" / "foam" / "render"
    render.mkdir(parents=True)
    (render / "m1_turn.mp4").write_bytes(FAKE_MP4)
    (render / "m1_still.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (render / "m1.glb").write_bytes(b"glTF\x02\x00\x00\x00")
    figures = directory / "assay2chip" / "work" / "doc_run" / "cfd" / "figures"
    figures.mkdir(parents=True)
    (figures / "demo_chip_v1_s1_speed.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
    (directory / "assay2chip" / "work" / "doc_run" / "cfd" / "findings.json").write_text("[]")
    (directory / "assay2chip" / "work" / "doc_run" / "run_manifest.json").write_text("{}")

    # the digital twin
    (directory / "twin" / "vendor" / "three").mkdir(parents=True)
    (directory / "twin" / "index.html").write_text(TWIN_INDEX)
    (directory / "twin" / "main.js").write_text(TWIN_MAIN)
    (directory / "twin" / "style.css").write_text("body { margin: 0 }\n")
    (directory / "twin" / "vendor" / "three" / "three.module.js").write_text("export const x=1;\n")
    (directory / "twin" / "notes.xlsx").write_bytes(b"PK\x03\x04\n")
    (directory / "runs").mkdir()
    (directory / "runs" / "index.json").write_text(json.dumps(
        {"runs": [{"file": "r1.json", "chip_spec": "chip/demo_chip_v1.json"}]}))
    (directory / "runs" / "r1.json").write_text(json.dumps(
        {"chip_spec": "chip/demo_chip_v1.json"}))
    # build_twin_site.py is another agent's; copy it so discover() is the real one
    tools = directory / "tools"
    tools.mkdir(exist_ok=True)
    if (ROOT / "tools" / "build_twin_site.py").is_file():
        shutil.copy(ROOT / "tools" / "build_twin_site.py", tools / "build_twin_site.py")
    if run_script:
        (directory / "assay2chip" / "run.py").write_text(FAKE_RUN)
    if cfd_script:
        (directory / "assay2chip" / "cfd").mkdir(parents=True, exist_ok=True)
        (directory / "assay2chip" / "cfd" / "run.py").write_text(
            "import sys\nprint('cfd stub', sys.argv[1:])\n")
    return directory


class ServerCase(unittest.TestCase):
    """A real server on a real socket, serving a temporary repository."""

    run_script = True
    cfd_script = False

    @classmethod
    def setUpClass(cls):
        cls._saved_root = paths.ROOT
        cls.tmp = Path(tempfile.mkdtemp(prefix="a2c_web_test_"))
        cls.repo = build_repo(cls.tmp / "repo", run_script=cls.run_script,
                              cfd_script=cls.cfd_script)
        cls.server = server.make_server("127.0.0.1", 0, concurrency=1, root=cls.repo,
                                        quiet=True)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        paths.configure(cls._saved_root)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # --- helpers --------------------------------------------------------------

    def url(self, path: str) -> str:
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def fetch(self, path, data=None, headers=None, method=None, raw=False):
        """(status, body). Never raises for an HTTP error status - that is the test."""
        request = urllib.request.Request(self.url(path), data=data, method=method,
                                         headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body, status = response.read(), response.status
                content_type = response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as error:
            body, status = error.read(), error.code
            content_type = error.headers.get("Content-Type", "")
        if raw:
            return status, body, content_type
        if b"json" in content_type.encode() or body[:1] in (b"{", b"["):
            return status, json.loads(body.decode("utf-8"))
        return status, body.decode("utf-8", "replace")

    def post_json(self, path, payload):
        return self.fetch(path, data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")

    def wait_for_run(self, run_id, *, timeout=30.0):
        """Block until the run leaves the queue, then return its /api/runs/<id> body."""
        import time
        deadline = time.time() + timeout
        body = {}
        while time.time() < deadline:
            status, body = self.fetch("/api/runs/%s" % run_id)
            self.assertEqual(200, status, body)
            if body.get("status") in ("done", "failed"):
                return body
            time.sleep(0.1)
        self.fail("run %r did not finish in %gs: %s" % (run_id, timeout, body))

    def sse(self, path, *, want="done", timeout=30.0, max_events=400):
        """Read an SSE stream until `want` arrives. Returns [(event, data), ...]."""
        request = urllib.request.Request(self.url(path))
        events = []
        with urllib.request.urlopen(request, timeout=timeout) as response:
            self.assertTrue(response.headers.get("Content-Type", "").startswith(
                "text/event-stream"), response.headers.get("Content-Type"))
            name, data = None, None
            while len(events) < max_events:
                line = response.readline()
                if not line:
                    break
                text = line.decode("utf-8").rstrip("\n")
                if text.startswith(":"):
                    events.append(("comment", text))
                elif text.startswith("event: "):
                    name = text[7:]
                elif text.startswith("data: "):
                    data = json.loads(text[6:])
                elif text == "" and name is not None:
                    events.append((name, data))
                    if name == want:
                        break
                    name, data = None, None
        return events


# --- the shape of every route ------------------------------------------------

class HealthAndStages(ServerCase):

    def test_health(self):
        status, body = self.fetch("/api/health")
        self.assertEqual(200, status)
        self.assertTrue(body["ok"])
        self.assertIsInstance(body["version"], str)
        self.assertEqual(13, len(body["stages"]))
        self.assertEqual("s0_ingest", body["stages"][0])
        self.assertIn(body["llm"], ("stub", "real"))

    def test_health_llm_is_honest(self):
        """A stub must never be reported as a real extraction, whatever was asked for."""
        status, body = self.fetch("/api/health")
        if not os.environ.get("ANTHROPIC_API_KEY"):
            self.assertEqual("stub", body["llm"])
            self.assertFalse(body["llm_detail"]["api_key_present"])
            self.assertIn("stub", body["llm_detail"]["note"].lower())

    def test_health_reports_which_scripts_exist(self):
        status, body = self.fetch("/api/health")
        self.assertTrue(body["scripts"]["run"]["available"])
        self.assertFalse(body["scripts"]["cfd"]["available"])

    def test_stages(self):
        status, body = self.fetch("/api/stages")
        self.assertEqual(200, status)
        self.assertEqual(13, len(body))
        self.assertEqual([row["stage"] for row in body],
                         ["s%d" % n for n in range(13)])
        for row in body:
            self.assertTrue(row["description"].strip())
            self.assertIn("implemented", row)
        self.assertTrue(body[0]["implemented"])      # s0_ingest.py is in the fake repo
        self.assertFalse(body[4]["implemented"])

    def test_unknown_route_is_404_json(self):
        status, body = self.fetch("/api/nope")
        self.assertEqual(404, status)
        self.assertIn("error", body)
        self.assertIn("detail", body)

    def test_wrong_method_is_405(self):
        status, body = self.fetch("/api/health", data=b"{}", method="POST",
                                  headers={"Content-Type": "application/json"})
        self.assertEqual(405, status)
        self.assertIn("GET", body["detail"])

    def test_index_without_a_frontend_still_answers(self):
        status, body, content_type = self.fetch("/", raw=True)
        self.assertEqual(200, status)
        self.assertIn("text/html", content_type)
        self.assertIn(b"/api/health", body)


class Chips(ServerCase):

    def test_list_chips(self):
        status, body = self.fetch("/api/chips")
        self.assertEqual(200, status)
        found = {row["id"]: row for row in body}
        self.assertEqual({"demo_chip_v1", "target_chip_v1"}, set(found))
        demo = found["demo_chip_v1"]
        self.assertEqual("chip/demo_chip_v1.json", demo["path"])
        self.assertEqual({"x_mm": 30.0, "y_mm": 20.0}, demo["footprint"])
        self.assertEqual("demo", demo["scale"])
        self.assertTrue(demo["demo"])
        self.assertEqual("DEMO SCALE", demo["scale_label"])
        self.assertIn("cad/figures/demo_chip_v1_layout.svg", demo["figures"])

    def test_target_scale_is_not_labelled_demo(self):
        status, body = self.fetch("/api/chips")
        target = [row for row in body if row["id"] == "target_chip_v1"][0]
        self.assertEqual("target", target["scale"])
        self.assertFalse(target["demo"])
        self.assertNotIn("DEMO", target["scale_label"])

    def test_get_chip(self):
        status, body = self.fetch("/api/chips/demo_chip_v1")
        self.assertEqual(200, status)
        self.assertEqual("demo_chip_v1", body["id"])
        self.assertEqual("demo", body["_meta"]["scale"])

    def test_unknown_chip(self):
        status, body = self.fetch("/api/chips/not_a_chip")
        self.assertEqual(404, status)
        self.assertEqual("unknown_chip", body["error"])

    def test_scale_comes_from_the_contract(self):
        """blocks.json is the authority: with it gone, no scale is invented."""
        from web.api import chips
        blocks = self.repo / "contracts" / "blocks.json"
        kept = blocks.read_bytes()
        blocks.unlink()
        try:
            chips._cache["stamp"] = None
            self.assertEqual("unknown", chips.classify({"x_mm": 30.0, "y_mm": 20.0})["scale"])
        finally:
            blocks.write_bytes(kept)
            chips._cache["stamp"] = None


class Runs(ServerCase):

    def test_run_from_pasted_text_then_every_route(self):
        status, body = self.post_json("/api/runs", {
            "text": "1. Add 100 uL of medium.\n2. Incubate 10 min at 37 C.\n",
            "id": "paste_one",
            "options": {"llm": "stub", "scale": "demo", "to": "s2"},
        })
        self.assertEqual(202, status, body)
        self.assertEqual("paste_one", body["run_id"])
        self.assertEqual("stub", body["llm"])
        upload = self.repo / "assay2chip" / "work" / "paste_one" / "upload" / "paste_one.md"
        self.assertTrue(upload.is_file(), "the pasted text goes to work/<id>/upload/")

        run = self.wait_for_run("paste_one")
        self.assertEqual("done", run["status"])
        self.assertEqual(13, len(run["stages"]))
        rows = {row["stage"]: row for row in run["stages"]}
        for row in run["stages"]:
            self.assertIn(row["status"], ("pending", "running", "done", "failed",
                                          "skipped", "not_implemented"))
            self.assertIn("started", row)
            self.assertIn("seconds", row)
            self.assertIsInstance(row["outputs"], list)
            self.assertIn("error", row)
        self.assertEqual("done", rows["s0"]["status"])
        self.assertEqual("assay2chip/work/paste_one/raw.json", rows["s0"]["outputs"][0])
        self.assertEqual("skipped", rows["s7"]["status"])
        self.assertEqual("stub", run["llm"])

        # a stage with no module yet is not_implemented, not a failure and not a success
        self.assertEqual("not_implemented", rows["s1"]["status"])
        self.assertIn("NOT YET IMPLEMENTED", rows["s1"]["error"])

        status, listing = self.fetch("/api/runs")
        self.assertEqual(200, status)
        row = [item for item in listing if item["run_id"] == "paste_one"][0]
        for key in ("run_id", "title", "created", "status", "chips",
                    "findings_by_severity"):
            self.assertIn(key, row)
        self.assertEqual("A fake protocol", row["title"])
        self.assertEqual({"blocker": 1, "low": 1}, row["findings_by_severity"])
        self.assertIn("demo_chip_v1", row["chips"])

        status, items = self.fetch("/api/runs/paste_one/artifacts")
        self.assertEqual(200, status)
        by_path = {item["path"]: item for item in items}
        self.assertIn("assay2chip/work/paste_one/raw.json", by_path)
        self.assertEqual("json", by_path["assay2chip/work/paste_one/raw.json"]["kind"])
        self.assertEqual("s0", by_path["assay2chip/work/paste_one/raw.json"]["stage"])
        self.assertGreater(by_path["assay2chip/work/paste_one/raw.json"]["bytes"], 0)
        self.assertEqual("md", by_path["assay2chip/work/paste_one/report.md"]["kind"])

        status, report = self.fetch("/api/runs/paste_one/report")
        self.assertEqual(200, status)
        self.assertIn("# report", report["markdown"])
        self.assertEqual("paste_one", report["json"]["id"])

        status, body, content_type = self.fetch(
            "/api/runs/paste_one/file?path=assay2chip/work/paste_one/raw.json", raw=True)
        self.assertEqual(200, status)
        self.assertIn("application/json", content_type)
        self.assertEqual("A fake protocol", json.loads(body)["title"])

        status, body = self.post_json("/api/runs/paste_one/rerun", {"from": "s2"})
        self.assertEqual(202, status, body)
        self.assertEqual("paste_one", body["run_id"])
        self.wait_for_run("paste_one")

    def test_doi_submission_needs_no_file(self):
        status, body = self.post_json("/api/runs", {"doi": "10.1000/xyz123",
                                                    "id": "doi_run"})
        self.assertEqual(202, status, body)
        self.assertEqual("doi", body["source_kind"])
        self.assertEqual("10.1000/xyz123", body["source"])
        self.wait_for_run("doi_run")

    def test_upload_lands_only_in_the_run_upload_directory(self):
        body, headers = multipart({"id": "upload_run"},
                                  ("file", "protocol.md", b"# Methods\n\nAdd buffer.\n"))
        status, response = self.fetch("/api/runs", data=body, headers=headers,
                                      method="POST")
        self.assertEqual(202, status, response)
        self.assertEqual("assay2chip/work/upload_run/upload/protocol.md",
                         response["source"])
        self.assertTrue((self.repo / response["source"]).is_file())
        self.wait_for_run("upload_run")

    def test_upload_over_the_cap_is_refused_and_nothing_is_written(self):
        oversized = b"x" * (paths.MAX_UPLOAD_BYTES + 1024)
        body, headers = multipart({"id": "too_big"}, ("file", "huge.pdf", oversized))
        status, response = self.fetch("/api/runs", data=body, headers=headers,
                                      method="POST")
        self.assertEqual(413, status, response)
        self.assertEqual("too_large", response["error"])
        self.assertEqual(paths.MAX_UPLOAD_BYTES, response["limit_bytes"])
        self.assertFalse((self.repo / "assay2chip" / "work" / "too_big").exists())

    def test_a_lying_content_length_is_still_capped(self):
        """The header is not trusted: the read itself stops at the cap."""
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            connection.sendall(
                b"POST /api/runs HTTP/1.1\r\nHost: localhost\r\n"
                b"Content-Type: application/json\r\nContent-Length: 999999999\r\n\r\n")
            connection.sendall(b'{"text": "' + b"y" * 4096)
            connection.settimeout(10)
            head = connection.recv(200).decode("latin-1")
        finally:
            connection.close()
        self.assertIn("413", head.split("\r\n")[0])

    def test_bad_requests(self):
        status, body = self.post_json("/api/runs", {})
        self.assertEqual(400, status)
        self.assertEqual("no_source", body["error"])

        status, body = self.post_json("/api/runs", {"text": "x", "id": "../escape"})
        self.assertEqual(400, status)
        self.assertEqual("bad_run_id", body["error"])

        status, body = self.post_json("/api/runs", {"text": "x", "id": "ok_id",
                                                    "options": {"from": "s99"}})
        self.assertEqual(400, status)
        self.assertEqual("unknown_stage", body["error"])

        status, body = self.post_json("/api/runs", {"text": "x", "id": "ok_id",
                                                    "options": {"scale": "enormous"}})
        self.assertEqual(400, status)
        self.assertEqual("bad_scale", body["error"])

        status, body = self.fetch("/api/runs/ghost_run")
        self.assertEqual(404, status)
        self.assertEqual("unknown_run", body["error"])

        status, body = self.post_json("/api/runs/ghost_run/rerun", {"from": "s2"})
        self.assertEqual(404, status)

    def test_real_llm_without_a_key_is_refused_not_faked(self):
        if os.environ.get("ANTHROPIC_API_KEY"):
            self.skipTest("a real key is present in this environment")
        status, body = self.post_json("/api/runs", {"text": "x", "id": "real_run",
                                                     "options": {"llm": "real"}})
        self.assertEqual(503, status)
        self.assertEqual("llm_unavailable", body["error"])
        self.assertEqual("stub", body["llm"]["mode"])


class Sandbox(ServerCase):
    """/file is a confidentiality boundary, not a convenience."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        work = cls.repo / "assay2chip" / "work" / "sandbox_run"
        (work / "upload").mkdir(parents=True)
        (work / "raw.json").write_text(json.dumps({"id": "sandbox_run"}))
        (work / "nested").mkdir()
        (work / "nested" / "note.md").write_text("inside\n")
        (cls.repo / "secret_outside.md").write_text("private lab work\n")
        os.symlink(cls.repo / "secret_outside.md", work / "escape.md")
        os.symlink("/etc/hosts", work / "absolute_escape.json")
        os.symlink(work / "raw.json", work / "inside_link.json")

    def file(self, path, run="sandbox_run"):
        quoted = path.replace(" ", "%20")
        return self.fetch("/api/runs/%s/file?path=%s" % (run, quoted))

    def test_serves_this_runs_own_files(self):
        status, body = self.file("assay2chip/work/sandbox_run/raw.json")
        self.assertEqual(200, status)
        self.assertEqual("sandbox_run", body["id"])
        status, body = self.file("assay2chip/work/sandbox_run/nested/note.md")
        self.assertEqual(200, status)

    def test_dotdot_is_refused(self):
        for attempt in ("../../secret_outside.md",
                        "assay2chip/work/sandbox_run/../../../secret_outside.md",
                        "assay2chip/./work/../../secret_outside.md"):
            status, body = self.file(attempt)
            self.assertEqual(403, status, attempt)
            self.assertEqual("forbidden_path", body["error"])
            self.assertIn("..", body["detail"])

    def test_encoded_dotdot_is_refused(self):
        for attempt in ("%2e%2e/secret_outside.md",
                        "assay2chip%2Fwork%2Fsandbox_run%2F%2e%2e%2F%2e%2e%2F%2e%2e%2Fetc%2Fpasswd",
                        "..%2f..%2fsecret_outside.md"):
            status, body = self.file(attempt)
            self.assertEqual(403, status, attempt)

    def test_absolute_paths_are_refused(self):
        for attempt in ("/etc/passwd", "//etc/passwd", str(self.repo / "secret_outside.md")):
            status, body = self.file(attempt)
            self.assertEqual(403, status, attempt)
            self.assertEqual("forbidden_path", body["error"])

    def test_symlink_out_of_the_sandbox_is_refused(self):
        for attempt in ("assay2chip/work/sandbox_run/escape.md",
                        "assay2chip/work/sandbox_run/absolute_escape.json"):
            status, body = self.file(attempt)
            self.assertEqual(403, status, attempt)
            self.assertEqual("forbidden_path", body["error"])

    def test_symlink_inside_the_sandbox_is_fine(self):
        status, body = self.file("assay2chip/work/sandbox_run/inside_link.json")
        self.assertEqual(200, status)
        self.assertEqual("sandbox_run", body["id"])

    def test_another_runs_files_are_refused(self):
        other = self.repo / "assay2chip" / "work" / "other_run"
        other.mkdir(parents=True, exist_ok=True)
        (other / "raw.json").write_text(json.dumps({"id": "other_run", "secret": True}))
        status, body = self.file("assay2chip/work/other_run/raw.json")
        self.assertEqual(403, status)

    def test_repo_specs_and_figures_are_served(self):
        for path, expected in (("chip/demo_chip_v1.json", "application/json"),
                               ("protocols/pipeline_mvp.json", "application/json"),
                               ("cad/figures/demo_chip_v1_layout.svg", "image/svg+xml")):
            status, body, content_type = self.fetch(
                "/api/runs/sandbox_run/file?path=%s" % path, raw=True)
            self.assertEqual(200, status, path)
            self.assertIn(expected, content_type, path)

    def test_build_scripts_beside_the_specs_are_not_served(self):
        status, body = self.file("protocols/build_module1.py")
        self.assertEqual(403, status)
        self.assertEqual("forbidden_path", body["error"])

    def test_exports_of_a_chip_this_run_did_not_make_are_refused(self):
        status, body = self.file("cad/exports/demo_chip_v1/cx_1.stl")
        self.assertEqual(403, status)

    def test_the_repo_scope_serves_figures_and_specs_with_no_run(self):
        status, body, content_type = self.fetch(
            "/api/runs/repo/file?path=cad/figures/demo_chip_v1_layout.svg", raw=True)
        self.assertEqual(200, status)
        self.assertEqual("image/svg+xml", content_type)
        status, body, content_type = self.fetch(
            "/api/runs/repo/file?path=cad/exports/demo_chip_v1/cx_1.stl", raw=True)
        self.assertEqual(200, status)
        self.assertEqual("model/stl", content_type)
        status, body = self.fetch("/api/runs/repo/file?path=../secret_outside.md")
        self.assertEqual(403, status)

    def test_missing_path_argument(self):
        status, body = self.fetch("/api/runs/sandbox_run/file")
        self.assertEqual(403, status)
        self.assertIn("required", body["detail"])

    def test_allowed_but_absent_is_404(self):
        status, body = self.file("assay2chip/work/sandbox_run/not_written_yet.json")
        self.assertEqual(404, status)

    def test_static_files_cannot_escape_either(self):
        status, body = self.fetch("/../../etc/passwd")
        self.assertIn(status, (400, 403, 404))


class ServerSentEvents(ServerCase):

    def test_stream_delivers_stage_then_done(self):
        status, body = self.post_json("/api/runs", {"text": "steps", "id": "sse_run",
                                                     "options": {"to": "s2"}})
        self.assertEqual(202, status, body)
        events = self.sse("/api/runs/sse_run/events")
        names = [name for name, _ in events]
        self.assertIn("stage", names)
        self.assertIn("log", names)
        self.assertIn("done", names)
        self.assertEqual("done", names[-1], "the stream ends with done")
        self.assertTrue(any(name == "comment" for name, _ in events),
                        "the stream opens with a comment, so a proxy sees bytes at once")

        stage_events = [data for name, data in events if name == "stage"]
        by_stage = {}
        for row in stage_events:
            by_stage.setdefault(row["stage"], []).append(row["status"])
        self.assertIn("running", by_stage["s0"])
        self.assertIn("done", by_stage["s0"])
        self.assertIn("not_implemented", by_stage["s1"])
        for row in stage_events:
            self.assertIn("status", row)
            self.assertIn("outputs", row)

        findings = [data for name, data in events if name == "finding"]
        self.assertEqual({"f1", "f2"}, {item["id"] for item in findings})
        self.assertEqual({"blocker", "low"}, {item["severity"] for item in findings})

        done = [data for name, data in events if name == "done"][0]
        self.assertEqual("sse_run", done["run_id"])
        self.assertEqual("done", done["status"])
        self.assertEqual(0, done["returncode"])
        self.assertEqual({"blocker": 1, "low": 1}, done["findings_by_severity"])
        self.assertEqual("stub", done["llm"])

    def test_stream_of_a_finished_run_replays_from_the_manifest(self):
        status, body = self.post_json("/api/runs", {"text": "steps", "id": "replay_run",
                                                     "options": {"to": "s2"}})
        self.assertEqual(202, status, body)
        self.wait_for_run("replay_run")
        # the live job is still registered, so ask the manager to forget it first
        self.server.app.manager._jobs.pop("replay_run", None)
        events = self.sse("/api/runs/replay_run/events")
        names = [name for name, _ in events]
        self.assertIn("stage", names)
        self.assertEqual("done", names[-1])
        done = [data for name, data in events if name == "done"][0]
        self.assertFalse(done["live"])
        self.assertEqual(13, len(done["stages"]))

    def test_stream_of_an_unknown_run_is_404(self):
        status, body = self.fetch("/api/runs/no_such_run/events")
        self.assertEqual(404, status)

    def test_second_run_of_the_same_id_is_refused_while_in_flight(self):
        status, first = self.post_json("/api/runs", {"text": "a", "id": "busy_run"})
        self.assertEqual(202, status, first)
        status, second = self.post_json("/api/runs", {"text": "a", "id": "busy_run"})
        self.assertIn(status, (202, 409))
        if status == 409:
            self.assertEqual("already_running", second["error"])
        self.wait_for_run("busy_run")

    def test_queued_jobs_say_so(self):
        """One at a time: the second submission is queued, and its status says it."""
        self.post_json("/api/runs", {"text": "a", "id": "queue_one"})
        status, second = self.post_json("/api/runs", {"text": "b", "id": "queue_two"})
        self.assertEqual(202, status, second)
        if second["status"] == "queued" and second["queued_behind"]:
            self.assertIn("one at a time", second["message"])
        self.wait_for_run("queue_one")
        self.wait_for_run("queue_two")


class MissingScripts(ServerCase):
    """The stage scripts are built in parallel: absent is 503, never 500, never a fake pass."""

    run_script = False
    cfd_script = False

    def test_run_without_the_script_is_503(self):
        status, body = self.post_json("/api/runs", {"text": "x", "id": "no_script"})
        self.assertEqual(503, status, body)
        self.assertEqual("script_not_built", body["error"])
        self.assertEqual("assay2chip/run.py", body["script"])
        self.assertIn("not in this checkout yet", body["detail"])
        self.assertFalse((self.repo / "assay2chip" / "work" / "no_script").exists(),
                         "a refused run writes nothing")

    def test_rerun_without_the_script_is_503(self):
        (self.repo / "assay2chip" / "work" / "old_run").mkdir(parents=True, exist_ok=True)
        status, body = self.post_json("/api/runs/old_run/rerun", {"from": "s2"})
        self.assertEqual(503, status, body)
        self.assertEqual("script_not_built", body["error"])

    def test_cfd_without_the_script_is_503(self):
        status, body = self.post_json("/api/cfd", {"pipeline": "protocols/pipeline_mvp.json"})
        self.assertEqual(503, status, body)
        self.assertEqual("script_not_built", body["error"])
        self.assertEqual("assay2chip/cfd/run.py", body["script"])

    def test_health_says_which_is_missing(self):
        status, body = self.fetch("/api/health")
        self.assertEqual(200, status)
        self.assertFalse(body["scripts"]["run"]["available"])
        self.assertFalse(body["scripts"]["cfd"]["available"])


class Cfd(ServerCase):

    cfd_script = True

    def test_cfd_by_pipeline(self):
        status, body = self.post_json("/api/cfd", {"pipeline": "protocols/pipeline_mvp.json"})
        self.assertEqual(202, status, body)
        self.assertIn("job_id", body)
        self.assertEqual("/api/runs/%s/events" % body["job_id"], body["events"])
        events = self.sse(body["events"])
        self.assertEqual("done", [name for name, _ in events][-1])

    def test_cfd_by_chip(self):
        status, body = self.post_json("/api/cfd", {"chip": "demo_chip_v1"})
        self.assertEqual(202, status, body)
        status, run = self.fetch("/api/runs/%s" % body["job_id"])
        self.assertEqual(200, status)
        self.assertEqual("cfd", run["kind"])

    def test_cfd_needs_exactly_one_subject(self):
        for payload in ({}, {"pipeline": "protocols/pipeline_mvp.json",
                             "chip": "demo_chip_v1"}):
            status, body = self.post_json("/api/cfd", payload)
            self.assertEqual(400, status, payload)

    def test_cfd_pipeline_cannot_escape_protocols(self):
        for attempt in ("../../etc/passwd", "chip/demo_chip_v1.json",
                        "protocols/../contracts/blocks.json"):
            status, body = self.post_json("/api/cfd", {"pipeline": attempt})
            self.assertEqual(400, status, attempt)

    def test_cfd_unknown_chip(self):
        status, body = self.post_json("/api/cfd", {"chip": "nope_v9"})
        self.assertEqual(404, status)


class Docs(ServerCase):
    """GET /docs/<path>: documentation, 3D renders and CFD figures, same sandbox."""

    def get(self, path, headers=None, raw=True):
        return self.fetch(path, headers=headers, raw=raw)

    def test_serves_the_two_json_documents(self):
        status, body, content_type = self.get("/docs/modules.json")
        self.assertEqual(200, status)
        self.assertIn("application/json", content_type)
        self.assertIn("modules", json.loads(body))
        status, body, content_type = self.get("/docs/videos/manifest.json")
        self.assertEqual(200, status)
        self.assertIn("videos", json.loads(body))

    def test_serves_markdown_under_docs(self):
        status, body, content_type = self.get("/docs/modules.md")
        self.assertEqual(200, status)
        self.assertTrue(content_type.startswith("text/markdown"), content_type)
        self.assertIn(b"DEMO SCALE", body)

    def test_the_api_alias_answers_too(self):
        status, body, content_type = self.get("/api/docs/modules.json")
        self.assertEqual(200, status)
        self.assertIn("modules", json.loads(body))

    def test_serves_render_output_with_the_right_types(self):
        for path, expected in (
                ("/assay2chip/work/doc_run/foam/render/m1_turn.mp4", "video/mp4"),
                ("/assay2chip/work/doc_run/foam/render/m1_still.png", "image/png"),
                ("/assay2chip/work/doc_run/foam/render/m1.glb", "model/gltf-binary")):
            status, body, content_type = self.get(path)
            self.assertEqual(200, status, path)
            self.assertEqual(expected, content_type, path)

    def test_serves_cfd_figures(self):
        status, body, content_type = self.get(
            "/assay2chip/work/doc_run/cfd/figures/demo_chip_v1_s1_speed.svg")
        self.assertEqual(200, status)
        self.assertEqual("image/svg+xml", content_type)

    def test_video_supports_range_so_a_player_can_seek(self):
        path = "/docs/videos/bench.mp4"
        status, body, content_type = self.get(path)
        self.assertEqual(200, status)
        self.assertEqual("video/mp4", content_type)
        self.assertEqual(FAKE_MP4, body)

        request = urllib.request.Request(self.url(path), headers={"Range": "bytes=8-23"})
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(206, response.status)
            self.assertEqual("bytes", response.headers.get("Accept-Ranges"))
            self.assertEqual("bytes 8-23/%d" % len(FAKE_MP4),
                             response.headers.get("Content-Range"))
            self.assertEqual(FAKE_MP4[8:24], response.read())

        request = urllib.request.Request(self.url(path), headers={"Range": "bytes=-8"})
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(206, response.status)
            self.assertEqual(FAKE_MP4[-8:], response.read())

        request = urllib.request.Request(self.url(path),
                                         headers={"Range": "bytes=999999-"})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                self.fail("an unsatisfiable range should not be 200: %s" % response.status)
        except urllib.error.HTTPError as error:
            self.assertEqual(416, error.code)
            self.assertEqual("bytes */%d" % len(FAKE_MP4),
                             error.headers.get("Content-Range"))

    def test_traversal_out_of_docs_is_refused(self):
        for attempt in ("/docs/%2e%2e/secret_outside.md",
                        "/docs/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                        "/docs/videos/%2e%2e/%2e%2e/contracts/sources/quotes.json",
                        "/docs/..%2fcontracts%2fsources%2fquotes.json"):
            status, body = self.fetch(attempt)
            self.assertIn(status, (403, 404), attempt)
            self.assertNotEqual(200, status, attempt)

    def test_absolute_paths_and_nul_are_refused(self):
        for attempt in ("/docs//etc/passwd", "/docs/%00etc/passwd"):
            status, body = self.fetch(attempt)
            self.assertIn(status, (400, 403, 404), attempt)

    def test_spreadsheets_and_word_documents_are_never_served(self):
        for attempt in ("/docs/secret.xlsx", "/docs/notes.docx", "/twin/notes.xlsx"):
            status, body = self.fetch(attempt)
            self.assertEqual(403, status, attempt)
            self.assertEqual("forbidden_path", body["error"])
            self.assertIn("never served", body["detail"])

    def test_contracts_sources_is_never_served(self):
        for attempt in ("/contracts/sources/quotes.json",
                        "/contracts/sources/prices.xlsx",
                        "/docs/%2e%2e/contracts/sources/quotes.json"):
            status, body = self.fetch(attempt)
            self.assertIn(status, (403, 404), attempt)
            self.assertNotEqual(200, status, attempt)

    def test_the_rest_of_a_run_is_not_reachable_through_the_docs_route(self):
        """Only foam/render and cfd/figures: the run's stage files stay behind /file."""
        for attempt in ("/assay2chip/work/doc_run/run_manifest.json",
                        "/assay2chip/work/doc_run/cfd/findings.json"):
            status, body = self.fetch(attempt)
            self.assertEqual(404, status, attempt)

    def test_symlink_out_of_docs_is_refused(self):
        link = self.repo / "docs" / "escape.md"
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink(self.repo / "secret_outside.md", link)
        (self.repo / "secret_outside.md").write_text("private\n")
        try:
            status, body = self.fetch("/docs/escape.md")
            self.assertEqual(403, status)
            self.assertEqual("forbidden_path", body["error"])
        finally:
            link.unlink()

    def test_a_missing_but_allowed_file_is_404(self):
        status, body = self.fetch("/docs/not_written_yet.md")
        self.assertEqual(404, status)
        self.assertEqual("not_found", body["error"])

    def test_health_reports_the_docs_route(self):
        status, body = self.fetch("/api/health")
        self.assertEqual("/docs/<path>", body["docs"]["route"])
        self.assertTrue(body["docs"]["available"])


class Twin(ServerCase):
    """GET /twin/: the digital twin, plus exactly the repo paths its code fetches."""

    def test_the_index_is_served_at_the_directory_url(self):
        status, body, content_type = self.fetch("/twin/", raw=True)
        self.assertEqual(200, status)
        self.assertIn("text/html", content_type)
        self.assertIn(b"main.js", body)

    def test_the_bare_path_redirects_so_relative_urls_work(self):
        request = urllib.request.Request(self.url("/twin"))

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(request, timeout=10) as response:
                self.assertEqual(301, response.status)
        except urllib.error.HTTPError as error:
            self.assertEqual(301, error.code)
            self.assertEqual("/twin/", error.headers.get("Location"))

    def test_the_twins_own_code_is_served(self):
        for path, expected in (("/twin/main.js", "text/javascript"),
                               ("/twin/style.css", "text/css"),
                               ("/twin/vendor/three/three.module.js", "text/javascript")):
            status, body, content_type = self.fetch(path, raw=True)
            self.assertEqual(200, status, path)
            self.assertTrue(content_type.startswith(expected), (path, content_type))

    def test_the_relative_fetches_in_its_code_resolve(self):
        """The twin asks for ../runs/index.json and friends from /twin/."""
        for path in ("/runs/index.json", "/runs/r1.json", "/chip/demo_chip_v1.json",
                     "/contracts/blocks.json", "/docs/modules.json"):
            status, body, content_type = self.fetch(path, raw=True)
            self.assertEqual(200, status, path)
            self.assertIn("application/json", content_type, path)

    def test_the_allowlist_comes_from_the_twins_code(self):
        from web.api import sandbox
        self.assertEqual("tools/build_twin_site.discover()", sandbox.twin_source())
        allowed = sandbox.twin_files()
        self.assertIn("twin/main.js", allowed)
        self.assertIn("runs/index.json", allowed)
        self.assertIn("chip/demo_chip_v1.json", allowed)
        self.assertNotIn("contracts/sources/quotes.json", allowed)
        self.assertNotIn("twin/notes.xlsx", allowed)

    def test_a_file_the_twin_never_fetches_is_not_served(self):
        (self.repo / "chip" / "unused_chip.json").write_text(json.dumps(
            {"id": "unused_chip", "footprint": {"x": 30, "y": 20}}))
        import time
        time.sleep(2.1)                         # let the derived allowlist refresh
        status, body = self.fetch("/chip/unused_chip.json")
        self.assertEqual(404, status, body)

    def test_traversal_against_the_twin_route_is_refused(self):
        (self.repo / "secret_outside.md").write_text("private\n")
        for attempt in ("/twin/%2e%2e/secret_outside.md",
                        "/twin/%2e%2e%2f%2e%2e%2fetc%2fpasswd",
                        "/twin/..%2f..%2fsecret_outside.md",
                        "/twin//etc/passwd",
                        "/twin/%00main.js"):
            status, body = self.fetch(attempt)
            self.assertIn(status, (400, 403, 404), attempt)
            self.assertNotEqual(200, status, attempt)

    def test_symlink_out_of_the_twin_is_refused(self):
        (self.repo / "secret_outside.md").write_text("private\n")
        link = self.repo / "twin" / "sneak.js"
        if link.exists() or link.is_symlink():
            link.unlink()
        os.symlink(self.repo / "secret_outside.md", link)
        import time
        time.sleep(2.1)
        try:
            status, body = self.fetch("/twin/sneak.js")
            self.assertIn(status, (403, 404))
            self.assertNotEqual(200, status)
        finally:
            link.unlink()

    def test_the_fallback_allowlist_still_refuses_the_same_things(self):
        """With the builder gone, the route narrows; it never widens."""
        from web.api import sandbox
        script = self.repo / "tools" / "build_twin_site.py"
        kept = script.read_bytes()
        script.unlink()
        sandbox._twin_cache["files"] = None
        try:
            allowed = sandbox.twin_files()
            self.assertTrue(sandbox.twin_source().startswith("fallback"),
                            sandbox.twin_source())
            self.assertIn("twin/main.js", allowed)
            self.assertNotIn("contracts/sources/quotes.json", allowed)
            self.assertNotIn("twin/notes.xlsx", allowed)
            for rel in allowed:
                self.assertIsNone(sandbox.forbidden(rel), rel)
        finally:
            script.write_bytes(kept)
            sandbox._twin_cache["files"] = None

    def test_health_reports_the_twin(self):
        status, body = self.fetch("/api/health")
        self.assertTrue(body["twin"]["available"])
        self.assertEqual("/twin/", body["twin"]["route"])
        self.assertGreater(body["twin"]["files"], 3)


# --- unit-level checks that need no server -----------------------------------

class Ids(unittest.TestCase):

    def test_sanitise_id(self):
        self.assertEqual("my_assay", paths.sanitise_id("my_assay"))
        self.assertEqual("my_assay", paths.sanitise_id("My Assay!"))
        self.assertEqual("etc_passwd", paths.sanitise_id("../../etc/passwd"))
        self.assertEqual("r2_run", paths.sanitise_id("2 run"))
        self.assertEqual("fallback", paths.sanitise_id("...", fallback="fallback"))
        self.assertEqual("fallback", paths.sanitise_id("", fallback="fallback"))
        self.assertTrue(paths.ID_RE.match(paths.sanitise_id("a" * 200)))

    def test_safe_filename(self):
        self.assertEqual("paper.pdf", paths.safe_filename("paper.pdf"))
        self.assertEqual("passwd", paths.safe_filename("../../../etc/passwd"))
        self.assertEqual("x.pdf", paths.safe_filename("/tmp/x.pdf"))
        self.assertEqual("evil.pdf", paths.safe_filename("..\\..\\evil.pdf"))
        self.assertEqual("upload.bin", paths.safe_filename(""))

    def test_content_types(self):
        self.assertEqual("image/svg+xml", paths.content_type("a/b.svg"))
        self.assertEqual("model/stl", paths.content_type("a/b.STL"))
        self.assertEqual("image/vnd.dxf", paths.content_type("a/b.dxf"))
        self.assertEqual("image/png", paths.content_type("a/b.png"))
        self.assertTrue(paths.content_type("a/b.md").startswith("text/markdown"))
        self.assertTrue(paths.content_type("a/b.json").startswith("application/json"))
        self.assertEqual("application/octet-stream", paths.content_type("a/b.weird"))
        for suffix, kind in ((".json", "json"), (".svg", "svg"), (".stl", "stl"),
                             (".dxf", "dxf"), (".md", "md"), (".png", "png")):
            self.assertEqual(kind, paths.kind_for("x" + suffix))


class StageVocabulary(unittest.TestCase):

    def test_run_py_statuses_map_onto_the_api(self):
        from web.api import stages
        self.assertEqual("done", stages.api_status("ok"))
        self.assertEqual("not_implemented", stages.api_status("not_implemented"))
        self.assertEqual("failed", stages.api_status("error"))
        self.assertEqual("failed", stages.api_status("refused"))
        self.assertEqual("failed", stages.api_status("wrote_nothing"))
        self.assertEqual("failed", stages.api_status("something_new"),
                         "an unknown status must never read as success")
        for status in stages.STATUS_MAP.values():
            self.assertIn(status, stages.API_STATUSES)

    def test_the_spine_matches_assay2chip(self):
        from web.api import stages
        self.assertEqual(13, len(stages.SPINE))
        self.assertEqual(["ingest", "extract", "normalise", "triage", "partition",
                          "group", "build", "route", "protocol", "verify", "cfd",
                          "fabricate", "report"],
                         [name for _, name, _, _ in stages.SPINE])
        self.assertEqual(0, stages.resolve("s0"))
        self.assertEqual(6, stages.resolve("s6_build"))
        self.assertEqual(6, stages.resolve("build"))
        self.assertEqual(10, stages.resolve("10"))
        self.assertIsNone(stages.resolve("s99"))


class RepoChips(unittest.TestCase):
    """The real repo, read only: every chip must classify and every figure must resolve."""

    def test_every_repo_chip_lists_with_a_footprint_and_a_scale(self):
        from web.api import chips
        rows = chips.listing()
        self.assertTrue(rows, "chip/*.json and cad/*_spec.json should not be empty")
        for row in rows:
            self.assertTrue(row["id"])
            self.assertIn(row["scale"], ("demo", "target", "over_target", "oversize",
                                         "unknown"))
            self.assertEqual(row["demo"], row["scale"] == "demo")
            if row["footprint"]:
                self.assertIn("x_mm", row["footprint"])
            for figure in row["figures"]:
                self.assertTrue((paths.ROOT / figure).is_file(), figure)


def multipart(fields, *files):
    """(body, headers) for a multipart/form-data POST, with CRLF line endings."""
    boundary = "----a2cTestBoundary7a1f"
    out = []
    for name, value in fields.items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, name, value)).encode("utf-8"))
    for name, filename, blob in files:
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; "
                    "filename=\"%s\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                    % (boundary, name, filename)).encode("utf-8"))
        out.append(blob + b"\r\n")
    out.append(("--%s--\r\n" % boundary).encode("utf-8"))
    body = b"".join(out)
    return body, {"Content-Type": "multipart/form-data; boundary=%s" % boundary,
                  "Content-Length": str(len(body))}


if __name__ == "__main__":
    unittest.main(verbosity=2)

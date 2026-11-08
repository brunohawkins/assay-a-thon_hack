"""assay2chip/hosting: the durable queue and the run state machine. Offline, fast.

    python -m unittest tests.test_deploy_queue -v
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assay2chip.hosting.orchestrator import Orchestrator, collect, foam_cases  # noqa: E402
from assay2chip.hosting.queue import Queue  # noqa: E402


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="a2c_q_"))
        self.q = Queue(self.tmp / "q.sqlite3")
        self.q.create_run("r1", title="t", paper=None, options={})

    def test_enqueue_is_idempotent(self):
        a = self.q.enqueue("r1", "ingest", "ingest")
        b = self.q.enqueue("r1", "ingest", "ingest")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(self.q.jobs("r1")), 1)

    def test_run_idempotency_key(self):
        first = self.q.create_run("r2", title="t", paper=None, options={}, idem_key="k")
        again = self.q.create_run("r3", title="t", paper=None, options={}, idem_key="k")
        self.assertFalse(first["existing"])
        self.assertTrue(again["existing"])
        self.assertEqual(again["id"], "r2")
        self.assertIsNone(self.q.run("r3"))

    def test_lease_retry_with_backoff_then_fail(self):
        self.q.enqueue("r1", "foam", "f", resource="foam", max_attempts=2)
        self.assertIsNone(self.q.lease("light", "w", 60), "resource classes are separate")
        job = self.q.lease("foam", "w", 60)
        self.assertEqual(job["attempts"], 1)
        self.q.finish(job["id"], "w", "failed", retry=True, backoff_s=0.05, error="boom")
        self.assertEqual(self.q.job(job["id"])["status"], "queued")
        self.assertIsNone(self.q.lease("foam", "w", 60), "backoff not respected")
        time.sleep(0.08)
        job = self.q.lease("foam", "w", 60)
        self.assertEqual(job["attempts"], 2)
        self.q.finish(job["id"], "w", "failed", retry=True, error="boom again")
        self.assertEqual(self.q.job(job["id"])["status"], "failed")

    def test_only_the_lease_owner_can_finish(self):
        self.q.enqueue("r1", "plan", "plan")
        job = self.q.lease("light", "w1", 60)
        self.q.finish(job["id"], "intruder", "done")
        self.assertEqual(self.q.job(job["id"])["status"], "running")

    def test_release_does_not_spend_an_attempt(self):
        self.q.enqueue("r1", "design", "design")
        job = self.q.lease("light", "w", 60)
        self.q.release(job["id"], "w", "stopping")
        again = self.q.lease("light", "w", 60)
        self.assertEqual(again["attempts"], 1)

    def test_reclaim_expired_and_orphaned(self):
        self.q.enqueue("r1", "a", "a")
        self.q.enqueue("r1", "b", "b")
        a = self.q.lease("light", "old:1:x", 0.01)
        b = self.q.lease("light", "old:1:y", 600)
        time.sleep(0.02)
        self.assertEqual(self.q.reclaim(), [a["id"]], "only the expired lease")
        self.assertEqual(self.q.reclaim(live_owner_prefix="new:2:"), [b["id"]],
                         "a dead process's job comes back on restart")
        self.assertEqual({j["status"] for j in self.q.jobs("r1")}, {"queued"})

    def test_cancel(self):
        self.q.enqueue("r1", "a", "a")
        self.q.enqueue("r1", "b", "b")
        running = self.q.lease("light", "w", 60)
        self.q.cancel_run("r1")
        statuses = {j["key"]: j["status"] for j in self.q.jobs("r1")}
        self.assertEqual(statuses["b"], "cancelled")
        self.assertTrue(self.q.heartbeat(running["id"], "w", 60), "running job told to stop")
        self.assertIsNone(self.q.lease("light", "w", 60))


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class OrchestratorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="a2c_o_"))
        self.q = Queue(self.tmp / "q.sqlite3")
        self.ws_root = self.tmp / "ws"
        self.ws = SimpleNamespace(path=lambda run_id: self.ws_root / run_id)
        self.settings = SimpleNamespace(foam_max_steps_per_chip=2)
        self.orch = Orchestrator(self.q, self.ws, self.settings)
        self.q.create_run("r", title="t", paper="p.md", options={"llm": "stub"})
        w = self.ws_root / "r"
        write(w / "assay2chip/work/r/partition.json",
              {"chips": [{"chip_id": "r_c1"}, {"chip_id": "r_c2"}, {"chip_id": "r_c3"}]})
        for chip in ("r_c1", "r_c2"):
            write(w / "chip" / ("%s.json" % chip), {"id": chip})
        write(w / "protocols/r_c1.json", {"steps": [
            {"step": 1, "flow_ul_min": 5}, {"step": 2, "flow_ul_min": 50},
            {"step": 3, "flow_ul_min": 20}, {"step": 4, "flow_ul_min": 0}]})
        write(w / "protocols/r_c2.json", {"steps": [{"step": 1, "flow_ul_min": 0}]})

    def finish_next(self, resource="light", status="done"):
        job = self.q.lease(resource, "w", 60)
        self.assertIsNotNone(job)
        self.q.finish(job["id"], "w", status)
        self.orch.advance(job["run_id"])
        return job

    def test_foam_cases_pick_highest_flow_and_report_the_rest(self):
        cases, skipped = foam_cases(self.ws_root / "r", "r", 2)
        self.assertEqual([(c["chip"], c["step"]) for c in cases], [("r_c1", 2), ("r_c1", 3)])
        whys = " ".join(s["why"] for s in skipped)
        self.assertIn("beyond", whys)              # step 1 of c1
        self.assertIn("no protocol step moves fluid", whys)   # c2
        self.assertIn("did not produce chip/r_c3.json", whys)

    def test_whole_run(self):
        self.orch.advance("r")
        for kind in ("ingest", "extract", "plan", "design"):
            self.assertEqual(self.finish_next()["kind"], kind)
        keys = {j["key"] for j in self.q.jobs("r")}
        self.assertIn("foam:r_c1:2", keys)
        self.assertIn("fabricate", keys)
        self.finish_next("foam", "failed")            # a foam failure does not fail the run
        self.finish_next("foam")
        self.assertEqual(self.finish_next()["kind"], "fabricate")
        collect_job = self.q.lease("light", "w", 60)
        self.assertEqual(collect_job["kind"], "collect")
        summary = collect(self.q, self.ws_root / "r", "r")
        self.assertEqual(summary["counts"], {"done": 1, "failed": 1})
        self.q.finish(collect_job["id"], "w", "done")
        self.orch.advance("r")
        self.assertEqual(self.finish_next()["kind"], "report")
        self.assertEqual(self.q.run("r")["status"], "done_with_errors")

    def test_chain_failure_fails_the_run(self):
        self.orch.advance("r")
        self.finish_next()
        job = self.q.lease("light", "w", 60)
        self.q.finish(job["id"], "w", "failed", error="no model")
        self.orch.advance("r")
        run = self.q.run("r")
        self.assertEqual(run["status"], "failed")
        self.assertIn("extract", run["error"])


if __name__ == "__main__":
    unittest.main()

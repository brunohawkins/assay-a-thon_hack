"""assay2chip/hosting: pool sizing, job commands, and the whole local service end to end.

    python -m unittest tests.test_deploy_local -v

The end-to-end test runs deploy/local/smoke.py: it starts the real service on 127.0.0.1 in
a throwaway data directory, with fast fakes standing in for S5-S10, OpenFOAM, S11 and S12
(ingest, extract and plan run for real), and checks submission, fan-out, retry, cancel,
signed downloads, a stop-and-restart that resumes a job, and a clean stop. About a minute.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assay2chip.hosting import commands, config  # noqa: E402


def machine(cores, mem, disk=500.0):
    return config.Machine(arch="arm64", cores=cores, mem_gb=mem, disk_free_gb=disk, os="x")


class PoolTest(unittest.TestCase):
    def test_this_mac_defaults(self):
        plan = config.plan_pool(machine(8, 24))
        self.assertEqual((plan.light_slots, plan.foam_slots, plan.foam_cores_per_case),
                         (2, 1, 6))

    def test_configurable(self):
        plan = config.plan_pool(machine(8, 24), light_slots=1, foam_cores=3, foam_cap=2)
        self.assertEqual((plan.light_slots, plan.foam_slots, plan.foam_cores_per_case),
                         (1, 2, 3))

    def test_limits(self):
        big = config.plan_pool(machine(64, 256), foam_cores=8, foam_cap=16)
        self.assertEqual(big.foam_slots, 7)
        self.assertEqual(big.limited_by, "cores")
        short = config.plan_pool(machine(64, 20), foam_cores=8, foam_cap=16)
        self.assertEqual((short.foam_slots, short.limited_by), (1, "memory"))
        tiny = config.plan_pool(machine(2, 4, disk=5))
        self.assertEqual(tiny.foam_slots, 1, "never zero: the fan-out would wait for ever")

    def test_settings_from_env(self):
        s = config.load({"A2C_DATA_DIR": "/tmp/x", "A2C_LIGHT_SLOTS": "3",
                         "A2C_FOAM_MAX_PARALLEL": "2", "A2C_PORT": "9000"})
        self.assertEqual((s.light_slots, s.foam_cap, s.port, s.host),
                         (3, 2, 9000, "127.0.0.1"))
        self.assertEqual(s.db_path, Path("/tmp/x/queue.sqlite3"))


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.s = config.load({"A2C_DATA_DIR": "/tmp/x", "A2C_PYTHON": "py"})
        self.run = {"id": "r", "paper": "assay2chip/work/r/upload/p.md",
                    "options": {"llm": "stub"}}

    def test_segments_and_foam(self):
        argv = commands.build_argv("ingest", {"payload": {}}, self.run, self.s)
        self.assertEqual(argv[1:], ["assay2chip/run.py", "--id", "r", "--from", "s0", "--to",
                                    "s0", "--llm", "stub", "--keep-going", "--paper",
                                    "assay2chip/work/r/upload/p.md"])
        argv = commands.build_argv("design", {"payload": {}}, self.run, self.s)
        self.assertEqual(argv[argv.index("--from") + 1], "s5")
        self.assertEqual(argv[argv.index("--to") + 1], "s10")
        foam = commands.build_argv("foam", {"payload": {"chip": "r_c1", "step": 2,
                                                        "out": "o"}}, self.run, self.s)
        self.assertEqual(foam[1:3], ["-m", "assay2chip.foam.run"])
        self.assertIn("--no-video", foam)

    def test_env_carries_no_secret_it_does_not_need(self):
        base = {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "k", "AWS_SECRET_ACCESS_KEY": "s",
                "A2C_S3_SECRET_ACCESS_KEY": "s"}
        env = commands.build_env("design", self.run, self.s, foam_cores=6, base=base)
        self.assertNotIn("ANTHROPIC_API_KEY", env, "stub mode never sees the key")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("A2C_S3_SECRET_ACCESS_KEY", env)
        real = dict(self.run, options={"llm": "real"})
        self.assertIn("ANTHROPIC_API_KEY",
                      commands.build_env("extract", real, self.s, foam_cores=6, base=base))
        self.assertNotIn("ANTHROPIC_API_KEY",
                         commands.build_env("foam", real, self.s, foam_cores=6, base=base))
        foam = commands.build_env("foam", self.run, self.s, foam_cores=6, base=base)
        self.assertEqual(foam["FOAM_CORES"], "6")


@unittest.skipIf(os.environ.get("A2C_SKIP_SMOKE"), "A2C_SKIP_SMOKE set")
class SmokeTest(unittest.TestCase):
    def test_local_service_end_to_end(self):
        sys.path.insert(0, str(ROOT / "deploy" / "local"))
        import smoke
        data = Path(tempfile.mkdtemp(prefix="a2c_smoke_"))
        s = smoke.Smoke(data, real=False, verbose=False)
        try:
            results = s.check()
        finally:
            if s.proc is not None and s.proc.poll() is None:
                s.proc.terminate()
                s.proc.wait(timeout=60)
        self.assertEqual(results["status"], "done")
        self.assertGreater(results["foam"], 0)
        self.assertIn("resumed", results)


if __name__ == "__main__":
    unittest.main()

"""The demo profile and the two fixes that made the chain finish.

    python -m unittest tests.test_demo_profile -v
    A2C_DEMO_E2E=1 python -m unittest tests.test_demo_profile -v    # + the whole demo run

The fast tests pin three things:
  * the profile is read from ASSAY2CHIP_PROFILE, defaults to `full`, and rejects typos;
  * S9 downgrades "port drilled into a chamber" ONLY for a port in its own `<port>_pad`
    port block - a port drilled into a real chamber is still a blocker;
  * S10 does not send the run back to a stage that cannot read a CFD fix.
The opt-in test runs tools/demo_pipeline.py end to end and checks that every stage ran,
the claimed artefacts exist, and the profile is stated.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assay2chip.stages import _profile  # noqa: E402
from assay2chip.stages import s9_verify, s10_cfd  # noqa: E402


class Profile(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.pop(_profile.ENV, None)

    def tearDown(self):
        os.environ.pop(_profile.ENV, None)
        if self.saved is not None:
            os.environ[_profile.ENV] = self.saved

    def test_default_is_full_fidelity(self):
        self.assertEqual(_profile.name(), "full")
        self.assertIsNone(_profile.get("cfd_max_steps"))
        self.assertIn("full", _profile.banner())

    def test_demo_is_smaller_not_stubbed(self):
        os.environ[_profile.ENV] = "demo"
        self.assertEqual(_profile.get("cfd_max_steps"), 2)
        self.assertEqual(_profile.get("cfd_validation_scope"), "run")
        self.assertIn("Every stage runs for real", _profile.banner())

    def test_a_typo_is_an_error_not_a_silent_full_run(self):
        os.environ[_profile.ENV] = "dmeo"
        with self.assertRaises(ValueError):
            _profile.name()


def _chip(pad_id="inlet_1_pad", block="port", center=(1.0, 4.25)):
    return {"ports": [{"id": "inlet_1", "position": [1.0, 4.25, 1.8]}],
            "chambers": [{"id": pad_id, "block": block, "center": [*center, 0.65],
                          "size": [1.2, 1.2, 0.3]}]}


def _row(chamber="inlet_1_pad"):
    line = (f"ERROR  port 'inlet_1' and chamber '{chamber}' overlap: the port is drilled "
            f"into a chamber.")
    return s9_verify.classify(line, source="cad/make_figures.py", chip="c1", scope="pipeline")


class OwnPortPad(unittest.TestCase):
    def test_own_pad_is_info(self):
        out = s9_verify.own_port_pad(_chip(), _row())
        self.assertEqual(out["severity"], "info")
        self.assertIsNone(out["rerun_from"])

    def test_real_chamber_stays_a_blocker(self):
        out = s9_verify.own_port_pad(_chip("trap_1", "trap_chamber"), _row("trap_1"))
        self.assertEqual(out["severity"], "blocker")

    def test_pad_block_under_another_name_stays_a_blocker(self):
        out = s9_verify.own_port_pad(_chip("outlet_1_pad"), _row("outlet_1_pad"))
        self.assertEqual(out["severity"], "blocker")

    def test_pad_that_does_not_contain_the_port_stays_a_blocker(self):
        out = s9_verify.own_port_pad(_chip(center=(5.0, 5.0)), _row())
        self.assertEqual(out["severity"], "blocker")


class NoUselessRerun(unittest.TestCase):
    def test_s6_cannot_consume_a_cfd_fix_today(self):
        why = s10_cfd._fix_consumer("s6")
        self.assertIsNotNone(why, "if S6 now reads CFD fixes, drop this test")
        self.assertIn("CONSUMES_CFD_FIXES", why)


@unittest.skipUnless(os.environ.get("A2C_DEMO_E2E"), "set A2C_DEMO_E2E=1 to run the demo")
class DemoEndToEnd(unittest.TestCase):
    def test_demo_runs_every_stage_and_says_so(self):
        proc = subprocess.run([sys.executable, "tools/demo_pipeline.py", "--json",
                               "--id", "demo_test"], cwd=ROOT, capture_output=True,
                              text=True, timeout=1800)
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-2000:])
        self.assertIn("PROFILE: demo", proc.stdout)
        data = json.loads(proc.stdout[proc.stdout.rindex("\n{"):])
        self.assertTrue(data["chips"])
        self.assertEqual(len(data["chip_specs"]), len(data["chips"]))
        self.assertEqual(len(data["protocols"]), len(data["chips"]))
        self.assertTrue(data["verify_ok"])
        self.assertTrue(data["print_files"])
        manifest = json.loads((ROOT / "assay2chip/work/demo_test/run_manifest.json").read_text())
        ran = {s["stage"] for s in manifest["stages"] if s["status"] == "ok"}
        self.assertEqual(ran, {f"s{n}" for n in range(13)})


if __name__ == "__main__":
    unittest.main()

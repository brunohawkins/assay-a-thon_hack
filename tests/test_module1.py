"""Module 1: one spec, two variants. The control chip's pair valves must never open.

    python -m unittest discover -s tests -v
"""

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "tools"))

import check_chip_spec as ccs  # noqa: E402
import planner  # noqa: E402
import safety  # noqa: E402
import schema_io  # noqa: E402

CHIP = schema_io.load_json(ROOT / "cad" / "m1_culture_spec.json")
PROTOCOL = schema_io.load_json(ROOT / "protocols" / "m1_culture.json")
PAIRS = ["m1_valve_pair_1", "m1_valve_pair_2", "m1_valve_pair_3"]
BRANCHES = ["m1_valve_branch_1", "m1_valve_branch_2", "m1_valve_branch_3"]


class SafetyValves(unittest.TestCase):
    def test_opening_a_pair_valve_in_control_is_blocked(self):
        r = safety.check_static("valve", {"valves": ["m1_valve_pair_2"], "state": "open"}, CHIP, {"variant": "control"})
        self.assertEqual(r.status, "blocked")
        self.assertIn("control variant", r.reasons[0])

    def test_opening_pair_valves_in_interaction_is_approved(self):
        r = safety.check_static("valve", {"valves": PAIRS, "state": "open"}, CHIP, {"variant": "interaction"})
        self.assertEqual(r.status, "approved")

    def test_branch_valves_and_closing_pairs_are_fine_in_control(self):
        for args in ({"valves": BRANCHES, "state": "open"}, {"valves": PAIRS, "state": "closed"}):
            self.assertEqual(safety.check_static("valve", args, CHIP, {"variant": "control"}).status, "approved")

    def test_unknown_valve_is_blocked(self):
        r = safety.check_static("valve", {"valves": ["m1_valve_nope"], "state": "open"}, CHIP, {"variant": "interaction"})
        self.assertEqual(r.status, "blocked")


class Variants(unittest.TestCase):
    def test_control_run_skips_the_interaction_only_steps_and_never_opens_a_pair(self):
        sim = planner.plan(PROTOCOL, CHIP, run_params={"variant": "control"})
        opened = [v for e in sim.events if e["command"] == "valve" and e["args"]["state"] == "open" for v in e["args"]["valves"]]
        self.assertFalse(set(opened) & set(PAIRS))
        self.assertEqual(sorted(x["step"] for x in sim.log if x["kind"] == "skip"), [8, 10])

    def test_interaction_run_opens_the_pairs(self):
        sim = planner.plan(PROTOCOL, CHIP, run_params={"variant": "interaction"})
        opened = {v for e in sim.events if e["command"] == "valve" and e["args"]["state"] == "open" for v in e["args"]["valves"]}
        self.assertTrue(set(PAIRS) <= opened)

    def test_an_untagged_pair_opening_in_control_is_stopped_by_safety(self):
        proto = copy.deepcopy(PROTOCOL)
        del proto["steps"][7]["variant"]  # step 8 opens the pair valves
        with self.assertRaisesRegex(planner.PlanError, "safety blocked valve"):
            planner.plan(proto, CHIP, run_params={"variant": "control"})

    def test_unknown_variant_is_refused(self):
        with self.assertRaises(planner.PlanError):
            planner.plan(PROTOCOL, CHIP, run_params={"variant": "both"})

    def test_split_passes_through_junctions_to_the_culture_chambers(self):
        self.assertEqual(planner.downstream_chambers(CHIP, "m1_ipsc_1"), ["m1_neuron_1", "m1_astrocyte_1", "m1_microglia_1"])


class Checks(unittest.TestCase):
    def check(self, chip):
        params = schema_io.load_json(ROOT / "contracts" / "parameters.json")
        materials = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
        return ccs.check(chip, [], params, materials, {"laser_kerf_um": None, "min_embossed_feature_um": None})

    def test_microgrooves_are_not_checked_as_capsule_channels(self):
        f = self.check(CHIP)
        self.assertEqual(f.errors, [])
        self.assertTrue(any("not fabricable here" in t and "m1_sig_1" in t for t in f.todo))

    def test_crossing_channels_are_reported(self):
        f = self.check(CHIP)
        self.assertTrue(any("m1_ch_feed_2" in w and "m1_sig_1" in w and "cross" in w for w in f.warnings))

    def test_a_too_narrow_cell_channel_is_an_error(self):
        chip = copy.deepcopy(CHIP)
        next(c for c in chip["channels"] if c["id"] == "m1_ch_out_1")["width_mm"] = 0.03
        self.assertTrue(any("m1_ch_out_1" in e and "carries cells" in e for e in self.check(chip).errors))


if __name__ == "__main__":
    unittest.main()

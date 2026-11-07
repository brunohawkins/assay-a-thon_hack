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

# The demo chip is single-layer and has no on-chip valves, so the valve rules in safety.py are
# exercised on a fixture: the same chip with one valve of each kind added.
PAIRS = ["fx_valve_pair_1", "fx_valve_pair_2"]
BRANCHES = ["fx_valve_branch_1", "fx_valve_branch_2"]
VALVED = copy.deepcopy(CHIP)
VALVED["valves"] = ([{"id": v, "kind": "pair"} for v in PAIRS] + [{"id": v, "kind": "branch"} for v in BRANCHES])


class SafetyValves(unittest.TestCase):
    def test_the_single_layer_chip_has_no_valves(self):
        self.assertEqual(CHIP.get("valves", []), [])

    def test_opening_a_pair_valve_in_control_is_blocked(self):
        r = safety.check_static("valve", {"valves": [PAIRS[1]], "state": "open"}, VALVED, {"variant": "control"})
        self.assertEqual(r.status, "blocked")
        self.assertIn("control variant", r.reasons[0])

    def test_opening_pair_valves_in_interaction_is_approved(self):
        r = safety.check_static("valve", {"valves": PAIRS, "state": "open"}, VALVED, {"variant": "interaction"})
        self.assertEqual(r.status, "approved")

    def test_branch_valves_and_closing_pairs_are_fine_in_control(self):
        for args in ({"valves": BRANCHES, "state": "open"}, {"valves": PAIRS, "state": "closed"}):
            self.assertEqual(safety.check_static("valve", args, VALVED, {"variant": "control"}).status, "approved")

    def test_unknown_valve_is_blocked(self):
        r = safety.check_static("valve", {"valves": ["m1_valve_nope"], "state": "open"}, VALVED, {"variant": "interaction"})
        self.assertEqual(r.status, "blocked")

    def test_any_valve_command_is_blocked_on_the_valveless_chip(self):
        for variant in ("control", "interaction"):
            r = safety.check_static("valve", {"valves": PAIRS, "state": "open"}, CHIP, {"variant": variant})
            self.assertEqual(r.status, "blocked", variant)
            self.assertIn("not a valve", r.reasons[0])


class Variants(unittest.TestCase):
    """Routing now lives in the step `ports` field, so a protocol must not address valves the chip lacks."""

    def valve_events(self, sim):
        return [e for e in sim.events if e["command"] == "valve"]

    def test_control_run_skips_the_interaction_only_steps_and_issues_no_valve_commands(self):
        sim = planner.plan(PROTOCOL, CHIP, run_params={"variant": "control"})
        self.assertEqual(self.valve_events(sim), [])
        tagged = sorted(s["step"] for s in PROTOCOL["steps"] if s.get("variant") == "interaction")
        self.assertEqual(sorted(x["step"] for x in sim.log if x["kind"] == "skip"), tagged)

    def test_interaction_run_plans_without_valve_commands(self):
        sim = planner.plan(PROTOCOL, CHIP, run_params={"variant": "interaction"})
        self.assertEqual(self.valve_events(sim), [])
        self.assertEqual([x for x in sim.log if x["kind"] == "skip" and x["step"] in
                          {s["step"] for s in PROTOCOL["steps"] if s.get("variant") == "interaction"}], [])

    def test_every_step_target_exists_on_the_chip(self):
        ids = {x["id"] for g in ("chambers", "ports", "valves") for x in CHIP.get(g, [])}
        for step in PROTOCOL["steps"]:
            for t in ([step["target"]] if step.get("target") else []) + list(step.get("targets") or []):
                self.assertIn(t, ids, f"step {step['step']} targets {t}")

    def test_unknown_variant_is_refused(self):
        with self.assertRaises(planner.PlanError):
            planner.plan(PROTOCOL, CHIP, run_params={"variant": "both"})

    def test_split_passes_through_junctions_to_the_lane_chambers(self):
        chambers = {c["id"]: c for c in CHIP["chambers"]}
        got = planner.downstream_chambers(CHIP, "m1_ipsc_1")
        self.assertTrue(got)
        self.assertFalse({c for c in got if chambers[c]["type"] in planner.PASS_THROUGH}, "junctions are passed through")
        culture = [c["id"] for c in CHIP["chambers"] if c["type"] == "culture"]
        self.assertEqual(len(got), len(culture), "one downstream chamber per lane")
        for lane in got:
            reach = {lane}
            frontier = [lane]
            while frontier:
                nxt = [c["to"] for c in CHIP["channels"] if c["from"] in frontier and c["to"] in chambers and c["to"] not in reach]
                reach |= set(nxt)
                frontier = nxt
            self.assertEqual(len(reach & set(culture)), 1, f"{lane} leads to exactly one culture chamber")


class Checks(unittest.TestCase):
    def check(self, chip):
        params = schema_io.load_json(ROOT / "contracts" / "parameters.json")
        materials = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
        return ccs.check(chip, [], params, materials, {"laser_kerf_um": None, "min_embossed_feature_um": None})

    def test_demo_chip_has_no_unprintable_microgrooves_or_crossings(self):
        f = self.check(CHIP)
        self.assertEqual(f.errors, [])
        ids = {c["id"] for c in CHIP["chambers"]} | {c["id"] for c in CHIP["channels"]}
        self.assertFalse({i for i in ids if "sig" in i or "groove" in i}, "signals-only microgrooves were removed")
        self.assertFalse([w for w in f.warnings if "cross" in w], "single layer: nothing may cross")

    def test_a_too_narrow_cell_channel_is_an_error(self):
        chip = copy.deepcopy(CHIP)
        next(c for c in chip["channels"] if c["id"] == "m1_ch_out_1")["width_mm"] = 0.03
        self.assertTrue(any("m1_ch_out_1" in e and "carries cells" in e for e in self.check(chip).errors))


if __name__ == "__main__":
    unittest.main()

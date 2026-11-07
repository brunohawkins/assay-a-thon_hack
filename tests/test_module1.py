"""Module 1 on the chip and protocol we SHIP: chip/module1_v1.json and
protocols/module1_v1.json.

This file used to load cad/m1_culture_spec.json, which its own module block marks
`superseded` and which tests/test_pipeline.py asserts is out of the pipeline. The shipped
chip is a different design: it has no valves and no run parameters at all, because routing
is done entirely by which port is driven, driven and vented (see the chip's description),
so the variant/pair-valve machinery below is exercised on a FIXTURE and labelled as such.

    python -m unittest discover -s tests -v
"""

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "cad"))

import check_chip_spec as ccs  # noqa: E402
import make_figures as mf  # noqa: E402
import planner  # noqa: E402
import safety  # noqa: E402
import schema_io  # noqa: E402

CHIP = schema_io.load_json(ROOT / "chip" / "module1_v1.json")
PROTOCOL = schema_io.load_json(ROOT / "protocols" / "module1_v1.json")
PARAMS = schema_io.load_json(ROOT / "contracts" / "parameters.json")
MATERIALS = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
CULTURE = [c["id"] for c in CHIP["chambers"] if c["type"] != "pool"]
POOL = next(c["id"] for c in CHIP["chambers"] if c["type"] == "pool")

# A FIXTURE, not a chip: NO spec in chip/ or cad/ has a single valve, because a pneumatic
# membrane valve needs a second fluidic layer and we print one (contracts/blocks.json
# "excluded"), and tests/test_envelope.py asserts that for every chip on disk. The three
# tests marked ValveRulesNoChipUses below therefore cover safety.py code paths that NO
# current chip can reach; they exist so the rules are not silently broken, and they are the
# only place in the suite allowed to invent valves.
PAIRS = ["fx_valve_pair_1", "fx_valve_pair_2"]
BRANCHES = ["fx_valve_branch_1", "fx_valve_branch_2"]
VALVED = copy.deepcopy(CHIP)
VALVED["valves"] = ([{"id": v, "kind": "pair"} for v in PAIRS] + [{"id": v, "kind": "branch"} for v in BRANCHES])


class ShippedChip(unittest.TestCase):
    def test_the_pipeline_still_points_at_this_chip_and_protocol(self):
        (_, pipe), = schema_io.pipelines()
        entry = next(m for m in pipe["modules"] if m["module"] == "module1")
        self.assertEqual(schema_io.pipeline_chip(entry)[0], "chip/module1_v1.json")
        self.assertEqual(entry["protocol"], "protocols/module1_v1.json")

    def test_the_old_demo_chip_is_superseded_by_this_one(self):
        old = schema_io.load_json(ROOT / "cad" / "m1_culture_spec.json")["module"]
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["superseded_by"], "chip/module1_v1.json")

    def test_the_chip_has_no_valves_and_no_run_parameters(self):
        # Routing is by port pressure, so there is nothing to switch and no variant to pick.
        self.assertEqual(CHIP.get("valves", []), [])
        self.assertEqual(CHIP.get("run_parameters", {}), {})
        self.assertEqual({s.get("variant") for s in PROTOCOL["steps"]}, {None})

    def test_any_valve_command_is_blocked_on_the_shipped_chip(self):
        r = safety.check_static("valve", {"valves": PAIRS, "state": "open"}, CHIP, {})
        self.assertEqual(r.status, "blocked")
        self.assertIn("not a valve", r.reasons[0])


class ValveRulesNoChipUses(unittest.TestCase):
    """safety.py's valve rules, on the VALVED fixture. No chip in the repo has a valve
    (single fluidic layer), so these cover code no current artefact can reach. If valves
    ever become buildable, point them at the real chip and delete the fixture."""

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
        r = safety.check_static("valve", {"valves": ["m1_valve_nope"], "state": "open"}, VALVED,
                                {"variant": "interaction"})
        self.assertEqual(r.status, "blocked")


class Protocol(unittest.TestCase):
    def test_the_shipped_protocol_plans_on_the_shipped_chip(self):
        sim = planner.plan(PROTOCOL, CHIP)
        self.assertEqual(len(sim.events), len(PROTOCOL["steps"]))
        self.assertEqual([e for e in sim.events if e["command"] == "valve"], [])
        self.assertEqual([x for x in sim.log if x["kind"] == "skip"], [])

    def test_every_step_target_exists_on_the_chip(self):
        ids = {x["id"] for g in ("chambers", "ports", "valves") for x in CHIP.get(g, [])}
        for step in PROTOCOL["steps"]:
            for t in ([step["target"]] if step.get("target") else []) + list(step.get("targets") or []):
                self.assertIn(t, ids, f"step {step['step']} targets {t}")

    def test_an_unknown_run_parameter_is_refused(self):
        with self.assertRaises(planner.PlanError):
            planner.plan(PROTOCOL, CHIP, run_params={"variant": "control"})

    def test_the_pool_step_gathers_every_culture_chamber_through_the_junction(self):
        # The three lanes meet at the pool chamber, OUTSIDE the chambers, which is the whole
        # point of the layout: no lane's medium crosses another lane.
        self.assertEqual(sorted(planner.upstream_chambers(CHIP, POOL)), sorted(CULTURE))
        pool_steps = [s for s in PROTOCOL["steps"] if s["operation"] == "pool"]
        self.assertTrue(pool_steps, "the shipped protocol pools the lanes")
        for step in pool_steps:
            command, args = planner.step_to_command(step, CHIP)
            self.assertEqual((command, sorted(args["sources"])), ("pool", sorted(CULTURE)))

    def test_each_lane_reaches_the_pool_without_passing_through_another_lane(self):
        chambers = {c["id"]: c for c in CHIP["chambers"]}
        for lane in CULTURE:
            reach, frontier = {lane}, [lane]
            while frontier:
                nxt = [c["to"] for c in CHIP["channels"]
                       if c["from"] in frontier and c["to"] in chambers and c["to"] not in reach]
                reach |= set(nxt)
                frontier = nxt
            self.assertEqual(reach & set(CULTURE), {lane}, f"{lane} drains through another lane")
            self.assertIn(POOL, reach, f"{lane} does not reach {POOL}")


class Checks(unittest.TestCase):
    def check(self, chip):
        return ccs.check(chip, [], PARAMS, MATERIALS, {"laser_kerf_um": None, "min_embossed_feature_um": None})

    def test_the_shipped_chip_has_no_design_errors(self):
        self.assertEqual(self.check(CHIP).errors, [])

    def test_no_microgroove_features_survive(self):
        ids = {c["id"] for c in CHIP["chambers"]} | {c["id"] for c in CHIP["channels"]}
        self.assertFalse({i for i in ids if "sig" in i or "groove" in i}, "signals-only microgrooves were removed")

    def test_nothing_crosses_in_the_one_layer(self):
        # ccs.crossings() only inspects channels that declare an explicit `path`, and NO
        # channel on this chip does, so it returns [] whatever the layout is: asserting on
        # it alone proves nothing. The real planar check is cad/make_figures.py, which
        # derives each channel's centreline from the chip and is the only collision detector
        # in the repo. Both are asserted here, and the vacuous one is labelled.
        with_path = [c["id"] for c in CHIP["channels"] if c.get("path")]
        self.assertEqual(with_path, [], "if channels gain paths, ccs.crossings() stops being vacuous here")
        self.assertEqual(ccs.crossings(CHIP), [])
        msgs, tightest = mf.geometry_warnings(CHIP)
        self.assertEqual([m for m in msgs if m.startswith("ERROR")], [])
        self.assertGreater(tightest[0], 0, f"features touch: {tightest[1]}")

    def test_every_channel_is_wide_enough_for_what_it_carries(self):
        d = CHIP.get("capsule", {}).get("diameter_um", PARAMS["parameters"]["capsule.diameter_um"]["value"])
        cell_min = PARAMS["parameters"]["dissociation.outlet_min_width_um"]["value"]
        for ch in CHIP["channels"]:
            carries = ch.get("carries", "capsules")
            if carries in ("liquid", "oil", "signals") or ch.get("capsule_passable") is False:
                continue
            floor = cell_min if carries == "cells" else ccs.CHANNEL_MIN_FACTOR * d
            self.assertGreaterEqual(ch["width_mm"] * 1000, floor, f"channel '{ch['id']}' carries {carries}")


if __name__ == "__main__":
    unittest.main()

"""Each rule in operator/safety.py, on the wash MVP chip."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))

import safety  # noqa: E402
import schema_io  # noqa: E402
from state import ChipState  # noqa: E402

CHIP = schema_io.load_json(ROOT / "cad" / "wash_mvp_spec.json")
PARAMS = schema_io.load_json(ROOT / "contracts" / "parameters.json")


def state(on_mesh=10000, suspended=0, **reservoirs):
    s = ChipState.from_chip_spec(CHIP, PARAMS, reservoirs_ul={"wash_buffer": 1000, "resuspension_buffer": 1000,
                                                             "capsule_suspension": 1000, **reservoirs})
    s.chambers["trap_1"].capsules_on_mesh = on_mesh
    s.chambers["trap_1"].capsules_suspended = suspended
    return s


WASH = {"target": "trap_1", "reagent": "wash_buffer", "volume_ul": 20, "washes": 2, "flow_ul_min": 40}


class Rules(unittest.TestCase):
    def test_normal_wash_is_approved(self):
        self.assertEqual(safety.check("exchange", WASH, state(), CHIP).status, "approved")

    def test_unknown_target_is_blocked(self):
        r = safety.check("exchange", {**WASH, "target": "trap_9"}, state(), CHIP)
        self.assertEqual(r.status, "blocked")
        self.assertIn("trap_9 not in chip", r.reasons[0])

    def test_port_as_target_is_blocked(self):
        self.assertEqual(safety.check("exchange", {**WASH, "target": "waste_1"}, state(), CHIP).status, "blocked")

    def test_pressure_that_pushes_capsules_through_is_blocked(self):
        r = safety.check("exchange", {**WASH, "flow_ul_min": 120}, state(), CHIP)   # 6 kPa >= 5 kPa
        self.assertEqual(r.status, "blocked")
        self.assertIn("pushed through", r.reasons[0])

    def test_pressure_near_the_limit_warns(self):
        r = safety.check("exchange", {**WASH, "flow_ul_min": 80}, state(), CHIP)    # 4 kPa > 70% of 5
        self.assertEqual(r.status, "warning")

    def test_backflush_has_no_mesh_pressure_rule(self):
        r = safety.check("exchange", {**WASH, "flow_ul_min": 120, "direction": "backflush"}, state(), CHIP)
        self.assertEqual(r.status, "approved")

    def test_waste_overflow_is_blocked(self):
        s = state()
        s.waste_ul = s.waste_capacity_ul - 10
        r = safety.check("exchange", WASH, s, CHIP)
        self.assertEqual(r.status, "blocked")
        self.assertIn("waste would overflow", r.reasons[0])

    def test_collection_overflow_is_blocked(self):
        s = state(on_mesh=0, suspended=10000)
        s.collection_ul = s.collection_capacity_ul - 5
        r = safety.check("release", {"target": "trap_1", "reagent": "resuspension_buffer", "volume_ul": 30}, s, CHIP)
        self.assertIn("collection would overflow", r.reasons[0])

    def test_dry_reservoir_is_blocked_and_low_one_warns(self):
        self.assertIn("run dry", safety.check("exchange", WASH, state(wash_buffer=30), CHIP).reasons[0])
        self.assertEqual(safety.check("exchange", WASH, state(wash_buffer=42), CHIP).status, "warning")

    def test_temperature_above_material_limit_is_blocked(self):
        r = safety.check("hold_temp", {"target": "trap_1", "temp_c": 98, "duration_s": 60}, state(), CHIP)
        self.assertEqual(r.status, "blocked")
        self.assertIn("max working temperature", r.reasons[0])
        ok = safety.check("hold_temp", {"target": "trap_1", "temp_c": 37, "duration_s": 60}, state(), CHIP)
        self.assertEqual(ok.status, "approved")

    def test_release_with_capsules_on_mesh_needs_backflush(self):
        r = safety.check("release", {"target": "trap_1", "reagent": "resuspension_buffer", "volume_ul": 30}, state(), CHIP)
        self.assertEqual(r.status, "blocked")
        self.assertIn("back-flush first", r.reasons[0])
        ok = safety.check("release", {"target": "trap_1", "reagent": "resuspension_buffer", "volume_ul": 30},
                          state(on_mesh=0, suspended=10000), CHIP)
        self.assertEqual(ok.status, "approved")


if __name__ == "__main__":
    unittest.main()

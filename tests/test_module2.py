"""Module 2: the encapsulation generator and the design rules it must meet.

    python -m unittest discover -s tests -v
"""

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "cad" / "generators"))

import check_chip_spec as ccs  # noqa: E402
import genlib  # noqa: E402
import m2_encapsulation as m2  # noqa: E402
import planner  # noqa: E402
import schema_io  # noqa: E402

PARAMS = schema_io.load_json(ROOT / "contracts" / "parameters.json")
MATERIALS = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
FABRICATION_FLOORS = schema_io.load_json(ROOT / "contracts" / "blocks.json")["fabrication_floors"]
FAB = {"laser_kerf_um": None, "min_embossed_feature_um": None}
DEFAULTS = genlib.validate_params("m2_encapsulation", {})


def check(chip):
    return ccs.check(chip, [], PARAMS, MATERIALS, FAB)


class Generator(unittest.TestCase):
    def test_defaults_build_a_valid_chip_that_passes_the_checks(self):
        spec = m2.build(DEFAULTS)
        self.assertEqual(schema_io.schema_errors("chip_spec", spec), [])
        f = check(spec)
        self.assertEqual(f.errors, [])
        self.assertEqual(ccs.crossings(spec), [])  # the oil branches go around the aqueous inlets

    def test_cold_delay_volume_is_flagged_for_off_chip_tubing(self):
        # Derived from the parameters, not restated: the delay line has to hold everything
        # the three pumps push during the gelation hold.
        total_ul_h = sum(DEFAULTS[k] for k in ("core_flow_ul_h", "shell_flow_ul_h", "oil_flow_ul_h"))
        lo, hi = m2.cold_delay_volume_ul(DEFAULTS)
        self.assertAlmostEqual(lo, total_ul_h * DEFAULTS["gelation_time_min_low"] / 60)
        self.assertAlmostEqual(hi, total_ul_h * DEFAULTS["gelation_time_min_high"] / 60)
        self.assertLess(lo, hi)
        stage = m2.build(DEFAULTS)["stages"][0]
        self.assertEqual(stage["location"], "off_chip")
        self.assertIn("off-chip tubing on a cold block", stage["note"].lower())

    def test_protocol_is_valid_and_runs_on_the_chip(self):
        spec, proto = m2.build(DEFAULTS), m2.protocol(DEFAULTS)
        self.assertEqual(schema_io.schema_errors("protocol", proto), [])
        sim = planner.plan(proto, spec)
        self.assertEqual([e["command"] for e in sim.events][:3], ["encapsulate", "hold_temp", "load"])
        self.assertIn("cure", [e["command"] for e in sim.events])

    def test_parameters_change_the_layout(self):
        wide = m2.build(genlib.validate_params("m2_encapsulation", {"y_junction_angle_deg": 120}))
        yj = lambda s: next(c for c in s["chambers"] if c["id"] == "m2_yjunction_1")["center"][0]  # noqa: E731
        self.assertLess(yj(wide), yj(m2.build(DEFAULTS)))  # a wider Y sits closer to the inlets

    def test_a_layout_that_does_not_fit_is_refused(self):
        with self.assertRaises(genlib.ParamError):
            m2.build(genlib.validate_params("m2_encapsulation", {"separation_length_mm": 60}))

    def test_bad_parameters_are_refused_by_the_schema(self):
        with self.assertRaises(genlib.ParamError):
            genlib.validate_params("m2_encapsulation", {"y_junction_angle_deg": 10})
        with self.assertRaises(genlib.ParamError):
            genlib.validate_params("m2_encapsulation", {"no_such_parameter": 1})


class Rules(unittest.TestCase):
    """The design rules ON THE BUILT SPEC, and the upstream enforcement that makes them hold.

    These tests used to WRITE a violation into the built spec, because the generator clamps
    every sub-floor in-plane feature up to contracts/blocks.json's min_open_channel_mm and
    so cannot emit one. Writing the violation in tested only the checker, which
    tests/test_check_chip_spec.py already covers on a synthetic chip. What is worth asserting
    here is what the generator actually produces, and the clamp that guarantees it.
    """

    FLOOR_UM = FABRICATION_FLOORS["min_open_channel_mm"] * 1000

    def test_every_cell_filter_in_the_built_spec_is_finer_than_the_built_nozzle(self):
        spec = m2.build(DEFAULTS)
        nozzle = spec["encapsulation"]["nozzle_um"]
        gaps = [("cell_filter", spec["cell_filter"]["gap_um"])] + \
               [(c["id"], c["filter"]["gap_um"]) for c in spec["chambers"]
                if (c.get("filter") or {}).get("purpose") == "cells"]
        self.assertTrue(gaps)
        for name, gap in gaps:
            self.assertLess(gap, nozzle, f"{name} would pass clumps the {nozzle:g} um nozzle cannot")
        # Honest reason it holds: the NOZZLE was clamped up to the in-plane floor, from the
        # requested width. The margin is a consequence of the clamp, not of the design, and
        # the chip's own description says it cannot make 70 um capsules at that width.
        self.assertGreater(nozzle, DEFAULTS["nozzle_width_um"])
        self.assertEqual(nozzle, self.FLOOR_UM)

    def test_the_generator_cannot_emit_an_in_plane_feature_below_the_floor(self):
        # This is the rule that makes "write the violation in" the only way to break the
        # capsule-width rule, so it is the rule under test.
        for request in ({}, {"nozzle_width_um": 10}, {"separation_width_um": 20}, {"coflow_width_um": 15},
                        {"inlet_width_um": 25}):
            spec = m2.build(genlib.validate_params("m2_encapsulation", request))
            with self.subTest(request=request):
                for ch in spec["channels"]:
                    self.assertGreaterEqual(ch["width_mm"] * 1000, self.FLOOR_UM, ch["id"])
                self.assertEqual(check(spec).errors, [])
                if request:
                    self.assertIn("CLAMPS:", spec["description"])

    def test_every_capsule_carrying_channel_in_the_built_spec_meets_the_capsule_rule(self):
        spec = m2.build(DEFAULTS)
        d = spec["capsule"]["diameter_um"]
        floor = ccs.CHANNEL_MIN_FACTOR * d
        carriers = [ch for ch in spec["channels"] if ch.get("carries") in ("capsules", "droplets")]
        self.assertTrue(carriers)
        for ch in carriers:
            self.assertGreaterEqual(ch["width_mm"] * 1000, floor,
                                    f"channel '{ch['id']}' carries {ch['carries']} of {d:g} um")

    def test_channels_before_the_nozzle_are_not_held_to_the_capsule_rule(self):
        spec = copy.deepcopy(m2.build(DEFAULTS))
        self.assertFalse(any("m2_ch_coflow_1" in e or "m2_ch_oil_1" in e for e in check(spec).errors))


if __name__ == "__main__":
    unittest.main()

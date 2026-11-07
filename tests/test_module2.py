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
        lo, hi = m2.cold_delay_volume_ul(DEFAULTS)
        self.assertAlmostEqual(lo, 262.5)
        self.assertAlmostEqual(hi, 525.0)
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
    def test_cell_filter_gap_equal_to_the_nozzle_is_an_error(self):
        # The generator now clamps sub-200 um values up to the in-plane fabrication floor,
        # so the violation has to be written into the built spec rather than requested from
        # the generator. The RULE is still the thing under test.
        spec = m2.build(DEFAULTS)
        spec["cell_filter"]["gap_um"] = spec["encapsulation"]["nozzle_um"]
        for c in spec["chambers"]:
            if c["id"] == "m2_filter_core_1" and "filter" in c:
                c["filter"]["gap_um"] = spec["encapsulation"]["nozzle_um"]
        errors = check(spec).errors
        self.assertTrue(any("cell_filter.gap_um" in e and ">= encapsulation nozzle" in e for e in errors))
        self.assertTrue(any("filter 'm2_filter_core_1'" in e for e in errors))

    def test_capsule_channel_narrower_than_1_5_d_is_an_error(self):
        spec = m2.build(DEFAULTS)
        for ch in spec["channels"]:            # written in, not requested: see the note above
            if ch["id"] == "m2_ch_separation_1":
                ch["width_mm"] = 0.100          # < 1.5 x 70 um = 105 um
        self.assertTrue(any("m2_ch_separation_1" in e and "< 1.5 x capsule diameter" in e for e in check(spec).errors))

    def test_channels_before_the_nozzle_are_not_held_to_the_capsule_rule(self):
        spec = copy.deepcopy(m2.build(DEFAULTS))
        self.assertFalse(any("m2_ch_coflow_1" in e or "m2_ch_oil_1" in e for e in check(spec).errors))


if __name__ == "__main__":
    unittest.main()

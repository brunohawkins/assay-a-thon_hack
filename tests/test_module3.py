"""Module 3: the parametric capsule-wash generator.

    python -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "cad" / "generators"))

import genlib  # noqa: E402
import m3_wash as m3  # noqa: E402
import planner  # noqa: E402
import schema_io  # noqa: E402

P = genlib.validate_params("m3_wash", {})


def params(**kw):
    return genlib.validate_params("m3_wash", kw)


class Generator(unittest.TestCase):
    def test_defaults_match_the_wash_mvp(self):
        spec = m3.build(P)
        trap = spec["chambers"][0]
        self.assertEqual(trap["size"][:2], [7.0, 7.0])
        self.assertEqual(trap["retention"]["opening_um"], 25)
        self.assertEqual(schema_io.schema_errors("chip_spec", spec), [])

    def test_mesh_at_or_above_0_4_d_is_refused(self):
        with self.assertRaisesRegex(genlib.ParamError, "must be < 0.4"):
            m3.build(params(mesh_opening_um=30))           # 30 >= 28 um for 70 um capsules
        m3.build(params(mesh_opening_um=30, capsule_diameter_um=80))  # fine for 80 um capsules (< 32)

    def test_chamber_lower_than_the_swollen_capsule_is_refused(self):
        with self.assertRaisesRegex(genlib.ParamError, "swollen"):
            m3.build(params(chamber_height_um=120))

    def test_channel_factor_outside_1_5_to_2_is_refused_by_the_schema(self):
        with self.assertRaises(genlib.ParamError):
            params(channel_width_factor=2.5)

    def test_trap_grows_with_capsule_count_and_inlets_and_backflush_are_options(self):
        big = m3.build(params(capsule_count=40000, inlets=3, backflush=False))
        self.assertAlmostEqual(big["chambers"][0]["size"][0], 14.0)
        ids = {p["id"] for p in big["ports"]}
        self.assertTrue({"m3_inlet_1", "m3_inlet_2", "m3_inlet_3"} <= ids)
        self.assertNotIn("m3_backflush_1", ids)

    def test_protocol_timings_come_from_flow_calc_and_run(self):
        spec = m3.build(P)
        proto = m3.protocol(P, spec)
        self.assertEqual(schema_io.schema_errors("protocol", proto), [])
        wash = proto["steps"][1]
        self.assertEqual(wash["duration_s"], round(3 * wash["volume_ul"] / wash["flow_ul_min"] * 60, 1))
        self.assertLess(m3.hydraulics(P)["pressure_kpa"], m3.hydraulics(P)["limit_kpa"])
        planner.plan(proto, spec)

    def test_dye_test_keeps_measured_values(self):
        spec = m3.build(P)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dye.json"
            with mock.patch.object(m3, "DYE_TEST", path):
                first = m3.dye_test(P, spec)
                first["points"][2]["measured_dye_fraction"] = 0.41
                schema_io.write_json(path, first)
                again = m3.dye_test(P, spec)
        self.assertEqual(again["points"][2]["measured_dye_fraction"], 0.41)
        self.assertEqual(again["measured_points"], 1)
        self.assertAlmostEqual(again["points"][2]["predicted_dye_fraction"], 0.3679, places=3)  # e^-1 at one chamber volume


if __name__ == "__main__":
    unittest.main()

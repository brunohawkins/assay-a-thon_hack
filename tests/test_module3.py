"""Module 3: the parametric capsule-wash generator.

    python -m unittest discover -s tests -v
"""

import math
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
import sizing  # noqa: E402

P = genlib.validate_params("m3_wash", {})
PARAMS = schema_io.load_json(ROOT / "contracts" / "parameters.json")["parameters"]
WEIR_GAP_UM = schema_io.load_json(ROOT / "contracts" / "blocks.json")["retention"]["weir_geometry"]["gap_um"]
EXCHANGE_TARGET = PARAMS["exchange.target_fraction"]["value"]


def params(**kw):
    return genlib.validate_params("m3_wash", kw)


class Generator(unittest.TestCase):
    def test_defaults_match_the_wash_mvp(self):
        spec = m3.build(P)
        trap = spec["chambers"][0]
        # The trap is a square big enough for one layer of capsules: side and gap are DERIVED
        # from operator/sizing.py and contracts/blocks.json, not restated here.
        side = round(math.sqrt(sizing.compute({"capsule_count": P["capsule_count"],
                                               "capsule_diameter_um": P["capsule_diameter_um"]})
                               ["monolayer_area_cm2"] * 100), 3)
        self.assertEqual(trap["size"][:2], [side, side])
        self.assertEqual(trap["retention"]["opening_um"], WEIR_GAP_UM)
        self.assertAlmostEqual(trap["volume_ul"], side * side * trap["height_um"] / 1000, places=3)
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
        count = 4 * P["capsule_count"]
        big = m3.build(params(capsule_count=count, inlets=3, backflush=False))
        # A monolayer's AREA is proportional to the capsule count, so 4x the capsules is 2x
        # the side. Both figures come from the sizing function.
        self.assertAlmostEqual(big["chambers"][0]["size"][0], m3.trap_side_mm(params(capsule_count=count)))
        self.assertAlmostEqual(big["chambers"][0]["size"][0], 2 * m3.trap_side_mm(P), places=2)
        ids = {p["id"] for p in big["ports"]}
        self.assertTrue({"m3_inlet_1", "m3_inlet_2", "m3_inlet_3"} <= ids)
        self.assertNotIn("m3_backflush_1", ids)

    def test_protocol_volumes_are_sized_against_the_chamber(self):
        # The old assertion here recomputed duration_s from the generator's own arithmetic
        # (washes x volume / flow x 60), which cannot fail, and never compared a volume with
        # the chamber it has to fill or sweep. These are the relations that matter.
        spec = m3.build(P)
        proto = m3.protocol(P, spec)
        self.assertEqual(schema_io.schema_errors("protocol", proto), [])
        vc = spec["chambers"][0]["volume_ul"]
        load = next(s for s in proto["steps"] if s["operation"] == "load")
        self.assertLessEqual(load["volume_ul"], vc, "a load fills the chamber; it must not overfill it")
        for step in (s for s in proto["steps"] if s["operation"] == "exchange"):
            with self.subTest(step=step["step"], reagent=step["reagent"]):
                # Each wash must sweep the chamber, and the washes together must reach the
                # contract's exchange target for a well-mixed chamber: 1 - exp(-V/Vc).
                self.assertGreaterEqual(step["volume_ul"], vc, f"{step['volume_ul'] / vc:.2f} chamber volumes")
                if step.get("direction") != "backflush":
                    swept = 1 - math.exp(-step["washes"] * step["volume_ul"] / vc)
                    self.assertGreaterEqual(swept, EXCHANGE_TARGET, f"{swept:.3%} exchanged")
        release = next(s for s in proto["steps"] if s["operation"] == "release")
        self.assertGreater(release["volume_ul"], vc, "a release sweeps the chamber out, not just fills it")

    def test_protocol_pressure_stays_under_the_limit_and_the_plan_runs(self):
        spec = m3.build(P)
        hy = m3.hydraulics(P)
        self.assertLess(hy["pressure_kpa"], hy["limit_kpa"])
        planner.plan(m3.protocol(P, spec), spec)

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
        # point 2 is one chamber volume (points step by half a volume), so e^-1 is left.
        self.assertAlmostEqual(again["points"][2]["volume_ul"], spec["chambers"][0]["volume_ul"], places=2)
        self.assertAlmostEqual(again["points"][2]["predicted_dye_fraction"], math.exp(-1), places=3)


if __name__ == "__main__":
    unittest.main()

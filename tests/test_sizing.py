"""Checks operator/sizing.py against Szonja's sheet (contracts/sources/chip_geometry_requirements.xlsx).

    python -m unittest discover -s tests -v
"""

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))

import sizing  # noqa: E402

# Values the sheet itself computes for its defaults (cached cell values, Sizing Calculator tab).
SHEET = {
    "capsule_volume_nl": 0.179594380030217,           # B23
    "packed_volume_ul": 28.0616218797213,             # B24
    "monolayer_area_cm2": 4.9,                        # B25
    "trap_gap_low_um": 21,                            # B26
    "trap_gap_high_um": 28,                           # B27
    "channel_width_low_um": 105,                      # B28
    "channel_width_high_um": 140,                     # B29
    "swollen_diameter_um": 140,                       # B30
    "barcode_combinations": 512,                      # B31
    "aqueous_flow_ul_h": 350,                         # B34
    "capsules_per_hour": 1948836.03785994,            # B35
    "generation_rate_hz": 541.343343849984,           # B36
    "cells_per_hour": 150000,                         # B37
    "mean_cells_per_capsule": 0.0769690200129499,     # B38
    "fraction_single_cell": 0.0712670424898798,       # B39
    "fraction_multi_of_occupied": 0.0379908729079278, # B40
    "hours_to_encapsulate": 0.0513126800086333,       # B41
    "cells_captured": 7696.90200129499,               # B42
    "barcode_reagent_lines": 24,                      # B54
    "total_reagent_inlets": 36,                       # B55
    "valves": 36,                                     # B56
    "capsules_per_split_chamber": 12500,              # B57
    "packed_volume_per_split_chamber_ul": 3.50770273496517,  # B58
    "wash_volume_per_exchange_ul": 420.92432819582,   # B59
    "total_wash_buffer_ml": 2.52554596917492,         # B60
    "barcode_combinations_with_sublibraries": 2048,   # B61
    "capsules_needed_for_target_cells": 25984.4805047992,    # B62
}


class SheetDefaults(unittest.TestCase):
    def setUp(self):
        self.r = sizing.compute()

    def test_headline_numbers(self):
        r = self.r
        self.assertAlmostEqual(r["capsule_volume_nl"], 0.18, delta=0.005)          # ~0.18 nL
        self.assertAlmostEqual(r["packed_volume_ul"], 28, delta=0.5)               # ~28 uL
        self.assertAlmostEqual(r["monolayer_area_cm2"], 4.9, places=9)             # 4.9 cm2
        self.assertAlmostEqual(r["capsules_per_hour"] / 1e6, 1.95, delta=0.005)    # ~1.95 M/h
        self.assertAlmostEqual(r["mean_cells_per_capsule"], 0.077, delta=0.0005)   # ~0.077

    def test_every_sheet_output_matches(self):
        for key, expected in SHEET.items():
            with self.subTest(key=key):
                self.assertTrue(math.isclose(self.r[key], expected, rel_tol=1e-12, abs_tol=1e-12),
                                f"{key}: {self.r[key]} != sheet {expected}")
        self.assertEqual(self.r["filter_check"], "OK")                             # B43


class Modes(unittest.TestCase):
    def test_cell_mode_matches_sheet_b62(self):
        r = sizing.compute({"target_cells": 2000}, mode="cells")
        self.assertAlmostEqual(r["capsule_count"], SHEET["capsules_needed_for_target_cells"], places=6)
        self.assertAlmostEqual(r["cells"], 2000, places=6)

    def test_cell_mode_sizes_everything_from_derived_count(self):
        r = sizing.compute({"target_cells": 100000}, mode="cells")
        same = sizing.compute({"capsule_count": r["capsule_count"]})
        for key in ("packed_volume_ul", "monolayer_area_cm2", "capsules_per_split_chamber", "hours_to_encapsulate"):
            self.assertAlmostEqual(r[key], same[key], places=9)
        self.assertGreater(r["capsule_count"], 1_000_000)   # most capsules are empty

    def test_mvp_preset(self):
        _, r = sizing.scenario("mvp")
        self.assertEqual(r["capsule_count"], 10000)
        self.assertEqual(r["barcode_combinations"], 512)
        self.assertAlmostEqual(r["monolayer_area_cm2"], 0.49, places=9)            # 7 x 7 mm


class Collisions(unittest.TestCase):
    def test_formula(self):
        r = sizing.compute({"capsule_count": 10000})
        n, c = r["cells"], r["barcode_combinations_with_sublibraries"]
        self.assertAlmostEqual(r["barcode_collision_fraction"], 1 - math.exp(-n / c), places=12)

    def test_warns_above_5_percent(self):
        many = sizing.compute({"target_cells": 100000}, mode="cells")
        self.assertTrue(any("barcode collisions" in w for w in many["warnings"]))
        few = sizing.compute({"target_cells": 50, "split_chambers_per_round": 96}, mode="cells")
        self.assertLess(few["barcode_collision_fraction"], 0.05)
        self.assertFalse(any("barcode collisions" in w for w in few["warnings"]))


if __name__ == "__main__":
    unittest.main()

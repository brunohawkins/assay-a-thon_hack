"""Checks operator/sizing.py against Szonja's sheet (contracts/sources/chip_geometry_requirements.xlsx).

    python -m unittest discover -s tests -v
"""

import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))

import sizing  # noqa: E402

# Barcoding numbers come from the contract, never restated here.
PARAMS = json.loads((ROOT / "contracts" / "parameters.json").read_text(encoding="utf-8"))["parameters"]
RANDOM_BASES = int(PARAMS["barcoding.random_bases"]["value"])
LOADING_MEAN = float(PARAMS["barcoding.loading_mean"]["value"])
LANES = int(PARAMS["barcoding.lanes"]["value"])
SUBLIBRARIES = int(PARAMS["sublibraries.count"]["value"])
BARCODE_POOL = 4 ** RANDOM_BASES

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
    # B31 (barcode_combinations) and B61 (barcode_combinations_with_sublibraries) are NOT
    # cached sheet cells any more and are deliberately absent from this oracle. The sheet
    # computed 8^3 = 512 split-and-pool combinations; both cells were then hand-patched to 3
    # and 3 x 4 = 12 on the belief that the per-lane oligos were the only barcodes left.
    # They are not the barcode space at all - a capsule's identity is a RANDOM barcode
    # amplified in its own droplet (4^barcoding.random_bases) - so pinning either number
    # here would re-encode that error. They are demultiplexing metadata and are asserted as
    # such, derived from the contract, in Barcodes.test_lanes_and_sublibraries_are_demux_metadata.
    "aqueous_flow_ul_h": 350,                         # B34
    "capsules_per_hour": 1948836.03785994,            # B35
    "generation_rate_hz": 541.343343849984,           # B36
    "cells_per_hour": 150000,                         # B37
    "mean_cells_per_capsule": 0.0769690200129499,     # B38
    "fraction_single_cell": 0.0712670424898798,       # B39
    "fraction_multi_of_occupied": 0.0379908729079278, # B40
    "hours_to_encapsulate": 0.0513126800086333,       # B41
    "cells_captured": 7696.90200129499,               # B42
    "barcode_reagent_lines": 0,                       # B54 (cells arrive barcoded)
    "total_reagent_inlets": 12,                       # B55
    "valves": 12,                                     # B56
    "capsules_per_sublibrary": 25000,                 # B57
    "packed_volume_per_sublibrary_ul": 7.015405469930339,    # B58
    "wash_volume_per_exchange_ul": 420.92432819582,   # B59
    "total_wash_buffer_ml": 1.68369731278328,         # B60
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
        for key in ("packed_volume_ul", "monolayer_area_cm2", "capsules_per_sublibrary", "hours_to_encapsulate"):
            self.assertAlmostEqual(r[key], same[key], places=9)
        self.assertGreater(r["capsule_count"], 1_000_000)   # most capsules are empty

    def test_mvp_preset(self):
        _, r = sizing.scenario("mvp")
        self.assertEqual(r["capsule_count"], 10000)
        self.assertEqual(r["barcode_combinations"], 3)
        self.assertAlmostEqual(r["monolayer_area_cm2"], 0.49, places=9)            # 7 x 7 mm


class Collisions(unittest.TestCase):
    """One cell per capsule, and each capsule's identity is a RANDOM barcode amplified
    clonally in its own oil droplet (protocols/sctip_spc_design.md). So the barcode space is
    the random pool, 4^barcoding.random_bases, and the two things that can go wrong are a
    repeated draw from that pool and Poisson loading of the barcode into the droplet.

    test_formula used to assert the collision fraction over lanes x sublibraries, which at a
    space of 12 came out at 1.0 - i.e. that every cell shares a barcode, which is where the
    "this is not single-cell" conclusion came from. Lanes and sublibraries are demux metadata.
    """

    def test_formula(self):
        r = sizing.compute({"capsule_count": 10000})
        self.assertEqual(r["barcode_pool"], BARCODE_POOL)
        self.assertAlmostEqual(r["barcode_collision_fraction"], 1 - math.exp(-r["cells"] / BARCODE_POOL), places=12)
        # The old space (12) would call essentially every cell a collision; the real pool
        # makes collisions negligible. Both are computed, so the difference is visible.
        self.assertAlmostEqual(1 - math.exp(-r["cells"] / r["demux_groups"]), 1.0, places=6)
        self.assertLess(r["barcode_collision_fraction"], 1e-4)

    def test_collision_rate_at_the_published_scale(self):
        # The source method reports fewer than 0.002% of 50,000 capsules sharing a barcode;
        # an independent anchor for the pool, not a restatement of our own arithmetic.
        r = sizing.compute({"target_cells": 50000}, mode="cells")
        self.assertAlmostEqual(r["cells"], 50000, places=6)
        self.assertLess(r["barcode_collision_fraction"], 0.00002)

    def test_poisson_loading_of_the_barcode_into_the_droplet(self):
        r = sizing.compute({"capsule_count": 10000})
        lam = LOADING_MEAN
        self.assertEqual(r["inputs"]["barcode_loading_mean"], lam)
        self.assertAlmostEqual(r["barcode_absent_fraction"], math.exp(-lam), places=12)
        self.assertAlmostEqual(r["barcode_multiple_fraction"], 1 - math.exp(-lam) - lam * math.exp(-lam), places=12)
        self.assertAlmostEqual(r["cells_with_a_barcode"], r["cells"] * (1 - math.exp(-lam)), places=9)
        # A droplet with no barcode is a lost cell; several are merged afterwards, so only
        # the absent fraction costs anything.
        self.assertLess(r["barcode_absent_fraction"], 0.1)

    def test_warns_above_5_percent(self):
        # COLLISION_WARN is unchanged; what changed is that a 4^16 pool never reaches it at
        # any sane cell count, so the warning PATH is driven by shortening the barcode. The
        # pool that just fails the threshold is derived from COLLISION_WARN itself:
        # 1 - exp(-n/pool) > WARN  <=>  pool < -n / ln(1 - WARN).
        cells = sizing.compute({"capsule_count": 10000})["cells"]
        colliding_bases = math.floor(math.log(-cells / math.log(1 - sizing.COLLISION_WARN), 4))
        many = sizing.compute({"capsule_count": 10000, "barcode_random_bases": colliding_bases})
        self.assertGreater(many["barcode_collision_fraction"], sizing.COLLISION_WARN)
        warning = next(w for w in many["warnings"] if "barcode collisions" in w)
        self.assertIn(f"{many['barcode_pool']:,}", warning)             # names the pool it ran out of
        self.assertIn(f"{colliding_bases} random bases", warning)
        self.assertNotIn("lane-level", warning)

        few = sizing.compute({"capsule_count": 10000, "barcode_random_bases": colliding_bases + 1})
        self.assertLess(few["barcode_collision_fraction"], sizing.COLLISION_WARN)
        self.assertFalse(any("barcode collisions" in w for w in few["warnings"]))

    def test_the_shipped_barcode_never_warns_at_the_runs_we_plan(self):
        for target in (2000, 100000):
            r = sizing.compute({"target_cells": target}, mode="cells")
            self.assertEqual([w for w in r["warnings"] if "barcode collisions" in w], [], msg=f"{target} cells")


class Barcodes(unittest.TestCase):
    def test_lanes_and_sublibraries_are_demux_metadata(self):
        r = sizing.compute({"capsule_count": 10000})
        self.assertEqual(r["demux_groups"], LANES * SUBLIBRARIES)
        self.assertEqual(r["barcode_combinations_with_sublibraries"], r["demux_groups"])
        self.assertEqual(r["barcode_combinations"], LANES)
        # Metadata is thousands of times smaller than the identity space: it cannot BE it.
        self.assertLess(r["demux_groups"], r["barcode_pool"])
        # The random barcode is made in the droplet, so it needs no reagent line of its own.
        self.assertEqual(r["barcode_reagent_lines"], 0)


class ParametersFile(unittest.TestCase):
    def test_parameters_json_matches_sizing_defaults(self):
        import json
        params = json.loads((ROOT / "contracts" / "parameters.json").read_text(encoding="utf-8"))["parameters"]
        for name, key in sizing.PARAMETER_KEYS.items():
            with self.subTest(input=name, parameter=key):
                self.assertIn(key, params)
                self.assertEqual(float(params[key]["value"]), float(sizing.DEFAULTS[name]))
        self.assertEqual(set(sizing.PARAMETER_KEYS), set(sizing.DEFAULTS))


if __name__ == "__main__":
    unittest.main()

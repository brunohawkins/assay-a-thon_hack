"""Each design rule in tools/check_chip_spec.py fires on a synthetic chip.

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
import schema_io  # noqa: E402

PARAMS = schema_io.load_json(ROOT / "contracts" / "parameters.json")
MATERIALS = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
FAB_TODO = {"laser_kerf_um": None, "min_embossed_feature_um": None}

GOOD = {
    "id": "test_chip", "placeholder": True, "units": "mm", "footprint": {"x": 20, "y": 12},
    "layers": [{"id": "base", "material": "pmma", "thickness_mm": 1}, {"id": "fluidic", "material": "pmma", "thickness_mm": 0.15}],
    "capsule": {"diameter_um": 70, "swell_factor": 2, "count": 10000},
    "chambers": [{"id": "trap_1", "type": "exchange", "center": [10, 6, 1.075], "size": [7, 7, 0.15], "height_um": 150,
                  "volume_ul": 7.35, "retention": {"type": "mesh", "opening_um": 25, "source": "bought"}}],
    "channels": [{"id": "ch_in", "from": "inlet_1", "to": "trap_1", "width_mm": 0.14, "depth_mm": 0.15},
                 {"id": "ch_waste", "from": "trap_1", "to": "waste_1", "width_mm": 0.05, "depth_mm": 0.05}],
    "ports": [{"id": "inlet_1", "type": "inlet", "position": [10, 6, 1.15], "role": "load", "capsule_passable": True},
              {"id": "waste_1", "type": "outlet", "position": [16, 6, 0], "role": "waste", "capsule_passable": False, "width_um": 50}],
}


def run(chip, protocols=(), fab=FAB_TODO):
    return ccs.check(chip, list(protocols), PARAMS, MATERIALS, fab)


def has(msgs, text):
    return any(text in m for m in msgs)


class Rules(unittest.TestCase):
    def test_good_chip_has_no_errors(self):
        f = run(GOOD)
        self.assertEqual(f.errors, [])
        self.assertTrue(has(f.todo, "fabrication TODO"))  # fabrication minimums unknown

    def test_retention_opening_at_0_4_d_is_an_error(self):
        chip = copy.deepcopy(GOOD)
        chip["chambers"][0]["retention"]["opening_um"] = 28  # exactly 0.4 x 70
        self.assertTrue(has(run(chip).errors, "opening 28 um >= 0.4"))

    def test_narrow_capsule_channel_is_an_error_but_waste_line_is_not(self):
        chip = copy.deepcopy(GOOD)
        chip["channels"][0]["width_mm"] = 0.1  # 100 um < 105 um
        errors = run(chip).errors
        self.assertTrue(has(errors, "channel 'ch_in'"))
        self.assertFalse(has(errors, "ch_waste"))  # behind the mesh: capsules never enter

    def test_narrow_capsule_outlet_is_an_error(self):
        chip = copy.deepcopy(GOOD)
        chip["ports"].append({"id": "release_1", "type": "outlet", "position": [3, 6, 1.15], "capsule_passable": True, "width_um": 90})
        self.assertTrue(has(run(chip).errors, "outlet 'release_1'"))

    def test_low_culture_chamber_is_an_error(self):
        chip = copy.deepcopy(GOOD)
        chip["thermal_zones"] = [{"id": "incubator", "chambers": ["trap_1"], "max_temp_c": 37, "purpose": "culture"}]
        chip["chambers"][0]["height_um"] = 120  # < 140 um swollen
        self.assertTrue(has(run(chip).errors, "culture happens there"))

    def test_culture_detected_from_protocol(self):
        chip = copy.deepcopy(GOOD)
        chip["chambers"][0]["height_um"] = 120
        proto = {"steps": [{"step": 1, "operation": "exchange", "target": "trap_1", "reagent": "culture_media"}]}
        self.assertTrue(has(run(chip, [(Path("p.json"), proto)]).errors, "culture happens there"))

    def test_cell_filter_not_smaller_than_nozzle_is_an_error(self):
        chip = copy.deepcopy(GOOD)
        chip["encapsulation"] = {"nozzle_um": 40}
        for gap, bad in ((45, True), (40, True), (30, False)):
            chip["cell_filter"] = {"gap_um": gap}
            self.assertEqual(has(run(chip).errors, f"cell_filter.gap_um {gap} >= encapsulation nozzle 40"), bad, gap)

    def test_hot_thermal_zone_is_an_error(self):
        chip = copy.deepcopy(GOOD)
        chip["thermal_zones"] = [{"id": "pcr", "chambers": ["trap_1"], "max_temp_c": 98, "purpose": "pcr"}]
        self.assertTrue(has(run(chip).errors, "above PMMA (acrylic)'s max working temperature"))

    def test_fabrication_minimum_only_warns(self):
        f = run(GOOD, fab={"laser_kerf_um": 100, "min_embossed_feature_um": 60})
        self.assertEqual(f.errors, [])
        self.assertTrue(has(f.warnings, "below the fabrication minimum"))

    def test_lanes_and_sublibraries_alone_do_not_raise_a_collision_warning(self):
        # They are DEMULTIPLEXING metadata (which lineage, which sublibrary), not the cell's
        # identity: that is a random barcode amplified in the capsule's own droplet, over a
        # 4^barcoding.random_bases pool (operator/sizing.py, protocols/sctip_spc_design.md).
        # This test used to assert the opposite - that 3 lanes x 1 sublibrary is "only 3
        # barcodes" - which is the arithmetic behind the claim that the assay is lane-level.
        chip = copy.deepcopy(GOOD)
        chip["barcoding"] = {"lanes": 3, "sublibraries": 1}
        f = run(chip)
        self.assertEqual(f.errors, [])
        self.assertFalse(has(f.warnings, "barcode collisions"))

    def test_a_chip_can_declare_the_random_barcode_so_the_collision_rule_can_fire(self):
        """GAP, asserted so it is not quietly dropped: the collision warning in
        check_chip_spec.py is now UNREACHABLE from any chip. The chip_spec schema's
        barcoding block is additionalProperties: false with only rounds, chambers_per_round,
        lanes and sublibraries, and check() forwards only lanes and sublibraries to
        sizing.compute(), so nothing a chip can say changes the random-barcode pool the rule
        is computed over. Closing it needs a schema field and one more forwarded input."""
        chip = copy.deepcopy(GOOD)
        chip["barcoding"] = {"lanes": 3, "sublibraries": 4, "random_bases": 4}   # 4^4 = 256
        self.assertEqual(schema_io.schema_errors("chip_spec", chip), [])
        self.assertTrue(has(run(chip).warnings, "barcode collisions"))

    def test_reagent_compatibility_todo_and_incompatible(self):
        proto = {"steps": [{"step": 1, "operation": "exchange", "target": "trap_1", "reagent": "ethanol"}]}
        self.assertTrue(has(run(GOOD, [(Path("p.json"), proto)]).todo, "compatibility TODO: PMMA (acrylic) with ethanol"))
        mats = copy.deepcopy(MATERIALS)
        mats["pmma"]["chemical_compatibility"]["ethanol"] = "incompatible"
        f = ccs.check(GOOD, [(Path("p.json"), proto)], PARAMS, mats, FAB_TODO)
        self.assertTrue(has(f.warnings, "incompatible with ethanol"))


if __name__ == "__main__":
    unittest.main()

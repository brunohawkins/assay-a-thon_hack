"""Every chip spec on disk against the build envelope and the CAD block library (contracts/blocks.json).

Chips are discovered the same way tools/check_chip_spec.py does (chip/*.json, cad/*_spec.json), so a new chip is
covered automatically.

    python -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "tools"))

import check_chip_spec as ccs  # noqa: E402
import schema_io  # noqa: E402

BLOCKS = schema_io.load_json(ROOT / "contracts" / "blocks.json")
ENV = BLOCKS["envelope"]
FLOORS = BLOCKS["fabrication_floors"]
LIBRARY = set(BLOCKS["basic"]) | set(BLOCKS["advanced"])
SPECS = {p: schema_io.load_json(p) for p in ccs.spec_paths([])}
TOL = 1e-9


class PerChip(unittest.TestCase):
    """Runs `check_chip(chip)` for every spec on disk, one subtest per chip, so one bad chip names itself."""

    def for_every_chip(self, check_chip):
        self.assertTrue(SPECS, "no chip specs found")
        for path, chip in SPECS.items():
            with self.subTest(chip=str(path.relative_to(ROOT))):
                check_chip(chip)


class Library(unittest.TestCase):
    def test_blocks_json_parses_and_has_the_expected_sections(self):
        for key in ("envelope", "fabrication_floors", "basic", "advanced"):
            self.assertIsInstance(BLOCKS[key], dict, key)
        self.assertTrue(BLOCKS["basic"] and BLOCKS["advanced"])

    def test_no_block_id_is_in_both_basic_and_advanced(self):
        self.assertEqual(set(BLOCKS["basic"]) & set(BLOCKS["advanced"]), set())

    def test_the_contract_still_states_the_project_constraints(self):
        # Pins blocks.json to the agreed envelope so the data-driven tests below cannot be loosened by editing it.
        self.assertEqual((ENV["max_x_mm"], ENV["max_y_mm"], ENV["max_depth_mm"], ENV["layers"]), (30, 20, 0.3, 1))
        self.assertEqual(FLOORS["max_ports_per_chip"], 6)
        self.assertEqual(FLOORS["min_open_channel_mm"], 0.2)


class Envelope(PerChip):
    def test_every_chip_fits_the_build_area(self):
        def one(chip):
            fp = chip.get("footprint") or {}
            self.assertIn("x", fp)
            self.assertIn("y", fp)
            self.assertLessEqual(fp["x"], ENV["max_x_mm"] + TOL, "footprint x")
            self.assertLessEqual(fp["y"], ENV["max_y_mm"] + TOL, "footprint y")
        self.for_every_chip(one)

    def test_nothing_is_deeper_than_the_depth_limit(self):
        def one(chip):
            for c in chip.get("chambers", []):
                self.assertLessEqual(c["size"][2], ENV["max_depth_mm"] + TOL, f"chamber {c['id']}")
            for ch in chip.get("channels", []):
                self.assertLessEqual(ch["depth_mm"], ENV["max_depth_mm"] + TOL, f"channel {ch['id']}")
        self.for_every_chip(one)

    def test_no_chip_has_more_ports_than_the_limit(self):
        def one(chip):
            self.assertLessEqual(len(chip.get("ports", [])), FLOORS["max_ports_per_chip"])
        self.for_every_chip(one)

    def test_every_channel_is_at_least_the_open_channel_floor(self):
        def one(chip):
            for ch in chip.get("channels", []):
                self.assertGreaterEqual(ch["width_mm"], FLOORS["min_open_channel_mm"] - TOL, f"channel {ch['id']}")
        self.for_every_chip(one)

    def test_no_chip_has_a_second_fluidic_layer(self):
        def one(chip):
            self.assertEqual(chip.get("valves", []), [], "on-chip valves need a second layer")
        self.for_every_chip(one)


class Blocks(PerChip):
    def test_every_declared_block_exists_in_the_library(self):
        def one(chip):
            for group in ("chambers", "channels"):
                for item in chip.get(group, []):
                    if "block" in item:
                        self.assertIn(item["block"], LIBRARY, f"{group[:-1]} {item['id']}")
        self.for_every_chip(one)

    def test_the_checker_agrees(self):
        params = schema_io.load_json(ROOT / "contracts" / "parameters.json")
        materials = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
        def one(chip):
            findings = ccs.check(chip, [], params, materials, {"laser_kerf_um": None, "min_embossed_feature_um": None},
                                 None, BLOCKS)
            envelope_errors = [e for e in findings.errors if "envelope" in e or "max depth" in e or "ports >" in e
                               or "minimum open channel" in e or "block library" in e]
            self.assertEqual(envelope_errors, [])
        self.for_every_chip(one)


if __name__ == "__main__":
    unittest.main()

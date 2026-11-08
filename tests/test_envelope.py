"""Every chip spec on disk against its build envelope and the CAD block library (contracts/blocks.json).

Chips are discovered the same way tools/check_chip_spec.py does (chip/*.json, cad/*_spec.json), so a new chip is
covered automatically.

TWO SCALES, ONE SET OF PROCESS FLOORS. A chip's optional top-level `scale` ("demo", the
schema default, or "target") says which envelope it is drawn to: `envelope` (30 x 20 mm, what
we can print today) or `target_envelope` (the ANSI/SLAS 127.76 x 85.48 mm plate, with the
290 x 190 mm ceiling above it). ONLY the x/y limits and the port COUNT move with scale -
blocks.json says in two places that max_ports_per_chip is what 30 x 20 mm fits at the 5 mm
pitch, a demo-envelope limit and not a process floor. Everything a process imposes binds at
both scales and is asserted for every chip regardless of scale: 0.3 mm max depth, one
fluidic layer at one etch depth, the 0.2 mm open-channel floor and the known block library
here, and min_wall_mm, min_port_mm, min_port_pitch_mm and one etch depth in
tests/test_design_rules.py.

A demo chip is checked exactly as strictly as before, and class Loophole stops `scale:
target` becoming a way out of the demo envelope.

    python -m unittest discover -s tests -v
"""

import math
import re
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
TARGET = BLOCKS["target_envelope"]
FLOORS = BLOCKS["fabrication_floors"]
LIBRARY = set(BLOCKS["basic"]) | set(BLOCKS["advanced"])
SPECS = {p: schema_io.load_json(p) for p in ccs.spec_paths([])}
PIPELINE_PATH, PIPELINE = schema_io.chosen_pipeline()
SCALE_FIELD = schema_io.load_json(ROOT / "contracts" / "schemas" / "chip_spec.schema.json")["properties"].get("scale")
TOL = 1e-9
SAYS_TARGET_SCALE = re.compile(r"target[ -]scale", re.I)


# The scale -> envelope mapping is NOT re-derived here: tools/check_chip_spec.py owns it and
# this file calls the same three functions, so the tool and the suite cannot disagree about
# which envelope a chip is held to. What this file still pins independently are the CONTRACT
# NUMBERS those functions must return (class Library), which is what keeps calling them from
# being circular.
scale_of = ccs.scale_of


def envelope_for(chip):
    """({max_x_mm, max_y_mm, max_depth_mm, layers}, label) for this chip's scale."""
    return ccs.envelope_for(chip, BLOCKS)


def max_ports_for(chip):
    """(limit, why) on this chip's port count: 6 at demo scale, the pitch grid at target."""
    env, _ = envelope_for(chip)
    return ccs.max_ports_for(chip, BLOCKS, env)


def etch_depths_mm(chip):
    return ({c["size"][2] for c in chip.get("chambers", [])}
            | {c["depth_mm"] for c in chip.get("channels", [])})


class PerChip(unittest.TestCase):
    """Runs `check_chip(chip)` for every spec on disk, one subtest per chip, so one bad chip names itself."""

    def for_every_chip(self, check_chip, scale=None):
        self.assertTrue(SPECS, "no chip specs found")
        seen = 0
        for path, chip in SPECS.items():
            if scale is not None and scale_of(chip) != scale:
                continue
            seen += 1
            with self.subTest(chip=str(path.relative_to(ROOT)), scale=scale_of(chip)):
                check_chip(chip)
        if scale == "demo":
            self.assertTrue(seen, "no demo-scale chip found: the demo checks are checking nothing")


class Library(unittest.TestCase):
    def test_blocks_json_parses_and_has_the_expected_sections(self):
        for key in ("envelope", "fabrication_floors", "basic", "advanced", "target_envelope"):
            self.assertIsInstance(BLOCKS[key], dict, key)
        self.assertTrue(BLOCKS["basic"] and BLOCKS["advanced"])

    def test_no_block_id_is_in_both_basic_and_advanced(self):
        self.assertEqual(set(BLOCKS["basic"]) & set(BLOCKS["advanced"]), set())

    def test_the_contract_still_states_the_project_constraints(self):
        # Pins blocks.json to the agreed envelope so the data-driven tests below cannot be loosened by editing it.
        self.assertEqual((ENV["max_x_mm"], ENV["max_y_mm"], ENV["max_depth_mm"], ENV["layers"]), (30, 20, 0.3, 1))
        self.assertEqual(FLOORS["max_ports_per_chip"], 6)
        self.assertEqual(FLOORS["min_open_channel_mm"], 0.2)

    def test_the_contract_still_states_the_target_scale(self):
        self.assertEqual((TARGET["x_mm"], TARGET["y_mm"]), (127.76, 85.48))          # ANSI/SLAS 1-2004
        self.assertEqual((TARGET["max_allowed_x_mm"], TARGET["max_allowed_y_mm"]), (290, 190))
        self.assertLess(TARGET["x_mm"], TARGET["max_allowed_x_mm"])
        self.assertLess(TARGET["y_mm"], TARGET["max_allowed_y_mm"])

    def test_the_process_does_not_change_with_scale(self):
        # Only x and y grow. If either of these ever differs, "target scale" has become a
        # second process and every floor below would need re-deriving.
        self.assertEqual(TARGET["depth_mm"], ENV["max_depth_mm"])
        self.assertEqual(TARGET["layers"], ENV["layers"])
        self.assertEqual(ENV["layers"], 1)

    def test_the_schema_offers_only_the_two_scales(self):
        self.assertIsNotNone(SCALE_FIELD, "chip_spec.schema.json has no `scale` field yet")
        self.assertEqual(set(SCALE_FIELD["enum"]), {"demo", "target"})
        self.assertEqual(SCALE_FIELD["default"], "demo")

    def test_the_checkers_envelope_and_port_rule_return_the_contract_numbers(self):
        """The one place this file checks the SELECTION rather than using it. Without this,
        calling check_chip_spec's helpers would make the assertions below agree with the tool
        by construction; with it, the tool has to agree with the pinned contract values."""
        demo, demo_label = ccs.envelope_for({}, BLOCKS)                        # no scale -> demo
        self.assertEqual(ccs.scale_of({}), "demo")
        self.assertEqual((demo["max_x_mm"], demo["max_y_mm"], demo["max_depth_mm"], demo["layers"]),
                         (ENV["max_x_mm"], ENV["max_y_mm"], ENV["max_depth_mm"], ENV["layers"]))
        self.assertIn("demo", demo_label.lower())
        self.assertEqual(ccs.max_ports_for({}, BLOCKS, demo)[0], FLOORS["max_ports_per_chip"])

        target, target_label = ccs.envelope_for({"scale": "target"}, BLOCKS)
        self.assertEqual((target["max_x_mm"], target["max_y_mm"], target["max_depth_mm"], target["layers"]),
                         (TARGET["x_mm"], TARGET["y_mm"], TARGET["depth_mm"], TARGET["layers"]))
        self.assertIn("target", target_label.lower())
        # The port cap lifts, and what replaces it is the pitch grid the plate area holds.
        pitch = FLOORS["min_port_pitch_mm"]
        self.assertEqual(ccs.max_ports_for({"scale": "target"}, BLOCKS, target)[0],
                         int(TARGET["x_mm"] // pitch) * int(TARGET["y_mm"] // pitch))
        self.assertGreater(ccs.max_ports_for({"scale": "target"}, BLOCKS, target)[0], FLOORS["max_ports_per_chip"])

    def test_every_chip_declares_a_scale_the_schema_allows(self):
        for path, chip in SPECS.items():
            with self.subTest(chip=str(path.relative_to(ROOT))):
                self.assertIn(scale_of(chip), set(SCALE_FIELD["enum"]))
                self.assertEqual(schema_io.schema_errors("chip_spec", chip), [])


class Envelope(PerChip):
    def test_every_demo_chip_fits_the_build_area(self):
        def one(chip):
            fp = chip.get("footprint") or {}
            self.assertIn("x", fp)
            self.assertIn("y", fp)
            self.assertLessEqual(fp["x"], ENV["max_x_mm"] + TOL, "footprint x")
            self.assertLessEqual(fp["y"], ENV["max_y_mm"] + TOL, "footprint y")
        self.for_every_chip(one, scale="demo")

    def test_every_target_chip_is_inside_the_agreed_ceiling(self):
        # 290 x 190 mm: the absolute maximum, set so both prototyping routes stay standard.
        # It is NOT the envelope any chip is drawn to, so it is read straight from the contract.
        def one(chip):
            fp = chip.get("footprint") or {}
            self.assertIn("x", fp)
            self.assertIn("y", fp)
            self.assertLessEqual(fp["x"], TARGET["max_allowed_x_mm"] + TOL, "footprint x over the ceiling")
            self.assertLessEqual(fp["y"], TARGET["max_allowed_y_mm"] + TOL, "footprint y over the ceiling")
        self.for_every_chip(one, scale="target")

    def test_every_chip_fits_the_envelope_for_its_scale(self):
        # Demo chips: exactly the old assertion, 30 x 20 mm. Target chips: the ANSI/SLAS
        # microplate footprint, because blocks.json says nothing in the design needs the
        # ceiling above it. The envelope comes from check_chip_spec.envelope_for either way.
        def one(chip):
            env, label = envelope_for(chip)
            fp = chip.get("footprint") or {}
            self.assertIn("x", fp)
            self.assertIn("y", fp)
            self.assertLessEqual(fp["x"], env["max_x_mm"] + TOL, f"footprint x over the {label}")
            self.assertLessEqual(fp["y"], env["max_y_mm"] + TOL, f"footprint y over the {label}")
        self.for_every_chip(one)

    def test_nothing_is_deeper_than_the_depth_limit(self):
        # A process floor: the same 0.3 mm at either scale.
        def one(chip):
            depth = envelope_for(chip)[0]["max_depth_mm"]
            for c in chip.get("chambers", []):
                self.assertLessEqual(c["size"][2], depth + TOL, f"chamber {c['id']}")
            for ch in chip.get("channels", []):
                self.assertLessEqual(ch["depth_mm"], depth + TOL, f"channel {ch['id']}")
        self.for_every_chip(one)

    def test_no_demo_chip_has_more_ports_than_the_limit(self):
        def one(chip):
            self.assertLessEqual(len(chip.get("ports", [])), FLOORS["max_ports_per_chip"])
        self.for_every_chip(one, scale="demo")

    def test_no_chip_has_more_ports_than_its_scale_allows(self):
        """The 6-port cap lifts at target scale (blocks.json says in two places that it is a
        demo-envelope limit, not a process floor), so the limit becomes how many positions the
        board holds at min_port_pitch_mm - which max_ports_for computes. The PITCH itself is a
        connector floor and binds at both scales."""
        pitch = FLOORS["min_port_pitch_mm"]

        def one(chip):
            ports = chip.get("ports", [])
            limit, why = max_ports_for(chip)
            if limit is not None:
                self.assertLessEqual(len(ports), limit, f"{len(ports)} ports > {limit:g}: {why}")
            placed = [p for p in ports if len(p.get("position") or []) >= 2]
            for i, a in enumerate(placed):
                for b in placed[i + 1:]:
                    dist = math.dist(a["position"][:2], b["position"][:2])
                    self.assertGreaterEqual(dist, pitch - TOL,
                                            f"ports '{a['id']}' and '{b['id']}' are {dist:.2f} mm apart, under the "
                                            f"{pitch:g} mm pitch")
        self.for_every_chip(one)

    def test_a_target_chip_needs_the_ports_it_claimed_the_scale_for(self):
        def one(chip):
            self.assertGreater(len(chip.get("ports", [])), FLOORS["max_ports_per_chip"],
                               "a target-scale chip that still fits the 6-port demo limit does not need target scale")
        self.for_every_chip(one, scale="target")

    def test_every_channel_is_at_least_the_open_channel_floor(self):
        # In-plane process floor: unchanged by scale.
        def one(chip):
            for ch in chip.get("channels", []):
                self.assertGreaterEqual(ch["width_mm"], FLOORS["min_open_channel_mm"] - TOL, f"channel {ch['id']}")
        self.for_every_chip(one)

    def test_no_chip_has_a_second_fluidic_layer(self):
        def one(chip):
            self.assertEqual(chip.get("valves", []), [], "on-chip valves need a second layer")
        self.for_every_chip(one)

    def test_every_chip_is_one_fluidic_layer_at_one_etch_depth(self):
        # The layer count in both envelopes is 1, so a target board is not allowed a second
        # fluidic layer to pay for its area.
        def one(chip):
            depths = etch_depths_mm(chip)
            self.assertEqual(len(depths), 1, f"{len(depths)} etch depths: {sorted(depths)}")
            fluidic = [la for la in chip.get("layers", []) if abs(la["thickness_mm"] - max(depths)) < 1e-9]
            self.assertEqual(len(fluidic), envelope_for(chip)[0]["layers"],
                             f"layers matching the {max(depths):g} mm etch depth: "
                             f"{[la['id'] for la in fluidic]}")
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


class Loophole(unittest.TestCase):
    """`scale: target` must not become a way out of the demo envelope.

    The demo envelope is what we can actually print this weekend, so a chip that declares
    itself target scale is making a design claim about a board we cannot build yet. These two
    tests make that claim explicit and keep it out of the demo build.
    """

    def target_chips(self):
        return {path: chip for path, chip in SPECS.items() if scale_of(chip) == "target"}

    def test_a_target_chip_says_target_scale_in_its_description(self):
        for path, chip in self.target_chips().items():
            with self.subTest(chip=str(path.relative_to(ROOT))):
                self.assertTrue(SAYS_TARGET_SCALE.search(chip.get("description") or ""),
                                "a chip drawn to an envelope we cannot print yet has to say so where people read it, "
                                "not only in a field a tool reads")

    def test_no_chip_in_the_demo_pipeline_is_target_scale_unless_the_pipeline_says_so(self):
        text = PIPELINE_PATH.read_text(encoding="utf-8")
        declared = SAYS_TARGET_SCALE.search(text)
        for m in PIPELINE["modules"]:
            chip_path = schema_io.pipeline_chip(m)[0]
            if not chip_path:
                continue
            chip = schema_io.load_json(ROOT / chip_path)
            with self.subTest(module=m["module"], chip=chip_path):
                if scale_of(chip) != "target":
                    continue
                self.assertTrue(declared,
                                f"{chip_path} is target scale but {PIPELINE_PATH.name} is the DEMO build and never "
                                f"mentions target scale: either the pipeline says which modules are target scale and "
                                f"why, or the demo pipeline names a board we cannot print")
                self.assertIn(chip_path, text, f"{PIPELINE_PATH.name} must name {chip_path} where it explains the scale")


if __name__ == "__main__":
    unittest.main()

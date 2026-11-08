"""S5 group and S8 protocol: the grouping rules, the physics, and the audit rules.

    python -m unittest tests.test_assay2chip_protocol -v
    python -m unittest discover -s tests

Runs offline: no network, no model call, no clock. The chip under test is an existing
hand-made one (chip/module3_v1_translated.json) with hand-made unit ops beside it in
assay2chip/protocolgen/fixtures/, because the chips S6 and S7 will build do not exist yet.

The last class is the comparison that matters: the same audit that S8's output has to pass
is run against the hand-written protocols/module3_v1.json, and it finds real defects there.
"""

import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "operator")):
    if path not in sys.path:
        sys.path.insert(0, path)

import planner  # noqa: E402
import schema_io  # noqa: E402

from assay2chip.lib import contracts  # noqa: E402
from assay2chip.protocolgen import audit, build, exchange, flows, geometry, reagents  # noqa: E402
from assay2chip.stages import s5_group, s8_protocol  # noqa: E402

FIXTURES = ROOT / "assay2chip" / "protocolgen" / "fixtures"
CHIP_PATH = ROOT / "chip" / "module3_v1_translated.json"
UNITOPS_PATH = FIXTURES / "module3_v1_translated_unitops.json"
PARTITION_PATH = FIXTURES / "module3_v1_translated_partition.json"

CHIP = json.loads(CHIP_PATH.read_text())
UNITOPS = json.loads(UNITOPS_PATH.read_text())
PARTITION = json.loads(PARTITION_PATH.read_text())
TRAP = "main_trap"


def build_module3():
    return s8_protocol.build_protocol(CHIP, UNITOPS, None, None, "module3_dev")


def on_chip_ops(unitops=None):
    return [op for op in (unitops or UNITOPS)["ops"]
            if op["placement"]["where"] == "on_chip"]


def mixer_chip(channel_width_mm=None, zone=True):
    """A two-chamber chip: a trap and a folded mixer, for the cases module 3 has none of."""
    mixer = {"id": "mix_1", "type": "mixer", "center": [20, 10, 1.15], "size": [8, 6, 0.3],
             "volume_ul": 10.0}
    if channel_width_mm:
        mixer["mixer"] = {"kind": "serpentine", "channel_width_mm": channel_width_mm,
                          "turns": 6, "length_mm": 40}
    spec = {
        "id": "two_place_chip", "units": "mm", "placeholder": True,
        "footprint": {"x": 30, "y": 20},
        "layers": [{"id": "base", "material": "coc", "thickness_mm": 1.0},
                   {"id": "fluidic", "material": "coc", "thickness_mm": 0.3},
                   {"id": "lid", "material": "coc", "thickness_mm": 1.0}],
        "chambers": [
            {"id": "cult_1", "type": "culture", "center": [8, 10, 1.15], "size": [6, 6, 0.3],
             "volume_ul": 10.8},
            mixer,
        ],
        "channels": [
            {"id": "ch_in", "from": "in_buffer", "to": "cult_1", "width_mm": 0.2,
             "depth_mm": 0.3, "path": [[1, 10], [5, 10]]},
            {"id": "ch_mid", "from": "cult_1", "to": "mix_1", "width_mm": 0.2, "depth_mm": 0.3,
             "path": [[11, 10], [16, 10]]},
            {"id": "ch_out", "from": "mix_1", "to": "out_cells", "width_mm": 0.2,
             "depth_mm": 0.3, "path": [[24, 10], [29, 10]]},
            {"id": "ch_reagent", "from": "in_reagent", "to": "mix_1", "width_mm": 0.2,
             "depth_mm": 0.3, "path": [[20, 1], [20, 7]]},
        ],
        "ports": [
            {"id": "in_buffer", "type": "inlet", "position": [1, 10, 2.3], "role": "reagent"},
            {"id": "in_reagent", "type": "inlet", "position": [20, 1, 2.3], "role": "reagent"},
            {"id": "out_cells", "type": "outlet", "position": [29, 10, 2.3], "role": "waste"},
        ],
    }
    if zone:
        spec["thermal_zones"] = [{"id": "warm", "chambers": ["cult_1", "mix_1"],
                                  "min_temp_c": 4, "max_temp_c": 40, "purpose": "culture"}]
    return spec


def op(number, kind, **kw):
    out = {"op": number, "from_steps": [number], "kind": kind,
           "fluid": {"volume_ul": kw.pop("volume_ul", None), "viscosity_class": "aqueous"},
           "particles": {"kind": kw.pop("particles", "none")},
           "duration_s": kw.pop("duration_s", None), "temp_c": kw.pop("temp_c", None),
           "shear_class": kw.pop("shear_class", "none"),
           "placement": {"where": "on_chip", "part": kw.pop("part", "culture_chamber"),
                         "why": "test"}}
    if out["particles"]["kind"] != "none":
        out["particles"]["diameter_um"] = kw.pop("diameter_um", 70)
    out.update(kw)
    return out


# --------------------------------------------------------------------------------------


class CrossSections(unittest.TestCase):
    """The width across the flow is never a bounding box."""

    def test_trap_width_is_the_side_across_the_flow(self):
        chip = geometry.Chip(CHIP)
        section = chip.cross_section(TRAP)
        self.assertEqual(chip.flow_axis(TRAP), 0)            # fed left, drained right
        self.assertAlmostEqual(section.width_mm, 11.32)      # the y side
        self.assertAlmostEqual(section.height_mm, 0.3)
        self.assertIn("y side", section.why)

    def test_a_folded_part_uses_its_channel_and_refuses_its_bounding_box(self):
        narrow = geometry.Chip(mixer_chip(channel_width_mm=0.5)).cross_section("mix_1")
        self.assertAlmostEqual(narrow.width_mm, 0.5)
        with self.assertRaisesRegex(geometry.GeometryError, "43x"):
            geometry.Chip(mixer_chip()).cross_section("mix_1")

    def test_the_bounding_box_would_be_43x_wrong_on_a_library_serpentine(self):
        # chip/components/catalogue.json serpentine_channel is 25 x 60 mm; its channel is
        # not. Shear goes as 1/w, so the bounding box under-reads it by the width ratio.
        mu = contracts.viscosity_for(37).pa_s
        real = flows.wall_shear_dyn_cm2(1.0, 0.5, 0.3, mu)
        box = flows.wall_shear_dyn_cm2(1.0, 25.0, 0.3, mu)
        self.assertAlmostEqual(real / box, 50.0, places=6)
        self.assertGreater(real / box, 40)

    def test_volume_comes_from_the_spec_not_from_the_box(self):
        chip = geometry.Chip(CHIP)
        self.assertEqual(chip.volume_ul(TRAP), 38.443)
        with self.assertRaisesRegex(geometry.GeometryError, "volume_ul"):
            spec = json.loads(CHIP_PATH.read_text())
            spec["chambers"][0].pop("volume_ul")
            geometry.Chip(spec).volume_ul(TRAP)


class Flows(unittest.TestCase):
    """tau = 6 mu Q / (w h^2), and the three scoped ceilings."""

    def test_shear_and_its_inverse_agree(self):
        mu = contracts.viscosity_for(37).pa_s
        q = flows.flow_at_shear(0.01, 11.32, 0.3, mu)
        self.assertAlmostEqual(flows.wall_shear_dyn_cm2(q, 11.32, 0.3, mu), 0.01, places=9)
        # hand check: Q = (tau/10) w h^2 / (6 mu), in SI, x60x1e9 for uL/min
        hand = (0.01 / 10) * 11.32e-3 * (0.3e-3 ** 2) / (6 * mu) * 60 * 1e9
        self.assertAlmostEqual(q, hand, places=6)

    def test_viscosity_is_the_37c_value_not_water(self):
        self.assertEqual(contracts.viscosity_for(37).key, "fluid.viscosity_37c_pa_s")
        self.assertEqual(contracts.viscosity_for(None).key, "fluid.viscosity_37c_pa_s")
        self.assertEqual(contracts.viscosity_for(20).key, "fluid.viscosity_pa_s")

    def test_the_prelysis_ceiling_is_thirty_times_the_culture_one(self):
        culture = contracts.shear_ceiling_for("adherent_cells").ceiling_dyn_cm2
        rinse = contracts.shear_ceiling_for("prelysis_rinse").ceiling_dyn_cm2
        mu = contracts.viscosity_for(37).pa_s
        self.assertAlmostEqual(
            flows.flow_at_shear(rinse, 6.0, 0.3, mu) / flows.flow_at_shear(culture, 6.0, 0.3, mu),
            rinse / culture)

    def test_adherent_cells_are_held_to_the_culture_ceiling(self):
        chip = geometry.Chip(mixer_chip(channel_width_mm=0.5))
        flow = flows.derive(chip, "cult_1", "adherent_cells", "in_buffer", "out_cells",
                            temp_c=37)
        self.assertEqual(flow.bound_by, "wall_shear")
        ceiling = contracts.shear_ceiling_for("adherent_cells").ceiling_dyn_cm2
        self.assertLessEqual(flow.shear_dyn_cm2, ceiling)
        self.assertAlmostEqual(flow.fraction, flows.SHEAR_HEADROOM)
        self.assertIn("shear.culture_ceiling_dyn_cm2", flow.ceiling)

    def test_capsule_handling_is_bounded_by_pressure_and_not_by_shear(self):
        chip = geometry.Chip(CHIP)
        flow = flows.derive(chip, TRAP, "capsules", "reagent_in", "waste",
                            temp_c=37, particles="capsules")
        self.assertEqual(flow.bound_by, "escape_pressure")
        self.assertIsNone(contracts.shear_ceiling_for("capsules").ceiling_dyn_cm2)
        self.assertLessEqual(flow.pressure_kpa, contracts.max_working_pressure_kpa() + 1e-9)
        # ... and it is far above what the culture ceiling would have allowed, which is the
        # whole point of scoping them separately.
        culture = flows.derive(chip, TRAP, "adherent_cells", "reagent_in", "waste", temp_c=37)
        self.assertGreater(flow.flow_ul_min, 4 * culture.flow_ul_min)

    def test_a_downstream_chamber_tightens_the_flow(self):
        chip = geometry.Chip(mixer_chip(channel_width_mm=0.5))
        alone = flows.derive(chip, "cult_1", "none", "in_buffer", "out_cells", temp_c=37)
        with_mixer = flows.derive(chip, "cult_1", "none", "in_buffer", "out_cells", temp_c=37,
                                  also_bounded=[("mix_1", "adherent_cells")])
        self.assertLess(with_mixer.flow_ul_min, alone.flow_ul_min)
        self.assertEqual(with_mixer.bound_by, "wall_shear")
        self.assertIn("mix_1", with_mixer.ceiling)

    def test_a_flow_is_rounded_down_onto_a_settable_grid_and_never_to_zero(self):
        self.assertEqual(flows.quantise(67.789), 67.7)
        self.assertEqual(flows.quantise(0.0456), 0.04)
        self.assertGreater(flows.quantise(1e-7), 0)
        for value in (0.37, 5.0, 1234.5):
            self.assertLessEqual(flows.quantise(value), value)

    def test_duration_is_volume_times_washes_over_flow_plus_the_margin(self):
        self.assertEqual(flows.duration_s(10.0, 5, 10.0, 1.25), math.ceil(5 * 60 * 1.25))
        self.assertEqual(flows.duration_s(10.0, 1, 10.0, 1.25, repeats=3), math.ceil(3 * 60 * 1.25))


class ExchangeKind(unittest.TestCase):
    """partial | sweep | fill, and absence meaning sweep."""

    def test_a_sweep_needs_minus_log_of_one_minus_the_target(self):
        target = contracts.param("exchange.target_fraction")
        self.assertAlmostEqual(exchange.sweep_chamber_volumes(), -math.log(1 - target))
        self.assertAlmostEqual(exchange.sweep_chamber_volumes(0.99), 4.60517, places=4)
        self.assertEqual(exchange.sweep_chamber_volumes(0.5), 1.0)   # the one-volume floor
        with self.assertRaises(exchange.ExchangeError):
            exchange.sweep_chamber_volumes(1.0)

    def test_a_sweep_is_sized_to_clear_the_chamber(self):
        sizing = exchange.size(exchange.SWEEP, 38.443)
        self.assertEqual((sizing.volume_ul, sizing.washes), (38.443, 5))
        self.assertGreaterEqual(sizing.total_ul,
                                exchange.sweep_chamber_volumes() * 38.443 - 1e-9)
        self.assertGreater(sizing.fraction, 0.99)

    def test_a_fill_is_one_chamber_volume_and_nothing_else(self):
        sizing = exchange.size(exchange.FILL, 38.443)
        self.assertEqual((sizing.total_ul, sizing.washes), (38.443, 1))
        self.assertAlmostEqual(sizing.chamber_volumes, 1.0)

    def test_the_papers_own_volume_sizes_nothing(self):
        # "Bench volumes do not transfer": a tube volume cannot reach any of these.
        for kind, extra in ((exchange.FILL, {}), (exchange.SWEEP, {}),
                            (exchange.PARTIAL, {"ratio": 1.0})):
            with self.subTest(kind):
                sizing = exchange.size(kind, 38.443, **extra)
                self.assertLessEqual(sizing.volume_ul, 38.443 + 1e-9)
        with self.assertRaises(TypeError):
            exchange.size(exchange.SWEEP, 38.443, requested_ul=500)

    def test_a_partial_comes_from_the_papers_ratio_and_states_the_fraction(self):
        sizing = exchange.size(exchange.PARTIAL, 40.0, ratio=1.0)
        self.assertEqual(sizing.washes, 1)
        self.assertAlmostEqual(sizing.chamber_volumes, math.log(2))
        self.assertAlmostEqual(sizing.volume_ul, round(40.0 * math.log(2), 3))
        self.assertAlmostEqual(sizing.fraction, 0.5)          # 1:1 reagent to sample
        self.assertIn("%", sizing.note)
        self.assertAlmostEqual(exchange.chamber_volumes_for_ratio(1 / 9), math.log(10 / 9))
        self.assertEqual(exchange.chamber_volumes_for_ratio(50), 1.0)   # capped at a volume
        with self.assertRaisesRegex(exchange.ExchangeError, "ratio"):
            exchange.size(exchange.PARTIAL, 40.0)

    def test_a_missing_ratio_is_an_open_question_not_a_number(self):
        question = exchange.ratio_question("lyse", 38.443)
        self.assertIn("OPEN QUESTION", question)
        self.assertIn("describes a tube", question)
        self.assertEqual(exchange.classify("lyse", False, None, 38.443)[0], exchange.SWEEP)

    def test_classification_is_by_what_the_step_is_for(self):
        empty = exchange.classify("wash", True, None, 40.0)[0]
        wash = exchange.classify("wash", False, None, 40.0)[0]
        lift = exchange.classify("wash", False, 1.0, 40.0, direction="backflush")[0]
        ratioed = exchange.classify("incubate", False, 1.0, 40.0)[0]
        displaced = exchange.classify("incubate", False, 50.0, 40.0)[0]
        unknown = exchange.classify("cure", False, None, 40.0)[0]
        self.assertEqual([empty, wash, lift, ratioed, displaced, unknown],
                         [exchange.FILL, exchange.SWEEP, exchange.PARTIAL,
                          exchange.PARTIAL, exchange.SWEEP, exchange.SWEEP])

    def test_the_diffusion_soak_comes_from_two_contract_values(self):
        soak = exchange.interior_soak_s()
        tau = contracts.param(exchange.DIFFUSION_TIME_KEY)
        target = contracts.param(exchange.INTERIOR_FRACTION_KEY)
        self.assertAlmostEqual(soak, -math.log(1 - target) * tau)
        self.assertGreater(soak, tau)         # the interior lags the chamber

    def test_one_chamber_volume_does_not_clear_a_chamber(self):
        step = {"step": 1, "operation": "exchange", "volume_ul": 38.443, "washes": 1,
                "exchange_kind": "sweep", "note": "fill the trap"}
        self.assertTrue(any("a sweep must reach" in p and "1.00 chamber volumes" in p
                            for p in exchange.problems(step, 38.443)))
        step["washes"] = 5
        self.assertEqual(exchange.problems(step, 38.443), [])

    def test_below_one_chamber_volume_no_number_of_repeats_is_enough(self):
        step = {"step": 1, "operation": "exchange", "volume_ul": 10.0, "washes": 1,
                "exchange_kind": "sweep", "note": "a small rinse"}
        self.assertTrue(any("no number of repeats" in p
                            for p in exchange.problems(step, 38.443)))

    def test_absence_is_held_to_the_sweep_rule(self):
        step = {"step": 1, "operation": "exchange", "volume_ul": 38.443, "washes": 1,
                "note": "fill the trap"}
        self.assertTrue(exchange.problems(step, 38.443))

    def test_a_wash_bigger_than_the_chamber_is_refused(self):
        step = {"step": 1, "operation": "exchange", "volume_ul": 200, "washes": 5,
                "exchange_kind": "sweep", "note": ""}
        self.assertTrue(any("more than the" in p for p in exchange.problems(step, 38.443)))


class Reagents(unittest.TestCase):
    """Generic names only, by construction and then by screen."""

    def test_the_screen_catches_what_must_never_be_committed(self):
        cases = {
            "lyse in 4 M guanidinium thiocyanate": "composition",
            "add 10 mg/mL enzyme": "concentration",
            "Trypsin for 5 min": "enzyme",
            "from Thermo Fisher, cat. no. 12345": "vendor",
            "primer ACGTACGTAC": "sequence",
            "see /Users/someone/OneDrive/protocol.docx": "file store",
            "dilute the 10x stock": "stock",
        }
        for text, label in cases.items():
            with self.subTest(label):
                self.assertTrue(reagents.screen_text(text, "note"), f"missed {label}")

    def test_the_screen_leaves_our_own_physics_prose_alone(self):
        clean = ("Sweep 'main_trap' with wash_buffer: 5 fills of one 38.443 uL chamber volume "
                 "each, 5 chamber volumes in total, 0.2 mm channels at 0.3 mm depth, 0.01 "
                 "dyn/cm2, 3.5 kPa, 0.8 mPa s at 37 C, 85% of the ceiling, 99% replaced.")
        self.assertEqual(reagents.screen_text(clean, "note"), [])

    def test_a_name_outside_the_vocabulary_cannot_be_written(self):
        with self.assertRaisesRegex(reagents.ReagentError, "generic vocabulary"):
            reagents.check_name("trizol")
        for name in reagents.VOCABULARY:
            reagents.check_name(name)

    def test_the_source_document_has_no_path_into_a_protocol(self):
        poisoned = json.loads(UNITOPS_PATH.read_text())
        poisoned["note"] = "lyse in 4 M guanidinium thiocyanate from Sigma, cat. no. 1234"
        for item in poisoned["ops"]:
            item["note"] = "Trypsin 0.25% w/v, 10 mg/mL, primer ACGTACGTACGT"
        protocol, _ = s8_protocol.build_protocol(CHIP, poisoned, None, None, "module3_dev")
        self.assertEqual(reagents.screen(protocol), [])
        self.assertNotIn("Trypsin", json.dumps(protocol))
        self.assertNotIn("guanidinium", json.dumps(protocol))

    def test_a_reagent_is_named_by_what_happens_to_it_next(self):
        delivery = op(1, "load", particles="capsules", volume_ul=40)
        first, _ = reagents.name_for(delivery, chamber_is_empty=True)
        later, _ = reagents.name_for(delivery, chamber_is_empty=False, next_kind="incubate")
        bare, gap = reagents.name_for(delivery, chamber_is_empty=False, next_kind="")
        self.assertEqual([first, later, bare],
                         ["capsule_suspension", "reaction_mix", "reagent"])
        self.assertTrue(gap)


class Module3Protocol(unittest.TestCase):
    """S8 on an existing chip with hand-made ops."""

    def setUp(self):
        self.protocol, self.plan = build_module3()
        self.classes = s8_protocol.classes_from_ops(
            on_chip_ops(), build.assign(geometry.Chip(CHIP), on_chip_ops(), None))

    def test_it_matches_the_protocol_contract(self):
        self.assertEqual(schema_io.schema_errors("protocol", self.protocol), [])

    def test_it_passes_every_audit_rule_it_is_held_to(self):
        self.assertEqual(s8_protocol.check(self.protocol, CHIP, self.classes), [])

    def test_the_shape_is_the_assay_in_order(self):
        self.assertEqual([s["operation"] for s in self.protocol["steps"]],
                         ["load", "exchange", "hold_temp", "exchange", "exchange",
                          "hold_temp", "exchange", "exchange", "release"])
        self.assertEqual([s["step"] for s in self.protocol["steps"]], list(range(1, 10)))

    def test_every_target_is_on_the_chip_and_numbering_is_contiguous(self):
        places = geometry.Chip(CHIP).place_ids
        for index, step in enumerate(self.protocol["steps"], start=1):
            self.assertEqual(step["step"], index)
            for target in [step.get("target")] + list(step.get("targets") or []):
                if target:
                    self.assertIn(target, places)

    def test_the_load_fills_the_built_chamber_not_the_papers_tube(self):
        load = self.protocol["steps"][0]
        paper = UNITOPS["ops"][0]["fluid"]["volume_ul"]
        self.assertEqual(load["operation"], "load")
        self.assertEqual(load["volume_ul"], 38.443)           # one chamber volume
        self.assertNotEqual(load["volume_ul"], paper)          # the tube volume, ignored
        self.assertEqual(load["reagent"], "capsule_suspension")
        self.assertTrue(any("describes a tube" in gap.question for gap in self.plan.gaps))

    def test_no_step_takes_a_volume_from_the_source(self):
        papers = {op["fluid"]["volume_ul"] for op in UNITOPS["ops"]
                  if (op.get("fluid") or {}).get("volume_ul")}
        self.assertTrue(papers)
        for step in self.protocol["steps"]:
            if step.get("volume_ul"):
                self.assertNotIn(step["volume_ul"], papers)
                self.assertLessEqual(step["volume_ul"], 38.443 + 1e-9)

    def test_a_stated_ratio_is_superseded_when_the_sample_is_retained(self):
        # The source adds an equal volume of mix, but the capsules sit on the weir and the
        # liquid around them is carrier: matching the ratio against carrier would halve the
        # reagent. The step sweeps instead, and says so.
        mix = next(s for s in self.protocol["steps"] if s["reagent"] == "reaction_mix")
        self.assertEqual(mix["exchange_kind"], "sweep")
        self.assertIn("carrier, not sample", mix["note"])
        self.assertTrue(any("retains what is being treated" in gap.question
                            for gap in self.plan.gaps))

    def test_a_missing_ratio_is_carried_as_an_open_question_in_the_note(self):
        lysis = next(s for s in self.protocol["steps"] if s["reagent"] == "lysis_buffer")
        self.assertIn("OPEN QUESTION", lysis["note"])
        self.assertEqual(lysis["exchange_kind"], "sweep")

    def test_a_capsule_step_gets_at_least_the_diffusion_soak(self):
        soak = exchange.interior_soak_s()
        for step in self.protocol["steps"]:
            if step["operation"] != "exchange":
                continue
            self.assertGreaterEqual(step["duration_s"], math.ceil(soak * step["washes"]) - 1)
        sweeps = [s for s in self.protocol["steps"] if s.get("exchange_kind") == "sweep"]
        self.assertTrue(all("diffuse INSIDE" in s["note"] for s in sweeps))
        self.assertTrue(all(s["duration_s"] > s["volume_ul"] * s["washes"]
                            / s["flow_ul_min"] * 60 * build.DURATION_MARGIN for s in sweeps))

    def test_every_exchange_declares_its_kind_and_meets_that_rule(self):
        kinds = [s.get("exchange_kind") for s in self.protocol["steps"]
                 if s["operation"] == "exchange"]
        self.assertTrue(kinds)
        self.assertNotIn(None, kinds)
        for step in self.protocol["steps"]:
            if step["operation"] == "exchange":
                self.assertEqual(exchange.problems(step, 38.443), [], step["note"])

    def test_the_reagent_change_before_the_hold_is_a_sweep_not_a_one_volume_fill(self):
        # The defect in the hand-written file: one chamber volume of reaction mix on top of
        # a chamber full of wash buffer leaves 37% of the wash buffer behind.
        mix = next(s for s in self.protocol["steps"] if s["reagent"] == "reaction_mix")
        self.assertEqual(mix["exchange_kind"], "sweep")
        self.assertGreaterEqual(mix["washes"] * mix["volume_ul"] / 38.443,
                                exchange.sweep_chamber_volumes())

    def test_flows_come_from_the_scoped_ceiling_on_the_real_cross_section(self):
        for number, flow in self.plan.flows:
            self.assertIn(flow.bound_by, ("escape_pressure", "lift_flow"))
            self.assertAlmostEqual(flow.section.width_mm, 11.32)
            self.assertLessEqual(flow.pressure_kpa, contracts.max_working_pressure_kpa() + 1e-9)
        steps = {s["step"]: s for s in self.protocol["steps"]}
        for number, flow in self.plan.flows:
            self.assertEqual(steps[number]["flow_ul_min"], flow.flow_ul_min)

    def test_the_backflush_and_release_run_at_the_lift_flow(self):
        lift = float(contracts.param(flows.LIFT_FLOW_KEY))
        backflush = next(s for s in self.protocol["steps"] if s.get("direction") == "backflush")
        release = next(s for s in self.protocol["steps"] if s["operation"] == "release")
        self.assertEqual(backflush["flow_ul_min"], lift)
        self.assertEqual(release["flow_ul_min"], lift)
        self.assertEqual(backflush["exchange_kind"], "partial")

    def test_durations_are_the_physics_plus_a_stated_margin_or_the_soak(self):
        soak = exchange.interior_soak_s()
        for step in self.protocol["steps"]:
            if not step.get("volume_ul") or not step.get("flow_ul_min"):
                continue
            washes = step["washes"] or 1
            needed = step["volume_ul"] * washes / step["flow_ul_min"] * 60
            self.assertEqual(step["duration_s"],
                             max(math.ceil(needed * build.DURATION_MARGIN),
                                 math.ceil(soak * washes)))
            self.assertIn("25%", step["note"])

    def test_every_flow_step_accounts_for_every_port(self):
        ports = set(geometry.Chip(CHIP).port_ids())
        for step in self.protocol["steps"]:
            state = step["ports"]
            listed = state["feed"] + state["vent"] + state["cap"]
            self.assertEqual(sorted(listed), sorted(ports), step["note"])
            self.assertEqual(len(listed), len(set(listed)))
            if step["operation"] in audit.FLOW_OPERATIONS:
                self.assertTrue(state["feed"] and state["vent"])
            else:
                self.assertEqual(state["feed"], [])

    def test_the_lysis_effluent_keeps_its_own_waste_line(self):
        lysis = next(s for s in self.protocol["steps"] if s["reagent"] == "lysis_buffer")
        wash = next(s for s in self.protocol["steps"] if s["reagent"] == "wash_buffer")
        self.assertEqual(lysis["waste"], "lysis_waste")
        self.assertEqual(wash["waste"], "waste")
        self.assertEqual(lysis["ports"]["vent"], ["lysis_waste"])

    def test_every_temperature_step_names_a_declared_zone_that_can_hold_it(self):
        chip = geometry.Chip(CHIP)
        holds = [s for s in self.protocol["steps"] if s["temp_c"] is not None]
        self.assertEqual(len(holds), 2)
        for step in holds:
            zone = chip.thermal_zone(step["target"])
            self.assertTrue(zone)
            self.assertIn(zone["id"], step["note"])
            self.assertEqual(chip.zone_holds(step["target"], step["temp_c"]), "")

    def test_a_hold_outside_every_zone_is_refused(self):
        hot = op(1, "lyse", temp_c=95, duration_s=60, particles="capsules",
                 shear_class="capsules")
        unitops = {"id": "hot", "ops": [hot], "unresolved": []}
        with self.assertRaisesRegex(build.BuildError, "thermal zone"):
            s8_protocol.build_protocol(CHIP, unitops, None, None, "hot")

    def test_gaps_are_recorded_rather_than_filled_in(self):
        questions = " ".join(gap.question for gap in self.plan.gaps)
        self.assertIn("describes a tube", questions)
        self.assertIn("off_chip", questions)
        self.assertIn("missing_part", questions)
        self.assertIn("GAPS", self.protocol["description"])
        self.assertTrue(self.protocol["placeholder"])

    def test_it_is_deterministic(self):
        again, _ = build_module3()
        self.assertEqual(json.dumps(self.protocol, sort_keys=True),
                         json.dumps(again, sort_keys=True))
        self.assertEqual(audit.TIMESTAMP.search(json.dumps(self.protocol)), None)

    def test_the_operator_can_plan_every_step_on_the_chip(self):
        self.assertEqual(planner.check_inputs(self.protocol, CHIP), [])
        adapter = planner.plan(self.protocol, CHIP)
        self.assertEqual(len(adapter.events), len(self.protocol["steps"]))

    def test_the_report_lines_up_with_the_steps(self):
        text = s8_protocol.report(self.protocol, self.plan)
        self.assertIn("pressure limit", text)
        self.assertEqual(text.count("GAP  "), len(self.plan.gaps))

    def test_a_broken_protocol_is_not_written(self):
        broken = json.loads(json.dumps(self.protocol))
        broken["steps"][1]["washes"] = 1                       # a one-volume sweep
        self.assertTrue(s8_protocol.check(broken, CHIP, self.classes))
        broken = json.loads(json.dumps(self.protocol))
        broken["steps"][1]["ports"]["cap"] = []
        self.assertTrue(any("undefined" in p
                            for p in s8_protocol.check(broken, CHIP, self.classes)))


class WhichChamber(unittest.TestCase):
    """An op's chamber comes from the chip, an explicit map, or a refusal."""

    def test_one_usable_chamber_needs_no_mapping(self):
        chip = geometry.Chip(CHIP)
        self.assertEqual(chip.workplaces(), [TRAP])            # the filter is not a workplace
        self.assertEqual(set(build.assign(chip, on_chip_ops(), None).values()), {TRAP})

    def test_an_ambiguous_chip_is_refused_and_from_ops_settles_it(self):
        spec = mixer_chip(channel_width_mm=0.5)
        spec["chambers"][1]["type"] = "culture"            # two chambers could host a wash
        ops = [op(1, "wash", particles="cells", shear_class="adherent_cells")]
        with self.assertRaisesRegex(build.BuildError, "from_ops"):
            build.assign(geometry.Chip(spec), ops, None)
        self.assertEqual(build.assign(geometry.Chip(spec), ops, {1: "mix_1"}), {1: "mix_1"})
        spec["chambers"][0]["from_ops"] = [1]
        self.assertEqual(build.assign(geometry.Chip(spec), ops, None), {1: "cult_1"})

    def test_a_filter_or_a_junction_is_never_where_an_op_runs(self):
        chip = geometry.Chip(CHIP)
        self.assertIn("capsule_prefilter", chip.chambers)
        self.assertNotIn("capsule_prefilter", chip.workplaces())

    def test_a_ratio_sizes_the_step_when_the_liquid_is_the_sample(self):
        # No retention feature, so the chamber's liquid IS the sample: the paper's 1:1
        # ratio survives as ln(2) chamber volumes, and the concentration matches the paper
        # whatever the chamber turned out to hold.
        spec = mixer_chip(channel_width_mm=0.5)
        spec["chambers"][0]["from_ops"] = [1, 2]
        ops = [op(1, "load", particles="cells", shear_class="none"),
               op(2, "load", particles="cells", shear_class="none", volume_ul=500)]
        ops[1]["ratio"] = {"reagent_to_sample": 1.0, "paper_reagent_ul": 500,
                           "paper_sample_ul": 500}
        protocol, plan = s8_protocol.build_protocol(
            spec, {"id": "ratio", "ops": ops, "unresolved": []}, None, {1: "cult_1", 2: "cult_1"})
        added = next(s for s in protocol["steps"] if s["operation"] == "exchange")
        volume = spec["chambers"][0]["volume_ul"]
        self.assertEqual(added["exchange_kind"], "partial")
        self.assertAlmostEqual(added["volume_ul"], round(volume * math.log(2), 3))
        self.assertIn("1:1 reagent-to-sample", added["note"])
        self.assertNotIn(500, [s.get("volume_ul") for s in protocol["steps"]])
        self.assertEqual(s8_protocol.check(protocol, spec, {"cult_1": "none"}), [])

    def test_a_kind_outside_s2s_vocabulary_is_refused(self):
        with self.assertRaisesRegex(build.BuildError, "s2_normalise"):
            build.mapping_for("sonicate")

    def test_the_mapping_is_s2s_and_still_fits_both_contracts(self):
        from assay2chip.stages import s2_normalise

        self.assertIs(build.KIND_TO_PROTOCOL, s2_normalise.KIND_TO_PROTOCOL)
        s2_normalise.check_vocabulary()


class Grouping(unittest.TestCase):
    """S5: the design file, in the format that already exists."""

    def setUp(self):
        entry = s5_group.chip_entry(PARTITION, "module3_v1_translated")
        self.ops = s5_group.ops_for(UNITOPS, entry)
        self.design = s5_group.group(entry, self.ops, UNITOPS)
        self.steps = s5_group.chip_steps(self.ops)

    def test_everything_in_one_place_is_one_module(self):
        self.assertEqual([m["id"] for m in self.design["modules"]], ["culture_1"])
        self.assertEqual(self.design["modules"][0]["steps"], [1, 2, 3, 4, 5, 6, 7, 8])

    def test_every_assay_step_is_accounted_for(self):
        self.assertEqual(s5_group.unaccounted(self.design, self.steps), [])
        table = s5_group.coverage(self.design, self.steps)
        self.assertEqual(len(table), len(self.steps))
        self.assertNotIn("NOT ACCOUNTED FOR", "".join(table))
        self.assertEqual(self.design["off_chip"][0]["steps"], [9])
        self.assertEqual(self.design["missing_parts"][0]["steps"], [10])

    def test_a_dropped_step_raises_rather_than_being_written(self):
        design = json.loads(json.dumps(self.design))
        design["modules"][0]["steps"] = [1, 2]
        self.assertEqual(s5_group.unaccounted(design, self.steps), [3, 4, 5, 6, 7, 8])
        self.assertIn("NOT ACCOUNTED FOR", "".join(s5_group.coverage(design, self.steps)))

    def test_build_chip_can_still_use_our_design(self):
        self.assertEqual(s5_group.check_buildable(self.design), [])

    def test_inlets_are_generic_reagent_lines_and_exclude_what_travels_the_chain(self):
        inlets = self.design["modules"][0]["inlets"]
        self.assertEqual(inlets, ["lysis_buffer", "wash_buffer", "reaction_mix"])
        for name in inlets:
            reagents.check_name(name)
            self.assertNotIn(name, s5_group.CARRIED)

    def test_a_retention_opening_comes_from_the_contract(self):
        pore = self.design["modules"][0]["mesh_pore_um"]
        self.assertAlmostEqual(pore, 70 * contracts.param("trap.gap_factor_low"))
        self.assertLess(pore, 70 * contracts.param("trap.gap_factor_high"))

    def test_a_new_place_starts_a_new_module(self):
        entry = {"chip_id": "two_place_chip", "ops": [1, 2, 3], "scale": "demo",
                 "reason": "test"}
        ops = [op(1, "load", particles="cells", part="culture_chamber"),
               op(2, "mix", part="serpentine_channel"),
               op(3, "wash", particles="cells", part="serpentine_channel")]
        design = s5_group.group(entry, ops, {"id": "two"},
                                parts={"culture_chamber": {}, "serpentine_channel": {}})
        self.assertEqual([m["id"] for m in design["modules"]],
                         ["culture_1", "serpentine_channel_1"])
        self.assertEqual(design["modules"][1]["steps"], [2, 3])

    def test_a_part_that_is_not_a_block_id_is_refused(self):
        entry = {"chip_id": "x", "ops": [1], "scale": "demo", "reason": "test"}
        ops = [op(1, "wash", part="unicorn_chamber")]
        with self.assertRaisesRegex(s5_group.GroupError, "contracts/blocks.json"):
            s5_group.group(entry, ops, {"id": "x"}, parts={"culture_chamber": {}})

    def test_the_block_id_is_authoritative_and_the_flowmatter_name_is_looked_up(self):
        module = self.design["modules"][0]
        self.assertEqual(module["block"], "culture_chamber")
        self.assertIn(module["block"], contracts.library())
        self.assertEqual(module["part"], s5_group.block_to_flowmatter()["culture_chamber"])
        self.assertTrue(self.design["build_chip_compatible"])

    def test_a_block_with_no_flowmatter_shape_is_recorded_not_invented(self):
        entry = {"chip_id": "x", "ops": [1], "scale": "demo", "reason": "test"}
        ops = [op(1, "mix", part="herringbone_mixer")]
        design = s5_group.group(entry, ops, {"id": "x"})
        module = design["modules"][0]
        self.assertEqual(module["block"], "herringbone_mixer")
        self.assertIsNone(module["part"])
        self.assertIn("no Flowmatter part", module["why_no_part"])
        self.assertFalse(design["build_chip_compatible"])
        self.assertIn("build_chip.py cannot build", design["note"])
        # His builder places one library STL per module, so it cannot represent this one;
        # that is the compatibility path, and S6 reads `block` instead.
        self.assertEqual(s5_group.check_buildable(design), [])
        self.assertNotIn("herringbone_mixer", s5_group.block_to_flowmatter())

    def test_s3s_catalogue_part_is_used_when_it_gives_one(self):
        entry = {"chip_id": "x", "ops": [1], "scale": "demo", "reason": "test"}
        ops = [op(1, "mix", part="serpentine_channel")]
        ops[0]["placement"]["catalogue_part"] = "snake_channel"
        design = s5_group.group(entry, ops, {"id": "x"})
        self.assertEqual(design["modules"][0]["part"], "snake_channel")
        self.assertEqual(design["modules"][0]["block"], "serpentine_channel")

    def test_an_op_on_no_chip_is_still_written_down_somewhere(self):
        partition = {"chips": [{"chip_id": "c1", "ops": [1], "scale": "demo", "reason": "a"},
                               {"chip_id": "c2", "ops": [4], "scale": "demo", "reason": "b"}]}
        bench = op(2, "image")
        bench["placement"] = {"where": "off_chip", "why": "a microscope does it"}
        unitops = {"id": "x", "ops": [op(1, "wash"), bench, op(4, "wash")]}
        spare = s5_group.elsewhere_by_chip(unitops, partition)
        self.assertEqual([o["op"] for o in spare["c1"]], [2])
        self.assertEqual(spare["c2"], [])

    def test_the_port_budget_is_read_from_the_contract_and_reported(self):
        entry = {"chip_id": "wide", "ops": list(range(1, 7)), "scale": "demo",
                 "reason": "test"}
        ops = [op(1, "load", particles="cells"), op(2, "wash", particles="cells"),
               op(3, "lyse", particles="cells"), op(4, "incubate", volume_ul=1),
               op(5, "culture", particles="cells"), op(6, "image")]
        design = s5_group.group(entry, ops, {"id": "wide"}, parts={"culture_chamber": {}})
        budget = contracts.max_ports_per_chip("demo")
        self.assertIn(str(budget), design.get("note", ""))

    def test_the_model_is_optional_and_stubbed_offline(self):
        from assay2chip.lib import llm

        design = s5_group.polish(json.loads(json.dumps(self.design)), "stub")
        self.assertEqual(design["modules"][0]["does"],
                         self.design["modules"][0]["does"])
        self.assertEqual(llm.provenance()["mode"], "stub")

    def test_it_is_deterministic(self):
        entry = s5_group.chip_entry(PARTITION, "module3_v1_translated")
        again = s5_group.group(entry, self.ops, UNITOPS)
        self.assertEqual(json.dumps(again, sort_keys=True),
                         json.dumps(self.design, sort_keys=True))


class TheHandWrittenProtocol(unittest.TestCase):
    """The comparison: the same rules, run against protocols/module3_v1.json."""

    @classmethod
    def setUpClass(cls):
        cls.hand = json.loads((ROOT / "protocols" / "module3_v1.json").read_text())
        cls.found = audit.problems(cls.hand, CHIP)

    def test_the_hand_written_file_is_valid_against_the_contract(self):
        # It is a good file: it passes the schema and tools/validate.py. The audit rules are
        # about what the schema cannot express.
        self.assertEqual(schema_io.schema_errors("protocol", self.hand), [])

    def test_the_audit_finds_the_undeclared_exchange_kinds(self):
        missing = [p for p in self.found if "no exchange_kind" in p]
        exchanges = [s for s in self.hand["steps"] if s["operation"] == "exchange"]
        self.assertEqual(len(missing), len(exchanges))

    def test_the_audit_finds_the_one_volume_fill_that_cannot_clear_the_chamber(self):
        self.assertTrue(any("a sweep must reach" in p and "1.00 chamber volumes" in p
                            for p in self.found))

    def test_the_audit_finds_durations_with_no_margin(self):
        self.assertTrue(any("bare transfer time" in p for p in self.found))

    def test_our_generated_protocol_passes_what_the_hand_written_one_does_not(self):
        ours, _ = build_module3()
        classes = s8_protocol.classes_from_ops(
            on_chip_ops(), build.assign(geometry.Chip(CHIP), on_chip_ops(), None))
        self.assertEqual(audit.problems(ours, CHIP, classes), [])
        self.assertGreater(len(self.found), 5)


if __name__ == "__main__":
    unittest.main()

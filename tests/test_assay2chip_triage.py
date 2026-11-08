"""S3 triage: does the rule engine reproduce the decisions this repo already made?

    python -m unittest tests.test_assay2chip_triage -v
    python -m unittest discover -s tests

Everything here runs offline. The fixture is a hand-made unitops.json describing the MVP
pipeline (protocols/pipeline_mvp.json, protocols/pipeline_status.md), and the ground truth
is what that pipeline says: encapsulation off chip on a kit, curing off chip, emulsion
breaking off chip, barcoding re-injection off chip, RT on chip in the capsule trap, index
PCR on chip in a high-Tg chip with a dock, ethanol bead clean-up off chip.

Two decisions where triage deliberately differs from the repo are asserted as
disagreements rather than hidden - see KNOWN_DISAGREEMENTS at the bottom, which says in
each case whether triage or the repo is wrong.
"""

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts, io as stage_io, llm  # noqa: E402
from assay2chip.stages import s3_triage  # noqa: E402
from assay2chip.triage import engine, facts, library, rules  # noqa: E402


def op(number, kind, note, particles="none", diameter_um=None, temp_c=None,
       duration_s=None, shear_class="none", viscosity_class="aqueous", volume_ul=100.0,
       from_steps=None, confidence=0.9):
    """One unit operation exactly as S2 hands it over: placement is `pending`."""
    body = {
        "op": number,
        "from_steps": from_steps or [number],
        "kind": kind,
        "confidence": confidence,
        "fluid": {"volume_ul": volume_ul, "viscosity_class": viscosity_class},
        "particles": {"kind": particles},
        "duration_s": duration_s,
        "temp_c": temp_c,
        "shear_class": shear_class,
        "note": note,
        "placement": {"where": "pending", "why": "S2 has not decided; S3 decides"},
    }
    if particles != "none":
        body["particles"]["diameter_um"] = diameter_um
    return body


CELL_UM = 12.0          # a bare iPSC, the 10-15 um case in protocols/retention_audit.md
BEAD_UM = 1.0           # protocols/module5_design.md section 6


def mvp_unitops():
    """The MVP pipeline as unit operations. No reagent recipes: generic names only."""
    capsule_um = contracts.capsule_diameter_um()
    return {
        "id": "mvp_ground_truth",
        "placeholder": True,
        "note": "Hand-made from protocols/pipeline_mvp.json and protocols/pipeline_status.md "
                "to test S3 triage. Generic reagent names only (CONFIDENTIALITY.md).",
        "ops": [
            op(1, "culture", "grow the wild-type lineages in medium at 37 C with continuous "
                             "perfusion from a vented reservoir",
               particles="cells", diameter_um=CELL_UM, temp_c=37.0,
               duration_s=87 * 86400, shear_class="adherent_cells"),
            op(2, "wash", "pass the harvested suspension through a strainer to remove clumps",
               particles="cells", diameter_um=CELL_UM, temp_c=4.0),
            op(3, "wash", "spin down the cells and resuspend them in fresh buffer",
               particles="cells", diameter_um=CELL_UM, temp_c=4.0),
            op(4, "encapsulate", "co-flow the cell suspension and the shell polymer into a "
                                 "flow-focusing nozzle where oil cuts droplets",
               particles="capsules", diameter_um=capsule_um, viscosity_class="oil"),
            op(5, "cure", "collect the droplets and flood-cure them",
               particles="capsules", diameter_um=capsule_um, duration_s=1200.0),
            op(6, "separate", "break the emulsion and wash off the oil",
               particles="capsules", diameter_um=capsule_um, viscosity_class="oil"),
            op(7, "lyse", "flow lysis buffer through the trapped capsule bed",
               particles="capsules", diameter_um=capsule_um, duration_s=900.0,
               shear_class="capsules"),
            op(8, "wash", "flush the capsule bed with wash buffer",
               particles="capsules", diameter_um=capsule_um, shear_class="capsules"),
            op(9, "incubate", "add the RT mix to the capsule bed and hold it",
               particles="capsules", diameter_um=capsule_um, temp_c=42.0, duration_s=3600.0,
               shear_class="capsules"),
            op(10, "merge", "re-inject the capsules close-packed so each meets one barcode in "
                            "its own droplet",
               particles="capsules", diameter_um=capsule_um, viscosity_class="oil"),
            op(11, "split", "split the pooled capsules into four equal sublibraries",
               particles="capsules", diameter_um=capsule_um),
            op(12, "thermocycle", "index PCR, 98 / 60 / 72 C",
               particles="capsules", diameter_um=capsule_um, temp_c=98.0, duration_s=3600.0),
            op(13, "wash", "ethanol wash on the magnetic beads, then air dry the pellet",
               particles="beads", diameter_um=BEAD_UM),
            op(14, "image", "image the chamber and count the cells for viability",
               particles="cells", diameter_um=CELL_UM),
            op(15, "separate", "separate the two fractions",
               volume_ul=50.0),
            op(16, "load", "load the cured capsules into the trap through its pre-filter",
               particles="capsules", diameter_um=capsule_um),
            op(17, "transfer", "carry the sublibrary to the clean-up chip",
               particles="capsules", diameter_um=capsule_um, volume_ul=40.0),
        ],
        "unresolved": [],
    }


#: What protocols/pipeline_mvp.json and protocols/pipeline_status.md already decided.
#: (op number) -> (where, part or None when the pipeline names no block)
GROUND_TRUTH = {
    2: ("off_chip", None),       # status 2b: spin, strainer, resuspend - OFF-CHIP
    4: ("off_chip", None),       # module2: a printable orifice cannot make 70 um capsules
    5: ("off_chip", None),       # pipeline_mvp module2 build: "curing off-chip"
    6: ("off_chip", None),       # status 3 -> 4: emulsion break and oil wash, OFF-CHIP HOLE
    7: ("on_chip", "trap_chamber"),    # module3 main_trap: lysis in the capsule trap
    8: ("on_chip", "trap_chamber"),    # module3 main_trap: wash in the capsule trap
    9: ("on_chip", "trap_chamber"),    # module3 steps 5-7: RT in the trap, 42 C
    10: ("off_chip", None),      # barcoding re-injection: bought droplet hardware
    12: ("on_chip", "polygon_chamber"),  # module4 sub_1-4: index PCR, high-Tg chip + dock
    13: ("off_chip", None),      # module5_design section 6: no ethanol wash or dry on chip
    16: ("on_chip", "trap_chamber"),     # module3: capsules load into main_trap
    17: ("off_chip", None),      # pipeline_mvp handoffs: tubing between chips, S4's job
}


class RuleTable(unittest.TestCase):
    def test_table_is_well_formed(self):
        self.assertEqual([], rules.audit())

    def test_every_mapped_part_is_a_block_id(self):
        self.assertEqual([], library.audit())
        allowed = contracts.library()
        for purpose, (part, citation) in library.PARTS.items():
            self.assertIn(part, allowed, "%s -> %s is not a block id" % (purpose, part))
            self.assertTrue(citation, "%s has no citation" % purpose)

    def test_tesla_valve_is_never_available(self):
        with self.assertRaises(library.PartError):
            library.check("tesla_valve", "rectify the backflow")
        with self.assertRaises(library.PartError):
            library.check("tesla_valve", "anything at all")
        self.assertNotIn("tesla_valve", [p for p, _ in library.PARTS.values()])

    def test_pillars_are_banned_for_retention_but_fine_as_support(self):
        with self.assertRaises(library.PartError):
            library.check("pillar_array", "retain the capsules at the outlet")
        self.assertEqual("pillar_array", library.check("pillar_array", "lid support"))

    def test_multilayer_and_invented_parts_are_refused(self):
        for name in ("membrane_filter", "pneumatic_valve", "crossing_channel", "mesh_insert"):
            with self.assertRaises(library.PartError):
                library.check(name, "whatever")

    def test_only_non_physics_rules_may_share_a_rank(self):
        by_rank = {}
        for r in rules.RULES:
            by_rank.setdefault(r.rank, []).append(r)
        for rank, group in by_rank.items():
            if len(group) > 1:
                self.assertTrue(all(not r.physics for r in group),
                                "rank %d mixes physics rules: %s"
                                % (rank, [r.id for r in group]))


class Geometry(unittest.TestCase):
    """The physics the rules rest on, straight out of the contracts."""

    def test_weir_retains_capsules(self):
        weir = facts.weir_for(contracts.capsule_diameter_um())
        self.assertTrue(weir.buildable)
        self.assertEqual(facts.agreed_weir_gap_um(), weir.gap_um)
        self.assertLessEqual(weir.gap_low_um, weir.gap_um)
        self.assertLess(weir.gap_um, weir.gap_high_um)

    def test_weir_does_not_retain_a_bare_cell(self):
        weir = facts.weir_for(CELL_UM)
        self.assertFalse(weir.buildable)
        self.assertIsNone(weir.gap_um)
        # 3-6 um for a 10-15 um cell, exactly as protocols/retention_audit.md says.
        self.assertLess(weir.gap_high_um, facts.agreed_weir_gap_um())

    def test_depth_is_not_subject_to_the_in_plane_floor(self):
        """The whole retention scheme: the gap is far below the floor and still legal."""
        weir = facts.weir_for(contracts.capsule_diameter_um())
        self.assertLess(weir.gap_um, facts.min_inplane_um())
        self.assertTrue(weir.buildable)
        self.assertEqual(facts.crest_thickness_um(), facts.min_inplane_um())

    def test_a_bigger_capsule_buys_a_bigger_gap(self):
        """The contract's own lever when the weir is too fine: make the capsule bigger."""
        _low, high = contracts.param_range("capsule.diameter_tunable_um")
        big = facts.weir_for(float(high))
        self.assertTrue(big.buildable)
        self.assertGreater(big.gap_um, facts.agreed_weir_gap_um())

    def test_droplet_nozzle_is_below_the_floor(self):
        orifice = facts.orifice_for(contracts.capsule_diameter_um())
        self.assertAlmostEqual(contracts.param("encapsulation.nozzle_um"), orifice, places=6)
        self.assertLess(orifice, facts.min_inplane_um())

    def test_high_tg_materials_come_from_the_contract(self):
        qualified = facts.high_tg_materials()
        self.assertTrue(qualified)
        for bad in ("pmma", "printed_resin", "cop_zeonor_1060r"):
            self.assertNotIn(bad, qualified)
            self.assertIn(bad, facts.disqualified_for_pcr())


class GroundTruth(unittest.TestCase):
    """Feed the MVP pipeline in and check triage reproduces what the repo decided."""

    @classmethod
    def setUpClass(cls):
        llm.configure("stub")
        llm.reset()
        cls.result = engine.triage(mvp_unitops())

    def test_reproduces_the_mvp_pipeline(self):
        disagreements = engine.compare(self.result, GROUND_TRUTH)
        self.assertEqual([], disagreements, "\n".join([""] + disagreements))

    def test_not_one_pending_survives(self):
        """S2 writes every op `pending`; finishing with one left is a failure."""
        self.assertEqual(["pending"] * len(mvp_unitops()["ops"]),
                         [o["placement"]["where"] for o in mvp_unitops()["ops"]])
        self.assertEqual([], engine.pending(self.result.unitops))
        self.assertEqual(0, self.result.summary["pending_left"])
        for item in self.result.unitops["ops"]:
            self.assertNotEqual("pending", item["placement"]["where"],
                                "op %s was left pending" % item["op"])

    def test_a_rule_may_never_return_pending(self):
        verdict = rules.Verdict("pending", "a rule trying to defer", rule="test.defer")
        with self.assertRaises(ValueError):
            verdict.placement()

    def test_a_half_triaged_file_is_refused(self):
        """If anything ever did leave a pending behind, the engine raises rather than returns."""
        payload = mvp_unitops()
        self.assertEqual([1], engine.pending({"ops": [payload["ops"][0]]}))
        with self.assertRaises(engine.PendingLeft):
            engine.triage(payload, rule_table=[rules.Rule(
                "test.defers_everything", 1, rules.CONTRACT, False,
                lambda f: None, "a table with no fallback")])

    def test_new_kinds_are_handled(self):
        self.assertEqual(("on_chip", "trap_chamber"), self.result.where(16))   # load
        self.assertEqual(("off_chip", None), self.result.where(17))            # transfer
        self.assertIn("handoff", self.result.unitops["ops"][16]["placement"]["why"])
        culture = self.result.unitops["ops"][0]["placement"]
        self.assertEqual(("on_chip", "culture_chamber"), self.result.where(1))
        self.assertIn("perfusion", culture["why"])

    def test_catalogue_part_is_filled_where_a_shape_exists(self):
        culture = self.result.unitops["ops"][0]["placement"]
        self.assertEqual("culture_chamber", culture.get("catalogue_part"))
        # The weir has no Flowmatter shape, so it gets no catalogue_part rather than a guess.
        self.assertNotIn("catalogue_part", self.result.unitops["ops"][8]["placement"])

    def test_every_op_has_a_placement_with_a_reason(self):
        for item in self.result.unitops["ops"]:
            placement = item.get("placement")
            self.assertIsInstance(placement, dict, "op %s has no placement" % item["op"])
            self.assertIn(placement["where"], ("on_chip", "off_chip", "missing_part"))
            self.assertTrue(placement.get("why", "").strip(),
                            "op %s has no why" % item["op"])
            self.assertTrue(
                any(token in placement["why"] for token in
                    ("contracts/", "protocols/", "chip/", "precedent")),
                "op %s cites neither a contract key nor a repo precedent: %s"
                % (item["op"], placement["why"]))
            if placement["where"] == "on_chip":
                self.assertIn(placement["part"], contracts.library())
            if placement["where"] == "missing_part":
                self.assertTrue(placement.get("need"))

    def test_the_file_still_matches_its_schema(self):
        self.assertEqual([], stage_io.problems("unitops", self.result.unitops))

    def test_parts_are_block_ids_and_never_excluded(self):
        for item in self.result.unitops["ops"]:
            part = item["placement"].get("part")
            if part:
                self.assertIn(part, contracts.library())
                self.assertNotEqual("tesla_valve", part)

    def test_retention_story_is_told_honestly(self):
        # RT in the trap is a weir, and it says so.
        rt = self.result.unitops["ops"][8]["placement"]
        self.assertEqual("trap_chamber", rt["part"])
        self.assertIn("DEPTH weir", rt["why"])
        self.assertIn("weir_gap_untested", self.result.summary["flags"])
        # The loose-cell wash is a sedimentation chamber and is labelled the weakest link.
        wash = self.result.unitops["ops"][2]["placement"]
        self.assertEqual("polygon_chamber", wash["part"])
        self.assertIn("SEDIMENTATION", wash["why"])
        self.assertIn("WEAKEST SUBSTITUTION", wash["note"])
        self.assertIn("weakest_substitution_loose_cell_wash", self.result.summary["flags"])
        # And it never claims a weir holds a cell.
        self.assertIn("CANNOT hold", wash["why"])

    def test_thermocycling_names_the_material_and_the_dock(self):
        pcr = self.result.unitops["ops"][11]["placement"]
        self.assertEqual("on_chip", pcr["where"])
        self.assertEqual("polygon_chamber", pcr["part"])
        for qualified in facts.high_tg_materials():
            self.assertIn(qualified, pcr["why"])
        for banned in facts.disqualified_for_pcr():
            self.assertIn(banned, pcr["why"])
        self.assertIn("Peltier dock", pcr["why"])
        self.assertIn("live cells", pcr["why"])
        self.assertIn("needs_peltier_dock", self.result.summary["flags"])
        self.assertIn("cut_from_live_cells", self.result.summary["flags"])

    def test_counts_and_the_parts_the_library_lacks(self):
        counts = self.result.summary["counts"]
        self.assertEqual(len(self.result.unitops["ops"]),
                         sum(counts.values()))
        self.assertGreater(counts["on_chip"], 0)
        self.assertGreater(counts["off_chip"], 0)
        self.assertEqual(1, counts["missing_part"])  # op 15, mechanism not stated
        self.assertTrue(self.result.summary["library_lacks"])
        self.assertTrue(self.result.summary["off_chip_kits"])

    def test_gaps_and_flags_reach_unresolved(self):
        questions = " ".join(u["question"] for u in self.result.unitops["unresolved"])
        self.assertIn("coupon", questions)          # the untested weir gap
        self.assertIn("oxygen", questions)          # the culture budget

    def test_it_is_reproducible(self):
        again = engine.triage(mvp_unitops())
        self.assertEqual(json.dumps(self.result.unitops, sort_keys=True),
                         json.dumps(again.unitops, sort_keys=True))

    def test_nothing_was_asked_of_a_model(self):
        """The MVP pipeline is decided entirely by rules: no tie, no call."""
        self.assertEqual([], self.result.summary["tiebreaks"])
        self.assertNotIn("llm", self.result.unitops)

    def test_disagreements_with_the_repo_are_reported(self):
        reported = {d["rule"] for d in self.result.summary["disagreements"]}
        self.assertIn("retain.cell_sedimentation", reported)
        for entry in self.result.summary["disagreements"]:
            self.assertTrue(entry["who_is_wrong"])


class HardStops(unittest.TestCase):
    def triage_one(self, item):
        payload = {"id": "one_op", "placeholder": True, "ops": [item], "unresolved": []}
        return engine.triage(payload)

    def test_live_cells_never_go_above_37c(self):
        result = self.triage_one(
            op(1, "incubate", "hold the cells at 50 C", particles="cells",
               diameter_um=CELL_UM, temp_c=50.0, duration_s=600.0))
        placement = result.unitops["ops"][0]["placement"]
        self.assertEqual("off_chip", placement["where"])
        self.assertIn("culture.temp_c", placement["why"])
        self.assertIn("live_cells_above_culture_temp", result.summary["flags"])

    def test_thermocycling_a_board_of_live_cells_is_refused(self):
        result = self.triage_one(
            op(1, "thermocycle", "cycle the chamber to 98 C", particles="cells",
               diameter_um=CELL_UM, temp_c=98.0))
        self.assertEqual("off_chip", result.unitops["ops"][0]["placement"]["where"])
        self.assertIn("live_cells_above_culture_temp", result.summary["flags"])

    def test_label_sorting_flags_the_engineered_line(self):
        result = self.triage_one(
            op(1, "separate", "sort the labelled population on the flow sorter",
               particles="cells", diameter_um=CELL_UM))
        placement = result.unitops["ops"][0]["placement"]
        self.assertEqual("off_chip", placement["where"])
        self.assertIn("wild-type", placement["why"])
        self.assertIn("needs_engineered_cell_line", result.summary["flags"])

    def test_chlorinated_solvent_is_excluded(self):
        result = self.triage_one(
            op(1, "mix", "quench the cells in cold chloroform-methanol",
               viscosity_class="organic"))
        placement = result.unitops["ops"][0]["placement"]
        self.assertEqual("off_chip", placement["where"])
        self.assertIn("excluded_solvent", result.summary["flags"])

    def test_a_tiny_capsule_has_no_weir_and_says_what_it_needs(self):
        result = self.triage_one(
            op(1, "wash", "flush the bed", particles="capsules", diameter_um=20.0,
               shear_class="capsules"))
        placement = result.unitops["ops"][0]["placement"]
        self.assertEqual("missing_part", placement["where"])
        self.assertIn("mesh", placement["need"])
        self.assertIn("capsule.diameter_tunable_um", placement["need"])

    def test_a_mis_scoped_shear_class_is_flagged_not_obeyed(self):
        result = self.triage_one(
            op(1, "wash", "flush the capsule bed", particles="capsules",
               diameter_um=contracts.capsule_diameter_um(), shear_class="adherent_cells"))
        self.assertIn("shear_class_mis_scoped", result.summary["flags"])
        note = result.unitops["ops"][0]["placement"]["note"]
        self.assertIn("escape_pressure", note)

    def test_an_unknown_mechanism_becomes_a_missing_part(self):
        result = self.triage_one(op(1, "separate", "separate the fractions somehow"))
        placement = result.unitops["ops"][0]["placement"]
        self.assertEqual("missing_part", placement["where"])
        self.assertTrue(placement["need"])

    def test_a_low_confidence_op_says_so_in_its_why(self):
        result = self.triage_one(
            op(1, "wash", "flush the capsule bed", particles="capsules",
               diameter_um=contracts.capsule_diameter_um(), shear_class="capsules",
               confidence=0.3))
        placement = result.unitops["ops"][0]["placement"]
        self.assertIn("LOW-CONFIDENCE", placement["why"])
        self.assertLessEqual(placement["confidence"], 0.3)
        self.assertIn("low_confidence_op", result.summary["flags"])


class TieBreak(unittest.TestCase):
    """The one place a model is allowed, and the one place it is not."""

    def setUp(self):
        llm.configure("stub")
        llm.reset()

    def test_a_wording_tie_is_broken_offline_and_recorded(self):
        payload = {
            "id": "tie", "placeholder": True, "unresolved": [],
            "ops": [op(1, "image", "count the GFP reporter cells by fluorescence",
                       particles="cells", diameter_um=CELL_UM)],
        }
        result = engine.triage(payload)
        placement = result.unitops["ops"][0]["placement"]
        self.assertEqual(1, len(result.summary["tiebreaks"]))
        note = result.summary["tiebreaks"][0]
        self.assertEqual("linguistic_tie", note["kind"])
        self.assertEqual("bio.engineered_line", note["chose"])
        self.assertEqual("qc.optical_window", note["over"])
        # The conservative reading wins: a reporter read needs a line we do not have.
        self.assertEqual("off_chip", placement["where"])
        self.assertIn("resolved_by_tiebreak", result.summary["flags"])
        self.assertEqual(0.5, placement["confidence"])
        self.assertEqual("stub", result.unitops["llm"]["mode"])

    def test_label_free_counting_alone_stays_on_chip(self):
        payload = {
            "id": "qc", "placeholder": True, "unresolved": [],
            "ops": [op(1, "image", "count the cells in brightfield", particles="cells",
                       diameter_um=CELL_UM)],
        }
        result = engine.triage(payload)
        placement = result.unitops["ops"][0]["placement"]
        self.assertEqual("on_chip", placement["where"])
        self.assertEqual("polygon_chamber", placement["part"])
        self.assertEqual([], result.summary["tiebreaks"])

    def test_a_physics_tie_is_never_put_to_a_model(self):
        """Two physics rules at one rank: resolved by id, flagged, model untouched."""
        table = list(rules.RULES)
        clash = rules.Rule(
            "zz.physics_clash", 50, rules.CONTRACT, True,
            lambda f: rules.Verdict(rules.ON, "a test rule (contracts/blocks.json)",
                                    part="polygon_chamber"),
            "a deliberately clashing physics rule")
        before = llm.provenance()["calls"]
        result = engine.triage(
            {"id": "clash", "placeholder": True, "unresolved": [],
             "ops": [op(1, "wash", "flush the capsule bed", particles="capsules",
                        diameter_um=contracts.capsule_diameter_um(),
                        shear_class="capsules")]},
            rule_table=table + [clash])
        self.assertEqual(before, llm.provenance()["calls"], "a physics tie reached the model")
        self.assertEqual("physics_tie", result.summary["tiebreaks"][0]["kind"])
        self.assertIn("physics_tie_not_arbitrated", result.summary["flags"])
        self.assertEqual("retain.capsule_weir", result.summary["tiebreaks"][0]["chose"])


class ReadsTheContract(unittest.TestCase):
    """Proof that no contract number is written into S3: move it, and S3 moves."""

    def setUp(self):
        self.blocks = copy.deepcopy(contracts.blocks())
        self.parameters = copy.deepcopy(contracts.parameters())

    def tearDown(self):
        contracts._cache["blocks"] = self.blocks
        contracts._cache["parameters"] = self.parameters

    def triage_one(self, item):
        return engine.triage({"id": "one_op", "placeholder": True, "ops": [item],
                              "unresolved": []})

    def test_lowering_the_in_plane_floor_brings_encapsulation_on_chip(self):
        patched = copy.deepcopy(self.blocks)
        nozzle = contracts.param("encapsulation.nozzle_um")
        patched["fabrication_floors"]["min_open_channel_mm"] = (nozzle / 2.0) / 1000.0
        contracts._cache["blocks"] = patched
        result = self.triage_one(
            op(1, "encapsulate", "cut droplets at a flow-focusing nozzle",
               particles="capsules", diameter_um=contracts.capsule_diameter_um(),
               viscosity_class="oil"))
        placement = result.unitops["ops"][0]["placement"]
        self.assertEqual("on_chip", placement["where"])
        self.assertEqual("flow_focusing", placement["part"])

    def test_narrowing_the_gap_band_takes_the_weir_away(self):
        patched = copy.deepcopy(self.parameters)
        patched["trap.gap_factor_high"]["value"] = 0.05
        patched["trap.gap_factor_low"]["value"] = 0.04
        contracts._cache["parameters"] = patched
        result = self.triage_one(
            op(1, "wash", "flush the capsule bed", particles="capsules",
               diameter_um=contracts.capsule_diameter_um(), shear_class="capsules"))
        self.assertEqual("missing_part", result.unitops["ops"][0]["placement"]["where"])

    def test_the_culture_temperature_comes_from_the_contract(self):
        patched = copy.deepcopy(self.parameters)
        patched["culture.temp_c"]["value"] = 55.0
        contracts._cache["parameters"] = patched
        result = self.triage_one(
            op(1, "incubate", "hold the cells warm", particles="cells", diameter_um=CELL_UM,
               temp_c=50.0, duration_s=600.0))
        # 50 C is now below the contract's culture temperature, so it is no longer a stop.
        self.assertNotEqual("off_chip", result.unitops["ops"][0]["placement"]["where"])


class StageEntryPoint(unittest.TestCase):
    def test_run_accepts_an_in_memory_payload_and_writes_nothing(self):
        result = s3_triage.run(unitops=mvp_unitops(), expect=GROUND_TRUTH)
        self.assertEqual([], result.summary["expected_disagreements"])
        self.assertEqual([], s3_triage.WRITES)

    def test_the_rule_table_prints(self):
        text = s3_triage.rule_table()
        self.assertIn("fab.droplet_generation", text)
        self.assertIn("retain.capsule_weir", text)

    def test_the_report_names_the_gaps(self):
        result = s3_triage.run(unitops=mvp_unitops())
        text = result.report()
        self.assertIn("PARTS THE LIBRARY LACKS", text)
        self.assertIn("DELIBERATE DISAGREEMENTS WITH THE REPO", text)
        self.assertIn("on_chip", text)


#: Where triage and the repo differ on the MVP fixture, and who is wrong. Asserted so the
#: list cannot rot silently: if a decision changes, this test fails and someone decides.
KNOWN_DISAGREEMENTS = {
    3: {
        "repo": "protocols/pipeline_status.md step 2b keeps the spin, strainer and "
                "resuspension OFF CHIP in a tube",
        "triage": "on_chip in a sedimentation chamber (polygon_chamber), flagged the weakest "
                  "substitution in the pipeline",
        "who_is_wrong": "neither, and the flag is the answer. contracts/blocks.json "
                        "retention.alternatives_ranked does list a sedimentation sump as "
                        "buildable, so triage is allowed to place it; but "
                        "protocols/retention_audit.md recommends off-chip for two of the "
                        "three real loose-cell steps, so S4 should be free to take the "
                        "flagged op off chip. Triage places it and says it is weak rather "
                        "than silently dropping the step.",
    },
    11: {
        "repo": "chip/module4_v1.json fills four polygon_chamber sublibraries one at a time "
                "through an OFF-CHIP rotary valve",
        "triage": "on_chip metering_trap, the block contracts/blocks.json calls 'the block "
                  "for metered splitting'",
        "who_is_wrong": "the repo, mildly: the rotary valve is a workaround for a chip that "
                        "has no metered split, and metering_trap is the contract's own answer. "
                        "Module 4 also still has no capsule outlet port "
                        "(protocols/pipeline_status.md join 5 -> 6), so that chip is not the "
                        "precedent to copy here.",
    },
}


class Disagreements(unittest.TestCase):
    def test_the_known_disagreements_are_still_exactly_these(self):
        result = engine.triage(mvp_unitops())
        self.assertEqual(("on_chip", "polygon_chamber"), result.where(3))
        self.assertEqual(("on_chip", "metering_trap"), result.where(11))
        for number, entry in KNOWN_DISAGREEMENTS.items():
            self.assertNotIn(number, GROUND_TRUTH,
                             "op %d is both ground truth and a disagreement" % number)
            self.assertTrue(entry["who_is_wrong"])


if __name__ == "__main__":
    unittest.main()

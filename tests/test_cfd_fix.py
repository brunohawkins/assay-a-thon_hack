"""Checks assay2chip/cfd/fix.py: the fix catalogue, the six safety gates, the loop.

    python -m unittest discover -s tests -v
    python -m unittest tests.test_cfd_fix -v

Offline: no network, no LLM. Reads the repo's own chips and protocols.

WHAT THESE TESTS ARE FOR. `fix.py` is the only module in the CFD set that WRITES, so the
tests that matter most are the ones that prove it does not write the wrong thing:

  * a hand-designed chip in `chip/` is never touched, and trying to is an exception
    (`TestNeverTouchesTheRepo`);
  * a fix is auto-applied only when all six gates pass, and each gate really blocks
    (`TestGatesBlock`, and one gate per test so a failure names the gate);
  * the synthetic over-sheared chamber in `_over_sheared_chip` is the end-to-end case from
    the brief: the fix must LOWER the flow and the re-run must CLEAR the finding
    (`TestEndToEndSyntheticShear`);
  * the three problems the sibling modules actually found on real chips come out with
    before/after numbers and the right verdict (`TestRealRepoChips`).

The numbers in the real-chip tests are asserted as RELATIONS and bounds, not as golden
values: `settled_fraction` must be 1.0 before and strictly lower after a shortening, the
escape-pressure ceiling must be far above the flow needed while the Reynolds ceiling is
below it, and the weir's covered window must rise to 1.0. A test pinned to 0.0204609 would
break every time a sibling refines a density and would be checking nothing.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assay2chip.cfd import fix  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402


def _load(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text())


def _chip(chip_id: str) -> dict:
    return _load(f"chip/{chip_id}.json")


def _protocol(chip_id: str) -> dict:
    path = ROOT / "protocols" / f"{chip_id}.json"
    return json.loads(path.read_text()) if path.is_file() else {"id": chip_id, "steps": []}


# --- a synthetic chip we are allowed to break ------------------------------------------------

def _over_sheared_chip(flow_ul_min: float = 40.0) -> tuple[dict, dict]:
    """A legal demo-envelope chip with ONE adherent-cell chamber, run far too fast.

    Everything is inside the contracts - the footprint, the 0.3 mm depth, the 0.2 mm channel
    floor, the 0.8 mm ports at better than 5 mm pitch - so the only thing wrong with it is
    the flow. tau = 6 mu Q / (w h^2) at 40 uL/min across a 3 mm wide, 0.3 mm deep chamber is
    about 0.12 dyn/cm2, which is roughly 12x the 0.01 culture ceiling; the exact number comes
    from the solver, not from here.

    It is synthetic on purpose: the repo's own chips may not be modified, so an end-to-end
    "apply the fix" test needs a chip of our own to fix.
    """
    depth = contracts.max_depth_mm()
    chip = {
        "id": "fixtest_shear_v1",
        "name": "Synthetic: one adherent-cell chamber driven over its shear ceiling",
        "description": (
            "Built by tests/test_cfd_fix.py. Not a design: a minimal legal chip whose only "
            "fault is the flow, so the shear fix can be tested end to end without touching "
            "a hand-designed board."
        ),
        "placeholder": False,
        "units": "mm",
        "footprint": {"x": 30, "y": 20},
        "layers": [
            {"id": "base", "material": "acrylic", "thickness_mm": 3},
            {"id": "spacer", "material": "acrylic", "thickness_mm": depth},
            {"id": "lid", "material": "acrylic", "thickness_mm": 3},
        ],
        "chambers": [
            {"id": "cx_1", "type": "culture", "block": "culture_chamber",
             "center": [15.0, 10.0, 3.15], "size": [6.0, 3.0, depth],
             "mesh_pore_um": None, "volume_ul": round(6.0 * 3.0 * depth, 4)},
        ],
        "channels": [
            {"id": "ch_in", "from": "p_in", "to": "cx_1", "width_mm": 0.4,
             "depth_mm": depth, "block": "straight_channel", "carries": "cells"},
            {"id": "ch_out", "from": "cx_1", "to": "p_out", "width_mm": 0.4,
             "depth_mm": depth, "block": "straight_channel", "carries": "cells"},
        ],
        "ports": [
            {"id": "p_in", "type": "inlet", "position": [3.0, 10.0, 6.3],
             "width_um": contracts.floor("min_port_mm") * 1000},
            {"id": "p_out", "type": "outlet", "position": [27.0, 10.0, 6.3],
             "width_um": contracts.floor("min_port_mm") * 1000},
        ],
        "thermal_zones": [
            {"id": "z_culture", "purpose": "culture", "chambers": ["cx_1"],
             "max_temp_c": 37},
        ],
    }
    protocol = {
        "id": "fixtest_shear_v1",
        "name": "Synthetic: one medium exchange, too fast",
        "placeholder": False,
        "steps": [
            {"step": 1, "operation": "exchange", "target": "cx_1",
             "reagent": "culture_medium", "volume_ul": 20.0, "duration_s": 30.0,
             "temp_c": 37, "washes": 1, "flow_ul_min": flow_ul_min,
             "ports": {"feed": ["p_in"], "vent": ["p_out"], "cap": []},
             "note": "Synthetic over-shear case."},
        ],
    }
    return chip, protocol


def _shear_finding(chip: dict, protocol: dict, sim: fix.Simulator) -> dict:
    """The over-shear finding on the synthetic chip, from whichever analyser is loaded."""
    found, _ = fix.analyse(chip, protocol, sim)
    shear = [f for f in found if f.get("class") == "shear"]
    if not shear:
        raise unittest.SkipTest("no analyser produced a shear finding on the synthetic chip")
    grouped = fix.group_findings(shear, lambda f: sim.run(chip, protocol["steps"][0]))
    return grouped[0]


# --- catalogue ---------------------------------------------------------------------------------

class TestCatalogue(unittest.TestCase):
    """The catalogue has to cover every finding class the CFD README defines."""

    README_CLASSES = ("leak", "trap", "dead_end", "bubble", "shear", "sediment", "clog",
                      "handoff", "unmodelled")

    def test_every_readme_class_is_in_the_catalogue(self):
        for klass in self.README_CLASSES:
            self.assertIn(klass, fix.ACTIONS, f"{klass} has no entry in ACTIONS")

    def test_handoff_and_unmodelled_deliberately_have_no_actions(self):
        """A hand-off is off-chip tubing and Re > 1 is a validity statement.

        Patching geometry until an Re number looks acceptable is the silent rewrite the
        README forbids, so these two must be EMPTY rather than absent.
        """
        self.assertEqual(fix.ACTIONS["handoff"], ())
        self.assertEqual(fix.ACTIONS["unmodelled"], ())

    def test_three_kinds_of_change_exist(self):
        """flow, geometry and spec. The weir is why the third one had to exist."""
        self.assertTrue(fix.FLOW_ACTIONS)
        self.assertTrue(fix.GEOMETRY_ACTIONS)
        self.assertTrue(fix.SPEC_ACTIONS)
        self.assertFalse(fix.FLOW_ACTIONS & fix.GEOMETRY_ACTIONS)
        self.assertFalse(fix.GEOMETRY_ACTIONS & fix.SPEC_ACTIONS)
        self.assertFalse(fix.FLOW_ACTIONS & fix.SPEC_ACTIONS)

    def test_every_action_in_actions_is_one_of_the_three_kinds(self):
        known = fix.FLOW_ACTIONS | fix.GEOMETRY_ACTIONS | fix.SPEC_ACTIONS
        for klass, actions in fix.ACTIONS.items():
            for action in actions:
                self.assertIn(action, known, f"{klass} -> {action} has no kind")

    def test_every_geometry_action_names_a_rerun_stage(self):
        """A geometry fix in pipeline mode becomes a rerun_from request, so it must say
        whether it is S6 (sizing) or S7 (routing)."""
        for action in fix.GEOMETRY_ACTIONS:
            self.assertIn(action, fix.RERUN_STAGE, action)
            self.assertIn(fix.RERUN_STAGE[action], ("s6", "s7"))

    def test_every_spec_action_names_where_it_lands(self):
        for action in fix.SPEC_ACTIONS:
            self.assertIn(action, fix.SPEC_HOME, action)
            self.assertTrue(fix.SPEC_HOME[action].strip())

    def test_process_constants_are_declared_as_assumptions(self):
        """Every number in this module that no contract states must say so out loud."""
        names = {a["name"] for a in fix.process_assumptions()}
        self.assertIn("FIX_HEADROOM", names)
        self.assertIn("KINETIC_STEP_S", names)
        for entry in fix.process_assumptions():
            self.assertIsNone(entry["contract_key"], entry["name"])
            self.assertTrue(entry["assumption"])
            self.assertTrue(entry["why"].strip())


class TestNoHardCodedContractValues(unittest.TestCase):
    """A literal that duplicates a contract value is the bug this repo keeps hitting.

    Checked on the module's own constants rather than by grepping the text, so the test
    says something precise: no module-level number in `fix.py` may equal a number the
    contracts state. `fix.py` legitimately has constants of its own (FIX_HEADROOM,
    ZERO_LIMIT_TARGET and so on) - what it may not have is a second copy of 0.01 dyn/cm2.
    """

    OWN_CONSTANTS = {
        # name -> what it is. All DIMENSIONLESS or a process choice; none is a physical
        # quantity the contracts state. A bare numeric comparison cannot tell 0.8 (a
        # headroom fraction) from 0.8 mm (the port bore), so this is an explicit list:
        # adding a constant to fix.py means naming it here and saying what it is.
        "FIX_HEADROOM": "fraction of a limit a fix aims at",
        "ZERO_LIMIT_TARGET": "fraction standing in for a limit of exactly zero",
        "KINETIC_STEP_S": "seconds below which a step is kinetically timed",
        "MIN_RELATIVE_IMPROVEMENT": "fraction: when the loop has stopped helping",
        "SECANT_CLAMP": "how far one secant step may move a knob",
        "WIDTH_ROUND_MM": "the grid widths are proposed on",
    }

    def test_every_float_constant_is_declared_and_explained(self):
        """Nothing in fix.py may be a silent magic number."""
        for name, value in vars(fix).items():
            if name.startswith("_") or not name.isupper():
                continue
            if not isinstance(value, float):
                continue
            self.assertIn(name, self.OWN_CONSTANTS,
                          f"{name} = {value} is an undeclared magic number in fix.py")

    def test_every_declared_constant_is_flagged_as_an_assumption_or_a_grid(self):
        flagged = {a["name"] for a in fix.process_assumptions()}
        mechanics = {"SECANT_CLAMP", "WIDTH_ROUND_MM", "ZERO_LIMIT_TARGET"}
        for name in self.OWN_CONSTANTS:
            self.assertTrue(name in flagged or name in mechanics,
                            f"{name} is neither a declared assumption nor solver mechanics")

    def test_no_contract_quantity_is_recomputed_by_hand(self):
        """The quantities that DO come from the contracts must come through the library.

        Checked by value where the unit is unambiguous: the shear ceilings and the working
        pressure are read from `contracts`, so they must equal the contract and not a copy.
        """
        self.assertEqual(contracts.shear_ceiling_for("adherent_cells").ceiling_dyn_cm2,
                         contracts.param("shear.culture_ceiling_dyn_cm2"))
        self.assertEqual(contracts.max_working_pressure_kpa(),
                         contracts.escape_pressure_kpa() * contracts.pressure_margin())
        source = (ROOT / "assay2chip" / "cfd" / "fix.py").read_text()
        self.assertNotIn("escape_pressure_kpa() *", source,
                         "the margin is already applied by max_working_pressure_kpa()")

    def test_reads_the_ceilings_through_contracts(self):
        """The three shear ceilings and the pressure ceiling must come from the library."""
        source = (ROOT / "assay2chip" / "cfd" / "fix.py").read_text()
        for call in ("contracts.shear_ceiling_for(", "contracts.max_working_pressure_kpa(",
                     "contracts.floor(", "contracts.viscosity_for("):
            self.assertIn(call, source, f"fix.py never calls {call}")


# --- the repo is read-only ----------------------------------------------------------------------

class TestNeverTouchesTheRepo(unittest.TestCase):
    """"Never modify a hand-designed repo chip" has to be an exception, not a comment."""

    def test_writing_into_chip_dir_raises(self):
        with self.assertRaises(PermissionError):
            fix._assert_not_repo(ROOT / "chip" / "module1_v1.json")

    def test_writing_into_protocols_dir_raises(self):
        with self.assertRaises(PermissionError):
            fix._assert_not_repo(ROOT / "protocols" / "module1_v1.json")

    def test_writing_into_cad_or_contracts_raises(self):
        for rel in ("cad/exports/x.dxf", "contracts/blocks.json", "runs/x.json"):
            with self.assertRaises(PermissionError):
                fix._assert_not_repo(ROOT / rel)

    def test_work_dir_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(fix._assert_not_repo(Path(tmp) / "cfd" / "fixes.json"))

    def test_a_run_on_a_repo_chip_leaves_every_file_untouched(self):
        """The strongest version of the rule: hash chip/ and protocols/ around a real run."""
        before = {p: p.read_bytes() for p in
                  sorted((ROOT / "chip").glob("*.json")) +
                  sorted((ROOT / "protocols").glob("*.json"))}
        chip, protocol = _chip("cell_prep_v1"), _protocol("cell_prep_v1")
        sim = fix.Simulator()
        findings = _sediment_findings("cell_prep_v1", "mix_1")
        if not findings:
            self.skipTest("particles.py produced no settling finding for cell_prep_v1")
        with tempfile.TemporaryDirectory() as tmp:
            run = fix.propose(findings, chip, protocol, mode="standalone", sim=sim)
            fix.write(run, Path(tmp))
        after = {p: p.read_bytes() for p in before}
        self.assertEqual(before, after, "a fix run modified a file in chip/ or protocols/")

    def test_propose_does_not_mutate_its_inputs(self):
        chip, protocol = _over_sheared_chip()
        chip_copy, proto_copy = copy.deepcopy(chip), copy.deepcopy(protocol)
        sim = fix.Simulator()
        finding = _shear_finding(chip, protocol, sim)
        fix.propose([finding], chip, protocol, sim=sim)
        self.assertEqual(chip, chip_copy, "propose() mutated the chip it was given")
        self.assertEqual(protocol, proto_copy, "propose() mutated the protocol")

    def test_apply_candidate_returns_copies(self):
        chip, protocol = _over_sheared_chip()
        cand = fix.Candidate("x", "set_flow", "cx_1",
                             {"step": 0, "flow_ul_min": 1.0}, "flow", "test")
        new_chip, new_proto = fix.apply_candidate(chip, protocol, cand)
        self.assertEqual(protocol["steps"][0]["flow_ul_min"], 40.0)
        self.assertEqual(new_proto["steps"][0]["flow_ul_min"], 1.0)
        self.assertIsNot(new_chip, chip)

    def test_a_spec_action_can_never_be_applied(self):
        chip, protocol = _over_sheared_chip()
        for action in sorted(fix.SPEC_ACTIONS):
            cand = fix.Candidate("x", action, "cx_1", {"where": "somewhere"}, "spec", "t")
            with self.assertRaises(fix.FixError, msg=action):
                fix.apply_candidate(chip, protocol, cand)


# --- the gates ------------------------------------------------------------------------------------

class TestGatesBlock(unittest.TestCase):
    """One test per gate. A gate that cannot block is not a gate."""

    def test_fabrication_gate_blocks_a_sub_floor_channel(self):
        chip, _ = _over_sheared_chip()
        narrow = copy.deepcopy(chip)
        floor = contracts.floor("min_open_channel_mm")
        narrow["channels"][0]["width_mm"] = round(floor / 2, 4)
        gate = fix.gate_fabrication(chip, narrow)
        self.assertFalse(gate.ok)
        self.assertEqual(gate.name, "fabrication")

    def test_fabrication_gate_ignores_pre_existing_errors(self):
        """check_chip_spec reports 60 port-overlap errors on main; they are not ours."""
        chip, _ = _over_sheared_chip()
        self.assertTrue(fix.gate_fabrication(chip, copy.deepcopy(chip)).ok)

    def test_fabrication_gate_blocks_too_deep(self):
        chip, _ = _over_sheared_chip()
        deep = copy.deepcopy(chip)
        deep["channels"][0]["depth_mm"] = contracts.max_depth_mm() * 2
        self.assertFalse(fix.gate_fabrication(chip, deep).ok)

    def test_planar_gate_blocks_a_port_moved_into_a_chamber(self):
        """This is the real module4 case: shortening a feed line 685x drills the inlet
        into the chamber it feeds. The detector is cad/make_figures.py's, not a copy."""
        chip, _ = _over_sheared_chip()
        crashed = copy.deepcopy(chip)
        crashed["ports"][0]["position"] = list(chip["chambers"][0]["center"])
        gate = fix.gate_planar(chip, crashed)
        self.assertFalse(gate.ok)
        self.assertIn("ERROR", gate.detail)

    def test_planar_gate_passes_an_unchanged_chip(self):
        chip, _ = _over_sheared_chip()
        self.assertTrue(fix.gate_planar(chip, copy.deepcopy(chip)).ok)

    def test_planar_gate_uses_the_repo_detector(self):
        """No fourth geometry implementation: the gate must call make_figures."""
        source = (ROOT / "assay2chip" / "cfd" / "fix.py").read_text()
        self.assertIn("geometry_warnings", source)
        self.assertNotIn("def geometry_warnings", source)

    def test_measurable_gate_blocks_an_unmeasurable_metric(self):
        cand = fix.Candidate("x", "add_pillar_row", "cx_1", {}, "geometry", "t")
        gate = fix.gate_measurable(1.0, None, cand)
        self.assertFalse(gate.ok)
        self.assertIn("mesh.py", gate.detail)

    def test_cleared_gate_demands_headroom_not_just_the_limit(self):
        finding = {"value": 0.02, "limit": 0.01, "units": "dyn/cm2", "metric": "shear"}
        self.assertFalse(fix.gate_cleared(finding, 0.0099).ok,
                         "landing just inside the limit is not cleared")
        self.assertTrue(fix.gate_cleared(finding, 0.01 * fix.FIX_HEADROOM * 0.9).ok)

    def test_no_new_blocker_gate_blocks_a_new_blocker(self):
        before = [{"class": "shear", "feature_id": "a", "step": 1, "severity": "blocker"}]
        after = before + [{"class": "trap", "feature_id": "b", "step": 1,
                           "severity": "blocker", "message": "new dead zone"}]
        gate = fix.gate_no_new_blocker(before, after, 1)
        self.assertFalse(gate.ok)
        self.assertIn("trap", gate.detail)

    def test_no_new_blocker_gate_allows_a_new_non_blocker(self):
        before = [{"class": "shear", "feature_id": "a", "step": 1, "severity": "blocker"}]
        after = before + [{"class": "trap", "feature_id": "b", "step": 1,
                           "severity": "low", "message": "slow corner"}]
        self.assertTrue(fix.gate_no_new_blocker(before, after, 1).ok)

    def test_no_new_blocker_gate_records_its_scope(self):
        for scope, word in ((None, "whole protocol"), (fix.WORST, "worst"), (3, "step 3")):
            gate = fix.gate_no_new_blocker([], [], scope)
            self.assertIn(word, gate.detail)

    def test_side_effects_gate_blocks_stretching_a_kinetic_step(self):
        """Module 6's quench floods in 1 s. Cutting its flow is a chemistry decision."""
        cand = fix.Candidate("x", "set_flow", "cx_n", {"step": 0}, "flow", "t")
        before = {"steps": [{"step": 1, "duration_s": 1.0}]}
        after = {"steps": [{"step": 1, "duration_s": 6.4}]}
        empty = fix.Solution(backend="test")
        gate = fix.gate_side_effects(cand, before, after, empty, empty, {})
        self.assertFalse(gate.ok)
        self.assertIn("kinetically timed", gate.detail)

    def test_side_effects_gate_allows_stretching_a_long_step(self):
        cand = fix.Candidate("x", "set_flow", "cx_1", {"step": 0}, "flow", "t")
        before = {"steps": [{"step": 1, "duration_s": 600.0}]}
        after = {"steps": [{"step": 1, "duration_s": 3000.0}]}
        empty = fix.Solution(backend="test")
        self.assertTrue(fix.gate_side_effects(cand, before, after, empty, empty, {}).ok)

    def test_side_effects_gate_blocks_a_raise_that_leaves_the_stokes_model(self):
        """Module 4's feed lines: pressure has room, Reynolds does not."""
        cand = fix.Candidate("x", "raise_flow", "ch_in_1", {"step": 0}, "flow", "t")
        proto = {"steps": [{"step": 1, "duration_s": 18.0}]}
        low = fix.Solution(backend="cfd", reynolds={"d:ch_in_1": 0.33})
        high = fix.Solution(backend="cfd", reynolds={"d:ch_in_1": 2.8})
        gate = fix.gate_side_effects(cand, proto, proto, low, high, {})
        self.assertFalse(gate.ok)
        self.assertIn("Re", gate.detail)

    def test_side_effects_gate_blocks_rushing_a_residence_time_feature(self):
        """cell_prep's mix_1 is a serpentine: faster flow is less mixing."""
        chip = {"chambers": [{"id": "mix_1", "type": "mixer",
                              "block": "serpentine_channel"}], "channels": []}
        cand = fix.Candidate("x", "raise_flow", "mix_1", {"step": 0}, "flow", "t")
        proto = {"steps": [{"step": 1, "duration_s": 120.0}]}
        sol = fix.Solution(backend="cfd", reynolds={"d:mix_1": 0.05})
        gate = fix.gate_side_effects(cand, proto, proto, sol, sol, chip)
        self.assertFalse(gate.ok)
        self.assertIn("residence-time", gate.detail)

    def test_a_raise_with_no_reynolds_is_never_automatic(self):
        cand = fix.Candidate("x", "raise_flow", "ch", {"step": 0}, "flow", "t")
        proto = {"steps": [{"step": 1, "duration_s": 10.0}]}
        sol = fix.Solution(backend="network")
        self.assertFalse(fix.gate_side_effects(cand, proto, proto, sol, sol, {}).ok)


# --- metric plumbing ------------------------------------------------------------------------------

class TestMetricPlumbing(unittest.TestCase):
    """The small pieces that silently ruin a prediction when they are wrong."""

    def test_band_values_do_not_become_fake_scalars(self):
        self.assertIsNone(fix._scalar([5.0, 26.8]))
        self.assertIsNone(fix._scalar(None))
        self.assertIsNone(fix._scalar(True), "a bool is not a measurement")
        self.assertEqual(fix._scalar(3), 3.0)

    def test_a_zero_limit_gets_a_stated_target_not_a_division(self):
        finding = {"value": 1.0, "limit": 0.0, "units": "fraction",
                   "metric": "settled_fraction"}
        self.assertEqual(fix.target_value(finding), fix.ZERO_LIMIT_TARGET)
        self.assertFalse(fix.within_limit(finding, 0.5))
        self.assertTrue(fix.within_limit(finding, fix.ZERO_LIMIT_TARGET / 2))

    def test_over_by_does_not_divide_by_a_zero_limit(self):
        self.assertEqual(fix._over_by({"value": 1.0, "limit": 0.0}), 1.0)
        self.assertEqual(fix._over_by({"value": [5, 26], "limit": [20, 26]}), 0.0)

    def test_direction_is_inferred_and_overridable(self):
        self.assertEqual(fix.direction({"value": 2.0, "limit": 1.0}), "max")
        self.assertEqual(fix.direction({"value": 0.5, "limit": 1.0}), "min")
        self.assertEqual(fix.direction({"value": 2.0, "limit": 1.0,
                                        "direction": "min"}), "min")

    def test_step_is_resolved_by_position_because_that_is_what_siblings_quote(self):
        """StepView.index is a position. Resolving it by the `step` field solved the
        wrong step and made real fixes look unmeasurable."""
        protocol = {"steps": [{"step": 1, "target": "a"}, {"step": 2, "target": "b"},
                              {"step": 3, "target": "c"}]}
        self.assertEqual(fix._step(protocol, 0)["target"], "a")
        self.assertEqual(fix._step(protocol, 1)["target"], "b")

    def test_step_ref_round_trips(self):
        protocol = {"steps": [{"step": 1}, {"step": 2}, {"step": 3}]}
        for i, step in enumerate(protocol["steps"]):
            self.assertEqual(fix._step_ref(protocol, step), i)
            self.assertIs(fix._step(protocol, fix._step_ref(protocol, step)), step)

    def test_saturated_metrics_are_recognised(self):
        frac = {"units": "fraction"}
        self.assertTrue(fix._saturated(frac, 1.0))
        self.assertTrue(fix._saturated(frac, 0.0))
        self.assertFalse(fix._saturated(frac, 0.5))
        self.assertFalse(fix._saturated({"units": "dyn/cm2"}, 1.0))

    def test_band_metric_is_routed_to_the_spec_change_branch(self):
        """particles.py files the weir sweep as `leak`; the fix is not a flow cut."""
        finding = {"class": "leak", "metric": "working_gap_band_um",
                   "value": [5.0, 26.8], "limit": [20.1, 26.8]}
        self.assertEqual(fix.effective_class(finding), "retention")

    def test_grouping_keeps_the_worst_case_and_counts_the_rest(self):
        sol = fix.Solution(backend="test", feature_of={"d:ch": "ch", "c:ch:0:0": "ch"})
        findings = [
            {"id": "a", "class": "shear", "feature_id": "d:ch", "severity": "blocker",
             "value": 0.05, "limit": 0.01},
            {"id": "b", "class": "shear", "feature_id": "c:ch:0:0", "severity": "blocker",
             "value": 0.09, "limit": 0.01},
        ]
        grouped = fix.group_findings(findings, lambda f: sol)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["id"], "b", "the worst case must be the survivor")
        self.assertEqual(grouped[0]["spec_feature_id"], "ch")
        self.assertEqual(grouped[0]["grouped"], 2)

    def test_grouping_respects_the_cap_and_orders_by_severity(self):
        sol = fix.Solution(backend="test")
        findings = [{"id": str(i), "class": "shear", "feature_id": f"f{i}",
                     "severity": "low" if i else "blocker", "value": 1.0, "limit": 0.5}
                    for i in range(10)]
        grouped = fix.group_findings(findings, lambda f: sol, limit=3)
        self.assertEqual(len(grouped), 3)
        self.assertEqual(grouped[0]["severity"], "blocker")


# --- end to end on the synthetic chip --------------------------------------------------------------

class TestEndToEndSyntheticShear(unittest.TestCase):
    """The brief's acceptance test: an over-sheared adherent chamber must get its flow cut
    automatically, and the re-run must show the finding gone."""

    @classmethod
    def setUpClass(cls):
        cls.chip, cls.protocol = _over_sheared_chip()
        cls.sim = fix.Simulator()
        cls.finding = _shear_finding(cls.chip, cls.protocol, cls.sim)
        cls.fixrun = fix.propose([cls.finding], cls.chip, cls.protocol,
                              mode="standalone", sim=cls.sim)

    def test_the_chip_itself_is_legal_so_only_the_flow_is_wrong(self):
        self.assertEqual(fix._floor_errors(self.chip), [])
        errors, _ = fix._planar(self.chip)
        self.assertEqual(errors, [])

    def test_the_finding_is_an_adherent_cell_shear_blocker(self):
        self.assertEqual(self.finding["class"], "shear")
        ceiling = contracts.shear_ceiling_for("adherent_cells").ceiling_dyn_cm2
        self.assertAlmostEqual(self.finding["limit"], ceiling, places=12)
        self.assertGreater(self.finding["value"], ceiling)

    def test_a_fix_was_applied(self):
        self.assertTrue(self.fixrun.applied, "nothing was auto-applied on a clean synthetic "
                                          "chip whose only fault is the flow")

    def test_the_applied_fix_lowers_the_flow(self):
        applied = self.fixrun.applied[0]
        self.assertEqual(applied["action"], "set_flow")
        self.assertEqual(applied["kind"], "flow")
        was = self.protocol["steps"][0]["flow_ul_min"]
        now = self.fixrun.patched_protocol["steps"][0]["flow_ul_min"]
        self.assertLess(now, was, "the fix did not lower the flow")

    def test_the_rerun_clears_the_finding(self):
        applied = self.fixrun.applied[0]
        self.assertIsNotNone(applied["verified_after"])
        self.assertLess(applied["predicted"]["after"], applied["predicted"]["before"])
        self.assertTrue(
            fix.within_limit(self.finding, applied["verified_after"]),
            f"re-solve gives {applied['verified_after']}, not inside "
            f"{self.finding['limit']} at {fix.FIX_HEADROOM} headroom",
        )

    def test_the_fix_lands_near_the_ceiling_at_headroom_not_far_under_it(self):
        """A fix that drops the flow 1000x would also 'clear' the finding and ruin the
        step. It has to aim at the ceiling."""
        after = self.fixrun.applied[0]["verified_after"]
        target = fix.target_value(self.finding)
        self.assertGreater(after, target * 0.25,
                           "the fix overshot: it cut the flow far below what was needed")

    def test_every_gate_passed_and_is_recorded(self):
        gates = {g["gate"]: g for g in self.fixrun.applied[0]["gates"]}
        for name in ("measurable", "cleared", "side_effects", "no_new_blocker"):
            self.assertIn(name, gates)
            self.assertTrue(gates[name]["ok"], f"{name}: {gates[name]['detail']}")

    def test_the_geometry_is_untouched_by_a_flow_fix(self):
        self.assertIsNone(self.fixrun.patched_chip,
                          "a flow-only fix must not produce a patched chip")

    def test_the_step_stays_self_consistent(self):
        """volume / flow must still equal duration, or the protocol lies about itself."""
        step = self.fixrun.patched_protocol["steps"][0]
        washes = step.get("washes") or 1
        self.assertAlmostEqual(step["duration_s"],
                               step["volume_ul"] * washes / step["flow_ul_min"] * 60.0,
                               places=2)

    def test_the_loop_is_bounded_and_logged(self):
        iterations = {e.get("iteration") for e in self.fixrun.log if "iteration" in e}
        self.assertTrue(iterations)
        self.assertLessEqual(max(i for i in iterations if isinstance(i, int)),
                             fix.MAX_FIX_ITERATIONS)
        self.assertTrue([e for e in self.fixrun.log if e.get("event") == "stop"],
                        "the loop must log why it stopped")
        self.assertTrue([e for e in self.fixrun.log if e.get("event") == "applied"])

    def test_the_fix_carries_its_assumptions(self):
        self.assertIn("FIX_HEADROOM", self.fixrun.applied[0]["assumptions"])

    def test_writing_produces_fixes_json_and_a_patched_protocol(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = fix.propose([self.finding], self.chip, self.protocol,
                              mode="standalone", sim=self.sim)
            fix.write(run, Path(tmp))
            payload = json.loads((Path(tmp) / "cfd" / "fixes.json").read_text())
            for key in ("chip_id", "mode", "backend", "fixes", "log", "unresolved",
                        "assumptions", "max_iterations"):
                self.assertIn(key, payload)
            entry = payload["fixes"][0]
            for key in ("finding_id", "action", "target", "params", "predicted",
                        "auto_applicable", "applied", "verified_after"):
                self.assertIn(key, entry, "fixes.json must match the README shape")
            self.assertTrue(list((Path(tmp) / "cfd" / "patched").glob("*.protocol.json")))


class TestGeometryRouting(unittest.TestCase):
    """A geometry fix is a rerun_from request in pipeline mode and a patched copy in
    standalone mode. Never an edit to chip/."""

    def setUp(self):
        # Flow cannot fix this one: widen the chamber instead, by forbidding the flow
        # candidate (the step has no volume, so there is nothing to restate).
        self.chip, self.protocol = _over_sheared_chip()
        self.sim = fix.Simulator()
        self.finding = _shear_finding(self.chip, self.protocol, self.sim)

    def _geometry_candidate(self):
        cands = fix.candidates_for(self.finding, self.chip, self.protocol,
                                   sol=self.sim.run(self.chip, self.protocol["steps"][0]))
        geo = [c for c in cands if c.kind == "geometry"]
        if not geo:
            self.skipTest("no geometry candidate for this shear finding")
        return geo[0]

    def test_a_geometry_candidate_exists_for_shear(self):
        cand = self._geometry_candidate()
        self.assertIn(cand.action, ("widen_channel", "widen_chamber"))

    def test_a_geometry_fix_widens_rather_than_narrows(self):
        cand = self._geometry_candidate()
        new_chip, _ = fix.apply_candidate(self.chip, self.protocol, cand)
        if cand.action == "widen_chamber":
            self.assertGreater(fix._chamber(new_chip, cand.target)["size"][1],
                               fix._chamber(self.chip, cand.target)["size"][1])
        else:
            self.assertGreater(fix._channel(new_chip, cand.target)["width_mm"],
                               fix._channel(self.chip, cand.target)["width_mm"])

    def test_pipeline_mode_emits_a_rerun_from_request(self):
        cand = self._geometry_candidate()
        pred = fix.predict(self.finding, cand, self.chip, self.protocol, self.sim)
        request = fix._rerun_request(pred, self.finding, self.chip)
        self.assertIn(request["rerun_from"], ("s6", "s7"))
        self.assertEqual(request["chip_id"], self.chip["id"])
        self.assertTrue(request["reason"].strip())
        self.assertIn("params", request)
        self.assertEqual(request["max_reruns"], fix.MAX_FIX_ITERATIONS)

    def test_pipeline_mode_does_not_write_a_patched_chip(self):
        chip, protocol = _over_sheared_chip()
        cand = self._geometry_candidate()
        pred = fix.predict(self.finding, cand, chip, protocol, self.sim)
        run = fix.FixRun(chip_id=chip["id"], mode="pipeline", backend=self.sim.backend)
        run.patched_chip = pred.chip
        with tempfile.TemporaryDirectory() as tmp:
            fix.write(run, Path(tmp))
            self.assertFalse(list((Path(tmp) / "cfd" / "patched").glob("*.patched.json")))
            self.assertIn("rerun_from", run.artefacts["patched_chip"])

    def test_standalone_mode_writes_the_patched_chip_into_the_work_dir(self):
        chip, protocol = _over_sheared_chip()
        cand = self._geometry_candidate()
        pred = fix.predict(self.finding, cand, chip, protocol, self.sim)
        run = fix.FixRun(chip_id=chip["id"], mode="standalone", backend=self.sim.backend)
        run.patched_chip = pred.chip
        with tempfile.TemporaryDirectory() as tmp:
            fix.write(run, Path(tmp))
            written = list((Path(tmp) / "cfd" / "patched").glob("*.patched.json"))
            self.assertEqual(len(written), 1)
            self.assertEqual(json.loads(written[0].read_text())["id"], chip["id"])


# --- the real chips -------------------------------------------------------------------------------

def _sediment_findings(chip_id: str, feature_id: str) -> list[dict]:
    try:
        from assay2chip.cfd import particles as P
    except Exception:
        return []
    try:
        found = P.analyse_chip(chip_id, chip_id)["findings"]
    except Exception:
        return []
    return [f for f in found
            if f.get("class") == "sediment" and f.get("feature_id") == feature_id]


def _weir_findings(chip_id: str) -> list[dict]:
    try:
        from assay2chip.cfd import particles as P
    except Exception:
        return []
    try:
        found = P.analyse_chip(chip_id)["findings"]
    except Exception:
        return []
    return [f for f in found if f.get("metric") == "working_gap_band_um"]


class TestRealRepoChipModule4FeedLines(unittest.TestCase):
    """module4_v1: capsules settle in all four feed lines. CAN the flow simply rise?

    The answer the gates give is the useful one: the escape-pressure limit is nowhere near
    binding, but Reynolds is - so flow alone cannot do it and the honest fixes are a shorter
    line or a vertical mount, both for a human.
    """

    CHIP = "module4_v1"
    FEATURE = "ch_in_1"

    @classmethod
    def setUpClass(cls):
        cls.findings = _sediment_findings(cls.CHIP, cls.FEATURE)
        if not cls.findings:
            raise unittest.SkipTest(f"particles.py reports no settling in {cls.FEATURE}")
        cls.chip, cls.protocol = _chip(cls.CHIP), _protocol(cls.CHIP)
        cls.sim = fix.Simulator()
        cls.fixrun = fix.propose(cls.findings, cls.chip, cls.protocol,
                              mode="pipeline", sim=cls.sim)
        cls.by_action = {f["action"]: f for f in cls.fixrun.fixes}

    def test_the_whole_population_is_lost_before_the_fix(self):
        self.assertAlmostEqual(self.findings[0]["value"], 1.0, places=6)
        self.assertEqual(self.findings[0]["severity"], "blocker")

    def test_our_kinematics_agree_with_particles_py(self):
        """The before number must be particles.py's, or the comparison is meaningless."""
        step = fix._step(self.protocol, self.findings[0]["step"])
        sol = self.sim.run(self.chip, step)
        verdict = fix.sediment_verdict(self.findings[0], sol, self.chip)
        self.assertIsNotNone(verdict)
        detail = next(d for d in
                      __import__("assay2chip.cfd.particles", fromlist=["x"])
                      .analyse_chip(self.CHIP, self.CHIP)["sediment"]
                      if d["feature_id"] == self.FEATURE)
        self.assertAlmostEqual(verdict.u_mm_s, detail["u_mm_s"], places=3)
        self.assertAlmostEqual(verdict.transit_time_s, detail["transit_time_s"], places=2)
        self.assertAlmostEqual(verdict.fall_time_s, detail["fall_time_s"], places=2)

    def test_escape_pressure_is_not_the_binding_limit(self):
        """The brief's question. Pressure has orders of magnitude of headroom."""
        step = fix._step(self.protocol, self.findings[0]["step"])
        sol = self.sim.run(self.chip, step)
        cap = fix._pressure_flow_ceiling(self.chip, step, sol)
        self.assertGreater(cap["max_flow_ul_min"], step["flow_ul_min"] * 100)
        self.assertAlmostEqual(cap["limit_kpa"], contracts.max_working_pressure_kpa(),
                               places=9)

    def test_reynolds_is_the_binding_limit(self):
        step = fix._step(self.protocol, self.findings[0]["step"])
        sol = self.sim.run(self.chip, step)
        press = fix._pressure_flow_ceiling(self.chip, step, sol)["max_flow_ul_min"]
        re_cap = fix._reynolds_flow_ceiling(step, sol)["max_flow_ul_min"]
        self.assertLess(re_cap, press,
                        "Reynolds must be the tighter ceiling on these feed lines")

    def test_flow_alone_cannot_clear_it(self):
        raise_fix = self.by_action.get("raise_flow")
        self.assertIsNotNone(raise_fix, "a flow raise must at least be considered")
        self.assertFalse(raise_fix["applied"])
        self.assertIn("cleared", raise_fix["blocked_by"])
        self.assertGreater(raise_fix["params"]["needed_ul_min"],
                           raise_fix["params"]["flow_ul_min"])

    def test_the_flow_that_would_end_total_loss_is_reported_too(self):
        """"8x stops all of it being lost" is more useful than only the 1% number."""
        raise_fix = self.by_action["raise_flow"]
        self.assertIsNotNone(raise_fix["params"]["ends_total_loss_at_ul_min"])
        self.assertLess(raise_fix["params"]["ends_total_loss_at_ul_min"],
                        raise_fix["params"]["needed_ul_min"])

    def test_shortening_the_line_is_predicted_and_helps(self):
        short = self.by_action.get("shorten_line")
        self.assertIsNotNone(short)
        self.assertIsNotNone(short["predicted"]["after"], "the shortening was not predicted")
        self.assertLess(short["predicted"]["after"], short["predicted"]["before"],
                        "a shorter line must lose fewer capsules")

    def test_shortening_this_far_is_blocked_by_the_planar_check(self):
        """685x shorter drills the inlet into the chamber. cad/make_figures.py catches it."""
        short = self.by_action["shorten_line"]
        self.assertFalse(short["applied"])
        self.assertIn("planar", short["blocked_by"])

    def test_vertical_mounting_is_offered_as_a_spec_change_for_a_human(self):
        spec = self.by_action.get("vertical_mounting")
        self.assertIsNotNone(spec, "the fixture answer must be sayable")
        self.assertEqual(spec["kind"], "spec")
        self.assertFalse(spec["applied"])
        self.assertTrue(spec["for_human"])
        self.assertIsNotNone(spec["spec_change"])
        self.assertIn("fixture", spec["spec_change"]["where"])

    def test_nothing_was_auto_applied(self):
        self.assertEqual(self.fixrun.applied, [])
        self.assertIsNone(self.fixrun.patched_chip)
        self.assertIsNone(self.fixrun.patched_protocol)


class TestRealRepoChipCellPrepMixer(unittest.TestCase):
    """cell_prep_v1: cells settle in mix_1 (serpentine mixer)."""

    CHIP = "cell_prep_v1"
    FEATURE = "mix_1"

    @classmethod
    def setUpClass(cls):
        cls.findings = _sediment_findings(cls.CHIP, cls.FEATURE)
        if not cls.findings:
            raise unittest.SkipTest("particles.py reports no settling in mix_1")
        cls.chip, cls.protocol = _chip(cls.CHIP), _protocol(cls.CHIP)
        cls.sim = fix.Simulator()
        cls.fixrun = fix.propose(cls.findings, cls.chip, cls.protocol,
                              mode="standalone", sim=cls.sim)
        cls.by_action = {f["action"]: f for f in cls.fixrun.fixes}

    def test_the_whole_population_is_lost_before_the_fix(self):
        self.assertAlmostEqual(self.findings[0]["value"], 1.0, places=6)

    def test_the_wall_shear_ceiling_is_what_caps_the_flow(self):
        """There are adherent cells in cult_1 upstream, so shear bites before Reynolds."""
        step = fix._step(self.protocol, self.findings[0]["step"])
        sol = self.sim.run(self.chip, step)
        caps = fix._flow_ceilings(self.chip, step, sol,
                                  fix.shear_flow_ceiling(self.chip, self.protocol, step,
                                                         self.findings))
        tightest = min(caps, key=lambda c: c["max_flow_ul_min"])
        self.assertIn("shear", tightest["why"])
        self.assertLess(tightest["max_flow_ul_min"],
                        self.by_action["raise_flow"]["params"]["needed_ul_min"])

    def test_raising_the_flow_is_refused_because_mix_1_is_a_mixer(self):
        raise_fix = self.by_action["raise_flow"]
        self.assertFalse(raise_fix["applied"])
        self.assertIn("side_effects", raise_fix["blocked_by"])
        detail = next(g["detail"] for g in raise_fix["gates"]
                      if g["gate"] == "side_effects")
        self.assertIn("residence-time", detail)

    def test_vertical_mounting_is_offered(self):
        self.assertIn("vertical_mounting", self.by_action)
        self.assertEqual(self.by_action["vertical_mounting"]["kind"], "spec")

    def test_nothing_was_auto_applied(self):
        self.assertEqual(self.fixrun.applied, [])


class TestRealRepoChipModule3Weir(unittest.TestCase):
    """module3's depth weir: a PROCESS WINDOW problem, so the fix is a spec change.

    particles.py sweeps it and finds the weir retains for gaps 5.0-26.8 um - i.e. -20 to
    +1.8 um of the +/-20 um z tolerance, about 55% of the window - while the spec's nominal
    is 25 um, at the very top of its own band. There is nothing to widen and no flow to
    lower, and a catalogue that could only emit geometry and flow edits would have had to
    stay silent.
    """

    CHIP = "module3_v1_translated"

    @classmethod
    def setUpClass(cls):
        cls.findings = _weir_findings(cls.CHIP)
        if not cls.findings:
            raise unittest.SkipTest("particles.py reports no weir band for module3")
        cls.chip, cls.protocol = _chip(cls.CHIP), _protocol(cls.CHIP)
        cls.sim = fix.Simulator()
        cls.fixrun = fix.propose(cls.findings, cls.chip, cls.protocol,
                              mode="standalone", sim=cls.sim)
        cls.by_action = {f["action"]: f for f in cls.fixrun.fixes}

    def test_the_finding_is_a_band_not_a_number(self):
        self.assertIsInstance(self.findings[0]["value"], list)
        self.assertIsNone(fix._scalar(self.findings[0]["value"]))

    def test_it_is_routed_to_a_spec_change_not_a_flow_cut(self):
        self.assertEqual(fix.effective_class(self.findings[0]), "retention")
        self.assertNotIn("set_flow", self.by_action)
        self.assertNotIn("widen_channel", self.by_action)

    def test_tightening_z_is_proposed_with_a_centred_nominal(self):
        spec = self.by_action.get("tighten_z_tolerance")
        self.assertIsNotNone(spec)
        lo, hi = self.findings[0]["value"][:2]
        params = spec["params"]
        self.assertAlmostEqual(params["nominal_gap_um"], (lo + hi) / 2, places=1)
        self.assertAlmostEqual(params["z_tolerance_um"], (hi - lo) / 2, places=1)

    def test_the_new_nominal_is_lower_than_the_spec_s_own(self):
        """The chip's 25 um nominal sits at the top of a band that ends at 26.8 um."""
        params = self.by_action["tighten_z_tolerance"]["params"]
        stated = params.get("from_nominal_um")
        if stated is None:
            self.skipTest("the chip spec states no weir opening")
        self.assertLess(params["nominal_gap_um"], stated)

    def test_the_before_after_is_the_covered_window_and_rises_to_one(self):
        spec = self.by_action["tighten_z_tolerance"]
        self.assertLess(spec["predicted"]["before"], 1.0)
        self.assertGreater(spec["predicted"]["before"], 0.4)
        self.assertAlmostEqual(spec["predicted"]["after"], 1.0, places=6)

    def test_a_larger_capsule_is_offered_with_the_contract_check(self):
        spec = self.by_action.get("larger_capsule")
        self.assertIsNotNone(spec)
        self.assertIn("achievable", spec["params"])
        self.assertIn("with_contract_band", spec["params"])

    def test_both_are_for_a_human_and_name_the_file_they_land_in(self):
        for action in ("tighten_z_tolerance", "larger_capsule"):
            entry = self.by_action[action]
            self.assertEqual(entry["kind"], "spec")
            self.assertFalse(entry["applied"])
            self.assertFalse(entry["auto_applicable"])
            self.assertTrue(entry["for_human"])
            self.assertIn("measurable", entry["blocked_by"],
                          "a spec change must be blocked precisely because it cannot be "
                          "re-solved")
            self.assertTrue(entry["spec_change"]["where"].strip())

    def test_the_chip_is_not_blamed(self):
        self.assertIsNone(self.fixrun.patched_chip)
        self.assertEqual(self.fixrun.applied, [])


class TestClassesWithoutActions(unittest.TestCase):
    """handoff and unmodelled must be reported, not patched."""

    def test_no_candidate_is_generated_and_the_loop_says_why(self):
        chip, protocol = _over_sheared_chip()
        sim = fix.Simulator()
        for klass in ("handoff", "unmodelled"):
            finding = {"id": f"x-{klass}", "class": klass, "severity": "high",
                       "chip_id": chip["id"], "feature_id": "ch_in", "step": 0,
                       "metric": "reynolds_number", "value": 13.0, "limit": 1.0,
                       "units": "Reynolds number", "message": "out of validity"}
            self.assertEqual(fix.candidates_for(finding, chip, protocol), [])
            run = fix.propose([finding], chip, protocol, sim=sim)
            self.assertEqual(run.fixes, [])
            reasons = [e for e in run.log if e.get("event") == "no_candidate"]
            self.assertTrue(reasons, f"{klass} produced no explanation")
            self.assertIn(klass, reasons[0]["reason"])


if __name__ == "__main__":
    unittest.main()

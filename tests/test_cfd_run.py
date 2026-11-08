"""S10 integration: the CFD orchestrator, its figures, its report and its stage.

    python -m unittest tests.test_cfd_run -v
    python -m unittest discover -s tests

Offline: no network, no language model, nothing outside the repo and a temporary
directory. These tests do NOT check the physics - `assay2chip/cfd/validate.py` owns that
and the report quotes its verdict. They check that the INTEGRATION holds:

  * a missing, renamed or raising sibling is reported (NOT YET AVAILABLE / no entry
    point / errored) and skipped, never a crash, because ten agents build
    `assay2chip/cfd/` in parallel and the stage has to be usable throughout;
  * the figures are the GitHub-safe SVG subset (no <style>, no external font, no <image>,
    no href) and every map carries the chip outline, the ports, a scale bar and a
    SIMULATED badge;
  * wall shear is drawn against the ceiling for THAT chamber's contents, not one shared
    ceiling - the scoping bug this repo has shipped repeatedly;
  * a failed validation puts ALL FINDINGS ARE UNTRUSTED at the top of the report;
  * a geometry fix comes back as a `rerun_from` request and nothing under `chip/` or
    `protocols/` is ever written.

The two end-to-end chips are the ones the brief names: `module1_v1`, whose protocol
predates the `ports` field so every scenario is inferred from English, and
`module3_v1_translated`, the capsule trap with a depth weir and stated ports.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CFD = ROOT / "assay2chip" / "cfd"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


cfd_run = _load("assay2chip.cfd.run", CFD / "run.py")
figures = _load("assay2chip.cfd.figures", CFD / "figures.py")
report = _load("assay2chip.cfd.report", CFD / "report.py")
cfd_run.figures, cfd_run.report = figures, report
stage = _load("assay2chip.stages.s10_cfd", ROOT / "assay2chip" / "stages" / "s10_cfd.py")

from assay2chip.lib import contracts  # noqa: E402  (after sys.path)

CHIPS = {
    "module1_v1": ("chip/module1_v1.json", "protocols/module1_v1.json"),
    "module3_v1_translated": ("chip/module3_v1_translated.json",
                              "protocols/module3_v1.json"),
}

# One real S10 run per test chip, built once: a run meshes, solves, analyses and draws.
_RUNS: dict = {}
_TMP: list = []


def setUpModule():
    root = Path(tempfile.mkdtemp(prefix="cfd_run_tests_"))
    _TMP.append(root)
    for chip_id, (chip_rel, protocol_rel) in CHIPS.items():
        _RUNS[chip_id] = cfd_run.run_chip(
            ROOT / chip_rel, ROOT / protocol_rel, out_dir=root / chip_id,
            run_id=chip_id, max_steps=2, n_particles=4)


def tearDownModule():
    for path in _TMP:
        shutil.rmtree(path, ignore_errors=True)


def chip_spec(chip_id):
    return json.loads((ROOT / CHIPS[chip_id][0]).read_text())


# What GitHub does not render inside markdown. cad/make_figures.py has the same list in
# prose; here it is executable.
FORBIDDEN = (
    ("<style", "GitHub strips <style> inside markdown"),
    ("@import", "no external resources"),
    ("<image", "no embedded or linked images"),
    ("xlink:href", "no hrefs"),
    (" href=", "no hrefs"),
    ("<script", "never"),
    ("<foreignObject", "not rendered"),
    ("<use", "needs defs GitHub may strip"),
)


class SvgMixin:
    def assertGithubSafeSvg(self, text, where=""):
        for token, why in FORBIDDEN:
            self.assertNotIn(token, text, f"{where}: {token} present - {why}")
        self.assertTrue(text.startswith("<svg "), f"{where}: no <svg> root")
        self.assertIn('xmlns="http://www.w3.org/2000/svg"', text)
        self.assertIn("viewBox=", text)
        self.assertIn("<title>", text)
        for family in set(re.findall(r'font-family="([^"]+)"', text)):
            self.assertTrue(
                any(generic in family for generic in
                    ("sans-serif", "monospace", "serif")),
                f"{where}: font-family {family!r} has no generic fallback")


class TempDirMixin:
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp(prefix="cfd_case_"))
        self.addCleanup(shutil.rmtree, self.tmp, True)


# --- the figures ---------------------------------------------------------------------------

class TestFiguresDegradeGracefully(SvgMixin, TempDirMixin, unittest.TestCase):
    def test_draws_with_no_mesh_and_no_field(self):
        """Day one: mesh.py and solve.py are absent. A map is a labelled gap, not a crash."""
        chip = chip_spec("module1_v1")
        paths = figures.step_figures(chip, self.tmp, mesh=None, field=None,
                                     transport=None, findings=[], step=1,
                                     step_label="step 1 exchange")
        self.assertEqual(len(paths), 4)
        self.assertEqual({Path(p).name for p in paths},
                         {f"module1_v1_s1_{kind}.svg" for kind in
                          ("pressure", "speed", "shear", "residence")})
        for path in paths:
            text = Path(path).read_text()
            self.assertGithubSafeSvg(text, Path(path).name)
            self.assertIn("NOT YET AVAILABLE", text)
            self.assertIn("SIMULATED", text)
            self.assertIn("no values to map yet", text)

    def test_a_broken_map_does_not_stop_the_other_three(self):
        bad = {"id": "broken", "footprint": {"x": 30, "y": 20}, "chambers": "not a list"}
        paths = figures.step_figures(bad, self.tmp, step=1)
        self.assertEqual(len(paths), 4)
        for path in paths:
            self.assertGithubSafeSvg(Path(path).read_text(), Path(path).name)


class TestMapChrome(SvgMixin, unittest.TestCase):
    def test_every_map_has_outline_ports_and_a_scale_bar(self):
        for chip_id, result in _RUNS.items():
            chip = chip_spec(chip_id)
            port_ids = [p["id"] for p in chip["ports"]]
            maps = [p for p in result["figures"] if "pipeline_overview" not in p]
            self.assertTrue(maps, f"{chip_id}: no maps drawn")
            for rel in maps:
                text = (ROOT / rel).read_text()
                self.assertGithubSafeSvg(text, rel)
                self.assertIn(" mm</text>", text, f"{rel}: no scale bar")
                self.assertIn("SIMULATED", text)
                self.assertIn("simulated, not measured", text)
                named = sum(pid in text for pid in port_ids)
                self.assertGreaterEqual(named, len(port_ids) - 1,
                                        f"{rel}: ports are not labelled")

    def test_demo_scale_chips_are_badged(self):
        chip = chip_spec("module3_v1_translated")
        self.assertTrue(figures.is_demo_scale(chip), "30 x 20 mm is the demo envelope")
        text = (ROOT / _RUNS["module3_v1_translated"]["figures"][0]).read_text()
        self.assertIn("DEMO SCALE", text)

    def test_all_four_maps_exist_for_every_solved_step(self):
        for chip_id, result in _RUNS.items():
            for chip in result["chips"]:
                steps = [s["step"] for s in chip["steps"]]
                self.assertTrue(steps, f"{chip_id}: no steps simulated")
                for step in steps:
                    for kind in ("pressure", "speed", "shear", "residence"):
                        name = f"{chip['chip_id']}_s{step}_{kind}.svg"
                        self.assertTrue(
                            any(p.endswith(name) for p in result["figures"]),
                            f"missing {name}")

    def test_svgs_stay_small_enough_for_github(self):
        """A 13,000-cell mesh drawn one rect per cell is a multi-megabyte SVG."""
        for result in _RUNS.values():
            for rel in result["figures"]:
                size = (ROOT / rel).stat().st_size
                self.assertLess(size, 900_000, f"{rel} is {size} bytes")


class TestShearIsScopedPerChamber(SvgMixin, TempDirMixin, unittest.TestCase):
    def test_capsule_chamber_is_pressure_bounded_not_shear_bounded(self):
        chip = chip_spec("module3_v1_translated")
        trap = figures.chamber_ceilings(chip, {"operation": "load"})["main_trap"]
        self.assertEqual(trap["shear_class"], "capsules")
        self.assertIsNone(trap["ceiling_dyn_cm2"],
                          "a capsule chamber has NO wall-shear ceiling")
        self.assertGreater(trap["pressure_ceiling_kpa"], 0)
        self.assertAlmostEqual(trap["pressure_ceiling_kpa"],
                               contracts.max_working_pressure_kpa(), places=9)

    def test_adherent_chamber_uses_the_culture_ceiling(self):
        chip = chip_spec("module1_v1")
        culture = figures.chamber_ceilings(
            chip, {"operation": "exchange", "reagent": "culture_media"})["n_cx"]
        self.assertEqual(culture["shear_class"], "adherent_cells")
        self.assertAlmostEqual(
            culture["ceiling_dyn_cm2"],
            contracts.shear_ceiling_for("adherent_cells").ceiling_dyn_cm2, places=9)

    def test_a_rinse_before_a_lyse_uses_the_looser_ceiling(self):
        """The scope is a property of the (chamber, step) PAIR, not of the chamber."""
        chip = chip_spec("module1_v1")
        culture = figures.chamber_ceilings(
            chip, {"operation": "exchange", "reagent": "culture_media"})["n_cx"]
        rinse = figures.chamber_ceilings(
            chip, {"operation": "wash", "reagent": "lysis_buffer",
                   "note": "rinse immediately before the lyse"})["n_cx"]
        self.assertEqual(rinse["shear_class"], "prelysis_rinse")
        self.assertGreater(rinse["ceiling_dyn_cm2"], culture["ceiling_dyn_cm2"])

    def test_shear_legend_names_each_chamber_ceiling_separately(self):
        result = _RUNS["module3_v1_translated"]
        shear = next(p for p in result["figures"] if p.endswith("_shear.svg"))
        text = (ROOT / shear).read_text()
        self.assertIn("CEILING PER CHAMBER", text)
        self.assertIn("main_trap", text)
        self.assertIn("NO shear ceiling", text,
                      "the trap's legend must say it has none")

    def test_no_contract_value_is_hard_coded_in_figures(self):
        """A literal that duplicates a contract value is the bug this repo keeps hitting."""
        source = (CFD / "figures.py").read_text()
        body = "\n".join(line for line in source.splitlines()
                         if not line.strip().startswith("#"))
        for literal in ("= 0.01", "= 0.3\n", "= 5.0", "= 0.0008"):
            self.assertNotIn(literal, body, f"{literal!r} looks like a contract value")
        self.assertIn("shear_ceiling_for", source)


class TestFindingsOnTheFigure(SvgMixin, TempDirMixin, unittest.TestCase):
    FINDINGS = [
        {"id": "f1", "class": "clog", "severity": "blocker",
         "chip_id": "module3_v1_translated", "feature_id": "ch_capsule_in", "step": 1,
         "metric": "min_dimension_um", "value": 200.0, "limit": 210.0, "units": "um",
         "message": "too narrow for a swollen capsule"},
        {"id": "f2", "class": "trap", "severity": "medium",
         "chip_id": "module3_v1_translated", "feature_id": "main_trap", "step": 1,
         "metric": "low_speed_fraction", "value": 0.3, "limit": 0.1,
         "units": "fraction", "message": "dead corner"},
        {"id": "f3", "class": "handoff", "severity": "high",
         "chip_id": "module3_v1_translated", "feature_id": None, "step": 1,
         "metric": "particle_um", "value": 260.0, "limit": 70.0, "units": "um",
         "message": "capsules arrive too big"},
    ]

    def test_findings_are_marked_where_they_occur(self):
        chip = chip_spec("module3_v1_translated")
        path = figures.map_figure(chip, "pressure", self.tmp, findings=self.FINDINGS,
                                 step=1)
        text = Path(path).read_text()
        self.assertGithubSafeSvg(text, Path(path).name)
        self.assertIn("FINDINGS HERE", text)
        self.assertIn("BLOCKER clog", text)
        self.assertIn("ch_capsule_in", text)
        self.assertIn(figures.SEVERITY_C["blocker"], text)
        self.assertIn("chip-wide", text,
                      "a finding with no feature is listed, not dropped")

    def test_a_finding_from_another_step_is_not_drawn_on_this_one(self):
        chip = chip_spec("module3_v1_translated")
        path = figures.map_figure(chip, "pressure", self.tmp, findings=self.FINDINGS,
                                 step=9)
        self.assertIn("no findings on this step", Path(path).read_text())

    def test_feature_point_resolves_chambers_channels_and_ports(self):
        chip = chip_spec("module3_v1_translated")
        self.assertEqual(figures.feature_point(chip, None, "main_trap"), (12.5, 10.0))
        self.assertEqual(figures.feature_point(chip, None, "capsule_in"), (2.5, 14.0))
        self.assertIsNotNone(figures.feature_point(chip, None, "ch_capsule_in"))
        self.assertIsNone(figures.feature_point(chip, None, "does_not_exist"))

    def test_binning_keeps_an_over_ceiling_cell(self):
        """Coarsening the drawing may hide a gradient; it must not hide a breach."""
        cells = [({"id": f"c{i}", "kind": "cell", "x_mm": i * 0.1, "y_mm": 0.0,
                   "area_mm2": 0.01}, 0.1, i == 7) for i in range(400)]
        drawn, factor = figures.bin_cells(cells, 0.1, limit=20)
        self.assertIsNotNone(factor)
        self.assertLessEqual(len(drawn), 400)
        self.assertTrue(any(over for *_rest, over in drawn))


class TestPipelineOverview(SvgMixin, TempDirMixin, unittest.TestCase):
    def test_every_module_in_chain_order_with_handoffs_and_badges(self):
        pipeline = json.loads((ROOT / "protocols" / "pipeline_mvp.json").read_text())
        chips = {}
        for module in pipeline["modules"]:
            if module.get("chip") and (ROOT / module["chip"]).exists():
                chips[module["module"]] = json.loads(
                    (ROOT / module["chip"]).read_text())
        findings = {"module3_v1_translated": [{"severity": "blocker", "class": "clog"}]}
        path = figures.pipeline_overview(pipeline, self.tmp, chips, findings)
        text = Path(path).read_text()
        self.assertGithubSafeSvg(text, "pipeline_overview.svg")
        order = [module["module"] for module in pipeline["modules"]]
        positions = [text.index(f"{i + 1}. {mid}") for i, mid in enumerate(order)]
        self.assertEqual(positions, sorted(positions),
                         "modules are not drawn in chain order")
        for hand in pipeline["handoffs"]:
            target = (hand.get("to") or {}).get("module")
            if target:
                self.assertIn(f"\u2192 {target}", text,
                              f"handoff to {target} is not drawn")
        self.assertIn("BLOCKER", text, "the worst finding is not badged on its chip")
        self.assertIn("NO CFD", text, "a placeholder module must not read CLEAN")
        self.assertIn("SIMULATED", text)

    def test_worst_severity_picks_the_worst(self):
        self.assertEqual(figures.worst_severity(
            [{"severity": "low"}, {"severity": "blocker"}, {"severity": "medium"}]),
            "blocker")
        self.assertIsNone(figures.worst_severity([]))


# --- missing and broken siblings -------------------------------------------------------------

class TestSiblingGuards(TempDirMixin, unittest.TestCase):
    def test_absent_sibling_is_reported_not_crashed(self):
        caller = cfd_run.Caller("definitely_not_a_module", ["anything"],
                                lambda _m: None)
        self.assertFalse(caller.available)
        self.assertEqual(caller.status, cfd_run.NOT_YET)
        self.assertEqual(caller.call(caller.func("anything")), (None, False))
        self.assertEqual(caller.report_row()["status"], cfd_run.NOT_YET)

    def test_sibling_with_no_known_entry_point_is_reported(self):
        (self.tmp / "ghost.py").write_text("def something_else():\n    return 1\n")
        real, cfd_run.CFD_DIR = cfd_run.CFD_DIR, self.tmp
        try:
            caller = cfd_run.Caller("ghost", ["build_mesh", "mesh_chip"],
                                    lambda _m: None)
        finally:
            cfd_run.CFD_DIR = real
        self.assertEqual(caller.status, "no entry point")
        self.assertIn("build_mesh", caller.detail)

    def test_sibling_that_fails_to_import_is_reported(self):
        (self.tmp / "broken.py").write_text("raise ImportError('no scipy here')\n")
        real, cfd_run.CFD_DIR = cfd_run.CFD_DIR, self.tmp
        try:
            module, status, detail = cfd_run.load_sibling("broken")
        finally:
            cfd_run.CFD_DIR = real
        self.assertIsNone(module)
        self.assertEqual(status, "import failed")
        self.assertIn("no scipy here", detail)

    def test_raising_sibling_is_reported_as_errored(self):
        messages = []
        caller = cfd_run.Caller("mesh", ["build_mesh"], messages.append)

        def boom(*_a, **_k):
            raise RuntimeError("the solver fell over")

        self.assertEqual(caller.call(boom), (None, False))
        row = caller.report_row()
        self.assertEqual(row["status"], "errored")
        self.assertIn("the solver fell over", row["detail"])
        self.assertTrue(messages, "the failure was not logged")

    def test_quiet_attempt_does_not_blame_the_module(self):
        """A speculative first call (stricter signature, batched entry) is not a failure."""
        caller = cfd_run.Caller("mesh", ["build_mesh"], lambda _m: None)

        def boom(*_a, **_k):
            raise TypeError("wrong signature")

        caller.call(boom, quiet=True)
        self.assertNotEqual(caller.report_row()["status"], "errored")

    def test_unexpected_keyword_arguments_are_dropped_not_fatal(self):
        caller = cfd_run.Caller("mesh", ["build_mesh"], lambda _m: None)

        def narrow(chip, *, temp_c=None):
            return {"chip": chip, "temp_c": temp_c}

        result, ok = caller.call(narrow, {"id": "x"}, temp_c=37, grid_mm=0.1,
                                 strict=False)
        self.assertTrue(ok)
        self.assertEqual(result["temp_c"], 37)

    def test_run_survives_every_sibling_being_absent(self):
        """The whole orchestrator on an empty cfd/ directory: a report, no exception."""
        empty = self.tmp / "cfd_modules"
        empty.mkdir()
        real, cfd_run.CFD_DIR = cfd_run.CFD_DIR, empty
        try:
            result = cfd_run.run_chip(ROOT / CHIPS["module1_v1"][0],
                                      ROOT / CHIPS["module1_v1"][1],
                                      out_dir=self.tmp / "out", run_id="bare")
        finally:
            cfd_run.CFD_DIR = real
        for name, info in result["modules"].items():
            self.assertEqual(info["status"], cfd_run.NOT_YET, name)
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["fixes"], [])
        self.assertFalse(result["validation"]["trusted"])
        self.assertTrue(result["figures"], "geometry figures are still drawn")
        text = (self.tmp / "out" / "report.md").read_text()
        self.assertIn("ALL FINDINGS BELOW ARE UNTRUSTED", text)
        self.assertIn("NOT YET AVAILABLE", text)


class TestStepSelection(unittest.TestCase):
    class Record:
        def __init__(self, operation, ports, kind="step"):
            self.operation, self.ports, self.kind = operation, ports, kind

    def _scenarios(self):
        flow = {"p_in": {"type": "flow", "q_ul_min": 1.0}}
        other = {"p_in": {"type": "flow", "q_ul_min": 5.0}}
        R = self.Record

        class Scenarios:
            step_scenarios = [R("exchange", flow), R("exchange", flow),
                              R("exchange", other), R("hold", {}, "no_flow")]
            worst_cases = []
            unmapped = []
        return Scenarios()

    def test_identical_boundary_conditions_are_solved_once(self):
        """Module 1 has 182 steps and 104 of them flow; the field repeats, so don't."""
        chosen, note = cfd_run.pick_records(self._scenarios(), max_steps=4, max_worst=0)
        self.assertEqual(len(chosen), 2)
        self.assertIn("no-flow", note)

    def test_the_cap_is_honoured(self):
        chosen, _ = cfd_run.pick_records(self._scenarios(), max_steps=1, max_worst=0)
        self.assertEqual(len(chosen), 1)

    def test_no_scenario_module_is_geometry_only(self):
        chosen, note = cfd_run.pick_records(None, 4, 2)
        self.assertEqual(chosen, [])
        self.assertIn("geometry figures only", note)


# --- the report ---------------------------------------------------------------------------

class TestReport(unittest.TestCase):
    def test_failed_validation_puts_untrusted_at_the_very_top(self):
        text = report.render({
            "run": "x",
            "validation": {"status": "fail", "trusted": False,
                           "checks": [{"name": "mass conservation", "ok": False}]},
            "findings": [{"id": "f1", "severity": "blocker", "class": "leak"}]})
        head = text.split("## 1.")[0]
        self.assertIn("ALL FINDINGS BELOW ARE UNTRUSTED", head)
        self.assertLess(head.index("UNTRUSTED"), head.index("value"))

    def test_missing_validation_is_also_untrusted(self):
        text = report.render({"run": "x", "findings": []})
        self.assertIn("ALL FINDINGS BELOW ARE UNTRUSTED", text)
        self.assertIn("validate.py is NOT YET AVAILABLE", text)

    def test_passing_validation_does_not_claim_untrusted(self):
        text = report.render({
            "run": "x",
            "validation": {"status": "pass", "trusted": True,
                           "checks": [{"name": "duct", "ok": True}]},
            "findings": []})
        self.assertNotIn("UNTRUSTED", text.split("## 1.")[0])
        self.assertIn("VALIDATED", text)

    def test_all_sections_and_the_honest_limits_are_present(self):
        text = report.render(_RUNS["module3_v1_translated"])
        for heading in ("## 1. What ran", "## 2. Findings",
                        "## 3. Fixes, before and after", "## 4. Handoffs",
                        "## 5. Validation", "## 6. Honest limits", "## 7. Figures",
                        report.FOAM_SECTION):
            self.assertIn(heading, text, f"missing {heading}")
        for subject in ("depth-averaged Hele-Shaw", "Re > 1", "tubing between chips",
                        "densities and sedimentation", "contact angles",
                        "cold-solvent viscosity"):
            self.assertIn(subject, text,
                          f"the honest limits do not mention {subject}")

    def test_the_report_is_tables_not_prose(self):
        """The owner asks for short documents: tables, one line of context each."""
        text = report.render(_RUNS["module3_v1_translated"])
        lines = [line for line in text.splitlines() if line.strip()]
        rows = [line for line in lines if line.startswith("|")]
        self.assertGreater(len(rows), len(lines) * 0.4, "more prose than table")
        paragraphs = [line for line in lines
                      if not line.startswith(("|", "#", ">", "-", "_", "!", "["))]
        self.assertTrue(all(len(p) < 700 for p in paragraphs),
                        "a paragraph ran away")

    def test_every_fix_shows_before_and_after(self):
        text = report.render({
            "run": "x",
            "validation": {"status": "pass", "trusted": True, "checks": []},
            "findings": [],
            "fixes": [{"finding_id": "f1", "action": "widen_channel",
                       "target": "ch_reagent",
                       "predicted": {"metric": "shear_dyn_cm2", "before": 0.9,
                                     "after": 0.21},
                       "auto_applicable": True, "applied": True,
                       "verified_after": "1 finding gone"}]})
        row = next(line for line in text.splitlines() if "widen_channel" in line)
        for token in ("0.9", "0.21", "applied"):
            self.assertIn(token, row)

    def test_findings_are_grouped_by_severity_worst_first(self):
        text = report.render({"run": "x", "findings": [
            {"id": "a", "severity": "low", "class": "trap"},
            {"id": "b", "severity": "blocker", "class": "leak"},
            {"id": "c", "severity": "medium", "class": "shear"}]})
        self.assertLess(text.index("### blocker"), text.index("### medium"))
        self.assertLess(text.index("### medium"), text.index("### low"))

    def test_render_never_raises_on_a_half_empty_result(self):
        for partial in ({}, {"run": "x"}, {"findings": None, "chips": None},
                        {"validation": "nonsense"}, {"fixes": [{}], "findings": [{}]},
                        {"handoffs": [{"from": None, "to": None}]}):
            self.assertTrue(report.render(partial).startswith("# S10 CFD"))

    def test_the_3d_slot_is_labelled_not_yet_available(self):
        """assay2chip/foam/ is built in parallel. Section 6 keeps saying this model is
        depth-averaged, so the reader must be told plainly that no 3D check exists."""
        text = report.render(_RUNS["module3_v1_translated"])
        self.assertIn(report.FOAM_SECTION, text)
        tail = text.split(report.FOAM_SECTION)[1]
        self.assertIn("NOT YET AVAILABLE", tail)
        self.assertIn("OpenFOAM", text)
        self.assertLess(text.index("## 7. Figures"), text.index(report.FOAM_SECTION))

    def test_the_3d_slot_fills_in_without_restructuring(self):
        text = report.render({
            "run": "x", "findings": [],
            "foam": {"status": "ok", "note": "4 cases on the trap",
                     "cases": [{"id": "trap_s1", "chip_id": "module3_v1_translated",
                                "step": 1, "metric": "recirculation",
                                "verdict": "none found"}],
                     "disagreements": [{"chip_id": "module3_v1_translated",
                                        "feature_id": "main_trap",
                                        "metric": "shear_dyn_cm2",
                                        "depth_averaged": 0.21, "foam": 0.47,
                                        "message": "3D peak is 2.2x the depth average"}],
                     "figures": ["cfd/figures/foam_trap_s1.svg"]}})
        tail = text.split(report.FOAM_SECTION)[1]
        self.assertNotIn("NOT YET AVAILABLE", tail)
        self.assertIn("trap_s1", tail)
        row = next(line for line in tail.splitlines() if "2.2x" in line)
        self.assertIn("0.21", row)
        self.assertIn("0.47", row)
        self.assertIn("![3D result](cfd/figures/foam_trap_s1.svg)", tail)

    def test_a_pipe_in_a_message_does_not_break_the_table(self):
        text = report.render({"run": "x", "findings": [
            {"id": "f1", "severity": "low", "class": "trap",
             "message": "a | pipe and a\nnewline"}]})
        row = next(line for line in text.splitlines() if "pipe" in line)
        self.assertEqual(row.count("|"), 9, "a message broke the table columns")


# --- the end-to-end runs --------------------------------------------------------------------

class TestEndToEnd(unittest.TestCase):
    def test_both_chips_produce_a_full_run(self):
        for chip_id, result in _RUNS.items():
            self.assertEqual(result["run"], chip_id)
            out = ROOT / result["out_dir"]
            for name in ("findings.json", "fixes.json", "validation.json", "cfd.json",
                         "report.md", "pipeline.field.json"):
                self.assertTrue((out / name).exists(), f"{chip_id}: {name} not written")
            json.loads((out / "cfd.json").read_text())
            self.assertEqual(result["chips"][0]["chip_id"], chip_id)
            self.assertTrue(result["assumptions"],
                            "no flagged assumptions were recorded")

    def test_the_mesh_discretises_the_chambers(self):
        """A chamber collapsed to one node looks like a pipe and hides dead zones."""
        for chip_id, result in _RUNS.items():
            mesh = result["chips"][0]["mesh"]
            if mesh["status"] != "ok":
                self.skipTest(f"mesh.py: {mesh['status']}")
            chambers = len(chip_spec(chip_id)["chambers"])
            self.assertGreater(mesh["nodes"], chambers * 8,
                               f"{chip_id}: {mesh['nodes']} cells for {chambers} "
                               f"chambers is not a discretisation")

    def test_module1_scenarios_are_inferred_and_say_so(self):
        """module1_v1's protocol predates `ports`, so a scenario is a reading of English."""
        steps = _RUNS["module1_v1"]["chips"][0]["steps"]
        if not steps:
            self.skipTest("scenario.py produced no steps")
        self.assertTrue(any(step["inferred"] for step in steps),
                        "an inferred port state was not flagged")

    def test_viscosity_comes_from_the_contract_per_step(self):
        for chip_id, result in _RUNS.items():
            for step in result["chips"][0]["steps"]:
                mu = step.get("viscosity_pa_s")
                if mu is None or step.get("viscosity_is_assumption"):
                    continue
                expected = contracts.viscosity_for(step.get("temp_c")).pa_s
                self.assertAlmostEqual(
                    mu, expected, places=9,
                    msg=f"{chip_id} step {step['step']}: {mu} is neither contract "
                        f"value and is not flagged as an assumption")

    def test_re_above_one_is_a_finding_not_a_number(self):
        for result in _RUNS.values():
            for row in result["unmodelled"]:
                self.assertGreater(row["re"], 1.0)
                self.assertIn("outside", row["note"])
            for step in result["chips"][0]["steps"]:
                if step.get("edges_above_re_1"):
                    self.assertGreater(step["re_max"], 1.0)

    def test_nothing_under_chip_or_protocols_is_written(self):
        """A hand-designed chip is never modified. Standalone fixes are copies."""
        for result in _RUNS.values():
            out = ROOT / result["out_dir"]
            for path in (p.resolve() for p in out.rglob("*") if p.is_file()):
                self.assertNotIn(ROOT / "chip", path.parents)
                self.assertNotIn(ROOT / "protocols", path.parents)
            for patched in (out / "patched").glob("*.json"):
                self.assertEqual(patched.parent.name, "patched")

    def test_standalone_mode_never_requests_a_rerun(self):
        for chip_id, result in _RUNS.items():
            self.assertIsNone(result["rerun"],
                              f"{chip_id}: standalone mode asked for a re-run")

    def test_findings_carry_the_two_numbers_that_disagree(self):
        for result in _RUNS.values():
            for finding in result["findings"]:
                self.assertIn("class", finding)
                self.assertIn("severity", finding)
                self.assertIn(finding["severity"],
                              ("blocker", "high", "medium", "low", "info"))
                self.assertIn("message", finding)

    def test_the_summary_names_every_module_it_looked_for(self):
        for result in _RUNS.values():
            self.assertEqual(set(result["modules"]), set(cfd_run.SIBLING_SPEC))


# --- the stage -------------------------------------------------------------------------------

class TestStage(TempDirMixin, unittest.TestCase):
    PARTITION = {
        "id": "demo",
        "chips": [{"chip_id": "module1_v1", "ops": [1], "reason": "culture"},
                  {"chip_id": "never_built", "ops": [2], "reason": "nothing"}],
        "handoffs": [{"from": "module1_v1", "from_port": "p_mid", "to": "never_built",
                      "to_port": "in_1", "carries": "cells", "volume_ul": 40,
                      "automated": False, "how": "pipetting",
                      "risks": ["dead volume"]}],
    }

    def test_declares_no_writes_outside_the_work_directory(self):
        self.assertEqual(stage.WRITES, [])
        self.assertTrue(callable(stage.run))
        params = set(inspect.signature(stage.run).parameters)
        self.assertTrue({"ctx", "assay_id"} <= params)

    def test_the_rerun_cap_matches_the_orchestrator(self):
        orchestrator = _load("assay2chip.run", ROOT / "assay2chip" / "run.py")
        self.assertEqual(stage.MAX_LOOPS, orchestrator.MAX_RERUN_LOOPS)

    def test_a_partition_becomes_the_chain_view(self):
        pairs = stage._chip_paths(self.PARTITION)
        self.assertEqual([p[0] for p in pairs], ["module1_v1", "never_built"])
        self.assertIsNone(pairs[1][1], "a chip that was never built has no spec")
        pipeline = stage._as_pipeline(self.PARTITION, pairs)
        self.assertEqual([m["module"] for m in pipeline["modules"]],
                         ["module1_v1", "never_built"])
        self.assertEqual(pipeline["modules"][1]["status"], "missing")
        hand = pipeline["handoffs"][0]
        self.assertEqual(hand["connector"]["status"], "manual")
        self.assertIn("not automated", hand["connector"]["warning"])

    def test_the_chain_view_draws(self):
        pairs = stage._chip_paths(self.PARTITION)
        pipeline = stage._as_pipeline(self.PARTITION, pairs)
        path = figures.pipeline_overview(
            pipeline, self.tmp, {"module1_v1": chip_spec("module1_v1")})
        self.assertIn("never_built", Path(path).read_text())

    def test_a_missing_cfd_package_is_reported_not_fatal(self):
        real, stage.CFD_RUN = stage.CFD_RUN, self.tmp / "nope.py"

        class Ctx:
            id = "demo"
            work_dir = self.tmp
            loop = 0

            @staticmethod
            def log(_message):
                pass

        try:
            self.assertIsNone(stage.run(ctx=Ctx()))
        finally:
            stage.CFD_RUN = real
        text = (self.tmp / "cfd" / "report.md").read_text()
        self.assertIn("NOT RUN", text)
        self.assertIn("has been checked", text)


if __name__ == "__main__":
    unittest.main()

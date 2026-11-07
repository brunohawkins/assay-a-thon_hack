"""The current pipeline (protocols/pipeline_mvp.json) and the Module 3 translation.

    python -m unittest discover -s tests -v
"""

import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "tools"))

import check_chip_spec as ccs  # noqa: E402
import convert_module3  # noqa: E402
import schema_io  # noqa: E402
import validate  # noqa: E402

PIPE = schema_io.load_json(ROOT / "protocols" / "pipeline_mvp.json")


def run_check(pipe, root=ROOT):
    report = validate.Report()
    validate.check_pipeline("pipeline", pipe, report, root)
    return report


class CurrentPipeline(unittest.TestCase):
    def test_order_and_status_badges(self):
        got = [(m["module"], m["status"]) for m in PIPE["modules"]]
        self.assertEqual(got, [("module1", "final"), ("module2", "draft"), ("module3", "translated"),
                               ("barcoding", "missing"), ("module4", "final"), ("module5", "final")])
        m2 = PIPE["modules"][1]
        self.assertEqual(m2["status_note"], "draft – being redesigned by MJ")
        self.assertEqual(m2["prefer"]["glob"], "chip/module2*.json")

    def test_it_validates_with_no_errors(self):
        self.assertEqual(run_check(PIPE).errors, [])

    def test_only_module1_is_built_for_the_demo(self):
        self.assertEqual([m["module"] for m in PIPE["modules"] if m.get("built_for_demo")], ["module1"])

    def test_superseded_drafts_are_out(self):
        chips = {m.get("chip") for m in PIPE["modules"]}
        for path in ("cad/m1_culture_spec.json", "cad/m1b_cellprep_spec.json", "cad/m3_wash_spec.json"):
            self.assertNotIn(path, chips)
            block = schema_io.load_json(ROOT / path)["module"]
            self.assertEqual(block["status"], "superseded")
            self.assertTrue((ROOT / block["superseded_by"]).is_file())

    def test_handoff_chain(self):
        ends = [(h["from"].get("module"), h["from"].get("port"), h["to"].get("module") or h["to"].get("roadmap"))
                for h in PIPE["handoffs"]]
        self.assertEqual(ends, [("module1", "p_mid", "module2"), ("module2", "m2_capsule_out_1", "module3"),
                                ("module3", "to_split_tree", "barcoding"), ("barcoding", "TBC", "module4"),
                                ("module4", "TBC", "module5"), ("module5", "out_lib", "library_out")])
        self.assertEqual(PIPE["handoffs"][3]["to"]["ports"], ["in_s1", "in_s2", "in_s3", "in_s4"])

    def test_tbc_ports_and_connector_mismatches_are_listed(self):
        warnings = " | ".join(run_check(PIPE).warnings)
        self.assertIn("TBC", warnings)
        self.assertIn("module1_to_module2", warnings)
        self.assertNotIn("module4_to_module5' connector", warnings)  # both COC, drilled: no adapter


class PipelineRules(unittest.TestCase):
    def test_a_superseded_chip_in_the_pipeline_is_an_error(self):
        pipe = copy.deepcopy(PIPE)
        pipe["modules"][2]["chip"] = "cad/m3_wash_spec.json"
        pipe["modules"][2]["status"] = "draft"
        self.assertTrue(any("superseded" in e for e in run_check(pipe).errors))

    def test_a_wrong_connector_status_is_an_error(self):
        pipe = copy.deepcopy(PIPE)
        pipe["handoffs"][4]["connector"]["status"] = "mismatch"
        pipe["handoffs"][4]["connector"]["warning"] = "x"
        self.assertTrue(any("module4_to_module5" in e and "connector" in e for e in run_check(pipe).errors))

    def test_a_named_port_on_a_missing_module_is_an_error(self):
        pipe = copy.deepcopy(PIPE)
        pipe["handoffs"][2]["to"]["port"] = "split_in"
        self.assertTrue(any("must be TBC" in e for e in run_check(pipe).errors))

    def test_a_missing_module_needs_no_chip_but_others_do(self):
        pipe = copy.deepcopy(PIPE)
        del pipe["modules"][4]["chip"]
        self.assertTrue(run_check(pipe).errors)

    def test_module2_switches_to_mjs_spec_when_it_appears(self):
        entry = PIPE["modules"][1]
        self.assertEqual(schema_io.pipeline_chip(entry)[:2], ("cad/m2_encapsulation_spec.json", "draft"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "chip").mkdir()
            (root / "chip" / "module2_v1.json").write_text("{}")
            path, status, note = schema_io.pipeline_chip(entry, root)
            self.assertEqual((path, status), ("chip/module2_v1.json", "final"))
            self.assertIn("automatically", note)


class Module3Translation(unittest.TestCase):
    SRC = schema_io.load_json(ROOT / convert_module3.SOURCE)
    SPEC, QUESTIONS = convert_module3.translate(SRC)

    def test_committed_translation_is_up_to_date(self):
        self.assertEqual(schema_io.load_json(ROOT / convert_module3.OUT), self.SPEC,
                         "re-run python tools/convert_module3.py")

    def test_translation_does_not_touch_the_source(self):
        before = hashlib.sha256((ROOT / convert_module3.SOURCE).read_bytes()).hexdigest()
        convert_module3.translate(json.loads((ROOT / convert_module3.SOURCE).read_text()))
        self.assertEqual(hashlib.sha256((ROOT / convert_module3.SOURCE).read_bytes()).hexdigest(), before)

    def test_keeps_her_ids_and_numbers(self):
        ids = {x["id"] for g in ("chambers", "ports", "valves") for x in self.SPEC[g]}
        for name in ("main_trap", "capsule_prefilter", "capsule_in", "lysis_buffer", "wash_buffer", "waste",
                     "lysis_waste", "to_split_tree", *(v["id"] for v in self.SRC["valves"])):
            self.assertIn(name, ids)
        trap = next(c for c in self.SPEC["chambers"] if c["id"] == "main_trap")
        self.assertEqual((trap["size"], trap["volume_ul"], trap["retention"]["opening_um"]), ([28.3, 28.3, 0.35], 280.3, 25))
        self.assertEqual({c["width_mm"] for c in self.SPEC["channels"]}, {0.14})
        self.assertEqual(self.SPEC["translated_from"], "chip/modules/module3_step1_3.json")

    def test_inferred_parts_are_marked_placeholder(self):
        self.assertTrue(self.SPEC["footprint"]["placeholder"])
        for group in ("chambers", "channels", "ports", "valves", "layers"):
            self.assertTrue(all(x.get("placeholder") for x in self.SPEC[group]), group)

    def test_is_a_valid_chip_spec_and_the_checker_finds_no_errors(self):
        self.assertEqual(schema_io.schema_errors("chip_spec", self.SPEC), [])
        params = schema_io.load_json(ROOT / "contracts" / "parameters.json")
        materials = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
        fab = schema_io.load_json(ROOT / "cad" / "fabrication.json")
        self.assertEqual(ccs.check(self.SPEC, [], params, materials, fab).errors, [])

    def test_questions_flag_the_valves_and_the_retention_gap(self):
        self.assertIn("7 valves", self.QUESTIONS[0])
        self.assertIn("no valves", self.QUESTIONS[0])
        self.assertIn("25 um", self.QUESTIONS[1])
        self.assertIn("laser-cuttable", self.QUESTIONS[1])


if __name__ == "__main__":
    unittest.main()

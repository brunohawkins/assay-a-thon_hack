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


def chain_order(pipe):
    """Module ids in hand-off order, read from the hand-offs themselves."""
    order = []
    for h in pipe["handoffs"]:
        for end in ("from", "to"):
            m = h[end].get("module")
            if m and m not in order:
                order.append(m)
    return order


class CurrentPipeline(unittest.TestCase):
    def test_modules_are_unique_and_in_hand_off_order(self):
        names = [m["module"] for m in PIPE["modules"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(names, chain_order(PIPE))

    def test_every_module_has_a_status_and_a_missing_one_has_no_chip(self):
        for m in PIPE["modules"]:
            self.assertTrue(m.get("status"), m["module"])
            if m["status"] == "missing":
                self.assertNotIn("chip", m, m["module"])
            else:
                self.assertTrue((ROOT / schema_io.pipeline_chip(m)[0]).is_file(), m["module"])

    def test_it_validates_with_no_errors(self):
        self.assertEqual(run_check(PIPE).errors, [])

    def test_superseded_drafts_are_out(self):
        chips = {m.get("chip") for m in PIPE["modules"]}
        for path in ("cad/m1_culture_spec.json", "cad/m1b_cellprep_spec.json", "cad/m3_wash_spec.json"):
            self.assertNotIn(path, chips)
            block = schema_io.load_json(ROOT / path)["module"]
            self.assertEqual(block["status"], "superseded")
            self.assertTrue((ROOT / block["superseded_by"]).is_file())

    def test_handoffs_join_consecutive_modules_and_end_at_the_roadmap(self):
        mods = [m["module"] for m in PIPE["modules"]]
        for h in PIPE["handoffs"]:
            src, dst = h["from"].get("module"), h["to"].get("module")
            if src and dst:
                self.assertEqual(mods.index(dst), mods.index(src) + 1, h["id"])
        self.assertTrue(PIPE["handoffs"][-1]["to"].get("roadmap"))

    def test_tbc_ports_and_connector_mismatches_are_listed(self):
        warnings = " | ".join(run_check(PIPE).warnings)
        self.assertIn("TBC", warnings)
        for h in PIPE["handoffs"]:
            if (h.get("connector") or {}).get("status") == "match":
                self.assertNotIn(f"{h['id']}' connector", warnings)


class PipelineRules(unittest.TestCase):
    def handoff(self, predicate):
        return next(i for i, h in enumerate(PIPE["handoffs"]) if predicate(h))

    def test_a_superseded_chip_in_the_pipeline_is_an_error(self):
        pipe = copy.deepcopy(PIPE)
        i = next(i for i, m in enumerate(pipe["modules"]) if m["module"] == "module3")
        pipe["modules"][i]["chip"] = "cad/m3_wash_spec.json"
        pipe["modules"][i]["status"] = "draft"
        self.assertTrue(any("superseded" in e for e in run_check(pipe).errors))

    def test_a_wrong_connector_status_is_an_error(self):
        pipe = copy.deepcopy(PIPE)
        i = self.handoff(lambda h: (h.get("connector") or {}).get("status") == "match")
        pipe["handoffs"][i]["connector"]["status"] = "mismatch"
        pipe["handoffs"][i]["connector"]["warning"] = "x"
        hid = pipe["handoffs"][i]["id"]
        self.assertTrue(any(hid in e and "connector" in e for e in run_check(pipe).errors))

    def test_a_named_port_on_a_missing_module_is_an_error(self):
        # Take a real handoff and strip the chip off the module it points at, leaving the
        # port named. Building the violation is better than hunting for a live one: every
        # TBC was resolved when reverse transcription got a chip.
        pipe = copy.deepcopy(PIPE)
        h = pipe["handoffs"][0]
        target = h["to"]["module"]
        i = next(i for i, m in enumerate(pipe["modules"]) if m["module"] == target)
        pipe["modules"][i].pop("chip", None)
        pipe["modules"][i]["status"] = "missing"
        self.assertTrue(any("must be TBC" in e for e in run_check(pipe).errors))

    def test_a_missing_module_needs_no_chip_but_others_do(self):
        pipe = copy.deepcopy(PIPE)
        i = next(i for i, m in enumerate(pipe["modules"]) if m["status"] not in ("missing",) and m.get("chip"))
        del pipe["modules"][i]["chip"]
        self.assertTrue(run_check(pipe).errors)

    def test_a_module_with_a_prefer_glob_switches_when_the_file_appears(self):
        entry = {"module": "x", "status": "draft", "chip": "cad/x_spec.json",
                 "prefer": {"glob": "chip/x*.json", "status": "final", "status_note": "picked up automatically"}}
        self.assertEqual(schema_io.pipeline_chip(entry)[:2], ("cad/x_spec.json", "draft"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "chip").mkdir()
            (root / "chip" / "x_v1.json").write_text("{}")
            path, status, note = schema_io.pipeline_chip(entry, root)
            self.assertEqual((path, status), ("chip/x_v1.json", "final"))
            self.assertIn("automatically", note)


class Module3Translation(unittest.TestCase):
    SRC = schema_io.load_json(ROOT / convert_module3.SOURCE)
    SPEC, QUESTIONS = convert_module3.translate(SRC)

    def test_committed_translation_keeps_the_sources_parts_at_demo_scale(self):
        # The committed chip is the translation AFTER the 30 x 20 mm fit (tools/fit_chip.py), the removal of its
        # on-chip valves and the merging of ports (6-port limit), so it no longer equals a fresh translate().
        # It must still be the same chip: same source, same chambers, a connected network, no more ports than before.
        committed = schema_io.load_json(ROOT / convert_module3.OUT)
        self.assertEqual(committed["translated_from"], self.SPEC["translated_from"])
        self.assertEqual([x["id"] for x in committed["chambers"]], [x["id"] for x in self.SPEC["chambers"]])
        self.assertLessEqual(len(committed["ports"]), len(self.SPEC["ports"]))
        nodes = {x["id"] for g in ("chambers", "ports") for x in committed[g]}
        for ch in committed["channels"]:
            self.assertLessEqual({ch["from"], ch["to"]}, nodes, ch["id"])
        self.assertEqual(committed.get("valves", []), [], "single fluidic layer: no on-chip valves")

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
        errors = ccs.check(self.SPEC, [], params, materials, fab).errors
        self.assertEqual([e for e in errors if not e.startswith("port overlap:")], [])  # own rule, own test

    def test_questions_flag_the_valves_and_the_retention_gap(self):
        self.assertIn("7 valves", self.QUESTIONS[0])
        self.assertIn("no valves", self.QUESTIONS[0])
        self.assertIn("25 um", self.QUESTIONS[1])
        self.assertIn("laser-cuttable", self.QUESTIONS[1])


if __name__ == "__main__":
    unittest.main()

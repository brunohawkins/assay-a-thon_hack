"""autochip stages 1 + 2: ingest (file / paper / prompt -> text) and extract (text -> protocol IR).

All offline: the rules backend, plus saved agent answers replayed through the validator.

    python3 -m pytest -q tests/test_autochip_extract.py
"""
import contextlib
import copy
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autochip import constraints as C  # noqa: E402
from autochip import ingest  # noqa: E402
from autochip.llm import Agent, AgentError  # noqa: E402
from autochip.stages import extract  # noqa: E402

EX = ROOT / "autochip" / "examples"
FIX = ROOT / "tests" / "fixtures" / "autochip" / "extract"
PROMPT = (EX / "capsule_scrnaseq_prompt.txt").read_text()
SPC_ACTIONS = {"culture", "dissociate", "quench", "centrifuge", "count", "encapsulate", "break_emulsion", "cure",
               "lyse", "reverse_transcribe", "split", "amplify", "release", "bind_beads", "wash_particles", "elute",
               "measure"}


def can_read_pdf() -> bool:
    if shutil.which("pdftotext"):
        return True
    try:
        import pypdf  # noqa: F401
        return True
    except ImportError:
        return False


def rules_ir(path=None, prompt=None):
    src = ingest.load_source(str(path) if path else None, prompt)
    ir = extract.run(src, C.load(), Agent(backend="rules", quiet=True), None)
    return src, ir


def actions(ir):
    return [s["action"] for s in ir["steps"]]


class TestIngest(unittest.TestCase):
    def test_prompt_only(self):
        s = ingest.load_source(None, PROMPT)
        self.assertEqual(s["kind"], "prompt")
        self.assertIsNone(s["file"])
        self.assertEqual(s["text"], PROMPT.strip())
        self.assertEqual(s["prompt"], PROMPT.strip())
        self.assertEqual(s["chars"], len(s["text"]))
        self.assertTrue(s["title"])

    def test_protocol_file_with_prompt_keeps_both(self):
        s = ingest.load_source(str(EX / "spc_capsule_protocol.md"), "only the capsule part")
        self.assertEqual(s["kind"], "protocol")
        self.assertEqual(s["prompt"], "only the capsule part")
        self.assertIn("Photocure", s["text"])
        self.assertEqual(s["title"], "Semi-permeable capsule (SPC) workflow: generic protocol")

    def test_errors(self):
        with self.assertRaises(ingest.IngestError):
            ingest.load_source(None, None)
        with self.assertRaises(ingest.IngestError):
            ingest.load_source(str(EX / "no_such_file.md"), None)

    @unittest.skipUnless(can_read_pdf(), "needs pdftotext or pypdf")
    def test_pdf_is_a_paper_with_methods_first(self):
        s = ingest.load_source(str(EX / "spc_methods_paper.pdf"), None)
        self.assertEqual(s["kind"], "paper")
        self.assertTrue(s["title"].startswith("Processing single cells inside semi-permeable capsules"))
        self.assertGreater(s["methods_chars"], 500)
        methods, rest = s["text"][:s["methods_chars"]], s["text"][s["methods_chars"]:]
        self.assertIn("Cell preparation", methods)
        self.assertIn("in water", methods)
        self.assertNotIn("Abstract", methods)
        self.assertIn("Abstract", rest)
        self.assertLess(s["text"].index("Methods"), s["text"].index("Abstract"))

    def test_long_text_with_paper_headings_is_a_paper(self):
        body = "Some words about the work. " * 60
        text = (f"A study title\n\nAbstract\n{body}\n\nIntroduction\n{body}\n\nMethods\n"
                "Cells were cultured at 37 °C. Cells were washed twice.\nMore methods text here.\n\n"
                f"Results\n{body}\n\nReferences\n1. Someone et al. 2020.\n")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "paper.txt"
            p.write_text(text)
            s = ingest.load_source(str(p), None)
        self.assertEqual(s["kind"], "paper")
        self.assertTrue(s["text"].startswith(ingest.METHODS_MARK))
        self.assertIn("washed twice", s["text"][:s["methods_chars"]])

    def test_docx_numbered_list_and_table(self):
        s = ingest.load_source(str(FIX / "bead_cleanup.docx"), None)
        self.assertEqual(s["kind"], "protocol")
        self.assertEqual(s["title"], "Bead clean-up (generic)")
        self.assertIn("1. Mix the DNA sample", s["text"])
        self.assertIn("6. Place on the magnet", s["text"])
        self.assertIn("| elution buffer | elute |", s["text"])

    def test_xlsx_and_csv_are_tables(self):
        for name in ("bead_cleanup.xlsx", "bead_cleanup_table.csv"):
            s = ingest.load_source(str(FIX / name), None)
            self.assertIn("| Step | Description | Volume (µL) | Time (min) | Temp (°C) |", s["text"], name)
            self.assertIn("| 3 | Wash the beads with ethanol wash solution on the magnet, twice | 200 | 0.5 |", s["text"], name)

    def test_protocol_json_rendered_as_steps(self):
        s = ingest.load_source(str(FIX / "capsule_exchange_protocol.json"), None)
        self.assertEqual(s["title"], "Capsule exchange demo")
        self.assertIn("3. Wash the capsules.", s["text"])
        self.assertIn("3 washes", s["text"])


class TestRulesExtract(unittest.TestCase):
    def check_valid(self, src, ir):
        self.assertEqual(extract.validate(ir, src), [])
        self.assertEqual(ir["source"]["kind"], src["kind"])
        self.assertEqual([s["n"] for s in ir["steps"]], list(range(1, len(ir["steps"]) + 1)))

    def test_spc_protocol(self):
        src, ir = rules_ir(EX / "spc_capsule_protocol.md")
        self.check_valid(src, ir)
        self.assertTrue(22 <= len(ir["steps"]) <= 32, len(ir["steps"]))
        self.assertTrue(SPC_ACTIONS <= set(actions(ir)), SPC_ACTIONS - set(actions(ir)))
        a = actions(ir)
        self.assertLess(a.index("encapsulate"), a.index("break_emulsion"))
        self.assertLess(a.index("lyse"), a.index("reverse_transcribe"))
        self.assertLess(a.index("reverse_transcribe"), a.index("amplify"))
        self.assertLess(a.index("release"), a.index("bind_beads"))
        self.assertLess(a.index("bind_beads"), a.index("elute"))
        by = {}
        for s in ir["steps"]:
            by.setdefault(s["action"], s)                                      # first step of each action
        cures = [s for s in ir["steps"] if s["action"] == "cure"]
        self.assertEqual(len(cures), 1)                                       # only the light cure is a cure
        self.assertEqual(cures[0]["conditions"]["light_nm"], 405)
        self.assertEqual(cures[0]["duration_s"], 30)
        cold = [s for s in ir["steps"] if "on ice" in s["text"]][0]           # the shell gels in a cold hold
        self.assertEqual(cold["action"], "incubate")
        self.assertEqual((cold["conditions"].get("cold"), cold["temp_c"], cold["duration_s"]), (True, 4, 1800))
        self.assertEqual(a[a.index("encapsulate") + 1:a.index("break_emulsion")], ["collect", "incubate"])
        # the example gives no cycle number, RT temperature or time (placeholders): none may be invented
        self.assertIsNone((by["amplify"].get("conditions") or {}).get("thermal_cycles"))
        self.assertIsNone(by["reverse_transcribe"].get("temp_c"))
        self.assertIsNone(by["reverse_transcribe"].get("duration_s"))
        self.assertEqual(by["encapsulate"]["on_chip_hint"], "required")
        self.assertEqual(by["encapsulate"]["flow_ul_min"], 2)
        self.assertIn("oil 10", by["encapsulate"]["note"])
        self.assertEqual(by["centrifuge"]["on_chip_hint"], "off_chip")
        self.assertIn("300 g", by["centrifuge"]["note"])
        self.assertEqual(by["encapsulate"]["input_material"], "cells")
        self.assertEqual(by["encapsulate"]["output_material"], "droplets")
        self.assertEqual(by["bind_beads"]["output_material"], "beads")
        washes = [s["repeats"] for s in ir["steps"] if s["action"] == "wash_particles"]
        self.assertEqual(washes, [3, 3, 2, 2])
        self.assertTrue(by["wash_particles"]["conditions"] == {} and ir["steps"][-3]["conditions"].get("magnet"))
        ids = {r["id"] for r in ir["reagents"]}
        self.assertTrue({"lysis_buffer", "rt_mix", "pcr_mix", "shell_polymer", "core_solution", "fluorinated_oil",
                         "magnetic_beads", "photoinitiator"} <= ids)
        self.assertTrue(ir["placeholder"])
        self.assertIn("Leonaviciene & Mazutis", ir["source"]["citation"])
        self.assertEqual(ir["id"], "spc_capsule_protocol")

    def test_bead_cleanup(self):
        src, ir = rules_ir(EX / "bead_cleanup_protocol.md")
        self.check_valid(src, ir)
        self.assertTrue(6 <= len(ir["steps"]) <= 10, len(ir["steps"]))
        a = actions(ir)
        self.assertEqual(a[0], "bind_beads")
        self.assertTrue({"magnetic_separate", "wash_particles", "elute", "measure"} <= set(a))
        wash = ir["steps"][a.index("wash_particles")]
        self.assertEqual(wash["repeats"], 2)
        self.assertTrue(wash["conditions"]["magnet"])
        self.assertEqual(wash["volume_ul"], 200)
        self.assertEqual(ir["steps"][0]["temp_c"], 22)          # room temperature
        self.assertEqual(ir["steps"][0]["input_material"], "nucleic_acid")
        elute = ir["steps"][a.index("elute")]
        self.assertNotIn("magnet", elute["conditions"])           # "remove from the magnet" is not on a magnet
        self.assertEqual(elute["reagents"], ["elution_buffer"])

    def test_prompt_only(self):
        src, ir = rules_ir(prompt=PROMPT)
        self.check_valid(src, ir)
        self.assertTrue(10 <= len(ir["steps"]) <= 20, len(ir["steps"]))
        self.assertTrue(ir["placeholder"])
        self.assertTrue(all(s["assumed"] for s in ir["steps"]))
        self.assertTrue(ir["assumptions"])
        want = {"culture", "dissociate", "encapsulate", "break_emulsion", "cure", "lyse", "wash_particles",
                "reverse_transcribe", "split", "amplify", "release", "bind_beads", "elute"}
        self.assertTrue(want <= set(actions(ir)), want - set(actions(ir)))
        a = actions(ir)
        self.assertEqual(a.index("amplify"), a.index("split") + 1)   # "split ... for index PCR"

    @unittest.skipUnless(can_read_pdf(), "needs pdftotext or pypdf")
    def test_methods_paper_pdf(self):
        src, ir = rules_ir(EX / "spc_methods_paper.pdf")
        self.check_valid(src, ir)
        self.assertEqual(ir["source"]["kind"], "paper")
        self.assertTrue(22 <= len(ir["steps"]) <= 32, len(ir["steps"]))
        self.assertTrue(SPC_ACTIONS <= set(actions(ir)), SPC_ACTIONS - set(actions(ir)))
        self.assertTrue(all(s["source_ref"].startswith("2.") for s in ir["steps"]))   # all from the Methods section
        self.assertEqual([s["conditions"].get("light_nm") for s in ir["steps"] if s["action"] == "cure"], [405])
        self.assertIn("incubate", actions(ir))
        self.assertIn([s for s in ir["steps"] if s["action"] == "wash_particles"][0]["repeats"], (3,))

    def test_table_and_docx_fixtures(self):
        for name in ("bead_cleanup_table.csv", "bead_cleanup.xlsx", "bead_cleanup.docx"):
            src, ir = rules_ir(FIX / name)
            self.check_valid(src, ir)
            a = actions(ir)
            self.assertTrue(6 <= len(a) <= 8, (name, a))
            self.assertEqual(a[0], "bind_beads", name)
            self.assertIn("elute", a, name)
            self.assertEqual(ir["steps"][0]["volume_ul"], 40, name)
            self.assertEqual(ir["steps"][0]["duration_s"], 300, name)
            self.assertEqual(ir["steps"][a.index("wash_particles")]["repeats"], 2, name)

    def test_protocol_json_fixture(self):
        src, ir = rules_ir(FIX / "capsule_exchange_protocol.json")
        self.check_valid(src, ir)
        self.assertEqual(actions(ir), ["transfer", "lyse", "wash_particles", "release"])
        self.assertEqual(ir["steps"][0]["input_material"], "capsules")
        self.assertEqual(ir["steps"][2]["repeats"], 3)
        self.assertEqual(ir["steps"][3]["temp_c"], 37)

    def test_unstructured_text_still_gives_a_valid_ir(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "notes.txt"
            p.write_text("Nothing here looks like a step at all\n")
            src, ir = rules_ir(p)
        self.assertEqual(extract.validate(ir, src), [])
        self.assertEqual(len(ir["steps"]), 1)


class TestQuantities(unittest.TestCase):
    def test_units_and_ranges(self):
        q = extract._quantities("Keep on ice for 15–30 min (0.5 mL) at 4 °C", "incubate")
        self.assertEqual(q["duration_s"], 1800)
        self.assertEqual(q["volume_ul"], 500)
        self.assertEqual(q["temp_c"], 4)
        self.assertEqual(extract._quantities("incubate overnight", "incubate")["duration_s"], 16 * 3600)
        self.assertEqual(extract._quantities("flow 30 uL/h", "transfer")["flow_ul_min"], 0.5)
        self.assertEqual(extract._quantities("hold 2 h at 37C", "incubate")["temp_c"], 37)

    def test_repeats(self):
        self.assertEqual(extract._repeats("wash twice"), 2)
        self.assertEqual(extract._repeats("Wash x3 in buffer"), 3)
        self.assertEqual(extract._repeats("wash 3x"), 3)
        self.assertEqual(extract._repeats("repeat the wash once"), 2)
        self.assertEqual(extract._repeats("discard. Repeat once (2 washes in total)"), 2)
        self.assertIsNone(extract._repeats("load 2 x 10^6 cells/mL"))
        self.assertIsNone(extract._repeats("beads (40 µL, 0.8x)"))
        self.assertIsNone(extract._repeats("rinse once with buffer"))


class TestValidateAndAgentPath(unittest.TestCase):
    def setUp(self):
        self.src, self.ir = rules_ir(EX / "bead_cleanup_protocol.md")

    def test_semantic_errors_are_caught(self):
        bad = copy.deepcopy(self.ir)
        bad["steps"][1]["n"] = 7
        bad["steps"][0]["reagents"].append("mystery_mix")
        bad["steps"][2]["input_material"] = "cells"
        bad["steps"][3]["duration_s"] = -5
        bad["steps"][4]["action"] = "centrifuge"
        bad["steps"][2]["repeats"] = None
        bad["steps"][5]["note"] = "primer ACGTACGTACGTACGT"
        del bad["steps"][6]["on_chip_hint"]
        errs = " | ".join(extract.validate(bad, self.src))
        for want in ("n must run", "mystery_mix", "input_material 'cells'", "duration_s is negative",
                     "action 'centrifuge'", "repeats is not set", "nucleotide sequences", "on_chip_hint missing"):
            self.assertIn(want, errs)

    def test_cold_hold_is_not_a_cure(self):
        src, ir = rules_ir(EX / "spc_capsule_protocol.md")
        bad = copy.deepcopy(ir)
        step = [s for s in bad["steps"] if "on ice" in s["text"]][0]
        step["action"] = "cure"
        step["conditions"] = {}
        errs = " | ".join(extract.validate(bad, src))
        self.assertIn("'cure' is only for light, UV or chemical crosslinking", errs)
        self.assertIn("on ice but conditions.cold is not true", errs)

    def test_live_agent_failure_falls_back_loudly(self):
        class FailingAgent:
            live = True

            def describe(self):
                return "test agent"

            def ask_json(self, *a, **k):
                raise AgentError("extract: no valid answer after 3 tries")

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            ir = extract.run(self.src, C.load(), FailingAgent(), None)
        self.assertIn("FALLING BACK", err.getvalue())
        self.assertTrue(ir["assumptions"][0].startswith(extract.FALLBACK_PREFIX))
        self.assertEqual(extract.validate(ir, self.src), [])

    def test_prompt_source_rules(self):
        src = ingest.load_source(None, PROMPT)
        _, ir = rules_ir(prompt=PROMPT)
        bad = copy.deepcopy(ir)
        bad["placeholder"] = False
        bad["assumptions"] = []
        bad["steps"][0]["assumed"] = False
        bad["steps"][0]["duration_s"] = 600
        errs = " | ".join(extract.validate(bad, src))
        self.assertIn("placeholder must be true", errs)
        self.assertIn("assumptions must list", errs)
        self.assertIn("set assumed: true", errs)

    def test_user_prompt_has_source_goal_constraints_and_schema(self):
        src = ingest.load_source(str(EX / "spc_capsule_protocol.md"), "only the capsule part")
        u = extract.user_prompt(src, C.load("assayathon_demo"))
        self.assertIn("Photocure the capsule shells", u)
        self.assertIn("only the capsule part", u)
        self.assertIn("Constraints 'assayathon_demo'", u)
        self.assertIn('"protocol_ir"', u)
        system = extract.PROMPT_PATH.read_text()
        for rule in ("NEVER write nucleotide sequences", "assumed: true", "on_chip_hint", "repeats", "snake_case"):
            self.assertIn(rule, system)

    def test_agent_path_with_replay(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "extract.json").write_text(json.dumps(self.ir))
            agent = Agent(backend="replay", replay_dir=Path(d), quiet=True)
            got = extract.run(self.src, C.load(), agent, Path(d))
            self.assertEqual(got["steps"], self.ir["steps"])
            broken = copy.deepcopy(self.ir)
            broken["steps"][0]["reagents"] = ["undeclared"]
            (Path(d) / "extract.json").write_text(json.dumps(broken))
            with self.assertRaises(AgentError):
                extract.run(self.src, C.load(), Agent(backend="replay", replay_dir=Path(d), quiet=True), Path(d))

    def test_saved_live_answers_still_validate(self):
        """Accepted answers from real agent runs (fixtures) must keep passing the validator."""
        cases = {"live_spc_capsule_protocol.json": (str(EX / "spc_capsule_protocol.md"), None),
                 "live_capsule_scrnaseq_prompt.json": (None, PROMPT)}
        found = 0
        for name, (path, prompt) in cases.items():
            f = FIX / name
            if not f.exists():
                continue
            found += 1
            src = ingest.load_source(path, prompt)
            self.assertEqual(extract.validate(json.loads(f.read_text()), src), [], name)
        self.assertGreater(found, 0)


if __name__ == "__main__":
    unittest.main()

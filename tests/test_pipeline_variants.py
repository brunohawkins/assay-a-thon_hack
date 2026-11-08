"""The two assay variants (CapSeq, TIP-seq) and pipeline_mvp.json pointing at one of them.

    python -m unittest discover -s tests -v
"""

import copy
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "tools"))

import choose_assay  # noqa: E402
import schema_io  # noqa: E402
import validate  # noqa: E402

CAP = schema_io.load_json(ROOT / "protocols" / "pipeline_capseq.json")
TIP = schema_io.load_json(ROOT / "protocols" / "pipeline_tipseq.json")
MVP = schema_io.load_json(ROOT / "protocols" / "pipeline_mvp.json")


def errors(pipe):
    report = validate.Report()
    validate.check_pipeline("pipeline", pipe, report)
    return report.errors


def chips(pipe):
    return [m.get("chip", f"({m['status']})") for m in pipe["modules"]]


class Variants(unittest.TestCase):
    def test_both_validate(self):
        self.assertEqual(errors(CAP), [])
        self.assertEqual(errors(TIP), [])

    def test_capseq_chain_with_its_two_placeholders(self):
        self.assertEqual(chips(CAP), ["chip/module1_v1.json", "chip/cell_prep_v1.json", "chip/spc_demo_v1.json",
                                      "chip/module3_v1_translated.json", "(missing)", "(missing)",
                                      "chip/module4_v1.json", "chip/module5_v1.json"])
        ghosts = [m["name"] for m in CAP["modules"] if m["status"] == "missing"]
        self.assertEqual(ghosts, ["Barcoding: split and pool (BC2, BC3)", "Hairpin ligation + pooled wash"])

    def test_tipseq_releases_before_index_pcr(self):
        self.assertEqual(chips(TIP), ["chip/module1_v1.json", "(bench)", "chip/spc_demo_v1.json", "chip/sctip_spc_v1.json",
                                      "chip/module5_v1.json", "chip/module4_v1.json"])
        release = next(h for h in TIP["handoffs"] if h["id"] == "release_to_index_pcr")
        self.assertEqual((release["from"]["module"], release["to"]["module"]), ("module5", "module4"))

    def test_placeholders_have_no_chip_and_only_tbc_ports(self):
        for pipe in (CAP, TIP):
            holes = {m["module"] for m in pipe["modules"] if m["status"] in ("missing", "bench")}
            for m in pipe["modules"]:
                if m["module"] in holes:
                    self.assertNotIn("chip", m)
            for h in pipe["handoffs"]:
                for side in ("from", "to"):
                    if h[side].get("module") in holes:
                        self.assertEqual(h[side].get("ports", [h[side].get("port")]), ["TBC"], h["id"])


class ChosenVariant(unittest.TestCase):
    def test_mvp_is_a_copy_of_capseq_by_default(self):
        self.assertEqual((MVP["same_as"], MVP["variant"]), ("protocols/pipeline_capseq.json", "capseq"))
        self.assertEqual(errors(MVP), [])

    def test_a_drifted_copy_is_an_error(self):
        drifted = copy.deepcopy(MVP)
        drifted["modules"][0]["note"] = "edited by hand"
        self.assertTrue(any("choose_assay" in e for e in errors(drifted)))

    def test_choose_writes_a_valid_copy_of_either_variant(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pipeline_mvp.json"
            for v, src in (("tipseq", TIP), ("capseq", CAP)):
                data = choose_assay.choose(v, out)
                self.assertEqual(data["modules"], src["modules"])
                self.assertEqual(errors(schema_io.load_json(out)), [])


if __name__ == "__main__":
    unittest.main()

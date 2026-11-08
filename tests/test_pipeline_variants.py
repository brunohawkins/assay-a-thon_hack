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

    def test_capseq_is_mjs_chain_and_barcodes_between_the_trap_and_the_split(self):
        """CapSeq is MJ's chain copied unchanged, and that chain BARCODES PER CELL.

        Plate-based split-and-pool was dropped (fcc6ef2) and its hairpin ligation with it, so
        neither may appear. What replaced them is NOT nothing: stage 4b, one clonal barcode per
        capsule generated in that capsule's own oil droplet and overlap-extended onto its cDNA
        (protocols/pipeline_status.md, which is the source of truth and says the same). So the
        placeholders are asserted GONE and the real stage is asserted PRESENT, in that position.
        """
        self.assertEqual(chips(CAP), ["chip/module1_v1.json", "chip/cell_prep_v1.json", "chip/spc_demo_v1.json",
                                      "chip/module3_v1_translated.json", "chip/sctip_spc_v1.json",
                                      "chip/module4_v1.json", "chip/module5_v1.json"])
        order = [m["module"] for m in CAP["modules"]]
        self.assertEqual(order[order.index("module3") + 1], "barcoding", "barcoding sits between module 3 and module 4")
        self.assertEqual(order[order.index("barcoding") + 1], "module4")
        self.assertNotIn("hairpin", order)
        self.assertFalse([m for m in CAP["modules"] if m["status"] in ("missing", "bench")])
        bc = next(m for m in CAP["modules"] if m["module"] == "barcoding")
        self.assertEqual(bc["protocol"], "protocols/sctip_spc_v1.json")
        # Our chemistry, bought droplet hardware: the stage must say so, not read as a part we make.
        self.assertIn("NOT PRINTABLE", bc["build"]["method"].upper())
        self.assertEqual([h["id"] for h in CAP["handoffs"] if "barcoding" in (h["from"].get("module"), h["to"].get("module"))],
                         ["module3_to_barcoding", "barcoding_to_module4"])

    def test_no_variant_has_split_and_pool_or_hairpin_ligation(self):
        """Per-cell barcoding is one clonal barcode per capsule in its own oil droplet, overlap-extended
        onto the cDNA: no split-and-pool rounds, and so no hairpin ligation to close them."""
        for pipe in (CAP, TIP, MVP):
            for m in pipe["modules"]:
                self.assertNotEqual(m["module"], "hairpin", pipe["id"])
                text = f"{m.get('name', '')} {m.get('status_note', '')}".lower()
                for word in ("split and pool", "split-and-pool", "split-pool", "hairpin", "bc2", "bc3"):
                    self.assertNotIn(word, text, f"{pipe['id']}: {m['module']}")
            for h in pipe["handoffs"]:
                self.assertNotIn("hairpin", (h["from"].get("module"), h["to"].get("module")), h["id"])

    def test_capseq_refuses_a_base_with_the_old_placeholders(self):
        import build_assay_variants
        old = copy.deepcopy(CAP)
        old["modules"].insert(4, {"module": "hairpin", "name": "Hairpin ligation", "status": "missing"})
        with self.assertRaises(build_assay_variants.BarcodingStageError):
            build_assay_variants.capseq(old)
        # A 'barcoding' module with no chip is the dropped split-and-pool placeholder, not stage 4b.
        placeholder = copy.deepcopy(CAP)
        placeholder["modules"][4] = {"module": "barcoding", "name": "Barcoding", "status": "missing"}
        with self.assertRaises(build_assay_variants.BarcodingStageError):
            build_assay_variants.capseq(placeholder)

    def test_capseq_refuses_a_base_that_has_lost_the_real_barcoding_stage(self):
        """The other direction, and the one that actually happened: the chain was rebuilt from an
        older copy of itself, the barcoding stage silently vanished, and the loss was then written
        up as a design choice. A base with no stage 4b is now an error with an explanation."""
        import build_assay_variants
        stripped = copy.deepcopy(CAP)
        stripped["modules"] = [m for m in stripped["modules"] if m["module"] != "barcoding"]
        stripped["handoffs"] = [h for h in stripped["handoffs"]
                                if "barcoding" not in (h["from"].get("module"), h["to"].get("module"))]
        with self.assertRaises(build_assay_variants.BarcodingStageError) as caught:
            build_assay_variants.capseq(stripped)
        for expected in ("stage 4b", "single-cell", "oil droplet", "will not be regenerated"):
            self.assertIn(expected, str(caught.exception))

    def test_the_six_printed_chips_are_flagged_and_nothing_else_is(self):
        """built_for_demo is set by the BUILDER (PRINTED_FOR_DEMO), so a rebuild keeps it. Module 6
        is reached as the roadmap branch module6_bulk_omics, so its flag sits on that entry."""
        import build_assay_variants
        for pipe in (CAP, MVP, TIP):
            for m in pipe["modules"]:
                want = bool(m.get("chip")) and Path(m["chip"]).stem in build_assay_variants.PRINTED_FOR_DEMO
                self.assertEqual(m.get("built_for_demo"), want, f"{pipe['id']}: {m['module']}")
            for r in pipe.get("roadmap", []):
                if r["id"] in build_assay_variants.PRINTED_ROADMAP:
                    self.assertTrue(r.get("built_for_demo"), f"{pipe['id']}: roadmap {r['id']}")
        flagged = {Path(m["chip"]).stem for m in CAP["modules"] if m.get("built_for_demo")}
        self.assertEqual(flagged, {"module1_v1", "spc_demo_v1", "module3_v1_translated", "module4_v1", "module5_v1"})
        self.assertFalse(next(m for m in CAP["modules"] if m["module"] == "cellprep")["built_for_demo"])
        self.assertFalse(next(m for m in CAP["modules"] if m["module"] == "barcoding")["built_for_demo"])

    def test_rebuilding_from_the_output_is_stable(self):
        """The docstring tells you to re-run with --base protocols/pipeline_mvp.json, so the output
        has to be a valid input. It was not: every rebuild stacked another "CapSeq (RNA): " onto
        the name and another CAPSEQ VARIANT paragraph onto the description."""
        import build_assay_variants
        once = build_assay_variants.flag_printed(build_assay_variants.capseq(copy.deepcopy(MVP)))
        twice = build_assay_variants.flag_printed(build_assay_variants.capseq(copy.deepcopy(once)))
        self.assertEqual(once, twice)
        self.assertEqual(once["name"], CAP["name"])
        self.assertEqual(once["name"].count("CapSeq (RNA):"), 1)
        self.assertEqual(once["description"].count("CAPSEQ VARIANT:"), 1)

    def test_the_chain_keeps_module_6_and_module_4s_real_outlet(self):
        """Two things an earlier rebuild dropped: the bulk-omics arm off culture, and Module 4's
        product outlet (it used to be a flat TBC, with the library sealed in the chip)."""
        for pipe in (CAP, MVP):
            self.assertIn("module6_bulk_omics", [r["id"] for r in pipe["roadmap"]])
            m6 = next(h for h in pipe["handoffs"] if h["to"].get("roadmap") == "module6_bulk_omics")
            self.assertEqual(m6["from"]["module"], "module1")
            m45 = next(h for h in pipe["handoffs"] if h["id"] == "module4_to_module5")
            self.assertEqual(m45["from"]["port"], "out_sub")
        # TIP-seq reads the same outlet out of the chain rather than keeping its own stale copy.
        self.assertEqual(next(h for h in TIP["handoffs"] if h["id"] == "index_pcr_to_roadmap")["from"]["port"], "out_sub")

    def test_no_capseq_port_is_still_tbc(self):
        for pipe in (CAP, MVP):
            for h in pipe["handoffs"]:
                for side in ("from", "to"):
                    for pid in h[side].get("ports", [h[side].get("port")]):
                        self.assertNotEqual(pid, "TBC", f"{pipe['id']}: {h['id']} {side}")

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

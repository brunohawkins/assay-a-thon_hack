"""Module 1b (automated cell prep; superseded by Module 1 v1, kept for reference).

    python -m unittest discover -s tests -v
"""

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "tools"))

import cellprep  # noqa: E402
import check_chip_spec as ccs  # noqa: E402
import safety  # noqa: E402
import schema_io  # noqa: E402

M1B = schema_io.load_json(ROOT / "cad" / "m1b_cellprep_spec.json")
M2 = schema_io.load_json(ROOT / "cad" / "m2_encapsulation_spec.json")
PROTO = schema_io.load_json(ROOT / "protocols" / "m1b_cellprep.json")
TARGET = safety.cell_prep_target(M2)


def scenario(name):
    path = ROOT / "scenarios" / f"m1b_cellprep_{name}.json"
    scn = schema_io.load_json(path)
    return cellprep.run(PROTO, M1B, ROOT / "cad" / "m1b_cellprep_spec.json", scn, path)


class SafetyGate(unittest.TestCase):
    def test_target_comes_from_module_2_flows(self):
        # 0.1 cells per capsule at 100 uL/h of core stream and ~1.95 M capsules/h -> ~1,950 cells/uL
        self.assertAlmostEqual(TARGET["target_cells_per_ul"], 1948.8, delta=1)

    def test_on_target_and_viable_is_approved(self):
        self.assertEqual(safety.check_cell_prep(1950, 0.93, M2).status, "approved")

    def test_low_viability_is_blocked(self):
        r = safety.check_cell_prep(1950, 0.85, M2)
        self.assertEqual(r.status, "blocked")
        self.assertIn("viability 85% is below 90%", r.reasons[0])

    def test_too_concentrated_says_how_much_to_dilute(self):
        r = safety.check_cell_prep(3900, 0.95, M2)
        self.assertEqual(r.status, "blocked")
        self.assertIn("too concentrated - dilute 2.0x", r.reasons[0])

    def test_too_dilute_says_how_much_to_concentrate(self):
        r = safety.check_cell_prep(975, 0.95, M2)
        self.assertIn("too dilute - concentrate 2.0x", r.reasons[0])


class Operator(unittest.TestCase):
    def test_operator_dilutes_a_concentrated_sample_itself(self):
        d, why = cellprep.choose_dilution({"cells_per_ul": 4000, "viability": 0.93}, TARGET)
        self.assertAlmostEqual(d, 4000 / TARGET["target_cells_per_ul"], places=2)
        self.assertIn("diluting", why)

    def test_demo_run_reaches_module_2(self):
        run = scenario("demo")
        self.assertEqual(run["status"], "done")
        self.assertEqual(run["events"][-1]["args"]["target"], "m1b_to_module2_1")
        self.assertTrue(TARGET["mean_low"] <= run["summary"]["mean_cells_per_capsule"] <= TARGET["mean_high"])
        kinds = [x["kind"] for x in run["log"]]
        self.assertEqual(kinds, ["measurement", "decision", "safety"])
        self.assertEqual(schema_io.schema_errors("run", run), [])

    def test_low_viability_stops_before_module_2(self):
        run = scenario("low_viability")
        self.assertEqual(run["status"], "stopped")
        self.assertNotIn("m1b_to_module2_1", [e["args"].get("target") for e in run["events"]])
        self.assertTrue(any(x["kind"] == "blocked" and "viability" in x["message"] for x in run["log"]))

    def test_too_dilute_stops_with_the_fix(self):
        run = scenario("too_dilute")
        self.assertEqual(run["status"], "stopped")
        self.assertTrue(any("concentrate" in x["message"] for x in run["log"] if x["kind"] == "blocked"))


class CrossChipFilter(unittest.TestCase):
    """The cross-chip rule (a cell filter must be finer than the nozzle it feeds) on the
    SHIPPED pipeline, plus the same rule on M1B as a reference-only regression."""

    def check(self, chip, downstream):
        params = schema_io.load_json(ROOT / "contracts" / "parameters.json")
        materials = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
        return ccs.check(chip, [], params, materials, {"laser_kerf_um": None, "min_embossed_feature_um": None}, downstream)

    def other_errors(self, findings):
        """Everything except port overlap, which is its own rule with its own test
        (tests/test_port_overlap.py): the CAD generator stops channels at the port edge, so
        every hand-designed chip fails it today and it would mask the rule under test here."""
        return [e for e in findings.errors if not e.startswith("port overlap:")]

    def pipeline(self):
        _, pipe = schema_io.chosen_pipeline()
        chips = {m["module"]: schema_io.load_json(ROOT / schema_io.pipeline_chip(m)[0])
                 for m in pipe["modules"] if schema_io.pipeline_chip(m)[0]}
        return pipe, chips

    def nozzle_of(self, chip):
        return ((chip or {}).get("encapsulation") or {}).get("nozzle_um")

    def cell_filters(self, chip):
        """(id, gap_um) for every declared cell filter: the chip-level one and filter chambers."""
        out = [(c["id"], c["filter"]["gap_um"]) for c in chip.get("chambers", [])
               if (c.get("filter") or {}).get("purpose") == "cells"]
        gap = (chip.get("cell_filter") or {}).get("gap_um")
        return out + ([("cell_filter", gap)] if gap is not None else [])

    def test_m1b_is_out_of_the_pipeline(self):
        self.assertEqual(ccs.downstream_nozzles(M1B), [])           # superseded: no longer in the pipeline

    def test_every_handoff_in_the_pipeline_is_classified_and_asserted(self):
        """Each hand-off either feeds a declared nozzle (and downstream_nozzles must say so)
        or it does not (and downstream_nozzles must stay silent) - with the reason named.
        Previously this loop ran over all the hand-offs and only one of them asserted."""
        pipe, chips = self.pipeline()
        fed = 0
        for h in pipe["handoffs"]:
            with self.subTest(handoff=h["id"]):
                src, dst = chips.get(h["from"].get("module")), chips.get(h["to"].get("module"))
                nozzle = self.nozzle_of(dst)
                port = h["from"].get("port")
                entries = [e for e in ccs.downstream_nozzles(src) if e[2] == h["id"]] if src else []
                ports = {p["id"] for p in (src or {}).get("ports", [])}
                if src and nozzle and port in ports:
                    fed += 1
                    self.assertEqual(entries, [(nozzle, dst["id"], h["id"])])
                    for fid, gap in self.cell_filters(src):
                        self.assertLess(gap, nozzle, f"{src['id']} filter '{fid}' feeds a {nozzle:g} um nozzle")
                else:
                    self.assertEqual(entries, [], "nothing downstream to check against")
                    why = (not src, nozzle is None, port not in ports)
                    self.assertTrue(any(why), f"{h['id']}: a fed nozzle must be reported, not skipped")
        self.assertTrue(fed, "no hand-off in the pipeline feeds a declared nozzle, so the rule never fires")

    def test_the_pipelines_encapsulation_chip_declares_its_nozzle(self):
        # Otherwise the upstream cell filter is never checked against it (check_chip_spec.downstream_nozzles is silent).
        pipe_path, pipe = schema_io.chosen_pipeline()
        entry = next(m for m in pipe["modules"] if m["module"] == "module2")
        chip_path = schema_io.pipeline_chip(entry)[0]
        chip = schema_io.load_json(ROOT / chip_path)
        self.assertTrue((chip.get("encapsulation") or {}).get("nozzle_um"),
                        f"{chip_path} has no encapsulation.nozzle_um, so no upstream filter is checked against it")

    def test_the_chip_feeding_the_encapsulation_nozzle_declares_a_cell_filter(self):
        """Without a declared filter upstream, nothing in the repo checks clump size against
        the nozzle: check_chip_spec reports the nozzle but has no gap to compare it with."""
        pipe, chips = self.pipeline()
        h = next(h for h in pipe["handoffs"] if self.nozzle_of(chips.get(h["to"].get("module"))))
        src, dst = chips[h["from"]["module"]], chips[h["to"]["module"]]
        nozzle = self.nozzle_of(dst)
        filters = self.cell_filters(src)
        self.assertTrue(filters, f"{src['id']} feeds {dst['id']}'s {nozzle:g} um nozzle through {h['id']} but declares "
                                 f"no cell filter, so clumps reaching the nozzle are unchecked")
        f = self.check(src, ccs.downstream_nozzles(src))
        self.assertEqual(self.other_errors(f), [])
        self.assertTrue([o for o in f.ok if "nozzle" in o], "the checker should confirm the filter against the nozzle")

    def test_filter_smaller_than_the_downstream_nozzle_is_confirmed(self):
        # REFERENCE ONLY: M1B is superseded and out of the pipeline, so it has no real
        # downstream nozzle. The nozzle is the SHIPPED encapsulation chip's, not a made-up 40.
        _, chips = self.pipeline()
        nozzle = self.nozzle_of(chips["module2"])
        f = self.check(M1B, [(nozzle, chips["module2"]["id"], "m1b_to_module2")])
        self.assertEqual(self.other_errors(f), [])
        self.assertTrue(any("m1b_filter_1" in o and f"< the {nozzle:g} um nozzle" in o for o in f.ok))

    def test_filter_as_wide_as_the_downstream_nozzle_is_an_error(self):
        _, chips = self.pipeline()
        nozzle = self.nozzle_of(chips["module2"])
        chip = copy.deepcopy(M1B)
        next(c for c in chip["chambers"] if c["id"] == "m1b_filter_1")["filter"]["gap_um"] = nozzle
        f = self.check(chip, [(nozzle, chips["module2"]["id"], "m1b_to_module2")])
        self.assertTrue(any("m1b_filter_1" in e and f"{chips['module2']['id']} downstream" in e for e in f.errors))


class Pipeline(unittest.TestCase):
    def test_m1b_is_superseded_and_the_pipeline_has_no_bench_step(self):
        self.assertEqual(M1B["module"]["status"], "superseded")
        # The successor is read from the pipeline, not pinned: m1b was automated CELL PREP,
        # so what replaced it is whatever chip the pipeline now runs cell prep on. Pinning a
        # path here made this test fail when the chain was corrected from chip/module1_v1.json
        # (culture, a different job) to the cell-prep chip.
        successor = M1B["module"]["superseded_by"]
        self.assertTrue((ROOT / successor).is_file(), successor)
        self.assertNotEqual(schema_io.load_json(ROOT / successor).get("module", {}).get("status"), "superseded",
                            f"{successor} is superseded too: the chain does not end at a shipped chip")
        # The CHOSEN chain has to run cell prep somewhere; the other assay variant need not
        # (TIP-seq tagments at the bench instead), so only the chosen one is asked for it.
        chosen = schema_io.chosen_pipeline()[1]
        pipeline_chips = {schema_io.pipeline_chip(m)[0] for m in chosen["modules"]}
        self.assertIn(successor, pipeline_chips, f"{successor} replaced m1b but is not in the pipeline")
        found = schema_io.pipelines()   # pipeline_mvp.json and the assay variants it can copy
        self.assertTrue(found)
        for path, pipe in found:
            self.assertEqual(schema_io.schema_errors("pipeline", pipe), [], path.name)
            self.assertNotIn("m1b", [m["module"] for m in pipe["modules"]])
            self.assertNotIn("bench_step", path.read_text())       # the old human-in-the-loop bridge
            self.assertNotIn("pause_for_human", path.read_text())


if __name__ == "__main__":
    unittest.main()

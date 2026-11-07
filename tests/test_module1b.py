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
    def check(self, chip, downstream):
        params = schema_io.load_json(ROOT / "contracts" / "parameters.json")
        materials = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
        return ccs.check(chip, [], params, materials, {"laser_kerf_um": None, "min_embossed_feature_um": None}, downstream)

    def test_m1b_is_out_of_the_pipeline(self):
        self.assertEqual(ccs.downstream_nozzles(M1B), [])           # superseded: no longer in the pipeline

    def test_a_chip_feeding_a_declared_nozzle_through_a_handoff_is_told_about_it(self):
        pipe_path, pipe = schema_io.pipelines()[0]
        chips = {m["module"]: schema_io.load_json(ROOT / schema_io.pipeline_chip(m)[0])
                 for m in pipe["modules"] if schema_io.pipeline_chip(m)[0]}
        for h in pipe["handoffs"]:
            src, dst = chips.get(h["from"].get("module")), chips.get(h["to"].get("module"))
            nozzle = ((dst or {}).get("encapsulation") or {}).get("nozzle_um")
            if src and nozzle and h["from"].get("port") in {p["id"] for p in src.get("ports", [])}:
                self.assertIn((nozzle, dst["id"], h["id"]), ccs.downstream_nozzles(src), h["id"])

    def test_the_pipelines_encapsulation_chip_declares_its_nozzle(self):
        # Otherwise the upstream cell filter is never checked against it (check_chip_spec.downstream_nozzles is silent).
        pipe_path, pipe = schema_io.pipelines()[0]
        entry = next(m for m in pipe["modules"] if m["module"] == "module2")
        chip_path = schema_io.pipeline_chip(entry)[0]
        chip = schema_io.load_json(ROOT / chip_path)
        self.assertTrue((chip.get("encapsulation") or {}).get("nozzle_um"),
                        f"{chip_path} has no encapsulation.nozzle_um, so no upstream filter is checked against it")

    def test_filter_smaller_than_the_downstream_nozzle_is_confirmed(self):
        f = self.check(M1B, [(40, "m2_encapsulation_v1", "m1b_to_m2")])
        self.assertEqual(f.errors, [])
        self.assertTrue(any("m1b_filter_1" in o and "< the 40 um nozzle" in o for o in f.ok))

    def test_filter_as_wide_as_the_downstream_nozzle_is_an_error(self):
        chip = copy.deepcopy(M1B)
        next(c for c in chip["chambers"] if c["id"] == "m1b_filter_1")["filter"]["gap_um"] = 40
        f = self.check(chip, [(40, "m2_encapsulation_v1", "m1b_to_m2")])
        self.assertTrue(any("m1b_filter_1" in e and "m2_encapsulation_v1 downstream" in e for e in f.errors))


class Pipeline(unittest.TestCase):
    def test_m1b_is_superseded_and_the_pipeline_has_no_bench_step(self):
        self.assertEqual(M1B["module"]["status"], "superseded")
        self.assertEqual(M1B["module"]["superseded_by"], "chip/module1_v1.json")
        (path, pipe), = schema_io.pipelines()
        self.assertEqual(schema_io.schema_errors("pipeline", pipe), [])
        self.assertNotIn("m1b", [m["module"] for m in pipe["modules"]])
        self.assertNotIn("bench_step", path.read_text())
        self.assertNotIn("pause_for_human", path.read_text())


if __name__ == "__main__":
    unittest.main()

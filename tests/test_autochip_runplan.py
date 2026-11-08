"""autochip stages 5 (runplan) and 6 (report) on hand-built fixtures (tests/fixtures/autochip/runplan/).

    python -m pytest -q tests/test_autochip_runplan.py
"""

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autochip import constraints as C  # noqa: E402
from autochip.stages import report, runplan  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "autochip" / "runplan"
SCHEMAS = ROOT / "contracts" / "schemas"


def load(name):
    return json.loads((FIX / name).read_text())


def validator(name):
    return jsonschema.Draft202012Validator(json.loads((SCHEMAS / f"{name}.schema.json").read_text()))


def inputs():
    ir, plan = load("protocol_ir.json"), load("chip_plan.json")
    chips = {c: {"spec": load(f"{c}.json"), "violations": [], "files": {}} for c in ("chip_a", "chip_b")}
    return ir, plan, chips


def errors(checks):
    return [c["message"] for c in checks if c["severity"] == "error"]


class Fixtures(unittest.TestCase):
    def test_fixtures_match_their_schemas(self):
        for name, schema in (("protocol_ir.json", "protocol_ir"), ("chip_plan.json", "chip_plan"),
                             ("chip_a.json", "chip_spec"), ("chip_b.json", "chip_spec")):
            errs = list(validator(schema).iter_errors(load(name)))
            self.assertEqual(errs, [], f"{name}: {[e.message for e in errs[:3]]}")


class RunPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        cls.ir, cls.plan, cls.chips = inputs()
        cls.cons = C.load("assayathon_demo")
        cls.res = runplan.run(cls.plan, cls.ir, cls.chips, cls.cons, cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def prog(self, cid):
        return json.loads((self.out / "runs" / f"{cid}.run.json").read_text())

    def test_one_result_per_chip_with_files(self):
        self.assertEqual(set(self.res), {"chip_a", "chip_b"})
        for cid, r in self.res.items():
            self.assertEqual(set(r) >= {"protocol", "checks", "files"}, True)
            for name in ("protocol", "run", "run_sheet", "pipeline_run_sheet"):
                self.assertTrue(Path(r["files"][name]).exists(), f"{cid}: {name}")

    def test_protocols_match_the_contract(self):
        v = validator("protocol")
        for cid in self.res:
            proto = json.loads((self.out / "runs" / f"{cid}.protocol.json").read_text())
            self.assertEqual([e.message for e in v.iter_errors(proto)], [])
            self.assertEqual([s["step"] for s in proto["steps"]], list(range(1, len(proto["steps"]) + 1)))
            self.assertTrue(proto["placeholder"])

    def test_no_errors_on_a_sound_plan(self):
        for cid, r in self.res.items():
            self.assertEqual(errors(r["checks"]), [], cid)

    def test_ir_actions_map_to_contract_operations(self):
        ops_a = [s["operation"] for s in self.res["chip_a"]["protocol"]["steps"]]
        # load the cells first; the quench mix carries them out of the chip at the end
        self.assertEqual(ops_a, ["load", "culture", "exchange", "exchange", "hold_temp", "mix"])
        ops_b = [s["operation"] for s in self.res["chip_b"]["protocol"]["steps"]]
        # encapsulate (cells arrive through inlet_1), cold hold in the delay line, MOVE to the trap, break, cure, wash,
        # lyse (short fill + stopped hold), COLLECT the capsules through outlet_1
        self.assertEqual(ops_b, ["encapsulate", "hold_temp", "transfer", "exchange", "cure", "exchange", "exchange",
                                 "hold_temp", "transfer"])
        cure = self.res["chip_b"]["protocol"]["steps"][4]
        self.assertEqual((cure["target"], cure["wavelength_nm"], cure["duration_s"]), ("capsule_trap", 405, 30))
        wash = self.res["chip_b"]["protocol"]["steps"][5]
        self.assertEqual((wash["reagent"], wash["washes"]), ("wash_buffer", 3))

    def test_exchange_feeds_the_modules_own_inlet_and_caps_the_rest(self):
        st = next(s for s in self.prog("chip_a")["steps"] if s["ir_step"] == 3 and s["phase"] == "fill")
        self.assertEqual(st["ir_step"], 3)
        self.assertEqual(st["feeds"], ["dissociation_enzyme_in"])
        self.assertEqual(st["vents"], ["outlet_1"])
        capped = {p for p, s in st["ports"].items() if s["state"] == "CAP"}
        self.assertEqual(capped, {"inlet_1", "medium_in", "wash_buffer_in", "quench_medium_in"})
        self.assertTrue(st["flow_check"]["solved"])
        self.assertGreater(st["flow_check"]["through_target_ul_min"], 0)
        self.assertEqual(st["flow_check"]["stray"], [])

    def test_trap_washes_vent_the_waste_port_behind_the_mesh(self):
        washes = [s for s in self.prog("chip_b")["steps"] if s["operation"] == "exchange" and s["phase"] == "fill"]
        self.assertTrue(washes)
        for s in washes:
            self.assertEqual(s["vents"], ["waste_out"], s["title"])
            self.assertEqual(s["ports"]["outlet_1"]["state"], "CAP")

    def test_encapsulation_coflow_uses_core_shell_and_oil(self):
        st = self.prog("chip_b")["steps"][0]
        self.assertEqual(st["operation"], "encapsulate")
        self.assertEqual(set(st["feeds"]), {"inlet_1", "shell_polymer_in", "oil_in"})
        self.assertLessEqual(len(st["feeds"]), self.cons["off_chip"]["pump_channels"])
        self.assertTrue(st["flow_check"]["ok"])

    def test_quench_in_a_mixer_flows_from_the_culture_chamber(self):
        st = self.prog("chip_a")["steps"][-1]
        self.assertEqual((st["ir_step"], st["operation"]), (4, "mix"))
        self.assertEqual(set(st["feeds"]), {"inlet_1", "quench_medium_in"})
        self.assertIn("culture_1", st["flow_check"]["passes"])

    def test_holds_cap_everything(self):
        for cid in self.res:
            for s in self.prog(cid)["steps"]:
                if s["operation"] in ("hold_temp", "cure", "count"):
                    self.assertEqual({p["state"] for p in s["ports"].values()}, {"CAP"}, (cid, s["n"]))

    def test_run_sheets_and_pipeline_sheet(self):
        sheet = (self.out / "runs" / "chip_b.run_sheet.md").read_text()
        for text in ("## Setup", "shell_polymer_in", "## Steps", "## Flow check", "405 nm"):
            self.assertIn(text, sheet)
        pipe = (self.out / "runs" / "pipeline_run_sheet.md").read_text()
        self.assertIn("outlet_1 of chip_a", pipe)                 # hand-off out
        self.assertIn("inlet_1 of chip_b", pipe)                  # hand-off in
        self.assertIn("at the bench", pipe)                       # off-chip steps 5, 6, 14
        self.assertIn("missing part", pipe)                       # step 12
        order = [pipe.index(f"**Step {n}**") for n in range(1, 15)]
        self.assertEqual(order, sorted(order))
        self.assertLess(pipe.index("Hand-off out of chip_a"), pipe.index("**Step 5**"))
        self.assertLess(pipe.index("Hand-off into chip_b"), pipe.index("**Step 7**"))

    def test_sample_moves_between_chips_and_modules(self):
        a, b = self.prog("chip_a")["steps"], self.prog("chip_b")["steps"]
        self.assertEqual((a[0]["phase"], a[0]["feeds"]), ("move", ["inlet_1"]))         # cells loaded onto chip_a
        self.assertEqual(a[-1]["collects"]["port"], "outlet_1")                         # and sent on to chip_b
        self.assertIn("for chip_b inlet_1", a[-1]["collects"]["label"])
        self.assertEqual(b[0]["feeds"][0], "inlet_1")                                   # chip_b takes them in
        move = next(s for s in b if s["phase"] == "move")
        self.assertEqual((move["protocol_op"] if "protocol_op" in move else move["operation"], move["target"]),
                         ("transfer", "capsule_trap"))                                   # delay line -> trap
        self.assertEqual(b[-1]["collects"]["port"], "outlet_1")                         # product, not waste
        self.assertNotIn("waste", b[-1]["ports"]["outlet_1"]["to"])

    def test_incubations_flush_briefly_then_stop(self):
        a = self.prog("chip_a")["steps"]
        fill = next(s for s in a if s["ir_step"] == 3 and s["phase"] == "fill")
        hold = next(s for s in a if s["ir_step"] == 3 and s["phase"] == "hold")
        self.assertAlmostEqual(fill["ports"]["dissociation_enzyme_in"]["volume_ul"], 1.5 * 12.8, places=1)
        self.assertEqual((hold["duration_s"], hold["temp_c"]), (300, 37))

    def test_syringe_sizes_and_display_units(self):
        sheet = (self.out / "runs" / "chip_a.run_sheet.md").read_text()
        self.assertIn("| Syringe |", sheet)
        self.assertIn("µL", sheet)
        self.assertIn("°C", sheet)
        self.assertNotRegex(sheet, r"\buL\b")
        self.assertTrue(all(r["syringe_ml"] for r in self.prog("chip_a")["syringes"]))

    def test_operator_planner_accepts_the_protocols(self):
        for cid in self.res:
            tw = self.prog(cid)["twin_run"]
            self.assertTrue(tw["ok"], tw)
            self.assertTrue((self.out / "runs" / f"{cid}.twin_run.json").exists())

    def test_twin_run_stores_a_repo_relative_chip_path_inside_the_repo(self):
        import shutil
        ir, plan, chips = inputs()
        out = Path(tempfile.mkdtemp(prefix="tmp_", dir=FIX))   # inside the repo; removed below
        try:
            (out / "chips").mkdir()
            for cid, e in chips.items():
                (out / "chips" / f"{cid}.json").write_text(json.dumps(e["spec"]))
            runplan.run(plan, ir, chips, C.load("unconstrained"), out)
            run = json.loads((out / "runs" / "chip_b.twin_run.json").read_text())
            self.assertEqual(run["chip_spec"], (out / "chips" / "chip_b.json").resolve().relative_to(ROOT.resolve()).as_posix())
            self.assertIn("so the twin can load and replay it", (out / "runs" / "chip_b.run_sheet.md").read_text())
        finally:
            shutil.rmtree(out)


class FlowCheckCatchesProblems(unittest.TestCase):
    def run_with(self, plan=None, chips=None, cons=None, ir=None):
        ir0, plan0, chips0 = inputs()
        with tempfile.TemporaryDirectory() as d:
            return runplan.run(plan or plan0, ir or ir0, chips or chips0, cons or C.load("unconstrained"), Path(d))

    def test_missing_reagent_inlet_for_a_coflow_is_an_error(self):
        _, _, chips = inputs()
        spec = chips["chip_b"]["spec"]
        spec["ports"] = [p for p in spec["ports"] if p["id"] != "oil_in"]
        spec["channels"] = [c for c in spec["channels"] if c["from"] != "oil_in"]
        res = self.run_with(chips=chips)
        self.assertTrue(any("oil inlet" in m for m in errors(res["chip_b"]["checks"])), res["chip_b"]["checks"])

    def test_no_inlet_reaching_the_module_is_an_error(self):
        _, _, chips = inputs()
        spec = chips["chip_b"]["spec"]
        cut = {"emulsion_breaker_in", "wash_buffer_in", "lysis_buffer_in"}
        spec["ports"] = [p for p in spec["ports"] if p["id"] not in cut]
        spec["channels"] = [c for c in spec["channels"] if c["from"] not in cut and c["id"] != "ch_gel_trap"]
        res = self.run_with(chips=chips)
        self.assertTrue(any("no inlet reaches capsule_trap" in m for m in errors(res["chip_b"]["checks"])))

    def test_too_many_feeds_for_the_pumps(self):
        cons = C.load("assayathon_demo")
        cons["off_chip"]["pump_channels"] = 2
        res = self.run_with(cons=cons)
        self.assertTrue(any("pump lines" in m for m in errors(res["chip_b"]["checks"])))

    def test_pressure_limit(self):
        cons = C.load("unconstrained")
        cons.setdefault("operating", {})["max_pressure_kpa"] = 0.01
        res = self.run_with(cons=cons)
        self.assertTrue(any("kPa limit" in m for m in errors(res["chip_b"]["checks"])))

    def test_feed_rate_outside_the_pump_range(self):
        ir, _, _ = inputs()
        ir["steps"][1]["flow_ul_min"] = 900.0
        cons = C.load("assayathon_demo")
        res = self.run_with(ir=ir, cons=cons)
        self.assertTrue(any("above the pump" in m for m in errors(res["chip_a"]["checks"])))

    def test_backflow_into_a_module_holding_cells_is_an_error(self):
        # an extra outlet straight off the culture chamber's inlet line, drawn backwards (port -> chamber):
        # venting it while the quench feeds pulls liquid back through culture_1
        _, plan, chips = inputs()
        spec = chips["chip_a"]["spec"]
        rp = runplan.ChipPlanner(plan["chips"][0], spec, inputs()[0], C.load("unconstrained"), plan)
        ph = {"ir_step": 4, "title": "test", "target": "quench_mix", "holds": "cells", "phase": "flow", "issues": [],
              "feeds": ["quench_medium_in"], "vents": ["outlet_1", "medium_in"],
              "ports": {"quench_medium_in": {"state": "FEED", "ul_min": 5.0}}}
        fc = rp.flow_check(ph)
        self.assertIn("culture_1", fc["stray"])
        self.assertFalse(fc["ok"])

    def test_chip_without_spec_is_reported(self):
        _, _, chips = inputs()
        del chips["chip_a"]
        res = self.run_with(chips=chips)
        self.assertTrue(any("no chip spec" in m for m in errors(res["chip_a"]["checks"])))
        self.assertEqual(list(validator("protocol").iter_errors(res["chip_a"]["protocol"])), [])


def lanes_case(n_lanes=4, lane_outlets=False, material="glass"):
    """A fan-out chip: trap -> splitter -> n lanes (each with its own reagent inlet, lanes of unequal length) -> pool."""
    H = 1.5
    ir = {"kind": "protocol_ir", "id": "lanes_demo", "name": "Lanes demo", "source": {"kind": "prompt"}, "placeholder": True,
          "steps": [
              {"n": 1, "text": "Wash the capsules.", "action": "wash_particles", "input_material": "capsules",
               "reagents": ["wash_buffer"]},
              {"n": 2, "text": "Split the capsules into portions.", "action": "split", "input_material": "capsules"},
              {"n": 3, "text": "Index PCR on each portion.", "action": "amplify", "input_material": "capsules",
               "reagents": ["pcr_mix"], "duration_s": 1800, "temp_c": 72},
              {"n": 4, "text": "Pool the portions.", "action": "pool", "input_material": "capsules"}]}
    lanes = [f"lane_{i + 1}" for i in range(n_lanes)]
    mods = [{"id": "trap_1", "part": "trap_chamber", "steps": [1], "inlets": ["wash_buffer"], "outlets": ["trap_1_waste"],
             "does": "trap", "holds": "capsules"},
            {"id": "split_1", "part": "splitter", "steps": [2], "does": "split", "holds": "capsules"}]
    mods += [{"id": ln, "part": "thermal_chamber", "steps": [3], "inlets": [f"pcr_mix_{i + 1}"], "does": "pcr",
              "holds": "capsules", **({"outlets": [f"{ln}_waste"]} if lane_outlets else {})} for i, ln in enumerate(lanes)]
    mods += [{"id": "pool_1", "part": "pool", "steps": [4], "does": "pool", "holds": "capsules"}]
    plan = {"kind": "chip_plan", "id": "lanes_plan", "protocol_id": "lanes_demo", "constraints_id": "unconstrained",
            "chips": [{"id": "chip_l", "name": "Lanes", "purpose": "split, PCR per lane, pool", "steps": [1, 2, 3, 4],
                       "modules": mods}],
            "handoffs": [], "off_chip": [], "missing_parts": [],
            "coverage": {"1": "chip_l.trap_1", "2": "chip_l.split_1", "3": "chip_l.lane_1", "4": "chip_l.pool_1"}}
    chambers = [{"id": "trap_1", "type": "exchange", "center": [10, 20, 1.1], "size": [6, 6, 0.2], "volume_ul": 7.2},
                {"id": "split_1", "type": "split", "center": [20, 20, 1.1], "size": [2, 2, 0.2], "volume_ul": 0.8},
                {"id": "pool_1", "type": "pool", "center": [40, 20, 1.1], "size": [2, 2, 0.2], "volume_ul": 0.8}]
    ports = [{"id": "inlet_1", "type": "inlet", "position": [2, 20, H], "role": "load"},
             {"id": "wash_buffer_in", "type": "inlet", "position": [10, 30, H], "role": "reagent"},
             {"id": "trap_1_waste_out", "type": "outlet", "position": [10, 10, H], "role": "waste", "capsule_passable": False},
             {"id": "outlet_1", "type": "outlet", "position": [48, 20, H], "role": "harvest"}]
    chans = [{"id": "c_in", "from": "inlet_1", "to": "trap_1", "path": [[2, 20], [7, 20]]},
             {"id": "c_wash", "from": "wash_buffer_in", "to": "trap_1", "path": [[10, 30], [10, 23]]},
             {"id": "c_trapw", "from": "trap_1", "to": "trap_1_waste_out", "path": [[10, 17], [10, 10]]},
             {"id": "c_split", "from": "trap_1", "to": "split_1", "path": [[13, 20], [19, 20]]},
             {"id": "c_out", "from": "pool_1", "to": "outlet_1", "path": [[41, 20], [48, 20]]}]
    span = 24.0 / max(n_lanes - 1, 1)
    for i, ln in enumerate(lanes):
        y = 8 + i * span
        chambers.append({"id": ln, "type": "exchange", "center": [30, y, 1.1], "size": [4, 3, 0.2], "volume_ul": 2.4})
        ports.append({"id": f"pcr_mix_{i + 1}_in", "type": "inlet", "position": [30, y + 4, H], "role": "reagent"})
        chans += [{"id": f"c_s_{ln}", "from": "split_1", "to": ln, "path": [[20, 21], [20, y], [28, y]]},
                  {"id": f"c_{ln}_p", "from": ln, "to": "pool_1", "path": [[32, y], [40, y], [40, 21]]},
                  {"id": f"c_r_{ln}", "from": f"pcr_mix_{i + 1}_in", "to": ln, "path": [[30, y + 4], [30, y + 1.5]]}]
        if lane_outlets:
            ports.append({"id": f"{ln}_waste_out", "type": "outlet", "position": [33, y - 3, H], "role": "waste",
                          "capsule_passable": False})
            chans.append({"id": f"c_{ln}_w", "from": ln, "to": f"{ln}_waste_out", "path": [[31, y - 1.5], [33, y - 3]]})
    for c in chans:
        c.update({"width_mm": 0.3, "depth_mm": 0.1})
    spec = {"id": "chip_l", "placeholder": True, "units": "mm", "footprint": {"x": 50, "y": 40},
            "layers": [{"id": "base", "material": material, "thickness_mm": 1}],
            "chambers": chambers, "channels": chans, "ports": ports}
    return ir, plan, {"chip_l": {"spec": spec, "violations": []}}


class ParallelLanes(unittest.TestCase):
    def run_lanes(self, n=4, outlets=False, cons="unconstrained", material="glass"):
        ir, plan, chips = lanes_case(n, outlets, material)
        for name, obj in (("protocol_ir", ir), ("chip_plan", plan), ("chip_spec", chips["chip_l"]["spec"])):
            self.assertEqual([e.message for e in validator(name).iter_errors(obj)], [], name)
        with tempfile.TemporaryDirectory() as d:
            return runplan.run(plan, ir, chips, C.load(cons), Path(d))["chip_l"]

    def test_lanes_with_their_own_outlets_are_filled_one_at_a_time(self):
        r = self.run_lanes(outlets=True)
        self.assertEqual(errors(r["checks"]), [])
        fills = [s for s in r["program"]["steps"] if s["ir_step"] == 3 and s["phase"] == "fill"]
        self.assertEqual(len(fills), 4)
        for i, s in enumerate(fills, 1):
            self.assertEqual((s["feeds"], s["vents"]), ([f"pcr_mix_{i}_in"], [f"lane_{i}_waste_out"]))
            self.assertEqual(s["flow_check"]["sibling_flow_ul_min"], 0)
        split = next(s for s in r["program"]["steps"] if s["operation"] == "split")
        self.assertEqual(len(split["vents"]), 4)                       # material goes into every lane
        self.assertEqual(r["program"]["twin_run"]["ok"], True)

    def test_shared_lanes_are_filled_together_with_balanced_rates(self):
        r = self.run_lanes(outlets=False)
        self.assertEqual(errors(r["checks"]), [])
        fill = [s for s in r["program"]["steps"] if s["ir_step"] == 3 and s["phase"] == "fill"]
        self.assertEqual(len(fill), 1)
        self.assertEqual(sorted(fill[0]["feeds"]), [f"pcr_mix_{i}_in" for i in range(1, 5)])
        self.assertTrue(all(v >= 0.99 for v in fill[0]["flow_check"]["lane_purity"].values()))
        self.assertNotIn("split_1", fill[0]["flow_check"]["stray"])
        proto = next(s for s in r["protocol"]["steps"] if s.get("targets") and s["operation"] == "exchange")
        self.assertEqual(proto["targets"], ["lane_1", "lane_2", "lane_3", "lane_4"])
        self.assertEqual(r["program"]["twin_run"]["ok"], True)

    def test_more_lanes_than_pump_lines_is_one_clear_error(self):
        r = self.run_lanes(n=5, outlets=False, cons="assayathon_demo")       # 4 pump lines
        errs = errors(r["checks"])
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("share a splitter", errs[0])

    def test_index_pcr_gets_a_different_mix_per_lane_and_explicit_thermal_cycling(self):
        r = self.run_lanes(outlets=False)
        fill = next(s for s in r["program"]["steps"] if s["ir_step"] == 3 and s["phase"] == "fill")
        mixes = [fill["ports"][p]["reagent"] for p in fill["feeds"]]
        self.assertEqual(mixes, [f"index_mix_{k}" for k in range(1, 5)])
        self.assertEqual(len({x["port"] for x in r["program"]["syringes"] if x["reagent"].startswith("index_mix")}), 4)
        cyc = next(s for s in r["program"]["steps"] if s.get("thermal_program"))
        self.assertIsNone(cyc["thermal_program"]["cycles"])                   # source gives no number: never invented
        self.assertIn("N cycles", cyc["title"] + " " + str(cyc.get("note", "")) + " " + json.dumps(cyc))
        self.assertEqual({p["state"] for p in cyc["ports"].values()}, {"CAP"})
        self.assertIn("thermocycler", " ".join(r["program"]["equipment"]))

    def test_thermal_cycling_on_acrylic_is_an_error(self):
        r = self.run_lanes(outlets=True, material="acrylic")                  # PMMA is rated to 80 C
        self.assertTrue(any("can take" in m for m in errors(r["checks"])), errors(r["checks"]))

    def test_lanes_are_loaded_from_the_split_and_the_pool_is_collected(self):
        r = self.run_lanes(outlets=False)
        steps = r["program"]["steps"]
        self.assertEqual(steps[0]["phase"], "move")                           # load the capsules first
        self.assertEqual(steps[0]["feeds"], ["inlet_1"])
        last = steps[-1]
        self.assertEqual((last["vents"], last["collects"]["port"]), (["outlet_1"], "outlet_1"))
        self.assertIn("tube labelled", last["ports"]["outlet_1"]["to"])


class Report(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name)
        ir, plan, chips = inputs()
        ir["assumptions"].append("FALLBACK: the agent was unavailable, the rules extractor read the protocol")
        chips["chip_b"]["violations"] = [{"severity": "warning", "rule": "ports", "message": "8 ports, preset allows 6"}]
        cons = C.load("assayathon_demo")
        cad = cls.out / "cad" / "chip_a"
        cad.mkdir(parents=True)
        (cad / "chip_a.svg").write_text('<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
                                        '<rect id="chip_a_marker" width="10" height="10"/></svg>')
        runs = runplan.run(plan, ir, chips, cons, cls.out)
        source = {"kind": "protocol", "title": "Generic capsule protocol", "file": "generic.md", "text": "...",
                  "prompt": "Make capsules from cultured cells", "chars": 3}
        cls.rep = report.run(source, ir, plan, chips, runs, cons, cls.out, "rules (offline)")
        cls.plan = plan

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_outputs_exist(self):
        for key in ("report", "pipeline", "overview", "html"):
            self.assertTrue(Path(self.rep[key]).exists(), key)

    def test_pipeline_matches_the_contract(self):
        pipe = json.loads(Path(self.rep["pipeline"]).read_text())
        self.assertEqual([e.message for e in validator("pipeline").iter_errors(pipe)], [])
        self.assertEqual([m["module"] for m in pipe["modules"]], ["chip_a", "chip_b"])
        h = pipe["handoffs"][0]
        self.assertEqual((h["from"]["port"], h["to"]["port"], h["carries"]), ("outlet_1", "inlet_1", "cells"))
        self.assertEqual(pipe["roadmap"][0]["name"], "Bench: step 14")

    def test_report_sections(self):
        md = Path(self.rep["report"]).read_text()
        for text in ("## What went in", "Make capsules from cultured cells", "rules (offline)", "## Constraints",
                     "## Chips", "## Step coverage", "## Off-chip steps", "## Missing parts", "## Assumptions",
                     "## Flow-check results", "## Files", "8 ports, preset allows 6", "pillar filter"):
            self.assertIn(text, md)
        for n in range(1, 15):
            self.assertIn(f"\n| {n} |", md)

    def test_open_problems_list_every_error(self):
        ir, plan, chips = inputs()
        cons = C.load("assayathon_demo")
        cons["off_chip"]["pump_channels"] = 2                    # encapsulation needs 3 lines: an error
        with tempfile.TemporaryDirectory() as d:
            runs = runplan.run(plan, ir, chips, cons, Path(d))
            rep = report.run({"kind": "protocol"}, ir, plan, chips, runs, cons, Path(d), "rules")
            md = Path(rep["report"]).read_text()
            page = Path(rep["html"]).read_text()
        section = md[md.index("## Open problems"):md.index("## What went in")]
        self.assertIn("pump lines", section)
        self.assertNotIn("None found", section)
        self.assertIn("@media (max-width:600px)", page)           # phone width: tables scroll, text wraps
        self.assertIn("overflow-wrap:anywhere", page)

    def test_agent_and_fallback_marker_at_the_top(self):
        md = Path(self.rep["report"]).read_text()
        head = md[:md.index("## What went in")]
        self.assertIn("**Agent:** rules (offline)", head)
        self.assertIn("**FALLBACK:** the agent was unavailable", head)
        page = Path(self.rep["html"]).read_text()
        self.assertLess(page.index('class="fallback"'), page.index("<h2>"))

    def test_run_sheet_says_whether_the_twin_can_load_the_run(self):
        sheet = (self.out / "runs" / "chip_a.run_sheet.md").read_text()
        self.assertIn("## Digital twin replay", sheet)
        self.assertIn("The twin can't load this run", sheet)      # temp folder: outside the repo

    def test_overview_and_html(self):
        svg = Path(self.rep["overview"]).read_text()
        self.assertTrue(svg.startswith("<svg"))
        for text in (">chip_a<", "Cell prep", ">chip_b<", "Bench (off chip)", "missing (step 12)"):
            self.assertIn(text, svg)
        page = Path(self.rep["html"]).read_text()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("<style>", page)
        self.assertNotIn("<script", page)
        self.assertNotIn('<link rel="stylesheet"', page)
        self.assertIn("chip_a_marker", page)                    # the chip's CAD top view is inlined
        self.assertEqual(page.count("<svg"), 2)                # overview + chip_a drawing
        self.assertIn("Lab run sheet", page)

    def test_report_from_saved_files(self):
        """--from-stage report: runs carry only the protocols; the programs come from runs/<chip>.run.json."""
        import shutil
        ir, plan, chips = inputs()
        with tempfile.TemporaryDirectory() as d:
            shutil.copytree(self.out / "runs", Path(d) / "runs")
            runs = {cid: {"protocol": json.loads((Path(d) / "runs" / f"{cid}.protocol.json").read_text()), "checks": []}
                    for cid in ("chip_a", "chip_b")}
            rep = report.run({"kind": "protocol"}, ir, plan, chips, runs, C.load("unconstrained"), Path(d), "replay")
            self.assertIn("chip_b #1 Encapsulate", Path(rep["report"]).read_text())


if __name__ == "__main__":
    unittest.main()

"""autochip stage 4 (layout), autochip/checks.py and autochip/cad.py on hand-made chip plans.

    python3 -m pytest -q tests/test_autochip_layout.py

Fixtures: tests/fixtures/autochip/layout/plan_*.json (valid chip plans) + ir_fixture.json.
Every spec must validate against the contract, pass tools/check_chip_spec.py without errors, and
carry flow through every main-path module when inlet_1 is fed and outlet_1 vented.
"""
LIB_ONLY = str(__import__("pathlib").Path(__file__).parent / "fixtures" / "autochip" / "assayathon_library_only.json")
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "operator", ROOT / "tools"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import check_chip_spec as ccs  # noqa: E402
import network  # noqa: E402
import schema_io  # noqa: E402
from autochip import cad, checks  # noqa: E402
from autochip import constraints as C  # noqa: E402
from autochip.stages import layout  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "autochip" / "layout"
IR = json.loads((FIX / "ir_fixture.json").read_text())
PARAMS = schema_io.load_json(ROOT / "contracts" / "parameters.json")
MATERIALS = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
FAB = schema_io.load_json(ROOT / "cad" / "fabrication.json")
_TMP = tempfile.TemporaryDirectory()
_CACHE: dict = {}


def plan(name: str) -> dict:
    return json.loads((FIX / f"plan_{name}.json").read_text())


def run(name: str, constraints: str | None = None, **plan_edits) -> dict:
    key = (name, constraints, json.dumps(plan_edits, sort_keys=True))
    if key not in _CACHE:
        pl = plan(name)
        pl.update(plan_edits)
        cons = C.load(constraints or pl["constraints_id"])
        out = Path(_TMP.name) / f"{name}_{constraints or 'own'}_{len(_CACHE)}"
        _CACHE[key] = layout.run(pl, IR, cons, None, out)
    return _CACHE[key]


def errors(vs):
    return [v for v in vs if v["severity"] == "error"]


ALL = ["four_modules", "four_reagents", "assayathon", "unconstrained", "glass", "lanes", "library_lanes"]


class Contract(unittest.TestCase):
    def test_fixture_plans_are_valid_chip_plans(self):
        import jsonschema
        v = jsonschema.Draft202012Validator(json.loads((ROOT / "contracts" / "schemas" / "chip_plan.schema.json").read_text()))
        for name in ALL:
            self.assertEqual([e.message for e in v.iter_errors(plan(name))], [], name)

    def test_every_spec_validates_and_passes_the_design_checker(self):
        for name in ALL:
            for cid, r in run(name).items():
                with self.subTest(plan=name, chip=cid):
                    spec = r["spec"]
                    self.assertEqual([schema_io.describe(e) for e in schema_io.schema_errors("chip_spec", spec)], [])
                    f = ccs.check(spec, [], PARAMS, MATERIALS, FAB)
                    self.assertEqual(f.errors, [])
                    self.assertTrue(spec["placeholder"])
                    self.assertEqual(spec["generator"]["params"]["constraints_id"], C.load(plan(name)["constraints_id"])["id"])
                    self.assertEqual(json.loads(Path(r["files"]["spec"]).read_text()), spec)

    def test_flow_reaches_every_main_path_module(self):
        for name in ALL:
            for cid, r in run(name).items():
                with self.subTest(plan=name, chip=cid):
                    spec = r["spec"]
                    sol = network.Network(spec).solve({"inlet_1": ("FEED", 5), "outlet_1": "VENT"})
                    lanes = spec["generator"]["params"]["lanes"]
                    in_lane = {m for blk in lanes for ln in blk["lanes"] for m in ln}
                    for c in spec["chambers"]:
                        q = sol["chambers"][c["id"]]["throughput_ul_min"]
                        if c["id"] in in_lane:
                            self.assertGreater(q, 0.1, c["id"])
                        else:
                            self.assertAlmostEqual(q, 5.0, places=3, msg=c["id"])
                    for blk in lanes:      # parallel lanes share the flow
                        self.assertAlmostEqual(sum(sol["chambers"][ln[0]]["throughput_ul_min"] for ln in blk["lanes"]), 5.0, 3)
                    main = [ch for ch in spec["channels"] if "main path" in ch["note"]]
                    n_units = len(spec["chambers"]) - sum(len(m) for blk in lanes for m in blk["lanes"]) - len(lanes)
                    self.assertEqual(len(main), n_units + 1)

    def test_ports_keep_pitch_and_outlet_is_on_the_right(self):
        for name in ALL:
            for cid, r in run(name).items():
                with self.subTest(plan=name, chip=cid):
                    rules = {v["rule"] for v in errors(r["violations"])}
                    self.assertNotIn("ports.min_pitch_mm", rules)
                    self.assertNotIn("channel_crossings", rules)
                    self.assertEqual(ccs.crossings(r["spec"]), [])
                    ports = {p["id"]: p["position"] for p in r["spec"]["ports"]}
                    self.assertGreater(ports["outlet_1"][0], ports["inlet_1"][0])
                    self.assertNotIn("layout.outlet_side", {v["rule"] for v in r["violations"]})

    def test_channels_end_at_port_centres_and_chamber_edges(self):
        for name in ALL:
            for r in run(name).values():
                spec = r["spec"]
                ports = {p["id"]: p["position"][:2] for p in spec["ports"]}
                rooms = {c["id"]: c for c in spec["chambers"]}
                for ch in spec["channels"]:
                    for end, pt in ((ch["from"], ch["path"][0]), (ch["to"], ch["path"][-1])):
                        if end in ports:
                            self.assertAlmostEqual(pt[0], ports[end][0], 3)
                            self.assertAlmostEqual(pt[1], ports[end][1], 3)
                        else:
                            (cx, cy, _), (sx, sy, _) = rooms[end]["center"], rooms[end]["size"]
                            on_x = abs(abs(pt[0] - cx) - sx / 2) < 1e-3 and abs(pt[1] - cy) <= sy / 2 + 1e-3
                            on_y = abs(abs(pt[1] - cy) - sy / 2) < 1e-3 and abs(pt[0] - cx) <= sx / 2 + 1e-3
                            self.assertTrue(on_x or on_y, f"{ch['id']} does not end on the edge of {end}")
                    for a, b in zip(ch["path"][:-1], ch["path"][1:]):       # orthogonal
                        self.assertTrue(abs(a[0] - b[0]) < 1e-6 or abs(a[1] - b[1]) < 1e-6, ch["id"])


class Layout(unittest.TestCase):
    def test_four_module_chip(self):
        spec = run("four_modules")["prep_chip"]["spec"]
        self.assertEqual([c["id"] for c in spec["chambers"]], ["dissociate", "quench_join", "quench_mix", "strain"])
        ports = {p["id"]: p for p in spec["ports"]}
        self.assertEqual(set(ports), {"inlet_1", "outlet_1", "enzyme_in", "quench_in", "clumps_out"})
        self.assertLess(ports["inlet_1"]["position"][0], ports["outlet_1"]["position"][0])     # left -> right
        mix = next(c for c in spec["chambers"] if c["id"] == "quench_mix")
        self.assertEqual(mix["type"], "mixer")
        self.assertIn("30 s residence at 5 uL/min", mix["note"])
        self.assertEqual(next(c for c in spec["chambers"] if c["id"] == "strain")["filter"]["gap_um"], 30)
        # library part at native footprint (no scaling needed on a 75 x 50 mm chip)
        self.assertEqual(next(c for c in spec["chambers"] if c["id"] == "quench_join")["size"][:2], [12.5, 20.0])
        mods = spec["generator"]["params"]["modules"]
        self.assertEqual(mods["strain"]["steps"], [4])
        # the 37 C chip temperature becomes a thermal zone + heat zone feature
        self.assertEqual(spec["thermal_zones"][0]["max_temp_c"], 37)
        # resin can't make 30 um pillar gaps: an actionable error goes back to partition
        errs = errors(run("four_modules")["prep_chip"]["violations"])
        self.assertEqual([e["rule"] for e in errs], ["min_feature.gap_mm"])
        self.assertIn("30 um", errs[0]["message"])

    def test_four_reagent_inlets_are_spread_at_pitch_without_crossings(self):
        r = run("four_reagents")["stain_chip"]
        spec = r["spec"]
        names = {p["id"] for p in spec["ports"]}
        self.assertTrue({"fixative_in", "perm_buffer_in", "dye_in", "wash_buffer_in", "waste_out"} <= names)
        self.assertEqual(ccs.crossings(spec), [])
        self.assertEqual(errors(r["violations"]), [])
        pitch = C.load("resin_print_multilayer")["ports"]["min_pitch_mm"]
        pts = [p["position"] for p in spec["ports"]]
        for i, a in enumerate(pts):
            for b in pts[i + 1:]:
                self.assertGreaterEqual(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5, pitch - 1e-3)
        trap = spec["chambers"][0]
        self.assertEqual(trap["retention"], {"type": "mesh", "opening_um": 10, "source": "bought"})
        # printed closed: channels at least the enclosed-channel minimum
        self.assertTrue(all(ch["width_mm"] >= 0.5 for ch in spec["channels"]))

    def test_assayathon_scales_library_parts_and_flags_ports(self):
        res = run("assayathon")
        cap = res["capsule_chip"]
        spec = cap["spec"]
        self.assertLessEqual(spec["footprint"]["x"], 30)
        self.assertLessEqual(spec["footprint"]["y"], 20)
        self.assertLess(spec["generator"]["params"]["scale"], 0.5)
        rules = {v["rule"] for v in cap["violations"]}
        self.assertIn("parts.scaled", rules)
        # 0.1 x scale makes ~50 um channels on a 200 um process, and a one-layer chip can't hold a mesh floor: both errors
        self.assertEqual({e["rule"] for e in errors(cap["violations"])}, {"parts.scaled_below_min_feature", "min_feature.gap_mm"})
        msg = next(e["message"] for e in errors(cap["violations"]) if e["rule"] == "parts.scaled_below_min_feature")
        self.assertIn("split the chip", msg)
        trap = next(c for c in spec["chambers"] if c["id"] == "trap")
        self.assertEqual(trap["retention"]["type"], "pillars")
        self.assertIn("no mesh floor", trap["note"])
        self.assertEqual(len(spec["layers"]), 3)                     # embossed substrate + relief + film
        self.assertEqual(spec["layers"][-1]["material"], "sealing film")
        depths = {c["size"][2] for c in spec["chambers"]} | {ch["depth_mm"] for ch in spec["channels"]}
        self.assertEqual(depths, {0.3})                             # single depth, max 0.3
        busy = res["busy_chip"]
        msg = next(v["message"] for v in errors(busy["violations"]) if v["rule"] == "ports.max_per_chip")
        self.assertTrue(msg.startswith("chip has 8 ports, max 6: move 2"), msg)
        self.assertIn("second chip", msg)

    def test_unconstrained_is_near_square_with_valve(self):
        r = run("unconstrained")["long_chip"]
        spec = r["spec"]
        fx, fy = spec["footprint"]["x"], spec["footprint"]["y"]
        self.assertLess(max(fx / fy, fy / fx), 2.0)
        self.assertGreater(spec["generator"]["params"]["rows"], 1)
        self.assertEqual(spec["valves"][0]["control_port"], "gate_ctrl")
        self.assertIn("gate_ctrl", {p["id"] for p in spec["ports"] if p["type"] == "control"})
        self.assertEqual(errors(r["violations"]), [])

    def test_glass_is_monolithic_and_parametric(self):
        r = run("glass")["lysis_chip"]
        spec = r["spec"]
        self.assertEqual(errors(r["violations"]), [])
        self.assertTrue(all(lay["material"] == "fused silica" for lay in spec["layers"]))
        self.assertIn("monolithic", spec["layers"][0]["note"])
        self.assertEqual(spec["thermal_zones"][0]["max_temp_c"], 65)
        bind = next(c for c in spec["chambers"] if c["id"] == "bind")
        self.assertEqual(bind["mixer"]["kind"], "serpentine")

    def test_library_part_under_parametric_only_is_an_error(self):
        r = run("assayathon", "glass_laser_3d_etch")
        msgs = [v["message"] for v in errors(r["capsule_chip"]["violations"])]
        self.assertTrue(any("parametric" in m for m in msgs), msgs)

    def test_parametric_part_under_library_only_is_an_error(self):
        r = run("four_modules", LIB_ONLY)["prep_chip"]
        self.assertTrue(any(v["rule"] in ("parts.unavailable", "parts.source") for v in errors(r["violations"])))
        self.assertEqual(schema_io.schema_errors("chip_spec", r["spec"]), [])


class Lanes(unittest.TestCase):
    """split -> parallel lanes -> pool: fan-out / fan-in channels, lanes side by side, a vent per lane."""

    def test_spec_shape_runplan_relies_on(self):
        spec = run("lanes")["pcr_chip"]["spec"]
        blk, = spec["generator"]["params"]["lanes"]
        self.assertEqual(blk["split"], "split_1")
        self.assertEqual(blk["pool"], "pool_1")
        self.assertEqual(blk["lanes"], [["pcr_1"], ["pcr_2"], ["pcr_3"]])
        self.assertEqual(blk["fan_out"], ["ch_split_1_pcr_1", "ch_split_1_pcr_2", "ch_split_1_pcr_3"])
        self.assertEqual(blk["fan_in"], ["ch_pcr_1_pool_1", "ch_pcr_2_pool_1", "ch_pcr_3_pool_1"])
        self.assertEqual(blk["lane_inlets"], [["pcr_mix_in"], ["pcr_mix_2_in"], ["pcr_mix_3_in"]])
        self.assertEqual(blk["lane_vents"], [["pcr_1_vent_out"], ["pcr_2_vent_out"], ["pcr_3_vent_out"]])
        ch = {c["id"]: c for c in spec["channels"]}
        for k in range(3):
            self.assertEqual((ch[blk["fan_out"][k]]["from"], ch[blk["fan_out"][k]]["to"]), ("split_1", f"pcr_{k + 1}"))
            self.assertEqual((ch[blk["fan_in"][k]]["from"], ch[blk["fan_in"][k]]["to"]), (f"pcr_{k + 1}", "pool_1"))
            self.assertIn(f"fan-out to lane {k + 1} of 3", ch[blk["fan_out"][k]]["note"])
        # no series link between lanes
        self.assertFalse(any({c["from"], c["to"]} <= {"pcr_1", "pcr_2", "pcr_3"} for c in spec["channels"]))
        # lanes side by side: same x, different y
        rooms = {c["id"]: c for c in spec["chambers"]}
        xs = {rooms[f"pcr_{k}"]["center"][0] for k in (1, 2, 3)}
        ys = [rooms[f"pcr_{k}"]["center"][1] for k in (1, 2, 3)]
        self.assertEqual(len(xs), 1)
        self.assertEqual(len(set(ys)), 3)
        self.assertEqual(spec["generator"]["params"]["modules"]["pcr_2"]["lane"], 2)

    def test_lane_fill_through_its_vent_touches_no_other_lane(self):
        spec = run("lanes")["pcr_chip"]["spec"]
        blk, = spec["generator"]["params"]["lanes"]
        net = network.Network(spec)
        for k, lane in enumerate(blk["lanes"]):
            sol = net.solve({blk["lane_inlets"][k][0]: ("FEED", 5), blk["lane_vents"][k][0]: "VENT"})
            for j, other in enumerate(blk["lanes"]):
                q = sol["chambers"][other[0]]["throughput_ul_min"]
                self.assertAlmostEqual(q, 5.0 if j == k else 0.0, places=6)
        # through outlet_1 it reaches the pool, and the shared split/pool leak is reported
        sol = net.solve({blk["lane_inlets"][0][0]: ("FEED", 5), "outlet_1": "VENT"})
        self.assertAlmostEqual(sol["chambers"]["pool_1"]["throughput_ul_min"], 5.0, places=3)
        self.assertIn("layout.lane_isolation", {v["rule"] for v in run("lanes")["pcr_chip"]["violations"]})

    def test_lanes_balanced_and_two_module_lanes(self):
        spec = run("library_lanes")["lane_chip"]["spec"]
        blk, = spec["generator"]["params"]["lanes"]
        self.assertEqual(blk["lanes"], [["a_1", "m_1"], ["a_2", "m_2"], ["a_3", "m_3"], ["a_4", "m_4"]])
        self.assertEqual(blk["lane_channels"][0], ["ch_a_1_m_1"])
        sol = network.Network(spec).solve({"inlet_1": ("FEED", 4), "outlet_1": "VENT"})
        for ln in blk["lanes"]:
            self.assertAlmostEqual(sol["chambers"][ln[0]]["throughput_ul_min"], 1.0, places=2)

    def test_planar_and_no_vents_when_ports_run_out(self):
        r = run("lanes", "assayathon_demo")["pcr_chip"]
        blk, = r["spec"]["generator"]["params"]["lanes"]
        self.assertEqual(ccs.crossings(r["spec"]), [])
        self.assertEqual(blk["lane_vents"], [[], [], []])          # 6-port chip: no room for a vent per lane

    def test_not_lanes_stays_in_series(self):
        pl = plan("lanes")
        pl["chips"][0]["modules"][3].update(part="chamber", steps=[9])   # lanes no longer repeat
        res = layout.run(pl, IR, C.load("resin_print_multilayer"), None, Path(_TMP.name) / "series")
        r = res["pcr_chip"]
        self.assertEqual(r["spec"]["generator"]["params"]["lanes"], [])
        self.assertIn("layout.lanes", {v["rule"] for v in r["violations"]})


def one_chamber_plan(volume_ul: float, constraints_id: str) -> dict:
    return {"kind": "chip_plan", "id": "one", "protocol_id": "fixture_ir", "constraints_id": constraints_id,
            "chips": [{"id": "culture_chip", "name": "One culture chamber", "purpose": "Culture.", "steps": [1],
                       "modules": [{"id": "culture_1", "part": "chamber", "part_source": "parametric",
                                    "params": {"volume_ul": volume_ul}, "steps": [1],
                                    "inlets": ["medium", "wash_buffer", "enzyme"], "outlets": ["waste"],
                                    "does": "Holds the cells.", "holds": "cells"}]}],
            "handoffs": [], "off_chip": [], "missing_parts": [], "coverage": {"1": "culture_chip.culture_1"}}


class Fit(unittest.TestCase):
    """Reshape parametric parts / turn library parts before reporting a size error; one oversize module is its own rule."""

    def run_one(self, volume_ul, cid="pdms_soft_lithography"):
        out = Path(_TMP.name) / f"one_{volume_ul}_{cid}"
        return layout.run(one_chamber_plan(volume_ul, cid), IR, C.load(cid), None, out)["culture_chip"]

    def test_parametric_chamber_is_reshaped_to_fit(self):
        r = self.run_one(100)                       # 100 uL at 0.1 mm = 1000 mm2: too tall at its default aspect for 75 x 25
        spec = r["spec"]
        fx, fy = spec["footprint"]["x"], spec["footprint"]["y"]
        self.assertTrue((fx <= 75 and fy <= 25) or (fx <= 25 and fy <= 75), (fx, fy))
        self.assertEqual(errors(r["violations"]), [])
        self.assertIn("layout.reshaped", {v["rule"] for v in r["violations"]})
        c = spec["chambers"][0]
        self.assertAlmostEqual(c["size"][0] * c["size"][1] * c["size"][2], 100, delta=1.5)      # volume kept
        self.assertAlmostEqual(c["volume_ul"], 100, places=3)

    def test_single_module_too_large_is_its_own_rule(self):
        r = self.run_one(2000)                      # 20 000 mm2 of chamber can't fit 75 x 25 mm whatever its shape
        errs = errors(r["violations"])
        self.assertEqual({e["rule"] for e in errs}, {"module_too_large"}, [e["message"] for e in errs])
        msg = errs[0]["message"]
        self.assertIn("culture_1", msg)
        self.assertIn("splitting the chip won't help", msg)
        self.assertIn("uL", msg)
        self.assertNotIn("split its", msg)
        # checks.py says the same for any one-module chip
        spec = copy.deepcopy(r["spec"])
        self.assertEqual({v["rule"] for v in errors(checks.check_chip(spec, C.load("pdms_soft_lithography")))
                          if "max_chip" in v["rule"] or "too_large" in v["rule"]}, {"module_too_large"})

    def test_rounding_never_exceeds_the_limit(self):
        self.assertEqual(layout._snap(24.9, (25.2, None)), 25.0)
        self.assertEqual(layout._snap(25.1, (25.2, None)), 25.2)
        self.assertEqual(layout._snap(19.8, (30, 20)), 20.0)
        self.assertEqual(layout._snap(29.7, (30, 20)), 30.0)
        for name in ALL:
            cons = C.load(plan(name)["constraints_id"])
            X, Y = C.max_chip_mm(cons)
            for r in run(name).values():
                if not X or any(v["rule"] in ("layout.does_not_fit", "module_too_large") for v in r["violations"]):
                    continue
                fx, fy = r["spec"]["footprint"]["x"], r["spec"]["footprint"]["y"]
                self.assertTrue((fx <= X and fy <= Y) or (fx <= Y and fy <= X), (name, fx, fy, X, Y))

    def test_serpentine_refolds_with_the_same_length(self):
        md = {"shape_kind": "serpentine", "length": 300.0, "w_line": 0.3, "wall": 0.05,
              "extra": {"mixer": {"kind": "serpentine", "channel_width_mm": 0.3, "turns": 1, "length_mm": 300}}}
        layout._shape(md, 1.0, False, False)
        square = (md["w"], md["h"])
        layout._shape(md, 6.0, True, False)
        lanes, lane, pitch = md["fold"]
        self.assertAlmostEqual(lanes * lane, 300.0, places=6)
        self.assertGreater(md["w"] / md["h"], 3)                  # wide and flat now
        self.assertLess(md["w"] * md["h"], square[0] * square[1])  # tighter fold, smaller box

    def test_turned_library_part_stl_fills_its_box(self):
        import stl_io
        c = {"id": "x", "center": [10, 10, 1], "size": [8.0, 2.0, 0.3], "note": "culture_chamber (library) | rotated 90 deg"}
        tris, how = cad.part_mesh(c, {"stl": "cad/library/culture_chamber.stl"})
        lo, hi = stl_io.bbox(tris)
        self.assertEqual(how, "library")
        self.assertAlmostEqual(hi[0] - lo[0], 8.0, 3)
        self.assertAlmostEqual(hi[1] - lo[1], 2.0, 3)


class Thermal(unittest.TestCase):
    """thermal_zones come from the IR steps of each module; checks flag material, operating range and equipment."""

    IR_T = {"kind": "protocol_ir", "id": "t", "name": "t", "source": {"kind": "prompt"}, "placeholder": True, "steps": [
        {"n": 1, "text": "Lyse at 72 C.", "action": "lyse", "temp_c": 72},
        {"n": 2, "text": "Index PCR.", "action": "amplify", "conditions": {"thermal_cycles": 12}},
        {"n": 3, "text": "Hold cold.", "action": "store", "conditions": {"cold": True}},
        {"n": 4, "text": "Wash.", "action": "wash_cells"}]}

    def plan(self, cid):
        mods = [{"id": f"m{i}", "part": "chamber", "part_source": "parametric", "params": {"volume_ul": 5}, "steps": [i],
                 "does": "x"} for i in (1, 2, 3, 4)]
        return {"kind": "chip_plan", "id": "t", "protocol_id": "t", "constraints_id": cid, "handoffs": [], "off_chip": [],
                "missing_parts": [], "coverage": {}, "chips": [{"id": "hot_chip", "name": "Hot", "purpose": "x",
                                                                 "steps": [1, 2, 3, 4], "modules": mods}]}

    def run_t(self, cons, tag):
        return layout.run(self.plan(cons["id"]), self.IR_T, cons, None, Path(_TMP.name) / f"thermal_{tag}")["hot_chip"]

    def test_zones_from_steps(self):
        r = self.run_t(C.load("unconstrained"), "zones")
        z = {tuple(x["chambers"]): x for x in r["spec"]["thermal_zones"]}
        self.assertEqual((z[("m1",)]["min_temp_c"], z[("m1",)]["max_temp_c"], z[("m1",)]["purpose"]), (72, 72, "heating"))
        self.assertEqual((z[("m2",)]["min_temp_c"], z[("m2",)]["max_temp_c"], z[("m2",)]["purpose"]), (55, 95, "pcr"))
        self.assertEqual((z[("m3",)]["max_temp_c"], z[("m3",)]["purpose"]), (4, "cold"))
        self.assertNotIn(("m4",), z)                                       # room temperature: no zone
        self.assertEqual(schema_io.schema_errors("chip_spec", r["spec"]), [])
        self.assertEqual(errors(r["violations"]), [])

    def test_resin_material_operating_range_and_equipment(self):
        cons = C.load("resin_print_multilayer")
        cons["operating"] = {"temp_range_c": [4, 50]}
        cons["off_chip"]["equipment"] = ["syringe_pump", "heater", "microscope"]
        rules = [e["rule"] for e in errors(self.run_t(cons, "resin")["violations"])]
        self.assertIn("material.max_temp", rules)               # resin: 50 C max, the PCR zone reaches 95 C
        self.assertIn("operating.temp_range_c", rules)          # 72 C lysis and 95 C PCR are outside 4-50 C
        msgs = [e["message"] for e in errors(self.run_t(cons, "resin")["violations"]) if e["rule"] == "off_chip.equipment"]
        self.assertTrue(any("thermocycler" in m for m in msgs) and any("cold plate" in m for m in msgs), msgs)

    def test_zoning_not_allowed(self):
        cons = C.load("unconstrained")
        cons["fabrication"]["thermal_zoning_allowed"] = False
        self.assertIn("thermal_zoning_allowed", {e["rule"] for e in errors(self.run_t(cons, "zoning")["violations"])})


class Checks(unittest.TestCase):
    def setUp(self):
        self.spec = copy.deepcopy(run("four_reagents")["stain_chip"]["spec"])
        self.cons = C.load("resin_print_multilayer")

    def rules(self, spec=None, cons=None):
        return {v["rule"] for v in errors(checks.check_chip(spec or self.spec, cons or self.cons))}

    def test_clean_spec_has_no_errors(self):
        self.assertEqual(self.rules(), set())

    def test_footprint(self):
        self.spec["footprint"]["x"] = 200
        self.assertIn("envelope.max_chip_mm", self.rules())

    def test_narrow_channel_and_depth(self):
        self.spec["channels"][0]["width_mm"] = 0.1
        self.spec["channels"][1]["depth_mm"] = 3
        self.assertTrue({"min_feature.channel_width", "depth.max_mm"} <= self.rules())

    def test_open_vs_enclosed_minimum(self):
        self.assertEqual(checks.min_channel_mm(C.load("resin_print_multilayer")), 0.5)    # printed closed
        self.assertEqual(checks.min_channel_mm(C.load("assayathon_demo")), 0.2)           # printed open, sealed by film

    def test_single_depth(self):
        cons = C.load("assayathon_demo")
        self.spec["chambers"][0]["size"][2] = 0.2
        self.assertIn("depth.single_depth", self.rules(cons=cons))

    def test_port_pitch_and_wall(self):
        a, b = self.spec["ports"][2], self.spec["ports"][3]
        b["position"] = [a["position"][0] + 1, a["position"][1], a["position"][2]]
        self.assertIn("ports.min_pitch_mm", self.rules())
        spec = copy.deepcopy(self.spec)
        c0, c1 = spec["chambers"]
        c1["center"] = [c0["center"][0] + c0["size"][0] / 2 + c1["size"][0] / 2 + 0.1, c0["center"][1], c1["center"][2]]
        self.assertIn("min_feature.wall_mm", self.rules(spec))

    def test_crossings_planar_vs_multilayer(self):
        ch = self.spec["channels"]
        a = next(c for c in ch if c["to"] == "outlet_1")              # shares no node with b
        b = next(c for c in ch if c["to"] == "waste_out")
        y = a["path"][0][1]
        xm = (a["path"][0][0] + a["path"][-1][0]) / 2
        b0, b1 = b["path"][0], b["path"][-1]
        b["path"] = [b0, [xm, b0[1]], [xm, y + 1], [b1[0], y + 1], b1]
        self.assertTrue(checks.crossings(self.spec))
        self.assertIn("channel_crossings", self.rules(cons=C.load("assayathon_demo")))
        b["note"] = "layer fluidic_2"
        self.assertNotIn("channel_crossings", self.rules())                      # multilayer: fine on two layers
        self.assertIn("multilayer.allowed", self.rules(cons=C.load("assayathon_demo")))

    def test_valves_ports_gap_temperature(self):
        spec = copy.deepcopy(run("unconstrained")["long_chip"]["spec"])
        self.assertIn("on_chip_valves.allowed", self.rules(spec, C.load("resin_print_multilayer")))
        self.spec["chambers"][0]["filter"] = {"type": "pillars", "gap_um": 20, "purpose": "particles"}
        self.spec["thermal_zones"] = [{"id": "heat_zone", "chambers": ["stain_trap"], "max_temp_c": 95, "purpose": "pcr"}]
        r = self.rules()
        self.assertTrue({"min_feature.gap_mm", "material.max_temp"} <= r, r)
        cons = C.load("glass_laser_3d_etch")
        self.spec["thermal_zones"][0]["max_temp_c"] = 120
        self.assertIn("operating.temp_range_c", self.rules(cons=cons))
        cons = C.load("assayathon_demo")
        msgs = [v["message"] for v in errors(checks.check_chip(self.spec, cons)) if v["rule"] == "ports.max_per_chip"]
        self.assertEqual(len(msgs), 1)
        self.assertIn("max 6", msgs[0])


class Cad(unittest.TestCase):
    def test_files_written(self):
        r = run("four_reagents")["stain_chip"]
        f = r["files"]
        for key in ("svg", "dxf", "stl", "stl_fluidic", "fusion", "build", "readme", "part:stain_trap", "part:image_window"):
            self.assertTrue(Path(f[key]).is_file(), key)
        svg = Path(f["svg"]).read_text()
        self.assertTrue(svg.startswith("<svg") and "fixative_in" in svg and "(2.5, 8)" in svg.replace("8.0)", "8)"))
        dxf = Path(f["dxf"]).read_text()
        for layer in ("OUTLINE", "CHAMBERS", "CHANNELS", "PORTS"):
            self.assertIn(f"\n8\n{layer}\n", dxf)
        self.assertIn("AC1009", dxf)
        self.assertTrue(dxf.rstrip().endswith("EOF"))
        # channel outlines are POLYLINEs, so the solver's LINE-rectangle DXF reader ignores them (falls back to `path`)
        self.assertEqual(network.dxf_channel_segments(Path(f["dxf"])), [])
        compile(Path(f["fusion"]).read_text(), "fusion", "exec")
        build = Path(f["build"]).read_text()
        self.assertIn("stain_trap", build)
        fp = r["spec"]["footprint"]
        self.assertIn(f"{fp['x']:g} x {fp['y']:g} mm", build)

    def test_library_part_stl_is_placed_and_scaled(self):
        r = run("assayathon")["capsule_chip"]
        import stl_io
        trap = next(c for c in r["spec"]["chambers"] if c["id"] == "trap")
        lo, hi = stl_io.bbox(stl_io.read_stl(Path(r["files"]["part:trap"])))
        for i in range(3):
            self.assertAlmostEqual((lo[i] + hi[i]) / 2, trap["center"][i], 2)
            self.assertAlmostEqual(hi[i] - lo[i], trap["size"][i], 2)

    def test_labels_never_overlap_or_cover_a_port(self):
        for name in ("four_reagents", "lanes", "assayathon"):
            for r in run(name).values():
                spec = r["spec"]
                lab = cad.place_labels(spec, 12.0, 11, 10, (-5, -5, spec["footprint"]["x"] + 5, spec["footprint"]["y"] + 5))
                boxes = [(b[0], b[1], b[0] + b[2], b[1] + b[3]) for b in list(lab["ports"].values()) + list(lab["modules"].values())]
                for i, a in enumerate(boxes):
                    for b in boxes[i + 1:]:
                        self.assertFalse(min(a[2], b[2]) - max(a[0], b[0]) > 1e-6 and min(a[3], b[3]) - max(a[1], b[1]) > 1e-6,
                                         (name, a, b))
                for p in spec["ports"]:
                    x, y = p["position"][:2]
                    for pid, b in lab["ports"].items():
                        if pid != p["id"]:
                            self.assertFalse(b[0] < x < b[0] + b[2] and b[1] < y < b[1] + b[3], (pid, p["id"]))

    def test_channel_outline(self):
        pts = cad.channel_outline([[0, 0], [10, 0], [10, 5]], 1.0)
        self.assertEqual(len(pts), 6)
        self.assertIn((10.5, -0.5), [(round(x, 6), round(y, 6)) for x, y in pts])


if __name__ == "__main__":
    unittest.main()

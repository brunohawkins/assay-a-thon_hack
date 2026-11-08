"""autochip parts registry and partition stage (rules backend, no network).

    python3 -m pytest -q tests/test_autochip_partition.py
"""
LIB_ONLY = str(__import__("pathlib").Path(__file__).parent / "fixtures" / "autochip" / "assayathon_library_only.json")
import copy
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from autochip import constraints as C  # noqa: E402
from autochip import parts as P  # noqa: E402
from autochip.llm import Agent  # noqa: E402
from autochip.stages import partition  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "autochip" / "partition"
EXAMPLES = ["spc_capsule_protocol", "capsule_scrnaseq_prompt", "bead_cleanup_protocol"]
CHAMBER_TYPES = json.loads((ROOT / "contracts" / "schemas" / "chip_spec.schema.json").read_text())["$defs"]["chamber"]["properties"]["type"]["enum"]


def load_ir(name: str) -> dict:
    return json.loads((FIX / f"{name}.ir.json").read_text())


def plan_for(name: str, cons: str | dict) -> tuple[dict, dict, dict, dict]:
    ir = load_ir(name)
    c = C.load(cons) if isinstance(cons, str) else cons
    parts = P.load_parts(c)
    plan = partition.run(ir, c, parts, Agent(backend="rules"), None)
    return plan, ir, c, parts


class FakeAgent:
    """Stands in for a live agent: records the prompt and answers with a given object."""
    live = True

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def ask_json(self, stage, system, user, validate=None, key=None):
        self.calls.append({"stage": stage, "system": system, "user": user, "key": key})
        errs = validate(self.answer) if validate else []
        if errs:
            raise AssertionError(errs)
        return copy.deepcopy(self.answer)


class TestParts(unittest.TestCase):
    def test_every_preset_loads(self):
        for preset in C.list_presets():
            parts = P.load_parts(C.load(preset))
            self.assertTrue(P.available(parts), preset)
            for name, p in parts.items():
                self.assertIn(p["chip_type"], CHAMBER_TYPES, name)
                self.assertGreaterEqual(p["openings"], 1)
                self.assertEqual(p["available"], p["why_unavailable"] is None)
                if p["source"] == "parametric":
                    self.assertTrue(p["params"], name)
                    self.assertEqual(len(p["size_mm"]), 3)

    def test_parametric_catalogue(self):
        parts = P.load_parts(C.load("unconstrained"))
        for name in ["chamber", "culture_chamber_param", "trap_chamber", "mixer", "delay_line", "pillar_filter",
                     "droplet_junction", "merge", "splitter", "pool", "thermal_chamber", "magnetic_chamber",
                     "optical_window", "open_well"]:
            self.assertEqual(parts[name]["source"], "parametric", name)
            self.assertTrue(parts[name]["available"], name)
        self.assertEqual(parts["droplet_junction"]["openings"], 4)
        self.assertEqual(set(parts["thermal_chamber"]["needs_one_of"]), {"thermocycler", "peltier_stage"})
        self.assertEqual(parts["magnetic_chamber"]["needs_one_of"], ["magnet"])
        self.assertEqual(parts["trap_chamber"]["params"]["retention"]["choices"], ["mesh", "weir", "pillars"])
        self.assertEqual(parts["culture_chamber"]["source"], "library")
        self.assertTrue(parts["culture_chamber"]["stl"].endswith(".stl"))

    def test_source_filters(self):
        lib = P.load_parts(C.load(LIB_ONLY))
        self.assertTrue(all(p["source"] == "library" for p in P.available(lib).values()))
        self.assertIn("library parts only", P.why_unavailable(lib)["pillar_filter"])
        par = P.load_parts(C.load("pdms_soft_lithography"))
        self.assertTrue(all(p["source"] == "parametric" for p in P.available(par).values()))

    def test_valves_equipment_and_min_feature(self):
        why = P.why_unavailable(P.load_parts(C.load("assayathon_demo")))
        self.assertIn("valves", why["normally_open_valve"])
        self.assertIn("magnet", P.why_unavailable(P.load_parts(C.load("pdms_soft_lithography")))["magnetic_chamber"])
        lam = P.load_parts(C.load("laser_cut_laminate"))
        self.assertIn("250", lam["pillar_filter"]["why_unavailable"])
        self.assertEqual(lam["droplet_junction"]["params"]["nozzle_um"]["min"], 250)
        self.assertTrue(P.load_parts(C.load("unconstrained"))["valve"]["available"])

    def test_allowed_and_forbidden(self):
        c = C.load("unconstrained")
        c["parts"]["forbidden"] = ["spiral_channel"]
        c["parts"]["allowed"] = ["spiral_channel", "chamber", "merge", "mixer"]
        parts = P.load_parts(c)
        self.assertEqual(set(P.available(parts)), {"chamber", "merge", "mixer"})
        self.assertIn("forbidden", parts["spiral_channel"]["why_unavailable"])
        self.assertIn("parts.allowed", parts["t_junction"]["why_unavailable"])

    def test_prompt_listing_leaves_out_unusable_parts(self):
        parts = P.load_parts(C.load(LIB_ONLY))
        text = P.parts_for_prompt(parts)
        self.assertIn("- culture_chamber [library", text)
        self.assertNotIn("- pillar_filter", text)
        self.assertNotIn("- normally_open_valve", text)

    def test_footprint_sizes_by_volume(self):
        c = C.load("unconstrained")
        parts = P.load_parts(c)
        small = P.footprint_mm(parts["chamber"], {"volume_ul": 10}, c)
        big = P.footprint_mm(parts["chamber"], {"volume_ul": 200}, c)
        self.assertGreater(big[0] * big[1], small[0] * small[1] * 10)
        long_delay = P.footprint_mm(parts["delay_line"], {"residence_s": 1800, "flow_ul_min": 20}, c)
        short_delay = P.footprint_mm(parts["delay_line"], {"residence_s": 60, "flow_ul_min": 20}, c)
        self.assertGreater(long_delay[0], short_delay[0])


class TestRulesPlans(unittest.TestCase):
    def test_all_examples_valid_on_all_presets(self):
        for name in EXAMPLES:
            for preset in C.list_presets():
                plan, ir, c, parts = plan_for(name, preset)
                self.assertEqual(partition.validate(plan, ir, c, parts), [], f"{name} / {preset}")
                self.assertEqual(set(plan["coverage"]), {str(s["n"]) for s in ir["steps"]})

    def test_spc_unconstrained_grouping(self):
        plan, ir, c, parts = plan_for("spc_capsule_protocol", "unconstrained")
        cov = plan["coverage"]
        chip_of = lambda n: cov[str(n)].split(".")[0]  # noqa: E731
        # culture + rinse + dissociation in one chamber
        self.assertEqual(cov["1"], cov["2"])
        self.assertEqual(cov["1"], cov["3"])
        # quench: merge then mixer, both listing step 4
        mods = {m["id"]: m for ch in plan["chips"] for m in ch["modules"]}
        doing4 = [m["part"] for m in mods.values() if 4 in m["steps"]]
        self.assertEqual(doing4, ["merge", "mixer"])
        # break emulsion + cure + wash in one trap
        self.assertEqual(len({cov["12"], cov["13"], cov["14"]}), 1)
        self.assertEqual(mods[cov["12"].split(".")[1]]["part"], "trap_chamber")
        # oil never on the culture chip; gelation delay line sized by the flow
        self.assertNotEqual(chip_of(1), chip_of(10))
        delay = mods[cov["11"].split(".")[1]]
        self.assertEqual(delay["part"], "delay_line")
        self.assertEqual(delay["params"]["residence_s"], 1200)
        # split-and-pool: 4 portion chambers on the thermal cycling chip
        pcr = [m for m in mods.values() if 21 in m["steps"]]
        self.assertEqual(len(pcr), 4)
        self.assertTrue(all(m["part"] == "thermal_chamber" for m in pcr))
        # magnet and thermocycler on different chips
        self.assertNotEqual(chip_of(21), chip_of(23))
        self.assertEqual(cov["8"], "off_chip")
        self.assertEqual(len(plan["handoffs"]), len(plan["chips"]) - 1)

    def test_assayathon_library_only_and_ports(self):
        plan, ir, c, parts = plan_for("spc_capsule_protocol", LIB_ONLY)
        for ch in plan["chips"]:
            n_ports = 2 + sum(len(m.get("inlets", [])) + len(m.get("outlets", [])) for m in ch["modules"])
            self.assertLessEqual(n_ports, 6, ch["id"])
            for m in ch["modules"]:
                self.assertEqual(m["part_source"], "library")
        self.assertEqual(plan["coverage"]["7"], "missing")     # no filter block in the library
        self.assertEqual(plan["coverage"]["23"], "off_chip")   # no magnet in this lab

    def test_bead_cleanup_without_magnet_stays_at_bench(self):
        plan, ir, c, parts = plan_for("bead_cleanup_protocol", "assayathon_demo")
        self.assertEqual([plan["coverage"][str(n)] for n in range(2, 8)], ["off_chip"] * 6)
        plan, ir, c, parts = plan_for("bead_cleanup_protocol", "unconstrained")
        self.assertEqual(len(plan["chips"]), 1)
        self.assertEqual(plan["chips"][0]["modules"][0]["part"], "magnetic_chamber")

    def test_objectives_change_the_split(self):
        few = C.load("unconstrained")
        few["objectives"]["prefer"] = "fewest_chips"
        per = C.load("unconstrained")
        per["objectives"]["prefer"] = "one_chip_per_module"
        n_default = len(plan_for("spc_capsule_protocol", "unconstrained")[0]["chips"])
        n_few = len(plan_for("spc_capsule_protocol", few)[0]["chips"])
        plan_per, ir, c, parts = plan_for("spc_capsule_protocol", per)
        self.assertLessEqual(n_few, n_default)
        self.assertGreater(len(plan_per["chips"]), n_default)
        self.assertEqual(partition.validate(plan_per, ir, c, parts), [])

    def test_encapsulation_lines_chosen_by_role(self):
        """The junction gets the shell and the oil on lines of their own whatever the reagent order; the core
        stream arrives on the main line (merged upstream, or through a merge + mixer right before the junction)."""
        base = load_ir("spc_capsule_protocol")
        enc = next(s for s in base["steps"] if s["action"] == "encapsulate")
        orders = [["core_solution", "shell_polymer", "oil"], ["shell_polymer", "oil", "core_solution"],
                  ["oil", "core_solution", "shell_polymer"]]
        for preset in ("unconstrained", "assayathon_demo", "laser_cut_laminate"):
            for upstream_merge in (True, False):
                for order in orders:
                    ir = copy.deepcopy(base)
                    if not upstream_merge:   # no resuspend-in-core step: the core is not on the stream yet
                        ir["steps"] = [s for s in ir["steps"] if s["action"] != "resuspend"]
                    next(s for s in ir["steps"] if s["n"] == enc["n"])["reagents"] = order
                    c = C.load(preset)
                    parts = P.load_parts(c)
                    plan = partition.run(ir, c, parts, Agent(backend="rules"), None)
                    label = f"{preset} merged={upstream_merge} {order}"
                    self.assertEqual(partition.validate(plan, ir, c, parts), [], label)
                    mods = [(ch, m) for ch in plan["chips"] for m in ch["modules"] if enc["n"] in m["steps"]]
                    junction = [m for ch, m in mods if parts[m["part"]]["role"] == "flow_focusing"]
                    self.assertEqual(len(junction), 1, label)
                    self.assertEqual(junction[0]["inlets"], ["shell_polymer", "oil"], label)
                    self.assertEqual(plan["coverage"][str(enc["n"])].split(".")[1], junction[0]["id"], label)
                    core_lines = [m for ch, m in mods if "core_solution" in m.get("inlets", [])]
                    self.assertEqual(len(core_lines), 0 if upstream_merge else 1, label)
                    all_inlets = [i for ch in plan["chips"] for m in ch["modules"] for i in m.get("inlets", [])]
                    self.assertEqual(all_inlets.count("core_solution"), 1, label)

    def test_oil_phase_keeps_its_own_line(self):
        c = C.load("unconstrained")
        c["ports"]["max_per_chip"] = 5
        plan, ir, c, parts = plan_for("spc_capsule_protocol", c)
        self.assertEqual(partition.validate(plan, ir, c, parts), [])
        trap = next(m for ch in plan["chips"] for m in ch["modules"] if 12 in m["steps"])
        self.assertIn("emulsion_breaker", trap["inlets"])
        self.assertIn("reagent_line", trap["inlets"])

    def test_chip_names_describe_contents(self):
        for name in EXAMPLES:
            for preset in ("unconstrained", "assayathon_demo"):
                plan, ir, c, parts = plan_for(name, preset)
                acts = {s["n"]: s["action"] for s in ir["steps"]}
                for ch in plan["chips"]:
                    done = {acts[n] for n in ch["steps"]}
                    for action, word in (("encapsulate", "encapsulation"), ("amplify", "index_pcr"), ("lyse", "lysis_rt"),
                                         ("reverse_transcribe", "lysis_rt"), ("release", "release"), ("culture", "cell_culture"),
                                         ("break_emulsion", "capsule_curing")):
                        if action in done:
                            self.assertIn(word, ch["id"], f"{name}/{preset}: {ch['id']} does {action}")
                    self.assertNotIn("etc", ch["id"])

    def test_section_headings_are_hand_offs(self):
        ir = load_ir("spc_capsule_protocol")
        for s in ir["steps"]:
            sec = "1.1 Cell preparation" if s["n"] <= 9 else "1.2 Capsule generation" if s["n"] <= 15 else "1.3 Processing inside capsules"
            s["source_ref"] = f"{sec}, sentence {s['n']}"
        c = C.load("unconstrained")
        c["fabrication"]["thermal_zoning_allowed"] = True
        parts = P.load_parts(c)
        plan = partition.run(ir, c, parts, Agent(backend="rules"), None)
        self.assertEqual(partition.validate(plan, ir, c, parts), [])
        chip_of = {int(k): v.split(".")[0] for k, v in plan["coverage"].items() if v.startswith("chip")}
        self.assertNotEqual(chip_of[15], chip_of[16])
        self.assertIn("processing inside capsules", " ".join(ch["why_separate"] for ch in plan["chips"]))

    def test_no_manual_steps(self):
        c = C.load("unconstrained")
        c["off_chip"]["manual_steps_allowed"] = False
        plan, ir, c, parts = plan_for("spc_capsule_protocol", c)
        self.assertEqual(plan["off_chip"], [])
        self.assertEqual(partition.validate(plan, ir, c, parts), [])
        self.assertTrue(plan["coverage"]["8"].startswith("chip"))  # counted in an optical window

    def test_port_limit_splits_chips(self):
        c = C.load("unconstrained")
        c["ports"]["max_per_chip"] = 4
        plan, ir, c, parts = plan_for("spc_capsule_protocol", c)
        self.assertEqual(partition.validate(plan, ir, c, parts), [])
        self.assertTrue(any("ports" in (ch.get("why_separate") or "") for ch in plan["chips"]))


class TestValidate(unittest.TestCase):
    def setUp(self):
        self.plan, self.ir, self.c, self.parts = plan_for("spc_capsule_protocol", "unconstrained")

    def errs(self, plan, c=None):
        return " | ".join(partition.validate(plan, self.ir, c or self.c, self.parts))

    def test_schema_error(self):
        bad = copy.deepcopy(self.plan)
        del bad["coverage"]
        self.assertIn("coverage", self.errs(bad))

    def test_semantic_errors(self):
        bad = copy.deepcopy(self.plan)
        ch = bad["chips"][0]
        del bad["coverage"]["5"]
        ch["modules"][1]["id"] = ch["modules"][0]["id"]
        ch["modules"][2]["part"] = "flux_capacitor"
        ch["modules"][2]["inlets"] = ["medium"]
        bad["handoffs"][0]["to_chip"] = "chip99"
        bad["coverage"]["99"] = "off_chip"
        e = self.errs(bad)
        for want in ["step 5 is not covered", "used twice", "no part 'flux_capacitor'", "inlet name 'medium'",
                     "no chip 'chip99'", "'99' is not a protocol step"]:
            self.assertIn(want, e)

    def test_encapsulation_without_own_oil_line(self):
        bad = copy.deepcopy(self.plan)
        junction = next(m for ch in bad["chips"] for m in ch["modules"] if m["part"] == "droplet_junction")
        junction["inlets"] = ["core_solution", "reagent_line"]
        e = self.errs(bad)
        self.assertIn("needs its own oil inlet", e)
        self.assertIn("needs its own shell inlet 'shell_polymer'", e)

    def test_unavailable_part_ports_and_chip_count(self):
        c = C.load("unconstrained")
        c["ports"]["max_per_chip"] = 3
        c["envelope"]["max_chips"] = 2
        c["fabrication"]["on_chip_valves"]["allowed"] = False
        parts = P.load_parts(c)
        bad = copy.deepcopy(self.plan)
        bad["chips"][0]["modules"][0]["part"] = "valve"
        bad["chips"][0]["modules"][0].pop("params", None)
        e = " | ".join(partition.validate(bad, self.ir, c, parts))
        self.assertIn("can't be used here: on-chip valves", e)
        self.assertIn("but the limit is 3", e)
        self.assertIn("at most 2", e)

    def test_param_checks(self):
        bad = copy.deepcopy(self.plan)
        mods = {m["id"]: m for ch in bad["chips"] for m in ch["modules"]}
        mods["filter_1"]["params"]["gap_um"] = 2          # below gap_mm (10 um)
        mods["trap_1"]["params"]["retention"] = "glue"
        mods["delay_1"]["params"]["speed"] = 3
        mods["split_1"]["params"]["n_out"] = 1.5
        e = self.errs(bad)
        self.assertIn("below what the process can make", e)
        self.assertIn("must be one of", e)
        self.assertIn("no param 'speed'", e)
        self.assertIn("whole number", e)

    def test_step_needing_missing_equipment_on_chip(self):
        c = C.load("unconstrained")
        c["off_chip"]["equipment"] = [e for e in c["off_chip"]["equipment"] if e not in ("thermocycler", "peltier_stage")]
        e = " | ".join(partition.validate(self.plan, self.ir, c, P.load_parts(c)))
        self.assertIn("step 21 needs thermocycler or peltier_stage", e)


class TestRound2(unittest.TestCase):
    def test_cell_filter_gap_below_nozzle(self):
        plan, ir, c, parts = plan_for("spc_capsule_protocol", "glass_laser_3d_etch")
        mods = {m["id"]: m for ch in plan["chips"] for m in ch["modules"]}
        cell_filter = next(m for m in mods.values() if (m.get("params") or {}).get("purpose") == "cells")
        nozzle = next(m for m in mods.values() if m["part"] == "droplet_junction")["params"]["nozzle_um"]
        self.assertEqual(nozzle, 40)
        self.assertEqual(cell_filter["params"]["gap_um"], 30)          # source said 40 um; capped at 0.75 x 40
        self.assertIn("capped at 30 um", cell_filter["does"])
        capsule_filter = next(m for m in mods.values() if (m.get("params") or {}).get("purpose") == "capsule_aggregates")
        self.assertEqual(capsule_filter["params"]["gap_um"], 100)      # capsule filters are not capped
        # a process that can't make a 30 um gap: missing part with a workaround
        plan, ir, c, parts = plan_for("spc_capsule_protocol", "resin_print_multilayer")
        miss = next(m for m in plan["missing_parts"] if 7 in m["steps"])
        self.assertIn("30 um", miss["need"])
        self.assertIn("workaround", miss)
        # no encapsulation downstream: the source's gap stands
        ir = load_ir("spc_capsule_protocol")
        ir["steps"] = [s for s in ir["steps"] if s["n"] < 10]
        c = C.load("glass_laser_3d_etch")
        plan = partition.run(ir, c, P.load_parts(c), Agent(backend="rules"), None)
        self.assertEqual(next(m for ch in plan["chips"] for m in ch["modules"] if m["part"] == "pillar_filter")["params"]["gap_um"], 40)

    def resplit(self, plan, ir, c, parts, feedback):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "chip_plan.json").write_text(json.dumps(plan))
            return partition.run(ir, c, parts, Agent(backend="rules"), Path(tmp), feedback=feedback)

    def test_rules_resplit_on_layout_feedback(self):
        plan, ir, c, parts = plan_for("spc_capsule_protocol", "unconstrained")
        big = next(ch for ch in plan["chips"] if any(m["part"] == "splitter" for m in ch["modules"]))
        single = next(ch for ch in plan["chips"] if len(ch["modules"]) == 1)
        first = plan["chips"][0]
        fb = [f"chip {big['id']}: chip is 84.5 x 42.0 mm, larger than the 75 x 50 mm maximum: split its "
              f"{len(big['modules'])} modules over two chips (about 4 each), or use smaller parts.",
              f"chip {single['id']}: chip is 80.0 x 60.0 mm, larger than the 75 x 50 mm maximum: split its 1 modules over "
              f"two chips (about 1 each), or use smaller parts.",
              f"chip {first['id']}: chip has 9 ports, max 6: move 3 of its extra lines (...) together with the modules "
              f"they feed to a second chip, or share one inlet between reagents."]
        new = self.resplit(plan, ir, c, parts, fb)
        self.assertEqual(partition.validate(new, ir, c, parts), [])
        self.assertEqual(len(new["chips"]), len(plan["chips"]) + 2)
        self.assertEqual([ch["id"].split("_")[0] for ch in new["chips"]], [f"chip{i}" for i in range(1, len(new["chips"]) + 1)])
        # the hand-offs chain the chips in order
        self.assertEqual([(h["from_chip"], h["to_chip"]) for h in new["handoffs"]],
                         list(zip([c["id"] for c in new["chips"]], [c["id"] for c in new["chips"]][1:])))
        chip_of = {m["id"]: ch["id"] for ch in new["chips"] for m in ch["modules"]}
        # merge + mixer pairs and the split -> lanes -> pool block stay together
        self.assertEqual(chip_of["join_1"], chip_of["mix_1"])
        self.assertEqual(chip_of["join_2"], chip_of["mix_2"])
        self.assertEqual(len({chip_of[m] for m in ("split_1", "pcr_1", "pcr_2", "pcr_3", "pcr_4", "pool_1")}), 1)
        # the PCR chip was cut; the one-module chip was not, and the rationale says so
        self.assertNotEqual(chip_of["trap_2"], chip_of["split_1"])
        self.assertIn(f"{single['id']} can't be split further (one module)", new["rationale"])
        # names still describe contents
        self.assertTrue(any("index_pcr" in ch["id"] for ch in new["chips"]))
        for n, where in new["coverage"].items():
            if where.startswith("chip"):
                self.assertEqual(chip_of[where.split(".")[1]], where.split(".")[0], n)
        # feeding the same errors again keeps cutting the pieces that still have more than one unit
        again = self.resplit(new, ir, c, parts, [f"chip {new['chips'][0]['id']}: chip is 99 x 99 mm, larger than the 75 x 50 mm maximum"])
        self.assertEqual(partition.validate(again, ir, c, parts), [])

    def test_resplit_respects_ports_and_max_chips(self):
        plan, ir, c, parts = plan_for("spc_capsule_protocol", "unconstrained")
        c = copy.deepcopy(c)
        c["envelope"]["max_chips"] = len(plan["chips"])
        new = self.resplit(plan, ir, c, parts, [f"chip {plan['chips'][0]['id']}: chip has 9 ports, max 6: move 3 lines"])
        self.assertEqual(len(new["chips"]), len(plan["chips"]))
        self.assertIn("not split: envelope.max_chips", new["rationale"])

    def test_coculture_gets_one_chamber_per_population(self):
        plan, ir, c, parts = plan_for("capsule_scrnaseq_prompt", "unconstrained")
        self.assertEqual(partition.validate(plan, ir, c, parts), [])
        chip = plan["chips"][0]
        ids = [m["id"] for m in chip["modules"]]
        self.assertEqual(ids[:5], ["split_1", "culture_neurons", "culture_microglia", "culture_astrocytes", "junction_mid"])
        mods = {m["id"]: m for m in chip["modules"]}
        for pop in ("neurons", "microglia", "astrocytes"):
            m = mods[f"culture_{pop}"]
            self.assertEqual(m["steps"][:2], [1, 2])
            self.assertIn(f"medium_{pop}", m["inlets"])
        self.assertEqual(mods["culture_neurons"]["outlets"], [])            # vents through the junction
        self.assertEqual(mods["culture_microglia"]["outlets"], ["microglia_vent"])
        self.assertEqual(parts[mods["junction_mid"]["part"]]["role"], "pool")
        self.assertEqual(chip["id"], "chip1_cell_culture")
        # a single population stays one chamber
        plan, ir, c, parts = plan_for("spc_capsule_protocol", "unconstrained")
        self.assertEqual([m["part"] for m in plan["chips"][0]["modules"]][0], "culture_chamber_param")
        # 6 ports: still one chamber per population, venting through the unfed medium ports
        plan, ir, c, parts = plan_for("capsule_scrnaseq_prompt", "assayathon_demo")
        self.assertEqual(partition.validate(plan, ir, c, parts), [])
        chip = plan["chips"][0]
        self.assertEqual([m["id"] for m in chip["modules"]][:5],
                         ["split_1", "culture_neurons", "culture_microglia", "culture_astrocytes", "junction_mid"])
        self.assertEqual([m["inlets"] for m in chip["modules"][1:4]], [["medium_neurons"], ["medium_microglia"], ["medium_astrocytes"]])
        self.assertLessEqual(2 + sum(len(m.get("inlets", [])) + len(m.get("outlets", [])) for m in chip["modules"]), 6)
        self.assertIn("co-culture", plan["rationale"].lower())
        # 4 ports: last resort, one chamber, reported as an open problem (rationale + missing_parts), coverage kept
        c = C.load("unconstrained")
        c["ports"]["max_per_chip"] = 4
        plan, ir, c, parts = plan_for("capsule_scrnaseq_prompt", c)
        self.assertEqual(partition.validate(plan, ir, c, parts), [])
        first = plan["chips"][0]["modules"][0]
        self.assertEqual(first["id"], "culture_all")
        self.assertIn("ports", first["does"])
        self.assertIn("OPEN PROBLEM", plan["rationale"])
        self.assertTrue(any("co-culture chip" in m["need"] for m in plan["missing_parts"]))
        self.assertTrue(plan["coverage"]["1"].endswith(".culture_all"))

    def test_index_lanes_get_their_own_lines(self):
        for preset in ("unconstrained", "assayathon_demo"):
            plan, ir, c, parts = plan_for("spc_capsule_protocol", preset)
            lanes = [m for ch in plan["chips"] for m in ch["modules"] if 21 in m["steps"]]
            self.assertEqual([m["inlets"] for m in lanes], [[f"index_mix_{k}"] for k in range(1, 5)], preset)
        # validate() rejects lanes that all get the same reagent line
        plan, ir, c, parts = plan_for("spc_capsule_protocol", "unconstrained")
        bad = copy.deepcopy(plan)
        lanes = [m for ch in bad["chips"] for m in ch["modules"] if 21 in m["steps"]]
        for k, m in enumerate(lanes, 1):
            m["inlets"] = ["index_pcr_mix" if k == 1 else f"index_pcr_mix_{k}"]
        e = " | ".join(partition.validate(bad, ir, c, parts))
        self.assertIn("all get the same index_pcr_mix line", e)
        lanes[0]["inlets"] = []
        self.assertIn("have no reagent line", " | ".join(partition.validate(bad, ir, c, parts)))
        # a split whose portions get the same reagent is fine with a shared name
        ir2 = copy.deepcopy(ir)
        next(s for s in ir2["steps"] if s["n"] == 21)["text"] = "Run PCR on each portion (placeholder number of cycles)."
        plan2 = partition.run(ir2, c, parts, Agent(backend="rules"), None)
        self.assertEqual(partition.validate(plan2, ir2, c, parts), [])

    def test_coculture_design_files_build_and_layout(self):
        plan, ir, c, parts = plan_for("capsule_scrnaseq_prompt", "unconstrained")
        designs = partition.design_files(plan, ir)
        self.assertEqual([m["part"] for m in designs[plan["chips"][0]["id"]]["modules"][:5]],
                         ["flow_manifold", "culture_chamber", "culture_chamber", "culture_chamber", "ring_junction"])


class TestAgentPath(unittest.TestCase):
    def test_prompt_and_accepted_answer(self):
        plan, ir, c, parts = plan_for("spc_capsule_protocol", LIB_ONLY)
        agent = FakeAgent(plan)
        got = partition.run(ir, c, parts, agent, None)
        self.assertEqual(got["chips"], plan["chips"])
        call = agent.calls[0]
        self.assertEqual(call["stage"], "partition")
        self.assertIsNone(call["key"])
        self.assertIn("One module per place the material sits", call["system"])
        for want in ["Protocol IR", "Constraints 'assayathon_demo'", "- culture_chamber [library", "pillar_filter: constraints allow library parts only",
                     "chip_plan.schema.json", '"kind":"chip_plan"']:
            self.assertIn(want, call["user"])

    def test_feedback_sends_previous_plan_and_errors(self):
        plan, ir, c, parts = plan_for("bead_cleanup_protocol", "unconstrained")
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "chip_plan.json").write_text(json.dumps(plan))
            agent = FakeAgent(plan)
            partition.run(ir, c, parts, agent, Path(tmp), feedback=["chip chip1_bead_cleanup: footprint 80 x 40 mm exceeds 30 x 20 mm"])
        call = agent.calls[0]
        self.assertTrue(call["key"].startswith("resplit_"))
        self.assertIn("RE-SPLIT REQUEST", call["user"])
        self.assertIn("footprint 80 x 40 mm exceeds", call["user"])
        self.assertIn("Previous plan:", call["user"])

    def test_part_source_filled_in(self):
        plan, ir, c, parts = plan_for("bead_cleanup_protocol", "unconstrained")
        for ch in plan["chips"]:
            for m in ch["modules"]:
                m.pop("part_source")
        got = partition.run(ir, c, parts, FakeAgent(plan), None)
        self.assertEqual(got["chips"][0]["modules"][0]["part_source"], "parametric")

    def test_extract_rules_output_partitions(self):
        from autochip import ingest
        from autochip.stages import extract
        src_path = ROOT / "autochip" / "examples" / "spc_capsule_protocol.md"
        c = C.load("unconstrained")
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ir = extract.run(ingest.load_source(str(src_path), None), c, Agent(backend="rules"), Path(tmp))
        except NotImplementedError:
            self.skipTest("extract stage not built yet")
        parts = P.load_parts(c)
        plan = partition.run(ir, c, parts, Agent(backend="rules"), None)
        self.assertEqual(partition.validate(plan, ir, c, parts), [])


class TestDesignFiles(unittest.TestCase):
    """Each chip's design file builds with chip/components/build_chip.py, run in a temp copy of the
    repo pieces it needs (it writes chip/<id>.json, cad/exports/<id>/ and cad/fusion/ under its own root)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        shutil.copytree(ROOT / "chip" / "components", root / "chip" / "components")
        (root / "operator").mkdir()
        shutil.copy(ROOT / "operator" / "schema_io.py", root / "operator" / "schema_io.py")
        shutil.copytree(ROOT / "contracts" / "schemas", root / "contracts" / "schemas")
        shutil.copytree(ROOT / "cad" / "library", root / "cad" / "library")
        (root / "designs").mkdir()
        cls.root = root

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def build(self, design: dict) -> subprocess.CompletedProcess:
        path = self.root / "designs" / f"{design['id']}.json"
        path.write_text(json.dumps(design, indent=1))
        return subprocess.run([sys.executable, str(self.root / "chip" / "components" / "build_chip.py"), str(path)],
                              capture_output=True, text=True, timeout=120)

    def test_library_only_plans_build(self):
        for name in EXAMPLES:
            plan, ir, c, parts = plan_for(name, LIB_ONLY)
            designs = partition.design_files(plan, ir)
            self.assertEqual(set(designs), {ch["id"] for ch in plan["chips"]})
            for cid, d in designs.items():
                self.assertFalse(d["needs_autochip_layout"], cid)
                r = self.build(d)
                self.assertEqual(r.returncode, 0, f"{name} {cid}: {r.stderr}")
                spec = json.loads((self.root / "chip" / f"{cid}.json").read_text())
                self.assertEqual([ch["id"] for ch in spec["chambers"]], [m["id"] for m in d["modules"]])
                self.assertTrue((self.root / "cad" / "exports" / cid / f"{cid}.stl").exists())

    def test_parametric_plans_build_with_stand_ins(self):
        plan, ir, c, parts = plan_for("spc_capsule_protocol", "unconstrained")
        designs = partition.design_files(plan, ir)
        d = designs[plan["chips"][0]["id"]]
        self.assertTrue(d["needs_autochip_layout"])
        culture = d["modules"][0]
        self.assertEqual((culture["part"], culture["autochip_part"]), ("culture_chamber", "culture_chamber_param"))
        self.assertIn("pillar_filter", " ".join(m["need"] for m in d["missing_parts"]))   # no library stand-in
        for cid, design in designs.items():
            r = self.build(design)
            self.assertEqual(r.returncode, 0, f"{cid}: {r.stderr}")

    def test_repo_left_clean(self):
        for name in EXAMPLES:
            plan, ir, c, parts = plan_for(name, "assayathon_demo")
            for cid in partition.design_files(plan, ir):
                self.assertFalse((ROOT / "chip" / f"{cid}.json").exists(), cid)
                self.assertFalse((ROOT / "cad" / "exports" / cid).exists(), cid)


if __name__ == "__main__":
    unittest.main()

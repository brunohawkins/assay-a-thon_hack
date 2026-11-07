"""The derived dye demo (tools/demo_plan.py) and docs/demo.md.

The point of these tests is that the demo document cannot go stale the way
cad/module1_dye_demo.md did. They check that every plan is physically runnable (real ports,
a watchable fill, a pressure under the assumed seal limit), that the one-pump story is
actually one pump, and that docs/demo.md is byte-for-byte what the generator produces.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "operator"))

import demo_plan as dp  # noqa: E402
import module1_operator as m1op  # noqa: E402

CHIPS = dp.demo_chips()
PLANS = [dp.plan(c) for c in CHIPS]
BY_ID = {p["chip_id"]: p for p in PLANS}
SPECS = {c["id"]: c for c in CHIPS}


class Coverage(unittest.TestCase):
    def test_every_chip_in_the_build_envelope_gets_a_plan(self):
        on_disk = {json.loads(p.read_text())["id"] for p in (ROOT / "chip").glob("*.json")
                   if str(json.loads(p.read_text()).get("scale") or "demo") == "demo"}
        self.assertEqual(on_disk, set(BY_ID))
        self.assertTrue(on_disk)

    def test_target_scale_chips_are_excluded(self):
        """A board that does not fit the 30 x 20 mm envelope cannot be on the bench."""
        target = {json.loads(p.read_text())["id"] for p in (ROOT / "chip").glob("*.json")
                  if json.loads(p.read_text()).get("scale") == "target"}
        self.assertTrue(target, "no target-scale chip left to check the filter with")
        self.assertFalse(target & set(BY_ID))

    def test_every_plan_has_a_route(self):
        for p in PLANS:
            with self.subTest(chip=p["chip_id"]):
                self.assertIsNotNone(p["flow_ul_min"], p.get("note"))


class Ports(unittest.TestCase):
    def test_every_plan_uses_only_ports_that_exist(self):
        for p in PLANS:
            ports = {q["id"] for q in SPECS[p["chip_id"]]["ports"]}
            named = {p["feed"], p["vent"], *p["plug"], *p["other_inlets"],
                     *(a["vent"] for a in p["alternatives"])}
            with self.subTest(chip=p["chip_id"]):
                self.assertTrue(named <= ports, f"not on the chip: {sorted(named - ports)}")

    def test_the_feed_is_an_inlet_and_the_vent_is_an_outlet(self):
        for p in PLANS:
            kinds = {q["id"]: q.get("type") for q in SPECS[p["chip_id"]]["ports"]}
            with self.subTest(chip=p["chip_id"]):
                self.assertEqual(kinds[p["feed"]], "inlet")
                self.assertEqual(kinds[p["vent"]], "outlet")
                for alt in p["alternatives"]:
                    self.assertEqual(kinds[alt["vent"]], "outlet")

    def test_one_pump_means_every_other_port_is_plugged(self):
        """Exactly one feed, exactly one vent, and no port left unaccounted for: an open
        port is a vent, which is the mistake the day-of risk list calls out."""
        for p in PLANS:
            ports = {q["id"] for q in SPECS[p["chip_id"]]["ports"]}
            with self.subTest(chip=p["chip_id"]):
                self.assertNotIn(p["feed"], p["plug"])
                self.assertNotIn(p["vent"], p["plug"])
                self.assertNotEqual(p["feed"], p["vent"])
                self.assertEqual(set(p["plug"]) | {p["feed"], p["vent"]}, ports)
                self.assertEqual(len(p["plug"]) + 2, len(ports))

    def test_every_part_in_the_sequence_is_on_the_chip(self):
        for p in PLANS:
            chip = SPECS[p["chip_id"]]
            known = ({c["id"] for c in chip["chambers"]} | {c["id"] for c in chip["channels"]}
                     | {q["id"] for q in chip["ports"]})
            for step in p["sequence"]:
                with self.subTest(chip=p["chip_id"], part=step["id"]):
                    self.assertIn(step["id"], known)

    def test_the_route_is_connected_feed_to_vent(self):
        """Each channel in the sequence joins the part before it to the part after it, so
        the chain in docs/demo.md is a path the liquid can actually take."""
        for p in PLANS:
            chip = SPECS[p["chip_id"]]
            ends = {c["id"]: {c["from"], c["to"]} for c in chip["channels"]}
            ids = [s["id"] for s in p["sequence"]]
            with self.subTest(chip=p["chip_id"]):
                self.assertEqual(ids[0], p["feed"])
                for i, step in enumerate(p["sequence"]):
                    if step["kind"] != "channel":
                        continue
                    nxt = ids[i + 1] if i + 1 < len(ids) else p["vent"]
                    self.assertEqual(ends[step["id"]], {ids[i - 1], nxt})


class FillTimes(unittest.TestCase):
    def test_every_fill_time_in_the_watch_list_is_inside_the_window(self):
        for p in PLANS:
            self.assertTrue(p["watch"], f"{p['chip_id']}: nothing to film")
            for w in p["watch"]:
                with self.subTest(chip=p["chip_id"], part=w["id"]):
                    self.assertGreaterEqual(w["fill_s"], dp.MIN_S)
                    self.assertLessEqual(w["fill_s"], dp.MAX_S)

    def test_the_headline_chamber_is_always_filmable(self):
        """The biggest chamber is the one a camera can resolve. Letting tiny junctions
        outvote it is what drove spc_demo_v1's flow to 0.078 uL/min."""
        for p in PLANS:
            with self.subTest(chip=p["chip_id"]):
                self.assertIn(p["headline"], [w["id"] for w in p["watch"]])

    def test_chambers_left_out_of_the_watch_list_really_are_outside_the_window(self):
        """Nothing is dropped for convenience: a chamber is unfilmable only if its own
        volume at the chosen flow puts it outside the window."""
        for p in PLANS:
            for step in p["sequence"]:
                if step["kind"] != "chamber" or step["filmable"]:
                    continue
                with self.subTest(chip=p["chip_id"], part=step["id"]):
                    self.assertTrue(step["fill_s"] < dp.MIN_S or step["fill_s"] > dp.MAX_S)

    def test_fill_times_come_from_the_specs_own_volumes_and_the_chosen_flow(self):
        for p in PLANS:
            volumes = {c["id"]: c["volume_ul"] for c in SPECS[p["chip_id"]]["chambers"]}
            for step in p["sequence"]:
                if step["kind"] != "chamber":
                    continue
                with self.subTest(chip=p["chip_id"], part=step["id"]):
                    self.assertAlmostEqual(step["volume_ul"], volumes[step["id"]], places=3)
                    self.assertAlmostEqual(step["fill_s"], 60 * volumes[step["id"]] / p["flow_ul_min"],
                                           delta=0.1)

    def test_the_sequence_clock_adds_up(self):
        for p in PLANS:
            running = 0.0
            for step in p["sequence"]:
                with self.subTest(chip=p["chip_id"], part=step["id"]):
                    self.assertAlmostEqual(step["starts_s"], running, delta=0.15)
                running = step["ends_s"]
            self.assertAlmostEqual(p["total_s"], running, delta=0.5)


class Pressure(unittest.TestCase):
    def test_every_predicted_pressure_is_under_the_demo_limit(self):
        for p in PLANS:
            with self.subTest(chip=p["chip_id"]):
                self.assertGreater(p["pressure_kpa"], 0)
                self.assertLess(p["pressure_kpa"], dp.PRESSURE_LIMIT_KPA)

    def test_pressure_is_flow_times_the_route_resistance(self):
        for p in PLANS:
            with self.subTest(chip=p["chip_id"]):
                self.assertAlmostEqual(p["pressure_kpa"],
                                       p["flow_ul_min"] * p["resistance_kpa_per_ul_min"], places=9)

    def test_the_demo_limit_is_declared_an_assumption_and_not_a_measurement(self):
        entry = dp._entry("demo.max_drive_pressure_kpa")
        self.assertTrue(entry["assumption"])
        self.assertEqual(entry["source"], ["Assumption"])
        self.assertIn("placeholder", entry["note"].lower())

    def test_the_demo_uses_water_at_20c_not_the_37c_medium(self):
        params = json.loads((ROOT / "contracts" / "parameters.json").read_text())["parameters"]
        self.assertEqual(dp.MU, params["fluid.viscosity_pa_s"]["value"])
        self.assertNotEqual(dp.MU, params["fluid.viscosity_37c_pa_s"]["value"])


class CellLimitsDoNotApply(unittest.TestCase):
    def test_the_dye_flows_are_free_of_the_adherent_cell_shear_ceiling(self):
        """The whole point: coloured water in an empty chip is not bounded by a limit that
        exists to keep cells attached. If any derived flow happened to land at or under the
        ceiling-derived culture feed, the demo would be unwatchable again."""
        culture_feed = 2.5     # operator/network.py DEFAULT_FEED_UL_MIN is about 2.4 uL/min
        self.assertTrue(any(p["flow_ul_min"] > culture_feed for p in PLANS))
        self.assertGreater(BY_ID["spc_demo_v1"]["flow_ul_min"], 1.0,
                           "spc_demo_v1's protocol runs its dye at 0.071 uL/min; the demo must not")


class SharedWithTheOperatorAndTwin(unittest.TestCase):
    def test_the_bench_flow_contract_matches_the_derivation(self):
        """contracts/parameters.json dye_demo.bench_flow_ul_min is what the twin and
        operator/module1_operator.py read. If the derivation moves, this fails rather than
        letting three places disagree."""
        self.assertEqual(dp._entry("dye_demo.bench_flow_ul_min")["value"],
                         BY_ID["module1_v1"]["flow_ul_min"])
        self.assertEqual(m1op.BENCH_FLOW_UL_MIN, BY_ID["module1_v1"]["flow_ul_min"])

    def test_the_port_hole_is_the_contract_bore_through_the_chips_own_lid(self):
        """The old literal was a 2 x 2 mm hole through 3 mm of lid (12 uL), a bore the chip
        no longer has. Both sides now derive it, and twin/contracts.js uses the same rule."""
        blocks = json.loads((ROOT / "contracts" / "blocks.json").read_text())
        bore = blocks["fabrication_floors"]["min_port_mm"]
        for p in PLANS:
            lid = SPECS[p["chip_id"]]["layers"][-1]["thickness_mm"]
            with self.subTest(chip=p["chip_id"]):
                self.assertAlmostEqual(p["port_hole_ul"], bore * bore * lid, places=4)
        self.assertAlmostEqual(m1op.PORT_HOLE_UL, bore * bore * 3, places=6)
        self.assertLess(m1op.PORT_HOLE_UL, 12.0)

    def test_the_python_operator_and_the_doc_agree_on_when_dye_reaches_j_mid(self):
        """Same port hole, same volumes, same flow: the two sides must land on the same
        second. They did not, and that is what started this."""
        import network as nw

        plan = BY_ID["module1_v1"]
        times = m1op.dye_times(nw.module1_network(),
                               *m1op.states_of("harvest", plan["flow_ul_min"]))["p_in"]
        reaches = next(s["starts_s"] for s in plan["sequence"] if s["id"] == "j_mid")
        self.assertAlmostEqual(times["j_mid"]["mean_s"], reaches, delta=0.2)


class CannotShow(unittest.TestCase):
    def test_every_chip_with_retention_says_the_demo_proves_nothing_about_it(self):
        for p in PLANS:
            retention = [c["id"] for c in SPECS[p["chip_id"]]["chambers"] if c.get("retention")]
            if not retention:
                continue
            joined = " ".join(p["cannot"])
            with self.subTest(chip=p["chip_id"]):
                self.assertIn("retention in", joined)
                for cid in retention:
                    self.assertIn(cid, joined)

    def test_module1_cannot_run_separate_growth_on_one_pump(self):
        self.assertTrue(any("j_mid" in c and "3 inlets" in c for c in BY_ID["module1_v1"]["cannot"]))

    def test_spc_demo_cannot_make_droplets(self):
        joined = " ".join(BY_ID["spc_demo_v1"]["cannot"])
        self.assertIn("two-phase", joined)
        self.assertIn("xj_1", joined)
        self.assertIn("no oil", joined)

    def test_module4_cannot_thermocycle(self):
        self.assertTrue(any("98" in c for c in BY_ID["module4_v1"]["cannot"]))

    def test_a_corner_is_not_reported_as_a_convergence(self):
        """spc_demo_v1's four oil corners and its oil_split are not places two streams are
        meant to meet; its nozzle and its aqueous merge are."""
        chip_g = dp.chip_graph(SPECS["spc_demo_v1"])
        nodes = {m["chamber"] for m in dp.multi_feed_nodes(chip_g)}
        self.assertEqual(nodes, {"xj_1", "aq_merge"})


class Document(unittest.TestCase):
    def test_docs_demo_md_is_exactly_the_generators_output(self):
        self.assertEqual((ROOT / "docs" / "demo.md").read_text(), dp.render_doc(PLANS),
                         "docs/demo.md is hand-edited or stale: run python tools/demo_plan.py --write")

    def test_the_check_flag_passes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(dp.main(["--check"]), 0)

    def test_the_document_says_it_is_generated(self):
        text = (ROOT / "docs" / "demo.md").read_text()
        self.assertIn("Generated file. Do not edit.", text)
        self.assertIn("tools/demo_plan.py --write", text)

    def test_the_complete_not_proven_list_is_there(self):
        text = (ROOT / "docs" / "demo.md").read_text().lower()
        for claim in ("no cell biology", "no thermal step", "no reagent compatibility",
                      "no weir capture", "no weeks-long sealing", "no dna adsorption",
                      "no droplets", "no single-cell barcoding", "no cfd validation"):
            with self.subTest(claim=claim):
                self.assertIn(claim, text)

    def test_the_day_of_risks_are_there_with_mitigations(self):
        text = (ROOT / "docs" / "demo.md")
        body = text.read_text()
        self.assertIn("## Day-of risks", body)
        for risk in ("0.8 mm port", "dead volume", "bubble", "plugged", "staining",
                     "Backlight", "too small to see", "spares"):
            with self.subTest(risk=risk):
                self.assertIn(risk, body)

    def test_the_old_procedure_points_at_the_new_one(self):
        old = (ROOT / "cad" / "module1_dye_demo.md").read_text()
        self.assertIn("docs/demo.md", old)
        self.assertIn("tools/demo_plan.py", old)

    def test_the_readme_links_the_demo_doc(self):
        self.assertIn("docs/demo.md", (ROOT / "README.md").read_text())

    def test_nothing_recommends_flipping_built_for_demo(self):
        """Only the owner knows what is on the print bed."""
        pipeline = json.loads((ROOT / "protocols" / "pipeline_mvp.json").read_text())
        self.assertTrue(all(m["built_for_demo"] is False for m in pipeline["modules"]))
        self.assertIn("built_for_demo", (ROOT / "docs" / "demo.md").read_text())


class Determinism(unittest.TestCase):
    def test_planning_twice_gives_the_same_answer(self):
        self.assertEqual([dp.plan(c) for c in dp.demo_chips()], PLANS)


if __name__ == "__main__":
    unittest.main()

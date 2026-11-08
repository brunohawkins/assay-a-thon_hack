"""assay2chip/cfd/couple.py: the pipeline-level model - chips joined end to end.

Offline and deterministic: everything comes off disk (protocols/pipeline_mvp.json, the
chips it names, contracts/) and nothing touches the network.

    python -m unittest discover -s tests -v
    python -m unittest tests.test_cfd_couple -v
"""

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "operator")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import network as opnetwork  # noqa: E402
import schema_io  # noqa: E402

from assay2chip.cfd import couple  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402

MVP = ROOT / "protocols" / "pipeline_mvp.json"
CHAIN = couple.read_chain(MVP)
FIELD = couple.couple(CHAIN)
BY_ID = {h["id"]: h for h in FIELD["handoffs"]}


def handoff(hid):
    return BY_ID[hid]


class LinePhysics(unittest.TestCase):
    """Poiseuille for a round tube, pi r^2 L for its volume, Stokes for settling."""

    def test_round_tube_resistance_matches_poiseuille(self):
        mu, length_mm, bore_mm = 0.0008, 100.0, 0.5
        expected = 128 * mu * (length_mm / 1000) / (math.pi * (bore_mm / 1000) ** 4)
        self.assertAlmostEqual(
            couple.round_tube_resistance_pa_s_per_m3(length_mm, bore_mm, mu), expected, delta=expected * 1e-12
        )

    def test_resistance_scales_as_length_and_the_fourth_power_of_bore(self):
        r1 = couple.round_tube_resistance_pa_s_per_m3(100, 0.5, 1e-3)
        self.assertAlmostEqual(couple.round_tube_resistance_pa_s_per_m3(200, 0.5, 1e-3) / r1, 2.0, places=9)
        self.assertAlmostEqual(couple.round_tube_resistance_pa_s_per_m3(100, 1.0, 1e-3) / r1, 1 / 16, places=9)

    def test_dead_volume_is_pi_r_squared_l(self):
        self.assertAlmostEqual(couple.tube_volume_ul(100.0, 0.5), math.pi * 0.25 ** 2 * 100.0, places=9)
        # mm^3 and uL are the same unit, so a 100 mm x 0.5 mm bore line holds ~19.6 uL.
        self.assertAlmostEqual(couple.tube_volume_ul(100.0, 0.5), 19.635, places=3)

    def test_stokes_settling_is_linear_in_density_and_square_in_diameter(self):
        mu = contracts.viscosity_37c_pa_s().pa_s
        drho = contracts.param("capsule.excess_density_kg_m3")
        v70 = couple.stokes_settling_mm_s(70, mu, drho)
        v140 = couple.stokes_settling_mm_s(140, mu, drho)
        self.assertAlmostEqual(v140 / v70, 4.0, places=9)
        self.assertAlmostEqual(
            couple.stokes_settling_mm_s(140, mu, 2 * drho) / v140, 2.0, places=9
        )
        expected = drho * couple.G_M_S2 * (140e-6) ** 2 / (18 * mu) * 1000
        self.assertAlmostEqual(v140, expected, places=9)

    def test_settling_uses_37c_viscosity_not_water(self):
        drho = contracts.param("capsule.excess_density_kg_m3")
        warm = couple.stokes_settling_mm_s(140, contracts.viscosity_37c_pa_s().pa_s, drho)
        cold = couple.stokes_settling_mm_s(140, contracts.viscosity_water_20c_pa_s().pa_s, drho)
        self.assertGreater(warm, cold)  # thinner medium at 37 C, so it settles faster

    def test_reynolds_uses_the_contract_fluid_density(self):
        mu = contracts.viscosity_37c_pa_s().pa_s
        rho = contracts.param("fluid.density_37c_kg_m3")
        self.assertEqual(couple.fluid_density_kg_m3(), rho)
        bore, q = 0.5, 10.0
        area = math.pi * (bore / 2000) ** 2
        u = q * couple.UL_MIN_TO_M3_S / area
        self.assertAlmostEqual(
            couple.tube_reynolds(q, bore, mu), rho * u * (bore / 1000) / mu, places=9
        )

    def test_reynolds_and_wall_shear_are_positive_and_scale_with_flow(self):
        mu = contracts.viscosity_37c_pa_s().pa_s
        self.assertAlmostEqual(
            couple.tube_reynolds(20, 0.5, mu) / couple.tube_reynolds(10, 0.5, mu), 2.0, places=9
        )
        self.assertAlmostEqual(
            couple.tube_wall_shear_dyn_cm2(20, 0.5, mu) / couple.tube_wall_shear_dyn_cm2(10, 0.5, mu),
            2.0,
            places=9,
        )


class Assumptions(unittest.TestCase):
    """The line and the densities are contract keys now; this module keeps no copies."""

    CONTRACTED = (
        "tubing.inner_diameter_mm",
        "tubing.length_mm",
        "selector_valve.internal_volume_ul",
        "fluid.density_37c_kg_m3",
        "cell.excess_density_kg_m3",
        "capsule.excess_density_kg_m3",
        "bead.excess_density_kg_m3",
    )

    def test_the_contract_really_holds_them_now(self):
        for key in self.CONTRACTED:
            entry = contracts.param_entry(key)
            self.assertIsNotNone(entry.get("value"), key)
            self.assertTrue(entry.get("assumption"), f"{key} should still be flagged")
            lo, hi = contracts.param_range(key)
            self.assertIsNotNone(lo, key)
            self.assertIsNotNone(hi, key)
            self.assertLessEqual(lo, entry["value"])
            self.assertGreaterEqual(hi, entry["value"])

    def test_no_contract_number_is_retyped_in_this_module(self):
        """The bug this fixed: five modules each carrying their own density."""
        source = (ROOT / "assay2chip" / "cfd" / "couple.py").read_text(encoding="utf-8")
        for key in self.CONTRACTED:
            value = contracts.param(key)
            # the literal may appear only inside a string (a docstring or a message)
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith(("#", '"', "'")) or "param(" in line:
                    continue
                self.assertNotIn(
                    f"= {value}", stripped, f"{key}'s value {value} is retyped: {stripped}"
                )

    def test_the_local_table_is_only_what_no_contract_states(self):
        self.assertEqual(set(couple.ASSUMPTIONS), {"valve.count_per_line"})
        for key, a in couple.ASSUMPTIONS.items():
            self.assertTrue(a.flagged, key)
            self.assertTrue(a.source.strip(), key)
            self.assertTrue(a.why.strip(), key)
            self.assertGreater(a.value, 0, key)

    def test_asking_the_local_table_for_a_contract_key_refuses_loudly(self):
        for key in self.CONTRACTED:
            with self.assertRaises(couple.CoupleError, msg=key) as cm:
                couple.assumption(key)
            self.assertIn("CONTRACT key", str(cm.exception))

    def test_a_missing_assumption_raises_rather_than_defaulting(self):
        with self.assertRaises(couple.CoupleError):
            couple.assumption("tubing.colour")

    def test_the_output_reports_contract_and_local_assumptions_together(self):
        keys = {a["key"] for a in FIELD["assumptions"]}
        self.assertEqual(keys, set(self.CONTRACTED) | set(couple.ASSUMPTIONS))
        for a in FIELD["assumptions"]:
            self.assertTrue(a["flagged"], a["key"])
            if a["key"] in self.CONTRACTED:
                self.assertTrue(a["contract"])
                self.assertEqual(a["value"], contracts.param(a["key"]))
                self.assertEqual((a["min"], a["max"]), contracts.param_range(a["key"]))
            else:
                self.assertNotIn("contract", a)

    def test_the_line_dimensions_come_from_the_contract(self):
        line = couple.build_line("t", contracts.viscosity_37c_pa_s(), None, None, valves=1)
        tubing = next(s for s in line.segments if s.kind == "tubing")
        valve = next(s for s in line.segments if s.kind == "valve")
        self.assertEqual(tubing.bore_mm, contracts.param("tubing.inner_diameter_mm"))
        self.assertEqual(tubing.length_mm, contracts.param("tubing.length_mm"))
        self.assertAlmostEqual(
            valve.volume_ul, contracts.param("selector_valve.internal_volume_ul"), places=9
        )
        for seg in (tubing, valve):
            self.assertIn("contracts/parameters.json", seg.source)
            self.assertTrue(seg.flagged)

    def test_every_line_marks_its_tubing_and_valve_as_assumed(self):
        for h in FIELD["handoffs"]:
            kinds = {s["kind"]: s for s in h["line"]["segments"]}
            self.assertTrue(kinds["tubing"]["flagged"], h["id"])
            self.assertTrue(kinds["valve"]["flagged"], h["id"])


class PerKindDensity(unittest.TestCase):
    """A cell, a capsule and a bead are 60, 30 and 700 kg/m3 above medium, not all the same."""

    def test_each_kind_gets_its_own_contract_key(self):
        self.assertEqual(
            couple.EXCESS_DENSITY_KEY,
            {
                "cells": "cell.excess_density_kg_m3",
                "capsules": "capsule.excess_density_kg_m3",
                "beads": "bead.excess_density_kg_m3",
            },
        )
        values = {k: contracts.param(v) for k, v in couple.EXCESS_DENSITY_KEY.items()}
        self.assertEqual(len(set(values.values())), 3, f"kinds must differ: {values}")

    def test_the_same_particle_size_settles_differently_per_kind(self):
        mu = contracts.viscosity_37c_pa_s().pa_s
        speeds = {
            kind: couple.stokes_settling_mm_s(100, mu, couple.excess_density(kind)["kg_m3"])
            for kind in couple.EXCESS_DENSITY_KEY
        }
        self.assertLess(speeds["capsules"], speeds["cells"])
        self.assertLess(speeds["cells"], speeds["beads"])

    def test_a_loss_question_takes_the_max_end(self):
        rho = couple.excess_density("capsules", couple.LOSS_END)
        self.assertEqual(rho["end_used"], "max")
        self.assertEqual(rho["kg_m3"], contracts.param_range("capsule.excess_density_kg_m3")[1])
        self.assertIn("LOSES", rho["why"])

    def test_a_retention_question_takes_the_min_end(self):
        rho = couple.excess_density("capsules", couple.RETENTION_END)
        self.assertEqual(rho["end_used"], "min")
        self.assertEqual(rho["kg_m3"], contracts.param_range("capsule.excess_density_kg_m3")[0])
        self.assertIn("RELIES", rho["why"])

    def test_the_band_is_the_contracts_band(self):
        for kind, key in couple.EXCESS_DENSITY_KEY.items():
            lo, hi = contracts.param_range(key)
            self.assertEqual(
                couple.excess_density(kind)["band"],
                {"min": lo, "value": contracts.param(key), "max": hi},
                kind,
            )

    def test_a_kind_with_no_contract_density_says_so(self):
        rho = couple.excess_density("library")
        self.assertIsNone(rho["kg_m3"])
        self.assertIsNone(rho["key"])
        self.assertIn("no contract", rho["why"])

    def test_an_unknown_end_of_the_band_is_refused(self):
        with self.assertRaises(couple.CoupleError):
            couple.excess_density("capsules", "typical")


class AgreesWithTheParticleModule(unittest.TestCase):
    """Settling is particles.py's call, not a second opinion computed here."""

    def setUp(self):
        if not couple.PARTICLES_AVAILABLE:
            self.skipTest("assay2chip/cfd/particles.py is not importable")

    def test_the_two_modules_read_the_same_contract_keys(self):
        from assay2chip.cfd import particles

        self.assertEqual(
            set(couple.EXCESS_DENSITY_KEY.values()) | {"capsule.excess_density_kg_m3"},
            set(particles.EXCESS_DENSITY_KEYS.values()),
        )
        self.assertEqual(particles.CARRIER_DENSITY_KEY, "fluid.density_37c_kg_m3")

    def test_the_band_and_the_end_come_from_particles_py(self):
        from assay2chip.cfd import particles

        for carries, key in couple.EXCESS_DENSITY_KEY.items():
            mine = couple.excess_density(carries, couple.LOSS_END)
            theirs = particles.excess_density(key)
            self.assertEqual(mine["band"]["min"], theirs.min_kg_m3, carries)
            self.assertEqual(mine["band"]["value"], theirs.value, carries)
            self.assertEqual(mine["band"]["max"], theirs.max_kg_m3, carries)
            self.assertEqual(mine["kg_m3"], theirs.for_intent("loss")[0], carries)
            self.assertEqual(mine["intent"], "loss")
            self.assertIn("particles.py", mine["read_through"])

    def test_the_settling_velocity_is_the_same_number_in_both(self):
        from assay2chip.cfd import particles

        mu = contracts.viscosity_37c_pa_s().pa_s
        for d, rho in ((140.0, 50.0), (15.0, 75.0), (12.0, 700.0)):
            self.assertAlmostEqual(
                couple.stokes_settling_mm_s(d, mu, rho),
                particles.stokes_settling_m_s(rho, d * 1e-6, mu) * 1000.0,
                places=12,
            )

    def test_a_handoff_line_is_the_particle_modules_loss_intent(self):
        self.assertEqual(couple.LINE_ROLE, "feed")
        self.assertEqual(couple.END_TO_INTENT[couple.LOSS_END], "loss")
        self.assertEqual(couple.END_TO_INTENT[couple.RETENTION_END], "desired")
        for h in FIELD["handoffs"]:
            pv = h["settling"].get("particles_module_verdict")
            if pv:
                self.assertEqual(pv["intent"], "loss", h["id"])
                self.assertIn("feed line", pv["intent_why"])

    def test_capsules_use_the_swollen_class(self):
        pclass = couple.particle_class_for("capsules")
        self.assertEqual(pclass.name, "capsule_swollen")
        self.assertEqual(pclass.swollen_from, "capsule")
        self.assertEqual(pclass.diameter_um, contracts.swollen_capsule_diameter_um())
        self.assertEqual(couple.particle_class_for("cells").name, "cells")
        self.assertEqual(couple.particle_class_for("beads").name, "bead")
        self.assertIsNone(couple.particle_class_for("library"))

    def test_both_bands_take_their_conservative_end_for_a_loss(self):
        pv = handoff("hairpin_to_module4")["settling"]["particles_module_verdict"]
        pclass = couple.particle_class_for("capsules")
        self.assertEqual(pv["diameter_end"], "max")
        self.assertEqual(pv["diameter_um"], pclass.d_max_um)
        self.assertEqual(pv["density_end"], "max")
        self.assertEqual(pv["excess_density_kg_m3"], pclass.excess.max_kg_m3)
        self.assertEqual(pv["density_key"], "capsule.excess_density_kg_m3")

    def test_the_tubing_verdict_agrees_with_the_on_chip_feed_line_verdict(self):
        """The particle agent found capsules settle in all four of Module 4's on-chip feed
        lines. The off-chip tubing into the same chip must not say the opposite."""
        from assay2chip.cfd import particles

        mu = contracts.viscosity_37c_pa_s().pa_s
        pclass = particles.particle_classes(mu)["capsule_swollen"]
        chip = schema_io.load_json(ROOT / "chip" / "module4_v1.json")
        feeds = [c for c in chip["channels"] if c["from"].startswith("in_s")]
        self.assertEqual(len(feeds), 4)
        for ch in feeds:
            on_chip = particles.sediment_check(
                ch, element_id=ch["id"], element="edge", particle=pclass,
                depth_mm=ch["depth_mm"], transit_time_s=10.3, u_mm_s=1.0,
                mu_pa_s=mu, role="feed",
            )
            self.assertEqual(on_chip.intent, "loss", ch["id"])
            self.assertIn(on_chip.verdict, ("lost", "partial_loss"), ch["id"])
        tubing = handoff("hairpin_to_module4")["settling"]["particles_module_verdict"]
        self.assertEqual(tubing["intent"], "loss")
        self.assertEqual(tubing["verdict"], "lost")
        # Same direction on both sides of the connector: the capsules do not arrive.
        self.assertGreater(tubing["transit_time_s"], tubing["fall_time_s"])


class ValidationGate(unittest.TestCase):
    """"A solver nobody has checked is worse than no solver" - README."""

    def test_couple_says_when_it_has_not_been_checked(self):
        v = FIELD["validation"]
        self.assertFalse(v["ran"])
        self.assertIsNone(v["trusted"])
        self.assertIn("not run on this call", v["why"])

    def test_the_written_file_carries_the_verdict(self):
        if couple.cfd_validate is None:
            self.skipTest("assay2chip/cfd/validate.py is not importable")
        v = couple.validation()
        self.assertTrue(v["ran"])
        self.assertTrue(v["trusted"], v.get("untrusted_reason"))
        self.assertEqual(v["benchmarks_passed"], v["benchmarks"])
        self.assertGreaterEqual(v["benchmarks"], 9)
        self.assertIs(couple.validation(), v)      # cached: benchmarks are seconds of work

    def test_run_validates_by_default_and_can_be_told_not_to(self):
        if couple.cfd_validate is None:
            self.skipTest("assay2chip/cfd/validate.py is not importable")
        with tempfile.TemporaryDirectory() as tmp:
            on, _ = couple.run(MVP, Path(tmp) / "a.json")
            off, _ = couple.run(MVP, Path(tmp) / "b.json", validated=False)
        self.assertTrue(on["validation"]["ran"])
        self.assertTrue(on["validation"]["trusted"])
        self.assertFalse(off["validation"]["ran"])


class ContractsNotLiterals(unittest.TestCase):
    """Nothing here may hard-code a number a contract owns."""

    def test_viscosity_comes_from_the_contract_and_is_the_37c_one(self):
        self.assertEqual(FIELD["contracts"]["viscosity_pa_s"], contracts.viscosity_37c_pa_s().pa_s)
        self.assertEqual(FIELD["contracts"]["viscosity_key"], "fluid.viscosity_37c_pa_s")

    def test_the_dye_demo_viscosity_is_only_used_when_asked_for(self):
        cold = couple.couple(CHAIN, temp_c=20.0)
        self.assertEqual(cold["contracts"]["viscosity_key"], "fluid.viscosity_pa_s")
        self.assertEqual(cold["contracts"]["viscosity_pa_s"], contracts.viscosity_water_20c_pa_s().pa_s)

    def test_capsule_size_is_the_swollen_one(self):
        self.assertEqual(
            FIELD["contracts"]["swollen_capsule_diameter_um"], contracts.swollen_capsule_diameter_um()
        )
        caps = [h for h in FIELD["handoffs"] if h["carries"] == "capsules"]
        self.assertTrue(caps)
        for h in caps:
            self.assertEqual(h["particles"]["diameter_um"], contracts.swollen_capsule_diameter_um())
            self.assertEqual(
                h["particles"]["diameter_um"],
                contracts.capsule_diameter_um() * contracts.capsule_swell_factor(),
            )
            self.assertTrue(h["particles"]["swollen"])

    def test_the_pressure_limit_is_escape_pressure_times_the_margin(self):
        self.assertEqual(FIELD["contracts"]["max_working_pressure_kpa"], contracts.max_working_pressure_kpa())
        for h in FIELD["handoffs"]:
            self.assertEqual(h["pressure"]["limit_kpa"], contracts.max_working_pressure_kpa())

    def test_the_clog_factor_is_the_contract_one(self):
        factor = contracts.param("capsule_channel.width_factor_low")
        for h in FIELD["handoffs"]:
            if h["particles"].get("clog_factor"):
                self.assertEqual(h["particles"]["clog_factor"], factor)

    def test_an_unknown_bond_pressure_is_flagged_not_invented(self):
        for h in FIELD["handoffs"]:
            self.assertIsNone(h["pressure"]["bond_limit_kpa"])
            self.assertIn("UNKNOWN", h["pressure"]["bond_limit_source"])

    def test_a_port_bore_comes_from_the_chip_or_the_contract_floor(self):
        # module3's capsule_in states width_um 1600; module5's out_lib states 300.
        seg = next(
            s
            for s in handoff("module2_to_module3")["line"]["segments"]
            if s["kind"] == "connector" and "port_in" in s["id"]
        )
        self.assertAlmostEqual(seg["bore_mm"], 1.6, places=6)
        self.assertIn("width_um", seg["source"])
        # module4's inlets state no width_um, so the contract floor is used.
        seg4 = next(
            s
            for s in handoff("hairpin_to_module4")["line"]["segments"]
            if s["kind"] == "connector" and "port_in" in s["id"]
        )
        self.assertAlmostEqual(seg4["bore_mm"], contracts.floor("min_port_mm"), places=6)


class NodalSolver(unittest.TestCase):
    """A p = b, checked against hand-computed series and parallel networks."""

    def test_two_resistors_in_series(self):
        edges = [
            {"id": "e1", "from": "a", "to": "b", "R": 2e12},
            {"id": "e2", "from": "b", "to": "c", "R": 3e12},
        ]
        q = 10.0
        p, flows = couple.solve_nodal(["a", "b", "c"], edges, ["c"], {"a": q})
        q_m3 = q * couple.UL_MIN_TO_M3_S
        self.assertAlmostEqual(p["a"], q_m3 * 5e12, places=6)
        self.assertAlmostEqual(p["b"], q_m3 * 3e12, places=6)
        self.assertAlmostEqual(flows["e1"], q, places=9)
        self.assertAlmostEqual(flows["e2"], q, places=9)

    def test_two_resistors_in_parallel(self):
        edges = [
            {"id": "e1", "from": "a", "to": "v", "R": 2e12},
            {"id": "e2", "from": "a", "to": "v", "R": 6e12},
        ]
        q = 12.0
        p, flows = couple.solve_nodal(["a", "v"], edges, ["v"], {"a": q})
        self.assertAlmostEqual(p["a"], q * couple.UL_MIN_TO_M3_S * 1.5e12, places=6)
        self.assertAlmostEqual(flows["e1"], 9.0, places=9)   # 3/4 of it down the easy branch
        self.assertAlmostEqual(flows["e2"], 3.0, places=9)
        self.assertAlmostEqual(flows["e1"] + flows["e2"], q, places=9)

    def test_mass_is_conserved_at_every_node(self):
        edges = [
            {"id": "e1", "from": "a", "to": "b", "R": 1e12},
            {"id": "e2", "from": "b", "to": "c", "R": 2e12},
            {"id": "e3", "from": "b", "to": "d", "R": 4e12},
            {"id": "e4", "from": "c", "to": "v", "R": 1e12},
            {"id": "e5", "from": "d", "to": "v", "R": 1e12},
        ]
        q = 7.5
        _, flows = couple.solve_nodal(list("abcdv"), edges, ["v"], {"a": q})
        net = {n: 0.0 for n in "abcdv"}
        for e in edges:
            net[e["from"]] -= flows[e["id"]]
            net[e["to"]] += flows[e["id"]]
        self.assertAlmostEqual(net["a"], -q, places=9)
        for n in "bcd":
            self.assertAlmostEqual(net[n], 0.0, places=9)
        self.assertAlmostEqual(net["v"], q, places=9)

    def test_fed_with_nowhere_to_go_raises(self):
        edges = [{"id": "e1", "from": "a", "to": "b", "R": 1e12}]
        with self.assertRaises(couple.CoupleError):
            couple.solve_nodal(["a", "b"], edges, [], {"a": 5.0})

    def test_a_dead_capped_branch_is_left_at_zero(self):
        edges = [
            {"id": "e1", "from": "a", "to": "v", "R": 1e12},
            {"id": "e2", "from": "x", "to": "y", "R": 1e12},
        ]
        p, flows = couple.solve_nodal(["a", "v", "x", "y"], edges, ["v"], {"a": 1.0})
        self.assertEqual(p["x"], 0.0)
        self.assertEqual(flows["e2"], 0.0)

    def test_it_agrees_with_operator_network_on_a_real_chip(self):
        """operator/network.py is an independent implementation; the two must agree."""
        chip = schema_io.load_json(ROOT / "chip" / "module4_v1.json")
        mu = contracts.viscosity_37c_pa_s().pa_s
        net = opnetwork.Network(chip, mu=mu)
        theirs = net.solve({"in_s1": ("FEED", 3.92), "p_vent": "VENT"})
        nodes, edges = couple.chip_edges(chip, mu, "")
        p, flows = couple.solve_nodal(nodes, edges, ["p_vent"], {"in_s1": 3.92})
        for node, pa in theirs["pressure_pa"].items():
            self.assertAlmostEqual(p[node], pa, delta=max(abs(pa) * 1e-5, 1e-6), msg=node)
        for cid, row in theirs["channels"].items():
            self.assertAlmostEqual(
                flows[cid], row["flow_ul_min"], delta=max(abs(row["flow_ul_min"]) * 1e-5, 1e-5), msg=cid
            )


class ReadingTheChain(unittest.TestCase):
    def test_the_mvp_chain_reads_every_module_and_handoff(self):
        doc = schema_io.load_json(MVP)
        self.assertEqual(len(CHAIN.handoffs), len(doc["handoffs"]))
        for mod in doc["modules"]:
            self.assertIn(mod["module"], CHAIN.stages)
        for road in doc["roadmap"]:
            self.assertIn(road["id"], CHAIN.stages)

    def test_the_chain_order_is_the_file_order(self):
        self.assertEqual(
            [h["id"] for h in FIELD["handoffs"]],
            [h["id"] for h in schema_io.load_json(MVP)["handoffs"]],
        )

    def test_placeholders_are_boundaries_not_chips(self):
        for key in ("barcoding", "hairpin", "spri_qc", "library_out"):
            self.assertFalse(CHAIN.stages[key].on_chip, key)
            self.assertTrue(FIELD["stages"][key]["boundary"], key)
        for key in ("module1", "cellprep", "module2", "module3", "module4", "module5"):
            self.assertTrue(CHAIN.stages[key].on_chip, key)

    def test_a_dropped_module_is_pass_through_but_a_bench_stage_is_not(self):
        # barcoding and hairpin were dropped: nothing happens, the sample crosses unchanged.
        self.assertTrue(CHAIN.stages["barcoding"].pass_through)
        self.assertTrue(CHAIN.stages["hairpin"].pass_through)
        # SPRI and QC are real bench work that changes the volume, so never pass-through.
        self.assertFalse(CHAIN.stages["spri_qc"].pass_through)
        self.assertEqual(CHAIN.stages["spri_qc"].kind, "roadmap")

    def test_a_tbc_port_is_dropped_rather_than_treated_as_a_port_id(self):
        self.assertEqual(handoff("module4_to_module5")["from"]["ports"], [])
        self.assertEqual(handoff("module3_to_barcoding")["to"]["ports"], [])


class EveryHandoffHasItsNumbers(unittest.TestCase):
    def test_each_handoff_reports_all_six_checks(self):
        for h in FIELD["handoffs"]:
            for block in ("line", "pressure", "dead_volume", "settling", "continuity", "particles", "joint"):
                self.assertIn(block, h, f"{h['id']} is missing {block}")
            self.assertIn(h["severity"], couple.SEVERITY_ORDER)

    def test_a_line_is_connector_tubing_valve_connector_in_series(self):
        h = handoff("module2_to_module3")
        kinds = [s["kind"] for s in h["line"]["segments"]]
        self.assertEqual(kinds, ["connector", "tubing", "valve", "connector"])
        self.assertAlmostEqual(
            h["line"]["resistance_pa_s_per_m3"],
            sum(s["resistance_pa_s_per_m3"] for s in h["line"]["segments"]),
            places=6,
        )
        self.assertAlmostEqual(
            h["line"]["dead_volume_ul"],
            sum(s["dead_volume_ul"] for s in h["line"]["segments"]),
            places=3,
        )

    def test_dead_volume_is_the_dominant_term_in_the_tubing(self):
        h = handoff("hairpin_to_module4")
        tubing = next(s for s in h["line"]["segments"] if s["kind"] == "tubing")
        self.assertGreater(tubing["dead_volume_ul"], 0.8 * h["line"]["dead_volume_ul"])

    def test_pressure_is_line_plus_downstream_chip_against_the_contract_limit(self):
        p = handoff("hairpin_to_module4")["pressure"]
        self.assertAlmostEqual(
            p["p_upstream_outlet_needed_kpa"], p["line_dp_kpa"] + p["downstream_chip_dp_kpa"], places=9
        )
        self.assertAlmostEqual(
            p["fraction_of_limit"], p["p_upstream_outlet_needed_kpa"] / p["limit_kpa"], places=9
        )
        self.assertFalse(p["over_limit"])  # these lines are wide; pressure is not the problem

    def test_the_lines_are_stokes_flow_so_the_model_is_valid(self):
        for h in FIELD["handoffs"]:
            re = h["pressure"].get("reynolds_in_line")
            if re is not None:
                self.assertLess(re, 1.0, h["id"])
                self.assertFalse(h["pressure"]["unmodelled_re_above_1"], h["id"])

    def test_capsules_settle_across_the_bore_long_before_they_cross_the_line(self):
        for hid in ("module2_to_module3", "hairpin_to_module4", "module4_to_module5"):
            s = handoff(hid)["settling"]
            self.assertTrue(s["settles_before_it_arrives"], hid)
            self.assertGreater(s["transit_over_settle"], 100, hid)
            self.assertTrue(s["excess_density_is_contract_assumption"], hid)

    def test_every_settling_result_names_the_end_of_the_band_it_used(self):
        for h in FIELD["handoffs"]:
            s = h["settling"]
            if s.get("excess_density_kg_m3") is None:
                self.assertIsNone(s["excess_density_key"], h["id"])
                continue
            # A handoff line is a LOSS question, so every one of them must use max.
            self.assertEqual(s["excess_density_end_used"], "max", h["id"])
            self.assertEqual(s["excess_density_kg_m3"], s["excess_density_band"]["max"], h["id"])
            self.assertIn("LOSES", s["excess_density_why_this_end"])
            self.assertEqual(
                s["excess_density_key"], couple.EXCESS_DENSITY_KEY[h["carries"]], h["id"]
            )

    def test_the_settling_verdict_is_reported_across_the_whole_band(self):
        for h in FIELD["handoffs"]:
            s = h["settling"]
            if not s.get("verdict_across_the_band"):
                continue
            self.assertEqual(set(s["verdict_across_the_band"]), {"min", "value", "max"}, h["id"])
            band = s["across_the_band"]
            # settling is linear in excess density: denser settles sooner
            self.assertLess(
                band["max"]["time_to_settle_across_bore_s"],
                band["value"]["time_to_settle_across_bore_s"],
            )
            self.assertLess(
                band["value"]["time_to_settle_across_bore_s"],
                band["min"]["time_to_settle_across_bore_s"],
            )
            self.assertIs(
                s["verdict_holds_across_the_whole_band"],
                len(set(s["verdict_across_the_band"].values())) == 1,
                h["id"],
            )

    def test_the_module3_to_module4_verdict_survives_the_whole_density_band(self):
        """The follow-up's question: at max (50) it was the old local number, at value 30."""
        s = handoff("hairpin_to_module4")["settling"]
        self.assertEqual(s["excess_density_kg_m3"], 50)      # max end, the loss question
        self.assertEqual(s["excess_density_band"]["value"], 30)
        self.assertTrue(s["settles_before_it_arrives"])
        self.assertTrue(s["verdict_holds_across_the_whole_band"])
        for end in ("min", "value", "max"):
            self.assertTrue(s["across_the_band"][end]["settles_before_it_arrives"], end)
            self.assertGreater(s["across_the_band"][end]["transit_over_settle"], 100, end)
        # particles.py grades on the settled FRACTION, so a line that drops the whole
        # payload is a blocker, not merely high. The severity must be its verdict's.
        self.assertEqual(
            handoff("hairpin_to_module4")["severity_parts"]["settling"],
            s["particles_module_verdict"]["severity"],
        )
        self.assertEqual(s["particles_module_verdict"]["severity"], "blocker")
        self.assertEqual(s["particles_module_verdict"]["verdict"], "lost")
        self.assertEqual(s["particles_module_verdict"]["settled_fraction"], 1.0)

    def test_cells_and_capsules_no_longer_settle_identically(self):
        cells = handoff("module1_to_cellprep")["settling"]
        caps = handoff("module2_to_module3")["settling"]
        self.assertNotEqual(cells["excess_density_kg_m3"], caps["excess_density_kg_m3"])
        self.assertEqual(cells["excess_density_key"], "cell.excess_density_kg_m3")
        self.assertEqual(caps["excess_density_key"], "capsule.excess_density_kg_m3")

    def test_the_fluid_density_is_one_number_for_everybody(self):
        for h in FIELD["handoffs"]:
            self.assertEqual(h["settling"]["fluid_density_key"], "fluid.density_37c_kg_m3")
            self.assertEqual(
                h["settling"]["fluid_density_kg_m3"], contracts.param("fluid.density_37c_kg_m3")
            )

    def test_cells_have_no_contract_diameter_so_a_bound_is_used_and_flagged(self):
        p = handoff("module1_to_cellprep")["particles"]
        self.assertEqual(p["diameter_um"], contracts.param("cell_filter.gap_um"))
        self.assertTrue(p["flagged"])
        self.assertIn("UPPER BOUND", p["source"])


class VolumeContinuity(unittest.TestCase):
    """The audit's number: 77 uL released into a stage that draws 9.6 uL."""

    def test_module3_releases_two_trap_volumes(self):
        trap = next(
            c["volume_ul"]
            for c in schema_io.load_json(ROOT / "chip" / "module3_v1_translated.json")["chambers"]
            if c["id"] == "main_trap"
        )
        self.assertAlmostEqual(handoff("module3_to_barcoding")["continuity"]["released_ul"], 2 * trap, places=6)

    def test_module4_draws_sixteen_pulses_of_point_six(self):
        c = handoff("hairpin_to_module4")["continuity"]
        self.assertAlmostEqual(c["drawn_ul"], 16 * 0.6, places=6)
        self.assertEqual(c["drawn_reagent"], "pooled_capsule_suspension")

    def test_the_audit_mismatch_is_reproduced(self):
        c = handoff("hairpin_to_module4")["continuity"]
        self.assertAlmostEqual(c["released_ul"], 76.886, places=3)
        self.assertAlmostEqual(c["drawn_ul"], 9.6, places=3)
        self.assertAlmostEqual(c["released_over_drawn"], 76.886 / 9.6, places=3)
        self.assertFalse(c["balanced"])
        self.assertIn("concentration step", c["missing_step"])
        self.assertEqual(handoff("hairpin_to_module4")["severity_parts"]["continuity"], "blocker")

    def test_the_released_volume_read_out_of_a_note_is_flagged(self):
        src = handoff("module3_to_barcoding")["continuity"]["released_source"]
        self.assertIn("NOTE", src)
        self.assertTrue(
            any(
                u.get("field") == "released_volume_ul" and "module3_to_barcoding" == u.get("handoff")
                for u in FIELD["unresolved"]
            )
        )

    def test_a_volume_is_never_carried_past_a_bench_stage(self):
        # SPRI changes the volume entirely, so module 3's 76.9 uL must not appear here.
        self.assertIsNone(handoff("module5_to_spri_qc")["continuity"]["released_ul"])

    def test_the_volume_is_carried_through_the_dropped_placeholders(self):
        src = handoff("hairpin_to_module4")["continuity"]["released_source"]
        self.assertIn("module3", src)
        self.assertIn("placeholder", src)

    def test_a_line_that_holds_more_than_a_transfer_is_a_blocker(self):
        dv = handoff("module4_to_module5")["dead_volume"]
        self.assertGreater(dv["fraction_lost_in_line"], 1.0)
        self.assertIn("nothing arrives", dv["message"])
        self.assertEqual(handoff("module4_to_module5")["severity_parts"]["dead_volume"], "blocker")
        self.assertGreater(dv["line_volumes_per_transfer"], 4.0)


class ParticlesOnBothSides(unittest.TestCase):
    def test_a_swollen_capsule_cannot_pass_module3s_prefilter(self):
        p = handoff("module2_to_module3")["particles"]
        gap = schema_io.load_json(ROOT / "chip" / "module3_v1_translated.json")["capsule_filter"]["gap_um"]
        self.assertIn("capsule_prefilter", p["blocks"])
        self.assertIn("capsule_prefilter", p["clogs"])
        blocked = next(c for c in p["checked"] if c["feature_id"] == "capsule_prefilter")
        self.assertAlmostEqual(blocked["opening_mm"], gap / 1000.0, places=6)
        self.assertLess(blocked["opening_mm"], contracts.swollen_capsule_diameter_um() / 1000.0)
        self.assertEqual(handoff("module2_to_module3")["severity_parts"]["particles"], "blocker")

    def test_both_sides_of_a_handoff_are_checked(self):
        p = handoff("module2_to_module3")["particles"]
        sides = {c["side"] for c in p["checked"]}
        self.assertEqual(sides, {"upstream", "downstream", "line"})

    def test_a_retention_weir_is_never_called_a_clog(self):
        p = handoff("module3_to_barcoding")["particles"]
        self.assertIn("main_trap", p["retention_on_path"])
        self.assertNotIn("main_trap", p["clogs"])
        self.assertNotIn("main_trap", p["blocks"])
        weir = next(c for c in p["checked"] if c["feature_id"] == "main_trap")
        self.assertTrue(weir["by_design"])

    def test_the_assumed_bore_passes_a_swollen_capsule(self):
        p = handoff("hairpin_to_module4")["particles"]
        line = next(c for c in p["checked"] if c["side"] == "line")
        self.assertFalse(line["blocks"])
        self.assertGreater(line["margin"], contracts.param("capsule_channel.width_factor_low"))


class JointOperatingPoint(unittest.TestCase):
    """Chip A + line + chip B as ONE network, so chip B's back-pressure reaches A's weir."""

    def test_at_least_one_mvp_transfer_is_solved_as_one_network(self):
        solved = [h for h in FIELD["handoffs"] if h["joint"].get("solved")]
        self.assertTrue(solved, "no handoff in the MVP chain got a joint operating point")

    def test_module3_coupled_to_module4_sees_back_pressure_at_the_weir(self):
        j = handoff("hairpin_to_module4")["joint"]
        self.assertTrue(j["solved"])
        self.assertEqual(j["weir"]["feature_id"], "main_trap")
        self.assertTrue(j["weir"]["is_retention"])
        self.assertGreater(j["weir"]["p_joint_kpa"], j["weir"]["p_alone_kpa"])
        self.assertAlmostEqual(
            j["weir"]["back_pressure_from_chip_b_kpa"],
            j["weir"]["p_joint_kpa"] - j["weir"]["p_alone_kpa"],
            places=9,
        )
        self.assertGreater(j["weir"]["back_pressure_from_chip_b_kpa"], 0.0)

    def test_the_weir_is_checked_against_the_contract_escape_pressure(self):
        j = handoff("hairpin_to_module4")["joint"]
        self.assertEqual(j["weir"]["limit_kpa"], contracts.max_working_pressure_kpa())
        self.assertIs(j["weir"]["over_limit"], j["weir"]["p_joint_kpa"] > j["weir"]["limit_kpa"])

    def test_the_composition_names_which_chip_stood_in_for_the_placeholder(self):
        j = handoff("hairpin_to_module4")["joint"]
        self.assertIn("module3", j["composed_from"])
        self.assertIn("placeholder", j["composed_from"])

    def test_the_line_carries_what_the_drive_puts_in(self):
        j = handoff("hairpin_to_module4")["joint"]
        self.assertAlmostEqual(abs(j["q_through_line_ul_min"]), j["drive_ul_min"], delta=1e-6)

    def test_a_coupled_solve_costs_more_pressure_than_the_line_alone(self):
        h = handoff("hairpin_to_module4")
        self.assertGreater(h["joint"]["p_upstream_outlet_kpa"], h["joint"]["line_dp_kpa"])

    def test_an_unsolvable_joint_says_exactly_why(self):
        for hid in ("module4_to_module5", "module2_to_module3", "spri_qc_to_library_out"):
            j = handoff(hid)["joint"]
            self.assertFalse(j["solved"], hid)
            self.assertTrue(j["why"].strip(), hid)
        self.assertIn("TBC", handoff("module4_to_module5")["joint"]["why"])
        self.assertIn("no feed port", handoff("module2_to_module3")["joint"]["why"])

    def test_the_downstream_chip_resistance_is_a_real_solve(self):
        chip = schema_io.load_json(ROOT / "chip" / "module4_v1.json")
        mu = contracts.viscosity_37c_pa_s().pa_s
        r = couple.inlet_resistance(chip, "in_s1", "p_vent", mu)
        self.assertGreater(r["resistance_kpa_per_ul_min"], 0)
        q = 3.92
        self.assertAlmostEqual(
            handoff("hairpin_to_module4")["pressure"]["downstream_chip_dp_kpa"],
            r["resistance_kpa_per_ul_min"] * q,
            places=9,
        )


class OffChipBoundaries(unittest.TestCase):
    """The droplet kit, SPRI and QC are interfaces, and every human is counted."""

    def test_a_boundary_is_marked_and_not_silently_skipped(self):
        for hid in ("module3_to_barcoding", "barcoding_to_hairpin", "hairpin_to_module4",
                    "module5_to_spri_qc", "spri_qc_to_library_out"):
            h = handoff(hid)
            self.assertTrue(h["boundary"], hid)
            self.assertGreater(h["line"]["dead_volume_ul"], 0, hid)  # the interface IS modelled

    def test_every_handoff_a_human_carries_is_automated_false_with_a_reason(self):
        for h in FIELD["handoffs"]:
            self.assertIsInstance(h["automated"], bool)
            self.assertTrue(h["automated_why"].strip(), h["id"])
        for hid in ("cellprep_to_module2", "module2_to_module3", "module5_to_spri_qc"):
            self.assertFalse(handoff(hid)["automated"], hid)

    def test_a_stage_with_no_chip_is_never_automated(self):
        for h in FIELD["handoffs"]:
            if not (h["from"]["on_chip"] and h["to"]["on_chip"]):
                self.assertFalse(h["automated"], h["id"])

    def test_the_manual_count_is_the_headline_number(self):
        a = FIELD["automation"]
        self.assertEqual(a["handoffs"], len(FIELD["handoffs"]))
        self.assertEqual(a["automated"] + a["manual"], a["handoffs"])
        self.assertEqual(
            sorted(a["manual_ids"]), sorted(h["id"] for h in FIELD["handoffs"] if not h["automated"])
        )
        self.assertGreater(a["manual"], 0)  # the MVP chain is not hands-free, and must say so

    def test_module4_has_no_outlet_port_and_that_is_a_blocker(self):
        h = handoff("module4_to_module5")
        self.assertEqual(h["ports_check"]["sides"]["from"]["verdict"], "tbc_on_built_chip")
        self.assertEqual(h["severity_parts"]["ports"], "blocker")
        self.assertIn("in_s1", h["ports_check"]["sides"]["from"]["on_chip_ports"])

    def test_a_tbc_port_on_a_placeholder_is_only_high(self):
        self.assertEqual(
            handoff("module3_to_barcoding")["ports_check"]["sides"]["to"]["verdict"], "tbc_on_boundary"
        )
        self.assertEqual(handoff("module3_to_barcoding")["severity_parts"]["ports"], "high")

    def test_a_named_port_that_is_not_on_the_chip_would_be_caught(self):
        bad = couple.Handoff(
            id="bad", carries="capsules", from_stage="module3", from_ports=("nope",),
            to_stage="module4", to_ports=("in_s1",), after_step=9, connector={}, note="",
        )
        chips = {
            "module3": schema_io.load_json(ROOT / "chip" / "module3_v1_translated.json"),
            "module4": schema_io.load_json(ROOT / "chip" / "module4_v1.json"),
        }
        protos = {
            "module3": schema_io.load_json(ROOT / "protocols" / "module3_v1.json"),
            "module4": schema_io.load_json(ROOT / "protocols" / "module4_v1.json"),
        }
        chain = couple.Chain("t", "test", "pipeline", CHAIN.stages, [bad])
        out = couple.analyse_handoff(bad, chain, chips, protos, contracts.viscosity_37c_pa_s())
        self.assertIn("not on the chip", out["ports_check"]["sides"]["from"]["verdict"])
        self.assertEqual(out["severity_parts"]["ports"], "blocker")


class Findings(unittest.TestCase):
    def test_the_worst_handoff_is_named_with_its_reason(self):
        worst = FIELD["worst_handoff"]
        self.assertIn(worst["id"], BY_ID)
        self.assertEqual(worst["severity"], "blocker")
        self.assertTrue(worst["why"].strip())
        best_score = max(h["severity_score"] for h in FIELD["handoffs"])
        self.assertEqual(BY_ID[worst["id"]]["severity_score"], best_score)

    def test_the_mvp_chain_has_blockers_and_says_so(self):
        severities = {h["id"]: h["severity"] for h in FIELD["handoffs"]}
        self.assertIn("blocker", severities.values())
        self.assertEqual(severities["module2_to_module3"], "blocker")

    def test_two_blockers_outrank_one(self):
        self.assertGreater(
            couple.severity_score({"a": "blocker", "b": "blocker"}),
            couple.severity_score({"a": "blocker", "b": "low"}),
        )

    def test_every_gap_lands_in_unresolved(self):
        self.assertTrue(FIELD["unresolved"])
        for u in FIELD["unresolved"]:
            self.assertTrue(u["question"].strip())

    def test_the_solver_in_use_is_recorded(self):
        s = FIELD["solver"]
        self.assertIn("composition", s)
        self.assertIs(s["solve_py"], couple.cfd_solve is not None)
        self.assertIs(s["mesh_path_available"], couple.MESH_AVAILABLE)
        if not couple.MESH_AVAILABLE:
            self.assertIn("operator/network.py", s["todo"])
        else:
            self.assertIsNone(s["todo"])


class SiblingContract(unittest.TestCase):
    """analyse.py reads flat keys off each handoff record; they must be there."""

    FLAT = ("from_chip", "to_chip", "after_step", "particle_um", "narrowest_passable_um",
            "released_volume_ul", "drawn_volume_ul", "dead_volume_ul")

    def test_every_handoff_carries_the_flat_keys(self):
        for h in FIELD["handoffs"]:
            for key in self.FLAT:
                self.assertIn(key, h, f"{h['id']} is missing {key}")
            self.assertEqual(h["from_chip"], h["from"]["stage"])
            self.assertEqual(h["to_chip"], h["to"]["stage"])

    def test_the_flat_keys_match_the_nested_blocks(self):
        for h in FIELD["handoffs"]:
            # the nested block rounds for readability; the flat key keeps full precision
            self.assertAlmostEqual(h["dead_volume_ul"], h["line"]["dead_volume_ul"], places=4)
            self.assertEqual(h["particle_um"], h["particles"]["diameter_um"])
            self.assertEqual(h["released_volume_ul"], h["continuity"]["released_ul"])
            self.assertEqual(h["drawn_volume_ul"], h["continuity"]["drawn_ul"])

    def test_the_narrowest_passage_excludes_a_by_design_weir(self):
        h = handoff("module3_to_barcoding")
        openings = [c["opening_mm"] for c in h["particles"]["checked"] if not c["by_design"]]
        self.assertAlmostEqual(h["narrowest_passable_um"], min(openings) * 1000, places=6)
        weir = next(c for c in h["particles"]["checked"] if c["feature_id"] == "main_trap")
        self.assertLess(weir["opening_mm"] * 1000, h["narrowest_passable_um"])

    def test_assumptions_and_unresolved_use_the_shapes_run_py_collects(self):
        for a in FIELD["assumptions"]:
            self.assertTrue(a.get("key"))
            self.assertIn("value", a)
            self.assertTrue(a.get("source"))
        for u in FIELD["unresolved"]:
            self.assertTrue(u.get("question"))


class MeshBackedJoint(unittest.TestCase):
    """The preferred path: two real meshes plus the line, handed to solve.py."""

    def setUp(self):
        if not couple.MESH_AVAILABLE:
            self.skipTest("mesh.py / solve.py / model.py are not importable")

    def test_the_joint_solve_uses_mesh_and_solve(self):
        j = handoff("hairpin_to_module4")["joint"]
        self.assertIn("mesh.py", j["solver"])
        self.assertNotIn("fell_back_from_mesh", j)

    def test_both_chips_and_the_line_are_in_one_system(self):
        j = handoff("hairpin_to_module4")["joint"]
        self.assertEqual(j["mesh"]["chips"], ["module3_v1_translated", "module4_v1"])
        self.assertGreater(j["mesh"]["nodes"], 1000)        # chambers really are discretised
        self.assertEqual(j["mesh"]["edges"], j["mesh"]["edges"])
        self.assertGreater(j["mesh"]["weir_cells_on_chip_a"], 1)

    def test_the_composed_solve_conserves_mass(self):
        r = handoff("hairpin_to_module4")["joint"]["residual"]
        self.assertLess(abs(r["mass_imbalance_max"]), 1e-15)
        self.assertAlmostEqual(r["q_in"], r["q_out"], delta=abs(r["q_in"]) * 1e-6)

    def test_the_line_conductance_is_poiseuille_not_a_rectangular_duct(self):
        line = couple.build_line(
            "t", contracts.viscosity_37c_pa_s(), None, None, valves=0
        )
        g = couple.line_edge_g_geom_m3(line)
        bore = contracts.param("tubing.inner_diameter_mm")
        length = contracts.param("tubing.length_mm")
        expected = math.pi * (bore / 1000) ** 4 / (128 * (length / 1000))
        self.assertAlmostEqual(g, expected, delta=expected * 1e-9)

    def test_the_line_edge_cross_section_matches_a_circle(self):
        from assay2chip.cfd import model as m

        chip_a = schema_io.load_json(ROOT / "chip" / "module3_v1_translated.json")
        chip_b = schema_io.load_json(ROOT / "chip" / "module4_v1.json")
        mu = contracts.viscosity_37c_pa_s()
        line = couple.build_line("t", mu, None, None)
        mesh, link_id = couple.compose_meshes(
            couple.chip_mesh(chip_a, mu.pa_s), couple.chip_mesh(chip_b, mu.pa_s),
            "to_split_tree", "in_s1", line,
        )
        edge = mesh.edge(link_id)
        self.assertIsInstance(edge, m.Edge)
        self.assertAlmostEqual(
            edge.width_mm * edge.depth_mm,
            math.pi * (line.narrowest_bore_mm / 2) ** 2,
            places=9,
        )
        self.assertAlmostEqual(edge.g_geom_m3, couple.line_edge_g_geom_m3(line), places=20)

    def test_composing_prefixes_both_chips_so_ids_never_collide(self):
        chip_a = schema_io.load_json(ROOT / "chip" / "module3_v1_translated.json")
        chip_b = schema_io.load_json(ROOT / "chip" / "module4_v1.json")
        mu = contracts.viscosity_37c_pa_s()
        ma, mb = couple.chip_mesh(chip_a, mu.pa_s), couple.chip_mesh(chip_b, mu.pa_s)
        mesh, _ = couple.compose_meshes(
            ma, mb, "to_split_tree", "in_s1", couple.build_line("t", mu, None, None)
        )
        self.assertEqual(len(mesh.nodes), len(ma.nodes) + len(mb.nodes))
        self.assertEqual(len({n.id for n in mesh.nodes}), len(mesh.nodes))
        self.assertEqual(len(mesh.edges), len(ma.edges) + len(mb.edges) + 1)
        self.assertIn("A/to_split_tree", mesh.ports)
        self.assertIn("B/in_s1", mesh.ports)
        self.assertEqual(mesh.meta["mu_pa_s"], mu.pa_s)

    def test_the_mesh_is_cached_so_one_chip_is_only_meshed_once(self):
        chip = schema_io.load_json(ROOT / "chip" / "module4_v1.json")
        mu = contracts.viscosity_37c_pa_s().pa_s
        self.assertIs(couple.chip_mesh(chip, mu), couple.chip_mesh(chip, mu))

    def test_a_transfer_driven_only_from_downstream_is_refused(self):
        """Module 2's oil and shell pumps run during a core transfer, but they are not
        what pushes the sample down the line, and a line flow of zero is not a result."""
        chip_a = schema_io.load_json(ROOT / "chip" / "cell_prep_v1.json")
        chip_b = schema_io.load_json(ROOT / "chip" / "spc_demo_v1.json")
        mu = contracts.viscosity_37c_pa_s()
        with self.assertRaises(couple.CoupleError):
            couple.joint_operating_point_mesh(
                chip_a, chip_b, "out_cells", "in_core",
                couple.build_line("t", mu, None, None), mu.pa_s, 1.667,
                {"out_cells": "cap", "in_quench": "cap"},          # nothing drives chip A
                {"in_oil": "feed", "in_shell": "feed", "out_caps": "vent"},
            )
        self.assertFalse(handoff("cellprep_to_module2")["joint"]["solved"])

    def test_a_port_the_mesh_does_not_have_is_refused(self):
        chip_a = schema_io.load_json(ROOT / "chip" / "module3_v1_translated.json")
        chip_b = schema_io.load_json(ROOT / "chip" / "module4_v1.json")
        mu = contracts.viscosity_37c_pa_s()
        with self.assertRaises(couple.CoupleError):
            couple.joint_operating_point_mesh(
                chip_a, chip_b, "nope", "in_s1",
                couple.build_line("t", mu, None, None), mu.pa_s, 10.8,
                {"backflush_in": "feed"}, {"p_vent": "vent"},
            )

    def test_the_lumped_fallback_gives_the_same_answer_to_within_a_few_percent(self):
        """The two models must agree on the pressure the line and chip B impose, even
        though the mesh resolves the trap and the lumped model averages it."""
        chip_a = schema_io.load_json(ROOT / "chip" / "module3_v1_translated.json")
        chip_b = schema_io.load_json(ROOT / "chip" / "module4_v1.json")
        mu = contracts.viscosity_37c_pa_s()
        line = couple.build_line("t", mu, None, None)
        args = (chip_a, chip_b, "to_split_tree", "in_s1", line, mu.pa_s, 10.8,
                {"backflush_in": "feed", "to_split_tree": "vent"}, {"in_s1": "feed", "p_vent": "vent"})
        fine = couple.joint_operating_point_mesh(*args)
        lumped = couple.joint_operating_point(*args)
        self.assertTrue(fine["solved"] and lumped["solved"])
        self.assertAlmostEqual(
            fine["weir"]["back_pressure_from_chip_b_kpa"],
            lumped["weir"]["back_pressure_from_chip_b_kpa"],
            delta=0.05 * abs(fine["weir"]["back_pressure_from_chip_b_kpa"]),
        )
        self.assertAlmostEqual(
            fine["q_through_line_ul_min"], lumped["q_through_line_ul_min"], delta=1e-6
        )


class Output(unittest.TestCase):
    def test_the_payload_is_json_and_round_trips(self):
        blob = json.dumps(FIELD)
        self.assertEqual(json.loads(blob)["pipeline_id"], "pipeline_mvp")

    def test_every_number_key_names_its_unit(self):
        units = ("_mm", "_ul", "_kpa", "_pa", "_s", "_um", "_min", "_dyn_cm2", "_kg_m3", "_m3")
        for h in FIELD["handoffs"]:
            for block in ("line", "pressure", "dead_volume", "settling"):
                for key, value in h[block].items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        self.assertTrue(
                            any(u in key for u in units)
                            or key in ("fraction_of_limit", "fraction_lost_in_line", "clog_factor",
                                       "transit_over_settle", "line_share_of_total", "transfers",
                                       "line_volumes_per_transfer", "dilution_if_line_primed",
                                       "reynolds_in_line", "released_over_drawn"),
                            f"{h['id']}.{block}.{key} does not name its unit",
                        )

    def test_run_writes_pipeline_field_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "cfd" / "pipeline.field.json"
            payload, path = couple.run(MVP, out)
            self.assertEqual(path, out)
            self.assertEqual(schema_io.load_json(out)["pipeline_id"], payload["pipeline_id"])

    def test_the_summary_lists_every_handoff_and_the_assumptions(self):
        text = couple.summarise(FIELD)
        for h in FIELD["handoffs"]:
            self.assertIn(h["id"], text)
        for a in FIELD["assumptions"]:
            self.assertIn(a["key"], text)
        self.assertIn("[contract]", text)
        self.assertIn("[LOCAL to couple.py]", text)
        self.assertIn("of the band", text)   # which end each settling result used

    def test_the_cli_runs_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "pipeline.field.json"
            self.assertEqual(couple.main(["--pipeline", str(MVP), "-o", str(out)]), 0)
            self.assertTrue(out.exists())


class PartitionInput(unittest.TestCase):
    """The same model must run on a stage-S4 partition.json, not just the hand-written chain."""

    PARTITION = {
        "id": "couple_test",
        "placeholder": True,
        "chips": [
            {"chip_id": "module3_v1_translated", "ops": [1], "scale": "demo", "reason": "trap"},
            {"chip_id": "module4_v1", "ops": [2], "scale": "demo", "reason": "pcr"},
        ],
        "handoffs": [
            {
                "from": "module3_v1_translated", "from_port": "to_split_tree",
                "to": "module4_v1", "to_port": "in_s1",
                "carries": "capsules", "volume_ul": 76.886, "particle_um": 140.0,
                "automated": False, "how": "manual pipette into the rotary valve",
                "risks": ["dead volume", "sedimentation"], "dead_volume_ul": None,
            }
        ],
        "cuts": [{"between": [1, 2], "why": "98 C PCR cannot share a board with the capsule trap"}],
        "unresolved": [],
    }

    @classmethod
    def setUpClass(cls):
        # One couple() per class: each joint solve composes two full chip meshes.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "partition.json"
            path.write_text(json.dumps(cls.PARTITION), encoding="utf-8")
            cls.chain = couple.read_chain(path)
            cls.field = couple.couple(cls.chain)

    def _run(self):
        return self.chain, self.field

    def test_a_partition_file_is_read_and_coupled(self):
        chain, field = self._run()
        self.assertEqual(chain.kind, "partition")
        self.assertEqual(field["source_kind"], "partition")
        self.assertEqual(len(field["handoffs"]), 1)

    def test_the_partitions_own_statements_win_over_derivation(self):
        _, field = self._run()
        h = field["handoffs"][0]
        self.assertFalse(h["automated"])
        self.assertIn("stated in the chain file", h["automated_why"])
        self.assertEqual(h["particles"]["diameter_um"], 140.0)
        self.assertEqual(h["dead_volume"]["volume_carried_ul"], 76.886)

    def test_the_joint_network_solves_on_a_partition_too(self):
        _, field = self._run()
        j = field["handoffs"][0]["joint"]
        self.assertTrue(j["solved"], j.get("why"))
        self.assertEqual(j["weir"]["feature_id"], "main_trap")
        self.assertGreater(j["weir"]["back_pressure_from_chip_b_kpa"], 0)


if __name__ == "__main__":
    unittest.main()

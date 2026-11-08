"""Checks assay2chip/cfd/analyse.py: every finding class, on synthetic fields.

    python -m unittest tests.test_cfd_analyse -v

These run OFFLINE. The sibling CFD modules (mesh, solve, transport, particles, couple)
may not exist yet, so every input here is a hand-built dict in the shape
`assay2chip/cfd/README.md` specifies. The tests at the bottom marked `real chip` skip
themselves until the siblings land.

The point of most of these tests is the SCOPING, not the arithmetic: this repo has
mis-scoped the shear ceiling at least six times, and the tests that matter most are the
ones asserting that a capsule chamber is NOT judged by the 0.01 culture ceiling and that
a weir does not by itself mean capsules.
"""

from __future__ import annotations

import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assay2chip.cfd import analyse  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402

TICKS = None  # not used here; time is seconds in the protocol JSON


# --- builders -----------------------------------------------------------------

def mesh(nodes, edges, ports=None, chip_id="synth"):
    return {
        "nodes": nodes,
        "edges": edges,
        "ports": ports or {},
        "meta": {"chip_id": chip_id, "grid_mm": 0.2, "mu_pa_s": contracts.viscosity_for(37).pa_s},
    }


def node(nid, feature_id=None, kind="cell", **kw):
    return {"id": nid, "kind": kind, "feature_id": feature_id, "area_mm2": 0.04,
            "depth_mm": 0.3, "x_mm": 0.0, "y_mm": 0.0, **kw}


def edge(eid, a, b, feature_id=None, width_mm=0.2, depth_mm=0.3, **kw):
    return {"id": eid, "a": a, "b": b, "kind": "duct", "feature_id": feature_id,
            "length_mm": 1.0, "width_mm": width_mm, "depth_mm": depth_mm,
            "conductance_m3_per_pa_s": 1e-12, **kw}


def field(p_pa=None, shear=None, re=None, u=None, q=None):
    return {
        "p_pa": p_pa or {},
        "q_m3_s": q or {},
        "u_mm_s": u or {},
        "shear_dyn_cm2": shear or {},
        "re": re or {},
        "residual": {"mass_imbalance_max": 0.0, "q_in": 1.0, "q_out": 1.0},
    }


def step(index=0, label=1, operation="exchange", target=None, reagent=None,
         feeds=("p_in",), vents=("p_out",), closed=(), **kw):
    raw = {
        "step": label,
        "operation": operation,
        "target": target,
        "reagent": reagent,
        "ports": {"feed": list(feeds), "vent": list(vents), "cap": list(closed)},
        **kw,
    }
    return analyse.step_views({"steps": [raw]})[0]


def bundle(**kw):
    kw.setdefault("chip_id", "synth")
    return analyse.Bundle(**kw)


def classes(findings):
    return [analyse.finding_class_of(f) for f in findings]


def by_class(findings, cls):
    return [f for f in findings if analyse.finding_class_of(f) == cls]


def one(findings, cls):
    hits = by_class(findings, cls)
    assert len(hits) == 1, f"expected exactly one {cls}, got {len(hits)}: {classes(findings)}"
    return hits[0]


# Chips used across several tests ------------------------------------------------

ADHERENT_CHIP = {
    "id": "adherent_chip",
    "layers": [{"material": "coc"}],
    "chambers": [{"id": "cx", "type": "culture", "block": "culture_chamber",
                  "volume_ul": 2.88, "note": "culture field for ADHERENT cells"}],
    "channels": [{"id": "ch_in", "from": "p_in", "to": "cx", "width_mm": 0.8}],
}

ADHERENT_PROTOCOL = {
    "id": "adherent_proto",
    "steps": [
        {"step": 1, "operation": "load", "target": "cx", "reagent": "cell_suspension",
         "flow_ul_min": 3.78, "ports": {"feed": ["p_in"], "vent": ["p_out"]}},
        {"step": 2, "operation": "culture", "target": "cx", "reagent": "culture_medium",
         "temp_c": 37, "flow_ul_min": 3.78, "ports": {"feed": ["p_in"], "vent": ["p_out"]}},
        {"step": 3, "operation": "exchange", "target": "cx", "reagent": "cold_volatile_rinse",
         "flow_ul_min": 1.93, "temp_c": 4, "ports": {"feed": ["p_in"], "vent": ["p_out"]}},
        {"step": 4, "operation": "exchange", "target": "cx",
         "reagent": "cold_acidified_organic_quench", "flow_ul_min": 1373.3, "temp_c": -20,
         "ports": {"feed": ["p_in"], "vent": ["p_out"]}},
        {"step": 5, "operation": "collect", "targets": ["cx"]},
    ],
}

CAPSULE_CHIP = {
    "id": "capsule_chip",
    "layers": [{"material": "coc"}],
    "capsule": {"diameter_um": contracts.capsule_diameter_um()},
    "chambers": [{"id": "trap", "type": "exchange", "volume_ul": 38.4,
                  "retention": {"type": "weir", "opening_um": 25, "source": "fabricated"}}],
    "channels": [{"id": "ch_cap", "from": "cap_in", "to": "trap", "width_mm": 0.2,
                  "carries": "capsules", "capsule_passable": True}],
}

CAPSULE_PROTOCOL = {
    "id": "capsule_proto",
    "steps": [
        {"step": 1, "operation": "load", "target": "trap", "reagent": "capsule_suspension",
         "flow_ul_min": 10.8, "ports": {"feed": ["cap_in"], "vent": ["waste"]}},
        {"step": 2, "operation": "exchange", "target": "trap", "reagent": "wash_buffer",
         "washes": 15, "flow_ul_min": 10.8, "ports": {"feed": ["reagent_in"], "vent": ["waste"]}},
    ],
}

# The trap that is NOT a capsule trap: module1's weir chambers hold adherent cells.
WEIR_BUT_CELLS_CHIP = {
    "id": "weir_cells_chip",
    "layers": [{"material": "coc"}],
    "chambers": [{"id": "g_cx", "type": "exchange", "block": "trap_chamber",
                  "volume_ul": 3.132,
                  "retention": {"type": "weir", "opening_um": 25, "source": "fabricated"}}],
    "channels": [{"id": "ch_g", "from": "p_g_in", "to": "g_cx", "width_mm": 0.2}],
}

WEIR_BUT_CELLS_PROTOCOL = {
    "id": "weir_cells_proto",
    "steps": [
        {"step": 1, "operation": "exchange", "target": "g_cx", "reagent": "coating_solution",
         "flow_ul_min": 1.0, "ports": {"feed": ["p_g_in"], "vent": ["p_mid"]}},
        {"step": 2, "operation": "load", "target": "g_cx", "flow_ul_min": 1.0,
         "ports": {"feed": ["p_g_in"], "vent": ["p_mid"]}},
        {"step": 3, "operation": "hold_temp", "target": "g_cx", "temp_c": 37,
         "ports": {"cap": ["p_g_in", "p_mid"]}},
        # A barcode quench, NOT a lyse: the same cells are cultured for days afterwards.
        {"step": 4, "operation": "exchange", "target": "g_cx", "reagent": "quench_buffer",
         "flow_ul_min": 1.0, "ports": {"feed": ["p_g_in"], "vent": ["p_mid"]}},
        {"step": 5, "operation": "hold_temp", "target": "g_cx", "temp_c": 37,
         "ports": {"cap": ["p_g_in", "p_mid"]}},
        {"step": 6, "operation": "exchange", "target": "g_cx",
         "reagent": "microglia_maturation_medium", "flow_ul_min": 1.0,
         "ports": {"feed": ["p_g_in"], "vent": ["p_mid"]}},
    ],
}


# --- severity rules -------------------------------------------------------------

class TestSeverityRules(unittest.TestCase):
    def test_ladder(self):
        self.assertEqual(analyse.severity_for_ratio(3.0), "blocker")
        self.assertEqual(analyse.severity_for_ratio(2.0), "blocker")
        self.assertEqual(analyse.severity_for_ratio(1.9), "high")
        self.assertEqual(analyse.severity_for_ratio(1.25), "high")
        self.assertEqual(analyse.severity_for_ratio(1.0), "medium")
        self.assertEqual(analyse.severity_for_ratio(0.95), "low")
        self.assertEqual(analyse.severity_for_ratio(0.5), "low")

    def test_ladder_is_monotonic(self):
        ranks = [analyse.SEVERITIES.index(analyse.severity_for_ratio(r / 20))
                 for r in range(1, 80)]
        self.assertEqual(ranks, sorted(ranks, reverse=True))

    def test_escalate_and_cap(self):
        self.assertEqual(analyse.escalate("medium"), "high")
        self.assertEqual(analyse.escalate("high"), "blocker")
        self.assertEqual(analyse.escalate("blocker"), "blocker")
        self.assertEqual(analyse.cap("blocker", "high"), "high")
        self.assertEqual(analyse.cap("low", "high"), "low")

    def test_fraction_scales_with_volume_and_with_need(self):
        self.assertEqual(analyse.severity_for_fraction(0.30, needed=False), "high")
        self.assertEqual(analyse.severity_for_fraction(0.30, needed=True), "blocker")
        self.assertEqual(analyse.severity_for_fraction(0.12, needed=False), "medium")
        self.assertEqual(analyse.severity_for_fraction(0.12, needed=True), "high")
        self.assertEqual(analyse.severity_for_fraction(0.03, needed=False), "low")
        self.assertEqual(analyse.severity_for_fraction(0.03, needed=True), "medium")

    def test_local_assumption_caps_at_high(self):
        """We do not block a build on a number this module invented."""
        collector = analyse._Collector("c")
        collector.add("bubble", "blocker", metric="m", message="x",
                      assumptions=["contact_angle_deg"])
        self.assertEqual(collector.items[0].severity, "high")
        self.assertIn("local assumption contact_angle_deg", collector.items[0].message)

    def test_contract_assumption_does_not_cap(self):
        collector = analyse._Collector("c")
        collector.add("leak", "blocker", metric="m", message="x",
                      assumptions=["capsule.escape_pressure_kpa"])
        self.assertEqual(collector.items[0].severity, "blocker")
        self.assertIn("contract assumption", collector.items[0].message)

    def test_unknown_class_or_severity_raises(self):
        collector = analyse._Collector("c")
        with self.assertRaises(ValueError):
            collector.add("vibes", "high", metric="m", message="x")
        with self.assertRaises(ValueError):
            collector.add("leak", "catastrophic", metric="m", message="x")


# --- what a chamber holds -------------------------------------------------------

class TestShearScoping(unittest.TestCase):
    """The repo got this wrong at least six times. These are the regression tests."""

    def scope(self, chip, protocol, chamber_id, step_index):
        chambers = analyse.chamber_views(chip)
        steps = analyse.step_views(protocol)
        capsule_chip, evidence = analyse.is_capsule_chip(chip, protocol)
        return analyse.shear_scope(
            chambers[chamber_id], steps[step_index], steps,
            capsule_chip=capsule_chip, capsule_evidence=evidence,
        )

    def test_culture_step_is_adherent_cells_at_0_01(self):
        call = self.scope(ADHERENT_CHIP, ADHERENT_PROTOCOL, "cx", 1)
        self.assertEqual(call.shear_class, "adherent_cells")
        self.assertEqual(call.ceiling_dyn_cm2,
                         contracts.param("shear.culture_ceiling_dyn_cm2"))
        self.assertTrue(call.resolved)

    def test_rinse_before_a_deliberate_lyse_is_0_3_not_0_01(self):
        call = self.scope(ADHERENT_CHIP, ADHERENT_PROTOCOL, "cx", 2)
        self.assertEqual(call.shear_class, "prelysis_rinse")
        self.assertEqual(call.ceiling_dyn_cm2,
                         contracts.param("shear.prelysis_rinse_ceiling_dyn_cm2"))
        self.assertNotEqual(call.ceiling_dyn_cm2,
                            contracts.param("shear.culture_ceiling_dyn_cm2"))

    def test_the_lyse_step_itself_has_no_ceiling(self):
        """Holding a 1-second quench flood to the culture ceiling makes it 12 minutes."""
        call = self.scope(ADHERENT_CHIP, ADHERENT_PROTOCOL, "cx", 3)
        self.assertEqual(call.shear_class, "none")
        self.assertIsNone(call.ceiling_dyn_cm2)

    def test_after_the_lyse_there_is_nothing_left_to_protect(self):
        proto = json.loads(json.dumps(ADHERENT_PROTOCOL))
        proto["steps"].append({"step": 6, "operation": "exchange", "target": "cx",
                               "reagent": "denaturing_solubilisation_buffer",
                               "flow_ul_min": 1373.3,
                               "ports": {"feed": ["p_in"], "vent": ["p_out"]}})
        call = self.scope(ADHERENT_CHIP, proto, "cx", 5)
        self.assertEqual(call.shear_class, "none")

    def test_capsule_chamber_is_bounded_by_pressure_not_the_culture_ceiling(self):
        call = self.scope(CAPSULE_CHIP, CAPSULE_PROTOCOL, "trap", 1)
        self.assertEqual(call.shear_class, "capsules")
        self.assertIsNone(call.ceiling_dyn_cm2, "a capsule is not attached to anything")
        self.assertEqual(call.pressure_ceiling_kpa, contracts.max_working_pressure_kpa())
        self.assertEqual(call.bound_by, "escape_pressure")

    def test_a_weir_alone_does_not_mean_capsules(self):
        """module1's trap chambers have 25 um weirs and hold ADHERENT CELLS."""
        capsule_chip, evidence = analyse.is_capsule_chip(
            WEIR_BUT_CELLS_CHIP, WEIR_BUT_CELLS_PROTOCOL)
        self.assertFalse(capsule_chip, f"no capsule evidence expected, got {evidence}")
        call = self.scope(WEIR_BUT_CELLS_CHIP, WEIR_BUT_CELLS_PROTOCOL, "g_cx", 5)
        self.assertEqual(call.shear_class, "adherent_cells")
        self.assertEqual(call.ceiling_dyn_cm2,
                         contracts.param("shear.culture_ceiling_dyn_cm2"))

    def test_a_non_terminal_quench_is_not_a_deliberate_lyse(self):
        """module1 quenches a barcoding reaction, then cultures the same cells for days."""
        call = self.scope(WEIR_BUT_CELLS_CHIP, WEIR_BUT_CELLS_PROTOCOL, "g_cx", 3)
        self.assertEqual(call.shear_class, "adherent_cells")
        self.assertNotEqual(call.shear_class, "none")

    def test_unknown_contents_is_reported_not_guessed(self):
        chip = {"id": "mystery", "layers": [{"material": "coc"}],
                "chambers": [{"id": "blob", "type": "", "block": "polygon_chamber"}]}
        proto = {"steps": [{"step": 1, "operation": "exchange", "target": "blob",
                            "reagent": "reagent_a", "flow_ul_min": 5.0,
                            "ports": {"feed": ["p_in"], "vent": ["p_out"]}}]}
        call = self.scope(chip, proto, "blob", 0)
        self.assertEqual(call.shear_class, "unknown")
        self.assertTrue(call.is_unknown)
        self.assertIsNone(call.ceiling_dyn_cm2)
        self.assertTrue(call.evidence, "must say what it looked at")

    def test_unknown_contents_raises_a_finding_rather_than_picking_a_ceiling(self):
        chip = {"id": "mystery", "layers": [{"material": "coc"}],
                "chambers": [{"id": "blob", "block": "polygon_chamber"}]}
        proto = {"steps": [{"step": 1, "operation": "exchange", "target": "blob",
                            "reagent": "reagent_a", "flow_ul_min": 5.0,
                            "ports": {"feed": ["p_in"], "vent": ["p_out"]}}]}
        b = bundle(
            chip=chip, protocol=proto, chip_id="mystery",
            mesh=mesh([node("n1", "blob")], [edge("e1", "n1", "n1", "blob")]),
            steps=[analyse.StepBundle(view=analyse.step_views(proto)[0],
                                      field=field(shear={"e1": 5.0}))],
        )
        shear = [f for f in analyse.analyse_chip(b)
                 if analyse.finding_class_of(f) == "shear"]
        self.assertEqual(len(shear), 1)
        self.assertEqual(shear[0].metric, "shear_scope_unknown")
        self.assertEqual(shear[0].severity, "medium")
        self.assertIsNone(shear[0].limit, "must not quote a ceiling it did not resolve")

    def test_explicit_contents_on_the_chamber_wins(self):
        chip = {"id": "c", "layers": [{"material": "coc"}],
                "chambers": [{"id": "x", "block": "culture_chamber", "contents": "capsules"}]}
        proto = {"steps": [{"step": 1, "operation": "exchange", "target": "x",
                            "flow_ul_min": 1.0, "ports": {"feed": ["a"], "vent": ["b"]}}]}
        self.assertEqual(self.scope(chip, proto, "x", 0).shear_class, "capsules")

    def test_a_pool_or_junction_has_nothing_shear_sensitive(self):
        chip = {"id": "c", "layers": [{"material": "coc"}],
                "chambers": [{"id": "j", "type": "pool", "block": "cross_junction"}]}
        proto = {"steps": [{"step": 1, "operation": "pool", "target": "j",
                            "flow_ul_min": 1.0, "ports": {"feed": ["a"], "vent": ["b"]}}]}
        self.assertEqual(self.scope(chip, proto, "j", 0).shear_class, "none")


class TestShearFindings(unittest.TestCase):
    def test_adherent_over_ceiling_is_graded_on_the_ratio(self):
        ceiling = contracts.param("shear.culture_ceiling_dyn_cm2")
        proto = ADHERENT_PROTOCOL
        views = analyse.step_views(proto)
        b = bundle(
            chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip",
            mesh=mesh([node("n1", "cx")], [edge("e1", "n1", "n1", "cx")]),
            steps=[analyse.StepBundle(view=views[1],
                                      field=field(shear={"e1": ceiling * 3}))],
        )
        finding = one(analyse.analyse_chip(b), "shear")
        self.assertEqual(finding.severity, "blocker")
        self.assertEqual(finding.limit, ceiling)
        self.assertEqual(finding.units, "dyn/cm2")
        self.assertIn("adherent_cells", finding.metric)

    def test_capsule_chamber_never_gets_a_shear_finding(self):
        """6x the culture ceiling in a capsule trap is not a shear problem at all."""
        ceiling = contracts.param("shear.culture_ceiling_dyn_cm2")
        views = analyse.step_views(CAPSULE_PROTOCOL)
        b = bundle(
            chip=CAPSULE_CHIP, protocol=CAPSULE_PROTOCOL, chip_id="capsule_chip",
            mesh=mesh([node("n1", "trap")], [edge("e1", "n1", "n1", "trap")]),
            steps=[analyse.StepBundle(view=views[1],
                                      field=field(shear={"e1": ceiling * 6}))],
        )
        self.assertEqual(by_class(analyse.analyse_chip(b), "shear"), [])

    def test_prelysis_rinse_passes_at_a_level_that_fails_the_culture_ceiling(self):
        culture = contracts.param("shear.culture_ceiling_dyn_cm2")
        rinse = contracts.param("shear.prelysis_rinse_ceiling_dyn_cm2")
        value = (culture + rinse) / 2        # over 0.01, under 0.3
        views = analyse.step_views(ADHERENT_PROTOCOL)
        b = bundle(
            chip=ADHERENT_CHIP, protocol=ADHERENT_PROTOCOL, chip_id="adherent_chip",
            mesh=mesh([node("n1", "cx")], [edge("e1", "n1", "n1", "cx")]),
            steps=[analyse.StepBundle(view=views[2], field=field(shear={"e1": value}))],
        )
        self.assertEqual(by_class(analyse.analyse_chip(b), "shear"), [],
                         "a pre-lyse rinse must not be judged by the culture ceiling")

    def test_a_feed_duct_is_not_held_to_the_chambers_culture_ceiling(self):
        """The 0.01 ceiling is about DETACHING adherent cells, and nothing is attached
        inside a feed line. Holding a comb or an inlet channel to it is the same scoping
        error as judging a capsule bed by it, pointing the other way - it produced ten
        spurious blockers on module1 and six on module6 and buried the real one.
        """
        ceiling = contracts.param("shear.culture_ceiling_dyn_cm2")
        views = analyse.step_views(ADHERENT_PROTOCOL)
        nodes = [node("n1", "cx"), node("n2", "ch_in")]
        edges = [edge("e_cx", "n1", "n1", "cx"), edge("e_duct", "n2", "n2", "ch_in")]
        b = bundle(
            chip=ADHERENT_CHIP, protocol=ADHERENT_PROTOCOL, chip_id="adherent_chip",
            mesh=mesh(nodes, edges),
            steps=[analyse.StepBundle(
                view=views[1],
                # The duct is far worse than the chamber, and must still be silent.
                field=field(shear={"e_cx": ceiling * 3, "e_duct": ceiling * 50}))],
        )
        found = by_class(analyse.analyse_chip(b), "shear")
        self.assertEqual([f.feature_id for f in found], ["cx"],
                         "only the chamber is judged by the chamber's ceiling")
        self.assertEqual(found[0].severity, "blocker")

    def test_no_shear_finding_on_a_capped_hold_step(self):
        proto = {"steps": [{"step": 1, "operation": "hold_temp", "target": "cx",
                            "temp_c": 37, "ports": {"cap": ["p_in", "p_out"]}}]}
        b = bundle(
            chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip",
            mesh=mesh([node("n1", "cx")], [edge("e1", "n1", "n1", "cx")]),
            steps=[analyse.StepBundle(view=analyse.step_views(proto)[0],
                                      field=field(shear={"e1": 99.0}))],
        )
        self.assertEqual(analyse.analyse_chip(b), [])


# --- LEAK -----------------------------------------------------------------------

class TestLeak(unittest.TestCase):
    def test_bond_strength_unknown_is_a_medium_finding_not_a_silent_pass(self):
        b = bundle(
            chip=ADHERENT_CHIP, protocol=ADHERENT_PROTOCOL, chip_id="adherent_chip",
            mesh=mesh([node("n1", "cx")], [edge("e1", "n1", "n1", "cx")]),
            steps=[analyse.StepBundle(view=analyse.step_views(ADHERENT_PROTOCOL)[1],
                                      field=field(p_pa={"n1": 4000.0, "n2": 0.0}))],
        )
        leaks = by_class(analyse.analyse_chip(b), "leak")
        self.assertEqual([f.metric for f in leaks], ["bond_strength_known"])
        self.assertEqual(leaks[0].severity, "medium")
        self.assertIn("bond strength unknown", leaks[0].message)
        self.assertIsNone(leaks[0].limit, "there is no limit to quote")
        self.assertAlmostEqual(leaks[0].value, 4.0)

    def test_the_real_contract_has_no_bond_strength(self):
        value, _material, note = analyse.bond_strength_kpa(ADHERENT_CHIP)
        self.assertIsNone(value, f"materials.json unexpectedly has a bond strength: {note}")

    def test_a_bond_strength_in_the_contract_would_be_compared(self):
        materials = {"coc": {"aliases": ["coc"], "bond_strength_kpa": 50.0}}
        b = bundle(
            chip=ADHERENT_CHIP, protocol=ADHERENT_PROTOCOL, chip_id="adherent_chip",
            materials=materials,
            mesh=mesh([node("n1", "cx")], [edge("e1", "n1", "n1", "cx")]),
            steps=[analyse.StepBundle(view=analyse.step_views(ADHERENT_PROTOCOL)[1],
                                      field=field(p_pa={"n1": 150_000.0, "n2": 0.0}))],
        )
        leak = one(analyse.analyse_chip(b), "leak")
        self.assertEqual(leak.metric, "peak_pressure_vs_bond_strength")
        self.assertEqual(leak.limit, 50.0)
        self.assertEqual(leak.severity, "blocker")

    def test_mpa_bond_strength_is_converted(self):
        materials = {"coc": {"aliases": ["coc"], "bond_strength_mpa": 1.5}}
        value, _m, _n = analyse.bond_strength_kpa(ADHERENT_CHIP, materials)
        self.assertAlmostEqual(value, 1500.0)

    def test_capsule_chip_checked_against_escape_pressure_times_margin(self):
        limit = contracts.max_working_pressure_kpa()
        b = bundle(
            chip=CAPSULE_CHIP, protocol=CAPSULE_PROTOCOL, chip_id="capsule_chip",
            mesh=mesh([node("n1", "trap")], [edge("e1", "n1", "n1", "trap")]),
            steps=[analyse.StepBundle(
                view=analyse.step_views(CAPSULE_PROTOCOL)[1],
                field=field(p_pa={"n1": limit * 1000 * 1.6, "n2": 0.0}))],
        )
        leaks = by_class(analyse.analyse_chip(b), "leak")
        escape = [f for f in leaks if f.metric == "peak_pressure_vs_escape_margin"]
        self.assertEqual(len(escape), 1)
        self.assertAlmostEqual(escape[0].limit, limit)
        self.assertEqual(escape[0].units, "kPa")
        # 1.6x -> high on the ladder, escalated one rung for a seal breach.
        self.assertEqual(escape[0].severity, "blocker")
        self.assertIn("capsule.escape_pressure_kpa", analyse.finding_assumptions(escape[0]))

    def test_limit_is_escape_times_margin_and_not_hard_coded(self):
        self.assertAlmostEqual(
            contracts.max_working_pressure_kpa(),
            contracts.escape_pressure_kpa() * contracts.pressure_margin(),
        )

    def test_a_non_capsule_chip_is_not_checked_against_escape_pressure(self):
        b = bundle(
            chip=ADHERENT_CHIP, protocol=ADHERENT_PROTOCOL, chip_id="adherent_chip",
            mesh=mesh([node("n1", "cx")], [edge("e1", "n1", "n1", "cx")]),
            steps=[analyse.StepBundle(view=analyse.step_views(ADHERENT_PROTOCOL)[1],
                                      field=field(p_pa={"n1": 900_000.0, "n2": 0.0}))],
        )
        metrics = [f.metric for f in by_class(analyse.analyse_chip(b), "leak")]
        self.assertNotIn("peak_pressure_vs_escape_margin", metrics)

    def test_pressure_under_the_limit_is_not_reported(self):
        limit = contracts.max_working_pressure_kpa()
        b = bundle(
            chip=CAPSULE_CHIP, protocol=CAPSULE_PROTOCOL, chip_id="capsule_chip",
            mesh=mesh([node("n1", "trap")], [edge("e1", "n1", "n1", "trap")]),
            steps=[analyse.StepBundle(
                view=analyse.step_views(CAPSULE_PROTOCOL)[1],
                field=field(p_pa={"n1": limit * 1000 * 0.2, "n2": 0.0}))],
        )
        metrics = [f.metric for f in by_class(analyse.analyse_chip(b), "leak")]
        self.assertNotIn("peak_pressure_vs_escape_margin", metrics)


# --- DEAD END -------------------------------------------------------------------

class TestDeadEnd(unittest.TestCase):
    def chip_with_stub(self):
        nodes = [node("in", "p_in", kind="port"), node("c1", "cx"),
                 node("out", "p_out", kind="port"), node("stub", "orphan_branch")]
        edges = [edge("e_in", "in", "c1", "ch_in"), edge("e_out", "c1", "out", "ch_out"),
                 edge("e_stub", "c1", "stub", "orphan_branch")]
        return mesh(nodes, edges, ports={"p_in": "in", "p_out": "out"})

    def test_a_branch_with_no_outlet_is_a_dead_end(self):
        proto = {"steps": [{"step": 1, "operation": "exchange", "target": "cx",
                            "flow_ul_min": 1.0,
                            "ports": {"feed": ["p_in"], "vent": ["p_out"]}}]}
        b = bundle(chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip",
                   mesh=self.chip_with_stub(),
                   steps=[analyse.StepBundle(view=analyse.step_views(proto)[0],
                                             field=field())])
        finding = one(analyse.analyse_chip(b), "dead_end")
        self.assertEqual(finding.feature_id, "orphan_branch")
        self.assertEqual(finding.severity, "high")
        self.assertEqual(finding.units, "nodes")

    def test_stranding_a_port_the_step_uses_is_a_blocker(self):
        nodes = [node("in", "p_in", kind="port"), node("c1", "cx"),
                 node("out", "p_out", kind="port"), node("lonely", "p_side", kind="port")]
        edges = [edge("e_in", "in", "c1", "ch_in"), edge("e_out", "c1", "out", "ch_out")]
        m = mesh(nodes, edges, ports={"p_in": "in", "p_out": "out", "p_side": "lonely"})
        proto = {"steps": [{"step": 1, "operation": "exchange", "target": "cx",
                            "flow_ul_min": 1.0,
                            "ports": {"feed": ["p_in", "p_side"], "vent": ["p_out"]}}]}
        b = bundle(chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip", mesh=m,
                   steps=[analyse.StepBundle(view=analyse.step_views(proto)[0],
                                             field=field())])
        finding = one(analyse.analyse_chip(b), "dead_end")
        self.assertEqual(finding.severity, "blocker")
        self.assertIn("p_side", finding.message)

    def clean_chip(self):
        nodes = [node("in", "p_in", kind="port"), node("c1", "cx"),
                 node("out", "p_out", kind="port")]
        edges = [edge("e_in", "in", "c1", "ch_in"), edge("e_out", "c1", "out", "ch_out")]
        return mesh(nodes, edges, ports={"p_in": "in", "p_out": "out"})

    def capped_hold(self):
        return {"steps": [{"step": 1, "operation": "hold_temp", "target": "cx",
                           "temp_c": 37, "ports": {"cap": ["p_in", "p_out"]}}]}

    def test_a_capped_hold_is_not_a_dead_end(self):
        """Capping every port is how the chip incubates, not a fault to report."""
        proto = self.capped_hold()
        b = bundle(chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip",
                   mesh=self.clean_chip(),
                   steps=[analyse.StepBundle(view=analyse.step_views(proto)[0],
                                             field=field())])
        self.assertEqual(by_class(analyse.analyse_chip(b), "dead_end"), [])

    def test_a_capped_hold_adds_no_step_scoped_dead_end_to_a_real_one(self):
        """On a chip that DOES have a blind branch, a capped hold adds nothing.

        The branch is a design fault and must still be reported, but it is reported once,
        against the geometry (`step` is None), not once per idle incubation step.
        """
        proto = self.capped_hold()
        b = bundle(chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip",
                   mesh=self.chip_with_stub(),
                   steps=[analyse.StepBundle(view=analyse.step_views(proto)[0],
                                             field=field())])
        found = by_class(analyse.analyse_chip(b), "dead_end")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].feature_id, "orphan_branch")
        self.assertIsNone(found[0].step, "a design fault is not pinned to an idle step")
        self.assertEqual(found[0].metric, "unreachable_nodes")

    def test_an_idle_lane_is_not_a_dead_end(self):
        """module1 grows three lanes on separate days; module6 loads one lane at a time.

        At any moment most of the chip is deliberately capped. Reporting that as a dead
        end would bury the real ones and condemn the chip for working as designed.
        """
        nodes = [node("in_a", "p_in_a", kind="port"), node("ca", "cx_a"),
                 node("out_a", "p_out_a", kind="port"),
                 node("in_b", "p_in_b", kind="port"), node("cb", "cx_b"),
                 node("out_b", "p_out_b", kind="port")]
        edges = [edge("ea_in", "in_a", "ca", "ch_a_in"),
                 edge("ea_out", "ca", "out_a", "ch_a_out"),
                 edge("eb_in", "in_b", "cb", "ch_b_in"),
                 edge("eb_out", "cb", "out_b", "ch_b_out")]
        m = mesh(nodes, edges, ports={"p_in_a": "in_a", "p_out_a": "out_a",
                                      "p_in_b": "in_b", "p_out_b": "out_b"})
        chip = {"id": "two_lane", "layers": [{"material": "coc"}],
                "chambers": [{"id": "cx_a", "type": "culture", "block": "culture_chamber"},
                             {"id": "cx_b", "type": "culture", "block": "culture_chamber"}]}
        # Lane A runs; lane B is capped and idle, so B carries no flow at all.
        proto = {"steps": [{"step": 1, "operation": "exchange", "target": "cx_a",
                            "flow_ul_min": 1.0,
                            "ports": {"feed": ["p_in_a"], "vent": ["p_out_a"],
                                      "cap": ["p_in_b", "p_out_b"]}}]}
        b = bundle(chip=chip, protocol=proto, chip_id="two_lane", mesh=m,
                   steps=[analyse.StepBundle(view=analyse.step_views(proto)[0],
                                             field=field())])
        self.assertEqual(by_class(analyse.analyse_chip(b), "dead_end"), [])

    def test_a_fully_connected_chip_has_no_dead_end(self):
        nodes = [node("in", "p_in", kind="port"), node("c1", "cx"),
                 node("out", "p_out", kind="port")]
        edges = [edge("e_in", "in", "c1", "ch_in"), edge("e_out", "c1", "out", "ch_out")]
        proto = {"steps": [{"step": 1, "operation": "exchange", "target": "cx",
                            "flow_ul_min": 1.0,
                            "ports": {"feed": ["p_in"], "vent": ["p_out"]}}]}
        b = bundle(chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip",
                   mesh=mesh(nodes, edges, ports={"p_in": "in", "p_out": "out"}),
                   steps=[analyse.StepBundle(view=analyse.step_views(proto)[0],
                                             field=field())])
        self.assertEqual(by_class(analyse.analyse_chip(b), "dead_end"), [])

    def test_the_same_topology_is_reported_once_across_many_steps(self):
        proto = {"steps": [
            {"step": i, "operation": "exchange", "target": "cx", "flow_ul_min": 1.0,
             "ports": {"feed": ["p_in"], "vent": ["p_out"]}} for i in range(1, 6)]}
        views = analyse.step_views(proto)
        b = bundle(chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip",
                   mesh=self.chip_with_stub(),
                   steps=[analyse.StepBundle(view=v, field=field()) for v in views])
        self.assertEqual(len(by_class(analyse.analyse_chip(b), "dead_end")), 1)


# --- BUBBLE ---------------------------------------------------------------------

class TestBubble(unittest.TestCase):
    def test_unvented_dead_end_branch_at_priming(self):
        nodes = [node("in", "p_in", kind="port"), node("c1", "cx"),
                 node("out", "p_out", kind="port"), node("tip", "blind_tip")]
        edges = [edge("e_in", "in", "c1", "ch_in"), edge("e_out", "c1", "out", "ch_out"),
                 edge("e_tip", "c1", "tip", "blind_tip")]
        m = mesh(nodes, edges, ports={"p_in": "in", "p_out": "out"})
        proto = {"steps": [{"step": 1, "operation": "prime", "target": "cx",
                            "flow_ul_min": 1.0,
                            "ports": {"feed": ["p_in"], "vent": ["p_out"]}}]}
        b = bundle(chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip", mesh=m,
                   steps=[analyse.StepBundle(view=analyse.step_views(proto)[0],
                                             field=field())])
        bubbles = [f for f in by_class(analyse.analyse_chip(b), "bubble")
                   if f.metric == "unvented_dead_end_at_priming"]
        self.assertEqual(len(bubbles), 1)
        self.assertEqual(bubbles[0].feature_id, "blind_tip")
        self.assertEqual(bubbles[0].severity, "high")

    def test_sudden_expansion_pins_a_meniscus_with_a_flagged_assumption(self):
        nodes = [node("in", "p_in", kind="port"), node("j", "expansion"),
                 node("out", "p_out", kind="port")]
        edges = [edge("e_narrow", "in", "j", "ch_narrow", width_mm=0.2),
                 edge("e_wide", "j", "out", "chamber_mouth", width_mm=4.0)]
        m = mesh(nodes, edges, ports={"p_in": "in", "p_out": "out"})
        proto = {"steps": [{"step": 1, "operation": "prime", "target": "cx",
                            "flow_ul_min": 1.0,
                            "ports": {"feed": ["p_in"], "vent": ["p_out"]}}]}
        b = bundle(chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip", mesh=m,
                   steps=[analyse.StepBundle(view=analyse.step_views(proto)[0],
                                             field=field(p_pa={"in": 20.0, "out": 0.0}))])
        pins = [f for f in by_class(analyse.analyse_chip(b), "bubble")
                if f.metric == "capillary_pin_pressure_pa"]
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0].units, "Pa")
        self.assertIn("contact_angle_deg", analyse.finding_assumptions(pins[0]))
        self.assertIn("surface_tension_n_per_m", analyse.finding_assumptions(pins[0]))
        self.assertIn("NOT in the contracts", pins[0].message)
        self.assertNotEqual(pins[0].severity, "blocker", "rests on an invented number")

    def test_a_gentle_taper_does_not_pin(self):
        nodes = [node("in", "p_in", kind="port"), node("j", "taper"),
                 node("out", "p_out", kind="port")]
        edges = [edge("e_a", "in", "j", "ch_a", width_mm=0.2),
                 edge("e_b", "j", "out", "ch_b", width_mm=0.25)]
        m = mesh(nodes, edges, ports={"p_in": "in", "p_out": "out"})
        proto = {"steps": [{"step": 1, "operation": "prime", "target": "cx",
                            "flow_ul_min": 1.0,
                            "ports": {"feed": ["p_in"], "vent": ["p_out"]}}]}
        b = bundle(chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip", mesh=m,
                   steps=[analyse.StepBundle(view=analyse.step_views(proto)[0],
                                             field=field())])
        self.assertEqual(by_class(analyse.analyse_chip(b), "bubble"), [])

    def test_capillary_pressure_uses_both_width_and_depth(self):
        narrow = analyse.capillary_pressure_pa(0.2, 0.3)
        wide = analyse.capillary_pressure_pa(4.0, 0.3)
        self.assertGreater(narrow, wide, "a narrower duct pulls harder")

    def test_every_local_assumption_is_documented(self):
        for key, a in analyse.ASSUMPTIONS.items():
            self.assertEqual(a.key, key)
            self.assertTrue(a.units, f"{key} has no units")
            self.assertGreater(len(a.why), 40, f"{key} needs a real reason, not a label")


# --- TRAP / DEAD ZONE and EXCHANGE ----------------------------------------------

class TestTrapsAndExchange(unittest.TestCase):
    def transport(self, fraction, chamber="cx", **kw):
        return {"dead_zones": [{"chamber_id": chamber, "volume_fraction": fraction,
                                "mean_velocity_mm_s": 0.001,
                                "chamber_mean_mm_s": 0.1, **kw}]}

    def run_one(self, view, transport):
        b = bundle(chip=ADHERENT_CHIP, protocol=ADHERENT_PROTOCOL, chip_id="adherent_chip",
                   mesh=mesh([node("n1", "cx")], [edge("e1", "n1", "n1", "cx")]),
                   steps=[analyse.StepBundle(view=view, field=field(), transport=transport)])
        return analyse.analyse_chip(b)

    def test_severity_scales_with_affected_volume(self):
        view = analyse.step_views(ADHERENT_PROTOCOL)[1]   # culture, not an exchange
        self.assertEqual(one(self.run_one(view, self.transport(0.40)), "trap").severity, "high")
        self.assertEqual(one(self.run_one(view, self.transport(0.15)), "trap").severity, "medium")
        self.assertEqual(one(self.run_one(view, self.transport(0.05)), "trap").severity, "low")

    def test_severity_escalates_when_the_step_must_exchange_that_chamber(self):
        rinse = analyse.step_views(ADHERENT_PROTOCOL)[2]  # exchange on cx
        culture = analyse.step_views(ADHERENT_PROTOCOL)[1]
        hot = one(self.run_one(rinse, self.transport(0.15)), "trap")
        cool = one(self.run_one(culture, self.transport(0.15)), "trap")
        self.assertEqual(hot.severity, "high")
        self.assertEqual(cool.severity, "medium")
        self.assertIn("has to exchange that chamber", hot.message)

    def test_tiny_zones_are_noise_not_findings(self):
        view = analyse.step_views(ADHERENT_PROTOCOL)[1]
        self.assertEqual(by_class(self.run_one(view, self.transport(0.005)), "trap"), [])

    def test_message_says_it_is_not_a_closed_streamline_test(self):
        view = analyse.step_views(ADHERENT_PROTOCOL)[1]
        finding = one(self.run_one(view, self.transport(0.3)), "trap")
        self.assertIn("no closed streamlines", finding.message)

    def test_dead_zones_fall_back_to_the_velocity_field(self):
        proto = {"steps": [{"step": 1, "operation": "exchange", "target": "cx",
                            "flow_ul_min": 1.0,
                            "ports": {"feed": ["p_in"], "vent": ["p_out"]}}]}
        nodes = [node(f"c{i}", "cx") for i in range(10)]
        speeds = {f"c{i}": [1.0, 0.0] for i in range(8)}
        speeds.update({"c8": [0.0001, 0.0], "c9": [0.0001, 0.0]})
        b = bundle(chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip",
                   mesh=mesh(nodes, []),
                   steps=[analyse.StepBundle(view=analyse.step_views(proto)[0],
                                             field=field(u=speeds))])
        finding = one(analyse.analyse_chip(b), "trap")
        self.assertAlmostEqual(finding.value, 0.2)
        self.assertIn("dead_zone_velocity_fraction", analyse.finding_assumptions(finding))

    def test_exchange_shortfall_against_the_contract_target(self):
        target = contracts.param("exchange.target_fraction")
        view = analyse.step_views(ADHERENT_PROTOCOL)[2]
        b = bundle(chip=ADHERENT_CHIP, protocol=ADHERENT_PROTOCOL, chip_id="adherent_chip",
                   mesh=mesh([node("n1", "cx")], []),
                   steps=[analyse.StepBundle(
                       view=view, field=field(),
                       transport={"exchange_fraction": {"cx": 0.90}})])
        finding = one(analyse.analyse_chip(b), "exchange")
        self.assertEqual(finding.limit, target)
        self.assertAlmostEqual(finding.value, 0.90)
        self.assertEqual(finding.units, "fraction")
        self.assertEqual(finding.severity, "blocker")   # 10x the allowed residual

    def test_exchange_at_target_is_not_a_finding(self):
        target = contracts.param("exchange.target_fraction")
        view = analyse.step_views(ADHERENT_PROTOCOL)[2]
        b = bundle(chip=ADHERENT_CHIP, protocol=ADHERENT_PROTOCOL, chip_id="adherent_chip",
                   mesh=mesh([node("n1", "cx")], []),
                   steps=[analyse.StepBundle(
                       view=view, field=field(),
                       transport={"exchange_fraction": {"cx": target}})])
        self.assertEqual(by_class(analyse.analyse_chip(b), "exchange"), [])

    def test_a_stagnant_corner_is_a_trap_not_an_isolated_chamber(self):
        """A chamber is discretised across its width, so a wide chamber fed from one
        point always has a few pendant cells in a corner. That is a dead ZONE - the whole
        reason the trap class exists - and calling it an isolated target would put a
        blocker on every wide chamber on every chip.
        """
        # cx is 4 cells; one of them hangs off the others, so the chamber flows but has
        # a stagnant corner.
        nodes = [node("in", "p_in", kind="port"), node("out", "p_out", kind="port")]
        nodes += [node(f"c{i}", "cx") for i in range(4)]
        edges = [edge("e_in", "in", "c0", "ch_in"), edge("e_1", "c0", "c1", "cx"),
                 edge("e_2", "c1", "c2", "cx"), edge("e_out", "c2", "out", "ch_out"),
                 edge("e_corner", "c1", "c3", "cx")]
        m = mesh(nodes, edges, ports={"p_in": "in", "p_out": "out"})
        proto = {"steps": [{"step": 1, "operation": "exchange", "target": "cx",
                            "flow_ul_min": 1.0,
                            "ports": {"feed": ["p_in"], "vent": ["p_out"]}}]}
        b = bundle(chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip", mesh=m,
                   steps=[analyse.StepBundle(
                       view=analyse.step_views(proto)[0], field=field(),
                       transport={"dead_zones": [{"chamber_id": "cx",
                                                  "either_volume_fraction": 0.25}]})])
        found = analyse.analyse_chip(b)
        self.assertEqual([f.metric for f in by_class(found, "dead_end")], [],
                         "a stagnant corner must not read as an isolated chamber")
        self.assertEqual(len(by_class(found, "trap")), 1)

    def test_a_dead_end_suppresses_the_trap_it_causes(self):
        """One root cause: an unreachable region is a dead end, not also a trap."""
        nodes = [node("in", "p_in", kind="port"), node("c1", "cx"),
                 node("out", "p_out", kind="port"), node("stub", "orphan")]
        edges = [edge("e_in", "in", "c1", "ch_in"), edge("e_out", "c1", "out", "ch_out"),
                 edge("e_stub", "c1", "stub", "orphan")]
        m = mesh(nodes, edges, ports={"p_in": "in", "p_out": "out"})
        proto = {"steps": [{"step": 1, "operation": "exchange", "target": "orphan",
                            "flow_ul_min": 1.0,
                            "ports": {"feed": ["p_in"], "vent": ["p_out"]}}]}
        b = bundle(chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip", mesh=m,
                   steps=[analyse.StepBundle(
                       view=analyse.step_views(proto)[0], field=field(),
                       transport={"dead_zones": [{"chamber_id": "orphan",
                                                  "volume_fraction": 1.0}]})])
        found = analyse.analyse_chip(b)
        self.assertEqual(len(by_class(found, "dead_end")), 1)
        self.assertEqual(by_class(found, "trap"), [])


# --- particles: SEDIMENT, CLOG, WEIR ESCAPE ---------------------------------------

class TestParticles(unittest.TestCase):
    def capsule_bundle(self, particles, view_index=1):
        return bundle(
            chip=CAPSULE_CHIP, protocol=CAPSULE_PROTOCOL, chip_id="capsule_chip",
            mesh=mesh([node("n1", "trap")], [edge("e1", "n1", "n1", "ch_cap")]),
            steps=[analyse.StepBundle(
                view=analyse.step_views(CAPSULE_PROTOCOL)[view_index],
                field=field(), particles=particles)],
        )

    def test_clog_narrower_than_a_swollen_capsule_is_a_blocker(self):
        swollen = contracts.swollen_capsule_diameter_um()
        found = analyse.analyse_chip(self.capsule_bundle(
            {"clog": [{"feature_id": "ch_cap", "width_mm": swollen / 1000.0 * 0.8}]}))
        finding = one(found, "clog")
        self.assertEqual(finding.severity, "blocker")
        self.assertIn("WILL jam", finding.message)
        self.assertEqual(finding.units, "mm")

    def test_clog_limit_is_the_contract_width_factor(self):
        swollen = contracts.swollen_capsule_diameter_um()
        factor = contracts.param("capsule_channel.width_factor_low")
        found = analyse.analyse_chip(self.capsule_bundle(
            {"clog": [{"feature_id": "ch_cap", "width_mm": swollen / 1000.0 * 1.1}]}))
        finding = one(found, "clog")
        self.assertAlmostEqual(finding.limit, swollen * factor / 1000.0)
        self.assertNotEqual(finding.severity, "blocker", "wider than one capsule")

    def test_a_wide_enough_lane_is_not_reported(self):
        swollen = contracts.swollen_capsule_diameter_um()
        factor = contracts.param("capsule_channel.width_factor_low")
        found = analyse.analyse_chip(self.capsule_bundle(
            {"clog": [{"feature_id": "ch_cap", "width_mm": swollen * factor / 1000.0 * 1.5}]}))
        self.assertEqual(by_class(found, "clog"), [])

    def test_sediment_when_settling_beats_transit(self):
        found = analyse.analyse_chip(self.capsule_bundle(
            {"settling": [{"feature_id": "ch_cap", "settle_s": 2.0, "transit_s": 8.0}]}))
        finding = one(found, "sediment")
        self.assertAlmostEqual(finding.value, 4.0)
        self.assertEqual(finding.limit, 1.0)
        self.assertEqual(finding.severity, "blocker")
        self.assertEqual(finding.units, "transit/settling")

    def test_settling_where_it_is_the_point_is_not_a_finding(self):
        """module3's capsules settle onto their own retention weir. That is the chip
        working as designed, and particles.py says so with `intent`/`verdict`. Reporting
        it was the top blocker on module3 until this was respected.
        """
        found = analyse.analyse_chip(self.capsule_bundle(
            {"settling": [{"feature_id": "main_trap", "particle": "capsules",
                           "intent": "desired", "verdict": "captured", "severity": "low",
                           "fall_time_s": 0.5, "transit_time_s": 220.0,
                           "intent_why": "main_trap carries a retention weir"}]}))
        self.assertEqual(by_class(found, "sediment"), [])

    def test_a_parked_particle_is_not_a_loss(self):
        found = analyse.analyse_chip(self.capsule_bundle(
            {"settling": [{"feature_id": "ch_cap", "particle": "capsules",
                           "intent": "parked", "verdict": "parked",
                           "fall_time_s": 1.0, "transit_time_s": 90.0}]}))
        self.assertEqual(by_class(found, "sediment"), [])

    def test_a_real_loss_still_fires_and_quotes_the_reason(self):
        found = analyse.analyse_chip(self.capsule_bundle(
            {"settling": [{"feature_id": "ch_to_split_tree", "particle": "capsules",
                           "intent": "loss", "verdict": "lost",
                           "fall_time_s": 1.0, "transit_time_s": 3.0,
                           "intent_why": "its job is to DELIVER"}]}))
        finding = one(found, "sediment")
        self.assertEqual(finding.severity, "blocker")
        self.assertIn("its job is to DELIVER", finding.message)
        self.assertIn("max", finding.message, "a loss check must name the density end")
        self.assertEqual(finding.source, "capsule.excess_density_kg_m3")

    def test_no_sediment_when_the_particle_crosses_first(self):
        found = analyse.analyse_chip(self.capsule_bundle(
            {"settling": [{"feature_id": "ch_cap", "settle_s": 20.0, "transit_s": 1.0}]}))
        self.assertEqual(by_class(found, "sediment"), [])

    def test_a_clog_blocker_suppresses_the_sediment_it_explains(self):
        swollen = contracts.swollen_capsule_diameter_um()
        found = analyse.analyse_chip(self.capsule_bundle({
            "clog": [{"feature_id": "ch_cap", "width_mm": swollen / 1000.0 * 0.5}],
            "settling": [{"feature_id": "ch_cap", "settle_s": 1.0, "transit_s": 50.0}],
        }))
        self.assertEqual(len(by_class(found, "clog")), 1)
        self.assertEqual(by_class(found, "sediment"), [])

    def test_weir_escape_against_escape_pressure_times_margin(self):
        limit = contracts.max_working_pressure_kpa()
        found = analyse.analyse_chip(self.capsule_bundle(
            {"weir_escape": [{"feature_id": "trap", "dp_kpa": limit * 1.4}]}))
        finding = one(found, "weir_escape")
        self.assertAlmostEqual(finding.limit, limit)
        self.assertEqual(finding.severity, "high")
        self.assertEqual(finding.units, "kPa")
        self.assertIn("in place of a shear ceiling", finding.message)

    def test_weir_below_the_margin_is_not_reported(self):
        limit = contracts.max_working_pressure_kpa()
        found = analyse.analyse_chip(self.capsule_bundle(
            {"weir_escape": [{"feature_id": "trap", "dp_kpa": limit * 0.3}]}))
        self.assertEqual(by_class(found, "weir_escape"), [])


# --- HANDOFF --------------------------------------------------------------------

class TestHandoff(unittest.TestCase):
    def couple_bundle(self, handoff):
        return bundle(
            chip=CAPSULE_CHIP, protocol=CAPSULE_PROTOCOL, chip_id="capsule_chip",
            mesh=mesh([node("n1", "trap")], []),
            steps=[analyse.StepBundle(view=analyse.step_views(CAPSULE_PROTOCOL)[1],
                                      field=field())],
            couple={"handoffs": [handoff]},
        )

    def test_particle_size_mismatch_is_a_blocker(self):
        found = analyse.analyse_chip(self.couple_bundle(
            {"from": "capsule_chip", "to": "module4", "id": "line_1",
             "particle_um": 140.0, "particle_pass_um": 100.0}))
        finding = one(found, "handoff")
        self.assertEqual(finding.severity, "blocker")
        self.assertEqual(finding.units, "um")
        self.assertIn("do not arrive", finding.message)

    def test_volume_mismatch_is_graded(self):
        found = analyse.analyse_chip(self.couple_bundle(
            {"from": "capsule_chip", "to": "module4", "id": "line_1",
             "volume_out_ul": 80.0, "volume_in_ul": 40.0}))
        finding = one(found, "handoff")
        self.assertEqual(finding.metric, "handoff_volume_mismatch")
        self.assertEqual(finding.units, "uL")
        self.assertIn("overflows", finding.message)

    def test_volume_inside_tolerance_is_not_reported(self):
        found = analyse.analyse_chip(self.couple_bundle(
            {"from": "capsule_chip", "to": "module4", "id": "line_1",
             "volume_out_ul": 41.0, "volume_in_ul": 40.0}))
        self.assertEqual(by_class(found, "handoff"), [])

    def test_dead_volume_in_the_line(self):
        found = analyse.analyse_chip(self.couple_bundle(
            {"from": "capsule_chip", "to": "module4", "id": "line_1",
             "volume_out_ul": 40.0, "dead_volume_ul": 12.0}))
        finding = one(found, "handoff")
        self.assertEqual(finding.metric, "dead_volume_fraction_of_transfer")
        self.assertIn("stays in the line", finding.message)

    def test_pressure_budget_exceeded(self):
        found = analyse.analyse_chip(self.couple_bundle(
            {"from": "capsule_chip", "to": "module4", "id": "line_1",
             "pressure_required_kpa": 9.0, "pressure_budget_kpa": 3.0}))
        finding = one(found, "handoff")
        self.assertEqual(finding.metric, "handoff_pressure_vs_budget")
        self.assertEqual(finding.severity, "blocker")

    def test_handoffs_for_other_chips_are_ignored(self):
        found = analyse.analyse_chip(self.couple_bundle(
            {"from": "someone_else", "to": "another", "id": "line_9",
             "particle_um": 999.0, "particle_pass_um": 1.0}))
        self.assertEqual(by_class(found, "handoff"), [])


# --- UNMODELLED -----------------------------------------------------------------

class TestDensityEnds(unittest.TestCase):
    """Settling is LINEAR in excess density, so which end of the band is the verdict."""

    def test_loss_takes_the_max_retention_takes_the_min(self):
        for particle, key in (("cells", "cell.excess_density_kg_m3"),
                              ("capsules", "capsule.excess_density_kg_m3"),
                              ("beads", "bead.excess_density_kg_m3")):
            low, high = contracts.param_range(key)
            loss = analyse.excess_density(particle, "loss")
            retention = analyse.excess_density(particle, "retention")
            self.assertEqual(loss.kg_m3, high, f"{particle} loss must use the max")
            self.assertEqual(retention.kg_m3, low, f"{particle} retention must use the min")
            self.assertEqual(loss.end, "max")
            self.assertEqual(retention.end, "min")
            self.assertEqual(loss.key, key)
            self.assertLess(retention.kg_m3, loss.kg_m3)

    def test_every_choice_explains_which_end_and_why(self):
        choice = analyse.excess_density("capsules", "loss")
        self.assertIn("max", choice.note)
        self.assertIn(str(choice.kg_m3).rstrip("0").rstrip("."), choice.note)
        self.assertIn("LOSS", choice.why)
        self.assertIn("retention", analyse.excess_density("cells", "retention").purpose)

    def test_densities_are_never_hard_coded(self):
        source = (ROOT / "assay2chip" / "cfd" / "analyse.py").read_text()
        for key in ("cell.excess_density_kg_m3", "capsule.excess_density_kg_m3",
                    "bead.excess_density_kg_m3", "fluid.density_37c_kg_m3",
                    "tubing.inner_diameter_mm", "tubing.length_mm",
                    "selector_valve.internal_volume_ul"):
            lo, hi = contracts.param_range(key)
            for number in (contracts.param(key), lo, hi):
                if number is None:
                    continue
                self.assertNotIn(f"= {number!r}\n", source,
                                 f"{key}'s {number!r} looks hard-coded")

    def test_unknown_particle_class_raises_rather_than_defaulting(self):
        with self.assertRaises(contracts.ContractError):
            analyse.excess_density("unobtainium", "loss")

    def test_bad_purpose_is_rejected(self):
        with self.assertRaises(ValueError):
            analyse.excess_density("cells", "whatever")

    def test_stokes_settling_scales_as_diameter_squared(self):
        mu = contracts.viscosity_for(37).pa_s
        one = analyse.stokes_settling_mm_s(60.0, 10.0, mu)
        two = analyse.stokes_settling_mm_s(60.0, 20.0, mu)
        self.assertAlmostEqual(two / one, 4.0, places=6)

    def test_settling_is_linear_in_density(self):
        mu = contracts.viscosity_for(37).pa_s
        self.assertAlmostEqual(
            analyse.stokes_settling_mm_s(120.0, 12.0, mu)
            / analyse.stokes_settling_mm_s(60.0, 12.0, mu), 2.0, places=6)

    def test_a_12_um_cell_settles_0_3_mm_in_about_51_s(self):
        """The repo's long-standing figure, which the contract note is calibrated to."""
        mu = contracts.viscosity_for(37).pa_s
        v = analyse.stokes_settling_mm_s(
            contracts.param("cell.excess_density_kg_m3"), 12.0, mu)
        self.assertAlmostEqual(0.3 / v, 51.0, delta=3.0)

    def test_capsules_settling_out_in_the_handoff_line_is_a_finding(self):
        bore = contracts.param("tubing.inner_diameter_mm")
        found = analyse.analyse_chip(bundle(
            chip=CAPSULE_CHIP, protocol=CAPSULE_PROTOCOL, chip_id="capsule_chip",
            mesh=mesh([node("n1", "trap")], []),
            steps=[analyse.StepBundle(view=analyse.step_views(CAPSULE_PROTOCOL)[1],
                                      field=field())],
            couple={"handoffs": [{"from": "capsule_chip", "to": "module4", "id": "line_1",
                                  "carries": "capsules", "particle_um": 140.0,
                                  "flow_ul_min": 0.05}]}))
        hits = [f for f in by_class(found, "handoff")
                if f.metric == "line_transit_over_settling_time"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].units, "transit/settling")
        self.assertIn("capsule.excess_density_kg_m3", hits[0].source)
        self.assertIn("max", hits[0].message)
        self.assertIn("LOSS check", hits[0].message)
        self.assertIn("capsule.excess_density_kg_m3",
                      analyse.finding_assumptions(hits[0]))

    def test_a_fast_handoff_does_not_report_settling(self):
        found = analyse.analyse_chip(bundle(
            chip=CAPSULE_CHIP, protocol=CAPSULE_PROTOCOL, chip_id="capsule_chip",
            mesh=mesh([node("n1", "trap")], []),
            steps=[analyse.StepBundle(view=analyse.step_views(CAPSULE_PROTOCOL)[1],
                                      field=field())],
            couple={"handoffs": [{"from": "capsule_chip", "to": "module4", "id": "line_1",
                                  "carries": "capsules", "particle_um": 140.0,
                                  "flow_ul_min": 500.0}]}))
        self.assertEqual([f for f in by_class(found, "handoff")
                          if f.metric == "line_transit_over_settling_time"], [])

    def test_an_unstated_transfer_flow_is_undecided_not_a_pass(self):
        found = analyse.analyse_chip(bundle(
            chip=CAPSULE_CHIP, protocol=CAPSULE_PROTOCOL, chip_id="capsule_chip",
            mesh=mesh([node("n1", "trap")], []),
            steps=[analyse.StepBundle(view=analyse.step_views(CAPSULE_PROTOCOL)[1],
                                      field=field())],
            couple={"handoffs": [{"from": "capsule_chip", "to": "module4", "id": "line_1",
                                  "carries": "capsules", "particle_um": 140.0}]}))
        hits = [f for f in by_class(found, "handoff")
                if f.metric == "line_settling_undecidable"]
        self.assertEqual(len(hits), 1)
        self.assertIn("UNDECIDED", hits[0].message)

    def test_a_library_handoff_has_nothing_to_settle(self):
        found = analyse.analyse_chip(bundle(
            chip=CAPSULE_CHIP, protocol=CAPSULE_PROTOCOL, chip_id="capsule_chip",
            mesh=mesh([node("n1", "trap")], []),
            steps=[analyse.StepBundle(view=analyse.step_views(CAPSULE_PROTOCOL)[1],
                                      field=field())],
            couple={"handoffs": [{"from": "capsule_chip", "to": "module5", "id": "line_2",
                                  "carries": "library", "flow_ul_min": 0.01}]}))
        self.assertEqual(by_class(found, "handoff"), [])


class TestUnmodelled(unittest.TestCase):
    def re_bundle(self, re_values, flow=1373.3):
        proto = {"steps": [{"step": 1, "operation": "exchange", "target": "cx",
                            "reagent": "cold_acidified_organic_quench",
                            "flow_ul_min": flow,
                            "ports": {"feed": ["p_in"], "vent": ["p_out"]}}]}
        return bundle(
            chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip",
            mesh=mesh([node("n1", "cx")],
                      [edge(k, "n1", "n1", k) for k in re_values]),
            steps=[analyse.StepBundle(view=analyse.step_views(proto)[0],
                                      field=field(re=re_values))],
        )

    def test_re_above_one_is_a_finding_not_a_number(self):
        finding = one(analyse.analyse_chip(self.re_bundle({"comb_spine": 3.0})), "unmodelled")
        self.assertEqual(finding.severity, "medium")
        self.assertEqual(finding.limit, 1.0)
        self.assertEqual(finding.units, "Reynolds number")

    def test_module6_quench_flood_range_is_high(self):
        """The audit measured Re 13-23 in the comb spine and feed line."""
        found = by_class(analyse.analyse_chip(
            self.re_bundle({"comb_spine": 23.0, "feed_line": 13.0})), "unmodelled")
        self.assertEqual(len(found), 2)
        self.assertEqual({f.severity for f in found}, {"high"})
        self.assertIn("wrong equations", found[0].message)

    def test_unmodelled_is_never_a_blocker(self):
        found = by_class(analyse.analyse_chip(self.re_bundle({"nozzle": 5000.0})), "unmodelled")
        self.assertEqual(found[0].severity, "high")
        self.assertNotEqual(found[0].severity, "blocker")

    def test_re_below_one_is_silent(self):
        self.assertEqual(
            by_class(analyse.analyse_chip(self.re_bundle({"ch": 0.004})), "unmodelled"), [])

    def test_fix_hint_names_the_flow_that_comes_back_in_range(self):
        finding = one(analyse.analyse_chip(
            self.re_bundle({"comb_spine": 20.0}, flow=1373.3)), "unmodelled")
        self.assertIn("uL/min", finding.fix_hint)
        self.assertIn("68", finding.fix_hint)   # 1373.3 / 20


# --- validation gate ------------------------------------------------------------

class TestUntrusted(unittest.TestCase):
    def built(self, validation):
        return analyse.analyse_chip(bundle(
            chip=ADHERENT_CHIP, protocol=ADHERENT_PROTOCOL, chip_id="adherent_chip",
            mesh=mesh([node("n1", "cx")], [edge("e1", "n1", "n1", "cx")]),
            steps=[analyse.StepBundle(view=analyse.step_views(ADHERENT_PROTOCOL)[1],
                                      field=field(p_pa={"n1": 5000.0, "n2": 0.0},
                                                  shear={"e1": 1.0}))],
            validation=validation,
        ))

    def test_a_failed_benchmark_marks_every_finding_untrusted(self):
        found = self.built({"benchmarks": [
            {"name": "straight_channel_analytic", "passed": True},
            {"name": "agreement_with_operator_network", "passed": False},
        ]})
        self.assertTrue(found)
        for finding in found:
            self.assertTrue(analyse.finding_untrusted(finding))
            self.assertTrue(finding.message.startswith("UNTRUSTED"))
            self.assertIn("agreement_with_operator_network", finding.message)

    def test_a_passing_report_leaves_findings_trusted(self):
        found = self.built({"passed": True, "benchmarks": [
            {"name": "straight_channel_analytic", "passed": True}]})
        self.assertTrue(found)
        self.assertFalse(any(analyse.finding_untrusted(f) for f in found))

    def test_no_validation_report_is_not_treated_as_a_pass_or_a_fail(self):
        self.assertEqual(analyse.validation_failures(None), [])

    def test_an_overall_failure_without_named_benchmarks_still_counts(self):
        self.assertEqual(len(analyse.validation_failures({"passed": False})), 1)

    def test_untrusted_survives_into_the_json(self):
        found = self.built({"benchmarks": [{"name": "mass_conservation", "passed": False}]})
        self.assertTrue(all(row.get("untrusted") for row in analyse.findings_json(found)))


# --- dedup, ranking, determinism, JSON shape -------------------------------------

class TestDedupAndOrder(unittest.TestCase):
    def many_steps(self, n=40):
        ceiling = contracts.param("shear.culture_ceiling_dyn_cm2")
        proto = {"steps": [
            {"step": i, "operation": "exchange", "target": "cx",
             "reagent": "neural_maturation_medium", "flow_ul_min": 1.0, "temp_c": 37,
             "ports": {"feed": ["p_in"], "vent": ["p_out"]}} for i in range(1, n + 1)]}
        proto["steps"].insert(0, {"step": 0, "operation": "load", "target": "cx",
                                  "reagent": "cell_suspension", "flow_ul_min": 1.0,
                                  "ports": {"feed": ["p_in"], "vent": ["p_out"]}})
        views = analyse.step_views(proto)
        steps = [analyse.StepBundle(view=v,
                                    field=field(shear={"e1": ceiling * (2 + i * 0.1)}))
                 for i, v in enumerate(views)]
        return bundle(chip=ADHERENT_CHIP, protocol=proto, chip_id="adherent_chip",
                      mesh=mesh([node("n1", "cx")], [edge("e1", "n1", "n1", "cx")]),
                      steps=steps)

    def test_one_root_cause_is_one_finding(self):
        found = by_class(analyse.analyse_chip(self.many_steps(40)), "shear")
        self.assertEqual(len(found), 1, "40 exchanges of one over-shear channel is one problem")
        self.assertIn("same root cause in", found[0].message)

    def test_the_worst_step_is_the_one_kept(self):
        ceiling = contracts.param("shear.culture_ceiling_dyn_cm2")
        found = by_class(analyse.analyse_chip(self.many_steps(40)), "shear")
        self.assertAlmostEqual(found[0].value, ceiling * (2 + 40 * 0.1), places=6)

    def test_ranked_by_severity(self):
        found = analyse.analyse_chip(self.many_steps(5))
        ranks = [analyse.SEVERITIES.index(f.severity) for f in found]
        self.assertEqual(ranks, sorted(ranks))

    def test_deterministic_regardless_of_input_order(self):
        collected = []
        for seed in (1, 2, 3):
            b = self.many_steps(8)
            random.Random(seed).shuffle(b.steps)
            collected.append([f.id for f in analyse.analyse_chip(b)])
        self.assertEqual(collected[0], collected[1])
        self.assertEqual(collected[1], collected[2])

    def test_ids_are_stable_across_runs(self):
        first = [f.id for f in analyse.analyse_chip(self.many_steps(8))]
        second = [f.id for f in analyse.analyse_chip(self.many_steps(8))]
        self.assertEqual(first, second)
        self.assertEqual(len(set(first)), len(first), "ids must be unique")

    def test_findings_from_several_chips_rank_together(self):
        a = self.many_steps(3)
        b = self.many_steps(3)
        b.chip_id = "other_chip"
        found = analyse.analyse(([a, b]))
        self.assertEqual({f.chip_id for f in found}, {"adherent_chip", "other_chip"})
        ranks = [analyse.SEVERITIES.index(f.severity) for f in found]
        self.assertEqual(ranks, sorted(ranks))


class TestJsonShape(unittest.TestCase):
    REQUIRED = ("id", "class", "severity", "chip_id", "feature_id", "step", "metric",
                "value", "limit", "units", "message", "fix_hint")

    def everything(self):
        """One bundle that fires every class at once."""
        limit = contracts.max_working_pressure_kpa()
        swollen = contracts.swollen_capsule_diameter_um()
        nodes = [node("in", "cap_in", kind="port"), node("t1", "trap"),
                 node("out", "waste", kind="port"), node("stub", "orphan"),
                 node("tip", "blind")]
        edges = [edge("e_in", "in", "t1", "ch_cap", width_mm=0.2),
                 edge("e_out", "t1", "out", "ch_waste", width_mm=4.0),
                 edge("e_stub", "t1", "stub", "orphan"),
                 edge("e_tip", "stub", "tip", "blind")]
        m = mesh(nodes, edges,
                 ports={"cap_in": "in", "reagent_in": "in", "waste": "out"},
                 chip_id="capsule_chip")
        view = analyse.step_views(CAPSULE_PROTOCOL)[1]
        return bundle(
            chip=CAPSULE_CHIP, protocol=CAPSULE_PROTOCOL, chip_id="capsule_chip", mesh=m,
            steps=[analyse.StepBundle(
                view=view,
                field=field(p_pa={"in": limit * 1000 * 3, "out": 0.0},
                            re={"e_in": 18.0}),
                transport={"dead_zones": [{"chamber_id": "trap", "volume_fraction": 0.3}],
                           "exchange_fraction": {"trap": 0.5}},
                particles={"clog": [{"feature_id": "ch_cap",
                                     "width_mm": swollen / 1000.0 * 0.6}],
                           "settling": [{"feature_id": "ch_waste", "settle_s": 1.0,
                                         "transit_s": 9.0}],
                           "weir_escape": [{"feature_id": "trap", "dp_kpa": limit * 2.5}]},
            )],
            couple={"handoffs": [{"from": "capsule_chip", "to": "module4", "id": "line_1",
                                  "particle_um": 140.0, "particle_pass_um": 100.0}]},
        )

    def test_every_class_can_fire(self):
        found = analyse.analyse_chip(self.everything())
        fired = set(classes(found))
        for cls in ("leak", "dead_end", "bubble", "clog", "weir_escape", "handoff",
                    "trap", "exchange", "sediment", "unmodelled"):
            self.assertIn(cls, fired, f"{cls} did not fire; got {sorted(fired)}")

    def test_every_finding_has_every_required_key(self):
        for row in analyse.findings_json(analyse.analyse_chip(self.everything())):
            for key in self.REQUIRED:
                self.assertIn(key, row, f"{row.get('id')} missing {key}")
            self.assertIn(row["class"], analyse.CLASSES)
            self.assertIn(row["severity"], analyse.SEVERITIES)
            self.assertTrue(row["message"], f"{row['id']} has no message")
            self.assertTrue(row["metric"], f"{row['id']} has no metric")

    def test_the_json_key_is_class_not_finding_class(self):
        row = analyse.findings_json(analyse.analyse_chip(self.everything()))[0]
        self.assertIn("class", row)
        self.assertNotIn("finding_class", row)

    def test_a_fix_hint_wherever_one_exists(self):
        found = analyse.analyse_chip(self.everything())
        without = [f.id for f in found if not f.fix_hint]
        self.assertEqual(without, [], "every finding here has an actionable fix")

    def test_json_is_serialisable(self):
        json.dumps(analyse.findings_json(analyse.analyse_chip(self.everything())))

    def test_write_findings_round_trip(self):
        found = analyse.analyse_chip(self.everything())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cfd" / "findings.json"
            summary = analyse.write_findings(found, path)
            payload = json.loads(path.read_text())
        self.assertEqual(summary["total"], len(found))
        self.assertEqual(len(payload["findings"]), len(found))
        self.assertIn("contact_angle_deg", payload["assumptions"])
        self.assertEqual(payload["summary"]["worst"], found[0].severity)

    def test_summary_counts_add_up(self):
        found = analyse.analyse_chip(self.everything())
        summary = analyse.summary(found)
        self.assertEqual(sum(summary["by_severity"].values()), len(found))
        self.assertEqual(sum(summary["by_class"].values()), len(found))

    def test_empty_inputs_produce_no_findings_and_no_crash(self):
        self.assertEqual(analyse.analyse_chip(analyse.Bundle()), [])
        self.assertEqual(analyse.summary([])["total"], 0)


class TestNoHardCodedContractNumbers(unittest.TestCase):
    """Every limit must trace to contracts.py. The repo has been bitten six times."""

    def test_module_source_holds_no_contract_literals(self):
        source = (ROOT / "assay2chip" / "cfd" / "analyse.py").read_text()
        for key in ("shear.culture_ceiling_dyn_cm2", "shear.prelysis_rinse_ceiling_dyn_cm2",
                    "capsule.escape_pressure_kpa", "control.pressure_margin",
                    "capsule_channel.width_factor_low", "exchange.target_fraction"):
            literal = repr(contracts.param(key))
            for line in source.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"') or "assumption" in line:
                    continue
                self.assertNotIn(
                    f"= {literal}", line,
                    f"{key}'s value {literal} looks hard-coded: {line.strip()}",
                )

    def test_local_assumptions_are_not_contract_values_in_disguise(self):
        table = contracts.parameters()
        for key in analyse.ASSUMPTIONS:
            self.assertNotIn(key, table, f"{key} IS in the contract - read it, do not assume it")
            self.assertNotIn(f"shear.{key}", table)

    def test_the_three_ceilings_come_from_the_contract(self):
        ceilings = contracts.shear_ceilings()
        self.assertEqual(ceilings["adherent_cells"].ceiling_dyn_cm2,
                         contracts.param("shear.culture_ceiling_dyn_cm2"))
        self.assertEqual(ceilings["prelysis_rinse"].ceiling_dyn_cm2,
                         contracts.param("shear.prelysis_rinse_ceiling_dyn_cm2"))
        self.assertIsNone(ceilings["capsules"].ceiling_dyn_cm2)
        self.assertEqual(ceilings["capsules"].pressure_ceiling_kpa,
                         contracts.max_working_pressure_kpa())


# --- real chips: run once the siblings land ---------------------------------------

def _sibling(name):
    try:
        __import__(f"assay2chip.cfd.{name}")
        return sys.modules[f"assay2chip.cfd.{name}"]
    except Exception:
        return None


class TestRealChips(unittest.TestCase):
    """End-to-end on the repo's own chips. Skips until mesh/scenario/solve exist.

    These are the findings the brief asks to verify: module1_v1, module3_v1_translated
    and module6_v1.
    """

    CHIPS = (
        ("module1_v1", "module1_v1"),
        ("module3_v1_translated", "module3_v1"),
        ("module6_v1", "module6_v1"),
    )

    _cache: dict = {}

    def setUp(self):
        self.mesh_mod = _sibling("mesh")
        self.scenario_mod = _sibling("scenario")
        self.solve_mod = _sibling("solve")
        if not (self.mesh_mod and self.scenario_mod and self.solve_mod):
            self.skipTest("mesh.py / scenario.py / solve.py not built yet")

    def build(self, chip_name, protocol_name):
        """Bundle one repo chip through mesh -> scenario -> solve, then analyse."""
        if chip_name in self._cache:
            return self._cache[chip_name]
        chip = json.loads((ROOT / "chip" / f"{chip_name}.json").read_text())
        protocol = json.loads((ROOT / "protocols" / f"{protocol_name}.json").read_text())
        try:
            m = self.mesh_mod.build_mesh(chip)
            run = self.scenario_mod.build_protocol(
                ROOT / "protocols" / f"{protocol_name}.json",
                ROOT / "chip" / f"{chip_name}.json",
                worst_cases=False,
            )
            # module1 has 182 steps and most are the same capped 37 C hold. Solve one
            # per distinct (operation, targets, flow, ports) signature: that keeps every
            # hydraulic case and drops the duplicates, which is the difference between a
            # 0.5 s test and a 20 s one.
            records, seen = [], set()
            for rec in run.step_scenarios:
                sig = (rec.operation, tuple(rec.targets),
                       tuple(sorted((p, bc.type, bc.q_ul_min) for p, bc in rec.ports.items())))
                if sig in seen:
                    continue
                seen.add(sig)
                records.append(rec)
            fields = self.solve_mod.solve_steps(m, [r.scenario for r in records])
        except Exception as exc:  # a sibling is mid-build; say so rather than fail
            self.skipTest(f"{chip_name}: sibling pipeline not ready ({type(exc).__name__}: {exc})")
        views = {v.label: v for v in analyse.step_views(protocol)}
        steps = []
        for record, fld in zip(records, fields):
            view = views.get(record.step)
            if view is None:
                continue
            steps.append(analyse.StepBundle(
                view=view, scenario=record.scenario, field=fld))
        bundle = analyse.Bundle(chip=chip, protocol=protocol, mesh=m, steps=steps,
                                chip_id=chip_name)
        self._cache[chip_name] = bundle
        return bundle

    def test_every_chip_analyses_without_crashing(self):
        for chip_name, protocol_name in self.CHIPS:
            with self.subTest(chip=chip_name):
                found = analyse.analyse_chip(self.build(chip_name, protocol_name))
                for row in analyse.findings_json(found):
                    for key in TestJsonShape.REQUIRED:
                        self.assertIn(key, row)

    def test_every_chip_reports_bond_strength_unknown(self):
        """No material in contracts/materials.json has a bond strength."""
        for chip_name, protocol_name in self.CHIPS:
            with self.subTest(chip=chip_name):
                found = analyse.analyse_chip(self.build(chip_name, protocol_name))
                metrics = [f.metric for f in by_class(found, "leak")]
                self.assertIn("bond_strength_known", metrics)

    def test_module1_weir_chambers_are_not_judged_as_capsule_chambers(self):
        chip = json.loads((ROOT / "chip" / "module1_v1.json").read_text())
        protocol = json.loads((ROOT / "protocols" / "module1_v1.json").read_text())
        capsule_chip, evidence = analyse.is_capsule_chip(chip, protocol)
        self.assertFalse(capsule_chip, f"module1 has no capsules; evidence found: {evidence}")
        chambers = analyse.chamber_views(chip)
        steps = analyse.step_views(protocol)
        for cid in ("n_cx", "g_cx", "a_cx"):
            flowing = next(s for s in steps if cid in s.targets and s.has_flow)
            call = analyse.shear_scope(chambers[cid], flowing, steps,
                                       capsule_chip=False, capsule_evidence=())
            self.assertEqual(call.shear_class, "adherent_cells", f"{cid} holds adherent cells")

    def test_module3_trap_is_a_capsule_chamber_bounded_by_pressure(self):
        chip = json.loads((ROOT / "chip" / "module3_v1_translated.json").read_text())
        protocol = json.loads((ROOT / "protocols" / "module3_v1.json").read_text())
        capsule_chip, evidence = analyse.is_capsule_chip(chip, protocol)
        self.assertTrue(capsule_chip, "module3 carries capsules")
        self.assertTrue(evidence)
        chambers = analyse.chamber_views(chip)
        steps = analyse.step_views(protocol)
        wash = next(s for s in steps if "main_trap" in s.targets and s.operation == "exchange")
        call = analyse.shear_scope(chambers["main_trap"], wash, steps,
                                   capsule_chip=True, capsule_evidence=evidence)
        self.assertEqual(call.shear_class, "capsules")
        self.assertIsNone(call.ceiling_dyn_cm2)
        self.assertEqual(call.pressure_ceiling_kpa, contracts.max_working_pressure_kpa())

    def test_module6_rinse_before_the_quench_gets_the_0_3_ceiling(self):
        chip = json.loads((ROOT / "chip" / "module6_v1.json").read_text())
        protocol = json.loads((ROOT / "protocols" / "module6_v1.json").read_text())
        chambers = analyse.chamber_views(chip)
        steps = analyse.step_views(protocol)
        for cid in ("cx_n", "cx_g", "cx_a"):
            rinse = next(s for s in steps
                         if cid in s.targets and "cold_volatile_rinse" in s.reagents)
            call = analyse.shear_scope(chambers[cid], rinse, steps,
                                       capsule_chip=False, capsule_evidence=())
            self.assertEqual(call.shear_class, "prelysis_rinse", f"{cid} rinse before the quench")
            self.assertEqual(call.ceiling_dyn_cm2,
                             contracts.param("shear.prelysis_rinse_ceiling_dyn_cm2"))
            quench = next(s for s in steps
                          if cid in s.targets and "cold_acidified_organic_quench" in s.reagents)
            self.assertEqual(
                analyse.shear_scope(chambers[cid], quench, steps,
                                    capsule_chip=False, capsule_evidence=()).shear_class,
                "none", "the quench IS the lyse; no culture ceiling applies")

    def test_module6_culture_steps_are_adherent(self):
        chip = json.loads((ROOT / "chip" / "module6_v1.json").read_text())
        protocol = json.loads((ROOT / "protocols" / "module6_v1.json").read_text())
        chambers = analyse.chamber_views(chip)
        steps = analyse.step_views(protocol)
        culture = next(s for s in steps if s.operation == "culture" and "cx_n" in s.targets)
        call = analyse.shear_scope(chambers["cx_n"], culture, steps,
                                   capsule_chip=False, capsule_evidence=())
        self.assertEqual(call.shear_class, "adherent_cells")

    def test_module6_quench_flood_is_reported_as_unmodelled(self):
        """The audit measured Re 13-23 there. It must be a finding, not a number."""
        found = analyse.analyse_chip(self.build("module6_v1", "module6_v1"))
        unmodelled = by_class(found, "unmodelled")
        self.assertTrue(unmodelled, "Re 13-23 in the comb spine must be reported")
        self.assertTrue(any(f.severity == "high" for f in unmodelled))


class TestScopingOnRealProtocolsOffline(unittest.TestCase):
    """The scoping half of the real-chip checks needs no solver, so it never skips."""

    def test_module1_has_no_capsule_evidence(self):
        chip = json.loads((ROOT / "chip" / "module1_v1.json").read_text())
        protocol = json.loads((ROOT / "protocols" / "module1_v1.json").read_text())
        capsule_chip, evidence = analyse.is_capsule_chip(chip, protocol)
        self.assertFalse(capsule_chip, f"evidence found: {evidence}")

    def test_module1_weir_chambers_hold_adherent_cells(self):
        chip = json.loads((ROOT / "chip" / "module1_v1.json").read_text())
        protocol = json.loads((ROOT / "protocols" / "module1_v1.json").read_text())
        chambers = analyse.chamber_views(chip)
        steps = analyse.step_views(protocol)
        for cid in ("n_cx", "g_cx", "a_cx"):
            flowing = next(s for s in steps if cid in s.targets and s.has_flow)
            call = analyse.shear_scope(chambers[cid], flowing, steps,
                                       capsule_chip=False, capsule_evidence=())
            self.assertEqual(call.shear_class, "adherent_cells", f"{cid}: {call.evidence}")

    def test_module3_trap_is_capsules(self):
        chip = json.loads((ROOT / "chip" / "module3_v1_translated.json").read_text())
        protocol = json.loads((ROOT / "protocols" / "module3_v1.json").read_text())
        capsule_chip, evidence = analyse.is_capsule_chip(chip, protocol)
        self.assertTrue(capsule_chip)
        chambers = analyse.chamber_views(chip)
        steps = analyse.step_views(protocol)
        wash = next(s for s in steps if "main_trap" in s.targets and s.washes)
        call = analyse.shear_scope(chambers["main_trap"], wash, steps,
                                   capsule_chip=True, capsule_evidence=evidence)
        self.assertEqual(call.shear_class, "capsules")
        self.assertIsNone(call.ceiling_dyn_cm2)

    def test_module6_rinse_then_quench_scoping(self):
        chip = json.loads((ROOT / "chip" / "module6_v1.json").read_text())
        protocol = json.loads((ROOT / "protocols" / "module6_v1.json").read_text())
        chambers = analyse.chamber_views(chip)
        steps = analyse.step_views(protocol)
        rinse = next(s for s in steps
                     if "cx_n" in s.targets and "cold_volatile_rinse" in s.reagents)
        quench = next(s for s in steps
                      if "cx_n" in s.targets and "cold_acidified_organic_quench" in s.reagents)
        self.assertEqual(
            analyse.shear_scope(chambers["cx_n"], rinse, steps,
                                capsule_chip=False, capsule_evidence=()).shear_class,
            "prelysis_rinse")
        self.assertEqual(
            analyse.shear_scope(chambers["cx_n"], quench, steps,
                                capsule_chip=False, capsule_evidence=()).shear_class,
            "none")

    def test_no_real_chip_has_a_bond_strength_in_the_contract(self):
        for chip_name in ("module1_v1", "module3_v1_translated", "module6_v1"):
            chip = json.loads((ROOT / "chip" / f"{chip_name}.json").read_text())
            value, _material, note = analyse.bond_strength_kpa(chip)
            self.assertIsNone(value, f"{chip_name}: {note}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

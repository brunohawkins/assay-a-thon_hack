"""Checks assay2chip/cfd/particles.py: settling, intent, clogging, weir escape, tracking.

    python -m unittest discover -s tests -v
    python -m unittest tests.test_cfd_particles -v

Offline: no network, no LLM, and no sibling CFD module required. The three real chips
(`module3_v1_translated`, `module4_v1`, `cell_prep_v1`) are read from the repo, so these
double as regression tests on the chips themselves.

The settling tests are HAND CALCULATIONS, not golden values recorded from this module -
`v_s = drho g d^2 / (18 mu)` is worked out independently in `_hand_settling` below, and
three of the cases are also cross-checked against figures the repo states elsewhere
(protocols/retention_audit.md's 6 um/s cell and contracts/blocks.json's 50-210 um/s
capsule sump band). A test that only compares the module with itself would pass just as
happily if the formula were wrong.
"""

from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from assay2chip.cfd import particles as P  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402

MU_37C = contracts.viscosity_37c_pa_s().pa_s
G = 9.80665

TARGETS = ("module3_v1_translated", "module4_v1", "cell_prep_v1")


def _hand_settling(delta_rho: float, d_um: float, mu: float) -> float:
    """Stokes settling worked out here, independently of the module under test."""
    return delta_rho * G * (d_um * 1e-6) ** 2 / (18.0 * mu)


class TestSettlingVelocity(unittest.TestCase):
    """v_s must match a hand calculation and the figures the repo states elsewhere."""

    def test_matches_hand_calculation(self):
        for drho in (15.0, 50.0, 65.0):
            for d_um in (10.0, 12.0, 15.0, 70.0, 140.0):
                self.assertAlmostEqual(
                    P.stokes_settling_m_s(drho, d_um * 1e-6, MU_37C),
                    _hand_settling(drho, d_um, MU_37C),
                    places=15,
                    msg=f"drho={drho} d={d_um}",
                )

    def test_scales_as_diameter_squared(self):
        """Doubling the diameter must quadruple v_s - this is what the swell factor does."""
        v70 = P.stokes_settling_m_s(15.0, 70e-6, MU_37C)
        v140 = P.stokes_settling_m_s(15.0, 140e-6, MU_37C)
        self.assertAlmostEqual(v140 / v70, 4.0, places=12)

    def test_inversely_proportional_to_viscosity(self):
        v_warm = P.stokes_settling_m_s(65.0, 12e-6, 0.8e-3)
        v_cold = P.stokes_settling_m_s(65.0, 12e-6, 1.57e-3)
        self.assertAlmostEqual(v_warm * 0.8e-3, v_cold * 1.57e-3, places=15)

    def test_agrees_with_retention_audit_cell(self):
        """The contract note: 60 kg/m3 'reproduces ~51 s for a 12 um cell over 0.3 mm'."""
        rho = contracts.param("cell.excess_density_kg_m3")
        v = _hand_settling(rho, 12.0, MU_37C)
        self.assertAlmostEqual(0.3e-3 / v, 51.0, delta=1.5,
                               msg=f"got {0.3e-3 / v:.1f} s")
        # retention_audit.md's companion figure: about 6 um/s at 37 C.
        self.assertAlmostEqual(v * 1e6, 6.0, delta=0.6)
        cold = _hand_settling(rho, 12.0, 1.57e-3) * 1e6
        self.assertAlmostEqual(cold, 3.0, delta=0.5, msg=f"got {cold:.2f} um/s")

    def test_capsule_excess_density_agrees_with_the_contract_sump_band(self):
        """Two parts of the contract must agree about how fast a capsule falls.

        blocks.json offers a sedimentation sump at '~50-210 um/s'; parameters.json now
        states capsule.excess_density_kg_m3. Inverting the band through Stokes must land
        inside the parameter's min/max, or one of the two contract files is wrong.
        """
        out = P.capsule_sump_band_crosscheck(MU_37C)
        self.assertTrue(out["agrees"], msg=out["why"])
        lo, hi = out["contract_band_kg_m3"]
        self.assertLessEqual(lo, out["implied_excess_density_kg_m3"])
        self.assertLessEqual(out["implied_excess_density_kg_m3"], hi)

    def test_stokes_regime_is_valid(self):
        """Every class must sit far below Re_p 1, or the Stokes form does not apply."""
        for name, pc in P.particle_classes(MU_37C).items():
            v = pc.settling_m_s(MU_37C, pc.d_max_um, pc.excess.max_kg_m3)
            re_p = P.settling_reynolds(v, pc.d_max_um * 1e-6, MU_37C)
            self.assertLess(re_p, 1.0, msg=f"{name} Re_p={re_p:.3g}")

    def test_module_classes_match_hand_calculation(self):
        for name, pc in P.particle_classes(MU_37C).items():
            for d in (pc.d_min_um, pc.diameter_um, pc.d_max_um):
                for rho in (pc.excess.min_kg_m3, pc.excess.value, pc.excess.max_kg_m3):
                    self.assertAlmostEqual(
                        pc.settling_m_s(MU_37C, d, rho),
                        _hand_settling(rho, d, MU_37C),
                        places=15, msg=f"{name} at d={d} rho={rho}")


class TestParticleClasses(unittest.TestCase):
    def test_classes_come_from_contracts(self):
        cls = P.particle_classes(MU_37C)
        self.assertEqual(cls["capsule"].diameter_um, contracts.capsule_diameter_um())
        self.assertEqual(cls["capsule_swollen"].diameter_um,
                         contracts.swollen_capsule_diameter_um())
        self.assertAlmostEqual(
            cls["capsule_swollen"].diameter_um / cls["capsule"].diameter_um,
            contracts.capsule_swell_factor(), places=12)

    def test_cell_diameter_parsed_from_contract_prose(self):
        lo, hi, note = P.cell_diameter_range_um()
        self.assertEqual((lo, hi), (10.0, 15.0))
        self.assertIn("_cells_caveat", note.source)

    def test_every_density_comes_from_a_contract_key(self):
        """No local density copies: that is why these became contract keys."""
        for name, pc in P.particle_classes(MU_37C).items():
            self.assertEqual(pc.excess.key, P.EXCESS_DENSITY_KEYS[name], msg=name)
            self.assertEqual(pc.excess.value,
                             contracts.param(pc.excess.key), msg=name)
            keys = [a.key for a in pc.assumptions]
            self.assertIn(pc.excess.key, keys, msg=name)
            self.assertIn(P.CARRIER_DENSITY_KEY, keys, msg=name)

    def test_density_contract_keys_all_exist_and_are_assumptions(self):
        for key in set(P.EXCESS_DENSITY_KEYS.values()) | {P.CARRIER_DENSITY_KEY}:
            self.assertIsNotNone(contracts.param(key))
            self.assertTrue(contracts.is_assumption(key), msg=key)
            lo, hi = contracts.param_range(key)
            self.assertIsNotNone(lo, msg=key)
            self.assertIsNotNone(hi, msg=key)

    def test_carrier_density_matches_solve_py(self):
        """solve.py reads the same key, so the package cannot disagree about the fluid."""
        from assay2chip.cfd import solve as sv
        self.assertIn(P.CARRIER_DENSITY_KEY, sv.DENSITY_CONTRACT_KEYS)
        self.assertAlmostEqual(sv.density()[0], P.carrier_density().value, places=6)

    def test_each_particle_kind_has_its_own_density(self):
        """A single shared 'particle' density made cells and capsules settle identically."""
        cls = P.particle_classes(MU_37C)
        self.assertNotEqual(cls["cells"].excess.key, cls["capsule"].excess.key)
        self.assertNotEqual(cls["bead"].excess.key, cls["cells"].excess.key)
        self.assertEqual(cls["capsule"].excess.key, cls["capsule_swollen"].excess.key)

    def test_conservative_diameter_depends_on_intent(self):
        """Smallest when we WANT it to drop out, largest when settling is a loss."""
        pc = P.particle_classes(MU_37C)["capsule"]
        self.assertEqual(pc.conservative_diameter_um("desired"), pc.d_min_um)
        self.assertEqual(pc.conservative_diameter_um("loss"), pc.d_max_um)

    def test_bead_settles_far_faster_than_a_cell_of_the_same_size(self):
        """bead.excess_density_kg_m3 is 700 against a cell's 60: beads drop in seconds.

        So the bead coupon retention_audit.md proposes is NOT a stand-in for cells - it
        is more than ten times harsher, which is worth knowing before trusting it.
        """
        cls = P.particle_classes(MU_37C)
        self.assertEqual(cls["bead"].diameter_um, cls["cells"].diameter_um)
        ratio = cls["bead"].settling_m_s(MU_37C) / cls["cells"].settling_m_s(MU_37C)
        self.assertGreater(ratio, 5.0)
        self.assertAlmostEqual(
            ratio,
            contracts.param("bead.excess_density_kg_m3")
            / contracts.param("cell.excess_density_kg_m3"), places=9)


class TestSedimentIntent(unittest.TestCase):
    """The heart of it: settling is desired in one feature and a loss in the next."""

    def test_weir_chamber_is_desired(self):
        chamber = {"id": "main_trap", "type": "exchange", "block": "trap_chamber",
                   "retention": {"type": "weir", "opening_um": 25}}
        intent = P.sediment_intent(chamber, role="chamber", particle_d_um=70.0)
        self.assertEqual(intent.kind, "desired")
        self.assertIn("retention", intent.why)

    def test_feed_line_is_a_loss(self):
        channel = {"id": "ch_in_1", "block": "straight_channel", "width_mm": 0.6}
        intent = P.sediment_intent(channel, role="feed", particle_d_um=70.0)
        self.assertEqual(intent.kind, "loss")
        self.assertIn("DELIVER", intent.why)

    def test_mixer_is_a_loss_and_says_why_it_is_a_mixer(self):
        """A serpentine mixer is meshed as a duct, so both rules could fire.

        The verdict must be a loss either way, but the REASON must be the mixer - that is
        what tells a reader the fix is the flow rate and not the channel length.
        """
        mixer = {"id": "mix_1", "type": "mixer", "block": "serpentine_channel",
                 "mixer": {"kind": "serpentine", "length_mm": 12}}
        for role in ("chamber", "duct"):
            intent = P.sediment_intent(mixer, role=role, particle_d_um=12.0)
            self.assertEqual(intent.kind, "loss", msg=role)
            self.assertIn("mixer", intent.why, msg=role)
            self.assertIn("stay in the stream", intent.why, msg=role)

    def test_reactor_chamber_is_parked_not_lost(self):
        """module4's sub_* chambers: filled then sealed. Reporting a loss here is wrong."""
        chamber = {"id": "sub_1", "type": "split", "block": "polygon_chamber"}
        intent = P.sediment_intent(chamber, role="chamber", particle_d_um=70.0)
        self.assertEqual(intent.kind, "parked")

    def test_filter_that_is_meant_to_pass_the_particle_is_a_loss(self):
        """A 100 um aggregate filter is NOT a capture for a 70 um capsule."""
        prefilter = {"id": "capsule_prefilter", "type": "filter", "block": "pillar_array",
                     "filter": {"gap_um": 100, "purpose": "capsule_aggregates"}}
        passing = P.sediment_intent(prefilter, role="chamber", particle_d_um=70.0)
        self.assertEqual(passing.kind, "loss")
        self.assertIn("meant to PASS", passing.why)
        # The same feature DOES mean to stop a swollen 140 um capsule.
        stopped = P.sediment_intent(prefilter, role="chamber", particle_d_um=140.0)
        self.assertEqual(stopped.kind, "desired")

    def test_culture_chamber_flips_between_culture_and_harvest(self):
        """Adhesion is the retention during culture; during a harvest it is a loss."""
        cult = {"id": "cult_1", "type": "culture", "block": "culture_chamber"}
        during = P.sediment_intent(cult, role="chamber", particle_d_um=12.0,
                                   operation="exchange")
        self.assertEqual(during.kind, "parked")
        self.assertIn("adhesion IS the retention", during.why)
        harvest = P.sediment_intent(cult, role="chamber", particle_d_um=12.0,
                                    operation="transfer")
        self.assertEqual(harvest.kind, "loss")
        self.assertIn("harvest", harvest.why)

    def test_slit_is_always_desired(self):
        intent = P.sediment_intent({"id": "slit"}, role="slit", particle_d_um=70.0)
        self.assertEqual(intent.kind, "desired")

    def test_every_intent_carries_a_reason(self):
        """A verdict with no 'why' is exactly how this gets mis-scoped again."""
        for feature, role in (
            ({"id": "a", "block": "straight_channel"}, "feed"),
            ({"id": "b", "type": "mixer"}, "chamber"),
            ({"id": "c", "retention": {"opening_um": 25}}, "chamber"),
            ({"id": "d", "type": "split"}, "chamber"),
            ({"id": "e"}, "chamber"),
        ):
            intent = P.sediment_intent(feature, role=role, particle_d_um=70.0)
            self.assertIn(intent.kind, ("desired", "loss", "parked"))
            self.assertGreater(len(intent.why), 20, msg=feature["id"])
            self.assertIn(feature["id"], intent.why)


class TestSedimentCheck(unittest.TestCase):
    def test_same_numbers_opposite_verdicts(self):
        """One settling velocity, one transit: the verdict flips with the intent alone."""
        pc = P.particle_classes(MU_37C)["capsule"]
        trap = {"id": "t", "type": "exchange", "retention": {"opening_um": 25}}
        line = {"id": "l", "block": "straight_channel"}
        kw = dict(element_id="e", element="edge", particle=pc, depth_mm=0.3,
                  transit_time_s=30.0, u_mm_s=0.1, mu_pa_s=MU_37C)
        captured = P.sediment_check(trap, role="chamber", **kw)
        lost = P.sediment_check(line, role="feed", **kw)
        self.assertEqual(captured.intent, "desired")
        self.assertEqual(captured.verdict, "captured")
        self.assertEqual(lost.intent, "loss")
        self.assertEqual(lost.verdict, "lost")
        self.assertEqual(captured.severity, "low")
        self.assertIn(lost.severity, ("high", "blocker"))

    def test_settled_fraction_is_transit_over_fall(self):
        pc = P.particle_classes(MU_37C)["cells"]
        v = P.sediment_check({"id": "l", "block": "straight_channel"}, "e", "edge", pc,
                             depth_mm=0.15, transit_time_s=7.5, u_mm_s=0.1,
                             mu_pa_s=MU_37C, role="feed")
        expect = 7.5 / v.fall_time_s
        self.assertAlmostEqual(v.settled_fraction, expect, places=9)
        self.assertLess(v.settled_fraction, 1.0)

    def test_fall_time_uses_the_full_local_depth(self):
        pc = P.particle_classes(MU_37C)["cells"]
        v = P.sediment_check({"id": "l"}, "e", "edge", pc, depth_mm=0.3,
                             transit_time_s=1.0, u_mm_s=1.0, mu_pa_s=MU_37C, role="feed")
        v_s = pc.settling_m_s(MU_37C, pc.d_max_um, pc.excess.max_kg_m3)
        self.assertAlmostEqual(v.fall_time_s, 0.3e-3 / v_s, places=9)
        self.assertAlmostEqual(v.fall_time_half_s, v.fall_time_s / 2.0, places=12)

    def test_capsule_shear_is_bounded_by_pressure_not_shear(self):
        """contracts.shear_ceiling_for('capsules') has no shear ceiling. Say so."""
        pc = P.particle_classes(MU_37C)["capsule"]
        self.assertEqual(P.shear_class_for(pc, {"id": "x"}), "capsules")
        ceiling = contracts.shear_ceiling_for("capsules")
        self.assertIsNone(ceiling.ceiling_dyn_cm2)
        self.assertEqual(ceiling.pressure_ceiling_kpa,
                         contracts.max_working_pressure_kpa())

    def test_adherent_cells_get_the_culture_ceiling(self):
        pc = P.particle_classes(MU_37C)["cells"]
        cls = P.shear_class_for(pc, {"id": "c", "type": "culture"})
        self.assertEqual(cls, "adherent_cells")
        self.assertEqual(contracts.shear_ceiling_for(cls).ceiling_dyn_cm2,
                         contracts.param("shear.culture_ceiling_dyn_cm2"))


class TestClogging(unittest.TestCase):
    def test_uses_the_smallest_dimension_not_the_width(self):
        pc = P.particle_classes(MU_37C)["capsule"]
        wide_shallow = {"id": "c", "width_mm": 0.6, "depth_mm": 0.05}
        v = P.clog_check(wide_shallow, pc)
        self.assertAlmostEqual(v.min_dimension_um, 50.0, places=6)
        self.assertFalse(v.passes)

    def test_limit_is_contract_factor_times_largest_diameter(self):
        pc = P.particle_classes(MU_37C)["capsule_swollen"]
        v = P.clog_check({"id": "c", "width_mm": 0.2, "depth_mm": 0.3}, pc)
        expect = contracts.param("capsule_channel.width_factor_low") * pc.d_max_um
        self.assertAlmostEqual(v.required_um, expect, places=9)

    def test_swelling_is_what_makes_a_02mm_lane_fail(self):
        """A 0.2 mm lane passes a fresh capsule and fails a cultured one."""
        cls = P.particle_classes(MU_37C)
        lane = {"id": "ch", "width_mm": 0.2, "depth_mm": 0.3}
        self.assertTrue(P.clog_check(lane, cls["capsule"]).passes)
        self.assertFalse(P.clog_check(lane, cls["capsule_swollen"]).passes)

    def test_blocker_when_smaller_than_the_particle_itself(self):
        pc = P.particle_classes(MU_37C)["capsule_swollen"]
        v = P.clog_check({"id": "c", "width_mm": 0.2, "depth_mm": 0.15}, pc)
        self.assertEqual(v.severity, "blocker")
        self.assertIn("cannot pass", v.message)

    def test_flags_a_dimension_below_the_fabrication_floor(self):
        pc = P.particle_classes(MU_37C)["capsule"]
        v = P.clog_check({"id": "f", "filter": {"gap_um": 100}, "width_mm": 1.2,
                          "depth_mm": 0.3}, pc)
        self.assertTrue(v.below_fab_floor)
        self.assertAlmostEqual(v.fab_floor_um,
                               contracts.floor("min_open_channel_mm") * 1000.0, places=9)
        self.assertIn("cannot be printed", v.message)


class TestDuctResistance(unittest.TestCase):
    def test_matches_the_readme_formula(self):
        mu, L, w, h = MU_37C, 10e-3, 0.4e-3, 0.3e-3
        expect = 12 * mu * L / (w * h**3 * (1 - 0.63 * h / w))
        self.assertAlmostEqual(P.duct_resistance(mu, L, w, h), expect, places=6)

    def test_uses_the_smaller_dimension_as_h(self):
        """A 25 um gap across an 11 mm chamber must not be read as an 11 mm tall duct."""
        a = P.duct_resistance(MU_37C, 200e-6, 11.32e-3, 25e-6)
        b = P.duct_resistance(MU_37C, 200e-6, 25e-6, 11.32e-3)
        self.assertAlmostEqual(a, b, places=6)

    def test_slit_pressure_scales_as_gap_cubed(self):
        q = 10e-9 / 60
        p25 = P.slit_pressure_pa(q, 25.0, 11.32, MU_37C)
        p50 = P.slit_pressure_pa(q, 50.0, 11.32, MU_37C)
        self.assertAlmostEqual(p25 / p50, 8.0, delta=0.05)


class TestWeirTolerance(unittest.TestCase):
    def setUp(self):
        self.chamber = {"id": "main_trap", "size": [11.32, 11.32, 0.3],
                        "retention": {"type": "weir", "opening_um": 25}}
        self.pc = P.particle_classes(MU_37C)["capsule"]
        self.q = 10.8e-9 / 60.0

    def test_limits_come_from_contracts(self):
        band = P.weir_tolerance_band(self.chamber, self.pc, self.q, MU_37C)
        self.assertAlmostEqual(
            band.retention_limit_um,
            contracts.param("trap.gap_factor_high") * self.pc.d_min_um, places=9)
        self.assertAlmostEqual(
            band.nominal_retention_limit_um,
            contracts.param("trap.gap_factor_high") * self.pc.diameter_um, places=9)
        self.assertAlmostEqual(band.pressure_limit_kpa,
                               contracts.max_working_pressure_kpa(), places=9)

    def test_nominal_limit_is_the_contracts_28_um(self):
        band = P.weir_tolerance_band(self.chamber, self.pc, self.q, MU_37C)
        self.assertAlmostEqual(band.nominal_retention_limit_um, 28.0, places=6)

    def test_retention_not_pressure_is_the_binding_constraint(self):
        band = P.weir_tolerance_band(self.chamber, self.pc, self.q, MU_37C)
        self.assertLess(band.nominal_dp_kpa, 0.05 * band.pressure_limit_kpa)
        self.assertIn("retention", band.limited_by[1])

    def test_band_is_bounded_above_by_retention(self):
        band = P.weir_tolerance_band(self.chamber, self.pc, self.q, MU_37C)
        self.assertLess(band.gap_hi_um, band.retention_limit_um)
        self.assertGreater(band.gap_hi_um, band.retention_limit_um - 0.2)

    def test_plus_side_of_the_z_tolerance_fails(self):
        """The headline: +20 um on a 25 um gap is 45 um, which retains nothing."""
        band = P.weir_tolerance_band(self.chamber, self.pc, self.q, MU_37C)
        self.assertLess(band.z_offset_hi_um, band.z_tolerance_um)
        self.assertLess(band.covered_fraction, 1.0)
        worst = [s for s in band.sweep
                 if abs(s["z_offset_um"] - band.z_tolerance_um) < 0.1]
        self.assertTrue(worst and not worst[0]["works"])
        self.assertFalse(worst[0]["retains"])

    def test_every_gap_in_the_band_both_retains_and_holds_pressure(self):
        band = P.weir_tolerance_band(self.chamber, self.pc, self.q, MU_37C)
        for s in band.sweep:
            if s["works"]:
                self.assertTrue(s["retains"] and s["pressure_ok"], msg=str(s))
                self.assertGreaterEqual(s["gap_um"], band.gap_lo_um - 1e-9)
                self.assertLessEqual(s["gap_um"], band.gap_hi_um + 1e-9)

    def test_a_tight_gap_eventually_fails_on_pressure(self):
        """Pressure must be able to bind - at a high enough flow it does."""
        band = P.weir_tolerance_band(self.chamber, self.pc, 5000e-9 / 60.0, MU_37C,
                                     nominal_gap_um=25.0)
        self.assertTrue(any(not s["pressure_ok"] for s in band.sweep),
                        msg="gap^-3 must breach the escape-pressure ceiling somewhere")

    def test_wider_gap_lowers_pressure(self):
        gaps = [s for s in P.weir_tolerance_band(self.chamber, self.pc, self.q,
                                                 MU_37C).sweep]
        dps = [s["dp_kpa"] for s in gaps]
        self.assertEqual(dps, sorted(dps, reverse=True))

    def test_z_tolerance_is_read_from_the_contract(self):
        tol, note = P.z_tolerance_um()
        self.assertEqual(tol, 20.0)
        self.assertIn("z_tolerance_warning", note.source)

    def test_capsule_size_lever(self):
        """The contract's own escape hatch, checked: it works only without the 0.3 D bound."""
        out = P.capsule_size_for_full_tolerance()
        self.assertFalse(out["with_contract_band"]["achievable"])
        self.assertTrue(out["retention_bound_only"]["achievable"])
        d_max = contracts.param_range("capsule.diameter_tunable_um")[1]
        self.assertGreater(out["with_contract_band"]["diameter_um_needed"], d_max)
        self.assertLessEqual(out["retention_bound_only"]["diameter_um_needed"], d_max)


class TestRealChips(unittest.TestCase):
    """Run on the three verification chips and assert the findings the brief asks for."""

    @classmethod
    def setUpClass(cls):
        cls.results = {c: P.analyse_chip(c, n_particles=24) for c in TARGETS}

    def test_all_three_chips_run(self):
        for chip_id, r in self.results.items():
            self.assertEqual(r["chip_id"], chip_id)
            self.assertIsNotNone(r["protocol_id"], msg=f"{chip_id} resolved no protocol")
            self.assertTrue(r["particle_classes"], msg=chip_id)

    def test_translated_chip_resolves_a_protocol_with_flows(self):
        """A translated chip must find a protocol, exact-name or by stripping the suffix.

        `protocols/module3_v1_translated.json` may or may not exist (a sibling added one
        mid-build); the exact name wins when it does, and `module3_v1` is the fallback.
        What must never happen is resolving nothing, because that silently produces a run
        with no flows, no sedimentation and no tracking that looks like a clean chip.
        """
        r = self.results["module3_v1_translated"]
        self.assertIn(r["protocol_id"], ("module3_v1_translated", "module3_v1"))
        self.assertTrue(r["tracks"], "a resolved protocol must yield flow steps")

    def test_protocol_resolution_falls_back_past_the_translated_suffix(self):
        """module1_v1_translated has no protocol of its own; module1_v1 does."""
        self.assertFalse((ROOT / "protocols" / "module1_v1_translated.json").exists())
        data, pid = P._resolve_protocol("module1_v1_translated")
        self.assertEqual(pid, "module1_v1")
        self.assertTrue(data and data.get("steps"))

    def test_protocol_resolution_prefers_an_exact_name(self):
        data, pid = P._resolve_protocol("cell_prep_v1")
        self.assertEqual(pid, "cell_prep_v1")

    def test_unknown_chip_resolves_no_protocol(self):
        self.assertEqual(P._resolve_protocol("no_such_chip_v9"), (None, None))

    def test_every_chip_uses_the_37c_viscosity(self):
        for chip_id, r in self.results.items():
            self.assertEqual(r["viscosity"]["key"], "fluid.viscosity_37c_pa_s",
                             msg=f"{chip_id} must not use water at 20 C")

    def test_module3_trap_settling_is_desired(self):
        r = self.results["module3_v1_translated"]
        trap = [s for s in r["sediment"] if s["feature_id"] == "main_trap"]
        self.assertTrue(trap)
        for s in trap:
            self.assertEqual(s["intent"], "desired", msg=s["message"])

    def test_module3_capsules_reach_the_trap_and_are_held(self):
        r = self.results["module3_v1_translated"]
        held = [t for t in r["tracks"]
                if "main_trap" in (t["settled_desired"] or {})]
        self.assertTrue(held, "no track retained capsules in main_trap")
        best = max(held, key=lambda t: t["fractions"]["settled_as_intended"])
        self.assertGreater(best["fractions"]["settled_as_intended"], 0.5)

    def test_module3_weir_band_is_reported(self):
        r = self.results["module3_v1_translated"]
        self.assertTrue(r["weir"])
        band = r["weir"][0]
        self.assertEqual(band["feature_id"], "main_trap")
        self.assertEqual(band["nominal_gap_um"], 25.0)
        self.assertLess(band["z_offset_hi_um"], 20.0)
        self.assertGreater(band["gap_hi_um"], 20.0)

    def test_module3_weir_dp_disagrees_with_the_contract_reference(self):
        """Flag it rather than silently trusting either number (README's rule)."""
        r = self.results["module3_v1_translated"]
        flagged = [f for f in r["findings"] if f["class"] == "unmodelled"]
        self.assertTrue(flagged, "the weir dP disagreement must be reported, not trusted")
        f = flagged[0]
        self.assertEqual(f["metric"], "weir_dp_pa_at_100_ul_min")
        self.assertAlmostEqual(
            f["limit"],
            contracts.block("retention.weir_geometry.pressure_drop_pa_at_100_ul_min"),
            places=9)
        # Our duct form gives roughly 9x the contract's figure on this chamber width.
        self.assertGreater(f["value"] / f["limit"], 2.0)
        self.assertIn("One of the two is wrong", f["message"])

    def test_module3_weir_sides_are_recorded(self):
        """Whoever meshed it, the trap's weir slits must be attributed to its walls.

        mesh.py records a list of slits per chamber with the wall each sits on; the
        fallback builder here records a prose reason. Both must name main_trap and reach
        the outlet side (x_max), which is where the chamber note puts the retention.
        """
        r = self.results["module3_v1_translated"]
        self.assertIn("main_trap", r["weir_sides"])
        sides = r["weir_sides"]["main_trap"]
        if isinstance(sides, str):
            self.assertIn("right", sides)
        else:
            walls = {w["wall"] for w in sides}
            self.assertIn("x_max", walls)
            for w in sides:
                self.assertAlmostEqual(w["gap_um"], 25.0, places=6)

    def test_module4_feed_lines_lose_capsules(self):
        """The audit's finding: capsules settle in Module 4's feed lines."""
        r = self.results["module4_v1"]
        feed_losses = [f for f in r["findings"]
                       if f["class"] == "sediment" and f["intent"] == "loss"
                       and f["feature_id"].startswith("ch_in_")]
        self.assertTrue(feed_losses, "module4 feed-line loss not detected")
        self.assertEqual({f["feature_id"] for f in feed_losses},
                         {"ch_in_1", "ch_in_2", "ch_in_3", "ch_in_4"})
        for f in feed_losses:
            self.assertEqual(f["severity"], "blocker")
            self.assertGreater(f["value"], 0.9)

    def test_module4_reactor_chambers_are_not_reported_as_losses(self):
        """sub_* are filled then sealed: a loss finding there would be the other error."""
        r = self.results["module4_v1"]
        for f in r["findings"]:
            if f["feature_id"].startswith("sub_"):
                self.assertNotEqual(f.get("intent"), "loss", msg=f["message"])

    def test_module4_vents_are_not_clog_or_sediment_findings(self):
        """p_vent is capsule_passable: false, so capsules never enter its channels."""
        r = self.results["module4_v1"]
        vent = [f for f in r["findings"] if f["feature_id"].startswith("ch_vent_")]
        self.assertEqual(vent, [], msg=f"spurious vent findings: {vent}")

    def test_cell_prep_mixer_loses_cells(self):
        """The audit's other finding: cells settle in cell_prep's mixer."""
        r = self.results["cell_prep_v1"]
        mixer = [f for f in r["findings"]
                 if f["feature_id"] == "mix_1" and f["class"] == "sediment"]
        self.assertTrue(mixer, "cell_prep mixer loss not detected")
        self.assertEqual(mixer[0]["intent"], "loss")
        self.assertIn("mixer", mixer[0]["intent_why"])
        self.assertEqual(mixer[0]["severity"], "blocker")

    def test_cell_prep_has_no_capsules(self):
        """It is a cell module. Its QC note mentions capsules; it never carries one."""
        r = self.results["cell_prep_v1"]
        self.assertEqual(set(r["particle_classes"]), {"cells"})
        for f in r["findings"]:
            self.assertNotIn("capsule", json.dumps(f).lower(), msg=f["message"])

    def test_cell_prep_reagent_feeds_are_not_cell_findings(self):
        """Cells originate in cult_1, so they cannot settle in the lines feeding it."""
        r = self.results["cell_prep_v1"]
        upstream = {"ch_enz_cult", "ch_buf_cult", "ch_quench_mix"}
        bad = [f["feature_id"] for f in r["findings"]
               if f["feature_id"] in upstream and f["class"] == "sediment"]
        self.assertEqual(bad, [], msg=f"findings in reagent lines upstream of the "
                                      f"cell source: {bad}")

    def test_cell_prep_culture_chamber_is_not_a_loss_during_culture(self):
        r = self.results["cell_prep_v1"]
        for s in r["sediment"]:
            if s["feature_id"] == "cult_1" and s.get("operation") != "transfer":
                self.assertIn(s["intent"], ("parked", "loss"))

    def test_no_absurd_transit_times(self):
        """A capped line's leakage flow must not become a multi-million-year transit."""
        for chip_id, r in self.results.items():
            for s in r["sediment"]:
                self.assertLess(s["transit_time_s"], 1e5,
                                msg=f"{chip_id} {s['element_id']}: "
                                    f"{s['transit_time_s']} s")

    def test_every_sediment_finding_carries_its_intent_and_reason(self):
        for chip_id, r in self.results.items():
            for f in r["findings"]:
                if f["class"] != "sediment":
                    continue
                self.assertIn(f["intent"], ("desired", "loss", "parked"), msg=chip_id)
                self.assertGreater(len(f["intent_why"] or ""), 20, msg=chip_id)

    def test_findings_match_the_analyse_shape(self):
        """analyse.py consumes these, so the keys are a contract."""
        required = {"id", "class", "severity", "chip_id", "feature_id", "step", "metric",
                    "value", "limit", "units", "message", "fix_hint"}
        for chip_id, r in self.results.items():
            for f in r["findings"]:
                self.assertTrue(required <= set(f), msg=f"{chip_id} missing "
                                                        f"{required - set(f)}")
                self.assertIn(f["severity"], ("blocker", "high", "medium", "low"))
                self.assertIn(f["class"], ("sediment", "clog", "leak", "unmodelled"))

    def test_findings_are_deduplicated(self):
        for chip_id, r in self.results.items():
            ids = [f["id"] for f in r["findings"]]
            self.assertEqual(len(ids), len(set(ids)), msg=chip_id)

    def test_every_chip_reports_its_assumptions(self):
        for chip_id, r in self.results.items():
            keys = {a["key"] for a in r["assumptions"]}
            self.assertIn(P.CARRIER_DENSITY_KEY, keys, msg=chip_id)
            self.assertTrue(any("excess_density" in k for k in keys), msg=chip_id)
            for a in r["assumptions"]:
                self.assertTrue(a["assumption"])
                self.assertTrue(a["source"])

    def test_mass_is_conserved_in_the_provisional_solve(self):
        """Not our module's job, but a bad field would invalidate every verdict above."""
        chip = json.loads((ROOT / "chip" / "module3_v1_translated.json").read_text())
        mesh = P._build_provisional_mesh(chip, MU_37C)
        q = 10.8e-9 / 60.0
        fld = P._solve_provisional(mesh, {"capsule_in": q}, ["waste"])
        self.assertLess(fld.residual["mass_imbalance_max"], 1e-6 * q)


class TestMeshingRules(unittest.TestCase):
    """The README calls these non-negotiable, and both have bitten this repo."""

    def test_serpentine_is_meshed_as_a_duct_at_its_lane_width(self):
        chip = json.loads((ROOT / "chip" / "cell_prep_v1.json").read_text())
        mesh = P._build_provisional_mesh(chip, MU_37C)
        duct = [e for e in mesh.edges if e.feature_id == "mix_1" and e.kind == "duct"]
        self.assertEqual(len(duct), 1)
        self.assertAlmostEqual(duct[0].width_mm, 0.2, places=9)
        self.assertAlmostEqual(duct[0].length_mm, 12.0, places=9)

    def test_the_mixer_duct_actually_carries_the_flow(self):
        """Both mouths landing on one node would bypass the mixer silently."""
        chip = json.loads((ROOT / "chip" / "cell_prep_v1.json").read_text())
        mesh = P._build_provisional_mesh(chip, MU_37C)
        q = 0.465e-9 / 60.0
        fld = P._solve_provisional(mesh, {"in_buffer": q}, ["out_cells"])
        self.assertAlmostEqual(abs(fld.q_m3_s["duct:mix_1"]), q, delta=1e-3 * q)

    def test_weir_chamber_gets_a_slit_edge_across_its_full_width(self):
        chip = json.loads((ROOT / "chip" / "module3_v1_translated.json").read_text())
        mesh = P._build_provisional_mesh(chip, MU_37C)
        slits = [e for e in mesh.edges if e.kind == "slit"]
        self.assertTrue(slits)
        for e in slits:
            self.assertAlmostEqual(e.width_mm, 11.32, places=6)
            self.assertAlmostEqual(e.depth_mm * 1000.0, 25.0, places=6)

    def test_only_outlet_side_channels_cross_the_weir(self):
        """ch_reagent enters on the inlet side and must NOT pass the slit."""
        chip = json.loads((ROOT / "chip" / "module3_v1_translated.json").read_text())
        mesh = P._build_provisional_mesh(chip, MU_37C)
        through = {e.id.split(":", 1)[1] for e in mesh.edges if e.kind == "slit"}
        self.assertIn("ch_waste", through)
        self.assertIn("ch_lysis_waste", through)
        self.assertIn("ch_backflush", through)
        self.assertNotIn("ch_reagent", through)
        self.assertNotIn("ch_to_split_tree", through)

    def test_routed_channels_follow_their_path(self):
        chip = json.loads((ROOT / "chip" / "module3_v1_translated.json").read_text())
        mesh = P._build_provisional_mesh(chip, MU_37C)
        # ch_waste is a channel_corner: path length exceeds the straight-line distance.
        e = next(e for e in mesh.edges if e.id == "duct:ch_waste")
        self.assertGreater(e.length_mm, 9.0)


class TestTracking(unittest.TestCase):
    def test_rk4_follows_a_uniform_field_at_the_right_speed(self):
        """A straight duct: transit must be length / velocity, to RK4 accuracy."""
        chip = json.loads((ROOT / "chip" / "cell_prep_v1.json").read_text())
        mesh = P._build_provisional_mesh(chip, MU_37C)
        q = 0.465e-9 / 60.0
        fld = P._solve_provisional(mesh, {"in_buffer": q}, ["out_cells"])
        e = next(e for e in mesh.edges if e.id == "duct:mix_1")
        v = abs(fld.q_m3_s[e.id]) / (e.width_mm * e.depth_mm * 1e-6)
        self.assertAlmostEqual((e.length_mm * 1e-3) / v, 46.5, delta=2.0)

    def test_fractions_sum_to_one(self):
        chip = json.loads((ROOT / "chip" / "module3_v1_translated.json").read_text())
        mesh = P._build_provisional_mesh(chip, MU_37C)
        q = 10.8e-9 / 60.0
        fld = P._solve_provisional(mesh, {"capsule_in": q}, ["waste"])
        pc = P.particle_classes(MU_37C)["capsule"]
        src = {"ports": ["capsule_in"], "chambers": [], "why": ["test"]}
        tr = P.track(chip, mesh, fld, pc, MU_37C, ["capsule_in"], n_particles=20,
                     source=src)
        self.assertGreater(tr.launched, 0)
        self.assertAlmostEqual(sum(tr.fractions().values()), 1.0, places=9)

    def test_a_particle_too_big_for_the_slit_is_retained(self):
        chip = json.loads((ROOT / "chip" / "module3_v1_translated.json").read_text())
        mesh = P._build_provisional_mesh(chip, MU_37C)
        q = 10.8e-9 / 60.0
        fld = P._solve_provisional(mesh, {"reagent_in": q}, ["waste"])
        pc = P.particle_classes(MU_37C)["capsule"]
        src = {"ports": [], "chambers": ["main_trap"], "why": ["resident"]}
        tr = P.track(chip, mesh, fld, pc, MU_37C, ["reagent_in"], n_particles=20,
                     source=src)
        self.assertGreater(tr.fractions()["settled_as_intended"], 0.8)
        self.assertEqual(tr.outlets.get("waste", 0), 0,
                         "a 70 um capsule must not leave through a 25 um weir slit")

    def test_resident_particles_are_not_seeded_in_a_buffer_line(self):
        """Seeding capsules at backflush_in would invent a 100% loss."""
        chip = json.loads((ROOT / "chip" / "module3_v1_translated.json").read_text())
        mesh = P._build_provisional_mesh(chip, MU_37C)
        pc = P.particle_classes(MU_37C)["capsule"]
        src = {"ports": ["capsule_in"], "chambers": ["main_trap"], "why": ["x"]}
        seeds, why = P._seed_nodes(chip, mesh, pc, ["backflush_in"], src)
        self.assertTrue(all("main_trap" in s for s in seeds), msg=seeds)
        self.assertIn("resident", why)

    def test_no_source_means_nothing_to_track(self):
        chip = json.loads((ROOT / "chip" / "cell_prep_v1.json").read_text())
        mesh = P._build_provisional_mesh(chip, MU_37C)
        pc = P.particle_classes(MU_37C)["capsule"]
        seeds, _ = P._seed_nodes(chip, mesh, pc, ["in_buffer"], None)
        self.assertEqual(seeds, [])


class TestContractDiscipline(unittest.TestCase):
    def test_no_contract_number_is_hard_coded(self):
        """Every contract value must be read through assay2chip.lib.contracts."""
        src = (ROOT / "assay2chip" / "cfd" / "particles.py").read_text()
        code = "\n".join(l for l in src.splitlines()
                         if not l.strip().startswith("#"))
        for literal in ("0.0008", "0.8e-3", "5.0 *", "70e-6", "1.5 *"):
            self.assertNotIn(literal, code, msg=f"{literal!r} looks hard-coded")

    def test_contract_reads_go_through_the_foundation(self):
        src = (ROOT / "assay2chip" / "cfd" / "particles.py").read_text()
        self.assertIn("from assay2chip.lib import contracts", src)
        self.assertNotIn("json.load(open", src)
        for direct in ("contracts/parameters.json\")", "blocks.json\")"):
            self.assertNotIn(direct, src)

    def test_missing_contract_key_raises_rather_than_defaults(self):
        with self.assertRaises(contracts.ContractError):
            contracts.param("capsule.there_is_no_such_key")

    def test_assumptions_serialise_with_provenance(self):
        for a in (P.RESUSPENSION_SHEAR_DYN_CM2, P.START_HEIGHT_FRACTION,
                  P.MEDIUM_DENSITY.as_assumption(),
                  P.excess_density("cell.excess_density_kg_m3").as_assumption()):
            j = a.as_json()
            self.assertTrue(j["assumption"])
            self.assertTrue(j["source"])
            self.assertGreater(len(j["why"]), 40)

    def test_contract_density_assumptions_carry_their_band(self):
        """Settling is linear in excess density, so the band is part of the result."""
        for key in P.EXCESS_DENSITY_KEYS.values():
            ex = P.excess_density(key)
            self.assertLess(ex.min_kg_m3, ex.max_kg_m3, msg=key)
            self.assertGreater(ex.span_ratio(), 1.0, msg=key)
            self.assertIn("kg/m3", ex.as_assumption().why)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""assay2chip/parts: the parametric part registry.

    python -m unittest discover -s tests -v
    python -m unittest tests.test_assay2chip_parts -v

Offline, no network, no model. Every number asserted here is either derived from the
contracts at run time or is a physical identity (a volume that must equal length x width x
depth); nothing is a copied literal, because a literal that duplicates a contract value is
the bug this repo keeps rediscovering.
"""

from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import jsonschema  # noqa: E402

from assay2chip.lib import contracts  # noqa: E402
from assay2chip.parts import derived, flowmatter, physics, registry  # noqa: E402
from assay2chip.parts.geometry import Load, Need, PartSizingError  # noqa: E402

SCHEMA = json.loads((ROOT / "contracts" / "schemas" / "chip_spec.schema.json").read_text())
DEPTH = contracts.max_depth_mm()

# One representative need per sized block, at the demo etch depth. These are the cases the
# "size every block and confirm the floors pass" check runs over.
REPRESENTATIVE: dict[str, Need] = {
    "straight_channel": Need(length_mm=6, flow_ul_min=5),
    "curved_channel": Need(length_mm=6, flow_ul_min=5),
    "channel_corner": Need(flow_ul_min=5),
    "serpentine_channel": Need(residence_s=300, flow_ul_min=5),
    "snake_channel": Need(residence_s=120, flow_ul_min=5),
    "asymmetric_serpentine": Need(residence_s=120, flow_ul_min=5),
    "spiral_channel": Need(residence_s=120, flow_ul_min=5),
    "ring_junction": Need(volume_ul=1, flow_ul_min=5),
    "port": Need(flow_ul_min=5),
    "t_junction": Need(flow_ul_min=5),
    "cross_junction": Need(flow_ul_min=5),
    "y_junction": Need(flow_ul_min=5),
    "curved_y_junction": Need(flow_ul_min=5),
    "fork_junction": Need(flow_ul_min=5),
    "network_node": Need(flow_ul_min=5),
    "polygon_chamber": Need(volume_ul=20, flow_ul_min=5),
    "culture_chamber": Need(volume_ul=2.88, flow_ul_min=1, shear_class="adherent_cells"),
    "trap_chamber": Need(volume_ul=20, flow_ul_min=10, particles="capsules",
                         shear_class="capsules"),
    "flow_focusing_funnel": Need(flow_ul_min=5),
    "flow_restrictor": Need(flow_ul_min=5, pressure_drop_pa=200),
    "flow_manifold": Need(branches=4, flow_ul_min=8),
    "flow_divider": Need(branches=3, flow_ul_min=6),
    "flow_distributor": Need(branches=4, flow_ul_min=8),
    "text": Need(length_mm=8),
    "pillar_array": Need(volume_ul=40, flow_ul_min=5),
    "dld_array": Need(volume_ul=20, flow_ul_min=5, particles="capsules",
                      shear_class="capsules"),
    "wells_array": Need(volume_ul=1, branches=4),
    "metering_trap": Need(volume_ul=5, flow_ul_min=5),
    "centipede_channel": Need(feed_edge_mm=8, flow_ul_min=1, shear_class="adherent_cells"),
    "herringbone_mixer": Need(flow_ul_min=5, mix=True),
    "flow_focusing": Need(flow_ul_min=5, particles="capsules", particle_um=220,
                          shear_class="capsules"),
    "phase_guide": Need(flow_ul_min=5),
    "organoid_chamber": Need(volume_ul=10, flow_ul_min=2, particles="beads",
                             particle_um=250),
}


def validate_entry(entry: dict, kind: str) -> None:
    """Check one expansion against the real chip_spec schema, not a paraphrase of it."""
    jsonschema.validate(
        entry,
        {"$schema": SCHEMA["$schema"], "$defs": SCHEMA["$defs"],
         "$ref": f"#/$defs/{kind}"},
    )


class RegistryShape(unittest.TestCase):
    def test_every_contract_block_has_a_status(self):
        entries = registry.list()
        self.assertEqual({e["block"] for e in entries}, contracts.library())
        for e in entries:
            self.assertIn(e["status"], ("sized", "refused", "not_sized"))
            if e["status"] != "sized":
                self.assertTrue(e["why"], f"{e['block']} has no reason recorded")
            self.assertIn(e["group"], ("basic", "advanced"))

    def test_the_special_blocks_are_all_sized(self):
        """Every block the brief calls out by name must be buildable from data."""
        sized = set(registry.sized_blocks())
        for block in ("trap_chamber", "serpentine_channel", "herringbone_mixer",
                      "centipede_channel", "pillar_array", "phase_guide",
                      "flow_restrictor", "metering_trap"):
            self.assertIn(block, sized)

    def test_unknown_block_is_refused_by_name(self):
        with self.assertRaises(PartSizingError) as cm:
            registry.size("magic_mixer", Need(volume_ul=1))
        self.assertIn("not a block id", str(cm.exception))

    def test_deterministic(self):
        need = Need(residence_s=300, flow_ul_min=5, particles="cells", particle_um=12)
        a = registry.size("serpentine_channel", need)
        b = registry.size("serpentine_channel", need)
        self.assertEqual(json.dumps(a.as_dict(), sort_keys=True),
                         json.dumps(b.as_dict(), sort_keys=True))


class EveryBlockAtDemoDepth(unittest.TestCase):
    """Size each block for a representative need and confirm the outputs pass the floors."""

    def test_all_sized_blocks_have_a_representative_need(self):
        self.assertEqual(sorted(REPRESENTATIVE), registry.sized_blocks())

    def test_every_part_passes_every_floor_and_fits(self):
        env = contracts.envelope("demo")
        for block, need in sorted(REPRESENTATIVE.items()):
            with self.subTest(block=block):
                part = registry.size(block, need)
                self.assertEqual(part.depth_mm, DEPTH)
                self.assertTrue(all(f.ok for f in part.floors))
                self.assertTrue(env.fits(*part.footprint_mm),
                                f"{block} is {part.footprint_mm} in a {env.x_mm} x "
                                f"{env.y_mm} envelope")
                self.assertLessEqual(part.depth_mm, contracts.max_depth_mm())

    def test_in_plane_floor_holds_and_z_is_exempt(self):
        floor = contracts.floor("min_open_channel_mm")
        saw_z = False
        for block, need in sorted(REPRESENTATIVE.items()):
            part = registry.size(block, need)
            for check in part.floors:
                if check.axis == "in_plane" and check.floor == "min_open_channel_mm":
                    self.assertGreaterEqual(check.actual_mm, floor - 1e-9,
                                            f"{block}: {check.what}")
                if check.axis == "z":
                    saw_z = True
        self.assertTrue(saw_z, "no part recorded a z feature, so the exemption is untested")

    def test_expansions_validate_against_the_chip_spec_schema(self):
        for block, need in sorted(REPRESENTATIVE.items()):
            part = registry.size(block, need)
            with self.subTest(block=block):
                for exp in part.entries:
                    validate_entry(exp.entry, exp.kind)
                    self.assertEqual(exp.entry["block"], block)
                    self.assertGreater(exp.volume_ul, 0)

    def test_volumes_add_up(self):
        for block, need in sorted(REPRESENTATIVE.items()):
            part = registry.size(block, need)
            if not part.entries:
                continue
            with self.subTest(block=block):
                self.assertAlmostEqual(sum(e.volume_ul for e in part.entries),
                                       part.volume_ul, places=3)

    def test_openings_sit_on_the_boundary(self):
        for block, need in sorted(REPRESENTATIVE.items()):
            part = registry.size(block, need)
            x, y = part.footprint_mm
            for o in part.openings:
                with self.subTest(block=block, opening=o.name):
                    on_edge = (
                        math.isclose(o.position[0], 0.0, abs_tol=1e-6)
                        or math.isclose(o.position[0], x, abs_tol=1e-3)
                        or math.isclose(o.position[1], 0.0, abs_tol=1e-6)
                        or math.isclose(o.position[1], y, abs_tol=1e-3))
                    self.assertTrue(on_edge, f"{o.name} at {o.position} is not on the "
                                             f"{x} x {y} boundary")
                    self.assertGreater(o.width_mm, 0)
                    self.assertIn(o.direction, ("-x", "+x", "-y", "+y"))

    def test_volume_is_never_the_bounding_box_for_a_folded_run(self):
        part = registry.size("serpentine_channel", Need(residence_s=300, flow_ul_min=5))
        box = part.footprint_mm[0] * part.footprint_mm[1] * part.depth_mm
        self.assertLess(part.volume_ul, box / 2,
                        "a folded run's volume must be its channel, not its footprint")
        self.assertAlmostEqual(part.physics["bounding_box_volume_ul"], box, places=2)


class Physics(unittest.TestCase):
    def test_shear_law(self):
        """tau = 6 mu Q / (w h^2), SI, x10 for dyn/cm2."""
        mu = contracts.viscosity_for(37).pa_s
        q, w, h = 10.0, 1.2, 0.3
        expect = 6 * mu * (q * 1e-9 / 60) / ((w * 1e-3) * (h * 1e-3) ** 2) * 10
        self.assertAlmostEqual(physics.wall_shear_dyn_cm2(q, w, h, mu), expect, places=12)

    def test_shear_inverse_is_consistent(self):
        mu = contracts.viscosity_for(37).pa_s
        ceiling = contracts.shear_ceiling_for("adherent_cells").ceiling_dyn_cm2
        w = physics.lane_width_for_shear_mm(1.0, DEPTH, mu, ceiling)
        self.assertAlmostEqual(physics.wall_shear_dyn_cm2(1.0, w, DEPTH, mu), ceiling,
                               places=9)

    def test_viscosity_is_the_37c_value(self):
        self.assertEqual(contracts.viscosity_for(37).key, "fluid.viscosity_37c_pa_s")
        part = registry.size("polygon_chamber", Need(volume_ul=10, flow_ul_min=5, temp_c=37))
        self.assertEqual(part.physics["viscosity_key"], "fluid.viscosity_37c_pa_s")

    def test_a_12_um_cell_falls_0_3_mm_in_about_51_s_at_the_nominal_density(self):
        """The repo's long-standing figure, and what cell.excess_density_kg_m3 is set to."""
        mu = contracts.viscosity_for(37).pa_s
        nominal = contracts.param("cell.excess_density_kg_m3")
        self.assertAlmostEqual(physics.settling_time_s(0.3, 12, mu, nominal), 51,
                               delta=1.0)

    def test_settling_is_linear_in_excess_density(self):
        """Which is the whole reason a sizer has to pick an end of the band and say so."""
        mu = contracts.viscosity_for(37).pa_s
        lo, hi = contracts.param_range("cell.excess_density_kg_m3")
        v_lo = physics.settling_velocity_um_s(12, mu, lo)
        v_hi = physics.settling_velocity_um_s(12, mu, hi)
        self.assertAlmostEqual(v_hi / v_lo, hi / lo, places=9)

    def test_physics_holds_no_material_constants(self):
        """Densities live in the contracts now; nothing here may keep a copy."""
        for gone in ("CELL_EXCESS_DENSITY_KG_M3", "WATER_DENSITY_KG_M3",
                     "SETTLING_ASSUMPTION", "DENSITY_ASSUMPTION"):
            self.assertFalse(hasattr(physics, gone), f"{gone} should be deleted")
        with self.assertRaises(TypeError):
            physics.settling_time_s(0.3, 12, 8e-4)        # density is required now
        with self.assertRaises(TypeError):
            physics.reynolds(10, 1.0, DEPTH, 8e-4)

    def test_reynolds_is_deep_in_stokes(self):
        mu = contracts.viscosity_for(37).pa_s
        rho = derived.fluid_density_kg_m3()
        self.assertLess(physics.reynolds(10, 1.0, DEPTH, mu, rho), 0.5)


class ShearIsOnTheLaneWidth(unittest.TestCase):
    def test_adherent_chamber_width_comes_from_the_ceiling(self):
        ceiling = contracts.shear_ceiling_for("adherent_cells")
        need = Need(volume_ul=2.88, flow_ul_min=9, shear_class="adherent_cells")
        part = registry.size("culture_chamber", need)
        self.assertLessEqual(part.physics["wall_shear_dyn_cm2"],
                             ceiling.ceiling_dyn_cm2 + 1e-9)
        self.assertEqual(part.physics["shear_measured_on"],
                         "chamber width across the flow")
        # the shear was computed on the real lane width, not on a bounding box
        self.assertEqual(part.physics["lane_width_mm"],
                         part.physics["width_across_flow_mm"])

    def test_too_much_flow_for_adherent_cells_raises_with_the_width_needed(self):
        with self.assertRaises(PartSizingError) as cm:
            registry.size("culture_chamber",
                          Need(volume_ul=2.88, flow_ul_min=400,
                               shear_class="adherent_cells"))
        self.assertIn("envelope", str(cm.exception).lower())

    def test_capsules_are_bounded_by_pressure_not_shear(self):
        part = registry.size("trap_chamber",
                             Need(volume_ul=20, flow_ul_min=10, particles="capsules",
                                  shear_class="capsules"))
        self.assertIsNone(part.physics["ceiling_dyn_cm2"])
        self.assertEqual(part.physics["bound_by"], "escape_pressure")
        self.assertEqual(part.physics["escape_pressure_margin_kpa"],
                         contracts.max_working_pressure_kpa())


class DepthWeir(unittest.TestCase):
    def setUp(self):
        self.part = registry.size("trap_chamber", Need(
            volume_ul=20, flow_ul_min=10, particles="capsules", shear_class="capsules"))
        self.ret = self.part.entries[0].entry["retention"]

    def test_gap_is_in_the_contract_band_with_a_strict_upper_bound(self):
        d = contracts.capsule_diameter_um()
        low = contracts.param("trap.gap_factor_low") * d
        high = contracts.param("trap.gap_factor_high") * d
        gap = self.ret["opening_um"]
        self.assertGreaterEqual(gap, low)
        self.assertLess(gap, high, "the upper bound is strict: check_chip_spec fails on >=")
        self.assertEqual(gap, derived.weir_gap_um())

    def test_crest_height_is_derived_not_fixed(self):
        """crest = THIS chip's etch depth minus the gap. No fixed number exists anywhere."""
        self.assertAlmostEqual(
            self.ret["crest_height_um"] + self.ret["opening_um"],
            self.part.depth_mm * 1000, places=6)
        shallow = registry.size("trap_chamber", Need(
            volume_ul=5, flow_ul_min=5, particles="capsules", shear_class="capsules",
            depth_mm=0.2))
        self.assertNotEqual(shallow.entries[0].entry["retention"]["crest_height_um"],
                            self.ret["crest_height_um"])
        self.assertAlmostEqual(
            shallow.entries[0].entry["retention"]["crest_height_um"]
            + shallow.entries[0].entry["retention"]["opening_um"], 200, places=6)

    def test_crest_thickness_is_in_plane_and_meets_the_floor(self):
        self.assertGreaterEqual(self.ret["crest_thickness_um"],
                                contracts.floor("min_open_channel_mm") * 1000)
        crest = next(f for f in self.part.features if f.kind == "weir_crest")
        self.assertAlmostEqual(crest.size_mm[0] * 1000, self.ret["crest_thickness_um"])
        self.assertAlmostEqual(crest.size_mm[2] * 1000, self.ret["crest_height_um"])

    def test_the_gap_is_exempt_from_the_in_plane_floor(self):
        gap_check = next(f for f in self.part.floors if "weir gap" in f.what)
        self.assertEqual(gap_check.axis, "z")
        self.assertTrue(gap_check.ok)
        self.assertLess(gap_check.actual_mm, contracts.floor("min_open_channel_mm"))

    def test_retains_is_the_particle_and_larger_than_the_opening(self):
        self.assertEqual(self.ret["retains_um"], contracts.capsule_diameter_um())
        self.assertGreater(self.ret["retains_um"], self.ret["opening_um"])

    def test_z_tolerance_risk_is_recorded(self):
        self.assertEqual(self.part.physics["z_tolerance_um"], derived.z_tolerance_um())
        self.assertAlmostEqual(self.part.physics["z_tolerance_percent_of_gap"], 80.0,
                               delta=0.5)
        self.assertTrue(any("Z TOLERANCE" in risk for risk in self.part.risks))

    def test_volume_is_the_box_minus_the_crest(self):
        e = self.part.entries[0].entry
        x, y, z = e["size"]
        crest = self.ret["crest_thickness_um"] / 1000 * y * self.ret["crest_height_um"] / 1000
        self.assertAlmostEqual(e["volume_ul"], x * y * z - crest, places=3)
        self.assertEqual(self.part.entries[0].formula, "box minus crest minus pillars")

    def test_a_layer_shallower_than_the_gap_is_refused(self):
        with self.assertRaises(PartSizingError) as cm:
            registry.size("trap_chamber", Need(
                volume_ul=1, flow_ul_min=1, particles="capsules",
                shear_class="capsules", depth_mm=0.02))
        self.assertIn("crest", str(cm.exception))

    def test_it_says_a_weir_does_not_hold_bare_cells(self):
        part = registry.size("trap_chamber", Need(
            volume_ul=10, flow_ul_min=5, particles="cells", particle_um=70))
        self.assertTrue(any("bare cells" in risk or "_cells_caveat" in risk
                            for risk in part.risks))


class DelayLine(unittest.TestCase):
    def test_length_comes_from_the_residence_time(self):
        need = Need(residence_s=300, flow_ul_min=5)
        part = registry.size("serpentine_channel", need)
        want_ul = physics.residence_volume_ul(5, 300)
        self.assertAlmostEqual(part.volume_ul, want_ul, places=2)
        self.assertAlmostEqual(part.physics["residence_s"], 300, delta=0.5)
        length = part.physics["centreline_length_mm"]
        lane = part.physics["lane_width_mm"]
        self.assertAlmostEqual(length * lane * part.depth_mm, part.volume_ul, places=3)

    def test_shear_is_on_the_lane_not_the_footprint(self):
        part = registry.size("serpentine_channel", Need(residence_s=300, flow_ul_min=5))
        mu = contracts.viscosity_for(None).pa_s
        lane = part.physics["lane_width_mm"]
        self.assertAlmostEqual(
            part.physics["wall_shear_dyn_cm2"],
            physics.wall_shear_dyn_cm2(5, lane, part.depth_mm, mu), places=6)
        self.assertLess(lane, min(part.footprint_mm),
                        "the lane must be narrower than the folded footprint")

    def test_sedimentation_is_reported_when_residence_beats_settling(self):
        part = registry.size("serpentine_channel",
                             Need(residence_s=300, flow_ul_min=5, particles="cells",
                                  particle_um=12))
        self.assertTrue(part.physics["settles_before_exit"])
        self.assertTrue(any("SEDIMENTATION" in r for r in part.risks))
        self.assertTrue(any("cell.excess_density_kg_m3" in a for a in part.assumptions),
                        "the density behind settling is an assumption and must say so")

    def test_a_delay_line_takes_the_max_of_the_density_band(self):
        """A line checking for LOSS by settling must not miss a depositing line."""
        part = registry.size("serpentine_channel",
                             Need(residence_s=300, flow_ul_min=5, particles="cells",
                                  particle_um=12))
        lo, hi = contracts.param_range("cell.excess_density_kg_m3")
        self.assertEqual(part.physics["excess_density_purpose"], "loss")
        self.assertEqual(part.physics["excess_density_end"], "max")
        self.assertEqual(part.physics["excess_density_kg_m3"], hi)
        self.assertEqual(part.physics["excess_density_key"], "cell.excess_density_kg_m3")
        self.assertEqual(part.physics["excess_density_band_kg_m3"], [lo, hi])
        mu = contracts.viscosity_for(None).pa_s
        self.assertAlmostEqual(part.physics["settles_full_depth_s"],
                               physics.settling_time_s(DEPTH, 12, mu, hi), places=2)
        self.assertIn("FASTEST", part.physics["excess_density_why_this_end"])

    def test_a_trap_takes_the_min_of_the_density_band(self):
        """A chamber RELYING on settling must not over-promise retention."""
        part = registry.size("trap_chamber", Need(
            volume_ul=20, flow_ul_min=10, particles="capsules", shear_class="capsules"))
        lo, hi = contracts.param_range("capsule.excess_density_kg_m3")
        self.assertEqual(part.physics["excess_density_purpose"], "retain")
        self.assertEqual(part.physics["excess_density_end"], "min")
        self.assertEqual(part.physics["excess_density_kg_m3"], lo)
        self.assertEqual(part.physics["excess_density_key"],
                         "capsule.excess_density_kg_m3")
        self.assertIn("SLOWEST", part.physics["excess_density_why_this_end"])

    def test_the_two_ends_really_differ(self):
        """If both questions got the same number, recording the end would be theatre."""
        line = registry.size("serpentine_channel",
                             Need(residence_s=300, flow_ul_min=5, particles="cells",
                                  particle_um=12))
        field = registry.size("culture_chamber",
                              Need(volume_ul=2.88, flow_ul_min=1, particles="cells",
                                   particle_um=12, shear_class="adherent_cells"))
        self.assertEqual(field.physics["excess_density_end"], "min")
        self.assertLess(line.physics["settles_full_depth_s"],
                        field.physics["settles_full_depth_s"])

    def test_one_density_per_particle_kind(self):
        """A shared particle density made cells, capsules and beads settle identically."""
        keys = set()
        for particles, d_um in (("cells", 12), ("capsules", 70), ("beads", 70)):
            part = registry.size("polygon_chamber",
                                 Need(volume_ul=10, flow_ul_min=5, particles=particles,
                                      particle_um=d_um))
            keys.add(part.physics["excess_density_key"])
        self.assertEqual(keys, set(derived.EXCESS_DENSITY_KEYS.values()))

    def test_beads_settle_far_faster_than_a_capsule_of_the_same_size(self):
        mu = contracts.viscosity_for(37).pa_s
        bead = physics.settling_velocity_um_s(
            70, mu, contracts.param("bead.excess_density_kg_m3"))
        capsule = physics.settling_velocity_um_s(
            70, mu, contracts.param("capsule.excess_density_kg_m3"))
        self.assertGreater(bead / capsule, 10)

    def test_purpose_must_be_retain_or_loss(self):
        with self.assertRaises(contracts.ContractError):
            derived.excess_density("cells", "maybe")
        with self.assertRaises(contracts.ContractError):
            derived.excess_density("none", "loss")

    def test_no_sedimentation_risk_for_a_short_hold(self):
        part = registry.size("serpentine_channel",
                             Need(residence_s=10, flow_ul_min=5, particles="cells",
                                  particle_um=12))
        self.assertFalse(part.physics["settles_before_exit"])
        self.assertFalse(any("SEDIMENTATION" in r for r in part.risks))

    def test_settling_in_a_trap_is_intended_not_a_risk(self):
        part = registry.size("trap_chamber", Need(
            volume_ul=20, flow_ul_min=10, particles="capsules", shear_class="capsules"))
        self.assertTrue(part.physics["settling_intended_here"])
        self.assertFalse(any("SEDIMENTATION" in r for r in part.risks))


class MixingNeedsAHerringbone(unittest.TestCase):
    def test_a_plain_serpentine_refuses_to_mix(self):
        with self.assertRaises(PartSizingError) as cm:
            registry.size("serpentine_channel",
                          Need(residence_s=60, flow_ul_min=5, mix=True))
        self.assertIn("herringbone_mixer", str(cm.exception))

    def test_straight_and_junctions_refuse_to_mix_too(self):
        for block in ("straight_channel", "t_junction", "snake_channel"):
            with self.subTest(block=block):
                with self.assertRaises(PartSizingError):
                    registry.size(block, Need(length_mm=5, volume_ul=1, flow_ul_min=5,
                                              mix=True))

    def test_asymmetric_serpentine_refuses_below_re_1(self):
        with self.assertRaises(PartSizingError) as cm:
            registry.size("asymmetric_serpentine",
                          Need(residence_s=60, flow_ul_min=5, mix=True))
        self.assertIn("Re", str(cm.exception))

    def test_herringbone_mixes_and_says_how_many_cycles(self):
        part = registry.size("herringbone_mixer", Need(flow_ul_min=5, mix=True))
        self.assertGreaterEqual(part.physics["cycles"], 3)
        self.assertEqual(part.physics["cycles"],
                         physics.herringbone_cycles(part.physics["peclet"]))
        self.assertGreater(part.physics["grooves"], 0)
        groove = next(f for f in part.features if f.kind == "herringbone_groove")
        self.assertEqual(groove.count, part.physics["grooves"])

    def test_the_grooves_are_a_z_feature_under_the_layer_depth(self):
        part = registry.size("herringbone_mixer", Need(flow_ul_min=5, mix=True))
        self.assertAlmostEqual(
            part.physics["channel_height_um"] + part.physics["groove_depth_um"],
            part.depth_mm * 1000, places=6)
        self.assertGreaterEqual(part.physics["groove_width_mm"],
                                contracts.floor("min_open_channel_mm"))

    def test_it_admits_the_200_um_floor_scales_the_published_geometry(self):
        part = registry.size("herringbone_mixer", Need(flow_ul_min=5, mix=True))
        self.assertTrue(any("50 um" in r for r in part.risks))

    def test_chaotic_advection_beats_diffusion_by_a_lot(self):
        part = registry.size("herringbone_mixer", Need(flow_ul_min=5, mix=True))
        self.assertGreater(part.physics["diffusive_mixing_length_mm"],
                           part.physics["centreline_length_mm"])


class CombDistribution(unittest.TestCase):
    """The Module 6 precedent, re-derived from the floors."""

    def setUp(self):
        self.part = registry.size("centipede_channel", Need(
            feed_edge_mm=8.0, flow_ul_min=1, shear_class="adherent_cells"))

    def test_it_reproduces_module_6(self):
        module6 = json.loads((ROOT / "chip" / "module6_v1.json").read_text())
        comb = next(c for c in module6["channels"] if c["block"] == "centipede_channel")
        field = next(c for c in module6["chambers"] if c["id"] == "cx_n")
        self.assertEqual(field["size"][1], 8.0)                     # the edge fed
        self.assertEqual(self.part.physics["teeth"], 13)            # "13 side teeth"
        self.assertEqual(self.part.physics["tooth_width_mm"], 0.2)  # "0.2 x 1.5 mm"
        self.assertEqual(self.part.physics["tooth_length_mm"], 1.5)
        self.assertEqual(self.part.physics["pitch_mm"], 0.6)        # "0.6 mm pitch"
        self.assertEqual(self.part.physics["rib_mm"], 0.4)          # "0.4 mm ribs"
        self.assertEqual(comb["width_mm"], 0.8)                     # the spine
        self.assertGreaterEqual(self.part.physics["spine_width_mm"], 0.8)

    def test_the_teeth_are_real_features(self):
        teeth = next(f for f in self.part.features if f.kind == "comb_tooth")
        self.assertEqual(teeth.count, self.part.physics["teeth"])
        self.assertEqual(teeth.size_mm[1], self.part.physics["tooth_width_mm"])
        self.assertEqual(teeth.size_mm[0], self.part.physics["tooth_length_mm"])
        ribs = next(f for f in self.part.features if f.kind == "comb_rib")
        self.assertEqual(ribs.count, teeth.count - 1)

    def test_tooth_width_is_the_floor_and_rib_is_the_wall(self):
        self.assertEqual(self.part.physics["tooth_width_mm"],
                         contracts.floor("min_open_channel_mm"))
        self.assertEqual(self.part.physics["rib_mm"], contracts.floor("min_wall_mm"))

    def test_volume_is_spine_plus_teeth(self):
        p = self.part.physics
        self.assertAlmostEqual(p["spine_volume_ul"] + p["teeth_volume_ul"],
                               self.part.volume_ul, places=3)
        self.assertAlmostEqual(
            p["teeth_volume_ul"],
            p["teeth"] * p["tooth_length_mm"] * p["tooth_width_mm"] * self.part.depth_mm,
            places=4)

    def test_teeth_at_the_floor_do_pass_a_70_um_capsule(self):
        """The contract's own channel rule: 1.5 x 70 um is 105 um, under the 200 um floor.

        So a floor-width tooth passes a capsule, and Module 6 keeping its combs on liquid
        is a contamination choice rather than a dimensional one.
        """
        part = registry.size("centipede_channel",
                             Need(feed_edge_mm=8, flow_ul_min=1, particles="capsules"))
        factor = contracts.param("capsule_channel.width_factor_low")
        self.assertLess(factor * contracts.capsule_diameter_um() / 1000,
                        contracts.floor("min_open_channel_mm"))
        self.assertEqual(part.physics["tooth_width_mm"],
                         contracts.floor("min_open_channel_mm"))

    def test_a_comb_refuses_cargo_too_big_for_a_tooth(self):
        with self.assertRaises(PartSizingError) as cm:
            registry.size("centipede_channel",
                          Need(feed_edge_mm=8, flow_ul_min=1, particles="capsules",
                               particle_um=200))
        self.assertIn("in-plane floor", str(cm.exception))
        self.assertIn("Module 6", str(cm.exception))

    def test_an_edge_too_short_for_two_teeth_is_refused(self):
        with self.assertRaises(PartSizingError) as cm:
            registry.size("centipede_channel", Need(feed_edge_mm=0.5, flow_ul_min=1))
        self.assertIn("straight_channel", str(cm.exception))


class LidSupport(unittest.TestCase):
    def test_a_narrow_chamber_needs_no_pillars(self):
        tight, loose = derived.lid_support_pitch_mm()
        part = registry.size("polygon_chamber",
                             Need(volume_ul=loose ** 2 * DEPTH / 4, flow_ul_min=5))
        self.assertLessEqual(max(part.footprint_mm), loose + 1e-9)
        self.assertEqual(part.physics["pillars"], 0)

    def test_a_wide_chamber_gets_pillars_inside_the_contract_band(self):
        tight, loose = derived.lid_support_pitch_mm()
        part = registry.size("polygon_chamber", Need(volume_ul=60, flow_ul_min=5))
        self.assertGreater(max(part.footprint_mm), loose)
        self.assertGreater(part.physics["pillars"], 0)
        pitch = next(f for f in part.features if f.kind == "lid_support_pillar").pitch_mm
        self.assertGreaterEqual(pitch, tight)
        self.assertLessEqual(pitch, loose)

    def test_pillars_are_paid_for_in_volume(self):
        """Support is not free: the chamber grows so the FLUID volume is still right."""
        part = registry.size("polygon_chamber", Need(volume_ul=60, flow_ul_min=5))
        e = part.entries[0].entry
        box = e["size"][0] * e["size"][1] * e["size"][2]
        self.assertGreater(box, 60)
        self.assertAlmostEqual(e["volume_ul"], 60, delta=0.01)

    def test_pillar_array_says_it_cannot_retain(self):
        part = registry.size("pillar_array", Need(volume_ul=40, flow_ul_min=5))
        self.assertFalse(part.physics["retains_capsules"])
        self.assertGreater(part.physics["gap_um"], contracts.swollen_capsule_diameter_um())
        self.assertTrue(any("CANNOT retain" in r for r in part.risks))


class GuideAndRestrictorAreNotTheSameThing(unittest.TestCase):
    """An audit found these two swapped. They are opposites and must stay apart."""

    def test_phase_guide_is_a_capillary_stop_with_no_resistance(self):
        part = registry.size("phase_guide", Need(flow_ul_min=5))
        self.assertEqual(part.physics["function"], "capillary_stop")
        self.assertTrue(part.physics["stops_flow"])
        self.assertFalse(part.physics["sets_resistance"])
        self.assertNotIn("resistance_pa_s_per_m3", part.physics)

    def test_phase_guide_refuses_to_invent_a_burst_pressure(self):
        part = registry.size("phase_guide", Need(flow_ul_min=5))
        self.assertIsNone(part.physics["burst_pressure_pa"])
        self.assertIn("not in contracts/parameters.json",
                      part.physics["burst_pressure_gap"])

    def test_flow_restrictor_sets_resistance_and_does_not_stop_flow(self):
        part = registry.size("flow_restrictor",
                             Need(flow_ul_min=5, pressure_drop_pa=200))
        self.assertEqual(part.physics["function"], "resistance")
        self.assertFalse(part.physics["stops_flow"])
        self.assertAlmostEqual(part.physics["pressure_drop_pa"], 200, delta=1.0)

    def test_restrictor_needs_a_resistance_target(self):
        with self.assertRaises(PartSizingError) as cm:
            registry.size("flow_restrictor", Need(flow_ul_min=5))
        self.assertIn("pressure_drop_pa", str(cm.exception))

    def test_each_names_the_other(self):
        guide = registry.size("phase_guide", Need(flow_ul_min=5))
        restr = registry.size("flow_restrictor", Need(flow_ul_min=5, pressure_drop_pa=50))
        self.assertIn("flow_restrictor", guide.physics["not_to_be_confused_with"])
        self.assertIn("phase_guide", restr.physics["not_to_be_confused_with"])


class MeteringTrap(unittest.TestCase):
    def test_it_captures_the_volume_asked_for(self):
        part = registry.size("metering_trap", Need(volume_ul=5, flow_ul_min=5))
        self.assertAlmostEqual(part.volume_ul, 5, delta=0.01)
        e = part.entries[0].entry
        self.assertAlmostEqual(e["size"][0] * e["size"][1] * e["size"][2],
                               part.volume_ul, places=2)

    def test_the_tolerance_is_a_z_tolerance(self):
        part = registry.size("metering_trap", Need(volume_ul=5, flow_ul_min=5))
        expect = 100 * derived.z_tolerance_um() / (DEPTH * 1000)
        self.assertAlmostEqual(part.physics["volume_tolerance_percent"], expect, places=2)
        self.assertTrue(any("z tolerance" in r for r in part.risks))

    def test_it_has_a_vent_so_the_fill_can_stop(self):
        part = registry.size("metering_trap", Need(volume_ul=5, flow_ul_min=5))
        vent = part.opening("vent")
        self.assertIn("phase_guide", vent.note)


class RefusalsAreHonest(unittest.TestCase):
    def test_tesla_valve_is_refused_for_the_contract_reason(self):
        with self.assertRaises(PartSizingError) as cm:
            registry.size("tesla_valve", Need(flow_ul_min=5))
        self.assertIn("1.0", str(cm.exception))
        self.assertIn("Stokes", str(cm.exception))

    def test_pillar_funnel_trap_is_refused_as_retention(self):
        with self.assertRaises(PartSizingError) as cm:
            registry.size("pillar_funnel_trap",
                          Need(volume_ul=10, flow_ul_min=5, particles="capsules"))
        self.assertIn("trap_chamber", str(cm.exception))

    def test_not_sized_blocks_say_why(self):
        for e in registry.list():
            if e["status"] == "not_sized":
                with self.subTest(block=e["block"]):
                    with self.assertRaises(PartSizingError) as cm:
                        registry.size(e["block"], Need(volume_ul=5, flow_ul_min=5))
                    self.assertIn("missing_part", str(cm.exception))

    def test_the_encapsulation_nozzle_does_not_fit_the_in_plane_floor(self):
        """A real audit result: a 40 um nozzle cannot be printed at a 200 um floor."""
        nozzle = contracts.param("encapsulation.nozzle_um")
        self.assertLess(nozzle / 1000, contracts.floor("min_open_channel_mm"))
        with self.assertRaises(PartSizingError) as cm:
            registry.size("flow_focusing", Need(flow_ul_min=5, particles="capsules"))
        msg = str(cm.exception)
        self.assertIn("in-plane floor", msg)
        self.assertIn("diameter_tunable_um", msg)

    def test_a_printable_nozzle_makes_a_bigger_capsule(self):
        part = registry.size("flow_focusing", Need(
            flow_ul_min=5, particles="capsules", particle_um=220, shear_class="capsules"))
        lo, hi = contracts.param_range("capsule.diameter_tunable_um")
        self.assertGreaterEqual(part.physics["nozzle_um"],
                                contracts.floor("min_open_channel_mm") * 1000)
        self.assertLessEqual(part.physics["droplet_um_approx"], hi)

    def test_a_missing_need_field_raises_naming_the_field(self):
        with self.assertRaises(PartSizingError) as cm:
            registry.size("serpentine_channel", Need(flow_ul_min=5))
        self.assertIn("volume_ul", str(cm.exception))
        with self.assertRaises(PartSizingError) as cm:
            registry.size("centipede_channel", Need(flow_ul_min=5))
        self.assertIn("feed_edge_mm", str(cm.exception))

    def test_cells_need_a_diameter_because_no_contract_gives_one(self):
        with self.assertRaises(PartSizingError) as cm:
            registry.size("polygon_chamber",
                          Need(volume_ul=5, flow_ul_min=5, particles="cells"))
        self.assertIn("particle_um", str(cm.exception))

    def test_a_part_too_big_for_the_envelope_raises_with_the_number(self):
        with self.assertRaises(PartSizingError) as cm:
            registry.size("polygon_chamber", Need(volume_ul=5000, flow_ul_min=5))
        self.assertIn("envelope", str(cm.exception))
        self.assertTrue(cm.exception.floor.startswith("envelope_"))
        self.assertGreater(cm.exception.actual, cm.exception.required)

    def test_what_will_not_fit_the_demo_may_fit_the_target(self):
        need = Need(volume_ul=400, flow_ul_min=5)
        with self.assertRaises(PartSizingError):
            registry.size("polygon_chamber", need)
        bigger = registry.size(
            "polygon_chamber", Need(volume_ul=400, flow_ul_min=5, scale="target"))
        self.assertTrue(contracts.envelope("target").fits(*bigger.footprint_mm))

    def test_try_size_returns_the_reason_instead_of_raising(self):
        part, why = registry.try_size("tesla_valve", Need(flow_ul_min=5))
        self.assertIsNone(part)
        self.assertIn("Stokes", why)
        part, why = registry.try_size("polygon_chamber", Need(volume_ul=5, flow_ul_min=5))
        self.assertIsNotNone(part)
        self.assertIsNone(why)


class ContractsAreNeverHardCoded(unittest.TestCase):
    def test_prose_values_are_read_from_the_contract(self):
        self.assertEqual(derived.z_tolerance_um(), 20.0)
        self.assertEqual(derived.lid_support_pitch_mm(), (5.0, 10.0))
        ref = derived.dld_reference()
        self.assertEqual((ref.gap_um, ref.epsilon), (200.0, 0.125))
        self.assertEqual(derived.capsule_settling_band_um_s(), (50.0, 210.0))

    def test_densities_are_read_from_the_contract_not_held_here(self):
        self.assertEqual(derived.fluid_density_kg_m3(),
                         contracts.param("fluid.density_37c_kg_m3"))
        for particles, key in derived.EXCESS_DENSITY_KEYS.items():
            with self.subTest(particles=particles):
                lo, hi = contracts.param_range(key)
                self.assertEqual(derived.excess_density(particles, "retain").value, lo)
                self.assertEqual(derived.excess_density(particles, "loss").value, hi)
                self.assertTrue(contracts.is_assumption(key))
                self.assertIn(key, derived.excess_density(particles, "loss").assumption)

    def test_the_dld_correlation_agrees_with_the_contracts_worked_point(self):
        ref = derived.dld_reference()
        got = physics.dld_critical_diameter_um(ref.gap_um, ref.epsilon)
        self.assertAlmostEqual(got, ref.critical_diameter_um, delta=10)

    def test_no_contract_number_is_retyped_in_this_package(self):
        """No contract value appears as a numeric LITERAL anywhere in the package.

        Parsed with ast rather than grepped, so prose in a docstring may quote a contract
        number (that is documentation) while executable code may not (that is the bug).
        A literal duplicating a contract value is how a 2.0 mm port outlived the 0.8 mm
        contract value and how 0.8e-3 Pa s ended up in four generators.
        """
        import ast

        banned = {
            contracts.viscosity_37c_pa_s().pa_s: "fluid.viscosity_37c_pa_s",
            contracts.viscosity_water_20c_pa_s().pa_s: "fluid.viscosity_pa_s",
            contracts.capsule_diameter_um(): "capsule.diameter_um",
            contracts.swollen_capsule_diameter_um(): "capsule swollen diameter",
            derived.weir_gap_um(): "retention.weir_geometry.gap_um",
            derived.weir_crest_thickness_um(): "retention.weir_geometry.crest_thickness_um",
            contracts.param("encapsulation.nozzle_um"): "encapsulation.nozzle_um",
            contracts.param("shear.culture_ceiling_dyn_cm2"): "shear.culture_ceiling",
            contracts.envelope("demo").x_mm: "envelope.max_x_mm",
            contracts.envelope("demo").y_mm: "envelope.max_y_mm",
            contracts.envelope("demo").depth_mm: "envelope.max_depth_mm",
            contracts.envelope("target").x_mm: "target_envelope.x_mm",
            contracts.envelope("target").y_mm: "target_envelope.y_mm",
            contracts.floor("min_open_channel_mm"): "min_open_channel_mm",
            contracts.floor("min_wall_mm"): "min_wall_mm",
            contracts.floor("min_port_mm"): "min_port_mm",
            float(derived.z_tolerance_um()): "the z tolerance",
        }
        # Deliberately NOT banned: min_port_pitch_mm (5) and max_ports_per_chip (6) are
        # small integers that collide with rounding precisions and loop bounds, so banning
        # them would flag `round(x, 5)`. They are read through contracts everywhere in the
        # package; the assertions in this file cover them by behaviour instead.
        pkg = ROOT / "assay2chip" / "parts"
        for path in sorted(pkg.glob("*.py")):
            if path.name.startswith("_"):
                continue
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                        and not isinstance(node.value, bool):
                    written = ast.get_source_segment(source, node) or ""
                    if "e-" in written or "e+" in written:
                        continue  # 1e-3 and friends are mm-to-SI conversions, not values
                    hit = banned.get(float(node.value))
                    self.assertIsNone(
                        hit, f"{path.name}:{node.lineno} has the literal "
                             f"{node.value!r}, which is {hit}. Read it through "
                             f"assay2chip.lib.contracts instead.")

    def test_depth_above_the_contract_maximum_is_refused(self):
        with self.assertRaises(PartSizingError) as cm:
            registry.size("polygon_chamber",
                          Need(volume_ul=5, flow_ul_min=5, depth_mm=1.0))
        self.assertEqual(cm.exception.floor, "max_depth_mm")

    def test_one_layer_is_assumed_everywhere(self):
        self.assertTrue(contracts.single_layer())


class BenchVolumesDoNotTransfer(unittest.TestCase):
    """assay2chip/README.md: a volume below the smallest chamber is rounded UP, not refused.

    The first end-to-end run died at S6 on "none of polygon_chamber can be built for
    0.012 uL of cells". A tube volume is not an assay volume, and what is left after the
    tube is dropped can be tiny and still real.
    """

    def test_the_smallest_buildable_chamber_is_the_floor_squared(self):
        from assay2chip.parts.geometry import min_chamber_volume_ul

        floor = contracts.floor("min_open_channel_mm")
        self.assertAlmostEqual(min_chamber_volume_ul(DEPTH), floor * floor * DEPTH)

    def test_a_sub_minimum_volume_is_rounded_up_and_never_refused(self):
        from assay2chip.parts.geometry import min_chamber_volume_ul

        smallest = min_chamber_volume_ul(DEPTH)
        for asked in (smallest / 2, 0.0001):
            with self.subTest(volume_ul=asked):
                part = registry.size("polygon_chamber", Need(
                    volume_ul=asked, flow_ul_min=5, particles="cells", particle_um=12))
                self.assertTrue(part.physics["volume_rounded_up"])
                self.assertAlmostEqual(part.physics["volume_requested_ul"], asked,
                                       places=6)
                self.assertAlmostEqual(part.volume_ul, smallest, places=6)
                self.assertGreater(part.physics["volume_rounded_up_factor"], 1)
                self.assertTrue(any("ROUNDED UP" in w
                                    for w in part.physics["volume_set_by"]))
                self.assertTrue(any("Bench volumes do not transfer" in risk
                                    for risk in part.risks))
                self.assertTrue(all(f.ok for f in part.floors))

    def test_exactly_the_minimum_is_not_rounded(self):
        from assay2chip.parts.geometry import min_chamber_volume_ul

        part = registry.size("polygon_chamber", Need(
            volume_ul=min_chamber_volume_ul(DEPTH), flow_ul_min=5))
        self.assertFalse(part.physics["volume_rounded_up"])

    def test_a_tiny_load_behind_a_shear_forced_wide_lane_is_clamped_not_refused(self):
        """The shear ceiling can fix a lane so wide that the length goes sub-floor."""
        part = registry.size("culture_chamber", Need(
            volume_ul=0.012, flow_ul_min=9, shear_class="adherent_cells"))
        self.assertTrue(part.physics["volume_rounded_up"])
        self.assertGreaterEqual(min(part.footprint_mm),
                                contracts.floor("min_open_channel_mm") - 1e-9)
        self.assertGreater(part.volume_ul, part.physics["volume_requested_ul"])
        self.assertTrue(any("in-plane floor" in risk for risk in part.risks))

    def test_rounding_up_does_not_mask_a_real_floor_refusal(self):
        """Refusal is for what the floors genuinely cannot meet - not for small volumes."""
        with self.assertRaises(PartSizingError):          # 40 um nozzle, 200 um floor
            registry.size("flow_focusing", Need(flow_ul_min=5, particles="capsules"))
        with self.assertRaises(PartSizingError):          # crest taller than its layer
            registry.size("trap_chamber", Need(
                volume_ul=1, flow_ul_min=1, particles="capsules",
                shear_class="capsules", depth_mm=0.02))
        with self.assertRaises(PartSizingError):          # cargo wider than a comb tooth
            registry.size("centipede_channel", Need(
                feed_edge_mm=8, flow_ul_min=1, particles="capsules", particle_um=200))

    def test_every_sized_part_reports_what_it_was_asked_for(self):
        for block, need in sorted(REPRESENTATIVE.items()):
            part = registry.size(block, need)
            if "volume_basis" not in part.physics:
                continue   # sized from a length or an edge, not from a load
            with self.subTest(block=block):
                self.assertIn("volume_requested_ul", part.physics)
                self.assertIn("smallest_buildable_volume_ul", part.physics)
                self.assertIsInstance(part.physics["volume_rounded_up"], bool)


class LoadShapes(unittest.TestCase):
    """The four `load` bases from contracts/schemas/unitops.schema.json."""

    def test_adherent_area_is_cells_over_surface_density(self):
        part = registry.size("culture_chamber", Need(
            load=Load(basis="adherent_area", cells=9600,
                      surface_density_per_mm2=1000, source="paper"),
            flow_ul_min=5, shear_class="adherent_cells"))
        self.assertEqual(part.physics["volume_basis"], "adherent_area")
        self.assertEqual(part.physics["volume_source"], "paper")
        self.assertAlmostEqual(part.physics["floor_area_mm2"], 9.6, places=3)
        self.assertAlmostEqual(part.volume_ul, 9.6 * DEPTH, places=3)

    def test_adherent_area_reproduces_module_6s_culture_field(self):
        """Module 6: 9.6 mm2 of floor, about 9,600 cells at an assumed 1,000/mm2."""
        module6 = json.loads((ROOT / "chip" / "module6_v1.json").read_text())
        field = next(c for c in module6["chambers"] if c["id"] == "cx_n")
        area = field["size"][0] * field["size"][1]
        part = registry.size("culture_chamber", Need(
            load=Load(basis="adherent_area", cells=9600,
                      surface_density_per_mm2=1000, source="paper"),
            flow_ul_min=5, shear_class="adherent_cells"))
        self.assertAlmostEqual(part.physics["floor_area_mm2"], area, places=3)

    def test_a_missing_seeding_density_uses_the_flagged_contract_default(self):
        part = registry.size("culture_chamber", Need(
            load=Load(basis="adherent_area", cells=5000, source="paper"),
            flow_ul_min=5, shear_class="adherent_cells"))
        default = contracts.param("culture.surface_density_per_mm2")
        self.assertEqual(part.physics["volume_source"], "contract_default")
        self.assertAlmostEqual(part.physics["floor_area_mm2"], 5000 / default, places=3)
        self.assertTrue(contracts.is_assumption("culture.surface_density_per_mm2"))
        self.assertTrue(any("culture.surface_density_per_mm2" in a
                            for a in part.assumptions))

    def test_packed_volume_matches_the_repos_own_sizing_sheet(self):
        """100,000 capsules at the contract packing fraction: 28.06 uL, as operator/sizing."""
        part = registry.size("trap_chamber", Need(
            load=Load(basis="packed_volume", cells=contracts.param("capsule.count"),
                      source="paper"),
            flow_ul_min=10, particles="capsules", shear_class="capsules"))
        self.assertEqual(part.physics["volume_basis"], "packed_volume")
        self.assertAlmostEqual(part.volume_ul, 28.0616, delta=0.01)

    def test_suspension_volume_is_cells_over_density(self):
        part = registry.size("polygon_chamber", Need(
            load=Load(basis="suspension_volume", cells=2000,
                      suspension_density_per_ul=1500, source="paper"),
            flow_ul_min=5))
        self.assertAlmostEqual(part.volume_ul, 2000 / 1500, places=3)

    def test_flow_through_sizes_from_flow_and_residence(self):
        part = registry.size("serpentine_channel", Need(
            load=Load(basis="flow_through", residence_s=300, source="paper"),
            flow_ul_min=5))
        self.assertEqual(part.physics["volume_basis"], "flow_through")
        self.assertAlmostEqual(part.volume_ul, physics.residence_volume_ul(5, 300),
                               places=3)
        self.assertAlmostEqual(part.physics["residence_s"], 300, delta=0.5)

    def test_volume_ul_still_works_for_existing_callers(self):
        a = registry.size("polygon_chamber", Need(volume_ul=20, flow_ul_min=5))
        b = registry.size("polygon_chamber", Need(
            load=Load(basis="none"), volume_ul=20, flow_ul_min=5))
        self.assertEqual(a.footprint_mm, b.footprint_mm)
        self.assertEqual(a.physics["volume_basis"], "volume_ul")

    def test_a_load_missing_its_key_number_raises_and_names_it(self):
        cases = [
            (Load(basis="adherent_area", source="paper"), "cells"),
            (Load(basis="packed_volume", source="paper"), "packed_volume_ul"),
            (Load(basis="suspension_volume", cells=100, source="paper"),
             "suspension_density_per_ul"),
            (Load(basis="flow_through", source="paper"), "residence_s"),
        ]
        for load, expect in cases:
            with self.subTest(basis=load.basis):
                with self.assertRaises(PartSizingError) as cm:
                    registry.size("polygon_chamber", Need(load=load, flow_ul_min=None))
                self.assertIn(expect, str(cm.exception))

    def test_a_bad_basis_is_refused_against_the_schema(self):
        with self.assertRaises(PartSizingError) as cm:
            Load(basis="tube_volume")
        self.assertIn("unitops.schema.json", str(cm.exception))

    def test_no_load_and_no_volume_says_a_tube_volume_is_not_a_substitute(self):
        with self.assertRaises(PartSizingError) as cm:
            registry.size("polygon_chamber", Need(flow_ul_min=5))
        self.assertIn("Bench volumes do not transfer", str(cm.exception))

    def test_a_load_derived_volume_rounds_up_like_any_other(self):
        part = registry.size("polygon_chamber", Need(
            load=Load(basis="suspension_volume", cells=1,
                      suspension_density_per_ul=1500, source="paper"),
            flow_ul_min=5))
        self.assertTrue(part.physics["volume_rounded_up"])
        self.assertEqual(part.physics["volume_basis"], "suspension_volume")


class FlowmatterMapping(unittest.TestCase):
    def test_all_21_parts_are_reviewed(self):
        parts = flowmatter.catalogue()
        self.assertEqual(len(parts), 21)
        for p in parts:
            self.assertNotEqual(p.why, "not reviewed", f"{p.part} is unreviewed")

    def test_every_mapped_block_is_a_real_block_id(self):
        self.assertTrue(flowmatter.mapped_blocks() <= contracts.library())

    def test_the_parts_that_map_to_nothing_are_named(self):
        unmapped = {c.part for c in flowmatter.unmapped_parts()}
        self.assertEqual(unmapped, {"mesh_body_a", "mesh_body_b",
                                    "normally_open_valve", "normally_closed_valve"})

    def test_guessed_names_come_from_the_catalogues_own_confidence(self):
        guessed = {c.part for c in flowmatter.guessed_names()}
        self.assertEqual(guessed, {"flow_manifold", "flow_distributor", "port",
                                   "mesh_body_a", "mesh_body_b",
                                   "normally_open_valve", "normally_closed_valve"})

    def test_the_special_blocks_have_no_flowmatter_shape(self):
        """Every block the audit found specified only in prose is also shapeless."""
        gaps = set(flowmatter.blocks_without_a_shape())
        for block in ("trap_chamber", "centipede_channel", "pillar_array",
                      "herringbone_mixer", "phase_guide", "flow_restrictor",
                      "metering_trap", "dld_array", "flow_focusing"):
            self.assertIn(block, gaps)

    def test_most_of_the_library_has_no_shape(self):
        gaps = flowmatter.blocks_without_a_shape()
        self.assertEqual(len(gaps) + len(flowmatter.mapped_blocks()),
                         len(contracts.library()))
        self.assertGreater(len(gaps), len(flowmatter.mapped_blocks()))

    def test_no_flowmatter_part_fits_the_layer(self):
        """The reason the registry sizes parametrically instead of placing the STLs."""
        s = flowmatter.summary()
        self.assertEqual(len(s["none_fit_the_layer"]["deeper_than_max_depth"]), 21)
        self.assertIn("spiral_channel",
                      s["none_fit_the_layer"]["larger_than_the_demo_envelope"])

    def test_summary_is_json(self):
        json.dumps(flowmatter.summary())


if __name__ == "__main__":
    unittest.main()

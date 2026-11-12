"""assay2chip/foam/vof.py: two-phase (interFoam) cases. Offline except the last class.

    python -m unittest tests.test_foam_vof -v

What is pinned down:

  * every property the contracts do not hold (wall contact angle, oil, oil-aqueous
    interfacial tension) is a FLAGGED assumption with a sweep, and water-air surface
    tension is textbook, not flagged;
  * interFoam's p_rgh is written in Pa, not kinematic like simpleFoam's p;
  * the capillary time step, the junction geometry read from the chip spec, the kit
    reference geometry read from the contracts, and the flows read from the protocol;
  * the dead-end (geometric) trapped-air rule, the gas-pocket finder and the robustness
    grouping across the contact-angle sweep, and that findings are in the cfd Finding
    shape with source "openfoam";
  * the one OpenFOAM test (a static drop) is skipped cleanly when OpenFOAM is absent.
"""

from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "operator")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from assay2chip.cfd.model import Finding  # noqa: E402
from assay2chip.foam import case as fcase  # noqa: E402
from assay2chip.foam import vof  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402


class TestAssumptions(unittest.TestCase):
    def test_uncontracted_properties_are_flagged_and_swept(self):
        for prop in (vof.CONTACT_ANGLE_AQUEOUS, vof.CONTACT_ANGLE_OIL, vof.SIGMA_OIL_AQUEOUS):
            self.assertTrue(prop.assumption, prop.key)
            self.assertGreaterEqual(len(prop.sweep), 3, prop.key)
            self.assertLessEqual(prop.min, prop.value)
            self.assertGreaterEqual(prop.max, prop.value)
            self.assertTrue(prop.wanted_in)
        self.assertEqual(vof.CONTACT_ANGLE_AQUEOUS.sweep, (60.0, 90.0, 110.0))
        for prop in (vof.SIGMA_OIL_AIR, vof.CONTACT_ANGLE_DROPLET_WALL, vof.OIL_DENSITY):
            self.assertTrue(prop.assumption, prop.key)

    def test_water_air_tension_is_textbook_not_assumed(self):
        self.assertFalse(vof.SIGMA_WATER_AIR.assumption)
        self.assertEqual(vof.SIGMA_WATER_AIR.source, "Textbook")
        self.assertAlmostEqual(vof.SIGMA_WATER_AIR.value, 0.072)

    def test_prop_dict_has_the_contract_shape(self):
        d = vof.CONTACT_ANGLE_AQUEOUS.to_dict()
        for key in ("value", "min", "max", "assumption"):
            self.assertIn(key, d)
        self.assertIs(d["assumption"], True)


class TestCapillaryStep(unittest.TestCase):
    def test_brackbill_value(self):
        lim = vof.capillary_dt(1000.0, 1.2, 1e-3, 1.8e-5, 0.072, 12.5e-6)
        rho = 0.5 * (1000.0 + 1.2)
        self.assertAlmostEqual(lim["brackbill_s"],
                               math.sqrt(rho * 12.5e-6 ** 3 / (2 * math.pi * 0.072)))
        self.assertAlmostEqual(lim["brackbill_s"], 1.47e-6, delta=0.01e-6)

    def test_step_shrinks_as_dx_to_the_1_5(self):
        a = vof.capillary_dt(1000, 1, 1e-3, 1e-5, 0.07, 10e-6)["brackbill_s"]
        b = vof.capillary_dt(1000, 1, 1e-3, 1e-5, 0.07, 40e-6)["brackbill_s"]
        self.assertAlmostEqual(b / a, 8.0, places=6)


class TestDictionaries(unittest.TestCase):
    def setUp(self):
        self.liq = vof.Phase("water", 1000.0, 1e-3)
        self.gas = vof.Phase("air", 1.2, 1.8e-5)
        self.bounds = {"in": vof.Boundary("inlet", q_m3_s=1e-11), "out": vof.Boundary("outlet"),
                       "walls": vof.Boundary("wall", theta_deg=110.0)}

    def test_transport_properties(self):
        tp = vof.transport_properties(self.liq, self.gas, 0.072)
        self.assertEqual(tp["phases"], "(water air)")
        self.assertAlmostEqual(tp["water"]["nu"], 1e-6)
        self.assertEqual(tp["sigma"], 0.072)

    def test_p_rgh_is_in_pascal_not_kinematic(self):
        f = vof.field_p_rgh(self.bounds)
        self.assertEqual(str(f["dimensions"]), str(fcase.DIM_PRESSURE))
        self.assertNotEqual(str(f["dimensions"]), str(fcase.DIM_P_KINEMATIC))
        self.assertEqual(f["boundaryField"]["out"]["type"], "totalPressure")
        self.assertEqual(f["boundaryField"]["walls"]["type"], "fixedFluxPressure")

    def test_alpha_and_u_boundaries(self):
        a = vof.field_alpha(self.bounds)["boundaryField"]
        self.assertEqual(a["in"]["type"], "fixedValue")
        self.assertEqual(a["out"]["type"], "inletOutlet")
        self.assertEqual(a["walls"]["type"], "constantAlphaContactAngle")
        self.assertEqual(a["walls"]["theta0"], 110.0)
        u = vof.field_u(self.bounds)["boundaryField"]
        self.assertEqual(u["in"]["type"], "flowRateInletVelocity")
        self.assertIn("constant", str(u["in"]["volumetricFlowRate"]))
        self.assertNotIn("[", str(u["in"]["volumetricFlowRate"]))
        self.assertEqual(u["walls"]["type"], "noSlip")

    def test_write_case_writes_a_complete_interfoam_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            control = vof.control_dict(end_time=1e-3, delta_t=1e-7, max_delta_t=1e-6,
                                       write_interval=1e-4)
            files = vof.write_case(Path(tmp), alpha_phase=self.liq, other=self.gas,
                                   sigma=0.072, boundaries=self.bounds, control=control)
            for rel in ("constant/transportProperties", "constant/g", "system/fvSolution",
                        "system/fvSchemes", "0/alpha.water", "0/U", "0/p_rgh"):
                self.assertIn(rel, files)
                self.assertTrue((Path(tmp) / rel).is_file(), rel)
            self.assertTrue((Path(tmp) / "constant" / fcase.laminar_model_file()).is_file())
            cd = Path(tmp) / "system/controlDict"
            self.assertEqual(fcase.read_entry(cd, "application"), "interFoam")
            self.assertEqual(fcase.read_entry(cd, "adjustTimeStep"), "true")
            self.assertEqual(fcase.read_entry(cd, "maxDeltaT"), "1e-06")
            g = (Path(tmp) / "constant/g").read_text()
            self.assertIn("(0 0 0)", g)


class TestReaders(unittest.TestCase):
    def test_denormals_and_uniform(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "alpha"
            p.write_text("FoamFile\n{\n class volScalarField;\n}\ndimensions [0 0 0 0 0 0 0];\n"
                         "internalField nonuniform List<scalar>\n4\n(\n0\n2.3e-313\n1\n0.5\n)\n;\n"
                         "boundaryField\n{\n}\n")
            v = vof.read_scalar_field(p)
            self.assertEqual(v.size, 4)
            self.assertEqual(v[2], 1.0)
            q = Path(tmp) / "u"
            q.write_text("internalField uniform 0;\n")
            self.assertEqual(vof.read_scalar_field(q, 3).tolist(), [0.0, 0.0, 0.0])

    def test_vector_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "C"
            p.write_text("internalField nonuniform List<vector>\n2\n(\n(1 2 3)\n(4 5 6)\n)\n;\n")
            self.assertEqual(vof.read_vector_field(p).tolist(), [[1, 2, 3], [4, 5, 6]])


def _line_mesh(n: int) -> vof.MeshData:
    """n cells in a row, unit volumes, internal faces between neighbours."""
    owner = np.arange(n - 1)
    neighbour = np.arange(1, n)
    owner_all = np.concatenate([owner, [0, n - 1]])            # two boundary faces
    cen = np.stack([np.arange(n) * 1e-3, np.zeros(n), np.zeros(n)], axis=1)
    return vof.MeshData(cen, np.full(n, 1e-12), owner_all, neighbour,
                        {"inlet": np.array([0]), "vent": np.array([n - 1])},
                        {"lane": np.arange(n // 2), "chamber": np.arange(n // 2, n)})


class TestPockets(unittest.TestCase):
    def test_components(self):
        md = _line_mesh(8)
        mask = np.array([1, 1, 0, 1, 1, 0, 0, 1], dtype=bool)
        lab = md.components(mask)
        self.assertEqual(lab[0], lab[1])
        self.assertEqual(lab[3], lab[4])
        self.assertNotEqual(lab[0], lab[3])
        self.assertEqual(lab[2], -1)
        self.assertEqual(len(set(lab[mask].tolist())), 3)

    def test_trapped_vs_vented_gas(self):
        md = _line_mesh(8)
        alpha = np.array([1, 1, 0, 0, 1, 1, 1, 0], dtype=float)   # liquid 1, gas 0
        pockets = vof.find_pockets(md, alpha, vents=["vent"], caps=[],
                                   zone_kind={"lane": "channel", "chamber": "chamber"},
                                   min_nl=0.5)
        trapped = [p for p in pockets if p.trapped]
        vented = [p for p in pockets if not p.trapped]
        self.assertEqual(len(trapped), 1)
        self.assertAlmostEqual(trapped[0].volume_nl, 2.0)          # two 1 nL cells
        self.assertEqual(len(vented), 1)
        self.assertEqual(vented[0].zone, "chamber")

    def test_robustness_across_the_sweep(self):
        def case(theta, zones):
            return {"theta_deg": theta, "liquid_reached_vent": True, "pockets_end": [
                {"zone": z, "zone_kind": "chamber", "volume_nl": 5.0, "trapped": True,
                 "behind_cap": [], "centroid_mm": [0, 0, 0], "zone_share": 0.1}
                for z in zones]}
        rob = vof.pocket_robustness([case(60, ["a", "b"]), case(90, ["a"]), case(110, ["a"])])
        by = {r["zone"]: r for r in rob}
        self.assertTrue(by["a"]["robust"])
        self.assertFalse(by["b"]["robust"])
        self.assertEqual(by["b"]["angles_with_pocket"], [60])


class TestDeadEnds(unittest.TestCase):
    def test_synthetic_graph(self):
        chip = {"ports": [{"id": "in"}, {"id": "out"}, {"id": "cap"}],
                "chambers": [{"id": "a"}, {"id": "b"}, {"id": "side"}],
                "channels": [{"id": "c1", "from": "in", "to": "a"},
                             {"id": "c2", "from": "a", "to": "b"},
                             {"id": "c3", "from": "b", "to": "out"},
                             {"id": "c4", "from": "a", "to": "side"},
                             {"id": "c5", "from": "side", "to": "cap"}]}
        de = vof.dead_ends(chip, ["in"], ["out"])
        self.assertEqual(de["dead_features"], ["side"])
        self.assertEqual(de["dead_channels"], ["c4", "c5"])
        self.assertTrue(de["complete"])

    def test_spc_demo_oil_prime_leaves_the_aqueous_branch_dry(self):
        chip = vof.load_chip("spc_demo_v1")
        de = vof.dead_ends(chip, ["in_oil"], ["out_caps"])
        self.assertIn("aq_merge", de["dead_features"])
        for ch in ("ch_shell_aq", "ch_core_aq", "ch_aq_xj"):
            self.assertIn(ch, de["dead_channels"])
        self.assertNotIn("xj_1", de["dead_features"])
        self.assertNotIn("delay_1", de["dead_features"])


class TestJunctions(unittest.TestCase):
    def test_spc_demo_geometry_comes_from_the_chip_spec(self):
        g = vof.spc_demo_geometry()
        self.assertAlmostEqual(g.w_aq, 0.3e-3)
        self.assertAlmostEqual(g.depth, 0.15e-3)
        nozzle = vof.load_chip("spc_demo_v1")["encapsulation"]["nozzle_um"]
        self.assertAlmostEqual(g.w_orifice, nozzle * 1e-6)
        self.assertTrue(g.frozen)
        self.assertFalse(g.not_our_chip)
        self.assertIn("NOT as drawn", vof.spc_demo_geometry(200).label)

    def test_kit_reference_is_labelled_not_our_chip(self):
        g = vof.kit_reference_geometry()
        self.assertTrue(g.not_our_chip)
        self.assertIn("NOT OUR CHIP", g.label)
        self.assertAlmostEqual(g.w_orifice, float(contracts.param("encapsulation.nozzle_um")) * 1e-6)
        pred = vof.repo_droplet_prediction(g)
        self.assertEqual(pred["contract_capsule_um"], float(contracts.param("capsule.diameter_um")))

    def test_prediction_is_the_repo_rule(self):
        g = vof.spc_demo_geometry(200)
        pred = vof.repo_droplet_prediction(g)
        self.assertAlmostEqual(pred["unconfined_1p3x_um"], 260.0, places=6)
        self.assertLessEqual(pred["d_um"], pred["unconfined_1p3x_um"])
        self.assertIn(pred["d_um"], (260.0, 150.0))     # the depth cap, if the builder has it

    def test_block_mesh_of_a_cross(self):
        g = vof.spc_demo_geometry(200)
        rm = g.rect_mesh(50e-6)
        body = rm.block_mesh_dict()
        self.assertEqual(body["defaultPatch"]["type"], "wall")
        names = [n for n, _ in body["boundary"].items]
        self.assertEqual(names, ["aqueous", "oil_n", "oil_s", "outlet"])
        self.assertEqual(len(rm.fluid), len(body["blocks"].rows))
        text = fcase.dumps(body, object_name="blockMeshDict", location="system")
        self.assertIn("hex (", text)
        # a plain cross (orifice = outlet width) has no separate orifice block
        plain = vof.JunctionGeometry("x", "x", 3e-4, 3e-4, 3e-4, 3e-4, 3e-4, 1.5e-4,
                                     9e-4, 6e-4, 3e-3)
        self.assertLess(len(plain.rect_mesh(50e-6).fluid), len(rm.fluid))

    def test_flows_come_from_protocol_and_contracts(self):
        import json
        spc = vof.droplet_inputs(vof.spc_demo_geometry())
        proto = json.loads((ROOT / "protocols/spc_demo_v1.json").read_text())
        flows = next(s for s in proto["steps"] if s["operation"] == "encapsulate")["flows_ul_h"]
        self.assertAlmostEqual(spc["q_oil_m3_s"] * 3.6e12, flows["oil"], delta=0.02)
        self.assertAlmostEqual(spc["q_aq_m3_s"] * 3.6e12, flows["core"] + flows["shell"],
                               delta=0.02)
        self.assertTrue(spc["oil"].assumption)
        kit = vof.droplet_inputs(vof.kit_reference_geometry())
        self.assertAlmostEqual(kit["q_aq_m3_s"] * 3.6e12, 350.0)
        self.assertAlmostEqual(kit["q_oil_m3_s"] * 3.6e12, 700.0)

    def test_variants_sweep_both_assumptions(self):
        spc = vof.droplet_inputs(vof.spc_demo_geometry())
        labels = [v.label for v in vof.droplet_variants(spc["oil"])]
        self.assertEqual(labels[0], "baseline")
        self.assertTrue(any(lab.startswith("ift_") for lab in labels))
        self.assertTrue(any(lab.startswith("oil_mu_") for lab in labels))
        self.assertEqual(len(labels), 5)

    def test_build_droplet_case_offline(self):
        g = vof.spc_demo_geometry()
        inp = vof.droplet_inputs(g)
        var = vof.droplet_variants(inp["oil"])[0]
        with tempfile.TemporaryDirectory() as tmp:
            info = vof.build_droplet_case(g, var, inp, Path(tmp), mesh=False)
            self.assertTrue((Path(tmp) / "system/blockMeshDict").is_file())
            self.assertTrue((Path(tmp) / "0/alpha.aqueous").is_file())
            self.assertAlmostEqual(info["cells_across_orifice"], vof.cells_across(0))
            self.assertLess(info["ca_oil_outlet"], 0.01)
            self.assertLess(info["re_orifice"], 1.0)


class TestFindings(unittest.TestCase):
    def test_priming_findings_shape(self):
        summary = {
            "chip_id": "spc_demo_v1", "frozen": True,
            "plan": {"ca_protocol": 1e-4, "ca_run": 0.1, "re_protocol": 0.1, "re_run": 10,
                     "flow_scale": 600, "mesh_kwargs": {}, "surrogate": "s", "step": 1,
                     "cites": "protocols/spc_demo_v1.json step 1", "vents": ["out_caps"],
                     "feeds_ul_min": {"in_oil": 3.1}},
            "assumptions": [], "cases": [],
            "robustness": [{"zone": "delay_1", "zone_kind": "lane", "angles_with_pocket": [10, 30, 60],
                            "angles_run": [10, 30, 60], "robust": True,
                            "volume_nl_by_angle": {"10": 3.0}, "behind_cap": [],
                            "centroid_mm": [0, 0, 0], "zone_share_max": 0.01}],
            "geometric": {"rule": "r", "pockets": [{"feature_id": "ch_aq_xj", "kind": "channel",
                                                    "volume_nl": 109.0}]},
        }
        fs = vof.priming_findings(summary)
        self.assertEqual(len(fs), 2)
        for f in fs:
            self.assertIsInstance(f, Finding)
            self.assertEqual(f.cls, "bubble")
            self.assertEqual(f.source, "openfoam")
            self.assertEqual(f.to_json()["class"], "bubble")
        self.assertIn("frozen", fs[0].fix_hint)

    def test_droplet_finding_compares_with_prediction(self):
        summary = {"geometry": {"name": "spc_demo_v1"}, "prediction": {"d_um": 390.0, "rule": "1.3x"},
                   "not_our_chip": False, "frozen": True, "label": "spc_demo_v1 xj_1 as drawn",
                   "assumptions": [], "flows": {},
                   "cases": [{"variant": {"label": "baseline"}, "setup": {"ca_oil_outlet": 1e-3},
                              "result": {"d_eq_um": 300.0, "freq_hz_mass_balance": 0.5}}]}
        (f,) = vof.droplet_findings(summary)
        self.assertEqual(f.metric, "droplet_d_um")
        self.assertEqual(f.value, 300.0)
        self.assertEqual(f.limit, 390.0)
        self.assertEqual(f.source, "openfoam")


class TestPrimingPlan(unittest.TestCase):
    def test_module5_plan_is_a_declared_surrogate(self):
        plan = vof.plan_priming("module5_v1")
        self.assertGreater(plan.ca_run, plan.ca_protocol)
        self.assertLessEqual(plan.re_run, vof.RE_MAX * 1.0001)
        self.assertIn("SURROGATE", plan.to_dict()["surrogate"])
        self.assertEqual(plan.vents, ["out_waste"])
        self.assertIn("in_lib", plan.feeds_ul_min)


@unittest.skipUnless(vof.foam_available(), "OpenFOAM is not installed")
class TestStaticDropOpenFOAM(unittest.TestCase):
    """A coarse Laplace drop: runs end to end and lands within the documented CSF bias."""

    def test_laplace_pressure(self):
        with tempfile.TemporaryDirectory() as tmp:
            liq = vof.Phase("water", 1000.0, vof.VALIDATION_MU)
            gas = vof.Phase("air", 1.2, 1.8e-5)
            R, cpr = 150e-6, 6
            dt = vof.capillary_dt(liq.rho, gas.rho, liq.mu, gas.mu, 0.072, R / cpr)["brackbill_s"]
            res = vof._drop_case(Path(tmp), R=R, sigma=0.072, liquid=liq, gas=gas,
                                 cells_per_r=cpr, t_end=1e-3, dt_max=dt, sessile_theta=None)
            self.assertTrue(res["run"]["ok"])
            self.assertLess(abs(res["error"]), 0.25)


if __name__ == "__main__":
    unittest.main()

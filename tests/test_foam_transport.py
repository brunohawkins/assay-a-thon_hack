"""assay2chip/foam/transport.py: 3D mean age, dead zones and washout.

    python -m unittest tests.test_foam_transport -v
    FOAM_SKIP_INTEGRATION=1 python -m unittest tests.test_foam_transport   # offline only

OFFLINE - the parts that decide the numbers, on hand-built inputs:
  * the ascii readers (scalar, vector, uniform, boundary, cellZones);
  * the flow-ordered age sweep against hand-computed ages, with a loop to break and a
    dead-end that must come out UNREACHABLE rather than as a division by zero;
  * `mean_age_3d` on a synthetic channel, where the flux-weighted outlet age must equal
    reached volume / throughput exactly;
  * dead-zone fractions per cell and per plan column, and the inflow into a zone.

INTEGRATION (OpenFOAM; skipped cleanly when absent) - a straight duct, simpleFoam, then
the age (outlet age = V/Q) and a scalarTransportFoam washout that must clear the duct.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "operator")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from assay2chip.foam import transport as ft  # noqa: E402

SKIP_INTEGRATION = bool(os.environ.get("FOAM_SKIP_INTEGRATION"))


def needs_openfoam(test):
    if SKIP_INTEGRATION:
        return unittest.skip("FOAM_SKIP_INTEGRATION is set")(test)
    from assay2chip.foam import runner

    if not runner.available():
        return unittest.skip(f"OpenFOAM is not installed. {runner.INSTALL_COMMAND}")(test)
    return test


HEADER = """FoamFile
{
    version     2.0;
    format      ascii;
    class       %s;
    object      %s;
}
"""


class Readers(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def write(self, name, text):
        p = self.tmp / name
        p.write_text(text)
        return p

    def test_scalar_and_vector_fields(self):
        p = self.write("T", HEADER % ("volScalarField", "T") + """
dimensions [0 0 0 0 0 0 0];
internalField   nonuniform List<scalar>
3
(
1
2.5e-3
-4
)
;
boundaryField
{
    inlet
    {
        type            fixedValue;
        value           uniform 0;
    }
    outlet
    {
        type            calculated;
        value           nonuniform List<scalar> 2(1.5 -2);
    }
}
""")
        np.testing.assert_allclose(ft.read_internal_field(p), [1, 2.5e-3, -4])
        np.testing.assert_allclose(ft.read_patch_values(p, "inlet", 4), [0, 0, 0, 0])
        np.testing.assert_allclose(ft.read_patch_values(p, "outlet", 2), [1.5, -2])
        self.assertIsNone(ft.read_patch_values(p, "nope", 2))
        u = self.write("U", HEADER % ("volVectorField", "U") + """
internalField   nonuniform List<vector>
2
(
(1 2 3)
(4 5 6e-1)
)
;
""")
        np.testing.assert_allclose(ft.read_internal_field(u, 3, 2), [[1, 2, 3], [4, 5, 0.6]])
        uni = self.write("V", HEADER % ("volScalarField", "V") + "internalField uniform 7;\n")
        np.testing.assert_allclose(ft.read_internal_field(uni, 1, 3), [7, 7, 7])

    def test_boundary_and_cell_zones(self):
        b = self.write("boundary", HEADER % ("polyBoundaryMesh", "boundary") + """
2
(
    p_in
    {
        type            patch;
        nFaces          4;
        startFace       10;
    }
    walls
    {
        type            wall;
        nFaces          6;
        startFace       14;
    }
)
""")
        got = ft.read_boundary(b)
        self.assertEqual([g["name"] for g in got], ["p_in", "walls"])
        self.assertEqual(got[1]["startFace"], 14)
        z = self.write("cellZones", HEADER % ("regIOobject", "cellZones") + """
2
(
chamber
{
    type            cellZone;
    cellLabels      List<label>
3
(
0
2
3
)
;
}
line
{
    type            cellZone;
    cellLabels      List<label>
1
(
1
)
;
}
)
""")
        names, zone = ft.read_cell_zones(z, 5)
        self.assertEqual(names, ["chamber", "line"])
        self.assertEqual(zone.tolist(), [0, 1, 0, 0, -1])


class AgeSweep(unittest.TestCase):
    def test_chain_and_branch_against_hand_computed_ages(self):
        # 0 -> {1, 3} -> 2 -> out, unit volumes, 1 m3/s through
        src, dst = np.array([0, 1, 0, 3]), np.array([1, 2, 3, 2])
        w = np.array([0.5, 0.5, 0.5, 0.5])
        age, info = ft.age_sweep(4, src, dst, w, np.ones(4), np.array([0, 0, 1.0, 0]),
                                 qtol=1e-12)
        np.testing.assert_allclose(age, [1, 3, 4, 3])
        self.assertEqual(info["cells_left_in_loops"], 0)

    def test_dead_end_is_unreachable_not_a_division_by_zero(self):
        src, dst, w = np.array([0]), np.array([1]), np.array([1.0])
        age, _ = ft.age_sweep(3, src, dst, w, np.ones(3), np.array([0, 1.0, 0]), qtol=1e-12)
        self.assertTrue(np.isfinite(age[:2]).all())
        self.assertTrue(np.isinf(age[2]))

    def test_round_off_loop_is_broken_and_reported(self):
        # 0 -> 1 -> 2 -> out, plus a 1e-15 back-flow 2 -> 1 that closes a loop
        src, dst = np.array([0, 1, 2]), np.array([1, 2, 1])
        w = np.array([1.0, 1.0, 1e-15])
        age, info = ft.age_sweep(3, src, dst, w, np.ones(3), np.array([0, 0, 1.0]),
                                 qtol=1e-20)
        np.testing.assert_allclose(age, [1, 2, 3], rtol=1e-9)
        self.assertGreaterEqual(info["dropped_faces"], 1)
        self.assertLess(info["dropped_flux_fraction"], 1e-12)


def synthetic_fields(n=10, q=1e-12, slow_cell=None):
    """A 1 x n channel of 1 nL cells along x, fed at cell 0 and drained at cell n-1."""
    owner = np.concatenate([np.arange(n - 1), [0, n - 1] + [i for i in range(n)]])
    neighbour = np.arange(1, n)
    phi = np.full(n - 1, q)
    boundary = [{"name": "p_in", "type": "patch", "nFaces": 1, "startFace": n - 1},
                {"name": "p_out", "type": "patch", "nFaces": 1, "startFace": n},
                {"name": "walls", "type": "wall", "nFaces": n, "startFace": n + 1}]
    phi_b = {"p_in": np.array([-q]), "p_out": np.array([q]), "walls": np.zeros(n)}
    U = np.zeros((n, 3))
    U[:, 0] = 1e-3
    if slow_cell is not None:
        U[slow_cell, 0] = 1e-6
    V = np.full(n, 1e-12)
    C = np.stack([np.arange(n) * 1e-4 + 5e-5, np.full(n, 5e-5), np.full(n, 1.5e-4)], 1)
    ij = np.stack([np.arange(n), np.zeros(n, dtype=int)], 1)
    zone = np.zeros(n, dtype=np.int32)
    return ft.Fields3D(n, owner, neighbour, phi, boundary, phi_b, U, V, C, ["ch"], zone,
                       ij, 3e-4)


class MeanAgeAndZones(unittest.TestCase):
    def test_outlet_age_is_volume_over_flow(self):
        f = synthetic_fields()
        a = ft.mean_age_3d(f)
        self.assertAlmostEqual(a.outlet_mean_age_s, 10e-12 / 1e-12, places=9)
        self.assertLess(a.consistency_error, 1e-9)
        np.testing.assert_allclose(a.age_s, np.arange(1, 11))

    def test_dead_zone_fractions_cell_and_column(self):
        f = synthetic_fields(slow_cell=4)
        a = ft.mean_age_3d(f)
        dz = ft.dead_zones_3d(f, a.age_s, "ch")
        self.assertAlmostEqual(dz["low_speed_volume_fraction_cells"], 0.1)
        self.assertAlmostEqual(dz["dead_volume_fraction_columns"], 0.1)
        self.assertEqual(dz["unreachable_volume_fraction"], 0.0)

    def test_zone_inflow(self):
        f = synthetic_fields(q=2e-12)
        self.assertAlmostEqual(ft.zone_inflow_m3_s(f, "ch"), 2e-12)

    def test_fidelity_and_frozen_framing(self):
        self.assertEqual(ft.fidelity("fast").cell_budget, ft.FAST_CELL_BUDGET)
        self.assertGreater(ft.fidelity("high").cell_budget, ft.FAST_CELL_BUDGET)
        self.assertIn("FULL-SPEC", ft._fix_frame("module4_v1"))
        self.assertNotIn("FROZEN", ft._fix_frame("cell_prep_v1"))
        with self.assertRaises(ValueError):
            ft.fidelity("medium")


def duct_steady(tmp: Path, *, L=4e-3, w=0.6e-3, h=0.3e-3, q_ul_min=2.0):
    """A straight duct in one cellZone, solved with simpleFoam, as a SteadyCase."""
    from assay2chip.foam import case, runner

    cd = tmp / "duct"
    v = [(0, 0, 0), (L, 0, 0), (L, w, 0), (0, w, 0), (0, 0, h), (L, 0, h), (L, w, h), (0, w, h)]
    nx, ny, nz = 40, 12, 8
    bm = case.block_mesh_dict(v, [f"hex (0 1 2 3 4 5 6 7) duct ({nx} {ny} {nz}) simpleGrading (1 1 1)"], [
        ("p_in", {"type": "patch", "faces": case.Table([case.Verbatim("(0 4 7 3)")])}),
        ("p_out", {"type": "patch", "faces": case.Table([case.Verbatim("(1 2 6 5)")])}),
        ("walls", {"type": "wall", "faces": case.Table([
            case.Verbatim("(0 1 5 4)"), case.Verbatim("(3 7 6 2)"),
            case.Verbatim("(0 3 2 1)"), case.Verbatim("(4 5 6 7)")])}),
    ])
    case.write_dict(cd / "system" / "blockMeshDict", bm)
    fluid = case.fluid_for(37.0)
    patches = {"p_in": case.PatchBC("feed", q_m3_s=q_ul_min / 6e10), "p_out": case.PatchBC("vent"),
               "walls": case.PatchBC("wall")}
    case.build_case(cd, fluid=fluid, patches=patches, functions={
        "p_p_in": case.patch_mean_pressure("p_in"), "p_p_out": case.patch_mean_pressure("p_out")})
    runner.run(cd, "blockMesh")
    runner.run(cd, "simpleFoam")
    td = ft.latest_time(cd)
    for util in ("writeCellVolumes", "writeCellCentres"):
        runner.run(cd, "postProcess", args=["-func", util, "-time", td.name], steady=False,
                   log_name=f"log.{util}")
    meta = {"chip_id": "duct", "cells": nx * ny * nz,
            "xy": {"dxy_mm": L / nx * 1e3, "origin_mm": [0.0, 0.0]},
            "z": {"depth_mm": h * 1e3, "cells": nz}, "weirs": [], "patches": []}
    return ft.SteadyCase("duct", 1, cd, td, meta, fluid, patches, ["p_in"], ["p_out"], {}, {})


class Integration(unittest.TestCase):
    @needs_openfoam
    def test_duct_age_and_washout(self):
        tmp = Path(tempfile.mkdtemp())
        steady = duct_steady(tmp)
        f = ft.read_fields(steady)
        a = ft.mean_age_3d(f)
        # flux-weighted outlet age = V / Q, exactly, for any steady field
        self.assertLess(a.consistency_error, 1e-3)
        self.assertEqual(a.unreachable_volume_fraction, 0.0)
        v = float(f.V.sum())
        q = ft.zone_inflow_m3_s(f, "duct")
        self.assertAlmostEqual(q * 6e10, 2.0, places=3)
        w = ft.run_washout(steady, f, ["duct"], t_end_s=3 * v / q, steps=48,
                           diffusivity_m2_s=4e-10, n_proc=1)["duct"]
        # a duct is near plug flow: about half exchanged at 0.5 CV, well past 90% at 2
        self.assertGreater(w.fraction_at_cv(0.5), 0.4)
        self.assertGreater(w.fraction_at_cv(2.0), 0.9)
        self.assertTrue(np.all(np.diff(w.fraction) >= -1e-6))


if __name__ == "__main__":
    unittest.main()

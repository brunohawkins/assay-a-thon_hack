"""assay2chip/foam/flow.py and validate_foam.py: the 3D flow solve and its evidence.

    python -m unittest tests.test_foam_flow -v
    python -m unittest discover -s tests -v

EVERY TEST HERE RUNS WITHOUT OPENFOAM. That is a requirement, not a convenience
(`assay2chip/foam/README.md`: "Tests that need OpenFOAM are skipped cleanly when it is
absent"), and it is achievable because `flow.py` is split so that the physics and the
post-processing are PURE: readers take paths, post-processing takes dataclasses, and only
`build_case` / `run_case` / `run_step` touch a solver. So the parts that decide what a
number MEANS are all testable on synthetic fields, which is where the mistakes live.

What is covered, and why each one is worth a test:

  the .dat reader       `mag(U)` and `(x y z)` both contain parentheses and mean different
                        things; the parallel `fieldMinMax` layout adds an unnamed
                        processor column. Both have already produced a wrong answer in
                        this module's history, so both are pinned here.
  stale-file resolution OpenFOAM writes `name_0.dat` BESIDE an existing `name.dat` instead
                        of overwriting it, so the suffixed file is the NEWER one. Reading
                        the wrong one reports a previous run's numbers as this run's.
  polyMesh + field      a hand-written four-cell mesh with one `walls` patch, so the
                        owner-cell -> cellZone attribution of wall shear is checked against
                        an answer that can be worked out by eye.
  kinematic -> Pa       the factor of ~1000 that an incompressible solver's `p` and
                        `wallShearStress` carry. A test that would pass either way would
                        be worthless, so the expected values are written in Pa.
  mass balance, split   signs: OpenFOAM's `phi` is positive OUT of the domain and the rest
                        of the repo is positive IN.
  solver choice         Re > 1, a named transient event and a step shorter than the
                        viscous settling time each have to force pimpleFoam, and a long
                        Stokes step has to stay steady.
  shear scoping         the three ceilings are NOT interchangeable and this repo has
                        picked the wrong one at least six times. The tests assert that
                        `findings_for` goes through `cfd.analyse.shear_scope` and so
                        judges an adherent-cell chamber at 0.01, a pre-lysis rinse at 0.3,
                        and a capsule chamber NOT AT ALL.
  grid sensitivity      the GCI arithmetic, including the case where it is larger than a
                        cross-validation disagreement.
  attribution           every branch of `validate_foam.attribute`: agreement, inertia,
                        side walls, staircase, slit, and unexplained - and that an
                        unexplained row stays unexplained instead of being blamed on
                        whichever cause is nearby.
"""

from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.cfd.model import Mesh, Node, Edge, PortState, Scenario  # noqa: E402
from assay2chip.foam import flow  # noqa: E402
from assay2chip.foam import validate_foam as vf  # noqa: E402

RHO = 1000.0            # the contract density; the tests assert the x1000 explicitly
MU = 8e-4


# ======================================================================================
# the .dat reader
# ======================================================================================


class TestDatReader(unittest.TestCase):
    def test_scalar_columns(self):
        table = flow.parse_dat(
            "# Region type : patch inlet\n"
            "# Faces         : 256\n"
            "# Area          : 2.40000000e-07\n"
            "# Time          \tsum(phi)\n"
            "25              \t-1.60000000e-11\n"
            "50              \t-1.66666667e-11\n"
        )
        self.assertEqual(table.columns, ["Time", "sum(phi)"])
        self.assertEqual(len(table.rows), 2)
        # the LAST row is the converged one
        self.assertAlmostEqual(table.value("sum(phi)"), -1.66666667e-11)
        self.assertAlmostEqual(table.meta_float("Area"), 2.4e-07)
        self.assertEqual(table.meta["Region type"], "patch inlet")

    def test_label_with_parentheses_is_not_split(self):
        """`mag(U)` is a LABEL. Splitting on its brackets shifts every later column."""
        table = flow.parse_dat(
            "# Time          \tfield           \tmin  \tlocation(min)\tmax\tlocation(max)\n"
            "50   mag(U)   0   (0.0158 0.0027 0.0003)   5.4777e-04   (0.0174 0.0082 0.00025)\n"
            "50   p   -1.0e-06   (0.0174 0.00825 0.000295)   9.0865e-04   (0.0029 0.0083 0.000295)\n"
        )
        self.assertIn("field", table.columns)
        self.assertEqual([r[1] for r in table.rows], ["mag(U)", "p"])
        self.assertAlmostEqual(table.select("field", "mag(U)").group_anchor(1), 5.4777e-04)
        self.assertAlmostEqual(table.select("field", "p").group_anchor(1), 9.0865e-04)
        self.assertAlmostEqual(table.select("field", "p").group_anchor(0), -1.0e-06)

    def test_parallel_fieldminmax_has_an_unnamed_processor_column(self):
        """In parallel, `location: true` adds a processor id the header does not name.

        A reader that looks up the column called `max` gets the processor id of the
        MINIMUM - a small integer that looks like a plausible velocity. `group_anchor`
        reads the scalar before each location group instead, which is layout-independent.
        """
        serial = flow.parse_dat(
            "# Time \tfield \tmin \tlocation(min)\tmax\tlocation(max)\n"
            "50  mag(U)  0  (1 2 3)  5.4777e-04  (4 5 6)\n"
        )
        parallel = flow.parse_dat(
            "# Time \tfield \tmin \tlocation(min)\tmax\tlocation(max)\n"
            "50  mag(U)  0  (1 2 3)  0  5.3446e-04  (4 5 6)  5\n"
        )
        self.assertAlmostEqual(serial.select("field", "mag(U)").group_anchor(1), 5.4777e-04)
        self.assertAlmostEqual(parallel.select("field", "mag(U)").group_anchor(1), 5.3446e-04)
        # and the naive lookup really would have been wrong on the parallel file
        self.assertEqual(parallel.select("field", "mag(U)").value("max"), 0.0)

    def test_vector_columns_get_a_magnitude(self):
        table = flow.parse_dat(
            "# Time \tmin \tmax\n"
            "1 (0 0 0) (1 2 2)\n"
        )
        self.assertIn("max_mag", table.columns)
        self.assertAlmostEqual(table.value("max_mag"), 3.0)

    def test_empty_file_is_not_an_error(self):
        table = flow.parse_dat("# Time \tsum(phi)\n")
        self.assertEqual(table.last(), {})
        self.assertIsNone(table.value("sum(phi)"))
        with self.assertRaises(flow.FlowError):
            table.column("nope")


class TestFindDatFiles(unittest.TestCase):
    """OpenFOAM's duplicate-file convention, which is the opposite of what it looks like."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.case = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rel: str, text: str, mtime: float):
        import os
        path = self.case / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        os.utime(path, (mtime, mtime))
        return path

    def test_suffixed_file_is_the_newer_one(self):
        self._write("postProcessing/flow_p_in/0/surfaceFieldValue.dat",
                    "# Time \tsum(phi)\n50 -1.0e-11\n", 1000.0)
        newer = self._write("postProcessing/flow_p_in/0/surfaceFieldValue_0.dat",
                            "# Time \tsum(phi)\n50 -2.0e-11\n", 2000.0)
        found = flow.find_dat_files(self.case)
        self.assertEqual(found["flow_p_in/surfaceFieldValue"], newer)
        self.assertAlmostEqual(flow.read_dat(found["flow_p_in/surfaceFieldValue"])
                               .value("sum(phi)"), -2.0e-11)

    def test_highest_time_directory_wins(self):
        self._write("postProcessing/fieldMinMax/0/fieldMinMax.dat",
                    "# Time \tfield \tmin \tlocation(min)\tmax\tlocation(max)\n"
                    "0 p 0 (0 0 0) 1 (0 0 0)\n", 1000.0)
        late = self._write("postProcessing/fieldMinMax/120/fieldMinMax.dat",
                           "# Time \tfield \tmin \tlocation(min)\tmax\tlocation(max)\n"
                           "120 p 0 (0 0 0) 7 (0 0 0)\n", 1000.0)
        found = flow.find_dat_files(self.case)
        self.assertEqual(found["fieldMinMax/fieldMinMax"], late)

    def test_no_postprocessing_directory_is_empty_not_an_error(self):
        self.assertEqual(flow.find_dat_files(self.case), {})


# ======================================================================================
# polyMesh and field readers, on a mesh small enough to check by eye
# ======================================================================================


FOAM_HEAD = """FoamFile
{
    version     2.0;
    format      ascii;
    class       %s;
    object      %s;
}
"""


class TestPolyMeshReaders(unittest.TestCase):
    """A four-cell chip: two cells in zone `chamber`, two in zone `waste`.

    The `walls` patch has four faces, one per cell, starting at global face 10. So
    face 10 is owned by cell 0, face 11 by cell 1, face 12 by cell 2 and face 13 by
    cell 3 - and the per-zone maxima can be worked out by hand.
    """

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.case = Path(self.tmp.name)
        poly = self.case / "constant" / "polyMesh"
        poly.mkdir(parents=True)
        # 10 internal faces then the 4 wall faces; only the owner list matters here
        owners = list(range(10)) + [0, 1, 2, 3]
        (poly / "owner").write_text(
            (FOAM_HEAD % ("labelList", "owner")) + f"{len(owners)}\n(\n"
            + "\n".join(str(o) for o in owners) + "\n)\n"
        )
        (poly / "boundary").write_text(
            (FOAM_HEAD % ("polyBoundaryMesh", "boundary")) + """1
(
    walls
    {
        type            wall;
        nFaces          4;
        startFace       10;
    }
)
"""
        )
        (poly / "cellZones").write_text(
            (FOAM_HEAD % ("regIOobject", "cellZones")) + """2
(
    chamber
    {
        type cellZone;
        cellLabels List<label> 2(0 1);
    }
    waste
    {
        type cellZone;
        cellLabels List<label> 2(2 3);
    }
)
"""
        )
        time_dir = self.case / "100"
        time_dir.mkdir()
        # kinematic wall shear, m2/s2. Zone `chamber` peaks at 3e-6, `waste` at 9e-6.
        (time_dir / "wallShearStress").write_text(
            (FOAM_HEAD % ("volVectorField", "wallShearStress"))
            + """dimensions      [0 2 -2 0 0 0 0];
internalField   uniform (0 0 0);
boundaryField
{
    walls
    {
        type            calculated;
        value           nonuniform List<vector>
4
(
(1e-06 0 0)
(3e-06 0 0)
(9e-06 0 0)
(2e-06 0 0)
)
;
    }
}
"""
        )
        (time_dir / "U").write_text(
            (FOAM_HEAD % ("volVectorField", "U"))
            + """dimensions      [0 1 -1 0 0 0 0];
internalField   nonuniform List<vector>
4
(
(0.001 0 0)
(0.002 0 0)
(0.010 0 0)
(0.004 0 0)
)
;
boundaryField
{
    walls { type noSlip; }
}
"""
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_boundary_and_zones(self):
        poly = self.case / "constant" / "polyMesh"
        boundary = flow.read_boundary(poly / "boundary")
        self.assertEqual(boundary["walls"]["nFaces"], 4)
        self.assertEqual(boundary["walls"]["startFace"], 10)
        self.assertEqual(boundary["walls"]["type"], "wall")
        zones = flow.read_cell_zones(poly / "cellZones")
        self.assertEqual(zones, {"chamber": [0, 1], "waste": [2, 3]})
        self.assertEqual(flow.read_label_list(poly / "owner")[10:], [0, 1, 2, 3])

    def test_latest_time_dir(self):
        self.assertEqual(flow.latest_time_dir(self.case).name, "100")

    def test_wall_shear_attributed_by_owner_cell(self):
        """One `walls` patch, two zones: the owner cell decides which zone a face is in.

        This is the only route to a per-feature shear number when the mesh gives the chip
        a single wall patch, which is what `assay2chip/foam/README.md` specifies.
        """
        per_zone, how, notes = flow.wall_shear_by_zone(self.case)
        self.assertEqual(how, "owner_cell_to_cellzone")
        self.assertAlmostEqual(per_zone["chamber"], 3e-06)
        self.assertAlmostEqual(per_zone["waste"], 9e-06)
        self.assertAlmostEqual(per_zone["__chip__"], 9e-06)
        self.assertEqual(notes, [])

    def test_zone_internal_max(self):
        per_zone = flow.zone_internal_max(self.case, "U")
        self.assertAlmostEqual(per_zone["chamber"], 0.002)
        self.assertAlmostEqual(per_zone["waste"], 0.010)
        self.assertAlmostEqual(per_zone["__chip__"], 0.010)

    def test_missing_wall_shear_field_is_a_loud_error(self):
        (self.case / "100" / "wallShearStress").unlink()
        with self.assertRaises(flow.FlowError) as ctx:
            flow.wall_shear_by_zone(self.case)
        self.assertIn("wallShearStress", str(ctx.exception))

    def test_binary_field_is_refused(self):
        with self.assertRaises(flow.FlowError):
            flow.parse_vol_field(
                "FoamFile{ format binary; class volScalarField; }\ninternalField uniform 0;",
                "p",
            )


class TestPerZoneWallPatches(unittest.TestCase):
    """When mesh.py names its wall patches per feature, they are used directly."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.case = Path(self.tmp.name)
        (self.case / "constant" / "polyMesh").mkdir(parents=True)
        time_dir = self.case / "5"
        time_dir.mkdir()
        (time_dir / "wallShearStress").write_text(
            (FOAM_HEAD % ("volVectorField", "wallShearStress"))
            + """internalField   uniform (0 0 0);
boundaryField
{
    walls_entry { type calculated; value nonuniform List<vector> 2 ( (5e-06 0 0) (1e-06 0 0) ); }
    walls_dev   { type calculated; value nonuniform List<vector> 2 ( (2e-06 0 0) (3e-06 0 0) ); }
}
"""
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_per_zone_patches_skip_the_owner_lookup(self):
        per_zone, how, _ = flow.wall_shear_by_zone(self.case)
        self.assertEqual(how, "per_zone_wall_patches")
        self.assertAlmostEqual(per_zone["entry"], 5e-06)
        self.assertAlmostEqual(per_zone["dev"], 3e-06)


# ======================================================================================
# post-processing on synthetic fields
# ======================================================================================


def a_plan(**kw) -> flow.CasePlan:
    base = dict(chip_id="synthetic", step=1, solver="simpleFoam",
                feeds=["p_in"], vents=["p_out"], capped=["p_cap"],
                q_in_ul_min=60.0, mu_pa_s=MU, rho_kg_m3=RHO, nu_m2_s=MU / RHO)
    base.update(kw)
    return flow.CasePlan(**base)


def raw_two_outlets() -> flow.RawFields:
    """1e-9 m3/s (60 uL/min) in at `p_in`, out 75/25 through `p_out` and `p_out2`."""
    return flow.RawFields(
        patches=[
            # phi is positive OUT of the domain, so an inlet's phi is NEGATIVE
            flow.PatchSample(port="p_in", role="feed", flow_m3_s=1.0e-9,
                             p_mean_kinematic=2.0e-3, area_m2=2.4e-7),
            flow.PatchSample(port="p_out", role="vent", flow_m3_s=-0.75e-9,
                             p_mean_kinematic=0.0, area_m2=2.4e-7),
            flow.PatchSample(port="p_out2", role="vent", flow_m3_s=-0.25e-9,
                             p_mean_kinematic=0.0, area_m2=2.4e-7),
            flow.PatchSample(port="p_cap", role="cap", flow_m3_s=0.0),
        ],
        zones=[
            flow.ZoneSample(zone="culture", wall_shear_max_kinematic=1.5e-6,
                            wall_shear_source="owner_cell_to_cellzone",
                            u_max_m_s=2.0e-4, width_m=2.0e-3, depth_m=3.0e-4),
            flow.ZoneSample(zone="feed_line", wall_shear_max_kinematic=2.0e-5,
                            wall_shear_source="owner_cell_to_cellzone",
                            u_max_m_s=1.0e-2, width_m=2.0e-4, depth_m=3.0e-4),
        ],
        p_max_kinematic=2.1e-3,
        u_max_m_s=1.0e-2,
        wall_shear_max_kinematic=2.0e-5,
        cells=12345,
        converged=True,
    )


class TestPostProcessing(unittest.TestCase):
    def test_mass_balance_signs(self):
        balance = flow.mass_balance(raw_two_outlets())
        self.assertAlmostEqual(balance["q_in_ul_min"], 60.0, places=6)
        self.assertAlmostEqual(balance["q_out_ul_min"], 60.0, places=6)
        self.assertAlmostEqual(balance["closure"], 0.0, places=12)
        self.assertAlmostEqual(balance["per_patch_ul_min"]["p_cap"], 0.0)

    def test_mass_balance_reports_a_leak(self):
        raw = raw_two_outlets()
        raw.patches[1].flow_m3_s = -0.70e-9        # 5% unaccounted
        balance = flow.mass_balance(raw)
        self.assertAlmostEqual(balance["closure"], 0.05, places=9)

    def test_outlet_split(self):
        split = flow.outlet_split(raw_two_outlets())
        self.assertAlmostEqual(split["fraction"]["p_out"], 0.75)
        self.assertAlmostEqual(split["fraction"]["p_out2"], 0.25)
        self.assertNotIn("p_in", split["fraction"])
        self.assertAlmostEqual(split["total_out_ul_min"], 60.0, places=6)

    def test_pressure_is_multiplied_by_density(self):
        """OpenFOAM's incompressible `p` is p/rho. Forgetting it is a silent x1000."""
        pressure = flow.pressure_summary(raw_two_outlets(), RHO,
                                         feeds=["p_in"], vents=["p_out"])
        self.assertAlmostEqual(pressure["peak_pa"], 2.1)          # 2.1e-3 m2/s2 x 1000
        self.assertAlmostEqual(pressure["peak_kpa"], 0.0021)
        self.assertAlmostEqual(pressure["drop_pa"]["p_in->p_out"], 2.0)
        self.assertEqual(pressure["peak_source"], "fieldMinMax(p)")

    def test_peak_pressure_falls_back_to_patches_and_says_so(self):
        raw = raw_two_outlets()
        raw.p_max_kinematic = math.nan
        raw.patches[0].p_max_kinematic = 2.05e-3
        pressure = flow.pressure_summary(raw, RHO, feeds=["p_in"], vents=["p_out"])
        self.assertAlmostEqual(pressure["peak_pa"], 2.05)
        self.assertIn("patch maxima only", pressure["peak_source"])

    def test_shear_in_both_units(self):
        shear = flow.zone_shear(raw_two_outlets(), RHO)
        self.assertAlmostEqual(shear["per_zone"]["culture"]["max_pa"], 1.5e-3)
        self.assertAlmostEqual(shear["per_zone"]["culture"]["max_dyn_cm2"], 1.5e-2)
        self.assertAlmostEqual(shear["per_zone"]["feed_line"]["max_pa"], 2.0e-2)
        self.assertAlmostEqual(shear["chip_max_dyn_cm2"], 2.0e-1)
        self.assertAlmostEqual(shear["kinematic_to_pa"], RHO)

    def test_reynolds_uses_the_local_hydraulic_diameter(self):
        reynolds = flow.zone_reynolds(raw_two_outlets(), RHO, MU)
        # culture: D_h = 2 w h / (w + h) = 2 * 2e-3 * 3e-4 / 2.3e-3
        d_h = 2 * 2.0e-3 * 3.0e-4 / (2.0e-3 + 3.0e-4)
        self.assertAlmostEqual(reynolds["per_zone"]["culture"]["re_max"],
                               RHO * 2.0e-4 * d_h / MU)
        self.assertAlmostEqual(reynolds["per_zone"]["culture"]["hydraulic_diameter_mm"],
                               d_h * 1e3)
        # the feed line is the faster, narrower one, so it owns the chip maximum
        self.assertGreater(reynolds["per_zone"]["feed_line"]["re_max"],
                           reynolds["per_zone"]["culture"]["re_max"])
        self.assertAlmostEqual(reynolds["chip_max"],
                               reynolds["per_zone"]["feed_line"]["re_max"])

    def test_reynolds_flags_what_is_over_the_limit(self):
        raw = raw_two_outlets()
        raw.zones[1].u_max_m_s = 5.0            # absurd, to push Re well over 1
        reynolds = flow.zone_reynolds(raw, RHO, MU)
        self.assertEqual(reynolds["over_limit"], ["feed_line"])
        self.assertEqual(reynolds["limit"], flow.RE_STEADY_LIMIT)

    def test_summary_shape(self):
        summary = flow.summarise(raw_two_outlets(), a_plan())
        self.assertEqual(summary["schema"], flow.SCHEMA)
        self.assertEqual(summary["source"], "openfoam")
        self.assertEqual(summary["labels"], ["SIMULATED"])
        self.assertTrue(summary["fluid_properties"]["kinematic_inputs"])
        self.assertIsInstance(summary["fluid"], str)   # the NAME, for render.py
        self.assertAlmostEqual(summary["flow"]["delivered_vs_prescribed"], 0.0, places=6)
        self.assertEqual(summary["grid"]["cells"], 12345)
        self.assertIn("mesh.py", summary["grid"]["spacing_chosen_by"])
        json.dumps(summary)        # the summary has to be writable as it stands


# ======================================================================================
# which solver, and why
# ======================================================================================


class _Rec:
    """The few attributes `plan_case` and `transient_reason` read off a ScenarioRecord."""

    def __init__(self, **kw):
        self.step = kw.get("step", 1)
        self.operation = kw.get("operation", "exchange")
        self.note = kw.get("note", "")
        self.targets = kw.get("targets", ())
        self.exchange_kind = kw.get("exchange_kind", "")
        self.duration_s = kw.get("duration_s", None)
        self.q_in_ul_min = kw.get("q_in_ul_min", 10.0)
        self.fluid = kw.get("fluid", None)
        self.ports = kw.get("ports", {})
        self._scenario = kw.get("scenario")

    @property
    def scenario(self):
        return self._scenario


class TestSolverChoice(unittest.TestCase):
    #: h^2 / nu for a 0.3 mm etch in the contract fluid: ~0.11 s.
    T_VISC = (3.0e-4) ** 2 / (MU / RHO)

    def test_stokes_and_long_is_steady(self):
        self.assertEqual(
            flow.transient_reason(_Rec(duration_s=600.0), 0.08, self.T_VISC, 600.0), ""
        )

    def test_inertia_forces_transient(self):
        why = flow.transient_reason(_Rec(duration_s=600.0), 5.6, self.T_VISC, 600.0)
        self.assertIn("Re = 5.6", why)
        self.assertIn("inertia", why)

    def test_a_named_transient_event_forces_transient_whatever_re_says(self):
        why = flow.transient_reason(_Rec(operation="flood", duration_s=600.0),
                                    0.01, self.T_VISC, 600.0)
        self.assertIn("transient event", why)
        self.assertIn("flood", why)

    def test_a_step_shorter_than_the_settling_time_forces_transient(self):
        why = flow.transient_reason(_Rec(duration_s=0.2), 0.01, self.T_VISC, 0.2)
        self.assertIn("viscous settling time", why)

    def test_single_phase_transient_terms_force_pimplefoam(self):
        for term in ("quench", "flood", "backflush", "purge"):
            with self.subTest(term=term):
                self.assertNotEqual(
                    flow.transient_reason(_Rec(operation=term), 0.01, 0.1, 600.0), ""
                )

    def test_a_two_phase_step_is_solved_STEADY_and_handed_to_vof(self):
        """Priming, droplet formation and encapsulation are transient because an
        INTERFACE moves. A single-phase solver has none, so pimpleFoam would integrate a
        one-fluid field to exactly the steady state simpleFoam reaches - at a thousand
        times the cost, and still saying nothing about the interface. spc_demo_v1 step 1
        is the case: 505k cells, Re 0.077, and the note says "Prime the board with oil"."""
        for term in ("prime", "droplet", "encapsulate", "emulsify"):
            with self.subTest(term=term):
                rec = _Rec(operation=term)
                self.assertEqual(flow.transient_reason(rec, 0.01, 0.1, 600.0), "",
                                 "a moving interface is not a single-phase transient")
                self.assertIn("vof.py", flow.two_phase_reason(rec))

    def test_a_two_phase_step_at_high_re_is_still_transient(self):
        """The interface is vof.py's, but inertia is still this module's."""
        rec = _Rec(operation="droplet")
        self.assertIn("Re", flow.transient_reason(rec, 9.0, 0.1, 600.0))

    def test_a_feature_name_is_not_an_event(self):
        """`ch_backflush` is a channel. A step that merely targets it is not a back-flush,
        and matching on target names made every step that touched it transient."""
        rec = _Rec(operation="exchange", targets=("ch_backflush", "main_trap"))
        self.assertEqual(flow.transient_reason(rec, 0.01, 0.1, 600.0), "")


class TestPlanCase(unittest.TestCase):
    """`plan_case` on a hand-built two-node mesh: no chip spec, no OpenFOAM."""

    def _mesh(self) -> Mesh:
        nodes = [
            Node(id="n_in", kind="port", depth_mm=0.3, feature_id="p_in"),
            Node(id="n_out", kind="port", depth_mm=0.3, feature_id="p_out"),
            Node(id="n_mid", kind="junction", depth_mm=0.3, area_mm2=0.1,
                 feature_id="ch_a"),
        ]
        edges = [
            Edge(id="e1", a="n_in", b="n_mid", kind="duct", length_mm=5.0,
                 width_mm=0.2, depth_mm=0.3, feature_id="ch_a",
                 conductance_m3_per_pa_s=1e-11, g_geom_m3=8e-15),
            Edge(id="e2", a="n_mid", b="n_out", kind="duct", length_mm=5.0,
                 width_mm=0.2, depth_mm=0.3, feature_id="ch_a",
                 conductance_m3_per_pa_s=1e-11, g_geom_m3=8e-15),
        ]
        return Mesh(nodes, edges, {"p_in": "n_in", "p_out": "n_out"},
                    {"chip_id": "synthetic", "mu_pa_s": MU, "grid_mm": 0.25})

    def _record(self, **kw):
        scenario = Scenario(step=1, ports={"p_in": PortState.flow(10.0),
                                           "p_out": PortState.vent()},
                            fluid="medium", temp_c=37.0, mu_pa_s=MU)
        return _Rec(scenario=scenario, **kw)

    def test_steady_plan(self):
        plan = flow.plan_case({"id": "synthetic"}, self._record(duration_s=600.0),
                              self._mesh())
        self.assertEqual(plan.solver, "simpleFoam")
        self.assertFalse(plan.transient)
        self.assertEqual(plan.feeds, ["p_in"])
        self.assertEqual(plan.vents, ["p_out"])
        self.assertAlmostEqual(plan.rho_kg_m3, RHO)
        self.assertAlmostEqual(plan.mu_pa_s, MU)
        self.assertAlmostEqual(plan.nu_m2_s, MU / RHO)
        self.assertIsNone(plan.spacing_mm, "mesh.py owns the default spacing")

    def test_spacing_defaults_to_mesh_py(self):
        plan = flow.plan_case({"id": "synthetic"}, self._record(), self._mesh())
        self.assertIsNone(plan.spacing_mm)

    def test_forcing_a_steady_solver_on_an_inertial_step_says_what_it_costs(self):
        record = self._record(operation="flood", duration_s=600.0)
        plan = flow.plan_case({"id": "synthetic"}, record, self._mesh(),
                              force_solver="simpleFoam")
        self.assertEqual(plan.solver, "simpleFoam")
        self.assertFalse(plan.transient)
        self.assertIn("STEADY LIMIT", plan.solver_forced_reason)
        self.assertIn("transient run is still required", plan.solver_forced_reason)

    def test_the_conversion_density_is_the_cases_own(self):
        """`plan.rho_kg_m3` must be the density `constant/transportProperties` was written
        with, because it is also the number every kinematic result is multiplied by. If
        the case and the post-processing could read different densities they would drift
        apart by a factor nobody would spot."""
        from assay2chip.foam import case as foam_case
        plan = flow.plan_case({"id": "synthetic"}, self._record(), self._mesh())
        self.assertAlmostEqual(plan.rho_kg_m3, foam_case.density_kg_m3())
        self.assertIn("transportProperties", plan.mu_source)
        # and the two independent resolutions agree, which is what makes a comparison mean
        # anything at all
        self.assertIsNone(flow._check_fluid_agreement(
            foam_case.fluid_for(37.0), plan))

    def test_an_assumed_density_is_carried_as_an_assumption(self):
        plan = flow.plan_case({"id": "synthetic"}, self._record(), self._mesh())
        # the contract marks fluid.density_37c_kg_m3 an assumption, so every pressure and
        # shear from the case is linear in an assumed number and has to say so
        self.assertTrue(plan.rho_is_assumption)
        self.assertTrue(any("density" in a for a in plan.assumptions))


# ======================================================================================
# findings, and the shear scoping that must come from analyse.py
# ======================================================================================


CULTURE_CHIP = {
    "id": "scope_test",
    "chambers": [
        {"id": "culture", "type": "culture", "block": "culture_chamber",
         "volume_ul": 5.0, "size": [3, 3, 0.3], "center": [5, 5, 0]},
        {"id": "waste_pool", "type": "waste", "block": "", "size": [2, 2, 0.3],
         "center": [9, 5, 0]},
    ],
    "channels": [], "ports": [],
}

CULTURE_PROTOCOL = {
    "id": "scope_test",
    "steps": [
        {"step": 1, "operation": "load", "target": "culture", "reagent": "cell suspension",
         "flow_ul_min": 10.0, "temp_c": 37.0},
        {"step": 2, "operation": "culture", "target": "culture", "temp_c": 37.0},
        {"step": 3, "operation": "exchange", "target": "culture", "reagent": "medium",
         "flow_ul_min": 10.0, "temp_c": 37.0},
    ],
}

#: The same chamber, one step before a deliberate lyse: the 0.3 ceiling, not 0.01.
RINSE_PROTOCOL = {
    "id": "scope_test",
    "steps": [
        {"step": 1, "operation": "load", "target": "culture", "reagent": "cell suspension",
         "flow_ul_min": 10.0, "temp_c": 37.0},
        {"step": 2, "operation": "culture", "target": "culture", "temp_c": 37.0},
        {"step": 3, "operation": "rinse", "target": "culture", "reagent": "wash buffer",
         "flow_ul_min": 10.0, "temp_c": 37.0},
        {"step": 4, "operation": "lyse", "target": "culture", "reagent": "lysis buffer",
         "flow_ul_min": 10.0, "temp_c": 37.0},
    ],
}


def summary_with_shear(zone: str, dyn_cm2: float, step=3, **kw) -> dict:
    plan = a_plan(chip_id="scope_test", step=step, feeds=["p_in"], vents=["p_out"],
                  capped=[], q_in_ul_min=10.0)
    raw = flow.RawFields(
        patches=[flow.PatchSample(port="p_in", role="feed", flow_m3_s=10.0 / 6e10),
                 flow.PatchSample(port="p_out", role="vent", flow_m3_s=-10.0 / 6e10)],
        zones=[flow.ZoneSample(zone=zone,
                               wall_shear_max_kinematic=dyn_cm2 / 10.0 / RHO,
                               wall_shear_source=kw.get("source",
                                                        "owner_cell_to_cellzone"),
                               u_max_m_s=1e-4, width_m=2e-4, depth_m=3e-4)],
        p_max_kinematic=1e-3, u_max_m_s=1e-4, wall_shear_max_kinematic=dyn_cm2 / 10.0 / RHO,
        converged=True, cells=1000,
    )
    return flow.summarise(raw, plan)


class TestCaseDirectoryConvention(unittest.TestCase):
    """ONE case directory, `<out>/<chip_id>/step_<N>/`, with the mesh inside it.

    This is a regression suite for a bug that cost a real OpenFOAM job: the mesh went to
    `foam/<chip>/1/` and the flow case to `foam/foam/<chip>/step_1/`, so `checkMesh` ran
    on an empty directory. Two separate mistakes - a doubled `foam/` from joining a run
    directory already called `foam` to a literal `foam`, and `<step>` against `step_<N>`.
    `case.case_dir` is now the only answer and `flow.plan_case` asks it.
    """

    def test_no_doubled_foam_for_the_default_run(self):
        from assay2chip.foam import case as foam_case
        out = foam_case.out_dir()
        self.assertEqual(out.name, "foam")
        self.assertNotEqual(out.parent.name, "foam", "work/foam/foam is the bug")
        self.assertNotIn("foam/foam", foam_case.case_dir(None, "spc_demo_v1", 1).as_posix())

    def test_out_dir_is_idempotent(self):
        from assay2chip.foam import case as foam_case
        run_dir = ROOT / "assay2chip" / "work" / "e2e"
        self.assertEqual(foam_case.out_dir(work=run_dir),
                         foam_case.out_dir(work=run_dir / "foam"))

    def test_step_name_is_step_underscore_n(self):
        from assay2chip.foam import case as foam_case
        self.assertEqual(foam_case.step_name(1), "step_1")
        self.assertEqual(foam_case.step_name("4b"), "step_4b")
        self.assertEqual(foam_case.step_name("step_7"), "step_7")   # already prefixed
        self.assertEqual(foam_case.step_name(None), foam_case.ALL_STEPS)

    def test_a_step_id_cannot_escape_its_directory(self):
        from assay2chip.foam import case as foam_case
        self.assertEqual(foam_case.case_dir(None, "chip", "../../etc").name,
                         "step_.._.._etc")
        with self.assertRaises(Exception):
            foam_case.case_dir(None, "..", 1)

    def test_plan_case_uses_the_helper(self):
        from assay2chip.foam import case as foam_case
        mesh = Mesh([Node(id="a", kind="port", depth_mm=0.3)],
                    [Edge(id="e", a="a", b="a", kind="duct", width_mm=0.2, depth_mm=0.3,
                          length_mm=1.0)],
                    {"p_in": "a"}, {"chip_id": "spc_demo_v1", "mu_pa_s": MU})
        mesh.edges[0].b = "a"
        scenario = Scenario(step=1, ports={"p_in": PortState.flow(10.0)},
                            fluid="medium", temp_c=37.0, mu_pa_s=MU)
        plan = flow.plan_case({"id": "spc_demo_v1"}, _Rec(scenario=scenario), mesh)
        self.assertEqual(Path(plan.case_dir),
                         foam_case.case_dir(None, "spc_demo_v1", 1))
        self.assertEqual(Path(plan.case_dir).name, "step_1")

    def test_a_named_run_puts_the_case_under_that_run(self):
        from assay2chip.foam import case as foam_case
        mesh = Mesh([Node(id="a", kind="port", depth_mm=0.3)], [],
                    {"p_in": "a"}, {"chip_id": "x", "mu_pa_s": MU})
        scenario = Scenario(step=2, ports={"p_in": PortState.flow(1.0)},
                            fluid="medium", temp_c=37.0, mu_pa_s=MU)
        plan = flow.plan_case({"id": "x"}, _Rec(scenario=scenario, step=2), mesh,
                              run="e2e")
        self.assertEqual(Path(plan.case_dir),
                         foam_case.out_dir("e2e") / "x" / "step_2")

    def test_a_doubled_foam_collapses_whoever_built_it(self):
        """`work/foam/foam` is the reported bug. It must resolve to `work/foam` even when
        a caller has already joined `foam` onto a directory called `foam`."""
        from assay2chip.foam import case as foam_case
        doubled = ROOT / "assay2chip" / "work" / "foam" / "foam"
        self.assertEqual(foam_case.out_dir(work=doubled), foam_case.out_dir())
        self.assertEqual(foam_case.case_dir(doubled, "spc_demo_v1", 1),
                         foam_case.case_dir(None, "spc_demo_v1", 1))

    def test_render_py_looks_in_the_same_place(self):
        """`render.py` discovers and writes cases too, and it reached the path by its own
        route. The two agreeing on EVERY input is the only thing that keeps the mesh and
        the case in one directory, so it is asserted rather than assumed."""
        from assay2chip.foam import case as foam_case
        from assay2chip.foam import render
        for out in (None, ROOT / "assay2chip" / "work" / "e2e",
                    ROOT / "assay2chip" / "work" / "e2e" / "foam",
                    ROOT / "assay2chip" / "work" / "foam" / "foam"):
            for step in (1, "4b", None):
                with self.subTest(out=out, step=step):
                    self.assertEqual(
                        Path(render.case_dir("spc_demo_v1", step, out=out)),
                        foam_case.case_dir(out, "spc_demo_v1", step),
                    )
        run_dir = ROOT / "assay2chip" / "work" / "e2e"
        self.assertEqual(render.foam_out_dir(run_dir), foam_case.out_dir(work=run_dir))
        # ...and render still READS the old bare `<chip>/<N>` layout, so a half-finished
        # run from before the fix is not silently invisible
        self.assertIn(foam_case.case_dir(run_dir, "module1_v1", 1),
                      render._step_dirs(foam_case.out_dir(work=run_dir), "module1_v1", 1))


class TestVtkConversion(unittest.TestCase):
    """Without `foamToVTK` there is no 3D picture, and the renderer says nothing is wrong.

    `render.foam_vtk()` falls back to the reduced-order 2.5D field when it finds no VTK,
    which is legitimate behaviour - so a converged 3D solve that was never converted
    renders as the very model it replaced, silently. Every chip did exactly that.
    """

    def test_the_field_list_is_shell_quoted(self):
        """`runner.run` joins `args` into a SHELL command, so a bare `(U p ...)` is a
        subshell: foamToVTK exited 2 before it ran and before its log existed."""
        stack = flow.FoamStack(reason="no runner")
        self.assertFalse(flow.convert_to_vtk(Path("/nonexistent"), stack=stack)["ok"])

        calls = []

        class _Result:
            ok, returncode, wall_time_s = True, 0, 1.0

        class _Runner:
            @staticmethod
            def run(case_dir, command, **kw):
                calls.append((command, list(kw.get("args", ()))))
                return _Result()

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = flow.convert_to_vtk(Path(tmp), stack=flow.FoamStack(
                mesh=object(), case=object(), runner=_Runner()))
        command, args = calls[0]
        self.assertEqual(command, "foamToVTK")
        self.assertIn("-latestTime", args)
        quoted = args[args.index("-fields") + 1]
        self.assertTrue(quoted.startswith("'") and quoted.endswith("'"),
                        f"the field list must be shell-quoted, got {quoted!r}")
        self.assertIn("wallShearStress", quoted)
        # no VTK was written, so it is NOT ok even though the command succeeded
        self.assertFalse(out["ok"])
        self.assertIn("wrote no VTK", out["reason"])

    def test_only_the_fields_the_renderer_draws(self):
        self.assertEqual(tuple(flow.VTK_FIELDS), ("U", "p", "wallShearStress"))

    def test_render_py_finds_the_top_level_multiblock(self):
        """foamToVTK writes `<case>_<time>.vtm` BEFORE the `boundary.vtm` it points at,
        and `render.foam_vtk()` takes the newest by mtime - so the renderer would get
        wall surfaces with no interior to slice. The top-level file is touched last."""
        import tempfile
        from assay2chip.foam import render
        with tempfile.TemporaryDirectory() as tmp:
            case = Path(tmp) / "foam" / "spc_demo_v1" / "step_1"
            vtk = case / "VTK" / "step_1_62"
            vtk.mkdir(parents=True)
            top = case / "VTK" / "step_1_62.vtm"
            top.write_text("<VTKFile/>")
            (vtk / "boundary.vtm").write_text("<VTKFile/>")
            (vtk / "internal.vtu").write_text("<VTKFile/>")
            import os
            # the order foamToVTK leaves: the top-level pointer first, its parts after
            os.utime(top, (1000.0, 1000.0))
            os.utime(vtk / "internal.vtu", (1500.0, 1500.0))
            os.utime(vtk / "boundary.vtm", (2000.0, 2000.0))
            self.assertEqual(render.foam_vtk(Path(tmp), "spc_demo_v1", 1).name,
                             "boundary.vtm", "the bug this guards against")
            top.touch()
            self.assertEqual(render.foam_vtk(Path(tmp), "spc_demo_v1", 1).name,
                             "step_1_62.vtm")

    def test_run_step_converts_by_default(self):
        import inspect
        self.assertIs(inspect.signature(flow.run_step).parameters["vtk"].default, True)


class TestShearScoping(unittest.TestCase):
    """The three ceilings are not interchangeable, and this module must not choose."""

    def test_adherent_cells_are_judged_at_the_culture_ceiling(self):
        summary = summary_with_shear("culture", 0.05)      # 5x the 0.01 ceiling
        findings = flow.findings_for(summary, CULTURE_CHIP, CULTURE_PROTOCOL,
                                     step_index=2)
        shear = [f for f in findings if f.cls == "shear"]
        self.assertEqual(len(shear), 1)
        self.assertAlmostEqual(shear[0].limit, 0.01)
        self.assertEqual(shear[0].metric, "wall_shear_vs_adherent_cells_ceiling")
        self.assertEqual(shear[0].source, "openfoam")
        self.assertEqual(shear[0].severity, "blocker")
        self.assertIn("scope_evidence", shear[0].audit)

    def test_a_rinse_before_a_deliberate_lyse_gets_the_0_3_ceiling(self):
        """0.05 dyn/cm2 is 5x over for adherent cells and FINE for a pre-lysis rinse.

        Picking the strictest ceiling here would invent a blocker on a step whose whole
        purpose is to be followed by deliberate lysis - the mistake this repo has made
        repeatedly.
        """
        summary = summary_with_shear("culture", 0.05)
        findings = flow.findings_for(summary, CULTURE_CHIP, RINSE_PROTOCOL, step_index=2)
        shear = [f for f in findings if f.cls == "shear"]
        self.assertEqual(shear, [], "0.05 dyn/cm2 is under the 0.3 pre-lysis ceiling")
        over = flow.findings_for(summary_with_shear("culture", 0.9), CULTURE_CHIP,
                                 RINSE_PROTOCOL, step_index=2)
        limits = [f.limit for f in over if f.cls == "shear"]
        self.assertEqual(limits, [0.3])

    def test_a_chamber_with_nothing_shear_sensitive_is_not_judged(self):
        summary = summary_with_shear("waste_pool", 5.0)
        findings = flow.findings_for(summary, CULTURE_CHIP, CULTURE_PROTOCOL,
                                     step_index=2)
        self.assertEqual([f for f in findings if f.cls == "shear"], [],
                         "a waste pool has no shear ceiling; reporting one is noise")

    def test_an_unscopable_chamber_is_reported_as_unjudged_not_guessed(self):
        chip = {"id": "scope_test", "chambers": [{"id": "mystery", "size": [1, 1, 0.3],
                                                  "center": [1, 1, 0]}],
                "channels": [], "ports": []}
        protocol = {"id": "scope_test",
                    "steps": [{"step": 3, "operation": "exchange", "target": "mystery",
                               "flow_ul_min": 10.0}]}
        findings = flow.findings_for(summary_with_shear("mystery", 5.0), chip, protocol,
                                     step_index=0)
        scope = [f for f in findings if f.metric == "shear_scope_unknown"]
        self.assertEqual(len(scope), 1)
        self.assertIsNone(scope[0].limit)
        self.assertIn("UNJUDGED", scope[0].message)

    def test_a_capsule_chamber_is_bounded_by_pressure_not_shear(self):
        """A capsule bed has NO shear ceiling. Judging it by one is the classic error."""
        chip = {
            "id": "scope_test",
            "capsule": {"diameter_um": 400},
            "chambers": [{"id": "trap", "type": "trap", "size": [3, 3, 0.3],
                          "center": [5, 5, 0],
                          "retention": {"type": "weir", "opening_um": 25}}],
            "channels": [{"id": "ch_in", "from": "p_in", "to": "trap",
                          "carries": "capsules"}],
            "ports": [],
        }
        protocol = {"id": "scope_test",
                    "steps": [{"step": 3, "operation": "load", "target": "trap",
                               "reagent": "capsule suspension", "flow_ul_min": 10.0}]}
        findings = flow.findings_for(summary_with_shear("trap", 5.0), chip, protocol,
                                     step_index=0)
        self.assertEqual([f for f in findings if f.cls == "shear"], [])

    def test_an_estimated_shear_is_flagged_as_an_assumption(self):
        summary = summary_with_shear("culture", 0.05, source="estimate_from_zone_u")
        findings = flow.findings_for(summary, CULTURE_CHIP, CULTURE_PROTOCOL,
                                     step_index=2)
        shear = [f for f in findings if f.cls == "shear"]
        self.assertTrue(shear[0].assumption)


class TestNumericalFindings(unittest.TestCase):
    def test_a_bad_mass_balance_is_a_high_finding(self):
        raw = raw_two_outlets()
        raw.patches[1].flow_m3_s = -0.5e-9           # 25% unaccounted
        summary = flow.summarise(raw, a_plan())
        findings = flow.findings_for(summary)
        closure = [f for f in findings if f.metric == "patch_mass_closure"]
        self.assertEqual(len(closure), 1)
        self.assertEqual(closure[0].severity, "high")
        self.assertEqual(closure[0].cls, "unmodelled")

    def test_the_wrong_delivered_flow_is_caught(self):
        summary = flow.summarise(raw_two_outlets(), a_plan(q_in_ul_min=120.0))
        findings = flow.findings_for(summary)
        self.assertTrue(any(f.metric == "delivered_vs_prescribed_inflow" for f in findings))

    def test_an_unconverged_solve_is_a_finding(self):
        raw = raw_two_outlets()
        raw.converged = False
        findings = flow.findings_for(flow.summarise(raw, a_plan()))
        self.assertTrue(any(f.metric == "solver_converged" for f in findings))

    def test_re_over_one_replaces_the_reduced_model_rather_than_blaming_it(self):
        raw = raw_two_outlets()
        raw.zones[1].u_max_m_s = 5.0
        summary = flow.summarise(raw, a_plan())
        findings = flow.findings_for(summary)
        unmodelled = [f for f in findings
                      if f.metric == "reynolds_vs_stokes_validity"]
        self.assertEqual(len(unmodelled), 1)
        self.assertEqual(unmodelled[0].feature_id, "feed_line")
        self.assertIn("outside its validity", unmodelled[0].message)
        self.assertIn("nothing to fix", unmodelled[0].fix_hint)

    def test_findings_serialise_with_the_class_key(self):
        findings = flow.findings_for(summary_with_shear("culture", 0.05),
                                     CULTURE_CHIP, CULTURE_PROTOCOL, step_index=2)
        blob = [f.to_json() for f in findings]
        self.assertTrue(all("class" in f for f in blob))
        self.assertTrue(all(f["source"] == "openfoam" for f in blob))
        json.dumps(blob)


# ======================================================================================
# grid sensitivity
# ======================================================================================


class TestGridSensitivity(unittest.TestCase):
    def _summary(self, peak_pa: float, shear: float, cells: int) -> dict:
        return {"pressure": {"peak_pa": peak_pa},
                "shear": {"chip_max_dyn_cm2": shear},
                "reynolds": {"chip_max": 0.2},
                "grid": {"cells": cells}}

    def test_gci_arithmetic(self):
        out = flow.grid_sensitivity(
            [self._summary(1.00, 0.10, 1000), self._summary(1.02, 0.102, 3375)],
            [0.15, 0.10],
        )
        self.assertEqual(out["status"], "ok")
        self.assertAlmostEqual(out["refinement_ratio"], 1.5)
        peak = out["quantities"]["peak_pressure_pa"]
        # the coarse grid is the first entry, so f_coarse = 1.00, f_fine = 1.02
        self.assertAlmostEqual(peak["coarse"], 1.00)
        self.assertAlmostEqual(peak["fine"], 1.02)
        rel = abs((1.00 - 1.02) / 1.02)
        expected = flow.GCI_SAFETY_TWO_GRID * rel / (1.5 ** flow.ASSUMED_ORDER - 1)
        self.assertAlmostEqual(peak["gci"], expected)
        self.assertAlmostEqual(out["worst_gci"], max(
            v["gci"] for v in out["quantities"].values() if v["gci"]))

    def test_order_is_assumed_and_the_report_says_so(self):
        out = flow.grid_sensitivity(
            [self._summary(1.0, 0.1, 10), self._summary(1.0, 0.1, 20)], [0.2, 0.1])
        self.assertEqual(out["assumed_order"], flow.ASSUMED_ORDER)
        self.assertIn("ASSUMED", out["note"])

    def test_one_resolution_is_not_a_study(self):
        out = flow.grid_sensitivity([self._summary(1.0, 0.1, 10)], [0.1])
        self.assertEqual(out["status"], "incomplete")


# ======================================================================================
# cross-validation: the reference side, and the attribution of a disagreement
# ======================================================================================


class TestReducedSide(unittest.TestCase):
    """The reduced-order numbers the 3D run is compared against."""

    def test_reduced_pressure_drop_reads_the_port_nodes(self):
        from assay2chip.cfd.model import Field
        mesh = Mesh([Node(id="a", kind="port"), Node(id="b", kind="port")],
                    [Edge(id="e", a="a", b="b", conductance_m3_per_pa_s=1e-11,
                          g_geom_m3=8e-15)],
                    {"p_in": "a", "p_out": "b"}, {"chip_id": "x", "mu_pa_s": MU})
        field = Field(p_pa={"a": 12.5, "b": 0.0})
        drops = vf.reduced_pressure_drop(mesh, field, ["p_in"], ["p_out"])
        self.assertAlmostEqual(drops["p_in->p_out"], 12.5)

    def test_reduced_outlet_split_uses_the_negative_port_flows(self):
        from assay2chip.cfd.model import Field
        field = Field(meta={"port_flow_ul_min": {"p_in": 60.0, "p_out": -45.0,
                                                 "p_out2": -15.0, "p_cap": 0.0}})
        split = vf.reduced_outlet_split(field)
        self.assertAlmostEqual(split["p_out"], 0.75)
        self.assertAlmostEqual(split["p_out2"], 0.25)
        self.assertNotIn("p_in", split)
        self.assertNotIn("p_cap", split)


def a_row(**kw) -> vf.CrossRow:
    base = dict(chip_id="x", step=1, solver="simpleFoam",
                dp_label="p_in->p_out", dp_foam_pa=1.0, dp_reduced_pa=1.0,
                split_foam={"p_out": 1.0}, split_reduced={"p_out": 1.0},
                re_reduced=0.1, re_foam=0.1)
    base.update(kw)
    return vf.CrossRow(**base)


def a_context(**kw) -> vf.ChipContext:
    base = dict(chip_id="x", spacing_mm=0.0333, min_channel_mm=0.2,
                min_aspect_w_over_h=10.0, depth_mm=0.3, z_cells=13)
    base.update(kw)
    return vf.ChipContext(**base)


class TestAttribution(unittest.TestCase):
    """Every branch, including the one that refuses to explain."""

    def test_agreement_is_evidence_for_both(self):
        row = vf.attribute(a_row(dp_foam_pa=1.01), a_context())
        self.assertEqual(row.verdict, "agree")
        self.assertEqual(row.believed, "both")
        self.assertIn("evidence that BOTH are right", row.why)

    def test_a_split_disagreement_alone_breaks_agreement(self):
        row = vf.attribute(a_row(split_foam={"p_out": 0.6, "p_out2": 0.4},
                                 split_reduced={"p_out": 0.5, "p_out2": 0.5}),
                           a_context())
        self.assertNotEqual(row.verdict, "agree")

    def test_inertia_believes_the_3d_run(self):
        row = vf.attribute(a_row(dp_foam_pa=1.4, re_foam=5.6), a_context())
        self.assertEqual(row.verdict, "disagree")
        self.assertEqual(row.believed, "openfoam")
        self.assertIn("Stokes", row.why)
        self.assertIn("5.6", row.why)

    def test_side_walls_believe_the_3d_run_and_check_the_sign(self):
        row = vf.attribute(a_row(dp_foam_pa=1.25), a_context(min_aspect_w_over_h=1.5))
        self.assertEqual(row.believed, "openfoam")
        self.assertIn("side-wall", row.why)
        # ...but only when the 3D run is the HIGHER one. Side-wall drag can only ADD
        # resistance, so a 3D drop that is LOWER is not explained by it and must not be
        # attributed to it just because a narrow lane happens to be on the chip.
        other = vf.attribute(a_row(dp_foam_pa=0.75),
                             a_context(min_aspect_w_over_h=1.5))
        self.assertEqual(other.believed, "unresolved")
        self.assertIn("UNEXPLAINED", other.why)

    def test_a_staircased_mesh_believes_the_reduced_model(self):
        row = vf.attribute(a_row(dp_foam_pa=1.25), a_context(spacing_mm=0.08))
        self.assertEqual(row.verdict, "inconclusive")
        self.assertEqual(row.believed, "reduced")
        self.assertIn("cells across the narrowest channel", row.why)

    def test_an_under_resolved_weir_slit_believes_the_reduced_model(self):
        row = vf.attribute(a_row(dp_foam_pa=1.25),
                           a_context(slit_gaps_um=[25.0], z_cells=4))
        self.assertEqual(row.believed, "reduced")
        self.assertIn("gap^3", row.why)

    def test_a_disagreement_inside_the_error_bar_is_not_a_disagreement(self):
        row = vf.attribute(a_row(dp_foam_pa=1.08, gci=0.20), a_context())
        self.assertEqual(row.verdict, "inconclusive")
        self.assertEqual(row.believed, "neither")
        self.assertIn("discretisation error bar", row.why)

    def test_an_unexplained_disagreement_stays_unexplained(self):
        row = vf.attribute(a_row(dp_foam_pa=1.4), a_context())
        self.assertEqual(row.verdict, "disagree")
        self.assertEqual(row.believed, "unresolved")
        self.assertIn("UNEXPLAINED", row.why)
        self.assertTrue(row.caveats)

    def test_no_comparable_number_is_incomplete(self):
        row = vf.attribute(a_row(dp_foam_pa=None, dp_reduced_pa=None, split_foam={},
                                 split_reduced={}), a_context())
        self.assertEqual(row.verdict, "incomplete")

    def test_rows_serialise(self):
        row = vf.attribute(a_row(dp_foam_pa=1.4, re_foam=5.6), a_context())
        blob = row.to_json()
        self.assertEqual(blob["believed_correct"], "openfoam")
        self.assertAlmostEqual(blob["pressure_drop"]["difference_percent"], 40.0, places=6)
        json.dumps(blob)


class TestRoutingMismatch(unittest.TestCase):
    """Two models that send the liquid down different paths are not validating each other.

    Written for `spc_demo_v1`, where the reduced mesh attaches both of the delay line's
    neighbours to the SAME end node, so its 234 mm serpentine hangs off one point as a
    dead-end stub and carries exactly zero. The reduced model then reports 39 Pa where the
    exact duct law says 1194 Pa and OpenFOAM says 1136 Pa. A routing fault explains a
    disagreement of ANY size, so it is tested for before inertia or side walls.
    """

    def _mesh(self, bypass: bool) -> Mesh:
        """`xj -> delay -> out`, with the delay line correctly in line or stubbed off."""
        nodes = [Node(id="j_xj", kind="junction", depth_mm=0.15, feature_id="xj_1"),
                 Node(id="j_a", kind="junction", depth_mm=0.15, feature_id="delay_1"),
                 Node(id="j_b", kind="junction", depth_mm=0.15, feature_id="delay_1"),
                 Node(id="p_out", kind="port", depth_mm=0.15, feature_id="out")]
        out_from = "j_a" if bypass else "j_b"
        edges = [
            Edge(id="d:delay_1", a="j_a", b="j_b", kind="duct", length_mm=234.0,
                 width_mm=0.2, depth_mm=0.15, feature_id="delay_1",
                 conductance_m3_per_pa_s=1.585e-13, g_geom_m3=4.755e-16),
            Edge(id="d:in", a="j_xj", b="j_a", kind="duct", length_mm=0.85,
                 width_mm=0.2, depth_mm=0.15, feature_id="ch_xj_delay",
                 conductance_m3_per_pa_s=4.4e-11, g_geom_m3=1.3e-13),
            Edge(id="d:out", a=out_from, b="p_out", kind="duct", length_mm=0.7,
                 width_mm=0.2, depth_mm=0.15, feature_id="ch_delay_out",
                 conductance_m3_per_pa_s=5.3e-11, g_geom_m3=1.6e-13),
        ]
        return Mesh(nodes, edges, {"p_in": "j_xj", "p_out": "p_out"},
                    {"chip_id": "spc_demo_v1", "mu_pa_s": 3e-3})

    def _summary(self, delay_u_mm_s: float) -> dict:
        return {"reynolds": {"per_zone": {
            "delay_1": {"u_max_mm_s": delay_u_mm_s, "re_max": 0.2},
            "ch_xj_delay": {"u_max_mm_s": 3.4, "re_max": 0.2},
            "ch_delay_out": {"u_max_mm_s": 3.4, "re_max": 0.2},
        }}}

    def _field(self, delay_q: float):
        from assay2chip.cfd.model import Field, Residual
        q_in = 5.178e-11
        return Field(q_m3_s={"d:delay_1": delay_q, "d:in": q_in, "d:out": q_in},
                     residual=Residual(q_in=q_in, q_out=q_in))

    def test_a_bypassed_duct_is_found(self):
        found = vf.routing_mismatch(self._mesh(bypass=True),
                                    self._field(9.4e-30), self._summary(3.46))
        self.assertEqual([r["feature_id"] for r in found], ["delay_1"])
        self.assertAlmostEqual(found[0]["duct_length_mm"], 234.0)
        self.assertLess(found[0]["reduced_fraction_of_through_flow"], 1e-10)

    def test_a_correctly_routed_chip_reports_nothing(self):
        found = vf.routing_mismatch(self._mesh(bypass=False),
                                    self._field(5.178e-11), self._summary(3.46))
        self.assertEqual(found, [])

    def test_a_duct_both_models_leave_quiet_is_not_a_mismatch(self):
        """A capped branch is dry in BOTH models. That is agreement, not a fault."""
        found = vf.routing_mismatch(self._mesh(bypass=True),
                                    self._field(0.0), self._summary(0.0))
        self.assertEqual(found, [])

    def test_routing_outranks_every_other_cause(self):
        row = a_row(dp_foam_pa=1136.0, dp_reduced_pa=39.4, re_foam=5.0,
                    routing=[{"feature_id": "delay_1", "duct_length_mm": 234.0,
                              "reduced_fraction_of_through_flow": 1.8e-19,
                              "openfoam_u_max_mm_s": 3.46,
                              "reduced_nodes": ["j:delay_1:a", "j:delay_1:b"]}])
        # Re > 1 AND a narrow lane are both present; neither may win
        row = vf.attribute(row, a_context(min_aspect_w_over_h=1.33))
        self.assertEqual(row.believed, "openfoam")
        self.assertIn("route the flow DIFFERENTLY", row.why)
        self.assertIn("assay2chip/cfd/mesh.py", row.why)
        self.assertNotIn("side-wall drag, nor staircase", row.why)
        self.assertTrue(row.caveats)

    def test_the_3d_answer_matches_the_exact_duct_law(self):
        """The claim "the 3D answer is correct" is only worth making because a third,
        independent reference agrees with it: the exact series for the same duct.

        234 mm x 0.2 x 0.15 mm at 3.107 uL/min in the step's 3e-3 Pa s oil.
        """
        from assay2chip.cfd import validate as cfd_validate
        q = 3.107e-9 / 60.0
        analytic = cfd_validate.duct_resistance_exact(234.0, 0.2, 0.15, 3e-3) * q
        self.assertAlmostEqual(analytic, 1193.7, delta=1.0)
        foam, reduced = 1136.16, 39.38
        self.assertLess(abs(foam - analytic) / analytic, 0.06,
                        "OpenFOAM is within a few percent of the exact duct law")
        self.assertGreater(abs(reduced - analytic) / analytic, 0.9,
                           "the reduced model is not, because it bypasses the duct")


class TestChipContext(unittest.TestCase):
    def test_measures_the_narrowest_channel_and_the_worst_aspect_ratio(self):
        mesh = Mesh(
            [Node(id="a", kind="port", depth_mm=0.3), Node(id="b", kind="port", depth_mm=0.3)],
            [
                Edge(id="wide", a="a", b="b", kind="duct", width_mm=0.8, depth_mm=0.3,
                     length_mm=5.0, feature_id="ch_wide"),
                Edge(id="narrow", a="a", b="b", kind="duct", width_mm=0.2, depth_mm=0.3,
                     length_mm=5.0, feature_id="ch_narrow"),
                Edge(id="crest", a="a", b="b", kind="slit", width_mm=2.5, gap_um=25.0,
                     length_mm=0.2, feature_id="trap"),
            ],
            {"p_in": "a", "p_out": "b"}, {"chip_id": "x", "mu_pa_s": MU},
        )
        ctx = vf.chip_context(mesh, 0.0333)
        self.assertAlmostEqual(ctx.min_channel_mm, 0.2)
        self.assertAlmostEqual(ctx.min_aspect_w_over_h, 1.5)      # 0.3 / 0.2
        self.assertIn("ch_narrow", ctx.narrow_features)
        self.assertEqual(ctx.slit_gaps_um, [25.0])
        self.assertAlmostEqual(ctx.cells_across_narrowest, 0.2 / 0.0333, places=3)
        json.dumps(ctx.to_json())

    def test_no_spacing_means_no_staircase_verdict(self):
        ctx = vf.ChipContext(chip_id="x", spacing_mm=None, min_channel_mm=0.2)
        self.assertTrue(math.isinf(ctx.cells_across_narrowest))

    def test_mouth_edges_do_not_count_as_channels(self):
        """A `mouth` edge is as wide as the raster cells it spans, so it tracks `grid_mm`
        and not the part. Counting it halves every cells-across figure and invents a
        staircase - which it did, on module1_v1, before this was pinned."""
        mesh = Mesh(
            [Node(id="a", kind="port"), Node(id="b", kind="cell", area_mm2=0.06)],
            [Edge(id="d", a="a", b="b", kind="duct", width_mm=0.2, depth_mm=0.3,
                  length_mm=2.0, feature_id="ch"),
             Edge(id="m", a="a", b="b", kind="mouth", width_mm=0.1, depth_mm=0.3,
                  length_mm=0.1, feature_id="cx")],
            {"p": "a"}, {"chip_id": "x", "mu_pa_s": MU},
        )
        self.assertAlmostEqual(flow._min_channel_mm(mesh), 0.2)

    def test_adopt_grid_takes_the_meshers_own_measurements(self):
        ctx = a_context(spacing_mm=0.08, min_channel_mm=0.1)
        self.assertLess(ctx.cells_across_narrowest, vf.MIN_CELLS_ACROSS_CHANNEL)
        vf.adopt_grid(ctx, {"grid": {"spacing_mm": 1 / 30, "min_channel_mm": 0.2,
                                     "cells_across_narrowest": 6.0, "z_cells": 13}})
        self.assertAlmostEqual(ctx.cells_across_narrowest, 6.0)
        self.assertEqual(ctx.z_cells, 13)
        # ...and a row that would have been called a staircase now gets judged on physics
        row = vf.attribute(a_row(dp_foam_pa=1.02), ctx)
        self.assertEqual(row.verdict, "agree")


# ======================================================================================
# the exact duct reference, and the report when OpenFOAM is absent
# ======================================================================================


class TestDuctReference(unittest.TestCase):
    """The reference side of the duct benchmark needs no solver at all."""

    def test_two_block_mesh_splits_the_walls(self):
        from assay2chip.foam import case as foam_case
        blob = foam_case.dumps(vf.duct_two_block_mesh(foam_case, 15.0, 16, 16, 8.0))
        self.assertIn("walls_entry", blob)
        self.assertIn("walls_dev", blob)
        self.assertIn("inlet", blob)
        self.assertIn("outlet", blob)
        self.assertEqual(blob.count("hex ("), 2, "two blocks, split at the entry length")

    def test_the_entry_must_be_inside_the_duct(self):
        from assay2chip.foam import case as foam_case
        with self.assertRaises(ValueError):
            vf.duct_two_block_mesh(foam_case, 0.5, 16, 16, 8.0)

    def test_the_observed_order_check(self):
        """Halve the error under a 1.5x refinement twice over and the order is 2."""
        exact = 100.0
        coarse, fine = exact * (1 + 0.0225), exact * (1 + 0.01)
        check = vf._order_check("gradient", coarse, fine, exact, 1.5)
        self.assertAlmostEqual(check["got"], 2.0, places=2)
        self.assertTrue(check["passed"])
        self.assertLess(abs(check["extrapolated_error_percent"]), 0.1)

    def test_a_first_order_scheme_fails_the_order_check(self):
        exact = 100.0
        check = vf._order_check("gradient", exact * 1.015, exact * 1.010, exact, 1.5)
        self.assertLess(check["got"], vf.DUCT_ORDER_BAND[0])
        self.assertFalse(check["passed"])

    def test_check_reports_the_arithmetic(self):
        check = vf._check("x", "req", 1.0, 1.02, 0.05, "Pa")
        self.assertTrue(check["passed"])
        self.assertAlmostEqual(check["difference"], 0.02 / 1.02)
        self.assertIn("within", check["message"])
        bad = vf._check("x", "req", 1.0, 2.0, 0.05, "Pa")
        self.assertFalse(bad["passed"])
        self.assertIn("OUTSIDE", bad["message"])


class TestWithoutOpenFoam(unittest.TestCase):
    """A missing or half-built sibling is a skipped benchmark, never a crash."""

    def test_an_empty_stack_is_unavailable_with_a_reason(self):
        stack = flow.FoamStack(reason="assay2chip/foam/mesh.py: not written yet")
        self.assertFalse(stack.available)
        self.assertFalse(stack.openfoam_available())
        self.assertIn("mesh.py", stack.as_json()["reason"])

    def test_duct_check_skips_cleanly(self):
        out = vf.duct_check(flow.FoamStack(reason="no OpenFOAM here"))
        self.assertEqual(out["status"], "skipped")
        self.assertIsNone(out["passed"])
        self.assertIn("no OpenFOAM here", out["reason"])
        # the geometry and the reference are still reported, so the table is readable
        self.assertIn("width_mm", out["geometry"])

    def test_grid_study_skips_cleanly(self):
        out = vf.grid_study(stack=flow.FoamStack(reason="no OpenFOAM here"))
        self.assertEqual(out["status"], "skipped")

    def test_a_skipped_report_is_never_trusted(self):
        report = {
            "duct": {"status": "skipped", "reason": "no OpenFOAM", "passed": None},
            "cross_validation": {"status": "skipped", "rows": []},
            "grid_sensitivity": {"status": "skipped"},
        }
        findings = vf.findings(report)
        self.assertTrue(any(f.id == "foam_duct_skipped" for f in findings))
        self.assertTrue(all(f.source == "openfoam" for f in findings))

    def test_build_case_refuses_without_the_stack(self):
        with self.assertRaises(flow.FlowError) as ctx:
            flow.build_case({"id": "x"}, _Rec(), Mesh([], [], {}, {"mu_pa_s": MU}),
                            a_plan(case_dir="/tmp/nope"),
                            flow.FoamStack(reason="mesh.py missing"))
        self.assertIn("mesh.py missing", str(ctx.exception))

    def test_a_disagreement_becomes_a_model_error_finding(self):
        report = {"duct": {"status": "pass", "passed": True, "checks": []},
                  "cross_validation": {"rows": [a_row(dp_foam_pa=1.4, re_foam=5.6)
                                                .to_json() | {"verdict": "disagree",
                                                              "believed_correct": "openfoam",
                                                              "why": "inertia"}]},
                  "grid_sensitivity": {"status": "pass"}}
        findings = vf.findings(report)
        model = [f for f in findings if f.cls == "model_error"]
        self.assertEqual(len(model), 1)
        self.assertEqual(model[0].severity, "low",
                         "an explained divergence is not an implementation fault")
        self.assertIn("Believed correct: openfoam", model[0].message)


class TestFluidAgreement(unittest.TestCase):
    """The two models must be solving the same liquid, or a comparison means nothing."""

    def test_a_viscosity_mismatch_is_refused(self):
        class _Fluid:
            mu_pa_s = MU * 1.01
            rho_kg_m3 = RHO
        with self.assertRaises(flow.FlowError) as ctx:
            flow._check_fluid_agreement(_Fluid(), a_plan())
        self.assertIn("mu_pa_s", str(ctx.exception))

    def test_agreement_passes_silently(self):
        class _Fluid:
            mu_pa_s = MU
            rho_kg_m3 = RHO
        self.assertIsNone(flow._check_fluid_agreement(_Fluid(), a_plan()))


class TestFunctionObjectNames(unittest.TestCase):
    """`case.standard_functions` names its objects `flow_<port>` and `p_<port>`, and this
    repo's ports are themselves called `p_in`, `p_mid`, `p_a_out`."""

    ROLES = {"p_in": "feed", "p_mid": "vent", "p_a_in": "cap", "waste": "vent"}

    def test_port_resolution(self):
        for name, expected in (
            ("flow_p_in", "p_in"),
            ("p_p_in", "p_in"),
            ("p_p_mid", "p_mid"),
            ("flow_p_a_in", "p_a_in"),
            ("flow_waste", "waste"),
            ("p_waste", "waste"),
            ("waste", "waste"),
            ("fieldMinMax", None),
            ("wallShearStress", None),
            ("age", None),
        ):
            with self.subTest(name=name):
                self.assertEqual(flow._port_from_fo_name(name, self.ROLES), expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)

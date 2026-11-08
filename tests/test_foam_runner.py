"""assay2chip/foam/runner.py: launching OpenFOAM, reading its logs, judging convergence.

    python -m unittest tests.test_foam_runner -v
    FOAM_SKIP_INTEGRATION=1 python -m unittest tests.test_foam_runner   # offline only

Two halves, and the split is deliberate (assay2chip/foam/README.md: "Tests that need
OpenFOAM are skipped cleanly when it is absent").

OFFLINE - log parsing and the convergence verdict, on captured OpenFOAM output. These are
the tests that matter most, because the verdict is what every sibling will trust: the
difference between "residualControl met" and "ran out of iterations" is the difference
between a result and a guess, and both look like a finished run from outside.

INTEGRATION - skipped with a clear reason when OpenFOAM is absent:

  * the lid-driven cavity tutorial, OpenFOAM's own case, end to end through `run()`;
  * a straight rectangular duct solved with simpleFoam and checked against the EXACT
    duct solution (the Fourier series, not the 0.63 engineering fit) - this is the test
    that says the whole stack, from `case.py`'s dictionaries through the launcher to the
    log parser, produces correct physics and not merely output;
  * the same case in parallel, which must give the same answer as serial;
  * mass conservation: what goes in comes out.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "operator")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from assay2chip.foam import case, runner  # noqa: E402

SKIP_INTEGRATION = bool(os.environ.get("FOAM_SKIP_INTEGRATION"))


def needs_openfoam(test):
    """Skip cleanly, and say WHY, when there is no OpenFOAM on this machine."""
    if SKIP_INTEGRATION:
        return unittest.skip("FOAM_SKIP_INTEGRATION is set")(test)
    if not runner.available():
        return unittest.skip(f"OpenFOAM is not installed. {runner.INSTALL_COMMAND}")(test)
    return test


# A real simpleFoam log, trimmed. Everything the parser has to understand is in it.
CONVERGED_LOG = """\
/*---------------------------------------------------------------------------*\\
| =========                 |                                                 |
\\*---------------------------------------------------------------------------*/
Build  : v2606 OPENFOAM=2606
Create mesh for time = 0

SIMPLE: convergence criteria
    field p\t tolerance 1e-05
    field U\t tolerance 1e-06

Starting time loop

Time = 1

smoothSolver:  Solving for Ux, Initial residual = 1, Final residual = 9.2e-09, No Iterations 4
smoothSolver:  Solving for Uy, Initial residual = 1, Final residual = 8.1e-09, No Iterations 4
GAMG:  Solving for p, Initial residual = 1, Final residual = 0.0085, No Iterations 3
time step continuity errors : sum local = 2.4e-07, global = -1.1e-19, cumulative = -1.1e-19
Courant Number mean: 0.0012 max: 0.0098
ExecutionTime = 0.3 s  ClockTime = 1 s

Time = 2

smoothSolver:  Solving for Ux, Initial residual = 0.0021, Final residual = 1.2e-10, No Iterations 3
smoothSolver:  Solving for Uy, Initial residual = 0.0033, Final residual = 1.9e-10, No Iterations 3
GAMG:  Solving for p, Initial residual = 4.4e-06, Final residual = 3.1e-08, No Iterations 2
time step continuity errors : sum local = 9.1e-13, global = 2.2e-19, cumulative = -8.8e-19
Courant Number mean: 0.0012 max: 0.0098
ExecutionTime = 0.6 s  ClockTime = 1 s

SIMPLE solution converged in 2 iterations

End
"""

FATAL_LOG = """\
Create mesh for time = 0

Reading field p

--> FOAM FATAL IO ERROR: (openfoam-2606)
Wrong token type - expected scalar value, found on line 25: punctuation '['

file: 0/U/boundaryField/inlet/volumetricFlowRate at line 25.

    From Istream &Foam::operator>>(Istream &, doubleScalar &)

FOAM exiting
"""

DIVERGED_LOG = """\
Time = 1

smoothSolver:  Solving for Ux, Initial residual = 1, Final residual = 0.2, No Iterations 1000
GAMG:  Solving for p, Initial residual = 1e+12, Final residual = 1e+09, No Iterations 1000
time step continuity errors : sum local = 1e+08, global = 1e+08, cumulative = 1e+08
ExecutionTime = 9 s  ClockTime = 9 s

End
"""

CHECKMESH_OK_LOG = """\
Mesh stats
    points:           8000
    cells:            5880

Checking geometry...
    Overall domain bounding box (0 0 0) (0.01 0.0005 0.0003)
    Mesh non-orthogonality Max: 0 average: 0
    Max skewness = 1e-13 OK.

Mesh OK.

End
"""

CHECKMESH_BAD_LOG = """\
Mesh stats
    cells:            12

Checking geometry...
 ***Number of severely non-orthogonal (> 70 degrees) faces: 44.
 ***High aspect ratio cells found, Max aspect ratio: 2100, number of cells 9
Failed 2 mesh checks.

End
"""


class TestLogParsing(unittest.TestCase):
    """Offline. Captured OpenFOAM output in, numbers out."""

    def setUp(self):
        self.converged = runner.parse_log(CONVERGED_LOG)

    def test_iterations_are_counted_from_the_time_lines(self):
        self.assertEqual(self.converged.iterations, 2)
        self.assertEqual(self.converged.times, [1.0, 2.0])

    def test_the_three_velocity_components_are_one_field(self):
        """residualControl's `U` means the worst of Ux, Uy, Uz - so the parser agrees."""
        self.assertIn("U", self.converged.final_residuals)
        self.assertNotIn("Ux", self.converged.final_residuals)
        self.assertAlmostEqual(self.converged.final_residuals["U"], 0.0033)

    def test_initial_and_final_residuals_are_both_kept(self):
        self.assertAlmostEqual(self.converged.initial_residuals["p"], 1.0)
        self.assertAlmostEqual(self.converged.final_residuals["p"], 4.4e-06)
        self.assertGreater(self.converged.residual_drop["p"], 1e5)

    def test_continuity_errors_are_the_last_ones(self):
        self.assertAlmostEqual(self.converged.continuity_local, 9.1e-13)
        self.assertAlmostEqual(self.converged.continuity_cumulative, -8.8e-19)
        self.assertFalse(self.converged.continuity_growing)

    def test_courant_number_is_the_worst_seen(self):
        self.assertAlmostEqual(self.converged.courant_max, 0.0098)

    def test_wall_time_comes_from_execution_time(self):
        self.assertAlmostEqual(self.converged.execution_time_s, 0.6)
        self.assertAlmostEqual(self.converged.clock_time_s, 1.0)

    def test_the_simple_converged_line_is_found(self):
        self.assertEqual(self.converged.simple_converged_at, 2)
        self.assertTrue(self.converged.reached_end)

    def test_a_fatal_error_is_captured_whole(self):
        summary = runner.parse_log(FATAL_LOG)
        self.assertTrue(summary.fatal)
        self.assertIn("Wrong token type", summary.fatal[0])
        self.assertIn("volumetricFlowRate", summary.fatal[0])

    def test_checkmesh_pass_and_fail_are_told_apart(self):
        self.assertIs(runner.parse_log(CHECKMESH_OK_LOG).mesh_ok, True)
        bad = runner.parse_log(CHECKMESH_BAD_LOG)
        self.assertIs(bad.mesh_ok, False)
        self.assertEqual(len(bad.mesh_failures), 2)
        self.assertIn("non-orthogonal", bad.mesh_failures[0])

    def test_an_empty_log_does_not_explode(self):
        summary = runner.parse_log("")
        self.assertEqual(summary.iterations, 0)
        self.assertEqual(summary.final_residuals, {})

    def test_nul_bytes_from_a_parallel_log_are_stripped(self):
        """Open MPI pads interleaved output, which makes the log a binary file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "log.simpleFoam"
            path.write_bytes(CONVERGED_LOG.encode().replace(b"Time = 1", b"Time = 1\x00\x00"))
            text = runner.read_log(path)
            self.assertNotIn("\x00", text)
            self.assertEqual(runner.parse_log(text).iterations, 2)

    def test_a_missing_log_reads_as_empty_rather_than_raising(self):
        self.assertEqual(runner.read_log(Path("/nonexistent/log.simpleFoam")), "")

    def test_the_summary_is_json_safe(self):
        import json
        json.dumps(self.converged.to_dict())


class TestConvergenceVerdict(unittest.TestCase):
    """Offline. The judgement every sibling will trust."""

    def test_residual_control_met_is_converged(self):
        converged, verdict = runner.convergence(runner.parse_log(CONVERGED_LOG))
        self.assertTrue(converged)
        self.assertEqual(verdict, "converged")

    def test_out_of_iterations_is_NOT_converged(self):
        """The distinction this whole module exists for."""
        text = CONVERGED_LOG.replace("SIMPLE solution converged in 2 iterations\n", "")
        text = text.replace("Initial residual = 4.4e-06", "Initial residual = 0.02")
        converged, verdict = runner.convergence(runner.parse_log(text))
        self.assertFalse(converged)
        self.assertEqual(verdict, "iteration_limit")
        self.assertIn("NOT converged", runner.VERDICT_MEANING[verdict])

    def test_a_nonzero_exit_is_failed_whatever_the_residuals_say(self):
        converged, verdict = runner.convergence(runner.parse_log(CONVERGED_LOG), returncode=1)
        self.assertFalse(converged)
        self.assertEqual(verdict, "failed")

    def test_a_fatal_error_is_failed(self):
        self.assertEqual(runner.convergence(runner.parse_log(FATAL_LOG))[1], "failed")

    def test_divergence_is_its_own_verdict(self):
        converged, verdict = runner.convergence(runner.parse_log(DIVERGED_LOG))
        self.assertFalse(converged)
        self.assertEqual(verdict, "diverged")

    def test_a_growing_continuity_error_is_mass_not_conserved(self):
        rows = []
        for i in range(1, 21):
            rows.append(f"Time = {i}\n")
            rows.append("GAMG:  Solving for p, Initial residual = 0.5, "
                        "Final residual = 1e-08, No Iterations 2\n")
            rows.append(f"time step continuity errors : sum local = 1e-09, global = 1e-09, "
                        f"cumulative = {1e-9 * 10 ** (i / 2.0):.3e}\n")
        converged, verdict = runner.convergence(runner.parse_log("".join(rows)))
        self.assertFalse(converged)
        self.assertEqual(verdict, "mass_not_conserved")

    def test_a_high_courant_number_only_matters_on_a_transient_run(self):
        text = CONVERGED_LOG.replace("max: 0.0098", "max: 7.4")
        self.assertEqual(runner.convergence(runner.parse_log(text), steady=True)[1], "converged")
        self.assertEqual(
            runner.convergence(runner.parse_log(text), steady=False)[1], "courant_too_high")

    def test_a_function_object_residual_does_not_decide_the_flow_verdict(self):
        """`age` solves its own equation and reports into the same log. It is not U or p."""
        text = CONVERGED_LOG.replace(
            "SIMPLE solution converged in 2 iterations",
            "smoothSolver:  Solving for age, Initial residual = 0.06, "
            "Final residual = 1e-09, No Iterations 5\nSIMPLE solution converged in 2 iterations")
        summary = runner.parse_log(text)
        self.assertIn("age", summary.final_residuals)
        self.assertTrue(runner.convergence(summary)[0])

    def test_a_mesh_has_no_convergence_only_a_pass_or_fail(self):
        """None is not False: a caller treating it as False would reject every mesh."""
        converged, verdict = runner.convergence(runner.parse_log(CHECKMESH_OK_LOG))
        self.assertIsNone(converged)
        self.assertEqual(verdict, "mesh_ok")
        self.assertEqual(runner.convergence(runner.parse_log(CHECKMESH_BAD_LOG))[1], "mesh_failed")

    def test_every_verdict_is_explained(self):
        for verdict in runner.VERDICTS:
            self.assertIn(verdict, runner.VERDICT_MEANING)
            self.assertTrue(runner.VERDICT_MEANING[verdict])


class TestOfflineBehaviour(unittest.TestCase):
    """Offline. Everything that must work, or fail well, without OpenFOAM."""

    def test_available_never_raises(self):
        self.assertIsInstance(runner.available(), bool)

    def test_why_not_names_the_install_command(self):
        message = runner.why_not()
        self.assertIn("brew install gerlero/openfoam/openfoam", message)
        self.assertIn("FOAM_LAUNCHER", message)

    def test_default_cores_leaves_headroom(self):
        self.assertLessEqual(runner.default_cores(), os.cpu_count() or 8)
        self.assertGreaterEqual(runner.default_cores(), 1)

    def test_foam_cores_overrides_the_default(self):
        old = os.environ.get("FOAM_CORES")
        try:
            os.environ["FOAM_CORES"] = "3"
            self.assertEqual(runner.default_cores(), 3)
            os.environ["FOAM_CORES"] = "banana"
            with self.assertRaises(runner.FoamError):
                runner.default_cores()
        finally:
            os.environ.pop("FOAM_CORES", None)
            if old is not None:
                os.environ["FOAM_CORES"] = old

    def test_running_in_a_directory_that_is_not_a_case_says_so(self):
        with self.assertRaises(runner.FoamError) as ctx:
            runner.run("/nonexistent/case", "simpleFoam")
        self.assertIn("no case directory", str(ctx.exception))

    def test_a_core_count_that_contradicts_the_case_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            case.write_dict(Path(tmp) / "system" / "decomposeParDict",
                            case.decompose_par_dict(6))
            with self.assertRaises(runner.FoamError) as ctx:
                runner.run(tmp, "simpleFoam", parallel=4)
            self.assertIn("numberOfSubdomains 6", str(ctx.exception))

    def test_the_decomposition_is_read_out_of_the_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            case.write_dict(Path(tmp) / "system" / "decomposeParDict",
                            case.decompose_par_dict(6))
            self.assertEqual(runner._decomposed_subdomains(Path(tmp)), 6)
            self.assertIsNone(runner._decomposed_subdomains(Path(tmp) / "nope"))

    def test_blame_points_at_decomposePar_when_the_solver_never_started(self):
        """A parallel run is three commands; naming the wrong one costs ten minutes."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "log.decomposePar").write_text(FATAL_LOG)
            result = runner.RunResult(
                command="simpleFoam", case_dir=tmp, returncode=1, ok=False,
                log_path=tmp / "log.simpleFoam", wall_time_s=1.0,
                foam_version="v2606", launcher="/opt/homebrew/bin/openfoam", parallel=6,
            )
            step, log_path, summary = runner.blame(result)
            self.assertEqual(step, "decomposePar")
            self.assertIn("Wrong token type", summary.fatal[0])
            message = runner._fail_message("simpleFoam", result)
            self.assertIn("actually failed is `decomposePar`", message)

    def test_blame_is_the_command_itself_for_a_serial_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "log.simpleFoam").write_text(FATAL_LOG)
            result = runner.RunResult(
                command="simpleFoam", case_dir=tmp, returncode=1, ok=False,
                log_path=tmp / "log.simpleFoam", wall_time_s=1.0, foam_version="v2606",
                launcher="x", log=runner.parse_log(FATAL_LOG))
            self.assertEqual(runner.blame(result)[0], "simpleFoam")

    def test_the_manifest_always_records_the_version_and_the_launcher(self):
        """assay2chip/foam/README.md: every run manifest carries both."""
        import json

        with tempfile.TemporaryDirectory() as tmp:
            result = runner.RunResult(
                command="simpleFoam", case_dir=Path(tmp), returncode=0, ok=True,
                log_path=Path(tmp) / "log.simpleFoam", wall_time_s=12.5,
                foam_version="v2606", launcher="/opt/homebrew/bin/openfoam",
                parallel=6, log=runner.parse_log(CONVERGED_LOG),
                converged=True, verdict="converged")
            manifest = result.manifest()
            json.dumps(manifest)
            self.assertEqual(manifest["openfoam"]["version"], "v2606")
            self.assertEqual(manifest["openfoam"]["launcher"], "/opt/homebrew/bin/openfoam")
            self.assertEqual(manifest["parallel"], 6)
            self.assertEqual(manifest["verdict"], "converged")
            self.assertIn("residualControl", manifest["verdict_means"])

    def test_clean_removes_output_and_keeps_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            for rel in ("0/U", "constant/transportProperties", "system/controlDict",
                        "500/U", "processor0/0/U"):
                (tmp / rel).parent.mkdir(parents=True, exist_ok=True)
                (tmp / rel).write_text("x")
            (tmp / "log.simpleFoam").write_text("x")
            removed = runner.clean(tmp)
            self.assertIn("500", removed)
            self.assertIn("processor0", removed)
            self.assertIn("log.simpleFoam", removed)
            self.assertTrue((tmp / "0" / "U").exists())
            self.assertTrue((tmp / "system" / "controlDict").exists())


@needs_openfoam
class TestInstalledOpenFOAM(unittest.TestCase):
    """Integration. Skipped cleanly with the install command when there is none."""

    def test_it_reports_a_version_and_a_launcher(self):
        got = runner.install()
        self.assertTrue(got.version)
        self.assertTrue(got.launcher.exists())
        self.assertEqual(runner.version(), got.version)

    def test_it_is_the_esi_distribution_the_readme_pins(self):
        """ESI versions are dates (2606); the Foundation's are small integers (13)."""
        api = runner.install().api or runner.version() or ""
        digits = "".join(c for c in api if c.isdigit())
        self.assertEqual(len(digits), 4, f"{api} does not look like an ESI version")
        self.assertGreaterEqual(int(digits), 2412, "foam/README.md wants v2412 or newer")

    def test_every_solver_the_package_names_is_present(self):
        """assay2chip/foam/README.md lists these by name."""
        for name in ("simpleFoam", "pimpleFoam", "scalarTransportFoam", "interFoam",
                     "icoUncoupledKinematicParcelFoam", "blockMesh", "checkMesh",
                     "decomposePar", "reconstructPar", "foamToVTK", "postProcess"):
            self.assertTrue(runner.has_solver(name), f"{name} is missing")

    def test_the_function_objects_the_siblings_were_promised(self):
        for name in ("wallShearStress", "surfaceFieldValue", "fieldMinMax", "age"):
            self.assertTrue(runner.has_function_object(name), f"{name} is missing")

    def test_a_name_that_is_not_a_function_object_says_no(self):
        self.assertFalse(runner.has_function_object("definitelyNotAFunctionObject"))

    def test_the_laminar_model_file_matches_this_installation(self):
        """The one that would otherwise be guessed, checked against the real tutorials."""
        name = case.laminar_model_file(runner.version())
        tutorials = runner.tutorials_dir()
        self.assertIsNotNone(tutorials)
        found = list((tutorials / "incompressible" / "simpleFoam").glob(f"*/constant/{name}"))
        self.assertTrue(found, f"no tutorial in this install has constant/{name}")


@needs_openfoam
class TestCavityTutorial(unittest.TestCase):
    """Integration. OpenFOAM's own lid-driven cavity, end to end through `run()`."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="foam-cavity-")
        cls.result = runner.cavity_smoke_test(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_the_mesh_is_clean(self):
        self.assertIs(self.result["mesh_ok"], True)

    def test_icofoam_ran_its_hundred_steps_and_ended(self):
        solve = self.result["icoFoam"]
        self.assertTrue(solve["ok"])
        self.assertEqual(self.result["iterations"], 100)
        self.assertTrue(solve["reached_end"])

    def test_the_residuals_fell_a_long_way(self):
        for value in self.result["final_residuals"].values():
            self.assertLess(value, 1e-5)

    def test_mass_is_conserved_to_machine_precision(self):
        self.assertLess(abs(self.result["icoFoam"]["continuity"]["cumulative"]), 1e-12)

    def test_the_manifest_carries_the_version_and_the_launcher(self):
        self.assertEqual(self.result["icoFoam"]["openfoam"]["version"], runner.version())
        self.assertTrue(self.result["icoFoam"]["openfoam"]["launcher"])


@needs_openfoam
class TestStraightDuctAgainstTheExactSolution(unittest.TestCase):
    """Integration. THE test: does this stack produce correct physics?

    A straight rectangular duct, 10 x 0.5 x 0.3 mm, at 1 uL/min of the 37 C contract
    fluid - a real chip channel at a real protocol flow rate, Re ~ 5e-5. Built entirely
    through `case.build_case`, meshed with blockMesh, solved with simpleFoam, and read
    back through the `surfaceFieldValue` function objects the siblings will use.

    The answer is checked against the EXACT rectangular-duct solution (the Fourier
    series), not against the 0.63 engineering fit, and not against `assay2chip/cfd` -
    there is no circularity here at all.
    """

    LENGTH_M, WIDTH_M, HEIGHT_M = 10e-3, 0.5e-3, 0.3e-3
    Q_UL_MIN = 1.0
    TOLERANCE_PCT = 3.0

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="foam-duct-")
        cls.dir = Path(cls.tmp) / "duct"
        cls.q_m3_s = cls.Q_UL_MIN / 6e10
        cls.fluid = case.fluid_for(37.0)
        patches = {
            "inlet": case.PatchBC("feed", q_m3_s=cls.q_m3_s),
            "outlet": case.PatchBC("vent"),
            "walls": case.PatchBC("wall"),
        }
        # No `age` here: it solves a second equation and this test is about the flow.
        functions = case.standard_functions(patches, include_age=False, write_interval=10)
        cls.built = case.build_case(cls.dir, fluid=cls.fluid, patches=patches,
                                    functions=functions, end_time=2000,
                                    write_interval=2000, n_cores=1)
        case.write_dict(
            cls.dir / "system" / "blockMeshDict",
            case.duct_block_mesh(cls.LENGTH_M, cls.WIDTH_M, cls.HEIGHT_M,
                                 nx=80, ny=20, nz=16),
            object_name="blockMeshDict", location="system")
        cls.mesh = runner.run(cls.dir, "blockMesh")
        cls.check = runner.run(cls.dir, "checkMesh")
        cls.solve = runner.run(cls.dir, "simpleFoam", timeout_s=1200)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    @staticmethod
    def _last(path: Path) -> float:
        rows = [line for line in runner.read_log(path).splitlines()
                if line and not line.startswith("#")]
        return float(rows[-1].split()[-1])

    def _probe(self, name: str) -> float:
        found = sorted((self.dir / "postProcessing" / name).glob("*/*.dat"))
        self.assertTrue(found, f"no output from the {name} function object")
        return self._last(found[-1])

    def test_the_mesh_passes_checkmesh(self):
        self.assertIs(self.check.log.mesh_ok, True)
        self.assertEqual(self.check.verdict, "mesh_ok")

    def test_the_solver_converged_on_residual_control(self):
        self.assertTrue(self.solve.converged)
        self.assertEqual(self.solve.verdict, "converged")
        self.assertGreater(self.solve.iterations, 1)

    def test_what_goes_in_comes_out(self):
        """phi sums to the set flow rate at the inlet and its negative at the outlet."""
        q_in, q_out = self._probe("flow_inlet"), self._probe("flow_outlet")
        self.assertAlmostEqual(abs(q_in) / self.q_m3_s, 1.0, places=4)
        self.assertAlmostEqual(abs(q_out) / self.q_m3_s, 1.0, places=4)
        self.assertLess(abs(q_in + q_out) / self.q_m3_s, 1e-4)

    def test_the_pressure_drop_matches_the_exact_duct_solution(self):
        """The whole stack, measured against textbook physics."""
        p_in = self.fluid.pressure_pa(self._probe("p_inlet"))
        p_out = self.fluid.pressure_pa(self._probe("p_outlet"))
        measured = p_in - p_out
        exact = case.duct_pressure_drop_pa(
            self.q_m3_s, self.fluid.mu_pa_s, self.LENGTH_M, self.WIDTH_M, self.HEIGHT_M)
        error_pct = 100.0 * abs(measured - exact) / exact
        self.assertLess(
            error_pct, self.TOLERANCE_PCT,
            f"simpleFoam gave {measured:.6g} Pa, the exact duct solution is {exact:.6g} Pa "
            f"({error_pct:.2f}% apart)")

    def test_the_flow_is_deep_in_the_stokes_regime(self):
        """Re << 1, which is why assay2chip/cfd's reduced model is right for these chips."""
        u = self.q_m3_s / (self.WIDTH_M * self.HEIGHT_M)
        d_h = 4 * (self.WIDTH_M * self.HEIGHT_M) / (2 * (self.WIDTH_M + self.HEIGHT_M))
        self.assertLess(self.fluid.reynolds(u, d_h), 1.0)

    def test_the_case_summary_records_what_was_run(self):
        summary = self.built.to_dict()
        self.assertEqual(summary["solver"], "simpleFoam")
        self.assertEqual(summary["fluid"]["mu_pa_s"], self.fluid.mu_pa_s)
        self.assertTrue(summary["digest"])


@needs_openfoam
class TestParallel(unittest.TestCase):
    """Integration. decomposePar + mpirun + reconstructPar must not change the answer."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="foam-parallel-")
        cls.dir = Path(cls.tmp) / "duct"
        cls.cores = 4
        cls.q_m3_s = 1.0 / 6e10
        fluid = case.fluid_for(37.0)
        patches = {
            "inlet": case.PatchBC("feed", q_m3_s=cls.q_m3_s),
            "outlet": case.PatchBC("vent"),
            "walls": case.PatchBC("wall"),
        }
        cls.fluid = fluid
        case.build_case(cls.dir, fluid=fluid, patches=patches, n_cores=cls.cores,
                        end_time=2000, write_interval=2000,
                        functions=case.standard_functions(patches, include_age=False,
                                                          write_interval=10))
        case.write_dict(cls.dir / "system" / "blockMeshDict",
                        case.duct_block_mesh(10e-3, 0.5e-3, 0.3e-3, nx=60, ny=16, nz=12),
                        object_name="blockMeshDict", location="system")
        runner.run(cls.dir, "blockMesh")
        cls.result = runner.run(cls.dir, "simpleFoam", parallel=True, timeout_s=1200)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_it_really_ran_on_several_cores(self):
        self.assertEqual(self.result.parallel, self.cores)
        self.assertTrue((self.dir / f"processor{self.cores - 1}").is_dir())

    def test_the_manifest_records_all_three_steps(self):
        steps = [s["step"] for s in self.result.manifest()["steps"]]
        self.assertEqual(steps, ["decomposePar", "simpleFoam", "reconstructPar"])

    def test_it_converged_and_reconstructed(self):
        self.assertTrue(self.result.converged)
        self.assertTrue((self.dir / "log.reconstructPar").exists())

    def test_the_parallel_answer_is_the_same_physics(self):
        found = sorted((self.dir / "postProcessing" / "p_inlet").glob("*/*.dat"))
        rows = [r for r in runner.read_log(found[-1]).splitlines() if not r.startswith("#")]
        measured = self.fluid.pressure_pa(float(rows[-1].split()[-1]))
        exact = case.duct_pressure_drop_pa(self.q_m3_s, self.fluid.mu_pa_s,
                                           10e-3, 0.5e-3, 0.3e-3)
        self.assertLess(100.0 * abs(measured - exact) / exact, 4.0)


if __name__ == "__main__":
    unittest.main()

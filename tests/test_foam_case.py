"""assay2chip/foam/case.py: OpenFOAM dictionaries out of Python. ALL OFFLINE.

    python -m unittest tests.test_foam_case -v

Not one test here needs OpenFOAM installed, which is the point: `assay2chip/foam/README.md`
requires that case generation be tested without it, so a sibling can develop the mesher or
the renderer on a machine that has none.

What is actually being pinned down:

  * the writer round-trips every shape OpenFOAM has - nested dicts, lists, dimensioned
    values, Function1, booleans, verbatim macros - and is BYTE-DETERMINISTIC, because the
    run manifest carries a hash of the case;
  * the two version-specific spellings that would otherwise be guessed: the laminar model
    file (`turbulenceProperties` for ESI, `momentumTransport` for the Foundation) and
    `volumetricFlowRate` being a Function1 with NO dimensions bracket. Both of those have
    already cost a failed run;
  * fluid properties come from the contracts and nowhere else, and an assumed fluid stays
    flagged all the way into the case summary;
  * the feed/vent/cap/wall map produces the right boundary types, and the mistakes that
    silently produce a useless case - a feed with no vent, a feed at zero flow - are
    LOUD errors with an actionable message;
  * the exact rectangular-duct law, against its own published shape factors, so the
    OpenFOAM integration test is measured with a ruler that has itself been checked.
"""

from __future__ import annotations

import json
import math
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "operator")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from assay2chip.foam import case  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402


class TestFormatting(unittest.TestCase):
    """One value in, one OpenFOAM token out."""

    def test_numbers_lose_the_pointless_decimal(self):
        self.assertEqual(case.format_value(1.0), "1")
        self.assertEqual(case.format_value(2), "2")
        self.assertEqual(case.format_value(-3.5), "-3.5")

    def test_small_numbers_keep_their_precision(self):
        # nu for the 37 C contract fluid is 8e-07: written as 1e-06 scale, not rounded to 0.
        self.assertEqual(case.format_value(8e-07), "8e-07")
        self.assertEqual(case.format_value(1.6666666666e-11), "1.66666666660e-11".replace("60e", "6e"))

    def test_bools_are_foam_bools_not_python_ones(self):
        self.assertEqual(case.format_value(True), "true")
        self.assertEqual(case.format_value(False), "false")

    def test_strings_pass_through_untouched(self):
        # `uniform (0 0 0)` and `$internalField` are OpenFOAM syntax, not data to escape.
        self.assertEqual(case.format_value("uniform (0 0 0)"), "uniform (0 0 0)")
        self.assertEqual(case.format_value("$internalField"), "$internalField")

    def test_sequences_become_foam_lists(self):
        self.assertEqual(case.format_value((0, 0, -9.81)), "(0 0 -9.81)")
        self.assertEqual(case.format_value(["a", "b"]), "(a b)")

    def test_none_is_refused_with_a_usable_message(self):
        with self.assertRaises(case.CaseError) as ctx:
            case.format_value(None)
        self.assertIn("leave the key out", str(ctx.exception))

    def test_nan_and_inf_are_refused(self):
        for bad in (float("nan"), float("inf")):
            with self.assertRaises(case.CaseError):
                case.format_value(bad)

    def test_dimensioned_value(self):
        self.assertEqual(str(case.Dimensioned(1e-6, case.DIM_NU)), "[0 2 -1 0 0 0 0] 1e-06")
        self.assertEqual(str(case.Dimensioned(9.81, case.DIM_VELOCITY, name="g")),
                         "g [0 1 -1 0 0 0 0] 9.81")

    def test_function1_has_no_dimensions_bracket(self):
        """The bug that killed a real run: flowRateInletVelocity takes a Function1."""
        self.assertEqual(str(case.Function1("constant", 1.5e-11)), "constant 1.5e-11")
        self.assertNotIn("[", str(case.Function1("constant", 1.5e-11)))

    def test_function1_table_is_a_ramp(self):
        self.assertEqual(str(case.Function1("table", [(0, 0), (1, 2e-9)])),
                         "table ((0 0) (1 2e-09))")


class TestDictWriting(unittest.TestCase):
    def test_nested_dicts_indent_and_brace(self):
        text = case.dumps({"solvers": {"p": {"solver": "GAMG", "tolerance": 1e-8}}})
        self.assertIn("solvers\n{\n    p\n    {\n        solver GAMG;", text)
        self.assertIn("        tolerance 1e-08;", text)

    def test_header_names_the_object_and_class(self):
        text = case.dumps({}, "volVectorField", "U", "0")
        self.assertIn("class       volVectorField;".replace("       ", " "), text.replace("  ", " "))
        self.assertIn("object      U;".replace("      ", " "), text.replace("  ", " "))
        self.assertIn('location    "0";'.replace("    ", " "), text.replace("  ", " "))

    def test_macro_keys_take_no_semicolon(self):
        text = case.dumps({"#include": '"initialConditions"'})
        self.assertIn('#include "initialConditions"', text)
        self.assertNotIn('#include "initialConditions";', text)

    def test_verbatim_is_copied_exactly(self):
        text = case.dumps({"boundaryField": {"#includeEtc": '"caseDicts/setConstraintTypes"'}})
        self.assertIn('#includeEtc "caseDicts/setConstraintTypes"', text)

    def test_named_blocks_for_blockmesh_boundary(self):
        text = case.dumps({"boundary": case.NamedBlocks([("inlet", {"type": "patch"})])})
        self.assertIn("boundary\n(\n    inlet\n    {\n        type patch;\n    }\n);", text)

    def test_table_is_one_row_per_line(self):
        text = case.dumps({"vertices": case.Table([(0, 0, 0), (1, 0, 0)])})
        self.assertIn("vertices\n(\n    (0 0 0)\n    (1 0 0)\n);", text)

    def test_output_is_byte_deterministic(self):
        """The run manifest hashes the case, so the same input must give the same bytes."""
        body = {"a": 1, "nested": {"b": [1, 2, 3], "c": case.Dimensioned(1e-6, case.DIM_NU)}}
        self.assertEqual(case.dumps(body), case.dumps(body))

    def test_key_order_is_the_order_written(self):
        text = case.dumps({"zebra": 1, "apple": 2})
        self.assertLess(text.index("zebra"), text.index("apple"))

    def test_write_dict_defaults_object_and_location_from_the_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = case.write_dict(Path(tmp) / "system" / "fvSchemes", {"ddtSchemes": {}})
            text = path.read_text()
            self.assertIn("object", text)
            self.assertIn("fvSchemes", text)
            self.assertIn('"system"', text)

    def test_read_entry_finds_a_top_level_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = case.write_dict(Path(tmp) / "controlDict", {"endTime": 2000})
            self.assertEqual(case.read_entry(path, "endTime"), "2000")
            self.assertIsNone(case.read_entry(path, "nope"))


class TestFluidFromContracts(unittest.TestCase):
    """nu = contracts.viscosity_for(temp) / fluid.density_37c_kg_m3, and nothing else."""

    def test_nu_is_the_contract_viscosity_over_the_contract_density(self):
        fluid = case.fluid_for(37.0)
        mu = contracts.viscosity_37c_pa_s().pa_s
        rho = contracts.param("fluid.density_37c_kg_m3")
        self.assertAlmostEqual(fluid.nu_m2_s, mu / rho, places=15)
        self.assertEqual(fluid.mu_pa_s, mu)
        self.assertEqual(fluid.rho_kg_m3, rho)

    def test_no_contract_value_is_hard_coded_anywhere_in_the_module(self):
        """The repo has been bitten six times by a literal outliving its contract.

        Checked against the parsed SOURCE, not the text: a substring search matches the
        "~1000" in a sentence about unit conversion and would have to be weakened until
        it caught nothing. Every numeric literal in the module is compared with every
        contract value this package reads, which is exact and cannot be talked around.
        """
        import ast

        tree = ast.parse(Path(case.__file__).read_text())
        # Only the functions that decide a fluid property or write one into the case. A
        # blanket scan of the module flags coincidences that are not fluid properties at
        # all - the default transient timestep is 1e-3 s, and water at 20 C is 1e-3 Pa s.
        owners = {"density_kg_m3", "fluid_for", "fluid_from_spec", "transport_properties",
                  "Fluid"}
        scanned = [node for node in ast.walk(tree)
                   if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in owners]
        self.assertEqual(len(scanned), len(owners), "a fluid function was renamed; update this test")
        literals = {
            node.value
            for owner in scanned for node in ast.walk(owner)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float))
            and not isinstance(node.value, bool)
        }
        for key, value in (
            (contracts.VISCOSITY_37C_KEY, contracts.viscosity_37c_pa_s().pa_s),
            (contracts.VISCOSITY_20C_KEY, contracts.viscosity_water_20c_pa_s().pa_s),
            (case.DENSITY_KEY, contracts.param(case.DENSITY_KEY)),
        ):
            self.assertNotIn(
                float(value), {float(x) for x in literals},
                f"{value} ({key}) is a literal in case.py; read it through contracts")

    def test_a_room_temperature_step_gets_the_other_contract_value(self):
        cold = case.fluid_for(20.0)
        warm = case.fluid_for(37.0)
        self.assertGreater(cold.mu_pa_s, warm.mu_pa_s)
        self.assertEqual(cold.contract_key, contracts.VISCOSITY_20C_KEY)
        self.assertEqual(warm.contract_key, contracts.VISCOSITY_37C_KEY)

    def test_an_unknown_temperature_is_the_warm_value_because_the_device_runs_warm(self):
        self.assertEqual(case.fluid_for(None).contract_key, contracts.VISCOSITY_37C_KEY)

    def test_pressure_and_shear_come_back_out_of_kinematic_units(self):
        fluid = case.fluid_for(37.0)
        self.assertAlmostEqual(fluid.pressure_pa(1.0), fluid.rho_kg_m3)
        self.assertAlmostEqual(fluid.wall_shear_pa(2.0), 2.0 * fluid.rho_kg_m3)
        # dyn/cm^2 is Pa x 10 - the unit every shear ceiling is stated in.
        self.assertAlmostEqual(fluid.wall_shear_dyn_cm2(2.0), fluid.wall_shear_pa(2.0) * 10.0)

    def test_an_assumed_fluid_stays_flagged_into_the_case(self):
        """A fluid the contract does not cover is NEVER silently water."""
        from assay2chip.cfd import scenario

        unresolved: list[dict] = []
        spec = scenario.resolve_fluid("carrier oil", "encapsulate", 25.0,
                                      is_dye_demo=False, unresolved=unresolved,
                                      port_hint="in_oil")
        fluid = case.fluid_from_spec(spec)
        self.assertTrue(fluid.assumption)
        self.assertEqual(fluid.fluid_class, "oil")
        self.assertNotEqual(fluid.mu_pa_s, contracts.viscosity_37c_pa_s().pa_s)

        with tempfile.TemporaryDirectory() as tmp:
            built = case.build_case(tmp, fluid=fluid, patches={
                "in_oil": case.PatchBC("feed", q_m3_s=1e-10),
                "out": case.PatchBC("vent"),
                "walls": case.PatchBC("wall"),
            })
            self.assertTrue(built.unresolved, "an assumed fluid must reach the case summary")
            self.assertIn("ASSUMPTION", json.dumps(built.to_dict()).upper())

    def test_fluid_from_spec_refuses_something_that_is_not_a_fluid_spec(self):
        with self.assertRaises(case.CaseError) as ctx:
            case.fluid_from_spec(object())
        self.assertIn("FluidSpec", str(ctx.exception))


class TestPatchBoundaryConditions(unittest.TestCase):
    def test_the_four_roles_and_nothing_else(self):
        with self.assertRaises(case.CaseError) as ctx:
            case.PatchBC("inlet")
        self.assertIn("feed, vent, cap, wall", str(ctx.exception))

    def test_a_feed_without_a_flow_rate_is_an_error_not_a_zero(self):
        with self.assertRaises(case.CaseError) as ctx:
            case.PatchBC("feed")
        self.assertIn("q_m3_s", str(ctx.exception))

    def test_a_feed_at_zero_flow_must_say_it_is_a_cap(self):
        with self.assertRaises(case.CaseError) as ctx:
            case.PatchBC("feed", q_m3_s=0.0)
        self.assertIn("cap", str(ctx.exception))

    def test_feeds_with_no_vent_is_refused_before_the_solver_wastes_ten_minutes(self):
        with self.assertRaises(case.CaseError) as ctx:
            case.validate_patches({"a": case.PatchBC("feed", q_m3_s=1e-10),
                                   "w": case.PatchBC("wall")})
        self.assertIn("NO vent", str(ctx.exception))

    def test_no_feed_is_only_a_warning(self):
        warnings = case.validate_patches({"out": case.PatchBC("vent"),
                                          "walls": case.PatchBC("wall")})
        self.assertTrue(any("no feed" in w for w in warnings))

    def test_a_bad_patch_name_is_caught_here_not_by_blockmesh(self):
        with self.assertRaises(case.CaseError):
            case.validate_patches({"2fast": case.PatchBC("wall")})

    def test_u_boundaries_match_the_role_table(self):
        self.assertEqual(case.u_boundary("a", case.PatchBC("vent"))["type"], "zeroGradient")
        self.assertEqual(case.u_boundary("a", case.PatchBC("cap"))["type"], "noSlip")
        self.assertEqual(case.u_boundary("a", case.PatchBC("wall"))["type"], "noSlip")
        feed = case.u_boundary("a", case.PatchBC("feed", q_m3_s=2.5e-11))
        self.assertEqual(feed["type"], "flowRateInletVelocity")
        self.assertEqual(str(feed["volumetricFlowRate"]), "constant 2.5e-11")

    def test_p_boundaries_are_kinematic(self):
        fluid = case.fluid_for(37.0)
        vent = case.p_boundary("a", case.PatchBC("vent", p_pa=1000.0), fluid)
        self.assertEqual(vent["type"], "fixedValue")
        self.assertAlmostEqual(float(vent["value"].split()[-1]), 1000.0 / fluid.rho_kg_m3)
        self.assertEqual(case.p_boundary("a", case.PatchBC("wall"), fluid)["type"], "zeroGradient")

    def test_every_patch_appears_in_both_fields(self):
        fluid = case.fluid_for(37.0)
        patches = {"p_in": case.PatchBC("feed", q_m3_s=1e-11),
                   "p_out": case.PatchBC("vent"),
                   "p_mid": case.PatchBC("cap"),
                   "walls": case.PatchBC("wall")}
        for body in (case.field_u(patches), case.field_p(patches, fluid)):
            self.assertEqual(set(body["boundaryField"]), set(patches))

    def test_a_scenario_record_maps_straight_onto_patches(self):
        from assay2chip.cfd.model import PortState, Scenario

        scenario_obj = Scenario(step=1, ports={
            "p_in": PortState(type="flow", q_ul_min=2.0),
            "p_out": PortState(type="pressure", p_pa=0.0),
            "p_x": PortState(type="closed"),
        })
        patches = case.patches_from_scenario(scenario_obj)
        self.assertEqual(patches["p_in"].role, "feed")
        self.assertAlmostEqual(patches["p_in"].q_m3_s, 2.0 / 6e10)
        self.assertEqual(patches["p_out"].role, "vent")
        self.assertEqual(patches["p_x"].role, "cap")
        self.assertEqual(patches["walls"].role, "wall")

    def test_a_flow_port_at_zero_becomes_a_cap_rather_than_failing(self):
        from assay2chip.cfd.model import PortState, Scenario

        patches = case.patches_from_scenario(Scenario(step=1, ports={
            "p_in": PortState(type="flow", q_ul_min=0.0),
            "p_out": PortState(type="pressure", p_pa=0.0),
        }))
        self.assertEqual(patches["p_in"].role, "cap")


class TestVersionSpecificSpellings(unittest.TestCase):
    """The details that differ between distributions, and would otherwise be guessed."""

    def test_esi_uses_turbulenceProperties(self):
        # VERIFIED against the installed v2606: its own simpleFoam tutorials write this
        # file, and `momentumTransport` appears nowhere in the tree.
        self.assertEqual(case.laminar_model_file("v2606"), "turbulenceProperties")
        self.assertEqual(case.laminar_model_file("2412"), "turbulenceProperties")

    def test_the_foundation_spelling_is_different_and_known(self):
        self.assertEqual(case.laminar_model_file("13"), "momentumTransport")
        self.assertEqual(case.laminar_model_file("9"), "momentumTransport")

    def test_esi_laminar_subdict_keyword(self):
        body = case.momentum_transport("v2606")
        self.assertEqual(body["simulationType"], "laminar")
        self.assertEqual(body["laminar"], {"model": "Stokes"})

    def test_foundation_laminar_subdict_keyword(self):
        self.assertEqual(case.momentum_transport("13")["laminar"], {"laminarModel": "Stokes"})


class TestCaseStructure(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "case"
        self.fluid = case.fluid_for(37.0)
        self.patches = {
            "p_in": case.PatchBC("feed", q_m3_s=1.0 / 6e10),
            "p_out": case.PatchBC("vent"),
            "walls": case.PatchBC("wall"),
        }

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, **kwargs):
        kwargs.setdefault("functions", case.standard_functions(self.patches, include_age=False))
        return case.build_case(self.dir, fluid=self.fluid, patches=self.patches, **kwargs)

    def test_every_file_a_solver_needs_is_written(self):
        built = self.build(n_cores=6)
        for rel in ("constant/transportProperties",
                    f"constant/{built.momentum_transport_file}",
                    "system/controlDict", "system/fvSchemes", "system/fvSolution",
                    "system/decomposeParDict", "0/U", "0/p"):
            self.assertTrue((self.dir / rel).is_file(), f"{rel} missing")

    def test_it_does_not_write_a_mesh(self):
        """mesh.py owns constant/polyMesh and may run before or after this."""
        self.build()
        self.assertFalse((self.dir / "constant" / "polyMesh").exists())

    def test_nu_in_the_case_is_the_contract_value(self):
        self.build()
        written = case.read_entry(self.dir / "constant" / "transportProperties", "nu")
        self.assertIn(case.format_value(self.fluid.nu_m2_s), written)

    def test_decompose_par_dict_uses_scotch_and_the_core_count(self):
        self.build(n_cores=6)
        path = self.dir / "system" / "decomposeParDict"
        self.assertEqual(case.read_entry(path, "numberOfSubdomains"), "6")
        self.assertEqual(case.read_entry(path, "method"), "scotch")

    def test_gamg_solves_the_pressure(self):
        self.build()
        text = (self.dir / "system" / "fvSolution").read_text()
        self.assertIn("solver GAMG;", text)
        self.assertIn("residualControl", text)

    def test_steady_runs_get_steady_state_ddt_and_transient_ones_do_not(self):
        self.build()
        self.assertIn("default steadyState;", (self.dir / "system" / "fvSchemes").read_text())
        built = case.build_case(self.dir, fluid=self.fluid, patches=self.patches,
                                solver="pimpleFoam",
                                functions=case.standard_functions(self.patches, include_age=False))
        self.assertFalse(built.steady)
        self.assertIn("default Euler;", (self.dir / "system" / "fvSchemes").read_text())

    def test_an_age_function_object_brings_its_own_scheme_and_solver(self):
        """Forgetting this kills the solver on iteration one, after the expensive part."""
        built = case.build_case(self.dir, fluid=self.fluid, patches=self.patches,
                                functions={"age": case.age_function()})
        self.assertIn("age", built.functions)
        self.assertIn("div(phi,age)", (self.dir / "system" / "fvSchemes").read_text())
        self.assertIn("age", (self.dir / "system" / "fvSolution").read_text())

    def test_solved_fields_reads_the_type_not_the_key(self):
        self.assertEqual(case.solved_fields({"meanAge": case.age_function()}), ["age"])
        self.assertEqual(case.solved_fields({"age": case.field_min_max()}), [])

    def test_standard_functions_cover_what_the_siblings_asked_for(self):
        functions = case.standard_functions(self.patches, include_age=False)
        self.assertIn("wallShearStress", functions)
        self.assertIn("fieldMinMax", functions)
        self.assertIn("flow_p_in", functions)
        self.assertIn("p_p_out", functions)
        self.assertEqual(functions["flow_p_in"]["fields"], ["phi"])
        self.assertEqual(functions["flow_p_in"]["operation"], "sum")
        self.assertEqual(functions["p_p_out"]["operation"], "areaAverage")

    def test_wall_shear_is_asked_for_on_walls_and_caps_only(self):
        patches = dict(self.patches, p_mid=case.PatchBC("cap"))
        functions = case.standard_functions(patches, include_age=False)
        self.assertEqual(set(functions["wallShearStress"]["patches"]), {"walls", "p_mid"})

    def test_the_summary_is_json_and_names_the_version_and_the_fluid(self):
        built = self.build(foam_version="v2606")
        blob = json.dumps(built.to_dict())
        self.assertIn("v2606", blob)
        self.assertIn("turbulenceProperties", blob)
        self.assertIn(contracts.VISCOSITY_37C_KEY, blob)

    def test_the_digest_is_stable_and_changes_with_the_case(self):
        first = self.build().digest
        self.assertEqual(first, self.build().digest)
        other = dict(self.patches)
        other["p_in"] = case.PatchBC("feed", q_m3_s=2.0 / 6e10)
        changed = case.build_case(self.dir, fluid=self.fluid, patches=other,
                                  functions=case.standard_functions(other, include_age=False))
        self.assertNotEqual(first, changed.digest)


class TestDuctReference(unittest.TestCase):
    """The ruler the OpenFOAM integration test is measured with, checked against itself."""

    def test_the_exact_series_matches_published_shape_factors(self):
        """f Re (on D_h) for a rectangular duct: 96 at h/w -> 0, 56.91 at square.

        Shah & London's table, which every textbook reprints. Deriving it from the
        resistance is the independent check that the series is implemented right.
        """
        for ratio, expected in ((1.0, 56.91), (0.5, 62.19), (0.25, 72.93), (0.125, 82.34)):
            w, h = 1e-3, 1e-3 * ratio
            mu, length = 1e-3, 1.0
            resistance = case.duct_resistance_exact_pa_s_m3(mu, length, w, h)
            area = w * h
            d_h = 4 * area / (2 * (w + h))
            # dP = R Q, u = Q / A, f = 2 d_h dP / (rho u^2 L), Re = rho u d_h / mu
            # => f Re = 2 d_h^2 dP / (mu u L) = 2 d_h^2 R A / (mu L)
            f_re = 2 * d_h ** 2 * resistance * area / (mu * length)
            self.assertAlmostEqual(f_re, expected, delta=0.02,
                                   msg=f"aspect ratio {ratio}")

    def test_the_063_approximation_is_close_but_not_exact(self):
        """It is an engineering fit, so the integration test must not use it as truth."""
        mu, length, w, h = 8e-4, 10e-3, 0.5e-3, 0.3e-3
        exact = case.duct_resistance_exact_pa_s_m3(mu, length, w, h)
        approx = case.duct_resistance_pa_s_m3(mu, length, w, h)
        self.assertNotAlmostEqual(exact, approx, places=12)
        self.assertLess(abs(approx - exact) / exact, 0.02)

    def test_the_law_does_not_care_which_side_is_called_width(self):
        a = case.duct_resistance_exact_pa_s_m3(1e-3, 1e-2, 0.5e-3, 0.3e-3)
        b = case.duct_resistance_exact_pa_s_m3(1e-3, 1e-2, 0.3e-3, 0.5e-3)
        self.assertAlmostEqual(a, b, places=12)

    def test_resistance_scales_as_the_cube_of_the_short_side(self):
        """Halving the depth is 8x the resistance; it is why the weir gap tolerance matters."""
        thin = case.duct_resistance_exact_pa_s_m3(1e-3, 1e-2, 10e-3, 0.05e-3)
        thick = case.duct_resistance_exact_pa_s_m3(1e-3, 1e-2, 10e-3, 0.1e-3)
        self.assertAlmostEqual(thin / thick, 8.0, delta=0.1)

    def test_pressure_drop_is_resistance_times_flow(self):
        q = 1.0 / 6e10
        dp = case.duct_pressure_drop_pa(q, 8e-4, 10e-3, 0.5e-3, 0.3e-3)
        self.assertAlmostEqual(
            dp, q * case.duct_resistance_exact_pa_s_m3(8e-4, 10e-3, 0.5e-3, 0.3e-3))

    def test_the_duct_block_mesh_is_a_closed_box_with_three_patches(self):
        body = case.duct_block_mesh(10e-3, 0.5e-3, 0.3e-3)
        text = case.dumps(body, "dictionary", "blockMeshDict", "system")
        self.assertIn("hex (0 1 2 3 4 5 6 7)", text)
        for patch in ("inlet", "outlet", "walls"):
            self.assertIn(patch, text)
        self.assertEqual(len(body["vertices"].rows), 8)
        # Every one of the box's six faces is claimed exactly once.
        faces = re.findall(r"\(\d \d \d \d\)", text)
        self.assertEqual(len(faces), 6)
        self.assertEqual(len(set(faces)), 6)


if __name__ == "__main__":
    unittest.main()

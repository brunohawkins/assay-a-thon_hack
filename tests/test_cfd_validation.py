"""assay2chip/cfd/validate.py: the benchmark suite, and whether it can actually fail.

    python -m unittest tests.test_cfd_validation -v
    python -m unittest discover -s tests

Three jobs here, and the third is the one that matters.

1. The ANALYTIC side is right. validate.py's exact rectangular-duct series is checked
   against numbers nobody in this repo derived: the textbook square-duct coefficient
   0.035144, the parallel-plate limit, and the 1.6013 / 1.4842 shear factors the audit
   measured (quoted in assay2chip/cfd/solve.py, which reaches them through a different
   series expansion).

2. The HARNESS produces what its consumers read. analyse.py gates every finding on this
   report and report.py renders it, so both are driven here with the real report rather
   than a fixture.

3. The suite CAN FAIL. A benchmark suite that passes whatever it is given is worse than
   none, so `SuiteHasTeeth` hands validate.py a deliberately broken solver - a duct
   conductance 10% off, a shear factor 1% off, pressures scaled by 1.5 - and requires the
   matching benchmark to catch each one.

Offline, no network, and nothing is written outside a temp directory.
"""

import json
import math
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "operator")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import flow_calc  # noqa: E402
from assay2chip.cfd import model, validate  # noqa: E402

CFD = validate.discover_cfd()
#: One full run, shared: it meshes and solves every repo chip and takes a few seconds.
REPORT = validate.run()
MU = 0.8e-3


class _Patched:
    """A module with some attributes replaced. The sabotage rig for `SuiteHasTeeth`."""

    def __init__(self, module, **overrides):
        object.__setattr__(self, "_module", module)
        object.__setattr__(self, "_overrides", overrides)

    def __getattr__(self, name):
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        return getattr(object.__getattribute__(self, "_module"), name)


def _bench(report, bench_id):
    return next(b for b in report["benchmarks"] if b["id"] == bench_id)


class ExactDuctPhysics(unittest.TestCase):
    """The series solution, against values from outside this repo."""

    def test_square_duct_matches_the_textbook_coefficient(self):
        # Poiseuille flow in a square of side a: Q = 0.035144 a^4 (dP/L) / mu, and
        # Q = B a^4 dP / (12 mu L), so B / 12 must be that coefficient.
        b = validate.duct_shape_factor(1.0, 1.0)
        self.assertAlmostEqual(b / 12.0, 0.035144, places=6)
        self.assertAlmostEqual(b, 0.421731, places=5)

    def test_wide_channel_tends_to_the_parallel_plate_limit(self):
        # Not "B is about 1": B approaches 1 from below at a KNOWN rate, 0.6302489 h/w.
        # Asserting the rate is strictly stronger than asserting the limit, and it is what
        # catches a wrong coefficient.
        for ratio in (10.0, 100.0, 1000.0, 10_000.0):
            deficit = 1 - validate.duct_shape_factor(ratio, 1.0)
            self.assertAlmostEqual(deficit / (validate.WIDE_COEFFICIENT / ratio), 1.0,
                                   places=9, msg=f"w/h {ratio}")
            self.assertLess(validate.duct_shape_factor(ratio, 1.0), 1.0)

    def test_the_series_is_converged_not_truncated_even_at_w_over_h_1e4(self):
        # An extreme aspect ratio is where a tanh series is usually suspected of
        # truncating. It does not: every term beyond the first few is 1/n^5 and the sum is
        # at machine precision by 500 terms, so B(1e4) = 1 - 6.3025e-5 is the real value.
        for ratio in (1.0, 2.0, 30.0, 1e4):
            coarse = validate.duct_shape_factor(ratio, 1.0, 500)
            fine = validate.duct_shape_factor(ratio, 1.0, 20_000)
            self.assertLess(abs(coarse - fine), 1e-12, msg=f"w/h {ratio}")
        self.assertAlmostEqual(1 - validate.duct_shape_factor(1e4, 1.0),
                               validate.WIDE_COEFFICIENT * 1e-4, places=15)

    def test_0p63_is_the_rounded_wide_limit_of_the_same_series(self):
        coefficient = 192 * sum(1 / n ** 5 for n in range(1, 20_001, 2)) / math.pi ** 5
        self.assertAlmostEqual(coefficient, validate.WIDE_COEFFICIENT, places=15)
        self.assertAlmostEqual(coefficient, 0.6302489, places=7)
        far = validate.duct_shape_factor(1000.0, 1.0)
        self.assertAlmostEqual(far, 1 - coefficient / 1000, places=12)
        # The contract rounds 0.6302489 down to 0.63, which is the whole reason the
        # formula's error changes sign.
        self.assertLess(0.63, coefficient)

    def test_approx_form_is_exactly_what_operator_flow_calc_computes(self):
        for length, width, depth in ((10, 0.8, 0.3), (3.4, 0.2, 0.3), (8, 0.2, 0.15)):
            self.assertAlmostEqual(
                validate.duct_resistance_approx(length, width, depth, MU)
                / flow_calc.rect_channel_resistance_pa_s_m3(length, width, depth, MU),
                1.0, places=12)

    def test_the_0p63_error_changes_sign_and_the_report_says_where(self):
        # Two effects compete: tanh < 1 makes the true bracket smaller (formula
        # conservative), and rounding 0.6302489 down to 0.63 makes it larger (formula
        # optimistic). Below the crossover the first wins, above it the second.
        crossover = validate._error_sign_crossover()
        self.assertTrue(2.0 < crossover < 3.0, crossover)
        for ratio in (1.0, 1.1, 1.333, 1.5, 2.0):
            width, depth = ratio * 0.3, 0.3
            self.assertGreater(validate.duct_resistance_approx(1, width, depth, MU),
                               validate.duct_resistance_exact(1, width, depth, MU),
                               msg=f"w/h {ratio} should be conservative")
            self.assertGreater(validate.duct_resistance_error(width, depth), 0.0)
        for ratio in (5.0, 10.0, 30.0):
            width, depth = ratio * 0.3, 0.3
            self.assertLess(validate.duct_resistance_error(width, depth), 0.0,
                            msg=f"w/h {ratio} should be slightly optimistic")
            # Optimistic, but never by enough to matter: bounded by the 0.63 rounding.
            self.assertLess(abs(validate.duct_resistance_error(width, depth)), 1.6e-3,
                            msg=f"w/h {ratio}")

    def test_the_error_the_audit_asked_about(self):
        # The square case is the worst in the repo; 0.2 x 0.15 mm is the one the audit named.
        self.assertAlmostEqual(validate.duct_resistance_error(0.3, 0.3), 0.1397, places=3)
        self.assertAlmostEqual(validate.duct_resistance_error(0.2, 0.15), 0.0264, places=3)
        self.assertAlmostEqual(validate.duct_resistance_error(0.2, 0.3), 0.0124, places=3)
        for ratio in (2.0, 3.0, 5.0, 10.0, 30.0):
            self.assertLess(validate.duct_resistance_error(ratio * 0.3, 0.3), 2e-3,
                            msg=f"w/h {ratio}")

    def test_a_hele_shaw_face_carries_no_side_wall_term(self):
        # An interior face has no side wall, so its conductance is the plain plate value.
        g = validate.hele_shaw_face_conductance(0.1, 0.1, 0.3, MU)
        plate = (0.1e-3) * (0.3e-3) ** 3 / (12 * MU * 0.1e-3)
        self.assertAlmostEqual(g / plate, 1.0, places=12)
        duct = 1 / validate.duct_resistance_approx(0.1, 0.1, 0.3, MU)
        self.assertGreater(g, duct)


class WallShear(unittest.TestCase):
    """The correction the slot formula needs, and the solver's own version of it."""

    def test_matches_the_factors_the_audit_measured(self):
        self.assertAlmostEqual(validate.wall_shear_correction(1.0, 1.0), 1.6013, places=4)
        self.assertAlmostEqual(validate.wall_shear_correction(1.33, 1.0), 1.4842, places=3)

    def test_never_optimistic_and_falls_monotonically(self):
        factors = [validate.wall_shear_correction(r, 1.0)
                   for r in (1, 1.5, 2, 3, 5, 10, 20, 30, 100)]
        self.assertTrue(all(f >= 1.0 for f in factors), factors)
        self.assertEqual(factors, sorted(factors, reverse=True))

    def test_wide_limit_is_one_over_the_0p63_bracket(self):
        for ratio in (10.0, 30.0, 100.0):
            self.assertAlmostEqual(validate.wall_shear_correction(ratio, 1.0),
                                   1 / (1 - 0.63 / ratio), places=4)

    def test_slot_formula_approaches_the_exact_peak_at_a_known_rate(self):
        # The slot form is the h/w -> 0 limit, and its shortfall is 0.63 h/w to leading
        # order. Asserting that rate catches a wrong limit, where "about 1" would not.
        q = 10 * validate.UL_MIN_TO_M3_S
        for width in (3.0, 30.0, 300.0, 3000.0):
            slot = validate.wall_shear_slot(q, width, 0.3, MU)
            exact = validate.wall_shear_max_exact(q, width, 0.3, MU)
            self.assertLess(slot, exact, msg=f"w {width}")
            shortfall = 1 - slot / exact
            self.assertAlmostEqual(shortfall / (0.63 * 0.3 / width), 1.0, delta=0.02,
                                   msg=f"w {width}")

    def test_mean_shear_is_the_force_balance_value(self):
        # tau_mean = G A / P, so for a square it is G a / 4 exactly.
        q = 10 * validate.UL_MIN_TO_M3_S
        a = 0.3
        g = validate.duct_resistance_exact(1.0, a, a, MU) * q / 1e-3
        self.assertAlmostEqual(validate.wall_shear_mean_exact(q, a, a, MU),
                               g * a * 1e-3 / 4, places=12)

    @unittest.skipUnless(CFD.available, "assay2chip/cfd/solve.py not importable")
    def test_two_independent_series_agree(self):
        for ratio in (1.0, 1.333, 1.5, 2.0, 5.0, 30.0):
            width, depth = ratio * 0.3, 0.3
            self.assertAlmostEqual(CFD.solve.rect_shear_factor(width, depth)
                                   / validate.wall_shear_correction(width, depth),
                                   1.0, places=6, msg=f"w/h {ratio}")


class ReferenceSolver(unittest.TestCase):
    """validate.py's own network solver - the tiebreaker the cross-check verdict rests on."""

    def setUp(self):
        self.r = [validate.duct_resistance_exact(*d, MU) for d in validate.BENCH_DUCTS]
        self.q = 10 * validate.UL_MIN_TO_M3_S

    def test_series_resistances_add(self):
        net = validate.LinearNetwork(["in", "a", "b", "out"])
        for k, (x, y) in enumerate((("in", "a"), ("a", "b"), ("b", "out"))):
            net.add_edge(x, y, 1 / self.r[k], f"s{k}")
        sol = net.solve({"in": self.q}, {"out": 0.0})
        self.assertAlmostEqual(sol["p_pa"]["in"] / (sum(self.r) * self.q), 1.0, places=9)
        for k in range(3):
            self.assertAlmostEqual(sol["q_m3_s"][f"s{k}"] / self.q, 1.0, places=9)

    def test_parallel_conductances_add_and_split_by_conductance(self):
        net = validate.LinearNetwork(["in", "out"])
        for k in range(3):
            net.add_edge("in", "out", 1 / self.r[k], f"p{k}")
        sol = net.solve({"in": self.q}, {"out": 0.0})
        r_par = 1 / sum(1 / x for x in self.r)
        self.assertAlmostEqual(sol["p_pa"]["in"] / (r_par * self.q), 1.0, places=9)
        for k in range(3):
            self.assertAlmostEqual(sol["q_m3_s"][f"p{k}"] / (self.q * r_par / self.r[k]),
                                   1.0, places=9)

    def test_mass_is_conserved_at_every_interior_node(self):
        net = validate.LinearNetwork(["in", "a", "b", "out"])
        net.add_edge("in", "a", 1 / self.r[0], "e0")
        net.add_edge("a", "b", 1 / self.r[1], "e1")
        net.add_edge("a", "b", 1 / self.r[2], "e2")
        net.add_edge("b", "out", 1 / self.r[0], "e3")
        sol = net.solve({"in": self.q}, {"out": 0.0})
        self.assertLess(sol["max_interior_imbalance_frac"], 1e-12)

    def test_reference_network_reads_the_weirs_out_of_the_chip_spec(self):
        chip = json.loads((ROOT / "chip" / "module1_v1.json").read_text())
        with_weirs, meta = validate.reference_network(chip, MU, weirs=True)
        without, _ = validate.reference_network(chip, MU, weirs=False)
        crests = [c for m in meta.values() for c in m["weir_crests"]]
        self.assertTrue(crests, "module1_v1 declares weirs on g_cx and a_cx")
        self.assertEqual({c["gap_um"] for c in crests}, {25.0})
        # A 25 um crest is a bigger resistance than the channel it sits on: gap^3.
        channel = validate.duct_resistance_exact(3.4, 0.2, 0.3, MU)
        self.assertGreater(min(c["r_pa_s_m3"] for c in crests), channel)
        sources = {"p_g_in": 1 * validate.UL_MIN_TO_M3_S}
        fixed = {"p_mid": 0.0, "p_in": 0.0, "p_a_out": 0.0, "p_g_out": 0.0, "p_a_in": 0.0}
        self.assertGreater(with_weirs.solve(sources, fixed)["p_pa"]["p_g_in"],
                           2 * without.solve(sources, fixed)["p_pa"]["p_g_in"])


class UnroutedChips(unittest.TestCase):
    """S6 writes a chip before S7 routes it, and `chip/` holds both at once.

    A spec with no channels has no flow, so it is not a conservation failure and not a
    solver failure. Pinning a mid-pipeline artefact on the solver would be the easiest way
    for this suite to cry wolf, so the classification is tested rather than assumed.
    """

    def test_a_chip_with_no_channels_is_not_routed(self):
        chambers = [{"id": "c1", "center": [1, 1, 0], "size": [1, 1, 0.3]}]
        self.assertFalse(validate._is_routed({"chambers": chambers, "channels": []}))
        self.assertFalse(validate._is_routed({"chambers": chambers}))
        self.assertFalse(validate._is_routed({"chambers": [], "channels": [{"id": "x"}]}))
        self.assertTrue(validate._is_routed({"chambers": chambers,
                                             "channels": [{"id": "ch"}]}))

    def test_every_repo_chip_the_suite_solved_is_routed(self):
        detail = _bench(REPORT, "mass_conservation")["detail"]
        for chip in detail["chips"]:
            path = ROOT / "chip" / f"{chip['chip_id']}.json"
            if not path.exists():          # another agent may add or remove one mid-run
                continue
            routed = validate._is_routed(json.loads(path.read_text()))
            self.assertEqual(routed, "skipped" not in chip, msg=chip["chip_id"])

    def test_unrouted_chips_are_reported_but_do_not_fail_the_benchmark(self):
        detail = _bench(REPORT, "mass_conservation")["detail"]
        self.assertIn("chips_unrouted", detail)
        self.assertEqual(_bench(REPORT, "mass_conservation")["status"], "pass")
        if detail["chips_unrouted"]:
            note = next(f for f in REPORT["validation_findings"]
                        if f["id"] == "validate_unrouted_chips")
            self.assertEqual(note["severity"], "low")


class Richardson(unittest.TestCase):
    def test_recovers_a_known_second_order_sequence(self):
        exact, c = 3.0, 0.5
        spacings = [0.2, 0.1, 0.05]
        fit = validate._richardson([exact + c * h ** 2 for h in spacings], spacings)
        self.assertAlmostEqual(fit["order"], 2.0, places=6)
        self.assertAlmostEqual(fit["extrapolated"], exact, places=9)

    def test_recovers_a_known_first_order_sequence(self):
        spacings = [0.2, 0.1, 0.05]
        fit = validate._richardson([1.0 + 2 * h for h in spacings], spacings)
        self.assertAlmostEqual(fit["order"], 1.0, places=6)
        self.assertAlmostEqual(fit["extrapolated"], 1.0, places=9)

    def test_refuses_to_fit_an_oscillating_sequence(self):
        fit = validate._richardson([1.0, 1.2, 1.0], [0.2, 0.1, 0.05])
        self.assertIsNone(fit["order"])
        self.assertIsNone(fit["gci_fine_pct"])

    def test_area_quantile_is_area_weighted_not_cell_weighted(self):
        import numpy as np
        slow = np.array([1.0, 10.0, 10.0, 10.0])
        # One big slow cell against three small fast ones: the 5% area point is the slow one.
        self.assertEqual(validate._area_quantile(slow, np.array([9.0, 1, 1, 1]), 0.05), 1.0)
        self.assertEqual(validate._area_quantile(slow, np.array([0.01, 1, 1, 1]), 0.5), 10.0)


class ReportShape(unittest.TestCase):
    """What analyse.py and report.py actually read out of this file."""

    def test_top_level_keys(self):
        for key in ("schema", "status", "passed", "trusted", "reason", "benchmarks",
                    "checks", "validation_findings", "recommended_grid_mm", "counts",
                    "contracts", "mu_pa_s", "flow_ul_min"):
            self.assertIn(key, REPORT)
        self.assertEqual(REPORT["schema"], "cfd-validation/1")
        self.assertIn(REPORT["status"], ("pass", "fail", "incomplete"))

    def test_every_benchmark_is_fully_described(self):
        self.assertEqual(len(REPORT["benchmarks"]), len(validate.BENCHMARKS))
        for b in REPORT["benchmarks"]:
            for key in ("id", "name", "requirement", "solver", "status", "metric",
                        "value", "tolerance", "units", "message", "detail", "passed"):
                self.assertIn(key, b, msg=b["id"])
            self.assertIn(b["status"], ("pass", "fail", "skipped"), msg=b["id"])
            self.assertTrue(b["message"], msg=f"{b['id']} must say what it found")

    def test_checks_are_the_shape_report_py_renders(self):
        self.assertEqual(len(REPORT["checks"]), len(REPORT["benchmarks"]))
        for c in REPORT["checks"]:
            self.assertEqual(set(c), {"name", "ok", "expected", "got", "detail"})
            self.assertIsInstance(c["ok"], bool)

    def test_carries_every_key_cfd_run_py_reads_off_it(self):
        # assay2chip/cfd/run.py builds its own validation.json from this dict. These are
        # the keys it reaches for; a rename here silently blanks a line in the report.
        for key in ("benchmarks", "status", "trusted", "counts", "solver",
                    "recommended_grid_mm", "validation_findings", "untrusted_reason"):
            self.assertIn(key, REPORT, msg="assay2chip/cfd/run.py reads this")
        for bench in REPORT["benchmarks"]:
            for key in ("id", "name", "status", "tolerance", "value", "metric",
                        "units", "message"):
                self.assertIn(key, bench, msg="assay2chip/cfd/run.py reads this")
        self.assertEqual(REPORT["untrusted_reason"], REPORT["reason"])

    def test_run_is_the_entry_point_run_py_looks_up(self):
        self.assertTrue(callable(validate.run))

    def test_the_report_is_json_without_a_fallback_encoder(self):
        # Consumers get the dict, not only the file: a stray numpy scalar would break them.
        json.dumps(REPORT)

    def test_findings_are_mergeable_into_findings_json(self):
        for f in REPORT["validation_findings"]:
            for key in ("id", "class", "severity", "chip_id", "feature_id", "metric",
                        "value", "limit", "units", "message", "fix_hint", "source"):
                self.assertIn(key, f)
            self.assertIn(f["severity"], model.SEVERITIES, msg=f["id"])
            self.assertEqual(f["source"], "validate")
            self.assertTrue(f["message"].strip())
            finding = model.Finding.from_json(f)      # must survive the real dataclass
            self.assertEqual(finding.to_json()["class"], f["class"])
        ids = [f["id"] for f in REPORT["validation_findings"]]
        self.assertEqual(len(ids), len(set(ids)), "finding ids must be unique")

    def test_writes_an_atomic_report_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "deeper" / "validation.json"
            self.assertEqual(validate.write_report(REPORT, out), out)
            self.assertEqual(json.loads(out.read_text())["schema"], "cfd-validation/1")
            self.assertFalse(list(out.parent.glob("*.tmp")))

    def test_renders_a_table_without_a_solver(self):
        report = validate.run(cfd=validate.CfdUnderTest(reason="not built yet"))
        self.assertEqual(report["status"], "incomplete")
        self.assertFalse(report["passed"])
        self.assertIn("not built yet", report["reason"])
        self.assertIn("not built yet", validate.format_table(report))
        # The analytic benchmarks do not need a solver and must still run.
        self.assertEqual(_bench(report, "duct_resistance")["status"], "pass")
        self.assertEqual(_bench(report, "wall_shear")["status"], "pass")
        self.assertEqual(_bench(report, "series_parallel")["status"], "skipped")


class TheSuitePasses(unittest.TestCase):
    """The headline: does the CFD in this repo pass its own benchmarks?"""

    def test_every_benchmark_passes(self):
        failed = [b["id"] for b in REPORT["benchmarks"] if b["status"] != "pass"]
        self.assertEqual(failed, [], msg=REPORT["reason"])
        self.assertEqual(REPORT["status"], "pass")
        self.assertTrue(REPORT["passed"] and REPORT["trusted"])

    def test_it_was_run_against_the_real_solver_at_the_contract_viscosity(self):
        self.assertTrue(REPORT["solver"]["available"])
        self.assertEqual(REPORT["mu_pa_s"], 0.0008)
        self.assertEqual(REPORT["viscosity_source"], "fluid.viscosity_37c_pa_s")

    def test_analyse_py_gate_lets_a_passing_report_through(self):
        from assay2chip.cfd import analyse
        self.assertEqual(analyse.validation_failures(REPORT), [])

    def test_analyse_py_gate_catches_a_failing_benchmark(self):
        from assay2chip.cfd import analyse
        broken = dict(REPORT, benchmarks=[dict(REPORT["benchmarks"][0], passed=False)]
                      + REPORT["benchmarks"][1:])
        self.assertTrue(analyse.validation_failures(broken))

    def test_report_py_calls_it_validated(self):
        from assay2chip.cfd import report as cfd_report
        verdict, trusted, _ = cfd_report.validation_verdict({"validation": REPORT})
        self.assertEqual(verdict, "VALIDATED")
        self.assertTrue(trusted)

    def test_it_recommends_a_grid_spacing_and_says_why(self):
        grid = REPORT["recommended_grid_mm"]
        self.assertIsInstance(grid, float)
        self.assertTrue(0.0 < grid <= 0.2)
        detail = _bench(REPORT, "grid_convergence")["detail"]
        self.assertEqual(detail["recommended_grid_mm"], grid)
        # The recommendation must be inside tolerance on every quantity it was fitted on.
        row = next(r for r in detail["runs"] if r["requested_grid_mm"] == grid)
        for key in detail["usable_qois"]:
            self.assertLessEqual(row[f"{key}_error"], detail["tolerance"], msg=key)
        self.assertTrue(detail["usable_qois"])

    def test_pressure_converges_at_about_second_order(self):
        detail = _bench(REPORT, "grid_convergence")["detail"]
        order = detail["qois"]["inlet_pressure_pa"]["order"]
        self.assertTrue(1.0 <= order <= 2.5, order)
        self.assertEqual([r["requested_grid_mm"] for r in detail["runs"]], [0.2, 0.1, 0.05])

    def test_mass_is_conserved_on_every_repo_chip(self):
        detail = _bench(REPORT, "mass_conservation")["detail"]
        solved = [c for c in detail["chips"] if "skipped" not in c]
        self.assertGreaterEqual(len(solved), 9)
        for chip in solved:
            self.assertLess(chip["max_node_imbalance_frac"], validate.TOL["mass_imbalance"],
                            msg=chip["chip_id"])
            self.assertLess(chip["total_in_vs_out_error"], validate.TOL["mass_imbalance"],
                            msg=chip["chip_id"])
            self.assertTrue(chip["residual_agrees"], msg=chip["chip_id"])

    def test_the_crosscheck_names_which_solver_is_wrong(self):
        detail = _bench(REPORT, "operator_crosscheck")["detail"]
        self.assertEqual(detail["which_is_wrong"], "operator/network.py")
        worst = detail["worst"]
        # The CFD matches an independent network built here; operator/network.py matches
        # that same network only when the spec's weirs are left out of it.
        self.assertLess(worst["B_cfd_vs_reference_p"], validate.TOL["crosscheck"])
        self.assertLess(worst["B_cfd_vs_reference_q"], validate.TOL["crosscheck"])
        self.assertLess(worst["A_operator_vs_reference_p"], validate.TOL["crosscheck"])
        self.assertGreater(worst["C_cfd_vs_operator_p"], 0.1)
        self.assertIn("operator/network.py IS THE ONE THAT IS WRONG", detail["verdict"])
        self.assertIn("NOTHING WAS TUNED", detail["verdict"])

    def test_the_crosscheck_covers_every_module1_mode(self):
        import network as nw
        detail = _bench(REPORT, "operator_crosscheck")["detail"]
        covered = {m["mode"] for m in detail["modes"] if "skipped" not in m}
        expected = set(nw.MODULE1_MODES) - {"idle"}
        self.assertEqual(covered, expected)
        for mode in detail["modes"]:
            self.assertLess(mode["cfd_mass_imbalance_vs_inflow"], 1e-8, msg=mode["mode"])

    def test_it_reports_where_a_depth_averaged_model_is_questionable(self):
        detail = _bench(REPORT, "hele_shaw_validity")["detail"]
        self.assertEqual(detail["aspect_floor"], 5.0)
        self.assertTrue(detail["channels_all_meshed_as_ducts"])
        for row in detail["features"]:
            self.assertLess(row["w_over_h"], 5.0)
        # Sub-floor chambers are a caveat on those chambers, not a reason to distrust the
        # solver, so this benchmark must not gate the findings.
        self.assertNotIn("hele_shaw_validity", validate.GATING)
        self.assertTrue(detail["chambers_meshed_2d_below_floor"])

    def test_the_0p63_audit_question_is_answered_across_the_aspect_range(self):
        detail = _bench(REPORT, "duct_resistance")["detail"]
        ratios = [row["w_over_h"] for row in detail["aspect_sweep"]]
        self.assertEqual(min(ratios), 1.0)
        self.assertEqual(max(ratios), 30.0)
        self.assertAlmostEqual(detail["worst_overall"]["w_over_h"], 1.0)
        sections = {(r["width_mm"], r["depth_mm"]): r for r in detail["repo_sections"]}
        self.assertIn((0.2, 0.15), sections)
        self.assertAlmostEqual(sections[(0.2, 0.15)]["r_error_pct"], 2.64, places=1)
        self.assertAlmostEqual(detail["square_duct_check"]["textbook_q_coefficient"],
                               detail["square_duct_check"]["textbook_value"], places=6)
        # The report must carry the evidence that the series is converged, and must say
        # which side of the sign change each aspect ratio is on.
        self.assertLess(detail["series_convergence"]["max_change_from_10x_terms"], 1e-12)
        self.assertTrue(2.0 < detail["sign_of_the_error"]["crossover_w_over_h"] < 3.0)
        # mesh.py must build its duct edges from the contract form, exactly.
        self.assertLess(detail["mesh_vs_contract_form_max_error"],
                        validate.TOL["law_consistency"])


class SuiteHasTeeth(unittest.TestCase):
    """Sabotage the solver and require the right benchmark to catch it.

    If these pass with a broken solver, the suite is decoration.
    """

    @unittest.skipUnless(CFD.available, "assay2chip/cfd not importable")
    def test_a_duct_conductance_10_percent_off_is_caught(self):
        real = CFD.mesh.duct_g_geom_m3
        broken = validate.CfdUnderTest(
            model=CFD.model, solve=CFD.solve, reason="",
            mesh=_Patched(CFD.mesh, duct_g_geom_m3=lambda *a, **k: real(*a, **k) * 1.1))
        bench, _ = validate.bench_duct_resistance(
            {"mu": MU, "flow_ul_min": 1.0, "grid_mm": 0.1, "cfd": broken})
        self.assertEqual(bench.status, "fail")
        self.assertGreater(bench.detail["mesh_vs_contract_form_max_error"], 0.05)

    @unittest.skipUnless(CFD.available, "assay2chip/cfd not importable")
    def test_a_shear_factor_1_percent_off_is_caught(self):
        real = CFD.solve.rect_shear_factor
        broken = validate.CfdUnderTest(
            model=CFD.model, mesh=CFD.mesh, reason="",
            solve=_Patched(CFD.solve, rect_shear_factor=lambda *a: real(*a) * 1.01))
        bench, findings = validate.bench_wall_shear(
            {"mu": MU, "flow_ul_min": 1.0, "grid_mm": 0.1, "cfd": broken})
        self.assertEqual(bench.status, "fail")
        self.assertIn("validate_shear_factor", [f["id"] for f in findings])

    @unittest.skipUnless(CFD.available, "assay2chip/cfd not importable")
    def test_pressures_scaled_by_1_5_are_caught(self):
        real = CFD.solve.solve_step

        def scaled(mesh, scenario, topology=None):
            field = real(mesh, scenario, topology)
            return replace(field, p_pa={k: v * 1.5 for k, v in field.p_pa.items()})

        broken = validate.CfdUnderTest(model=CFD.model, mesh=CFD.mesh, reason="",
                                       solve=_Patched(CFD.solve, solve_step=scaled))
        ctx = {"mu": MU, "flow_ul_min": 1.0, "grid_mm": 0.1, "cfd": broken}
        self.assertEqual(validate.bench_series_parallel(ctx)[0].status, "fail")
        self.assertEqual(validate.bench_hele_shaw_uniform(ctx)[0].status, "fail")

    @unittest.skipUnless(CFD.available, "assay2chip/cfd not importable")
    def test_a_broken_solver_makes_the_whole_report_untrusted(self):
        real = CFD.solve.rect_shear_factor
        broken = validate.CfdUnderTest(
            model=CFD.model, mesh=CFD.mesh, reason="",
            solve=_Patched(CFD.solve, rect_shear_factor=lambda *a: real(*a) * 1.01))
        report = validate.run(cfd=broken)
        self.assertEqual(report["status"], "fail")
        self.assertFalse(report["passed"])
        self.assertFalse(report["trusted"])
        self.assertIn("wall_shear", report["reason"])
        from assay2chip.cfd import analyse
        self.assertTrue(analyse.validation_failures(report))

    def test_a_benchmark_that_raises_becomes_a_skip_not_a_crash(self):
        def exploding(_ctx):
            raise RuntimeError("the mesher ate the chip")

        original = validate.BENCHMARKS
        try:
            validate.BENCHMARKS = (exploding,)
            exploding.__name__ = "bench_boom"
            report = validate.run(cfd=validate.CfdUnderTest(reason="n/a"))
        finally:
            validate.BENCHMARKS = original
        self.assertEqual(report["counts"], {"pass": 0, "fail": 0, "skipped": 1})
        self.assertIn("the mesher ate the chip", report["benchmarks"][0]["message"])
        self.assertFalse(report["passed"])


class Cli(unittest.TestCase):
    def test_main_writes_the_report_and_exits_zero_when_it_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "validation.json"
            code = validate.main(["--out", str(out), "--quiet"])
            self.assertEqual(code, 0)
            written = json.loads(out.read_text())
            self.assertEqual(written["status"], "pass")
            self.assertEqual(len(written["benchmarks"]), len(validate.BENCHMARKS))

    def test_default_output_is_in_the_work_dir(self):
        self.assertTrue(validate.DEFAULT_OUT.is_relative_to(ROOT / "assay2chip" / "work"))


if __name__ == "__main__":
    unittest.main()

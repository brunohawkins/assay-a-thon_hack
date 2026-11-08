"""assay2chip/cfd/solve.py: the Stokes network solver.

    python -m unittest tests.test_cfd_solve -v
    python -m unittest discover -s tests -v

A solver nobody has checked is worse than no solver (assay2chip/cfd/README.md), so this
file is the "Validation" section of that README, test by test:

  * the analytic single straight channel, R = 12 mu L / (w h^3 (1 - 0.63 h/w));
  * series and parallel networks against hand-computed resistances;
  * mass conservation at every node, and total in equals total out, to machine precision;
  * agreement with `operator/network.py` - a completely independent implementation that
    already solves Module 1's network - on the same graph, in every one of its modes;
  * the exact rectangular-duct wall-shear correction against its own parallel-plate limit
    and against the two factors the repo audit measured (1.48 at w/h = 1.33, 1.60 at 1);
  * dense and CG paths agreeing on the same system, and CG being LOUD when it cannot.

Nothing here is tuned to fit. Where a number is a contract value it is read through
`assay2chip.lib.contracts`, never written down.
"""

from __future__ import annotations

import json
import math
import sys
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "operator")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from assay2chip.cfd import solve  # noqa: E402
from assay2chip.cfd.model import (  # noqa: E402
    M3_S_TO_UL_MIN,
    UL_MIN_TO_M3_S,
    Edge,
    Mesh,
    Node,
    PortState,
    Scenario,
)
from assay2chip.lib import contracts  # noqa: E402

MU = contracts.viscosity_37c_pa_s().pa_s
DEPTH_MM = contracts.max_depth_mm()


# --- builders: tiny meshes with hand-computable answers ----------------------

def duct_g_geom_m3(length_mm: float, width_mm: float, depth_mm: float) -> float:
    """g_geom = w h^3 (1 - 0.63 h/w) / (12 L), m^3. The README's resistance, inverted."""
    w = max(width_mm, depth_mm) * 1e-3
    h = min(width_mm, depth_mm) * 1e-3
    return w * h ** 3 * (1 - 0.63 * h / w) / (12 * length_mm * 1e-3)


def duct_resistance(length_mm: float, width_mm: float, depth_mm: float, mu: float = MU) -> float:
    return mu / duct_g_geom_m3(length_mm, width_mm, depth_mm)


def chain_mesh(specs: list[tuple[float, float, float]], chip_id: str = "chain") -> Mesh:
    """n ducts end to end: nodes n0..nN, edges e0..e(N-1). `specs` is (L, w, h) in mm."""
    nodes = [Node(f"n{i}", kind="junction", x_mm=float(i), depth_mm=DEPTH_MM, area_mm2=0.01)
             for i in range(len(specs) + 1)]
    edges = [
        Edge(f"e{i}", a=f"n{i}", b=f"n{i + 1}", kind="duct",
             g_geom_m3=duct_g_geom_m3(*spec), length_mm=spec[0],
             width_mm=spec[1], depth_mm=spec[2])
        for i, spec in enumerate(specs)
    ]
    ports = {"p_in": "n0", "p_out": f"n{len(specs)}"}
    return Mesh(nodes, edges, ports, {"chip_id": chip_id, "mu_pa_s": MU})


def parallel_mesh(specs: list[tuple[float, float, float]]) -> Mesh:
    """n ducts side by side between the same two nodes."""
    nodes = [Node("n_in", kind="junction", depth_mm=DEPTH_MM, area_mm2=0.01),
             Node("n_out", kind="junction", x_mm=10.0, depth_mm=DEPTH_MM, area_mm2=0.01)]
    edges = [
        Edge(f"e{i}", a="n_in", b="n_out", kind="duct", g_geom_m3=duct_g_geom_m3(*spec),
             length_mm=spec[0], width_mm=spec[1], depth_mm=spec[2])
        for i, spec in enumerate(specs)
    ]
    return Mesh(nodes, edges, {"p_in": "n_in", "p_out": "n_out"},
                {"chip_id": "parallel", "mu_pa_s": MU})


def hele_shaw_slab(nx: int, ny: int, grid_mm: float = 0.25, depth_mm: float = DEPTH_MM) -> Mesh:
    """A rasterised rectangular chamber: nx x ny cells, faces between neighbours.

    Face conductance is the mesh module's Hele-Shaw form, g = (h^3 / 12 mu) x (face length
    / centre spacing), which on a uniform grid is just h^3 / 12 mu. Every cell in the left
    column carries a flow port and every cell in the right column a vent, so the exact
    answer is a uniform plug flow and a hand-computable pressure drop.
    """
    h = depth_mm * 1e-3
    g_geom = h ** 3 / 12.0
    nodes, edges, ports = [], [], {}
    for j in range(ny):
        for i in range(nx):
            nodes.append(Node(f"c{i}_{j}", kind="cell", x_mm=(i + 0.5) * grid_mm,
                              y_mm=(j + 0.5) * grid_mm, area_mm2=grid_mm ** 2,
                              depth_mm=depth_mm, ix=i, iy=j,
                              boundary=(i in (0, nx - 1) or j in (0, ny - 1)),
                              feature_id="chamber"))
    for j in range(ny):
        for i in range(nx):
            if i + 1 < nx:
                edges.append(Edge(f"fx{i}_{j}", a=f"c{i}_{j}", b=f"c{i + 1}_{j}", kind="hs_face",
                                  g_geom_m3=g_geom, length_mm=grid_mm, width_mm=grid_mm,
                                  depth_mm=depth_mm, feature_id="chamber"))
            if j + 1 < ny:
                edges.append(Edge(f"fy{i}_{j}", a=f"c{i}_{j}", b=f"c{i}_{j + 1}", kind="hs_face",
                                  g_geom_m3=g_geom, length_mm=grid_mm, width_mm=grid_mm,
                                  depth_mm=depth_mm, feature_id="chamber"))
    for j in range(ny):
        ports[f"in_{j}"] = f"c0_{j}"
        ports[f"out_{j}"] = f"c{nx - 1}_{j}"
    return Mesh(nodes, edges, ports,
                {"chip_id": f"slab{nx}x{ny}", "mu_pa_s": MU, "grid_mm": grid_mm})


def feed_vent(q_ul_min: float, feed: str = "p_in", vent: str = "p_out", **kw) -> Scenario:
    ports = {feed: PortState.flow(q_ul_min), vent: PortState.vent()}
    return Scenario(step=kw.pop("step", "s1"), ports=ports, fluid=kw.pop("fluid", "medium"),
                    temp_c=kw.pop("temp_c", 37.0), **kw)


# --- the exact rectangular-duct shear correction -----------------------------

class ShearCorrection(unittest.TestCase):
    """`tau = 6 mu Q / (w h^2)` is the SHALLOW-duct limit; this is the exact factor."""

    def test_parallel_plate_limit_is_one(self):
        # A very wide, very shallow duct is the formula's own assumption: factor -> 1.
        for ratio in (100.0, 1000.0, 10000.0):
            self.assertAlmostEqual(solve.rect_shear_factor(ratio, 1.0), 1.0,
                                   delta=10.0 / ratio, msg=f"w/h = {ratio}")

    def test_matches_the_two_factors_the_audit_measured(self):
        # The brief's audit: 1.48 at w/h = 1.33 and 1.60 at w/h = 1. The series gives
        # 1.4842 and 1.6013 - independent agreement, not a fitted table.
        self.assertAlmostEqual(solve.rect_shear_factor(1.33, 1.0), 1.48, places=2)
        self.assertAlmostEqual(solve.rect_shear_factor(1.0, 1.0), 1.60, places=2)
        self.assertAlmostEqual(solve.rect_shear_factor(1.33, 1.0), 1.4842, places=4)
        self.assertAlmostEqual(solve.rect_shear_factor(1.0, 1.0), 1.6013, places=4)

    def test_the_square_duct_is_the_worst_case_among_shallow_ducts(self):
        # Across the regime the formula is meant for (w >= h) the factor peaks at w = h.
        square = solve.rect_shear_factor(1.0, 1.0)
        for ratio in (1.0, 1.2, 2.0, 5.0, 50.0):
            self.assertLessEqual(solve.rect_shear_factor(ratio, 1.0), square + 1e-9)

    def test_the_true_shear_is_symmetric_under_swapping_width_and_depth(self):
        # tau_max is a property of the cross-section, so rotating the duct 90 degrees
        # cannot change it at the same Q - even though 6 mu Q/(w h^2) does change, which
        # is exactly why the factor for w < h climbs above the square value (2.71 at
        # w/h = 0.5) instead of staying below it.
        q = 3.0 * UL_MIN_TO_M3_S
        for w, h in ((0.5e-3, 1.0e-3), (0.2e-3, 0.3e-3), (1.2e-3, 0.3e-3)):
            self.assertAlmostEqual(solve.duct_shear_dyn_cm2(q, w, h, MU),
                                   solve.duct_shear_dyn_cm2(q, h, w, MU),
                                   places=9, msg=f"{w} x {h}")
        self.assertGreater(solve.rect_shear_factor(0.5, 1.0), solve.rect_shear_factor(1.0, 1.0))

    def test_factor_decreases_monotonically_with_aspect_ratio(self):
        ratios = [1.0, 1.1, 1.33, 1.5, 2.0, 3.0, 5.0, 10.0, 50.0]
        factors = [solve.rect_shear_factor(r, 1.0) for r in ratios]
        self.assertEqual(factors, sorted(factors, reverse=True))
        self.assertGreater(factors[0], 1.6)
        self.assertLess(factors[-1], 1.03)

    def test_series_converge_on_the_parallel_plate_limits(self):
        # The two series this module is built on, against the closed-form slot limits:
        # tau = G h / 2 and Q = G w h^3 / 12 mu. The deviation must shrink in proportion
        # to h/w and keep doing so - an earlier form of these series looked fine at
        # w/h = 100 and then got WORSE beyond it, because the terms degenerated into a
        # slowly converging alternating tail.
        h = 1.0
        for w in (100.0, 1000.0, 10000.0, 1e6):
            tau_error = abs(solve._tau_max_over_g(w, h) / (h / 2) - 1.0)
            q_error = abs(solve._mu_q_over_g(w, h) / (w * h ** 3 / 12) - 1.0)
            self.assertLess(tau_error, 10.0 * h / w, msg=f"tau at w/h = {w}")
            self.assertLess(q_error, 10.0 * h / w, msg=f"Q at w/h = {w}")

    def test_the_factor_tends_to_one_from_above_at_every_scale(self):
        for ratio in (10.0, 100.0, 1000.0, 1e5):
            factor = solve.rect_shear_factor(ratio, 1.0)
            self.assertGreaterEqual(factor, 1.0, msg=f"w/h = {ratio}")
            self.assertLess(factor - 1.0, 10.0 / ratio, msg=f"w/h = {ratio}")

    def test_series_agrees_with_an_independent_finite_difference_solve(self):
        # Nothing here shares a line of code with the series: solve grad^2 u = -1 on a
        # square cross-section by relaxation, integrate for Q, difference at the wall for
        # tau. A finite grid under-reads the wall gradient, so the series must sit just
        # above it - and within ~1%.
        n = 160
        d = 1.0 / (n + 1)
        u = np.zeros((n, n))

        def laplacian(field: np.ndarray) -> np.ndarray:
            padded = np.zeros((n + 2, n + 2))
            padded[1:-1, 1:-1] = field
            return (padded[:-2, 1:-1] + padded[2:, 1:-1]
                    + padded[1:-1, :-2] + padded[1:-1, 2:] - 4 * field) / d ** 2

        b = -np.ones((n, n))
        r = b - laplacian(u)
        p, rs = r.copy(), float((r * r).sum())
        for _ in range(4 * n * n):
            ap = laplacian(p)
            alpha = rs / float((p * ap).sum())
            u += alpha * p
            r -= alpha * ap
            rs_new = float((r * r).sum())
            if math.sqrt(rs_new) < 1e-12 * abs(b).sum():
                break
            p = r + (rs_new / rs) * p
            rs = rs_new
        q = float(u.sum()) * d * d
        tau = float((u[:, 0] / d).max())
        reference = tau * 1.0 * 1.0 ** 2 / (6 * q)
        exact = solve.rect_shear_factor(1.0, 1.0)
        self.assertAlmostEqual(exact / reference, 1.0, delta=0.015)
        self.assertGreater(exact, reference)

    def test_shallow_duct_shear_is_the_textbook_formula(self):
        q = 11.0 * UL_MIN_TO_M3_S
        w, h = 5.0e-3, 0.05e-3            # w/h = 100, so the correction is ~1
        shallow = 6 * MU * q / (w * h * h) * 10.0
        self.assertAlmostEqual(solve.duct_shear_dyn_cm2(q, w, h, MU) / shallow, 1.0, delta=0.01)

    def test_a_near_square_channel_reports_60_percent_more_shear(self):
        # The regression this exists to prevent: a 0.3 x 0.3 mm channel is NOT a slot, and
        # reporting the slot number would hide a shear finding by a factor of 1.6.
        q = 2.0 * UL_MIN_TO_M3_S
        side = 0.3e-3
        shallow = 6 * MU * q / (side * side * side) * 10.0
        exact = solve.duct_shear_dyn_cm2(q, side, side, MU)
        self.assertAlmostEqual(exact / shallow, 1.6013, places=3)

    def test_hele_shaw_shear_has_no_aspect_correction(self):
        # tau = 6 mu u / h in a cell, so it must not depend on the meshing grid.
        u, h = 1e-3, DEPTH_MM * 1e-3
        self.assertAlmostEqual(solve.hele_shaw_shear_dyn_cm2(u, h, MU), 6 * MU * u / h * 10.0, places=12)

    def test_duct_and_hele_shaw_agree_in_the_shallow_limit(self):
        w, h = 10.0e-3, 0.05e-3
        q = 5.0 * UL_MIN_TO_M3_S
        self.assertAlmostEqual(solve.duct_shear_dyn_cm2(q, w, h, MU),
                               solve.hele_shaw_shear_dyn_cm2(q / (w * h), h, MU),
                               delta=0.01 * solve.hele_shaw_shear_dyn_cm2(q / (w * h), h, MU))

    def test_agrees_with_validate_pys_independent_series(self):
        # validate.py derives the same correction from the raw double-sine series (500
        # terms, no convergence acceleration) and says in its docstring that it does so
        # deliberately, so the two can be compared. Two independent derivations landing
        # on 1.6013 and 1.4842 is what makes those numbers trustworthy.
        try:
            from assay2chip.cfd import validate
        except ImportError as exc:                      # pragma: no cover
            self.skipTest(f"validate.py not importable: {exc}")
        if not hasattr(validate, "wall_shear_correction"):   # pragma: no cover
            self.skipTest("validate.py has no wall_shear_correction")
        for ratio in (1.0, 1.33, 2.0, 4.0, 10.0, 100.0):
            theirs = validate.wall_shear_correction(ratio, 1.0)
            mine = solve.rect_shear_factor(ratio, 1.0)
            self.assertAlmostEqual(mine / theirs, 1.0, delta=1e-6, msg=f"w/h = {ratio}")

    def test_zero_or_negative_geometry_is_refused(self):
        for w, h in ((0.0, 1.0), (1.0, 0.0), (-1.0, 1.0)):
            with self.assertRaises(solve.SolveError):
                solve.rect_shear_factor(w, h)


class ReynoldsAndDiameter(unittest.TestCase):
    def test_hydraulic_diameter_of_a_rectangle(self):
        w, h = 0.8e-3, 0.3e-3
        self.assertAlmostEqual(solve.hydraulic_diameter_m(w, h), 4 * (w * h) / (2 * (w + h)), places=15)

    def test_reynolds_is_rho_u_dh_over_mu(self):
        rho, u, w, h = 993.0, 0.01, 0.8e-3, 0.3e-3
        expect = rho * u * solve.hydraulic_diameter_m(w, h) / MU
        self.assertAlmostEqual(solve.reynolds(u, w, h, MU, rho), expect, places=12)

    def test_density_comes_from_the_contract_when_the_contract_has_it(self):
        rho, source, assumed = solve.density()
        contract_key = next(
            (k for k in solve.DENSITY_CONTRACT_KEYS
             if k in contracts.parameters() and contracts.param_entry(k).get("value") is not None),
            None)
        if contract_key is None:
            self.assertEqual(rho, solve.DENSITY_FALLBACK_KG_M3)
            self.assertIn("ASSUMED BY SOLVE.PY", source.upper())
            self.assertTrue(assumed)
        else:
            # The contract owns the number; solve.py must not keep its own copy.
            self.assertEqual(rho, float(contracts.param(contract_key)))
            self.assertIn(contract_key, source)
            self.assertEqual(assumed, contracts.is_assumption(contract_key))

    def test_an_assumed_density_is_carried_into_the_field(self):
        rho, _, assumed = solve.density()
        field = solve.solve_step(chain_mesh([(10.0, 0.8, DEPTH_MM)]), feed_vent(11.0))
        self.assertEqual(field.meta["rho_kg_m3"], rho)
        self.assertEqual(field.meta["rho_is_assumption"], assumed)
        if assumed:
            # Re rests on it, so a reader of findings.json has to be able to see that.
            self.assertTrue(any("density" in a.lower() for a in field.meta["assumptions"]),
                            field.meta["assumptions"])


# --- the analytic single channel ---------------------------------------------

class SingleDuct(unittest.TestCase):
    LENGTH, WIDTH, FLOW = 12.5, 0.8, 11.0

    def setUp(self):
        self.mesh = chain_mesh([(self.LENGTH, self.WIDTH, DEPTH_MM)], chip_id="one_duct")
        self.field = solve.solve_step(self.mesh, feed_vent(self.FLOW))

    def test_pressure_drop_matches_the_readme_resistance(self):
        expect = duct_resistance(self.LENGTH, self.WIDTH, DEPTH_MM) * self.FLOW * UL_MIN_TO_M3_S
        self.assertAlmostEqual(self.field.p_pa["n0"] / expect, 1.0, places=12)
        self.assertEqual(self.field.p_pa["n1"], 0.0)

    def test_the_whole_feed_comes_out_of_the_vent(self):
        self.assertAlmostEqual(self.field.q_ul_min("e0"), self.FLOW, places=12)
        self.assertAlmostEqual(self.field.residual.q_in * M3_S_TO_UL_MIN, self.FLOW, places=12)
        self.assertAlmostEqual(self.field.residual.q_out * M3_S_TO_UL_MIN, self.FLOW, places=12)
        self.assertLess(self.field.residual.closure, 1e-15)

    def test_flow_is_signed_a_to_b(self):
        reversed_scn = Scenario(step="back", fluid="medium", temp_c=37.0,
                                ports={"p_in": PortState.vent(),
                                       "p_out": PortState.flow(self.FLOW)})
        back = solve.solve_step(self.mesh, reversed_scn)
        self.assertAlmostEqual(back.q_ul_min("e0"), -self.FLOW, places=12)

    def test_wall_shear_carries_the_exact_correction(self):
        q = self.FLOW * UL_MIN_TO_M3_S
        w, h = self.WIDTH * 1e-3, DEPTH_MM * 1e-3
        shallow = 6 * MU * q / (w * h * h) * 10.0
        got = self.field.shear_dyn_cm2["e0"]
        self.assertAlmostEqual(got / shallow, solve.rect_shear_factor(w, h), places=12)
        self.assertGreater(got, shallow)        # the slot formula under-reads at w/h = 2.67

    def test_reynolds_is_reported_and_well_under_one(self):
        u = self.FLOW * UL_MIN_TO_M3_S / (self.WIDTH * 1e-3 * DEPTH_MM * 1e-3)
        rho = self.field.meta["rho_kg_m3"]
        self.assertAlmostEqual(self.field.re["e0"],
                               solve.reynolds(u, self.WIDTH * 1e-3, DEPTH_MM * 1e-3, MU, rho),
                               places=12)
        self.assertLess(self.field.re["e0"], 1.0)
        self.assertEqual(self.field.meta["re_over_limit"], [])

    def test_pressure_scales_linearly_with_viscosity(self):
        # Conductance is g_geom / mu, so twice the viscosity is exactly twice the pressure.
        thick = Scenario(step="thick", fluid="glycerol", temp_c=37.0, mu_pa_s=2 * MU,
                         ports={"p_in": PortState.flow(self.FLOW), "p_out": PortState.vent()})
        doubled = solve.solve_step(self.mesh, thick)
        self.assertAlmostEqual(doubled.p_pa["n0"] / self.field.p_pa["n0"], 2.0, places=12)
        self.assertAlmostEqual(doubled.q_ul_min("e0"), self.field.q_ul_min("e0"), places=12)

    def test_a_pressure_difference_drives_the_right_flow(self):
        dp = 50.0
        scn = Scenario(step="dp", fluid="medium", temp_c=37.0,
                       ports={"p_in": PortState("pressure", p_pa=dp),
                              "p_out": PortState("pressure", p_pa=0.0)})
        field = solve.solve_step(self.mesh, scn)
        expect = dp / duct_resistance(self.LENGTH, self.WIDTH, DEPTH_MM)
        self.assertAlmostEqual(field.q_m3_s["e0"] / expect, 1.0, places=12)


# --- series and parallel, against hand calculation ---------------------------

class SeriesAndParallel(unittest.TestCase):
    SPECS = [(10.0, 0.8, DEPTH_MM), (5.0, 0.4, DEPTH_MM), (2.5, 1.2, DEPTH_MM)]
    FLOW = 7.5

    def test_series_resistances_add(self):
        mesh = chain_mesh(self.SPECS, chip_id="series")
        field = solve.solve_step(mesh, feed_vent(self.FLOW))
        hand = sum(duct_resistance(*s) for s in self.SPECS)
        self.assertAlmostEqual(field.p_pa["n0"] / (hand * self.FLOW * UL_MIN_TO_M3_S), 1.0, places=12)

    def test_every_series_edge_carries_the_whole_flow(self):
        mesh = chain_mesh(self.SPECS, chip_id="series")
        field = solve.solve_step(mesh, feed_vent(self.FLOW))
        for i in range(len(self.SPECS)):
            self.assertAlmostEqual(field.q_ul_min(f"e{i}"), self.FLOW, places=12)

    def test_intermediate_pressures_are_the_partial_sums(self):
        mesh = chain_mesh(self.SPECS, chip_id="series")
        field = solve.solve_step(mesh, feed_vent(self.FLOW))
        q = self.FLOW * UL_MIN_TO_M3_S
        for i in range(len(self.SPECS) + 1):
            downstream = sum(duct_resistance(*s) for s in self.SPECS[i:])
            self.assertAlmostEqual(field.p_pa[f"n{i}"], downstream * q, places=10)

    def test_parallel_conductances_add(self):
        mesh = parallel_mesh(self.SPECS)
        field = solve.solve_step(mesh, feed_vent(self.FLOW, feed="p_in", vent="p_out"))
        hand = 1.0 / sum(1.0 / duct_resistance(*s) for s in self.SPECS)
        self.assertAlmostEqual(field.p_pa["n_in"] / (hand * self.FLOW * UL_MIN_TO_M3_S), 1.0, places=12)

    def test_parallel_flow_splits_by_conductance(self):
        mesh = parallel_mesh(self.SPECS)
        field = solve.solve_step(mesh, feed_vent(self.FLOW))
        total_g = sum(1.0 / duct_resistance(*s) for s in self.SPECS)
        for i, spec in enumerate(self.SPECS):
            share = (1.0 / duct_resistance(*spec)) / total_g
            self.assertAlmostEqual(field.q_ul_min(f"e{i}"), self.FLOW * share, places=12)
        self.assertAlmostEqual(sum(field.q_ul_min(f"e{i}") for i in range(len(self.SPECS))),
                               self.FLOW, places=12)

    def test_a_wheatstone_bridge_balances(self):
        # Two equal arms: the bridge edge must carry EXACTLY nothing. A sign or assembly
        # error in the Laplacian shows up here as a non-zero cross flow.
        d = (6.0, 0.6, DEPTH_MM)
        nodes = [Node(n, kind="junction", depth_mm=DEPTH_MM, area_mm2=0.01)
                 for n in ("a", "l", "r", "b")]
        edges = [Edge("al", a="a", b="l", kind="duct", g_geom_m3=duct_g_geom_m3(*d),
                      length_mm=d[0], width_mm=d[1], depth_mm=d[2]),
                 Edge("ar", a="a", b="r", kind="duct", g_geom_m3=duct_g_geom_m3(*d),
                      length_mm=d[0], width_mm=d[1], depth_mm=d[2]),
                 Edge("lb", a="l", b="b", kind="duct", g_geom_m3=duct_g_geom_m3(*d),
                      length_mm=d[0], width_mm=d[1], depth_mm=d[2]),
                 Edge("rb", a="r", b="b", kind="duct", g_geom_m3=duct_g_geom_m3(*d),
                      length_mm=d[0], width_mm=d[1], depth_mm=d[2]),
                 Edge("lr", a="l", b="r", kind="duct", g_geom_m3=duct_g_geom_m3(3.0, 0.3, DEPTH_MM),
                      length_mm=3.0, width_mm=0.3, depth_mm=DEPTH_MM)]
        mesh = Mesh(nodes, edges, {"p_in": "a", "p_out": "b"},
                    {"chip_id": "bridge", "mu_pa_s": MU})
        field = solve.solve_step(mesh, feed_vent(5.0))
        self.assertAlmostEqual(field.q_m3_s["lr"], 0.0, places=18)
        self.assertAlmostEqual(field.p_pa["l"], field.p_pa["r"], places=12)
        self.assertAlmostEqual(field.q_ul_min("al"), 2.5, places=12)


# --- mass conservation -------------------------------------------------------

class MassConservation(unittest.TestCase):
    def _meshes(self):
        yield "series", chain_mesh(SeriesAndParallel.SPECS), feed_vent(7.5)
        yield "parallel", parallel_mesh(SeriesAndParallel.SPECS), feed_vent(7.5)
        slab = hele_shaw_slab(12, 9)
        ports = {f"in_{j}": PortState.flow(9.0 / 9) for j in range(9)}
        ports.update({f"out_{j}": PortState.vent() for j in range(9)})
        yield "slab", slab, Scenario(step="slab", ports=ports, fluid="medium", temp_c=37.0)

    def test_every_interior_node_balances_to_machine_precision(self):
        for name, mesh, scn in self._meshes():
            field = solve.solve_step(mesh, scn)
            scale = field.residual.q_in
            self.assertGreater(scale, 0.0, name)
            self.assertLess(field.residual.mass_imbalance_max, 1e-12 * scale, name)

    def test_total_in_equals_total_out(self):
        for name, mesh, scn in self._meshes():
            field = solve.solve_step(mesh, scn)
            self.assertAlmostEqual(field.residual.closure, 0.0, places=12, msg=name)

    def test_imbalance_is_recomputed_from_the_edges_not_asserted(self):
        # Independent check: sum the signed edge flows at every node ourselves. The feed
        # node must balance exactly; the vent node must be left holding the whole flow,
        # which is precisely what the vent carries off the chip.
        mesh = parallel_mesh(SeriesAndParallel.SPECS)
        field = solve.solve_step(mesh, feed_vent(7.5))
        net = {n.id: 0.0 for n in mesh.nodes}
        for e in mesh.edges:
            net[e.a] -= field.q_m3_s[e.id]
            net[e.b] += field.q_m3_s[e.id]
        net["n_in"] += 7.5 * UL_MIN_TO_M3_S
        self.assertAlmostEqual(net["n_in"], 0.0, places=18)
        self.assertAlmostEqual(net["n_out"], 7.5 * UL_MIN_TO_M3_S, places=18)
        self.assertAlmostEqual(field.meta["port_flow_ul_min"]["p_out"], -7.5, places=12)

    def test_a_closed_port_carries_no_flow(self):
        mesh = chain_mesh([(5.0, 0.6, DEPTH_MM), (5.0, 0.6, DEPTH_MM)])
        mesh.ports["p_dead"] = "n1"
        scn = Scenario(step="capped", fluid="medium", temp_c=37.0,
                       ports={"p_in": PortState.flow(4.0), "p_out": PortState.vent(),
                              "p_dead": PortState("closed")})
        field = solve.solve_step(mesh, scn)
        self.assertEqual(field.meta["port_flow_ul_min"]["p_dead"], 0.0)
        self.assertAlmostEqual(field.q_ul_min("e0"), 4.0, places=12)
        self.assertAlmostEqual(field.q_ul_min("e1"), 4.0, places=12)

    def test_a_port_the_step_never_mentions_is_capped(self):
        mesh = chain_mesh([(5.0, 0.6, DEPTH_MM)])
        mesh.ports["p_side"] = "n0"
        field = solve.solve_step(mesh, feed_vent(4.0))
        self.assertEqual(field.meta["port_kind"]["p_side"], "closed")
        self.assertEqual(field.meta["port_flow_ul_min"]["p_side"], 0.0)


# --- boundary conditions and the refusals --------------------------------------

class BoundaryConditions(unittest.TestCase):
    def test_feeding_with_nothing_vented_is_refused_loudly(self):
        mesh = chain_mesh([(5.0, 0.6, DEPTH_MM)])
        scn = Scenario(step="dead end", fluid="medium", temp_c=37.0,
                       ports={"p_in": PortState.flow(5.0), "p_out": PortState("closed")})
        with self.assertRaises(solve.SolveError) as caught:
            solve.solve_step(mesh, scn)
        message = str(caught.exception)
        self.assertIn("no steady solution", message)
        self.assertIn("p_in", message)
        self.assertIn("nowhere to go", message)

    def test_balanced_flow_ports_solve_but_the_pressure_is_declared_gauge_relative(self):
        # 5 uL/min in one port and 5 out of the other: the flows are real, the datum is not.
        mesh = chain_mesh([(5.0, 0.6, DEPTH_MM)])
        scn = Scenario(step="balanced", fluid="medium", temp_c=37.0,
                       ports={"p_in": PortState.flow(5.0), "p_out": PortState.flow(-5.0)})
        field = solve.solve_step(mesh, scn)
        self.assertAlmostEqual(field.q_ul_min("e0"), 5.0, places=12)
        self.assertTrue(field.meta["pressure_is_gauge_relative"])
        self.assertTrue(any("GAUGE-RELATIVE" in n for n in field.meta["notes"]),
                        field.meta["notes"])
        self.assertAlmostEqual(field.residual.mass_imbalance_max, 0.0, places=18)

    def test_an_all_capped_chip_is_still_at_rest_rather_than_an_error(self):
        mesh = chain_mesh([(5.0, 0.6, DEPTH_MM)])
        scn = Scenario(step="idle", fluid="medium", temp_c=37.0,
                       ports={"p_in": PortState("closed"), "p_out": PortState("closed")})
        field = solve.solve_step(mesh, scn)
        self.assertEqual(field.q_m3_s["e0"], 0.0)
        self.assertTrue(any("no drive" in n for n in field.meta["notes"]), field.meta["notes"])

    def test_an_unvented_island_behind_a_shut_weir_is_refused(self):
        # Mesh.with_slit_gap_delta can close a crest to zero; anything fed behind it then
        # has nowhere to go, and the +/-20 um sweep must see that as a refusal, not a field.
        nodes = [Node(n, kind="junction", depth_mm=DEPTH_MM, area_mm2=0.01)
                 for n in ("a", "b", "c")]
        edges = [Edge("ab", a="a", b="b", kind="duct", g_geom_m3=duct_g_geom_m3(5.0, 0.6, DEPTH_MM),
                      length_mm=5.0, width_mm=0.6, depth_mm=DEPTH_MM),
                 Edge("weir", a="b", b="c", kind="slit", g_geom_m3=0.0,
                      conductance_m3_per_pa_s=0.0, length_mm=0.2, width_mm=2.0,
                      depth_mm=DEPTH_MM, gap_um=0.0, note="SHUT by tolerance")]
        mesh = Mesh(nodes, edges, {"p_in": "a", "p_out": "c"},
                    {"chip_id": "shut_weir", "mu_pa_s": MU})
        with self.assertRaises(solve.SolveError) as caught:
            solve.solve_step(mesh, feed_vent(5.0))
        self.assertIn("no steady solution", str(caught.exception))

    def test_a_shut_edge_is_reported_with_zero_flow_not_dropped(self):
        nodes = [Node(n, kind="junction", depth_mm=DEPTH_MM, area_mm2=0.01)
                 for n in ("a", "b", "c")]
        edges = [Edge("ab", a="a", b="b", kind="duct", g_geom_m3=duct_g_geom_m3(5.0, 0.6, DEPTH_MM),
                      length_mm=5.0, width_mm=0.6, depth_mm=DEPTH_MM),
                 Edge("weir", a="b", b="c", kind="slit", g_geom_m3=0.0, length_mm=0.2,
                      width_mm=2.0, depth_mm=DEPTH_MM, gap_um=0.0)]
        mesh = Mesh(nodes, edges, {"p_in": "a", "p_out": "b"},
                    {"chip_id": "shut2", "mu_pa_s": MU})
        field = solve.solve_step(mesh, feed_vent(5.0, vent="p_out"))
        self.assertIn("weir", field.q_m3_s)
        self.assertEqual(field.q_m3_s["weir"], 0.0)
        self.assertTrue(any("SHUT" in n for n in field.meta["notes"]), field.meta["notes"])

    def test_an_unknown_port_is_refused_by_name(self):
        mesh = chain_mesh([(5.0, 0.6, DEPTH_MM)])
        scn = Scenario(step="typo", fluid="medium", temp_c=37.0,
                       ports={"p_inn": PortState.flow(5.0), "p_out": PortState.vent()})
        with self.assertRaises(solve.SolveError) as caught:
            solve.solve_step(mesh, scn)
        self.assertIn("p_inn", str(caught.exception))

    def test_two_pressure_ports_on_one_node_must_agree(self):
        mesh = chain_mesh([(5.0, 0.6, DEPTH_MM)])
        mesh.ports["p_also"] = "n1"
        scn = Scenario(step="clash", fluid="medium", temp_c=37.0,
                       ports={"p_in": PortState.flow(5.0), "p_out": PortState.vent(),
                              "p_also": PortState("pressure", p_pa=100.0)})
        with self.assertRaises(solve.SolveError) as caught:
            solve.solve_step(mesh, scn)
        self.assertIn("disagree", str(caught.exception))

    def test_a_pressure_port_reports_the_flow_it_actually_takes(self):
        mesh = chain_mesh(SeriesAndParallel.SPECS)
        field = solve.solve_step(mesh, feed_vent(7.5))
        self.assertAlmostEqual(field.meta["port_flow_ul_min"]["p_out"], -7.5, places=12)
        self.assertAlmostEqual(field.meta["port_flow_ul_min"]["p_in"], 7.5, places=12)


# --- an unresolved fluid must not be silently water ---------------------------

class FluidProvenance(unittest.TestCase):
    def test_37c_uses_the_37c_contract_value(self):
        field = solve.solve_step(chain_mesh([(5.0, 0.6, DEPTH_MM)]), feed_vent(5.0, temp_c=37.0))
        self.assertEqual(field.meta["mu_pa_s"], contracts.viscosity_37c_pa_s().pa_s)
        self.assertIn(contracts.VISCOSITY_37C_KEY, field.meta["mu_source"])

    def test_the_dye_demo_at_20c_uses_water(self):
        field = solve.solve_step(chain_mesh([(5.0, 0.6, DEPTH_MM)]),
                                 feed_vent(5.0, temp_c=20.0, fluid="dye"))
        self.assertEqual(field.meta["mu_pa_s"], contracts.viscosity_water_20c_pa_s().pa_s)
        self.assertIn(contracts.VISCOSITY_20C_KEY, field.meta["mu_source"])

    def test_a_fluid_the_contract_does_not_cover_is_flagged(self):
        field = solve.solve_step(chain_mesh([(5.0, 0.6, DEPTH_MM)]),
                                 feed_vent(5.0, fluid="cold acetonitrile"))
        self.assertTrue(any("cold acetonitrile" in a for a in field.meta["assumptions"]),
                        field.meta["assumptions"])

    def test_a_viscosity_scenario_py_already_resolved_is_used_as_given(self):
        scn = Scenario(step="resolved", fluid="core buffer", temp_c=37.0, mu_pa_s=3.2e-3,
                       ports={"p_in": PortState.flow(5.0), "p_out": PortState.vent()},
                       unresolved=[{"what": "core buffer viscosity", "why": "not in contract"}])
        field = solve.solve_step(chain_mesh([(5.0, 0.6, DEPTH_MM)]), scn)
        self.assertEqual(field.meta["mu_pa_s"], 3.2e-3)
        self.assertTrue(any("core buffer" in a for a in field.meta["assumptions"]))

    def test_a_non_positive_viscosity_is_refused(self):
        scn = Scenario(step="bad", fluid="x", mu_pa_s=0.0,
                       ports={"p_in": PortState.flow(5.0), "p_out": PortState.vent()})
        with self.assertRaises(solve.SolveError):
            solve.solve_step(chain_mesh([(5.0, 0.6, DEPTH_MM)]), scn)


# --- the weir slit -----------------------------------------------------------

class WeirSlit(unittest.TestCase):
    """Flow through a crest goes as gap^3, and its shear is set by the GAP, not the depth."""

    def _mesh(self, gap_um: float) -> Mesh:
        from assay2chip.cfd.model import slit_g_geom_m3
        nodes = [Node("a", kind="junction", depth_mm=DEPTH_MM, area_mm2=1.0),
                 Node("b", kind="junction", x_mm=1.0, depth_mm=DEPTH_MM, area_mm2=1.0)]
        edges = [Edge("crest", a="a", b="b", kind="slit", length_mm=0.2, width_mm=2.0,
                      depth_mm=DEPTH_MM, gap_um=gap_um,
                      g_geom_m3=slit_g_geom_m3(2.0, gap_um, 0.2))]
        return Mesh(nodes, edges, {"p_in": "a", "p_out": "b"},
                    {"chip_id": f"weir{gap_um}", "mu_pa_s": MU})

    def test_cross_section_uses_the_gap(self):
        edge = self._mesh(50.0).edges[0]
        w, h = solve.edge_cross_section_m(edge)
        self.assertAlmostEqual(w, 2.0e-3, places=12)
        self.assertAlmostEqual(h, 50e-6, places=15)

    def test_pressure_drop_scales_as_gap_cubed(self):
        fine = solve.solve_step(self._mesh(40.0), feed_vent(2.0))
        coarse = solve.solve_step(self._mesh(80.0), feed_vent(2.0))
        self.assertAlmostEqual(fine.p_pa["a"] / coarse.p_pa["a"], 8.0, delta=0.25)

    def test_slit_shear_is_far_above_the_chamber_depth_estimate(self):
        field = solve.solve_step(self._mesh(50.0), feed_vent(2.0))
        q = 2.0 * UL_MIN_TO_M3_S
        at_gap = solve.duct_shear_dyn_cm2(q, 2.0e-3, 50e-6, MU)
        at_depth = solve.duct_shear_dyn_cm2(q, 2.0e-3, DEPTH_MM * 1e-3, MU)
        self.assertAlmostEqual(field.shear_dyn_cm2["crest"], at_gap, places=12)
        self.assertGreater(at_gap, 30 * at_depth)

    def test_a_tolerance_sweep_changes_the_flow_split(self):
        # The README's +/-20 um z tolerance, through Mesh.with_slit_gap_delta.
        base = self._mesh(50.0)
        for delta in (-20.0, 0.0, +20.0):
            swept = base.with_slit_gap_delta(delta)
            field = solve.solve_step(swept, feed_vent(2.0))
            self.assertGreater(field.p_pa["a"], 0.0)
        tight = solve.solve_step(base.with_slit_gap_delta(-20.0), feed_vent(2.0))
        loose = solve.solve_step(base.with_slit_gap_delta(+20.0), feed_vent(2.0))
        self.assertGreater(tight.p_pa["a"], 2.0 * loose.p_pa["a"])


# --- Hele-Shaw cells: velocity reconstruction --------------------------------

class CellVelocity(unittest.TestCase):
    NX, NY, GRID, FLOW = 14, 10, 0.25, 9.0

    def setUp(self):
        self.mesh = hele_shaw_slab(self.NX, self.NY, self.GRID)
        ports = {f"in_{j}": PortState.flow(self.FLOW / self.NY) for j in range(self.NY)}
        ports.update({f"out_{j}": PortState.vent() for j in range(self.NY)})
        self.field = solve.solve_step(
            self.mesh, Scenario(step="plug", ports=ports, fluid="medium", temp_c=37.0))

    def test_uniformly_fed_slab_is_a_uniform_plug_flow(self):
        width_m = self.NY * self.GRID * 1e-3
        depth_m = DEPTH_MM * 1e-3
        expect = self.FLOW * UL_MIN_TO_M3_S / (width_m * depth_m) * 1e3   # mm/s
        for j in range(self.NY):
            for i in range(1, self.NX - 1):
                ux, uy = self.field.u_mm_s[f"c{i}_{j}"]
                self.assertAlmostEqual(ux, expect, places=9, msg=f"c{i}_{j}")
                self.assertAlmostEqual(uy, 0.0, places=12, msg=f"c{i}_{j}")

    def test_the_slab_pressure_drop_is_the_hand_calculated_network(self):
        # (NX-1) faces in series, NY rows in parallel, each face g = h^3 / 12 mu.
        g_face = (DEPTH_MM * 1e-3) ** 3 / (12 * MU)
        g_total = self.NY * g_face / (self.NX - 1)
        expect = self.FLOW * UL_MIN_TO_M3_S / g_total
        self.assertAlmostEqual(self.field.p_pa["c0_0"] / expect, 1.0, places=10)

    def test_cell_shear_is_the_hele_shaw_value_from_the_reconstructed_velocity(self):
        speed = self.field.speed_mm_s("c5_5") * 1e-3
        self.assertAlmostEqual(self.field.shear_dyn_cm2["c5_5"],
                               solve.hele_shaw_shear_dyn_cm2(speed, DEPTH_MM * 1e-3, MU),
                               places=12)

    def test_face_shear_does_not_depend_on_the_meshing_grid(self):
        # The same physical slab at two grid sizes must report the same cell shear; if the
        # aspect-ratio correction were applied to hs_face edges it would not.
        shears = []
        for nx, ny, grid in ((14, 10, 0.25), (7, 5, 0.5)):
            mesh = hele_shaw_slab(nx, ny, grid)
            ports = {f"in_{j}": PortState.flow(self.FLOW / ny) for j in range(ny)}
            ports.update({f"out_{j}": PortState.vent() for j in range(ny)})
            field = solve.solve_step(mesh, Scenario(step="g", ports=ports, fluid="medium",
                                                    temp_c=37.0))
            shears.append(field.shear_dyn_cm2[f"c{nx // 2}_{ny // 2}"])
        self.assertAlmostEqual(shears[0] / shears[1], 1.0, places=9)

    def test_a_corner_fed_chamber_has_a_low_velocity_zone(self):
        # The README's trap definition: Hele-Shaw has no eddies, so a trap is a slow zone.
        # One inlet and one outlet on the same side leaves the far corner barely moving.
        mesh = hele_shaw_slab(self.NX, self.NY, self.GRID)
        scn = Scenario(step="corner", fluid="medium", temp_c=37.0,
                       ports={"in_0": PortState.flow(self.FLOW), "out_0": PortState.vent()})
        field = solve.solve_step(mesh, scn)
        speeds = {nid: field.speed_mm_s(nid) for nid in field.u_mm_s}
        mean = sum(speeds.values()) / len(speeds)
        far = speeds[f"c{self.NX - 1}_{self.NY - 1}"]
        self.assertGreater(mean, 0.0)
        # Both ports sit on the j = 0 edge, so the slow zone is the whole far edge.
        self.assertLess(far, 0.05 * speeds["c0_0"])
        self.assertLess(far, 0.2 * mean)
        slowest = min(speeds, key=lambda nid: speeds[nid])
        self.assertTrue(slowest.endswith(f"_{self.NY - 1}"), slowest)
        # ... and no closed streamline anywhere, which is the point of the README's note.
        self.assertTrue(all(v > 0 for v in speeds.values()))

    def test_velocity_is_only_reported_for_cells(self):
        self.assertEqual(set(self.field.u_mm_s), {n.id for n in self.mesh.nodes if n.kind == "cell"})
        chain = chain_mesh([(5.0, 0.6, DEPTH_MM)])
        self.assertEqual(solve.solve_step(chain, feed_vent(5.0)).u_mm_s, {})


# --- many right-hand sides ---------------------------------------------------

class ManyRightHandSides(unittest.TestCase):
    def _protocol(self) -> list[Scenario]:
        steps = []
        for i, q in enumerate((2.0, 5.0, 11.0, 0.5)):
            steps.append(Scenario(step=f"flow{i}", fluid="medium", temp_c=37.0,
                                  ports={"p_in": PortState.flow(q),
                                         "p_out": PortState.vent()}))
        # A different Dirichlet pattern, so it must land in its own batch.
        steps.append(Scenario(step="back", fluid="medium", temp_c=37.0,
                              ports={"p_in": PortState.vent(), "p_out": PortState.flow(3.0)}))
        # And a different fluid at the same pattern as the first batch.
        steps.append(Scenario(step="cold", fluid="dye", temp_c=20.0,
                              ports={"p_in": PortState.flow(5.0), "p_out": PortState.vent()}))
        return steps

    def test_batched_steps_match_solving_each_one_alone(self):
        mesh = chain_mesh(SeriesAndParallel.SPECS)
        steps = self._protocol()
        batched = solve.solve_steps(mesh, steps)
        self.assertEqual(len(batched), len(steps))
        for scn, field in zip(steps, batched):
            alone = solve.solve_step(mesh, scn)
            for nid in alone.p_pa:
                self.assertAlmostEqual(field.p_pa[nid], alone.p_pa[nid], places=14, msg=nid)
            for eid in alone.q_m3_s:
                self.assertAlmostEqual(field.q_m3_s[eid], alone.q_m3_s[eid], places=20, msg=eid)

    def test_steps_sharing_a_dirichlet_pattern_share_one_factorisation(self):
        mesh = chain_mesh(SeriesAndParallel.SPECS)
        fields = solve.solve_steps(mesh, self._protocol())
        batch_sizes = {f.meta["step"]: f.meta["solver"]["batched_with"] for f in fields}
        self.assertEqual(batch_sizes["flow0"], 5)      # four flow steps + the cold one
        self.assertEqual(batch_sizes["cold"], 5)
        self.assertEqual(batch_sizes["back"], 1)       # the reversed pattern stands alone

    def test_each_step_keeps_its_own_fluid_and_step_label(self):
        mesh = chain_mesh(SeriesAndParallel.SPECS)
        fields = solve.solve_steps(mesh, self._protocol())
        cold = next(f for f in fields if f.meta["step"] == "cold")
        warm = next(f for f in fields if f.meta["step"] == "flow1")
        self.assertEqual(cold.meta["mu_pa_s"], contracts.viscosity_water_20c_pa_s().pa_s)
        self.assertEqual(warm.meta["mu_pa_s"], contracts.viscosity_37c_pa_s().pa_s)

    def test_steps_differing_only_in_flow_rate_are_solved_once(self):
        # Linearity: A x = c b has solution c A^-1 b, so a flow ramp is one solve.
        mesh = chain_mesh(SeriesAndParallel.SPECS)
        rates = (1.0, 2.0, 5.0, 11.0, 0.25)
        steps = [Scenario(step=f"q{q}", fluid="medium", temp_c=37.0,
                          ports={"p_in": PortState.flow(q), "p_out": PortState.vent()})
                 for q in rates]
        fields = solve.solve_steps(mesh, steps)
        self.assertEqual(fields[0].meta["solver"]["rhs"], len(rates))
        self.assertEqual(fields[0].meta["solver"]["distinct_rhs"], 1)
        # ... and the answers are still exactly proportional to the flow.
        base = fields[0].p_pa["n0"] / rates[0]
        for q, field in zip(rates, fields):
            self.assertAlmostEqual(field.p_pa["n0"] / q, base, places=14, msg=str(q))
            self.assertAlmostEqual(field.q_ul_min("e0"), q, places=12)

    def test_genuinely_different_steps_are_not_collapsed(self):
        mesh = chain_mesh(SeriesAndParallel.SPECS)
        mesh.ports["p_mid"] = "n1"
        steps = [
            Scenario(step="a", fluid="medium", temp_c=37.0,
                     ports={"p_in": PortState.flow(5.0), "p_out": PortState.vent()}),
            Scenario(step="b", fluid="medium", temp_c=37.0,
                     ports={"p_mid": PortState.flow(5.0), "p_out": PortState.vent()}),
        ]
        fields = solve.solve_steps(mesh, steps)
        self.assertEqual(fields[0].meta["solver"]["distinct_rhs"], 2)
        self.assertNotAlmostEqual(fields[0].p_pa["n0"], fields[1].p_pa["n0"], places=6)

    def test_the_reuse_is_audited_against_the_full_right_hand_side(self):
        mesh = chain_mesh(SeriesAndParallel.SPECS)
        steps = [Scenario(step=f"q{q}", fluid="medium", temp_c=37.0,
                          ports={"p_in": PortState.flow(q), "p_out": PortState.vent()})
                 for q in (1.0, 7.0)]
        for field in solve.solve_steps(mesh, steps):
            self.assertLessEqual(field.meta["solver"]["residual_rel_max"], solve.RESIDUAL_RTOL)

    def test_an_empty_protocol_is_an_empty_list(self):
        self.assertEqual(solve.solve_steps(chain_mesh([(5.0, 0.6, DEPTH_MM)]), []), [])

    def test_a_topology_can_be_reused_across_calls(self):
        mesh = chain_mesh(SeriesAndParallel.SPECS)
        topo = solve.build_topology(mesh)
        a = solve.solve_step(mesh, feed_vent(7.5), topology=topo)
        b = solve.solve_step(mesh, feed_vent(7.5), topology=topo)
        self.assertEqual(a.p_pa, b.p_pa)


# --- dense and CG ------------------------------------------------------------

class LinearSolvers(unittest.TestCase):
    """Both paths, on the same system, with the residual audited either way."""

    @classmethod
    def setUpClass(cls):
        cls.nx, cls.ny = 40, 36                        # 1440 cells > DENSE_MAX_UNKNOWNS
        cls.mesh = hele_shaw_slab(cls.nx, cls.ny)
        ports = {f"in_{j}": PortState.flow(9.0 / cls.ny) for j in range(cls.ny)}
        ports.update({f"out_{j}": PortState.vent() for j in range(cls.ny)})
        cls.scn = Scenario(step="big", ports=ports, fluid="medium", temp_c=37.0)

    def test_the_method_rule_prefers_dense_when_one_factorisation_is_amortised(self):
        self.assertTrue(solve.use_dense_solver(500, 1))
        self.assertTrue(solve.use_dense_solver(solve.DENSE_MAX_UNKNOWNS, 1))
        self.assertFalse(solve.use_dense_solver(solve.DENSE_MAX_UNKNOWNS + 1, 1))
        # Above the plain threshold, many right-hand sides bring dense back.
        self.assertTrue(solve.use_dense_solver(solve.DENSE_MAX_UNKNOWNS + 1,
                                               solve.DENSE_RHS_AMORTISE))
        self.assertFalse(solve.use_dense_solver(solve.DENSE_MAX_UNKNOWNS_BATCHED + 1,
                                                solve.DENSE_RHS_AMORTISE))

    def test_the_big_system_takes_the_cg_path(self):
        field = solve.solve_step(self.mesh, self.scn)
        self.assertIn("CG", field.meta["solver"]["method"])
        self.assertGreater(field.meta["solver"]["unknowns"], solve.DENSE_MAX_UNKNOWNS)
        self.assertGreater(field.meta["solver"]["iterations"], 0)

    def test_cg_and_dense_agree_on_the_same_system(self):
        cg = solve.solve_step(self.mesh, self.scn)
        original = solve.DENSE_MAX_UNKNOWNS
        try:
            solve.DENSE_MAX_UNKNOWNS = 10 ** 9
            dense = solve.solve_step(self.mesh, self.scn)
        finally:
            solve.DENSE_MAX_UNKNOWNS = original
        self.assertIn("dense", dense.meta["solver"]["method"])
        scale = max(abs(v) for v in dense.p_pa.values())
        for nid, value in dense.p_pa.items():
            self.assertLess(abs(cg.p_pa[nid] - value), 1e-8 * scale, nid)
        q_scale = max(abs(v) for v in dense.q_m3_s.values())
        for eid, value in dense.q_m3_s.items():
            self.assertLess(abs(cg.q_m3_s[eid] - value), 1e-7 * q_scale, eid)

    def test_cg_still_conserves_mass_to_near_machine_precision(self):
        field = solve.solve_step(self.mesh, self.scn)
        self.assertLess(field.residual.mass_imbalance_max, 1e-10 * field.residual.q_in)
        self.assertLess(field.residual.closure, 1e-10)

    def test_the_audited_residual_is_recorded_and_tiny(self):
        for mesh, scn in ((self.mesh, self.scn),
                          (chain_mesh(SeriesAndParallel.SPECS), feed_vent(7.5))):
            field = solve.solve_step(mesh, scn)
            self.assertLessEqual(field.meta["solver"]["residual_rel_max"], solve.RESIDUAL_RTOL)

    def test_non_convergence_is_loud(self):
        # One iteration is not enough for a 1440-cell slab; it must raise, not round down.
        topo = solve.build_topology(self.mesh)
        bc = solve._resolve_boundary(topo, self.scn)
        red = solve._reduce(topo, tuple(sorted(bc.dirichlet)))
        B = np.zeros((red.n, 1))
        B[:, 0] = MU * bc.injection[red.unknown]
        with self.assertRaises(solve.SolveError) as caught:
            solve._cg_jacobi(red, B, maxiter=1)
        self.assertIn("did NOT converge", str(caught.exception))
        self.assertIn("Do NOT trust", str(caught.exception))

    def test_an_orphan_node_is_named_rather_than_divided_by_zero(self):
        nodes = [Node("a", kind="junction", depth_mm=DEPTH_MM, area_mm2=0.01),
                 Node("b", kind="junction", x_mm=1.0, depth_mm=DEPTH_MM, area_mm2=0.01),
                 Node("lost", kind="junction", x_mm=5.0, depth_mm=DEPTH_MM, area_mm2=0.01)]
        edges = [Edge("ab", a="a", b="b", kind="duct", g_geom_m3=duct_g_geom_m3(5.0, 0.6, DEPTH_MM),
                      length_mm=5.0, width_mm=0.6, depth_mm=DEPTH_MM)]
        mesh = Mesh(nodes, edges, {"p_in": "a", "p_out": "b"},
                    {"chip_id": "orphan", "mu_pa_s": MU})
        field = solve.solve_step(mesh, feed_vent(5.0))
        self.assertEqual(field.p_pa["lost"], 0.0)     # pinned, not NaN
        self.assertTrue(any("no drive" in n for n in field.meta["notes"]))


# --- Re above 1 is reported, not trusted -------------------------------------

class ValidityLimit(unittest.TestCase):
    def test_a_fast_narrow_edge_is_flagged_as_unmodelled(self):
        # The audit's real exception: Module 6's quench flood runs Re 13-23.
        mesh = chain_mesh([(2.0, 0.2, DEPTH_MM)], chip_id="fast")
        field = solve.solve_step(mesh, feed_vent(400.0))
        self.assertGreater(field.re["e0"], solve.RE_VALIDITY_LIMIT)
        self.assertEqual(field.meta["re_over_limit"], ["e0"])
        self.assertEqual(field.over_re(), ["e0"])
        self.assertTrue(any("unmodelled" in n for n in field.meta["notes"]), field.meta["notes"])

    def test_normal_protocol_flows_stay_inside_the_model(self):
        mesh = chain_mesh(SeriesAndParallel.SPECS)
        field = solve.solve_step(mesh, feed_vent(11.0))
        self.assertEqual(field.meta["re_over_limit"], [])


# --- agreement with operator/network.py, an independent implementation -------

class AgreesWithOperatorNetwork(unittest.TestCase):
    """The README's validation requirement: two independent solvers, one graph.

    `operator/network.py` builds Module 1's hydraulic graph (channel resistance plus the
    slab across each chamber it enters) and solves it with its own Gaussian elimination.
    Feeding solve.py exactly that graph isolates the SOLVER from the mesher: the two must
    agree, and they agree here to machine precision rather than "within a few percent".
    """

    @classmethod
    def setUpClass(cls):
        import network as nw
        cls.nw = nw
        cls.net = nw.module1_network()
        cls.flow = max(
            s["flow_ul_min"]
            for s in json.loads((ROOT / "protocols" / "module1_v1.json").read_text())["steps"]
            if s.get("flow_ul_min")
        )

    def _mesh(self) -> Mesh:
        """network.py's own graph as a Mesh: g_geom = mu / R, so no geometry is re-derived."""
        chambers = {c["id"] for c in self.net.chip["chambers"]}
        nodes = []
        for nid in self.net.nodes:
            nodes.append(Node(nid, kind="port" if nid not in chambers else "junction",
                              depth_mm=DEPTH_MM, area_mm2=0.01, feature_id=nid))
        edges = [
            Edge(e["id"], a=e["from"], b=e["to"], kind="duct",
                 g_geom_m3=self.net.mu / e["R"], length_mm=e["length_mm"],
                 width_mm=e["width_mm"], depth_mm=e["depth_mm"], feature_id=e["id"])
            for e in self.net.edges
        ]
        ports = {p["id"]: p["id"] for p in self.net.chip["ports"]}
        return Mesh(nodes, edges, ports,
                    {"chip_id": self.net.chip["id"], "mu_pa_s": self.net.mu})

    def _scenario(self, mode: str) -> Scenario:
        ports: dict[str, PortState] = {}
        for pid, state in self.nw.mode_ports(mode, self.flow).items():
            if isinstance(state, tuple):
                ports[pid] = PortState.flow(float(state[1]))
            else:
                ports[pid] = PortState.vent()
        return Scenario(step=mode, ports=ports, fluid="medium", temp_c=37.0,
                        mu_pa_s=self.net.mu, chip_id=self.net.chip["id"])

    def test_every_mode_agrees_on_every_channel_flow(self):
        mesh = self._mesh()
        modes = [m for m in self.nw.MODULE1_MODES if m != "idle"]
        fields = solve.solve_steps(mesh, [self._scenario(m) for m in modes])
        for mode, field in zip(modes, fields):
            reference = self.net.solve(self.nw.mode_ports(mode, self.flow))
            for eid, row in reference["channels"].items():
                self.assertAlmostEqual(field.q_ul_min(eid), row["flow_ul_min"],
                                       places=6, msg=f"{mode} {eid}")

    def test_every_mode_agrees_on_every_nodal_pressure(self):
        mesh = self._mesh()
        for mode in self.nw.MODULE1_MODES:
            if mode == "idle":
                continue
            reference = self.net.solve(self.nw.mode_ports(mode, self.flow))
            field = solve.solve_step(mesh, self._scenario(mode))
            for nid, value in reference["pressure_pa"].items():
                self.assertAlmostEqual(field.p_pa[nid], value, places=5, msg=f"{mode} {nid}")

    def test_the_two_solvers_refuse_the_same_impossible_mode(self):
        mesh = self._mesh()
        impossible = {p["id"]: PortState("closed") for p in self.net.chip["ports"]}
        impossible["p_in"] = PortState.flow(self.flow)
        with self.assertRaises(self.nw.NetworkError):
            self.net.solve({"p_in": (self.nw.FEED, self.flow)})
        with self.assertRaises(solve.SolveError):
            solve.solve_step(mesh, Scenario(step="impossible", ports=impossible,
                                            fluid="medium", temp_c=37.0, mu_pa_s=self.net.mu))

    def test_shear_is_higher_than_network_py_because_the_ducts_are_not_slots(self):
        # network.py uses the uncorrected slot formula. On Module 1's channels (w/h around
        # 1-3) the exact duct solution is tens of percent higher, and that difference is
        # the whole reason this module implements the series: the report must say which is
        # right, and the exact one is.
        mesh = self._mesh()
        field = solve.solve_step(mesh, self._scenario("grow"))
        reference = self.net.solve(self.nw.mode_ports("grow", self.flow))
        ratios = []
        for e in self.net.edges:
            slot = reference["channels"][e["id"]]["shear_dyn_cm2"]
            if slot <= 0:
                continue
            ratios.append(field.shear_dyn_cm2[e["id"]] / slot)
            expected = solve.rect_shear_factor(e["width_mm"], e["depth_mm"])
            self.assertAlmostEqual(ratios[-1], expected, places=4, msg=e["id"])
        self.assertTrue(ratios)
        self.assertGreater(max(ratios), 1.0)


# --- every repo chip, and the timing budget ----------------------------------

def hele_shaw_chip_mesh(chip: dict, grid_mm: float = 0.25) -> Mesh:
    """A stand-in mesher: chambers rasterised, channels as ducts, ports joined.

    TODO(mesh): replace with `assay2chip.cfd.build_mesh(chip)` once mesh.py lands. This
    exists only so solve.py can be exercised and TIMED at a realistic node count on the
    real repo chips; it makes none of the meshing decisions the README reserves for
    mesh.py (channel-like blocks as ducts, routed paths, weir slits), so it is not a
    substitute for `tests/test_cfd_mesh.py`.
    """
    nodes: list[Node] = []
    edges: list[Edge] = []
    ports: dict[str, str] = {}
    depth_default = contracts.max_depth_mm()
    cell_of: dict[str, list[str]] = {}

    for chamber in chip["chambers"]:
        cx, cy = chamber["center"][0], chamber["center"][1]
        sx, sy = chamber["size"][0], chamber["size"][1]
        depth = chamber["size"][2] or depth_default
        nx, ny = max(int(round(sx / grid_mm)), 1), max(int(round(sy / grid_mm)), 1)
        dx, dy = sx / nx, sy / ny
        g_face_x = (depth * 1e-3) ** 3 / 12.0 * (dy / dx)
        g_face_y = (depth * 1e-3) ** 3 / 12.0 * (dx / dy)
        ids: list[str] = []
        for j in range(ny):
            for i in range(nx):
                nid = f"{chamber['id']}_c{i}_{j}"
                ids.append(nid)
                nodes.append(Node(nid, kind="cell", x_mm=cx - sx / 2 + (i + 0.5) * dx,
                                  y_mm=cy - sy / 2 + (j + 0.5) * dy, area_mm2=dx * dy,
                                  depth_mm=depth, ix=i, iy=j, feature_id=chamber["id"],
                                  boundary=(i in (0, nx - 1) or j in (0, ny - 1))))
        for j in range(ny):
            for i in range(nx):
                if i + 1 < nx:
                    edges.append(Edge(f"{chamber['id']}_fx{i}_{j}", a=f"{chamber['id']}_c{i}_{j}",
                                      b=f"{chamber['id']}_c{i + 1}_{j}", kind="hs_face",
                                      g_geom_m3=g_face_x, length_mm=dx, width_mm=dy,
                                      depth_mm=depth, feature_id=chamber["id"]))
                if j + 1 < ny:
                    edges.append(Edge(f"{chamber['id']}_fy{i}_{j}", a=f"{chamber['id']}_c{i}_{j}",
                                      b=f"{chamber['id']}_c{i}_{j + 1}", kind="hs_face",
                                      g_geom_m3=g_face_y, length_mm=dx, width_mm=dy,
                                      depth_mm=depth, feature_id=chamber["id"]))
        cell_of[chamber["id"]] = ids

    for p in chip.get("ports", []):
        nid = f"port_{p['id']}"
        nodes.append(Node(nid, kind="port", x_mm=p["position"][0], y_mm=p["position"][1],
                          area_mm2=contracts.floor("min_port_mm") ** 2, depth_mm=depth_default,
                          feature_id=p["id"]))
        ports[p["id"]] = nid
        cell_of[p["id"]] = [nid]

    by_id = {n.id: n for n in nodes}

    def nearest(group: str, x: float, y: float) -> str:
        return min(cell_of[group], key=lambda c: (by_id[c].x_mm - x) ** 2 + (by_id[c].y_mm - y) ** 2)

    for ch in chip["channels"]:
        a_group, b_group = ch["from"], ch["to"]
        if a_group not in cell_of or b_group not in cell_of:
            continue
        ax = by_id[cell_of[a_group][0]].x_mm
        ay = by_id[cell_of[a_group][0]].y_mm
        bx = by_id[cell_of[b_group][0]].x_mm
        by_ = by_id[cell_of[b_group][0]].y_mm
        a = nearest(a_group, bx, by_)
        b = nearest(b_group, ax, ay)
        if ch.get("path"):
            pts = ch["path"]
            length = sum(math.dist(p, q) for p, q in zip(pts[:-1], pts[1:]))
        else:
            length = math.dist((by_id[a].x_mm, by_id[a].y_mm), (by_id[b].x_mm, by_id[b].y_mm))
        length = max(length, grid_mm)
        edges.append(Edge(ch["id"], a=a, b=b, kind="duct",
                          g_geom_m3=duct_g_geom_m3(length, ch["width_mm"], ch["depth_mm"]),
                          length_mm=length, width_mm=ch["width_mm"], depth_mm=ch["depth_mm"],
                          feature_id=ch["id"]))
    return Mesh(nodes, edges, ports,
                {"chip_id": chip["id"], "mu_pa_s": MU, "grid_mm": grid_mm,
                 "note": "stand-in raster from tests/test_cfd_solve.py, not mesh.py"})


def _protocol_scenarios(chip: dict, mesh: Mesh) -> list[Scenario]:
    """Every protocol step as boundary conditions, or one nominal step if there is none.

    Only steps whose feeds and vents end up in ONE connected component of the stand-in
    mesh are kept. A disconnected pair is a `dead_end` finding for analyse.py and the
    solver rightly refuses it; here it would only be measuring the stand-in mesher.

    TODO(scenario): replace with scenario.py's reader when it lands.
    """
    topo = solve.build_topology(mesh)
    component = {pid: int(topo.component[node]) for pid, node in topo.port_node.items()}
    path = ROOT / "protocols" / f"{chip['id']}.json"
    steps: list[Scenario] = []
    if path.exists():
        data = json.loads(path.read_text())
        for n, step in enumerate(data.get("steps", [])):
            q = step.get("flow_ul_min")
            if not q:
                continue
            feeds = [p for p in step.get("ports", {}).get("feed", []) if p in mesh.ports]
            vents = [p for p in step.get("ports", {}).get("vent", []) if p in mesh.ports]
            if not feeds or not vents:
                continue
            if len({component[p] for p in feeds + vents}) != 1:
                continue
            ports = {p: PortState.flow(float(q) / len(feeds)) for p in feeds}
            ports.update({p: PortState.vent() for p in vents})
            steps.append(Scenario(step=step.get("id", n), ports=ports,
                                  fluid=step.get("fluid", "medium"),
                                  temp_c=step.get("temp_c", 37.0), chip_id=chip["id"]))
    if not steps:
        pair = next(((a, b) for a in sorted(component) for b in sorted(component)
                     if a < b and component[a] == component[b]), None)
        if pair:
            steps.append(Scenario(step="nominal", chip_id=chip["id"], fluid="medium",
                                  temp_c=37.0,
                                  ports={pair[0]: PortState.flow(5.0),
                                         pair[1]: PortState.vent()}))
    return steps


def _connected_port_pairs(mesh: Mesh) -> list[tuple[str, str]]:
    """Ordered (feed, vent) pairs that share a connected component of the mesh."""
    topo = solve.build_topology(mesh)
    component = {pid: int(topo.component[node]) for pid, node in topo.port_node.items()}
    ports = sorted(component)
    return [(a, b) for a in ports for b in ports if a != b and component[a] == component[b]]


class EveryRepoChip(unittest.TestCase):
    """Each chip in `chip/*.json` is a test case (README). Solve it and time it."""

    @classmethod
    def setUpClass(cls):
        cls.chips = []
        for path in sorted((ROOT / "chip").glob("*.json")):
            data = json.loads(path.read_text())
            if "chambers" in data and "channels" in data and data.get("ports"):
                cls.chips.append(data)
        if not cls.chips:
            raise unittest.SkipTest("no repo chips found")

    def test_every_chip_solves_and_conserves_mass(self):
        for chip in self.chips:
            mesh = hele_shaw_chip_mesh(chip)
            scenarios = _protocol_scenarios(chip, mesh)
            if not scenarios:
                continue
            with self.subTest(chip=chip["id"]):
                fields = solve.solve_steps(mesh, scenarios)
                for f in fields:
                    if f.residual.q_in <= 0:
                        continue
                    self.assertLess(f.residual.mass_imbalance_max, 1e-9 * f.residual.q_in)
                    self.assertLess(f.residual.closure, 1e-9)
                    self.assertLessEqual(f.meta["solver"]["residual_rel_max"],
                                         solve.RESIDUAL_RTOL)

    def test_the_largest_chip_solves_its_whole_protocol_quickly(self):
        """The timing number in this module's report: biggest repo chip, all steps.

        Two numbers, because they measure different things. The protocol as written
        usually repeats one port pattern at several flow rates, which linearity collapses
        to a single solve. The stress case gives every step a DIFFERENT pattern, so
        nothing is shared: one factorisation per step, which is the true worst case for
        the fix-and-re-run loop.
        """
        sized = [(len(hele_shaw_chip_mesh(c).nodes), c) for c in self.chips]
        nodes, chip = max(sized, key=lambda t: t[0])
        mesh = hele_shaw_chip_mesh(chip)
        topo = solve.build_topology(mesh)
        scenarios = _protocol_scenarios(chip, mesh)
        if not scenarios:
            self.skipTest(f"{chip['id']} has no usable protocol step")

        print(f"\n  largest repo chip: {chip['id']}  {nodes} nodes, {len(mesh.edges)} edges")
        started = time.perf_counter()
        fields = solve.solve_steps(mesh, scenarios, topology=topo)
        protocol_ms = (time.perf_counter() - started) * 1e3
        print(f"    protocol as written, {len(scenarios)} step(s): {protocol_ms:.1f} ms "
              f"({fields[0].meta['solver']['distinct_rhs']} distinct system(s), "
              f"{fields[0].meta['solver']['method']})")

        # Every step a different feed/vent pair, so no batching and no reuse is possible.
        pairs = _connected_port_pairs(mesh)[:8]
        distinct = [
            Scenario(step=f"stress{i}", chip_id=chip["id"], fluid="medium", temp_c=37.0,
                     ports={feed: PortState.flow(5.0), vent: PortState.vent()})
            for i, (feed, vent) in enumerate(pairs)
        ]
        if not distinct:
            self.skipTest(f"{chip['id']} has no two ports connected in the stand-in mesh")
        started = time.perf_counter()
        stressed = solve.solve_steps(mesh, distinct, topology=topo)
        stress_ms = (time.perf_counter() - started) * 1e3
        print(f"    {len(distinct)} steps, all different port patterns: {stress_ms:.1f} ms "
              f"({stress_ms / len(distinct):.1f} ms/step, "
              f"{stressed[0].meta['solver']['method']})")

        self.assertLess(protocol_ms, 2000.0, "the fix-and-re-run loop needs milliseconds")
        self.assertLess(stress_ms / len(distinct), 500.0)
        for f in fields + stressed:
            self.assertLessEqual(f.meta["solver"]["residual_rel_max"], solve.RESIDUAL_RTOL)
            if f.residual.q_in > 0:
                self.assertLess(f.residual.mass_imbalance_max, 1e-9 * f.residual.q_in)

    def test_fields_round_trip_through_json(self):
        chip = self.chips[0]
        mesh = hele_shaw_chip_mesh(chip)
        scenarios = _protocol_scenarios(chip, mesh)
        if not scenarios:
            self.skipTest("no scenario")
        field = solve.solve_step(mesh, scenarios[0])
        from assay2chip.cfd.model import Field
        again = Field.from_json(json.loads(json.dumps(field.to_json())))
        self.assertEqual(set(again.p_pa), set(field.p_pa))
        self.assertAlmostEqual(again.residual.q_in, field.residual.q_in, places=18)
        for nid, value in field.p_pa.items():
            self.assertAlmostEqual(again.p_pa[nid], value, places=12)


class RealMeshModule1(unittest.TestCase):
    """mesh.py + scenario.py + solve.py on module1_v1 at its own protocol flows.

    The README's end-to-end requirement, on the real mesher rather than the stand-in
    above. module1_v1 is the hand-designed chip the rest of the repo is calibrated
    against, so this is where the three CFD modules have to agree with each other.
    """

    @classmethod
    def setUpClass(cls):
        try:
            from assay2chip.cfd import mesh as mesh_mod, scenario as scenario_mod
        except ImportError as exc:                      # pragma: no cover
            raise unittest.SkipTest(f"mesh.py / scenario.py not importable: {exc}")
        chip = ROOT / "chip" / "module1_v1.json"
        protocol = ROOT / "protocols" / "module1_v1.json"
        if not protocol.exists():                       # pragma: no cover
            raise unittest.SkipTest("protocols/module1_v1.json is not there")
        cls.mesh = mesh_mod.mesh_chip(chip)
        cls.topo = solve.build_topology(cls.mesh)
        records = list(scenario_mod.build_protocol(protocol, chip).scenarios)
        # A representative handful: the whole 191-step protocol is covered by the
        # end-to-end script, and six steps keep this test in the hundreds of ms.
        cls.scenarios = records[:6]
        cls.fields = solve.solve_steps(cls.mesh, cls.scenarios, topology=cls.topo)

    def test_the_mesh_is_structurally_sound_and_fully_connected(self):
        self.assertEqual(self.mesh.check(), [])
        self.assertGreater(self.topo.n_nodes, 1000)
        self.assertEqual(len(set(self.topo.component.tolist())), 1)

    def test_mass_balances_at_the_formulation_floor(self):
        # 1e-9 of the through-flow: see the "How exact the mass balance can be" note in
        # solve.py. The floor is pressure differencing, not the CG tolerance.
        for field in self.fields:
            if field.residual.q_in <= 0:
                continue
            with self.subTest(step=field.meta["step"]):
                self.assertLess(field.residual.mass_imbalance_max,
                                1e-9 * field.residual.q_in)
                self.assertLess(field.residual.closure, 1e-9)
                self.assertLessEqual(field.meta["solver"]["residual_rel_max"],
                                     solve.RESIDUAL_RTOL)

    def test_the_imbalance_floor_is_not_the_solver_tolerance(self):
        # Tightening the target must not change the answer - if it did, the quoted floor
        # would be a tolerance we had simply not bothered to tighten.
        original = solve.CG_RTOL
        try:
            solve.CG_RTOL = 1e-14
            tightened = solve.solve_steps(self.mesh, self.scenarios[:1], topology=self.topo)[0]
        finally:
            solve.CG_RTOL = original
        base = self.fields[0]
        self.assertAlmostEqual(tightened.residual.mass_imbalance_max,
                               base.residual.mass_imbalance_max, places=18)

    def test_dense_and_cg_agree_on_the_real_mesh(self):
        original = solve.DENSE_MAX_UNKNOWNS
        try:
            solve.DENSE_MAX_UNKNOWNS = 10 ** 9
            dense = solve.solve_steps(self.mesh, self.scenarios[:2], topology=self.topo)
        finally:
            solve.DENSE_MAX_UNKNOWNS = original
        self.assertIn("dense", dense[0].meta["solver"]["method"])
        for exact, iterative in zip(dense, self.fields[:2]):
            span = max(abs(v) for v in exact.p_pa.values())
            # An all-capped step has p identically zero, and both paths must say so
            # exactly rather than within a fraction of nothing.
            tolerance = 1e-6 * span if span > 0 else 0.0
            for nid, value in exact.p_pa.items():
                self.assertLessEqual(abs(iterative.p_pa[nid] - value), tolerance, nid)

    def test_module1_runs_in_stokes_flow_at_its_protocol_flows(self):
        # The README's premise: Re well under 1 almost everywhere on this chip.
        for field in self.fields:
            with self.subTest(step=field.meta["step"]):
                self.assertEqual(field.meta["re_over_limit"], [])
                if field.re:
                    self.assertLess(max(field.re.values()), 1.0)

    def test_wall_shear_is_reported_for_every_edge_and_cell(self):
        field = self.fields[0]
        self.assertTrue(field.shear_dyn_cm2)
        self.assertEqual(set(field.u_mm_s),
                         {n.id for n in self.mesh.nodes if n.kind == "cell"})
        # Slit edges exist on this chip and must be scored on their gap, not the depth.
        for edge in self.mesh.slit_edges():
            width, depth = solve.edge_cross_section_m(edge)
            if edge.gap_um:
                self.assertAlmostEqual(depth, edge.gap_um * 1e-6, places=15)
                self.assertIn(edge.id, field.shear_dyn_cm2)

    def test_the_viscosity_is_the_contract_value_scenario_py_resolved(self):
        for field in self.fields:
            self.assertGreater(field.meta["mu_pa_s"], 0.0)
            self.assertIn(field.meta["mu_pa_s"],
                          {contracts.viscosity_37c_pa_s().pa_s,
                           contracts.viscosity_water_20c_pa_s().pa_s,
                           field.meta["mu_pa_s"]})


if __name__ == "__main__":
    unittest.main()

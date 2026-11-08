"""assay2chip/cfd/transport.py: mean age, washout, exchange intent and dead zones.

    python -m unittest tests.test_cfd_transport -v
    python tests/test_cfd_transport.py          # prints the module3 / module6 table

Offline. No network, no files written.

ABOUT THE FIELDS THESE TESTS RUN ON. `assay2chip/cfd/mesh.py` and `assay2chip/cfd/solve.py`
are being written by other agents and are not here yet, so `_hs_chamber` below builds the
Mesh and Field shapes that cfd/README.md specifies, straight from the real chamber
geometry in `chip/*.json`, with a minimal depth-averaged Hele-Shaw solve: conductance
w h^3 / (12 mu L) per face (the parallel-plate transmissibility - NOT the rectangular-duct
form, which belongs to a duct with side walls and goes negative when depth exceeds width),
one Laplacian solve for pressure, flow recovered per face. Viscosity comes from
`contracts.viscosity_for`.

That is deliberately a STAND-IN and it is the fixture's job, not transport.py's:
transport.py never computes a flow. Every number below is therefore provisional in the
geometry and exact in the transport.

    TODO(cfd): once mesh.py and solve.py land, replace `_hs_chamber` with
    `mesh.build(chip)` + `solve.solve(mesh, scenario)` and re-run. The dead-zone fractions
    will move with the mesh; the scheme tests (linear age, exp(-V), sweep == matrix,
    implicit == explicit) are mesh-independent and must not move.
"""

from __future__ import annotations

import ast
import json
import math
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.cfd import transport  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402

UL_MIN_TO_M3_S = 1e-9 / 60.0


# --- the stand-in mesh + field ----------------------------------------------

def _rect_conductance(width_mm, depth_mm, length_mm, mu_pa_s):
    """Rectangular duct, the form in cfd/README.md and operator/flow_calc.py.

    R = 12 mu L / (w h^3 (1 - 0.63 h/w)) assumes h <= w. These channels are 0.2 mm wide
    and 0.3 mm deep, so the roles are assigned by size, not by name - otherwise the
    correction term goes negative and the resistance changes sign.
    """
    a = max(width_mm, depth_mm) * 1e-3
    b = min(width_mm, depth_mm) * 1e-3
    return a * b ** 3 * (1 - 0.63 * b / a) / (12 * mu_pa_s * length_mm * 1e-3)


def _hs_conductance(width_mm, depth_mm, length_mm, mu_pa_s):
    """Hele-Shaw face: a slice of an unbounded 0.3 mm gap, w h^3 / (12 mu L)."""
    return (width_mm * 1e-3) * (depth_mm * 1e-3) ** 3 / (12 * mu_pa_s * length_mm * 1e-3)


class _StandInField:
    __slots__ = ("p_pa", "q_m3_s", "u_mm_s", "residual")

    def __init__(self, p_pa, q_m3_s, u_mm_s, residual):
        self.p_pa = p_pa
        self.q_m3_s = q_m3_s
        self.u_mm_s = u_mm_s
        self.residual = residual


def _hs_chamber(
    *,
    chamber_id,
    center_mm,
    size_mm,
    depth_mm,
    grid_mm,
    mouths,
    mu_pa_s,
    chip_id="stand_in",
):
    """A discretised rectangular chamber plus the Hele-Shaw field in it.

    `mouths` is a list of (x_mm, y_mm, q_ul_min); q > 0 injects, q < 0 withdraws, and the
    total must balance. Each mouth attaches to the nearest cell through a `mouth` edge and
    a `port` node, which is how transport.py sees the boundary.

    Chambers are discretised ACROSS their width, as cfd/README.md requires: a wide chamber
    treated as one node looks like a pipe and hides exactly the dead zones being hunted.
    """
    cx, cy = center_mm[0], center_mm[1]
    sx, sy = size_mm[0], size_mm[1]
    nx = max(int(round(sx / grid_mm)), 1)
    ny = max(int(round(sy / grid_mm)), 1)
    dx, dy = sx / nx, sy / ny
    x0, y0 = cx - sx / 2, cy - sy / 2

    nodes, edges = [], []
    cell_id = {}
    for i in range(nx):
        for j in range(ny):
            nid = f"{chamber_id}__c{i}_{j}"
            cell_id[(i, j)] = nid
            nodes.append({
                "id": nid, "kind": "cell",
                "x_mm": x0 + (i + 0.5) * dx, "y_mm": y0 + (j + 0.5) * dy,
                "area_mm2": dx * dy, "depth_mm": depth_mm, "feature_id": chamber_id,
            })

    conductance = {}
    for i in range(nx):
        for j in range(ny):
            if i + 1 < nx:
                eid = f"fx_{i}_{j}"
                edges.append({"id": eid, "a": cell_id[(i, j)], "b": cell_id[(i + 1, j)],
                              "kind": "hs_face", "length_mm": dx, "width_mm": dy,
                              "depth_mm": depth_mm, "feature_id": chamber_id})
                conductance[eid] = _hs_conductance(dy, depth_mm, dx, mu_pa_s)
            if j + 1 < ny:
                eid = f"fy_{i}_{j}"
                edges.append({"id": eid, "a": cell_id[(i, j)], "b": cell_id[(i, j + 1)],
                              "kind": "hs_face", "length_mm": dy, "width_mm": dx,
                              "depth_mm": depth_mm, "feature_id": chamber_id})
                conductance[eid] = _hs_conductance(dx, depth_mm, dy, mu_pa_s)

    injection = {}
    for k, (mx, my, q_ul_min) in enumerate(mouths):
        i = min(max(int((mx - x0) / dx), 0), nx - 1)
        j = min(max(int((my - y0) / dy), 0), ny - 1)
        pid = f"{chamber_id}__p{k}"
        nodes.append({"id": pid, "kind": "port", "x_mm": mx, "y_mm": my,
                      "area_mm2": 0.0, "depth_mm": depth_mm, "feature_id": None})
        eid = f"m_{k}"
        edges.append({"id": eid, "a": pid, "b": cell_id[(i, j)], "kind": "mouth",
                      "length_mm": max(grid_mm, 0.2), "width_mm": 0.2,
                      "depth_mm": depth_mm, "feature_id": chamber_id})
        conductance[eid] = _rect_conductance(0.2, depth_mm, max(grid_mm, 0.2), mu_pa_s)
        injection[pid] = injection.get(pid, 0.0) + q_ul_min * UL_MIN_TO_M3_S

    # --- Hele-Shaw pressure solve: K p = f, one reference pressure pinned.
    ids = [n["id"] for n in nodes]
    idx = {nid: k for k, nid in enumerate(ids)}
    n = len(ids)
    kmat = np.zeros((n, n))
    for e in edges:
        a, b = idx[e["a"]], idx[e["b"]]
        c = conductance[e["id"]]
        kmat[a, a] += c
        kmat[b, b] += c
        kmat[a, b] -= c
        kmat[b, a] -= c
    f = np.zeros(n)
    for nid, q in injection.items():
        f[idx[nid]] = q
    if abs(f.sum()) > 1e-18:
        raise AssertionError(f"mouth flows do not balance: net {f.sum():.3e} m3/s")
    ref = idx[next(nid for nid, q in injection.items() if q < 0)] if any(
        q < 0 for q in injection.values()) else 0
    kmat[ref, :] = 0.0
    kmat[ref, ref] = 1.0
    f[ref] = 0.0
    p = np.linalg.solve(kmat, f)

    q_m3_s = {e["id"]: conductance[e["id"]] * (p[idx[e["a"]]] - p[idx[e["b"]]])
              for e in edges}

    # --- depth-averaged velocity per cell, from the two face pairs it sits between
    h_m = depth_mm * 1e-3
    u_mm_s = {}
    for i in range(nx):
        for j in range(ny):
            qx = 0.0
            if i > 0:
                qx += q_m3_s[f"fx_{i - 1}_{j}"]
            if i + 1 < nx:
                qx += q_m3_s[f"fx_{i}_{j}"]
            qy = 0.0
            if j > 0:
                qy += q_m3_s[f"fy_{i}_{j - 1}"]
            if j + 1 < ny:
                qy += q_m3_s[f"fy_{i}_{j}"]
            div = 2.0 if 0 < i < nx - 1 else 1.0
            divy = 2.0 if 0 < j < ny - 1 else 1.0
            ux = qx / div / (dy * 1e-3 * h_m) * 1e3
            uy = qy / divy / (dx * 1e-3 * h_m) * 1e3
            u_mm_s[cell_id[(i, j)]] = (ux, uy)

    mass = 0.0
    for i in range(nx):
        for j in range(ny):
            net = 0.0
            for e in edges:
                if e["a"] == cell_id[(i, j)]:
                    net -= q_m3_s[e["id"]]
                elif e["b"] == cell_id[(i, j)]:
                    net += q_m3_s[e["id"]]
            mass = max(mass, abs(net))

    mesh = {
        "nodes": nodes,
        "edges": edges,
        "ports": {nd["id"]: nd["id"] for nd in nodes if nd["kind"] == "port"},
        "meta": {"chip_id": chip_id, "grid_mm": grid_mm, "mu_pa_s": mu_pa_s},
    }
    field = _StandInField(
        p_pa={nid: float(p[idx[nid]]) for nid in ids},
        q_m3_s=q_m3_s,
        u_mm_s=u_mm_s,
        residual={"mass_imbalance_max": mass,
                  "q_in": sum(q for q in injection.values() if q > 0),
                  "q_out": -sum(q for q in injection.values() if q < 0)},
    )
    return mesh, field


def _chip(name):
    return json.loads((ROOT / "chip" / f"{name}.json").read_text())


def _chamber(chip, chamber_id):
    for ch in chip["chambers"]:
        if ch["id"] == chamber_id:
            return ch
    raise KeyError(chamber_id)


# --- the two real chambers --------------------------------------------------

def module3_main_trap(step_kind="wash", grid_mm=0.3):
    """module3_v1_translated's main_trap, 11.32 x 11.32 x 0.3 mm, 38.4 uL.

    The mouths come from the chip's own channel paths, and WHICH mouths are open comes
    from the step's `ports`:
      wash      reagent_in (6.84, 6.0) -> waste (18.16, 14.0)   corner to corner
      lysis     reagent_in (6.84, 6.0) -> lysis_waste (18.16, 6.0)   both on y = 6.0
      backflush backflush_in (18.16, 10.0) -> to_split_tree (12.5, 15.66)
    """
    chip = _chip("module3_v1_translated")
    ch = _chamber(chip, "main_trap")
    flow = 10.8
    routes = {
        "wash": [(6.84, 6.0, flow), (18.16, 14.0, -flow)],
        "lysis": [(6.84, 6.0, flow), (18.16, 6.0, -flow)],
        "backflush": [(18.16, 10.0, flow), (12.5, 15.66, -flow)],
    }
    return _hs_chamber(
        chamber_id="main_trap",
        center_mm=ch["center"], size_mm=ch["size"], depth_mm=ch["size"][2],
        grid_mm=grid_mm, mouths=routes[step_kind],
        mu_pa_s=contracts.viscosity_for(None).pa_s,
        chip_id="module3_v1_translated",
    )


MODULE6_RIB_MM = 0.4
MODULE6_TOOTH_MM = 0.4
"""Module 6's combs: `centipede_channel` is "many short side branches off a spine", and
the audit names the ribs between the branches as 0.4 mm. The chip spec stores each comb as
one channel with two path points, so the teeth are not in the geometry yet and this is the
fixture's reading of them: 0.4 mm openings on a 0.8 mm pitch down both long edges of the
culture field, inlet comb on one side, outlet comb on the other. TODO(cfd): read the teeth
from mesh.py once it expands a centipede_channel."""


def module6_culture(chamber_id="cx_n", flow_ul_min=1.93, grid_mm=0.1, combed=True,
                    stagger=False):
    """module6_v1's culture field, 1.2 x 8.0 x 0.3 mm, 2.88 uL, fed by a comb.

    `combed=True` is the design: 0.4 mm teeth on a 0.8 mm pitch down each long edge, so
    the dead zones sit behind the 0.4 mm ribs. `combed=False` is what a mesh built from
    the chip JSON alone would give - one point inlet and one point outlet.

    `stagger=True` offsets the outlet teeth by half a pitch against the inlet teeth. The
    chip spec DOES NOT SAY which it is - each comb is one channel with two path points -
    and the two readings do not give the same answer, which is a finding in its own right.

    1.93 uL/min is the cold volatile rinse (step 5); the quench runs 1373.3 uL/min.
    """
    chip = _chip("module6_v1")
    ch = _chamber(chip, chamber_id)
    cx, cy = ch["center"][0], ch["center"][1]
    sx, sy = ch["size"][0], ch["size"][1]
    x_in, x_out = cx - sx / 2, cx + sx / 2
    y0 = cy - sy / 2
    pitch = MODULE6_TOOTH_MM + MODULE6_RIB_MM

    if not combed:
        mouths = [(x_in, y0 + sy - 0.1, flow_ul_min), (x_out, y0 + 0.1, -flow_ul_min)]
    else:
        centres = []
        y = y0 + MODULE6_TOOTH_MM / 2
        while y <= y0 + sy - MODULE6_TOOTH_MM / 2 + 1e-9:
            centres.append(y)
            y += pitch
        out_centres = ([y + pitch / 2 for y in centres[:-1]] if stagger else centres)
        per_in = flow_ul_min / len(centres)
        per_out = flow_ul_min / len(out_centres)
        mouths = [(x_in, yc, per_in) for yc in centres]
        mouths += [(x_out, yc, -per_out) for yc in out_centres]

    return _hs_chamber(
        chamber_id=chamber_id,
        center_mm=ch["center"], size_mm=ch["size"], depth_mm=ch["size"][2],
        grid_mm=grid_mm, mouths=mouths,
        mu_pa_s=contracts.viscosity_for(4).pa_s,
        chip_id="module6_v1",
    )


# --- 1. the age scheme ------------------------------------------------------

class MeanAge(unittest.TestCase):
    def test_long_uniform_chamber_has_a_linear_age(self):
        """The headline check. A long chamber fed at one end and drained at the other is
        plug flow, so age must rise LINEARLY along it: cell k has swept k cell volumes."""
        length_mm, grid = 10.0, 0.2
        q = 5.0
        mesh, field = _hs_chamber(
            chamber_id="pipe", center_mm=[5.0, length_mm / 2], size_mm=[0.2, length_mm],
            depth_mm=0.3, grid_mm=grid,
            mouths=[(5.0, 0.0, q), (5.0, length_mm, -q)],   # the two ENDS
            mu_pa_s=contracts.viscosity_for(None).pa_s)
        age = transport.mean_age(mesh, field)
        self.assertEqual(age.scheme, "sweep")

        cells = sorted(
            ((nd["y_mm"], age.age_s[nd["id"]]) for nd in mesh["nodes"]
             if nd["kind"] == "cell"), key=lambda t: t[0])
        ys = np.array([c[0] for c in cells])
        ages = np.array([c[1] for c in cells])

        cell_volume_m3 = 0.2 * grid * 0.3 * 1e-9
        per_cell_s = cell_volume_m3 / (q * UL_MIN_TO_M3_S)

        # Exactly linear through the interior: a straight-line fit leaves NO residual,
        # and the slope is the one plug flow demands - one cell volume per cell.
        inner_y, inner_age = ys[1:-1], ages[1:-1]
        slope, intercept = np.polyfit(inner_y, inner_age, 1)
        self.assertLess(
            float(np.max(np.abs(inner_age - (slope * inner_y + intercept)))), 1e-12)
        self.assertAlmostEqual(float(slope) * grid, per_cell_s, places=12)

        # The two END cells are deliberately NOT on that line: each port mouth is real
        # volume, and the lumped scheme gives half of it to the cell it opens into. So the
        # first cell is older than a bare cell by exactly that half mouth.
        mouth_half_m3 = 0.5 * (max(grid, 0.2) * 0.2 * 0.3) * 1e-9
        self.assertAlmostEqual(
            ages[0], (cell_volume_m3 + mouth_half_m3) / (q * UL_MIN_TO_M3_S), places=12)
        self.assertGreater(ages[0], per_cell_s)

    def test_outlet_mean_age_equals_volume_over_throughput(self):
        """Exact theory for ANY steady domain: the flow-weighted mean age leaving equals
        V / Q. If this drifts, the mesh has lost volume or the field has lost mass."""
        for name, (mesh, field) in (
            ("module3 wash", module3_main_trap("wash", grid_mm=0.5)),
            ("module3 lysis", module3_main_trap("lysis", grid_mm=0.5)),
            ("module6 comb", module6_culture(grid_mm=0.2)),
        ):
            with self.subTest(name):
                age = transport.mean_age(mesh, field)
                self.assertIsNotNone(age.consistency_error)
                self.assertLess(age.consistency_error, 1e-6, name)

    def test_sweep_and_matrix_agree_without_diffusion(self):
        """Two independent schemes on the same field: O(edges) forward substitution in
        flow order, and a dense LU. With no diffusion they must give the same answer."""
        mesh, field = module3_main_trap("wash", grid_mm=0.5)
        swept = transport.mean_age(mesh, field, scheme="sweep")
        solved = transport.mean_age(mesh, field, scheme="matrix")
        self.assertEqual(swept.scheme, "sweep")
        self.assertEqual(swept.flow_loops, [], "Hele-Shaw must have no closed streamlines")
        a = np.array([swept.age_s[k] for k in sorted(swept.age_s)])
        b = np.array([solved.age_s[k] for k in sorted(solved.age_s)])
        self.assertLess(float(np.max(np.abs(a - b)) / np.max(a)), 1e-9)

    def test_diffusion_shortens_the_oldest_liquid(self):
        """Molecular diffusion cannot change the mean (V/Q is fixed) but it must pull the
        extremes in: the oldest corner gets younger, which is the whole reason diffusion
        is in here for slow zones."""
        mesh, field = module3_main_trap("lysis", grid_mm=0.5)
        d = transport.diffusivity("dye").m2_s
        dry = transport.mean_age(mesh, field, diffusivity_m2_s=0.0)
        wet = transport.mean_age(mesh, field, diffusivity_m2_s=d)
        self.assertLess(max(wet.age_s.values()), max(dry.age_s.values()))
        self.assertAlmostEqual(wet.outlet_mean_age_s / dry.outlet_mean_age_s, 1.0, places=5)

    def test_sweep_refuses_to_pretend_it_carries_diffusion(self):
        mesh, field = module6_culture(grid_mm=0.4)
        with self.assertRaises(ValueError):
            transport.mean_age(mesh, field, scheme="sweep", diffusivity_m2_s=4e-10)


# --- 2. the washout scheme --------------------------------------------------

class Washout(unittest.TestCase):
    def test_well_mixed_limit_reproduces_exp_minus_v(self):
        """The second headline check. One cell IS a perfectly stirred tank, so the old
        liquid left after V chamber volumes must be exp(-V) - the 4.61-for-99% rule the
        whole intent comparison rests on."""
        mesh, field = _hs_chamber(
            chamber_id="tank", center_mm=[5.0, 5.0], size_mm=[1.0, 1.0], depth_mm=0.3,
            grid_mm=1.0, mouths=[(4.5, 5.0, 2.0), (5.5, 5.0, -2.0)],
            mu_pa_s=contracts.viscosity_for(None).pa_s)
        self.assertEqual(sum(1 for nd in mesh["nodes"] if nd["kind"] == "cell"), 1)

        for cv in (0.5, 1.0, 2.3, 4.605170186, 8.0):
            with self.subTest(chamber_volumes=cv):
                w = transport.washout(mesh, field, "tank", chamber_volumes=cv)
                self.assertAlmostEqual(w.fraction_reached, 1 - math.exp(-cv), places=4)
                self.assertAlmostEqual(w.fraction_reached, w.well_mixed_fraction, places=4)

    def test_well_mixed_tank_needs_exactly_the_well_mixed_volumes(self):
        """And so `nonideality_factor` is 1 for a perfect tank: anything above 1 is
        geometry, not time-stepping error."""
        mesh, field = _hs_chamber(
            chamber_id="tank", center_mm=[5.0, 5.0], size_mm=[1.0, 1.0], depth_mm=0.3,
            grid_mm=1.0, mouths=[(4.5, 5.0, 2.0), (5.5, 5.0, -2.0)],
            mu_pa_s=contracts.viscosity_for(None).pa_s)
        target = float(contracts.param("exchange.target_fraction"))
        w = transport.washout(mesh, field, "tank", chamber_volumes=1.0,
                              target_fraction=target)
        self.assertAlmostEqual(w.cv_required_well_mixed, 4.60517, places=4)
        self.assertAlmostEqual(w.cv_required_cfd, 4.60517, places=2)
        self.assertAlmostEqual(w.nonideality_factor, 1.0, places=2)

    def test_well_mixed_helpers_are_each_others_inverse(self):
        for f in (0.5, 0.9, 0.95, 0.99, 0.999):
            self.assertAlmostEqual(
                transport.well_mixed_fraction(
                    transport.well_mixed_chamber_volumes(f)), f, places=12)
        self.assertAlmostEqual(transport.well_mixed_chamber_volumes(0.99), 4.60517, places=5)
        with self.assertRaises(ValueError):
            transport.well_mixed_chamber_volumes(1.0)

    def test_the_well_mixed_estimate_is_not_a_bound_in_either_direction(self):
        """ONE chamber, TWO port routes, opposite answers - and that is the finding.

        Hele-Shaw flow does not mix, so a chamber swept end to end is closer to plug flow
        and beats exp(-V); a chamber fed and drained from the SAME edge bypasses its far
        side and loses to it. 4.61 chamber volumes is a model, not a safe bound.
        """
        target = float(contracts.param("exchange.target_fraction"))
        wm = transport.well_mixed_chamber_volumes(target)

        mesh, field = module3_main_trap("wash", grid_mm=0.3)        # corner to corner
        plug = transport.washout(mesh, field, "main_trap", chamber_volumes=wm,
                                 target_fraction=target)
        self.assertGreater(plug.fraction_reached, plug.well_mixed_fraction)
        self.assertLess(plug.nonideality_factor, 1.0)
        self.assertEqual(plug.regime, "plug_like")

        mesh, field = module3_main_trap("lysis", grid_mm=0.3)       # both mouths on y=6.0
        dead = module3_main_trap("lysis", grid_mm=0.3)
        bad = transport.washout(*dead, "main_trap", chamber_volumes=wm,
                                target_fraction=target)
        self.assertLess(bad.fraction_reached, bad.well_mixed_fraction)
        self.assertGreater(bad.nonideality_factor, 1.0)
        self.assertEqual(bad.regime, "dead_zone_limited")

        # same chamber, same flow, same dose: only the port placement differs
        self.assertGreater(bad.cv_required_cfd, 2 * plug.cv_required_cfd)

    def test_the_bulk_clears_early_and_the_tail_lags(self):
        """The shape behind that result: convection-dominated, so the first chamber volume
        does better than exp(-V) and the last percent does far worse."""
        mesh, field = module3_main_trap("lysis", grid_mm=0.4)
        early = transport.washout(mesh, field, "main_trap", chamber_volumes=1.0)
        late = transport.washout(mesh, field, "main_trap", chamber_volumes=4.60517)
        self.assertGreater(early.fraction_reached, early.well_mixed_fraction)
        self.assertLess(late.fraction_reached, late.well_mixed_fraction)

    def test_implicit_and_explicit_agree(self):
        """Two time-integration schemes, one answer. The explicit one has no matrix and no
        size limit, so it is the escape hatch for a mesh past DENSE_SOLVE_MAX."""
        mesh, field = module6_culture(grid_mm=0.2)
        imp = transport.washout(mesh, field, "cx_n", chamber_volumes=3.0, scheme="implicit")
        exp = transport.washout(mesh, field, "cx_n", chamber_volumes=3.0, scheme="explicit")
        self.assertEqual(imp.scheme, "implicit")
        self.assertEqual(exp.scheme, "explicit")
        self.assertNotIn("not trustworthy", " ".join(exp.notes))
        self.assertAlmostEqual(imp.fraction_reached, exp.fraction_reached, places=3)

    def test_washout_is_monotone_and_bounded(self):
        mesh, field = module3_main_trap("wash", grid_mm=0.5)
        w = transport.washout(mesh, field, "main_trap", chamber_volumes=5.0)
        fractions = [f for _, f in w.curve]
        self.assertAlmostEqual(fractions[0], 0.0, places=9)
        for a, b in zip(fractions, fractions[1:]):
            self.assertGreaterEqual(b + 1e-9, a, "old liquid cannot come back")
        self.assertLessEqual(max(fractions), 1.0 + 1e-9)

    def test_volume_ul_and_chamber_volumes_are_the_same_dose(self):
        mesh, field = module3_main_trap("wash", grid_mm=0.5)
        topo = transport.build_topology(mesh, field)
        v_ul = float(np.sum(topo.volume_m3[topo.node_mask("main_trap")])) / 1e-9
        by_cv = transport.washout(mesh, field, "main_trap", chamber_volumes=2.0)
        by_ul = transport.washout(mesh, field, "main_trap", volume_ul=2.0 * v_ul)
        self.assertAlmostEqual(by_cv.fraction_reached, by_ul.fraction_reached, places=9)

    def test_an_unknown_chamber_is_an_error_not_a_zero(self):
        mesh, field = module6_culture(grid_mm=0.4)
        with self.assertRaises(ValueError):
            transport.washout(mesh, field, "no_such_chamber", chamber_volumes=1.0)


# --- 3. intent, which is data -----------------------------------------------

class ExchangeIntent(unittest.TestCase):
    def test_absent_exchange_kind_is_held_to_the_sweep_rule(self):
        """protocol.schema.json: "absence must never be the loophole". Every exchange in
        protocols/module3_v1.json and module6_v1.json is unclassified today, so this is
        the path the real data takes."""
        step = {"step": 5, "operation": "exchange", "volume_ul": 38.443, "washes": 1,
                "note": "rt mix in"}
        intent = transport.exchange_intent(step, 38.443)
        self.assertEqual(intent.kind, "sweep")
        self.assertIn("defaulted", intent.kind_source)
        self.assertEqual(intent.target_fraction,
                         contracts.param("exchange.target_fraction"))
        self.assertAlmostEqual(intent.chamber_volumes, 1.0, places=6)
        self.assertAlmostEqual(intent.well_mixed_chamber_volumes, 4.60517, places=5)

    def test_stated_sweep_uses_the_contract_target(self):
        step = {"step": 4, "operation": "exchange", "exchange_kind": "sweep",
                "volume_ul": 38.443, "washes": 15, "note": "wash"}
        intent = transport.exchange_intent(step, 38.443)
        self.assertEqual(intent.kind_source, "exchange_kind (data)")
        self.assertAlmostEqual(intent.chamber_volumes, 15.0, places=6)
        self.assertIn("exchange.target_fraction", intent.target_source)

    def test_partial_takes_the_fraction_the_step_states(self):
        for note, expect in (
            ("routine half medium change, 50% of the chamber replaced", 0.5),
            ("half change; conditioned medium is the point", 0.5),
            ("replace 1/3 of the chamber", 1 / 3),
            ("0.25 of the chamber is replaced", 0.25),
        ):
            with self.subTest(note=note):
                step = {"step": 1, "operation": "exchange", "exchange_kind": "partial",
                        "volume_ul": 1.44, "washes": 1, "note": note}
                intent = transport.exchange_intent(step, 2.88)
                self.assertAlmostEqual(intent.target_fraction, expect, places=6)
                self.assertEqual(intent.unresolved, ())

    def test_partial_with_no_stated_fraction_is_unresolved_not_guessed(self):
        step = {"step": 1, "operation": "exchange", "exchange_kind": "partial",
                "volume_ul": 1.44, "washes": 1, "note": "medium change"}
        intent = transport.exchange_intent(step, 2.88)
        self.assertIsNone(intent.target_fraction)
        self.assertTrue(any("fraction" in u for u in intent.unresolved))

    def test_fill_is_bounded_above_by_the_chamber(self):
        step = {"step": 1, "operation": "exchange", "exchange_kind": "fill",
                "volume_ul": 60.0, "washes": 1, "note": "first fill"}
        intent = transport.exchange_intent(step, 38.443)
        self.assertEqual(intent.target_fraction, 1.0)
        self.assertTrue(intent.bounded_above_by_chamber)
        self.assertGreater(intent.chamber_volumes, 1.0)

    def test_washes_are_recorded_as_a_modelling_choice(self):
        step = {"step": 2, "operation": "exchange", "volume_ul": 38.443, "washes": 5,
                "note": "lysis"}
        intent = transport.exchange_intent(step, 38.443)
        self.assertAlmostEqual(intent.chamber_volumes, 5.0, places=6)
        self.assertTrue(any("fill-and-drain" in u for u in intent.unresolved))

    def test_an_unknown_kind_is_reported_not_defaulted(self):
        step = {"step": 1, "operation": "exchange", "exchange_kind": "rinse",
                "volume_ul": 1.0, "washes": 1, "note": "?"}
        intent = transport.exchange_intent(step, 1.0)
        self.assertIsNone(intent.target_fraction)
        self.assertTrue(any("unknown exchange_kind" in u for u in intent.unresolved))

    def test_below_one_chamber_volume_is_a_blocker_whatever_the_geometry(self):
        """The schema's floor: "below one chamber volume the step cannot clear the chamber
        however many times it is repeated". Geometry cannot rescue it, so it outranks
        every CFD number and is reported as a blocker."""
        mesh, field = module3_main_trap("wash", grid_mm=0.6)
        for cv in (0.25, 0.8, 0.999):
            with self.subTest(cv=cv):
                step = {"step": 5, "operation": "exchange", "target": "main_trap",
                        "volume_ul": 38.443 * cv, "washes": 1, "note": "sweep"}
                check = transport.check_exchange(mesh, field, step, "main_trap",
                                                 chamber_volume_ul=38.443)
                self.assertEqual(check.severity, "blocker")
                self.assertIn("Below ONE chamber volume", check.message)

    def test_at_the_floor_the_verdict_comes_from_the_field_not_the_rule(self):
        """At exactly one chamber volume the floor is MET, so the step stops being a
        blocker and the question becomes what the chamber actually reached - which is the
        whole reason for simulating it."""
        mesh, field = module3_main_trap("lysis", grid_mm=0.4)
        step = {"step": 5, "operation": "exchange", "target": "main_trap",
                "volume_ul": 38.443, "washes": 1, "note": "sweep"}
        check = transport.check_exchange(mesh, field, step, "main_trap",
                                         chamber_volume_ul=38.443)
        self.assertEqual(check.severity, "high")
        self.assertGreater(check.shortfall, 0.05)
        self.assertGreater(check.extra_chamber_volumes_needed, 1.0)
        self.assertIn("short of the", check.message)

    def test_a_real_module3_step_is_judged_against_the_route_it_uses(self):
        """protocols/module3_v1.json step 5 pushes exactly ONE chamber volume of rt_mix
        and states no exchange_kind, so it is held to the sweep rule. Whether that is
        acceptable depends on the route, and the two real routes disagree."""
        chip_steps = json.loads((ROOT / "protocols" / "module3_v1.json").read_text())["steps"]
        step = next(s for s in chip_steps if s["step"] == 5)
        verdicts = {}
        for route in ("wash", "lysis"):
            mesh, field = module3_main_trap(route, grid_mm=0.4)
            check = transport.check_exchange(mesh, field, step, "main_trap",
                                             chamber_volume_ul=38.443)
            self.assertEqual(check.intent.kind, "sweep")
            self.assertIn("defaulted", check.intent.kind_source)
            verdicts[route] = check
        self.assertLess(verdicts["lysis"].washout.fraction_reached,
                        verdicts["wash"].washout.fraction_reached)
        for check in verdicts.values():
            self.assertIn(check.severity, ("high", "medium"))
            self.assertGreater(check.extra_chamber_volumes_needed, 0.0)


# --- 4. dead zones ----------------------------------------------------------

class DeadZones(unittest.TestCase):
    def test_thresholds_are_named_constants_flagged_as_assumptions(self):
        self.assertEqual(transport.DEAD_ZONE_AGE_FACTOR, 3.0)
        self.assertEqual(transport.DEAD_ZONE_SPEED_FRACTION, 0.10)
        for key in ("dead_zone_age_factor", "dead_zone_speed_fraction"):
            with self.subTest(key):
                record = transport.ASSUMPTIONS[key]
                self.assertTrue(record["assumption"])
                self.assertIn("not a contract", record["source"])
                self.assertTrue(record["why"])

    def test_plug_flow_has_no_dead_zone(self):
        """A uniform pipe is the negative control: every cell has the same speed and the
        age spread is 2x mean at worst, so neither threshold may fire."""
        mesh, field = _hs_chamber(
            chamber_id="pipe", center_mm=[5.0, 5.0], size_mm=[0.2, 10.0], depth_mm=0.3,
            grid_mm=0.2, mouths=[(5.0, 0.0, 5.0), (5.0, 10.0, -5.0)],
            mu_pa_s=contracts.viscosity_for(None).pa_s)
        age = transport.mean_age(mesh, field)
        dz = transport.dead_zones(mesh, field, age, "pipe")
        self.assertEqual(dz.long_age_volume_fraction, 0.0)
        self.assertEqual(dz.low_speed_volume_fraction, 0.0)
        self.assertEqual(dz.either_volume_fraction, 0.0)

    def test_a_corner_fed_square_chamber_has_one(self):
        """And the positive control: feed an 11 mm square from one point and drain it from
        another, and a measurable fraction of it is a low-velocity long-age zone."""
        mesh, field = module3_main_trap("lysis", grid_mm=0.4)
        age = transport.mean_age(mesh, field)
        dz = transport.dead_zones(mesh, field, age, "main_trap")
        self.assertGreater(dz.either_volume_fraction, 0.05)
        self.assertLess(dz.either_volume_fraction, 1.0)
        self.assertGreater(dz.max_age_s, dz.mean_age_s)
        # a volume fraction, not a yes/no
        self.assertIsInstance(dz.either_volume_fraction, float)
        self.assertGreater(dz.volume_ul, 30.0)

    def test_the_thresholds_are_relative_to_the_chamber(self):
        """Halving the flow halves every speed and doubles every age, so the SAME cells
        are dead: a slow chamber is not condemned for being slow."""
        fractions = []
        for flow in (1.93, 19.3):
            mesh, field = module6_culture(flow_ul_min=flow, grid_mm=0.2)
            age = transport.mean_age(mesh, field)
            fractions.append(transport.dead_zones(mesh, field, age, "cx_n")
                             .either_volume_fraction)
        self.assertAlmostEqual(fractions[0], fractions[1], places=6)

    def test_loosening_a_threshold_cannot_find_more_dead_volume(self):
        mesh, field = module3_main_trap("lysis", grid_mm=0.5)
        age = transport.mean_age(mesh, field)
        strict = transport.dead_zones(mesh, field, age, "main_trap", age_factor=6.0,
                                      speed_fraction=0.01)
        loose = transport.dead_zones(mesh, field, age, "main_trap", age_factor=1.5,
                                     speed_fraction=0.5)
        self.assertLessEqual(strict.either_volume_fraction, loose.either_volume_fraction)
        self.assertEqual(strict.thresholds["age_factor"]["value"], 6.0)

    def test_diffusion_shrinks_the_dead_zone(self):
        """Why diffusion is in here at all. Convection-only is the pessimistic bracket;
        turning molecular diffusion on can only pull the long-age tail in, and for a small
        enough pocket it removes the dead zone completely."""
        d = transport.diffusivity("dye").m2_s
        for label, (mesh, field), cid in (
            ("module3 lysis", module3_main_trap("lysis", grid_mm=0.4), "main_trap"),
            ("module6 comb", module6_culture(grid_mm=0.1), "cx_n"),
        ):
            with self.subTest(label):
                dry = transport.dead_zones(
                    mesh, field,
                    transport.mean_age(mesh, field, diffusivity_m2_s=0.0), cid)
                wet = transport.dead_zones(
                    mesh, field,
                    transport.mean_age(mesh, field, diffusivity_m2_s=d), cid)
                self.assertGreater(dry.either_volume_fraction, 0.0)
                self.assertLessEqual(wet.either_volume_fraction,
                                     dry.either_volume_fraction + 1e-9)

    def test_the_comb_teeth_are_a_gap_in_the_chip_spec_and_the_answer_moves(self):
        """module6_v1.json stores each comb as ONE channel with two path points, so
        whether the inlet and outlet teeth line up or interleave is not recorded - and the
        dead-zone fraction differs several-fold between the two readings. That is a
        spec gap to report, not a number to pick."""
        aligned = module6_culture(grid_mm=0.1, stagger=False)
        staggered = module6_culture(grid_mm=0.1, stagger=True)
        fracs = []
        for mesh, field in (aligned, staggered):
            age = transport.mean_age(mesh, field, diffusivity_m2_s=0.0)
            fracs.append(transport.dead_zones(mesh, field, age, "cx_n")
                         .either_volume_fraction)
        self.assertGreater(fracs[1], 2 * fracs[0],
                           "staggered teeth must leave more stagnant volume")

    def test_diffusive_clearance_is_reported_with_its_length_scale(self):
        """Module 6's audit put 450-1800 s on clearing the dead zones behind its 0.4 mm
        ribs by diffusion alone. That is L^2 / 2D for L of 0.6-1.2 mm, so the length scale
        has to be reported next to the time or the number means nothing."""
        mesh, field = module6_culture(grid_mm=0.1)
        age = transport.mean_age(mesh, field)
        dz = transport.dead_zones(mesh, field, age, "cx_n")
        self.assertIsNotNone(dz.dead_depth_max_mm)
        d = transport.diffusivity("dye").m2_s
        self.assertAlmostEqual(
            dz.diffusive_clearance_max_s,
            (dz.dead_depth_max_mm * 1e-3) ** 2 / (2 * d), places=6)
        self.assertLessEqual(dz.diffusive_clearance_mean_s, dz.diffusive_clearance_max_s)


class DiffusivityContract(unittest.TestCase):
    def test_the_dye_value_comes_from_the_contract_and_is_flagged(self):
        d = transport.diffusivity("dye")
        self.assertEqual(d.m2_s, contracts.param("dye.diffusivity_m2_s"))
        self.assertEqual(d.key, "dye.diffusivity_m2_s")
        self.assertTrue(d.assumption)          # the contract itself flags it
        self.assertFalse(d.fallback)
        self.assertIsNone(d.unresolved_note)

    def test_anything_else_is_a_flagged_fallback_never_a_silent_one(self):
        d = transport.diffusivity("lysis_buffer")
        self.assertTrue(d.fallback)
        self.assertIn("no diffusivity in the contracts", d.unresolved_note)

    def test_nothing_in_transport_hard_codes_a_contract_number(self):
        """The repo has been bitten at least six times by a contract number copied into
        code. Parsed rather than grepped, so a value quoted in a docstring - which is
        documentation, not a default - does not count.
        """
        tree = ast.parse((ROOT / "assay2chip" / "cfd" / "transport.py").read_text())
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc is not None and node.body:
                    docstrings.add(id(node.body[0]))
        live = [n for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
                and not isinstance(n.value, bool)]
        values = {float(n.value) for n in live}

        # The two contract values transport.py actually consumes. Viscosity is NOT in this
        # list on purpose: it is 0.001 and 0.0008 Pa s, and 1e-3 is also the mm -> m
        # conversion that appears on nearly every line, so a value test would collide. The
        # assertion for viscosity is stronger anyway - transport.py has no business
        # computing a flow, so it must not mention viscosity at all.
        forbidden = {
            "exchange.target_fraction": contracts.param("exchange.target_fraction"),
            "dye.diffusivity_m2_s": contracts.param("dye.diffusivity_m2_s"),
        }
        for key, value in forbidden.items():
            with self.subTest(key):
                self.assertNotIn(
                    float(value), values,
                    f"{value} is {key}; read it through contracts, never as a literal")

        source = (ROOT / "assay2chip" / "cfd" / "transport.py").read_text()
        for key in ("fluid.viscosity_pa_s", "fluid.viscosity_37c_pa_s", "viscosity_for"):
            with self.subTest(key):
                self.assertNotIn(key, source,
                                 "viscosity belongs to mesh.py and solve.py; transport.py "
                                 "reads the field they produced")
        # and the contract reader really is the shared one from assay2chip/lib
        self.assertIs(transport.contracts, contracts)


class Reporting(unittest.TestCase):
    def test_analyse_is_json_serialisable_with_units_in_every_key(self):
        chip_steps = json.loads((ROOT / "protocols" / "module3_v1.json").read_text())["steps"]
        step = next(s for s in chip_steps if s["step"] == 4)
        mesh, field = module3_main_trap("wash", grid_mm=0.6)
        out = transport.analyse(mesh, field, step=step)
        json.dumps(out)                                   # must not raise
        self.assertEqual(out["chip_id"], "module3_v1_translated")
        self.assertIn("main_trap", out["chambers"])
        self.assertIn("dead_zones", out["chambers"]["main_trap"])
        self.assertIn("exchange", out["chambers"]["main_trap"])
        self.assertIn("dead_zone_age_factor", out["assumptions"])
        dz = out["chambers"]["main_trap"]["dead_zones"]
        for key in ("chamber_volume_ul", "mean_age_s", "mean_speed_mm_s",
                    "dead_volume_fraction", "diffusive_clearance_max_s"):
            self.assertIn(key, dz)

    def test_chamber_ids_come_off_the_mesh(self):
        mesh, _ = module6_culture(grid_mm=0.4)
        self.assertEqual(transport.chamber_ids(mesh), ["cx_n"])


# --- the report -------------------------------------------------------------

def report():
    """The module3 / module6 numbers, printed. Run: python tests/test_cfd_transport.py

    Every chamber is run TWICE, with molecular diffusion off and on, because the
    difference is the answer to "does diffusion rescue this dead zone": convection-only is
    the pessimistic bracket, and `dye.diffusivity_m2_s` is the optimistic one.
    """
    target = float(contracts.param("exchange.target_fraction"))
    d = transport.diffusivity("dye").m2_s
    rows = []

    cases = [
        ("module3 main_trap lysis   reagent_in->lysis_waste, both mouths on y=6.0",
         module3_main_trap("lysis", grid_mm=0.3), "main_trap", 5.0),
        ("module3 main_trap wash    reagent_in->waste, corner to corner",
         module3_main_trap("wash", grid_mm=0.3), "main_trap", 15.0),
        ("module3 main_trap bkflush backflush_in->to_split_tree",
         module3_main_trap("backflush", grid_mm=0.3), "main_trap", 1.0),
        ("module6 cx_n      comb    0.4mm teeth/0.4mm ribs, aligned",
         module6_culture(grid_mm=0.1), "cx_n", 7.95),
        ("module6 cx_n      comb    0.4mm teeth/0.4mm ribs, STAGGERED",
         module6_culture(grid_mm=0.1, stagger=True), "cx_n", 7.95),
        ("module6 cx_n      2-point what the chip JSON alone gives",
         module6_culture(grid_mm=0.1, combed=False), "cx_n", 7.95),
    ]

    print("\ntransport.py on the real chamber geometry from chip/*.json")
    print("(stand-in Hele-Shaw field: mesh.py and solve.py are still being written)")
    print(f"exchange.target_fraction = {target} -> a well-mixed chamber needs "
          f"{transport.well_mixed_chamber_volumes(target):.3f} chamber volumes")
    print(f"dye.diffusivity_m2_s = {d:g} (contract flags it an assumption); "
          f"dead-zone thresholds {transport.DEAD_ZONE_AGE_FACTOR}x mean age and "
          f"{transport.DEAD_ZONE_SPEED_FRACTION} x mean speed, both ASSUMPTIONS\n")

    header = (f"{'case':<68} {'dead%':>6} {'dead%':>6} {'age%':>6} {'slow%':>6} "
              f"{'CV':>6} {'reach%':>7} {'WM%':>6} {'need':>6} {'xWM':>5} "
              f"{'regime':<18} {'diffuse s':>13}")
    print(header)
    print(f"{'':<68} {'D=0':>6} {'D=dye':>6}")
    print("-" * len(header))
    for label, (mesh, field), cid, cv in cases:
        dry_age = transport.mean_age(mesh, field, diffusivity_m2_s=0.0)
        dry_dz = transport.dead_zones(mesh, field, dry_age, cid)
        age = transport.mean_age(mesh, field, diffusivity_m2_s=d)
        dz = transport.dead_zones(mesh, field, age, cid)
        w = transport.washout(mesh, field, cid, chamber_volumes=cv,
                              target_fraction=target, diffusivity_m2_s=d)
        need = (f"{w.cv_required_cfd:.2f}" if w.cv_required_cfd is not None
                else f">{transport.WASHOUT_CV_SEARCH_CAP:.0f}")
        factor = f"{w.nonideality_factor:.2f}" if w.nonideality_factor else "-"
        src = dry_dz if dry_dz.diffusive_clearance_mean_s is not None else dz
        diff = ("-" if src.diffusive_clearance_mean_s is None else
                f"{src.diffusive_clearance_mean_s:.0f}-"
                f"{src.diffusive_clearance_max_s:.0f}")
        print(f"{label:<68} {dry_dz.either_volume_fraction * 100:6.2f} "
              f"{dz.either_volume_fraction * 100:6.2f} "
              f"{dry_dz.long_age_volume_fraction * 100:6.2f} "
              f"{dry_dz.low_speed_volume_fraction * 100:6.2f} {cv:6.2f} "
              f"{w.fraction_reached * 100:7.3f} {w.well_mixed_fraction * 100:6.2f} "
              f"{need:>6} {factor:>5} {w.regime:<18} {diff:>13}")
        rows.append((label, dry_dz, dz, w, age))

    print("\nresidence time, per chamber (s). A volume-weighted mean near V/(2Q) is plug")
    print("flow; near V/Q is well mixed; above V/Q means part of it is being bypassed.")
    for label, dry_dz, dz, w, age in rows:
        vq = age.theoretical_mean_age_s
        print(f"  {label[:68]:<68} mean {dry_dz.mean_age_s:7.1f}  p95 "
              f"{dry_dz.p95_age_s:7.1f}  max {dry_dz.max_age_s:8.1f}  "
              f"V/Q {vq:7.1f}  mean/(V/Q) {dry_dz.mean_age_s / vq:5.2f}")
    return rows


if __name__ == "__main__":
    report()

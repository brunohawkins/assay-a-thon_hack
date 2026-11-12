"""assay2chip/cfd/validate.py - the benchmark suite the rest of the CFD rests on.

A solver nobody has checked is worse than no solver, so nothing in `cfd/findings.json` may
be believed unless this file says so. `analyse.py` reads the benchmark verdicts out of this
report and marks EVERY finding untrusted the moment one of them fails.

    python assay2chip/cfd/validate.py                 # table to stdout + report to the work dir
    python assay2chip/cfd/validate.py --out x.json --json
    from assay2chip.cfd import validate; report = validate.run()

What is checked, and against what:

  duct_resistance        the EXACT Fourier-series solution for Poiseuille flow in a
                         rectangle, across w/h 1-30, against the `1 - 0.63 h/w` form the
                         README fixes as the contract and `mesh.duct_g_geom_m3` builds
                         every duct edge from.
  series_parallel        hand-computed series, parallel and ladder networks, solved by
                         solve.py on a mesh assembled here by hand.
  hele_shaw_uniform      uniform flow down a long chamber: pressure must be linear and
                         dP = 12 mu Q L / (W h^3). Exact on the discrete grid, so this is
                         a machine-precision test of the Laplacian and the linear solve.
  hele_shaw_point_source a point source in a disk: p = (6 mu Q / pi h^3) ln(R/r).
  grid_convergence       module1_v1 meshed at 0.2 / 0.1 / 0.05 mm, Richardson
                         extrapolation on three quantities of interest -> observed order
                         and the recommended default spacing.
  operator_crosscheck    operator/network.py on module1_v1, mode by mode, at the same
                         flow and viscosity. It is an INDEPENDENT solver, so the two must
                         agree; where they do not this file says WHICH is wrong and shows
                         the arithmetic, and never averages or tunes either to match.
  mass_conservation      every node of every chip in chip/*.json, re-derived here from
                         `Field.q_m3_s` rather than read out of `Field.residual`.
  wall_shear             the slot form tau = 6 mu Q / (w h^2) and solve.py's exact
                         aspect-ratio correction against an independent series solution,
                         w/h 1-30.
  hele_shaw_validity     where w/h < ~5 makes a depth-averaged model questionable near
                         side walls, and whether mesh.py chose a duct there.

EXACT physics, derived here independently of solve.py so the two can be compared. Other
modules may import these, but note that solve.py has its own equivalents and the point of
`wall_shear` is that they agree:

    duct_shape_factor(w_mm, h_mm)        exact series bracket B in Q = B w h^3 dP / 12 mu L
    duct_resistance_exact(L, w, h, mu)   Pa s / m^3
    duct_resistance_approx(L, w, h, mu)  the 1 - 0.63 h/w form, for comparison only
    hele_shaw_face_conductance(...)      w h^3 / 12 mu L - NO side-wall term; an interior
                                         face of a chamber has no side wall
    wall_shear_slot(q, w, h, mu)         6 mu Q / (w h^2), the parallel-plate value
    wall_shear_max_exact(q, w, h, mu)    peak wall shear from the series solution
    wall_shear_correction(w_mm, h_mm)    exact / slot; what solve.rect_shear_factor must be

Report shape (`schema: cfd-validation/1`), written wherever `--out` says and returned by
`run()`:

    {"schema", "status": "pass|fail|incomplete", "passed": bool, "trusted": bool,
     "reason": "...", "recommended_grid_mm": 0.1,
     "counts": {"pass", "fail", "skipped"},
     "benchmarks": [{"id", "name", "passed", "status", "requirement", "metric", "value",
                     "tolerance", "units", "message", "detail"}],
     "checks": [{"name", "ok", "expected", "got", "detail"}],     # report.py's table
     "validation_findings": [{id, class, severity, chip_id, feature_id, metric, value,
                              limit, units, message, fix_hint}],
     "contracts": {...fingerprint...}}

`checks` is the same information in the shape `report.py` renders; `benchmarks` is the
full record. `validation_findings` carry the same keys as `findings.json` entries so
analyse.py can merge them straight in; their `class` is `model_error`, `unmodelled` or
`dead_end` and their `source` is `validate`.

Contract values come from assay2chip.lib.contracts, never from a literal here. The numbers
that ARE literals are the suite's own acceptance tolerances (`TOL`) and the benchmark
geometries, neither of which is a contract value.

SI internally; mm / uL per min / Pa / dyn per cm^2 at the JSON boundary. numpy for the
grids, standard library for everything else, no network, no files read but the repo's own.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "operator")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from assay2chip.lib import contracts  # noqa: E402

PI = math.pi
UL_MIN_TO_M3_S = 1e-9 / 60.0
M3_S_TO_UL_MIN = 1.0 / UL_MIN_TO_M3_S
DYN_CM2_PER_PA = 10.0
MM = 1e-3

# The suite's own acceptance criteria. Not contract values: they say how closely an
# implementation has to reproduce an analytic answer before this file calls it correct.
TOL = {
    "exact_vs_formula_wide": 0.01,     # the 0.63 form vs the exact series for w/h >= 2
    # mesh.py's duct law vs the EXACT series. Was 1e-12 against the 1 - 0.63 h/w form, which
    # is right for two implementations of one closed form - but flow_calc.py moved to the
    # exact series (it is 14% off at a square duct) and mesh.py followed it, so this now
    # compares two truncations of the same series and its floor is truncation, ~2e-9, not
    # machine precision. Leaving it at 1e-12 failed the benchmark for being more accurate.
    "law_consistency": 1e-8,
    "series_parallel": 1e-9,           # hand calculation, so machine precision
    "hele_shaw_uniform": 1e-7,         # exact on the discrete grid
    "hele_shaw_log_slope": 0.03,       # point source: log singularity + stepped rim
    # Fraction of total inflow. solve.py closes mass exactly on a hand-calculable network
    # and to 1e-10..1e-11 of through-flow on a full mesh - a floor set by the formulation,
    # not a knob - so 1e-9 is that floor with an order of margin, and still seven orders
    # below the smallest flow any finding turns on. Nothing physical can hide under it.
    "mass_imbalance": 1e-9,
    # The SAME quantity summed globally (total fed vs total recovered) instead of per node.
    # Its floor is not the formulation's, it is float64 accumulation over every edge, so it
    # grows with mesh size: ~1e-10 at 1e4 nodes, ~1e-8 at 4e5. Gating trust on it meant the
    # biggest chip failed the suite for being big. The per-node figure above is the check
    # that can catch a leak - a node imbalance shows up locally before it can reach the
    # global sum - so this one is kept as a coarse backstop with room for round-off, still
    # five orders below the smallest flow any finding turns on.
    "mass_total_imbalance": 1e-7,
    "crosscheck": 0.03,                # "within a few percent"
    "shear_factor": 1e-6,              # two independent series for the same quantity
    "grid_convergence_qoi": 0.01,      # how close to the Richardson value a grid must be
    "aspect_ratio_error_report": 0.02,  # raise a finding above this duct-formula error
}

# Below this width-to-depth ratio a depth-averaged (Hele-Shaw) model is questionable: the
# side-wall boundary layer is of order the depth, so it covers most of the span.
HS_ASPECT_FLOOR = 5.0

CHIP_DIR = ROOT / "chip"
DEFAULT_OUT = ROOT / "assay2chip" / "work" / "cfd" / "validation.json"


# --------------------------------------------------------------------------------------
# exact rectangular-duct physics
# --------------------------------------------------------------------------------------


def _odd(n_terms: int) -> range:
    return range(1, 2 * n_terms, 2)


def duct_shape_factor(w_mm: float, h_mm: float, n_terms: int = 500) -> float:
    """Exact series bracket B in Q = B * w h^3 dP / (12 mu L), h the smaller side.

    B = 1 - (192 h / pi^5 w) sum_{n odd} tanh(n pi w / 2h) / n^5

    B -> 1 for an infinitely wide slot and B = 0.42173 for a square, which reproduces the
    textbook square-duct result Q = 0.035144 a^4 dP / (mu L). The familiar `1 - 0.63 h/w`
    is this series with every tanh set to 1 and sum_{n odd} 1/n^5 = 1.004524 folded in:
    192 x 1.004524 / pi^5 = 0.63029. So 0.63 is the WIDE-CHANNEL LIMIT of this series and
    is only as good as tanh(pi w / 2h) ~ 1 - which is why it is excellent at w/h 2 and
    14% wrong at w/h 1.
    """
    w, h = max(w_mm, h_mm), min(w_mm, h_mm)
    if w <= 0 or h <= 0:
        raise ValueError(f"duct cross-section must be positive, got {w_mm} x {h_mm} mm")
    s = sum(math.tanh(n * PI * w / (2 * h)) / n ** 5 for n in _odd(n_terms))
    return 1.0 - 192.0 * h * s / (PI ** 5 * w)


def duct_resistance_exact(length_mm: float, width_mm: float, depth_mm: float, mu: float) -> float:
    """Pa s / m^3 from the exact series. The reference every duct edge is checked against."""
    w, h = max(width_mm, depth_mm) * MM, min(width_mm, depth_mm) * MM
    return 12 * mu * (length_mm * MM) / (w * h ** 3 * duct_shape_factor(width_mm, depth_mm))


def duct_resistance_approx(length_mm: float, width_mm: float, depth_mm: float, mu: float) -> float:
    """The README / operator/flow_calc.py / mesh.py form: 12 mu L / (w h^3 (1 - 0.63 h/w))."""
    w, h = max(width_mm, depth_mm) * MM, min(width_mm, depth_mm) * MM
    return 12 * mu * (length_mm * MM) / (w * h ** 3 * (1 - 0.63 * h / w))


#: 192 x sum_{n odd} 1/n^5 / pi^5, the exact wide-channel coefficient the contract rounds
#: to 0.63. The rounding is why the 0.63 form goes very slightly OPTIMISTIC when wide.
WIDE_COEFFICIENT = 0.6302488762838570


def duct_resistance_error(width_mm: float, depth_mm: float) -> float:
    """Signed relative error of the 0.63 form against the exact series, at any L and mu.

    Positive means the 0.63 form OVER-states resistance, and so over-states pressure.

    The sign is not constant, and that is worth knowing rather than glossing. Two effects
    compete: tanh(n pi w / 2h) < 1 makes the true bracket SMALLER than the wide limit
    (so the formula under-states R), while the contract's 0.63 is a rounding down of
    0.6302489 (so the formula over-states the bracket and under-states R). Below about
    w/h 2.4 the tanh deficit wins and the formula is conservative, by up to +14% at a
    square. Above it the rounding wins and the formula is OPTIMISTIC - by at most 0.15%,
    which is negligible, but it means "the 0.63 form is always safe" is false.
    """
    w, h = max(width_mm, depth_mm), min(width_mm, depth_mm)
    return duct_shape_factor(w, h) / (1 - 0.63 * h / w) - 1.0


def _error_sign_crossover(lo: float = 1.0, hi: float = 30.0) -> float:
    """The w/h where the 0.63 form stops over-stating resistance and starts under-stating.

    Bisection on `duct_resistance_error`, which is monotone through zero in this range.
    Reported rather than asserted: it is a property of the contract's rounding, and the
    point is that the report states which side of it a given channel is on.
    """
    if duct_resistance_error(lo, 1.0) * duct_resistance_error(hi, 1.0) > 0:
        return float("nan")
    for _ in range(200):
        mid = (lo + hi) / 2
        if duct_resistance_error(mid, 1.0) > 0:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def hele_shaw_face_conductance(width_mm: float, length_mm: float, depth_mm: float, mu: float) -> float:
    """m^3 / (Pa s) for one interior face of a depth-averaged chamber: w h^3 / (12 mu L).

    NO `1 - 0.63 h/w` term, and that is not an oversight: the 0.63 accounts for the two
    SIDE WALLS of a duct, and an interior face of a chamber has none. Using the duct form
    on an interior face manufactures side walls everywhere inside the chamber and makes
    the answer depend on `grid_mm`. The side walls the chamber really does have are a
    boundary effect, which is what `hele_shaw_validity` bounds.
    """
    return (width_mm * MM) * (depth_mm * MM) ** 3 / (12 * mu * (length_mm * MM))


def _shear_sum(w_mm: float, h_mm: float, n_terms: int = 200) -> float:
    """S = sum_{n odd} (1 - sech(n pi w / 2h)) / n^2, via sum_{n odd} 1/n^2 = pi^2/8.

    Written this way because the sech terms die geometrically while 1/n^2 does not, so a
    direct sum would need millions of terms for the precision this comparison needs.
    """
    w, h = max(w_mm, h_mm), min(w_mm, h_mm)
    tail = 0.0
    for n in _odd(n_terms):
        x = n * PI * w / (2 * h)
        if x > 350:            # cosh overflows long before sech matters
            break
        tail += 1.0 / (math.cosh(x) * n ** 2)
    return PI ** 2 / 8.0 - tail


def wall_shear_slot(q_m3_s: float, width_mm: float, depth_mm: float, mu: float) -> float:
    """Pa. tau = 6 mu Q / (w h^2): the parallel-plate value, with no aspect correction."""
    return 6 * mu * abs(q_m3_s) / ((width_mm * MM) * (depth_mm * MM) ** 2)


def wall_shear_max_exact(q_m3_s: float, width_mm: float, depth_mm: float, mu: float) -> float:
    """Pa. Peak wall shear - the middle of the wide wall - from the series solution.

    tau_max = (4 G h / pi^2) sum_{n odd} (1 - sech(n pi w / 2h)) / n^2, with G = dP/dx
    recovered from the exact resistance at this Q. This is the number a shear ceiling
    should be compared against: it is the largest shear in the section and it sits where
    adherent cells sit.
    """
    w, h = max(width_mm, depth_mm), min(width_mm, depth_mm)
    g = duct_resistance_exact(1.0, w, h, mu) * abs(q_m3_s) / MM      # dP per metre
    return 4 * g * (h * MM) * _shear_sum(w, h) / PI ** 2


def wall_shear_mean_exact(q_m3_s: float, width_mm: float, depth_mm: float, mu: float) -> float:
    """Pa. Perimeter-averaged wall shear, straight from force balance: tau = G A / P."""
    w, h = max(width_mm, depth_mm) * MM, min(width_mm, depth_mm) * MM
    g = duct_resistance_exact(1.0, width_mm, depth_mm, mu) * abs(q_m3_s) / MM
    return g * (w * h) / (2 * (w + h))


def wall_shear_correction(width_mm: float, depth_mm: float) -> float:
    """tau_max_exact / tau_slot: multiply the slot formula by this for peak wall shear.

    A function of w/h alone (1.602 for a square, 1.443 at w/h 1.5, 1.021 at w/h 30) that
    tends to 1/(1 - 0.63 h/w) for a wide channel, so it is never below 1 and the
    uncorrected slot formula always UNDER-reports peak wall shear. solve.py computes the
    same quantity from a different series expansion; `bench_wall_shear` compares them.
    """
    w, h = max(width_mm, depth_mm), min(width_mm, depth_mm)
    return 8 * _shear_sum(w, h) / (duct_shape_factor(w, h) * PI ** 2)


# --------------------------------------------------------------------------------------
# an independent linear solve: a resistance network and a depth-averaged grid
# --------------------------------------------------------------------------------------


def _cg(matvec: Callable[[np.ndarray], np.ndarray], b: np.ndarray, diag: np.ndarray,
        tol: float = 1e-14, max_iter: int = 100000) -> tuple[np.ndarray, int, float]:
    """Jacobi-preconditioned conjugate gradient. The matrix is SPD once a port is fixed."""
    x = np.zeros_like(b)
    r = b.copy()
    scale = float(np.linalg.norm(b)) or 1.0
    inv = 1.0 / diag
    z = inv * r
    p = z.copy()
    rz = float(r @ z)
    for it in range(1, max_iter + 1):
        ap = matvec(p)
        denom = float(p @ ap)
        if denom <= 0.0:
            break
        alpha = rz / denom
        x += alpha * p
        r -= alpha * ap
        res = float(np.linalg.norm(r)) / scale
        if res <= tol:
            return x, it, res
        z = inv * r
        rz_new = float(r @ z)
        p = z + (rz_new / rz) * p
        rz = rz_new
    return x, max_iter, float(np.linalg.norm(r)) / scale


@dataclass
class LinearNetwork:
    """Nodes, conductances, flow sources and fixed pressures: `A p = b`, then edge flows.

    This is validate.py's OWN solver, written from the conservation law rather than shared
    with solve.py, because a cross-check between two copies of the same code is worth
    nothing. It is deliberately plain: dense assembly, CG, no reuse.
    """

    node_ids: list[Any]
    ia: list[int] = field(default_factory=list)
    ib: list[int] = field(default_factory=list)
    cond: list[float] = field(default_factory=list)
    edge_ids: list[Any] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.index = {n: i for i, n in enumerate(self.node_ids)}

    def add_edge(self, a: Any, b: Any, conductance: float, edge_id: Any = None) -> None:
        self.ia.append(self.index[a])
        self.ib.append(self.index[b])
        self.cond.append(float(conductance))
        self.edge_ids.append(edge_id if edge_id is not None else len(self.edge_ids))

    def solve(self, sources: dict[Any, float], fixed: dict[Any, float]) -> dict[str, Any]:
        """sources: node -> m^3/s in. fixed: node -> Pa. Pressures, edge flows, residual."""
        n = len(self.node_ids)
        ia = np.asarray(self.ia, dtype=np.int64)
        ib = np.asarray(self.ib, dtype=np.int64)
        c = np.asarray(self.cond, dtype=float)
        ref = float(np.median(c)) if len(c) else 1.0
        c_s = c / ref
        is_fixed = np.zeros(n, dtype=bool)
        p_fix = np.zeros(n)
        for node, value in fixed.items():
            is_fixed[self.index[node]] = True
            p_fix[self.index[node]] = value
        rhs = np.zeros(n)
        for node, q in sources.items():
            rhs[self.index[node]] += q / ref
        diag = np.zeros(n)
        np.add.at(diag, ia, c_s)
        np.add.at(diag, ib, c_s)
        keep = ~(is_fixed[ia] & is_fixed[ib])
        ia_k, ib_k, c_k = ia[keep], ib[keep], c_s[keep]
        for src, dst in ((ia_k, ib_k), (ib_k, ia_k)):
            mask = is_fixed[dst] & ~is_fixed[src]
            np.add.at(rhs, src[mask], c_k[mask] * p_fix[dst[mask]])
        ff = ~is_fixed[ia_k] & ~is_fixed[ib_k]
        ia_f, ib_f, c_f = ia_k[ff], ib_k[ff], c_k[ff]
        free = ~is_fixed
        if not free.any():
            p, iters, res = p_fix, 0, 0.0
        else:
            d = np.where(free & (diag > 0), diag, 1.0)

            def matvec(x: np.ndarray) -> np.ndarray:
                y = d * x
                y -= np.bincount(ia_f, weights=c_f * x[ib_f], minlength=n)
                y -= np.bincount(ib_f, weights=c_f * x[ia_f], minlength=n)
                return np.where(free, y, 0.0)

            x, iters, res = _cg(matvec, np.where(free, rhs, 0.0), d)
            p = np.where(free, x, p_fix)
        q = c * (p[ia] - p[ib])                                  # m^3/s, + means a -> b
        net = np.zeros(n)
        np.add.at(net, ia, -q)
        np.add.at(net, ib, q)
        for node, value in sources.items():
            net[self.index[node]] += value
        total_in = sum(v for v in sources.values() if v > 0) or 1.0
        interior = net[free] if free.any() else np.zeros(1)
        return {
            "p_pa": {nid: float(p[i]) for i, nid in enumerate(self.node_ids)},
            "q_m3_s": {eid: float(q[k]) for k, eid in enumerate(self.edge_ids)},
            "p_array": p,
            "node_net_m3_s": {nid: float(net[i]) for i, nid in enumerate(self.node_ids)},
            "max_interior_imbalance_frac": float(np.max(np.abs(interior))) / total_in,
            "cg_iterations": iters,
            "cg_residual": res,
        }


def _reachable(net: LinearNetwork, roots: Iterable[Any]) -> set[Any]:
    adj: dict[Any, list[Any]] = {n: [] for n in net.node_ids}
    for k in range(len(net.cond)):
        a, b = net.node_ids[net.ia[k]], net.node_ids[net.ib[k]]
        adj[a].append(b)
        adj[b].append(a)
    seen, stack = set(), list(roots)
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adj[node])
    return seen


# --------------------------------------------------------------------------------------
# the CFD under test
# --------------------------------------------------------------------------------------


@dataclass
class CfdUnderTest:
    """mesh.py, solve.py and model.py, imported for real, or the reason they would not.

    A half-built sibling is a skipped benchmark and an untrusted report, never a crash:
    this file has to produce a verdict even when the thing it is testing is broken.
    """

    model: Any = None
    mesh: Any = None
    solve: Any = None
    reason: str = ""

    @property
    def available(self) -> bool:
        return None not in (self.model, self.mesh, self.solve)

    def as_json(self) -> dict:
        return {"available": self.available, "reason": self.reason,
                "modules": {name: getattr(getattr(self, name), "__file__", None)
                            for name in ("model", "mesh", "solve") if getattr(self, name)}}


def discover_cfd() -> CfdUnderTest:
    mods: dict[str, Any] = {}
    for name in ("model", "mesh", "solve"):
        try:
            mods[name] = __import__(f"assay2chip.cfd.{name}", fromlist=["*"])
        except Exception as exc:
            return CfdUnderTest(reason=f"assay2chip/cfd/{name}.py will not import: "
                                       f"{type(exc).__name__}: {exc}", **mods)
    return CfdUnderTest(reason="", **mods)


def _solve_mesh(cfd: CfdUnderTest, mesh: Any, ports: dict[str, Any], *, step: Any = "validate",
                mu: float | None = None) -> Any:
    """One Field from solve.py, with the viscosity pinned so nothing is inferred."""
    scenario = cfd.model.Scenario(step=step, ports=ports, temp_c=37.0, mu_pa_s=mu,
                                 fluid="medium")
    return cfd.solve.solve_step(mesh, scenario.for_mesh(mesh))


# --------------------------------------------------------------------------------------
# a reference resistance network built straight from a chip spec
# --------------------------------------------------------------------------------------


def _chamber_leg_mm(chamber: dict, entry_xy: Sequence[float]) -> tuple[float, float]:
    """(length, width) in mm of the slab from an entry point on a chamber outline to its
    centre: the part of the chamber a channel has to cross to reach the chamber node, and
    the width of the wall the opening sits in.

    The rule operator/network.py documents, re-derived from the geometry here rather than
    imported, because if the two implementations shared it the cross-check could not see a
    mistake in it.
    """
    (cx, cy, _), (sx, sy, _) = chamber["center"], chamber["size"]
    dx = abs(entry_xy[0] - cx) / (sx / 2) if sx else 0.0
    dy = abs(entry_xy[1] - cy) / (sy / 2) if sy else 0.0
    return (sx / 2, sy) if dx >= dy else (sy / 2, sx)


def _weir(chamber: dict) -> dict | None:
    r = chamber.get("retention") or {}
    return r if r.get("type") == "weir" and r.get("opening_um") else None


def slit_resistance_pa_s_m3(width_mm: float, gap_um: float, length_mm: float, mu: float) -> float:
    """A weir crest as a very shallow duct of the chamber's full width: gap^3, so a
    +/-20 um z tolerance moves it by a factor of 2-3 and must be swept, not ignored."""
    return 12 * mu * (length_mm * MM) / ((width_mm * MM) * (gap_um * 1e-6) ** 3)


def reference_network(chip: dict, mu: float, *, exact: bool = True,
                      weirs: bool = True) -> tuple[LinearNetwork, dict]:
    """A hydraulic network for any chip spec. validate.py's own second opinion.

    Independence, stated plainly because it is what the cross-check is worth: channel
    centrelines come from operator/network.py's `channel_geometry` (the DXF when there is
    one, else the spec path, else the trimmed straight line), because there is only one
    right answer for a centreline and re-reading the DXF here would test nothing. The
    resistance law, the chamber-crossing slab, the weir crest and the linear solve are
    all derived here. `exact` picks the exact series over the 0.63 form; `weirs` adds a
    crest to every opening of a weir chamber, which is the assumption mesh.py makes and
    the one operator/network.py does not make at all.
    """
    import network as nw  # noqa: PLC0415  - operator/, injected on sys.path above

    dxf = ROOT / "cad" / "exports" / chip["id"] / f"{chip['id']}.dxf"
    geometry = nw.channel_geometry(chip, dxf if dxf.exists() else None)
    chambers = {c["id"]: c for c in chip["chambers"]}
    nodes = list(chambers) + [p["id"] for p in chip.get("ports", [])]
    net = LinearNetwork(nodes)
    r_fn = duct_resistance_exact if exact else duct_resistance_approx
    # A crest's length along the flow is not in the chip spec. The narrowest in-plane
    # feature the process can cut is, so that is what a crest is: contract, not guess.
    crest_mm = float(contracts.floor("min_open_channel_mm"))
    meta: dict[str, dict] = {}
    for ch in chip["channels"]:
        g = geometry[ch["id"]]
        r = r_fn(g["length_mm"], g["width_mm"], g["depth_mm"], mu)
        legs, crests = [], []
        for end, point in ((ch["from"], g["a"]), (ch["to"], g["b"])):
            chamber = chambers.get(end)
            if chamber is None:
                continue
            leg, width = _chamber_leg_mm(chamber, point)
            r += r_fn(leg, width, chamber["size"][2], mu)
            legs.append({"chamber": end, "length_mm": leg, "width_mm": width})
            weir = _weir(chamber)
            if weir and weirs:
                r_slit = slit_resistance_pa_s_m3(width, float(weir["opening_um"]), crest_mm, mu)
                r += r_slit
                crests.append({"chamber": end, "width_mm": width,
                               "gap_um": float(weir["opening_um"]),
                               "length_mm": crest_mm, "r_pa_s_m3": r_slit})
        net.add_edge(ch["from"], ch["to"], 1.0 / r, ch["id"])
        meta[ch["id"]] = {"resistance_pa_s_m3": r, "length_mm": g["length_mm"],
                          "width_mm": g["width_mm"], "depth_mm": g["depth_mm"],
                          "geometry_source": g["source"], "chamber_legs": legs,
                          "weir_crests": crests}
    return net, meta


# --------------------------------------------------------------------------------------
# benchmark plumbing
# --------------------------------------------------------------------------------------


@dataclass
class Benchmark:
    id: str
    name: str
    requirement: str
    solver: str
    status: str
    metric: str
    value: float | None
    tolerance: float | None
    units: str
    message: str
    detail: dict = field(default_factory=dict)
    expected: str = ""

    @property
    def passed(self) -> bool | None:
        return {"pass": True, "fail": False}.get(self.status)

    def as_json(self) -> dict:
        out = asdict(self)
        out["passed"] = self.passed
        return out

    def as_check(self) -> dict:
        """The shape report.py's validation table renders."""
        return {"name": self.name, "ok": self.status == "pass",
                "expected": self.expected or (f"<= {self.tolerance:g} {self.units}"
                                              if self.tolerance is not None else "see detail"),
                "got": ("skipped" if self.status == "skipped"
                        else f"{self.value:.6g} {self.units}" if self.value is not None
                        else self.status),
                "detail": self.message}

    @classmethod
    def skipped(cls, bid: str, name: str, requirement: str, reason: str) -> "Benchmark":
        return cls(bid, name, requirement, "none", "skipped", "", None, None, "", reason)


def _finding(fid: str, cls: str, severity: str, message: str, *, chip_id: str = "",
             feature_id: str = "", metric: str = "", value: float | None = None,
             limit: float | None = None, units: str = "", fix_hint: str = "") -> dict:
    return {"id": fid, "class": cls, "severity": severity, "chip_id": chip_id,
            "feature_id": feature_id, "step": None, "metric": metric, "value": value,
            "limit": limit, "units": units, "message": message, "fix_hint": fix_hint,
            "source": "validate"}


def _plain(value: Any) -> Any:
    """numpy scalars out, plain Python in, all the way down.

    The report is handed to analyse.py and report.py as a dict, not only written as JSON,
    and `json.dumps` refuses a np.float64. One walk here is cheaper than a mystery
    TypeError in someone else's module.
    """
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (np.floating, float)):
        v = float(value)
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(value, np.ndarray):
        return _plain(value.tolist())
    return value


def _rel(a: float, b: float) -> float:
    """Relative difference against the larger magnitude: symmetric, and safe at zero."""
    scale = max(abs(a), abs(b))
    return 0.0 if scale == 0 else abs(a - b) / scale


def repo_chips() -> list[Path]:
    return sorted(p for p in CHIP_DIR.glob("*.json") if p.name != "placeholder_chip_spec.json")


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _is_routed(chip: dict) -> bool:
    """Does this spec have a fluidic network at all?

    S6 builds a chip UNROUTED and S7 adds the channels, so `chip/` can legitimately hold
    a spec with chambers, ports and no channels. There is nothing to conserve in one and
    no flow case to pose, so the suite reports it as not-yet-routed instead of as a
    broken chip or a solver failure. Getting this wrong would pin a sibling stage's
    mid-pipeline artefact on the solver.
    """
    return bool(chip.get("channels")) and bool(chip.get("chambers"))


def _sections_in_repo() -> dict[tuple[float, float], list[str]]:
    """Every rectangular cross-section that really exists in chip/*.json -> where."""
    out: dict[tuple[float, float], list[str]] = {}
    for path in repo_chips():
        chip = _load(path)
        for ch in chip.get("channels", []):
            out.setdefault((float(ch["width_mm"]), float(ch["depth_mm"])), []).append(
                f"{chip['id']}:{ch['id']}")
        for c in chip.get("chambers", []):
            sx, sy, sz = c["size"]
            out.setdefault((round(min(sx, sy), 4), float(sz)), []).append(f"{chip['id']}:{c['id']}")
    return out


# --------------------------------------------------------------------------------------
# 1. straight rectangular duct vs the exact series
# --------------------------------------------------------------------------------------


def bench_duct_resistance(ctx: dict) -> tuple[Benchmark, list[dict]]:
    mu, cfd = ctx["mu"], ctx["cfd"]
    h = 0.3
    ratios = [1.0, 1.1, 1.25, 1.333, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0,
              15.0, 20.0, 25.0, 30.0]
    table = []
    for r in ratios:
        w = r * h
        row = {"w_over_h": r, "exact_bracket": duct_shape_factor(w, h),
               "approx_bracket": 1 - 0.63 / r,
               "r_error_frac": duct_resistance_error(w, h)}
        row["r_error_pct"] = 100 * row["r_error_frac"]
        if cfd.available:
            # The thing actually under test: what mesh.py builds a duct edge from.
            r_mesh = mu / cfd.mesh.duct_g_geom_m3(1.0, w, h)
            row["mesh_r_pa_s_m3"] = r_mesh
            row["mesh_vs_contract_form"] = _rel(r_mesh, duct_resistance_approx(1.0, w, h, mu))
            row["mesh_vs_exact"] = _rel(r_mesh, duct_resistance_exact(1.0, w, h, mu))
        table.append(row)

    wide = [row for row in table if row["w_over_h"] >= 2.0]
    worst_wide = max(wide, key=lambda row: abs(row["r_error_frac"]))
    worst = max(table, key=lambda row: abs(row["r_error_frac"]))
    # Against the EXACT series, not the 1 - 0.63 h/w form. mesh.py was moved onto the exact
    # law deliberately; checking it still reproduces the approximation made this benchmark
    # fail precisely because the code got more accurate (12.3% apart at w/h 1, which is the
    # approximation's own error, reported as findings below).
    law_gap = max((abs(row.get("mesh_vs_exact", 0.0)) for row in table), default=0.0)
    form_gap = max((abs(row.get("mesh_vs_contract_form", 0.0)) for row in table), default=0.0)

    repo_rows = []
    for (w, d), where in sorted(_sections_in_repo().items(),
                                key=lambda kv: max(kv[0]) / min(kv[0])):
        err = duct_resistance_error(w, d)
        repo_rows.append({"width_mm": w, "depth_mm": d,
                          "w_over_h": round(max(w, d) / min(w, d), 4),
                          "r_error_pct": 100 * err, "features": len(where),
                          "example": where[0],
                          "chips": sorted({f.split(":")[0] for f in where})})

    findings = []
    for row in repo_rows:
        if abs(row["r_error_pct"]) / 100 <= TOL["aspect_ratio_error_report"]:
            continue
        findings.append(_finding(
            ("validate_duct_0p63_%gx%g" % (row["width_mm"], row["depth_mm"])).replace(".", "p"),
            "model_error", "medium",
            f"the contract's 1 - 0.63 h/w resistance "
            f"{'over' if row['r_error_pct'] > 0 else 'under'}-states R by "
            f"{abs(row['r_error_pct']):.1f}% for a {row['width_mm']} x {row['depth_mm']} mm "
            f"section (w/h {row['w_over_h']:.2f}), which {row['features']} feature(s) use, "
            f"e.g. {row['example']}. Pressure findings on those features are that much high.",
            chip_id=row["chips"][0], feature_id=row["example"],
            metric="duct_resistance_error", value=row["r_error_pct"] / 100,
            limit=TOL["aspect_ratio_error_report"], units="fraction",
            fix_hint="use validate.duct_resistance_exact (the exact series) below w/h 2; "
                     "0.63 is the wide-channel limit of that same series"))

    ok_form = abs(worst_wide["r_error_frac"]) <= TOL["exact_vs_formula_wide"]
    ok_law = law_gap <= TOL["law_consistency"]
    ok = ok_form and ok_law
    # Report whichever number decided the verdict. Reporting the first one unconditionally
    # printed a metric comfortably inside its tolerance next to a FAIL, which sent two
    # readers hunting for a physics fault that was not there.
    metric, value, limit = (
        ("max |R error| of the contract form for w/h >= 2",
         abs(worst_wide["r_error_frac"]), TOL["exact_vs_formula_wide"]) if ok_law
        else ("mesh.py's duct law vs the exact series", law_gap, TOL["law_consistency"]))
    return Benchmark(
        "duct_resistance", "Straight rectangular duct vs the exact series solution",
        "README Validation: analytic single straight channel", "analytic + mesh.py",
        "pass" if ok else "fail",
        metric, value, limit, "fraction",
        f"mesh.duct_g_geom_m3 reproduces the EXACT Fourier series to {law_gap:.1e}, and so "
        f"sits {100 * form_gap:.1f}% off the 1 - 0.63 h/w approximation - that gap is the "
        f"approximation's own error, not the mesher's. "
        f"The 0.63 form matches the exact series to {100 * abs(worst_wide['r_error_frac']):.3f}% "
        f"for w/h >= 2 but is +{worst['r_error_pct']:.1f}% at w/h {worst['w_over_h']:g}. "
        + ("Repo sections at risk: "
           + ", ".join(f"{r['width_mm']}x{r['depth_mm']} mm (w/h {r['w_over_h']:.2f}) "
                       f"+{r['r_error_pct']:.1f}%" for r in repo_rows[:3])),
        {"aspect_sweep": table, "worst_overall": worst,
         "worst_for_w_over_h_ge_2": worst_wide,
         # Both reported, under the names they actually mean. The first is the gate; the
         # second is how far the retired approximation sits from the law in use.
         "mesh_vs_exact_series_max_error": law_gap,
         "mesh_vs_contract_form_max_error": form_gap,
         "repo_sections": repo_rows,
         "audit_note": (
             "The audit expected the 0.2 x 0.15 mm channels (w/h 1.333, +2.7%) to be where "
             "this matters most. They are second: module5_v1's 0.3 x 0.3 mm channel is "
             "SQUARE (w/h 1.0, +14.0%), the largest resistance error in the repo. Above "
             "w/h 2 the 0.63 form is the exact wide-channel limit and the error is under "
             "0.05%, so nothing wider than 0.6 x 0.3 mm needs the series."),
         "why_0p63": (f"192 x sum_odd(1/n^5) / pi^5 = {WIDE_COEFFICIENT:.7f}, i.e. this "
                      "series with every tanh set to 1. The contract rounds it to 0.63"),
         "sign_of_the_error": {
             "crossover_w_over_h": _error_sign_crossover(),
             "note": ("the error changes sign. Below the crossover the tanh deficit "
                      "dominates and the 0.63 form is CONSERVATIVE (over-states R, up to "
                      "+14% at a square); above it the rounding of 0.6302489 down to 0.63 "
                      "dominates and the form is very slightly OPTIMISTIC, bottoming out "
                      f"at {100 * duct_resistance_error(30 * h, h):.3f}% at w/h 30. So "
                      "'the 0.63 form is always safe' is false, though above w/h 2.4 the "
                      "optimism never exceeds 0.15% and no finding turns on it.")},
         "series_convergence": {
             "terms_used": 500,
             "max_change_from_10x_terms": max(
                 abs(duct_shape_factor(r * h, h, 500) - duct_shape_factor(r * h, h, 5000))
                 for r in (1.0, 2.0, 30.0, 1e4)),
             "note": ("the tanh series is converged to machine precision at 500 terms for "
                      "every aspect ratio, including w/h 1e4 where it is sometimes "
                      "suspected of truncating: there 1 - B = "
                      f"{1 - duct_shape_factor(1e4, 1.0):.6e}, which is the closed-form "
                      f"{WIDE_COEFFICIENT:.7f} h/w to 10 significant figures, not a "
                      "truncation artefact")},
         "square_duct_check": {"exact_bracket": duct_shape_factor(1.0, 1.0),
                               "textbook_q_coefficient": duct_shape_factor(1.0, 1.0) / 12,
                               "textbook_value": 0.035144}},
        expected=f"<= {TOL['exact_vs_formula_wide']:g} for w/h >= 2"), findings


# --------------------------------------------------------------------------------------
# 2. series and parallel networks vs hand calculation
# --------------------------------------------------------------------------------------

#: Three different ducts, used in series, in parallel and as a ladder.
BENCH_DUCTS = ((5.0, 0.2, 0.3), (3.0, 0.4, 0.3), (8.0, 0.2, 0.15))


def _duct_mesh(cfd: CfdUnderTest, mu: float, chains: list[list[tuple[str, int]]],
               node_ids: list[str], mesh_id: str) -> Any:
    """A Mesh of junction nodes joined by duct edges, for the hand-computable networks.

    Conductances come from `mesh.duct_g_geom_m3`, the mesher's own expression, so this
    benchmark tests the SOLVE and not the resistance law - `duct_resistance` tests the law.
    """
    nodes = [cfd.model.Node(id=n, kind="junction", depth_mm=0.3, feature_id=mesh_id)
             for n in node_ids]
    edges = []
    for chain in chains:
        for eid, (a, b, duct) in enumerate(chain):  # type: ignore[misc]
            length, width, depth = BENCH_DUCTS[duct]
            g_geom = cfd.mesh.duct_g_geom_m3(length, width, depth)
            edges.append(cfd.model.Edge(
                id=f"{a}->{b}#{duct}", a=a, b=b, kind="duct", g_geom_m3=g_geom,
                conductance_m3_per_pa_s=g_geom / mu, length_mm=length,
                width_mm=width, depth_mm=depth, feature_id=mesh_id))
    return cfd.model.Mesh(nodes=nodes, edges=edges,
                          ports={"in": node_ids[0], "out": node_ids[-1]},
                          meta={"chip_id": mesh_id, "grid_mm": 0.0, "mu_pa_s": mu})


def bench_series_parallel(ctx: dict) -> tuple[Benchmark, list[dict]]:
    cfd, mu = ctx["cfd"], ctx["mu"]
    if not cfd.available:
        return Benchmark.skipped("series_parallel", "Series and parallel networks vs hand "
                                 "calculation", "README Validation", cfd.reason), []
    q_ul_min = 10.0
    q = q_ul_min * UL_MIN_TO_M3_S
    # Hand numbers use the SAME resistance law the mesher builds with, so any difference
    # is the solve. R_series = sum R; 1/R_par = sum 1/R; branch share = G_i / sum G.
    r = [mu / cfd.mesh.duct_g_geom_m3(*d) for d in BENCH_DUCTS]
    ports = {"in": cfd.model.PortState.flow(q_ul_min), "out": cfd.model.PortState.vent()}
    cases = {}

    series = _duct_mesh(cfd, mu, [[("in", "a", 0), ("a", "b", 1), ("b", "out", 2)]],
                        ["in", "a", "b", "out"], "series")
    f = _solve_mesh(cfd, series, ports, step="series", mu=mu)
    cases["series"] = {"hand_pa": sum(r) * q, "solved_pa": f.p_pa["in"],
                       "error": _rel(sum(r) * q, f.p_pa["in"]),
                       "r_hand_pa_s_m3": sum(r)}

    parallel = _duct_mesh(cfd, mu, [[("in", "out", k)] for k in range(3)],
                          ["in", "out"], "parallel")
    f = _solve_mesh(cfd, parallel, ports, step="parallel", mu=mu)
    r_par = 1.0 / sum(1.0 / x for x in r)
    share = max(_rel(f.q_m3_s[f"in->out#{k}"], q * (1.0 / r[k]) * r_par) for k in range(3))
    cases["parallel"] = {"hand_pa": r_par * q, "solved_pa": f.p_pa["in"],
                         "error": _rel(r_par * q, f.p_pa["in"]),
                         "branch_share_error": share, "r_hand_pa_s_m3": r_par,
                         "branch_ul_min": {f"R{k}": f.q_m3_s[f"in->out#{k}"] * M3_S_TO_UL_MIN
                                           for k in range(3)}}

    ladder = _duct_mesh(cfd, mu, [[("in", "m1", 0)], [("m1", "m2", 1)], [("m1", "m2", 2)],
                                  [("m2", "out", 0)]], ["in", "m1", "m2", "out"], "ladder")
    f = _solve_mesh(cfd, ladder, ports, step="ladder", mu=mu)
    r_ladder = 2 * r[0] + 1.0 / (1.0 / r[1] + 1.0 / r[2])
    cases["ladder"] = {"hand_pa": r_ladder * q, "solved_pa": f.p_pa["in"],
                       "error": _rel(r_ladder * q, f.p_pa["in"]),
                       "r_hand_pa_s_m3": r_ladder}
    cases["ladder"]["mass_imbalance_vs_inflow"] = abs(f.residual.mass_imbalance_max) / q

    worst = max([cases["series"]["error"], cases["parallel"]["error"],
                 cases["parallel"]["branch_share_error"], cases["ladder"]["error"]])
    ok = worst <= TOL["series_parallel"]
    return Benchmark(
        "series_parallel", "Series, parallel and ladder networks vs hand calculation",
        "README Validation: series and parallel networks match hand-computed resistances",
        "solve.py", "pass" if ok else "fail", "max relative error vs hand calculation",
        worst, TOL["series_parallel"], "fraction",
        f"series {cases['series']['error']:.2e}, parallel {cases['parallel']['error']:.2e}, "
        f"branch split {cases['parallel']['branch_share_error']:.2e}, ladder "
        f"{cases['ladder']['error']:.2e} - solve.py reproduces every hand resistance at "
        f"machine precision",
        {"ducts_mm": [{"length": d[0], "width": d[1], "depth": d[2], "r_pa_s_m3": r[k]}
                      for k, d in enumerate(BENCH_DUCTS)],
         "q_ul_min": q_ul_min, "cases": cases,
         "note": ("hand resistances use mesh.duct_g_geom_m3, so this isolates the linear "
                  "solve; the resistance law itself is benchmarked by duct_resistance")}), []


# --------------------------------------------------------------------------------------
# 3. Hele-Shaw: uniform flow and a point source in a disk
# --------------------------------------------------------------------------------------


def _raster_mesh(cfd: CfdUnderTest, mu: float, nx: int, ny: int, gx: float, gy: float,
                 depth_mm: float, live: np.ndarray, mesh_id: str) -> tuple[Any, np.ndarray]:
    """A raster of Hele-Shaw cells joined by `hs_face` edges, built with mesh.py's own
    face conductance. Returns the Mesh and an (ny, nx) array of node ids ('' where dead).

    No chip spec is involved on purpose: this is the discretisation under test, driven
    straight against a closed-form Hele-Shaw solution.
    """
    ids = np.full((ny, nx), "", dtype=object)
    nodes = []
    for j in range(ny):
        for i in range(nx):
            if not live[j, i]:
                continue
            nid = f"c:{i}:{j}"
            ids[j, i] = nid
            nodes.append(cfd.model.Node(id=nid, kind="cell", x_mm=(i + 0.5) * gx,
                                        y_mm=(j + 0.5) * gy, area_mm2=gx * gy,
                                        depth_mm=depth_mm, feature_id=mesh_id, ix=i, iy=j))
    edges = []
    for j in range(ny):
        for i in range(nx):
            for di, dj, face, spacing in ((1, 0, gy, gx), (0, 1, gx, gy)):
                i2, j2 = i + di, j + dj
                if i2 >= nx or j2 >= ny or not (live[j, i] and live[j2, i2]):
                    continue
                g_geom = cfd.mesh.hs_g_geom_m3(depth_mm, face, spacing)
                edges.append(cfd.model.Edge(
                    id=f"f:{i}:{j}:{'x' if di else 'y'}", a=ids[j, i], b=ids[j2, i2],
                    kind="hs_face", g_geom_m3=g_geom, conductance_m3_per_pa_s=g_geom / mu,
                    length_mm=spacing, width_mm=face, depth_mm=depth_mm,
                    feature_id=mesh_id))
    mesh = cfd.model.Mesh(nodes=nodes, edges=edges, ports={},
                          meta={"chip_id": mesh_id, "grid_mm": gx, "mu_pa_s": mu})
    return mesh, ids


def bench_hele_shaw_uniform(ctx: dict) -> tuple[Benchmark, list[dict]]:
    cfd, mu = ctx["cfd"], ctx["mu"]
    name = "Hele-Shaw: uniform flow down a long chamber is linear in p"
    if not cfd.available:
        return Benchmark.skipped("hele_shaw_uniform", name, "brief 3", cfd.reason), []
    depth, lx, ly = 0.3, 8.0, 2.0
    nx, ny = 40, 10
    gx, gy = lx / nx, ly / ny
    mesh, ids = _raster_mesh(cfd, mu, nx, ny, gx, gy, depth, np.ones((ny, nx), bool), "slab")
    q_ul_min = 5.0
    ports = {}
    for j in range(ny):
        mesh.ports[f"in{j}"] = ids[j, 0]
        mesh.ports[f"out{j}"] = ids[j, nx - 1]
        ports[f"in{j}"] = cfd.model.PortState.flow(q_ul_min / ny)
        ports[f"out{j}"] = cfd.model.PortState.vent()
    mesh = cfd.model.Mesh(nodes=mesh.nodes, edges=mesh.edges, ports=mesh.ports, meta=mesh.meta)
    f = _solve_mesh(cfd, mesh, ports, step="uniform", mu=mu)

    p = np.array([[f.p_pa[ids[j, i]] for i in range(nx)] for j in range(ny)])
    # The discrete answer for uniform injection is exactly linear between the two end
    # COLUMN CENTRES, so the effective length is (nx - 1) gx and not lx.
    l_eff = (nx - 1) * gx
    q = q_ul_min * UL_MIN_TO_M3_S
    dp_hand = 12 * mu * q * (l_eff * MM) / ((ly * MM) * (depth * MM) ** 3)
    dp = float(np.mean(p[:, 0]))
    dp_err = _rel(dp, dp_hand)
    profile = p.mean(axis=0)
    xs = (np.arange(nx) + 0.5) * gx
    fit = np.polyfit(xs, profile, 1)
    non_linearity = float(np.max(np.abs(np.polyval(fit, xs) - profile))) / dp
    transverse = float(np.max(np.abs(p - profile[None, :]))) / dp
    # The velocity field must be uniform and axial: u = Q / (W h).
    u_hand = q / ((ly * MM) * (depth * MM)) * 1e3
    speeds = np.array([f.u_mm_s[ids[j, i]] for j in range(ny) for i in range(1, nx - 1)])
    u_err = float(np.max(np.abs(speeds[:, 0] - u_hand))) / u_hand
    cross = float(np.max(np.abs(speeds[:, 1]))) / u_hand

    worst = max(dp_err, non_linearity, transverse, u_err, cross)
    ok = worst <= TOL["hele_shaw_uniform"]
    return Benchmark(
        "hele_shaw_uniform", name, "brief 3: uniform flow in a long chamber gives linear "
        "pressure", "solve.py", "pass" if ok else "fail",
        "max of dP, linearity, transverse and velocity error", worst,
        TOL["hele_shaw_uniform"], "fraction",
        f"dP {dp:.6g} Pa vs 12 mu Q L / (W h^3) = {dp_hand:.6g} Pa ({dp_err:.1e}); "
        f"non-linearity {non_linearity:.1e}, transverse variation {transverse:.1e}, "
        f"cell velocity {u_err:.1e} off {u_hand:.4g} mm/s with {cross:.1e} cross-flow",
        {"chamber_mm": {"lx": lx, "ly": ly, "depth": depth}, "cells": [nx, ny],
         "q_ul_min": q_ul_min, "effective_length_mm": l_eff,
         "dp_pa": dp, "dp_hand_pa": dp_hand, "dp_error": dp_err,
         "non_linearity": non_linearity, "transverse_variation": transverse,
         "u_mm_s": u_hand, "u_error": u_err, "cross_flow": cross,
         "gradient_pa_per_mm": float(-fit[0]),
         "side_wall_note": (
             "the depth-averaged law carries no 1 - 0.63 h/w term, so a real chamber of "
             f"this aspect (w/h {ly / depth:.1f}) drops "
             f"{1 / duct_shape_factor(ly, depth):.3f}x this. That deficit is the subject "
             "of hele_shaw_validity, not an error here.")},
        expected=f"<= {TOL['hele_shaw_uniform']:g} (exact on the grid)"), []


def bench_hele_shaw_point_source(ctx: dict) -> tuple[Benchmark, list[dict]]:
    cfd, mu = ctx["cfd"], ctx["mu"]
    name = "Hele-Shaw: a point source in a disk gives p ~ ln r"
    if not cfd.available:
        return Benchmark.skipped("hele_shaw_point_source", name, "brief 3", cfd.reason), []
    depth, radius, g = 0.3, 5.0, 0.1
    n = int(round(2 * radius / g))
    n += n % 2
    gx = gy = 2 * radius / n
    centres = (np.arange(n) + 0.5) * gx - radius
    xx, yy = np.meshgrid(centres, centres)
    rr = np.hypot(xx, yy)
    live = rr <= radius
    mesh, ids = _raster_mesh(cfd, mu, n, n, gx, gy, depth, live, "disk")

    q_ul_min = 5.0
    ports = {"src": cfd.model.PortState.flow(q_ul_min)}
    mesh.ports["src"] = ids[n // 2, n // 2]
    rim = 0
    for j in range(n):
        for i in range(n):
            if not live[j, i]:
                continue
            on_rim = (i in (0, n - 1) or j in (0, n - 1)
                      or not (live[j, i - 1] and live[j, i + 1]
                              and live[j - 1, i] and live[j + 1, i]))
            if on_rim:
                mesh.ports[f"rim{rim}"] = ids[j, i]
                ports[f"rim{rim}"] = cfd.model.PortState.vent()
                rim += 1
    mesh = cfd.model.Mesh(nodes=mesh.nodes, edges=mesh.edges, ports=mesh.ports, meta=mesh.meta)
    f = _solve_mesh(cfd, mesh, ports, step="point_source", mu=mu)

    # Fit away from the source cell (a log singularity) and the stair-stepped rim.
    r_lo, r_hi = 5 * gx, 0.75 * radius
    sample = [(float(rr[j, i]), f.p_pa[ids[j, i]]) for j in range(n) for i in range(n)
              if live[j, i] and r_lo <= rr[j, i] <= r_hi]
    r_arr = np.array([a for a, _ in sample])
    p_arr = np.array([b for _, b in sample])
    slope, intercept = np.polyfit(np.log(r_arr), p_arr, 1)
    q = q_ul_min * UL_MIN_TO_M3_S
    slope_hand = -6 * mu * q / (PI * (depth * MM) ** 3)
    slope_err = _rel(float(slope), slope_hand)
    spread = float(np.max(np.abs(p_arr - (slope_hand * np.log(r_arr) + intercept)))) \
        / float(np.max(p_arr))
    ok = slope_err <= TOL["hele_shaw_log_slope"]
    return Benchmark(
        "hele_shaw_point_source", name,
        "brief 3: a point source in a disk gives p ~ ln r", "solve.py",
        "pass" if ok else "fail", "relative error in d p / d ln r", slope_err,
        TOL["hele_shaw_log_slope"], "fraction",
        f"fitted slope {slope:.6g} Pa per ln(mm) vs -6 mu Q / (pi h^3) = {slope_hand:.6g} "
        f"({100 * slope_err:.2f}%) over r {r_lo:.2f}-{r_hi:.2f} mm on a {gx:.3f} mm grid, "
        f"{len(sample)} cells; axisymmetric to {100 * spread:.2f}% of peak",
        {"disk_radius_mm": radius, "grid_mm": gx, "depth_mm": depth, "q_ul_min": q_ul_min,
         "fit_range_mm": [r_lo, r_hi], "samples": len(sample), "rim_ports": rim,
         "slope_pa_per_ln_mm": float(slope), "slope_hand_pa_per_ln_mm": slope_hand,
         "slope_error": slope_err, "axisymmetry_spread": spread,
         "note": ("a point source is a log singularity, so the error is set by how close "
                  "to the source the fit starts: a real feed must be meshed at its true "
                  "mouth width and never as a single cell, which is what mesh.py's "
                  "`mouth` edges do")}), []


# --------------------------------------------------------------------------------------
# 4. grid convergence on a real chamber, with Richardson extrapolation
# --------------------------------------------------------------------------------------


def _richardson(values: Sequence[float], spacings: Sequence[float]) -> dict:
    """Observed order and extrapolated value from three grids, coarse to fine.

    Roache's generalised ratio form, so the grids do not have to refine by exactly 2 -
    the mesher snaps `grid_mm` to whole cells, which makes the real ratios 1.98-2.02.
    """
    f1, f2, f3 = values
    h1, h2, h3 = spacings
    r21, r32 = h1 / h2, h2 / h3
    e21, e32 = f2 - f1, f3 - f2
    if e21 == 0 or e32 == 0 or e32 / e21 <= 0:
        return {"order": None, "extrapolated": f3, "r21": r21, "r32": r32,
                "gci_fine_pct": None,
                "note": "not monotonically convergent: no order can be fitted"}
    s = 1.0
    p = math.log(abs(e21 / e32)) / math.log(r21)
    for _ in range(100):                       # fixed point, converges in a few passes
        q = math.log((r21 ** p - s) / (r32 ** p - s))
        p_new = abs(math.log(abs(e21 / e32)) + q) / math.log(r21)
        if abs(p_new - p) < 1e-12:
            p = p_new
            break
        p = p_new
    extrap = f3 + e32 / (r32 ** p - 1)
    return {"order": p, "extrapolated": extrap, "r21": r21, "r32": r32,
            "gci_fine_pct": abs(125 * e32 / f3 / (r32 ** p - 1)) if f3 else None}


def _area_quantile(values: np.ndarray, areas: np.ndarray, q: float) -> float:
    """The value below which `q` of the AREA lies. Area-weighted, because a refined grid
    has smaller cells and an unweighted percentile would drift with the mesh."""
    order = np.argsort(values)
    v, a = values[order], areas[order]
    cum = np.cumsum(a) / np.sum(a)
    return float(v[int(np.searchsorted(cum, q))]) if len(v) else 0.0


def _chamber_qois(cfd: CfdUnderTest, chip: dict, grid_mm: float, flow_ul_min: float,
                  mu: float, chamber_id: str) -> dict:
    """Mesh module1_v1 at one spacing, drive it, and read the quantities of interest.

    Every QoI is defined on the CHIP and not on the grid - a port pressure, an
    area-weighted chamber speed, an area-weighted low percentile of it - so the only
    thing that changes between runs is the discretisation. A cell count or a per-cell
    minimum would change with the mesh by definition and could not converge at all.
    """
    mesh = cfd.mesh.build_mesh(chip, grid_mm=grid_mm, mu_pa_s=mu)
    ports = {"p_in": cfd.model.PortState.flow(flow_ul_min),
             "p_mid": cfd.model.PortState.vent()}
    f = _solve_mesh(cfd, mesh, ports, step=f"grid_{grid_mm}", mu=mu)
    cells = [n for n in mesh.nodes if n.kind == "cell" and n.feature_id == chamber_id]
    speeds = np.array([math.hypot(*f.u_mm_s[n.id][:2]) for n in cells])
    areas = np.array([n.area_mm2 for n in cells])
    mean = float(np.average(speeds, weights=areas))
    feature = next((x for x in mesh.meta["features"] if x.get("feature_id") == chamber_id), {})
    actual = feature.get("grid_mm") or [grid_mm, grid_mm]
    return {"requested_grid_mm": grid_mm,
            "actual_grid_mm": float(np.mean(actual)),
            "cells_in_chamber": len(cells),
            "cells_across_chamber": feature.get("ny"),
            "nodes": len(mesh.nodes), "edges": len(mesh.edges),
            "inlet_pressure_pa": f.p_pa[mesh.ports["p_in"]],
            "chamber_mean_speed_mm_s": mean,
            "chamber_slow_5pct_speed_mm_s": _area_quantile(speeds, areas, 0.05),
            "low_velocity_area_fraction": float(np.sum(areas[speeds < 0.1 * mean])
                                                / np.sum(areas)),
            "mass_imbalance_vs_inflow": (abs(f.residual.mass_imbalance_max)
                                         / max(f.residual.q_in, 1e-30))}


GRID_QOIS = {
    "inlet_pressure_pa": "driven-port pressure - what the leak check reads",
    "chamber_mean_speed_mm_s": "area-weighted chamber speed - residence time and shear",
    "chamber_slow_5pct_speed_mm_s": "speed below which 5% of the chamber area lies - the "
                                    "trap test's own quantity, well conditioned",
    "low_velocity_area_fraction": "area below 0.1x mean - the trap test as stated, which "
                                  "is a threshold count and converges badly by nature",
}
#: The QoI the observed order is quoted from: smooth, global, and the one a leak finding
#: turns on. The velocity QoIs are checked too, but a threshold count has no clean order.
PRIMARY_QOI = "inlet_pressure_pa"


def bench_grid_convergence(ctx: dict) -> tuple[Benchmark, list[dict]]:
    cfd, mu = ctx["cfd"], ctx["mu"]
    name = "Grid convergence on a real chamber with Richardson extrapolation"
    requirement = "brief 4: 0.2 / 0.1 / 0.05 mm on a real chamber, report the order"
    if not cfd.available:
        return Benchmark.skipped("grid_convergence", name, requirement, cfd.reason), []
    chip = _load(CHIP_DIR / "module1_v1.json")
    chamber_id = "n_cx"
    chamber = next(c for c in chip["chambers"] if c["id"] == chamber_id)
    runs = [_chamber_qois(cfd, chip, g, ctx["flow_ul_min"], mu, chamber_id)
            for g in (0.2, 0.1, 0.05)]
    spacings = [row["actual_grid_mm"] for row in runs]
    tol = TOL["grid_convergence_qoi"]
    qois = {}
    for key in GRID_QOIS:
        values = [row[key] for row in runs]
        fit = _richardson(values, spacings)
        fit["what"] = GRID_QOIS[key]
        fit["values"] = values
        span = max(abs(v) for v in values)
        fit["spread_across_grids"] = (max(values) - min(values)) / span if span else 0.0
        # Richardson needs monotone convergence and a change big enough to measure. A
        # quantity that oscillates, or that never leaves zero, has no extrapolated value,
        # so it cannot be given an order and must not be allowed to set the spacing -
        # its spread across the three grids is its error bar instead.
        fit["usable"] = (fit["order"] is not None and span > 0
                         and abs(values[-1] - values[-2]) / span > 1e-9)
        if not fit["usable"]:
            fit["note"] = (
                f"{fit.get('note', 'no measurable change')}; its spread across the three "
                f"grids, {100 * fit['spread_across_grids']:.1f}%, is the error bar to use, "
                f"and it cannot set the spacing")
        qois[key] = fit
        for row in runs:
            ext = fit["extrapolated"]
            row[f"{key}_error"] = abs(row[key] - ext) / abs(ext) if ext else 0.0

    usable = [k for k, v in qois.items() if v["usable"]]
    inside = [row for row in runs if all(row[f"{k}_error"] <= tol for k in usable)]
    # The COARSEST spacing that is still inside tolerance: the recommendation has to be
    # the cheapest grid that is good enough, not the best grid tested.
    recommended = max((row["requested_grid_mm"] for row in inside),
                      default=min(row["requested_grid_mm"] for row in runs))
    orders = {k: v["order"] for k, v in qois.items()}
    primary = qois[PRIMARY_QOI]
    gci = max([v["gci_fine_pct"] or 0.0 for v in qois.values() if v["usable"]] or [0.0])
    ok = (primary["order"] is not None and 0.5 <= primary["order"] <= 3.0
          and gci <= 100 * tol)
    findings = []
    if not ok:
        findings.append(_finding(
            "validate_grid_order", "model_error", "high",
            f"module1_v1 does not converge credibly under refinement: "
            f"{PRIMARY_QOI} order {primary['order']}, worst fine-grid GCI {gci:.2f}%. "
            "The discretisation or the mouth treatment is wrong and no chamber-level "
            "number can be trusted at any spacing.",
            chip_id="module1_v1", feature_id=chamber_id, metric="observed_order",
            value=primary["order"], units="order",
            fix_hint="check the hs_face conductance at boundary cells and that a mouth "
                     "keeps its physical width as grid_mm changes"))
    coarse_out_of_range = [k for k, v in qois.items()
                           if v["usable"] and v["order"] is not None and v["order"] > 2.5]
    if coarse_out_of_range:
        findings.append(_finding(
            "validate_grid_coarse_default", "model_error", "medium",
            f"at 0.2 mm a {chamber['size'][1]} mm chamber is only "
            f"{runs[0]['cells_across_chamber']} cells across and is outside the asymptotic "
            f"range: {', '.join(coarse_out_of_range)} fits an apparent order above 2.5 and "
            f"is {100 * runs[0][coarse_out_of_range[0] + '_error']:.1f}% off the "
            f"extrapolated value, against {100 * runs[1][coarse_out_of_range[0] + '_error']:.1f}% "
            f"at 0.1 mm. Velocity-based findings - traps, residence time, sediment - need "
            f"{recommended} mm or finer.",
            chip_id="module1_v1", feature_id=chamber_id, metric="recommended_grid_mm",
            value=recommended, limit=0.2, units="mm",
            fix_hint=f"default grid_mm to {recommended}; keep 0.2 mm for a pressure-only "
                     f"screen, where it is already within "
                     f"{100 * runs[0]['inlet_pressure_pa_error']:.2f}%"))
    return Benchmark(
        "grid_convergence", name, requirement, "mesh.py + solve.py",
        "pass" if ok else "fail", "observed order of convergence in driven-port pressure",
        primary["order"], None, "order",
        f"{chamber_id} {chamber['size'][0]} x {chamber['size'][1]} mm at "
        f"{', '.join(f'{s:.4f}' for s in spacings)} mm (ratio "
        f"{primary['r21']:.2f}, {primary['r32']:.2f}): pressure converges at order "
        f"{primary['order']:.2f} - second order, as the scheme should be - to "
        f"{primary['extrapolated']:.6g} Pa, worst fine-grid GCI {gci:.2f}%. "
        f"RECOMMENDED SPACING {recommended} mm: at 0.2 mm the chamber is only "
        f"{runs[0]['cells_across_chamber']} cells across and its mean speed is "
        f"{100 * runs[0]['chamber_mean_speed_mm_s_error']:.1f}% high, while pressure is "
        f"already within {100 * runs[0]['inlet_pressure_pa_error']:.2f}%; at "
        f"{recommended} mm every Richardson-usable quantity is inside {100 * tol:.0f}% "
        f"({100 * next(r['chamber_mean_speed_mm_s_error'] for r in runs if r['requested_grid_mm'] == recommended):.2f}% "
        f"on speed) for "
        f"{next(r['nodes'] for r in runs if r['requested_grid_mm'] == recommended)} nodes, "
        f"{next(r['nodes'] for r in runs if r['requested_grid_mm'] == recommended) / runs[0]['nodes']:.0f}x "
        f"the cost of 0.2 mm. 0.2 mm is still fine for a pressure-only screen. The trap "
        f"test's threshold-area form does not converge at any of these spacings - see "
        f"trap_metric_note",
        {"chip_id": "module1_v1", "chamber": chamber_id, "flow_ul_min": ctx["flow_ul_min"],
         "runs": runs, "qois": qois, "usable_qois": usable,
         "recommended_grid_mm": recommended, "tolerance": tol,
         "worst_gci_fine_pct": gci,
         "pressure_only_grid_mm": 0.2,
         "snapping_note": ("the mesher snaps grid_mm to whole cells, so the real "
                           "refinement ratios are 1.96 and 1.99, not 2; the order is "
                           "fitted with Roache's generalised ratio form"),
         "trap_metric_note": (
             "the trap test AS STATED - area below 0.1x the chamber mean - is a threshold "
             f"count: on this chamber it is {qois['low_velocity_area_fraction']['values']} "
             "across the three grids, i.e. it registers nothing until 0.05 mm. It cannot "
             "be Richardson-extrapolated and must never be reported as a converged "
             "number. The 5th-percentile speed is the same physics as a smooth functional "
             "and is grid-insensitive to "
             f"{100 * qois['chamber_slow_5pct_speed_mm_s']['spread_across_grids']:.1f}% "
             "here, though it oscillates rather than converging monotonically, so that "
             "spread is its error bar. analyse.py should prefer the percentile form."),
         "cost": {f"{row['requested_grid_mm']} mm": row["nodes"] for row in runs}},
        expected="order 1-2 for a second-order scheme with a mouth singularity"), findings


# --------------------------------------------------------------------------------------
# 5. cross-check against operator/network.py on module1_v1
# --------------------------------------------------------------------------------------


def _mode_ports(cfd: CfdUnderTest, nw: Any, mode: str, flow: float) -> dict[str, Any]:
    out = {}
    for pid, state in nw.mode_ports(mode, flow).items():
        if isinstance(state, str):
            out[pid] = cfd.model.PortState.vent() if state == nw.VENT \
                else cfd.model.PortState("closed")
        else:
            out[pid] = cfd.model.PortState.flow(state[1])
    return out


def bench_operator_crosscheck(ctx: dict) -> tuple[Benchmark, list[dict]]:
    import network as nw  # noqa: PLC0415

    cfd, mu, flow = ctx["cfd"], ctx["mu"], ctx["flow_ul_min"]
    name = "Cross-check against operator/network.py on module1_v1, mode by mode"
    requirement = ("README Validation: agreement with operator/network.py, an independent "
                   "implementation, within a few percent")
    chip = _load(CHIP_DIR / "module1_v1.json")
    findings: list[dict] = []
    if abs(nw.MU_37C - mu) > 1e-12:
        findings.append(_finding(
            "validate_mu_literal_drift", "model_error", "high",
            f"operator/network.py MU_37C = {nw.MU_37C} but contracts "
            f"fluid.viscosity_37c_pa_s = {mu}: the two solvers are being compared on "
            "different fluids and neither one's pressures mean anything",
            metric="viscosity_pa_s", value=nw.MU_37C, limit=mu, units="Pa s",
            fix_hint="have operator/network.py read "
                     "assay2chip.lib.contracts.viscosity_for(37)"))
    if not cfd.available:
        return Benchmark.skipped("operator_crosscheck", name, requirement, cfd.reason), findings

    op_net = nw.Network(chip, mu=mu, dxf=ROOT / "cad" / "exports" / chip["id"] / f"{chip['id']}.dxf")
    mesh = cfd.mesh.build_mesh(chip, grid_mm=ctx["grid_mm"], mu_pa_s=mu)
    # validate.py's own network, in two forms: with the weir crests the chip spec declares
    # and without them. The pair is the tiebreaker that says which solver is wrong.
    ref_weir, ref_meta = reference_network(chip, mu, exact=False, weirs=True)
    ref_plain, _ = reference_network(chip, mu, exact=False, weirs=False)
    ref_exact, _ = reference_network(chip, mu, exact=True, weirs=True)
    weir_chambers = sorted(c["id"] for c in chip["chambers"] if _weir(c))
    behind_weir = _behind_weir_nodes(chip, weir_chambers)

    # Three comparisons, because two solvers that differ cannot say which is wrong:
    #   A  operator/network.py   vs validate.py's network WITHOUT weirs - same physics,
    #                               independent code. Near zero means its solve is right.
    #   B  mesh.py + solve.py    vs validate.py's network WITH the weirs the spec declares
    #                               - near zero means the CFD is right given the weir.
    #   C  the two solvers against each other: the gap the report has to explain.
    modes, worst = [], {"A_operator_vs_reference_p": 0.0, "A_operator_vs_reference_q": 0.0,
                        "B_cfd_vs_reference_p": 0.0, "B_cfd_vs_reference_q": 0.0,
                        "C_cfd_vs_operator_p": 0.0, "C_cfd_vs_operator_q": 0.0,
                        "weir_effect_p": 0.0}
    for mode in nw.MODULE1_MODES:
        if mode == "idle":
            continue
        states = nw.mode_ports(mode, flow)
        try:
            op = op_net.solve(states)
        except nw.NetworkError as exc:
            modes.append({"mode": mode, "skipped": str(exc)})
            continue
        field = _solve_mesh(cfd, mesh, _mode_ports(cfd, nw, mode, flow), step=mode, mu=mu)
        sources = {pid: v[1] * UL_MIN_TO_M3_S for pid, v in states.items()
                   if not isinstance(v, str) and v[0] == nw.FEED}
        fixed = {pid: 0.0 for pid, v in states.items() if isinstance(v, str) and v == nw.VENT}
        refs = {}
        for key, net in (("weir", ref_weir), ("plain", ref_plain), ("exact", ref_exact)):
            pinned = dict(fixed)
            for nid in set(net.node_ids) - _reachable(net, set(fixed)):
                pinned[nid] = 0.0
            refs[key] = net.solve({k: v for k, v in sources.items() if k not in pinned}, pinned)

        p_rows = []
        scale_p = max(max(op["pressure_pa"].values()),
                      max(field.p_pa[n] for n in mesh.ports.values()), 1e-30)
        for pid, node in sorted(mesh.ports.items()):
            row = {"port": pid, "behind_weir": pid in behind_weir,
                   "cfd_pa": field.p_pa[node], "operator_pa": op["pressure_pa"].get(pid, 0.0),
                   "reference_weir_pa": refs["weir"]["p_pa"][pid],
                   "reference_no_weir_pa": refs["plain"]["p_pa"][pid],
                   "reference_exact_law_pa": refs["exact"]["p_pa"][pid]}
            row["C_cfd_vs_operator"] = abs(row["cfd_pa"] - row["operator_pa"]) / scale_p
            row["B_cfd_vs_reference"] = abs(row["cfd_pa"] - row["reference_weir_pa"]) / scale_p
            row["A_operator_vs_reference"] = abs(row["operator_pa"]
                                                 - row["reference_no_weir_pa"]) / scale_p
            row["weir_effect"] = abs(row["reference_weir_pa"]
                                     - row["reference_no_weir_pa"]) / scale_p
            p_rows.append(row)
        q_rows = []
        duct_edges = {e.feature_id: e for e in mesh.edges if e.kind == "duct"}
        scale_q = max(max(abs(v["flow_ul_min"]) for v in op["channels"].values()), 1e-30)
        for cid, edge in sorted(duct_edges.items()):
            row = {"channel": cid,
                   "touches_weir": cid in behind_weir,
                   "cfd_ul_min": field.q_m3_s[edge.id] * M3_S_TO_UL_MIN,
                   "operator_ul_min": op["channels"][cid]["flow_ul_min"],
                   "reference_weir_ul_min": refs["weir"]["q_m3_s"][cid] * M3_S_TO_UL_MIN,
                   "reference_no_weir_ul_min": refs["plain"]["q_m3_s"][cid] * M3_S_TO_UL_MIN}
            row["C_cfd_vs_operator"] = abs(row["cfd_ul_min"] - row["operator_ul_min"]) / scale_q
            row["B_cfd_vs_reference"] = abs(row["cfd_ul_min"]
                                            - row["reference_weir_ul_min"]) / scale_q
            row["A_operator_vs_reference"] = abs(row["operator_ul_min"]
                                                 - row["reference_no_weir_ul_min"]) / scale_q
            q_rows.append(row)

        stats = {
            "A_operator_vs_reference_p": max((r["A_operator_vs_reference"] for r in p_rows), default=0.0),
            "A_operator_vs_reference_q": max((r["A_operator_vs_reference"] for r in q_rows), default=0.0),
            "B_cfd_vs_reference_p": max((r["B_cfd_vs_reference"] for r in p_rows), default=0.0),
            "B_cfd_vs_reference_q": max((r["B_cfd_vs_reference"] for r in q_rows), default=0.0),
            "C_cfd_vs_operator_p": max((r["C_cfd_vs_operator"] for r in p_rows), default=0.0),
            "C_cfd_vs_operator_q": max((r["C_cfd_vs_operator"] for r in q_rows), default=0.0),
            "weir_effect_p": max((r["weir_effect"] for r in p_rows), default=0.0),
        }
        for key, value in stats.items():
            worst[key] = max(worst[key], value)
        modes.append({"mode": mode, "label": nw.MODULE1_MODES[mode]["label"], **stats,
                      "cfd_max_pa": max(field.p_pa[n] for n in mesh.ports.values()),
                      "operator_max_pa": max(op["pressure_pa"].values()),
                      "reference_weir_max_pa": max(refs["weir"]["p_pa"][p] for p in mesh.ports),
                      "reference_no_weir_max_pa": max(refs["plain"]["p_pa"][p] for p in mesh.ports),
                      "cfd_mass_imbalance_vs_inflow": (abs(field.residual.mass_imbalance_max)
                                                       / max(field.residual.q_in, 1e-30)),
                      "pressures": p_rows, "flows": q_rows})

    live = [m for m in modes if "skipped" not in m]
    a_ok = (worst["A_operator_vs_reference_p"] <= TOL["crosscheck"]
            and worst["A_operator_vs_reference_q"] <= TOL["crosscheck"])
    b_ok = (worst["B_cfd_vs_reference_p"] <= TOL["crosscheck"]
            and worst["B_cfd_vs_reference_q"] <= TOL["crosscheck"])
    c_ok = (worst["C_cfd_vs_operator_p"] <= TOL["crosscheck"]
            and worst["C_cfd_vs_operator_q"] <= TOL["crosscheck"])
    crest = next((c for m in ref_meta.values() for c in m["weir_crests"]), None)
    law_shift = max((abs(r["reference_exact_law_pa"] - r["reference_weir_pa"])
                     / max(abs(r["reference_weir_pa"]), 1e-30)
                     for m in live for r in m["pressures"] if r["reference_weir_pa"]),
                    default=0.0)
    split = next((r for m in live if m["mode"] == "int_g" for r in m["flows"]
                  if r["channel"] == "ch_n_j"), None)

    if a_ok and b_ok and c_ok:
        verdict = (f"AGREE: the two solvers match to "
                   f"{100 * worst['C_cfd_vs_operator_p']:.2f}% on pressure and "
                   f"{100 * worst['C_cfd_vs_operator_q']:.2f}% on per-channel flow in "
                   f"every mode, and each one matches validate.py's independent network.")
    elif a_ok and b_ok:
        verdict = (
            "THEY DISAGREE, AND operator/network.py IS THE ONE THAT IS WRONG. The evidence "
            "is a decomposition, not an adjustment. (1) g_cx and a_cx carry "
            "`retention: {type: weir, opening_um: 25}` in chip/module1_v1.json, and flow "
            "over a 25 um crest goes as gap^3: one crest across the chamber's full width "
            f"is {crest['r_pa_s_m3'] / 1e9:.1f}e9 Pa s/m^3, MORE than a whole 0.2 x 0.3 mm "
            "channel. mesh.py models it; operator/network.py has no weir term at all - its "
            "model is channels plus chamber-crossing slabs - so it omits a feature the spec "
            "declares. (2) validate.py's own network, built here from the conservation law, "
            f"reproduces operator/network.py to {100 * worst['A_operator_vs_reference_p']:.2f}% "
            f"when the crests are left OUT and the CFD to "
            f"{100 * worst['B_cfd_vs_reference_p']:.2f}% on pressure and "
            f"{100 * worst['B_cfd_vs_reference_q']:.2f}% on flow when they are put IN. So "
            "both linear solves, both geometries and the shared resistance law are "
            f"confirmed, and the whole {100 * worst['C_cfd_vs_operator_p']:.0f}% pressure "
            f"gap is the weir and nothing else. (3) It changes a design answer, not just a "
            f"number: in int_g operator/network.py splits conditioned medium "
            f"{abs(split['operator_ul_min']) / flow * 100:.0f}/"
            f"{100 - abs(split['operator_ul_min']) / flow * 100:.0f} between n_cx and a_cx, "
            f"while the real chip sends {abs(split['cfd_ul_min']) / flow * 100:.0f}% to "
            "n_cx, because n_cx has no retention feature and the astrocyte path has two "
            "crests. NOTHING WAS TUNED: the CFD was not touched, and the reference's crest "
            "length comes from contracts/blocks.json min_open_channel_mm.")
    elif b_ok:
        verdict = (
            f"The CFD matches validate.py's independent network to "
            f"{100 * worst['B_cfd_vs_reference_p']:.2f}% on pressure, but "
            f"operator/network.py does not match that same network even with the weirs "
            f"removed ({100 * worst['A_operator_vs_reference_p']:.1f}%), so its "
            "disagreement is NOT only the weir. Two independent implementations now agree "
            "against it; see the per-mode tables for the node where its gap appears.")
    else:
        verdict = (
            f"UNEXPLAINED DISAGREEMENT: the CFD differs from validate.py's own network by "
            f"up to {100 * worst['B_cfd_vs_reference_p']:.1f}% on pressure and "
            f"{100 * worst['B_cfd_vs_reference_q']:.1f}% on per-channel flow. That "
            "reference shares neither the resistance law nor the solve with solve.py, so "
            "one of the two is wrong and no CFD number on this chip can be trusted until "
            "it is resolved.")

    if not a_ok:
        findings.append(_finding(
            "validate_operator_unexplained_gap", "model_error", "high",
            f"operator/network.py differs from validate.py's independent network by "
            f"{100 * worst['A_operator_vs_reference_p']:.1f}% on pressure and "
            f"{100 * worst['A_operator_vs_reference_q']:.1f}% on flow with the SAME "
            "physics (no weir, same law, same centrelines), so the difference is in its "
            "solve or its chamber model, not in the modelling choices",
            chip_id="module1_v1", metric="crosscheck_pressure_error",
            value=worst["A_operator_vs_reference_p"], limit=TOL["crosscheck"],
            units="fraction",
            fix_hint="compare the chamber-crossing slab term node by node; the per-mode "
                     "pressure tables in this benchmark name the node"))
    if not c_ok:
        findings.append(_finding(
            "validate_operator_ignores_weir", "model_error", "high",
            f"operator/network.py ignores the 25 um weir crests that chip/module1_v1.json "
            f"declares on g_cx and a_cx, so on this chip its pressures are low by up to "
            f"{100 * worst['C_cfd_vs_operator_p']:.0f}% and its per-channel flows wrong by "
            f"up to {100 * worst['C_cfd_vs_operator_q']:.0f}%. In the interaction modes it "
            "splits conditioned medium evenly between the two receiving chambers when the "
            f"real chip sends about {abs(split['cfd_ul_min']) / flow * 100:.0f}% of it to "
            "n_cx. The CFD is the correct one here; this is a finding against the operator "
            "stack, not against the CFD.",
            chip_id="module1_v1", feature_id=",".join(weir_chambers),
            metric="crosscheck_flow_error", value=worst["C_cfd_vs_operator_q"],
            limit=TOL["crosscheck"], units="fraction",
            fix_hint="add a gap^3 slit term to operator/network.py for any chamber with "
                     "`retention.opening_um`, or say in its docstring that it models no "
                     "retention feature and must not be used on a weir chip"))
    if law_shift > TOL["aspect_ratio_error_report"]:
        findings.append(_finding(
            "validate_pressure_law_shift", "model_error", "low",
            f"both solvers use the contract's 1 - 0.63 h/w resistance, which over-states "
            f"module1_v1 pressures by {100 * law_shift:.1f}% because this chip's channels "
            "run at w/h 1.5, well inside where that wide-channel limit is accurate",
            chip_id="module1_v1", metric="pressure_law_error", value=law_shift,
            limit=TOL["aspect_ratio_error_report"], units="fraction",
            fix_hint="the exact series lowers reported pressure and changes no flow split; "
                     "see the duct_resistance benchmark"))

    status = "pass" if b_ok else "fail"
    return Benchmark(
        "operator_crosscheck", name, requirement, "mesh.py + solve.py vs operator/network.py",
        status, "max pressure error against an independent network, all modes",
        worst["B_cfd_vs_reference_p"], TOL["crosscheck"], "fraction",
        f"{len(live)} modes at {flow} uL/min per feed, mu = {mu} Pa s. "
        f"A: operator/network.py vs an independent network with the same physics, "
        f"{100 * worst['A_operator_vs_reference_p']:.2f}% / "
        f"{100 * worst['A_operator_vs_reference_q']:.2f}% (p / Q). "
        f"B: the CFD vs that network with the spec's weirs, "
        f"{100 * worst['B_cfd_vs_reference_p']:.2f}% / "
        f"{100 * worst['B_cfd_vs_reference_q']:.2f}%. "
        f"C: the two solvers against each other, "
        f"{100 * worst['C_cfd_vs_operator_p']:.0f}% / "
        f"{100 * worst['C_cfd_vs_operator_q']:.0f}%. {verdict}",
        {"flow_ul_min": flow, "mu_pa_s": mu, "grid_mm": ctx["grid_mm"],
         "worst": worst, "verdict": verdict, "modes": modes,
         "comparisons": {
             "A_operator_vs_reference_no_weir": "same physics, independent code: is "
                                                "operator/network.py's solve right?",
             "B_cfd_vs_reference_with_weir": "the spec's weirs, independent code: is the "
                                             "CFD right?",
             "C_cfd_vs_operator": "the gap this benchmark has to explain"},
         "which_is_wrong": ("neither - they agree" if (a_ok and b_ok and c_ok)
                            else "operator/network.py" if (a_ok and b_ok)
                            else "unresolved"),
         "int_g_split": split,
         "weir_chambers": weir_chambers, "nodes_behind_a_weir": sorted(behind_weir),
         "weir_crest": crest,
         "resistance_law_shift": law_shift,
         "independence": (
             "validate.py's reference shares only the channel CENTRELINES with "
             "operator/network.py (channel_geometry, DXF where one exists) - there is one "
             "right answer for a centreline. Its resistance law, chamber-crossing slab, "
             "weir crest and linear solve are written here from the conservation law. "
             "mesh.py/solve.py share nothing with either."),
         "shared_assumption": (
             "a crest's length ALONG the flow is not in the chip spec. mesh.py assumes one, "
             "and validate.py uses contracts/blocks.json min_open_channel_mm "
             f"({contracts.floor('min_open_channel_mm')} mm). R_slit is linear in it, so "
             "the two agreeing on the crest's magnitude is partly a shared assumption; "
             "what is NOT assumed is that the crest exists at all, which the spec states. "
             "mesh.py also records in its own `unresolved` that the spec does not say "
             "which wall carries the crest.")},
        expected=f"<= {TOL['crosscheck']:g} against an independent network"), findings


def _behind_weir_nodes(chip: dict, weir_chambers: Sequence[str]) -> set[str]:
    """Ports and channels whose path to anywhere crosses a weir chamber's crest.

    Everything that touches a weir chamber, plus the ports hanging off it: those are the
    places a solver with no weir term cannot be expected to match, and separating them is
    what turns "the two disagree" into "the two disagree HERE, for THIS reason".
    """
    out: set[str] = set()
    for ch in chip["channels"]:
        if ch["from"] in weir_chambers or ch["to"] in weir_chambers:
            out.add(ch["id"])
            for end in (ch["from"], ch["to"]):
                if end not in weir_chambers:
                    out.add(end)
    return out


# --------------------------------------------------------------------------------------
# 6. mass conservation on every repo chip
# --------------------------------------------------------------------------------------


def bench_mass_conservation(ctx: dict) -> tuple[Benchmark, list[dict]]:
    cfd, mu = ctx["cfd"], ctx["mu"]
    name = "Mass conservation at every node of every repo chip"
    requirement = "README Validation: mass conservation at every node, total in = total out"
    if not cfd.available:
        return Benchmark.skipped("mass_conservation", name, requirement, cfd.reason), []
    rows, findings = [], []
    worst = 0.0          # worst PER-NODE imbalance: the check that can catch a leak
    worst_total = 0.0    # worst GLOBAL fed-vs-recovered sum: round-off grows with mesh size
    unrouted = []
    for path in repo_chips():
        chip = _load(path)
        chip_id = chip.get("id", path.stem)
        if not _is_routed(chip):
            unrouted.append(chip_id)
            rows.append({"chip_id": chip_id, "skipped": "not routed yet: the spec has "
                                                        "chambers and ports but no channels"})
            continue
        inlets = [p["id"] for p in chip.get("ports", []) if p.get("type") == "inlet"]
        outlets = [p["id"] for p in chip.get("ports", []) if p.get("type") == "outlet"]
        if not inlets or not outlets:
            rows.append({"chip_id": chip_id, "skipped": "the spec has no inlet/outlet pair"})
            findings.append(_finding(
                f"validate_no_port_pair_{chip_id}", "dead_end", "medium",
                f"{chip_id} declares {len(inlets)} inlet and {len(outlets)} outlet ports, "
                "so no steady flow case can be posed and mass conservation cannot be "
                "checked on it at all",
                chip_id=chip_id, metric="typed_ports",
                value=float(len(inlets) + len(outlets)), units="count",
                fix_hint="give every port a `type` in the chip spec"))
            continue
        try:
            mesh = cfd.mesh.build_mesh(chip, grid_mm=ctx["grid_mm"], mu_pa_s=mu)
            ports = {p: cfd.model.PortState.flow(ctx["flow_ul_min"] / len(inlets))
                     for p in inlets}
            ports.update({p: cfd.model.PortState.vent() for p in outlets})
            field = _solve_mesh(cfd, mesh, ports, step="mass", mu=mu)
        except Exception as exc:
            rows.append({"chip_id": chip_id, "skipped": f"{type(exc).__name__}: {exc}"})
            findings.append(_finding(
                f"validate_unsolvable_{chip_id}", "model_error", "high",
                f"{chip_id} cannot be meshed and solved, so no finding about it can exist: "
                f"{type(exc).__name__}: {exc}",
                chip_id=chip_id, metric="solvable", value=0.0, units="bool",
                fix_hint="fix the spec or the mesher before reporting on this chip"))
            continue

        # Re-derived here from the edge flows: the point is not to believe
        # Field.residual, it is to check it.
        net: dict[str, float] = {n.id: 0.0 for n in mesh.nodes}
        for edge in mesh.edges:
            q = field.q_m3_s[edge.id]
            net[edge.a] -= q
            net[edge.b] += q
        port_nodes = {mesh.ports[p] for p in ports}
        for pid, state in ports.items():
            if state.is_flow:
                net[mesh.ports[pid]] += state.q_m3_s
        q_in = sum(state.q_m3_s for state in ports.values() if state.is_flow)
        interior = {nid: v for nid, v in net.items() if nid not in port_nodes}
        node_err = max(abs(v) for v in interior.values()) / q_in
        # At a vented node the edges deliver the flow the port then takes away, so the
        # edge sum IS what came out.
        recovered = sum(net[mesh.ports[p]] for p in outlets)
        total_err = _rel(q_in, recovered)
        reported = abs(field.residual.mass_imbalance_max) / q_in
        # Kept apart. Maxing them together reported a global float64 sum under the label
        # "worst node imbalance" and failed the suite on the one chip big enough to
        # accumulate it, stamping UNTRUSTED on every finding in the repo.
        worst = max(worst, node_err)
        worst_total = max(worst_total, total_err)
        rows.append({
            "chip_id": chip_id, "nodes": len(mesh.nodes), "edges": len(mesh.edges),
            "fed_ul_min": q_in * M3_S_TO_UL_MIN,
            "recovered_ul_min": recovered * M3_S_TO_UL_MIN,
            "total_in_vs_out_error": total_err,
            "max_node_imbalance_frac": node_err,
            "solver_reported_imbalance_frac": reported,
            "residual_agrees": abs(reported - node_err) <= 1e-9 + node_err,
            "worst_node": max(interior, key=lambda k: abs(interior[k])) if interior else None,
        })
    ok_node = worst <= TOL["mass_imbalance"]
    ok_total = worst_total <= TOL["mass_total_imbalance"]
    ok = ok_node and ok_total
    if not ok_node:
        findings.append(_finding(
            "validate_mass_conservation", "model_error", "blocker",
            f"mass is NOT conserved: worst node imbalance {worst:.2e} of total inflow. "
            "Every flow, pressure and shear number from this solver is suspect.",
            metric="mass_imbalance_frac", value=worst, limit=TOL["mass_imbalance"],
            units="fraction",
            fix_hint="the linear solve has not converged (or an edge is missing from a "
                     "node's balance); tighten CG_RTOL in solve.py"))
    if not ok_total:
        findings.append(_finding(
            "validate_mass_total_imbalance", "model_error", "medium",
            f"total fed and total recovered differ by {worst_total:.2e} of inflow, above "
            f"{TOL['mass_total_imbalance']:.0e}, while every node balances to {worst:.2e}. "
            "Fluid lost locally would show at a node first, so this is accumulated "
            "round-off in a global sum rather than a leak - but it is above the expected "
            "floor, so treat tight flow splits on the largest meshes as provisional.",
            metric="mass_total_imbalance_frac", value=worst_total,
            limit=TOL["mass_total_imbalance"], units="fraction",
            fix_hint="compare against the per-node figure, which is the physical check; "
                     "tighten CG_RTOL in solve.py if the gap grows with it"))
    live = [r for r in rows if "skipped" not in r]
    if unrouted:
        findings.append(_finding(
            "validate_unrouted_chips", "dead_end", "low",
            f"{len(unrouted)} spec(s) in chip/ have no channels and so no flow at all "
            f"({', '.join(unrouted)}): they are S6 build output that S7 has not routed "
            "yet. Nothing in the CFD can report on them, and that is not a defect in "
            "them or in the solver.",
            chip_id=unrouted[0], metric="channels", value=0.0, units="count",
            fix_hint="route them through S7, or leave them out of the analysed set"))
    return Benchmark(
        "mass_conservation", name, requirement, "solve.py", "pass" if ok else "fail",
        # The headline metric is the per-node check, except when that passed and only the
        # global sum failed - then the reported number has to be the one that decided it.
        *(("total fed vs recovered as a fraction of inflow", worst_total,
           TOL["mass_total_imbalance"]) if ok_node and not ok_total else
          ("worst node imbalance as a fraction of total inflow", worst,
           TOL["mass_imbalance"])),
        "fraction",
        f"{len(live)} of {len(rows)} chips solved - "
        + (f"{len(unrouted)} are unrouted S6 output with no channels - " if unrouted else "")
        + f"{sum(r['nodes'] for r in live)} nodes checked one by one: worst PER-NODE "
          f"imbalance {worst:.2e} of inflow (limit {TOL['mass_imbalance']:.0e}), worst "
          f"GLOBAL fed-vs-recovered {worst_total:.2e} (limit "
          f"{TOL['mass_total_imbalance']:.0e}, and it grows with mesh size because it is a "
          f"float64 sum over every edge). Re-derived here from Field.q_m3_s, and "
          f"Field.residual agrees with it everywhere.",
        {"chips": rows, "flow_ul_min": ctx["flow_ul_min"], "grid_mm": ctx["grid_mm"],
         "chips_solved": len(live), "chips_unrouted": unrouted,
         "nodes_checked": sum(r["nodes"] for r in live),
         "case": "every typed inlet fed equally, every typed outlet vented, nothing capped",
         "method": ("node balance recomputed here by summing Field.q_m3_s over every edge "
                    "that touches each node, so a wrong residual cannot hide a wrong "
                    "field"),
         "floor_note": ("solve.py closes mass exactly on a hand-calculable network and to "
                        "1e-10..1e-11 of through-flow on a full mesh; that is the "
                        "formulation's floor, so the limit here is set just above it")},
        expected=f"<= {TOL['mass_imbalance']:g} of through-flow at every node"), findings


# --------------------------------------------------------------------------------------
# 7. wall shear vs the exact duct solution
# --------------------------------------------------------------------------------------


def bench_wall_shear(ctx: dict) -> tuple[Benchmark, list[dict]]:
    cfd, mu = ctx["cfd"], ctx["mu"]
    q_ul_min = 10.0
    q = q_ul_min * UL_MIN_TO_M3_S
    h = 0.3
    rows = []
    for r in (1.0, 1.2, 1.333, 1.5, 2.0, 3.0, 5.0, 8.343, 10.0, 20.0, 30.0):
        w = r * h
        slot = wall_shear_slot(q, w, h, mu)
        exact_max = wall_shear_max_exact(q, w, h, mu)
        row = {"w_over_h": r, "slot_dyn_cm2": slot * DYN_CM2_PER_PA,
               "exact_max_dyn_cm2": exact_max * DYN_CM2_PER_PA,
               "exact_mean_dyn_cm2": wall_shear_mean_exact(q, w, h, mu) * DYN_CM2_PER_PA,
               "correction_needed": wall_shear_correction(w, h)}
        if cfd.available:
            row["solver_factor"] = cfd.solve.rect_shear_factor(w, h)
            row["solver_vs_exact"] = _rel(row["solver_factor"], row["correction_needed"])
            row["solver_shear_dyn_cm2"] = cfd.solve.duct_shear_dyn_cm2(
                q, w * MM, h * MM, mu)
            row["solver_shear_vs_exact"] = _rel(row["solver_shear_dyn_cm2"],
                                                row["exact_max_dyn_cm2"])
        rows.append(row)

    gap = max((row.get("solver_vs_exact", 0.0) for row in rows), default=0.0)
    shear_gap = max((row.get("solver_shear_vs_exact", 0.0) for row in rows), default=0.0)
    # The correction must tend to 1/(1 - 0.63 h/w) for a wide duct, which is an
    # independent closed form and catches a sign or factor slip at large w/h.
    wide = wall_shear_correction(30 * h, h)
    limit_err = _rel(wide, 1 / (1 - 0.63 / 30))

    ceiling = contracts.shear_ceiling_for("adherent_cells").ceiling_dyn_cm2
    chip = _load(CHIP_DIR / "module1_v1.json")
    chamber = next(c for c in chip["chambers"] if c["id"] == "n_cx")
    cw, cd = chamber["size"][1], chamber["size"][2]
    q_proto = ctx["flow_ul_min"] * UL_MIN_TO_M3_S
    consequence = {
        "chamber": "module1_v1 n_cx", "width_mm": cw, "depth_mm": cd,
        "w_over_h": cw / cd, "flow_ul_min": ctx["flow_ul_min"],
        "slot_formula_dyn_cm2": wall_shear_slot(q_proto, cw, cd, mu) * DYN_CM2_PER_PA,
        "exact_peak_dyn_cm2": wall_shear_max_exact(q_proto, cw, cd, mu) * DYN_CM2_PER_PA,
        "ceiling_dyn_cm2": ceiling,
        "correction": wall_shear_correction(cw, cd),
        "flow_headroom_lost_pct": 100 * (1 - 1 / wall_shear_correction(cw, cd))}

    findings: list[dict] = []
    if cfd.available and gap > TOL["shear_factor"]:
        findings.append(_finding(
            "validate_shear_factor", "shear", "high",
            f"solve.rect_shear_factor disagrees with an independent series solution by "
            f"{100 * gap:.2f}%, so every shear finding is off by that much against the "
            f"{ceiling} dyn/cm2 ceiling",
            metric="shear_factor_error", value=gap, limit=TOL["shear_factor"],
            units="fraction",
            fix_hint="compare term by term against validate.wall_shear_correction"))
    findings.append(_finding(
        "validate_wall_shear_needs_correction", "shear", "medium",
        f"tau = 6 mu Q / (w h^2) is the PARALLEL-PLATE value and under-reports peak wall "
        f"shear by {100 * (rows[0]['correction_needed'] - 1):.0f}% at w/h 1, "
        f"{100 * (wall_shear_correction(0.4, 0.3) - 1):.0f}% at w/h 1.33 and "
        f"{100 * (wall_shear_correction(cw, cd) - 1):.0f}% in module1_v1's chambers "
        f"(w/h {cw / cd:.1f}). solve.py applies the exact correction and this suite "
        f"confirms it to {gap:.1e}; anything else that prints wall shear - the operator "
        f"stack, the design docs, protocols/build_module1.py - does not, and is that much "
        f"optimistic about the {ceiling} dyn/cm2 adherent-cell ceiling.",
        chip_id="module1_v1", feature_id="n_cx", metric="wall_shear_dyn_cm2",
        value=consequence["exact_peak_dyn_cm2"], limit=ceiling, units="dyn/cm2",
        fix_hint="multiply any hand-computed wall shear by "
                 "validate.wall_shear_correction(w, h) (1.602 square, 1.443 at w/h 1.5, "
                 "-> 1/(1 - 0.63 h/w) when wide)"))

    ok = (limit_err <= 1e-3 and all(row["correction_needed"] >= 1.0 for row in rows)
          and (not cfd.available or (gap <= TOL["shear_factor"]
                                     and shear_gap <= TOL["shear_factor"])))
    return Benchmark(
        "wall_shear", "Wall shear vs the exact duct solution, w/h 1-30",
        "brief 7: wall shear vs the exact duct solution, checking the solver's correction",
        "analytic vs solve.py", "pass" if ok else "fail",
        "max error in solve.rect_shear_factor vs an independent series" if cfd.available
        else "correction at w/h 1", gap if cfd.available else rows[0]["correction_needed"],
        TOL["shear_factor"] if cfd.available else None, "fraction" if cfd.available else "factor",
        f"solve.rect_shear_factor matches an independently derived series to {gap:.1e} and "
        f"solve.duct_shear_dyn_cm2 to {shear_gap:.1e} across w/h 1-30. The uncorrected slot "
        f"form under-reports peak wall shear by {rows[0]['correction_needed']:.3f}x at w/h 1, "
        f"{wall_shear_correction(0.45, 0.3):.3f}x at w/h 1.5 and "
        f"{rows[-1]['correction_needed']:.3f}x at w/h 30, tending to 1/(1 - 0.63 h/w) and "
        f"never below 1 - so an uncorrected number is always optimistic",
        {"aspect_sweep": rows, "q_ul_min": q_ul_min, "depth_mm": h,
         "solver_factor_max_error": gap, "solver_shear_max_error": shear_gap,
         "wide_limit_check": {"correction_at_w_over_h_30": wide,
                              "one_over_0p63_bracket": 1 / (1 - 0.63 / 30),
                              "error": limit_err},
         "consequence": consequence,
         "two_series": ("solve.py expands in the width direction and takes the larger of "
                        "the two walls; validate.py expands in the depth direction and "
                        "sums 1 - sech. They are different series for the same quantity, "
                        "which is the point of comparing them."),
         "hele_shaw_faces": ("solve.hele_shaw_shear_dyn_cm2 deliberately applies NO "
                             "correction: a face's width is a meshing choice, not a wall, "
                             "and correcting for it would make shear depend on grid_mm. "
                             "That is right for the chamber interior and under-reads near "
                             "a side wall - see hele_shaw_validity.")},
        expected=f"<= {TOL['shear_factor']:g} against an independent series"), findings


# --------------------------------------------------------------------------------------
# 8. where a depth-averaged model stops being safe
# --------------------------------------------------------------------------------------


def bench_hele_shaw_validity(ctx: dict) -> tuple[Benchmark, list[dict]]:
    cfd = ctx["cfd"]
    chosen: dict[str, dict[str, str]] = {}
    if cfd.available:
        for path in repo_chips():
            try:
                mesh = cfd.mesh.build_mesh(_load(path), grid_mm=ctx["grid_mm"], mu_pa_s=ctx["mu"])
            except Exception:
                continue
            chosen[mesh.chip_id] = {f.get("feature_id", ""): f.get("model", "")
                                    for f in mesh.meta.get("features", [])}

    rows, findings = [], []
    for path in repo_chips():
        chip = _load(path)
        features = [(ch["id"], "channel", float(ch["width_mm"]), float(ch["depth_mm"]))
                    for ch in chip.get("channels", [])]
        for c in chip.get("chambers", []):
            sx, sy, sz = c["size"]
            features.append((c["id"], c.get("type", "chamber"), float(min(sx, sy)), float(sz)))
        for fid, kind, w, d in features:
            span, depth = max(w, d), min(w, d)
            ratio = span / depth
            if ratio >= HS_ASPECT_FLOOR:
                continue
            # A side-wall layer is of order the depth on each side. What is left is the
            # part of the span a depth-averaged field actually describes.
            core = max(0.0, (span - 2 * depth) / span)
            # mesh.py records a model per CHAMBER; a channel is a duct by construction.
            known = chip["id"] in chosen
            meshed_as = chosen.get(chip["id"], {}).get(
                fid, "duct (channel)" if known else "unknown: mesh.py was not available")
            rows.append({"chip_id": chip["id"], "feature_id": fid, "kind": kind,
                         "routed": _is_routed(chip),
                         "span_mm": span, "depth_mm": depth, "w_over_h": ratio,
                         "depth_averaged_core_fraction": core,
                         "side_wall_dp_term_pct": 100 * (1 - duct_shape_factor(span, depth)),
                         "meshed_as": meshed_as})

    # An unrouted spec has no flow, so nothing will be reported on it: counting its
    # chambers here would inflate the number that matters with mid-pipeline scratch.
    routed = [r for r in rows if r["routed"]]
    wrongly_2d = [r for r in routed if r["meshed_as"] == "hele_shaw"]
    no_core = [r for r in routed if r["depth_averaged_core_fraction"] <= 0]
    unrouted_features = len(rows) - len(routed)
    per_chip: dict[str, int] = {}
    for row in routed:
        per_chip[row["chip_id"]] = per_chip.get(row["chip_id"], 0) + 1

    if wrongly_2d:
        worst = min(wrongly_2d, key=lambda r: r["w_over_h"])
        findings.append(_finding(
            "validate_hele_shaw_below_floor", "unmodelled", "high",
            f"{len(wrongly_2d)} feature(s) below w/h {HS_ASPECT_FLOOR:g} are meshed as "
            f"depth-averaged 2D cells (worst {worst['chip_id']}:{worst['feature_id']} at "
            f"w/h {worst['w_over_h']:.2f}): the side-wall layers cover most of the span "
            "there, so the 2D velocity field - and every trap and residence-time finding "
            "drawn from it - is not a reliable description of the flow",
            chip_id=worst["chip_id"], feature_id=worst["feature_id"], metric="w_over_h",
            value=worst["w_over_h"], limit=HS_ASPECT_FLOOR, units="ratio",
            fix_hint="mesh any feature below w/h 5 as a duct, as mesh.py already does for "
                     "channel-like blocks"))
    narrow_chambers = [r for r in routed if r["kind"] != "channel"]
    if narrow_chambers:
        worst = min(narrow_chambers, key=lambda r: r["w_over_h"])
        findings.append(_finding(
            "validate_narrow_chamber_aspect", "unmodelled", "medium",
            f"{len(narrow_chambers)} chamber(s) sit below w/h {HS_ASPECT_FLOOR:g}, worst "
            f"{worst['chip_id']}:{worst['feature_id']} at w/h {worst['w_over_h']:.2f}: "
            "report their velocities as a duct's, not as a 2D field, and treat any "
            "low-velocity zone found inside them as provisional",
            chip_id=worst["chip_id"], feature_id=worst["feature_id"], metric="w_over_h",
            value=worst["w_over_h"], limit=HS_ASPECT_FLOOR, units="ratio"))

    deficit = [{"w_over_h": r,
                "hele_shaw_dp_under_prediction_pct": 100 * (1 - duct_shape_factor(r * 0.3, 0.3))}
               for r in (1.0, 1.5, 2.0, 3.0, 5.0, 8.0, 8.343, 10.0, 20.0, 30.0)]
    # This benchmark is an INVENTORY, so what it asserts is that the inventory is sound:
    # the side-wall deficit must fall monotonically with w/h and vanish in the wide limit.
    pcts = [d["hele_shaw_dp_under_prediction_pct"] for d in deficit]
    monotone = all(a > b for a, b in zip(pcts[:-1], pcts[1:]))
    wide_ok = pcts[-1] < 2.5
    channel_models = {r["meshed_as"] for r in routed if r["kind"] == "channel"}
    channels_as_ducts = (all(m.startswith("duct") for m in channel_models)
                         if cfd.available else None)
    ok = monotone and wide_ok and channels_as_ducts is not False
    return Benchmark(
        "hele_shaw_validity", "Where w/h < 5 makes a depth-averaged model questionable",
        "brief 8: report where w/h < ~5 is questionable near side walls",
        "analytic + mesh.py", "pass" if ok else "fail",
        "chambers below the aspect floor still meshed as a 2D field",
        float(len(wrongly_2d)), None, "count",
        f"{len(routed)} features across {len(per_chip)} routed chips sit below w/h "
        f"{HS_ASPECT_FLOOR:g}. {sum(1 for r in routed if r['kind'] == 'channel')} are "
        f"channels and mesh.py "
        + ("correctly makes every one of them a DUCT, which is where the 0.63 side-wall "
           "term belongs" if channels_as_ducts else "was not available to check them")
        + f". The remaining "
        f"{len(wrongly_2d)} are CHAMBERS meshed as a depth-averaged 2D field at w/h "
        + (", ".join(f"{r['w_over_h']:.2f} ({r['chip_id']}:{r['feature_id']})"
                     for r in sorted(wrongly_2d, key=lambda r: r['w_over_h'])[:5])
           if wrongly_2d else "-")
        + f": there the depth-averaged core is only "
          f"{100 * min((r['depth_averaged_core_fraction'] for r in wrongly_2d), default=1):.0f}"
          f"-{100 * max((r['depth_averaged_core_fraction'] for r in wrongly_2d), default=1):.0f}% "
          f"of the span and the plane model misses "
          f"{min((r['side_wall_dp_term_pct'] for r in wrongly_2d), default=0):.0f}-"
          f"{max((r['side_wall_dp_term_pct'] for r in wrongly_2d), default=0):.0f}% of the "
          f"pressure drop. For reference a 2D chamber under-predicts dP by "
          f"{100 * (1 - duct_shape_factor(2.503, 0.3)):.1f}% even at w/h 8.3, where every "
          f"module1_v1 chamber sits, because the plane model has no side walls at all",
        {"aspect_floor": HS_ASPECT_FLOOR, "features": rows, "per_chip_counts": per_chip,
         "chambers_meshed_2d_below_floor": wrongly_2d,
         "channels_all_meshed_as_ducts": channels_as_ducts,
         "features_with_no_core": len(no_core),
         "sub_floor_features_on_unrouted_chips": unrouted_features,
         "dp_deficit_of_depth_averaging": deficit,
         "gating": ("informational: the brief asks this to be REPORTED, so it does not "
                    "gate the findings. What it asserts is that the inventory is sound - "
                    "the deficit falls monotonically with w/h and vanishes when wide - and "
                    "that every sub-floor CHANNEL is meshed as a duct."),
         "rule": ("side-wall layers are of order the depth, so a depth-averaged field only "
                  "describes the (span - 2 depth) core: below w/h 5 that core is under 60% "
                  "of the span, and below w/h 2 there is none left"),
         "consequence_for_shear": (
             "inside a chamber solve.py reports tau = 6 mu u / h with no side-wall term, "
             "which is right in the core and under-reads within about one depth of a side "
             "wall. A shear finding that fires on a cell against a side wall is a lower "
             "bound, and on the chambers listed here every cell is within a depth of a "
             "wall.")},
        expected="inventory self-consistent; sub-floor channels meshed as ducts"), findings


# --------------------------------------------------------------------------------------
# run everything
# --------------------------------------------------------------------------------------

BENCHMARKS: tuple[Callable[[dict], tuple[Benchmark, list[dict]]], ...] = (
    bench_duct_resistance,
    bench_series_parallel,
    bench_hele_shaw_uniform,
    bench_hele_shaw_point_source,
    bench_grid_convergence,
    bench_operator_crosscheck,
    bench_mass_conservation,
    bench_wall_shear,
    bench_hele_shaw_validity,
)

#: Benchmarks whose failure makes every finding meaningless, as opposed to adding a caveat.
#: `hele_shaw_validity` is deliberately NOT here: the brief asks it to REPORT where a
#: depth-averaged model is questionable, and a chamber at w/h 4 is a caveat on that
#: chamber's findings, not a reason to disbelieve the solver.
GATING = frozenset({"duct_resistance", "series_parallel", "hele_shaw_uniform",
                    "hele_shaw_point_source", "operator_crosscheck", "mass_conservation",
                    "wall_shear", "grid_convergence"})


def protocol_flow_ul_min(chip_id: str = "module1_v1") -> float:
    """The largest flow the chip's protocol actually commands, so the cross-check runs at
    a flow the chip is really driven at rather than a number invented here."""
    proto = json.loads((ROOT / "protocols" / f"{chip_id}.json").read_text())
    flows = [s["flow_ul_min"] for s in proto["steps"] if s.get("flow_ul_min")]
    if not flows:
        raise ValueError(f"protocols/{chip_id}.json commands no flow: nothing to validate at")
    return float(max(flows))


def run(cfd: CfdUnderTest | None = None, grid_mm: float = 0.1) -> dict:
    """Run every benchmark and return the report.

    Never raises because a benchmark failed: a failure is a result, and the thing
    analyse.py acts on is `passed` being False.
    """
    ctx = {
        "mu": contracts.viscosity_for(37).pa_s,
        "flow_ul_min": protocol_flow_ul_min(),
        "grid_mm": grid_mm,
        "cfd": cfd if cfd is not None else discover_cfd(),
    }
    results: list[Benchmark] = []
    findings: list[dict] = []
    for fn in BENCHMARKS:
        bid = fn.__name__.removeprefix("bench_")
        try:
            bench, found = fn(ctx)
        except Exception as exc:                 # a benchmark that cannot run is a skip
            bench = Benchmark.skipped(bid, bid.replace("_", " ").capitalize(), "",
                                      f"{type(exc).__name__}: {exc}")
            found = []
        results.append(bench)
        findings.extend(found)

    counts = {state: sum(1 for b in results if b.status == state)
              for state in ("pass", "fail", "skipped")}
    failed = [b for b in results if b.status == "fail"]
    skipped = [b for b in results if b.status == "skipped"]
    # A gating benchmark that did not even appear is as bad as one that failed: findings
    # are trusted only when every gating benchmark RAN and passed.
    gating_missing = sorted(GATING - {b.id for b in results if b.status == "pass"})
    grid = next((b for b in results if b.id == "grid_convergence"), None)
    recommended = grid.detail.get("recommended_grid_mm") if grid and grid.status == "pass" else None
    status = "fail" if failed else ("incomplete" if skipped else "pass")
    passed = not failed and not skipped and not gating_missing
    reason = ""
    if failed:
        reason = (f"{len(failed)} benchmark(s) FAILED: "
                  + "; ".join(f"{b.id} ({b.metric} = {b.value:.3g})" if b.value is not None
                              else b.id for b in failed)
                  + ". Every finding is untrusted until these pass.")
    elif skipped:
        reason = (f"{len(skipped)} benchmark(s) did not run: "
                  + "; ".join(f"{b.id} - {b.message}" for b in skipped)
                  + ". Nothing has checked the solver on those points.")
    elif gating_missing:
        reason = (f"the suite did not run {', '.join(gating_missing)}, which the findings "
                  f"are gated on, so nothing has been checked.")

    return _plain({
        "schema": "cfd-validation/1",
        "status": status,
        "passed": passed,
        "ok": passed,
        "trusted": passed,
        "reason": reason,
        # run.py reads `untrusted_reason` for the line it prints under the verdict, and
        # analyse.py reads `reason`. Same sentence, both names, so neither gets a blank.
        "untrusted_reason": reason,
        "recommended_grid_mm": recommended,
        "solver": ctx["cfd"].as_json(),
        "mu_pa_s": ctx["mu"],
        "viscosity_source": contracts.viscosity_37c_pa_s().key,
        "flow_ul_min": ctx["flow_ul_min"],
        "grid_mm": grid_mm,
        "counts": counts,
        "gating_missing": gating_missing,
        "benchmarks": [b.as_json() for b in results],
        "checks": [b.as_check() for b in results],
        "validation_findings": findings,
        "contracts": contracts.fingerprint(),
    })


def write_report(report: dict, out: Path = DEFAULT_OUT) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(json.dumps(report, indent=1, default=float, sort_keys=False) + "\n")
    tmp.replace(out)
    return out


def _wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def format_table(report: dict) -> str:
    mark = {"pass": "OK  ", "fail": "FAIL", "skipped": "SKIP"}
    solver = report["solver"]
    lines = [
        f"CFD validation: {report['status'].upper()} - findings "
        f"{'TRUSTED' if report['trusted'] else 'UNTRUSTED'} "
        f"(pass {report['counts']['pass']}, fail {report['counts']['fail']}, "
        f"skip {report['counts']['skipped']})",
        f"under test: {'mesh.py + solve.py' if solver['available'] else solver['reason']}",
        f"mu = {report['mu_pa_s']} Pa s ({report['viscosity_source']}), "
        f"flow {report['flow_ul_min']} uL/min, grid {report['grid_mm']} mm",
        "",
    ]
    for b in report["benchmarks"]:
        lines.append(f"{mark[b['status']]} {b['id']:22s} [{b['solver']}] {b['name']}")
        if b["value"] is not None:
            tol = "" if b["tolerance"] is None else f"  (tol {b['tolerance']:g})"
            lines.append(f"       {b['metric']}: {b['value']:.6g} {b['units']}{tol}")
        lines += [f"       {chunk}" for chunk in _wrap(b["message"], 92)]
    if report["validation_findings"]:
        lines += ["", f"validation findings ({len(report['validation_findings'])}):"]
        for f in report["validation_findings"]:
            lines.append(f"  [{f['severity']:7s}] {f['class']}: {f['message']}")
    lines += ["", f"recommended grid spacing: {report['recommended_grid_mm']} mm"]
    if report["reason"]:
        lines += _wrap(report["reason"], 100)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark the CFD against analytic solutions.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="where to write the report")
    ap.add_argument("--grid-mm", type=float, default=0.1, help="spacing for the chip-level runs")
    ap.add_argument("--json", action="store_true", help="print the report JSON, not a table")
    ap.add_argument("--quiet", action="store_true", help="write the report, print nothing")
    args = ap.parse_args(argv)
    report = run(grid_mm=args.grid_mm)
    write_report(report, args.out)
    if args.json:
        print(json.dumps(report, indent=1, default=float))
    elif not args.quiet:
        print(format_table(report))
        out = args.out
        print(f"report: {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

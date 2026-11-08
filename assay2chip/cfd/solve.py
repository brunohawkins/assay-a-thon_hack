"""Mesh + Scenario -> Field: the Stokes / resistance-network solve.

    from assay2chip.cfd import solve
    field  = solve.solve_step(mesh, scenario)           # one protocol step
    fields = solve.solve_steps(mesh, scenarios)         # a whole protocol, one assembly

Physics. Every edge is a shallow rectangular duct, a Hele-Shaw face, a mouth or a weir
slit, and the audit measured Re well under 1 almost everywhere, so the depth-averaged
resistance-network form is the correct physics and not a convenience (see
`assay2chip/cfd/README.md`). Flow through an edge is `q = g (p_a - p_b)` with
`g = g_geom_m3 / mu`, and mass conservation at every node gives the weighted graph
Laplacian `L p = f`.

The GEOMETRIC Laplacian is viscosity-free, which is what makes a whole protocol cheap:
`L(mu) = L_geom / mu`, so `L_geom p = mu f`. One assembly and one factorisation serve
every step, whatever each step's fluid and temperature. Steps are batched by their
Dirichlet pattern - which ports are pressure-controlled - because that is the only thing
that changes the reduced matrix. (This is why `solve` never calls `Mesh.at_viscosity`:
rescaling every edge per step would throw the shared factorisation away.)

Boundary conditions, per port:
    flow      a source term in f (uL/min in, m^3/s internally; negative draws out)
    pressure  Dirichlet, eliminated from the system rather than penalised
    closed    nothing at all - a capped port is simply not a boundary

A pressure reference is required. With only flow and closed ports the pressure field is
determined only up to a constant per connected component, and the net inflow of such a
component MUST be zero or no steady solution exists - raised as a `SolveError` naming the
ports, never returned as a field of plausible-looking numbers. When the inflow does
balance, a gauge node is pinned and `Field.meta["notes"]` says so: the flows are then
genuine and only the pressure datum is arbitrary.

Linear algebra. Dense LAPACK below `DENSE_MAX_UNKNOWNS` unknowns (one LU, many
back-substitutions, machine precision); above it a Jacobi-preconditioned conjugate
gradient - the reduced Laplacian is symmetric positive definite - vectorised across all
right-hand sides at once. Either way the TRUE residual of every column is recomputed and
checked afterwards, and non-convergence raises. A quietly unconverged solve would feed
`analyse.py` confident nonsense, so it has to be loud.

How exact the mass balance can be, which `validate.py` needs in order to decide what
"tiny" means in `Residual.mass_imbalance_max`. On a network whose answer can be worked out
by hand - a series or parallel chain, a Wheatstone bridge, a uniformly fed slab - it is
machine precision, 1e-16 or better relative to the through-flow, and the tests assert
that. On a full chip mesh it settles at 1e-10 to 1e-11 of the through-flow, and that is a
FLOOR OF THE FORMULATION rather than a loose tolerance: flow is recovered by differencing
the pressure field, and on module1_v1 the quietest faces carry a dP of 4e-17 Pa inside a
field spanning 0.9 Pa, i.e. sixteen orders of cancellation. Tightening `CG_RTOL` from
1e-10 to 1e-14 changes neither the iteration count nor the imbalance; forcing the same
mesh through dense LU does not either. So 1e-9 of the through-flow is a safe limit for
`validate.py`, and anything far worse means a real assembly fault, not arithmetic.

Units. SI internally; `mm`, `uL/min`, `Pa`, `dyn/cm^2` at the JSON boundary, with the unit
in the key name. Contract values come from `assay2chip.lib.contracts` and are never
written here.
"""

from __future__ import annotations

import math
import sys
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.cfd.model import (  # noqa: E402
    DYN_CM2_PER_PA,
    M3_S_TO_UL_MIN,
    UL_MIN_TO_M3_S,
    Edge,
    Field,
    Mesh,
    Node,
    PortState,
    Residual,
    Scenario,
)
from assay2chip.lib import contracts  # noqa: E402

# --- thresholds, all in one place -------------------------------------------

#: Unknowns at or below this go to dense LAPACK; above it, Jacobi-PCG. Dense is exact to
#: machine precision and factors ONCE for every right-hand side, so it wins until the
#: O(n^3) term bites: at n = 1200 that is ~0.6 GFLOP, well inside the "milliseconds"
#: budget the README's fix-and-re-run loop needs. Measured on this repo's chips rastered
#: at 0.25 mm (n ~ 1700-2300, ~2.5 nnz per row): dense is ~240 ms at n = 2300 against
#: ~26 ms for one CG solve, so CG owns the large end.
DENSE_MAX_UNKNOWNS = 1200

#: ...but one dense factorisation serves a whole protocol, while CG pays per step. With
#: this many right-hand sides in one batch, dense wins again up to a higher n: measured
#: at n = 1670 with 24 steps, dense took ~120 ms against ~195 ms for the batched CG.
DENSE_RHS_AMORTISE = 8
DENSE_MAX_UNKNOWNS_BATCHED = 2000

#: CG stops when every column's residual is this small relative to ||b||.
CG_RTOL = 1e-10
#: Iteration cap. A 2D Laplacian with Jacobi needs O(sqrt(n)) iterations, so 30 sqrt(n)
#: is ~30x headroom: reaching it means something is wrong, not that we need patience.
CG_MAXITER_FLOOR = 1000
CG_MAXITER_SQRT_FACTOR = 30
#: How often to replace CG's updated residual with the real `B - A X`. The recurrence
#: drifts, and on an ill-conditioned mesh it drifts OPTIMISTIC: on module3_v1_translated
#: it claimed 1e-11 while the true residual had stalled at 2e-10. Stopping on a number
#: that flattering is how a solver quietly reports a non-solution, so the true residual
#: decides - every 25 iterations costs one extra matvec in 25, about 4%.
CG_TRUE_RESIDUAL_EVERY = 25
#: A block of iterations "made progress" if it cut the true residual to this fraction of
#: the best seen so far.
CG_STALL_FACTOR = 0.9
#: How many consecutive blocks may stop making progress before we stop polishing - but
#: ONLY once the residual is already inside `RESIDUAL_RTOL`, i.e. the answer is already
#: good enough to report and the remaining iterations would buy nothing. Giving up while
#: the residual is still large is never right: Jacobi-PCG on a mesh whose conductances
#: span orders (a weir slit beside a duct) plateaus for a hundred iterations and then
#: converges, and an earlier version of this abandoned module1_v1 at a residual of 0.25.
CG_STALL_PATIENCE = 2
#: The true residual every solve is audited against, relative to ||b||. Dense lands near
#: 1e-15, CG is asked for 1e-10; above this we raise rather than report.
RESIDUAL_RTOL = 1e-8

#: Re above this is outside the model's validity. README: report it, do not trust it.
RE_VALIDITY_LIMIT = 1.0

#: Odd terms in the rectangular-duct series. Both series below are written around their
#: parallel-plate limit and decay as sech(n pi L / 2S)/n^2 and tanh/n^5, so about six
#: terms already exhaust float64 at any aspect ratio; 41 is free and leaves no doubt.
SHEAR_SERIES_TERMS = 41

#: Water at 37 C, used ONLY when the contracts have no density - and then it is FLAGGED.
#: Re is linear in density, so an assumed density makes Re an assumed number.
DENSITY_FALLBACK_KG_M3 = 993.0
DENSITY_CONTRACT_KEYS = ("fluid.density_kg_m3", "fluid.density_37c_kg_m3")

#: Fluids the viscosity contract actually covers. Anything else is a FLAGGED assumption,
#: never silent water - README: "Never silently use water."
CONTRACT_FLUIDS = frozenset({
    "", "water", "medium", "media", "buffer", "pbs", "saline", "dye", "dye_water",
    "cells", "capsules", "cell_suspension", "capsule_suspension",
})

#: Edge kinds whose conductance the mesher built from the RECTANGULAR DUCT expression, so
#: the exact duct shear correction below is the self-consistent wall shear for them.
DUCT_KINDS = frozenset({"duct", "mouth", "slit"})
#: Edge kinds built from the parallel-plate Hele-Shaw expression, which take tau = 6 mu u/h
#: with NO aspect-ratio correction. See `hele_shaw_shear_dyn_cm2`.
HELE_SHAW_KINDS = frozenset({"hs_face"})


def as_port_state(raw: Any) -> PortState:
    """Any port boundary condition -> `model.PortState`.

    `scenario.py` hands over its own richer `PortBC` (it carries the role and the reason
    the state was chosen), and a Field can also be re-solved straight from JSON. All of
    them agree on `type` / `q_ul_min` / `p_pa`, so read those rather than insisting on one
    class: the contract in the README is the field names, not the type.
    """
    if isinstance(raw, PortState):
        return raw
    if isinstance(raw, (dict, str)):
        return PortState.from_json(raw)
    kind = getattr(raw, "type", None)
    if kind is not None:
        return PortState(str(kind), q_ul_min=getattr(raw, "q_ul_min", None),
                         p_pa=getattr(raw, "p_pa", None))
    for method in ("to_dict", "to_json"):
        if hasattr(raw, method):
            return PortState.from_json(getattr(raw, method)())
    raise SolveError(
        f"cannot read a port boundary condition from {type(raw).__name__}: it needs a "
        f"'type' of flow, pressure or closed, with q_ul_min or p_pa to match."
    )


def as_scenario(raw: Any) -> Any:
    """Unwrap a scenario.py `ScenarioRecord` (which carries provenance around a Scenario)."""
    inner = getattr(raw, "scenario", None)
    return inner if inner is not None and hasattr(inner, "ports") else raw


class SolveError(ValueError):
    """The scenario has no steady solution, or the linear solve did not converge.

    Both are loud on purpose. `analyse.py` turns these numbers into findings with
    severities attached, so a field whose pressures are a free constant because nothing
    was referenced, or one that quietly stopped short of convergence, is worse than no
    field at all.
    """


# --- contract-backed fluid properties ----------------------------------------

@dataclass(frozen=True)
class Fluid:
    """Everything the solve needs about one step's liquid, with its provenance."""

    mu_pa_s: float
    mu_source: str
    rho_kg_m3: float
    rho_source: str
    rho_is_assumption: bool
    name: str = ""
    temp_c: float | None = None
    notes: tuple[str, ...] = ()


def density() -> tuple[float, str, bool]:
    """(kg/m^3, source, is_assumption) for Reynolds numbers.

    `Re = rho u D_h / mu` is linear in density, so this cannot be swallowed. There are
    three distinct answers and the caller is told which:

      * a contract value the contract stands behind      -> not an assumption
      * a contract value the contract itself flags as a rule of thumb (which is the case
        for `fluid.density_37c_kg_m3`: 1000 kg/m^3, stated to +/-1%) -> an assumption,
        but a reviewed one
      * no contract value at all -> water at 37 C and a loud flag

    Only the last is a number this module picked, and it disappears the moment the
    contract gains the key.
    """
    for key in DENSITY_CONTRACT_KEYS:
        try:
            value = float(contracts.param(key))
        except contracts.ContractError:
            continue
        try:
            assumed = bool(contracts.is_assumption(key))
        except contracts.ContractError:
            assumed = False
        source = f"contracts/parameters.json {key}"
        if assumed:
            source += " (the contract marks this value an assumption)"
        return value, source, assumed
    return (
        DENSITY_FALLBACK_KG_M3,
        f"ASSUMED by solve.py: water at 37 C; contracts/parameters.json has none of "
        f"{', '.join(DENSITY_CONTRACT_KEYS)}",
        True,
    )


def fluid_for(scenario: Scenario) -> Fluid:
    """The step's viscosity and density, from the contracts - never hard-coded here.

    `Scenario.mu_pa_s` wins when scenario.py has already resolved it (it also records its
    own `unresolved`, so we do not second-guess the fluid name in that case). Otherwise
    `contracts.viscosity_for(temp_c)` picks `fluid.viscosity_37c_pa_s` for the real assay
    and `fluid.viscosity_pa_s` (water, 20 C) for the dye demo, and a fluid the contract
    does not cover - the README names cold organic solvent and viscous core buffer - gets
    a flagged assumption.
    """
    name = str(getattr(scenario, "fluid", "") or "")
    temp_c = getattr(scenario, "temp_c", None)
    notes: list[str] = []

    resolved = getattr(scenario, "mu_pa_s", None)
    if resolved is not None:
        mu = float(resolved)
        mu_source = f"Scenario.mu_pa_s, resolved by scenario.py for fluid {name!r}"
    else:
        visc = contracts.viscosity_for(temp_c)
        mu, mu_source = visc.pa_s, f"contracts/parameters.json {visc.key} ({visc.scope})"
        if name.lower() not in CONTRACT_FLUIDS:
            notes.append(
                f"ASSUMED viscosity: fluid {name!r} is not covered by the viscosity "
                f"contract, so {visc.key} = {visc.pa_s} Pa s was used. Resistance, wall "
                f"shear and Re all scale with mu, and a cold or viscous fluid can be "
                f"several-fold off - treat every number from this step as provisional."
            )
    if not math.isfinite(mu) or mu <= 0:
        raise SolveError(
            f"step {getattr(scenario, 'step', '?')!r}: viscosity must be finite and > 0, got {mu}"
        )

    rho, rho_source, rho_assumed = density()
    if rho_assumed:
        notes.append(
            f"ASSUMED density: {rho} kg/m^3 - {rho_source}. Re is linear in it, so every "
            f"Reynolds number here is an assumption, including any 'unmodelled' finding."
        )
    for item in getattr(scenario, "unresolved", []) or []:
        notes.append(f"scenario unresolved: {item}")
    return Fluid(mu, mu_source, rho, rho_source, rho_assumed, name, temp_c, tuple(notes))


# --- wall shear: the exact rectangular duct ----------------------------------
#
# This is the part that has to be right. `tau = 6 mu Q / (w h^2)` is the INFINITE PARALLEL
# PLATE result: it assumes a SHALLOW duct, w the in-plane width ACROSS the flow and h the
# depth, and it under-reads badly once w approaches h. The repo audit measured the true
# maximum wall shear at 1.48x that formula when w/h = 1.33, and 1.60x at w/h = 1.
#
# Implemented here as the EXACT SERIES SOLUTION - not a tabulated factor - from the
# double-sine eigenfunction expansion of Poiseuille flow in a rectangle w x h:
#
#   u = sum_{m,n odd} A_mn sin(m pi y / w) sin(n pi z / h),
#   A_mn = (G / mu) 16 / (pi^2 m n [(m pi / w)^2 + (n pi / h)^2]),      G = -dp/dx
#
# The n-sums close with  sum_{n odd} 1/(n^2 + c^2) = (pi / 4c) tanh(pi c / 2), leaving two
# single series in m that converge like 1/m^2:
#
#   tau_max(wall of width w) / G = (4w/pi^2) sum_{m odd} (-1)^((m-1)/2) tanh(pi m h/2w)/m^2
#   mu Q / G = (64 w^3 h / pi^6) sum_{m odd} [pi^2/8 - (pi w/4 m h) tanh(pi m h/2w)] / m^4
#
# Both reduce to the parallel-plate results (tau = G h/2, Q = G w h^3 / 12 mu) as h/w -> 0,
# which `tests/test_cfd_solve.py` checks to 1e-6. The maximum is taken over BOTH walls -
# the wide one at its centre and the narrow one at its centre - so the factor is also
# right for w < h, a tall narrow slit where the side walls carry the peak.
#
# Note what this factor is NOT: it is not a correction to the mesh's conductance. The
# mesher builds conductance from the README's `12 mu L / (w h^3 (1 - 0.63 h/w))`, which is
# the agreed contract. The factor here is defined as a ratio AT A GIVEN Q, so it is
# independent of how Q was obtained and composes correctly with whatever the mesh did.

_SHEAR_FACTOR_CACHE: dict[int, float] = {}


def _sech(x: np.ndarray) -> np.ndarray:
    """1 / cosh(x) for x >= 0, written so a large argument underflows instead of overflowing."""
    e = np.exp(-x)
    return 2.0 * e / (1.0 + e * e)


def _long_short(w: float, h: float) -> tuple[float, float]:
    """(longer side, shorter side). Q and tau_max are properties of the cross-section, so
    both are symmetric in w and h, and the peak wall shear always sits at the centre of
    the LONGER wall - at w = h the two coincide."""
    return (w, h) if w >= h else (h, w)


def _tau_max_over_g(w: float, h: float, terms: int = SHEAR_SERIES_TERMS) -> float:
    """Maximum wall shear anywhere on the perimeter, divided by G = -dp/dx.

        tau_max / G = (S/2) [1 - (8/pi^2) sum_{n odd} sech(n pi L / 2S) / n^2]

    with L and S the longer and shorter side. Writing it around the parallel-plate value
    S/2 is what makes it well behaved: the series argument is never below pi/2, so sech
    kills the terms exponentially - six of them already exceed float64 - and there is no
    slowly-converging alternating tail and no cancellation at any aspect ratio.
    """
    long_side, short_side = _long_short(w, h)
    n = np.arange(1, terms + 1, 2, dtype=float)
    series = float(np.sum(_sech(n * math.pi * long_side / (2 * short_side)) / n ** 2))
    return (short_side / 2.0) * (1.0 - 8.0 / math.pi ** 2 * series)


def _mu_q_over_g(w: float, h: float, terms: int = SHEAR_SERIES_TERMS) -> float:
    """mu Q / G for a rectangular duct: the exact Poiseuille conductance.

        mu Q / G = (L S^3 / 12) [1 - (192 S / (pi^5 L)) sum_{n odd} tanh(n pi L / 2S)/n^5]

    the classical result (White, *Viscous Fluid Flow*), again written around its
    parallel-plate value `L S^3 / 12` and converging like 1/n^5.
    """
    long_side, short_side = _long_short(w, h)
    n = np.arange(1, terms + 1, 2, dtype=float)
    series = float(np.sum(np.tanh(n * math.pi * long_side / (2 * short_side)) / n ** 5))
    bracket = 1.0 - (192.0 * short_side / (math.pi ** 5 * long_side)) * series
    return long_side * short_side ** 3 / 12.0 * bracket


def rect_shear_factor(width: float, depth: float) -> float:
    """`tau_max / (6 mu Q / (w h^2))` for a rectangular duct. A function of w/h alone.

    `width` is the IN-PLANE width across the flow, `depth` the etch depth, in any
    consistent unit. 1.0 for a very shallow duct, 1.4842 at w/h = 1.33 and 1.6013 at
    w/h = 1 - the two the audit measured. Multiply the shallow-duct formula by this and
    the result is the exact maximum wall shear at that flow rate, at any aspect ratio.
    """
    if not (width > 0 and depth > 0):
        raise SolveError(f"a rectangular duct needs width and depth > 0, got {width} x {depth}")
    ratio = width / depth
    key = int(round(ratio * 1e6))
    cached = _SHEAR_FACTOR_CACHE.get(key)
    if cached is not None:
        return cached
    w, h = ratio, 1.0
    factor = _tau_max_over_g(w, h) * w * h ** 2 / (6.0 * _mu_q_over_g(w, h))
    _SHEAR_FACTOR_CACHE[key] = factor
    return factor


def duct_shear_dyn_cm2(q_m3_s: float, width_m: float, depth_m: float, mu_pa_s: float) -> float:
    """Exact maximum wall shear in a rectangular duct, dyn/cm^2.

    `6 mu Q / (w h^2)` times the exact aspect-ratio correction above. Reported in
    dyn/cm^2 (1 Pa = 10 dyn/cm^2) because that is the unit all three contract ceilings in
    `contracts.shear_ceiling_for()` use.
    """
    shallow = 6.0 * mu_pa_s * abs(q_m3_s) / (width_m * depth_m * depth_m)
    return shallow * rect_shear_factor(width_m, depth_m) * DYN_CM2_PER_PA


def hele_shaw_shear_dyn_cm2(u_m_s: float, depth_m: float, mu_pa_s: float) -> float:
    """Wall shear under a depth-averaged velocity u in a Hele-Shaw cell: tau = 6 mu u / h.

    No aspect-ratio correction, deliberately. A Hele-Shaw face is a slice through a wide
    shallow chamber, where the flow really is parallel-plate between the floor and the lid,
    and the face's width is a MESHING CHOICE rather than a wall. Correcting for it would
    manufacture shear that is not there, and would make the answer depend on `grid_mm`.
    Ducts, mouths and slits - whose conductance the mesher built from the duct expression -
    get the correction; faces do not.
    """
    return 6.0 * mu_pa_s * abs(u_m_s) / depth_m * DYN_CM2_PER_PA


def hydraulic_diameter_m(width_m: float, depth_m: float) -> float:
    """D_h = 4A/P = 2 w h / (w + h) for a rectangle."""
    return 2.0 * width_m * depth_m / (width_m + depth_m)


def reynolds(u_m_s: float, width_m: float, depth_m: float, mu_pa_s: float, rho_kg_m3: float) -> float:
    """Re = rho u D_h / mu."""
    return rho_kg_m3 * abs(u_m_s) * hydraulic_diameter_m(width_m, depth_m) / mu_pa_s


def edge_cross_section_m(edge: Edge) -> tuple[float, float]:
    """(in-plane width, depth) of an edge's cross-section in metres, NaN when unknown.

    A weir slit's flow depth is its GAP, not the etch depth - flow through it goes as
    gap^3 - so a slit reporting shear at the chamber depth would under-read by orders of
    magnitude. `Edge.area_mm2` makes the same distinction.
    """
    width = float(edge.width_mm or 0.0) * 1e-3
    if edge.kind == "slit" and edge.gap_um:
        depth = float(edge.gap_um) * 1e-6
    else:
        depth = float(edge.depth_mm or 0.0) * 1e-3
    return (width if width > 0 else math.nan, depth if depth > 0 else math.nan)


# --- topology ----------------------------------------------------------------

@dataclass
class Topology:
    """A mesh's viscosity-free geometry, assembled once and reused for every step.

    Hand this back to `solve_steps(..., topology=topo)` to solve the same chip again - the
    fix-and-re-run loop re-meshes, but a tolerance sweep or a flow sweep does not.
    """

    node_ids: list[str]
    node_index: dict[str, int]
    is_cell: np.ndarray
    x_m: np.ndarray
    y_m: np.ndarray
    node_depth_m: np.ndarray
    node_area_m2: np.ndarray
    edge_ids: list[str]
    edge_kind: list[str]
    ea: np.ndarray
    eb: np.ndarray
    g_geom_m3: np.ndarray
    width_m: np.ndarray
    depth_m: np.ndarray
    component: np.ndarray
    port_node: dict[str, int]
    #: Per-edge geometry for shear and Re. These depend on the cross-section alone, never
    #: on the step, so they are built once here instead of per field - the duct series was
    #: the single most expensive thing in the solve while it ran per edge per step.
    shear_factor: np.ndarray = dc_field(default_factory=lambda: np.zeros(0))
    hydraulic_diameter: np.ndarray = dc_field(default_factory=lambda: np.zeros(0))
    is_hele_shaw: np.ndarray = dc_field(default_factory=lambda: np.zeros(0, dtype=bool))
    has_section: np.ndarray = dc_field(default_factory=lambda: np.zeros(0, dtype=bool))
    chip_id: str = ""
    notes: list[str] = dc_field(default_factory=list)

    @property
    def n_nodes(self) -> int:
        return len(self.node_ids)

    @property
    def n_edges(self) -> int:
        return len(self.edge_ids)

    @property
    def face_area_m2(self) -> np.ndarray:
        return self.width_m * self.depth_m


def _edge_g_geom_m3(edge: Edge, mesh: Mesh, notes: list[str]) -> float:
    """The edge's viscosity-free conductance in m^3, so any step's mu can be applied.

    `g_geom_m3` is the contract. A mesh that only filled in `conductance_m3_per_pa_s` is
    rescaled by the viscosity it was built at (`Mesh.meta['mu_pa_s']`, which is why that
    key is part of the mesh contract); without it a per-step viscosity cannot be applied
    and we refuse rather than silently solve the wrong fluid.

    Zero is legal and means SHUT - `Mesh.with_slit_gap_delta` closes a weir crest exactly
    that way, and that failure mode is the point of the +/-20 um sweep. A shut edge stays
    in the Field carrying zero flow instead of vanishing from the report.
    """
    g_geom = float(edge.g_geom_m3 or 0.0)
    conductance = float(edge.conductance_m3_per_pa_s or 0.0)

    if g_geom > 0:
        if conductance > 0:
            try:
                implied = g_geom / mesh.mu_pa_s
            except Exception:
                implied = None
            if implied is not None and abs(implied - conductance) > 1e-6 * max(implied, conductance):
                notes.append(
                    f"edge {edge.id!r}: g_geom_m3 / meta.mu_pa_s = {implied:.6g} but "
                    f"conductance_m3_per_pa_s = {conductance:.6g}. g_geom_m3 was used; the "
                    f"mesh's two fields disagree, which usually means a viscosity swap "
                    f"updated one and not the other."
                )
        return g_geom

    if conductance > 0:
        try:
            mu = mesh.mu_pa_s
        except Exception as exc:
            raise SolveError(
                f"edge {edge.id!r} has conductance_m3_per_pa_s but no g_geom_m3, and the "
                f"mesh meta has no usable mu_pa_s ({exc}), so it cannot be rescaled to this "
                f"step's fluid. Have mesh.py emit g_geom_m3 (= conductance x mu)."
            ) from None
        notes.append(f"edge {edge.id!r}: g_geom_m3 derived as conductance x meta.mu_pa_s")
        return conductance * float(mu)

    if g_geom < 0 or conductance < 0:
        raise SolveError(f"edge {edge.id!r} has a negative conductance ({g_geom}, {conductance})")
    return 0.0


def build_topology(mesh: Mesh) -> Topology:
    """Assemble the viscosity-free geometry once. Independent of every scenario."""
    if not isinstance(mesh, Mesh):
        mesh = Mesh.from_json(mesh) if isinstance(mesh, dict) else mesh
    nodes: list[Node] = list(mesh.nodes)
    edges: list[Edge] = list(mesh.edges)
    notes: list[str] = []

    node_ids = [n.id for n in nodes]
    index = {nid: i for i, nid in enumerate(node_ids)}
    if len(index) != len(node_ids):
        seen: set[str] = set()
        dupes = sorted({nid for nid in node_ids if nid in seen or seen.add(nid)})
        raise SolveError(f"mesh {mesh.chip_id!r} has duplicate node ids: {', '.join(dupes[:8])}")

    edge_ids: list[str] = []
    edge_kind: list[str] = []
    ea: list[int] = []
    eb: list[int] = []
    gg: list[float] = []
    ww: list[float] = []
    hh: list[float] = []
    shut: list[str] = []
    for e in edges:
        if e.a not in index or e.b not in index:
            missing = [x for x in (e.a, e.b) if x not in index]
            raise SolveError(f"edge {e.id!r} hangs off node(s) not in the mesh: {missing}")
        if e.a == e.b:
            notes.append(f"edge {e.id!r} joins {e.a!r} to itself and was dropped: it carries no flow")
            continue
        g_geom = _edge_g_geom_m3(e, mesh, notes)
        if not math.isfinite(g_geom):
            raise SolveError(f"edge {e.id!r} has a non-finite conductance ({g_geom})")
        if g_geom == 0.0:
            shut.append(e.id)
        w, h = edge_cross_section_m(e)
        edge_ids.append(e.id)
        edge_kind.append(e.kind)
        ea.append(index[e.a])
        eb.append(index[e.b])
        gg.append(g_geom)
        ww.append(w)
        hh.append(h)
    if shut:
        notes.append(
            f"{len(shut)} edge(s) are SHUT (zero conductance) and carry no flow: "
            f"{', '.join(shut[:6])}{'...' if len(shut) > 6 else ''}. Anything behind them "
            f"is hydraulically isolated."
        )

    port_node: dict[str, int] = {}
    for pid, nid in mesh.ports.items():
        if nid not in index:
            raise SolveError(f"port {pid!r} maps to node {nid!r}, which is not in the mesh")
        port_node[str(pid)] = index[nid]

    ea_arr = np.asarray(ea, dtype=np.int64)
    eb_arr = np.asarray(eb, dtype=np.int64)
    g_arr = np.asarray(gg, dtype=float)
    node_depth = np.asarray([float(n.depth_mm or 0.0) * 1e-3 for n in nodes], dtype=float)
    depth = np.asarray(hh, dtype=float)
    if depth.size:
        # A face with no depth of its own sits at the etch depth of the cells it joins.
        fallback = np.maximum(node_depth[ea_arr], node_depth[eb_arr])
        depth = np.where(np.isnan(depth) & (fallback > 0), fallback, depth)

    width = np.asarray(ww, dtype=float)
    has_section = np.isfinite(width) & np.isfinite(depth) & (width > 0) & (depth > 0)
    is_hele_shaw = np.asarray([k in HELE_SHAW_KINDS for k in edge_kind], dtype=bool)
    # One series evaluation per DISTINCT aspect ratio: a chip has a handful of channel
    # sizes and thousands of edges, and the factor is a function of w/h alone.
    shear_factor = np.ones(len(edge_ids), dtype=float)
    if has_section.any():
        ratio = np.where(has_section, width / np.where(has_section, depth, 1.0), 1.0)
        duct = has_section & ~is_hele_shaw
        for value in np.unique(ratio[duct]) if duct.any() else ():
            shear_factor[duct & (ratio == value)] = rect_shear_factor(float(value), 1.0)
    hydraulic_diameter = np.where(
        has_section, 2.0 * width * depth / np.where(has_section, width + depth, 1.0), math.nan)

    return Topology(
        node_ids=node_ids,
        node_index=index,
        is_cell=np.asarray([n.kind == "cell" for n in nodes], dtype=bool),
        x_m=np.asarray([float(n.x_mm or 0.0) * 1e-3 for n in nodes], dtype=float),
        y_m=np.asarray([float(n.y_mm or 0.0) * 1e-3 for n in nodes], dtype=float),
        node_depth_m=node_depth,
        node_area_m2=np.asarray([float(n.area_mm2 or 0.0) * 1e-6 for n in nodes], dtype=float),
        edge_ids=edge_ids,
        edge_kind=edge_kind,
        ea=ea_arr,
        eb=eb_arr,
        g_geom_m3=g_arr,
        width_m=width,
        depth_m=depth,
        shear_factor=shear_factor,
        hydraulic_diameter=hydraulic_diameter,
        is_hele_shaw=is_hele_shaw,
        has_section=has_section,
        # Components are built from edges that actually conduct: a node behind a shut weir
        # is isolated, and treating it as connected would make the matrix singular.
        component=_components(len(node_ids), ea_arr[g_arr > 0], eb_arr[g_arr > 0]),
        port_node=port_node,
        chip_id=mesh.chip_id,
        notes=notes,
    )


def _components(n: int, ea: np.ndarray, eb: np.ndarray) -> np.ndarray:
    """Connected-component label per node (union-find). Drives the solvability checks."""
    parent = list(range(n))

    def find(i: int) -> int:
        root = i
        while parent[root] != root:
            root = parent[root]
        while parent[i] != root:
            parent[i], i = root, parent[i]
        return root

    for a, b in zip(ea.tolist(), eb.tolist()):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    return np.asarray([find(i) for i in range(n)], dtype=np.int64)


# --- boundary conditions -----------------------------------------------------

@dataclass
class _Boundary:
    """One scenario's boundary conditions, resolved onto node indices."""

    dirichlet: dict[int, float]        # node index -> pressure, Pa
    injection: np.ndarray              # m^3/s into each node, from flow ports
    pinned: list[int]                  # gauge references we had to add ourselves
    port_kind: dict[str, str]
    port_q: dict[str, float]           # m^3/s prescribed AT EACH PORT, not per node
    notes: list[str]


def _resolve_boundary(topo: Topology, scenario: Scenario) -> _Boundary:
    step = getattr(scenario, "step", "?")
    dirichlet: dict[int, float] = {}
    injection = np.zeros(topo.n_nodes, dtype=float)
    port_kind: dict[str, str] = {pid: "closed" for pid in topo.port_node}
    port_q: dict[str, float] = {pid: 0.0 for pid in topo.port_node}
    notes: list[str] = []

    for pid, raw in dict(getattr(scenario, "ports", {}) or {}).items():
        pid = str(pid)
        if pid not in topo.port_node:
            raise SolveError(
                f"step {step!r}: port {pid!r} is not in mesh {topo.chip_id or 'this chip'}, "
                f"which has {', '.join(sorted(topo.port_node)) or '(no ports)'}"
            )
        state = as_port_state(raw)
        node = topo.port_node[pid]
        port_kind[pid] = state.type
        if state.is_closed:
            continue
        if state.is_flow:
            injection[node] += state.q_m3_s
            port_q[pid] = state.q_m3_s
        else:
            p = float(state.p_pa or 0.0)
            if node in dirichlet and abs(dirichlet[node] - p) > 1e-12:
                raise SolveError(
                    f"step {step!r}: two pressure ports share node "
                    f"{topo.node_ids[node]!r} but disagree ({dirichlet[node]} vs {p} Pa)"
                )
            dirichlet[node] = p

    # A pressure reference per connected component, or a reason it does not need one.
    pinned: list[int] = []
    scale = max(float(np.abs(injection).sum()), 1e-18)
    port_of_node = {node: pid for pid, node in topo.port_node.items()}
    for comp in np.unique(topo.component):
        members = np.flatnonzero(topo.component == comp)
        if any(int(m) in dirichlet for m in members):
            continue
        net = float(injection[members].sum())
        if abs(net) > 1e-12 + 1e-9 * scale:
            fed = [
                f"{port_of_node.get(int(m), topo.node_ids[int(m)])} "
                f"{injection[int(m)] * M3_S_TO_UL_MIN:+.4g} uL/min"
                for m in members if injection[int(m)] != 0.0
            ]
            raise SolveError(
                f"step {step!r} has no steady solution. The part of "
                f"{topo.chip_id or 'the chip'} containing "
                f"{', '.join(topo.node_ids[int(m)] for m in members[:4])}"
                f"{', ...' if members.size > 4 else ''} has NO pressure port, so nothing can "
                f"absorb its net inflow of {net * M3_S_TO_UL_MIN:+.6g} uL/min "
                f"({'; '.join(fed)}). Liquid is being pushed in with nowhere to go: open a "
                f"port with type 'pressure' (a vent is p_pa = 0) or make its flow ports sum "
                f"to zero. Refusing to return a field."
            )
        pin = int(members[0])
        dirichlet[pin] = 0.0
        pinned.append(pin)
        if np.any(injection[members] != 0.0):
            notes.append(
                f"no pressure port on the part of the chip containing "
                f"{topo.node_ids[pin]!r}: its flow ports balance to zero, so q is exact but "
                f"p is GAUGE-RELATIVE - p[{topo.node_ids[pin]}] = 0 by choice, not physics. "
                f"Any absolute-pressure finding (leak, capsule escape) is meaningless here."
            )
        else:
            notes.append(
                f"the part of the chip containing {topo.node_ids[pin]!r} has no drive and no "
                f"pressure port: zero flow everywhere in it, pressure undefined, reported 0."
            )
    shared = [
        nodes for nodes in (
            [pid for pid, n in topo.port_node.items()
             if n == node and port_kind[pid] == "pressure"]
            for node in {topo.port_node[p] for p in port_kind if port_kind[p] == "pressure"}
        ) if len(nodes) > 1
    ]
    for group in shared:
        notes.append(
            f"ports {', '.join(sorted(group))} share one node, so the flow through each is "
            f"reported as an equal share of the node's total - the mesh cannot tell them apart."
        )
    return _Boundary(dirichlet, injection, pinned, port_kind, port_q, notes)


# --- linear algebra ----------------------------------------------------------

@dataclass
class _Reduced:
    """The reduced SPD system for one Dirichlet pattern, ready for many right-hand sides."""

    unknown: np.ndarray        # node indices, in row order
    diag: np.ndarray           # geometric diagonal, m^3
    ia: np.ndarray             # interior edges: row of endpoint a
    ib: np.ndarray             # interior edges: row of endpoint b
    ig: np.ndarray             # interior edge g_geom
    ba: np.ndarray             # boundary edges: row of the unknown endpoint
    bd: np.ndarray             # boundary edges: node index of the Dirichlet endpoint
    bg: np.ndarray             # boundary edge g_geom

    @property
    def n(self) -> int:
        return int(self.unknown.size)


def _reduce(topo: Topology, dirichlet_nodes: Sequence[int]) -> _Reduced:
    fixed = np.zeros(topo.n_nodes, dtype=bool)
    if len(dirichlet_nodes):
        fixed[np.asarray(dirichlet_nodes, dtype=np.int64)] = True
    unknown = np.flatnonzero(~fixed)
    row = np.full(topo.n_nodes, -1, dtype=np.int64)
    row[unknown] = np.arange(unknown.size, dtype=np.int64)

    g = topo.g_geom_m3
    diag = np.zeros(topo.n_nodes, dtype=float)
    if g.size:
        diag += np.bincount(topo.ea, weights=g, minlength=topo.n_nodes)
        diag += np.bincount(topo.eb, weights=g, minlength=topo.n_nodes)

    if g.size:
        a_free, b_free = ~fixed[topo.ea], ~fixed[topo.eb]
    else:
        a_free = b_free = np.zeros(0, dtype=bool)
    interior = a_free & b_free
    bnd_a = a_free & ~b_free
    bnd_b = b_free & ~a_free

    return _Reduced(
        unknown=unknown,
        diag=diag[unknown],
        ia=row[topo.ea[interior]],
        ib=row[topo.eb[interior]],
        ig=g[interior],
        ba=np.concatenate([row[topo.ea[bnd_a]], row[topo.eb[bnd_b]]]),
        bd=np.concatenate([topo.eb[bnd_a], topo.ea[bnd_b]]),
        bg=np.concatenate([g[bnd_a], g[bnd_b]]),
    )


def _csr(red: _Reduced) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """CSR of the reduced Laplacian, for a loop-free sparse matvec via `add.reduceat`.

    Every row carries its diagonal entry even when that entry is zero, so the row pointer
    is strictly increasing - `reduceat` does not behave on repeated indices.
    """
    n = red.n
    diag_rows = np.arange(n, dtype=np.int64)
    rows = np.concatenate([red.ia, red.ib, diag_rows])
    cols = np.concatenate([red.ib, red.ia, diag_rows])
    vals = np.concatenate([-red.ig, -red.ig, red.diag])
    order = np.argsort(rows, kind="stable")
    indptr = np.zeros(n + 1, dtype=np.int64)
    np.cumsum(np.bincount(rows, minlength=n), out=indptr[1:])
    return indptr, cols[order], vals[order]


def _matvec(indptr: np.ndarray, cols: np.ndarray, vals: np.ndarray, X: np.ndarray) -> np.ndarray:
    return np.add.reduceat(vals[:, None] * X[cols], indptr[:-1], axis=0)


def _cg_jacobi(
    red: _Reduced,
    B: np.ndarray,
    rtol: float = CG_RTOL,
    maxiter: int | None = None,
) -> tuple[np.ndarray, int, np.ndarray]:
    """Jacobi-preconditioned CG on the reduced Laplacian, all right-hand sides at once.

    The reduced matrix is symmetric positive definite - a graph Laplacian with at least one
    node grounded - so CG is the right method and needs no restarts. The columns are
    independent problems sharing one matrix, so each keeps its own alpha and beta and a
    converged column is frozen rather than dragging the others round another iteration.
    """
    n, k = B.shape
    if maxiter is None:
        maxiter = max(CG_MAXITER_FLOOR, CG_MAXITER_SQRT_FACTOR * int(math.sqrt(max(n, 1))))
    if np.any(red.diag <= 0):
        bad = int(np.flatnonzero(red.diag <= 0)[0])
        raise SolveError(
            f"reduced row {bad} has a non-positive diagonal ({red.diag[bad]:.3g}): that node "
            f"has no conducting edge, so the system is singular and Jacobi has nothing to "
            f"divide by. mesh.check() should have caught an orphan."
        )
    indptr, cols, vals = _csr(red)
    inv_diag = (1.0 / red.diag)[:, None]

    X = np.zeros((n, k), dtype=float)
    R = B.copy()
    bnorm = np.linalg.norm(B, axis=0)
    bnorm_safe = np.maximum(bnorm, np.finfo(float).tiny)
    target = np.maximum(rtol * bnorm, np.finfo(float).tiny)
    active = bnorm > 0.0            # an all-zero column is already solved by X = 0
    Z = R * inv_diag
    P = Z.copy()
    rz = np.einsum("ij,ij->j", R, Z)
    best = np.full(k, np.inf)
    used = 0
    stalled = False
    flat_blocks = 0

    def true_residual() -> np.ndarray:
        return np.linalg.norm(B - _matvec(indptr, cols, vals, X), axis=0)

    while used < maxiter and active.any():
        used += 1
        AP = _matvec(indptr, cols, vals, P)
        pap = np.einsum("ij,ij->j", P, AP)
        alpha = np.where(active & (pap > 0), rz / np.where(pap > 0, pap, 1.0), 0.0)
        X += alpha * P
        R -= alpha * AP
        recurrence_done = np.linalg.norm(R, axis=0) <= target
        # The updated residual drifts away from B - A X as the iteration proceeds, and on
        # an ill-conditioned mesh it ends up OPTIMISTIC - it reported 1e-11 where the true
        # residual had stalled at 2e-10. So confirm with the real thing before stopping,
        # and periodically regardless, which is also where stagnation gets detected.
        if recurrence_done.all() or used % CG_TRUE_RESIDUAL_EVERY == 0:
            residual = true_residual()
            active = residual > target
            if not active.any():
                break
            progressed = bool(np.any(residual[active] < CG_STALL_FACTOR * best[active]))
            # "Good enough" means inside the audit limit this solve will be judged by.
            usable = bool(np.all(residual[active] <= RESIDUAL_RTOL * bnorm_safe[active]))
            if progressed or not usable:
                flat_blocks = 0
            else:
                flat_blocks += 1
                if flat_blocks >= CG_STALL_PATIENCE:
                    stalled = True
                    break
            best = np.minimum(best, residual)
        else:
            active = active & ~recurrence_done
            if not active.any():
                continue
        Z = R * inv_diag
        rz_new = np.einsum("ij,ij->j", R, Z)
        beta = np.where(active & (rz > 0), rz_new / np.where(rz > 0, rz, 1.0), 0.0)
        P = Z + beta * P
        rz = rz_new

    relative = true_residual() / np.maximum(bnorm, np.finfo(float).tiny)
    worst_column = int(np.argmax(relative)) if relative.size else 0
    worst = float(relative[worst_column]) if relative.size else 0.0
    if worst > RESIDUAL_RTOL:
        raise SolveError(
            f"conjugate gradient did NOT converge: after {used} iterations on {n} unknowns "
            f"the worst of {k} right-hand side(s) still has a relative residual of "
            f"{worst:.3e}, above the {RESIDUAL_RTOL:.0e} audit limit"
            + (" and no longer improving" if stalled else f" (target {rtol:.1e})")
            + ". A conductance span of many orders - a weir slit next to a duct - defeats "
            "Jacobi preconditioning. Do NOT trust any field from this solve."
        )
    return X, used, relative


def use_dense_solver(unknowns: int, right_hand_sides: int) -> bool:
    """Dense LU or Jacobi-PCG, for a system of this size with this many steps batched.

    A heuristic, and a measured one - see the thresholds above. Both paths produce the
    same answer and both have their residual audited afterwards, so getting this wrong
    costs time and nothing else.
    """
    if unknowns <= DENSE_MAX_UNKNOWNS:
        return True
    return right_hand_sides >= DENSE_RHS_AMORTISE and unknowns <= DENSE_MAX_UNKNOWNS_BATCHED


def _residual_rel(red: _Reduced, B: np.ndarray, X: np.ndarray) -> np.ndarray:
    """||A X - B|| / ||B||, per column. The audit every path has to pass."""
    indptr, cols, vals = _csr(red)
    return np.linalg.norm(B - _matvec(indptr, cols, vals, X), axis=0) / np.maximum(
        np.linalg.norm(B, axis=0), np.finfo(float).tiny
    )


def _dedupe_columns(B: np.ndarray) -> tuple[np.ndarray, list[tuple[int, float]]]:
    """Split B into its distinct DIRECTIONS and a scale per column.

    A protocol usually runs the same ports at a series of flow rates, and the system is
    linear: `A x = c b` has solution `c A^-1 b`, so twenty steps that differ only in
    uL/min are ONE solve and nineteen multiplications. This is exact arithmetic, not an
    approximation - it just declines to re-derive the same answer.

    Returns the matrix of unit columns and, per original column, which unit column it is
    and by how much it is scaled (`(-1, 0.0)` for an all-zero right-hand side).
    """
    n, k = B.shape
    units: list[np.ndarray] = []
    mapping: list[tuple[int, float]] = []
    for j in range(k):
        column = B[:, j]
        scale = float(np.max(np.abs(column))) if n else 0.0
        if scale == 0.0:
            mapping.append((-1, 0.0))
            continue
        unit = column / scale
        for index, candidate in enumerate(units):
            if np.allclose(unit, candidate, rtol=1e-14, atol=1e-16):
                mapping.append((index, scale))
                break
        else:
            units.append(unit)
            mapping.append((len(units) - 1, scale))
    return (np.stack(units, axis=1) if units else np.zeros((n, 0))), mapping


def _solve_reduced(red: _Reduced, B: np.ndarray) -> tuple[np.ndarray, dict]:
    """Solve `L_geom p = b` for every column of B, in symmetrically scaled coordinates.

    The raw matrix is badly SCALED, not merely ill-conditioned: a weir slit's conductance
    and a chamber face's differ by orders, so the diagonal spans orders too. Solved raw,
    even dense LU left a relative residual of 2e-11 and a mass imbalance of 1e-11 of the
    through-flow - the big rows swamping the small ones - which is nowhere near what the
    arithmetic can do.

    So substitute `p = D^-1/2 y` and solve `D^-1/2 L D^-1/2 y = D^-1/2 b`, whose matrix
    has a unit diagonal. It is a congruence by a positive diagonal, so it is exact, it
    preserves symmetry and positive definiteness, and it costs one multiply per entry.
    Both paths then land at machine precision, and CG needs fewer iterations because the
    scaling IS its Jacobi preconditioner, applied once instead of every iteration.
    """
    n, k = B.shape
    if n == 0:
        return np.zeros((0, k)), {
            "method": "none (every node is pressure-controlled)", "unknowns": 0, "rhs": k,
            "distinct_rhs": 0, "iterations": 0, "residual_rel_max": 0.0, "seconds": 0.0,
        }
    if np.any(red.diag <= 0):
        bad = int(np.flatnonzero(red.diag <= 0)[0])
        raise SolveError(
            f"reduced row {bad} has a non-positive diagonal ({red.diag[bad]:.3g}): that node "
            f"has no conducting edge, so the system is singular. mesh.check() reports such a "
            f"node as an orphan."
        )
    scale = 1.0 / np.sqrt(red.diag)
    scaled = _Reduced(
        unknown=red.unknown,
        diag=np.ones(n, dtype=float),
        ia=red.ia,
        ib=red.ib,
        ig=red.ig * scale[red.ia] * scale[red.ib],
        ba=red.ba,
        bd=red.bd,
        bg=red.bg,
    )
    Y, info = _solve_scaled(scaled, B * scale[:, None])
    return Y * scale[:, None], info


def _solve_scaled(red: _Reduced, B: np.ndarray) -> tuple[np.ndarray, dict]:
    """Solve a unit-diagonal reduced system, reusing proportional columns, then audit."""
    n, k = B.shape
    started = time.perf_counter()
    if n and k > 1:
        units, mapping = _dedupe_columns(B)
        if units.shape[1] < k:
            solved, info = _solve_scaled(red, units) if units.size else (
                np.zeros((n, 0)), {"method": "none (every right-hand side is zero)",
                                   "unknowns": n, "rhs": 0, "distinct_rhs": 0,
                                   "iterations": 0, "residual_rel_max": 0.0,
                                   "seconds": 0.0})
            X = np.zeros((n, k), dtype=float)
            for j, (index, factor) in enumerate(mapping):
                if index >= 0:
                    X[:, j] = factor * solved[:, index]
            # Audit the REUSED columns against the right-hand sides they claim to solve,
            # so the saving is verified rather than trusted.
            worst = float(np.max(_residual_rel(red, B, X)))
            if worst > RESIDUAL_RTOL:
                raise SolveError(
                    f"reusing {k} right-hand sides from {units.shape[1]} distinct "
                    f"direction(s) left a relative residual of {worst:.3e}, above the "
                    f"{RESIDUAL_RTOL:.0e} audit limit: two steps were treated as "
                    f"proportional when they are not. Refusing to report them."
                )
            return X, dict(info, rhs=k, distinct_rhs=units.shape[1],
                           residual_rel_max=max(info["residual_rel_max"], worst),
                           seconds=time.perf_counter() - started)
    if use_dense_solver(n, k):
        A = np.zeros((n, n), dtype=float)
        A[np.arange(n), np.arange(n)] = red.diag
        if red.ig.size:
            np.add.at(A, (red.ia, red.ib), -red.ig)
            np.add.at(A, (red.ib, red.ia), -red.ig)
        try:
            X = np.linalg.solve(A, B)
        except np.linalg.LinAlgError as exc:
            raise SolveError(
                f"the reduced {n}x{n} system is singular ({exc}): some part of the mesh has "
                f"no pressure reference reachable through a conducting edge."
            ) from None
        method, iterations = f"dense LU (numpy.linalg.solve, n={n})", 0
        relative = np.linalg.norm(B - A @ X, axis=0) / np.maximum(
            np.linalg.norm(B, axis=0), np.finfo(float).tiny
        )
    else:
        X, iterations, relative = _cg_jacobi(red, B)
        method = f"Jacobi-preconditioned CG (n={n}, rtol={CG_RTOL:.0e})"

    worst = float(np.max(relative)) if relative.size else 0.0
    if worst > RESIDUAL_RTOL:
        raise SolveError(
            f"{method} left a relative residual of {worst:.3e} on a {n}-unknown system, "
            f"above the {RESIDUAL_RTOL:.0e} audit limit. These pressures are not a solution "
            f"of this network; refusing to report them."
        )
    return X, {
        "method": method,
        "unknowns": n,
        "rhs": k,
        "distinct_rhs": k,
        "iterations": int(iterations),
        "residual_rel_max": worst,
        "seconds": time.perf_counter() - started,
    }


# --- derived fields ----------------------------------------------------------

def _cell_velocity(topo: Topology, q: np.ndarray) -> np.ndarray:
    """In-plane velocity per node from the incident face fluxes, m/s, least squares.

    For each face of a cell the outward normal velocity is `Q_f / A_f`, with the normal the
    unit vector from this cell's centre towards its neighbour's. Fitting `u . n_f = v_f`
    over all of a cell's faces in least squares gives the depth-averaged cell velocity:
    the network solves for FLUXES, so velocity is a reconstruction and not a primary
    unknown, and this is the standard one for a cell-centred flux field.

    Degenerate cells - a single face, or every face along one line - have a rank-1 normal
    matrix. They get the velocity in the one direction their faces actually resolve,
    rather than a direction invented by regularisation.
    """
    n = topo.n_nodes
    u = np.zeros((n, 2), dtype=float)
    if not topo.n_edges:
        return u

    node = np.concatenate([topo.ea, topo.eb])
    other = np.concatenate([topo.eb, topo.ea])
    outward = np.concatenate([q, -q])                      # positive leaves this node
    area = np.concatenate([topo.face_area_m2, topo.face_area_m2])

    dx = topo.x_m[other] - topo.x_m[node]
    dy = topo.y_m[other] - topo.y_m[node]
    length = np.hypot(dx, dy)
    ok = topo.is_cell[node] & (length > 0) & np.isfinite(area) & (area > 0)
    if not ok.any():
        return u

    nx, ny = dx[ok] / length[ok], dy[ok] / length[ok]
    v = outward[ok] / area[ok]
    idx = node[ok]
    sxx = np.bincount(idx, weights=nx * nx, minlength=n)
    sxy = np.bincount(idx, weights=nx * ny, minlength=n)
    syy = np.bincount(idx, weights=ny * ny, minlength=n)
    rx = np.bincount(idx, weights=nx * v, minlength=n)
    ry = np.bincount(idx, weights=ny * v, minlength=n)

    det = sxx * syy - sxy * sxy
    trace = sxx + syy
    full = det > 1e-9 * np.maximum(trace * trace, np.finfo(float).tiny)
    with np.errstate(invalid="ignore", divide="ignore"):
        u[full, 0] = (syy[full] * rx[full] - sxy[full] * ry[full]) / det[full]
        u[full, 1] = (sxx[full] * ry[full] - sxy[full] * rx[full]) / det[full]

    for i in np.flatnonzero(~full & (trace > 0)):
        # Rank-1: M = lam vhat vhat^T, so vhat is the only direction the faces resolve.
        lam = float(trace[i])
        vec = np.array([sxy[i], lam - sxx[i]]) if abs(sxy[i]) > 1e-30 else np.array([1.0, 0.0])
        norm = float(np.hypot(*vec))
        if norm <= 0 or lam <= 0:
            continue
        vhat = vec / norm
        u[i] = (float(vhat @ np.array([rx[i], ry[i]])) / lam) * vhat
    return u


def _edge_shear_and_re(topo: Topology, q: np.ndarray, fluid: Fluid) -> tuple[dict[str, float], dict[str, float]]:
    """Per-edge wall shear (dyn/cm^2) and Reynolds number, skipping unknown geometry.

    Vectorised over edges using the per-edge factors the topology already holds. `shear`
    is `6 mu Q / (w h^2)` times the exact duct correction for duct, mouth and slit edges,
    and exactly `6 mu u / h` for Hele-Shaw faces, whose correction factor is held at 1.
    """
    ok = topo.has_section
    if not ok.any():
        return {}, {}
    w = np.where(ok, topo.width_m, 1.0)
    h = np.where(ok, topo.depth_m, 1.0)
    speed = np.abs(q) / (w * h)
    shallow = 6.0 * fluid.mu_pa_s * speed / h * DYN_CM2_PER_PA
    shear_all = shallow * topo.shear_factor
    re_all = fluid.rho_kg_m3 * speed * topo.hydraulic_diameter / fluid.mu_pa_s
    # `.tolist()` then zip, rather than indexing the arrays element by element: pulling
    # numpy scalars out one at a time dominated the whole solve on a 4500-edge mesh.
    keep = ok.tolist()
    shear = {eid: value for eid, value, good
             in zip(topo.edge_ids, shear_all.tolist(), keep) if good}
    re = {eid: value for eid, value, good
          in zip(topo.edge_ids, re_all.tolist(), keep) if good}
    return shear, re


# --- the solve ---------------------------------------------------------------

def solve_steps(
    mesh: Mesh,
    scenarios: Iterable[Scenario],
    topology: Topology | None = None,
) -> list[Field]:
    """Every scenario on one mesh: one assembly, one factorisation per Dirichlet pattern.

    This is the entry point for a protocol. A chip's geometry is assembled once and the
    whole protocol is solved as a multiple-right-hand-side problem, which is what keeps
    the README's fix-and-re-run loop in milliseconds. Returns a Field per scenario, in
    the order given.
    """
    scenarios = [as_scenario(s) for s in scenarios]
    topo = topology if topology is not None else build_topology(mesh)
    if not scenarios:
        return []

    boundaries = [_resolve_boundary(topo, s) for s in scenarios]
    fluids = [fluid_for(s) for s in scenarios]

    groups: dict[tuple[int, ...], list[int]] = {}
    for i, bc in enumerate(boundaries):
        groups.setdefault(tuple(sorted(bc.dirichlet)), []).append(i)

    pressures: list[np.ndarray] = [np.zeros(topo.n_nodes) for _ in scenarios]
    stats: list[dict] = [{} for _ in scenarios]
    for key, members in groups.items():
        red = _reduce(topo, key)
        B = np.zeros((red.n, len(members)), dtype=float)
        for col, i in enumerate(members):
            bc = boundaries[i]
            # L_geom p = mu f, with the Dirichlet columns eliminated onto the right.
            B[:, col] = fluids[i].mu_pa_s * bc.injection[red.unknown]
            if red.bg.size:
                p_d = np.asarray([bc.dirichlet[int(d)] for d in red.bd], dtype=float)
                B[:, col] += np.bincount(red.ba, weights=red.bg * p_d, minlength=red.n)
        X, info = _solve_reduced(red, B)
        for col, i in enumerate(members):
            p = np.zeros(topo.n_nodes, dtype=float)
            p[red.unknown] = X[:, col]
            for node, value in boundaries[i].dirichlet.items():
                p[node] = value
            pressures[i] = p
            stats[i] = dict(info, batched_with=len(members))

    return [
        _assemble_field(topo, scenarios[i], boundaries[i], fluids[i], pressures[i], stats[i])
        for i in range(len(scenarios))
    ]


def solve_step(mesh: Mesh, scenario: Scenario, topology: Topology | None = None) -> Field:
    """One scenario on one mesh -> one Field."""
    return solve_steps(mesh, [scenario], topology=topology)[0]


def _assemble_field(
    topo: Topology,
    scenario: Scenario,
    bc: _Boundary,
    fluid: Fluid,
    p: np.ndarray,
    stats: dict,
) -> Field:
    """Turn nodal pressures into the Field of the README's module-contracts table."""
    g = topo.g_geom_m3 / fluid.mu_pa_s
    q = g * (p[topo.ea] - p[topo.eb]) if topo.n_edges else np.zeros(0)

    # Mass conservation. Net inflow at a node is its prescribed injection minus what its
    # edges carry away. At a Dirichlet node the port absorbs the difference, so that
    # node's "imbalance" IS the port flow and is excluded from the imbalance measure.
    divergence = np.zeros(topo.n_nodes, dtype=float)
    if topo.n_edges:
        divergence += np.bincount(topo.ea, weights=q, minlength=topo.n_nodes)
        divergence -= np.bincount(topo.eb, weights=q, minlength=topo.n_nodes)
    net_in = bc.injection - divergence
    interior = np.ones(topo.n_nodes, dtype=bool)
    if bc.dirichlet:
        interior[list(bc.dirichlet)] = False
    imbalance_max = float(np.max(np.abs(net_in[interior]))) if interior.any() else 0.0

    # Flow INTO the chip at each port. A flow port carries what it was told to; a pressure
    # port carries whatever the edges at its node did not account for. Read per PORT and
    # not per node: two ports can share a node, and `injection[node]` would then credit a
    # capped port with its neighbour's pump.
    pressure_ports_at: dict[int, list[str]] = {}
    for pid, kind in bc.port_kind.items():
        if kind == "pressure":
            pressure_ports_at.setdefault(topo.port_node[pid], []).append(pid)
    port_flow: dict[str, float] = {}
    for pid, node in topo.port_node.items():
        kind = bc.port_kind.get(pid, "closed")
        if kind == "pressure":
            siblings = pressure_ports_at[node]
            port_flow[pid] = float(divergence[node] - bc.injection[node]) / len(siblings)
        else:
            port_flow[pid] = float(bc.port_q.get(pid, 0.0))
    q_in = float(sum(v for v in port_flow.values() if v > 0))
    q_out = float(-sum(v for v in port_flow.values() if v < 0))

    u = _cell_velocity(topo, q)
    shear, re = _edge_shear_and_re(topo, q, fluid)

    u_mm_s: dict[str, list[float]] = {}
    cell_shear = np.where(topo.node_depth_m > 0,
                          6.0 * fluid.mu_pa_s * np.hypot(u[:, 0], u[:, 1])
                          / np.where(topo.node_depth_m > 0, topo.node_depth_m, 1.0)
                          * DYN_CM2_PER_PA, math.nan)
    for nid, ux, uy, is_cell, tau in zip(topo.node_ids, (u[:, 0] * 1e3).tolist(),
                                         (u[:, 1] * 1e3).tolist(), topo.is_cell.tolist(),
                                         cell_shear.tolist()):
        if not is_cell:
            continue
        u_mm_s[nid] = [ux, uy]
        if tau == tau:                      # not NaN: the node has a depth
            shear[nid] = tau

    over = sorted((eid for eid, value in re.items() if value > RE_VALIDITY_LIMIT),
                  key=lambda e: -re[e])
    notes = list(topo.notes) + list(bc.notes)
    if over:
        notes.append(
            f"Re > {RE_VALIDITY_LIMIT:g} on {len(over)} edge(s) (worst: "
            f"{', '.join(f'{e} Re={re[e]:.1f}' for e in over[:4])}): OUTSIDE the Stokes / "
            f"Hele-Shaw model this solver implements. analyse.py must report these as "
            f"'unmodelled' rather than trust the flow, shear or pressure through them."
        )

    return Field(
        p_pa=dict(zip(topo.node_ids, p.tolist())),
        q_m3_s=dict(zip(topo.edge_ids, q.tolist())),
        u_mm_s=u_mm_s,
        shear_dyn_cm2=shear,
        re=re,
        residual=Residual(mass_imbalance_max=imbalance_max, q_in=q_in, q_out=q_out),
        meta={
            "chip_id": topo.chip_id or getattr(scenario, "chip_id", "") or "",
            "step": getattr(scenario, "step", None),
            "fluid": fluid.name,
            "temp_c": fluid.temp_c,
            "mu_pa_s": fluid.mu_pa_s,
            "mu_source": fluid.mu_source,
            "rho_kg_m3": fluid.rho_kg_m3,
            "rho_source": fluid.rho_source,
            "rho_is_assumption": fluid.rho_is_assumption,
            "nodes": topo.n_nodes,
            "edges": topo.n_edges,
            "port_flow_ul_min": {pid: v * M3_S_TO_UL_MIN for pid, v in sorted(port_flow.items())},
            "port_kind": dict(sorted(bc.port_kind.items())),
            "pressure_reference": sorted(topo.node_ids[n] for n in bc.dirichlet),
            "pressure_is_gauge_relative": bool(bc.pinned),
            "mass_imbalance_max_ul_min": imbalance_max * M3_S_TO_UL_MIN,
            "q_in_ul_min": q_in * M3_S_TO_UL_MIN,
            "q_out_ul_min": q_out * M3_S_TO_UL_MIN,
            "q_in_minus_q_out_ul_min": (q_in - q_out) * M3_S_TO_UL_MIN,
            "re_over_limit": over,
            "shear_model": (
                "duct|mouth|slit: tau = 6 mu Q / (w h^2) x the exact rectangular-duct "
                "series correction (1.4842 at w/h = 1.33, 1.6013 at w/h = 1); hs_face and "
                "cells: tau = 6 mu u / h, Hele-Shaw, uncorrected"
            ),
            "solver": stats,
            "notes": notes,
            "assumptions": list(fluid.notes),
        },
    )


# --- self-check --------------------------------------------------------------

def _self_check() -> int:
    """`python assay2chip/cfd/solve.py` - the analytic cases, printed."""
    mu = contracts.viscosity_37c_pa_s().pa_s
    length_mm, width_mm, depth_mm, q_ul_min = 10.0, 0.8, 0.3, 11.0
    w, h = width_mm * 1e-3, depth_mm * 1e-3
    g_geom = w * h ** 3 * (1 - 0.63 * h / w) / (12 * length_mm * 1e-3)
    mesh = Mesh(
        nodes=[Node("a", kind="port", x_mm=0.0, y_mm=0.0, depth_mm=depth_mm),
               Node("b", kind="port", x_mm=length_mm, y_mm=0.0, depth_mm=depth_mm)],
        edges=[Edge("e1", a="a", b="b", kind="duct", g_geom_m3=g_geom,
                    length_mm=length_mm, width_mm=width_mm, depth_mm=depth_mm)],
        ports={"p_in": "a", "p_out": "b"},
        meta={"chip_id": "selfcheck", "mu_pa_s": mu},
    )
    scenario = Scenario(
        step="single duct",
        ports={"p_in": PortState.flow(q_ul_min), "p_out": PortState.vent()},
        fluid="medium", temp_c=37.0,
    )
    f = solve_step(mesh, scenario)
    analytic = (12 * mu * length_mm * 1e-3 / (w * h ** 3 * (1 - 0.63 * h / w))) * q_ul_min * UL_MIN_TO_M3_S
    print(f"single duct {length_mm} x {width_mm} x {depth_mm} mm at {q_ul_min} uL/min, "
          f"mu = {mu} Pa s ({contracts.viscosity_37c_pa_s().key})")
    print(f"  analytic dP {analytic:.6f} Pa    solver dP {f.p_pa['a']:.6f} Pa")
    print(f"  wall shear  {f.shear_dyn_cm2['e1']:.6f} dyn/cm2   Re {f.re['e1']:.4f}   "
          f"(w/h = {width_mm / depth_mm:.2f}, correction x{rect_shear_factor(w, h):.4f})")
    print(f"  mass imbalance {f.residual.mass_imbalance_max:.3e} m3/s, closure "
          f"{f.residual.closure:.3e}")
    print("\nexact rectangular-duct shear correction, tau_max / (6 mu Q / w h^2):")
    for ar in (1.0, 1.33, 2.0, 4.0, 10.0, 100.0):
        print(f"  w/h = {ar:6.2f}   x{rect_shear_factor(ar, 1.0):.4f}")
    for note in f.meta["assumptions"]:
        print(f"\nASSUMPTION: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_self_check())

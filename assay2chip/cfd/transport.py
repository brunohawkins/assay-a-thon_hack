"""assay2chip/cfd/transport.py: scalar transport on the solved field.

This is how traps are actually found. `assay2chip/cfd/README.md` is explicit about why:
depth-averaged Hele-Shaw flow is a potential flow in plane, so it has NO recirculation
eddies and a closed-streamline test can never fire in a chamber. What is left is
**residence time and velocity magnitude**, and both of those are scalar transport problems
on the field `solve.py` already produced.

Four things live here.

1. MEAN AGE (`mean_age`). The steady age equation, Spalding's: the mean time since a
   parcel of liquid entered the chip, one number per cell. Conservative upwind
   finite volume, `outflow x age - inflow x age = cell volume`, optionally with molecular
   diffusion. It is LINEAR, so it costs one solve - no particle tracking, no time
   marching. Because the flow has no closed streamlines the convective operator is
   TRIANGULAR in flow order, so with no diffusion the exact answer comes out of one
   forward substitution in O(edges) (`scheme="sweep"`), and a flow loop that breaks the
   sweep is itself a finding worth reporting.

2. WASHOUT (`washout`). Time-dependent upwind transport of an old-liquid tracer for the
   step's volume: the exchanged fraction a step ACTUALLY reaches, as opposed to the
   fraction a well-mixed tank would reach. Implicit (Crank-Nicolson, unconditionally
   stable) by default, explicit with a CFL check on request.

3. THE COMPARISON WITH INTENT (`exchange_intent`, `check_exchange`). What an exchange is
   FOR is DATA now, in `exchange_kind` (contracts/schemas/protocol.schema.json), not
   English inside `note`. sweep or absent -> `exchange.target_fraction`; partial -> the
   fraction the note states; fill -> the chamber's own volume. A well-mixed chamber keeps
   exp(-V) of the old liquid after V chamber volumes, so 99% needs 4.61 chamber volumes -
   the rule that caught nine protocols sweeping 1-2.5.

   What the CFD then adds is that 4.61 is NOT A BOUND, in either direction, and the
   measured chambers land on both sides of it. Hele-Shaw flow does not mix, so a
   well-swept chamber is closer to PLUG flow and clears in about one chamber volume -
   module 3's corner-to-corner wash route needs 2.2, half the well-mixed figure. Put both
   mouths on the same edge and the far side of the chamber is bypassed instead, and the
   same chamber needs 6.4. `nonideality_factor` is the ratio and `Washout.regime` says
   which side it fell on, because "the well-mixed estimate is optimistic here" and "the
   well-mixed estimate is conservative here" are different findings with different fixes.
   The floor survives both: below one chamber volume nothing works.

4. DEAD ZONES (`dead_zones`). Cells whose age exceeds a multiple of the chamber mean, or
   whose speed is below a fraction of it. Both thresholds are named constants declared as
   ASSUMPTIONS below, because no contract states them, and the answer is a VOLUME
   FRACTION per chamber plus how long molecular diffusion alone would take to clear it -
   not a yes/no.

Units: SI internally (m, m^3, m^3/s, s, m^2/s). mm, uL and s with the unit in the key name
at the JSON boundary, as the README requires.

Contract values come from `assay2chip.lib.contracts`; nothing here hard-codes a contract
number. The thresholds in ASSUMPTIONS are not contract numbers and say so.
"""

from __future__ import annotations

import math
import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field as dc_field
from typing import Any, Iterable, Sequence

import numpy as np

from assay2chip.lib import contracts

# `model.py` is owned by the mesh module and carries the Mesh/Node/Edge dataclasses;
# `solve.py` owns Field. Everything in this file reads those shapes through `_attr`, so it
# works on the dataclasses, on the plain dicts they serialise to, and on the JSON reloaded
# from `cfd/<chip_id>.mesh.json`. The import is advisory: it is how a caller gets the
# types, and it must not make transport.py unimportable while mesh.py is still being
# written.
try:  # pragma: no cover - depends on a sibling module landing
    from assay2chip.cfd import model as cfd_model
except ImportError:  # pragma: no cover
    cfd_model = None  # TODO(cfd): drop the fallback once assay2chip/cfd/model.py exists.


# --- assumptions -------------------------------------------------------------
#
# These four numbers are NOT in any contract. They are the thresholds that turn a
# continuous field into a finding, and a finding that rests on a rule of thumb must say
# so, exactly as contracts.is_assumption() does for contract parameters. Every result
# dataclass carries them through to its JSON so the report can print them next to the
# number they produced.

DEAD_ZONE_AGE_FACTOR = 3.0
"""A cell is a long-residence-time zone when its mean age exceeds this multiple of the
chamber's volume-weighted mean age. ASSUMPTION."""

DEAD_ZONE_SPEED_FRACTION = 0.10
"""A cell is a low-velocity zone when its speed is below this fraction of the chamber's
volume-weighted mean speed. ASSUMPTION."""

WASHOUT_CV_SEARCH_CAP = 25.0
"""How many chamber volumes `washout` will march before giving up on reaching the target.
Well above the well-mixed 4.61 for 99%, so a chamber that needs more than this is badly
dead rather than slightly non-ideal. ASSUMPTION."""

WASHOUT_STEPS_PER_CV = 32
"""Implicit time steps per chamber volume swept.

This sets the time-discretisation error, and it has to be small for an honest answer: the
whole point of `nonideality_factor` is to attribute "needs more than 4.61 chamber volumes"
to GEOMETRY, so any time-stepping error shows up as fake non-ideality. Plain backward
Euler is first-order and at 32 steps/CV would report a perfectly mixed chamber as needing
4.70 instead of 4.605 - a 2% lie. Hence Crank-Nicolson (second order, error ~ (dt/tau)^2 /
12, which is 1.4e-4 here) with two backward-Euler startup steps to damp the initial
discontinuity. ASSUMPTION."""

EXPLICIT_STEP_REFINEMENT = 4
"""The explicit scheme is only first-order, so it takes this many times more steps than
the implicit one to land in the same place. ASSUMPTION."""

RANNACHER_SUBSTEPS = 8
"""Backward-Euler substeps used for the FIRST implicit step only, to damp the initial
discontinuity without costing Crank-Nicolson its second order. ASSUMPTION."""

CFL_SAFETY = 0.4
"""Safety factor on the explicit time step, dt <= f x min(V_cell / outflux_cell).
ASSUMPTION."""

ASSUMPTIONS: dict[str, dict[str, Any]] = {
    "dead_zone_age_factor": {
        "value": DEAD_ZONE_AGE_FACTOR,
        "units": "x chamber mean age",
        "assumption": True,
        "source": "assay2chip/cfd/transport.py (not a contract value)",
        "why": "No contract states when a residence-time outlier becomes a trap. 3x the "
               "chamber mean is a rule of thumb; the reported volume fraction is the "
               "number to argue about, not the yes/no.",
    },
    "dead_zone_speed_fraction": {
        "value": DEAD_ZONE_SPEED_FRACTION,
        "units": "fraction of chamber mean speed",
        "assumption": True,
        "source": "assay2chip/cfd/transport.py (not a contract value)",
        "why": "The README's trap test is 'velocity below a threshold fraction of the "
               "chamber mean' and leaves the fraction open. 0.1 is a rule of thumb.",
    },
    "washout_cv_search_cap": {
        "value": WASHOUT_CV_SEARCH_CAP,
        "units": "chamber volumes",
        "assumption": True,
        "source": "assay2chip/cfd/transport.py (not a contract value)",
        "why": "Where the search for 'how many chamber volumes would this actually need' "
               "stops. Reported as a floor, never as the answer.",
    },
    "washout_steps_per_cv": {
        "value": WASHOUT_STEPS_PER_CV,
        "units": "implicit time steps per chamber volume",
        "assumption": True,
        "source": "assay2chip/cfd/transport.py (not a contract value)",
        "why": "Time-discretisation resolution. Any error here would be misread as "
               "geometric non-ideality, so the scheme is second-order and this is set "
               "where the well-mixed limit reproduces exp(-V) to about 1e-4.",
    },
    "cfl_safety": {
        "value": CFL_SAFETY,
        "units": "fraction of the stability limit",
        "assumption": True,
        "source": "assay2chip/cfd/transport.py (not a contract value)",
        "why": "Explicit scheme only; the implicit default has no stability limit.",
    },
}

DENSE_SOLVE_MAX = 4000
"""Above this many unknowns the solves switch from a dense factorisation to the
matrix-free iterative one (`_bicgstab`).

This is a performance switch, not an accuracy one: both paths solve the same system to
the same tolerance, and `tests/test_cfd_transport.py` checks they agree. The limit exists
because mesh.py's default grid puts 12769 cells in module 3's main_trap alone, and dense
at that size is a 1.3 GB matrix.

scipy is NOT a dependency of this repo (`import scipy` fails in .venv), so the iterative
solver is implemented here against numpy only."""

ITERATIVE_TOL = 1e-11
"""Relative residual the iterative solver must reach. Tight, because the dead-zone
thresholds compare a cell's age against a multiple of the chamber mean, and a sloppy solve
would move cells across that line."""

ITERATIVE_MAXITER = 2000

_QTOL_REL = 1e-12
_SECONDS_PER_MINUTE = 60.0
_M3_PER_UL = 1e-9


# --- reading the Mesh and Field shapes --------------------------------------

def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """One accessor for both the model.py dataclasses and the JSON they serialise to."""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _items(container: Any) -> list[Any]:
    """`nodes`/`edges` as a list, whether the Mesh stores them as a list or by id."""
    if container is None:
        return []
    if isinstance(container, Mapping):
        return list(container.values())
    return list(container)


def _lookup(container: Any, key: str, default: Any = None) -> Any:
    """A per-node or per-edge Field table, which may be a dict or a sequence."""
    if container is None:
        return default
    if isinstance(container, Mapping):
        return container.get(key, default)
    return default


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _speed_mm_s(vec: Any) -> float | None:
    """|u| from a Field `u_mm_s[cell]`, which the README defines as a vector."""
    if vec is None:
        return None
    if isinstance(vec, Mapping):
        parts = [_float(vec.get(k)) for k in ("x", "y", "z") if k in vec]
        if not parts:
            mag = vec.get("magnitude_mm_s", vec.get("mag"))
            return None if mag is None else abs(_float(mag))
        return math.sqrt(sum(p * p for p in parts))
    if isinstance(vec, (int, float)):
        return abs(float(vec))
    if isinstance(vec, Sequence):
        return math.sqrt(sum(_float(p) ** 2 for p in vec))
    return None


# --- diffusivity -------------------------------------------------------------

@dataclass(frozen=True)
class Diffusivity:
    """A molecular diffusivity and an honest account of where it came from.

    The contracts hold exactly ONE diffusivity, `dye.diffusivity_m2_s`, and it is itself
    flagged an assumption and scoped to a small-molecule dye in water. Anything else -
    lysis buffer, a cold organic quench, an RT mix - has no contract value, so asking for
    it returns the dye number with `fallback=True`, and the caller is expected to put
    `unresolved_note` into its stage's `unresolved`. Never a silent substitution.
    """

    m2_s: float
    key: str
    species: str
    assumption: bool
    fallback: bool
    scope: str
    note: str

    @property
    def unresolved_note(self) -> str | None:
        if not self.fallback:
            return None
        return (f"transport: no diffusivity in the contracts for {self.species!r}; used "
                f"{self.key} = {self.m2_s:g} m2/s (a small-molecule dye in water) as a "
                f"stand-in. Any diffusive clearance time for {self.species!r} is "
                f"order-of-magnitude only.")

    def to_json(self) -> dict[str, Any]:
        return {
            "diffusivity_m2_s": self.m2_s,
            "contract_key": self.key,
            "species": self.species,
            "assumption": self.assumption,
            "fallback": self.fallback,
            "scope": self.scope,
            "note": self.note,
        }


DYE_DIFFUSIVITY_KEY = "dye.diffusivity_m2_s"


def diffusivity(species: str = "dye") -> Diffusivity:
    """Molecular diffusivity for a species, from the contracts, flagged.

    `species="dye"` is the one the contract actually covers (the dye demo). Anything else
    gets the dye value marked `fallback`.
    """
    entry = contracts.param_entry(DYE_DIFFUSIVITY_KEY)
    value = float(contracts.param(DYE_DIFFUSIVITY_KEY))
    is_dye = species in ("dye", "small_molecule_dye")
    return Diffusivity(
        m2_s=value,
        key=DYE_DIFFUSIVITY_KEY,
        species=species,
        assumption=contracts.is_assumption(DYE_DIFFUSIVITY_KEY),
        fallback=not is_dye,
        scope="small-molecule dye in water (contracts/parameters.json)",
        note=str(entry.get("note", "")),
    )


# --- topology ----------------------------------------------------------------

@dataclass
class _Topology:
    """The mesh and field reduced to what a finite-volume scalar solve needs.

    Unknowns are every node that is NOT a port: cells inside chambers and the junctions
    that ducts meet at. Ports are the BOUNDARY - a port is a reservoir outside the chip,
    so inflow across a port mouth carries a known value and outflow carries the upwind
    interior value. That keeps the system non-singular without pinning anything inside the
    chip.

    Volume is lumped onto nodes. A node contributes `area_mm2 x depth_mm` (a chamber cell
    has an area; a junction usually does not), and every incident `duct` or `mouth` edge
    hands half its own `length x width x depth` to each end. `hs_face` edges are the
    interior faces of a discretised chamber, so their volume is already in the cell areas
    and they contribute none - counting it would double the chamber.
    """

    ids: list[str]
    index: dict[str, int]
    volume_m3: np.ndarray
    kind: list[str]
    feature: list[str | None]
    x_mm: np.ndarray
    y_mm: np.ndarray
    area_mm2: np.ndarray
    depth_mm: np.ndarray
    speed_mm_s: np.ndarray          # NaN where the Field gave no velocity for that cell
    # interior faces, between two unknowns
    fa: np.ndarray
    fb: np.ndarray
    fq: np.ndarray                  # m^3/s, positive from fa to fb
    fgeo: np.ndarray                # face area / distance, in metres: x D -> m^3/s
    fid: list[str]
    # boundary faces, from an unknown to a port
    bi: np.ndarray
    bq: np.ndarray                  # m^3/s, positive LEAVING the domain
    bgeo: np.ndarray
    bport: list[str]
    qtol: float
    notes: list[str] = dc_field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.ids)

    # --- derived sums, used by every scheme ---

    def outflux(self, diffusivity_m2_s: float = 0.0) -> np.ndarray:
        """Per-node sum of leaving convective flow plus diffusive conductance.

        This is the matrix diagonal, and the quantity the CFL limit divides the volume by.
        """
        out = np.zeros(self.n)
        if len(self.fq):
            np.add.at(out, self.fa, np.maximum(self.fq, 0.0))
            np.add.at(out, self.fb, np.maximum(-self.fq, 0.0))
        if len(self.bq):
            np.add.at(out, self.bi, np.maximum(self.bq, 0.0))
        if diffusivity_m2_s > 0:
            if len(self.fgeo):
                g = diffusivity_m2_s * self.fgeo
                np.add.at(out, self.fa, g)
                np.add.at(out, self.fb, g)
            if len(self.bgeo):
                np.add.at(out, self.bi, diffusivity_m2_s * self.bgeo)
        return out

    def boundary_inflow(self, value: float, diffusivity_m2_s: float = 0.0) -> np.ndarray:
        """Source term from the boundary: inflow at `value` across every port mouth.

        Convective inflow is `|q| x value`. Diffusive exchange with the port reservoir is
        `D x A/d x value` and is symmetric, so its counterpart sits on the diagonal in
        `outflux` - that is what makes a capped-but-wetted mouth able to clear by
        diffusion alone. `bgeo` is already zeroed on OUTFLOW mouths (see
        `build_topology`), so this only ever reaches inflow and no-flow mouths.
        """
        src = np.zeros(self.n)
        if value == 0.0 and diffusivity_m2_s <= 0:
            return src
        if len(self.bq):
            np.add.at(src, self.bi, np.maximum(-self.bq, 0.0) * value)
        if diffusivity_m2_s > 0 and len(self.bgeo):
            np.add.at(src, self.bi, diffusivity_m2_s * self.bgeo * value)
        return src

    def total_inflow_m3_s(self) -> float:
        return float(np.sum(np.maximum(-self.bq, 0.0))) if len(self.bq) else 0.0

    def total_outflow_m3_s(self) -> float:
        return float(np.sum(np.maximum(self.bq, 0.0))) if len(self.bq) else 0.0

    def apply(self, c: np.ndarray, diffusivity_m2_s: float = 0.0) -> np.ndarray:
        """A @ c, where A is the upwind convection-diffusion operator (boundary inflow
        excluded - it is a source, see `boundary_inflow`). Vectorised, no dense matrix."""
        out = self.outflux(diffusivity_m2_s) * c
        if len(self.fq):
            # a face with flow a->b takes c[a] out of a and puts it into b, and the
            # diagonal part of that is already in `outflux`, so only the off-diagonal
            # (the receiving end) is left.
            q_pos = np.maximum(self.fq, 0.0)
            q_neg = np.maximum(-self.fq, 0.0)
            np.add.at(out, self.fb, -q_pos * c[self.fa])
            np.add.at(out, self.fa, -q_neg * c[self.fb])
        if diffusivity_m2_s > 0 and len(self.fgeo):
            g = diffusivity_m2_s * self.fgeo
            np.add.at(out, self.fa, -g * c[self.fb])
            np.add.at(out, self.fb, -g * c[self.fa])
        return out

    def dense(self, diffusivity_m2_s: float = 0.0) -> np.ndarray:
        if self.n > DENSE_SOLVE_MAX:
            raise ValueError(
                f"transport: {self.n} unknowns exceeds DENSE_SOLVE_MAX={DENSE_SOLVE_MAX}; "
                f"use the matrix-free iterative path instead of assembling this.")
        a = np.zeros((self.n, self.n))
        a[np.arange(self.n), np.arange(self.n)] = self.outflux(diffusivity_m2_s)
        if len(self.fq):
            np.add.at(a, (self.fb, self.fa), -np.maximum(self.fq, 0.0))
            np.add.at(a, (self.fa, self.fb), -np.maximum(-self.fq, 0.0))
        if diffusivity_m2_s > 0 and len(self.fgeo):
            g = diffusivity_m2_s * self.fgeo
            np.add.at(a, (self.fa, self.fb), -g)
            np.add.at(a, (self.fb, self.fa), -g)
        return a

    def node_mask(self, feature_id: str | None) -> np.ndarray:
        """Which unknowns belong to one feature, INCLUDING its junctions.

        Rarely what a report wants - see `cell_mask`.
        """
        if feature_id is None:
            return np.array([k == "cell" for k in self.kind])
        return np.array([f == feature_id for f in self.feature])

    def cell_mask(self, feature_id: str) -> np.ndarray:
        """The CELLS of one chamber: `kind == "cell"` and `feature_id` matches.

        This, not `node_mask`, is a chamber. mesh.py also tags the junction where a
        channel meets the chamber with that chamber's `feature_id` - `j:comb_in_n:to` and
        friends - and lumps half the channel's own volume onto it. Those junctions are
        mouths, not chamber volume, and counting them was worth an inflation of 1.8x on
        module 6's 2.88 uL culture field: every chamber volume delivered came out as 0.56
        of one, and because a mouth junction has no `u_mm_s` it also read as a
        zero-velocity cell and reported 41.6% of the chamber dead. mesh.py's own docstring
        is the authority here: "the sum over one chamber's cells is that chamber's
        footprint area".
        """
        return np.array([f == feature_id and k == "cell"
                         for f, k in zip(self.feature, self.kind)])


def _edge_face_geometry(edge: Any, grid_mm: float | None) -> float:
    """Face area / separation, in METRES, so that `D x this` is a m^3/s conductance.

    Returns 0.0 when the edge does not carry enough geometry to say - diffusion across
    that face is then dropped rather than guessed, and `_Topology.notes` records it.
    """
    width_mm = _float(_attr(edge, "width_mm"), 0.0)
    depth_mm = _float(_attr(edge, "depth_mm"), 0.0)
    length_mm = _float(_attr(edge, "length_mm"), 0.0)
    if length_mm <= 0 and grid_mm:
        length_mm = float(grid_mm)
    if width_mm <= 0 or depth_mm <= 0 or length_mm <= 0:
        return 0.0
    return (width_mm * 1e-3) * (depth_mm * 1e-3) / (length_mm * 1e-3)


def _edge_volume_m3(edge: Any) -> float:
    kind = _attr(edge, "kind", "duct")
    if kind == "hs_face":
        return 0.0            # interior face of a chamber: volume is already in the cells
    length_mm = _float(_attr(edge, "length_mm"), 0.0)
    width_mm = _float(_attr(edge, "width_mm"), 0.0)
    depth_mm = _float(_attr(edge, "depth_mm"), 0.0)
    if min(length_mm, width_mm, depth_mm) <= 0:
        return 0.0
    return (length_mm * width_mm * depth_mm) * 1e-9


def build_topology(mesh: Any, field: Any) -> _Topology:
    """Reduce a `Mesh` + `Field` to the finite-volume topology the solves run on."""
    nodes = _items(_attr(mesh, "nodes"))
    edges = _items(_attr(mesh, "edges"))
    meta = _attr(mesh, "meta") or {}
    grid_mm = _float(_attr(meta, "grid_mm"), 0.0) or None
    q_table = _attr(field, "q_m3_s")
    u_table = _attr(field, "u_mm_s")
    notes: list[str] = []

    port_ids: set[str] = set()
    interior: list[Any] = []
    for node in nodes:
        nid = str(_attr(node, "id"))
        if _attr(node, "kind") == "port":
            port_ids.add(nid)
        else:
            interior.append(node)

    ids = [str(_attr(n, "id")) for n in interior]
    index = {nid: i for i, nid in enumerate(ids)}
    n = len(ids)

    volume = np.zeros(n)
    kinds: list[str] = []
    features: list[str | None] = []
    xs = np.zeros(n)
    ys = np.zeros(n)
    areas = np.zeros(n)
    depths = np.zeros(n)
    speeds = np.full(n, np.nan)
    for i, node in enumerate(interior):
        area_mm2 = _float(_attr(node, "area_mm2"), 0.0)
        depth_mm = _float(_attr(node, "depth_mm"), 0.0)
        areas[i] = area_mm2
        depths[i] = depth_mm
        volume[i] = area_mm2 * depth_mm * 1e-9
        kinds.append(str(_attr(node, "kind", "cell")))
        feat = _attr(node, "feature_id")
        features.append(None if feat is None else str(feat))
        xs[i] = _float(_attr(node, "x_mm"), math.nan)
        ys[i] = _float(_attr(node, "y_mm"), math.nan)
        sp = _speed_mm_s(_lookup(u_table, ids[i]))
        if sp is not None:
            speeds[i] = sp

    fa: list[int] = []
    fb: list[int] = []
    fq: list[float] = []
    fgeo: list[float] = []
    fid: list[str] = []
    bi: list[int] = []
    bq: list[float] = []
    bgeo: list[float] = []
    bport: list[str] = []
    missing_geometry = 0

    for edge in edges:
        eid = str(_attr(edge, "id"))
        a = str(_attr(edge, "a"))
        b = str(_attr(edge, "b"))
        q = _float(_lookup(q_table, eid), 0.0)      # signed a -> b, m^3/s
        geo = _edge_face_geometry(edge, grid_mm)
        if geo == 0.0:
            missing_geometry += 1
        half = 0.5 * _edge_volume_m3(edge)
        a_port, b_port = a in port_ids, b in port_ids
        if a in index:
            volume[index[a]] += half
        if b in index:
            volume[index[b]] += half
        if a_port and b_port:
            continue                                 # port to port: not a chip volume
        if a_port or b_port:
            inner, outer, outward = (b, a, -q) if a_port else (a, b, q)
            if inner not in index:
                continue
            bi.append(index[inner])
            bq.append(outward)
            bgeo.append(geo)
            bport.append(outer)
            continue
        if a not in index or b not in index:
            continue
        fa.append(index[a])
        fb.append(index[b])
        fq.append(q)
        fgeo.append(geo)
        fid.append(eid)

    if missing_geometry:
        notes.append(
            f"{missing_geometry} edge(s) carry no width/depth/length, so molecular "
            f"diffusion across them is dropped rather than guessed; convection is "
            f"unaffected.")
    if np.isnan(speeds).all() and n:
        notes.append(
            "Field has no u_mm_s, so cell speeds were estimated from face flows "
            "(half the sum of |q| over a cell's faces, divided by the cell's "
            "cross-section). The low-velocity threshold is approximate.")

    fq_arr = np.array(fq, dtype=float)
    bq_arr = np.array(bq, dtype=float)
    bgeo_arr = np.array(bgeo, dtype=float)
    qmax = max(
        float(np.max(np.abs(fq_arr))) if fq_arr.size else 0.0,
        float(np.max(np.abs(bq_arr))) if bq_arr.size else 0.0,
    )
    qtol = max(qmax * _QTOL_REL, 1e-24)

    # Boundary condition on the port mouths. A mouth the liquid ENTERS through is a
    # Dirichlet condition: the reservoir behind it holds fresh liquid at a known value,
    # and it exchanges both convectively and diffusively. A mouth the liquid LEAVES
    # through is zero-gradient (convective outflow only): the tube beyond it is not a
    # reservoir of "age", so a Dirichlet there would diffusively drain age out of the chip
    # and break the exact V/Q identity that `AgeField.consistency_error` checks. A mouth
    # with no flow keeps its diffusive term - that is how a capped but wetted mouth
    # clears, which is the Module 6 case the audit cares about.
    if bgeo_arr.size:
        bgeo_arr = np.where(bq_arr > qtol, 0.0, bgeo_arr)
    topo = _Topology(
        ids=ids,
        index=index,
        volume_m3=volume,
        kind=kinds,
        feature=features,
        x_mm=xs,
        y_mm=ys,
        area_mm2=areas,
        depth_mm=depths,
        speed_mm_s=speeds,
        fa=np.array(fa, dtype=int),
        fb=np.array(fb, dtype=int),
        fq=fq_arr,
        fgeo=np.array(fgeo, dtype=float),
        fid=fid,
        bi=np.array(bi, dtype=int),
        bq=bq_arr,
        bgeo=bgeo_arr,
        bport=bport,
        qtol=qtol,
        notes=notes,
    )
    _fill_missing_speeds(topo)
    return topo


class ConvergenceError(RuntimeError):
    """The iterative solve did not converge, so there is no field to report.

    Raised rather than returning the last iterate: a half-converged age field would move
    cells across the dead-zone threshold and produce findings nobody could reproduce.
    """


def _bicgstab(
    apply: Any,
    b: np.ndarray,
    diag: np.ndarray,
    *,
    x0: np.ndarray | None = None,
    tol: float = ITERATIVE_TOL,
    maxiter: int = ITERATIVE_MAXITER,
) -> tuple[np.ndarray, dict[str, Any]]:
    """Matrix-free BiCGSTAB with Jacobi preconditioning. numpy only, no scipy.

    The upwind convection-diffusion operator is NON-SYMMETRIC, so conjugate gradients do
    not apply and solve.py's `_cg_jacobi` (which solves the symmetric pressure Laplacian)
    cannot be reused. BiCGSTAB handles it, and the operator is a diagonally dominant
    M-matrix, so Jacobi preconditioning on its diagonal is enough.

    `apply(x)` is the operator; `diag` is its diagonal, for the preconditioner.
    """
    n = b.size
    minv = np.divide(1.0, diag, out=np.ones_like(diag), where=np.abs(diag) > 0)
    x = np.zeros(n) if x0 is None else x0.astype(float).copy()
    r = b - apply(x)
    b_norm = float(np.linalg.norm(b)) or 1.0
    if float(np.linalg.norm(r)) / b_norm <= tol:
        return x, {"iterations": 0, "residual": float(np.linalg.norm(r)) / b_norm}

    r_hat = r.copy()
    rho = alpha = omega = 1.0
    v = np.zeros(n)
    p = np.zeros(n)
    for it in range(1, maxiter + 1):
        rho_new = float(r_hat @ r)
        if rho_new == 0.0:
            break
        if it == 1:
            p = r.copy()
        else:
            beta = (rho_new / rho) * (alpha / omega)
            p = r + beta * (p - omega * v)
        rho = rho_new
        y = minv * p
        v = apply(y)
        denom = float(r_hat @ v)
        if denom == 0.0:
            break
        alpha = rho / denom
        s = r - alpha * v
        if float(np.linalg.norm(s)) / b_norm <= tol:
            x += alpha * y
            return x, {"iterations": it, "residual": float(np.linalg.norm(s)) / b_norm}
        z = minv * s
        t = apply(z)
        tt = float(t @ t)
        if tt == 0.0:
            break
        omega = float(t @ s) / tt
        x += alpha * y + omega * z
        r = s - omega * t
        res = float(np.linalg.norm(r)) / b_norm
        if res <= tol:
            return x, {"iterations": it, "residual": res}
        if omega == 0.0:
            break
    res = float(np.linalg.norm(b - apply(x))) / b_norm
    if res > max(tol, 1e-8) * 100:
        raise ConvergenceError(
            f"transport: BiCGSTAB stalled at relative residual {res:.3e} after "
            f"{maxiter} iterations on {n} unknowns. The field may not be mass-conserving "
            f"(check Field.residual.mass_imbalance_max) - a scalar transport solve cannot "
            f"be better conditioned than the flow it runs on.")
    return x, {"iterations": maxiter, "residual": res, "warning": "loose convergence"}


def _fill_missing_speeds(topo: _Topology) -> None:
    """Estimate |u| for cells the Field did not give a velocity for.

    A cell's throughput is half the sum of |q| over its faces - what goes in comes out, so
    halving the total avoids counting it twice. Dividing by the cross-section the cell
    presents to the flow, `depth x sqrt(area)` for a square cell of that area, gives a
    depth-averaged speed.

    This is a FALLBACK and `_Topology.notes` says so. solve.py's `u_mm_s` is the real
    number, and it is a vector, so it also knows the direction this cannot.
    """
    missing = ~np.isfinite(topo.speed_mm_s)
    if not missing.any():
        return
    throughput = np.zeros(topo.n)
    if len(topo.fq):
        np.add.at(throughput, topo.fa, np.abs(topo.fq))
        np.add.at(throughput, topo.fb, np.abs(topo.fq))
    if len(topo.bq):
        np.add.at(throughput, topo.bi, np.abs(topo.bq))
    throughput *= 0.5

    side_m = np.sqrt(np.maximum(topo.area_mm2, 0.0)) * 1e-3
    cross_m2 = side_m * (topo.depth_mm * 1e-3)
    usable = missing & (cross_m2 > 0)
    topo.speed_mm_s[usable] = throughput[usable] / cross_m2[usable] * 1e3
    topo.speed_mm_s[~np.isfinite(topo.speed_mm_s)] = 0.0


# --- 1. mean age -------------------------------------------------------------

@dataclass
class AgeField:
    """Mean age (residence time) per node, plus the checks that say whether to trust it."""

    age_s: dict[str, float]
    diffusivity_m2_s: float
    scheme: str
    volume_m3: float
    q_in_m3_s: float
    q_out_m3_s: float
    theoretical_mean_age_s: float | None
    outlet_mean_age_s: float | None
    unreachable_nodes: list[str]
    unreachable_volume_fraction: float
    flow_loops: list[str]
    notes: list[str]

    @property
    def consistency_error(self) -> float | None:
        """|outlet mean age / (V/Q) - 1|. Exact theory: the flow-weighted mean age at the
        outflow of ANY steady domain equals its volume over its throughput. If this is not
        small the field's mass balance is off, or the mesh lost volume - either way the
        dead-zone numbers below are not to be trusted."""
        if not self.theoretical_mean_age_s or self.outlet_mean_age_s is None:
            return None
        return abs(self.outlet_mean_age_s / self.theoretical_mean_age_s - 1.0)

    def to_json(self) -> dict[str, Any]:
        return {
            "scheme": self.scheme,
            "diffusivity_m2_s": self.diffusivity_m2_s,
            "volume_ul": self.volume_m3 / _M3_PER_UL,
            "q_in_ul_min": self.q_in_m3_s / _M3_PER_UL * _SECONDS_PER_MINUTE,
            "theoretical_mean_age_s": self.theoretical_mean_age_s,
            "outlet_mean_age_s": self.outlet_mean_age_s,
            "consistency_error": self.consistency_error,
            "unreachable_nodes": len(self.unreachable_nodes),
            "unreachable_volume_fraction": self.unreachable_volume_fraction,
            "flow_loops": len(self.flow_loops),
            "notes": list(self.notes),
        }


def mean_age(
    mesh: Any,
    field: Any,
    *,
    diffusivity_m2_s: float = 0.0,
    inlet_age_s: float = 0.0,
    scheme: str = "auto",
    topology: _Topology | None = None,
) -> AgeField:
    """Solve the steady mean-age equation on the mesh.

    Conservative upwind finite volume, one equation per non-port node:

        sum over faces [ q_face x age_upwind ]  -  sum over faces [ D A/d (age_nb - age) ]
            =  cell volume

    in m^3 s on both sides. Liquid entering through a port is `inlet_age_s` old (0 by
    default: the age clock starts at the chip inlet). Liquid leaving carries the cell's own
    age, which is what makes the system closed without pinning anything inside the chip.

    `scheme`:
      "sweep"   no diffusion. Hele-Shaw has no closed streamlines, so ordering the nodes
                along the flow makes the operator lower-triangular and one forward
                substitution is EXACT, in O(edges). If the sweep cannot finish there is a
                closed flow loop, which contradicts the model and is reported in
                `flow_loops` before falling back to the matrix.
      "matrix"  dense LU. Required once D > 0.
      "auto"    sweep when `diffusivity_m2_s` is 0, matrix otherwise.
    """
    topo = topology if topology is not None else build_topology(mesh, field)
    notes = list(topo.notes)
    if scheme == "auto":
        scheme = "sweep" if diffusivity_m2_s <= 0 else "matrix"
    if scheme == "sweep" and diffusivity_m2_s > 0:
        raise ValueError("transport.mean_age: the sweep scheme cannot carry diffusion; "
                         "use scheme='matrix' (or 'auto') when diffusivity_m2_s > 0.")

    loops: list[str] = []
    if scheme == "sweep":
        age, unreachable, loops = _age_by_sweep(topo, inlet_age_s)
        if loops:
            notes.append(
                f"A closed flow loop spans {len(loops)} node(s), which depth-averaged "
                f"Hele-Shaw flow cannot produce (see cfd/README.md): either solve.py's "
                f"field is not mass-conserving or the mesh is not a Hele-Shaw mesh. "
                f"Fell back to the matrix solve; treat the ages as suspect.")
            age, unreachable = _age_by_matrix(topo, diffusivity_m2_s, inlet_age_s)
            scheme = "sweep->matrix"
    else:
        age, unreachable = _age_by_matrix(topo, diffusivity_m2_s, inlet_age_s)

    total_volume = float(np.sum(topo.volume_m3))
    q_in = topo.total_inflow_m3_s()
    q_out = topo.total_outflow_m3_s()
    theoretical = (total_volume / q_in) if q_in > topo.qtol else None

    outlet_mean = None
    if len(topo.bq):
        leaving = np.maximum(topo.bq, 0.0)
        if leaving.sum() > topo.qtol:
            ages_at_exit = age[topo.bi]
            good = np.isfinite(ages_at_exit)
            w = leaving * good
            if w.sum() > topo.qtol:
                outlet_mean = float(np.sum(w * np.nan_to_num(ages_at_exit)) / np.sum(w))

    bad = ~np.isfinite(age)
    unreachable_fraction = (
        float(np.sum(topo.volume_m3[bad]) / total_volume) if total_volume > 0 else 0.0)
    if unreachable_fraction > 0:
        notes.append(
            f"{int(bad.sum())} node(s), {unreachable_fraction * 100:.2f}% of the wetted "
            f"volume, have no convective path to an outflow and no diffusive one either: "
            f"their age is infinite. That is a dead_end / bubble finding for analyse.py, "
            f"not a residence time.")

    return AgeField(
        age_s={nid: (float(age[i]) if np.isfinite(age[i]) else math.inf)
               for i, nid in enumerate(topo.ids)},
        diffusivity_m2_s=float(diffusivity_m2_s),
        scheme=scheme,
        volume_m3=total_volume,
        q_in_m3_s=q_in,
        q_out_m3_s=q_out,
        theoretical_mean_age_s=theoretical,
        outlet_mean_age_s=outlet_mean,
        unreachable_nodes=[topo.ids[i] for i in np.flatnonzero(bad)],
        unreachable_volume_fraction=unreachable_fraction,
        flow_loops=loops,
        notes=notes,
    )


def _age_by_sweep(topo: _Topology, inlet_age_s: float) -> tuple[np.ndarray, list[str], list[str]]:
    """Exact forward substitution in flow order (no diffusion).

    Kahn's algorithm on the flow digraph. A node is ready when every face that carries
    flow INTO it comes from a node already done, or from a port. Then
    `age = (volume + sum of inflow x upstream age) / total outflow`, which is the upwind
    equation solved for the one unknown it still has.
    """
    n = topo.n
    tol = topo.qtol
    inflow_from: list[list[tuple[int, float]]] = [[] for _ in range(n)]
    pending = np.zeros(n, dtype=int)
    downstream: list[list[int]] = [[] for _ in range(n)]
    for k in range(len(topo.fq)):
        a, b, q = int(topo.fa[k]), int(topo.fb[k]), float(topo.fq[k])
        if q > tol:
            inflow_from[b].append((a, q))
            downstream[a].append(b)
            pending[b] += 1
        elif q < -tol:
            inflow_from[a].append((b, -q))
            downstream[b].append(a)
            pending[a] += 1

    source = topo.volume_m3.copy()
    if len(topo.bq):
        np.add.at(source, topo.bi, np.maximum(-topo.bq, 0.0) * inlet_age_s)
    out = topo.outflux(0.0)

    age = np.full(n, np.nan)
    acc = source.copy()
    queue = deque(int(i) for i in np.flatnonzero(pending == 0))
    done = np.zeros(n, dtype=bool)
    while queue:
        i = queue.popleft()
        done[i] = True
        age[i] = (acc[i] / out[i]) if out[i] > tol else math.inf
        value = age[i] if np.isfinite(age[i]) else 0.0
        for j in downstream[i]:
            for src, q in inflow_from[j]:
                if src == i:
                    acc[j] += q * value
                    break
            pending[j] -= 1
            if pending[j] == 0:
                queue.append(j)

    loops = [topo.ids[i] for i in np.flatnonzero(~done)]
    unreachable = [topo.ids[i] for i in np.flatnonzero(~np.isfinite(age))]
    return age, unreachable, loops


def _age_by_matrix(topo: _Topology, d: float, inlet_age_s: float) -> tuple[np.ndarray, list[str]]:
    """The mean-age system, solved densely on a small mesh and iteratively on a big one.

    A node with no convective outflow AND no diffusive neighbour has an empty row: its age
    is genuinely infinite, so it is held out of the system and marked afterwards instead of
    making the matrix singular.
    """
    b = topo.volume_m3 + topo.boundary_inflow(inlet_age_s, d)
    diag = topo.outflux(d)
    isolated = diag <= topo.qtol
    live = ~isolated

    if topo.n <= DENSE_SOLVE_MAX:
        a = topo.dense(d)
        if isolated.any():
            idx = np.flatnonzero(isolated)
            a[idx, :] = 0.0
            a[idx, idx] = 1.0
            b = b.copy()
            b[idx] = 0.0
        age = np.linalg.solve(a, b)
    else:
        rhs = np.where(live, b, 0.0)

        def apply(x: np.ndarray) -> np.ndarray:
            y = topo.apply(np.where(live, x, 0.0), d)
            return np.where(live, y, x)       # identity row for an isolated node

        age, _ = _bicgstab(apply, rhs, np.where(live, diag, 1.0))

    age = np.asarray(age, dtype=float)
    age[isolated] = math.inf
    return age, [topo.ids[i] for i in np.flatnonzero(isolated)]


# --- 2. washout --------------------------------------------------------------

@dataclass
class Washout:
    """Time-dependent washout of the old liquid in one chamber."""

    chamber_id: str
    chamber_volume_m3: float
    q_m3_s: float
    cv_delivered: float
    fraction_reached: float
    well_mixed_fraction: float
    curve: list[tuple[float, float]]           # (chamber volumes swept, fraction reached)
    target_fraction: float | None
    cv_required_cfd: float | None
    cv_required_well_mixed: float | None
    nonideality_factor: float | None
    scheme: str
    dt_s: float
    steps: int
    diffusivity_m2_s: float
    notes: list[str]

    @property
    def target_reached(self) -> bool | None:
        if self.target_fraction is None:
            return None
        return self.fraction_reached + 1e-12 >= self.target_fraction

    @property
    def regime(self) -> str:
        """Which way this chamber departs from a stirred tank - and it goes BOTH ways.

        A Hele-Shaw chamber has no mixing at all, so it is closer to plug flow than to a
        stirred tank: its bulk clears in about ONE chamber volume, well ahead of exp(-V).
        What lags is the dead corners, and whether the chamber beats or loses to the
        well-mixed figure depends on whether that dead volume is bigger than the 1% the
        target leaves on the table.

        So `nonideality_factor` is not a penalty that is always above 1:

          plug_like           below 1. Port placement sweeps the whole chamber, and the
                              well-mixed estimate is CONSERVATIVE - it asks for more
                              volume than the chamber needs.
          dead_zone_limited   above 1. Part of the chamber is bypassed, and the well-mixed
                              estimate is OPTIMISTIC - it is NOT a safe bound.

        Either way the well-mixed floor still does its job: below one chamber volume
        nothing works, whatever the geometry.
        """
        if self.nonideality_factor is None:
            return "unreached"
        if self.nonideality_factor < 0.95:
            return "plug_like"
        if self.nonideality_factor > 1.05:
            return "dead_zone_limited"
        return "near_well_mixed"

    def to_json(self) -> dict[str, Any]:
        return {
            "chamber_id": self.chamber_id,
            "chamber_volume_ul": self.chamber_volume_m3 / _M3_PER_UL,
            "flow_ul_min": self.q_m3_s / _M3_PER_UL * _SECONDS_PER_MINUTE,
            "chamber_volumes_delivered": self.cv_delivered,
            "fraction_reached_cfd": self.fraction_reached,
            "fraction_reached_well_mixed": self.well_mixed_fraction,
            "target_fraction": self.target_fraction,
            "chamber_volumes_required_cfd": self.cv_required_cfd,
            "chamber_volumes_required_well_mixed": self.cv_required_well_mixed,
            "nonideality_factor": self.nonideality_factor,
            "regime": self.regime,
            "target_reached": self.target_reached,
            "scheme": self.scheme,
            "dt_s": self.dt_s,
            "steps": self.steps,
            "diffusivity_m2_s": self.diffusivity_m2_s,
            "curve_cv_fraction": [[round(v, 4), round(f, 6)] for v, f in self.curve],
            "notes": list(self.notes),
        }


def well_mixed_fraction(chamber_volumes: float) -> float:
    """What a perfectly stirred tank reaches after V chamber volumes: 1 - exp(-V).

    The baseline every CFD number here is compared against, and the source of the 4.61.
    """
    return 1.0 - math.exp(-max(chamber_volumes, 0.0))


def well_mixed_chamber_volumes(target_fraction: float) -> float:
    """Chamber volumes a perfectly stirred tank needs for `target_fraction`: -ln(1 - f).

    exchange.target_fraction = 0.99 gives 4.605. Below ONE chamber volume the step cannot
    clear the chamber however many times it is repeated, which is the floor the sweep rule
    enforces.
    """
    if not 0.0 <= target_fraction < 1.0:
        raise ValueError(f"target_fraction must be in [0, 1): got {target_fraction}")
    return -math.log(1.0 - target_fraction)


def washout(
    mesh: Any,
    field: Any,
    chamber_id: str,
    *,
    chamber_volumes: float | None = None,
    volume_ul: float | None = None,
    target_fraction: float | None = None,
    diffusivity_m2_s: float = 0.0,
    scheme: str = "auto",
    samples: int = 48,
    topology: _Topology | None = None,
) -> Washout:
    """March an old-liquid tracer out of `chamber_id` for the volume the step delivers.

    The tracer starts at 1 everywhere inside the chamber and 0 elsewhere, and liquid
    entering from a port is fresh (0). The answer is the VOLUME-WEIGHTED mean of the old
    liquid still in the chamber: `fraction_reached = 1 - mean(c)` is the exchange the step
    actually achieved, as opposed to the volume it pushed through.

    Give the dose either as `chamber_volumes` or as `volume_ul` (washes x volume_ul, as
    tools/validate.py already computes it). If `target_fraction` is given, the march
    continues past the dose up to WASHOUT_CV_SEARCH_CAP to find how many chamber volumes
    this chamber would ACTUALLY need - `nonideality_factor` is that over the well-mixed
    requirement, and is the "a real chamber with dead corners needs more" number.

    `scheme`: "implicit" (Crank-Nicolson, unconditionally stable; a reused dense inverse
    on a small mesh, matrix-free BiCGSTAB on a large one) or "explicit" (forward Euler,
    matrix-free, dt from the CFL limit x CFL_SAFETY). "auto" is implicit, which is almost
    always right: on mesh.py's default grid the CFL limit is a few milliseconds against a
    wash that lasts an hour, so the explicit scheme is for cross-checking, not production.
    """
    topo = topology if topology is not None else build_topology(mesh, field)
    notes = list(topo.notes)
    mask = topo.node_mask(chamber_id)
    if not mask.any():
        raise ValueError(
            f"transport.washout: no mesh node has feature_id {chamber_id!r}; the mesh has "
            f"{sorted({f for f in topo.feature if f})}")

    v_ch = float(np.sum(topo.volume_m3[mask]))
    q_ch = _chamber_inflow_m3_s(topo, mask)
    if q_ch <= topo.qtol:
        notes.append(
            f"No flow enters {chamber_id!r} in this field, so nothing washes out by "
            f"convection. Reported fractions are diffusion only.")

    if chamber_volumes is None:
        if volume_ul is None:
            raise ValueError("transport.washout needs chamber_volumes or volume_ul")
        chamber_volumes = (volume_ul * _M3_PER_UL) / v_ch if v_ch > 0 else 0.0
    cv_dose = float(chamber_volumes)

    march_to = cv_dose
    if target_fraction is not None:
        march_to = max(cv_dose, WASHOUT_CV_SEARCH_CAP)
    if q_ch <= topo.qtol:
        march_to = cv_dose

    if scheme == "auto":
        scheme = "implicit"

    t_end = (march_to * v_ch / q_ch) if q_ch > topo.qtol else 0.0
    c0 = np.where(mask, 1.0, 0.0)
    if t_end <= 0:
        curve = [(0.0, 0.0)]
        dt = 0.0
        steps = 0
        trace = [(0.0, 0.0)]
    else:
        trace, dt, steps, extra = _march(topo, c0, mask, t_end, diffusivity_m2_s, scheme)
        notes.extend(extra)
        curve = [(t * q_ch / v_ch, f) for t, f in trace]

    def _fraction_at(cv: float) -> float:
        if not curve:
            return 0.0
        xs = [p[0] for p in curve]
        ys = [p[1] for p in curve]
        if cv <= xs[0]:
            return ys[0]
        if cv >= xs[-1]:
            return ys[-1]
        return float(np.interp(cv, xs, ys))

    reached = _fraction_at(cv_dose)
    cv_required = None
    nonideality = None
    cv_wm = None
    if target_fraction is not None:
        cv_wm = well_mixed_chamber_volumes(target_fraction)
        cv_required = _first_crossing(curve, target_fraction)
        if cv_required is None:
            notes.append(
                f"{chamber_id!r} had not reached {target_fraction:.4g} after "
                f"{march_to:.1f} chamber volumes (WASHOUT_CV_SEARCH_CAP), so the "
                f"requirement is reported as a lower bound, not a number.")
        elif cv_wm > 0:
            nonideality = cv_required / cv_wm

    sampled = _thin(curve, samples)
    return Washout(
        chamber_id=chamber_id,
        chamber_volume_m3=v_ch,
        q_m3_s=q_ch,
        cv_delivered=cv_dose,
        fraction_reached=reached,
        well_mixed_fraction=well_mixed_fraction(cv_dose),
        curve=sampled,
        target_fraction=target_fraction,
        cv_required_cfd=cv_required,
        cv_required_well_mixed=cv_wm,
        nonideality_factor=nonideality,
        scheme=scheme,
        dt_s=dt,
        steps=steps,
        diffusivity_m2_s=float(diffusivity_m2_s),
        notes=notes,
    )


def _chamber_inflow_m3_s(topo: _Topology, mask: np.ndarray) -> float:
    """Volumetric flow entering one chamber, across both its mouths and its own faces.

    A chamber meshed into cells has interior `hs_face` edges that do not cross its border;
    only faces with exactly one end inside count, plus port mouths on inside nodes.
    """
    total = 0.0
    if len(topo.fq):
        a_in = mask[topo.fa]
        b_in = mask[topo.fb]
        crossing = a_in ^ b_in
        if crossing.any():
            q = topo.fq[crossing]
            into = np.where(b_in[crossing], q, -q)     # positive = entering the chamber
            total += float(np.sum(np.maximum(into, 0.0)))
    if len(topo.bq):
        inside = mask[topo.bi]
        if inside.any():
            total += float(np.sum(np.maximum(-topo.bq[inside], 0.0)))
    return total


def _march(
    topo: _Topology,
    c0: np.ndarray,
    mask: np.ndarray,
    t_end: float,
    d: float,
    scheme: str,
) -> tuple[list[tuple[float, float]], float, int, list[str]]:
    """Time-march the tracer and record (time, exchanged fraction) as it goes."""
    notes: list[str] = []
    vol = topo.volume_m3
    w = vol * mask
    w_sum = float(w.sum())

    def fraction(c: np.ndarray) -> float:
        if w_sum <= 0:
            return 0.0
        return float(1.0 - np.sum(w * c) / w_sum)

    q_ch = _chamber_inflow_m3_s(topo, mask)
    v_ch = float(np.sum(vol[mask]))
    cv_total = (t_end * q_ch / v_ch) if v_ch > 0 and q_ch > 0 else 1.0
    wanted = max(int(math.ceil(cv_total * WASHOUT_STEPS_PER_CV)), 8)

    src = topo.boundary_inflow(0.0, d)       # fresh liquid in: c_inlet = 0
    excursion = 0.0

    def record(c: np.ndarray) -> np.ndarray:
        """Physical bounds. The tracer starts in [0, 1] and nothing feeds it anything
        outside that, so an excursion is discretisation noise, not physics - but it is
        measured and reported rather than quietly clipped away."""
        nonlocal excursion
        excursion = max(excursion, float(np.max(np.abs(c - np.clip(c, 0.0, 1.0)))))
        return np.clip(c, 0.0, 1.0)

    if scheme == "explicit":
        out = topo.outflux(d)
        with np.errstate(divide="ignore", invalid="ignore"):
            safe = np.where((out > 0) & (vol > 0), vol / np.where(out > 0, out, 1.0), np.inf)
        dt_cfl = (CFL_SAFETY * float(np.min(safe)) if np.isfinite(safe).any()
                  else t_end / wanted)
        target_dt = t_end / (wanted * EXPLICIT_STEP_REFINEMENT)
        dt = min(dt_cfl, target_dt)
        if not math.isfinite(dt) or dt <= 0:
            dt = target_dt
        steps = max(int(math.ceil(t_end / dt)), 1)
        dt = t_end / steps
        if dt > dt_cfl / CFL_SAFETY:
            notes.append(
                f"explicit dt {dt:.4g} s is past the CFL stability limit "
                f"{dt_cfl / CFL_SAFETY:.4g} s: the result is not trustworthy. Use the "
                f"implicit scheme.")
        inv_vol = np.divide(1.0, vol, out=np.zeros_like(vol), where=vol > 0)
        c = c0.copy()
        trace = [(0.0, fraction(c))]
        for k in range(steps):
            c = record(c + dt * inv_vol * (src - topo.apply(c, d)))
            trace.append(((k + 1) * dt, fraction(c)))
        if excursion > 1e-6:
            notes.append(f"explicit tracer left [0, 1] by {excursion:.2e} before clipping.")
        return trace, dt, steps, notes

    # Implicit. A and dt are both constant, so the Crank-Nicolson matrix is factorised
    # ONCE and reused for every step; this repo has no scipy, so a dense inverse is the
    # factorisation available (see DENSE_SOLVE_MAX). A is a diagonally dominant M-matrix
    # and V/dt sits on its diagonal, so the system is well conditioned.
    #
    #   backward Euler  (V/dt + A)     c_new = (V/dt) c_old              + src
    #   Crank-Nicolson  (V/dt + A/2)   c_new = (V/dt - A/2) c_old        + src
    #
    # Rannacher smoothing: the FIRST step is taken as `RANNACHER_SUBSTEPS` backward-Euler
    # substeps of dt/k. The initial condition is a step function at the chamber mouth and
    # CN alone rings on it, but plain BE steps at the full dt would cost O(dt) accuracy -
    # which is exactly the error that would show up as fake non-ideality. Substeps cost
    # O(dt^2), so the scheme stays second order overall.
    steps = wanted
    dt = t_end / steps
    vdt = vol / dt
    sub = RANNACHER_SUBSTEPS
    vdt_sub = vol / (dt / sub)
    # A zero-volume node (a bare junction) gets no V/dt, so its row stays the algebraic
    # steady balance - correct, since it has no capacity to store tracer.

    if topo.n <= DENSE_SOLVE_MAX:
        a = topo.dense(d)
        diag_ix = (np.arange(topo.n), np.arange(topo.n))
        m_cn = 0.5 * a
        m_cn[diag_ix] += vdt
        cn_inv = np.linalg.inv(m_cn)
        m_be_sub = a.copy()
        m_be_sub[diag_ix] += vdt_sub

        def solve_be(rhs, guess):
            return np.linalg.solve(m_be_sub, rhs)

        def solve_cn(rhs, guess):
            return cn_inv @ rhs
    else:
        a_diag = topo.outflux(d)
        iters = []

        def solve_be(rhs, guess):
            x, info = _bicgstab(lambda v: topo.apply(v, d) + vdt_sub * v, rhs,
                                a_diag + vdt_sub, x0=guess)
            iters.append(info["iterations"])
            return x

        def solve_cn(rhs, guess):
            x, info = _bicgstab(lambda v: 0.5 * topo.apply(v, d) + vdt * v, rhs,
                                0.5 * a_diag + vdt, x0=guess)
            iters.append(info["iterations"])
            return x

    c = c0.copy()
    trace = [(0.0, fraction(c))]
    for k in range(steps):
        if k == 0:
            for _ in range(sub):
                c = record(solve_be(vdt_sub * c + src, c))
        else:
            c = record(solve_cn(vdt * c - 0.5 * topo.apply(c, d) + src, c))
        trace.append(((k + 1) * dt, fraction(c)))
    if topo.n > DENSE_SOLVE_MAX:
        notes.append(f"matrix-free implicit solve on {topo.n} unknowns: "
                     f"{sum(iters)} BiCGSTAB iterations over {steps} time steps "
                     f"({sum(iters) / max(len(iters), 1):.1f} per step).")
    if excursion > 1e-6:
        notes.append(f"Crank-Nicolson tracer left [0, 1] by {excursion:.2e} before "
                     f"clipping; raise WASHOUT_STEPS_PER_CV if that matters.")
    return trace, dt, steps, notes


def _first_crossing(curve: Sequence[tuple[float, float]], target: float) -> float | None:
    """The chamber volumes at which the washout curve first reaches `target`."""
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        if y1 + 1e-15 >= target:
            if y1 == y0:
                return x1
            return x0 + (target - y0) * (x1 - x0) / (y1 - y0)
    if curve and curve[0][1] >= target:
        return curve[0][0]
    return None


def _thin(curve: Sequence[tuple[float, float]], samples: int) -> list[tuple[float, float]]:
    if samples <= 0 or len(curve) <= samples:
        return [(float(a), float(b)) for a, b in curve]
    idx = np.unique(np.linspace(0, len(curve) - 1, samples).astype(int))
    return [(float(curve[i][0]), float(curve[i][1])) for i in idx]


# --- 3. the comparison with intent ------------------------------------------

@dataclass(frozen=True)
class ExchangeIntent:
    """What an exchange step is FOR, read from data rather than from English.

    `exchange_kind` in contracts/schemas/protocol.schema.json is the authority, and its
    absence is NOT a loophole: the schema says an unclassified exchange is held to the
    sweep rule, so `kind_source` records whether the kind was stated or defaulted.
    """

    kind: str
    kind_source: str
    target_fraction: float | None
    target_source: str
    volume_ul: float | None
    chamber_volumes: float | None
    bounded_above_by_chamber: bool
    unresolved: tuple[str, ...]

    @property
    def well_mixed_chamber_volumes(self) -> float | None:
        if self.target_fraction is None or self.target_fraction >= 1.0:
            return None
        return well_mixed_chamber_volumes(self.target_fraction)

    def to_json(self) -> dict[str, Any]:
        return {
            "exchange_kind": self.kind,
            "exchange_kind_source": self.kind_source,
            "target_fraction": self.target_fraction,
            "target_fraction_source": self.target_source,
            "volume_ul": self.volume_ul,
            "chamber_volumes_delivered": self.chamber_volumes,
            "chamber_volumes_required_well_mixed": self.well_mixed_chamber_volumes,
            "bounded_above_by_chamber_volume": self.bounded_above_by_chamber,
            "unresolved": list(self.unresolved),
        }


_FRACTION_WORDS = {
    "half": 0.5, "a half": 0.5, "one half": 0.5,
    "third": 1 / 3, "a third": 1 / 3, "one third": 1 / 3,
    "two thirds": 2 / 3, "quarter": 0.25, "a quarter": 0.25,
    "three quarters": 0.75,
}
_PERCENT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_RATIO_RE = re.compile(r"\b(\d+)\s*/\s*(\d+)\b")
_DECIMAL_RE = re.compile(r"\b(0?\.\d+)\b")


def stated_partial_fraction(step: Any) -> float | None:
    """The fraction a `partial` exchange replaces, as stated in the step.

    The schema requires a partial exchange's `note` to say what fraction of the chamber is
    replaced, and gives it nowhere machine-readable, so this reads the note: "50%",
    "half", "1/2", "0.5". Returns None when the note does not say, and the caller records
    that as unresolved rather than assuming a number - a partial exchange whose fraction
    nobody stated cannot be checked against anything.
    """
    for key in ("target_fraction", "exchange_fraction", "fraction"):
        if _attr(step, key) is not None:
            return float(_attr(step, key))
    note = str(_attr(step, "note") or "").lower()
    if not note:
        return None
    m = _PERCENT_RE.search(note)
    if m:
        return float(m.group(1)) / 100.0
    m = _RATIO_RE.search(note)
    if m and float(m.group(2)) != 0:
        value = float(m.group(1)) / float(m.group(2))
        if 0 < value <= 1:
            return value
    for word, value in sorted(_FRACTION_WORDS.items(), key=lambda kv: -len(kv[0])):
        if word in note:
            return value
    m = _DECIMAL_RE.search(note)
    if m:
        value = float(m.group(1))
        if 0 < value < 1:
            return value
    return None


def exchange_intent(step: Any, chamber_volume_ul: float) -> ExchangeIntent:
    """Turn one protocol step into the target its CFD result must be judged against.

    sweep (or absent)  exchange.target_fraction from contracts/parameters.json.
    partial            the fraction the step states. Not trying to clear the chamber, so
                       the one-chamber-volume floor does not apply.
    fill               the chamber full: bounded ABOVE by its volume_ul, no lower bound.

    `step` is either a RAW protocol step (a dict straight out of `protocols/*.json`) or
    scenario.py's `ScenarioRecord`, which has already parsed one. Both are accepted
    because they carry different things: the raw step is the only place that still knows
    whether `exchange_kind` was STATED or ABSENT, and the record is the one that resolved
    `washes x volume_ul` into `volume_total_ul`. Where they overlap the record wins, so
    transport and scenario cannot drift apart on the volume arithmetic.
    """
    unresolved: list[str] = []
    number = _attr(step, "step")
    from_record = _attr(step, "volume_total_ul") is not None

    kind = _attr(step, "exchange_kind")
    if kind is None:
        kind, kind_source = "sweep", (
            "defaulted: the step states no exchange_kind, and protocol.schema.json holds "
            "an unclassified exchange to the STRICT sweep rule")
    elif from_record:
        kind, kind_source = str(kind), (
            "exchange_kind via scenario.py, which applies the same strict sweep default: "
            "'stated' and 'absent' are no longer distinguishable here, so pass the raw "
            "protocol step if that provenance matters")
    else:
        kind, kind_source = str(kind), "exchange_kind (data)"

    washes = _attr(step, "washes")
    if washes is None:
        washes = _attr(step, "repeats")
    volume_ul = _attr(step, "volume_ul")
    total_ul: float | None = _attr(step, "volume_total_ul")

    if total_ul is None and volume_ul is None:
        unresolved.append(f"step {number}: volume_ul is null, so no volume was delivered "
                          f"to compare against")
    elif total_ul is None:
        total_ul = float(volume_ul) * (int(washes) if washes else 1)
    if total_ul is not None:
        total_ul = float(total_ul)
    if washes and int(washes) > 1:
        unresolved.append(
            f"step {number}: {int(washes)} washes x {float(volume_ul or 0):g} uL is "
            f"modelled as {total_ul:g} uL of CONTINUOUS flow, the same arithmetic "
            f"tools/validate.py and scenario.py use. A fill-and-drain cycle that truly "
            f"emptied the chamber between washes would clear it faster than continuous "
            f"flow - but it would also put air in a 0.3 mm chamber, and nothing in the "
            f"chip spec says these chambers can be drained to air. Continuous flow is the "
            f"defensible reading; the alternative needs a vent and a priming check.")

    cv = (total_ul / chamber_volume_ul) if (total_ul is not None and chamber_volume_ul > 0) else None

    if kind == "sweep":
        target = float(contracts.param("exchange.target_fraction"))
        target_source = "contracts/parameters.json exchange.target_fraction"
        if contracts.is_assumption("exchange.target_fraction"):
            target_source += " (contract flags it an assumption)"
        bounded = False
    elif kind == "partial":
        target = stated_partial_fraction(step)
        if target is None:
            target_source = "not stated"
            unresolved.append(
                f"step {number}: exchange_kind is 'partial' but neither the step "
                f"nor its note states what fraction of the chamber is replaced, which "
                f"protocol.schema.json requires. Nothing to check the CFD result against.")
        else:
            target_source = "stated by the step (note)"
        bounded = False
    elif kind == "fill":
        target = 1.0
        target_source = "fill: the chamber full (bounded above by its volume_ul)"
        bounded = True
    else:
        target = None
        target_source = "unknown exchange_kind"
        unresolved.append(f"step {number}: unknown exchange_kind {kind!r}; "
                          f"protocol.schema.json allows partial, sweep, fill or absent")
        bounded = False

    return ExchangeIntent(
        kind=kind,
        kind_source=kind_source,
        target_fraction=target,
        target_source=target_source,
        volume_ul=total_ul,
        chamber_volumes=cv,
        bounded_above_by_chamber=bounded,
        unresolved=tuple(unresolved),
    )


@dataclass
class ExchangeCheck:
    """A step's intent against what the field says it actually reached."""

    chip_id: str | None
    step: int | None
    chamber_id: str
    intent: ExchangeIntent
    washout: Washout | None
    shortfall: float | None
    extra_chamber_volumes_needed: float | None
    nonideality_factor: float | None
    severity: str
    message: str
    unresolved: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "chip_id": self.chip_id,
            "step": self.step,
            "chamber_id": self.chamber_id,
            "intent": self.intent.to_json(),
            "washout": self.washout.to_json() if self.washout else None,
            "shortfall_fraction": self.shortfall,
            "extra_chamber_volumes_needed": self.extra_chamber_volumes_needed,
            "nonideality_factor": self.nonideality_factor,
            "severity": self.severity,
            "message": self.message,
            "unresolved": list(self.unresolved),
        }


def check_exchange(
    mesh: Any,
    field: Any,
    step: Any,                       # raw protocol step or scenario.ScenarioRecord
    chamber_id: str,
    *,
    chamber_volume_ul: float | None = None,
    diffusivity_m2_s: float = 0.0,
    scheme: str = "auto",
    chip_id: str | None = None,
    topology: _Topology | None = None,
) -> ExchangeCheck:
    """Compare one exchange step's INTENT with the exchange the field actually delivers.

    The well-mixed rule already catches a step that cannot possibly work: 99% needs 4.61
    chamber volumes, so 1-2.5 is short before any geometry is considered. This adds what
    the well-mixed rule cannot know - whether THIS chamber at THIS flow is better or worse
    than a stirred tank. See `Washout.regime`: a chamber swept end to end beats 4.61, one
    fed and drained from the same edge loses to it, and only the second case makes the
    well-mixed figure unsafe.
    """
    topo = topology if topology is not None else build_topology(mesh, field)
    mask = topo.node_mask(chamber_id)
    if not mask.any():
        raise ValueError(f"transport.check_exchange: no mesh node has feature_id {chamber_id!r}")
    v_ch_ul = float(np.sum(topo.volume_m3[mask])) / _M3_PER_UL
    if chamber_volume_ul is None:
        chamber_volume_ul = v_ch_ul

    intent = exchange_intent(step, chamber_volume_ul)
    unresolved = list(intent.unresolved)

    if intent.chamber_volumes is None:
        return ExchangeCheck(
            chip_id=chip_id, step=_attr(step, "step"), chamber_id=chamber_id, intent=intent,
            washout=None, shortfall=None, extra_chamber_volumes_needed=None,
            nonideality_factor=None, severity="medium",
            message=f"step {_attr(step, 'step')} on {chamber_id}: no volume to simulate.",
            unresolved=unresolved)

    cv = intent.chamber_volumes
    if intent.bounded_above_by_chamber:
        cv = min(cv, 1.0)

    wash = washout(mesh, field, chamber_id, chamber_volumes=cv,
                   target_fraction=intent.target_fraction,
                   diffusivity_m2_s=diffusivity_m2_s, scheme=scheme, topology=topo)
    unresolved.extend(n for n in wash.notes if "no diffusivity" in n)

    target = intent.target_fraction
    shortfall = None if target is None else max(target - wash.fraction_reached, 0.0)
    extra = None
    if target is not None and wash.cv_required_cfd is not None:
        extra = max(wash.cv_required_cfd - cv, 0.0)

    severity, message = _exchange_verdict(intent, wash, chamber_id, cv, shortfall, extra)
    return ExchangeCheck(
        chip_id=chip_id,
        step=_attr(step, "step"),
        chamber_id=chamber_id,
        intent=intent,
        washout=wash,
        shortfall=shortfall,
        extra_chamber_volumes_needed=extra,
        nonideality_factor=wash.nonideality_factor,
        severity=severity,
        message=message,
        unresolved=unresolved,
    )


def _exchange_verdict(
    intent: ExchangeIntent,
    wash: Washout,
    chamber_id: str,
    cv: float,
    shortfall: float | None,
    extra: float | None,
) -> tuple[str, str]:
    kind_note = "" if intent.kind_source.startswith("exchange_kind") else " (kind defaulted to sweep)"
    if intent.target_fraction is None:
        return "medium", (f"{chamber_id}: {intent.kind} exchange with no stated target, so "
                          f"the {wash.fraction_reached * 100:.1f}% the field reaches after "
                          f"{cv:.2f} chamber volumes cannot be judged.")
    if intent.kind == "fill":
        if wash.fraction_reached >= intent.target_fraction - 1e-9:
            return "low", (f"{chamber_id}: fill reaches "
                           f"{wash.fraction_reached * 100:.1f}% of the chamber.")
        return "high", (f"{chamber_id}: fill reaches only "
                        f"{wash.fraction_reached * 100:.1f}% of the chamber in one chamber "
                        f"volume - {(1 - wash.fraction_reached) * 100:.1f}% stays "
                        f"unwetted, which is a prime-failure / bubble risk.")
    wm = intent.well_mixed_chamber_volumes or 0.0
    if intent.kind == "sweep" and cv < 1.0:
        return "blocker", (
            f"{chamber_id}: sweep moves {cv:.4g} chamber volumes{kind_note}. Below ONE "
            f"chamber volume the step cannot clear the chamber however many times it is "
            f"repeated; the field reaches {wash.fraction_reached * 100:.1f}% against a "
            f"target of {intent.target_fraction * 100:.1f}%.")
    if shortfall and shortfall > 1e-6:
        need = (f"{wash.cv_required_cfd:.2f}" if wash.cv_required_cfd is not None
                else f">{WASHOUT_CV_SEARCH_CAP:.0f}")
        severity = "high" if shortfall > 0.05 else "medium"
        return severity, (
            f"{chamber_id}: {intent.kind} moves {cv:.2f} chamber volumes{kind_note} and "
            f"reaches {wash.fraction_reached * 100:.2f}%, short of the "
            f"{intent.target_fraction * 100:.1f}% target by "
            f"{shortfall * 100:.2f} points. Well mixed would need {wm:.2f} chamber "
            f"volumes; THIS chamber needs {need}"
            + (f" ({wash.nonideality_factor:.2f}x the well-mixed figure)"
               if wash.nonideality_factor else "")
            + (f", so {extra:.2f} more than the step delivers." if extra else "."))
    factor = (f" It needed {wash.cv_required_cfd:.2f} chamber volumes against the "
              f"well-mixed {wm:.2f} ({wash.nonideality_factor:.2f}x)."
              if wash.cv_required_cfd is not None and wash.nonideality_factor else "")
    return "low", (f"{chamber_id}: {intent.kind} reaches "
                   f"{wash.fraction_reached * 100:.2f}% against a "
                   f"{intent.target_fraction * 100:.1f}% target in {cv:.2f} chamber "
                   f"volumes.{factor}")


# --- 4. dead zones -----------------------------------------------------------

@dataclass
class DeadZones:
    """Low-velocity / long-residence-time volume in one chamber.

    Hele-Shaw has no recirculation, so this - not a closed streamline - is what a trap IS.
    Both criteria are reported, because they catch different things: a long-age cell may
    still be moving slowly but steadily through, while a low-speed cell next to the inlet
    may still be young.
    """

    chamber_id: str
    volume_m3: float
    cell_count: int
    mean_age_s: float
    median_age_s: float
    p95_age_s: float
    max_age_s: float
    mean_speed_mm_s: float
    age_threshold_s: float
    speed_threshold_mm_s: float
    long_age_volume_fraction: float
    low_speed_volume_fraction: float
    either_volume_fraction: float
    both_volume_fraction: float
    unreachable_volume_fraction: float
    dead_depth_mean_mm: float | None
    dead_depth_max_mm: float | None
    diffusive_clearance_mean_s: float | None
    diffusive_clearance_max_s: float | None
    diffusivity: Diffusivity | None
    thresholds: dict[str, Any]
    notes: list[str]

    @property
    def volume_ul(self) -> float:
        return self.volume_m3 / _M3_PER_UL

    def to_json(self) -> dict[str, Any]:
        return {
            "chamber_id": self.chamber_id,
            "chamber_volume_ul": self.volume_ul,
            "cells": self.cell_count,
            "mean_age_s": self.mean_age_s,
            "median_age_s": self.median_age_s,
            "p95_age_s": self.p95_age_s,
            "max_age_s": self.max_age_s,
            "mean_speed_mm_s": self.mean_speed_mm_s,
            "age_threshold_s": self.age_threshold_s,
            "speed_threshold_mm_s": self.speed_threshold_mm_s,
            "long_age_volume_fraction": self.long_age_volume_fraction,
            "low_speed_volume_fraction": self.low_speed_volume_fraction,
            "dead_volume_fraction": self.either_volume_fraction,
            "both_criteria_volume_fraction": self.both_volume_fraction,
            "unreachable_volume_fraction": self.unreachable_volume_fraction,
            "dead_depth_mean_mm": self.dead_depth_mean_mm,
            "dead_depth_max_mm": self.dead_depth_max_mm,
            "diffusive_clearance_mean_s": self.diffusive_clearance_mean_s,
            "diffusive_clearance_max_s": self.diffusive_clearance_max_s,
            "diffusivity": self.diffusivity.to_json() if self.diffusivity else None,
            "thresholds": self.thresholds,
            "notes": list(self.notes),
        }


def dead_zones(
    mesh: Any,
    field: Any,
    age: AgeField,
    chamber_id: str,
    *,
    age_factor: float = DEAD_ZONE_AGE_FACTOR,
    speed_fraction: float = DEAD_ZONE_SPEED_FRACTION,
    species: str = "dye",
    topology: _Topology | None = None,
) -> DeadZones:
    """Volume fraction of one chamber that is a low-velocity or long-age zone.

    Thresholds are relative to the chamber's own volume-weighted mean, so a slow chamber
    is not condemned for being slow - what matters is one part of it flushing far slower
    than the rest. Both are ASSUMPTIONS and travel into the result's `thresholds`.

    The diffusive clearance time is the other half of the story and the reason diffusion
    is in here at all: a dead zone a distance L from moving liquid clears in about
    L^2 / 2D by molecular diffusion alone with no help from the flow. L is measured as the
    in-plane distance from each dead cell to the nearest live one, so the mean and the max
    bracket "how long would it take if we just waited".
    """
    topo = topology if topology is not None else build_topology(mesh, field)
    notes = list(topo.notes)
    mask = topo.node_mask(chamber_id)
    if not mask.any():
        raise ValueError(f"transport.dead_zones: no mesh node has feature_id {chamber_id!r}")

    idx = np.flatnonzero(mask)
    vol = topo.volume_m3[idx]
    total = float(vol.sum())
    ages = np.array([age.age_s.get(topo.ids[i], math.inf) for i in idx], dtype=float)
    speeds = topo.speed_mm_s[idx]

    finite = np.isfinite(ages)
    unreachable_fraction = float(vol[~finite].sum() / total) if total > 0 else 0.0
    if finite.any() and total > 0:
        w = vol[finite]
        mean_age = float(np.sum(w * ages[finite]) / w.sum())
        order = np.argsort(ages[finite])
        cum = np.cumsum(w[order]) / w.sum()
        sorted_ages = ages[finite][order]
        median_age = float(sorted_ages[int(np.searchsorted(cum, 0.5))])
        p95_age = float(sorted_ages[min(int(np.searchsorted(cum, 0.95)), len(sorted_ages) - 1)])
        max_age = float(sorted_ages[-1])
    else:
        mean_age = median_age = p95_age = max_age = math.inf
        notes.append(f"{chamber_id!r}: no cell has a finite age; the chamber has no "
                     f"outflow path in this field.")

    mean_speed = float(np.sum(vol * speeds) / total) if total > 0 else 0.0
    age_threshold = age_factor * mean_age if math.isfinite(mean_age) else math.inf
    speed_threshold = speed_fraction * mean_speed

    long_age = (~finite) | (ages > age_threshold)
    low_speed = speeds < speed_threshold
    either = long_age | low_speed
    both = long_age & low_speed

    def frac(sel: np.ndarray) -> float:
        return float(vol[sel].sum() / total) if total > 0 else 0.0

    depth_mean = depth_max = None
    clear_mean = clear_max = None
    diff = None
    if either.any() and not either.all():
        dead_xy = np.column_stack([topo.x_mm[idx][either], topo.y_mm[idx][either]])
        live_xy = np.column_stack([topo.x_mm[idx][~either], topo.y_mm[idx][~either]])
        if np.isfinite(dead_xy).all() and np.isfinite(live_xy).all():
            d2 = ((dead_xy[:, None, :] - live_xy[None, :, :]) ** 2).sum(axis=2)
            depths = np.sqrt(d2.min(axis=1))
            dead_vol = vol[either]
            depth_mean = float(np.sum(dead_vol * depths) / dead_vol.sum())
            depth_max = float(depths.max())
            diff = diffusivity(species)
            clear_mean = (depth_mean * 1e-3) ** 2 / (2.0 * diff.m2_s)
            clear_max = (depth_max * 1e-3) ** 2 / (2.0 * diff.m2_s)
            if diff.unresolved_note:
                notes.append(diff.unresolved_note)
        else:
            notes.append("Mesh nodes have no x_mm/y_mm, so the diffusive clearance length "
                         "could not be measured.")
    elif either.all() and either.any():
        notes.append(f"{chamber_id!r}: every cell meets a dead-zone criterion, so there is "
                     f"no live liquid to diffuse into. Either the chamber is not being "
                     f"flushed at all in this step, or the thresholds are too loose here.")

    return DeadZones(
        chamber_id=chamber_id,
        volume_m3=total,
        cell_count=int(mask.sum()),
        mean_age_s=mean_age,
        median_age_s=median_age,
        p95_age_s=p95_age,
        max_age_s=max_age,
        mean_speed_mm_s=mean_speed,
        age_threshold_s=age_threshold,
        speed_threshold_mm_s=speed_threshold,
        long_age_volume_fraction=frac(long_age),
        low_speed_volume_fraction=frac(low_speed),
        either_volume_fraction=frac(either),
        both_volume_fraction=frac(both),
        unreachable_volume_fraction=unreachable_fraction,
        dead_depth_mean_mm=depth_mean,
        dead_depth_max_mm=depth_max,
        diffusive_clearance_mean_s=clear_mean,
        diffusive_clearance_max_s=clear_max,
        diffusivity=diff,
        thresholds={
            "age_factor": {**ASSUMPTIONS["dead_zone_age_factor"], "value": age_factor},
            "speed_fraction": {**ASSUMPTIONS["dead_zone_speed_fraction"],
                              "value": speed_fraction},
        },
        notes=notes,
    )


# --- the whole thing on one field -------------------------------------------

def chamber_ids(mesh: Any) -> list[str]:
    """Every `feature_id` that owns at least one node of kind `cell`, in mesh order."""
    out: list[str] = []
    for node in _items(_attr(mesh, "nodes")):
        if _attr(node, "kind") != "cell":
            continue
        feat = _attr(node, "feature_id")
        if feat is not None and str(feat) not in out:
            out.append(str(feat))
    return out


def analyse(
    mesh: Any,
    field: Any,
    *,
    step: Any = None,                # raw protocol step or scenario.ScenarioRecord
    chambers: Iterable[str] | None = None,
    diffusivity_m2_s: float | None = None,
    species: str = "dye",
    scheme: str = "auto",
) -> dict[str, Any]:
    """Age, washout, intent and dead zones for every chamber in one field, as JSON.

    `diffusivity_m2_s=None` takes the contract's value for `species` (flagged, see
    `diffusivity`). Pass 0.0 for convection only - worth doing deliberately, because it
    brackets the answer: no diffusion is the pessimistic dead-zone case and the difference
    between the two is exactly what diffusion is worth in this chamber.
    """
    topo = build_topology(mesh, field)
    diff_record = None
    if diffusivity_m2_s is None:
        diff_record = diffusivity(species)
        d = diff_record.m2_s
    else:
        d = float(diffusivity_m2_s)

    age = mean_age(mesh, field, diffusivity_m2_s=d, topology=topo)
    targets = list(chambers) if chambers is not None else chamber_ids(mesh)
    unresolved: list[str] = []
    if diff_record and diff_record.unresolved_note:
        unresolved.append(diff_record.unresolved_note)

    out: dict[str, Any] = {
        "chip_id": _attr(_attr(mesh, "meta") or {}, "chip_id"),
        "step": _attr(step, "step") if step is not None else None,
        "age": age.to_json(),
        "diffusivity": diff_record.to_json() if diff_record else {"diffusivity_m2_s": d},
        "assumptions": ASSUMPTIONS,
        "chambers": {},
        "unresolved": unresolved,
    }
    for cid in targets:
        entry: dict[str, Any] = {
            "dead_zones": dead_zones(mesh, field, age, cid, species=species,
                                     topology=topo).to_json(),
        }
        if step is not None and _attr(step, "operation") == "exchange":
            step_targets = (_attr(step, "targets")
                            or ([_attr(step, "target")] if _attr(step, "target") else []))
            if cid in step_targets:
                check = check_exchange(mesh, field, step, cid, diffusivity_m2_s=d,
                                       scheme=scheme, chip_id=out["chip_id"], topology=topo)
                entry["exchange"] = check.to_json()
                unresolved.extend(check.unresolved)
        out["chambers"][cid] = entry
    return out

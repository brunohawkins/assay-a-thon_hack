"""The shapes every `assay2chip/cfd/` module passes around: Mesh, Scenario, Field, Finding.

Owned by the mesh module, imported by all ten. The field names are the ones in the
"Module contracts" table of `assay2chip/cfd/README.md` and that table is authoritative:
if a name here and a name there ever disagree, the README wins and this file is the bug.

    from assay2chip.cfd.model import Mesh, Scenario, Field, Finding, PortState

    mesh = Mesh.load("cfd/module1_v1.mesh.json")
    cold = mesh.at_viscosity(2.5e-3)      # same geometry, a different fluid
    field = Field(p_pa={...}, q_m3_s={...})
    Finding(id="leak_1", cls="leak", severity="high", ...).to_json()["class"]  # -> 'leak'

UNITS. SI inside the objects, with the unit written into the key name at the JSON
boundary exactly as the README asks: `*_mm`, `*_mm2`, `*_pa`, `*_m3_s`, `*_ul_min`,
`*_dyn_cm2`. The three exceptions are the README's own names and are documented where
they appear: `Field.re` (dimensionless), `Field.residual.q_in` / `q_out` (m^3/s).

VISCOSITY IS NOT A PROPERTY OF THE MESH, AND THAT IS WHY EVERY EDGE CARRIES TWO NUMBERS.
A protocol runs at 37 C in medium, then a 4 C rinse, then cold organic solvent, so mu
changes from step to step while the geometry does not. Every edge therefore stores

    conductance_m3_per_pa_s   the conductance AT `Mesh.meta["mu_pa_s"]`, ready to solve
    g_geom_m3                 conductance x mu: pure geometry, in m^3, independent of fluid

so a solver can rescale any mesh to any fluid with `conductance = g_geom_m3 / mu` - or
just call `Mesh.at_viscosity(mu)` - instead of re-meshing. Both forms come from the same
expression, so `g_geom_m3 / mu` is exact, not an approximation: for a rectangular duct
`g_geom_m3 = w h^3 (1 - 0.63 h/w) / (12 L)` and for a Hele-Shaw face `h^3 W / (12 s)`.
Never read a viscosity from anywhere but `assay2chip.lib.contracts.viscosity_for(temp_c)`.

Nothing in this module computes physics or touches a contract: it is shapes, validation
and JSON only, so importing it is cheap and cannot fail on a missing contract key.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = "cfd-model-1"

# --- unit conversions, in one place so no module writes 1e-9 / 60 again ---------------

UL_MIN_TO_M3_S = 1e-9 / 60.0
M3_S_TO_UL_MIN = 1.0 / UL_MIN_TO_M3_S
MM_TO_M = 1e-3
M_TO_MM = 1e3
MM2_TO_M2 = 1e-6
DYN_CM2_PER_PA = 10.0          # 1 Pa = 10 dyn/cm^2
UL_PER_MM3 = 1.0               # 1 mm^3 = 1 uL

# --- the closed vocabularies ----------------------------------------------------------

NODE_KINDS = ("cell", "junction", "port")
#: `duct|hs_face|mouth` are the README's three. `slit` is the fourth and it is the weir:
#: the README's meshing rules require a weir chamber to get a crest edge of gap
#: `retention.opening_um`, whose flow scales with gap^3, and a solver must be able to
#: find those edges to sweep the +/-20 um z tolerance (`Mesh.slit_edges()`).
EDGE_KINDS = ("duct", "hs_face", "mouth", "slit")
PORT_STATE_TYPES = ("flow", "pressure", "closed")
SEVERITIES = ("blocker", "high", "medium", "low")
#: The classes in the README's findings table. `analyse.py` may add to this list; it must
#: not invent a severity.
FINDING_CLASSES = (
    "leak", "trap", "dead_end", "bubble", "shear", "sediment", "clog", "handoff",
    "unmodelled",
)


class ModelError(ValueError):
    """A shape is not what the README says it is (bad kind, dangling edge, unknown port)."""


def _known(cls: type, data: dict, rename: dict[str, str] | None = None) -> dict:
    """The subset of `data` that `cls` actually has fields for, keys renamed first.

    Unknown keys are dropped rather than raising, so a newer writer and an older reader
    still talk to each other; anything a module must not lose belongs in `meta`.
    """
    out = dict(data)
    for src, dst in (rename or {}).items():
        if src in out:
            out[dst] = out.pop(src)
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in out.items() if k in names}


def _clean(value: Any) -> Any:
    """JSON-safe: tuples to lists, numpy scalars to floats, NaN/inf to None."""
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, bool) or value is None or isinstance(value, (str, int)):
        return value
    if hasattr(value, "item") and not isinstance(value, (float, str)):   # numpy scalar
        value = value.item()
    if isinstance(value, float):
        return None if math.isnan(value) or math.isinf(value) else value
    return value


# --- Mesh -----------------------------------------------------------------------------


@dataclass
class Node:
    """One node of the mesh: a Hele-Shaw cell, a junction or a port.

    kind        cell      a depth-averaged chamber cell on the raster grid
                junction  a point node: a channel end, a small chamber, a weir crest
                port       a through-hole to tubing; `Mesh.ports` maps port id -> node id
    x_mm, y_mm  the node's position in chip coordinates (chamber cell = cell centre)
    area_mm2    in-plane area the node owns (a cell's tile area, clipped to the footprint;
                a duct end's cross-section; a port's bore). The sum over one chamber's
                cells is that chamber's footprint area, which `tests/test_cfd_mesh.py`
                checks to 1%.
    depth_mm    the etch depth at this node (the chip has one, but a weir crest does not).
    feature_id  the chip-spec id this node belongs to, so every finding can name a part.
    ix, iy      raster indices for `cell` nodes, None otherwise: figures.py plots with them.
    boundary    True when a cell touches the footprint edge, i.e. it can carry a mouth.
    """

    id: str
    kind: str = "cell"
    x_mm: float = 0.0
    y_mm: float = 0.0
    area_mm2: float = 0.0
    depth_mm: float = 0.0
    feature_id: str = ""
    ix: int | None = None
    iy: int | None = None
    boundary: bool = False
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in NODE_KINDS:
            raise ModelError(
                f"node {self.id!r}: kind {self.kind!r} is not one of {', '.join(NODE_KINDS)}"
            )

    @property
    def volume_ul(self) -> float:
        """Wetted volume this node owns (1 mm^3 = 1 uL). Transport integrates these."""
        return self.area_mm2 * self.depth_mm * UL_PER_MM3

    def to_json(self) -> dict:
        return _clean({
            "id": self.id, "kind": self.kind, "x_mm": self.x_mm, "y_mm": self.y_mm,
            "area_mm2": self.area_mm2, "depth_mm": self.depth_mm,
            "feature_id": self.feature_id, "ix": self.ix, "iy": self.iy,
            "boundary": self.boundary, **({"note": self.note} if self.note else {}),
        })

    @classmethod
    def from_json(cls, data: dict) -> "Node":
        return cls(**_known(cls, data))


@dataclass
class Edge:
    """One edge: a 1D duct, a face between two Hele-Shaw cells, a mouth or a weir slit.

    kind     duct     a rectangular channel, R = 12 mu L / (w h^3 (1 - 0.63 h/w)),
                      h the SMALLER and w the LARGER of width and depth
             hs_face  a face between neighbouring chamber cells,
                      g = (h^3 / 12 mu) x (face length / centre spacing)
             mouth    where a channel meets a chamber: one edge per boundary cell the
                      opening spans, so the inflow is spread instead of entering a point
             slit     a weir crest: gap `gap_um` in z, `length_mm` along the flow, across
                      the chamber's full width. Flow goes as gap^3 - sweep it.
    a, b     node ids. Flow is signed a -> b everywhere downstream (`Field.q_m3_s`).
    conductance_m3_per_pa_s  Q = conductance x dP, at `Mesh.meta["mu_pa_s"]`.
    g_geom_m3                conductance x mu: geometry only. See the module docstring.
    gap_um   slits only: the weir gap, so a tolerance sweep can rebuild the conductance.
    """

    id: str
    a: str = ""
    b: str = ""
    kind: str = "duct"
    conductance_m3_per_pa_s: float = 0.0
    length_mm: float = 0.0
    width_mm: float = 0.0
    depth_mm: float = 0.0
    feature_id: str = ""
    g_geom_m3: float = 0.0
    gap_um: float | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.kind not in EDGE_KINDS:
            raise ModelError(
                f"edge {self.id!r}: kind {self.kind!r} is not one of {', '.join(EDGE_KINDS)}"
            )

    @property
    def resistance_pa_s_m3(self) -> float:
        """1 / conductance, or inf for a closed edge."""
        g = self.conductance_m3_per_pa_s
        return math.inf if g <= 0.0 else 1.0 / g

    @property
    def area_mm2(self) -> float:
        """Cross-section the flow passes through: width x depth (slit: width x gap)."""
        if self.kind == "slit" and self.gap_um:
            return self.width_mm * self.gap_um / 1000.0
        return self.width_mm * self.depth_mm

    @property
    def volume_ul(self) -> float:
        return self.area_mm2 * self.length_mm * UL_PER_MM3

    def conductance_at(self, mu_pa_s: float) -> float:
        """The same geometry in a fluid of viscosity `mu_pa_s`: g_geom_m3 / mu. Exact."""
        if mu_pa_s <= 0.0:
            raise ModelError(f"viscosity must be positive, got {mu_pa_s!r}")
        return self.g_geom_m3 / mu_pa_s

    def at_viscosity(self, mu_pa_s: float) -> "Edge":
        from dataclasses import replace
        return replace(self, conductance_m3_per_pa_s=self.conductance_at(mu_pa_s))

    def touches(self, node_id: str) -> bool:
        return node_id in (self.a, self.b)

    def other(self, node_id: str) -> str:
        if node_id == self.a:
            return self.b
        if node_id == self.b:
            return self.a
        raise ModelError(f"edge {self.id!r} does not touch node {node_id!r}")

    def to_json(self) -> dict:
        out = {
            "id": self.id, "a": self.a, "b": self.b, "kind": self.kind,
            "conductance_m3_per_pa_s": self.conductance_m3_per_pa_s,
            "g_geom_m3": self.g_geom_m3, "length_mm": self.length_mm,
            "width_mm": self.width_mm, "depth_mm": self.depth_mm,
            "feature_id": self.feature_id,
        }
        if self.gap_um is not None:
            out["gap_um"] = self.gap_um
        if self.note:
            out["note"] = self.note
        return _clean(out)

    @classmethod
    def from_json(cls, data: dict) -> "Edge":
        return cls(**_known(cls, data))


@dataclass
class Mesh:
    """A chip discretised: `cfd/<chip_id>.mesh.json`. Written by mesh.py, read by all.

    nodes / edges  as above, in a deterministic order (same spec in, same file out).
    ports          port id from the chip spec -> node id in this mesh. The only legal
                   place to drive or vent the chip, so `Scenario.ports` keys live here.
    meta           chip_id, grid_mm, mu_pa_s, plus the mesher's own record: per-feature
                   model choice, cell counts, area checks and `unresolved` - every
                   feature it could not mesh from the spec and why. Read it before
                   trusting a number that depends on an assumed lane width.
    """

    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    ports: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.nodes = [n if isinstance(n, Node) else Node.from_json(n) for n in self.nodes]
        self.edges = [e if isinstance(e, Edge) else Edge.from_json(e) for e in self.edges]

    # -- lookups ----------------------------------------------------------------

    @property
    def chip_id(self) -> str:
        return str(self.meta.get("chip_id", ""))

    @property
    def mu_pa_s(self) -> float:
        """The viscosity `conductance_m3_per_pa_s` was built at. Raises if meta lacks it."""
        if "mu_pa_s" not in self.meta:
            raise ModelError("mesh meta has no 'mu_pa_s': which fluid are these conductances?")
        return float(self.meta["mu_pa_s"])

    def node(self, node_id: str) -> Node:
        try:
            return self._node_index[node_id]
        except KeyError:
            raise ModelError(f"no node {node_id!r} in mesh {self.chip_id!r}") from None

    def edge(self, edge_id: str) -> Edge:
        try:
            return self._edge_index[edge_id]
        except KeyError:
            raise ModelError(f"no edge {edge_id!r} in mesh {self.chip_id!r}") from None

    @property
    def _node_index(self) -> dict[str, Node]:
        if getattr(self, "_ncache", None) is None or len(self._ncache) != len(self.nodes):
            self._ncache = {n.id: n for n in self.nodes}
        return self._ncache

    @property
    def _edge_index(self) -> dict[str, Edge]:
        if getattr(self, "_ecache", None) is None or len(self._ecache) != len(self.edges):
            self._ecache = {e.id: e for e in self.edges}
        return self._ecache

    def nodes_of_kind(self, kind: str) -> list[Node]:
        return [n for n in self.nodes if n.kind == kind]

    def edges_of_kind(self, kind: str) -> list[Edge]:
        return [e for e in self.edges if e.kind == kind]

    def slit_edges(self) -> list[Edge]:
        """Every weir crest. Sweep these for the +/-20 um z tolerance, the design's
        biggest unknown: `mesh.with_slit_gap_delta(+20)` and `(-20)`."""
        return self.edges_of_kind("slit")

    def adjacency(self) -> dict[str, list[Edge]]:
        out: dict[str, list[Edge]] = {n.id: [] for n in self.nodes}
        for e in self.edges:
            for end in (e.a, e.b):
                if end in out:
                    out[end].append(e)
        return out

    def edges_at(self, node_id: str) -> list[Edge]:
        return [e for e in self.edges if e.touches(node_id)]

    def features(self) -> list[str]:
        seen = {n.feature_id for n in self.nodes if n.feature_id}
        return sorted(seen)

    def cells_of(self, feature_id: str) -> list[Node]:
        return [n for n in self.nodes if n.kind == "cell" and n.feature_id == feature_id]

    def nodes_of(self, feature_id: str) -> list[Node]:
        return [n for n in self.nodes if n.feature_id == feature_id]

    def edges_of(self, feature_id: str) -> list[Edge]:
        return [e for e in self.edges if e.feature_id == feature_id]

    def area_mm2(self, feature_id: str) -> float:
        return sum(n.area_mm2 for n in self.cells_of(feature_id))

    @property
    def volume_ul(self) -> float:
        """Wetted volume: cells and junctions by area x depth, ducts by their own volume."""
        return (sum(n.volume_ul for n in self.nodes if n.kind != "port")
                + sum(e.volume_ul for e in self.edges if e.kind == "duct"))

    def __iter__(self) -> Iterator[Node]:
        return iter(self.nodes)

    def __len__(self) -> int:
        return len(self.nodes)

    # -- fluid swap -------------------------------------------------------------

    def at_viscosity(self, mu_pa_s: float) -> "Mesh":
        """The same geometry solved in a different fluid: every conductance rescaled from
        `g_geom_m3`, `meta.mu_pa_s` updated, nothing re-meshed. This is how a protocol
        whose steps run at 37 C, then 4 C, then in cold solvent uses ONE mesh."""
        meta = dict(self.meta)
        meta["mu_pa_s"] = float(mu_pa_s)
        meta["mu_pa_s_was"] = self.meta.get("mu_pa_s")
        return Mesh([*self.nodes], [e.at_viscosity(mu_pa_s) for e in self.edges],
                    dict(self.ports), meta)

    def with_slit_gap_delta(self, delta_um: float) -> "Mesh":
        """Every weir crest opened or closed by `delta_um` (the +/-20 um z tolerance).

        Conductance goes as gap^3 through (1 - 0.63 h/w) as well, so it is rebuilt from
        the duct expression rather than cubed by hand. A crest closed to or past zero
        becomes a shut edge (conductance 0) - which is the failure mode worth reporting.
        """
        out = []
        for e in self.edges:
            if e.kind != "slit" or e.gap_um is None:
                out.append(e)
                continue
            gap = e.gap_um + delta_um
            from dataclasses import replace
            if gap <= 0.0:
                out.append(replace(e, gap_um=max(gap, 0.0), conductance_m3_per_pa_s=0.0,
                                   g_geom_m3=0.0, note=(e.note + " SHUT by tolerance").strip()))
                continue
            g_geom = slit_g_geom_m3(e.width_mm, gap, e.length_mm)
            out.append(replace(e, gap_um=gap, g_geom_m3=g_geom,
                               conductance_m3_per_pa_s=g_geom / self.mu_pa_s))
        meta = dict(self.meta)
        meta["slit_gap_delta_um"] = delta_um
        return Mesh([*self.nodes], out, dict(self.ports), meta)

    # -- checks -----------------------------------------------------------------

    def reachable_from_ports(self) -> set[str]:
        """Every node a port can reach through the mesh. Anything missing is a dead end."""
        adj = self.adjacency()
        seen: set[str] = set()
        stack = [n for n in self.ports.values() if n in adj]
        while stack:
            here = stack.pop()
            if here in seen:
                continue
            seen.add(here)
            for e in adj[here]:
                nxt = e.other(here)
                if nxt not in seen:
                    stack.append(nxt)
        return seen

    def orphans(self) -> list[str]:
        """Nodes with no edge at all: always a mesher bug, never a design finding."""
        adj = self.adjacency()
        return sorted(n.id for n in self.nodes if not adj[n.id])

    def unreachable(self) -> list[str]:
        """Nodes no port can reach. A design finding (`dead_end`), not necessarily a bug."""
        seen = self.reachable_from_ports()
        return sorted(n.id for n in self.nodes if n.id not in seen)

    def check(self) -> list[str]:
        """Everything structurally wrong with this mesh, as readable lines. Empty is good."""
        problems: list[str] = []
        ids = [n.id for n in self.nodes]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            problems.append(f"duplicate node ids: {', '.join(dupes)}")
        eids = [e.id for e in self.edges]
        if len(set(eids)) != len(eids):
            dupes = sorted({i for i in eids if eids.count(i) > 1})
            problems.append(f"duplicate edge ids: {', '.join(dupes)}")
        known = set(ids)
        for e in self.edges:
            for end in (e.a, e.b):
                if end not in known:
                    problems.append(f"edge {e.id!r} hangs off a node that is not here: {end!r}")
            if e.a == e.b:
                problems.append(f"edge {e.id!r} joins node {e.a!r} to itself")
            if e.conductance_m3_per_pa_s < 0 or e.g_geom_m3 < 0:
                problems.append(f"edge {e.id!r} has a negative conductance")
        for pid, nid in sorted(self.ports.items()):
            if nid not in known:
                problems.append(f"port {pid!r} maps to node {nid!r}, which is not here")
        for nid in self.orphans():
            problems.append(f"node {nid!r} has no edge (orphan)")
        for nid in self.unreachable():
            problems.append(f"node {nid!r} is not reachable from any port")
        if "mu_pa_s" not in self.meta:
            problems.append("meta has no 'mu_pa_s'")
        return problems

    def stats(self) -> dict:
        """What the mesh is, in numbers: counts, areas, conductance span, assumptions."""
        by_node = {k: len(self.nodes_of_kind(k)) for k in NODE_KINDS}
        by_edge = {k: len(self.edges_of_kind(k)) for k in EDGE_KINDS}
        g = sorted(e.conductance_m3_per_pa_s for e in self.edges if e.conductance_m3_per_pa_s > 0)
        return _clean({
            "chip_id": self.chip_id,
            "nodes": len(self.nodes), "edges": len(self.edges),
            "nodes_by_kind": by_node, "edges_by_kind": by_edge,
            "ports": len(self.ports),
            "features": len(self.features()),
            "grid_mm": self.meta.get("grid_mm"),
            "mu_pa_s": self.meta.get("mu_pa_s"),
            "wetted_volume_ul": round(self.volume_ul, 6),
            "conductance_m3_per_pa_s": {
                "min": g[0] if g else None, "max": g[-1] if g else None,
                "span_decades": (round(math.log10(g[-1] / g[0]), 2) if g and g[0] > 0 else None),
            },
            "orphans": self.orphans(),
            "unreachable": self.unreachable(),
            "unresolved": len(self.meta.get("unresolved", [])),
        })

    # -- JSON -------------------------------------------------------------------

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "nodes": [n.to_json() for n in self.nodes],
            "edges": [e.to_json() for e in self.edges],
            "ports": dict(sorted(self.ports.items())),
            "meta": _clean(self.meta),
        }

    @classmethod
    def from_json(cls, data: dict) -> "Mesh":
        return cls(
            nodes=[Node.from_json(n) for n in data.get("nodes", [])],
            edges=[Edge.from_json(e) for e in data.get("edges", [])],
            ports=dict(data.get("ports", {})),
            meta=dict(data.get("meta", {})),
        )

    def save(self, path: Path | str) -> Path:
        return _write_json(path, self.to_json())

    @classmethod
    def load(cls, path: Path | str) -> "Mesh":
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))


def slit_g_geom_m3(width_mm: float, gap_um: float, length_mm: float) -> float:
    """Geometry factor of a weir crest: the rectangular-duct form with h = the gap.

    g_geom = w h^3 (1 - 0.63 h/w) / (12 L), in m^3, so conductance = g_geom / mu. Lives
    here rather than in mesh.py because `Mesh.with_slit_gap_delta` needs it to rebuild a
    swept gap without importing the mesher.
    """
    w, h, length = width_mm * MM_TO_M, gap_um * 1e-6, length_mm * MM_TO_M
    if min(w, h, length) <= 0.0:
        return 0.0
    w, h = max(w, h), min(w, h)
    return w * h ** 3 * (1 - 0.63 * h / w) / (12 * length)


# --- Scenario -------------------------------------------------------------------------


@dataclass
class PortState:
    """How one port is driven in one step: a pump, an open outlet, or a closed valve.

    type  flow      a syringe pump: `q_ul_min` into the chip (negative draws out)
          pressure  held at `p_pa` (an open vent is pressure 0)
          closed    capped: no flow. A chip with flow in and nothing open has no solution.
    """

    type: str = "closed"
    q_ul_min: float | None = None
    p_pa: float | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.type not in PORT_STATE_TYPES:
            raise ModelError(
                f"port state {self.type!r} is not one of {', '.join(PORT_STATE_TYPES)}"
            )
        if self.type == "flow" and self.q_ul_min is None:
            raise ModelError("a 'flow' port needs q_ul_min")
        if self.type == "pressure" and self.p_pa is None:
            raise ModelError("a 'pressure' port needs p_pa")

    @property
    def q_m3_s(self) -> float:
        return 0.0 if self.q_ul_min is None else self.q_ul_min * UL_MIN_TO_M3_S

    @property
    def is_flow(self) -> bool:
        return self.type == "flow"

    @property
    def is_pressure(self) -> bool:
        return self.type == "pressure"

    @property
    def is_closed(self) -> bool:
        return self.type == "closed"

    def to_json(self) -> dict:
        out: dict[str, Any] = {"type": self.type}
        if self.q_ul_min is not None:
            out["q_ul_min"] = self.q_ul_min
        if self.p_pa is not None:
            out["p_pa"] = self.p_pa
        if self.note:
            out["note"] = self.note
        return _clean(out)

    @classmethod
    def from_json(cls, data: "dict | PortState | str") -> "PortState":
        if isinstance(data, PortState):
            return data
        if isinstance(data, str):            # "closed" / "vent" shorthand
            return cls("pressure", p_pa=0.0) if data in ("vent", "open") else cls(data)
        return cls(**_known(cls, dict(data)))

    @classmethod
    def vent(cls) -> "PortState":
        return cls("pressure", p_pa=0.0)

    @classmethod
    def flow(cls, q_ul_min: float) -> "PortState":
        return cls("flow", q_ul_min=q_ul_min)


@dataclass
class Scenario:
    """One protocol step as boundary conditions on one mesh (`scenario.py`).

    step     the step number or id from `protocols/<id>.json`
    ports    port id (a key of `Mesh.ports`) -> PortState, from `ports.feed/vent/cap`
    fluid    what is flowing, in the protocol's own words ('medium', 'cold quench')
    temp_c   the step's temperature: `contracts.viscosity_for(temp_c)` picks mu from it
    mu_pa_s  the viscosity actually used, once resolved, so a Field can be reproduced
    unresolved  anything assumed rather than read (a fluid the contract does not cover);
                a result that rests on an assumption must say so.
    """

    step: int | str | None = None
    ports: dict[str, PortState] = field(default_factory=dict)
    fluid: str = ""
    temp_c: float | None = None
    mu_pa_s: float | None = None
    chip_id: str = ""
    flow_ul_min: float | None = None
    unresolved: list[dict] = field(default_factory=list)
    note: str = ""

    def __post_init__(self) -> None:
        self.ports = {k: PortState.from_json(v) for k, v in self.ports.items()}

    def driven(self) -> dict[str, PortState]:
        return {k: v for k, v in sorted(self.ports.items()) if v.is_flow}

    def fixed(self) -> dict[str, PortState]:
        return {k: v for k, v in sorted(self.ports.items()) if v.is_pressure}

    def closed(self) -> list[str]:
        return [k for k, v in sorted(self.ports.items()) if v.is_closed]

    @property
    def q_in_m3_s(self) -> float:
        return sum(p.q_m3_s for p in self.ports.values() if p.is_flow)

    def for_mesh(self, mesh: Mesh) -> "Scenario":
        """This scenario checked against a mesh: unknown port ids are refused, and ports
        the step does not mention are capped, which is what the chip does."""
        unknown = sorted(set(self.ports) - set(mesh.ports))
        if unknown:
            raise ModelError(
                f"scenario step {self.step!r} drives {', '.join(unknown)}, which "
                f"{mesh.chip_id or 'this chip'} does not have: it has "
                f"{', '.join(sorted(mesh.ports))}"
            )
        ports = {pid: self.ports.get(pid, PortState("closed")) for pid in sorted(mesh.ports)}
        from dataclasses import replace
        return replace(self, ports=ports, chip_id=self.chip_id or mesh.chip_id)

    def to_json(self) -> dict:
        out = {
            "step": self.step,
            "ports": {k: v.to_json() for k, v in sorted(self.ports.items())},
            "fluid": self.fluid, "temp_c": self.temp_c, "mu_pa_s": self.mu_pa_s,
            "chip_id": self.chip_id,
        }
        if self.flow_ul_min is not None:
            out["flow_ul_min"] = self.flow_ul_min
        if self.unresolved:
            out["unresolved"] = self.unresolved
        if self.note:
            out["note"] = self.note
        return _clean(out)

    @classmethod
    def from_json(cls, data: dict) -> "Scenario":
        kw = _known(cls, dict(data))
        kw["ports"] = {k: PortState.from_json(v) for k, v in dict(data.get("ports", {})).items()}
        return cls(**kw)


# --- Field ----------------------------------------------------------------------------


@dataclass
class Residual:
    """How well the solve closed. `q_in`/`q_out` are m^3/s - the README's own key names.

    mass_imbalance_max is the worst |net flow| at any node, in m^3/s: it must be tiny
    next to q_in, and `validate.py` is the module that decides what tiny means.
    """

    mass_imbalance_max: float = 0.0
    q_in: float = 0.0
    q_out: float = 0.0

    @property
    def closure(self) -> float:
        """|q_in - q_out| / q_in: 0 is perfect. 1 means nothing came out."""
        return abs(self.q_in - self.q_out) / self.q_in if self.q_in else 0.0

    def to_json(self) -> dict:
        return _clean({"mass_imbalance_max": self.mass_imbalance_max,
                       "q_in": self.q_in, "q_out": self.q_out})

    @classmethod
    def from_json(cls, data: "dict | Residual | None") -> "Residual":
        if isinstance(data, Residual):
            return data
        return cls(**_known(cls, dict(data or {})))


@dataclass
class Field:
    """One solved step on one mesh: `cfd/<chip_id>.field.json` (`solve.py`).

    p_pa            node id -> pressure, Pa (gauge: an open vent is 0)
    q_m3_s          edge id -> volumetric flow, SIGNED a -> b
    u_mm_s          cell node id -> depth-averaged velocity [ux, uy], mm/s
    shear_dyn_cm2   edge id OR cell node id -> wall shear, dyn/cm^2 (one flat dict: the
                    keys are unique across nodes and edges, and the README asks for
                    `[edge|cell]`)
    re              edge id -> Reynolds number, dimensionless. Above 1 the Stokes model
                    is out of its validity and analyse.py must report it as `unmodelled`
                    rather than trust the number.
    residual        see Residual.
    meta            chip_id, step, fluid, temp_c, mu_pa_s: what was solved, so a figure
                    or a finding can label itself without re-reading the scenario.
    """

    p_pa: dict[str, float] = field(default_factory=dict)
    q_m3_s: dict[str, float] = field(default_factory=dict)
    u_mm_s: dict[str, list[float]] = field(default_factory=dict)
    shear_dyn_cm2: dict[str, float] = field(default_factory=dict)
    re: dict[str, float] = field(default_factory=dict)
    residual: Residual = field(default_factory=Residual)
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.residual = Residual.from_json(self.residual)
        self.u_mm_s = {k: list(v) for k, v in self.u_mm_s.items()}

    @property
    def chip_id(self) -> str:
        return str(self.meta.get("chip_id", ""))

    @property
    def step(self) -> Any:
        return self.meta.get("step")

    def q_ul_min(self, edge_id: str) -> float:
        return self.q_m3_s[edge_id] * M3_S_TO_UL_MIN

    def speed_mm_s(self, node_id: str) -> float:
        ux, uy = self.u_mm_s.get(node_id, (0.0, 0.0))[:2]
        return math.hypot(ux, uy)

    def max_pressure_pa(self) -> float:
        return max(self.p_pa.values()) if self.p_pa else 0.0

    def over_re(self, limit: float = 1.0) -> list[str]:
        """Edges where Stokes flow stops being the right physics, worst first."""
        return [e for e, _ in sorted(((e, r) for e, r in self.re.items() if r > limit),
                                     key=lambda kv: -kv[1])]

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA_VERSION,
            "p_pa": _clean(self.p_pa), "q_m3_s": _clean(self.q_m3_s),
            "u_mm_s": _clean(self.u_mm_s), "shear_dyn_cm2": _clean(self.shear_dyn_cm2),
            "re": _clean(self.re), "residual": self.residual.to_json(),
            "meta": _clean(self.meta),
        }

    @classmethod
    def from_json(cls, data: dict) -> "Field":
        kw = _known(cls, dict(data))
        kw["residual"] = Residual.from_json(data.get("residual"))
        return cls(**kw)

    def save(self, path: Path | str) -> Path:
        return _write_json(path, self.to_json())

    @classmethod
    def load(cls, path: Path | str) -> "Field":
        return cls.from_json(json.loads(Path(path).read_text(encoding="utf-8")))


# --- Finding --------------------------------------------------------------------------


@dataclass
class Finding:
    """One thing wrong with the design (`analyse.py` -> `cfd/findings.json`).

    THE JSON KEY IS `class`, WHICH PYTHON WILL NOT LET US NAME A FIELD, so the attribute
    is `cls` and `to_json()` writes `class`. `from_json()` accepts either.

    A finding is two numbers that disagree, named: `metric` is what was measured, `value`
    what it came to, `limit` what it had to stay inside, `units` the unit of both. Keep
    `message` a sentence a human can act on, and put the contract or file the limit came
    from in `source`, so nobody has to guess whether a limit is published or assumed.
    """

    id: str = ""
    cls: str = ""
    severity: str = "medium"
    chip_id: str = ""
    feature_id: str = ""
    step: int | str | None = None
    metric: str = ""
    value: float | None = None
    limit: float | None = None
    units: str = ""
    message: str = ""
    fix_hint: str = ""
    source: str = ""
    assumption: bool = False
    audit: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITIES:
            raise ModelError(
                f"finding {self.id!r}: severity {self.severity!r} is not one of "
                f"{', '.join(SEVERITIES)}"
            )

    @property
    def over_by(self) -> float | None:
        """value / limit, the number a report leads with. None when either is missing."""
        if self.value is None or not self.limit:
            return None
        return self.value / self.limit

    def to_json(self) -> dict:
        out = {
            "id": self.id, "class": self.cls, "severity": self.severity,
            "chip_id": self.chip_id, "feature_id": self.feature_id, "step": self.step,
            "metric": self.metric, "value": self.value, "limit": self.limit,
            "units": self.units, "message": self.message, "fix_hint": self.fix_hint,
        }
        if self.source:
            out["source"] = self.source
        if self.assumption:
            out["assumption"] = True
        if self.audit:
            out["audit"] = self.audit
        return _clean(out)

    @classmethod
    def from_json(cls, data: dict) -> "Finding":
        return cls(**_known(cls, dict(data), rename={"class": "cls"}))


def findings_to_json(findings: list[Finding]) -> list[dict]:
    """`cfd/findings.json`, worst first then by chip and feature: a stable report order."""
    order = {s: i for i, s in enumerate(SEVERITIES)}
    return [f.to_json() for f in sorted(
        findings, key=lambda f: (order.get(f.severity, 9), f.chip_id, f.cls, f.feature_id, f.id))]


def findings_from_json(data: list[dict]) -> list[Finding]:
    return [Finding.from_json(d) for d in data]


# --- writing --------------------------------------------------------------------------


def _write_json(path: Path | str, data: Any) -> Path:
    """Atomic 2-space JSON with a trailing newline - the same bytes as
    `assay2chip.lib.io.write_json`, imported lazily so `model` stays dependency-free."""
    try:
        import sys
        root = Path(__file__).resolve().parents[2]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        from assay2chip.lib.io import write_json
        return write_json(path, data)
    except Exception:                                  # pragma: no cover - fallback only
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return path

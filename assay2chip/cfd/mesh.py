"""chip/<id>.json -> `Mesh`: the hybrid discretisation the rest of cfd/ solves on.

    python assay2chip/cfd/mesh.py chip/module1_v1.json          # stats to stdout
    python assay2chip/cfd/mesh.py --all --write                  # every repo chip
    python assay2chip/cfd/mesh.py --all --json                   # one JSON report

    from assay2chip.cfd.mesh import build_mesh, mesh_chip
    mesh = mesh_chip("chip/module1_v1.json")

TWO MODELS IN ONE MESH, because the chip has two kinds of geometry and using one model
for both is what produced the two worst errors in the audits.

  CHAMBERS are 2D. A chamber footprint is rasterised onto a depth-averaged Hele-Shaw grid
  (0.1 mm cells by default, refined so the narrow dimension of a narrow chamber still gets
  at least MIN_CELLS_ACROSS cells). Face conductance between neighbouring cells is
  (h^3 / 12 mu) x (face length / centre spacing). A chamber must not be one node: a wide
  chamber collapsed to a node looks like a pipe and hides the dead zones we are hunting.

  CHANNELS are 1D. One duct edge at the exact rectangular resistance

      R = 12 mu L / (w h^3 f)       h the SMALLER, w the LARGER side

  where f is the EXACT Fourier series for Poiseuille flow in a rectangle
  (`flow_calc.duct_shape_factor`). This used to read `f = 1 - 0.63 h/w`; that shortcut is
  14% off at a square duct, so flow_calc.py moved to the series and this follows it by
  calling it. Nothing here re-types the law.
  Taken from `operator/flow_calc.py` rather than re-typed, with L the real centreline
  length: the `path` polyline when the spec gives one, otherwise the straight line between
  the two features trimmed at their walls exactly as `cad/make_train_cad.py` cuts it.

  CHANNEL-LIKE BLOCKS ARE CHANNELS EVEN WHEN THE SPEC FILES THEM AS CHAMBERS.
  `serpentine_channel`, `herringbone_mixer`, `spiral_channel`, `centipede_channel`, every
  `*_channel` and a `mixer` whose kind is serpentine are ducts at their REAL LANE WIDTH
  and PATH LENGTH - never the bounding box. Meshing a serpentine as a 2D chamber is the
  43x shear error the audit found: a 0.2 mm lane read as a 4.5 mm chamber is 22x too wide
  and the shear that follows is meaningless. Where the spec does not give a lane width the
  mesher says so in `meta.unresolved` instead of quietly using the box.

  MOUTHS. Where a channel meets a chamber the opening is spread over the boundary cells it
  spans, so flow enters over a width instead of at a point. A channel that runs ALONG a
  chamber wall (a comb spine, a manifold) feeds the whole length it covers - that is what
  `centipede_channel` does on Module 6 and a single-point mouth there would invent a dead
  zone over 8 mm of culture field.

  WEIRS. A chamber whose `retention.type` is weir drains over a crest: its cells join a
  crest node across the chamber's FULL wall width, and one `slit` edge of gap
  `retention.opening_um` and length `retention.crest_thickness_um` carries the flow out.
  Flow through the slit goes as gap^3, and the +/-20 um z tolerance on a 25 um gap is
  +/-80% - the biggest unknown in the retention scheme - so sweep it with
  `mesh.with_slit_gap_delta(+20)` / `(-20)` rather than quoting one number.

  PORTS. One node per port, joined to the feature its bore opens onto: through the channel
  that names it, or - for a port with no channel - by a short bore edge into the feature
  underneath it.

EVERY EDGE CARRIES TWO CONDUCTANCES. `conductance_m3_per_pa_s` at `meta.mu_pa_s` and
`g_geom_m3` = conductance x mu, which is pure geometry. A protocol runs at 37 C in medium,
then a 4 C rinse, then cold organic solvent: the solver rescales with
`Mesh.at_viscosity(mu)` instead of re-meshing, and `g_geom_m3 / mu` is exact because both
numbers come from the same expression. Viscosity itself only ever comes from
`assay2chip.lib.contracts.viscosity_for(temp_c)`.

Deterministic: ids and ordering come from the spec, so the same chip in gives byte-identical
JSON out. Standard library plus `operator/flow_calc.py` and `assay2chip/lib`; no numpy here.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "operator")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import flow_calc  # noqa: E402  operator/flow_calc.py: the one place the R formula lives
import schema_io  # noqa: E402

from assay2chip.lib import contracts  # noqa: E402
from assay2chip.cfd.model import (  # noqa: E402
    MM_TO_M,
    Edge,
    Mesh,
    ModelError,
    Node,
    slit_g_geom_m3,
)

# --- mesher parameters (NOT contract values: these are discretisation choices) ---------

#: Default Hele-Shaw cell size. A mesh parameter, not a fabrication number.
DEFAULT_GRID_MM = 0.1
#: A narrow chamber is refined until its narrow dimension holds at least this many cells.
#: Fewer than this and the cross-channel velocity profile - which is what a trap finding
#: rests on - is not resolved at all.
MIN_CELLS_ACROSS = 8
#: Raster cells overlapping the footprint by less than this are dropped (round chambers).
COVERAGE_FLOOR = 0.02
#: Subsamples per axis when measuring how much of a cell a round footprint covers.
COVERAGE_SUBSAMPLES = 16

#: Blocks that are channels however the spec files them. Anything ending in `_channel` is
#: matched by rule; these are the ones whose names do not say so.
CHANNEL_LIKE_BLOCKS = frozenset({
    "herringbone_mixer",
    "asymmetric_serpentine",
    "round_maze",
    "square_maze",
    "gradient_generator",
    "branching_arbor",
})
#: `channel_corner` is deliberately NOT in there: it is a corner node, not a run of channel.
NOT_CHANNEL_LIKE_BLOCKS = frozenset({"channel_corner", "trap_channel_note"})


class MeshError(ValueError):
    """The spec cannot be meshed at all (missing geometry a mesh cannot do without)."""


# --- geometry -------------------------------------------------------------------------


@dataclass(frozen=True)
class Footprint:
    """A feature's plan outline: a box, or a circle when the spec says `shape: cylinder`."""

    cx: float
    cy: float
    sx: float
    sy: float
    shape: str = "box"

    @property
    def area_mm2(self) -> float:
        if self.shape == "cylinder":
            return math.pi * (min(self.sx, self.sy) / 2.0) ** 2
        return self.sx * self.sy

    @property
    def span_min(self) -> float:
        return min(self.sx, self.sy)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.cx - self.sx / 2, self.cx + self.sx / 2,
                self.cy - self.sy / 2, self.cy + self.sy / 2)

    def contains(self, x: float, y: float) -> bool:
        x0, x1, y0, y1 = self.bounds
        if self.shape == "cylinder":
            r = min(self.sx, self.sy) / 2.0
            return math.hypot(x - self.cx, y - self.cy) <= r
        return x0 <= x <= x1 and y0 <= y <= y1

    def coverage(self, x0: float, y0: float, x1: float, y1: float) -> float:
        """Fraction of the rectangle that lies inside the footprint.

        Exactly 1 for a box (the raster tiles it), measured by subsampling for a circle -
        which is why a round chamber's cell areas still add up to pi r^2 rather than to a
        staircase, and why the 1% area check in tests/test_cfd_mesh.py passes for both.
        """
        if self.shape != "cylinder":
            return 1.0
        corners = [self.contains(x, y) for x in (x0, x1) for y in (y0, y1)]
        if all(corners) and self.contains((x0 + x1) / 2, (y0 + y1) / 2):
            return 1.0
        if not any(corners) and not self.contains((x0 + x1) / 2, (y0 + y1) / 2):
            # A circle can still clip a corner-free cell, so measure rather than assume.
            pass
        n = COVERAGE_SUBSAMPLES
        inside = 0
        for i in range(n):
            x = x0 + (i + 0.5) * (x1 - x0) / n
            for j in range(n):
                y = y0 + (j + 0.5) * (y1 - y0) / n
                if self.contains(x, y):
                    inside += 1
        return inside / (n * n)


@dataclass(frozen=True)
class Wall:
    """One side of a box footprint: the line a mouth opens through."""

    name: str
    axis: str            # 'x': a vertical wall at x = pos, spanning y in [lo, hi]
    pos: float
    lo: float
    hi: float

    @property
    def length(self) -> float:
        return self.hi - self.lo

    def point(self, along: float) -> tuple[float, float]:
        return (self.pos, along) if self.axis == "x" else (along, self.pos)

    def along(self, p: tuple[float, float]) -> float:
        return p[1] if self.axis == "x" else p[0]

    def distance(self, p: tuple[float, float]) -> float:
        a = min(max(self.along(p), self.lo), self.hi)
        return math.dist(p, self.point(a))


def walls_of(fp: Footprint) -> list[Wall]:
    x0, x1, y0, y1 = fp.bounds
    return [
        Wall("x_min", "x", x0, y0, y1),
        Wall("x_max", "x", x1, y0, y1),
        Wall("y_min", "y", y0, x0, x1),
        Wall("y_max", "y", y1, x0, x1),
    ]


def clip_to_box(p0: tuple[float, float], p1: tuple[float, float],
                center: Iterable[float], size: Iterable[float]) -> tuple[float, float]:
    """Move p0 along p0 -> p1 until it leaves the box: the rule `cad/make_train_cad.py`
    trims channels with, so a meshed channel is the length the DXF actually draws.

    Kept here as the same few lines rather than imported, because cad/make_train_cad.py is
    a CAD entry point and the CFD package must not depend on it to mesh a chip.
    """
    (cx, cy), (sx, sy) = tuple(center)[:2], tuple(size)[:2]
    x0, x1, y0, y1 = cx - sx / 2, cx + sx / 2, cy - sy / 2, cy + sy / 2
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    best = 0.0
    for lo, hi, start, delta in ((x0, x1, p0[0], dx), (y0, y1, p0[1], dy)):
        if abs(delta) < 1e-9:
            continue
        for bound in (lo, hi):
            t = (bound - start) / delta
            if 0.0 <= t <= 1.0:
                px, py = p0[0] + dx * t, p0[1] + dy * t
                if x0 - 1e-6 <= px <= x1 + 1e-6 and y0 - 1e-6 <= py <= y1 + 1e-6:
                    best = max(best, t)
    return (p0[0] + dx * best, p0[1] + dy * best)


def polyline_length_mm(points: list[Iterable[float]]) -> float:
    pts = [tuple(p)[:2] for p in points]
    return sum(math.dist(a, b) for a, b in zip(pts[:-1], pts[1:]))


# --- physics (both forms of every conductance) ----------------------------------------


def duct_g_geom_m3(length_mm: float, width_mm: float, depth_mm: float) -> float:
    """Geometry factor of a rectangular duct: conductance x mu, in m^3.

    Straight from `R = 12 mu L / (w h^3 f)` in operator/flow_calc.py - f the exact series,
    not the retired 1 - 0.63 h/w shortcut - with mu divided out, so `g_geom / mu`
    reproduces 1/R exactly for any fluid. It CALLS flow_calc, so the two cannot drift. h is the
    SMALLER and w the LARGER of width and depth - getting that round the wrong way turns
    a 0.2 x 0.3 mm channel into a 0.3 x 0.2 mm one and the resistance with it.
    """
    if min(length_mm, width_mm, depth_mm) <= 0.0:
        return 0.0
    return 1.0 / flow_calc.rect_channel_resistance_pa_s_m3(length_mm, width_mm, depth_mm, 1.0)


def hs_g_geom_m3(depth_mm: float, face_mm: float, spacing_mm: float) -> float:
    """Geometry factor of a Hele-Shaw face: (h^3 / 12) x (face length / spacing), in m^3.

    Depth-averaged plane Stokes flow: Q = (h^3 / 12 mu) W dP / L. This is a potential
    flow, so it has no eddies - a trap in a chamber is a slow or long-residence region,
    never a closed streamline (see the physics note in the README).
    """
    if min(depth_mm, face_mm, spacing_mm) <= 0.0:
        return 0.0
    h, w, s = depth_mm * MM_TO_M, face_mm * MM_TO_M, spacing_mm * MM_TO_M
    return h ** 3 * w / (12.0 * s)


# --- the mesher -----------------------------------------------------------------------


class Mesher:
    """Builds one `Mesh` from one chip spec. Use `build_mesh()`; this is the machinery."""

    def __init__(self, chip: dict, grid_mm: float | None = None,
                 mu_pa_s: float | None = None, min_cells_across: int = MIN_CELLS_ACROSS,
                 temp_c: float | None = None):
        self.chip = chip
        self.chip_id = str(chip.get("id", "")) or "unknown_chip"
        self.grid_mm = float(grid_mm or DEFAULT_GRID_MM)
        self.min_cells_across = int(min_cells_across)
        visc = contracts.viscosity_for(temp_c)
        self.mu = float(mu_pa_s) if mu_pa_s else visc.pa_s
        self.mu_source = (f"{visc.key} ({visc.scope})" if not mu_pa_s
                          else f"caller override mu = {mu_pa_s} Pa s")
        self.port_bore_mm = float(contracts.floor("min_port_mm"))
        self.min_lane_mm = float(contracts.floor("min_open_channel_mm"))
        self.min_wall_mm = float(contracts.floor("min_wall_mm"))

        self.chambers: dict[str, dict] = {c["id"]: c for c in chip.get("chambers", [])}
        self.ports: dict[str, dict] = {p["id"]: p for p in chip.get("ports", [])}
        self.channels: list[dict] = list(chip.get("channels", []))

        self.nodes: list[Node] = []
        self._by_id: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.port_nodes: dict[str, str] = {}
        self.unresolved: list[dict] = []
        self.feature_notes: list[dict] = []
        #: feature id -> its grid, for the modules that walk a chamber cell by cell
        #: (particles.py, figures.py). See `meta.grids` in `build`.
        self.grids: dict[str, dict] = {}
        #: chamber id -> every crest on it: which wall, which opening, what gap.
        self.weir_sides: dict[str, list[dict]] = {}
        #: chamber id -> how it was meshed, used when a channel attaches to it
        self._model: dict[str, dict] = {}
        #: duct-meshed feature -> {channel tag: which END of the run it attaches to}
        self._duct_attach: dict[str, dict[str, str]] = {}

    # -- small helpers ----------------------------------------------------------

    def _add_node(self, node: Node) -> str:
        if node.id in self._by_id:
            raise MeshError(f"{self.chip_id}: two nodes would share the id {node.id!r}")
        self.nodes.append(node)
        self._by_id[node.id] = node
        return node.id

    def _add_edge(self, edge_id: str, a: str, b: str, kind: str, g_geom: float,
                  length_mm: float, width_mm: float, depth_mm: float, feature_id: str,
                  gap_um: float | None = None, note: str = "") -> None:
        self.edges.append(Edge(
            id=edge_id, a=a, b=b, kind=kind,
            conductance_m3_per_pa_s=g_geom / self.mu if g_geom > 0 else 0.0,
            g_geom_m3=g_geom, length_mm=length_mm, width_mm=width_mm,
            depth_mm=depth_mm, feature_id=feature_id, gap_um=gap_um, note=note,
        ))

    def _flag(self, feature_id: str, question: str, assumed: str = "",
              effect: str = "") -> None:
        """Record something the spec does not say. A guessed number that reads as fact is
        the failure mode this repo keeps being bitten by, so it goes in the file."""
        entry = {"feature_id": feature_id, "question": question}
        if assumed:
            entry["assumed"] = assumed
        if effect:
            entry["effect"] = effect
        if entry not in self.unresolved:        # one weir with three openings is one gap
            self.unresolved.append(entry)

    def footprint(self, chamber: dict) -> Footprint:
        cx, cy = float(chamber["center"][0]), float(chamber["center"][1])
        sx, sy = float(chamber["size"][0]), float(chamber["size"][1])
        return Footprint(cx, cy, sx, sy, str(chamber.get("shape", "box")))

    def depth_mm(self, chamber: dict) -> float:
        """Etch depth of a chamber: `height_um` when given in um precision, else size[2]."""
        if chamber.get("height_um"):
            return float(chamber["height_um"]) / 1000.0
        return float(chamber["size"][2])

    def box_of(self, feature_id: str) -> tuple[list[float], list[float]]:
        """(center, size) used to trim a channel at this feature, as the CAD does."""
        if feature_id in self.chambers:
            c = self.chambers[feature_id]
            return list(c["center"]), list(c["size"])
        if feature_id in self.ports:
            p = self.ports[feature_id]
            bore = (float(p["width_um"]) / 1000.0) if p.get("width_um") else self.port_bore_mm
            return list(p["position"]), [bore, bore, 0.0]
        raise MeshError(
            f"{self.chip_id}: a channel names {feature_id!r}, which is neither a chamber "
            f"nor a port in this spec"
        )

    # -- classification ---------------------------------------------------------

    def is_channel_like(self, chamber: dict) -> bool:
        """True when this 'chamber' is really a run of channel (see the module docstring)."""
        block = str(chamber.get("block", ""))
        if block in NOT_CHANNEL_LIKE_BLOCKS:
            return False
        if block.endswith("_channel") or block in CHANNEL_LIKE_BLOCKS:
            return True
        mixer = chamber.get("mixer") or {}
        return mixer.get("kind") == "serpentine"

    def is_point_feature(self, chamber: dict) -> bool:
        """A chamber too small to hold a 2D field: a junction, a corner, a nozzle.

        The test is geometric rather than a list of block names: a feature narrower than
        two fabrication-floor channel widths cannot support a cross-channel profile, so
        rasterising it would be arithmetic without physics. A 2.4 mm cross_junction is
        still rasterised; a 0.3 mm one is a node.
        """
        fp = self.footprint(chamber)
        return fp.span_min < 2.0 * self.min_lane_mm

    # -- chambers ---------------------------------------------------------------

    def mesh_chambers(self) -> None:
        for cid in sorted(self.chambers):
            chamber = self.chambers[cid]
            if self.is_channel_like(chamber):
                self._mesh_duct_chamber(chamber)
            elif self.is_point_feature(chamber):
                self._mesh_point_chamber(chamber)
            else:
                self._mesh_hele_shaw_chamber(chamber)

    def _mesh_point_chamber(self, chamber: dict) -> None:
        cid = chamber["id"]
        fp, depth = self.footprint(chamber), self.depth_mm(chamber)
        node_id = f"j:{cid}"
        self._add_node(Node(node_id, "junction", fp.cx, fp.cy, fp.area_mm2, depth, cid,
                            note=f"{chamber.get('block', chamber.get('type', ''))} "
                                 f"{fp.sx:g} x {fp.sy:g} mm: one node, too small for a "
                                 f"2D field"))
        self._model[cid] = {"kind": "junction", "node": node_id}
        self.grids[cid] = {
            "kind": "junction", "nodes": [node_id], "size": [fp.sx, fp.sy],
            "depth_mm": depth, "center": [fp.cx, fp.cy], "shape": fp.shape,
        }
        self.feature_notes.append({
            "feature_id": cid, "model": "junction", "block": chamber.get("block"),
            "reason": f"narrower than 2 x min_open_channel_mm "
                      f"({2 * self.min_lane_mm:g} mm)",
            "nodes": 1, "footprint_area_mm2": round(fp.area_mm2, 6),
        })
        if chamber.get("filter") or chamber.get("mesh_pore_um"):
            self._flag(cid, "pillar/mesh filter geometry inside the feature is not meshed",
                       "a clear node", "its extra resistance is missing from the network")

    def _mesh_hele_shaw_chamber(self, chamber: dict) -> None:
        """Rasterise a chamber footprint and wire neighbouring cells with Hele-Shaw faces."""
        cid = chamber["id"]
        fp, depth = self.footprint(chamber), self.depth_mm(chamber)
        target = min(self.grid_mm, fp.span_min / self.min_cells_across)
        nx = max(1, int(round(fp.sx / target)))
        ny = max(1, int(round(fp.sy / target)))
        if fp.sx <= fp.sy:
            nx = max(nx, self.min_cells_across)
        if fp.sy <= fp.sx:
            ny = max(ny, self.min_cells_across)
        dx, dy = fp.sx / nx, fp.sy / ny
        x0, _, y0, _ = fp.bounds

        cells: dict[tuple[int, int], tuple[str, float]] = {}
        for ix in range(nx):
            for iy in range(ny):
                cx0, cy0 = x0 + ix * dx, y0 + iy * dy
                cov = fp.coverage(cx0, cy0, cx0 + dx, cy0 + dy)
                if cov < COVERAGE_FLOOR:
                    continue
                nid = f"c:{cid}:{ix}:{iy}"
                self._add_node(Node(
                    nid, "cell", cx0 + dx / 2, cy0 + dy / 2, cov * dx * dy, depth, cid,
                    ix=ix, iy=iy,
                    boundary=(ix in (0, nx - 1) or iy in (0, ny - 1) or cov < 1.0),
                ))
                cells[(ix, iy)] = (nid, cov)

        if not cells:
            raise MeshError(f"{self.chip_id}: chamber {cid!r} rasterised to no cells at all")

        for (ix, iy), (nid, cov) in sorted(cells.items()):
            for jx, jy, face, spacing in ((ix + 1, iy, dy, dx), (ix, iy + 1, dx, dy)):
                other = cells.get((jx, jy))
                if other is None:
                    continue
                oid, ocov = other
                g = hs_g_geom_m3(depth, face * min(cov, ocov), spacing)
                self._add_edge(f"f:{cid}:{ix}:{iy}>{jx}:{jy}", nid, oid, "hs_face", g,
                               spacing, face * min(cov, ocov), depth, cid)

        area = sum(cov * dx * dy for _, cov in cells.values())
        self.grids[cid] = {
            "kind": "grid", "nx": nx, "ny": ny, "dx_mm": dx, "dy_mm": dy,
            "size": [fp.sx, fp.sy], "depth_mm": depth, "center": [fp.cx, fp.cy],
            "shape": fp.shape,
            # ids[ix][iy], None where a round footprint does not reach. `nodes` is the
            # flat list of the cells that exist, which is the safe one to iterate.
            "ids": [[(cells[(ix, iy)][0] if (ix, iy) in cells else None)
                     for iy in range(ny)] for ix in range(nx)],
            "nodes": [nid for nid, _ in (cells[k] for k in sorted(cells))],
        }
        self._model[cid] = {
            "kind": "hele_shaw", "cells": cells, "nx": nx, "ny": ny, "dx": dx, "dy": dy,
            "footprint": fp, "depth": depth,
        }
        self.feature_notes.append({
            "feature_id": cid, "model": "hele_shaw", "block": chamber.get("block"),
            "grid_mm": [round(dx, 6), round(dy, 6)], "cells": len(cells),
            "nx": nx, "ny": ny, "depth_mm": depth,
            "footprint_area_mm2": round(fp.area_mm2, 6),
            "cell_area_mm2": round(area, 6),
            "area_ratio": round(area / fp.area_mm2, 6) if fp.area_mm2 else None,
        })
        if chamber.get("filter") or chamber.get("mesh_pore_um"):
            kind = (chamber.get("filter") or {}).get("type", "mesh")
            self._flag(cid, f"{kind} filter inside the chamber is not resolved",
                       "an open Hele-Shaw field",
                       "the filter's own resistance and its trapping are missing")

    def duct_attachments(self, cid: str) -> list[tuple[str, tuple[float, float]]]:
        """(tag, anchor) for every channel end that names this feature, from the spec alone.

        Known before anything is meshed, which is what lets a duct-meshed feature be put
        IN the flow path between its neighbours instead of hanging off one of its nodes.
        """
        out: list[tuple[str, tuple[float, float]]] = []
        for channel in self.channels:
            if cid not in (channel.get("from"), channel.get("to")):
                continue
            try:
                seg_a, seg_b, _length, _source = self._terminal_segments(channel)
            except MeshError:
                continue                      # a dangling id; _mesh_channel reports it
            if channel["from"] == cid:
                out.append((f"{channel['id']}:from", seg_a[0]))
            if channel["to"] == cid:
                out.append((f"{channel['id']}:to", seg_b[0]))
        return out

    def _duct_end_points(self, cid: str, fp: Footprint,
                         anchors: list[tuple[str, tuple[float, float]]],
                         ) -> tuple[tuple[float, float], tuple[float, float]]:
        """Where the two ends of a duct-meshed run go: at the two attachments FARTHEST
        apart, so the run spans the gap its neighbours leave.

        The ends used to sit at the footprint's long-axis extremes, which is wrong twice
        over: the extremes are not where the liquid enters, and on a square-ish footprint
        both neighbours are equidistant from both of them, so a nearest-end rule put them
        BOTH on end a. spc_demo_v1's 234 mm delay line then hung off one node as a
        dead-end stub carrying 1.8e-19 of the flow, and its 1194 Pa (duct law) / 1136 Pa
        (OpenFOAM) came out as 39 Pa.
        """
        x0, x1, y0, y1 = fp.bounds
        extremes = (((x0, fp.cy), (x1, fp.cy)) if fp.sx >= fp.sy
                    else ((fp.cx, y0), (fp.cx, y1)))
        if len(anchors) >= 2:
            pairs = [(math.dist(p, q), i, j)
                     for i, (_ti, p) in enumerate(anchors)
                     for j, (_tj, q) in enumerate(anchors) if i < j]
            far, i, j = max(pairs, key=lambda t: (t[0], -t[1], -t[2]))
            if far > 1e-9:
                a, b = anchors[i][1], anchors[j][1]
                return (a, b) if (a[0], a[1]) <= (b[0], b[1]) else (b, a)
            self._flag(cid, "every channel on this run arrives at the same point, so the "
                            "two ends of the run cannot be told apart",
                       "the footprint's long-axis extremes",
                       "the run may not be in series with its neighbours; check the layout")
            return extremes
        if len(anchors) == 1:
            a = anchors[0][1]
            b = max(((x0, fp.cy), (x1, fp.cy), (fp.cx, y0), (fp.cx, y1)),
                    key=lambda p: (math.dist(a, p), p))
            self._flag(cid, "only one channel attaches to this run, so it is a stub",
                       "the far end left open", "nothing flows through it")
            return a, b
        return extremes

    def _mesh_duct_chamber(self, chamber: dict) -> None:
        """A channel-like chamber: two end nodes and ONE duct at the real lane geometry.

        The two ends are placed at - and the attached channels assigned to - opposite ends
        of the run, so a feature the spec puts in series IS in series. Each channel keeps
        its own attachment; what it must not do is share an end with the channel on the
        other side of the run.
        """
        cid = chamber["id"]
        fp, depth = self.footprint(chamber), self.depth_mm(chamber)
        lane = self._lane_geometry(chamber, fp, depth)
        width, length, source = lane["width_mm"], lane["length_mm"], lane["source"]

        anchors = self.duct_attachments(cid)
        ends = self._duct_end_points(cid, fp, anchors)
        ids = []
        for suffix, (px, py) in zip(("a", "b"), ends):
            nid = f"j:{cid}:{suffix}"
            self._add_node(Node(nid, "junction", px, py, 0.0, depth, cid,
                                note=f"{suffix} end of the {chamber.get('block', 'channel')} "
                                     f"run (its volume is on the duct edge)"))
            ids.append(nid)
        # Each attachment to the nearer end, and the pair that defined the ends lands on
        # its own end exactly (distance 0), so the run always spans them.
        self._duct_attach[cid] = {
            tag: min(zip(ids, ends), key=lambda t: (math.dist(anchor, t[1]), t[0]))[0]
            for tag, anchor in anchors
        }
        g = duct_g_geom_m3(length, width, depth)
        self._add_edge(f"d:{cid}", ids[0], ids[1], "duct", g, length, width, depth, cid,
                       note=f"channel-like block meshed as a duct ({source})")
        self._model[cid] = {"kind": "duct", "ends": ids, "footprint": fp,
                            "points": [ends[0], ends[1]]}
        self.grids[cid] = {
            "kind": "duct", "nodes": ids, "edges": [f"d:{cid}"],
            "lane_width_mm": width, "path_length_mm": length, "depth_mm": depth,
            "size": [fp.sx, fp.sy], "center": [fp.cx, fp.cy], "shape": fp.shape,
            "lane_source": source,
        }
        self.feature_notes.append({
            "feature_id": cid, "model": "duct", "block": chamber.get("block"),
            "lane_width_mm": round(width, 6), "path_length_mm": round(length, 6),
            "depth_mm": depth, "lane_source": source,
            "footprint_area_mm2": round(fp.area_mm2, 6),
            "bounding_box_would_be_width_mm": round(fp.span_min, 6),
            "width_error_if_boxed": round(fp.span_min / width, 1) if width else None,
        })

    def _lane_geometry(self, chamber: dict, fp: Footprint, depth: float) -> dict:
        """The real lane width and path length of a channel-like chamber, in priority order.

        1. `mixer.channel_width_mm` + `mixer.length_mm` - the spec says both outright.
        2. `mixer.channel_width_mm` + `volume_ul` - length = V / (w h), UNLESS the stated
           volume is just the bounding box (which several specs record), in which case it
           carries no lane information at all and using it would re-introduce the error.
        3. Nothing usable: fold the footprint at the fabrication floors - lanes of
           `min_open_channel_mm` separated by `min_wall_mm` walls, running along the long
           axis - and FLAG it. That is an assumption, not a measurement, but it is an
           assumption about a 0.2 mm lane instead of a 4.5 mm chamber, which is the
           difference between a shear number that is roughly right and one that is 43x out.
        """
        cid = chamber["id"]
        mixer = chamber.get("mixer") or {}
        width = float(mixer.get("channel_width_mm") or 0.0)
        volume_ul = float(chamber.get("volume_ul") or 0.0)
        boxed = bool(volume_ul) and abs(volume_ul - fp.area_mm2 * depth) <= 0.02 * volume_ul

        if width and mixer.get("length_mm"):
            return {"width_mm": width, "length_mm": float(mixer["length_mm"]),
                    "source": "spec mixer.channel_width_mm + mixer.length_mm"}
        if width and volume_ul and not boxed:
            return {"width_mm": width, "length_mm": volume_ul / (width * depth),
                    "source": "spec mixer.channel_width_mm + volume_ul / (w h)"}

        lane = max(width, self.min_lane_mm)
        pitch = lane + self.min_wall_mm
        run = max(fp.sx, fp.sy)
        lanes = max(1, int(min(fp.sx, fp.sy) // pitch))
        length = lanes * run
        why = ("volume_ul is the bounding-box volume, so it says nothing about the lane"
               if boxed else "the spec gives no lane width or path length")
        self._flag(
            cid,
            f"channel-like block {chamber.get('block')!r}: {why}",
            f"{lanes} lanes of {lane:g} mm at the {self.min_wall_mm:g} mm wall floor "
            f"along the {run:g} mm axis -> {length:g} mm of path",
            "path length and so residence time, resistance and shear are an estimate; "
            "add mixer.channel_width_mm and mixer.length_mm (or a path) to fix it",
        )
        return {"width_mm": lane, "length_mm": length,
                "source": "ASSUMED: folded at the fabrication floors (see meta.unresolved)"}

    # -- ports ------------------------------------------------------------------

    def mesh_ports(self) -> None:
        for pid in sorted(self.ports):
            port = self.ports[pid]
            bore = (float(port["width_um"]) / 1000.0) if port.get("width_um") else self.port_bore_mm
            nid = f"p:{pid}"
            self._add_node(Node(
                nid, "port", float(port["position"][0]), float(port["position"][1]),
                math.pi * (bore / 2.0) ** 2, 0.0, pid,
                note=f"{port.get('type', '')} port, {bore:g} mm bore"
                     f"{', role ' + port['role'] if port.get('role') else ''}",
            ))
            self.port_nodes[pid] = nid

    def connect_loose_ports(self) -> None:
        """A port no channel names still opens onto something: join it to that feature.

        Every repo chip wires its ports through channels, so this is the path for a spec
        where a port sits straight over a chamber. The bore edge is a duct of the bore
        width through the lid layer, which is a small resistance next to anything on chip -
        the point is connectivity, so no port is ever an orphan.
        """
        used = {c["from"] for c in self.channels} | {c["to"] for c in self.channels}
        for pid in sorted(set(self.ports) - used):
            port = self.ports[pid]
            p = (float(port["position"][0]), float(port["position"][1]))
            target = self._feature_under(p)
            if target is None:
                self._flag(pid, "port is not on a channel and nothing lies under its bore",
                           "left unconnected",
                           "the mesh check reports it as an orphan, which is a real design "
                           "problem, not a mesher artefact")
                continue
            bore = (float(port["width_um"]) / 1000.0) if port.get("width_um") else self.port_bore_mm
            depth = self.depth_mm(self.chambers[target])
            attach = self._attach(target, (p, p), bore, f"b:{pid}", purpose="bore")
            g = duct_g_geom_m3(max(depth, bore), bore, bore)
            self._add_edge(f"b:{pid}", self.port_nodes[pid], attach, "mouth", g,
                           max(depth, bore), bore, bore, pid,
                           note=f"port bore opening onto {target}")

    def _feature_under(self, p: tuple[float, float]) -> str | None:
        inside = [cid for cid in sorted(self.chambers)
                  if self.footprint(self.chambers[cid]).contains(*p)]
        if inside:
            return inside[0]
        if not self.chambers:
            return None
        nearest = min(sorted(self.chambers),
                      key=lambda cid: math.dist(p, (self.footprint(self.chambers[cid]).cx,
                                                    self.footprint(self.chambers[cid]).cy)))
        fp = self.footprint(self.chambers[nearest])
        return nearest if math.dist(p, (fp.cx, fp.cy)) <= max(fp.sx, fp.sy) else None

    # -- channels ---------------------------------------------------------------

    def mesh_channels(self) -> None:
        for channel in self.channels:
            self._mesh_channel(channel)

    def _terminal_segments(self, channel: dict) -> tuple[tuple, tuple, float, str]:
        """((near, next) at the `from` end, same at the `to` end, length, source).

        A `path` is followed, because a straight line between endpoints is wrong for a
        routed channel; without one the centreline is the straight line between the two
        features trimmed at their walls, which is what the CAD cuts.
        """
        if channel.get("path"):
            pts = [tuple(float(v) for v in p)[:2] for p in channel["path"]]
            return ((pts[0], pts[1]), (pts[-1], pts[-2]),
                    polyline_length_mm(pts), "spec path")
        ca, sa = self.box_of(channel["from"])
        cb, sb = self.box_of(channel["to"])
        a = clip_to_box(tuple(ca[:2]), tuple(cb[:2]), ca, sa)
        b = clip_to_box(tuple(cb[:2]), tuple(ca[:2]), cb, sb)
        return ((a, b), (b, a), math.dist(a, b), "spec (straight, trimmed at the walls)")

    def _mesh_channel(self, channel: dict) -> None:
        cid = channel["id"]
        width = float(channel["width_mm"])
        depth = float(channel["depth_mm"])
        seg_a, seg_b, length, source = self._terminal_segments(channel)

        if length < 1e-3:
            self._flag(cid, "channel has no length once trimmed to its two features "
                            f"({length:.4g} mm): they touch or overlap",
                       f"half its width, {width / 2:g} mm",
                       "its resistance is nominal; check the layout")
            length = max(width / 2.0, 1e-3)

        a = self._attach(channel["from"], seg_a, width, f"{cid}:from", purpose="channel")
        b = self._attach(channel["to"], seg_b, width, f"{cid}:to", purpose="channel")
        g = duct_g_geom_m3(length, width, depth)
        note = source
        block = str(channel.get("block", ""))
        if block == "centipede_channel":
            note += ("; centipede teeth are not in the spec, so the comb is one spine duct "
                     "with its mouth spread along the wall it covers")
            self._flag(cid, "centipede_channel teeth are not given as geometry",
                       "a single spine duct at the stated width, feeding the whole wall it "
                       "runs along",
                       "per-tooth flow spread (the spec's own note says 0.61-2.02x the "
                       "mean) is not resolved")
        if channel.get("open_top"):
            self._flag(cid, "channel is an open groove (open_top), not a closed duct",
                       "the closed rectangular-duct resistance",
                       "a free surface changes the resistance; the estimate is approximate")
        self._add_edge(f"d:{cid}", a, b, "duct", g, length, width, depth, cid, note=note)

    # -- attachment: ports, point features, duct ends and mouths ----------------

    def _attach(self, feature_id: str, seg: tuple, width_mm: float, tag: str,
                purpose: str = "channel") -> str:
        """The node a channel end joins, creating a mouth (and any weir crest) if needed."""
        if feature_id in self.port_nodes:
            return self.port_nodes[feature_id]
        model = self._model.get(feature_id)
        if model is None:
            raise MeshError(f"{self.chip_id}: nothing meshed for feature {feature_id!r}")
        if model["kind"] == "junction":
            return model["node"]
        if model["kind"] == "duct":
            end = self._duct_attach.get(feature_id, {}).get(tag)
            if end is not None:
                return end
            near = seg[0]                      # a port bore, which names no channel
            return min(model["ends"],
                       key=lambda nid: (math.dist(near, (self.node(nid).x_mm,
                                                         self.node(nid).y_mm)), nid))
        return self._mouth(feature_id, model, seg, width_mm, tag, purpose)

    def node(self, node_id: str) -> Node:
        try:
            return self._by_id[node_id]
        except KeyError:
            raise MeshError(f"{self.chip_id}: no node {node_id!r}") from None

    def _mouth(self, cid: str, model: dict, seg: tuple, width_mm: float, tag: str,
               purpose: str) -> str:
        """Open a channel into a rasterised chamber over the boundary cells it spans.

        The opening is NOT a point. Its extent is the terminal segment of the channel
        projected onto the wall it meets, widened to at least the channel width: a channel
        arriving head-on spans its own width, while a spine running along the wall feeds
        every cell it covers. A weir chamber gets the full wall instead, because the crest
        runs across the whole chamber.
        """
        chamber = self.chambers[cid]
        fp, depth = model["footprint"], model["depth"]
        retention = chamber.get("retention") or {}
        is_weir = retention.get("type") == "weir"
        if fp.shape != "box":
            return self._round_mouth(cid, model, seg, width_mm, tag, purpose, is_weir,
                                     retention)

        wall = min(walls_of(fp), key=lambda w: (w.distance(seg[0]), w.name))
        near, nxt = seg[0], seg[1]
        lo, hi = sorted((wall.along(near), wall.along(nxt)))
        lo, hi = max(lo, wall.lo), min(hi, wall.hi)
        if hi - lo < width_mm:
            mid = min(max(wall.along(near), wall.lo), wall.hi)
            lo, hi = mid - width_mm / 2, mid + width_mm / 2
            lo, hi = max(lo, wall.lo), min(hi, wall.hi)
        if is_weir:
            lo, hi = wall.lo, wall.hi

        px, py = wall.point((lo + hi) / 2)
        outer = f"j:{tag}" if purpose == "channel" else f"j:{tag}:mouth"
        self._add_node(Node(outer, "junction", px, py, 0.0, depth, cid,
                            note=f"opening on the {wall.name} wall of {cid}, "
                                 f"{hi - lo:.4g} mm of it"))
        inner = outer
        if is_weir:
            inner = f"w:{cid}:{tag}"
            crest_mm, gap_um = self._weir(cid, retention, depth)
            self._add_node(Node(inner, "junction", px, py, 0.0, depth, cid,
                                note=f"weir crest across the {wall.name} wall of {cid}"))
            g = slit_g_geom_m3(hi - lo, gap_um, crest_mm)
            self._add_edge(f"s:{cid}:{tag}", inner, outer, "slit", g, crest_mm, hi - lo,
                           gap_um / 1000.0, cid, gap_um=gap_um,
                           note=f"weir crest, gap {gap_um:g} um across {hi - lo:.4g} mm: "
                                f"flow goes as gap^3, sweep the z tolerance")
            self._note_weir(cid, wall.name, tag, f"s:{cid}:{tag}", gap_um, hi - lo,
                            crest_mm)

        dx, dy = model["dx"], model["dy"]
        step = dy if wall.axis == "x" else dx
        spacing = (dx if wall.axis == "x" else dy) / 2.0
        hits = 0
        for (ix, iy), (nid, cov) in sorted(model["cells"].items()):
            cell = self.node(nid)
            c_lo = (cell.y_mm - dy / 2) if wall.axis == "x" else (cell.x_mm - dx / 2)
            overlap = min(hi, c_lo + step) - max(lo, c_lo)
            if overlap <= 1e-9:
                continue
            if not self._on_wall(cell, wall, dx, dy):
                continue
            g = hs_g_geom_m3(depth, overlap * min(cov, 1.0), spacing)
            self._add_edge(f"m:{tag}>{nid}", inner, nid, "mouth", g, spacing, overlap,
                           depth, cid)
            hits += 1
        if not hits:
            self._flag(cid, f"opening {tag} found no boundary cell on the {wall.name} wall",
                       "the nearest boundary cells instead",
                       "the opening is in the right place to within one cell")
            self._spread_over_nearest(cid, model, (px, py), hi - lo, inner, tag)
        return outer

    def _round_mouth(self, cid: str, model: dict, seg: tuple, width_mm: float, tag: str,
                     purpose: str, is_weir: bool, retention: dict) -> str:
        """Mouth on a round (cylinder) chamber: the four box walls do not describe its
        outline, so the opening is spread over the boundary cells nearest where the
        channel arrives, until it has covered the opening's width - or the whole rim for a
        weir, whose crest runs all the way round. Approximate by one cell, and said so."""
        fp, depth = model["footprint"], model["depth"]
        near = seg[0]
        radius = fp.span_min / 2.0
        angle = math.atan2(near[1] - fp.cy, near[0] - fp.cx)
        px, py = fp.cx + radius * math.cos(angle), fp.cy + radius * math.sin(angle)
        span = 2 * math.pi * radius if is_weir else width_mm

        outer = f"j:{tag}" if purpose == "channel" else f"j:{tag}:mouth"
        self._add_node(Node(outer, "junction", px, py, 0.0, depth, cid,
                            note=f"opening on the rim of the round chamber {cid}, "
                                 f"{span:.4g} mm of it"))
        inner = outer
        if is_weir:
            inner = f"w:{cid}:{tag}"
            crest_mm, gap_um = self._weir(cid, retention, depth)
            self._add_node(Node(inner, "junction", px, py, 0.0, depth, cid,
                                note=f"weir crest around the rim of {cid}"))
            g = slit_g_geom_m3(span, gap_um, crest_mm)
            self._add_edge(f"s:{cid}:{tag}", inner, outer, "slit", g, crest_mm, span,
                           gap_um / 1000.0, cid, gap_um=gap_um,
                           note=f"weir crest, gap {gap_um:g} um around {span:.4g} mm of "
                                f"rim: flow goes as gap^3, sweep the z tolerance")
            self._note_weir(cid, "rim", tag, f"s:{cid}:{tag}", gap_um, span, crest_mm)
        self._spread_over_nearest(cid, model, (px, py), span, inner, tag)
        return outer

    def _spread_over_nearest(self, cid: str, model: dict, anchor: tuple[float, float],
                             span_mm: float, inner: str, tag: str) -> None:
        """Join `span_mm` of opening to the boundary cells closest to `anchor`."""
        dx, dy, depth = model["dx"], model["dy"], model["depth"]
        step, spacing = min(dx, dy), min(dx, dy) / 2.0
        cells = [(self.node(nid), cov) for nid, cov in sorted(model["cells"].values())]
        rim = [(n, cov) for n, cov in cells if n.boundary] or cells
        rim.sort(key=lambda t: (math.dist(anchor, (t[0].x_mm, t[0].y_mm)), t[0].id))
        left = max(span_mm, step)
        for node, cov in rim:
            if left <= 1e-9:
                break
            take = min(step * min(cov, 1.0), left)
            g = hs_g_geom_m3(depth, take, spacing)
            self._add_edge(f"m:{tag}>{node.id}", inner, node.id, "mouth", g, spacing,
                           take, depth, cid)
            left -= take

    def _note_weir(self, cid: str, wall: str, tag: str, slit_id: str, gap_um: float,
                   width_mm: float, crest_mm: float) -> None:
        """Publish where each crest ended up: `meta.weir_sides`, which particles.py reads
        for its escape check and the report quotes."""
        self.weir_sides.setdefault(cid, []).append({
            "wall": wall, "opening": tag, "slit_edge": slit_id, "gap_um": gap_um,
            "width_mm": round(width_mm, 6), "crest_thickness_um": round(crest_mm * 1000, 3),
        })

    def _on_wall(self, cell: Node, wall: Wall, dx: float, dy: float) -> bool:
        """Is this cell the one against `wall`? (Half a cell from the wall line.)"""
        if wall.axis == "x":
            return abs(cell.x_mm - wall.pos) <= dx / 2 + 1e-9
        return abs(cell.y_mm - wall.pos) <= dy / 2 + 1e-9

    def _weir(self, cid: str, retention: dict, depth_mm: float) -> tuple[float, float]:
        """(crest thickness in mm, gap in um) for a weir, from the spec then the contract.

        `opening_um` is required by the schema. `crest_thickness_um` is optional and no
        repo chip states it, so it falls back to the contract's weir geometry
        (blocks.json retention.weir_geometry.crest_thickness_um, which is simply the
        in-plane fabrication floor) - read through contracts.py, never written here.
        """
        gap_um = float(retention["opening_um"])
        crest_um = retention.get("crest_thickness_um")
        if crest_um is None:
            crest_um = contracts.block("retention.weir_geometry.crest_thickness_um")
            self._flag(cid, "weir does not state crest_thickness_um (its length along the "
                            "flow)",
                       f"the contract's {float(crest_um):g} um "
                       f"(blocks.json retention.weir_geometry.crest_thickness_um)",
                       "slit resistance scales with crest thickness, linearly")
        if gap_um >= depth_mm * 1000.0:
            self._flag(cid, f"weir gap {gap_um:g} um is not below the chamber depth "
                            f"({depth_mm * 1000:g} um), so there is no crest to flow over",
                       "the gap as stated", "the slit is as deep as the chamber")
        return float(crest_um) / 1000.0, gap_um

    def isolated_features(self) -> list[str]:
        """Chambers the spec joins to NOTHING: no channel names them, no port opens onto
        them. A rasterised one comes out as a connected island (unreachable); a point
        feature comes out as a single node with no edge at all, which looks like an orphan
        but is not a mesher fault - it is a design dead end, and `build_mesh` says so
        rather than refusing the chip.
        """
        outward: set[str] = set()
        for edge in self.edges:
            fa, fb = self.node(edge.a).feature_id, self.node(edge.b).feature_id
            if fa != fb:                      # an edge that leaves the feature
                outward.update((fa, fb))
        return sorted(cid for cid in self.chambers if cid not in outward)

    # -- what we could not mesh -------------------------------------------------

    def note_unmeshed(self) -> None:
        """Everything in the spec that this mesh deliberately does not represent."""
        for valve in self.chip.get("valves", []):
            self._flag(valve["id"], "on-chip valve: open/closed is a per-step state, not "
                                    "geometry",
                       "the channel it sits on is always open",
                       "scenario.py must close the channel for a step where the valve is "
                       "shut; the mesh cannot know")
        for feature in self.chip.get("features", []):
            self._flag(feature["id"], f"{feature.get('type')} is not a fluidic feature",
                       "not meshed", "none: it carries no flow")
        for stage in self.chip.get("stages", []):
            self._flag(stage["id"], f"{stage.get('type')} between {stage.get('from_port')} "
                                    f"and {stage.get('to_port')} is "
                                    f"{stage.get('location')}",
                       "not meshed", "off-chip tubing volume belongs to couple.py")
        enc = self.chip.get("encapsulation") or {}
        if enc.get("nozzle_um"):
            self._flag(enc.get("nozzle", "encapsulation"),
                       f"{enc['nozzle_um']:g} um droplet nozzle orifice is not resolved as "
                       f"geometry",
                       "the junction chamber it sits in",
                       "the orifice's own pressure drop is missing, and the audit measured "
                       "Re ~9 here, outside the Stokes model altogether")
        for cid in sorted(self.chambers):
            ret = (self.chambers[cid].get("retention") or {}).get("type")
            if ret in ("mesh", "pillars"):
                self._flag(cid, f"retention type {ret!r} is not a weir and has no slit model",
                           "an open opening",
                           "its retention resistance is missing; operator/flow_calc.py has "
                           "a clean-mesh model and the bed resistance is a fitted parameter")

    # -- build ------------------------------------------------------------------

    def check_spec(self) -> None:
        """Refuse a spec whose channels name features that are not there.

        Caught up front and by name: a channel with a `path` never looks its endpoints up
        for geometry, so without this the spec error surfaced much later as an obscure
        'nothing meshed for feature' from the attachment code.
        """
        known = set(self.chambers) | set(self.ports)
        missing = sorted({end for c in self.channels
                          for end in (c.get("from"), c.get("to"))
                          if end not in known})
        if missing:
            raise MeshError(
                f"{self.chip_id}: channel endpoint(s) {', '.join(repr(m) for m in missing)} "
                f"are neither a chamber nor a port in this spec, so there is nothing to "
                f"join: "
                + "; ".join(f"{c['id']} ({c['from']} -> {c['to']})" for c in self.channels
                            if c.get("from") in missing or c.get("to") in missing)
                + f". The spec has chambers {', '.join(sorted(self.chambers))} and ports "
                  f"{', '.join(sorted(self.ports)) or '(none)'}."
            )

    def build(self) -> Mesh:
        if not self.chambers:
            raise MeshError(f"{self.chip_id}: a chip spec with no chambers cannot be meshed")
        self.check_spec()
        self.mesh_ports()
        self.mesh_chambers()
        self.mesh_channels()
        self.connect_loose_ports()
        self.note_unmeshed()

        weirs = sorted(cid for cid in self.chambers
                       if (self.chambers[cid].get("retention") or {}).get("type") == "weir")
        if weirs:
            self._flag(", ".join(weirs),
                       "the spec does not say WHICH wall of a weir chamber carries the crest",
                       "every opening of a weir chamber drains over a crest",
                       "an opening that has no crest in the real part is modelled with "
                       "~80 Pa more drop at 100 uL/min than it has; the contract's figure "
                       "for one crest is 2 Pa, which the duct form does not reproduce - "
                       "analyse.py should report that disagreement")

        isolated = self.isolated_features()
        for cid in isolated:
            self._flag(cid, "nothing in the spec joins this feature to anything: no "
                            "channel names it and no port opens onto it",
                       "meshed on its own, with no edge leaving it",
                       "a dead_end finding for analyse.py; it is not a mesher fault, so "
                       "the mesh is still handed back")

        meta: dict[str, Any] = {
            "chip_id": self.chip_id,
            "isolated_features": isolated,
            "grid_mm": self.grid_mm,
            "mu_pa_s": self.mu,
            "mu_source": self.mu_source,
            "min_cells_across": self.min_cells_across,
            "model": "hybrid: Hele-Shaw chambers + 1D rectangular ducts",
            "conductance_note": (
                "every edge carries conductance_m3_per_pa_s at meta.mu_pa_s AND "
                "g_geom_m3 = conductance x mu (geometry only, m^3). Rescale for another "
                "fluid with Mesh.at_viscosity(mu); do not re-mesh."
            ),
            "scale": self.chip.get("scale", "demo"),
            "footprint_mm": [self.chip.get("footprint", {}).get("x"),
                             self.chip.get("footprint", {}).get("y")],
            "features": self.feature_notes,
            # feature id -> {kind: grid|duct|junction, ...}: the per-feature view, for the
            # modules that walk a chamber cell by cell (particles.py, figures.py). A grid
            # carries ids[ix][iy] (None where a round footprint does not reach) and a flat
            # `nodes` list, which is the safe one to iterate.
            "grids": self.grids,
            # chamber id -> every weir crest on it: which wall, gap, width, thickness.
            "weir_sides": self.weir_sides,
            "channels": {
                c["id"]: {"from": c["from"], "to": c["to"], "block": c.get("block"),
                          "path": c.get("path")}
                for c in self.channels
            },
            "unresolved": self.unresolved,
            "contracts": contracts.fingerprint(),
        }
        mesh = Mesh(nodes=self.nodes, edges=self.edges, ports=self.port_nodes, meta=meta)
        meta["stats"] = mesh.stats()
        return mesh


# --- routing: is every feature actually IN the flow path? -----------------------------


def routing(mesh: Mesh) -> dict[str, dict]:
    """Per duct-meshed feature: which neighbours hang off which end of its run.

    This is the view that makes a bypassed feature obvious WITHOUT a solve, and it is why
    it lives in the mesher: `assay2chip/foam/validate_foam.py routing_mismatch()` finds
    the same fault by comparing a reduced-order field against a 3D one, which is both
    expensive and far too late - by then every pressure, shear and residence time on the
    chip has already been reported wrong.
    """
    out: dict[str, dict] = {}
    for feature, grid in sorted(mesh.meta.get("grids", {}).items()):
        if grid.get("kind") != "duct":
            continue
        ends = list(grid.get("nodes") or [])
        sides = {}
        for node in ends:
            sides[node] = sorted({e.feature_id for e in mesh.edges_at(node)
                                  if e.feature_id != feature})
        neighbours = sorted(set().union(*sides.values()) if sides else set())
        out[feature] = {
            "ends": ends, "by_end": sides, "neighbours": neighbours,
            "in_series": all(sides.get(n) for n in ends) if len(ends) == 2 else False,
            "duct_length_mm": max((e.length_mm for e in mesh.edges_of(feature)
                                   if e.kind == "duct"), default=0.0),
        }
    return out


def routing_problems(mesh: Mesh) -> list[str]:
    """Features the mesh would let the flow BYPASS. Each line is a blocker, not a warning.

    A duct-meshed run with neighbours on only one of its two ends is a dead-end stub: the
    network solves happily and quietly routes everything past it. That is what happened to
    spc_demo_v1's delay line, so this is checked at mesh time and `build_mesh(strict=True)`
    refuses to hand back such a mesh.

    A feature with exactly ONE neighbour in the whole spec is a different thing - a design
    stub, reported as a dead end - so it is not a routing fault and is not flagged here.
    """
    problems = []
    for feature, info in routing(mesh).items():
        if len(info["neighbours"]) < 2 or info["in_series"]:
            continue
        crowded = max(info["by_end"].items(), key=lambda kv: (len(kv[1]), kv[0]))
        empty = sorted(n for n in info["ends"] if not info["by_end"].get(n))
        problems.append(
            f"{feature} is meshed as a {info['duct_length_mm']:.4g} mm duct but "
            f"{', '.join(info['neighbours'])} all attach to {crowded[0]} while "
            f"{', '.join(empty)} has nothing on it: the run is a DEAD-END STUB and the "
            f"flow bypasses it. A feature the spec puts in series must be meshed in series."
        )
    # Two channels must never share one opening into a rasterised chamber either: each
    # names the chamber at ITS OWN mouth, or they short-circuit through a single point
    # instead of through the chamber.
    openings: dict[str, set[str]] = {}
    for edge in mesh.edges_of_kind("mouth"):
        if not edge.id.startswith("m:"):
            continue
        tag, _, _cell = edge.id[2:].partition(">")
        for end in (edge.a, edge.b):
            if mesh.node(end).kind == "junction":
                openings.setdefault(end, set()).add(tag)
    for node, tags in sorted(openings.items()):
        if len(tags) > 1:
            problems.append(
                f"node {node} is the single opening for {', '.join(sorted(tags))}: two "
                f"channels sharing one mouth bypass the chamber between them"
            )
    return problems


# --- entry points ---------------------------------------------------------------------


def build_mesh(chip: dict, *, grid_mm: float | None = None, mu_pa_s: float | None = None,
               min_cells_across: int = MIN_CELLS_ACROSS, temp_c: float | None = None,
               strict: bool = True) -> Mesh:
    """Mesh one loaded chip spec. `strict` raises on a structurally broken mesh.

    `temp_c` picks the viscosity through `contracts.viscosity_for` (None - the default -
    means the real device at 37 C, not water). `mu_pa_s` overrides it outright, which is
    how a cold-solvent step is meshed; or mesh once and call `Mesh.at_viscosity`.
    """
    mesh = Mesher(chip, grid_mm=grid_mm, mu_pa_s=mu_pa_s,
                  min_cells_across=min_cells_across, temp_c=temp_c).build()
    # A feature the spec joins to nothing is a DESIGN dead end - reported, never a reason
    # to refuse the mesh. Anything else `check()` finds is structural and is a refusal.
    lonely = {f"node {n.id!r} has no edge (orphan)" for n in mesh.nodes
              if n.feature_id in set(mesh.meta.get("isolated_features", []))}
    problems = [p for p in mesh.check()
                if "not reachable from any port" not in p and p not in lonely]
    mesh.meta["routing"] = routing(mesh)
    problems += routing_problems(mesh)
    # The mesher's own verdict, so nobody has to re-derive it from problem strings:
    # empty means strict mode accepts this mesh.
    mesh.meta["structural_problems"] = list(problems)
    if problems and strict:
        raise MeshError(f"{mesh.chip_id}: the mesh is not sound:\n  - "
                        + "\n  - ".join(problems))
    mesh.meta["problems"] = mesh.check() + routing_problems(mesh)

    # A node no port can reach is a DESIGN dead end, not a broken mesh - graph
    # reachability is exactly how the README's `dead_end` class is found - so it is not a
    # strictness failure. It is still written down here, with the reason, because a
    # chamber nobody can reach is the kind of thing a mesh file must state out loud.
    dead = sorted({mesh.node(n).feature_id for n in mesh.unreachable()})
    mesh.meta["dead_end_features"] = dead
    for feature in dead:
        attached = [cid for cid, c in mesh.meta.get("channels", {}).items()
                    if feature in (c.get("from"), c.get("to"))]
        mesh.meta["unresolved"].append({
            "feature_id": feature,
            "question": ("no path from any port reaches this feature: "
                         + ("it has no channel on it in the spec" if not attached
                            else f"its channels ({', '.join(attached)}) lead nowhere with "
                                 f"a port on it")),
            "assumed": "meshed as specified, and left disconnected",
            "effect": "a dead_end finding for analyse.py; nothing can flow here, so no "
                      "pressure, velocity or shear is defined in it",
        })
    return mesh


#: `build` is `build_mesh`. Two of the other modules reach for this name, and a mesher
#: that answers to both costs nothing.
build = build_mesh


def mesh_chip(path: Path | str, **kwargs: Any) -> Mesh:
    """Load `chip/<id>.json` (schema-checked) and mesh it."""
    path = Path(path)
    chip = schema_io.load_json(path)
    errors = schema_io.schema_errors("chip_spec", chip)
    mesh = build_mesh(chip, **kwargs)
    if errors:
        mesh.meta["spec_schema_problems"] = [
            f"{schema_io.path_str(e)}: {schema_io.describe(e)}" for e in errors[:20]
        ]
    mesh.meta["spec_path"] = path.resolve().relative_to(ROOT).as_posix() \
        if path.is_absolute() else str(path)
    return mesh


def repo_chips() -> list[Path]:
    """Every hand-written chip spec in chip/, in a stable order."""
    return sorted((ROOT / "chip").glob("*.json"))


def mesh_all(paths: Iterable[Path] | None = None, **kwargs: Any) -> dict[str, Any]:
    """Mesh every chip and report, including the ones that failed and why."""
    out: dict[str, Any] = {"meshed": {}, "failed": {}, "unresolved": {}}
    for path in (paths if paths is not None else repo_chips()):
        try:
            mesh = mesh_chip(path, **kwargs)
        except Exception as exc:                       # a spec we cannot mesh is a result
            out["failed"][path.name] = f"{type(exc).__name__}: {exc}"
            continue
        out["meshed"][mesh.chip_id] = mesh.stats()
        out["unresolved"][mesh.chip_id] = mesh.meta.get("unresolved", [])
    return out


# --- CLI ------------------------------------------------------------------------------


def _print_mesh(mesh: Mesh) -> None:
    s = mesh.stats()
    print(f"\n== {s['chip_id']}  {s['nodes']} nodes, {s['edges']} edges, "
          f"{s['ports']} ports, {s['wetted_volume_ul']:g} uL wetted")
    print(f"   nodes {s['nodes_by_kind']}  edges {s['edges_by_kind']}")
    print(f"   grid {s['grid_mm']} mm, mu {s['mu_pa_s']} Pa s, "
          f"conductance spans {s['conductance_m3_per_pa_s']['span_decades']} decades")
    for f in mesh.meta.get("features", []):
        if f["model"] == "hele_shaw":
            print(f"   {f['feature_id']:14s} hele_shaw {f['cells']:5d} cells "
                  f"({f['nx']}x{f['ny']} at {f['grid_mm'][0]:.4f}x{f['grid_mm'][1]:.4f} mm), "
                  f"area {f['cell_area_mm2']:.4f} / {f['footprint_area_mm2']:.4f} mm2 "
                  f"= {f['area_ratio']:.4f}")
        elif f["model"] == "duct":
            print(f"   {f['feature_id']:14s} duct      lane {f['lane_width_mm']:g} mm x "
                  f"{f['path_length_mm']:g} mm ({f['lane_source']}); as a box it would be "
                  f"{f['bounding_box_would_be_width_mm']:g} mm wide "
                  f"({f['width_error_if_boxed']}x)")
        else:
            print(f"   {f['feature_id']:14s} junction  {f['reason']}")
    for u in mesh.meta.get("unresolved", []):
        print(f"   ! {u['feature_id']}: {u['question']}"
              + (f" -> assumed {u['assumed']}" if u.get("assumed") else ""))
    for p in mesh.meta.get("problems", []):
        print(f"   PROBLEM {p}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mesh a chip spec for the CFD stage.")
    ap.add_argument("chip", type=Path, nargs="*", help="chip/<id>.json (default: all of chip/)")
    ap.add_argument("--all", action="store_true", help="every chip/*.json")
    ap.add_argument("--grid-mm", type=float, default=None,
                    help=f"Hele-Shaw cell size (default {DEFAULT_GRID_MM})")
    ap.add_argument("--temp-c", type=float, default=None,
                    help="step temperature; picks the viscosity through contracts")
    ap.add_argument("--write", metavar="DIR", nargs="?",
                    const=str(ROOT / "assay2chip" / "work" / "cfd"),
                    help="write <chip_id>.mesh.json into DIR")
    ap.add_argument("--json", action="store_true", help="one JSON report instead of a table")
    args = ap.parse_args(argv)

    paths = args.chip or (repo_chips() if (args.all or not args.chip) else [])
    report: dict[str, Any] = {"meshed": {}, "failed": {}}
    rc = 0
    for path in paths:
        try:
            mesh = mesh_chip(path, grid_mm=args.grid_mm, temp_c=args.temp_c, strict=False)
        except Exception as exc:
            report["failed"][path.name] = f"{type(exc).__name__}: {exc}"
            if not args.json:
                print(f"\n== {path.name}: COULD NOT MESH: {type(exc).__name__}: {exc}")
            rc = 1
            continue
        if args.write:
            out = Path(args.write) / f"{mesh.chip_id}.mesh.json"
            mesh.save(out)
            if not args.json:
                print(f"\nwrote {out}")
        report["meshed"][mesh.chip_id] = {
            **mesh.stats(), "unresolved_detail": mesh.meta.get("unresolved", []),
            "problems": mesh.meta.get("problems", []),
        }
        if not args.json:
            _print_mesh(mesh)
    if args.json:
        print(json.dumps(report, indent=2))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

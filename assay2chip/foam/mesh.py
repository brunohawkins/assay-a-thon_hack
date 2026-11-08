"""chip/<id>.json -> an OpenFOAM structured hex `polyMesh`, written directly.

    python assay2chip/foam/mesh.py chip/module1_v1.json            # stats to stdout
    python assay2chip/foam/mesh.py --all --json                    # every repo chip
    python assay2chip/foam/mesh.py chip/module1_v1.json --out case # write the polyMesh

    from assay2chip.foam.mesh import mesh_chip
    result = mesh_chip("chip/module1_v1.json", case_dir)   # -> FoamMesh(meta, case_dir)

THE FLUID DOMAIN IS THE PLAN VIEW EXTRUDED TO THE CHIP'S SINGLE ETCH DEPTH, rasterised
onto a uniform xy grid and extruded on a GRADED z grid. Every cell is a hexahedron, so
`points`, `faces`, `owner`, `neighbour`, `boundary` and `cellZones` are written straight
out: no STL, no booleans, no snappyHexMesh failure modes (foam/README.md "Mesh").

NO FOURTH GEOMETRY IMPLEMENTATION. Everything about WHAT the plan view is comes from code
that already exists:

  * `assay2chip.cfd.mesh` is the geometry oracle. `build_mesh()` is run first and this
    module reads its answers: which 'chambers' are really channel runs
    (`is_channel_like`), the real lane width and path length of each of those
    (`meta.grids[id]`, which applies the spec's `mixer` fields and says so in
    `lane_source`), where each weir crest sits (`meta.weir_sides`), the etch depth per
    chamber (`depth_mm`), round vs box footprints (`Footprint`) and every assumption it
    had to make (`meta.unresolved`, copied into this mesh's metadata unchanged).
  * `clip_to_box` - the same function `cad/make_figures.py` imports from
    `cad/make_train_cad.py` - trims a channel at the chamber wall, so a meshed channel
    ends exactly where `make_figures.segments_of` draws it ending.
  * `tools/check_chip_spec.py` supplies the port-overlap thresholds, so the port report
    here is the repo's port rule and not a second opinion.

TWO THINGS ARE SYNTHESISED HERE, because no existing module produces them, and both are
named in `meta.synthesised` on every mesh that contains one:

  1. THE LANE POLYLINE of a channel-like block. cfd/mesh.py needs only a lane WIDTH and a
     LENGTH, which it derives; a 3D mesh needs the actual lane. The fold is deterministic:
     `turns + 1` lanes (or as many `min_open_channel_mm` lanes at `min_wall_mm` pitch as
     the short span holds when the spec gives no turns) run along the long axis, centred,
     evenly pitched, and the lane run is set so the total path length equals the length
     cfd/mesh.py derived. Where that length does not fit the bounding box the run is
     clamped and the shortfall is reported - never silently absorbed.
  2. HERRINGBONE FLOOR GROOVES ARE NOT REPRESENTED. They are a SECOND DEPTH and this chip
     is one fluidic layer at one etch depth (chip_spec.schema.json), so a herringbone
     mixer is meshed as a plain lane and listed in `meta.unrepresented`. The chaotic
     advection the grooves exist for is therefore absent from this mesh; a mixing number
     taken from it is a floor, not an estimate.

WEIRS. A weir is a standing ridge across the chamber at an opening that stops short of the
lid (chip_spec.schema.json `retention`), so its footprint is SOLID FROM THE FLOOR UP TO
`depth - opening_um` and the slit above it is fluid. The crest strip is the full wall width
by `crest_thickness_um` (the contract's in-plane floor when the spec omits it), placed just
inside every wall of the chamber that carries an opening - exactly the set cfd/mesh.py puts
a slit edge on, and flagged there for the same reason: the spec does not say which wall.
The z grid carries an interface at `depth - opening_um` EXACTLY and at least
`MIN_CELLS_GAP` cells above it. That slit carries every capsule wash in the project and its
+/-20 um z tolerance on a 25 um gap is +/-80% in flow, so `gap_um` / `gap_delta_um` override
it for a tolerance sweep instead of quoting one number.

PORTS. One patch per port, named by the port id, on the lid face (z = depth) inside the
bore circle (`width_um`, else the contract's `min_port_mm`). The bore is NOT fluid the
mesher may invent: a port whose bore opens onto too little fluid is REPORTED
(`meta.ports`, `enough_fluid: false`, against `tools/check_chip_spec.py`'s own threshold),
and a bore with no fluid under it at all gets NO patch - which is the repo's port-overlap
rule showing up in the mesh instead of being papered over. Everything else is `walls`.

At the port end a channel is rasterised to the PORT CENTRE rather than trimmed at the
0.8 mm port pad the DXF trims it at. That is the geometry
`tools/check_chip_spec.py::_spec_outlines` measures the port rule against, and it is what
the spec means by a channel whose `from` is a port; the DXF's own trim there is the known
port-overlap defect that `tests/test_port_overlap.py` records. Chamber ends are trimmed
exactly as `make_figures.segments_of` trims them.

CELLZONES are named by feature id - one zone per chamber and per channel, each cell in
exactly one zone - so the flow stage can report wall shear per chamber without re-deriving
which cells belong to what.

Deterministic: feature order, grid origin and every id come from the spec, so the same chip
in gives byte-identical polyMesh files out. numpy for the mesh itself; everything else is
the standard library plus `assay2chip`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence, Union

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "operator"), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import schema_io  # noqa: E402

from assay2chip.cfd import mesh as cfd_mesh  # noqa: E402  the geometry oracle
from assay2chip.cfd.mesh import Footprint, clip_to_box  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402

MM_TO_M = 1.0e-3

# --- discretisation parameters (mesh choices, NOT contract values) --------------------

#: Hard cell ceiling. A 3D mesh that will not fit in memory is not a better answer than a
#: coarse one, so xy spacing is relaxed to stay under this and the relaxation is reported.
DEFAULT_CELL_BUDGET = 2_000_000
#: Cells across the narrowest fluid dimension in plan (foam/README.md: "at least 6").
MIN_CELLS_ACROSS = 6
#: Cells across the etch depth (README: "at least 8 cells across the depth").
MIN_CELLS_Z = 8
#: Cells across a weir gap (README: "a 25 um weir gap gets at least 3 cells").
MIN_CELLS_GAP = 3
#: Largest ratio between neighbouring z cell heights. checkMesh is happy well beyond this;
#: 1.3 is the usual CFD comfort limit and keeps the graded column cheap.
MAX_Z_EXPANSION = 1.3

#: Blocks whose floor relief needs a second etch depth this single-layer chip has not got.
SECOND_DEPTH_BLOCKS = frozenset({"herringbone_mixer", "asymmetric_serpentine"})

#: The six face loops of a hex cell, as corner offsets (di, dj, dk) from the cell's own
#: (i, j, k) grid corner, wound so the normal points OUT of the cell in that direction.
#: Checked by tests/test_foam_mesh.py against the signed volume of every cell.
FACE_CORNERS: dict[str, tuple[tuple[int, int, int], ...]] = {
    "+x": ((1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)),
    "-x": ((0, 0, 0), (0, 0, 1), (0, 1, 1), (0, 1, 0)),
    "+y": ((0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)),
    "-y": ((0, 0, 0), (1, 0, 0), (1, 0, 1), (0, 0, 1)),
    "+z": ((0, 0, 1), (1, 0, 1), (1, 1, 1), (0, 1, 1)),
    "-z": ((0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0)),
}


class FoamMeshError(ValueError):
    """The spec cannot be turned into a hex mesh at all."""


def _port_overlap_rule() -> tuple[float, float, str]:
    """(area needed for a big bore, fraction needed for a small one, where it came from).

    Read out of tools/check_chip_spec.py so the mesh judges a port bore by the SAME rule
    the spec checker does. A second copy of these two numbers is exactly the kind of drift
    assay2chip/lib/contracts.py exists to prevent.
    """
    import check_chip_spec as ccs

    return (float(ccs.PORT_OVERLAP_MM2), float(ccs.PORT_OVERLAP_SMALL_FRACTION),
            "tools/check_chip_spec.py PORT_OVERLAP_MM2 / PORT_OVERLAP_SMALL_FRACTION")


# --- plan geometry: stamps -------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    """An axis-aligned box footprint."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def area_mm2(self) -> float:
        return (self.x1 - self.x0) * (self.y1 - self.y0)


@dataclass(frozen=True)
class Disc:
    """A round footprint (`shape: cylinder`), and the round end of nothing else."""

    cx: float
    cy: float
    r: float

    @property
    def area_mm2(self) -> float:
        return math.pi * self.r * self.r


@dataclass(frozen=True)
class Ring:
    """An annulus: the crest of a weir on a round chamber, which runs all the way round."""

    cx: float
    cy: float
    r_outer: float
    r_inner: float

    @property
    def area_mm2(self) -> float:
        return math.pi * (self.r_outer ** 2 - max(self.r_inner, 0.0) ** 2)


@dataclass(frozen=True)
class Seg:
    """One run of channel: a rectangle of width `w` along p0 -> p1, butt ends.

    Exactly `cad/make_figures.quad`'s rectangle. Interior joints of a polyline get a `Disc`
    so a corner does not leave a notch of solid in the middle of the lane.
    """

    x0: float
    y0: float
    x1: float
    y1: float
    w: float

    @property
    def length_mm(self) -> float:
        return math.dist((self.x0, self.y0), (self.x1, self.y1))

    @property
    def area_mm2(self) -> float:
        return self.length_mm * self.w


Stamp = Union[Rect, Disc, Ring, Seg]


def _stamp_bounds(stamps: Sequence[Stamp]) -> tuple[float, float, float, float]:
    xs: list[float] = []
    ys: list[float] = []
    for s in stamps:
        if isinstance(s, Rect):
            xs += [s.x0, s.x1]
            ys += [s.y0, s.y1]
        elif isinstance(s, Disc):
            xs += [s.cx - s.r, s.cx + s.r]
            ys += [s.cy - s.r, s.cy + s.r]
        elif isinstance(s, Ring):
            xs += [s.cx - s.r_outer, s.cx + s.r_outer]
            ys += [s.cy - s.r_outer, s.cy + s.r_outer]
        else:
            h = s.w / 2 + 1e-9
            xs += [min(s.x0, s.x1) - h, max(s.x0, s.x1) + h]
            ys += [min(s.y0, s.y1) - h, max(s.y0, s.y1) + h]
    if not xs:
        raise FoamMeshError("no plan geometry at all: nothing to mesh")
    return min(xs), min(ys), max(xs), max(ys)


def _rasterise(stamps: Sequence[Stamp], xc: np.ndarray, yc: np.ndarray,
               dxy: float) -> np.ndarray:
    """Cell-centre-in-footprint mask of `stamps` on the grid whose cell centres are
    `xc` x `yc`. Vectorised per stamp over its own bounding box only."""
    mask = np.zeros((xc.size, yc.size), dtype=bool)
    for s in stamps:
        if isinstance(s, Rect):
            lo_i, hi_i = np.searchsorted(xc, (s.x0, s.x1))
            lo_j, hi_j = np.searchsorted(yc, (s.y0, s.y1))
            mask[lo_i:hi_i, lo_j:hi_j] = True
            continue
        if isinstance(s, (Disc, Ring)):
            r_out = s.r if isinstance(s, Disc) else s.r_outer
            cx, cy = s.cx, s.cy
        else:
            r_out = s.w / 2 + s.length_mm / 2
            cx, cy = (s.x0 + s.x1) / 2, (s.y0 + s.y1) / 2
        lo_i, hi_i = np.searchsorted(xc, (cx - r_out - dxy, cx + r_out + dxy))
        lo_j, hi_j = np.searchsorted(yc, (cy - r_out - dxy, cy + r_out + dxy))
        if lo_i >= hi_i or lo_j >= hi_j:
            continue
        gx = xc[lo_i:hi_i][:, None]
        gy = yc[lo_j:hi_j][None, :]
        if isinstance(s, Disc):
            hit = (gx - s.cx) ** 2 + (gy - s.cy) ** 2 <= s.r ** 2
        elif isinstance(s, Ring):
            d2 = (gx - s.cx) ** 2 + (gy - s.cy) ** 2
            hit = (d2 <= s.r_outer ** 2) & (d2 >= max(s.r_inner, 0.0) ** 2)
        else:
            dx, dy = s.x1 - s.x0, s.y1 - s.y0
            L = math.hypot(dx, dy)
            if L < 1e-12:
                continue
            ux, uy = dx / L, dy / L
            px, py = gx - s.x0, gy - s.y0
            along = px * ux + py * uy
            perp = -px * uy + py * ux
            hit = (along >= 0.0) & (along <= L) & (np.abs(perp) <= s.w / 2)
        mask[lo_i:hi_i, lo_j:hi_j] |= hit
    return mask


# --- plan geometry: features ----------------------------------------------------------


@dataclass
class PlanFeature:
    """One spec feature as plan geometry, with everything the report needs about it."""

    id: str
    kind: str                       # chamber | lane | channel
    block: str
    stamps: list[Stamp]
    depth_mm: float
    spec_volume_ul: float | None
    nominal_area_mm2: float         # analytic plan area of what we meshed
    nominal_length_mm: float        # centreline length, 0 for a chamber
    nominal_width_mm: float         # lane/channel width, 0 for a chamber
    note: str = ""
    synthesised: dict | None = None
    unrepresented: list[dict] = field(default_factory=list)

    @property
    def nominal_volume_ul(self) -> float:
        return self.nominal_area_mm2 * self.depth_mm


def serpentine_lane(fp: Footprint, lane_w: float, target_len_mm: float,
                    turns: int | None, min_wall_mm: float) -> dict:
    """Fold `target_len_mm` of `lane_w` lane into `fp`, deterministically.

    `turns + 1` lanes when the spec gives turns, else as many lanes as the short span
    holds at `lane_w + min_wall_mm` pitch. Lanes run along the long axis, centred, evenly
    pitched; the run length is whatever makes the path the requested length, clamped to the
    bounding box. Returns the polyline and every number the clamp changed, for
    `meta.synthesised` - the whole point is that nothing here reads as measured.
    """
    x0, x1, y0, y1 = fp.bounds
    along_x = fp.sx >= fp.sy
    long_lo, long_hi = (x0, x1) if along_x else (y0, y1)
    short_lo, short_hi = (y0, y1) if along_x else (x0, x1)
    short = short_hi - short_lo
    long_span = long_hi - long_lo

    fit = max(1, int((short - lane_w) // (lane_w + min_wall_mm)) + 1)
    n = int(turns) + 1 if turns else fit
    n = max(1, min(n, max(1, int(short / max(lane_w, 1e-9)))))
    pitch = (short - lane_w) / (n - 1) if n > 1 else 0.0

    run_max = max(long_span - lane_w, lane_w)
    run_want = (target_len_mm - (n - 1) * pitch) / n if n else target_len_mm
    run = min(max(run_want, lane_w), run_max)
    achieved = n * run + (n - 1) * pitch

    a_lo = (long_lo + long_hi) / 2 - run / 2
    a_hi = a_lo + run
    pts: list[tuple[float, float]] = []
    for m in range(n):
        p = short_lo + lane_w / 2 + m * pitch
        ends = (a_lo, a_hi) if m % 2 == 0 else (a_hi, a_lo)
        for a in ends:
            pts.append((a, p) if along_x else (p, a))
    return {
        "points": pts, "lanes": n, "lane_width_mm": lane_w, "pitch_mm": pitch,
        "run_mm": run, "requested_length_mm": target_len_mm,
        "meshed_length_mm": achieved, "axis": "x" if along_x else "y",
        "turns_from_spec": int(turns) if turns else None,
        "lanes_from": "mixer.turns + 1" if turns else
                      f"{fit} lanes of {lane_w:g} mm at {min_wall_mm:g} mm wall pitch",
        "pitch_below_wall_floor": bool(n > 1 and pitch < lane_w + min_wall_mm - 1e-9),
        "length_clamped_to_box": bool(abs(achieved - target_len_mm) > 1e-6),
    }


def polyline_stamps(pts: Sequence[tuple[float, float]], w: float) -> list[Stamp]:
    """A centreline polyline as rectangles plus a disc at each interior joint."""
    out: list[Stamp] = []
    for a, b in zip(pts[:-1], pts[1:]):
        if math.dist(a, b) > 1e-12:
            out.append(Seg(a[0], a[1], b[0], b[1], w))
    for p in pts[1:-1]:
        out.append(Disc(p[0], p[1], w / 2))
    return out


def polyline_length(pts: Sequence[tuple[float, float]]) -> float:
    return sum(math.dist(a, b) for a, b in zip(pts[:-1], pts[1:]))


class Plan:
    """The plan view of one chip: ordered features, the etch depth and the weir crests.

    Every geometric decision is delegated to `assay2chip.cfd.mesh`; this class turns its
    answers into stamps.
    """

    def __init__(self, chip: dict):
        self.chip = chip
        self.chip_id = str(chip.get("id") or "unknown_chip")
        self.oracle = cfd_mesh.Mesher(chip)
        self.chambers: dict[str, dict] = self.oracle.chambers
        self.ports: dict[str, dict] = self.oracle.ports
        self.channels: list[dict] = self.oracle.channels
        self.port_bore_mm = float(contracts.floor("min_port_mm"))
        self.min_wall_mm = float(contracts.floor("min_wall_mm"))

        self.unresolved: list[dict] = []
        self.unrepresented: list[dict] = []
        self.synthesised: list[dict] = []
        #: channel-like chamber id -> the two ends of its synthesised lane. A channel
        #: attaching to one of these runs to the LANE END, not to the bounding box: the
        #: box is nominal and the lane is the fluid, and a channel stopping at the box
        #: wall would leave the lane a disconnected island (checkMesh counts regions).
        self.lane_ends: dict[str, list[tuple[float, float]]] = {}

        self.cfd_meta = self._run_oracle()
        self.depth_mm, self.depth_note = self._etch_depth()
        self.features = self._build_features()
        self.crests = self._build_crests()

    # -- the oracle -------------------------------------------------------------

    def _run_oracle(self) -> dict:
        """Run the reduced-order mesher for its geometry decisions and its honesty.

        Its `unresolved` list is this mesh's too: the 3D mesh inherits every assumption
        the lane widths and weir crests were built on.
        """
        try:
            reduced = cfd_mesh.build_mesh(self.chip, strict=False)
        except Exception as exc:                     # a spec cfd cannot mesh is a result
            self.unresolved.append({
                "feature_id": self.chip_id,
                "question": f"assay2chip.cfd.mesh could not mesh this spec "
                            f"({type(exc).__name__}: {exc})",
                "assumed": "its geometry decisions were re-derived here from the same "
                           "Mesher helpers, feature by feature",
                "effect": "the two models may not agree on this chip; fix the reduced "
                          "mesh first",
            })
            return {}
        self.unresolved += [dict(u) for u in reduced.meta.get("unresolved", [])]
        return reduced.meta

    def _grid(self, cid: str) -> dict:
        return (self.cfd_meta.get("grids") or {}).get(cid, {})

    # -- depth ------------------------------------------------------------------

    def _etch_depth(self) -> tuple[float, str]:
        """The chip's single etch depth, and what had to be flattened onto it."""
        depths = sorted({round(self.oracle.depth_mm(c), 9) for c in self.chambers.values()})
        if not depths:
            raise FoamMeshError(f"{self.chip_id}: no chambers, so no etch depth")
        depth = max(depths)
        note = f"{depth:g} mm"
        if len(depths) > 1:
            note += f" (spec has {len(depths)}: {depths})"
            self.unresolved.append({
                "feature_id": self.chip_id,
                "question": f"chambers give {len(depths)} different depths {depths}, but "
                            f"the chip is one fluidic layer at one etch depth",
                "assumed": f"the deepest, {depth:g} mm, for the whole extrusion",
                "effect": "a shallower feature is meshed deeper than it is, so its "
                          "resistance is low and its wall shear optimistic",
            })
        for ch in self.channels:
            if float(ch["depth_mm"]) < depth - 1e-9:
                self.unrepresented.append({
                    "feature_id": ch["id"],
                    "what": f"groove {float(ch['depth_mm']):g} mm deep in a "
                            f"{depth:g} mm extrusion",
                    "why": "the extrusion has one depth; a shallower channel is a second "
                           "etch this chip does not have",
                    "effect": "meshed at the full depth, so its resistance is "
                              f"{(depth / float(ch['depth_mm'])) ** 3:.1f}x too low",
                })
        return depth, note

    # -- features ---------------------------------------------------------------

    def _build_features(self) -> list[PlanFeature]:
        out: list[PlanFeature] = [self._chamber_feature(cid) for cid in sorted(self.chambers)]
        out += [f for f in (self._channel_feature(ch) for ch in self.channels) if f]
        return out

    def _chamber_feature(self, cid: str) -> PlanFeature:
        chamber = self.chambers[cid]
        fp = self.oracle.footprint(chamber)
        block = str(chamber.get("block") or chamber.get("type") or "")
        vol = float(chamber["volume_ul"]) if chamber.get("volume_ul") else None

        if self.oracle.is_channel_like(chamber):
            return self._lane_feature(cid, chamber, fp, block, vol)

        if fp.shape == "cylinder":
            r = fp.span_min / 2.0
            stamps: list[Stamp] = [Disc(fp.cx, fp.cy, r)]
            area = math.pi * r * r
        else:
            x0, x1, y0, y1 = fp.bounds
            stamps = [Rect(x0, y0, x1, y1)]
            area = fp.sx * fp.sy
        feature = PlanFeature(cid, "chamber", block, stamps, self.depth_mm, vol, area,
                              0.0, 0.0, note=f"{fp.shape} {fp.sx:g} x {fp.sy:g} mm")
        if chamber.get("filter") or chamber.get("mesh_pore_um"):
            kind = (chamber.get("filter") or {}).get("type", "mesh")
            feature.unrepresented.append({
                "feature_id": cid,
                "what": f"{kind} filter inside the chamber",
                "why": "the spec gives a gap, not pillar positions, so there is no "
                       "geometry to rasterise",
                "effect": "meshed as an open chamber: the filter's own resistance and its "
                          "trapping are missing",
            })
        return feature

    def _lane_feature(self, cid: str, chamber: dict, fp: Footprint, block: str,
                      vol: float | None) -> PlanFeature:
        """A channel-like block: the REAL LANE, folded, never the bounding box."""
        grid = self._grid(cid)
        mixer = chamber.get("mixer") or {}
        lane_w = float(grid.get("lane_width_mm")
                       or mixer.get("channel_width_mm")
                       or contracts.floor("min_open_channel_mm"))
        length = float(grid.get("path_length_mm") or 0.0)
        source = str(grid.get("lane_source") or "")
        if length <= 0.0:
            lane = self.oracle._lane_geometry(chamber, fp, self.depth_mm)
            lane_w, length, source = lane["width_mm"], lane["length_mm"], lane["source"]

        fold = serpentine_lane(fp, lane_w, length, mixer.get("turns"), self.min_wall_mm)
        fold["feature_id"] = cid
        fold["block"] = block
        fold["lane_width_and_length_from"] = source
        fold["how"] = ("lanes along the long axis of the bounding box, centred and evenly "
                       "pitched, run length set so the path is the length above; "
                       "DETERMINISTIC, and an assumption about the lane, not a measurement")
        self.synthesised.append(fold)

        pts = fold["points"]
        self.lane_ends[cid] = [pts[0], pts[-1]]
        feature = PlanFeature(
            cid, "lane", block, polyline_stamps(pts, lane_w), self.depth_mm, vol,
            fold["meshed_length_mm"] * lane_w, fold["meshed_length_mm"], lane_w,
            note=f"{fold['lanes']} lanes of {lane_w:g} mm, "
                 f"{fold['meshed_length_mm']:.4g} mm of path ({source})",
            synthesised=fold,
        )
        if fold["length_clamped_to_box"]:
            self.unresolved.append({
                "feature_id": cid,
                "question": f"{fold['requested_length_mm']:.4g} mm of "
                            f"{lane_w:g} mm lane does not fold into the "
                            f"{fp.sx:g} x {fp.sy:g} mm bounding box at "
                            f"{fold['lanes']} lanes",
                "assumed": f"{fold['meshed_length_mm']:.4g} mm, clamped to the box",
                "effect": f"resistance and residence time are "
                          f"{fold['meshed_length_mm'] / max(fold['requested_length_mm'], 1e-9):.2f}x "
                          f"the spec's; the spec's own geometry is inconsistent",
            })
        if fold["pitch_below_wall_floor"]:
            self.unresolved.append({
                "feature_id": cid,
                "question": f"{fold['lanes']} lanes in {min(fp.sx, fp.sy):g} mm leaves a "
                            f"{fold['pitch_mm'] - lane_w:g} mm wall, below the "
                            f"{self.min_wall_mm:g} mm floor",
                "assumed": "the fold as specified by mixer.turns",
                "effect": "the lane pattern as meshed is not fabricable; either turns or "
                          "the footprint is wrong in the spec",
            })
        if block in SECOND_DEPTH_BLOCKS:
            feature.unrepresented.append({
                "feature_id": cid,
                "what": f"{block} floor grooves",
                "why": "the grooves are a SECOND depth and this chip is one fluidic layer "
                       "at one etch depth (chip_spec.schema.json)",
                "effect": "meshed as a plain lane: the chaotic advection the grooves exist "
                          "for is absent, so any mixing number from this mesh is a lower "
                          "bound and not an estimate",
            })
        return feature

    def _channel_polyline(self, ch: dict) -> tuple[list[tuple[float, float]], str]:
        """A channel's centreline: its `path`, else straight and trimmed at the chamber.

        The chamber end is trimmed with `clip_to_box` - the same call, on the same boxes,
        that `make_figures.segments_of` uses, so the lane ends where the drawing ends. The
        PORT end keeps the port centre: see the module docstring.
        """
        if ch.get("path"):
            pts = [(float(p[0]), float(p[1])) for p in ch["path"]]
            return self._snap_to_lanes(ch, pts, "spec path")
        ends: list[tuple[float, float]] = []
        ids = (ch["from"], ch["to"])
        for me, other in (ids, ids[::-1]):
            c_me, s_me = self.oracle.box_of(me)
            c_other, _ = self.oracle.box_of(other)
            here = (float(c_me[0]), float(c_me[1]))
            there = (float(c_other[0]), float(c_other[1]))
            if me in self.ports:
                ends.append(here)
            else:
                ends.append(clip_to_box(here, there, c_me, s_me))
        return self._snap_to_lanes(
            ch, ends, "spec (straight, trimmed at the chamber walls as make_figures does)")

    def _snap_to_lanes(self, ch: dict, pts: list, source: str) -> tuple[list, str]:
        """Carry an end that meets a channel-like block through to its LANE end.

        A spec trims the channel at the block's BOUNDING BOX, but the fluid inside that
        box is a folded lane set in from the walls, so a channel stopping at the box wall
        meets nothing and the lane becomes a separate mesh region. The box is nominal; the
        lane is the fluid, so the channel runs the last fraction of a millimetre to the
        nearer end of the lane and the extension is named in the channel's note.
        """
        pts = list(pts)
        snapped: list[str] = []
        for idx, end_id in ((0, ch["from"]), (-1, ch["to"])):
            lane = self.lane_ends.get(end_id)
            if not lane:
                continue
            target = min(lane, key=lambda p: (math.dist(pts[idx], p), p))
            if math.dist(pts[idx], target) > 1e-9:
                snapped.append(f"{end_id} (+{math.dist(pts[idx], target):.4g} mm)")
            pts[idx] = target
        if snapped:
            source += ("; carried through to the lane end of " + ", ".join(snapped)
                       + " instead of stopping at the bounding box")
        return pts, source

    def _channel_feature(self, ch: dict) -> PlanFeature | None:
        cid = ch["id"]
        for end in (ch["from"], ch["to"]):
            if end not in self.chambers and end not in self.ports:
                self.unresolved.append({
                    "feature_id": cid,
                    "question": f"channel names {end!r}, which is neither a chamber nor a "
                                f"port in this spec",
                    "assumed": "the channel is not meshed",
                    "effect": "that path carries no flow in this mesh",
                })
                return None
        w = float(ch["width_mm"])
        pts, source = self._channel_polyline(ch)
        length = polyline_length(pts)
        if length < 1e-6:
            self.unresolved.append({
                "feature_id": cid,
                "question": f"channel has no length once trimmed to its two features "
                            f"({length:.4g} mm): they touch or overlap",
                "assumed": "not rasterised (a zero-length lane has no cells)",
                "effect": "the two features it joins are connected only where their "
                          "footprints overlap; check the layout",
            })
            return None
        feature = PlanFeature(cid, "channel", str(ch.get("block") or ""),
                              polyline_stamps(pts, w), self.depth_mm, None,
                              length * w, length, w,
                              note=f"{w:g} mm wide, {length:.4g} mm long ({source})")
        if ch.get("open_top"):
            feature.unrepresented.append({
                "feature_id": cid,
                "what": "open groove (open_top): a free surface at z = depth",
                "why": "this mesh is a closed single-phase domain; a free surface is "
                       "vof.py's job",
                "effect": "meshed as a closed duct under a lid, which over-predicts "
                          "resistance for an open groove",
            })
        return feature

    # -- weir crests ------------------------------------------------------------

    def _build_crests(self) -> list[dict]:
        """One crest strip per (weir chamber, wall that carries an opening).

        The walls come from `cfd.mesh` `meta.weir_sides`, which is where the reduced model
        put its slit edges; so the two models put the crest in the same places, including
        the same flag that the spec does not say WHICH wall carries it.
        """
        sides = self.cfd_meta.get("weir_sides") or {}
        out: list[dict] = []
        for cid in sorted(self.chambers):
            chamber = self.chambers[cid]
            retention = chamber.get("retention") or {}
            if retention.get("type") != "weir":
                continue
            fp = self.oracle.footprint(chamber)
            crest_mm, gap_um = self.oracle._weir(cid, retention, self.depth_mm)
            walls = sorted({str(s["wall"]) for s in sides.get(cid, [])})
            if not walls:
                walls = self._walls_with_openings(cid, fp)
            if not walls:
                walls = ["rim"] if fp.shape == "cylinder" else ["x_max"]
                self.unresolved.append({
                    "feature_id": cid,
                    "question": "weir chamber has no opening for a crest to sit at",
                    "assumed": f"a crest on {walls[0]}",
                    "effect": "the crest is in an arbitrary place; nothing flows over it "
                              "anyway because nothing is attached to this chamber",
                })
            for wall in walls:
                out.append({"chamber": cid, "wall": wall, "gap_um": float(gap_um),
                            "crest_thickness_mm": float(crest_mm),
                            "stamps": self._crest_stamps(fp, wall, crest_mm)})
        return out

    def _walls_with_openings(self, cid: str, fp: Footprint) -> list[str]:
        """Which walls of `cid` a channel arrives at, by `cfd.mesh.Wall.distance`.

        Only needed when the reduced mesher could not run and so published no
        `weir_sides`; the rule is the one `cfd.mesh.Mesher._mouth` uses.
        """
        if fp.shape == "cylinder":
            return ["rim"]
        walls = cfd_mesh.walls_of(fp)
        found: set[str] = set()
        for ch in self.channels:
            if cid not in (ch["from"], ch["to"]):
                continue
            pts, _ = self._channel_polyline(ch)
            near = pts[0] if ch["from"] == cid else pts[-1]
            found.add(min(walls, key=lambda w: (w.distance(near), w.name)).name)
        return sorted(found)

    def _crest_stamps(self, fp: Footprint, wall: str, t: float) -> list[Stamp]:
        """The crest footprint: the full wall width by `t`, just inside that wall."""
        x0, x1, y0, y1 = fp.bounds
        if fp.shape == "cylinder" or wall == "rim":
            r = fp.span_min / 2.0
            return [Ring(fp.cx, fp.cy, r, r - t)]
        if wall == "x_min":
            return [Rect(x0, y0, min(x0 + t, x1), y1)]
        if wall == "x_max":
            return [Rect(max(x1 - t, x0), y0, x1, y1)]
        if wall == "y_min":
            return [Rect(x0, y0, x1, min(y0 + t, y1))]
        return [Rect(x0, max(y1 - t, y0), x1, y1)]

    # -- sizing -----------------------------------------------------------------

    def narrowest_mm(self) -> tuple[float, str]:
        """The narrowest fluid dimension in plan, and what has it.

        Crest thickness is in the running: a crest thinner than a few cells is a slit
        resistance made of staircase, which is the number the whole retention scheme rests
        on.
        """
        best, who = math.inf, ""
        for f in self.features:
            if f.kind == "chamber":
                fp_span = min(
                    (s.x1 - s.x0) if isinstance(s, Rect) else
                    (2 * s.r if isinstance(s, Disc) else 2 * s.r_outer) for s in f.stamps)
                span_y = min(
                    (s.y1 - s.y0) if isinstance(s, Rect) else
                    (2 * s.r if isinstance(s, Disc) else 2 * s.r_outer) for s in f.stamps)
                span = min(fp_span, span_y)
            else:
                span = f.nominal_width_mm
            if 0.0 < span < best:
                best, who = span, f.id
        for crest in self.crests:
            if 0.0 < crest["crest_thickness_mm"] < best:
                best = crest["crest_thickness_mm"]
                who = f"{crest['chamber']} weir crest"
        if not math.isfinite(best):
            raise FoamMeshError(f"{self.chip_id}: no fluid feature has a width")
        return best, who

    def fluid_area_mm2(self) -> float:
        return sum(s.area_mm2 for f in self.features for s in f.stamps)

    def all_unrepresented(self) -> list[dict]:
        out = list(self.unrepresented)
        for f in self.features:
            out += f.unrepresented
        for valve in self.chip.get("valves", []):
            out.append({
                "feature_id": valve["id"],
                "what": f"{valve.get('type', 'valve')} on {valve.get('channel')}",
                "why": "open/closed is a per-step state, not geometry",
                "effect": "the channel is always open in this mesh; case.py must close it "
                          "for a step where the valve is shut",
            })
        enc = self.chip.get("encapsulation") or {}
        if enc.get("nozzle_um"):
            out.append({
                "feature_id": str(enc.get("nozzle") or "encapsulation"),
                "what": f"{float(enc['nozzle_um']):g} um droplet nozzle orifice",
                "why": "the spec gives an orifice width, not an orifice outline, and it is "
                       "below the in-plane floor the rest of the chip is drawn to",
                "effect": "the junction chamber is meshed open: the orifice pressure drop "
                          "and the droplet break-up are both absent (vof.py)",
            })
        for stage in self.chip.get("stages", []):
            out.append({
                "feature_id": stage["id"],
                "what": f"{stage.get('type')} between {stage.get('from_port')} and "
                        f"{stage.get('to_port')}, {stage.get('location')}",
                "why": "off-chip tubing is not part of the chip's fluid domain",
                "effect": "none on chip; its volume and delay belong to cfd/couple.py",
            })
        return out


# --- the z column ---------------------------------------------------------------------


def _graded_sizes(height: float, h_end: float, ratio: float, n_min: int) -> list[float]:
    """Cell heights filling `height`, fine (`h_end`) at the TOP, growing downward.

    Geometric at `ratio` until it covers the band, then scaled to fit exactly - scaling
    keeps the ratio and can only make the fine end finer, never coarser.
    """
    n = max(1, int(n_min))
    while True:
        sizes = [h_end * ratio ** i for i in range(n)]     # [0] is the top cell
        if sum(sizes) >= height - 1e-15 or n > 10_000:
            break
        n += 1
    scale = height / sum(sizes)
    return [s * scale for s in reversed(sizes)]            # bottom -> top


def z_levels(depth_mm: float, crest_tops: Iterable[float], *,
             min_cells_z: int = MIN_CELLS_Z, min_cells_gap: int = MIN_CELLS_GAP,
             ratio: float = MAX_Z_EXPANSION) -> np.ndarray:
    """z interface heights from 0 to `depth_mm`, GRADED, with an interface at every crest.

    Every crest top is an interface EXACTLY (so the gap is the gap, not a rounded gap) and
    every band between two interfaces gets at least `min_cells_gap` cells, which gives each
    weir gap at least that many. Bands below an interface are graded fine at the top, so
    the cells next to a slit are slit-sized and the ones near the floor are not.
    """
    depth = float(depth_mm)
    if depth <= 0.0:
        raise FoamMeshError(f"etch depth {depth!r} is not positive")
    tops = sorted({round(float(t), 12) for t in crest_tops if 1e-12 < float(t) < depth})
    anchors = [0.0] + tops + [depth]
    if not tops:
        n = max(2, int(min_cells_z))
        return np.linspace(0.0, depth, n + 1)

    bands = list(zip(anchors[:-1], anchors[1:]))
    h_fine = min((hi - lo) / max(1, int(min_cells_gap)) for lo, hi in bands)
    sizes: list[float] = []
    for idx, (lo, hi) in enumerate(bands):
        H = hi - lo
        if idx == len(bands) - 1:                   # the gap under the lid: uniform
            n = max(int(min_cells_gap), int(math.ceil(H / h_fine - 1e-9)))
            sizes += [H / n] * n
        else:
            sizes += _graded_sizes(H, h_fine, ratio, min_cells_gap)
    while len(sizes) < int(min_cells_z):            # README's floor on the whole column
        big = max(range(len(sizes)), key=lambda i: sizes[i])
        sizes[big:big + 1] = [sizes[big] / 2, sizes[big] / 2]
    levels = np.concatenate([[0.0], np.cumsum(np.asarray(sizes, dtype=float))])
    levels[-1] = depth
    return levels


# --- the mesh -------------------------------------------------------------------------


@dataclass
class FoamMesh:
    """What `write_polymesh` produced: where it went and everything about it."""

    case_dir: Path | None
    meta: dict

    @property
    def cells(self) -> int:
        return int(self.meta["cells"])

    def save_meta(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.meta, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
        return path


def _foam_header(cls: str, obj: str, note: str = "") -> str:
    lines = [
        "/*--------------------------------*- C++ -*----------------------------------*\\",
        "| =========                 |                                                 |",
        "| \\\\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox           |",
        "|  \\\\    /   O peration     | polyMesh written directly by                    |",
        "|   \\\\  /    A nd           | assay2chip/foam/mesh.py                         |",
        "|    \\\\/     M anipulation  |                                                 |",
        "\\*---------------------------------------------------------------------------*/",
        "FoamFile",
        "{",
        "    version     2.0;",
        "    format      ascii;",
        f"    class       {cls};",
    ]
    if note:
        lines.append(f"    note        \"{note}\";")
    lines += [
        "    location    \"constant/polyMesh\";",
        f"    object      {obj};",
        "}",
        "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //",
        "",
    ]
    return "\n".join(lines)


_CHUNK = 250_000


def _write_lines(fh, parts: Iterable[str]) -> None:
    buf: list[str] = []
    for p in parts:
        buf.append(p)
        if len(buf) >= _CHUNK:
            fh.write("\n".join(buf))
            fh.write("\n")
            buf = []
    if buf:
        fh.write("\n".join(buf))
        fh.write("\n")


def _write_label_list(path: Path, obj: str, arr: np.ndarray, note: str = "") -> None:
    with path.open("w", encoding="utf-8") as fh:
        fh.write(_foam_header("labelList", obj, note))
        fh.write(f"{arr.size}\n(\n")
        _write_lines(fh, map(str, arr.tolist()))
        fh.write(")\n\n"
                 "// ************************************************************* //\n")


def write_polymesh(chip: dict, case_dir: Path | str | None = None, *,
                   cell_budget: int = DEFAULT_CELL_BUDGET,
                   min_cells_across: int = MIN_CELLS_ACROSS,
                   min_cells_z: int = MIN_CELLS_Z,
                   min_cells_gap: int = MIN_CELLS_GAP,
                   z_expansion: float = MAX_Z_EXPANSION,
                   gap_um: float | None = None,
                   gap_delta_um: float = 0.0,
                   dxy_mm: float | None = None) -> FoamMesh:
    """Mesh one loaded chip spec; write `<case_dir>/constant/polyMesh` when given a dir.

    `gap_um` replaces every weir gap outright and `gap_delta_um` shifts it: the +/-20 um z
    tolerance on a 25 um crest is +/-80% in slit flow, so the gap is a SWEEP PARAMETER and
    not a number this mesher is entitled to fix. `cell_budget` is the ceiling; xy spacing
    is relaxed from `min_cells_across` to stay under it and says so in the metadata.
    """
    t0 = time.perf_counter()
    plan = Plan(chip)
    depth = plan.depth_mm

    # -- the weir gap, and the z column it forces -------------------------------
    crests = [dict(c) for c in plan.crests]
    for c in crests:
        base = float(gap_um) if gap_um is not None else c["gap_um"]
        c["gap_um_spec"] = c["gap_um"]
        c["gap_um"] = max(base + float(gap_delta_um), 0.0)
        c["crest_height_mm"] = max(depth - c["gap_um"] / 1000.0, 0.0)
        if c["gap_um"] <= 0.0 or c["crest_height_mm"] <= 0.0:
            raise FoamMeshError(
                f"{plan.chip_id}: weir gap {c['gap_um']:g} um in a {depth * 1000:g} um "
                f"layer leaves nothing to mesh (gap_um / gap_delta_um swept too far)")
    gap_override = {"gap_um": gap_um, "gap_delta_um": gap_delta_um,
                    "why": "the +/-20 um z tolerance on a 25 um crest is +/-80% in slit "
                           "flow; sweep it rather than quote one number"}
    levels = z_levels(depth, [c["crest_height_mm"] for c in crests],
                      min_cells_z=min_cells_z, min_cells_gap=min_cells_gap,
                      ratio=z_expansion)
    nz = levels.size - 1
    dz = np.diff(levels)

    # -- xy spacing: 6 cells across the narrowest lane, inside the cell budget ---
    narrow, narrow_who = plan.narrowest_mm()
    dxy_wanted = narrow / float(min_cells_across)
    dxy = float(dxy_mm) if dxy_mm else dxy_wanted
    area = plan.fluid_area_mm2()
    budget_note = ""
    if not dxy_mm and area / (dxy * dxy) * nz > cell_budget:
        dxy = math.sqrt(area * nz / float(cell_budget))
        budget_note = (f"xy spacing relaxed from {dxy_wanted:.6g} to {dxy:.6g} mm to stay "
                       f"inside the {cell_budget} cell budget: the narrowest lane "
                       f"({narrow:g} mm, {narrow_who}) now holds about "
                       f"{narrow / dxy:.1f} cells, not {min_cells_across}")

    # -- rasterise --------------------------------------------------------------
    all_stamps = [s for f in plan.features for s in f.stamps]
    bx0, by0, bx1, by1 = _stamp_bounds(all_stamps)
    i0 = int(math.floor(bx0 / dxy)) - 1
    j0 = int(math.floor(by0 / dxy)) - 1
    nx = int(math.ceil(bx1 / dxy)) + 2 - i0
    ny = int(math.ceil(by1 / dxy)) + 2 - j0
    xe = (i0 + np.arange(nx + 1)) * dxy                  # cell edges, mm
    ye = (j0 + np.arange(ny + 1)) * dxy
    xc = (xe[:-1] + xe[1:]) / 2
    yc = (ye[:-1] + ye[1:]) / 2

    # Features claim cells in a fixed order - chambers by id, then channels in spec
    # order - and the FIRST claim wins, so every cell is in exactly one cellZone and a
    # channel never steals the chamber it is trimmed to. What a feature's own footprint
    # covered but ceded to an earlier one is measured (`own_cells`) and reported, so it is
    # never mistaken for a staircase error.
    feat = np.full((nx, ny), -1, dtype=np.int32)
    names = [f.id for f in plan.features]
    own_cells = np.zeros(len(plan.features), dtype=np.int64)
    overlaps: list[dict] = []
    for idx, f in enumerate(plan.features):
        m = _rasterise(f.stamps, xc, yc, dxy)
        own_cells[idx] = int(m.sum())
        taken = m & (feat >= 0)
        if taken.any():
            overlaps.append({"feature_id": f.id, "cells_already_claimed": int(taken.sum()),
                             "of_its_own": int(own_cells[idx]),
                             "by": sorted({names[k] for k in np.unique(feat[taken])})})
        feat[m & (feat < 0)] = idx
    occ = feat >= 0
    if not occ.any():
        raise FoamMeshError(f"{plan.chip_id}: the plan view rasterised to no cells at all")

    # -- crest solidity: k0 is the first FLUID layer of each plan cell ----------
    k0 = np.zeros((nx, ny), dtype=np.int32)
    for c in crests:
        k = int(np.argmin(np.abs(levels - c["crest_height_mm"])))
        c["crest_top_level"] = k
        c["crest_top_mm"] = float(levels[k])
        c["gap_cells"] = int(nz - k)
        c["gap_mm_meshed"] = float(depth - levels[k])
        cm = _rasterise(c["stamps"], xc, yc, dxy) & (feat == names.index(c["chamber"]))
        c["crest_plan_cells"] = int(cm.sum())
        c["crest_cells_across"] = round(c["crest_thickness_mm"] / dxy, 2)
        k0 = np.maximum(k0, np.where(cm, k, 0)).astype(np.int32)
    blocked = occ & (k0 >= nz)
    if blocked.any():
        plan.unresolved.append({
            "feature_id": ", ".join(sorted({names[k] for k in np.unique(feat[blocked])})),
            "question": f"{int(blocked.sum())} plan cells are solid crest all the way to "
                        f"the lid, so they carry no fluid",
            "assumed": "those cells are left out of the mesh",
            "effect": "the crest seals there instead of leaving a slit; check the gap "
                      "against the etch depth",
        })
        occ &= ~blocked
        feat[blocked] = -1

    counts = np.where(occ, nz - k0, 0).astype(np.int64)
    n_cells = int(counts.sum())
    if n_cells == 0:
        raise FoamMeshError(f"{plan.chip_id}: every plan cell is solid crest")

    # -- cell table, ordered (i, j, k) ------------------------------------------
    flat_counts = counts.ravel()
    cell_start = (np.cumsum(flat_counts) - flat_counts).reshape(nx, ny)
    ii, jj = np.nonzero(occ)
    cnt = counts[ii, jj]
    cell_i = np.repeat(ii, cnt).astype(np.int32)
    cell_j = np.repeat(jj, cnt).astype(np.int32)
    base = np.repeat(cell_start[ii, jj], cnt)
    cell_k = (np.repeat(k0[ii, jj], cnt)
              + (np.arange(n_cells, dtype=np.int64) - base)).astype(np.int32)
    cell_zone = feat[cell_i, cell_j]

    # -- points -----------------------------------------------------------------
    used = np.zeros((nx + 1, ny + 1, nz + 1), dtype=bool)
    for di in (0, 1):
        for dj in (0, 1):
            for dk in (0, 1):
                used[cell_i + di, cell_j + dj, cell_k + dk] = True
    pid = np.full(used.shape, -1, dtype=np.int32)
    n_points = int(used.sum())
    pid[used] = np.arange(n_points, dtype=np.int32)
    pi, pj, pk = np.nonzero(used)
    px = xe[pi] * MM_TO_M
    py = ye[pj] * MM_TO_M
    pz = levels[pk] * MM_TO_M

    # -- faces ------------------------------------------------------------------
    def neighbour_cell(di: int, dj: int) -> np.ndarray:
        """Cell id of the (di, dj) plan neighbour at the same k, or -1 if it has none."""
        ni, nj = cell_i + di, cell_j + dj
        inside = (ni >= 0) & (ni < nx) & (nj >= 0) & (nj < ny)
        ci = np.clip(ni, 0, nx - 1)
        cj = np.clip(nj, 0, ny - 1)
        ok = inside & occ[ci, cj] & (cell_k >= k0[ci, cj])
        out = np.full(n_cells, -1, dtype=np.int64)
        out[ok] = (cell_start[ci[ok], cj[ok]] + (cell_k[ok] - k0[ci[ok], cj[ok]]))
        return out

    nbr_px, nbr_py = neighbour_cell(1, 0), neighbour_cell(0, 1)
    nbr_mx, nbr_my = neighbour_cell(-1, 0), neighbour_cell(0, -1)
    up = cell_k < (nz - 1)
    cells = np.arange(n_cells, dtype=np.int64)

    internal: list[tuple[np.ndarray, np.ndarray, str]] = [
        (cells[nbr_px >= 0], nbr_px[nbr_px >= 0], "+x"),
        (cells[nbr_py >= 0], nbr_py[nbr_py >= 0], "+y"),
        (cells[up], cells[up] + 1, "+z"),
    ]
    own = np.concatenate([o for o, _, _ in internal])
    nei = np.concatenate([n for _, n, _ in internal])
    dirs = np.concatenate([np.full(o.size, di, dtype=np.int8)
                           for di, (o, _, _) in enumerate(internal)])
    order = np.lexsort((nei, own))
    own, nei, dirs = own[order], nei[order], dirs[order]
    n_internal = own.size

    bnd_dirs = {
        "+x": nbr_px < 0, "-x": nbr_mx < 0, "+y": nbr_py < 0, "-y": nbr_my < 0,
        "+z": cell_k == (nz - 1), "-z": cell_k == k0[cell_i, cell_j],
    }

    # -- port patches on the lid ------------------------------------------------
    need_big, need_frac, need_src = _port_overlap_rule()
    lid = np.nonzero(bnd_dirs["+z"])[0]
    lid_x, lid_y = xc[cell_i[lid]], yc[cell_j[lid]]
    claimed = np.zeros(lid.size, dtype=bool)
    port_patches: list[tuple[str, np.ndarray]] = []
    port_report: list[dict] = []
    cell_area = dxy * dxy
    for pidx in sorted(plan.ports):
        port = plan.ports[pidx]
        if port.get("type") == "control":
            port_report.append({
                "port": pidx, "type": "control", "patch": None, "enough_fluid": None,
                "problem": "a control port drives a valve membrane, not fluid: no patch "
                           "(tools/check_chip_spec.py skips control ports too)",
            })
            continue
        bore = (float(port["width_um"]) / 1000.0) if port.get("width_um") else plan.port_bore_mm
        r = bore / 2.0
        hit = (((lid_x - float(port["position"][0])) ** 2
                + (lid_y - float(port["position"][1])) ** 2) <= r * r) & ~claimed
        bore_area = math.pi * r * r
        patch_area = float(hit.sum()) * cell_area
        need = need_big if bore * bore >= 4.0 else need_frac * bore * bore
        row = {
            "port": pidx, "type": port.get("type"), "role": port.get("role"),
            "bore_mm": bore, "bore_area_mm2": round(bore_area, 6),
            "patch_faces": int(hit.sum()), "patch_area_mm2": round(patch_area, 6),
            "need_mm2": round(need, 6), "need_from": need_src,
            "enough_fluid": bool(patch_area + cell_area >= need),
            "patch": pidx if hit.any() else None,
        }
        if hit.any():
            claimed |= hit
            port_patches.append((pidx, lid[hit]))
            if not row["enough_fluid"]:
                row["problem"] = (
                    f"the {bore:g} mm bore of {pidx!r} opens onto {patch_area:.3f} mm2 of "
                    f"fluid, under the {need:.3f} mm2 the port rule needs: the patch is "
                    f"real but the drilled hole would barely break through")
        else:
            row["problem"] = (
                f"the {bore:g} mm bore of {pidx!r} has NO fluid under it, so it gets no "
                f"patch: nothing can be driven or vented through this port. This is the "
                f"repo's port-overlap rule (tools/check_chip_spec.py check_port_overlap), "
                f"not a mesher artefact - run the channel across the hole or add a port "
                f"pad under it")
        port_report.append(row)

    # Boundary groups, in file order: every port patch, then `walls` as the six
    # directions of everything left over. Each group is (patch name, patch type, cell
    # ids, which face of those cells).
    groups: list[tuple[str, str, np.ndarray, str]] = [
        (name, "patch", faces, "+z") for name, faces in port_patches
    ]
    groups.append(("walls", "wall", lid[~claimed], "+z"))
    for d in ("-z", "+x", "-x", "+y", "-y"):
        groups.append(("walls", "wall", np.nonzero(bnd_dirs[d])[0], d))
    groups = [g for g in groups if g[2].size or g[0] != "walls"]

    # -- internal face node loops, in the stored (owner, neighbour) order -------
    dir_names = ("+x", "+y", "+z")
    nodes = np.empty((n_internal, 4), dtype=np.int32)
    for di, d in enumerate(dir_names):
        sel = np.nonzero(dirs == di)[0]
        if sel.size:
            c = own[sel]
            nodes[sel] = _face_nodes(pid, cell_i[c], cell_j[c], cell_k[c], d)

    n_boundary = sum(int(g[2].size) for g in groups)
    note = (f"nPoints:{n_points} nCells:{n_cells} nFaces:{n_internal + n_boundary} "
            f"nInternalFaces:{n_internal}")

    written: list[str] = []
    case = Path(case_dir) if case_dir is not None else None
    if case is not None:
        pm = case / "constant" / "polyMesh"
        pm.mkdir(parents=True, exist_ok=True)

        with (pm / "points").open("w", encoding="utf-8") as fh:
            fh.write(_foam_header("vectorField", "points", note))
            fh.write(f"{n_points}\n(\n")
            _write_lines(fh, (f"({x:.10g} {y:.10g} {z:.10g})"
                              for x, y, z in zip(px.tolist(), py.tolist(), pz.tolist())))
            fh.write(")\n\n// ********************************************** //\n")

        with (pm / "faces").open("w", encoding="utf-8") as fh:
            fh.write(_foam_header("faceList", "faces", note))
            fh.write(f"{n_internal + n_boundary}\n(\n")
            _write_face_block(fh, nodes)
            for _name, _kind, faces, d in groups:
                if faces.size:
                    _write_face_block(fh, _face_nodes(pid, cell_i[faces], cell_j[faces],
                                                      cell_k[faces], d))
            fh.write(")\n\n// ********************************************** //\n")

        owner = np.concatenate([own.astype(np.int64)]
                               + [g[2].astype(np.int64) for g in groups if g[2].size])
        _write_label_list(pm / "owner", "owner", owner, note)
        _write_label_list(pm / "neighbour", "neighbour", nei.astype(np.int64), note)
        _write_boundary(pm / "boundary", groups, n_internal, note)
        _write_cellzones(pm / "cellZones", names, cell_zone, note)
        written = ["points", "faces", "owner", "neighbour", "boundary", "cellZones"]

    # -- metadata ---------------------------------------------------------------
    zone_cells = np.bincount(cell_zone, minlength=len(names))
    # Fluid height of each plan column: the whole depth, less the solid crest under it.
    fluid_h = np.zeros((nx, ny))
    fluid_h[occ] = (depth - levels)[k0[occ]]
    feature_rows: list[dict] = []
    total_meshed = 0.0
    total_crest = 0.0
    total_ceded = 0.0
    for idx, f in enumerate(plan.features):
        mine = feat == idx
        plan_cells = int(mine.sum())
        vol = float(fluid_h[mine].sum()) * cell_area
        crest_solid = float((depth - fluid_h[mine]).sum()) * cell_area
        # Its own rasterised footprint, before any of it was ceded to a neighbour.
        raster_vol = float(own_cells[idx]) * cell_area * depth
        ceded = max(raster_vol - vol - crest_solid, 0.0)
        total_meshed += vol
        total_crest += crest_solid
        total_ceded += ceded
        row = {
            "feature_id": f.id, "kind": f.kind, "block": f.block,
            "cell_zone": f.id, "cells": int(zone_cells[idx]),
            "plan_cells": plan_cells,
            "meshed_volume_ul": round(vol, 6),
            "weir_crest_solid_ul": round(crest_solid, 6),
            "ceded_to_neighbour_ul": round(ceded, 6),
            "rasterised_footprint_ul": round(raster_vol, 6),
            "nominal_volume_ul": round(f.nominal_volume_ul, 6),
            "spec_volume_ul": f.spec_volume_ul,
            # THE STAIRCASE ERROR ALONE: the whole rasterised footprint against the
            # analytic one. The weir crest's solid and anything ceded to a neighbouring
            # footprint are design, not discretisation, so both are added back first and
            # reported in their own right.
            "staircase_error_vs_nominal": (
                round(raster_vol / f.nominal_volume_ul - 1.0, 6)
                if f.nominal_volume_ul else None),
            "error_vs_spec_volume_ul": (
                round(vol / f.spec_volume_ul - 1.0, 6) if f.spec_volume_ul else None),
            "note": f.note,
        }
        # MEASURED off the raster, not nominal: plan cells per cell of centreline. Taken
        # over the feature's OWN footprint, so a lane that overlaps a neighbour still
        # reports the resolution it was meshed at rather than the share it kept.
        row["cells_across_lane"] = (
            round(float(own_cells[idx]) / (f.nominal_length_mm / dxy), 2)
            if f.kind in ("channel", "lane") and f.nominal_length_mm > 0 else None)
        if f.synthesised:
            row["synthesised"] = True
        feature_rows.append(row)

    # The spec comparison is only over the features that HAVE a volume_ul (chambers do,
    # channels do not), so it is like for like instead of chip total vs chamber total.
    with_spec = [r for r in feature_rows if r["spec_volume_ul"]]
    spec_total = sum(r["spec_volume_ul"] for r in with_spec)
    spec_meshed = sum(r["meshed_volume_ul"] for r in with_spec)
    nominal_total = sum(r["nominal_volume_ul"] for r in feature_rows)
    across = [r["cells_across_lane"] for r in feature_rows if r["cells_across_lane"]]

    # -- connectivity: a fluid island is a real design fault, so it is reported ---
    labels = _plan_regions(occ)
    # Which region each port patch actually opens onto, taken from its own faces.
    port_regions: dict[str, set] = {
        name: set(labels[cell_i[faces], cell_j[faces]].tolist())
        for name, faces in port_patches
    }
    roots, sizes = np.unique(labels[occ], return_counts=True)
    regions = []
    for root, size in sorted(zip(roots.tolist(), sizes.tolist()),
                             key=lambda t: (-t[1], t[0])):
        in_region = occ & (labels == root)
        regions.append({
            "plan_cells": int(size),
            "volume_ul": round(float(fluid_h[in_region].sum()) * cell_area, 6),
            "features": sorted({names[k] for k in np.unique(feat[in_region])}),
            "ports": sorted(name for name, rs in port_regions.items() if root in rs),
        })
    # A channel whose footprint does not actually touch a feature it names is the precise
    # statement of what is wrong, and it is a SPEC fault: the reduced model hides it by
    # spreading a mouth over the wall the spine runs along, a 3D mesh cannot.
    feature_regions = {f.id: set(labels[feat == i][labels[feat == i] >= 0].tolist())
                       for i, f in enumerate(plan.features)}
    not_touching: list[dict] = []
    for ch in plan.channels:
        mine = feature_regions.get(ch["id"])
        if mine is None:
            continue
        for end in (ch["from"], ch["to"]):
            theirs = feature_regions.get(end)
            if theirs and not (mine & theirs):
                not_touching.append({"channel": ch["id"], "names": end,
                                     "block": ch.get("block")})
    if not_touching:
        plan.unresolved.append({
            "feature_id": ", ".join(sorted({r["channel"] for r in not_touching})),
            "question": "this channel's drawn footprint never reaches the feature it "
                        "names: " + "; ".join(f"{r['channel']} -> {r['names']}"
                                              for r in not_touching),
            "assumed": "meshed exactly where the spec draws it, so it is not connected",
            "effect": "no flow can pass between them in 3D. A centipede or comb block "
                      "whose TEETH are not in the spec looks like this: cfd/mesh.py "
                      "spreads a mouth along the wall the spine covers, which a hex mesh "
                      "cannot do without inventing the teeth. Add the teeth (or a path "
                      "that meets the chamber) to the spec",
        })
    if len(regions) > 1:
        plan.unresolved.append({
            "feature_id": ", ".join(sorted(
                f for r in regions[1:] for f in r["features"])),
            "question": f"the fluid domain is in {len(regions)} disconnected pieces; "
                        f"checkMesh reports the same count as regions",
            "assumed": "meshed as specified, in pieces",
            "effect": "a piece with no port in it cannot be driven or vented at all, and "
                      "the solver will leave its pressure undefined: this is the "
                      "dead_end class of assay2chip/cfd, in 3D",
        })

    meta: dict[str, Any] = {
        "chip_id": plan.chip_id,
        "model": "plan view extruded to one etch depth; structured hex polyMesh written "
                 "directly (no STL, no snappyHexMesh)",
        "scale": chip.get("scale", "demo"),
        "case_dir": str(case) if case else None,
        "files_written": written,
        "cells": n_cells,
        "points": n_points,
        "faces": n_internal + n_boundary,
        "internal_faces": n_internal,
        "boundary_faces": n_boundary,
        "build_seconds": round(time.perf_counter() - t0, 3),
        "geometry_source": {
            "plan": "assay2chip/cfd/mesh.py (Mesher: footprints, is_channel_like, lane "
                    "width and length, weir_sides, depth_mm)",
            "channel_trim": "cad/make_train_cad.clip_to_box via assay2chip.cfd.mesh, the "
                            "same call cad/make_figures.segments_of uses",
            "port_rule": need_src,
            "contracts": contracts.fingerprint(),
        },
        "xy": {
            "dxy_mm": dxy, "nx": nx, "ny": ny,
            "origin_mm": [float(xe[0]), float(ye[0])],
            "narrowest_feature_mm": narrow, "narrowest_feature": narrow_who,
            "cells_across_narrowest": round(narrow / dxy, 2),
            "min_cells_across_target": min_cells_across,
            "min_cells_across_measured": min(across) if across else None,
            "cell_budget": cell_budget,
            "budget_note": budget_note or "the target spacing fitted the cell budget",
            "fluid_plan_area_mm2": round(float(occ.sum()) * cell_area, 6),
        },
        "z": {
            "depth_mm": depth, "depth_note": plan.depth_note, "cells": nz,
            "levels_mm": [round(float(v), 9) for v in levels.tolist()],
            "dz_mm": [round(float(v), 9) for v in dz.tolist()],
            "dz_min_mm": float(dz.min()), "dz_max_mm": float(dz.max()),
            "max_expansion": round(float(np.max(dz[1:] / dz[:-1])) if nz > 1 else 1.0, 4),
            "min_cells_z_target": min_cells_z,
            "graded": bool(len(crests) > 0),
            "why_graded": "an interface sits exactly at every weir crest top and the gap "
                          "above it carries at least "
                          f"{min_cells_gap} cells; without a weir the column is uniform",
        },
        "weirs": [{k: v for k, v in c.items() if k != "stamps"} for c in crests],
        "weir_gap_override": gap_override,
        "ports": port_report,
        "ports_without_fluid": [r["port"] for r in port_report if r["patch"] is None],
        "ports_under_the_rule": [r["port"] for r in port_report
                                 if r["patch"] and not r["enough_fluid"]],
        "patches": [{"name": n, "type": "patch", "faces": int(f.size),
                     "area_mm2": round(float(f.size) * cell_area, 6)}
                    for n, f in port_patches]
                   + [{"name": "walls", "type": "wall",
                       "faces": sum(int(g[2].size) for g in groups if g[0] == "walls"),
                       "note": "every face that is not a port bore on the lid: the floor, "
                               "the lid, the side walls and the top of every weir crest"}],
        "cell_zones": {f.id: int(zone_cells[i]) for i, f in enumerate(plan.features)},
        "connectivity": {
            "regions": len(regions),
            "detail": regions,
            "channels_not_touching_what_they_name": not_touching,
            "note": "connected pieces of the fluid domain, biggest first, each with the "
                    "features in it and the port patches that open onto it. More than one "
                    "means the chip's own geometry does not join up - checkMesh reports "
                    "the same number as regions",
        },
        "features": feature_rows,
        "volume": {
            "meshed_total_ul": round(total_meshed, 6),
            "weir_crest_solid_ul": round(total_crest, 6),
            "ceded_between_footprints_ul": round(total_ceded, 6),
            "nominal_total_ul": round(nominal_total, 6),
            "spec_total_ul": round(spec_total, 6) if spec_total else None,
            "meshed_total_of_features_with_spec_volume_ul": round(spec_meshed, 6),
            "staircase_error_vs_nominal": (
                round((total_meshed + total_crest + total_ceded) / nominal_total - 1.0, 6)
                if nominal_total else None),
            "error_vs_spec_total": (round(spec_meshed / spec_total - 1.0, 6)
                                    if spec_total else None),
            "note": "1 mm^3 = 1 uL. nominal is the analytic volume of the plan geometry "
                    "this mesh represents, extruded, BEFORE a weir crest is subtracted "
                    "and before overlapping footprints are resolved; staircase_error adds "
                    "both back, so it is discretisation alone. spec is the feature's own "
                    "volume_ul, compared only over the features that have one, and for a "
                    "channel-like block it is often the BOUNDING BOX and so is not the "
                    "lane at all",
        },
        "synthesised": plan.synthesised,
        "unrepresented": plan.all_unrepresented(),
        "footprint_overlaps": overlaps,
        "unresolved": plan.unresolved,
    }
    return FoamMesh(case, meta)


def _plan_regions(occ: np.ndarray) -> np.ndarray:
    """Connected-component label per plan cell (4-connectivity), -1 where there is none.

    Why in plan and not in 3D: the only thing that breaks a column is a weir crest, and a
    crest leaves a slit above it, so two plan cells that touch are connected. A crest that
    seals to the lid is removed from `occ` before this runs, so it does split the labels -
    which is right, because it really does split the fluid.

    Union-find over the adjacency pairs, which numpy builds in two slices. numpy only, no
    scipy: requirements.txt stays lightweight (foam/README.md).
    """
    idx = np.full(occ.shape, -1, dtype=np.int64)
    n = int(occ.sum())
    idx[occ] = np.arange(n)
    pairs = []
    for sl_a, sl_b in (((slice(None, -1), slice(None)), (slice(1, None), slice(None))),
                       ((slice(None), slice(None, -1)), (slice(None), slice(1, None)))):
        a, b = idx[sl_a], idx[sl_b]
        both = (a >= 0) & (b >= 0)
        pairs.append(np.stack([a[both], b[both]], axis=1))
    edges = np.concatenate(pairs) if pairs else np.zeros((0, 2), dtype=np.int64)

    parent = list(range(n))

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    for a, b in edges.tolist():
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    roots = np.fromiter((find(i) for i in range(n)), dtype=np.int64, count=n)
    out = np.full(occ.shape, -1, dtype=np.int64)
    out[occ] = roots
    return out


def _face_nodes(pid: np.ndarray, i: np.ndarray, j: np.ndarray, k: np.ndarray,
                direction: str) -> np.ndarray:
    """The four point ids of one face of each cell, wound so the normal points `direction`."""
    corners = FACE_CORNERS[direction]
    out = np.empty((i.size, 4), dtype=np.int32)
    for c, (di, dj, dk) in enumerate(corners):
        out[:, c] = pid[i + di, j + dj, k + dk]
    return out


def _write_face_block(fh, nodes: np.ndarray) -> None:
    _write_lines(fh, (f"4({a} {b} {c} {d})" for a, b, c, d in nodes.tolist()))


def _write_boundary(path: Path, groups: list, n_internal: int, note: str) -> None:
    """One entry per patch, in face order. Consecutive `walls` groups are ONE patch."""
    body: list[str] = []
    start = n_internal
    order: list[tuple[str, str, int, int]] = []
    for name, kind, faces, _d in groups:
        if order and order[-1][0] == name:
            order[-1] = (name, kind, order[-1][2] + int(faces.size), order[-1][3])
        else:
            order.append((name, kind, int(faces.size), start))
        start += int(faces.size)
    for name, kind, n, first in order:
        body.append(f"    {name}\n    {{\n        type            {kind};\n"
                    f"        nFaces          {n};\n"
                    f"        startFace       {first};\n    }}")
    with path.open("w", encoding="utf-8") as fh:
        fh.write(_foam_header("polyBoundaryMesh", "boundary", note))
        fh.write(f"{len(body)}\n(\n")
        fh.write("\n".join(body))
        fh.write("\n)\n\n// ********************************************** //\n")


def _write_cellzones(path: Path, names: list[str], cell_zone: np.ndarray,
                     note: str) -> None:
    order = np.argsort(cell_zone, kind="stable")
    sorted_zone = cell_zone[order]
    bounds = np.searchsorted(sorted_zone, np.arange(len(names) + 1))
    with path.open("w", encoding="utf-8") as fh:
        fh.write(_foam_header("regIOobject", "cellZones", note))
        present = [(n, order[bounds[i]:bounds[i + 1]]) for i, n in enumerate(names)
                   if bounds[i + 1] > bounds[i]]
        fh.write(f"{len(present)}\n(\n")
        for name, ids in present:
            fh.write(f"{name}\n{{\n    type            cellZone;\n"
                     f"    cellLabels      List<label> \n{ids.size}\n(\n")
            _write_lines(fh, map(str, np.sort(ids).tolist()))
            fh.write(")\n;\n}\n")
        fh.write(")\n\n// ********************************************** //\n")


# --- entry points ---------------------------------------------------------------------


def mesh_chip(path: Path | str, case_dir: Path | str | None = None,
              **kwargs: Any) -> FoamMesh:
    """Load `chip/<id>.json` (schema-checked) and write its polyMesh."""
    path = Path(path)
    chip = schema_io.load_json(path)
    errors = schema_io.schema_errors("chip_spec", chip)
    out = write_polymesh(chip, case_dir, **kwargs)
    if errors:
        out.meta["spec_schema_problems"] = [
            f"{schema_io.path_str(e)}: {schema_io.describe(e)}" for e in errors[:20]
        ]
    out.meta["spec_path"] = str(path)
    return out


def repo_chips() -> list[Path]:
    return sorted((ROOT / "chip").glob("*.json"))


def checkmesh(case_dir: Path | str, **kwargs: Any) -> dict:
    """Run `checkMesh` on a written case through `assay2chip.foam.runner`.

    The runner is the ONLY place that knows how to launch OpenFOAM (foam/README.md), so a
    missing runner or a missing OpenFOAM is a clean "did not run" with the reason - never a
    local fallback, and never a pass by default.
    """
    try:
        from assay2chip.foam import runner  # noqa: PLC0415  runner.py is a sibling's
    except Exception as exc:
        return {"ran": False, "reason": f"assay2chip.foam.runner is not importable ({exc})"}
    if hasattr(runner, "available") and not runner.available():
        return {"ran": False, "reason": (runner.why_not() if hasattr(runner, "why_not")
                                         else "OpenFOAM is not installed")}
    # checkMesh reads system/controlDict, fvSchemes and fvSolution before it will look at
    # a mesh at all. A real solver case is case.py's job; these are the three dictionaries
    # needed to READ a mesh, and they are written through case.py's own writers so the
    # dictionary format keeps one owner.
    system = Path(case_dir) / "system"
    stub = ("mesh check only: assay2chip/foam/mesh.py wrote this so checkMesh can read "
            "the polyMesh. A solver case comes from assay2chip.foam.case.")
    if not (system / "controlDict").is_file():
        try:
            from assay2chip.foam import case  # noqa: PLC0415  case.py is a sibling's

            case.write_dict(system / "controlDict",
                            case.control_dict(application="checkMesh", end_time=0,
                                              write_interval=1), note=stub)
            case.write_dict(system / "fvSchemes", case.fv_schemes(), note=stub)
            case.write_dict(system / "fvSolution", case.fv_solution(), note=stub)
        except Exception as exc:
            return {"ran": False,
                    "reason": f"checkMesh needs system/controlDict, fvSchemes and "
                              f"fvSolution, and assay2chip.foam.case could not write them "
                              f"({type(exc).__name__}: {exc})"}
    try:
        # Absolute: the runner's session may not share this process's working directory.
        res = runner.run(Path(case_dir).resolve(), "checkMesh", check=False, **kwargs)
    except Exception as exc:
        return {"ran": False, "reason": f"{type(exc).__name__}: {exc}"}
    text = res.log_text() if hasattr(res, "log_text") else ""
    lines = [ln.strip() for ln in text.splitlines()]
    return {
        "ran": True,
        "ok": bool("Mesh OK." in text) and int(getattr(res, "returncode", 1) or 0) == 0,
        "returncode": getattr(res, "returncode", None),
        "verdict": next((ln for ln in lines
                         if ln.startswith("Mesh OK") or "mesh check" in ln.lower()), ""),
        "failed_checks": [ln for ln in lines if ln.startswith("***")],
        "warnings": [ln for ln in lines if ln.startswith("<<") or " ***" in ln],
        "cells": next((ln for ln in lines if ln.startswith("cells:")), ""),
        "log": str(getattr(res, "log_path", "")),
        "tail": "\n".join(lines[-30:]),
    }


# --- CLI ------------------------------------------------------------------------------


def _print(meta: dict) -> None:
    print(f"\n== {meta['chip_id']}  {meta['cells']} cells, {meta['points']} points, "
          f"{meta['faces']} faces ({meta['internal_faces']} internal) in "
          f"{meta['build_seconds']}s")
    xy, z, v = meta["xy"], meta["z"], meta["volume"]
    print(f"   xy {xy['dxy_mm'] * 1000:.2f} um ({xy['cells_across_narrowest']} cells "
          f"across {xy['narrowest_feature']} at {xy['narrowest_feature_mm']:g} mm), "
          f"grid {xy['nx']}x{xy['ny']}")
    print(f"   z  {z['cells']} cells over {z['depth_mm']:g} mm, "
          f"dz {z['dz_min_mm'] * 1000:.2f}-{z['dz_max_mm'] * 1000:.2f} um, "
          f"expansion {z['max_expansion']}, graded={z['graded']}")
    print(f"   volume {v['meshed_total_ul']:.4g} uL meshed (+{v['weir_crest_solid_ul']:.4g} "
          f"crest solid) vs {v['nominal_total_ul']:.4g} "
          f"nominal ({v['staircase_error_vs_nominal']:+.2%} staircase)"
          + (f" vs {v['spec_total_ul']:.4g} spec ({v['error_vs_spec_total']:+.2%})"
             if v["spec_total_ul"] else ""))
    conn = meta["connectivity"]
    print(f"   fluid is in {conn['regions']} connected region(s)"
          + ("" if conn["regions"] == 1 else
             ": " + "; ".join(f"{r['plan_cells']} cells, ports {r['ports'] or 'NONE'}, "
                              f"{', '.join(r['features'])}" for r in conn["detail"])))
    for w in meta["weirs"]:
        print(f"   weir {w['chamber']} {w['wall']}: crest {w['crest_thickness_mm']:g} mm "
              f"thick ({w['crest_cells_across']} cells) solid to "
              f"{w['crest_top_mm'] * 1000:.1f} um, gap {w['gap_um']:g} um in "
              f"{w['gap_cells']} cells")
    for p in meta["ports"]:
        if p.get("patch") is None and p.get("type") == "control":
            print(f"   port {p['port']:12s} --  control line, no patch")
            continue
        flag = "ok  " if p["enough_fluid"] else ("NONE" if p["patch"] is None else "LOW ")
        print(f"   port {p['port']:12s} {flag} {p['patch_faces']:5d} faces, "
              f"{p['patch_area_mm2']:.4f} mm2 of {p['bore_area_mm2']:.4f} mm2 bore "
              f"(needs {p['need_mm2']:.3f})")
    for f in meta["features"]:
        err = f["staircase_error_vs_nominal"]
        print(f"   {f['feature_id']:16s} {f['kind']:7s} {f['cells']:8d} cells  "
              f"{f['meshed_volume_ul']:9.4f} uL  "
              + (f"staircase {err:+.2%}" if err is not None else "")
              + (f"  vs spec {f['error_vs_spec_volume_ul']:+.2%}"
                 if f["error_vs_spec_volume_ul"] is not None else "")
              + (f"  ceded {f['ceded_to_neighbour_ul']:.4f} uL"
                 if f["ceded_to_neighbour_ul"] > 1e-9 else ""))
    for s in meta["synthesised"]:
        print(f"   ~ SYNTHESISED {s['feature_id']}: {s['lanes']} lanes, "
              f"{s['meshed_length_mm']:.4g} mm of {s['lane_width_mm']:g} mm lane "
              f"({s['lanes_from']})")
    for u in meta["unrepresented"]:
        print(f"   - NOT REPRESENTED {u['feature_id']}: {u['what']}")
    for u in meta["unresolved"]:
        print(f"   ! {u['feature_id']}: {u['question']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="chip/<id>.json -> an OpenFOAM structured hex polyMesh.")
    ap.add_argument("chip", type=Path, nargs="*", help="chip/<id>.json (default: all)")
    ap.add_argument("--all", action="store_true", help="every chip/*.json")
    ap.add_argument("--out", metavar="DIR", default=None,
                    help="write <DIR>/<chip_id>/constant/polyMesh")
    ap.add_argument("--cell-budget", type=int, default=DEFAULT_CELL_BUDGET)
    ap.add_argument("--cells-across", type=int, default=MIN_CELLS_ACROSS)
    ap.add_argument("--cells-z", type=int, default=MIN_CELLS_Z)
    ap.add_argument("--cells-gap", type=int, default=MIN_CELLS_GAP)
    ap.add_argument("--gap-um", type=float, default=None,
                    help="override every weir gap (tolerance sweep)")
    ap.add_argument("--gap-delta-um", type=float, default=0.0,
                    help="shift every weir gap, e.g. +20 / -20")
    ap.add_argument("--checkmesh", action="store_true",
                    help="run checkMesh through assay2chip.foam.runner")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    paths = args.chip or (repo_chips() if (args.all or not args.chip) else [])
    report: dict[str, Any] = {"meshed": {}, "failed": {}}
    rc = 0
    for path in paths:
        case = Path(args.out) / path.stem if args.out else None
        try:
            out = mesh_chip(path, case, cell_budget=args.cell_budget,
                            min_cells_across=args.cells_across,
                            min_cells_z=args.cells_z, min_cells_gap=args.cells_gap,
                            gap_um=args.gap_um, gap_delta_um=args.gap_delta_um)
        except Exception as exc:
            report["failed"][path.name] = f"{type(exc).__name__}: {exc}"
            if not args.json:
                print(f"\n== {path.name}: COULD NOT MESH: {type(exc).__name__}: {exc}")
            rc = 1
            continue
        if args.checkmesh and case:
            out.meta["checkMesh"] = checkmesh(case)
        report["meshed"][out.meta["chip_id"]] = out.meta
        if not args.json:
            _print(out.meta)
            if out.meta.get("checkMesh"):
                print(f"   checkMesh: {out.meta['checkMesh']}")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

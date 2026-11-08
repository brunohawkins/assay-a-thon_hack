"""A chip spec -> a rectilinear height field, and the outline of its cut region.

The fabrication geometry for both routes is one height field over the footprint:

  chip body     a plate as tall as base + fluidic, LOWERED by each feature's depth where
                the chip is cut (channels open, sealed afterwards with film or a lid -
                cad/fabrication.json, contracts/blocks.json fabrication_floors.note)
  emboss mould  a plate MOULD_BASE_MM tall, RAISED by the same amount in the same places:
                the exact inverse, which is what chip/fusion/chip_from_spec builds

So one grid serves both, and the mould is guaranteed to be the inverse of the body
because the two meshes come from the same cell classification.

WHY A RECTILINEAR GRID AND NOT A RASTER. The grid lines are the feature edges themselves
(every chamber wall, every channel wall), so an axis-aligned feature lands on grid lines
EXACTLY: 30.000 mm stays 30.000 mm and a 0.2 mm channel stays 0.2 mm. Only a feature with
no axis-aligned edge - a diagonal channel run, a round port pad - is sampled, at
`pitch`, and `plan_geometry()` reports how many features that applied to so the manifest
can say so. A uniform raster fine enough for a 0.2 mm floor would also be ~10^6 cells.

The feature list comes from chip/fusion/chip_from_spec.plan(), which is the authority on
what the mould contains: it honours a channel's routed `path` when the spec has one and
otherwise applies the same automatic route the twin and the display model use. S11 never
re-derives it, so a routed channel can never come out as a straight line between its
endpoints.

Grid topology matters for watertightness. A lattice point where the four cells around it
alternate cut/uncut (a checkerboard, which a sampled diagonal produces) would make its
vertical edge belong to four faces instead of two, and the mesh would not be closed.
`repair_corners()` removes those by opening the cut - never by closing it, because a
blocked channel is a dead chip and a channel one grid cell wide at a diagonal step is
cosmetic. Every repair is counted and reported.
"""

from __future__ import annotations

import bisect
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_MOULD_DIR = ROOT / "chip" / "fusion" / "chip_from_spec"
for _p in (str(ROOT), str(_MOULD_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import chip_from_spec  # noqa: E402  the positive-mould builder; do not duplicate its plan

from assay2chip.lib import contracts  # noqa: E402  the ONE place a contract value comes from

plan = chip_from_spec.plan
MOULD_BASE_MM = chip_from_spec.MOULD_BASE_MM
MIRROR_MOULD = chip_from_spec.MIRROR_MOULD

SAMPLING_FINENESS = 4       # sampling steps across the smallest feature anyone may draw


def default_pitch_mm() -> float:
    """Sampling pitch for the only features with no axis-aligned edge: a diagonal channel
    run and a round port pad.

    A quarter of the in-plane fabrication floor, read through assay2chip.lib.contracts so
    the number lives in contracts/blocks.json and nowhere else. A ContractError here is
    correct: S11 must not invent a pitch.
    """
    return contracts.floor("min_open_channel_mm") / SAMPLING_FINENESS


SNAP_MM = 1e-6      # grid lines closer together than this are the same line
EPS = 1e-9
MAX_REPAIR_PASSES = 32


# --- features ---------------------------------------------------------------------------

def is_axis_aligned(f: dict) -> bool:
    """True when the feature's own edges are parallel to x and y (so they can BE grid lines)."""
    if f["kind"] != "box":
        return False
    ux, uy = f["dir"]
    return abs(ux) < 1e-9 or abs(uy) < 1e-9


def extent(f: dict) -> tuple[float, float, float, float]:
    """(x0, x1, y0, y1) bounding box of one planned feature, in mm."""
    if f["kind"] == "cyl":
        return f["x"] - f["r"], f["x"] + f["r"], f["y"] - f["r"], f["y"] + f["r"]
    ux, uy = f["dir"]
    hx = (abs(ux) * f["len"] + abs(uy) * f["wid"]) / 2
    hy = (abs(uy) * f["len"] + abs(ux) * f["wid"]) / 2
    return f["x"] - hx, f["x"] + hx, f["y"] - hy, f["y"] + hy


def covers(f: dict, x: float, y: float) -> bool:
    """Is (x, y) inside this feature? Exact for both a rotated box and a circle."""
    if f["kind"] == "cyl":
        return (x - f["x"]) ** 2 + (y - f["y"]) ** 2 <= f["r"] ** 2 + EPS
    ux, uy = f["dir"]
    dx, dy = x - f["x"], y - f["y"]
    along = dx * ux + dy * uy
    across = -dx * uy + dy * ux
    return abs(along) <= f["len"] / 2 + EPS and abs(across) <= f["wid"] / 2 + EPS


# --- the grid ---------------------------------------------------------------------------

def _dedupe(values: list[float], lo: float, hi: float) -> list[float]:
    out: list[float] = []
    for v in sorted(min(max(v, lo), hi) for v in values):
        if not out or v - out[-1] > SNAP_MM:
            out.append(v)
    if out[0] > lo + SNAP_MM:
        out.insert(0, lo)
    if out[-1] < hi - SNAP_MM:
        out.append(hi)
    out[0], out[-1] = lo, hi
    return out


def grid_lines(p: dict, pitch: float) -> tuple[list[float], list[float], list[str]]:
    """Grid lines for a plan: every axis-aligned feature edge, plus `pitch` sampling across
    anything round or diagonal. Returns (xs, ys, ids of the sampled features)."""
    fx, fy = p["fx"], p["fy"]
    xs, ys = [0.0, fx], [0.0, fy]
    sampled: list[str] = []
    for f in p["features"]:
        x0, x1, y0, y1 = extent(f)
        xs += [x0, x1]
        ys += [y0, y1]
        if is_axis_aligned(f):
            continue
        sampled.append(f["id"])
        n_x = max(1, int(math.ceil((x1 - x0) / pitch)))
        n_y = max(1, int(math.ceil((y1 - y0) / pitch)))
        xs += [x0 + (x1 - x0) * i / n_x for i in range(1, n_x)]
        ys += [y0 + (y1 - y0) * j / n_y for j in range(1, n_y)]
    return _dedupe(xs, 0.0, fx), _dedupe(ys, 0.0, fy), sorted(set(sampled))


def rasterise(p: dict, xs: list[float], ys: list[float]) -> list[list[float]]:
    """depths[j][i] = how deep cell (i, j) is cut, in mm (0.0 where the chip is solid).

    A cell covered by two features (a channel entering a chamber, a port pad on a channel)
    takes the deeper of the two, because one etch depth cuts to the deepest thing asked for.
    """
    depths = [[0.0] * (len(xs) - 1) for _ in range(len(ys) - 1)]
    for f in p["features"]:
        x0, x1, y0, y1 = extent(f)
        for j in range(len(ys) - 1):
            cy = (ys[j] + ys[j + 1]) / 2
            if cy < y0 - EPS or cy > y1 + EPS:
                continue
            row = depths[j]
            for i in range(len(xs) - 1):
                cx = (xs[i] + xs[i + 1]) / 2
                if cx < x0 - EPS or cx > x1 + EPS:
                    continue
                if covers(f, cx, cy) and f["height"] > row[i]:
                    row[i] = f["height"]
    return depths


def repair_corners(depths: list[list[float]]) -> int:
    """Make the height field closeable: no lattice point may have more than two steps.

    Around a lattice point the four cells form a 4-cycle; the number of neighbouring pairs
    with different heights is even, and 4 means the point is a checkerboard whose vertical
    edge would belong to four faces. Flattening all four cells to the DEEPEST of them
    leaves zero steps there. Only ever cuts deeper (never fills), so it terminates, and a
    nonzero return belongs in the manifest.
    """
    ny, nx = len(depths), len(depths[0]) if depths else 0
    fixed = 0
    for _ in range(MAX_REPAIR_PASSES):
        changed = 0
        for j in range(1, ny):
            for i in range(1, nx):
                a, b = depths[j - 1][i - 1], depths[j - 1][i]
                c, d = depths[j][i - 1], depths[j][i]
                steps = sum(abs(u - v) > SNAP_MM for u, v in ((a, c), (b, d), (a, b), (c, d)))
                if steps <= 2:
                    continue
                deep = max(a, b, c, d)
                depths[j - 1][i - 1] = depths[j - 1][i] = depths[j][i - 1] = depths[j][i] = deep
                changed += 1
        fixed += changed
        if not changed:
            return fixed
    raise RuntimeError(
        f"height field still has checkerboard corners after {MAX_REPAIR_PASSES} passes "
        f"({fixed} repaired): the feature plan is finer than the sampling pitch"
    )


# --- outline of the cut region ----------------------------------------------------------

def trace_loops(xs: list[float], ys: list[float],
                depths: list[list[float]]) -> list[list[tuple[float, float]]]:
    """Closed boundary loops of the CUT region, cut on the left (outer loops run
    counter-clockwise, holes clockwise). This is the contour a cutter would follow and the
    fill the top-view render shades. Collinear points are dropped, so a plain rectangular
    chamber comes out as four corners."""
    nx, ny = len(xs) - 1, len(ys) - 1

    def cut(i: int, j: int) -> bool:
        return 0 <= i < nx and 0 <= j < ny and depths[j][i] > SNAP_MM

    edges: dict[tuple[float, float], list[tuple[float, float]]] = {}

    def add(a: tuple[float, float], b: tuple[float, float]) -> None:
        edges.setdefault(a, []).append(b)

    for i in range(nx + 1):                     # vertical grid edges at x = xs[i]
        for j in range(ny):
            left, right = cut(i - 1, j), cut(i, j)
            if left == right:
                continue
            # Travelling +y keeps -x on the left, so the run goes +y when the CUT is left.
            lo, hi = (xs[i], ys[j]), (xs[i], ys[j + 1])
            add(lo, hi) if left else add(hi, lo)
    for j in range(ny + 1):                     # horizontal grid edges at y = ys[j]
        for i in range(nx):
            below, above = cut(i, j - 1), cut(i, j)
            if below == above:
                continue
            # Travelling +x keeps +y on the left, so the run goes +x when the CUT is above.
            lo, hi = (xs[i], ys[j]), (xs[i + 1], ys[j])
            add(hi, lo) if below else add(lo, hi)

    loops: list[list[tuple[float, float]]] = []
    while edges:
        start = next(iter(edges))
        loop = [start]
        here = start
        while True:
            nxt = edges[here].pop()
            if not edges[here]:
                del edges[here]
            if nxt == start:
                break
            loop.append(nxt)
            here = nxt
            if here not in edges:
                raise RuntimeError(f"cut outline does not close (open end at {here})")
        loops.append(_simplify(loop))
    return loops


def _simplify(loop: list[tuple[float, float]]) -> list[tuple[float, float]]:
    n = len(loop)
    out = []
    for k in range(n):
        a, b, c = loop[(k - 1) % n], loop[k], loop[(k + 1) % n]
        cross = (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])
        if abs(cross) > 1e-12:
            out.append(b)
    return out or loop


def depth_at(geom: dict, x: float, y: float) -> float:
    """How deep the chip is cut at (x, y), in mm; 0.0 where it is solid.

    The one honest way to ask "is there a channel here?", and what the routed-path test
    uses to prove a bend was built and the straight chord between its endpoints was not.
    """
    xs, ys = geom["xs"], geom["ys"]
    i = min(max(bisect.bisect_right(xs, x) - 1, 0), len(xs) - 2)
    j = min(max(bisect.bisect_right(ys, y) - 1, 0), len(ys) - 2)
    return geom["depths"][j][i]


def loop_area(loop: list[tuple[float, float]]) -> float:
    """Signed area, mm2. Positive = counter-clockwise = an outer loop of the cut region."""
    n = len(loop)
    return sum(loop[k][0] * loop[(k + 1) % n][1] - loop[(k + 1) % n][0] * loop[k][1]
               for k in range(n)) / 2


# --- the one entry point ----------------------------------------------------------------

def mirrored_grid(xs: list[float], fx: float) -> list[float]:
    """The x grid lines reflected about fx, in order. Used so the mould's grid is the
    body's grid EXACTLY rather than nearly.

    Re-deriving sampling lines in the mirrored frame gives lines a micron or two off the
    body's, because `ceil((x1 - x0) / pitch)` lands either side of an integer depending on
    where the feature sits. The grids then disagree and the cell-by-cell inverse check
    cannot be made. Reflecting is exact, and it leaves the mould's cell DEPTHS still
    derived independently from chip_from_spec's mirrored plan - so `is_inverse_of` remains
    a real comparison of two separate calculations and not a tautology.
    """
    return [fx - x for x in reversed(xs)]


def plan_geometry(spec: dict, mirror: bool = False, pitch: float = None,
                  grid: tuple = None) -> dict:
    """Everything both STLs, the DXF and the renders are built from.

    mirror=False is the chip as the spec draws it (the body, the DXF, the renders);
    mirror=True is chip_from_spec's mirrored tool (the embossing mould).
    `grid` forces (xs, ys) instead of deriving them - see `mirrored_grid`.
    """
    pitch = default_pitch_mm() if pitch is None else pitch
    p = plan(spec, mirror=mirror)
    if grid is None:
        xs, ys, sampled = grid_lines(p, pitch)
    else:
        xs, ys = [list(grid[0]), list(grid[1])]
        sampled = [f["id"] for f in p["features"] if not is_axis_aligned(f)]
    depths = rasterise(p, xs, ys)
    repaired = repair_corners(depths)
    cut_area = sum((xs[i + 1] - xs[i]) * (ys[j + 1] - ys[j])
                   for j in range(len(ys) - 1) for i in range(len(xs) - 1)
                   if depths[j][i] > SNAP_MM)
    return {
        "plan": p,
        "xs": xs,
        "ys": ys,
        "depths": depths,
        "pitch_mm": pitch,
        "sampled_features": sampled,
        "corner_repairs": repaired,
        "cells": (len(xs) - 1) * (len(ys) - 1),
        "cut_area_mm2": cut_area,
        "max_depth_mm": max((d for row in depths for d in row), default=0.0),
        "depths_seen_mm": sorted({round(d, 6) for row in depths for d in row if d > SNAP_MM}),
    }

"""Plane geometry for S7: points, segments, rectangles and polylines, in millimetres.

Every distance here is a CENTRELINE distance. A channel's body is its polyline grown by
width/2, so the clearance between two channels is

    dist(polyline_a, polyline_b) - (width_a + width_b) / 2

and the same idea with a rectangle (a chamber) or a point (a port hole centre, grown by
half the bore). That one convention is what `planarity.py` checks and what the router
rasterises, so the two cannot disagree about what "0.4 mm wall" means.

The segment maths is deliberately the SAME maths as cad/make_figures.py (_point_seg,
_seg_seg, _seg_rect) and tools/check_chip_spec.py (_intersect). It is written out here
rather than imported because cad/make_figures.py is a 2,000-line figure script with
module-level side effects, and this module has to be importable from a test with no
matplotlib, no files and no chip. tests/test_assay2chip_route.py pins the two together:
it imports both and asserts they agree on random inputs, so a drift is a test failure
rather than a routed chip that one of the two tools thinks is legal.

Standard library only.
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence

Pt = tuple[float, float]
Rect = tuple[float, float, float, float]   # (x0, y0, x1, y1), x0 <= x1, y0 <= y1

EPS = 1e-9


# --- points and segments --------------------------------------------------------------

def dist_point_seg(p: Sequence[float], a: Sequence[float], b: Sequence[float]) -> float:
    """Distance from point p to the segment a-b. Same maths as make_figures._point_seg."""
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    l2 = dx * dx + dy * dy
    if l2 < 1e-12:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / l2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _cross(o: Sequence[float], p: Sequence[float], q: Sequence[float]) -> float:
    return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])


def dist_seg_seg(a: Sequence[float], b: Sequence[float],
                 c: Sequence[float], d: Sequence[float]) -> float:
    """Minimum distance between segments a-b and c-d; 0.0 if they cross.

    Same maths as make_figures._seg_seg.
    """
    d1, d2 = _cross(c, d, a), _cross(c, d, b)
    d3, d4 = _cross(a, b, c), _cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 0.0
    return min(dist_point_seg(a, c, d), dist_point_seg(b, c, d),
               dist_point_seg(c, a, b), dist_point_seg(d, a, b))


def proper_intersection(a: Sequence[float], b: Sequence[float],
                        c: Sequence[float], d: Sequence[float]) -> Pt | None:
    """Where a-b and c-d cross, strictly inside BOTH segments, else None.

    Same maths and the same 1e-6 end tolerance as check_chip_spec._intersect, so a
    crossing this function reports is a crossing that tool reports too.
    """
    den = (b[0] - a[0]) * (d[1] - c[1]) - (b[1] - a[1]) * (d[0] - c[0])
    if abs(den) < 1e-12:
        return None
    t = ((c[0] - a[0]) * (d[1] - c[1]) - (c[1] - a[1]) * (d[0] - c[0])) / den
    u = ((c[0] - a[0]) * (b[1] - a[1]) - (c[1] - a[1]) * (b[0] - a[0])) / den
    eps = 1e-6
    if eps < t < 1 - eps and eps < u < 1 - eps:
        return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
    return None


# --- rectangles -----------------------------------------------------------------------

def rect_corners(r: Rect) -> list[Pt]:
    return [(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])]


def rect_edges(r: Rect) -> list[tuple[Pt, Pt]]:
    c = rect_corners(r)
    return list(zip(c, c[1:] + c[:1]))


def point_in_rect(p: Sequence[float], r: Rect, pad: float = 0.0) -> bool:
    return (r[0] - pad <= p[0] <= r[2] + pad) and (r[1] - pad <= p[1] <= r[3] + pad)


def dist_point_rect(p: Sequence[float], r: Rect) -> float:
    """0.0 inside or on the rectangle, else the distance to its boundary."""
    dx = max(r[0] - p[0], 0.0, p[0] - r[2])
    dy = max(r[1] - p[1], 0.0, p[1] - r[3])
    return math.hypot(dx, dy)


def dist_seg_rect(a: Sequence[float], b: Sequence[float], r: Rect) -> float:
    """Distance from segment a-b to rectangle r; 0.0 if they touch or it is inside.

    Same maths as make_figures._seg_rect.
    """
    if point_in_rect(a, r) or point_in_rect(b, r):
        return 0.0
    edges = rect_edges(r)
    if any(dist_seg_seg(a, b, e0, e1) == 0.0 for e0, e1 in edges):
        return 0.0
    return min([dist_point_seg(c, a, b) for c in rect_corners(r)]
               + [min(dist_point_seg(a, e0, e1), dist_point_seg(b, e0, e1)) for e0, e1 in edges])


def rect_gap(r: Rect, q: Rect) -> float:
    """Gap between two rectangles; 0.0 when they touch or overlap."""
    dx = max(q[0] - r[2], r[0] - q[2], 0.0)
    dy = max(q[1] - r[3], r[1] - q[3], 0.0)
    return math.hypot(dx, dy)


def square_at(p: Sequence[float], side: float) -> Rect:
    h = side / 2.0
    return (p[0] - h, p[1] - h, p[0] + h, p[1] + h)


# --- polylines ------------------------------------------------------------------------

def segments(path: Sequence[Sequence[float]]) -> list[tuple[Pt, Pt]]:
    return [((a[0], a[1]), (b[0], b[1])) for a, b in zip(path[:-1], path[1:])]


def length(path: Sequence[Sequence[float]]) -> float:
    return sum(math.dist(a, b) for a, b in segments(path))


def dist_point_path(p: Sequence[float], path: Sequence[Sequence[float]]) -> float:
    return min(dist_point_seg(p, a, b) for a, b in segments(path))


def dist_path_path(p: Sequence[Sequence[float]], q: Sequence[Sequence[float]]) -> float:
    return min(dist_seg_seg(a, b, c, d) for a, b in segments(p) for c, d in segments(q))


def dist_path_rect(path: Sequence[Sequence[float]], r: Rect) -> float:
    return min(dist_seg_rect(a, b, r) for a, b in segments(path))


def path_crossings(p: Sequence[Sequence[float]],
                   q: Sequence[Sequence[float]]) -> list[Pt]:
    """Every point where two polylines properly cross. Empty means planar, for this pair."""
    out = []
    for a, b in segments(p):
        for c, d in segments(q):
            hit = proper_intersection(a, b, c, d)
            if hit is not None:
                out.append(hit)
    return out


def self_crossings(path: Sequence[Sequence[float]]) -> list[Pt]:
    """Where a polyline crosses itself - a meander folded too far does this."""
    segs = segments(path)
    out = []
    for i, (a, b) in enumerate(segs):
        for c, d in segs[i + 2:]:
            hit = proper_intersection(a, b, c, d)
            if hit is not None:
                out.append(hit)
    return out


def simplify(path: Sequence[Sequence[float]], tol: float = 1e-7) -> list[Pt]:
    """Drop repeated and collinear vertices, so a straight run is ONE segment.

    This is what makes the emitted `path` honest about its block: a two-point path is a
    straight_channel, and every surviving interior vertex is a real channel_corner.
    """
    pts: list[Pt] = []
    for p in path:
        q = (float(p[0]), float(p[1]))
        if not pts or math.dist(pts[-1], q) > tol:
            pts.append(q)
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    for prev, cur, nxt in zip(pts, pts[1:], pts[2:]):
        if abs(_cross(prev, cur, nxt)) > tol:
            out.append(cur)
    out.append(pts[-1])
    return out


def corners(path: Sequence[Sequence[float]]) -> int:
    return max(0, len(simplify(path)) - 2)


def is_rectilinear(path: Sequence[Sequence[float]], tol: float = 1e-6) -> bool:
    """True when every segment runs along x or along y. The router only emits these."""
    return all(abs(b[0] - a[0]) < tol or abs(b[1] - a[1]) < tol for a, b in segments(path))


def bbox(points: Iterable[Sequence[float]]) -> Rect:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))

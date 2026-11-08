"""Height field -> a closed triangle mesh, and the validator every S11 STL must pass.

`heightfield()` builds the solid between a flat floor at z0 and a per-cell top:

  bottom   two triangles per cell, so its edges match the cell grid above it
  top      two triangles per cell at that cell's own height
  steps    a vertical quad on every grid edge whose two cells differ in height
  rim      a vertical quad on the footprint boundary, floor to that cell's top

Tessellating the bottom per cell looks wasteful next to two big triangles, and it is the
whole reason the mesh closes: a 30 x 20 mm rectangle split in two does not share edges
with the hundreds of little step quads standing on it, and every one of those would be a
naked edge. The cost is a few thousand extra triangles.

`validate()` is not a sanity check, it is the acceptance test named in the S11 brief, and
it fails loudly rather than warning:

  watertight       every undirected edge used exactly twice
  oriented         every directed edge used exactly once, so no neighbours disagree
  outward          signed volume positive, i.e. normals point out of the solid and not in
  bbox             the x/y bounding box equals the spec footprint, to 1 um
  origin           the solid starts at the spec origin (x=0, y=0, z=z0)
  units            millimetres, asserted by the bbox agreeing with the spec in mm

Triangles are (v0, v1, v2) with the outward normal by the right-hand rule, the same shape
chip/components/stl_io.py reads and writes - and that is the writer S11 uses, rather than
a second STL writer in this package.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_COMPONENTS = ROOT / "chip" / "components"
if str(_COMPONENTS) not in sys.path:
    sys.path.insert(0, str(_COMPONENTS))

import stl_io  # noqa: E402  the repo's STL writer/reader; S11 adds no second one

Pt = tuple[float, float, float]
Tri = tuple[Pt, Pt, Pt]

TOL_MM = 1e-6
ROUND = 6       # vertices snapped to 1 nm before edges are compared


def _q(p: Pt) -> tuple[float, float, float]:
    return (round(p[0], ROUND), round(p[1], ROUND), round(p[2], ROUND))


def heightfield(xs: list[float], ys: list[float], tops: list[list[float]],
                z0: float = 0.0) -> list[Tri]:
    """A closed solid from z0 up to tops[j][i] over the rectilinear grid xs x ys."""
    nx, ny = len(xs) - 1, len(ys) - 1
    if nx < 1 or ny < 1:
        raise ValueError("a height field needs at least one cell")
    tris: list[Tri] = []

    def quad(a: Pt, b: Pt, c: Pt, d: Pt) -> None:
        tris.append((a, b, c))
        tris.append((a, c, d))

    for j in range(ny):
        y0, y1 = ys[j], ys[j + 1]
        for i in range(nx):
            x0, x1 = xs[i], xs[i + 1]
            t = tops[j][i]
            if t <= z0 + TOL_MM:
                raise ValueError(f"cell ({i}, {j}) has no thickness: top {t} <= floor {z0}")
            # floor, normal -z
            quad((x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0))
            # top, normal +z
            quad((x0, y0, t), (x1, y0, t), (x1, y1, t), (x0, y1, t))

    for j in range(ny):
        y0, y1 = ys[j], ys[j + 1]
        for i in range(nx + 1):                     # vertical grid line x = xs[i]
            x = xs[i]
            left = tops[j][i - 1] if i > 0 else None
            right = tops[j][i] if i < nx else None
            if left is None:                        # rim, normal -x
                quad((x, y0, z0), (x, y0, right), (x, y1, right), (x, y1, z0))
            elif right is None:                     # rim, normal +x
                quad((x, y0, z0), (x, y1, z0), (x, y1, left), (x, y0, left))
            elif abs(left - right) > TOL_MM:
                lo, hi = min(left, right), max(left, right)
                if left > right:                    # step faces +x
                    quad((x, y0, lo), (x, y1, lo), (x, y1, hi), (x, y0, hi))
                else:                               # step faces -x
                    quad((x, y0, lo), (x, y0, hi), (x, y1, hi), (x, y1, lo))

    for i in range(nx):
        x0, x1 = xs[i], xs[i + 1]
        for j in range(ny + 1):                     # horizontal grid line y = ys[j]
            y = ys[j]
            below = tops[j - 1][i] if j > 0 else None
            above = tops[j][i] if j < ny else None
            if below is None:                       # rim, normal -y
                quad((x0, y, z0), (x1, y, z0), (x1, y, above), (x0, y, above))
            elif above is None:                     # rim, normal +y
                quad((x0, y, z0), (x0, y, below), (x1, y, below), (x1, y, z0))
            elif abs(below - above) > TOL_MM:
                lo, hi = min(below, above), max(below, above)
                if below > above:                   # step faces +y
                    quad((x0, y, lo), (x0, y, hi), (x1, y, hi), (x1, y, lo))
                else:                               # step faces -y
                    quad((x0, y, lo), (x1, y, lo), (x1, y, hi), (x0, y, hi))

    return tris


# --- measurement ------------------------------------------------------------------------

def bbox(tris: list[Tri]) -> tuple[list[float], list[float]]:
    return stl_io.bbox(tris)


def volume(tris: list[Tri]) -> float:
    """Enclosed volume, mm3. Signed, so a negative value means the normals point inward."""
    v = 0.0
    for a, b, c in tris:
        v += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
              + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6
    return v


def edge_faults(tris: list[Tri]) -> tuple[list[str], list[str]]:
    """(naked/over-used edges, edges two faces wind the same way). Both empty = closed and
    consistently oriented."""
    undirected: dict[frozenset, int] = defaultdict(int)
    directed: dict[tuple, int] = defaultdict(int)
    for t in tris:
        q = [_q(p) for p in t]
        for a, b in ((q[0], q[1]), (q[1], q[2]), (q[2], q[0])):
            undirected[frozenset((a, b))] += 1
            directed[(a, b)] += 1
    open_edges = [f"edge {sorted(e)} used {n}x (should be 2)"
                  for e, n in undirected.items() if n != 2]
    flipped = [f"directed edge {a} -> {b} used {n}x (should be 1): neighbours disagree"
               for (a, b), n in directed.items() if n != 1]
    return open_edges[:8], flipped[:8]


def validate(tris: list[Tri], footprint: dict, z0: float = 0.0,
             z_top: float | None = None, name: str = "mesh") -> dict:
    """Every STL check the S11 brief names, as a dict the manifest records verbatim."""
    checks: dict[str, dict] = {}

    def record(key: str, passed: bool, detail: str) -> None:
        checks[key] = {"passed": bool(passed), "detail": detail}

    open_edges, flipped = edge_faults(tris)
    record("watertight", not open_edges,
           "every edge used exactly twice" if not open_edges else "; ".join(open_edges))
    record("consistent_winding", not flipped,
           "no two faces wind an edge the same way" if not flipped else "; ".join(flipped))

    vol = volume(tris)
    record("outward_normals", vol > 0,
           f"signed volume {vol:.4f} mm3 > 0: normals point out of the solid" if vol > 0
           else f"signed volume {vol:.4f} mm3 <= 0: normals point INTO the solid")

    lo, hi = bbox(tris)
    want_x, want_y = float(footprint["x"]), float(footprint["y"])
    size_ok = abs(hi[0] - lo[0] - want_x) <= TOL_MM and abs(hi[1] - lo[1] - want_y) <= TOL_MM
    record("bbox_is_footprint", size_ok,
           f"{hi[0] - lo[0]:.6g} x {hi[1] - lo[1]:.6g} mm vs spec footprint "
           f"{want_x:g} x {want_y:g} mm")

    at_origin = abs(lo[0]) <= TOL_MM and abs(lo[1]) <= TOL_MM and abs(lo[2] - z0) <= TOL_MM
    record("at_spec_origin", at_origin,
           f"min corner ({lo[0]:.6g}, {lo[1]:.6g}, {lo[2]:.6g}) mm, expected (0, 0, {z0:g})")

    record("units_mm", size_ok,
           "footprint in the mesh matches the spec's millimetres, so the file is mm "
           "(a 25.4x unit error would show here as inches)")

    if z_top is not None:
        record("height_is_stackup", abs(hi[2] - z_top) <= TOL_MM,
               f"top at z = {hi[2]:.6g} mm, expected {z_top:g} mm")

    return {
        "name": name,
        "triangles": len(tris),
        "volume_mm3": round(vol, 6),
        "bbox_min_mm": [round(v, 6) for v in lo],
        "bbox_max_mm": [round(v, 6) for v in hi],
        "checks": checks,
        "passed": all(c["passed"] for c in checks.values()),
    }


class MeshNotPrintable(ValueError):
    """An STL S11 built does not close, is inside out, or is not the size the spec says."""


def write_validated(path: Path, tris: list[Tri], footprint: dict, z0: float = 0.0,
                    z_top: float | None = None, name: str = "") -> dict:
    """Validate, THEN write. A mesh that fails leaves no file, so nothing unprintable can
    reach a printer or a press from this pipeline."""
    report = validate(tris, footprint, z0, z_top, name or path.stem)
    if not report["passed"]:
        bad = "; ".join(f"{k}: {v['detail']}" for k, v in report["checks"].items()
                        if not v["passed"])
        raise MeshNotPrintable(f"{path.name} was not written: {bad}")
    stl_io.write_stl(path, tris, name or path.stem)
    return report

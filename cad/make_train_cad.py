"""Generate CAD for a chip spec: one STL per part, plus a DXF outline of the fluidic layer.

    python cad/make_train_cad.py                     # default: the iPSC-to-MGL train
    python cad/make_train_cad.py --chip chip/<spec>.json
    python cad/make_train_cad.py --all               # every chip spec, chip/ and cad/*_spec.json

Writes, into cad/exports/:
  <chamber_id>.stl       the chamber cavity, a box, exported in place in mm
  <port_id>.stl          the through-hole in the lid, in place
  <chip_id>.dxf          the outline of the fluidic layer: every chamber and channel
  <chip_id>_closed.dxf   the same outline as CLOSED polylines: the file to machine

Why a box is the right shape here: every feature is one depth, set by the fluidic layer
(resin-printed tool, hot-embossed relief), so a chamber really is a rectangle in one flat
layer. The DXF is that outline: layers CHAMBERS and CHANNELS. Any channel shallower than
the fluidic layer lands on a GROOVES layer instead, and this script says so loudly: with
no photolithography that chip cannot be built as drawn.

THE TWO DXFs, AND WHY THERE ARE TWO
  <chip_id>_closed.dxf is the FABRICATION file. Every chamber and every channel is ONE
  closed LWPOLYLINE (group 70 = 1), so a laser or CAM tool sees a cut contour it can
  offset and fill instead of a scribe. Each channel STOPS on the chamber wall it enters
  (its end cap is trimmed back to that wall, not left straddling it), so no CHAMBERS
  outline and no CHANNELS outline cross, no channel segment runs through the inside of a
  chamber, and the two shapes meet edge to edge: under even-odd fill the junction stays
  open. Machine this one.
  <chip_id>.dxf is the REFERENCE drawing, and the hydraulic model's input. Every channel
  is the full centreline rectangle, four LINE entities, running to where its centreline
  meets the chamber wall: that is where the channel's hydraulic length ends, so
  operator/network.py and twin/network.js read their channel lengths and widths out of it
  (4 LINEs per channel, in order, on layer CHANNELS). Oblique channels therefore poke a
  half-width into the chamber here. Do not machine this one; it is a drawing of
  centrelines, not a cut path.
  Both files declare their units ($INSUNITS = 4, millimetres, plus $MEASUREMENT = 1), and
  both are rewritten on every run, so neither can go stale against the other.

Ownership of cad/exports/: the flat folder belongs to ONE chip (DEFAULT_CHIP); every other
chip writes to cad/exports/<chip_id>/. A hidden .chip_id file in each folder records the
owner, and a second chip trying to write into it is refused (a COLLISION), because part
ids repeat across chips and would overwrite each other silently. In a chip's OWN folder,
parts left behind by an older revision of that chip are deleted, because an orphan STL in
a folder a fabricator browses is a part that gets made by mistake; in the shared flat
folder they are only reported.

Export sets for retired or superseded specs get a RETIRED.md alongside the parts, so
nothing on disk looks live when it is not.

PRINTED CHIPS ARE FROZEN. A chip printed for the demo (frozen_chips(): built_for_demo, from
tools/build_assay_variants.py PRINTED_FOR_DEMO and protocols/pipeline*.json) is REFUSED: its
exports are the as-printed record, and a newer generator must not quietly put a different
chip on disk. --force-frozen overrides that, for the byte-identical proof only.

CHANNEL PATHS AND PORT PADS. A channel's "path" polyline is followed (cut back at each end
to the part it leaves); without one the channel is the straight clipped run it always was.
A pathed channel is one mitred closed outline in the _closed.dxf and one rectangle per
segment (4 LINEs each) in the reference .dxf. A port's optional "pad" is cut in the fluidic
layer under the bore: <port_id>_pad.stl, layer PADS in both DXFs, and the port's channels
stop on the pad's boundary so the bore opens onto the pad. centrelines() and the pad
helpers are the one implementation cad/make_figures.py, tools/check_chip_spec.py and
operator/network.py all import.

Standard library only. See cad/README.md for the export rules the twin depends on
(names, millimetres, shared origin).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORTS = ROOT / "cad" / "exports"
DEFAULT_CHIP = ROOT / "chip" / "ipsc_to_mgl_chip_v1.json"

def _port_size_mm() -> float:
    """Tubing port bore, from contracts/blocks.json rather than a literal.

    This was hard-coded at 2.0 mm, which was fine on a 150 x 100 mm board and absurd
    once every chip was scaled into a 30 x 20 mm envelope: a 2 mm port is wider than the
    junction it taps, and it reported short circuits that were artefacts of the drawing
    rather than the design. The fabrication floor is the honest number.
    """
    try:
        import json
        b = json.loads((ROOT / "contracts" / "blocks.json").read_text())
        return float(b["fabrication_floors"]["min_port_mm"])
    except Exception:
        return 2.0


PORT_SIZE_MM = _port_size_mm()   # square through-hole drawn for each tubing port
BLOCKS = ROOT / "contracts" / "blocks.json"

# Specs the project has retired or superseded (cad/README.md "Generated specs",
# protocols/pipeline_status.md "Retired and duplicates"). Their export folders still hold
# real-looking parts, so each one gets a RETIRED.md: a fabricator browsing cad/exports/
# cannot otherwise tell them from the live chips.
RETIRED = {
    "m1_culture_v1": "superseded by chip/module1_v1.json (Module 1 v1)",
    "m1b_cellprep_v1": "superseded by chip/cell_prep_v1.json (the spec still says module1_v1; "
                       "protocols/pipeline_status.md has the correct replacement)",
    "m2_encapsulation_v1": "RETIRED: capsules cannot be made at this scale; replaced by "
                           "chip/spc_demo_v1.json",
    "m3_wash_v1": "superseded by chip/module3_v1_translated.json",
    "sctip_spc_v1": "retired: stage A is replaced by chip/spc_demo_v1.json, stages B and C "
                    "duplicate chip/module3_v1_translated.json",
}


# --- STL ------------------------------------------------------------------------------

def box_triangles(center: list[float], size: list[float]):
    """12 triangles for an axis-aligned box, as (normal, (v0, v1, v2))."""
    (cx, cy, cz), (sx, sy, sz) = center, size
    x0, x1 = cx - sx / 2, cx + sx / 2
    y0, y1 = cy - sy / 2, cy + sy / 2
    z0, z1 = cz - sz / 2, cz + sz / 2
    c = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [((0, 0, -1), (0, 2, 1)), ((0, 0, -1), (0, 3, 2)),
             ((0, 0, 1), (4, 5, 6)), ((0, 0, 1), (4, 6, 7)),
             ((0, -1, 0), (0, 1, 5)), ((0, -1, 0), (0, 5, 4)),
             ((0, 1, 0), (3, 7, 6)), ((0, 1, 0), (3, 6, 2)),
             ((-1, 0, 0), (0, 4, 7)), ((-1, 0, 0), (0, 7, 3)),
             ((1, 0, 0), (1, 2, 6)), ((1, 0, 0), (1, 6, 5))]
    return [(n, (c[a], c[b], c[d])) for n, (a, b, d) in faces]


def prism_triangles(poly, z0: float, z1: float):
    """Triangles for a convex counter-clockwise polygon extruded from z0 to z1 (a port pad)."""
    out = []
    n = len(poly)
    for i in range(1, n - 1):
        a, b, c = poly[0], poly[i], poly[i + 1]
        out.append(((0, 0, -1), ((a[0], a[1], z0), (c[0], c[1], z0), (b[0], b[1], z0))))
        out.append(((0, 0, 1), ((a[0], a[1], z1), (b[0], b[1], z1), (c[0], c[1], z1))))
    for i in range(n):
        a, b = poly[i], poly[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = math.hypot(dx, dy) or 1.0
        nrm = (dy / length, -dx / length, 0)
        out.append((nrm, ((a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1))))
        out.append((nrm, ((a[0], a[1], z0), (b[0], b[1], z1), (a[0], a[1], z1))))
    return out


def write_stl(path: Path, name: str, triangles) -> None:
    out = [f"solid {name}"]
    for (nx, ny, nz), verts in triangles:
        out.append(f"  facet normal {nx:.6e} {ny:.6e} {nz:.6e}")
        out.append("    outer loop")
        out += [f"      vertex {x:.6e} {y:.6e} {z:.6e}" for x, y, z in verts]
        out += ["    endloop", "  endfacet"]
    out.append(f"endsolid {name}\n")
    path.write_text("\n".join(out))


def lid_span(chip) -> tuple[float, float]:
    """(z_bottom, z_top) of the lid, from the layer stack: where a port hole must sit.

    A port's through-hole goes through the LID, so it spans the lid's own slice of the
    stack. It used to be drawn from the port's own z downwards, which is the same thing
    only while every port's z happens to be the top of the chip: three ports
    (m2_under_mesh_1, m3_backflush_1, m3_waste_1) carry z = 0 and so were exported as
    solids from z = -1.0 to 0.0, a hole in the air under the chip. The stack is the
    honest source: the lid is the last layer, and the chip's bottom face is z = 0.
    """
    total = sum(float(l["thickness_mm"]) for l in chip["layers"])
    lid = float(chip["layers"][-1]["thickness_mm"])
    return total - lid, total


# --- DXF geometry ---------------------------------------------------------------------

TOL = 1e-6


def clip_to_box(p0, p1, center, size):
    """Move p0 along p0->p1 until it leaves the axis-aligned box. Used to trim channels."""
    (cx, cy), (sx, sy) = center[:2], size[:2]
    x0, x1 = cx - sx / 2, cx + sx / 2
    y0, y1 = cy - sy / 2, cy + sy / 2
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


def rect_polygon(cx, cy, sx, sy):
    """An axis-aligned rectangle as four corners, counter-clockwise."""
    x0, x1 = cx - sx / 2, cx + sx / 2
    y0, y1 = cy - sy / 2, cy + sy / 2
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def rect_lines(cx, cy, sx, sy):
    return polygon_lines(rect_polygon(cx, cy, sx, sy))


def polygon_lines(poly):
    """A closed polygon as its edges, head to tail, last edge back to the first point."""
    return [(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]


def channel_polygon(a, b, width):
    """A channel as a rectangle of `width` running from point a to point b.

    Corner order is a+n, b+n, b-n, a-n, so edge 0 and edge 2 are the two long sides and
    the first and third corners of each end cap bracket the cap: operator/network.py
    reads the cap mid-points out of exactly that order.
    """
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = (dx * dx + dy * dy) ** 0.5
    if length < 1e-6:
        return []
    ux, uy = -dy / length * width / 2, dx / length * width / 2
    return [(a[0] + ux, a[1] + uy), (b[0] + ux, b[1] + uy),
            (b[0] - ux, b[1] - uy), (a[0] - ux, a[1] - uy)]


def channel_lines(a, b, width):
    """A channel as a rectangle of `width` running from point a to point b, as edges."""
    poly = channel_polygon(a, b, width)
    return polygon_lines(poly) if poly else []


def _side(p, point, normal):
    return (p[0] - point[0]) * normal[0] + (p[1] - point[1]) * normal[1]


def clip_polygon_outside(poly, point, normal):
    """The part of a convex polygon on the +normal side of the line through `point`.

    Sutherland-Hodgman against one half-plane, so a convex polygon stays convex. Used to
    cut a channel's end cap back to the chamber wall it crosses.
    """
    out = []
    for i in range(len(poly)):
        prv, cur = poly[i - 1], poly[i]
        sp, sc = _side(prv, point, normal), _side(cur, point, normal)
        if (sp >= -TOL) != (sc >= -TOL) and abs(sp - sc) > 1e-15:
            t = sp / (sp - sc)
            out.append((prv[0] + (cur[0] - prv[0]) * t, prv[1] + (cur[1] - prv[1]) * t))
        if sc >= -TOL:
            out.append(cur)
    return dedupe_polygon(out)


def dedupe_polygon(poly):
    out = []
    for p in poly:
        if not out or math.dist(p, out[-1]) > 1e-9:
            out.append(p)
    while len(out) > 1 and math.dist(out[0], out[-1]) <= 1e-9:
        out.pop()
    return out


def box_walls(center, size):
    """The four walls of an axis-aligned box as (point on the wall, outward normal)."""
    (cx, cy), (sx, sy) = center[:2], size[:2]
    x0, x1 = cx - sx / 2, cx + sx / 2
    y0, y1 = cy - sy / 2, cy + sy / 2
    return [((x0, cy), (-1.0, 0.0)), ((x1, cy), (1.0, 0.0)),
            ((cx, y0), (0.0, -1.0)), ((cx, y1), (0.0, 1.0))]


def trim_to_wall(poly, entry, away, center, size):
    """Cut `poly` back onto the wall of the box that its centreline left by.

    `entry` is where the centreline meets the box (from clip_to_box) and `away` points
    out of the box along the channel. The wall is the one `entry` sits on whose outward
    normal best agrees with `away` (two walls agree at a corner, hence the tie-break).
    Clipping to the outside of ONE wall already puts the whole polygon outside the whole
    box, so one clip is enough and the cap ends up lying ON that wall: the channel stops
    at the chamber wall, the two outlines meet edge to edge, and neither crosses the
    other.
    """
    if not poly:
        return poly
    walls = [(pt, n) for pt, n in box_walls(center, size)
             if abs(_side(entry, pt, n)) <= 1e-6]
    if not walls:
        return poly
    point, normal = max(walls, key=lambda w: w[1][0] * away[0] + w[1][1] * away[1])
    cut = clip_polygon_outside(poly, point, normal)
    return cut if len(cut) >= 3 and polygon_area(cut) > 1e-12 else poly


def polygon_area(poly):
    return abs(sum(poly[i][0] * poly[i - 1][1] - poly[i - 1][0] * poly[i][1]
                   for i in range(len(poly)))) / 2


def segments_cross(p1, p2, p3, p4):
    """True only for a transversal crossing: touching or collinear overlap is not one."""
    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)

    d1, d2 = orient(p3, p4, p1), orient(p3, p4, p2)
    d3, d4 = orient(p1, p2, p3), orient(p1, p2, p4)
    return d1 * d2 < 0 and d3 * d4 < 0


# --- port pads and channel centrelines: ONE implementation -----------------------------
#
# cad/make_figures.py (drawing + collision checks), tools/check_chip_spec.py and
# operator/network.py all import these, so the cut path, the drawing, the design checks and
# the hydraulic model read the same centreline and cannot disagree.
#
# THE PORT PAD. A port's bore is drilled through the LID; the channel runs in the fluidic
# layer below it. Clipping the channel at the edge of the bore's own square (what every tool
# did) leaves the 0.8 mm hole opening onto 0 mm2 of channel: the drill lands on solid plastic.
# An optional `pad` on the port (contracts/schemas/chip_spec.schema.json) is a cut feature in
# the FLUIDIC layer, centred under the bore and at least as big as it; the port's channels run
# into the pad and stop at its boundary, so the bore opens onto the pad and the pad onto the
# channels. A port without a pad is treated exactly as before, byte for byte.

PAD_ROUND_SEGMENTS = 64      # a round pad is cut as this regular polygon, inscribed in its circle


def pad_polygon(port) -> list | None:
    """The port's pad outline in plan view (counter-clockwise), or None when it has no pad.

    square: side size_mm, axis-aligned. round: diameter size_mm, as a regular
    PAD_ROUND_SEGMENTS-gon inscribed in that circle. Every tool uses this same polygon (the
    DXF, the STL, the clipping, the checks), so "round" never means two different things.
    """
    pad = port.get("pad")
    if not pad:
        return None
    x, y = port["position"][0], port["position"][1]
    size = float(pad["size_mm"])
    if pad.get("shape", "square") == "square":
        return rect_polygon(x, y, size, size)
    r = size / 2
    n = PAD_ROUND_SEGMENTS
    return [(x + r * math.cos(2 * math.pi * k / n), y + r * math.sin(2 * math.pi * k / n))
            for k in range(n)]


def port_pads(chip) -> dict:
    """port id -> pad polygon, for the ports that declare one."""
    return {p["id"]: poly for p in chip.get("ports", []) if (poly := pad_polygon(p))}


def anchors_of(chip) -> dict:
    """Every channel end: chambers as their boxes, ports as the bore's square (PORT_SIZE_MM)."""
    anchors = {c["id"]: (c["center"], c["size"]) for c in chip["chambers"]}
    for port in chip["ports"]:
        x, y, z = port["position"]
        anchors[port["id"]] = ([x, y, z], [PORT_SIZE_MM, PORT_SIZE_MM, 0])
    return anchors


def _inside_box(p, center, size, tol=1e-9) -> bool:
    return (abs(p[0] - center[0]) <= size[0] / 2 + tol and abs(p[1] - center[1]) <= size[1] / 2 + tol)


def inside_convex(p, poly, tol=1e-9) -> bool:
    """p inside or on a counter-clockwise convex polygon."""
    for i in range(len(poly)):
        a, b = poly[i - 1], poly[i]
        if (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) < -tol:
            return False
    return True


def ray_exit_convex(p, d, poly) -> float:
    """t >= 0 where p + t d leaves the counter-clockwise convex polygon (p inside it)."""
    best = math.inf
    for i in range(len(poly)):
        a, b = poly[i - 1], poly[i]
        nx, ny = b[1] - a[1], -(b[0] - a[0])            # outward normal of a CCW edge
        den = nx * d[0] + ny * d[1]
        if den > 1e-15:
            best = min(best, (nx * (a[0] - p[0]) + ny * (a[1] - p[1])) / den)
    return max(best, 0.0) if best < math.inf else 0.0


def clip_to_polygon(p0, p1, poly):
    """Move p0 (inside the convex polygon) along p0->p1 to where it leaves the polygon."""
    d = (p1[0] - p0[0], p1[1] - p0[1])
    t = min(ray_exit_convex(p0, d, poly), 1.0)
    return (p0[0] + d[0] * t, p0[1] + d[1] * t)


def _clip_path_start(pts, center, size, pad):
    """A path's first points, cut back to where it leaves the part at that end.

    A pad end is taken from the PORT CENTRE: points inside the pad are dropped and the
    channel starts where the ray from the pad centre to the first point outside it crosses
    the pad boundary. A box end (a chamber, or a bore with no pad) is cut where the path
    leaves the box; a path that starts outside its part is left as drawn (it does not reach
    it, and the checks say so).
    """
    if pad is not None:
        k = next((i for i, p in enumerate(pts) if not inside_convex(p, pad)), None)
        if k is None:
            return pts
        c = (center[0], center[1])
        return [clip_to_polygon(c, pts[k], pad)] + pts[k:]
    if not _inside_box(pts[0], center, size):
        return pts
    k = next((i for i, p in enumerate(pts) if not _inside_box(p, center, size)), None)
    if k is None:
        return pts
    return [clip_to_box(pts[k - 1], pts[k], center, size)] + pts[k:]


def channel_centreline(ch, anchors, pads) -> list | None:
    """The channel's cut centreline as a list of (x, y) points, or None if an end is unknown.

    With a "path" it follows the path, cut back at each end to the part it leaves (a
    chamber's box, the port's pad, or the bore's square). Without one it is the straight
    run between the two parts' centres, clipped at each end exactly as it always was.
    """
    a, b = anchors.get(ch["from"]), anchors.get(ch["to"])
    if a is None or b is None:
        return None
    pa, pb = pads.get(ch["from"]), pads.get(ch["to"])
    if ch.get("path"):
        pts = [(float(p[0]), float(p[1])) for p in ch["path"]]
        pts = _clip_path_start(pts, a[0], a[1], pa)
        pts = _clip_path_start(pts[::-1], b[0], b[1], pb)[::-1]
        return pts
    ca, cb = (a[0][0], a[0][1]), (b[0][0], b[0][1])
    p0 = clip_to_polygon(ca, cb, pa) if pa else clip_to_box(a[0][:2], b[0][:2], a[0], a[1])
    p1 = clip_to_polygon(cb, ca, pb) if pb else clip_to_box(b[0][:2], a[0][:2], b[0], b[1])
    return [p0, p1]


def centrelines(chip) -> list:
    """[(channel, [points])] for every channel whose two ends exist, in spec order."""
    anchors, pads = anchors_of(chip), port_pads(chip)
    out = []
    for ch in chip["channels"]:
        pts = channel_centreline(ch, anchors, pads)
        if pts is not None:
            out.append((ch, pts))
    return out


def _unit(p, q):
    dx, dy = q[0] - p[0], q[1] - p[1]
    n = math.hypot(dx, dy)
    return (dx / n, dy / n) if n > 1e-12 else (0.0, 0.0)


def _joint(prev, cur, nxt, half):
    """(left, right) mitred offsets of a polyline corner."""
    d1, d2 = _unit(prev, cur), _unit(cur, nxt)
    n1, n2 = (-d1[1], d1[0]), (-d2[1], d2[0])
    mx, my = n1[0] + n2[0], n1[1] + n2[1]
    m = math.hypot(mx, my)
    if m < 1e-9:
        mx, my, m = n1[0], n1[1], 1.0
    mx, my = mx / m, my / m
    s = half / max(mx * n1[0] + my * n1[1], 1e-6)
    return (cur[0] + mx * s, cur[1] + my * s), (cur[0] - mx * s, cur[1] - my * s)


def pad_cap(pad, c, u, half):
    """The stretch of a pad's boundary a channel of half-width `half` leaving the pad centre
    `c` in direction `u` opens onto, from its right edge to its left edge.

    That is the end cap of the channel's cut outline: the channel meets the pad edge to edge
    along the pad's own boundary, so under even-odd fill the junction stays open. Empty if
    the channel is wider than the pad (tools/check_chip_spec.py reports that).
    """
    n = (-u[1], u[0])
    right = (c[0] - n[0] * half, c[1] - n[1] * half)
    left = (c[0] + n[0] * half, c[1] + n[1] * half)
    if not (inside_convex(right, pad, -1e-12) and inside_convex(left, pad, -1e-12)):
        return []
    tr, tl = ray_exit_convex(right, u, pad), ray_exit_convex(left, u, pad)
    er = (right[0] + u[0] * tr, right[1] + u[1] * tr)
    el = (left[0] + u[0] * tl, left[1] + u[1] * tl)
    mid = []
    for v in pad:
        along = (v[0] - c[0]) * u[0] + (v[1] - c[1]) * u[1]
        across = (v[0] - c[0]) * n[0] + (v[1] - c[1]) * n[1]
        if along > 1e-9 and -half + 1e-9 < across < half - 1e-9:
            mid.append((across, v))
    return [er] + [v for _, v in sorted(mid)] + [el]


def _between(poly, start, stop):
    """The vertices of `poly` strictly after `start` and before `stop`, walking forward."""
    def find(p):
        return next((i for i, q in enumerate(poly) if math.dist(p, q) < 1e-9), None)
    i, j = find(start), find(stop)
    if i is None or j is None:
        return None
    out, k = [], (i + 1) % len(poly)
    while k != j:
        out.append(poly[k])
        k = (k + 1) % len(poly)
    return out


def channel_outline(pts, width, start=None, end=None):
    """A centreline polyline as one closed outline: mitred at the bends.

    start / end: ("box", center, size) for an end trimmed onto a chamber wall,
    ("pad", polygon, centre) for an end that meets a port pad along its boundary, or None
    for a square cap.
    """
    half = width / 2
    first = channel_polygon(pts[0], pts[1], width)
    last = channel_polygon(pts[-2], pts[-1], width)
    if not first or not last:
        return []
    chain_s = [first[3], first[0]]
    if start and start[0] == "pad":
        cap = pad_cap(start[1], start[2], _unit(start[2], pts[0]) if math.dist(start[2], pts[0]) > 1e-12
                      else _unit(pts[0], pts[1]), half)
        chain_s = cap or chain_s
    elif start and start[0] == "box":
        cut = trim_to_wall(first, pts[0], _unit(pts[0], pts[1]), start[1], start[2])
        chain_s = _between(cut, first[2], first[1]) or chain_s
    chain_e = [last[1], last[2]]
    if end and end[0] == "pad":
        cap = pad_cap(end[1], end[2], _unit(end[2], pts[-1]) if math.dist(end[2], pts[-1]) > 1e-12
                      else _unit(pts[-1], pts[-2]), half)
        chain_e = cap or chain_e
    elif end and end[0] == "box":
        cut = trim_to_wall(last, pts[-1], _unit(pts[-1], pts[-2]), end[1], end[2])
        chain_e = _between(cut, last[0], last[3]) or chain_e
    joints = [_joint(pts[i - 1], pts[i], pts[i + 1], half) for i in range(1, len(pts) - 1)]
    return dedupe_polygon(chain_s + [j[0] for j in joints] + chain_e + [j[1] for j in joints[::-1]])


def segment_rects(pts, width):
    """The reference drawing of a centreline: one full rectangle per straight segment."""
    return [r for a, b in zip(pts, pts[1:]) if (r := channel_polygon(a, b, width))]


def channel_cut(ch, pts, chip_chambers, pads, anchors):
    """(reference rectangles, cut outline) for one channel.

    A straight channel between two parts with no pad is cut exactly as it always was
    (full rectangle, end caps trimmed onto the chamber walls), so a chip that declares no
    pad and no path exports the same bytes as before.
    """
    w = ch["width_mm"]
    pa, pb = pads.get(ch["from"]), pads.get(ch["to"])
    if len(pts) == 2 and pa is None and pb is None:
        full = channel_polygon(pts[0], pts[1], w)
        if not full:
            return [], []
        span = (pts[1][0] - pts[0][0], pts[1][1] - pts[0][1])
        cut = full
        if ch["from"] in chip_chambers:
            a = anchors[ch["from"]]
            cut = trim_to_wall(cut, pts[0], span, a[0], a[1])
        if ch["to"] in chip_chambers:
            b = anchors[ch["to"]]
            cut = trim_to_wall(cut, pts[1], (-span[0], -span[1]), b[0], b[1])
        if len(cut) < 3 or polygon_area(cut) < 1e-9:
            print(f"  WARNING: channel '{ch['id']}' vanishes when trimmed to its chambers' "
                  f"walls (it is shorter than its own width): left untrimmed")
            cut = full
        return [full], cut

    def end(pid, pad):
        if pad is not None:
            pos = anchors[pid][0]
            return ("pad", pad, (pos[0], pos[1]))
        if pid in chip_chambers:
            return ("box", anchors[pid][0], anchors[pid][1])
        return None
    rects = segment_rects(pts, w)
    if not rects:
        return [], []
    cut = channel_outline(pts, w, end(ch["from"], pa), end(ch["to"], pb))
    if len(cut) < 3 or polygon_area(cut) < 1e-9:
        print(f"  WARNING: channel '{ch['id']}' vanishes when trimmed to its ends: left untrimmed")
        cut = channel_outline(pts, w)
    return rects, cut


def polygon_distance(a, b) -> float:
    """Shortest gap between two simple polygons in plan view; 0 when they touch or overlap."""
    if not a or not b:
        return math.inf
    ea, eb = polygon_lines(a), polygon_lines(b)
    for s in ea:
        for t in eb:
            if segments_cross(s[0], s[1], t[0], t[1]):
                return 0.0

    def inside(p, poly):
        hit = False
        for (x1, y1), (x2, y2) in polygon_lines(poly):
            if (y1 > p[1]) != (y2 > p[1]) and p[0] < (x2 - x1) * (p[1] - y1) / (y2 - y1) + x1:
                hit = not hit
        return hit
    if inside(a[0], b) or inside(b[0], a):
        return 0.0

    def pseg(p, s):
        (ax, ay), (bx, by) = s
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 < 1e-18 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
        return math.hypot(p[0] - ax - t * dx, p[1] - ay - t * dy)
    return min(min(pseg(p, s) for p in a for s in eb), min(pseg(p, s) for p in b for s in ea))


# --- DXF files ------------------------------------------------------------------------

INSUNITS_MM = 4          # DXF $INSUNITS: 4 = millimetres
LAYER_COLOUR = {"CHAMBERS": 5, "CHANNELS": 3, "GROOVES": 1, "PADS": 4}


class _Dxf:
    """A minimal AC1015 DXF writer: HEADER (with units), TABLES, ENTITIES.

    The units are the point. Written without a HEADER section these files declared
    nothing, and a CAM importer that defaults to inches scales the part by 25.4: a 30 mm
    chip arrives 762 mm wide and every feature is 25x too big. $INSUNITS = 4 says
    millimetres; $MEASUREMENT = 1 says metric for the readers that look there instead.
    """

    def __init__(self, layers):
        self.out: list[str] = []
        self.handle = 0x100
        self._header()
        self._tables(layers)
        self.out += ["0", "SECTION", "2", "ENTITIES"]

    def _pair(self, code, value):
        self.out += [str(code), str(value)]

    def _next_handle(self) -> str:
        self.handle += 1
        return f"{self.handle:X}"

    def _header(self):
        self.out += ["0", "SECTION", "2", "HEADER"]
        for var, code, value in (("$ACADVER", 1, "AC1015"),
                                 ("$HANDSEED", 5, "FFFF"),
                                 ("$INSUNITS", 70, INSUNITS_MM),
                                 ("$MEASUREMENT", 70, 1),
                                 ("$LUNITS", 70, 2)):
            self._pair(9, var)
            self._pair(code, value)
        self.out += ["0", "ENDSEC"]

    def _tables(self, layers):
        self.out += ["0", "SECTION", "2", "TABLES"]
        # CONTINUOUS has to exist before a layer can name it as its linetype.
        self.out += ["0", "TABLE", "2", "LTYPE", "5", self._next_handle(),
                     "100", "AcDbSymbolTable", "70", "1",
                     "0", "LTYPE", "5", self._next_handle(),
                     "100", "AcDbSymbolTableRecord", "100", "AcDbLinetypeTableRecord",
                     "2", "CONTINUOUS", "70", "0", "3", "Solid line", "72", "65",
                     "73", "0", "40", "0.0",
                     "0", "ENDTAB"]
        self.out += ["0", "TABLE", "2", "LAYER", "5", self._next_handle(),
                     "100", "AcDbSymbolTable", "70", str(len(layers))]
        for name in layers:
            self.out += ["0", "LAYER", "5", self._next_handle(),
                         "100", "AcDbSymbolTableRecord", "100", "AcDbLayerTableRecord",
                         "2", name, "70", "0",
                         "62", str(LAYER_COLOUR.get(name, 7)), "6", "CONTINUOUS"]
        self.out += ["0", "ENDTAB", "0", "ENDSEC"]

    def line(self, layer, a, b):
        self.out += ["0", "LINE", "8", layer,
                     "10", f"{a[0]:.4f}", "20", f"{a[1]:.4f}", "30", "0.0",
                     "11", f"{b[0]:.4f}", "21", f"{b[1]:.4f}", "31", "0.0"]

    def closed_polyline(self, layer, poly):
        self.out += ["0", "LWPOLYLINE", "5", self._next_handle(),
                     "100", "AcDbEntity", "8", layer,
                     "100", "AcDbPolyline", "90", str(len(poly)), "70", "1"]
        for x, y in poly:
            self.out += ["10", f"{x:.4f}", "20", f"{y:.4f}"]

    def write(self, path: Path):
        path.write_text("\n".join(self.out + ["0", "ENDSEC", "0", "EOF", ""]))


def write_dxf(path: Path, layers: dict[str, list]) -> None:
    """The reference drawing: every outline as LINE entities, in millimetres.

    Entity-for-entity the same geometry this script has always written (4 LINEs per
    channel rectangle, in order, on layer CHANNELS: operator/network.py and
    twin/network.js read their channel lengths out of that), now inside a real DXF that
    says what its units are.
    """
    dxf = _Dxf(list(layers))
    for layer, lines in layers.items():
        for a, b in lines:
            dxf.line(layer, a, b)
    dxf.write(path)


def write_dxf_closed(path: Path, layers: dict[str, list]) -> None:
    """The fabrication file: every outline as ONE closed LWPOLYLINE, in millimetres."""
    dxf = _Dxf(list(layers))
    for layer, polys in layers.items():
        for poly in polys:
            dxf.closed_polyline(layer, poly)
    dxf.write(path)


def outline_crossings(closed: dict[str, list]) -> list[str]:
    """Layer pairs whose closed outlines still cross. Should always be empty.

    Only across layers: the chamber outlines against the channel outlines. That is the
    pair that matters, because a crossing there is what voided the junction under
    even-odd fill. Two channels overlapping each other inside one layer is a plan-view
    short circuit, which cad/make_figures.py is the tool for.
    """
    bad = []
    names = list(closed)
    for i, la in enumerate(names):
        for lb in names[i + 1:]:
            n = sum(1 for pa in closed[la] for pb in closed[lb]
                    for sa in polygon_lines(pa) for sb in polygon_lines(pb)
                    if segments_cross(sa[0], sa[1], sb[0], sb[1]))
            if n:
                bad.append(f"{la}/{lb} {n}")
    return bad


# --- main -----------------------------------------------------------------------------

OWNER_FILE = ".chip_id"
RETIRED_FILE = "RETIRED.md"


def check_owner(out: Path, chip_id: str) -> str | None:
    """None if `out` is free or already this chip's, else a message naming the other owner."""
    marker = out / OWNER_FILE
    if marker.exists():
        owner = marker.read_text().strip()
        if owner != chip_id:
            return f"{out} already belongs to chip '{owner}'"
    return None


def duplicate_ids(chip) -> list[str]:
    """Part files are named by bare id, so two parts with one id overwrite each other."""
    seen, dup = set(), []
    for part in [c["id"] for c in chip["chambers"]] + [p["id"] for p in chip["ports"]]:
        if part in seen:
            dup.append(part)
        seen.add(part)
    return dup


def in_envelope(chip) -> bool:
    """True for a chip inside the resin-print + hot-emboss build envelope (contracts/blocks.json)."""
    try:
        env = json.loads(BLOCKS.read_text())["envelope"]
    except (OSError, KeyError, ValueError):
        return False
    deep = max([c["size"][2] for c in chip["chambers"]] + [c["depth_mm"] for c in chip["channels"]])
    return (chip["footprint"]["x"] <= env["max_x_mm"] and chip["footprint"]["y"] <= env["max_y_mm"]
            and deep <= env["max_depth_mm"] + 1e-9)


def depth_problems(chip, spacer) -> list[str]:
    """Features that are not at the fluidic layer's depth: one embossing depth only."""
    if spacer is None:
        return [f"no layer is {chip['chambers'][0]['size'][2]:g} mm thick, so no layer can "
                f"hold the chambers"]
    t = spacer["thickness_mm"]
    out = []
    for c in chip["chambers"]:
        if abs(c["size"][2] - t) > 1e-6:
            out.append(f"chamber '{c['id']}' is {c['size'][2] * 1000:g} um deep, layer is "
                       f"{t * 1000:g} um")
    for c in chip["channels"]:
        if abs(c["depth_mm"] - t) > 1e-6:
            out.append(f"channel '{c['id']}' is {c['depth_mm'] * 1000:g} um deep, layer is "
                       f"{t * 1000:g} um")
    return out


def retired_note(chip_id: str) -> str:
    return (f"# RETIRED: {chip_id}\n\n"
            f"**Do not fabricate anything in this folder.**\n\n"
            f"`{chip_id}` is {RETIRED[chip_id]}.\n\n"
            f"The parts here are kept for reference only. They are regenerated so they stay\n"
            f"consistent with the spec they came from, not because the chip is live. Which chip\n"
            f"does which job: `protocols/pipeline_status.md` and `cad/README.md`.\n\n"
            f"Written by `cad/make_train_cad.py`; don't edit by hand.\n")


def chip_specs() -> list[Path]:
    """Every chip spec in the repo: chip/*.json plus the generated cad/*_spec.json."""
    return sorted((ROOT / "chip").glob("*.json")) + sorted((ROOT / "cad").glob("*_spec.json"))


def frozen_chips() -> dict[str, str]:
    """chip id -> why, for every chip printed for the demo (built_for_demo).

    Two sources, unioned: tools/build_assay_variants.py PRINTED_FOR_DEMO (the list the project
    lead confirmed, which is what sets the flags) and every pipeline module in
    protocols/pipeline*.json flagged built_for_demo. The constant is read as data with ast,
    not imported, so this script stays standard-library and side-effect free. module6_v1 is
    only in the constant: its pipeline entry is a roadmap branch with no chip path.
    """
    import ast
    out: dict[str, str] = {}
    builder = ROOT / "tools" / "build_assay_variants.py"
    try:
        for node in ast.parse(builder.read_text()).body:
            if (isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "PRINTED_FOR_DEMO"
                                                     for t in node.targets)):
                for cid in ast.literal_eval(node.value):
                    out[cid] = "tools/build_assay_variants.py PRINTED_FOR_DEMO"
    except (OSError, SyntaxError, ValueError):
        pass
    for path in sorted((ROOT / "protocols").glob("pipeline*.json")):
        try:
            modules = json.loads(path.read_text()).get("modules", [])
        except (OSError, ValueError):
            continue
        for m in modules:
            if m.get("built_for_demo") and m.get("chip"):
                out.setdefault(Path(m["chip"]).stem, f"{path.relative_to(ROOT).as_posix()} built_for_demo")
    return out


def build(spec_path: Path, force_frozen: bool = False, exports: Path | None = None) -> int:
    """Write one chip's parts. Returns 0 (written), 2 (collision or crossing) or 3 (refused,
    frozen). `exports` redirects the output root (tests regenerate into a scratch folder)."""
    chip = json.loads(spec_path.read_text())
    if not all(k in chip for k in ("id", "chambers", "ports", "channels", "layers")):
        print(f"{spec_path.name}: skipped, not a chip spec "
              f"(no id/chambers/ports/channels/layers): nothing written")
        return 0
    # PRINTED CHIPS ARE FROZEN (AGENTS.md). Their exports are what went to the printer;
    # regenerating them with a newer generator (one that honours channel paths, say) would
    # silently put a different chip on disk than the one on the bench.
    # Only the real cad/exports/ is protected: tools/check_asbuilt.py and the tests regenerate
    # into a scratch folder (exports=, or a patched EXPORTS) to compare against it.
    frozen = frozen_chips()
    real = (exports or EXPORTS).resolve() == (ROOT / "cad" / "exports").resolve()
    if chip["id"] in frozen and not force_frozen and real:
        print(f"FROZEN: {chip['id']} is printed for the demo ({frozen[chip['id']]}), so its "
              f"exports are the as-printed record and were NOT rewritten. Make a new versioned "
              f"spec instead; --force-frozen overwrites them anyway (only to prove they are "
              f"byte-identical).")
        return 3
    # Part files are named by bare id, and ids repeat across chips: both of ours have an
    # 'inlet_1'. twin/main.js loads cad/exports/ flat, so the flat namespace can only
    # hold one chip, and it holds the default one. Every other chip gets its own folder.
    is_default = spec_path.resolve() == DEFAULT_CHIP.resolve()
    root = exports or EXPORTS
    out = root if is_default else root / chip["id"]
    dup = duplicate_ids(chip)
    clash = check_owner(out, chip["id"])
    if dup or clash:
        for d in dup:
            print(f"COLLISION: id '{d}' is used twice in {chip['id']}, one STL would "
                  f"overwrite the other")
        if clash:
            print(f"COLLISION: {clash}, so '{chip['id']}' was NOT written")
        return 2
    out.mkdir(parents=True, exist_ok=True)
    (out / OWNER_FILE).write_text(chip["id"] + "\n")
    rel = "cad/exports" if is_default else f"cad/exports/{chip['id']}"
    by_id = {c["id"]: c for c in chip["chambers"]}
    written = []

    for ch in chip["chambers"]:
        dest = out / f"{ch['id']}.stl"
        write_stl(dest, ch["id"], box_triangles(ch["center"], ch["size"]))
        written.append(dest)

    # A port is a through-hole in the LID, so it spans the lid's slice of the layer
    # stack, whatever z the spec happens to give the port itself.
    lid_z0, lid_z1 = lid_span(chip)
    lid = lid_z1 - lid_z0
    misplaced = []
    for port in chip["ports"]:
        x, y, z = port["position"]
        if abs(z - lid_z1) > 1e-6:
            misplaced.append((port["id"], z))
        dest = out / f"{port['id']}.stl"
        write_stl(dest, port["id"],
                  box_triangles([x, y, (lid_z0 + lid_z1) / 2], [PORT_SIZE_MM, PORT_SIZE_MM, lid]))
        written.append(dest)

    anchors = anchors_of(chip)
    pads = port_pads(chip)

    # The spacer is the layer whose thickness sets the chamber height.
    depth = chip["chambers"][0]["size"][2]
    spacer = next((l for l in chip["layers"] if abs(l["thickness_mm"] - depth) < 1e-6), None)

    # A pad is cut in the fluidic layer, so its solid sits at the chambers' own z and depth.
    for pid, poly in pads.items():
        z_mid = chip["chambers"][0]["center"][2]
        dest = out / f"{pid}_pad.stl"
        write_stl(dest, f"{pid}_pad", prism_triangles(poly, z_mid - depth / 2, z_mid + depth / 2))
        written.append(dest)

    chamber_lines, channel_lines_out, groove_lines = [], [], []
    chamber_polys, channel_polys, groove_polys = [], [], []
    for ch in chip["chambers"]:
        poly = rect_polygon(ch["center"][0], ch["center"][1], ch["size"][0], ch["size"][1])
        chamber_polys.append(poly)
        chamber_lines += polygon_lines(poly)
    for ch, pts in centrelines(chip):
        # The cut path: trim each end onto the wall of the CHAMBER it enters, or onto the
        # boundary of its port's PAD, so the channel stops there instead of straddling it.
        # A port with no pad is a hole in the lid, not an outline in this drawing, so there
        # is nothing to meet there and its end cap is left square.
        rects, cut = channel_cut(ch, pts, by_id, pads, anchors)
        if not rects:
            continue
        lines = [ln for r in rects for ln in polygon_lines(r)]
        # A channel shallower than the fluidic layer is a second depth, which one
        # embossing tool cannot make. It goes on its own layer so it cannot be missed.
        if spacer and ch["depth_mm"] < spacer["thickness_mm"] - 1e-6:
            groove_lines += lines
            groove_polys.append(cut)
        else:
            channel_lines_out += lines
            channel_polys.append(cut)

    layers = {"CHAMBERS": chamber_lines, "CHANNELS": channel_lines_out}
    closed = {"CHAMBERS": chamber_polys, "CHANNELS": channel_polys}
    if groove_lines:
        layers["GROOVES"] = groove_lines
        closed["GROOVES"] = groove_polys
    # Port pads: their own layer, written only when a chip declares one, so a chip without
    # pads keeps exactly the layer table it always had.
    if pads:
        layers["PADS"] = [ln for poly in pads.values() for ln in polygon_lines(poly)]
        closed["PADS"] = list(pads.values())

    dxf = out / f"{chip['id']}.dxf"
    write_dxf(dxf, layers)
    written.append(dxf)
    dxf_cut = out / f"{chip['id']}_closed.dxf"
    write_dxf_closed(dxf_cut, closed)
    written.append(dxf_cut)
    crossings = outline_crossings(closed)

    note = out / RETIRED_FILE
    if chip["id"] in RETIRED:
        note.write_text(retired_note(chip["id"]))
        written.append(note)
    elif note.exists():
        note.unlink()

    # Parts left over from an older revision of this chip. In the chip's own folder they
    # are deleted: an orphan STL is a part a fabricator makes by mistake. The flat folder
    # is shared with the twin, so there they are only reported. <chip_id>.stl is spared:
    # that name means a one-piece print of the whole chip (tools/validate.py allows it,
    # and cad/exports/hello_world_plate/hello_world_plate.stl is one), which this script
    # does not write and must not delete.
    # <chip_id>_asbuilt.dxf is tools/check_asbuilt.py's output, not a stale part of ours.
    wanted = ({p.name for p in written}
              | {f"{chip['id']}.stl", f"{chip['id']}.glb", f"{chip['id']}_asbuilt.dxf"})
    stale = sorted(p for p in out.glob("*") if p.name not in wanted
                   and p.suffix.lower() in (".stl", ".dxf"))
    owned_flat = out == root
    removed = []
    if stale and not owned_flat:
        for p in stale:
            p.unlink()
            removed.append(p.name)
    problems = depth_problems(chip, spacer)

    print(f"chip {chip['id']}: {len(by_id)} chambers, {len(chip['ports'])} ports, "
          f"{len(chip['channels'])} channels")
    for p in written:
        print(f"  wrote {rel}/{p.name}")
    if spacer:
        how = ": resin-printed tool, hot-embossed" if in_envelope(chip) else ""
        print(f"\n{dxf.name}: CHAMBERS and CHANNELS are the outline of the '{spacer['id']}' "
              f"fluidic layer ({spacer['thickness_mm']} mm {spacer['material']}){how}. "
              f"Units mm ($INSUNITS 4), chip-spec origin.")
    print(f"  {dxf_cut.name} is the file to MACHINE: "
          f"{sum(len(v) for v in closed.values())} closed polylines "
          f"({', '.join(f'{k} {len(v)}' for k, v in closed.items())}), channels trimmed to the "
          f"chamber walls, {'NO' if not crossings else 'STILL'} outline crossings.")
    if crossings:
        print(f"  CROSSING: {'; '.join(crossings)}: two outlines still cross, which voids the "
              f"junction under even-odd fill. A channel probably runs through a chamber it is "
              f"not connected to (a short circuit): run cad/make_figures.py.")
    if misplaced:
        print(f"  PORT z: {', '.join(f'{i} (spec z={z:g})' for i, z in misplaced)} "
              f"not at the top of the stack; the through-hole was drawn in the lid "
              f"({lid_z0:g} to {lid_z1:g} mm) regardless.")
    if groove_polys:
        shallow = min(c["depth_mm"] for c in chip["channels"]) * 1000
        print(f"  GROOVES layer written: BUILD PROBLEM. Its shallowest feature is {shallow:g} um, "
              f"a second\n  depth. We have no photolithography and one embossing tool makes one "
              f"depth, so this\n  chip cannot be built as drawn. Re-draw it at one depth.")
    for pr in problems:
        print(f"  DEPTH: {pr}")
    if chip["id"] in RETIRED:
        print(f"  RETIRED: {rel}/{RETIRED_FILE} written: {RETIRED[chip['id']]}")
    if removed:
        print(f"  REMOVED: {out} held parts this chip no longer has: {', '.join(removed)}")
    if stale and owned_flat:
        print(f"  STALE: {out} holds parts this chip no longer has: "
              f"{', '.join(p.name for p in stale)} (the flat folder is the twin's, check them)")
    return 2 if crossings else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chip", type=Path, default=None)
    ap.add_argument("--all", action="store_true",
                    help="every chip spec in chip/ and cad/*_spec.json")
    ap.add_argument("--force-frozen", action="store_true",
                    help="also overwrite the exports of chips printed for the demo (built_for_demo)")
    args = ap.parse_args()
    if args.all and args.chip:
        ap.error("--all and --chip are exclusive")
    specs = chip_specs() if args.all else [args.chip or DEFAULT_CHIP]
    worst = 0
    for i, spec in enumerate(specs):
        if i:
            print()
        code = build(spec, force_frozen=args.force_frozen)
        # In --all a frozen chip is skipped on purpose, not a failure; asked for by name it is.
        worst = max(worst, 0 if (code == 3 and args.all) else code)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())

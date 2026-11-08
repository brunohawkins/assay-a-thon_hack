"""A software renderer: two SVGs so a person can SEE the part before it is printed.

Standard library only - no numpy, no matplotlib, no headless GL. Painter's algorithm with
flat Lambert shading, which is all a one-depth plate needs.

    render_top(...)   orthographic from +z: the cut region shaded, every chamber and port
                      labelled. True 1:1, so a ruler on the screen is a ruler on the chip.
    render_iso(...)   isometric, z exaggerated, so the channels and chambers read as real
                      pockets rather than as a drawing.

WHY Z IS EXAGGERATED, AND WHY IT IS LABELLED. The chip is 0.8 mm thick across 30 mm, a
1:37 aspect: drawn true to scale the pockets are a third of a pixel deep and the render
shows a blank rectangle, which is worse than no render. The exaggeration factor is
`cad/fabrication.json` `display_scale` - the number cad/export_display_model.py already
uses for exactly this reason - and it is printed on the image, next to the real depth, so
nobody can read the picture as the part's proportions.

Both images carry a SCALE BAR in the part's own frame (the iso bar lies on the chip's
base plane and is projected with it, so it is foreshortened exactly as the part is), and
both carry the DEMO-vs-TARGET banner, because a 30 x 20 mm test article must never be
mistaken for the 127.76 x 85.48 mm device.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib.io import write_text  # noqa: E402  atomic: a crash leaves no half SVG

from . import geometry  # noqa: E402

FABRICATION = ROOT / "cad" / "fabrication.json"

# Palette. Pale COC body, a saturated channel colour so the fluid path is unmistakable.
BODY = (214, 217, 222)
FLOOR = (38, 120, 158)        # the etched floor: where fluid goes
WALL = (150, 156, 164)
INK = "#1d2126"
MUTED = "#5d646d"
PAPER = "#fbfcfd"
WARN = "#9a3412"

LIGHT = (-0.35, -0.50, 0.79)   # from the upper left front
AMBIENT = 0.42
AZIMUTH_DEG = -40.0
ELEVATION_DEG = 50.0           # steep enough to see the floor of a pocket, not just its wall
VISIBLE_ASPECT = 0.5           # drawn depth, as a fraction of the narrowest pocket's width


def display_scale() -> float:
    """cad/fabrication.json `display_scale`: the CEILING on how much the view exaggerates z."""
    return float(json.loads(FABRICATION.read_text())["display_scale"])


def narrowest_cut_mm(geom: dict) -> float:
    """Width of the narrowest pocket in plan - a 0.2 mm vent channel, usually."""
    widths = [f["r"] * 2 if f["kind"] == "cyl" else min(f["len"], f["wid"])
              for f in geom["plan"]["features"]]
    return min(widths) if widths else 1.0


def z_exaggeration_for(geom: dict) -> float:
    """How much to stretch z, picked from the geometry rather than fixed at display_scale.

    A flat plate drawn true to scale shows nothing, but over-stretching is worse: at the
    10x of cad/fabrication.json a 0.3 mm pocket becomes 3 mm deep while staying 0.2-1.2 mm
    wide, so from any viewing angle you see its far WALL and never its floor, and the
    channels read as black slots. Stretching only until the drawn depth is half the
    narrowest pocket's width keeps every floor in view at this elevation, which is what
    makes the fluid path legible. Clamped to [1, display_scale] and printed on the image.
    """
    depth = geom["max_depth_mm"] or 1.0
    want = VISIBLE_ASPECT * narrowest_cut_mm(geom) / depth
    return max(1.0, min(display_scale(), want))


# --- a light mesh for drawing only ------------------------------------------------------

def display_mesh(xs: list[float], ys: list[float], tops: list[list[float]],
                 z0: float, floor_z: float) -> list[tuple[tuple, str]]:
    """Triangles to draw, each tagged 'top' | 'floor' | 'wall'.

    Cells of equal height are merged along x into runs. That leaves T-vertices, which is
    fine for a picture and fatal for an STL - which is why this is a SEPARATE mesh from
    mesh.heightfield() and is never written to a file.
    """
    nx, ny = len(xs) - 1, len(ys) - 1
    tris: list[tuple[tuple, str]] = []

    def quad(a, b, c, d, role):
        tris.append(((a, b, c), role))
        tris.append(((a, c, d), role))

    for j in range(ny):
        y0, y1 = ys[j], ys[j + 1]
        i = 0
        while i < nx:
            k = i
            t = tops[j][i]
            while k + 1 < nx and abs(tops[j][k + 1] - t) <= 1e-9:
                k += 1
            x0, x1 = xs[i], xs[k + 1]
            role = "floor" if t <= floor_z + 1e-9 else "top"
            quad((x0, y0, t), (x1, y0, t), (x1, y1, t), (x0, y1, t), role)
            quad((x0, y0, z0), (x0, y1, z0), (x1, y1, z0), (x1, y0, z0), "wall")
            i = k + 1

    for j in range(ny):
        y0, y1 = ys[j], ys[j + 1]
        for i in range(nx + 1):
            x = xs[i]
            left = tops[j][i - 1] if i > 0 else None
            right = tops[j][i] if i < nx else None
            if left is None:
                quad((x, y0, z0), (x, y0, right), (x, y1, right), (x, y1, z0), "wall")
            elif right is None:
                quad((x, y0, z0), (x, y1, z0), (x, y1, left), (x, y0, left), "wall")
            elif abs(left - right) > 1e-9:
                lo, hi = min(left, right), max(left, right)
                if left > right:
                    quad((x, y0, lo), (x, y1, lo), (x, y1, hi), (x, y0, hi), "wall")
                else:
                    quad((x, y0, lo), (x, y0, hi), (x, y1, hi), (x, y1, lo), "wall")

    for i in range(nx):
        x0, x1 = xs[i], xs[i + 1]
        for j in range(ny + 1):
            y = ys[j]
            below = tops[j - 1][i] if j > 0 else None
            above = tops[j][i] if j < ny else None
            if below is None:
                quad((x0, y, z0), (x1, y, z0), (x1, y, above), (x0, y, above), "wall")
            elif above is None:
                quad((x0, y, z0), (x0, y, below), (x1, y, below), (x1, y, z0), "wall")
            elif abs(below - above) > 1e-9:
                lo, hi = min(below, above), max(below, above)
                if below > above:
                    quad((x0, y, lo), (x0, y, hi), (x1, y, hi), (x1, y, lo), "wall")
                else:
                    quad((x0, y, lo), (x1, y, lo), (x1, y, hi), (x0, y, hi), "wall")
    return tris


# --- maths ------------------------------------------------------------------------------

def _norm(v):
    n = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / n for c in v)


def _camera(azimuth_deg: float, elevation_deg: float):
    az, el = math.radians(azimuth_deg), math.radians(elevation_deg)
    w = _norm((math.cos(el) * math.cos(az), math.cos(el) * math.sin(az), math.sin(el)))
    r = _norm((-w[1], w[0], 0.0))
    u = (w[1] * r[2] - w[2] * r[1], w[2] * r[0] - w[0] * r[2], w[0] * r[1] - w[1] * r[0])
    return r, u, w


def _shade(rgb, n, flip: bool):
    if flip:
        n = (-n[0], -n[1], -n[2])
    lit = AMBIENT + (1 - AMBIENT) * max(0.0, sum(a * b for a, b in zip(n, _norm(LIGHT))))
    return "#%02x%02x%02x" % tuple(min(255, max(0, int(c * lit))) for c in rgb)


def _scale_bar_mm(span: float) -> float:
    """A round bar length under a third of the part: 1, 2, 5, 10, 20, 50 mm."""
    for v in (50.0, 20.0, 10.0, 5.0, 2.0, 1.0, 0.5):
        if v <= span / 3:
            return v
    return span / 3


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


WRAP_CHARS = 104        # caption line length before it is wrapped


def _wrap(text: str, width: int = WRAP_CHARS) -> list:
    """Greedy word wrap. The scale warning is a whole sentence and must never be clipped:
    'NOT the device' falling off the right-hand edge is exactly the failure it exists to
    prevent."""
    words, lines, line = str(text).split(), [], ""
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        lines.append(line)
    return lines or [""]


def _text_width(lines: list, font_px: float) -> float:
    """Roughly how wide the caption will be, so the canvas is never narrower than its own
    text. Helvetica's average advance is about 0.55 em; 0.6 leaves a little slack."""
    longest = max((len(s) for group in lines for s in _wrap(group)), default=0)
    return longest * font_px * 0.6


def _titleblock_height(lines: list, banner: str, top: float = 26.0) -> float:
    """How much headroom the wrapped caption needs, so it never overlaps the drawing."""
    n = sum(len(_wrap(g)) for g in lines) + len(_wrap(banner))
    return top + 17 + n * 13 + 16


def _titleblock(out: list[str], x: float, y: float, title: str, lines: list[str],
                banner: str) -> float:
    out.append(f'<text x="{x:.1f}" y="{y:.1f}" font-size="15" font-weight="600" '
               f'fill="{INK}" font-family="Helvetica,Arial,sans-serif">{_esc(title)}</text>')
    yy = y + 17
    for line in [w for group in lines for w in _wrap(group)]:
        out.append(f'<text x="{x:.1f}" y="{yy:.1f}" font-size="10.5" fill="{MUTED}" '
                   f'font-family="Helvetica,Arial,sans-serif">{_esc(line)}</text>')
        yy += 13
    yy += 3
    for line in _wrap(banner):
        out.append(f'<text x="{x:.1f}" y="{yy:.1f}" font-size="10.5" font-weight="600" '
                   f'fill="{WARN}" font-family="Helvetica,Arial,sans-serif">'
                   f'{_esc(line)}</text>')
        yy += 13
    return yy


# --- top view ---------------------------------------------------------------------------

def render_top(path: Path, spec: dict, geom: dict, scale_note: str,
               px_per_mm: float = 22.0) -> dict:
    """Orthographic plan view, true 1:1 in the part's own millimetres."""
    fx, fy = geom["plan"]["fx"], geom["plan"]["fy"]
    pad_l, pad_r, pad_b = 54, 30, 86
    depth = geom["max_depth_mm"]
    caption = [
        f"footprint {fx:g} x {fy:g} mm  \u00b7  one fluidic layer, {depth * 1000:g} um deep",
        f"{len(spec.get('chambers', []))} chambers, {len(spec.get('channels', []))} channels, "
        f"{len(spec.get('ports', []))} ports  \u00b7  cut area {geom['cut_area_mm2']:.1f} mm2  "
        f"\u00b7  blue is cut away (the fluid path), grey is solid chip",
    ]
    # Never narrower than its own caption: the scale banner is a whole sentence and
    # clipping the words "NOT the device" off the right-hand edge defeats the point of it.
    pad_t = _titleblock_height(caption, scale_note)
    w = max(fx * px_per_mm + pad_l + pad_r,
            _text_width(caption + [scale_note], 10.5) + pad_l + pad_r)
    h = fy * px_per_mm + pad_t + pad_b

    def X(mx: float) -> float:
        return pad_l + mx * px_per_mm

    def Y(my: float) -> float:
        return pad_t + (fy - my) * px_per_mm      # y up in the part, down in SVG

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
           f'viewBox="0 0 {w:.0f} {h:.0f}">',
           f'<rect width="{w:.0f}" height="{h:.0f}" fill="{PAPER}"/>']
    _titleblock(out, pad_l, 26, f"{spec.get('id', 'chip')} - top view (1:1 in mm)",
                caption, scale_note)

    out.append(f'<rect x="{X(0):.2f}" y="{Y(fy):.2f}" width="{fx * px_per_mm:.2f}" '
               f'height="{fy * px_per_mm:.2f}" fill="{"#%02x%02x%02x" % BODY}" '
               f'stroke="{INK}" stroke-width="1.4"/>')

    loops = geometry.trace_loops(geom["xs"], geom["ys"], geom["depths"])
    d = " ".join("M " + " L ".join(f"{X(x):.2f} {Y(y):.2f}" for x, y in loop) + " Z"
                 for loop in loops)
    out.append(f'<path d="{d}" fill="{"#%02x%02x%02x" % FLOOR}" fill-rule="evenodd" '
               f'stroke="#12415a" stroke-width="0.7"/>')

    for c in spec.get("chambers", []):
        cx, cy = c["center"][:2]
        out.append(f'<text x="{X(cx):.1f}" y="{Y(cy):.1f}" font-size="9" fill="#ffffff" '
                   f'text-anchor="middle" font-weight="600" '
                   f'font-family="Helvetica,Arial,sans-serif">{_esc(c["id"])}</text>')
    for p in spec.get("ports", []):
        px, py = p["position"][:2]
        r = (p.get("width_um") or 1600) / 2000 * px_per_mm
        colour = "#b45309" if p.get("type") == "inlet" else "#15803d"
        out.append(f'<circle cx="{X(px):.2f}" cy="{Y(py):.2f}" r="{max(r, 3.5):.2f}" '
                   f'fill="none" stroke="{colour}" stroke-width="1.6"/>')
        above = py > fy / 2
        out.append(f'<text x="{X(px):.1f}" y="{Y(py) + (-10 if above else 16):.1f}" '
                   f'font-size="9" fill="{colour}" text-anchor="middle" '
                   f'font-family="Helvetica,Arial,sans-serif">{_esc(p["id"])}</text>')

    bar = _scale_bar_mm(fx)
    bx, by = X(0), Y(0) + 34
    out += [f'<line x1="{bx:.1f}" y1="{by:.1f}" x2="{bx + bar * px_per_mm:.1f}" '
            f'y2="{by:.1f}" stroke="{INK}" stroke-width="2.4"/>',
            f'<line x1="{bx:.1f}" y1="{by - 5:.1f}" x2="{bx:.1f}" y2="{by + 5:.1f}" '
            f'stroke="{INK}" stroke-width="2.4"/>',
            f'<line x1="{bx + bar * px_per_mm:.1f}" y1="{by - 5:.1f}" '
            f'x2="{bx + bar * px_per_mm:.1f}" y2="{by + 5:.1f}" stroke="{INK}" '
            f'stroke-width="2.4"/>',
            f'<text x="{bx + bar * px_per_mm / 2:.1f}" y="{by + 19:.1f}" font-size="11" '
            f'fill="{INK}" text-anchor="middle" font-weight="600" '
            f'font-family="Helvetica,Arial,sans-serif">{bar:g} mm</text>',
            f'<text x="{bx + bar * px_per_mm + 14:.1f}" y="{by + 4:.1f}" font-size="10" '
            f'fill="{MUTED}" font-family="Helvetica,Arial,sans-serif">'
            f'origin (0, 0) at the lower left, as the spec draws it</text>',
            '</svg>', '']
    write_text(path, "\n".join(out))
    return {"view": "top", "px_per_mm": px_per_mm, "loops": len(loops),
            "scale_bar_mm": bar, "true_scale": True}


# --- isometric --------------------------------------------------------------------------

def render_iso(path: Path, spec: dict, geom: dict, body: dict, scale_note: str,
               px_per_mm: float = 17.0, z_exaggeration: float = None) -> dict:
    """Isometric view of the body mesh: painter's algorithm, flat Lambert shading."""
    fx, fy = geom["plan"]["fx"], geom["plan"]["fy"]
    exaggerate = z_exaggeration_for(geom) if z_exaggeration is None else z_exaggeration
    depth = geom["max_depth_mm"]
    top_z, floor_z = body["z_top_mm"], body["recess_floor_mm"]

    tops = [[top_z - d for d in row] for row in geom["depths"]]
    tris = display_mesh(geom["xs"], geom["ys"], tops, body["z0_mm"], floor_z)

    r, u, view = _camera(AZIMUTH_DEG, ELEVATION_DEG)

    def project(p):
        x, y, z = p[0], p[1], p[2] * exaggerate
        return (x * r[0] + y * r[1] + z * r[2], -(x * u[0] + y * u[1] + z * u[2]))

    def depth_of(p):
        return p[0] * view[0] + p[1] * view[1] + p[2] * exaggerate * view[2]

    drawn = []
    for (a, b, c), role in tris:
        ax, ay, az = a[0], a[1], a[2] * exaggerate
        bx, by, bz = b[0], b[1], b[2] * exaggerate
        cx, cy, cz = c[0], c[1], c[2] * exaggerate
        e1, e2 = (bx - ax, by - ay, bz - az), (cx - ax, cy - ay, cz - az)
        n = _norm((e1[1] * e2[2] - e1[2] * e2[1],
                   e1[2] * e2[0] - e1[0] * e2[2],
                   e1[0] * e2[1] - e1[1] * e2[0]))
        facing = sum(a * b for a, b in zip(n, view))
        if facing <= 1e-9:                       # back face: cull
            continue
        zs = (depth_of(a) + depth_of(b) + depth_of(c)) / 3
        rgb = {"top": BODY, "floor": FLOOR, "wall": WALL}[role]
        drawn.append((zs, [project(a), project(b), project(c)], _shade(rgb, n, False)))
    drawn.sort(key=lambda t: t[0])

    bar = _scale_bar_mm(fx)
    # The scale bar lives in the bottom margin, in screen space, but it is drawn along the
    # PROJECTED x axis and is as long as the projection makes `bar` mm - so it is
    # foreshortened exactly as the part is, while staying off it.
    o, e = project((0.0, 0.0, 0.0)), project((bar, 0.0, 0.0))
    bar_vec = ((e[0] - o[0]) * px_per_mm, (e[1] - o[1]) * px_per_mm)

    extra = [project(p) for p in ((0, 0, 0), (fx, 0, 0), (0, fy, 0), (fx, fy, 0),
                                  (0, 0, top_z), (fx, 0, top_z), (0, fy, top_z),
                                  (fx, fy, top_z))]
    pts = [p for _, poly, _ in drawn for p in poly] + extra
    minx, maxx = min(p[0] for p in pts), max(p[0] for p in pts)
    miny, maxy = min(p[1] for p in pts), max(p[1] for p in pts)
    # The bottom margin has to hold the legend, then the scale bar - which runs down as
    # well as across, because it follows the projected x axis - and then its label.
    pad_l, pad_r = 48, 48
    legend_y, bar_top = 18, 60
    pad_b = int(bar_top + max(0.0, bar_vec[1]) + 34)

    def S(p):
        return (pad_l + (p[0] - minx) * px_per_mm, pad_t + (p[1] - miny) * px_per_mm)

    caption = [
        f"footprint {fx:g} x {fy:g} mm, body {top_z:g} mm thick "
        f"({body['stack']['base_mm']:g} mm base + {body['stack']['fluidic_mm']:g} mm fluidic)",
        f"channels and chambers cut {depth * 1000:g} um deep, floor at z = {floor_z:g} mm; "
        f"channels OPEN, sealed by {body['stack']['seal'].split(':')[0]}",
        f"DEPTH EXAGGERATED {exaggerate:.2g}x so the pockets are visible - the real relief "
        f"is {depth * 1000:g} um, far flatter than this. Plan dimensions are NOT exaggerated.",
    ]
    pad_t = _titleblock_height(caption, scale_note)
    w = max((maxx - minx) * px_per_mm + pad_l + pad_r,
            _text_width(caption + [scale_note], 10.5) + pad_l + pad_r)
    h = (maxy - miny) * px_per_mm + pad_t + pad_b

    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
           f'viewBox="0 0 {w:.0f} {h:.0f}">',
           f'<rect width="{w:.0f}" height="{h:.0f}" fill="{PAPER}"/>']
    _titleblock(out, pad_l, 26, f"{spec.get('id', 'chip')} - isometric view", caption,
                scale_note)

    for _, poly, fill in drawn:
        pts_s = " ".join(f"{x:.2f},{y:.2f}" for x, y in (S(p) for p in poly))
        out.append(f'<polygon points="{pts_s}" fill="{fill}" stroke="{fill}" '
                   f'stroke-width="0.7" stroke-linejoin="round"/>')

    # The rim of every pocket, traced from the cut region and drawn on the top surface.
    # A channel narrower than it is deep hides its own floor from any viewpoint, so without
    # this outline the narrow runs would read as shadow rather than as fluid path.
    loops = geometry.trace_loops(geom["xs"], geom["ys"], geom["depths"])
    rim = " ".join(
        "M " + " L ".join("{:.2f} {:.2f}".format(*S(project((x, y, top_z))))
                          for x, y in loop) + " Z"
        for loop in loops)
    out.append(f'<path d="{rim}" fill="none" stroke="#0b3650" stroke-width="1.1" '
               f'stroke-linejoin="round" opacity="0.85"/>')

    key = [("chip body (solid)", "#%02x%02x%02x" % BODY),
           (f"etched floor, {depth * 1000:g} um down (fluid path)", "#%02x%02x%02x" % FLOOR)]
    ky = h - pad_b + legend_y
    for label, colour in key:
        out += [f'<rect x="{pad_l:.1f}" y="{ky - 9:.1f}" width="13" height="11" '
                f'fill="{colour}" stroke="{INK}" stroke-width="0.6"/>',
                f'<text x="{pad_l + 19:.1f}" y="{ky:.1f}" font-size="10.5" fill="{MUTED}" '
                f'font-family="Helvetica,Arial,sans-serif">{_esc(label)}</text>']
        ky += 16

    bx, by = pad_l, h - pad_b + bar_top
    ex, ey = bx + bar_vec[0], by + bar_vec[1]
    out += [f'<line x1="{bx:.1f}" y1="{by:.1f}" x2="{ex:.1f}" y2="{ey:.1f}" '
            f'stroke="{INK}" stroke-width="2.6"/>',
            f'<circle cx="{bx:.1f}" cy="{by:.1f}" r="2.6" fill="{INK}"/>',
            f'<circle cx="{ex:.1f}" cy="{ey:.1f}" r="2.6" fill="{INK}"/>',
            f'<text x="{max(bx, ex) + 12:.1f}" y="{(by + ey) / 2 + 4:.1f}" '
            f'font-size="11" fill="{INK}" text-anchor="start" font-weight="600" '
            f'font-family="Helvetica,Arial,sans-serif">{bar:g} mm along the part\'s x axis, '
            f'foreshortened exactly as the part is</text>',
            '</svg>', '']
    write_text(path, "\n".join(out))
    return {"view": "isometric", "triangles_drawn": len(drawn), "px_per_mm": px_per_mm,
            "z_exaggeration": exaggerate, "scale_bar_mm": bar, "true_scale": False,
            "azimuth_deg": AZIMUTH_DEG, "elevation_deg": ELEVATION_DEG}

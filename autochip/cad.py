"""CAD exports for one autochip chip spec (stage 4). Standard library only; matplotlib optional (PNG).

export_chip(spec, cad_dir, extra, violations) -> {name: path}
  <chip>.svg          to-scale top view: outline, 1 mm grid (bold every 5), modules, channels at true width, ports
  <chip>.png          same view through matplotlib (skipped when matplotlib is missing)
  <chip>.dxf          R12 ASCII, mm: layers OUTLINE / CHAMBERS / CHANNELS / PORTS (+ LABELS); every outline a
                      closed POLYLINE (channels as closed outlines at their width), ports as CIRCLEs
  parts/<module>.stl  each module placed in chip coordinates (library STL scaled into its box; parametric = box)
  <chip>.stl          body block + the fluidic features as separate closed shells (subtract them in CAD)
  <chip>_fluidic.stl  the fluidic features only (the negative: chambers, channels, port holes)
  <chip>_fusion.py    Fusion 360 script: imports the placed parts, sketches outline, channel paths, ports
  BUILD.md            step-by-step instructions for redrawing the chip by hand in any CAD tool
  README.md           one line per file
"""
from __future__ import annotations

import json
import math
import sys
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "chip" / "components") not in sys.path:
    sys.path.insert(0, str(ROOT / "chip" / "components"))
import stl_io  # noqa: E402

INLET_C, OUTLET_C, CONTROL_C = "#2e9e44", "#d23c3c", "#7a7a7a"
CHANNEL_C, CHANNEL2_C = "#2f6fb0", "#e08a1e"
MODULE_FILL, MODULE_EDGE = "#dbe9f7", "#2b4f7a"


# --------------------------------------------------------------------------- geometry helpers


def _clean(path):
    pts = []
    for p in path:
        p = (float(p[0]), float(p[1]))
        if pts and math.dist(pts[-1], p) < 1e-9:
            continue
        pts.append(p)
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, b, c = out[-1], pts[i], pts[i + 1]
        if abs((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) > 1e-9:
            out.append(b)
    out.append(pts[-1])
    return out


def channel_outline(path, width):
    """Closed outline (list of points, not repeated) of a polyline channel at its width (mitred corners)."""
    pts = _clean(path)
    h = width / 2
    normals = []
    for a, b in zip(pts[:-1], pts[1:]):
        L = math.dist(a, b)
        normals.append((-(b[1] - a[1]) / L, (b[0] - a[0]) / L))
    left, right = [], []
    for i, p in enumerate(pts):
        if i == 0:
            n = normals[0]
            off = (n[0] * h, n[1] * h)
        elif i == len(pts) - 1:
            n = normals[-1]
            off = (n[0] * h, n[1] * h)
        else:
            n1, n2 = normals[i - 1], normals[i]
            k = h / (1 + n1[0] * n2[0] + n1[1] * n2[1])
            off = ((n1[0] + n2[0]) * k, (n1[1] + n2[1]) * k)
        left.append((p[0] + off[0], p[1] + off[1]))
        right.append((p[0] - off[0], p[1] - off[1]))
    return left + right[::-1]


def path_length(path):
    return sum(math.dist(a, b) for a, b in zip(path[:-1], path[1:]))


def _box(x0, y0, z0, x1, y1, z1):
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0), (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    f = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6),
         (3, 0, 4), (3, 4, 7)]
    return [(v[a], v[b], v[c]) for a, b, c in f]


def _cylinder(x, y, r, z0, z1, n=24):
    tris = []
    ring = [(x + r * math.cos(2 * math.pi * i / n), y + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        tris.append(((x, y, z0), (b[0], b[1], z0), (a[0], a[1], z0)))
        tris.append(((x, y, z1), (a[0], a[1], z1), (b[0], b[1], z1)))
        tris.append(((a[0], a[1], z0), (b[0], b[1], z0), (b[0], b[1], z1)))
        tris.append(((a[0], a[1], z0), (b[0], b[1], z1), (a[0], a[1], z1)))
    return tris


def _fluidic_z(spec):
    z = 0.0
    out = {}
    for lay in spec["layers"]:
        out[lay["id"]] = (z, z + lay["thickness_mm"])
        z += lay["thickness_mm"]
    return out, z


def _layer(item):
    note = str(item.get("note", ""))
    return note.split()[1].rstrip(";,") if note.startswith("layer ") else "fluidic"


# --------------------------------------------------------------------------- SVG


def place_labels(spec: dict, k: float, fs_mod: float, fs_port: float, canvas: tuple, cw: float = 0.6) -> dict:
    """Collision-free label boxes in mm (k = px per mm, fs_* = font sizes in px, canvas = (x0, y0, x1, y1) in mm).
    Module labels go inside their module when they fit, else below / above / beside it. Each port label (id and
    (x, y)) tries the four sides of its port, then the diagonals, then further out with a leader line; no label
    may cover a port circle or another label, and labels prefer not to cover modules.
    -> {"modules": {id: (x, y, w, h)}, "ports": {id: (x, y, w, h, leader)}}, (x, y) = bottom-left corner."""
    X0, Y0, X1, Y1 = canvas
    placed: list[tuple] = []
    circles = {}
    for p in spec["ports"]:
        x, y = p["position"][:2]
        r = max(p.get("width_um", 1000) / 2000, 3 / k)
        circles[p["id"]] = (x - r, y - r, x + r, y + r)
    rooms = [(c["center"][0] - c["size"][0] / 2, c["center"][1] - c["size"][1] / 2,
              c["center"][0] + c["size"][0] / 2, c["center"][1] + c["size"][1] / 2) for c in spec["chambers"]]

    def area(a, b):
        w, h = min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])
        return w * h if w > 0 and h > 0 else 0.0

    def hard(b, own=None):
        off = 1e6 if (b[0] < X0 or b[1] < Y0 or b[2] > X1 or b[3] > Y1) else 0.0
        return off + sum(area(b, c) for c in placed) + 10 * sum(area(b, c) for pid, c in circles.items() if pid != own)

    def pick(boxes, own=None, inside=-1):
        def key(i):
            h = hard(boxes[i], own)
            soft = i != inside and any(area(boxes[i], m) > 0 for m in rooms)
            return (h > 1e-9, h, i + (4 if soft else 0))
        return min(range(len(boxes)), key=key)
    out: dict = {"modules": {}, "ports": {}}
    g = 3 / k
    later = []
    for c in spec["chambers"]:          # pass 1: labels that fit inside their own module
        (x, y, _), (sx, sy, sz) = c["center"], c["size"]
        w = max(len(c["id"]), len(f"{sx:.3g} x {sy:.3g} x {sz:g} mm")) * cw * fs_mod / k + 6 / k
        h = 2.3 * fs_mod / k + 4 / k
        if w <= sx - 2 / k and h <= sy - 2 / k:
            box = (x - w / 2, y - h / 2, x + w / 2, y + h / 2)
            placed.append(box)
            out["modules"][c["id"]] = (box[0], box[1], w, h)
        else:
            later.append((c, w, h))
    for c, w, h in later:               # pass 2: the rest beside their module, avoiding everything placed
        (x, y, _), (sx, sy, sz) = c["center"], c["size"]
        cands = [(x - w / 2, y - sy / 2 - g - h), (x - w / 2, y + sy / 2 + g), (x + sx / 2 + g, y - h / 2), (x - sx / 2 - g - w, y - h / 2)]
        for step in range(1, 5):
            t = step * (h + g)
            cands += [(x - w / 2, y - sy / 2 - g - h - t), (x - w / 2, y + sy / 2 + g + t),
                      (x + sx / 2 + g, y - h / 2 + t), (x + sx / 2 + g, y - h / 2 - t)]
        boxes = [(a, b, a + w, b + h) for a, b in cands]
        i = pick(boxes)
        placed.append(boxes[i])
        out["modules"][c["id"]] = (boxes[i][0], boxes[i][1], w, h)
    for p in sorted(spec["ports"], key=lambda p: (-p["position"][1], p["position"][0])):
        x, y = p["position"][:2]
        r = circles[p["id"]][2] - x
        w = max(len(p["id"]), len(f"({x:g}, {y:g})") * 0.9) * cw * fs_port / k + 6 / k
        h = 2.3 * fs_port / k + 4 / k
        d = r * 0.7 + g
        cands = [(x + r + g, y - h / 2), (x - r - g - w, y - h / 2), (x - w / 2, y + r + g), (x - w / 2, y - r - g - h),
                 (x + d, y + d), (x - d - w, y + d), (x + d, y - d - h), (x - d - w, y - d - h)]
        near = len(cands)
        for step in range(1, 9):
            s = step * (h + g)
            cands += [(x - w / 2, y + r + g + s), (x - w / 2, y - r - g - h - s), (x + r + g, y - h / 2 + s), (x + r + g, y - h / 2 - s),
                      (x - r - g - w, y - h / 2 + s), (x - r - g - w, y - h / 2 - s)]
        boxes = [(a, b, a + w, b + h) for a, b in cands]
        i = pick(boxes, own=p["id"])
        placed.append(boxes[i])
        out["ports"][p["id"]] = (boxes[i][0], boxes[i][1], w, h, i >= near)
    return out


def _leader_end(x: float, y: float, box: tuple) -> tuple[float, float]:
    bx, by, bw, bh = box[:4]
    return min(max(x, bx), bx + bw), min(max(y, by), by + bh)


def svg(spec: dict, violations: list | None = None) -> str:
    fx, fy = spec["footprint"]["x"], spec["footprint"]["y"]
    k = max(6.0, min(40.0, 1000.0 / max(fx, fy * 1.5)))       # px per mm
    padl, padt, padr, padb = 56, 64, 28, 44
    W, H = fx * k + padl + padr, fy * k + padt + padb

    def X(x):
        return padl + x * k

    def Y(y):
        return padt + (fy - y) * k
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W:.0f}" height="{H:.0f}" viewBox="0 0 {W:.0f} {H:.0f}" '
         f'font-family="Helvetica, Arial, sans-serif">',
         f'<rect width="100%" height="100%" fill="white"/>']
    method = (spec.get("generator", {}).get("params", {}) or {}).get("fabrication_method", "")
    cons = (spec.get("generator", {}).get("params", {}) or {}).get("constraints_id", "")
    o.append(f'<text x="{padl}" y="22" font-size="15" font-weight="bold">{escape(spec["id"])}: {escape(spec.get("name", ""))}</text>')
    o.append(f'<text x="{padl}" y="40" font-size="11" fill="#444">{fx:g} x {fy:g} mm, top view, to scale ({k:.3g} px/mm). '
             f'Origin bottom-left. Constraints: {escape(str(cons))} ({escape(str(method))}). '
             f'Depth {spec["channels"][0]["depth_mm"] if spec["channels"] else "-"} mm.</text>')
    # grid
    step_lbl = 5 if max(fx, fy) <= 120 else 10
    for i in range(int(math.floor(fx)) + 1):
        bold = i % 5 == 0
        o.append(f'<line x1="{X(i):.2f}" y1="{Y(0):.2f}" x2="{X(i):.2f}" y2="{Y(fy):.2f}" stroke="{"#c4c4c4" if bold else "#ececec"}" '
                 f'stroke-width="{1 if bold else 0.5}"/>')
        if i % step_lbl == 0:
            o.append(f'<text x="{X(i):.2f}" y="{Y(0) + 14:.2f}" font-size="10" text-anchor="middle" fill="#555">{i}</text>')
    for j in range(int(math.floor(fy)) + 1):
        bold = j % 5 == 0
        o.append(f'<line x1="{X(0):.2f}" y1="{Y(j):.2f}" x2="{X(fx):.2f}" y2="{Y(j):.2f}" stroke="{"#c4c4c4" if bold else "#ececec"}" '
                 f'stroke-width="{1 if bold else 0.5}"/>')
        if j % step_lbl == 0:
            o.append(f'<text x="{X(0) - 6:.2f}" y="{Y(j) + 3.5:.2f}" font-size="10" text-anchor="end" fill="#555">{j}</text>')
    o.append(f'<text x="{X(fx / 2):.2f}" y="{Y(0) + 30:.2f}" font-size="10" text-anchor="middle" fill="#555">x (mm)</text>')
    o.append(f'<rect x="{X(0):.2f}" y="{Y(fy):.2f}" width="{fx * k:.2f}" height="{fy * k:.2f}" fill="none" stroke="#111" stroke-width="2"/>')
    # modules
    for c in spec["chambers"]:
        (cx, cy, _), (sx, sy, sz) = c["center"], c["size"]
        o.append(f'<rect x="{X(cx - sx / 2):.2f}" y="{Y(cy + sy / 2):.2f}" width="{sx * k:.2f}" height="{sy * k:.2f}" '
                 f'fill="{MODULE_FILL}" stroke="{MODULE_EDGE}" stroke-width="1.2"/>')
    # channels
    for ch in spec["channels"]:
        if not ch.get("path"):
            continue
        two = _layer(ch) != "fluidic"
        pts = " ".join(f"{X(x):.2f},{Y(y):.2f}" for x, y in ch["path"])
        o.append(f'<polyline points="{pts}" fill="none" stroke="{CHANNEL2_C if two else CHANNEL_C}" '
                 f'stroke-width="{max(ch["width_mm"] * k, 0.8):.2f}" stroke-linejoin="miter" stroke-linecap="butt"'
                 f'{" stroke-dasharray=" + chr(34) + "6 3" + chr(34) if two else ""} opacity="0.9"/>')
    # labels (placed without overlaps), then the port circles on top
    fsm, fsp = 11, 10
    lab = place_labels(spec, k, fsm, fsp, (-(padl - 8) / k, -(padb - 22) / k, fx + (padr - 4) / k, fy + (padt - 50) / k))
    for c in spec["chambers"]:
        (cx, cy, _), (sx, sy, sz) = c["center"], c["size"]
        bx, by, bw, bh = lab["modules"][c["id"]]
        tx, ty = X(bx + bw / 2), Y(by + bh) + fsm + 1
        o.append(f'<text x="{tx:.2f}" y="{ty:.2f}" font-size="{fsm}" font-weight="bold" text-anchor="middle" fill="#13314f" '
                 f'stroke="white" stroke-width="3" paint-order="stroke">{escape(c["id"])}</text>')
        o.append(f'<text x="{tx:.2f}" y="{ty + fsm + 1:.2f}" font-size="{fsm - 1}" text-anchor="middle" fill="#13314f" '
                 f'stroke="white" stroke-width="3" paint-order="stroke">{escape(f"{sx:.3g} x {sy:.3g} x {sz:g} mm")}</text>')
    for p in spec["ports"]:
        x, y, _ = p["position"]
        col = INLET_C if p["type"] == "inlet" else OUTLET_C if p["type"] == "outlet" else CONTROL_C
        bx, by, bw, bh, leader = lab["ports"][p["id"]]
        if leader:
            ex, ey = _leader_end(x, y, (bx, by, bw, bh))
            o.append(f'<line x1="{X(x):.2f}" y1="{Y(y):.2f}" x2="{X(ex):.2f}" y2="{Y(ey):.2f}" stroke="{col}" stroke-width="0.8"/>')
        o.append(f'<text x="{X(bx) + 3:.2f}" y="{Y(by + bh) + fsp + 1:.2f}" font-size="{fsp}" fill="{col}" font-weight="bold" '
                 f'stroke="white" stroke-width="3" paint-order="stroke">{escape(p["id"])}</text>')
        o.append(f'<text x="{X(bx) + 3:.2f}" y="{Y(by + bh) + 2 * fsp + 2:.2f}" font-size="{fsp - 1}" fill="#333" '
                 f'stroke="white" stroke-width="3" paint-order="stroke">({x:g}, {y:g})</text>')
    for p in spec["ports"]:
        x, y, _ = p["position"]
        r = p.get("width_um", 1000) / 2000
        col = INLET_C if p["type"] == "inlet" else OUTLET_C if p["type"] == "outlet" else CONTROL_C
        o.append(f'<circle cx="{X(x):.2f}" cy="{Y(y):.2f}" r="{max(r * k, 3):.2f}" fill="{col}" stroke="#111" stroke-width="1"/>')
    # legend
    ly = H - 10
    items = [(INLET_C, "inlet"), (OUTLET_C, "outlet")]
    if any(p["type"] == "control" for p in spec["ports"]):
        items.append((CONTROL_C, "valve control"))
    items.append((CHANNEL_C, "channel (true width)"))
    if any(_layer(ch) != "fluidic" for ch in spec["channels"]):
        items.append((CHANNEL2_C, "channel, 2nd layer"))
    lx = padl
    for col, label in items:
        o.append(f'<rect x="{lx}" y="{ly - 9}" width="12" height="10" fill="{col}"/>')
        o.append(f'<text x="{lx + 16}" y="{ly}" font-size="10" fill="#333">{escape(label)}</text>')
        lx += 22 + 6.2 * len(label)
    n_err = sum(1 for v in violations or [] if v.get("severity") == "error")
    if n_err:
        o.append(f'<text x="{W - padr}" y="{ly}" font-size="10" text-anchor="end" fill="{OUTLET_C}">{n_err} constraint error(s): see BUILD.md</text>')
    o.append("</svg>")
    return "\n".join(o)


def png(spec: dict, path: Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Circle, Polygon, Rectangle
    except Exception:  # noqa: BLE001
        return False
    fx, fy = spec["footprint"]["x"], spec["footprint"]["y"]
    scale = min(14 / fx, 10 / fy, 0.5)
    fig, ax = plt.subplots(figsize=(max(6, fx * scale + 1.5), max(4, fy * scale + 1.4)), dpi=150)
    for i in range(int(fx) + 1):
        ax.axvline(i, color="#c4c4c4" if i % 5 == 0 else "#ececec", lw=0.9 if i % 5 == 0 else 0.4, zorder=0)
    for j in range(int(fy) + 1):
        ax.axhline(j, color="#c4c4c4" if j % 5 == 0 else "#ececec", lw=0.9 if j % 5 == 0 else 0.4, zorder=0)
    ax.add_patch(Rectangle((0, 0), fx, fy, fill=False, ec="#111", lw=1.8, zorder=1))
    for c in spec["chambers"]:
        (cx, cy, _), (sx, sy, _) = c["center"], c["size"]
        ax.add_patch(Rectangle((cx - sx / 2, cy - sy / 2), sx, sy, fc=MODULE_FILL, ec=MODULE_EDGE, lw=1, zorder=2))
    for ch in spec["channels"]:
        if ch.get("path"):
            two = _layer(ch) != "fluidic"
            ax.add_patch(Polygon(channel_outline(ch["path"], ch["width_mm"]), closed=True, fc=CHANNEL2_C if two else CHANNEL_C,
                                 ec="none", alpha=0.9, zorder=3))
    ax.set_xlim(-0.5, fx + 0.5)
    ax.set_ylim(-0.5, fy + 0.5)
    ax.set_aspect("equal")
    ax.set_xlabel("x (mm)")
    ax.set_ylabel("y (mm)")
    ax.set_title(f"{spec['id']}: {fx:g} x {fy:g} mm (top view, to scale)", fontsize=10)
    fig.tight_layout()
    fig.canvas.draw()
    k = (ax.transData.transform((1, 0))[0] - ax.transData.transform((0, 0))[0])     # display px per mm
    px = fig.dpi / 72
    fsm, fsp = (7.5, 6.5) if max(fx, fy) <= 60 else (7, 6)
    lab = place_labels(spec, k, fsm * px, fsp * px, (-0.5, -0.5, fx + 0.5, fy + 0.5), cw=0.64)
    for c in spec["chambers"]:
        (cx, cy, _), (sx, sy, sz) = c["center"], c["size"]
        bx, by, bw, bh = lab["modules"][c["id"]]
        ax.add_patch(Rectangle((bx, by), bw, bh, fc="white", ec="none", alpha=0.8, zorder=5))
        ax.text(bx + bw / 2, by + bh / 2, f"{c['id']}\n{sx:.3g} x {sy:.3g} x {sz:g} mm", ha="center", va="center", fontsize=fsm,
                color="#13314f", weight="bold", zorder=6, linespacing=1.15)
    for p in spec["ports"]:
        x, y, _ = p["position"]
        r = p.get("width_um", 1000) / 2000
        col = INLET_C if p["type"] == "inlet" else OUTLET_C if p["type"] == "outlet" else CONTROL_C
        bx, by, bw, bh, leader = lab["ports"][p["id"]]
        if leader:
            ex, ey = _leader_end(x, y, (bx, by, bw, bh))
            ax.plot([x, ex], [y, ey], color=col, lw=0.6, zorder=5)
        ax.add_patch(Rectangle((bx, by), bw, bh, fc="white", ec="none", alpha=0.85, zorder=5))
        ax.text(bx + 3 / k, by + bh / 2, f"{p['id']}\n({x:g}, {y:g})", ha="left", va="center", fontsize=fsp, color=col,
                weight="bold", zorder=6, linespacing=1.15)
        ax.add_patch(Circle((x, y), r, fc=col, ec="#111", lw=0.6, zorder=8))
    fig.savefig(path)
    plt.close(fig)
    return True


# --------------------------------------------------------------------------- DXF (R12)


def dxf(spec: dict) -> str:
    out = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009", "9", "$INSUNITS", "70", "4", "0", "ENDSEC",
           "0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER", "70", "5"]
    for name, color in (("OUTLINE", 7), ("CHAMBERS", 5), ("CHANNELS", 1), ("PORTS", 3), ("LABELS", 8)):
        out += ["0", "LAYER", "2", name, "70", "0", "62", str(color), "6", "CONTINUOUS"]
    out += ["0", "ENDTAB", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]

    def poly(layer, pts):
        out.extend(["0", "POLYLINE", "8", layer, "66", "1", "10", "0.0", "20", "0.0", "30", "0.0", "70", "1"])
        for x, y in pts:
            out.extend(["0", "VERTEX", "8", layer, "10", f"{x:.4f}", "20", f"{y:.4f}", "30", "0.0"])
        out.extend(["0", "SEQEND", "8", layer])

    def text(x, y, h, s):
        out.extend(["0", "TEXT", "8", "LABELS", "10", f"{x:.4f}", "20", f"{y:.4f}", "30", "0.0", "40", f"{h:.3f}", "1", s])
    fx, fy = spec["footprint"]["x"], spec["footprint"]["y"]
    poly("OUTLINE", [(0, 0), (fx, 0), (fx, fy), (0, fy)])
    th = max(0.25, min(1.0, min(fx, fy) / 40))
    for c in spec["chambers"]:
        (cx, cy, _), (sx, sy, _) = c["center"], c["size"]
        poly("CHAMBERS", [(cx - sx / 2, cy - sy / 2), (cx + sx / 2, cy - sy / 2), (cx + sx / 2, cy + sy / 2), (cx - sx / 2, cy + sy / 2)])
        text(cx - sx / 2, cy + sy / 2 + th * 0.4, th, c["id"])
    for ch in spec["channels"]:
        if ch.get("path"):
            poly("CHANNELS", channel_outline(ch["path"], ch["width_mm"]))
    for p in spec["ports"]:
        x, y, _ = p["position"]
        out.extend(["0", "CIRCLE", "8", "PORTS", "10", f"{x:.4f}", "20", f"{y:.4f}", "30", "0.0", "40",
                    f"{p.get('width_um', 1000) / 2000:.4f}"])
        text(x + p.get("width_um", 1000) / 2000 + 0.2, y + 0.2, th * 0.8, p["id"])
    out += ["0", "ENDSEC", "0", "EOF"]
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- STL


def part_mesh(chamber: dict, part: dict | None):
    (cx, cy, cz), (sx, sy, sz) = chamber["center"], chamber["size"]
    x0, y0, z0 = cx - sx / 2, cy - sy / 2, cz - sz / 2
    stl = (part or {}).get("stl")
    if stl and (ROOT / stl).exists():
        tris = stl_io.read_stl(ROOT / stl)
        if "rotated 90 deg" in str(chamber.get("note", "")):      # layout turned this library part: (x, y) -> (-y, x)
            tris = [tuple((-v[1], v[0], v[2]) for v in t) for t in tris]
        lo, hi = stl_io.bbox(tris)
        ext = [max(hi[i] - lo[i], 1e-9) for i in range(3)]
        k = (sx / ext[0], sy / ext[1], sz / ext[2])
        return [tuple(tuple((p[i] - lo[i]) * k[i] + (x0, y0, z0)[i] for i in range(3)) for p in t) for t in tris], "library"
    return _box(x0, y0, z0, x0 + sx, y0 + sy, z0 + sz), "box"


def fluidic_mesh(spec: dict):
    zs, top = _fluidic_z(spec)
    tris = []
    for c in spec["chambers"]:
        (cx, cy, cz), (sx, sy, sz) = c["center"], c["size"]
        tris += _box(cx - sx / 2, cy - sy / 2, cz - sz / 2, cx + sx / 2, cy + sy / 2, cz + sz / 2)
    for ch in spec["channels"]:
        z0 = zs.get(_layer(ch), zs.get("fluidic", (1, 1)))[0]
        h = ch["width_mm"] / 2
        for a, b in zip((ch.get("path") or [])[:-1], (ch.get("path") or [])[1:]):
            tris += _box(min(a[0], b[0]) - h, min(a[1], b[1]) - h, z0, max(a[0], b[0]) + h, max(a[1], b[1]) + h, z0 + ch["depth_mm"])
    fz = zs.get("fluidic", (0, 0))[0]
    for p in spec["ports"]:
        x, y, _ = p["position"]
        tris += _cylinder(x, y, p.get("width_um", 1000) / 2000, fz, top)
    return tris


# --------------------------------------------------------------------------- Fusion script

FUSION = '''"""Fusion 360 script: chip {chip_id} ({fx:g} x {fy:g} mm), generated by autochip/cad.py from chips/{chip_id}.json.
Run it in Fusion: Utilities > Scripts and Add-Ins, or send it through the Fusion MCP execute_code tool
(then set PARTS_DIR first). It
  1. imports every placed part (parts/<module>.stl, mm, chip origin) as its own component,
  2. sketches the board outline, the port circles and every channel centreline ("channel paths"),
  3. sketches every channel as a closed outline at its true width ("channel outlines"), ready to
     extrude-cut {depth:g} mm into the fluidic layer (top of the fluidic layer at z = {ztop:g} mm).
Fusion's API works in cm; the numbers below are mm and converted on the way in.
"""
import adsk.core, adsk.fusion
import os

try:
    PARTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parts")
except NameError:
    PARTS_DIR = globals()["PARTS_DIR"]
BOARD = [{fx}, {fy}]
PARTS = {parts}
CHANNELS = {channels}
OUTLINES = {outlines}
PORTS = {ports}


def P(xy, z=0.0):
    return adsk.core.Point3D.create(xy[0] / 10.0, xy[1] / 10.0, z / 10.0)


def run(context=None):
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)
    root = design.rootComponent
    for module_id, part in PARTS:
        occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        comp = occ.component
        comp.name = module_id + " (" + part + ")"
        path = os.path.join(PARTS_DIR, module_id + ".stl")
        if design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
            base = comp.features.baseFeatures.add()
            base.startEdit()
            comp.meshBodies.add(path, adsk.fusion.MeshUnits.MillimeterMeshUnit, base)
            base.finishEdit()
        else:
            comp.meshBodies.add(path, adsk.fusion.MeshUnits.MillimeterMeshUnit)
    sk = root.sketches.add(root.xYConstructionPlane)
    sk.name = "{chip_id} outline + ports + channel paths"
    lines = sk.sketchCurves.sketchLines
    lines.addTwoPointRectangle(P((0, 0)), P(BOARD))
    for pid, x, y, d in PORTS:
        sk.sketchCurves.sketchCircles.addByCenterRadius(P((x, y)), d / 20.0)
    for cid, width, pts in CHANNELS:
        for a, b in zip(pts[:-1], pts[1:]):
            lines.addByTwoPoints(P(a), P(b))
    sk2 = root.sketches.add(root.xYConstructionPlane)
    sk2.name = "{chip_id} channel outlines (true width)"
    for cid, pts in OUTLINES:
        for a, b in zip(pts, pts[1:] + pts[:1]):
            sk2.sketchCurves.sketchLines.addByTwoPoints(P(a), P(b))
    app.activeViewport.fit()
    return "imported %d parts, sketched %d channels and %d ports" % (len(PARTS), len(CHANNELS), len(PORTS))
'''


def fusion_script(spec: dict, extra: dict) -> str:
    zs, _ = _fluidic_z(spec)
    fl = zs.get("fluidic", (0, 0))
    parts = [[c["id"], (extra.get("parts", {}).get(c["id"]) or {}).get("name", c["type"])] for c in spec["chambers"]]
    chans = [[ch["id"], ch["width_mm"], ch["path"]] for ch in spec["channels"] if ch.get("path")]
    outl = [[ch["id"], [[round(x, 4), round(y, 4)] for x, y in channel_outline(ch["path"], ch["width_mm"])]]
            for ch in spec["channels"] if ch.get("path")]
    ports = [[p["id"], p["position"][0], p["position"][1], p.get("width_um", 1000) / 1000] for p in spec["ports"]]
    depth = spec["channels"][0]["depth_mm"] if spec["channels"] else 0
    return FUSION.format(chip_id=spec["id"], fx=spec["footprint"]["x"], fy=spec["footprint"]["y"], parts=json.dumps(parts),
                         channels=json.dumps(chans), outlines=json.dumps(outl), ports=json.dumps(ports), depth=depth,
                         ztop=round(fl[1], 4))


# --------------------------------------------------------------------------- BUILD.md

SEALING = {
    "resin_print_hot_emboss": "Print (or emboss from a printed master) the substrate with the fluidic relief facing up, all features "
                              "{d:g} mm deep. Punch/drill the port holes through the film (or the substrate), then seal the open "
                              "channels with the film.",
    "resin_print": "Print the chip closed (channels inside the part); drain and flush uncured resin out through the ports.",
    "soft_lithography_pdms": "Make a master with the flow-layer features {d:g} mm high, cast PDMS, peel, punch the ports, and "
                             "plasma-bond to a glass slide (control layer on top when there are valves).",
    "glass_laser_3d_etch": "Write the channel and chamber volumes into one glass block with the laser (fluidic layer z = {z0:g} to "
                           "{z1:g} mm), write the port shafts up to the top face, then etch. Nothing is bonded.",
    "laser_cut_laminate": "Cut the channels through the {d:g} mm spacer sheet, cut port holes in the top sheet, stack base / spacer / "
                          "lid and bond.",
}


def build_md(spec: dict, violations: list, extra: dict, files: list[str]) -> str:
    g = spec.get("generator", {}).get("params", {})
    fx, fy = spec["footprint"]["x"], spec["footprint"]["y"]
    zs, top = _fluidic_z(spec)
    z0, z1 = zs.get("fluidic", (0, 0))
    method = g.get("fabrication_method", "other")
    depth = spec["channels"][0]["depth_mm"] if spec["channels"] else z1 - z0
    L = [f"# Build {spec['id']} by hand in CAD", "",
         f"{spec.get('name', '')}. Generated by autochip from `chips/{spec['id']}.json` (constraints `{g.get('constraints_id')}`, "
         f"{method}). **Placeholder geometry**: a first automatic layout; check it before you make it.", "",
         "Units are mm. Origin at the bottom-left corner of the chip, x to the right, y up, z up out of the chip. "
         f"The top view `{spec['id']}.svg` / `.png` shows everything below to scale, with a 1 mm grid.", "",
         "## 1. Board", "",
         f"1. Draw a {fx:g} x {fy:g} mm rectangle with its bottom-left corner at (0, 0).",
         f"2. Layer stack, bottom to top (total {top:g} mm):"]
    for lay in spec["layers"]:
        a, b = zs[lay["id"]]
        L.append(f"   - `{lay['id']}`: {lay['material']}, {lay['thickness_mm']:g} mm (z {a:g} to {b:g})"
                 + (f" - {lay['note']}" if lay.get("note") else ""))
    L += [f"3. All fluidic features sit in layer `fluidic`, cut down from z = {z1:g} mm. Channels are {depth:g} mm deep; chambers "
          "as listed below.", "", "## 2. Modules (chambers), in flow order", ""]
    mods = g.get("modules", {})
    for i, c in enumerate(spec["chambers"], 1):
        (cx, cy, cz), (sx, sy, sz) = c["center"], c["size"]
        m = mods.get(c["id"], {})
        part = m.get("part", c["type"])
        how = f"import `parts/{c['id']}.stl` (library part `{part}`" + (f", scaled {m['scale']:g} x" if m.get("scale", 1) < 0.9999 else "") \
            + ") and place its bounding box there" if m.get("source") == "library" else f"draw it as a {c['type']} ({part}, parametric)"
        L.append(f"{i}. **{c['id']}** - {sx:.3g} x {sy:.3g} mm, {sz:g} mm deep, bottom-left corner at ({cx - sx / 2:.3f}, "
                 f"{cy - sy / 2:.3f}), centre ({cx:g}, {cy:g}); {c['volume_ul']:.3g} uL; type {c['type']}. {how.capitalize()}.")
        if m.get("does"):
            L.append(f"   - Does (steps {', '.join(map(str, m.get('steps', []))) or '-'}): {m['does']}")
        if c.get("mixer"):
            mx = c["mixer"]
            L.append(f"   - Inside: a serpentine of {mx.get('length_mm', 0):.4g} mm of {mx['channel_width_mm']:g} mm channel, "
                     f"{mx.get('turns', 1) + 1} lanes filling the box.")
        if c.get("filter"):
            L.append(f"   - Inside: a row of pillars with {c['filter']['gap_um']:g} um gaps across the flow ({c['filter']['purpose']}).")
        if c.get("mesh_pore_um"):
            L.append(f"   - Floor: a {c['mesh_pore_um']:g} um mesh (bought sheet, inserted at assembly).")
    L += ["", "## 3. Channels", "",
          "Each channel is a path of straight segments through the listed points (its centreline), drawn at its width and "
          "cut to its depth. It starts at the edge of the `from` part and ends at the edge of the `to` part, or at a port centre.", ""]
    for i, ch in enumerate(spec["channels"], 1):
        pts = " -> ".join(f"({x:g}, {y:g})" for x, y in ch.get("path", []))
        L.append(f"{i}. **{ch['id']}**: {ch['from']} -> {ch['to']}, {ch['width_mm']:g} mm wide, {ch['depth_mm']:g} mm deep, "
                 f"carries {ch.get('carries', 'liquid')}, {path_length(ch.get('path', [[0, 0], [0, 0]])):.2f} mm long"
                 + (f", on layer `{_layer(ch)}`" if _layer(ch) != "fluidic" else "") + f". Path: {pts}.")
    L += ["", "## 4. Ports", "",
          "Each port is a round hole from the top face down into the fluidic layer, centred on the channel end.", ""]
    for p in spec["ports"]:
        x, y, z = p["position"]
        L.append(f"- **{p['id']}** ({p['type']}, {p.get('role', '-')}): {p.get('width_um', 1000) / 1000:g} mm hole at ({x:g}, {y:g}), "
                 f"from z = {z:g} down to z = {z0:g}." + (f" {p['note']}." if p.get("note") else ""))
    for v in spec.get("valves", []):
        L.append(f"- Valve **{v['id']}** ({v['type']}) on {v['channel']}, switched through {v['control_port']}.")
    L += ["", "## 5. Make and seal", ""]
    L.append(SEALING.get(method, "Make the fluidic layer with the features above and close it with the lid layer.").format(
        d=depth, z0=z0, z1=z1))
    for f in spec.get("features", []):
        L.append(f"\nZone `{f['id']}`: {f.get('note', f['type'])} (covers {', '.join(f.get('covers', []))}).")
    L += ["", "## 6. Constraint findings", ""]
    if violations:
        L += [f"- **{v['severity'].upper()}** ({v['rule']}): {v['message']}" for v in violations]
    else:
        L.append("None: the chip meets every constraint autochip checks.")
    L += ["", "## Files", ""] + [f"- `{f}`" for f in files]
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------- entry point


def export_chip(spec: dict, cad_dir: Path, extra: dict | None = None, violations: list | None = None) -> dict:
    extra = extra or {}
    violations = violations or []
    cad_dir = Path(cad_dir)
    cad_dir.mkdir(parents=True, exist_ok=True)
    cid = spec["id"]
    files: dict[str, str] = {}
    readme: list[str] = []

    def put(name: str, path: Path, what: str):
        files[name] = str(path)
        readme.append(f"- `{path.relative_to(cad_dir).as_posix()}`: {what}")
    p = cad_dir / f"{cid}.svg"
    p.write_text(svg(spec, violations))
    put("svg", p, "top view to scale (1 mm grid, bold every 5 mm), modules with id and size, channels at true width, ports "
                  "(green inlets, red outlets) with id and (x, y)")
    p = cad_dir / f"{cid}.png"
    if png(spec, p):
        put("png", p, "the same top view as a picture")
    p = cad_dir / f"{cid}.dxf"
    p.write_text(dxf(spec))
    put("dxf", p, "R12 ASCII DXF in mm: OUTLINE, CHAMBERS, CHANNELS (closed outlines at true width), PORTS (circles), LABELS")
    whole_parts = []
    for c in spec["chambers"]:
        tris, how = part_mesh(c, (extra.get("parts") or {}).get(c["id"]))
        pp = cad_dir / "parts" / f"{c['id']}.stl"
        stl_io.write_stl(pp, tris, c["id"])
        whole_parts += tris
        put(f"part:{c['id']}", pp, f"module {c['id']} placed in chip coordinates ("
                                   + ("library STL scaled into its box" if how == "library" else "parametric part as a box") + ")")
    fl = fluidic_mesh(spec)
    _, top = _fluidic_z(spec)
    body = _box(0, 0, 0, spec["footprint"]["x"], spec["footprint"]["y"], top)
    p = cad_dir / f"{cid}.stl"
    stl_io.write_stl(p, body + fl, cid)
    put("stl", p, "whole chip: a plain body block plus the fluidic features (chambers, channels, port holes) as separate "
                  "closed shells in the same file; subtract the features from the block in CAD to get the real part")
    p = cad_dir / f"{cid}_fluidic.stl"
    stl_io.write_stl(p, fl, cid + "_fluidic")
    put("stl_fluidic", p, "the fluidic features only (the negative to subtract)")
    p = cad_dir / f"{cid}_fusion.py"
    p.write_text(fusion_script(spec, extra))
    put("fusion", p, "Fusion 360 script: imports parts/*.stl in place and sketches the outline, ports, channel paths and "
                     "channel outlines")
    p = cad_dir / "BUILD.md"
    put("build", p, "step-by-step instructions to redraw the chip by hand in CAD")
    p.write_text(build_md(spec, violations, extra, [Path(v).relative_to(cad_dir).as_posix() for v in files.values()]))
    p = cad_dir / "README.md"
    p.write_text(f"# CAD for {cid}\n\nGenerated by autochip/cad.py from chips/{cid}.json (placeholder geometry).\n\n"
                 + "\n".join(readme) + "\n")
    files["readme"] = str(p)
    return files

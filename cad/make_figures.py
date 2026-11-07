"""Generate the design figures for a chip spec: SVG drawings that render on GitHub.

    python cad/make_figures.py                      # every spec in chip/
    python cad/make_figures.py --chip chip/<spec>.json

Writes, into cad/figures/:
  <chip_id>_layout.svg    plan view, to scale: chambers, channels, ports, lanes
  <chip_id>_stack.svg     the layer stack, and which process makes each layer
  <chip_id>_features.svg  every feature size against the process floors
  <chip_id>_barrier.svg   groove-barrier detail, only for chips that have one
  <chip_id>_schedule.svg  lane timing, only for chips whose protocol stages lanes
  <chip_id>_modes.svg     one mini plan view per operating mode, only for chips that are
                          routed by port pressure instead of valves

Channel geometry is imported from make_train_cad rather than re-derived, so a drawing
and the DXF cut path cannot disagree.

It also prints geometry warnings, which is half the point of drawing at all. In a
single-layer chip two channels that cross in plan view ARE one channel, and a port
that lands on a channel it does not belong to is a short circuit. Neither is visible
in a JSON spec and neither is checked by tools/check_chip_spec.py; both are obvious
the moment you draw it. See geometry_warnings().

SVG is written with presentation attributes only, no <style> block and no external
fonts, because that is the subset GitHub renders inside markdown.

Standard library only, and it only writes into cad/figures/.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_train_cad import PORT_SIZE_MM, clip_to_box  # noqa: E402  (sibling module)

ROOT = Path(__file__).resolve().parents[1]
FIGURES = ROOT / "cad" / "figures"
CHIP_DIR = ROOT / "chip"

# Chips whose protocol deliberately staggers its lanes. The schedule figure is drawn
# from the generator that owns the schedule, so the two cannot drift apart.
SCHEDULE_BUILDERS = {"module1_v1": "protocols/build_module1.py"}

# Process floors, for the feature figure. These are published/vendor typicals, NOT our
# measured numbers: cad/fabrication.json still has laser_kerf_um and
# min_embossed_feature_um set to null, and real values override these when they land.
FLOORS = [
    (1.0, "photolithography (SU-8)", "#6b5bb0"),
    (80.0, "CO2 laser, min feature", "#c0392b"),
    (127.0, "CNC, smallest end mill", "#bf7b2f"),
]
TIGHT_MM = 0.5          # thin-rib threshold for the clearance warnings
SCALE = 6.0             # px per mm in the layout figure

FONT = "DejaVu Sans, Helvetica, Arial, sans-serif"
INK, INK2, INK3 = "#1b2026", "#5a636d", "#8b949e"
PAPER, GRID = "#ffffff", "#eaeef2"
BOARD_F, BOARD_S = "#f6f8fa", "#c9d1d9"
CHAN_F, CHAN_S = "#dde2e8", "#8d97a3"
GROOVE_F, GROOVE_S = "#f3dcf1", "#a3449b"
INLET_C, OUTLET_C = "#2f7d32", "#c0392b"
TYPES = {
    "exchange": ("#cfe3f7", "#2f6fb0"),
    "split": ("#d7eed4", "#468a4a"),
    "pool": ("#fde6c8", "#bf7b2f"),
    "well": ("#e8e2f7", "#6b5bb0"),
}
TYPE_FALLBACK = ("#e6eaef", "#6e7781")
LANE_TINTS = ["#eaf2fb", "#fbf0e9", "#ecf6ec", "#f3eefb"]
LANE_INKS = ["#2f6fb0", "#bf7b2f", "#468a4a", "#6b5bb0"]


# --- SVG ------------------------------------------------------------------------------

def esc(s: object) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def n(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


class Svg:
    """A tiny SVG document. Presentation attributes only; see the module docstring."""

    def __init__(self, w: float, h: float, title: str):
        self.w, self.h = w, h
        self.body: list[str] = []
        self.title = title

    def add(self, s: str) -> None:
        self.body.append("  " + s)

    def rect(self, x, y, w, h, fill="none", stroke="none", sw=1.0, rx=0.0, dash=None, op=None):
        a = f'<rect x="{n(x)}" y="{n(y)}" width="{n(max(w, 0))}" height="{n(max(h, 0))}"'
        a += f' fill="{fill}" stroke="{stroke}"'
        if stroke != "none":
            a += f' stroke-width="{n(sw)}"'
        if rx:
            a += f' rx="{n(rx)}"'
        if dash:
            a += f' stroke-dasharray="{dash}"'
        if op is not None:
            a += f' opacity="{n(op)}"'
        self.add(a + "/>")

    def line(self, x0, y0, x1, y1, stroke=INK3, sw=1.0, dash=None, cap=None):
        a = f'<line x1="{n(x0)}" y1="{n(y0)}" x2="{n(x1)}" y2="{n(y1)}"'
        a += f' stroke="{stroke}" stroke-width="{n(sw)}"'
        if dash:
            a += f' stroke-dasharray="{dash}"'
        if cap:
            a += f' stroke-linecap="{cap}"'
        self.add(a + "/>")

    def poly(self, pts, fill="none", stroke="none", sw=1.0, dash=None, op=None):
        d = " ".join(f"{n(x)},{n(y)}" for x, y in pts)
        a = f'<polygon points="{d}" fill="{fill}" stroke="{stroke}"'
        if stroke != "none":
            a += f' stroke-width="{n(sw)}"'
        if dash:
            a += f' stroke-dasharray="{dash}"'
        if op is not None:
            a += f' opacity="{n(op)}"'
        self.add(a + "/>")

    def circle(self, cx, cy, r, fill="none", stroke="none", sw=1.0):
        a = f'<circle cx="{n(cx)}" cy="{n(cy)}" r="{n(r)}" fill="{fill}" stroke="{stroke}"'
        if stroke != "none":
            a += f' stroke-width="{n(sw)}"'
        self.add(a + "/>")

    def text(self, x, y, s, size=10.0, fill=INK, anchor="start", weight="normal", mono=False):
        fam = "ui-monospace, SFMono-Regular, Menlo, monospace" if mono else FONT
        a = f'<text x="{n(x)}" y="{n(y)}" font-family="{fam}" font-size="{n(size)}"'
        a += f' fill="{fill}" text-anchor="{anchor}"'
        if weight != "normal":
            a += f' font-weight="{weight}"'
        self.add(a + f">{esc(s)}</text>")

    def halo(self, x, y, s, size=10.0, fill=INK, anchor="start", weight="normal",
             mono=False, pad=3.0):
        """Text on a small opaque backing, so a label stays readable over a channel."""
        wpx = len(str(s)) * size * (0.60 if mono else 0.56)
        x0 = x if anchor == "start" else (x - wpx / 2 if anchor == "middle" else x - wpx)
        self.rect(x0 - pad, y - size * 0.84, wpx + 2 * pad, size * 1.16, PAPER)
        self.text(x, y, s, size, fill, anchor, weight, mono)

    def save(self, path: Path) -> Path:
        head = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{n(self.w)}" '
            f'height="{n(self.h)}" viewBox="0 0 {n(self.w)} {n(self.h)}" '
            f'role="img" aria-label="{esc(self.title)}">'
        )
        out = [head, f"  <title>{esc(self.title)}</title>",
               f'  <rect width="{n(self.w)}" height="{n(self.h)}" fill="{PAPER}"/>']
        out += self.body
        out.append("</svg>\n")
        path.write_text("\n".join(out), encoding="utf-8")
        return path


class View:
    """mm -> px, with y flipped: the chip spec has y up, SVG has y down."""

    def __init__(self, chip, scale, ox, oy):
        self.s, self.ox, self.oy = scale, ox, oy
        self.fy = chip["footprint"]["y"]

    def x(self, mm): return self.ox + mm * self.s
    def y(self, mm): return self.oy + (self.fy - mm) * self.s
    def d(self, mm): return mm * self.s
    def p(self, pt): return (self.x(pt[0]), self.y(pt[1]))


# --- geometry -------------------------------------------------------------------------

def spacer_of(chip):
    """The layer whose thickness sets the chamber height, as in make_train_cad."""
    depth = chip["chambers"][0]["size"][2]
    return next((l for l in chip["layers"] if abs(l["thickness_mm"] - depth) < 1e-6), None)


def anchors_of(chip):
    a = {c["id"]: (c["center"], c["size"]) for c in chip["chambers"]}
    for port in chip["ports"]:
        x, y, z = port["position"]
        a[port["id"]] = ([x, y, z], [PORT_SIZE_MM, PORT_SIZE_MM, 0])
    return a


def segments_of(chip):
    """Every channel as (channel, p0, p1, is_groove), clipped exactly as the DXF is."""
    anchors, spacer = anchors_of(chip), spacer_of(chip)
    out = []
    for ch in chip["channels"]:
        a, b = anchors.get(ch["from"]), anchors.get(ch["to"])
        if a is None or b is None:
            continue
        p0 = clip_to_box(a[0][:2], b[0][:2], a[0], a[1])
        p1 = clip_to_box(b[0][:2], a[0][:2], b[0], b[1])
        groove = bool(spacer) and ch["depth_mm"] < spacer["thickness_mm"] - 1e-6
        out.append((ch, p0, p1, groove))
    return out


def quad(p0, p1, width):
    """A channel as four corners, same maths as make_train_cad.channel_lines."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length = (dx * dx + dy * dy) ** 0.5
    if length < 1e-9:
        return []
    ux, uy = -dy / length * width / 2, dx / length * width / 2
    return [(p0[0] + ux, p0[1] + uy), (p1[0] + ux, p1[1] + uy),
            (p1[0] - ux, p1[1] - uy), (p0[0] - ux, p0[1] - uy)]


def _point_seg(p, a, b):
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-12:
        return ((p[0] - ax) ** 2 + (p[1] - ay) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    cx, cy = ax + t * dx, ay + t * dy
    return ((p[0] - cx) ** 2 + (p[1] - cy) ** 2) ** 0.5


def _seg_seg(a, b, c, d):
    """Minimum distance between two segments; 0 if they cross."""
    def cross(o, p, q):
        return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])
    d1, d2 = cross(c, d, a), cross(c, d, b)
    d3, d4 = cross(a, b, c), cross(a, b, d)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 0.0
    return min(_point_seg(a, c, d), _point_seg(b, c, d),
               _point_seg(c, a, b), _point_seg(d, a, b))


def geometry_warnings(chip):
    """Planar collisions and thin ribs. Returns (messages, tightest_clearance_mm).

    A single-layer chip has no vias: two channels that touch in plan view are one
    channel, and a port whose hole lands on a channel it is not connected to is a
    short circuit between whatever those two lines serve.
    """
    segs = segments_of(chip)
    ports = {p["id"]: p for p in chip["ports"]}
    msgs, tightest = [], (float("inf"), "")

    def note(gap, what):
        nonlocal tightest
        if gap < tightest[0]:
            tightest = (gap, what)

    for i, (c1, a1, b1, _) in enumerate(segs):
        for c2, a2, b2, _ in segs[i + 1:]:
            shared = {c1["from"], c1["to"]} & {c2["from"], c2["to"]}
            # Channels meeting at a port genuinely merge there: that is what a port is.
            if shared & ports.keys():
                continue
            gap = _seg_seg(a1, b1, a2, b2) - (c1["width_mm"] + c2["width_mm"]) / 2
            what = f"channels '{c1['id']}' and '{c2['id']}'"
            note(gap, what)
            if gap < 0 and not shared:
                msgs.append(f"ERROR  {what} overlap in plan view ({-gap:.2f} mm of overlap). "
                            f"In a single-layer chip that makes them one channel.")
            elif gap < 0:
                msgs.append(f"ERROR  {what} overlap by {-gap:.2f} mm where they leave "
                            f"'{sorted(shared)[0]}'.")
            elif gap < TIGHT_MM:
                msgs.append(f"WARN   {what} are {gap:.2f} mm apart, a thinner rib than "
                            f"{TIGHT_MM:g} mm.")

    for pid, port in ports.items():
        pos = port["position"][:2]
        for ch, a, b, _ in segs:
            if pid in (ch["from"], ch["to"]):
                continue
            gap = _point_seg(pos, a, b) - PORT_SIZE_MM / 2 - ch["width_mm"] / 2
            what = f"port '{pid}' and channel '{ch['id']}'"
            note(gap, what)
            if gap < 0:
                msgs.append(f"ERROR  {what} overlap by {-gap:.2f} mm. The port is drilled "
                            f"into a channel it is not connected to: a short circuit "
                            f"between '{ch['from']}' and '{ch['to']}'.")
            elif gap < TIGHT_MM:
                msgs.append(f"WARN   {what} are {gap:.2f} mm apart, a thinner rib than "
                            f"{TIGHT_MM:g} mm.")
    return msgs, tightest


def lanes_of(chip):
    """Rows of >=2 non-pool chambers sharing an id prefix: one differentiation lane.

    Pool chambers are excluded because a pool is shared by every lane by definition.
    A serial train (one row, mixed prefixes) yields no lanes, which is correct.
    """
    rows: dict[float, list] = {}
    for c in chip["chambers"]:
        if c.get("type") == "pool":
            continue
        rows.setdefault(round(c["center"][1], 3), []).append(c)
    lanes = []
    for y, cs in sorted(rows.items(), reverse=True):
        prefixes = {c["id"].split("_")[0] for c in cs}
        if len(cs) >= 2 and len(prefixes) == 1:
            lanes.append((prefixes.pop(), y, sorted(cs, key=lambda c: c["center"][0])))
    return lanes


def load_schedule(chip_id):
    """The generator that owns this chip's lane schedule, or None.

    Absent, unimportable or incomplete builders all give None, so the figures still
    build while a protocol generator is being written.
    """
    rel = SCHEDULE_BUILDERS.get(chip_id)
    if not rel or not (ROOT / rel).exists():
        return None
    try:
        spec = importlib.util.spec_from_file_location("schedule_builder", ROOT / rel)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        return None
    if not all(hasattr(mod, a) for a in ("LANES", "MATURE_DAY", "INTERACT_DAYS")):
        return None
    return mod


def lane_names(chip, sched):
    """prefix -> the lane's name in the protocol, when a schedule generator exists."""
    if sched is None:
        return {}
    out = {}
    for name, entry in sched.LANES.items():
        # A lane entry is a dict keyed by 'chamber', or an older positional tuple whose
        # first item is the chamber id. Accept either rather than tying the figure to
        # one generator's shape.
        chamber = entry.get("chamber") if isinstance(entry, dict) else entry[0]
        if chamber:
            out[str(chamber).split("_")[0]] = name
    return out


# --- figure: plan view ----------------------------------------------------------------

def _dim_h(svg, x0, x1, y, label):
    svg.line(x0, y, x1, y, INK3, 0.8)
    for xx in (x0, x1):
        svg.line(xx, y - 4, xx, y + 4, INK3, 0.8)
    svg.rect((x0 + x1) / 2 - 34, y - 7, 68, 14, PAPER)
    svg.text((x0 + x1) / 2, y + 4, label, 9, INK2, "middle")


def _dim_v(svg, y0, y1, x, label):
    svg.line(x, y0, x, y1, INK3, 0.8)
    for yy in (y0, y1):
        svg.line(x - 4, yy, x + 4, yy, INK3, 0.8)
    svg.text(x + 7, (y0 + y1) / 2 + 3, label, 9, INK2, "start")


def _swatch(svg, x, y, fill, stroke, label, w=14, h=10):
    svg.rect(x, y - h + 2, w, h, fill, stroke, 1.0, rx=1.5)
    svg.text(x + w + 6, y, label, 8.5, INK2)
    return x + w + 12 + len(label) * 4.9


def barrier_band(chip, ch):
    """The shared chamber edge a groove channel actually spans: (x, y, w, h, axis)."""
    by = {c["id"]: c for c in chip["chambers"]}
    a, b = by.get(ch["from"]), by.get(ch["to"])
    if not a or not b:
        return None
    ax0, ax1 = a["center"][0] - a["size"][0] / 2, a["center"][0] + a["size"][0] / 2
    bx0, bx1 = b["center"][0] - b["size"][0] / 2, b["center"][0] + b["size"][0] / 2
    ay0, ay1 = a["center"][1] - a["size"][1] / 2, a["center"][1] + a["size"][1] / 2
    by0, by1 = b["center"][1] - b["size"][1] / 2, b["center"][1] + b["size"][1] / 2
    ox0, ox1 = max(ax0, bx0), min(ax1, bx1)
    oy0, oy1 = max(ay0, by0), min(ay1, by1)
    if ox1 > ox0 and oy1 <= oy0:
        return (ox0, min(ay1, by1), ox1 - ox0, max(ay0, by0) - min(ay1, by1), "h")
    if oy1 > oy0 and ox1 <= ox0:
        return (min(ax1, bx1), oy0, max(ax0, bx0) - min(ax1, bx1), oy1 - oy0, "v")
    return None


def fig_layout(chip, sched):
    fx, fy = chip["footprint"]["x"], chip["footprint"]["y"]
    lanes, names = lanes_of(chip), lane_names(chip, sched)
    pad_l = 104 if lanes else 44
    pad_t, pad_r = 56, 86
    board_h = fy * SCALE
    w = pad_l + fx * SCALE + pad_r
    h = pad_t + board_h + 154
    svg = Svg(w, h, f"{chip['id']}: plan view")
    v = View(chip, SCALE, pad_l, pad_t)
    bottom = v.y(0)

    svg.text(pad_l, 24, chip["name"], 14.5, INK, weight="bold")
    svg.text(pad_l, 41, f"chip/{chip['id']}.json   ·   footprint {fx:g} × {fy:g} mm   ·   "
                        f"plan view, to scale   ·   all dimensions mm", 9.5, INK2)
    svg.text(w - pad_r + 72, 24, "generated by", 8, INK3, "end")
    svg.text(w - pad_r + 72, 36, "cad/make_figures.py", 8, INK3, "end", mono=True)
    svg.text(w - pad_r + 72, 48, "do not edit by hand", 8, INK3, "end")

    svg.rect(v.x(0), v.y(fy), fx * SCALE, board_h, BOARD_F, BOARD_S, 1.3, rx=5)

    for i, (prefix, y, cs) in enumerate(lanes):
        x1 = max(c["center"][0] + c["size"][0] / 2 for c in cs)
        bh = max(c["size"][1] for c in cs) + 3.0
        svg.rect(v.x(0.8), v.y(y + bh / 2), v.d(x1 + 2.6), v.d(bh),
                 LANE_TINTS[i % len(LANE_TINTS)], "none", rx=4)
        label = f"{names.get(prefix, prefix)} lane" if names else f"{prefix}_ lane"
        svg.text(v.x(0) - 12, v.y(y) - 2, label, 10, LANE_INKS[i % len(LANE_INKS)],
                 "end", weight="bold")
        svg.text(v.x(0) - 12, v.y(y) + 11, f"ids {prefix}_*", 8, INK3, "end", mono=True)

    grooves = []
    for ch, p0, p1, is_groove in segments_of(chip):
        if is_groove:
            grooves.append((ch, p0, p1))
            continue
        pts = quad(p0, p1, ch["width_mm"])
        if pts:
            svg.poly([v.p(c) for c in pts], CHAN_F, CHAN_S, 0.8)

    for c in chip["chambers"]:
        fill, stroke = TYPES.get(c.get("type"), TYPE_FALLBACK)
        sx, sy = c["size"][0], c["size"][1]
        px, py = v.x(c["center"][0] - sx / 2), v.y(c["center"][1] + sy / 2)
        svg.rect(px, py, v.d(sx), v.d(sy), fill, stroke, 1.3, rx=2.5)
        cx, cy = v.x(c["center"][0]), v.y(c["center"][1])
        mesh = f"  ·  mesh {c['mesh_pore_um']:g} µm" if c.get("mesh_pore_um") else ""
        if v.d(sx) >= 66 and v.d(sy) >= 36:
            svg.text(cx, cy - 4, c["id"], 9.5, stroke, "middle", "bold", mono=True)
            svg.text(cx, cy + 9, f"{sx:g} × {sy:g} mm", 8, INK2, "middle")
            svg.text(cx, cy + 20, f"{c['volume_ul']:g} µL{mesh}", 8, INK2, "middle")
        else:
            svg.halo(cx, py - 16, c["id"], 9, stroke, "middle", "bold", mono=True)
            svg.halo(cx, py - 6, f"{sx:g} × {sy:g} mm · {c['volume_ul']:g} µL{mesh}",
                     7.5, INK2, "middle")

    # After the chambers: the barrier sits in a 0.5 mm gap, so a chamber fill would
    # otherwise paint over most of it.
    for ch, p0, p1 in grooves:
        band = barrier_band(chip, ch)
        length_mm = ((p1[0] - p0[0]) ** 2 + (p1[1] - p0[1]) ** 2) ** 0.5
        if band is None:
            continue
        bx, by_, bw, bh, axis = band
        if axis == "h":
            px, pw = v.x(bx), v.d(bw)
            ph = max(v.d(bh), 11.0)
            py = (v.y(by_) + v.y(by_ + bh)) / 2 - ph / 2
        else:
            py, ph = v.y(by_ + bh), v.d(bh)
            pw = max(v.d(bw), 11.0)
            px = (v.x(bx) + v.x(bx + bw)) / 2 - pw / 2
        svg.rect(px, py, pw, ph, GROOVE_F, GROOVE_S, 1.1)
        step = 7.0
        if axis == "h":
            k = px + step
            while k < px + pw - 1:
                svg.line(k, py + 1, k, py + ph - 1, GROOVE_S, 0.7)
                k += step
        else:
            k = py + step
            while k < py + ph - 1:
                svg.line(px + 1, k, px + pw - 1, k, GROOVE_S, 0.7)
                k += step
        # Left of the barrier is the empty column between the two chamber banks; right of
        # it is where the collection channels run, so a label there sits on top of them.
        svg.halo(px - 10, py + ph / 2 + 3, ch["id"], 8, GROOVE_S, "end", mono=True)
        svg.halo(px - 10, py + ph / 2 + 14, f"~{GROOVE_COUNT} grooves, {length_mm * 1000:g} µm",
                 7.5, INK3, "end")

    balloons = []
    for i, port in enumerate(chip["ports"], start=1):
        x, y = port["position"][0], port["position"][1]
        colour = INLET_C if port.get("type") == "inlet" else OUTLET_C
        svg.circle(v.x(x), v.y(y), v.d(PORT_SIZE_MM / 2), PAPER, colour, 2.0)
        svg.circle(v.x(x), v.y(y), v.d(PORT_SIZE_MM / 2) - 3.5, colour, "none")
        dx, dy = x - fx / 2, y - fy / 2
        mag = max((dx * dx + dy * dy) ** 0.5, 1e-6)
        bx = v.x(x) + dx / mag * 19
        by_ = v.y(y) - dy / mag * 19
        svg.circle(bx, by_, 7.5, PAPER, colour, 1.2)
        svg.text(bx, by_ + 3.2, str(i), 8.5, colour, "middle", "bold")
        balloons.append((i, port, colour))

    _dim_h(svg, v.x(0), v.x(fx), bottom + 26, f"{fx:g}")
    _dim_v(svg, v.y(fy), v.y(0), v.x(fx) + 24, f"{fy:g}")
    svg.line(pad_l, bottom + 46, w - pad_r + 72, bottom + 46, GRID, 1.0)

    row = bottom + 64
    x = pad_l
    seen = []
    for c in chip["chambers"]:
        t = c.get("type")
        if t not in seen:
            seen.append(t)
    for t in seen:
        fill, stroke = TYPES.get(t, TYPE_FALLBACK)
        x = _swatch(svg, x, row, fill, stroke, f"{t} chamber")
    x = _swatch(svg, x, row, CHAN_F, CHAN_S, "channel (0.3 mm deep, in the spacer)")
    if grooves:
        x = _swatch(svg, x, row, GROOVE_F, GROOVE_S, "groove barrier (exaggerated)")
    svg.circle(x + 6, row - 3, 5, PAPER, INLET_C, 1.8)
    svg.text(x + 16, row, "inlet", 8.5, INK2)
    svg.circle(x + 58, row - 3, 5, PAPER, OUTLET_C, 1.8)
    svg.text(x + 68, row, "outlet", 8.5, INK2)

    row += 20
    svg.text(pad_l, row, "ports", 8.5, INK3, weight="bold")
    px = pad_l + 38
    for i, port, colour in balloons:
        label = f"{i}  {port['id']}"
        if px + len(label) * 5.4 + 20 > w - pad_r + 72:
            row += 14
            px = pad_l + 38
        svg.text(px, row, label, 8.5, colour, mono=True)
        px += len(label) * 5.4 + 20

    msgs, tightest = geometry_warnings(chip)
    row += 22
    for note in _layout_notes(chip, grooves, tightest, msgs):
        svg.text(pad_l, row, note, 8.5, INK2)
        row += 13
    svg.h = max(svg.h, row + 14)
    return svg, msgs, tightest


def _layout_notes(chip, grooves, tightest, msgs):
    spacer = spacer_of(chip)
    out = []
    if spacer:
        out.append(f"Chamber height is the {spacer['thickness_mm']:g} mm "
                   f"{spacer['material']} '{spacer['id']}' layer, so every chamber and "
                   f"channel is a cut-out in one flat sheet.")
    if grooves:
        g = grooves[0][0]
        out.append(f"Groove barriers are drawn thicker than they are: each is an ARRAY of "
                   f"~100 grooves {g['width_mm'] * 1000:g} µm wide and "
                   f"{g['depth_mm'] * 1000:g} µm tall, which is invisible at this scale. "
                   f"They are not valves and cannot be closed.")
        out.append("They need photolithography, not the laser: see the stack and feature "
                   "figures.")
    if tightest[0] < float("inf"):
        verdict = "no overlaps" if not any(m.startswith("ERROR") for m in msgs) \
            else f"{sum(m.startswith('ERROR') for m in msgs)} OVERLAP(S) — see the run log"
        out.append(f"Tightest clearance between unconnected features: "
                   f"{tightest[0]:.2f} mm, between {tightest[1]}. Planar check: {verdict}.")
    return out


# --- figure: layer stack and fabrication route ----------------------------------------

MATERIAL_FILL = {
    "acrylic": ("#e3eaf2", "#8d9bab"),
    "glass": ("#d9edf3", "#6f9fad"),
    "pdms": ("#efdcf2", "#a3449b"),
    "psa": ("#f6e9cf", "#bf9b4f"),
}
LASER_C, LITHO_C, STOCK_C = "#c0392b", "#6b5bb0", "#6e7781"


def groove_layer_of(chip):
    return next((l for l in chip["layers"] if "groove" in l["id"].lower()), None)


def process_of(layer, chip, spacer, groove_layer):
    if groove_layer is not None and layer["id"] == groove_layer["id"]:
        return ("photolithography", "SU-8 master, then cast: the groove array", LITHO_C)
    if spacer is not None and layer["id"] == spacer["id"]:
        return ("laser", "cut right through: chamber cavities + channels", LASER_C)
    if layer is chip["layers"][-1]:
        return ("laser", "cut to outline, ports drilled through", LASER_C)
    return ("stock", "stock sheet, cut to outline only", STOCK_C)


def fig_stack(chip):
    layers, spacer = list(chip["layers"]), spacer_of(chip)
    groove_layer = groove_layer_of(chip)
    has_grooves = any(g for _, _, _, g in segments_of(chip) if g)

    SC, slab_w, off, offy, gap = 30.0, 300.0, 34.0, 18.0, 26.0
    pitch = 68.0            # text rows are evenly spaced; the slabs stay to scale
    pad_l, pad_t = 50, 56
    hs = [max(l["thickness_mm"] * SC, 10.0) for l in layers]
    stack_h = sum(hs) + gap * (len(layers) - 1) + offy
    body_h = max(stack_h, pitch * len(layers))
    w = pad_l + slab_w + off + 430
    h = pad_t + body_h + 150
    svg = Svg(w, h, f"{chip['id']}: layer stack and fabrication route")

    svg.text(pad_l, 24, "Layer stack and how each layer is made", 14.5, INK, weight="bold")
    svg.text(pad_l, 41, f"chip/{chip['id']}.json   ·   exploded, thickness to scale   ·   "
                        f"{len(layers)} layers, {sum(l['thickness_mm'] for l in layers):g} mm total",
             9.5, INK2)

    y, ty = pad_t + offy, pad_t
    tx = pad_l + slab_w + off + 56
    for layer, hh in zip(reversed(layers), reversed(hs)):
        fill, stroke = MATERIAL_FILL.get(layer["material"], ("#e6eaef", "#8b949e"))
        kind, detail, colour = process_of(layer, chip, spacer, groove_layer)
        svg.line(pad_l + slab_w + off + 4, y - offy / 2 + hh / 2, tx - 12, ty + 14,
                 "#d6dbe1", 1.0)
        svg.poly([(pad_l, y), (pad_l + off, y - offy),
                  (pad_l + slab_w + off, y - offy), (pad_l + slab_w, y)], fill, stroke, 1.1)
        svg.poly([(pad_l + slab_w, y), (pad_l + slab_w + off, y - offy),
                  (pad_l + slab_w + off, y - offy + hh), (pad_l + slab_w, y + hh)],
                 stroke, stroke, 1.1, op=0.5)
        svg.rect(pad_l, y, slab_w, hh, fill, stroke, 1.1)

        if spacer is not None and layer["id"] == spacer["id"]:
            for k in range(3):
                cx = pad_l + 58 + k * 84
                svg.rect(cx, y + 1, 46, hh - 2, PAPER, stroke, 0.8)
        if groove_layer is not None and layer["id"] == groove_layer["id"]:
            for k in range(14):
                gx = pad_l + 46 + k * 15
                svg.line(gx, y + hh - 2, gx, y + hh - 7, GROOVE_S, 1.0)

        svg.text(tx, ty + 14, layer["id"], 11, INK, weight="bold", mono=True)
        svg.text(tx, ty + 28, f"{layer['material']}   ·   {layer['thickness_mm']:g} mm",
                 9, INK2)
        svg.rect(tx, ty + 36, 100, 16, PAPER, colour, 1.1, rx=8)
        svg.text(tx + 50, ty + 47.5, kind, 8.5, colour, "middle", "bold")
        svg.text(tx + 110, ty + 47.5, detail, 8.5, INK2)
        y += hh + gap
        ty += pitch

    row = pad_t + body_h + 46
    svg.line(pad_l, row - 18, w - 40, row - 18, GRID, 1.0)
    if spacer is not None:
        svg.text(pad_l, row, f"The chamber height IS the spacer: {spacer['thickness_mm']:g} mm. "
                             f"That is why a chamber is a rectangle in a flat sheet rather than "
                             f"a milled pocket.", 9, INK2)
        row += 15
    if has_grooves and groove_layer is not None:
        svg.text(pad_l, row, f"Two processes, not one. '{spacer['id'] if spacer else '?'}' goes to "
                             f"the laser cutter; '{groove_layer['id']}' cannot, because its "
                             f"features are microns tall.", 9, INK, weight="bold")
        row += 15
        svg.text(pad_l, row, "cad/exports/<chip>.dxf keeps them apart: layers CHAMBERS and "
                             "CHANNELS are the cut path, layer GROOVES is a mask.", 9, INK2)
        row += 15
    elif has_grooves:
        svg.text(pad_l, row, "WARNING: this chip has sub-spacer channels but no layer to hold "
                             "them. Add a cast layer to chip.layers.", 9, LASER_C, weight="bold")
        row += 15
    else:
        svg.text(pad_l, row, "One process: every feature on this chip is within reach of the "
                             "laser cutter. See the feature figure.", 9, INK2)
        row += 15
    svg.text(pad_l, row, "Process floors are vendor typicals, not measured: "
                         "cad/fabrication.json still has laser_kerf_um = null.", 8.5, INK3)
    svg.h = row + 26
    return svg


# --- figure: feature sizes against the process floors ---------------------------------

def feature_rows(chip):
    """(label, micrometres, fabricated_by_us, detail), smallest first."""
    rows = []
    inplane = min(min(c["size"][0], c["size"][1]) for c in chip["chambers"])
    small = min(chip["chambers"], key=lambda c: min(c["size"][0], c["size"][1]))
    rows.append(("chamber, smallest side", inplane * 1000, True, small["id"]))
    for d in sorted({c["size"][2] for c in chip["chambers"]}):
        rows.append(("chamber depth", d * 1000, True, "set by the spacer"))
    for wd in sorted({c["width_mm"] for c in chip["channels"]}):
        ids = [c["id"] for c in chip["channels"] if c["width_mm"] == wd]
        rows.append(("channel width", wd * 1000, True,
                     ids[0] if len(ids) == 1 else f"{len(ids)} channels"))
    for dp in sorted({c["depth_mm"] for c in chip["channels"]}):
        ids = [c["id"] for c in chip["channels"] if c["depth_mm"] == dp]
        rows.append(("channel depth", dp * 1000, True,
                     ids[0] if len(ids) == 1 else f"{len(ids)} channels"))
    rows.append(("tubing port", PORT_SIZE_MM * 1000, True, "drilled in the lid"))
    for m in sorted({c["mesh_pore_um"] for c in chip["chambers"] if c.get("mesh_pore_um")}):
        rows.append(("retention mesh pore", m, False, "bought sheet, NOT fabricated"))
    return sorted(rows, key=lambda r: r[1])


def fig_features(chip):
    rows = feature_rows(chip)
    pad_l, pad_t = 232, 86
    axw, axh = 470.0, 20.0
    w = pad_l + axw + 190
    h = pad_t + len(rows) * axh + 150
    svg = Svg(w, h, f"{chip['id']}: feature sizes against the process floors")

    def lx(um):
        return pad_l + (math.log10(max(um, 1.0)) / 5.0) * axw

    fab = [r for r in rows if r[2]]
    smallest = min(r[1] for r in fab) if fab else float("inf")
    # The lowest floor of any process that CUTS. Below it, a feature has to be cast.
    laser = min(v for v, label, _ in FLOORS if "lithograph" not in label.lower())

    svg.text(40, 24, "Every feature on this chip, against what each process can make",
             14.5, INK, weight="bold")
    svg.text(40, 41, f"chip/{chip['id']}.json   ·   log scale   ·   micrometres", 9.5, INK2)

    y0, y1 = pad_t - 14, pad_t + len(rows) * axh
    svg.rect(lx(1), y0, lx(laser) - lx(1), y1 - y0, "#fdeeec", "none")
    svg.text(lx(laser) - 8, y0 - 24, "no laser cutter reaches this", 8.5, LASER_C, "end",
             weight="bold")
    svg.text(lx(laser) - 8, y0 - 12, "photolithography territory", 8.5, LASER_C, "end")

    # Floors that sit close together on a log axis get their own row, or the labels merge.
    taken: list[tuple[float, float, int]] = []
    for um, label, colour in FLOORS:
        x, half = lx(um), max(len(label) * 2.3, 30.0)
        tier = 0
        while any(t == tier and x - half < hi and lo < x + half for lo, hi, t in taken):
            tier += 1
        taken.append((x - half, x + half, tier))
        base = y1 + 30 + tier * 26
        svg.line(x, y0, x, y1 + 22 + tier * 26, colour, 1.3, dash="4 3")
        svg.text(x, base, f"{um:g} µm", 8.5, colour, "middle", weight="bold")
        svg.text(x, base + 12, label, 8, colour, "middle")
    tiers = max(t for _, _, t in taken) + 1 if taken else 1

    for e in range(6):
        um = 10.0 ** e
        svg.line(lx(um), y1, lx(um), y1 + 5, INK3, 0.8)
        pretty = f"{um:g} µm" if um < 1000 else f"{um / 1000:g} mm"
        svg.text(lx(um), y1 + 17, pretty, 8, INK3, "middle")
    svg.line(lx(1), y1, lx(10 ** 5), y1, INK3, 1.0)

    y = pad_t
    for label, um, fabricated, detail in rows:
        svg.line(pad_l, y, lx(um), y, GRID, 1.0)
        if fabricated:
            colour = LITHO_C if um < laser else "#2f6fb0"
            svg.circle(lx(um), y, 5.0, colour, PAPER, 1.4)
        else:
            colour = INK3
            svg.circle(lx(um), y, 5.0, PAPER, colour, 1.8)
        svg.text(pad_l - 12, y + 3.4, label, 9, INK, "end")
        svg.text(pad_l - 12, y + 14, detail, 7.5, INK3, "end")
        pretty = f"{um:g} µm" if um < 1000 else f"{um:g} µm  ({um / 1000:g} mm)"
        svg.text(lx(um) + 11, y + 3.4, pretty, 8.5, colour, weight="bold")
        y += axh

    row = y1 + 44 + tiers * 26
    svg.line(40, row - 14, w - 40, row - 14, GRID, 1.0)
    if smallest < laser:
        svg.text(40, row, f"Smallest feature we have to make: {smallest:g} µm, below every "
                          f"cutting floor. This chip needs TWO processes.", 9.5, INK,
                 weight="bold")
    else:
        svg.text(40, row, f"Smallest feature we have to make: {smallest:g} µm, clear of every "
                          f"floor. One process is enough.", 9.5, INK, weight="bold")
    row += 16
    svg.text(40, row, "Hollow markers are bought parts. A 5 µm mesh pore is below the laser "
                      "floor too, but nobody has to cut it: it arrives as a sheet.", 8.5, INK2)
    row += 14
    svg.text(40, row, "Floors are vendor/published typicals. cad/fabrication.json has "
                      "laser_kerf_um = null, so tools/check_chip_spec.py still reports a TODO.",
             8.5, INK3)
    svg.h = row + 26
    return svg


# --- figure: groove barrier detail ----------------------------------------------------

GROOVE_COUNT = 100       # design intent; the schema can only carry one groove
CELL_UM = 15.0           # a human cell body, mid-range
AXON_UM = 1.0


def fig_barrier(chip):
    """Detail of the groove array. None for chips that have no sub-spacer channel."""
    segs = [(ch, p0, p1) for ch, p0, p1, g in segments_of(chip) if g]
    if not segs:
        return None
    ch, p0, p1 = segs[0]
    gw_um = ch["width_mm"] * 1000
    gh_um = ch["depth_mm"] * 1000
    glen_um = ((p1[0] - p0[0]) ** 2 + (p1[1] - p0[1]) ** 2) ** 0.5 * 1000
    band = barrier_band(chip, ch)
    edge_mm = (band[2] if band and band[4] == "h" else band[3]) if band else 0.0
    pitch_um = edge_mm * 1000 / GROOVE_COUNT if edge_mm else 0.0

    S = 0.55                      # px per micrometre, plan detail
    shown = 5
    plan_w = shown * pitch_um * S if pitch_um else 500.0
    pad_l, pad_t = 60, 96
    w = max(pad_l + plan_w + 300, 980)
    svg = Svg(w, 760, f"{chip['id']}: groove barrier detail")

    svg.text(pad_l, 24, "The barrier, drawn at its real size", 14.5, INK, weight="bold")
    svg.text(pad_l, 41, f"{ch['id']}   ·   {gw_um:g} × {gh_um:g} × {glen_um:g} µm per groove   "
                        f"·   ~{GROOVE_COUNT} grooves across the {edge_mm:g} mm shared edge",
             9.5, INK2)
    svg.text(pad_l, 58, "This is not a valve. It has no moving part and cannot be closed.",
             9.5, LASER_C, weight="bold")

    # --- plan view --------------------------------------------------------------------
    chamber_h = 86.0
    gl = glen_um * S
    y_top = pad_t
    svg.text(pad_l, y_top - 12, "PLAN  ·  looking down", 9, INK3, weight="bold")
    a_id, b_id = ch["from"], ch["to"]
    svg.rect(pad_l, y_top, plan_w, chamber_h, "#cfe3f7", "#2f6fb0", 1.2, rx=3)
    svg.text(pad_l + plan_w / 2, y_top + chamber_h / 2 + 4, a_id, 10, "#2f6fb0", "middle",
             "bold", mono=True)
    yb = y_top + chamber_h + gl
    svg.rect(pad_l, yb, plan_w, chamber_h, "#fde6c8", "#bf7b2f", 1.2, rx=3)
    svg.text(pad_l + plan_w / 2, yb + chamber_h / 2 + 4, b_id, 10, "#bf7b2f", "middle",
             "bold", mono=True)
    svg.rect(pad_l, y_top + chamber_h, plan_w, gl, "#faf4f9", "none")
    for k in range(shown):
        gx = pad_l + pitch_um * S * (k + 0.5) - gw_um * S / 2
        svg.rect(gx, y_top + chamber_h, max(gw_um * S, 2.0), gl, GROOVE_F, GROOVE_S, 1.0)
    svg.line(pad_l, y_top + chamber_h, pad_l + plan_w, y_top + chamber_h, "#2f6fb0", 1.0)
    svg.line(pad_l, yb, pad_l + plan_w, yb, "#bf7b2f", 1.0)

    gx0 = pad_l + pitch_um * S * 0.5
    _dim_v(svg, y_top + chamber_h, yb, pad_l + plan_w + 26, f"{glen_um:g} µm long")
    _dim_h(svg, gx0, gx0 + pitch_um * S, yb + chamber_h + 26, f"{pitch_um:g} µm pitch")
    svg.text(gx0 + 8, y_top + chamber_h + gl / 2, f"{gw_um:g} µm wide", 8.5, GROOVE_S)
    svg.text(pad_l + plan_w + 26, y_top + 16, f"only {shown} of ~{GROOVE_COUNT} grooves",
             8.5, INK3)
    svg.text(pad_l + plan_w + 26, y_top + 30, "are drawn", 8.5, INK3)

    # --- cross-section ----------------------------------------------------------------
    CS = 5.2                      # px per micrometre, section
    sy = yb + chamber_h + 118
    svg.text(pad_l, sy - 14, "SECTION  ·  along one groove, looking from the side",
             9, INK3, weight="bold")
    floor_y = sy + 172
    gap_px = gh_um * CS
    sec_w = plan_w + 150
    step_x = pad_l + 236          # where the chamber ends and the groove begins
    ceil_y = floor_y - 130        # chamber ceiling: DRAWN height, not to scale
    xend = pad_l + sec_w

    svg.rect(pad_l, floor_y, sec_w, 30, "#d9edf3", "#6f9fad", 1.0)
    svg.text(pad_l + 8, floor_y + 20, "floor", 8.5, INK2)
    svg.rect(pad_l, ceil_y, step_x - pad_l, floor_y - ceil_y, "#f8fbfd", "none")
    svg.rect(step_x, floor_y - gap_px, xend - step_x, gap_px, GROOVE_F, "none")
    svg.poly([(pad_l, ceil_y - 32), (xend, ceil_y - 32), (xend, floor_y - gap_px),
              (step_x, floor_y - gap_px), (step_x, ceil_y), (pad_l, ceil_y)],
             "#e3eaf2", "#8d9bab", 1.0)
    svg.text(pad_l + 8, ceil_y - 12, "lid", 8.5, INK2)
    _dim_v(svg, floor_y - gap_px, floor_y, xend + 18, f"{gh_um:g} µm")
    svg.text(pad_l + 8, ceil_y + 16, f"chamber, {chip['chambers'][0]['size'][2] * 1000:g} µm "
                                     f"tall — drawn short, not to scale", 8, INK3)
    svg.text(step_x + 12, floor_y - gap_px - 14, f"groove, {gh_um:g} µm tall — to scale",
             8, GROOVE_S, weight="bold")

    cell_r = CELL_UM * CS / 2
    cell_x = step_x - cell_r - 16
    svg.circle(cell_x, floor_y - cell_r, cell_r, "#f6d6d2", "#c0392b", 1.6)
    svg.text(cell_x, floor_y - cell_r + 3.4, "cell", 8.5, "#c0392b", "middle", "bold")
    svg.text(cell_x, floor_y + 48, f"cell body ~{CELL_UM:g} µm across", 8.5, "#c0392b",
             "middle", weight="bold")
    svg.text(cell_x, floor_y + 60, f"{CELL_UM / gh_um:.0f}× the groove: mostly blocked",
             8.5, "#c0392b", "middle")

    ay = floor_y - gap_px / 2
    svg.line(cell_x + cell_r - 6, ay, xend - 14, ay, "#2f7d32",
             max(AXON_UM * CS, 1.8), cap="round")
    svg.text(xend - 14, ay - 13, f"axon ~{AXON_UM:g} µm, plus soluble factors "
                                 f"and vesicles: through", 8.5, "#2f7d32", "end",
             weight="bold")

    row = floor_y + 92
    svg.line(pad_l, row - 16, w - 40, row - 16, GRID, 1.0)
    for note in [
        f"Published geometry: Taylor 2003 (10.1021/la026417v) 10 × 3 × 150 µm, 120 grooves; "
        f"Taylor 2005 (10.1038/nmeth777) adds ≥450 µm; Tujula 2025 10 × 3.5 × 250 µm.",
        "Isolation is a MAINTAINED hydrostatic difference, not a closed valve. Taylor 2005 "
        "held ~4.7 × 10³ across the barrier for over 20 h. The protocol levels the volumes "
        "to open it.",
        f"{gh_um:g} µm leaks cells: Goshi 2022 (10.1039/d2lc00683a) counted 31.8 ± 30.3 "
        f"escapes at 3 µm against 0.25 ± 0.65 at 1.5 µm. It stays at {gh_um:g} µm because "
        f"axons have to cross.",
        "That is what the per-lane barcode is for: a migrated cell keeps its own label, so "
        "it reads as data rather than contamination.",
    ]:
        svg.text(pad_l, row, note, 8.5, INK2)
        row += 14
    svg.h = row + 20
    return svg


# --- figure: lane schedule ------------------------------------------------------------

def fig_schedule(chip, sched):
    if sched is None:
        return None
    lanes = sched.LANES
    mature, interact = sched.MATURE_DAY, sched.INTERACT_DAYS
    end = mature + interact
    names = list(lanes)
    pad_l, pad_t = 150, 92
    axw, rowh = 640.0, 34.0
    w = pad_l + axw + 210
    svg = Svg(w, 620, f"{chip['id']}: lane schedule")

    def dx(day):
        return pad_l + day / end * axw

    svg.text(40, 24, "Three lineages, three different lengths, one finish date", 14.5, INK,
             weight="bold")
    svg.text(40, 41, f"from protocols/{SCHEDULE_BUILDERS[chip['id']].split('/')[-1]}   ·   "
                     f"days   ·   {end} days end to end", 9.5, INK2)

    def axis(y, label):
        svg.line(pad_l, y, pad_l + axw, y, INK3, 1.0)
        for d in range(0, end + 1, 10):
            svg.line(dx(d), y, dx(d), y + 5, INK3, 0.8)
            svg.text(dx(d), y + 17, str(d), 8, INK3, "middle")
        svg.text(pad_l + axw / 2, y + 32, label, 8.5, INK3, "middle")

    # --- panel A: as built -------------------------------------------------------------
    svg.text(40, pad_t - 20, "AS BUILT  ·  staggered seeding", 10, INK, weight="bold")
    y = pad_t
    for i, name in enumerate(names):
        s1, s2, seed, passage, _ = lanes[name]
        ink = LANE_INKS[i % len(LANE_INKS)]
        tint = LANE_TINTS[i % len(LANE_TINTS)]
        svg.text(pad_l - 14, y + 14, name, 10, ink, "end", weight="bold")
        svg.text(pad_l - 14, y + 26, f"{mature - seed} days", 8, INK3, "end")
        svg.rect(dx(0), y + 4, axw, 20, "#fafbfc", "none", rx=3)
        svg.rect(dx(seed), y + 4, dx(passage) - dx(seed), 20, tint, ink, 1.2, rx=3)
        svg.rect(dx(passage), y + 4, dx(mature) - dx(passage), 20, ink, ink, 1.2, rx=3)
        svg.text((dx(seed) + dx(passage)) / 2, y + 18, s1, 7.5, ink, "middle", mono=True)
        svg.text((dx(passage) + dx(mature)) / 2, y + 18, s2, 7.5, PAPER, "middle", mono=True)
        svg.circle(dx(seed), y + 14, 3.4, PAPER, ink, 1.6)
        y += rowh
    svg.rect(dx(mature), pad_t + 4, dx(end) - dx(mature), rowh * len(names) - 14,
             "#f3dcf1", GROOVE_S, 1.2, rx=3)
    svg.text((dx(mature) + dx(end)) / 2, pad_t + rowh * len(names) / 2,
             "interact", 8.5, GROOVE_S, "middle", weight="bold")
    svg.line(dx(mature), pad_t - 10, dx(mature), pad_t + rowh * len(names) - 8,
             GROOVE_S, 1.4, dash="5 3")
    svg.text(dx(mature), pad_t - 16, f"day {mature}: barriers opened", 8.5, GROOVE_S, "middle",
             weight="bold")
    svg.text(dx(end) + 10, pad_t + rowh * len(names) / 2, f"day {end}:", 8.5, INK, "start",
             weight="bold")
    svg.text(dx(end) + 10, pad_t + rowh * len(names) / 2 + 12, "barcode, pool, harvest",
             8.5, INK2, "start")
    axis(pad_t + rowh * len(names) - 4, "run day")

    # --- panel B: the thing we are avoiding --------------------------------------------
    y2 = pad_t + rowh * len(names) + 76
    svg.text(40, y2 - 20, "IF ALL THREE STARTED ON DAY 0  ·  what staggering avoids", 10,
             LASER_C, weight="bold")
    y = y2
    finishes = []
    for i, name in enumerate(names):
        s1, s2, seed, passage, _ = lanes[name]
        dur = mature - seed
        ink = LANE_INKS[i % len(LANE_INKS)]
        svg.text(pad_l - 14, y + 14, name, 10, ink, "end", weight="bold")
        svg.rect(dx(0), y + 4, axw, 20, "#fafbfc", "none", rx=3)
        svg.rect(dx(0), y + 4, dx(dur) - dx(0), 20, ink, ink, 1.2, rx=3)
        svg.text(dx(dur) + 8, y + 18, f"ready day {dur}", 8.5, ink, weight="bold")
        finishes.append(dur)
        y += rowh
    lo, hi = min(finishes), max(finishes)
    svg.line(dx(lo), y2 - 8, dx(lo), y + 4, LASER_C, 1.2, dash="4 3")
    svg.line(dx(hi), y2 - 8, dx(hi), y + 4, LASER_C, 1.2, dash="4 3")
    svg.rect(dx(lo), y + 8, dx(hi) - dx(lo), 16, "#fdeeec", LASER_C, 1.1, rx=3)
    svg.text((dx(lo) + dx(hi)) / 2, y + 20, f"{hi - lo} days apart", 8.5, LASER_C, "middle",
             weight="bold")
    axis(y + 36, "run day")

    row = y + 92
    svg.line(40, row - 16, w - 40, row - 16, GRID, 1.0)
    for note in [
        "Light bar = stage one, solid bar = stage two, hollow dot = the day that lane is "
        "seeded. Every lane is fed through its own port, so a lane being seeded cannot "
        "reach a lane that is 40 days in.",
        "Published co-culture work solves the same mismatch the same way: Guttikonda 2021 "
        "matures neurons first and adds microglia last; Klimmt 2026 banks frozen "
        "intermediates and thaws them into alignment.",
    ]:
        svg.text(40, row, note, 8.5, INK2)
        row += 14
    svg.h = row + 20
    return svg


# --- figure: operating modes ----------------------------------------------------------

MODE_SCALE = 3.2         # px per mm, the mini plan views
MIXED_C = "#3d4650"      # a channel that carries more than one medium
MEDIUM_C = {"p_in": "#2f6fb0", "p_g_in": "#468a4a", "p_a_in": "#bf7b2f"}


def _flow_directions(chip, state):
    """Which way each channel flows, from the port states alone.

    The channel graph of a pressure-routed chip is a tree. Cut one channel and the tree
    falls into two sides: fluid runs across the cut from side A to side B when side A
    holds a pressurised port and side B holds a vented one. A pressurised port facing
    only blocked ports pushes against a dead end, so it moves nothing.
    Returns {channel id: (from node, to node, [source ports])} for flowing channels.
    """
    adj: dict[str, list[tuple[str, str]]] = {}
    for ch in chip["channels"]:
        adj.setdefault(ch["from"], []).append((ch["to"], ch["id"]))
        adj.setdefault(ch["to"], []).append((ch["from"], ch["id"]))

    def side(start, cut):
        seen, todo = {start}, [start]
        while todo:
            for nxt, cid in adj.get(todo.pop(), []):
                if cid != cut and nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen

    def count(nodes, kind):
        return [p for p in nodes if state.get(p, ("B", None))[0] == kind]

    out = {}
    for ch in chip["channels"]:
        a, b = side(ch["from"], ch["id"]), side(ch["to"], ch["id"])
        fwd = count(a, "P") and count(b, "V")
        back = count(b, "P") and count(a, "V")
        if fwd and not back:
            out[ch["id"]] = (ch["from"], ch["to"], count(a, "P"))
        elif back and not fwd:
            out[ch["id"]] = (ch["to"], ch["from"], count(b, "P"))
    return out


def _arrow(svg, p0, p1, colour, size=10.0, at=0.5):
    """A filled arrowhead on p0 -> p1, centred a fraction `at` of the way along it."""
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return
    ux, uy = dx / length, dy / length
    cx, cy = p0[0] + dx * at, p0[1] + dy * at
    tip = (cx + ux * size * 0.6, cy + uy * size * 0.6)
    bx, by = cx - ux * size * 0.6, cy - uy * size * 0.6
    svg.poly([tip, (bx - uy * size * 0.55, by + ux * size * 0.55),
              (bx + uy * size * 0.55, by - ux * size * 0.55)], colour, PAPER, 0.8)


def fig_modes(chip):
    """One mini plan view per operating mode. None unless the chip is routed by ports."""
    # DESIGN INTENT, not a simulation. For every operating mode this table says what each
    # port is doing: "P" pressurised (pushes fluid in, cannot be flowed out of), "V" vented
    # (open, the drain) or "B" blocked (capped, no flow either way), plus what a
    # pressurised port is carrying. Every flow arrow below is derived from it by
    # _flow_directions(); change the table and the figure follows. A port a mode does not
    # mention is blocked.
    PORT_STATE = {
        "p_in":    {"seed": ("P", "cells"), "grow": ("P", "medium N"),
                    "n": ("P", "N medium"), "g": ("V", None), "a": ("V", None)},
        "p_g_in":  {"grow": ("P", "medium G"), "g": ("P", "G medium")},
        "p_a_in":  {"grow": ("P", "medium A"), "a": ("P", "A medium")},
        "p_mid":   {"grow": ("V", None)},
        "p_g_out": {"seed": ("V", None), "n": ("V", None), "a": ("V", None)},
        "p_a_out": {"seed": ("V", None), "n": ("V", None), "g": ("V", None)},
    }
    MODES = [
        ("seed", "SEED", "FALLBACK ONLY. Cells enter p_in and cross n_cx toward g_cx and a_cx, but most\nsettle long before they arrive, so each chamber is normally seeded through its own inlet."),
        ("grow", "SEPARATE GROWTH",
         "Three media in, one drain at p_mid. The weeks-long differentiation mode."),
        ("n", "INTERACT  N → G + A", "N medium leaves n_cx, reaches the junction and splits to g_cx and a_cx."),
        ("g", "INTERACT  G → N + A", "G medium leaves g_cx, reaches the junction and splits to n_cx and a_cx."),
        ("a", "INTERACT  A → N + G", "A medium leaves a_cx, reaches the junction and splits to n_cx and g_cx."),
    ]
    ids = {p["id"] for p in chip["ports"]}
    if not set(PORT_STATE) <= ids or not {"n_cx", "g_cx", "a_cx"} <= {c["id"] for c in chip["chambers"]}:
        return None

    fx, fy = chip["footprint"]["x"], chip["footprint"]["y"]
    S = MODE_SCALE
    bw, bh = fx * S, fy * S
    pad_x, gap_x = 40.0, 44.0
    head, gap_y = 54.0, 30.0
    cell_h = head + bh + gap_y
    w = pad_x * 2 + bw * 2 + gap_x
    top = 84.0
    h = top + cell_h * 3 + 70
    svg = Svg(w, h, f"{chip['id']}: operating modes")

    svg.text(pad_x, 24, "Operating modes: the chip has no valves, so the ports do the routing",
             14.5, INK, weight="bold")
    svg.text(pad_x, 41, f"chip/{chip['id']}.json   ·   one plan view per mode   ·   a "
                        f"driven port is a source and a capped port is a true block, so the vented port is "
                        f"the drain", 9.5, INK2)
    svg.text(w - pad_x, 24, "generated by", 8, INK3, "end")
    svg.text(w - pad_x, 36, "cad/make_figures.py", 8, INK3, "end", mono=True)
    svg.text(w - pad_x, 48, "do not edit by hand", 8, INK3, "end")

    segs = segments_of(chip)
    chambers = chip["chambers"]
    ports = {p["id"]: p for p in chip["ports"]}
    # (dx, dy, anchor) for the id label, in px from the port centre.
    label_at = {"p_in": (0, -22, "middle"), "p_g_in": (0, -22, "middle"),
                "p_a_in": (0, 22, "middle"), "p_mid": (14, -2, "start"),
                "p_g_out": (0, 22, "middle"), "p_a_out": (0, 22, "middle")}
    lineage = {"n_cx": "neurons", "g_cx": "glia", "a_cx": "astrocytes"}
    R = 6.5

    def port_glyph(x, y, kind, colour):
        if kind == "P":
            svg.circle(x, y, R, colour, INK, 1.4)
        elif kind == "V":
            svg.circle(x, y, R, PAPER, INK, 2.0)
        else:
            svg.circle(x, y, R, "#e6eaef", INK3, 1.2)
            svg.line(x - 3.2, y - 3.2, x + 3.2, y + 3.2, INK3, 1.5)
            svg.line(x - 3.2, y + 3.2, x + 3.2, y - 3.2, INK3, 1.5)

    for k, (mode, title, caption) in enumerate(MODES):
        col, row = k % 2, k // 2
        x0 = pad_x + col * (bw + gap_x)
        y0 = top + row * cell_h
        v = View(chip, S, x0, y0 + head)
        state = {pid: PORT_STATE[pid].get(mode, ("B", None)) for pid in PORT_STATE}
        flows = _flow_directions(chip, state)

        svg.circle(x0 + 8, y0 + 6, 8, INK, "none")
        svg.text(x0 + 8, y0 + 9.4, str(k + 1), 9, PAPER, "middle", "bold")
        svg.text(x0 + 24, y0 + 10, title, 11.5, INK, weight="bold")
        svg.text(x0 + 24, y0 + 25, caption, 8.5, INK2)
        groups = {kind: [p for p in PORT_STATE if state[p][0] == kind] for kind in "PVB"}
        summary = "  |  ".join(f"{word}: {', '.join(groups[kind])}" for kind, word in
                            (("P", "pressurised"), ("V", "vented"), ("B", "blocked"))
                            if groups[kind])
        svg.text(x0 + 24, y0 + 38, summary, 7.5, INK3, mono=True)

        svg.rect(v.x(0), v.y(fy), bw, bh, BOARD_F, BOARD_S, 1.2, rx=4)

        arrows = []
        for ch, p0, p1, _ in segs:
            a, b = v.p(p0), v.p(p1)
            mid = [v.p(c) for c in quad(p0, p1, max(ch["width_mm"], 5.0 / S))]
            flow = flows.get(ch["id"])
            svg.poly(mid, CHAN_F, CHAN_S, 0.8)
            if flow is None:
                continue
            src, dst, sources = flow
            colour = MEDIUM_C.get(sources[0], MIXED_C) if len(sources) == 1 else MIXED_C
            svg.poly(mid, colour, "none", op=0.30)
            if src != ch["from"]:
                a, b = b, a
            arrows.append((a, b, colour))

        for c in chambers:
            sx, sy = c["size"][0], c["size"][1]
            fill, stroke = TYPES.get(c.get("type"), TYPE_FALLBACK)
            svg.rect(v.x(c["center"][0] - sx / 2), v.y(c["center"][1] + sy / 2),
                     v.d(sx), v.d(sy), fill, stroke, 1.3, rx=2.5)
            cx, cy = v.x(c["center"][0]), v.y(c["center"][1])
            svg.text(cx, cy - 1, c["id"], 9, stroke, "middle", "bold", mono=True)
            svg.text(cx, cy + 10, lineage.get(c["id"], ""), 8, INK2, "middle")

        for a, b, colour in arrows:
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            count = max(1, int(length // 48))
            for i in range(count):
                _arrow(svg, a, b, colour, at=(i + 0.5) / count)

        for pid, port in ports.items():
            kind, medium = state[pid]
            px, py = v.x(port["position"][0]), v.y(port["position"][1])
            colour = MEDIUM_C.get(pid, INK)
            port_glyph(px, py, kind, colour)
            dx, dy, anchor = label_at.get(pid, (0, 22, "middle"))
            ink = {"P": colour if pid in MEDIUM_C else INK, "V": INK, "B": INK3}[kind]
            word = medium if kind == "P" else ("vent" if kind == "V" else "blocked")
            svg.text(px + dx, py + dy, pid, 8, ink, anchor, "bold", mono=True)
            svg.text(px + dx, py + dy + 9.5, word, 7.5, ink, anchor,
                     "bold" if kind != "B" else "normal")

    # --- legend, in the sixth cell -----------------------------------------------------
    lx = pad_x + (bw + gap_x)
    ly = top + cell_h * 2 + 8
    svg.text(lx, ly + 2, "HOW TO READ IT", 10, INK, weight="bold")
    svg.line(lx, ly + 10, lx + bw, ly + 10, GRID, 1.0)
    y = ly + 34
    port_glyph(lx + 10, y, "P", MEDIUM_C["p_in"])
    svg.text(lx + 28, y - 2, "pressurised", 9, INK, weight="bold")
    svg.text(lx + 28, y + 10, "pushes its medium in. Fluid cannot leave through it, so it blocks.",
             8.5, INK2)
    y += 36
    port_glyph(lx + 10, y, "V", MEDIUM_C["p_in"])
    svg.text(lx + 28, y - 2, "vented", 9, INK, weight="bold")
    svg.text(lx + 28, y + 10, "open to the air. Flow goes here: it is the drain.", 8.5, INK2)
    y += 36
    port_glyph(lx + 10, y, "B", MEDIUM_C["p_in"])
    svg.text(lx + 28, y - 2, "blocked", 9, INK, weight="bold")
    svg.text(lx + 28, y + 10, "capped. Nothing moves in or out.", 8.5, INK2)
    y += 36
    _arrow(svg, (lx + 2, y), (lx + 18, y), MEDIUM_C["p_in"], 11.0)
    svg.text(lx + 28, y - 2, "arrow", 9, INK, weight="bold")
    svg.text(lx + 28, y + 10, "flow direction in this mode. Colour is the port that feeds it;",
             8.5, INK2)
    svg.text(lx + 28, y + 21, "dark grey is a channel carrying more than one medium.", 8.5, INK2)
    x = lx + 28
    for pid, name in (("p_in", "p_in"), ("p_g_in", "p_g_in"), ("p_a_in", "p_a_in")):
        svg.rect(x, y + 28, 10, 8, MEDIUM_C[pid], "none", rx=1.5)
        svg.text(x + 14, y + 35.5, name, 8, MEDIUM_C[pid], weight="bold", mono=True)
        x += 78
    svg.rect(x, y + 28, 10, 8, MIXED_C, "none", rx=1.5)
    svg.text(x + 14, y + 35.5, "mixed", 8, MIXED_C, weight="bold", mono=True)

    row = top + cell_h * 3 + 22
    svg.line(pad_x, row - 14, w - pad_x, row - 14, GRID, 1.0)
    svg.text(pad_x, row, "Every mode is a different pressure map on the same board; nothing on "
                         "it moves. Arrows are derived from the port states, not simulated.",
             8.5, INK2)
    svg.text(pad_x, row + 14, "Channels are drawn wider than the 0.8 mm they are, so the "
                              "arrows fit. The port table is in fig_modes() in cad/make_figures.py.",
             8.5, INK3)
    svg.h = row + 34
    return svg


# --- main -----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chip", type=Path, help="one spec; default is every spec in chip/")
    ap.add_argument("--all", action="store_true", help="include placeholder specs")
    args = ap.parse_args()

    paths = [args.chip] if args.chip else sorted(CHIP_DIR.glob("*.json"))
    FIGURES.mkdir(parents=True, exist_ok=True)
    errors = 0

    for path in paths:
        chip = json.loads(path.read_text())
        if chip.get("placeholder") and not args.all and args.chip is None:
            print(f"{path.name}: placeholder, skipped (--all to include)")
            continue
        sched = load_schedule(chip["id"])
        layout, msgs, tightest = fig_layout(chip, sched)
        written = [layout.save(FIGURES / f"{chip['id']}_layout.svg"),
                   fig_stack(chip).save(FIGURES / f"{chip['id']}_stack.svg"),
                   fig_features(chip).save(FIGURES / f"{chip['id']}_features.svg")]
        try:
            schedule = fig_schedule(chip, sched)
        except (KeyError, ValueError, TypeError, IndexError):
            schedule = None      # a builder whose LANES has a different shape: skip the figure
        for fig, suffix in ((fig_barrier(chip), "barrier"), (schedule, "schedule"),
                            (fig_modes(chip), "modes")):
            if fig is not None:
                written.append(fig.save(FIGURES / f"{chip['id']}_{suffix}.svg"))

        print(f"\nchip {chip['id']}")
        for p in written:
            print(f"  wrote cad/figures/{p.name}")
        if tightest[0] < float("inf"):
            print(f"  tightest clearance between unconnected features: "
                  f"{tightest[0]:.2f} mm, between {tightest[1]}")
        for m in msgs:
            print(f"  {m}")
        errors += sum(m.startswith("ERROR") for m in msgs)

    if errors:
        print(f"\n{errors} planar collision(s). In a single-layer chip these are short "
              f"circuits, not drawing glitches.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

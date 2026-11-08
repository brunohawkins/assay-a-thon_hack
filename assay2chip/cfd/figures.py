"""GitHub-safe SVG figures for the CFD stage (S10).

Four maps per chip per protocol step, plus one overview of the whole chain:

    <chip_id>_s<step>_pressure.svg    nodal pressure, kPa
    <chip_id>_s<step>_speed.svg       depth-averaged speed, mm/s
    <chip_id>_s<step>_shear.svg       wall shear as a FRACTION of that chamber's
                                      own correct ceiling - see chamber_ceilings()
    <chip_id>_s<step>_residence.svg   residence time, s
    pipeline_overview.svg             every chip in chain order, the handoffs, a
                                      severity badge on each

Every map carries the chip outline, the ports, a scale bar, and a marker on every
finding at the feature it was raised against.

Three rules this module will not bend:

  SIMULATED        Every number drawn here comes out of a depth-averaged Hele-Shaw
                   solve, not a measurement. Each figure says so in its subtitle and
                   in its own <title>. Nothing here is data.
  DEMO SCALE       A chip on the 30 x 20 mm demo envelope (contracts/blocks.json, not
                   a literal here) is badged DEMO SCALE, because its channel widths
                   were clamped to the fabrication floor and its resistances no longer
                   match the design the text describes.
  THREE CEILINGS   Wall shear is drawn as a fraction of the ceiling for THAT chamber's
                   contents: 0.01 dyn/cm2 for adherent cells, 0.3 for a pre-lysis
                   rinse, and NO shear ceiling at all for a capsule chamber, which is
                   bounded by escape pressure instead. One shared colour scale across
                   three different limits is the bug this repo has already shipped
                   five times; see contracts.shear_ceilings().

Nothing in here imports a CFD sibling. It takes plain dicts - a chip spec, a Mesh, a
Field, a transport result, a findings list - so a figure still draws when half the
package does not exist yet: missing inputs become a labelled gap on the drawing, never
an exception. Pass `mesh=None, field=None` and you get the geometry with a NOT YET
AVAILABLE banner, which is exactly what the integrator wants on day one.

SVG is written with presentation attributes only, no <style> block, no external font,
no <image> and no href, because that is the subset GitHub renders inside markdown. Same
convention as cad/make_figures.py.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:                                                    # the repo's own port bore and
    sys.path.insert(0, str(ROOT / "cad"))               # channel clipping, so a CFD
    from make_train_cad import PORT_SIZE_MM             # figure and the DXF agree
    from make_train_cad import clip_to_box as _clip_to_box
except Exception:                                       # pragma: no cover - cad/ absent
    PORT_SIZE_MM = 0.8
    _clip_to_box = None

try:
    from assay2chip.lib import contracts
except Exception:                                       # pragma: no cover
    contracts = None


# --- palette and conventions, shared with cad/make_figures.py -------------------------

FONT = "DejaVu Sans, Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, monospace"
INK, INK2, INK3 = "#1b2026", "#5a636d", "#8b949e"
PAPER, GRID = "#ffffff", "#eaeef2"
BOARD_F, BOARD_S = "#f6f8fa", "#c9d1d9"
CHAN_S = "#8d97a3"
INLET_C, OUTLET_C = "#2f7d32", "#c0392b"
GAP_F, GAP_S = "#fdf3f3", "#c0392b"

SEVERITY_C = {
    "blocker": "#8e1b2e",
    "high": "#c0392b",
    "medium": "#bf7b2f",
    "low": "#6e7781",
    "info": "#8b949e",
}
SEVERITY_ORDER = ["blocker", "high", "medium", "low", "info"]

# Sequential ramps, low -> high. Hand-picked hex stops: no colormap library, and a ramp
# that is not monotone in lightness reads as noise on a small chip.
RAMPS = {
    "pressure": ["#f7fbff", "#c6dbef", "#6baed6", "#2171b5", "#08306b"],
    "speed": ["#fffdf0", "#fee391", "#fe9929", "#cc4c02", "#662506"],
    "residence": ["#fcf7fb", "#d4b9da", "#c994c7", "#df65b0", "#67001f"],
    "shear": ["#edf8e9", "#bae4b3", "#fed976", "#fd8d3c", "#bd0026"],
}
OVER_C = "#7a0012"          # over the ceiling: beyond the top of the shear ramp

SCALE_PX = 22.0             # px per mm floor; a 30 mm board is scaled up to BOARD_W_PX
BOARD_W_PX = 560.0
LEGEND_W = 250.0

# A real mesh is 5,000-13,000 cells and 26,000 edges. One <rect> per cell and one <path>
# per edge is a multi-megabyte SVG that GitHub will not render, so the DRAWING is binned
# onto a coarser raster (values averaged, "over the ceiling" OR-ed so a hot cell is never
# averaged away) and interior Hele-Shaw faces are not drawn at all - the cells already
# show what they carry. The numbers in the report come from the full mesh either way.
MAX_DRAWN_CELLS = 2600
INTERIOR_EDGE_KINDS = ("hs_face", "face", "interior")


def _f(value, default=None):
    """float(value) or `default`; None, '', 'TBC' and junk all become the default."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if out != out else out            # NaN


def esc(s: object) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def trim(text: str) -> str:
    """Drop trailing zeros only AFTER a decimal point.

    A bare `.rstrip("0")` turns 140 into 14 and 1200 into 12. `n()` gets away with it
    because `:.2f` always leaves a decimal point; `sig()` at 0 decimal places does not,
    which is how a 140 um capsule was once labelled 14 um. One guarded helper, used by
    both.
    """
    return (text.rstrip("0").rstrip(".") or "0") if "." in text else text


def n(v: float) -> str:
    return trim(f"{float(v):.2f}")


def sig(v, digits=3):
    """A number for a label: 3 significant figures, or '-' when there is no number."""
    x = _f(v)
    if x is None:
        return "-"
    if x == 0:
        return "0"
    if abs(x) >= 1e5 or abs(x) < 1e-3:
        return f"{x:.1e}"
    places = max(0, digits - 1 - int(math.floor(math.log10(abs(x)))))
    return trim(f"{x:.{places}f}")


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def ramp(name: str, t: float) -> str:
    """Interpolate a named ramp at t in [0, 1]."""
    stops = RAMPS.get(name) or RAMPS["pressure"]
    t = 0.0 if t != t else min(max(float(t), 0.0), 1.0)
    pos = t * (len(stops) - 1)
    i = min(int(pos), len(stops) - 2)
    f = pos - i
    a, b = _hex(stops[i]), _hex(stops[i + 1])
    return "#%02x%02x%02x" % tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3))


def ink_on(fill: str) -> str:
    """Ink that stays readable on `fill`."""
    r, g, b = _hex(fill)
    return INK if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else PAPER


# --- SVG ------------------------------------------------------------------------------

class Svg:
    """A tiny SVG document. Presentation attributes only; see the module docstring."""

    def __init__(self, w: float, h: float, title: str):
        self.w, self.h = float(w), float(h)
        self.title = title
        self.body: list[str] = []

    def add(self, s: str) -> None:
        self.body.append("  " + s)

    def rect(self, x, y, w, h, fill="none", stroke="none", sw=1.0, rx=0.0, dash=None,
             op=None):
        a = (f'<rect x="{n(x)}" y="{n(y)}" width="{n(max(w, 0))}" '
             f'height="{n(max(h, 0))}" fill="{fill}" stroke="{stroke}"')
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
        a = (f'<line x1="{n(x0)}" y1="{n(y0)}" x2="{n(x1)}" y2="{n(y1)}" '
             f'stroke="{stroke}" stroke-width="{n(sw)}"')
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

    def path(self, pts, stroke=INK3, sw=1.0, cap="round", op=None):
        if len(pts) < 2:
            return
        d = "M " + " L ".join(f"{n(x)} {n(y)}" for x, y in pts)
        a = (f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{n(sw)}" '
             f'stroke-linecap="{cap}" stroke-linejoin="round"')
        if op is not None:
            a += f' opacity="{n(op)}"'
        self.add(a + "/>")

    def circle(self, cx, cy, r, fill="none", stroke="none", sw=1.0):
        a = f'<circle cx="{n(cx)}" cy="{n(cy)}" r="{n(r)}" fill="{fill}" stroke="{stroke}"'
        if stroke != "none":
            a += f' stroke-width="{n(sw)}"'
        self.add(a + "/>")

    def text(self, x, y, s, size=10.0, fill=INK, anchor="start", weight="normal",
             mono=False, rot=None):
        fam = MONO if mono else FONT
        a = (f'<text x="{n(x)}" y="{n(y)}" font-family="{fam}" font-size="{n(size)}" '
             f'fill="{fill}" text-anchor="{anchor}"')
        if rot:
            a += f' transform="rotate({n(rot[0])} {n(rot[1])} {n(rot[2])})"'
        if weight != "normal":
            a += f' font-weight="{weight}"'
        self.add(a + f">{esc(s)}</text>")

    def halo(self, x, y, s, size=9.0, fill=INK, anchor="start", weight="normal",
             mono=False, pad=2.5, bg=PAPER, op=0.92):
        wpx = len(str(s)) * size * (0.60 if mono else 0.56)
        x0 = x if anchor == "start" else (x - wpx / 2 if anchor == "middle" else x - wpx)
        self.rect(x0 - pad, y - size * 0.84, wpx + 2 * pad, size * 1.16, bg, "none", op=op)
        self.text(x, y, s, size, fill, anchor, weight, mono)

    def badge(self, x, y, label, colour, size=8.5, pad=5.0, h=15.0):
        w = len(str(label)) * size * 0.62 + 2 * pad
        self.rect(x, y, w, h, PAPER, colour, 1.2, rx=h / 2)
        self.text(x + w / 2, y + h * 0.70, label, size, colour, "middle", "bold")
        return x + w + 6

    def save(self, path: Path) -> Path:
        head = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{n(self.w)}" '
                f'height="{n(self.h)}" viewBox="0 0 {n(self.w)} {n(self.h)}" '
                f'role="img" aria-label="{esc(self.title)}">')
        out = [head, f"  <title>{esc(self.title)}</title>",
               f'  <rect width="{n(self.w)}" height="{n(self.h)}" fill="{PAPER}"/>']
        out += self.body
        out.append("</svg>\n")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(out), encoding="utf-8")
        return path


class View:
    """mm -> px, y flipped: a chip spec has y up, SVG has y down."""

    def __init__(self, footprint_y_mm, scale, ox, oy):
        self.s, self.ox, self.oy = float(scale), float(ox), float(oy)
        self.fy = float(footprint_y_mm)

    def x(self, mm): return self.ox + float(mm) * self.s
    def y(self, mm): return self.oy + (self.fy - float(mm)) * self.s
    def d(self, mm): return float(mm) * self.s
    def p(self, pt): return (self.x(pt[0]), self.y(pt[1]))


# --- tolerant readers for whatever the siblings hand over -----------------------------

def as_dict(obj):
    """A Mesh/Field/anything as a plain dict, whether it is one already or a dataclass."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    for attr in ("to_dict", "as_dict", "asdict", "json_dict"):
        fn = getattr(obj, attr, None)
        if callable(fn):
            try:
                out = fn()
                if isinstance(out, dict):
                    return out
            except Exception:
                pass
    try:
        import dataclasses
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return dataclasses.asdict(obj)
    except Exception:
        pass
    d = getattr(obj, "__dict__", None)
    return dict(d) if isinstance(d, dict) else {}


def _rows(container, id_keys=("id",)):
    """A list of dicts out of a list, a dict-of-dicts, or a list of dataclasses."""
    if container is None:
        return []
    if isinstance(container, dict):
        out = []
        for key, value in container.items():
            row = as_dict(value)
            row = dict(row) if row else {}
            for k in id_keys:
                row.setdefault(k, key)
            out.append(row)
        return out
    out = []
    for item in container:
        row = as_dict(item)
        out.append(dict(row) if row else {})
    return out


def mesh_nodes(mesh):
    return _rows(as_dict(mesh).get("nodes"))


def mesh_edges(mesh):
    return _rows(as_dict(mesh).get("edges"))


def series(field, *keys):
    """A `{key: number}` series out of a Field, under any of several key spellings.

    Accepts a dict keyed by id, a dict keyed by index, or a bare list (positional).
    Returns (mapping, is_positional).
    """
    data = as_dict(field)
    for key in keys:
        if key in data and data[key] is not None:
            value = data[key]
            if isinstance(value, dict):
                return dict(value), False
            if isinstance(value, (list, tuple)):
                return {i: v for i, v in enumerate(value)}, True
    return {}, False


def _at(mapping, positional, row, index):
    """Look a row's value up by id, feature_id, or position."""
    if positional:
        return mapping.get(index)
    for key in ("id", "node", "cell", "edge", "feature_id"):
        value = row.get(key)
        if value is not None and value in mapping:
            return mapping[value]
    return mapping.get(index)


def magnitude(value):
    """Speed from a vector [ux, uy], a {x, y} dict, or an already-scalar speed."""
    if isinstance(value, (list, tuple)):
        return math.hypot(_f(value[0], 0.0) or 0.0, _f(value[1], 0.0) or 0.0) if value else None
    if isinstance(value, dict):
        return math.hypot(_f(value.get("x") or value.get("ux"), 0.0) or 0.0,
                          _f(value.get("y") or value.get("uy"), 0.0) or 0.0)
    return _f(value)


# --- chip geometry --------------------------------------------------------------------

def clip_to_box(p0, p1, center, size):
    """Trim a channel end to the wall of the chamber it leaves. Same maths as the DXF."""
    if _clip_to_box is not None:
        try:
            return _clip_to_box(p0, p1, center, size)
        except Exception:
            pass
    cx, cy = float(center[0]), float(center[1])
    hx, hy = float(size[0]) / 2, float(size[1]) / 2
    dx, dy = float(p1[0]) - cx, float(p1[1]) - cy
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return (cx, cy)
    ts = [t for t in ((hx / abs(dx)) if abs(dx) > 1e-12 else None,
                      (hy / abs(dy)) if abs(dy) > 1e-12 else None) if t is not None]
    t = min(min(ts), 1.0)
    return (cx + dx * t, cy + dy * t)


def footprint(chip):
    fp = (chip or {}).get("footprint") or {}
    return _f(fp.get("x"), 30.0), _f(fp.get("y"), 20.0)


def anchors(chip):
    out = {}
    for c in (chip or {}).get("chambers") or []:
        out[c.get("id")] = (c.get("center") or [0, 0, 0], c.get("size") or [0.5, 0.5, 0])
    for p in (chip or {}).get("ports") or []:
        pos = p.get("position") or [0, 0, 0]
        out[p.get("id")] = (pos, [PORT_SIZE_MM, PORT_SIZE_MM, 0])
    return out


def channel_path(chip, channel, anchor_table=None):
    """The polyline a channel actually follows, in mm.

    A routed channel carries an explicit `path` and MUST follow it: a straight line
    between endpoints is the wrong length and the wrong shear (cfd/README.md).
    """
    table = anchor_table if anchor_table is not None else anchors(chip)
    path = channel.get("path")
    if path and len(path) >= 2:
        return [(_f(p[0], 0.0), _f(p[1], 0.0)) for p in path]
    a, b = table.get(channel.get("from")), table.get(channel.get("to"))
    if a is None or b is None:
        return []
    p0 = clip_to_box(a[0][:2], b[0][:2], a[0], a[1])
    p1 = clip_to_box(b[0][:2], a[0][:2], b[0], b[1])
    return [tuple(p0), tuple(p1)]


def feature_point(chip, mesh, feature_id):
    """Where a finding happened, in mm, or None when it is chip-wide."""
    if not feature_id:
        return None
    fid = str(feature_id)
    for c in (chip or {}).get("chambers") or []:
        if c.get("id") == fid:
            return (_f(c["center"][0], 0.0), _f(c["center"][1], 0.0))
    for p in (chip or {}).get("ports") or []:
        if p.get("id") == fid:
            return (_f(p["position"][0], 0.0), _f(p["position"][1], 0.0))
    table = anchors(chip)
    for ch in (chip or {}).get("channels") or []:
        if ch.get("id") == fid:
            pts = channel_path(chip, ch, table)
            if pts:
                return pts[len(pts) // 2]
    nodes = {str(nd.get("id")): nd for nd in mesh_nodes(mesh)}
    if fid in nodes:
        nd = nodes[fid]
        return (_f(nd.get("x_mm"), 0.0), _f(nd.get("y_mm"), 0.0))
    for ed in mesh_edges(mesh):
        if str(ed.get("id")) == fid:
            a, b = nodes.get(str(ed.get("a"))), nodes.get(str(ed.get("b")))
            if a and b:
                return ((_f(a.get("x_mm"), 0.0) + _f(b.get("x_mm"), 0.0)) / 2,
                        (_f(a.get("y_mm"), 0.0) + _f(b.get("y_mm"), 0.0)) / 2)
    for nd in mesh_nodes(mesh):
        if str(nd.get("feature_id")) == fid:
            return (_f(nd.get("x_mm"), 0.0), _f(nd.get("y_mm"), 0.0))
    return None


def is_demo_scale(chip):
    """True for a chip on the demo envelope, which is badged DEMO SCALE.

    The envelope comes from contracts/blocks.json, never from a literal here.
    """
    fx, fy = footprint(chip)
    if contracts is not None:
        try:
            env = contracts.envelope("demo")
            return fx <= env.x_mm + 1e-6 and fy <= env.y_mm + 1e-6
        except Exception:
            pass
    return fx <= 30.0 + 1e-6 and fy <= 20.0 + 1e-6      # fallback, flagged in the caption


# --- the three shear ceilings, scoped per chamber -------------------------------------

LYSE_WORDS = ("lys", "quench", "digest", "protease", "release", "collagenase", "elut")
CELL_BLOCKS = ("culture_chamber", "seeding_chamber", "adherent")
CAPSULE_WORDS = ("capsule", "bead", "trap")


def as_step(step):
    """A step as a mapping, whatever the caller had to hand.

    Callers legitimately hold a step NUMBER (the figure filename needs one) or the step
    DICT (the shear scoping needs its operation and reagent). Taking either, and
    returning an empty mapping for a number, is what stops a `step=1` from being read as
    a step record - which silently scoped every chamber to `none` before it raised.
    """
    if isinstance(step, dict):
        return step
    for attr in ("raw", "_asdict"):
        value = getattr(step, attr, None)
        if isinstance(value, dict):
            return value
        if callable(value):
            try:
                out = value()
                if isinstance(out, dict):
                    return out
            except Exception:
                pass
    if step is None or isinstance(step, (int, float, str)):
        return {}
    return as_dict(step)


def _step_text(step):
    step = as_step(step)
    bits = [step.get("operation"), step.get("reagent"), step.get("note"),
            step.get("target"), step.get("kind")]
    return " ".join(str(b).lower() for b in bits if b)


def chamber_shear_class(chip, chamber, step=None):
    """Which of the three ceilings governs THIS chamber in THIS step, and why.

    Returns (shear_class, why). The classes are the ones in contracts.shear_ceilings():
    `adherent_cells`, `prelysis_rinse`, `capsules` (pressure-bounded, no shear ceiling)
    and `none`.
    """
    chamber = as_step(chamber)
    cid = str(chamber.get("id", ""))
    block = str(chamber.get("block") or "")
    text = _step_text(step)
    capsule_chip = bool((chip or {}).get("capsule") or (chip or {}).get("trap"))
    retention = chamber.get("retention") or chamber.get("filter")
    holds_capsules = (
        bool(retention)
        or any(w in block for w in CAPSULE_WORDS)
        or any(w in cid.lower() for w in ("trap", "caps", "bed"))
        or (capsule_chip and chamber.get("type") == "exchange")
    )
    if holds_capsules:
        return "capsules", (
            f"'{cid}' holds capsules (retention/trap feature), so it is bounded by "
            f"escape pressure x margin, NOT by a wall-shear ceiling")
    adherent = any(w in block for w in CELL_BLOCKS) or chamber.get("type") in (
        "exchange", "culture", "well")
    if adherent and any(w in text for w in LYSE_WORDS):
        return "prelysis_rinse", (
            f"'{cid}' holds cells but this step is a deliberate lyse/quench, so the "
            f"pre-lysis rinse ceiling applies, not the culture one")
    if adherent:
        return "adherent_cells", f"'{cid}' holds adherent cells: detachment is the risk"
    return "none", f"'{cid}' holds nothing shear-sensitive in this step"


def chamber_ceilings(chip, step=None):
    """{chamber_id: {shear_class, ceiling_dyn_cm2, pressure_ceiling_kpa, why, source}}.

    This is the one place a figure decides what 100% of the scale means, and it is per
    chamber. Three different limits never share one colour bar.
    """
    out = {}
    for chamber in (chip or {}).get("chambers") or []:
        shear_class, why = chamber_shear_class(chip, chamber, step)
        ceiling, pressure, source = None, None, "contracts/parameters.json"
        if contracts is not None:
            try:
                sc = contracts.shear_ceiling_for(shear_class)
                ceiling, pressure, source = (sc.ceiling_dyn_cm2, sc.pressure_ceiling_kpa,
                                             sc.source)
            except Exception as exc:
                source = f"CONTRACT GAP: {exc}"
        out[str(chamber.get("id"))] = {
            "shear_class": shear_class,
            "ceiling_dyn_cm2": ceiling,
            "pressure_ceiling_kpa": pressure,
            "why": why,
            "source": source,
        }
    return out


def _owner_chamber(chip, mesh, row, nodes_by_id=None):
    """The chamber a mesh cell or edge belongs to, by feature_id, else by containment."""
    fid = row.get("feature_id")
    chambers = (chip or {}).get("chambers") or []
    if fid:
        for c in chambers:
            if c.get("id") == fid:
                return c
    x, y = row.get("x_mm"), row.get("y_mm")
    if x is None and nodes_by_id is not None:
        a = nodes_by_id.get(str(row.get("a")))
        if a:
            x, y = a.get("x_mm"), a.get("y_mm")
    x, y = _f(x), _f(y)
    if x is None or y is None:
        return None
    for c in chambers:
        cx, cy = _f(c["center"][0], 0.0), _f(c["center"][1], 0.0)
        sx, sy = _f(c["size"][0], 0.0), _f(c["size"][1], 0.0)
        if abs(x - cx) <= sx / 2 + 1e-9 and abs(y - cy) <= sy / 2 + 1e-9:
            return c
    return None


# --- shared chrome: board, ports, scale bar, findings ---------------------------------

def _scale_for(chip):
    fx, _ = footprint(chip)
    return max(SCALE_PX, BOARD_W_PX / max(fx, 1e-6))


def _nice_bar_mm(fx):
    for candidate in (0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0):
        if candidate >= fx / 6.0:
            return candidate
    return 50.0


def draw_board(svg, chip, v, label_features=True):
    """Chip outline, chambers and channels in grey: the frame every map is drawn on."""
    fx, fy = footprint(chip)
    svg.rect(v.x(0), v.y(fy), v.d(fx), v.d(fy), BOARD_F, BOARD_S, 1.4, rx=5)
    table = anchors(chip)
    for ch in (chip or {}).get("channels") or []:
        pts = channel_path(chip, ch, table)
        if len(pts) >= 2:
            svg.path([v.p(p) for p in pts], CHAN_S,
                     max(v.d(_f(ch.get("width_mm"), 0.2) or 0.2), 1.0), op=0.35)
    for c in (chip or {}).get("chambers") or []:
        sx, sy = _f(c["size"][0], 0.5) or 0.5, _f(c["size"][1], 0.5) or 0.5
        cx, cy = _f(c["center"][0], 0.0), _f(c["center"][1], 0.0)
        svg.rect(v.x(cx - sx / 2), v.y(cy + sy / 2), v.d(sx), v.d(sy),
                 "none", BOARD_S, 1.1, rx=2)
        if label_features:
            svg.halo(v.x(cx), v.y(cy + sy / 2) - 3, str(c.get("id")), 8.0, INK3,
                     "middle", mono=True, op=0.0)


def draw_ports(svg, chip, v):
    """Ports as rings, inlets green and outlets red, each named."""
    for p in (chip or {}).get("ports") or []:
        pos = p.get("position") or [0, 0, 0]
        colour = INLET_C if p.get("type") == "inlet" else OUTLET_C
        px, py = v.x(pos[0]), v.y(pos[1])
        r = max(v.d(PORT_SIZE_MM / 2), 3.5)
        svg.circle(px, py, r, PAPER, colour, 2.0)
        svg.circle(px, py, max(r - 2.5, 1.0), colour, "none")
        svg.halo(px, py - r - 4, str(p.get("id")), 7.5, colour, "middle", mono=True)


def draw_scale_bar(svg, chip, v, y_px):
    fx, _ = footprint(chip)
    bar = _nice_bar_mm(fx)
    x0 = v.x(0)
    x1 = x0 + v.d(bar)
    svg.line(x0, y_px, x1, y_px, INK2, 2.2, cap="butt")
    for xx in (x0, x1):
        svg.line(xx, y_px - 4, xx, y_px + 4, INK2, 1.4)
    svg.text((x0 + x1) / 2, y_px + 15, f"{bar:g} mm", 9, INK2, "middle")


def draw_findings(svg, chip, mesh, v, findings, step=None):
    """A severity-coloured marker on every finding, at the feature it was raised on.

    Returns the key rows: (number, severity, finding) in drawing order, plus the ones
    that had no place on the chip, which the key reports as chip-wide.
    """
    rows, unplaced = [], []
    seen: dict[tuple, int] = {}
    for finding in findings or []:
        f = as_dict(finding)
        fstep = f.get("step")
        if step is not None and fstep not in (None, "", step):
            continue
        severity = str(f.get("severity") or "info").lower()
        point = feature_point(chip, mesh, f.get("feature_id"))
        if point is None:
            unplaced.append(f)
            continue
        key = (round(point[0], 3), round(point[1], 3))
        seen[key] = seen.get(key, 0) + 1
        rows.append((len(rows) + 1, severity, f, point, seen[key]))
    for number, severity, f, point, stack in rows:
        colour = SEVERITY_C.get(severity, SEVERITY_C["info"])
        px, py = v.x(point[0]), v.y(point[1]) - (stack - 1) * 17
        svg.poly([(px, py - 9), (px + 9, py), (px, py + 9), (px - 9, py)],
                 PAPER, colour, 1.8)
        svg.text(px, py + 3.2, str(number), 8.5, colour, "middle", "bold")
    return rows, unplaced


# --- the legend / colour bar -----------------------------------------------------------

def draw_colour_bar(svg, x, y, w, h, ramp_name, lo, hi, units, over_label=None):
    steps = 48
    for i in range(steps):
        t = i / (steps - 1)
        svg.rect(x + i * (w / steps), y, w / steps + 0.6, h, ramp(ramp_name, t))
    svg.rect(x, y, w, h, "none", INK3, 0.8)
    svg.text(x, y + h + 11, sig(lo), 8, INK2)
    svg.text(x + w, y + h + 11, sig(hi), 8, INK2, "end")
    svg.text(x + w / 2, y + h + 11, units, 8, INK3, "middle")
    if over_label:
        svg.rect(x + w + 8, y, 16, h, OVER_C, INK3, 0.8)
        svg.text(x + w + 28, y + h - 2, over_label, 8, OVER_C, weight="bold")


# --- the map figures -------------------------------------------------------------------

MAPS = {
    "pressure": {
        "ramp": "pressure", "units": "kPa", "title": "pressure",
        "what": "Nodal pressure from the depth-averaged resistance-network solve.",
    },
    "speed": {
        "ramp": "speed", "units": "mm/s", "title": "speed",
        "what": "Depth-averaged speed. In Hele-Shaw flow a trap is a LOW-SPEED zone: "
                "this plane flow has no recirculation eddies at all.",
    },
    "shear": {
        "ramp": "shear", "units": "fraction of this chamber's own ceiling",
        "title": "wall shear vs ceiling",
        "what": "Wall shear as a fraction of the ceiling for THAT chamber's contents. "
                "1.0 is the limit; a capsule chamber has no shear ceiling and is "
                "bounded by escape pressure instead.",
    },
    "residence": {
        "ramp": "residence", "units": "s", "title": "residence time",
        "what": "Time a parcel spends in each cell. A residence-time outlier is the "
                "other half of the trap test, with low speed.",
    },
}


def _cell_values(chip, mesh, field, transport, kind, ceilings):
    """[(row, value, is_over)] for mesh cells, in the units the map wants."""
    nodes = mesh_nodes(mesh)
    if kind == "pressure":
        src, pos = series(field, "p_pa", "pressure_pa", "p")
        return [(nd, (lambda p: None if p is None else p / 1000.0)(
            _f(_at(src, pos, nd, i))), False) for i, nd in enumerate(nodes)]
    if kind == "speed":
        src, pos = series(field, "u_mm_s", "u", "speed_mm_s", "velocity_mm_s")
        return [(nd, magnitude(_at(src, pos, nd, i)), False)
                for i, nd in enumerate(nodes)]
    if kind == "residence":
        # transport.py reports residence time as the steady MEAN AGE of each cell, under
        # `age.age_s`. Earlier and alternative spellings are accepted so a sibling
        # renaming a key degrades to an empty map rather than a wrong one.
        src, pos = {}, False
        for container in (as_dict(transport).get("age"), transport, field):
            src, pos = series(container, "age_s", "mean_age_s", "residence_s",
                              "residence_time_s", "residence_time", "tau_s")
            if src:
                break
        return [(nd, _f(_at(src, pos, nd, i)), False) for i, nd in enumerate(nodes)]
    if kind == "shear":
        src, pos = series(field, "shear_dyn_cm2", "shear", "tau_wall_dyn_cm2")
        out = []
        for i, nd in enumerate(nodes):
            tau = _f(_at(src, pos, nd, i))
            chamber = _owner_chamber(chip, mesh, nd)
            info = ceilings.get(str((chamber or {}).get("id")), {})
            ceiling = info.get("ceiling_dyn_cm2")
            if tau is None or not ceiling:
                out.append((nd, None, False))
            else:
                out.append((nd, tau / ceiling, tau > ceiling))
        return out
    return []


def _edge_values(chip, mesh, field, kind, ceilings):
    edges = mesh_edges(mesh)
    nodes_by_id = {str(nd.get("id")): nd for nd in mesh_nodes(mesh)}
    if kind == "shear":
        src, pos = series(field, "shear_dyn_cm2", "shear", "tau_wall_dyn_cm2")
        out = []
        for i, ed in enumerate(edges):
            tau = _f(_at(src, pos, ed, i))
            chamber = _owner_chamber(chip, mesh, ed, nodes_by_id)
            info = ceilings.get(str((chamber or {}).get("id")), {})
            ceiling = info.get("ceiling_dyn_cm2")
            if ceiling is None and contracts is not None:
                try:                     # a duct outside every chamber: the strictest
                    ceiling = contracts.shear_ceiling_for("adherent_cells").ceiling_dyn_cm2
                except Exception:
                    ceiling = None
            if tau is None or not ceiling:
                out.append((ed, None, False))
            else:
                out.append((ed, tau / ceiling, tau > ceiling))
        return out
    if kind == "speed":
        src, pos = series(field, "u_mm_s", "u", "speed_mm_s")
        if src:
            return [(ed, magnitude(_at(src, pos, ed, i)), False)
                    for i, ed in enumerate(edges)]
        q, qpos = series(field, "q_m3_s", "q", "flow_m3_s")
        out = []
        for i, ed in enumerate(edges):
            flow = _f(_at(q, qpos, ed, i))
            w = _f(ed.get("width_mm")) or 0.0
            h = _f(ed.get("depth_mm")) or 0.0
            area = (w / 1000.0) * (h / 1000.0)
            out.append((ed, abs(flow) / area * 1000.0 if flow is not None and area > 0
                        else None, False))
        return out
    return [(ed, None, False) for ed in edges]


def bin_cells(cells, grid_mm, limit=MAX_DRAWN_CELLS):
    """[(x_mm, y_mm, side_mm, value, over)] - the cells to DRAW, binned if there are many.

    Returns the real cells untouched when there are few enough. Binning averages the
    value over each bin but keeps `over` if ANY cell in it was over its ceiling, so
    coarsening the drawing can hide a gradient but never hide a breach.
    """
    rows = []
    for nd, value, over in cells:
        if str(nd.get("kind") or "cell") not in ("cell", "chamber", ""):
            continue
        x, y = _f(nd.get("x_mm")), _f(nd.get("y_mm"))
        if x is None or y is None:
            continue
        side = grid_mm or math.sqrt(max(_f(nd.get("area_mm2"), 0.04) or 0.04, 1e-6))
        rows.append((x, y, side, value, over))
    if len(rows) <= limit:
        return rows, None
    factor = max(2, int(math.ceil(math.sqrt(len(rows) / limit))))
    bins = {}
    for x, y, side, value, over in rows:
        pitch = side * factor
        key = (math.floor(x / pitch), math.floor(y / pitch))
        slot = bins.setdefault(key, [0.0, 0.0, 0, 0.0, 0, False, pitch])
        slot[0] += x
        slot[1] += y
        slot[2] += 1
        if value is not None:
            slot[3] += value
            slot[4] += 1
        slot[5] = slot[5] or bool(over)
    out = []
    for slot in bins.values():
        count, valued = slot[2], slot[4]
        out.append((slot[0] / count, slot[1] / count, slot[6],
                    slot[3] / valued if valued else None, slot[5]))
    return out, factor


def _span(values):
    nums = [v for v in values if v is not None]
    if not nums:
        return None, None
    lo, hi = min(nums), max(nums)
    if hi - lo < 1e-12:
        hi = lo + max(abs(lo) * 0.01, 1e-9)
    return lo, hi


def map_figure(chip, kind, out_dir, mesh=None, field=None, transport=None,
               findings=(), step=None, step_label=None, notes=(), chip_label=None,
               step_data=None):
    """One map. Draws whatever exists; labels whatever does not.

    `step` names the step (it goes in the filename and the subtitle); `step_data` is the
    step RECORD, and the shear scoping needs it - the ceiling for a chamber depends on
    what that chamber holds DURING THIS STEP, so a rinse before a lyse is scoped
    differently from the same chamber on day 3. Passing only the number scopes by
    chamber alone, which is the looser reading and says so on the figure.
    """
    cfg = MAPS[kind]
    chip = chip or {}
    chip_id = chip_label or str(chip.get("id") or "chip")
    fx, fy = footprint(chip)
    scale = _scale_for(chip)
    pad_l, pad_t, pad_r = 44.0, 92.0, LEGEND_W
    board_h = fy * scale
    w = pad_l + fx * scale + pad_r
    v = View(fy, scale, pad_l, pad_t)
    gaps: list[str] = []
    if not mesh_nodes(mesh):
        gaps.append("mesh.py: NOT YET AVAILABLE (no cells, so no map)")
    if not as_dict(field):
        gaps.append("solve.py: NOT YET AVAILABLE (no field)")
    if kind == "residence" and not as_dict(transport):
        gaps.append("transport.py: NOT YET AVAILABLE (no residence time)")

    step_txt = step_label or (f"step {step}" if step is not None else "no protocol step")
    title = f"{chip_id}: SIMULATED {cfg['title']} - {step_txt}"
    svg = Svg(w, pad_t + board_h + 150, title)

    svg.text(pad_l, 26, f"{chip_id}  ·  {cfg['title']}", 14.5, INK, weight="bold")
    svg.text(pad_l, 43, f"{step_txt}  ·  footprint {fx:g} × {fy:g} mm  ·  plan view, "
                        f"to scale", 9.5, INK2)
    x = svg.badge(pad_l, 52, "SIMULATED", "#6b5bb0")
    if is_demo_scale(chip):
        x = svg.badge(x, 52, "DEMO SCALE", "#bf7b2f")
    if gaps:
        x = svg.badge(x, 52, "INCOMPLETE", GAP_S)
    svg.text(w - 14, 26, "generated by", 8, INK3, "end")
    svg.text(w - 14, 38, "assay2chip/cfd/figures.py", 8, INK3, "end", mono=True)
    svg.text(w - 14, 50, "simulated, not measured", 8, GAP_S, "end")

    draw_board(svg, chip, v)
    scope_step = as_step(step_data) or as_step(step)
    ceilings = chamber_ceilings(chip, scope_step)

    cells = _cell_values(chip, mesh, field, transport, kind, ceilings)
    all_edges = _edge_values(chip, mesh, field, kind, ceilings)
    edges = [row for row in all_edges
             if str(row[0].get("kind") or "") not in INTERIOR_EDGE_KINDS]
    interior_dropped = len(all_edges) - len(edges)
    lo, hi = _span([value for _, value, _ in cells] + [value for _, value, _ in edges])
    if kind == "shear" and lo is not None:
        lo, hi = 0.0, max(hi, 1.0)

    grid_mm = _f(as_dict(mesh).get("meta", {}).get("grid_mm")) if as_dict(mesh) else None
    nodes_by_id = {str(nd.get("id")): nd for nd in mesh_nodes(mesh)}

    drawn, factor = bin_cells(cells, grid_mm)
    for x, y, side, value, over in drawn:
        px, py = v.x(x - side / 2), v.y(y + side / 2)
        if value is None:
            svg.rect(px, py, v.d(side), v.d(side), GAP_F, GAP_S, 0.4, op=0.5)
            continue
        colour = OVER_C if over else ramp(cfg["ramp"], (value - lo) / (hi - lo))
        svg.rect(px, py, v.d(side) + 0.5, v.d(side) + 0.5, colour, "none")
        if over:                                   # hatched, so it reads without colour
            svg.line(px, py + v.d(side), px + v.d(side), py, PAPER, 0.9)

    for ed, value, over in edges:
        a, b = nodes_by_id.get(str(ed.get("a"))), nodes_by_id.get(str(ed.get("b")))
        if not a or not b or value is None:
            continue
        colour = OVER_C if over else ramp(cfg["ramp"], (value - lo) / (hi - lo))
        svg.path([(v.x(_f(a.get("x_mm"), 0.0)), v.y(_f(a.get("y_mm"), 0.0))),
                  (v.x(_f(b.get("x_mm"), 0.0)), v.y(_f(b.get("y_mm"), 0.0)))],
                 colour, max(v.d(_f(ed.get("width_mm"), 0.2) or 0.2), 2.0))

    draw_ports(svg, chip, v)
    rows, unplaced = draw_findings(svg, chip, mesh, v, findings, step)
    draw_scale_bar(svg, chip, v, v.y(0) + 26)

    # --- legend ------------------------------------------------------------------------
    lx = pad_l + fx * scale + 22
    ly = pad_t
    svg.text(lx, ly, cfg["title"].upper(), 10, INK, weight="bold")
    ly += 16
    if lo is None:
        svg.rect(lx, ly - 10, LEGEND_W - 40, 30, GAP_F, GAP_S, 1.0, rx=3)
        svg.text(lx + 8, ly + 8, "no values to map yet", 9, GAP_S, weight="bold")
        ly += 42
    else:
        draw_colour_bar(svg, lx, ly, LEGEND_W - 80, 12, cfg["ramp"], lo, hi,
                        cfg["units"] if kind != "shear" else "× ceiling",
                        "over" if kind == "shear" else None)
        ly += 44
    for line in _wrap(cfg["what"], 34):
        svg.text(lx, ly, line, 8, INK2)
        ly += 11
    ly += 6

    if kind == "shear":
        svg.text(lx, ly, "CEILING PER CHAMBER", 9, INK, weight="bold")
        ly += 13
        for cid, info in ceilings.items():
            ceiling = info.get("ceiling_dyn_cm2")
            if ceiling is not None:
                txt = f"{cid}: {sig(ceiling)} dyn/cm² ({info['shear_class']})"
            else:
                pressure = info.get("pressure_ceiling_kpa")
                txt = (f"{cid}: NO shear ceiling - bounded by "
                       f"{sig(pressure)} kPa" if pressure else
                       f"{cid}: no ceiling ({info['shear_class']})")
            for k, line in enumerate(_wrap(txt, 36)):
                svg.text(lx + (0 if k == 0 else 8), ly, line, 7.5, INK2, mono=(k == 0))
                ly += 10
        ly += 6

    if rows or unplaced:
        svg.text(lx, ly, "FINDINGS HERE", 9, INK, weight="bold")
        ly += 13
        for number, severity, f, _, _ in rows:
            colour = SEVERITY_C.get(severity, SEVERITY_C["info"])
            svg.circle(lx + 5, ly - 3, 6, PAPER, colour, 1.2)
            svg.text(lx + 5, ly, str(number), 7.5, colour, "middle", "bold")
            label = (f"{severity.upper()} {f.get('class', '?')} · "
                     f"{f.get('feature_id', '?')}")
            svg.text(lx + 16, ly, label[:40], 7.5, INK2)
            ly += 11
            detail = f"{sig(f.get('value'))} vs {sig(f.get('limit'))} {f.get('units', '')}"
            svg.text(lx + 16, ly, detail.strip(), 7.5, INK3, mono=True)
            ly += 13
        for f in unplaced:
            svg.text(lx, ly, f"· chip-wide: {str(f.get('class', '?'))[:34]}", 7.5, INK3)
            ly += 11
    elif findings is not None:
        svg.text(lx, ly, "no findings on this step", 8, INK3)
        ly += 13

    if gaps:
        ly += 6
        svg.rect(lx - 4, ly - 11, LEGEND_W - 32, 15 + 12 * len(gaps), GAP_F, GAP_S, 1.0,
                 rx=3)
        svg.text(lx, ly, "NOT YET AVAILABLE", 9, GAP_S, weight="bold")
        ly += 12
        for gap in gaps:
            svg.text(lx, ly, gap, 7.5, GAP_S)
            ly += 11

    extra = list(notes)
    if factor:
        extra.append(f"Drawing binned {factor}x{factor} ({len(cells)} mesh cells -> "
                     f"{len(drawn)} squares) so the SVG stays small enough for GitHub. "
                     f"Values are averaged per bin; a cell over its ceiling keeps the "
                     f"over-ceiling mark. The report's numbers use the full mesh.")
    if interior_dropped:
        extra.append(f"{interior_dropped} interior Hele-Shaw faces are not drawn: the "
                     f"cell squares already carry what they transmit. Ducts, mouths and "
                     f"weir slits are drawn.")
    row = max(v.y(0) + 52, ly + 16)
    svg.line(pad_l, row - 12, w - 14, row - 12, GRID, 1.0)
    for note in extra + _standing_notes(chip, gaps):
        svg.text(pad_l, row, note, 8.5, GAP_S if note.startswith(("GAP", "NOT")) else INK2)
        row += 12
    svg.h = max(svg.h, row + 14)
    name = f"{chip_id}_s{step if step is not None else 'x'}_{kind}.svg"
    return svg.save(Path(out_dir) / name)


def _standing_notes(chip, gaps):
    out = ["SIMULATED, not measured: depth-averaged Hele-Shaw (Stokes) network solve. "
           "Re > 1 regions are outside the model and are reported as findings, not numbers."]
    if is_demo_scale(chip):
        out.append("DEMO SCALE chip: widths were clamped to the fabrication floor when it "
                   "was scaled down, so these resistances are not the original design's.")
    if gaps:
        out.append("NOT YET AVAILABLE: " + "; ".join(gaps))
    return out


def _wrap(text, width):
    words, lines, line = str(text).split(), [], ""
    for word in words:
        if len(line) + len(word) + 1 > width and line:
            lines.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        lines.append(line)
    return lines


def step_figures(chip, out_dir, mesh=None, field=None, transport=None, findings=(),
                 step=None, step_label=None, step_data=None,
                 kinds=("pressure", "speed", "shear", "residence")):
    """All four maps for one chip and one protocol step. Returns the paths written."""
    out = []
    for kind in kinds:
        try:
            out.append(map_figure(chip, kind, out_dir, mesh, field, transport,
                                  findings, step, step_label, step_data=step_data))
        except Exception as exc:                        # a figure never stops a run
            out.append(_failed_figure(chip, kind, out_dir, step, exc))
    return out


def _failed_figure(chip, kind, out_dir, step, exc):
    chip_id = str((chip or {}).get("id") or "chip")
    svg = Svg(560, 150, f"{chip_id}: {kind} figure failed")
    svg.text(20, 30, f"{chip_id}  ·  {kind}", 14, INK, weight="bold")
    svg.rect(16, 46, 520, 80, GAP_F, GAP_S, 1.2, rx=4)
    svg.text(28, 70, "FIGURE FAILED - the run continued", 10, GAP_S, weight="bold")
    for i, line in enumerate(_wrap(f"{type(exc).__name__}: {exc}", 70)[:3]):
        svg.text(28, 88 + i * 12, line, 8, INK2, mono=True)
    return svg.save(Path(out_dir) / f"{chip_id}_s{step}_{kind}.svg")


# --- the pipeline overview -------------------------------------------------------------

STATUS_C = {
    "final": "#2f7d32",
    "translated": "#2f6fb0",
    "draft": "#bf7b2f",
    "missing": "#c0392b",
    "placeholder": "#c0392b",
}


def worst_severity(findings):
    present = {str(as_dict(f).get("severity") or "info").lower() for f in findings or []}
    for severity in SEVERITY_ORDER:
        if severity in present:
            return severity
    return None


def pipeline_overview(pipeline, out_dir, chips=None, findings_by_chip=None,
                      statuses=None, coupling=None):
    """Every chip in chain order, the handoffs between them, a severity badge on each.

    `pipeline` is protocols/pipeline_mvp.json. `chips` maps module id -> chip spec,
    `findings_by_chip` maps chip id -> findings, `statuses` maps module id -> a short
    string for modules whose CFD could not run.
    """
    pipeline = pipeline or {}
    chips = chips or {}
    findings_by_chip = findings_by_chip or {}
    statuses = statuses or {}
    modules = pipeline.get("modules") or []
    handoffs = pipeline.get("handoffs") or []
    hand_by_from: dict[str, list] = {}
    for h in handoffs:
        key = str((h.get("from") or {}).get("module") or (h.get("from") or {}).get("roadmap")
                  or h.get("from") or "")
        hand_by_from.setdefault(key, []).append(h)

    box_w, box_h, gap = 300.0, 74.0, 46.0
    pad_l, pad_t = 34.0, 118.0
    w = pad_l + box_w + 500.0
    h = pad_t + len(modules) * (box_h + gap) + 190.0
    svg = Svg(w, h, f"{pipeline.get('id', 'pipeline')}: SIMULATED CFD overview, "
                    f"chain order with handoffs and severity")

    svg.text(pad_l, 28, pipeline.get("name", pipeline.get("id", "pipeline"))[:96], 14.5,
             INK, weight="bold")
    svg.text(pad_l, 46, f"{len(modules)} modules in chain order  ·  {len(handoffs)} "
                        f"handoffs  ·  SIMULATED CFD (S10), depth-averaged Hele-Shaw",
             9.5, INK2)
    x = svg.badge(pad_l, 56, "SIMULATED", "#6b5bb0")
    x = svg.badge(x, 56, "not measured", GAP_S)
    svg.text(w - 16, 28, "generated by", 8, INK3, "end")
    svg.text(w - 16, 40, "assay2chip/cfd/figures.py", 8, INK3, "end", mono=True)

    svg.text(pad_l, 96, "CHIP", 8.5, INK3, weight="bold")
    svg.text(pad_l + box_w + 30, 96, "HANDOFF OUT OF THIS CHIP", 8.5, INK3, weight="bold")

    y = pad_t
    for index, module in enumerate(modules):
        mid = str(module.get("module") or module.get("id") or f"m{index}")
        chip = chips.get(mid) or {}
        chip_id = str(chip.get("id") or Path(str(module.get("chip") or "")).stem or "-")
        status = str(module.get("status") or "unknown").lower()
        colour = STATUS_C.get(status, INK3)
        findings = findings_by_chip.get(chip_id) or []
        severity = worst_severity(findings)
        badge_c = SEVERITY_C.get(severity, "#2f7d32")

        svg.rect(pad_l, y, box_w, box_h, PAPER, colour, 1.6, rx=5)
        svg.rect(pad_l, y, 5, box_h, colour, "none", rx=2)
        svg.text(pad_l + 14, y + 19, f"{index + 1}. {mid}", 10.5, INK, weight="bold",
                 mono=True)
        svg.text(pad_l + 14, y + 34, str(module.get("name", ""))[:46], 8.5, INK2)
        if chip:
            fx, fy = footprint(chip)
            demo = "  ·  DEMO SCALE" if is_demo_scale(chip) else ""
            svg.text(pad_l + 14, y + 48, f"chip/{chip_id}.json  ·  {fx:g} × {fy:g} mm{demo}",
                     8, INK3, mono=True)
            svg.text(pad_l + 14, y + 62,
                     f"{len(chip.get('chambers') or [])} chambers  ·  "
                     f"{len(chip.get('channels') or [])} channels  ·  "
                     f"{len(chip.get('ports') or [])} ports", 8, INK3)
        else:
            svg.text(pad_l + 14, y + 48, str(module.get("status_note")
                                             or "no chip: placeholder")[:46], 8, GAP_S)
        bx = pad_l + box_w - 8
        label = (f"{severity.upper()} ×{sum(1 for f in findings if str(as_dict(f).get('severity', '')).lower() == severity)}"
                 if severity else ("CLEAN" if chip else "NO CFD"))
        wpx = len(label) * 8.5 * 0.62 + 10
        svg.rect(bx - wpx, y + 8, wpx, 15, PAPER, badge_c if chip else GAP_S, 1.3, rx=7.5)
        svg.text(bx - wpx / 2, y + 19, label, 8.5, badge_c if chip else GAP_S, "middle",
                 "bold")
        svg.badge(pad_l + box_w - 86, y + box_h - 22, status, colour, 7.5, 4.0, 13.0)

        note = statuses.get(mid)
        if note:
            svg.halo(pad_l + 14, y + box_h + 14, f"CFD: {note}"[:64], 7.5, GAP_S)

        hx = pad_l + box_w + 30
        hy = y + 16
        for hand in hand_by_from.get(mid, []):
            to = hand.get("to") or {}
            target = str(to.get("module") or to.get("roadmap") or "?")
            carries = str(hand.get("carries") or "?")
            connector = (hand.get("connector") or {}).get("status", "")
            warn = (hand.get("connector") or {}).get("warning")
            bits = [f"→ {target}", f"carries {carries}"]
            if hand.get("volume_ul"):
                bits.append(f"{sig(hand['volume_ul'])} µL")
            if hand.get("particle_um"):
                bits.append(f"{sig(hand['particle_um'])} µm")
            tone = GAP_S if connector not in ("match", "") else INK2
            svg.text(hx, hy, "  ·  ".join(bits)[:70], 8.5, tone, mono=True)
            hy += 11
            if connector:
                svg.text(hx + 10, hy, f"connector: {connector}", 7.5,
                         GAP_S if connector != "match" else INK3)
                hy += 10
            if warn:
                for line in _wrap(str(warn), 58)[:2]:
                    svg.text(hx + 10, hy, line, 7.5, GAP_S)
                    hy += 10
            dead = (coupling or {}).get(str(hand.get("id")), {})
            if dead:
                svg.text(hx + 10, hy,
                         f"dead volume {sig(dead.get('dead_volume_ul'))} µL  ·  "
                         f"dP {sig(dead.get('dp_kpa'))} kPa", 7.5, INK3, mono=True)
                hy += 10
            hy += 4
        if not hand_by_from.get(mid):
            svg.text(hx, hy, "no handoff recorded out of this module", 8, INK3)

        if index < len(modules) - 1:
            mx = pad_l + box_w / 2
            svg.line(mx, y + box_h, mx, y + box_h + gap - 10, INK3, 1.4)
            svg.poly([(mx - 5, y + box_h + gap - 10), (mx + 5, y + box_h + gap - 10),
                      (mx, y + box_h + gap - 2)], INK3, "none")
        y += box_h + gap

    row = y + 16
    svg.line(pad_l, row - 14, w - 16, row - 14, GRID, 1.0)
    svg.text(pad_l, row, "SEVERITY", 8.5, INK3, weight="bold")
    bx = pad_l + 70
    for severity in SEVERITY_ORDER:
        colour = SEVERITY_C[severity]
        svg.rect(bx, row - 9, 12, 11, PAPER, colour, 1.3, rx=2)
        svg.text(bx + 18, row, severity, 8.5, colour)
        bx += 30 + len(severity) * 5.4
    row += 18
    for note in [
        "Every number in this figure is SIMULATED (depth-averaged Hele-Shaw), not "
        "measured. The badge on a chip is its worst finding, not a pass or a fail.",
        "A module with no chip is a placeholder in protocols/pipeline_mvp.json: there "
        "is nothing to simulate, so it carries NO CFD rather than CLEAN.",
    ]:
        for line in _wrap(note, 150):
            svg.text(pad_l, row, line, 8.5, INK2)
            row += 12
    svg.h = max(svg.h, row + 16)
    return svg.save(Path(out_dir) / "pipeline_overview.svg")


# --- CLI, for looking at one chip's geometry without the rest of the package -----------

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chip", help="chip/<id>.json: draw the four maps, empty if no solver")
    ap.add_argument("--pipeline", help="protocols/pipeline_mvp.json: draw the overview")
    ap.add_argument("--out", default=None, help="output directory")
    args = ap.parse_args(argv)
    out = Path(args.out or (ROOT / "assay2chip" / "work" / "cfd_figures"))
    out.mkdir(parents=True, exist_ok=True)
    written = []
    if args.chip:
        chip = json.loads(Path(args.chip).read_text())
        written += step_figures(chip, out, step=None)
    if args.pipeline:
        pipeline = json.loads(Path(args.pipeline).read_text())
        chips = {}
        for module in pipeline.get("modules") or []:
            path = ROOT / str(module.get("chip") or "")
            if module.get("chip") and path.exists():
                chips[str(module.get("module"))] = json.loads(path.read_text())
        written.append(pipeline_overview(pipeline, out, chips))
    if not written:
        ap.error("nothing to draw: pass --chip and/or --pipeline")
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

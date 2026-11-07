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


# --- DXF files ------------------------------------------------------------------------

INSUNITS_MM = 4          # DXF $INSUNITS: 4 = millimetres
LAYER_COLOUR = {"CHAMBERS": 5, "CHANNELS": 3, "GROOVES": 1}


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


def build(spec_path: Path) -> int:
    chip = json.loads(spec_path.read_text())
    if not all(k in chip for k in ("id", "chambers", "ports", "channels", "layers")):
        print(f"{spec_path.name}: skipped, not a chip spec "
              f"(no id/chambers/ports/channels/layers): nothing written")
        return 0
    # Part files are named by bare id, and ids repeat across chips: both of ours have an
    # 'inlet_1'. twin/main.js loads cad/exports/ flat, so the flat namespace can only
    # hold one chip, and it holds the default one. Every other chip gets its own folder.
    is_default = spec_path.resolve() == DEFAULT_CHIP.resolve()
    out = EXPORTS if is_default else EXPORTS / chip["id"]
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

    anchors = {c["id"]: (c["center"], c["size"]) for c in chip["chambers"]}
    for port in chip["ports"]:
        x, y, z = port["position"]
        anchors[port["id"]] = ([x, y, z], [PORT_SIZE_MM, PORT_SIZE_MM, 0])

    # The spacer is the layer whose thickness sets the chamber height.
    depth = chip["chambers"][0]["size"][2]
    spacer = next((l for l in chip["layers"] if abs(l["thickness_mm"] - depth) < 1e-6), None)

    chamber_lines, channel_lines_out, groove_lines = [], [], []
    chamber_polys, channel_polys, groove_polys = [], [], []
    for ch in chip["chambers"]:
        poly = rect_polygon(ch["center"][0], ch["center"][1], ch["size"][0], ch["size"][1])
        chamber_polys.append(poly)
        chamber_lines += polygon_lines(poly)
    for ch in chip["channels"]:
        a, b = anchors.get(ch["from"]), anchors.get(ch["to"])
        if a is None or b is None:
            continue
        p0 = clip_to_box(a[0][:2], b[0][:2], a[0], a[1])
        p1 = clip_to_box(b[0][:2], a[0][:2], b[0], b[1])
        full = channel_polygon(p0, p1, ch["width_mm"])
        if not full:
            continue
        # The cut path: trim the end cap back onto the wall of each CHAMBER this channel
        # enters, so the channel stops at the wall instead of straddling it. A port is a
        # hole in the lid, not an outline in this drawing, so there is nothing to meet
        # there and its end cap is left square.
        span = (p1[0] - p0[0], p1[1] - p0[1])
        cut = full
        if ch["from"] in by_id:
            cut = trim_to_wall(cut, p0, span, a[0], a[1])
        if ch["to"] in by_id:
            cut = trim_to_wall(cut, p1, (-span[0], -span[1]), b[0], b[1])
        if len(cut) < 3 or polygon_area(cut) < 1e-9:
            print(f"  WARNING: channel '{ch['id']}' vanishes when trimmed to its chambers' "
                  f"walls (it is shorter than its own width): left untrimmed")
            cut = full
        # A channel shallower than the fluidic layer is a second depth, which one
        # embossing tool cannot make. It goes on its own layer so it cannot be missed.
        if spacer and ch["depth_mm"] < spacer["thickness_mm"] - 1e-6:
            groove_lines += polygon_lines(full)
            groove_polys.append(cut)
        else:
            channel_lines_out += polygon_lines(full)
            channel_polys.append(cut)

    layers = {"CHAMBERS": chamber_lines, "CHANNELS": channel_lines_out}
    closed = {"CHAMBERS": chamber_polys, "CHANNELS": channel_polys}
    if groove_lines:
        layers["GROOVES"] = groove_lines
        closed["GROOVES"] = groove_polys

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
    wanted = {p.name for p in written} | {f"{chip['id']}.stl", f"{chip['id']}.glb"}
    stale = sorted(p for p in out.glob("*") if p.name not in wanted
                   and p.suffix.lower() in (".stl", ".dxf"))
    owned_flat = out == EXPORTS
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
    args = ap.parse_args()
    if args.all and args.chip:
        ap.error("--all and --chip are exclusive")
    specs = chip_specs() if args.all else [args.chip or DEFAULT_CHIP]
    worst = 0
    for i, spec in enumerate(specs):
        if i:
            print()
        worst = max(worst, build(spec))
    return worst


if __name__ == "__main__":
    raise SystemExit(main())

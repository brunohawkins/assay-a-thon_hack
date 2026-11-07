"""Export a printable DISPLAY MODEL of a chip spec (a scaled-up show piece, not a working chip).

    python cad/export_display_model.py cad/m3_wash_spec.json
    python cad/export_display_model.py chip/placeholder_chip_spec.json --scale 5

Scales every dimension by display_scale (cad/fabrication.json, default 10) and writes
binary STLs to cad/display/<chip_id>/:

  <part_id>.stl                 one per part: base, each chamber, channel and port, label
  <chip_id>_display_print.stl   all parts in place, in one file: SEND THIS TO THE PRINTER
  manifest.json                 parts, scale and sizes (the twin's display view reads it)

How parts are shown, so the model reads from across a room:
  - base: the bottom layer, as a solid plate (the lid is left out so you can see inside)
  - chambers: a rim at the chamber's real (scaled) height (round for cylinder chambers); a
    mesh floor becomes a raised lattice with a coarse pitch, so it reads as a mesh from a distance
  - channels: raised ribs along the channel path (the spec's explicit path when it has one),
    at the channel's scaled width and depth
  - ports: hollow tubes; tall for ports on top, short for ports under the mesh
  - valves: small discs on their channel; features (windows, heated zones): thin frames
  - module chips: every file is named by a module-prefixed id (m2_base.stl, m2_label.stl ...)
  - label plate in front, with raised lettering "Display model – <scale>× scale" (label_text.stl)

Standard library only. The real working dimensions stay in the chip spec and the twin.
"""

from __future__ import annotations

import argparse
import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))

import schema_io  # noqa: E402

DISPLAY_DIR = ROOT / "cad" / "display"

# Display-model styling, in display mm (after scaling).
RIM_WALL = 1.6          # chamber rim wall thickness
LATTICE_PITCH = 5.0     # mesh lattice pitch
LATTICE_BAR = 1.2       # mesh lattice bar width
LATTICE_HEIGHT = 0.9    # mesh lattice height (below the rim)
MIN_FEATURE = 1.0       # nothing thinner than this, so it prints
LOW_PORT_HEIGHT = 2.5   # ports under the mesh are short tubes
LABEL_DEPTH = 18.0      # label plate size (y) and thickness
LABEL_THICK = 3.0
LABEL_TEXT_HEIGHT = 1.2

Tri = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]


# --- mesh primitives ------------------------------------------------------------------


def box(x0, y0, z0, x1, y1, z1) -> list[Tri]:
    """Axis-aligned box as 12 outward-facing triangles."""
    v = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
         (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
    faces = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4),
             (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return [(v[a], v[b], v[c]) for a, b, c in faces]


def tube(cx, cy, z0, z1, r_out, r_in, segments=48) -> list[Tri]:
    """Hollow cylinder (a port)."""
    tris: list[Tri] = []
    for i in range(segments):
        a0, a1 = 2 * math.pi * i / segments, 2 * math.pi * (i + 1) / segments
        o0 = (cx + r_out * math.cos(a0), cy + r_out * math.sin(a0))
        o1 = (cx + r_out * math.cos(a1), cy + r_out * math.sin(a1))
        i0 = (cx + r_in * math.cos(a0), cy + r_in * math.sin(a0))
        i1 = (cx + r_in * math.cos(a1), cy + r_in * math.sin(a1))
        tris += [((*o0, z0), (*o1, z0), (*o1, z1)), ((*o0, z0), (*o1, z1), (*o0, z1))]      # outer wall
        tris += [((*i0, z0), (*i1, z1), (*i1, z0)), ((*i0, z0), (*i0, z1), (*i1, z1))]      # inner wall
        tris += [((*o0, z1), (*o1, z1), (*i1, z1)), ((*o0, z1), (*i1, z1), (*i0, z1))]      # top ring
        tris += [((*o0, z0), (*i1, z0), (*o1, z0)), ((*o0, z0), (*i0, z0), (*i1, z0))]      # bottom ring
    return tris


def write_stl(path: Path, tris: list[Tri], name: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(f"display model {name}".encode("ascii")[:80].ljust(80, b" "))
        f.write(struct.pack("<I", len(tris)))
        for a, b, c in tris:
            u = [b[i] - a[i] for i in range(3)]
            w = [c[i] - a[i] for i in range(3)]
            n = [u[1] * w[2] - u[2] * w[1], u[2] * w[0] - u[0] * w[2], u[0] * w[1] - u[1] * w[0]]
            ln = math.sqrt(sum(x * x for x in n)) or 1.0
            f.write(struct.pack("<3f", *(x / ln for x in n)))
            for p in (a, b, c):
                f.write(struct.pack("<3f", *p))
            f.write(b"\0\0")


def bbox(tris: list[Tri]) -> tuple[list[float], list[float]]:
    pts = [p for t in tris for p in t]
    return [min(p[i] for p in pts) for i in range(3)], [max(p[i] for p in pts) for i in range(3)]


# --- label font (5 x 7 pixels) -----------------------------------------------------------

FONT = {
    "D": ["####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."],
    "i": ["..#..", ".....", ".##..", "..#..", "..#..", "..#..", ".###."],
    "s": [".....", ".....", ".####", "#....", ".###.", "....#", "####."],
    "p": [".....", ".....", "####.", "#...#", "####.", "#....", "#...."],
    "l": [".##..", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "a": [".....", ".....", ".###.", "....#", ".####", "#...#", ".####"],
    "y": [".....", ".....", "#...#", "#...#", ".####", "....#", ".###."],
    "m": [".....", ".....", "##.#.", "#.#.#", "#.#.#", "#...#", "#...#"],
    "o": [".....", ".....", ".###.", "#...#", "#...#", "#...#", ".###."],
    "d": ["....#", "....#", ".##.#", "#..##", "#...#", "#...#", ".####"],
    "e": [".....", ".....", ".###.", "#...#", "#####", "#....", ".###."],
    "c": [".....", ".....", ".###.", "#....", "#....", "#...#", ".###."],
    "–": [".....", ".....", ".....", "#####", ".....", ".....", "....."],
    "×": [".....", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "....."],
    ".": [".....", ".....", ".....", ".....", ".....", ".##..", ".##.."],
    " ": ["....."] * 7,
    "0": [".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."],
    "1": ["..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."],
    "2": [".###.", "#...#", "....#", "...#.", "..#..", ".#...", "#####"],
    "3": ["####.", "....#", "....#", ".###.", "....#", "....#", "####."],
    "4": ["...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."],
    "5": ["#####", "#....", "####.", "....#", "....#", "#...#", ".###."],
    "6": ["..##.", ".#...", "#....", "####.", "#...#", "#...#", ".###."],
    "7": ["#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."],
    "8": [".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."],
    "9": [".###.", "#...#", "#...#", ".####", "....#", "...#.", ".##.."],
}


def text_boxes(text: str, x0: float, y0: float, z0: float, px: float, height: float) -> list[Tri]:
    """Raised pixel text; each run of lit pixels in a row is one box."""
    tris: list[Tri] = []
    for k, ch in enumerate(text):
        glyph = FONT.get(ch)
        if glyph is None:
            raise ValueError(f"no glyph for {ch!r} in the label font")
        gx = x0 + k * 6 * px
        for row, bits in enumerate(glyph):
            y_top = y0 + (6 - row) * px
            col = 0
            while col < 5:
                if bits[col] == "#":
                    start = col
                    while col < 5 and bits[col] == "#":
                        col += 1
                    tris += box(gx + start * px, y_top, z0, gx + col * px, y_top + px, z0 + height)
                else:
                    col += 1
    return tris


# --- chip -> parts ---------------------------------------------------------------------


def channel_route(a: dict, b: dict, chambers: dict) -> list[tuple[float, float]]:
    """xy path of a channel, matching the twin: chamber<->port leaves the chamber edge
    facing the port; a port inside the chamber outline is vertical (no xy path)."""
    def pt(end):
        return chambers[end]["center"][:2] if end in chambers else a_ports[end]["position"][:2]
    (ax, ay), (bx, by) = pt(a["id"]), pt(b["id"])
    a_ch, b_ch = a["id"] in chambers, b["id"] in chambers
    if a_ch and b_ch:  # chamber to chamber: right-angle route between facing edges
        sa, sb = chambers[a["id"]]["size"], chambers[b["id"]]["size"]
        dirx = 1 if bx >= ax else -1
        p0, p3 = (ax + dirx * sa[0] / 2, ay), (bx - dirx * sb[0] / 2, by)
        if abs(ay - by) < 1e-6:
            return [p0, p3]
        mid = (p0[0] + p3[0]) / 2
        return [p0, (mid, ay), (mid, by), p3]
    ch, port = (a, b) if a_ch else (b, a)
    (cx, cy), size = chambers[ch["id"]]["center"][:2], chambers[ch["id"]]["size"]
    px, py = a_ports[port["id"]]["position"][:2]
    hx, hy = size[0] / 2, size[1] / 2
    if abs(px - cx) <= hx and abs(py - cy) <= hy:
        return []  # port over/under the chamber: vertical connection
    if abs(px - cx) / hx >= abs(py - cy) / hy:
        edge = (cx + math.copysign(hx, px - cx), min(max(py, cy - hy), cy + hy))
        path = [edge, (px, edge[1])] if abs(edge[1] - py) > 1e-6 else [edge]
    else:
        edge = (min(max(px, cx - hx), cx + hx), cy + math.copysign(hy, py - cy))
        path = [edge, (edge[0], py)] if abs(edge[0] - px) > 1e-6 else [edge]
    return path + [(px, py)]


a_ports: dict = {}


def build_parts(chip: dict, scale: float) -> list[dict]:
    global a_ports
    s = scale
    chambers = {c["id"]: c for c in chip["chambers"]}
    a_ports = {p["id"]: p for p in chip["ports"]}
    layers = chip["layers"]
    base_t = layers[0]["thickness_mm"] * s
    top = sum(l["thickness_mm"] for l in layers) * s
    fx, fy = chip["footprint"]["x"] * s, chip["footprint"]["y"] * s
    parts = [{"id": layers[0]["id"], "kind": "base", "tris": box(0, 0, 0, fx, fy, base_t)}]

    for c in chip["chambers"]:
        (cx, cy, _), (sx, sy, _) = [v * s for v in c["center"]], [v * s for v in c["size"]]
        h = max(c.get("height_um", c["size"][2] * 1000) / 1000 * s, MIN_FEATURE)
        x0, x1, y0, y1 = cx - sx / 2, cx + sx / 2, cy - sy / 2, cy + sy / 2
        z0, z1 = base_t, base_t + h
        w = RIM_WALL
        if c.get("shape") == "cylinder":
            tris = tube(cx, cy, z0, z1, sx / 2 + w, sx / 2)
        else:
            tris = (box(x0 - w, y0 - w, z0, x1 + w, y0, z1) + box(x0 - w, y1, z0, x1 + w, y1 + w, z1) +
                    box(x0 - w, y0, z0, x0, y1, z1) + box(x1, y0, z0, x1 + w, y1, z1))
        ret = c.get("retention") or ({"type": "mesh"} if c.get("mesh_pore_um") else None)
        if ret and ret["type"] == "mesh":
            lz = z0 + min(LATTICE_HEIGHT, h * 0.8)
            n_x = max(2, round(sx / LATTICE_PITCH))
            n_y = max(2, round(sy / LATTICE_PITCH))
            for i in range(1, n_x):
                gx = x0 + sx * i / n_x
                tris += box(gx - LATTICE_BAR / 2, y0, z0, gx + LATTICE_BAR / 2, y1, lz)
            for j in range(1, n_y):
                gy = y0 + sy * j / n_y
                tris += box(x0, gy - LATTICE_BAR / 2, z0, x1, gy + LATTICE_BAR / 2, lz)
        parts.append({"id": c["id"], "kind": "chamber", "tris": tris})

    for ch in chip["channels"]:
        a = {"id": ch["from"]}
        b = {"id": ch["to"]}
        path = [tuple(pt) for pt in ch["path"]] if ch.get("path") else channel_route(a, b, chambers)
        w = max(ch["width_mm"] * s, MIN_FEATURE)
        h = max(ch["depth_mm"] * s, MIN_FEATURE)
        tris: list[Tri] = []
        for (x0, y0), (x1, y1) in zip([(p[0] * s, p[1] * s) for p in path[:-1]], [(p[0] * s, p[1] * s) for p in path[1:]]):
            tris += box(min(x0, x1) - w / 2, min(y0, y1) - w / 2, base_t, max(x0, x1) + w / 2, max(y0, y1) + w / 2, base_t + h)
        if tris:
            parts.append({"id": ch["id"], "kind": "channel", "tris": tris})

    for p in chip["ports"]:
        px, py, pz = [v * s for v in p["position"]]
        r_out = max(p.get("width_um", 1000) / 1000 * s / 2, 2.0) + 1.2
        r_in = r_out - 1.2
        bottom = pz < top / 2
        z1 = base_t + LOW_PORT_HEIGHT if bottom else top
        parts.append({"id": p["id"], "kind": "port_bottom" if bottom else "port_top",
                      "tris": tube(px, py, base_t, z1, r_out, r_in)})

    for v in chip.get("valves", []):
        vx, vy, _ = [c * s for c in v["position"]]
        rv = max(0.6 * s, 2 * MIN_FEATURE)
        parts.append({"id": v["id"], "kind": "valve", "tris": tube(vx, vy, base_t, base_t + max(0.3 * s, 2.0), rv, 0.0001)})
    for f in chip.get("features", []):
        (fcx, fcy, _), (fsx, fsy, _) = [v * s for v in f["center"]], [v * s for v in f["size"]]
        fx0, fx1, fy0, fy1, fw, fz = fcx - fsx / 2, fcx + fsx / 2, fcy - fsy / 2, fcy + fsy / 2, MIN_FEATURE, base_t + 0.6
        tris = (box(fx0, fy0, base_t, fx1, fy0 + fw, fz) + box(fx0, fy1 - fw, base_t, fx1, fy1, fz) +
                box(fx0, fy0, base_t, fx0 + fw, fy1, fz) + box(fx1 - fw, fy0, base_t, fx1, fy1, fz))
        parts.append({"id": f["id"], "kind": "feature", "tris": tris})

    label = f"Display model – {scale:g}× scale"
    px = min(1.6, (fx - 12) / (len(label) * 6))
    text_w = len(label) * 6 * px
    prefix = f"{chip['module']['id']}_" if chip.get("module") else ""   # module chips: every file is module-prefixed
    parts.append({"id": f"{prefix}label", "kind": "label", "tris": box(0, -LABEL_DEPTH, 0, fx, 0, LABEL_THICK), "text": label})
    # Raised lettering as its own part, so it can be printed (and shown) in a second colour.
    parts.append({"id": f"{prefix}label_text", "kind": "label_text",
                  "tris": text_boxes(label, (fx - text_w) / 2, -LABEL_DEPTH + (LABEL_DEPTH - 7 * px) / 2,
                                     LABEL_THICK, px, LABEL_TEXT_HEIGHT)})
    return parts


def export(spec_path: Path, scale: float | None = None) -> dict:
    chip = schema_io.load_json(spec_path)
    errors = schema_io.schema_errors("chip_spec", chip)
    if errors:
        raise ValueError(f"{spec_path}: not a valid chip spec; run tools/validate.py")
    if scale is None:
        scale = float(schema_io.load_json(ROOT / "cad" / "fabrication.json").get("display_scale", 10))
    out_dir = DISPLAY_DIR / chip["id"]
    parts = build_parts(chip, scale)
    for old in out_dir.glob("*.stl"):
        old.unlink()  # only files this script wrote: cad/display/<chip_id>/
    entries, everything = [], []
    for part in parts:
        write_stl(out_dir / f"{part['id']}.stl", part["tris"], part["id"])
        lo, hi = bbox(part["tris"])
        entries.append({"id": part["id"], "kind": part["kind"], "file": f"{part['id']}.stl",
                        "bbox_mm": [[round(v, 2) for v in lo], [round(v, 2) for v in hi]]})
        everything += part["tris"]
    print_file = f"{chip['id']}_display_print.stl"
    write_stl(out_dir / print_file, everything, f"{chip['id']} print")
    lo, hi = bbox(everything)
    manifest = {
        "chip_id": chip["id"],
        "chip_spec": spec_path.resolve().relative_to(ROOT).as_posix(),
        "display_scale": scale,
        "units": "mm",
        "note": "Display model: real chip geometry x display_scale. Not a working chip. The lid layer is left out.",
        "print_file": print_file,
        "overall_mm": [round(h - l, 2) for h, l in zip(hi, lo)],
        "min_mm": [round(v, 2) for v in lo],
        "real_footprint_mm": [chip["footprint"]["x"], chip["footprint"]["y"]],
        "label": next(p["text"] for p in parts if p["kind"] == "label"),
        "parts": entries,
    }
    schema_io.write_json(out_dir / "manifest.json", manifest)
    return manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Export a printable display model of a chip spec.")
    ap.add_argument("spec", type=Path, help="chip spec, e.g. cad/m3_wash_spec.json")
    ap.add_argument("--scale", type=float, default=None, help="override display_scale from cad/fabrication.json")
    args = ap.parse_args(argv)
    try:
        m = export(args.spec, args.scale)
    except (ValueError, FileNotFoundError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    x, y, z = m["overall_mm"]
    print(f"wrote cad/display/{m['chip_id']}/: {len(m['parts'])} part STLs + {m['print_file']} + manifest.json")
    print(f"PRINT THIS: cad/display/{m['chip_id']}/{m['print_file']}  ({x:g} x {y:g} x {z:g} mm, {m['display_scale']:g}x scale)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

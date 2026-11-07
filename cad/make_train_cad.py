"""Generate CAD for a chip spec: one STL per part, plus a laser-cut DXF of the spacer layer.

    python cad/make_train_cad.py                     # default: the iPSC-to-MGL train
    python cad/make_train_cad.py --chip chip/<spec>.json

Writes, into cad/exports/:
  <chamber_id>.stl   the chamber cavity, a box, exported in place in mm
  <port_id>.stl      the through-hole in the lid, in place
  <chip_id>.dxf      the cut path for the spacer layer: every chamber and channel

Why a box is the right shape here: chamber height is set by an adhesive spacer, so a
chamber really is a rectangle cut out of one flat layer. The DXF is that cut path and
can go straight to a laser cutter. Channels are trimmed to the chamber walls, so the
outline has no lines running through the inside of a chamber.

Standard library only, and it never touches files it did not write. See cad/README.md
for the export rules the twin depends on (names, millimetres, shared origin).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXPORTS = ROOT / "cad" / "exports"
DEFAULT_CHIP = ROOT / "chip" / "ipsc_to_mgl_chip_v1.json"

PORT_SIZE_MM = 2.0   # square through-hole drawn for each tubing port


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


# --- DXF ------------------------------------------------------------------------------

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


def rect_lines(cx, cy, sx, sy):
    x0, x1 = cx - sx / 2, cx + sx / 2
    y0, y1 = cy - sy / 2, cy + sy / 2
    return [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)),
            ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))]


def channel_lines(a, b, width):
    """A channel as a rectangle of `width` running from point a to point b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = (dx * dx + dy * dy) ** 0.5
    if length < 1e-6:
        return []
    ux, uy = -dy / length * width / 2, dx / length * width / 2
    c = [(a[0] + ux, a[1] + uy), (b[0] + ux, b[1] + uy),
         (b[0] - ux, b[1] - uy), (a[0] - ux, a[1] - uy)]
    return [(c[i], c[(i + 1) % 4]) for i in range(4)]


def write_dxf(path: Path, layers: dict[str, list]) -> None:
    out = ["0", "SECTION", "2", "ENTITIES"]
    for layer, lines in layers.items():
        for (x0, y0), (x1, y1) in lines:
            out += ["0", "LINE", "8", layer,
                    "10", f"{x0:.4f}", "20", f"{y0:.4f}", "30", "0.0",
                    "11", f"{x1:.4f}", "21", f"{y1:.4f}", "31", "0.0"]
    out += ["0", "ENDSEC", "0", "EOF", ""]
    path.write_text("\n".join(out))


# --- main -----------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chip", type=Path, default=DEFAULT_CHIP)
    args = ap.parse_args()

    chip = json.loads(args.chip.read_text())
    EXPORTS.mkdir(parents=True, exist_ok=True)
    by_id = {c["id"]: c for c in chip["chambers"]}
    written = []

    for ch in chip["chambers"]:
        out = EXPORTS / f"{ch['id']}.stl"
        write_stl(out, ch["id"], box_triangles(ch["center"], ch["size"]))
        written.append(out)

    lid = chip["layers"][-1]["thickness_mm"]
    for port in chip["ports"]:
        x, y, z = port["position"]
        out = EXPORTS / f"{port['id']}.stl"
        write_stl(out, port["id"],
                  box_triangles([x, y, z - lid / 2], [PORT_SIZE_MM, PORT_SIZE_MM, lid]))
        written.append(out)

    anchors = {c["id"]: (c["center"], c["size"]) for c in chip["chambers"]}
    for port in chip["ports"]:
        x, y, z = port["position"]
        anchors[port["id"]] = ([x, y, z], [PORT_SIZE_MM, PORT_SIZE_MM, 0])

    chamber_lines, channel_outlines = [], []
    for ch in chip["chambers"]:
        chamber_lines += rect_lines(ch["center"][0], ch["center"][1], ch["size"][0], ch["size"][1])
    for ch in chip["channels"]:
        a, b = anchors.get(ch["from"]), anchors.get(ch["to"])
        if a is None or b is None:
            continue
        p0 = clip_to_box(a[0][:2], b[0][:2], a[0], a[1])
        p1 = clip_to_box(b[0][:2], a[0][:2], b[0], b[1])
        channel_outlines += channel_lines(p0, p1, ch["width_mm"])

    dxf = EXPORTS / f"{chip['id']}.dxf"
    write_dxf(dxf, {"CHAMBERS": chamber_lines, "CHANNELS": channel_outlines})
    written.append(dxf)

    spacer = next((l for l in chip["layers"] if "psa" in l["material"] or l is chip["layers"][1]), None)
    print(f"chip {chip['id']}: {len(by_id)} chambers, {len(chip['ports'])} ports, "
          f"{len(chip['channels'])} channels")
    for p in written:
        print(f"  wrote cad/exports/{p.name}")
    if spacer:
        print(f"\n{dxf.name} is the cut path for the '{spacer['id']}' layer "
              f"({spacer['thickness_mm']} mm {spacer['material']}).")
        print("Layers in the DXF: CHAMBERS, CHANNELS. Units are mm, origin as in the chip spec.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

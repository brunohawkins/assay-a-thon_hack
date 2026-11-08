"""Generate CAD for a chip spec: one STL per part, plus a DXF outline of the fluidic layer.

    python cad/make_train_cad.py                     # default: the iPSC-to-MGL train
    python cad/make_train_cad.py --chip chip/<spec>.json

Writes, into cad/exports/:
  <chamber_id>.stl   the chamber cavity, a box, exported in place in mm
  <port_id>.stl      the through-hole in the lid, in place
  <chip_id>.dxf      the outline of the fluidic layer: every chamber and channel

Why a box is the right shape here: every feature is one depth, set by the fluidic layer
(resin-printed tool, hot-embossed relief), so a chamber really is a rectangle in one flat
layer. The DXF is that outline: layers CHAMBERS and CHANNELS. Channels are trimmed to
the chamber walls, so the outline has no lines running through the inside of a chamber.
Any channel shallower than the fluidic layer lands on a GROOVES layer instead, and this
script says so loudly: with no photolithography that chip cannot be built as drawn.

Ownership of cad/exports/: the flat folder belongs to ONE chip (DEFAULT_CHIP); every other
chip writes to cad/exports/<chip_id>/. A hidden .chip_id file in each folder records the
owner, and a second chip trying to write into it is refused (a COLLISION), because part
ids repeat across chips and would overwrite each other silently.

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
FOREIGN_FLAT = {"hello_world_plate.stl"}   # written by cad/hello_world_spec.py, not by a chip


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

OWNER_FILE = ".chip_id"


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chip", type=Path, default=DEFAULT_CHIP)
    args = ap.parse_args()

    chip = json.loads(args.chip.read_text())
    if not all(k in chip for k in ("id", "chambers", "ports", "channels", "layers")):
        print(f"{args.chip.name}: skipped, not a chip spec "
              f"(no id/chambers/ports/channels/layers): nothing written")
        return 0
    # Part files are named by bare id, and ids repeat across chips: both of ours have an
    # 'inlet_1'. twin/main.js loads cad/exports/ flat, so the flat namespace can only
    # hold one chip, and it holds the default one. Every other chip gets its own folder.
    is_default = args.chip.resolve() == DEFAULT_CHIP.resolve()
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

    lid = chip["layers"][-1]["thickness_mm"]
    for port in chip["ports"]:
        x, y, z = port["position"]
        dest = out / f"{port['id']}.stl"
        write_stl(dest, port["id"],
                  box_triangles([x, y, z - lid / 2], [PORT_SIZE_MM, PORT_SIZE_MM, lid]))
        written.append(dest)

    anchors = {c["id"]: (c["center"], c["size"]) for c in chip["chambers"]}
    for port in chip["ports"]:
        x, y, z = port["position"]
        anchors[port["id"]] = ([x, y, z], [PORT_SIZE_MM, PORT_SIZE_MM, 0])

    # The spacer is the layer whose thickness sets the chamber height.
    depth = chip["chambers"][0]["size"][2]
    spacer = next((l for l in chip["layers"] if abs(l["thickness_mm"] - depth) < 1e-6), None)

    chamber_lines, channel_outlines, groove_outlines = [], [], []
    for ch in chip["chambers"]:
        chamber_lines += rect_lines(ch["center"][0], ch["center"][1], ch["size"][0], ch["size"][1])
    for ch in chip["channels"]:
        a, b = anchors.get(ch["from"]), anchors.get(ch["to"])
        if a is None or b is None:
            continue
        p0 = clip_to_box(a[0][:2], b[0][:2], a[0], a[1])
        p1 = clip_to_box(b[0][:2], a[0][:2], b[0], b[1])
        outline = channel_lines(p0, p1, ch["width_mm"])
        # A channel shallower than the fluidic layer is a second depth, which one
        # embossing tool cannot make. It goes on its own layer so it cannot be missed.
        if spacer and ch["depth_mm"] < spacer["thickness_mm"] - 1e-6:
            groove_outlines += outline
        else:
            channel_outlines += outline

    dxf = out / f"{chip['id']}.dxf"
    layers = {"CHAMBERS": chamber_lines, "CHANNELS": channel_outlines}
    if groove_outlines:
        layers["GROOVES"] = groove_outlines
    write_dxf(dxf, layers)
    written.append(dxf)
    # Parts left over from an older version of this chip: reported, never deleted.
    wanted = {p.name for p in written}
    stale = sorted(p.name for p in out.glob("*.stl")
                   if p.name not in wanted and p.name not in FOREIGN_FLAT)
    owned_flat = out == EXPORTS
    problems = depth_problems(chip, spacer)

    print(f"chip {chip['id']}: {len(by_id)} chambers, {len(chip['ports'])} ports, "
          f"{len(chip['channels'])} channels")
    for p in written:
        print(f"  wrote {rel}/{p.name}")
    if spacer:
        how = ": resin-printed tool, hot-embossed" if in_envelope(chip) else ""
        print(f"\n{dxf.name}: CHAMBERS and CHANNELS are the outline of the '{spacer['id']}' "
              f"fluidic layer ({spacer['thickness_mm']} mm {spacer['material']}){how}. "
              f"Units mm, chip-spec origin.")
    if groove_outlines:
        shallow = min(c["depth_mm"] for c in chip["channels"]) * 1000
        print(f"  GROOVES layer written: BUILD PROBLEM. Its shallowest feature is {shallow:g} um, "
              f"a second\n  depth. We have no photolithography and one embossing tool makes one "
              f"depth, so this\n  chip cannot be built as drawn. Re-draw it at one depth.")
    for pr in problems:
        print(f"  DEPTH: {pr}")
    if stale:
        note = " (the flat folder is the twin's, check them)" if owned_flat else ""
        print(f"  STALE: {out} holds parts this chip no longer has: {', '.join(stale)}{note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

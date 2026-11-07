"""Split the Flowmatter block sheet into one STL per part, plus a catalogue.

    python chip/components/build_library.py            # default source STL
    python chip/components/build_library.py other.stl

Input:  cad/BP_assayathon_chips.stl, all blocks in one file, exported from Fusion in um.
Output: cad/library/<part>.stl, each in mm with its bounding box starting at (0, 0, 0)
        chip/components/catalogue.json, one entry per part (size, volume, role)

Parts are recognised by where they sit in the source file (their bounding-box corner),
so if the source sheet is re-exported with blocks moved, update PARTS below.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
import stl_io  # noqa: E402

SOURCE = ROOT / "cad" / "BP_assayathon_chips.stl"
LIB_DIR = ROOT / "cad" / "library"
CATALOGUE = HERE / "catalogue.json"
UM_TO_MM = 0.001

# role -> what it's for. chip_type is the chip_spec chamber type used when the part is placed.
ROLES = {
    "chamber":        ("exchange", "Holds cells or capsules while liquids flow past: culture, trap, wash, incubate, image, cure."),
    "split":          ("split",    "One stream in, several out: split-and-pool rounds, feeding parallel chambers."),
    "merge":          ("pool",     "Two streams join: add a reagent to a flowing stream (quench, dilute)."),
    "flow_focusing":  ("exchange", "Cross junction: makes droplets/capsules (centre stream pinched by two side streams)."),
    "mixer":          ("exchange", "Long folded channel: mixes two streams that just joined."),
    "spiral":         ("exchange", "Long spiral: delay line for residence time, or inertial cell/medium separation."),
    "pool":           ("pool",     "Several streams into a ring: gather from parallel branches."),
    "check_valve":    ("exchange", "Tesla valve: lets flow pass one way, resists backflow."),
    "valve":          ("exchange", "On/off valve on a line."),
    "connector":      ("exchange", "Plain channel piece for routing between parts."),
    "port":           ("exchange", "Where tubing or a pipette connects."),
    "unknown":        ("exchange", "Role not yet known."),
}

# name, role, source bbox min corner in mm (x, y), name_confidence
PARTS = [
    ("spiral_channel",        "spiral",        (-19.2, -359.3), "high"),
    ("flow_manifold",         "split",         (-103.6, -248.2), "medium: manifold vs distributor guessed from shape"),
    ("culture_chamber",       "chamber",       (-34.1, -211.5), "high"),
    ("flow_distributor",      "split",         (42.2, -175.5), "medium: manifold vs distributor guessed from shape"),
    ("y_junction",            "merge",         (-88.9, -104.8), "high"),
    ("cross_junction",        "flow_focusing", (1.2, -71.7), "high"),
    ("t_junction",            "merge",         (-39.4, -70.2), "high"),
    ("fork_junction",         "split",         (46.4, -65.7), "high"),
    ("y_junction_curved",     "merge",         (-85.0, -64.2), "high"),
    ("serpentine_channel",    "mixer",         (27.3, -30.0), "high"),
    ("snake_channel",         "mixer",         (-40.9, -26.8), "high"),
    ("ring_junction",         "pool",          (-97.6, -6.2), "high"),
    ("curved_channel",        "connector",     (-67.5, 47.6), "high"),
    ("channel_corner",        "connector",     (5.7, 53.9), "high"),
    ("tesla_valve",           "check_valve",   (54.9, 82.4), "high"),
    ("straight_channel",      "connector",     (-67.2, 121.5), "high"),
    ("port",                  "port",          (-37.4, 122.0), "medium: teardrop shape assumed to be the port"),
    ("mesh_body_a",           "unknown",       (-14.2, 226.0), "low: listed as MeshBody19/20 in Fusion"),
    ("normally_open_valve",   "valve",         (-67.7, 230.9), "low: NO and NC valves look identical from above"),
    ("normally_closed_valve", "valve",         (-35.7, 230.9), "low: NO and NC valves look identical from above"),
    ("mesh_body_b",           "unknown",       (-14.2, 237.3), "low: listed as MeshBody19/20 in Fusion"),
]


def main(argv: list[str]) -> int:
    src = Path(argv[0]) if argv else SOURCE
    pieces = stl_io.shells(stl_io.read_stl(src))
    print(f"{src.name}: {len(pieces)} parts")
    catalogue, unmatched = [], []
    for tris in pieces:
        mn, mx = stl_io.bbox(tris)
        corner = (mn[0] * UM_TO_MM, mn[1] * UM_TO_MM)
        match = next((p for p in PARTS if abs(p[2][0] - corner[0]) < 1 and abs(p[2][1] - corner[1]) < 1), None)
        if match is None:
            unmatched.append(corner)
            continue
        name, role, _, confidence = match
        part = stl_io.transform(tris, UM_TO_MM, tuple(-c * UM_TO_MM for c in mn))
        stl_io.write_stl(LIB_DIR / f"{name}.stl", part, name)
        size = [round((b - a) * UM_TO_MM, 3) for a, b in zip(mn, mx)]
        catalogue.append({
            "part": name,
            "role": role,
            "chip_type": ROLES[role][0],
            "what_for": ROLES[role][1],
            "size_mm": size,
            "volume_ul": round(stl_io.volume(part), 2),
            "stl": f"cad/library/{name}.stl",
            "name_confidence": confidence,
        })
    order = [p[0] for p in PARTS]
    catalogue.sort(key=lambda c: order.index(c["part"]))
    CATALOGUE.write_text(json.dumps({
        "_about": "Pre-made Flowmatter blocks, split from cad/BP_assayathon_chips.stl by "
                  "chip/components/build_library.py. Sizes in mm, volume in uL (1 mm^3 = 1 uL). "
                  "Regenerate, don't edit by hand; change names/roles in build_library.py.",
        "source": str(src.relative_to(ROOT)) if src.is_relative_to(ROOT) else src.name,
        "parts": catalogue,
    }, indent=2) + "\n")
    for c in catalogue:
        print(f"  {c['part']:<22} {c['role']:<14} {c['size_mm'][0]:>6} x {c['size_mm'][1]:>6} x {c['size_mm'][2]} mm  {c['volume_ul']:>8} uL")
    missing = set(order) - {c["part"] for c in catalogue}
    for corner in unmatched:
        print(f"WARNING: part at ({corner[0]:.1f}, {corner[1]:.1f}) mm isn't in PARTS; skipped", file=sys.stderr)
    for name in sorted(missing):
        print(f"WARNING: {name} not found in {src.name}", file=sys.stderr)
    print(f"wrote {len(catalogue)} parts to {LIB_DIR.relative_to(ROOT)}/ and {CATALOGUE.relative_to(ROOT)}")
    return 1 if unmatched or missing else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Design (paper steps grouped into library parts) -> chip spec + placed STLs + Fusion script.

    python chip/components/build_chip.py chip/designs/<design>.json

A design file lists modules in flow order. Each module is one library part
(see chip/components/catalogue.json) and covers one or more protocol steps.
Format: chip/designs/README.md.

Writes:
  chip/<id>.json                 chip spec (contract), validated before writing
  cad/exports/<id>/<module_id>.stl   each placed part, mm, chip origin
  cad/exports/<id>/<id>.stl          the whole chip in one STL
  (a subfolder per chip, as cad/README.md export rule 6 asks)
  cad/fusion/<id>_build.py       Fusion 360 script that imports the placed parts

Layout is deliberately simple: parts in a row left to right in flow order, centred on
one line, 5 mm apart. Parts are NOT joined by drawn channels yet; the chip spec's
`channels` record which part feeds which, and joining them is a manual CAD step.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "operator"))
import schema_io  # noqa: E402
import stl_io  # noqa: E402

CATALOGUE = HERE / "catalogue.json"
MARGIN, GAP, PORT_ZONE, PORT_ROW = 5.0, 5.0, 10.0, 6.0
BASE_MM, LID_MM = 1.0, 1.0
CHANNEL_W, CHANNEL_D = 0.5, 0.5


def load_catalogue() -> dict[str, dict]:
    if not CATALOGUE.exists():
        sys.exit("no catalogue yet: run python chip/components/build_library.py first")
    return {p["part"]: p for p in schema_io.load_json(CATALOGUE)["parts"]}


def check_design(design: dict, parts: dict[str, dict]) -> list[str]:
    errors = []
    ids = [m["id"] for m in design["modules"]]
    for dup in {i for i in ids if ids.count(i) > 1}:
        errors.append(f"module id '{dup}' is used twice")
    for m in design["modules"]:
        if m["part"] not in parts:
            errors.append(f"module '{m['id']}': no part '{m['part']}' in the catalogue ({', '.join(parts)})")
    return errors


def coverage(design: dict) -> list[str]:
    """One line per protocol step saying where it went, so nothing is silently dropped."""
    where: dict[int, list[str]] = {}
    for m in design["modules"]:
        for s in m.get("steps", []):
            where.setdefault(s, []).append(f"{m['id']} ({m['part']})")
    for kind in ("off_chip", "missing_parts"):
        for item in design.get(kind, []):
            label = "OFF CHIP" if kind == "off_chip" else f"NO PART: needs {item.get('need', '?')}"
            for s in item["steps"]:
                where.setdefault(s, []).append(label)
    n = max(where) if where else 0
    lines = []
    for s in range(1, n + 1):
        lines.append(f"  step {s:>2}: {' + '.join(where.get(s, ['NOT ASSIGNED']))}")
    return lines


def layout(design: dict, parts: dict[str, dict]) -> tuple[dict, dict[str, tuple[float, float, float]]]:
    mods = design["modules"]
    fluidic_mm = max(parts[m["part"]]["size_mm"][2] for m in mods)
    max_h = max(parts[m["part"]]["size_mm"][1] for m in mods)
    mid_y = MARGIN + max_h / 2
    top_z = BASE_MM + fluidic_mm + LID_MM

    x = PORT_ZONE
    chambers, channels, ports, offsets = [], [], [], {}
    for m in mods:
        p = parts[m["part"]]
        sx, sy, sz = p["size_mm"]
        ox, oy, oz = x, mid_y - sy / 2, BASE_MM
        offsets[m["id"]] = (ox, oy, oz)
        chambers.append({
            "id": m["id"], "type": p["chip_type"],
            "center": [round(ox + sx / 2, 3), round(oy + sy / 2, 3), round(oz + sz / 2, 3)],
            "size": [sx, sy, sz], "mesh_pore_um": m.get("mesh_pore_um"),
            "volume_ul": p["volume_ul"],
        })
        for i, reagent in enumerate(m.get("inlets", [])):
            pid = f"{reagent}_in"
            ports.append({"id": pid, "type": "inlet",
                          "position": [round(ox + 3 + i * 4, 3), round(MARGIN + max_h + PORT_ROW / 2, 3), top_z]})
            channels.append({"from": pid, "to": m["id"]})
        x += sx + GAP
    width = x - GAP + PORT_ZONE

    ports.insert(0, {"id": "inlet_1", "type": "inlet", "position": [PORT_ZONE / 2, mid_y, top_z]})
    ports.append({"id": "outlet_1", "type": "outlet", "position": [width - PORT_ZONE / 2, mid_y, top_z]})
    chain = ["inlet_1"] + [m["id"] for m in mods] + ["outlet_1"]
    channels = [{"from": a, "to": b} for a, b in zip(chain, chain[1:])] + channels
    channels = [{"id": f"ch_{i}", **c, "width_mm": CHANNEL_W, "depth_mm": CHANNEL_D}
                for i, c in enumerate(channels, 1)]

    spec = {
        "id": design["id"],
        "name": design.get("name", design["id"]),
        "description": design.get("description", "") +
                       f" Built from Flowmatter library parts by chip/components/build_chip.py "
                       f"from chip/designs/{design['id']}.json.",
        "placeholder": design.get("placeholder", True),
        "units": "mm",
        "footprint": {"x": round(width, 3), "y": round(max_h + 2 * MARGIN + PORT_ROW, 3)},
        "layers": [
            {"id": "base", "material": design.get("material", "unknown"), "thickness_mm": BASE_MM},
            {"id": "fluidic", "material": design.get("material", "unknown"), "thickness_mm": fluidic_mm},
            {"id": "lid", "material": design.get("material", "unknown"), "thickness_mm": LID_MM},
        ],
        "chambers": chambers, "channels": channels, "ports": ports,
    }
    return spec, offsets


FUSION_SCRIPT = '''"""Fusion 360 script: import the parts of chip {chip_id}, already placed (mm, chip origin).
Generated by chip/components/build_chip.py. Run it in Fusion: Utilities > Scripts and Add-Ins,
or send it through the Fusion MCP execute_code tool. Each part becomes its own component,
named by its module id, so you can join them with channels by hand afterwards.
"""
import adsk.core, adsk.fusion

import os

# Folder holding this chip's part STLs. Found next to this script; when the code is sent
# through the Fusion MCP execute_code tool (no __file__), set EXPORTS before it.
try:
    EXPORTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "exports", "{chip_id}")
except NameError:
    EXPORTS = globals()["EXPORTS"]
PARTS = {parts}

def run(context=None):
    app = adsk.core.Application.get()
    design = adsk.fusion.Design.cast(app.activeProduct)
    root = design.rootComponent
    for module_id, part in PARTS:
        occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
        comp = occ.component
        comp.name = module_id + " (" + part + ")"
        path = EXPORTS + "/" + module_id + ".stl"
        if design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
            base = comp.features.baseFeatures.add()
            base.startEdit()
            comp.meshBodies.add(path, adsk.fusion.MeshUnits.MillimeterMeshUnit, base)
            base.finishEdit()
        else:
            comp.meshBodies.add(path, adsk.fusion.MeshUnits.MillimeterMeshUnit)
    app.activeViewport.fit()
    return "imported %d parts" % len(PARTS)
'''


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__)
        return 2
    design = schema_io.load_json(argv[0])
    parts = load_catalogue()
    errors = check_design(design, parts)
    if errors:
        for e in errors:
            print(f"ERROR {e}", file=sys.stderr)
        return 1

    spec, offsets = layout(design, parts)
    errs = schema_io.schema_errors("chip_spec", spec)
    if errs:
        for e in errs:
            print(f"ERROR {schema_io.path_str(e) or '(top level)'}: {schema_io.describe(e)}", file=sys.stderr)
        return 1

    exports = ROOT / "cad" / "exports" / spec["id"]
    whole = []
    for m in design["modules"]:
        tris = stl_io.transform(stl_io.read_stl(ROOT / parts[m["part"]]["stl"]), 1.0, offsets[m["id"]])
        stl_io.write_stl(exports / f"{m['id']}.stl", tris, m["id"])
        whole += tris
    stl_io.write_stl(exports / f"{spec['id']}.stl", whole, spec["id"])

    schema_io.write_json(ROOT / "chip" / f"{spec['id']}.json", spec)
    fusion = ROOT / "cad" / "fusion" / f"{spec['id']}_build.py"
    fusion.parent.mkdir(parents=True, exist_ok=True)
    fusion.write_text(FUSION_SCRIPT.format(
        chip_id=spec["id"],
        parts=json.dumps([[m["id"], m["part"]] for m in design["modules"]])))

    fx, fy = spec["footprint"]["x"], spec["footprint"]["y"]
    print(f"chip {spec['id']}: {len(design['modules'])} parts on {fx} x {fy} mm")
    print("  flow: " + " -> ".join(c["from"] for c in spec["channels"] if not c["from"].endswith("_in")) + " -> outlet_1")
    print("protocol steps:")
    print("\n".join(coverage(design)))
    print(f"wrote chip/{spec['id']}.json, cad/exports/{spec['id']}/ (whole chip + one STL per module), "
          f"cad/fusion/{fusion.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

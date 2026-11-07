"""Fusion 360 script: build a real, printable chip from a chip spec (chip/*.json).

Run it in Fusion: Utilities > Add-Ins > Scripts and Add-Ins (Shift+S) > + > pick this
folder > Run. It asks for one or more chip specs (it opens in the repo's chip/ folder; Ctrl+A
picks them all), lays the chips out side by side in a grid, optionally saves STLs to
chip/fusion/stl/ (at the chip's own origin), and builds, per spec, one component named after
the chip id:

  <id>_chip   the printed part: base + fluidic layer as one block, with every chamber,
              channel and port well cut DOWN from its top face. Print it open, seal with
              film or the lid (contracts/blocks.json: "print open and seal with film").
  <id>_lid    the cover, with the tubing holes through it at the port positions.

Geometry comes only from the spec, in mm with the spec's origin (bottom-left corner of the
footprint, z up):
  chambers  boxes at center/size, cut as deep as height_um (or size z); shape "cylinder"
            gives a round chamber
  channels  straight runs of width_mm x depth_mm along `path` when given; otherwise the
            same automatic route the twin and display model use (cad/export_display_model.py)
  ports     round holes of width_um (default 1.6 mm, press-fit 1/16" tubing): through the
            lid, and a well in the chip as deep as its deepest feature

Not built (on purpose; the spec has no shape for them): retention meshes (a bought insert),
valves and `features`. The script lists what it skipped.

The geometry is planned by plan() in plain Python, so it can be checked without Fusion:
    python -I chip/fusion/chip_from_spec/chip_from_spec.py chip/spc_demo_v1.json
"""

import json
import math
import os
import sys

DEFAULT_PORT_MM = 1.6     # spc_demo_v1: press-fit 1.6 mm hole for 1/16 inch tubing
OVERCUT_MM = 0.01         # cutters poke this far above the cut face, so no zero-thickness skins
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
CHIP_DIR = os.path.normpath(os.path.join(HERE, "..", ".."))


# --- geometry plan: plain Python, no Fusion ------------------------------------------------

def _route(a, b, chambers, ports):
    """xy centreline of a channel without a `path`; same rules as cad/export_display_model.py."""
    def pt(end):
        return chambers[end]["center"][:2] if end in chambers else ports[end]["position"][:2]
    (ax, ay), (bx, by) = pt(a), pt(b)
    a_ch, b_ch = a in chambers, b in chambers
    if not a_ch and not b_ch:                      # port to port: straight
        return [(ax, ay), (bx, by)]
    if a_ch and b_ch:                              # chamber to chamber: right angles between facing edges
        sa, sb = chambers[a]["size"], chambers[b]["size"]
        d = 1 if bx >= ax else -1
        p0, p3 = (ax + d * sa[0] / 2, ay), (bx - d * sb[0] / 2, by)
        if abs(ay - by) < 1e-6:
            return [p0, p3]
        mid = (p0[0] + p3[0]) / 2
        return [p0, (mid, ay), (mid, by), p3]
    ch, port = (a, b) if a_ch else (b, a)
    (cx, cy), size = chambers[ch]["center"][:2], chambers[ch]["size"]
    px, py = ports[port]["position"][:2]
    hx, hy = size[0] / 2, size[1] / 2
    if abs(px - cx) <= hx and abs(py - cy) <= hy:
        return []                                  # port sits over the chamber: no channel needed
    if abs(px - cx) / hx >= abs(py - cy) / hy:
        edge = (cx + math.copysign(hx, px - cx), min(max(py, cy - hy), cy + hy))
        path = [edge, (px, edge[1])] if abs(edge[1] - py) > 1e-6 else [edge]
    else:
        edge = (min(max(px, cx - hx), cx + hx), cy + math.copysign(hy, py - cy))
        path = [edge, (edge[0], py)] if abs(edge[0] - px) > 1e-6 else [edge]
    path = path + [(px, py)]
    return path if ch == a else path[::-1]


def plan(spec):
    """Everything to build, in mm. Returns a dict of plain numbers and lists."""
    layers = spec["layers"]
    if len(layers) < 2:
        raise ValueError("need at least a base and a lid layer")
    lid_t = layers[-1]["thickness_mm"]
    chip_t = sum(l["thickness_mm"] for l in layers[:-1])   # base + every fluidic layer, printed as one
    top = chip_t                                            # cuts start at the top face of the chip
    fx, fy = spec["footprint"]["x"], spec["footprint"]["y"]
    chambers = {c["id"]: c for c in spec["chambers"]}
    ports = {p["id"]: p for p in spec["ports"]}
    cuts, skipped = [], []

    def depth_of_chamber(c):
        return c["height_um"] / 1000 if c.get("height_um") else c["size"][2]

    for c in spec["chambers"]:
        cx, cy = c["center"][:2]
        sx, sy = c["size"][:2]
        d = depth_of_chamber(c)
        if c.get("shape") == "cylinder":
            cuts.append({"kind": "cyl", "id": c["id"], "x": cx, "y": cy, "r": sx / 2, "depth": d})
        else:
            cuts.append({"kind": "box", "id": c["id"], "x": cx, "y": cy, "len": sx, "wid": sy,
                         "dir": (1.0, 0.0), "depth": d})
        if c.get("retention") or c.get("mesh_pore_um"):
            skipped.append(f"{c['id']}: retention/mesh not modelled (bought insert)")

    for ch in spec["channels"]:
        path = [tuple(p[:2]) for p in ch["path"]] if ch.get("path") else _route(ch["from"], ch["to"], chambers, ports)
        w, d = ch["width_mm"], ch["depth_mm"]
        for (x0, y0), (x1, y1) in zip(path[:-1], path[1:]):
            L = math.hypot(x1 - x0, y1 - y0)
            if L < 1e-9:
                continue
            # extend each run by half a width at both ends so corners and joints close up
            cuts.append({"kind": "box", "id": ch["id"], "x": (x0 + x1) / 2, "y": (y0 + y1) / 2,
                         "len": L + w, "wid": w, "dir": ((x1 - x0) / L, (y1 - y0) / L), "depth": d})

    deepest = max([c["depth"] for c in cuts] or [top - layers[0]["thickness_mm"]])
    holes = []
    for p in spec["ports"]:
        r = (p.get("width_um") or DEFAULT_PORT_MM * 1000) / 2000
        x, y = p["position"][:2]
        holes.append({"id": p["id"], "x": x, "y": y, "r": r})
        cuts.append({"kind": "cyl", "id": p["id"], "x": x, "y": y, "r": r, "depth": deepest})

    for key in ("valves", "features"):
        for v in spec.get(key, []):
            skipped.append(f"{v['id']}: {key[:-1]} not modelled")

    problems = []
    for c in cuts:
        if c["depth"] >= top:
            problems.append(f"{c['id']}: {c['depth']} mm deep cuts through the {top} mm chip")
        reach = c["r"] if c["kind"] == "cyl" else (abs(c["dir"][0]) * c["len"] + abs(c["dir"][1]) * c["wid"]) / 2
        reach_y = c["r"] if c["kind"] == "cyl" else (abs(c["dir"][1]) * c["len"] + abs(c["dir"][0]) * c["wid"]) / 2
        if c["x"] - reach < -1e-6 or c["x"] + reach > fx + 1e-6 or c["y"] - reach_y < -1e-6 or c["y"] + reach_y > fy + 1e-6:
            problems.append(f"{c['id']}: runs off the {fx} x {fy} mm footprint")
    return {"id": spec["id"], "fx": fx, "fy": fy, "chip_t": chip_t, "lid_t": lid_t, "top": top,
            "cuts": cuts, "holes": holes, "skipped": skipped, "problems": problems}


# --- Fusion -----------------------------------------------------------------------------------

def _build(p, design, stl_dir=None, offset_mm=(0.0, 0.0)):
    """Build one chip as its own component. Exports STLs (chip origin) before moving it to offset_mm."""
    import adsk.core
    import adsk.fusion
    tbm = adsk.fusion.TemporaryBRepManager.get()
    cm = 0.1                                        # the Fusion API works in cm
    P = lambda x, y, z: adsk.core.Point3D.create(x * cm, y * cm, z * cm)
    V = lambda x, y: adsk.core.Vector3D.create(x, y, 0)

    def box(x, y, z0, z1, length, width, d=(1.0, 0.0)):
        obb = adsk.core.OrientedBoundingBox3D.create(P(x, y, (z0 + z1) / 2), V(d[0], d[1]), V(-d[1], d[0]),
                                                     length * cm, width * cm, (z1 - z0) * cm)
        return tbm.createBox(obb)

    def cyl(x, y, z0, z1, r):
        return tbm.createCylinderOrCone(P(x, y, z0), r * cm, P(x, y, z1), r * cm)

    diff = adsk.fusion.BooleanTypes.DifferenceBooleanType
    union = adsk.fusion.BooleanTypes.UnionBooleanType
    top = p["top"]

    chip = box(p["fx"] / 2, p["fy"] / 2, 0, p["chip_t"], p["fx"], p["fy"])
    tool = None
    for c in p["cuts"]:
        z0, z1 = top - c["depth"], top + OVERCUT_MM
        b = cyl(c["x"], c["y"], z0, z1, c["r"]) if c["kind"] == "cyl" else \
            box(c["x"], c["y"], z0, z1, c["len"], c["wid"], c["dir"])
        if tool is None:
            tool = b
        else:
            tbm.booleanOperation(tool, b, union)
    if tool is not None:
        tbm.booleanOperation(chip, tool, diff)

    lid = box(p["fx"] / 2, p["fy"] / 2, top, top + p["lid_t"], p["fx"], p["fy"])
    for h in p["holes"]:
        tbm.booleanOperation(lid, cyl(h["x"], h["y"], top - OVERCUT_MM, top + p["lid_t"] + OVERCUT_MM, h["r"]), diff)

    occ = design.rootComponent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = p["id"]
    if design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
        base = comp.features.baseFeatures.add()
        base.startEdit()
        bodies = [comp.bRepBodies.add(chip, base), comp.bRepBodies.add(lid, base)]
        base.finishEdit()
        bodies = [comp.bRepBodies.item(i) for i in range(comp.bRepBodies.count)]
    else:
        bodies = [comp.bRepBodies.add(chip), comp.bRepBodies.add(lid)]
    for body, name in zip(bodies, (p["id"] + "_chip", p["id"] + "_lid")):
        body.name = name

    written = []
    if stl_dir:
        os.makedirs(stl_dir, exist_ok=True)
        em = design.exportManager
        for body in bodies:
            path = os.path.join(stl_dir, body.name + ".stl")
            opts = em.createSTLExportOptions(body, path)
            opts.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh
            em.execute(opts)
            written.append(path)

    if offset_mm != (0.0, 0.0):                    # spread the chips out so they don't overlap
        m = adsk.core.Matrix3D.create()
        m.translation = adsk.core.Vector3D.create(offset_mm[0] * cm, offset_mm[1] * cm, 0)
        try:
            occ.transform2 = m
        except AttributeError:
            occ.transform = m
        if design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
            try:
                design.snapshots.add()             # capture the position in the timeline
            except Exception:
                pass
    return written


GRID_COLS = 4
GRID_GAP_MM = 10.0
STL_DIR = os.path.join(HERE, "..", "stl")       # chip/fusion/stl/<id>_chip.stl, <id>_lid.stl


def run(context=None):
    import adsk.core
    import adsk.fusion
    app = adsk.core.Application.get()
    ui = app.userInterface
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            ui.messageBox("Open a design first (File > New Design, Hybrid).")
            return
        dlg = ui.createFileDialog()
        dlg.title = "Pick one or more chip specs (Ctrl+A in chip/ for all)"
        dlg.filter = "Chip spec (*.json)"
        dlg.isMultiSelectEnabled = True
        dlg.initialDirectory = CHIP_DIR
        if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
            return
        files = list(dlg.filenames)
        ans = ui.messageBox(f"Also save STLs (chip + lid per spec) to\n{os.path.normpath(STL_DIR)} ?",
                            "chip_from_spec", adsk.core.MessageBoxButtonTypes.YesNoButtonType)
        stl_dir = os.path.normpath(STL_DIR) if ans == adsk.core.DialogResults.DialogYes else None

        plans, report = [], []
        for fn in files:
            try:
                with open(fn, encoding="utf-8") as f:
                    spec = json.load(f)
                if "chambers" not in spec or "footprint" not in spec:
                    report.append(f"SKIP {os.path.basename(fn)}: not a chip spec")
                    continue
                plans.append(plan(spec))
            except Exception as e:
                report.append(f"SKIP {os.path.basename(fn)}: {e}")

        cell_x = max([p["fx"] for p in plans] or [0]) + GRID_GAP_MM
        cell_y = max([p["fy"] for p in plans] or [0]) + GRID_GAP_MM
        n_stl = 0
        for i, p in enumerate(plans):
            off = ((i % GRID_COLS) * cell_x, -(i // GRID_COLS) * cell_y)
            try:
                n_stl += len(_build(p, design, stl_dir, off))
                line = f"OK   {p['id']}: {p['fx']} x {p['fy']} mm, {len(p['cuts'])} cuts, {len(p['holes'])} ports"
                if p["problems"]:
                    line += f", {len(p['problems'])} to CHECK (e.g. {p['problems'][0]})"
                if p["skipped"]:
                    line += f", {len(p['skipped'])} not modelled"
                report.append(line)
            except Exception as e:
                report.append(f"FAIL {p['id']}: {e}")
            adsk.doEvents()
        app.activeViewport.fit()
        if stl_dir:
            report.append(f"\n{n_stl} STL files in {stl_dir}")
        ui.messageBox("\n".join(report) or "nothing built")
    except Exception:
        import traceback
        ui.messageBox("chip_from_spec failed:\n" + traceback.format_exc())


if __name__ == "__main__" and "adsk" not in sys.modules:
    for path in sys.argv[1:]:
        with open(path, encoding="utf-8") as f:
            p = plan(json.load(f))
        print(f"{p['id']}: {p['fx']} x {p['fy']} mm, chip {p['chip_t']} + lid {p['lid_t']} mm, "
              f"{len(p['cuts'])} cuts, {len(p['holes'])} ports, "
              f"{len(p['problems'])} problems, {len(p['skipped'])} skipped")
        for line in p["problems"]:
            print("  CHECK", line)

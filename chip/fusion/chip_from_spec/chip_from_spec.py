"""Fusion 360 script: build the positive embossing mould for chip specs (chip/*.json).

Our process (cad/fabrication.json): resin 3D print a TOOL, hot-emboss it into a COC/COP
sheet, then seal the open channels with film or a lid with drilled ports. This script
builds that tool, nothing else.

Run it in Fusion: Utilities > Add-Ins > Scripts and Add-Ins (Shift+S) > + > pick this
folder > Run. It first offers to build every module chip listed in
protocols/pipeline_mvp.json, in one row in pipeline order (components 01_..., 02_...);
answer No to pick spec files yourself instead (it opens in the repo's chip/ folder; Ctrl+A
picks them all). It optionally saves one STL per mould to chip/fusion/stl/<id>_mould.stl
(at the mould's own origin, mm), ready for the printer.

Per spec, one component with one body, <id>_mould:
  a flat MOULD_BASE_MM plate, the spec's footprint (30 x 20 mm for every module), with
  every chamber, channel and port pad RAISED by its depth: a positive mould, like Ayrton's
  cad/exports/hello_world_plate.stl. Mirrored in x by default (MIRROR_MOULD): an embossed
  copy is the mirror image of its tool, so the mirrored tool gives a chip that matches the
  spec seen from the port side.

Geometry comes only from the spec, in mm with the spec's origin (bottom-left corner of the
footprint, z up), before mirroring:
  chambers  boxes at center/size, as tall as height_um (or size z); shape "cylinder"
            gives a round one
  channels  straight runs of width_mm x depth_mm along `path` when given; otherwise the
            same automatic route the twin and display model use (cad/export_display_model.py)
  ports     round pads of width_um (default 1.6 mm, press-fit 1/16" tubing), as tall as
            the tallest feature: they emboss the well under each drilled tubing hole

Not built (on purpose; the spec has no shape for them): retention meshes (a bought insert),
valves and `features`. Parts the spec only gives as a box (e.g. a serpentine block) come
out as a solid block. No draft angles or fillets. The script lists what it skipped.

The geometry is planned by plan() in plain Python, so it can be checked without Fusion:
    python -I chip/fusion/chip_from_spec/chip_from_spec.py --pipeline
    python -I chip/fusion/chip_from_spec/chip_from_spec.py chip/spc_demo_v1.json
"""

import json
import math
import os
import sys

DEFAULT_PORT_MM = 1.6     # spc_demo_v1: press-fit 1.6 mm hole for 1/16 inch tubing
FUSE_MM = 0.01            # raised features start this far inside the plate, so they fuse cleanly
MOULD_BASE_MM = 2.0       # mould plate thickness: stiff enough for the press (Ayrton's MVP used 0.5)
MIRROR_MOULD = True       # embossing mirrors the tool, so mirror the tool to get the spec's layout
HERE = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else os.getcwd()
CHIP_DIR = os.path.normpath(os.path.join(HERE, "..", ".."))
REPO_DIR = os.path.normpath(os.path.join(CHIP_DIR, ".."))
PIPELINE = os.path.join(REPO_DIR, "protocols", "pipeline_mvp.json")
STL_DIR = os.path.join(HERE, "..", "stl")       # chip/fusion/stl/<id>_mould.stl (git-ignored)
GRID_COLS = 4
GRID_GAP_MM = 10.0


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


def plan(spec, mirror=MIRROR_MOULD):
    """The mould for one spec, in mm: the raised features (mirrored when `mirror`), what was
    skipped, and anything to check. Plain numbers and lists only."""
    fx, fy = spec["footprint"]["x"], spec["footprint"]["y"]
    chambers = {c["id"]: c for c in spec["chambers"]}
    ports = {p["id"]: p for p in spec["ports"]}
    feats, skipped = [], []

    for c in spec["chambers"]:
        cx, cy = c["center"][:2]
        sx, sy = c["size"][:2]
        h = c["height_um"] / 1000 if c.get("height_um") else c["size"][2]
        if c.get("shape") == "cylinder":
            feats.append({"kind": "cyl", "id": c["id"], "x": cx, "y": cy, "r": sx / 2, "height": h})
        else:
            feats.append({"kind": "box", "id": c["id"], "x": cx, "y": cy, "len": sx, "wid": sy,
                          "dir": (1.0, 0.0), "height": h})
        if c.get("retention") or c.get("mesh_pore_um"):
            skipped.append(f"{c['id']}: retention/mesh not modelled (bought insert)")

    for ch in spec["channels"]:
        path = [tuple(p[:2]) for p in ch["path"]] if ch.get("path") else _route(ch["from"], ch["to"], chambers, ports)
        w, h = ch["width_mm"], ch["depth_mm"]
        for (x0, y0), (x1, y1) in zip(path[:-1], path[1:]):
            L = math.hypot(x1 - x0, y1 - y0)
            if L < 1e-9:
                continue
            # extend each run by half a width at both ends so corners and joints close up
            feats.append({"kind": "box", "id": ch["id"], "x": (x0 + x1) / 2, "y": (y0 + y1) / 2,
                          "len": L + w, "wid": w, "dir": ((x1 - x0) / L, (y1 - y0) / L), "height": h})

    tallest = max([f["height"] for f in feats] or [0.3])
    for p in spec["ports"]:
        r = (p.get("width_um") or DEFAULT_PORT_MM * 1000) / 2000
        x, y = p["position"][:2]
        feats.append({"kind": "cyl", "id": p["id"], "x": x, "y": y, "r": r, "height": tallest})

    for key in ("valves", "features"):
        for v in spec.get(key, []):
            skipped.append(f"{v['id']}: {key[:-1]} not modelled")

    problems = []
    for f in feats:
        rx = f["r"] if f["kind"] == "cyl" else (abs(f["dir"][0]) * f["len"] + abs(f["dir"][1]) * f["wid"]) / 2
        ry = f["r"] if f["kind"] == "cyl" else (abs(f["dir"][1]) * f["len"] + abs(f["dir"][0]) * f["wid"]) / 2
        if f["x"] - rx < -1e-6 or f["x"] + rx > fx + 1e-6 or f["y"] - ry < -1e-6 or f["y"] + ry > fy + 1e-6:
            problems.append(f"{f['id']}: runs off the {fx} x {fy} mm footprint")

    if mirror:                                      # x -> fx - x; box directions flip in x
        for f in feats:
            f["x"] = fx - f["x"]
            if f["kind"] == "box":
                f["dir"] = (-f["dir"][0], f["dir"][1])
    return {"id": spec["id"], "fx": fx, "fy": fy, "mirrored": mirror, "features": feats,
            "ports": len(spec["ports"]), "tallest": tallest, "skipped": skipped, "problems": problems}


def pipeline_specs():
    """The module chips of the current pipeline, in pipeline order: [(module name, spec path)]."""
    with open(PIPELINE, encoding="utf-8") as f:
        pipe = json.load(f)
    return [(m.get("name", m["chip"]), os.path.join(REPO_DIR, os.path.normpath(m["chip"])))
            for m in pipe.get("modules", []) if m.get("chip")]


# --- Fusion -----------------------------------------------------------------------------------

def _build(p, design, stl_dir=None, offset_mm=(0.0, 0.0), name=None):
    """Build one mould as its own component. Exports the STL (mould origin) before moving it
    to offset_mm. Returns the STL paths written."""
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

    union = adsk.fusion.BooleanTypes.UnionBooleanType
    mb = MOULD_BASE_MM
    plate = box(p["fx"] / 2, p["fy"] / 2, 0, mb, p["fx"], p["fy"])
    for f in p["features"]:                         # features stand UP from the plate by their height
        z0, z1 = mb - FUSE_MM, mb + f["height"]
        b = cyl(f["x"], f["y"], z0, z1, f["r"]) if f["kind"] == "cyl" else \
            box(f["x"], f["y"], z0, z1, f["len"], f["wid"], f["dir"])
        tbm.booleanOperation(plate, b, union)

    occ = design.rootComponent.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = name or p["id"]
    if design.designType == adsk.fusion.DesignTypes.ParametricDesignType:
        base = comp.features.baseFeatures.add()
        base.startEdit()
        comp.bRepBodies.add(plate, base)
        base.finishEdit()
        body = comp.bRepBodies.item(0)
    else:
        body = comp.bRepBodies.add(plate)
    body.name = p["id"] + "_mould"

    written = []
    if stl_dir:
        os.makedirs(stl_dir, exist_ok=True)
        em = design.exportManager
        path = os.path.join(stl_dir, body.name + ".stl")
        opts = em.createSTLExportOptions(body, path)
        opts.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh
        em.execute(opts)
        written.append(path)

    if offset_mm != (0.0, 0.0):                    # spread the moulds out so they don't overlap
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


def run(context=None):
    import adsk.core
    import adsk.fusion
    app = adsk.core.Application.get()
    ui = app.userInterface
    title = "Positive mould from spec"
    try:
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            ui.messageBox("Open a design first (File > New Design, Hybrid).")
            return
        mods = pipeline_specs() if os.path.exists(PIPELINE) else []
        ans = adsk.core.DialogResults.DialogNo
        if mods:
            ans = ui.messageBox(
                f"Build the embossing moulds for all {len(mods)} modules of protocols/pipeline_mvp.json, "
                "in one row in pipeline order?\n\n"
                + "\n".join(f"  {i + 1}. {n}" for i, (n, _) in enumerate(mods))
                + "\n\nYes = all modules     No = pick spec files myself",
                title, adsk.core.MessageBoxButtonTypes.YesNoCancelButtonType)
        if ans == adsk.core.DialogResults.DialogCancel:
            return
        all_modules = ans == adsk.core.DialogResults.DialogYes
        if all_modules:
            files = [path for _, path in mods]
        else:
            dlg = ui.createFileDialog()
            dlg.title = "Pick one or more chip specs (Ctrl+A in chip/ for all)"
            dlg.filter = "Chip spec (*.json)"
            dlg.isMultiSelectEnabled = True
            dlg.initialDirectory = CHIP_DIR
            if dlg.showOpen() != adsk.core.DialogResults.DialogOK:
                return
            files = list(dlg.filenames)
        ans = ui.messageBox(f"Also save one STL per mould to\n{os.path.normpath(STL_DIR)} ?",
                            title, adsk.core.MessageBoxButtonTypes.YesNoButtonType)
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
        cols = max(len(plans), 1) if all_modules else GRID_COLS   # modules: one row, left to right
        n_stl = 0
        for i, p in enumerate(plans):
            off = ((i % cols) * cell_x, -(i // cols) * cell_y)
            name = f"{i + 1:02d}_{p['id']}" if all_modules else None
            try:
                n_stl += len(_build(p, design, stl_dir, off, name))
                line = (f"OK   {p['id']}_mould: {p['fx']} x {p['fy']} mm, {MOULD_BASE_MM:g} mm plate, "
                        f"features up to {p['tallest']:g} mm, {p['ports']} port pads")
                if p["problems"]:
                    line += f", {len(p['problems'])} to CHECK (e.g. {p['problems'][0]})"
                if p["skipped"]:
                    line += f", {len(p['skipped'])} not modelled"
                report.append(line)
            except Exception as e:
                report.append(f"FAIL {p['id']}: {e}")
            adsk.doEvents()
        app.activeViewport.fit()
        report.append("\nMirrored in x, so the embossed chip matches the spec." if MIRROR_MOULD
                      else "\nNOT mirrored: the embossed chip will be the mirror image of the spec.")
        if stl_dir:
            report.append(f"{n_stl} STL files in {stl_dir}")
        ui.messageBox("\n".join(report) or "nothing built", title)
    except Exception:
        import traceback
        ui.messageBox("chip_from_spec failed:\n" + traceback.format_exc())


if __name__ == "__main__" and "adsk" not in sys.modules:
    args = sys.argv[1:]
    if args == ["--pipeline"]:                     # what "Yes = all modules" will build
        args = [path for _, path in pipeline_specs()]
    for path in args:
        with open(path, encoding="utf-8") as f:
            p = plan(json.load(f))
        print(f"{p['id']}_mould: {p['fx']} x {p['fy']} mm, {MOULD_BASE_MM:g} mm plate, "
              f"{len(p['features'])} raised features up to {p['tallest']:g} mm, {p['ports']} port pads, "
              f"{'mirrored' if p['mirrored'] else 'not mirrored'}, "
              f"{len(p['problems'])} problems, {len(p['skipped'])} skipped")
        for line in p["problems"]:
            print("  CHECK", line)

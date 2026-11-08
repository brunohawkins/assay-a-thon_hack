"""Constraint checks for one chip spec (autochip stage 4).

check_chip(spec, constraints) -> [ {"severity": "error" | "warning", "rule": str, "message": str} ]

Errors go back to the partition agent (autochip/cli.py re-splits the chips), so every message says
what is wrong in plain English and what to change ("chip has 8 ports, max 6: move ...").
Rules: footprint vs envelope; port count, pitch, diameter and edge margin; channel width vs the
process minimum (open vs enclosed channel, by sealing) and aspect ratio; depth max / min / allowed /
single depth; walls between neighbouring features (bounding boxes); fluidic layer count vs
multilayer; channel crossings (tools/check_chip_spec.py crossings()) vs channel_crossings_allowed;
on-chip valves; mesh / pillar / filter gaps vs min_feature.gap_mm; operating temperature and the
chip material's max temperature; plus the errors of tools/check_chip_spec.py (capsule-scale rules).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

from autochip import constraints as C

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "operator", ROOT / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import check_chip_spec as _ccs  # noqa: E402
import schema_io as _schema_io  # noqa: E402

MAX_WALL_MESSAGES = 8
NATIVE_FEATURE_MM = 0.5     # assumed narrowest internal channel of a library (Flowmatter) block at native size
HEATERS = {"heater", "incubator_37c", "peltier_stage", "thermocycler"}
COOLERS = {"cold_plate", "peltier_stage"}
CYCLERS = {"thermocycler", "peltier_stage"}
EPS = 1e-6


def crossings(spec: dict) -> list[tuple[str, str, tuple[float, float]]]:
    """Channels (with explicit paths) whose centrelines cross: tools/check_chip_spec.py crossings()."""
    return _ccs.crossings(spec)


def min_channel_mm(c: dict) -> float:
    """Narrowest channel the process makes. Printed-closed processes are limited by the enclosed-channel
    figure; print-open-and-seal (film, lid, bond) by the open-channel figure."""
    open_w = C.get(c, "fabrication.min_feature.open_channel_width_mm")
    closed_w = C.get(c, "fabrication.min_feature.enclosed_channel_width_mm")
    printed_closed = C.get(c, "fabrication.sealing") == "printed_closed" or C.get(c, "fabrication.method") == "glass_laser_3d_etch"
    if printed_closed:
        return closed_w or open_w or 0.05
    return open_w or closed_w or 0.05


def _layer_of(item: dict) -> str:
    note = str(item.get("note", ""))
    if note.startswith("layer "):
        return note.split()[1].rstrip(";,")
    return "fluidic"


def _fluidic_layers(spec: dict) -> list[str]:
    return [lay["id"] for lay in spec.get("layers", []) if lay["id"] == "fluidic" or lay["id"].startswith("fluidic_")]


def _rect_gap(a, b) -> float:
    """Clearance between two boxes (x0, y0, x1, y1); negative-ish (0) when they touch or overlap."""
    dx = max(0.0, b[0] - a[2], a[0] - b[2])
    dy = max(0.0, b[1] - a[3], a[1] - b[3])
    return math.hypot(dx, dy) if dx > 0 and dy > 0 else max(dx, dy)


def _features(spec: dict) -> list[dict]:
    """Bounding boxes: chambers, channel segments (at their width), port holes."""
    out = []
    for c in spec.get("chambers", []):
        (cx, cy, _), (sx, sy, _) = c["center"], c["size"]
        out.append({"id": c["id"], "kind": "chamber", "box": (cx - sx / 2, cy - sy / 2, cx + sx / 2, cy + sy / 2),
                    "layer": "fluidic", "links": {c["id"]}})
    for ch in spec.get("channels", []):
        pts = ch.get("path")
        if not pts:
            continue
        h = ch["width_mm"] / 2
        for a, b in zip(pts[:-1], pts[1:]):
            out.append({"id": ch["id"], "kind": "channel", "layer": _layer_of(ch), "links": {ch["id"], ch["from"], ch["to"]},
                        "box": (min(a[0], b[0]) - h, min(a[1], b[1]) - h, max(a[0], b[0]) + h, max(a[1], b[1]) + h)})
    for p in spec.get("ports", []):
        r = p.get("width_um", 1000) / 2000
        x, y = p["position"][:2]
        out.append({"id": p["id"], "kind": "port", "box": (x - r, y - r, x + r, y + r), "layer": "any", "links": {p["id"]}})
    return out


def check_chip(spec: dict, constraints: dict) -> list[dict]:
    c = constraints
    out: list[dict] = []

    def add(sev, rule, msg):
        out.append({"severity": sev, "rule": rule, "message": msg})

    def g(path, default=None):
        return C.get(c, path, default)
    cid = spec.get("id", "?")

    # --- contract ---------------------------------------------------------------------------
    for e in _schema_io.schema_errors("chip_spec", spec)[:5]:
        add("error", "schema", f"chip spec is not valid against the contract ({_schema_io.path_str(e) or 'top'}: "
                               f"{_schema_io.describe(e)}): this is a layout bug, report it.")

    # --- footprint --------------------------------------------------------------------------
    fx, fy = spec["footprint"]["x"], spec["footprint"]["y"]
    mx, my = C.max_chip_mm(c)
    if mx and my:
        fits = (fx <= mx + EPS and fy <= my + EPS) or (fx <= my + EPS and fy <= mx + EPS)
        n_mod = len(spec.get("chambers", []))
        if not fits and n_mod == 1:
            only = spec["chambers"][0]
            add("error", "module_too_large",
                f"chip is {fx:.1f} x {fy:.1f} mm, larger than the {mx:g} x {my:g} mm maximum, and its only module '{only['id']}' "
                f"({only['volume_ul']:g} uL) is what makes it so: splitting the chip won't help. Shrink that module (smaller "
                f"volume or shorter channel), use a different part, or raise envelope.max_chip_mm.")
        elif not fits:
            add("error", "envelope.max_chip_mm",
                f"chip is {fx:.1f} x {fy:.1f} mm, larger than the {mx:g} x {my:g} mm maximum: split its {n_mod} modules over "
                f"two chips (about {max(1, n_mod // 2)} each), or use smaller parts.")
    elif mx and fx > mx + EPS:
        add("error", "envelope.max_chip_mm", f"chip is {fx:.1f} mm wide, more than the {mx:g} mm maximum: split it over two chips.")
    pa = g("envelope.printable_area_mm")
    if pa and pa.get("x") and pa.get("y"):
        if len(spec.get("chambers", [])) == 1 and any(v["rule"] == "module_too_large" for v in out):
            pass                                       # one module: already reported, splitting can't help
        elif not ((fx <= pa["x"] + EPS and fy <= pa["y"] + EPS) or (fx <= pa["y"] + EPS and fy <= pa["x"] + EPS)):
            add("error", "envelope.printable_area_mm",
                f"chip is {fx:.1f} x {fy:.1f} mm but the machine's build area is {pa['x']:g} x {pa['y']:g} mm: split it over "
                f"two chips.")

    # --- ports ------------------------------------------------------------------------------
    ports = spec.get("ports", [])
    maxp = g("ports.max_per_chip")
    if maxp and len(ports) > maxp:
        extra = len(ports) - maxp
        reagents = [p["id"] for p in ports if p["type"] == "inlet" and p["id"] != "inlet_1"]
        outs = [p["id"] for p in ports if p["type"] == "outlet" and p["id"] != "outlet_1"]
        ctrl = [p["id"] for p in ports if p["type"] == "control"]
        movable = reagents + outs + ctrl
        add("error", "ports.max_per_chip",
            f"chip has {len(ports)} ports, max {maxp}: move {extra} of its extra lines "
            f"({', '.join(movable) or 'none'}) together with the modules they feed to a second chip, or share one inlet "
            f"between reagents that are used one after another (switch them off-chip) or merge waste outlets.")
    pitch = g("ports.min_pitch_mm")
    if pitch:
        for i, a in enumerate(ports):
            for b in ports[i + 1:]:
                d = math.dist(a["position"][:2], b["position"][:2])
                if d < pitch - 1e-3:
                    add("error", "ports.min_pitch_mm",
                        f"ports '{a['id']}' and '{b['id']}' are {d:.2f} mm apart, closer than the {pitch:g} mm minimum pitch: "
                        f"the chip needs more room, so put fewer modules or reagent lines on it.")
    dmin = g("ports.min_diameter_mm")
    margin = g("envelope.edge_margin_mm", 0) or 0
    for p in ports:
        dia = p.get("width_um", 0) / 1000
        if dmin and dia and dia < dmin - EPS:
            add("error", "ports.min_diameter_mm", f"port '{p['id']}' is {dia:g} mm across, below the {dmin:g} mm minimum: "
                                                  f"widen it to {dmin:g} mm.")
        x, y = p["position"][:2]
        r = dia / 2
        clear = min(x - r, y - r, fx - x - r, fy - y - r)
        if clear < -EPS:
            add("error", "envelope.edge", f"port '{p['id']}' at ({x:g}, {y:g}) is outside the chip: move it inside.")
        elif clear < margin - 1e-3:
            add("error", "envelope.edge_margin_mm",
                f"port '{p['id']}' is {clear:.2f} mm from the chip edge, less than the {margin:g} mm edge margin: move it inward.")
    placement = g("ports.placement")
    if placement == "edge":
        add("warning", "ports.placement", "ports are drawn as holes in the top face; this process wants edge ports, so turn "
                                          "them into edge connectors in CAD.")

    # --- channels: width, aspect ratio --------------------------------------------------------
    wmin = min_channel_mm(c)
    sealed = "printed closed" if (g("fabrication.sealing") == "printed_closed" or g("fabrication.method") == "glass_laser_3d_etch") \
        else "printed open and sealed"
    ar = g("fabrication.min_feature.max_aspect_ratio")
    for ch in spec.get("channels", []):
        if ch.get("fabricable") is False:
            continue
        if ch["width_mm"] < wmin - EPS:
            add("error", "min_feature.channel_width",
                f"channel '{ch['id']}' ({ch['from']} -> {ch['to']}) is {ch['width_mm']:g} mm wide, below the {wmin:g} mm "
                f"smallest channel for this process ({sealed}): widen it to {wmin:g} mm.")
        if ar and ch["depth_mm"] / ch["width_mm"] > ar + EPS:
            add("error", "min_feature.max_aspect_ratio",
                f"channel '{ch['id']}' is {ch['depth_mm']:g} mm deep and {ch['width_mm']:g} mm wide (aspect {ch['depth_mm'] / ch['width_mm']:.1f}), "
                f"above the {ar:g} limit: widen it to at least {ch['depth_mm'] / ar:.3g} mm.")

    # --- depth ------------------------------------------------------------------------------
    dmax, dmin_d = g("fabrication.depth.max_mm"), g("fabrication.depth.min_mm")
    allowed = g("fabrication.depth.allowed_mm") or []
    items = [("chamber", x["id"], x["size"][2], "fluidic") for x in spec.get("chambers", [])] + \
            [("channel", x["id"], x["depth_mm"], _layer_of(x)) for x in spec.get("channels", []) if x.get("fabricable", True)]
    for kind, iid, d, _ in items:
        if dmax and d > dmax + EPS:
            add("error", "depth.max_mm", f"{kind} '{iid}' is {d:g} mm deep, deeper than the {dmax:g} mm this process allows: "
                                         f"make it {dmax:g} mm deep (a chamber gets bigger in x/y to keep its volume).")
        if dmin_d and d < dmin_d - EPS:
            add("error", "depth.min_mm", f"{kind} '{iid}' is {d:g} mm deep, shallower than the {dmin_d:g} mm minimum.")
        if allowed and not any(abs(d - a) < 1e-4 for a in allowed):
            add("error", "depth.allowed_mm", f"{kind} '{iid}' is {d:g} mm deep, but this process only makes depths of "
                                             f"{', '.join(f'{a:g}' for a in allowed)} mm: pick one of those.")
    if g("fabrication.depth.single_depth"):
        by_layer: dict[str, dict[float, list[str]]] = {}
        for kind, iid, d, layer in items:
            by_layer.setdefault(layer, {}).setdefault(round(d, 4), []).append(iid)
        for layer, depths in by_layer.items():
            if len(depths) > 1:
                desc = "; ".join(f"{d:g} mm: {', '.join(ids[:4])}{'...' if len(ids) > 4 else ''}" for d, ids in sorted(depths.items()))
                add("error", "depth.single_depth", f"layer '{layer}' has features at {len(depths)} depths ({desc}), but this "
                                                   f"process makes one depth per layer: give them all the same depth.")

    # --- fluidic layers ---------------------------------------------------------------------
    fl = _fluidic_layers(spec)
    used = {_layer_of(ch) for ch in spec.get("channels", [])} | {"fluidic"}
    n_fl = max(len(fl), len(used))
    if n_fl > 1 and not C.multilayer(c):
        add("error", "multilayer.allowed", f"chip uses {n_fl} fluidic layers, but this process makes one: make the layout "
                                           f"planar (reorder modules, or move crossing lines to another chip).")
    mfl = g("fabrication.multilayer.max_fluidic_layers")
    if mfl and n_fl > mfl:
        add("error", "multilayer.max_fluidic_layers", f"chip uses {n_fl} fluidic layers, more than the {mfl} allowed: "
                                                      f"simplify the routing or split the chip.")

    # --- crossings --------------------------------------------------------------------------
    chans = {ch["id"]: ch for ch in spec.get("channels", [])}
    for a, b, (x, y) in crossings(spec):
        la, lb = _layer_of(chans[a]), _layer_of(chans[b])
        if la == lb:
            if C.crossings_allowed(c) and C.multilayer(c):
                add("error", "channel_crossings", f"channels '{a}' and '{b}' cross at ({x:g}, {y:g}) in the same layer, so they "
                                                  f"would join: put one of them on a second fluidic layer.")
            else:
                add("error", "channel_crossings", f"channels '{a}' and '{b}' cross at ({x:g}, {y:g}), but this process is planar "
                                                  f"(one layer, no crossings): reorder the modules so the lines don't cross, feed "
                                                  f"the reagent from the other edge, or move one of the modules to its own chip.")
        elif not C.crossings_allowed(c):
            add("error", "channel_crossings", f"channels '{a}' and '{b}' cross at ({x:g}, {y:g}) on two layers, but this "
                                              f"process doesn't allow crossings: make the layout planar.")

    # --- walls between neighbouring features ---------------------------------------------------
    wall = g("fabrication.min_feature.wall_mm")
    if wall:
        feats = _features(spec)
        bad = []
        for i, a in enumerate(feats):
            for b in feats[i + 1:]:
                if a["id"] == b["id"] or (a["links"] & b["links"]):
                    continue
                if "any" not in (a["layer"], b["layer"]) and a["layer"] != b["layer"]:
                    continue
                if a["kind"] == "port" and b["kind"] == "port":
                    continue          # port pitch covers these
                gap = _rect_gap(a["box"], b["box"])
                if gap < wall - 1e-4:
                    bad.append((gap, a, b))
        for gap, a, b in sorted(bad, key=lambda t: t[0])[:MAX_WALL_MESSAGES]:
            what = "overlap" if gap <= 0 else f"are {gap:.2f} mm apart"
            add("error", "min_feature.wall_mm",
                f"{a['kind']} '{a['id']}' and {b['kind']} '{b['id']}' {what}, less than the {wall:g} mm minimum wall: "
                f"the chip is too crowded, so give it fewer modules or reagent lines.")
        if len(bad) > MAX_WALL_MESSAGES:
            add("error", "min_feature.wall_mm", f"... and {len(bad) - MAX_WALL_MESSAGES} more thin walls.")
    for ch in spec.get("chambers", []):
        (cx, cy, _), (sx, sy, _) = ch["center"], ch["size"]
        clear = min(cx - sx / 2, cy - sy / 2, fx - cx - sx / 2, fy - cy - sy / 2)
        if clear < -EPS:
            add("error", "envelope.edge", f"chamber '{ch['id']}' sticks out of the {fx:g} x {fy:g} mm chip: make the chip bigger.")
        elif clear < margin - 1e-3:
            add("warning", "envelope.edge_margin_mm", f"chamber '{ch['id']}' is {clear:.2f} mm from the chip edge, inside the "
                                                      f"{margin:g} mm edge margin.")

    # --- valves -----------------------------------------------------------------------------
    valve_mods = [ch["id"] for ch in spec.get("chambers", []) if "valve part" in str(ch.get("note", ""))]
    if not g("fabrication.on_chip_valves.allowed"):
        names = [v["id"] for v in spec.get("valves", [])] + valve_mods
        if names:
            add("error", "on_chip_valves.allowed",
                f"chip has on-chip valves ({', '.join(names)}), but this process can't make them: drop them and switch flow "
                f"off-chip (which pump pushes, which port is capped), or use a passive part such as a tesla valve.")

    # --- gaps: filters, pillars, weirs, meshes ------------------------------------------------
    gap_mm = g("fabrication.min_feature.gap_mm")
    if gap_mm:
        gmin = gap_mm * 1000
        for ch in spec.get("chambers", []):
            ret = ch.get("retention") or {}
            fil = ch.get("filter") or {}
            if fil.get("gap_um") is not None and fil["gap_um"] < gmin - EPS:
                add("error", "min_feature.gap_mm", f"filter '{ch['id']}' needs {fil['gap_um']:g} um gaps, below the {gmin:g} um "
                                                   f"smallest gap this process makes: use a bought mesh, or move the filtering "
                                                   f"off-chip.")
            if ret and ret.get("source") == "fabricated" and ret["opening_um"] < gmin - EPS:
                layers = "" if C.multilayer(c) else (" This process has one fluidic layer, so a bought mesh floor has nothing "
                                                      "to clamp it either.")
                add("error", "min_feature.gap_mm", f"chamber '{ch['id']}' retains with {ret['type']} {ret['opening_um']:g} um apart, "
                                                   f"below the {gmin:g} um smallest gap this process makes.{layers} Needs a bought "
                                                   f"mesh held by a second layer (another process), finer features, or the "
                                                   f"retention step off-chip.")
            elif ch.get("mesh_pore_um") and ch["mesh_pore_um"] < gmin - EPS:
                add("warning", "min_feature.gap_mm", f"chamber '{ch['id']}' needs a {ch['mesh_pore_um']:g} um mesh, finer than the "
                                                     f"{gmin:g} um this process can make: it has to be a bought mesh sheet "
                                                     f"inserted during assembly.")

    # --- temperature --------------------------------------------------------------------------
    zones = spec.get("thermal_zones", [])
    have = set(g("off_chip.equipment") or [])
    if g("off_chip.equipment") is not None:
        for z in zones:
            lo, hi = z.get("min_temp_c", z["max_temp_c"]), z["max_temp_c"]
            if z.get("purpose") == "pcr":
                need, what = CYCLERS, "thermal cycling needs a thermocycler or a Peltier stage"
            elif hi < 18:
                need, what = COOLERS, f"holding {hi:g} C needs a cold plate or a Peltier stage"
            else:
                need, what = HEATERS, f"holding {hi:g} C needs a heater, incubator, Peltier stage or thermocycler"
            if not need & have:
                add("error", "off_chip.equipment", f"'{z['id']}' ({', '.join(z['chambers'])}): {what}, and the lab list has none "
                                                   f"({', '.join(sorted(have)) or 'nothing listed'}): do these steps off-chip, or "
                                                   f"add the equipment to the constraints.")
    distinct = {(z.get("min_temp_c", z["max_temp_c"]), z["max_temp_c"]) for z in zones}
    if len(distinct) > 1 and g("fabrication.thermal_zoning_allowed") is False:
        add("error", "thermal_zoning_allowed", f"chip has {len(distinct)} different temperature zones "
                                               f"({', '.join(z['id'] for z in zones)}), but these constraints don't allow "
                                               f"thermal zones on one chip: put each temperature on its own chip.")
    tr = g("operating.temp_range_c")
    temps = [(z["id"], z.get("min_temp_c", z["max_temp_c"]), z["max_temp_c"]) for z in spec.get("thermal_zones", [])]
    zoned = [set(z["chambers"]) for z in spec.get("thermal_zones", [])]
    temps += [(f["id"], f["temp_c"], f["temp_c"]) for f in spec.get("features", [])     # features not already a thermal zone
              if f.get("temp_c") is not None and set(f.get("covers", [])) not in zoned]
    if tr:
        for zid, lo, hi in temps:
            if hi > tr[1] + EPS or lo < tr[0] - EPS:
                add("error", "operating.temp_range_c", f"'{zid}' runs at {lo:g}-{hi:g} C, outside the {tr[0]:g}-{tr[1]:g} C this "
                                                       f"chip can work at: do that step on another chip or off-chip.")
    try:
        materials = _schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
        mats, _ = _ccs.resolve_materials(spec, materials)
    except (OSError, ValueError, KeyError):
        mats = []
    if mats and temps:
        _, mat = min(mats, key=lambda m: m[1]["max_working_temp_c"])
        for zid, _, hi in temps:
            if hi > mat["max_working_temp_c"]:
                add("error", "material.max_temp", f"'{zid}' reaches {hi:g} C, above {mat['name']}'s {mat['max_working_temp_c']:g} C "
                                                  f"limit: move the hot step to its own chip or off-chip (e.g. a thermocycler).")

    # --- library parts scaled below what the process can make -----------------------------------
    gp = (spec.get("generator") or {}).get("params") or {}
    shrunk = [(mid, m.get("scale", 1.0)) for mid, m in (gp.get("modules") or {}).items()
              if m.get("source") == "library" and m.get("scale", 1.0) < 0.999]
    if shrunk:
        smin = min(sc for _, sc in shrunk)
        feat = NATIVE_FEATURE_MM * smin
        if feat < wmin - EPS:
            n_mod = len(spec.get("chambers", []))
            mx_, my_ = C.max_chip_mm(c)
            limit = f"{mx_:g} x {my_:g} mm" if mx_ and my_ else "this chip"
            fix = (f"too many modules for {limit}: split the chip, use fewer or smaller library parts"
                   + (", or parametric parts" if g("parts.source", "library_and_parametric") != "library_only" else "")
                   if n_mod > 1 else
                   "this one part can't shrink that far: use a smaller part, a different process, or raise envelope.max_chip_mm")
            add("error", "parts.scaled_below_min_feature",
                f"library parts ({', '.join(mid for mid, _ in shrunk)}) are scaled to {smin:.3f} x to fit, so their internal "
                f"channels (about {NATIVE_FEATURE_MM * 1000:g} um at full size) become about {feat * 1000:.0f} um, below the "
                f"{wmin * 1000:.0f} um this process can make, and their chambers shrink {1 / (smin * smin):.0f}-fold: {fix}.")

    # --- the team's design checker (capsule-scale rules) ---------------------------------------
    try:
        params = _schema_io.load_json(ROOT / "contracts" / "parameters.json")
        fab = _schema_io.load_json(ROOT / "cad" / "fabrication.json")
        materials = _schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
        f = _ccs.check(spec, [], params, materials, fab)
        for e in f.errors:
            if not e.startswith("thermal zone '"):      # reported above as material.max_temp, with a fix
                add("error", "check_chip_spec", e)
    except Exception as e:  # noqa: BLE001 - never let the checker crash the stage
        add("warning", "check_chip_spec", f"tools/check_chip_spec.py could not run on chip {cid}: {e}")
    return out

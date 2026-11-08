"""Parts registry: library parts (chip/components/catalogue.json, Flowmatter blocks) plus parametric
parts the compiler sizes itself, filtered by the constraints.

load_parts(constraints) -> {name: part}. Every known part is returned, usable or not:
  name, source ("library" | "parametric"), role, chip_type (contract chamber type, chip_spec.schema.json
  $defs.chamber.type), what_for, size_mm [x, y, z] (library: native; parametric: at the defaults),
  volume_ul, stl (library) | None, openings (fluid connections it can take, main in/out included),
  params {name: {default, unit, description, min?, choices?}} (parametric only),
  needs_one_of [equipment] (the chip must sit on / next to one of these), library_equivalent (parametric:
  nearest library block, used for hand-built design files), available (bool), why_unavailable (str | None).
available(parts) -> only the usable ones; why_unavailable(parts) -> {name: reason}.
parts_for_prompt(parts) -> compact listing of the usable parts for agent prompts (unusable ones left out;
  report them from why_unavailable()).
footprint_mm(part, params, constraints) -> [x, y, z] estimate for a module (used to split chips).
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from autochip import constraints as C

ROOT = Path(__file__).resolve().parent.parent
CATALOGUE = ROOT / "chip" / "components" / "catalogue.json"

# Fluid connections per library role (main in + main out + side lines). A chamber takes reagent lines.
OPENINGS_BY_ROLE = {"chamber": 6, "spiral": 4, "mixer": 3, "merge": 3, "flow_focusing": 4, "split": 5,
                    "pool": 5, "connector": 2, "check_valve": 2, "valve": 2, "port": 1, "unknown": 2}

# Parametric parts. Sizes in mm, volumes in uL, gaps in um. "feature" names the param that must meet a
# fabrication floor: (param, constraints path, mm->um factor, largest value that still does the job).
PARAMETRIC: dict[str, dict] = {
    "chamber": {
        "role": "chamber", "chip_type": "exchange", "openings": 6, "library_equivalent": "culture_chamber",
        "what_for": "Box chamber sized to a volume: hold a liquid or particles while reagents are exchanged, incubate.",
        "params": {"volume_ul": (50, "uL", "liquid volume held"), "depth_mm": (None, "mm", "chamber depth (default: max depth allowed, capped at 0.5)")}},
    "culture_chamber_param": {
        "role": "chamber", "chip_type": "culture", "openings": 6, "library_equivalent": "culture_chamber",
        "what_for": "Culture chamber sized to a volume: grow, feed, rinse and detach cells in place (sits in a 37 C incubator or on a heater).",
        "params": {"volume_ul": (100, "uL", "medium volume"), "depth_mm": (None, "mm", "chamber depth")}},
    "trap_chamber": {
        "role": "chamber", "chip_type": "cell_wash", "openings": 6, "library_equivalent": "culture_chamber",
        "what_for": "Chamber with retention at its outlet: capsules, beads or cells stay while liquids flush through (wash, break emulsion, cure, lyse, react in place).",
        "params": {"volume_ul": (50, "uL", "chamber volume"), "depth_mm": (None, "mm", "chamber depth"),
                   "retention": ("mesh", "", "mesh (inserted membrane, any pore) | weir (depth step under the lid) | pillars"),
                   "gap_um": (25, "um", "mesh pore or weir/pillar gap; under half the particle diameter")},
        "choices": {"retention": ["mesh", "weir", "pillars"]}},
    "mixer": {
        "role": "mixer", "chip_type": "mixer", "openings": 3, "library_equivalent": "serpentine_channel",
        "what_for": "Serpentine mixer after a merge. Size by length_mm, or by residence_s at flow_ul_min. Set viscous=true for wide, gentle turns.",
        "params": {"length_mm": (30, "mm", "channel length"), "residence_s": (None, "s", "alternative to length_mm"),
                   "flow_ul_min": (10, "uL/min", "total flow through it"), "width_mm": (None, "mm", "channel width (default 2x min channel)"),
                   "viscous": (False, "", "wide rounded turns for viscous liquids")}},
    "delay_line": {
        "role": "delay", "chip_type": "mixer", "openings": 2, "library_equivalent": "serpentine_channel",
        "what_for": "Long folded channel for a timed wait in flow (gelation, incubation): volume = residence_s x flow_ul_min / 60, length = volume / (width x depth).",
        "params": {"residence_s": (600, "s", "time the stream spends inside"), "flow_ul_min": (10, "uL/min", "total flow through it"),
                   "width_mm": (None, "mm", "channel width (default 2x min channel, at least 0.5)")}},
    "spiral_separator": {
        "role": "spiral", "chip_type": "cell_wash", "openings": 4, "library_equivalent": "spiral_channel",
        "what_for": "Inertial spiral: moves loose cells into clean buffer (replaces centrifuge pellet + resuspend). Sample + buffer in, cells out, waste out.",
        "params": {"turns": (4, "", "spiral turns"), "width_mm": (0.5, "mm", "channel width")}},
    "pillar_filter": {
        "role": "filter", "chip_type": "filter", "openings": 2, "library_equivalent": None,
        "what_for": "Pillar strainer in flow: removes clumps (cells) or aggregates (capsules). gap_um is the pillar gap.",
        "params": {"gap_um": (30, "um", "pillar gap"), "purpose": ("cells", "", "cells | particles | capsule_aggregates")},
        "choices": {"purpose": ["cells", "particles", "capsule_aggregates"]},
        "feature": ("gap_um", "fabrication.min_feature.gap_mm", 150)},
    "droplet_junction": {
        "role": "flow_focusing", "chip_type": "junction", "openings": 4, "library_equivalent": "cross_junction",
        "what_for": "Flow-focusing junction: centre stream pinched by two side streams (3 inlets, 1 outlet) makes droplets or capsules (droplets come out larger than the nozzle, e.g. ~70 um from a 40 um nozzle). A cell filter upstream must have gaps below the nozzle.",
        "params": {"nozzle_um": (40, "um", "throat width (smaller than the droplets it makes)")},
        "feature": ("nozzle_um", "min_channel", 300)},
    "merge": {
        "role": "merge", "chip_type": "junction", "openings": 3, "library_equivalent": "t_junction",
        "what_for": "Two streams join (T or Y): add a reagent to a flowing stream. Put a mixer after it.",
        "params": {"shape": ("t", "", "t | y")}, "choices": {"shape": ["t", "y"]}},
    "splitter": {
        "role": "split", "chip_type": "split", "openings": 9, "library_equivalent": "flow_manifold",
        "what_for": "One stream into n_out equal branches: split-and-pool rounds, feeding parallel chambers.",
        "params": {"n_out": (4, "", "number of branches")}},
    "pool": {
        "role": "pool", "chip_type": "pool", "openings": 9, "library_equivalent": "ring_junction",
        "what_for": "n_in branches back into one stream.",
        "params": {"n_in": (4, "", "number of branches")}},
    "thermal_chamber": {
        "role": "chamber", "chip_type": "exchange", "openings": 4, "library_equivalent": "culture_chamber",
        "what_for": "Thin sealed chamber for thermal cycling (PCR) or precise heating; the chip sits on the thermocycler block / Peltier stage.",
        "params": {"volume_ul": (25, "uL", "reaction volume"), "depth_mm": (None, "mm", "chamber depth")},
        "needs_one_of": ["thermocycler", "peltier_stage"]},
    "magnetic_chamber": {
        "role": "chamber", "chip_type": "exchange", "openings": 6, "library_equivalent": "culture_chamber",
        "what_for": "Chamber over an external magnet: bind beads, hold them while washing, release and elute.",
        "params": {"volume_ul": (50, "uL", "chamber volume"), "depth_mm": (None, "mm", "chamber depth")},
        "needs_one_of": ["magnet"]},
    "optical_window": {
        "role": "qc", "chip_type": "qc_window", "openings": 2, "library_equivalent": None,
        "what_for": "Shallow clear section for imaging, counting or QC on the microscope.",
        "params": {"length_mm": (5, "mm", "window length")},
        "needs_one_of": ["microscope"]},
    "open_well": {
        "role": "well", "chip_type": "well", "openings": 2, "library_equivalent": None,
        "what_for": "Open-top well for pipette loading or collection.",
        "params": {"volume_ul": (50, "uL", "well volume")}},
    "valve": {
        "role": "valve", "chip_type": "exchange", "openings": 2, "library_equivalent": "normally_closed_valve",
        "what_for": "On-chip on/off valve on a line (only where the process makes valves).",
        "params": {"kind": ("membrane", "", "valve type")}, "needs_valves": True},
}

def _library(constraints: dict) -> list[dict]:
    path = C.get(constraints, "parts.library")
    p = Path(path) if path else CATALOGUE
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return []
    return json.loads(p.read_text())["parts"]


def default_depth_mm(constraints: dict) -> float:
    return min(C.get(constraints, "fabrication.depth.max_mm") or 0.5, 0.5)


def _fits(size: list, constraints: dict) -> bool:
    x, y = C.max_chip_mm(constraints)
    if not x or not y:
        return True
    m = 2 * (C.get(constraints, "envelope.edge_margin_mm") or 0)
    sx, sy = size[0], size[1]
    return (sx <= x - m and sy <= y - m) or (sy <= x - m and sx <= y - m)


def load_parts(constraints: dict) -> dict[str, dict]:
    source = C.get(constraints, "parts.source", "library_and_parametric")
    allowed = C.get(constraints, "parts.allowed")
    forbidden = set(C.get(constraints, "parts.forbidden") or [])
    equipment = set(C.get(constraints, "off_chip.equipment") or [])
    valves = bool(C.get(constraints, "fabrication.on_chip_valves.allowed", False))
    scaling = bool(C.get(constraints, "parts.allow_scaling", False))
    depth = default_depth_mm(constraints)
    out: dict[str, dict] = {}

    for p in _library(constraints):
        role = p.get("role", "unknown")
        part = {"name": p["part"], "source": "library", "role": role, "chip_type": p.get("chip_type", "exchange"),
                "what_for": p.get("what_for", ""), "size_mm": p["size_mm"], "volume_ul": p.get("volume_ul"),
                "stl": p.get("stl"), "openings": OPENINGS_BY_ROLE.get(role, 2), "params": {}, "needs_one_of": [],
                "library_equivalent": p["part"], "name_confidence": p.get("name_confidence"),
                "available": True, "why_unavailable": None}
        why = None
        if source == "parametric_only":
            why = "constraints allow parametric parts only (parts.source = parametric_only)"
        elif role == "port":
            why = "ports are added by the build (inlet_1, outlet_1, <name>_in), not placed as modules"
        elif role == "unknown":
            why = f"role not known yet (catalogue name confidence: {p.get('name_confidence')})"
        elif role == "valve" and not valves:
            why = "on-chip valves are not allowed by the fabrication process"
        elif not scaling and not _fits(p["size_mm"], constraints):
            x, y = C.max_chip_mm(constraints)
            why = f"native size {p['size_mm'][0]:.0f} x {p['size_mm'][1]:.0f} mm does not fit the {x} x {y} mm chip and scaling is not allowed"
        if role == "check_valve":
            part["what_for"] += " Only works at Re well above 1; at microfluidic flows it does nothing."
        out[p["part"]] = _gate(part, why, allowed, forbidden)

    for name, d in PARAMETRIC.items():
        params = {}
        for k, (default, unit, desc) in d["params"].items():
            params[k] = {"default": default, "unit": unit, "description": desc}
            if "choices" in d and k in d["choices"]:
                params[k]["choices"] = d["choices"][k]
        if "depth_mm" in params:
            params["depth_mm"]["default"] = depth
        if "width_mm" in params and params["width_mm"]["default"] is None:
            params["width_mm"]["default"] = round(max(2 * C.min_channel_mm(constraints), 0.5 if name == "delay_line" else 0.1), 3)
        part = {"name": name, "source": "parametric", "role": d["role"], "chip_type": d["chip_type"],
                "what_for": d["what_for"], "size_mm": None, "volume_ul": None, "stl": None, "openings": d["openings"],
                "params": params, "needs_one_of": d.get("needs_one_of", []),
                "library_equivalent": d.get("library_equivalent"), "available": True, "why_unavailable": None}
        why = None
        if source == "library_only":
            why = "constraints allow library parts only (parts.source = library_only)"
        elif d.get("needs_valves") and not valves:
            why = "on-chip valves are not allowed by the fabrication process"
        elif part["needs_one_of"] and not equipment & set(part["needs_one_of"]):
            why = f"needs {' or '.join(part['needs_one_of'])}, which is not in off_chip.equipment"
        if "feature" in d:
            pname, path, useful_max = d["feature"]
            floor_mm = C.min_channel_mm(constraints) if path == "min_channel" else C.get(constraints, path)
            if floor_mm:
                floor_um = round(floor_mm * 1000, 1)
                params[pname]["min"] = floor_um
                if params[pname]["default"] < floor_um:
                    params[pname]["default"] = floor_um
                if floor_um > useful_max and why is None:
                    what = "smallest gap" if "gap" in pname else "narrowest channel"
                    why = (f"the {what} this process makes is {floor_um:g} um "
                           f"({'fabrication.min_feature.gap_mm' if path != 'min_channel' else 'min channel width'}); "
                           f"{name} needs {pname} of {useful_max} um or less to do its job")
        if name == "trap_chamber":
            gap = C.get(constraints, "fabrication.min_feature.gap_mm")
            if gap:
                params["gap_um"]["min_printed"] = round(gap * 1000, 1)
                params["gap_um"]["description"] += f"; weir/pillars need gap >= {gap * 1000:g} um here, a mesh can be finer"
        part["size_mm"] = footprint_mm(part, {}, constraints)
        part["volume_ul"] = _volume(part, {})
        out[name] = _gate(part, why, allowed, forbidden)
    return out


def _gate(part: dict, why: str | None, allowed, forbidden: set) -> dict:
    if why is None and allowed is not None and part["name"] not in allowed:
        why = "not in parts.allowed"
    if why is None and part["name"] in forbidden:
        why = "listed in parts.forbidden"
    part["available"] = why is None
    part["why_unavailable"] = why
    return part


def available(parts: dict) -> dict:
    return {k: v for k, v in parts.items() if v.get("available", True)}


def why_unavailable(parts: dict) -> dict[str, str]:
    return {k: v["why_unavailable"] for k, v in parts.items() if not v.get("available", True)}


def param(part: dict, params: dict | None, key: str):
    """The module's value for a parametric param, else the part default."""
    if params and params.get(key) is not None:
        return params[key]
    spec = (part.get("params") or {}).get(key)
    return spec.get("default") if spec else None


def _volume(part: dict, params: dict) -> float | None:
    if part["source"] == "library":
        return part.get("volume_ul")
    name = part["name"]
    if "volume_ul" in part["params"]:
        return param(part, params, "volume_ul")
    if name == "delay_line":
        return round(param(part, params, "residence_s") * param(part, params, "flow_ul_min") / 60, 2)
    return None


def delay_length_mm(part: dict, params: dict, constraints: dict) -> float:
    vol = (param(part, params, "residence_s") or 0) * (param(part, params, "flow_ul_min") or 0) / 60
    w = param(part, params, "width_mm") or 0.5
    return vol / (w * default_depth_mm(constraints))


def footprint_mm(part: dict, params: dict | None, constraints: dict) -> list[float]:
    """Rough [x, y, z] of one module, for deciding chip boundaries before layout."""
    params = params or {}
    if part["source"] == "library":
        sx, sy, sz = part["size_mm"]
        x, y = C.max_chip_mm(constraints)
        if x and y and C.get(constraints, "parts.allow_scaling") and not _fits([sx, sy], constraints):
            m = 2 * (C.get(constraints, "envelope.edge_margin_mm") or 0)
            s = max(min((x - m) / sx, (y - m) / sy), min((x - m) / sy, (y - m) / sx))
            sx, sy = sx * s, sy * s
        return [round(sx, 2), round(sy, 2), sz]
    depth = param(part, params, "depth_mm") or default_depth_mm(constraints)
    wall = C.get(constraints, "fabrication.min_feature.wall_mm") or 0.1
    name = part["name"]
    if "volume_ul" in part["params"]:
        vol = param(part, params, "volume_ul") or 10
        d = depth if name != "open_well" else 2.0
        area = vol / d
        x, y = math.sqrt(2 * area), math.sqrt(area / 2)
        return [round(x + 2 * wall, 2), round(y + 2 * wall, 2), round(d, 3)]
    if name in ("mixer", "delay_line"):
        w = param(part, params, "width_mm") or 0.5
        if name == "delay_line" or param(part, params, "residence_s"):
            length = delay_length_mm(part, params, constraints)
        else:
            length = param(part, params, "length_mm") or 30
        area = length * (w + wall) * (1.5 if param(part, params, "viscous") else 1.0)
        side = max(math.sqrt(area), 3 * w)
        return [round(side, 2), round(side, 2), round(depth, 3)]
    if name == "spiral_separator":
        r = 2 + (param(part, params, "turns") or 4) * 2 * ((param(part, params, "width_mm") or 0.5) + wall)
        return [round(2 * r, 2), round(2 * r, 2), round(depth, 3)]
    n = param(part, params, "n_out") or param(part, params, "n_in") or 1
    fixed = {"pillar_filter": [6, 4], "droplet_junction": [6, 6], "merge": [4, 4], "optical_window": [6, 3],
             "valve": [3, 3], "splitter": [3 + 2 * n, 6], "pool": [3 + 2 * n, 6]}
    x, y = fixed.get(name, [5, 5])
    return [x, y, round(depth, 3)]


def parts_for_prompt(parts: dict) -> str:
    """Compact listing of the usable parts. Unusable ones are left out (see why_unavailable)."""
    lines = []
    for name, p in sorted(available(parts).items(), key=lambda kv: (kv[1]["source"], kv[0])):
        head = f"- {name} [{p['source']}, {p['role']} -> {p['chip_type']}, {p['openings']} openings]"
        if p["source"] == "library":
            sx, sy, sz = p["size_mm"]
            head += f" {sx:g} x {sy:g} x {sz:g} mm, {p['volume_ul']:g} uL"
        head += f": {p['what_for']}"
        if p["params"]:
            ps = []
            for k, s in p["params"].items():
                bit = (f"{k}={s['default']}{(' ' + s['unit']) if s['unit'] else ''}" if s["default"] is not None
                       else f"{k} ({s['unit']}, optional)")
                if s.get("min") is not None:
                    bit += f" (min {s['min']:g})"
                if s.get("choices"):
                    bit += f" ({'|'.join(s['choices'])})"
                ps.append(bit)
            head += f" Params: {', '.join(ps)}."
        if p["needs_one_of"]:
            head += f" Needs {' or '.join(p['needs_one_of'])}."
        lines.append(head)
    return "\n".join(lines)

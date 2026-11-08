"""Stage 4 (OWNER: agent C). Chip plan -> one contract chip spec per chip + CAD files.

run(plan, ir, constraints, parts, out_dir) -> dict[chip_id, {"spec": dict, "violations": [ {"severity": "error"|"warning", "rule": str, "message": str} ], "files": {name: path}}]
Writes out_dir/chips/<chip>.json and out_dir/cad/<chip>/...

How a chip is laid out
  1. Size every module. Library parts keep their native footprint (scaled down uniformly when
     parts.allow_scaling and the chip would not fit max_chip_mm otherwise); their depth is clamped
     to the process depth. Parametric parts are sized from their params: a chamber from volume_ul
     and depth, a delay line from residence_s x flow (-> channel length, folded into a serpentine
     bounding box), a mixer from length_mm, a pillar filter from gap_um, a junction from the
     channel width.
  2. Place them in flow order along rows: inlet_1 on the left, outlet_1 at the end of the last row
     (right for one row), rows snake (boustrophedon) when the chip would get too wide, near-square
     when nothing limits the size. Each module owns a vertical "slot"; its reagent inlets
     (<name>_in) sit in the slot on the top edge and its extra outlets (<name>_out) on the bottom
     edge, at least ports.min_pitch_mm apart and edge_margin_mm from the edge.
  3. Route orthogonal channels with `path` waypoints from the edge of the from-node to the edge of
     the to-node (ending at port centres). Main path along the row midline, row changes in the side
     columns, reagent lines straight down/up (or a non-crossing dog-leg when a slot's ports are
     wider than its module). The arrangement is planar by construction; any crossing left over is
     moved to a second fluidic layer when the process allows it, else checks reports an error.
  4. checks.check_chip(spec, constraints) + layout findings -> violations; autochip/cad.py writes
     the SVG/PNG/DXF/STL/Fusion/BUILD.md files.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from autochip import cad, checks
from autochip import constraints as C

ROOT = Path(__file__).resolve().parents[2]
CATALOGUE = ROOT / "chip" / "components" / "catalogue.json"
SCRIPT = "autochip/stages/layout.py"

CHAMBER_TYPES = {"inlet", "exchange", "split", "pool", "outlet", "well", "culture", "junction", "bubble_trap",
                 "filter", "mixer", "cell_wash", "qc_window"}
NATIVE_FEATURE_MM = checks.NATIVE_FEATURE_MM   # assumed narrowest internal channel of a library block at native size
DEFAULT_FLOW_UL_MIN = 10.0
DEFAULT_RESIDENCE_S = 60.0
DEFAULT_CHAMBER_UL = 10.0
DEFAULT_FILTER_GAP_UM = 20.0
PREFERRED_DEPTH_MM = 0.3
ROOM_C = 22.0

# Built-in parametric parts, used only until autochip/parts.py answers (and for unknown parametric names).
PARAMETRIC_FALLBACK = {
    "chamber": {"chip_type": "exchange", "role": "chamber", "size_mm": [6.0, 3.0, 0.5], "volume_ul": 9.0,
                "params": {"volume_ul": {"default": 10, "unit": "uL"}, "depth_mm": {"default": 0.5, "unit": "mm"}}},
    "trap_chamber": {"chip_type": "exchange", "role": "trap", "size_mm": [6.0, 3.0, 0.5], "volume_ul": 9.0,
                     "params": {"volume_ul": {"default": 10, "unit": "uL"}, "mesh_pore_um": {"default": 20, "unit": "um"}}},
    "delay_line": {"chip_type": "mixer", "role": "delay", "size_mm": [10.0, 5.0, 0.3], "volume_ul": 5.0,
                   "params": {"residence_s": {"default": 60, "unit": "s"}, "flow_ul_min": {"default": 10, "unit": "uL/min"}}},
    "serpentine_mixer": {"chip_type": "mixer", "role": "mixer", "size_mm": [6.0, 4.0, 0.3], "volume_ul": 2.0,
                         "params": {"length_mm": {"default": 30, "unit": "mm"}}},
    "pillar_filter": {"chip_type": "filter", "role": "filter", "size_mm": [3.0, 3.0, 0.3], "volume_ul": 1.0,
                      "params": {"gap_um": {"default": 20, "unit": "um"}}},
    "junction": {"chip_type": "junction", "role": "merge", "size_mm": [2.0, 2.0, 0.3], "volume_ul": 0.5, "params": {}},
    "flow_focusing_junction": {"chip_type": "junction", "role": "flow_focusing", "size_mm": [2.0, 2.0, 0.3],
                               "volume_ul": 0.5, "params": {"nozzle_um": {"default": 40, "unit": "um"}}},
}


def R(v: float) -> float:
    return round(float(v), 4)


def sid(s: str, prefix: str = "x") -> str:
    """Any plan id -> a contract snake_id (^[a-z][a-z0-9]*(_[a-z0-9]+)*$)."""
    s = re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")
    s = re.sub(r"_+", "_", s) or prefix
    return s if s[0].isalpha() else f"{prefix}_{s}"


def finding(severity: str, rule: str, message: str) -> dict:
    return {"severity": severity, "rule": rule, "message": message}


# --------------------------------------------------------------------------- parts adapter


def _catalogue_parts() -> dict:
    out = {}
    try:
        cat = json.loads(CATALOGUE.read_text())["parts"]
    except (OSError, ValueError, KeyError):
        cat = []
    for p in cat:
        out[p["part"]] = {"name": p["part"], "source": "library", "chip_type": p.get("chip_type", "exchange"),
                          "role": p.get("role", ""), "what_for": p.get("what_for", ""), "size_mm": p["size_mm"],
                          "volume_ul": p.get("volume_ul"), "stl": p.get("stl"), "openings": 2, "params": {}}
    for name, p in PARAMETRIC_FALLBACK.items():
        out[name] = {"name": name, "source": "parametric", "stl": None, "openings": 2, "what_for": "", **p}
    return out


def registry(constraints: dict, parts: dict | None) -> dict:
    """parts from the pipeline (autochip.parts.load_parts) when given, else the catalogue + built-ins."""
    if parts:
        return parts
    try:
        from autochip import parts as P
        got = P.load_parts(constraints)
        if got:
            return got
    except Exception:  # noqa: BLE001 - parts.py may still be a stub
        pass
    return _catalogue_parts()


def _kind(name: str, part: dict, params: dict) -> str:
    role = str(part.get("role") or "").lower()
    ctype = str(part.get("chip_type") or "").lower()
    n = name.lower()
    if role == "valve" or (n.endswith("valve") and "tesla" not in n):
        return "valve"
    if part.get("source") == "library":
        return "library"
    if role in ("delay", "delay_line") or "delay" in n or "residence_s" in params:
        return "delay"
    if role == "mixer" or ctype == "mixer" or "mix" in n or "serpentine" in n:
        return "mixer"
    if role == "filter" or ctype == "filter" or "filter" in n:
        return "filter"
    if role in ("merge", "flow_focusing", "junction") or ctype == "junction" or "junction" in n or n == "merge":
        return "junction"
    vol_param = "volume_ul" in (part.get("params") or {}) or params.get("volume_ul") is not None
    if role in ("chamber", "trap", "culture", "well") and (vol_param or part.get("volume_ul")):
        return "chamber"
    if vol_param:
        return "chamber"
    return "fixed"          # spiral, splitter, pool, window, parametric valve...: the registry's own footprint


def _chamber_type(part: dict, kind: str) -> str:
    t = part.get("chip_type")
    if t in CHAMBER_TYPES:
        return t
    return {"delay": "mixer", "mixer": "mixer", "filter": "filter", "junction": "junction"}.get(kind, "exchange")


# --------------------------------------------------------------------------- process numbers


class Process:
    """The numbers layout needs from the constraints, with defaults filled in."""

    def __init__(self, c: dict):
        def g(p, d=None):
            return C.get(c, p, d)
        self.c = c
        self.method = g("fabrication.method", "other")
        self.sealing = g("fabrication.sealing")
        self.material = g("fabrication.material")
        self.margin = float(g("envelope.edge_margin_mm", 2.0))
        self.wall = float(g("fabrication.min_feature.wall_mm", 0.1))
        self.port_d = max(float(g("ports.min_diameter_mm", 0.8)), 1.0)
        self.pitch = max(float(g("ports.min_pitch_mm", 0) or 0), self.port_d + 2 * self.wall)
        self.max_x, self.max_y = C.max_chip_mm(c)
        self.single_depth = bool(g("fabrication.depth.single_depth", False))
        self.depth_max = g("fabrication.depth.max_mm")
        self.depth_min = g("fabrication.depth.min_mm")
        self.allowed_depths = g("fabrication.depth.allowed_mm") or []
        self.multilayer = C.multilayer(c)
        self.max_layers = g("fabrication.multilayer.max_fluidic_layers")
        self.crossings_ok = C.crossings_allowed(c)
        self.valves_ok = bool(g("fabrication.on_chip_valves.allowed", False))
        self.allow_scaling = bool(g("parts.allow_scaling", False))
        self.parts_source = g("parts.source", "library_and_parametric")
        self.min_channel = checks.min_channel_mm(c)
        self.gap = max(1.0, 3 * self.wall)              # clearance between modules / rows
        self.channel_depth = self.snap_depth(PREFERRED_DEPTH_MM)

    def snap_depth(self, want: float) -> float:
        d = want
        if self.depth_max:
            d = min(d, self.depth_max)
        if self.depth_min:
            d = max(d, self.depth_min)
        if self.allowed_depths:
            ok = [a for a in self.allowed_depths if not self.depth_max or a <= self.depth_max + 1e-9] or self.allowed_depths
            below = [a for a in ok if a <= d + 1e-9]
            d = max(below) if below else min(ok)
        return R(d)

    def chamber_depth(self, want: float) -> float:
        return self.channel_depth if self.single_depth else self.snap_depth(want)


# --------------------------------------------------------------------------- module sizing


def _ir_steps(ir: dict | None) -> dict:
    return {s.get("n"): s for s in (ir or {}).get("steps", []) if isinstance(s, dict)}


def _material_carries(mat: str | None) -> str:
    return {"cells": "cells", "beads": "cells", "capsules": "capsules", "droplets": "droplets",
            "oil": "oil"}.get(mat or "", "liquid")


def channel_width(carries: str, proc: Process, capsule_um: float) -> float:
    """max(the process's narrowest channel, a sensible width for what it carries)."""
    base = {"capsules": max(0.5, 1.6 * capsule_um / 1000), "droplets": max(0.5, 1.6 * capsule_um / 1000),
            "cells": 0.3, "oil": 0.3, "liquid": 0.2}.get(carries, 0.2)
    return R(max(proc.min_channel, base))


def _registry_footprint(part: dict, params: dict, cons: dict) -> list[float]:
    try:
        from autochip import parts as P
        return [float(v) for v in P.footprint_mm(part, params, cons)]
    except Exception:  # noqa: BLE001 - parts.py missing or a part it can't size
        return [float(v) for v in (part.get("size_mm") or [3.0, 3.0, 0.5])]


def size_module(mod: dict, part: dict, kind: str, proc: Process, steps: dict, main_w: float) -> dict:
    """-> {w, h, d, volume_ul, type, extra (chamber fields), scalable, notes[]} at native scale."""
    params = dict(mod.get("params") or {})
    msteps = [steps[n] for n in mod.get("steps", []) if n in steps]
    notes: list[str] = []
    pdef = {k: (v.get("default") if isinstance(v, dict) else v) for k, v in (part.get("params") or {}).items()}

    def p(key, fallback=None):
        if params.get(key) is not None:
            return params[key]
        return pdef.get(key) if pdef.get(key) is not None else fallback
    ctype = _chamber_type(part, kind)
    extra: dict = {}
    if part.get("source") == "library":
        sx, sy, sz = [float(v) for v in part["size_mm"]]
        d = proc.chamber_depth(sz)
        vol = float(part.get("volume_ul") or sx * sy * sz) * (d / sz if sz else 1)
        if abs(d - sz) > 1e-6:
            notes.append(f"native depth {sz:g} mm set to the process depth {d:g} mm")
        return {"w": sx, "h": sy, "d": d, "volume_ul": vol, "type": ctype, "extra": extra, "scalable": True, "notes": notes,
                "shape_kind": "library", "native": (sx, sy)}

    w_line = max(float(p("width_mm") or main_w), proc.min_channel)
    pitch = w_line + max(proc.wall, w_line)
    if kind == "fixed":
        sx, sy, sz = _registry_footprint(part, params, proc.c)
        d = proc.chamber_depth(float(p("depth_mm") or sz or proc.channel_depth))
        notes.append(f"footprint from the parts registry ({sx:g} x {sy:g} mm)")
        return {"w": max(sx, 1.0), "h": max(sy, 1.0), "d": d, "volume_ul": sx * sy * d * 0.5, "type": ctype, "extra": extra,
                "scalable": False, "notes": notes}
    if kind in ("delay", "mixer"):
        d = proc.chamber_depth(float(p("depth_mm", proc.channel_depth)))
        if kind == "delay" or params.get("residence_s") is not None:
            flow = p("flow_ul_min") or max([s.get("flow_ul_min") or 0 for s in msteps] + [0]) or DEFAULT_FLOW_UL_MIN
            res = params.get("residence_s") or max([s.get("duration_s") or 0 for s in msteps] + [0]) or p("residence_s") \
                or DEFAULT_RESIDENCE_S
            vol = float(res) / 60.0 * float(flow)
            length = params.get("length_mm") or vol / (w_line * d)
            notes.append(f"{float(res):g} s residence at {float(flow):g} uL/min = {vol:.3g} uL -> {float(length):.4g} mm "
                         f"of {w_line:g} x {d:g} mm channel")
        else:
            length = float(p("length_mm", 30.0))
        length = max(float(length), 2.0)
        extra["mixer"] = {"kind": "serpentine", "channel_width_mm": R(w_line), "turns": 1, "length_mm": R(length)}
        md = {"d": d, "volume_ul": length * w_line * d, "type": "mixer", "extra": extra, "scalable": False, "notes": notes,
              "shape_kind": "serpentine", "length": length, "w_line": w_line, "wall": proc.wall}
        _shape(md, 1.0, False, False)
        return md
    if kind == "filter":
        d = proc.chamber_depth(float(p("depth_mm", proc.channel_depth)))
        gap_um = float(p("gap_um") or mod.get("mesh_pore_um") or DEFAULT_FILTER_GAP_UM)
        gap = gap_um / 1000
        post = max(proc.wall, 2 * gap)
        fp = post + gap
        w = max(2.0, 6 * fp + 4 * w_line)
        h = max(2.0, 20 * fp, 4 * w_line)
        holds = mod.get("holds")
        purpose = "cells" if holds == "cells" else "capsule_aggregates" if holds in ("capsules", "droplets") else "particles"
        if params.get("purpose") in ("cells", "particles", "capsule_aggregates"):
            purpose = params["purpose"]
        extra["filter"] = {"type": "pillars", "gap_um": R(gap_um), "purpose": purpose}
        notes.append(f"pillar filter: {gap_um:g} um gaps between {post * 1000:.3g} um posts")
        return {"w": w, "h": h, "d": d, "volume_ul": w * h * d * 0.5, "type": "filter", "extra": extra, "scalable": False,
                "notes": notes}
    if kind == "junction":
        d = proc.chamber_depth(float(p("depth_mm", proc.channel_depth)))
        side = max(1.5, 6 * w_line)
        return {"w": side, "h": side, "d": d, "volume_ul": side * side * d,
                "type": ctype if ctype != "exchange" else "junction", "extra": extra, "scalable": False, "notes": notes}
    # chamber (default; also a parametric valve body)
    want = p("depth_mm") or (part.get("size_mm") or [0, 0, 0.5])[2] or 0.5
    d = proc.chamber_depth(float(want))
    vol = float(params.get("volume_ul") or max([s.get("volume_ul") or 0 for s in msteps] + [0]) or p("volume_ul")
                or part.get("volume_ul") or DEFAULT_CHAMBER_UL)
    notes.append(f"{vol:g} uL at {d:g} mm deep")
    ret, gap = p("retention"), p("gap_um")
    if ret in ("mesh", "pillars", "weir") and gap and not mod.get("mesh_pore_um"):
        extra["retention"] = {"type": ret, "opening_um": float(gap), "source": "bought" if ret == "mesh" else "fabricated"}
        if ret == "mesh":
            extra["mesh_pore_um"] = float(gap)
    md = {"d": d, "volume_ul": vol, "type": ctype, "extra": extra, "scalable": False, "notes": notes,
          "shape_kind": "area", "area": vol / d}
    _shape(md, 1.0, False, False)
    return md


def _shape(md: dict, q: float, tight: bool, rot: bool) -> None:
    """Set a module's footprint (w, h) for one shape option. Parametric chambers keep their volume (area x depth)
    and take aspect 2q; serpentines (delay lines, mixers) keep their channel length and fold to aspect ~q, with
    tighter lanes (wall = max(min wall, w/4)) when `tight`; library parts keep their shape, turned 90 deg when `rot`."""
    kind = md.get("shape_kind")
    if kind == "area":
        area = md["area"]
        w = max(1.0, math.sqrt(area * 2 * q))
        h = max(1.0, area / w)
        md["w"], md["h"] = max(w, area / h), h
    elif kind == "serpentine":
        wl, L = md["w_line"], md["length"]
        pitch = wl + (max(md["wall"], 0.25 * wl) if tight else max(md["wall"], wl))
        lanes = max(1, round(math.sqrt(L / (pitch * q))))
        lane = max(L / lanes, 2 * pitch)
        md["w"], md["h"] = lane + wl, lanes * pitch + wl
        md["extra"]["mixer"]["turns"] = max(1, lanes - 1)
        md["fold"] = (lanes, lane, pitch)
    elif kind == "library":
        sx, sy = md["native"]
        md["w"], md["h"], md["rot"] = (sy, sx, True) if rot else (sx, sy, False)


# --------------------------------------------------------------------------- placement


def _assign_port_sides(mods: list[dict]) -> None:
    """Reagent inlets (and a valve control port) on the top edge, extra outlets on the bottom edge;
    spill inlets to the bottom when the top has more than one extra."""
    for m in mods:
        top = [("inlet", n) for n in m["inlets"]]
        bot = [("outlet", n) for n in m["outlets"]]
        if m.get("control"):
            top.append(("control", m["control"]))
        while len(top) > len(bot) + 1:
            bot.append(top.pop())
        m["top"], m["bottom"] = top, bot


def _band(row: list[dict], side: str, hmax: float, proc: Process, bus: float, rw: float) -> float:
    """Height of the port band on one side of a row (or lane) of modules, measured from its tallest edge."""
    P, pd = proc.pitch, proc.port_d
    need = 0.0
    for md in row:
        k = len(md[side])
        if not k:
            continue
        levels = k if k * P > md["W"] - 2 * proc.wall else 0
        drop = bus * (levels + 1) - (hmax - md["H"]) / 2      # shorter modules have room already
        need = max(need, max(drop, 2 * proc.wall + rw) + pd)
    return need


def _pad_bands(top: float, hmax: float, bot: float, P: float, pd: float) -> tuple[float, float]:
    """Grow the two port bands of a row (or lane) so its top and bottom ports stay at least a port pitch apart."""
    short = P - (top + hmax + bot - pd)
    if top and bot and short > 0:
        return top + short / 2, bot + short / 2
    return top, bot


def _col(md: dict, P: float) -> float:
    """Width a module needs: itself, or its ports at pitch, whichever is wider."""
    return max(md["W"], len(md["top"]) * P, len(md["bottom"]) * P)


# --------------------------------------------------------------------------- parallel lanes


def _is_split(md: dict) -> bool:
    part, n = md["part"], md["part_name"].lower()
    n_out = (md["plan"].get("params") or {}).get("n_out")
    return (str(part.get("role", "")).lower() == "split" or part.get("chip_type") == "split"
            or any(k in n for k in ("split", "distributor", "manifold", "fork")) or (isinstance(n_out, (int, float)) and n_out > 1))


def _is_pool(md: dict) -> bool:
    part, n = md["part"], md["part_name"].lower()
    n_in = (md["plan"].get("params") or {}).get("n_in")
    return (str(part.get("role", "")).lower() == "pool" or part.get("chip_type") == "pool" and "junction" not in n
            or "pool" in n or "ring_junction" in n or (isinstance(n_in, (int, float)) and n_in > 1))


def _period(keys: list) -> int | None:
    """Smallest lane length L such that the modules repeat every L and there are at least 2 lanes."""
    for L in range(1, len(keys) // 2 + 1):
        if len(keys) % L == 0 and all(keys[k] == keys[k % L] for k in range(len(keys))):
            return L
    return None


def _units(mods: list[dict], findings: list[dict]) -> list[dict]:
    """Group modules into placement units: single modules, and split -> parallel lanes -> pool blocks.
    Lanes are the modules between a split part and the next pool part that repeat with the same steps
    (one module per lane, or a repeating chain). Without a pool, a run of modules with the same steps right
    after the split are the lanes and the module after them collects them."""
    units, i = [], 0
    while i < len(mods):
        md = mods[i]
        blk = None
        if _is_split(md):
            j = next((k for k in range(i + 1, len(mods)) if _is_pool(mods[k])), None)

            def key(m, part_only=False):
                return m["part_name"] if part_only else (m["part_name"], tuple(sorted(m["plan"].get("steps", []))))
            if j is not None:
                between = mods[i + 1:j]
                L = _period([key(m) for m in between]) or _period([key(m, True) for m in between])
                if L:
                    blk = {"split": md, "pool": mods[j], "lanes": [between[k:k + L] for k in range(0, len(between), L)], "end": j + 1}
            else:
                run = 0
                while i + 1 + run < len(mods) and key(mods[i + 1 + run]) == key(mods[i + 1]):
                    run += 1
                if run >= 2 and i + 1 + run < len(mods):
                    blk = {"split": md, "pool": mods[i + 1 + run], "lanes": [[m] for m in mods[i + 1:i + 1 + run]],
                           "end": i + 2 + run}
            if blk is None and i + 1 < len(mods):
                findings.append(finding("warning", "layout.lanes",
                                        f"'{md['id']}' splits the flow, but the modules after it don't form parallel lanes "
                                        f"(the same steps repeated) ending in a pool, so they are laid out in series."))
        if blk:
            n_out = (md["plan"].get("params") or {}).get("n_out")
            if isinstance(n_out, (int, float)) and n_out != len(blk["lanes"]):
                findings.append(finding("warning", "layout.lanes",
                                        f"'{md['id']}' has n_out = {n_out:g} but {len(blk['lanes'])} lanes follow it; "
                                        f"drew {len(blk['lanes'])} lanes."))
            blk.update(unit="block", id=md["id"], mods=[md] + [m for ln in blk["lanes"] for m in ln] + [blk["pool"]])
            units.append(blk)
            i = blk["end"]
        else:
            md["unit"] = "module"
            units.append(md)
            i += 1
    return units


def _block_size(b: dict, proc: Process, rw: float, mw: float, bus: float) -> None:
    """Size a split -> lanes -> pool block: split column, fan-out bus, lanes stacked side by side,
    fan-in bus, pool column."""
    S, Pm, lanes = b["split"], b["pool"], b["lanes"]
    P, g, pd = proc.pitch, proc.gap, proc.port_d
    sp = mw + max(proc.wall, mw)
    lg = []
    for lane in lanes:
        slots = [_col(md, P) for md in lane]
        hmax = max(md["H"] for md in lane)
        tb, bb = _pad_bands(_band(lane, "top", hmax, proc, bus, rw), hmax, _band(lane, "bottom", hmax, proc, bus, rw), P, pd)
        x = {"slots": slots, "w": sum(slots) + g * (len(lane) - 1), "hmax": hmax, "top": tb, "bot": bb}
        x["h"] = x["top"] + x["hmax"] + x["bot"]
        lg.append(x)
    gaps = []
    for a, c in zip(lg, lg[1:]):
        gap = max(g, mw + 2 * proc.wall)
        if a["bot"] and c["top"]:
            gap = max(gap, P - pd)                  # bottom ports of one lane vs top ports of the next
        if a["top"] and c["top"]:
            gap = max(gap, P - a["h"])
        gaps.append(gap)
    stack = sum(x["h"] for x in lg) + sum(gaps)
    # a parametric split / pool is drawn as a manifold spanning its lanes, so every fan channel is straight and the
    # lanes get equal flow; library blocks keep their shape (nested dog-legs instead)
    first = stack / 2 - lg[0]["top"] - lg[0]["hmax"] / 2
    last = -stack / 2 + lg[-1]["bot"] + lg[-1]["hmax"] / 2
    span = 2 * (max(abs(first), abs(last)) + mw / 2 + proc.wall + 0.05)    # + slack for the 0.01 mm rounding of lane positions
    for md in (S, Pm):
        if not md["scalable"] and md["H"] < span:
            md["H"], md["stretched"] = span, "span"
            md["W"] = min(md["W"], max(2.0, 4 * mw))     # a manifold spanning its lanes only needs to be a few channels wide
        elif md["H"] < (len(lanes) + 1) * sp:        # too short an edge for one channel per lane at channel pitch
            md["H"], md["stretched"] = (len(lanes) + 1) * sp, "outlets"

    def need(md):
        tb, bb = _pad_bands(_band([md], "top", md["H"], proc, bus, rw), md["H"], _band([md], "bottom", md["H"], proc, bus, rw), P, pd)
        return md["H"] + 2 * max(tb, bb)
    cS, cP = _col(S, P), _col(Pm, P)
    def fan_w(md):      # straight fan channels (a manifold spanning the lanes) need no bus columns, just clearance
        return g if md.get("stretched") == "span" else sp * (len(lanes) + 1) + g
    fan, fan_p = fan_w(S), fan_w(Pm)
    lane_w = max(x["w"] for x in lg)
    b.update(W=cS + fan + lane_w + fan_p + cP, H=max(stack, need(S), need(Pm)), lg=lg, gaps=gaps, stack=stack, cS=cS, cP=cP,
             fan=fan, sp=sp, top=[], bottom=[])


def _place_block(b: dict, x0: float, y_mid: float, d: int, r_i: int, proc: Process, rw: float, bus: float) -> None:
    """Global positions of every module in a block (flow left to right, mirrored when the row runs right to left)."""
    W, H, pd, g = b["W"], b["H"], proc.port_d, proc.gap
    y0 = y_mid - H / 2
    b.update(dir=d, x0=x0, y0=y0, row=r_i)

    def put(md, lx0, gy0, lslot, ptop, pbot):
        md["x0"] = round(x0 + lx0 if d == 1 else x0 + W - lx0 - md["W"], 2)
        md["y0"] = gy0
        a, c = lslot
        md["slot"] = (x0 + a, x0 + c) if d == 1 else (x0 + W - c, x0 + W - a)
        md["port_top_y"] = round(ptop, 2) if ptop is not None else None
        md["port_bot_y"] = round(pbot, 2) if pbot is not None else None
        md["row"] = r_i
    for md, col, lx in ((b["split"], b["cS"], 0.0), (b["pool"], b["cP"], W - b["cP"])):
        gy0 = y_mid - md["H"] / 2
        tb, bb = _pad_bands(_band([md], "top", md["H"], proc, bus, rw), md["H"], _band([md], "bottom", md["H"], proc, bus, rw),
                            proc.pitch, pd)
        put(md, lx + (col - md["W"]) / 2, gy0, (lx, lx + col),
            gy0 + md["H"] + tb - pd / 2 if md["top"] else None, gy0 - bb + pd / 2 if md["bottom"] else None)
    y = y0 + H / 2 + b["stack"] / 2
    b["lane_y"] = []
    for li, (lane, x) in enumerate(zip(b["lanes"], b["lg"])):
        ym = round(y - x["top"] - x["hmax"] / 2, 2)
        b["lane_y"].append(ym)
        lx = b["cS"] + b["fan"]
        for md, sw in zip(lane, x["slots"]):
            put(md, lx + (sw - md["W"]) / 2, ym - md["H"] / 2, (lx, lx + sw),
                y - pd / 2 if x["top"] else None, y - x["h"] + pd / 2 if x["bot"] else None)
            md["lane"] = li
            lx += sw + g
        y -= x["h"] + (b["gaps"][li] if li < len(b["gaps"]) else 0)


def _fan(x0: float, ys: list[float], xd: list[float], yd: list[float], sp: float, sgn: int) -> list[list]:
    """Orthogonal paths from one edge (x0, ys[i]) to (xd[i], yd[i]), turning on nested bus columns so none
    cross: of the routes going up, the highest start turns nearest the edge; of those going down, the lowest."""
    n = len(ys)
    up = sorted((i for i in range(n) if yd[i] > ys[i] + 1e-6), key=lambda i: -ys[i])
    down = sorted((i for i in range(n) if yd[i] < ys[i] - 1e-6), key=lambda i: ys[i])
    rank = {i: r for r, i in enumerate(up)}
    rank.update({i: r for r, i in enumerate(down)})
    out = []
    for i in range(n):
        if i not in rank:
            out.append([(R(x0), R(ys[i])), (R(xd[i]), R(yd[i]))])
        else:
            xb = x0 + sgn * sp * (rank[i] + 1)
            out.append([(R(x0), R(ys[i])), (R(xb), R(ys[i])), (R(xb), R(yd[i])), (R(xd[i]), R(yd[i]))])
    return out


def _route_block(b: dict, proc: Process, mw: float) -> list[tuple]:
    """Channels inside a block: lane chains, fan-out (split -> first module of each lane), fan-in (last module
    of each lane -> pool). -> [(from, to, path, kind, lane)]"""
    d, S, Pm, lanes, ty = b["dir"], b["split"], b["pool"], b["lanes"], b["lane_y"]
    n = len(lanes)
    out = []
    for li, lane in enumerate(lanes):
        for a, c in zip(lane, lane[1:]):
            xa = a["x0"] + a["W"] if d == 1 else a["x0"]
            xc = c["x0"] if d == 1 else c["x0"] + c["W"]
            out.append((a["id"], c["id"], [(R(xa), R(ty[li])), (R(xc), R(ty[li]))], "lane", li))

    def exits(md):
        lo, hi = md["y0"] + mw / 2 + proc.wall, md["y0"] + md["H"] - mw / 2 - proc.wall
        if all(lo - 0.02 <= t <= hi + 0.02 for t in ty):
            return list(ty)
        return [md["y0"] + md["H"] - md["H"] * (i + 1) / (n + 1) for i in range(n)]
    xs = S["x0"] + S["W"] if d == 1 else S["x0"]
    xl = [ln[0]["x0"] if d == 1 else ln[0]["x0"] + ln[0]["W"] for ln in lanes]
    for li, path in enumerate(_fan(xs, exits(S), xl, ty, b["sp"], d)):
        out.append((S["id"], lanes[li][0]["id"], path, "fan_out", li))
    xp = Pm["x0"] if d == 1 else Pm["x0"] + Pm["W"]
    xe = [ln[-1]["x0"] + ln[-1]["W"] if d == 1 else ln[-1]["x0"] for ln in lanes]
    for li, path in enumerate(_fan(xp, exits(Pm), xe, ty, b["sp"], -d)):
        out.append((lanes[li][-1]["id"], Pm["id"], list(reversed(path)), "fan_in", li))
    return out


def _ends(u: dict) -> tuple[dict, dict]:
    """(module the main path enters, module it leaves) of a unit."""
    return (u["split"], u["pool"]) if u.get("unit") == "block" else (u, u)


def _snap(v: float, limits: tuple, step: float = 0.5) -> float:
    """Round a board size up to a whole step (easier to redraw by hand), but never past a limit it fits under
    (either chip limit: the chip may be placed turned)."""
    up = math.ceil(v / step - 1e-9) * step
    under = [lim for lim in limits if lim and v <= lim + 1e-9 < up]
    return min(under) if under else up


def _arrange(units: list[dict], mods: list[dict], proc: Process, s: float, n_rows: int, rw: float, mw: float) -> dict:
    """Place units (library parts scaled by s) in n_rows rows. Rows snake (boustrophedon); with an even row count
    the last row runs left to right again (a carriage-return channel between the last two rows) so outlet_1
    stays on the right edge. Returns geometry (no channels)."""
    n = len(units)
    per = math.ceil(n / n_rows)
    rows = [units[i:i + per] for i in range(0, n, per)]
    pd, P, g, m = proc.port_d, proc.pitch, proc.gap, proc.margin
    side_col = m + pd / 2 + max(P / 2, pd / 2 + g + mw)
    bus = rw + max(proc.wall, rw)
    for md in mods:
        md["applied_scale"] = s if md["scalable"] else 1.0
        md["W"], md["H"] = md["w"] * md["applied_scale"], md["h"] * md["applied_scale"]
        md.pop("stretched", None)
    for u in units:
        if u.get("unit") == "block":
            _block_size(u, proc, rw, mw, bus)
    info = []
    for row in rows:
        slots = [_col(u, P) for u in row]
        hmax = max(u["H"] for u in row)
        tb, bb = _pad_bands(_band(row, "top", hmax, proc, bus, rw), hmax, _band(row, "bottom", hmax, proc, bus, rw), P, pd)
        info.append({"units": row, "slots": slots, "content": sum(slots) + g * (len(row) - 1), "hmax": hmax,
                     "top": tb, "bottom": bb})
    cr = len(rows) >= 2 and len(rows) % 2 == 0
    def facing(r, side):    # ports on that side of a row: its port band, or ports inside a lane block near that edge
        if r["top" if side == "top" else "bottom"]:
            return True
        for u in r["units"]:
            if u.get("unit") == "block":
                lane = u["lg"][0]["top"] if side == "top" else u["lg"][-1]["bot"]
                if lane or u["split"][side] or u["pool"][side]:
                    return True
        return False
    gaps = [max(g, P - pd) if facing(a, "bottom") and facing(c, "top") else g for a, c in zip(info, info[1:])]   # ports at pitch
    if cr:
        gaps[-1] = max(gaps[-1], mw + 2 * max(proc.wall, 0.4))
    content_w = max(r["content"] for r in info)
    heights = [r["top"] + r["hmax"] + r["bottom"] for r in info]
    fx = _snap(side_col * 2 + content_w, (proc.max_x, proc.max_y))
    fy = _snap(max(2 * m + sum(heights) + sum(gaps), 2 * m + pd + 2 * proc.wall), (proc.max_x, proc.max_y))
    spare = (fy - 2 * m - sum(heights) - sum(gaps)) / len(rows)
    y = fy - m
    cr_y = None
    for r_i, r in enumerate(info):
        r["y_mid"] = round(y - r["top"] - spare / 2 - r["hmax"] / 2, 2)
        r["port_top_y"] = round(y - pd / 2, 2) if r["top"] else None
        bottom_edge = y - r["top"] - spare - r["hmax"] - r["bottom"]
        r["port_bot_y"] = round(bottom_edge + pd / 2, 2) if r["bottom"] else None
        if r_i < len(gaps):
            if cr and r_i == len(rows) - 2:
                cr_y = round(bottom_edge - gaps[r_i] / 2, 2)
            y = bottom_edge - gaps[r_i]
        r["dir"] = 1 if (r_i % 2 == 0 or (cr and r_i == len(rows) - 1)) else -1
        x = side_col if r["dir"] == 1 else fx - side_col
        for u, sw in zip(r["units"], r["slots"]):
            x0 = x if r["dir"] == 1 else x - sw
            if u.get("unit") == "block":
                _place_block(u, x0 + (sw - u["W"]) / 2, r["y_mid"], r["dir"], r_i, proc, rw, bus)
            else:
                u["slot"] = (x0, x0 + sw)
                u["x0"] = round(x0 + (sw - u["W"]) / 2, 2)
                u["y0"] = r["y_mid"] - u["H"] / 2
                u["row"] = r_i
                u["port_top_y"], u["port_bot_y"] = r["port_top_y"], r["port_bot_y"]
            x = x0 + sw + g if r["dir"] == 1 else x0 - g
    return {"fx": fx, "fy": fy, "rows": info, "side_col": side_col, "bus": bus, "cr_y": cr_y}


SHAPES = [(1.0, False), (1.0, True), (2.0, False), (2.0, True), (0.5, False), (0.5, True), (3.0, True), (1 / 3, True),
          (4.0, True), (0.25, True), (6.0, True), (1 / 6, True), (8.0, True), (12.0, True)]


def _shape_options(mods: list[dict], X, Y, few: bool = False) -> list[tuple]:
    """(q, tight, rot) options to try, least change first."""
    if not X and not Y:
        return [(1.0, False, False)]
    reshapable = any(md.get("shape_kind") in ("area", "serpentine") for md in mods)
    shapes = (SHAPES[:2] + [(3.0, True), (1 / 3, True)] if few else SHAPES) if reshapable else SHAPES[:1]
    opts = [(q, t, False) for q, t in shapes]
    if any(md.get("shape_kind") == "library" and abs(md["native"][0] - md["native"][1]) > 1e-6 for md in mods):
        opts += [(q, t, True) for q, t in shapes]
    return opts


def _choose(units: list[dict], mods: list[dict], proc: Process, rw: float, mw: float) -> tuple[dict, float, list[dict]]:
    """Pick shapes, row count and library-part scale so the chip fits max_chip_mm (either way round).
    Order of resort: parametric parts reshaped (same volume / channel length, other aspect, tighter folds) and
    library parts turned 90 deg; then library parts scaled down (parts.allow_scaling). Returns (geometry, scale, findings)."""
    findings = []
    n = len(units)
    X, Y = proc.max_x, proc.max_y

    def fits(geo):
        if not X and not Y:
            return True
        straight = (not X or geo["fx"] <= X + 1e-6) and (not Y or geo["fy"] <= Y + 1e-6)
        turned = bool(X and Y) and geo["fx"] <= Y + 1e-6 and geo["fy"] <= X + 1e-6
        return straight or turned

    def over(geo):
        a = max(geo["fx"] / X if X else 0, geo["fy"] / Y if Y else 0)
        b = max(geo["fx"] / Y, geo["fy"] / X) if X and Y else a
        return min(a, b)

    def best_at(s, opts, us=units, ms=mods):
        fail = None
        for opt in opts:
            for md in ms:
                _shape(md, *opt)
            cands = []
            for k in range(1, len(us) + 1):
                geo = _arrange(us, ms, proc, s, k, rw, mw)
                if len(geo["rows"]) == k:
                    cands.append((geo, k))
            if not X and not Y:
                def squareness(gk):     # near square; an even row count needs a carriage-return channel, so it costs a little
                    geo, k = gk
                    return max(geo["fx"] / geo["fy"], geo["fy"] / geo["fx"]) * (1.3 if k % 2 == 0 else 1.0), k
                return _arrange(us, ms, proc, s, min(cands, key=squareness)[1], rw, mw), True, opt
            ok = [k for geo, k in cands if fits(geo)]
            if ok:
                odd = [k for k in ok if k % 2 == 1 and k <= min(ok) + 1]   # an odd row count needs no carriage return
                return _arrange(us, ms, proc, s, odd[0] if odd else min(ok), rw, mw), True, opt
            geo, k = min(cands, key=lambda gk: over(gk[0]))
            if fail is None or over(geo) < fail[0]:
                fail = (over(geo), opt, k)
        for md in ms:
            _shape(md, *fail[1])
        return _arrange(us, ms, proc, s, fail[2], rw, mw), False, fail[1]

    opts = _shape_options(mods, X, Y)
    geo, ok, opt = best_at(1.0, opts)
    scale = 1.0
    can_scale = proc.allow_scaling and any(md["scalable"] for md in mods)
    if not ok and can_scale:
        few = _shape_options(mods, X, Y, few=True)
        lo, hi = 0.02, 1.0
        geo_lo, ok_lo, _ = best_at(lo, few)
        if ok_lo:
            for _ in range(22):
                mid = (lo + hi) / 2
                lo, hi = (mid, hi) if best_at(mid, few)[1] else (lo, mid)
            scale = math.floor(lo * 1000) / 1000
        else:
            scale = lo
        geo, ok, opt = best_at(scale, few)
        names = ", ".join(md["id"] for md in mods if md["scalable"])
        findings.append(finding("warning", "parts.scaled",
                                f"library parts ({names}) were scaled to {scale:.3f} x their native size so the chip fits "
                                f"{X or '-'} x {Y or '-'} mm; their volumes shrink by {scale * scale:.3g} x."))
    if opt != (1.0, False, False):
        changed = [md["id"] for md in mods if md.get("shape_kind") in ("area", "serpentine")] if opt[:2] != (1.0, False) else []
        turned = [md["id"] for md in mods if md.get("rot")]
        how = ", ".join(([f"aspect x{opt[0]:.3g}"] if opt[0] != 1.0 else []) + (["tighter serpentine folds"] if opt[1] else []))
        what = ([f"reshaped parametric parts {', '.join(changed)} (same volume / channel length; {how})"] if changed else []) + \
               ([f"turned library parts {', '.join(turned)} by 90 deg"] if turned else [])
        if what:
            findings.append(finding("warning", "layout.reshaped", "to fit the chip limit, " + " and ".join(what) + "."))
    if not ok:
        # is one module too big on its own? then splitting the chip can't help
        too_big = []
        for md in mods:
            alone = {**md, "unit": "module"}
            alone_geo, alone_ok, _ = best_at(scale if md["scalable"] else 1.0, _shape_options([alone], X, Y), [alone], [alone])
            if not alone_ok:
                too_big.append((md, alone_geo))
        geo, ok, opt = best_at(scale, [opt])                  # restore the chosen arrangement
        for md, g in too_big:
            findings.append(finding("error", "module_too_large", _too_large_message(md, g, proc)))
        if not too_big:
            findings.append(finding("error", "layout.does_not_fit",
                                    f"the {len(mods)} modules need at least {geo['fx']:.1f} x {geo['fy']:.1f} mm (ports at "
                                    f"{proc.pitch:g} mm pitch, {proc.margin:g} mm edge margin), more than the {X or 'unlimited'} x "
                                    f"{Y or 'unlimited'} mm chip limit even with parametric parts reshaped"
                                    + (" and library parts scaled down" if can_scale else "")
                                    + ": move some modules, or their reagent inlets, to a second chip."))
    return geo, scale, findings


def _fit_note(fx: float, fy: float, X, Y) -> str:
    if not X or not Y:
        return "no size limit"
    if fx <= X + 1e-6 and fy <= Y + 1e-6:
        return f"fits the {X:g} x {Y:g} mm limit"
    if fx <= Y + 1e-6 and fy <= X + 1e-6:
        return f"fits rotated ({X:g} x {Y:g} mm limit)"
    return f"exceeds the {X:g} x {Y:g} mm limit"


def _too_large_message(md: dict, geo: dict, proc: Process) -> str:
    """Plain-English fix for one module that can't fit the chip limit even alone."""
    X, Y = proc.max_x, proc.max_y
    # room left for the module itself once margins, port bands and side columns are taken off (best orientation)
    room = max(((X or 1e9) - (geo["fx"] - md["W"])) * ((Y or 1e9) - (geo["fy"] - md["H"])),
               ((Y or 1e9) - (geo["fx"] - md["W"])) * ((X or 1e9) - (geo["fy"] - md["H"])) if X and Y else 0, 0)
    ratio = max(0.0, min(1.0, room / max(md["W"] * md["H"], 1e-9)))
    n_ports = len(md["top"]) + len(md["bottom"]) + 2
    head = (f"module '{md['id']}' ({md['part_name']}) can't fit a {X or '-'} x {Y or '-'} mm chip even on its own "
            f"(it needs at least {geo['fx']:.1f} x {geo['fy']:.1f} mm with its {n_ports} ports, after reshaping), so splitting "
            f"the chip won't help: ")
    params = md["plan"].get("params") or {}
    if md.get("shape_kind") == "area":
        fix = f"shrink it to about {md['volume_ul'] * ratio * 0.9:.4g} uL or less (now {md['volume_ul']:.4g} uL)"
    elif md.get("shape_kind") == "serpentine":
        key = "residence_s" if params.get("residence_s") or md["kind"] == "delay" else "length_mm"
        fix = (f"cut its {key.replace('_', ' ')} to about {ratio * 0.9:.0%} of the current value (or move the wait off-chip, "
               f"e.g. into tubing)")
    elif md["scalable"]:
        fix = "use a smaller library part"
    else:
        fix = "use a smaller part"
    return head + fix + ", use a different part, or raise envelope.max_chip_mm."


# --------------------------------------------------------------------------- routing


def _route_side(md: dict, side: str, port_y: float, geo: dict, proc: Process, width: float) -> list[dict]:
    """Ports on one edge of one module's slot + orthogonal paths port -> module edge (no crossings:
    ports sorted like their targets; dog-legs nest so none crosses another)."""
    items = md[side]
    if not items:
        return []
    k = len(items)
    P = proc.pitch
    x0s, x1s = md["slot"]
    base = round(x0s + (x1s - x0s - k * P) / 2 + P / 2, 2)
    px = [base + i * P for i in range(k)]
    mx0, mx1 = md["x0"], md["x0"] + md["W"]
    if all(mx0 + width / 2 + proc.wall <= x <= mx1 - width / 2 - proc.wall for x in px):
        tx = list(px)                                   # straight drops
    else:
        tx = [mx0 + md["W"] * (i + 1) / (k + 1) for i in range(k)]
    edge = md["y0"] + md["H"] if side == "top" else md["y0"]
    sgn = 1 if side == "top" else -1
    right = [i for i in range(k) if px[i] < tx[i] - 1e-6]
    left = [i for i in range(k) if px[i] > tx[i] + 1e-6]
    level = {i: rank for rank, i in enumerate(right)}
    level.update({i: rank for rank, i in enumerate(reversed(left))})
    out = []
    for i, (kind, name) in enumerate(items):
        p = (R(px[i]), R(port_y))
        if i in level:
            by = edge + sgn * geo["bus"] * (level[i] + 1)
            path = [p, (R(px[i]), R(by)), (R(tx[i]), R(by)), (R(tx[i]), R(edge))]
        else:
            path = [p, (R(tx[i]), R(edge))]
        out.append({"kind": kind, "name": name, "pos": p, "path": path})
    return out


# --------------------------------------------------------------------------- spec assembly


def _layers(proc: Process, depth: float, n_fluidic: int, valves: bool) -> tuple[list[dict], float]:
    """Layer stack for the process -> (layers bottom to top, z where the fluidic layer starts)."""
    mat = proc.material or "unspecified"
    low = mat.lower()
    norm = "resin" if "resin" in low else "pmma" if ("pmma" in low or "acryl" in low) else mat
    note_mat = f" ({mat})" if norm != mat else ""
    m = proc.method
    depth = R(depth)
    extra = []
    if m == "resin_print_hot_emboss":
        layers = [{"id": "substrate", "material": norm, "thickness_mm": 1.5,
                   "note": f"printed / embossed body{note_mat}; the fluidic relief is embossed into its top face"},
                  {"id": "fluidic", "material": norm, "thickness_mm": depth, "note": "embossed channels and chambers, one depth"}]
        lid = {"id": "seal", "material": "sealing film", "thickness_mm": 0.1, "note": f"sealed by {proc.sealing or 'film'}"}
    elif m == "soft_lithography_pdms":
        layers = [{"id": "glass", "material": "glass", "thickness_mm": 1.0, "note": "glass slide, plasma bonded"},
                  {"id": "fluidic", "material": "pdms", "thickness_mm": depth, "note": "PDMS flow layer (cast on a master)"}]
        lid = {"id": "pdms_body", "material": "pdms", "thickness_mm": 4.0, "note": "PDMS above the flow layer"}
        if valves:
            extra = [{"id": "membrane", "material": "pdms", "thickness_mm": 0.03, "note": "valve membrane"},
                     {"id": "control", "material": "pdms", "thickness_mm": 0.05, "note": "PDMS control layer (pneumatic valve lines)"}]
    elif m == "glass_laser_3d_etch":
        layers = [{"id": "glass_below", "material": norm, "thickness_mm": 1.0,
                   "note": "monolithic glass block: these layers are only z bookkeeping, nothing is bonded"},
                  {"id": "fluidic", "material": norm, "thickness_mm": depth, "note": "laser-written, etched-out channels"}]
        lid = {"id": "glass_above", "material": norm, "thickness_mm": 1.0, "note": "same block; port holes etched from the top"}
    elif m == "laser_cut_laminate":
        layers = [{"id": "base", "material": norm, "thickness_mm": 1.5, "note": f"bottom sheet{note_mat}"},
                  {"id": "fluidic", "material": norm, "thickness_mm": depth, "note": "spacer sheet with the channels cut through"}]
        lid = {"id": "lid", "material": norm, "thickness_mm": 1.5, "note": f"top sheet with port holes, {proc.sealing or 'bonded'}"}
    else:
        layers = [{"id": "base", "material": norm, "thickness_mm": 1.0, "note": f"{m}{note_mat}"},
                  {"id": "fluidic", "material": norm, "thickness_mm": depth, "note": "fluidic layer"}]
        lid = {"id": "lid", "material": norm, "thickness_mm": 1.0, "note": f"sealed by {proc.sealing or 'a lid'}"}
    for k in range(2, n_fluidic + 1):
        layers.append({"id": f"interlayer_{k - 1}", "material": layers[1]["material"], "thickness_mm": 0.2,
                       "note": f"between fluidic layers {k - 1} and {k}; vias at channel ends"})
        layers.append({"id": f"fluidic_{k}", "material": layers[1]["material"], "thickness_mm": depth,
                       "note": f"fluidic layer {k} (channels moved here to avoid crossings)"})
    layers += extra + [lid]
    return layers, layers[0]["thickness_mm"]


def _carries_for(mod: dict | None, steps: dict, which: str, default: str = "liquid") -> str:
    if mod is None:
        return default
    msteps = [steps[n] for n in mod.get("steps", []) if n in steps]
    if which == "out" and msteps and msteps[-1].get("output_material"):
        return _material_carries(msteps[-1]["output_material"])
    if which == "in" and msteps and msteps[0].get("input_material"):
        return _material_carries(msteps[0]["input_material"])
    return _material_carries(mod.get("holds")) if mod.get("holds") else default


def _reagent_role(name: str) -> str:
    n = name.lower()
    for key, role in (("oil", "oil"), ("cell", "cells"), ("medium", "medium"), ("media", "medium"), ("factor", "factor")):
        if key in n:
            return role
    return "reagent"


def _capsule_um(ir: dict | None) -> float | None:
    for m in (ir or {}).get("materials", []) or []:
        if m.get("kind") in ("capsules", "droplets") and m.get("size_um"):
            return float(m["size_um"])
    return None


PCR_DENATURE_C, PCR_ANNEAL_C, COLD_C = 95.0, 55.0, 4.0


def _module_temps(md: dict, steps: dict, chip_temp) -> tuple | None:
    """(min C, max C, thermal cycles) a module runs at, from its IR steps (temp_c; conditions.cold -> 4 C;
    conditions.thermal_cycles or an 'amplify' step -> cycling up to 95 C); None at room temperature."""
    lo, hi, cycles = None, None, 0
    for n in md["plan"].get("steps", []):
        st = steps.get(n) or {}
        cond = st.get("conditions") or {}
        ts = []
        if st.get("temp_c") is not None:
            ts.append(float(st["temp_c"]))
        if cond.get("cold") and st.get("temp_c") is None:
            ts.append(COLD_C)
        if cond.get("thermal_cycles") or (st.get("action") == "amplify" and st.get("temp_c") is None):
            cycles = max(cycles, int(cond.get("thermal_cycles") or 0)) or -1     # -1: cycling, count not given
            ts += [PCR_ANNEAL_C, PCR_DENATURE_C] + ([float(st["temp_c"])] if st.get("temp_c") is not None else [])
        ts = [t for t in ts if abs(t - ROOM_C) > 3]
        if ts:
            lo = min(ts + ([lo] if lo is not None else []))
            hi = max(ts + ([hi] if hi is not None else []))
    if lo is None and chip_temp is not None and abs(float(chip_temp) - ROOM_C) > 3:
        lo = hi = float(chip_temp)
    return (lo, hi, cycles) if lo is not None else None


def _lane_flow_findings(spec: dict, lane_info: list[dict]) -> list[dict]:
    """What the hydraulic solver (operator/network.py) says about each set of parallel lanes."""
    if not lane_info:
        return []
    try:
        import network
        net = network.Network(spec)
    except Exception:  # noqa: BLE001 - never let the check break layout
        return []
    out = []
    for info in lane_info:
        lanes = info["lanes"]
        try:
            sol = net.solve({"inlet_1": ("FEED", 1.0), "outlet_1": "VENT"})
            share = [sol["chambers"][ln[0]]["throughput_ul_min"] for ln in lanes]
            if min(share) > 0 and max(share) / min(share) > 1.25:
                out.append(finding("warning", "layout.lane_balance",
                                   f"the {len(lanes)} lanes after '{info['split']}' get unequal shares of the flow "
                                   f"({', '.join(f'{x:.0%}' for x in share)}) because their channels differ in length."))
            leaks = []
            for k, ln in enumerate(lanes):
                if not info["lane_inlets"][k]:
                    continue
                sol = net.solve({info["lane_inlets"][k][0]: ("FEED", 1.0), "outlet_1": "VENT"})
                leaks.append(sum(sol["chambers"][m]["throughput_ul_min"] for j, o in enumerate(lanes) if j != k for m in o))
        except network.NetworkError:
            continue
        if leaks and max(leaks) > 0.01:
            how = ("feed the lane's inlet and vent its own <lane>_vent_out port with outlet_1 capped: then no liquid enters "
                   "the other lanes" if any(info["lane_vents"]) else
                   "the chip has no ports left for a vent per lane, so fill the lanes before the split (or accept the mixing)")
            out.append(finding("warning", "layout.lane_isolation",
                               f"lanes {', '.join(ln[0] for ln in lanes)} share '{info['split']}' and '{info['pool']}': filling "
                               f"one lane through its own inlet with outlet_1 vented also pushes up to {max(leaks):.0%} of the "
                               f"flow through the other lanes. To fill one lane alone, {how}."))
    return out


def layout_chip(chip: dict, plan: dict, ir: dict | None, cons: dict, reg: dict) -> tuple[dict, list[dict], dict]:
    """One plan chip -> (spec, layout findings, extras for cad)."""
    proc = Process(cons)
    steps = _ir_steps(ir)
    findings: list[dict] = []
    cap_um = _capsule_um(ir) or 70.0
    chip_id = sid(chip["id"], "chip")
    used: set[str] = {"inlet_1", "outlet_1"}

    def uniq(s: str) -> str:
        base, k = s, 2
        while s in used:
            s = f"{base}_{k}"
            k += 1
        used.add(s)
        return s
    library_names = sorted(k for k, v in reg.items() if v.get("source") == "library")
    mods = []
    for m in chip["modules"]:
        mid = uniq(sid(m["id"], "m"))
        name = m["part"]
        part = reg.get(name)
        params = m.get("params") or {}
        if part is None:
            src = m.get("part_source") or ("parametric" if params else "library")
            guess = next((k for k in PARAMETRIC_FALLBACK if k.split("_")[0] in name.lower()), "chamber")
            if src == "library":
                findings.append(finding("error", "parts.unknown",
                                        f"module '{m['id']}' uses part '{name}', which is not in the parts library: pick one of "
                                        f"{', '.join(library_names) or 'the library parts'}, or a parametric part."))
            else:
                findings.append(finding("warning", "parts.unknown",
                                        f"parametric part '{name}' (module '{m['id']}') is not in the parts registry; drawn as a "
                                        f"parametric {guess.replace('_', ' ')}."))
            part = {"name": name, "source": "parametric", "stl": None, **PARAMETRIC_FALLBACK[guess]}
        if part.get("available") is False:
            findings.append(finding("error", "parts.unavailable",
                                    f"module '{m['id']}' uses part '{name}', which these constraints rule out "
                                    f"({part.get('why_unavailable') or 'not available'}): pick another part"
                                    + (f" (e.g. library part '{part['library_equivalent']}')" if part.get("library_equivalent")
                                       and part.get("source") == "parametric" else "") + " or move its steps off-chip."))
        elif part.get("source") == "parametric" and proc.parts_source == "library_only":
            findings.append(finding("error", "parts.source",
                                    f"module '{m['id']}' uses parametric part '{name}', but these constraints allow library parts "
                                    f"only: replace it with a library block or move its steps off-chip."))
        elif part.get("source") == "library" and proc.parts_source == "parametric_only":
            findings.append(finding("error", "parts.source",
                                    f"module '{m['id']}' uses library block '{name}', but this process ({proc.method}) makes "
                                    f"parametric parts only: use a parametric chamber, mixer, filter or delay line instead."))
        allowed = C.get(cons, "parts.allowed")
        if (allowed and name not in allowed) or name in (C.get(cons, "parts.forbidden") or []):
            findings.append(finding("error", "parts.allowed",
                                    f"part '{name}' (module '{m['id']}') is not allowed by these constraints: pick another part."))
        kind = _kind(name, part, params)
        mods.append({"id": mid, "plan": m, "part": part, "part_name": name, "kind": kind,
                     "inlets": [sid(x, "r") for x in m.get("inlets", [])], "outlets": [sid(x, "r") for x in m.get("outlets", [])]})

    carries = {md["id"]: _carries_for(md["plan"], steps, "out") for md in mods}
    first_in = _carries_for(mods[0]["plan"], steps, "in")
    width = {c: channel_width(c, proc, cap_um) for c in set(carries.values()) | {first_in, "liquid"}}
    reagent_w = width["liquid"]
    main_w = max(width.values())
    for md in mods:
        md.update(size_module(md["plan"], md["part"], md["kind"], proc, steps, main_w))
        if md["kind"] == "valve" and proc.valves_ok:
            md["control"] = f"{md['id']}_ctrl"
    _assign_port_sides(mods)
    units = _units(mods, findings)
    budget = C.get(cons, "ports.max_per_chip")
    n_ports = 2 + sum(len(md["top"]) + len(md["bottom"]) for md in mods)
    for u in units:
        if u.get("unit") == "block":
            u["vents"] = budget is None or n_ports + len(u["lanes"]) <= budget
            if u["vents"]:                 # a vent per lane: fill one lane (its inlet -> its vent) without touching the others
                for ln in u["lanes"]:
                    ln[-1]["bottom"].append(("outlet", f"{ln[-1]['id']}_vent"))
                n_ports += len(u["lanes"])
    geo, scale, more = _choose(units, mods, proc, reagent_w, main_w)
    too_big = {"layout.does_not_fit", "module_too_large"}
    vented = [u for u in units if u.get("vents")]
    if vented and any(v["rule"] in too_big for v in more):      # the lane vents are extras: drop them before giving up
        for u in vented:
            for ln in u["lanes"]:
                ln[-1]["bottom"] = [it for it in ln[-1]["bottom"] if it != ("outlet", f"{ln[-1]['id']}_vent")]
        geo2, scale2, more2 = _choose(units, mods, proc, reagent_w, main_w)
        if not any(v["rule"] in too_big for v in more2):
            geo, scale, more = geo2, scale2, more2
            for u in vented:
                u["vents"] = False
            more.append(finding("warning", "layout.lane_vents", "left out the per-lane vent ports so the chip fits its size "
                                                                "limit; lanes can only be filled together."))
        else:
            for u in vented:
                for ln in u["lanes"]:
                    ln[-1]["bottom"].append(("outlet", f"{ln[-1]['id']}_vent"))
            geo, scale, more = _choose(units, mods, proc, reagent_w, main_w)
    findings += more
    fx, fy = R(geo["fx"]), R(geo["fy"])

    # ---- ports + channels
    ports, channels = [], []
    pd = proc.port_d
    rows = geo["rows"]
    r0, rl = rows[0], rows[-1]
    in_x = proc.margin + pd / 2
    ins = chip.get("inputs") or []
    ports.append({"id": "inlet_1", "type": "inlet", "xy": (R(in_x), R(r0["y_mid"])), "role": "load",
                  "note": "main inlet" + (f": {ins[0].get('name')} from {ins[0].get('from')}" if ins else "")})
    out_x = fx - proc.margin - pd / 2 if rl["dir"] == 1 else in_x
    outs = chip.get("outputs") or []
    to = (outs[0].get("to") if outs else "external") or "external"
    ports.append({"id": "outlet_1", "type": "outlet", "xy": (R(out_x), R(rl["y_mid"])),
                  "role": "waste" if to == "waste" else "transfer" if "." in to else "harvest", "note": f"main outlet -> {to}"})
    if rl["dir"] == -1:
        findings.append(finding("warning", "layout.outlet_side",
                                "the module rows snake back, so outlet_1 is on the left edge of the bottom row, not the right."))

    def cid(a, b):
        return uniq(f"ch_{a}_{b}")

    def x_in(md, d):        # edge where the flow enters a module in a row running in direction d
        return md["x0"] if d == 1 else md["x0"] + md["W"]

    def x_out(md, d):
        return md["x0"] + md["W"] if d == 1 else md["x0"]
    chain = ["inlet_1"] + units + ["outlet_1"]
    xt_r, xt_l = fx - geo["side_col"] + proc.gap / 2, geo["side_col"] - proc.gap / 2
    for i, (a, b) in enumerate(zip(chain, chain[1:])):
        if isinstance(a, str):
            B = _ends(b)[0]
            rb = rows[b["row"]]
            path = [ports[0]["xy"], (R(x_in(B, rb["dir"])), R(rb["y_mid"]))]
            frm, too, carry = "inlet_1", B["id"], first_in
        elif isinstance(b, str):
            A = _ends(a)[1]
            ra = rows[a["row"]]
            path = [(R(x_out(A, ra["dir"])), R(ra["y_mid"])), ports[1]["xy"]]
            frm, too, carry = A["id"], "outlet_1", carries[A["id"]]
        else:
            A, B = _ends(a)[1], _ends(b)[0]
            ra, rb = rows[a["row"]], rows[b["row"]]
            xa, xb, ya, yb = R(x_out(A, ra["dir"])), R(x_in(B, rb["dir"])), R(ra["y_mid"]), R(rb["y_mid"])
            if a["row"] == b["row"]:
                path = [(xa, ya), (xb, yb)]
            elif ra["dir"] == 1 and rb["dir"] == 1:      # carriage return: right column, gap between rows, left column
                cy = R(geo["cr_y"])
                path = [(xa, ya), (R(xt_r), ya), (R(xt_r), cy), (R(xt_l), cy), (R(xt_l), yb), (xb, yb)]
            else:
                xt = R(xt_r if ra["dir"] == 1 else xt_l)
                path = [(xa, ya), (xt, ya), (xt, yb), (xb, yb)]
            frm, too, carry = A["id"], B["id"], carries[A["id"]]
        channels.append({"id": cid(frm, too), "from": frm, "to": too, "width_mm": width[carry], "path": path, "carries": carry,
                         "note": "main path"})
    lane_info = []
    for u in units:
        if u.get("unit") != "block":
            continue
        n_l = len(u["lanes"])
        info = {"split": u["split"]["id"], "pool": u["pool"]["id"], "lanes": [[m["id"] for m in ln] for ln in u["lanes"]],
                "fan_out": [], "fan_in": [], "lane_channels": [[] for _ in range(n_l)], "lane_inlets": [[] for _ in range(n_l)],
                "lane_vents": [[] for _ in range(n_l)]}
        for frm, too, path, kind, li in _route_block(u, proc, main_w):
            c = cid(frm, too)
            note = {"lane": f"lane {li + 1} of {n_l}", "fan_out": f"fan-out to lane {li + 1} of {n_l}",
                    "fan_in": f"fan-in from lane {li + 1} of {n_l}"}[kind]
            channels.append({"id": c, "from": frm, "to": too, "width_mm": width[carries[frm]], "path": path,
                             "carries": carries[frm], "note": note})
            (info["fan_out"] if kind == "fan_out" else info["fan_in"] if kind == "fan_in" else info["lane_channels"][li]).append(c)
        u["info"] = info
        lane_info.append(info)
    for md in mods:
        for side, py in (("top", md.get("port_top_y")), ("bottom", md.get("port_bot_y"))):
            for it in _route_side(md, side, py, geo, proc, reagent_w):
                if it["kind"] == "control":
                    ports.append({"id": uniq(it["name"]), "type": "control", "xy": it["pos"], "role": "control",
                                  "note": f"valve control line for {md['id']} (control layer, no fluid channel)"})
                elif it["kind"] == "inlet":
                    pid = uniq(f"{it['name']}_in")
                    ports.append({"id": pid, "type": "inlet", "xy": it["pos"], "role": _reagent_role(it["name"]),
                                  "note": f"reagent '{it['name']}' into {md['id']}"})
                    channels.append({"id": cid(pid, md["id"]), "from": pid, "to": md["id"], "width_mm": reagent_w,
                                     "path": it["path"], "carries": "oil" if "oil" in it["name"] else "liquid"})
                    if "lane" in md:
                        blk = next(i for i in lane_info if md["id"] in [m for ln in i["lanes"] for m in ln])
                        blk["lane_inlets"][md["lane"]].append(pid)
                else:
                    pid = uniq(f"{it['name']}_out")
                    harvest = any(k in it["name"] for k in ("harvest", "collect", "product"))
                    vent = "lane" in md and it["name"] == f"{md['id']}_vent"
                    ports.append({"id": pid, "type": "outlet", "xy": it["pos"], "role": "harvest" if harvest else "waste",
                                  "note": (f"vent of lane {md['lane'] + 1}: feed that lane's inlet and vent here (outlet_1 capped) "
                                           f"to fill the lane without flow through the other lanes") if vent
                                  else f"'{it['name']}' out of {md['id']}"})
                    if vent:
                        blk = next(i for i in lane_info if md["id"] in [m for ln in i["lanes"] for m in ln])
                        blk["lane_vents"][md["lane"]].append(pid)
                    channels.append({"id": cid(md["id"], pid), "from": md["id"], "to": pid, "width_mm": reagent_w,
                                     "path": list(reversed(it["path"])), "carries": "liquid"})

    # ---- crossings left over -> second fluidic layer when the process allows it (checks reports the rest)
    n_fluidic = 1
    two_layers = proc.multilayer and proc.crossings_ok and (proc.max_layers is None or proc.max_layers >= 2)
    tmp = {"channels": [{"id": c["id"], "from": c["from"], "to": c["to"], "path": [list(p) for p in c["path"]]} for c in channels]}
    for a, b, (x, y) in checks.crossings(tmp):
        if not two_layers:
            continue
        ch = next(c for c in channels if c["id"] == b)
        other = next(c for c in channels if c["id"] == a)
        if other.get("layer") == "fluidic_2" or ch.get("layer") == "fluidic_2":
            continue
        ch["layer"], n_fluidic = "fluidic_2", 2
        findings.append(finding("warning", "layout.second_layer",
                                f"channel '{b}' crosses '{a}' at ({x:g}, {y:g}); '{b}' runs on the second fluidic layer "
                                f"with vias at its ends."))

    # ---- depths, layers, chambers
    d_ch = proc.channel_depth
    d_layer = max([d_ch] + [md["d"] for md in mods])
    has_valves = any(p["type"] == "control" for p in ports)
    layers, z0 = _layers(proc, d_layer, n_fluidic, has_valves)
    top_z = R(sum(lay["thickness_mm"] for lay in layers))
    chambers = []
    for md in mods:
        s = md["applied_scale"]
        vol = min(md["volume_ul"] * s * s, md["W"] * md["H"] * md["d"])
        pm = md["plan"]
        note = (f"{md['part_name']} ({md['part'].get('source', 'library')}"
                + (f", scaled {s:.3f}" if md["scalable"] and s < 0.9999 else "")
                + f"); steps {', '.join(map(str, pm.get('steps', []))) or '-'}: {pm.get('does', '')}"
                + (" | " + "; ".join(md["notes"]) if md["notes"] else "")
                + (f" | serpentine: {md['fold'][0]} lanes of {md['fold'][1]:.3g} mm at {md['fold'][2]:.3g} mm pitch"
                   if md.get("fold") else "")
                + (" | rotated 90 deg" if md.get("rot") else "")
                + (" | valve part" if md["kind"] == "valve" else "")
                + (" | drawn as a manifold spanning its parallel lanes" if md.get("stretched") == "span" else
                   f" | stretched in y so its lane channels fit side by side" if md.get("stretched") else ""))
        c = {"id": md["id"], "type": md["type"],
             "center": [R(md["x0"] + md["W"] / 2), R(md["y0"] + md["H"] / 2), R(z0 + md["d"] / 2)],
             "size": [R(md["W"]), R(md["H"]), R(md["d"])], "volume_ul": R(max(vol, 1e-4)),
             "mesh_pore_um": pm.get("mesh_pore_um"), "note": note, "placeholder": True}
        if pm.get("mesh_pore_um"):
            c["retention"] = {"type": "mesh", "opening_um": pm["mesh_pore_um"], "source": "bought"}
        c.update(md["extra"])
        ret = c.get("retention")
        if ret and ret["type"] == "mesh" and not proc.multilayer:
            # one fluidic layer has nowhere to clamp a mesh floor: retain in-plane with a pillar row at the outlet
            c["retention"] = {"type": "pillars", "opening_um": ret["opening_um"], "source": "fabricated"}
            c["note"] += (f" | retention: a row of pillars with {ret['opening_um']:g} um gaps across the outlet (one fluidic "
                          f"layer: no mesh floor)")
        chambers.append(c)
    spec_ports = [{"id": p["id"], "type": p["type"], "position": [p["xy"][0], p["xy"][1], top_z], "role": p["role"],
                   "width_um": R(pd * 1000), "note": p["note"]} for p in ports]
    spec_channels = [{"id": ch["id"], "from": ch["from"], "to": ch["to"], "width_mm": ch["width_mm"], "depth_mm": d_ch,
                      "carries": ch["carries"], "path": [list(p) for p in ch["path"]],
                      "note": f"layer {ch.get('layer', 'fluidic')}" + (f"; {ch['note']}" if ch.get("note") else "")}
                     for ch in channels]
    valves = []
    for md in mods:
        if md.get("control"):
            port_ids = {p["id"] for p in spec_ports}
            inc = next(c for c in spec_channels if c["to"] == md["id"] and c["from"] not in port_ids)
            valves.append({"id": f"{md['id']}_valve", "type": "normally_open" if "open" in md["part_name"] else "normally_closed",
                           "kind": "isolation", "channel": inc["id"],
                           "position": [inc["path"][-1][0], inc["path"][-1][1], R(z0 + d_layer / 2)],
                           "control_port": md["control"], "placeholder": True})

    # ---- temperature: one thermal zone per temperature the modules run at (from their IR steps; the plan's chip
    # temperature for modules whose steps don't say), + a heat/cold zone feature over the same chambers
    zones, feats = [], []
    groups: dict[tuple, list[dict]] = {}
    for md in mods:
        prof = _module_temps(md, steps, chip.get("temp_c"))
        if prof:
            groups.setdefault(prof, []).append(md)
    for k, ((lo, hi, cycles), ms) in enumerate(sorted(groups.items(), key=lambda kv: (-kv[0][1], kv[0][0]))):
        hot = hi > ROOM_C
        caps = any(md["plan"].get("holds") == "capsules" for md in ms)
        purpose = ("pcr" if cycles else "culture" if abs(hi - 37) < 1 and caps and lo == hi
                   else "cold" if hi < ROOM_C else "heating")
        zid = uniq(f"{'pcr' if cycles else 'heat' if hot else 'cold'}_zone")
        zones.append({"id": zid, "chambers": [md["id"] for md in ms], "max_temp_c": hi, "min_temp_c": lo, "purpose": purpose})
        xs = [md["x0"] for md in ms] + [md["x0"] + md["W"] for md in ms]
        ys = [md["y0"] for md in ms] + [md["y0"] + md["H"] for md in ms]
        held = (f"thermal cycling {lo:g}-{hi:g} C ({cycles if cycles > 0 else 'placeholder number of'} cycles)" if cycles else
                f"held at {hi:g} C" if lo == hi else f"held at {lo:g}-{hi:g} C")
        feats.append({"id": uniq(f"{zid}_area"), "type": "heat_zone" if hot else "cold_zone",
                      "center": [R((min(xs) + max(xs)) / 2), R((min(ys) + max(ys)) / 2), R(top_z / 2)],
                      "size": [R(max(xs) - min(xs) + 1), R(max(ys) - min(ys) + 1), R(top_z)], "temp_c": hi,
                      "covers": [md["id"] for md in ms],
                      "note": f"{', '.join(md['id'] for md in ms)} {held} by off-chip equipment"})
    def unit_text(u):
        if isinstance(u, str):
            return u
        if u.get("unit") == "block":
            lanes = " | ".join(" -> ".join(m["id"] for m in ln) for ln in u["lanes"])
            return f"{u['split']['id']} -> [{len(u['lanes'])} parallel lanes: {lanes}] -> {u['pool']['id']}"
        return u["id"]
    flow_text = " -> ".join(unit_text(u) for u in chain)
    mod_map = {md["id"]: {"plan_id": md["plan"]["id"], "part": md["part_name"], "source": md["part"].get("source", "library"),
                          "kind": md["kind"], "steps": md["plan"].get("steps", []), "does": md["plan"].get("does", ""),
                          "scale": round(md["applied_scale"], 4), "inlets": md["inlets"], "outlets": md["outlets"],
                          **({"lane": md["lane"] + 1} if "lane" in md else {}),
                          **({"rotated_90": True} if md.get("rot") else {})}
               for md in mods}
    spec = {
        "id": chip_id,
        "name": chip.get("name", chip_id),
        "description": (f"{chip.get('purpose', '')} Generated by autochip (stage 4 layout) from chip plan "
                        f"'{plan.get('id', '')}' under constraints '{cons['id']}' ({proc.method}). Main path: "
                        f"{flow_text}. Modules -> protocol steps: "
                        + "; ".join(f"{k} = {v['part']} (steps {', '.join(map(str, v['steps'])) or '-'})" for k, v in mod_map.items())
                        + ". Placeholder geometry: a first layout, not a drawn design.").strip(),
        "placeholder": True,
        "units": "mm",
        "generator": {"script": SCRIPT, "params": {"plan_id": plan.get("id"), "chip": chip["id"], "constraints_id": cons["id"],
                                                   "fabrication_method": proc.method, "scale": scale, "modules": mod_map,
                                                   "fluidic_layers": n_fluidic, "port_diameter_mm": pd,
                                                   "port_pitch_mm": proc.pitch, "rows": len(rows),
                                                   "lanes": lane_info}},
        "footprint": {"x": fx, "y": fy, "note": f"{len(rows)} row(s); " + _fit_note(fx, fy, proc.max_x, proc.max_y)},
        "layers": layers,
        "chambers": chambers,
        "channels": spec_channels,
        "ports": spec_ports,
    }
    if valves:
        spec["valves"] = valves
    if feats:
        spec["features"] = feats
    if zones:
        spec["thermal_zones"] = zones
    if _capsule_um(ir):
        spec["capsule"] = {"diameter_um": _capsule_um(ir)}
    findings += _lane_flow_findings(spec, lane_info)
    return spec, findings, {"fluidic_z": z0, "scale": scale, "parts": {md["id"]: md["part"] for md in mods}}


# --------------------------------------------------------------------------- entry point


def run(plan, ir, constraints, parts, out_dir):
    out_dir = Path(out_dir)
    reg = registry(constraints, parts)
    result = {}
    current = {sid(c["id"], "chip") for c in plan["chips"]}
    if (out_dir / "chips").is_dir():
        for old in (out_dir / "chips").glob("*.json"):
            if old.stem not in current:      # a chip from an earlier run / partition round: runplan and --resume must not pick it up
                old.unlink()
    for chip in plan["chips"]:
        spec, findings, extra = layout_chip(chip, plan, ir, constraints, reg)
        seen, violations = set(), []
        own = {v["rule"] for v in findings}
        chk = [v for v in checks.check_chip(spec, constraints)
               if not ("module_too_large" in own and v["rule"] in ("envelope.max_chip_mm", "envelope.printable_area_mm",
                                                                    "module_too_large"))]
        for v in findings + chk:
            key = (v["severity"], v["rule"], v["message"])
            if key not in seen:
                seen.add(key)
                violations.append(v)
        spec_path = out_dir / "chips" / f"{spec['id']}.json"
        spec_path.parent.mkdir(parents=True, exist_ok=True)
        spec_path.write_text(json.dumps(spec, indent=2))
        files = {"spec": str(spec_path)}
        files.update(cad.export_chip(spec, out_dir / "cad" / spec["id"], extra, violations))
        result[chip["id"]] = {"spec": spec, "violations": violations, "files": files}
    return result

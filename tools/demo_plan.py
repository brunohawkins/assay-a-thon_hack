"""Derive the one-pump food-dye procedure for every demo-scale chip, and write docs/demo.md.

    python tools/demo_plan.py                 # the per-chip table
    python tools/demo_plan.py --write         # also regenerate docs/demo.md
    python tools/demo_plan.py --check         # fail if docs/demo.md is out of date
    python tools/demo_plan.py --json          # the plans as JSON

WHY THIS IS A SCRIPT AND NOT A DOCUMENT. The repo's only dye procedure,
cad/module1_dye_demo.md, was hand-written for a 140 x 75 mm laser-cut board with three
syringe pumps. The chip was rescaled to 30 x 20 mm and the document could not follow: its
fill times came out 6-8x too long and its first line had to say "Out of date". So nothing
here is typed in. Every port, flow, time and pressure is derived from the chip spec and
contracts/parameters.json, and docs/demo.md is this script's output - regenerate it, never
edit it.

WHAT THE DEMO HAS: printed and embossed chips at 30 x 20 mm, ONE syringe pump, food dye in
water. No cells, no reagents, no thermocycler, no incubator, no beads, no oil.

HOW THE FLOW IS CHOSEN. Dye has no shear limit. shear.culture_ceiling_dyn_cm2 is the
ADHERENT-CELL ceiling and capsule.escape_pressure_kpa is the capsule limit; neither applies
to coloured water on an empty chip, and holding a dye fill to the cell ceiling is exactly
what left spc_demo_v1's protocol at 0.071 uL/min - over an hour to fill 4.68 uL. The real
bounds are:
  (a) a human-visible fill: demo.target_fill_s, with its min/max as the window, applied to
      each chamber on the route from its own volume_ul;
  (b) the bonded seal: demo.max_drive_pressure_kpa, an ASSUMPTION placeholder, against the
      pressure operator/flow_calc.py predicts for the route at fluid.viscosity_pa_s (water
      at 20 C - this demo is exactly the case that value exists for).
The flow is the one that puts the most route chambers inside the window, breaking ties
towards filling the largest chamber in demo.target_fill_s, then capped by the pressure
limit and rounded to a value a pump can be dialled to.

ROUTE. One feed, one vent, every other port plugged. The route is the cheapest path (by
swept volume, so least dye and least waiting) from an inlet, through the chip's largest
chamber - the thing a camera can actually see - to an outlet. Channel centrelines come from
operator/network.py, which reads them from the DXF when there is one and otherwise cuts
them exactly as cad/make_train_cad.py does, so the drawing, the cut and these times cannot
disagree.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))

import flow_calc  # noqa: E402
import module1_operator  # noqa: E402
import network  # noqa: E402

DOC_PATH = ROOT / "docs" / "demo.md"
EPS = 1e-9

# Water at 20 C: fluid.viscosity_pa_s. The bench test is at room temperature, so this is
# the one calculation in the repo that must NOT use fluid.viscosity_37c_pa_s.
MU = flow_calc.VISCOSITY_WATER_20C_PA_S

# Chamber blocks that are a FOLDED CHANNEL, not a box: their "size" is a bounding box the
# liquid never fills (spc_demo_v1's delay_1 is 11 x 13 mm but holds 4.68 uL), so their
# resistance has to come from volume_ul and the feeding channel's cross-section instead.
FOLDED_BLOCKS = {"serpentine_channel"}

# Blocks that only work with two immiscible phases: no oil on the bench, so no demo.
TWO_PHASE_BLOCKS = {"flow_focusing", "flow_focusing_funnel", "phase_guide"}

# Blocks the drawing uses where separate streams are MEANT to meet. Only these are checked
# for needing more than one pump. Every chamber on a connected chip sits between two inlets
# in the graph sense, so testing all of them flagged all of them (module1_v1's n_cx as
# loudly as its j_mid, spc_demo_v1's four oil corners as loudly as its nozzle) and said
# nothing. A corner and a 1-to-2 split are not convergences.
CONVERGENCE_BLOCKS = {"cross_junction", "t_junction", "flow_focusing", "flow_focusing_funnel",
                      "herringbone_mixer", "curved_y_junction"}
# contracts/blocks.json calls y_junction "two-into-one OR one-into-two": a merge and a split
# share the block, so which one it is has to be read off the chip. It is a merge when two or
# more of its branches lead only to inlets.
AMBIGUOUS_MERGE_BLOCKS = {"y_junction"}

# Washing a chamber out is exponential if it mixes: 3 volumes takes it to ~5%.
WASHOUT_VOLUMES = math.log(20)


def _parameters() -> dict:
    return json.loads((ROOT / "contracts" / "parameters.json").read_text())["parameters"]


def _entry(name: str) -> dict:
    """One whole parameter entry, so min/max and 'assumption' travel with the value."""
    params = _parameters()
    if name not in params:
        raise KeyError(f"contracts/parameters.json has no {name}")
    return params[name]


FILL = _entry("demo.target_fill_s")
MAX_PRESSURE = _entry("demo.max_drive_pressure_kpa")
TARGET_S = float(FILL["value"])
MIN_S = float(FILL["min"])
MAX_S = float(FILL["max"])
PRESSURE_LIMIT_KPA = float(MAX_PRESSURE["value"])
AMBIENT_C = float(_entry("thermal.ambient_c")["value"])
DYE_D_M2_S = flow_calc.DYE_DIFFUSIVITY_M2_S
PORT_BORE_MM = network.PORT_HOLE_MM     # contracts/blocks.json fabrication_floors.min_port_mm

# The port hole a feed has to fill before anything is visible on the chip. Imported from
# operator/module1_operator.py rather than recomputed, so this file, the Python operator and
# twin/contracts.js portHoleUl() share ONE derivation (bore^2 x the chip's own lid). The
# twin found the Python side still using a 2 mm bore that no longer exists, which is what a
# second copy of a derivation always ends up doing.
port_hole_ul = module1_operator.port_hole_ul


# --- the chips ------------------------------------------------------------------------


def demo_chips() -> list[dict]:
    """Every chip drawn to the demo build envelope, by id.

    The filter is the spec's own "scale" field (contracts/schemas/chip_spec.schema.json),
    read through tools/check_chip_spec.py's rule rather than by comparing footprints: a
    target-scale board cannot be printed in the 30 x 20 mm envelope, so it cannot be on
    the bench, so it gets no dye plan.
    """
    chips = []
    for path in sorted((ROOT / "chip").glob("*.json")):
        chip = json.loads(path.read_text())
        if str(chip.get("scale") or "demo") == "demo":
            chip["_path"] = path.relative_to(ROOT).as_posix()
            chips.append(chip)
    return sorted(chips, key=lambda c: c["id"])


# --- geometry and resistance ----------------------------------------------------------


def _chamber_leg(chamber: dict, entry: tuple[float, float]) -> tuple[float, float]:
    """(length, width) in mm of the slab from an entry point on the outline to the centre.

    Same rule as operator/network.py._chamber_leg: which edge the stream came in through
    decides which way it crosses the chamber.
    """
    (cx, cy, _), (sx, sy, _) = chamber["center"], chamber["size"]
    on_x_edge = abs(abs(entry[0] - cx) - sx / 2) < 1e-3
    return (sx / 2, sy) if on_x_edge else (sy / 2, sx)


def chip_graph(chip: dict) -> dict:
    """Nodes (chambers and ports) and edges (channels) with length, volume and resistance."""
    geo = network.channel_geometry(chip)
    chambers = {c["id"]: c for c in chip["chambers"]}
    ports = {p["id"]: p for p in chip["ports"]}
    edges, adj = {}, {n: [] for n in list(chambers) + list(ports)}
    for ch in chip["channels"]:
        g = geo[ch["id"]]
        volume = g["length_mm"] * g["width_mm"] * g["depth_mm"]        # mm3 == uL
        edges[ch["id"]] = {
            "id": ch["id"], "from": ch["from"], "to": ch["to"], "block": ch.get("block"),
            "length_mm": g["length_mm"], "width_mm": g["width_mm"], "depth_mm": g["depth_mm"],
            "volume_ul": volume, "geometry": g["source"], "ends": {ch["from"]: g["a"], ch["to"]: g["b"]},
            "r_kpa_per_ul_min": flow_calc.channel_resistance(g["length_mm"], g["width_mm"], g["depth_mm"], MU),
        }
        adj[ch["from"]].append((ch["to"], ch["id"]))
        adj[ch["to"]].append((ch["from"], ch["id"]))
    return {"chambers": chambers, "ports": ports, "edges": edges, "adj": adj}


def chamber_resistance(chip_g: dict, chamber_id: str, channel_ids: list[str]) -> float:
    """kPa per uL/min to cross one chamber, entering and leaving by the given channels."""
    chamber = chip_g["chambers"][chamber_id]
    depth = chamber["size"][2]
    if chamber.get("block") in FOLDED_BLOCKS:
        # A folded channel: length from the volume it actually holds and the width of the
        # channel that feeds it. Treating the bounding box as a chamber understates this
        # by two orders of magnitude.
        width = min((chip_g["edges"][cid]["width_mm"] for cid in channel_ids), default=depth)
        length = chamber["volume_ul"] / (width * depth)
        return flow_calc.channel_resistance(length, width, depth, MU)
    total = 0.0
    for cid in channel_ids:
        entry = chip_g["edges"][cid]["ends"][chamber_id]
        leg, width = _chamber_leg(chamber, entry)
        total += flow_calc.channel_resistance(leg, width, depth, MU)
    return total


# --- route ----------------------------------------------------------------------------


def _cheapest_paths(chip_g: dict, start: str) -> dict[str, list[tuple[str, str]]]:
    """start -> {node: [(channel_id, node), ...]}, cheapest by swept volume.

    Cost of a step is the channel's own volume plus the volume of the chamber it enters:
    the cheapest route is the one that needs the least dye and the least waiting, which is
    what a demo wants. Dijkstra with a plain list: these graphs have tens of nodes.
    """
    cost = {start: 0.0}
    path: dict[str, list[tuple[str, str]]] = {start: []}
    seen: set[str] = set()
    while True:
        here = min((n for n in cost if n not in seen), key=lambda n: (cost[n], n), default=None)
        if here is None:
            break
        seen.add(here)
        for nxt, cid in sorted(chip_g["adj"][here], key=lambda x: (x[1], x[0])):
            step = chip_g["edges"][cid]["volume_ul"] + chip_g["chambers"].get(nxt, {}).get("volume_ul", 0.0)
            c = cost[here] + step
            if c < cost.get(nxt, math.inf) - EPS:
                cost[nxt], path[nxt] = c, path[here] + [(cid, nxt)]
    return path


def headline_chamber(chip: dict) -> dict:
    """The biggest chamber on the chip: the one a phone camera can actually resolve."""
    return max(chip["chambers"], key=lambda c: (c["volume_ul"], -chip["chambers"].index(c)))


def choose_route(chip: dict, chip_g: dict) -> dict | None:
    """One feed, one vent, the cheapest simple path between them through the headline chamber."""
    head = headline_chamber(chip)["id"]
    inlets = [p["id"] for p in chip["ports"] if p.get("type") == "inlet"]
    outlets = [p["id"] for p in chip["ports"] if p.get("type") == "outlet"]
    from_head = _cheapest_paths(chip_g, head)
    best = None
    for feed in inlets:
        to_head = _cheapest_paths(chip_g, feed).get(head)
        if to_head is None:
            continue
        for vent in outlets:
            onward = from_head.get(vent)
            if not onward:
                continue
            nodes = [feed] + [n for _, n in to_head] + [n for _, n in onward]
            if len(set(nodes)) != len(nodes):          # not a simple path: it would loop
                continue
            steps = to_head + onward
            cost = sum(chip_g["edges"][cid]["volume_ul"]
                       + chip_g["chambers"].get(n, {}).get("volume_ul", 0.0) for cid, n in steps)
            key = (round(cost, 9), len(steps), feed, vent)
            if best is None or key < best[0]:
                best = (key, {"feed": feed, "vent": vent, "head": head, "steps": steps,
                              "to_head": to_head, "swept_ul": cost})
    return best[1] if best else None


def route_resistance(chip_g: dict, steps: list[tuple[str, str]], feed: str) -> float:
    """kPa per uL/min of the whole series route: every channel plus every chamber crossed."""
    total = sum(chip_g["edges"][cid]["r_kpa_per_ul_min"] for cid, _ in steps)
    nodes = [feed] + [n for _, n in steps]
    for i, node in enumerate(nodes):
        if node not in chip_g["chambers"]:
            continue
        touching = [steps[i - 1][0]] + ([steps[i][0]] if i < len(steps) else [])
        total += chamber_resistance(chip_g, node, touching)
    return total


# --- flow -----------------------------------------------------------------------------


def _sig(value: float, digits: int, up: bool) -> float:
    """value rounded to `digits` significant figures, up or down."""
    if value <= 0:
        return value
    exp = math.floor(math.log10(value)) - (digits - 1)
    step = 10.0 ** exp
    out = (math.ceil(value / step - 1e-12) if up else math.floor(value / step + 1e-12)) * step
    return round(out, max(0, -exp) + 3)


def _settable(flow: float, lo: float, hi: float) -> float:
    """The flow rounded to something you can dial into a pump and still inside [lo, hi]."""
    for digits in (2, 3, 4, 6):
        inside = [c for c in (_sig(flow, digits, False), _sig(flow, digits, True))
                  if lo - EPS <= c <= hi + EPS and c > 0]
        if inside:
            return min(inside, key=lambda c: abs(math.log(c / flow)))
    return round(flow, 6)


def choose_flow(volumes: list[float], head_volume: float, resistance: float) -> dict:
    """The one flow for this chip, derived from the chamber volumes on its route.

    A chamber of volume V is filmable at flow q when its fill time 60 V / q is inside
    [demo.target_fill_s.min, .max], i.e. when q is in [60 V / max, 60 V / min].

    THE HEADLINE CHAMBER'S WINDOW COMES FIRST. Only inside it is the count of other filmable
    chambers maximised, and ties go to the flow that fills the headline closest to
    demo.target_fill_s. Without that precedence the rule optimised the wrong thing: on
    spc_demo_v1 two 0.013 uL junctions outvoted the 4.68 uL serpentine, and the "best" flow
    came out at 0.078 uL/min - an hour to fill the chip, which is the exact failure this
    whole file exists to stop.

    Finally the flow is capped by demo.max_drive_pressure_kpa and rounded to a value a pump
    can be dialled to.
    """
    windows = [(60 * v / MAX_S, 60 * v / MIN_S) for v in volumes]
    head_lo, head_hi = 60 * head_volume / MAX_S, 60 * head_volume / MIN_S
    q_pressure = PRESSURE_LIMIT_KPA / resistance if resistance > 0 else math.inf
    lo, hi = head_lo, min(head_hi, q_pressure)
    if lo > hi + EPS:                 # the seal limit is below a watchable fill: pressure wins
        return {"flow_ul_min": _settable(q_pressure, 0.0, q_pressure), "covered": [], "pressure_bound": True}
    q_target = min(max(60 * head_volume / TARGET_S, lo), hi)
    best = None
    for q in sorted({q_target, lo, hi} | {x for w in windows for x in w if lo - EPS <= x <= hi + EPS}):
        covered = [i for i, (a, b) in enumerate(windows) if a - EPS <= q <= b + EPS]
        key = (-len(covered), abs(math.log(60 * head_volume / q / TARGET_S)), q)
        if best is None or key < best[0]:
            best = (key, q, covered)
    _, q, covered = best
    lo = max([windows[i][0] for i in covered] + [lo])
    hi = min([windows[i][1] for i in covered] + [hi])
    flow = _settable(min(max(q_target, lo), hi), lo, hi)
    return {"flow_ul_min": flow, "covered": covered, "pressure_bound": False}


# --- what the chip cannot show --------------------------------------------------------


def _behind(chip_g: dict, chamber_id: str, channel_id: str) -> tuple[set[str], bool]:
    """({inlets}, reaches_an_outlet) through one channel of a chamber, not re-entering it."""
    e = chip_g["edges"][channel_id]
    start = e["to"] if e["from"] == chamber_id else e["from"]
    seen, stack, inlets, outlet = {chamber_id, start}, [start], set(), False
    while stack:
        node = stack.pop()
        kind = chip_g["ports"].get(node, {}).get("type")
        if kind == "inlet":
            inlets.add(node)
        elif kind == "outlet":
            outlet = True
        for nxt, _cid in chip_g["adj"][node]:
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return inlets, outlet


def multi_feed_nodes(chip_g: dict) -> list[dict]:
    """Convergence nodes whose design mode needs more than one inlet running at once.

    A node qualifies when its BLOCK says streams are meant to meet there
    (CONVERGENCE_BLOCKS), or when it is a y_junction used as a merge rather than a split -
    read off the chip by counting branches that lead only to inlets. Then it is reported if
    two or more of its branches between them reach two or more different inlets.
    """
    out = []
    for cid, chamber in chip_g["chambers"].items():
        block = chamber.get("block")
        branches = {}
        sources = 0
        for _nxt, chan in chip_g["adj"][cid]:
            inlets, reaches_outlet = _behind(chip_g, cid, chan)
            if inlets:
                branches[chan] = inlets
                sources += not reaches_outlet
        if block in AMBIGUOUS_MERGE_BLOCKS and sources < 2:
            continue                       # a 1-to-2 split, not a merge
        if block not in CONVERGENCE_BLOCKS and block not in AMBIGUOUS_MERGE_BLOCKS:
            continue
        feeds = sorted({i for s in branches.values() for i in s})
        if len(branches) >= 2 and len(feeds) >= 2:
            out.append({"chamber": cid, "feeds": feeds, "block": block})
    return out


def chip_protocols(chip_id: str) -> list[str]:
    """Protocol files run against this chip, from runs/ (ids do not always match: the
    ipsc_to_mgl_v1 protocol runs on ipsc_to_mgl_chip_v1), plus the same-name file."""
    found = set()
    by_id = {}
    for path in sorted((ROOT / "protocols").glob("*.json")):
        try:
            by_id[json.loads(path.read_text()).get("id")] = path
        except json.JSONDecodeError:
            continue
    for run in sorted((ROOT / "runs").glob("*.json")):
        if run.name == "index.json":
            continue
        try:
            data = json.loads(run.read_text())
        except json.JSONDecodeError:
            continue
        if data.get("chip_id") == chip_id and data.get("protocol_id") in by_id:
            found.add(by_id[data["protocol_id"]])
    if chip_id in by_id:
        found.add(by_id[chip_id])
    return sorted(p.relative_to(ROOT).as_posix() for p in found)


def protocol_temps(chip_id: str) -> tuple[list[float], list[str]]:
    """(hold temperatures away from room temperature, the protocols that ask for them)."""
    temps, where = set(), []
    for rel in chip_protocols(chip_id):
        steps = json.loads((ROOT / rel).read_text()).get("steps", [])
        got = {float(s["temp_c"]) for s in steps if s.get("temp_c") is not None}
        got = {t for t in got if abs(t - AMBIENT_C) > 1.0}
        if got:
            temps |= got
            where.append(rel)
    return sorted(temps), where


def cannot_show(chip: dict, chip_g: dict, route: dict | None) -> list[str]:
    """The honest list, derived from the spec rather than remembered."""
    out = []
    retention = [(c["id"], c["retention"]) for c in chip["chambers"] if c.get("retention")]
    if retention:
        kinds = sorted({r["type"] for _, r in retention})
        gaps = sorted({r["opening_um"] for _, r in retention})
        out.append(f"retention in {', '.join(cid for cid, _ in retention)}: a "
                   f"{'/'.join(kinds)} at {'/'.join(f'{g:g}' for g in gaps)} um retains NOTHING with no "
                   f"particles in the liquid. Dye passes it either way, so the demo says nothing about whether "
                   f"it would hold a capsule")
    two_phase = [c["id"] for c in chip["chambers"] if c.get("block") in TWO_PHASE_BLOCKS]
    if two_phase:
        out.append(f"anything two-phase at {', '.join(two_phase)}: droplets need oil and an aqueous stream at "
                   f"once, and there is no oil and one pump")
    # Nodes that need the same set of feeds are one finding, not one per node: module4_v1's
    # four spine nodes all wait on the same four inlets.
    grouped: dict[tuple[str, ...], list[str]] = {}
    for m in multi_feed_nodes(chip_g):
        grouped.setdefault(tuple(m["feeds"]), []).append(m["chamber"])
    for feeds, nodes in grouped.items():
        out.append(f"the streams meeting at {', '.join(nodes)}: they come from {len(feeds)} inlets "
                   f"({', '.join(feeds)}) and one pump drives one at a time, so they never meet at once")
    temps, where = protocol_temps(chip["id"])
    if temps:
        out.append(f"every thermal step {', '.join(where)} asks for "
                   f"({', '.join(f'{t:g}' for t in temps)} C): no thermocycler and no incubator on the bench")
    zones = [z["id"] for z in chip.get("thermal_zones", []) if z.get("max_temp_c", 0) > AMBIENT_C + 1]
    if zones and not temps:
        out.append(f"thermal zone(s) {', '.join(zones)}: nothing on the bench heats the chip")
    if route:
        off = [c["id"] for c in chip["chambers"] if c["id"] not in [n for _, n in route["steps"]]]
        if off:
            out.append(f"{', '.join(off)}: not on the one-pump route, so "
                       f"{'it stays' if len(off) == 1 else 'they stay'} empty (a second run with a "
                       f"different vent, or a second pump, would reach "
                       f"{'it' if len(off) == 1 else 'them'})")
    return out


# --- the plan -------------------------------------------------------------------------


def plan(chip: dict) -> dict:
    """The whole one-pump dye procedure for one chip, derived from its spec."""
    chip_g = chip_graph(chip)
    route = choose_route(chip, chip_g)
    out = {
        "chip_id": chip["id"], "spec": chip.get("_path", f"chip/{chip['id']}.json"),
        "name": chip.get("name", chip["id"]), "placeholder": bool(chip.get("placeholder")),
        "viscosity_pa_s": MU, "pressure_limit_kpa": PRESSURE_LIMIT_KPA,
        "window_s": [MIN_S, MAX_S], "target_fill_s": TARGET_S,
    }
    if route is None:
        out.update({"feed": None, "vent": None, "plug": [p["id"] for p in chip["ports"]],
                    "flow_ul_min": None, "pressure_kpa": None, "watch": [], "sequence": [],
                    "alternatives": [], "cannot": cannot_show(chip, chip_g, None),
                    "note": "no inlet reaches the largest chamber and an outlet without looping: nothing to film"})
        return out

    resistance = route_resistance(chip_g, route["steps"], route["feed"])
    on_route = [n for _, n in route["steps"] if n in chip_g["chambers"]]
    volumes = [chip_g["chambers"][n]["volume_ul"] for n in on_route]
    head_v = chip_g["chambers"][route["head"]]["volume_ul"]
    flow = choose_flow(volumes, head_v, resistance)["flow_ul_min"]

    # The feed port's own hole fills before anything shows on the chip. Derived with
    # module1_operator.port_hole_ul, the same rule the twin uses, so the two agree: the old
    # Python literal assumed a 2 mm bore (12 uL) and stretched every time threefold.
    hole_ul = port_hole_ul(chip)
    sequence = [{"kind": "port", "id": route["feed"], "volume_ul": round(hole_ul, 4),
                 "starts_s": 0.0, "fill_s": round(60 * hole_ul / flow, 1),
                 "ends_s": round(60 * hole_ul / flow, 1), "filmable": False}]
    cumulative = 60 * hole_ul / flow
    for cid, node in route["steps"]:
        for kind, nid, vol in (("channel", cid, chip_g["edges"][cid]["volume_ul"]),
                               ("chamber", node, chip_g["chambers"].get(node, {}).get("volume_ul"))):
            if vol is None:
                continue
            fill_s = 60 * vol / flow
            sequence.append({"kind": kind, "id": nid, "volume_ul": round(vol, 4),
                             "starts_s": round(cumulative, 1), "fill_s": round(fill_s, 1),
                             "ends_s": round(cumulative + fill_s, 1),
                             "filmable": kind == "chamber" and MIN_S - EPS <= fill_s <= MAX_S + EPS})
            cumulative += fill_s
    watch = [s for s in sequence if s["filmable"]]

    alternatives = []
    from_head = _cheapest_paths(chip_g, route["head"])
    for vent in sorted(p["id"] for p in chip["ports"] if p.get("type") == "outlet"):
        if vent == route["vent"] or vent not in from_head:
            continue
        onward = from_head[vent]
        if any(n in [x for _, x in route["to_head"]] + [route["feed"]] for _, n in onward):
            continue
        extra = [n for _, n in onward if n in chip_g["chambers"]]
        if not extra:
            continue      # a second drain off the same chamber shows nothing new
        vol = sum(chip_g["edges"][cid]["volume_ul"] + chip_g["chambers"].get(n, {}).get("volume_ul", 0.0)
                  for cid, n in onward)
        alternatives.append({"vent": vent, "extra_chambers": extra, "swept_ul": round(vol, 3),
                             "reach_s": round(60 * vol / flow, 1)})
    alternatives.sort(key=lambda a: a["swept_ul"])
    others = sorted(p["id"] for p in chip["ports"]
                    if p.get("type") == "inlet" and p["id"] != route["feed"])

    out.update({
        "feed": route["feed"], "vent": route["vent"], "headline": route["head"],
        "plug": sorted(p["id"] for p in chip["ports"] if p["id"] not in (route["feed"], route["vent"])),
        "flow_ul_min": flow, "resistance_kpa_per_ul_min": resistance,
        "pressure_kpa": flow * resistance, "port_hole_ul": round(hole_ul, 4),
        "swept_ul": round(route["swept_ul"] + hole_ul, 3),
        "total_s": round(sum(s["fill_s"] for s in sequence), 1),
        "reaches_headline_s": round(next(s["starts_s"] for s in sequence if s["id"] == route["head"]), 1),
        # Switch the syringe to clear water and the headline chamber empties exponentially,
        # not linearly: one volume per 1/e, about three to look clear. A well-mixed chamber
        # is the APPROXIMATION here - a real chamber short-circuits and keeps corners longer.
        "washout_tau_s": round(60 * head_v / flow, 1),
        "washout_s": round(WASHOUT_VOLUMES * 60 * head_v / flow, 1),
        "sequence": sequence, "watch": watch, "alternatives": alternatives,
        "other_inlets": others, "cannot": cannot_show(chip, chip_g, route),
    })
    return out


def diffusion_spread_mm(seconds: float) -> float:
    """How far dye spreads with the pump off: sqrt(2 D t), D = dye.diffusivity_m2_s."""
    return math.sqrt(2 * DYE_D_M2_S * seconds) * 1000


def plans() -> list[dict]:
    return [plan(c) for c in demo_chips()]


# --- recommendation -------------------------------------------------------------------


def pipeline_chips() -> set[str]:
    """Chip ids the MVP pipeline actually uses, so a spare spec is not mistaken for a module."""
    path = ROOT / "protocols" / "pipeline_mvp.json"
    if not path.exists():
        return set()
    out = set()
    for module in json.loads(path.read_text()).get("modules", []):
        spec = module.get("chip")
        if spec and (ROOT / spec).exists():
            out.add(json.loads((ROOT / spec).read_text())["id"])
    return out


def verdict(p: dict, in_pipeline: set[str]) -> tuple[str, str]:
    """(print / skip, why) for one chip. A recommendation only: nobody here flips
    built_for_demo in protocols/pipeline_mvp.json - only the owner knows what is on the
    print bed."""
    if not p["watch"]:
        return "skip", "nothing on its route fills inside the window, so there is nothing to film"
    if p["placeholder"] and p["chip_id"] not in in_pipeline:
        return "skip", "placeholder spec, and no pipeline module uses it"
    n = len(p["watch"])
    times = " and ".join(f"{w['fill_s']:.0f} s" for w in p["watch"])
    bits = [f"{n} chamber fills in {times}" if n == 1 else f"{n} chambers fill in {times}"]
    bits.append("an MVP pipeline module" if p["chip_id"] in in_pipeline else "not a pipeline module")
    if p["alternatives"]:
        vents = ", ".join("`" + a["vent"] + "`" for a in p["alternatives"][:2])
        bits.append(f"and moving the vent to {vents} reroutes the dye, which is the design's actual claim")
    return "print", "; ".join(bits)


def rank_key(p: dict, in_pipeline: set[str]) -> tuple:
    """Print order. In this order: it films at all; it is a module the pipeline uses; more
    filmable chambers; it can show routing by port (a second vent that reaches a new
    chamber), which is the one claim these valve-free chips actually make; more visible
    liquid; then the id, so the order never depends on dict order."""
    return (verdict(p, in_pipeline)[0] != "print",
            p["chip_id"] not in in_pipeline,
            -len(p["watch"]),
            -len(p["alternatives"]),
            -sum(w["volume_ul"] for w in p["watch"]),
            p["chip_id"])


# --- output ---------------------------------------------------------------------------


def _ports(ids: list[str]) -> str:
    return ", ".join(f"`{i}`" for i in ids) if ids else "nothing (every port is in use)"


def _secs(value: float) -> str:
    """Seconds at a precision that does not pretend: 0.7 s, not 1 s; 36 s, not 35.98 s."""
    return f"{value:.1f}" if value < 10 else f"{value:.0f}"


def _percent(part: float, whole: float) -> str:
    frac = part / whole
    return "<0.1%" if frac < 0.001 else f"{frac:.1%}"


def table(ps: list[dict]) -> str:
    rows = [("chip", "feed", "vent", "uL/min", "headline fill", "filmable", "kPa"),
            ("---", "---", "---", "---:", "---:", "---:", "---:")]
    for p in ps:
        if p["flow_ul_min"] is None:
            rows.append((p["chip_id"], "-", "-", "-", "-", "0", "-"))
            continue
        head = next((s for s in p["sequence"] if s["id"] == p["headline"]), None)
        rows.append((p["chip_id"], p["feed"], p["vent"], f"{p['flow_ul_min']:g}",
                     f"{head['fill_s']:.0f} s" if head else "-",
                     str(len(p["watch"])), f"{p['pressure_kpa']:.3f}"))
    width = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("| " + " | ".join(c.ljust(width[i]) for i, c in enumerate(r)) + " |" for r in rows)


def _chip_section(p: dict) -> list[str]:
    """One chip in about eight lines. The owner asks for short documents, so the fill order
    is one arrow chain rather than a table per chip."""
    if p["flow_ul_min"] is None:
        return [f"### `{p['chip_id']}`", "", f"No one-pump route: {p['note']}.", ""]
    chain = []
    for s in p["sequence"]:
        mark = "**" if s["filmable"] else ""
        label = f"`{s['id']}` hole" if s["kind"] == "port" else f"`{s['id']}`"
        chain.append(f"{mark}{label} {_secs(s['fill_s'])} s{mark}")
    out = [f"### `{p['chip_id']}` - feed `{p['feed']}` at {p['flow_ul_min']:g} uL/min, "
           f"vent `{p['vent']}`, plug {_ports(p['plug'])}", "",
           " -> ".join(chain) + f" -> out of `{p['vent']}` at {p['total_s']:.0f} s.", "",
           f"{p['swept_ul']:g} uL in all ({p['port_hole_ul']:g} uL of it lost in the feed port before "
           f"anything shows). Drive pressure {p['pressure_kpa']:.3f} kPa, "
           f"{_percent(p['pressure_kpa'], p['pressure_limit_kpa'])} of the assumed limit. "
           f"Clear-water wash-out of `{p['headline']}`: 1/e every {p['washout_tau_s']:.0f} s, "
           f"clear by about {p['washout_s']:.0f} s (well-mixed chamber, APPROXIMATE)."]
    extras = []
    if p["alternatives"]:
        extras.append("Other vents, same flow: " + "; ".join(
            f"`{a['vent']}` carries the dye on into "
            f"{', '.join(f'`{c}`' for c in a['extra_chambers'])} (+{_secs(a['reach_s'])} s)"
            for a in p["alternatives"][:2]) + ".")
    if p["other_inlets"]:
        extras.append(f"Repeat from {_ports(p['other_inlets'])} one at a time for the rest of the chip.")
    if extras:
        out += ["", " ".join(extras)]
    out += ["", "**Cannot show:** " + ("; ".join(p["cannot"]) + "." if p["cannot"]
                                       else "nothing beyond the general list below."), ""]
    return out


HEADER = """# The dye demo

**Generated file. Do not edit.** `python tools/demo_plan.py --write` rebuilds it from the
chip specs and `contracts/parameters.json`. Hand-written timings are exactly how the old
procedure (`cad/module1_dye_demo.md`) went stale when the boards were rescaled.

## What is on the bench

Printed and hot-embossed chips at 30 x 20 mm, **one** syringe pump, food dye in water, and
plugs for every port that is not the feed or the vent. No cells, no reagents, no
thermocycler, no incubator, no beads, no oil.

## How the flow was chosen

Dye has no shear limit. `shear.culture_ceiling_dyn_cm2` is the **adherent-cell** ceiling and
`capsule.escape_pressure_kpa` is the **capsule** limit; neither applies to coloured water in
an empty chip. Holding a dye fill to the cell ceiling is what left `spc_demo_v1`'s protocol
at 0.071 uL/min - over an hour to fill 4.68 uL, which nobody could film. The two bounds that
do apply are a fill a human can watch (`demo.target_fill_s`, {min:g}-{max:g} s per chamber,
aiming for {target:g} s) and the bonded seal (`demo.max_drive_pressure_kpa` = {limit:g} kPa,
an **assumption**, see below). Pressures are `operator/flow_calc.py` at
`fluid.viscosity_pa_s` = {mu:g} Pa s, water at 20 C, which is the one case that value is for.

Volumes are the specs' own `volume_ul` and channel centrelines from `operator/network.py`
(the DXF where there is one, otherwise cut exactly as `cad/make_train_cad.py` cuts it). The
port hole each feed has to fill first is `operator/module1_operator.py port_hole_ul`: the
contract bore `fabrication_floors.min_port_mm` = {bore:g} mm squared, through that chip's own
lid layer. The twin computes it the same way, from the same function's rule, so the bench
sheet and the twin cannot disagree.

Every number below is derived. Nothing is typed in.
"""

PRESSURE_NOTE = """## The pressure limit is an assumption

`demo.max_drive_pressure_kpa` = {limit:g} kPa is a **deliberately conservative placeholder,
not a measurement**. The contracts held no bond-strength value because nobody has burst a
real chip. Until someone pressurises one to failure and writes the number down, treat this
as a ceiling to stay well inside, never as any chip's strength. The worst route above
({worst_chip}) sits at {worst:g} kPa, {worst_pct} of the limit, so the limit is not what sets
any flow here - the visible-fill window is. That also means the limit could be badly wrong in
either direction without changing a single number above, which is the honest reason to measure it.
"""

NOT_PROVEN = """## What this demo does not prove

Food dye in water through an empty chip. It shows that the liquid goes where the drawing
says and that the chip is sealed enough to run at all. It proves **none** of the following,
and no number from the demo should be quoted for any of them:

- **No cell biology.** No cells are loaded. Nothing about attachment, viability,
  differentiation, lineage identity, conditioned medium or any cell response.
- **No thermal step.** Nothing heats or cools the chip: no 37 C culture, no 4 C hold,
  no 42 C reverse transcription, no 98 C index PCR, no ramp rate.
- **No reagent compatibility.** Water and food dye only. Nothing about the chaotropic lysis
  buffer, enzymes, oil, photoinitiator or solvents against the resin, the film or the
  adhesive - and `contracts/materials.json` lists those as TODO for a reason.
- **No weir capture at the real gap.** With no particles in the liquid, a 25 um weir retains
  nothing by definition. The demo cannot distinguish a correctly built weir from a missing one.
- **No weeks-long sealing.** A bond that holds for a ten-minute dye run says nothing about
  evaporation, creep, delamination or sterility over the weeks of culture the design assumes.
- **No DNA adsorption.** Nucleic acid losses to the wetted surfaces are untested and
  untestable without nucleic acid.
- **No droplets.** Flow focusing needs oil plus two aqueous streams at once. One pump and no
  oil means no droplet is formed, so nothing about size, monodispersity or core-shell structure.
- **No single-cell barcoding.** No capsules, no emulsion, no barcode pool. Nothing about
  collision rate, Poisson loading or demultiplexing.
- **No CFD validation.** The times above come from one-dimensional volume/flow arithmetic
  and the rectangular-channel resistance formula. Comparing them with a stopwatch checks the
  geometry and the pump; it does not validate any flow simulation, and the measurement should
  be recorded as a measurement, not folded into the prediction.
"""


def risks_section() -> str:
    return f"""## Day-of risks

| Risk | What it looks like | Mitigation |
|---|---|---|
| Connecting tubing to a {PORT_BORE_MM:g} mm port | The bore is `fabrication_floors.min_port_mm` = {PORT_BORE_MM:g} mm. Push-fit tubing either will not enter or will not seal, and forcing it splits the printed boss. | Go in with a blunt dispensing needle rather than bare tubing: the published gauge dimensions put 21G at about 0.82 mm outside diameter and 20G at about 0.91 mm, so 21G is the one that enters a {PORT_BORE_MM:g} mm hole and 20G is an interference fit. Seat it with a drop of adhesive or a short silicone sleeve, test every joint in a scrap chip first, and never push against a plugged port. |
| Tubing dead volume against an ~11 uL chip | A metre of fine tubing holds many times the whole chip. Dye spends minutes in the line while the chamber stays clear, and the demo looks broken. | Keep the lines as short as they can physically be, prefill them with dye to the port face before connecting, and start the stopwatch at the port, not at the pump. The times in this file begin at the port and exclude all tubing. |
| Priming and bubbles | One trapped bubble in a 0.2 mm x 0.3 mm channel stops the flow, splits the stream or parks in the chamber and sits there as a white blob on camera. | Prime the whole chip with plain water first and confirm every chamber is clear before any dye. Fill syringes nose-up and tap them out. Introduce dye only into an already-wet chip. If a bubble lodges, raise the flow briefly rather than rapping the chip. |
| Unused ports not plugged | An open port is a vent. The dye leaves by the nearest hole, the chamber never fills, and it looks like a design fault. A switched-off pump on an open line is also a vent, not a plug. | Plug every port in the plan's plug list, physically, before starting. Count the plugs against the list out loud. A stopped pump still needs its port plugged. |
| Dye staining the resin | Food dye in a printed part can tint the walls, so the "after" state never clears and a second run on the same chip reads wrong. | Treat the first dye run on a chip as the only trustworthy one. Keep one chip per chamber state, run clear water last, and do not try to reuse a stained chip for the clean-fill shot. |
| Hazy printed resin hiding the dye | `contracts/materials.json` says printed resin's clear grades are "translucent to slightly hazy and yellow with age; not a reliable optical window". Front-lit, a 0.3 mm deep channel of dye is nearly invisible. | **Backlight it.** Put a diffuse white light or a phone torch through paper behind the chip and shoot the transmitted image. Pick a dye colour with contrast against the resin's yellow (blue or green beats yellow or red). |
| 30 x 20 mm is too small to see | The audience sees a thumbnail-sized blank rectangle; the whole chip is smaller than a credit card and the chambers are a few millimetres. | Phone on a stand, macro mode, locked focus and exposure, chip taped down so nothing drifts. Put a ruler in the shot for scale, and frame one chamber rather than the whole board. Record, do not rely on anyone leaning in. |
| One chip, one chance | A split boss, a leak, a stained chamber or a blocked channel ends the demo if it is the only chip. | Print spares - at least two of each chip you intend to show - and have a second syringe, a second set of plugs and pre-cut spare tubing on the table. Run every chip through a water prime the day before, not on the day. |
"""


def recommendation(ps: list[dict]) -> str:
    in_pipeline = pipeline_chips()
    lines = ["## Which chips to print", "",
             "A recommendation only. `built_for_demo` in `protocols/pipeline_mvp.json` is left",
             "untouched: only the owner knows what is actually on the print bed. Order is",
             "films-at-all, then pipeline module, then filmable chambers, then whether moving",
             "the vent reroutes the dye.", "",
             "| chip | verdict | why |", "|---|---|---|"]
    ranked = sorted(ps, key=lambda p: rank_key(p, in_pipeline))
    for p in ranked:
        v, why = verdict(p, in_pipeline)
        lines.append(f"| `{p['chip_id']}` | {v} | {why} |")
    picks = [p for p in ranked if verdict(p, in_pipeline)[0] == "print"][:3]
    names = ", ".join(f"`{p['chip_id']}`" for p in picks)
    lines += ["", f"**If only a few can be printed, print {names}**, two of each. Between them they show the three "
                  "things a dye test can honestly show: a chamber filling in a readable time, liquid arriving in a "
                  "second chamber in the right order, and the route changing when you move the plug to a different "
                  "port - which on a valve-free chip is the whole design claim. Everything else on the list is a "
                  "single chamber filling, which the first chip already demonstrates.", ""]
    return "\n".join(lines)


def diffusion_section() -> str:
    rows = [(label, seconds) for label, seconds in
            (("1 min", 60), ("10 min", 600), ("1 hour", 3600), ("overnight (12 h)", 43200))]
    lines = ["## Pump off: diffusion", "",
             "Stop the pump, plug every port, and the only thing left is diffusion. The front "
             f"spreads as sqrt(2 D t) with D = `dye.diffusivity_m2_s` = {DYE_D_M2_S:g} m2/s "
             "(itself a TODO placeholder, so treat these as an order of magnitude):", ""]
    lines += [f"- {label}: about {diffusion_spread_mm(s):.2f} mm" for label, s in rows]
    lines += ["", "So within a demo you see a sharp dye interface at a chamber mouth go soft by a "
                  "fraction of a millimetre: real, filmable under macro, and nothing more. The old "
                  "procedure's \"8.8 mm in 12 hours\" is "
                  f"{8.8 / diffusion_spread_mm(43200):.1f}x this estimate - the same order, so that "
                  "one figure survived the rescale. It is still an overnight claim, not something to "
                  "show on the day.", ""]
    return "\n".join(lines)


def render_doc(ps: list[dict]) -> str:
    parts = [HEADER.format(min=MIN_S, max=MAX_S, target=TARGET_S, limit=PRESSURE_LIMIT_KPA,
                           mu=MU, bore=PORT_BORE_MM),
             "## Every chip at a glance", "", table(ps), "",
             "`headline fill` is the largest chamber on the route; `filmable` counts the chambers whose "
             f"fill time lands inside the {MIN_S:g}-{MAX_S:g} s window.", "",
             "## Chip by chip", "",
             "One feed, one vent, everything else plugged. Each chain below is the order the dye "
             "arrives in, with how long that part takes to colour. **Bold is what a viewer can read; "
             "the rest goes by too fast to film.** The clock starts when dye reaches the port, and the "
             "fast centre of the stream shows about a third sooner than the figure given. None of these "
             "are stopwatch readings - write those down separately and compare.", ""]
    for p in ps:
        parts += _chip_section(p)
    worst = max((p for p in ps if p["pressure_kpa"] is not None), key=lambda p: p["pressure_kpa"])
    parts += [diffusion_section(),
              PRESSURE_NOTE.format(limit=PRESSURE_LIMIT_KPA, worst_chip=f"`{worst['chip_id']}`",
                                   worst=round(worst["pressure_kpa"], 3),
                                   worst_pct=_percent(worst["pressure_kpa"], PRESSURE_LIMIT_KPA)),
              NOT_PROVEN, risks_section(), recommendation(ps)]
    return "\n".join(parts).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Derive the one-pump dye demo for every demo-scale chip.")
    ap.add_argument("--write", action="store_true", help="regenerate docs/demo.md")
    ap.add_argument("--check", action="store_true", help="exit 1 if docs/demo.md is out of date")
    ap.add_argument("--json", action="store_true", help="print the plans as JSON")
    args = ap.parse_args(argv)

    ps = plans()
    if args.json:
        print(json.dumps(ps, indent=2))
        return 0
    doc = render_doc(ps)
    if args.check:
        current = DOC_PATH.read_text() if DOC_PATH.exists() else ""
        if current != doc:
            print(f"{DOC_PATH.relative_to(ROOT)} is out of date: run python tools/demo_plan.py --write")
            return 1
        print(f"{DOC_PATH.relative_to(ROOT)} is up to date")
        return 0
    if args.write:
        DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
        DOC_PATH.write_text(doc)
        print(f"wrote {DOC_PATH.relative_to(ROOT)}")
    print(table(ps))
    print(f"\nwater at 20 C, mu = {MU:g} Pa s; window {MIN_S:g}-{MAX_S:g} s, target {TARGET_S:g} s; "
          f"pressure limit {PRESSURE_LIMIT_KPA:g} kPa (ASSUMPTION)")
    for p in ps:
        if p["flow_ul_min"] is None:
            continue
        print(f"\n{p['chip_id']}: feed {p['feed']} at {p['flow_ul_min']:g} uL/min, vent {p['vent']}, "
              f"plug {', '.join(p['plug']) or 'nothing (every port is in use)'}")
        for s in p["sequence"]:
            flag = " <- filmable" if s["filmable"] else ""
            print(f"    {s['starts_s']:6.1f} -> {s['ends_s']:6.1f} s  {s['kind']:8s} {s['id']:18s} "
                  f"{s['volume_ul']:8.4f} uL{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""What S7 has to join, and how wide. S6 never draws a channel between two parts.

`assay2chip/work/<id>/connections_<chip_id>.json` is the hand-off between placement and
routing. It is deliberately a REQUIREMENTS file and not a draft route: it says which
opening joins which, what travels between them, the narrowest the channel may be and why,
plus the obstacles and the open corridors S7 has to route around and through. The router
owns the path; S6 owns the endpoints.

Where each width comes from:

  capsules / droplets   `capsule_channel.width_factor_low` x the particle diameter, which
                        is the rule tools/check_chip_spec.py enforces at 1.5 x
  cells                 `dissociation.outlet_min_width_um`
  liquid / oil          `min_open_channel_mm`, the in-plane fabrication floor

and never below `min_open_channel_mm`, whatever the particle rule says, because a channel
narrower than that cannot be made.

WHICH OPENING, decided by ROLE AND CARGO rather than by name. The registry labels every
opening, so a trap chamber's three openings sort themselves out: the sample follows the
opening that `carries` its own cargo, spent liquid follows the one that carries `liquid`,
and a reagent uses a spare inlet. When a part has only one outlet, the sample and the
waste SHARE it, and this file says so (`shared_opening`) instead of inventing a second.

ORDER MATTERS. `order` is the flow order, so a router that wants to lay the sample path
first and the service lines around it can. `must_not_cross` lists the connections a given
one shares no endpoint with, which in one fluidic layer is exactly the set it may not
intersect.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402

from .needs import Brief  # noqa: E402
from .parts import Opening, SizedPart  # noqa: E402
from .place import Placement  # noqa: E402
from .ports import PortPlan  # noqa: E402

STAGE = "assay2chip/stages/s6_build.py"


def min_width_mm(carries: str, brief: Brief) -> tuple[float, str]:
    """(width, why) for a channel carrying `carries` to or from this part."""
    floor = contracts.floor("min_open_channel_mm")
    if carries in ("capsules", "droplets"):
        diameter = brief.particle_um or contracts.capsule_diameter_um()
        factor = float(contracts.param("capsule_channel.width_factor_low"))
        want = factor * diameter / 1000.0
        why = (f"{factor:g} x the {diameter:g} um particle "
               f"(contracts/parameters.json capsule_channel.width_factor_low)")
    elif carries == "cells":
        want = float(contracts.param("dissociation.outlet_min_width_um")) / 1000.0
        why = "contracts/parameters.json dissociation.outlet_min_width_um"
    else:
        want, why = floor, "no particles to pass, so the in-plane fabrication floor"
    if want < floor:
        return (round(floor, 4),
                f"{why}, raised to the {floor:g} mm in-plane fabrication floor "
                f"(contracts/blocks.json fabrication_floors.min_open_channel_mm)")
    return (round(want, 4), why)


def _endpoint(placement: Placement, part: SizedPart, opening: Opening) -> dict:
    placed = placement.by_id(part.part_id)
    position, direction = placed.place_opening(opening)
    return {
        "part": part.part_id,
        "block": part.block,
        # The feature a channel actually attaches to. A part that expands to a CHANNEL
        # rather than a chamber (a straight run, a serpentine) has no chamber id, and
        # naming the part instead gives S7 "unknown part: the placed chip has no part
        # called ...". So: the first entry the part really emits.
        "feature": (part.chambers[0]["id"] if part.chambers
                    else part.ids[0] if part.ids else part.part_id),
        "opening": opening.name,
        "position": [position[0], position[1]],
        "direction": direction,
        "opening_width_mm": round(opening.width_mm, 4),
        "role": opening.role,
        "carries": opening.carries,
        "row": placed.row,
    }


def _port_endpoint(plan: PortPlan, port_id: str) -> dict:
    port = plan.by_id(port_id)
    return {
        "port": port.id,
        "feature": f"{port.id}_pad",
        "opening": "pad",
        "position": [port.position[0], port.position[1]],
        "direction": "none",
        "opening_width_mm": round(port.pad_side_mm, 4),
        "note": "route INTO the pad, not to the hole centre: the pad is already cut, so the "
                "channel only has to reach its rim for the port-overlap rule to hold",
    }


def _inline_between(inline: Sequence[Brief], after: int, before: int | None) -> list[Brief]:
    """The inline ops whose op numbers fall between two parts in flow order."""
    return [b for b in inline
            if min(b.ops) > after and (before is None or max(b.ops) < before)]


def _apply_inline(entry: dict, realised: Sequence[Brief]) -> dict:
    """Fold inline ops into the connection that spans them.

    The channel IS the op: it has to be as long as the op needs, so `target_length_mm`
    goes on the net (S7's `meander.py` folds a route until it reaches it) and `realises`
    records which ops that length is for. Without the length the channel would carry the
    fluid past in a fraction of the time the assay asks for, or - for a mixer - never mix
    it at all.
    """
    if not realised:
        return entry
    targets = [b.target_length_mm for b in realised if b.target_length_mm]
    notes = "; ".join(b.inline_why for b in realised if b.inline_why)
    out = dict(entry)
    out["realises"] = [n for b in realised for n in b.ops]
    if targets:
        out["target_length_mm"] = round(max(targets), 3)
    out["why"] = (f"{entry['why']}; it also PERFORMS op(s) "
                  f"{', '.join(str(n) for b in realised for n in b.ops)} "
                  f"({', '.join(k for b in realised for k in b.kinds)}), which are the "
                  f"channel itself rather than parts of their own")
    out["note"] = ((entry.get("note", "") + " " + notes).strip()
                   or "realised as the connecting channel")
    return out


def connections(parts: Sequence[SizedPart], placement: Placement, port_plan: PortPlan,
                inline: Sequence[Brief] = ()) -> list[dict]:
    """Every join S7 must make, in flow order: entry, the chain, the services, the exit.

    An op that is a CONDUIT too small to be a part of its own (`Brief.inline`) is not
    missing from this file: it is folded into the connection that spans it, with the
    length that channel must reach to do its job.
    """
    out: list[dict] = []
    order = 0

    def add(entry: dict) -> None:
        nonlocal order
        order += 1
        out.append({"id": f"conn_{order}", "order": order, **entry})

    first, last = parts[0], parts[-1]
    entry_port = port_plan.inlets[0]
    width, why = min_width_mm(entry_port.request.carries or "liquid", first.brief)
    add(_apply_inline({
        "from": _port_endpoint(port_plan, entry_port.id),
        "to": _endpoint(placement, first, first.sample_in()),
        "carries": entry_port.request.carries or "liquid",
        "min_width_mm": width,
        "width_from": why,
        "why": f"the sample enters at '{entry_port.id}' and the first thing it meets is "
               f"op {first.brief.op} ({first.brief.kind})",
    }, _inline_between(inline, 0, min(first.brief.ops))))

    for upstream, downstream in zip(parts, parts[1:]):
        out_opening = upstream.sample_out()
        in_opening = downstream.sample_in()
        carries = out_opening.carries or in_opening.carries or "liquid"
        width, why = min_width_mm(carries, downstream.brief)
        add(_apply_inline({
            "from": _endpoint(placement, upstream, out_opening),
            "to": _endpoint(placement, downstream, in_opening),
            "carries": carries,
            "min_width_mm": width,
            "width_from": why,
            "why": f"flow order: op {upstream.brief.op} ({upstream.brief.kind}) feeds "
                   f"op {downstream.brief.op} ({downstream.brief.kind})",
        }, _inline_between(inline, max(upstream.brief.ops), min(downstream.brief.ops))))

    reagent = next((p for p in port_plan.inlets if p.request.role == "reagent"), None)
    oil = next((p for p in port_plan.inlets if p.request.role == "oil"), None)
    waste = next((p for p in port_plan.outlets if p.request.role == "waste"), None)
    vent = next((p for p in port_plan.outlets if p.id == "vent"), None)

    # ONE reagent line and ONE drain per chip, not one per part. The chip is a serial
    # train: a reagent injected at the first place that needs it flows through the places
    # downstream, and spent liquid leaves at the last place that drains. Giving every part
    # its own feed and its own drain would need a port each (the demo budget is six) and
    # would funnel four or five channels onto one 1.2 mm pad, which S7 correctly refuses:
    # "every mouth on this pad is within width/2 + min_wall of another feature". The cost
    # is recorded in the connection's `why` and in `serves`, because an upstream wash's
    # effluent really does pass through the places after it - the same trade-off
    # chip/module3_v1_translated.json makes with its one merged reagent line and its
    # shared waste.
    feeders = [p for p in parts if p.brief.needs_reagent]
    drainers = [p for p in parts if p.brief.drains]
    reagent_at = feeders[0] if feeders else None
    drain_at = drainers[-1] if drainers else None

    for part in parts:
        brief = part.brief
        spare_inlets = part.extra_inlets()
        if part is reagent_at and reagent is not None:
            if spare_inlets:
                width, why = min_width_mm("liquid", brief)
                add({
                    "from": _port_endpoint(port_plan, reagent.id),
                    "to": _endpoint(placement, part, spare_inlets[0]),
                    "carries": "liquid",
                    "min_width_mm": width,
                    "width_from": why,
                    "why": f"op {brief.op} ({brief.kind}) needs a reagent delivered to it; "
                           f"which reagent is selected off-chip, and the places downstream "
                           f"of it are served through the chain rather than each having "
                           f"their own feed",
                    "shared_port": True,
                    "serves": [n for f in feeders for n in f.brief.ops],
                })
            elif reagent.id != entry_port.id:
                # The block has ONE inlet, so the reagent and the sample have to meet
                # before the part. They meet in the ENTRY PAD, not at the part's own
                # mouth: a pad is a wide piece of already-cut area that can take two
                # channels, while a chamber face that already has the sample line on it
                # has no second mouth left - S7 reports exactly that ("every mouth on this
                # chamber is within width/2 + min_wall of another feature") and it is
                # right. Which line is live is decided off-chip by the selector, the same
                # arrangement as chip/module3_v1_translated.json's merged reagent feed.
                width, why = min_width_mm("liquid", brief)
                add({
                    "from": _port_endpoint(port_plan, reagent.id),
                    "to": _port_endpoint(port_plan, entry_port.id),
                    "carries": "liquid",
                    "min_width_mm": width,
                    "width_from": why,
                    "why": f"op {brief.op} ({brief.kind}) needs a reagent and block "
                           f"'{part.block}' has only one inlet, so the reagent joins the "
                           f"sample line in the '{entry_port.id}' pad upstream of the part",
                    "shared_port": True,
                    "shared_opening": True,
                    "tees_into": f"{entry_port.id}_pad",
                    "note": "the off-chip selector decides which of the two lines is live; "
                            "the pad is the junction, so nothing has to enter the part twice",
                })
        if brief.kind == "encapsulate" and oil is not None and len(spare_inlets) > 1:
            width, why = min_width_mm("liquid", brief)
            add({
                "from": _port_endpoint(port_plan, oil.id),
                "to": _endpoint(placement, part, spare_inlets[-1]),
                "carries": "oil",
                "min_width_mm": width,
                "width_from": why,
                "why": f"the oil phase pinches off droplets at op {brief.op}",
            })
        drain = part.liquid_out()
        if part is drain_at and waste is not None:
            target = drain or part.sample_out()
            width, why = min_width_mm("liquid", brief)
            add({
                "from": _endpoint(placement, part, target),
                "to": _port_endpoint(port_plan, waste.id),
                "carries": "liquid",
                "min_width_mm": width,
                "width_from": why,
                "why": f"spent liquid leaves to waste here, at the last place on this chip "
                       f"that drains (op {brief.op}, {brief.kind})"
                       + (", through the retention crest, so no particles go with it"
                          if brief.weir_retained and drain is not None else "")
                       + ("; the effluent of the places upstream reaches it through the "
                          "chain, which is the price of one shared drain"
                          if len(drainers) > 1 else ""),
                "shared_port": True,
                "serves": [n for d in drainers for n in d.brief.ops],
                **({} if drain is not None else {
                    "shared_opening": True,
                    "note": f"block '{part.block}' has one outlet, so waste and sample share "
                            f"it; S7 must branch outside the part and the branch is selected "
                            f"off-chip"}),
            })
        if brief.kind == "thermocycle" and vent is not None:
            width, why = min_width_mm("liquid", brief)
            add({
                "from": _endpoint(placement, part, part.sample_out()),
                "to": _port_endpoint(port_plan, vent.id),
                "carries": "liquid",
                "min_width_mm": width,
                "width_from": why,
                "why": f"air leaves op {brief.op}'s reactor while it fills",
                "capillary_stop": True,
                "note": "S7 must make this a constriction (block flow_restrictor) so liquid "
                        "halts at it and the reaction stays isolated from anything else "
                        "sharing the vent",
            })

    exit_port = next((p for p in port_plan.outlets
                      if p.request.role != "waste" and p.id != "vent"), port_plan.outlets[0])
    carries = exit_port.request.carries or "liquid"
    width, why = min_width_mm(carries, last.brief)
    add(_apply_inline({
        "from": _endpoint(placement, last, last.sample_out()),
        "to": _port_endpoint(port_plan, exit_port.id),
        "carries": carries,
        "min_width_mm": width,
        "width_from": why,
        "why": f"the product leaves op {last.brief.op} ({last.brief.kind}) at "
               f"'{exit_port.id}'",
    }, _inline_between(inline, max(last.brief.ops), None)))

    _mark_non_crossing(out)
    return out


def _mark_non_crossing(items: Sequence[dict]) -> None:
    """For each connection, which others it may not intersect: those it shares no end with."""
    def ends(c: dict) -> set[str]:
        return {str(c["from"].get("feature")), str(c["to"].get("feature"))}
    for a in items:
        a["must_not_cross"] = sorted(b["id"] for b in items
                                     if b is not a and not (ends(a) & ends(b)))


def obstacles(placement: Placement, port_plan: PortPlan) -> list[dict]:
    """Rectangles S7 must keep a wall away from: the parts, and every port pad."""
    out = [{"id": p.part.part_id, "kind": "part", "block": p.part.block,
            "rect": [round(v, 4) for v in p.rect]} for p in placement.placed]
    for port in port_plan.ports:
        half = port.pad_side_mm / 2.0
        x, y = port.position
        out.append({"id": f"{port.id}_pad", "kind": "port_pad",
                    "rect": [round(x - half, 4), round(y - half, 4),
                             round(x + half, 4), round(y + half, 4)]})
    return out


def internal_features(parts: Sequence[SizedPart]) -> list[dict]:
    """What is INSIDE each part: crests, pillars, nozzle throats. S7 must not route over them."""
    out = []
    for part in parts:
        for feature in part.features:
            out.append({
                "part": part.part_id, "kind": feature.kind, "count": feature.count,
                "size_mm": [round(v, 4) for v in feature.size_mm],
                "pitch_mm": feature.pitch_mm, "in_plane_min_mm": feature.in_plane_min_mm,
                "note": feature.note,
            })
    return out


def connection_file(*, assay_id: str, chip_id: str, scale: str, parts: Sequence[SizedPart],
                    placement: Placement, port_plan: PortPlan,
                    inline: Sequence[Brief] = (),
                    unresolved: Sequence[dict] = ()) -> dict:
    """The whole connections_<chip_id>.json payload."""
    env = placement.envelope
    floors = contracts.fabrication_floors()
    return {
        "id": assay_id,
        "chip_id": chip_id,
        "written_by": STAGE,
        "for_stage": "s7_route",
        "scale": scale,
        "footprint": {"x": env.x_mm, "y": env.y_mm},
        "etch_depth_mm": contracts.max_depth_mm(),
        "layers": env.layers,
        "rules": {
            "min_open_channel_mm": floors["min_open_channel_mm"],
            "min_wall_mm": floors["min_wall_mm"],
            "max_depth_mm": contracts.max_depth_mm(),
            "no_crossings": True,
            "why_no_crossings": "ONE fluidic layer (contracts/blocks.json envelope.layers = 1 "
                                "and excluded.multilayer): two channels that meet in plan "
                                "view are a short circuit, not a bridge",
        },
        "placement": {
            "rows": placement.rows,
            "row_bands": [[b[0], b[1]] for b in placement.row_bands],
            "usable": [round(v, 4) for v in placement.usable],
            "edge_margin_mm": placement.edge_margin_mm,
            "clearance_mm": placement.clearance_mm,
            "chain_length_mm": placement.chain_length_mm,
            "crossing_risk": placement.crossing_risk,
        },
        "corridors": placement.corridors,
        "obstacles": obstacles(placement, port_plan),
        "internal_features": internal_features(parts),
        "connections": connections(parts, placement, port_plan, inline),
        "realised_as_channels": [
            {"ops": list(b.ops), "kinds": list(b.kinds), "why": b.inline_why,
             "target_length_mm": b.target_length_mm} for b in inline],
        "unresolved": [dict(u) for u in unresolved],
    }

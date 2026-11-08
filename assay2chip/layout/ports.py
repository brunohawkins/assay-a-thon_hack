"""Ports on the boundary, each with a pad, at a legal bore, pitch and count.

THE PORT PAD, and why every port here has one. `tools/check_chip_spec.py` enforces a rule
(`contracts/README.md`, "Port overlap"): a lid hole must open onto at least 1 mm2 of cut
channel or chamber, or a QUARTER of its own area when the hole is smaller than 2 x 2 mm. A
`min_port_mm` bore is 0.8 mm, so it has 0.64 mm2 in all and must land on at least
0.16 mm2 of cut. Drilling at the END of a channel gives about half the bore, which is why
main's hand-made chips report 60 of these errors. The fix the rule itself names is a PAD: a
squat pocket cut at the same etch depth, centred on the hole and wider than the bore.

The pad is NOT sized here. `registry.size("port", need)` already builds the hole plus its
pad from the contract's floors and widens the bore when particles have to pass, so this
module asks the registry for every port exactly as it asks it for every other part, and
takes `physics["hole_mm"]`, `physics["pad_mm"]` and the pad's chip-spec entry from it. One
pad formula, in the registry, used by everyone.

One thing the registry cannot know is WHICH EDGE a port is on, so its `stub` opening
(pad edge, +x) is advisory: S6 places the pad centred on a boundary position and tells S7
to route into the pad, whose whole rim is available. That is also why the pad's volume is
recomputed from the size written here - the registry's figure folds in a stub channel that
this chip does not cut.

WHICH EDGE. Inlets on one side and outlets on the other is the planar default, so the
chain's entry port goes on the left edge and its exit port on the edge the chain ends at.
Reagent feeds come in from the bottom edge under the part that needs them, and waste leaves
by the top edge; `place.py` has already preferred a row count where the parts needing a
side port can reach their edge without crossing anything.

WHICH FLOORS. Bore >= `min_port_mm`; centre-to-centre pitch >= `min_port_pitch_mm` against
EVERY other port, not just its neighbours on the same edge (a corner pair on two different
edges is the case that catches people); count against `max_ports_per_chip` for this scale,
which `contracts.max_ports_per_chip` returns as None at target scale because the contract
says the limit lifts there.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402
from assay2chip.parts import registry  # noqa: E402
from assay2chip.parts.geometry import Need, PartGeometry, cylinder_volume_ul  # noqa: E402

from .needs import Brief  # noqa: E402
from .place import Placement  # noqa: E402

PORT_BLOCK = "port"
SLOT_STEP_MM = 0.25        # how finely a port may slide along its edge


class PortBudgetExceeded(Exception):
    """More ports than the scale allows, or no legal position left on an edge."""

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


@dataclass(frozen=True)
class PortRequest:
    """A port the chip needs, before it has a position."""

    id: str
    type: str                       # inlet | outlet
    role: str | None
    edge: str                       # left | right | bottom | top
    near: int | None                # index into placement.placed
    carries: str | None
    particles: str                  # what has to fit through the bore
    particle_um: float | None
    capsule_passable: bool | None
    why: str
    note: str = ""


@dataclass(frozen=True)
class PlacedPort:
    """A port with a position, and the pad the registry sized for it."""

    request: PortRequest
    position: tuple[float, float]
    geometry: PartGeometry

    @property
    def id(self) -> str:
        return self.request.id

    @property
    def hole_mm(self) -> float:
        return float(self.geometry.physics["hole_mm"])

    @property
    def pad_side_mm(self) -> float:
        return float(self.geometry.physics["pad_mm"])

    @property
    def depth_mm(self) -> float:
        return self.geometry.depth_mm

    def spec(self, top_z: float) -> dict:
        out: dict = {"id": self.id, "type": self.request.type,
                     "position": [self.position[0], self.position[1], round(top_z, 4)]}
        if self.request.role:
            out["role"] = self.request.role
        if self.request.capsule_passable is not None:
            out["capsule_passable"] = self.request.capsule_passable
        out["width_um"] = round(self.hole_mm * 1000.0, 1)
        out["note"] = self.request.note or self.request.why
        return out

    def pad(self, floor_z: float) -> dict:
        """The pad as a chip-spec chamber. This is what makes the port-overlap rule pass."""
        entry = dict(self.geometry.chambers[0])
        side = self.pad_side_mm
        entry["id"] = f"{self.id}_pad"
        entry["type"] = "inlet" if self.request.type == "inlet" else "outlet"
        entry["size"] = [side, side, self.depth_mm]
        entry["center"] = [self.position[0], self.position[1],
                           round(floor_z + self.depth_mm / 2.0, 4)]
        entry["volume_ul"] = round(cylinder_volume_ul(side, side, self.depth_mm), 4)
        entry["note"] = (f"{entry.get('note', '')} S7 routes INTO this pad, not to the hole "
                         f"centre.").strip()
        return entry


@dataclass
class PortPlan:
    ports: list[PlacedPort]
    pad_side_mm: float
    bore_mm: float
    pitch_mm: float
    limit: int | None
    unresolved: list[dict] = field(default_factory=list)

    def by_id(self, port_id: str) -> PlacedPort:
        for p in self.ports:
            if p.id == port_id:
                return p
        raise KeyError(port_id)

    @property
    def inlets(self) -> list[PlacedPort]:
        return [p for p in self.ports if p.request.type == "inlet"]

    @property
    def outlets(self) -> list[PlacedPort]:
        return [p for p in self.ports if p.request.type == "outlet"]


def _port_need(particles: str, particle_um: float | None, *, port_id: str,
               scale: str) -> Need:
    return Need(particles=particles, particle_um=particle_um, label=port_id, scale=scale,
                note=f"port {port_id}")


def size_port(particles: str, particle_um: float | None, *, port_id: str = "port",
              scale: str = "demo") -> PartGeometry:
    """The registry's hole-plus-pad for one port."""
    return registry.size(PORT_BLOCK, _port_need(particles, particle_um, port_id=port_id,
                                                scale=scale))


def pad_budget(briefs: Sequence[Brief], *, scale: str = "demo") -> float:
    """The LARGEST pad any port on this chip will need.

    `place.py` insets the usable band by this, so every pad - including a wide
    capsule-passable one - keeps a full wall to the nearest part and to the chip edge.
    Computed before placement, because the margin decides where the parts can go.
    """
    pads = [size_port("none", None, scale=scale).physics["pad_mm"]]
    for b in briefs:
        pads.append(size_port(b.particles, b.particle_um, scale=scale).physics["pad_mm"])
    return round(max(float(p) for p in pads), 4)


def _carries(brief: Brief) -> str:
    if brief.particles == "none":
        return "oil" if brief.viscosity_class == "oil" else "liquid"
    return {"cells": "cells", "capsules": "capsules", "beads": "capsules"}[brief.particles]


def requests_for(briefs: Sequence[Brief], placement: Placement, *, floors: dict,
                 handoffs: Sequence[dict] = (), chip_id: str = "") -> list[PortRequest]:
    """Which ports this chip needs, in the order they matter if the budget gets tight.

    The entry and exit ports are named by the partition's hand-offs when there is one, so
    `partition.json` stays the single place that says which outlet feeds which inlet.
    """
    first, last = briefs[0], briefs[-1]
    arriving = next((h for h in handoffs if h.get("to") == chip_id), None)
    leaving = next((h for h in handoffs if h.get("from") == chip_id), None)

    out = [
        PortRequest(
            id=(arriving or {}).get("to_port") or "inlet_1", type="inlet", role="load",
            edge=placement.entry_edge, near=0, carries=_carries(first),
            particles=first.particles, particle_um=first.particle_um,
            capsule_passable=True if first.particles == "capsules" else None,
            why=(f"the chip's entry: op {first.op} ({first.kind}) is the first thing on it"
                 + (f", fed by the hand-off from {arriving['from']}" if arriving else "")),
        ),
        PortRequest(
            id=(leaving or {}).get("from_port") or "outlet_1", type="outlet",
            role="harvest" if _carries(last) != "liquid" else "release",
            edge=placement.exit_edge, near=len(briefs) - 1, carries=_carries(last),
            particles=last.particles, particle_um=last.particle_um,
            capsule_passable=True if last.particles == "capsules" else None,
            why=(f"the chip's product outlet, after op {last.op} ({last.kind})"
                 + (f", handed off to {leaving['to']}" if leaving else "")),
        ),
    ]
    feeders = [i for i, b in enumerate(briefs) if b.needs_reagent]
    if feeders:
        ops = ", ".join(str(briefs[i].op) for i in feeders)
        out.append(PortRequest(
            id="reagent_in", type="inlet", role="reagent", edge="bottom", near=feeders[0],
            carries="liquid", particles="none", particle_um=None, capsule_passable=False,
            why=(f"one reagent feed for op(s) {ops}; WHICH reagent is selected OFF chip by a "
                 f"rotary or selector valve, because an on-chip valve needs a second "
                 f"fluidic layer and contracts/blocks.json excludes those"),
        ))
    oil = [i for i, b in enumerate(briefs)
           if b.viscosity_class == "oil" or b.kind == "encapsulate"]
    if oil:
        out.append(PortRequest(
            id="oil_in", type="inlet", role="oil", edge="bottom", near=oil[0], carries="oil",
            particles="none", particle_um=None, capsule_passable=False,
            why=f"the oil phase for op {briefs[oil[0]].op} on its own line: it must never "
                f"share a feed with the aqueous reagents",
        ))
    drains = [i for i, b in enumerate(briefs) if b.drains]
    if drains:
        ops = ", ".join(str(briefs[i].op) for i in drains)
        retained = any(briefs[i].weir_retained for i in drains)
        out.append(PortRequest(
            id="waste", type="outlet", role="waste", edge="top", near=drains[-1],
            carries="liquid", particles="none", particle_um=None, capsule_passable=False,
            why=f"spent liquid from op(s) {ops} leaves here"
                + (", behind the retention crest, so no particles pass it" if retained else ""),
        ))
    vents = [i for i, b in enumerate(briefs) if b.kind == "thermocycle"]
    if vents:
        out.append(PortRequest(
            id="vent", type="outlet", role=None, edge="top", near=vents[0], carries="liquid",
            particles="none", particle_um=None, capsule_passable=False,
            why=f"air escapes while op {briefs[vents[0]].op}'s reactor fills and while it is "
                f"thermocycled; S7 must reach it through a capillary stop so liquid halts at "
                f"the constriction and the reaction stays isolated, as chip/module4_v1.json does",
        ))
    return out


def _slots(edge: str, placement: Placement, inset: float) -> list[tuple[float, float]]:
    """Candidate positions along one edge, inside the margins."""
    env = placement.envelope
    lo, hi = inset, (env.x_mm if edge in ("bottom", "top") else env.y_mm) - inset
    n = max(int((hi - lo) / SLOT_STEP_MM), 1)
    values = [round(lo + i * SLOT_STEP_MM, 4) for i in range(n + 1)]
    if edge == "left":
        return [(inset, v) for v in values]
    if edge == "right":
        return [(round(env.x_mm - inset, 4), v) for v in values]
    if edge == "bottom":
        return [(v, inset) for v in values]
    return [(v, round(env.y_mm - inset, 4)) for v in values]


def plan_ports(briefs: Sequence[Brief], placement: Placement, *, floors: dict, scale: str,
               handoffs: Sequence[dict] = (), chip_id: str = "") -> PortPlan:
    """Place every port this chip needs. Raises PortBudgetExceeded when it cannot."""
    requests = requests_for(briefs, placement, floors=floors, handoffs=handoffs,
                            chip_id=chip_id)
    limit = contracts.max_ports_per_chip(scale)
    if limit is not None and len(requests) > limit:
        raise PortBudgetExceeded(
            f"this chip needs {len(requests)} ports ({', '.join(r.id for r in requests)}) but "
            f"the {scale} envelope allows {limit} "
            f"(contracts/blocks.json fabrication_floors.max_ports_per_chip)",
            {"wanted": len(requests), "limit": limit, "scale": scale,
             "ports": [{"id": r.id, "why": r.why} for r in requests]})

    budget = pad_budget(briefs, scale=scale)
    pitch = float(floors["min_port_pitch_mm"])
    inset = float(floors["min_wall_mm"]) + budget / 2.0
    placed: list[PlacedPort] = []
    unresolved: list[dict] = []
    for request in requests:
        geometry = size_port(request.particles, request.particle_um, port_id=request.id,
                             scale=scale)
        tx, ty = (placement.placed[request.near].centre if request.near is not None
                  else (placement.envelope.x_mm / 2.0, placement.envelope.y_mm / 2.0))
        slots = _slots(request.edge, placement, inset)
        # Nearest legal slot to the part this port serves: the shortest stub, so the
        # shortest channel for S7 to draw and the least chance of it meeting anything else.
        if request.edge in ("left", "right"):
            def key(slot: tuple[float, float]) -> float:
                return abs(slot[1] - ty)
        else:
            def key(slot: tuple[float, float]) -> float:
                return abs(slot[0] - tx)
        chosen = next((s for s in sorted(slots, key=key)
                       if all(_distance(s, p.position) >= pitch - 1e-9 for p in placed)), None)
        if chosen is None:
            raise PortBudgetExceeded(
                f"no position left on the {request.edge} edge for port '{request.id}': every "
                f"slot is within the {pitch:g} mm minimum port pitch of a port already placed",
                {"port": request.id, "edge": request.edge, "pitch_mm": pitch,
                 "placed": [p.id for p in placed], "scale": scale})
        for risk in geometry.risks:
            unresolved.append({
                "chip_id": chip_id, "field": f"ports.{request.id}",
                "question": f"port '{request.id}': {risk}",
                "assumed": round(float(geometry.physics["hole_mm"]), 4),
                "assumed_from": "assay2chip/parts/registry.py size('port', ...)"})
        placed.append(PlacedPort(request=request,
                                 position=(round(chosen[0], 4), round(chosen[1], 4)),
                                 geometry=geometry))
    return PortPlan(ports=placed, pad_side_mm=budget,
                    bore_mm=float(floors["min_port_mm"]), pitch_mm=pitch, limit=limit,
                    unresolved=unresolved)


def _distance(a: Sequence[float], b: Sequence[float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5

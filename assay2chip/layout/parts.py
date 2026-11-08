"""The ONE place S6 talks to the parts registry.

`assay2chip/parts/registry.py` owns the physics: `registry.size(block_id, need)` returns a
`PartGeometry` - footprint, depth, named boundary openings, the internal features a mould
has to cut, and the chip-spec entries the part expands to - or raises with the physical
reason. This module does three things:

  1. tries a Brief's CANDIDATE blocks in order through `registry.try_size`, keeping the
     registry's reason for every refusal, so "nothing can do this op" is always a
     quotable argument;
  2. wraps the winner in `SizedPart`, which carries the part id, the brief, what was
     tried, and the semantic lookups S6 keeps asking for - which opening is the sample
     inlet, which one the sample leaves by, which one drains liquid, which one takes a
     reagent;
  3. nothing else. It sizes no geometry of its own.

PART-LOCAL COORDINATES (the registry's contract, `assay2chip/parts/geometry.py`): origin
at the footprint's bottom-left corner, +x the nominal flow direction, `openings` on the
boundary with an outward normal, entry `center`/`path` part-local. `place.py` adds the
part's origin and its rotation; `spec.py` adds the layer's z. No sizer ever thinks about
rotation.

OPENING ROLES, and how S6 reads them. The registry labels each opening `inlet`, `outlet`
or `release` and says what it `carries`. That is enough to tell a trap chamber's three
openings apart without naming them: `inlet` takes the suspension, the `outlet` carrying
`liquid` is the filtrate drain behind the crest, and the `release` opening carrying
`capsules` is how the bed leaves. So `sample_out()` follows the cargo and `liquid_out()`
follows the waste, and a part with only one outlet shares it - which S6 then says out loud
rather than inventing a second one.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402
from assay2chip.parts import registry  # noqa: E402
from assay2chip.parts.geometry import (  # noqa: E402
    CARRIES_FOR_PARTICLE, Expansion, Feature, Opening, PartGeometry, PartSizingError,
)

from .needs import Brief  # noqa: E402

REGISTRY_SOURCE = "assay2chip/parts/registry.py"


class PartError(Exception):
    """No candidate block can perform this op. `detail` carries every refusal."""

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


@dataclass(frozen=True)
class SizedPart:
    """One sized part: the registry's geometry plus who it is for and what was tried."""

    part_id: str
    block: str
    brief: Brief
    geometry: PartGeometry
    tried: tuple[dict, ...] = field(default_factory=tuple)

    # --- the bits place.py and spec.py use ------------------------------------
    @property
    def footprint(self) -> tuple[float, float]:
        return self.geometry.footprint_mm

    @property
    def depth_mm(self) -> float:
        return self.geometry.depth_mm

    @property
    def area_mm2(self) -> float:
        return self.geometry.area_mm2

    @property
    def volume_ul(self) -> float:
        return self.geometry.volume_ul

    @property
    def openings(self) -> tuple[Opening, ...]:
        return self.geometry.openings

    @property
    def chambers(self) -> tuple[dict, ...]:
        return self.geometry.chambers

    @property
    def channels(self) -> tuple[dict, ...]:
        return self.geometry.channels

    @property
    def entries(self) -> tuple[Expansion, ...]:
        return self.geometry.entries

    @property
    def features(self) -> tuple[Feature, ...]:
        """Internal geometry a mould has to cut: a weir crest, pillars, a nozzle throat.

        NOT chip-spec `features` (those are windows and heat zones). They travel in the
        connections file so S7 and S11 can see what is inside a part.
        """
        return self.geometry.features

    @property
    def ids(self) -> list[str]:
        return [e.entry["id"] for e in self.entries]

    def opening(self, name: str) -> Opening:
        return self.geometry.opening(name)

    # --- semantic lookups -----------------------------------------------------
    @property
    def cargo(self) -> str:
        """What the sample is, as a chip-spec `carries` value."""
        return CARRIES_FOR_PARTICLE[self.brief.particles]

    def sample_in(self) -> Opening:
        """Where the sample arrives: the inlet carrying the cargo, else any opening.

        Some blocks have a single opening that serves both ways (a stub on a pad, a
        one-armed node). Falling back to it is right: S7 joins openings, and a part with
        one is a part where everything meets at that one. Only a part with NO opening at
        all is unusable, and then the error says so.
        """
        if not self.openings:
            raise PartError(f"part '{self.part_id}' ({self.block}) has no openings at all, "
                            f"so nothing can be joined to it")
        inlets = [o for o in self.openings if o.role == "inlet"] or list(self.openings)
        return next((o for o in inlets if o.carries == self.cargo), inlets[0])

    def sample_out(self) -> Opening:
        """Where the sample leaves: follow the cargo, not the name."""
        if not self.openings:
            raise PartError(f"part '{self.part_id}' ({self.block}) has no openings at all, "
                            f"so nothing can be joined to it")
        outs = [o for o in self.openings if o.role in ("outlet", "release")]
        if not outs:
            # One-opening part: it leaves the way it came, which S7 has to know about.
            outs = [o for o in self.openings if o is not self.sample_in()] or list(self.openings)
        return next((o for o in outs if o.carries == self.cargo),
                    next((o for o in outs if o.name == "outlet"), outs[0]))

    def liquid_out(self) -> Opening | None:
        """Where spent liquid drains, when that is a different opening from the sample's."""
        sample = self.sample_out()
        return next((o for o in self.openings
                     if o.role in ("outlet", "release") and o is not sample
                     and o.carries == "liquid"), None)

    def reagent_in(self) -> Opening | None:
        """A second inlet a reagent can be delivered through, when the part has one."""
        sample = self.sample_in()
        return next((o for o in self.openings if o.role == "inlet" and o is not sample), None)

    def extra_inlets(self) -> list[Opening]:
        sample = self.sample_in()
        return [o for o in self.openings if o.role == "inlet" and o is not sample]


def registry_source() -> str:
    """Named in the chip's `generator.params`, so a reader knows what sized its parts."""
    return REGISTRY_SOURCE


ENVELOPE_FLOORS = ("envelope_x_mm", "envelope_y_mm")

# The flow ladder for `size_parts`. See `size_parts` for why reducing the flow is a legal
# move and raising it is not.
FLOW_RELIEF = (1.0, 0.5, 0.25, 0.1, 0.05, 0.02, 0.01)


def size_part(brief: Brief, *, flow_scale: float = 1.0) -> SizedPart:
    """Size the part for `brief`, trying its candidate blocks in order.

    Raises PartError listing every refusal when none of them can be built, keeping each
    refusal's `floor` so S6 can tell "this op is too big for the envelope" (which is S4's
    to solve by splitting) from "no block can do this at all" (which is not). S6 never
    substitutes a block of its own and never shrinks one to fit.
    """
    tried: list[dict] = []
    if flow_scale != 1.0 and brief.flow_ul_min:
        brief = replace(brief, flow_ul_min=brief.flow_ul_min * flow_scale)
    need = brief.need
    for block in brief.blocks:
        try:
            geometry = registry.size(block, need)
        except PartSizingError as e:
            tried.append({"block": block, "sized": False, "why": str(e), "floor": e.floor,
                          "required_mm": e.required, "actual_mm": e.actual})
            continue
        except contracts.ContractError as e:
            tried.append({"block": block, "sized": False, "why": str(e), "floor": None})
            continue
        tried.append({"block": block, "sized": True})
        return SizedPart(part_id=brief.part_id, block=block, brief=brief, geometry=geometry,
                         tried=tuple(tried))
    too_big = bool(tried) and all(t.get("floor") in ENVELOPE_FLOORS for t in tried)
    size = (f"{brief.volume_ul:g} uL" if brief.volume_ul is not None else
            f"a flow-through part ({brief.residence_s or 0:g} s of residence)")
    raise PartError(
        f"op {brief.op} ({brief.kind}): none of {', '.join(brief.blocks)} can be built for "
        f"{size} of {brief.particles} at the {brief.scale} scale"
        + (f"; the smallest one still overruns the envelope" if too_big else ""),
        {"op": brief.op, "kind": brief.kind, "volume_ul": brief.volume_ul,
         "volume_source": brief.volume_source, "particles": brief.particles,
         "scale": brief.scale, "too_big": too_big, "blocks": list(brief.blocks),
         "tried": tried})


def size_parts(briefs: Sequence[Brief], *, flow_scale: float = 1.0) -> list[SizedPart]:
    """Size every part on a chip at one flow scale.

    WHY A FLOW SCALE IS A LEGAL MOVE AND SHRINKING A PART IS NOT. No flow rate appears
    anywhere in `unitops.json`, so every flow on a generated chip is a DESIGN flow that S6
    derived and flagged (`assay2chip/layout/needs.py carry_flow_forward`). The contracts
    bound it only from ABOVE - the wall-shear ceilings, and
    `capsule.escape_pressure_kpa x control.pressure_margin` - so running slower is always
    legal and running faster may not be. And flow is what makes these parts big: a
    herringbone mixer's length is set by the Peclet number, and a chamber holding adherent
    cells gets its width from the shear ceiling, both proportional to the flow. A 30 uL/min
    design flow asks for a 9.5 mm mixer; a 3 uL/min one asks for a much smaller part that
    does the same job more slowly.

    So when a chip does not fit, S6 slows it down rather than shrinking a part below its
    floor or asking S4 to re-cut the assay. The ladder is bounded (FLOW_RELIEF), the factor
    that was used is recorded on the chip, and S8 may raise the flow again later within the
    ceilings. A part is NEVER taken below a fabrication floor to make it fit - that is the
    registry's refusal to make, and it still makes it.
    """
    return [size_part(b, flow_scale=flow_scale) for b in briefs]


def joinable_minimum_mm(brief: Brief) -> float:
    """The smallest a part may be and still have a mouth a channel can enter.

    A channel of `route_width_mm` arriving at a face needs its own width plus a wall on
    each side of it, so a face shorter than that has nowhere for the channel to land. A
    part below this is not a small part, it is an unusable one - which is why S7 refuses
    it with "every mouth on this part is within width/2 + min_wall of another feature.
    This is a placement problem, not a routing one."
    """
    return brief.route_width_mm + 2 * float(contracts.floor("min_wall_mm"))


def joinable(part: SizedPart) -> bool:
    """True when both of a part's faces are long enough for a channel to attach to."""
    return min(part.footprint) >= joinable_minimum_mm(part.brief) - 1e-9


def mixing_length_mm(part: SizedPart) -> float | None:
    """The centreline length the registry says this part needs to do its job.

    A herringbone reports `centreline_length_mm` - the length that gives enough chevron
    cycles to mix at this Peclet number. When a mixing op is realised as a channel instead
    of a part, that is the length the channel has to be, not merely the residence length:
    a shorter one carries the fluid for long enough but never mixes it.
    """
    value = part.geometry.physics.get("centreline_length_mm")
    return float(value) if isinstance(value, (int, float)) else None


def route_width_mm(parts: Sequence[SizedPart]) -> float:
    """The widest channel S7 will have to draw on this chip, so corridors can hold it.

    From the CARGO rule, not from the openings: an opening on a chamber is as wide as the
    chamber face (that is the point - the front arrives evenly), while the channel joining
    it only has to pass the particles.
    """
    floor = contracts.floor("min_open_channel_mm")
    return round(max([floor] + [p.brief.route_width_mm for p in parts]), 4)


__all__ = ["Expansion", "Feature", "Opening", "PartError", "PartGeometry", "PartSizingError",
           "SizedPart", "registry_source", "route_width_mm", "size_part", "size_parts"]

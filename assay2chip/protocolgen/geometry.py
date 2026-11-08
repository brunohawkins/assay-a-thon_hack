"""The chip spec as a graph, and the one place a cross-section is decided.

THE WIDTH IS THE WIDTH ACROSS THE FLOW OF THE REAL CHANNEL, NEVER A BOUNDING BOX.
Wall shear is tau = 6 mu Q / (w h^2), so w enters linearly and h squared: taking a
serpentine mixer's bounding box (25 x 60 mm) instead of its channel (0.5 mm) made one
shear number 43x too small, which is 43x too much flow through live cells. `cross_section`
therefore refuses to guess. A folded part (`mixer`, or a block whose shape is a channel)
must state its channel width in the spec; a box chamber gets the in-plane side ACROSS the
flow, and the flow axis is read from where its channels actually enter and leave.

Everything here is read-only: nothing in this module modifies the chip spec.

    chip = Chip(json.loads(Path("chip/module3_v1_translated.json").read_text()))
    chip.cross_section("main_trap")       # CrossSection(width_mm=11.32, height_mm=0.3, ...)
    chip.volume_ul("main_trap")           # 38.443, from the spec - never recomputed
    chip.drains("main_trap")              # ['waste', 'lysis_waste', 'to_split_tree']
    chip.thermal_zone("main_trap")        # the declared zone, or None
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Chamber types liquid passes through without stopping, so a route continues past them.
PASS_THROUGH_TYPES = ("junction",)

# Chamber types that are not a place an op can run in: they are features in the line.
NOT_A_WORKPLACE = ("junction", "filter", "inlet", "outlet", "bubble_trap")

# A part whose shape IS a channel: its flow width is the channel width, and the bounding
# box is meaningless for shear. Matched against the chamber's `block` and `id`.
FOLDED_BLOCKS = ("serpentine", "snake", "spiral", "tesla", "channel", "mixer", "meander")


class GeometryError(ValueError):
    """The chip spec does not say something S8 needs, and S8 will not invent it."""


@dataclass(frozen=True)
class CrossSection:
    """The rectangle the liquid actually flows through, in mm.

    `width_mm` is across the flow and `height_mm` is the etch depth. `why` says how the
    width was decided and is carried into the protocol so a reader can check it.
    """

    width_mm: float
    height_mm: float
    why: str

    @property
    def area_mm2(self) -> float:
        return self.width_mm * self.height_mm


@dataclass(frozen=True)
class Segment:
    """One piece of a flow route: a channel, a chamber, or a retention feature."""

    id: str
    kind: str                 # 'channel' | 'chamber' | 'retention'
    length_mm: float
    section: CrossSection


def _mid(size: Any) -> float:
    return float(size)


class Chip:
    """A chip spec, indexed. Pure reader: ids in, geometry out."""

    def __init__(self, spec: dict) -> None:
        self.spec = spec
        self.id = spec.get("id", "?")
        self.chambers = {c["id"]: c for c in spec.get("chambers", [])}
        self.ports = {p["id"]: p for p in spec.get("ports", [])}
        self.channels = list(spec.get("channels", []))
        self.zones = list(spec.get("thermal_zones", []))
        self.features = list(spec.get("features", []))

    # --- places ---------------------------------------------------------------

    @property
    def place_ids(self) -> set:
        return set(self.chambers) | set(self.ports)

    def chamber(self, chamber_id: str) -> dict:
        if chamber_id not in self.chambers:
            raise GeometryError(
                f"chip {self.id} has no chamber '{chamber_id}' "
                f"(it has: {', '.join(sorted(self.chambers)) or 'none'})"
            )
        return self.chambers[chamber_id]

    def workplaces(self) -> list:
        """Chambers an op can run in, in spec order. A filter or a junction is not one."""
        return [c["id"] for c in self.spec.get("chambers", [])
                if c.get("type") not in NOT_A_WORKPLACE]

    def volume_ul(self, chamber_id: str) -> float:
        """The chamber's volume FROM THE SPEC. A round chamber's box would overstate it by
        21%, so this never recomputes it from `size`."""
        chamber = self.chamber(chamber_id)
        volume = chamber.get("volume_ul")
        if volume is None:
            raise GeometryError(
                f"chamber '{chamber_id}' of chip {self.id} has no volume_ul, and every "
                f"volume in a protocol is a number of chamber volumes: S6 must write it"
            )
        return float(volume)

    def centre(self, place_id: str) -> list:
        if place_id in self.chambers:
            return list(self.chambers[place_id]["center"])
        return list(self.ports[place_id]["position"])

    # --- cross-sections -------------------------------------------------------

    def depth_mm(self, chamber_id: str) -> float:
        chamber = self.chamber(chamber_id)
        if chamber.get("height_um"):
            return float(chamber["height_um"]) / 1000.0
        return float(chamber["size"][2])

    def is_folded(self, chamber_id: str) -> bool:
        chamber = self.chamber(chamber_id)
        words = f"{chamber.get('block', '')} {chamber.get('type', '')} {chamber_id}".lower()
        return any(token in words for token in FOLDED_BLOCKS)

    def flow_axis(self, chamber_id: str) -> int:
        """0 if the liquid crosses the chamber along x, 1 if along y, -1 if unknown.

        Read from where the channels actually meet the chamber: the inlet end and the
        outlet end differ most along the flow axis. The width across the flow is then the
        OTHER in-plane side.
        """
        ins = [self._chamber_end(ch, chamber_id) for ch in self.channels if ch.get("to") == chamber_id]
        outs = [self._chamber_end(ch, chamber_id) for ch in self.channels if ch.get("from") == chamber_id]
        ins = [p for p in ins if p]
        outs = [p for p in outs if p]
        if not ins or not outs:
            return -1
        dx = max(abs(a[0] - b[0]) for a in ins for b in outs)
        dy = max(abs(a[1] - b[1]) for a in ins for b in outs)
        if math.isclose(dx, dy, rel_tol=0.05):
            return -1
        return 0 if dx > dy else 1

    def _chamber_end(self, channel: dict, chamber_id: str) -> list:
        """Where a channel meets a chamber, from its path waypoints if it has them."""
        path = channel.get("path")
        if path:
            return list(path[-1] if channel.get("to") == chamber_id else path[0])
        other = channel["from"] if channel.get("to") == chamber_id else channel["to"]
        if other in self.place_ids:
            return self.centre(other)[:2]
        return []

    def cross_section(self, chamber_id: str) -> CrossSection:
        """The real flow cross-section of a chamber. Raises rather than guess a bounding box."""
        chamber = self.chamber(chamber_id)
        height = self.depth_mm(chamber_id)
        mixer = chamber.get("mixer") or {}
        if mixer.get("channel_width_mm"):
            return CrossSection(
                float(mixer["channel_width_mm"]), height,
                f"the folded channel's own width ({mixer['channel_width_mm']:g} mm), not the "
                f"{chamber['size'][0]:g} x {chamber['size'][1]:g} mm bounding box of the part",
            )
        if self.is_folded(chamber_id):
            raise GeometryError(
                f"chamber '{chamber_id}' of chip {self.id} is a folded channel "
                f"({chamber.get('block') or chamber.get('type')}) but states no "
                f"mixer.channel_width_mm. Its {chamber['size'][0]:g} x {chamber['size'][1]:g} mm "
                f"bounding box is NOT the width across the flow - using it put one shear "
                f"number out by 43x - so S6 must write the channel width into the spec."
            )
        sx, sy = float(chamber["size"][0]), float(chamber["size"][1])
        axis = self.flow_axis(chamber_id)
        if axis == 0:
            return CrossSection(sy, height, f"the chamber's y side ({sy:g} mm), across a flow that "
                                            f"crosses it along x")
        if axis == 1:
            return CrossSection(sx, height, f"the chamber's x side ({sx:g} mm), across a flow that "
                                            f"crosses it along y")
        return CrossSection(min(sx, sy), height,
                            f"the chamber's narrower in-plane side ({min(sx, sy):g} mm): its "
                            f"channels do not say which way the liquid crosses it, so the "
                            f"narrower side is used, which under-estimates the safe flow")

    def channel_section(self, channel: dict) -> CrossSection:
        return CrossSection(float(channel["width_mm"]), float(channel["depth_mm"]),
                            f"channel '{channel.get('id')}' as drawn")

    def channel_length_mm(self, channel: dict) -> float:
        """Polyline length of the drawn path; without a path, centre to centre."""
        path = channel.get("path")
        if path and len(path) >= 2:
            return sum(math.dist(path[i][:2], path[i + 1][:2]) for i in range(len(path) - 1))
        a, b = channel.get("from"), channel.get("to")
        if a in self.place_ids and b in self.place_ids:
            return math.dist(self.centre(a)[:2], self.centre(b)[:2])
        raise GeometryError(
            f"channel '{channel.get('id')}' of chip {self.id} has no path and connects "
            f"'{a}' -> '{b}', at least one of which is not a place on the chip"
        )

    def retention(self, chamber_id: str) -> dict:
        return self.chamber(chamber_id).get("retention") or {}

    # --- routes ---------------------------------------------------------------

    def out_edges(self, place_id: str) -> list:
        return [ch for ch in self.channels if ch.get("from") == place_id]

    def in_edges(self, place_id: str) -> list:
        return [ch for ch in self.channels if ch.get("to") == place_id]

    def route(self, start: str, end: str) -> list:
        """Channels from `start` to `end`, following the drawn direction. [] if unreachable.

        Breadth-first and deterministic: the channels are visited in spec order, so the
        same chip always gives the same route.
        """
        queue = [(start, [])]
        seen = {start}
        while queue:
            place, so_far = queue.pop(0)
            if place == end:
                return so_far
            for channel in self.out_edges(place):
                nxt = channel.get("to")
                if nxt in seen:
                    continue
                seen.add(nxt)
                queue.append((nxt, so_far + [channel]))
        return []

    def reaches(self, start: str, end: str) -> bool:
        return start == end or bool(self.route(start, end))

    def feeds(self, chamber_id: str) -> list:
        """Inlet ports the liquid in this chamber can be driven from, in spec order."""
        return [pid for pid, port in self.ports.items()
                if port.get("type") == "inlet" and self.reaches(pid, chamber_id)]

    def drains(self, chamber_id: str) -> list:
        """Outlet ports this chamber can be drained to, in spec order."""
        return [pid for pid, port in self.ports.items()
                if port.get("type") == "outlet" and self.reaches(chamber_id, pid)]

    def segments(self, feed: str, chamber_id: str, vent: str) -> list:
        """Every piece of the route the liquid travels: feed channels, the chamber, its
        retention feature, then the drain channels. Used for the pressure drop."""
        out: list = []
        for channel in self.route(feed, chamber_id):
            out.append(Segment(str(channel.get("id")), "channel",
                               self.channel_length_mm(channel), self.channel_section(channel)))
        section = self.cross_section(chamber_id)
        chamber = self.chamber(chamber_id)
        axis = self.flow_axis(chamber_id)
        along = float(chamber["size"][0] if axis != 1 else chamber["size"][1])
        out.append(Segment(chamber_id, "chamber", along, section))
        gap = (self.retention(chamber_id) or {}).get("opening_um")
        if gap:
            out.append(Segment(f"{chamber_id}_retention", "retention", 0.0,
                               CrossSection(section.width_mm, float(gap) / 1000.0,
                                            f"retention opening {gap:g} um")))
        for channel in self.route(chamber_id, vent):
            out.append(Segment(str(channel.get("id")), "channel",
                               self.channel_length_mm(channel), self.channel_section(channel)))
        return out

    # --- ports ----------------------------------------------------------------

    def port_role(self, port_id: str) -> str:
        return str(self.ports.get(port_id, {}).get("role") or "")

    def capsule_passable(self, port_id: str) -> bool:
        return bool(self.ports.get(port_id, {}).get("capsule_passable"))

    def port_ids(self) -> list:
        return [p["id"] for p in self.spec.get("ports", [])]

    # --- thermal --------------------------------------------------------------

    def thermal_zone(self, chamber_id: str) -> dict:
        """The declared zone covering this chamber, or {} when there is none."""
        for zone in self.zones:
            if chamber_id in zone.get("chambers", []):
                return zone
        return {}

    def zone_holds(self, chamber_id: str, temp_c: float) -> str:
        """'' when the chamber's declared zone can hold temp_c, else why not."""
        zone = self.thermal_zone(chamber_id)
        if not zone:
            return (f"chamber '{chamber_id}' is in no declared thermal zone of chip {self.id}, "
                    f"so nothing on the chip can hold it at a temperature")
        low, high = zone.get("min_temp_c"), zone.get("max_temp_c")
        if low is not None and temp_c < float(low) - 1e-9:
            return (f"{temp_c:g} C is below the {low:g} C floor of thermal zone "
                    f"'{zone['id']}'")
        if high is not None and temp_c > float(high) + 1e-9:
            return (f"{temp_c:g} C is above the {high:g} C ceiling of thermal zone "
                    f"'{zone['id']}'")
        return ""

    def curing_wavelength_nm(self, chamber_id: str) -> float:
        """From the curing window over this chamber, or 0.0 when the chip declares none."""
        for feature in self.features:
            if feature.get("type") == "curing_window" and chamber_id in feature.get("covers", []):
                if feature.get("wavelength_nm"):
                    return float(feature["wavelength_nm"])
        return 0.0

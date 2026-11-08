"""The router: every connection as a channel on ONE fluidic layer, with no crossings.

HOW PLANARITY IS GUARANTEED - three independent mechanisms, in this order:

  1. BY CONSTRUCTION. Nets are routed one at a time, and a routed net is rasterised into
     the blocked mask grown by min_wall + both half widths (obstacles.blocked_mask). Net k
     therefore has NO legal cell on the far side of nets 1..k-1: a crossing would need a
     blocked cell and the search never stands on one. Chambers, port holes and the board
     rim are in the same mask, so "no crossing", "min_wall clearance" and "never over or
     beside an unconnected port" are one mechanism, not three.
  2. BY ORDERING AND RIP-UP. Routing one net at a time can still paint a later net into a
     corner. When a net has no path, the router rips up the already-routed nets whose body
     lies in the failed net's ideal corridor, raises a congestion cost on the cells they
     fought over (PathFinder-style negotiated congestion), reprioritises the failed net to
     the front and starts the pass again. Bounded (`max_passes`), and every rip-up is in
     the report.
  3. BY INDEPENDENT RE-CHECK. The emitted chip is handed to planarity.check(), which
     re-derives every clearance FROM THE EMITTED `path` AND WIDTHS ALONE - not from the
     router's internal state - and to tools/check_chip_spec.crossings(). If either finds
     anything, S7 returns unroutable with the conflict. A crossing is never emitted to get
     a result: the stage would rather send the chip back to S6 or S4.

NET ORDERING. Short nets first, by Manhattan span between the parts' centres, with ties
broken by fewer alternatives (a net between two small parts has fewer legal mouths). Short
first because a short net rarely needs a detour and leaves a short obstacle behind, so it
constrains the board least per millimetre of wire; and because when it does go wrong, the
rip-up loop promotes the net that failed, which converges on hardest-first exactly where
hardest-first is needed. Nets sharing a terminal are kept adjacent in the order so the
mouths they take on that part end up neighbours rather than scattered round its outline.

WHAT IS NOT DONE HERE. Nothing is moved. S7 may not nudge a chamber or a port by a
micrometre - that is S6's placement, and silently moving it would make the chip disagree
with the protocol, the STL names and the operator's port map. If the board cannot be
routed as placed, S7 says so and asks for a re-place (S6) or a re-partition (S4).

Standard library only (plus the pipeline's own assay2chip.lib).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from . import geometry as geo
from . import maze, meander, planarity
from .connections import Net
from .obstacles import (Feature, Grid, Obstacle, Terminal, blocked_mask, features_of,
                        net_obstacle, one_feature_with, static_obstacles,
                        terminal_is_clear, terminals_for)

DEFAULT_GRID_MM = 0.05          # the coarsest S7 is allowed to use
DEFAULT_BEND_MM = 0.20
DEFAULT_MAX_PASSES = 8
HISTORY_STEP = 0.5              # congestion cost added to a contested cell per rip-up
HUG_COST = 0.02                 # soft cost for a cell inside a net's OWN part's clearance


@dataclass
class RoutedNet:
    """One connection, routed."""

    net: Net
    path: list[tuple[float, float]]
    width_mm: float
    depth_mm: float
    from_side: str
    to_side: str
    length_mm: float
    meander: meander.Meander | None = None
    ripped_up: int = 0
    block: str = "straight_channel"
    openings_as_asked: tuple[str, str] = ("point", "point")

    @property
    def corners(self) -> int:
        return geo.corners(self.path)

    def as_channel(self, carries_from_net: bool = True) -> dict:
        """The channel object for the chip spec. `path` is the whole point of this stage."""
        ch = {
            "id": self.net.emit_id,
            "from": self.net.a,
            "to": self.net.b,
            "width_mm": round(self.width_mm, 4),
            "depth_mm": round(self.depth_mm, 4),
            "block": self.block,
            "path": [[round(x, 4), round(y, 4)] for x, y in self.path],
        }
        if carries_from_net:
            ch["carries"] = self.net.carries
        if self.net.capsule_passable is not None:
            ch["capsule_passable"] = self.net.capsule_passable
        note = self.net.note.strip()
        bits = [f"S7 route: {self.corners} corner(s), {self.length_mm:.2f} mm centreline, "
                f"mouths {self.from_side}/{self.to_side}"]
        if self.meander and self.meander.folds:
            bits.append(f"meandered to {self.meander.target_mm:.2f} mm target "
                        f"({self.meander.folds} fold(s), amplitude "
                        f"{self.meander.amplitude_mm:.2f} mm)")
        if self.ripped_up:
            bits.append(f"rerouted after {self.ripped_up} rip-up(s)")
        ch["note"] = ((note + " ") if note else "") + "; ".join(bits) + "."
        return ch

    def as_dict(self) -> dict:
        d = {"net": self.net.id, "channel": self.net.emit_id, "from": self.net.a,
             "to": self.net.b, "carries": self.net.carries,
             "width_mm": round(self.width_mm, 4), "depth_mm": round(self.depth_mm, 4),
             "length_mm": round(self.length_mm, 4), "corners": self.corners,
             "block": self.block, "from_side": self.from_side, "to_side": self.to_side,
             "ripped_up": self.ripped_up, "vertices": len(self.path),
             "mouth_fidelity": {"from": self.openings_as_asked[0],
                                "to": self.openings_as_asked[1]}}
        if self.meander:
            d["length"] = self.meander.as_dict()
        return d


@dataclass
class Failure:
    """A net that cannot be routed, and exactly what is in the way."""

    net: Net
    reason: str
    blockers: list[str] = field(default_factory=list)
    detail: str = ""

    def as_dict(self) -> dict:
        return {"net": self.net.id, "from": self.net.a, "to": self.net.b,
                "reason": self.reason, "blockers": self.blockers, "detail": self.detail}


@dataclass
class RouteOutcome:
    ok: bool
    routed: list[RoutedNet] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    passes: int = 0
    ripups: list[dict] = field(default_factory=list)
    grid: Grid | None = None
    notes: list[str] = field(default_factory=list)


class Router:
    def __init__(self, chip: dict, nets: Sequence[Net], *, grid_mm: float = DEFAULT_GRID_MM,
                 bend_mm: float = DEFAULT_BEND_MM, max_passes: int = DEFAULT_MAX_PASSES) -> None:
        from ..lib import contracts

        if grid_mm > DEFAULT_GRID_MM + 1e-12:
            raise ValueError(
                f"grid of {grid_mm:g} mm is coarser than the {DEFAULT_GRID_MM:g} mm S7 is "
                f"allowed to use: a coarse grid hides a thin wall rather than finding it")
        self.chip = chip
        self.nets = list(nets)
        self.contracts = contracts
        self.min_wall = contracts.floor("min_wall_mm")
        self.min_open = contracts.floor("min_open_channel_mm")
        self.width_factor = float(contracts.param("capsule_channel.width_factor_low"))
        self.swell = contracts.capsule_swell_factor()
        self.depth_mm = self._depth_mm()
        self.features = features_of(chip)
        self.statics = static_obstacles(self.features)
        self.grid = Grid(chip["footprint"]["x"], chip["footprint"]["y"], grid_mm)
        self.bend_mm = bend_mm
        self.max_passes = max_passes
        self.history: dict[int, float] = {}
        self.notes: list[str] = []

    # --- sizing -------------------------------------------------------------------------

    def _depth_mm(self) -> float:
        """One etch depth for the whole layer, taken from the chip, capped by the contract.

        contracts/blocks.json envelope: ONE fluidic layer at ONE depth. A channel deeper
        than its chambers would be a second depth no single embossing tool can make, so the
        channel depth is the chambers' depth.
        """
        depths = sorted({round(c["size"][2], 6) for c in self.chip.get("chambers", [])})
        cap = self.contracts.max_depth_mm()
        if not depths:
            return cap
        if len(depths) > 1:
            self.notes.append(
                f"the placed chambers are at {len(depths)} different depths "
                f"({', '.join(f'{d:g}' for d in depths)} mm); S7 cuts every channel at the "
                f"shallowest, {min(depths):g} mm, because one etch depth is all one tool "
                f"makes - S6 should settle on one depth")
        return min(min(depths), cap)

    def width_for(self, net: Net) -> float:
        """The width this net must have: the widest of every rule that applies to it.

        min_open_channel_mm always; the particle rule when it carries particles (the factor
        from contracts/parameters.json capsule_channel.width_factor_low against the SWOLLEN
        diameter, because the swollen capsule is the one that has to get through); whatever
        S6 asked for; and enough to satisfy the port-overlap rule where the net lands on a
        port - a bore x width band under the hole must reach the area the rule wants.
        """
        need = [self.min_open]
        if net.min_width_mm:
            need.append(float(net.min_width_mm))
        particle = planarity._particle_width_mm(
            {"carries": net.carries, "capsule_passable": net.capsule_passable},
            {"particle_um": net.particle_um, "particle_swollen": net.particle_swollen},
            self.width_factor, self.swell, self.contracts)
        if particle is not None:
            need.append(particle)
        for end in (net.a, net.b):
            f = self.features.get(end)
            if f is not None and f.kind == "port":
                need.append(self._port_band_width(f))
        return max(need)

    def _port_band_width(self, f: Feature) -> float:
        """Width that makes a channel crossing this hole meet the port-overlap rule.

        tools/check_chip_spec.py wants a quarter of the hole's own area for a hole under
        2 x 2 mm, and a band of `width` across a `side` hole gives side x width, so
        width >= side / 4. A tenth is added because the rule is sampled on a 50 x 50 grid
        and a result that lands exactly on the threshold is a coin toss.
        """
        side = f.port_side_mm
        area = side * side
        need = 1.0 if area >= 4.0 else 0.25 * area
        return need / side * 1.10

    # --- ordering -----------------------------------------------------------------------

    def order(self, nets: Sequence[Net], promote: Iterable[str] = ()) -> list[Net]:
        promote = list(promote)
        rank = {nid: i for i, nid in enumerate(promote)}

        def span(net: Net) -> float:
            a, b = self.features.get(net.a), self.features.get(net.b)
            if a is None or b is None:
                return 0.0
            return abs(a.centre[0] - b.centre[0]) + abs(a.centre[1] - b.centre[1])

        def mouths(net: Net) -> int:
            out = 0
            for end in (net.a, net.b):
                f = self.features.get(end)
                if f is None:
                    continue
                out += 4 if f.kind == "port" else int(
                    2 * ((f.rect[2] - f.rect[0]) + (f.rect[3] - f.rect[1])) / self.grid.res)
            return out

        ordered = sorted(nets, key=lambda n: (rank.get(n.id, len(promote)), span(n), mouths(n), n.id))
        # Keep nets that share a part adjacent, so their mouths land next to each other.
        out: list[Net] = []
        pending = list(ordered)
        while pending:
            cur = pending.pop(0)
            out.append(cur)
            touching = [n for n in pending if {n.a, n.b} & {cur.a, cur.b}]
            for n in touching:
                pending.remove(n)
                out.append(n)
        return out

    # --- one net ------------------------------------------------------------------------

    def obstacles_for(self, net: Net, placed: dict[str, RoutedNet]) -> list[Obstacle]:
        """Static parts plus every already-routed net, with the `meeting` exemptions."""
        obs = list(self.statics)
        for other in placed.values():
            o = net_obstacle(other.net.emit_id, other.path, other.width_mm)
            shared = {net.a, net.b} & {other.net.a, other.net.b}
            for sid in shared:
                f = self.features.get(sid)
                if f is None or not f.is_meeting:
                    continue
                # A port or a junction is where streams are MEANT to converge, so the two
                # nets may touch - but only within a bubble around that part, never further.
                reach = (max(f.rect[2] - f.rect[0], f.rect[3] - f.rect[1]) / 2
                         + self.min_wall + o.half_width + 2 * self.grid.res)
                o.near_exempt.append((f.centre[0], f.centre[1], reach))
            obs.append(o)
        return obs

    def route_one(self, net: Net, placed: dict[str, RoutedNet]
                  ) -> tuple[RoutedNet | None, Failure | None, bytearray]:
        width = self.width_for(net)
        a, b = self.features.get(net.a), self.features.get(net.b)
        if a is None or b is None:
            missing = [e for e in (net.a, net.b) if e not in self.features]
            return None, Failure(net, "unknown part",
                                 detail=f"the placed chip has no part called "
                                        f"{', '.join(repr(m) for m in missing)}"), bytearray()
        obs = self.obstacles_for(net, placed)
        # A port and the pad it is drilled into are one feature, so exempting the pad has to
        # exempt the port too, or the port fences off every mouth on its own pad.
        exempt = one_feature_with({net.a, net.b}, self.features)
        mask = blocked_mask(self.grid, obs, width, self.min_wall, exempt)
        starts, a_asked = self._mouths(net.a_end, a, width, obs, mask, exempt)
        goals, b_asked = self._mouths(net.b_end, b, width, obs, mask, exempt)
        if not starts or not goals:
            end = net.a if not starts else net.b
            f = self.features[end]
            wanted = sum(1 for n in self.nets if end in (n.a, n.b))
            return None, Failure(
                net, "no legal opening",
                blockers=self._blockers_near(f, obs, width),
                detail=f"'{end}' is {f.rect[2] - f.rect[0]:.2f} x {f.rect[3] - f.rect[1]:.2f} mm, "
                       f"{wanted} connection(s) have to meet it, and a {width * 1000:.0f} um "
                       f"channel at the {self.min_wall:g} mm minimum wall has nowhere to leave "
                       f"it: " + self._wall_census(f, width, obs, mask, exempt)
                       + ". This is a placement problem, not a routing one - the part needs "
                         "room on at least one wall, or has to be bigger."), mask
        return self._search(net, a, b, width, obs, mask, starts, goals,
                            a_asked, b_asked, placed)

    def _wall_census(self, f: Feature, width: float, obs: Sequence[Obstacle],
                     mask: bytearray, exempt: Iterable[str]) -> str:
        """Wall by wall, how many mouths exist and what is sitting on them.

        The useful half of a 'no legal opening' failure. "every mouth is blocked" sends S6
        looking at the whole board; "east is blocked by outlet_1_pad, north and south by
        ch_conn_6, west is off the board" names the part to move.
        """
        bits = []
        for side in ("east", "north", "west", "south"):
            every = terminals_for(self.grid, f, width, self.min_wall, [side])
            if not every:
                bits.append(f"{side}: no mouth fits this wall")
                continue
            if self._usable(every, obs, mask, width, exempt):
                bits.append(f"{side}: free")
                continue
            culprits = sorted({f"{o.kind} '{o.owner}'" for o in obs
                               if o.owner not in set(exempt)
                               and min(o.distance(*t.anchor) for t in every)
                               < width / 2 + self.min_wall + o.half_width})
            off_board = any(not (width / 2 + self.min_wall <= t.lead[-1][0]
                                 <= self.grid.fx - width / 2 - self.min_wall
                                 and width / 2 + self.min_wall <= t.lead[-1][1]
                                 <= self.grid.fy - width / 2 - self.min_wall)
                            for t in every)
            why = ", ".join(culprits) or ("off the board" if off_board else "the board rim")
            bits.append(f"{side}: blocked by {why}")
        return "; ".join(bits)

    def _search(self, net: Net, a: Feature, b: Feature, width: float, obs, mask,
                starts, goals, a_asked, b_asked, placed):
        result = maze.search(self.grid, mask, starts, goals, bend_mm=self.bend_mm,
                             history=self._costs(net, width))
        if not result.ok:
            seeds = [t.cell for t in starts]
            reach = maze.reachable_cells(self.grid, mask, seeds)
            return None, Failure(
                net, "fenced in",
                blockers=self._blockers_between(net, obs, width, placed),
                detail=f"{result.reason}: a {width * 1000:.0f} um channel at "
                       f"{self.min_wall:g} mm clearance can reach {len(reach)} of "
                       f"{self.grid.cells} cells from '{net.a}', and none of them is a mouth "
                       f"on '{net.b}'"), mask

        path = self._assemble(result)
        routed = RoutedNet(net, path, width, self.depth_mm, result.start.side,
                           result.goal.side, geo.length(path))
        routed.block = block_for(routed.path, meandered=False)
        routed.openings_as_asked = (a_asked, b_asked)
        return routed, None, mask

    def _mouths(self, end, feature: Feature, width: float, obs: Sequence[Obstacle],
                mask: bytearray, exempt: Iterable[str]) -> tuple[list[Terminal], str]:
        """The mouths this net may use on one part, and how much of S6's request survived.

        S6 names the opening three ways over: a `position`, how far along the wall it may
        move (`opening_width_mm`), and the wall itself (`direction`, the outward normal).
        Those are not decoration. The position is chosen so two openings on one part cannot
        collide, and the wall is chosen because a block's openings are not interchangeable -
        a trap_chamber's weir is on its outlet side, so an inlet moved to that wall arrives
        behind the retention feature and the part stops working.

        So this is a CASCADE, widest fidelity first, and what it settled for is returned:

          "point"  the mouth S6 asked for, inside its stated opening width;
          "wall"   elsewhere on the wall S6 asked for;
          "any"    another wall entirely - which may break the part, so S7 puts it in the
                   report's `unresolved` rather than just doing it;
          ""       nothing legal anywhere, which is the "no legal opening" failure.

        The first version only cascaded from "point" to "wall", because `every` was already
        filtered to the requested side - so a chip whose requested wall was fenced in was
        declared unroutable while three clear walls went unexamined.
        """
        sides = (end.sides or None) if end else None
        point = end.point if end else None
        on_side = terminals_for(self.grid, feature, width, self.min_wall, sides)
        if point is not None:
            reach = max(2 * self.grid.res,
                        ((end.reach_mm or 0.0) - width) / 2 if end.reach_mm else 0.0)
            near = [t for t in on_side if math.dist(t.anchor, point) <= reach + 1e-9]
            usable = self._usable(near, obs, mask, width, exempt)
            if usable:
                return usable, "point"
        usable = self._usable(on_side, obs, mask, width, exempt)
        if usable:
            return usable, "wall" if (point is not None or sides) else "point"
        if not sides:
            return [], ""
        everywhere = terminals_for(self.grid, feature, width, self.min_wall, None)
        return self._usable(everywhere, obs, mask, width, exempt), "any"

    def _usable(self, terms: Sequence[Terminal], obs: Sequence[Obstacle], mask: bytearray,
                width: float, exempt: Iterable[str]) -> list[Terminal]:
        """Mouths that are legal: the stub is clear AND the first cell is not blocked.

        Both halves matter. `terminal_is_clear` judges the stub, which is off the grid; the
        mask judges the cell the search would stand on. Filtering here rather than inside
        the search is what lets a failure say "no legal opening on this part" - a placement
        problem - instead of the vaguer "fenced in".
        """
        return [t for t in terms
                if self.grid.inside(*t.cell) and not mask[self.grid.index(*t.cell)]
                and terminal_is_clear(t, obs, width, self.min_wall, exempt)]

    def _costs(self, net: Net, width: float) -> dict[int, float]:
        """Congestion history, plus a SOFT penalty for hugging the net's own two parts.

        A net's own `from`/`to` parts are exempt from the blocked mask - they have to be,
        or the channel could not touch them. That exemption is global, though, so nothing
        stops a route from running the length of its own chamber's wall at zero clearance
        and leaving a rib too thin to print. It is not a short circuit (the two are one
        fluid path already, which is why cad/make_figures.py and tools/check_chip_spec.py
        skip the pair outright), so it is priced rather than forbidden: the route goes round
        at a proper distance whenever there is room, and planarity.check still reports the
        rib if it ends up thin.
        """
        costs = dict(self.history)
        band = width / 2 + self.min_wall
        for end in (net.a, net.b):
            f = self.features.get(end)
            if f is None:
                continue
            x0, y0, x1, y1 = f.rect
            i0, j0 = self.grid.cell_containing(x0 - band, y0 - band)
            i1, j1 = self.grid.cell_containing(x1 + band, y1 + band)
            for i in range(i0, i1 + 1):
                for j in range(j0, j1 + 1):
                    if geo.dist_point_rect(self.grid.centre(i, j), f.rect) < band:
                        idx = self.grid.index(i, j)
                        costs[idx] = costs.get(idx, 0.0) + HUG_COST
        return costs

    def _assemble(self, result: maze.SearchResult) -> list[tuple[float, float]]:
        """Mouth, lead-in, the cells, lead-out, mouth - simplified to real corners only."""
        pts: list[tuple[float, float]] = [result.start.anchor, *result.start.lead]
        pts += [self.grid.centre(i, j) for i, j in result.cells[1:]]
        pts += [*reversed(result.goal.lead[:-1]), result.goal.anchor]
        return geo.simplify(pts)

    def _blockers_near(self, f: Feature, obs: Sequence[Obstacle], width: float) -> list[str]:
        reach = width / 2 + self.min_wall + max(f.rect[2] - f.rect[0], f.rect[3] - f.rect[1])
        out = []
        for o in obs:
            if o.owner == f.id:
                continue
            if o.distance(*f.centre) <= reach:
                out.append(f"{o.kind} '{o.owner}'")
        return sorted(out)

    def _blockers_between(self, net: Net, obs: Sequence[Obstacle], width: float,
                          placed: dict[str, RoutedNet]) -> list[str]:
        """What sits on the straight line the net would have liked to take."""
        a, b = self.features[net.a].centre, self.features[net.b].centre
        out = []
        for o in obs:
            if o.owner in (net.a, net.b):
                continue
            gap = (geo.dist_seg_rect(a, b, o.rect) if o.rect is not None
                   else min(geo.dist_seg_seg(a, b, c, d) for c, d in geo.segments(o.path or [])))
            if gap < width / 2 + self.min_wall + o.half_width:
                out.append(f"{o.kind} '{o.owner}'")
        return sorted(out)

    # --- a necessary condition, checked before any searching -----------------------------

    def mouth_budget(self) -> list[Failure]:
        """Parts asked to take more channels than their outline has room for mouths.

        A COUNTING ARGUMENT, so it cannot be wrong: a wall of length L can hold at most
        floor((L - 2 x keep) / (w + min_wall)) + 1 mouths of width w, where keep is
        w/2 + min_wall at the corners, and a wall shorter than that holds exactly one (the
        small-part exception in obstacles.chamber_terminals). Sum over the four walls and
        compare with the number of nets that have to land on the part. Neighbours and other
        nets can only REDUCE the real capacity, so an over-subscribed part here is
        definitely over-subscribed, and no routing order, rip-up or regrid will help.

        Worth doing before the search because the answer is so much more useful: "lyse_26
        is 0.83 x 0.83 mm and four channels have to meet it, which fits three" tells S6
        exactly what to change, where eight passes of rip-up and "fenced in" does not.

        Meeting features - ports, junctions and port pads - are skipped: streams are meant
        to converge there, so their mouths may share space.
        """
        per_part: dict[str, list[Net]] = {}
        for net in self.nets:
            for end in (net.a, net.b):
                per_part.setdefault(end, []).append(net)
        out = []
        for part, nets in sorted(per_part.items()):
            f = self.features.get(part)
            if f is None or f.kind == "port" or f.is_meeting or len(nets) < 2:
                continue
            width = max(self.width_for(n) for n in nets)
            keep = width / 2 + self.min_wall
            pitch = width + self.min_wall
            capacity = 0
            walls = []
            for length in ((f.rect[2] - f.rect[0]), (f.rect[3] - f.rect[1])):
                room = length - 2 * keep
                n = (int(room / pitch) + 1) if room >= 0 else (1 if length >= width else 0)
                capacity += 2 * n
                walls.append((length, n))
            if capacity >= len(nets):
                continue
            out.append(Failure(
                nets[0], "part over-subscribed",
                blockers=[part],
                detail=(f"'{part}' is {f.rect[2] - f.rect[0]:.2f} x {f.rect[3] - f.rect[1]:.2f} mm "
                        f"and {len(nets)} connection(s) have to meet it "
                        f"({', '.join(n.id for n in nets)}), but its outline holds at most "
                        f"{capacity} mouth(s) of {width * 1000:.0f} um at the "
                        f"{self.min_wall:g} mm minimum wall "
                        f"({' and '.join(f'{n} per {L:.2f} mm wall' for L, n in walls)}). "
                        f"This is arithmetic, not a routing order: the part has to be bigger, "
                        f"or some of those lines have to be merged before they reach it.")))
        return out

    # --- the whole board, with rip-up ----------------------------------------------------

    def run(self) -> RouteOutcome:
        over = self.mouth_budget()
        if over:
            return RouteOutcome(False, [], over, 0, [], self.grid, list(self.notes))
        promote: list[str] = []
        ripups: list[dict] = []
        seen: dict[tuple, int] = {}
        for npass in range(1, self.max_passes + 1):
            placed: dict[str, RoutedNet] = {}
            failures: list[Failure] = []
            for net in self.order(self.nets, promote):
                routed, failure, _mask = self.route_one(net, placed)
                if routed is None:
                    failures.append(failure)
                    break
                placed[net.id] = routed
            if not failures:
                out = RouteOutcome(True, [placed[n.id] for n in self.nets if n.id in placed],
                                   [], npass, ripups, self.grid, list(self.notes))
                self._apply_length_targets(out, placed)
                return out

            failed = failures[0]
            if failed.reason == "unknown part" or failed.reason == "no legal opening":
                return RouteOutcome(False, list(placed.values()), failures, npass, ripups,
                                    self.grid, list(self.notes))
            victims = self._rip_up(failed, placed)
            ripups.append({"pass": npass, "net": failed.net.id, "reason": failed.reason,
                           "ripped_up": [v for v in victims],
                           "blockers": failed.blockers,
                           "why": "the failed net is promoted to the front of the order and "
                                  "the cells the two fought over cost more from now on"})
            for nid in victims:
                net = next((n for n in self.nets if n.id == nid), None)
                if net is not None:
                    net.audit["ripped_up"] = int(net.audit.get("ripped_up", 0)) + 1
            # To the FRONT, even if it has failed before. Appending it instead left the net
            # that failed four passes running sitting fourth in the order behind three nets
            # that had since succeeded, so it never got the first pick it was promoted for.
            if failed.net.id in promote:
                promote.remove(failed.net.id)
            promote.insert(0, failed.net.id)
            if not victims:
                # Nothing left to give: the board, not the order, is the problem.
                return RouteOutcome(False, list(placed.values()), failures, npass, ripups,
                                    self.grid, list(self.notes))
            signature = (failed.net.id, tuple(victims))
            seen[signature] = seen.get(signature, 0) + 1
            if seen[signature] >= 3:
                # The same net losing to the same nets three times is not an ordering
                # problem that another pass will solve, and saying so beats burning the
                # budget and then reporting the same conflict anyway.
                self.notes.append(
                    f"gave up after pass {npass}: net '{failed.net.id}' lost to the same "
                    f"net(s) ({', '.join(victims)}) three times even routed first, so no "
                    f"ordering of these nets fits this placement")
                return RouteOutcome(False, list(placed.values()), failures, npass, ripups,
                                    self.grid, list(self.notes))
        return RouteOutcome(False, [], failures, self.max_passes, ripups, self.grid,
                            list(self.notes))

    def _rip_up(self, failed: Failure, placed: dict[str, RoutedNet]) -> list[str]:
        """Remove the routed nets in the failed net's way and make their cells expensive.

        The corridor is the route the failed net WOULD have taken with the parts in place
        and no other nets at all - searched for, not guessed at from the straight line
        between the two parts, because the line between two parts on opposite corners of
        the board says nothing about which gap the net actually needs. Every routed net
        whose body is within a wall of that corridor is ripped up, and its cells get a
        congestion cost, so on the next pass it has a reason to go round instead of the
        failed net having to.
        """
        width = self.width_for(failed.net)
        corridor = self._ideal_corridor(failed.net, width)
        victims = []
        for nid, routed in placed.items():
            if nid == failed.net.id:
                continue
            need = width / 2 + self.min_wall + routed.width_mm / 2
            gap = min(geo.dist_seg_seg(a, b, c, d)
                      for a, b in geo.segments(corridor)
                      for c, d in geo.segments(routed.path))
            if gap < need:
                victims.append(nid)
                for x, y in _sample_path(routed.path, self.grid.res):
                    i, j = self.grid.cell_containing(x, y)
                    if self.grid.inside(i, j):
                        idx = self.grid.index(i, j)
                        self.history[idx] = self.history.get(idx, 0.0) + HISTORY_STEP
        return sorted(victims)

    def _ideal_corridor(self, net: Net, width: float) -> list[tuple[float, float]]:
        """The route this net would take on an empty board: parts only, no other nets."""
        exempt = one_feature_with({net.a, net.b}, self.features)
        mask = blocked_mask(self.grid, self.statics, width, self.min_wall, exempt)
        a, b = self.features[net.a], self.features[net.b]
        starts = self._usable(terminals_for(self.grid, a, width, self.min_wall,
                                            net.a_sides or None),
                              self.statics, mask, width, exempt)
        goals = self._usable(terminals_for(self.grid, b, width, self.min_wall,
                                           net.b_sides or None),
                             self.statics, mask, width, exempt)
        result = maze.search(self.grid, mask, starts, goals, bend_mm=self.bend_mm)
        if result.ok:
            return self._assemble(result)
        return [a.centre, b.centre]

    # --- length targets ------------------------------------------------------------------

    def _apply_length_targets(self, out: RouteOutcome, placed: dict[str, RoutedNet]) -> None:
        """Meander every net that has a target, and give a balance group a common one.

        A balance group's target is the length that matches the group's MOST RESISTIVE
        branch, computed with operator/network.py's own resistance formula, so S7 and the
        hydraulic solver agree about what "balanced" means.

        Each fold is probed against a mask built from EVERY OTHER FINAL ROUTE, including the
        nets that were routed after this one and the folds already added to earlier nets, so
        a serpentine cannot grow into a neighbour that did not exist when this net was
        searched.
        """
        mu = self.contracts.viscosity_for(37.0).pa_s
        groups: dict[str, list[RoutedNet]] = {}
        for r in out.routed:
            if r.net.balance_group:
                groups.setdefault(r.net.balance_group, []).append(r)
        targets: dict[str, float] = {r.net.id: r.net.target_length_mm
                                     for r in out.routed if r.net.target_length_mm}
        for name, members in groups.items():
            if len(members) < 2:
                out.notes.append(
                    f"balance group '{name}' has one member, so there is nothing to balance "
                    f"against; its own target_length_mm is used if S6 gave one")
                continue
            group = [(r.net.id, r.length_mm, r.width_mm, r.depth_mm) for r in members]
            want = meander.resistance_targets(group, mu)
            for nid, L in want.items():
                stated = targets.get(nid)
                targets[nid] = max(L, stated) if stated else L

        for r in out.routed:
            target = targets.get(r.net.id)
            if not target:
                continue
            others = {nid: p for nid, p in placed.items() if nid != r.net.id}
            obs = self.obstacles_for(r.net, others)
            mask = blocked_mask(self.grid, obs, r.width_mm, self.min_wall,
                            one_feature_with({r.net.a, r.net.b}, self.features))
            m = meander.fold_to_length(r.path, target, r.width_mm, self.min_wall,
                                       self.grid, mask)
            r.meander = m
            r.path = m.path
            r.length_mm = m.achieved_mm
            r.block = block_for(r.path, meandered=bool(m.folds))
            if not m.met:
                out.notes.append(
                    f"net '{r.net.id}' wanted {target:.2f} mm and got {m.achieved_mm:.2f} mm: "
                    f"{m.why or 'the fold did not fit'}")


def _sample_path(path: Sequence[Sequence[float]], step: float) -> list[tuple[float, float]]:
    out = []
    for a, b in geo.segments(path):
        n = max(1, int(math.ceil(math.dist(a, b) / step)))
        for k in range(n + 1):
            out.append((a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n))
    return out


def block_for(path: Sequence[Sequence[float]], meandered: bool) -> str:
    """The contracts/blocks.json id this geometry actually is.

    `block` is not decoration: check_chip_spec.check_blocks errors on a block that is not
    in the library, and the CAD tool can place nothing else. A routed channel is one of
    exactly three things.
    """
    if meandered:
        return "serpentine_channel"
    return "straight_channel" if geo.corners(path) == 0 else "channel_corner"


def connection_graph_is_overfull(nets: Sequence[Net]) -> tuple[bool, str]:
    """Euler's necessary condition for a planar graph: E <= 3V - 6.

    If the CONNECTION GRAPH itself breaks this, no placement whatsoever can route it on one
    layer and there is no point sending it back to S6 - the ops have to be split differently,
    which is S4. This is only a necessary condition, so it never wrongly condemns a board:
    a graph that passes it may still be unroutable for placement reasons, and that is the
    S6 answer.
    """
    verts = {n.a for n in nets} | {n.b for n in nets}
    edges = {frozenset((n.a, n.b)) for n in nets}
    v, e = len(verts), len(edges)
    if v >= 3 and e > 3 * v - 6:
        return True, (f"the connection graph has {v} parts and {e} distinct connections, and "
                      f"any planar graph obeys E <= 3V - 6 ({3 * v - 6} here). No placement of "
                      f"these parts can be wired on one fluidic layer, so re-placing will not "
                      f"help: the ops have to be split across more chips.")
    return False, ""

"""What a router is not allowed to touch, and the grid it is rasterised onto.

THE ONE RULE THIS FILE EXISTS FOR. On a single fluidic layer there are no vias, so two
features that come within `min_wall_mm` of each other in plan view are not two features:
they are one, and the chip is a short circuit. So before a single cell is searched, every
placed chamber, every port hole, every already-routed channel and the board edge is grown
by

    min_wall_mm + (the routed channel's own width / 2) + (the obstacle's own half width)

and every grid cell whose CENTRE falls inside that grown shape is marked blocked. A path
made only of unblocked cells is therefore legal by construction, for every rule that is a
clearance rule. Nothing in the search has to remember not to cross something, because a
crossing needs a blocked cell and there are none in the path.

An already-routed net is an obstacle exactly like a chamber. That is the whole planarity
argument: net k cannot cross nets 1..k-1 because they are not empty space any more.

Two deliberate exceptions, both from contracts/blocks.json and both mirrored in
cad/make_figures.py:

  * the TWO features this net connects are exempt from the clearance, because the channel
    has to touch them - their interiors stay blocked so the search cannot shortcut THROUGH
    a chamber and out of the far wall;
  * a net that shares a terminal with this one, where that terminal is a port or a
    `junction` chamber, is exempt WITHIN A SMALL RADIUS of that shared terminal, because a
    port and a junction are places streams are meant to converge (make_figures calls that
    set `meeting`). Everywhere else the two must still clear each other.

Grid: cell (i, j) is the square [i*res, (i+1)*res) x [j*res, (j+1)*res) and the router only
ever stands at its CENTRE, ((i+0.5)*res, (j+0.5)*res). Default res is 0.05 mm, the
coarsest grid S7 is allowed to use; a 30 x 20 mm demo board is 600 x 400 = 240,000 cells.

Standard library only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Iterable, Sequence

from . import geometry as geo
from .geometry import Pt, Rect

# Directions, as indices, so a search state is (i, j, direction).
EAST, NORTH, WEST, SOUTH = 0, 1, 2, 3
STEP = {EAST: (1, 0), NORTH: (0, 1), WEST: (-1, 0), SOUTH: (0, -1)}
SIDE_OF_DIR = {EAST: "east", NORTH: "north", WEST: "west", SOUTH: "south"}
DIR_OF_SIDE = {v: k for k, v in SIDE_OF_DIR.items()}
OPPOSITE = {EAST: WEST, WEST: EAST, NORTH: SOUTH, SOUTH: NORTH}


@dataclass(frozen=True)
class Feature:
    """A placed part S7 may connect to but must never move: a chamber or a port hole."""

    id: str
    kind: str                 # "chamber" | "port"
    rect: Rect
    centre: Pt
    is_meeting: bool          # a port, or a chamber of type "junction": streams may merge
    port_side_mm: float = 0.0  # ports only: the bore, so the channel can cross the hole


@dataclass
class Obstacle:
    """Something a routed channel must keep `min_wall` away from.

    `half_width` is the obstacle's own half width: 0 for a solid (a chamber rectangle, a
    port hole), width/2 for another channel's centreline. `near_exempt` is the list of
    (x, y, radius) bubbles where this obstacle does not apply - see the module docstring.
    """

    owner: str
    kind: str                       # "chamber" | "port" | "net" | "edge"
    rect: Rect | None = None
    path: list[Pt] | None = None
    half_width: float = 0.0
    near_exempt: list[tuple[float, float, float]] = field(default_factory=list)

    def bounds(self) -> Rect:
        if self.rect is not None:
            return self.rect
        return geo.bbox(self.path or [])

    def distance(self, x: float, y: float) -> float:
        if self.rect is not None:
            return geo.dist_point_rect((x, y), self.rect)
        return geo.dist_point_path((x, y), self.path or [])

    def exempt_at(self, x: float, y: float) -> bool:
        return any(math.hypot(x - ex, y - ey) <= r for ex, ey, r in self.near_exempt)


@dataclass(frozen=True)
class Terminal:
    """Where a channel meets a part: the mouth, and the first grid cell outside it.

    anchor      the point ON the part's outline (a chamber wall) or on the far side of a
                port hole, which is where the emitted `path` starts or ends.
    lead        the extra vertices between the anchor and the cell centre, inclusive of
                the cell centre. Always axis-aligned, so a chamber mouth is a clean T and
                a DXF closed contour has no outline crossing there.
    cell        (i, j) the search starts from or must arrive at.
    direction   direction of travel from the anchor into the cell. The search must arrive
                at the far terminal travelling the OPPOSITE of its direction, which is
                what keeps the last segment perpendicular to the wall.
    """

    feature: str
    side: str
    anchor: Pt
    lead: tuple[Pt, ...]
    cell: tuple[int, int]
    direction: int


class Grid:
    """Cell centres on a fixed pitch over the whole board."""

    def __init__(self, footprint_x: float, footprint_y: float, res: float) -> None:
        if res <= 0:
            raise ValueError("grid resolution must be positive")
        self.res = float(res)
        self.fx, self.fy = float(footprint_x), float(footprint_y)
        self.nx = max(1, int(math.floor(self.fx / self.res)))
        self.ny = max(1, int(math.floor(self.fy / self.res)))

    @property
    def cells(self) -> int:
        return self.nx * self.ny

    def cx(self, i: int) -> float:
        return (i + 0.5) * self.res

    def cy(self, j: int) -> float:
        return (j + 0.5) * self.res

    def centre(self, i: int, j: int) -> Pt:
        return (self.cx(i), self.cy(j))

    def index(self, i: int, j: int) -> int:
        return i * self.ny + j

    def inside(self, i: int, j: int) -> bool:
        return 0 <= i < self.nx and 0 <= j < self.ny

    def cell_containing(self, x: float, y: float) -> tuple[int, int]:
        return (min(self.nx - 1, max(0, int(math.floor(x / self.res)))),
                min(self.ny - 1, max(0, int(math.floor(y / self.res)))))

    def first_cell_beyond(self, coord: float, axis: int, positive: bool) -> int:
        """Index of the first cell whose centre is strictly past `coord` along `axis`."""
        n = self.nx if axis == 0 else self.ny
        k = int(math.floor(coord / self.res))
        if positive:
            while k < n and (k + 0.5) * self.res <= coord + 1e-12:
                k += 1
            return k
        while k >= 0 and (k + 0.5) * self.res >= coord - 1e-12:
            k -= 1
        return k


def features_of(chip: dict) -> dict[str, Feature]:
    """Every chamber and port of a placed chip, as immutable keep-outs.

    A port's rect is its lid hole, a square of `width_um` when the spec gives one, else the
    contract bore - the same square cad/make_train_cad.py cuts and the same one
    check_chip_spec's port-overlap rule measures.
    """
    from .. lib import contracts   # late: keeps this module importable without contracts

    bore = contracts.floor("min_port_mm")
    out: dict[str, Feature] = {}
    for c in chip.get("chambers", []):
        cx, cy = c["center"][0], c["center"][1]
        sx, sy = c["size"][0], c["size"][1]
        out[c["id"]] = Feature(c["id"], "chamber",
                               (cx - sx / 2, cy - sy / 2, cx + sx / 2, cy + sy / 2),
                               (cx, cy), c.get("type") == "junction")
    for p in chip.get("ports", []):
        side = (p.get("width_um") / 1000.0) if p.get("width_um") else bore
        x, y = p["position"][0], p["position"][1]
        out[p["id"]] = Feature(p["id"], "port", geo.square_at((x, y), side), (x, y), True, side)
    # A PORT PAD IS A PLACE STREAMS MEET. cad/make_figures.py's `meeting` set is ports plus
    # `junction` chambers, and a pad is neither by type - but it is a port in every way that
    # matters here: it is the cut area under the hole, and three waste lines arriving at one
    # waste port merge in the hole whatever the chip says. Without this, S6's 1.2 mm pads
    # cannot take the two or three lines that share a port: a 1.2 mm wall has 0.2 mm of
    # legal mouth left after the minimum wall at each corner, so the second line is told the
    # pad is fenced in and a perfectly ordinary shared waste reads as unroutable.
    for port, pad in drilled_into(out).items():
        if not out[pad].is_meeting:
            out[pad] = replace(out[pad], is_meeting=True)
    return out


def drilled_into(features: dict[str, Feature]) -> dict[str, str]:
    """{port id: the chamber its hole is cut into}, for every port that sits in one.

    S6 puts a PORT PAD under every port: a small chamber, wider than the bore, so that the
    0.8 mm lid hole opens onto cut area and tools/check_chip_spec.py's port-overlap rule is
    satisfied before a channel is even drawn. A port and its pad are therefore ONE feature,
    and a channel that meets the pad has to be allowed to touch the port - otherwise the
    port's own keep-out fences off every mouth on its pad and the chip looks unroutable,
    which is exactly what the first run on S6's output reported.

    Worked out from the geometry rather than taken from a field, so it is also right for a
    hand-made chip with a port drilled into a chamber and no S6 anywhere.
    """
    out: dict[str, str] = {}
    for f in features.values():
        if f.kind != "port":
            continue
        inside = [c for c in features.values()
                  if c.kind == "chamber" and geo.point_in_rect(f.centre, c.rect)]
        if inside:
            out[f.id] = min(inside, key=lambda c: (c.rect[2] - c.rect[0]) * (c.rect[3] - c.rect[1])).id
    return out


def one_feature_with(ids: Iterable[str], features: dict[str, Feature]) -> set[str]:
    """`ids` plus everything that is physically the same feature: pads and their ports."""
    pads = drilled_into(features)
    out = set(ids)
    out |= {port for port, chamber in pads.items() if chamber in out}
    out |= {pads[port] for port in list(out) if port in pads}
    return out


def static_obstacles(features: dict[str, Feature]) -> list[Obstacle]:
    return [Obstacle(f.id, f.kind, rect=f.rect) for f in features.values()]


def net_obstacle(net_id: str, path: Sequence[Sequence[float]], width_mm: float) -> Obstacle:
    return Obstacle(net_id, "net", path=[(float(p[0]), float(p[1])) for p in path],
                    half_width=width_mm / 2.0)


def blocked_mask(grid: Grid, obstacles: Iterable[Obstacle], width_mm: float,
                 min_wall_mm: float, exempt: Iterable[str] = ()) -> bytearray:
    """1 where a channel of `width_mm` may NOT put its centreline.

    Cost is one pass over each obstacle's grown bounding box, not over the board, so a
    board full of small parts rasterises in milliseconds.
    """
    exempt = set(exempt)
    mask = bytearray(grid.cells)
    margin = width_mm / 2.0 + min_wall_mm

    # The board edge is a wall like any other: the channel body must stay on the board.
    for i in range(grid.nx):
        x = grid.cx(i)
        if x < margin or x > grid.fx - margin:
            base = i * grid.ny
            for j in range(grid.ny):
                mask[base + j] = 1
    for j in range(grid.ny):
        y = grid.cy(j)
        if y < margin or y > grid.fy - margin:
            for i in range(grid.nx):
                mask[i * grid.ny + j] = 1

    for obs in obstacles:
        own = obs.owner in exempt
        # An exempt part still blocks its own interior, so the search cannot cut through a
        # chamber and leave by the far wall - it must enter at the mouth and stop.
        clear = 0.0 if own else margin + obs.half_width
        x0, y0, x1, y1 = obs.bounds()
        i0, j0 = grid.cell_containing(x0 - clear - grid.res, y0 - clear - grid.res)
        i1, j1 = grid.cell_containing(x1 + clear + grid.res, y1 + clear + grid.res)
        for i in range(i0, i1 + 1):
            x = grid.cx(i)
            base = i * grid.ny
            for j in range(j0, j1 + 1):
                if mask[base + j]:
                    continue
                y = grid.cy(j)
                if obs.distance(x, y) < max(clear, 1e-9) and not obs.exempt_at(x, y):
                    mask[base + j] = 1
    return mask


def _wall_anchor(grid: Grid, rect: Rect, side: int, k: int) -> tuple[Pt, tuple[int, int]]:
    """Mouth point on one wall of a rectangle, and the first cell outside it.

    The mouth is pinned to a grid centre LINE (the cell's y for a vertical wall, its x for
    a horizontal one), so the stub from the wall to the first cell centre is exactly
    perpendicular to the wall. That is what gives a clean mouth in a closed-contour DXF.
    """
    x0, y0, x1, y1 = rect
    if side == EAST:
        return (x1, grid.cy(k)), (grid.first_cell_beyond(x1, 0, True), k)
    if side == WEST:
        return (x0, grid.cy(k)), (grid.first_cell_beyond(x0, 0, False), k)
    if side == NORTH:
        return (grid.cx(k), y1), (k, grid.first_cell_beyond(y1, 1, True))
    return (grid.cx(k), y0), (k, grid.first_cell_beyond(y0, 1, False))


def chamber_terminals(grid: Grid, feature: Feature, width_mm: float, min_wall_mm: float,
                      sides: Sequence[str] | None = None) -> list[Terminal]:
    """Every legal mouth on a chamber's outline, for a channel of this width.

    A mouth is kept clear of the wall's own corners by width/2 + min_wall, so the corner of
    the chamber is never thinned below the minimum wall by the opening.

    ONE EXCEPTION, for small parts. A funnel or a pre-filter can be only a little wider
    than the channel feeding it - a 0.8 mm wall taking a 0.44 mm channel has no room for
    that margin on both sides, and refusing it would send a perfectly buildable chip back
    to S6 for no reason. So when the wall is shorter than width + 2 x min_wall, the ONE
    mouth nearest the middle of the wall is offered instead, on the weaker condition that
    the channel does not overhang the wall at all. There is no material in the corner to
    protect in that case: the chamber carries on round it.
    """
    x0, y0, x1, y1 = feature.rect
    keep = width_mm / 2.0 + min_wall_mm
    want = {DIR_OF_SIDE[s] for s in sides} if sides else set(STEP)
    out: list[Terminal] = []
    for side in sorted(want):
        along_y = side in (EAST, WEST)
        lo, hi = (y0 + keep, y1 - keep) if along_y else (x0 + keep, x1 - keep)
        if hi < lo:
            slack = ((y1 - y0) if along_y else (x1 - x0)) - width_mm
            if slack < -1e-9:
                continue   # the channel is wider than the wall: it would overhang the part
            mid = (y0 + y1) / 2 if along_y else (x0 + x1) / 2
            lo, hi = mid - slack / 2, mid + slack / 2
            k = (grid.cell_containing(mid, mid)[1 if along_y else 0])
            for cand in (k, k + 1, k - 1):
                pos = grid.cy(cand) if along_y else grid.cx(cand)
                if lo - 1e-12 <= pos <= hi + 1e-12:
                    anchor, cell = _wall_anchor(grid, feature.rect, side, cand)
                    if grid.inside(*cell):
                        out.append(Terminal(feature.id, SIDE_OF_DIR[side], anchor,
                                            (grid.centre(*cell),), cell, side))
                    break
            continue
        k_lo, _ = grid.cell_containing(lo, lo)
        n = grid.ny if along_y else grid.nx
        for k in range(max(0, k_lo), n):
            pos = grid.cy(k) if along_y else grid.cx(k)
            if pos < lo - 1e-12:
                continue
            if pos > hi + 1e-12:
                break
            anchor, cell = _wall_anchor(grid, feature.rect, side, k)
            if not grid.inside(*cell):
                continue
            out.append(Terminal(feature.id, SIDE_OF_DIR[side], anchor,
                                (grid.centre(*cell),), cell, side))
    return out


def port_terminals(grid: Grid, feature: Feature, width_mm: float,
                   sides: Sequence[str] | None = None) -> list[Terminal]:
    """Mouths that run the channel ACROSS a port hole, not up to its edge.

    check_chip_spec's port-overlap rule wants a 0.8 mm hole to open onto at least a
    quarter of its own area of cut channel. A channel that stops at the hole's edge - which
    is what cad/make_train_cad.py cuts today, and why there are 60 of these errors on main -
    opens onto nothing. So the mouth sits on the FAR side of the hole and the channel
    crosses the whole bore, giving bore x width of cut area under the lid hole.
    """
    side_mm = feature.port_side_mm
    half = side_mm / 2.0
    px, py = feature.centre
    want = {DIR_OF_SIDE[s] for s in sides} if sides else set(STEP)
    out: list[Terminal] = []
    for d in sorted(want):
        dx, dy = STEP[d]
        if dx:
            j = grid.cell_containing(px, py)[1]
            if abs(grid.cy(j) - py) + width_mm / 2.0 > half:
                continue   # the band would hang off the hole: no overlap credit
            anchor = (px - dx * half, grid.cy(j))
            i = grid.first_cell_beyond(px + dx * half, 0, dx > 0)
            cell = (i, j)
        else:
            i = grid.cell_containing(px, py)[0]
            if abs(grid.cx(i) - px) + width_mm / 2.0 > half:
                continue
            anchor = (grid.cx(i), py - dy * half)
            j = grid.first_cell_beyond(py + dy * half, 1, dy > 0)
            cell = (i, j)
        if not grid.inside(*cell):
            continue
        out.append(Terminal(feature.id, SIDE_OF_DIR[d], anchor, (grid.centre(*cell),), cell, d))
    return out


def terminals_for(grid: Grid, feature: Feature, width_mm: float, min_wall_mm: float,
                  sides: Sequence[str] | None = None) -> list[Terminal]:
    if feature.kind == "port":
        return port_terminals(grid, feature, width_mm, sides)
    return chamber_terminals(grid, feature, width_mm, min_wall_mm, sides)


def terminal_is_clear(term: Terminal, obstacles: Iterable[Obstacle], width_mm: float,
                      min_wall_mm: float, exempt: Iterable[str]) -> bool:
    """Is the mouth itself legal? The stub is not on the grid, so it is checked by hand."""
    exempt = set(exempt)
    stub = [term.anchor, *term.lead]
    margin = width_mm / 2.0 + min_wall_mm
    for obs in obstacles:
        if obs.owner in exempt:
            continue
        need = margin + obs.half_width
        for a, b in geo.segments(stub):
            if obs.rect is not None:
                gap = geo.dist_seg_rect(a, b, obs.rect)
            else:
                gap = min(geo.dist_seg_seg(a, b, c, d) for c, d in geo.segments(obs.path or []))
            if gap < need and not obs.exempt_at((a[0] + b[0]) / 2, (a[1] + b[1]) / 2):
                return False
    return True

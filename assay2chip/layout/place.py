"""Where every part sits. An optimisation, not a template.

THE PROBLEM. Parts come in flow order. They have to fit inside one of the three agreed
envelopes with `min_wall_mm` between every pair of features and to every edge, they may be
rotated, the channels S7 will draw must have somewhere to go, and because the chip is ONE
fluidic layer, a channel that crosses another is a short circuit. So the objective is:

    minimise (crossings risked, total channel length, rows used)

in that order. A crossing is fatal and a long channel is only expensive, so the ordering is
not arbitrary.

THE SHAPE OF THE SEARCH. The chain of parts is a PATH, and a path drawn monotonically is
planar no matter how it is routed. So parts are packed into horizontal rows in flow order
and the rows alternate direction (boustrophedon): row 0 runs left to right, row 1 right to
left, and the link between two rows is a short vertical hop at the end they share. Within a
row the parts are spread evenly across the usable width rather than packed to one side.
That costs a little channel length and buys two things: the inter-row hop stays vertical
and short, and the slack turns into ROUTING CORRIDORS between neighbours, which is where
S7 puts the reagent and waste runs.

WHAT IS LEFT TO SEARCH is then small and worth searching exhaustively: the number of rows,
and each part's rotation. For a given row count the row height is fixed, so a part's
rotation is forced when only one orientation fits and otherwise chosen to keep the row
short. Every row count from 1 up is tried and scored, and the best wins.

WHY ROW COUNT DRIVES CROSSING RISK. Reagents come in from one long edge and waste leaves
by the other - inlets on one side, outlets on the other, the planar default. A part in row
0 can reach the bottom edge, and a part in the top row can reach the top edge, without
crossing anything. A part in an INTERIOR row cannot: its stub has to cross a neighbouring
row's chain. So a 1-row or 2-row layout has zero crossing risk and a 3-row layout has one
risk per side port on the middle row, which is exactly what the score ranks.

WHAT THIS NEVER DOES is shrink a part to make it fit. A part below its floor is not a part.
When nothing fits, `place_parts` raises `CannotFit`, and S6 turns that into a
`rerun_from: s4` request with the area arithmetic in the reason.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib.contracts import Envelope  # noqa: E402

from .parts import Opening, SizedPart, route_width_mm  # noqa: E402

MAX_ROWS = 6                # beyond this the crossing risk makes the result useless
SPREAD_MIN_PARTS = 2        # a lone part in a row is centred instead


class CannotFit(Exception):
    """The parts do not fit this envelope. `detail` carries the arithmetic for S4."""

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


@dataclass(frozen=True)
class Placed:
    """One part, placed. `x`/`y` are the lower-left corner of the ROTATED footprint."""

    part: SizedPart
    x: float
    y: float
    w: float
    h: float
    rotation: int           # 0 or 90 degrees, counter-clockwise
    row: int
    flow: int               # +1 this row runs left to right, -1 right to left
    index: int              # position in flow order

    @property
    def centre(self) -> tuple[float, float]:
        return (self.x + self.w / 2.0, self.y + self.h / 2.0)

    @property
    def rect(self) -> tuple[float, float, float, float]:
        return (self.x, self.y, self.x + self.w, self.y + self.h)

    def place_opening(self, opening: Opening) -> tuple[tuple[float, float], str]:
        """An opening's absolute position and outward direction after rotation."""
        return (self.to_chip(opening.position), rotate_direction(opening.direction, self.rotation))

    def to_chip(self, local: Sequence[float]) -> tuple[float, float]:
        """Part-local (x, y) -> absolute chip (x, y), through this part's rotation.

        Counter-clockwise, then translated so the rotated bounding box's lower-left corner
        sits at (self.x, self.y).
        """
        lx, ly = float(local[0]), float(local[1])
        fx, fy = self.part.footprint
        turn = self.rotation % 360
        if turn == 90:
            lx, ly = fy - ly, lx
        elif turn == 180:
            lx, ly = fx - lx, fy - ly
        elif turn == 270:
            lx, ly = ly, fx - lx
        return (round(self.x + lx, 4), round(self.y + ly, 4))

    def size_to_chip(self, size: Sequence[float]) -> list[float]:
        """A part-local [x, y, z] extent, swapped if this part is turned a quarter turn."""
        sx, sy = float(size[0]), float(size[1])
        rest = [float(v) for v in size[2:]]
        swapped = self.rotation % 180 == 90
        return ([sy, sx] if swapped else [sx, sy]) + rest


@dataclass
class Placement:
    """The chosen layout, plus what it cost and what else was tried."""

    placed: list[Placed]
    rows: int
    row_bands: list[tuple[float, float]]            # (y0, y1) per row, bottom row first
    usable: tuple[float, float, float, float]       # x0, y0, x1, y1 the parts may occupy
    envelope: Envelope
    edge_margin_mm: float
    clearance_mm: float
    chain_length_mm: float
    crossing_risk: int
    corridors: list[dict] = field(default_factory=list)
    tried: list[dict] = field(default_factory=list)

    @property
    def score(self) -> tuple[int, float, int]:
        return (self.crossing_risk, round(self.chain_length_mm, 3), self.rows)

    @property
    def first(self) -> Placed:
        return self.placed[0]

    @property
    def last(self) -> Placed:
        return self.placed[-1]

    @property
    def entry_edge(self) -> str:
        """Which edge the chain starts at: always the left, row 0."""
        return "left"

    @property
    def exit_edge(self) -> str:
        """Which edge the chain ends at - the right for an odd row count, the left for even."""
        return "right" if self.last.flow > 0 else "left"

    def by_id(self, part_id: str) -> Placed:
        for p in self.placed:
            if p.part.part_id == part_id:
                return p
        raise KeyError(part_id)

    def area_used_mm2(self) -> float:
        return sum(p.w * p.h for p in self.placed)


QUARTER_TURN = {"+x": "+y", "+y": "-x", "-x": "-y", "-y": "+x"}


def rotate_direction(direction: str, rotation: int) -> str:
    """Outward normal after a counter-clockwise rotation of 0, 90, 180 or 270 degrees."""
    out = direction
    for _ in range((rotation % 360) // 90):
        out = QUARTER_TURN[out]
    return out


def _orientations(part: SizedPart, row_h: float, policy: str) -> list[tuple[int, float, float]]:
    """(rotation, w, h) options that fit a row of height `row_h`, best for `policy` first.

    A part's +x IS its flow direction and its shape encodes that: a trap is wide across
    the flow and short along it so the front arrives evenly. So under the `keep` policy
    rotation 0 comes first and 90 degrees is only a packing tool. Under `flat` the
    shorter-in-y orientation comes first, which is what makes a tall part share a shelf
    with its neighbours instead of claiming a whole band of the chip.
    """
    fx, fy = part.footprint
    options = [(0, fx, fy)] + ([(90, fy, fx)] if abs(fx - fy) > 1e-9 else [])
    fits = [o for o in options if o[2] <= row_h + 1e-9]
    if policy == "flat":
        fits.sort(key=lambda o: (o[2], o[0]))
    return fits


def _shelves(parts: Sequence[SizedPart], usable_w: float, usable_h: float,
             clearance: float, *, policy: str = "keep", width_budget: float = 1.0,
             height_break: float | None = None
             ) -> tuple[list[list[tuple[int, float, float]]], list[float]] | None:
    """Pack parts in flow order into SHELVES of their own heights. None if they do not fit.

    Shelf packing rather than equal-height rows: a row is only as tall as the tallest part
    on it, so one 9 mm mixer does not force every other row to be 9 mm as well. That is
    the difference between a chip that fits and a re-run request - a fixed row height
    wasted 40% of the board and then asked S4 to split an assay that was never too big.

    `width_budget` is the fraction of the usable width a shelf may use. Below 1.0 it packs
    fewer parts per shelf, which is how the caller explores layouts with more rows (and
    therefore different port edges) rather than only the tightest one.

    `height_break` groups parts BY HEIGHT: when the next part's height differs from the
    shelf's by more than this fraction, a new shelf starts even though the width would
    still allow it. Because a shelf costs the height of its TALLEST member, putting one
    9.7 mm chamber on a shelf of 2 mm chambers wastes 7.7 mm of board twice over, and two
    9.7 mm chambers sharing one shelf costs that height once. That is the difference
    between a chip that fits and a re-run request.
    """
    limit = usable_w * width_budget
    shelves: list[list[tuple[int, float, float]]] = [[]]
    heights: list[float] = [0.0]
    width = 0.0
    for part in parts:
        options = _orientations(part, usable_h, policy)
        if not options or min(o[1] for o in options) > usable_w + 1e-9:
            return None
        gap = clearance if shelves[-1] else 0.0
        fits = [o for o in options if width + gap + o[1] <= max(limit, o[1]) + 1e-9]
        if fits and height_break is not None and shelves[-1]:
            tall = max(fits[0][2], heights[-1])
            if tall > 0 and abs(fits[0][2] - heights[-1]) / tall > height_break:
                fits = []
        if fits:
            rotation, w, h = fits[0]
            width += gap + w
            shelves[-1].append((rotation, w, h))
            heights[-1] = max(heights[-1], h)
        else:
            rotation, w, h = options[0]
            shelves.append([(rotation, w, h)])
            heights.append(h)
            width = w
        total = sum(heights) + (len(heights) - 1) * clearance
        if total > usable_h + 1e-9:
            return None
    return shelves, heights


def _bands(heights: Sequence[float], y0: float, clearance: float) -> list[tuple[float, float]]:
    """(y0, y1) of each shelf, bottom shelf first, from the shelves' own heights."""
    out, cursor = [], y0
    for h in heights:
        out.append((round(cursor, 4), round(cursor + h, 4)))
        cursor += h + clearance
    return out


def _lay_out(parts: Sequence[SizedPart], rows: list[list[tuple[int, float, float]]],
             heights: Sequence[float], usable: tuple[float, float, float, float],
             clearance: float) -> list[Placed]:
    x0, y0, x1, _ = usable
    usable_w = x1 - x0
    bands = _bands(heights, y0, clearance)
    out: list[Placed] = []
    index = 0
    for r, row in enumerate(rows):
        flow = 1 if r % 2 == 0 else -1
        band_y0, band_y1 = bands[r]
        row_h = band_y1 - band_y0
        widths = [w for _, w, _ in row]
        slack = usable_w - sum(widths) - (len(row) - 1) * clearance
        if len(row) >= SPREAD_MIN_PARTS:
            # Spread the slack evenly into the gaps: the row spans the full usable width,
            # so the hop to the next row is vertical, and every gap is a routing corridor.
            gap = clearance + slack / (len(row) - 1)
            start = 0.0
        else:
            gap = clearance
            start = slack / 2.0
        cursor = start
        ordered = list(enumerate(row)) if flow > 0 else list(reversed(list(enumerate(row))))
        positions: dict[int, float] = {}
        for i, (_, w, _h) in ordered:
            positions[i] = x0 + cursor
            cursor += w + gap
        for i, (rotation, w, h) in enumerate(row):
            # A row that runs RIGHT TO LEFT gets its parts turned through 180 degrees, so
            # each part's inlet faces the flow arriving at it. Without this, every link in
            # a reversed row has to wrap around the part it is joining, and S7 reports
            # "every mouth on this part is within width/2 + min_wall of another feature" -
            # the incoming channel is sitting on the face the outgoing one needs. A half
            # turn costs nothing: the footprint is unchanged, so the packing above is
            # unaffected, and it is a rotation rather than a mirror, so the part is the
            # same part.
            turn = (rotation + (0 if flow > 0 else 180)) % 360
            out.append(Placed(
                part=parts[index],
                x=round(positions[i], 4),
                y=round(band_y0 + (row_h - h) / 2.0, 4),
                w=round(w, 4),
                h=round(h, 4),
                rotation=turn,
                row=r,
                flow=flow,
                index=index,
            ))
            index += 1
    return out


def _chain_length(placed: Sequence[Placed], usable: tuple[float, float, float, float],
                  side_feeds: Iterable[int], drains: Iterable[int]) -> float:
    """Manhattan centre-to-centre length of everything S7 will have to draw."""
    x0, y0, x1, y1 = usable
    total = 0.0
    for a, b in zip(placed, placed[1:]):
        (ax, ay), (bx, by) = a.centre, b.centre
        total += abs(bx - ax) + abs(by - ay)
    if placed:
        fx, fy = placed[0].centre
        total += abs(fx - x0) + 0.0                     # entry port on the left edge
        lx, ly = placed[-1].centre
        total += (x1 - lx) if placed[-1].flow > 0 else (lx - x0)
    for i in side_feeds:                                # stub down to the bottom edge
        total += abs(placed[i].centre[1] - y0)
    for i in drains:                                    # stub up to the top edge
        total += abs(y1 - placed[i].centre[1])
    return total


def _crossing_risk(placed: Sequence[Placed], side_feeds: Iterable[int],
                   drains: Iterable[int], rows: int) -> int:
    """Side ports that cannot reach their edge without crossing another row's chain."""
    risk = 0
    for i in side_feeds:
        if placed[i].row != 0:
            risk += 1
    for i in drains:
        if placed[i].row != rows - 1:
            risk += 1
    return risk


def _corridors(placed: Sequence[Placed], row_bands: Sequence[tuple[float, float]],
               usable: tuple[float, float, float, float], clearance: float) -> list[dict]:
    """The open rectangles S7 may route through: gaps inside a row, lanes between rows."""
    x0, _, x1, _ = usable
    out: list[dict] = []
    by_row: dict[int, list[Placed]] = {}
    for p in placed:
        by_row.setdefault(p.row, []).append(p)
    for r, row in sorted(by_row.items()):
        band = row_bands[r]
        ordered = sorted(row, key=lambda p: p.x)
        edges = [x0] + [v for p in ordered for v in (p.x, p.x + p.w)] + [x1]
        for a, b in zip(edges[0::2], edges[1::2]):
            if b - a > clearance / 2.0:
                out.append({"rect": [round(a, 3), round(band[0], 3), round(b, 3), round(band[1], 3)],
                            "why": f"routing corridor inside row {r}"})
    for r, (band, nxt) in enumerate(zip(row_bands, row_bands[1:])):
        out.append({"rect": [round(x0, 3), round(band[1], 3), round(x1, 3), round(nxt[0], 3)],
                    "why": f"routing lane between rows {r} and {r + 1}"})
    return out


def place_parts(parts: Sequence[SizedPart], envelope: Envelope, *, floors: dict,
                port_pad_mm: float, side_feeds: Iterable[int] = (),
                drains: Iterable[int] = (), max_rows: int = MAX_ROWS) -> Placement:
    """Place `parts` in flow order inside `envelope`. Raises CannotFit when they do not fit.

    `port_pad_mm` is the side of a port pad: the usable band is inset far enough that a pad
    on the edge still keeps a full wall to the nearest part.
    `side_feeds` / `drains` are indices into `parts` that need a reagent or waste stub, so
    the score can count the crossings they would risk.
    """
    if not parts:
        raise CannotFit("no parts to place", {"reason": "no on-chip ops"})
    min_wall = float(floors["min_wall_mm"])
    # A corridor has to hold the widest channel plus a wall on each side; features need at
    # least min_wall between them whatever else is true.
    route_w = route_width_mm(parts)
    # A corridor wide enough for TWO channels side by side, not one. A part's faces carry
    # more than one net - a trap chamber has a sample inlet, a filtrate drain and a
    # release opening - and with room for only one channel between two parts, the net that
    # arrives first occupies the only mouth and S7 reports that nothing can leave
    # ("every mouth on this part is within width/2 + min_wall of another feature. This is
    # a placement problem, not a routing one"). It is right, and this is the fix.
    clearance = max(min_wall, 2 * route_w + 3 * min_wall)
    # Edge margin: the port centre sits min_wall + pad/2 in, the pad reaches pad/2 further,
    # and then a FULL CORRIDOR before the nearest part - not just a wall. A channel has to
    # LEAVE the pad, and it needs its own width plus a wall on each side to do it; with
    # only a wall there, S7 reports "every mouth on this pad is within width/2 + min_wall
    # of another feature, so a channel cannot leave it at all - this is a placement
    # problem, not a routing one", and it is right.
    edge_margin = min_wall + port_pad_mm + clearance
    x0, y0 = edge_margin, edge_margin
    x1, y1 = envelope.x_mm - edge_margin, envelope.y_mm - edge_margin
    usable = (x0, y0, x1, y1)
    usable_w, usable_h = x1 - x0, y1 - y0
    side_feeds, drains = list(side_feeds), list(drains)

    need_area = sum(p.area_mm2 for p in parts)
    if usable_w <= 0 or usable_h <= 0:
        raise CannotFit(
            f"the {envelope.name} envelope ({envelope.x_mm:g} x {envelope.y_mm:g} mm) has no "
            f"usable area once {edge_margin:g} mm of port margin is taken off each edge",
            {"envelope": envelope.name, "edge_margin_mm": edge_margin})
    biggest = max(parts, key=lambda p: min(p.footprint))
    if min(biggest.footprint) > max(usable_w, usable_h) + 1e-9:
        raise CannotFit(
            f"part '{biggest.part_id}' is {biggest.footprint[0]:g} x {biggest.footprint[1]:g} mm "
            f"and does not fit the {usable_w:g} x {usable_h:g} mm usable area of the "
            f"{envelope.name} envelope in either orientation",
            {"part": biggest.part_id, "footprint_mm": list(biggest.footprint),
             "usable_mm": [round(usable_w, 3), round(usable_h, 3)],
             "envelope": envelope.name, "needs_mm2": round(need_area, 1),
             "usable_mm2": round(usable_w * usable_h, 1)})

    best: Placement | None = None
    tried: list[dict] = []
    # The search: how each part is turned, and how full a shelf may get before the next
    # one starts. Small and worth enumerating exhaustively; the score picks the winner.
    for policy in ("keep", "flat"):
        for budget in (1.0, 0.75, 0.5, 0.34):
          for height_break in (None, 0.5, 0.25):
              packed = _shelves(parts, usable_w, usable_h, clearance, policy=policy,
                                width_budget=budget, height_break=height_break)
              if packed is None:
                  tried.append({"policy": policy, "width_budget": budget,
                                "height_break": height_break, "fits": False,
                                "why": "the parts do not fit the usable area this way"})
                  continue
              rows, heights = packed
              placed = _lay_out(parts, rows, heights, usable, clearance)
              length = _chain_length(placed, usable, side_feeds, drains)
              risk = _crossing_risk(placed, side_feeds, drains, len(rows))
              bands = _bands(heights, y0, clearance)
              candidate = Placement(
                  placed=placed, rows=len(rows), row_bands=bands, usable=usable,
                  envelope=envelope, edge_margin_mm=round(edge_margin, 4),
                  clearance_mm=round(clearance, 4), chain_length_mm=round(length, 3),
                  crossing_risk=risk,
                  corridors=_corridors(placed, bands, usable, clearance))
              tried.append({"policy": policy, "width_budget": budget, "fits": True,
                            "rows": len(rows), "crossing_risk": risk,
                            "chain_length_mm": round(length, 3),
                            "rotated": sum(1 for p in placed if p.rotation),
                            "row_heights_mm": [round(h, 3) for h in heights]})
              if best is None or candidate.score < best.score:
                  best = candidate
    if best is None:
        tallest = max(parts, key=lambda p: min(p.footprint))
        raise CannotFit(
            f"{len(parts)} part(s) needing {need_area:.1f} mm2 do not fit the "
            f"{usable_w:.1f} x {usable_h:.1f} mm usable area of the {envelope.name} envelope "
            f"({usable_w * usable_h:.1f} mm2); the shelves they need are taller than "
            f"{usable_h:.1f} mm (the largest part is '{tallest.part_id}' at "
            f"{tallest.footprint[0]:g} x {tallest.footprint[1]:g} mm)",
            {"envelope": envelope.name, "needs_mm2": round(need_area, 1),
             "usable_mm2": round(usable_w * usable_h, 1),
             "usable_mm": [round(usable_w, 3), round(usable_h, 3)],
             "parts": [{"id": p.part_id, "footprint_mm": list(p.footprint)} for p in parts],
             "tried": tried})
    best.tried = tried
    check_walls(best, min_wall)
    return best


def check_walls(placement: Placement, min_wall: float) -> None:
    """Assert the invariant the whole stage exists to keep. Raises CannotFit if broken."""
    x0, y0, x1, y1 = placement.usable
    env = placement.envelope
    for p in placement.placed:
        ax0, ay0, ax1, ay1 = p.rect
        edge = min(ax0, ay0, env.x_mm - ax1, env.y_mm - ay1)
        if edge < min_wall - 1e-6:
            raise CannotFit(
                f"part '{p.part.part_id}' is {edge:.3f} mm from the chip edge, inside the "
                f"{min_wall:g} mm minimum wall",
                {"part": p.part.part_id, "edge_mm": round(edge, 4)})
    for a, b in ((a, b) for i, a in enumerate(placement.placed) for b in placement.placed[i + 1:]):
        gap = _rect_gap(a.rect, b.rect)
        if gap < min_wall - 1e-6:
            raise CannotFit(
                f"parts '{a.part.part_id}' and '{b.part.part_id}' are {gap:.3f} mm apart, "
                f"inside the {min_wall:g} mm minimum wall",
                {"parts": [a.part.part_id, b.part.part_id], "gap_mm": round(gap, 4)})


def _rect_gap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Shortest distance between two axis-aligned rectangles (0 when they touch or overlap)."""
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    if dx == 0.0 and dy == 0.0:
        return 0.0
    return (dx * dx + dy * dy) ** 0.5

"""Lee / A* maze search on the routing grid: the shortest legal rectilinear path.

WHY A MAZE SEARCH AND NOT A VISIBILITY GRAPH. Both were on the table; the grid wins here
for four reasons, in order of how much they matter:

  1. Planarity is free. An already-routed net is rasterised into the blocked mask, so the
     next net CANNOT cross it - there is no legal cell on the other side of it. A
     visibility graph would have to re-offset and re-merge the growing obstacle set after
     every net and then prove no edge of the new tour crosses an old one, which is the same
     work plus a tangent-line special case for every merged corner.
  2. Clearance is a single rasterisation. Our clearance is "min_wall + half of MY width +
     half of YOURS" against a mixed bag of rectangles and polylines. On a grid that is one
     distance test per cell. On a visibility graph it is a Minkowski sum of two shapes per
     obstacle per net, and the offsets of two nearby chambers merge into a non-convex
     polygon whose tangents are fiddly to get right - precisely the kind of code that
     silently emits a 0.39 mm wall.
  3. Length targets need slack, not shortest. A balanced feed has to be MADE longer. A
     visibility graph only knows how to be short; a grid has free cells beside the route
     that meander.py folds into, and it can tell they are free.
  4. The result is rectilinear by construction, so every vertex is a right angle and maps
     onto exactly one block in contracts/blocks.json: straight_channel, channel_corner or
     serpentine_channel. No block in that library is a free-angle run.

The cost is resolution: a 0.05 mm grid cannot express a 0.03 mm offset. That is the right
trade - 0.05 mm is a quarter of the 0.2 mm minimum feature and well under the +/-20 um
process tolerance quoted in contracts/blocks.json.

Search details. A state is (i, j, direction), so a turn can be PRICED rather than being
free: `bend_mm` is added on every change of direction, which keeps routes with fewer
corners (fewer corner losses, fewer places for a bubble to lodge) without ever preferring
an illegal cell. `history` is a per-cell congestion cost that the rip-up loop raises on
contested cells (PathFinder-style negotiated congestion), which is how the router
eventually finds an ordering that fits instead of failing on the first collision.

Multi-source and multi-goal: a chamber offers a mouth on every grid line of every wall, so
the search starts from all of a part's legal mouths at once and stops at the first legal
mouth of the other part it reaches. The route and the two openings are therefore chosen
together, which is what you want - picking the mouths first is what makes hand layouts
unroutable.

Standard library only.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Iterable, Sequence

from .obstacles import OPPOSITE, STEP, Grid, Terminal


@dataclass
class SearchResult:
    """What the search found, or enough detail to say exactly why it did not."""

    cells: list[tuple[int, int]] | None
    start: Terminal | None
    goal: Terminal | None
    cost: float = 0.0
    expanded: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.cells is not None


def search(grid: Grid, mask: Sequence[int], starts: Iterable[Terminal],
           goals: Iterable[Terminal], *, bend_mm: float = 0.20,
           history: dict[int, float] | None = None,
           max_expand: int = 4_000_000) -> SearchResult:
    """Cheapest legal rectilinear path from any start mouth to any goal mouth.

    A start is entered travelling `terminal.direction`; a goal must be entered travelling
    `terminal.direction` too, because a goal terminal's `direction` points from its anchor
    INTO the board, so arriving along it means the last move runs straight at the wall. A
    corner is never left sitting on a chamber mouth, which is what keeps the mouth clean.
    """
    history = history or {}
    starts = [t for t in starts if grid.inside(*t.cell) and not mask[grid.index(*t.cell)]]
    goals = [t for t in goals if grid.inside(*t.cell) and not mask[grid.index(*t.cell)]]
    if not starts:
        return SearchResult(None, None, None, reason="no legal mouth on the 'from' part")
    if not goals:
        return SearchResult(None, None, None, reason="no legal mouth on the 'to' part")

    # A terminal's `direction` runs from its anchor (on the wall) into the first cell. The
    # path leaves the START that way, and reaches the GOAL the other way round: it travels
    # from the goal cell out to the anchor, so it must ARRIVE at that cell moving along
    # OPPOSITE[direction]. Requiring that is what keeps the last move pointing straight at
    # the wall, with no corner sitting on the mouth.
    goal_states: dict[tuple[int, int, int], Terminal] = {}
    for t in goals:
        goal_states[(t.cell[0], t.cell[1], OPPOSITE[t.direction])] = t
    goal_cells = {t.cell for t in goals}

    res = grid.res

    def heuristic(i: int, j: int) -> float:
        return res * min(abs(i - gi) + abs(j - gj) for gi, gj in goal_cells)

    best: dict[tuple[int, int, int], float] = {}
    came: dict[tuple[int, int, int], tuple[int, int, int] | None] = {}
    heap: list[tuple[float, float, tuple[int, int, int]]] = []
    start_by_state: dict[tuple[int, int, int], Terminal] = {}
    for t in starts:
        state = (t.cell[0], t.cell[1], t.direction)
        g = history.get(grid.index(*t.cell), 0.0)
        if state not in best or g < best[state]:
            best[state] = g
            came[state] = None
            start_by_state[state] = t
            heapq.heappush(heap, (g + heuristic(*t.cell), g, state))

    expanded = 0
    while heap:
        _, g, state = heapq.heappop(heap)
        if g > best.get(state, float("inf")) + 1e-12:
            continue
        expanded += 1
        if expanded > max_expand:
            return SearchResult(None, None, None, reason="search budget exhausted",
                                expanded=expanded)
        if state in goal_states:
            cells = _unwind(state, came)
            start = _start_of(state, came, start_by_state)
            return SearchResult(cells, start, goal_states[state], g, expanded)
        i, j, d = state
        for nd, (di, dj) in STEP.items():
            ni, nj = i + di, j + dj
            if not grid.inside(ni, nj):
                continue
            idx = grid.index(ni, nj)
            if mask[idx]:
                continue
            step = res + (bend_mm if nd != d else 0.0) + history.get(idx, 0.0)
            nstate = (ni, nj, nd)
            ng = g + step
            if ng + 1e-12 < best.get(nstate, float("inf")):
                best[nstate] = ng
                came[nstate] = state
                heapq.heappush(heap, (ng + heuristic(ni, nj), ng, nstate))
    return SearchResult(None, None, None, reason="no legal path: the goal is fenced in",
                        expanded=expanded)


def _unwind(state, came) -> list[tuple[int, int]]:
    cells = []
    cur = state
    while cur is not None:
        cells.append((cur[0], cur[1]))
        cur = came[cur]
    cells.reverse()
    return cells


def _start_of(state, came, start_by_state) -> Terminal | None:
    cur = state
    while came.get(cur) is not None:
        cur = came[cur]
    return start_by_state.get(cur)


def reachable_cells(grid: Grid, mask: Sequence[int], seeds: Iterable[tuple[int, int]]) -> set[int]:
    """Flood fill from `seeds` over free cells. Used only to explain a failure.

    When a net is unroutable, the useful question is "which side is fenced in and by
    what", and the answer is the flood fill from each end plus the obstacles on its border.
    """
    out: set[int] = set()
    stack = []
    for i, j in seeds:
        if grid.inside(i, j) and not mask[grid.index(i, j)]:
            idx = grid.index(i, j)
            if idx not in out:
                out.add(idx)
                stack.append((i, j))
    while stack:
        i, j = stack.pop()
        for di, dj in STEP.values():
            ni, nj = i + di, j + dj
            if not grid.inside(ni, nj):
                continue
            idx = grid.index(ni, nj)
            if idx in out or mask[idx]:
                continue
            out.add(idx)
            stack.append((ni, nj))
    return out

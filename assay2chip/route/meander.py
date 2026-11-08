"""Length targets: fold a routed channel until it is as long as it has to be.

WHY A ROUTE EVER NEEDS TO BE LONGER. Two reasons, and both are design intent rather than
tidiness:

  * RESISTANCE. Feeding N parallel chambers from one header splits the flow in the ratio
    of the branch conductances. Equal flow needs equal resistance, and
    R = 12 mu L / (w h^3 (1 - 0.63 h/w)) (operator/network.py rect_resistance), so with w
    and h fixed by the particle and the etch depth, L is the only lever. The short branch
    has to be made as resistive as the long one or the near chamber steals the flow.
  * RESIDENCE TIME. A line that has to hold a fluid for t seconds at flow Q needs volume
    Q t, so length Q t / (w h). contracts/blocks.json target_envelope lists residence time
    under `not_scale_free` for exactly this reason.

So the target comes from S6 (`target_length_mm`) or from a `balance_group`, where the
target is whatever makes every branch in the group match the MOST RESISTIVE one -
resistance, not raw length, because a group may legitimately mix widths and a longer thin
branch is not the same as a longer fat one. `resistance_targets()` does that arithmetic
with operator/network.py's own formula, so S7 and the hydraulic solver cannot disagree.

HOW. A square-wave serpentine is folded into the longest straight run of the route, on
whichever side has free cells:

      ____        ____              pitch  p = width + min_wall  (centre to centre, so the
     |    |      |    |             rib between two risers is exactly the minimum wall)
  ___|    |______|    |___          amplitude a >= width + min_wall as well, so the top run
                                    clears the baseline by a wall too
      <-p->

One period consumes p of the baseline and adds 2a of length. Only free cells are used, the
amplitude is probed against the same blocked mask the router searched, and the result is
handed back to `planarity.check` like any other path - a fold that would break a wall is
shrunk and retried, never emitted.

What this module will NOT do: it will not meet a target by crossing something, and it will
not lie about having met one. If the free space beside the route cannot hold enough folds,
the achieved length is returned with the shortfall, S7 reports achieved vs target, and the
chip is still planar. A resistance miss is a performance problem; a crossing is a dead
chip.

Standard library only.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import geometry as geo
from .obstacles import Grid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "operator"))


@dataclass
class Meander:
    """The outcome of trying to hit a length target."""

    path: list[tuple[float, float]]
    achieved_mm: float
    target_mm: float | None
    added_mm: float
    folds: int
    amplitude_mm: float
    pitch_mm: float
    met: bool
    why: str = ""

    @property
    def error_mm(self) -> float | None:
        return None if self.target_mm is None else self.achieved_mm - self.target_mm

    def as_dict(self) -> dict:
        return {"achieved_length_mm": round(self.achieved_mm, 4),
                "target_length_mm": None if self.target_mm is None else round(self.target_mm, 4),
                "length_error_mm": None if self.error_mm is None else round(self.error_mm, 4),
                "added_mm": round(self.added_mm, 4), "folds": self.folds,
                "amplitude_mm": round(self.amplitude_mm, 4), "pitch_mm": round(self.pitch_mm, 4),
                "met": self.met, "why": self.why}


def rect_resistance(length_mm: float, width_mm: float, depth_mm: float, mu: float) -> float:
    """operator/network.py's own formula, imported so the two cannot drift apart."""
    import network

    return network.rect_resistance(length_mm, width_mm, depth_mm, mu)


def length_for_resistance(resistance: float, width_mm: float, depth_mm: float,
                          mu: float) -> float:
    """Invert rect_resistance: the length that gives this resistance at this cross-section."""
    unit = rect_resistance(1.0, width_mm, depth_mm, mu)
    return resistance / unit if unit else 0.0


def resistance_targets(group: Sequence[tuple[str, float, float, float]], mu: float,
                       ) -> dict[str, float]:
    """Equal-resistance length targets for one balance group.

    `group` is (net id, achieved length mm, width mm, depth mm). Every branch is brought up
    to the resistance of the most resistive one, because a branch can be made longer and
    cannot be made shorter than the route it needs.
    """
    rs = {nid: rect_resistance(L, w, h, mu) for nid, L, w, h in group}
    if not rs:
        return {}
    worst = max(rs.values())
    return {nid: length_for_resistance(worst, w, h, mu) for nid, _, w, h in group}


def _free(grid: Grid, mask: Sequence[int], x: float, y: float) -> bool:
    i, j = grid.cell_containing(x, y)
    if not grid.inside(i, j):
        return False
    return not mask[grid.index(i, j)]


def _corridor_free(grid: Grid, mask: Sequence[int], x0: float, y0: float,
                   x1: float, y1: float, step: float) -> bool:
    """Is every grid cell under this axis-aligned band free?"""
    xs = _samples(x0, x1, step)
    ys = _samples(y0, y1, step)
    return all(_free(grid, mask, x, y) for x in xs for y in ys)


def _samples(a: float, b: float, step: float) -> list[float]:
    lo, hi = min(a, b), max(a, b)
    n = max(1, int(math.ceil((hi - lo) / step)))
    return [lo + (hi - lo) * k / n for k in range(n + 1)]


def _longest_run(path: Sequence[Sequence[float]]) -> int:
    segs = geo.segments(path)
    return max(range(len(segs)), key=lambda k: math.dist(*segs[k])) if segs else -1


def _fold_span(a, b, pitch: float) -> tuple[tuple[float, float], tuple[float, float]] | None:
    """The part of segment a-b a fold may use: the middle, inset a pitch from each end.

    THE INSET IS NOT TIDINESS. Both ends of a routed segment sit against something - a
    chamber wall, a port hole, or the corner where the route turns - and those cells are
    blocked. Probing the offset corridor over the WHOLE segment therefore always fails,
    which is why the first version of this module folded nothing at all and reported every
    balance group as unmatched. A fold lives in the middle of a run, clear of both ends.
    """
    span = math.dist(a, b)
    inset = pitch
    if span <= 2 * inset + 2 * pitch:
        return None
    ux = (b[0] - a[0]) / span
    uy = (b[1] - a[1]) / span
    return ((a[0] + ux * inset, a[1] + uy * inset),
            (b[0] - ux * inset, b[1] - uy * inset))


def _free_amplitude(grid: Grid, mask: Sequence[int], a, b, sign: int,
                    want: float, width_mm: float, min_wall: float) -> float:
    """How far the fold can reach off this segment on one side before it hits something."""
    horizontal = abs(b[1] - a[1]) < 1e-9
    probe = grid.res
    best = 0.0
    reach = probe
    while reach <= want + 1e-9:
        if horizontal:
            y = a[1] + sign * reach
            ok = _corridor_free(grid, mask, min(a[0], b[0]), y, max(a[0], b[0]), y, grid.res)
        else:
            x = a[0] + sign * reach
            ok = _corridor_free(grid, mask, x, min(a[1], b[1]), x, max(a[1], b[1]), grid.res)
        if not ok:
            break
        best = reach
        reach += probe
    # The outermost run has to clear whatever stopped us by a wall as well.
    return max(0.0, best - (width_mm / 2 + min_wall) if best > 0 else 0.0)


def _square_wave(a, b, folds: int, amplitude: float, pitch: float, sign: int
                 ) -> list[tuple[float, float]]:
    """Vertices of a square wave folded off segment a-b, starting and ending on it.

    ONE FOLD CONSUMES TWO PITCHES of baseline, not one. The up riser and the down riser of
    the same fold are a pitch apart and so are consecutive folds, so every rib in the
    serpentine - riser to riser, and the outward run to the baseline - is exactly
    width + min_wall centre to centre, i.e. a minimum wall of material. Spacing the two
    risers half a pitch apart (the obvious square wave) leaves a rib of
    (width + min_wall)/2 - width, which at our floors is 0.1 mm: unprintable.
    """
    horizontal = abs(b[1] - a[1]) < 1e-9
    span = math.dist(a, b)
    total = 2.0 * pitch * folds
    ux = (b[0] - a[0]) / span if span else 0.0
    uy = (b[1] - a[1]) / span if span else 0.0
    # Centre the wave on the segment so both tails stay straight.
    start = (span - total) / 2.0
    out: list[tuple[float, float]] = [(a[0], a[1])]
    for k in range(folds):
        s0 = start + k * 2.0 * pitch
        for s, off in ((s0, 0.0), (s0, amplitude), (s0 + pitch, amplitude), (s0 + pitch, 0.0)):
            if horizontal:
                out.append((a[0] + ux * s, a[1] + sign * off))
            else:
                out.append((a[0] + sign * off, a[1] + uy * s))
    out.append((b[0], b[1]))
    return out


def fold_to_length(path: Sequence[Sequence[float]], target_mm: float | None, width_mm: float,
                   min_wall_mm: float, grid: Grid, mask: Sequence[int],
                   tolerance_mm: float = 0.05) -> Meander:
    """Lengthen `path` to `target_mm` with a serpentine, without breaking any clearance."""
    base = geo.simplify(path)
    have = geo.length(base)
    pitch = width_mm + min_wall_mm
    if target_mm is None or target_mm <= have + tolerance_mm:
        why = "" if target_mm is None else "the route is already at or over the target"
        return Meander(list(base), have, target_mm, 0.0, 0, 0.0, pitch, True, why)

    want = target_mm - have
    segs = geo.segments(base)
    order = sorted(range(len(segs)), key=lambda k: -math.dist(*segs[k]))
    for k in order:
        inner = _fold_span(*segs[k], pitch)
        if inner is None:
            continue   # too short for a fold plus the inset at each end
        ia, ib = inner
        span = math.dist(ia, ib)
        for sign in (1, -1):
            # An amplitude is only useful at or above one wall plus the width, or the two
            # legs of the fold are themselves too close together.
            room = _free_amplitude(grid, mask, ia, ib, sign,
                                   want=max(want / 2.0, pitch) + 2 * pitch,
                                   width_mm=width_mm, min_wall=min_wall_mm)
            if room < pitch:
                continue
            max_folds = int(math.floor(span / (2.0 * pitch)))
            if max_folds < 1:
                continue
            # Prefer the smallest amplitude that works: a shallow wide fold is easier to
            # fill and easier to print than a deep narrow one.
            for folds in range(1, max_folds + 1):
                amplitude = want / (2.0 * folds)
                if amplitude < pitch:
                    amplitude = pitch
                if amplitude > room:
                    continue
                wave = _square_wave(ia, ib, folds, amplitude, pitch, sign)
                # Only the WAVE is checked against the mask, never the rest of the route.
                # The untouched prefix and suffix end ON their parts' outlines, and a part
                # this net connects to has its interior blocked (that is what stops the
                # search cutting through it), so re-testing those cells would reject every
                # fold ever proposed - which is what the first version of this did.
                if not _all_free(grid, mask, wave):
                    continue
                candidate = geo.simplify(
                    list(base[:k + 1]) + wave + list(base[k + 1:]))
                if geo.self_crossings(candidate):
                    continue
                got = geo.length(candidate)
                if got + tolerance_mm < target_mm and folds < max_folds:
                    continue      # try more folds
                return Meander(candidate, got, target_mm, got - have, folds, amplitude,
                               pitch, abs(got - target_mm) <= tolerance_mm,
                               "" if abs(got - target_mm) <= tolerance_mm
                               else "folded as far as the free space allowed")
    return Meander(list(base), have, target_mm, 0.0, 0, 0.0, pitch, False,
                   "no straight run on this route has free space beside it for a fold of "
                   f"pitch {pitch:g} mm: the target needs {want:.3f} mm more length. S6 can "
                   "give this net more room, or the branch can be made narrower if what it "
                   "carries allows it.")


def _all_free(grid: Grid, mask: Sequence[int], path: Sequence[Sequence[float]]) -> bool:
    """Every sampled point along this polyline lands on a cell the net may use."""
    for a, b in geo.segments(path):
        span = math.dist(a, b)
        if span < 1e-12:
            continue
        for t in _samples(0.0, 1.0, grid.res / span):
            if not _free(grid, mask, a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t):
                return False
    return True

"""The PATH-AWARE planar check: does this routed chip short-circuit itself?

READ THIS BEFORE TRUSTING cad/make_figures.py ON A ROUTED CHIP.
=================================================================================
cad/make_figures.py is this repo's only planar-collision detector, and in this worktree
it does NOT read a channel's `path`. `make_figures.segments_of()` takes the `from` and
`to` anchors, trims them with `clip_to_box`, and checks ONE STRAIGHT SEGMENT between
them. cad/make_train_cad.py cuts the DXF the same way. So on a chip whose channels are
routed polylines, both tools are looking at geometry that is not on the chip:

  * a routed detour is INVISIBLE to fabrication - the DXF cuts straight through whatever
    the router went around;
  * make_figures' verdict is about the straight line, so "0 collisions" from it is
    NECESSARY BUT NOT SUFFICIENT, and a collision it reports may be an artefact of the
    straight line rather than a fault in the route.

operator/network.py DOES read `path` (channel_geometry, "spec path"), so resistance is
computed on the routed length while the cut path is straight: the three geometry
computations disagree by construction. `cad_path_support()` below probes for this at
runtime so the S7 report states it as a fact rather than a rumour, and S7 refuses to
call a chip verified on make_figures alone.

This module is the stopgap: the same rules, measured on the polyline.

THE RULES, and where each comes from
------------------------------------
crossing          two channel centrelines properly intersect. contracts/blocks.json
                  excluded.multilayer: one fluidic layer, so a crossing is one channel.
                  This is the rule that may never be traded away.
self_crossing     a channel crosses itself. A meander folded past its own pitch does it.
clearance         centreline distance minus both half widths < fabrication_floors
                  .min_wall_mm, against every unconnected channel, chamber and port.
                  STRICTER THAN make_figures, which calls anything above 0 a warning;
                  S7 treats a sub-wall rib as an error, because it is a wall we cannot
                  print and the chip is ours to redraw.
port_overlap      a port hole must open onto cut channel: tools/check_chip_spec.py's own
                  port_overlaps() is called, not reimplemented.
mouth             a channel's end sits ON its part's outline, meets it at a right angle,
                  and stays width/2 + min_wall clear of that wall's corners - so a
                  closed-contour DXF has no outline crossing at the mouth.
edge              the channel body stays on the board, min_wall from the rim.
width             >= min_open_channel_mm, and for a particle-carrying line
                  >= capsule_channel.width_factor_low x the SWOLLEN particle diameter.
rectilinear       every segment runs along x or y, so every vertex is a right angle and
                  maps onto a block in the library.

Channels that share a terminal are exempt from `clearance` at that terminal when the
terminal is a place streams are MEANT to converge - a port or a `junction` chamber. That
is exactly make_figures' `meeting` set, so the two tools agree on what a tee is.

Standard library only.
"""

from __future__ import annotations

import math
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

from . import geometry as geo
from .obstacles import Feature, features_of, one_feature_with

ROOT = Path(__file__).resolve().parents[2]

# How exactly a mouth has to sit on its part's outline. 0.15 um, which is ONE AND A HALF
# UNITS IN THE LAST PLACE of the geometry this repo can actually express: cad/make_train_cad
# writes DXF coordinates as "%.4f" mm, so 0.1 um is the finest distinction a cut path can
# carry, and S7 rounds its own `path` to the same 4 places so the spec and the DXF agree.
# A tighter test would reject walls that are only unequal because they were written down.
# S6's parts land on coordinates like 1.99995 mm (a 9.4667 mm part centred on 6.7333), so
# this is not hypothetical - at 1e-6 every S6 mouth failed.
MOUTH_TOL_MM = 1.5e-4
PATH_DECIMALS = 4


@dataclass
class Conflict:
    """One broken rule, with enough detail to fix it without opening the file."""

    rule: str
    severity: str               # "error" | "warning"
    what: str
    detail: str
    at: tuple[float, float] | None = None
    gap_mm: float | None = None
    parts: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["at"] = list(self.at) if self.at else None
        return d

    def __str__(self) -> str:
        where = f" at ({self.at[0]:.3f}, {self.at[1]:.3f})" if self.at else ""
        gap = f" [gap {self.gap_mm:.3f} mm]" if self.gap_mm is not None else ""
        return f"{self.severity.upper():7} {self.rule}: {self.what}{where}{gap} - {self.detail}"


def _path_of(chip: dict, ch: dict, feats: dict[str, Feature]) -> list[tuple[float, float]] | None:
    """A channel's centreline. `path` when it has one, else the straight trimmed line.

    The fallback is only so an UNROUTED chip can be checked at all; S7's own output always
    carries `path`, and `routed_paths_only` reports any channel that does not.
    """
    if ch.get("path"):
        return [(float(p[0]), float(p[1])) for p in ch["path"]]
    a, b = feats.get(ch.get("from")), feats.get(ch.get("to"))
    if a is None or b is None:
        return None
    return [a.centre, b.centre]


def check(chip: dict, *, connections: Sequence[dict] = (), strict_walls: bool = True) -> list[Conflict]:
    """Every rule above, on the chip as written. Empty list means the layer is planar."""
    from ..lib import contracts

    min_wall = contracts.floor("min_wall_mm")
    min_open = contracts.floor("min_open_channel_mm")
    width_factor = float(contracts.param("capsule_channel.width_factor_low"))
    swell = contracts.capsule_swell_factor()
    feats = features_of(chip)
    fx, fy = chip["footprint"]["x"], chip["footprint"]["y"]
    carries_by_id = {c.get("channel") or c.get("id"): c for c in connections}

    chans = []
    for ch in chip.get("channels", []):
        path = _path_of(chip, ch, feats)
        if path is None or len(path) < 2:
            continue
        chans.append((ch, path, float(ch["width_mm"])))

    out: list[Conflict] = []

    # --- the rule that may never be traded away -----------------------------------------
    for i, (a, pa, wa) in enumerate(chans):
        for b, pb, wb in chans[i + 1:]:
            for hit in geo.path_crossings(pa, pb):
                out.append(Conflict(
                    "crossing", "error", f"channels '{a['id']}' and '{b['id']}'",
                    "two centrelines cross on one fluidic layer: that is ONE channel, a "
                    "short circuit between what each of them serves. Never traded away - "
                    "reroute, re-place (S6) or re-partition (S4).",
                    at=hit, parts=[a["id"], b["id"]]))
        for hit in geo.self_crossings(pa):
            out.append(Conflict("self_crossing", "error", f"channel '{a['id']}'",
                                "the channel crosses itself (a meander folded past its own "
                                "pitch): it short-circuits its own length.",
                                at=hit, parts=[a["id"]]))

    # --- clearance: channel to channel ---------------------------------------------------
    meeting = {f.id for f in feats.values() if f.is_meeting}
    for i, (a, pa, wa) in enumerate(chans):
        for b, pb, wb in chans[i + 1:]:
            shared = {a["from"], a["to"]} & {b["from"], b["to"]}
            if shared & meeting:
                continue        # a port or a junction: streams are meant to converge there
            gap = geo.dist_path_path(pa, pb) - (wa + wb) / 2
            if gap < min_wall - 1e-9:
                sev = "error" if (gap < 0 or strict_walls) else "warning"
                out.append(Conflict(
                    "clearance", sev, f"channels '{a['id']}' and '{b['id']}'",
                    ("they overlap in plan view: one channel, not two"
                     if gap < 0 else
                     f"the rib between them is {gap:.3f} mm, under the {min_wall:g} mm "
                     f"minimum wall") +
                    (f" (they share '{sorted(shared)[0]}', which is not a port or a "
                     f"junction, so they must still clear each other)" if shared else ""),
                    gap_mm=gap, parts=[a["id"], b["id"]]))

    # --- clearance: channel to chamber, and the mouth ------------------------------------
    for ch, path, w in chans:
        # A port drilled into a pad chamber IS that chamber, as far as clearance goes: the
        # channel that feeds the pad has to cross the hole. obstacles.one_feature_with works
        # that out from the geometry, so it holds for a hand-made chip too.
        ends = one_feature_with({ch.get("from"), ch.get("to")}, feats)
        for f in feats.values():
            if f.id in ends:
                continue
            gap = (geo.dist_path_rect(path, f.rect) if f.kind == "chamber"
                   else geo.dist_point_path(f.centre, path) - f.port_side_mm / 2) - w / 2
            if gap >= min_wall - 1e-9:
                continue
            if f.kind == "chamber":
                detail = ("the channel runs through a chamber it is not connected to: a "
                          "short circuit" if gap < 0 else
                          f"the wall between them is {gap:.3f} mm, under {min_wall:g} mm")
            else:
                detail = ("the port hole is drilled into a channel it is not connected "
                          "to: a short circuit between that channel's two ends"
                          if gap < 0 else
                          f"the channel runs {gap:.3f} mm beside an unconnected port, "
                          f"under the {min_wall:g} mm wall: the hole breaks into it")
            out.append(Conflict("clearance", "error" if (gap < 0 or strict_walls) else "warning",
                                f"channel '{ch['id']}' and {f.kind} '{f.id}'", detail,
                                gap_mm=gap, parts=[ch["id"], f.id]))
        out += _mouth_conflicts(ch, path, w, feats, min_wall)
        out += _hug_conflicts(ch, path, w, feats, min_wall)

        # --- board edge -----------------------------------------------------------------
        margin = w / 2 + min_wall
        for x, y in path:
            if x < margin - 1e-9 or y < margin - 1e-9 or x > fx - margin + 1e-9 or y > fy - margin + 1e-9:
                out.append(Conflict("edge", "error", f"channel '{ch['id']}'",
                                    f"a vertex is closer than width/2 + {min_wall:g} mm to the "
                                    f"rim of the {fx:g} x {fy:g} mm board",
                                    at=(x, y), parts=[ch["id"]]))
                break

        # --- width ----------------------------------------------------------------------
        if w < min_open - 1e-9:
            out.append(Conflict("width", "error", f"channel '{ch['id']}'",
                                f"{w * 1000:g} um wide, below the {min_open * 1000:g} um "
                                f"minimum open channel", parts=[ch["id"]]))
        need = _particle_width_mm(ch, carries_by_id.get(ch["id"]), width_factor, swell, contracts)
        if need is not None and w < need - 1e-9:
            out.append(Conflict("width", "error", f"channel '{ch['id']}'",
                                f"carries '{ch.get('carries', 'capsules')}' but is {w * 1000:g} um "
                                f"wide, under {need * 1000:.0f} um "
                                f"({width_factor:g} x the swollen particle diameter)",
                                parts=[ch["id"]]))
        if not geo.is_rectilinear(path):
            out.append(Conflict("rectilinear", "warning", f"channel '{ch['id']}'",
                                "a segment is neither along x nor along y, so it is not a "
                                "straight_channel / channel_corner / serpentine_channel run",
                                parts=[ch["id"]]))

    out += port_overlap_conflicts(chip)
    return out


def _particle_width_mm(ch: dict, conn: dict | None, factor: float, swell: float,
                       contracts) -> float | None:
    """Minimum width for what this channel carries, or None when no particle rule applies.

    capsules / droplets (or unset): factor x the SWOLLEN diameter - swollen, because a
    capsule that has been through culture is what has to get out again, and
    contracts/parameters.json capsule.swell_factor is the multiplier. Stricter than
    check_chip_spec.py, which uses the unswollen diameter, so a channel that passes here
    passes there too.
    cells: dissociation.outlet_min_width_um, as check_chip_spec does.
    liquid / oil / signals, or capsule_passable false: no particle rule.
    """
    carries = ch.get("carries", "capsules")
    if ch.get("capsule_passable") is False or carries in ("liquid", "oil", "signals"):
        return None
    if carries == "cells":
        return float(contracts.param("dissociation.outlet_min_width_um")) / 1000.0
    d_um = (conn or {}).get("particle_um")
    if d_um:
        d_um = float(d_um)
        # A connection states the diameter it carries. Treat it as unswollen unless S6
        # says otherwise, and swell it, because the swollen capsule is the one to pass.
        if not (conn or {}).get("particle_swollen"):
            d_um *= swell
    else:
        d_um = contracts.swollen_capsule_diameter_um()
    return factor * d_um / 1000.0


def _mouth_conflicts(ch: dict, path: Sequence[Sequence[float]], w: float,
                     feats: dict[str, Feature], min_wall: float) -> list[Conflict]:
    """Does each end of the channel meet its part squarely, on the outline?"""
    out: list[Conflict] = []
    for end, point, neighbour in (("from", path[0], path[1]), ("to", path[-1], path[-2])):
        f = feats.get(ch.get(end))
        if f is None:
            out.append(Conflict("mouth", "error", f"channel '{ch['id']}'",
                                f"'{end}' names '{ch.get(end)}', which is not a chamber or a "
                                f"port on this chip", parts=[ch["id"]]))
            continue
        if f.kind == "port":
            # A port mouth is inside the hole (the channel crosses the bore), not on a wall.
            if geo.dist_point_rect(point, f.rect) > MOUTH_TOL_MM:
                out.append(Conflict("mouth", "error", f"channel '{ch['id']}' at port '{f.id}'",
                                    "the path does not reach the port hole, so the hole opens "
                                    "onto nothing", at=tuple(point), parts=[ch["id"], f.id]))
            continue
        on_wall = (geo.dist_point_rect(point, f.rect) <= MOUTH_TOL_MM
                   and _on_outline(point, f.rect))
        if not on_wall:
            out.append(Conflict("mouth", "error", f"channel '{ch['id']}' at chamber '{f.id}'",
                                "the path's end is not ON the chamber outline: in a closed "
                                "contour that is either a gap or a line running inside the "
                                "chamber", at=tuple(point), parts=[ch["id"], f.id]))
            continue
        vertical = (abs(point[0] - f.rect[0]) <= MOUTH_TOL_MM
                    or abs(point[0] - f.rect[2]) <= MOUTH_TOL_MM)
        square = (abs(neighbour[1] - point[1]) if vertical
                  else abs(neighbour[0] - point[0])) <= MOUTH_TOL_MM
        if not square:
            out.append(Conflict("mouth", "error", f"channel '{ch['id']}' at chamber '{f.id}'",
                                "the last segment is not perpendicular to the wall it meets, so "
                                "the channel outline crosses the chamber outline at the mouth",
                                at=tuple(point), parts=[ch["id"], f.id]))
        along = point[1] if vertical else point[0]
        lo, hi = (f.rect[1], f.rect[3]) if vertical else (f.rect[0], f.rect[2])
        wall = hi - lo
        # The same small-part exception obstacles.chamber_terminals makes: a wall with no
        # room for width/2 + min_wall on both sides only has to not be overhung.
        keep = (w / 2 + min_wall) if wall >= w + 2 * min_wall - 1e-9 else w / 2
        if along < lo + keep - 1e-9 or along > hi - keep + 1e-9:
            out.append(Conflict("mouth", "error", f"channel '{ch['id']}' at chamber '{f.id}'",
                                f"the mouth is {min(along - lo, hi - along):.3f} mm from the corner "
                                f"of a {wall:g} mm wall, under the {keep:.3f} mm it needs"
                                + (f" (width/2 + the {min_wall:g} mm wall)"
                                   if keep > w / 2 + 1e-9 else
                                   " (width/2: the channel would overhang the part)"),
                                at=tuple(point), parts=[ch["id"], f.id]))
    return out


def _hug_conflicts(ch: dict, path: Sequence[Sequence[float]], w: float,
                   feats: dict[str, Feature], min_wall: float) -> list[Conflict]:
    """A channel may touch its OWN part at the mouth, and must clear it everywhere else.

    cad/make_figures.py and tools/check_chip_spec.py both skip a channel against its own
    `from`/`to` part entirely, which is right at the mouth and wrong twenty millimetres
    later: a route that doubles back and runs along its own chamber's wall leaves a rib
    nobody checks. Here the mouth is excused - the first and last two vertices - and the
    rest of the route is measured.

    WARNING, NOT ERROR, and the distinction is the physics. The channel and the chamber it
    feeds are already ONE fluid path, so a rib between them that is too thin to print does
    not short two circuits together: it merges and the mouth comes out wider than drawn.
    That is a tolerance defect for S6 to fix by moving the part, not a dead chip, and
    S7 reports it rather than refusing to route. The router prices these cells
    (router.HUG_COST) so a route takes the clear way round whenever there is one.
    """
    out: list[Conflict] = []
    middle = path[2:-2]
    if len(middle) < 2:
        return out
    for end in ("from", "to"):
        f = feats.get(ch.get(end))
        if f is None or f.kind != "chamber":
            continue
        gap = geo.dist_path_rect(middle, f.rect) - w / 2
        if gap < min_wall - 1e-9:
            out.append(Conflict(
                "own_part_rib", "warning", f"channel '{ch['id']}' and its own chamber '{f.id}'",
                f"away from the mouth the route runs {gap:.3f} mm from the chamber it "
                f"belongs to, under the {min_wall:g} mm wall. They are one fluid path, so "
                f"this is not a short circuit - the rib merges and the mouth comes out wider "
                f"than drawn. S6 can clear it by giving this net more room",
                gap_mm=gap, parts=[ch["id"], f.id]))
    return out


def _on_outline(p: Sequence[float], r) -> bool:
    tol = MOUTH_TOL_MM
    on_x = abs(p[0] - r[0]) <= tol or abs(p[0] - r[2]) <= tol
    on_y = abs(p[1] - r[1]) <= tol or abs(p[1] - r[3]) <= tol
    return ((on_x and r[1] - tol <= p[1] <= r[3] + tol)
            or (on_y and r[0] - tol <= p[0] <= r[2] + tol))


# --- the repo's own port-overlap rule, called not copied --------------------------------

def port_overlap_conflicts(chip: dict) -> list[Conflict]:
    """tools/check_chip_spec.py's port-overlap rule, run on this chip.

    Called rather than reimplemented so S7 cannot pass a rule S9 will fail. Note the tool
    prefers an exported DXF over the spec when one exists - and the DXF is cut without
    `path`, so for a routed chip the DXF answer is about the straight line. S7 measures
    the SPEC geometry here and says so.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import check_chip_spec as ccs
    except Exception as exc:                      # pragma: no cover - tools must be there
        return [Conflict("port_overlap", "warning", "tools/check_chip_spec.py",
                         f"could not be imported ({exc}), so the port-overlap rule was not run")]
    polys = ccs._spec_outlines(chip)
    out = []
    for r in ccs.port_overlaps(chip, polys=polys, source="the spec's routed paths"):
        if r["overlap_mm2"] + r["tolerance_mm2"] < r["need_mm2"]:
            out.append(Conflict(
                "port_overlap", "error", f"port '{r['port']}'",
                f"its {r['side_mm']:g} mm hole opens onto {r['overlap_mm2']:.3f} mm2 of cut "
                f"channel, under the {r['need_mm2']:.3f} mm2 the rule needs: run the channel "
                f"across the hole or ask S6 for a port pad",
                gap_mm=r["overlap_mm2"] - r["need_mm2"], parts=[r["port"]]))
    return out


# --- is cad/ path-aware in THIS worktree? ----------------------------------------------

def cad_path_support() -> dict:
    """Probe the three geometry computations for whether they read a channel's `path`.

    Not a guess from a changelog: make_figures is actually CALLED on a chip with a
    dog-legged path and asked what segments it sees, and the generator and the hydraulic
    solver are read for the access. The S7 report carries the answer.
    """
    out = {
        "make_figures": {"honours_path": None, "how": "", "is_collision_detector": True},
        "make_train_cad": {"honours_path": None, "how": "", "is_cut_path": True},
        "network": {"honours_path": None, "how": "", "is_resistance": True},
    }
    dogleg = {
        "id": "path_probe", "placeholder": True, "units": "mm",
        "footprint": {"x": 30, "y": 20},
        "layers": [{"id": "fluidic", "material": "acrylic", "thickness_mm": 0.3}],
        "chambers": [
            {"id": "a", "type": "inlet", "center": [4.0, 10.0, 0.15], "size": [2.0, 2.0, 0.3],
             "volume_ul": 1.2},
            {"id": "b", "type": "outlet", "center": [20.0, 10.0, 0.15], "size": [2.0, 2.0, 0.3],
             "volume_ul": 1.2}],
        "channels": [{"id": "ch", "from": "a", "to": "b", "width_mm": 0.3, "depth_mm": 0.3,
                      "path": [[5.0, 10.0], [12.0, 10.0], [12.0, 3.0], [19.0, 3.0], [19.0, 10.0]]}],
        "ports": [],
    }
    sys.path.insert(0, str(ROOT / "cad"))
    try:
        import make_figures
        segs = make_figures.segments_of(dogleg)
        seen = [(round(p0[0], 3), round(p0[1], 3), round(p1[0], 3), round(p1[1], 3))
                for _, p0, p1, _ in segs]
        straight = [(5.0, 10.0, 19.0, 10.0)]
        out["make_figures"]["honours_path"] = not (len(seen) == 1 and seen == straight)
        out["make_figures"]["how"] = (
            "segments_of() returned the straight anchor-to-anchor line and ignored a "
            "5-vertex path" if seen == straight else
            f"segments_of() returned {len(seen)} segment(s) for a 5-vertex path")
    except Exception as exc:                      # pragma: no cover
        out["make_figures"]["how"] = f"probe failed: {exc}"
    for name, rel in (("make_train_cad", "cad/make_train_cad.py"), ("network", "operator/network.py")):
        try:
            src = (ROOT / rel).read_text(errors="replace")
            out[name]["honours_path"] = ('ch.get("path")' in src or "ch['path']" in src
                                         or 'ch["path"]' in src)
            out[name]["how"] = f"{rel} {'reads' if out[name]['honours_path'] else 'never reads'} a channel's `path`"
        except OSError as exc:                    # pragma: no cover
            out[name]["how"] = f"could not read {rel}: {exc}"
    blind = [k for k, v in out.items() if v["honours_path"] is False]
    out["path_blind"] = blind
    out["verdict"] = (
        "cad/ is PATH-BLIND: " + ", ".join(blind) + ". A routed polyline is invisible to "
        "them, so the DXF cuts a straight line through whatever the router went around and "
        "the repo's only planar-collision detector checks geometry that is not on the chip. "
        "make_figures reporting 0 collisions on a routed chip is NECESSARY BUT NOT "
        "SUFFICIENT; assay2chip/route/planarity.py is the authority until cad/ is fixed."
        if blind else
        "cad/ reads `path` everywhere: make_figures' verdict and the DXF cut path are both "
        "about the routed geometry.")
    return out


def straight_line_proxy(chip: dict) -> dict:
    """What cad/make_figures.py SEES: its verdict on the straight-line proxy, labelled.

    Kept separate from `check` on purpose. Two numbers that mean different things must not
    be added together, and a reader of the S7 report has to be able to tell which geometry
    each verdict is about.
    """
    sys.path.insert(0, str(ROOT / "cad"))
    try:
        import make_figures
    except Exception as exc:                      # pragma: no cover
        return {"available": False, "why": str(exc), "errors": [], "warnings": []}
    msgs, tightest = make_figures.geometry_warnings(chip)
    return {
        "available": True,
        "geometry": "straight anchor-to-anchor lines (make_figures ignores `path` here)",
        "errors": [m for m in msgs if m.startswith("ERROR")],
        "warnings": [m for m in msgs if m.startswith("WARN")],
        "tightest_clearance_mm": None if math.isinf(tightest[0]) else round(tightest[0], 3),
        "tightest_between": tightest[1],
    }


def errors(conflicts: Sequence[Conflict]) -> list[Conflict]:
    return [c for c in conflicts if c.severity == "error"]


def routed_paths_only(chip: dict) -> list[str]:
    """Channels with no explicit `path`. S7's own output must never have any."""
    return [ch["id"] for ch in chip.get("channels", []) if not ch.get("path")]

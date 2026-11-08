"""S7 route: a placed chip plus S6's connections -> planar channels on ONE fluidic layer.

    python assay2chip/stages/s7_route.py --id my_assay --chip my_assay_c1
    python assay2chip/stages/s7_route.py --chip-file chip/module3_v1_translated.json --dry-run

In:   chip/<chip_id>.json                              S6, placed, no channels
      assay2chip/work/<id>/connections_<chip_id>.json  S6, what must be joined
Out:  chip/<chip_id>.json                              the same chip, with `channels`
      assay2chip/work/<id>/route_<chip_id>.json        the routing report

The one rule this stage exists to keep: contracts/blocks.json says one fluidic layer and
excludes every block that needs a via, so NO CHANNEL MAY CROSS ANOTHER. A crossing is not
a cosmetic defect, it is a short circuit between whatever the two lines serve, and this
stage will return `rerun_from` rather than emit one. See assay2chip/route/router.py for how
planarity is guaranteed (blocked mask, rip-up, independent re-check) and
assay2chip/route/maze.py for why it is a grid maze search and not a visibility graph.

WHAT THIS STAGE WILL NOT DO. It does not move anything. Chamber and port positions are
S6's, and a stage that quietly nudged a port by 0.3 mm to make its own job easier would
leave the protocol, the STL names and the operator's port map describing a different chip.
When the board cannot be wired as placed, S7 returns

    {"rerun_from": "s6", "reason": "..."}   re-place: the parts are in each other's way
    {"rerun_from": "s4", "reason": "..."}   re-partition: no placement can work

and the reason names the net, the parts in the way and the clearance that failed.

READ THIS ABOUT cad/ - IT IS NOT A FOOTNOTE
-------------------------------------------
There are THREE independent geometry computations for a channel in this repo, and in this
worktree they do not agree:

    cad/make_train_cad.py   the DXF cut path      IGNORES `path` (straight, clipped)
    cad/make_figures.py     the ONLY collision detector   IGNORES `path` (straight, clipped)
    operator/network.py     resistance              READS `path`

So a routed channel is invisible to fabrication, and a 0-collision verdict from
make_figures on a routed chip is about a straight line that is not on the chip. S7 does not
paper over this: `assay2chip/route/planarity.py` re-checks every clearance on the emitted
polylines and is the authority, the report records both verdicts separately and labelled,
and `--loud` prints the warning on every run. S7 does not edit cad/ - that fix belongs to
whoever owns those files. `check_path_support()` probes for it at runtime, so when the fix
lands this stage stops shouting by itself.

Standard library only (plus assay2chip.lib).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts                      # noqa: E402
from assay2chip.lib.io import load_json, write_json       # noqa: E402
from assay2chip.route import connections as conn          # noqa: E402
from assay2chip.route import planarity, router            # noqa: E402

WRITES = ["chip/{chip_id}.json"]
STAGE = "s7_route"
CHIP_DIR = ROOT / "chip"
LENGTH_TOLERANCE_MM = 0.05


class RouteError(ValueError):
    """The stage cannot run at all: a missing file, or a chip that is not a chip."""


def rel(path: Path) -> str:
    """A path for a log line, repo-relative when it is in the repo, absolute otherwise.

    `Path.relative_to` raises on anything outside the repo, which is every temp file a test
    routes, so the stage used to die in its own logging.
    """
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


# --- the cad/ cross-check --------------------------------------------------------------

def check_path_support() -> dict:
    """Do the repo's three channel-geometry computations read `path`? Probed, not assumed."""
    return planarity.cad_path_support()


def path_support_warning(support: dict) -> list[str]:
    """The loud lines. Empty when cad/ honours `path` everywhere."""
    blind = support.get("path_blind") or []
    if not blind:
        return []
    lines = [
        "!" * 78,
        "cad/ IS PATH-BLIND IN THIS WORKTREE. A ROUTED CHANNEL IS NOT WHAT GETS CUT.",
        "!" * 78,
    ]
    for name in ("make_train_cad", "make_figures", "network"):
        info = support.get(name) or {}
        if info.get("honours_path") is None:
            continue
        mark = "reads `path`" if info["honours_path"] else "IGNORES `path`"
        lines.append(f"  {name:16} {mark:14} {info.get('how', '')}")
    lines += [
        "  Consequences, both real:",
        "   1. cad/make_train_cad.py cuts a STRAIGHT line between the two parts, straight",
        "      through whatever the router routed around. The DXF is not this chip.",
        "   2. cad/make_figures.py is the repo's ONLY planar-collision detector and it",
        "      measures that same straight line, so its verdict on a routed chip is about",
        "      geometry that does not exist. 0 ERRORs from it is necessary, NOT sufficient.",
        "  Meanwhile operator/network.py DOES use `path`, so resistance is computed on the",
        "  routed length while the cut path is straight: the two disagree by construction.",
        "  S7 does not touch cad/. assay2chip/route/planarity.py re-checks every clearance",
        "  on the emitted polylines and is the authority until that fix lands.",
        "!" * 78,
    ]
    return lines


# --- the stage -------------------------------------------------------------------------

def route_chip(chip: dict, nets: Sequence[conn.Net], *, grid_mm: float = router.DEFAULT_GRID_MM,
               max_passes: int = router.DEFAULT_MAX_PASSES,
               connections_source: str = "") -> dict:
    """Route one chip. Returns a result dict; never mutates `chip`.

    Keys: ok, chip (the routed spec, when ok), report, and on failure rerun_from + reason.
    """
    import copy

    out_chip = copy.deepcopy(chip)
    scale = out_chip.get("scale", "demo")
    env = contracts.envelope(scale)
    report: dict[str, Any] = {
        "stage": STAGE,
        "chip_id": out_chip.get("id"),
        "scale": scale,
        "envelope": {"name": env.name, "x_mm": env.x_mm, "y_mm": env.y_mm,
                     "depth_mm": env.depth_mm, "layers": env.layers, "source": env.source},
        "algorithm": {
            "name": "Lee / A* maze routing on a uniform grid, with rip-up and reroute",
            "grid_mm": grid_mm,
            "connectivity": "4-connected, so every route is rectilinear and every vertex is "
                            "a right angle that maps onto one block in contracts/blocks.json",
            "why_not_visibility_graph":
                "planarity is free on a grid: a routed net is rasterised into the blocked "
                "mask, so the next net has no legal cell on the far side of it. A visibility "
                "graph would have to re-offset and re-merge the growing obstacle set after "
                "every net, get the tangents of merged non-convex offsets right, and it "
                "could not meander, because it only knows how to be short. See "
                "assay2chip/route/maze.py.",
            "net_order": "shortest Manhattan span first, nets that share a part kept "
                         "adjacent; a net that fails is promoted to the front",
            "rip_up": "PathFinder-style negotiated congestion: the cells two nets fought "
                      "over cost more on the next pass",
        },
        "contracts": {
            "min_wall_mm": contracts.floor("min_wall_mm"),
            "min_open_channel_mm": contracts.floor("min_open_channel_mm"),
            "min_port_mm": contracts.floor("min_port_mm"),
            "capsule_channel.width_factor_low": contracts.param("capsule_channel.width_factor_low"),
            "capsule.swell_factor": contracts.capsule_swell_factor(),
            "swollen_capsule_diameter_um": contracts.swollen_capsule_diameter_um(),
            "fingerprint": contracts.fingerprint(),
        },
        "connections": {"source": connections_source, "count": len(nets),
                        "nets": [n.as_dict() for n in nets]},
        "cad_path_support": check_path_support(),
        "unresolved": [],
    }

    if not nets:
        report["nets"] = []
        report["checks"] = {"path_aware_planarity": {"errors": [], "warnings": [], "ok": True}}
        out_chip["channels"] = []
        report["result"] = "nothing to route: the chip has no connections"
        return {"ok": True, "chip": out_chip, "report": report}

    overfull, why = router.connection_graph_is_overfull(nets)
    if overfull:
        report["result"] = "unroutable: the connection graph is not planar"
        report["conflict"] = {"reason": why}
        return {"ok": False, "chip": None, "report": report, "rerun_from": "s4", "reason": why}

    r = router.Router(out_chip, nets, grid_mm=grid_mm, max_passes=max_passes)
    outcome = r.run()
    report["passes"] = outcome.passes
    report["ripups"] = outcome.ripups
    report["grid"] = {"res_mm": r.grid.res, "nx": r.grid.nx, "ny": r.grid.ny,
                      "cells": r.grid.cells}
    report["notes"] = outcome.notes
    report["nets"] = [rn.as_dict() for rn in outcome.routed]

    if not outcome.ok:
        failures = [f.as_dict() for f in outcome.failures]
        report["result"] = "unroutable"
        report["failures"] = failures
        first = outcome.failures[0]
        if first.reason == "part over-subscribed":
            reason = (f"{first.detail} (first affected connection: '{first.net.id}'; "
                      f"{len(outcome.failures)} part(s) over-subscribed in all)")
        else:
            reason = (
                f"net '{first.net.id}' ({first.net.a} -> {first.net.b}) cannot be routed: "
                f"{first.reason}. {first.detail}"
                + (f" In the way: {', '.join(first.blockers)}." if first.blockers else "")
                + f" {len(outcome.ripups)} rip-up(s) over {outcome.passes} pass(es) did not "
                  f"free a corridor. S7 will not emit a crossing to get a result, and it will "
                  f"not move a part S6 placed."
            )
        report["conflict"] = first.as_dict()
        return {"ok": False, "chip": None, "report": report, "rerun_from": "s6",
                "reason": reason}

    out_chip["channels"] = [rn.as_channel() for rn in outcome.routed]
    if out_chip.get("placeholder") and all(not rn.net.note for rn in outcome.routed):
        pass   # leave `placeholder` exactly as S6 set it: S7 does not judge the parts

    # --- verification, in the order that matters ----------------------------------------
    conflicts = planarity.check(out_chip, connections=[n.as_dict() for n in nets])
    errs = planarity.errors(conflicts)
    sys.path.insert(0, str(ROOT / "tools"))
    import check_chip_spec as ccs                            # noqa: E402

    crossings = ccs.crossings(out_chip)
    report["checks"] = {
        "path_aware_planarity": {
            "geometry": "the emitted `path` polylines and widths - the chip as routed",
            "errors": [c.as_dict() for c in errs],
            "warnings": [c.as_dict() for c in conflicts if c.severity == "warning"],
            "ok": not errs and not crossings,
        },
        "check_chip_spec_crossings": [
            {"a": a, "b": b, "at": list(at)} for a, b, at in crossings],
        "straight_line_proxy": planarity.straight_line_proxy(out_chip),
        "channels_without_path": planarity.routed_paths_only(out_chip),
    }
    report["length_targets"] = _length_report(outcome, r)

    if errs or crossings:
        report["result"] = "routed, but the result does not pass its own planar check"
        reason = ("S7 produced a layout that fails the path-aware planar check, so it is "
                  "being thrown away rather than written: "
                  + "; ".join(str(c) for c in errs[:5])
                  + (f" plus {len(errs) - 5} more" if len(errs) > 5 else "")
                  + "".join(f"; centrelines of '{a}' and '{b}' cross at {at}"
                            for a, b, at in crossings[:3]))
        return {"ok": False, "chip": None, "report": report, "rerun_from": "s6",
                "reason": reason}

    report["unresolved"] += _proxy_unresolved(report["checks"]["straight_line_proxy"])
    report["unresolved"] += _moved_mouths(outcome, nets)
    report["result"] = "routed"
    return {"ok": True, "chip": out_chip, "report": report}


def _proxy_unresolved(proxy: dict) -> list[dict]:
    """cad/make_figures.py's complaints, sorted into the two different things they are.

    Both are real and NEITHER is a fault in the route, so they are reported apart rather
    than as one number: they go to different owners.
    """
    lines = proxy.get("errors") or []
    if not lines:
        return []
    pad = [m for m in lines if "is drilled into a chamber" in m]
    rest = [m for m in lines if m not in pad]
    out = []
    if rest:
        out.append({
            "what": f"cad/make_figures.py reports {len(rest)} collision(s) on this chip",
            "owner": "cad/make_figures.py and cad/make_train_cad.py",
            "why": "it measures the straight anchor-to-anchor line, not the routed `path`, "
                   "so every one of these is about geometry that is not on the chip. The "
                   "path-aware check on the real geometry is clean. This cannot be fixed "
                   "from assay2chip/: those two scripts have to read `path`.",
            "count": len(rest), "lines": rest,
        })
    if pad:
        out.append({
            "what": f"cad/make_figures.py calls {len(pad)} port pad(s) a short circuit",
            "owner": "cad/make_figures.py vs tools/check_chip_spec.py - the two rules "
                     "contradict each other",
            "why": "make_figures errors on ANY port whose hole touches a chamber ('the port "
                   "is drilled into a chamber'), while check_chip_spec's port-overlap rule "
                   "REQUIRES the hole to open onto at least a quarter of its own area of cut "
                   "feature - which a port pad is the intended way to provide. A chip cannot "
                   "satisfy both. S6 cuts the pads, S7 routes to them, and neither can settle "
                   "which rule is right: make_figures needs to exempt a port from the chamber "
                   "it is deliberately drilled into, exactly as it already exempts channels "
                   "that share a port.",
            "count": len(pad), "lines": pad,
        })
    return out


def _moved_mouths(outcome: router.RouteOutcome, nets: Sequence[conn.Net]) -> list[dict]:
    """Openings S7 had to take off the wall S6 chose. Never silent: a block's openings
    are not interchangeable (a trap_chamber's weir is on its outlet side), so an inlet
    moved to another wall can arrive behind the retention feature."""
    moved = []
    for rn in outcome.routed:
        for which, fidelity, end in (("from", rn.openings_as_asked[0], rn.net.a_end),
                                     ("to", rn.openings_as_asked[1], rn.net.b_end)):
            if fidelity == "any":
                moved.append({
                    "net": rn.net.id, "end": which, "feature": end.feature if end else None,
                    "opening": (end.opening if end else "") or None,
                    "s6_wall": list(end.sides) if end and end.sides else None,
                    "s7_wall": rn.from_side if which == "from" else rn.to_side,
                })
    if not moved:
        return []
    return [{
        "what": f"{len(moved)} opening(s) could not stay on the wall S6 chose",
        "owner": "S6 placement",
        "why": "the wall S6 named was fenced in by another part or an earlier net, so the "
               "mouth moved to a different wall of the same part. That is legal geometry - "
               "the planar check passed - but a block's openings are NOT interchangeable, so "
               "S6 should confirm the part still works fed from this side, or re-place it.",
        "openings": moved,
    }]


def _length_report(outcome: router.RouteOutcome, r: router.Router) -> list[dict]:
    """Achieved vs target for every net that had one, plus the resistance it lands on."""
    from assay2chip.route import meander as mnd

    mu = contracts.viscosity_for(37.0).pa_s
    out = []
    for rn in outcome.routed:
        m = rn.meander
        if m is None or m.target_mm is None:
            continue
        row = {
            "net": rn.net.id, "channel": rn.net.emit_id,
            "balance_group": rn.net.balance_group,
            "target_length_mm": round(m.target_mm, 4),
            "achieved_length_mm": round(m.achieved_mm, 4),
            "error_mm": round(m.achieved_mm - m.target_mm, 4),
            "error_percent": round(100 * (m.achieved_mm - m.target_mm) / m.target_mm, 2),
            "folds": m.folds, "amplitude_mm": round(m.amplitude_mm, 4),
            "pitch_mm": round(m.pitch_mm, 4),
            "within_tolerance": abs(m.achieved_mm - m.target_mm) <= LENGTH_TOLERANCE_MM,
            "resistance_pa_s_per_m3": mnd.rect_resistance(m.achieved_mm, rn.width_mm,
                                                          rn.depth_mm, mu),
            "viscosity_pa_s": mu,
            "why": m.why,
        }
        out.append(row)
    return out


# --- stage entry point -----------------------------------------------------------------

def run(assay_id: str | None = None, ctx: Any = None, force: bool = False,
        verbose: bool = False, **kwargs) -> dict | None:
    """Route every chip of this assay. Returns None, or {"rerun_from", "reason"}.

    The first four parameters are NAMED rather than swept into **kwargs because that is how
    a stage declares what it wants: assay2chip/run.py and tests/test_assay2chip_e2e.py both
    build the call with `inspect.signature(run).parameters`, and a bare `**kwargs` matches
    none of them, so the stage would be handed nothing and route nothing.

    Also accepted, for the CLI and for S10's fix-up loop: chip_id / chip_ids, chip_file,
    grid_mm, max_passes, dry_run, loud, log.
    """
    assay_id = assay_id or kwargs.get("id")
    chip_file = kwargs.get("chip_file")
    chip_ids = kwargs.get("chip_ids") or ([kwargs["chip_id"]] if kwargs.get("chip_id") else None)
    grid_mm = float(kwargs.get("grid_mm") or router.DEFAULT_GRID_MM)
    max_passes = int(kwargs.get("max_passes") or router.DEFAULT_MAX_PASSES)
    dry_run = bool(kwargs.get("dry_run"))
    loud = kwargs.get("loud", True)
    log = kwargs.get("log") or (lambda line: print(line))
    del ctx, force, verbose      # accepted so run.py can offer them; not used here

    support = check_path_support()
    if loud:
        for line in path_support_warning(support):
            log(line)

    targets = _chip_paths(assay_id, chip_ids, chip_file)
    if not targets:
        raise RouteError(
            "nothing to route: give --chip <chip_id> (with --id for the connections file) "
            "or --chip-file <path>, or let S4's partition name the chips")

    rerun: dict | None = None
    for path in targets:
        chip = load_json(path)
        nets, source = _nets_for(assay_id, chip)
        result = route_chip(chip, nets, grid_mm=grid_mm, max_passes=max_passes,
                            connections_source=source)
        report = result["report"]
        if assay_id:
            report_path = (ROOT / "assay2chip" / "work" / assay_id
                           / f"route_{chip['id']}.json")
            write_json(report_path, report)
            report["_written_to"] = report_path.relative_to(ROOT).as_posix()
        _log_result(log, path, result)
        if not result["ok"]:
            rerun = rerun or {"rerun_from": result["rerun_from"], "reason": result["reason"]}
            continue
        if not dry_run:
            write_json(path, result["chip"])
            log(f"  wrote {rel(path)}  "
                f"({len(result['chip']['channels'])} channel(s))")
        else:
            log("  --dry-run: nothing written")
    return rerun


def _chip_paths(assay_id: str | None, chip_ids: Sequence[str] | None,
                chip_file: str | None) -> list[Path]:
    if chip_file:
        return [Path(chip_file) if Path(chip_file).is_absolute() else ROOT / chip_file]
    if chip_ids:
        return [CHIP_DIR / f"{cid}.json" for cid in chip_ids]
    if assay_id:
        part = ROOT / "assay2chip" / "work" / assay_id / "partition.json"
        if part.is_file():
            data = json.loads(part.read_text())
            return [CHIP_DIR / f"{c['chip_id']}.json" for c in data.get("chips", [])]
    return []


def _nets_for(assay_id: str | None, chip: dict) -> tuple[list[conn.Net], str]:
    if assay_id:
        return conn.load(assay_id, chip["id"], chip)
    return conn.from_chip(chip), "the chip spec's existing channels (no --id, no connections file)"


def _log_result(log, path: Path, result: dict) -> None:
    rep = result["report"]
    log(f"\n{STAGE}: {rel(path)}  (chip {rep['chip_id']}, "
        f"{rep['scale']} envelope)")
    log(f"  connections: {rep['connections']['count']} from {rep['connections']['source']}")
    if "grid" in rep:
        g = rep["grid"]
        log(f"  grid {g['res_mm']:g} mm, {g['nx']} x {g['ny']} = {g['cells']:,} cells; "
            f"{rep.get('passes', 0)} pass(es), {len(rep.get('ripups', []))} rip-up(s)")
    for rip in rep.get("ripups", []):
        log(f"    RIP-UP pass {rip['pass']}: '{rip['net']}' {rip['reason']}, "
            f"ripped up {', '.join(rip['ripped_up']) or 'nothing'}")
    for net in rep.get("nets", []):
        extra = ""
        if net.get("length"):
            L = net["length"]
            extra = (f"  length {L['achieved_length_mm']:.2f} mm vs target "
                     f"{L['target_length_mm']:.2f} mm "
                     f"({L['length_error_mm']:+.3f} mm, {L['folds']} fold(s))")
        log(f"    {net['channel']:<22} {net['from']} -> {net['to']:<20} "
            f"{net['width_mm'] * 1000:>4.0f} um  {net['length_mm']:>6.2f} mm  "
            f"{net['corners']} corner(s)  {net['block']}{extra}")
    for row in rep.get("length_targets", []):
        verdict = "OK" if row["within_tolerance"] else "MISSED"
        log(f"    LENGTH {verdict:<6} {row['net']}: {row['achieved_length_mm']:.3f} mm of "
            f"{row['target_length_mm']:.3f} mm ({row['error_percent']:+.2f}%)"
            + (f" - {row['why']}" if row["why"] else ""))
    checks = rep.get("checks") or {}
    pa = checks.get("path_aware_planarity") or {}
    log(f"  path-aware planar check (THE AUTHORITY): {len(pa.get('errors', []))} error(s), "
        f"{len(pa.get('warnings', []))} warning(s)")
    for c in pa.get("errors", []):
        log(f"    ERROR  {c['rule']}: {c['what']} - {c['detail']}")
    for c in pa.get("warnings", []):
        log(f"    WARN   {c['rule']}: {c['what']} - {c['detail']}")
    proxy = checks.get("straight_line_proxy") or {}
    if proxy.get("available"):
        log(f"  cad/make_figures.py sees {len(proxy['errors'])} collision(s) on the "
            f"{proxy['geometry']}")
        for line in proxy["errors"]:
            log(f"    [straight-line proxy, NOT this chip] {line}")
    for note in rep.get("notes", []):
        log(f"  note: {note}")
    for u in rep.get("unresolved", []):
        log(f"  UNRESOLVED: {u['what']} - {u['why']}")
    if not result["ok"]:
        log(f"  UNROUTABLE -> rerun_from {result['rerun_from']}: {result['reason']}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S7 route: planar channels on one fluidic layer")
    ap.add_argument("--id", dest="assay_id", help="work id: reads connections_<chip>.json, "
                                                 "writes route_<chip>.json")
    ap.add_argument("--chip", dest="chip_id", action="append",
                    help="chip id in chip/ (repeatable); default: every chip in partition.json")
    ap.add_argument("--chip-file", help="path to a chip spec, instead of --chip")
    ap.add_argument("--grid-mm", type=float, default=router.DEFAULT_GRID_MM,
                    help=f"routing grid, at most {router.DEFAULT_GRID_MM:g} mm")
    ap.add_argument("--max-passes", type=int, default=router.DEFAULT_MAX_PASSES,
                    help="rip-up and reroute passes before giving up")
    ap.add_argument("--dry-run", action="store_true", help="route and check, write nothing")
    ap.add_argument("--quiet", action="store_true", help="do not print the cad/ path warning")
    args = ap.parse_args(argv)

    try:
        rerun = run(assay_id=args.assay_id, chip_ids=args.chip_id, chip_file=args.chip_file,
                    grid_mm=args.grid_mm, max_passes=args.max_passes, dry_run=args.dry_run,
                    loud=not args.quiet)
    except (RouteError, conn.ConnectionError_, contracts.ContractError) as exc:
        print(f"{STAGE}: {exc}", file=sys.stderr)
        return 2
    if rerun:
        print(f"\n{STAGE}: rerun_from {rerun['rerun_from']}: {rerun['reason']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

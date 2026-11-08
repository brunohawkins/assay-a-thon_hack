"""S6 build: partition.json -> chip/<chip_id>.json, parts SIZED FROM PHYSICS and PLACED.

    python assay2chip/stages/s6_build.py --id my_assay
    python assay2chip/stages/s6_build.py --id my_assay --out-dir /tmp/try   # no repo writes

For each chip in `partition.json`, in order:

  1. SIZE   every op's part through `assay2chip/parts/registry.py` (one `Need` each, built
            from the op's volume, particles, temperature and shear class).
  2. SCALE  the envelope the partition chose - demo 30 x 20 mm, target 127.76 x 85.48 mm -
            read through `contracts.envelope()`. 290 x 190 mm is the ceiling, not a design
            scale, so a chip is never built to it here.
  3. PLACE  in flow order inside the envelope: walls kept to every feature and every edge,
            rotations allowed, routing corridors left open, total channel length and
            crossing risk minimised (`assay2chip/layout/place.py`).
  4. PORT   on the boundary, at a legal bore, pitch and count, each one over a PORT PAD so
            a 0.8 mm hole opens onto real cut area (`assay2chip/layout/ports.py`).
  5. EMIT   the chip spec: ONE fluidic layer at ONE etch depth with a base and a lid, the
            material the ops actually need, chambers with computed volumes, thermal zones
            for every temperature-holding op, weir crests derived per chip.
  6. HAND   the connections S7 must route to
            `assay2chip/work/<id>/connections_<chip_id>.json`. S6 draws no channels.
  7. OR     ask for a re-run: `{"rerun_from": "s4", "reason": "..."}` when a chip cannot
            fit. A part is NEVER shrunk below its fabrication floor to make it fit; too
            big means the assay has to be cut differently, and that is S4's decision.

WHAT CANNOT PASS YET, and why that is correct. The chip this stage writes has NO channels,
because routing is S7's. So before S7:

    tools/validate.py                 passes (it checks that channels connect real things,
                                      and an empty channel list trivially does)
    tools/check_chip_spec.py          passes, INCLUDING the port-overlap rule - that is the
                                      whole point of the pads
    tests/test_envelope.py            passes at demo scale

and these can only pass after S7, because each needs a channel to exist:

    channel width vs capsule diameter       (no channels yet)
    channel crossings in one layer          (no paths yet)
    every protocol target reachable         (S8 writes the protocol)
    a chamber being connected to anything   (not a rule in either tool today, but it is
                                            what a reader will notice: say so, do not hide it)

`verification()` returns that list so the S12 report can print it instead of someone
having to remember it.
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

from assay2chip.layout import connections as conn  # noqa: E402
from assay2chip.layout import materials as mats  # noqa: E402
from assay2chip.layout import needs as needs_mod  # noqa: E402
from assay2chip.layout import parts as parts_mod  # noqa: E402
from assay2chip.layout import place as place_mod  # noqa: E402
from assay2chip.layout import ports as ports_mod  # noqa: E402
from assay2chip.layout import spec as spec_mod  # noqa: E402
from assay2chip.lib import contracts, io as stage_io  # noqa: E402

STAGE = "assay2chip/stages/s6_build.py"
# Declared for assay2chip/run.py: it records these and REFUSES to overwrite a file this
# pipeline did not produce, which is what stops a generated chip replacing a hand-designed
# one. chip/module1_v1.json is somebody's real design and has a real id.
WRITES = ["chip/{chip_id}.json"]

# Rules that cannot pass until S7 has routed, with the reason. Printed, not remembered.
AFTER_ROUTING = [
    ("tools/check_chip_spec.py: channel carrying capsules >= 1.5 x capsule diameter",
     "there are no channels yet, so the rule has nothing to measure"),
    ("tools/check_chip_spec.py: channels with explicit paths that cross",
     "no channel has a path until S7 draws one; in ONE fluidic layer a crossing is a short "
     "circuit, so this is the rule S7 exists to satisfy"),
    ("tools/check_chip_spec.py: outlet without a width_um / channel shallower than a capsule",
     "both are per-channel warnings and there are no channels"),
    ("tools/validate.py: every protocol target exists on the chip",
     "S8 writes the protocol from this geometry, so there is no protocol to check yet"),
    ("a reader's own check: every chamber is connected to something",
     "neither tool enforces it today, but an unrouted chip really does have unconnected "
     "chambers, and that is fixed by S7, not by S6"),
]


class BuildError(Exception):
    """S6 cannot build this chip and it is not a fit problem (so not S4's to solve)."""


def verification() -> dict:
    """Which checks pass now and which only after S7. For the S12 report."""
    return {
        "passes_now": [
            "tools/validate.py",
            "tools/check_chip_spec.py (including the port-overlap rule: every port has a pad)",
            "tests/test_envelope.py at demo scale",
        ],
        "after_s7_only": [{"rule": r, "why": w} for r, w in AFTER_ROUTING],
    }


# --- one chip ------------------------------------------------------------------

def build_chip(chip_entry: dict, ops_by_number: dict[int, dict], *, assay_id: str,
               handoffs: Sequence[dict] = ()) -> dict:
    """Build one chip from one `partition.json` chips[] entry.

    Returns {"chip": spec, "connections": {...}, "unresolved": [...], "summary": {...}} on
    success, or {"rerun_from": "s4", "reason": ..., "detail": {...}} when it cannot fit.
    """
    chip_id = chip_entry["chip_id"]
    scale = chip_entry.get("scale", "demo")
    envelope = contracts.envelope(scale)
    floors = contracts.fabrication_floors()

    missing = [n for n in chip_entry["ops"] if n not in ops_by_number]
    if missing:
        raise BuildError(
            f"{chip_id}: partition.json puts op(s) {missing} on this chip, but unitops.json "
            f"has no such op. Fix the partition, do not guess what the op was.")

    chosen: list[dict] = []
    skipped: list[dict] = []
    for number in chip_entry["ops"]:
        op = ops_by_number[number]
        where = (op.get("placement") or {}).get("where")
        if where in ("off_chip", "missing_part"):
            skipped.append({"op": number, "where": where,
                            "why": (op.get("placement") or {}).get("why", ""),
                            "need": (op.get("placement") or {}).get("need")})
            continue
        chosen.append(op)
    if not chosen:
        return _rerun(chip_id, scale,
                      "no on-chip ops: every op S4 put on this chip is off_chip or "
                      "missing_part, so there is nothing to build",
                      {"skipped": skipped})

    # One Brief per PART, not per op: ops that act on the same population in the same
    # place share a chamber (chip/designs/README.md rule 1), and the part is sized from
    # each op's `load` - never from the paper's tube volume.
    briefs = needs_mod.briefs_for(chosen, scale=scale)
    depth = contracts.max_depth_mm()

    try:
        material = mats.choose_material(briefs)
    except mats.MaterialError as e:
        return _rerun(chip_id, scale, f"no material can do these ops together: {e}", e.detail)

    side_feeds = [i for i, b in enumerate(briefs) if b.needs_reagent]
    drains = [i for i, b in enumerate(briefs) if b.drains]
    pad = ports_mod.pad_budget(briefs, scale=scale)

    # SIZE AND PLACE, SLOWING DOWN RATHER THAN GIVING UP. Every flow on this chip is one
    # S6 derived (unitops.json has no flow field) and the contracts bound flow only from
    # above, so running slower is always legal - and flow is what makes a mixer long and a
    # shear-bounded chamber wide. So when the parts will not fit, try the same chip at a
    # lower design flow before asking S4 to re-cut the assay. Bounded by the ladder in
    # parts.FLOW_RELIEF, and the factor used is recorded on the chip.
    parts: list = []
    inline: list = []
    placement = None
    failure: Exception | None = None
    relief = 1.0
    for relief in parts_mod.FLOW_RELIEF:
        try:
            parts, inline = size_and_split(briefs, flow_scale=relief)
            if not parts:
                return _rerun(chip_id, scale,
                              "nothing on this chip is a part: every op on it carries a "
                              "stream and is realised as a channel, so there is no "
                              "geometry to place",
                              {"inline": [b.part_id for b in inline]})
            wrong_depth = [p.part_id for p in parts if abs(p.depth_mm - depth) > 1e-9]
            if wrong_depth:
                raise BuildError(
                    f"{chip_id}: part(s) {', '.join(wrong_depth)} came back at a depth other "
                    f"than the chip's {depth:g} mm etch depth. The chip is ONE layer at ONE "
                    f"depth (contracts/blocks.json envelope), so this is a registry bug, not "
                    f"something to paper over here.")
            placement = place_mod.place_parts(
                parts, envelope, floors=floors, port_pad_mm=pad,
                side_feeds=side_feeds, drains=drains)
        except (parts_mod.PartError, place_mod.CannotFit) as e:
            failure = e
            continue
        break
    if placement is None:
        detail = dict(getattr(failure, "detail", {}) or {})
        detail["flow_relief_tried"] = list(parts_mod.FLOW_RELIEF)
        too_big = detail.get("too_big") or isinstance(failure, place_mod.CannotFit)
        head = "too big: split" if too_big else "no part can do an op here"
        return _rerun(chip_id, scale,
                      f"{head} ({failure}); the design flow was reduced to "
                      f"{min(parts_mod.FLOW_RELIEF):g} x and it still does not fit, so this "
                      f"is a partition problem and not a flow one", detail)

    flow_gaps: list[dict] = []
    if relief != 1.0:
        flow_gaps.append({
            "chip_id": chip_id, "field": "flow_ul_min",
            "question": f"at the design flow S6 first derived, this chip's parts did not fit "
                        f"the {scale} envelope, so every derived flow on it was reduced to "
                        f"{relief:g} x. Running slower is always legal (every contract bound "
                        f"on flow is an upper bound) and it is what makes a mixer shorter and "
                        f"a shear-bounded chamber narrower; no part was shrunk below a "
                        f"fabrication floor. S8 may raise it again within the ceilings.",
            "assumed": relief,
            "assumed_from": "assay2chip/layout/parts.py FLOW_RELIEF",
        })

    try:
        port_plan = ports_mod.plan_ports(briefs, placement, floors=floors, scale=scale,
                                         handoffs=handoffs, chip_id=chip_id)
    except ports_mod.PortBudgetExceeded as e:
        return _rerun(chip_id, scale, f"too many ports: split ({e})", e.detail)

    unresolved = flow_gaps + [
        {"chip_id": chip_id, "op": s["op"],
         "question": f"op {s['op']} is {s['where']} and is NOT on this chip: {s['why']}",
         "assumed": None, "assumed_from": "unitops.json placement"}
        for s in skipped]
    try:
        chip, gaps = spec_mod.build_chip_spec(
            chip_id=chip_id, parts=parts, placement=placement, port_plan=port_plan,
            material=material, scale=scale, reason=chip_entry.get("reason", ""),
            unresolved=unresolved, registry=parts_mod.registry_source())
        connection = conn.connection_file(
            assay_id=assay_id, chip_id=chip_id, scale=scale, parts=parts,
            placement=placement, port_plan=port_plan, unresolved=gaps)
    except parts_mod.PartError as e:
        # A part that cannot be joined up is a part that cannot be used, and that is a
        # partition decision, not a crash halfway through writing a chip.
        return _rerun(chip_id, scale, f"a part on this chip cannot be connected: {e}",
                      e.detail)

    return {
        "chip_id": chip_id,
        "chip": chip,
        "connections": connection,
        "unresolved": gaps,
        "summary": {
            "chip_id": chip_id,
            "scale": scale,
            "footprint_mm": [envelope.x_mm, envelope.y_mm],
            "ops": [n for b in briefs for n in b.ops],
            "parts": len(parts),
            "merged_parts": {p.part_id: list(p.brief.ops) for p in parts if p.brief.merged},
            "blocks": {p.part_id: p.block for p in parts},
            "chambers": len(chip["chambers"]),
            "ports": len(chip["ports"]),
            "rows": placement.rows,
            "rotated": sum(1 for p in placement.placed if p.rotation),
            "area_used_mm2": round(placement.area_used_mm2(), 1),
            "area_mm2": round(envelope.area_mm2, 1),
            "fill": round(placement.area_used_mm2() / envelope.area_mm2, 3),
            "chain_length_mm": placement.chain_length_mm,
            "crossing_risk": placement.crossing_risk,
            "connections": len(connection["connections"]),
            "material": material.id,
            "flow_relief": relief,
            "thermal_zones": len(chip.get("thermal_zones", [])),
            "part_registry": parts_mod.registry_source(),
            "skipped_ops": skipped,
        },
    }


def _rerun(chip_id: str, scale: str, reason: str, detail: dict | None = None) -> dict:
    return {"chip_id": chip_id, "rerun_from": "s4", "scale": scale,
            "reason": f"{chip_id}: {reason}", "detail": detail or {}}


# --- the stage -----------------------------------------------------------------

def build(partition: dict, unitops: dict, *, assay_id: str | None = None) -> dict:
    """Build every chip in a partition. Pure: writes nothing."""
    assay_id = assay_id or partition["id"]
    ops_by_number = {op["op"]: op for op in unitops.get("ops", [])}
    handoffs = partition.get("handoffs", [])
    built, reruns = [], []
    for entry in partition["chips"]:
        result = build_chip(entry, ops_by_number, assay_id=assay_id, handoffs=handoffs)
        (reruns if result.get("rerun_from") else built).append(result)
    return {
        "id": assay_id,
        "stage": "s6_build",
        "built": built,
        "reruns": reruns,
        "unresolved": [u for b in built for u in b["unresolved"]],
        "verification": verification(),
        "contracts": contracts.fingerprint(),
    }


def ours(path: Path, chip_id: str) -> bool:
    """True when an existing chip/<id>.json is one this pipeline wrote.

    `assay2chip/run.py` already guards this with its produced-file ledger. This is the
    second lock, for the case where S6 is run on its own: a chip whose `generator.script`
    is not this stage belongs to whoever hand-designed it, and the repo's chips have real
    ids.
    """
    try:
        existing = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    return (existing.get("generator") or {}).get("script") == STAGE and existing.get("id") == chip_id


def write(result: dict, *, assay_id: str, out_dir: Path | None = None,
          force: bool = False) -> list[Path]:
    """Write every built chip and its connections file. Returns the paths written."""
    chip_dir = Path(out_dir) if out_dir else ROOT / "chip"
    written: list[Path] = []
    for item in result["built"]:
        chip_id = item["chip_id"]
        target = chip_dir / f"{chip_id}.json"
        if target.exists() and not ours(target, chip_id) and not force:
            raise BuildError(
                f"{target.relative_to(ROOT) if target.is_relative_to(ROOT) else target} already "
                f"exists and this pipeline did not write it. It may be a hand-designed chip "
                f"with a real id. Pick a different chip_id in partition.json, or pass --force.")
        written.append(stage_io.write_json(target, item["chip"]))
        written.append(stage_io.write_json(
            stage_io.work_dir(assay_id, create=True) / f"connections_{chip_id}.json",
            item["connections"]))
    return written


def run(assay_id: str | None = None, ctx: Any = None, force: bool = False,
        **_: Any) -> dict | None:
    """Stage entry point for assay2chip/run.py."""
    assay_id = assay_id or getattr(ctx, "id", None)
    if not assay_id:
        raise BuildError("S6 needs an assay id")
    partition = stage_io.read_stage(assay_id, "partition")
    unitops = stage_io.read_stage(assay_id, "unitops")
    result = build(partition, unitops, assay_id=assay_id)
    write(result, assay_id=assay_id, force=force or bool(getattr(ctx, "force", False)))
    log = getattr(ctx, "log", None) or (lambda msg: print(f"  {msg}"))
    for item in result["built"]:
        s = item["summary"]
        log(f"{s['chip_id']}: {s['parts']} part(s) in {s['rows']} row(s) on "
            f"{s['footprint_mm'][0]:g} x {s['footprint_mm'][1]:g} mm {s['scale']} "
            f"({s['fill']:.0%} filled), {s['ports']} port(s) + pads, "
            f"{s['connections']} connection(s) for S7, {s['material']}")
    if result["reruns"]:
        reasons = "; ".join(r["reason"] for r in result["reruns"])
        return {"rerun_from": "s4", "reason": reasons, "detail": result["reruns"]}
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="S6 build: partition.json -> placed chip specs")
    ap.add_argument("--id", required=True, help="work id: assay2chip/work/<id>/")
    ap.add_argument("--out-dir", help="write the chips here instead of chip/ (for trying a build)")
    ap.add_argument("--force", action="store_true",
                    help="overwrite a chip/<id>.json this pipeline did not produce")
    ap.add_argument("--dry-run", action="store_true", help="build and report, write nothing")
    ap.add_argument("--json", action="store_true", help="print the whole result as JSON")
    args = ap.parse_args(argv)

    partition = stage_io.read_stage(args.id, "partition")
    unitops = stage_io.read_stage(args.id, "unitops")
    result = build(partition, unitops, assay_id=args.id)

    if args.json:
        print(json.dumps({k: v for k, v in result.items() if k != "built"}
                         | {"built": [b["summary"] for b in result["built"]]}, indent=2))
    else:
        print(f"s6_build: {len(result['built'])} chip(s) built, "
              f"{len(result['reruns'])} needing a different partition "
              f"(parts sized by {parts_mod.registry_source()})")
        for item in result["built"]:
            s = item["summary"]
            print(f"  {s['chip_id']:<24} {s['footprint_mm'][0]:g} x {s['footprint_mm'][1]:g} mm "
                  f"{s['scale']:<6} ops {s['ops']}  {s['parts']} part(s), {s['rows']} row(s), "
                  f"{s['rotated']} rotated, {s['fill']:.0%} filled")
            print(f"  {'':<24} {s['chambers']} chamber(s) incl. pads, {s['ports']} port(s), "
                  f"{s['thermal_zones']} thermal zone(s), {s['material']}, "
                  f"chain {s['chain_length_mm']:.1f} mm, crossing risk {s['crossing_risk']}, "
                  f"{s['connections']} connection(s) for S7")
            for skip in s["skipped_ops"]:
                print(f"  {'':<24} op {skip['op']} NOT on chip ({skip['where']}): {skip['why']}")
        for rerun in result["reruns"]:
            print(f"  RERUN s4  {rerun['reason']}")
        if result["unresolved"]:
            print(f"  {len(result['unresolved'])} unresolved value(s), all flagged in the specs:")
            for gap in result["unresolved"][:12]:
                print(f"    {gap.get('field', '?')}: {gap['question']}")

    if args.dry_run:
        print("  --dry-run: nothing written")
        return 1 if result["reruns"] else 0

    written = write(result, assay_id=args.id,
                    out_dir=Path(args.out_dir) if args.out_dir else None, force=args.force)
    for path in written:
        print(f"  wrote {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")
    if result["reruns"]:
        print("  s6_build: asking for a re-run from s4 (see the reasons above)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

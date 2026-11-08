"""S4 partition: ``unitops.json`` in, ``partition.json`` out - which ops go on which chip.

    python assay2chip/stages/s4_partition.py --id my_assay
    python assay2chip/stages/s4_partition.py --id my_assay --stdout
    python assay2chip/stages/s4_partition.py --mvp-chain          # the repo's own chain

One assay does not fit on one chip. Port count, area, incompatible temperatures, chemistry
and the single fluidic layer all force splits, and ``assay2chip/README.md`` calls deciding
*where* to cut the heart of this pipeline. This stage makes that decision, and every cut
it makes carries the constraint and the contract key that forced it.

**The constraints, each able to force a cut on its own** (``partition/constraints.py``):

* temperature - live cells cannot share a board with anything at ``rt.temp_c`` (42 C) or
  above, and the ``pcr.max_temp_c`` (98 C) ramp owns its board at demo scale because the
  board *is* the dock;
* ports - ``fabrication_floors.max_ports_per_chip``, counted from what each op actually
  needs and lifted at target scale exactly as the contract says it is;
* area - parts (volume / etch depth), walls, port pitch cells and channel runs against
  the demo, then the target envelope; never the ceiling, which is a maximum, not a scale;
* planarity - ONE fluidic layer, so no channel may cross another, tested exactly with the
  ports held on the board boundary;
* chemistry - fluids that may not share a wetted path, which is why a chaotropic lysate
  keeps its own outlet, and fluids that may not share a board at all;
* material - ops needing different polymers split (``contracts/materials.json``);
* placement - an ``off_chip`` op is a natural cut point and becomes a handoff.

**The output.** ``chips[]``, ``handoffs[]`` and ``cuts[]``, per
``contracts/schemas/partition.schema.json``. The headline number this stage exists to
produce is **how many handoffs still need a human**: it is printed, put in ``note`` and
left in ``handoffs[].automated`` for S10 to count. Nothing is ever marked automated to
make that number look better.

**Refusals.** A ``unitops.json`` that still holds a ``placement.where: "pending"`` op is
refused outright, naming the ops: pending is S2's explicit "not decided", S3 must replace
it, and partitioning an undecided op would mean inventing a board for it.

Deterministic and offline. No model call, no clock, no randomness: the same unitops in
gives a byte-identical partition out, so a diff always means a real change.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts, io as stage_io  # noqa: E402
from assay2chip.partition import constraints, mvp_chain, ops as ops_mod, segment  # noqa: E402
from assay2chip.partition.handoffs import count_manual  # noqa: E402
from assay2chip.partition.ops import PendingPlacement  # noqa: E402

#: Nothing outside assay2chip/work/<id>/ is written by this stage.
WRITES: List[str] = []

STAGE = "s4"


def plan_for(assay_id: str, unitops: Dict[str, Any],
             scale: Optional[str] = None) -> segment.Plan:
    """The partition decision for one unitops payload. Raises on a pending placement.

    `scale` pins every chip to one envelope ("demo" or "target"); None, the normal case,
    lets each chip take the cheapest envelope it fits.
    """
    return segment.partition(assay_id, ops_mod.load_ops(unitops), scale)


def to_partition(assay_id: str, unitops: Dict[str, Any],
                 plan: segment.Plan) -> Dict[str, Any]:
    """Render a plan as a schema-valid partition.json payload."""
    chips: List[Dict[str, Any]] = []
    for chip in plan.chips:
        verdict = chip.verdict
        entry: Dict[str, Any] = {
            "chip_id": chip.chip_id,
            "ops": chip.numbers,
            "scale": verdict.scale or "target",
            "reason": constraints.why_together(chip.ops, verdict),
        }
        if verdict.area is not None:
            total = verdict.area.total_mm2
            entry["area_mm2"] = round(total, 2) if total > 0 else None
        entry["ports"] = verdict.port_count
        span = constraints.thermal_span(chip.ops)
        if span is not None:
            entry["temp_range_c"] = [span[0], span[1]]
        notes = list(verdict.notes)
        if verdict.port_plan is not None:
            notes.append("ports: " + "; ".join(
                "%s (%s) - %s" % (p.id, p.role, p.why) for p in verdict.port_plan.ports))
        if notes:
            entry["note"] = " | ".join(notes)
        chips.append(entry)

    handoffs = [dict(h.data) for h in plan.handoffs]
    manual = count_manual(handoffs)

    unresolved = _unresolved(unitops, plan)

    note = ("%d chip(s), %d handoff(s), %d of them still needing a human. %s"
            % (len(chips), len(handoffs), manual, " | ".join(plan.notes)))

    payload: Dict[str, Any] = {
        "id": assay_id,
        "chips": chips,
        "handoffs": handoffs,
        "cuts": list(plan.cuts),
        "unresolved": unresolved,
        "note": note,
    }
    if unitops.get("placeholder"):
        payload["placeholder"] = True
    payload["llm"] = {
        "mode": "none", "model": None, "calls": 0, "tasks": [],
        "prompt_sha256": None,
        "note": "S4 is deterministic: the partition is decided by constraint checks on "
                "contract values, so no model is involved and the result is reproducible.",
    }
    return payload


def _unresolved(unitops: Dict[str, Any], plan: segment.Plan) -> List[Dict[str, Any]]:
    """Carried forward from unitops.json, then everything this plan had to leave open."""
    out: List[Dict[str, Any]] = []
    seen = set()

    def add(entry: Dict[str, Any]) -> None:
        allowed = ("step", "op", "chip_id", "field", "question", "assumed",
                   "assumed_from", "note")
        clean = {k: v for k, v in entry.items() if k in allowed and v is not None}
        if "question" not in clean:
            return
        key = (clean.get("op"), clean.get("chip_id"), clean.get("field"),
               clean["question"])
        if key in seen:
            return
        seen.add(key)
        out.append(clean)

    for entry in unitops.get("unresolved", []) or []:
        add(dict(entry))
    for entry in plan.gaps:
        add(dict(entry))
    for chip in plan.infeasible:
        for violation in chip.verdict.violations:
            add({
                "chip_id": chip.chip_id, "field": violation.constraint,
                "question": "%s cannot be built as planned: %s. It is one op or a group "
                            "that no envelope satisfies, so it needs a part change rather "
                            "than another cut." % (chip.chip_id, violation.why),
                "assumed_from": ", ".join(violation.keys) or None,
            })
    return out


def summary(payload: Dict[str, Any]) -> str:
    """A few lines a human can check at a glance."""
    lines = []
    manual = count_manual(payload["handoffs"])
    lines.append("s4_partition: %d chip(s), %d handoff(s), %d NOT automated"
                 % (len(payload["chips"]), len(payload["handoffs"]), manual))
    for chip in payload["chips"]:
        span = chip.get("temp_range_c")
        lines.append("  %-18s ops %-18s %-6s %2d ports %8s %s"
                     % (chip["chip_id"],
                        ",".join(str(n) for n in chip["ops"]),
                        chip["scale"],
                        chip.get("ports") or 0,
                        ("%.0f mm2" % chip["area_mm2"]) if chip.get("area_mm2") else "-",
                        ("%g-%g C" % (span[0], span[1])) if span else ""))
    for cut in payload["cuts"]:
        lines.append("  cut %s  [%s] %s" % (cut["between"], cut.get("forced_by", "?"),
                                            cut["why"]))
    for handoff in payload["handoffs"]:
        lines.append("  %s %s.%s -> %s.%s  %s%s  %s"
                     % ("AUTO  " if handoff["automated"] else "MANUAL",
                        handoff["from"], handoff["from_port"],
                        handoff["to"], handoff["to_port"], handoff["carries"],
                        (" %g uL" % handoff["volume_ul"])
                        if handoff.get("volume_ul") is not None else "",
                        handoff["how"]))
    if payload["unresolved"]:
        lines.append("  unresolved: %d" % len(payload["unresolved"]))
    return "\n".join(lines)


def run(ctx: Any = None, assay_id: str = "", *, id: str = "",
        unitops: Optional[Dict[str, Any]] = None, write: bool = True,
        scale: Optional[str] = None, llm_mode: str = "none", force: bool = False,
        **_: Any) -> Dict[str, Any]:
    """Stage entry point for assay2chip/run.py.

    Reads ``unitops.json`` for `assay_id` (or takes a payload directly, which is what the
    tests do), decides the partition and writes ``partition.json``. Returns the payload.
    """
    assay_id = assay_id or id or (getattr(ctx, "assay_id", "") if ctx else "")
    if unitops is None:
        if not assay_id:
            raise ValueError("s4_partition.run needs an assay id (--id) or a unitops payload")
        unitops = stage_io.read_stage(assay_id, "unitops")
    assay_id = assay_id or str(unitops.get("id", ""))
    if not assay_id:
        raise ValueError("s4_partition.run needs an assay id: unitops.json has no 'id'")

    plan = plan_for(assay_id, unitops, scale)
    payload = to_partition(assay_id, unitops, plan)
    if write:
        stage_io.write_stage(assay_id, "partition", payload)
    return payload


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="s4_partition",
        description="S4: decide which unit operations share a chip, and where to cut")
    parser.add_argument("--id", default="", help="assay id (the work directory name)")
    parser.add_argument("--stdout", action="store_true",
                        help="print the artefact, write nothing")
    parser.add_argument("--mvp-chain", action="store_true",
                        help="partition the repo's own MVP chain instead of a work file")
    parser.add_argument("--scale", choices=("demo", "target"), default=None,
                        help="pin every chip to one envelope instead of taking the "
                             "cheapest that fits")
    parser.add_argument("--contracts", action="store_true",
                        help="print the contract values this stage reads, then exit")
    args = parser.parse_args(argv)

    if args.contracts:
        print(json.dumps(_contract_report(), indent=2))
        return 0

    unitops: Optional[Dict[str, Any]] = None
    assay_id = args.id
    if args.mvp_chain:
        assay_id = assay_id or "mvp_chain"
        unitops = mvp_chain.unitops(assay_id)
    elif not assay_id:
        parser.error("give --id <assay id>, or --mvp-chain")

    try:
        payload = run(assay_id=assay_id, unitops=unitops, scale=args.scale,
                      write=not (args.stdout or args.mvp_chain))
    except PendingPlacement as error:
        print("s4_partition: %s" % error, file=sys.stderr)
        return 2
    except stage_io.StageFileMissing as error:
        print("s4_partition: %s" % error, file=sys.stderr)
        return 2
    except (contracts.ContractError, stage_io.StageValidationError) as error:
        print("s4_partition: %s" % error, file=sys.stderr)
        return 1

    if args.stdout or args.mvp_chain:
        if args.stdout:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(summary(payload))
    else:
        print(summary(payload))
        print("  -> %s" % stage_io.stage_path(assay_id, "partition")
              .relative_to(ROOT).as_posix())
    return 0


def _contract_report() -> Dict[str, Any]:
    """Everything S4 reads from the contracts, so a gap is visible without a run."""
    from assay2chip.partition import thermal

    return {
        "envelopes": {name: {"x_mm": env.x_mm, "y_mm": env.y_mm,
                             "area_mm2": round(env.area_mm2, 1)}
                      for name, env in contracts.envelopes().items()},
        "max_ports_per_chip": {"demo": contracts.max_ports_per_chip("demo"),
                               "target": contracts.max_ports_per_chip("target")},
        "floors": contracts.fabrication_floors(),
        "capsule": {"diameter_um": contracts.capsule_diameter_um(),
                    "swollen_diameter_um": contracts.swollen_capsule_diameter_um()},
        "temperatures": {"culture.temp_c": thermal.cells_ceiling_c(),
                         "rt.temp_c": thermal.lethal_c(),
                         "pcr.max_temp_c": thermal.thermocycle_c()},
        "viscosity_37c_pa_s": contracts.viscosity_for(37).pa_s,
        "control.error_threshold": contracts.param("control.error_threshold"),
        "materials": sorted(thermal.materials()),
        "missing_keys": contracts.missing_keys(),
    }


if __name__ == "__main__":
    raise SystemExit(main())

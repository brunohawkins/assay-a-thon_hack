"""Deterministic operator: protocol.json + chip_spec.json -> run file.

Each protocol step becomes one command (an exchange with several targets becomes one
command per target). The planner:
  1. validates both files against their schemas,
  2. checks every step's target exists in the chip spec,
  3. skips steps meant for another variant (a step's "variant" vs the run's run_parameters),
  4. maps the step to commands (split/pool fan-out comes from the chip's channels, passing
     through junctions), and checks each with safety.check_static (e.g. no pair valve may
     open in a control run),
  5. lays the commands out in time using each step's duration_s,
  6. uses each step's "note" as narration.

Usually called through run.py:
    python operator/run.py --protocol protocols/placeholder_scrnaseq.json --chip chip/placeholder_chip_spec.json
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import commands
import safety
import schema_io
from adapters.sim import SimAdapter

ROOT = schema_io.ROOT
RUNS_DIR = ROOT / "runs"


class PlanError(ValueError):
    """The protocol can't be run on this chip. Message lists every problem found."""


# --- chip topology ------------------------------------------------------------------


def _chambers(chip: dict) -> dict[str, dict]:
    return {c["id"]: c for c in chip["chambers"]}


PASS_THROUGH = ("junction",)  # chamber types capsules or cells flow through without stopping


def downstream_chambers(chip: dict, chamber_id: str) -> list[str]:
    """Chambers fed by a channel out of chamber_id, in channel order. Junctions are passed
    through, so a split that branches at a junction reaches the chambers beyond it."""
    chambers = _chambers(chip)
    out: list[str] = []
    for ch in chip["channels"]:
        nxt = ch["to"]
        if ch["from"] != chamber_id or nxt not in chambers:
            continue
        if chambers[nxt]["type"] in PASS_THROUGH:
            out += [c for c in downstream_chambers(chip, nxt) if c not in out]
        elif nxt not in out:
            out.append(nxt)
    return out


def upstream_chambers(chip: dict, chamber_id: str) -> list[str]:
    """Chambers with a channel into chamber_id, in channel order (through junctions)."""
    chambers = _chambers(chip)
    out: list[str] = []
    for ch in chip["channels"]:
        prev = ch["from"]
        if ch["to"] != chamber_id or prev not in chambers:
            continue
        if chambers[prev]["type"] in PASS_THROUGH:
            out += [c for c in upstream_chambers(chip, prev) if c not in out]
        elif prev not in out:
            out.append(prev)
    return out


# --- planning -------------------------------------------------------------------------


def check_inputs(protocol: dict, chip: dict) -> list[str]:
    """Every reason this protocol can't run on this chip, as readable lines."""
    problems = []
    for kind, data, label in (("protocol", protocol, "protocol"), ("chip_spec", chip, "chip spec")):
        for e in schema_io.schema_errors(kind, data):
            path = list(e.absolute_path)
            if kind == "protocol" and len(path) >= 2 and path[0] == "steps" and isinstance(path[1], int):
                where = f"step {path[1] + 1}"
            else:
                where = schema_io.path_str(e) or "(top level)"
            problems.append(f"{label} {where}: {schema_io.describe(e)}")
    if problems:
        return problems
    known = commands.place_ids(chip)
    for step in protocol["steps"]:
        for target in commands.step_targets(step):
            if target not in known:
                problems.append(f"step {step['step']}: target '{target}' not in chip {chip['id']}")
    return problems


def run_params_for(chip: dict, given: dict | None = None) -> dict:
    """The run's parameter values: given ones, else each parameter's default."""
    declared = chip.get("run_parameters") or {}
    out = {k: v["default"] for k, v in declared.items()}
    for k, v in (given or {}).items():
        if k not in declared:
            raise PlanError(f"chip {chip['id']} has no run parameter '{k}' (it has: {', '.join(declared) or 'none'})")
        if v not in declared[k]["values"]:
            raise PlanError(f"run parameter {k}={v!r} is not one of {', '.join(declared[k]['values'])}")
        out[k] = v
    return out


def step_applies(step: dict, run_params: dict) -> bool:
    """False for a step meant for another variant."""
    return "variant" not in step or run_params.get("variant") == step["variant"]


def step_to_commands(step: dict, chip: dict) -> list[tuple[str, dict]]:
    """Map one protocol step to its commands: one, or one per target for an exchange with targets."""
    if step["operation"] == "exchange" and step.get("targets"):
        reagents = step.get("reagents") or [step["reagent"]] * len(step["targets"])
        if len(reagents) != len(step["targets"]):
            raise PlanError(f"step {step['step']}: {len(step['targets'])} targets but {len(reagents)} reagents")
        return [step_to_command({**step, "target": t, "reagent": r, "targets": None}, chip)
                for t, r in zip(step["targets"], reagents)]
    return [step_to_command(step, chip)]


def step_to_command(step: dict, chip: dict) -> tuple[str, dict]:
    """Map one protocol step to (command, args)."""
    op, target, n = step["operation"], step.get("target"), step["step"]
    if op == "load":
        return "load", {"target": target, "volume_ul": step["volume_ul"]}
    if op == "exchange":
        args = {"target": target, "reagent": step["reagent"], "volume_ul": step["volume_ul"],
                "washes": step["washes"], "flow_ul_min": step["flow_ul_min"]}
        if step.get("direction"):
            args["direction"] = step["direction"]
        return "exchange", args
    if op == "hold_temp":
        return "hold_temp", {"target": target, "temp_c": step["temp_c"], "duration_s": step["duration_s"]}
    if op == "split":
        chambers = _chambers(chip)
        downstream = downstream_chambers(chip, target)
        targets = [c for c in downstream if chambers[c]["type"] == "split"] or downstream
        if not targets:
            raise PlanError(f"step {n}: split from '{target}' but no channel leads out of it to a chamber")
        return "split", {"source": target, "targets": targets}
    if op == "pool":
        sources = upstream_chambers(chip, target)
        if not sources:
            raise PlanError(f"step {n}: pool into '{target}' but no channel leads into it from a chamber")
        return "pool", {"sources": sources, "target": target}
    if op == "release":
        return "release", {"target": target, "reagent": step["reagent"]}
    targets = commands.step_targets(step)
    if op == "valve":
        return "valve", {"valves": targets, "state": step["valve_state"]}
    if op == "culture":
        args = {"targets": targets, "reagents": step["reagents"], "duration_s": step["duration_s"],
                "exchange_interval_s": step["exchange_interval_s"], "volume_ul": step["volume_ul"],
                "flow_ul_min": step["flow_ul_min"], "temp_c": step["temp_c"]}
        if len(step["reagents"]) != len(targets):
            raise PlanError(f"step {n}: {len(targets)} targets but {len(step['reagents'])} reagents")
        if step.get("factors"):
            args["factors"] = step["factors"]
        return "culture", args
    if op == "collect":
        return "collect", {"targets": targets, "volume_ul": step["volume_ul"]}
    if op == "harvest":
        return "harvest", {"targets": targets, "reagent": step["reagent"]}
    raise PlanError(f"step {n}: unknown operation {op!r}")


def plan(protocol: dict, chip: dict, adapter: SimAdapter | None = None, run_params: dict | None = None) -> SimAdapter:
    """Send each protocol step's commands to the adapter, after the static safety check.
    Steps for another variant are skipped and logged. Returns the adapter."""
    problems = check_inputs(protocol, chip)
    if problems:
        raise PlanError("\n".join(problems))
    adapter = adapter or SimAdapter(chip)
    run_params = run_params_for(chip, run_params)
    for step in protocol["steps"]:
        if not step_applies(step, run_params):
            adapter.note("skip", f"step {step['step']} skipped: only in the {step['variant']} variant "
                                 f"(this run: {run_params.get('variant')}). {step['note']}", step["step"])
            continue
        cmds = step_to_commands(step, chip)
        for command, args in cmds:
            verdict = safety.check_static(command, args, chip, run_params)
            if verdict.status == "blocked":
                raise PlanError(f"step {step['step']}: safety blocked {command}: {'; '.join(verdict.reasons)}")
            try:
                adapter.send(command, args, narration=step["note"], protocol_step=step["step"],
                             duration_s=step["duration_s"] / len(cmds))
            except commands.CommandError as e:
                raise PlanError(f"step {step['step']}: {e}") from e
    return adapter


# --- run files ------------------------------------------------------------------------


def repo_relative(path: Path | str) -> str:
    """Path relative to the repo root with forward slashes, as stored in run files."""
    p = Path(path).resolve()
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        raise PlanError(f"{path} is outside the repo; the twin can only load files inside it") from None


def run_header(protocol: dict, chip: dict, chip_path: Path | str, operator: str,
               model: str | None = None) -> dict:
    header = {
        "protocol_id": protocol["id"],
        "protocol_name": protocol["name"],
        "chip_id": chip["id"],
        "chip_spec": repo_relative(chip_path),
        "operator": operator,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "placeholder": bool(protocol.get("placeholder") or chip.get("placeholder")),
    }
    if model:
        header["model"] = model
    return header


def run_path(protocol_id: str, chip_id: str, operator: str = "planner", runs_dir: Path = RUNS_DIR,
             variant: str | None = None) -> Path:
    """runs/<protocol_id>__<chip_id>[__<variant>].json for planner runs; llm runs get a __llm suffix."""
    suffix = (f"__{variant}" if variant else "") + ("" if operator == "planner" else f"__{operator}")
    return Path(runs_dir) / f"{protocol_id}__{chip_id}{suffix}.json"


if __name__ == "__main__":
    import run

    raise SystemExit(run.main())

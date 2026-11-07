"""Deterministic operator: protocol.json + chip_spec.json -> run file.

Each protocol step becomes exactly one command. The planner:
  1. validates both files against their schemas,
  2. checks every step's target exists in the chip spec,
  3. maps the step to a command (split/pool fan-out comes from the chip's channels),
  4. lays the commands out in time using each step's duration_s,
  5. uses each step's "note" as narration.

Usually called through run.py:
    python operator/run.py --protocol protocols/placeholder_scrnaseq.json --chip chip/placeholder_chip_spec.json
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import commands
import schema_io
from adapters.sim import SimAdapter

ROOT = schema_io.ROOT
RUNS_DIR = ROOT / "runs"


class PlanError(ValueError):
    """The protocol can't be run on this chip. Message lists every problem found."""


# --- chip topology ------------------------------------------------------------------


def _chambers(chip: dict) -> dict[str, dict]:
    return {c["id"]: c for c in chip["chambers"]}


def downstream_chambers(chip: dict, chamber_id: str) -> list[str]:
    """Chambers fed directly by a channel out of chamber_id, in channel order."""
    chambers = _chambers(chip)
    return [ch["to"] for ch in chip["channels"] if ch["from"] == chamber_id and ch["to"] in chambers]


def upstream_chambers(chip: dict, chamber_id: str) -> list[str]:
    """Chambers with a channel into chamber_id, in channel order."""
    chambers = _chambers(chip)
    return [ch["from"] for ch in chip["channels"] if ch["to"] == chamber_id and ch["from"] in chambers]


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
    known = commands.chamber_ids(chip) | commands.port_ids(chip)
    for step in protocol["steps"]:
        if step["target"] not in known:
            problems.append(f"step {step['step']}: target '{step['target']}' not in chip {chip['id']}")
    return problems


def step_to_command(step: dict, chip: dict) -> tuple[str, dict]:
    """Map one protocol step to (command, args)."""
    op, target, n = step["operation"], step["target"], step["step"]
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
    raise PlanError(f"step {n}: unknown operation {op!r}")


def plan(protocol: dict, chip: dict, adapter: SimAdapter | None = None) -> SimAdapter:
    """Send one command per protocol step to the adapter. Returns the adapter."""
    problems = check_inputs(protocol, chip)
    if problems:
        raise PlanError("\n".join(problems))
    adapter = adapter or SimAdapter(chip)
    for step in protocol["steps"]:
        command, args = step_to_command(step, chip)
        try:
            adapter.send(command, args, narration=step["note"], protocol_step=step["step"],
                         duration_s=step["duration_s"])
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


def run_path(protocol_id: str, chip_id: str, operator: str = "planner", runs_dir: Path = RUNS_DIR) -> Path:
    """runs/<protocol_id>__<chip_id>.json for planner runs; llm runs get a __llm suffix."""
    suffix = "" if operator == "planner" else f"__{operator}"
    return Path(runs_dir) / f"{protocol_id}__{chip_id}{suffix}.json"


if __name__ == "__main__":
    import run

    raise SystemExit(run.main())

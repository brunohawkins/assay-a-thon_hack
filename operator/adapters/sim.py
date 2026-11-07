"""Simulation adapter: validates each command and records it as a run event.

This is the default adapter. Nothing moves; the run file it writes is what the twin
replays.
"""

from __future__ import annotations

from pathlib import Path

import commands
import schema_io


class SimAdapter:
    name = "sim"

    def __init__(self, chip: dict | None = None, start_t_s: float = 0.0):
        self.chip = chip
        self.t_s = float(start_t_s)
        self.events: list[dict] = []
        self.log: list[dict] = []

    def note(self, kind: str, message: str, step: int | None = None) -> None:
        """Add an operator-log line (e.g. a step skipped for this variant)."""
        self.log.append({"t_s": round(self.t_s, 3), "kind": kind, "step": step, "message": message})

    def send(self, command: str, args: dict, *, narration: str = "",
             protocol_step: int | None = None, duration_s: float | None = None) -> dict:
        """Validate and record one command. Raises commands.CommandError if it's invalid."""
        cmd = commands.validate(command, args, self.chip)
        if duration_s is None:
            duration_s = commands.estimate_duration_s(command, cmd["args"])
        event = {
            "t_s": round(self.t_s, 3),
            "duration_s": round(float(duration_s), 3),
            "command": command,
            "args": cmd["args"],
            "narration": narration,
            "protocol_step": protocol_step,
        }
        self.events.append(event)
        self.t_s += float(duration_s)
        return {"ok": True, "event_index": len(self.events) - 1,
                "t_s": event["t_s"], "duration_s": event["duration_s"]}

    def to_run(self, header: dict) -> dict:
        """Assemble a run dict (see contracts/schemas/run.schema.json)."""
        run = dict(header)
        run["total_duration_s"] = round(self.t_s, 3)
        run["events"] = list(self.events)
        if self.log:
            run["log"] = list(self.log)
        return run

    def write(self, path: Path | str, header: dict) -> dict:
        """Write the run file after checking it against the run schema."""
        run = self.to_run(header)
        errors = schema_io.schema_errors("run", run)
        if errors:
            details = "\n  ".join(f"{schema_io.path_str(e)}: {schema_io.describe(e)}" for e in errors)
            raise ValueError(f"run does not match run.schema.json:\n  {details}")
        schema_io.write_json(path, run)
        return run

"""The chip's instruction set: exactly six commands.

    load(target, volume_ul)
    exchange(target, reagent, volume_ul, washes, flow_ul_min)
    hold_temp(target, temp_c, duration_s)
    split(source, targets[])
    pool(sources[], target)
    release(target, reagent)

Each function returns a command dict {"command": ..., "args": {...}} after checking it
against contracts/schemas/commands.schema.json, and against a chip spec when one is
given. Adapters (adapters/sim.py, adapters/serial_pump.py) take these dicts.
"""

from __future__ import annotations

import copy
from typing import Any

import schema_io

COMMAND_NAMES = ("load", "exchange", "hold_temp", "split", "pool", "release")

# Which args hold chamber ids, per command. Used for chip checks and by the twin.
ID_ARGS = {
    "load": ("target",),
    "exchange": ("target",),
    "hold_temp": ("target",),
    "split": ("source", "targets"),
    "pool": ("sources", "target"),
    "release": ("target",),
}

# Nominal time for commands whose duration isn't implied by their args, in seconds.
NOMINAL_DURATION_S = 60.0


class CommandError(ValueError):
    """A command that doesn't fit the contract or the chip."""


# --- the six commands -------------------------------------------------------------


def load(target: str, volume_ul: float, chip: dict | None = None) -> dict:
    return validate("load", {"target": target, "volume_ul": volume_ul}, chip)


def exchange(target: str, reagent: str, volume_ul: float, washes: int, flow_ul_min: float,
             chip: dict | None = None) -> dict:
    return validate("exchange", {"target": target, "reagent": reagent, "volume_ul": volume_ul,
                                 "washes": washes, "flow_ul_min": flow_ul_min}, chip)


def hold_temp(target: str, temp_c: float, duration_s: float, chip: dict | None = None) -> dict:
    return validate("hold_temp", {"target": target, "temp_c": temp_c, "duration_s": duration_s}, chip)


def split(source: str, targets: list[str], chip: dict | None = None) -> dict:
    return validate("split", {"source": source, "targets": list(targets)}, chip)


def pool(sources: list[str], target: str, chip: dict | None = None) -> dict:
    return validate("pool", {"sources": list(sources), "target": target}, chip)


def release(target: str, reagent: str, chip: dict | None = None) -> dict:
    return validate("release", {"target": target, "reagent": reagent}, chip)


COMMANDS = {
    "load": load,
    "exchange": exchange,
    "hold_temp": hold_temp,
    "split": split,
    "pool": pool,
    "release": release,
}


# --- validation ------------------------------------------------------------------


def chamber_ids(chip: dict) -> set[str]:
    return {c["id"] for c in chip.get("chambers", [])}


def port_ids(chip: dict) -> set[str]:
    return {p["id"] for p in chip.get("ports", [])}


def ids_in(command: str, args: dict) -> list[str]:
    """Every chamber id a command refers to."""
    out: list[str] = []
    for key in ID_ARGS.get(command, ()):
        value = args.get(key)
        if isinstance(value, list):
            out.extend(v for v in value if isinstance(v, str))
        elif isinstance(value, str):
            out.append(value)
    return out


def validate(command: str, args: dict, chip: dict | None = None) -> dict:
    """Check one command. Returns {"command", "args"}; raises CommandError with a readable message."""
    if command not in COMMAND_NAMES:
        raise CommandError(f"unknown command {command!r}; the six commands are {', '.join(COMMAND_NAMES)}")
    cmd = {"command": command, "args": dict(args)}
    errors = schema_io.schema_errors("commands", cmd)
    if errors:
        details = "; ".join(schema_io.describe(e) for e in errors)
        raise CommandError(f"{command}: {details}")
    if chip is not None:
        chambers = chamber_ids(chip)
        for cid in ids_in(command, args):
            if cid not in chambers:
                hint = " (that is a port; commands act on chambers)" if cid in port_ids(chip) else ""
                raise CommandError(f"{command}: '{cid}' is not a chamber in chip {chip.get('id')}{hint}")
    return cmd


def estimate_duration_s(command: str, args: dict) -> float:
    """Best guess at how long a command takes, when nobody told us."""
    if command == "hold_temp":
        return float(args["duration_s"])
    if command == "exchange":
        return float(args["washes"]) * float(args["volume_ul"]) / float(args["flow_ul_min"]) * 60.0
    return NOMINAL_DURATION_S


# --- Claude API tool definitions ----------------------------------------------------

# Strict tool schemas don't accept numeric/length/pattern constraints, so these are
# stripped from the tool definitions and enforced by validate() instead.
_UNSUPPORTED_IN_STRICT = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems", "title",
}


def _strip(schema: Any) -> Any:
    if isinstance(schema, dict):
        return {k: _strip(v) for k, v in schema.items() if k not in _UNSUPPORTED_IN_STRICT}
    if isinstance(schema, list):
        return [_strip(v) for v in schema]
    return schema


def tool_definitions() -> list[dict]:
    """The six commands as Claude API tools, built from commands.schema.json.

    Each tool also takes `reason` (one sentence, shown as narration) and `protocol_step`.
    """
    defs = schema_io.schemas()["commands"]["$defs"]
    tools = []
    for name in COMMAND_NAMES:
        args_schema = copy.deepcopy(defs[name])
        input_schema = _strip(args_schema)
        input_schema["properties"]["reason"] = {
            "type": "string",
            "description": "One plain-English sentence explaining this decision. Shown to the audience as narration.",
        }
        input_schema["properties"]["protocol_step"] = {
            "type": "integer",
            "description": "The protocol step number this command carries out.",
        }
        # Strict tool use: list every property as required (optional args like
        # exchange.direction then always get a value, e.g. "forward").
        input_schema["required"] = list(input_schema["properties"])
        tools.append({
            "name": name,
            "description": f"{defs[name]['title']}. {defs[name]['description']}",
            "input_schema": input_schema,
            "strict": True,
        })
    return tools

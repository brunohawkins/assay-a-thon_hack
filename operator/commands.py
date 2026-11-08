"""The chip's instruction set.

    load(target, volume_ul)
    exchange(target, reagent, volume_ul, washes, flow_ul_min)
    hold_temp(target, temp_c, duration_s)
    split(source, targets[])
    pool(sources[], target)
    release(target, reagent)
    culture(targets[], reagents[], duration_s, exchange_interval_s, volume_ul, flow_ul_min, temp_c)
    collect(targets[], volume_ul)                            supernatant out, cells stay
    harvest(targets[], reagent)                              cells out through each outlet
    encapsulate(target, core/shell/oil_flow_ul_h, capsules)  Module 2: make capsules at a nozzle
    cure(target, wavelength_nm, duration_s)                  Module 2: light-cure capsule shells
    mix(target, reagent, volume_ul, dilution)                Module 1b: quench / resuspend in a mixer
    transfer(source, target)                                 Module 1b: cells to the next stage or out
    count(target)                                            Module 1b: cell count + viability at a QC window

Each function returns a command dict {"command": ..., "args": {...}} after checking it
against contracts/schemas/commands.schema.json, and against a chip spec when one is
given. Adapters (adapters/sim.py, adapters/syringe_pump.py) take these dicts.

There is no `valve` command. On-chip valves were dropped from the design - a pneumatic
membrane valve needs a second fluidic layer and we print one, so contracts/blocks.json
lists them as excluded and no chip declares any - and the command went with them. Routing
is by which port is fed, vented or capped. The rig's OFF-CHIP pinch/solenoid valves are
hardware, not a command: they live in operator/adapters/.
"""

from __future__ import annotations

import copy
from typing import Any

import schema_io

COMMAND_NAMES = ("load", "exchange", "hold_temp", "split", "pool", "release",
                 "culture", "collect", "harvest", "encapsulate", "cure", "mix", "transfer", "count")

# Which args hold chamber ids, per command. Used for chip checks and by the twin.
ID_ARGS = {
    "load": ("target",),
    "exchange": ("target",),
    "hold_temp": ("target",),
    "split": ("source", "targets"),
    "pool": ("sources", "target"),
    "release": ("target",),
    "culture": ("targets",),
    "collect": ("targets",),
    "harvest": ("targets",),
    "encapsulate": ("target",),
    "cure": ("target",),
    "mix": ("target",),
    "transfer": ("source",),       # its target may be a chamber or an outlet port (checked below)
    "count": ("target",),
}
# Args that hold valve ids. No command has any (the `valve` command is gone), so this is
# now empty and ids_in(..., VALVE_ARGS) finds nothing for every command. KEPT because
# tools/validate.py checks run files against it: an old run that still contains a valve
# event must keep being read and reported, not crash the validator.
VALVE_ARGS: dict[str, tuple[str, ...]] = {}

# Nominal time for commands whose duration isn't implied by their args, in seconds.
NOMINAL_DURATION_S = 60.0


class CommandError(ValueError):
    """A command that doesn't fit the contract or the chip."""


# --- commands -------------------------------------------------------------------


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


# --- validation ------------------------------------------------------------------


def chamber_ids(chip: dict) -> set[str]:
    return {c["id"] for c in chip.get("chambers", [])}


def port_ids(chip: dict) -> set[str]:
    return {p["id"] for p in chip.get("ports", [])}


def stage_ids(chip: dict) -> set[str]:
    """Off-chip stages (e.g. a cold delay in tubing). Commands may target them like chambers."""
    return {s["id"] for s in chip.get("stages", [])}


def valve_ids(chip: dict) -> set[str]:
    """Valves a chip declares. No chip declares any (one fluidic layer), so this is empty
    everywhere; kept because tools/validate.py checks chips and runs with it."""
    return {v["id"] for v in chip.get("valves", [])}


def place_ids(chip: dict) -> set[str]:
    """Everything a protocol step may name as a target: chambers, ports and stages."""
    return chamber_ids(chip) | port_ids(chip) | valve_ids(chip) | stage_ids(chip)


def step_targets(step: dict) -> list[str]:
    """The ids a protocol step acts on: its targets list, or its single target."""
    if step.get("targets"):
        return list(step["targets"])
    return [step["target"]] if step.get("target") else []


def ids_in(command: str, args: dict, which: dict | None = None) -> list[str]:
    """Every chamber id a command refers to (valve ids with which=VALVE_ARGS).

    `which` is used when given, even when it is empty: `which or ID_ARGS` would fall back
    to the chamber map for an empty VALVE_ARGS and check chamber ids against valve ids.
    """
    out: list[str] = []
    for key in (ID_ARGS if which is None else which).get(command, ()):
        value = args.get(key)
        if isinstance(value, list):
            out.extend(v for v in value if isinstance(v, str))
        elif isinstance(value, str):
            out.append(value)
    return out


def validate(command: str, args: dict, chip: dict | None = None) -> dict:
    """Check one command. Returns {"command", "args"}; raises CommandError with a readable message."""
    if command not in COMMAND_NAMES:
        raise CommandError(f"unknown command {command!r}; the commands are {', '.join(COMMAND_NAMES)}")
    cmd = {"command": command, "args": dict(args)}
    errors = schema_io.schema_errors("commands", cmd)
    if errors:
        details = "; ".join(schema_io.describe(e) for e in errors)
        raise CommandError(f"{command}: {details}")
    if chip is not None:
        chambers = chamber_ids(chip) | stage_ids(chip)
        for cid in ids_in(command, args):
            if cid not in chambers:
                hint = " (that is a port; commands act on chambers)" if cid in port_ids(chip) else ""
                raise CommandError(f"{command}: '{cid}' is not a chamber in chip {chip.get('id')}{hint}")
        if command == "transfer" and args["target"] not in chambers | port_ids(chip):
            raise CommandError(f"transfer: '{args['target']}' is not a chamber or port in chip {chip.get('id')}")
        if args.get("waste") and args["waste"] not in port_ids(chip):
            raise CommandError(f"{command}: waste '{args['waste']}' is not a port in chip {chip.get('id')}")
        valves = valve_ids(chip)
        for vid in ids_in(command, args, VALVE_ARGS):
            if vid not in valves:
                raise CommandError(f"{command}: '{vid}' is not a valve in chip {chip.get('id')}")
    return cmd


def estimate_duration_s(command: str, args: dict) -> float:
    """Best guess at how long a command takes, when nobody told us."""
    if command == "hold_temp":
        return float(args["duration_s"])
    if command == "exchange":
        return float(args["washes"]) * float(args["volume_ul"]) / float(args["flow_ul_min"]) * 60.0
    if command == "culture":
        return float(args["duration_s"])
    if command == "cure":
        return float(args["duration_s"])
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
    """The commands as Claude API tools, built from commands.schema.json.

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

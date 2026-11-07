"""The module generators, callable from the natural-language design tool.

Each generator's parameters are documented in ONE JSON Schema
(contracts/schemas/generators/<name>.schema.json: names, types, defaults, units in the
names, x-source for where a default comes from). An NL step only has to turn a request
into a parameter object; this module does the rest.

    from generators import list_generators, build, tool_definitions
    list_generators()                                 # name, module, script, parameters
    build("m2_encapsulation", {"nozzle_width_um": 50})  # -> chip spec dict, validated, nothing written
    tool_definitions()                                # Claude API tools, one per generator

    python chip/nl_to_spec/generators.py --list
    python chip/nl_to_spec/generators.py m2_encapsulation '{"nozzle_width_um": 50}'          # print the spec
    python chip/nl_to_spec/generators.py m2_encapsulation '{"nozzle_width_um": 50}' --write  # the full generator run

--write runs the generator itself (cad/generators/<name>.py --params-json ...): it writes the
spec, runs the design check, prints the sizing summary, exports the display model and
regenerates the runs for the twin.
"""

from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GEN_DIR = ROOT / "cad" / "generators"
sys.path.insert(0, str(GEN_DIR))

import genlib  # noqa: E402

GENERATORS = {  # name -> module it builds
    "m1_culture": "m1", "m1b_cellprep": "m1b", "m2_encapsulation": "m2", "m3_wash": "m3",
}


def _module(name: str):
    if name not in GENERATORS:
        raise KeyError(f"unknown generator {name!r}; known: {', '.join(GENERATORS)}")
    spec = importlib.util.spec_from_file_location(f"gen_{name}", GEN_DIR / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def list_generators() -> list[dict]:
    out = []
    for name, module in GENERATORS.items():
        schema = genlib.schema_for(name)
        out.append({"name": name, "module": module, "script": f"cad/generators/{name}.py",
                    "schema": f"contracts/schemas/generators/{name}.schema.json", "description": schema["description"],
                    "parameters": schema["properties"]})
    return out


def build(name: str, params: dict) -> dict:
    """Chip spec for these parameters (defaults filled in, schema- and rule-checked). Raises genlib.ParamError."""
    return _module(name).build(genlib.validate_params(name, params))


def write(name: str, params: dict) -> int:
    """Run the generator for real (writes files, checks, exports, regenerates runs)."""
    return subprocess.call([sys.executable, str(GEN_DIR / f"{name}.py"), "--params-json", json.dumps(params)])


_STRICT_UNSUPPORTED = {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "default", "$schema", "$id", "title"}


def tool_definitions() -> list[dict]:
    """One Claude API tool per generator. Every parameter is optional (unset = default);
    numeric limits are enforced by build() / the schema, not by the tool definition."""
    tools = []
    for g in list_generators():
        props = {}
        for key, prop in copy.deepcopy(g["parameters"]).items():
            desc = f"{prop.get('description', '')} Default {prop.get('default')}."
            props[key] = {k: v for k, v in prop.items() if k not in _STRICT_UNSUPPORTED and not k.startswith("x-")}
            props[key]["description"] = desc
        tools.append({"name": f"design_{g['name']}", "description": f"Generate the Module {g['module']} chip spec. {g['description']}",
                      "input_schema": {"type": "object", "properties": props, "additionalProperties": False}})
    return tools


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Call a module generator with a parameter object.")
    ap.add_argument("name", nargs="?", help=", ".join(GENERATORS))
    ap.add_argument("params", nargs="?", default="{}", help="JSON object of parameters (unset = default)")
    ap.add_argument("--list", action="store_true", help="list the generators and their parameters")
    ap.add_argument("--write", action="store_true", help="run the generator for real (write, check, export, runs)")
    args = ap.parse_args(argv)
    if args.list or not args.name:
        for g in list_generators():
            print(f"{g['name']}  (Module {g['module']}, {g['script']}; schema {g['schema']})")
            for key, prop in g["parameters"].items():
                print(f"    {key:28s} default {prop.get('default')!s:8s} {prop.get('description', '')}")
        return 0
    params = json.loads(args.params)
    if args.write:
        return write(args.name, params)
    try:
        print(json.dumps(build(args.name, params), indent=2))
    except genlib.ParamError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

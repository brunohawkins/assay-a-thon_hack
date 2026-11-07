"""Shared plumbing for the module generators (cad/generators/m*.py).

A generator's parameters are defined ONCE, in contracts/schemas/generators/<name>.schema.json
(names, types, defaults, units, sources). This module turns that schema into command-line
flags, so

    python cad/generators/m2_encapsulation.py --nozzle-um 50 --oil-flow-ul-h 800

and Yuvraj's natural-language tool (which reads the same schema) set the same parameters.
Standard library + jsonschema only.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "operator"))

import schema_io  # noqa: E402

SCHEMA_DIR = ROOT / "contracts" / "schemas" / "generators"


class ParamError(ValueError):
    """Parameters that break the generator's schema or its design rules."""


def schema_for(name: str) -> dict:
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


def defaults(name: str) -> dict:
    return {k: v["default"] for k, v in schema_for(name)["properties"].items() if "default" in v}


def validate_params(name: str, params: dict) -> dict:
    """Fill defaults, then check against the schema. Raises ParamError with readable lines."""
    import jsonschema

    schema = schema_for(name)
    full = {**defaults(name), **params}
    errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(full), key=lambda e: list(e.path))
    if errors:
        raise ParamError("\n".join(f"{'/'.join(map(str, e.path)) or '(params)'}: {e.message}" for e in errors))
    return full


def parse_params(name: str, argv: list[str] | None, description: str) -> tuple[dict, argparse.Namespace]:
    """--<param> flags from the schema, plus --params-json (a JSON object or a file holding one)."""
    schema = schema_for(name)
    ap = argparse.ArgumentParser(description=description,
                                 epilog=f"Parameters are defined in {(SCHEMA_DIR / (name + '.schema.json')).relative_to(ROOT)}.")
    for key, prop in schema["properties"].items():
        kind = prop.get("type")
        flag = "--" + key.replace("_", "-")
        help_ = f"{prop.get('description', '')} (default {prop.get('default')})"
        if kind == "boolean":
            ap.add_argument(flag, dest=key, type=_bool, metavar="true|false", help=help_)
        elif kind in ("number", "integer"):
            ap.add_argument(flag, dest=key, type=float if kind == "number" else int, help=help_)
        else:
            ap.add_argument(flag, dest=key, help=help_)
    ap.add_argument("--params-json", help="all parameters as a JSON object, or a path to a .json file holding one")
    ap.add_argument("--out", type=Path, help="where to write the spec (default: the module's cad/*_spec.json)")
    ap.add_argument("--no-export", action="store_true", help="skip the display-model STL export")
    args = ap.parse_args(argv)
    params: dict = {}
    if args.params_json:
        text = args.params_json
        if not text.lstrip().startswith("{"):
            text = Path(text).read_text(encoding="utf-8")
        params.update(json.loads(text))
    for key in schema["properties"]:
        if getattr(args, key, None) is not None:
            params[key] = getattr(args, key)
    return validate_params(name, params), args


def _bool(text: str) -> bool:
    if text.lower() in ("1", "true", "yes", "on"):
        return True
    if text.lower() in ("0", "false", "no", "off"):
        return False
    raise argparse.ArgumentTypeError(f"expected true or false, got {text!r}")


def r(x: float, nd: int = 4) -> float:
    return round(float(x), nd)


def edge_toward(center: tuple[float, float], radius: float, toward: tuple[float, float]) -> list[float]:
    """Point on a circle's edge facing another point (where a channel meets a round chamber)."""
    dx, dy = toward[0] - center[0], toward[1] - center[1]
    n = math.hypot(dx, dy) or 1.0
    return [r(center[0] + radius * dx / n, 3), r(center[1] + radius * dy / n, 3)]


def write_spec(spec: dict, out: Path) -> None:
    errors = schema_io.schema_errors("chip_spec", spec)
    if errors:
        raise ParamError("generated spec breaks contracts/schemas/chip_spec.schema.json:\n" + "\n".join(
            f"  {schema_io.path_str(e)}: {schema_io.describe(e)}" for e in errors[:10]))
    schema_io.write_json(out, spec)


def rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)

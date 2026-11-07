"""Load the contract schemas in contracts/schemas/ and validate data against them.

Shared by operator/, tools/validate.py and chip/nl_to_spec/. Standard library plus
jsonschema only.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "contracts" / "schemas"
SCHEMA_FILES = {
    "protocol": "protocol.schema.json",
    "chip_spec": "chip_spec.schema.json",
    "commands": "commands.schema.json",
    "run": "run.schema.json",
    "parameters": "parameters.schema.json",
}

_validators: dict[str, Draft202012Validator] | None = None
_schemas: dict[str, dict] | None = None


def load_json(path: Path | str) -> Any:
    """Read a JSON file as UTF-8 (explicit, so Windows laptops behave the same)."""
    with open(path, encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"{path}: not valid JSON (line {e.lineno}, column {e.colno}: {e.msg})") from None


def write_json(path: Path | str, data: Any) -> None:
    """Write JSON the same way everywhere: 2-space indent, UTF-8, trailing newline."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def schemas() -> dict[str, dict]:
    global _schemas
    if _schemas is None:
        _schemas = {kind: load_json(SCHEMA_DIR / name) for kind, name in SCHEMA_FILES.items()}
    return _schemas


def validator(kind: str) -> Draft202012Validator:
    """Validator for one of: protocol, chip_spec, commands, run."""
    global _validators
    if _validators is None:
        all_schemas = schemas()
        registry = Registry().with_resources(
            (s["$id"], Resource.from_contents(s)) for s in all_schemas.values()
        )
        _validators = {
            kind: Draft202012Validator(s, registry=registry) for kind, s in all_schemas.items()
        }
    return _validators[kind]


def schema_errors(kind: str, data: Any) -> list[ValidationError]:
    """All schema errors, in document order."""
    return sorted(validator(kind).iter_errors(data), key=lambda e: list(map(str, e.absolute_path)))


def describe(error: ValidationError) -> str:
    """Turn a jsonschema error into a short sentence a non-programmer can act on."""
    path = list(error.absolute_path)
    field = str(path[-1]) if path and isinstance(path[-1], str) else None
    if error.validator == "required":
        return error.message.replace("is a required property", "is missing")
    if error.validator == "additionalProperties":
        return error.message + " (check the spelling against contracts/README.md)"
    if error.validator == "unevaluatedProperties":
        return error.message + " (field not in the contract)"
    if error.validator == "type" and error.instance is None and field:
        return f"{field} must be filled in (not null) for this kind of step"
    if error.validator == "pattern" and isinstance(error.instance, str):
        return f"{error.instance!r} must be lowercase snake_case, like exchange_1 or wash_buffer"
    if field:
        return f"{field}: {error.message}"
    return error.message


def path_str(error: ValidationError) -> str:
    """JSON path like steps[3].volume_ul, for messages."""
    out = ""
    for part in error.absolute_path:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out

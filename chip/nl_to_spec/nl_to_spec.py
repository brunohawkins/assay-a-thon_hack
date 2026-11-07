"""Natural language -> chip spec.

    python chip/nl_to_spec/nl_to_spec.py chip/nl_to_spec/prompts/example_prompt.txt
    python chip/nl_to_spec/nl_to_spec.py my_prompt.txt --out chip/nl_to_spec/out/chip_spec.json

STUB: for now this ignores the prompt's content and returns the placeholder chip
(chip/placeholder_chip_spec.json), so everything downstream has a valid file to use.

TODO (Yuvraj + Szonja): replace generate_spec() with the real NL -> spec step
(rules, an LLM call, or both). Whatever it does, the output must pass
contracts/schemas/chip_spec.schema.json, which this script checks before writing.
When a generated spec is good, copy it into chip/<name>.json so tools/validate.py
and the twin pick it up.
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "operator"))

import schema_io  # noqa: E402

PLACEHOLDER = ROOT / "chip" / "placeholder_chip_spec.json"
DEFAULT_OUT = ROOT / "chip" / "nl_to_spec" / "out" / "chip_spec.json"


def generate_spec(prompt: str) -> dict:
    """Turn a plain-English description into a chip spec dict.

    TODO: real implementation. Returns a copy of the placeholder chip for now.
    """
    spec = copy.deepcopy(schema_io.load_json(PLACEHOLDER))
    first_line = prompt.strip().splitlines()[0] if prompt.strip() else ""
    spec["description"] = f"STUB OUTPUT (placeholder chip). Prompt was: {first_line[:200]}"
    return spec


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Natural language -> chip_spec.json (stub).")
    parser.add_argument("prompt", type=Path, help="text file describing the chip")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output path (default: {DEFAULT_OUT.relative_to(ROOT)})")
    args = parser.parse_args(argv)

    prompt = args.prompt.read_text(encoding="utf-8")
    spec = generate_spec(prompt)

    errors = schema_io.schema_errors("chip_spec", spec)
    if errors:
        for e in errors:
            print(f"ERROR {schema_io.path_str(e) or '(top level)'}: {schema_io.describe(e)}", file=sys.stderr)
        return 1
    schema_io.write_json(args.out, spec)
    print(f"wrote {args.out}  (chip {spec['id']}, {len(spec['chambers'])} chambers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

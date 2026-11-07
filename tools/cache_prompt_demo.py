"""Cache Yuvraj's prompt-to-chip result for the presenter view (works offline, branch not merged).

    python tools/cache_prompt_demo.py                                  # from origin/chip/flowmatter-paper-to-chip
    python tools/cache_prompt_demo.py --ref <branch or commit> --design capsule_prep_v1

Yuvraj's tool (branch chip/flowmatter-paper-to-chip, chip/components/build_chip.py) turns a
design file (protocol steps grouped into Flowmatter library parts by Claude, following
chip/designs/README.md) into a chip spec in our contract format, plus one placed STL per part.
This copies its committed output, unchanged, into docs/demo/prompt_to_chip/:

    design.json       chip/designs/<id>.json            (the grouping: modules, parts, steps)
    chip_spec.json    chip/<id>.json                    (the generated chip spec)
    stl/<module>.stl  cad/exports/<id>/<module>.stl     (each placed library part)

and runs OUR checks on the spec: the chip-spec schema and tools/check_chip_spec.py. The result
goes to check.json, which the presenter shows as it is (if the checker finds errors, the
presenter says so). Nothing on the branch is changed. Standard library + jsonschema; needs git.
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "demo" / "prompt_to_chip"
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "tools"))

import check_chip_spec as ccs  # noqa: E402
import schema_io  # noqa: E402


def git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True).stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ref", default="origin/chip/flowmatter-paper-to-chip")
    ap.add_argument("--design", default="capsule_prep_v1")
    args = ap.parse_args(argv)
    try:
        commit = git_bytes("rev-parse", "--short", args.ref).decode().strip()
        design = json.loads(git_bytes("show", f"{args.ref}:chip/designs/{args.design}.json"))
        spec_bytes = git_bytes("show", f"{args.ref}:chip/{args.design}.json")
    except subprocess.CalledProcessError as e:
        print(f"Could not read {args.ref}: {e.stderr.decode().strip()} (git fetch first?)", file=sys.stderr)
        return 1
    spec = json.loads(spec_bytes)
    (OUT / "stl").mkdir(parents=True, exist_ok=True)
    for old in (OUT / "stl").glob("*.stl"):
        old.unlink()
    (OUT / "design.json").write_bytes(git_bytes("show", f"{args.ref}:chip/designs/{args.design}.json"))
    (OUT / "chip_spec.json").write_bytes(spec_bytes)
    for m in design["modules"]:
        (OUT / "stl" / f"{m['id']}.stl").write_bytes(git_bytes("show", f"{args.ref}:cad/exports/{args.design}/{m['id']}.stl"))

    schema = [f"{schema_io.path_str(e) or '(top level)'}: {schema_io.describe(e)}" for e in schema_io.schema_errors("chip_spec", spec)]
    params = schema_io.load_json(ROOT / "contracts" / "parameters.json")
    materials = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
    fab = schema_io.load_json(ROOT / "cad" / "fabrication.json")
    found = ccs.check(spec, [], params, materials, fab)
    result = {
        "ref": args.ref, "commit": commit, "design": args.design,
        "checked_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "schema_errors": schema, "errors": found.errors, "warnings": found.warnings, "todo": found.todo,
        "passes": not schema and not found.errors,
        "counts": {"parts": len(spec["chambers"]), "ports": len(spec["ports"]), "channels": len(spec["channels"]),
                   "footprint_mm": [spec["footprint"]["x"], spec["footprint"]["y"]]},
    }
    schema_io.write_json(OUT / "check.json", result)
    print(f"cached {args.design} from {args.ref} @ {commit} into {OUT.relative_to(ROOT)}: "
          f"{len(design['modules'])} parts, schema {'OK' if not schema else f'{len(schema)} errors'}, "
          f"check_chip_spec {len(found.errors)} error(s), {len(found.warnings)} warning(s), {len(found.todo)} TODO")
    return 0 if result["passes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

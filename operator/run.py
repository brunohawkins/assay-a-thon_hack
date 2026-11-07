"""Generate run files for the twin.

    python operator/run.py --protocol protocols/placeholder_scrnaseq.json --chip chip/placeholder_chip_spec.json
    python operator/run.py --protocol protocols/placeholder_scrnaseq.json --chip chip/placeholder_chip_spec.json --llm
    python operator/run.py --all                 # every run again on its own chip; new protocols on the placeholder chip
    python operator/run.py --reindex             # only rebuild runs/index.json

Writes runs/<protocol_id>__<chip_id>.json (llm runs: ...__llm.json) and rebuilds
runs/index.json, which the twin's dropdown reads.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import planner
import schema_io
import sizing

ROOT = schema_io.ROOT
DEFAULT_CHIP = ROOT / "chip" / "placeholder_chip_spec.json"
INDEX_NAME = "index.json"


def generate(protocol_path: Path, chip_path: Path, use_llm: bool = False, runs_dir: Path = planner.RUNS_DIR) -> Path:
    protocol = schema_io.load_json(protocol_path)
    chip = schema_io.load_json(chip_path)

    operator, model = "planner", None
    adapter = None
    if use_llm:
        print("The LLM operator drives the closed-loop controller: give a scenario (--scenario). "
              "Falling back to the deterministic planner for this simple run.")
    if adapter is None:
        adapter = planner.plan(protocol, chip)

    out = planner.run_path(protocol["id"], chip["id"], operator, runs_dir)
    header = planner.run_header(protocol, chip, chip_path, operator, model)
    header["sizing"] = sizing.chip_sizing(chip)
    run = adapter.write(out, header)
    print(f"wrote {planner.repo_relative(out)}  ({len(run['events'])} events, "
          f"{run['total_duration_s']:.0f} s of protocol time, operator={operator})")
    return out


def rebuild_index(runs_dir: Path = planner.RUNS_DIR) -> Path:
    """Scan runs/*.json and write runs/index.json for the twin."""
    entries = []
    for path in sorted(Path(runs_dir).glob("*.json")):
        if path.name == INDEX_NAME:
            continue
        try:
            run = schema_io.load_json(path)
        except ValueError as e:
            print(f"skipping {path.name}: not valid JSON ({e})")
            continue
        entries.append({
            "file": path.name,
            "protocol_id": run.get("protocol_id"),
            "protocol_name": run.get("protocol_name", run.get("protocol_id")),
            "chip_id": run.get("chip_id"),
            "chip_spec": run.get("chip_spec"),
            "operator": run.get("operator"),
            "events": len(run.get("events", [])),
            "total_duration_s": run.get("total_duration_s"),
            "placeholder": run.get("placeholder", False),
        })
    entries.sort(key=lambda e: (str(e["protocol_name"]), str(e["operator"]), e["file"]))
    out = Path(runs_dir) / INDEX_NAME
    schema_io.write_json(out, {"generated_by": "operator/run.py", "runs": entries})
    print(f"wrote {planner.repo_relative(out)}  ({len(entries)} runs)")
    return out


def all_pairs(chip: Path | None) -> list[tuple[Path, Path]]:
    """(protocol, chip) pairs for --all. With --chip: every protocol on that chip.
    Without: every existing run again on its own chip, plus protocols that have no run
    yet on the default chip."""
    protocols = sorted((ROOT / "protocols").glob("*.json"))
    if chip is not None:
        return [(p, chip) for p in protocols]
    by_id = {schema_io.load_json(p).get("id"): p for p in protocols}
    pairs, covered = [], set()
    for run_path in sorted(planner.RUNS_DIR.glob("*.json")):
        if run_path.name == INDEX_NAME:
            continue
        run = schema_io.load_json(run_path)
        if run.get("operator") != "planner" or run.get("protocol_id") not in by_id or not run.get("chip_spec"):
            continue
        pair = (by_id[run["protocol_id"]], ROOT / run["chip_spec"])
        if pair not in pairs:
            pairs.append(pair)
        covered.add(run["protocol_id"])
    pairs += [(p, DEFAULT_CHIP) for pid, p in by_id.items() if pid not in covered]
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Turn a protocol + chip spec into a run file for the twin.")
    parser.add_argument("--protocol", type=Path, help="protocol JSON, e.g. protocols/placeholder_scrnaseq.json")
    parser.add_argument("--chip", type=Path, default=None, help=f"chip spec JSON (default for --all: {planner.repo_relative(DEFAULT_CHIP)})")
    parser.add_argument("--llm", action="store_true", help="let Claude operate (needs ANTHROPIC_API_KEY; falls back to the planner)")
    parser.add_argument("--all", action="store_true", help="regenerate every run on its own chip (or every protocol on --chip)")
    parser.add_argument("--reindex", action="store_true", help="only rebuild runs/index.json")
    args = parser.parse_args(argv)

    try:
        if args.reindex and not (args.protocol or args.all):
            rebuild_index()
            return 0
        if args.all:
            for protocol_path, chip_path in all_pairs(args.chip):
                try:
                    generate(protocol_path, chip_path, args.llm)
                except planner.PlanError as e:
                    print(f"skipped {planner.repo_relative(protocol_path)} on {planner.repo_relative(chip_path)}: "
                          f"{str(e).splitlines()[0]}")
        elif args.protocol and args.chip:
            generate(args.protocol, args.chip, args.llm)
        else:
            parser.error("give --protocol and --chip, or --all, or --reindex")
        rebuild_index()
    except planner.PlanError as e:
        print(f"ERROR: cannot run this protocol on this chip:\n{e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"ERROR: file not found: {e.filename}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

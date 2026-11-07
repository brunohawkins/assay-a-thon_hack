"""Generate run files for the twin.

Closed-loop runs (the management system; SIMULATED HARDWARE by default):

    python operator/run.py --scenario scenarios/m3_wash_clog.json
    python operator/run.py --protocol protocols/m3_wash.json --chip cad/m3_wash_spec.json \\
        --operator planner|llm|scripted --adapter virtual|dryrun|syringe_pump --scenario scenarios/m3_wash_clog.json
    python operator/run.py --scenario scenarios/m3_wash_clog.json --operator llm          # needs ANTHROPIC_API_KEY
    python operator/run.py --scenario scenarios/m3_wash_clog.json --operator scripted \\
        --transcript runs/transcripts/<run>.json                                          # offline replay

  writes runs/<protocol>__<chip>__<scenario>[__<operator>].json, the report
  runs/<...>_report.md and the operator transcript runs/transcripts/<...>.json.

Simple runs (no scenario: one command per step, no simulation):

    python operator/run.py --protocol protocols/placeholder_scrnaseq.json --chip chip/placeholder_chip_spec.json

Housekeeping:

    python operator/run.py --all          # regenerate every run (planner runs; LLM runs are kept)
    python operator/run.py --reindex      # only rebuild runs/index.json (what the twin's dropdown reads)
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
TRANSCRIPTS = planner.RUNS_DIR / "transcripts"


# --- simple runs ----------------------------------------------------------------------


def generate(protocol_path: Path, chip_path: Path, use_llm: bool = False, runs_dir: Path = planner.RUNS_DIR) -> Path:
    protocol = schema_io.load_json(protocol_path)
    chip = schema_io.load_json(chip_path)
    if use_llm:
        print("The LLM operator drives the closed-loop controller: give a scenario (--scenario). "
              "Falling back to the deterministic planner for this simple run.")
    adapter = planner.plan(protocol, chip)
    out = planner.run_path(protocol["id"], chip["id"], "planner", runs_dir)
    header = planner.run_header(protocol, chip, chip_path, "planner")
    header["sizing"] = sizing.chip_sizing(chip)
    run = adapter.write(out, header)
    print(f"wrote {planner.repo_relative(out)}  ({len(run['events'])} events, "
          f"{run['total_duration_s']:.0f} s of protocol time, operator=planner)")
    return out


# --- closed-loop runs -------------------------------------------------------------------


def make_adapter(kind: str, chip: dict, params: dict, scenario: dict, reagents: list[str]):
    if kind == "virtual":
        from adapters.virtual_chip import VirtualChip
        from faults import build_faults
        return VirtualChip(chip, params, scenario, reagents=reagents, faults=build_faults(scenario))
    if kind == "dryrun":
        from adapters.dryrun import DryRun
        return DryRun(chip, params, scenario, reagents=reagents)
    if kind == "syringe_pump":
        from adapters.syringe_pump import SyringePumpAdapter
        return SyringePumpAdapter(chip, params, scenario, reagents=reagents)
    raise ValueError(f"unknown adapter {kind!r}: use virtual, dryrun or syringe_pump")


def closed_loop(scenario_path: Path, protocol_path: Path | None, chip_path: Path | None, operator: str = "planner",
                adapter_kind: str = "virtual", transcript_path: Path | None = None, max_tool_calls: int | None = None) -> Path:
    import report
    import safety
    from controller import Controller
    from faults import Monitor
    from operators import run_planner, run_scripted, transcript_from

    scenario = schema_io.load_json(scenario_path)
    scenario.pop("_about", None)
    scenario.setdefault("name", scenario_path.stem)
    protocol_path = Path(protocol_path or ROOT / scenario["protocol"])
    chip_path = Path(chip_path or ROOT / scenario["chip"])
    protocol, chip = schema_io.load_json(protocol_path), schema_io.load_json(chip_path)
    problems = planner.check_inputs(protocol, chip)
    if problems:
        raise planner.PlanError("\n".join(problems))
    params, _ = safety._contracts()
    reagents = sorted({s["reagent"] for s in protocol["steps"] if s.get("reagent")} | {"capsule_suspension"})
    adapter = make_adapter(adapter_kind, chip, params, scenario, reagents)

    model_name, source = None, operator
    if operator == "llm":
        import llm_operator
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            print("ANTHROPIC_API_KEY is not set: falling back to the deterministic planner.")
            operator = "planner"
    ctl = Controller(protocol, chip, adapter, scenario, operator=operator, monitor=Monitor(params))
    if operator == "llm":
        try:
            print(f"Asking {llm_operator.MODEL} to operate the (simulated) chip...")
            llm_operator.run_llm(ctl, key, max_tool_calls=max_tool_calls or llm_operator.DEFAULT_TOOL_CALL_CAP)
            model_name, source = llm_operator.MODEL, "llm"
        except llm_operator.LLMError as e:
            print(f"LLM operator failed ({e}): falling back to the deterministic planner.")
            adapter = make_adapter(adapter_kind, chip, params, scenario, reagents)
            operator = "planner"
            ctl = Controller(protocol, chip, adapter, scenario, operator=operator, monitor=Monitor(params))
            run_planner(ctl)
    elif operator == "scripted":
        stem = f"{protocol['id']}__{chip['id']}__{scenario['name']}"
        transcript_path = Path(transcript_path or TRANSCRIPTS / f"{stem}__llm.json")
        if not transcript_path.exists():
            raise FileNotFoundError(transcript_path)
        transcript = schema_io.load_json(transcript_path)
        print(f"Replaying {planner.repo_relative(transcript_path)} ({transcript.get('source')}, {len(transcript['calls'])} calls)")
        run_scripted(ctl, transcript)
        source = f"scripted replay of {planner.repo_relative(transcript_path)}"
    else:
        run_planner(ctl)

    stem = (f"{protocol['id']}__{chip['id']}__{scenario['name']}" + ("" if operator == "planner" else f"__{operator}")
            + ("" if adapter_kind == "virtual" else f"__{adapter_kind}"))
    out = planner.RUNS_DIR / f"{stem}.json"
    report_path = planner.RUNS_DIR / f"{stem}_report.md"
    header = planner.run_header(protocol, chip, chip_path, operator, model_name)
    header["sizing"] = sizing.chip_sizing(chip)
    run = report.build_run(ctl, header, scenario, planner.repo_relative(scenario_path))
    run["report"] = planner.repo_relative(report_path)
    if operator != "scripted":
        tpath = TRANSCRIPTS / f"{stem}.json"
        label = "llm" if operator == "llm" else "planner (deterministic; not model output)"
        schema_io.write_json(tpath, transcript_from(ctl, label, model_name))
        run["transcript"] = planner.repo_relative(tpath)
    errors = schema_io.schema_errors("run", run)
    if errors:
        raise ValueError("run does not match run.schema.json: " + "; ".join(
            f"{schema_io.path_str(e)}: {schema_io.describe(e)}" for e in errors[:5]))
    schema_io.write_json(out, run)
    report.write_report(run, report_path)
    s = run["summary"]
    print(f"wrote {planner.repo_relative(out)} + report  [{operator}, {run['loop']} loop, {adapter.name}]  "
          f"status {s['status']}, {s['alerts']} alert(s), {s['recoveries']} recovery(ies), {s['model_updates']} model update(s)")
    return out


# --- index and --all ------------------------------------------------------------------


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
        scn = run.get("scenario") or {}
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
            "simulated_hardware": run.get("simulated_hardware", False),
            "scenario": scn.get("name"),
            "loop": run.get("loop"),
            "faults": [f.get("type") for f in scn.get("faults", [])],
            "status": run.get("status"),
        })
    entries.sort(key=lambda e: (str(e["protocol_name"]), str(e["scenario"] or ""), str(e["operator"]), e["file"]))
    out = Path(runs_dir) / INDEX_NAME
    schema_io.write_json(out, {"generated_by": "operator/run.py", "runs": entries})
    print(f"wrote {planner.repo_relative(out)}  ({len(entries)} runs)")
    return out


def all_runs(chip: Path | None) -> tuple[list[tuple[Path, Path]], list[Path]]:
    """For --all: (simple (protocol, chip) pairs, scenario files). With --chip: every
    protocol on that chip. Without: every existing planner run again."""
    protocols = sorted((ROOT / "protocols").glob("*.json"))
    if chip is not None:
        return [(p, chip) for p in protocols], []
    by_id = {schema_io.load_json(p).get("id"): p for p in protocols}
    pairs, scenarios, covered = [], [], set()
    for run_path in sorted(planner.RUNS_DIR.glob("*.json")):
        if run_path.name == INDEX_NAME:
            continue
        run = schema_io.load_json(run_path)
        if run.get("operator") != "planner" or run.get("protocol_id") not in by_id or not run.get("chip_spec"):
            continue
        covered.add(run["protocol_id"])
        scn_file = (run.get("scenario") or {}).get("file")
        if scn_file:
            if ROOT / scn_file not in scenarios:
                scenarios.append(ROOT / scn_file)
            continue
        pair = (by_id[run["protocol_id"]], ROOT / run["chip_spec"])
        if pair not in pairs:
            pairs.append(pair)
    pairs += [(p, DEFAULT_CHIP) for pid, p in by_id.items() if pid not in covered]
    return pairs, scenarios


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Turn a protocol + chip spec into a run file for the twin.")
    parser.add_argument("--protocol", type=Path, help="protocol JSON (default with --scenario: the scenario's)")
    parser.add_argument("--chip", type=Path, default=None, help="chip spec JSON (default with --scenario: the scenario's)")
    parser.add_argument("--scenario", type=Path, help="scenario JSON in scenarios/: runs the closed-loop system")
    parser.add_argument("--operator", choices=["planner", "llm", "scripted"], default="planner")
    parser.add_argument("--adapter", choices=["virtual", "dryrun", "syringe_pump"], default="virtual")
    parser.add_argument("--transcript", type=Path, help="transcript to replay with --operator scripted")
    parser.add_argument("--max-tool-calls", type=int, default=None, help="cap on LLM tool calls per run")
    parser.add_argument("--llm", action="store_true", help="same as --operator llm")
    parser.add_argument("--all", action="store_true", help="regenerate every planner run (simple and scenario)")
    parser.add_argument("--reindex", action="store_true", help="only rebuild runs/index.json")
    args = parser.parse_args(argv)
    operator = "llm" if args.llm else args.operator

    try:
        if args.reindex and not (args.protocol or args.all or args.scenario):
            rebuild_index()
            return 0
        if args.all:
            pairs, scenarios = all_runs(args.chip)
            for protocol_path, chip_path in pairs:
                try:
                    generate(protocol_path, chip_path)
                except planner.PlanError as e:
                    print(f"skipped {planner.repo_relative(protocol_path)} on {planner.repo_relative(chip_path)}: "
                          f"{str(e).splitlines()[0]}")
            for scn in scenarios:
                closed_loop(scn, None, None)
        elif args.scenario:
            closed_loop(args.scenario, args.protocol, args.chip, operator, args.adapter, args.transcript, args.max_tool_calls)
        elif args.protocol and args.chip:
            if operator != "planner":
                print(f"--operator {operator} needs a scenario (--scenario); running the simple planner instead.")
            generate(args.protocol, args.chip)
        else:
            parser.error("give --scenario, or --protocol and --chip, or --all, or --reindex")
        rebuild_index()
    except planner.PlanError as e:
        print(f"ERROR: cannot run this protocol on this chip:\n{e}", file=sys.stderr)
        return 1
    except FileNotFoundError as e:
        print(f"ERROR: file not found: {e.filename}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

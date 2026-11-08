"""Protocol compiler CLI: protocol / paper / prompt -> chip-by-chip pipeline -> chip designs + run protocols.

  python -m autochip run protocols/my_protocol.md
  python -m autochip run paper.pdf --constraints assayathon_demo
  python -m autochip run --prompt "Grow iPSC-derived neurons, wrap single cells in capsules, ..." --constraints my_lab.json
  python -m autochip run paper.pdf --backend rules            # offline, no agent
  python -m autochip run paper.pdf --backend replay --replay autochip/examples/out/x/agent
  python -m autochip run --resume out/x --from-stage layout   # redo layout onward from saved stage files
  python -m autochip run --resume out/x                       # continue from the first stage with no output
  python -m autochip constraints --list | --show assayathon_demo

Stages (each writes into the output folder, so any stage can be re-run on its own):
  1 ingest     source.json            text of the paper / protocol / prompt
  2 extract    protocol_ir.json       typed steps (agent; rules fallback)
  3 partition  chip_plan.json         chips, modules per chip, hand-offs, off-chip, missing parts (agent; rules fallback)
                designs/<chip>.json    each chip as a hand-editable design file (chip/designs format)
  4 layout     chips/<chip>.json      chip spec per chip (contract format) + cad/<chip>/ (STL, DXF, SVG/PNG, Fusion script)
               layout_findings.json   each chip's constraint findings (what a resume from runplan/report reads)
               If a chip breaks the constraints, the errors go back to partition (agent, or the rules'
               re-split) up to --rounds times; it stops early when a round gives the same plan or the same
               errors as the one before. A replay re-splits only with the saved answers of the original run.
  5 runplan    runs/<chip>.protocol.json + runs/<chip>.run_sheet.md   what to do on each chip, checked by the flow solver
  6 report     pipeline.json, report.md, report.html, overview.svg/png
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autochip import constraints as C  # noqa: E402
from autochip.llm import BACKENDS, Agent, AgentError  # noqa: E402

STAGES = ["ingest", "extract", "partition", "layout", "runplan", "report"]
LAYOUT_RECORD = "layout_findings.json"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]+$")


class ResumeError(RuntimeError):
    """The output folder lacks a file the requested --from-stage needs."""


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:48] or "run"


def _dump(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2))


def _load(path: Path, from_stage: str | None = None):
    if not path.exists():
        raise ResumeError(f"{'--from-stage ' + from_stage if from_stage else 'resuming'} needs {path} from an earlier "
                          f"run: start from an earlier stage")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ResumeError(f"{path} is not valid JSON ({e}): fix it or re-run the stage that writes it") from e


def _plan_ids(plan: dict | None) -> list[str]:
    return [c["id"] for c in (plan or {}).get("chips", []) if isinstance(c, dict) and c.get("id")]


def _same(a, b) -> bool:
    """Same JSON content (key order ignored)."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _spec_hash(spec: dict) -> str:
    return hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def _read_json(path: Path):
    try:
        return json.loads(path.read_text()) if path.exists() else None
    except json.JSONDecodeError:
        return None


def continue_stage(out: Path) -> str:
    """The first stage whose output is missing in an existing output folder ('report' when all are there)."""
    if not (out / "source.json").exists():
        return "ingest"
    if not (out / "protocol_ir.json").exists():
        return "extract"
    plan = _read_json(out / "chip_plan.json")
    if not plan:
        return "partition"
    ids = _plan_ids(plan)
    rec = _read_json(out / LAYOUT_RECORD)
    if not isinstance(rec, dict) or any(i not in rec or not _spec_path(out, i, rec).exists() for i in ids):
        return "layout"
    if any(not (out / "runs" / f"{i}.protocol.json").exists() for i in ids):
        return "runplan"
    return "report"


def _prune(out: Path, stale: set[str], say) -> None:
    """Remove the files an earlier plan's chips left behind (chips/, designs/, cad/, runs/), so a re-split or an
    edited chip_plan.json doesn't leave old chips in the folder, the report's file list or a later resume.
    Only ids that this folder's own plans or layout record named, and only safe file names."""
    removed = []
    for cid in sorted(i for i in stale if _SAFE_ID.match(i)):
        files = [out / "chips" / f"{cid}.json", out / "designs" / f"{cid}.json"]
        files += [out / "runs" / f"{cid}.{ext}" for ext in ("protocol.json", "run.json", "run_sheet.md", "twin_run.json")]
        hit = False
        for f in files:
            if f.is_file():
                f.unlink()
                hit = True
        cad_dir = out / "cad" / cid
        if cad_dir.is_dir() and (cad_dir / "README.md").exists():      # a folder cad.export_chip wrote
            shutil.rmtree(cad_dir)
            hit = True
        if hit:
            removed.append(cid)
    if removed:
        say(f"      removed the files of chips no longer in the plan: {', '.join(removed)}")


def _save_layout(out: Path, chips: dict) -> None:
    _dump(out / LAYOUT_RECORD, {cid: {"spec_id": r["spec"].get("id", cid), "spec_sha1": _spec_hash(r["spec"]),
                                      "violations": r.get("violations") or []}
                                for cid, r in chips.items()})


def _spec_path(out: Path, cid: str, rec) -> Path:
    """chips/<spec id>.json: layout names the file after the spec id, which can differ from the plan's chip id."""
    r = rec.get(cid) if isinstance(rec, dict) else None
    sid = r.get("spec_id") if isinstance(r, dict) else None
    return out / "chips" / f"{sid or cid}.json"


def _load_layout(out: Path, plan: dict, cons: dict, from_stage: str, say) -> dict:
    """Chip specs of the plan's chips plus their constraint findings, for a resume after layout. The findings
    come from layout_findings.json; for a spec edited by hand since (or an older folder without the record),
    the chip is re-checked against the constraints instead."""
    from autochip import checks
    rec = _read_json(out / LAYOUT_RECORD) or {}
    chips, rechecked = {}, []
    for cid in _plan_ids(plan):
        p = _spec_path(out, cid, rec)
        spec = _load(p, from_stage)
        r = rec.get(cid) if isinstance(rec, dict) else None
        if isinstance(r, dict) and r.get("spec_sha1") == _spec_hash(spec):
            violations = r.get("violations") or []
        else:
            violations = checks.check_chip(spec, cons)
            rechecked.append(cid)
        chips[cid] = {"spec": spec, "violations": violations, "files": {"spec": str(p)}}
    if rechecked:
        say(f"      re-checked {', '.join(rechecked)} against the constraints (spec changed since layout, or no "
            f"{LAYOUT_RECORD}); layout's own notes for them are not available")
    return chips


def _load_runs(out: Path, plan: dict, say) -> dict:
    runs, missing = {}, []
    for cid in _plan_ids(plan):
        p = out / "runs" / f"{cid}.protocol.json"
        if not p.exists():
            missing.append(cid)
            continue
        entry = {"protocol": _load(p, "report"), "checks": []}
        prog = _read_json(out / "runs" / f"{cid}.run.json")
        if isinstance(prog, dict):
            entry["program"] = prog
            entry["checks"] = prog.get("checks") or []
        runs[cid] = entry
    if missing:
        say(f"      no run protocol for {', '.join(missing)}: run from --from-stage runplan to make them")
    return runs


def run_pipeline(input_path: str | None, prompt: str | None, constraints: str | None, out: Path | None,
                 backend: str = "auto", model: str | None = None, replay: Path | None = None,
                 from_stage: str = "ingest", rounds: int = 2, quiet: bool = False) -> dict:
    from autochip import ingest, parts as P
    from autochip.stages import extract, layout, partition, report, runplan

    t0 = time.time()
    if from_stage not in STAGES:
        raise ValueError(f"from_stage must be one of {STAGES}")
    if out is None:
        base = Path(input_path).stem if input_path else _slug(prompt or "prompt")
        out = ROOT / "autochip" / "out" / _slug(base)
    start = STAGES.index(from_stage)
    if start > 0 and not out.is_dir():
        raise ResumeError(f"--from-stage {from_stage} needs an earlier run's output folder, and {out} does not exist")
    cons_path = out / "constraints.json"
    # Load (and validate) everything that can fail before the folder is created or touched.
    if from_stage == "ingest" or not cons_path.exists() or constraints:
        cons = C.load(constraints)
        new_cons = True
    else:
        cons = C.load(cons_path)        # validated too: it may have been edited by hand
        new_cons = False
    agent = Agent(backend=backend, model=model, log_dir=None, replay_dir=replay, quiet=quiet)
    out.mkdir(parents=True, exist_ok=True)
    agent.log_dir = out / "agent"
    agent.log_dir.mkdir(parents=True, exist_ok=True)
    _dump(out / "run_meta.json", {"backend": agent.backend, "model": agent.model if agent.backend == "api" else None,
                                  "replay": str(replay) if replay else None, "constraints": cons.get("id")})
    if new_cons:
        _dump(cons_path, cons)
    # Chips this folder has made before (to clear their files if a new plan drops them).
    made_before = set(_plan_ids(_read_json(out / "chip_plan.json")))
    rec = _read_json(out / LAYOUT_RECORD)
    if isinstance(rec, dict):
        made_before |= set(rec) | {r["spec_id"] for r in rec.values() if isinstance(r, dict) and r.get("spec_id")}

    say = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))
    say(f"autochip -> {out}\n  constraints: {cons['id']}  |  agent: {agent.describe()}"
        + (f"  |  from stage {from_stage}" if start else ""))

    # 1 ingest
    if start <= 0:
        source = ingest.load_source(input_path, prompt)
        _dump(out / "source.json", source)
        say(f"[1/6] ingest: {source['kind']} '{source.get('title') or ''}' ({len(source['text'])} chars)")
    else:
        source = _load(out / "source.json", from_stage)

    # 2 extract
    if start <= 1:
        ir = extract.run(source, cons, agent, out)
        _dump(out / "protocol_ir.json", ir)
        say(f"[2/6] extract: {len(ir['steps'])} steps, {len(ir.get('assumptions', []))} assumptions")
    else:
        ir = _load(out / "protocol_ir.json", from_stage)

    parts = P.load_parts(cons)

    # 3 partition + 4 layout, with a feedback loop when a chip breaks the constraints
    if start <= 3:
        feedback: list[str] | None = None
        plan = _load(out / "chip_plan.json", from_stage) if start == 3 else None
        if plan is not None:
            # chip_plan.json may have been edited by hand: give any new chip its design file, but don't
            # overwrite existing ones (they may have been edited by hand too: the manual route).
            for chip_id, design in partition.design_files(plan, ir).items():
                if not (out / "designs" / f"{chip_id}.json").exists():
                    _dump(out / "designs" / f"{chip_id}.json", design)
        # Re-split rounds: a live agent, the rules (partition's deterministic re-split on feedback), or a replay,
        # which gives back the saved re-split answer for the same feedback (layout is deterministic, so a replay
        # of a live run meets the same errors and finds the answer the agent gave). No saved answer: keep the plan.
        can_resplit = True
        who = ("the partition agent" if agent.live else
               "the saved partition answers (replay)" if agent.backend == "replay" else "the partition rules")
        prev_errors: list[str] | None = None
        for rnd in range(rounds + 1):
            if plan is None or feedback:
                try:
                    new_plan = partition.run(ir, cons, parts, agent, out, feedback=feedback)
                except AgentError as e:
                    if not feedback:
                        raise
                    say(f"      WARNING: the re-split failed ({str(e)[:300]}); keeping the round {rnd} plan and its "
                        f"constraint errors")
                    break
                if feedback and _same(new_plan, plan):
                    say(f"      the re-split gave the same plan again; stopping with the round {rnd} plan")
                    break
                plan = new_plan
                made_before |= set(_plan_ids(plan))
                _dump(out / "chip_plan.json", plan)
                for chip_id, design in partition.design_files(plan, ir).items():
                    _dump(out / "designs" / f"{chip_id}.json", design)
                say(f"[3/6] partition{' (round ' + str(rnd + 1) + ')' if rnd else ''}: {len(plan['chips'])} chips, "
                    f"{sum(len(c['modules']) for c in plan['chips'])} modules, {len(plan['off_chip'])} off-chip groups, "
                    f"{len(plan['missing_parts'])} missing parts")
            chips = layout.run(plan, ir, cons, parts, out)
            _save_layout(out, chips)
            bad = {cid: r["violations"] for cid, r in chips.items() if any(v.get("severity") == "error" for v in r["violations"])}
            say(f"[4/6] layout: {len(chips)} chip specs, {sum(len(r['violations']) for r in chips.values())} constraint findings"
                + (f" ({len(bad)} chips with errors)" if bad else ""))
            if not bad or not can_resplit or rnd == rounds:
                break
            errors = [f"chip {cid}: {v['message']}" for cid, vs in bad.items() for v in vs if v.get("severity") == "error"]
            if prev_errors is not None and sorted(errors) == sorted(prev_errors):
                say(f"      round {rnd + 1} left the same constraint errors as round {rnd}; stopping")
                break
            prev_errors = feedback = errors
            say(f"      sending {len(feedback)} constraint errors back to {who}")
        _prune(out, made_before - set(_plan_ids(plan)) - {r["spec"].get("id") for r in chips.values()}, say)
    else:
        plan = _load(out / "chip_plan.json", from_stage)
        chips = _load_layout(out, plan, cons, from_stage, say)

    # 5 runplan
    if start <= 4:
        runs = runplan.run(plan, ir, chips, cons, out)
        n_err = sum(1 for r in runs.values() for c in r.get("checks", []) if c.get("severity") == "error")
        say(f"[5/6] runplan: {len(runs)} run protocols, {n_err} flow-check errors")
    else:
        runs = _load_runs(out, plan, say)

    # 6 report
    rep = report.run(source, ir, plan, chips, runs, cons, out, agent.describe())
    say(f"[6/6] report: {Path(rep['report']).relative_to(out)}  ({time.time() - t0:.0f}s)")
    return {"out": str(out), "plan": plan, "chips": list(chips), "report": rep}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m autochip", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="compile a protocol / paper / prompt into chips")
    r.add_argument("input", nargs="?", help="protocol or paper: .md .txt .pdf .docx .xlsx .csv .json")
    r.add_argument("--prompt", help="plain-English goal (instead of, or as well as, an input file)")
    r.add_argument("--constraints", help=f"preset id ({', '.join(C.list_presets())}) or path to a constraints JSON")
    r.add_argument("--out", type=Path, help="output folder (default autochip/out/<name>)")
    r.add_argument("--backend", choices=BACKENDS, default=None,
                   help="agent backend (default auto; with --resume, the backend the folder was made with)")
    r.add_argument("--model", help="model for the api and claude-cli backends (api default: AUTOCHIP_MODEL or "
                                   "claude-opus-5-5; claude-cli default: AUTOCHIP_CLI_MODEL or the CLI's own)")
    r.add_argument("--replay", type=Path, help="folder of saved agent answers (for --backend replay; with --resume "
                                               "the default is <resume>/agent)")
    r.add_argument("--resume", type=Path, help="existing output folder to continue; without --from-stage, continues "
                                               "from the first stage whose output is missing")
    r.add_argument("--from-stage", choices=STAGES, default=None, help="first stage to (re)run (default: ingest, or "
                                                                       "with --resume the first stage with no output)")
    r.add_argument("--rounds", type=int, default=2, help="max re-partition rounds when a chip breaks the constraints")
    r.add_argument("--quiet", action="store_true")
    c = sub.add_parser("constraints", help="list or show constraint presets")
    c.add_argument("--list", action="store_true")
    c.add_argument("--show", help="preset id or path")
    a = ap.parse_args(argv)

    if a.cmd == "constraints":
        try:
            if a.show:
                print(C.summary(C.load(a.show)))
            else:
                for p in C.list_presets():
                    print(f"{p:28s} {C.load(p).get('name', '')}")
        except C.ConstraintsError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        return 0
    if not a.input and not a.prompt and not a.resume:
        ap.error("give an input file, --prompt, or --resume")
    if a.rounds < 0:
        ap.error("--rounds must be 0 or more")
    if a.resume and a.out and a.resume.resolve() != a.out.resolve():
        ap.error("--resume already names the output folder; leave out --out (or give the same folder)")
    if a.resume and not a.resume.is_dir():
        ap.error(f"--resume: no output folder {a.resume}")
    if a.input and not Path(a.input).is_file():
        ap.error(f"no such input file: {a.input}" if not Path(a.input).exists() else f"{a.input} is not a file")
    from_stage = a.from_stage
    if from_stage is None:
        from_stage = continue_stage(a.resume) if a.resume and not (a.input or a.prompt) else "ingest"
    if from_stage == "ingest" and not (a.input or a.prompt):
        ap.error("starting at ingest needs the input file or --prompt" + (f" ({a.resume} has no source.json)" if a.resume else ""))
    if a.resume and from_stage != "ingest" and (a.input or a.prompt):
        print(f"note: starting at {from_stage}, so the input / --prompt is not read again (source.json is used)",
              file=sys.stderr)
    if a.backend is None:
        # Resuming: keep the backend the folder was made with, so a rules run isn't relabelled (or re-split)
        # by a live agent just because one happens to be available now.
        prev = _read_json(a.resume / "run_meta.json") if a.resume else None
        a.backend = (prev or {}).get("backend") or "auto"
        if prev and prev.get("backend") == "replay" and a.replay is None and prev.get("replay"):
            a.replay = Path(prev["replay"])
        if prev and not a.quiet:
            print(f"note: resuming with the backend this folder was made with: {a.backend} (override with --backend)",
                  file=sys.stderr)
    replay = a.replay
    if a.backend == "replay" and replay is None and a.resume:
        replay = a.resume / "agent"
    out = a.resume or a.out
    try:
        run_pipeline(a.input, a.prompt, a.constraints, out, backend=a.backend, model=a.model, replay=replay,
                     from_stage=from_stage, rounds=a.rounds, quiet=a.quiet)
    except (AgentError, C.ConstraintsError, ResumeError, ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

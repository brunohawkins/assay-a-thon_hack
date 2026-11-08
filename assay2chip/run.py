"""assay2chip orchestrator: paper or protocol in, printable chips out.

    python assay2chip/run.py --paper path/to/protocol.pdf --id my_assay
    python assay2chip/run.py --id my_assay --from s4            # resume at a stage
    python assay2chip/run.py --id my_assay --from s6 --to s9    # run a slice
    python assay2chip/run.py --list                             # the stages, and which exist
    python assay2chip/run.py --id my_assay --dry-run            # the plan, touching nothing

Thirteen stages in assay2chip/stages/, run in NUMERIC order (s10 after s9, which a string
sort gets wrong). Each one reads the previous stage's file and writes its own; nothing
mutates an earlier output.

Three things this orchestrator does that are easy to get wrong:

  NOT YET IMPLEMENTED   A stage whose module does not exist yet is reported and skipped,
                        so the pipeline is usable while it is being written.
  BOUNDED RE-RUNS       S10 (CFD) can send a chip back to S6/S7 when an automatic fix
                        changes geometry. A stage asks by returning
                        {"rerun_from": "s6", "reason": "..."}; the loop is capped at
                        MAX_RERUN_LOOPS and every pass is logged in the manifest with its
                        reason. It never loops silently.
  WRITES OUTSIDE work/  S5-S11 write into chip/, protocols/ and cad/, where real
                        hand-designed files live. A stage declares those paths (WRITES or
                        writes(ctx)); the orchestrator records their hashes and REFUSES to
                        overwrite a file this pipeline did not produce. --force overrides,
                        loudly.

The run manifest (assay2chip/work/<id>/run_manifest.json) records every stage, its input
and output hashes, its timing, the files it wrote outside the work directory, whether the
language model was real or stubbed, and every re-run with its reason.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts, llm  # noqa: E402
from assay2chip.lib import io as stage_io  # noqa: E402

STAGE_DIR = ROOT / "assay2chip" / "stages"
WORK = stage_io.WORK
LEDGER = "produced.json"

# How many times a stage may send the pipeline backwards in one run. S10's fix-and-re-run
# loop is the reason this exists; the cap is what stops it spinning.
MAX_RERUN_LOOPS = 3

# The spine, from assay2chip/README.md. `reads` and `writes` are stage-file kinds
# (assay2chip/lib/io.FILES); `outside` is what the stage writes beyond the work directory,
# for --list and for the declaration check.
SPINE: list[tuple[int, str, str | None, str | None, str]] = [
    (0, "ingest", None, "raw", ""),
    (1, "extract", "raw", "assay", ""),
    (2, "normalise", "assay", "unitops", ""),
    (3, "triage", "unitops", "unitops", ""),
    (4, "partition", "unitops", "partition", ""),
    (5, "group", "partition", None, "chip/designs/<chip_id>.json"),
    (6, "build", "partition", None, "chip/<chip_id>.json (unrouted)"),
    (7, "route", "partition", None, "chip/<chip_id>.json (routed)"),
    (8, "protocol", "partition", None, "protocols/<chip_id>.json"),
    (9, "verify", "partition", None, "assay2chip/work/<id>/verify.json, figures"),
    (10, "cfd", "partition", None, "assay2chip/work/<id>/cfd/"),
    (11, "fabricate", "partition", None, "cad/exports/<chip_id>/, print/"),
    (12, "report", "partition", "report", "assay2chip/work/<id>/report.md"),
]


class RunError(Exception):
    """The run cannot safely continue."""


@dataclass
class Stage:
    number: int
    name: str
    reads: str | None
    writes: str | None
    outside: str
    module_name: str | None = None
    path: Path | None = None

    @property
    def token(self) -> str:
        return f"s{self.number}"

    @property
    def label(self) -> str:
        return f"{self.token}_{self.name}"

    @property
    def implemented(self) -> bool:
        return self.path is not None


@dataclass
class StageContext:
    """What a stage is handed. Take the fields you need as keyword arguments."""

    id: str
    paper: str | None
    work_dir: Path
    stage: Stage
    llm_mode: str
    force: bool
    offline: bool
    verbose: bool
    loop: int = 0
    rerun_reason: str | None = None
    log_lines: list[str] = field(default_factory=list)

    @property
    def assay_id(self) -> str:
        return self.id

    @property
    def root(self) -> Path:
        return ROOT

    @property
    def work_root(self) -> Path:
        return WORK

    def read(self, kind: str | None = None) -> Any:
        """Read a stage file (default: the one this stage reads), schema-validated."""
        return stage_io.read_stage(self.id, kind or self.stage.reads or "")

    def write(self, kind: str, data: Any) -> Path:
        """Write a stage file, schema-validated, atomically."""
        return stage_io.write_stage(self.id, kind, data)

    def path_for(self, kind: str) -> Path:
        return stage_io.stage_path(self.id, kind)

    def log(self, message: str) -> None:
        self.log_lines.append(message)
        if self.verbose:
            print(f"    {message}")


def slug(text: str) -> str:
    """Same slug as s0_ingest, so --paper without --id lands in the directory S0 uses."""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return text or "untitled"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- the registry ------------------------------------------------------------

def discover() -> list[Stage]:
    """The spine, in numeric order, with whichever stage modules exist on disk.

    A module is matched by its NUMBER, so s6_build.py and s6_build_chip.py both count as
    S6 and the spine's own name is only the expected one. Numeric sort throughout.
    """
    found: dict[int, Path] = {}
    for path in sorted(STAGE_DIR.glob("s*.py")) if STAGE_DIR.exists() else []:
        m = re.match(r"^s(\d+)_[a-z0-9_]+$", path.stem)
        if not m or path.stem.startswith("_"):
            continue
        number = int(m.group(1))
        if number in found:
            raise RunError(
                f"two modules claim stage s{number}: {found[number].name} and {path.name}. "
                f"One stage, one module."
            )
        found[number] = path

    stages = []
    for number, name, reads, writes, outside in SPINE:
        stage = Stage(number, name, reads, writes, outside)
        path = found.pop(number, None)
        if path is not None:
            stage.path, stage.module_name = path, path.stem
            stage.name = path.stem.split("_", 1)[1]
        stages.append(stage)
    for number, path in sorted(found.items()):
        extra = Stage(number, path.stem.split("_", 1)[1], None, None, "not in the spine")
        extra.path, extra.module_name = path, path.stem
        stages.append(extra)
    stages.sort(key=lambda s: s.number)          # NUMERIC: s10 after s9
    return stages


def resolve(stages: list[Stage], token: str | None) -> int | None:
    """Index of the stage a --from/--to argument names: 's6', '6', 's6_build' or 'build'."""
    if token is None:
        return None
    want = token.strip().lower()
    want = want[:-3] if want.endswith(".py") else want
    for i, stage in enumerate(stages):
        if want in (stage.token, str(stage.number), stage.label, stage.name,
                    stage.module_name or ""):
            return i
    known = ", ".join(s.label for s in stages)
    raise RunError(f"no stage called {token!r}. The stages are: {known}")


# --- the produced-file ledger ------------------------------------------------

def ledger_path(assay_id: str) -> Path:
    return WORK / assay_id / LEDGER


def ledger_load(assay_id: str) -> dict:
    path = ledger_path(assay_id)
    if not path.exists():
        return {"paths": {}}
    data = stage_io.load_json(path)
    data.setdefault("paths", {})
    return data


def produced_anywhere() -> dict[str, dict]:
    """Every file any assay2chip run has produced, from every work directory's ledger.

    A path in here is ours to regenerate. A path NOT in here that exists on disk is
    somebody's real work, and a stage is not allowed to overwrite it.
    """
    out: dict[str, dict] = {}
    if not WORK.exists():
        return out
    for path in sorted(WORK.glob(f"*/{LEDGER}")):
        try:
            data = stage_io.load_json(path)
        except ValueError:
            continue
        for rel, meta in (data.get("paths") or {}).items():
            out[rel] = meta
    return out


def ledger_record(assay_id: str, entries: dict[str, dict]) -> None:
    data = ledger_load(assay_id)
    data["paths"].update(entries)
    data["updated_at"] = now()
    stage_io.write_json(ledger_path(assay_id), data)


def rel(path: Path) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def declared_writes(module: Any, ctx: StageContext) -> list[str]:
    """The paths a stage says it writes outside its own stage file.

    From `WRITES` (a list of templates, with {chip_id} and {id} expanded) or from
    `writes(ctx)`. A stage that declares nothing is taken at its word.
    """
    out: list[str] = []
    writes_fn = getattr(module, "writes", None)
    if callable(writes_fn):
        try:
            out += [str(p) for p in (call_with_context(writes_fn, ctx) or [])]
        except Exception as e:                      # a declaration must not kill the run
            ctx.log(f"could not ask {module.__name__}.writes(): {e}")
    templates = getattr(module, "WRITES", None) or []
    chip_ids = chip_ids_for(ctx.id)
    for template in templates:
        text = str(template)
        if "{chip_id}" in text:
            out += [text.format(chip_id=c, id=ctx.id) for c in chip_ids]
        else:
            out.append(text.format(id=ctx.id))
    seen, unique = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    return unique


def chip_ids_for(assay_id: str) -> list[str]:
    """The chip ids in partition.json, when it exists, for expanding {chip_id}."""
    path = stage_io.stage_path(assay_id, "partition")
    if not path.exists():
        return []
    try:
        data = stage_io.load_json(path)
    except ValueError:
        return []
    return [c.get("chip_id", "") for c in data.get("chips", []) if c.get("chip_id")]


def guard_writes(stage: Stage, paths: list[str], force: bool) -> list[str]:
    """Refuse to clobber a file this pipeline did not produce. Returns the paths it will write.

    This is the check that stops a generated chip silently replacing a hand-designed one:
    chip/module1_v1.json is somebody's real design, and it has a real id.
    """
    ours = produced_anywhere()
    clashes = []
    for p in paths:
        target = ROOT / p
        if target.exists() and p not in ours:
            clashes.append(p)
    if clashes and not force:
        listing = "\n".join(f"  - {p}" for p in clashes)
        raise RunError(
            f"{stage.label} would overwrite {len(clashes)} file(s) this pipeline did not "
            f"produce:\n{listing}\n"
            f"These may be hand-designed chips, protocols or exports. Pick different chip "
            f"ids in partition.json, or re-run with --force to overwrite them anyway."
        )
    if clashes:
        print(
            f"  !! --force: {stage.label} is overwriting {len(clashes)} file(s) it did not "
            f"produce: {', '.join(clashes)}",
            file=sys.stderr,
        )
    return paths


# --- calling a stage ---------------------------------------------------------

def call_with_context(fn: Callable, ctx: StageContext) -> Any:
    """Call a stage function with only the keyword arguments it accepts.

    Stages are written by different people, so the orchestrator adapts to the signature
    rather than demanding one. A function taking a single positional parameter gets the
    context object.
    """
    offer: dict[str, Any] = {
        "ctx": ctx,
        "context": ctx,
        "assay_id": ctx.id,
        "id": ctx.id,
        "paper": ctx.paper or "",
        "source": ctx.paper or "",
        "work_root": WORK,
        "work_dir": ctx.work_dir,
        "root": ROOT,
        "llm_mode": ctx.llm_mode,
        "llm": ctx.llm_mode,
        "offline": ctx.offline,
        "force": ctx.force,
        "verbose": ctx.verbose,
        "loop": ctx.loop,
        "rerun_reason": ctx.rerun_reason,
        "log": ctx.log,
    }
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn()
    kwargs: dict[str, Any] = {}
    positional: list[Any] = []
    for name, p in sig.parameters.items():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if name in offer:
            if p.kind is p.POSITIONAL_ONLY:
                positional.append(offer[name])
            else:
                kwargs[name] = offer[name]
        elif p.default is p.empty and p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            # An unnamed required parameter: hand it the context and let the stage decide.
            positional.append(ctx)
    return fn(*positional, **kwargs)


def load_stage_module(stage: Stage) -> Any:
    return importlib.import_module(f"assay2chip.stages.{stage.module_name}")


def entry_point(module: Any) -> Callable | None:
    for name in ("run", "stage", "main"):
        fn = getattr(module, name, None)
        if callable(fn):
            return fn
    return None


def read_rerun(result: Any) -> tuple[str | None, str]:
    """A stage's re-run request, if it made one: ('s6', 'reason')."""
    if result is None:
        return None, ""
    if isinstance(result, dict):
        target = result.get("rerun_from") or result.get("rerun")
        reason = result.get("reason") or result.get("rerun_reason") or ""
    else:
        target = getattr(result, "rerun_from", None)
        reason = getattr(result, "reason", "") or ""
    return (str(target) if target else None), str(reason)


def extra_writes_from(result: Any) -> list[str]:
    if isinstance(result, dict):
        value = result.get("writes") or result.get("wrote") or []
    else:
        value = getattr(result, "writes", None) or []
    return [rel(Path(p)) for p in value] if isinstance(value, (list, tuple)) else []


# --- the run -----------------------------------------------------------------

def run_stage(stage: Stage, ctx: StageContext) -> dict:
    """Run one stage and return its manifest entry."""
    entry: dict[str, Any] = {
        "stage": stage.token,
        "name": stage.name,
        "module": f"assay2chip/stages/{stage.module_name}.py" if stage.implemented else None,
        "loop": ctx.loop,
        "started_at": now(),
        "status": "ok",
        "reads": stage.reads,
        "writes": stage.writes,
    }
    if stage.reads:
        path = stage_io.stage_path(ctx.id, stage.reads)
        entry["input"] = {"path": rel(path), "sha256": stage_io.sha256_file(path)}

    if not stage.implemented:
        entry["status"] = "not_implemented"
        entry["message"] = (
            f"NOT YET IMPLEMENTED: assay2chip/stages/{stage.token}_{stage.name}.py does not "
            f"exist yet"
        )
        entry["duration_s"] = 0.0
        print(f"  {stage.label:16} NOT YET IMPLEMENTED (skipped)")
        return entry

    module = load_stage_module(stage)
    fn = entry_point(module)
    if fn is None:
        entry["status"] = "error"
        entry["message"] = (
            f"{stage.module_name} has no run() (or stage(), or main()); see "
            f"assay2chip/stages/__init__.py for the entry point a stage must provide"
        )
        print(f"  {stage.label:16} ERROR  {entry['message']}")
        return entry

    declared = declared_writes(module, ctx)
    if declared:
        guard_writes(stage, declared, ctx.force)
        entry["declared_writes"] = declared

    llm.reset()
    started = time.perf_counter()
    try:
        result = call_with_context(fn, ctx)
    except stage_io.StageError as e:
        entry.update(status="error", message=str(e),
                     duration_s=round(time.perf_counter() - started, 3))
        print(f"  {stage.label:16} ERROR\n{e}")
        return entry
    except Exception as e:                      # a stage's own failure, reported not hidden
        entry.update(status="error", message=f"{type(e).__name__}: {e}",
                     duration_s=round(time.perf_counter() - started, 3))
        print(f"  {stage.label:16} ERROR  {type(e).__name__}: {e}")
        return entry
    entry["duration_s"] = round(time.perf_counter() - started, 3)
    entry["llm"] = llm.provenance()

    target, reason = read_rerun(result)
    if target:
        entry["rerun_request"] = {"rerun_from": target, "reason": reason}

    if stage.writes:
        path = stage_io.stage_path(ctx.id, stage.writes)
        entry["output"] = {"path": rel(path), "sha256": stage_io.sha256_file(path)}
        if not path.exists():
            entry["status"] = "wrote_nothing"
            entry["message"] = f"stage finished but {rel(path)} was not written"

    produced = {}
    for p in dict.fromkeys(declared + extra_writes_from(result)):
        sha = stage_io.sha256_file(ROOT / p)
        produced[p] = {"sha256": sha, "stage": stage.token, "at": now(),
                       "exists": sha is not None}
    if produced:
        entry["extra_writes"] = [{"path": p, **meta} for p, meta in produced.items()]
        ledger_record(ctx.id, produced)

    if ctx.log_lines:
        entry["log"] = list(ctx.log_lines)

    mode = (entry.get("llm") or {}).get("mode", "none")
    suffix = f"  [llm: {mode}]" if mode != "none" else ""
    extra = f"  +{len(produced)} file(s)" if produced else ""
    print(f"  {stage.label:16} {entry['status']:16} {entry['duration_s']:>7.2f}s{extra}{suffix}")
    return entry


def execute(
    assay_id: str,
    *,
    paper: str | None = None,
    start: int = 0,
    stop: int | None = None,
    llm_mode: str = llm.STUB,
    force: bool = False,
    verbose: bool = False,
    keep_going: bool = False,
    stages: list[Stage] | None = None,
) -> dict:
    """Run the pipeline from `start` to `stop` (inclusive indices) and write the manifest."""
    stages = stages or discover()
    stop = len(stages) - 1 if stop is None else stop
    plan = stages[start : stop + 1]
    llm.configure(llm_mode)

    manifest: dict[str, Any] = {
        "id": assay_id,
        "created_at": now(),
        "command": " ".join(["python", "assay2chip/run.py", *sys.argv[1:]]),
        "args": {"paper": paper, "from": plan[0].token if plan else None,
                 "to": plan[-1].token if plan else None, "force": force},
        "llm": {"mode": llm.current_mode(),
                "model": llm.model_name() if llm.current_mode() == llm.REAL else None},
        "contracts": contracts.fingerprint(),
        "missing_contract_keys": contracts.missing_keys(),
        "plan": [s.label for s in plan],
        "max_rerun_loops": MAX_RERUN_LOOPS,
        "reruns": [],
        "rerun_cap_reached": False,
        "stages": [],
    }
    stage_io.work_dir(assay_id, create=True)

    index, reruns = 0, 0
    status = "ok"
    while index < len(plan):
        stage = plan[index]
        ctx = StageContext(
            id=assay_id, paper=paper, work_dir=stage_io.work_dir(assay_id, create=True),
            stage=stage, llm_mode=llm.current_mode(), force=force,
            offline=llm.current_mode() == llm.STUB, verbose=verbose, loop=reruns,
        )
        try:
            entry = run_stage(stage, ctx)
        except RunError as e:
            entry = {"stage": stage.token, "name": stage.name, "status": "refused",
                     "message": str(e), "started_at": now(), "duration_s": 0.0}
            manifest["stages"].append(entry)
            manifest["status"] = "refused"
            print(f"\n{e}\n", file=sys.stderr)
            break
        manifest["stages"].append(entry)

        if entry["status"] == "error":
            status = "error"
            if not keep_going:
                manifest["status"] = status
                print("\nstopped at the first failing stage (--keep-going runs on anyway)",
                      file=sys.stderr)
                break

        request = entry.get("rerun_request") or {}
        target, reason = request.get("rerun_from"), request.get("reason", "")
        if target:
            record = {"requested_by": stage.token, "rerun_from": target,
                      "reason": reason or "(no reason given)", "loop": reruns + 1, "at": now()}
            if reruns >= MAX_RERUN_LOOPS:
                record["honoured"] = False
                record["note"] = (
                    f"re-run cap of {MAX_RERUN_LOOPS} already reached; carrying on without "
                    f"going back. The finding this fix was for is still open."
                )
                manifest["rerun_cap_reached"] = True
                manifest["reruns"].append(record)
                print(f"  !! {stage.token} asked to re-run from {target} but the cap of "
                      f"{MAX_RERUN_LOOPS} is reached: {record['reason']}", file=sys.stderr)
            else:
                back = resolve(plan, target)
                if back is None or back > index:
                    record["honoured"] = False
                    record["note"] = f"{target!r} is not an earlier stage in this plan"
                    manifest["reruns"].append(record)
                    print(f"  !! {stage.token} asked to re-run from {target}, which is not "
                          f"earlier in this plan; ignored", file=sys.stderr)
                else:
                    reruns += 1
                    record["honoured"] = True
                    manifest["reruns"].append(record)
                    print(f"  ++ loop {reruns}/{MAX_RERUN_LOOPS}: {stage.token} sends the run "
                          f"back to {plan[back].label} - {record['reason']}")
                    index = back
                    continue
        index += 1

    manifest.setdefault("status", status)
    manifest["finished_at"] = now()
    counts: dict[str, int] = {}
    for entry in manifest["stages"]:
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
    manifest["summary"] = counts
    stage_io.write_stage(assay_id, "manifest", manifest)
    print(f"\nmanifest: {rel(stage_io.stage_path(assay_id, 'manifest'))}")
    print("  " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
    return manifest



def print_list(stages: list[Stage]) -> None:
    missing = contracts.missing_keys()
    env = contracts.envelope("demo")
    print("assay2chip stages (numeric order)\n")
    print(f"  {'stage':16} {'module':28} {'reads':10} {'writes':10} also writes")
    for stage in stages:
        module = f"{stage.module_name}.py" if stage.implemented else "- NOT YET IMPLEMENTED"
        print(f"  {stage.label:16} {module:28} {str(stage.reads or '-'):10} "
              f"{str(stage.writes or '-'):10} {stage.outside}")
    done = sum(1 for s in stages if s.implemented)
    print(f"\n  {done}/{len(stages)} stages implemented; re-run cap {MAX_RERUN_LOOPS}")
    print(f"  contracts: demo envelope {env.x_mm:g} x {env.y_mm:g} mm, depth "
          f"{contracts.max_depth_mm():g} mm, {contracts.max_ports_per_chip('demo')} ports, "
          f"{len(contracts.library())} blocks")
    print(f"  llm mode: {llm.current_mode()} (stub is offline and deterministic)")
    if missing:
        print(f"\n  !! MISSING CONTRACT KEYS ({len(missing)}): {', '.join(missing)}",
              file=sys.stderr)


def print_plan(stages: list[Stage], start: int, stop: int, assay_id: str) -> None:
    print(f"dry run for id '{assay_id}': {stages[start].label} -> {stages[stop].label}\n")
    for stage in stages[start : stop + 1]:
        state = "ready" if stage.implemented else "NOT YET IMPLEMENTED"
        reads = stage_io.stage_path(assay_id, stage.reads) if stage.reads else None
        note = ""
        if reads is not None:
            note = f"  reads {rel(reads)}{'' if reads.exists() else ' (missing)'}"
        print(f"  {stage.label:16} {state:20}{note}")
    print("\nnothing was written (--dry-run)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="assay2chip/run.py",
        description="Run the assay-to-chip pipeline (paper or protocol in, chips out).",
    )
    parser.add_argument("--paper", help="source document: a .pdf/.md/.txt path, a URL or a DOI")
    parser.add_argument("--id", help="run id; names assay2chip/work/<id>/")
    parser.add_argument("--from", dest="start", help="first stage to run, e.g. s4")
    parser.add_argument("--to", dest="stop", help="last stage to run, e.g. s9")
    parser.add_argument("--list", action="store_true", help="list the stages and exit")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan and write nothing")
    parser.add_argument("--llm", choices=list(llm.MODES), default=llm.STUB,
                        help="real calls a model; stub (the default) is offline and deterministic")
    parser.add_argument("--force", action="store_true",
                        help="allow a stage to overwrite files this pipeline did not produce")
    parser.add_argument("--keep-going", action="store_true",
                        help="carry on after a stage fails instead of stopping")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    try:
        stages = discover()
    except RunError as e:
        print(f"assay2chip: {e}", file=sys.stderr)
        return 2

    if args.list:
        print_list(stages)
        return 0

    assay_id = args.id or (slug(Path(args.paper).stem) if args.paper else "")
    if not assay_id:
        parser.error("give --id (or --paper, whose file name becomes the id)")
    if not re.match(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$", assay_id):
        parser.error(
            f"--id {assay_id!r} must be snake_case: lowercase letters, digits and "
            f"underscores, starting with a letter (contracts/README.md)"
        )

    try:
        start = resolve(stages, args.start) or 0
        stop = resolve(stages, args.stop)
        stop = len(stages) - 1 if stop is None else stop
    except RunError as e:
        print(f"assay2chip: {e}", file=sys.stderr)
        return 2
    if stop < start:
        print(f"assay2chip: --to {stages[stop].label} comes before --from "
              f"{stages[start].label}", file=sys.stderr)
        return 2

    llm.configure(args.llm)
    missing = contracts.missing_keys()
    if missing:
        print(f"!! MISSING CONTRACT KEYS ({len(missing)}): {', '.join(missing)}\n"
              f"   Add them to the contract; no stage may invent a value for them.\n",
              file=sys.stderr)

    if args.dry_run:
        print_plan(stages, start, stop, assay_id)
        return 0

    if start == 0 and not args.paper and not stage_io.has_stage(assay_id, "raw"):
        parser.error("s0_ingest needs --paper (or start later with --from)")

    print(f"assay2chip: id '{assay_id}', {stages[start].label} -> {stages[stop].label}, "
          f"llm {llm.current_mode()}")
    manifest = execute(
        assay_id, paper=args.paper, start=start, stop=stop, llm_mode=args.llm,
        force=args.force, verbose=args.verbose, keep_going=args.keep_going, stages=stages,
    )
    return 0 if manifest["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

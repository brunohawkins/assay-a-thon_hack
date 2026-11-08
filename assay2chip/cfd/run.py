"""S10 CFD orchestrator: does the whole chip set actually flow?

    python assay2chip/cfd/run.py --pipeline protocols/pipeline_mvp.json
    python assay2chip/cfd/run.py --chip chip/module3_v1_translated.json \
                                 --protocol protocols/module3_v1.json
    python assay2chip/cfd/run.py --all-chips
    python assay2chip/cfd/run.py --list                 # which cfd modules exist

Runs mesh -> scenario -> solve -> transport -> particles -> couple -> analyse -> fix
(bounded) -> figures -> report, and writes everything into `assay2chip/work/<run>/cfd/`:

    <chip_id>.mesh.json     the depth-averaged mesh
    <chip_id>.field.json    one entry per simulated scenario: p, u per cell, Q per edge
    pipeline.field.json     the chips joined through tubing models
    findings.json  fixes.json  validation.json
    cfd.json                the machine-readable run summary (report.py's input)
    report.md               the short human report
    figures/*.svg           four maps per chip per step, plus the chain overview
    patched/<chip_id>.json  standalone only: a COPY with the auto-applied fixes

ORDER MATTERS, and it is the README's order. Couple runs before analyse, because a
handoff finding needs both chips. Validate runs before analyse, because a failed
benchmark marks every finding `untrusted` rather than deleting it. Fix runs after
analyse and owns its own bounded loop; figures run last, so every map can mark the
findings that landed on it.

TEN AGENTS BUILD `assay2chip/cfd/` IN PARALLEL, so at any moment some of it may not
exist. Every sibling is loaded by path and called through a guard: absent is reported as
NOT YET AVAILABLE, present-but-unrecognised as `no entry point`, and a raise as
`errored`. None of the three stops the run and none is replaced by a guessed number -
the report says exactly what did not run. The preferred entry point for each module is
its documented public API; the alternatives behind it are only there so a sibling that
renames a function degrades to a message instead of a traceback.

A hand-designed chip in `chip/` is NEVER modified. In pipeline mode a geometry fix
becomes a `rerun_from` request that `assay2chip/stages/s10_cfd.py` returns to the
orchestrator (cap 3 loops, logged). Standalone, it is a proposal plus a patched copy.

SI internally; mm, uL/min, Pa and dyn/cm^2 at the JSON boundary with the unit in the key
name. Every contract value comes from `assay2chip.lib.contracts`, where a missing key
raises instead of defaulting. Standard library plus whatever the siblings need.
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import math
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CFD_DIR = ROOT / "assay2chip" / "cfd"
WORK = ROOT / "assay2chip" / "work"
CHIP_DIR = ROOT / "chip"
PROTOCOL_DIR = ROOT / "protocols"

figures = None          # loaded by path below, like every other sibling
report = None

try:
    from assay2chip.lib import contracts
except Exception as exc:                                # pragma: no cover
    contracts = None
    _CONTRACTS_ERROR = f"{type(exc).__name__}: {exc}"
else:
    _CONTRACTS_ERROR = None

NOT_YET = "NOT YET AVAILABLE"
MAX_FIX_LOOPS = 3           # the cap assay2chip/run.py puts on rerun_from
#: Wall-clock budget for ONE call into ONE sibling. A sibling that overruns it is
#: reported as `timed out` and skipped, exactly like one that raises. Without this a
#: single slow module hangs the whole-pipeline run with no output and no clue which one
#: it was - which is precisely what happened the first time this was run end to end.
#: Raise it with --timeout, or set 0 to wait forever.
CALL_TIMEOUT_S = 420
MAX_STEPS = 4               # hydraulically distinct steps simulated per chip
MAX_WORST_CASES = 2         # scenario.py's bracketing cases, on top of the steps
N_PARTICLES = 24            # tracked per particle class; particles.py defaults to 60


# --- loading siblings that may not exist yet ------------------------------------------

def shortest(path):
    """A path relative to the repo when it is inside it, else the path itself.

    `Path.relative_to` RAISES for anything outside the repo, which made a tidy status
    message the thing that killed the run - including under a temporary directory in the
    tests. Reporting never raises.
    """
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except (ValueError, OSError):
        return str(path)


def load_sibling(name):
    """Import `assay2chip/cfd/<name>.py` by path. -> (module, status, detail).

    By path rather than as a package import so that a sibling which fails to import is a
    reported status instead of a traceback that kills the whole run.
    """
    path = CFD_DIR / f"{name}.py"
    if not path.exists():
        return None, NOT_YET, f"{shortest(path)} does not exist yet"
    try:
        spec = importlib.util.spec_from_file_location(f"assay2chip.cfd.{name}", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules.setdefault(spec.name, module)
        spec.loader.exec_module(module)
    except Exception as exc:
        return None, "import failed", f"{type(exc).__name__}: {exc}"
    return module, "ok", shortest(path)


# Entry points in preference order: the documented public API first, then the names a
# sibling might plausibly have used instead. A module exposing none of them is reported,
# never second-guessed.
SIBLING_SPEC = {
    "model": ["Mesh", "Field", "Scenario"],
    "mesh": ["build_mesh", "mesh_chip", "make_mesh", "from_chip", "run"],
    "scenario": ["build_protocol", "scenarios_for", "build_scenarios", "run"],
    "solve": ["solve_steps", "solve_step", "solve", "run"],
    "transport": ["analyse", "transport", "residence_times", "run"],
    "particles": ["analyse_chip", "particles", "analyse", "run"],
    "couple": ["couple", "couple_pipeline", "run"],
    "analyse": ["analyse", "analyse_chip", "run"],
    "fix": ["propose", "propose_fixes", "run"],
    "validate": ["run", "validate", "benchmarks"],
}


class SiblingTimeout(Exception):
    """A sibling ran past its wall-clock budget. Reported, like any other failure."""


class _Budget:
    """SIGALRM around one sibling call, on the main thread, or a no-op elsewhere.

    Interrupts Python-level loops, which is where a slow module actually spends its
    time. A long call inside one numpy primitive cannot be interrupted, so this is a
    guard rather than a guarantee - but a guard that turns "no output for an hour" into
    a named, reported overrun is worth having.
    """

    def __init__(self, seconds, label):
        self.seconds = float(seconds or 0)
        self.label = label
        self.previous = None

    def __enter__(self):
        if self.seconds <= 0:
            return self
        try:
            import signal
            import threading
            if threading.current_thread() is not threading.main_thread():
                return self
            self.previous = signal.signal(signal.SIGALRM, self._fire)
            signal.setitimer(signal.ITIMER_REAL, self.seconds)
        except (ImportError, ValueError, OSError):
            self.previous = None
        return self

    def _fire(self, _signum, _frame):
        raise SiblingTimeout(
            f"{self.label} ran past its {self.seconds:g} s budget and was interrupted "
            f"(raise it with --timeout, or 0 to wait)")

    def __exit__(self, *_exc):
        if self.previous is None:
            return False
        try:
            import signal
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self.previous)
        except (ImportError, ValueError, OSError):
            pass
        return False


class Caller:
    """One sibling module: whether it is here, and a guarded way to call into it."""

    def __init__(self, name, names, log, timeout=CALL_TIMEOUT_S):
        self.name = name
        self.names = names
        self.log = log
        self.timeout = timeout
        self.seconds = 0.0
        self.module, self.status, self.detail = load_sibling(name)
        self.entry = None
        self.calls = 0
        self.errors = []
        self.notes = []
        if self.module is not None:
            for candidate in names:
                if callable(getattr(self.module, candidate, None)) or hasattr(
                        self.module, candidate):
                    self.entry = candidate
                    break
            if self.entry is None:
                self.status = "no entry point"
                self.detail = (f"{name}.py exists but exposes none of "
                               f"{', '.join(names)} - the integrator calls the "
                               f"documented API; rename or tell it which to use")
            else:
                self.detail = f"{self.detail} · {self.entry}"

    @property
    def available(self):
        return self.module is not None and self.entry is not None

    def func(self, *names):
        """The first of `names` this module exposes as a callable, or None."""
        for name in names or self.names:
            fn = getattr(self.module, name, None) if self.module else None
            if callable(fn):
                return fn
        return None

    def call(self, fn, *args, quiet=False, **kwargs):
        """-> (result, ok). Keyword arguments the signature does not name are dropped.

        `quiet` is for a speculative first attempt (a stricter signature, a batched
        entry point): its failure is not held against the module in the report.
        """
        if fn is None:
            return None, False
        try:
            sig = inspect.signature(fn)
            if not any(p.kind == p.VAR_KEYWORD for p in sig.parameters.values()):
                kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
        except (TypeError, ValueError):
            pass
        label = f"{self.name}.{getattr(fn, '__name__', '?')}()"
        started = time.monotonic()
        try:
            with _Budget(self.timeout, label):
                result = fn(*args, **kwargs)
        except Exception as exc:
            self.seconds += time.monotonic() - started
            verb = "timed out" if isinstance(exc, SiblingTimeout) else \
                f"raised {type(exc).__name__}"
            message = f"{label} {verb}: {exc}"
            if quiet:
                self.notes.append(message)
            elif message not in self.errors:
                self.errors.append(message)
                self.log(f"  ! {message}")
                if not isinstance(exc, SiblingTimeout):
                    for line in traceback.format_exc(limit=3).strip().splitlines()[-3:]:
                        self.log(f"    {line.strip()}")
            return None, False
        self.seconds += time.monotonic() - started
        self.calls += 1
        return result, True

    def report_row(self):
        status, detail = self.status, self.detail
        if self.errors:
            status = "timed out" if all("timed out" in e for e in self.errors) \
                else "errored"
            detail = f"{detail} · {self.errors[0]}"
        elif self.available and self.calls == 0:
            detail = f"{detail} · never needed in this run"
        elif self.notes and self.available:
            detail = f"{detail} · recovered from: {self.notes[0]}"
        return {"status": status, "detail": detail, "entry_point": self.entry,
                "calls": self.calls, "seconds": round(self.seconds, 1),
                "errors": self.errors[:3]}


# --- small helpers ---------------------------------------------------------------------

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _f(value, default=None):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if out != out else out


def attr(obj, *names, default=None):
    """A field off a dataclass, an object or a dict - whichever the sibling returned."""
    for name in names:
        if isinstance(obj, dict):
            if name in obj:
                return obj[name]
        else:
            value = getattr(obj, name, None)
            if value is not None:
                return value
    return default


def jsonable(obj, depth=0):
    """Anything a sibling hands back, as JSON. Dataclasses included."""
    if depth > 14:
        return str(obj)
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else str(obj)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [jsonable(v, depth + 1) for v in obj]
    for name in ("to_json", "as_json", "to_dict", "as_dict", "asdict"):
        fn = getattr(obj, name, None)
        if callable(fn):
            try:
                return jsonable(fn(), depth + 1)
            except Exception:
                pass
    try:
        import dataclasses
        if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
            return {k: jsonable(v, depth + 1)
                    for k, v in dataclasses.asdict(obj).items()}
    except Exception:
        pass
    if hasattr(obj, "_asdict"):
        try:
            return jsonable(obj._asdict(), depth + 1)
        except Exception:
            pass
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        return {str(k): jsonable(v, depth + 1) for k, v in d.items()
                if not str(k).startswith("_")}
    try:
        import numpy as np
        if isinstance(obj, np.generic):
            return jsonable(obj.item(), depth + 1)
        if isinstance(obj, np.ndarray):
            return jsonable(obj.tolist(), depth + 1)
    except Exception:
        pass
    return str(obj)


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(jsonable(data), indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def rel(path):
    return shortest(path)


# --- choosing what to simulate ----------------------------------------------------------

def record_key(record):
    """What makes two scenarios hydraulically the same: ports driven, and how hard.

    The fortieth medium exchange of the same chamber at the same rate through the same
    port has the same field as the first, and Module 1 has 104 flowing steps.
    """
    ports = attr(record, "ports", default={}) or {}
    state = []
    for pid in sorted(ports):
        bc = ports[pid]
        state.append((pid, str(attr(bc, "type", default="?")),
                      round(_f(attr(bc, "q_ul_min", default=0.0), 0.0) or 0.0, 5),
                      round(_f(attr(bc, "p_pa", default=0.0), 0.0) or 0.0, 3)))
    return (str(attr(record, "operation", default="")), tuple(state))


def pick_records(srun, max_steps, max_worst):
    """The scenarios worth solving, and a sentence saying what was left out.

    No-flow steps are kept by scenario.py on purpose (bubbles, evaporation and settling
    all happen during a capped hold) but there is no field to solve in one, so they are
    counted here and not solved. The worst cases come along because they bracket an
    ambiguous multi-feed step, which is exactly where a single reading would mislead.
    """
    if srun is None:
        return [], "scenario.py produced nothing: geometry figures only"
    steps = list(attr(srun, "step_scenarios", default=None)
                 or attr(srun, "scenarios", default=[]) or [])
    worst = list(attr(srun, "worst_cases", default=[]) or [])
    worst_ids = {id(w) for w in worst}
    steps = [s for s in steps if id(s) not in worst_ids]
    no_flow = [s for s in steps if str(attr(s, "kind", default="")) == "no_flow"]
    flowing = [s for s in steps if s not in no_flow]
    seen, distinct = set(), []
    for record in flowing:
        key = record_key(record)
        if key in seen:
            continue
        seen.add(key)
        distinct.append(record)
    chosen = distinct[:max_steps] + worst[:max_worst]
    unmapped = list(attr(srun, "unmapped", default=[]) or [])
    note = (f"{len(steps)} scenarios ({len(no_flow)} no-flow, not solved), "
            f"{len(distinct)} hydraulically distinct, {len(worst)} worst cases; "
            f"{len(chosen)} solved"
            + (f" (capped at {max_steps} + {max_worst}; raise with --max-steps)"
               if len(distinct) > max_steps else "")
            + (f"; {len(unmapped)} step(s) could not be mapped" if unmapped else ""))
    return chosen, note


def record_label(record):
    bits = []
    step = attr(record, "step", default=None)
    if step is not None:
        bits.append(f"step {step}")
    kind = str(attr(record, "kind", default="") or "")
    if kind and kind != "step":
        bits.append(kind.replace("_", " "))
    operation = attr(record, "operation", default=None)
    if operation:
        bits.append(str(operation))
    targets = attr(record, "targets", default=None) or []
    if targets:
        bits.append("→ " + ", ".join(str(t) for t in targets[:2]))
    flow = _f(attr(record, "q_in_ul_min", default=None))
    if flow:
        bits.append(f"{flow:g} µL/min")
    temp = _f(attr(record, "temp_c", default=None))
    if temp is not None:
        bits.append(f"{temp:g} °C")
    return "  ·  ".join(bits) or "scenario"


# --- flagged assumptions ----------------------------------------------------------------

def standing_assumptions():
    """Contract values flagged `assumption: true` that every finding here rests on."""
    out = []
    if contracts is None:
        return [{"key": "assay2chip.lib.contracts", "value": None,
                 "used_for": "every limit in this report",
                 "source": f"UNAVAILABLE: {_CONTRACTS_ERROR}"}]
    for key, used_for in (
        ("capsule.escape_pressure_kpa", "leak and weir findings: the safe pressure"),
        ("control.pressure_margin", "leak findings: fraction of escape pressure allowed"),
        ("shear.culture_ceiling_dyn_cm2", "shear findings in ADHERENT-CELL chambers"),
        ("shear.prelysis_rinse_ceiling_dyn_cm2", "shear findings in a pre-lysis rinse"),
        ("capsule_channel.width_factor_low", "clog findings: capsules swell"),
        ("capsule.swell_factor", "clog findings: swollen capsule diameter"),
        ("exchange.target_fraction", "trap and exchange findings: the target"),
        ("fluid.viscosity_37c_pa_s", "every resistance and shear at 37 C"),
        ("mesh.bed_resistance_kpa_per_ul_min", "a packed capsule bed's resistance"),
    ):
        try:
            if contracts.is_assumption(key):
                out.append({"key": key, "value": contracts.param(key),
                            "used_for": used_for,
                            "source": "contracts/parameters.json (assumption: true)"})
        except Exception as exc:
            out.append({"key": key, "value": None, "used_for": used_for,
                        "source": f"CONTRACT GAP: {exc}"})
    return out


def collect_assumptions(blob, used_for, into):
    """Pull whatever `assumptions` a sibling reported into one list.

    Siblings publish them three ways - a list of dicts, a dict KEYED by name, and a
    list of Assumption dataclasses - so this goes through `figures._rows`, which keys a
    mapping by its own key. Iterating the mapping directly yields its keys as bare
    strings and silently loses every value, which is exactly what the first
    whole-pipeline report printed: eight flagged assumptions with no numbers.
    """
    for item in figures._rows(attr(blob, "assumptions", default=[]) or [],
                              id_keys=("key",)):
        item = jsonable(item)
        if not isinstance(item, dict):
            continue
        entry = {"key": str(item.get("key") or item.get("name") or "-"),
                 "value": item.get("value"),
                 "units": item.get("units"),
                 "why": item.get("why") or item.get("note") or "",
                 "used_for": used_for,
                 "source": item.get("source") or "sibling-local assumption"}
        into.append(entry)


def merge_assumptions(entries):
    """One row per (key, value): the same rule of thumb used on six chips is one
    assumption, not six, and a long report is one nobody reads to the end."""
    merged = {}
    for entry in entries:
        slot = merged.setdefault((entry["key"], json.dumps(entry.get("value"))),
                                 dict(entry, used_for=[]))
        used = entry.get("used_for")
        for item in (used if isinstance(used, list) else [used]):
            if item and item not in slot["used_for"]:
                slot["used_for"].append(item)
    out = []
    for entry in merged.values():
        where = entry["used_for"]
        shown = ", ".join(where[:3]) + (f" (+{len(where) - 3} more)"
                                        if len(where) > 3 else "")
        detail = f"{shown}. {entry.get('why', '')}".strip()
        out.append({"key": entry["key"],
                    "value": entry.get("value"),
                    "units": entry.get("units"),
                    "used_for": detail,
                    "source": entry.get("source")})
    return out


def collect_gaps(blob, subject, into):
    for item in jsonable(attr(blob, "unresolved", default=[]) or []):
        if not isinstance(item, dict):
            item = {"question": str(item)}
        entry = {"subject": f"{subject}: {item.get('key') or item.get('subject') or ''}"
                            .strip(" :"),
                 "question": item.get("question") or item.get("note")
                 or item.get("why") or json.dumps(item)[:200]}
        if entry not in into:
            into.append(entry)


# --- one chip's work, carried from the solve to the analysis ----------------------------

class ChipWork:
    """Everything one chip produced, held until couple and validate have run."""

    def __init__(self, chip, protocol, chip_path, protocol_path, module):
        self.chip = chip
        self.protocol = protocol
        self.chip_path = chip_path
        self.protocol_path = protocol_path
        self.module = module
        self.chip_id = str((chip or {}).get("id")
                           or Path(str(chip_path or "chip")).stem)
        self.mesh = None
        self.records = []          # ScenarioRecord per solved scenario
        self.fields = []           # the Field for each, or None
        self.transport = []        # the transport result for each, or None
        self.particles = None      # chip-level: particles.analyse_chip()
        self.findings = []
        self.fixes = []
        self.record = {}


# --- the run -----------------------------------------------------------------------------

class Run:
    def __init__(self, run_id, out_dir, mode, verbose=False, max_steps=MAX_STEPS,
                 max_loops=MAX_FIX_LOOPS, pipeline_mode=False,
                 n_particles=N_PARTICLES, timeout=CALL_TIMEOUT_S):
        self.run_id = run_id
        self.n_particles = n_particles
        self.timeout = timeout
        self.started = time.monotonic()
        self.out = Path(out_dir)
        self.figures_dir = self.out / "figures"
        self.mode = mode
        self.verbose = verbose
        self.max_steps = max_steps
        self.max_worst = MAX_WORST_CASES if max_steps > 1 else 0
        self.max_loops = max_loops
        self.pipeline_mode = pipeline_mode
        self.log_lines = []
        self.callers = {name: Caller(name, names, self.log, timeout)
                        for name, names in SIBLING_SPEC.items()}
        self.works = []
        self.assumptions = []
        self.gaps = []
        self.unmodelled = []
        self.figure_paths = []
        self.rerun = None
        self.loops = 0
        self.validation = None
        self.coupling = {}
        self.out.mkdir(parents=True, exist_ok=True)
        self.figures_dir.mkdir(parents=True, exist_ok=True)

    def log(self, message):
        """Timestamped from the start of the run, and FLUSHED.

        Block-buffered output is why the first whole-pipeline run looked like a hang
        with an empty log for forty minutes: it had in fact got as far as analyse.
        """
        stamp = f"[{time.monotonic() - self.started:6.1f}s]"
        self.log_lines.append(f"{stamp} {message}")
        if self.verbose:
            print(f"{stamp} {message}", flush=True)

    # --- mesh -> scenario -> solve -> transport -> particles ---------------------------

    def simulate_chip(self, chip, protocol, chip_path=None, protocol_path=None,
                      module=None):
        work = ChipWork(chip, protocol, chip_path, protocol_path, module)
        self.works.append(work)
        self.log(f"  {work.chip_id}")
        record = {
            "chip_id": work.chip_id,
            "module": module,
            "chip_path": rel(chip_path) if chip_path else None,
            "protocol_path": rel(protocol_path) if protocol_path else None,
            "scale": "demo" if figures.is_demo_scale(chip) else "target",
            "footprint_mm": [(chip.get("footprint") or {}).get("x"),
                             (chip.get("footprint") or {}).get("y")],
            "steps": [], "figures": [], "findings": [], "fixes": [], "notes": [],
            "mesh": {"status": NOT_YET, "nodes": None, "edges": None},
        }
        work.record = record

        srun = self._build_scenarios(work)
        work.records, note = pick_records(srun, self.max_steps, self.max_worst)
        record["step_note"] = note
        self.log(f"    {note}")
        for unmapped in jsonable(attr(srun, "unmapped", default=[]) or []):
            self.gaps.append({
                "subject": f"{work.chip_id} step {unmapped.get('step')}",
                "question": f"scenario not built: {unmapped.get('reason')} - "
                            f"{unmapped.get('detail')}"})

        work.mesh = self._build_mesh(work)
        if work.mesh is None:
            record["notes"].append(
                f"mesh.py {self.callers['mesh'].status}: no field can be solved, so the "
                f"figures for this chip are geometry only and there are no flow findings.")
            work.records = work.records[:1] if work.records else []

        work.fields = self._solve(work)
        work.transport = self._transport(work)
        work.particles = self._particles(work)
        record["steps"] = [self._step_summary(work, i) for i in range(len(work.records))]
        self._write_fields(work)
        return record

    def _build_scenarios(self, work):
        caller = self.callers["scenario"]
        if not caller.available or work.protocol_path is None:
            if work.protocol_path is None:
                self.gaps.append({
                    "subject": work.chip_id,
                    "question": "no protocol file: no port is driven or vented, so there "
                                "is no boundary condition to solve. Geometry only."})
            return None
        fn = caller.func("build_protocol")
        if fn is not None:
            srun, ok = caller.call(fn, work.protocol_path, work.chip_path)
            if ok:
                collect_gaps(srun, f"{work.chip_id} scenarios", self.gaps)
                return srun
            return None
        fn = caller.func("scenarios_for", "build_scenarios", "run")
        srun, ok = caller.call(fn, work.protocol, work.mesh)
        return srun if ok else None

    def _build_mesh(self, work):
        caller = self.callers["mesh"]
        if not caller.available:
            return None
        temp = self._dominant_temp(work)
        fn = caller.func("build_mesh")
        mesh = None
        if fn is not None:
            mesh, ok = caller.call(fn, work.chip, temp_c=temp, quiet=True)
            if not ok:
                mesh, ok = caller.call(fn, work.chip, temp_c=temp, strict=False)
                if ok:
                    work.record["notes"].append(
                        "meshed with strict=False after a strict MeshError: see "
                        "meta.unresolved in the mesh file for what the mesher could not "
                        "resolve from the spec.")
            if not ok:
                mesh = None
        if mesh is None and caller.func("mesh_chip") and work.chip_path:
            mesh, ok = caller.call(caller.func("mesh_chip"), work.chip_path)
            mesh = mesh if ok else None
        if mesh is None:
            work.record["mesh"]["status"] = caller.status
            return None
        mesh = self._fit_mesh_to_solvers(work, mesh, fn, temp)
        nodes = figures.mesh_nodes(mesh)
        edges = figures.mesh_edges(mesh)
        meta = figures.as_dict(mesh).get("meta") or {}
        work.record["mesh"] = {
            "status": "ok", "nodes": len(nodes), "edges": len(edges),
            "grid_mm": meta.get("grid_mm"), "mu_pa_s": meta.get("mu_pa_s"),
        }
        collect_gaps(meta, f"{work.chip_id} mesh", self.gaps)
        if isinstance(meta, dict):
            for item in meta.get("unresolved") or []:
                entry = {"subject": f"{work.chip_id} mesh",
                         "question": json.dumps(jsonable(item))[:260]}
                if entry not in self.gaps:
                    self.gaps.append(entry)
        write_json(self.out / f"{work.chip_id}.mesh.json", mesh)
        self.log(f"    mesh: {len(nodes)} nodes, {len(edges)} edges "
                 f"(grid {meta.get('grid_mm')} mm)")
        return mesh

    def _fit_mesh_to_solvers(self, work, mesh, build_fn, temp):
        """Coarsen the grid until the scalar-transport solve can actually run on it.

        `transport.py` has no sparse factorisation (no scipy in this repo) and refuses
        above `DENSE_SOLVE_MAX` unknowns rather than returning something unconverged -
        which is the right call. Module 3's trap meshes to ~12900 cells at 0.1 mm, so
        without this the residence-time half of the trap test silently never runs.
        Coarsening is recorded on the chip, because a coarser grid resolves a dead corner
        less well and the reader has to know which grid a number came from.
        """
        transport = self.callers["transport"]
        if build_fn is None or not transport.available:
            return mesh
        limit = int(getattr(transport.module, "DENSE_SOLVE_MAX", 0) or 0)
        if limit <= 0:
            return mesh
        for _ in range(4):
            count = len(figures.mesh_nodes(mesh))
            if count <= limit:
                return mesh
            meta = figures.as_dict(mesh).get("meta") or {}
            grid = _f(meta.get("grid_mm"))
            if not grid:
                return mesh
            coarser = round(grid * math.sqrt(count / (limit * 0.85)), 3)
            retry, ok = self.callers["mesh"].call(
                build_fn, work.chip, temp_c=temp, grid_mm=coarser, quiet=True)
            if not ok or retry is None:
                return mesh
            work.record["notes"].append(
                f"mesh coarsened {grid:g} -> {coarser:g} mm ({count} cells is over "
                f"transport.py's DENSE_SOLVE_MAX of {limit}); a coarser grid resolves a "
                f"dead corner less finely, and the solve itself did not need it.")
            mesh = retry
        return mesh

    def _dominant_temp(self, work):
        temps = [_f(attr(r, "temp_c", default=None)) for r in work.records]
        temps = [t for t in temps if t is not None]
        if temps:
            return max(temps)
        steps = (work.protocol or {}).get("steps") or []
        temps = [_f(s.get("temp_c")) for s in steps if isinstance(s, dict)]
        temps = [t for t in temps if t is not None]
        return max(temps) if temps else None

    def _model_scenario(self, record):
        """The `Scenario` the solver wants, out of a scenario.py record."""
        built = attr(record, "scenario", default=None)
        if built is not None and not isinstance(built, (str, int, float)):
            return built
        caller = self.callers["scenario"]
        fn = caller.func("make_scenario")
        if fn is not None:
            made, ok = caller.call(fn, record, quiet=True)
            if ok:
                return made
        return record

    def _solve(self, work):
        caller = self.callers["solve"]
        out = [None] * len(work.records)
        if work.mesh is None or not work.records or not caller.available:
            return out
        scenarios = [self._model_scenario(r) for r in work.records]
        batch = caller.func("solve_steps")
        if batch is not None:
            fields, ok = caller.call(batch, work.mesh, scenarios, quiet=True)
            if ok and fields is not None and len(list(fields)) == len(scenarios):
                self.log(f"    solved {len(scenarios)} scenario(s) in one assembly")
                return list(fields)
        single = caller.func("solve_step", "solve")
        solved = 0
        for i, scenario in enumerate(scenarios):
            field, ok = caller.call(single, work.mesh, scenario)
            if ok:
                out[i] = field
                solved += 1
        if batch is not None and solved:
            work.record["notes"].append(
                f"batched solve failed, so each scenario was solved on its own: "
                f"{solved} of {len(scenarios)} succeeded. One unsolvable step no longer "
                f"costs the others.")
        self.log(f"    solved {solved} of {len(scenarios)} scenario(s)")
        return out

    def _transport(self, work):
        caller = self.callers["transport"]
        out = [None] * len(work.records)
        if not caller.available:
            return out
        self.log(f"    transport on {sum(f is not None for f in work.fields)} field(s)")
        fn = caller.func("analyse", "transport", "residence_times")
        for i, field in enumerate(work.fields):
            if field is None:
                continue
            step = self._raw_step(work, work.records[i])
            result, ok = caller.call(fn, work.mesh, field, step=step)
            if ok:
                out[i] = result
                collect_assumptions(result, f"{work.chip_id} transport",
                                    self.assumptions)
        return out

    def _raw_step(self, work, record):
        number = attr(record, "step", default=None)
        for step in (work.protocol or {}).get("steps") or []:
            if isinstance(step, dict) and step.get("step") == number:
                return step
        return {"step": number, "operation": attr(record, "operation", default=""),
                "flow_ul_min": _f(attr(record, "q_in_ul_min", default=None)),
                "volume_ul": _f(attr(record, "volume_ul", default=None)),
                "temp_c": _f(attr(record, "temp_c", default=None))}

    def _particles(self, work):
        caller = self.callers["particles"]
        if not caller.available:
            return None
        self.log(f"    particles ({self.n_particles} per class)")
        fn = caller.func("analyse_chip")
        if fn is not None:
            protocol_id = (work.protocol or {}).get("id")
            result, ok = caller.call(fn, work.chip_id, protocol_id,
                                     n_particles=self.n_particles)
            if ok:
                collect_assumptions(result, f"{work.chip_id} particles",
                                    self.assumptions)
                return result
            return None
        fn = caller.func("particles", "analyse", "run")
        result, ok = caller.call(fn, work.mesh, work.fields[0] if work.fields else None)
        return result if ok else None

    def _step_summary(self, work, i):
        record = work.records[i]
        field = work.fields[i]
        fluid = attr(record, "fluid", default=None)
        summary = {
            "step": attr(record, "step", default=None),
            "scenario_id": attr(record, "id", default=None),
            "kind": attr(record, "kind", default=None),
            "label": record_label(record),
            "operation": attr(record, "operation", default=None),
            "targets": list(attr(record, "targets", default=[]) or []),
            "flow_ul_min": _f(attr(record, "q_in_ul_min", default=None)),
            "temp_c": _f(attr(record, "temp_c", default=None)),
            "cites": attr(record, "cites", default=None),
            "inferred": bool(attr(record, "inferred", default=False)),
            "ambiguous": bool(attr(record, "ambiguous", default=False)),
            "viscosity_pa_s": _f(attr(fluid, "viscosity_pa_s", default=None)),
            "viscosity_key": attr(fluid, "contract_key", default=None),
            "viscosity_is_assumption": bool(attr(fluid, "assumption", default=False)),
            "fluid": attr(fluid, "name", default=None),
            "field": "ok" if field is not None else self._why("solve"),
            "transport": "ok" if work.transport[i] is not None
            else self._why("transport"),
        }
        if summary["viscosity_is_assumption"]:
            entry = {"key": attr(fluid, "contract_key", default=None)
                     or f"viscosity of {summary['fluid']}",
                     "value": summary["viscosity_pa_s"],
                     "used_for": f"{work.chip_id} step {summary['step']} "
                                 f"({summary['fluid']}): no contract covers this fluid",
                     "source": str(attr(fluid, "source", default="scenario.py"))}
            if entry not in self.assumptions:
                self.assumptions.append(entry)
        if field is not None:
            residual = figures.as_dict(field).get("residual")
            if residual:
                summary["residual"] = jsonable(residual)
            bad = self._unmodelled(work.chip_id, summary["step"], work.mesh, field)
            if bad:
                self.unmodelled += bad
                summary["edges_above_re_1"] = len(bad)
                summary["re_max"] = bad[0]["re"]
        return summary

    def _why(self, caller_name):
        """Why there is no result from this module, in one word the report can print."""
        caller = self.callers[caller_name]
        if caller.errors:
            return "errored"
        if not caller.available:
            return caller.status
        return "not solved"

    def _unmodelled(self, chip_id, step, mesh, field):
        """Edges above Re 1: outside Stokes, so reported rather than trusted.

        cfd/README.md is explicit that Re > 1 is a FINDING and not a number, and the
        audit found real cases (Module 6's quench spine at Re 13-23, a nozzle at Re ~9).
        """
        re_map, positional = figures.series(field, "re", "reynolds")
        if not re_map:
            return []
        out = []
        for i, edge in enumerate(figures.mesh_edges(mesh)):
            re = _f(figures._at(re_map, positional, edge, i))
            if re is not None and re > 1.0:
                out.append({"chip_id": chip_id, "edge": edge.get("id") or i,
                            "feature_id": edge.get("feature_id"), "step": step,
                            "re": re,
                            "note": "above Re 1: outside the depth-averaged Stokes model"})
        out.sort(key=lambda row: -row["re"])
        return out

    def _write_fields(self, work):
        if not work.records:
            return
        entries = []
        for i, record in enumerate(work.records):
            entries.append({
                "step": attr(record, "step", default=None),
                "scenario_id": attr(record, "id", default=None),
                "label": record_label(record),
                "scenario": jsonable(record),
                "field": jsonable(work.fields[i]),
                "transport": jsonable(work.transport[i]),
            })
        write_json(self.out / f"{work.chip_id}.field.json", {
            "chip_id": work.chip_id,
            "note": "One entry per SOLVED scenario: p per node and Q per edge live in "
                    "each entry's `field`, residence time and washout in `transport`. "
                    "No-flow steps are not solved and are not here.",
            "steps": entries,
        })

    # --- couple, validate, analyse, fix, figures ---------------------------------------

    def couple(self, chain_path, handoffs):
        caller = self.callers["couple"]
        result = None
        coupling = {}
        if caller.available and chain_path is not None:
            reader = caller.func("read_chain", "read_pipeline", "read_partition")
            chain, ok = (caller.call(reader, chain_path) if reader else (None, False))
            fn = caller.func("couple", "couple_pipeline")
            if ok and chain is not None and fn is not None:
                result, ok = caller.call(fn, chain)
                if not ok:
                    result = None
            elif fn is not None:
                runner = caller.func("run")
                if runner is not None:
                    pair, ok = caller.call(runner, chain_path, self.out, quiet=True)
                    result = pair[0] if ok and isinstance(pair, tuple) else None
        if result is not None:
            data = jsonable(result)
            for row in figures._rows(data.get("handoffs") or data.get("joins")
                                     or data.get("coupling") or [], id_keys=("id",)):
                coupling[str(row.get("id") or len(coupling))] = row
            collect_assumptions(data, "tubing and handoffs", self.assumptions)
            collect_gaps(data, "handoffs", self.gaps)
            self.log(f"  coupled {len(coupling)} handoff(s)")
        payload = {
            "run": self.run_id,
            "status": "ok" if result is not None else caller.status,
            "detail": caller.detail,
            "handoffs": list(handoffs),
            "coupling": coupling or None,
            "note": ("Chips joined through tubing models: dead volume, the pressure "
                     "budget and the particle path per handoff."
                     if result is not None else
                     f"couple.py {caller.status}: the per-chip fields were NOT joined, "
                     f"so nothing here accounts for tubing, dead volume or the "
                     f"cross-chip pressure budget. The handoffs below are only what the "
                     f"pipeline file declares."),
        }
        if result is not None:
            payload["pipeline_field"] = jsonable(result)
        write_json(self.out / "pipeline.field.json", payload)
        self.coupling = coupling
        self._couple_result = result
        return coupling

    def validate(self):
        """Run the benchmarks FIRST: a failed one makes every finding untrusted.

        Called before any chip is simulated so `analyse.py` gets the verdict in its
        Bundle and can mark its findings, rather than having to be told afterwards.
        """
        if self.validation is not None:
            return self.validation
        caller = self.callers["validate"]
        if not caller.available:
            payload = {"status": NOT_YET, "detail": caller.detail, "checks": [],
                       "trusted": False,
                       "note": "Nothing has checked this solver, so every finding in "
                               "this run is UNTRUSTED (cfd/README.md)."}
            write_json(self.out / "validation.json", payload)
            self.validation = payload
            return payload
        fn = caller.func("run", "validate", "benchmarks")
        result, ok = caller.call(fn)
        if not ok or result is None:
            payload = {"status": "errored", "trusted": False, "checks": [],
                       "detail": caller.errors[0] if caller.errors else caller.detail}
            write_json(self.out / "validation.json", payload)
            self.validation = payload
            return payload
        data = jsonable(result)
        # validate.py publishes `checks` in exactly the shape report.py renders. Prefer
        # it; only fall back to flattening `benchmarks` for a validator that does not.
        checks = [row for row in figures._rows(data.get("checks") or [],
                                               id_keys=("name",))
                  if row.get("name")]
        if not checks:
            for bench in figures._rows(data.get("benchmarks") or [], id_keys=("id",)):
                status = str(bench.get("status") or
                             ("pass" if bench.get("ok") else "fail")).lower()
                checks.append({
                    "name": bench.get("name") or bench.get("id"),
                    "ok": status == "pass",
                    "status": status,
                    "expected": bench.get("expected") or bench.get("tolerance"),
                    "got": bench.get("value"),
                    "detail": f"{bench.get('metric', '')} {bench.get('units', '')} · "
                              f"{bench.get('message', '')}".strip(" ·"),
                })
        status = str(data.get("status") or
                     ("pass" if checks and all(c.get("ok") for c in checks)
                      else "unknown"))
        # `trusted` is validate.py's own verdict and gates on every benchmark having RUN
        # as well as passed, so it is not the same as "nothing failed". Never recompute it.
        stated = next((data[key] for key in ("trusted", "passed", "ok") if key in data),
                      None)
        trusted = bool(stated) if stated is not None else status == "pass"
        payload = {
            "status": status,
            "trusted": trusted,
            "detail": (data.get("reason") or data.get("untrusted_reason")
                       or caller.detail),
            "counts": data.get("counts"),
            "gating_missing": data.get("gating_missing"),
            "solver": data.get("solver"),
            "recommended_grid_mm": data.get("recommended_grid_mm"),
            "grid_mm": data.get("grid_mm"),
            "checks": checks,
            "validation_findings": data.get("validation_findings") or [],
        }
        write_json(self.out / "validation.json", payload)
        self.validation = payload
        self.log(f"  validation: {status} (trusted={trusted}) "
                 f"{data.get('counts') or ''}")
        return payload

    def analyse(self):
        """One analyse call over every chip, so handoff findings see both sides."""
        caller = self.callers["analyse"]
        if not caller.available:
            for work in self.works:
                if work.particles is not None:
                    found = jsonable(attr(work.particles, "findings", default=[]) or [])
                    work.findings = [dict(f) for f in found]
                    work.record["notes"].append(
                        f"analyse.py {caller.status}: the findings shown for this chip "
                        f"are particles.py's own, which cover clog, sediment and weir "
                        f"escape only - not leaks, traps, dead ends or shear.")
            return
        bundle_cls = getattr(caller.module, "Bundle", None)
        step_cls = getattr(caller.module, "StepBundle", None)
        views_fn = caller.func("step_views")
        fn = caller.func("analyse", "analyse_chip")
        if bundle_cls is None or step_cls is None or views_fn is None:
            result, ok = caller.call(fn, self.works[0].chip if self.works else None)
            if ok:
                for work in self.works:
                    work.findings = [dict(f) for f in figures._rows(result)]
            return
        bundles, owners = [], []
        for work in self.works:
            views, ok = caller.call(views_fn, work.protocol, quiet=True)
            by_label = {}
            for view in (views or []) if ok else []:
                by_label[str(attr(view, "label", default=None))] = view
            steps = []
            for i, record in enumerate(work.records):
                view = by_label.get(str(attr(record, "step", default=None)))
                if view is None and views:
                    view = views[min(i, len(views) - 1)]
                if view is None:
                    continue
                steps.append(step_cls(view=view, field=work.fields[i],
                                      scenario=record, transport=work.transport[i],
                                      particles=work.particles))
            bundle, ok = caller.call(
                bundle_cls, chip=work.chip, protocol=work.protocol, mesh=work.mesh,
                steps=steps, couple=getattr(self, "_couple_result", None),
                validation=self.validation, chip_id=work.chip_id)
            if ok and bundle is not None:
                bundles.append(bundle)
                owners.append(work)
        if not bundles:
            return
        self.log(f"  analyse: {len(bundles)} bundle(s), "
                 f"{sum(len(b.steps) for b in bundles if hasattr(b, 'steps'))} step(s)")
        found, ok = caller.call(fn, bundles)
        if not ok:
            found = []
            for bundle, work in zip(bundles, owners):
                part, ok = caller.call(fn, bundle)
                if ok:
                    found += list(part or [])
        to_json = caller.func("findings_json")
        rows = (to_json(found) if to_json else [jsonable(f) for f in found or []])
        by_chip = {}
        for row in rows:
            by_chip.setdefault(str(row.get("chip_id") or ""), []).append(row)
        for work in self.works:
            work.findings = by_chip.get(work.chip_id, [])
            work.record["findings"] = work.findings
        orphans = [r for cid, group in by_chip.items() for r in group
                   if cid not in {w.chip_id for w in self.works}]
        if orphans and self.works:
            self.works[0].record.setdefault("cross_chip_findings", orphans)
        self.log(f"  findings: {len(rows)}")

    def fix(self):
        caller = self.callers["fix"]
        mode = "pipeline" if self.pipeline_mode else "standalone"
        for work in self.works:
            if not work.findings:
                continue
            if not caller.available:
                work.record["notes"].append(
                    f"fix.py {caller.status}: findings are reported without fixes.")
                continue
            fn = caller.func("propose", "propose_fixes", "run")
            self.log(f"  fix: {work.chip_id}, {len(work.findings)} finding(s), "
                     f"mode {mode}")
            result, ok = caller.call(fn, work.findings, work.chip,
                                     work.protocol or {}, mode=mode)
            if not ok or result is None:
                continue
            self.loops = max(self.loops, 1)
            data = jsonable(result)
            work.fixes = [dict(f) for f in data.get("fixes") or []]
            work.record["fixes"] = work.fixes
            collect_assumptions(data, f"{work.chip_id} fixes", self.assumptions)
            collect_gaps(data, f"{work.chip_id} fixes", self.gaps)
            applied = [f for f in work.fixes if f.get("applied")]
            work.record["notes"].append(
                f"fix.py ({mode}, backend {data.get('backend')}): "
                f"{len(work.fixes)} fix(es), {len(applied)} applied to a COPY, "
                f"{len(data.get('rerun_requests') or [])} re-run request(s).")
            patched = data.get("patched_chip") or attr(result, "patched_chip",
                                                       default=None)
            if patched and not self.pipeline_mode:
                path = write_json(self.out / "patched" / f"{work.chip_id}.json", patched)
                work.record["patched_chip"] = rel(path)
            for request in data.get("rerun_requests") or []:
                stage = (request.get("rerun_from") or request.get("stage") or "s6")
                reason = (request.get("reason") or request.get("why")
                          or f"{work.chip_id}: a geometry fix changes the chip")
                if self.rerun is None and self.pipeline_mode:
                    self.rerun = {
                        "rerun_from": str(stage),
                        "reason": f"{work.chip_id}: {reason}",
                        "chip_id": work.chip_id,
                        "requests": data.get("rerun_requests"),
                    }
                    self.log(f"    rerun_from {stage}: {reason}")

    def draw(self, pipeline=None, chips_by_module=None):
        for work in self.works:
            entries = list(range(len(work.records))) or [None]
            for i in entries:
                record = work.records[i] if i is not None else None
                step = attr(record, "step", default=None) if record else None
                label = record_label(record) if record else "no protocol step"
                paths = figures.step_figures(
                    work.chip, self.figures_dir, mesh=work.mesh,
                    field=work.fields[i] if i is not None else None,
                    transport=work.transport[i] if i is not None else None,
                    findings=[f for f in work.findings if f.get("step") in (None, step)],
                    step=step if step is not None else f"c{i or 0}", step_label=label,
                    step_data=self._raw_step(work, record) if record else None)
                for path in paths:
                    work.record["figures"].append(rel(path))
                    self.figure_paths.append(rel(path))
        if pipeline is not None:
            try:
                path = figures.pipeline_overview(
                    pipeline, self.figures_dir, chips=chips_by_module or {},
                    findings_by_chip={w.chip_id: w.findings for w in self.works},
                    statuses={w.module: (w.record["mesh"]["status"]
                                         if w.record["mesh"]["status"] != "ok" else None)
                              for w in self.works if w.module},
                    coupling=self.coupling)
                self.figure_paths.append(rel(path))
            except Exception as exc:
                self.log(f"  ! pipeline overview failed: {type(exc).__name__}: {exc}")

    # --- the summary ---------------------------------------------------------------------

    def finish(self, title, handoffs=(), chain_path=None, pipeline=None,
               chips_by_module=None):
        self.log("  couple")
        self.couple(chain_path, handoffs)
        self.validate()
        self.analyse()
        self.fix()
        self.log("  figures")
        self.draw(pipeline, chips_by_module)
        findings = [f for work in self.works for f in work.findings]
        fixes = [f for work in self.works for f in work.fixes]
        result = {
            "run": self.run_id,
            "title": title,
            "mode": self.mode,
            "generated_at": now(),
            "out_dir": rel(self.out),
            "modules": {name: caller.report_row()
                        for name, caller in self.callers.items()},
            "chips": [work.record for work in self.works],
            "handoffs": list(handoffs),
            "coupling": self.coupling,
            "findings": findings,
            "fixes": fixes,
            "validation": self.validation,
            "unmodelled": self.unmodelled,
            "assumptions": merge_assumptions(standing_assumptions()
                                             + self.assumptions),
            "gaps": self.gaps,
            "figures": self.figure_paths,
            "loops": self.loops,
            "max_loops": self.max_loops,
            "rerun": self.rerun,
            "log": self.log_lines,
        }
        write_json(self.out / "findings.json", findings)
        write_json(self.out / "fixes.json", fixes)
        write_json(self.out / "cfd.json", result)
        if report is not None:
            try:
                report.write(result, self.out / "report.md")
            except Exception as exc:
                self.log(f"  ! report failed: {type(exc).__name__}: {exc}")
        return result


# --- the three modes --------------------------------------------------------------------

def _read_chip(path):
    chip = load_json(path)
    if not isinstance(chip, dict) or "footprint" not in chip:
        raise ValueError(f"{rel(path)} is not a chip spec (no footprint)")
    return chip


def protocol_for(chip_id, run=None):
    """The protocol that drives this chip. -> (path, how it was found).

    `protocols/<chip_id>.json` covers most of the repo, but not all of it:
    `chip/ipsc_to_mgl_chip_v1.json` is driven by `protocols/ipsc_to_mgl_v1.json`. Rather
    than keep a hand-written table here, ask `scenario.py` - it owns the pairing and has
    `chip_for_protocol()` for exactly this. Falling back to the name convention keeps
    standalone mode working while scenario.py is unavailable.
    """
    direct = PROTOCOL_DIR / f"{chip_id}.json"
    if direct.exists():
        return direct, "protocols/<chip_id>.json"
    caller = run.callers["scenario"] if run is not None else None
    if caller is not None and caller.module is not None:
        matcher = caller.func("chip_for_protocol")
        if matcher is not None:
            for path in sorted(PROTOCOL_DIR.glob("*.json")):
                try:
                    protocol = load_json(path)
                except ValueError:
                    continue
                if protocol.get("kind") == "pipeline":
                    continue
                pair, ok = caller.call(matcher, protocol, quiet=True)
                if ok and pair and str(pair[0]) == chip_id:
                    return path, f"scenario.chip_for_protocol: {pair[1]}"
    return None, "no protocol found"


def run_pipeline(pipeline_path, out_dir=None, run_id=None, verbose=False,
                 max_steps=MAX_STEPS, max_loops=MAX_FIX_LOOPS,
                 n_particles=N_PARTICLES, timeout=CALL_TIMEOUT_S):
    """The WHOLE chain, end to end: every module's chip, then the joins between them."""
    pipeline = load_json(pipeline_path)
    run_id = run_id or str(pipeline.get("id") or Path(pipeline_path).stem)
    run = Run(run_id, out_dir or (WORK / run_id / "cfd"), "pipeline", verbose,
              max_steps, max_loops, pipeline_mode=True, n_particles=n_particles,
              timeout=timeout)
    run.log(f"S10 CFD: {run_id} ({rel(pipeline_path)})")
    run.validate()
    chips_by_module = {}
    for module in pipeline.get("modules") or []:
        mid = str(module.get("module") or module.get("id") or "?")
        chip_rel = module.get("chip")
        if not chip_rel or not (ROOT / chip_rel).exists():
            run.log(f"  {mid}: no chip (status {module.get('status')}) - skipped")
            run.gaps.append({
                "subject": f"module {mid}",
                "question": (f"{chip_rel} does not exist" if chip_rel else
                             f"placeholder with no chip: "
                             f"{module.get('status_note') or module.get('note') or '-'}")})
            continue
        chip_path = ROOT / chip_rel
        try:
            chip = _read_chip(chip_path)
        except Exception as exc:
            run.log(f"  ! {mid}: {exc}")
            run.gaps.append({"subject": f"module {mid}", "question": str(exc)})
            continue
        protocol_rel = module.get("protocol")
        protocol_path = ROOT / protocol_rel if protocol_rel else None
        protocol = None
        if protocol_path and protocol_path.exists():
            try:
                protocol = load_json(protocol_path)
            except Exception as exc:
                run.gaps.append({"subject": f"module {mid} protocol",
                                 "question": str(exc)})
                protocol_path = None
        elif protocol_rel:
            run.gaps.append({"subject": f"module {mid} protocol",
                             "question": f"{protocol_rel} does not exist"})
            protocol_path = None
        chips_by_module[mid] = chip
        run.simulate_chip(chip, protocol, chip_path, protocol_path, mid)
    return run.finish(pipeline.get("name") or run_id, pipeline.get("handoffs") or [],
                      pipeline_path, pipeline, chips_by_module)


def run_chip(chip_path, protocol_path=None, out_dir=None, run_id=None, verbose=False,
             max_steps=MAX_STEPS, max_loops=MAX_FIX_LOOPS, n_particles=N_PARTICLES,
             timeout=CALL_TIMEOUT_S):
    chip = _read_chip(chip_path)
    chip_id = str(chip.get("id") or Path(chip_path).stem)
    run_id = run_id or chip_id
    run = Run(run_id, out_dir or (WORK / run_id / "cfd"), "chip", verbose, max_steps,
              max_loops, pipeline_mode=False, n_particles=n_particles,
              timeout=timeout)
    run.log(f"S10 CFD: {chip_id} ({rel(chip_path)})")
    if protocol_path is None:
        protocol_path, how = protocol_for(chip_id, run)
        if protocol_path is not None and how != "protocols/<chip_id>.json":
            run.log(f"  protocol: {rel(protocol_path)} ({how})")
    protocol = load_json(protocol_path) if protocol_path else None
    run.validate()
    run.simulate_chip(chip, protocol, chip_path, protocol_path)
    return run.finish(chip.get("name") or chip_id)


def run_all_chips(out_dir=None, run_id="all_chips", verbose=False,
                  max_steps=MAX_STEPS, max_loops=MAX_FIX_LOOPS,
                  n_particles=N_PARTICLES, timeout=CALL_TIMEOUT_S):
    """Every chip in chip/. Each one is a test case for the solver (cfd/README.md)."""
    run = Run(run_id, out_dir or (WORK / run_id / "cfd"), "all-chips", verbose,
              max_steps, max_loops, pipeline_mode=False, n_particles=n_particles,
              timeout=timeout)
    run.log(f"S10 CFD: every chip in {rel(CHIP_DIR)}")
    run.validate()
    for chip_path in sorted(CHIP_DIR.glob("*.json")):
        try:
            chip = _read_chip(chip_path)
        except Exception as exc:
            run.log(f"  ! {chip_path.name}: {exc}")
            run.gaps.append({"subject": chip_path.name, "question": str(exc)})
            continue
        chip_id = str(chip.get("id") or chip_path.stem)
        protocol_path, how = protocol_for(chip_id, run)
        if protocol_path is None:
            run.gaps.append({
                "subject": chip_id,
                "question": "no protocol drives this chip, by name or by "
                            "scenario.chip_for_protocol: no port is driven or vented, "
                            "so only its geometry was drawn."})
        protocol = load_json(protocol_path) if protocol_path else None
        run.simulate_chip(chip, protocol, chip_path, protocol_path)
    return run.finish("Every chip in chip/")


# --- CLI ---------------------------------------------------------------------------------

def bootstrap():
    """Load this package's own figures and report, by path like every sibling."""
    global figures, report
    if figures is None:
        figures, status, detail = load_sibling("figures")
        if figures is None:
            raise SystemExit(f"assay2chip/cfd/figures.py: {status} ({detail})")
    if report is None:
        report, status, detail = load_sibling("report")
        if report is None:
            print(f"report.py: {status} ({detail}) - writing JSON only", file=sys.stderr)
    return figures, report


def describe_modules():
    out = []
    for name, names in SIBLING_SPEC.items():
        row = Caller(name, names, lambda _m: None).report_row()
        out.append(f"  {name + '.py':<14} {row['status']:<18} {row['detail']}")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="S10 CFD: simulate the whole chip set end to end.",
        epilog="A cfd module that does not exist yet is reported as NOT YET AVAILABLE "
               "and skipped, never a crash.")
    ap.add_argument("--pipeline", help="protocols/pipeline_mvp.json: the whole chain")
    ap.add_argument("--chip", help="chip/<id>.json: one chip")
    ap.add_argument("--protocol", help="protocols/<id>.json, with --chip")
    ap.add_argument("--all-chips", action="store_true", help="every chip in chip/")
    ap.add_argument("--out", help="output directory (default work/<run>/cfd)")
    ap.add_argument("--id", help="run id, i.e. the work/<id> directory")
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS,
                    help=f"hydraulically distinct steps solved per chip (default "
                         f"{MAX_STEPS})")
    ap.add_argument("--max-loops", type=int, default=MAX_FIX_LOOPS,
                    help=f"cap on the fix-and-re-run loop (default {MAX_FIX_LOOPS})")
    ap.add_argument("--particles", type=int, default=N_PARTICLES,
                    help=f"particles tracked per class in particles.py (default "
                         f"{N_PARTICLES}; tracking is the slowest part of a run)")
    ap.add_argument("--timeout", type=float, default=CALL_TIMEOUT_S,
                    help=f"wall-clock budget for ONE call into ONE cfd module, seconds "
                         f"(default {CALL_TIMEOUT_S:g}; 0 waits forever). An overrun is "
                         f"reported as `timed out` and skipped.")
    ap.add_argument("--list", action="store_true",
                    help="which assay2chip/cfd modules exist, and exit")
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if args.list:
        print("assay2chip/cfd modules:")
        print(describe_modules())
        return 0
    bootstrap()
    if not (args.pipeline or args.chip or args.all_chips):
        ap.error("pass --pipeline, --chip or --all-chips")

    results = []
    common = dict(verbose=args.verbose, max_steps=args.max_steps,
                  max_loops=args.max_loops, n_particles=args.particles,
                  timeout=args.timeout)
    if args.pipeline:
        results.append(run_pipeline(Path(args.pipeline), args.out, args.id, **common))
    if args.chip:
        results.append(run_chip(Path(args.chip),
                                Path(args.protocol) if args.protocol else None,
                                args.out, args.id, **common))
    if args.all_chips:
        results.append(run_all_chips(args.out, args.id or "all_chips", **common))

    for result in results:
        if args.json:
            print(json.dumps(jsonable(result), indent=1))
            continue
        print(f"\n{result['run']}: {result['mode']}")
        for name, info in result["modules"].items():
            if info["status"] != "ok":
                print(f"  {name}.py: {info['status']} - {info['detail']}")
        counts = {}
        for finding in result["findings"]:
            key = str(finding.get("severity") or "info")
            counts[key] = counts.get(key, 0) + 1
        print(f"  chips: {len(result['chips'])}  "
              f"scenarios solved: {sum(1 for c in result['chips'] for s in c['steps'] if s.get('field') == 'ok')}"
              f"/{sum(len(c['steps']) for c in result['chips'])}  "
              f"findings: {counts or 'none'}  figures: {len(result['figures'])}")
        validation = result["validation"] or {}
        print(f"  validation: {validation.get('status')} "
              f"(trusted={validation.get('trusted')})")
        if result["rerun"]:
            print(f"  rerun_from {result['rerun']['rerun_from']}: "
                  f"{result['rerun']['reason'][:140]}")
        print(f"  -> {result['out_dir']}/report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

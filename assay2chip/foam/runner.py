"""assay2chip/foam/runner.py: the ONLY place that knows how to launch OpenFOAM.

    python -m assay2chip.foam.runner            # what is installed, and where
    python -m unittest tests.test_foam_runner -v

Everything else in `assay2chip/foam/` builds a case directory and asks this module to run
a command in it. Nothing else imports `subprocess`, nothing else knows what `openfoam`
is called or where the app lives, and nothing else decides whether a run converged.

    from assay2chip.foam import runner

    if not runner.available():
        print(runner.why_not())              # a full sentence, with the install command
    runner.version()                          # '2606'
    runner.install().launcher                 # PosixPath('/opt/homebrew/bin/openfoam')

    runner.run(case_dir, "blockMesh")
    runner.run(case_dir, "checkMesh")
    res = runner.run(case_dir, "simpleFoam", parallel=True)   # 6 of 8 cores
    res.converged, res.verdict, res.final_residuals, res.wall_time_s
    res.manifest()                            # straight into the run summary JSON

WHAT `parallel` DOES. `parallel=True` (or an int) runs the full sequence: `decomposePar
-force`, then `mpirun -np N <command> -parallel`, then `reconstructPar`. The number of
subdomains comes from `system/decomposeParDict`, which `case.build_case(n_cores=...)`
wrote - the two are checked against each other and a mismatch is an error, not a silent
re-decomposition. `parallel=None` is serial. `parallel=False` is serial and explicit.

WHY SIX CORES. This machine has 8. OpenFOAM scales well until the per-core cell count
drops below a few thousand, and leaving two cores free keeps the rest of the pipeline (and
the machine) responsive; a chip mesh of 1-3 M cells is comfortably above that at 6. It is
only a default: `parallel=8` or `FOAM_CORES=4` in the environment both override it.

ERRORS ARE LOUD AND SPECIFIC. A failed run raises `FoamRunFailed` carrying the command,
the exit code, the log path and the last OpenFOAM `--> FOAM FATAL ERROR` block verbatim,
because "simpleFoam exited 1" has never once helped anybody. `FoamNotInstalled` prints
the exact Homebrew command from `assay2chip/foam/README.md`.

CONVERGENCE IS A VERDICT, NOT A BOOLEAN DRESSED UP. A steady run that hits
`residualControl` converged. One that reached `endTime` with falling residuals did NOT -
it ran out of iterations, which is a different thing, and `verdict` says so. Divergence,
a continuity error that grows, and a Courant number above 1 on a transient run each get
their own verdict so the caller can tell them apart.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: What `assay2chip/foam/README.md` pins: ESI OpenCFD OpenFOAM, native on Apple Silicon.
INSTALL_COMMAND = "brew install gerlero/openfoam/openfoam"
PROJECT_URL = "https://github.com/gerlero/openfoam-app"

#: The session wrapper the cask installs. It sets up the OpenFOAM environment (and mounts
#: the case-sensitive disk image the app ships) and then runs whatever follows.
LAUNCHER_NAMES = ("openfoam",)
#: Versioned casks install `openfoam2606`, `openfoam2512`, ... alongside the generic one.
VERSIONED_LAUNCHER = re.compile(r"^openfoam(\d{4})$")
#: Where the app lands when installed by hand instead of by Homebrew.
APP_GLOBS = ("/Applications/OpenFOAM-v*.app", str(Path.home() / "Applications/OpenFOAM-v*.app"))
APP_LAUNCHER = "Contents/Resources/etc/openfoam"

#: 8 cores on this machine; leave two for everything else. FOAM_CORES overrides.
DEFAULT_CORES = 6
#: Half an hour. A chip case at this size solves in minutes; anything near this is wrong.
DEFAULT_TIMEOUT_S = 1800.0

_cache: dict[str, Any] = {}


class FoamError(RuntimeError):
    """Anything that stopped an OpenFOAM run. Always carries enough to act on."""


class FoamNotInstalled(FoamError):
    """No OpenFOAM on this machine. The message is the install command."""


class FoamRunFailed(FoamError):
    """A command ran and failed. Carries the result, including the log tail."""

    def __init__(self, message: str, result: "RunResult" | None = None):
        super().__init__(message)
        self.result = result


class FoamTimeout(FoamRunFailed):
    """A command was still running at `timeout_s` and was killed."""


# --- finding the installation ------------------------------------------------

@dataclass(frozen=True)
class Install:
    """Where OpenFOAM is and what it is, recorded in every run manifest."""

    launcher: Path
    version: str
    api: str = ""
    build: str = ""
    source: str = ""
    mpirun: str | None = None

    def to_dict(self) -> dict:
        return {
            "launcher": self.launcher.as_posix(),
            "version": self.version,
            "api": self.api,
            "build": self.build,
            "source": self.source,
            "mpirun": self.mpirun,
            "install_command": INSTALL_COMMAND,
        }


def _candidate_launchers() -> list[tuple[Path, str]]:
    """Every plausible launcher on this machine, best first: (path, how we found it)."""
    out: list[tuple[Path, str]] = []
    override = os.environ.get("FOAM_LAUNCHER")
    if override:
        out.append((Path(override), "FOAM_LAUNCHER"))
    for name in LAUNCHER_NAMES:
        found = shutil.which(name)
        if found:
            out.append((Path(found), f"{name} on PATH"))
    # Versioned wrappers, newest first: openfoam2606 beats openfoam2512.
    versioned: list[tuple[int, Path]] = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for entry in entries:
            match = VERSIONED_LAUNCHER.match(entry)
            if match:
                versioned.append((int(match.group(1)), Path(directory) / entry))
    for _, path in sorted(versioned, reverse=True):
        out.append((path, "versioned launcher on PATH"))
    # A hand-downloaded app bundle.
    import glob
    for pattern in APP_GLOBS:
        for app in sorted(glob.glob(pattern), reverse=True):
            launcher = Path(app) / APP_LAUNCHER
            if launcher.exists():
                out.append((launcher, "OpenFOAM.app bundle"))
    seen: set[str] = set()
    unique: list[tuple[Path, str]] = []
    for path, why in out:
        key = path.as_posix()
        if key not in seen:
            seen.add(key)
            unique.append((path, why))
    return unique


def _probe(launcher: Path) -> dict[str, str] | None:
    """Ask a launcher what it is. Returns None when it is not a working OpenFOAM."""
    try:
        proc = subprocess.run(
            [str(launcher), "-c",
             'echo "VERSION=$WM_PROJECT_VERSION"; echo "API=$FOAM_API"; '
             'echo "PROJECT=$WM_PROJECT"; echo "DIR=$WM_PROJECT_DIR"; '
             'echo "MPIRUN=$(command -v mpirun)"; echo "TUTORIALS=$FOAM_TUTORIALS"; '
             'echo "LIBBIN=$FOAM_LIBBIN"; echo "APPBIN=$FOAM_APPBIN"; '
             'echo "ETC=$WM_PROJECT_DIR/etc"'],
            capture_output=True, text=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    found: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            if key.isupper():
                found[key] = value.strip()
    if not found.get("VERSION") or not found.get("DIR"):
        return None
    return found


def install(refresh: bool = False) -> Install:
    """The OpenFOAM this machine will use. Raises `FoamNotInstalled` when there is none.

    Probed once and cached: starting a session mounts a disk image and costs a second or
    two, which is nothing once but a lot if every dictionary write asks.
    """
    if refresh:
        _cache.pop("install", None)
        _cache.pop("probe", None)
    if "install" in _cache:
        found = _cache["install"]
        if isinstance(found, FoamNotInstalled):
            raise found
        return found
    for launcher, why in _candidate_launchers():
        probed = _probe(launcher)
        if probed is None:
            continue
        got = Install(
            launcher=launcher,
            version=probed.get("VERSION", ""),
            api=probed.get("API", ""),
            build=probed.get("PROJECT", ""),
            source=why,
            mpirun=probed.get("MPIRUN") or None,
        )
        _cache["install"] = got
        _cache["probe"] = probed
        return got
    error = FoamNotInstalled(why_not())
    _cache["install"] = error
    raise error


def why_not() -> str:
    """Why there is no OpenFOAM, and the one command that fixes it."""
    tried = ", ".join(p.as_posix() for p, _ in _candidate_launchers()) or "nothing on PATH"
    return (
        f"OpenFOAM is not installed (or not working): tried {tried}. "
        f"assay2chip/foam/README.md pins ESI OpenCFD OpenFOAM, native on Apple Silicon, "
        f"no Docker:\n\n    {INSTALL_COMMAND}\n\n"
        f"({PROJECT_URL}). Then `openfoam -c 'simpleFoam -help'` must work. "
        f"Set FOAM_LAUNCHER to point at a launcher somewhere else."
    )


def available() -> bool:
    """True when a working OpenFOAM was found. Never raises - tests skip on this."""
    try:
        install()
        return True
    except FoamNotInstalled:
        return False


def version() -> str | None:
    """The OpenFOAM version string, e.g. '2606', or None when none is installed."""
    try:
        return install().version
    except FoamNotInstalled:
        return None


def foam_env(refresh: bool = False) -> dict[str, str]:
    """`WM_PROJECT_DIR`, `FOAM_TUTORIALS` and friends, out of a probe. {} with no install."""
    try:
        install(refresh=refresh)
    except FoamNotInstalled:
        return {}
    return dict(_cache.get("probe", {}))


def tutorials_dir() -> Path | None:
    """`$FOAM_TUTORIALS`, which the lid-driven cavity smoke test copies from."""
    path = foam_env().get("TUTORIALS")
    return Path(path) if path and Path(path).is_dir() else None


def has_solver(name: str) -> bool:
    """Is this solver or utility in the installation? `has_solver('simpleFoam')`."""
    key = f"solver:{name}"
    if key not in _cache:
        try:
            proc = _session([f"command -v {name} >/dev/null 2>&1 && echo YES || echo NO"],
                            timeout_s=120)
            _cache[key] = "YES" in (proc.stdout or "")
        except FoamError:
            _cache[key] = False
    return bool(_cache[key])


#: Where function objects live. `age`, `wallShearStress`, `fieldMinMax` and
#: `surfaceFieldValue` are all in the field library; the others are here so a sibling can
#: ask about a Lagrangian or solver function object without editing this module.
FUNCTION_OBJECT_LIBS = (
    "libfieldFunctionObjects",
    "libutilityFunctionObjects",
    "libsolverFunctionObjects",
    "liblagrangianFunctionObjects",
)
#: Shared-library suffixes, macOS first.
LIB_SUFFIXES = (".dylib", ".so")


def _function_object_libraries() -> list[str]:
    """Full paths to the function-object libraries that actually exist.

    Resolved in PYTHON, from `$FOAM_LIBBIN`, and never by shell globbing: a `lib*` glob
    inside the session's bash SEGFAULTS on the app's mounted case-sensitive volume
    (reproducible on v2606), which would turn a capability question into a crash.
    """
    libbin = foam_env().get("LIBBIN")
    if not libbin or not Path(libbin).is_dir():
        return []
    out: list[str] = []
    for lib in FUNCTION_OBJECT_LIBS:
        for suffix in LIB_SUFFIXES:
            path = Path(libbin) / f"{lib}{suffix}"
            if path.is_file():
                out.append(path.as_posix())
                break
    return out


def has_function_object(name: str) -> bool:
    """Does this OpenFOAM ship the named function object? `has_function_object('age')`.

    Asked rather than assumed. `age` in particular arrived at different versions in the
    ESI and Foundation lines, and a missing function object is a runtime failure HALFWAY
    THROUGH a solve - after the expensive part. `case.standard_functions()` asks before
    including it.

    The answer comes from the library's own exported symbols (demangled, so
    `functionObjects::fieldValues::surfaceFieldValue` matches `surfaceFieldValue` even
    though it sits in a nested namespace). That is the registration table itself rather
    than a guess from the version number or from the `etc/caseDicts` convenience configs,
    which do not list everything - `age` has no caseDicts entry in v2606 but the function
    object is there.
    """
    key = f"fo:{name}"
    if key in _cache:
        return bool(_cache[key])
    if not re.match(r"^\w+$", name):
        raise FoamError(f"{name!r} is not a function object name")
    libs = _function_object_libraries()
    found = False
    if libs:
        try:
            proc = _session(
                ["{ " + "; ".join(f"nm -gU {_q(lib)}" for lib in libs) + "; } 2>/dev/null"
                 " | c++filt 2>/dev/null"
                 f" | grep -qE 'functionObjects::([A-Za-z_]+::)*{name}([^A-Za-z_]|$)'"
                 " && echo FOUND || echo MISSING"],
                timeout_s=300,
            )
            found = "FOUND" in (proc.stdout or "")
        except FoamError:
            found = False
    if not found:
        # Fall back to the caseDicts tree: a stripped library has no symbols to read.
        etc = foam_env().get("DIR")
        if etc:
            found = bool(list(Path(etc, "etc", "caseDicts", "postProcessing").rglob(f"{name}*")))
    _cache[key] = found
    return found


# --- running -----------------------------------------------------------------

def default_cores() -> int:
    """How many cores a parallel run uses by default: 6 of this machine's 8."""
    override = os.environ.get("FOAM_CORES")
    if override:
        try:
            return max(1, int(override))
        except ValueError:
            raise FoamError(f"FOAM_CORES={override!r} is not an integer")
    return max(1, min(DEFAULT_CORES, os.cpu_count() or DEFAULT_CORES))


def _session(
    shell_commands: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Run shell inside an OpenFOAM session. The one place `subprocess` is called."""
    got = install()
    full_env = dict(os.environ)
    if env:
        full_env.update({str(k): str(v) for k, v in env.items()})
    script = "set -e\n" + "\n".join(shell_commands)
    try:
        return subprocess.run(
            [str(got.launcher), "-c", script],
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=full_env,
        )
    except subprocess.TimeoutExpired as exc:
        raise FoamTimeout(
            f"OpenFOAM session timed out after {timeout_s:g} s running:\n  "
            + "\n  ".join(shell_commands)
        ) from exc


def _decomposed_subdomains(case_dir: Path) -> int | None:
    """`numberOfSubdomains` out of system/decomposeParDict, without parsing OpenFOAM."""
    path = Path(case_dir) / "system" / "decomposeParDict"
    if not path.exists():
        return None
    match = re.search(r"^\s*numberOfSubdomains\s+(\d+)\s*;", path.read_text(encoding="utf-8"), re.M)
    return int(match.group(1)) if match else None


# --- log parsing -------------------------------------------------------------

#: `smoothSolver:  Solving for Ux, Initial residual = 1e-3, Final residual = 1e-9, No Iterations 4`
RESIDUAL_RE = re.compile(
    r"Solving for (\w+), Initial residual = ([0-9eE.+-]+), "
    r"Final residual = ([0-9eE.+-]+), No Iterations (\d+)"
)
#: `time step continuity errors : sum local = 1e-12, global = -1e-16, cumulative = -1e-15`
CONTINUITY_RE = re.compile(
    r"continuity errors : sum local = ([0-9eE.+-]+), global = ([0-9eE.+-]+), "
    r"cumulative = ([0-9eE.+-]+)"
)
COURANT_RE = re.compile(r"Courant Number mean: ([0-9eE.+-]+) max: ([0-9eE.+-]+)")
TIME_RE = re.compile(r"^Time = ([0-9eE.+-]+)\s*$", re.M)
EXEC_TIME_RE = re.compile(r"ExecutionTime = ([0-9.eE+-]+) s  ClockTime = ([0-9.eE+-]+) s")
CONVERGED_RE = re.compile(r"SIMPLE solution converged in (\d+) iterations")
END_RE = re.compile(r"^End\s*$", re.M)
#: A FOAM FATAL block runs from the `-->` to `FOAM exiting`, blank lines and all. Stopping
#: at the first blank line throws away the `file: 0/U/.../volumetricFlowRate at line 25`
#: line, which is the single most useful thing OpenFOAM ever prints.
FATAL_RE = re.compile(
    r"-->\s*FOAM FATAL (?:ERROR|IO ERROR).*?"
    r"(?=FOAM exiting|FOAM parallel run exiting|-->\s*FOAM FATAL|\Z)", re.S
)
WARNING_RE = re.compile(r"-->\s*FOAM Warning\s*:\s*\n?(.*?)(?=\n\s*\n|\Z)", re.S)
#: checkMesh
MESH_FAIL_RE = re.compile(r"\*\*\*(.*)")
MESH_OK_RE = re.compile(r"^Mesh OK\.\s*$", re.M)


def read_log(path: Path | str) -> str:
    """An OpenFOAM log as text. NUL bytes stripped - a parallel log is full of them.

    Open MPI interleaves rank output and pads it, so `log.simpleFoam` from a parallel run
    is technically a binary file. Left in, the NULs make `grep` give up and can break a
    regex across the fatal-error block that explains the failure.
    """
    path = Path(path)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").replace("\x00", "")


def _float(text: str) -> float:
    try:
        return float(text)
    except ValueError:
        return float("nan")


@dataclass
class LogSummary:
    """Everything worth knowing that an OpenFOAM log says.

    Residuals are per FIELD and per ITERATION, kept as the first and last value seen plus
    the worst-to-best ratio, because "did it fall" is the question and a single final
    number cannot answer it.
    """

    iterations: int = 0
    times: list[float] = field(default_factory=list)
    initial_residuals: dict[str, float] = field(default_factory=dict)
    final_residuals: dict[str, float] = field(default_factory=dict)
    residual_drop: dict[str, float] = field(default_factory=dict)
    continuity_local: float | None = None
    continuity_global: float | None = None
    continuity_cumulative: float | None = None
    continuity_growing: bool = False
    courant_max: float | None = None
    execution_time_s: float | None = None
    clock_time_s: float | None = None
    simple_converged_at: int | None = None
    reached_end: bool = False
    fatal: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    mesh_ok: bool | None = None
    mesh_failures: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "iterations": self.iterations,
            "initial_residuals": self.initial_residuals,
            "final_residuals": self.final_residuals,
            "residual_drop": self.residual_drop,
            "continuity": {
                "local": self.continuity_local,
                "global": self.continuity_global,
                "cumulative": self.continuity_cumulative,
                "growing": self.continuity_growing,
            },
            "courant_max": self.courant_max,
            "execution_time_s": self.execution_time_s,
            "clock_time_s": self.clock_time_s,
            "simple_converged_at": self.simple_converged_at,
            "reached_end": self.reached_end,
            "fatal": self.fatal,
            "warnings": self.warnings[:20],
            "mesh_ok": self.mesh_ok,
            "mesh_failures": self.mesh_failures,
        }


def parse_log(text: str) -> LogSummary:
    """Read an OpenFOAM log. Works on any solver's output and on checkMesh's."""
    out = LogSummary()
    first_seen: dict[str, float] = {}
    worst: dict[str, float] = {}
    for field_name, initial, _final, _iters in RESIDUAL_RE.findall(text):
        value = _float(initial)
        # Ux/Uy/Uz are one equation; report U as the worst of the three, which is what
        # residualControl's `U` entry means too.
        key = "U" if field_name in ("Ux", "Uy", "Uz") else field_name
        if key not in first_seen:
            first_seen[key] = value
            worst[key] = value
        out.final_residuals[key] = value
        worst[key] = max(worst[key], value)
    out.initial_residuals = first_seen
    for key, last in out.final_residuals.items():
        start = first_seen.get(key, last)
        out.residual_drop[key] = (start / last) if last > 0 else float("inf")

    continuity = CONTINUITY_RE.findall(text)
    if continuity:
        local, glob, cumulative = continuity[-1]
        out.continuity_local = _float(local)
        out.continuity_global = _float(glob)
        out.continuity_cumulative = _float(cumulative)
        if len(continuity) > 3:
            early = abs(_float(continuity[len(continuity) // 4][2]))
            late = abs(out.continuity_cumulative)
            # A cumulative error that keeps growing by orders of magnitude means mass is
            # being created. Slow linear drift is normal and is not flagged.
            out.continuity_growing = bool(late > max(early * 100.0, 1e-9) and late > 1e-9)

    courants = COURANT_RE.findall(text)
    if courants:
        out.courant_max = max(_float(c[1]) for c in courants)

    out.times = [_float(t) for t in TIME_RE.findall(text)]
    out.iterations = len(out.times)

    exec_times = EXEC_TIME_RE.findall(text)
    if exec_times:
        out.execution_time_s = _float(exec_times[-1][0])
        out.clock_time_s = _float(exec_times[-1][1])

    converged = CONVERGED_RE.search(text)
    if converged:
        out.simple_converged_at = int(converged.group(1))
    out.reached_end = bool(END_RE.search(text))
    out.fatal = [m.strip() for m in FATAL_RE.findall(text)]
    out.warnings = [m.strip().splitlines()[0] for m in WARNING_RE.findall(text) if m.strip()]

    if "Checking geometry" in text or "Mesh stats" in text:
        out.mesh_ok = bool(MESH_OK_RE.search(text))
        out.mesh_failures = [m.strip() for m in MESH_FAIL_RE.findall(text)]
    return out


#: Verdicts, in the order they are tested. Only `converged` and `mesh_ok` are good news.
VERDICTS = (
    "failed", "diverged", "mass_not_conserved", "courant_too_high",
    "iteration_limit", "converged", "no_iterations", "mesh_ok", "mesh_failed", "ok",
)


#: The fields `residualControl` actually governs. A function object that solves its own
#: equation (`age`) reports residuals into the same log but is NOT part of the momentum
#: and pressure convergence, and judging the run on its residual would call a perfectly
#: converged flow unconverged.
MOMENTUM_FIELDS = ("p", "U")


def convergence(
    summary: LogSummary,
    *,
    steady: bool = True,
    returncode: int = 0,
    residual_target: float = 1e-5,
    fields: Sequence[str] = MOMENTUM_FIELDS,
) -> tuple[bool | None, str]:
    """(converged, verdict) for one log. None means "this command does not converge".

    `blockMesh` and `decomposePar` do not iterate, so they get `converged=None` and a
    verdict of `ok` or `mesh_ok`; only a solver can return True or False. The distinction
    matters because a caller that treats `None` as `False` would reject every mesh.
    """
    judged = {k: v for k, v in summary.final_residuals.items() if k in fields} or \
        summary.final_residuals
    if returncode != 0 or summary.fatal:
        return False, "failed"
    if any(r != r or r == float("inf") or r > 1e3 for r in judged.values()):
        return False, "diverged"
    if summary.mesh_ok is not None:
        return (None, "mesh_ok") if summary.mesh_ok and not summary.mesh_failures else (False, "mesh_failed")
    if not summary.iterations:
        return None, "ok" if summary.reached_end else "no_iterations"
    if summary.continuity_growing:
        return False, "mass_not_conserved"
    if summary.courant_max is not None and summary.courant_max > 1.0 and not steady:
        return False, "courant_too_high"
    if steady:
        if summary.simple_converged_at:
            return True, "converged"
        worst = max(judged.values(), default=1.0)
        if worst <= residual_target:
            return True, "converged"
        return False, "iteration_limit"
    # Transient: there is no residualControl to hit. A run that reached `End` with a
    # Courant number under 1 and no growing continuity error did what it was asked.
    return (True, "converged") if summary.reached_end else (False, "iteration_limit")


VERDICT_MEANING = {
    "converged": "residualControl met (steady) or the run reached endTime cleanly (transient)",
    "iteration_limit": "the run ran out of iterations with residuals still above target: "
                       "NOT converged - raise endTime, relax less, or check the mesh",
    "diverged": "a residual went to infinity or NaN: the case is unstable",
    "mass_not_conserved": "the cumulative continuity error grew by orders of magnitude",
    "courant_too_high": "max Courant number above 1 on a transient run: reduce deltaT",
    "failed": "OpenFOAM exited non-zero or printed a FOAM FATAL ERROR",
    "mesh_ok": "checkMesh passed",
    "mesh_failed": "checkMesh reported a *** failure",
    "ok": "the command finished; it does not iterate, so there is nothing to converge",
    "no_iterations": "the command produced no Time steps and no End",
}


# --- the result --------------------------------------------------------------

@dataclass
class RunResult:
    """One OpenFOAM command, start to finish, with everything a caller might want."""

    command: str
    case_dir: Path
    returncode: int
    ok: bool
    log_path: Path
    wall_time_s: float
    foam_version: str
    launcher: str
    parallel: int | None = None
    log: LogSummary = field(default_factory=LogSummary)
    converged: bool | None = None
    verdict: str = "ok"
    steps: list[dict] = field(default_factory=list)
    stderr_tail: str = ""

    @property
    def iterations(self) -> int:
        return self.log.iterations

    @property
    def final_residuals(self) -> dict[str, float]:
        return self.log.final_residuals

    @property
    def courant_max(self) -> float | None:
        return self.log.courant_max

    @property
    def fatal(self) -> list[str]:
        return self.log.fatal

    def log_text(self) -> str:
        return read_log(self.log_path)

    def tail(self, lines: int = 40) -> str:
        return "\n".join(self.log_text().splitlines()[-lines:])

    def manifest(self) -> dict:
        """The record that goes in the run summary JSON. Version and launcher always."""
        return {
            "command": self.command,
            "case_dir": self.case_dir.as_posix(),
            "ok": self.ok,
            "returncode": self.returncode,
            "converged": self.converged,
            "verdict": self.verdict,
            "verdict_means": VERDICT_MEANING.get(self.verdict, ""),
            "wall_time_s": round(self.wall_time_s, 3),
            "log": self.log_path.as_posix(),
            "parallel": self.parallel,
            "openfoam": {
                "version": self.foam_version,
                "launcher": self.launcher,
                "install_command": INSTALL_COMMAND,
            },
            "steps": self.steps,
            **self.log.to_dict(),
        }

    def to_dict(self) -> dict:
        return self.manifest()


def blame(result: RunResult) -> tuple[str, Path, LogSummary]:
    """Which STEP of a run actually failed, and its log.

    A parallel run is three commands - `decomposePar`, the solver, `reconstructPar` - and
    when the first one dies the others never run, so the solver's log does not exist and
    reporting "simpleFoam failed" is a lie that costs ten minutes of looking in the wrong
    file. (It cost exactly that once: a `volumetricFlowRate` with a dimensions bracket
    killed decomposePar, and the error pointed at simpleFoam.)

    Returns the first step whose log holds a FOAM FATAL block, else the first step whose
    log is missing entirely, else the command itself.
    """
    if not result.parallel:
        return result.command, result.log_path, result.log
    ordered = [
        ("decomposePar", result.case_dir / "log.decomposePar"),
        (result.command, result.log_path),
        ("reconstructPar", result.case_dir / "log.reconstructPar"),
    ]
    summaries = [(name, path, parse_log(read_log(path))) for name, path in ordered]
    for name, path, summary in summaries:
        if summary.fatal:
            return name, path, summary
    for index, (name, path, summary) in enumerate(summaries):
        if not path.exists() and index:
            previous_name, previous_path, previous = summaries[index - 1]
            return previous_name, previous_path, previous
    return result.command, result.log_path, result.log


def _fail_message(command: str, result: RunResult) -> str:
    step, log_path, summary = blame(result)
    where = f"parallel x{result.parallel}" if result.parallel else "serial"
    head = (
        f"OpenFOAM `{command}` failed in {result.case_dir} "
        f"(exit {result.returncode}, {result.wall_time_s:.1f} s, {where}, "
        f"OpenFOAM {result.foam_version})."
    )
    if step != command:
        head += (
            f"\nThe step that actually failed is `{step}`, not `{command}`: a parallel run "
            f"is decomposePar -> {command} -> reconstructPar and it stopped at the first."
        )
    parts = [head]
    if summary.fatal:
        parts.append("\nFOAM FATAL ERROR:\n" + "\n".join(summary.fatal[:2]))
    if summary.mesh_failures:
        parts.append("\ncheckMesh failures:\n  " + "\n  ".join(summary.mesh_failures[:10]))
    if result.stderr_tail.strip():
        parts.append("\nstderr:\n" + result.stderr_tail.strip()[-2000:])
    tail = "\n".join(read_log(log_path).splitlines()[-30:])
    if tail.strip():
        parts.append(f"\nlast 30 lines of {log_path}:\n{tail}")
    elif not log_path.exists():
        parts.append(f"\n{log_path} was never written: `{step}` did not start.")
    return "\n".join(parts)


def run(
    case_dir: Path | str,
    command: str,
    *,
    parallel: bool | int | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    log_name: str | None = None,
    args: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
    check: bool = True,
    steady: bool | None = None,
    reconstruct: bool = True,
) -> RunResult:
    """Run one OpenFOAM command in `case_dir`. The only entry point anything else uses.

    `command` is the executable name - `blockMesh`, `checkMesh`, `simpleFoam`,
    `postProcess`, `foamToVTK` - with any extra flags in `args`.

    `parallel`:
      None / False  serial.
      True          `decomposePar -force`, `mpirun -np N ... -parallel`, `reconstructPar`,
                    with N from system/decomposeParDict.
      an int        the same, and decomposeParDict must already say that number.

    The log goes to `<case_dir>/log.<command>` (override with `log_name`) and is parsed
    whatever the exit code, so a failure still reports its residuals.

    Raises `FoamRunFailed` on a non-zero exit when `check` (the default). `check=False`
    returns the result instead - for a caller that wants to look at the log first.
    """
    case_dir = Path(case_dir)
    if not case_dir.is_dir():
        raise FoamError(f"no case directory at {case_dir}: build it with case.build_case() first")
    got = install()

    log_name = log_name or f"log.{command}"
    log_path = case_dir / log_name
    extra = " ".join(str(a) for a in args)
    steady = (command in ("simpleFoam", "potentialFoam")) if steady is None else steady

    n_cores: int | None = None
    if parallel is True:
        n_cores = _decomposed_subdomains(case_dir) or default_cores()
    elif isinstance(parallel, int) and not isinstance(parallel, bool):
        n_cores = int(parallel)
        stated = _decomposed_subdomains(case_dir)
        if stated is not None and stated != n_cores:
            raise FoamError(
                f"asked for {n_cores} cores but {case_dir}/system/decomposeParDict says "
                f"numberOfSubdomains {stated}. They have to agree: rebuild the case with "
                f"case.build_case(n_cores={n_cores}) or call run(..., parallel=True) to "
                f"use the {stated} the case already asks for."
            )
    if n_cores is not None and n_cores < 2:
        n_cores = None  # one subdomain is serial with extra steps

    steps: list[dict] = []
    shell: list[str] = [f"cd {_q(case_dir)}"]
    if n_cores:
        if _decomposed_subdomains(case_dir) is None:
            raise FoamError(
                f"{case_dir}/system/decomposeParDict is missing, so a parallel run has no "
                f"domain decomposition. case.build_case(n_cores={n_cores}) writes it."
            )
        if not got.mpirun:
            raise FoamError(
                f"OpenFOAM {got.version} at {got.launcher} has no mpirun in its "
                f"environment, so `parallel` cannot work. Run serially (parallel=None) or "
                f"reinstall with `{INSTALL_COMMAND}`."
            )
        shell.append(f"decomposePar -force > {_q(case_dir / 'log.decomposePar')} 2>&1")
        shell.append(
            f"mpirun -np {n_cores} {command} -parallel {extra} > {_q(log_path)} 2>&1"
        )
        if reconstruct:
            shell.append(f"reconstructPar > {_q(case_dir / 'log.reconstructPar')} 2>&1")
    else:
        shell.append(f"{command} {extra} > {_q(log_path)} 2>&1")

    started = time.time()
    try:
        proc = _session(shell, cwd=case_dir, timeout_s=timeout_s, env=env)
        returncode, stderr = proc.returncode, proc.stderr or ""
    except FoamTimeout as exc:
        elapsed = time.time() - started
        summary = parse_log(read_log(log_path))
        result = RunResult(
            command=command, case_dir=case_dir, returncode=124, ok=False, log_path=log_path,
            wall_time_s=elapsed, foam_version=got.version, launcher=got.launcher.as_posix(),
            parallel=n_cores, log=summary, converged=False, verdict="failed",
            stderr_tail=str(exc),
        )
        raise FoamTimeout(
            f"OpenFOAM `{command}` in {case_dir} was still running after {timeout_s:g} s "
            f"and was killed. It reached iteration {summary.iterations}"
            + (f" with residuals {summary.final_residuals}" if summary.final_residuals else "")
            + f". Raise timeout_s, coarsen the mesh, or run it in parallel.",
            result,
        ) from exc
    elapsed = time.time() - started

    summary = parse_log(read_log(log_path))
    converged, verdict = convergence(summary, steady=steady, returncode=returncode)
    if n_cores:
        steps = [
            {"step": "decomposePar", "cores": n_cores,
             "log": (case_dir / "log.decomposePar").as_posix()},
            {"step": command, "launcher": f"mpirun -np {n_cores}", "log": log_path.as_posix()},
        ]
        if reconstruct:
            steps.append({"step": "reconstructPar",
                          "log": (case_dir / "log.reconstructPar").as_posix()})

    result = RunResult(
        command=command,
        case_dir=case_dir,
        returncode=returncode,
        ok=(returncode == 0 and not summary.fatal),
        log_path=log_path,
        wall_time_s=elapsed,
        foam_version=got.version,
        launcher=got.launcher.as_posix(),
        parallel=n_cores,
        log=summary,
        converged=converged,
        verdict=verdict,
        steps=steps,
        stderr_tail=stderr[-4000:],
    )
    if result.ok and n_cores:
        # decomposePar and reconstructPar have their own logs. A fatal in either is a
        # failed run even when the solver's own log looks clean.
        step, _, step_summary = blame(result)
        if step_summary.fatal or (step != command and not log_path.exists()):
            result.ok = False
            result.converged = False
            result.verdict = "failed"
    if check and not result.ok:
        raise FoamRunFailed(_fail_message(command, result), result)
    return result


def run_all(
    case_dir: Path | str,
    commands: Iterable[str | tuple[str, dict]],
    **common: Any,
) -> list[RunResult]:
    """Run commands in order, stopping at the first failure. `blockMesh`, `checkMesh`, ...

    Each entry is a command name or `(name, kwargs)` whose kwargs override `common`.
    """
    out: list[RunResult] = []
    for entry in commands:
        name, kwargs = entry if isinstance(entry, tuple) else (entry, {})
        merged = dict(common)
        merged.update(kwargs)
        out.append(run(case_dir, name, **merged))
    return out


def clean(case_dir: Path | str, *, keep_zero: bool = True) -> list[str]:
    """Delete solver OUTPUT from a case: time directories, processor*, logs, VTK.

    Leaves `0/`, `constant/` and `system/` - the inputs - alone, so a case can be re-run
    from scratch without being rebuilt. Returns what it removed.
    """
    case_dir = Path(case_dir)
    removed: list[str] = []
    for child in sorted(case_dir.iterdir()):
        name = child.name
        drop = (
            name.startswith("processor")
            or name.startswith("log.")
            or name in ("VTK", "postProcessing")
            or (_is_time_name(name) and not (keep_zero and name == "0"))
        )
        if drop:
            shutil.rmtree(child) if child.is_dir() else child.unlink()
            removed.append(name)
    return removed


def _is_time_name(name: str) -> bool:
    try:
        float(name)
    except ValueError:
        return False
    return True


def _q(path: Path | str) -> str:
    """Quote a path for the shell the session runs."""
    return "'" + str(path).replace("'", "'\\''") + "'"


# --- the smoke test ----------------------------------------------------------

def cavity_smoke_test(work_dir: Path | str, *, timeout_s: float = 600.0) -> dict:
    """Copy OpenFOAM's own lid-driven cavity tutorial and run it end to end THROUGH `run`.

    The point is not the cavity - it is that this module's launcher, environment, logging,
    parsing and verdict all work on a case nobody here wrote. The cavity has an analytic
    character everyone knows: at Re 10 the lid drags a single stable vortex and icoFoam
    converges in well under a second.

    Returns a dict for the manifest; raises with the usual loud message on failure.
    """
    tutorials = tutorials_dir()
    if tutorials is None:
        raise FoamError(
            f"OpenFOAM {version()} has no $FOAM_TUTORIALS directory, so the cavity smoke "
            f"test cannot run. The gerlero cask ships them; check `openfoam -c 'echo "
            f"$FOAM_TUTORIALS'`."
        )
    source = tutorials / "incompressible" / "icoFoam" / "cavity" / "cavity"
    if not source.is_dir():
        raise FoamError(f"the lid-driven cavity tutorial is not at {source}")
    work_dir = Path(work_dir)
    case_dir = work_dir / "cavity"
    if case_dir.exists():
        shutil.rmtree(case_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, case_dir)

    results = run_all(case_dir, ["blockMesh", "checkMesh", "icoFoam"], timeout_s=timeout_s)
    mesh, check_mesh, solve = results
    return {
        "case_dir": case_dir.as_posix(),
        "source": source.as_posix(),
        "openfoam": install().to_dict(),
        "blockMesh": mesh.manifest(),
        "checkMesh": check_mesh.manifest(),
        "icoFoam": solve.manifest(),
        "mesh_ok": check_mesh.log.mesh_ok,
        "iterations": solve.iterations,
        "final_residuals": solve.final_residuals,
        "wall_time_s": round(sum(r.wall_time_s for r in results), 3),
    }


def main(argv: Sequence[str]) -> int:
    """python -m assay2chip.foam.runner [--cavity DIR] - what is installed, and does it run."""
    import json

    if not available():
        print(why_not(), file=sys.stderr)
        return 1
    got = install()
    report = {
        "openfoam": got.to_dict(),
        "default_cores": default_cores(),
        "tutorials": tutorials_dir().as_posix() if tutorials_dir() else None,
        "solvers": {name: has_solver(name) for name in
                    ("simpleFoam", "pimpleFoam", "interFoam", "scalarTransportFoam",
                     "icoUncoupledKinematicParcelFoam", "blockMesh", "checkMesh",
                     "decomposePar", "reconstructPar", "foamToVTK", "postProcess")},
        "function_objects": {name: has_function_object(name) for name in
                             ("age", "wallShearStress", "surfaceFieldValue", "fieldMinMax")},
    }
    if "--cavity" in argv:
        where = argv[argv.index("--cavity") + 1]
        report["cavity"] = cavity_smoke_test(where)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

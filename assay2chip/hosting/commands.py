"""What each job kind runs, and with which environment.

A run is split into SEGMENTS of assay2chip/run.py, each one job:

    ingest     s0          the upload, URL or DOI -> raw.json
    extract    s1          the language-model call (when A2C_LLM=real): its own job so a
                           transient API failure is retried without redoing anything else
    plan       s2-s4       unit ops, triage, partition
    design     s5-s10      group, build, route, protocol, verify, reduced-order CFD.
                           ONE job on purpose: S10 can send a chip back to S6/S7, and
                           run.py only honours that loop inside a single invocation
    foam       per chip x protocol step: python -m assay2chip.foam.run (which launches
                           OpenFOAM only through assay2chip/foam/runner.py)
    fabricate  s11         STL, mould, DXF, renders (in parallel with the foam fan-out)
    collect    -           in-process: gathers the foam results into foam/summary.json
    report     s12

Segments run with --keep-going, so a stage error is recorded in the manifest and the run
carries on to the report (job status `done_with_errors`, never retried: it is
deterministic). A crash, a timeout or an unexpected exit code IS retried.

A2C_COMMAND_OVERRIDES may point at a JSON file {kind: [argv template]} (placeholders
{python} {run_id} {paper} {llm} {chip} {step} {out} {from} {to}); the smoke test uses it to
stand fast fakes in for the slow segments.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

SEGMENTS = [
    ("ingest", "s0", "s0"),
    ("extract", "s1", "s1"),
    ("plan", "s2", "s4"),
    ("design", "s5", "s10"),
    ("fabricate", "s11", "s11"),
    ("report", "s12", "s12"),
]
SEGMENT = {name: (a, b) for name, a, b in SEGMENTS}
CHAIN = ["ingest", "extract", "plan", "design"]

MAX_ATTEMPTS = {"ingest": 2, "extract": 3, "plan": 2, "design": 2, "foam": 2,
                "fabricate": 2, "collect": 2, "report": 2}

#: Inherited from the launching environment. Everything else is dropped, so a stage never
#: sees a secret it has no use for.
PASS_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TERM",
            "FOAM_LAUNCHER", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE")
#: Only for the kinds in A2C_LLM_KINDS, and only when the run's LLM mode is `real`.
LLM_ENV = ("ANTHROPIC_API_KEY", "ASSAY2CHIP_LLM_MODEL")


def foam_out(run_id: str, chip: str, step) -> str:
    return "assay2chip/work/%s/foam/jobs/%s/step_%s" % (run_id, chip, step)


def default_argv(kind: str, ctx: dict, settings) -> List[str]:
    if kind in SEGMENT:
        start, stop = SEGMENT[kind]
        argv = [ctx["python"], "assay2chip/run.py", "--id", ctx["run_id"], "--from", start,
                "--to", stop, "--llm", ctx["llm"], "--keep-going"]
        if start == "s0":
            argv += ["--paper", ctx["paper"]]
        return argv
    if kind == "foam":
        argv = [ctx["python"], "-m", "assay2chip.foam.run",
                "--chip", "chip/%s.json" % ctx["chip"],
                "--protocol", "protocols/%s.json" % ctx["chip"],
                "--step", str(ctx["step"]), "--run", ctx["run_id"], "--out", ctx["out"],
                "--resolution", "720p", "--no-glb"]
        if not settings.foam_video:
            argv.append("--no-video")
        if settings.foam_solve:
            argv.append("--solve")          # without it foam/run.py renders reduced-order
        return argv
    raise ValueError("job kind %r has no command" % kind)


def build_argv(kind: str, job: dict, run: dict, settings) -> List[str]:
    payload = job.get("payload") or {}
    options = run.get("options") or {}
    ctx = {
        "python": settings.python, "run_id": run["id"], "paper": run.get("paper") or "",
        "llm": options.get("llm") or settings.llm_mode,
        "chip": payload.get("chip", ""), "step": payload.get("step", ""),
        "out": payload.get("out", ""),
        "from": SEGMENT.get(kind, ("", ""))[0], "to": SEGMENT.get(kind, ("", ""))[1],
        "kind": kind,
    }
    if settings.command_overrides and settings.command_overrides.exists():
        table = json.loads(settings.command_overrides.read_text())
        if kind in table:
            return [str(part).format(**ctx) for part in table[kind]]
    return default_argv(kind, ctx, settings)


def build_env(kind: str, run: dict, settings, *, foam_cores: int,
              base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    base = dict(os.environ if base is None else base)
    env = {k: base[k] for k in PASS_ENV if k in base}
    env.update({k: v for k, v in base.items() if k.startswith("LC_") or
                k.startswith("A2C_TEST_")})
    # Homebrew's OpenFOAM and ffmpeg live here; a launch from a bare environment would
    # otherwise not find them.
    path = env.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin").split(os.pathsep)
    for extra in ("/opt/homebrew/bin", "/usr/local/bin"):
        if extra not in path and os.path.isdir(extra):
            path.append(extra)
    env["PATH"] = os.pathsep.join(path)
    llm = (run.get("options") or {}).get("llm") or settings.llm_mode
    env["ASSAY2CHIP_LLM"] = llm
    env["PYTHONUNBUFFERED"] = "1"
    env["MPLBACKEND"] = "Agg"
    if llm == "real" and kind in settings.llm_kinds:
        env.update({k: base[k] for k in LLM_ENV if k in base})
    if kind == "foam":
        env["FOAM_CORES"] = str(foam_cores)
        env["OMP_NUM_THREADS"] = "1"
        # Several cases at once must not all bind their ranks to cores 0..N-1.
        if (settings.foam_cap or 1) > 1:
            env["OMPI_MCA_hwloc_base_binding_policy"] = "none"
    else:
        env["OMP_NUM_THREADS"] = "1"
    return env


def timeout_for(kind: str, settings) -> int:
    if kind == "foam":
        return settings.foam_timeout_s
    if kind == "design":
        return settings.design_timeout_s
    return settings.light_timeout_s

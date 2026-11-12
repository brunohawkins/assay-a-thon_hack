"""Run profiles: `full` (the default, for real work) and `demo` (small enough to watch).

    ASSAY2CHIP_PROFILE=demo python assay2chip/run.py --paper ... --id ...
    python tools/demo_pipeline.py                  # sets demo for you

The demo profile is a SMALLER PROBLEM, not a fake one. Every stage runs its real code on
real geometry; what changes is how much of it there is:

  setting                full                       demo
  ---------------------  -------------------------  --------------------------------------
  routing grid (S7)      0.05 mm                    0.05 mm - the router refuses anything
                                                    coarser ("a coarse grid hides a thin
                                                    wall"), so the demo does NOT coarsen it
  rip-up passes (S7)     8                          4
  CFD steps per chip     4 + 2 worst cases          2 + 0 worst cases
  particles per class    24                         8
  CFD validation scope   every chip in chip/        the analytic benchmarks + module1_v1's
                                                    reference checks + THIS run's chips
  CFD figures            every solved step          the first solved step per chip
  LLM (S1/S5)            whatever --llm says        stub (offline, deterministic)

Underscore-prefixed, so assay2chip/run.py's stage discovery (s<N>_*.py) never sees it.
"""

from __future__ import annotations

import os

ENV = "ASSAY2CHIP_PROFILE"
FULL, DEMO = "full", "demo"

PROFILES: dict[str, dict] = {
    FULL: {"route_max_passes": None, "cfd_max_steps": None, "cfd_particles": None,
           "cfd_validation_scope": "repo", "cfd_figure_steps": None},
    DEMO: {"route_max_passes": 4, "cfd_max_steps": 2, "cfd_particles": 8,
           "cfd_validation_scope": "run", "cfd_figure_steps": 1},
}


def name() -> str:
    value = (os.environ.get(ENV) or FULL).strip().lower()
    if value not in PROFILES:
        raise ValueError(f"{ENV}={value!r} is not a profile: use one of {', '.join(PROFILES)}")
    return value


def get(key: str):
    """The current profile's value for `key`; None means 'the stage's own default'."""
    return PROFILES[name()][key]


def banner() -> str:
    n = name()
    if n == DEMO:
        return ("PROFILE: demo - a small protocol, 4 rip-up passes, 2 CFD steps per chip, "
                "8 particles per class, CFD validation scoped to this run's chips. "
                "Every stage runs for real.")
    return "PROFILE: full - full-fidelity defaults"

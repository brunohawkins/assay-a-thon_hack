"""Full 3D CFD with OpenFOAM, and a rendered end-to-end pipeline. See foam/README.md.

The 3D counterpart to `assay2chip/cfd/`: where the reduced Hele-Shaw model stops being
correct - Re above 1, real recirculation, two-phase priming, settling particles - this
package runs the real thing and renders it.

Two modules are the foundation everything else stands on, and they are the only ones that
talk to the outside world:

    runner    the ONLY place that knows how to launch OpenFOAM. available(), version(),
              run(case_dir, command, parallel=...), log parsing, a convergence verdict.
    case      OpenFOAM dictionaries out of Python dicts, and a complete laminar
              incompressible case for low Re from a per-patch boundary-condition map.

    from assay2chip.foam import runner, case

    if not runner.available():
        print(runner.why_not())                  # the install command, in one line

    fluid = case.fluid_for(37.0)                 # nu from the contracts, never guessed
    built = case.build_case(case_dir, fluid=fluid, patches={
        "p_in":  case.PatchBC("feed", q_m3_s=1.0 / 6e10),   # 1 uL/min in
        "p_out": case.PatchBC("vent"),                       # open to atmosphere
        "walls": case.PatchBC("wall"),
    }, n_cores=runner.default_cores())
    runner.run(case_dir, "blockMesh")
    result = runner.run(case_dir, "simpleFoam", parallel=True)
    result.converged, result.verdict, result.manifest()

The rest of the package - mesh, flow, transport, particles, vof, render, scene, run - is
built by the other agents on exactly this API. Nothing but `runner` imports `subprocess`;
nothing but `case` writes a dictionary.

WHAT IS INSTALLED HERE: ESI OpenCFD OpenFOAM v2606, native arm64, from the gerlero cask
(`brew install gerlero/openfoam/openfoam`), launcher `/opt/homebrew/bin/openfoam`. Its
laminar model file is `constant/turbulenceProperties` (NOT `momentumTransport`, which is
the Foundation spelling) and it DOES have the `age` function object. None of those three
facts is assumed anywhere: `case.laminar_model_file()` and
`runner.has_function_object()` ask the installation.

Rendering needs `requirements-foam.txt` (pyvista + vtk), which is NOT in requirements.txt
- the rest of the repo must run without either it or OpenFOAM. Importing this package
does neither, so `import assay2chip.foam` is always safe.
"""

from __future__ import annotations

__all__ = [
    "case",
    "runner",
    "available",
    "version",
    "why_not",
    "FoamError",
    "FoamNotInstalled",
    "FoamRunFailed",
    "CaseError",
    "PatchBC",
    "Fluid",
]


_FROM_RUNNER = ("available", "version", "why_not", "FoamError", "FoamNotInstalled",
                "FoamRunFailed")
_FROM_CASE = ("CaseError", "PatchBC", "Fluid")


def __getattr__(name: str):
    """Lazy re-exports: importing the package must never start an OpenFOAM session.

    `runner.install()` mounts the app's disk image, which costs a second or two. Doing
    that at import time would tax every test in the repo, including the ones that have
    nothing to do with OpenFOAM, so the names resolve on first use instead.

    `import_module` rather than `from . import runner`: the `from` form re-enters this
    very function while the submodule is still being set up, and recurses until Python
    gives up.
    """
    import importlib

    source = "runner" if name in _FROM_RUNNER or name == "runner" else (
        "case" if name in _FROM_CASE or name == "case" else None)
    if source is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = importlib.import_module(f"{__name__}.{source}")
    value = module if name in ("runner", "case") else getattr(module, name)
    globals()[name] = value  # resolve once, then it is a plain attribute
    return value

"""CFD for the whole chip set: does it actually flow? See assay2chip/cfd/README.md.

Stokes flow in shallow rectangular ducts and depth-averaged Hele-Shaw chambers, solved as
a resistance network in milliseconds, which is what makes the fix-and-re-run loop possible.

The shared shapes live in `model.py` and are re-exported here, so every module imports
them the same way and nothing else has to know where they live:

    from assay2chip.cfd import Mesh, Scenario, Field, Finding, PortState

    mesh = build_mesh("chip/module1_v1.json")       # mesh.py
    cold = mesh.at_viscosity(2.5e-3)                # same geometry, cold solvent

Modules, in pipeline order (one owner each, contracts in the README's table):

    model     these dataclasses, with to_json / from_json. No physics, no contracts.
    mesh      chip/<id>.json -> Mesh. Chambers rasterised, channels and channel-like
              blocks as 1D ducts, weirs as gap^3 slits, ports joined to what they open on.
    scenario  protocols/<id>.json + Mesh -> Scenario per step (boundary conditions).
    solve     Mesh + Scenario -> Field (p, Q, u, shear, Re, residual).
    transport residence time, washout, exchange fraction.
    particles settling vs transit, clog margin, weir escape.
    couple    per-chip Fields joined through tubing models.
    analyse   -> findings.json.      fix       -> fixes.json.
    validate  benchmarks that must pass before any finding is trusted.
    figures / report / run   SVG, report section, CLI.

`model` is imported eagerly because it is cheap and depends on nothing. `build_mesh` is
resolved lazily so importing the shapes never drags in the contracts or the mesher.
"""

from __future__ import annotations

from typing import Any

from .model import (  # noqa: F401
    DYN_CM2_PER_PA,
    EDGE_KINDS,
    FINDING_CLASSES,
    NODE_KINDS,
    PORT_STATE_TYPES,
    SCHEMA_VERSION,
    SEVERITIES,
    UL_MIN_TO_M3_S,
    M3_S_TO_UL_MIN,
    Edge,
    Field,
    Finding,
    Mesh,
    ModelError,
    Node,
    PortState,
    Residual,
    Scenario,
    findings_from_json,
    findings_to_json,
    slit_g_geom_m3,
)

__all__ = [
    "Mesh", "Node", "Edge", "Scenario", "PortState", "Field", "Residual", "Finding",
    "ModelError", "NODE_KINDS", "EDGE_KINDS", "PORT_STATE_TYPES", "SEVERITIES",
    "FINDING_CLASSES", "SCHEMA_VERSION", "UL_MIN_TO_M3_S", "M3_S_TO_UL_MIN",
    "DYN_CM2_PER_PA", "findings_to_json", "findings_from_json", "slit_g_geom_m3",
    "build_mesh", "mesh_chip",
]


def __getattr__(name: str) -> Any:
    """Lazy `build_mesh` / `mesh_chip`: see assay2chip/cfd/mesh.py."""
    if name in ("build_mesh", "mesh_chip"):
        from . import mesh as _mesh
        return getattr(_mesh, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

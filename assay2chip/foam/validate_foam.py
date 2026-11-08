"""assay2chip/foam/validate_foam.py - the evidence that the 3D solve is right.

    python assay2chip/foam/validate_foam.py                      # everything, table out
    python assay2chip/foam/validate_foam.py --duct-only
    python assay2chip/foam/validate_foam.py --chip module1_v1 --json
    from assay2chip.foam import validate_foam; report = validate_foam.run()

A CFD package nobody has checked is worse than no CFD package, and a 3D solve is far
easier to believe and far harder to check than the reduced one, so this file is where the
3D results earn their place. Three things, in order of how much they prove:

1. **THE DUCT, against the EXACT series solution.** A straight rectangular duct is the one
   geometry in this repo whose answer is known in closed form: the double-sine
   eigenfunction expansion of Poiseuille flow in a rectangle, which
   `assay2chip/cfd/validate.py` already implements independently
   (`duct_resistance_exact`, `wall_shear_max_exact`). If OpenFOAM cannot reproduce that,
   nothing else here means anything.

   Two details that are the difference between a real check and a flattering one:

   * **The entrance is subtracted, not ignored.** `flowRateInletVelocity` imposes a flat
     profile, so the first part of the duct carries an excess pressure drop that has
     nothing to do with the series solution. The check therefore runs the duct at TWO
     lengths and differences them: `(dP_long - dP_short) / (L_long - L_short)` is the
     fully developed gradient, free of the entrance by construction. The raw single-length
     dP is reported too, as a diagnostic, and it is expected to read slightly HIGH.
   * **Peak wall shear is measured away from the entrance.** The mesh is written as two
     blocks with two wall patches, `walls_entry` and `walls_dev`, so the peak on the
     developed wall can be read on its own. That is the number that tests the series'
     SHAPE - the 1.48x at w/h = 1.33 the repo audit measured - rather than just repeating
     the pressure test through a force balance.

2. **CROSS-VALIDATION against the reduced-order solver**, on every repo chip and protocol
   step both can run: pressure drop feed -> vent, and the outlet flow split. Where
   Re << 1 the two must agree within a few percent, and THAT AGREEMENT IS THE EVIDENCE
   THAT BOTH ARE RIGHT - two independent discretisations of two independent formulations
   of the same physics do not land on the same wrong number.

   Every disagreement is reported with the model believed correct and WHY. The causes
   this file knows how to tell apart, which are the ones the READMEs name:

     inertia             Re > 1 somewhere on the path. The reduced model is a Stokes
                         solver and says so; the 3D run is correct.
     side walls          a chamber or lane narrow enough (w/h below ~5) that
                         depth-averaging drops real side-wall drag. The reduced model
                         under-predicts resistance; the 3D run is correct.
     staircase           the hex mesh is too coarse to resolve the narrowest channel, or
                         the grid-convergence estimate for this chip is larger than the
                         disagreement. Then the 3D number carries the bigger error bar and
                         the REDUCED model is the one to believe - or neither, and the row
                         says inconclusive.
     weir slit           a 25 um crest with too few cells across it. Flow through a slit
                         goes as gap^3, so three cells is not a resolution, it is a guess;
                         the reduced model's analytic slit wins.
     unexplained         everything else, reported as unexplained rather than attributed
                         to a cause that happens to be nearby.

   NEITHER MODEL IS EVER TUNED TO MATCH THE OTHER. There is no fudge factor anywhere in
   this package and no constant in it was chosen by looking at a disagreement. A
   disagreement is a finding (`class: "model_error"`, `source: "openfoam"`), not an input.

3. **GRID SENSITIVITY** on one chip at two resolutions, so every number reported above
   carries a discretisation error bar: `flow.grid_sensitivity` turns the pair into a Grid
   Convergence Index at the formal second order with Roache's conservative two-grid safety
   factor. A cross-validation disagreement smaller than the GCI is not a disagreement, and
   the table says so instead of picking a winner.

Report shape (`schema: foam-validation/1`), written to
`assay2chip/work/foam/validation.json` and returned by `run()`:

    {"schema", "status": "pass|fail|incomplete", "passed", "trusted", "reason",
     "openfoam": {...}, "duct": {...}, "cross_validation": {"rows": [...], "summary": {}},
     "grid_sensitivity": {...}, "findings": [...], "contracts": {...}}

Everything that needs OpenFOAM is SKIPPED with a reason when it is absent, and the report
then says `incomplete` and `trusted: false` - never `pass`. The parts that do not need it
(the exact series solution, the reduced-order solve, the disagreement attribution rules)
run anywhere, which is what `tests/test_foam_flow.py` exercises.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.cfd import mesh as cfd_mesh  # noqa: E402
from assay2chip.cfd import scenario as cfd_scenario  # noqa: E402
from assay2chip.cfd import solve as cfd_solve  # noqa: E402
from assay2chip.cfd import validate as cfd_validate  # noqa: E402
from assay2chip.cfd.model import M3_S_TO_UL_MIN, Field, Finding, Mesh  # noqa: E402
from assay2chip.foam import flow  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402

SCHEMA = "foam-validation/1"
DEFAULT_OUT = ROOT / "assay2chip" / "work" / "foam" / "validation.json"

# --- acceptance tolerances. These are the SUITE'S tolerances, not contract values, and
# --- not one of them was chosen by looking at a result.

#: The fully developed pressure gradient against the exact series solution, on the FINE
#: duct mesh. Measured at 0.80% on 24 x 24 cells across the cross-section.
TOL_DUCT_GRADIENT = 0.015
#: Peak wall shear on the DEVELOPED wall against the exact series maximum. Slightly looser
#: than the gradient because a maximum is a point value on a finite mesh: the peak sits at
#: the centre of the wide wall and is read from the nearest face, half a cell away.
#: Measured at 0.86% on the fine mesh.
TOL_DUCT_SHEAR = 0.02
#: Mass balance across the duct's two patches.
TOL_DUCT_MASS = 1e-6
#: The OBSERVED order of convergence towards the exact series, which is the real test:
#: a solver that lands near the right answer by luck does not approach it at second order.
#: `fvSchemes` is second-order, so the band is centred on 2. Measured at 2.21 and 2.22 for
#: the gradient and the peak shear on this geometry.
DUCT_ORDER_BAND = (1.5, 2.6)

#: "Agreement within a few percent" (assay2chip/foam/README.md) for the cross-validation,
#: where Re << 1. Pressure drop first.
TOL_CROSS_DP = 0.05
#: The outlet split is a RATIO of flows and is much better conditioned than a pressure
#: drop - it does not depend on the absolute resistance at all - so it gets a tighter
#: tolerance. In absolute fraction, not relative: a 0.5/0.5 split disagreeing by 0.01 is
#: a 2% relative difference but is still a 0.01 split difference, which is what matters.
TOL_CROSS_SPLIT = 0.02

#: Below this width-to-depth ratio a depth-averaged model is dropping real side-wall drag
#: (`assay2chip/cfd/validate.py`'s `hele_shaw_validity` benchmark uses the same ~5).
ASPECT_SIDEWALL_LIMIT = 5.0
#: The README's meshing rule: at least 6 cells across the narrowest channel, and at least
#: 3 across a 25 um weir gap.
MIN_CELLS_ACROSS_CHANNEL = 6
MIN_CELLS_ACROSS_SLIT = 3

#: The chip the grid study runs on, and the two spacings. module1_v1 is the repo's first
#: real target (`assay2chip/cfd/README.md`) and the one `cfd/validate.py` also refines.
GRID_CHIP = "module1_v1"
#: Ratio 1.5, which is what the Grid Convergence Index wants. The FINE level is what
#: mesh.py chooses on its own for module1_v1 (0.0333 mm = 6 cells across the 0.2 mm
#: `ch_in_n` channel, the README's floor); the COARSE level is deliberately BELOW that
#: floor and exists only to produce the error bar - it is not a result.
GRID_SPACINGS = (0.05, 1.0 / 30.0)

#: The duct benchmark geometry: a real lane from this repo's chips (0.8 mm x 0.3 mm) at
#: two lengths, so the entrance drop differences out. Neither is a contract value.
DUCT_WIDTH_MM = 0.8
DUCT_DEPTH_MM = 0.3
DUCT_SHORT_MM = 5.0
DUCT_LONG_MM = 15.0
DUCT_ENTRY_MM = 1.0
DUCT_Q_UL_MIN = 10.0
#: (cells across the width, cells across the depth, cells per mm along the flow), coarse
#: first. The ratio between them is the refinement ratio the observed order is measured
#: on, so it is uniform in all three directions: 24/16 = 12/8 = 1.5.
DUCT_RESOLUTIONS = ((16, 16, 8.0), (24, 24, 12.0))


# ======================================================================================
# the duct benchmark
# ======================================================================================


def duct_two_block_mesh(foam_case: Any, length_mm: float, ny: int, nz: int,
                        cells_per_mm: float, *, entry_mm: float = DUCT_ENTRY_MM,
                        width_mm: float = DUCT_WIDTH_MM,
                        depth_mm: float = DUCT_DEPTH_MM) -> dict:
    """A straight duct as TWO blocks along x, with the walls split at `entry_mm`.

    `case.duct_block_mesh` writes one block and one `walls` patch, which cannot separate
    the entrance from the developed length - and the entrance is where
    `flowRateInletVelocity`'s flat profile puts a wall-shear spike that has nothing to do
    with the analytic solution. Two blocks give `walls_entry` and `walls_dev`, and
    `flow.wall_shear_by_zone` reads per-zone wall patches straight off their names.

    Hex vertex order is blockMesh's: the four vertices of the x-low face first, then the
    x-high face, each counter-clockwise seen from outside.
    """
    hw, hh = width_mm / 2e3, depth_mm / 2e3
    x0, x1, x2 = 0.0, entry_mm * 1e-3, length_mm * 1e-3
    if not (0 < x1 < x2):
        raise ValueError(f"duct needs 0 < entry ({entry_mm} mm) < length ({length_mm} mm)")
    vertices = [
        (x0, -hw, -hh), (x1, -hw, -hh), (x1, hw, -hh), (x0, hw, -hh),
        (x0, -hw, hh), (x1, -hw, hh), (x1, hw, hh), (x0, hw, hh),
        (x2, -hw, -hh), (x2, hw, -hh), (x2, -hw, hh), (x2, hw, hh),
    ]
    nx_entry = max(int(round(entry_mm * cells_per_mm)), 4)
    nx_dev = max(int(round((length_mm - entry_mm) * cells_per_mm)), 4)
    blocks = [
        f"hex (0 1 2 3 4 5 6 7) ({nx_entry} {ny} {nz}) simpleGrading (1 1 1)",
        f"hex (1 8 9 2 5 10 11 6) ({nx_dev} {ny} {nz}) simpleGrading (1 1 1)",
    ]
    V = foam_case.Verbatim
    T = foam_case.Table
    boundary = [
        ("inlet", {"type": "patch", "faces": T([V("(0 4 7 3)")])}),
        ("outlet", {"type": "patch", "faces": T([V("(8 9 11 10)")])}),
        ("walls_entry", {"type": "wall", "faces": T([
            V("(0 1 5 4)"), V("(3 7 6 2)"), V("(0 3 2 1)"), V("(4 5 6 7)")])}),
        ("walls_dev", {"type": "wall", "faces": T([
            V("(1 8 10 5)"), V("(2 6 11 9)"), V("(1 2 9 8)"), V("(5 10 11 6)")])}),
    ]
    return foam_case.block_mesh_dict(vertices, blocks, boundary)


def _run_duct(stack: flow.FoamStack, case_dir: Path, length_mm: float, fluid: Any,
              q_m3_s: float, resolution: Tuple[int, int, float]) -> dict:
    """One duct case: blockMesh, checkMesh, simpleFoam, then the numbers out of it."""
    foam_case, foam_runner = stack.case, stack.runner
    ny, nz, cells_per_mm = resolution
    case_dir.mkdir(parents=True, exist_ok=True)
    patches = {
        "inlet": foam_case.PatchBC("feed", q_m3_s=q_m3_s),
        "outlet": foam_case.PatchBC("vent"),
        "walls_entry": foam_case.PatchBC("wall"),
        "walls_dev": foam_case.PatchBC("wall"),
    }
    foam_case.write_dict(
        case_dir / "system" / "blockMeshDict",
        duct_two_block_mesh(foam_case, length_mm, ny, nz, cells_per_mm),
        object_name="blockMeshDict", location="system",
    )
    foam_case.build_case(case_dir, fluid=fluid, patches=patches, solver="simpleFoam",
                         functions=foam_case.standard_functions(patches, write_interval=50))
    for command in ("blockMesh", "checkMesh", "simpleFoam"):
        result = foam_runner.run(case_dir, command, check=False)
        if not result.ok:
            raise flow.FlowError(
                f"the duct benchmark failed at `{command}` (L = {length_mm} mm): "
                f"{result.tail(20)}"
            )
    plan = flow.CasePlan(
        chip_id=f"duct_{length_mm:g}mm", step="duct", solver="simpleFoam",
        feeds=["inlet"], vents=["outlet"],
        mu_pa_s=fluid.mu_pa_s, rho_kg_m3=fluid.rho_kg_m3, nu_m2_s=fluid.nu_m2_s,
        q_in_ul_min=q_m3_s * M3_S_TO_UL_MIN, case_dir=str(case_dir),
    )
    raw = flow.collect(case_dir, plan)
    summary = flow.summarise(raw, plan)
    shear = dict((summary.get("shear") or {}).get("per_zone") or {})
    return {
        "length_mm": length_mm,
        "resolution": {"ny": ny, "nz": nz, "cells_per_mm": cells_per_mm},
        "dp_pa": (summary.get("pressure") or {}).get("drop_pa", {}).get("inlet->outlet"),
        "mass_closure": ((summary.get("flow") or {}).get("mass_balance") or {}).get("closure"),
        "shear_dev_pa": (shear.get("dev") or {}).get("max_pa"),
        "shear_entry_pa": (shear.get("entry") or {}).get("max_pa"),
        "cells": raw.cells,
        "case_dir": str(case_dir),
        "summary": summary,
    }


def duct_check(stack: Optional[flow.FoamStack] = None, *,
               out: Optional[Path] = None) -> dict:
    """OpenFOAM on a straight rectangular duct against the exact series solution.

    The gated numbers are the fully developed pressure GRADIENT (entrance differenced out)
    and the peak wall shear on the developed wall. The reference values come from
    `assay2chip/cfd/validate.py`, which derives the series independently of both solvers,
    so this is not a solver checking itself.
    """
    stack = stack or flow.discover_foam()
    report: Dict[str, Any] = {
        "id": "duct_series",
        "name": "straight rectangular duct vs the exact Fourier-series solution",
        "geometry": {
            "width_mm": DUCT_WIDTH_MM, "depth_mm": DUCT_DEPTH_MM,
            "aspect_w_over_h": DUCT_WIDTH_MM / DUCT_DEPTH_MM,
            "lengths_mm": [DUCT_SHORT_MM, DUCT_LONG_MM],
            "entry_block_mm": DUCT_ENTRY_MM,
            "q_ul_min": DUCT_Q_UL_MIN,
            "resolutions": [{"ny": r[0], "nz": r[1], "cells_per_mm": r[2]}
                            for r in DUCT_RESOLUTIONS],
        },
        "reference": "assay2chip/cfd/validate.py (exact series, derived independently)",
        "method": (
            "two lengths so the entrance drop differences out, two resolutions so the "
            "approach to the exact answer can be measured rather than asserted, and the "
            "peak shear read on the developed wall patch only"
        ),
    }
    if not (stack.available and stack.openfoam_available()):
        report.update(status="skipped", passed=None,
                   reason=stack.reason or "OpenFOAM is not installed")
        return report

    foam_case = stack.case
    fluid = foam_case.fluid_for(37.0, name="duct benchmark (contract medium at 37 C)")
    mu = fluid.mu_pa_s
    q = DUCT_Q_UL_MIN / 6e10
    work = foam_case.out_dir(work=out) if out else foam_case.out_dir("foam_validate")

    # --- exact: the series solution, per unit length and at the peak ------------------
    r_exact_short = cfd_validate.duct_resistance_exact(DUCT_SHORT_MM, DUCT_WIDTH_MM,
                                                       DUCT_DEPTH_MM, mu)
    r_exact_long = cfd_validate.duct_resistance_exact(DUCT_LONG_MM, DUCT_WIDTH_MM,
                                                      DUCT_DEPTH_MM, mu)
    dx = (DUCT_LONG_MM - DUCT_SHORT_MM) * 1e-3
    grad_exact = (r_exact_long - r_exact_short) * q / dx
    tau_exact_pa = cfd_validate.wall_shear_max_exact(q, DUCT_WIDTH_MM, DUCT_DEPTH_MM, mu)
    r_approx_long = cfd_validate.duct_resistance_approx(DUCT_LONG_MM, DUCT_WIDTH_MM,
                                                        DUCT_DEPTH_MM, mu)

    levels: List[dict] = []
    for resolution in DUCT_RESOLUTIONS:
        tag = f"{resolution[0]}x{resolution[1]}"
        try:
            short = _run_duct(stack, work / f"duct_{tag}_short", DUCT_SHORT_MM, fluid, q,
                              resolution)
            long = _run_duct(stack, work / f"duct_{tag}_long", DUCT_LONG_MM, fluid, q,
                             resolution)
        except flow.FlowError as exc:
            report.update(status="fail", passed=False, reason=str(exc))
            return report
        if short["dp_pa"] is None or long["dp_pa"] is None:
            report.update(status="fail", passed=False,
                       reason="no inlet->outlet pressure drop in the duct cases: case.py's "
                              "`p_<patch>` surfaceFieldValue objects did not write")
            return report
        levels.append({
            "tag": tag,
            "resolution": {"ny": resolution[0], "nz": resolution[1],
                           "cells_per_mm": resolution[2]},
            "cells_long": long["cells"],
            "gradient_pa_per_m": (long["dp_pa"] - short["dp_pa"]) / dx,
            "peak_shear_dev_pa": long["shear_dev_pa"],
            "peak_shear_entry_pa": long["shear_entry_pa"],
            "dp_long_pa": long["dp_pa"],
            "dp_short_pa": short["dp_pa"],
            "mass_closure": long["mass_closure"],
            "case_dirs": [short["case_dir"], long["case_dir"]],
        })

    coarse, fine = levels[0], levels[-1]
    ratio = DUCT_RESOLUTIONS[-1][0] / DUCT_RESOLUTIONS[0][0]

    checks: List[dict] = [
        _check("developed_pressure_gradient",
               f"fully developed dp/dx on the {fine['tag']} mesh, entrance differenced out",
               fine["gradient_pa_per_m"], grad_exact, TOL_DUCT_GRADIENT, "Pa/m"),
        _check("peak_wall_shear_developed",
               f"max |tau| on walls_dev ({fine['tag']} mesh) vs the exact series maximum",
               fine["peak_shear_dev_pa"], tau_exact_pa, TOL_DUCT_SHEAR, "Pa"),
        _check("mass_balance", "inlet flow vs outlet flow",
               fine["mass_closure"], 0.0, TOL_DUCT_MASS, "fraction", absolute=True),
    ]
    for name, key, exact in (("pressure_gradient", "gradient_pa_per_m", grad_exact),
                             ("peak_wall_shear", "peak_shear_dev_pa", tau_exact_pa)):
        checks.append(_order_check(name, coarse[key], fine[key], exact, ratio))

    # Diagnostics, NOT gates: each is expected to differ, for a reason stated next to it.
    # A suite that gated on these would be asserting the wrong physics.
    entrance_excess = fine["dp_long_pa"] - r_exact_long * q
    diagnostics = [
        {
            "name": "end_to_end_dp_vs_fully_developed",
            "foam": fine["dp_long_pa"],
            "exact_fully_developed": r_exact_long * q,
            "relative": _rel(fine["dp_long_pa"], r_exact_long * q),
            "observed": (
                f"the end-to-end drop reads {'HIGH' if entrance_excess > 0 else 'LOW'} by "
                f"{_pct(_rel(fine['dp_long_pa'], r_exact_long * q))}. Two effects fight "
                f"here and this number is their SUM, which is why it is not a gate: "
                f"`flowRateInletVelocity`'s flat profile adds an entrance drop (positive), "
                f"while the finite mesh under-resolves the no-slip wall and removes drop "
                f"(negative). In Stokes flow the development length is of order the depth, "
                f"0.3 mm in 15 mm, so the mesh term dominates - and the gated number is the "
                f"differenced gradient, where both duct lengths share the same entrance and "
                f"it cancels exactly."
            ),
        },
        {
            "name": "series_vs_0.63_form",
            "exact": r_exact_long * q,
            "contract_form": r_approx_long * q,
            "relative": _rel(r_approx_long, r_exact_long),
            "observed": (
                f"the README's `1 - 0.63 h/w` form against the exact series at w/h = "
                f"{DUCT_WIDTH_MM / DUCT_DEPTH_MM:.3g}: {_pct(_rel(r_approx_long, r_exact_long))}. "
                f"Both solvers build conductance from the 0.63 form by agreement, so this is "
                f"a modelling error they SHARE and it cancels in the cross-validation - it "
                f"is NOT evidence about either implementation."
            ),
        },
        {
            "name": "entry_vs_developed_peak_shear",
            "entry_pa": fine["peak_shear_entry_pa"],
            "developed_pa": fine["peak_shear_dev_pa"],
            "observed": (
                "the entry peak is the higher of the two; that is the flat inlet profile, "
                "not a solver error, and it is why the gated peak is read on walls_dev."
            ),
        },
    ]
    passed = all(c["passed"] for c in checks if c["passed"] is not None)
    report.update(
        status="pass" if passed else "fail",
        passed=passed,
        checks=checks,
        diagnostics=diagnostics,
        exact={"gradient_pa_per_m": grad_exact, "peak_wall_shear_pa": tau_exact_pa},
        levels=levels,
        refinement_ratio=ratio,
        fluid=fluid.to_dict(),
    )
    return report


def _order_check(name: str, coarse: Optional[float], fine: Optional[float],
                 exact: float, ratio: float) -> dict:
    """The OBSERVED order of convergence towards a known exact answer.

    `p = ln(e_coarse / e_fine) / ln(r)`. Because the exact value is known this is the
    order itself, not the Richardson estimate of it. It is the strongest statement this
    benchmark can make: a solver can sit near the right answer for the wrong reason, but
    it cannot approach it at the formal order of its own schemes by accident.
    """
    lo, hi = DUCT_ORDER_BAND
    if coarse is None or fine is None or not exact:
        return {"name": f"{name}_observed_order", "passed": None,
                "requirement": "observed order of convergence towards the exact series",
                "message": "not reported at both resolutions"}
    e_coarse, e_fine = abs(coarse - exact), abs(fine - exact)
    if not (e_coarse > 0 and e_fine > 0):
        return {"name": f"{name}_observed_order", "passed": True,
                "requirement": "observed order of convergence towards the exact series",
                "got": None, "message": f"{name} is exact on at least one mesh"}
    order = math.log(e_coarse / e_fine) / math.log(ratio)
    extrapolated = fine + (fine - coarse) / (ratio ** order - 1.0)
    return {
        "name": f"{name}_observed_order",
        "requirement": f"observed order in [{lo:g}, {hi:g}] (fvSchemes is second order)",
        "got": order, "want": 2.0,
        "tolerance": f"[{lo:g}, {hi:g}]",
        "units": "order",
        "passed": bool(lo <= order <= hi),
        "coarse_error_percent": e_coarse / abs(exact) * 100.0,
        "fine_error_percent": e_fine / abs(exact) * 100.0,
        "richardson_extrapolated": extrapolated,
        "extrapolated_error_percent": _rel(extrapolated, exact) * 100.0,
        "message": (
            f"{name}: error {e_coarse / abs(exact) * 100:.3g}% -> "
            f"{e_fine / abs(exact) * 100:.3g}% under a {ratio:g}x refinement, an observed "
            f"order of {order:.3g}"
            f"{' (second order, as the schemes claim)' if lo <= order <= hi else ' OUTSIDE the expected band'}"
            f"; extrapolated to zero spacing it is {_rel(extrapolated, exact) * 100:.3g}% "
            f"from the exact series"
        ),
    }


def _check(name: str, requirement: str, got: Optional[float], want: Optional[float],
           tol: float, units: str, absolute: bool = False) -> dict:
    """One gated comparison, with the arithmetic visible."""
    if got is None or want is None:
        return {"name": name, "requirement": requirement, "passed": None,
                "got": got, "want": want, "tolerance": tol, "units": units,
                "message": "not reported by the case"}
    diff = abs(got - want) if absolute else _rel(got, want)
    return {
        "name": name, "requirement": requirement,
        "got": got, "want": want,
        "difference": diff, "difference_percent": diff * 100.0,
        "tolerance": tol, "tolerance_percent": tol * 100.0,
        "units": units,
        "passed": bool(diff <= tol),
        "message": (
            f"{name}: OpenFOAM {got:.6g} {units} against the exact {want:.6g} {units}, "
            f"{diff * 100:.3g}% {'within' if diff <= tol else 'OUTSIDE'} the "
            f"{tol * 100:.3g}% tolerance"
        ),
    }


def _rel(got: Optional[float], want: Optional[float]) -> float:
    if got is None or want is None:
        return math.nan
    denom = max(abs(want), 1e-300)
    return abs(got - want) / denom


# ======================================================================================
# cross-validation against the reduced-order solver
# ======================================================================================


def reduced_pressure_drop(mesh: Mesh, field: Field, feeds: Sequence[str],
                          vents: Sequence[str]) -> Dict[str, float]:
    """dP from each feed port node to each vent port node in the reduced-order field.

    The reduced model's port node sits where the port meets the channel, while the 3D
    patch is the port's face on the chip surface. The bore between them is short and the
    two are compared as-is; the residual difference that causes is part of what the
    attribution below has to consider, and it is named in the row's `caveats`.
    """
    out: Dict[str, float] = {}
    for feed in feeds:
        fn = mesh.ports.get(feed)
        if fn is None:
            continue
        for vent in vents:
            vn = mesh.ports.get(vent)
            if vn is None:
                continue
            out[f"{feed}->{vent}"] = field.p_pa.get(fn, 0.0) - field.p_pa.get(vn, 0.0)
    return out


def reduced_outlet_split(field: Field) -> Dict[str, float]:
    """The reduced model's outlet split, from `Field.meta["port_flow_ul_min"]`.

    Port flows are positive INTO the chip there, so an outlet is a negative entry; the
    fractions are of the total that actually leaves.
    """
    flows = dict(field.meta.get("port_flow_ul_min", {}) or {})
    out = {port: -q for port, q in flows.items() if q < 0}
    total = sum(out.values())
    return {port: (q / total if total > 0 else math.nan) for port, q in sorted(out.items())}


@dataclass
class CrossRow:
    """One (chip, step) comparison. The row of the table the README asks for."""

    chip_id: str
    step: Any
    operation: str = ""
    fluid: str = ""
    q_in_ul_min: float = 0.0
    feeds: List[str] = dc_field(default_factory=list)
    vents: List[str] = dc_field(default_factory=list)
    solver: str = ""
    re_reduced: Optional[float] = None
    re_foam: Optional[float] = None
    dp_label: str = ""
    dp_foam_pa: Optional[float] = None
    dp_reduced_pa: Optional[float] = None
    split_foam: Dict[str, float] = dc_field(default_factory=dict)
    split_reduced: Dict[str, float] = dc_field(default_factory=dict)
    gci: Optional[float] = None
    #: Features the two models route the flow through differently (`routing_mismatch`).
    #: Checked before every other cause, because it explains any magnitude of difference.
    routing: List[dict] = dc_field(default_factory=list)
    verdict: str = ""
    believed: str = ""
    why: str = ""
    caveats: List[str] = dc_field(default_factory=list)
    status: str = "ok"

    @property
    def dp_difference(self) -> Optional[float]:
        if self.dp_foam_pa is None or self.dp_reduced_pa is None:
            return None
        return _rel(self.dp_foam_pa, self.dp_reduced_pa)

    @property
    def split_difference(self) -> Optional[float]:
        """The largest absolute difference in any outlet's fraction."""
        ports = set(self.split_foam) | set(self.split_reduced)
        diffs = []
        for port in ports:
            a, b = self.split_foam.get(port), self.split_reduced.get(port)
            if a is None or b is None or not (math.isfinite(a) and math.isfinite(b)):
                continue
            diffs.append(abs(a - b))
        return max(diffs) if diffs else None

    def to_json(self) -> dict:
        return {
            "chip_id": self.chip_id, "step": self.step, "operation": self.operation,
            "fluid": self.fluid, "q_in_ul_min": self.q_in_ul_min,
            "feeds": list(self.feeds), "vents": list(self.vents),
            "solver": self.solver,
            "re": {"reduced": self.re_reduced, "openfoam": self.re_foam},
            "pressure_drop": {
                "label": self.dp_label,
                "openfoam_pa": self.dp_foam_pa,
                "reduced_pa": self.dp_reduced_pa,
                "difference": self.dp_difference,
                "difference_percent": (self.dp_difference * 100.0
                                       if self.dp_difference is not None else None),
                "tolerance_percent": TOL_CROSS_DP * 100.0,
            },
            "outlet_split": {
                "openfoam": dict(self.split_foam),
                "reduced": dict(self.split_reduced),
                "max_difference": self.split_difference,
                "tolerance": TOL_CROSS_SPLIT,
            },
            "grid_convergence_index": self.gci,
            "routing_mismatch": list(self.routing),
            "verdict": self.verdict,
            "believed_correct": self.believed,
            "why": self.why,
            "caveats": list(self.caveats),
            "status": self.status,
        }


@dataclass
class ChipContext:
    """The geometric facts the attribution rules need, read once per chip and spacing."""

    chip_id: str
    spacing_mm: Optional[float]
    min_channel_mm: float = 0.0
    min_aspect_w_over_h: float = math.inf
    narrow_features: List[str] = dc_field(default_factory=list)
    slit_gaps_um: List[float] = dc_field(default_factory=list)
    z_cells: int = 8
    depth_mm: float = 0.0
    #: What mesh.py measured, once the case has been meshed (`adopt_grid`). It wins over
    #: the ratio computed here, because the mesher knows where its cells went.
    cells_across_measured: Optional[float] = None

    @property
    def cells_across_narrowest(self) -> float:
        if self.cells_across_measured:
            return self.cells_across_measured
        if not self.spacing_mm or not self.min_channel_mm > 0:
            return math.inf
        return self.min_channel_mm / self.spacing_mm

    @property
    def cells_across_slit(self) -> float:
        """Cells across the tightest weir crest, assuming the z grading the README asks
        for: `z_cells` across the depth with the crest resolved by at least 3."""
        if not self.slit_gaps_um or not (self.depth_mm and self.z_cells):
            return math.inf
        gap_mm = min(self.slit_gaps_um) / 1000.0
        return gap_mm / (self.depth_mm / self.z_cells)

    def to_json(self) -> dict:
        return {
            "chip_id": self.chip_id, "spacing_mm": self.spacing_mm,
            "min_channel_mm": self.min_channel_mm,
            "cells_across_narrowest": (None if math.isinf(self.cells_across_narrowest)
                                       else self.cells_across_narrowest),
            "min_aspect_w_over_h": (None if math.isinf(self.min_aspect_w_over_h)
                                    else self.min_aspect_w_over_h),
            "narrow_features": list(self.narrow_features),
            "slit_gaps_um": list(self.slit_gaps_um),
            "cells_across_slit": (None if math.isinf(self.cells_across_slit)
                                  else self.cells_across_slit),
            "z_cells": self.z_cells, "depth_mm": self.depth_mm,
        }


def chip_context(mesh: Mesh, spacing_mm: Optional[float], z_cells: int = 8) -> ChipContext:
    """Measure the chip once: narrowest channel, worst aspect ratio, weir gaps."""
    ctx = ChipContext(chip_id=mesh.chip_id, spacing_mm=spacing_mm, z_cells=z_cells)
    widths = []
    for edge in mesh.edges:
        if edge.kind == "hs_face":
            continue
        if edge.kind == "slit" and edge.gap_um:
            ctx.slit_gaps_um.append(float(edge.gap_um))
            continue
        if edge.width_mm > 0:
            widths.append(edge.width_mm)
        if edge.width_mm > 0 and edge.depth_mm > 0:
            aspect = max(edge.width_mm, edge.depth_mm) / min(edge.width_mm, edge.depth_mm)
            if aspect < ctx.min_aspect_w_over_h:
                ctx.min_aspect_w_over_h = aspect
            if aspect < ASPECT_SIDEWALL_LIMIT and edge.feature_id:
                if edge.feature_id not in ctx.narrow_features:
                    ctx.narrow_features.append(edge.feature_id)
    ctx.min_channel_mm = min(widths) if widths else 0.0
    ctx.depth_mm = max((n.depth_mm for n in mesh.nodes if n.depth_mm > 0), default=0.0)
    ctx.slit_gaps_um = sorted(set(ctx.slit_gaps_um))
    return ctx


#: A reduced-model edge carrying less than this fraction of the through-flow is carrying
#: nothing: the solver's own floor is ~1e-10 of the through-flow, so 1e-3 is far above the
#: numerical noise and far below any real flow split.
ROUTING_DEAD_FRACTION = 1e-3
#: ...while the same feature moving more than this fraction of the chip's peak velocity in
#: the 3D field is carrying something. Between them the two models disagree about the ROUTE
#: the liquid takes, which is a different and much more serious thing than disagreeing
#: about a number.
ROUTING_LIVE_FRACTION = 0.05


def routing_mismatch(mesh: Mesh, field: Field, summary: Mapping) -> List[dict]:
    """Features the two models route the flow through DIFFERENTLY.

    This is checked before anything else, because a routing difference explains a
    disagreement of any size and attributing it to inertia or to side walls would be
    nonsense. It is also the fault worth finding: a feature that one model bypasses
    entirely is not a few percent of error, it is a chip whose delay line does not exist.

    The test is deliberately crude and one-directional - a `duct` edge that the reduced
    model leaves dry while the 3D field has real velocity in the matching cellZone - so it
    cannot fire on a genuine 60/40 split or on numerical noise. `spc_demo_v1` is the case
    it was written for: `ch_xj_delay` and `ch_delay_out` both attach to `j:delay_1:a`, so
    the 234 mm serpentine hangs off one node as a dead-end stub, carries exactly zero, and
    the reduced model reports 39 Pa where the duct law and OpenFOAM both say ~1150.
    """
    q_in = abs(float(field.residual.q_in) or 0.0)
    if not q_in:
        return []
    per_zone = dict((summary.get("reynolds") or {}).get("per_zone") or {})
    u_chip = max((abs(float(v.get("u_max_mm_s") or 0.0)) for v in per_zone.values()),
                 default=0.0)
    if not u_chip:
        return []
    out: List[dict] = []
    for feature in sorted(mesh.features()):
        ducts = [e for e in mesh.edges_of(feature) if e.kind == "duct"]
        if not ducts:
            continue
        reduced_q = max((abs(field.q_m3_s.get(e.id, 0.0)) for e in ducts), default=0.0)
        u_foam = abs(float((per_zone.get(feature) or {}).get("u_max_mm_s") or 0.0))
        if reduced_q / q_in >= ROUTING_DEAD_FRACTION:
            continue
        if u_foam / u_chip < ROUTING_LIVE_FRACTION:
            continue
        longest = max(ducts, key=lambda e: e.length_mm)
        out.append({
            "feature_id": feature,
            "reduced_fraction_of_through_flow": reduced_q / q_in,
            "openfoam_u_max_mm_s": u_foam,
            "openfoam_fraction_of_peak_u": u_foam / u_chip,
            "duct_length_mm": longest.length_mm,
            "duct_width_mm": longest.width_mm,
            "duct_depth_mm": longest.depth_mm,
            "reduced_nodes": [longest.a, longest.b],
        })
    return out


def adopt_grid(ctx: ChipContext, summary: Mapping) -> ChipContext:
    """Replace the context's guesses with what the 3D mesh ACTUALLY did.

    `chip_context` is built from the reduced model before the 3D run exists, so its
    spacing is whatever the caller asked for and its narrowest channel comes from the
    reduced mesh's duct widths. Once the case is meshed, `mesh.py` has measured the
    spacing it chose, the narrowest feature and the cells across it - and the staircase
    verdict must be reached on THOSE numbers, not on a pre-run estimate. Getting this
    wrong turns a 6-cell mesh into a 3-cell one on paper and invents a staircase.
    """
    grid = dict(summary.get("grid") or {})
    if grid.get("spacing_mm"):
        ctx.spacing_mm = float(grid["spacing_mm"])
    if grid.get("min_channel_mm"):
        ctx.min_channel_mm = float(grid["min_channel_mm"])
    if grid.get("cells_across_narrowest"):
        ctx.cells_across_measured = float(grid["cells_across_narrowest"])
    if grid.get("z_cells"):
        ctx.z_cells = int(grid["z_cells"])
    return ctx


def attribute(row: CrossRow, ctx: ChipContext) -> CrossRow:
    """Decide the verdict for one row: agreement, or a named cause and a believed model.

    THE ORDER IS THE POINT. Inertia and side-wall drag are failures OF THE REDUCED MODEL
    and are checked first because they are the two the READMEs predict. Resolution is a
    failure of the 3D run and is checked next. Only when none of them applies is the row
    called unexplained - which is a real answer, and a better one than attributing a
    disagreement to whichever cause happens to be nearby.

    Nothing here adjusts a number. A row that cannot be explained stays unexplained.
    """
    dp_diff = row.dp_difference
    split_diff = row.split_difference
    if dp_diff is None and split_diff is None:
        row.verdict = "incomplete"
        row.believed = "neither"
        row.why = "one of the two models produced no comparable number for this step"
        row.status = "incomplete"
        return row

    dp_ok = dp_diff is None or dp_diff <= TOL_CROSS_DP
    split_ok = split_diff is None or split_diff <= TOL_CROSS_SPLIT
    re_max = max([r for r in (row.re_reduced, row.re_foam) if r is not None] or [0.0])

    # 0. THE ROUTE ITSELF. Checked before agreement, because two models that send the
    # liquid down different paths are not validating each other even when their numbers
    # happen to land close together.
    if row.routing:
        worst = max(row.routing, key=lambda r: r.get("duct_length_mm") or 0.0)
        row.verdict = "disagree"
        row.believed = "openfoam"
        row.why = (
            f"the two models route the flow DIFFERENTLY: the reduced model leaves "
            f"{', '.join(r['feature_id'] for r in row.routing)} dry "
            f"({worst['reduced_fraction_of_through_flow']:.1e} of the through-flow) while "
            f"the 3D field carries "
            f"{worst['openfoam_u_max_mm_s']:.3g} mm/s through it. {worst['feature_id']} is "
            f"a {worst['duct_length_mm']:.4g} mm duct, so bypassing it removes essentially "
            f"all of the chip's resistance - which is the {_pct(dp_diff)} seen here. In the "
            f"reduced mesh both of its neighbours attach to the SAME end node "
            f"({worst['reduced_nodes'][0]}), so the duct hangs off one point as a dead-end "
            f"stub. The 3D mesh is a raster of the plan view and cannot make that mistake, "
            f"so the 3D answer is correct and this is a BUG IN assay2chip/cfd/mesh.py, not "
            f"a modelling difference."
        )
        row.caveats.append(
            "every reduced-order number for this chip - pressure, shear, residence time - "
            "is wrong for as long as the bypass stands, not just this step's"
        )
        return row

    if dp_ok and split_ok:
        row.verdict = "agree"
        row.believed = "both"
        row.why = (
            f"the two models agree to {_pct(dp_diff)} on pressure drop and "
            f"{_pct(split_diff, absolute=True)} on the outlet split, at Re = "
            f"{re_max:.3g}. Two independent formulations - a 3D finite-volume "
            f"Navier-Stokes solve and a depth-averaged Stokes resistance network - do not "
            f"land on the same wrong answer, so this agreement is the evidence that BOTH "
            f"are right here."
        )
        if re_max > flow.RE_STEADY_LIMIT:
            row.caveats.append(
                f"they agree even though Re = {re_max:.3g} > 1, which means inertia is not "
                f"yet changing the pressure drop at this flow - it does not make the "
                f"reduced model valid for shear or recirculation here"
            )
        return row

    # 1. inertia: the reduced model is a Stokes solver and says so.
    if re_max > flow.RE_STEADY_LIMIT:
        row.verdict = "disagree"
        row.believed = "openfoam"
        row.why = (
            f"Re = {re_max:.3g} > {flow.RE_STEADY_LIMIT:g}: the reduced model is a Stokes "
            f"solver, so it has NO inertial term at all and reports this edge as "
            f"`unmodelled` itself. The 3D {row.solver} run carries the convective term, so "
            f"its dP ({_fmt(row.dp_foam_pa)} Pa vs {_fmt(row.dp_reduced_pa)} Pa, "
            f"{_pct(dp_diff)}) is the number to use. Inertia raises the drop through bends "
            f"and expansions, which is the sign seen here"
            if (row.dp_foam_pa or 0) >= (row.dp_reduced_pa or 0) else
            f"Re = {re_max:.3g} > {flow.RE_STEADY_LIMIT:g}: the reduced Stokes model has no "
            f"inertial term, so the 3D run is the one to use. The 3D drop is LOWER here, "
            f"which is worth a look - inertia usually adds drop - and most often means the "
            f"reduced mesh is over-resisting a chamber it modelled as a duct"
        )
        return row

    # 2. side walls: depth-averaging drops real drag in a narrow chamber or lane.
    if ctx.min_aspect_w_over_h < ASPECT_SIDEWALL_LIMIT and (
            row.dp_foam_pa or 0) > (row.dp_reduced_pa or 0):
        row.verdict = "disagree"
        row.believed = "openfoam"
        row.why = (
            f"the narrowest flow cross-section on this chip has w/h = "
            f"{ctx.min_aspect_w_over_h:.3g}, below the ~{ASPECT_SIDEWALL_LIMIT:g} where a "
            f"depth-averaged model stops being safe"
            + (f" (features: {', '.join(ctx.narrow_features[:4])})"
               if ctx.narrow_features else "")
            + f". Hele-Shaw drops the side-wall drag entirely, so the reduced model "
              f"under-predicts resistance - and the 3D run reads HIGHER "
              f"({_fmt(row.dp_foam_pa)} vs {_fmt(row.dp_reduced_pa)} Pa, {_pct(dp_diff)}), "
              f"which is exactly that sign. The 3D number is correct."
        )
        return row

    # 3. resolution: the 3D run's own error bar is the bigger number.
    if ctx.cells_across_narrowest < MIN_CELLS_ACROSS_CHANNEL:
        row.verdict = "inconclusive"
        row.believed = "reduced"
        row.why = (
            f"the hex mesh puts only {ctx.cells_across_narrowest:.2g} cells across the "
            f"narrowest channel ({ctx.min_channel_mm:.3g} mm at {ctx.spacing_mm:g} mm "
            f"spacing), under the README's floor of {MIN_CELLS_ACROSS_CHANNEL}. A "
            f"staircased channel wall is both narrower and rougher than the real one, so "
            f"the 3D drop is a discretisation artefact at this spacing and the reduced "
            f"model's analytic duct is the better number. Re-run finer before believing "
            f"the 3D value."
        )
        return row
    if ctx.cells_across_slit < MIN_CELLS_ACROSS_SLIT and ctx.slit_gaps_um:
        row.verdict = "inconclusive"
        row.believed = "reduced"
        row.why = (
            f"the tightest weir crest is {min(ctx.slit_gaps_um):g} um and the z grading "
            f"gives it only {ctx.cells_across_slit:.2g} cells. Flow through a slit goes as "
            f"gap^3, so a crest resolved by fewer than {MIN_CELLS_ACROSS_SLIT} cells is a "
            f"guess, not a measurement; the reduced model's analytic slit is correct here."
        )
        return row
    if row.gci is not None and dp_diff is not None and row.gci >= dp_diff:
        row.verdict = "inconclusive"
        row.believed = "neither"
        row.why = (
            f"the models differ by {_pct(dp_diff)} but the 3D run's own grid-convergence "
            f"index is {_pct(row.gci)}: the disagreement is inside the discretisation error "
            f"bar, so this is not evidence that either model is wrong. Refine the mesh "
            f"before reading anything into it."
        )
        return row

    # 4. unexplained. Reported as such.
    row.verdict = "disagree"
    row.believed = "unresolved"
    row.why = (
        f"the models differ by {_pct(dp_diff)} on pressure drop and "
        f"{_pct(split_diff, absolute=True)} on the outlet split at Re = {re_max:.3g}, with "
        f"{ctx.cells_across_narrowest:.2g} cells across the narrowest channel and a worst "
        f"aspect ratio of {ctx.min_aspect_w_over_h:.3g} - so neither inertia, nor side-wall "
        f"drag, nor staircase resolution explains it. UNEXPLAINED: one of the two models is "
        f"wrong here and this suite cannot yet say which. Neither has been adjusted."
    )
    row.caveats.append(
        "a 3D port patch sits on the chip surface while the reduced model's port node sits "
        "where the port meets the channel; the bore between them is unmodelled in one and "
        "lumped in the other, which is the first thing to check"
    )
    return row


def _pct(value: Optional[float], absolute: bool = False) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    return f"{value:.3g}" if absolute else f"{value * 100:.3g}%"


def _fmt(value: Any, digits: int = 5) -> str:
    return flow._fmt(value, digits)


def cross_validate(
    chip_ids: Optional[Sequence[str]] = None,
    *,
    stack: Optional[flow.FoamStack] = None,
    spacing_mm: Optional[float] = None,
    grid_mm: float = 0.25,
    max_steps: Optional[int] = 3,
    n_proc: int = 1,
    gci_by_chip: Optional[Mapping[str, float]] = None,
    out: Optional[Path] = None,
) -> dict:
    """The cross-validation table: every chip and step both models can run.

    "Both can run" means: the chip has a protocol in `protocols/`, the step moves liquid,
    the scenario maps onto the mesh, and the reduced solve succeeds. A step only one of
    them can run is listed under `not_comparable` with the reason rather than dropped,
    because "we never checked this one" is information.
    """
    stack = stack or flow.discover_foam()
    rows: List[CrossRow] = []
    not_comparable: List[dict] = []
    contexts: Dict[str, dict] = {}
    have_foam = stack.available and stack.openfoam_available()

    for chip_path in _chip_paths(chip_ids):
        chip = json.loads(chip_path.read_text(encoding="utf-8"))
        chip_id = chip.get("id", chip_path.stem)
        protocol_path, protocol = flow._load_protocol_for(chip_id)
        if protocol_path is None:
            not_comparable.append({"chip_id": chip_id, "reason": "no protocol in protocols/"})
            continue
        try:
            mesh = cfd_mesh.build_mesh(chip, grid_mm=grid_mm)
            run = cfd_scenario.build_protocol(protocol_path, chip_path, worst_cases=False)
        except Exception as exc:
            not_comparable.append({"chip_id": chip_id,
                                   "reason": f"{type(exc).__name__}: {exc}"})
            continue
        ctx = chip_context(mesh, spacing_mm)
        contexts[chip_id] = ctx.to_json()
        records = flow.flowing_steps(run)
        if max_steps is not None:
            records = records[:max_steps]
        if not records:
            not_comparable.append({"chip_id": chip_id,
                                   "reason": "no flowing step the scenario builder could map"})
            continue
        try:
            scenarios = [r.scenario.for_mesh(mesh) for r in records]
            fields = cfd_solve.solve_steps(mesh, scenarios)
        except Exception as exc:
            not_comparable.append({"chip_id": chip_id,
                                   "reason": f"reduced solve failed: {type(exc).__name__}: {exc}"})
            continue

        for record, field in zip(records, fields):
            feeds = sorted(k for k, v in record.ports.items() if v.type == "flow")
            vents = sorted(k for k, v in record.ports.items() if v.type == "pressure")
            reduced_dp = reduced_pressure_drop(mesh, field, feeds, vents)
            label = next(iter(sorted(reduced_dp)), "")
            row = CrossRow(
                chip_id=chip_id, step=record.step, operation=record.operation,
                fluid=record.fluid.name, q_in_ul_min=record.q_in_ul_min,
                feeds=feeds, vents=vents,
                re_reduced=(max(field.re.values()) if field.re else None),
                dp_label=label,
                dp_reduced_pa=reduced_dp.get(label),
                split_reduced=reduced_outlet_split(field),
                gci=(gci_by_chip or {}).get(chip_id),
            )
            if not have_foam:
                row.status = "skipped"
                row.verdict = "skipped"
                row.believed = "neither"
                row.why = stack.reason or "OpenFOAM is not installed, so only the reduced-order side of this row exists"
                rows.append(row)
                continue
            try:
                summary = flow.run_step(
                    chip_path, record, mesh=mesh, protocol=protocol,
                    spacing_mm=spacing_mm, n_proc=n_proc, reduced=field,
                    out=out, stack=stack,
                )
            except Exception as exc:
                row.status = "error"
                row.verdict = "error"
                row.believed = "neither"
                row.why = f"the 3D run failed: {type(exc).__name__}: {exc}"
                rows.append(row)
                continue
            row.solver = str(summary.get("solver", ""))
            row.re_foam = (summary.get("reynolds") or {}).get("chip_max")
            row.dp_foam_pa = ((summary.get("pressure") or {}).get("drop_pa") or {}).get(label)
            adopt_grid(ctx, summary)
            row.routing = routing_mismatch(mesh, field, summary)
            contexts[chip_id] = ctx.to_json()
            row.split_foam = {
                k: v for k, v in (((summary.get("flow") or {})
                                   .get("outlet_split") or {}).get("fraction") or {}).items()
                if v is not None
            }
            rows.append(attribute(row, ctx))

    agree = [r for r in rows if r.verdict == "agree"]
    disagree = [r for r in rows if r.verdict == "disagree"]
    inconclusive = [r for r in rows if r.verdict == "inconclusive"]
    return {
        "id": "cross_validation",
        "name": "3D OpenFOAM vs the reduced-order solver: pressure drop and outlet split",
        "status": ("skipped" if not have_foam else
                   "pass" if not disagree and agree else
                   "fail" if disagree else "incomplete"),
        "tolerances": {"pressure_drop_relative": TOL_CROSS_DP,
                       "outlet_split_absolute": TOL_CROSS_SPLIT},
        "spacing_mm": spacing_mm,
        "reduced_grid_mm": grid_mm,
        "counts": {"rows": len(rows), "agree": len(agree), "disagree": len(disagree),
                   "inconclusive": len(inconclusive),
                   "skipped": len([r for r in rows if r.status == "skipped"]),
                   "error": len([r for r in rows if r.status == "error"])},
        "rows": [r.to_json() for r in rows],
        "chip_context": contexts,
        "not_comparable": not_comparable,
        "note": (
            "agreement within a few percent where Re << 1 is the evidence that BOTH models "
            "are right; a disagreement names the model believed correct and why. Neither "
            "model is ever tuned to match the other."
        ),
    }


def _chip_paths(chip_ids: Optional[Sequence[str]]) -> List[Path]:
    if chip_ids:
        return [ROOT / "chip" / f"{cid}.json" for cid in chip_ids]
    return [p for p in sorted((ROOT / "chip").glob("*.json"))
            if not p.stem.startswith("placeholder")]


# ======================================================================================
# grid sensitivity
# ======================================================================================


def grid_study(chip_id: str = GRID_CHIP, spacings: Sequence[float] = GRID_SPACINGS, *,
               stack: Optional[flow.FoamStack] = None, grid_mm: float = 0.25,
               n_proc: int = 1, out: Optional[Path] = None) -> dict:
    """One chip, one step, two resolutions: the error bar every other number here needs.

    Without this the cross-validation cannot tell a real disagreement from a coarse mesh,
    so the GCI it produces is fed back into `attribute()` as `row.gci`.
    """
    stack = stack or flow.discover_foam()
    report: Dict[str, Any] = {"id": "grid_sensitivity", "chip_id": chip_id,
                           "spacings_mm": list(spacings)}
    if not (stack.available and stack.openfoam_available()):
        report.update(status="skipped", reason=stack.reason or "OpenFOAM is not installed")
        return report
    chip_path = ROOT / "chip" / f"{chip_id}.json"
    chip = json.loads(chip_path.read_text(encoding="utf-8"))
    protocol_path, protocol = flow._load_protocol_for(chip_id)
    mesh = cfd_mesh.build_mesh(chip, grid_mm=grid_mm)
    run = cfd_scenario.build_protocol(protocol_path, chip_path, worst_cases=False)
    record = flow.first_flowing_step(run)
    if record is None:
        report.update(status="incomplete", reason=f"{chip_id} has no flowing step to refine")
        return report
    reduced = cfd_solve.solve_step(mesh, record.scenario.for_mesh(mesh))
    summaries = []
    for spacing in spacings:
        try:
            summaries.append(flow.run_step(
                chip_path, record, mesh=mesh, protocol=protocol, spacing_mm=spacing,
                n_proc=n_proc, reduced=reduced, stack=stack,
                report=out, run=f"foam_grid_{spacing:g}",
            ))
        except Exception as exc:
            report.update(status="fail",
                       reason=f"the {spacing:g} mm run failed: {type(exc).__name__}: {exc}")
            return report
    report.update(step=record.step, operation=record.operation,
               **flow.grid_sensitivity(summaries, spacings))
    return report


# ======================================================================================
# the report
# ======================================================================================


def findings(report: Mapping) -> List[Finding]:
    """Every validation verdict that a reader of `findings.json` has to know about.

    `class: "model_error"` is the class `assay2chip/cfd/validate.py` already uses for a
    benchmark failure, and `source: "openfoam"` marks whose benchmark it was.
    """
    out: List[Finding] = []
    duct = report.get("duct", {}) or {}
    for check in duct.get("checks", []) or []:
        if check.get("passed") is False:
            out.append(Finding(
                id=f"foam_duct_{check['name']}", cls="model_error", severity="blocker",
                metric=check["name"], value=check.get("difference"),
                limit=check.get("tolerance"), units="fraction",
                message=(
                    f"the OpenFOAM duct benchmark FAILS: {check['message']}. Until this "
                    f"passes, no 3D number in this package may be believed."
                ),
                fix_hint="refine the duct mesh, check fvSchemes, or check the BC map in case.py",
                source="openfoam",
            ))
    if duct.get("status") == "skipped":
        out.append(Finding(
            id="foam_duct_skipped", cls="model_error", severity="high",
            metric="duct_series_benchmark", message=(
                f"the 3D duct benchmark did not run ({duct.get('reason')}), so nothing from "
                f"assay2chip/foam has been validated against an exact solution."
            ),
            fix_hint="install OpenFOAM (see assay2chip/foam/runner.py) and re-run",
            source="openfoam",
        ))
    for row in (report.get("cross_validation", {}) or {}).get("rows", []) or []:
        if row.get("verdict") not in ("disagree", "inconclusive"):
            continue
        dp = row.get("pressure_drop", {}) or {}
        severity = "high" if row["verdict"] == "disagree" else "medium"
        if row.get("believed_correct") == "openfoam":
            # A predicted, explained divergence. It is still reported, because the
            # reduced model's number is in findings.json too and a reader has to know
            # which to trust - but it is not a fault in either implementation.
            severity = "low"
        out.append(Finding(
            id=f"foam_cross_{row['chip_id']}_{row['step']}",
            cls="model_error", severity=severity,
            chip_id=str(row.get("chip_id", "")), step=row.get("step"),
            metric="dp_3d_vs_reduced",
            value=dp.get("difference"), limit=TOL_CROSS_DP, units="fraction",
            message=(
                f"3D vs reduced-order on {row['chip_id']} step {row['step']}: dP "
                f"{_fmt(dp.get('openfoam_pa'))} Pa vs {_fmt(dp.get('reduced_pa'))} Pa "
                f"({_pct(dp.get('difference'))}), outlet split max difference "
                f"{_pct((row.get('outlet_split') or {}).get('max_difference'), absolute=True)}. "
                f"Believed correct: {row.get('believed_correct')}. {row.get('why')}"
            ),
            fix_hint=(
                "neither model has been tuned; use the believed-correct one and record why"
            ),
            source="openfoam",
            audit={"verdict": row["verdict"], "caveats": row.get("caveats", []),
                   "re": row.get("re")},
        ))
    grid = report.get("grid_sensitivity", {}) or {}
    worst = grid.get("worst_gci")
    if worst is not None and worst > 0.05:
        out.append(Finding(
            id="foam_grid_gci", cls="model_error", severity="medium",
            chip_id=str(grid.get("chip_id", "")), step=grid.get("step"),
            metric="grid_convergence_index", value=float(worst), limit=0.05,
            units="fraction",
            message=(
                f"the 3D results on {grid.get('chip_id')} carry a grid-convergence index of "
                f"{_pct(worst)} at {grid.get('spacing_mm', {}).get('fine')} mm: every 3D "
                f"number in this report has at least that error bar, and any disagreement "
                f"smaller than it is not evidence of anything."
            ),
            fix_hint="refine the mesh until the GCI is below the cross-validation tolerance",
            source="openfoam",
        ))
    return out


def run(
    *,
    chip_ids: Optional[Sequence[str]] = None,
    spacing_mm: Optional[float] = None,
    duct: bool = True,
    cross: bool = True,
    grid: bool = True,
    max_steps: Optional[int] = 3,
    n_proc: int = 1,
    out: Optional[Path] = None,
) -> dict:
    """The whole suite. Returns the report; `write_report` puts it on disk."""
    stack = flow.discover_foam()
    report: Dict[str, Any] = {
        "schema": SCHEMA,
        "openfoam": stack.as_json(),
        "duct": {"status": "not_run"},
        "cross_validation": {"status": "not_run"},
        "grid_sensitivity": {"status": "not_run"},
    }
    if duct:
        report["duct"] = duct_check(stack, out=out)
    if grid:
        report["grid_sensitivity"] = grid_study(stack=stack, n_proc=n_proc,
                                                out=out)
    gci_by_chip: Dict[str, float] = {}
    worst = (report.get("grid_sensitivity") or {}).get("worst_gci")
    if worst is not None:
        gci_by_chip[str(report["grid_sensitivity"].get("chip_id", ""))] = float(worst)
    if cross:
        report["cross_validation"] = cross_validate(
            chip_ids, stack=stack, spacing_mm=spacing_mm, max_steps=max_steps,
            n_proc=n_proc, gci_by_chip=gci_by_chip, out=out,
        )

    statuses = [report[k].get("status") for k in
                ("duct", "cross_validation", "grid_sensitivity")]
    failed = [k for k in ("duct", "cross_validation", "grid_sensitivity")
              if report[k].get("status") == "fail"]
    skipped = [k for k in ("duct", "cross_validation", "grid_sensitivity")
               if report[k].get("status") in ("skipped", "not_run", "incomplete")]
    report["passed"] = not failed and not skipped
    report["status"] = "fail" if failed else ("incomplete" if skipped else "pass")
    # The duct benchmark is the gate. Nothing in this package is trustworthy without it,
    # whatever the cross-validation says - two models can agree and both be wrong.
    report["trusted"] = report["duct"].get("passed") is True and not failed
    report["reason"] = (
        f"failed: {', '.join(failed)}" if failed else
        f"incomplete: {', '.join(skipped)}" if skipped else
        "every benchmark passed"
    )
    report["findings"] = [f.to_json() for f in findings(report)]
    try:
        report["contracts"] = contracts.fingerprint()
    except Exception as exc:  # pragma: no cover - a contract read should not sink a report
        report["contracts"] = {"error": f"{type(exc).__name__}: {exc}"}
    report["statuses"] = dict(zip(("duct", "cross_validation", "grid_sensitivity"),
                                  statuses))
    return report


def write_report(report: Mapping, out: Path = DEFAULT_OUT) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    return out


def format_table(report: Mapping) -> str:
    """The report as the table the brief asks for: duct check, then every cross row."""
    lines: List[str] = []
    duct = report.get("duct", {}) or {}
    lines.append(f"DUCT vs EXACT SERIES                              [{duct.get('status')}]")
    for check in duct.get("checks", []) or []:
        mark = "ok " if check.get("passed") else ("-- " if check.get("passed") is None else "FAIL")
        lines.append(f"  {mark} {check['name']:<34} {_fmt(check.get('got'))} vs "
                     f"{_fmt(check.get('want'))} {check.get('units','')}  "
                     f"{_pct(check.get('difference'))}")
    if duct.get("reason"):
        lines.append(f"      {duct['reason']}")

    cross = report.get("cross_validation", {}) or {}
    lines.append("")
    lines.append(f"CROSS-VALIDATION 3D vs REDUCED-ORDER              [{cross.get('status')}]")
    header = (f"  {'chip':<24} {'step':>5} {'Re':>7} {'dP 3D':>10} {'dP red':>10} "
              f"{'dP %':>7} {'split d':>8}  verdict")
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for row in cross.get("rows", []) or []:
        dp = row.get("pressure_drop", {}) or {}
        split = row.get("outlet_split", {}) or {}
        re = row.get("re", {}) or {}
        re_value = re.get("openfoam") if re.get("openfoam") is not None else re.get("reduced")
        lines.append(
            f"  {str(row.get('chip_id'))[:24]:<24} {str(row.get('step')):>5} "
            f"{_fmt(re_value, 3):>7} {_fmt(dp.get('openfoam_pa'), 4):>10} "
            f"{_fmt(dp.get('reduced_pa'), 4):>10} "
            f"{_pct(dp.get('difference')):>7} "
            f"{_pct(split.get('max_difference'), absolute=True):>8}  "
            f"{row.get('verdict')} ({row.get('believed_correct')})"
        )
        if row.get("verdict") not in ("agree", "skipped"):
            lines.append(f"      why: {row.get('why','')}")
    for item in cross.get("not_comparable", []) or []:
        lines.append(f"  (not comparable) {item.get('chip_id')}: {item.get('reason')}")

    grid = report.get("grid_sensitivity", {}) or {}
    lines.append("")
    lines.append(f"GRID SENSITIVITY {grid.get('chip_id','')} step {grid.get('step','')}"
                 f"                 [{grid.get('status')}]")
    for name, rec in sorted((grid.get("quantities") or {}).items()):
        lines.append(f"  {name:<30} coarse {_fmt(rec.get('coarse'))} -> fine "
                     f"{_fmt(rec.get('fine'))}  GCI {_pct(rec.get('gci'))}")
    if grid.get("reason"):
        lines.append(f"      {grid['reason']}")

    lines.append("")
    lines.append(f"STATUS {report.get('status')}  trusted={report.get('trusted')}  "
                 f"{report.get('reason','')}")
    for f in report.get("findings", []) or []:
        lines.append(f"  [{f.get('severity'):<7}] {f.get('message','')[:150]}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chip", action="append", dest="chips", default=None,
                    help="limit the cross-validation to this chip id (repeatable)")
    ap.add_argument("--spacing-mm", type=float, default=None,
                    help="xy cell size for the cross-validation; omit to let mesh.py choose")
    ap.add_argument("--max-steps", type=int, default=3,
                    help="flowing steps per chip (0 for all)")
    ap.add_argument("--np", type=int, default=1, dest="n_proc")
    ap.add_argument("--duct-only", action="store_true")
    ap.add_argument("--no-grid", action="store_true")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    report = run(
        chip_ids=args.chips,
        spacing_mm=args.spacing_mm,
        duct=True,
        cross=not args.duct_only,
        grid=not args.duct_only and not args.no_grid,
        max_steps=(None if args.max_steps == 0 else args.max_steps),
        n_proc=args.n_proc,
    )
    write_report(report, args.out)
    print(json.dumps(report, indent=2, default=str) if args.json else format_table(report))
    return 0 if report.get("status") != "fail" else 1


if __name__ == "__main__":
    raise SystemExit(main())

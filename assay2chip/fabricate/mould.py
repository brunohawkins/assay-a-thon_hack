"""The POSITIVE embossing mould: the inverse of the chip body.

Raised where the chip has channels, chambers and port wells; flat everywhere the chip is
solid. This is the resin-printed TOOL that gets hot-embossed into a COC/COP sheet
(cad/fabrication.json process `resin_3d_print_plus_hot_emboss`), so the STL S11 ships here
is the thing that actually goes on the printer.

The feature plan, the plate thickness and the mirroring all come from
`chip/fusion/chip_from_spec` - S11 imports it rather than re-deriving a mould, because
that script is the authority and a second opinion on mould geometry is a bug waiting to
happen. What this module adds is the one thing the Fusion script cannot do outside Fusion:
turn the plan into a watertight mesh offline, with no CAD kernel.

MIRRORED, and that is deliberate (chip_from_spec.MIRROR_MOULD): an embossed copy is the
mirror image of its tool, so the tool is mirrored in x to make the embossed chip match the
spec. `is_inverse_of()` is the check that the mould really is the body's complement, done
by un-mirroring the mould's cell depths and comparing them with the body's cell for cell.
"""

from __future__ import annotations

from . import geometry, mesh

TOL_MM = 1e-6


def geometry_for(spec: dict, body_geom: dict) -> dict:
    """The mould's own geometry, on the body's grid reflected in x.

    The feature list still comes from `chip_from_spec.plan(spec, mirror=True)`, so the
    mould is the authority's mould; only the grid lines are shared, so that
    `is_inverse_of()` can compare the two rasterisations cell for cell.
    """
    fx = body_geom["plan"]["fx"]
    xs = (geometry.mirrored_grid(body_geom["xs"], fx) if geometry.MIRROR_MOULD
          else list(body_geom["xs"]))
    return geometry.plan_geometry(spec, mirror=geometry.MIRROR_MOULD,
                                  pitch=body_geom["pitch_mm"], grid=(xs, body_geom["ys"]))


def build(spec: dict, geom: dict = None) -> dict:
    """Triangles for the positive mould, from a MIRRORED geometry.plan_geometry()."""
    geom = geom if geom is not None else geometry.plan_geometry(
        spec, mirror=geometry.MIRROR_MOULD)
    plate = geometry.MOULD_BASE_MM
    tops = [[plate + d for d in row] for row in geom["depths"]]
    tallest = geom["max_depth_mm"]
    return {
        "triangles": mesh.heightfield(geom["xs"], geom["ys"], tops, 0.0),
        "z0_mm": 0.0,
        "z_top_mm": plate + tallest,
        "plate_mm": plate,
        "relief_mm": tallest,
        "mirrored": bool(geom["plan"]["mirrored"]),
        "skipped": list(geom["plan"]["skipped"]),
        "problems": list(geom["plan"]["problems"]),
        "role": ("the resin-printed positive tool: raised exactly where the chip is cut, "
                 "hot-embossed into a COC/COP sheet"),
        "mirror_note": (
            "mirrored in x, so the EMBOSSED chip matches the spec seen from the port side"
            if geom["plan"]["mirrored"] else
            "NOT mirrored: the embossed chip will be the mirror image of the spec"),
        "source": "chip/fusion/chip_from_spec/chip_from_spec.py (plan + MOULD_BASE_MM)",
    }


def is_inverse_of(body_geom: dict, mould_geom: dict) -> dict:
    """Is the mould the body's complement? Un-mirrors the mould and compares cell depths.

    Both grids are built from the same plan with the same pitch, so a mirrored x grid is
    the body's x grid reflected about fx and reversed. Any cell where one is cut and the
    other is not means the tool would not emboss the chip that was designed.
    """
    bx, by, bd = body_geom["xs"], body_geom["ys"], body_geom["depths"]
    mx, my, md = mould_geom["xs"], mould_geom["ys"], mould_geom["depths"]
    mirrored = bool(mould_geom["plan"]["mirrored"])
    fx = body_geom["plan"]["fx"]

    if len(bx) != len(mx) or len(by) != len(my):
        return {"passed": False, "detail": (
            f"grids differ: body {len(bx) - 1}x{len(by) - 1} cells, mould "
            f"{len(mx) - 1}x{len(my) - 1}; the mould is not the same part")}

    worst, mismatches = 0.0, 0
    n_x, n_y = len(bx) - 1, len(by) - 1
    for j in range(n_y):
        if abs(by[j] - my[j]) > TOL_MM:
            return {"passed": False, "detail": f"y grid line {j} differs: {by[j]} vs {my[j]}"}
        for i in range(n_x):
            k = n_x - 1 - i if mirrored else i
            want = bd[j][i]
            got = md[j][k]
            if abs(want - got) > TOL_MM:
                mismatches += 1
                worst = max(worst, abs(want - got))
    if mirrored:
        for i in range(len(bx)):
            if abs((fx - bx[len(bx) - 1 - i]) - mx[i]) > TOL_MM:
                return {"passed": False,
                        "detail": f"mirrored x grid line {i} is not fx - x: "
                                  f"{mx[i]} vs {fx - bx[len(bx) - 1 - i]}"}
    if mismatches:
        return {"passed": False, "detail": (
            f"{mismatches} of {n_x * n_y} cells disagree (worst {worst * 1000:.1f} um): the "
            f"mould is not the inverse of the body")}
    return {"passed": True, "detail": (
        f"all {n_x * n_y} cells match: the mould is raised exactly where the body is cut"
        + (" (after un-mirroring in x)" if mirrored else ""))}

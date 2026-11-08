"""S11's print bundle: fabrication geometry, a closed DXF, a Fusion rebuild and renders.

One chip spec in, one folder under ``cad/exports/<chip_id>/print/`` out, holding both
fabrication routes this project uses (``cad/fabrication.json`` process
``resin_3d_print_plus_hot_emboss``):

    geometry.py   the plan -> a rectilinear height field (grid lines ON the feature edges,
                  so axis-aligned geometry is EXACT, not rasterised) + boundary tracing
    mesh.py       height field -> triangles, and the validator every STL must pass
    body.py       the printable chip body (base + fluidic layer, channels open)
    mould.py      the POSITIVE embossing mould (the inverse: raised where the chip is cut)
    dxf.py        closed LWPOLYLINE DXF with a HEADER declaring $INSUNITS = 4 (mm)
    fusion.py     a Fusion 360 rebuild script, pattern of chip/components/build_chip.py
    render.py     a software renderer (standard library) -> isometric and top-view SVG
    checks.py     the fabrication checks the manifest records as passed or failed
    manifest.py   manifest.json: units, footprint, depth, material, features, checks,
                  hashes, and DEMO vs TARGET scale
    bundle.py     writes all of the above for one chip

Nothing here edits ``cad/`` or ``chip/components/``: the mould plan comes from
``chip/fusion/chip_from_spec`` and the chip spec from disk.
"""

from __future__ import annotations

__all__ = [
    "body",
    "bundle",
    "checks",
    "dxf",
    "fusion",
    "geometry",
    "manifest",
    "mesh",
    "mould",
    "render",
]

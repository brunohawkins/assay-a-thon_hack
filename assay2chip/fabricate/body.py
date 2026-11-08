"""The printable chip body: base + fluidic layer, with every feature cut into the top.

This is the part itself, for either route - resin-printed directly, or the shape the
embossed COC/COP sheet takes after `mould.py`'s tool is pressed into it. The channels are
OPEN, because that is the process: contracts/blocks.json fabrication_floors.note says
"Print open and seal with film rather than printing an enclosed channel below 0.5 mm",
and an enclosed 0.3 mm channel is below that floor. So the lid is NOT in this body; it is
a separate film or plate with drilled ports, and `layer_stack()` reports it as such.

The z stack-up comes from the spec's own `layers`, never from a literal here:

    base      everything below the fluidic layer
    fluidic   the layer whose thickness is the feature depth - the same rule
              cad/make_train_cad.py uses to find the spacer
    lid       not built (the seal)

so the body runs from z = 0 (the spec origin) to z = base + fluidic, and a feature `d`
deep has its floor at base + fluidic - d. For module4_v1 that is 0 -> 0.8 mm with channel
floors at 0.5 mm, which is exactly where the spec puts its chamber centres (z 0.65 =
0.5 + 0.3/2).
"""

from __future__ import annotations

from . import geometry, mesh

TOL_MM = 1e-6


def layer_stack(spec: dict, depth_mm: float) -> dict:
    """Where the fluidic layer sits in the spec's stack-up, and what seals it."""
    layers = spec.get("layers") or []
    if not layers:
        raise ValueError(f"chip {spec.get('id')!r} has no `layers`: nothing says how thick it is")

    fluidic_at = next((i for i, l in enumerate(layers)
                       if abs(float(l["thickness_mm"]) - depth_mm) <= TOL_MM), None)
    gaps: list[str] = []
    if fluidic_at is None:
        # No layer is as thick as the deepest feature. make_train_cad.py calls this a DEPTH
        # problem and so do we: the body is still built, at the depth the features need, and
        # the manifest carries the gap rather than quietly resizing the chip.
        fluidic_mm = depth_mm
        base_mm = sum(float(l["thickness_mm"]) for l in layers[:1])
        listed = ", ".join("{} {:g} mm".format(l["id"], l["thickness_mm"]) for l in layers)
        gaps.append(
            f"no layer is {depth_mm:g} mm thick, so no layer can hold the features "
            f"(spec layers: {listed}); the body was built with a {fluidic_mm:g} mm fluidic "
            f"layer on a {base_mm:g} mm base"
        )
        lid = layers[-1] if len(layers) > 1 else None
    else:
        fluidic_mm = float(layers[fluidic_at]["thickness_mm"])
        base_mm = sum(float(l["thickness_mm"]) for l in layers[:fluidic_at])
        lid = layers[fluidic_at + 1] if fluidic_at + 1 < len(layers) else None
        if base_mm <= 0:
            gaps.append("the fluidic layer is the bottom layer: the body has no base under "
                        "the channels, so the part is only as thick as the channels are deep")

    materials = sorted({str(l.get("material", "unknown")) for l in layers})
    return {
        "base_mm": base_mm,
        "fluidic_mm": fluidic_mm,
        "body_top_mm": base_mm + fluidic_mm,
        "lid": {"id": lid["id"], "thickness_mm": float(lid["thickness_mm"]),
                "material": lid.get("material", "unknown")} if lid else None,
        "seal": ("a separate lid or film with drilled ports: the channels in this body are "
                 "OPEN by design (contracts/blocks.json fabrication_floors.note)"),
        "materials": materials,
        "material": materials[0] if len(materials) == 1 else "mixed",
        "gaps": gaps,
    }


def build(spec: dict, geom: dict = None) -> dict:
    """Triangles for the chip body, plus the stack-up they were built from.

    `geom` is an unmirrored geometry.plan_geometry() - the chip as the spec draws it.
    """
    geom = geom if geom is not None else geometry.plan_geometry(spec, mirror=False)
    if geom["plan"]["mirrored"]:
        raise ValueError("the chip body must be built from an UNMIRRORED plan: a mirrored "
                         "body is the wrong hand and would be printed back to front")

    depth = geom["max_depth_mm"]
    stack = layer_stack(spec, depth)
    top = stack["body_top_mm"]
    tops = [[top - d for d in row] for row in geom["depths"]]

    floor_min = min(min(row) for row in tops)
    if floor_min <= TOL_MM:
        raise ValueError(
            f"chip {spec.get('id')!r}: a {depth:g} mm feature in a {top:g} mm body would cut "
            f"through the floor (recess floor at {floor_min:g} mm). The base layer is too thin."
        )

    return {
        "triangles": mesh.heightfield(geom["xs"], geom["ys"], tops, 0.0),
        "z0_mm": 0.0,
        "z_top_mm": top,
        "stack": stack,
        "depth_mm": depth,
        "recess_floor_mm": top - depth,
        "role": ("the chip itself: resin-printed direct, or the shape the embossed sheet "
                 "takes under the mould. Channels open, sealed afterwards."),
    }

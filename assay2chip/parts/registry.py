"""The parametric part registry: `registry.list()` and `registry.size(block_id, need)`.

S6's whole job is to turn a unit operation into geometry that fits a 30 x 20 x 0.3 mm
envelope, and this is the only place that happens. Two calls:

    from assay2chip.parts import registry, Need

    for entry in registry.list():            # every block id, and what it can be sized to
        ...
    part = registry.size("trap_chamber", Need(
        volume_ul=20, flow_ul_min=10, particles="capsules",
        shear_class="capsules", depth_mm=0.3))

`size` is DETERMINISTIC: the same Need gives byte-identical geometry, because every
number comes from the contracts through assay2chip.lib.contracts, every dimension is
rounded to one precision, and nothing iterates on anything but a capped fixed point.

It returns a PartGeometry or it RAISES. It never returns a part that breaks a fabrication
floor, never silently shrinks a volume to fit, and never invents a value the assay did not
give - a missing need field raises naming the field so S6 can record it in `unresolved`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402
from assay2chip.parts import flowmatter, sizers  # noqa: E402
from assay2chip.parts.geometry import (  # noqa: E402
    Ctx, Floors, Need, PartGeometry, PartSizingError, r,
)

# What each sizer insists on. Published so S3 can tell "this op needs a flow rate" from
# "this op cannot be built", instead of discovering it by catching an exception.
REQUIRES: dict[str, tuple[str, ...]] = {
    "serpentine_channel": ("load | volume_ul | residence_s + flow_ul_min",),
    "snake_channel": ("load | volume_ul | residence_s + flow_ul_min",),
    "asymmetric_serpentine": ("load | volume_ul | residence_s + flow_ul_min",),
    "spiral_channel": ("load | volume_ul | residence_s + flow_ul_min",),
    "straight_channel": ("length_mm | load | volume_ul",),
    "curved_channel": ("length_mm | load | volume_ul",),
    "ring_junction": ("load | volume_ul | residence_s + flow_ul_min",),
    "polygon_chamber": ("load | volume_ul | residence_s + flow_ul_min",),
    "culture_chamber": ("load | volume_ul | residence_s + flow_ul_min", "flow_ul_min"),
    "organoid_chamber": ("load | volume_ul", "particle_um"),
    "trap_chamber": ("load | volume_ul | residence_s + flow_ul_min", "particles != none"),
    "metering_trap": ("load | volume_ul",),
    "pillar_array": ("load | volume_ul",),
    "dld_array": ("load | volume_ul",),
    "wells_array": ("load | volume_ul", "branches"),
    "centipede_channel": ("feed_edge_mm",),
    "herringbone_mixer": ("flow_ul_min",),
    "flow_restrictor": ("flow_ul_min", "pressure_drop_pa | length_mm"),
    "flow_manifold": ("branches",),
    "flow_divider": ("branches",),
    "flow_distributor": ("branches (a power of two)",),
    "text": ("length_mm",),
}


def _group(block_id: str) -> str:
    if block_id in contracts.block("basic"):
        return "basic"
    if block_id in contracts.block("advanced"):
        return "advanced"
    return "unknown"


def _summary(block_id: str) -> str:
    for group in ("basic", "advanced"):
        table = contracts.block(group)
        if block_id in table:
            return str(table[block_id])
    return ""


def list() -> list[dict]:  # noqa: A001 - the brief names this method `list`
    """Every block id in contracts/blocks.json, and what this registry can do with it.

    status is one of:
      sized      there is a sizer; call size() with the fields in `requires`
      refused    the block exists but the physics here makes it useless or unsafe, and
                 `why` quotes the contract's reason
      not_sized  no formula yet. Listed on purpose: S6 emits a `missing_part` placement
                 with this reason rather than guessing geometry.
    """
    out = []
    for block_id in sorted(contracts.library()):
        if block_id in sizers.SIZERS:
            status, why = "sized", ""
        elif block_id in sizers.REFUSED:
            status, why = "refused", sizers.REFUSED[block_id]
        else:
            status, why = "not_sized", sizers.NOT_SIZED.get(
                block_id, "no sizer and no recorded reason yet")
        out.append({
            "block": block_id,
            "group": _group(block_id),
            "contract_summary": _summary(block_id),
            "status": status,
            "why": why,
            "requires": [*REQUIRES.get(block_id, ())],
            "flowmatter_part": next(
                (c.part for c in flowmatter.catalogue() if c.block == block_id), None),
        })
    return out


def sized_blocks() -> list[str]:
    return sorted(sizers.SIZERS)


def size(block_id: str, need: Need) -> PartGeometry:
    """Size one block for one need. Returns a printable part, or raises with the reason.

    The depth is the chip's single etch depth and defaults to the contract's maximum. It
    is the SAME depth for every part on the chip: one fluidic layer at one etch depth, so
    a sizer that wanted its own depth would be asking for a second layer.
    """
    if block_id not in contracts.library():
        raise PartSizingError(
            block_id,
            f"not a block id in contracts/blocks.json. We can place nothing else, so this "
            f"op needs a `missing_part` placement, not a new name. The library has "
            f"{len(contracts.library())} ids.")
    if block_id in sizers.REFUSED:
        raise PartSizingError(block_id, sizers.REFUSED[block_id])
    if block_id not in sizers.SIZERS:
        raise PartSizingError(
            block_id,
            f"is in the contract library but has no sizer: {sizers.NOT_SIZED.get(block_id, 'no reason recorded')}. "
            f"Emit a `missing_part` placement rather than guessing its geometry.")

    depth = contracts.max_depth_mm() if need.depth_mm is None else float(need.depth_mm)
    envelope = contracts.envelope(need.scale)
    floors = Floors(block_id, need.scale)
    viscosity = contracts.viscosity_for(need.temp_c)
    ceiling = contracts.shear_ceiling_for(need.shear_class)

    particle_um = need.particle_um
    if particle_um is None and need.particles in ("capsules",):
        particle_um = contracts.capsule_diameter_um()
    if particle_um is None and need.particles in ("cells", "beads"):
        raise PartSizingError(
            block_id,
            f"particles={need.particles!r} but no particle_um, and the contracts give no "
            f"diameter for {need.particles} (capsule.diameter_um is for capsules). The "
            f"assay has to say, or the gap goes in `unresolved`.")

    ctx = Ctx(block=block_id, need=need, depth_mm=depth, floors=floors, envelope=envelope,
              viscosity=viscosity, ceiling=ceiling, particle_um=particle_um)
    ctx.cite(envelope.source, f"contracts/parameters.json {viscosity.key}")
    if not contracts.single_layer():
        raise PartSizingError(block_id, "the contract no longer says one fluidic layer; "
                                        "this registry assumes it everywhere")

    footprint, volume, openings, features, entries, physics_out = sizers.SIZERS[block_id](ctx)

    checks = floors.finish()
    total = sum(e.volume_ul for e in entries)
    if entries and abs(total - volume) > 1e-3:
        raise PartSizingError(
            block_id,
            f"internal check: the expanded entries total {total:.4g} uL but the part holds "
            f"{volume:.4g} uL. A volume that does not add up is the bug that makes a "
            f"protocol flush the wrong amount.")

    # The volume record goes on EVERY part that was sized from one, so a volume rounded up
    # to the smallest buildable chamber is visible without the caller knowing which sizer
    # it came from.
    volume_out = ctx.volume.as_physics() if ctx.volume is not None else {}
    physics_out = {"depth_mm": r(depth), "etch_depth_um": r(depth * 1000),
                   **volume_out, **physics_out}
    return PartGeometry(
        block=block_id,
        footprint_mm=(r(footprint[0]), r(footprint[1])),
        depth_mm=r(depth),
        volume_ul=r(volume),
        openings=tuple(openings),
        features=tuple(features),
        entries=tuple(entries),
        floors=checks,
        physics=physics_out,
        risks=tuple(ctx.risks),
        assumptions=tuple(ctx.assumptions),
        provenance=tuple(ctx.provenance),
        note=_summary(block_id),
    )


def try_size(block_id: str, need: Need) -> tuple[PartGeometry | None, str | None]:
    """size(), but a refusal comes back as a reason instead of an exception.

    For S3/S6, which want to try several blocks for one op and keep the reasons.
    """
    try:
        return size(block_id, need), None
    except (PartSizingError, contracts.ContractError) as exc:
        return None, str(exc)


def main(argv: "list[str] | None" = None) -> int:
    """python assay2chip/parts/registry.py [--flowmatter] - what the registry can build."""
    argv = sys.argv[1:] if argv is None else argv
    if "--flowmatter" in argv:
        print(json.dumps(flowmatter.summary(), indent=2))
        return 0
    entries = list()
    by_status: dict[str, int] = {}
    for e in entries:
        by_status[e["status"]] = by_status.get(e["status"], 0) + 1
    print(json.dumps({"blocks": len(entries), "by_status": by_status,
                      "registry": entries}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

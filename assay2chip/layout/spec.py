"""Assemble the chip spec: one fluidic layer, one etch depth, base and lid.

What this writes and why each piece is there:

  layers          base / fluidic / lid, bottom to top. The fluidic layer's thickness IS
                  the etch depth, so there is exactly ONE layer in the stack whose
                  thickness matches a chamber depth - which is how `check_blocks` in
                  tools/check_chip_spec.py recognises a single-fluidic-layer chip.
  chambers        every part's chambers, moved from part-local into chip coordinates
                  through its rotation, plus one PORT PAD per port. `volume_ul` comes from
                  the registry, which uses the right formula per shape (a box is x*y*z, a
                  weir chamber is the box MINUS the crest, a serpentine is centreline
                  length x lane width x depth), and is only recomputed when rotation or
                  clamping changed the size that is actually written.
  channels        the part-INTERNAL channels a part expands to (a serpentine's own lane,
                  for instance), with their paths rotated into chip coordinates. The
                  channels BETWEEN parts stay empty: S7 routes those from
                  work/<id>/connections_<chip_id>.json.
  ports           boundary ports, each over its pad.
  thermal_zones   one per temperature-holding op. A zone naming a temperature the material
                  cannot take is an error in tools/check_chip_spec.py, which is the point:
                  the material was chosen for these zones, so they have to agree.
  capsule / trap  declared when the chip handles capsules, so the capsule rules in
                  tools/check_chip_spec.py measure against this design's figures instead
                  of falling back to contracts/parameters.json.
  scale           emitted only when contracts/schemas/chip_spec.schema.json has the field;
                  otherwise the chip says so in `unresolved` rather than failing validation.

`placeholder` is true whenever anything had to be derived rather than read from the source.
A chip built from a volume nobody stated is a draft, and it says so.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "operator"))

import schema_io  # noqa: E402

from assay2chip.lib import contracts  # noqa: E402

from .materials import LayerConvention, Material, layer_convention  # noqa: E402
from .needs import Brief  # noqa: E402
from .parts import SizedPart  # noqa: E402
from .place import Placed, Placement  # noqa: E402
from .ports import PortPlan  # noqa: E402

CHIP_SPEC_SCHEMA = ROOT / "contracts" / "schemas" / "chip_spec.schema.json"
STAGE = "assay2chip/stages/s6_build.py"


def schema_has_scale() -> bool:
    """True once the chip-spec schema carries the optional top-level `scale` field.

    Another workstream owns that field. Until it lands, writing it would fail validation,
    so the chip records the scale it was built to in `unresolved` instead.
    """
    try:
        schema = schema_io.load_json(CHIP_SPEC_SCHEMA)
    except (ValueError, FileNotFoundError):        # pragma: no cover - contract missing
        return False
    return "scale" in (schema.get("properties") or {})


def _box(size: Sequence[float]) -> float:
    return float(size[0]) * float(size[1]) * float(size[2])


def _place_chamber(entry: dict, placed: Placed, floor_z: float) -> dict:
    """One part-local chamber -> a chip-spec chamber in chip coordinates."""
    out = dict(entry)
    size = placed.size_to_chip(entry["size"])
    size[2] = float(entry["size"][2])
    cx, cy = placed.to_chip(entry["center"])
    out["center"] = [cx, cy, round(floor_z + size[2] / 2.0, 4)]
    out["size"] = [round(v, 4) for v in size]
    # The registry already used the right formula per shape; rotation does not change a
    # volume, so the only guard needed is that the number never exceeds the box written
    # (tools/validate.py warns when it does, and it would be right to).
    out["volume_ul"] = round(min(float(entry["volume_ul"]), _box(out["size"])), 4)
    return out


def _place_channel(entry: dict, placed: Placed) -> dict:
    """A part-internal channel, with its centreline rotated into chip coordinates."""
    out = dict(entry)
    if entry.get("path"):
        out["path"] = [list(placed.to_chip(point)) for point in entry["path"]]
    return out


def thermal_zones(parts: Sequence[SizedPart]) -> list[dict]:
    """One zone per temperature-holding OP, naming the chambers of the part it runs in.

    A merged part (one trap doing lysis, washes and a reverse-transcription hold) gets one
    zone PER HOLD, all naming the same chamber, because that is what the chip has to do:
    hold the same bed at 55 C and then at 50 C. Exactly the idiom
    chip/module3_v1_translated.json uses for its trap.
    """
    out = []
    for part in parts:
        brief = part.brief
        chambers = [c["id"] for c in part.chambers]
        if not chambers:
            continue
        for hold in brief.temp_holds:
            zone: dict[str, Any] = {
                "id": f"{hold['kind']}_{hold['op']}_zone",
                "chambers": chambers,
                "max_temp_c": float(hold["temp_c"]),
                "purpose": hold["purpose"],
            }
            if hold["kind"] == "thermocycle":
                # A thermocycled zone swings, and the low end matters to the material too.
                zone["min_temp_c"] = float(contracts.param("culture.temp_c"))
            out.append(zone)
    return out


def build_chip_spec(*, chip_id: str, parts: Sequence[SizedPart], placement: Placement,
                    port_plan: PortPlan, material: Material, scale: str, reason: str = "",
                    unresolved: Sequence[dict] = (), registry: str = "",
                    inline: Sequence = (), name: str | None = None) -> tuple[dict, list[dict]]:
    """The chip spec, and the `unresolved` entries S6 should report.

    Channels BETWEEN parts stay empty on purpose: routing is S7's.
    """
    briefs = [p.brief for p in parts]
    depth = contracts.max_depth_mm()
    env = placement.envelope
    convention = layer_convention(depth)
    gaps: list[dict] = [dict(u) for u in unresolved]
    gaps.append({
        "chip_id": chip_id,
        "field": "layers",
        "question": "base and lid thickness are mechanical choices and are in no contract",
        "assumed": {"base_mm": convention.base_mm, "lid_mm": convention.lid_mm},
        "assumed_from": convention.source,
    })

    floor_z = convention.base_mm
    top_z = convention.base_mm + depth + convention.lid_mm

    chambers: list[dict] = []
    channels: list[dict] = []
    for part in parts:
        placed = placement.by_id(part.part_id)
        for entry in part.chambers:
            chambers.append(_place_chamber(entry, placed, floor_z))
        for entry in part.channels:
            channels.append(_place_channel(entry, placed))
    for port in port_plan.ports:
        chambers.append(port.pad(floor_z))

    spec: dict[str, Any] = {
        "id": chip_id,
        "name": name or f"{chip_id}: {', '.join(dict.fromkeys(b.kind for b in briefs))}",
        "description": _description(chip_id, parts, placement, port_plan, material, scale,
                                    reason, convention, registry, inline),
        "placeholder": any(b.gaps for b in briefs) or bool(port_plan.unresolved),
        "units": "mm",
        "footprint": {"x": env.x_mm, "y": env.y_mm},
        "layers": [
            {"id": "base", "material": material.spec_name, "thickness_mm": convention.base_mm,
             "note": f"{material.name}: {material.why}"},
            {"id": "fluidic", "material": material.spec_name, "thickness_mm": depth,
             "note": f"the ONE fluidic layer, at the one etch depth "
                     f"(contracts/blocks.json envelope.max_depth_mm = {depth:g} mm)"},
            {"id": "lid", "material": material.spec_name, "thickness_mm": convention.lid_mm,
             "note": "bonded lid; the ports are drilled through it onto their pads"},
        ],
        "chambers": chambers,
        "channels": channels,
        "ports": [p.spec(top_z) for p in port_plan.ports],
        "generator": {
            "script": STAGE,
            "params": {
                "scale": scale,
                "envelope_mm": [env.x_mm, env.y_mm],
                "etch_depth_mm": depth,
                "rows": placement.rows,
                "rotated_parts": sum(1 for p in placement.placed if p.rotation),
                "chain_length_mm": placement.chain_length_mm,
                "crossing_risk": placement.crossing_risk,
                "ops": [n for b in briefs for n in b.ops],
                "blocks": {p.part_id: p.block for p in parts},
                "ops_per_part": {p.part_id: list(p.brief.ops) for p in parts},
                "sized_from": {p.part_id: p.brief.load_basis for p in parts},
                "ops_realised_as_channels": [n for b in inline for n in b.ops],
                "part_registry": registry,
                "material_choice": material.why,
                "layer_convention": convention.source,
                "routed": False,
            },
        },
    }
    zones = thermal_zones(parts)
    if zones:
        spec["thermal_zones"] = zones

    capsule_parts = [p for p in parts if p.brief.particles == "capsules"]
    if capsule_parts:
        spec["capsule"] = {
            "diameter_um": contracts.capsule_diameter_um(),
            "swell_factor": contracts.capsule_swell_factor(),
        }
        counts = [p.brief.particle_count for p in capsule_parts if p.brief.particle_count]
        if counts:
            spec["capsule"]["count"] = max(counts)
    weirs = [c["retention"]["opening_um"] for c in chambers
             if (c.get("retention") or {}).get("type") == "weir"]
    if weirs:
        spec["trap"] = {"gap_um": min(weirs)}

    if schema_has_scale():
        spec["scale"] = scale
    else:
        gaps.append({
            "chip_id": chip_id, "field": "scale",
            "question": f"this chip was built to the {scale} envelope "
                        f"({env.x_mm:g} x {env.y_mm:g} mm), but "
                        f"contracts/schemas/chip_spec.schema.json has no top-level `scale` "
                        f"field yet, so the spec cannot declare it",
            "assumed": scale,
            "assumed_from": f"partition.json chips[].scale -> contracts.envelope('{scale}')",
        })
    for part in parts:
        gaps.extend(dict(g, chip_id=chip_id) for g in part.brief.gaps)
        for refusal in part.tried:
            if refusal.get("sized"):
                continue
            gaps.append({
                "chip_id": chip_id, "op": part.brief.op,
                "field": f"blocks.{refusal['block']}",
                "question": f"op {part.brief.op} ({part.brief.kind}) would have preferred "
                            f"block '{refusal['block']}', and the registry refused: "
                            f"{refusal.get('why', '')}",
                "assumed": part.block,
                "assumed_from": "assay2chip/parts/registry.py try_size()",
            })
        for assumption in part.geometry.assumptions:
            gaps.append({
                "chip_id": chip_id, "op": part.brief.op,
                "field": f"parts.{part.part_id}",
                "question": f"part '{part.part_id}' ({part.block}) rests on an assumption: "
                            f"{assumption}",
                "assumed": None, "assumed_from": "; ".join(part.geometry.provenance),
            })
    gaps.extend(port_plan.unresolved)
    return spec, gaps


def _description(chip_id: str, parts: Sequence[SizedPart], placement: Placement,
                 port_plan: PortPlan, material: Material, scale: str, reason: str,
                 convention: LayerConvention, registry: str,
                 inline: Sequence = ()) -> str:
    env = placement.envelope
    briefs = [p.brief for p in parts]
    ops = ", ".join(
        f"op{'s' if p.brief.merged else ''} "
        f"{', '.join(f'{n} {k}' for n, k in zip(p.brief.ops, p.brief.kinds))} -> "
        f"{p.block} '{p.part_id}'" for p in parts)
    merged = [p for p in parts if p.brief.merged]
    sizing = "; ".join(f"{p.part_id} from {p.brief.volume_source}" for p in parts)
    rotated = [p.part.part_id for p in placement.placed if p.rotation]
    hottest = max((b.temp_c for b in briefs if b.temp_c is not None), default=None)
    pattern = ("left to right" if placement.rows == 1 else
               "alternating direction row by row, so the chain stays a single "
               "non-self-crossing path")
    risks = sorted({r for p in parts for r in p.geometry.risks})
    bits = [
        f"UNROUTED. Built by {STAGE} from partition.json: {ops}.",
        reason.strip(),
        f"{env.x_mm:g} x {env.y_mm:g} mm ({scale} envelope), ONE fluidic layer at ONE "
        f"{contracts.max_depth_mm():g} mm etch depth, base and lid "
        f"{convention.base_mm:g} / {convention.lid_mm:g} mm.",
        f"{material.name} because {material.why}."
        + (f" The hottest hold here is {hottest:g} C." if hottest is not None else ""),
        f"SIZED FROM THE LOAD, never from the paper's tube volumes "
        f"(assay2chip/README.md, 'Bench volumes do not transfer'): {sizing}.",
        (f"ONE PART PER PLACE (chip/designs/README.md rule 1): "
         + "; ".join(f"ops {', '.join(str(n) for n in p.brief.ops)} "
                     f"({', '.join(p.brief.kinds)}) all act on the same "
                     f"{p.brief.particles} in '{p.part_id}', so they share one chamber "
                     f"sized for the largest of their loads and held at each "
                     f"temperature in turn" for p in merged) + ".") if merged else "",
        f"PLACEMENT: parts sit in flow order in {placement.rows} row(s) ({pattern}), spread "
        f"across the usable {placement.usable[2] - placement.usable[0]:.1f} x "
        f"{placement.usable[3] - placement.usable[1]:.1f} mm so the slack between them is "
        f"routing corridor. Total chain length {placement.chain_length_mm:.1f} mm, crossing "
        f"risk {placement.crossing_risk}"
        + (f", rotated 90 deg: {', '.join(rotated)}" if rotated else ", nothing rotated")
        + f"; every feature keeps at least {contracts.floor('min_wall_mm'):g} mm to its "
          f"neighbours and to the chip edge.",
        f"PORTS: {len(port_plan.ports)} ({', '.join(p.id for p in port_plan.ports)}), each "
        f"over a PORT PAD the registry sized, so the bore opens entirely onto cut area and "
        f"the port-overlap rule in tools/check_chip_spec.py passes. Inlets and outlets are "
        f"on opposite edges, which is what keeps the layout planar in one layer, and no two "
        f"ports are closer than {port_plan.pitch_mm:g} mm.",
        f"CHANNELS BETWEEN PARTS ARE DELIBERATELY EMPTY: S7 routes them from "
        f"assay2chip/work/<id>/connections_{chip_id}.json. Until it has run, a chamber here "
        f"is unconnected on purpose, and every rule that needs a channel - channel width "
        f"against capsule diameter, channel crossings, protocol targets - can only be "
        f"checked after S7.",
        (f"REALISED AS CHANNELS, not as parts: "
         + "; ".join(f"op(s) {', '.join(str(n) for n in b.ops)} "
                     f"({', '.join(b.kinds)}) - {b.inline_why}" for b in inline)
         + ". A conduit is sized from flow and residence time, and one that comes out "
           "smaller than a face a channel can attach to is not a small part but an "
           "unusable one, so it becomes the channel instead, lengthened to the length it "
           "needs.") if inline else "",
        f"Parts sized by {registry}." if registry else "",
        ("RISKS carried from sizing: " + "; ".join(risks) + ".") if risks else "",
    ]
    return " ".join(b for b in bits if b)

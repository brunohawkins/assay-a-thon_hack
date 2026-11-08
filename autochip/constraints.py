"""Load a constraints file (preset id or path), apply 'extends', fill defaults, validate.

Every stage takes the resolved dict. A missing field or null means 'no limit'. Helpers here
give the numbers stages need so nobody re-implements the defaults.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRESETS = Path(__file__).resolve().parent / "constraints"
SCHEMA = ROOT / "contracts" / "schemas" / "constraints.schema.json"
DEFAULT = "unconstrained"


class ConstraintsError(ValueError):
    pass


def list_presets() -> list[str]:
    return sorted(p.stem for p in PRESETS.glob("*.json"))


def _merge(base: dict, over: dict) -> dict:
    """Deep merge for 'extends': objects merge key by key; anything else in `over` (a list, a number, a
    string, null) replaces the base value. So a list is replaced, not appended to, and null clears a
    preset's limit ('no limit')."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _resolve(name_or_path: str | Path, base_dir: Path | None = None) -> Path:
    """A preset id (no suffix) or a path. A relative path in 'extends' is relative to the file that
    extends it, then to the working directory."""
    p = Path(name_or_path)
    if not p.suffix:
        return PRESETS / f"{name_or_path}.json"
    if not p.is_absolute() and base_dir is not None and (base_dir / p).exists():
        return base_dir / p
    return p


def _read(name_or_path: str | Path, base_dir: Path | None = None) -> tuple[dict, Path]:
    p = _resolve(name_or_path, base_dir)
    if not p.is_file():
        raise ConstraintsError(f"no constraints file or preset {str(name_or_path)!r} (presets: {', '.join(list_presets())})")
    try:
        c = json.loads(p.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ConstraintsError(f"{p} is not valid JSON: {e}") from e
    if not isinstance(c, dict):
        raise ConstraintsError(f"{p} must hold one JSON object, not a {type(c).__name__}")
    return c, p


def load(name_or_path: str | Path | None = None) -> dict:
    c, path = _read(name_or_path or DEFAULT)
    seen = {path.resolve()}
    while c.get("extends") is not None:
        parent = c.pop("extends")
        if not isinstance(parent, str) or not parent:
            raise ConstraintsError(f"'extends' in {path} must be a preset id or a file path, not {parent!r}")
        base, ppath = _read(parent, path.parent)
        if ppath.resolve() in seen:
            raise ConstraintsError(f"'extends' loop: {path} extends {parent}, which is already in the chain")
        seen.add(ppath.resolve())
        c, path = _merge(base, c), ppath
    validate(c)
    return c


def validate(c: dict) -> None:
    """Schema, then settings that contradict each other (see consistency_errors)."""
    import jsonschema
    errs = sorted(jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text())).iter_errors(c), key=lambda e: list(e.path))
    if errs:
        raise ConstraintsError("constraints invalid:\n" + "\n".join(f"  {'/'.join(map(str, e.path)) or '(top)'}: {e.message}" for e in errs))
    bad = consistency_errors(c)
    if bad:
        raise ConstraintsError(f"constraints '{c.get('id')}' contradict themselves:\n" + "\n".join(f"  {m}" for m in bad))


def consistency_errors(c: dict) -> list[str]:
    """Settings the schema allows one by one but that can't all hold. Only contradictions that would
    change what the stages do; leftovers that are simply ignored (a valve type while valves are off,
    vias while multilayer is off) are fine, so turning a preset feature off with 'extends' stays one line."""
    out = []
    if get(c, "fabrication.channel_crossings_allowed") and not multilayer(c):
        out.append("fabrication.channel_crossings_allowed is true but fabrication.multilayer.allowed is not: a "
                   "crossing needs a second fluidic layer (true-3D processes too), so set crossings to false or "
                   "allow multilayer")
    dmin, dmax = get(c, "fabrication.depth.min_mm"), get(c, "fabrication.depth.max_mm")
    if dmin and dmax and dmin > dmax:
        out.append(f"fabrication.depth.min_mm ({dmin}) is larger than depth.max_mm ({dmax})")
    allowed = get(c, "fabrication.depth.allowed_mm") or []
    off = [d for d in allowed if (dmax and d > dmax) or (dmin and d < dmin)]
    if off:
        out.append(f"fabrication.depth.allowed_mm has {off}, outside depth.min_mm..max_mm ({dmin}..{dmax})")
    pa, mc = get(c, "envelope.printable_area_mm"), get(c, "envelope.max_chip_mm")
    if pa and mc and pa.get("x") and pa.get("y") and mc.get("x") and mc.get("y"):
        if not ((mc["x"] <= pa["x"] and mc["y"] <= pa["y"]) or (mc["x"] <= pa["y"] and mc["y"] <= pa["x"])):
            out.append(f"envelope.max_chip_mm ({mc['x']} x {mc['y']}) does not fit the printable area "
                       f"({pa['x']} x {pa['y']})")
    if pa and pa.get("z") and dmax and dmax >= pa["z"]:
        out.append(f"fabrication.depth.max_mm ({dmax}) is not smaller than the build height printable_area_mm.z ({pa['z']})")
    x, y = max_chip_mm(c)
    margin = get(c, "envelope.edge_margin_mm") or 0
    if x and y and 2 * margin >= min(x, y):
        out.append(f"envelope.edge_margin_mm ({margin}) on both sides leaves nothing of a {x} x {y} mm chip")
    dia, pitch = get(c, "ports.min_diameter_mm"), get(c, "ports.min_pitch_mm")
    if dia and pitch is not None and pitch < dia:
        out.append(f"ports.min_pitch_mm ({pitch}) is smaller than ports.min_diameter_mm ({dia}): neighbouring "
                   f"port holes would overlap (pitch is centre to centre)")
    for path in ("off_chip.flow_range_ul_min", "operating.temp_range_c"):
        r = get(c, path)
        if r and r[0] > r[1]:
            out.append(f"{path} {r} runs from high to low: give [low, high]")
    return out


def get(c: dict, path: str, default=None):
    """get(c, 'fabrication.min_feature.wall_mm', 0.1) -> value or default when missing/null."""
    cur = c
    for k in path.split("."):
        if not isinstance(cur, dict) or cur.get(k) is None:
            return default
        cur = cur[k]
    return cur


def max_chip_mm(c: dict) -> tuple[float | None, float | None]:
    m = get(c, "envelope.max_chip_mm") or get(c, "envelope.printable_area_mm") or {}
    return m.get("x"), m.get("y")


def multilayer(c: dict) -> bool:
    return bool(get(c, "fabrication.multilayer.allowed", False))


def crossings_allowed(c: dict) -> bool:
    return bool(get(c, "fabrication.channel_crossings_allowed", multilayer(c)))


def min_channel_mm(c: dict) -> float:
    """Narrowest channel we may draw. Printed-closed processes are limited by the enclosed-channel figure;
    print-open-and-seal (film, lid, bond) by the open-channel figure."""
    open_w = get(c, "fabrication.min_feature.open_channel_width_mm")
    closed_w = get(c, "fabrication.min_feature.enclosed_channel_width_mm")
    if get(c, "fabrication.sealing") == "printed_closed" or get(c, "fabrication.method") == "glass_laser_3d_etch":
        return closed_w or open_w or 0.05
    return open_w or closed_w or 0.05


def _mm(v) -> str:
    return "no limit" if v is None else f"{v:g} mm" if isinstance(v, (int, float)) else str(v)


def summary(c: dict) -> str:
    """Plain-English summary for agent prompts and reports. Every field a stage or check reads is here,
    and so are the ones only the agents can act on (operating needs, connector, parts lists)."""
    fab = c.get("fabrication", {})
    x, y = max_chip_mm(c)
    pa = get(c, "envelope.printable_area_mm") or {}
    depth = [f"max {_mm(get(c, 'fabrication.depth.max_mm'))}" if get(c, "fabrication.depth.max_mm") else "no maximum"]
    if get(c, "fabrication.depth.min_mm"):
        depth.append(f"min {_mm(get(c, 'fabrication.depth.min_mm'))}")
    if get(c, "fabrication.depth.allowed_mm"):
        depth.append("only these depths: " + ", ".join(f"{d:g}" for d in get(c, "fabrication.depth.allowed_mm")) + " mm")
    if get(c, "fabrication.depth.single_depth"):
        depth.append("one depth per layer")
    mf = lambda k: _mm(get(c, f"fabrication.min_feature.{k}"))  # noqa: E731
    ports = [f"max {get(c, 'ports.max_per_chip')} per chip" if get(c, "ports.max_per_chip") else "any number per chip", f"min diameter {_mm(get(c, 'ports.min_diameter_mm'))}",
             f"min pitch {_mm(get(c, 'ports.min_pitch_mm'))}"]
    if get(c, "ports.connector"):
        ports.append(f"connector {get(c, 'ports.connector')}")
    if get(c, "ports.placement"):
        ports.append(f"placed on the {get(c, 'ports.placement')}" if get(c, "ports.placement") != "any" else "placed anywhere")
    lines = [f"Constraints '{c['id']}': {c.get('name', '')}",
             f"- Fabrication: {fab.get('method')}" + (f" on {fab['machine']}" if fab.get("machine") else "")
             + (f", material {fab['material']}" if fab.get("material") else "") + (f", sealed by {fab['sealing']}" if fab.get("sealing") else "")
             + (f" ({fab['method_note']})" if fab.get("method_note") else ""),
             f"- Multilayer: {'allowed' + (' (max ' + str(get(c, 'fabrication.multilayer.max_fluidic_layers')) + ' fluidic layers)' if get(c, 'fabrication.multilayer.max_fluidic_layers') else '') + (', vias between layers' if get(c, 'fabrication.multilayer.vias_allowed') else ', no vias') if multilayer(c) else 'NOT allowed: one fluidic layer'}",
             f"- Channel crossings: {'allowed' if crossings_allowed(c) else 'NOT allowed (planar layout)'}",
             f"- Thermal zones on one chip: {'allowed' if get(c, 'fabrication.thermal_zoning_allowed') else 'NOT allowed: clashing temperatures go on different chips' if get(c, 'fabrication.thermal_zoning_allowed') is False else 'not stated (assume no: clashing temperatures go on different chips)'}",
             f"- On-chip valves: {('allowed, ' + str(get(c, 'fabrication.on_chip_valves.type', 'type not stated'))) if get(c, 'fabrication.on_chip_valves.allowed') else 'NOT allowed: switch flow off-chip (which pump pushes, which port is capped)'}",
             f"- Max chip footprint: {f'{x:g} x {y:g} mm' if x and y else 'no limit'}"
             + (f" (build area {pa['x']:g} x {pa['y']:g}" + (f" x {pa['z']:g}" if pa.get('z') else "") + " mm)" if pa.get("x") and pa.get("y") else "")
             + f"; edge margin {_mm(get(c, 'envelope.edge_margin_mm'))}; max chips: {get(c, 'envelope.max_chips') or 'no limit'}",
             "- Depth: " + ", ".join(depth),
             f"- Smallest features: open channel {mf('open_channel_width_mm')}, enclosed channel {mf('enclosed_channel_width_mm')}, "
             f"wall {mf('wall_mm')}, gap {mf('gap_mm')}"
             + (f"; depth/width at most {get(c, 'fabrication.min_feature.max_aspect_ratio'):g}" if get(c, "fabrication.min_feature.max_aspect_ratio") else ""),
             "- Ports: " + ", ".join(ports),
             f"- Parts: {get(c, 'parts.source', 'library_and_parametric')}" + (" (library parts may be scaled)" if get(c, 'parts.allow_scaling') else "")
             + (f"; only: {', '.join(get(c, 'parts.allowed'))}" if get(c, "parts.allowed") else "")
             + (f"; never: {', '.join(get(c, 'parts.forbidden'))}" if get(c, "parts.forbidden") else ""),
             f"- Off-chip equipment available: {', '.join(get(c, 'off_chip.equipment', [])) or 'not stated'}; pump lines: {get(c, 'off_chip.pump_channels') or 'no limit'}"
             + (f" ({get(c, 'off_chip.flow_range_ul_min')[0]:g}-{get(c, 'off_chip.flow_range_ul_min')[1]:g} uL/min)" if get(c, "off_chip.flow_range_ul_min") else "")
             + f"; manual bench steps {'allowed' if get(c, 'off_chip.manual_steps_allowed', True) else 'NOT allowed'}",
             f"- Objective: {get(c, 'objectives.prefer', 'most_on_chip')}"]
    if get(c, "operating.temp_range_c"):
        lo, hi = get(c, "operating.temp_range_c")
        lines.append(f"- Operating temperature range: {lo:g} to {hi:g} C")
    if get(c, "operating.max_pressure_kpa"):
        lines.append(f"- Max pressure in a chip: {get(c, 'operating.max_pressure_kpa'):g} kPa")
    needs = [w for k, w in (("biocompatible_required", "biocompatible materials"), ("optical_window_required", "an optical window for imaging"),
                            ("sterilisable", "to survive sterilisation")) if get(c, f"operating.{k}")]
    if needs:
        lines.append(f"- Chips need: {', '.join(needs)}")
    for n in (c.get("notes") or []) + (get(c, "objectives.notes") or []):
        lines.append(f"- Note: {n}")
    return "\n".join(lines)

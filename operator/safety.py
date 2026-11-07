"""Safety checker: deterministic rules every action must pass. Never the LLM.

    check(command, args, believed_state, chip_spec, model=None) -> SafetyResult
    SafetyResult.status: "approved" | "warning" | "blocked", with readable reasons.

The AI (or planner) PROPOSES, the model SIMULATES, this module APPROVES. Rules:
  1. target chamber or port doesn't exist                                  -> blocked
  2. predicted pressure would push capsules through the retention opening  -> blocked
     (>= capsule.escape_pressure_kpa); above control.pressure_margin of it  -> warning
  3. waste or collection would overflow                                    -> blocked
  4. a reagent reservoir would run dry                                     -> blocked (<10% spare: warning)
  5. temperature above the chip material's max working temperature         -> blocked
  6. release or pool while capsules are still on a mesh (back-flush first)  -> blocked
Thresholds come from contracts/parameters.json and contracts/materials.json.
Commands here are whole plans (volume_ul per wash x washes), not time slices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import schema_io
from model import ModelParams, ProcessModel
from state import ChipState

ROOT = schema_io.ROOT
_cache: dict = {}


def _contracts() -> tuple[dict, dict]:
    if "params" not in _cache:
        _cache["params"] = schema_io.load_json(ROOT / "contracts" / "parameters.json")
        _cache["materials"] = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
    return _cache["params"], _cache["materials"]


@dataclass
class SafetyResult:
    status: str                       # approved | warning | blocked
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status != "blocked"

    def to_dict(self) -> dict:
        return {"result": self.status, "reasons": list(self.reasons)}


def Approved() -> SafetyResult:  # noqa: N802 (names from the design doc)
    return SafetyResult("approved")


def Blocked(reason: str) -> SafetyResult:  # noqa: N802
    return SafetyResult("blocked", [reason])


def Warning(reason: str) -> SafetyResult:  # noqa: N802,A001
    return SafetyResult("warning", [reason])


def total_volume(command: str, args: dict, model: ProcessModel) -> float:
    if command == "exchange":
        return float(args["volume_ul"]) * int(args.get("washes", 1))
    if command == "load":
        return float(args.get("volume_ul", 0))
    if command == "release":
        return float(args.get("volume_ul", model.params.chamber_volume_ul.get(args["target"], 0) * 4))
    return 0.0


def material_limit(chip: dict, materials: dict) -> tuple[float | None, str | None, list[str]]:
    """Lowest max working temperature over the chip's layer materials."""
    best, name, unknown = None, None, []
    for layer in chip.get("layers", []):
        mat = str(layer.get("material", "")).lower()
        hit = next((m for mid, m in materials.items() if mat == mid or mat in [a.lower() for a in m.get("aliases", [])]), None)
        if hit is None:
            unknown.append(mat)
        elif best is None or hit["max_working_temp_c"] < best:
            best, name = hit["max_working_temp_c"], hit["name"]
    return best, name, unknown


def check(command: str, args: dict, believed: ChipState, chip: dict, model: ProcessModel | None = None) -> SafetyResult:
    params, materials = _contracts()
    p = params["parameters"]
    if model is None:
        model = ProcessModel(ModelParams.from_chip(chip, params))
    blocked: list[str] = []
    warnings: list[str] = []
    chambers = {c["id"] for c in chip["chambers"]}
    places = chambers | {pt["id"] for pt in chip.get("ports", [])}

    # 1. ids exist
    ids = [args.get(k) for k in ("target", "source") if args.get(k)] + list(args.get("targets", [])) + list(args.get("sources", []))
    missing = [i for i in ids if i not in places]
    if missing:
        return Blocked(f"{', '.join(missing)} not in chip {chip['id']}")
    for i in ids:
        if i not in chambers:
            return Blocked(f"'{i}' is a port; {command} acts on chambers")

    target = args.get("target")
    vol = total_volume(command, args, model)

    # 2. pressure at the retention
    if command in ("exchange", "load") and args.get("direction") != "backflush" and target:
        ch = believed.chambers[target]
        if ch.has_retention:
            future = believed.copy()
            if command == "load":
                future.chambers[target].capsules_on_mesh += args.get("capsules", 0)
            q = float(args.get("flow_ul_min", 20.0))
            pressure = model.mesh_pressure(future, target, q)
            limit = model.params.escape_pressure_kpa
            margin = p["control.pressure_margin"]["value"]
            if pressure >= limit:
                blocked.append(f"predicted {pressure:.2f} kPa at the mesh >= escape pressure {limit:g} kPa: "
                               f"capsules would be pushed through the retention opening (lower the flow below "
                               f"{limit / max(pressure / q, 1e-9):.0f} uL/min)")
            elif pressure >= margin * limit:
                warnings.append(f"predicted {pressure:.2f} kPa is above {margin:.0%} of the {limit:g} kPa escape pressure")

    # 3. waste / collection overflow
    if command in ("exchange", "load") and vol > 0 and believed.waste_ul + vol > believed.waste_capacity_ul:
        blocked.append(f"waste would overflow: {believed.waste_ul:.0f} + {vol:.0f} uL > {believed.waste_capacity_ul:.0f} uL capacity")
    if command == "release" and believed.collection_ul + vol > believed.collection_capacity_ul:
        blocked.append(f"collection would overflow: {believed.collection_ul:.0f} + {vol:.0f} uL > {believed.collection_capacity_ul:.0f} uL")

    # 4. reservoir would run dry
    reagent = args.get("reagent") or ("capsule_suspension" if command == "load" else None)
    if reagent and vol > 0 and reagent in believed.reservoirs_ul:
        left = believed.reservoirs_ul[reagent]
        if left < vol:
            blocked.append(f"reservoir '{reagent}' would run dry: needs {vol:.0f} uL, {left:.0f} uL left")
        elif left < 1.1 * vol:
            warnings.append(f"reservoir '{reagent}' is nearly empty: {left:.0f} uL left for {vol:.0f} uL")

    # 5. temperature vs material
    if command == "hold_temp":
        limit, name, unknown = material_limit(chip, materials)
        if limit is not None and args["temp_c"] > limit:
            blocked.append(f"{args['temp_c']:g} C is above {name}'s max working temperature ({limit:g} C)")
        if unknown:
            warnings.append(f"material(s) {', '.join(unknown)} not in contracts/materials.json: temperature not checked")

    # 6. capsules still on the mesh
    if command in ("release", "pool"):
        sources = [target] if command == "release" else list(args.get("sources", []))
        for cid in sources:
            ch = believed.chambers.get(cid)
            if ch and ch.has_retention and ch.capsules_on_mesh > max(0.5, 0.01 * ch.capsules):
                blocked.append(f"{ch.capsules_on_mesh:,.0f} capsules are still on the mesh in '{cid}': back-flush first")

    if blocked:
        return SafetyResult("blocked", blocked + warnings)
    if warnings:
        return SafetyResult("warning", warnings)
    return Approved()

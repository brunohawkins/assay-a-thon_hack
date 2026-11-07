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
  7. opening a pair valve (the link between two culture chambers) in a run  -> blocked
     whose variant is "control": the control chip must stay isolated
  8. cells leaving Module 1b for Module 2 (check_cell_prep): viability below  -> blocked
     qc.viability_min (90%, 10x), or a concentration that misses ~0.1 cells per capsule at
     Module 2's flows (sizing.py) by more than cellprep.mean_cells_tolerance

check_static(command, args, chip, run_params) applies the rules that need no chip state
(ids, temperature, valves); the simple planner calls it for every command, and check()
includes it.
Thresholds come from contracts/parameters.json and contracts/materials.json.
Commands here are whole plans (volume_ul per wash x washes), not time slices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import schema_io
import sizing
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
        # A release with no stated volume is sized like one wash: wash.chamber_volumes_per_wash
        # chamber volumes, from contracts/parameters.json. This used to be a literal 4 that no
        # longer matched the contract's 5, so the overflow rule under-counted every such release.
        params, _ = _contracts()
        per_wash = params["parameters"]["wash.chamber_volumes_per_wash"]["value"]
        return float(args.get("volume_ul", model.params.chamber_volume_ul.get(args["target"], 0) * per_wash))
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


def check_static(command: str, args: dict, chip: dict, run_params: dict | None = None) -> SafetyResult:
    """Rules that need no believed state: valve ids, the control-variant pair-valve rule,
    and temperatures against the chip material."""
    _, materials = _contracts()
    blocked: list[str] = []
    warnings: list[str] = []
    valves = {v["id"]: v for v in chip.get("valves", [])}
    if command == "valve":
        missing = [v for v in args.get("valves", []) if v not in valves]
        if missing:
            return Blocked(f"{', '.join(missing)} not a valve in chip {chip['id']}")
        variant = (run_params or {}).get("variant")
        pairs = [v for v in args["valves"] if valves[v]["kind"] == "pair"]
        if args.get("state") == "open" and pairs and variant == "control":
            blocked.append(f"opening {', '.join(pairs)} is not allowed in the control variant: the control chip's "
                           f"chambers must stay isolated (pair valves stay closed)")
    # Any command carrying a temperature is checked, not just hold_temp and culture: a PCR
    # step at 98 C is still 98 C against the chip material, whatever the command is called.
    temp = args.get("temp_c")
    if temp is not None:
        limit, name, unknown = material_limit(chip, materials)
        if limit is not None and temp > limit:
            blocked.append(f"{temp:g} C is above {name}'s max working temperature ({limit:g} C)")
        if unknown:
            warnings.append(f"material(s) {', '.join(unknown)} not in contracts/materials.json: temperature not checked")
    if blocked:
        return SafetyResult("blocked", blocked + warnings)
    if warnings:
        return SafetyResult("warning", warnings)
    return Approved()


def cell_prep_target(m2_chip: dict) -> dict:
    """What Module 2 needs from Module 1b, at Module 2's flows (from its chip spec)."""
    params, _ = _contracts()
    p = params["parameters"]
    enc = m2_chip.get("encapsulation", {})
    core = enc.get("core_flow_ul_h", p["encapsulation.cell_flow_ul_h"]["value"])
    s = sizing.compute({
        "capsule_diameter_um": m2_chip.get("capsule", {}).get("diameter_um", p["capsule.diameter_um"]["value"]),
        "cell_flow_ul_h": core,
        "shell_flow_ul_h": enc.get("shell_flow_ul_h", p["encapsulation.shell_flow_ul_h"]["value"]),
        "oil_flow_ul_h": enc.get("oil_flow_ul_h", p["encapsulation.oil_flow_ul_h"]["value"]),
    })
    mean = p["capsule.mean_cells_target"]["value"]
    tol = p["cellprep.mean_cells_tolerance"]["value"]
    return {"capsules_per_hour": s["capsules_per_hour"], "core_flow_ul_h": core, "target_mean": mean,
            "mean_low": mean * (1 - tol), "mean_high": mean * (1 + tol),
            "target_cells_per_ul": mean * s["capsules_per_hour"] / core,
            "viability_min": p["qc.viability_min"]["value"], "max_dilution": p["cellprep.max_dilution"]["value"]}


def mean_cells_per_capsule(cells_per_ul: float, target: dict) -> float:
    return cells_per_ul * target["core_flow_ul_h"] / target["capsules_per_hour"]


def check_cell_prep(cells_per_ul: float, viability: float, m2_chip: dict) -> SafetyResult:
    """Rule 8: may these cells go into Module 2's core inlet?"""
    t = cell_prep_target(m2_chip)
    mean = mean_cells_per_capsule(cells_per_ul, t)
    blocked = []
    if viability < t["viability_min"]:
        blocked.append(f"viability {viability:.0%} is below {t['viability_min']:.0%} (10x guidance): don't encapsulate "
                       f"these cells")
    if mean > t["mean_high"]:
        blocked.append(f"too concentrated - dilute {mean / t['target_mean']:.1f}x: {cells_per_ul:,.0f} cells/uL gives "
                       f"{mean:.2f} cells per capsule at Module 2's flows (target {t['target_mean']:g})")
    elif mean < t["mean_low"]:
        # mean is 0 when cells_per_ul is 0: there is nothing to concentrate, so say so
        # rather than dividing by it.
        how = "no cells to concentrate" if mean <= 0 else f"concentrate {t['target_mean'] / mean:.1f}x"
        blocked.append(f"too dilute - {how}: {cells_per_ul:,.0f} cells/uL gives "
                       f"{mean:.3f} cells per capsule at Module 2's flows (target {t['target_mean']:g})")
    if blocked:
        return SafetyResult("blocked", blocked)
    return SafetyResult("approved", [f"{cells_per_ul:,.0f} cells/uL -> {mean:.3f} cells per capsule at Module 2's flows "
                                     f"(target {t['target_mean']:g}); viability {viability:.0%}"])


def check(command: str, args: dict, believed: ChipState, chip: dict, model: ProcessModel | None = None,
          run_params: dict | None = None) -> SafetyResult:
    if command == "valve":
        return check_static(command, args, chip, run_params)
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
            # The flow must be stated. A default here approved the command against a pressure
            # computed at a flow the run never uses, which is worse than refusing to judge it.
            if args.get("flow_ul_min") is None:
                return Blocked(f"{command} on '{target}' gives no flow_ul_min: the mesh pressure rule cannot be "
                               f"checked, and '{target}' has a retention that capsules can be pushed through")
            q = float(args["flow_ul_min"])
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
    if reagent and vol > 0:
        if reagent not in believed.reservoirs_ul:
            # Unknown names used to skip the rule entirely, so a typo or a renamed reagent
            # walked straight past it. An unknown reservoir cannot be shown to hold enough.
            blocked.append(f"reagent '{reagent}' has no reservoir in the believed state "
                           f"(known: {', '.join(sorted(believed.reservoirs_ul)) or 'none'}): "
                           f"cannot tell whether it would run dry - check the name")
        else:
            left = believed.reservoirs_ul[reagent]
            if left < vol:
                blocked.append(f"reservoir '{reagent}' would run dry: needs {vol:.0f} uL, {left:.0f} uL left")
            elif left < 1.1 * vol:
                warnings.append(f"reservoir '{reagent}' is nearly empty: {left:.0f} uL left for {vol:.0f} uL")

    # 5. temperature vs material, for any command that carries one (not just hold_temp)
    if args.get("temp_c") is not None:
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

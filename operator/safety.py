"""Safety checker: deterministic rules every action must pass. Never the LLM.

    check(command, args, believed_state, chip_spec, model=None, run_params=None) -> SafetyResult
    SafetyResult.status: "approved" | "warning" | "blocked", with readable reasons.

The AI (or planner) PROPOSES, the model SIMULATES, this module APPROVES. Rules:
  1. target chamber or port doesn't exist                                  -> blocked
  2. predicted pressure at the retention, at the WORST point of the plan and in either
     direction (a back-flush pushes just as hard, upwards), would push capsules through
     the opening (>= capsule.escape_pressure_kpa)                           -> blocked
     above control.pressure_margin of it                                    -> warning
  3. waste or collection would overflow                                    -> blocked
  4. a reagent reservoir would run dry                                     -> blocked (<10% spare: warning)
  5. temperature above the chip material's max working temperature         -> blocked,
     for ANY command that carries a temp_c (hold_temp and culture both do)
  6. release or pool while capsules are still on a mesh (back-flush first)  -> blocked
  7. an action that joins two culture chambers in a run whose variant is    -> blocked
     "control": the control chip's lanes must stay isolated. (This used to be phrased as
     "don't open a pair valve". On-chip valves need a second fluidic layer and were
     removed, so the rule is now written against what can actually join two lanes: one
     medium line feeding several culture chambers, or pooling them.)
  8. cells leaving a cell-prep chip for Module 2 (check_cell_prep): viability below  -> blocked
     qc.viability_min (90%, 10x), or a concentration that misses ~0.1 cells per capsule at
     Module 2's flows (sizing.py) by more than cellprep.mean_cells_tolerance
  9. wall shear above the ceiling that applies to what the chamber HOLDS (see
     chamber_contents and shear_ceiling below)                              -> blocked

check_static(command, args, chip, run_params) applies the rules that need no chip state
(ids, temperatures, control-variant isolation); the simple planner calls it for every
command, and check() calls it too, so a rule added there is live in both paths.
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


# --- what a chamber holds, and therefore which ceiling binds it ------------------------
#
# contracts/parameters.json defines three ceilings and they are SCOPED. Applying the wrong
# one is not conservative, it is wrong in both directions: 0.01 dyn/cm2 on a capsule
# chamber makes every wash ~6x slower for no benefit (the contract says so in
# shear.culture_ceiling_dyn_cm2's own note, and forbids it), while 0.3 dyn/cm2 on an
# adherent culture would wash the cells off the floor. So the chamber is CLASSIFIED first
# and judged second, and a chamber we cannot classify is BLOCKED rather than guessed.

# A chamber whose liquid is a culture medium is a culture chamber: the word lists are the
# ones tools/check_chip_spec.py and tests/test_design_rules.py already read protocols with.
CULTURE_WORDS = ("culture", "media", "medium")
LYSE_WORDS = ("lysis", "lyse", "quench", "solubilisation")
RINSE_WORDS = ("rinse",)
CAPSULE_WORDS = ("capsule", "emulsion", "droplet", "bead")
# Chamber types that hold cells in SUSPENSION on a cell chip: nothing is attached, so the
# adherent-cell ceiling is not the bound (they are handled like a capsule: gentle, but by
# the pressure and the geometry, not by the detachment ceiling).
SUSPENSION_TYPES = ("mixer", "cell_wash", "qc_window", "filter", "junction", "bubble_trap")
ADHERENT_TYPES = ("culture",)


def is_capsule_chip(chip: dict) -> bool:
    """True when the chip's own spec says it carries capsules."""
    return bool(chip.get("capsule") or chip.get("trap") or chip.get("capsule_filter")
                or (chip.get("encapsulation") and not chip.get("cell_filter")))


def chamber_contents(chip: dict, chamber_id: str, believed: ChipState | None = None,
                     reagent: str | None = None) -> str:
    """What chamber_id holds: "capsules", "adherent_cells", "suspended_cells", "liquid"
    or "unknown". Structural only - the chip spec, the believed state and the reagent the
    command is pushing - so it works the same in the planner and in the closed loop.
    """
    chamber = next((c for c in chip.get("chambers", []) if c["id"] == chamber_id), None)
    if chamber is None:
        return "unknown"
    ctype = str(chamber.get("type", ""))
    if believed is not None:
        ch = believed.chambers.get(chamber_id)
        if ch is not None and ch.capsules > 0:
            return "capsules"                      # something is in there, and it is capsules
    if is_capsule_chip(chip):
        # On a capsule chip every chamber on the capsule path holds (or will hold)
        # capsules, and capsule.escape_pressure_kpa is their bound, not a shear ceiling.
        return "capsules"
    if ctype in ADHERENT_TYPES:
        return "adherent_cells"
    if ctype in SUSPENSION_TYPES:
        return "suspended_cells"
    if reagent and any(w in reagent.lower() for w in CAPSULE_WORDS):
        return "capsules"                          # what is being pushed in says what is in there
    if reagent and any(w in reagent.lower() for w in CULTURE_WORDS):
        return "adherent_cells"                    # a medium is flowing over cells that grow here
    # Deliberately NOT inferred from the retention: chip/module1_v1.json gives two of its
    # CULTURE chambers a 25 um weir, so "has a retention narrower than a capsule" would
    # have classified living cells as capsules and lifted the ceiling off them - the exact
    # mistake contracts/parameters.json warns about, in the dangerous direction.
    return "unknown"


def chamber_section_mm(chip: dict, chamber_id: str) -> tuple[float, float] | None:
    """(width, depth) of the chamber's wetted cross-section in mm, in the FAVOURABLE
    orientation: a spec does not say which way the liquid crosses a chamber, and the longer
    in-plane side gives the LOWEST shear, so a failure here fails in every orientation.
    Same convention as tests/test_design_rules.py and operator/network.py."""
    chamber = next((c for c in chip.get("chambers", []) if c["id"] == chamber_id), None)
    if chamber is None or len(chamber.get("size") or []) < 3:
        return None
    width = max(float(chamber["size"][0]), float(chamber["size"][1]))
    depth = float(chamber["height_um"]) / 1000.0 if chamber.get("height_um") else float(chamber["size"][2])
    if width <= 0 or depth <= 0:
        return None
    return width, depth


def shear_dyn_cm2(flow_ul_min: float, width_mm: float, depth_mm: float) -> float:
    """Wall shear of a wide slot: tau = 6 mu Q / (w h^2), in dyn/cm2 (1 Pa = 10 dyn/cm2).

    mu is fluid.viscosity_37c_pa_s (0.8 mPa s), the 37 C medium value the chips actually
    run - NOT fluid.viscosity_pa_s, which is water at 20 C and belongs to the dye demo;
    using it here would make every derived flow 20% too conservative (that parameter's own
    note says to keep the two apart). Same formula as operator/network.py and the
    generators, so the design, the drawing and this check cannot disagree.
    """
    params, _ = _contracts()
    mu = float(params["parameters"]["fluid.viscosity_37c_pa_s"]["value"])
    q = float(flow_ul_min) * 1e-9 / 60.0                      # uL/min -> m3/s
    return 10.0 * 6.0 * mu * q / ((width_mm / 1000.0) * (depth_mm / 1000.0) ** 2)


def shear_ceiling(contents: str, pre_lysis_rinse: bool) -> tuple[float | None, str]:
    """The ceiling that binds this chamber, and the contract key it came from.

    adherent cells          shear.culture_ceiling_dyn_cm2 (0.01): detaching them is the risk.
    a rinse just before a
    deliberate lyse         shear.prelysis_rinse_ceiling_dyn_cm2 (0.3): the cells are about
                            to be killed on purpose, and holding this rinse to the culture
                            ceiling takes ~12 minutes, which perturbs the measurement far
                            more than it protects it.
    capsules                NO shear ceiling: a capsule is attached to nothing. The bound is
                            capsule.escape_pressure_kpa across the retention, which rule 2
                            enforces. The contract forbids applying 0.01 here.
    cells in suspension,
    plain liquid            no shear ceiling either: nothing is attached.
    """
    params, _ = _contracts()
    p = params["parameters"]
    if contents == "adherent_cells":
        if pre_lysis_rinse:
            return p["shear.prelysis_rinse_ceiling_dyn_cm2"]["value"], "shear.prelysis_rinse_ceiling_dyn_cm2"
        return p["shear.culture_ceiling_dyn_cm2"]["value"], "shear.culture_ceiling_dyn_cm2"
    return None, "capsule.escape_pressure_kpa" if contents == "capsules" else "-"


def check_shear(command: str, args: dict, chip: dict, believed: ChipState | None = None,
                lyse_ahead: bool = False) -> SafetyResult:
    """Rule 9. `lyse_ahead` says this chamber's contents are deliberately killed later in
    the protocol, which is what makes a rinse a PRE-LYSIS rinse."""
    if command not in ("load", "exchange", "release", "culture", "mix", "harvest", "collect"):
        return Approved()
    flow = args.get("flow_ul_min")
    if flow is None or float(flow) <= 0:
        return Approved()                      # no flow stated: rule 2 handles the ones that need one
    reagent = (args.get("reagent") or (args.get("reagents") or [None])[0]
               or ("capsule_suspension" if command == "load" else None))
    if reagent and any(w in str(reagent).lower() for w in LYSE_WORDS):
        # The lyse/quench itself: these cells are being killed on purpose, so no ceiling
        # that exists to keep them alive and attached applies to this step.
        return Approved()
    targets = [t for t in ([args.get("target")] + list(args.get("targets") or [])) if t]
    blocked, warnings = [], []
    for cid in targets:
        contents = chamber_contents(chip, cid, believed, reagent)
        section = chamber_section_mm(chip, cid)
        if section is None:
            blocked.append(f"chamber '{cid}' has no usable cross-section in chip {chip.get('id')}: wall shear "
                           f"cannot be computed, so this flow cannot be approved")
            continue
        if contents == "unknown":
            # Which ceiling applies is unknown, so the only flow that can be approved is one
            # that passes the STRICTEST of them. Above it, refuse: say what is missing rather
            # than pick a ceiling and hope.
            strictest, key = shear_ceiling("adherent_cells", False)
            tau = shear_dyn_cm2(float(flow), *section)
            ctype = next((c.get("type") for c in chip["chambers"] if c["id"] == cid), "?")
            where = (f"chip {chip.get('id')} does not say what '{cid}' holds (chamber type {ctype!r}, no capsules in "
                     f"the believed state, reagent {reagent!r})")
            if tau > strictest * (1 + 1e-9):
                blocked.append(f"{where}, and {float(flow):g} uL/min is {tau:.4f} dyn/cm2, over the strictest "
                               f"ceiling ({strictest:g} dyn/cm2, {key}): whether that is safe depends on whether "
                               f"adherent cells are in there, so it cannot be approved until the spec says")
            else:
                warnings.append(f"{where}; {tau:.4f} dyn/cm2 is under the strictest ceiling ({strictest:g} dyn/cm2, "
                                f"{key}), so this flow is safe either way")
            continue
        rinsing = bool(lyse_ahead) and bool(reagent) and any(w in str(reagent).lower() for w in RINSE_WORDS)
        ceiling, key = shear_ceiling(contents, rinsing)
        if ceiling is None:
            continue
        tau = shear_dyn_cm2(float(flow), *section)
        at_ceiling = float(flow) * ceiling / tau if tau > 0 else float("inf")
        if tau > ceiling * (1 + 1e-9):
            blocked.append(f"{float(flow):g} uL/min through '{cid}' is {tau:.4f} dyn/cm2 of wall shear, over the "
                           f"{ceiling:g} dyn/cm2 {key} for {contents.replace('_', ' ')} "
                           f"(at or below {at_ceiling:.2f} uL/min)")
        elif tau > 0.9 * ceiling:
            warnings.append(f"{float(flow):g} uL/min through '{cid}' is {tau:.4f} dyn/cm2, within 10% of the "
                            f"{ceiling:g} dyn/cm2 {key}")
    if blocked:
        return SafetyResult("blocked", blocked + warnings)
    if warnings:
        return SafetyResult("warning", warnings)
    return Approved()


def joins_culture_chambers(command: str, args: dict, chip: dict) -> list[str]:
    """Rule 7's real content: actions that connect two culture chambers to each other.

    On the shipped culture chip the lanes are joined either by feeding several of them from
    ONE medium line (the same reagent at the same time: that line is the shared path) or by
    pooling them. Seeding a split, or harvesting through the one shared outlet at the end,
    does not keep two cultures in contact and is not caught here.
    """
    cultured = [c["id"] for c in chip.get("chambers", []) if str(c.get("type")) in ADHERENT_TYPES]
    if command == "pool":
        joined = [s for s in args.get("sources", []) if s in cultured]
        return joined if len(joined) > 1 else []
    if command == "culture":
        targets = [t for t in args.get("targets", []) if t in cultured]
        reagents = list(args.get("reagents") or [])
        if len(targets) > 1 and len(set(reagents)) == 1 and reagents:
            return targets
    return []


def check_static(command: str, args: dict, chip: dict, run_params: dict | None = None) -> SafetyResult:
    """Rules that need no believed state: ids of things that no longer exist, the
    control-variant isolation rule, and temperatures against the chip material."""
    _, materials = _contracts()
    blocked: list[str] = []
    warnings: list[str] = []
    if command == "valve":
        # The valve command was removed with the on-chip valves themselves: a pneumatic
        # membrane valve needs a second fluidic layer and we print one, so no chip declares
        # any (contracts/blocks.json lists them as excluded). An old transcript or protocol
        # that still asks for one is refused here rather than passed as "approved".
        named = ", ".join(args.get("valves", []) or ["(none)"])
        return Blocked(f"{named} not a valve in chip {chip['id']}: the valve command no longer exists, because "
                       f"on-chip valves need a second fluidic layer and were removed from the design. Route by "
                       f"which port is fed, vented or capped instead.")
    # 7. the control chip's lanes must stay isolated
    if (run_params or {}).get("variant") == "control":
        joined = joins_culture_chambers(command, args, chip)
        if joined:
            blocked.append(f"{command} would join {', '.join(joined)} in the control variant: the control chip's "
                           f"culture chambers must stay isolated, so no medium line and no pooling step may "
                           f"connect two of them")
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


def plan_peak_pressure(command: str, args: dict, believed: ChipState, model: ProcessModel,
                       target: str) -> tuple[float, float]:
    """Worst |pressure| at the retention over the WHOLE plan, and the flow it happens at.

    The rule used to judge only the state the plan starts from, so anything the plan itself
    does to the chamber - a load piling capsules onto the mesh wash by wash - was judged at
    a cover the chip never has again after the first slice. The plan is simulated wash by
    wash through the same process model the optimizer uses, and the worst point is taken.
    """
    q = float(args["flow_ul_min"])
    washes = max(1, int(args.get("washes", 1) or 1))
    per_wash = float(args.get("volume_ul") or 0.0)
    state = believed.copy()
    peak = abs(model.mesh_pressure(state, target, q))
    sl = {k: v for k, v in args.items() if k not in ("washes", "soak_s")}
    for i in range(washes):
        if per_wash <= 0:
            break
        step = dict(sl, volume_ul=per_wash)
        if command == "load":
            step["capsules"] = float(args.get("capsules", 0) or 0) / washes
        state, out = model.predict(state, command, step)
        peak = max(peak, abs(out.get("pressure_kpa") or 0.0), abs(model.mesh_pressure(state, target, q)))
    return peak, q


def check(command: str, args: dict, believed: ChipState, chip: dict, model: ProcessModel | None = None,
          run_params: dict | None = None, lyse_ahead: bool = False) -> SafetyResult:
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

    # 0. the state-free rules, so there is exactly one copy of them: ids of removed
    # hardware, the control-variant isolation rule and temperature vs the chip material.
    # check()'s docstring claimed this for a long time without doing it, which is how the
    # control-chip rule came to be unreachable from every closed-loop and optimizer path.
    static = check_static(command, args, chip, run_params)
    if static.status == "blocked":
        return static
    warnings += static.reasons

    target = args.get("target")
    vol = total_volume(command, args, model)

    # 2. pressure at the retention, in BOTH directions and over the whole plan.
    # A back-flush used to skip this rule entirely: the pressure across the retention is
    # then negative (it pushes up), but its magnitude is just as able to push capsules
    # through the opening and out of the chamber, and the optimizer offers back-flush flows
    # twice the lift flow. Judge the magnitude, whichever way the liquid is going.
    if command in ("exchange", "load") and target:
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
            start = abs(model.mesh_pressure(future, target, q))
            pressure = max(start, plan_peak_pressure(command, args, believed, model, target)[0])
            limit = model.params.escape_pressure_kpa
            margin = p["control.pressure_margin"]["value"]
            where = "lifting off the retention" if args.get("direction") == "backflush" else "at the mesh"
            if pressure >= limit:
                blocked.append(f"predicted {pressure:.2f} kPa {where} >= escape pressure {limit:g} kPa: "
                               f"capsules would be pushed through the retention opening (lower the flow below "
                               f"{limit / max(pressure / q, 1e-9):.0f} uL/min)")
            elif pressure >= margin * limit:
                warnings.append(f"predicted {pressure:.2f} kPa {where} is above {margin:.0%} of the "
                                f"{limit:g} kPa escape pressure")

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

    # 5. temperature vs material: done by check_static above, for every command that
    # carries a temp_c - hold_temp AND culture, which also carries one.

    # 6. capsules still on the mesh
    if command in ("release", "pool"):
        sources = [target] if command == "release" else list(args.get("sources", []))
        for cid in sources:
            ch = believed.chambers.get(cid)
            if ch and ch.has_retention and ch.capsules_on_mesh > max(0.5, 0.01 * ch.capsules):
                blocked.append(f"{ch.capsules_on_mesh:,.0f} capsules are still on the mesh in '{cid}': back-flush first")

    # 9. wall shear, against the ceiling for what this chamber holds
    shear = check_shear(command, args, chip, believed, lyse_ahead=lyse_ahead)
    if shear.status == "blocked":
        blocked += shear.reasons
    else:
        warnings += shear.reasons

    if blocked:
        return SafetyResult("blocked", blocked + warnings)
    if warnings:
        return SafetyResult("warning", warnings)
    return Approved()

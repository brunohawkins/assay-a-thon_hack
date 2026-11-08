"""Temperature and material: the two constraints that cut this assay first.

Three rules, each traceable to a contract key rather than to a number written here.

1. **Live cells cap the board.** ``protocols/thermal_map.md``: "Everything from RT onward
   (42-98 C) kills cells, so library prep cannot share a board with culture." The
   threshold is ``parameters.rt.temp_c`` (the reverse-transcription hold) and the culture
   setpoint is ``parameters.culture.temp_c``; neither is written as a literal below.

2. **Thermocycling does not share a demo board.** ``parameters.pcr.max_temp_c`` is the
   98 C hold, and ``contracts/blocks.json target_envelope.design_rules_at_this_scale``
   says "The 98 C index PCR stays a small zoned dock. Heating the whole board would
   expand it by millimetres and cook the culture." At demo scale the board *is* the dock -
   30 x 20 mm has nowhere to put a zone - so a thermocycled op gets its own chip. At
   target scale the same contract line permits a zoned dock, so a thermocycled op may
   share with ops that are not biological and tolerate the material. The difference
   between the two scales is read from the contract, not assumed.

3. **The material has to survive the hottest step and tolerate the fluids.**
   ``contracts/materials.json`` holds ``max_working_temp_c``, ``pcr_98c_status`` and a
   per-reagent ``chemical_compatibility`` table. The chooser takes the simplest material
   that survives the heat, is not disqualified for a 98 C hold when there is one, and is
   not ``TODO`` or ``incompatible`` for anything this chip wets. That reproduces the
   repo's own hand choices: acrylic for 37 C culture (module1_v1), COC for the lysis and
   PCR boards (module3/4/5), because PMMA's guanidinium entry is still TODO and it is
   disqualified outright at 98 C.

``contracts/materials.json`` has no accessor in ``assay2chip/lib/contracts.py`` yet, so
it is read here through ``lib.io.load_json`` - the file, never a copied value.

    TODO(s4): move ``materials()`` into assay2chip/lib/contracts.py when that module
    grows a materials accessor, and delete the loader below.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from assay2chip.lib import contracts
from assay2chip.lib.io import ROOT, load_json
from assay2chip.partition import chemistry
from assay2chip.partition.ops import ROUTING_KINDS, Op

MATERIALS_PATH = ROOT / "contracts" / "materials.json"

#: Which piece of hardware holds the board at a setpoint, from thermal_map.md's
#: "Five setpoints, three pieces of hardware". Boundaries come from contract keys.
ZONE_SOURCES = {
    "fridge": "contracts/parameters.json gelation.temp_c",
    "ambient": "contracts/parameters.json thermal.ambient_c",
    "incubator": "contracts/parameters.json culture.temp_c",
    "dry_block": "contracts/parameters.json rt.temp_c",
    "cycler": "contracts/parameters.json pcr.max_temp_c",
}

_materials_cache: Dict[str, Any] = {}


def materials() -> Dict[str, Any]:
    """contracts/materials.json, read once."""
    if "data" not in _materials_cache:
        _materials_cache["data"] = load_json(MATERIALS_PATH)
    return _materials_cache["data"]["materials"]


def cells_ceiling_c() -> float:
    """The hottest a board carrying live cells may get: the culture setpoint."""
    return float(contracts.param("culture.temp_c"))


def lethal_c() -> float:
    """The first setpoint that kills cells - the RT hold, per thermal_map.md."""
    return float(contracts.param("rt.temp_c"))


def thermocycle_c() -> float:
    return float(contracts.param("pcr.max_temp_c"))


def zone_of(op: Op) -> str:
    """Which piece of hardware this op needs under the board."""
    if op.kind == "thermocycle":
        return "cycler"
    if op.temp_c is None:
        # Not stated. The honest reading is "whatever the rest of the board is", so it
        # constrains nothing and the caller records the gap.
        return "unknown"
    temp = float(op.temp_c)
    if temp <= float(contracts.param("gelation.temp_c")):
        return "fridge"
    if temp >= lethal_c():
        return "dry_block"
    if temp >= cells_ceiling_c():
        return "incubator"
    return "ambient"


@dataclass(frozen=True)
class MaterialChoice:
    material_id: str
    name: str
    max_working_temp_c: float
    why: str
    unverified: Tuple[str, ...] = ()

    @property
    def source(self) -> str:
        return "contracts/materials.json materials.%s" % self.material_id


def _candidates() -> List[Tuple[str, Dict[str, Any]]]:
    """Materials we could actually build in, simplest (coolest-rated) first.

    Anything whose `suitable_for` says it is only a print master for embossing is not a
    wetted-surface material, so it is not a candidate for the chip itself.
    """
    out = []
    for key, entry in materials().items():
        suitable = entry.get("suitable_for") or []
        if suitable and all(s == "print_master_for_hot_embossing" for s in suitable):
            continue
        if entry.get("max_working_temp_c") is None:
            continue
        out.append((key, entry))
    out.sort(key=lambda item: (float(item[1]["max_working_temp_c"]), item[0]))
    return out


def choose_material(ops: Sequence[Op]) -> Optional[MaterialChoice]:
    """The simplest material that survives this chip's heat and tolerates its fluids."""
    temps = [op.temp_c for op in ops if op.temp_c is not None]
    hottest = max(temps) if temps else None
    cycles = any(op.kind == "thermocycle" for op in ops)
    if cycles:
        hottest = max(hottest or 0.0, thermocycle_c())
    reagents = chemistry.material_reagents(ops)

    best_rejected: List[str] = []
    for key, entry in _candidates():
        limit = float(entry["max_working_temp_c"])
        if hottest is not None and hottest > limit:
            best_rejected.append("%s is rated to %g C" % (entry["name"], limit))
            continue
        status = str(entry.get("pcr_98c_status", ""))
        if cycles and status.upper().startswith("DISQUALIFIED"):
            best_rejected.append("%s: %s" % (entry["name"], status))
            continue
        compat = entry.get("chemical_compatibility") or {}
        bad = [r for r in reagents if compat.get(r) == "incompatible"]
        unknown = [r for r in reagents if compat.get(r, "TODO") == "TODO"]
        if bad:
            best_rejected.append("%s is incompatible with %s"
                                 % (entry["name"], ", ".join(bad)))
            continue
        if unknown:
            best_rejected.append("%s has no compatibility verdict for %s"
                                 % (entry["name"], ", ".join(unknown)))
            continue
        why = "%s is rated to %g C" % (entry["name"], limit)
        if hottest is not None:
            why += ", above this chip's %g C" % hottest
        if cycles:
            why += "; %s" % status
        if reagents:
            why += "; compatible with %s" % ", ".join(reagents)
        unverified = ("contracts/materials.json marks these values an assumption",) \
            if entry.get("assumption") else ()
        return MaterialChoice(key, str(entry["name"]), limit, why, unverified)

    if best_rejected:
        return MaterialChoice(
            "", "", float("nan"),
            "no material in contracts/materials.json qualifies: " + "; ".join(best_rejected))
    return None


@dataclass(frozen=True)
class ThermalVerdict:
    ok: bool
    why: str = ""
    keys: Tuple[str, ...] = ()
    #: [min, max] over the ops that state a temperature, or None when none do.
    span: Optional[Tuple[float, float]] = None


def check(ops: Sequence[Op], scale: str) -> List[ThermalVerdict]:
    """Every thermal reason these ops cannot share one board. Empty list means they can."""
    out: List[ThermalVerdict] = []
    temps = [op.temp_c for op in ops if op.temp_c is not None]
    span = (min(temps), max(temps)) if temps else None

    live = [op for op in ops if op.live_cells]
    if live:
        hot = [op for op in ops if op.temp_c is not None and op.temp_c >= lethal_c()]
        if hot:
            out.append(ThermalVerdict(
                False,
                "live cells in %s cannot share a board with %s at %g C: parameters."
                "rt.temp_c (%g C) onward is lethal, and the board may not exceed "
                "parameters.culture.temp_c (%g C)"
                % (live[0].label(), hot[0].label(), hot[0].temp_c, lethal_c(),
                   cells_ceiling_c()),
                ("parameters.rt.temp_c", "parameters.culture.temp_c",
                 "protocols/thermal_map.md"),
                span))
        cycled = [op for op in ops if op.kind == "thermocycle"]
        if cycled and not hot:
            out.append(ThermalVerdict(
                False,
                "live cells in %s cannot share a board with the %g C thermocycling of %s "
                "(parameters.pcr.max_temp_c)"
                % (live[0].label(), thermocycle_c(), cycled[0].label()),
                ("parameters.pcr.max_temp_c", "parameters.culture.temp_c",
                 "protocols/thermal_map.md"),
                span))

    cycled = [op for op in ops if op.kind == "thermocycle"]
    if cycled:
        # At demo scale the whole 30 x 20 mm board IS the dock, so every op on it gets
        # ramped to 98 C. Ops that only move fluid survive that - which is why
        # module4_v1 legitimately carries the sublibrary split and the index PCR on one
        # board - but anything that has to be HELD at a setpoint of its own does not.
        held = [op for op in ops if op.kind != "thermocycle"
                and op.kind not in ROUTING_KINDS]
        if held and scale == "demo":
            out.append(ThermalVerdict(
                False,
                "the %g C thermocycling of %s ramps the whole demo board, and %s has to "
                "be held at %s instead: blocks.json target_envelope."
                "design_rules_at_this_scale keeps the 98 C PCR a small zoned dock because "
                "heating a whole board would expand it by millimetres, and 30 x 20 mm has "
                "nowhere to put a zone"
                % (thermocycle_c(), cycled[0].label(), held[0].label(),
                   "%g C" % held[0].temp_c if held[0].temp_c is not None
                   else "its own setpoint"),
                ("blocks.json target_envelope.design_rules_at_this_scale",
                 "parameters.pcr.max_temp_c", "blocks.json envelope.max_x_mm"),
                span))
        elif held:
            biological = [op for op in held if op.live_cells]
            if biological:
                out.append(ThermalVerdict(
                    False,
                    "a zoned %g C dock is allowed at target scale, but not next to the "
                    "live cells in %s" % (thermocycle_c(), biological[0].label()),
                    ("blocks.json target_envelope.design_rules_at_this_scale",
                     "parameters.culture.temp_c"),
                    span))

    choice = choose_material(ops)
    if choice is not None and not choice.material_id:
        out.append(ThermalVerdict(
            False, choice.why,
            ("contracts/materials.json",), span))
    return out


def material_split(a: Sequence[Op], b: Sequence[Op]) -> Optional[str]:
    """Why two groups of ops cannot share a board because the material differs."""
    first, second = choose_material(a), choose_material(b)
    if first is None or second is None:
        return None
    if not first.material_id or not second.material_id:
        return None
    if first.material_id == second.material_id:
        return None
    return ("%s needs %s and %s needs %s (contracts/materials.json), and one board is "
            "one material" % (a[0].label(), first.name, b[0].label(), second.name))

"""The ONE place assay2chip reads contracts/blocks.json and contracts/parameters.json.

NEVER hard-code a number that exists in a contract. This repo has been bitten by that at
least six times - a 2.0 mm port literal that outlived the 0.8 mm contract value, a shear
ceiling copied into six files and then scoped wrongly in five of them, and 0.8e-3 Pa s
written into four generators. If a stage needs a number, it calls a function here. If the
number is not in a contract, the stage records a gap in `unresolved` rather than inventing
one.

    from assay2chip.lib import contracts

    env = contracts.envelope("demo")          # Envelope(x_mm=30, y_mm=20, depth_mm=0.3)
    floors = contracts.fabrication_floors()   # dict, straight from blocks.json
    mu = contracts.viscosity_for(37)          # Viscosity(pa_s=..., key=..., scope=...)
    sc = contracts.shear_ceiling_for("capsules")

Nothing here defaults. A missing key raises ContractError naming the key and the file,
because a silently defaulted contract value is indistinguishable from a guess.

VISCOSITY - read this before using either number:

  fluid.viscosity_37c_pa_s   anything at 37 C: medium, buffer, cells, capsules, the whole
                             real assay. This is the one almost every stage wants.
  fluid.viscosity_pa_s       water at 20 C. It belongs to the DYE DEMO and nothing else.
                             Using it at 37 C overstates resistance and wall shear by 25%.

`viscosity_for(temp_c)` picks the right one, so prefer it over naming either key.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "operator"))

import schema_io  # noqa: E402

BLOCKS_PATH = ROOT / "contracts" / "blocks.json"
PARAMETERS_PATH = ROOT / "contracts" / "parameters.json"

# Contract keys this pipeline depends on. Listed in one place so run.py can report a
# contract gap up front instead of a stage dying halfway through.
PARAMETER_KEYS = [
    "capsule.diameter_um",
    "capsule.swell_factor",
    "capsule.escape_pressure_kpa",
    "control.pressure_margin",
    "shear.culture_ceiling_dyn_cm2",
    "shear.prelysis_rinse_ceiling_dyn_cm2",
    "fluid.viscosity_pa_s",
    "fluid.viscosity_37c_pa_s",
    "capsule_channel.width_factor_low",
    "trap.gap_factor_low",
    "trap.gap_factor_high",
    "barcoding.random_bases",
    "barcoding.loading_mean",
]

VISCOSITY_37C_KEY = "fluid.viscosity_37c_pa_s"
VISCOSITY_20C_KEY = "fluid.viscosity_pa_s"


class ContractError(KeyError):
    """A contract key this pipeline needs is missing or the wrong shape.

    Raised loudly and never defaulted: assay2chip/README.md requires the key, so the fix
    is to add it to the contract (and tell whoever owns it), not to pick a number here.
    """

    def __str__(self) -> str:  # KeyError would quote the message
        return self.args[0] if self.args else ""


@dataclass(frozen=True)
class Envelope:
    """A build envelope. `name` is 'demo', 'target' or 'ceiling'."""

    name: str
    x_mm: float
    y_mm: float
    depth_mm: float
    layers: int
    why: str
    source: str

    @property
    def area_mm2(self) -> float:
        return self.x_mm * self.y_mm

    def fits(self, x_mm: float, y_mm: float) -> bool:
        return x_mm <= self.x_mm and y_mm <= self.y_mm


@dataclass(frozen=True)
class ShearCeiling:
    """What bounds the flow for one kind of chamber contents.

    There are THREE cases and they are not interchangeable - that is the whole point of
    this type. Two are wall-shear numbers; the third has no shear ceiling at all and is
    bounded by pressure, so `ceiling_dyn_cm2` is None and `pressure_ceiling_kpa` is set.
    """

    shear_class: str
    ceiling_dyn_cm2: float | None
    pressure_ceiling_kpa: float | None
    bound_by: str
    scope: str
    source: str


@dataclass(frozen=True)
class Viscosity:
    pa_s: float
    key: str
    scope: str
    temp_c: float


@dataclass(frozen=True)
class PortLimit:
    limit: int | None
    scope: str
    why: str


@dataclass(frozen=True)
class Capsule:
    diameter_um: float
    swell_factor: float
    source: str = ""
    _: Any = field(default=None, repr=False, compare=False)

    @property
    def swollen_diameter_um(self) -> float:
        return self.diameter_um * self.swell_factor


_cache: dict[str, Any] = {}


def blocks() -> dict:
    """contracts/blocks.json, the authority on geometry and fabrication."""
    if "blocks" not in _cache:
        _cache["blocks"] = schema_io.load_json(BLOCKS_PATH)
    return _cache["blocks"]


def parameters() -> dict:
    """The `parameters` table of contracts/parameters.json (flat dotted keys)."""
    if "parameters" not in _cache:
        data = schema_io.load_json(PARAMETERS_PATH)
        if "parameters" not in data:
            raise ContractError("contracts/parameters.json has no 'parameters' table")
        _cache["parameters"] = data["parameters"]
    return _cache["parameters"]


def param(key: str) -> Any:
    """One parameter's `value` from contracts/parameters.json. Raises if absent or null.

    Many parameters are a range with value: null and min/max (e.g. gelation.time_min).
    Those have no single number, so this refuses them: use `param_range` and decide
    explicitly, recording the choice in `unresolved`.
    """
    table = parameters()
    if key not in table:
        raise ContractError(
            f"contracts/parameters.json has no parameter '{key}', which assay2chip needs "
            f"(assay2chip/lib/contracts.py PARAMETER_KEYS). Add it to the contract with its "
            f"units and source - do not pick a number in code."
        )
        
    entry = table[key]
    if entry.get("value") is None:
        lo, hi = entry.get("min"), entry.get("max")
        raise ContractError(
            f"contracts/parameters.json '{key}' has no single value"
            + (f" (it is a range {lo}-{hi} {entry.get('units', '')})" if lo is not None else "")
            + ": choose one deliberately with contracts.param_range() and record the choice "
            "in the stage's `unresolved`."
        )
    return entry["value"]


def param_entry(key: str) -> dict:
    """The whole parameter entry: value, units, source, assumption flag, note."""
    table = parameters()
    if key not in table:
        raise ContractError(f"contracts/parameters.json has no parameter '{key}'")
    return table[key]


def param_range(key: str) -> tuple[float | None, float | None]:
    """(min, max) for a parameter stated as a range."""
    entry = param_entry(key)
    return entry.get("min"), entry.get("max")


def is_assumption(key: str) -> bool:
    """True when the contract marks this parameter a rule of thumb rather than published.

    Stages should carry this into their output: a result that rests on an assumption is
    not the same as one that rests on a paper.
    """
    return bool(param_entry(key).get("assumption"))


def block(path: str) -> Any:
    """A value from contracts/blocks.json by dotted path, e.g. 'envelope.max_x_mm'."""
    node: Any = blocks()
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ContractError(
                f"contracts/blocks.json has no '{path}', which assay2chip needs. Add it to "
                f"the contract - do not pick a number in code."
            )
        node = node[part]
    return node


def library() -> set[str]:
    """Every block id we are allowed to place (blocks.json basic + advanced).

    Anything not in here needs a `missing_part` placement, not a new name.
    """
    return set(block("basic")) | set(block("advanced"))


# --- envelopes ---------------------------------------------------------------

def envelope(name: str = "demo") -> Envelope:
    """One of the three agreed envelopes.

    demo    30 x 20 mm      what we can print and emboss today; chip/ is built to it.
    target  127.76 x 85.48  the design target, the ANSI/SLAS 1-2004 microplate footprint.
    ceiling 290 x 190 mm    the agreed MAXIMUM. Nothing in the design needs it; it exists
                            so both prototyping routes stay standard.

    All three are 0.3 mm deep and ONE fluidic layer, so no channel may cross another.
    """
    if name == "demo":
        return Envelope(
            "demo",
            float(block("envelope.max_x_mm")),
            float(block("envelope.max_y_mm")),
            float(block("envelope.max_depth_mm")),
            int(block("envelope.layers")),
            str(block("envelope.note")),
            "contracts/blocks.json envelope",
        )
    if name == "target":
        return Envelope(
            "target",
            float(block("target_envelope.x_mm")),
            float(block("target_envelope.y_mm")),
            float(block("target_envelope.depth_mm")),
            int(block("target_envelope.layers")),
            str(block("target_envelope.standard")),
            "contracts/blocks.json target_envelope",
        )
    if name == "ceiling":
        return Envelope(
            "ceiling",
            float(block("target_envelope.max_allowed_x_mm")),
            float(block("target_envelope.max_allowed_y_mm")),
            float(block("target_envelope.depth_mm")),
            int(block("target_envelope.layers")),
            "Agreed maximum chip size; nothing in the current design needs it.",
            "contracts/blocks.json target_envelope.max_allowed_*",
        )
    raise ContractError(
        f"no envelope called {name!r}: the three are 'demo', 'target' and 'ceiling'"
    )


def envelopes() -> dict[str, Envelope]:
    return {name: envelope(name) for name in ("demo", "target", "ceiling")}


def max_depth_mm() -> float:
    """Etch depth ceiling, the same for every chip: ONE fluidic layer at ONE depth."""
    return float(block("envelope.max_depth_mm"))


def single_layer() -> bool:
    """Always true, and worth asserting: it is why no channel may cross another."""
    return int(block("envelope.layers")) == 1


# --- fabrication floors ------------------------------------------------------

def fabrication_floors() -> dict[str, float]:
    """The printable minimums from contracts/blocks.json, without the prose.

    min_open_channel_mm, min_enclosed_printed_channel_mm, min_wall_mm, min_port_mm,
    min_port_pitch_mm, max_ports_per_chip.
    """
    raw = block("fabrication_floors")
    return {k: v for k, v in raw.items() if not k.startswith("_") and k != "note"}


def floor(name: str) -> float:
    """One fabrication floor, e.g. floor('min_port_mm') -> 0.8."""
    value = fabrication_floors().get(name)
    if value is None:
        raise ContractError(
            f"contracts/blocks.json fabrication_floors has no '{name}'; it has "
            f"{', '.join(sorted(fabrication_floors()))}"
        )
    return float(value)


def max_ports_per_chip(scale: str = "demo") -> int | None:
    """Port budget for a chip, which is the usual reason S4 has to split an assay.

    demo: the contract's fabrication_floors.max_ports_per_chip. target/ceiling: the
    contract says the limit "is a demo constraint and lifts here" and gives no number, so
    this returns None - meaning "not bounded by the contract", never "unlimited by
    assumption". Use port_limit() when you want the reason too.
    """
    return port_limit(scale).limit


def port_limit(scale: str = "demo") -> PortLimit:
    if scale == "demo":
        return PortLimit(
            int(floor("max_ports_per_chip")),
            "demo envelope (30 x 20 mm)",
            str(block("fabrication_floors.note")),
        )
    if scale in ("target", "ceiling"):
        rules = block("target_envelope.design_rules_at_this_scale")
        why = next((r for r in rules if "port" in r), "")
        return PortLimit(None, f"{scale} envelope", why)
    raise ContractError(f"no port limit for scale {scale!r}: use 'demo', 'target' or 'ceiling'")


# --- shear, the three ceilings ----------------------------------------------

def shear_ceilings() -> dict[str, ShearCeiling]:
    """THREE ceilings, correctly scoped. Mixing them up has already caused real bugs.

    adherent_cells   shear.culture_ceiling_dyn_cm2 (0.01). ADHERENT CELLS only, where the
                     risk is detaching them. It does NOT apply to capsule chambers.
    prelysis_rinse   shear.prelysis_rinse_ceiling_dyn_cm2 (0.3). A rinse immediately
                     before a deliberate lyse, where cell detachment no longer matters.
    capsules         NO shear ceiling. Capsule handling is bounded by pressure:
                     capsule.escape_pressure_kpa x control.pressure_margin.
    """
    return {
        "adherent_cells": ShearCeiling(
            "adherent_cells",
            float(param("shear.culture_ceiling_dyn_cm2")),
            None,
            "wall_shear",
            "Chambers holding adherent cells, where shear would detach them.",
            "contracts/parameters.json shear.culture_ceiling_dyn_cm2",
        ),
        "prelysis_rinse": ShearCeiling(
            "prelysis_rinse",
            float(param("shear.prelysis_rinse_ceiling_dyn_cm2")),
            None,
            "wall_shear",
            "A rinse immediately before a deliberate lyse or quench. Not a culture step.",
            "contracts/parameters.json shear.prelysis_rinse_ceiling_dyn_cm2",
        ),
        "capsules": ShearCeiling(
            "capsules",
            None,
            max_working_pressure_kpa(),
            "escape_pressure",
            str(param_entry("shear.capsule_handling_note").get("note", "")),
            "contracts/parameters.json shear.capsule_handling_note + capsule.escape_pressure_kpa",
        ),
        "none": ShearCeiling(
            "none",
            None,
            None,
            "unbounded",
            "Nothing shear-sensitive in this op (no cells, no capsules).",
            "assay2chip/README.md shear_class",
        ),
    }


def shear_ceiling_for(shear_class: str) -> ShearCeiling:
    """The ceiling for a unitops.json `shear_class`. Raises on an unknown class."""
    table = shear_ceilings()
    if shear_class not in table:
        raise ContractError(
            f"unknown shear_class {shear_class!r}: unitops.json allows "
            f"{', '.join(sorted(table))}"
        )
    return table[shear_class]


# --- capsules and pressure ---------------------------------------------------

def capsule_diameter_um() -> float:
    """Unswollen capsule diameter (capsule.diameter_um)."""
    return float(param("capsule.diameter_um"))


def capsule_swell_factor() -> float:
    """How much a capsule swells in culture (capsule.swell_factor)."""
    return float(param("capsule.swell_factor"))


def swollen_capsule_diameter_um() -> float:
    """What channels and ports actually have to pass: diameter x swell factor."""
    return capsule_diameter_um() * capsule_swell_factor()


def capsule() -> Capsule:
    return Capsule(capsule_diameter_um(), capsule_swell_factor(), "contracts/parameters.json")


def escape_pressure_kpa() -> float:
    """Pressure across a retention feature at which soft capsules start squeezing through.

    Marked an assumption in the contract (nobody has measured it), so any finding that
    rests on it should say so: contracts.is_assumption('capsule.escape_pressure_kpa').
    """
    return float(param("capsule.escape_pressure_kpa"))


def pressure_margin() -> float:
    """Fraction of the escape pressure a plan is allowed to reach (control.pressure_margin)."""
    return float(param("control.pressure_margin"))


def max_working_pressure_kpa() -> float:
    """The number a plan is actually checked against: escape pressure x margin."""
    return escape_pressure_kpa() * pressure_margin()


# --- viscosity ---------------------------------------------------------------

def viscosity_37c_pa_s() -> Viscosity:
    """Viscosity at 37 C - medium, buffer, cells, capsules: the real assay.

    THE DEFAULT FOR EVERYTHING ON THE REAL DEVICE. Not fluid.viscosity_pa_s.
    """
    return Viscosity(
        float(param(VISCOSITY_37C_KEY)),
        VISCOSITY_37C_KEY,
        "Anything at 37 C: medium, buffer, cells, capsules. The real assay.",
        37.0,
    )


def viscosity_water_20c_pa_s() -> Viscosity:
    """Water at 20 C - the DYE DEMO ONLY.

    Using this at 37 C overstates channel resistance and wall shear by about 25%.
    """
    return Viscosity(
        float(param(VISCOSITY_20C_KEY)),
        VISCOSITY_20C_KEY,
        "Water at 20 C. The dye demo only - never the real assay.",
        20.0,
    )


def barcode_random_bases() -> int:
    """Length of the random cell barcode (barcoding.random_bases).

    The barcode space is 4 ** this, which is what sets the collision rate - NOT
    lanes x sublibraries.
    """
    return int(param("barcoding.random_bases"))


def barcode_space() -> int:
    """4 ** barcoding.random_bases: how many distinct barcodes exist."""
    return 4 ** barcode_random_bases()


def barcode_loading_mean() -> float:
    """Mean barcodes per droplet (barcoding.loading_mean).

    Loading is Poisson, so this is a mean and not a count: aiming for exactly one would
    discard 37% of cells, which is why the published method runs above 1.
    """
    return float(param("barcoding.loading_mean"))


def barcoding_lanes() -> int:
    """Per-lane lipid-oligo barcodes carried by the cells, one per lineage."""
    return int(param("barcoding.lanes"))


def viscosity_for(temp_c: float | None) -> Viscosity:
    """The right viscosity for a temperature. Prefer this over naming either key.

    Anything at or above 30 C gets the 37 C value; below that, water at 20 C. A null
    temperature gets the 37 C value, because the real device runs warm - and a stage that
    did not know the temperature should have recorded that in `unresolved`.
    """
    if temp_c is None or float(temp_c) >= 30.0:
        return viscosity_37c_pa_s()
    return viscosity_water_20c_pa_s()


# --- health check ------------------------------------------------------------

def missing_keys() -> list[str]:
    """Contract keys assay2chip needs that are not in the contracts (usually empty).

    run.py prints this before anything runs, so a contract gap is a loud message at the
    top of the run instead of a stage crashing in the middle of it.
    """
    table = parameters()
    out = [k for k in PARAMETER_KEYS if k not in table]
    for path in (
        "envelope.max_x_mm",
        "envelope.max_y_mm",
        "envelope.max_depth_mm",
        "envelope.layers",
        "target_envelope.x_mm",
        "target_envelope.y_mm",
        "target_envelope.max_allowed_x_mm",
        "target_envelope.max_allowed_y_mm",
        "fabrication_floors.max_ports_per_chip",
    ):
        try:
            block(path)
        except ContractError:
            out.append(f"blocks.json:{path}")
    return out


def fingerprint() -> dict[str, str]:
    """sha256 of each contract file, recorded in the run manifest so a run can be redone."""
    import hashlib

    out = {}
    for path in (BLOCKS_PATH, PARAMETERS_PATH):
        out[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def summary() -> dict:
    """Everything this pipeline reads from the contracts, as plain JSON.

    Handy for the S10 report and for `run.py --list`, and it doubles as the test that
    every key is really there.
    """
    out: dict[str, Any] = {
        "envelopes": {
            name: {"x_mm": e.x_mm, "y_mm": e.y_mm, "depth_mm": e.depth_mm, "layers": e.layers,
                   "area_mm2": round(e.area_mm2, 1), "source": e.source}
            for name, e in envelopes().items()
        },
        "max_depth_mm": max_depth_mm(),
        "fabrication_floors": fabrication_floors(),
        "max_ports_per_chip": {
            "demo": max_ports_per_chip("demo"),
            "target": max_ports_per_chip("target"),
        },
        "blocks": len(library()),
        "contracts": fingerprint(),
        "missing_keys": missing_keys(),
    }
    def _try(name, fn):
        try:
            out[name] = fn()
        except ContractError as e:
            out[name] = {"missing": str(e)}

    _try("shear_ceilings", lambda: {
        k: {"ceiling_dyn_cm2": c.ceiling_dyn_cm2, "pressure_ceiling_kpa": c.pressure_ceiling_kpa,
            "bound_by": c.bound_by, "scope": c.scope, "source": c.source}
        for k, c in shear_ceilings().items()})
    _try("capsule", lambda: {"diameter_um": capsule_diameter_um(),
                             "swell_factor": capsule_swell_factor(),
                             "swollen_diameter_um": swollen_capsule_diameter_um()})
    _try("pressure", lambda: {"escape_pressure_kpa": escape_pressure_kpa(),
                              "margin": pressure_margin(),
                              "max_working_kpa": max_working_pressure_kpa()})
    _try("barcoding", lambda: {"random_bases": barcode_random_bases(),
                               "space": barcode_space(),
                               "loading_mean": barcode_loading_mean()})
    _try("viscosity", lambda: {
        "at_37c": {"pa_s": viscosity_37c_pa_s().pa_s, "key": VISCOSITY_37C_KEY,
                   "scope": viscosity_37c_pa_s().scope},
        "water_20c": {"pa_s": viscosity_water_20c_pa_s().pa_s, "key": VISCOSITY_20C_KEY,
                      "scope": viscosity_water_20c_pa_s().scope}})
    return out


def main(argv: list[str]) -> int:
    """python assay2chip/lib/contracts.py - print everything the pipeline reads."""
    print(json.dumps(summary(), indent=2))
    gaps = missing_keys()
    if gaps:
        print(f"\nMISSING CONTRACT KEYS ({len(gaps)}): {', '.join(gaps)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

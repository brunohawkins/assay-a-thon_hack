"""Which material the ops need, read out of contracts/materials.json.

A 98 C index PCR is the case this exists for. `contracts/materials.json` is explicit:
PMMA is "DISQUALIFIED for the PCR chip" (Tg ~105 C), printed resin softens around 73 C
AND inhibits PCR, bare `cop`/`zeonor` deliberately resolve to the 1060R grade so a PCR
chip has to name a high-Tg one, and COC 5013/6013 and Zeonor 1420R "SURVIVE 98 C PCR". So
picking a material is a LOOKUP against stated requirements, never a preference:

    max working temperature   >= the hottest hold on the chip
    suitable_for              contains the process route (hot embossing) and, above
                              PCR_TEMP_C, 'pcr_chip'
    dna_adsorption           'Low' when the chip handles nucleic acid (lyse, thermocycle,
                              collect a library): a silica-like surface that binds DNA in
                              chaotropic salt loses the sample
    chemical_compatibility   tie-break: prefer the material whose compatibility with our
                              reagents is actually KNOWN rather than TODO

TODO(assay2chip/lib/contracts.py): `contracts.py` is the one accessor for blocks.json and
parameters.json and has no materials accessor yet. This module is the only place in
assay2chip that opens materials.json, so moving it behind `contracts.material(...)` later
is a one-file change. It holds no material numbers of its own.

LAYER THICKNESSES. The etch depth is a contract value. The base and lid thicknesses are
not in any contract - they are a mechanical choice about stiffness under bonding pressure.
So rather than invent them, `layer_convention()` reads them off the chips the team has
already built (`chip/*.json`): the modal base and lid of every stack whose middle layer is
the etch depth. The chip records where they came from in `unresolved`.
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "operator"))

import schema_io  # noqa: E402

from assay2chip.lib import contracts  # noqa: E402

MATERIALS_PATH = ROOT / "contracts" / "materials.json"
CHIP_DIR = ROOT / "chip"

# Above this a hold is a PCR hold, and materials.json's pcr_98c_status / suitable_for
# 'pcr_chip' verdicts are the ones that decide. It is the temperature of the index PCR
# denaturation step the assay itself states, not a process constant, so it is a module
# constant here rather than a contract key.
PCR_TEMP_C = 90.0
ROUTE = "hot_embossing"
# Ops whose sample is nucleic acid: the surface must not bind DNA.
DNA_KINDS = frozenset({"lyse", "thermocycle", "collect"})


class MaterialError(Exception):
    """No material in contracts/materials.json can do what these ops need."""

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


@dataclass(frozen=True)
class Material:
    id: str
    name: str
    max_working_temp_c: float
    glass_transition_c: float | None
    why: str
    unknown_compatibility: int
    assumption: bool

    @property
    def spec_name(self) -> str:
        """What goes in a layer's `material` field: the id materials.json resolves."""
        return self.id


def materials() -> dict:
    return schema_io.load_json(MATERIALS_PATH)["materials"]


def choose_material(needs: Sequence, *, route: str = ROUTE) -> Material:
    """The material these ops need. Raises MaterialError when nothing qualifies."""
    temps = [float(n.temp_c) for n in needs if n.temp_c is not None]
    hottest = max(temps) if temps else None
    needs_pcr = hottest is not None and hottest >= PCR_TEMP_C
    handles_dna = any(n.kind in DNA_KINDS for n in needs)

    table = materials()
    rejected: list[dict] = []
    candidates: list[Material] = []
    for mid, m in table.items():
        suitable = [str(s) for s in m.get("suitable_for", [])]
        compat = m.get("chemical_compatibility", {})
        unknown = sum(1 for v in compat.values() if v in ("TODO", "incompatible"))
        reasons = []
        if route not in suitable:
            reasons.append(f"not suitable for {route} ({', '.join(suitable) or 'nothing stated'})")
        if hottest is not None and float(m["max_working_temp_c"]) < hottest:
            reasons.append(f"max working temperature {m['max_working_temp_c']:g} C is below the "
                           f"{hottest:g} C hold on this chip")
        if needs_pcr and "pcr_chip" not in suitable:
            reasons.append(f"not suitable for a PCR chip: {m.get('pcr_98c_status', 'no verdict')}")
        if handles_dna and not str(m.get("dna_adsorption", "")).lower().startswith("low"):
            reasons.append(f"DNA adsorption is not low ({m.get('dna_adsorption', 'unknown')})")
        if reasons:
            rejected.append({"material": mid, "why": reasons})
            continue
        why = [f"{route} route"]
        if hottest is not None:
            why.append(f"{m['max_working_temp_c']:g} C max working temperature covers the "
                       f"{hottest:g} C hold")
        if needs_pcr:
            why.append(str(m.get("pcr_98c_status", "")))
        if handles_dna:
            why.append(f"DNA adsorption {m.get('dna_adsorption', '')}".strip())
        candidates.append(Material(
            id=mid, name=str(m["name"]), max_working_temp_c=float(m["max_working_temp_c"]),
            glass_transition_c=m.get("glass_transition_c"),
            why="; ".join(w for w in why if w), unknown_compatibility=unknown,
            assumption=bool(m.get("assumption")),
        ))
    if not candidates:
        wanted = [f"the {route} route"]
        if hottest is not None:
            wanted.append(f"a {hottest:g} C hold")
        if handles_dna:
            wanted.append("low DNA adsorption")
        raise MaterialError(
            f"no material in contracts/materials.json satisfies " + ", ".join(wanted),
            {"hottest_c": hottest, "needs_pcr": needs_pcr, "handles_dna": handles_dna,
             "rejected": rejected})
    # Prefer the material whose compatibility with our reagents is KNOWN, then the least
    # exotic one that still clears the temperature.
    candidates.sort(key=lambda c: (c.unknown_compatibility, c.max_working_temp_c, c.id))
    return candidates[0]


@dataclass(frozen=True)
class LayerConvention:
    base_mm: float
    lid_mm: float
    source: str


def layer_convention(etch_depth_mm: float | None = None) -> LayerConvention:
    """Base and lid thickness, taken from the chips the team has already built.

    Not a contract value and not invented: the modal base and lid of every chip in `chip/`
    whose middle layer is the etch depth. If there is no such chip, both fall back to the
    etch depth itself and the caller records that in `unresolved`.
    """
    depth = contracts.max_depth_mm() if etch_depth_mm is None else float(etch_depth_mm)
    bases: Counter[float] = Counter()
    lids: Counter[float] = Counter()
    used: list[str] = []
    for path in sorted(CHIP_DIR.glob("*.json")):
        try:
            chip = schema_io.load_json(path)
        except ValueError:
            continue
        layers = chip.get("layers")
        if not isinstance(layers, list) or len(layers) != 3:
            continue
        thick = [la.get("thickness_mm") for la in layers]
        if not all(isinstance(t, (int, float)) for t in thick):
            continue
        if abs(float(thick[1]) - depth) > 1e-9:
            continue
        bases[float(thick[0])] += 1
        lids[float(thick[2])] += 1
        used.append(path.name)
    if not bases or not lids:
        return LayerConvention(depth, depth, "no built chip to copy; both set to the etch depth")
    base = bases.most_common(1)[0][0]
    lid = lids.most_common(1)[0][0]
    return LayerConvention(base, lid, f"the layer stack of {len(used)} built chip(s): "
                                     f"{', '.join(f'chip/{n}' for n in used[:4])}")


def reagent_todos(material: Material, kinds: Iterable[str]) -> list[str]:
    """Reagent classes these op kinds imply whose compatibility with the material is TODO.

    Only informational: unitops.json carries no reagent names, so this cannot be a check.
    """
    compat = materials()[material.id].get("chemical_compatibility", {})
    wanted = {"lyse": "lysis_buffer", "wash": "wash_buffer", "thermocycle": "pcr_mix",
              "incubate": "culture_media", "cure": "lap_photoinitiator",
              "encapsulate": "gelma", "collect": "release_buffer"}
    out = []
    for kind in kinds:
        reagent = wanted.get(kind)
        if reagent and compat.get(reagent, "TODO") in ("TODO", "incompatible"):
            out.append(f"{reagent}: {compat.get(reagent, 'TODO')}")
    return sorted(set(out))

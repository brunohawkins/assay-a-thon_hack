"""Everything an S3 rule is allowed to reason from, derived once per unit operation.

A rule never reads a contract itself and never writes a number. It reads a `Facts`, and
every number on a `Facts` was computed here from a contract key through
assay2chip/lib/contracts.py. That is the whole point of this module: there is exactly one
place where "200 um" or "25 um" can enter S3, and it is a contract lookup.

Three derivations are worth reading before using them.

**The in-plane floor.** `min_inplane_um` is fabrication_floors.min_open_channel_mm. It
applies to WIDTH only. Depth is free, which is why a weir works and an in-plane sieve
does not.

**The weir.** `weir_for(d)` gives the legal gap band
[trap.gap_factor_low x d, trap.gap_factor_high x d) and says whether the band reaches the
one gap the contract is willing to certify (retention.weir_geometry.gap_um). For a 70 um
capsule the band is 21-28 um and the agreed 25 um sits inside it. For a 12 um cell the
band is 3.6-4.8 um: finer than anything the contract certifies, against a z tolerance the
contract itself calls the biggest unknown in the scheme, so `buildable` is False. The
contract's own lever is capsule size, and this function follows it: a larger particle
gets a proportionally larger, more printable gap.

**The droplet orifice.** `orifice_for(d)` scales encapsulation.nozzle_um by
capsule.diameter_um, so the contract's own 40 um nozzle / 70 um capsule pair is
reproduced exactly and any other target diameter is scaled from it.

Linguistic evidence is confined to `Facts.text` and the `signal()` method. It is a
lower-cased join of the op's note fields and whatever reagent names S2 left behind, and a
rule that leans on it says so and carries a lower confidence than one that leans on a
number.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402

# contracts/materials.json has no accessor in assay2chip/lib/contracts.py yet.
# TODO(s3): move this loader into assay2chip/lib/contracts.py when that module grows a
# materials() function, and delete it from here. Until then it is read-only and keyed, so
# no material number is written down in S3 either.
MATERIALS_PATH = ROOT / "contracts" / "materials.json"

_materials_cache: dict[str, Any] = {}


def materials() -> dict:
    """The `materials` table of contracts/materials.json."""
    if "materials" not in _materials_cache:
        data = json.loads(MATERIALS_PATH.read_text(encoding="utf-8"))
        if "materials" not in data:
            raise contracts.ContractError("contracts/materials.json has no 'materials' table")
        _materials_cache["materials"] = data["materials"]
    return _materials_cache["materials"]


def high_tg_materials() -> list[str]:
    """Material ids whose own `pcr_98c_status` says they survive the 98 C hold."""
    return sorted(
        mid for mid, m in materials().items()
        if str(m.get("pcr_98c_status", "")).upper().startswith("SURVIVES")
        and "pcr_chip" in (m.get("suitable_for") or [])
    )


def disqualified_for_pcr() -> list[str]:
    """Material ids contracts/materials.json disqualifies for the 98 C chip."""
    return sorted(
        mid for mid, m in materials().items()
        if str(m.get("pcr_98c_status", "")).upper().startswith("DISQUALIFIED")
    )


def optical_materials() -> list[str]:
    """Materials whose transparency entry claims an optical path (not 'not a reliable')."""
    out = []
    for mid, m in materials().items():
        note = str(m.get("optical_transparency", "")).lower()
        if "optically clear" in note and "not a reliable" not in note:
            out.append(mid)
    return sorted(out)


def compatible_materials(reagent: str) -> list[str]:
    """Materials whose chemical_compatibility marks `reagent` compatible."""
    return sorted(
        mid for mid, m in materials().items()
        if (m.get("chemical_compatibility") or {}).get(reagent) == "compatible"
    )


# --- contract-derived geometry ------------------------------------------------

def min_inplane_um() -> float:
    """The in-plane feature floor, in um. WIDTH only: depth is not subject to it."""
    return contracts.floor("min_open_channel_mm") * 1000.0


def agreed_weir_gap_um() -> float:
    """The one weir gap the contract certifies (retention.weir_geometry.gap_um)."""
    return float(contracts.block("retention.weir_geometry.gap_um"))


def crest_thickness_um() -> float:
    """The weir crest's IN-PLANE thickness, which is simply the in-plane floor."""
    return float(contracts.block("retention.weir_geometry.crest_thickness_um"))


@dataclass(frozen=True)
class Weir:
    """A depth weir for one particle diameter, sized from the contract's own rule."""

    diameter_um: float
    gap_low_um: float          # at or above this (trap.gap_factor_low x D)
    gap_high_um: float         # STRICTLY below this (trap.gap_factor_high x D)
    agreed_gap_um: float       # retention.weir_geometry.gap_um
    gap_um: float | None       # what we would actually cut, or None
    buildable: bool
    why: str

    @property
    def band(self) -> str:
        return f"{self.gap_low_um:.3g}-{self.gap_high_um:.3g} um"


def weir_for(diameter_um: float) -> Weir:
    """Is a depth weir deliverable for a particle of this diameter, and at what gap?

    Legal band from trap.gap_factor_low / trap.gap_factor_high (the rule in
    contracts/blocks.json retention.weir_geometry.rule: at or above low x D, strictly
    below high x D). Deliverable only when that band reaches the agreed 25 um gap, the
    single figure the contract certifies: anything finer is below everything the contract
    is willing to quote and sits inside the z tolerance it calls the biggest unknown in
    the retention scheme.
    """
    low = float(contracts.param("trap.gap_factor_low")) * diameter_um
    high = float(contracts.param("trap.gap_factor_high")) * diameter_um
    agreed = agreed_weir_gap_um()
    if low <= agreed < high:
        return Weir(diameter_um, low, high, agreed, agreed, True,
                    f"the agreed {agreed:.3g} um gap sits inside the legal "
                    f"{low:.3g}-{high:.3g} um band for a {diameter_um:.3g} um particle")
    if low > agreed:
        return Weir(diameter_um, low, high, agreed, low, True,
                    f"a {diameter_um:.3g} um particle allows a {low:.3g} um gap, coarser "
                    f"than the agreed {agreed:.3g} um and so more printable")
    return Weir(diameter_um, low, high, agreed, None, False,
                f"the legal band for a {diameter_um:.3g} um particle is {low:.3g}-"
                f"{high:.3g} um, finer than the agreed {agreed:.3g} um gap and inside the "
                f"z tolerance the contract calls the biggest unknown in the scheme")


def orifice_for(capsule_um: float) -> float:
    """Flow-focusing orifice needed for a capsule of this diameter, in um.

    Scaled from the contract's own pair: encapsulation.nozzle_um (40 um) makes
    capsule.diameter_um (70 um), so the ratio is a contract value, not a literal.
    """
    ratio = float(contracts.param("encapsulation.nozzle_um")) / contracts.capsule_diameter_um()
    return capsule_um * ratio


# Repo precedents that are NOT contract values. Each carries its provenance, and a rule
# that uses one says "precedent" in its `why` rather than naming a contract key.
PRECEDENT: dict[str, dict] = {
    "reinjection_entrance_factor": {
        "value": 0.85,
        "units": "x capsule diameter",
        "source": "protocols/sctip_spc_design.md section 2 (10.1021/acs.analchem.5c05488)",
        "note": "Close-packed re-injection needs an entrance of about 0.85 x D: 115 um for "
                "a 135 um capsule, 60 um for a 70 um one.",
    },
    "phase_separation_slot_um": {
        "value": [100, 150],
        "units": "um",
        "source": "protocols/sctip_spc_design.md section 6, stage C",
        "note": "Emulsion breaking and the packed-bed trap are drawn as 100 x 150 um slots; "
                "the 100 um side is in plane.",
    },
    "sealed_static_culture_s": {
        "value": 8 * 3600,
        "units": "s",
        "source": "assay2chip S3 oxygen audit; budget in protocols/ipsc_to_mgl_design.md "
                  "('a sealed static chamber is about seven times short')",
        "note": "A sealed microlitre-scale chamber of cells goes anoxic in about 8 h, so "
                "anything longer needs continuous perfusion and an off-chip vented "
                "gas-exchange reservoir. ASSUMPTION: no contract key holds this yet.",
    },
    "inflow_delay_line_s": {
        "value": 300,
        "units": "s",
        "source": "chip/designs/README.md rule 4 (timed waits in flow are a delay line)",
        "note": "Above a few minutes a delay line at our flows is metres long, so the hold "
                "becomes a chamber instead (protocols/sctip_spc_design.md section 6).",
    },
}


def precedent(name: str) -> Any:
    entry = PRECEDENT.get(name)
    if entry is None:
        raise KeyError(f"no triage precedent called {name!r}: have {', '.join(sorted(PRECEDENT))}")
    return entry["value"]


def precedent_source(name: str) -> str:
    return PRECEDENT[name]["source"]


# --- linguistic signals ------------------------------------------------------

# The ONLY place S3 looks at words. Each key is a signal a rule may ask for; a rule that
# uses one must say so in `why` and carry text-level confidence, because a word is weaker
# evidence than a number. S2 should put reagent and intent wording in the op's note.
SIGNALS: dict[str, tuple[str, ...]] = {
    "droplet": ("droplet", "flow focus", "flow-focus", "nozzle", "orifice", "emulsify",
                "emulsification", "encapsulat", "water-in-oil", "water in oil"),
    "reinjection": ("re-inject", "reinject", "re inject", "close-pack", "close pack",
                    "barcod", "overlap extension", "ampli drop", "amplidrop"),
    "emulsion_break": ("break the emulsion", "breaking the emulsion", "emulsion break",
                       "de-emulsif", "demulsif", "wash off the oil", "oil removal",
                       "remove the oil", "phase separat", "de-oil"),
    "centrifuge": ("centrifug", "spin down", "spun down", "pellet", "rcf", "x g,", " x g "),
    "ethanol": ("ethanol", "isopropanol", "etoh"),
    "dry": ("air dry", "air-dry", "dry the", "drying", "to dryness", "dried"),
    "chlorinated": ("chloroform", "dichloromethane", "methylene chloride", "trichloro",
                    "carbon tetrachloride", "chlorinated"),
    "quench": ("quench", "cold solvent", "methanol", "acetonitrile", "extraction solvent"),
    # Deliberately NOT 'sort by': sorting by SIZE is a different problem with a different
    # answer. Every token here implies a label or a genetic modification.
    "label_sort": ("facs", "fluorescence-activated", "fluorescence activated",
                   "flow cytomet", "flow sorter", "cell sorter", "magnetic-activated",
                   "macs", "reporter", "gfp", "mcherry", "tdtomato", "knock-in", "knockin",
                   "transgen", "engineered line", "engineered cell line", "antibody",
                   "immunolabel", "labelled", "labeled", "stained", "fluorophore"),
    "size_exclusion": ("strain", "mesh", "filter", "sieve", "size exclusion",
                       "size-exclusion", "clump", "pore"),
    "metered": ("equal", "metered", "aliquot", "split evenly", "fixed volume"),
    "sealed": ("sealed", "static", "no flow", "no perfusion", "closed chamber"),
    "perfusion": ("perfus", "recirculat", "continuous flow", "medium exchange",
                  "feed continuously"),
    "not_live": ("fixed", "permeabilis", "permeabiliz", "lysed", "lysate", "dead",
                 "paraformaldehyde", "denatur", "heat-kill"),
    "adherent": ("adherent", "attached", "monolayer", "in situ", "on the floor of",
                 "seeded"),
    "imaging": ("image", "imaging", "count", "brightfield", "bright-field", "microscop",
                "viability"),
    "magnetic": ("magnet", "spri", "bead capture"),
    "culture": ("cultur", "differentiat", "incubator", "co2", "passage", "medium"),
}


def _text_of(op: dict) -> str:
    """Every scrap of wording attached to an op, lower-cased and joined.

    `note` is the schema field. `notes` is what S2 currently writes; both are read so a
    rename upstream cannot silently blind the rule engine.
    """
    bits: list[str] = []
    for key in ("note", "notes"):
        value = op.get(key)
        if isinstance(value, str):
            bits.append(value)
    for holder in ("fluid", "particles"):
        sub = op.get(holder)
        if isinstance(sub, dict) and isinstance(sub.get("note"), str):
            bits.append(sub["note"])
    derived = op.get("derived")
    if isinstance(derived, dict):
        if isinstance(derived.get("target"), str):
            bits.append(derived["target"])
        for reagent in derived.get("reagents") or []:
            if isinstance(reagent, dict):
                for key in ("name", "role"):
                    if isinstance(reagent.get(key), str):
                        bits.append(reagent[key])
    protocol = op.get("protocol")
    if isinstance(protocol, dict):
        for value in protocol.values():
            if isinstance(value, str):
                bits.append(value)
    return " ".join(bits).lower()


@dataclass(frozen=True)
class Facts:
    """One unit operation, plus every contract-derived number a rule may want."""

    op: int
    kind: str
    particles: str
    diameter_um: float | None
    swollen_um: float | None
    count: int | None
    volume_ul: float | None
    viscosity_class: str
    duration_s: float | None
    temp_c: float | None
    shear_class: str
    confidence: float | None
    text: str

    min_inplane_um: float
    culture_temp_c: float
    pcr_max_temp_c: float
    weir: Weir | None
    orifice_um: float | None
    reinjection_entrance_um: float | None
    gaps: tuple[dict, ...] = ()

    # -- questions a rule asks ------------------------------------------------
    def signal(self, name: str) -> bool:
        """Does the op's wording carry this signal? Weak evidence, cited as such."""
        if name not in SIGNALS:
            raise KeyError(f"no triage signal called {name!r}: have {', '.join(sorted(SIGNALS))}")
        return any(token in self.text for token in SIGNALS[name])

    @property
    def has_particles(self) -> bool:
        return self.particles not in ("none", "")

    @property
    def live_cells(self) -> bool:
        """Cells that are still alive.

        A `lyse` op is killing them deliberately, and wording like 'fixed', 'permeabilised'
        or 'lysate' says they are already dead, so neither counts as live.
        """
        if self.particles != "cells" or self.kind == "lyse":
            return False
        return not self.signal("not_live")

    @property
    def above_culture_temp(self) -> bool:
        return self.temp_c is not None and float(self.temp_c) > self.culture_temp_c

    def printable(self, feature_um: float | None) -> bool:
        """Is an in-plane feature of this size printable? Depth is never asked this."""
        return feature_um is not None and feature_um >= self.min_inplane_um


def facts_for(op: dict, gaps: list[dict] | None = None) -> Facts:
    """Derive the facts for one op. Records a contract default it had to fall back on."""
    recorded: list[dict] = [] if gaps is None else gaps
    particles = op.get("particles") or {}
    kind_of_particle = particles.get("kind") or "none"
    diameter = particles.get("diameter_um")
    swollen = particles.get("swollen_diameter_um")

    if kind_of_particle == "capsules" and diameter is None:
        diameter = contracts.capsule_diameter_um()
        recorded.append({
            "op": op.get("op"),
            "field": "particles.diameter_um",
            "question": "the source never gave a capsule diameter, which decides whether a "
                        "weir can retain them and whether the nozzle is printable",
            "assumed": diameter,
            "assumed_from": "capsule.diameter_um",
        })
    if kind_of_particle == "capsules" and swollen is None and diameter is not None:
        swollen = diameter * contracts.capsule_swell_factor()

    weir = weir_for(float(diameter)) if diameter else None
    fluid = op.get("fluid") or {}

    target_capsule = None
    if op.get("kind") == "encapsulate":
        if kind_of_particle == "capsules" and diameter:
            target_capsule = float(diameter)
        else:
            target_capsule = contracts.capsule_diameter_um()
            recorded.append({
                "op": op.get("op"),
                "field": "particles.diameter_um",
                "question": "an encapsulation op that does not state the capsule diameter it "
                            "is making; the nozzle size follows from it",
                "assumed": target_capsule,
                "assumed_from": "capsule.diameter_um",
            })

    return Facts(
        op=int(op.get("op") or 0),
        kind=str(op.get("kind") or ""),
        particles=kind_of_particle,
        diameter_um=float(diameter) if diameter else None,
        swollen_um=float(swollen) if swollen else None,
        count=particles.get("count"),
        volume_ul=fluid.get("volume_ul"),
        viscosity_class=str(fluid.get("viscosity_class") or "aqueous"),
        duration_s=op.get("duration_s"),
        temp_c=op.get("temp_c"),
        shear_class=str(op.get("shear_class") or "none"),
        confidence=op.get("confidence"),
        text=_text_of(op),
        min_inplane_um=min_inplane_um(),
        culture_temp_c=float(contracts.param("culture.temp_c")),
        pcr_max_temp_c=float(contracts.param("pcr.max_temp_c")),
        weir=weir,
        orifice_um=orifice_for(target_capsule) if target_capsule else None,
        reinjection_entrance_um=(
            float(diameter) * precedent("reinjection_entrance_factor") if diameter else None
        ),
        gaps=tuple(recorded),
    )

"""Chip state: what is in every chamber, reservoir, the waste and the collection tube.

Two instances exist at runtime:
  BELIEVED  what the operator's model thinks is happening (label "believed")
  TRUE      what is "really" happening on the virtual chip (label "true"; simulation only)
The controller compares them to show how far belief drifts from truth, with and
without sensors. Units: uL, s, C, kPa, capsule counts (floats: expected values).
"""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field


@dataclass
class ChamberState:
    id: str
    capacity_ul: float
    has_retention: bool = False         # mesh / pillars / weir that holds capsules
    volume_ul: float = 0.0              # liquid in the chamber
    reagent: str | None = None          # liquid identity (the reagent being exchanged in)
    concentration: float = 0.0          # fraction of `reagent` in the chamber liquid (0-1): exchange completeness
    capsule_concentration: float = 0.0  # the same inside the capsules (lags by diffusion)
    capsules_on_mesh: float = 0.0
    capsules_suspended: float = 0.0
    capsules_in_channel: float = 0.0
    swollen: bool = False
    temp_c: float = 22.0
    pressure_kpa: float = 0.0           # across the retention, + = pushing down onto it

    @property
    def capsules(self) -> float:
        return self.capsules_on_mesh + self.capsules_suspended + self.capsules_in_channel


@dataclass
class ChipState:
    chip_id: str
    chambers: dict[str, ChamberState]
    reservoirs_ul: dict[str, float]
    waste_capacity_ul: float
    collection_capacity_ul: float
    label: str = "believed"
    waste_ul: float = 0.0
    collection_ul: float = 0.0
    capsules_loaded: float = 0.0
    capsules_collected: float = 0.0
    capsules_lost: float = 0.0          # escaped to waste
    clock_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    # --- construction -------------------------------------------------------------

    @classmethod
    def from_chip_spec(cls, chip: dict, parameters: dict, reagents: list[str] | None = None,
                       reservoirs_ul: dict[str, float] | None = None, label: str = "believed") -> "ChipState":
        p = parameters["parameters"]
        ambient = p.get("thermal.ambient_c", {}).get("value", 22.0)
        chambers = {}
        for c in chip["chambers"]:
            has_ret = bool(c.get("retention") or c.get("mesh_pore_um"))
            chambers[c["id"]] = ChamberState(id=c["id"], capacity_ul=float(c["volume_ul"]), has_retention=has_ret,
                                             temp_c=ambient)
        default_res = p.get("reservoir.default_ul", {}).get("value", 1000.0)
        res = {r: float(default_res) for r in (reagents or [])}
        res.update({k: float(v) for k, v in (reservoirs_ul or {}).items()})
        return cls(
            chip_id=chip["id"], chambers=chambers, reservoirs_ul=res,
            waste_capacity_ul=float(p.get("waste.capacity_ul", {}).get("value", 2000.0)),
            collection_capacity_ul=float(p.get("collection.capacity_ul", {}).get("value", 500.0)),
            label=label,
        )

    # --- copying and JSON -------------------------------------------------------------

    def copy(self) -> "ChipState":
        return copy.deepcopy(self)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ChipState":
        d = dict(d)
        d["chambers"] = {k: ChamberState(**v) for k, v in d["chambers"].items()}
        return cls(**d)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ChipState":
        return cls.from_dict(json.loads(text))

    # --- summaries (run files, twin) ----------------------------------------------------

    def summary(self, chamber_id: str | None = None) -> dict:
        """Compact numbers for run files and the twin's believed-vs-true panel."""
        out = {
            "clock_s": round(self.clock_s, 2),
            "waste_ul": round(self.waste_ul, 2),
            "collection_ul": round(self.collection_ul, 2),
            "capsules_loaded": round(self.capsules_loaded),
            "capsules_collected": round(self.capsules_collected),
            "capsules_lost": round(self.capsules_lost),
            "reservoirs_ul": {k: round(v, 2) for k, v in self.reservoirs_ul.items()},
        }
        if chamber_id and chamber_id in self.chambers:
            c = self.chambers[chamber_id]
            out.update({
                "chamber": chamber_id,
                "volume_ul": round(c.volume_ul, 3),
                "reagent": c.reagent,
                "exchange_pct": round(100 * c.concentration, 2),
                "capsule_exchange_pct": round(100 * c.capsule_concentration, 2),
                "capsules_on_mesh": round(c.capsules_on_mesh),
                "capsules_suspended": round(c.capsules_suspended),
                "temp_c": round(c.temp_c, 2),
                "pressure_kpa": round(c.pressure_kpa, 3),
            })
        return out

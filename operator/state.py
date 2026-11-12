"""Chip state: what is in every chamber, reservoir, the waste and the collection tube.

Two instances exist at runtime:
  BELIEVED  what the operator's model thinks is happening (label "believed")
  TRUE      what is "really" happening on the virtual chip (label "true"; simulation only)
The controller compares them to show how far belief drifts from truth, with and
without sensors. Units: uL, s, C, kPa, capsule counts (floats: expected values).

Capsule counts obey one invariant, checked by conservation_problem() and restored by
enforce_conservation(): loaded == on a mesh + suspended + collected + lost. Every limit
comes from contracts/parameters.json and a missing key is an error, never a default.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass, field


class StateError(ValueError):
    """The believed state cannot be built, or it stopped adding up."""


def _value(parameters: dict, key: str) -> float:
    """One contract value, or a loud failure.

    These used to be `p.get(key, {}).get("value", 22.0 / 1000 / 2000 / 500)`. A renamed or
    deleted key then turned into an invented number - the waste limit silently became
    2000 uL whatever contracts/parameters.json said - and safety rule 3 was judging a plan
    against a literal in this file. A missing contract is a broken install, not a default.
    """
    p = parameters.get("parameters", parameters)
    if key not in p:
        raise StateError(f"contracts/parameters.json has no '{key}': the believed state cannot be built "
                         f"without it (it sets a safety limit; no default is safe to invent)")
    value = p[key].get("value") if isinstance(p[key], dict) else p[key]
    if value is None:
        raise StateError(f"contracts/parameters.json '{key}' has no value yet (it is a TODO placeholder): "
                         f"fill it in; the believed state will not invent one")
    return float(value)


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
    swollen: bool = False
    temp_c: float = 22.0
    pressure_kpa: float = 0.0           # across the retention, + = pushing down onto it

    @property
    def capsules(self) -> float:
        # Every place a capsule can be in this chamber. There used to be a third term,
        # capsules_in_channel, that nothing ever wrote: it always added 0, which made the
        # conservation sum below look complete while a capsule in transit was unaccounted
        # for. A compartment nobody fills is not accounting, so it is gone; add it back
        # only together with the code that moves capsules into and out of it.
        return self.capsules_on_mesh + self.capsules_suspended


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
        ambient = _value(parameters, "thermal.ambient_c")
        chambers = {}
        for c in chip["chambers"]:
            has_ret = bool(c.get("retention") or c.get("mesh_pore_um"))
            chambers[c["id"]] = ChamberState(id=c["id"], capacity_ul=float(c["volume_ul"]), has_retention=has_ret,
                                             temp_c=ambient)
        default_res = _value(parameters, "reservoir.default_ul")
        res = {r: float(default_res) for r in (reagents or [])}
        res.update({k: float(v) for k, v in (reservoirs_ul or {}).items()})
        return cls(
            chip_id=chip["id"], chambers=chambers, reservoirs_ul=res,
            waste_capacity_ul=_value(parameters, "waste.capacity_ul"),
            collection_capacity_ul=_value(parameters, "collection.capacity_ul"),
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

    # --- the conservation invariant ------------------------------------------------------
    #
    # Capsules are not created or destroyed by anything the operator can command: every
    # capsule that was loaded is on a mesh, suspended in a chamber, in the collection tube
    # or lost to waste. Nothing enforced that, so a noisy outlet counter could push
    # capsules_lost negative or collect more capsules than were ever loaded, and the
    # recovery percentage in the report was then arithmetic on an impossible state.

    CONSERVATION_TOLERANCE = 0.5          # capsules; counts are expected values, not integers

    def capsules_accounted(self) -> float:
        return (sum(ch.capsules for ch in self.chambers.values())
                + self.capsules_collected + self.capsules_lost)

    def conservation_error(self) -> float:
        """Accounted-for capsules minus the ones that were loaded. Must be ~0."""
        return self.capsules_accounted() - self.capsules_loaded

    def conservation_problem(self) -> str | None:
        """A readable description of a broken capsule count, or None when it adds up."""
        parts = []
        for name in ("capsules_loaded", "capsules_collected", "capsules_lost"):
            if getattr(self, name) < -self.CONSERVATION_TOLERANCE:
                parts.append(f"{name} is negative ({getattr(self, name):.1f})")
        for cid, ch in self.chambers.items():
            for name in ("capsules_on_mesh", "capsules_suspended"):
                if getattr(ch, name) < -self.CONSERVATION_TOLERANCE:
                    parts.append(f"{cid}.{name} is negative ({getattr(ch, name):.1f})")
        err = self.conservation_error()
        if abs(err) > max(self.CONSERVATION_TOLERANCE, 1e-6 * max(self.capsules_loaded, 1.0)):
            parts.append(f"{self.capsules_accounted():,.1f} capsules accounted for against "
                         f"{self.capsules_loaded:,.1f} loaded ({err:+,.1f})")
        return "; ".join(parts) or None

    def enforce_conservation(self) -> list[str]:
        """Clamp the counts back into a possible state. Returns what had to be changed.

        Counts only ever go wrong through a measurement correction, so the fix is to limit
        each correction to what the chip can physically supply rather than to let the
        arithmetic run past zero (or past the number loaded) and look authoritative.
        """
        fixes = []
        for cid, ch in self.chambers.items():
            for name in ("capsules_on_mesh", "capsules_suspended"):
                if getattr(ch, name) < 0:
                    fixes.append(f"{cid}.{name} {getattr(ch, name):.1f} -> 0")
                    setattr(ch, name, 0.0)
        for name in ("capsules_collected", "capsules_lost"):
            if getattr(self, name) < 0:
                fixes.append(f"{name} {getattr(self, name):.1f} -> 0")
                setattr(self, name, 0.0)
        over = self.capsules_accounted() - self.capsules_loaded
        if over > self.CONSERVATION_TOLERANCE:
            # More capsules in the books than were ever loaded: the surplus can only have
            # come from an over-count, so take it off the counted compartments.
            for name in ("capsules_lost", "capsules_collected"):
                take = min(over, getattr(self, name))
                if take > 0:
                    fixes.append(f"{name} {getattr(self, name):.1f} -> {getattr(self, name) - take:.1f}")
                    setattr(self, name, getattr(self, name) - take)
                    over -= take
        return fixes

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
            "capsules_accounted": round(self.capsules_accounted()),
            "capsules_unaccounted": round(self.conservation_error(), 2),
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

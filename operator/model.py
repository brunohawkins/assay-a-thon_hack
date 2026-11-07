"""Process model: predicts what a command does to a ChipState.

Used three ways: the optimizer simulates candidate plans with it, the controller
predicts each time slice with it (then compares with the sensors), and the virtual
chip uses the SAME equations with DIFFERENT parameters as its "reality".

Physics (every constant from contracts/parameters.json; TODO placeholders are marked there):
  exchange   well-mixed chamber: the old liquid left after pumping V through a chamber
             of volume Vc is (1 - c0) * e^(-V/Vc). Inside the capsules the new reagent
             lags with a first-order diffusion time (capsule.diffusion_time_s).
  pressure   across the retention mesh = R * flow, R = clean mesh (flow_calc, tiny) +
             capsule-bed resistance x how much of the mesh the capsules cover.
  escape     above capsule.escape_pressure_kpa, capsules on the mesh squeeze through
             at capsule.loss_rate per 10 s per unit of overpressure.
  backflush  upward flow lifts capsules off the mesh (backflush.lift_flow_ul_min).
  hold_temp  first-order heating/cooling (thermal.tau_s) with a ramp-rate limit.
  volumes    reservoirs -> chamber -> waste (washes) or collection (release).

Commands are applied as time SLICES: args carry the slice's volume (exchange, load,
release) or duration (hold_temp, wait).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field

import flow_calc
from state import ChipState

CARRIER = "capsule_suspension"


def _p(parameters: dict, key: str) -> float:
    return float(parameters["parameters"][key]["value"])


@dataclass
class ModelParams:
    chamber_volume_ul: dict[str, float]
    mesh_resistance: dict[str, float]          # capsule-bed resistance at full cover, kPa per uL/min
    mesh_clean_resistance: dict[str, float]    # clean mesh, kPa per uL/min
    monolayer_capacity: dict[str, float]       # capsules that cover the mesh once
    escape_pressure_kpa: float
    loss_rate: float
    diffusion_time_s: float
    thermal_tau_s: float
    max_ramp_c_per_s: float
    ambient_c: float
    lift_flow_ul_min: float
    capsule_diameter_um: float
    notes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_chip(cls, chip: dict, parameters: dict) -> "ModelParams":
        d_um = chip.get("capsule", {}).get("diameter_um", _p(parameters, "capsule.diameter_um"))
        mu = _p(parameters, "fluid.viscosity_37c_pa_s")   # 37 C medium, not water at 20 C
        vol, bed, clean, cap = {}, {}, {}, {}
        for c in chip["chambers"]:
            cid = c["id"]
            vol[cid] = float(c["volume_ul"])
            area = c["size"][0] * c["size"][1]
            opening = (c.get("retention") or {}).get("opening_um") or c.get("mesh_pore_um")
            if opening:
                clean[cid] = flow_calc.mesh_clean_resistance(opening, area, _p(parameters, "mesh.open_fraction"), mu)
                bed[cid] = _p(parameters, "mesh.bed_resistance_kpa_per_ul_min")
                cap[cid] = area / (d_um / 1000) ** 2
            else:
                clean[cid], bed[cid], cap[cid] = 0.0, 0.0, 1.0
        return cls(
            chamber_volume_ul=vol, mesh_resistance=bed, mesh_clean_resistance=clean, monolayer_capacity=cap,
            escape_pressure_kpa=_p(parameters, "capsule.escape_pressure_kpa"),
            loss_rate=_p(parameters, "capsule.loss_rate"),
            diffusion_time_s=_p(parameters, "capsule.diffusion_time_s"),
            thermal_tau_s=_p(parameters, "thermal.tau_s"),
            max_ramp_c_per_s=_p(parameters, "thermal.max_ramp_c_per_s"),
            ambient_c=_p(parameters, "thermal.ambient_c"),
            lift_flow_ul_min=_p(parameters, "backflush.lift_flow_ul_min"),
            capsule_diameter_um=float(d_um),
        )

    def copy(self) -> "ModelParams":
        return ModelParams(**{k: (dict(v) if isinstance(v, dict) else v) for k, v in asdict(self).items()})

    def to_dict(self) -> dict:
        return asdict(self)


class ProcessModel:
    def __init__(self, params: ModelParams):
        self.params = params

    # --- pieces the optimizer and safety checker use directly -------------------------

    def resistance(self, state: ChipState, chamber: str) -> float:
        """Current flow resistance across the chamber's retention, kPa per uL/min."""
        p = self.params
        ch = state.chambers[chamber]
        cover = min(1.0, (ch.capsules_on_mesh + ch.capsules_suspended) / max(p.monolayer_capacity.get(chamber, 1.0), 1.0))
        return p.mesh_clean_resistance.get(chamber, 0.0) + p.mesh_resistance.get(chamber, 0.0) * cover

    def mesh_pressure(self, state: ChipState, chamber: str, flow_ul_min: float) -> float:
        if not state.chambers[chamber].has_retention:
            return 0.0
        return self.resistance(state, chamber) * flow_ul_min

    def escape_risk(self, pressure_kpa: float) -> float:
        """0 = safe, 1 = at the escape pressure, >1 = capsules are being pushed through."""
        return max(pressure_kpa, 0.0) / self.params.escape_pressure_kpa

    # --- the main prediction ----------------------------------------------------------

    def predict(self, state: ChipState, command: str, args: dict) -> tuple[ChipState, dict]:
        """Apply one slice of a command to a copy of state. Returns (new_state, outputs)."""
        s = state.copy()
        out = {"pressure_kpa": 0.0, "outlet_absorbance": None, "temp_c": None, "dispensed_ul": 0.0,
               "capsules_out_waste": 0.0, "capsules_out_collection": 0.0, "escape_risk": 0.0, "dt_s": 0.0,
               "reservoir_short": False}
        handler = getattr(self, f"_{command}", None)
        if handler is None:
            raise ValueError(f"process model has no rule for command {command!r}")
        handler(s, args, out)
        for cid, ch in s.chambers.items():
            if command != "hold_temp" or cid != args.get("target"):
                ch.temp_c += (self.params.ambient_c - ch.temp_c) * (1 - math.exp(-out["dt_s"] / (4 * self.params.thermal_tau_s)))
        s.clock_s += out["dt_s"]
        return s, out

    # --- helpers ------------------------------------------------------------------------

    def _draw(self, s: ChipState, reagent: str, volume: float, out: dict) -> float:
        """Take liquid from a reservoir; returns what was actually available."""
        have = s.reservoirs_ul.get(reagent)
        if have is None:            # unknown reservoir: treat as unlimited (but tracked)
            s.reservoirs_ul[reagent] = 0.0
            have = float("inf")
        got = min(volume, max(have, 0.0))
        if got < volume - 1e-9:
            out["reservoir_short"] = True
        if have != float("inf"):
            s.reservoirs_ul[reagent] = have - got
        out["dispensed_ul"] = got
        return got

    def _flush(self, s: ChipState, chamber: str, reagent: str, volume: float, dt: float) -> float:
        """Well-mixed displacement of the chamber liquid by `volume` of `reagent`."""
        ch = s.chambers[chamber]
        vc = self.params.chamber_volume_ul[chamber]
        if ch.reagent != reagent:
            ch.reagent, ch.concentration, ch.capsule_concentration = reagent, 0.0, 0.0
        c0 = ch.concentration
        ch.concentration = 1 - (1 - c0) * math.exp(-volume / vc)
        c_avg = (c0 + ch.concentration) / 2
        ch.capsule_concentration = c_avg + (ch.capsule_concentration - c_avg) * math.exp(-dt / self.params.diffusion_time_s)
        ch.volume_ul = vc
        return ch.concentration

    def _escape(self, s: ChipState, chamber: str, pressure: float, dt: float, out: dict) -> None:
        ch = s.chambers[chamber]
        risk = self.escape_risk(pressure)
        out["escape_risk"] = risk
        if risk > 1 and ch.capsules_on_mesh > 0:
            lost = ch.capsules_on_mesh * min(1.0, self.params.loss_rate * (risk - 1) * dt / 10)
            ch.capsules_on_mesh -= lost
            s.capsules_lost += lost
            out["capsules_out_waste"] = lost

    # --- commands -------------------------------------------------------------------------

    def _load(self, s: ChipState, a: dict, out: dict) -> None:
        q = a.get("flow_ul_min", 20.0)
        v = self._draw(s, CARRIER, a["volume_ul"], out)
        dt = v / q * 60
        ch = s.chambers[a["target"]]
        n = a.get("capsules", 0.0)
        if ch.has_retention:
            ch.capsules_on_mesh += n          # capsules settle onto the mesh as they arrive
        else:
            ch.capsules_suspended += n
        s.capsules_loaded += n
        self._flush(s, a["target"], CARRIER, v, dt)
        p = self.mesh_pressure(s, a["target"], q)
        ch.pressure_kpa = p
        s.waste_ul += v
        out.update(pressure_kpa=p, outlet_absorbance=ch.concentration, dt_s=dt)
        self._escape(s, a["target"], p, dt, out)

    def _exchange(self, s: ChipState, a: dict, out: dict) -> None:
        q = a["flow_ul_min"]
        v = self._draw(s, a["reagent"], a["volume_ul"], out)
        dt = a["volume_ul"] / q * 60                     # pump runs for the commanded time
        ch = s.chambers[a["target"]]
        if a.get("direction") == "backflush":
            # From below: lifts capsules off the mesh; liquid leaves through the top (to waste).
            lift = min(1.0, q / self.params.lift_flow_ul_min) * (1 - math.exp(-2 * v / self.params.chamber_volume_ul[a["target"]]))
            moved = ch.capsules_on_mesh * lift
            ch.capsules_on_mesh -= moved
            ch.capsules_suspended += moved
            self._flush(s, a["target"], a["reagent"], v, dt)
            p = -self.resistance(s, a["target"]) * q if ch.has_retention else 0.0
            ch.pressure_kpa = p
            s.waste_ul += v
            out.update(pressure_kpa=p, outlet_absorbance=ch.concentration, dt_s=dt)
            return
        # Forward: down through the mesh to waste; suspended capsules settle back on it.
        ch.capsules_on_mesh += ch.capsules_suspended
        ch.capsules_suspended = 0.0
        self._flush(s, a["target"], a["reagent"], v, dt)
        p = self.mesh_pressure(s, a["target"], q) * (v / a["volume_ul"] if a["volume_ul"] else 1)
        ch.pressure_kpa = p
        s.waste_ul += v
        out.update(pressure_kpa=p, outlet_absorbance=ch.concentration, dt_s=dt)
        self._escape(s, a["target"], p, dt, out)

    def _release(self, s: ChipState, a: dict, out: dict) -> None:
        q = a.get("flow_ul_min", 20.0)
        v = self._draw(s, a["reagent"], a.get("volume_ul", self.params.chamber_volume_ul[a["target"]] * 4), out)
        dt = v / q * 60 if q else 0.0
        ch = s.chambers[a["target"]]
        vc = self.params.chamber_volume_ul[a["target"]]
        out_frac = 1 - math.exp(-v / vc)                 # suspended capsules wash out like a tracer
        leaving = ch.capsules_suspended * out_frac
        ch.capsules_suspended -= leaving
        s.capsules_collected += leaving
        s.collection_ul += v
        self._flush(s, a["target"], a["reagent"], v, dt)
        ch.pressure_kpa = 0.0                            # exits above the mesh, not through it
        out.update(capsules_out_collection=leaving, outlet_absorbance=ch.concentration, dt_s=dt)

    def _hold_temp(self, s: ChipState, a: dict, out: dict) -> None:
        ch = s.chambers[a["target"]]
        dt = a["duration_s"]
        ramp = min(a.get("ramp_c_per_s", self.params.max_ramp_c_per_s), self.params.max_ramp_c_per_s)
        target = a["temp_c"]
        step = (target - ch.temp_c) * (1 - math.exp(-dt / self.params.thermal_tau_s))
        step = max(-ramp * dt, min(ramp * dt, step))
        ch.temp_c += step
        if target >= 30 and ch.capsules > 0 and dt >= 3600:
            ch.swollen = True                            # long warm holds = culture: capsules swell
        out.update(temp_c=ch.temp_c, dt_s=dt)

    def _wait(self, s: ChipState, a: dict, out: dict) -> None:
        """No flow: capsules keep soaking (diffusion catches up)."""
        dt = a["duration_s"]
        for ch in s.chambers.values():
            ch.capsule_concentration = ch.concentration + (ch.capsule_concentration - ch.concentration) * math.exp(-dt / self.params.diffusion_time_s)
            ch.pressure_kpa = 0.0
        out["dt_s"] = dt

    def _split(self, s: ChipState, a: dict, out: dict) -> None:
        src = s.chambers[a["source"]]
        n = src.capsules_on_mesh + src.capsules_suspended
        src.capsules_on_mesh = src.capsules_suspended = 0.0
        for t in a["targets"]:
            dst = s.chambers[t]
            share = n / len(a["targets"])
            if dst.has_retention:
                dst.capsules_on_mesh += share
            else:
                dst.capsules_suspended += share
        out["dt_s"] = a.get("duration_s", 60.0)

    def _pool(self, s: ChipState, a: dict, out: dict) -> None:
        dst = s.chambers[a["target"]]
        for src_id in a["sources"]:
            src = s.chambers[src_id]
            n = src.capsules_on_mesh + src.capsules_suspended
            src.capsules_on_mesh = src.capsules_suspended = 0.0
            if dst.has_retention:
                dst.capsules_on_mesh += n
            else:
                dst.capsules_suspended += n
        out["dt_s"] = a.get("duration_s", 60.0)

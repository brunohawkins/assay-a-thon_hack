"""SIMULATED HARDWARE: a virtual chip that stands in for the real pump, valves and sensors.

There is no physical hardware this weekend. This adapter simulates it, DELIBERATELY not
identical to the operator's model, so prediction and reality diverge and the closed loop
has something to correct:

  - its parameters differ from the model's (default: mesh resistance +30%, chamber
    volume -10%), set in the scenario's "reality" block;
  - every measurement carries small random noise (seeded, so runs are reproducible);
  - it returns SENSOR READINGS, not its true state: pump-reported dispensed volume,
    pressure at the mesh, outlet absorbance (proxy for reagent concentration),
    temperature, and a capsule counter at the outlet (a future camera);
  - each sensor can be switched off in the scenario, to show what each piece of
    hardware adds; faults (operator/faults.py) can be injected.

true_state() exposes the simulated truth for the twin and the report. It is labelled
TRUE (simulation only): real hardware can never report it.
"""

from __future__ import annotations

import random

from adapters.base import SENSORS, Adapter
from model import ModelParams, ProcessModel
from state import ChipState

DEFAULT_NOISE = {
    "pump_volume_ul": 0.01,       # relative
    "pressure_kpa": 0.03,         # kPa
    "outlet_absorbance": 0.005,   # AU
    "temperature_c": 0.1,         # C
    "capsules_counted": 0.02,     # relative
}


class VirtualChip(Adapter):
    name = "virtual_chip"
    simulated = True

    def __init__(self, chip: dict, parameters: dict, scenario: dict | None = None,
                 reagents: list[str] | None = None, faults: list | None = None):
        scenario = scenario or {}
        reality = scenario.get("reality", {})
        self.chip = chip
        self.params = ModelParams.from_chip(chip, parameters)
        f_r = reality.get("mesh_resistance_factor", 1.3)
        f_v = reality.get("chamber_volume_factor", 0.9)
        for cid in self.params.mesh_resistance:
            self.params.mesh_resistance[cid] *= f_r
        for cid in self.params.chamber_volume_ul:
            self.params.chamber_volume_ul[cid] *= f_v
        self.base_mesh_resistance = dict(self.params.mesh_resistance)
        self.model = ProcessModel(self.params)
        self.state = ChipState.from_chip_spec(chip, parameters, reagents=reagents,
                                              reservoirs_ul=scenario.get("reservoirs_ul"), label="true")
        for cid, ch in self.state.chambers.items():      # the real chamber is smaller than drawn
            ch.capacity_ul = self.params.chamber_volume_ul[cid]
        self.rng = random.Random(scenario.get("seed", 1))
        wanted = scenario.get("sensors", {name: True for name in SENSORS})
        self.sensors_on = [name for name in SENSORS if wanted.get(name, False)]
        self.noise = {**DEFAULT_NOISE, **reality.get("noise", {})}
        self.faults = list(faults or [])
        self.last = {"pressure_kpa": 0.0, "outlet_absorbance": 0.0, "temp_c": self.params.ambient_c, "dispensed_ul": 0.0,
                     "capsules_out_waste": 0.0, "capsules_out_collection": 0.0}
        self.last_command: tuple[str, dict] | None = None
        self.wash_index = 0          # counts forward-exchange washes (faults key off it)
        self.events: list[dict] = []  # fault/reality notes, for the report
        for f in self.faults:
            f.on_create(self)

    def capabilities(self) -> dict:
        return {"sensors": list(self.sensors_on), "actuators": ["pump", "valves", "heater"], "simulated": True}

    def send(self, command: str, args: dict, **meta) -> dict:
        for f in self.faults:
            f.before_slice(self, command, args, meta)
        if command == "exchange" and args.get("direction") != "backflush" and meta.get("wash_start"):
            self.wash_index += 1
        self.state, out = self.model.predict(self.state, command, args)
        for f in self.faults:
            f.after_slice(self, command, args, out)
        self.last = out
        self.last_command = (command, args)
        return {"ok": not out.get("reservoir_short"), "dispensed_ul": out["dispensed_ul"],
                "simulated": True, "reservoir_short": bool(out.get("reservoir_short"))}

    def read_sensors(self) -> dict:
        g = self.rng.gauss
        o = self.last
        raw = {
            "pump_volume_ul": o.get("dispensed_ul", 0.0) * (1 + g(0, self.noise["pump_volume_ul"])),
            "pressure_kpa": o.get("pressure_kpa", 0.0) + g(0, self.noise["pressure_kpa"]),
            "outlet_absorbance": None if o.get("outlet_absorbance") is None
            else min(1.2, max(0.0, o["outlet_absorbance"] + g(0, self.noise["outlet_absorbance"]))),
            "temperature_c": (o.get("temp_c") if o.get("temp_c") is not None
                              else max(c.temp_c for c in self.state.chambers.values())) + g(0, self.noise["temperature_c"]),
            "capsules_counted": max(0.0, round((o.get("capsules_out_waste", 0.0) + o.get("capsules_out_collection", 0.0))
                                               * (1 + g(0, self.noise["capsules_counted"])))),
        }
        readings = {k: raw[k] for k in self.sensors_on if raw.get(k) is not None}
        for f in self.faults:
            readings = f.on_readings(self, readings)
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in readings.items()}

    def true_state(self) -> dict:
        return self.state.to_dict()

    def true_params(self) -> dict:
        return {"mesh_resistance": dict(self.params.mesh_resistance), "chamber_volume_ul": dict(self.params.chamber_volume_ul)}

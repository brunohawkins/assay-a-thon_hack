"""Dry-run adapter: prints the exact hardware actions each command would cause, with
timestamps, and returns the MODEL'S PREDICTIONS as "measurements".

Use it to check what a run would do to real pumps, valves and the heater before any are
connected. Nothing moves. Because measurements are predictions, the closed loop sees no
error and never updates the model (that is expected in a dry run).

UNTESTED against real hardware (none available); the printed actions mirror what
syringe_pump.py / valves.py would send.
"""

from __future__ import annotations

import sys

from adapters.base import SENSORS, Adapter
from model import ModelParams, ProcessModel
from state import ChipState


class DryRun(Adapter):
    name = "dryrun"
    simulated = False   # not a simulation of truth: it only echoes the model

    def __init__(self, chip: dict, parameters: dict, scenario: dict | None = None, reagents: list[str] | None = None,
                 out=None):
        scenario = scenario or {}
        self.chip = chip
        self.model = ProcessModel(ModelParams.from_chip(chip, parameters))
        self.state = ChipState.from_chip_spec(chip, parameters, reagents=reagents,
                                              reservoirs_ul=scenario.get("reservoirs_ul"), label="dry-run prediction")
        wanted = scenario.get("sensors", {name: True for name in SENSORS})
        self.sensors_on = [n for n in SENSORS if wanted.get(n, False)]
        self.out = out or sys.stdout
        self.last: dict = {}
        self.lines: list[str] = []
        ports = {p["id"]: p for p in chip.get("ports", [])}
        self.inlet = next((p for p, v in ports.items() if v.get("role") == "load"), None)
        self.waste = next((p for p, v in ports.items() if v.get("role") == "waste"), None)
        self.backflush = next((p for p, v in ports.items() if v.get("role") == "backflush"), None)
        self.release = next((p for p, v in ports.items() if v.get("role") == "release"), None)

    def capabilities(self) -> dict:
        return {"sensors": list(self.sensors_on), "actuators": ["pump", "valves", "heater"], "simulated": False,
                "dry_run": True}

    def _say(self, text: str) -> None:
        line = f"[t={self.state.clock_s:8.1f} s] {text}"
        self.lines.append(line)
        print(line, file=self.out)

    def send(self, command: str, args: dict, **meta) -> dict:
        t = args.get("target")
        if command in ("exchange", "load", "release"):
            reagent = args.get("reagent", "capsule_suspension")
            if command == "exchange" and args.get("direction") == "backflush":
                route = f"{self.backflush or 'backflush'} -> {t} -> {self.inlet or 'vent'}"
            elif command == "release":
                route = f"{self.inlet or 'inlet'} -> {t} -> {self.release or 'release'}"
            else:
                route = f"{self.inlet or 'inlet'} -> {t} -> {self.waste or 'waste'}"
            self._say(f"VALVES  route {route}")
            self._say(f"PUMP    [{reagent}] rate {args.get('flow_ul_min', 20):g} uL/min, volume {args['volume_ul']:.3f} uL, RUN")
        elif command == "hold_temp":
            self._say(f"HEATER  setpoint {args['temp_c']:g} C for {args['duration_s']:.1f} s (ramp {args.get('ramp_c_per_s', 1):g} C/s)")
        elif command == "wait":
            self._say(f"WAIT    {args['duration_s']:.1f} s (no flow: capsules soak)")
        else:
            self._say(f"{command.upper():7s} {args}")
        self.state, self.last = self.model.predict(self.state, command, args)
        return {"ok": True, "dry_run": True, "dispensed_ul": self.last["dispensed_ul"]}

    def read_sensors(self) -> dict:
        o = self.last
        pred = {"pump_volume_ul": o.get("dispensed_ul"), "pressure_kpa": o.get("pressure_kpa"),
                "outlet_absorbance": o.get("outlet_absorbance"), "temperature_c": o.get("temp_c"),
                "capsules_counted": (o.get("capsules_out_waste", 0) or 0) + (o.get("capsules_out_collection", 0) or 0)}
        return {k: pred[k] for k in self.sensors_on if pred.get(k) is not None}

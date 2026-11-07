"""Injectable faults for the virtual chip, and the monitor that raises alerts.

Faults (set in a scenario's "faults" list; they change the SIMULATED truth only):
  {"type": "clog", "start_wash": 2, "growth_per_ul": 0.035, "backflush_clears": 0.8}
      mesh resistance rises with the volume filtered from wash N on; a back-flush clears
      most of the build-up
  {"type": "reagent_low", "reagent": "wash_buffer", "start_ul": 30}
      a reservoir starts short (the operator still believes it is full)
  {"type": "capsule_loss", "fraction_per_wash": 0.05}
      a fraction of the capsules on the mesh escapes during every wash
  {"type": "sensor_fault", "sensor": "pressure_kpa", "mode": "stuck" | "wild", "start_t_s": 90}
      a sensor freezes on its last value, or returns wild values, from a time on

Monitor alerts (each with its evidence):
  pressure_high         measured pressure above monitor.pressure_alert_fraction of the escape pressure
  reagent_low           the pump delivered much less than commanded, or the believed reservoir
                        can't cover the rest of the protocol
  capsule_count_drop    the outlet capsule counter sees capsules leaving during a wash
  sensor_disagreement   a sensor is stuck (no noise) or wildly off from the model and from itself
"""

from __future__ import annotations

import math


class Fault:
    type = "fault"

    def __init__(self, spec: dict):
        self.spec = spec

    def on_create(self, chip) -> None: ...
    def before_slice(self, chip, command: str, args: dict, meta: dict) -> None: ...
    def after_slice(self, chip, command: str, args: dict, out: dict) -> None: ...
    def on_readings(self, chip, readings: dict) -> dict:
        return readings

    def describe(self) -> str:
        return f"{self.type}: {self.spec}"


class Clog(Fault):
    type = "clog"

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.start_wash = int(spec.get("start_wash", 2))
        self.growth_per_ul = float(spec.get("growth_per_ul", 0.035))
        self.clears = float(spec.get("backflush_clears", 0.8))
        self.growth = 0.0

    def before_slice(self, chip, command, args, meta):
        target = args.get("target")
        if command != "exchange" or target not in chip.base_mesh_resistance:
            return
        if args.get("direction") == "backflush":
            if self.growth > 0:
                chip.events.append({"fault": "clog", "note": f"back-flush cleared {self.clears:.0%} of the clog",
                                    "growth_before": round(self.growth, 3)})
            self.growth *= 1 - self.clears
        elif chip.wash_index + (1 if meta.get("wash_start") else 0) >= self.start_wash:
            self.growth += self.growth_per_ul * args["volume_ul"]
        chip.params.mesh_resistance[target] = chip.base_mesh_resistance[target] * (1 + self.growth)


class ReagentLow(Fault):
    type = "reagent_low"

    def on_create(self, chip):
        chip.state.reservoirs_ul[self.spec["reagent"]] = float(self.spec.get("start_ul", 0.0))


class CapsuleLoss(Fault):
    type = "capsule_loss"

    def after_slice(self, chip, command, args, out):
        if command != "exchange" or args.get("direction") == "backflush":
            return
        ch = chip.state.chambers.get(args["target"])
        if not ch or ch.capsules_on_mesh <= 0:
            return
        per_wash = float(self.spec.get("fraction_per_wash", 0.05))
        slices = float(self.spec.get("slices_per_wash", 5))
        lost = ch.capsules_on_mesh * (1 - (1 - per_wash) ** (1 / slices))
        ch.capsules_on_mesh -= lost
        chip.state.capsules_lost += lost
        out["capsules_out_waste"] = out.get("capsules_out_waste", 0.0) + lost


class SensorFault(Fault):
    type = "sensor_fault"

    def __init__(self, spec: dict):
        super().__init__(spec)
        self.frozen = None

    def on_readings(self, chip, readings):
        sensor = self.spec["sensor"]
        if sensor not in readings or chip.state.clock_s < float(self.spec.get("start_t_s", 0)):
            return readings
        out = dict(readings)
        if self.spec.get("mode", "stuck") == "stuck":
            if self.frozen is None:
                self.frozen = readings[sensor]
                chip.events.append({"fault": "sensor_fault", "note": f"{sensor} stuck at {self.frozen}",
                                    "t_s": round(chip.state.clock_s, 1)})
            out[sensor] = self.frozen
        else:
            out[sensor] = readings[sensor] * chip.rng.uniform(0.2, 5.0) + chip.rng.uniform(-2, 2)
        return out


FAULTS = {"clog": Clog, "reagent_low": ReagentLow, "capsule_loss": CapsuleLoss, "sensor_fault": SensorFault}


def build_faults(scenario: dict) -> list[Fault]:
    out = []
    for spec in scenario.get("faults", []):
        cls = FAULTS.get(spec.get("type"))
        if cls is None:
            raise ValueError(f"unknown fault type {spec.get('type')!r}; known: {', '.join(FAULTS)}")
        out.append(cls(spec))
    return out


# --- monitor -------------------------------------------------------------------------


class Monitor:
    def __init__(self, parameters: dict):
        p = parameters["parameters"]
        self.limit = p["capsule.escape_pressure_kpa"]["value"]
        self.alert_fraction = p["monitor.pressure_alert_fraction"]["value"]
        self.history: dict[str, list[tuple[float, float]]] = {}
        self.raised: set[str] = set()

    def _raise(self, ctl, name: str, severity: str, message: str, evidence: dict) -> dict | None:
        active = {a["name"] + "|" + str(a["evidence"].get("sensor", "")) for a in ctl.active_alerts}
        key = name + "|" + str(evidence.get("sensor", ""))
        if key in active:
            return None
        alert = {"t_s": round(ctl.believed.clock_s, 2), "step": ctl.current_step, "name": name, "severity": severity,
                 "message": message, "evidence": evidence}
        ctl.alerts.append(alert)
        ctl.active_alerts.append(alert)
        ctl._log("alert", f"{name}: {message}", **{"alert": alert})
        return alert

    def check(self, ctl, command: str, sl: dict, pred: dict, meas: dict, target: str) -> list[dict]:
        alerts = []
        forward = command in ("exchange", "load") and sl.get("direction") != "backflush"
        disagree = False

        # sensor_disagreement first, so a broken sensor doesn't trigger a false pressure alarm
        if "pressure_kpa" in meas:
            h = self.history.setdefault("pressure_kpa", [])
            h.append((meas["pressure_kpa"], pred["pressure_kpa"]))
            last = h[-3:]
            stuck = len(last) == 3 and len({v for v, _ in last}) == 1
            jumped = len(h) >= 2 and abs(h[-1][0] - h[-2][0]) > 2.0 and abs(h[-1][1] - h[-2][1]) < 0.5
            wild = jumped and abs(meas["pressure_kpa"] - pred["pressure_kpa"]) > max(1.0, 2 * abs(pred["pressure_kpa"]))
            if stuck or wild:
                disagree = True
                a = self._raise(ctl, "sensor_disagreement", "critical",
                                "pressure sensor reading is " + ("frozen (identical values, no noise)" if stuck
                                                                 else "jumping far from the model and its own last reading"),
                                {"sensor": "pressure_kpa", "readings": [round(v, 4) for v, _ in last],
                                 "predicted": [round(p, 4) for _, p in last], "mode": "stuck" if stuck else "wild"})
                if a:
                    alerts.append(a)

        if forward and "pressure_kpa" in meas and not disagree and "pressure_kpa" in ctl.trusted:
            if meas["pressure_kpa"] >= self.alert_fraction * self.limit:
                a = self._raise(ctl, "pressure_high", "critical",
                                f"{meas['pressure_kpa']:.2f} kPa at the mesh (alert at {self.alert_fraction * self.limit:.2f}, "
                                f"capsules escape at {self.limit:g})",
                                {"measured_kpa": meas["pressure_kpa"], "predicted_kpa": round(pred["pressure_kpa"], 3),
                                 "limit_kpa": self.limit, "flow_ul_min": sl.get("flow_ul_min")})
                if a:
                    alerts.append(a)

        if "pump_volume_ul" in meas and sl.get("volume_ul", 0) > 0 and meas["pump_volume_ul"] < 0.8 * sl["volume_ul"]:
            a = self._raise(ctl, "reagent_low", "critical",
                            f"pump delivered {meas['pump_volume_ul']:.2f} of {sl['volume_ul']:.2f} uL: reservoir "
                            f"'{sl.get('reagent', 'capsule_suspension')}' may be empty",
                            {"sensor": "pump_volume_ul", "commanded_ul": round(sl["volume_ul"], 3),
                             "delivered_ul": meas["pump_volume_ul"], "reagent": sl.get("reagent")})
            if a:
                alerts.append(a)

        if forward and "capsules_counted" in meas:
            loaded = max(ctl.believed.capsules_loaded, 1.0)
            if meas["capsules_counted"] > max(5.0, 0.005 * loaded):
                a = self._raise(ctl, "capsule_count_drop", "critical",
                                f"outlet counter saw {meas['capsules_counted']:.0f} capsules leave during a wash",
                                {"sensor": "capsules_counted", "counted": meas["capsules_counted"],
                                 "capsules_loaded": round(loaded)})
                if a:
                    alerts.append(a)
        return alerts

    def check_supplies(self, ctl, upcoming_steps: list[dict]) -> list[dict]:
        """Before a step: does the believed reservoir cover what the rest of the protocol needs?"""
        need: dict[str, float] = {}
        for s in upcoming_steps:
            if s.get("reagent") and s.get("volume_ul"):
                need[s["reagent"]] = need.get(s["reagent"], 0) + s["volume_ul"] * (s.get("washes") or 1)
        out = []
        for reagent, vol in need.items():
            have = ctl.believed.reservoirs_ul.get(reagent, math.inf)
            if have < vol:
                a = self._raise(ctl, "reagent_low", "critical",
                                f"reservoir '{reagent}' has {have:.0f} uL, the rest of the protocol needs about {vol:.0f} uL",
                                {"reagent": reagent, "believed_ul": round(have, 1), "needed_ul": round(vol, 1)})
                if a:
                    out.append(a)
        return out

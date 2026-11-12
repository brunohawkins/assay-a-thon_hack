"""Syringe pump adapter: drives generic RS-232 / USB-serial syringe pumps.

UNTESTED – no hardware available. Written to plug in when pumps arrive.

Pieces
  SyringePumpDriver   one pump on one serial port: set diameter, set rate, set volume,
                      run, stop, query status. Command strings come from
                      contracts/hardware/pump_<model>.json (see pump_generic.json), so a
                      different pump model needs a new JSON file, not new code.
  SyringePumpAdapter  the adapter the controller talks to (same interface as the virtual
                      chip): maps each reagent to a pump (contracts/hardware/rig_example.json)
                      and turns each time slice of a command into rate + volume + run, then
                      waits for the pump to finish. Routing needs a valve controller
                      (valves.py, a stub for now); without one it logs the route for a
                      person to set by hand. Sensors come from sensors.py (stubs).

Serial access needs pyserial (`pip install pyserial`). It is deliberately NOT in
requirements.txt: only someone with the hardware needs it, and it is imported lazily.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from adapters.base import Adapter

ROOT = Path(__file__).resolve().parents[2]
HARDWARE_DIR = ROOT / "contracts" / "hardware"


def load_pump_model(model: str) -> dict:
    path = HARDWARE_DIR / f"pump_{model}.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class SyringePumpDriver:
    """One syringe pump. UNTESTED – no hardware available."""

    def __init__(self, port: str, model: str = "generic", diameter_mm: float | None = None,
                 baud: int | None = None, address: str = "", timeout_s: float = 2.0):
        self.cfg = load_pump_model(model)
        self.port = port
        self.baud = baud or self.cfg.get("baud", 9600)
        self.address = address
        self.diameter_mm = diameter_mm or self.cfg.get("default_diameter_mm")
        self.timeout_s = timeout_s
        self.serial = None

    # --- transport ----------------------------------------------------------------------

    def connect(self) -> None:
        try:
            import serial  # pyserial, only needed with real hardware
        except ImportError as e:
            raise RuntimeError("pyserial is needed for real pumps: pip install pyserial") from e
        self.serial = serial.Serial(self.port, self.baud, timeout=self.timeout_s,
                                    bytesize=self.cfg.get("bytesize", 8), parity=self.cfg.get("parity", "N"),
                                    stopbits=self.cfg.get("stopbits", 1))
        if self.diameter_mm:
            self.set_diameter(self.diameter_mm)

    def close(self) -> None:
        if self.serial is not None:
            self.serial.close()
            self.serial = None

    def _command(self, key: str, **values) -> str:
        template = self.cfg["commands"][key]
        return self.cfg.get("address_format", "{address}").format(address=self.address) + template.format(**values)

    def send_raw(self, text: str) -> str:
        if self.serial is None:
            raise RuntimeError(f"pump on {self.port} is not connected (call connect())")
        term = self.cfg.get("terminator", "\r")
        self.serial.write((text + term).encode("ascii"))
        reply = self.serial.read_until(self.cfg.get("reply_terminator", "\x03").encode("ascii"))
        reply_text = reply.decode("ascii", errors="replace").strip("\x02\x03\r\n ")
        for bad in self.cfg.get("error_markers", []):
            if bad in reply_text:
                raise RuntimeError(f"pump on {self.port} rejected {text!r}: {reply_text!r}")
        return reply_text

    # --- operations -----------------------------------------------------------------------

    def set_diameter(self, mm: float) -> str:
        return self.send_raw(self._command("set_diameter", diameter_mm=f"{mm:.2f}"))

    def set_rate(self, ul_min: float) -> str:
        return self.send_raw(self._command("set_rate", rate=f"{ul_min:.3f}", unit=self.cfg.get("rate_unit", "UM")))

    def set_volume(self, ul: float) -> str:
        return self.send_raw(self._command("set_volume", volume=f"{ul:.3f}", unit=self.cfg.get("volume_unit", "UL")))

    def set_direction(self, infuse: bool = True) -> str:
        return self.send_raw(self._command("infuse" if infuse else "withdraw"))

    def run(self) -> str:
        return self.send_raw(self._command("run"))

    def stop(self) -> str:
        return self.send_raw(self._command("stop"))

    def status(self) -> dict:
        reply = self.send_raw(self._command("status"))
        running = any(m in reply for m in self.cfg.get("running_markers", []))
        return {"raw": reply, "running": running}

    def dispensed_ul(self) -> float | None:
        if "dispensed" not in self.cfg["commands"]:
            return None
        reply = self.send_raw(self._command("dispensed"))
        digits = "".join(ch for ch in reply if ch in "0123456789.")
        return float(digits) if digits else None

    def deliver(self, ul: float, ul_min: float, poll_s: float = 0.5) -> float | None:
        """Infuse `ul` at `ul_min` and block until the pump stops (or a timeout)."""
        self.set_direction(True)
        self.set_rate(ul_min)
        self.set_volume(ul)
        self.run()
        deadline = time.monotonic() + ul / ul_min * 60 * 1.5 + 10
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            if not self.status()["running"]:
                break
        else:
            self.stop()
            raise RuntimeError(f"pump on {self.port} did not finish {ul:.2f} uL in time; stopped")
        return self.dispensed_ul()


class SyringePumpAdapter(Adapter):
    """Controller-facing adapter for a rig of syringe pumps. UNTESTED – no hardware available."""

    name = "syringe_pump"
    simulated = False

    def __init__(self, chip: dict, parameters: dict, scenario: dict | None = None, reagents: list[str] | None = None,
                 rig_file: str | Path | None = None, valves=None, sensors: dict | None = None):
        rig_path = Path(rig_file or (scenario or {}).get("rig") or HARDWARE_DIR / "rig_example.json")
        with open(rig_path, encoding="utf-8") as f:
            self.rig = json.load(f)
        self.pumps: dict[str, SyringePumpDriver] = {}
        for reagent, p in self.rig.get("pumps", {}).items():
            self.pumps[reagent] = SyringePumpDriver(p["port"], p.get("model", "generic"), p.get("diameter_mm"),
                                                    p.get("baud"), p.get("address", ""))
        self.valves = valves          # valves.ValveController, when built
        self.sensors = sensors or {}  # name -> sensors.* instance, when built
        self.clock_s = 0.0
        self.last_dispensed = 0.0
        self.log: list[str] = []
        missing = [r for r in (reagents or []) if r not in self.pumps]
        if missing:
            raise RuntimeError(f"rig {rig_path.name} has no pump for: {', '.join(missing)}")
        for pump in self.pumps.values():
            pump.connect()

    def capabilities(self) -> dict:
        return {"sensors": ["pump_volume_ul", *self.sensors.keys()], "actuators": ["pump"] + (["valves"] if self.valves else []),
                "simulated": False, "untested": True}

    def _route(self, route: str) -> None:
        if self.valves is None:
            self.log.append(f"[t={self.clock_s:.1f}] MANUAL ROUTING NEEDED: {route}")
        else:
            self.valves.route(route)

    def send(self, command: str, args: dict, **meta) -> dict:
        if command == "wait":
            time.sleep(args["duration_s"])
            self.clock_s += args["duration_s"]
            return {"ok": True}
        if command == "hold_temp":
            raise NotImplementedError("heater control is not built yet (see sensors.py / docs/hardware_roadmap.md)")
        if command not in ("exchange", "load", "release"):
            raise NotImplementedError(f"{command} needs the rig's off-chip routing valves to move capsules between "
                                      f"chambers (adapters/valves.py is still a stub)")
        reagent = args.get("reagent", "capsule_suspension")
        direction = args.get("direction", "forward")
        self._route(f"{command} {direction} via {args['target']}")
        delivered = self.pumps[reagent].deliver(args["volume_ul"], args["flow_ul_min"])
        self.last_dispensed = args["volume_ul"] if delivered is None else delivered
        self.clock_s += args["volume_ul"] / args["flow_ul_min"] * 60
        return {"ok": True, "dispensed_ul": self.last_dispensed}

    def read_sensors(self) -> dict:
        out = {"pump_volume_ul": self.last_dispensed}
        for name, sensor in self.sensors.items():
            out[name] = sensor.read()
        return out

    def close(self) -> None:
        for p in self.pumps.values():
            p.close()

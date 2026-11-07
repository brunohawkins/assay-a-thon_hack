"""The interface every hardware adapter implements (PyLabRobot-style frontend/backend).

The controller (frontend) only ever talks to this interface, so the same operator code
drives the virtual chip now and real pumps, valves and sensors later: swap the adapter.

    adapter.capabilities() -> {"sensors": [...], "actuators": [...], "simulated": bool}
    adapter.send(command, args, **meta) -> result dict      # one time slice of a command
    adapter.read_sensors() -> {"pressure_kpa": 2.6, ...}    # only the sensors it has
    adapter.true_state() -> dict | None                     # simulation only; None on hardware
    adapter.close()

Sensor names (all optional): pump_volume_ul, pressure_kpa, outlet_absorbance,
temperature_c, capsules_counted.

Slice commands: exchange / load / release carry the slice's volume_ul and flow_ul_min;
hold_temp and wait carry duration_s.
"""

from __future__ import annotations

SENSORS = ("pump_volume_ul", "pressure_kpa", "outlet_absorbance", "temperature_c", "capsules_counted")


class Adapter:
    name = "base"
    simulated = False

    def capabilities(self) -> dict:
        return {"sensors": [], "actuators": [], "simulated": self.simulated}

    def send(self, command: str, args: dict, **meta) -> dict:
        raise NotImplementedError

    def read_sensors(self) -> dict:
        return {}

    def true_state(self) -> dict | None:
        return None

    def close(self) -> None:
        pass

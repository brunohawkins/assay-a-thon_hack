"""Sensor drivers. STUBS.

UNTESTED – no hardware available. Not implemented: read() raises NotImplementedError.
Each class documents what it should return so the closed loop can use it as soon as the
hardware exists. Readings use the same names and units as the virtual chip:

  PressureSensor     -> "pressure_kpa": gauge pressure just above the retention mesh, kPa.
                        e.g. an inline microfluidic pressure sensor, or a board-mount
                        differential sensor on a tee, read over I2C/analog by a
                        microcontroller. Closes the loop on clogging and on the
                        mesh-resistance model.
  TemperatureSensor  -> "temperature_c": at the chip under the trap, C. e.g. a thermistor
                        or a thin-film RTD on the Peltier stage. Closes hold_temp.
  AbsorbanceSensor   -> "outlet_absorbance": absorbance of the outlet stream at the dye's
                        wavelength, AU (relative to clean buffer). e.g. an LED + photodiode
                        across a transparent outlet tube. Closes "is the wash complete".
  CapsuleCounter     -> "capsules_counted": capsules seen passing the outlet in the last
                        slice. e.g. a camera over the outlet channel with a blob counter.

All are read by a microcontroller that answers "READ <sensor>" with "VALUE <number>"
over USB serial (115200 8N1), the same link as the valve controller.
"""

from __future__ import annotations


class _Sensor:
    name = "sensor"

    def __init__(self, port: str, channel: str = ""):
        self.port, self.channel = port, channel

    def read(self) -> float:
        raise NotImplementedError(f"{self.name} driver not built yet (see operator/adapters/sensors.py)")


class PressureSensor(_Sensor):
    """UNTESTED – no hardware available. Returns pressure_kpa."""
    name = "pressure_kpa"


class TemperatureSensor(_Sensor):
    """UNTESTED – no hardware available. Returns temperature_c."""
    name = "temperature_c"


class AbsorbanceSensor(_Sensor):
    """UNTESTED – no hardware available. Returns outlet_absorbance."""
    name = "outlet_absorbance"


class CapsuleCounter(_Sensor):
    """UNTESTED – no hardware available. Returns capsules_counted."""
    name = "capsules_counted"

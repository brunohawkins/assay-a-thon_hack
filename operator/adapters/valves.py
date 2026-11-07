"""Valve controller (pinch or solenoid valves via a microcontroller). STUB.

UNTESTED – no hardware available. Not implemented: every method raises
NotImplementedError. This documents the plan so the firmware and this driver can be
written to the same protocol.

Hardware: pinch valves (tubing squeezed shut) or 2/3-way solenoid valves, switched by
MOSFETs or a relay board on a microcontroller (Arduino / RP2040), USB serial, 115200 8N1.

Serial protocol (ASCII, newline-terminated, one request at a time):
  host -> mcu   "SET <valve_id> <0|1>"        open (1) / close (0) one valve
                "ROUTE <name>"                apply a named route stored in the firmware
                "GET <valve_id>"              read one valve's state
                "ALL 0"                       close everything (safe state)
  mcu -> host   "OK"  |  "STATE <valve_id> <0|1>"  |  "ERR <message>"
Valve ids and named routes come from a rig file (contracts/hardware/rig_example.json,
"valves" and "routes"), e.g. "wash_forward": inlet_1 -> trap_1 -> waste_1.
On any error, or if the host disconnects, the firmware closes all valves.
"""

from __future__ import annotations


class ValveController:
    """UNTESTED – no hardware available. Stub."""

    def __init__(self, port: str, routes: dict | None = None, baud: int = 115200):
        self.port, self.routes, self.baud = port, routes or {}, baud

    def set(self, valve_id: str, open_: bool) -> None:
        raise NotImplementedError("valve controller not built yet: see the protocol in this module's docstring")

    def route(self, name: str) -> None:
        raise NotImplementedError("valve controller not built yet: see the protocol in this module's docstring")

    def all_closed(self) -> None:
        raise NotImplementedError("valve controller not built yet: see the protocol in this module's docstring")

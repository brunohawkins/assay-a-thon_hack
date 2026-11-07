"""Serial pump adapter: same interface as SimAdapter, for real hardware later.

Not implemented this weekend (no pumps). When hardware arrives, this is the plan:

Transport
    USB serial, 115200 baud, 8N1, newline-terminated ASCII lines. Needs `pyserial`,
    which is NOT in requirements.txt yet; add it only when this adapter is built.

Request (host -> controller), one line per command:
    <seq> <COMMAND> <json-args>\\n
    e.g.  7 EXCHANGE {"target":"exchange_1","reagent":"wash_buffer","volume_ul":100,"washes":3,"flow_ul_min":100}

    <seq> is an integer that increases by 1 per request. The controller firmware maps
    chamber ids to valve/pump channels from its own table, generated from chip_spec.json.

Responses (controller -> host):
    <seq> ACK\\n                 command accepted, starting
    <seq> PROGRESS <0-100>\\n    optional progress updates
    <seq> DONE <json-result>\\n  finished; result like {"t_s": 812.0}
    <seq> ERR <message>\\n       refused or failed; host stops the run

Rules
    - One command in flight at a time; send() blocks until DONE or ERR.
    - Timeout per command: estimated duration (commands.estimate_duration_s) + 30 s.
    - On ERR or timeout, send "<seq> STOP" and raise.
    - Every sent command is also recorded via SimAdapter so the twin can replay
      the real run.
"""

from __future__ import annotations


class SerialPumpAdapter:
    name = "serial_pump"

    def __init__(self, port: str = "/dev/tty.usbmodem0", baud: int = 115200, chip: dict | None = None):
        self.port = port
        self.baud = baud
        self.chip = chip

    def send(self, command: str, args: dict, **meta) -> dict:
        """Send one command to the controller and wait for DONE. See module docstring."""
        raise NotImplementedError(
            "serial_pump adapter is a stub. Use the sim adapter (default) until hardware exists; "
            "see operator/adapters/serial_pump.py for the planned serial protocol."
        )

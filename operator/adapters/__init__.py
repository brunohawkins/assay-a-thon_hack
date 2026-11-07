"""Adapters turn commands into action. They all share one interface:

    adapter.send(command: str, args: dict, **meta) -> dict

- sim.SimAdapter         records commands into a run file (the default)
- serial_pump.SerialPumpAdapter   talks to real hardware (stub for now)

`meta` is optional context (narration, protocol_step, duration_s) that the sim adapter
records and hardware adapters may ignore.
"""

import sys
from pathlib import Path

# Let adapters import operator/commands.py however they were reached.
_OPERATOR_DIR = str(Path(__file__).resolve().parents[1])
if _OPERATOR_DIR not in sys.path:
    sys.path.insert(0, _OPERATOR_DIR)

"""Adapters turn commands into action. The interface is in base.py:

    adapter.capabilities(), adapter.send(command, args, **meta), adapter.read_sensors()

- virtual_chip.VirtualChip   SIMULATED HARDWARE for the closed loop (default with a scenario)
- sim.SimAdapter             records commands into a run file (simple runs, no scenario)

`meta` is optional context that recording adapters keep and hardware adapters may ignore.
"""

import sys
from pathlib import Path

# Let adapters import operator/commands.py however they were reached.
_OPERATOR_DIR = str(Path(__file__).resolve().parents[1])
if _OPERATOR_DIR not in sys.path:
    sys.path.insert(0, _OPERATOR_DIR)

"""S4's working parts: deciding which unit ops share a chip, and where to cut.

One assay does not fit on one chip, and ``assay2chip/README.md`` calls deciding *where*
to cut "stage S4 and the heart of this pipeline". The decision is split across small
modules so that each constraint can be read, tested and argued with on its own:

    ops.py          one typed view of a unitops.json op; "runs" of ops that share a part
    graph.py        exact planarity, and the one-layer-with-ports-on-the-rim test
    chemistry.py    which fluids may share a wetted path and which may never meet
    thermal.py      live cells versus 42 C, the 98 C dock, and the material that follows
    ports.py        the port count, calibrated against the six chips already in the repo
    area.py         board area from volume / etch depth, walls, port cells, channel runs
    constraints.py  can these ops share a chip, and at which envelope
    handoffs.py     what moves between chips, with risks computed from its own numbers
    segment.py      greedy segmentation of the flow-ordered ops, then local improvement
    mvp_chain.py    the repo's hand-built MVP chain as unitops, for the comparison test

The stage that drives them is ``assay2chip/stages/s4_partition.py``.

Every number comes from ``assay2chip/lib/contracts.py``, including the tubing, valve and
per-particle density values the handoff risks need. Where a contract value is a band and
the question has a conservative end, the end taken is part of the output: a handoff risk
asks what is LOST in the line, so it reads the maximum excess density and says so.

Two things have no contract and are therefore named constants in the module that uses
them, with every use reported in ``unresolved``: a ``split``'s fan-out when the assay
never states it (``ports.SPLIT_FALLBACK``), and the reagent-versus-reagent compatibility
table (``chemistry.SOURCE``). Nothing else is a literal.
"""

from __future__ import annotations

from assay2chip.partition import (  # noqa: F401
    area, chemistry, constraints, graph, handoffs, mvp_chain, ops, ports, segment, thermal,
)

__all__ = [
    "area", "chemistry", "constraints", "graph", "handoffs", "mvp_chain", "ops", "ports",
    "segment", "thermal",
]

"""protocolgen: the physics and bookkeeping behind S8 (and the grouping rules behind S5).

S8 turns a BUILT and ROUTED chip spec plus `unitops.json` into a protocol that passes
`contracts/schemas/protocol.schema.json`, the way `protocols/build_module3.py` and
`protocols/build_cell_prep.py` do it by hand: nothing numeric about liquid is typed in.
Every volume is a number of chamber volumes of a chamber in the chip spec, every flow is
derived from that chamber's real cross-section and the shear or pressure ceiling that
applies to what it holds, and every duration is volume x washes / flow plus a stated
margin.

Six modules, each one answerable for one thing:

  geometry   the chip as a graph: real cross-sections (never a bounding box), channel
             lengths, feed and drain ports, thermal zones.
  flows      wall shear, route resistance, and the one function that turns a chamber plus
             a `shear_class` into a flow with the ceiling it came from.
  exchange   what an exchange is FOR (`exchange_kind`) and the volume that follows from it.
  reagents   the generic reagent vocabulary, and the screen that keeps a source paper's
             wording out of a committed protocol.
  build      op -> step(s), step numbering, port states, and the assembled protocol.
  audit      the seven rules an audit of the hand-written protocols here turned up, as a
             checker that runs on our output and on anybody else's.

Every number that exists in a contract is read through `assay2chip/lib/contracts.py`.
Nothing here calls a model, touches the network or reads a clock: the same inputs give
byte-identical output.
"""

from __future__ import annotations

from . import audit, build, exchange, flows, geometry, reagents

__all__ = ["audit", "build", "exchange", "flows", "geometry", "reagents"]

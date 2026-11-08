"""S6's placement engine: sized parts in, a placed and ported chip spec out.

S6 itself (`assay2chip/stages/s6_build.py`) is the orchestrator. Everything it has to
*decide* lives here, in modules small enough to test on their own:

    needs.py        a unitops op -> Brief: the registry's `Need` plus the candidate blocks
                    worth trying for it and the gaps that had to be filled
    parts.py        the ONE place that talks to assay2chip/parts/registry.py: tries a
                    Brief's candidate blocks, keeps every refusal's reason, and answers
                    "which opening is the sample inlet / the drain / a spare feed"
    materials.py    which material the ops need, read out of contracts/materials.json
                    (a 98 C hold needs a high-Tg grade, and that is a lookup, not a guess)
    place.py        the optimiser: rotate and pack parts in flow order inside the
                    envelope, walls kept, routing corridors left open, channel length and
                    crossing risk minimised
    ports.py        boundary ports at a legal bore, pitch and count, each with the PORT PAD
                    the registry sizes, so a 0.8 mm hole opens onto real cut area
    spec.py         the chip spec itself: one fluidic layer, one etch depth, chambers with
                    the registry's volumes, thermal zones, part-internal channels
    connections.py  what S7 has to join, with widths - this stage never draws a channel
                    between two parts

Nothing in here sizes geometry of its own, and nothing holds a literal that exists in a
contract: every number comes through `assay2chip/lib/contracts.py` or the parts registry.
`materials.py` is the one module that opens a contract file directly, because
`lib/contracts.py` has no materials accessor yet; its TODO says so.
"""

from __future__ import annotations

from .connections import connection_file, min_width_mm
from .materials import Material, MaterialError, choose_material, layer_convention
from .needs import Brief, brief_for_op, briefs_for, candidate_blocks
from .parts import PartError, SizedPart, registry_source, size_part, size_parts
from .place import CannotFit, Placed, Placement, place_parts
from .ports import PlacedPort, PortBudgetExceeded, PortPlan, pad_budget, plan_ports
from .spec import build_chip_spec, schema_has_scale, thermal_zones

__all__ = [
    "Brief",
    "CannotFit",
    "Material",
    "MaterialError",
    "PartError",
    "Placed",
    "Placement",
    "PlacedPort",
    "PortBudgetExceeded",
    "PortPlan",
    "SizedPart",
    "brief_for_op",
    "briefs_for",
    "build_chip_spec",
    "candidate_blocks",
    "choose_material",
    "connection_file",
    "layer_convention",
    "min_width_mm",
    "pad_budget",
    "place_parts",
    "plan_ports",
    "registry_source",
    "schema_has_scale",
    "size_part",
    "size_parts",
    "thermal_zones",
]

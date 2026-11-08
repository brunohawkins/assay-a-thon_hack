"""assay2chip/parts: the parametric part registry, keyed by contracts/blocks.json ids.

Two problems live here and they turned out to be one problem.

The library parts do not fit. chip/components/catalogue.json is Yuvraj's Flowmatter
library and it is a SHAPE reference: its parts are 1.0-2.0 mm deep against a 0.3 mm layer
and the largest is 104.8 x 99.8 mm against a 30 x 20 mm envelope. Placing them as they are
is what produced a 317 x 116 mm, 1 mm deep, 12-port chip that failed the contract.

The blocks with real internal geometry had no geometry. A block audit found that every
one of them - the weir crest, the comb teeth, the serpentine length, the droplet nozzle -
was specified only in English prose, so nothing downstream could build it from data.

The fix for both is the same: size every part from physics, from the Need, against the
contract, and emit the internal features as data.

    from assay2chip.parts import registry, Need, PartSizingError

    registry.list()                                   # every block id and its status
    part = registry.size("serpentine_channel", Need(
        residence_s=600, flow_ul_min=5, particles="cells", particle_um=12,
        shear_class="none", depth_mm=0.3))
    part.footprint_mm, part.openings, part.features, part.chambers, part.channels
    part.risks          # e.g. the cargo settles before it reaches the far end
    part.satisfies      # the fabrication floors this part was checked against

Every contract value is read through assay2chip.lib.contracts. There is no number in this
package that duplicates one in contracts/blocks.json or contracts/parameters.json, and
`assay2chip/parts/derived.py` exists so that even the three values the contract states in
prose are read rather than retyped.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.parts.geometry import (  # noqa: E402
    Expansion, Feature, FloorCheck, Load, Need, Opening, PartGeometry, PartSizingError,
    VolumeRecord,
)
from assay2chip.parts import derived, flowmatter, physics, registry, sizers  # noqa: E402

__all__ = [
    "Expansion", "Feature", "FloorCheck", "Load", "Need", "Opening", "PartGeometry",
    "PartSizingError", "VolumeRecord", "derived", "flowmatter", "physics", "registry",
    "sizers",
]

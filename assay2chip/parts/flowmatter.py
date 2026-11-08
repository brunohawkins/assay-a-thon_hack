"""Where Yuvraj's 21 Flowmatter parts sit against the contract's block vocabulary.

The catalogue is a SHAPE REFERENCE, not a source of dimensions. Its parts are 1.0 mm deep
against a 0.3 mm layer, and the largest is 104.8 x 99.8 mm against a 30 x 20 mm envelope -
an STL that is 17x the whole chip. Placing them as they are is what produced a 317 x 116
mm, 1 mm deep, 12-port chip that failed the contract outright. So this module maps names,
and the registry sizes geometry.

Three things it is honest about:

  which Flowmatter part corresponds to which block id,
  which blocks have NO Flowmatter shape at all (including every block with internal
  geometry: the weir, the comb, the pillars, the mixer, the guide, the restrictor),
  which Flowmatter NAMES are guesses - the catalogue says so itself in name_confidence,
  and a guessed name is a guessed correspondence.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402

CATALOGUE_PATH = ROOT / "chip" / "components" / "catalogue.json"

# Flowmatter part name -> (block id or None, why). Everything here is a NAME mapping. No
# dimension from the catalogue is ever used: see this module's docstring.
MAPPING: dict[str, tuple[str | None, str]] = {
    "spiral_channel": ("spiral_channel", "same shape; the STL is 104.8 x 99.8 mm, 17x the "
                                         "demo envelope, so only the shape transfers"),
    "flow_manifold": ("flow_manifold", "catalogue admits manifold vs distributor was "
                                       "guessed from the shape, and the two differ "
                                       "hydraulically: a header splits unevenly, a "
                                       "distributor's matched paths do not"),
    "culture_chamber": ("culture_chamber", "same shape; the STL is 20 x 80 mm, longer than "
                                           "the chip, so the size comes from shear"),
    "flow_distributor": ("flow_distributor", "same name-confidence caveat as flow_manifold"),
    "y_junction": ("y_junction", "direct"),
    "cross_junction": ("cross_junction", "direct. Note the catalogue files its ROLE as "
                                         "flow_focusing; flow_focusing is a separate block "
                                         "id with no Flowmatter shape of its own"),
    "t_junction": ("t_junction", "direct"),
    "fork_junction": ("fork_junction", "direct"),
    "y_junction_curved": ("curved_y_junction", "catalogue name and block id differ in word "
                                               "order only"),
    "serpentine_channel": ("serpentine_channel", "direct. The catalogue calls it a mixer; "
                                                 "at Re << 1 it is a delay line, and "
                                                 "mixing needs herringbone_mixer"),
    "snake_channel": ("snake_channel", "direct, same mixer caveat"),
    "ring_junction": ("ring_junction", "direct"),
    "curved_channel": ("curved_channel", "direct"),
    "channel_corner": ("channel_corner", "direct"),
    "tesla_valve": ("tesla_valve", "direct, and the block is REFUSED: blocks.json states "
                                   "diodicity is exactly 1.0 in Stokes flow"),
    "straight_channel": ("straight_channel", "direct. The STL is 2.0 mm deep, nearly 7x "
                                             "the layer"),
    "port": ("port", "teardrop shape ASSUMED to be the port; the STL is 2.0 mm deep and "
                     "25 x 10 mm, where the contract's port is a 0.8 mm hole"),
    "mesh_body_a": (None, "role unknown, listed as MeshBody19/20 in Fusion. A mesh is the "
                          "one retention route the contract calls BOUGHT, not fabricated, "
                          "so this may not be a block at all"),
    "mesh_body_b": (None, "as mesh_body_a"),
    "normally_open_valve": (None, "valves are not a blocks.json block: a chip spec files "
                                  "them under `valves`, each on a channel with a control "
                                  "port"),
    "normally_closed_valve": (None, "as normally_open_valve; the catalogue also says the "
                                    "two look identical from above, so which is which is "
                                    "a guess"),
}


@dataclass(frozen=True)
class CataloguePart:
    part: str
    block: str | None
    why: str
    size_mm: tuple[float, float, float]
    volume_ul: float
    name_confidence: str

    @property
    def name_is_a_guess(self) -> bool:
        """The catalogue grades its own naming; anything but 'high' is a guess."""
        return not self.name_confidence.startswith("high")

    @property
    def too_deep(self) -> bool:
        return self.size_mm[2] > contracts.max_depth_mm() + 1e-9

    @property
    def fits_demo(self) -> bool:
        return contracts.envelope("demo").fits(self.size_mm[0], self.size_mm[1])


def catalogue() -> list[CataloguePart]:
    """The 21 Flowmatter parts, each with the block id it corresponds to or None."""
    import json

    data = json.loads(CATALOGUE_PATH.read_text())
    out = []
    for p in data["parts"]:
        block, why = MAPPING.get(p["part"], (None, "not reviewed"))
        if block is not None and block not in contracts.library():
            raise contracts.ContractError(
                f"assay2chip/parts/flowmatter.py maps '{p['part']}' to '{block}', which is "
                f"not a block id in contracts/blocks.json")
        out.append(CataloguePart(p["part"], block, why, tuple(p["size_mm"]),
                                 p["volume_ul"], p["name_confidence"]))
    return out


def mapped_blocks() -> set[str]:
    return {c.block for c in catalogue() if c.block}


def blocks_without_a_shape() -> list[str]:
    """Block ids in the contract library that no Flowmatter part corresponds to.

    This is the list that matters: it contains every block the audit found specified only
    in prose, so there was neither a shape nor a number for them anywhere.
    """
    return sorted(contracts.library() - mapped_blocks())


def guessed_names() -> list[CataloguePart]:
    """Flowmatter parts whose own name_confidence is not 'high'."""
    return [c for c in catalogue() if c.name_is_a_guess]


def unmapped_parts() -> list[CataloguePart]:
    """Flowmatter parts that correspond to no block id."""
    return [c for c in catalogue() if c.block is None]


def summary() -> dict:
    """Plain JSON for the S12 report and the tests."""
    parts = catalogue()
    return {
        "catalogue": str(CATALOGUE_PATH.relative_to(ROOT)),
        "parts": len(parts),
        "blocks_in_library": len(contracts.library()),
        "parts_mapped": sum(1 for c in parts if c.block),
        "blocks_covered": len(mapped_blocks()),
        "blocks_without_a_flowmatter_shape": blocks_without_a_shape(),
        "parts_with_no_block": [
            {"part": c.part, "why": c.why, "name_confidence": c.name_confidence}
            for c in unmapped_parts()],
        "guessed_names": [
            {"part": c.part, "block": c.block, "name_confidence": c.name_confidence}
            for c in guessed_names()],
        "none_fit_the_layer": {
            "deeper_than_max_depth": [c.part for c in parts if c.too_deep],
            "larger_than_the_demo_envelope": [c.part for c in parts if not c.fits_demo],
            "why_it_matters": (
                "every part is 1.0-2.0 mm deep against a 0.3 mm layer, which is why the "
                "registry sizes parametrically and treats these STLs as shape reference "
                "only"),
        },
        "mapping": [
            {"part": c.part, "block": c.block, "size_mm": list(c.size_mm),
             "name_confidence": c.name_confidence, "why": c.why}
            for c in parts],
    }

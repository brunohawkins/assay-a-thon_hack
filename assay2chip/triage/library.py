"""The block vocabulary guard: what S3 is allowed to call a part.

`placement.part` is a block id from contracts/blocks.json `basic` or `advanced`. Nothing
else. Not a catalogue STL name, not an invented name, not a multilayer block, and never a
block whose own description says it cannot do the job asked of it.

Three bans, each with a reason in the contract rather than in anybody's opinion:

  multilayer      contracts/blocks.json `excluded` removes all 15 multilayer blocks: we
                  print ONE fluidic layer, so a via, a membrane, a second level or a
                  crossing channel is unavailable. Any name not in basic|advanced is
                  rejected, which covers them.
  tesla_valve     "USELESS BELOW Re 1: diodicity is exactly 1.0 in Stokes flow". Our
                  flows are Re ~ 0.02, so it is never a rectifier here. Banned outright,
                  because rectification is the only thing anyone reaches for it for.
  pillar_array,   contracts/blocks.json retention.not_viable: they "cannot retain by size
  pillar_funnel_  exclusion at a 200 um throat". Allowed for support or as a coarse
  trap            pre-filter; banned the moment the purpose is retention.

An audit of this repo found 8 features whose block did not match their function, which is
why `part_for` refuses to return a block at all when nothing fits: the caller then has to
emit `missing_part` with a `need`, and the gap stays visible.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402


class PartError(ValueError):
    """A part name S3 is not allowed to emit, with the contract reason why."""


#: Blocks banned for every purpose, and the contract sentence that bans them.
BANNED: dict[str, str] = {
    "tesla_valve": (
        "contracts/blocks.json advanced.tesla_valve: 'USELESS BELOW Re 1: diodicity is "
        "exactly 1.0 in Stokes flow, so do not use it for rectification at our flow "
        "rates' - our channels run at Re of order 0.02"
    ),
}

#: Blocks banned when the purpose is retention by size exclusion.
BANNED_FOR_RETENTION: dict[str, str] = {
    "pillar_array": "contracts/blocks.json retention.not_viable",
    "pillar_funnel_trap": "contracts/blocks.json retention.not_viable",
    "dld_array": (
        "contracts/blocks.json retention.alternatives_ranked: a DLD at our 200 um gap has "
        "a ~105 um critical diameter, so it is a continuous wash, not retention"
    ),
}

#: Words that give away an invented multilayer part, for a clearer error message.
MULTILAYER_WORDS = ("membrane", "via", "valve_layer", "crossing", "bridge", "stacked",
                    "second_layer", "pneumatic")

RETENTION_PURPOSES = ("retain", "retention", "trap", "hold back", "size exclusion",
                      "size-exclusion")


def is_block(part: str) -> bool:
    return part in contracts.library()


def check(part: str, purpose: str = "") -> str:
    """Return `part` if S3 may emit it for this purpose, else raise PartError.

    `purpose` is free text; it only decides the retention bans, so a caller that is
    placing a pillar_array to hold a lid flat says 'lid support' and passes.
    """
    if part in BANNED:
        raise PartError(f"'{part}' may not be used at our flow rates: {BANNED[part]}")
    if not is_block(part):
        extra = ""
        if any(word in part for word in MULTILAYER_WORDS):
            extra = (" It reads like a multilayer part, and contracts/blocks.json "
                     "`excluded` removes all 15 multilayer blocks: we print ONE fluidic "
                     "layer, so a via, a membrane or a crossing channel is unavailable.")
        raise PartError(
            f"'{part}' is not a block id in contracts/blocks.json basic or advanced, so it "
            f"is not a part this project can place.{extra} Emit missing_part with a `need` "
            f"instead of forcing a wrong block."
        )
    lowered = purpose.lower()
    if any(word in lowered for word in RETENTION_PURPOSES) and part in BANNED_FOR_RETENTION:
        raise PartError(
            f"'{part}' cannot retain by size exclusion at the in-plane floor "
            f"({BANNED_FOR_RETENTION[part]}); use the depth weir (trap_chamber) or say "
            f"missing_part."
        )
    return part


def describe(part: str) -> str:
    """The block's own one-line description from contracts/blocks.json."""
    for group in ("basic", "advanced"):
        table = contracts.block(group)
        if part in table:
            return str(table[part])
    return ""


#: Purpose -> block id, with the repo precedent that already does it this way. This is the
#: only table that turns an intent into a part, so a reviewer can check every choice in
#: one place. Every value is checked against contracts/blocks.json by `audit()`.
PARTS: dict[str, tuple[str, str]] = {
    "droplet_generation": ("flow_focusing",
                           "contracts/blocks.json advanced.flow_focusing; precedent "
                           "chip/spc_demo_v1.json xj_1"),
    "capsule_trap": ("trap_chamber",
                     "contracts/blocks.json retention.weir_geometry.block; precedent "
                     "chip/module3_v1_translated.json main_trap"),
    "culture": ("culture_chamber",
                "precedent chip/module1_v1.json n_cx and chip/cell_prep_v1.json cult_1"),
    "thermocycle": ("polygon_chamber",
                    "precedent chip/module4_v1.json sub_1-sub_4, the index-PCR chambers"),
    "static_hold": ("polygon_chamber", "contracts/blocks.json basic.polygon_chamber"),
    "sedimentation": ("polygon_chamber",
                      "contracts/blocks.json retention.alternatives_ranked, the "
                      "sedimentation sump"),
    "qc_window": ("polygon_chamber", "precedent chip/cell_prep_v1.json qc_1 (qc_window)"),
    "bead_capture": ("polygon_chamber",
                     "precedent chip/module5_v1.json bead_cx; retention is magnetic, not "
                     "geometric (protocols/module5_design.md section 6)"),
    "delay_line": ("serpentine_channel", "chip/designs/README.md rule 4"),
    "delay_line_viscous": ("snake_channel", "chip/designs/README.md rule 2"),
    "mix": ("herringbone_mixer",
            "contracts/blocks.json advanced.herringbone_mixer, the one mixer whose "
            "description holds at low Re"),
    "mix_viscous": ("snake_channel", "chip/designs/README.md rule 2"),
    "merge": ("y_junction", "chip/designs/README.md rule 2"),
    "merge_gentle": ("curved_y_junction",
                     "contracts/blocks.json basic.curved_y_junction, lower shear than a "
                     "sharp Y"),
    "split": ("flow_distributor", "chip/designs/README.md rule 5"),
    "split_metered": ("metering_trap",
                      "contracts/blocks.json advanced.metering_trap, the block for metered "
                      "splitting"),
    "pool": ("ring_junction", "chip/designs/README.md rule 5"),
    "collect": ("port", "contracts/blocks.json basic.port, the only fluid outlet"),
    "long_path": ("spiral_channel",
                  "contracts/blocks.json advanced.spiral_channel, used as a long path "
                  "only: inertial focusing needs Re >> 1 and ours is ~0.02"),
}


def part_for(purpose: str) -> tuple[str, str]:
    """(block id, citation) for a purpose in PARTS. Raises if the purpose is unknown.

    An unknown purpose is not an excuse to invent a block: the caller must emit
    missing_part with a `need`.
    """
    if purpose not in PARTS:
        raise PartError(
            f"no block is mapped to the purpose {purpose!r}; add it to "
            f"assay2chip/triage/library.PARTS with its contract or precedent, or emit "
            f"missing_part with a `need`"
        )
    part, citation = PARTS[purpose]
    return check(part, purpose), citation


_shapes: dict = {}


def catalogue_part_for(part: str) -> "str | None":
    """The Flowmatter catalogue part whose SHAPE matches this block, or None.

    Display only: those parts are 1.0 mm deep against a 0.3 mm layer and some are larger
    than the whole chip, so none is ever placed as-is. The mapping lives in
    assay2chip/parts/flowmatter.py, which owns it; this is a read-only lookup, and a block
    with no Flowmatter shape - the weir, the mixer, the restrictor, every block with
    internal geometry - simply has no `catalogue_part`.
    """
    if "map" not in _shapes:
        _shapes["map"] = {}
        try:
            from assay2chip.parts import flowmatter
            for name, (block_id, _why) in flowmatter.MAPPING.items():
                if block_id and block_id not in BANNED and block_id not in _shapes["map"]:
                    _shapes["map"][block_id] = name
        except Exception:
            # assay2chip/parts/ is being built in parallel. If its mapping is not importable
            # yet, S3 simply omits the display-only catalogue_part rather than guessing one.
            pass
    return _shapes["map"].get(part)


def audit() -> list[str]:
    """Problems with this module's own tables. Empty list means the vocabulary is clean."""
    problems: list[str] = []
    allowed = contracts.library()
    for purpose, (part, citation) in sorted(PARTS.items()):
        if part not in allowed:
            problems.append(f"PARTS[{purpose!r}] = {part!r}, which is not a block id in "
                            f"contracts/blocks.json basic or advanced")
        if part in BANNED:
            problems.append(f"PARTS[{purpose!r}] = {part!r}, which is banned: {BANNED[part]}")
        if not citation:
            problems.append(f"PARTS[{purpose!r}] has no contract key or repo precedent")
    for part in sorted(BANNED_FOR_RETENTION):
        if part not in allowed:
            problems.append(f"BANNED_FOR_RETENTION names {part!r}, which is not a block id")
    return problems

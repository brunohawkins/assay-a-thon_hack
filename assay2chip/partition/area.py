"""Will these ops fit an envelope? Sized from `load`, never from the paper's volume.

**This module used to divide ``fluid.volume_ul`` by the etch depth, and that is what broke
the first end-to-end run at S6.** A paper that says "wash the pellet in 500 uL" is
describing a tube. 500 uL at the 0.3 mm single etch depth is 1,667 mm2, nearly three demo
boards, so every chip came out at target scale for no reason at all. ``assay2chip/README.md``,
"Bench volumes do not transfer", is the rule, and ``unitops.json`` now carries it: ops have
a ``load`` field for what the place must HOLD, and ``fluid.volume_ul`` is explicitly "the
PAPER'S volume ... kept for traceability ONLY ... must NEVER size a part".

So area comes from ``load.basis``:

``adherent_area``      the floor the cells need: ``area_mm2``, or ``cells`` divided by the
                       paper's ``surface_density_per_mm2``. A monolayer is an area, and it
                       does not care how much medium sat on top of it.
``packed_volume``      the bed: ``packed_volume_ul``, or ``cells`` x one swollen particle's
                       volume divided by ``capsule.packing_fraction``, then over the etch
                       depth. 100,000 capsules is a bed whatever tube they arrived in.
``suspension_volume``  ``cells`` divided by ``suspension_density_per_ul``, then over the
                       etch depth. Still not the paper's volume: the density is what was
                       stated, the volume follows from the count.
``flow_through``       nothing is held. A mixer, a junction or a delay line only has to
                       carry a stream, so it contributes a channel, not a chamber, and
                       ``residence_s`` is left for S6 to turn into a length.
``none``               no load, no area.

**A wash adds nothing.** It is a sweep of the chamber its cells already sit in - the
README's own words, "becomes a SWEEP of the chamber: -ln(1 - exchange.target_fraction)
chamber volumes" - so a wash never contributes area, and neither does any op sharing a run
with the op that holds the load: within one run the area is the MAXIMUM of its ops' loads,
not their sum. Different runs are different places, so those are summed.

An op with no ``load`` at all is a gap, recorded in ``unresolved``: the particle floor is
used so the chip is not under-sized, and the paper's volume is still never consulted.

The remaining terms are unchanged and each is a contract value:

**Particle floor.** A part is at least ``capsule_channel.width_factor_low`` x the *swollen*
diameter wide in both directions.
**Walls.** A wall ring at ``fabrication_floors.min_wall_mm`` round each part.
**Port cells.** Each port occupies a ``min_port_pitch_mm`` square: six ports claim 150 mm2
of the 600 mm2 demo board before any fluid.
**Channel runs.** Parts sit at least a port pitch apart and a channel is at least
``min_open_channel_mm`` wide.

Scale: ``demo`` if it fits, else ``target`` (ANSI/SLAS), never the ``ceiling``, which is a
maximum rather than a design scale and is not a value ``partition.schema.json`` accepts.
A single op too big for ``target`` cannot be halved, so it is reported pointing at the
contract's own answer - "KEEP CHAMBER LENGTH CONSTANT and replicate chambers".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from assay2chip.lib import contracts
from assay2chip.partition.ops import Op

#: Scales a chip may be built to, in order of preference. `ceiling` is not here on
#: purpose: it is a maximum, not a design target, and partition.schema.json forbids it.
SCALES = ("demo", "target")

#: Ops that never contribute area, whatever they declare. The list is deliberately almost
#: empty: an op's own ``load`` is the authority, and an op that holds nothing says so with
#: basis ``flow_through`` or ``none``. Only the README's one hard rule is forced here - a
#: reagent volume for a wash or rinse is "dropped - becomes a SWEEP of the chamber" - plus
#: imaging, which is not fluidic at all. `load`, `collect` and `pool` are NOT sweeps: they
#: put material into a place, which is exactly what decides the place has to hold it.
SWEEP_KINDS = ("wash", "image")


@dataclass(frozen=True)
class PartArea:
    op_numbers: Tuple[int, ...]
    area_mm2: Optional[float]
    side_mm: Optional[float]
    driver: str
    basis: str = "none"
    #: True when no op in the run carried a `load`, so this is a floor, not an estimate.
    from_floor: bool = False

    @property
    def known(self) -> bool:
        return self.area_mm2 is not None


@dataclass(frozen=True)
class AreaEstimate:
    """What the ops need, against what the envelope offers."""

    scale: str
    parts_mm2: float
    walls_mm2: float
    ports_mm2: float
    channels_mm2: float
    envelope_mm2: float
    longest_side_mm: Optional[float]
    envelope_short_side_mm: float
    #: Ops that state no load, so their share of the board is a floor rather than a size.
    unknown_loads: Tuple[int, ...]
    parts: Tuple[PartArea, ...]

    @property
    def total_mm2(self) -> float:
        return self.parts_mm2 + self.walls_mm2 + self.ports_mm2 + self.channels_mm2

    @property
    def fits(self) -> bool:
        if self.total_mm2 > self.envelope_mm2:
            return False
        if self.longest_side_mm is not None \
                and self.longest_side_mm > self.envelope_short_side_mm:
            return False
        return True

    @property
    def utilisation(self) -> float:
        return self.total_mm2 / self.envelope_mm2 if self.envelope_mm2 else 0.0

    def why(self) -> str:
        bases = sorted({p.basis for p in self.parts if p.known and p.basis != "none"})
        return ("%.0f mm2 needed (%.0f parts from load%s + %.0f walls + %.0f port cells "
                "at the %g mm pitch + %.0f channel runs) against %.0f mm2 of the %s "
                "envelope, %.0f%% full"
                % (self.total_mm2, self.parts_mm2,
                   " (%s)" % ", ".join(bases) if bases else "",
                   self.walls_mm2, self.ports_mm2,
                   contracts.floor("min_port_pitch_mm"), self.channels_mm2,
                   self.envelope_mm2, self.scale, 100 * self.utilisation))

    def why_not(self) -> str:
        if self.total_mm2 > self.envelope_mm2:
            return ("%.0f mm2 of parts, walls, port cells and channel runs does not fit "
                    "the %.0f mm2 of the %s envelope (blocks.json); the parts come from "
                    "the ops' `load`, not from the paper's tube volumes"
                    % (self.total_mm2, self.envelope_mm2, self.scale))
        return ("a part %.1f mm across does not fit the %g mm short side of the %s "
                "envelope (blocks.json)"
                % (self.longest_side_mm or 0.0, self.envelope_short_side_mm, self.scale))


# --- load -> area -------------------------------------------------------------

def _particle_floor(ops: Sequence[Op]) -> Optional[float]:
    """Side in mm of the narrowest part that still passes the swollen particle."""
    particles = [op.swollen_um for op in ops if op.swollen_um is not None]
    if not particles:
        return None
    factor = contracts.param("capsule_channel.width_factor_low")
    return factor * max(particles) / 1000.0


def _packed_volume_ul(op: Op) -> Optional[float]:
    """A bed's volume from its count, the swollen diameter and the packing fraction."""
    if op.load_packed_volume_ul is not None:
        return op.load_packed_volume_ul
    if op.load_cells is None or op.swollen_um is None:
        return None
    radius_mm = op.swollen_um / 2000.0
    one = 4.0 / 3.0 * math.pi * radius_mm ** 3          # mm3 == uL
    return op.load_cells * one / contracts.param("capsule.packing_fraction")


def load_area_mm2(op: Op) -> Tuple[Optional[float], str]:
    """(area, why) for one op's load. None when its load does not pin an area down."""
    depth = contracts.max_depth_mm()
    basis = op.load_basis

    if basis == "adherent_area":
        if op.load_area_mm2 is not None:
            return op.load_area_mm2, "%g mm2 of floor for an adherent layer" % op.load_area_mm2
        if op.load_cells is not None and op.load_surface_density_per_mm2:
            area = op.load_cells / op.load_surface_density_per_mm2
            return area, ("%g cells at %g per mm2 of floor"
                          % (op.load_cells, op.load_surface_density_per_mm2))
        return None, "adherent_area with neither an area nor a seeding density"

    if basis == "packed_volume":
        volume = _packed_volume_ul(op)
        if volume is not None:
            how = ("%g uL of bed" % volume) if op.load_packed_volume_ul is not None else (
                "%g particles of %g um swollen at packing fraction %g = %.3f uL of bed"
                % (op.load_cells, op.swollen_um,
                   contracts.param("capsule.packing_fraction"), volume))
            return volume / depth, "%s over the %g mm etch depth" % (how, depth)
        return None, "packed_volume with no bed volume and no count"

    if basis == "suspension_volume":
        if op.load_cells is not None and op.load_suspension_density_per_ul:
            volume = op.load_cells / op.load_suspension_density_per_ul
            return volume / depth, ("%g particles at %g per uL = %.3f uL over the %g mm "
                                    "etch depth" % (op.load_cells,
                                                    op.load_suspension_density_per_ul,
                                                    volume, depth))
        return None, "suspension_volume with no count and density"

    if basis == "flow_through":
        return None, ("flow_through holds nothing, so it is a channel rather than a "
                      "chamber%s" % ("; residence %gs is S6's to turn into a length"
                                     % op.load_residence_s
                                     if op.load_residence_s is not None else ""))

    return None, "no load to size from"


def part_area(ops: Sequence[Op]) -> PartArea:
    """Board area one run of ops needs - from `load`, and never from fluid.volume_ul.

    Within a run the area is the MAXIMUM of the ops' loads, not the sum: the ops happen
    one after another in the same place. Washes and other sweeps contribute nothing.
    """
    numbers = tuple(op.number for op in ops)
    floor_side = _particle_floor(ops)
    floor_area = floor_side * floor_side if floor_side else None

    best: Optional[float] = None
    driver = ""
    basis = "none"
    holds_nothing = ""
    for op in ops:
        if op.kind in SWEEP_KINDS:
            continue          # a sweep of a chamber that already exists
        area, why = load_area_mm2(op)
        if area is None:
            if op.has_load and op.load_basis in ("flow_through", "none"):
                holds_nothing = "op %d: %s" % (op.number, why)
            continue
        if best is None or area > best:
            best, driver, basis = area, "op %d: %s" % (op.number, why), op.load_basis

    if best is not None:
        if floor_area is not None and floor_area > best:
            return PartArea(numbers, floor_area, floor_area ** 0.5,
                            "the swollen-particle floor (%g x diameter, "
                            "capsule_channel.width_factor_low) beats the load: %s"
                            % (contracts.param("capsule_channel.width_factor_low"), driver),
                            basis)
        return PartArea(numbers, best, best ** 0.5, driver, basis)

    stated = [op for op in ops if op.has_load]
    if floor_area is not None:
        return PartArea(numbers, floor_area, floor_side,
                        "no holding load here%s, so the swollen-particle floor (%g x "
                        "diameter, capsule_channel.width_factor_low) sets it"
                        % ("" if stated else " and no `load` field at all",
                           contracts.param("capsule_channel.width_factor_low")),
                        ops[0].load_basis if stated else "none",
                        from_floor=not stated)
    if holds_nothing:
        return PartArea(numbers, None, None, holds_nothing,
                        ops[0].load_basis if stated else "none")
    return PartArea(numbers, None, None,
                    "no load and no particle size, so this part cannot be sized here",
                    ops[0].load_basis if stated else "none", from_floor=not stated)


def estimate(ops: Sequence[Op], port_count: int, scale: str) -> AreaEstimate:
    """Board area for these ops at this scale, term by term."""
    from assay2chip.partition.ops import runs_of

    env = contracts.envelope(scale)
    wall = contracts.floor("min_wall_mm")
    pitch = contracts.floor("min_port_pitch_mm")
    channel = contracts.floor("min_open_channel_mm")

    on_chip = [op for op in ops if op.on_chip]
    runs = runs_of(on_chip)
    parts = tuple(part_area(run.ops) for run in runs)

    parts_mm2 = sum(p.area_mm2 for p in parts if p.known)
    walls_mm2 = 0.0
    longest: Optional[float] = None
    for item in parts:
        if not item.known:
            continue
        side = item.side_mm or 0.0
        walls_mm2 += (side + 2 * wall) ** 2 - side * side
        outer = side + 2 * wall
        longest = outer if longest is None else max(longest, outer)

    ports_mm2 = port_count * pitch * pitch
    links = max(0, len(runs) - 1) + port_count
    channels_mm2 = links * pitch * channel

    unknown = tuple(op.number for op in on_chip
                    if not op.has_load and op.kind not in SWEEP_KINDS)

    return AreaEstimate(scale, parts_mm2, walls_mm2, ports_mm2, channels_mm2,
                        env.area_mm2, longest, min(env.x_mm, env.y_mm), unknown, parts)


def smallest_scale(ops: Sequence[Op], port_count: int) -> Tuple[Optional[str], Dict[str, AreaEstimate]]:
    """The cheapest envelope these ops fit, preferring demo. None when even target fails."""
    tried: Dict[str, AreaEstimate] = {}
    for scale in SCALES:
        tried[scale] = estimate(ops, port_count, scale)
        if tried[scale].fits:
            return scale, tried
    return None, tried


def over_ceiling(ops: Sequence[Op], port_count: int) -> Optional[AreaEstimate]:
    """An estimate against the agreed maximum, only to say how far past target it is."""
    got = estimate(ops, port_count, "ceiling")
    return got if not got.fits else None


def replication_rule() -> str:
    """The contract's own answer to 'it does not fit': replicate, do not stretch."""
    rules = contracts.block("target_envelope.design_rules_at_this_scale")
    return next((r for r in rules if "REPLICATE" in r.upper()), rules[0])

"""Unit operations -> one `Brief` per PART: what S6 knows, and the registry `Need`.

`assay2chip/parts/registry.py` owns sizing, and it speaks `assay2chip.parts.geometry.Need`
- volume, residence time, flow, particles, shear class, depth, scale. A Brief is that Need
plus the things only S6 cares about: which blocks are worth trying, whether a reagent has
to be delivered or liquid drained, which ops hold a temperature (so each gets a thermal
zone), and the gaps that had to be filled to get this far.

THE SIZE COMES FROM `load`, NEVER FROM `fluid.volume_ul`
--------------------------------------------------------
`assay2chip/README.md`, "Bench volumes do not transfer": a paper's "lyse in 200 uL" or
"wash the pellet in 500 uL" describes a TUBE. Sizing a chamber from it is what made the
first end-to-end run ask for 500 uL chambers and then refuse 0.012 uL steps. So this
module reads `op.load` and nothing else for size:

    adherent_area       floor area (stated, or cells / surface density) x the etch depth
    packed_volume       the bed volume (stated, or count x particle volume / packing)
    suspension_volume   cells / suspension density
    flow_through        nothing is held: `residence_s` goes to the registry instead and
                        the part is sized from flow and residence time
    none / no load      a flagged contract default, with an `unresolved` question

`fluid.volume_ul` is carried into `volume_source` and into `audit` as the paper's number
and is never used as a dimension. A wash's reagent volume becomes a SWEEP of the chamber
at protocol time (S8), which is why it does not appear here at all.

A load below the smallest buildable chamber is ROUNDED UP, with a note, and NEVER refused
(README: "a volume below the smallest buildable chamber -> rounded UP"). The registry
rounds up too; this clamps first so a re-run request can never be caused by smallness.

ONE PART PER PLACE THE POPULATION SITS
--------------------------------------
`chip/designs/README.md` rule 1: "Steps that happen to the same cells in the same spot
become one chamber". A lysis, then three washes, then a reverse-transcription hold, all on
the same capsules in the same trap, is ONE `trap_chamber` with one weir and one bed volume
- not four chambers with three channels between them that would move the bed back and
forth. `group_ops()` does that merge; `brief_for_group()` unions the resulting demands
(the largest load, the most restrictive shear ceiling, one thermal zone per holding op) and
`Brief.ops` keeps every op number so the chip can still be traced back to the paper.

Two rules this module also enforces:

  1. **No invented number.** A value the source never gave is derived from a contract and
     recorded in `Brief.gaps`, which S6 copies into the chip's `unresolved`. A derived
     number is flagged; it never reads as a measurement.
  2. **Candidate blocks, not one guess.** Each kind has an ordered list of library blocks,
     best first; `parts.py` tries them and keeps the registry's REASON for every refusal.
     The ordering encodes real preferences: a shear-sensitive merge gets
     `curved_y_junction` before the sharp `y_junction`, a capsule wash gets `trap_chamber`,
     a long hold on cells gets `culture_chamber`.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field, fields as dataclass_fields, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402
from assay2chip.parts.geometry import Load, Need  # noqa: E402

# Ops where liquid is exchanged while the population stays behind.
EXCHANGING_KINDS = frozenset({"wash", "separate", "lyse"})
# Ops that drain spent liquid to waste.
DRAINING_KINDS = frozenset({"wash", "separate", "lyse", "collect", "culture"})
# Ops that need a reagent delivered to them.
REAGENT_KINDS = frozenset({"wash", "lyse", "mix", "merge", "separate", "cure",
                           "thermocycle", "culture"})
# Ops that hold a temperature, so they get a thermal zone.
HOLDING_KINDS = frozenset({"incubate", "culture", "thermocycle", "lyse", "cure", "mix",
                           "wash"})
# Ops that MIX two streams, which is not the same as joining them. S2's own grouping rule
# is "a reagent added to a flowing stream is a merge part (t_junction, y_junction), THEN a
# mixer": a Y joins, a herringbone mixes. The registry refuses `mix=True` on a junction for
# exactly that reason (Pe is enormous and Re about 1e-2, so side-by-side streams mix only
# by diffusion), so `merge` must not claim to mix.
MIXING_KINDS = frozenset({"mix"})

# CONDUITS, not vessels. These ops do not HOLD a population: they carry a stream while
# something happens to it (two streams join, they mix, a population moves). They are sized
# from flow and residence time, never from a volume, and when the flow and the time are
# too small to need a part of their own they become the CONNECTING CHANNEL instead - see
# Brief.inline. Rounding a conduit up to the smallest buildable AREA is what produced a
# 0.38 x 0.20 mm "mixer": a part too small for a channel to enter, which is no part at all.
CONDUIT_KINDS = frozenset({"mix", "merge", "transfer"})

# thermal_zones[].purpose. 'culture' is special: tools/check_chip_spec.py uses it to
# require the chamber be tall enough for a SWOLLEN capsule, so it is set only for a real
# culture hold and never as a generic label for "37 C".
PURPOSE_BY_KIND: dict[str, str] = {
    "load": "load", "culture": "culture", "transfer": "transfer",
    "incubate": "incubation", "thermocycle": "pcr", "lyse": "lysis", "cure": "gelation",
    "wash": "wash", "mix": "mix", "merge": "merge", "separate": "separation",
    "collect": "collection", "split": "split", "pool": "pool", "image": "imaging",
    "encapsulate": "encapsulation",
}

# A hold at culture temperature this long is culture, which is a different block AND
# triggers the swollen-capsule height rule. One day, in seconds.
CULTURE_MIN_DURATION_S = 24 * 3600

# Ops that happen IN PLACE: the population stays put and something is done to it. These
# are the ones chip/designs/README.md rule 1 merges into a single chamber. Everything else
# (merge, mix, encapsulate, cure, split, pool, image) is its own place in the flow.
IN_PLACE_KINDS = frozenset({"load", "wash", "lyse", "incubate", "culture", "separate",
                            "collect", "thermocycle"})

# Most restrictive wall-shear ceiling first: a group of merged ops takes the tightest one
# any of its members had, because the part has to satisfy all of them.
SHEAR_PRECEDENCE = ("adherent_cells", "prelysis_rinse", "capsules", "none")


@dataclass(frozen=True)
class Brief:
    """What S6 knows about one on-chip op, and the Need the registry sizes from."""

    op: int                          # the FIRST op in the group; names the part
    kind: str                        # that op's kind
    ops: tuple[int, ...]             # every op this one part performs, in order
    kinds: tuple[str, ...]           # their kinds, in the same order
    from_steps: tuple[int, ...]
    part: str | None                 # the catalogue part name S3 chose, for provenance
    blocks: tuple[str, ...]          # candidates, best first
    load: Load                       # handed straight to the registry, which resolves it
    volume_ul: float | None          # a local ESTIMATE of that load, for comparison only
    volume_source: str
    load_basis: str
    residence_s: float | None        # for flow_through: time the stream spends in the part
    flow_ul_min: float | None        # derived only where a part cannot be sized without it
    duration_s: float | None
    temp_c: float | None
    temp_holds: tuple[dict, ...]     # one entry per temperature-holding op in the group
    viscosity_class: str
    particles: str
    particle_um: float | None
    swollen_um: float | None
    particle_count: int | None
    shear_class: str
    scale: str
    mix: bool
    branches: int | None
    retains: bool                    # liquid leaves, the population stays
    needs_reagent: bool
    drains: bool
    holds_temp: bool
    purpose: str
    route_width_mm: float            # narrowest channel allowed to serve this part
    paper_volume_ul: float | None = None   # fluid.volume_ul: traceability ONLY
    inline: bool = False             # realise as the connecting channel, not as a part
    inline_why: str = ""
    target_length_mm: float | None = None   # how long that channel has to be
    gaps: tuple[dict, ...] = field(default_factory=tuple)
    note: str = ""

    @property
    def part_id(self) -> str:
        """The chip-spec id prefix for this part: `wash_3` for op 3 of kind wash.

        A merged group is named after its FIRST op, and `ops` lists the rest, so one
        chamber doing lysis, washes and a hold is still traceable to all three.
        """
        return f"{self.kind}_{self.op}"

    @property
    def merged(self) -> bool:
        return len(self.ops) > 1

    @property
    def conduit(self) -> bool:
        """True when this part carries a stream rather than holding a population.

        A conduit has no volume of its own to be sized from, so it is sized from flow and
        residence time - and if that comes to less than a part a channel could even enter,
        it is realised as the channel itself (`inline`).
        """
        return self.kind in CONDUIT_KINDS or self.load_basis == "flow_through"

    def residence_length_mm(self, width_mm: float, depth_mm: float) -> float | None:
        """Channel length that holds this op's stream for its residence time.

        L = Q t / (w h): the volume a flow Q occupies in t seconds, divided by the
        cross-section. contracts/blocks.json target_envelope lists residence time under
        `not_scale_free` for exactly this reason - it does not survive a change of scale,
        it has to be built.
        """
        if not (self.residence_s and self.flow_ul_min and width_mm > 0 and depth_mm > 0):
            return None
        volume_mm3 = float(self.flow_ul_min) * float(self.residence_s) / 60.0
        return round(volume_mm3 / (width_mm * depth_mm), 3)

    @property
    def weir_retained(self) -> bool:
        """True when a DEPTH WEIR can really hold this population back.

        Capsules only: `contracts/blocks.json retention._cells_caveat` says so plainly,
        and a 15 um cell would need a gap of about 5 um, which is a quarter of the
        process's own z tolerance.
        """
        return self.retains and self.particles == "capsules"

    @property
    def need(self) -> Need:
        """The registry's Need. `label` becomes the prefix of every id the part expands to."""
        return Need(
            # The registry resolves `load` and rounds it up if it is below the smallest
            # buildable part. volume_ul is passed ONLY when there is no load for it to
            # resolve, which is the basis 'none' case.
            volume_ul=self.volume_ul if self.load.basis == "none" else None,
            load=self.load,
            residence_s=self.residence_s,
            flow_ul_min=self.flow_ul_min,
            particles=self.particles,
            particle_um=self.particle_um,
            shear_class=self.shear_class,
            depth_mm=None,             # the registry uses the contract's one etch depth
            temp_c=self.temp_c,
            scale=self.scale,
            mix=self.mix,
            branches=self.branches,
            label=self.part_id,
            note=(f"op{'s' if self.merged else ''} "
                  f"{', '.join(str(o) for o in self.ops)} "
                  f"({', '.join(self.kinds)}); size from {self.volume_source}"),
        )

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}   # noqa: SLF001


def candidate_blocks(op: dict, *, particles: str, culture: bool, branches: int | None,
                     shear_sensitive: bool, kinds: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Library blocks that could perform this part's op(s), best first.

    S3's `placement.blocks` go first when they name something in the library: S3 read the
    sentence and this module did not. Everything else is a stated preference, and the
    registry gets the final word on whether any of them can actually be built.

    `kinds` is every kind the part performs, because a merged part has to satisfy all of
    them: one wash anywhere in the group means the chamber needs a retention crest, and
    the block is chosen for the whole group rather than for its first op.
    """
    kind = op.get("kind", "")
    kinds = kinds or (kind,)
    lib = contracts.library()
    # `port` is the chip's lid hole and `text` is an engraved label: ports.py plans the
    # ports and neither is a place in the flow, so neither may be a PART - even when S3
    # suggests one. A `load` op hinted as `port` would put a second hole in the lid.
    not_a_part = {"port", "text"}
    hints = [b for b in ((op.get("placement") or {}).get("blocks") or [])
             if b in lib and b not in not_a_part]
    # A retaining block is offered for CAPSULES ONLY. blocks.json retention._cells_caveat:
    # the depth weir holds a 70 um capsule and explicitly does not hold a 10-15 um cell,
    # and a SPRI bead is about 1 um. Offering trap_chamber for cells would get a weir gap
    # of about 5 um, which the z tolerance alone makes meaningless.
    retains = any(k in EXCHANGING_KINDS for k in kinds) and particles == "capsules"
    table: dict[str, list[str]] = {
        # Where the population lands when it arrives, and where it leaves from. A `load`
        # is a PLACE, not the inlet: ports.py plans the chip's ports, and a part that was
        # itself a port would be a second hole in the lid.
        "load": (["trap_chamber"] if retains else []) + ["polygon_chamber"],
        # A transfer has no vessel of its own: moving a population from A to B IS the
        # channel between them. A `straight_channel` part would be a channel S7 cannot
        # route to, so the chamber is the only candidate and a transfer with nothing to
        # hold ends up inline.
        "transfer": ["polygon_chamber"],
        "culture": ["culture_chamber", "polygon_chamber"],
        "merge": ["curved_y_junction", "y_junction"] if shear_sensitive else ["y_junction"],
        "mix": ["herringbone_mixer", "asymmetric_serpentine", "snake_channel", "polygon_chamber"],
        "incubate": (["culture_chamber"] if culture else []) + ["polygon_chamber"],
        "wash": (["trap_chamber"] if retains else []) + ["polygon_chamber"],
        "separate": (["trap_chamber", "dld_array"] if retains else []) + ["polygon_chamber"],
        "lyse": (["trap_chamber"] if retains else []) + ["polygon_chamber"],
        "thermocycle": ["polygon_chamber"],
        "split": (["flow_divider", "flow_manifold"] if branches else []) + ["polygon_chamber"],
        "pool": ["ring_junction", "polygon_chamber"],
        "collect": ["polygon_chamber"],
        "image": ["polygon_chamber"],
        "encapsulate": ["flow_focusing", "flow_focusing_funnel", "cross_junction"],
        "cure": ["polygon_chamber"],
    }
    # A merged part takes the union of its ops' candidates, in group order, so a lysis +
    # wash + hold group is offered the trap first and a plain chamber as the fallback.
    wanted: list[str] = []
    for k in kinds:
        for block in table.get(k, ["polygon_chamber"]):
            if block not in wanted:
                wanted.append(block)
    chosen = hints + [b for b in wanted if b not in hints and b not in not_a_part]
    missing = [b for b in chosen if b not in lib]
    if missing:
        raise contracts.ContractError(
            f"block(s) {', '.join(missing)} for a '{kind}' op are not in the CAD block "
            f"library (contracts/blocks.json basic/advanced). The contract renamed them, or "
            f"this op needs a missing_part placement from S3 - do not invent a block.")
    return tuple(chosen)


def bed_volume_ul(count: float, diameter_um: float, packing_fraction: float) -> float:
    """Volume a packed bed of `count` spheres occupies, in microlitres (1 mm^3 = 1 uL)."""
    one_mm3 = math.pi / 6.0 * (diameter_um / 1000.0) ** 3
    return count * one_mm3 / packing_fraction


def _gap(op: int, field_name: str, question: str, assumed: Any, assumed_from: str) -> dict:
    return {"op": op, "field": field_name, "question": question, "assumed": assumed,
            "assumed_from": assumed_from}


# --- the load: what the place must HOLD -------------------------------------------
#
# S6 DOES NOT SIZE. `assay2chip/parts/registry.py` resolves all four load bases, applies
# the contract defaults for anything the paper left out, and ROUNDS UP any need below the
# smallest buildable part (reporting `volume_requested_ul` and `volume_rounded_up`). So
# this module's whole job is to hand it the right `load` and get out of the way. There is
# deliberately NO minimum-size filter here: a 1.3 uL or a 0.012 uL load goes straight
# through, because a part S6 refused for being small is a part the registry would have
# rounded up and built.
#
# The one number computed locally is an ESTIMATE, used only to compare two ops' loads
# (which of a merged group is the biggest) and to derive a mean flow. It never becomes a
# dimension; `estimate_ul` and the registry's answer can differ and the registry wins.


def bed_volume_ul(count: float, diameter_um: float, packing_fraction: float) -> float:
    """Volume a packed bed of `count` spheres occupies, in microlitres (1 mm^3 = 1 uL)."""
    one_mm3 = math.pi / 6.0 * (diameter_um / 1000.0) ** 3
    return count * one_mm3 / packing_fraction


def adherent_area_mm2(cells: float, surface_density_per_mm2: float) -> float:
    """Floor area a monolayer of `cells` needs at the paper's seeding density."""
    return cells / surface_density_per_mm2


def floor_volume_ul() -> float:
    """The smallest chamber the fabrication floors allow, for reporting only.

    NOT a filter. The registry rounds a smaller load up to its own minimum and says it
    did; this is here so a report can state what that minimum is.
    """
    return float(contracts.floor("min_open_channel_mm")) ** 2 * contracts.max_depth_mm()


def _bed_diameter_um(particles: dict) -> float:
    """The diameter the particles have AT THIS OP, which is what sets the bed volume.

    S2 already reports `particles.diameter_um` as the swollen figure from the first
    culture op onward (its `derived.capsule_swollen` flag), so this takes it as given and
    does not swell it again - a bed sized at 2 x the diameter is 8 x the volume, and
    doing that twice is how a 5 uL bed becomes a 45 uL one.
    """
    return float(particles.get("diameter_um") or contracts.capsule_diameter_um())


def _passing_diameter_um(particles: dict) -> float:
    """The LARGEST diameter a channel has to pass: swollen if the op says it swells.

    Different question from the bed volume, and deliberately the pessimistic answer: a
    channel sized for an unswollen capsule blocks the moment one swells in it.
    """
    return max(_bed_diameter_um(particles),
               float(particles.get("swollen_diameter_um") or 0.0))


def sizing_diameter_um(particles: dict, number: int, gaps: list[dict]) -> float | None:
    """The particle diameter the registry sizes against, or None when nothing is carried.

    THE CONTRACTS HAVE NO CELL DIAMETER AND NO BEAD DIAMETER. S2 leaves
    `particles.diameter_um` null when the paper never said, which is correct, and the
    registry will not invent one. But a cell whose size nobody stated still has to be
    carried, and the contract does state what a channel carrying single cells and small
    clumps must pass: `dissociation.outlet_min_width_um`. So that is the STAND-IN, with a
    gap recorded, rather than stopping the build. It is only ever "what must get through":
    no chamber volume is computed from it.
    """
    kind = str(particles.get("kind", "none"))
    if kind == "none":
        return None
    if particles.get("diameter_um"):
        # The OPERATIVE diameter, not the swollen one. The registry uses this for the weir
        # gap as well as for widths, and a weir derived from a swollen capsule leaves a gap
        # about twice as large, which the unswollen capsules then walk straight through.
        # The pessimistic swollen figure belongs to channel widths; route_width_mm has it.
        return _bed_diameter_um(particles)
    if kind == "capsules":
        value = contracts.capsule_diameter_um()
        gaps.append(_gap(
            number, "particles.diameter_um",
            f"op {number} carries capsules and the source never gave a diameter, so the "
            f"contract's capsule diameter was used for every width this part needs",
            value, "contracts/parameters.json capsule.diameter_um"))
        return value
    value = float(contracts.param("dissociation.outlet_min_width_um"))
    gaps.append(_gap(
        number, "particles.diameter_um",
        f"op {number} carries {kind} and NEITHER the source NOR the contracts give a "
        f"{kind[:-1]} diameter, so sizing used the contract's own minimum width for a "
        f"channel carrying single cells and small clumps as a stand-in for what has to get "
        f"through; it sets channel and port widths only, never a chamber volume",
        value, "contracts/parameters.json dissociation.outlet_min_width_um"))
    return value


@dataclass(frozen=True)
class ChosenLoad:
    """One op's `load`, ready for the registry, plus a comparison estimate."""

    load: Load
    estimate_ul: float | None        # for comparing loads and deriving a flow ONLY
    why: str
    gaps: tuple[dict, ...] = field(default_factory=tuple)


# What each basis needs before the registry can resolve it. It fills in the contract
# defaults for a seeding density and a particle diameter, but it has NO default for a
# suspension density and none for a population it was told nothing about, and it refuses
# rather than inventing them - correctly. These are those preconditions, stated once.
def _resolvable(load: Load) -> bool:
    if load.basis == "adherent_area":
        return bool(load.area_mm2 or load.cells)
    if load.basis == "packed_volume":
        return bool(load.packed_volume_ul or load.cells)
    if load.basis == "suspension_volume":
        return bool(load.cells and load.suspension_density_per_ul)
    if load.basis == "flow_through":
        return bool(load.residence_s)
    return False


def _downgrade(load: Load, particles: dict, number: int,
               gaps: list[dict]) -> Load:
    """Swap an unresolvable basis for one the registry CAN resolve, loudly.

    A suspension with no density is the case that matters: the contracts have no
    suspension density, so the registry refuses, and refusing would stop the run over a
    number nobody has. The same population as a PACKED bed is contract-derived (packing
    fraction and diameter) and is a LOWER BOUND on the volume a suspension of it needs, so
    the part is built to that and the gap says plainly that it is a lower bound.
    """
    kind = str(particles.get("kind", "none"))
    if load.basis == "suspension_volume" and load.cells:
        instead = "packed_volume" if kind in ("capsules", "beads") else "adherent_area"
        gaps.append(_gap(
            number, "load.suspension_density_per_ul",
            f"op {number} holds a suspension and neither the source nor the contracts give "
            f"a suspension density, so the part was sized on the '{instead}' basis for the "
            f"same {load.cells:,.0f} {kind} instead"
            + (" - a LOWER BOUND on the volume a suspension of them needs"
               if instead == "packed_volume" else ""),
            instead, "contracts/parameters.json has no suspension density for "
                     f"{kind}; basis swapped so the registry can resolve it"))
        return replace(load, basis=instead, source="derived",
                       note=(load.note + f" basis swapped from suspension_volume to "
                                         f"{instead}: no suspension density exists").strip())
    if load.basis == "flow_through" and not load.residence_s:
        # Handled by brief_for_group, which marks the whole part `inline`. Leaving the
        # basis alone keeps the reason visible instead of turning it into a floor-sized
        # chamber nobody asked for.
        # Nothing is held and no residence time was stated, so there is NOTHING to size
        # from. The part becomes the smallest one the floors allow. It must NOT become a
        # bed of the contract's whole population: a mixer in flow does not hold 100,000
        # capsules, and sizing it as though it did asks for a 28 uL, 95 mm2 part that
        # fills a quarter of the demo board for a step that holds nothing.
        gaps.append(_gap(
            number, "load.residence_s",
            f"op {number} only carries a stream and the source gave neither a residence "
            f"time nor a duration, so there is nothing to size a part from; it is realised "
            f"as the connecting channel instead of becoming a part of its own, and no "
            f"residence time is guaranteed",
            None,
            "contracts/blocks.json fabrication_floors.min_open_channel_mm + envelope.max_depth_mm"))
        return replace(load, source="derived",
                       note=(load.note + " no residence time: this op becomes the "
                                         "connecting channel, not a part").strip())
    return load


def _estimate_ul(load: Load, particles: dict) -> tuple[float | None, str]:
    """A local estimate of what `load` resolves to, and how it reads. NOT a dimension.

    Returns None when the load cannot be worked out here - a flow-through part holds
    nothing, and a load missing its count or density is for the registry's contract
    defaults to fill in, not for this module to guess at.
    """
    depth = contracts.max_depth_mm()
    kind = str(particles.get("kind", "none"))
    if load.basis == "adherent_area":
        if load.area_mm2:
            return (float(load.area_mm2) * depth,
                    f"a stated floor area of {float(load.area_mm2):g} mm2 x the "
                    f"{depth:g} mm etch depth")
        if load.cells and load.surface_density_per_mm2:
            area = adherent_area_mm2(float(load.cells), float(load.surface_density_per_mm2))
            return (area * depth,
                    f"{float(load.cells):,.0f} adherent cells at "
                    f"{float(load.surface_density_per_mm2):g} cells/mm2 = {area:.3g} mm2 of "
                    f"floor x the {depth:g} mm etch depth")
        return None, "a floor area the registry fills in from the contract seeding density"
    if load.basis == "packed_volume":
        if load.packed_volume_ul:
            return (float(load.packed_volume_ul),
                    f"a stated packed bed of {float(load.packed_volume_ul):g} uL")
        if load.cells:
            diameter = _bed_diameter_um(particles)
            packing = float(contracts.param("capsule.packing_fraction"))
            volume = bed_volume_ul(float(load.cells), diameter, packing)
            return (volume,
                    f"a packed bed of {float(load.cells):,.0f} x {diameter:g} um "
                    f"{kind if kind != 'none' else 'particles'} at packing fraction "
                    f"{packing:g} = {volume:.3g} uL")
        return None, "a packed bed the registry fills in from the contract population"
    if load.basis == "suspension_volume":
        if load.cells and load.suspension_density_per_ul:
            volume = float(load.cells) / float(load.suspension_density_per_ul)
            return (volume,
                    f"{float(load.cells):,.0f} {kind} at "
                    f"{float(load.suspension_density_per_ul):g} per uL = {volume:.3g} uL "
                    f"of suspension")
        return None, "a suspension the registry fills in from the contract density"
    if load.basis == "flow_through":
        if load.residence_s:
            return (None, f"nothing is held: a stream with "
                          f"{float(load.residence_s):g} s of residence")
        return None, "nothing is held and no residence time was stated"
    return (floor_volume_ul(),
            "the smallest part the fabrication floors allow, because nothing is held and "
            "nothing was stated to size it from")


def load_for_op(op: dict) -> ChosenLoad:
    """One op's `load`, as the registry's own type. NEVER reads `fluid.volume_ul`.

    `fluid.volume_ul` is the paper's TUBE volume: it travels in `Brief.paper_volume_ul`
    and in the wording, and it is never a dimension. See assay2chip/README.md, "Bench
    volumes do not transfer".
    """
    number = int(op.get("op", 0))
    particles = op.get("particles") or {"kind": "none"}
    raw = op.get("load")
    gaps: list[dict] = []
    paper = (op.get("fluid") or {}).get("volume_ul")

    if not raw:
        # S2 has not filled `load` for this op. Hand the registry the basis its population
        # implies and let it apply the contract defaults - it owns those, and it says which
        # it used. Never fall back to the paper's volume.
        kind = str(particles.get("kind", "none"))
        basis = ("packed_volume" if kind in ("capsules", "beads") else
                 "adherent_area" if kind == "cells" else "none")
        load = Load(basis=basis,
                    cells=float(particles.get("count")
                                or contracts.param("capsule.count")),
                    source="contract_default",
                    note=f"op {number} carried no `load`; basis inferred from "
                         f"particles.kind = {kind}")
        gaps.append(_gap(
            number, "load",
            f"op {number} has no `load`, so its part was sized on the '{basis}' basis its "
            f"population implies and the registry filled in the contract defaults."
            + (f" The paper's {float(paper):g} uL is a TUBE volume and was deliberately NOT "
               f"used (assay2chip/README.md, 'Bench volumes do not transfer')." if paper else ""),
            basis, "contracts/schemas/unitops.schema.json load.basis + the parts registry"))
        estimate, why = _estimate_ul(load, particles)
        return ChosenLoad(load, estimate, f"no load was recorded, so {why}", tuple(gaps))

    fields = {f.name for f in dataclass_fields(Load)}
    load = Load(**{k: v for k, v in raw.items() if k in fields})
    if load.basis == "flow_through" and not load.residence_s and op.get("duration_s"):
        load = replace(load, residence_s=float(op["duration_s"]),
                       note=(load.note + " residence taken from the op's duration").strip())
    if not load.cells and particles.get("count"):
        load = replace(load, cells=float(particles["count"]))
    if not _resolvable(load):
        load = _downgrade(load, particles, number, gaps)
        # Only a basis the OP itself named gets the contract population; a basis this
        # module had to swap in does not, because then nobody has said how much is there.
        if (not load.cells and load.basis in ("adherent_area", "packed_volume")
                and load.basis == str(raw.get("basis"))):
            load = replace(load, cells=float(contracts.param("capsule.count")),
                           source="contract_default")
            gaps.append(_gap(
                number, "load.cells",
                f"op {number}'s load names no population, so the contract's population "
                f"size was used",
                float(contracts.param("capsule.count")),
                "contracts/parameters.json capsule.count"))
    estimate, why = _estimate_ul(load, particles)
    if load.source == "contract_default":
        gaps.append(_gap(
            number, "load",
            f"op {number}'s load is a contract default rather than the paper's own number: "
            f"{load.note or why}",
            estimate and round(estimate, 4), "unitops.json load.source = contract_default"))
    return ChosenLoad(load, estimate, why, tuple(gaps))


# --- grouping: one part per place the population sits -----------------------------

def _population(op: dict) -> tuple:
    """What identifies the population an op acts on. Two ops with the same key, both in
    place, are the same cells in the same spot - chip/designs/README.md rule 1."""
    particles = op.get("particles") or {"kind": "none"}
    return (str(particles.get("kind", "none")),
            particles.get("diameter_um"),
            particles.get("swollen_diameter_um"))


def _groupable(a: dict, b: dict) -> bool:
    if a.get("kind") not in IN_PLACE_KINDS or b.get("kind") not in IN_PLACE_KINDS:
        return False
    if _population(a) != _population(b):
        return False
    # A step with nothing to hold is not the same place as one that holds a population.
    if _population(a)[0] == "none":
        return False
    basis_a = (a.get("load") or {}).get("basis")
    basis_b = (b.get("load") or {}).get("basis")
    if basis_a and basis_b and basis_a != basis_b:
        return False
    return "flow_through" not in (basis_a, basis_b)


def group_ops(ops: list[dict]) -> list[list[dict]]:
    """Consecutive ops that happen to the SAME population in the SAME place, grouped.

    chip/designs/README.md rule 1: "One module per place the cells or capsules sit. Steps
    that happen to the same cells in the same spot become one chamber: culture +
    dissociation; oil removal + curing + capsule wash in one trap." So a lysis, three
    washes and a reverse-transcription hold on one capsule bed become ONE trap_chamber.
    Anything in flow - a merge, a mixer, a nozzle, a split - is its own place and is never
    merged into a neighbour.
    """
    groups: list[list[dict]] = []
    for op in ops:
        if groups and _groupable(groups[-1][-1], op):
            groups[-1].append(op)
        else:
            groups.append([op])
    return groups


def brief_for_group(group: list[dict], *, scale: str = "demo") -> Brief:
    """One Brief for one part, from the one or more ops it performs."""
    if not group:
        raise ValueError("a part needs at least one op")
    head = group[0]
    number = int(head.get("op", 0))
    kinds = tuple(str(o.get("kind", "")) for o in group)
    numbers = tuple(int(o.get("op", 0)) for o in group)
    particles = head.get("particles") or {"kind": "none"}
    placement = head.get("placement") or {}
    p_kind = str(particles.get("kind", "none"))
    p_um = particles.get("diameter_um")
    gaps: list[dict] = []

    depth_mm = contracts.max_depth_mm()
    min_feature = float(contracts.floor("min_open_channel_mm"))

    # --- the load: the largest any member op demands, from `load` only ------------
    # The part gets ONE load - the biggest of the group's, because the place has to hold
    # the largest thing that ever sits in it - and the registry resolves it. There is NO
    # minimum-size check here: the registry rounds a small load up to its own minimum and
    # reports volume_requested_ul / volume_rounded_up, which spec.py turns into an
    # `unresolved` entry. A part S6 refused for being small is a part that would have been
    # built.
    chosen = [load_for_op(o) for o in group]
    for item in chosen:
        gaps.extend(item.gaps)
    sized = [c for c in chosen if c.estimate_ul is not None]
    biggest = max(sized, key=lambda c: c.estimate_ul) if sized else chosen[0]
    load = biggest.load
    estimate = biggest.estimate_ul
    residence = max((c.load.residence_s for c in chosen if c.load.residence_s is not None),
                    default=None)
    basis = load.basis
    source = biggest.why
    if len(group) > 1 and sized:
        at = numbers[chosen.index(biggest)]
        source = (f"{source} (the largest load of op(s) "
                  f"{', '.join(str(n) for n in numbers)}: op {at})")

    # --- unions across the group -------------------------------------------------
    retains = any(k in EXCHANGING_KINDS for k in kinds) and p_kind != "none"
    if retains and p_kind in ("cells", "beads"):
        instead = ("a bought mesh insert or sedimentation" if p_kind == "cells"
                   else "an external magnet (SPRI beads are about 1 um)")
        gaps.append(_gap(
            number, "retention",
            f"op(s) {', '.join(str(n) for n in numbers)} hold {p_kind} back while liquid "
            f"leaves, but the depth weir in contracts/blocks.json retains 70 um capsules "
            f"and explicitly does not retain {p_kind}; retention here needs {instead}, "
            f"which is not a block we place",
            None,
            "contracts/blocks.json retention._cells_caveat / retention.alternatives_ranked"))

    culture = any(o.get("kind") == "incubate" and p_kind in ("cells", "capsules")
                  and (o.get("duration_s") or 0) >= CULTURE_MIN_DURATION_S for o in group)
    duration = sum(float(o.get("duration_s") or 0) for o in group) or None
    temps = [float(o["temp_c"]) for o in group if o.get("temp_c") is not None]
    temp_holds = tuple(
        {"op": int(o.get("op", 0)), "kind": str(o.get("kind", "")),
         "temp_c": float(o["temp_c"]),
         "purpose": "culture" if (culture and o.get("kind") == "incubate")
                    else PURPOSE_BY_KIND.get(str(o.get("kind", "")), str(o.get("kind", "")))}
        for o in group
        if o.get("temp_c") is not None and str(o.get("kind", "")) in HOLDING_KINDS)
    shear_class = next((s for s in SHEAR_PRECEDENCE
                        if any(str(o.get("shear_class", "none")) == s for o in group)), "none")
    viscosities = [str((o.get("fluid") or {}).get("viscosity_class", "aqueous")) for o in group]
    v_class = next((v for v in ("oil", "organic", "viscous") if v in viscosities), "aqueous")
    if v_class != "aqueous":
        visc = contracts.viscosity_for(temps[0] if temps else None)
        gaps.append(_gap(
            number, "fluid.viscosity_class",
            f"op(s) {', '.join(str(n) for n in numbers)} are '{v_class}' and the contracts "
            f"give no viscosity for a {v_class} phase, so every pressure and shear number "
            f"for this part uses the aqueous value",
            visc.pa_s, f"contracts/parameters.json {visc.key}"))

    branches = placement.get("branches") or (head.get("derived") or {}).get("branches")
    if head.get("kind") in ("split", "pool") and not branches:
        gaps.append(_gap(
            number, "branches",
            f"op {number} is a {head.get('kind')} and nothing says into or from how many "
            f"ways, so no divider or manifold could be sized; it became a single chamber "
            f"that an off-chip rotary valve fills one at a time, which is how "
            f"chip/module4_v1.json does the same job",
            None, "unitops.json gives no branch count"))

    # --- the flow the registry needs, where it needs one -------------------------
    flow_ul_min: float | None = None
    ceiling = contracts.shear_ceiling_for(shear_class)
    mixes = any(k in MIXING_KINDS for k in kinds)
    if (ceiling.ceiling_dyn_cm2 is not None or mixes) and estimate is not None:
        why_needed = (f"the {ceiling.shear_class} wall-shear ceiling "
                      f"({ceiling.ceiling_dyn_cm2:g} dyn/cm2) sets this part's width and "
                      f"cannot be checked without a flow rate"
                      if ceiling.ceiling_dyn_cm2 is not None else
                      "a mixer's length is set by the Peclet number, so it cannot be sized "
                      "without a flow rate")
        if duration:
            flow_ul_min = float(estimate) / (float(duration) / 60.0)
            gaps.append(_gap(
                number, "flow_ul_min",
                f"op(s) {', '.join(str(n) for n in numbers)}: {why_needed}, and unitops.json "
                f"has no flow field; the MEAN flow was derived as the chamber volume over "
                f"the duration, so an instantaneous rate above this mean would exceed the "
                f"ceiling",
                round(flow_ul_min, 4),
                "the load-derived chamber volume / unitops.json duration_s"))
        else:
            gaps.append(_gap(
                number, "flow_ul_min",
                f"op(s) {', '.join(str(n) for n in numbers)}: {why_needed}, and the source "
                f"gave neither a flow rate nor a duration to derive one from",
                None, "unitops.json has no flow_ul_min and no duration_s"))

    sizing_um = sizing_diameter_um(particles, number, gaps)
    route_w = min_feature
    if p_kind in ("capsules", "beads") and sizing_um:
        factor = float(contracts.param("capsule_channel.width_factor_low"))
        # Channels take the PESSIMISTIC diameter: one that swells inside a channel sized
        # for its unswollen self blocks the chip.
        route_w = max(route_w,
                      factor * max(sizing_um, _passing_diameter_um(particles)) / 1000.0)
    elif p_kind == "cells":
        route_w = max(route_w,
                      float(contracts.param("dissociation.outlet_min_width_um")) / 1000.0)

    if len(group) > 1:
        gaps.append(_gap(
            number, "part",
            f"ops {', '.join(f'{n} ({k})' for n, k in zip(numbers, kinds))} all act on the "
            f"same {p_kind} in the same place, so they share ONE part rather than one "
            f"chamber each (chip/designs/README.md rule 1); the chamber is sized for the "
            f"largest of their loads and is held at each op's temperature in turn",
            f"one {p_kind} chamber", "chip/designs/README.md rule 1"))

    # A conduit with no residence time to build to is not a part: it is the channel
    # between its neighbours. Saying so here, rather than rounding it up to the smallest
    # buildable area, is what stops S6 emitting a 0.38 x 0.20 mm "mixer" that no channel
    # can enter. S6 attaches it to the connection that spans it, with the length it needs.
    conduit = kinds[0] in CONDUIT_KINDS or basis == "flow_through"
    inline = conduit and not residence
    inline_why = ""
    if inline:
        inline_why = (f"op(s) {', '.join(str(n) for n in numbers)} "
                      f"({', '.join(kinds)}) carry a stream and nothing states a residence "
                      f"time, so there is no length or volume to build to; realised as the "
                      f"connecting channel rather than as a part")
        gaps.append(_gap(
            number, "part",
            inline_why + " (a part sized from the fabrication floor instead would be too "
            "small for a channel to enter, which is no part at all)",
            "the connecting channel", "assay2chip/layout/needs.py Brief.inline"))

    shear_sensitive = p_kind in ("cells", "capsules") or shear_class != "none"
    paper = next((float(o["fluid"]["volume_ul"]) for o in group
                  if (o.get("fluid") or {}).get("volume_ul")), None)
    return Brief(
        op=number,
        kind=str(head.get("kind", "")),
        ops=numbers,
        kinds=kinds,
        from_steps=tuple(n for o in group for n in (o.get("from_steps") or ())),
        part=placement.get("part"),
        blocks=candidate_blocks(head, particles=p_kind, culture=culture,
                                branches=int(branches) if branches else None,
                                shear_sensitive=bool(shear_sensitive), kinds=kinds),
        load=load,
        volume_ul=float(estimate) if estimate is not None else None,
        volume_source=source,
        load_basis=basis,
        residence_s=residence,
        flow_ul_min=flow_ul_min,
        duration_s=duration,
        temp_c=max(temps) if temps else None,
        temp_holds=temp_holds,
        viscosity_class=v_class,
        particles=p_kind,
        particle_um=sizing_um,
        swollen_um=(float(particles["swollen_diameter_um"])
                    if particles.get("swollen_diameter_um") else None),
        particle_count=(int(particles["count"]) if particles.get("count") else None),
        shear_class=shear_class,
        scale=scale,
        mix=mixes,
        branches=int(branches) if branches else None,
        retains=retains,
        needs_reagent=any(k in REAGENT_KINDS for k in kinds),
        drains=any(k in DRAINING_KINDS for k in kinds),
        holds_temp=bool(temp_holds),
        purpose="culture" if culture else PURPOSE_BY_KIND.get(kinds[0], kinds[0]),
        route_width_mm=round(route_w, 4),
        paper_volume_ul=paper,
        inline=inline,
        inline_why=inline_why,
        gaps=tuple(gaps),
        note="; ".join(str(o.get("note", "")) for o in group if o.get("note")),
    )


def brief_for_op(op: dict, *, scale: str = "demo") -> Brief:
    """One op, one part. Convenience wrapper over brief_for_group for a single op."""
    return brief_for_group([op], scale=scale)


def needs_a_flow(brief: Brief) -> bool:
    """True when no block for this part can be sized without a flow rate.

    Three cases: a wall-shear ceiling applies (the ceiling sets the width), the part mixes
    (the Peclet number sets the length), or nothing is held so the size is Q x t.
    """
    if brief.volume_ul is None:
        return True
    return (contracts.shear_ceiling_for(brief.shear_class).ceiling_dyn_cm2 is not None
            or brief.mix)


def carry_flow_forward(briefs: list[Brief]) -> list[Brief]:
    """Give every part that needs a flow rate one, carried from the ops that stated enough.

    `unitops.json` has no flow field, so a flow can only ever be DERIVED. Where an op gave
    a volume and a duration, `brief_for_group` already derived the mean flow from them.
    This fills the rest in from their neighbours - the rate the line last had - which is
    exactly what this repo already does when a protocol step omits a flow
    (`tools/validate.py` reports those as `flow_rate_not_stated`: "each feed keeps the rate
    it last had"). Only if the whole assay states no duration anywhere does it fall back to
    a contract flow, and that is flagged too.

    Every substitution leaves an `unresolved` entry naming where the number came from, so
    no flow on a generated chip can be mistaken for one the paper gave.
    """
    stated = [b.flow_ul_min for b in briefs if b.flow_ul_min]
    out: list[Brief] = []
    last: float | None = None
    for i, brief in enumerate(briefs):
        if brief.flow_ul_min:
            last = brief.flow_ul_min
            out.append(brief)
            continue
        if not needs_a_flow(brief):
            out.append(brief)
            continue
        later = next((b.flow_ul_min for b in briefs[i + 1:] if b.flow_ul_min), None)
        if last is not None:
            flow, where = last, "the rate the line last had, from an earlier op on this chip"
        elif later is not None:
            flow, where = later, "the rate the next op on this chip runs at"
        elif stated:
            flow, where = stated[0], "the only rate derived anywhere in this assay"
        else:
            flow = float(contracts.param("backflush.lift_flow_ul_min"))
            where = ("a contract flow, because no op in this assay gave a volume and a "
                     "duration to derive one from")
        gap = _gap(
            brief.op, "flow_ul_min",
            f"part '{brief.part_id}' (op(s) {', '.join(str(n) for n in brief.ops)}) cannot "
            f"be sized without a flow rate and its own ops gave neither a flow nor a "
            f"duration, so it uses {where}; this is a DESIGN flow, not one the paper stated",
            round(flow, 4),
            "contracts/parameters.json backflush.lift_flow_ul_min"
            if not stated and last is None and later is None else
            "derived from another op's volume / duration in this same assay")
        out.append(replace(brief, flow_ul_min=flow, gaps=brief.gaps + (gap,)))
        last = flow
    return out


def briefs_for(ops: list[dict], *, scale: str = "demo") -> list[Brief]:
    """One Brief per PART for the on-chip ops, in process order.

    Off-chip and missing_part ops are skipped before grouping, so an off-chip imaging step
    in the middle of a wash train does not split one trap into two. The flow rates are
    filled in afterwards, in one pass over the whole chip, because a part with no duration
    of its own can still take the rate its neighbours run at.
    """
    on_chip = [o for o in ops
               if (o.get("placement") or {}).get("where") in (None, "on_chip")]
    return carry_flow_forward(
        [brief_for_group(group, scale=scale) for group in group_ops(on_chip)])

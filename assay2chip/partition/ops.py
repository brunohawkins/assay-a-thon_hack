"""One readable view of a unitops.json op, and the "runs" the rest of S4 reasons about.

Two ideas live here.

**Op** is a thin, typed read of one entry in ``unitops.json``. It never invents a value:
anything the file left null stays None and the caller records the gap in ``unresolved``.
The one derived number is the swollen particle diameter, and it comes from
``contracts.capsule_swell_factor()`` rather than a literal, because a capsule that goes
into a line at 70 um arrives at twice that and every port, tube and channel on the way
has to pass the swollen size.

**Run** is a maximal stretch of consecutive ops that sit on the SAME part. This is the
unit that matters physically, and getting it wrong is what makes a naive port count
disagree with the chips already in this repo. ``module3_v1_translated`` runs lysis, a
wash, the 42 C reverse transcription and another wash *in one chamber*, so those four ops
share one reagent inlet and one chamber node - they are one place, not four. Treating
them as four would both triple the port estimate and invent channel crossings that do not
exist. ``cell_prep_v1``, by contrast, adds enzyme at the culture chamber and quench
downstream at the mixer, which is two places and therefore two feed ports - which is
exactly what that chip has.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from assay2chip.lib import contracts

#: Ops that pull a reagent in from off the board, so the part they sit on needs a feed.
#: `thermocycle` is absent on purpose: the amplification mix arrives with the sample, as
#: it does on module4_v1, which has four sample inlets and a vent and no reagent port.
#: `culture` is here because a culture chamber is perfused with medium (module1_v1's lane
#: feeds); `load` and `transfer` are not, because they carry the sample itself.
REAGENT_KINDS = ("merge", "mix", "wash", "lyse", "separate", "encapsulate", "culture")

#: Ops that send spent fluid off the board. `load` is one of them: pushing a bed into a
#: trap displaces its carrier, which is what module3_v1_translated's `waste` port is for.
WASTE_KINDS = ("wash", "separate", "lyse", "load", "culture")

#: How many reagent streams the op needs at once. Encapsulation is a three-stream
#: flow-focusing junction (oil and shell from off board, sample as the core), which is
#: why spc_demo_v1 has in_oil, in_shell and in_core.
STREAMS = {"encapsulate": 2}

#: Ops that hold particles back while fluid passes, so the part is a trap.
RETAINING_KINDS = ("load", "wash", "incubate", "culture", "separate", "lyse")

#: Ops that move the retained particles on again. `transfer` is the explicit one.
RELEASING_KINDS = ("transfer", "collect", "pool", "split", "merge")

#: Moving material from one place to another and nothing else. A transfer that falls on a
#: chip boundary IS the handoff, so S4 treats it as a natural place to cut rather than as
#: work a board has to do.
TRANSFER_KINDS = ("transfer",)

#: Ops whose whole point is that the cells are alive, whatever `particles` says.
LIVE_KINDS = ("culture",)

#: Ops that only move fluid about and hold nothing at a setpoint of their own, so a
#: thermal ramp across the board does them no harm. This is what lets the sublibrary
#: split share module4_v1's 98 C board while a 37 C enzymatic release cannot.
ROUTING_KINDS = ("load", "split", "pool", "merge", "transfer", "collect", "image")

#: Ops that end the cells' life, so a later temperature no longer threatens them.
TERMINAL_FOR_CELLS = ("lyse", "thermocycle")

#: Ops that need a gas path. A sealed chamber held at pcr.max_temp_c expands and has to
#: vent, which is what module4_v1's p_vent is for. Encapsulation is deliberately NOT here:
#: its continuous phase leaves with the capsules through the product outlet, which is why
#: spc_demo_v1 has four ports and no vent.
VENT_KINDS = ("thermocycle",)


@dataclass(frozen=True)
class Op:
    """One unit operation, as S4 needs to see it."""

    number: int
    kind: str
    volume_ul: Optional[float]
    viscosity_class: str
    particle_kind: str
    particle_um: Optional[float]
    swollen_um: Optional[float]
    duration_s: Optional[float]
    temp_c: Optional[float]
    shear_class: str
    where: str
    part: str
    why: str
    text: str
    #: What the place this op happens in must HOLD. `basis` is "none" when the op carries
    #: no `load` at all, and `has_load` says which of those two it is.
    load_basis: str = "none"
    has_load: bool = False
    load_source: str = ""
    load_cells: Optional[float] = None
    load_surface_density_per_mm2: Optional[float] = None
    load_area_mm2: Optional[float] = None
    load_packed_volume_ul: Optional[float] = None
    load_suspension_density_per_ul: Optional[float] = None
    load_residence_s: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Op":
        fluid = data.get("fluid") or {}
        particles = data.get("particles") or {}
        placement = data.get("placement") or {}
        kind = str(data.get("kind", ""))
        particle_kind = str(particles.get("kind", "none"))
        diameter = particles.get("diameter_um")
        swollen = particles.get("swollen_diameter_um")
        if swollen is None and diameter is not None and particle_kind == "capsules":
            # A capsule swells in culture; contracts owns the factor.
            swollen = float(diameter) * contracts.capsule_swell_factor()
        elif swollen is None:
            swollen = diameter
        # Everything a keyword lookup may legitimately read: the op's own prose and the
        # triage reasoning, never a number.
        text = " ".join(str(part) for part in (
            data.get("note", ""), placement.get("why", ""), placement.get("note", ""),
            placement.get("need", ""), fluid.get("note", ""), particles.get("note", ""),
            placement.get("part", ""),
        ) if part)
        load = data.get("load")
        has_load = isinstance(load, dict)
        load = load if has_load else {}

        def number(key):
            value = load.get(key)
            return None if value is None else float(value)

        return cls(
            number=int(data.get("op", 0)),
            kind=kind,
            volume_ul=None if fluid.get("volume_ul") is None else float(fluid["volume_ul"]),
            viscosity_class=str(fluid.get("viscosity_class", "aqueous")),
            particle_kind=particle_kind,
            particle_um=None if diameter is None else float(diameter),
            swollen_um=None if swollen is None else float(swollen),
            duration_s=None if data.get("duration_s") is None else float(data["duration_s"]),
            temp_c=None if data.get("temp_c") is None else float(data["temp_c"]),
            shear_class=str(data.get("shear_class", "none")),
            where=str(placement.get("where", "on_chip")),
            part=str(placement.get("part", "")),
            why=str(placement.get("why", "")),
            text=text.lower(),
            load_basis=str(load.get("basis", "none")),
            has_load=has_load,
            load_source=str(load.get("source", "")),
            load_cells=number("cells"),
            load_surface_density_per_mm2=number("surface_density_per_mm2"),
            load_area_mm2=number("area_mm2"),
            load_packed_volume_ul=number("packed_volume_ul"),
            load_suspension_density_per_ul=number("suspension_density_per_ul"),
            load_residence_s=number("residence_s"),
            raw=data,
        )

    # -- small, honest predicates ------------------------------------------------

    @property
    def on_chip(self) -> bool:
        return self.where == "on_chip"

    @property
    def pending(self) -> bool:
        """S2 said "not decided". S3 must replace it and S4 must refuse it."""
        return self.where == "pending"

    @property
    def is_transfer(self) -> bool:
        return self.kind in TRANSFER_KINDS

    @property
    def has_particles(self) -> bool:
        return self.particle_kind not in ("none", "")

    @property
    def live_cells(self) -> bool:
        """Cells that are still alive, so the board has a temperature ceiling.

        A lysis or a thermocycle is not live cells even when the op still says `cells`:
        the point of the step is that they stop being alive.
        """
        if self.kind in TERMINAL_FOR_CELLS:
            return False
        if self.kind in LIVE_KINDS:
            return True
        return self.particle_kind == "cells" or self.shear_class == "adherent_cells"

    @property
    def needs_reagent(self) -> bool:
        return self.kind in REAGENT_KINDS

    @property
    def streams(self) -> int:
        return STREAMS.get(self.kind, 1)

    @property
    def makes_waste(self) -> bool:
        return self.kind in WASTE_KINDS

    @property
    def needs_vent(self) -> bool:
        return self.kind in VENT_KINDS

    def label(self) -> str:
        return "op %d (%s)" % (self.number, self.kind)


@dataclass
class Run:
    """Consecutive ops on one part: one physical place on the board."""

    index: int
    part: str
    ops: List[Op]

    @property
    def node(self) -> str:
        return "part_%d" % (self.index + 1)

    @property
    def numbers(self) -> List[int]:
        return [op.number for op in self.ops]

    @property
    def reagent_ops(self) -> List[Op]:
        return [op for op in self.ops if op.needs_reagent]

    @property
    def streams(self) -> int:
        return max([op.streams for op in self.reagent_ops] or [0])

    def label(self) -> str:
        numbers = self.numbers
        span = str(numbers[0]) if len(numbers) == 1 else "%d-%d" % (numbers[0], numbers[-1])
        return "ops %s on %s" % (span, self.part or "an unnamed part")


class PendingPlacement(ValueError):
    """unitops.json still holds a `pending` placement, so S3 has not finished.

    ``placement.where: "pending"`` is S2's explicit "not decided yet". Partitioning it
    would mean guessing whether the op is on a board, which is the one thing this stage
    must never do - it would silently invent a chip or silently drop a step.
    """


def refuse_pending(ops: Sequence[Op]) -> None:
    """Raise unless every placement has been decided."""
    pending = [op for op in ops if op.pending]
    if not pending:
        return
    listed = ", ".join("op %d (%s)" % (op.number, op.kind) for op in pending)
    raise PendingPlacement(
        "S4 cannot partition %d op(s) whose placement is still 'pending': %s. "
        "placement.where 'pending' is S2's explicit 'not decided'; S3 triage has to "
        "replace every one with on_chip, off_chip or missing_part first "
        "(python assay2chip/run.py --id <id> --from s3)." % (len(pending), listed))


def unknown_blocks(ops: Sequence[Op]) -> List[Tuple[int, str]]:
    """(op number, part) for on-chip ops naming something that is not a blocks.json id.

    ``placement.part`` is a block id from contracts/blocks.json - the same vocabulary as
    every chip spec's `block` field - so a name outside the library cannot be built and
    S4 reports it rather than planning around it.
    """
    library = contracts.library()
    return [(op.number, op.part) for op in ops
            if op.on_chip and op.part and op.part not in library]


def load_ops(unitops: Dict[str, Any]) -> List[Op]:
    """Every op in process order. Order comes from `op`, not from the list order."""
    return sorted((Op.from_dict(item) for item in unitops.get("ops", [])),
                  key=lambda op: op.number)


def same_place(previous: Op, op: Op) -> bool:
    """Do two consecutive ops happen in the same physical part?

    Two tests, and the second one matters because ``placement.part`` is a *block id*, not
    an instance id: module5_v1's reaction chamber and its bead chamber are both
    ``polygon_chamber``, so the block id alone would merge them into one place and lose a
    feed port. A new particle population is a new bed and therefore a new part - the
    magnetic beads arrive in their own chamber - while the same population staying put
    through lysis, washes and the RT hold is one bed in one chamber, which is exactly what
    module3_v1_translated does with its single ``main_trap``.
    """
    if not op.part or previous.part != op.part:
        return False
    if previous.particle_kind != op.particle_kind \
            and "none" not in (previous.particle_kind, op.particle_kind):
        return False
    return True


def runs_of(ops: Sequence[Op]) -> List[Run]:
    """Group consecutive ops that sit on the same part.

    An op with no `part` (it should not happen for on_chip ops, since the unitops schema
    requires one, but S4 must not crash on it) gets a run of its own, which is the
    pessimistic reading: its own place and its own ports.
    """
    out: List[Run] = []
    for op in ops:
        if out and same_place(out[-1].ops[-1], op):
            out[-1].ops.append(op)
            continue
        out.append(Run(len(out), op.part, [op]))
    return out


def temperatures(ops: Sequence[Op]) -> List[float]:
    return [op.temp_c for op in ops if op.temp_c is not None]


def temp_range(ops: Sequence[Op]) -> Optional[Tuple[float, float]]:
    values = temperatures(ops)
    if not values:
        return None
    return (min(values), max(values))


def largest_particle_um(ops: Sequence[Op]) -> Optional[float]:
    """The biggest thing any of these ops has to pass, swollen rather than as made."""
    values = [op.swollen_um for op in ops if op.swollen_um is not None]
    return max(values) if values else None

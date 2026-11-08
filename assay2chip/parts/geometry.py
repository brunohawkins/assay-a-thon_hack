"""The types `registry.size()` speaks: Need in, PartGeometry out.

A Need is what a unit operation requires - volume, residence time, flow, what particles
are in it, which shear ceiling applies, and the chip's single etch depth. A PartGeometry
is a real, printable part: a footprint, named openings on its boundary for the router to
join, the internal features a mould has to cut, the chip-spec entries it expands to, and
the fabrication floors it satisfies. When the physics cannot meet a floor there is no
PartGeometry: `size()` raises PartSizingError saying which floor and by how much.

VOLUME IS BY THE RIGHT FORMULA PER SHAPE, never a bounding box:

    box         x * y * z
    cylinder    pi/4 * dx * dy * z
    serpentine  centreline length * lane width * depth
    weir chamber    box MINUS the crest (crest_thickness * lane width * crest height)
    comb        spine + every tooth
    pillared    box MINUS every pillar

A bounding-box volume on a serpentine overstates it by the fold ratio - about 3x - and a
protocol that trusts it flushes a third of what it should.

ONE NOTE ON `entries`. The brief asks each expanded entry to carry `block` and
`volume_ul`. A chip-spec *chamber* can hold both, but the chip_spec schema's channel
object sets additionalProperties: false and has no volume_ul, so a channel entry with a
volume in it would fail contracts/schemas/chip_spec.schema.json. So each expansion is an
Expansion: the schema-valid entry plus the volume and the formula that produced it,
alongside. `.chambers` and `.channels` hand back the entries exactly as the schema wants
them; `.entries` is where the volume lives.
"""

from __future__ import annotations

import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402

PLACES = 4  # mm, i.e. 0.1 um - finer than any process here resolves

Direction = Literal["-x", "+x", "-y", "+y"]
NORMALS: dict[str, tuple[float, float]] = {
    "-x": (-1.0, 0.0), "+x": (1.0, 0.0), "-y": (0.0, -1.0), "+y": (0.0, 1.0),
}

PARTICLE_KINDS = ("none", "cells", "capsules", "beads")

# unitops.json particle kind -> chip_spec channel `carries`. beads take the capsule rule
# (width >= 1.5 x diameter) because it is the same rule; the schema has no bead value.
CARRIES_FOR_PARTICLE = {
    "none": "liquid", "cells": "cells", "capsules": "capsules", "beads": "capsules",
}


def r(value: float) -> float:
    """Round to the registry's one precision, so two identical Needs give one answer."""
    return round(float(value), PLACES)


def r_up(value: float) -> float:
    """Round UP to the registry's precision. For any dimension derived FROM a limit.

    A lane width inverted out of a shear ceiling is exact: round it to nearest and it can
    land 0.05 um narrow, which puts the shear a whisker OVER the ceiling and refuses a
    part that is really fine. Rounding away from the limit cannot do that.
    """
    return math.ceil(float(value) * 10 ** PLACES - 1e-9) / 10 ** PLACES


def snake(*bits: str) -> str:
    """A chip_spec snake_id: ^[a-z][a-z0-9]*(_[a-z0-9]+)*$."""
    raw = "_".join(str(b) for b in bits if str(b))
    out = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    out = re.sub(r"_+", "_", out)
    if not out or not out[0].isalpha():
        out = "p_" + out
    return out


class PartSizingError(ValueError):
    """This block cannot be built for this need, and here is the physical reason.

    Raised instead of returning a part that breaks a floor. Every message names the block,
    the floor or law it failed, the number it reached and the number it had to reach, so
    S6 can put it straight into `unresolved` or send the op off-chip.
    """

    def __init__(self, block: str, reason: str, *, floor: str | None = None,
                 required: float | None = None, actual: float | None = None) -> None:
        super().__init__(f"{block}: {reason}")
        self.block = block
        self.reason = reason
        self.floor = floor
        self.required = required
        self.actual = actual


# --- volume formulas ---------------------------------------------------------

def box_volume_ul(x_mm: float, y_mm: float, z_mm: float) -> float:
    return x_mm * y_mm * z_mm


def cylinder_volume_ul(dx_mm: float, dy_mm: float, z_mm: float) -> float:
    """pi/4 * dx * dy * z - an ellipse when dx != dy, a circle when they match."""
    return math.pi / 4.0 * dx_mm * dy_mm * z_mm


def serpentine_volume_ul(length_mm: float, width_mm: float, depth_mm: float) -> float:
    """Centreline length x lane width x depth. NOT the folded bounding box."""
    return length_mm * width_mm * depth_mm


def weir_chamber_volume_ul(x_mm: float, lane_width_mm: float, depth_mm: float,
                           crest_thickness_mm: float, crest_height_mm: float) -> float:
    """Box minus the standing crest, which is solid and displaces fluid."""
    return box_volume_ul(x_mm, lane_width_mm, depth_mm) - \
        crest_thickness_mm * lane_width_mm * crest_height_mm


def pillared_volume_ul(x_mm: float, y_mm: float, z_mm: float, pillar_count: int,
                       pillar_diameter_mm: float) -> float:
    """Box minus every pillar. Lid support is not free: it costs chamber volume."""
    return box_volume_ul(x_mm, y_mm, z_mm) - \
        pillar_count * cylinder_volume_ul(pillar_diameter_mm, pillar_diameter_mm, z_mm)


# --- the need ----------------------------------------------------------------

LOAD_BASES = ("adherent_area", "packed_volume", "suspension_volume", "flow_through", "none")


@dataclass(frozen=True)
class Load:
    """What the place this op happens in must HOLD - the unitops.json `load` object.

    This, not the paper's tube volume, is what a part is sized from. "Wash the pellet in
    500 uL" describes a TUBE: building a 500 uL chamber is wrong and so is refusing the
    0.012 uL that is left when the tube volume is dropped. See assay2chip/README.md,
    "Bench volumes do not transfer".

      adherent_area      floor area = cells / surface_density_per_mm2
      packed_volume      a bed: from count, diameter and capsule.packing_fraction
      suspension_volume  volume = cells / suspension_density_per_ul
      flow_through       nothing is held; size from flow and residence_s
      none               no load, so Need.volume_ul is used as before
    """

    basis: str = "none"
    cells: float | None = None
    surface_density_per_mm2: float | None = None
    area_mm2: float | None = None
    packed_volume_ul: float | None = None
    suspension_density_per_ul: float | None = None
    residence_s: float | None = None
    source: str = "derived"
    note: str = ""

    def __post_init__(self) -> None:
        if self.basis not in LOAD_BASES:
            raise PartSizingError(
                "need", f"load.basis must be one of {', '.join(LOAD_BASES)}, not "
                        f"{self.basis!r} (contracts/schemas/unitops.schema.json)")
        if self.source not in ("paper", "derived", "contract_default"):
            raise PartSizingError(
                "need", f"load.source must be paper, derived or contract_default, not "
                        f"{self.source!r}")


@dataclass(frozen=True)
class Need:
    """What one unit operation requires of one part. Nothing here is guessed.

    Leave a field None when the assay never said: a sizer that needs it raises with the
    name of the field rather than inventing a value, and S6 records that in `unresolved`.

    `load` is the preferred way to say how big the part must be; `volume_ul` still works
    unchanged for every existing caller, and is what a `load` resolves into.
    """

    # volume_ul stays FIRST: it was the first field before `load` existed, so any caller
    # that passed it positionally still works.
    volume_ul: float | None = None
    load: Load | None = None
    residence_s: float | None = None
    flow_ul_min: float | None = None
    particles: str = "none"
    particle_um: float | None = None
    shear_class: str = "none"
    depth_mm: float | None = None
    temp_c: float | None = None
    scale: str = "demo"
    mix: bool = False
    branches: int | None = None
    pressure_drop_pa: float | None = None
    length_mm: float | None = None
    feed_edge_mm: float | None = None
    label: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.particles not in PARTICLE_KINDS:
            raise PartSizingError(
                "need", f"particles must be one of {', '.join(PARTICLE_KINDS)}, "
                        f"not {self.particles!r}")
        for name in ("volume_ul", "residence_s", "flow_ul_min", "particle_um", "depth_mm"):
            v = getattr(self, name)
            if v is not None and float(v) <= 0:
                raise PartSizingError("need", f"{name} must be positive, got {v!r}")

    @property
    def carries(self) -> str:
        return CARRIES_FOR_PARTICLE[self.particles]

    def require(self, *names: str) -> None:
        missing = [n for n in names if getattr(self, n) is None]
        if missing:
            raise PartSizingError(
                "need", "this part cannot be sized without " + ", ".join(missing) +
                        " - record the gap in `unresolved` rather than assuming one")


# --- the output --------------------------------------------------------------

def min_span_mm() -> float:
    """The smallest in-plane dimension anything can have: the 200 um lateral floor."""
    return contracts.floor("min_open_channel_mm")


def min_chamber_volume_ul(depth_mm: float) -> float:
    """The smallest chamber the floors allow: a floor-square footprint at this depth.

    0.2 x 0.2 x 0.3 mm = 0.012 uL at the demo depth. Any load that resolves below this is
    ROUNDED UP to it rather than refused: a 0.012 uL step is a real step, and the first
    end-to-end run died at S6 refusing exactly that.
    """
    return min_span_mm() ** 2 * depth_mm


@dataclass(frozen=True)
class VolumeRecord:
    """What a part was sized to hold, what was asked for, and the gap between them.

    Carried into `physics` on every part, so a volume that was rounded up to the smallest
    buildable chamber says so instead of looking like the number the assay asked for.
    """

    volume_ul: float
    requested_ul: float
    basis: str
    source: str
    rounded_up: bool
    floor_volume_ul: float
    area_mm2: float | None = None
    why: tuple[str, ...] = ()

    def as_physics(self) -> dict:
        out: dict[str, Any] = {
            "volume_ul": r(self.volume_ul),
            "volume_requested_ul": r(self.requested_ul),
            "volume_basis": self.basis,
            "volume_source": self.source,
            "volume_rounded_up": self.rounded_up,
            "smallest_buildable_volume_ul": r(self.floor_volume_ul),
            "volume_set_by": list(self.why),
        }
        if self.area_mm2 is not None:
            out["floor_area_mm2"] = r(self.area_mm2)
        if self.rounded_up:
            out["volume_rounded_up_factor"] = round(
                self.volume_ul / max(self.requested_ul, 1e-12), 2)
        return out


@dataclass(frozen=True)
class Opening:
    """An attachment point on the part's boundary. The router joins these, nothing else.

    position is part-local mm with the origin at the footprint's bottom-left corner, so a
    placer adds one offset and is done. width_mm is the real lane width at the opening,
    which is what the shear law on the joining channel must use.
    """

    name: str
    position: tuple[float, float]
    direction: Direction
    width_mm: float
    role: str = "inlet"
    carries: str = "liquid"
    note: str = ""

    @property
    def normal(self) -> tuple[float, float]:
        return NORMALS[self.direction]


@dataclass(frozen=True)
class Feature:
    """Internal geometry a mould or printer actually has to cut.

    This is the half of the library that used to exist only as English prose: a weir
    crest, a comb tooth, a pillar, a herringbone groove, a nozzle throat. size_mm is
    (x, y, z) and the z component is a DEPTH feature, exempt from the 200 um in-plane
    floor - which is the whole reason a 25 um weir gap is buildable.
    """

    kind: str
    count: int
    size_mm: tuple[float, float, float]
    pitch_mm: float | None = None
    in_plane_min_mm: float | None = None
    note: str = ""


@dataclass(frozen=True)
class Expansion:
    """One chip-spec entry plus its volume and the formula that produced it."""

    kind: Literal["chamber", "channel"]
    entry: dict
    volume_ul: float
    formula: str

    @property
    def block(self) -> str:
        return self.entry.get("block", "")


@dataclass(frozen=True)
class FloorCheck:
    """One fabrication floor against one real dimension of this part.

    `axis` is the point of the type. in_plane dimensions are bound by the contract's
    floors; z dimensions are not bound by them at all, and recording that explicitly is
    what stops the next reader from "fixing" a 25 um gap up to 200 um.
    """

    floor: str
    what: str
    required_mm: float
    actual_mm: float
    axis: Literal["in_plane", "z", "process"]
    ok: bool
    note: str = ""


@dataclass(frozen=True)
class PartGeometry:
    """A printable part, sized from physics, ready for the placer and the router."""

    block: str
    footprint_mm: tuple[float, float]
    depth_mm: float
    volume_ul: float
    openings: tuple[Opening, ...] = ()
    features: tuple[Feature, ...] = ()
    entries: tuple[Expansion, ...] = ()
    floors: tuple[FloorCheck, ...] = ()
    physics: dict[str, Any] = field(default_factory=dict)
    risks: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    provenance: tuple[str, ...] = ()
    note: str = ""

    @property
    def chambers(self) -> tuple[dict, ...]:
        return tuple(e.entry for e in self.entries if e.kind == "chamber")

    @property
    def channels(self) -> tuple[dict, ...]:
        return tuple(e.entry for e in self.entries if e.kind == "channel")

    @property
    def area_mm2(self) -> float:
        return r(self.footprint_mm[0] * self.footprint_mm[1])

    @property
    def satisfies(self) -> tuple[str, ...]:
        """Names of the floors this part was checked against and passes."""
        return tuple(sorted({f.floor for f in self.floors if f.ok}))

    def opening(self, name: str) -> Opening:
        for o in self.openings:
            if o.name == name:
                return o
        raise KeyError(f"{self.block} has no opening {name!r}; it has "
                       f"{', '.join(o.name for o in self.openings)}")

    def as_dict(self) -> dict:
        """Plain JSON, for the S6 output, the report and the tests."""
        return {
            "block": self.block,
            "footprint_mm": [r(self.footprint_mm[0]), r(self.footprint_mm[1])],
            "area_mm2": self.area_mm2,
            "depth_mm": r(self.depth_mm),
            "volume_ul": r(self.volume_ul),
            "openings": [
                {"name": o.name, "position": [r(o.position[0]), r(o.position[1])],
                 "direction": o.direction, "width_mm": r(o.width_mm), "role": o.role,
                 "carries": o.carries, **({"note": o.note} if o.note else {})}
                for o in self.openings],
            "features": [
                {"kind": f.kind, "count": f.count,
                 "size_mm": [r(v) for v in f.size_mm], "pitch_mm": f.pitch_mm,
                 "in_plane_min_mm": f.in_plane_min_mm, "note": f.note}
                for f in self.features],
            "entries": [
                {"kind": e.kind, "volume_ul": r(e.volume_ul), "formula": e.formula,
                 "entry": e.entry}
                for e in self.entries],
            "floors": [
                {"floor": f.floor, "what": f.what, "required_mm": f.required_mm,
                 "actual_mm": r(f.actual_mm), "axis": f.axis, "ok": f.ok,
                 **({"note": f.note} if f.note else {})}
                for f in self.floors],
            "satisfies": list(self.satisfies),
            "physics": self.physics,
            "risks": list(self.risks),
            "assumptions": list(self.assumptions),
            "provenance": list(self.provenance),
            "note": self.note,
        }


# --- floor enforcement -------------------------------------------------------

class Floors:
    """Every fabrication floor, read from the contract once, enforced in one place.

    Call `in_plane()` for a lateral dimension, `wall()` for a standing wall or rib,
    `port()` for a port opening, `z()` to RECORD a depth feature (it is deliberately not
    checked against a lateral floor), and `enclosed()` for the print-open-and-seal rule.
    `finish()` raises PartSizingError if anything failed, naming the worst offender.
    """

    def __init__(self, block: str, scale: str = "demo") -> None:
        self.block = block
        self.values = contracts.fabrication_floors()
        self.envelope = contracts.envelope(scale)
        self.checks: list[FloorCheck] = []

    def floor(self, name: str) -> float:
        return contracts.floor(name)

    # the 200 um lateral floor: widths, gaps, tooth widths, crest thickness
    def in_plane(self, what: str, actual_mm: float, note: str = "") -> float:
        req = self.floor("min_open_channel_mm")
        self.checks.append(FloorCheck("min_open_channel_mm", what, req, actual_mm,
                                      "in_plane", actual_mm >= req - 1e-9, note))
        return actual_mm

    def wall(self, what: str, actual_mm: float, note: str = "") -> float:
        req = self.floor("min_wall_mm")
        self.checks.append(FloorCheck("min_wall_mm", what, req, actual_mm,
                                      "in_plane", actual_mm >= req - 1e-9, note))
        return actual_mm

    def port(self, what: str, actual_mm: float) -> float:
        req = self.floor("min_port_mm")
        self.checks.append(FloorCheck("min_port_mm", what, req, actual_mm,
                                      "in_plane", actual_mm >= req - 1e-9, ""))
        return actual_mm

    def port_pitch(self, what: str, actual_mm: float) -> float:
        req = self.floor("min_port_pitch_mm")
        self.checks.append(FloorCheck("min_port_pitch_mm", what, req, actual_mm,
                                      "in_plane", actual_mm >= req - 1e-9, ""))
        return actual_mm

    def z(self, what: str, actual_mm: float, note: str = "") -> float:
        """Record a depth feature. NOT checked against the lateral floor - by contract.

        required_mm is 0 because there is no z floor in contracts/blocks.json: z is set by
        the mould relief and is free below the layer depth. Only the layer depth itself is
        a ceiling, and `depth()` checks that.
        """
        self.checks.append(FloorCheck(
            "min_open_channel_mm", what, 0.0, actual_mm, "z", True,
            note or "z dimension: the 200 um in-plane floor does not apply (blocks.json "
                    "retention, chip_spec.schema.json chamber.retention)"))
        return actual_mm

    def depth(self, actual_mm: float) -> float:
        req = contracts.max_depth_mm()
        ok = actual_mm <= req + 1e-9
        self.checks.append(FloorCheck("max_depth_mm", "etch depth", req, actual_mm,
                                      "z", ok,
                                      "ONE fluidic layer at ONE etch depth"))
        if not ok:
            raise PartSizingError(self.block,
                                  f"etch depth {actual_mm:g} mm exceeds the contract's "
                                  f"max_depth_mm {req:g} mm",
                                  floor="max_depth_mm", required=req, actual=actual_mm)
        return actual_mm

    def enclosed(self, what: str, width_mm: float) -> None:
        """min_enclosed_printed_channel_mm, satisfied by PROCESS not by size.

        The contract's own note: "Print open and seal with film rather than printing an
        enclosed channel below 0.5 mm." So a 0.2 mm channel is legal - it is printed open
        and lidded - and this records which branch of that rule the part takes.
        """
        req = self.floor("min_enclosed_printed_channel_mm")
        if width_mm >= req - 1e-9:
            note = "wide enough to print enclosed if we ever wanted to"
        else:
            note = ("below min_enclosed_printed_channel_mm, so it is printed OPEN and "
                    "sealed with film - the contract's stated route, not a violation")
        self.checks.append(FloorCheck("min_enclosed_printed_channel_mm", what, req,
                                      width_mm, "process", True, note))

    def fits(self, x_mm: float, y_mm: float) -> None:
        env = self.envelope
        for axis, actual, limit in (("x", x_mm, env.x_mm), ("y", y_mm, env.y_mm)):
            ok = actual <= limit + 1e-9
            self.checks.append(FloorCheck(f"envelope_{axis}_mm", f"footprint {axis}",
                                          limit, actual, "in_plane", ok, env.source))
            if not ok:
                raise PartSizingError(
                    self.block,
                    f"footprint {axis} {actual:.3g} mm exceeds the {env.name} envelope's "
                    f"{limit:g} mm. Either split the op across more parts or move it to "
                    f"the {'target' if env.name == 'demo' else 'ceiling'} scale.",
                    floor=f"envelope_{axis}_mm", required=limit, actual=actual)

    def finish(self) -> tuple[FloorCheck, ...]:
        bad = [c for c in self.checks if not c.ok]
        if bad:
            worst = min(bad, key=lambda c: c.actual_mm - c.required_mm)
            raise PartSizingError(
                self.block,
                f"{worst.what} is {worst.actual_mm:.4g} mm, below the contract's "
                f"{worst.floor} ({worst.required_mm:g} mm)"
                + (f"; {len(bad)} floors fail in total" if len(bad) > 1 else ""),
                floor=worst.floor, required=worst.required_mm, actual=worst.actual_mm)
        return tuple(self.checks)


# --- shared layout helpers ---------------------------------------------------

@dataclass(frozen=True)
class Fold:
    """A folded run: n lanes of `run_mm`, lane width w, ribs between them."""

    lanes: int
    run_mm: float
    lane_width_mm: float
    rib_mm: float
    length_mm: float
    footprint_mm: tuple[float, float]

    @property
    def turns(self) -> int:
        return max(0, self.lanes - 1)

    def path(self) -> list[list[float]]:
        """Centreline waypoints, part-local mm, so the LENGTH is data and not prose."""
        pts: list[list[float]] = []
        half = self.lane_width_mm / 2.0
        pitch = self.lane_width_mm + self.rib_mm
        for i in range(self.lanes):
            y = r(half + i * pitch)
            x0, x1 = (0.0, self.run_mm) if i % 2 == 0 else (self.run_mm, 0.0)
            pts.append([r(x0), y])
            pts.append([r(x1), y])
        return pts


def fold_into_lanes(length_mm: float, lane_width_mm: float, rib_mm: float,
                    max_x_mm: float, block: str) -> Fold:
    """Fold a centreline length into a roughly square block of lanes. Deterministic.

    The turns are part of the length, so n lanes of `run` plus (n-1) pitch-long turns add
    up to exactly the length asked for - a fold that ignored its own turns would come out
    long, which on a delay line means the wrong residence time.
    """
    pitch = lane_width_mm + rib_mm
    side = math.sqrt(max(length_mm, 1e-9) * pitch)
    run = min(side, max_x_mm)
    lanes = max(1, math.ceil((length_mm + pitch) / (run + pitch)))
    run = (length_mm - (lanes - 1) * pitch) / lanes
    while run < pitch and lanes > 1:
        lanes -= 1
        run = (length_mm - (lanes - 1) * pitch) / lanes
    if run <= 0:
        raise PartSizingError(block, f"a {length_mm:.3g} mm run cannot be folded at "
                                     f"{pitch:.3g} mm pitch inside {max_x_mm:.3g} mm")
    if run > max_x_mm + 1e-9:
        raise PartSizingError(
            block, f"a single {run:.3g} mm lane is longer than the {max_x_mm:.3g} mm "
                   f"available, so this run cannot be folded into the envelope")
    y = lanes * lane_width_mm + (lanes - 1) * rib_mm
    return Fold(lanes, r(run), r(lane_width_mm), r(rib_mm), r(length_mm),
                (r(run), r(y)))


@dataclass
class Ctx:
    """Everything a sizer needs, resolved from the contracts exactly once per call.

    Built by registry.size() so no sizer reads a contract twice and none of them can
    disagree about the depth, the viscosity or the shear ceiling in play.
    """

    block: str
    need: Need
    depth_mm: float
    floors: Floors
    envelope: contracts.Envelope
    viscosity: contracts.Viscosity
    ceiling: contracts.ShearCeiling
    particle_um: float | None
    provenance: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    volume: VolumeRecord | None = None

    @property
    def mu(self) -> float:
        return self.viscosity.pa_s

    @property
    def margin_mm(self) -> float:
        """Edge margin between a part and the chip boundary: one minimum wall."""
        return contracts.floor("min_wall_mm")

    @property
    def usable_x_mm(self) -> float:
        return self.envelope.x_mm - 2 * self.margin_mm

    @property
    def usable_y_mm(self) -> float:
        return self.envelope.y_mm - 2 * self.margin_mm

    def cite(self, *sources: str) -> None:
        for s in sources:
            if s not in self.provenance:
                self.provenance.append(s)

    def assume(self, *items: str) -> None:
        for s in items:
            if s not in self.assumptions:
                self.assumptions.append(s)

    def risk(self, *items: str) -> None:
        for s in items:
            if s not in self.risks:
                self.risks.append(s)

    def prefix(self, default: str) -> str:
        return snake(self.need.label or default)

    def particle_floor_mm(self) -> float:
        """Narrowest lane the cargo can pass, from the contract's own factors.

        capsules / beads: capsule_channel.width_factor_low x diameter, and the diameter
        that matters is the SWOLLEN one wherever culture has happened.
        cells: dissociation.outlet_min_width_um, the same number tools/check_chip_spec.py
        applies to a cell-carrying channel.
        """
        kind = self.need.particles
        if kind in ("capsules", "beads"):
            d = self.particle_um or contracts.capsule_diameter_um()
            factor = float(contracts.param("capsule_channel.width_factor_low"))
            self.cite("contracts/parameters.json capsule_channel.width_factor_low")
            return factor * d / 1000.0
        if kind == "cells":
            self.cite("contracts/parameters.json dissociation.outlet_min_width_um")
            return float(contracts.param("dissociation.outlet_min_width_um")) / 1000.0
        return 0.0


def chamber_entry(local_id: str, type_: str, block: str, size_mm: tuple[float, float, float],
                  volume_ul: float, *, shape: str = "box",
                  center: tuple[float, float, float] | None = None,
                  **extra: Any) -> dict:
    """A chip_spec chamber, centred in its own part-local frame.

    `center` is part-local: the placer adds the part's origin and the layer's z base. The
    schema requires the field, so it is filled rather than left out.
    """
    cx, cy, cz = center or (size_mm[0] / 2.0, size_mm[1] / 2.0, size_mm[2] / 2.0)
    entry = {
        "block": block,
        "id": local_id,
        "type": type_,
        "shape": shape,
        "center": [r(cx), r(cy), r(cz)],
        "size": [r(size_mm[0]), r(size_mm[1]), r(size_mm[2])],
        "volume_ul": r(volume_ul),
    }
    entry.update({k: v for k, v in extra.items() if v is not None})
    return entry


def channel_entry(local_id: str, block: str, from_: str, to: str, width_mm: float,
                  depth_mm: float, *, carries: str = "liquid",
                  path: Iterable[Iterable[float]] | None = None, **extra: Any) -> dict:
    """A chip_spec channel. `path` carries the real centreline, so length is data."""
    entry: dict[str, Any] = {
        "block": block,
        "id": local_id,
        "from": from_,
        "to": to,
        "width_mm": r(width_mm),
        "depth_mm": r(depth_mm),
        "carries": carries,
    }
    if path is not None:
        entry["path"] = [[r(p[0]), r(p[1])] for p in path]
    entry.update({k: v for k, v in extra.items() if v is not None})
    return entry

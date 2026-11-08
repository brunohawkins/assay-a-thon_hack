"""Can these ops share one chip? Every constraint in one place, each one explainable.

``feasible(ops)`` is the only question the segmentation algorithm asks, and it answers
with the *first* reason it cannot be done rather than a bare False, because that reason
becomes the cut's ``why`` and has to name the contract key that forced it.

The seven checks, in the order they are applied (cheapest and most fundamental first):

1. ``placement``  - an off_chip or missing_part op is not on a chip at all.
2. ``temperature`` - live cells versus 42 C and above, the 98 C dock, and the material
                     that follows from the hottest step (``partition/thermal.py``).
3. ``contamination`` - reagents that may not share a wetted path at all
                     (``partition/chemistry.py``).
4. ``ports``      - against ``fabrication_floors.max_ports_per_chip``, which the contract
                     files as a floor but describes as a demo-envelope limit that lifts at
                     target scale, so it is only enforced for a demo chip.
5. ``area``       - parts, walls, port cells and channel runs against the envelope
                     (``partition/area.py``).
6. ``shear``      - a chip may not mix an adherent-cell ceiling with a capsule chamber
                     bounded by pressure instead; ``contracts.shear_ceiling_for`` keeps
                     the three cases apart and this check keeps them off one board.
7. ``planarity``  - the connectivity graph with every port on the boundary must embed in
                     one layer (``partition/graph.py``). Last because it is the dearest,
                     and unlike the others it cannot be fixed by a bigger envelope.

``forced_by`` values are exactly the enum in ``contracts/schemas/partition.schema.json``;
planarity and material have no enum of their own, so they are ``other`` with the
constraint named in ``note``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from assay2chip.lib import contracts
from assay2chip.partition import area, chemistry, ports, thermal
from assay2chip.partition.ops import Op


@dataclass(frozen=True)
class Violation:
    """One reason a set of ops cannot share a chip."""

    constraint: str          # the check's name, for the report and the cut's note
    forced_by: str           # partition.schema.json cuts[].forced_by
    why: str                 # one sentence, names the contract key
    keys: Tuple[str, ...] = ()

    def as_cut(self, between: Sequence[int]) -> Dict[str, object]:
        return {
            "between": [int(between[0]), int(between[1])],
            "why": self.why,
            "forced_by": self.forced_by,
            "note": "constraint: %s; contract keys: %s"
                    % (self.constraint, ", ".join(self.keys) if self.keys else "-"),
        }


@dataclass
class Feasibility:
    """The verdict for one candidate chip, with everything the chip entry needs."""

    ok: bool
    scale: Optional[str] = None
    violations: List[Violation] = field(default_factory=list)
    port_plan: Optional[ports.PortPlan] = None
    area: Optional[area.AreaEstimate] = None
    material: Optional[thermal.MaterialChoice] = None
    notes: List[str] = field(default_factory=list)
    gaps: List[Dict[str, object]] = field(default_factory=list)

    @property
    def first(self) -> Optional[Violation]:
        return self.violations[0] if self.violations else None

    @property
    def port_count(self) -> int:
        return self.port_plan.count if self.port_plan else 0


def _shear_violation(ops: Sequence[Op]) -> Optional[Violation]:
    """Adherent cells and capsules are bounded by different things, not by one number.

    ``contracts.shear_ceiling_for`` is explicit that adherent cells have a wall-shear
    ceiling while a capsule chamber has none and is bounded by
    ``capsule.escape_pressure_kpa`` x ``control.pressure_margin`` instead. One board is
    one pressure source, so running both on it means either detaching the cells or
    squeezing the capsules through the weir.
    """
    classes = {op.shear_class for op in ops if op.shear_class != "none"}
    if "adherent_cells" in classes and "capsules" in classes:
        culture = contracts.shear_ceiling_for("adherent_cells")
        capsule = contracts.shear_ceiling_for("capsules")
        return Violation(
            "shear", "shear",
            "an adherent-cell chamber is capped at %g dyn/cm2 (shear."
            "culture_ceiling_dyn_cm2) while a capsule chamber has no shear ceiling and is "
            "bounded by %g kPa (capsule.escape_pressure_kpa x control.pressure_margin); "
            "one board cannot serve both"
            % (culture.ceiling_dyn_cm2, capsule.pressure_ceiling_kpa),
            ("shear.culture_ceiling_dyn_cm2", "capsule.escape_pressure_kpa",
             "control.pressure_margin"))
    return None


def feasible(ops: Sequence[Op], scale: Optional[str] = None) -> Feasibility:
    """Can these ops share one chip? If so, at which envelope?

    `scale` pins the envelope; leave it None to take the cheapest one that works.
    """
    on_chip = [op for op in ops if op.on_chip]
    if not on_chip:
        return Feasibility(False, None, [Violation(
            "placement", "missing_part",
            "none of these ops is placed on_chip, so there is no chip to build",
            ("unitops.json placement.where",))])

    result = Feasibility(True)

    # 1. placement
    stray = [op for op in ops if not op.on_chip]
    if stray:
        return Feasibility(False, None, [Violation(
            "placement", "missing_part",
            "%s is %s, so it cannot be on a chip with the others"
            % (stray[0].label(), stray[0].where),
            ("unitops.json placement.where",))])

    scales = [scale] if scale else list(area.SCALES)
    attempts: List[Tuple[str, List[Violation], ports.PortPlan, area.AreaEstimate]] = []

    for candidate in scales:
        violations: List[Violation] = []

        # 2. temperature and material
        for verdict in thermal.check(on_chip, candidate):
            violations.append(Violation(
                "temperature" if "material" not in verdict.why else "material",
                "temperature", verdict.why, verdict.keys))

        # 3. chemistry
        for group_a, group_b, why in chemistry.hard_conflicts(on_chip):
            violations.append(Violation(
                "contamination", "contamination",
                "%s and %s cannot share this chip: %s" % (group_a, group_b, why),
                ("contracts/parameters.json lysis.gitc_molar", chemistry.SOURCE)))

        plan = ports.plan(on_chip, candidate)

        # 4. ports
        limit = ports.limit_for(candidate)
        if limit is not None and plan.count > limit:
            violations.append(Violation(
                "ports", "ports",
                "%d ports needed (%s) but fabrication_floors.max_ports_per_chip allows "
                "%d at %s scale, which is how many %g mm holes fit at the %g mm "
                "min_port_pitch_mm" % (plan.count, plan.tally(), limit, candidate,
                                       contracts.floor("min_port_mm"),
                                       contracts.floor("min_port_pitch_mm")),
                ("fabrication_floors.max_ports_per_chip",
                 "fabrication_floors.min_port_pitch_mm")))

        # 5. area
        estimate = area.estimate(on_chip, plan.count, candidate)
        if not estimate.fits:
            violations.append(Violation(
                "area", "area", estimate.why_not(),
                ("blocks.json envelope.max_x_mm", "blocks.json target_envelope.x_mm",
                 "blocks.json envelope.max_depth_mm")))

        # 6. shear
        shear = _shear_violation(on_chip)
        if shear is not None:
            violations.append(shear)

        # 7. planarity
        routing = ports.routable(plan)
        if not routing.planar:
            violations.append(Violation(
                "planarity", "other",
                "this network cannot be cut in ONE fluidic layer with the ports on the "
                "rim: %s (blocks.json excluded.multilayer - no via, no crossing)"
                % routing.reason,
                ("blocks.json excluded.multilayer", "blocks.json envelope.layers")))

        attempts.append((candidate, violations, plan, estimate))
        if not violations:
            result = Feasibility(True, candidate, [], plan, estimate,
                                 thermal.choose_material(on_chip))
            result.gaps = list(plan.gaps)
            result.notes = _notes(on_chip, plan, estimate, result.material)
            _record_area_gaps(result, on_chip, plan, estimate)
            return result

    # Nothing worked: report the attempt that got closest, so the cut names one reason.
    attempts.sort(key=lambda item: (len(item[1]), area.SCALES.index(item[0])
                                    if item[0] in area.SCALES else 99))
    candidate, violations, plan, estimate = attempts[0]
    failed = Feasibility(False, candidate, violations, plan, estimate,
                         thermal.choose_material(on_chip))
    failed.gaps = list(plan.gaps)
    return failed


def _notes(ops: Sequence[Op], plan: ports.PortPlan, estimate: area.AreaEstimate,
           material: Optional[thermal.MaterialChoice]) -> List[str]:
    out = [plan.tally(), estimate.why()]
    if material is not None and material.material_id:
        out.append("material %s: %s" % (material.name, material.why))
    reasons = chemistry.separation_reasons(ops)
    out.extend(reasons)
    return out


def _record_area_gaps(result: Feasibility, ops: Sequence[Op], plan: ports.PortPlan,
                      estimate: area.AreaEstimate) -> None:
    for number in estimate.unknown_loads:
        result.gaps.append({
            "op": int(number), "field": "load",
            "question": "op %d carries no `load`, so what its place has to hold is "
                        "unknown and this chip's area_mm2 is a floor rather than a size. "
                        "The paper's fluid.volume_ul is traceability only and was not "
                        "used (assay2chip/README.md, 'Bench volumes do not transfer')."
                        % number,
            "assumed_from": "contracts/parameters.json capsule_channel.width_factor_low",
        })
    for item in estimate.parts:
        if item.known or not item.op_numbers or "holds nothing" in item.driver:
            continue
        result.gaps.append({
            "op": int(item.op_numbers[0]), "field": "load",
            "question": "ops %s cannot be sized: %s"
                        % (", ".join(str(n) for n in item.op_numbers), item.driver),
        })


def why_together(ops: Sequence[Op], verdict: Feasibility) -> str:
    """The chip's `reason`: why these ops are on one board, in one sentence."""
    kinds = []
    for op in ops:
        if op.kind not in kinds:
            kinds.append(op.kind)
    span = thermal_span(ops)
    bits = ["%s share one board" % ", ".join(kinds)]
    if span:
        bits.append("they run between %g and %g C" % span)
    if verdict.port_plan is not None:
        limit = ports.limit_for(verdict.scale or "demo")
        if limit is not None:
            bits.append("%d of the %d ports allowed at %s scale"
                        % (verdict.port_plan.count, limit, verdict.scale))
        else:
            bits.append("%d ports, which the target envelope does not cap"
                        % verdict.port_plan.count)
    if verdict.area is not None:
        bits.append("%.0f%% of the envelope" % (100 * verdict.area.utilisation))
    return "; ".join(bits)


def thermal_span(ops: Sequence[Op]) -> Optional[Tuple[float, float]]:
    temps = [op.temp_c for op in ops if op.temp_c is not None]
    if not temps:
        return None
    return (min(temps), max(temps))

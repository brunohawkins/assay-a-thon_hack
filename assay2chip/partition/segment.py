"""Where to cut the assay: deterministic greedy segmentation, then local improvement.

The objective is to minimise **non-automated handoffs first and chips second**, subject to
every constraint in ``partition/constraints.py``. That order is deliberate: the stated
goal of the project is no human intervention, and two boards joined by a selector valve
are worth more than one board that someone has to pipette into.

    objective = (handoffs needing a human, chips, chips at target scale, total ports)

compared left to right, lower better. Target scale comes third because the contract
prefers what can be printed today ("DEMO 30 x 20 mm: ... what we can print today"), but
not at the price of an extra board.

The algorithm:

1. **Forced breaks first.** Ops that are not ``on_chip`` cannot be on a board, so every
   one of them splits the sequence. ``off_chip`` ops are the natural cut points the README
   describes; ``missing_part`` ops are cuts too, and ones that need saying out loud.
   A ``transfer`` op is also a natural boundary - the assay itself asked for the move - so
   a cut there is preferred over a cut one op either side of it.

2. **Greedy forward pass.** Walk the ops in flow order, keep adding to the current chip
   while ``constraints.feasible`` says the group can share a board at some envelope, and
   cut as soon as it cannot. The constraint that failed becomes the cut's `why`.

3. **Local improvement**, bounded and deterministic. Three moves, tried in a fixed order
   and applied only when the objective strictly improves:
   ``merge`` (drop a break: two chips become one, often at target scale),
   ``shift`` (move a break one op earlier or later, which fixes the greedy pass's habit of
   stuffing the first chip until it bursts), and
   ``slide to a transfer`` (move a break onto an adjacent transfer op).
   Ties never flip a decision, so the output of two identical runs is identical.

Greedy alone is not enough and the reason is worth stating: a forward pass fills chip 1
until a constraint breaks, which can leave chip 2 holding one op and chip 3 holding eight.
The shift move redistributes that, and because every candidate is re-checked against the
full constraint set, no improvement can smuggle in an infeasible chip.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from assay2chip.partition import area, constraints, handoffs as handoff_mod
from assay2chip.partition.ops import Op, refuse_pending, unknown_blocks

#: Hard cap on improvement rounds. Each round either improves the objective or stops, so
#: this is a belt-and-braces bound, not a tuning knob.
MAX_ROUNDS = 40


@dataclass
class Chip:
    chip_id: str
    ops: List[Op]
    verdict: constraints.Feasibility

    @property
    def numbers(self) -> List[int]:
        return [op.number for op in self.ops]


@dataclass
class Plan:
    """The whole S4 answer, before it is turned into partition.json."""

    chips: List[Chip] = field(default_factory=list)
    handoffs: List[handoff_mod.Handoff] = field(default_factory=list)
    cuts: List[Dict[str, object]] = field(default_factory=list)
    gaps: List[Dict[str, object]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def manual_handoffs(self) -> int:
        return sum(1 for h in self.handoffs if not h.automated)

    @property
    def target_chips(self) -> int:
        return sum(1 for c in self.chips if c.verdict.scale == "target")

    @property
    def total_ports(self) -> int:
        return sum(c.verdict.port_count for c in self.chips)

    @property
    def infeasible(self) -> List[Chip]:
        return [c for c in self.chips if not c.verdict.ok]

    @property
    def unsatisfied_ops(self) -> int:
        """Ops sitting on a board no envelope satisfies.

        Counted in ops rather than in chips on purpose: merging four broken chips into
        one broken chip would otherwise look like an improvement, and the only honest
        reading is that the same four ops are still unbuildable.
        """
        return sum(len(chip.ops) for chip in self.infeasible)

    def objective(self) -> Tuple[int, int, int, int, int]:
        """Lower is better, compared left to right.

        1. ops on a board no envelope satisfies - nothing else matters while one exists;
        2. handoffs needing a human - the project's stated goal is no intervention;
        3. boards that need the target envelope - demo is "what we can print today"
           (blocks.json envelope._three_scales), and the first end-to-end run failed at
           S6 on target-scale chips, so a plan that is buildable now beats a smaller one
           that is not;
        4. the number of boards;
        5. total ports, as a tie-break towards simpler chips.

        3 above 4 is the one ordering worth arguing about. It means S4 will not trade two
        printable boards for one ANSI/SLAS board, but it *will* trade a board to remove a
        manual handoff, because 2 outranks both. Pin `scale` to override.
        """
        return (self.unsatisfied_ops, self.manual_handoffs, self.target_chips,
                len(self.chips), self.total_ports)


# --- the sequence, split into what can and cannot be on a board ---------------

@dataclass
class Sequence_:
    """The op list arranged so breaks can be reasoned about by position."""

    all_ops: List[Op]
    on_chip: List[Op]
    #: Non-on_chip ops immediately before on_chip[i]; key len(on_chip) holds the trailing
    #: ones, key 0 the leading ones.
    gaps: Dict[int, List[Op]]
    forced: Set[int]

    @property
    def leading(self) -> List[Op]:
        return self.gaps.get(0, [])

    @property
    def trailing(self) -> List[Op]:
        return self.gaps.get(len(self.on_chip), [])

    def next_op_number(self, position: int) -> int:
        """The op number immediately after on_chip[position - 1] in the FULL sequence.

        Used for a cut's `between`, which the schema defines as two *consecutive* op
        numbers - so an off-chip op in the gap is the right-hand side, not the next chip's
        first op.
        """
        previous = self.on_chip[position - 1]
        index = self.all_ops.index(previous)
        if index + 1 < len(self.all_ops):
            return self.all_ops[index + 1].number
        return previous.number + 1


def arrange(ops: Sequence[Op]) -> Sequence_:
    all_ops = list(ops)
    on_chip = [op for op in all_ops if op.on_chip]
    gaps: Dict[int, List[Op]] = {}
    position = 0
    for op in all_ops:
        if op.on_chip:
            position += 1
            continue
        gaps.setdefault(position, []).append(op)
    forced = {p for p in gaps if 0 < p < len(on_chip)}
    return Sequence_(all_ops, on_chip, gaps, forced)


# --- building a plan from a set of breaks -------------------------------------

def segments_for(seq: Sequence_, breaks: Set[int]) -> List[List[Op]]:
    points = sorted({0, len(seq.on_chip)} | {b for b in breaks if 0 < b < len(seq.on_chip)})
    return [seq.on_chip[a:b] for a, b in zip(points, points[1:]) if b > a]


def build(assay_id: str, seq: Sequence_, breaks: Set[int],
          scale: Optional[str] = None) -> Plan:
    """Turn a set of break positions into a full plan, constraints and handoffs included.

    `scale` pins every chip to one envelope. Leave it None to let each chip take the
    cheapest envelope it fits, which is what a real run does; pin it to "demo" to ask the
    question the repo's hand-built chain answers, since every chip in chip/ is 30 x 20 mm.
    """
    plan = Plan()
    groups = segments_for(seq, breaks)
    if not groups:
        return plan

    for index, group in enumerate(groups, 1):
        verdict = constraints.feasible(group, scale)
        if not verdict.ok:
            # Keep the chip: every on_chip op must appear on exactly one chip. The
            # violations travel with it so the report can say what is wrong.
            verdict.notes = ["UNSATISFIED: " + v.why for v in verdict.violations]
        chip = Chip("%s_c%d" % (assay_id, index), list(group), verdict)
        plan.chips.append(chip)
        plan.gaps.extend(dict(gap, chip_id=chip.chip_id) for gap in verdict.gaps)

    positions = sorted({0, len(seq.on_chip)} | {b for b in breaks
                                                if 0 < b < len(seq.on_chip)})
    boundaries = positions[1:-1]

    for index, position in enumerate(boundaries):
        left, right = plan.chips[index], plan.chips[index + 1]
        gap_ops = seq.gaps.get(position, [])
        handoff = handoff_mod.build(
            left.chip_id, _outlet_of(left), right.chip_id, _inlet_of(right),
            left.ops, right.ops, gap_ops)
        plan.handoffs.append(handoff)
        plan.gaps.extend(dict(gap, chip_id=left.chip_id) for gap in handoff.gaps)
        plan.cuts.append(_cut(seq, position, left, right, gap_ops, scale))

    # Off-chip work before the first chip and after the last one is still a handoff: it
    # is still something a human probably does, and the headline count has to see it.
    if seq.leading:
        first = plan.chips[0]
        handoff = handoff_mod.build(
            "%s_bench" % assay_id, "bench_out", first.chip_id, _inlet_of(first),
            seq.leading, first.ops, seq.leading)
        handoff.data["note"] = ("%s is not a chip: it is the bench or an instrument "
                                "before the first board. %s"
                                % ("%s_bench" % assay_id, handoff.data.get("note", "")))
        plan.handoffs.insert(0, handoff)
        plan.gaps.extend(dict(gap, chip_id=first.chip_id) for gap in handoff.gaps)
    if seq.trailing:
        last = plan.chips[-1]
        handoff = handoff_mod.build(
            last.chip_id, _outlet_of(last), "%s_bench" % assay_id, "bench_in",
            last.ops, seq.trailing, seq.trailing)
        handoff.data["note"] = ("%s is not a chip: it is the bench or an instrument "
                                "after the last board. %s"
                                % ("%s_bench" % assay_id, handoff.data.get("note", "")))
        plan.handoffs.append(handoff)
        plan.gaps.extend(dict(gap, chip_id=last.chip_id) for gap in handoff.gaps)

    return plan


def _outlet_of(chip: Chip) -> str:
    plan = chip.verdict.port_plan
    port = plan.outlet if plan else None
    return port.id if port else "outlet_1"


def _inlet_of(chip: Chip) -> str:
    plan = chip.verdict.port_plan
    port = plan.inlet if plan else None
    return port.id if port else "inlet_1"


def _cut(seq: Sequence_, position: int, left: Chip, right: Chip,
         gap_ops: Sequence[Op], scale: Optional[str] = None) -> Dict[str, object]:
    """Why the sequence was cut here. Never 'because the algorithm said so'."""
    between = [left.numbers[-1], seq.next_op_number(position)]

    if gap_ops:
        missing = [op for op in gap_ops if op.where == "missing_part"]
        if missing:
            return {
                "between": between,
                "why": "%s cannot be built: %s, so the assay has to leave the board here"
                       % (missing[0].label(), missing[0].raw.get("placement", {})
                          .get("need", "no block does it")),
                "forced_by": "missing_part",
                "note": "constraint: placement; %s" % "; ".join(
                    "%s is %s (%s)" % (op.label(), op.where, op.why) for op in gap_ops),
            }
        return {
            "between": between,
            "why": "%s is off_chip (%s), which is a natural cut point"
                   % (gap_ops[0].label(), gap_ops[0].why or "no reason given"),
            "forced_by": "other",
            "note": "constraint: placement; off-chip ops in this gap: %s" % "; ".join(
                op.label() for op in gap_ops),
        }

    # A chosen cut: say which constraint the merged chip would have broken, judged at the
    # envelope these two boards are actually built to. Judging it at target instead would
    # report "no constraint" for a cut the demo port budget plainly forced.
    at = scale or left.verdict.scale or area.SCALES[0]
    merged = constraints.feasible(left.ops + right.ops, at)
    if merged.first is not None:
        return merged.first.as_cut(between)
    return {
        "between": between,
        "why": "kept apart to reduce the work each board does; the merged board is "
               "feasible, so this cut is an optimisation rather than a constraint",
        "forced_by": "other",
        "note": "constraint: none; review whether these two chips should be one",
    }


# --- greedy pass --------------------------------------------------------------

def greedy(seq: Sequence_, scale: Optional[str] = None) -> Set[int]:
    """Walk forward, cutting as soon as the current chip cannot take the next op.

    With no scale pinned, the growth test is against the PREFERRED envelope (demo), not
    against whichever envelope happens to work. Testing against target instead would let
    a chip grow until an ANSI/SLAS board was the only thing that could hold it, which is
    how the first end-to-end run ended up with target-scale chips that S6 could not build.
    A segment that still cannot fit demo on its own gets target from `build`, where the
    scale is actually chosen.
    """
    grow_at = scale or area.SCALES[0]
    breaks: Set[int] = set(seq.forced)
    if not seq.on_chip:
        return breaks
    start = 0
    for position in range(1, len(seq.on_chip) + 1):
        if position in seq.forced:
            start = position
            continue
        if position == len(seq.on_chip):
            break
        group = seq.on_chip[start:position + 1]
        if constraints.feasible(group, grow_at).ok:
            continue
        # The group including the next op fails. Cut before it - unless the single op we
        # already have is itself infeasible, in which case cutting cannot help and the op
        # travels alone with its violations recorded.
        breaks.add(position)
        start = position
    return breaks


# --- local improvement --------------------------------------------------------

def _candidates(seq: Sequence_, breaks: Set[int]) -> List[Tuple[str, Set[int]]]:
    """Every single move from here, in a fixed order: merge, then shift, then transfer."""
    movable = sorted(b for b in breaks if b not in seq.forced)
    out: List[Tuple[str, Set[int]]] = []
    for position in movable:
        out.append(("merge at %d" % position, breaks - {position}))
    for position in movable:
        for delta in (-1, 1):
            moved = position + delta
            if moved <= 0 or moved >= len(seq.on_chip) or moved in breaks:
                continue
            out.append(("shift %d to %d" % (position, moved),
                        (breaks - {position}) | {moved}))
    for position in movable:
        for delta in (-1, 1):
            moved = position + delta
            if moved <= 0 or moved >= len(seq.on_chip) or moved in breaks:
                continue
            if seq.on_chip[moved].is_transfer or seq.on_chip[moved - 1].is_transfer:
                out.append(("slide %d onto the transfer at %d" % (position, moved),
                            (breaks - {position}) | {moved}))
    return out


def improve(assay_id: str, seq: Sequence_, breaks: Set[int],
            scale: Optional[str] = None) -> Tuple[Set[int], List[str]]:
    """Hill-climb on the objective. Deterministic, bounded, and never accepts a tie."""
    best = set(breaks)
    best_plan = build(assay_id, seq, best, scale)
    best_score = best_plan.objective()
    log: List[str] = []
    for _ in range(MAX_ROUNDS):
        improved = False
        for label, candidate in _candidates(seq, best):
            plan = build(assay_id, seq, candidate, scale)
            if plan.infeasible and not best_plan.infeasible:
                continue  # never trade a working plan for a broken one
            if plan.infeasible and plan.unsatisfied_ops >= best_plan.unsatisfied_ops:
                continue  # and never merge broken boards just to count fewer of them
            score = plan.objective()
            if score < best_score:
                best, best_plan, best_score = candidate, plan, score
                log.append("%s: objective %s" % (label, list(score)))
                improved = True
                break
        if not improved:
            break
    return best, log


def partition(assay_id: str, ops: Sequence[Op],
              scale: Optional[str] = None) -> Plan:
    """The whole S4 decision: refuse undecided ops, segment, improve, explain.

    `scale` pins every chip to one envelope; None lets each chip take the cheapest it
    fits, preferring demo.
    """
    refuse_pending(ops)
    seq = arrange(ops)
    plan = Plan()
    if not seq.on_chip:
        plan.notes.append(
            "no op is placed on_chip, so there is no chip to build: every step is "
            "off_chip or missing a part")
        return plan

    breaks, log = improve(assay_id, seq, greedy(seq, scale), scale)
    plan = build(assay_id, seq, breaks, scale)
    plan.notes.append(
        "greedy segmentation of the flow-ordered ops with %d forced cut(s) at off-chip "
        "ops, then %d accepted local improvement(s); objective (infeasible, manual "
        "handoffs, chips, target-scale chips, ports) = %s"
        % (len(seq.forced), len(log), list(plan.objective())))
    plan.notes.extend(log)

    for number, part in unknown_blocks(ops):
        plan.gaps.append({
            "op": number, "field": "placement.part",
            "question": "op %d names part %r, which is not a block id in "
                        "contracts/blocks.json (basic or advanced), so nothing can be "
                        "sized for it" % (number, part),
            "assumed_from": "contracts/blocks.json basic + advanced",
        })
    return plan

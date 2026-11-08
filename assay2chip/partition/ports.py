"""How many ports a group of ops needs, and the graph those ports have to be routed in.

``fabrication_floors.max_ports_per_chip`` (6 at demo scale) is the limit that cuts this
assay most often, so the count has to be right rather than conservative-by-guessing. The
model below is derived from what each op physically needs, and it was calibrated against
every chip already in this repo - it reproduces their port counts exactly:

    chip                      model                                            real
    module3_v1_translated     1 sample in, 1 feed, 2 waste, 1 out, 1 backflush  6
    module4_v1                1 sample in, 3 split paths, 1 vent                5
    module5_v1                1 sample in, 2 feeds, 1 waste, 1 out              5
    cell_prep_v1              1 sample in, 2 feeds, 1 out                       4
    spc_demo_v1               1 sample in, 2 feeds, 1 out                       4

The rules:

**Sample inlet** - one, always. Material arrives from the previous chip or from the bench.

**Feeds** - one per *run* that pulls a reagent, not one per op, because consecutive ops on
the same part are one place and share one inlet. That is what module3 does: lysis buffer,
wash buffer and RT mix all enter ``main_trap`` through one ``reagent_in`` with washes
between, while cell_prep's enzyme goes to the culture chamber and its quench joins
downstream at the mixer, which is two places and two ports. An encapsulation run needs
two (oil and shell, with the sample as the core: spc_demo_v1's in_oil, in_shell, in_core).
At **target** scale the contract explicitly lifts this - "ports can sit on an area grid,
so lanes get dedicated feeds, drains and sampling lines instead of sharing off-chip
selectors" (blocks.json ``target_envelope.design_rules_at_this_scale``) - so feeds are
counted per reagent op there, which is more ports and a truer picture of the real device.

**Waste outlets** - one per wetted path that cannot be shared (see
``assay2chip/partition/chemistry.py``). This is where chemistry turns into a port cost,
and it is why module3 has both ``waste`` and ``lysis_waste``.

**Product outlet** - one, for what leaves towards the next chip.

**Split paths** - a split into n fractions needs n-1 extra ports, because each fraction
needs its own way on or off the board. module4_v1 is exactly this: four chamber inlets
for four sublibraries filled one at a time by an off-chip rotary valve.

**Vent** - one if anything is thermocycled (a sealed chamber expands) or encapsulated.

**Backflush** - one if the chip holds particles back and later has to move them on again.
module3 needs it; a chip that only flows through does not.

The graph is then built with one node per run and one node per port, plus a manifold node
when one feed serves several ops, and handed to ``graph.fits_one_layer`` with the ports
as the boundary set. The port nodes being on the boundary is the whole point: a
through-hole has to be reachable from the rim, and blocks.json allows no second layer to
get under the board.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from assay2chip.lib import contracts
from assay2chip.partition import chemistry, graph
from assay2chip.partition.ops import Op, Run, runs_of

#: Fallback fan-out for a `split` when neither the source nor a contract says how many
#: fractions. Recorded in `unresolved`, never silently used.
SPLIT_FALLBACK = 2


@dataclass(frozen=True)
class Port:
    """One through-hole. `id` is what a handoff and later chip/designs/<id>.json use."""

    id: str
    role: str          # inlet | feed | waste | outlet | vent | backflush | split
    why: str
    ops: Tuple[int, ...] = ()


@dataclass
class PortPlan:
    ports: List[Port] = field(default_factory=list)
    runs: List[Run] = field(default_factory=list)
    network: Dict[str, Set[str]] = field(default_factory=dict)
    #: Questions this plan had to leave open, as unresolved entries (without `chip_id`).
    gaps: List[Dict[str, object]] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.ports)

    def by_role(self, role: str) -> List[Port]:
        return [p for p in self.ports if p.role == role]

    @property
    def inlet(self) -> Optional[Port]:
        found = self.by_role("inlet")
        return found[0] if found else None

    @property
    def outlet(self) -> Optional[Port]:
        found = self.by_role("outlet")
        return found[0] if found else None

    @property
    def boundary(self) -> List[str]:
        return [p.id for p in self.ports]

    def tally(self) -> str:
        """'1 sample inlet, 1 feed, 2 waste, 1 outlet, 1 backflush' - for a chip note."""
        order = ("inlet", "feed", "split", "waste", "outlet", "vent", "backflush")
        names = {"inlet": "sample inlet", "feed": "feed", "split": "split path",
                 "waste": "waste outlet", "outlet": "product outlet", "vent": "vent",
                 "backflush": "backflush inlet"}
        bits = []
        for role in order:
            n = len(self.by_role(role))
            if n:
                bits.append("%d %s%s" % (n, names[role], "s" if n > 1 else ""))
        return ", ".join(bits)


def split_fanout(ops: Sequence[Op], split_op: Op) -> Tuple[int, Optional[str]]:
    """How many fractions a split makes, and the contract key it came from.

    ``unitops.json`` has no fan-out field. When the chip also thermocycles, this is the
    sublibrary split before the index PCR and ``parameters.sublibraries.count`` is the
    right number (4, which is what module4_v1 is built for). Otherwise there is nothing
    to read and the caller gets the fallback plus a gap to record.
    """
    if any(op.kind == "thermocycle" for op in ops):
        return int(contracts.param("sublibraries.count")), "parameters.sublibraries.count"
    return SPLIT_FALLBACK, None


#: Blocks that hold a bed back against the flow. A port list needs a backflush inlet only
#: when the particles are stuck - adherent on the surface, or packed behind a retention
#: feature. Cells in free suspension leave with the fluid, which is why cell_prep_v1 has
#: no backflush port while module3_v1_translated and module1_v1 both do.
RETENTION_BLOCKS = ("trap_chamber", "trap_channel", "pillar_array", "pillar_funnel_trap",
                    "lock_and_shift_trap", "metering_trap", "merging_trap", "wells_array",
                    "phase_guide")

#: Shear classes that mean the particles are held rather than carried.
HELD_SHEAR = ("adherent_cells", "capsules")


def _is_held(op: Op) -> bool:
    from assay2chip.partition.ops import RETAINING_KINDS

    if op.kind not in RETAINING_KINDS or not op.has_particles:
        return False
    return op.shear_class in HELD_SHEAR or op.part in RETENTION_BLOCKS


def retains_then_releases(ops: Sequence[Op]) -> Optional[Tuple[Op, Op]]:
    """The (retaining, releasing) pair that makes a backflush port necessary."""
    from assay2chip.partition.ops import RELEASING_KINDS

    hold: Optional[Op] = None
    for op in ops:
        if hold is None and _is_held(op):
            hold = op
            continue
        if hold is not None and op.kind in RELEASING_KINDS and op.has_particles:
            return hold, op
    return None


def plan(ops: Sequence[Op], scale: str = "demo") -> PortPlan:
    """The ports this chip needs, the runs it is made of, and the network between them."""
    on_chip = [op for op in ops if op.on_chip]
    out = PortPlan(runs=runs_of(on_chip))
    if not on_chip:
        return out

    runs = out.runs
    ports: List[Port] = []

    ports.append(Port("inlet_1", "inlet",
                      "the sample arrives here from the previous chip or the bench",
                      (on_chip[0].number,)))

    # --- feeds ---------------------------------------------------------------
    feed_index = 0
    feed_targets: List[Tuple[Port, List[Run]]] = []
    if scale == "demo":
        for run in runs:
            if not run.reagent_ops:
                continue
            for stream in range(max(1, run.streams)):
                feed_index += 1
                port = Port(
                    "feed_%d" % feed_index, "feed",
                    "one reagent line serves %s; distinct reagents share it through an "
                    "off-chip selector, which blocks.json envelope._max_ports says is "
                    "what the demo envelope forces" % run.label(),
                    tuple(op.number for op in run.reagent_ops))
                ports.append(port)
                if stream == 0:
                    feed_targets.append((port, [run]))
                else:
                    feed_targets.append((port, [run]))
    else:
        rule = _target_port_rule()
        for run in runs:
            for op in run.reagent_ops:
                for stream in range(op.streams):
                    feed_index += 1
                    port = Port("feed_%d" % feed_index, "feed",
                                "dedicated feed for %s: %s" % (op.label(), rule),
                                (op.number,))
                    ports.append(port)
                    feed_targets.append((port, [run]))

    # --- split fan-out -------------------------------------------------------
    split_ports: List[Tuple[Port, Run]] = []
    for run in runs:
        for op in run.ops:
            if op.kind != "split":
                continue
            fanout, key = split_fanout(on_chip, op)
            if key is None:
                out.gaps.append({
                    "op": op.number, "field": "split fan-out",
                    "question": "how many fractions does %s split into? The port count "
                                "depends on it." % op.label(),
                    "assumed": SPLIT_FALLBACK,
                    "assumed_from": "assay2chip/partition/ports.py SPLIT_FALLBACK",
                })
            else:
                out.gaps.append({
                    "op": op.number, "field": "split fan-out",
                    "question": "unitops.json does not say how many fractions %s makes; "
                                "the index PCR on this chip implies the sublibrary split."
                                % op.label(),
                    "assumed": fanout, "assumed_from": key,
                })
            for extra in range(max(0, fanout - 1)):
                port = Port("split_%d" % (len(split_ports) + 1), "split",
                            "fraction %d of the %d-way split at %s needs its own path "
                            "on or off the board" % (extra + 2, fanout, op.label()),
                            (op.number,))
                ports.append(port)
                split_ports.append((port, run))

    # --- waste ---------------------------------------------------------------
    waste_ports: List[Tuple[Port, List[int]]] = []
    for index, (group, numbers) in enumerate(chemistry.waste_paths(on_chip), 1):
        port = Port("waste_%d" % index, "waste",
                    "spent %s fluid from ops %s; it may not share a path with the other "
                    "waste on this chip" % (group, ", ".join(str(n) for n in numbers)),
                    tuple(numbers))
        ports.append(port)
        waste_ports.append((port, numbers))

    # --- product outlet ------------------------------------------------------
    ports.append(Port("outlet_1", "outlet",
                      "what leaves for the next chip, from %s" % on_chip[-1].label(),
                      (on_chip[-1].number,)))

    # --- vent ----------------------------------------------------------------
    vent_ops = [op for op in on_chip if op.needs_vent]
    if vent_ops:
        ports.append(Port("vent_1", "vent",
                          "%s needs a gas path: a sealed chamber at %s expands"
                          % (vent_ops[0].label(),
                             "%g C" % vent_ops[0].temp_c if vent_ops[0].temp_c is not None
                             else "temperature"),
                          tuple(op.number for op in vent_ops)))

    # --- backflush -----------------------------------------------------------
    pair = retains_then_releases(on_chip)
    if pair is not None:
        hold, release = pair
        ports.append(Port("backflush_1", "backflush",
                          "%s holds %s back and %s has to move them on again, so the bed "
                          "needs reverse flow" % (hold.label(), hold.particle_kind,
                                                  release.label()),
                          (hold.number, release.number)))

    out.ports = ports

    # --- the network ---------------------------------------------------------
    network: Dict[str, Set[str]] = {run.node: set() for run in runs}
    for first, second in zip(runs, runs[1:]):
        graph.add_edge(network, first.node, second.node)
    graph.add_edge(network, "inlet_1", runs[0].node)
    graph.add_edge(network, "outlet_1", runs[-1].node)

    for port, targets in feed_targets:
        if len(targets) == 1:
            graph.add_edge(network, port.id, targets[0].node)
            continue
        manifold = "manifold_%s" % port.id
        graph.add_edge(network, port.id, manifold)
        for run in targets:
            graph.add_edge(network, manifold, run.node)

    for port, run in split_ports:
        graph.add_edge(network, port.id, run.node)

    by_op = {op.number: run for run in runs for op in run.ops}
    for port, numbers in waste_ports:
        nodes = sorted({by_op[n].node for n in numbers if n in by_op})
        if len(nodes) == 1:
            graph.add_edge(network, port.id, nodes[0])
            continue
        manifold = "manifold_%s" % port.id
        graph.add_edge(network, port.id, manifold)
        for node in nodes:
            graph.add_edge(network, manifold, node)

    for port in ports:
        if port.role in ("vent", "backflush"):
            for number in port.ops:
                if number in by_op:
                    graph.add_edge(network, port.id, by_op[number].node)

    out.network = network
    return out


def _target_port_rule() -> str:
    rules = contracts.block("target_envelope.design_rules_at_this_scale")
    return next((r for r in rules if "port" in r), "the 6-port limit lifts at target scale")


def limit_for(scale: str) -> Optional[int]:
    """The port budget, or None when the contract does not bound it (target and above)."""
    return contracts.max_ports_per_chip(scale)


def routable(plan_: PortPlan) -> graph.PlanarityResult:
    """Can this network be cut in one layer with every port on the rim?"""
    if not plan_.network:
        return graph.PlanarityResult(True, "nothing to route")
    return graph.fits_one_layer(plan_.network, plan_.boundary)

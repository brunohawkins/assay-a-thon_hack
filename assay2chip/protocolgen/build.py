"""unit ops + a built chip -> protocol steps.

One op becomes one or more steps, and nothing numeric about liquid is typed in here:

  VOLUME    a number of chamber volumes of a chamber in the chip spec (`exchange.size`),
            so rescaling the chip and re-running this is all it takes. NEVER the paper's
            tube volume: `fluid.volume_ul` is traceability only (assay2chip/README.md,
            "Bench volumes do not transfer"), and what survives from the paper is the
            reagent-to-sample RATIO, which sets the fraction of the chamber a reagent
            addition replaces. A ratio the paper never gave becomes an open question in
            the step's note, and the step is held to the strict sweep rule meanwhile.
  FLOW      derived from that chamber's real cross-section and the ceiling scoped to what
            it holds (`flows.derive`), at a stated fraction of that ceiling.
  DURATION  volume x washes / flow, plus DURATION_MARGIN, stated in the note - and never
            less than the diffusion soak when the chamber holds capsules or beads, because
            a reagent has to get INSIDE them and a faster pump does not help.
  PORTS     feed, vent and cap on every step, from the chip's own port list, so every
            port has a defined state.
  REAGENT   from the closed generic vocabulary in `reagents.py`, never from the source.

WHICH CHAMBER DOES AN OP RUN IN? In order: `chambers[].from_ops` in the chip spec (the
shape S6 writes, mirroring `steps` in a chip/designs file); then an explicit mapping from
the caller; then, if the chip has exactly one chamber an op can run in, that one; then a
match on chamber type. If that is still ambiguous the generator REFUSES and says which
chambers it could not choose between, because quietly picking one would put the whole
protocol in the wrong place.

Every number the inputs did not provide is recorded as a gap (`Plan.gaps`) with what was
used instead and where it came from. S8 writes those into the stage log and the protocol's
description; none of them is silently defaulted.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from assay2chip.lib import contracts
from assay2chip.stages import s2_normalise

from . import exchange, flows, reagents
from .geometry import Chip, GeometryError

# Slack on every flow step's duration, over the bare volume / flow transfer time: a pump
# ramps, a line has dead volume, and a bed settles. Stated in the note of every step it
# affects. A process choice, not a contract value, so it is named once, here.
DURATION_MARGIN = 1.25

# unitops kind -> protocol operation is NOT restated here. S2 owns that mapping
# (assay2chip/stages/s2_normalise.py KIND_TO_PROTOCOL, checked against both schema enums
# by check_vocabulary()), and a second copy would be the thing this repo keeps getting
# bitten by. S8 reads it, and only decides between the ALTERNATES the mapping already
# authorises - which operation a `collect` is, whether a `merge` fills or joins - from what
# the chip and the plan's state say.
KIND_TO_PROTOCOL = s2_normalise.KIND_TO_PROTOCOL

# Chamber types that can host each op kind, best first. Only used when the chip does not
# say (`from_ops`) and has more than one chamber an op could run in.
CHAMBER_TYPES_FOR_KIND = {
    "load": ("exchange", "culture", "well"),
    "mix": ("mixer", "exchange"),
    "merge": ("junction", "mixer", "exchange"),
    "incubate": ("culture", "exchange", "well"),
    "culture": ("culture", "exchange"),
    "wash": ("exchange", "culture", "cell_wash"),
    "lyse": ("exchange", "culture"),
    "separate": ("exchange", "split"),
    "transfer": ("exchange", "culture"),
    "image": ("qc_window", "exchange"),
    "split": ("split", "exchange"),
    "pool": ("pool", "exchange"),
    "encapsulate": ("nozzle", "junction"),
    "cure": ("exchange", "culture"),
    "thermocycle": ("exchange", "culture", "well"),
    "collect": ("exchange", "culture"),
}

# The only kind that carries adherent cells, and therefore the only one the culture shear
# ceiling applies to (S2's vocabulary note). Everything else is held to the ceiling its own
# `shear_class` names, which for capsule handling is a pressure limit and not shear at all.
ADHERENT_KIND = "culture"

# Generic suffixes carried by every reagent and port name, so they say nothing about which
# dedicated line a reagent belongs on.
_COMMON_TOKENS = frozenset((
    "buffer", "mix", "reagent", "suspension", "medium", "oil", "polymer", "stain",
    "in", "out", "inlet", "outlet", "port", "waste", "to", "from", "line", "1", "2",
))


class BuildError(ValueError):
    """The protocol cannot be derived from what the chip and the ops actually say."""


@dataclass
class Gap:
    """A number the inputs did not give, and what was used instead.

    The same shape as an `unresolved` entry in the stage files, so S8 can hand these
    straight to the report without reshaping them.
    """

    question: str
    op: int = None
    field: str = ""
    assumed: object = None
    assumed_from: str = ""

    def line(self) -> str:
        tail = f" (used {self.assumed!r} from {self.assumed_from})" if self.assumed_from else ""
        return f"op {self.op}: {self.question}{tail}" if self.op else f"{self.question}{tail}"

    def as_unresolved(self) -> dict:
        out = {"question": self.question}
        if self.op:
            out["op"] = self.op
        if self.field:
            out["field"] = self.field
        if self.assumed is not None:
            out["assumed"] = self.assumed
        if self.assumed_from:
            out["assumed_from"] = self.assumed_from
        return out


def _tokens(name: str) -> set:
    return {t for t in str(name).split("_") if t and t not in _COMMON_TOKENS}


def assign(chip: Chip, ops: list, mapping: dict = None) -> dict:
    """{op number: chamber id}: where each on-chip op runs. Refuses to guess."""
    out = {}
    declared = {}
    for chamber_id, chamber in chip.chambers.items():
        for number in chamber.get("from_ops") or []:
            declared[int(number)] = chamber_id
    for op in ops:
        number = int(op["op"])
        if (op.get("placement") or {}).get("where") != "on_chip":
            continue
        if mapping and number in mapping:
            out[number] = mapping[number]
        elif number in declared:
            out[number] = declared[number]
        else:
            out[number] = _match_chamber(chip, op)
        if out[number] not in chip.chambers:
            raise BuildError(
                f"op {number} is mapped to '{out[number]}', which is not a chamber on chip "
                f"{chip.id}"
            )
    return out


def _match_chamber(chip: Chip, op: dict) -> str:
    places = chip.workplaces()
    if not places:
        raise BuildError(
            f"chip {chip.id} has no chamber an op can run in (every chamber is a junction, "
            f"filter or port feature), so no protocol can run on it"
        )
    if len(places) == 1:
        return places[0]
    kind = str(op.get("kind", ""))
    for want in CHAMBER_TYPES_FOR_KIND.get(kind, ()):
        hits = [p for p in places if chip.chambers[p].get("type") == want]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise BuildError(
                f"op {op.get('op')} ({kind}) could run in any of {', '.join(hits)} on chip "
                f"{chip.id} and nothing says which. S6 should write "
                f"\"from_ops\": [{op.get('op')}] on the chamber that does it (the same idea "
                f"as `steps` in a chip/designs file), or pass an explicit mapping."
            )
    listing = ", ".join(f"{p} ({chip.chambers[p].get('type')})" for p in places)
    raise BuildError(
        f"op {op.get('op')} ({kind}) matches no chamber type on chip {chip.id} "
        f"(chambers: {listing}). Either the op belongs off chip, or the chip is missing the "
        f"part that does it, or S6 should write \"from_ops\": [{op.get('op')}] on the chamber "
        f"that does it."
    )


class Plan:
    """Steps under construction, with the chamber state that decides `exchange_kind`."""

    def __init__(self, chip: Chip, assay_id: str = "") -> None:
        self.chip = chip
        self.assay_id = assay_id
        self.steps: list = []
        self.gaps: list = []
        self.filled: set = set()        # chambers that already hold liquid
        self.contents: dict = {}        # chamber -> the reagent currently in it
        self.flows: list = []           # (step number, Flow), for the report
        self.assumptions: set = set()
        self.next_kind: str = ""        # the kind of the op after this one, for reagents

    # --- bookkeeping ----------------------------------------------------------

    def gap(self, **kwargs) -> None:
        self.gaps.append(Gap(**kwargs))

    def add(self, operation: str, note: str, duration_s: float, target: str = None,
            targets: list = None, reagent: str = None, volume_ul: float = None,
            temp_c: float = None, washes: int = None, flow_ul_min: float = None,
            **extra) -> dict:
        """Append one step. Numbering is the position in the list, so it cannot drift."""
        if reagent is not None:
            reagents.check_name(reagent)
        step = {
            "step": len(self.steps) + 1,
            "operation": operation,
            "reagent": reagent,
            "volume_ul": volume_ul,
            "duration_s": duration_s if isinstance(duration_s, int) else round(float(duration_s)),
            "temp_c": temp_c,
            "washes": washes,
            "flow_ul_min": flow_ul_min,
            "note": note,
        }
        if targets:
            step["targets"] = list(targets)
        else:
            step["target"] = target
        for key, value in extra.items():
            if value is not None:
                step[key] = value
        self.steps.append(step)
        return step

    # --- ports ----------------------------------------------------------------

    def port_state(self, feed: list, vent: list) -> dict:
        """Every port not driven or vented is capped: an unlisted port has no defined state."""
        feed, vent = list(feed), list(vent)
        return {"feed": feed, "vent": vent,
                "cap": [p for p in self.chip.port_ids() if p not in feed + vent]}

    def feed_for(self, target: str, purpose: str, reagent: str = "") -> str:
        """The inlet this step is driven from, by the chip's own port roles and names."""
        candidates = self.chip.feeds(target)
        if not candidates:
            raise BuildError(
                f"no inlet port on chip {self.chip.id} reaches chamber '{target}', so nothing "
                f"can be pushed into it: S7 has not routed it, or the chip needs another port"
            )
        if purpose == "lift":
            back = [p for p in candidates
                    if self.chip.port_role(p) == "backflush" or "backflush" in p]
            if back:
                return back[0]
            self.gap(question=(f"chip {self.chip.id} has no port named or marked as a "
                               f"back-flush inlet, so the lift is driven from '{candidates[0]}', "
                               f"which pushes the bed the wrong way through the retention "
                               f"feature"),
                     field="ports.feed", assumed=candidates[0], assumed_from="chip ports")
            return candidates[0]
        if purpose == "load":
            by_role = [p for p in candidates if self.chip.port_role(p) == "load"]
            passable = [p for p in candidates if self.chip.capsule_passable(p)]
            return (by_role or passable or candidates)[0]
        shared = [p for p in candidates if _tokens(reagent) & _tokens(p)]
        by_role = [p for p in candidates if self.chip.port_role(p) == "reagent"]
        others = [p for p in candidates if self.chip.port_role(p) != "load"]
        return (shared or by_role or others or candidates)[0]

    def vent_for(self, target: str, purpose: str, reagent: str = "") -> str:
        """The outlet this step drains to.

        A dedicated line is recognised the way chip authors name one: the waste port
        shares a word with the reagent (a lysis reagent drains to a lysis waste), which is
        what keeps two effluents that must never meet apart.
        """
        candidates = self.chip.drains(target)
        if not candidates:
            raise BuildError(
                f"no outlet port on chip {self.chip.id} can be reached from chamber "
                f"'{target}', so the liquid has nowhere to drain to"
            )
        if purpose in ("release", "lift", "harvest"):
            release = [p for p in candidates if self.chip.port_role(p) == "release"
                       or self.chip.capsule_passable(p)]
            if release:
                return release[0]
            self.gap(question=(f"chip {self.chip.id} has no outlet marked as a release port or "
                               f"as passable by what this step moves, so '{candidates[0]}' is "
                               f"used and the material may not get through it"),
                     field="ports.vent", assumed=candidates[0], assumed_from="chip ports")
            return candidates[0]
        dedicated = [p for p in candidates if _tokens(reagent) & _tokens(p)]
        waste = [p for p in candidates if self.chip.port_role(p) == "waste"]
        return (dedicated or waste or candidates)[0]

    # --- the physics ----------------------------------------------------------

    def flow_for(self, target: str, op: dict, feed: str, vent: str, purpose: str = "forward"):
        shear_class = op.get("shear_class")
        if not shear_class:
            shear_class = "capsules" if (op.get("particles") or {}).get("kind") == "capsules" \
                else "none"
            self.gap(op=op.get("op"), field="shear_class",
                     question=("the op states no shear_class, so the flow could not be held to "
                               "a scoped ceiling by the op itself"),
                     assumed=shear_class, assumed_from="particles.kind")
        particles = str((op.get("particles") or {}).get("kind", "none"))
        flow = flows.derive(self.chip, target, shear_class, feed, vent,
                            temp_c=op.get("temp_c"), particles=particles, purpose=purpose)
        if purpose == "lift" and flow.bound_by != "lift_flow":
            self.gap(op=op.get("op"), field="flow_ul_min",
                     question=(f"the flow that lifts a retained bed ({flows.LIFT_FLOW_KEY}) "
                               f"cannot be reached inside the pressure limit on this geometry, "
                               f"so the step runs at {flow.flow_ul_min:g} uL/min and may not "
                               f"lift the bed"),
                     assumed=flow.flow_ul_min, assumed_from=flows.LIFT_FLOW_KEY)
        self.assumptions.update(flow.assumptions)
        self.flows.append((len(self.steps) + 1, flow))
        return flow

    def seconds(self, volume_ul: float, washes: int, flow_ul_min: float, repeats: int = 1,
                particles: str = "none") -> int:
        """The step's duration: the transfer time plus the margin, or the diffusion soak.

        Whichever is longer. Flushing a capsule bed faster does not get the reagent inside
        the capsules any sooner, so a bed exposure is floored at one soak per wash
        (precedent: protocols/build_sctip_spc.py, whose trap washes take a few minutes
        however fast the pump runs).
        """
        pumped = flows.duration_s(volume_ul, washes, flow_ul_min, DURATION_MARGIN, repeats)
        if particles not in exchange.DIFFUSION_LIMITED:
            return pumped
        soak = int(math.ceil(exchange.interior_soak_s() * max(1, washes) * repeats))
        self.soaked = getattr(self, "soaked", 0) + (1 if soak > pumped else 0)
        return max(pumped, soak)

    def soak_note(self, washes: int, pumped: int, total: int) -> str:
        if total <= pumped:
            return ""
        return (f" The pump would finish in {pumped} s, but a reagent has to diffuse INSIDE "
                f"the retained particles: one exposure needs "
                f"{exchange.interior_soak_s():.0f} s to reach "
                f"{float(contracts.param(exchange.INTERIOR_FRACTION_KEY)):.0%} of the interior "
                f"target ({exchange.INTERIOR_FRACTION_KEY} over "
                f"{exchange.DIFFUSION_TIME_KEY}), so {washes} of them set the floor at "
                f"{total} s and a faster pump does not help")

    def margin_note(self) -> str:
        return (f"Duration is volume x washes / flow plus {DURATION_MARGIN - 1:.0%} for pump "
                f"ramp, line dead volume and settling")

    # --- steps ----------------------------------------------------------------

    def load(self, op: dict, target: str) -> None:
        reagent, gap = reagents.name_for(op, chamber_is_empty=target not in self.filled,
                                        next_kind=self.next_kind)
        if gap:
            self.gap(op=op.get("op"), field="reagent", question=gap)
        # A first fill is ONE CHAMBER VOLUME, from the built chamber. The paper's own
        # volume describes a tube and is kept in unitops.json for traceability only
        # (assay2chip/README.md, "Bench volumes do not transfer"); a load that took it
        # would either overflow the chamber or under-fill it.
        sizing = exchange.size(exchange.FILL, self.chip.volume_ul(target))
        chamber_volume = self.chip.volume_ul(target)
        paper = (op.get("fluid") or {}).get("volume_ul")
        if paper is not None and abs(paper - chamber_volume) > 0.001:
            self.gap(op=op.get("op"), field="fluid.volume_ul",
                     question=(f"the source's own volume for this step describes a tube, not "
                               f"the {chamber_volume:g} uL chamber '{target}' that was built, "
                               f"so it does not set this volume; it stays in unitops.json for "
                               f"traceability"),
                     assumed=chamber_volume,
                     assumed_from=f"chip {self.chip.id} chamber '{target}' volume_ul")
        particles = str((op.get("particles") or {}).get("kind", "none"))
        feed = self.feed_for(target, "load", reagent)
        vent = self.vent_for(target, "load", reagent)
        flow = self.flow_for(target, op, feed, vent)
        retention = self.chip.retention(target)
        held = (f"What is carried stays on the {retention.get('type')} "
                f"({retention.get('opening_um'):g} um) and the carrier leaves through "
                f"'{vent}'. " if retention.get("opening_um") else "")
        pumped = flows.duration_s(sizing.volume_ul, 1, flow.flow_ul_min, DURATION_MARGIN)
        total = self.seconds(sizing.volume_ul, 1, flow.flow_ul_min, particles=particles)
        self.add("load",
                 f"Load {reagent} into '{target}' through '{feed}'. {held}{sizing.note}. "
                 f"{flow.why}. {self.margin_note()}.{self.soak_note(1, pumped, total)}",
                 total, target=target, reagent=reagent, volume_ul=sizing.volume_ul,
                 flow_ul_min=flow.flow_ul_min, ports=self.port_state([feed], [vent]))
        self.filled.add(target)
        self.contents[target] = reagent

    def exchange(self, op: dict, target: str, purpose: str = "forward",
                 direction: str = None, reagent: str = None) -> None:
        if reagent is None:
            reagent, gap = reagents.name_for(op, chamber_is_empty=target not in self.filled,
                                        next_kind=self.next_kind)
            if gap:
                self.gap(op=op.get("op"), field="reagent", question=gap)
        chamber_volume = self.chip.volume_ul(target)
        kind_name = str(op.get("kind"))
        ratio = (op.get("ratio") or {}).get("reagent_to_sample")
        # A lift is one chamber volume of carrier pushed the other way: enough to get behind
        # the bed and move it, and no more. ln(1 + r) = 1 at r = e - 1, so that ratio is how
        # "one chamber volume" is said in the units exchange.size() takes.
        if direction == "backflush":
            ratio = math.e - 1.0
        question = ""
        superseded = ""
        if ratio and direction != "backflush" and self.chip.retention(target):
            # The paper's ratio is reagent to SAMPLE. Here the sample is held on the
            # retention feature and the liquid around it is only carrier, so matching the
            # ratio against that liquid would leave the carrier diluting the reagent. The
            # reagent has to displace the carrier instead.
            superseded = (f"The source's {ratio:g}:1 reagent-to-sample ratio does not set "
                          f"this volume: what is being treated is held on the "
                          f"{self.chip.retention(target).get('type')} and the liquid around "
                          f"it is carrier, not sample, so the reagent has to displace the "
                          f"carrier rather than be mixed into it. ")
            self.gap(op=op.get("op"), field="ratio.reagent_to_sample",
                     question=(f"the source states a {ratio:g}:1 reagent-to-sample ratio, but "
                               f"'{target}' retains what is being treated and the liquid "
                               f"around it is carrier: the step sweeps instead, so the "
                               f"reagent is not diluted by the carrier left behind"),
                     assumed="sweep", assumed_from="chamber retention + exchange.target_fraction")
            ratio = None
        if (direction != "backflush" and target in self.filled
                and kind_name in exchange.RATIO_KINDS and not ratio and not superseded):
            question = exchange.ratio_question(kind_name, chamber_volume)
            self.gap(op=op.get("op"), field="ratio.reagent_to_sample",
                     question=(f"no reagent-to-sample ratio for this '{kind_name}', and the "
                               f"source's absolute volume cannot supply one (it describes a "
                               f"tube): the step is sized as a full sweep, the strict "
                               f"reading, and says so in its note"))
        kind, why = exchange.classify(kind_name, target not in self.filled, ratio,
                                      chamber_volume, direction=direction or "forward")
        sizing = exchange.size(kind, chamber_volume, ratio)
        feed = self.feed_for(target, purpose, reagent)
        vent = self.vent_for(target, purpose, reagent)
        flow = self.flow_for(target, op, feed, vent, purpose="lift" if direction == "backflush"
                             else purpose)
        cleared = (f"It drains to '{vent}', which keeps this effluent off the other waste "
                   f"line. " if _tokens(reagent) & _tokens(vent) else "")
        lift = ("Liquid is pushed back up through the retention feature, which lifts the bed "
                "off it and sweeps it towards the outlet. " if direction == "backflush" else "")
        particles = str((op.get("particles") or {}).get("kind", "none"))
        pumped = flows.duration_s(sizing.volume_ul, sizing.washes, flow.flow_ul_min,
                                  DURATION_MARGIN)
        total = self.seconds(sizing.volume_ul, sizing.washes, flow.flow_ul_min,
                             particles=particles)
        self.add("exchange",
                 f"{_verb(kind, direction)} '{target}' with {reagent} through '{feed}'. "
                 f"{lift}{cleared}{superseded}"
                 f"{sizing.note}. {flow.why}. {self.margin_note()}."
                 f"{self.soak_note(sizing.washes, pumped, total)}"
                 + (f" {question}" if question else ""),
                 total,
                 target=target, reagent=reagent, volume_ul=sizing.volume_ul,
                 washes=sizing.washes, flow_ul_min=flow.flow_ul_min,
                 exchange_kind=sizing.kind, direction=direction, waste=vent,
                 ports=self.port_state([feed], [vent]))
        self.filled.add(target)
        self.contents[target] = reagent

    def hold(self, op: dict, target: str, cycles: bool = False, dwell: bool = False) -> None:
        temp_c = op.get("temp_c")
        duration = op.get("duration_s")
        if cycles:
            self.gap(op=op.get("op"), field="cycles",
                     question=("the contract spells thermocycling as hold_temp with `cycles` "
                               "(count, ramp_c_s, profile), and unitops.json carries none of "
                               "the three: this step is written as ONE isothermal hold, which "
                               "is not thermocycling. The profile has to come from the source"))
        if temp_c is None:
            self.gap(op=op.get("op"), field="temp_c",
                     question=("the op asks for a hold but gives no temperature, so no "
                               "hold_temp step was written: the number has to come from the "
                               "source, not from S8"))
            return
        if not duration:
            self.gap(op=op.get("op"), field="duration_s",
                     question=(f"the op holds '{target}' at {temp_c:g} C but the source never "
                               f"gave a duration, so no hold_temp step was written: a hold with "
                               f"an invented length is worse than a visible gap"))
            return
        reason = self.chip.zone_holds(target, float(temp_c))
        if reason:
            raise BuildError(
                f"op {op.get('op')} holds '{target}' at {temp_c:g} C, but {reason}. A "
                f"temperature step needs a declared thermal zone that can reach it: either "
                f"S6 adds the zone or the op belongs on another chip (an S4 cut)."
            )
        zone = self.chip.thermal_zone(target)
        # The paper's time is kept - unless the reagent has to diffuse into a capsule or a
        # gel, which no pump and no clock can hurry: then the soak wins and says so.
        soak = 0.0
        if _particles(op) in exchange.DIFFUSION_LIMITED:
            soak = exchange.interior_soak_s()
        held = max(float(duration), soak)
        longer = ""
        if held > float(duration):
            longer = (f" The source asks for {_clock(duration)}, but a reagent has to reach "
                      f"the INSIDE of what is retained here: {held:.0f} s is what reaches "
                      f"{float(contracts.param(exchange.INTERIOR_FRACTION_KEY)):.0%} of the "
                      f"interior target ({exchange.INTERIOR_FRACTION_KEY} over "
                      f"{exchange.DIFFUSION_TIME_KEY}), so diffusion sets this time, not the "
                      f"paper.")
        what = ("Dwell so the reagent reaches what is held in the chamber" if dwell
                else f"Hold '{target}' at {temp_c:g} C")
        self.add("hold_temp",
                 f"{what} for {_clock(held)} at {temp_c:g} C with every port capped, in "
                 f"thermal zone '{zone['id']}', which holds "
                 f"{zone['min_temp_c']:g}-{zone['max_temp_c']:g} C. No flow during the "
                 f"hold.{longer}",
                 held, target=target, temp_c=temp_c, ports=self.port_state([], []))

    def culture(self, op: dict, target: str) -> None:
        """A `culture` op: medium changes with stop-flow between them, at temperature.

        The contract's `culture` operation needs `exchange_interval_s`, a FREQUENCY. The
        paper states one as "medium changed every other day", and neither unitops.json nor
        the contracts carry it - and the paper's medium VOLUME cannot stand in for it,
        because that volume describes a flask (assay2chip/README.md, "Bench volumes do not
        transfer"): dividing it by a chamber volume would turn a tube into a schedule.

        So without an interval this takes S2's other authorised spelling of the same op -
        one medium change, then a hold for as long as the paper says - and records the gap.
        The time and the temperature are the paper's either way.
        """
        temp_c, duration = op.get("temp_c"), op.get("duration_s")
        interval = op.get("exchange_interval_s")
        if temp_c is None or not duration:
            self.gap(op=op.get("op"), field="duration_s",
                     question=(f"a culture step needs how long it runs and at what "
                               f"temperature, and the source gives duration={duration!r}, "
                               f"temp={temp_c!r} C, so no culture step was written"))
            return
        reason = self.chip.zone_holds(target, float(temp_c))
        if reason:
            raise BuildError(f"op {op.get('op')} cultures '{target}' at {temp_c:g} C, but {reason}")
        zone = self.chip.thermal_zone(target)
        reagent, gap = reagents.name_for(op, chamber_is_empty=target not in self.filled,
                                        next_kind=self.next_kind)
        if gap:
            self.gap(op=op.get("op"), field="reagent", question=gap)

        if not interval:
            self.gap(op=op.get("op"), field="exchange_interval_s",
                     question=(f"the source gives no medium-change interval, and its medium "
                               f"volume cannot supply one (it describes a flask), so the "
                               f"culture is written the way protocols/ipsc_to_mgl_v1.json "
                               f"spells it - one medium change, then a hold of "
                               f"{_clock(duration)} - instead of a repeating schedule"))
            self.exchange(op, target, purpose="forward")
            self.hold(op, target)
            return

        chamber_volume = self.chip.volume_ul(target)
        feed = self.feed_for(target, "forward", reagent)
        vent = self.vent_for(target, "forward", reagent)
        flow = self.flow_for(target, op, feed, vent)
        self.add("culture",
                 f"Keep '{target}' in culture for {_clock(duration)} at {temp_c:g} C in "
                 f"thermal zone '{zone['id']}' ({zone['min_temp_c']:g}-{zone['max_temp_c']:g} C), "
                 f"with a batch of {chamber_volume:g} uL of {reagent} - one chamber volume, "
                 f"from the built chamber and not from the source's flask volume - every "
                 f"{_clock(interval)} and the flow stopped in between. {flow.why}. "
                 f"{self.margin_note()}.",
                 duration, targets=[target], reagents=[reagent], reagent=reagent,
                 volume_ul=round(chamber_volume, 3), temp_c=temp_c,
                 flow_ul_min=flow.flow_ul_min, exchange_interval_s=float(interval),
                 ports=self.port_state([feed], [vent]))
        self.filled.add(target)
        self.contents[target] = reagent

    def transfer(self, op: dict, source: str, target: str) -> None:
        reagent, _ = reagents.name_for(op, chamber_is_empty=target not in self.filled,
                                        next_kind=self.next_kind)
        feed = self.feed_for(source, "forward", reagent)
        vent = target if target in self.chip.ports else self.vent_for(target, "forward", reagent)
        flow = self.flow_for(source, op, feed, vent, purpose="forward")
        volume = self.chip.volume_ul(source)
        self.add("transfer",
                 f"Carry the contents of '{source}' on to '{target}' through whatever lies "
                 f"between them, driven from '{feed}' and vented at '{vent}'. One "
                 f"{volume:g} uL chamber volume of carrier moves them. {flow.why}. "
                 f"{self.margin_note()}.",
                 self.seconds(volume, 1, flow.flow_ul_min, particles=_particles(op)),
                 target=target, source=source, flow_ul_min=flow.flow_ul_min,
                 ports=self.port_state([feed], [vent]))
        self.filled.add(target)

    def release(self, op: dict, target: str) -> None:
        reagent, _ = reagents.name_for(op, purpose="release")
        retained = bool(self.chip.retention(target))
        purpose = "lift" if retained else "forward"
        if retained:
            # A bed on a retention feature has to be lifted before it can leave: the
            # closed-loop safety rules block a release while capsules are still on the
            # mesh (operator/safety.py rule 6), and S2's mapping says the same.
            self.exchange(op, target, purpose="lift", direction="backflush", reagent=reagent)
        feed = self.feed_for(target, purpose, reagent)
        vent = self.vent_for(target, "release", reagent)
        flow = self.flow_for(target, op, feed, vent, purpose=purpose)
        volume = self.chip.volume_ul(target)
        # A retained bed is carried out by sweeping the chamber twice: one chamber volume
        # fills behind it and the second carries it through the outlet. With nothing
        # retained the material is already in suspension, so the step is a full sweep.
        volumes = 2 if retained else int(math.ceil(exchange.sweep_chamber_volumes()))
        self.add("release",
                 f"Send what '{target}' holds out through '{vent}' as one suspension in "
                 f"{reagent}, driven from '{feed}'. {volumes} chamber volumes "
                 f"({volumes * volume:g} uL) carry it out. {flow.why}. {self.margin_note()}.",
                 self.seconds(volume, volumes, flow.flow_ul_min, particles=_particles(op)),
                 target=target, reagent=reagent, flow_ul_min=flow.flow_ul_min,
                 ports=self.port_state([feed], [vent]))
        self.contents.pop(target, None)

    def collect(self, op: dict, target: str, operation: str) -> None:
        """`collect` (supernatant out, contents stay) or `harvest` (the contents leave)."""
        reagent, _ = reagents.name_for(op, purpose="release" if operation == "harvest" else "")
        feed = self.feed_for(target, "forward", reagent)
        vent = self.vent_for(target, "harvest" if operation == "harvest" else "forward", reagent)
        flow = self.flow_for(target, op, feed, vent)
        volume = round(self.chip.volume_ul(target), 3)
        what = ("the cells, which leave through" if operation == "harvest"
                else "the supernatant, leaving what is retained behind, through")
        self.add(operation,
                 f"Take {what} '{vent}' out of '{target}', driven from '{feed}'. One "
                 f"{volume:g} uL chamber volume. {flow.why}. {self.margin_note()}.",
                 self.seconds(volume, 1, flow.flow_ul_min, particles=_particles(op)),
                 targets=[target], reagent=reagent if operation == "harvest" else None,
                 volume_ul=volume, flow_ul_min=flow.flow_ul_min,
                 ports=self.port_state([feed], [vent]))

    def count(self, op: dict, target: str) -> None:
        if self.chip.chambers[target].get("type") != "qc_window":
            self.gap(op=op.get("op"), field="placement",
                     question=(f"the contract's only readout operation is `count` at a QC "
                               f"window, and '{target}' is a "
                               f"{self.chip.chambers[target].get('type')}: this imaging op "
                               f"belongs off chip (chip/designs rule 6) and no step was "
                               f"written for it"))
            return
        duration = op.get("duration_s")
        if not duration:
            self.gap(op=op.get("op"), field="duration_s",
                     question="the op gives no imaging time, so the count step reads 0 s",
                     assumed=0, assumed_from="no source")
            duration = 0
        self.add("count",
                 f"Stop the flow and read '{target}': concentration and viability, with every "
                 f"port capped so nothing moves while it is read.",
                 duration, target=target, ports=self.port_state([], []))

    def mix(self, op: dict, target: str) -> None:
        reagent, gap = reagents.name_for(op, chamber_is_empty=target not in self.filled,
                                        next_kind=self.next_kind)
        if gap:
            self.gap(op=op.get("op"), field="reagent", question=gap)
        # One mixer volume of mixture passes, from the built mixer. What survives from the
        # paper is the reagent-to-sample RATIO, which is what the two pump rates are set
        # from; its absolute volumes describe a tube and do not transfer.
        chamber_volume = self.chip.volume_ul(target)
        volume = round(chamber_volume, 3)
        ratio = (op.get("ratio") or {}).get("reagent_to_sample")
        if ratio:
            share = (f"The two feeds run at {ratio:g} parts {reagent} to 1 part sample, which "
                     f"is the ratio the source states, so the mixture leaves at the source's "
                     f"final concentration whatever this mixer holds. ")
        else:
            share = f"{exchange.ratio_question(str(op.get('kind')), chamber_volume)}. "
            self.gap(op=op.get("op"), field="ratio.reagent_to_sample",
                     question=(f"no reagent-to-sample ratio for this co-flow, so the two pump "
                               f"rates cannot be set from the source; the step co-flows equal "
                               f"parts and says so in its note"))
        feed = self.feed_for(target, "forward", reagent)
        vent = self.vent_for(target, "forward", reagent)
        flow = self.flow_for(target, op, feed, vent)
        particles = str((op.get("particles") or {}).get("kind", "none"))
        self.add("mix",
                 f"Co-flow {reagent} into the stream in '{target}' through '{feed}', draining "
                 f"to '{vent}'. {volume:g} uL is one chamber volume of the mixer; the folded "
                 f"path is there because diffusion across the channel is too slow at this "
                 f"scale. {share}{flow.why}. {self.margin_note()}.",
                 self.seconds(volume, 1, flow.flow_ul_min, particles=particles),
                 target=target, reagent=reagent, volume_ul=volume,
                 flow_ul_min=flow.flow_ul_min, temp_c=op.get("temp_c"),
                 ports=self.port_state([feed], [vent]))
        self.filled.add(target)

    def split(self, op: dict, target: str) -> None:
        self._fan(op, target, "split",
                  f"Share what '{target}' holds between the chambers its channels lead to; the "
                  f"operator follows the chip's own channels to find them.")

    def pool(self, op: dict, target: str) -> None:
        self._fan(op, target, "pool",
                  f"Bring together into '{target}' what arrives from every chamber its channels "
                  f"lead from.")

    def _fan(self, op: dict, target: str, operation: str, lead: str) -> None:
        """A split or a pool: one chamber volume moves, at the route's derived flow."""
        reagent, _ = reagents.name_for(op, chamber_is_empty=target not in self.filled,
                                        next_kind=self.next_kind)
        feed = self.feed_for(target, "forward", reagent)
        vent = self.vent_for(target, "forward", reagent)
        flow = self.flow_for(target, op, feed, vent)
        volume = round(self.chip.volume_ul(target), 3)
        self.add(operation,
                 f"{lead} One {volume:g} uL chamber volume moves, driven from '{feed}' and "
                 f"vented at '{vent}'. {flow.why}. {self.margin_note()}.",
                 self.seconds(volume, 1, flow.flow_ul_min, particles=_particles(op)),
                 target=target, flow_ul_min=flow.flow_ul_min,
                 ports=self.port_state([feed], [vent]))

    def encapsulate(self, op: dict, target: str) -> None:
        enc = self.chip.spec.get("encapsulation") or {}
        needed = ("core_flow_ul_h", "shell_flow_ul_h", "oil_flow_ul_h")
        if not all(enc.get(k) for k in needed):
            raise BuildError(
                f"op {op.get('op')} makes capsules at '{target}', but chip {self.chip.id} "
                f"declares no encapsulation {', '.join(k for k in needed if not enc.get(k))}: "
                f"the three stream flows set the capsule size, so S8 will not invent them"
            )
        count = (op.get("particles") or {}).get("count") or \
            (self.chip.spec.get("capsule") or {}).get("count")
        if not count:
            raise BuildError(
                f"op {op.get('op')} makes capsules at '{target}' but neither the op nor chip "
                f"{self.chip.id} says how many"
            )
        flows_ul_h = {"core": float(enc["core_flow_ul_h"]), "shell": float(enc["shell_flow_ul_h"]),
                      "oil": float(enc["oil_flow_ul_h"])}
        total = sum(flows_ul_h.values())
        feed = self.feed_for(target, "forward", "shell_polymer")
        vent = self.vent_for(target, "forward", "shell_polymer")
        duration = int(math.ceil(op.get("duration_s") or 0))
        if not duration:
            raise BuildError(
                f"op {op.get('op')} makes {count} capsules at '{target}' but gives no "
                f"duration, and S8 will not invent one: operator/sizing.py turns the three "
                f"stream flows into capsules per hour, so the number belongs in S2"
            )
        self.add("encapsulate",
                 f"Co-flow the core and shell streams into '{target}' and pinch off droplets "
                 f"with the oil stream: {flows_ul_h['core']:g}, {flows_ul_h['shell']:g} and "
                 f"{flows_ul_h['oil']:g} uL/h ({total:g} uL/h in total), from the chip's own "
                 f"encapsulation block, for {count} capsules.",
                 duration, target=target, reagent="shell_polymer",
                 flows_ul_h=flows_ul_h, capsules=int(count),
                 ports=self.port_state([feed], [vent]))
        self.filled.add(target)

    def cure(self, op: dict, target: str) -> None:
        wavelength = self.chip.curing_wavelength_nm(target)
        if not wavelength:
            raise BuildError(
                f"op {op.get('op')} cures what is in '{target}', but chip {self.chip.id} "
                f"declares no curing window over it with a wavelength_nm"
            )
        duration = op.get("duration_s")
        if not duration:
            raise BuildError(
                f"op {op.get('op')} cures what is in '{target}' but the source never gave an "
                f"exposure time, and an invented cure time is a dose"
            )
        self.add("cure",
                 f"Cure what '{target}' holds through its curing window at {wavelength:g} nm, "
                 f"with every port capped so nothing moves during the exposure.",
                 duration, target=target, wavelength_nm=wavelength,
                 ports=self.port_state([], []))


def _particles(op: dict) -> str:
    """What this op carries, which decides whether diffusion sets the duration floor."""
    return str((op.get("particles") or {}).get("kind", "none"))


def _verb(kind: str, direction: str = None) -> str:
    if direction == "backflush":
        return "Back-flush"
    return {exchange.FILL: "Fill", exchange.SWEEP: "Sweep",
            exchange.PARTIAL: "Partly change"}[kind]


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    if seconds >= 3600:
        return f"{seconds / 3600:g} h"
    if seconds >= 60:
        return f"{seconds / 60:g} min"
    return f"{seconds} s"


def mapping_for(kind: str):
    """S2's ProtocolMapping for a kind. Raises if the kind is not in the vocabulary."""
    if kind not in KIND_TO_PROTOCOL:
        raise BuildError(
            f"kind {kind!r} is not in S2's vocabulary "
            f"({', '.join(s2_normalise.KINDS)}), so there is no mapping onto the protocol "
            f"contract for it. Add it in assay2chip/stages/s2_normalise.py, where "
            f"check_vocabulary() checks it against both schemas - not here."
        )
    return KIND_TO_PROTOCOL[kind]


def operation_for(plan: Plan, op: dict, target: str) -> str:
    """Which of the operations S2's mapping authorises this op becomes.

    Only ever the mapping's `operation` or one of its `alternates`, and the choice comes
    from the chip and the plan's state rather than from prose:

      collect   release when a retained bed must be freed, harvest when cells leave,
                collect when only the liquid phase leaves (the mapping's three).
      merge     load when the target is being filled, mix when it joins a flowing stream.
      load      a chamber that already holds liquid cannot be 'loaded' - a load has no
                washes and no residual-liquid rule - so it becomes the mapping's nearest
                authorised spelling, an exchange, and the substitution is recorded.
      culture   culture when the medium and the duration are both known, so
                exchange_interval_s can be derived from them; otherwise the mapping's
                alternate spelling is unreachable too and the gap is recorded.
    """
    kind = str(op.get("kind", ""))
    mapping = mapping_for(kind)
    particles = str((op.get("particles") or {}).get("kind", "none"))
    if kind == "collect":
        if particles == "capsules" or plan.chip.retention(target):
            return "release"
        return "harvest" if particles == "cells" else "collect"
    if kind == "merge":
        return "load" if target not in plan.filled else "mix"
    if kind == "load" and target in plan.filled:
        plan.gap(op=op.get("op"), field="kind",
                 question=(f"'{kind}' maps to the contract's load, but '{target}' already "
                           f"holds liquid and a load states no washes and no residual "
                           f"fraction: the step is written as an exchange instead, so the "
                           f"exchange_kind rule applies to it"),
                 assumed="exchange", assumed_from="s2_normalise.KIND_TO_PROTOCOL alternates")
        return "exchange"
    return mapping.operation


def steps_for_op(plan: Plan, op: dict, target: str, previous: str = None,
                 next_kind: str = "") -> None:
    """Append every step one op needs, in order.

    `next_kind` is the kind of the op that follows, which is how a reagent delivered into
    an already-full chamber gets a generic name: what it is for is said by what happens to
    it next (see reagents.name_for).
    """
    kind = str(op.get("kind", ""))
    plan.next_kind = next_kind
    operation = operation_for(plan, op, target)
    if kind != ADHERENT_KIND and op.get("shear_class") == "adherent_cells":
        plan.gap(op=op.get("op"), field="shear_class",
                 question=(f"'{kind}' claims the adherent-cell shear class, but '"
                           f"{ADHERENT_KIND}' is the only kind that carries adherent cells; "
                           f"the 0.01 dyn/cm2 ceiling is honoured here because it is the "
                           f"stricter reading, which makes this step slow"))
    if previous and previous != target and previous in plan.chip.chambers \
            and plan.chip.reaches(previous, target) and kind != "transfer":
        plan.transfer(op, previous, target)

    if operation == "load":
        plan.load(op, target)
    elif operation == "exchange":
        plan.exchange(op, target)
    elif operation == "hold_temp":
        plan.hold(op, target, cycles=(kind == "thermocycle"))
    elif operation == "culture":
        plan.culture(op, target)
    elif operation == "mix":
        plan.mix(op, target)
    elif operation == "transfer":
        plan.transfer(op, previous or target, target)
    elif operation == "release":
        plan.release(op, target)
    elif operation in ("collect", "harvest"):
        plan.collect(op, target, operation)
    elif operation == "count":
        plan.count(op, target)
    elif operation == "split":
        plan.split(op, target)
    elif operation == "pool":
        plan.pool(op, target)
    elif operation == "encapsulate":
        plan.encapsulate(op, target)
    elif operation == "cure":
        plan.cure(op, target)
    else:
        raise BuildError(
            f"op {op.get('op')} ({kind}) maps to protocol operation {operation!r}, which "
            f"protocolgen does not build yet"
        )
    if kind == "lyse" and op.get("temp_c") is not None and op.get("duration_s"):
        # S2's mapping marks lyse as exchange + hold_temp: deliver the reagent, then dwell.
        plan.hold(op, target, dwell=True)


def finish(plan: Plan) -> None:
    """Close the plan out: if the last op left material in a chamber with a release port
    and nothing took it out, say so as a gap rather than leave the chip loaded."""
    if not plan.contents:
        return
    for chamber in sorted(plan.contents):
        drains = [p for p in plan.chip.drains(chamber)
                  if plan.chip.port_role(p) == "release" or plan.chip.capsule_passable(p)]
        if drains:
            plan.gap(question=(f"the last op leaves material in '{chamber}' and no op takes it "
                               f"out through '{drains[0]}': the chip ends the protocol loaded, "
                               f"which is a gap in the assay, not in the chip"),
                     field="ops")

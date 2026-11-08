"""S10 CFD: turn a protocol (+ its chip) into one Scenario per step.

A Scenario is the boundary condition set the solver needs: what every port on the chip
does for the duration of one protocol step, which fluid is in it, and at what temperature.
`solve.py` turns a `Mesh` + a `Scenario` into a `Field`; this module is the only place that
reads `protocols/<id>.json` and decides what the step *means* hydraulically.

    from assay2chip.cfd import scenario

    run = scenario.build_protocol("protocols/module3_v1.json", "chip/module3_v1_translated.json")
    for rec in run.scenarios:          # one per step, plus the worst cases
        rec.scenario                   # the Scenario the solver wants
        rec.cites                      # "protocols/module3_v1.json step 4 (exchange main_trap)"
    run.unmapped                       # every step we could NOT map, with the reason
    run.unresolved                     # contract gaps and assumptions this run rests on

    chain = scenario.build_pipeline("protocols/pipeline_mvp.json")   # or a partition.json

PORT RULES (contracts/schemas/protocol.schema.json `ports`)

    ports.feed   ->  {"type": "flow", "q_ul_min": ...}   driven at the step's flow_ul_min
    ports.vent   ->  {"type": "pressure", "p_pa": 0.0}   open to atmosphere, the drain
    ports.cap    ->  {"type": "closed"}                  the only true block
    on the chip but in none of the three  ->  closed, flagged `port_not_stated`

Several feeds and ONE `flow_ul_min` is ambiguous and is never guessed silently. An
`encapsulate` step is exact, because `flows_ul_h` names the core, shell and oil streams and
they map onto the feed ports by name. Anything else gets a stated policy - per-feed full
flow when the note says the lanes run in parallel AND the same rate appears on a one-feed
step of the same chip, otherwise an equal split - and carries `ambiguous=True` with the
alternative readings, so the solver and the report can see the fork. The worst-case
scenarios then drive each feed alone at full rate, which brackets the answer either way.

NO-FLOW STEPS ARE KEPT. A `hold_temp` or a `culture` dwell with everything capped moves no
liquid, so there is nothing to solve - but bubble growth, evaporation, sedimentation and
thermal expansion all happen during exactly those steps, and they are usually the longest
steps in the protocol (73 of Module 1's 182 steps). They are emitted as `kind="no_flow"`
with every feed closed and the vents left at atmosphere, never dropped.

A STEP WITH NO `ports` BLOCK IS INFERRED, AND SAYS SO. Module 1 (182 steps) and the iPSC
chip (109) predate the `ports` field, so the port state has to come from the step's own
note ("through p_in, p_mid vented", "all ports capped", "back-flush g_cx from p_g_out"),
the `waste` field, the operation, the target and the chip's channel graph. Every such
scenario carries `inferred=True` and an `inference` record with the evidence, so nothing
downstream can mistake a reading of English for a stated boundary condition.

VISCOSITY - one decision per step, and never silently water:

    aqueous, >= 30 C          fluid.viscosity_37c_pa_s     contract, in scope
    aqueous, room temp, dye   fluid.viscosity_pa_s         contract, in scope
    aqueous, room temp, real  fluid.viscosity_pa_s         contract value, OUT OF SCOPE ->
                                                           flagged: that key is the dye demo
    aqueous below ~15 C       ASSUMPTION x water(20 C)     no contract value exists
    cold organic solvent      ASSUMPTION x water(20 C)     no contract value exists
    oil / viscous core buffer ASSUMPTION x water(20 C)     no contract value exists
    emulsion, denaturant      ASSUMPTION x water(20 C)     no contract value exists

Assumed fluids carry `assumption=True`, a `min`/`max` for the solver to sweep, and an entry
in `unresolved` naming the contract key that should exist. The step's temperature comes
from `temp_c`, else the `cycles` profile, else an explicit temperature in its own note
("at 37 C", "ice-cold", "room temperature") or the thermal zone the note references, else
the nearest preceding step that had one - recorded either way.

Units: uL/min, Pa, degrees C at the JSON boundary (unit in the key name); SI inside.
"""

from __future__ import annotations

import json
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402

# The shapes are the mesh module's (assay2chip/cfd/README.md: "Shapes live as dataclasses
# in assay2chip/cfd/model.py"), so `Scenario` and `PortState` come from there rather than
# being redefined here. `ScenarioRecord` below is this module's own, richer record: it
# carries the provenance - inference evidence, assumptions, ambiguities - that the solver
# does not need but the report must have.
from assay2chip.cfd.model import (  # noqa: E402
    UL_MIN_TO_M3_S,
    PortState,
    Scenario,
)


@dataclass(frozen=True)
class PortBC:
    """One port's boundary condition for one step."""

    port: str
    type: str                       # flow | pressure | closed
    q_ul_min: float | None = None   # flow only, signed +into the chip
    p_pa: float | None = None       # pressure only
    role: str = ""                  # feed | vent | cap | not_stated
    why: str = ""

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"type": self.type}
        if self.type == "flow":
            out["q_ul_min"] = self.q_ul_min
            out["q_m3_s"] = ul_min_to_m3_s(self.q_ul_min or 0.0)
        elif self.type == "pressure":
            out["p_pa"] = self.p_pa
        out["role"] = self.role
        if self.why:
            out["why"] = self.why
        return out


@dataclass(frozen=True)
class FluidSpec:
    """The fluid in the chip for one step, and how sure we are of its viscosity."""

    name: str
    fluid_class: str
    viscosity_pa_s: float
    temp_c: float | None
    source: str
    contract_key: str | None = None
    assumption: bool = False
    viscosity_min_pa_s: float | None = None
    viscosity_max_pa_s: float | None = None
    non_newtonian: bool = False
    why: str = ""

    def to_dict(self) -> dict:
        out = {
            "name": self.name,
            "class": self.fluid_class,
            "viscosity_pa_s": self.viscosity_pa_s,
            "temp_c": self.temp_c,
            "source": self.source,
            "assumption": self.assumption,
        }
        if self.contract_key:
            out["contract_key"] = self.contract_key
        if self.assumption:
            out["viscosity_min_pa_s"] = self.viscosity_min_pa_s
            out["viscosity_max_pa_s"] = self.viscosity_max_pa_s
        if self.non_newtonian:
            out["non_newtonian"] = True
        if self.why:
            out["why"] = self.why
        return out


@dataclass
class ScenarioRecord:
    """One scenario plus everything the report needs to defend it.

    `scenario` is the object `solve.py` wants; the rest is provenance. Nothing here is
    allowed to be silent: an inferred port state, an assumed viscosity, a split feed and a
    derived flow rate each leave a flag and an explanation.
    """

    id: str
    kind: str                       # step | no_flow | worst_case_chamber | worst_case_feed
    chip_id: str
    protocol_id: str
    step: int | None
    operation: str
    targets: list[str]
    ports: dict[str, PortBC]
    fluid: FluidSpec
    temp_c: float | None
    temp_source: str = ""
    cites: str = ""
    note: str = ""
    duration_s: float | None = None
    volume_ul: float | None = None
    repeats: int = 1
    volume_total_ul: float | None = None
    exchange_kind: str | None = None
    direction: str = "forward"
    temp_profile: list[dict] | None = None
    port_fluids: dict[str, dict] = field(default_factory=dict)
    inferred: bool = False
    inference: list[str] = field(default_factory=list)
    ambiguous: bool = False
    alternatives: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    mappable: bool = True
    handoff_in: str | None = None
    handoff_out: str | None = None

    @property
    def q_in_ul_min(self) -> float:
        return sum(p.q_ul_min or 0.0 for p in self.ports.values() if p.type == "flow")

    @property
    def feeds(self) -> list[str]:
        return [p.port for p in self.ports.values() if p.type == "flow"]

    @property
    def vents(self) -> list[str]:
        return [p.port for p in self.ports.values() if p.type == "pressure"]

    @property
    def scenario(self) -> Scenario:
        """The `model.Scenario` the solver consumes."""
        return make_scenario(self)

    def scenario_unresolved(self) -> list[dict]:
        """What this one scenario rests on that was assumed rather than read.

        It travels on the Scenario itself, so a Field, a finding and a figure built from
        it can all say what was taken on trust - `solve.py` reads `mu_pa_s` from here and
        stops second-guessing the fluid, which only works if the caveat comes with it.
        """
        out: list[dict] = []
        if self.fluid.assumption:
            out.append({
                "subject": f"{self.chip_id} step {self.step} fluid",
                "question": (
                    f"viscosity of '{self.fluid.name}' ({self.fluid.fluid_class}) at "
                    f"{self.temp_c} C is not in any contract: used "
                    f"{self.fluid.viscosity_pa_s:.3g} Pa s from {self.fluid.source}. "
                    f"{self.fluid.why}"
                ),
                "sweep_pa_s": [self.fluid.viscosity_min_pa_s, self.fluid.viscosity_max_pa_s],
            })
        if self.inferred:
            out.append({
                "subject": f"{self.chip_id} step {self.step} ports",
                "question": (
                    f"the step has no `ports` block, so the boundary conditions were "
                    f"inferred: {'; '.join(self.inference)}"
                ),
            })
        if self.ambiguous:
            out.append({
                "subject": f"{self.chip_id} step {self.step} flow split",
                "question": (
                    f"{len(self.feeds)} feed ports share one flow_ul_min; readings: "
                    + "; ".join(
                        f"{a['reading']} = {a['q_ul_min_each']:.4g} uL/min each"
                        for a in self.alternatives
                    )
                ),
            })
        for flag in self.flags:
            if flag.startswith(("flow_derived", "flow_inherited_per_port", "temp_unknown",
                                "temp_read_from_note", "temp_inherited", "viscosity_out_of_scope",
                                "note_contradicts", "feed_without_vent", "flow_rate_missing")):
                out.append({
                    "subject": f"{self.chip_id} step {self.step}",
                    "question": f"{flag} ({self.cites})",
                })
        return out

    def to_dict(self) -> dict:
        out = {
            "id": self.id,
            "kind": self.kind,
            "chip_id": self.chip_id,
            "protocol_id": self.protocol_id,
            "step": self.step,
            "cites": self.cites,
            "operation": self.operation,
            "targets": self.targets,
            "ports": {k: v.to_dict() for k, v in self.ports.items()},
            "fluid": self.fluid.to_dict(),
            "temp_c": self.temp_c,
            "temp_source": self.temp_source,
            "q_in_ul_min": round(self.q_in_ul_min, 6),
            "duration_s": self.duration_s,
            "volume_ul": self.volume_ul,
            "repeats": self.repeats,
            "volume_total_ul": self.volume_total_ul,
            "exchange_kind": self.exchange_kind,
            "direction": self.direction,
            "inferred": self.inferred,
            "ambiguous": self.ambiguous,
            "mappable": self.mappable,
            "flags": self.flags,
            "note": self.note,
        }
        if self.inference:
            out["inference"] = self.inference
        if self.alternatives:
            out["alternatives"] = self.alternatives
        if self.temp_profile:
            out["temp_profile"] = self.temp_profile
        if self.port_fluids:
            out["port_fluids"] = self.port_fluids
        if self.handoff_in:
            out["handoff_in"] = self.handoff_in
        if self.handoff_out:
            out["handoff_out"] = self.handoff_out
        return out


@dataclass
class UnmappedStep:
    """A step we could not turn into a solvable scenario, and why."""

    protocol_id: str
    chip_id: str | None
    step: int | None
    operation: str
    reason: str
    detail: str
    cites: str = ""
    scenario_emitted: str | None = None

    def to_dict(self) -> dict:
        return {
            "protocol_id": self.protocol_id,
            "chip_id": self.chip_id,
            "step": self.step,
            "operation": self.operation,
            "reason": self.reason,
            "detail": self.detail,
            "cites": self.cites,
            "scenario_emitted": self.scenario_emitted,
        }


@dataclass
class ProtocolScenarios:
    """Every scenario for one chip + protocol pair."""

    protocol_id: str
    chip_id: str
    protocol_path: str
    chip_path: str
    scenarios: list[ScenarioRecord] = field(default_factory=list)
    unmapped: list[UnmappedStep] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)

    @property
    def step_scenarios(self) -> list[ScenarioRecord]:
        return [s for s in self.scenarios if s.kind in ("step", "no_flow")]

    @property
    def worst_cases(self) -> list[ScenarioRecord]:
        return [s for s in self.scenarios if s.kind.startswith("worst_case")]

    def to_dict(self) -> dict:
        return {
            "protocol_id": self.protocol_id,
            "chip_id": self.chip_id,
            "protocol_path": self.protocol_path,
            "chip_path": self.chip_path,
            "counts": {
                "steps": len([s for s in self.scenarios if s.kind == "step"]),
                "no_flow": len([s for s in self.scenarios if s.kind == "no_flow"]),
                "worst_case": len(self.worst_cases),
                "inferred": len([s for s in self.scenarios if s.inferred]),
                "ambiguous": len([s for s in self.scenarios if s.ambiguous]),
                "unmapped": len(self.unmapped),
            },
            "scenarios": [s.to_dict() for s in self.scenarios],
            "unmapped": [u.to_dict() for u in self.unmapped],
            "unresolved": self.unresolved,
        }


@dataclass
class ChainLink:
    """One module in a pipeline, in chain order."""

    order: int
    module: str
    name: str
    status: str
    chip_id: str | None
    protocol_id: str | None
    run: ProtocolScenarios | None
    handoff_in: str | None = None
    handoff_out: str | None = None
    gap: str | None = None

    def to_dict(self) -> dict:
        return {
            "order": self.order,
            "module": self.module,
            "name": self.name,
            "status": self.status,
            "chip_id": self.chip_id,
            "protocol_id": self.protocol_id,
            "handoff_in": self.handoff_in,
            "handoff_out": self.handoff_out,
            "gap": self.gap,
            "run": self.run.to_dict() if self.run else None,
        }


@dataclass
class PipelineScenarios:
    pipeline_id: str
    source: str
    chain: list[ChainLink] = field(default_factory=list)
    handoffs: list[dict] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)

    @property
    def runs(self) -> list[ProtocolScenarios]:
        return [link.run for link in self.chain if link.run is not None]

    def to_dict(self) -> dict:
        return {
            "pipeline_id": self.pipeline_id,
            "source": self.source,
            "chain": [link.to_dict() for link in self.chain],
            "handoffs": self.handoffs,
            "unresolved": self.unresolved,
        }


def make_scenario(rec: ScenarioRecord) -> Scenario:
    """The `model.Scenario` the solver consumes, out of one of this module's records.

    `mu_pa_s` is set deliberately: `solve.fluid_for()` trusts it over its own fallback
    precisely because this module has already decided between the two contract values and
    the flagged assumptions, and the decision travels with the scenario in `unresolved`.
    """
    return Scenario(
        step=rec.cites or rec.step,
        ports={
            port: PortState(
                type=bc.type,
                q_ul_min=bc.q_ul_min,
                p_pa=bc.p_pa,
                note=f"{bc.role}: {bc.why}" if bc.why else bc.role,
            )
            for port, bc in rec.ports.items()
        },
        fluid=rec.fluid.name,
        temp_c=rec.temp_c,
        mu_pa_s=rec.fluid.viscosity_pa_s,
        chip_id=rec.chip_id,
        flow_ul_min=rec.q_in_ul_min or None,
        unresolved=rec.scenario_unresolved(),
        note=rec.note,
    )


# --- units -------------------------------------------------------------------

def ul_min_to_m3_s(q_ul_min: float) -> float:
    return float(q_ul_min) * UL_MIN_TO_M3_S


def ul_h_to_ul_min(q_ul_h: float) -> float:
    return float(q_ul_h) / 60.0


# --- assumptions, all in one place and all flagged ---------------------------

# Viscosity for fluids the contracts do not cover, as a FACTOR on the contract's water at
# 20 C (fluid.viscosity_pa_s) so that no absolute viscosity is written down here. Every one
# is an assumption: it produces assumption=True, a min/max for the solver to sweep, and a
# line in `unresolved` asking for a real contract key. Never use water for these silently -
# that is the bug this table exists to prevent.
ASSUMED_FLUIDS: dict[str, dict] = {
    "cold_aqueous": {
        # Water's viscosity rises as it cools; the factor is interpolated from
        # COLD_WATER_FACTORS below. The +/-25% band covers the buffer's solutes.
        "factor": None,
        "span": 0.25,
        "why": "Aqueous buffer below room temperature. The contracts give 37 C and water "
               "at 20 C only, so a cold rinse or quench has no contract value.",
        "wanted_key": "fluid.viscosity_4c_pa_s",
    },
    "cold_organic_solvent": {
        "factor": 1.2,
        "span": 1.2,
        "why": "Cold acidified organic quench (methanol/acetonitrile class) at -20 C. No "
               "contract value, and the alcohol and nitrile ends of that class differ by "
               "about 3x, so the span is deliberately wide.",
        "wanted_key": "fluid.viscosity_cold_organic_pa_s",
    },
    "oil": {
        "factor": 3.0,
        "span": 1.5,
        "why": "Carrier oil for droplet generation. Fluorinated oils sit near water, "
               "mineral oils an order of magnitude above it, and the protocols do not say "
               "which is used.",
        "wanted_key": "fluid.viscosity_carrier_oil_pa_s",
    },
    "viscous_polymer": {
        "factor": 10.0,
        "span": 0.9,
        "why": "The viscous core/shell buffer (GelMA-class precursor). Shear-thinning and "
               "strongly concentration-dependent; no contract value.",
        "wanted_key": "fluid.viscosity_core_buffer_pa_s",
        "non_newtonian": True,
    },
    "emulsion": {
        "factor": 5.0,
        "span": 0.9,
        "why": "A packed water-in-oil emulsion behaves as a shear-thinning suspension, not "
               "as either phase. No contract value.",
        "wanted_key": "fluid.viscosity_emulsion_pa_s",
        "non_newtonian": True,
    },
    "denaturant": {
        "factor": 1.8,
        "span": 0.5,
        "why": "Concentrated chaotrope (the trap must tolerate 4 M guanidinium "
               "thiocyanate) or a denaturing solubilisation buffer. Clearly above water "
               "and not in any contract.",
        "wanted_key": "fluid.viscosity_denaturant_pa_s",
    },
}

# Viscosity of water relative to 20 C, from standard tables. Used only to interpolate the
# `cold_aqueous` assumption above - it is an assumption, not a contract value.
COLD_WATER_FACTORS: list[tuple[float, float]] = [
    (0.0, 1.79), (4.0, 1.57), (10.0, 1.30), (15.0, 1.14), (20.0, 1.00)
]

# Below this, an aqueous fluid is "cold" and has no contract value. Our own boundary: the
# contracts stop at water/20 C, and contracts.viscosity_for() switches at 30 C.
COLD_AQUEOUS_MAX_C = 15.0
# Above this, the 37 C contract value is being stretched (RT holds are fine, PCR is not).
HOT_CONTRACT_MAX_C = 45.0

FLUID_PATTERNS: list[tuple[str, str]] = [
    ("oil", r"\boil\b|carrier_oil|fluorinated|hfe"),
    ("emulsion", r"\bemulsion\b"),
    ("cold_organic_solvent", r"organic|methanol|acetonitrile|ethanol|isopropanol|solvent|chloroform"),
    ("denaturant", r"denatur|guanidin|chaotrop|lysis|solubilisation|solubilization|\bsds\b|urea"),
    ("viscous_polymer", r"gelma|viscous|polymer|prepolymer|hydrogel|alginate|peg|shell|core"),
]

NOTE_TEMP_PATTERNS: list[tuple[str, float | None]] = [
    (r"ice[- ]cold|on ice|iced\b", 4.0),
    (r"\bchilled\b|\bcold block\b|\bfridge\b", 4.0),
    (r"room temperature|\broom temp\b|\bRT\b|ambient", 22.0),
    (r"\b(37)\s*(?:deg(?:rees)?\s*)?C\b", 37.0),
    (r"\b(\d{1,3}(?:\.\d+)?)\s*(?:deg(?:rees)?\s*)?C\b", None),  # whatever number it names
]

STOP_FLOW_NOTE = re.compile(
    r"stop[- ]?flow|stop the flow|flow (?:then )?stops|flow stays off|no flow\b|"
    r"flow is off|every port capped|all ports capped|all ports are capped|\bidle\b|"
    r"settle(?:s)? and (?:attach|adhere)|so they (?:can )?settle",
    re.I,
)
ALL_CAPPED_NOTE = re.compile(
    r"(?:all|every) ports?(?: are| is| stay| stays)? capped|with every port capped", re.I
)
OTHER_INLETS_CAPPED_NOTE = re.compile(
    r"other inlets?(?: stay| are| remain)?(?: all)? capped|the other inlets stay capped", re.I
)
BACKFLUSH_NOTE = re.compile(r"back[- ]?flush", re.I)
PARALLEL_LANE_NOTE = re.compile(
    r"\beach\b|their own|per lane|in parallel|one at a time|every lane|own ports", re.I
)

# Operations that must move liquid: if one of these has no usable flow rate, that is a
# finding, not a no-flow step.
FLOWING_OPS = {
    "load", "exchange", "split", "pool", "release", "collect", "harvest",
    "encapsulate", "mix", "transfer",
}
# Operations that legitimately move nothing.
STATIC_OPS = {"hold_temp", "cure", "count", "valve"}


# --- chip graph --------------------------------------------------------------

class ChipGraph:
    """The chip's channels as an undirected graph, for inferring what a port must do.

    Nodes are chamber ids and port ids; edges are `channels` entries. Used only for
    inference - the mesh module owns the real discretisation.
    """

    def __init__(self, chip: dict):
        self.chip = chip
        self.chip_id = chip.get("id", "?")
        self.ports: dict[str, dict] = {p["id"]: p for p in chip.get("ports", [])}
        self.chambers: dict[str, dict] = {c["id"]: c for c in chip.get("chambers", [])}
        self.adj: dict[str, set[str]] = {}
        for ch in chip.get("channels", []):
            a, b = ch.get("from"), ch.get("to")
            if a is None or b is None:
                continue
            self.adj.setdefault(a, set()).add(b)
            self.adj.setdefault(b, set()).add(a)

    def inlets(self) -> list[str]:
        return [p for p, d in self.ports.items() if d.get("type") == "inlet"]

    def outlets(self) -> list[str]:
        return [p for p, d in self.ports.items() if d.get("type") != "inlet"]

    def hops(self, start: str, goal: str, blocked: Iterable[str] = ()) -> int | None:
        """Channel hops from `start` to `goal`, not passing through `blocked`."""
        blocked = set(blocked) - {start, goal}
        if start == goal:
            return 0
        seen = {start}
        queue = deque([(start, 0)])
        while queue:
            node, dist = queue.popleft()
            for nxt in sorted(self.adj.get(node, ())):
                if nxt in seen or nxt in blocked:
                    continue
                if nxt == goal:
                    return dist + 1
                seen.add(nxt)
                queue.append((nxt, dist + 1))
        return None

    def nearest(self, target: str, candidates: Iterable[str], blocked: Iterable[str] = ()) -> list[str]:
        """`candidates` that can reach `target`, nearest first."""
        scored = []
        for cand in candidates:
            dist = self.hops(target, cand, blocked)
            if dist is not None:
                scored.append((dist, cand))
        scored.sort()
        return [c for _, c in scored]

    def knows(self, node: str) -> bool:
        return node in self.ports or node in self.chambers


# --- loading and pairing -----------------------------------------------------

def _load(path: Path | str) -> dict:
    with open(ROOT / path if not Path(path).is_absolute() else Path(path), encoding="utf-8") as f:
        return json.load(f)


def _rel(path: Path | str) -> str:
    p = Path(path)
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def chip_for_protocol(protocol: dict, chips: dict[str, dict] | None = None) -> tuple[str | None, str]:
    """Which chip in `chip/` this protocol runs on, and how we decided.

    In order: the chip whose id matches the protocol id; a chip translated from this
    protocol's module; the pairing a pipeline file states; then the chip whose port ids
    cover every port the protocol names.
    """
    chips = chips if chips is not None else load_chips()
    pid = protocol.get("id", "")
    if pid in chips:
        return pid, "chip id equals protocol id"
    for chip_id, chip in chips.items():
        if chip_id.startswith(pid) or pid.startswith(chip_id.replace("_translated", "")):
            if chip.get("translated_from") or chip_id.endswith("_translated"):
                return chip_id, f"{chip_id} is a translation of {pid}"
    for pipeline_path in sorted((ROOT / "protocols").glob("pipeline_*.json")):
        data = _load(pipeline_path)
        for module in data.get("modules", []):
            if module.get("protocol", "").endswith(f"{pid}.json") and module.get("chip"):
                chip_id = Path(module["chip"]).stem
                if chip_id in chips:
                    return chip_id, f"{_rel(pipeline_path)} pairs {pid} with {chip_id}"
    named = protocol_port_ids(protocol)
    if named:
        for chip_id, chip in chips.items():
            if named <= {p["id"] for p in chip.get("ports", [])}:
                return chip_id, "the only chip whose ports cover every port the protocol names"
    return None, "no chip in chip/ matches this protocol"


def protocol_port_ids(protocol: dict) -> set[str]:
    out: set[str] = set()
    for step in protocol.get("steps", []):
        ports = step.get("ports") or {}
        for key in ("feed", "vent", "cap"):
            out.update(ports.get(key, []))
        if step.get("waste"):
            out.add(step["waste"])
    return out


def load_chips(directory: Path | str = "chip") -> dict[str, dict]:
    out = {}
    for path in sorted((ROOT / directory).glob("*.json")):
        try:
            data = _load(path)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and data.get("ports") is not None:
            out[data.get("id", path.stem)] = data
    return out


def load_protocols(directory: Path | str = "protocols") -> dict[str, dict]:
    out = {}
    for path in sorted((ROOT / directory).glob("*.json")):
        data = _load(path)
        if isinstance(data, dict) and data.get("kind") != "pipeline" and data.get("steps"):
            out[data.get("id", path.stem)] = data
    return out


# --- temperature -------------------------------------------------------------

def _note_temp(note: str) -> tuple[float | None, str]:
    for pattern, value in NOTE_TEMP_PATTERNS:
        match = re.search(pattern, note, re.I)
        if not match:
            continue
        if value is not None:
            return value, f"note says {match.group(0).strip()!r}"
        try:
            found = float(match.group(1))
        except (IndexError, ValueError):
            continue
        if -20.0 <= found <= 120.0:
            return found, f"note says {match.group(0).strip()!r}"
    return None, ""


def resolve_temperature(
    step: dict, chip: dict, previous: float | None, previous_step: int | None
) -> tuple[float | None, str, list[dict] | None, list[str]]:
    """The step's temperature, where it came from, its thermocycling profile and flags."""
    flags: list[str] = []
    profile = None
    cycles = step.get("cycles")
    if cycles:
        profile = [dict(p) for p in cycles.get("profile", [])]
        flags.append("thermocycled")
    if step.get("temp_c") is not None:
        return float(step["temp_c"]), "step.temp_c", profile, flags
    if profile:
        hottest = max((p["temp_c"] for p in profile), default=None)
        if hottest is not None:
            flags.append("temp_from_cycle_profile")
            return float(hottest), "cycles.profile (hottest hold)", profile, flags
    note = step.get("note", "") or ""
    found, why = _note_temp(note)
    if found is not None:
        flags.append("temp_read_from_note")
        return found, f"note: {why}", profile, flags
    # A note that points at a thermal zone without naming a temperature: the zone's range
    # is all the chip gives us, so say so rather than invent a setpoint.
    if re.search(r"thermal zone|heated zone|its zone|zone under", note, re.I):
        for zone in chip.get("thermal_zones", []):
            lo, hi = zone.get("min_temp_c"), zone.get("max_temp_c")
            flags.append(f"temp_zone_range_only:{zone.get('id')}:{lo}-{hi}C")
    if previous is not None:
        flags.append(f"temp_inherited_from_step_{previous_step}")
        return previous, f"inherited from step {previous_step}", profile, flags
    flags.append("temp_unknown")
    return None, "not stated anywhere; contracts.viscosity_for(None) treats it as 37 C", profile, flags


# --- fluid -------------------------------------------------------------------

def classify_fluid(reagent: str | None, operation: str, port_hint: str = "") -> str:
    # Underscores are word characters, so `in_oil` would not match \boil\b: split the
    # reagent and port names into words first.
    text = re.sub(r"[^a-z0-9]+", " ", f"{reagent or ''} {port_hint}".lower())
    for name, pattern in FLUID_PATTERNS:
        if re.search(pattern, text):
            return name
    return "aqueous"


def _cold_water_factor(temp_c: float) -> float:
    table = COLD_WATER_FACTORS
    if temp_c <= table[0][0]:
        return table[0][1]
    for (t0, f0), (t1, f1) in zip(table, table[1:]):
        if t0 <= temp_c <= t1:
            span = t1 - t0
            return f0 + (f1 - f0) * ((temp_c - t0) / span if span else 0.0)
    return table[-1][1]


def _assumed_fluid(
    fluid_class: str, name: str, temp_c: float | None, unresolved: list[dict]
) -> FluidSpec:
    spec = ASSUMED_FLUIDS[fluid_class]
    water = contracts.viscosity_water_20c_pa_s()
    factor = spec["factor"]
    detail = ""
    if factor is None:  # cold_aqueous: interpolate from the water table
        factor = _cold_water_factor(temp_c if temp_c is not None else 4.0)
        detail = f" (water factor {factor:.2f} at {temp_c} C)"
    span = spec["span"]
    central = water.pa_s * factor
    key_wanted = spec["wanted_key"]
    if not any(u.get("key") == key_wanted for u in unresolved):
        unresolved.append({
            "kind": "contract_gap",
            "key": key_wanted,
            "message": (
                f"no contract viscosity for '{fluid_class}': using an ASSUMPTION of "
                f"{factor:.2f} x {contracts.VISCOSITY_20C_KEY} and sweeping "
                f"+/-{span * 100:.0f}%. {spec['why']}"
            ),
            "wanted_in": "contracts/parameters.json",
        })
    return FluidSpec(
        name=name,
        fluid_class=fluid_class,
        viscosity_pa_s=central,
        temp_c=temp_c,
        source=f"ASSUMPTION: {factor:.2f} x {contracts.VISCOSITY_20C_KEY}{detail}",
        contract_key=None,
        assumption=True,
        viscosity_min_pa_s=central * (1.0 - span) if span < 1 else water.pa_s,
        viscosity_max_pa_s=central * (1.0 + span),
        non_newtonian=bool(spec.get("non_newtonian")),
        why=spec["why"],
    )


def resolve_fluid(
    reagent: str | None,
    operation: str,
    temp_c: float | None,
    *,
    is_dye_demo: bool,
    unresolved: list[dict],
    port_hint: str = "",
    flags: list[str] | None = None,
    prefer_port: bool = False,
) -> FluidSpec:
    """One fluid decision for one step. Never silently water - see the module docstring.

    `prefer_port` is for co-flowing streams: at a droplet nozzle the port name says what
    is in that line (`in_core` is the viscous core buffer, `in_oil` the carrier) and the
    step's single `reagent` field cannot.
    """
    flags = flags if flags is not None else []
    name = reagent or "unspecified"
    fluid_class = classify_fluid(reagent, operation, port_hint)
    if prefer_port and port_hint:
        from_port = classify_fluid(None, operation, port_hint)
        if from_port != "aqueous":
            fluid_class = from_port
            name = f"{port_hint} stream" if not reagent else f"{reagent} via {port_hint}"
    if fluid_class != "aqueous":
        flags.append(f"viscosity_assumed:{fluid_class}")
        return _assumed_fluid(fluid_class, name, temp_c, unresolved)
    if temp_c is not None and temp_c < COLD_AQUEOUS_MAX_C:
        flags.append("viscosity_assumed:cold_aqueous")
        return _assumed_fluid("cold_aqueous", name, temp_c, unresolved)
    visc = contracts.viscosity_for(temp_c)  # 37 C value, or water at 20 C below 30 C
    if visc.key == contracts.VISCOSITY_20C_KEY and not is_dye_demo:
        # The contract scopes water/20 C to the dye demo. It is still the closest stated
        # value for a room-temperature aqueous step, so use it and say it is out of scope.
        flags.append("viscosity_out_of_scope:water_20c_on_a_real_assay_step")
        if not any(u.get("key") == "fluid.viscosity_rt_pa_s" for u in unresolved):
            unresolved.append({
                "kind": "contract_scope",
                "key": "fluid.viscosity_rt_pa_s",
                "message": (
                    f"a room-temperature step on the real assay has no contract viscosity: "
                    f"{contracts.VISCOSITY_20C_KEY} is scoped to the dye demo. Used it "
                    f"anyway and flagged every scenario that rests on it."
                ),
                "wanted_in": "contracts/parameters.json",
            })
        return FluidSpec(
            name=name,
            fluid_class="aqueous",
            viscosity_pa_s=visc.pa_s,
            temp_c=temp_c,
            source=f"{contracts.VISCOSITY_20C_KEY} (OUT OF SCOPE: that key is the dye demo)",
            contract_key=visc.key,
            assumption=True,
            viscosity_min_pa_s=visc.pa_s,
            viscosity_max_pa_s=contracts.viscosity_37c_pa_s().pa_s
            if contracts.viscosity_37c_pa_s().pa_s > visc.pa_s else visc.pa_s,
            why=visc.scope,
        )
    if temp_c is None:
        flags.append("viscosity_at_unknown_temperature")
    elif temp_c > HOT_CONTRACT_MAX_C:
        # The contract's warm value is for 37 C. A 42 C RT hold is close enough; a 98 C
        # PCR denaturation is not, and water at 98 C is about a third of the 37 C value.
        # These are no-flow steps today, so the number does not reach a flow result - but
        # it must not travel on unlabelled either.
        flags.append(f"viscosity_out_of_scope:step_runs_at_{temp_c:g}C")
        if not any(u.get("key") == "fluid.viscosity_hot_pa_s" for u in unresolved):
            unresolved.append({
                "kind": "contract_scope",
                "key": "fluid.viscosity_hot_pa_s",
                "message": (
                    f"step(s) run above {HOT_CONTRACT_MAX_C:g} C (up to {temp_c:g} C) and the "
                    f"contracts stop at {contracts.VISCOSITY_37C_KEY}. The 37 C value is used "
                    f"and flagged; it overstates viscosity badly at PCR temperatures."
                ),
                "wanted_in": "contracts/parameters.json",
            })
    return FluidSpec(
        name=name,
        fluid_class="aqueous",
        viscosity_pa_s=visc.pa_s,
        temp_c=temp_c,
        source=visc.key,
        contract_key=visc.key,
        assumption=False,
        why=visc.scope,
    )


# --- flow --------------------------------------------------------------------

@dataclass
class FlowPlan:
    """What drives the step: a rate per feed port, or nothing at all."""

    per_port: dict[str, float]
    no_flow: bool
    source: str
    flags: list[str] = field(default_factory=list)
    ambiguous: bool = False
    alternatives: list[dict] = field(default_factory=list)
    unmapped: tuple[str, str] | None = None  # (reason, detail)


def _encapsulate_flows(step: dict, feeds: list[str]) -> tuple[dict[str, float], list[str]] | None:
    """Map `flows_ul_h` {core, shell, oil} onto the feed ports, and say how.

    The port is usually named after its stream (`in_core`, `in_oil`). When it is not - the
    sctip nozzle re-injects capsules through `in_load` as the core and the amplification mix
    through `in_reagent` as the shell, which its note spells out - the synonyms below carry
    the mapping, and the scenario is flagged so the reading is visible.
    """
    flows = step.get("flows_ul_h")
    if not flows:
        return None
    synonyms = {
        "core": ("core", "load", "cell", "capsule", "pack", "sample", "inner"),
        "shell": ("shell", "reagent", "mix", "sheath", "middle"),
        "oil": ("oil", "carrier", "continuous", "outer"),
    }
    out: dict[str, float] = {}
    flags: list[str] = []
    left = list(feeds)
    for stream, q_ul_h in flows.items():
        exact = [p for p in left if stream in p.lower()]
        if len(exact) == 1:
            out[exact[0]] = ul_h_to_ul_min(q_ul_h)
            left.remove(exact[0])
            continue
        match = [p for p in left if any(s in p.lower() for s in synonyms.get(stream, ()))]
        if len(match) == 1:
            out[match[0]] = ul_h_to_ul_min(q_ul_h)
            left.remove(match[0])
            flags.append(f"encapsulate_stream_mapped_by_synonym:{stream}->{match[0]}")
            continue
        return None
    if left or set(out) != set(feeds):
        return None
    return out, flags


def resolve_flow(
    step: dict,
    feeds: list[str],
    history: list[ScenarioRecord],
    chip_single_feed_rates: set[float],
) -> FlowPlan:
    """How much liquid each feed port pushes, in uL/min.

    In order: the step's own `flow_ul_min`; an `encapsulate` step's named stream flows; a
    note that says the flow is stopped; volume/duration; the rate each feed port last had.
    A flowing operation that reaches the end of that list is reported, not invented.
    """
    operation = step.get("operation", "")
    note = step.get("note", "") or ""
    flags: list[str] = []

    if not feeds:
        return FlowPlan({}, True, "no feed port: nothing drives this step", ["no_feed"])

    named = _encapsulate_flows(step, feeds)
    if named is not None:
        per_port, named_flags = named
        return FlowPlan(per_port, False,
                        "flows_ul_h mapped onto the feed ports, one stream per port",
                        named_flags)

    rate = step.get("flow_ul_min")
    source = "step.flow_ul_min"

    if rate is None and ALL_CAPPED_NOTE.search(note):
        return FlowPlan({}, True, f"note says every port is capped: {note[:60]!r}",
                        ["no_flow_from_note"])
    if rate is None and STOP_FLOW_NOTE.search(note):
        return FlowPlan({}, True, f"note says the flow is stopped: {note[:60]!r}",
                        ["no_flow_from_note"])

    if rate is None:
        duration = step.get("duration_s")
        volume = step.get("volume_ul")
        if duration is not None and float(duration) == 0.0:
            return FlowPlan({}, True, "duration_s is 0: the step is a valve action, not a flow",
                            ["no_flow_zero_duration"])
        if volume is not None and duration:
            rate = float(volume) / (float(duration) / 60.0)
            source = f"derived from volume_ul {volume} over duration_s {duration}"
            flags.append("flow_derived_from_volume_and_duration")
    if rate is None:
        # Inherit PER PORT: a step that keeps the same streams running (the droplet train
        # being filmed, the oil pushing the emulsion out) carries each port's own last
        # rate. Inheriting a multi-feed total and then splitting it again would be wrong.
        inherited: dict[str, float] = {}
        from_steps: list[str] = []
        for port in feeds:
            for past in reversed(history):
                bc = past.ports.get(port)
                if bc is not None and bc.type == "flow" and (bc.q_ul_min or 0) > 0:
                    inherited[port] = float(bc.q_ul_min or 0.0)
                    from_steps.append(f"{port} from step {past.step}")
                    break
        if len(inherited) == len(feeds):
            flags.append("flow_inherited_per_port")
            flags.extend(f"flow_inherited_from_step_{s.rsplit(' ', 1)[-1]}" for s in from_steps)
            return FlowPlan(
                inherited, False,
                f"no flow_ul_min: each feed keeps the rate it last had ({'; '.join(from_steps)})",
                flags,
            )
    if rate is None:
        if operation in FLOWING_OPS:
            return FlowPlan(
                {}, True,
                "no flow_ul_min, nothing to derive it from and no earlier step on these "
                "feed ports",
                ["flow_rate_missing"],
                unmapped=(
                    "no_flow_rate",
                    f"operation '{operation}' must move liquid through {', '.join(feeds)} but "
                    f"flow_ul_min is null, volume_ul/duration_s cannot give a rate and no "
                    f"earlier step drives the same feed ports",
                ),
            )
        return FlowPlan({}, True, "static operation with no flow_ul_min", ["no_flow"])

    rate = float(rate)
    if len(feeds) == 1:
        return FlowPlan({feeds[0]: rate}, False, source, flags)

    # Several feeds, one rate. State the split, flag the fork, never guess silently.
    per_feed_full = {p: rate for p in feeds}
    equal_split = {p: rate / len(feeds) for p in feeds}
    alternatives = [
        {"reading": "per_feed_full", "q_ul_min_each": rate,
         "why": "flow_ul_min is the rate for each driven port"},
        {"reading": "equal_split", "q_ul_min_each": rate / len(feeds),
         "why": "flow_ul_min is the total and the feeds share it equally"},
    ]
    parallel = bool(PARALLEL_LANE_NOTE.search(note))
    same_rate_alone = rate in chip_single_feed_rates
    if parallel and same_rate_alone:
        flags.append("multi_feed_per_feed_full")
        return FlowPlan(
            per_feed_full, False,
            f"{source}; {len(feeds)} feeds, read as PER FEED because the note says the lanes "
            f"run in parallel and {rate} uL/min is also the rate of a one-feed step on this chip",
            flags, ambiguous=True, alternatives=alternatives,
        )
    flags.append("multi_feed_equal_split")
    return FlowPlan(
        equal_split, False,
        f"{source}; {len(feeds)} feeds and one rate, split EQUALLY "
        f"({rate / len(feeds):.4g} uL/min each) because nothing says otherwise",
        flags, ambiguous=True, alternatives=alternatives,
    )


# --- ports -------------------------------------------------------------------

@dataclass
class PortPlan:
    feed: list[str]
    vent: list[str]
    cap: list[str]
    inferred: bool = False
    inference: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    unmapped: tuple[str, str] | None = None


def _clauses(note: str) -> list[str]:
    return [c for c in re.split(r"[.;:,]", note) if c.strip()]


def infer_ports_from_note(step: dict, graph: ChipGraph) -> PortPlan:
    """Read the port state out of the step's own English, then fill the gaps from the graph.

    Module 1 and the iPSC protocol predate the `ports` field but their notes are explicit:
    "through p_in, p_mid vented", "with p_mid capped", "leaves by p_g_out and p_a_out
    (vented)", "Back-flush g_cx from p_g_out", "Idle (all ports capped)".
    """
    note = step.get("note", "") or ""
    all_ports = list(graph.ports)
    feed: list[str] = []
    vent: list[str] = []
    cap: list[str] = []
    evidence: list[str] = []

    if ALL_CAPPED_NOTE.search(note):
        evidence.append("note says every port is capped")
        return PortPlan([], [], list(all_ports), True, evidence, ["ports_inferred"])

    for clause in _clauses(note):
        mentions = [
            (m.start(), m.end(), m.group(0))
            for m in re.finditer(r"\b(" + "|".join(re.escape(p) for p in all_ports) + r")\b", clause)
        ] if all_ports else []
        for index, (start, end, port) in enumerate(mentions):
            post = clause[end:mentions[index + 1][0] if index + 1 < len(mentions) else len(clause)]
            pre = clause[mentions[index - 1][1] if index else 0:start]
            if re.search(r"\bcap+ed\b|\bclosed\b|\bshut\b|\bblocked\b", post[:40], re.I):
                role = "cap"
            elif re.search(r"\bvent(?:ed)?\b|\bopen\b", post[:40], re.I):
                role = "vent"
            elif re.search(r"leaves? by|leave by|out (?:by|through)|drains? to|exits? (?:by|through)|"
                           r"\bto waste\b|collected (?:at|from)|delivered? to", pre[-60:], re.I):
                role = "vent"
            elif re.search(r"\bthrough\b|\bfrom\b|\bvia\b|\binto\b.*\bfrom\b|\bdriven\b|\bpush",
                           pre[-60:], re.I):
                role = "feed"
            else:
                continue
            bucket = {"feed": feed, "vent": vent, "cap": cap}[role]
            if port not in feed + vent + cap:
                bucket.append(port)
                evidence.append(f"note puts {port} in {role} ({pre[-24:].strip()}|{post[:18].strip()})")

    if OTHER_INLETS_CAPPED_NOTE.search(note):
        for port in graph.inlets():
            if port not in feed + vent + cap:
                cap.append(port)
        evidence.append("note says the other inlets stay capped")

    if BACKFLUSH_NOTE.search(note) and not feed:
        # "Back-flush g_cx from p_g_out": the driven port is an OUTLET.
        match = re.search(r"from\s+(\w+)", note, re.I)
        if match and match.group(1) in graph.ports:
            feed.append(match.group(1))
            evidence.append(f"back-flush drives the outlet {match.group(1)}")

    waste = step.get("waste")
    if waste and waste in graph.ports and waste not in feed + vent + cap:
        vent.append(waste)
        evidence.append(f"step.waste names {waste} as the drain")

    return PortPlan(feed, vent, cap, True, evidence, ["ports_inferred"])


def infer_ports(step: dict, graph: ChipGraph, history: list[ScenarioRecord]) -> PortPlan:
    """Full inference for a step with no `ports` block: note first, then the channel graph."""
    plan = infer_ports_from_note(step, graph)
    note = step.get("note", "") or ""
    operation = step.get("operation", "")
    targets = step_targets(step)
    target = targets[0] if targets else None
    static = operation in STATIC_OPS or bool(STOP_FLOW_NOTE.search(note))

    if not plan.feed and not plan.vent and static:
        plan.cap = list(graph.ports)
        plan.inference.append(f"{operation} with no port named in the note: treated as a "
                              f"no-flow dwell with everything capped")
        return plan

    if not plan.feed and target and graph.knows(target):
        candidates = graph.nearest(target, graph.inlets(), blocked=plan.cap)
        if operation == "pool" and len(candidates) > 1:
            # Pooling means every lane pushes into the shared target at once, so all the
            # inlets that are equally far from it are feeds, not just the first one.
            closest = graph.hops(target, candidates[0], plan.cap)
            plan.feed = [c for c in candidates if graph.hops(target, c, plan.cap) == closest]
            plan.inference.append(
                f"pool: every inlet {closest} hops from {target} drives it "
                f"({', '.join(plan.feed)})"
            )
            plan.flags.append("pool_feeds_all_lanes")
        elif candidates:
            plan.feed = [candidates[0]]
            plan.inference.append(
                f"feed inferred as {candidates[0]}: the inlet nearest {target} in the "
                f"channel graph ({graph.hops(target, candidates[0], plan.cap)} hops)"
            )
            if len(candidates) > 1:
                plan.flags.append("feed_inferred_from_several_candidates")
                plan.inference.append(f"other inlets that reach {target}: {', '.join(candidates[1:])}")
        else:
            plan.flags.append("no_inlet_reaches_target")
            plan.unmapped = (
                "no_feed_path",
                f"no inlet on {graph.chip_id} reaches {target} through the channel graph",
            )

    if not plan.vent and target and graph.knows(target) and not static:
        blocked = set(plan.cap) | set(plan.feed)
        candidates = graph.nearest(target, [p for p in graph.outlets() if p not in blocked], blocked)
        if candidates:
            plan.vent = [candidates[0]]
            plan.inference.append(
                f"vent inferred as {candidates[0]}: the nearest outlet to {target} "
                f"({graph.hops(target, candidates[0], blocked)} hops)"
            )
            if len(candidates) > 1:
                plan.flags.append("vent_inferred_from_several_candidates")
                plan.inference.append(f"other outlets that reach {target}: {', '.join(candidates[1:])}")
        else:
            plan.flags.append("no_outlet_reaches_target")
            if plan.unmapped is None:  # a missing feed path is the more basic failure
                plan.unmapped = (
                    "no_vent_path",
                    f"no outlet on {graph.chip_id} reaches {target} with "
                    f"{', '.join(sorted(blocked)) or 'nothing'} blocked",
                )

    for port in graph.ports:
        if port not in plan.feed + plan.vent + plan.cap:
            plan.cap.append(port)
    return plan


def stated_ports(step: dict, graph: ChipGraph) -> PortPlan:
    """The step's own `ports` block, checked against the chip."""
    block = step.get("ports") or {}
    feed = list(block.get("feed", []))
    vent = list(block.get("vent", []))
    cap = list(block.get("cap", []))
    flags: list[str] = []
    unmapped = None

    unknown = [p for p in feed + vent + cap if p not in graph.ports]
    if unknown:
        flags.append("ports_not_on_chip")
        unmapped = (
            "port_not_on_chip",
            f"the step names {', '.join(sorted(set(unknown)))}, which {graph.chip_id} does not "
            f"have (its ports are {', '.join(graph.ports)})",
        )
    duplicated = [p for p in set(feed) & set(vent) | set(feed) & set(cap) | set(vent) & set(cap)]
    if duplicated:
        flags.append("port_in_two_roles")

    waste = step.get("waste")
    if waste and waste in graph.ports and waste not in feed + vent + cap:
        vent.append(waste)
        flags.append("vent_added_from_step_waste")

    missing = [p for p in graph.ports if p not in feed + vent + cap]
    if missing:
        flags.append("port_not_stated")
    return PortPlan(feed, vent, cap + missing, False, [], flags, unmapped)


def _compact(numbers: list[int | None]) -> str:
    """'1-16, 19, 22-24' - so a 182-step protocol's gaps fit on one line."""
    values = sorted(n for n in numbers if n is not None)
    if not values:
        return "-"
    runs: list[list[int]] = [[values[0], values[0]]]
    for value in values[1:]:
        if value == runs[-1][1] + 1:
            runs[-1][1] = value
        else:
            runs.append([value, value])
    return ", ".join(f"{a}" if a == b else f"{a}-{b}" for a, b in runs)


def step_targets(step: dict) -> list[str]:
    if step.get("targets"):
        return list(step["targets"])
    if step.get("target"):
        return [step["target"]]
    return []


# --- building scenarios ------------------------------------------------------

def _is_dye_demo(protocol: dict, chip: dict) -> bool:
    text = " ".join([
        protocol.get("id", ""), protocol.get("name", ""), protocol.get("description", ""),
        chip.get("id", ""), chip.get("name", ""),
    ]).lower()
    return "dye" in text or ("demo" in text and "dye" in text)


def _single_feed_rates(protocol: dict) -> set[float]:
    """Flow rates that appear on a step with exactly one feed port.

    Used to read a multi-feed step: if a lane alone runs at 3.78 uL/min and three lanes
    "in parallel through their own ports" also say 3.78, the rate is per lane.
    """
    out = set()
    for step in protocol.get("steps", []):
        ports = step.get("ports") or {}
        if len(ports.get("feed", [])) == 1 and step.get("flow_ul_min") is not None:
            out.add(float(step["flow_ul_min"]))
    return out


def build_protocol(
    protocol_path: Path | str,
    chip_path: Path | str | None = None,
    *,
    worst_cases: bool = True,
) -> ProtocolScenarios:
    """One Scenario per step of one protocol on one chip, plus the worst cases."""
    protocol = _load(protocol_path)
    chips = load_chips()
    if chip_path is None:
        chip_id, why = chip_for_protocol(protocol, chips)
        if chip_id is None:
            raise ValueError(
                f"{_rel(protocol_path)}: {why}. Pass chip_path explicitly to build scenarios "
                f"against a chip this pipeline has not paired."
            )
        chip = chips[chip_id]
        chip_path = ROOT / "chip" / f"{chip_id}.json"
    else:
        chip = _load(chip_path)
    graph = ChipGraph(chip)
    run = ProtocolScenarios(
        protocol_id=protocol.get("id", Path(protocol_path).stem),
        chip_id=chip.get("id", "?"),
        protocol_path=_rel(protocol_path),
        chip_path=_rel(chip_path),
    )
    if protocol.get("placeholder"):
        run.unresolved.append({
            "kind": "placeholder_protocol",
            "key": run.protocol_id,
            "message": f"{run.protocol_path} is marked placeholder: its volumes, flows and "
                       f"setpoints are round numbers to be fitted, so every scenario built "
                       f"from it inherits that.",
        })

    is_dye = _is_dye_demo(protocol, chip)
    single_rates = _single_feed_rates(protocol)
    previous_temp: float | None = None
    previous_step: int | None = None
    steps_without_temp: list[int | None] = []
    steps_without_rate: list[str] = []
    last_reagent: str | None = None
    last_reagent_step: int | None = None

    for step in protocol.get("steps", []):
        number = step.get("step")
        operation = step.get("operation", "?")
        targets = step_targets(step)
        cites = (
            f"{run.protocol_path} step {number} ({operation}"
            f"{' ' + ', '.join(targets) if targets else ''})"
        )
        flags: list[str] = []

        if step.get("ports") is not None:
            plan = stated_ports(step, graph)
        else:
            plan = infer_ports(step, graph, run.scenarios)
        flags.extend(plan.flags)

        unknown_targets = [t for t in targets if not graph.knows(t) and t not in graph.ports]
        if unknown_targets:
            run.unmapped.append(UnmappedStep(
                run.protocol_id, run.chip_id, number, operation, "target_not_on_chip",
                f"target(s) {', '.join(unknown_targets)} are neither a chamber nor a port of "
                f"{run.chip_id}", cites,
            ))
            flags.append("target_not_on_chip")

        if plan.unmapped:
            run.unmapped.append(UnmappedStep(
                run.protocol_id, run.chip_id, number, operation, plan.unmapped[0],
                plan.unmapped[1], cites, scenario_emitted=None if plan.unmapped[0]
                == "port_not_on_chip" else "no_flow",
            ))
            if plan.unmapped[0] == "port_not_on_chip":
                continue

        temp_c, temp_source, profile, temp_flags = resolve_temperature(
            step, chip, previous_temp, previous_step
        )
        flags.extend(temp_flags)
        if temp_c is not None:
            previous_temp, previous_step = temp_c, number
        if "temp_unknown" in temp_flags:
            steps_without_temp.append(number)

        flow = resolve_flow(step, plan.feed, run.scenarios, single_rates)
        flags.extend(flow.flags)
        if any(f.startswith(("flow_derived", "flow_inherited")) for f in flow.flags):
            steps_without_rate.append(f"{number} ({flow.source})")

        # A step whose note says everything is capped while its own `ports` block names a
        # feed is self-contradicting. The note wins (nothing can flow into a sealed chip),
        # but the contradiction is reported rather than quietly resolved. A `hold_temp`
        # dwell is exempt: its ports block records where the valves are left standing, and
        # a hold never flows by definition.
        if plan.feed and flow.no_flow and "no_flow_from_note" in flow.flags:
            if operation in STATIC_OPS:
                flags.append("feed_declared_on_a_static_step")
            else:
                flags.append("note_contradicts_ports_block")
        if "note_contradicts_ports_block" in flags:
            run.unmapped.append(UnmappedStep(
                run.protocol_id, run.chip_id, number, operation, "note_contradicts_ports_block",
                f"ports.feed names {', '.join(plan.feed)} but the note says the flow is "
                f"stopped or every port is capped. Mapped as a no-flow dwell, which is the "
                f"only consistent reading; if the fill is real the step needs a flow_ul_min "
                f"and a vent.", cites, scenario_emitted="no_flow",
            ))

        # A driven feed with nowhere to go cannot be solved: an incompressible network
        # needs at least one pressure reference. Report it instead of inventing a vent.
        dead_ended = bool(flow.per_port) and not plan.vent
        if dead_ended:
            flags.append("feed_without_vent")
            run.unmapped.append(UnmappedStep(
                run.protocol_id, run.chip_id, number, operation, "no_pressure_reference",
                f"{', '.join(plan.feed)} is driven but every other port is capped, so the "
                f"network has no pressure reference and incompressible flow has nowhere to "
                f"go. Emitted as a no-flow dwell instead.", cites, scenario_emitted="no_flow",
            ))
        if flow.unmapped:
            run.unmapped.append(UnmappedStep(
                run.protocol_id, run.chip_id, number, operation, flow.unmapped[0],
                flow.unmapped[1], cites, scenario_emitted="no_flow",
            ))

        no_flow = flow.no_flow or dead_ended
        ports: dict[str, PortBC] = {}
        for port in graph.ports:
            if not no_flow and port in flow.per_port:
                ports[port] = PortBC(
                    port, "flow", q_ul_min=flow.per_port[port], role="feed", why=flow.source
                )
            elif port in plan.vent:
                ports[port] = PortBC(
                    port, "pressure", p_pa=0.0, role="vent",
                    why="vented to atmosphere, so it is the drain",
                )
            elif port in plan.feed and no_flow:
                ports[port] = PortBC(
                    port, "closed", role="feed",
                    why=f"declared a feed but nothing flows: {flow.source}",
                )
            else:
                role = "cap" if port in plan.cap else "not_stated"
                ports[port] = PortBC(
                    port, "closed", role=role,
                    why="capped" if role == "cap"
                    else "not named in the step's ports block: closed, conservatively",
                )

        reagent = step.get("reagent")
        if not reagent and step.get("reagents"):
            reagent = step["reagents"][0]
        if not reagent and last_reagent:
            # A dwell, a collect or a cure names no reagent, but the chamber is still full
            # of whatever the last step pushed in - the lysis buffer in Module 3's lysis
            # dwell, the organic extract in Module 6's soak. Treating those as water would
            # be exactly the silent-water mistake the README forbids.
            reagent = last_reagent
            flags.append(f"fluid_carried_from_step_{last_reagent_step}")
        elif reagent:
            last_reagent, last_reagent_step = reagent, number
        port_hint = " ".join(plan.feed)
        fluid = resolve_fluid(
            reagent, operation, temp_c, is_dye_demo=is_dye, unresolved=run.unresolved,
            port_hint=port_hint, flags=flags,
        )
        port_fluids: dict[str, dict] = {}
        if len(flow.per_port) > 1 and not no_flow:
            # Co-flowing streams are different fluids; the solver gets the dominant one as
            # `fluid` and the per-port table as well.
            for port in flow.per_port:
                port_fluids[port] = resolve_fluid(
                    reagent, operation, temp_c, is_dye_demo=is_dye, unresolved=run.unresolved,
                    port_hint=port, prefer_port=True,
                ).to_dict()
            classes = {d["class"] for d in port_fluids.values()}
            if len(classes) > 1:
                flags.append("multi_fluid_step")
                # `fluid` is the continuous phase - the stream with the most flow - and
                # the per-port table carries the rest. One viscosity cannot describe a
                # droplet nozzle, and the scenario says so rather than averaging.
                dominant = max(flow.per_port, key=lambda p: flow.per_port[p])
                fluid = resolve_fluid(
                    reagent, operation, temp_c, is_dye_demo=is_dye, unresolved=run.unresolved,
                    port_hint=dominant, prefer_port=True,
                )

        repeats = int(step.get("washes") or 1)
        if step.get("cycles"):
            repeats = max(repeats, int(step["cycles"].get("count", 1)))
        volume = step.get("volume_ul")
        record = ScenarioRecord(
            id=f"{run.chip_id}:{run.protocol_id}:s{number}",
            kind="no_flow" if no_flow else "step",
            chip_id=run.chip_id,
            protocol_id=run.protocol_id,
            step=number,
            operation=operation,
            targets=targets,
            ports=ports,
            fluid=fluid,
            temp_c=temp_c,
            temp_source=temp_source,
            cites=cites,
            note=step.get("note", ""),
            duration_s=step.get("duration_s"),
            volume_ul=volume,
            repeats=repeats,
            volume_total_ul=float(volume) * repeats if volume is not None else None,
            exchange_kind=step.get("exchange_kind") or ("sweep" if operation == "exchange" else None),
            direction=step.get("direction", "forward"),
            temp_profile=profile,
            port_fluids=port_fluids,
            inferred=plan.inferred,
            inference=plan.inference,
            ambiguous=flow.ambiguous,
            alternatives=flow.alternatives,
            flags=sorted(set(flags)),
            mappable=not (flow.unmapped or dead_ended or plan.unmapped or unknown_targets),
        )
        if operation == "exchange" and not step.get("exchange_kind"):
            record.flags = sorted(set(record.flags + ["exchange_kind_absent_treated_as_sweep"]))
        run.scenarios.append(record)

    if steps_without_temp:
        run.unresolved.append({
            "kind": "unknown_temperature",
            "key": f"{run.protocol_id}#temperature",
            "message": (
                f"{len(steps_without_temp)} step(s) state no temperature and no earlier step "
                f"does either, so viscosity falls back to the 37 C contract value: steps "
                f"{_compact(steps_without_temp)}."
            ),
        })
    if steps_without_rate:
        run.unresolved.append({
            "kind": "flow_rate_not_stated",
            "key": f"{run.protocol_id}#flow_ul_min",
            "message": (
                f"{len(steps_without_rate)} step(s) have no flow_ul_min and their rate was "
                f"reconstructed: {'; '.join(steps_without_rate[:8])}"
                f"{' ...' if len(steps_without_rate) > 8 else ''}."
            ),
        })

    if worst_cases:
        run.scenarios.extend(build_worst_cases(run, graph))
    return run


def build_worst_cases(run: ProtocolScenarios, graph: ChipGraph) -> list[ScenarioRecord]:
    """The two worst cases the README's findings need.

    `worst_case_chamber`: every chamber at the highest flow the protocol ever sends it, so
    the shear and pressure findings are tested against the real maximum rather than the
    last step that happened to be examined.
    `worst_case_feed`: each feed port driven ALONE at the highest rate it ever sees, which
    is both the single-syringe failure mode and the bracket on every split-feed ambiguity.
    """
    out: list[ScenarioRecord] = []
    flowing = [s for s in run.scenarios if s.kind == "step" and s.q_in_ul_min > 0]

    by_chamber: dict[str, ScenarioRecord] = {}
    for record in flowing:
        for target in record.targets:
            best = by_chamber.get(target)
            if best is None or record.q_in_ul_min > best.q_in_ul_min:
                by_chamber[target] = record
    for chamber, record in sorted(by_chamber.items()):
        out.append(ScenarioRecord(
            id=f"{run.chip_id}:{run.protocol_id}:worst_chamber:{chamber}",
            kind="worst_case_chamber",
            chip_id=run.chip_id,
            protocol_id=run.protocol_id,
            step=record.step,
            operation=record.operation,
            targets=[chamber],
            ports=dict(record.ports),
            fluid=record.fluid,
            temp_c=record.temp_c,
            temp_source=record.temp_source,
            cites=f"worst case for {chamber}: highest protocol flow, from {record.cites}",
            note=f"{chamber} at its maximum protocol flow "
                 f"({record.q_in_ul_min:.4g} uL/min), taken from step {record.step}.",
            duration_s=record.duration_s,
            volume_ul=record.volume_ul,
            repeats=record.repeats,
            exchange_kind=record.exchange_kind,
            inferred=record.inferred,
            inference=record.inference,
            ambiguous=record.ambiguous,
            alternatives=record.alternatives,
            flags=sorted(set(record.flags + ["worst_case"])),
        ))

    max_by_feed: dict[str, ScenarioRecord] = {}
    for record in flowing:
        for port, bc in record.ports.items():
            if bc.type != "flow" or bc.q_ul_min is None:
                continue
            best = max_by_feed.get(port)
            best_q = best.ports[port].q_ul_min if best else -1.0
            if best_q is None or bc.q_ul_min > best_q:
                max_by_feed[port] = record
    for port, record in sorted(max_by_feed.items()):
        q = record.ports[port].q_ul_min or 0.0
        vents = record.vents or graph.nearest(
            record.targets[0] if record.targets else port,
            [p for p in graph.outlets() if p != port],
        )[:1]
        ports: dict[str, PortBC] = {}
        for other in graph.ports:
            if other == port:
                ports[other] = PortBC(other, "flow", q_ul_min=q, role="feed",
                                      why=f"driven alone at its maximum protocol rate "
                                          f"(step {record.step})")
            elif other in vents:
                ports[other] = PortBC(other, "pressure", p_pa=0.0, role="vent",
                                      why="the drain for this worst case")
            else:
                ports[other] = PortBC(other, "closed", role="cap",
                                      why="closed so this feed is driven alone")
        flags = sorted(set(record.flags + ["worst_case"]
                           + ([] if record.vents else ["vent_inferred_for_worst_case"])))
        out.append(ScenarioRecord(
            id=f"{run.chip_id}:{run.protocol_id}:worst_feed:{port}",
            kind="worst_case_feed",
            chip_id=run.chip_id,
            protocol_id=run.protocol_id,
            step=record.step,
            operation=record.operation,
            targets=record.targets,
            ports=ports,
            fluid=record.fluid,
            temp_c=record.temp_c,
            temp_source=record.temp_source,
            cites=f"worst case for feed {port}: driven alone, from {record.cites}",
            note=f"{port} alone at {q:.4g} uL/min, its highest rate in the protocol "
                 f"(step {record.step}), with every other port closed.",
            inferred=record.inferred,
            flags=flags,
        ))
    return out


# --- pipeline mode -----------------------------------------------------------

def build_pipeline(
    path: Path | str = "protocols/pipeline_mvp.json", *, worst_cases: bool = True
) -> PipelineScenarios:
    """Every chip and its protocol, in chain order, with the handoff steps marked.

    Takes a `protocols/pipeline_*.json` (a `modules` list plus `handoffs`) or an S4
    `partition.json` (a `chips` list plus `handoffs`). A module with no chip or no protocol
    - the dropped barcoding and hairpin placeholders, the bench steps - stays in the chain
    as a gap, because the chain order is part of the answer.
    """
    data = _load(path)
    if data.get("kind") == "pipeline" or data.get("modules"):
        return _pipeline_from_modules(data, path, worst_cases)
    if data.get("chips"):
        return _pipeline_from_partition(data, path, worst_cases)
    raise ValueError(
        f"{_rel(path)} is neither a pipeline file (a 'modules' list) nor a partition "
        f"(a 'chips' list)"
    )


def _mark_handoffs(chain: list[ChainLink], handoffs: list[dict]) -> list[dict]:
    """Tag the scenario a handoff leaves from, and the one the next chip arrives at."""
    by_module = {link.module: link for link in chain}
    out = []
    for handoff in handoffs:
        record = {
            "id": handoff.get("id"),
            "carries": handoff.get("carries"),
            "after_step": handoff.get("after_step"),
            "from": handoff.get("from", {}),
            "to": handoff.get("to", {}),
            "note": handoff.get("note", ""),
            "marked_on": [],
            "gap": None,
        }
        gaps: list[str] = []
        source = (handoff.get("from") or {}).get("module")
        destination = (handoff.get("to") or {}).get("module")
        after = handoff.get("after_step")
        source_link = by_module.get(source)
        if source_link and source_link.run and after is not None:
            for scenario in source_link.run.step_scenarios:
                if scenario.step == after:
                    scenario.handoff_out = record["id"]
                    record["marked_on"].append(scenario.id)
            source_link.handoff_out = record["id"]
        elif source_link and source_link.run and after is None:
            last = source_link.run.step_scenarios[-1] if source_link.run.step_scenarios else None
            if last:
                last.handoff_out = record["id"]
                record["marked_on"].append(last.id)
                gaps.append("no after_step: marked on the protocol's last step")
            source_link.handoff_out = record["id"]
        destination_link = by_module.get(destination)
        if destination_link and destination_link.run:
            first = (destination_link.run.step_scenarios or [None])[0]
            if first:
                first.handoff_in = record["id"]
                record["marked_on"].append(first.id)
            destination_link.handoff_in = record["id"]
        if not source_link or not destination_link:
            gaps.append(
                f"one end is not a chip in this chain: "
                f"from={source or (handoff.get('from') or {}).get('roadmap')}, "
                f"to={destination or (handoff.get('to') or {}).get('roadmap')}"
            )
        elif source_link.run is None or destination_link.run is None:
            missing = [
                link.module for link in (source_link, destination_link) if link.run is None
            ]
            gaps.append(
                f"one end has no chip or protocol to build scenarios from: "
                f"{', '.join(missing)}"
            )
        for end in ("from", "to"):
            side = handoff.get(end) or {}
            port = side.get("port")
            if port in (None, "TBC") and not side.get("ports"):
                gaps.append(
                    f"the {end} end names no port{' (TBC)' if port == 'TBC' else ''}, so the "
                    f"tubing has no node to attach to"
                )
        record["gap"] = "; ".join(gaps) or None
        out.append(record)
    return out


def _pipeline_from_modules(data: dict, path: Path | str, worst_cases: bool) -> PipelineScenarios:
    pipeline = PipelineScenarios(pipeline_id=data.get("id", Path(path).stem), source=_rel(path))
    for order, module in enumerate(data.get("modules", []), start=1):
        chip_ref, protocol_ref = module.get("chip"), module.get("protocol")
        link = ChainLink(
            order=order,
            module=module.get("module", f"m{order}"),
            name=module.get("name", ""),
            status=module.get("status", "?"),
            chip_id=Path(chip_ref).stem if chip_ref else None,
            protocol_id=Path(protocol_ref).stem if protocol_ref else None,
            run=None,
        )
        if not chip_ref or not protocol_ref:
            link.gap = (
                f"{link.module} has "
                f"{'no chip' if not chip_ref else 'no protocol'} "
                f"(status {link.status}): {module.get('status_note') or module.get('note', '')[:120]}"
            )
            pipeline.unresolved.append({
                "kind": "chain_gap", "key": link.module, "message": link.gap,
            })
        elif not (ROOT / chip_ref).exists() or not (ROOT / protocol_ref).exists():
            link.gap = f"{chip_ref} or {protocol_ref} is not in the repo"
            pipeline.unresolved.append({
                "kind": "chain_gap", "key": link.module, "message": link.gap,
            })
        else:
            link.run = build_protocol(protocol_ref, chip_ref, worst_cases=worst_cases)
            pipeline.unresolved.extend(link.run.unresolved)
        pipeline.chain.append(link)
    pipeline.handoffs = _mark_handoffs(pipeline.chain, data.get("handoffs", []))
    return pipeline


def _pipeline_from_partition(data: dict, path: Path | str, worst_cases: bool) -> PipelineScenarios:
    """An S4 partition.json: chips in process order, each named `chip_id`.

    A partition names chips and unit ops, not protocol files, so the protocol is looked up
    as `protocols/<chip_id>.json`; when there is none yet the link stays a gap.
    """
    pipeline = PipelineScenarios(pipeline_id=data.get("id", Path(path).stem), source=_rel(path))
    for order, entry in enumerate(data.get("chips", []), start=1):
        chip_id = entry.get("chip_id")
        chip_path = ROOT / "chip" / f"{chip_id}.json"
        protocol_path = ROOT / "protocols" / f"{chip_id}.json"
        link = ChainLink(
            order=order, module=chip_id, name=entry.get("reason", ""),
            status=entry.get("scale", "?"), chip_id=chip_id,
            protocol_id=chip_id if protocol_path.exists() else None, run=None,
        )
        if chip_path.exists() and protocol_path.exists():
            link.run = build_protocol(protocol_path, chip_path, worst_cases=worst_cases)
            pipeline.unresolved.extend(link.run.unresolved)
        else:
            link.gap = (
                f"partition chip '{chip_id}' has "
                f"{'no chip/' + chip_id + '.json' if not chip_path.exists() else ''}"
                f"{' and ' if not chip_path.exists() and not protocol_path.exists() else ''}"
                f"{'no protocols/' + chip_id + '.json' if not protocol_path.exists() else ''}"
                f" yet, so its ops (unitops {entry.get('ops')}) cannot be turned into scenarios"
            )
            pipeline.unresolved.append({
                "kind": "chain_gap", "key": chip_id or f"chip_{order}", "message": link.gap,
            })
        pipeline.chain.append(link)
    pipeline.handoffs = _mark_handoffs(pipeline.chain, data.get("handoffs", []))
    return pipeline


# --- verification sweep ------------------------------------------------------

def build_all(*, worst_cases: bool = True) -> dict[str, Any]:
    """Every protocol in protocols/ that pairs with a chip in chip/, built and reported.

    The point of this function is the `unmapped` list: it is the honest account of what
    the CFD stage cannot say anything about, with the reason for each step.
    """
    chips = load_chips()
    runs: dict[str, ProtocolScenarios] = {}
    unpaired: list[dict] = []
    failed: list[dict] = []
    for protocol_id, protocol in load_protocols().items():
        chip_id, why = chip_for_protocol(protocol, chips)
        if chip_id is None:
            unpaired.append({"protocol_id": protocol_id, "reason": why})
            continue
        try:
            runs[protocol_id] = build_protocol(
                ROOT / "protocols" / f"{protocol_id}.json",
                ROOT / "chip" / f"{chip_id}.json",
                worst_cases=worst_cases,
            )
        except Exception as exc:  # a build failure is a finding, not a crash
            failed.append({"protocol_id": protocol_id, "chip_id": chip_id, "error": repr(exc)})
    return {"runs": runs, "unpaired": unpaired, "failed": failed}


def report(result: dict[str, Any] | None = None) -> str:
    """A plain-text summary of build_all(), for the S10 report and the CLI."""
    result = result or build_all()
    lines = ["CFD scenarios: protocol -> chip", ""]
    for protocol_id, run in sorted(result["runs"].items()):
        counts = run.to_dict()["counts"]
        lines.append(
            f"{protocol_id:>16} -> {run.chip_id:<22} "
            f"{counts['steps']:>4} flow, {counts['no_flow']:>3} no-flow, "
            f"{counts['worst_case']:>3} worst-case, {counts['inferred']:>3} inferred, "
            f"{counts['ambiguous']:>2} ambiguous, {counts['unmapped']:>2} unmapped"
        )
    for entry in result["unpaired"]:
        lines.append(f"{entry['protocol_id']:>16} -- not built: {entry['reason']}")
    for entry in result["failed"]:
        lines.append(f"{entry['protocol_id']:>16} !! build failed: {entry['error']}")
    lines += ["", "Unmappable steps:"]
    any_unmapped = False
    for protocol_id, run in sorted(result["runs"].items()):
        for unmapped in run.unmapped:
            any_unmapped = True
            lines.append(
                f"  {protocol_id} step {unmapped.step} ({unmapped.operation}): "
                f"{unmapped.reason} - {unmapped.detail}"
            )
    if not any_unmapped:
        lines.append("  none")
    lines += ["", "Assumptions and contract gaps:"]
    seen = set()
    for run in result["runs"].values():
        for entry in run.unresolved:
            key = (entry["kind"], entry.get("key"))
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  [{entry['kind']}] {entry.get('key')}: {entry['message']}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """python assay2chip/cfd/scenario.py [--json] [--pipeline protocols/pipeline_mvp.json]"""
    if "--pipeline" in argv:
        index = argv.index("--pipeline")
        path = argv[index + 1] if len(argv) > index + 1 else "protocols/pipeline_mvp.json"
        pipeline = build_pipeline(path)
        if "--json" in argv:
            print(json.dumps(pipeline.to_dict(), indent=2))
            return 0
        print(f"{pipeline.pipeline_id} ({pipeline.source})")
        for link in pipeline.chain:
            counts = link.run.to_dict()["counts"] if link.run else {}
            marks = " ".join(filter(None, [
                f"<-{link.handoff_in}" if link.handoff_in else "",
                f"->{link.handoff_out}" if link.handoff_out else "",
            ]))
            body = (
                f"{counts.get('steps', 0)} flow + {counts.get('no_flow', 0)} no-flow, "
                f"{counts.get('unmapped', 0)} unmapped" if link.run else f"GAP: {link.gap}"
            )
            print(f"  {link.order}. {link.module:<10} {link.chip_id or '-':<24} {body}  {marks}")
        for handoff in pipeline.handoffs:
            if handoff["gap"]:
                print(f"  handoff {handoff['id']}: {handoff['gap']}")
        return 0
    result = build_all()
    if "--json" in argv:
        print(json.dumps(
            {k: v.to_dict() for k, v in result["runs"].items()}
            | {"unpaired": result["unpaired"], "failed": result["failed"]},
            indent=2,
        ))
        return 0
    print(report(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

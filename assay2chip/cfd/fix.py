"""S10 fix: turn CFD findings into concrete, checkable changes - and apply the safe ones.

    python assay2chip/cfd/fix.py --chip chip/module6_v1.json --protocol protocols/module6_v1.json
    python assay2chip/cfd/fix.py --chip chip/module1_v1.json --findings work/x/cfd/findings.json
    python assay2chip/cfd/fix.py --chip ... --mode pipeline     # geometry -> rerun_from request

Reads `findings.json` (assay2chip/cfd/analyse.py), the chip spec and the protocol; writes
`cfd/fixes.json`:

    [{finding_id, action, target, params, predicted{metric, before, after}, auto_applicable,
      applied, verified_after}]

plus the loop log, the rerun requests and the gate verdicts, because a fix nobody can audit
is worse than no fix.

WHAT A FIX IS HERE. Not advice. Every candidate is a parameter change to a COPY of the
chip or the protocol, and its effect is PREDICTED by re-meshing and re-solving that copy in
memory - never by re-applying the scaling law that generated it. The scaling laws
(tau ~ Q/(w h^2), dP ~ 1/(w h^3)) only pick the first guess; a secant step then corrects it
against the solver, which is why `predicted.after` is a solved number and not an estimate.

THE SIX GATES. A fix is applied automatically only if ALL of them pass; otherwise it is
proposed for a human with the gate that stopped it named in `blocked_by`:

    measurable      the re-solve produced a before AND an after for the finding's metric.
                    Residence time, exchange fraction and settling belong to transport.py
                    and particles.py; if they are not loaded, those fixes are proposals.
    fabrication     no NEW fabrication-floor error (tools/check_chip_spec.py check_blocks +
                    port overlap, measured against the patched spec, not a stale DXF).
    planar          no NEW planar collision and no new rib under min_wall_mm
                    (cad/make_figures.py geometry_warnings - the repo's ONE collision
                    detector; this module does not contain a second one).
    cleared         the finding's metric is inside its limit at FIX_HEADROOM.
    no_new_blocker  re-analysing the patched copy produces no blocker that was not there.
    side_effects    the change does not quietly break the step it fixes: a flow cut that
                    stretches a step under KINETIC_STEP_S is a kinetics decision, and a
                    flow RAISE is never automatic while Reynolds cannot be checked (the
                    contracts have no fluid density, so `unmodelled` stays analyse.py's).

NEVER MODIFY A HAND-DESIGNED REPO CHIP (assay2chip/cfd/README.md). Flow-only fixes edit a
copy of the protocol. Geometry fixes either emit a `rerun_from` s6/s7 request carrying the
parameters (pipeline mode) or write a patched copy into the work dir (standalone mode).
`_assert_not_repo()` makes writing into chip/ or protocols/ raise, not warn.

BACKENDS. The predictor prefers the CFD siblings (mesh.py + solve.py, which discretise a
chamber across its width and so can see a dead zone). When they are not importable it falls
back to operator/network.py - the repo's existing hydraulic solver, independently written
and the one assay2chip/cfd/README.md requires the new solver to agree with. The fallback
treats a chamber as one node, so it can measure shear, pressure, flow and velocity but NOT
a low-velocity zone; that is exactly why `measurable` is a gate and not an afterthought.

Contract values come from assay2chip.lib.contracts and nowhere else. The constants in this
file are process choices with no contract key; each is named, explained, and copied into
every fix's `assumptions` so no reader has to guess which numbers are published.

Standard library plus the repo's own modules. Runs offline.
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import json
import math
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
for _extra in (str(ROOT), str(ROOT / "operator"), str(ROOT / "cad"), str(ROOT / "tools")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

from assay2chip.lib import contracts  # noqa: E402

import network as _network            # operator/: the repo's hydraulic network solver
import make_figures as _figures       # cad/: the repo's ONLY planar-collision detector
import check_chip_spec as _spec       # tools/: the repo's fabrication-floor checker


# --- process constants (NOT contract values; every fix carries them in `assumptions`) -----

MAX_FIX_ITERATIONS = 3
"""Outer loop bound. Matches the `rerun_from` cap in assay2chip/README.md so a geometry fix
cannot ask for more re-runs than the pipeline will grant."""

MAX_REFINEMENTS = 2
"""Secant corrections per candidate when the first guess overshoots or undershoots the
target. Stokes flow is linear, so one is usually enough; two covers the duct correction
term (1 - 0.63 h/w) changing as a channel is widened."""

BEFORE_AGREEMENT = 0.25
"""How far this module's own `before` may differ from the finding's `value`, as a fraction.

This gate caught a real and nasty bug. On module4_v1 three shear fixes were APPLIED with
before = after = 0.0: the finding said 2.17 dyn/cm2, but the step the candidate resolved
left that feature with no flow in it, so the re-solve measured zero, the `cleared` gate saw
0 <= 0.008 and waved it through. A fix that "clears" a finding by not looking at it is the
worst failure this module can have - worse than missing the fix - so disagreeing with the
analyser is now a blocking condition rather than a silent pass.
"""

SECANT_CLAMP = 20.0
"""How far one secant step may move the knob. The scaling law is local and the metric can
be saturated, so an unclamped correction can walk the knob off the map in two steps."""

FIX_HEADROOM = 0.8
"""A fix aims at this fraction of the limit, not at the limit. No contract states it. The
reason it is not 1.0: the solver's own duct formula is good to about 10%, the weir z
tolerance is +/-20 um on a 25 um gap, and a fix that lands exactly on the ceiling is one
rounding away from being a finding again."""

KINETIC_STEP_S = 5.0
"""A step shorter than this is doing something kinetically timed - a quench flood, a
chase - where stretching the duration changes the chemistry, not just the schedule. No
contract states it. Cutting the flow of such a step is always a human decision."""

MIN_RELATIVE_IMPROVEMENT = 0.02
"""Stop the loop when an iteration moves the worst metric by less than this fraction."""

WIDTH_ROUND_MM = 0.01
"""Widths are proposed on a 10 um grid: a fix that asks for 0.2374 mm pretends to a
precision no mould has."""

ZERO_LIMIT_TARGET = 0.01
"""What "none of it may settle" is checked as. particles.py states `settled_fraction`
against a limit of 0.0, and nothing in a finite channel reaches exactly zero, so a fix aims
at 1% instead. No contract states it; it is the fraction of a cell or capsule population
this module treats as "did not settle on the way". Below it, the loss is smaller than the
20% capsule loss rate the contracts already carry for other causes."""

MAX_FINDINGS_PER_RUN = 40
"""How many grouped findings one run will work on. analyse.py reports per mesh element per
step - 3614 of them on module6_v1 - and `group_findings` collapses those to one per
(class, spec feature). This caps what is left, worst first, so a run cannot spend an hour
re-solving the tail. Everything dropped is counted in the log, never silently."""


# --- the catalogue ------------------------------------------------------------------------

ACTIONS: dict[str, tuple[str, ...]] = {
    # The CFD README's fix for shear is "the flow that meets the ceiling at a stated
    # headroom, or a wider chamber". `widen_channel` is in here because mesh.py meshes a
    # channel-like block as a duct and analyse.py's shear findings land on those ducts: the
    # physics is the same tau = 6 mu Q / (w h^2), and refusing to widen a duct would leave
    # the commonest real shear finding with only a flow fix.
    "shear": ("set_flow", "widen_channel", "widen_chamber"),
    "leak": ("set_flow", "widen_channel"),
    "trap": ("move_outlet", "add_flow_distributor", "add_pillar_row", "change_aspect_ratio"),
    "dead_end": ("add_vent", "taper_expansion"),
    "bubble": ("add_vent", "taper_expansion"),
    "sediment": ("raise_flow", "shorten_line", "vertical_mounting"),
    "clog": ("widen_channel", "larger_capsule"),
    "exchange": ("more_volume", "more_washes"),
    "retention": ("tighten_z_tolerance", "larger_capsule"),
    "handoff": (),
    "unmodelled": (),
}
"""Finding class -> the actions this module knows how to build, in the order it tries them.

`handoff` and `unmodelled` are deliberately empty. A hand-off is tubing, dead volume and a
selector valve off the chip, so the change is to the plan and not to a geometry this module
owns. Re > 1 is a statement about where the model stops being true: patching the geometry
until the number looks acceptable would be exactly the silent-rewrite failure the CFD README
forbids. Both are reported with a `fix_hint` and no candidate."""

CLASS_ALIASES = {"dead_zone": "trap", "low_velocity": "trap", "residence": "trap",
                 "dead-end": "dead_end", "clogging": "clog", "sedimentation": "sediment"}

FLOW_ACTIONS = frozenset({"set_flow", "raise_flow", "more_volume", "more_washes"})
"""Actions that change the protocol only. They never touch a geometry, so they never need a
rerun_from request - but they still change a COPY."""

GEOMETRY_ACTIONS = frozenset({"widen_chamber", "widen_channel", "change_aspect_ratio",
                              "move_outlet", "add_vent", "add_flow_distributor",
                              "add_pillar_row", "taper_expansion", "shorten_line"})

SPEC_ACTIONS = frozenset({"tighten_z_tolerance", "larger_capsule", "vertical_mounting"})
"""Changes that are NOT a chip edit and NOT a flow edit, and that this module must still be
able to say out loud.

The depth weir is the case that forced this third kind. It retains only for gaps of
5.0-26.8 um, i.e. -20 to +1.8 um of the +/-20 um z tolerance: there is no channel to widen
and no flow to lower, because the thing that is wrong is the PROCESS WINDOW. The honest fix
is "hold z to about +/-11 um around a 16 um nominal" (a line in cad/fabrication.json and a
coupon run) or "use a bigger capsule" (a line in contracts/parameters.json and a wet-lab
change). Module 4's feed lines need the same honesty when the flow cannot rise far enough:
the answer is a shorter line or mounting the board vertically, and a module that could only
emit geometry and flow edits would have had to stay silent or pretend.

A spec change is NEVER auto-applied. It carries `spec_change{where, from, to, who}` so a
human knows which file and which bench step it lands in, and the numbers in it are solved
or swept, not guessed."""

SPEC_HOME: dict[str, str] = {
    "tighten_z_tolerance": "cad/fabrication.json (process window) + a gap coupon run",
    "larger_capsule": "contracts/parameters.json capsule.diameter_tunable_um (wet lab)",
    "vertical_mounting": "the run sheet / fixture drawing, not the chip spec",
}

RERUN_STAGE: dict[str, str] = {
    # S6 sizes parts from physics, so anything that changes a part's dimensions goes there.
    "widen_chamber": "s6", "widen_channel": "s6", "change_aspect_ratio": "s6",
    "add_flow_distributor": "s6", "add_pillar_row": "s6", "taper_expansion": "s6",
    # S7 routes channels and places ports, so anything topological goes there.
    "move_outlet": "s7", "add_vent": "s7", "shorten_line": "s7",
}

NEEDS_SIBLING: dict[str, str] = {
    "move_outlet": "S7's router, for a port that cannot legally land on the far face",
    "add_flow_distributor": "mesh.py + S6 part sizing",
    "add_pillar_row": "mesh.py + S6 part sizing",
    "change_aspect_ratio": "S6 part sizing, when the new box will not fit the board",
    "add_vent": "analyse.py (capillary vs drive pressure)",
    "taper_expansion": "analyse.py (priming) + mesh.py",
    "shorten_line": "S7's router, when the channel has an explicit routed path",
    "more_volume": "transport.py (washout curve)",
    "more_washes": "transport.py (washout curve)",
}
"""Which sibling owns the metric an action is supposed to move. Named in the proposal so a
human reads "cannot be predicted yet, transport.py owns the washout curve" rather than
"cannot be predicted"."""


# --- shapes --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One concrete change, with the law that produced its first guess."""

    finding_id: str
    action: str
    target: str
    params: dict[str, Any]
    kind: str                      # "flow" | "geometry"
    rationale: str
    scaling: str = ""              # how `params` were first guessed, for the audit trail
    knob: str | None = None        # the parameter a secant step may correct
    exponent: float = 0.0          # d(log metric) / d(log knob); 0 = do not refine

    @property
    def stage(self) -> str | None:
        return RERUN_STAGE.get(self.action)


@dataclass
class Solution:
    """What one re-solve of one protocol step produced, in the units the findings use."""

    backend: str
    shear_dyn_cm2: dict[str, float] = field(default_factory=dict)
    velocity_mm_s: dict[str, float] = field(default_factory=dict)
    flow_ul_min: dict[str, float] = field(default_factory=dict)
    pressure_kpa: dict[str, float] = field(default_factory=dict)
    throughput_ul_min: dict[str, float] = field(default_factory=dict)
    max_pressure_kpa: float = 0.0
    reynolds: dict[str, float] = field(default_factory=dict)
    mu_pa_s: float = 0.0
    notes: list[str] = field(default_factory=list)
    mesh: Any = None                              # mesh.py Mesh, on the cfd backend
    field: Any = None                             # solve.py Field, on the cfd backend
    # `field` above shadows dataclasses.field inside this class body, so anything declared
    # after it spells the default factory out in full.
    feature_of: dict[str, str] = dataclasses.field(default_factory=dict)
    """mesh node/edge id -> the chip-spec feature it came from.

    analyse.py quotes mesh ids (`d:comb_out_n`, `c:cx_n:3:1`), but a fix is a parameter
    change on a SPEC feature (`comb_out_n`, `cx_n`). This is the only bridge between the
    two, and it comes from the mesh rather than from parsing the id."""

    def spec_feature(self, feature_id: str | None) -> str | None:
        """The chip-spec id behind a finding's `feature_id`, mesh id or not."""
        if feature_id is None:
            return None
        return self.feature_of.get(feature_id, feature_id)

    def worst_for(self, spec_id: str, table: dict[str, float]) -> tuple[str | None, float | None]:
        """The worst value in `table` over every mesh element of one spec feature.

        A chamber is many cells and a duct may be several edges, so "the shear in cx_n" is
        the worst cell's, not an average: an average is how a hot wall gets hidden.
        """
        hits = [(k, v) for k, v in table.items() if self.feature_of.get(k, k) == spec_id]
        if not hits:
            return (None, table.get(spec_id))
        return max(hits, key=lambda kv: abs(kv[1]))


@dataclass
class Gate:
    name: str
    ok: bool
    detail: str

    def as_json(self) -> dict:
        return {"gate": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class FixRun:
    """Everything this module decided, and why."""

    chip_id: str
    mode: str
    backend: str
    fixes: list[dict] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)
    rerun_requests: list[dict] = field(default_factory=list)
    artefacts: dict[str, str] = field(default_factory=dict)
    unresolved: list[dict] = field(default_factory=list)
    assumptions: list[dict] = field(default_factory=list)
    patched_chip: dict | None = None
    patched_protocol: dict | None = None

    @property
    def applied(self) -> list[dict]:
        return [f for f in self.fixes if f["applied"]]

    @property
    def proposed(self) -> list[dict]:
        return [f for f in self.fixes if not f["applied"]]

    def as_json(self) -> dict:
        return {
            "chip_id": self.chip_id,
            "mode": self.mode,
            "backend": self.backend,
            "max_iterations": MAX_FIX_ITERATIONS,
            "fixes": self.fixes,
            "log": self.log,
            "rerun_requests": self.rerun_requests,
            "artefacts": self.artefacts,
            "unresolved": self.unresolved,
            "assumptions": self.assumptions,
        }


def process_assumptions() -> list[dict]:
    """The constants in this file that no contract states, with the reason for each."""
    return [
        {"name": "FIX_HEADROOM", "value": FIX_HEADROOM, "units": "fraction of the limit",
         "why": "Solver error is ~10% and the weir z tolerance is +/-71%; a fix landing on "
                "the ceiling is one rounding from being a finding again.",
         "contract_key": None, "assumption": True},
        {"name": "KINETIC_STEP_S", "value": KINETIC_STEP_S, "units": "s",
         "why": "A step this short is kinetically timed, so stretching it changes the "
                "chemistry. Cutting its flow is a human decision.",
         "contract_key": None, "assumption": True},
        {"name": "MAX_FIX_ITERATIONS", "value": MAX_FIX_ITERATIONS, "units": "iterations",
         "why": "Matches the rerun_from cap in assay2chip/README.md.",
         "contract_key": None, "assumption": True},
        {"name": "MIN_RELATIVE_IMPROVEMENT", "value": MIN_RELATIVE_IMPROVEMENT,
         "units": "fraction", "why": "Stop the loop when an iteration stops helping.",
         "contract_key": None, "assumption": True},
        {"name": "BEFORE_AGREEMENT", "value": BEFORE_AGREEMENT, "units": "fraction",
         "why": "How far this module's re-solve may differ from the finding's own value "
                "before the fix is refused. A fix measured on a state the finding does not "
                "describe is worse than no fix.",
         "contract_key": None, "assumption": True},
    ]


# --- siblings -------------------------------------------------------------------------------

def _geom_key(chip: dict) -> str:
    """A fingerprint of everything that changes a mesh, and nothing else.

    The full chip dict is mostly prose - module6_v1's `description` alone is 3 kB - and
    hashing it on every cache lookup cost more than the solve it was meant to save.
    """
    import hashlib

    parts = {k: chip.get(k) for k in
             ("id", "footprint", "layers", "chambers", "channels", "ports", "capsule",
              "retention", "thermal_zones", "encapsulation", "cell_filter", "trap")}
    return hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _step_key(step: dict) -> str:
    keys = ("step", "operation", "target", "reagent", "flow_ul_min", "volume_ul", "washes",
            "temp_c", "duration_s", "ports")
    return json.dumps({k: step.get(k) for k in keys}, sort_keys=True, default=str)


def _proto_key(protocol: dict) -> str:
    import hashlib

    body = "|".join(_step_key(s) for s in protocol.get("steps", []))
    return hashlib.sha1(f"{protocol.get('id')}|{body}".encode()).hexdigest()


def _sibling(name: str):
    """One of the CFD modules, or None when that agent has not landed it yet."""
    try:
        return __import__(f"assay2chip.cfd.{name}", fromlist=[name])
    except Exception:
        return None


def siblings() -> dict[str, Any]:
    return {n: _sibling(n) for n in ("mesh", "solve", "transport", "particles", "analyse")}


# --- scenario: which ports feed, vent and cap for one step -----------------------------------

def _graph(chip: dict) -> dict[str, set[str]]:
    g: dict[str, set[str]] = {}
    for ch in chip.get("channels", []):
        g.setdefault(ch["from"], set()).add(ch["to"])
        g.setdefault(ch["to"], set()).add(ch["from"])
    return g


def _nearest(chip: dict, start: str, wanted: set[str]) -> str | None:
    """Breadth-first search for the closest node in `wanted`, through the channel graph."""
    g, seen, frontier = _graph(chip), {start}, [start]
    while frontier:
        nxt = []
        for node in frontier:
            for other in g.get(node, ()):
                if other in seen:
                    continue
                if other in wanted:
                    return other
                seen.add(other)
                nxt.append(other)
        frontier = nxt
    return None


def port_states(chip: dict, step: dict, flow_ul_min: float | None = None) -> dict:
    """FEED / VENT / CAP per port for one protocol step.

    A protocol that states its own port block (protocols/module6_v1.json does) is believed.
    Otherwise the inlet nearest the step's target feeds and the nearest outlet vents, which
    is the weakest assumption that still gives a solvable network - and it is recorded, not
    hidden, by `Simulator.run`.
    """
    q = step.get("flow_ul_min") if flow_ul_min is None else flow_ul_min
    q = 0.0 if q is None else float(q)
    block = step.get("ports")
    if isinstance(block, dict) and (block.get("feed") or block.get("vent")):
        states: dict[str, Any] = {p: (_network.FEED, q) for p in block.get("feed", [])}
        states.update({p: _network.VENT for p in block.get("vent", [])})
        return states
    ports = {p["id"]: p for p in chip.get("ports", [])}
    inlets = {i for i, p in ports.items() if p.get("type") == "inlet"}
    outlets = {i for i, p in ports.items() if p.get("type") == "outlet"}
    target = step.get("target")
    feed = _nearest(chip, target, inlets) if target in _graph(chip) else None
    vent = _nearest(chip, target, outlets) if target in _graph(chip) else None
    feed = feed or (sorted(inlets)[0] if inlets else None)
    vent = vent or (sorted(outlets)[0] if outlets else None)
    if not feed or not vent:
        return {}
    return {feed: (_network.FEED, q), vent: _network.VENT}


COLD_FLUID_WORDS = ("quench", "organic", "solvent", "chloroform", "methanol", "acetonitrile")


def viscosity_for_step(step: dict) -> tuple[float, list[dict]]:
    """Viscosity for a step, plus any gap this module had to flag.

    contracts.viscosity_for() covers water at 20 C and the 37 C assay. A cold organic quench
    is neither, and assay2chip/cfd/README.md is explicit that water must never be used
    silently for it - so the number is still the contract's, and the gap is recorded.
    """
    temp = step.get("temp_c")
    visc = contracts.viscosity_for(temp)
    gaps: list[dict] = []
    reagent = str(step.get("reagent") or "").lower()
    cold = temp is not None and float(temp) < 20.0
    organic = any(w in reagent for w in COLD_FLUID_WORDS)
    if cold or organic:
        gaps.append({
            "step": step.get("step"),
            "question": f"no contract viscosity for {reagent or 'this fluid'}"
                        f"{f' at {temp:g} C' if temp is not None else ''}; used "
                        f"{visc.key} = {visc.pa_s:g} Pa s ({visc.scope}), FLAGGED",
            "assumption": True,
        })
    return visc.pa_s, gaps


# --- the predictor ----------------------------------------------------------------------------

class Simulator:
    """Re-mesh and re-solve a chip + protocol step IN MEMORY.

    The `cfd` backend is the real one: mesh.py discretises a chamber across its width, so a
    dead zone is visible and a serpentine is a duct at its lane width, and solve.py returns
    Reynolds, which is what lets a flow RAISE be checked at all.

    The `network` backend is operator/network.py, the independent implementation
    assay2chip/cfd/README.md requires the new solver to agree with. It treats a chamber as
    one node, so it can measure shear, pressure, flow and velocity but NOT a low-velocity
    zone - hence the `measurable` gate. It is the fallback when a sibling is mid-build, and
    it is what `--backend network` selects for a cross-check.

    Neither backend reads an exported DXF: a patched chip's DXF is the geometry BEFORE the
    fix, and a before/after pair measured against two different geometries is not a
    comparison.
    """

    def __init__(self, backend: str | None = None):
        mods = siblings()
        self._mesh, self._solve = mods["mesh"], mods["solve"]
        self._transport, self._particles = mods["transport"], mods["particles"]
        self._analyse = mods["analyse"]
        self._model = _sibling("model")
        usable = bool(self._mesh and self._solve and self._model)
        self.backend = backend or ("cfd" if usable else "network")
        if self.backend == "cfd" and not usable:
            raise RuntimeError("backend 'cfd' asked for but assay2chip/cfd/mesh.py, solve.py "
                               "and model.py are not all importable")
        self._cache: dict[tuple, Solution] = {}
        self._mesh_cache: dict[str, Any] = {}
        self._findings_cache: dict[tuple, tuple[list[dict], list[dict]]] = {}
        self.solves = 0
        self.analyses = 0

    # -- public -------------------------------------------------------------------------
    def run(self, chip: dict, step: dict, flow_ul_min: float | None = None) -> Solution:
        key = (_geom_key(chip), _step_key(step), flow_ul_min)
        if key not in self._cache:
            self.solves += 1
            self._cache[key] = (self._run_cfd if self.backend == "cfd" else self._run_network)(
                chip, step, flow_ul_min)
        return self._cache[key]

    def mesh_of(self, chip: dict):
        """mesh.py's Mesh for a chip, cached. `strict=False`: a chip with an orphan feature
        is a finding for analyse.py, not a reason the fix loop cannot run."""
        key = _geom_key(chip)
        if key not in self._mesh_cache:
            self._mesh_cache[key] = self._mesh.build_mesh(chip, strict=False)
        return self._mesh_cache[key]

    def findings(self, chip: dict, protocol: dict,
                 only_step: Any = None) -> tuple[list[dict], list[dict]]:
        """findings.json for a chip + protocol, cached.

        `only_step` restricts the analysis to one protocol step. That is not a shortcut: a
        change to step N's flow cannot alter step M, so for a flow-only fix the before/after
        comparison on step N is the whole comparison. A GEOMETRY change can move any step,
        so `predict` asks for the full protocol there - and transport.py's washout curve
        costs 6-10 s per exchange step, which is exactly why this is cached and scoped.
        """
        key = (_geom_key(chip), _proto_key(protocol), only_step)
        if key not in self._findings_cache:
            self.analyses += 1
            self._findings_cache[key] = _analyse_uncached(chip, protocol, self, only_step)
        return self._findings_cache[key]

    def scenario_for(self, chip: dict, step: dict, flow_ul_min: float | None = None):
        """One protocol step as a model.Scenario.

        Built here rather than through scenario.build_protocol() because that takes FILE
        PATHS, and the whole point of this module is that the chip and protocol being solved
        are in-memory copies that must never be written to chip/ or protocols/.
        """
        mu, _ = viscosity_for_step(step)
        q = step.get("flow_ul_min") if flow_ul_min is None else flow_ul_min
        states = port_states(chip, step, flow_ul_min)
        ports = {}
        for pid, state in states.items():
            if isinstance(state, tuple):
                ports[pid] = self._model.PortState("flow", q_ul_min=float(state[1]))
            else:
                ports[pid] = self._model.PortState("pressure", p_pa=0.0)
        for p in chip.get("ports", []):
            ports.setdefault(p["id"], self._model.PortState("closed"))
        return self._model.Scenario(
            step=step.get("step"), ports=ports,
            fluid=str(step.get("reagent") or ""), temp_c=step.get("temp_c"), mu_pa_s=mu,
            chip_id=chip.get("id", ""), flow_ul_min=None if q is None else float(q))

    # -- backends ----------------------------------------------------------------------
    def _run_cfd(self, chip: dict, step: dict, flow_ul_min: float | None) -> Solution:
        mu, gaps = viscosity_for_step(step)
        sol = Solution(backend="cfd", mu_pa_s=mu, notes=["mesh.py + solve.py"])
        for g in gaps:
            sol.notes.append(g["question"])
        try:
            mesh = self.mesh_of(chip)
        except Exception as exc:
            sol.notes.append(f"mesh.build_mesh failed: {exc}")
            return sol
        sol.mesh = mesh
        sol.feature_of = {e.id: e.feature_id for e in mesh.edges}
        sol.feature_of.update({n.id: n.feature_id for n in mesh.nodes})
        if not port_states(chip, step, flow_ul_min):
            sol.notes.append("no feed/vent pair for this step: nothing to solve")
            return sol
        try:
            field_ = self._solve.solve_step(mesh, self.scenario_for(chip, step, flow_ul_min))
        except Exception as exc:
            sol.notes.append(f"no steady solution: {exc}")
            return sol
        sol.field = field_
        sol.shear_dyn_cm2 = dict(field_.shear_dyn_cm2)
        sol.reynolds = dict(field_.re)
        sol.pressure_kpa = {k: v / 1000.0 for k, v in field_.p_pa.items()}
        sol.max_pressure_kpa = field_.max_pressure_pa() / 1000.0
        sol.velocity_mm_s = {k: math.hypot(*(list(v)[:2])) for k, v in field_.u_mm_s.items()}
        sol.flow_ul_min = {k: v * self._model.M3_S_TO_UL_MIN for k, v in field_.q_m3_s.items()}
        return sol

    def _run_network(self, chip: dict, step: dict, flow_ul_min: float | None) -> Solution:
        mu, gaps = viscosity_for_step(step)
        states = port_states(chip, step, flow_ul_min)
        sol = Solution(backend="network", mu_pa_s=mu)
        sol.notes.append("operator/network.py; geometry from the spec (straight, trimmed), "
                         "never a stale DXF")
        if not isinstance(step.get("ports"), dict):
            sol.notes.append("port states inferred: nearest inlet feeds, nearest outlet vents")
        for g in gaps:
            sol.notes.append(g["question"])
        if not states:
            sol.notes.append("no feed/vent pair for this step: nothing to solve")
            return sol
        try:
            net = _network.Network(chip, mu=mu, dxf=None)
            out = net.solve(states)
        except Exception as exc:                       # NetworkError, singular, bad spec
            sol.notes.append(f"no steady solution: {exc}")
            return sol
        for cid, row in out["channels"].items():
            sol.shear_dyn_cm2[cid] = row["shear_dyn_cm2"]
            sol.velocity_mm_s[cid] = row["velocity_mm_s"]
            sol.flow_ul_min[cid] = row["flow_ul_min"]
        for cid, row in out["chambers"].items():
            sol.shear_dyn_cm2[cid] = row["shear_dyn_cm2"]
            sol.throughput_ul_min[cid] = row["throughput_ul_min"]
        sol.pressure_kpa = {n: p / 1000.0 for n, p in out["pressure_pa"].items()}
        sol.max_pressure_kpa = max(sol.pressure_kpa.values(), default=0.0)
        # Reynolds is deliberately empty: contracts/parameters.json has no fluid density, so
        # a number here would be invented. `unmodelled` stays analyse.py's finding, and the
        # side_effects gate refuses to raise a flow without it.
        sol.notes.append("Reynolds not computed: the contracts have no fluid density")
        return sol


def _accepts(fn: Callable, name: str) -> bool:
    try:
        import inspect

        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _get(obj: Any, name: str) -> Any:
    return obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)


def _scenario_ports(chip: dict, step: dict, flow_ul_min: float | None) -> dict:
    """The `Scenario.ports` shape from assay2chip/cfd/README.md."""
    out = {}
    for pid, state in port_states(chip, step, flow_ul_min).items():
        if isinstance(state, tuple):
            out[pid] = {"type": "flow", "q_ul_min": state[1]}
        else:
            out[pid] = {"type": "pressure", "p_pa": 0.0}
    for p in chip.get("ports", []):
        out.setdefault(p["id"], {"type": "closed"})
    return out


# --- measuring a finding's metric -------------------------------------------------------------

def _is_pa(units: str) -> bool:
    return units.strip().lower() in ("pa", "pascal", "pascals")


def _read(sol: Solution, table: dict[str, float], fid: str | None) -> float | None:
    """`table[fid]` when the finding quotes a mesh id, else the worst value over the spec
    feature - because a fix is applied to a feature and judged on its worst cell."""
    if fid is None:
        return None
    if fid in table:
        return table[fid]
    return sol.worst_for(fid, table)[1]


def edge_kinematics(sol: Solution, spec_id: str) -> tuple[float, float, float] | None:
    """(u_mm_s, transit_time_s, depth_mm) for a spec feature, from the re-solved field.

    The slowest mesh edge of the feature is the one that matters: a particle only has to
    lose the race once. Derived the same way particles.py derives it - u = Q / (w h), then
    transit = L / u - so the before/after pair and the finding agree on the definition.
    """
    if sol.mesh is None or sol.field is None:
        return None
    worst: tuple[float, float, float] | None = None
    for edge in sol.mesh.edges:
        if edge.feature_id != spec_id or edge.kind == "hs_face":
            continue
        q = abs(sol.field.q_m3_s.get(edge.id, 0.0))
        area_m2 = (edge.width_mm / 1000.0) * (edge.depth_mm / 1000.0)
        if not area_m2:
            continue
        u_mm_s = q / area_m2 * 1000.0
        if u_mm_s <= 0:
            continue
        transit_s = edge.length_mm / u_mm_s
        if worst is None or transit_s > worst[1]:
            worst = (u_mm_s, transit_s, edge.depth_mm)
    return worst


def sediment_verdict(finding: dict, sol: Solution, chip: dict):
    """particles.py's own settling verdict, recomputed on a re-solved field.

    `particles.analyse_chip` reads `chip/<id>.json` from disk, so it cannot be pointed at a
    patched copy. `particles.sediment_check` CAN be: it takes the feature, the particle
    class, the depth, the transit time and the velocity as arguments. So the intent (is
    settling a LOSS here, or the point of the feature?), the particle class, and the
    worst-case diameter and excess density all stay particles.py's calls - the only numbers
    this module supplies are the two it actually changed, u and transit.

    That matters more than it looks. An earlier version of this computed the fall time here
    from `stokes_settling_m_s` and got 0.98 s where particles.py gets 1.51 s, because it
    read the wrong end of `worst_case()`. One call, one owner, no second settling model.
    """
    mod = _sibling("particles")
    if mod is None:
        return None
    spec_id = _feature_id(finding)
    feature = _channel(chip, spec_id) or _chamber(chip, spec_id)
    kin = edge_kinematics(sol, spec_id)
    if feature is None or kin is None:
        return None
    u_mm_s, transit_s, depth_mm = kin
    name = str(finding.get("particle") or "").strip()
    if not name:
        # analyse.py puts the class in the finding id: `sediment:<chip>:<step>:<el>:<class>`
        name = str(finding.get("id", "")).rsplit(":", 1)[-1]
    particle = mod.particle_classes(sol.mu_pa_s or None).get(name)
    if particle is None:
        return None
    try:
        return mod.sediment_check(
            feature, f"d:{spec_id}", "edge", particle, depth_mm, transit_s, u_mm_s,
            sol.mu_pa_s, role="chamber" if _chamber(chip, spec_id) else "channel",
            operation=finding.get("operation"))
    except Exception:
        return None


def sediment_after(finding: dict, sol: Solution, chip: dict) -> float | None:
    """`settled_fraction` for one sediment finding on a re-solved field."""
    verdict = sediment_verdict(finding, sol, chip)
    return None if verdict is None else float(verdict.settled_fraction)


TRANSPORT_METRICS = {
    "dead_zone_volume_fraction": "dead_volume_fraction",
    "low_speed_volume_fraction": "low_speed_volume_fraction",
    "long_age_volume_fraction": "long_age_volume_fraction",
    "unreachable_volume_fraction": "unreachable_volume_fraction",
    "residence_time_s": "p95_age_s",
    "mean_age_s": "mean_age_s",
}
"""analyse.py's trap metrics -> the key in transport.DeadZones.to_json().

Having these means a trap fix - moving the outlet to the far face, changing the aspect
ratio - can actually be PREDICTED rather than only proposed, because transport.py can be
re-run on the patched mesh. Without them every Hele-Shaw dead-zone fix would fail the
`measurable` gate, which was true while transport.py was still being built and is not now.
"""


def transport_metric(finding: dict, sol: Solution, chip: dict) -> float | None:
    """One of transport.py's dead-zone numbers, recomputed on a re-solved field.

    Scoped to the one chamber the finding names, because `transport.analyse` walks every
    chamber and the age field is an advection-diffusion solve - about 10 s a step on Module
    1's mesh. One chamber is the only part that can have moved.
    """
    mod = _sibling("transport")
    key = TRANSPORT_METRICS.get(str(finding.get("metric") or ""))
    spec_id = sol.spec_feature(finding.get("feature_id"))
    if mod is None or key is None or sol.mesh is None or sol.field is None or not spec_id:
        return None
    try:
        age = mod.mean_age(sol.mesh, sol.field)
        zones = mod.dead_zones(sol.mesh, sol.field, age, spec_id)
    except Exception:
        return None
    value = zones.to_json().get(key)
    return float(value) if isinstance(value, (int, float)) else None


def measure(finding: dict, sol: Solution, chip: dict) -> float | None:
    """The finding's own metric, read out of a solved field. None = this module cannot see it.

    None is not a failure mode to paper over: residence time, exchange fraction and settling
    margin are transport.py's and particles.py's, and a fix whose effect cannot be measured
    is never applied automatically (the `measurable` gate).
    """
    metric = str(finding.get("metric") or "").lower()
    fid = finding.get("feature_id")
    units = str(finding.get("units") or "")
    if metric in TRANSPORT_METRICS:
        return transport_metric(finding, sol, chip)
    if "settled_fraction" in metric or "settl" in metric:
        return sediment_after(finding, sol, chip)
    if "transit" in metric:
        kin = edge_kinematics(sol, _feature_id(finding) or "")
        return None if kin is None else kin[1]
    if "shear" in metric:
        return _read(sol, sol.shear_dyn_cm2, fid)
    if "pressure" in metric:
        value = _read(sol, sol.pressure_kpa, fid)
        if value is None and fid in (None, "", "chip"):
            value = sol.max_pressure_kpa
        return None if value is None else (value * 1000.0 if _is_pa(units) else value)
    if "velocity" in metric or "speed" in metric:
        return _read(sol, sol.velocity_mm_s, fid)
    if "reynolds" in metric or metric in ("re", "re_number"):
        return _read(sol, sol.reynolds, fid)
    if "flow" in metric or metric.startswith("q_"):
        value = _read(sol, sol.flow_ul_min, fid)
        return value if value is None else abs(value)
    if "width" in metric:
        ch = _channel(chip, sol.spec_feature(fid))
        if ch is None:
            return None
        return ch["width_mm"] * (1000.0 if "um" in units.lower() else 1.0)
    return None


def _scalar(value: Any) -> float | None:
    """A finding's `value`/`limit` as one number, or None.

    particles.py's weir finding reports a BAND - `working_gap_band_um: [5.0, 26.8]` against
    a limit of `[20.1, 26.8]`. There is no single number to drive a flow or a width at, and
    inventing one (a midpoint, a width) would turn a fabrication window into a fake scalar.
    None here is what routes that finding to the spec-change branch of the catalogue.
    """
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def direction(finding: dict) -> str:
    """"max" when the finding fires above its limit, "min" when it fires below."""
    if finding.get("direction") in ("max", "min"):
        return finding["direction"]
    value, limit = _scalar(finding.get("value")), _scalar(finding.get("limit"))
    if value is not None and limit is not None:
        return "max" if value > limit else "min"
    return "max"


def target_value(finding: dict) -> float | None:
    """Where a fix should land: inside the limit by FIX_HEADROOM.

    A limit of zero is not a rounding problem, it is a different kind of limit: particles.py
    states `settled_fraction` against a limit of 0.0, meaning "none of it may settle". There
    is no 80% of zero, so the target is the `ZERO_LIMIT_TARGET` fraction instead, named and
    flagged like every other constant here.
    """
    limit = _scalar(finding.get("limit"))
    if limit is None:
        return None
    if limit == 0.0:
        return ZERO_LIMIT_TARGET if direction(finding) == "max" else 0.0
    return limit * FIX_HEADROOM if direction(finding) == "max" else limit / FIX_HEADROOM


def within_limit(finding: dict, value: float | None, headroom: bool = True) -> bool:
    limit = _scalar(finding.get("limit"))
    if value is None or limit is None:
        return False
    bound = target_value(finding) if headroom else limit
    if bound is None:
        return False
    return value <= bound if direction(finding) == "max" else value >= bound


# --- helpers on the spec -----------------------------------------------------------------------

def _channel(chip: dict, fid: str | None) -> dict | None:
    return next((c for c in chip.get("channels", []) if c.get("id") == fid), None)


def _chamber(chip: dict, fid: str | None) -> dict | None:
    return next((c for c in chip.get("chambers", []) if c.get("id") == fid), None)


def _port(chip: dict, fid: str | None) -> dict | None:
    return next((p for p in chip.get("ports", []) if p.get("id") == fid), None)


def _step(protocol: dict, number: Any) -> dict | None:
    """The raw protocol step a finding's `step` key refers to.

    TWO NUMBERING SCHEMES MEET HERE and getting it wrong silently solves the wrong step -
    which is worse than failing, because the fix then looks measured. analyse.py and
    particles.py both quote `StepView.index`, a POSITION in the protocol's step list;
    `StepView`'s own docstring says so ("Index is the key the findings quote as `step`").
    A protocol's `step` field is 1-based, so module4_v1's settling finding on `ch_in_1`
    carries step 0 and means the step the file calls step 1.

    Position therefore wins, because that is what every sibling emits. The `step` field is
    the fallback, for a findings file written by hand.

    The bug this prevents: preferring the `step` field resolved ch_in_2's finding (index 1)
    to the step that feeds in_s1, where ch_in_2 carries no flow at all - so the velocity came
    back zero, the metric came back None, and the fix was reported as unmeasurable.
    """
    steps = protocol.get("steps", [])
    if isinstance(number, int) and not isinstance(number, bool) and 0 <= number < len(steps):
        return steps[number]
    hit = next((s for s in steps if s.get("step") == number), None)
    if hit is None and number is None:
        hit = next((s for s in steps if s.get("flow_ul_min")), None)
    return hit


def _step_ref(protocol: dict, step: dict | None) -> int | None:
    """A candidate's step key: the POSITION, so `_step` resolves it the same way twice.

    Writing `step["step"]` into a candidate's params was a real bug: the finding quoted
    index 0, `_step` resolved it positionally to the right step, the candidate then stored
    that step's own `step` field (1), and the re-solve resolved 1 positionally to the NEXT
    step - where the fixed channel carries no flow, so the fix measured as unmeasurable.
    One scheme, written down once.
    """
    if step is None:
        return None
    for i, candidate in enumerate(protocol.get("steps", [])):
        if candidate is step:
            return i
    return None


def _box_volume_ul(chamber: dict) -> float:
    sx, sy, sz = chamber["size"][:3]
    return sx * sy * sz


def _restricting_channel(chip: dict, sol: Solution) -> str | None:
    """The channel dropping the most pressure: dP = Q R, and R ~ 1/(w h^3)."""
    mu = sol.mu_pa_s or contracts.viscosity_for(None).pa_s
    worst, worst_id = -1.0, None
    for ch in chip.get("channels", []):
        q = abs(sol.flow_ul_min.get(ch["id"], 0.0))
        if not q:
            continue
        geo = _network.channel_geometry(chip).get(ch["id"])
        if not geo:
            continue
        r = _network.rect_resistance(geo["length_mm"], geo["width_mm"], geo["depth_mm"], mu)
        dp = q * 1e-9 / 60 * r
        if dp > worst:
            worst, worst_id = dp, ch["id"]
    return worst_id


def _free_edge_position(chip: dict, feature: dict) -> list[float] | None:
    """A port position on the board edge closest to `feature`, at the min port pitch from
    every existing port. Returns None when nothing legal is free - which is the honest
    answer for a 30 x 20 mm board with six ports already on it."""
    fx, fy = chip["footprint"]["x"], chip["footprint"]["y"]
    bore = contracts.floor("min_port_mm")
    pitch = contracts.floor("min_port_pitch_mm")
    cx, cy = (feature.get("center") or feature.get("position"))[:2]
    z = next((p["position"][2] for p in chip.get("ports", [])), 0.0)
    margin = bore / 2 + contracts.floor("min_wall_mm")
    taken = [p["position"][:2] for p in chip.get("ports", [])]
    best = None
    for k in range(0, 81):
        t = k / 80.0
        for x, y in ((margin + t * (fx - 2 * margin), margin),
                     (margin + t * (fx - 2 * margin), fy - margin),
                     (margin, margin + t * (fy - 2 * margin)),
                     (fx - margin, margin + t * (fy - 2 * margin))):
            if any(math.dist((x, y), q) < pitch for q in taken):
                continue
            d = math.dist((x, y), (cx, cy))
            if best is None or d < best[0]:
                best = (d, [round(x, 3), round(y, 3), z])
    return best[1] if best else None


# --- candidate generation -----------------------------------------------------------------------

def _round_width(mm: float) -> float:
    return round(math.ceil(mm / WIDTH_ROUND_MM) * WIDTH_ROUND_MM, 4)


def _max_width_mm(chip: dict) -> float:
    """The widest a feature on this board may be: the short side, less a wall each end.

    A hard bound, not a nicety. The synthetic over-shear case is 391x its ceiling, so
    "widen until the shear is legal" asks for a 1.46 METRE chamber - and mesh.py dutifully
    tries to discretise it at eight cells across before anything gets as far as the
    fabrication gate. Clamping here keeps the prediction cheap and lets the `cleared` gate
    report honestly that the widest legal feature still does not do it.
    """
    footprint = chip.get("footprint") or {}
    short = min(float(footprint.get("x", 0) or 0), float(footprint.get("y", 0) or 0))
    if not short:
        short = contracts.envelope("demo").y_mm
    return max(short - 2 * contracts.floor("min_wall_mm"),
               contracts.floor("min_open_channel_mm"))


def _clamp_width(chip: dict, width_mm: float) -> tuple[float, bool]:
    """(width, was_clamped) - never wider than the board can hold."""
    cap = _max_width_mm(chip)
    return (cap, True) if width_mm > cap else (width_mm, False)


def _feature_id(finding: dict) -> str | None:
    """The CHIP-SPEC feature a fix has to change.

    `group_findings` resolves analyse.py's mesh id (`d:comb_out_n`, `c:cx_n:3:1`) to the spec
    id through the mesh and stores it as `spec_feature_id`. A finding that never went through
    grouping is taken at face value.
    """
    return finding.get("spec_feature_id") or finding.get("feature_id")


def _shear_candidates(finding: dict, chip: dict, protocol: dict, step: dict) -> list[Candidate]:
    """Meet the ceiling at FIX_HEADROOM: cut the flow, or widen the chamber.

    tau = 6 mu Q / (w h^2) and h is fixed by the one etch depth, so both knobs are linear:
    tau ~ Q and tau ~ 1/w. The ceiling itself is the finding's (analyse.py reads it from
    contracts.shear_ceiling_for(); when the finding does not carry one, this does the same).
    """
    out: list[Candidate] = []
    value, target = finding.get("value"), target_value(finding)
    q0 = step.get("flow_ul_min")
    feature = _feature_id(finding)
    if not isinstance(value, (int, float)) or target is None or not value:
        return out
    ratio = target / value
    if isinstance(q0, (int, float)) and q0 and ratio < 1.0:
        out.append(Candidate(
            finding["id"], "set_flow", step.get("target") or feature or "",
            {"step": _step_ref(protocol, step), "protocol_step": step.get("step"), "flow_ul_min": round(q0 * ratio, 4)}, "flow",
            f"wall shear is linear in flow, so {q0:g} -> {q0 * ratio:.4g} uL/min puts "
            f"{feature} at {FIX_HEADROOM:g} x its "
            f"{finding.get('limit'):g} {finding.get('units', '')} ceiling",
            scaling="tau = 6 mu Q / (w h^2), h fixed by the single etch depth",
            knob="flow_ul_min", exponent=1.0))
    if ratio >= 1.0:
        return out
    channel = _channel(chip, feature)
    if channel:
        w0 = channel["width_mm"]
        out.append(Candidate(
            finding["id"], "widen_channel", channel["id"],
            {"width_mm": _round_width(w0 / ratio)}, "geometry",
            f"widening the {channel['id']} duct from {w0:g} to "
            f"{_round_width(w0 / ratio):g} mm drops its wall shear by the same factor at "
            f"the same flow, so the step keeps its duration",
            scaling="tau ~ 1/w at fixed depth and flow",
            knob="width_mm", exponent=-1.0))
    chamber = _chamber(chip, feature)
    if chamber:
        w0 = chamber["size"][1]
        out.append(Candidate(
            finding["id"], "widen_chamber", chamber["id"],
            {"width_mm": _round_width(w0 / ratio)}, "geometry",
            f"widening {chamber['id']} across the flow from {w0:g} to "
            f"{_round_width(w0 / ratio):g} mm drops wall shear by the same factor",
            scaling="tau ~ 1/w at fixed depth and flow",
            knob="width_mm", exponent=-1.0))
    return out


def _leak_candidates(finding: dict, chip: dict, protocol: dict, step: dict,
                     sol: Solution | None = None) -> list[Candidate]:
    """Lower the flow, or widen whatever is dropping the pressure (dP ~ 1/(w h^3))."""
    out: list[Candidate] = []
    value, target = finding.get("value"), target_value(finding)
    q0 = step.get("flow_ul_min")
    if not isinstance(value, (int, float)) or target is None or not value:
        return out
    ratio = target / value
    if isinstance(q0, (int, float)) and q0 and ratio < 1.0:
        out.append(Candidate(
            finding["id"], "set_flow", step.get("target") or "",
            {"step": _step_ref(protocol, step), "protocol_step": step.get("step"), "flow_ul_min": round(q0 * ratio, 4)}, "flow",
            f"Stokes flow: dP is linear in Q, so {q0:g} -> {q0 * ratio:.4g} uL/min brings "
            f"the pressure to {FIX_HEADROOM:g} x the limit",
            scaling="dP = Q R", knob="flow_ul_min", exponent=1.0))
    cid = _feature_id(finding) if _channel(chip, _feature_id(finding)) else None
    cid = cid or (_restricting_channel(chip, sol) if sol else None)
    ch = _channel(chip, cid)
    if ch and ratio < 1.0:
        w0 = ch["width_mm"]
        out.append(Candidate(
            finding["id"], "widen_channel", ch["id"],
            {"width_mm": _round_width(w0 / ratio)}, "geometry",
            f"{ch['id']} drops the most pressure; dP ~ 1/(w h^3) and h is fixed, so "
            f"{w0:g} -> {_round_width(w0 / ratio):g} mm takes the same factor out",
            scaling="dP ~ 1/(w h^3), h fixed by the single etch depth",
            knob="width_mm", exponent=-1.0))
    return out


def _clog_candidates(finding: dict, chip: dict, protocol: dict, step: dict) -> list[Candidate]:
    """Widen to the particle rule: capsule_channel.width_factor_low x capsule diameter."""
    ch = _channel(chip, _feature_id(finding))
    if not ch:
        return []
    rule_um = contracts.capsule_diameter_um() * contracts.param("capsule_channel.width_factor_low")
    limit = finding.get("limit")
    need_um = max(rule_um, limit if isinstance(limit, (int, float)) and
                  str(finding.get("units", "")).lower().startswith("um") else 0.0)
    width_mm = _round_width(need_um / 1000.0)
    if width_mm <= ch["width_mm"]:
        return []
    return [Candidate(
        finding["id"], "widen_channel", ch["id"], {"width_mm": width_mm}, "geometry",
        f"{ch['id']} must pass a {contracts.capsule_diameter_um():g} um capsule at "
        f"{contracts.param('capsule_channel.width_factor_low'):g}x, i.e. {need_um:g} um; "
        f"capsules swell, so the rule is on diameter, not on the nominal width",
        scaling="contracts: capsule_channel.width_factor_low x capsule.diameter_um")]


def _sediment_candidates(finding: dict, chip: dict, protocol: dict, step: dict,
                         ceiling: dict | None = None, sol: Solution | None = None
                         ) -> list[Candidate]:
    """Raise the flow to beat settling IF the ceilings allow it, else shorten the line or
    turn the chip on its side.

    A particle stays in the stream if it crosses before it falls: transit = L/u must beat
    fall = h/v_s. u is linear in Q, so the flow has to rise by transit/fall - and that ratio
    comes from the solved field through `edge_kinematics`, not from the finding's prose.

    Two ceilings can refuse it. Wall shear bounds a chamber with cells in it
    (`shear_flow_ceiling`), and a capsule line is bounded by pressure instead -
    `capsule.escape_pressure_kpa` x `control.pressure_margin` - which is exactly Module 4's
    question. Both are checked here to pick the candidate, and both are re-checked by the
    gates on the re-solve, because this estimate is linear and the network is not.

    When neither flow nor length can do it, `vertical_mounting` says so: gravity is across
    the depth because the board lies flat, and a particle in a vertical channel settles
    ALONG the flow instead of across it. That is a fixture change, not a chip change, and
    the catalogue can now express it instead of staying silent.
    """
    out: list[Candidate] = []
    q0 = step.get("flow_ul_min")
    spec_id = _feature_id(finding) or ""
    verdict = sediment_verdict(finding, sol, chip) if sol else None
    f_target = target_value(finding) or ZERO_LIMIT_TARGET
    ratio: float | None = None
    end_total_loss: float | None = None
    detail = ""
    depth_um = None
    if verdict is not None and verdict.fall_time_s:
        # A particle starting anywhere across the depth reaches the floor during transit if
        # it has time to fall that far, so the lost fraction is transit/fall (saturating at
        # 1). Both times are particles.py's, recomputed on this module's re-solved velocity.
        # To lose only `f_target` of the population, transit must be f_target x fall - so
        # the flow has to rise by (transit/fall) / f_target. That is a harsh number, and it
        # is the true one: a shallow HORIZONTAL channel loses particles, and saying "8x"
        # when the target needs "800x" would be the comfortable lie.
        over = verdict.transit_time_s / verdict.fall_time_s
        ratio = over / f_target
        end_total_loss = over
        depth_um = verdict.depth_um
        detail = (f"transit {verdict.transit_time_s:.3g} s at u {verdict.u_mm_s:.3g} mm/s "
                  f"against a {verdict.fall_time_s:.3g} s fall through "
                  f"{verdict.depth_um:.0f} um for the {verdict.particle} "
                  f"({verdict.diameter_um:g} um, {verdict.diameter_end} end), so "
                  f"{verdict.settled_fraction:.0%} is lost; {over:.3g}x stops ALL of it "
                  f"being lost and {ratio:.3g}x reaches the {f_target:g} target")
    if ratio is None:
        value = _scalar(finding.get("value"))
        if value and value > f_target:
            ratio = value / f_target
            detail = f"settled fraction {value:g} against a {f_target:g} target"
    caps = _flow_ceilings(chip, step, sol, ceiling)
    allowed, allowed_why = (min(((c["max_flow_ul_min"], c["why"]) for c in caps),
                                key=lambda kv: kv[0]) if caps else (None, ""))

    if ratio and ratio > 1.0 and isinstance(q0, (int, float)) and q0:
        want = round(q0 * ratio, 4)
        headroom = (f"{allowed:.4g} uL/min, set by {allowed_why}" if allowed is not None
                    else "no ceiling this module can compute")
        if allowed is None or want <= allowed:
            out.append(Candidate(
                finding["id"], "raise_flow", spec_id or step.get("target") or "",
                {"step": _step_ref(protocol, step), "protocol_step": step.get("step"),
                 "flow_ul_min": want,
                 "ends_total_loss_at_ul_min": (None if end_total_loss is None
                                               else round(q0 * end_total_loss, 4)),
                 "ceilings_ul_min": [{"why": c["why"], "max_flow_ul_min": round(c["max_flow_ul_min"], 4)}
                                     for c in caps]}, "flow",
                f"{detail}; u is linear in flow, so {q0:g} -> {want:g} uL/min "
                f"({ratio:.3g}x) crosses before it falls, and the tightest ceiling is "
                f"{headroom}",
                scaling="transit = L / u, u = Q / (w h); fall = h / v_s (particles.py)",
                knob="flow_ul_min", exponent=-1.0))
        # settled_fraction FALLS as flow rises, so the slope is -1, not +1. With +1 the
        # secant pushed the flow the wrong way on every correction.
        else:
            out.append(Candidate(
                finding["id"], "raise_flow", spec_id or step.get("target") or "",
                {"step": _step_ref(protocol, step), "protocol_step": step.get("step"),
                 "flow_ul_min": round(allowed, 4),
                 "needed_ul_min": want, "short_by": round(want / allowed, 3),
                 "ends_total_loss_at_ul_min": (None if end_total_loss is None
                                               else round(q0 * end_total_loss, 4)),
                 "ceilings_ul_min": [{"why": c["why"], "max_flow_ul_min": round(c["max_flow_ul_min"], 4)}
                                     for c in caps]}, "flow",
                f"{detail}; beating it on flow needs {want:g} uL/min but the tightest "
                f"ceiling is {headroom}, so flow alone falls {want / allowed:.3g}x short - "
                f"this candidate runs the step at the ceiling and will NOT clear the finding",
                scaling="transit = L / u; the ceiling is the binding constraint, not the law",
                knob="flow_ul_min", exponent=0.0))   # pinned at the ceiling: nothing to refine
        # Always offered, not only when the flow fails: a human reading a settling finding
        # needs the alternatives in front of them, and the gates decide which is automatic.
        out.append(Candidate(
            finding["id"], "shorten_line", spec_id,
            {"length_scale": round(1.0 / ratio, 6), "flow_needed_ul_min": want,
             "flow_allowed_ul_min": None if allowed is None else round(allowed, 4),
             "length_scale_to_end_total_loss": (None if end_total_loss is None
                                                else round(1.0 / end_total_loss, 4)),
             "allowed_by": allowed_why}, "geometry",
            f"{ratio:.3g}x shorter restores the margin at the flow the step already runs: "
            f"a particle only has to stay up for the time it is actually in transit. In "
            f"pipeline mode this is an S7 re-route, not an edit to a hand-designed board",
            scaling="transit time ~ L at fixed u"))
        out.append(Candidate(
            finding["id"], "vertical_mounting", spec_id,
            {"axis": "mount the board with this line vertical",
             "flow_needed_ul_min": want,
             "flow_allowed_ul_min": None if allowed is None else round(allowed, 4),
             "where": SPEC_HOME["vertical_mounting"]}, "spec",
            f"the board lies FLAT, so gravity is across the "
            f"{depth_um or 0:.0f} um depth and the particle only has to fall that far. "
            f"Mounted with this line vertical it settles ALONG the flow, and the "
            f"{ratio:.3g}x the flow cannot safely deliver is not needed. Costs nothing in "
            f"geometry and nothing in flow - it is a fixture decision",
            scaling="settling is across the depth only while the board is horizontal"))
    if not out:
        out.append(Candidate(
            finding["id"], "shorten_line", spec_id, {"length_scale": None}, "geometry",
            "shorten the settling line; the solve could not produce a transit time for "
            "this feature, so by how much is particles.py's call",
            scaling="transit time ~ L / u"))
    return out


def _flow_ceilings(chip: dict, step: dict, sol: Solution | None,
                   shear: dict | None = None) -> list[dict]:
    """Every ceiling on this step's flow, so a raise can be checked against all of them.

    Three bite, and they bite different chips: wall shear where there are cells, escape
    pressure where there are capsules, and Reynolds everywhere - because above Re 1 the
    Stokes field that predicted the fix is no longer the right physics, which makes it a
    validity ceiling and not a taste one.
    """
    out = []
    if shear and shear.get("max_flow_ul_min") is not None:
        out.append({"max_flow_ul_min": shear["max_flow_ul_min"],
                    "why": shear.get("why", "the wall-shear ceiling"),
                    "set_by": shear.get("set_by")})
    for cap in (_pressure_flow_ceiling(chip, step, sol),
                _reynolds_flow_ceiling(step, sol)):
        if cap.get("max_flow_ul_min") is not None:
            out.append(cap)
    return out


def _reynolds_flow_ceiling(step: dict, sol: Solution | None) -> dict:
    """The largest flow that keeps every edge at Re <= 1.

    Re is linear in flow, so if the solved step reaches Re at q it reaches 1 at q / Re. This
    is the ceiling that answers Module 4's feed lines: pressure leaves enormous headroom, and
    Reynolds does not.
    """
    q0 = step.get("flow_ul_min")
    if sol is None or not sol.reynolds or not isinstance(q0, (int, float)) or not q0:
        return {}
    worst_id, worst = max(sol.reynolds.items(), key=lambda kv: kv[1])
    if worst <= 0:
        return {}
    return {
        "max_flow_ul_min": q0 * (1.0 * FIX_HEADROOM) / worst,
        "at_re": worst,
        "set_by": worst_id,
        "why": f"the Re 1 validity limit of the Stokes model (Re {worst:.3g} at {worst_id} "
               f"at {q0:g} uL/min)",
    }


def _pressure_flow_ceiling(chip: dict, step: dict, sol: Solution | None) -> dict:
    """The largest flow that keeps every port under escape pressure x margin.

    Stokes flow is linear, so if the solved step reaches p at q it reaches the limit at
    q x limit / p. This is the ceiling that decides Module 4's question - whether the feed
    lines can simply be run faster - and it is the contract's number, through
    contracts.max_working_pressure_kpa(), not a literal.
    """
    q0 = step.get("flow_ul_min")
    if sol is None or not isinstance(q0, (int, float)) or not q0 or not sol.max_pressure_kpa:
        return {}
    limit = contracts.max_working_pressure_kpa()
    return {
        "max_flow_ul_min": q0 * (limit * FIX_HEADROOM) / sol.max_pressure_kpa,
        "limit_kpa": limit,
        "at_kpa": sol.max_pressure_kpa,
        "why": f"the {limit:.4g} kPa escape-pressure limit "
               f"(capsule.escape_pressure_kpa x control.pressure_margin)",
        "assumption": contracts.is_assumption("capsule.escape_pressure_kpa"),
    }


def _retention_candidates(finding: dict, chip: dict, protocol: dict, step: dict,
                          sol: Solution | None = None) -> list[Candidate]:
    """The depth weir: a PROCESS WINDOW problem, so the fix is a spec change for a human.

    particles.py sweeps the gap over the +/-20 um z tolerance and reports the band that still
    retains - for module3's `main_trap`, 5.0-26.8 um, which is -20 to +1.8 um of the window,
    so 55% of it. Nothing in the chip spec is wrong: the nominal is simply not centred in its
    own working band, and the band is narrower than the tolerance. There is no channel to
    widen and no flow to lower.

    Two honest answers, both swept rather than guessed:
      tighten_z_tolerance  centre the nominal in the band and hold z to half its width.
      larger_capsule       particles.capsule_size_for_full_tolerance() - a bigger capsule
                           scales the whole band, and it says whether that is reachable
                           inside the contract's tunable range.
    """
    out: list[Candidate] = []
    mod = _sibling("particles")
    band = finding.get("value")
    lo, hi = (band + [None, None])[:2] if isinstance(band, list) else (None, None)
    feature = _feature_id(finding) or ""
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)) and hi > lo:
        nominal = (lo + hi) / 2.0
        tol = (hi - lo) / 2.0
        stated = _chamber(chip, feature) or {}
        now = (stated.get("retention") or {}).get("opening_um")
        out.append(Candidate(
            finding["id"], "tighten_z_tolerance", feature,
            {"nominal_gap_um": round(nominal, 1), "z_tolerance_um": round(tol, 1),
             "working_band_um": [lo, hi], "from_nominal_um": now,
             "where": SPEC_HOME["tighten_z_tolerance"]}, "spec",
            f"{feature} retains only for gaps {lo:g}-{hi:g} um while the nominal is "
            f"{now or '?'} um, so the window sits at the bottom of its own band. Centre it: "
            f"a {nominal:.1f} um nominal held to +/-{tol:.1f} um uses the whole band. That "
            f"is a fabrication spec and a coupon run, not a chip edit - the chip is right",
            scaling="the working band is swept by particles.weir_tolerance_band; the "
                    "nominal and tolerance are its centre and half-width"))
    if mod is not None:
        try:
            sizing = mod.capsule_size_for_full_tolerance(sol.mu_pa_s if sol else None)
        except Exception:
            sizing = None
        if sizing:
            reachable = sizing.get("retention_bound_only") or {}
            out.append(Candidate(
                finding["id"], "larger_capsule", feature,
                {"diameter_um_needed": reachable.get("diameter_um_needed"),
                 "nominal_gap_um": reachable.get("nominal_gap_um"),
                 "achievable": reachable.get("achievable"),
                 "with_contract_band": sizing.get("with_contract_band"),
                 "where": SPEC_HOME["larger_capsule"]}, "spec",
                f"a bigger capsule scales the whole band: "
                f"{reachable.get('why', 'see particles.capsule_size_for_full_tolerance')}",
                scaling="retention limit = trap.gap_factor_high x diameter, so the band "
                        "scales with the capsule"))
    return out


def _trap_candidates(finding: dict, chip: dict, protocol: dict, step: dict) -> list[Candidate]:
    """A Hele-Shaw trap is a low-velocity or long-residence zone, so every fix here changes
    where the liquid is made to go: move the outlet to the far face, feed the chamber across
    its width instead of at a point, break the slow corner with pillars, or change the
    aspect ratio so there is no far side to be slow."""
    out: list[Candidate] = []
    chamber = _chamber(chip, _feature_id(finding))
    if not chamber:
        return out
    inlet = _nearest(chip, chamber["id"], {p["id"] for p in chip.get("ports", [])
                                           if p.get("type") == "inlet"})
    outlet = _nearest(chip, chamber["id"], {p["id"] for p in chip.get("ports", [])
                                            if p.get("type") == "outlet"})
    if inlet and outlet and _port(chip, inlet) and _port(chip, outlet):
        ip = _port(chip, inlet)["position"]
        cx, cy = chamber["center"][:2]
        sx, sy = chamber["size"][:2]
        # Opposite face: reflect the inlet's approach through the chamber centre.
        on_x = abs(ip[0] - cx) >= abs(ip[1] - cy)
        far = [cx - math.copysign(sx, ip[0] - cx) if on_x else cx,
               cy if on_x else cy - math.copysign(sy, ip[1] - cy)]
        out.append(Candidate(
            finding["id"], "move_outlet", outlet,
            {"opposite_to": inlet, "toward": [round(v, 3) for v in far]}, "geometry",
            f"{chamber['id']} is fed from {inlet} and drained at {outlet} on the same side, "
            f"so the far end never sees flow; put the drain on the opposite face",
            scaling="Hele-Shaw potential flow: no eddies, so the only cure is a path"))
    out.append(Candidate(
        finding["id"], "add_flow_distributor", chamber["id"],
        {"block": "flow_distributor", "spans": "inlet face", "width_mm": chamber["size"][1]},
        "geometry",
        f"feed {chamber['id']} across its full width instead of at one point; a comb or "
        f"distributor is what turns a point source into a front",
        scaling="uniform inlet front removes the stagnation lobe"))
    out.append(Candidate(
        finding["id"], "add_pillar_row", chamber["id"],
        {"block": "pillar_array", "where": "across the slow zone"}, "geometry",
        f"a pillar row across the slow zone of {chamber['id']} splits the streamlines and "
        f"raises the local velocity",
        scaling="local resistance redistributes the depth-averaged flow"))
    sx, sy = chamber["size"][:2]
    if sy > 0:
        area = sx * sy
        # Long and narrow: a chamber fed at one end has no far side to be slow.
        new_sy = _round_width(math.sqrt(area / 4.0))
        out.append(Candidate(
            finding["id"], "change_aspect_ratio", chamber["id"],
            {"size_mm": [round(area / new_sy, 3), new_sy], "keeps_volume": True}, "geometry",
            f"{chamber['id']} is {sx:g} x {sy:g} mm; the same area as "
            f"{area / new_sy:.3g} x {new_sy:g} mm has no far side to stagnate",
            scaling="aspect ratio at constant area and depth, so volume is unchanged"))
    return out


def _dead_end_candidates(finding: dict, chip: dict, protocol: dict, step: dict) -> list[Candidate]:
    """A vent if the port budget allows, else taper the expansion so it primes."""
    out: list[Candidate] = []
    feature = (_chamber(chip, _feature_id(finding))
               or _channel(chip, _feature_id(finding))
               or _port(chip, _feature_id(finding)))
    scale = str(chip.get("scale") or "demo")
    limit = contracts.max_ports_per_chip(scale if scale in ("demo", "target", "ceiling") else "demo")
    have = len(chip.get("ports", []))
    budget = limit is None or have < limit
    if feature and budget:
        pos = _free_edge_position(chip, feature)
        if pos:
            out.append(Candidate(
                finding["id"], "add_vent", _feature_id(finding) or "",
                {"port": {"id": f"vent_{_feature_id(finding)}", "type": "outlet",
                          "role": "vent", "position": pos,
                          "width_um": contracts.floor("min_port_mm") * 1000},
                 "channel_width_mm": contracts.floor("min_open_channel_mm")}, "geometry",
                f"{_feature_id(finding)} has nowhere for gas to go; the board has "
                f"{have} of {limit} ports, so a vent fits",
                scaling="a dead end primes only if the trapped gas can leave"))
        else:
            out.append(Candidate(
                finding["id"], "add_vent", _feature_id(finding) or "",
                {"port": None, "why_blocked": "no edge position keeps the "
                 f"{contracts.floor('min_port_pitch_mm'):g} mm port pitch"}, "geometry",
                f"a vent is the right fix but no legal port position is free on this "
                f"{chip['footprint']['x']:g} x {chip['footprint']['y']:g} mm board",
                scaling="a dead end primes only if the trapped gas can leave"))
    elif feature:
        out.append(Candidate(
            finding["id"], "add_vent", _feature_id(finding) or "",
            {"port": None, "why_blocked": f"port budget is {limit} and the chip has {have}"},
            "geometry",
            f"a vent would fix it but the port budget is full ({have} of {limit}); either "
            f"move to the target envelope, where the contract sets no port limit, or taper",
            scaling="a dead end primes only if the trapped gas can leave"))
    out.append(Candidate(
        finding["id"], "taper_expansion", _feature_id(finding) or "",
        {"taper": "gradual", "why": "a sudden expansion pins the meniscus"}, "geometry",
        f"taper the expansion into {_feature_id(finding)} so the meniscus advances "
        f"instead of pinning and leaving a bubble",
        scaling="capillary pressure vs drive pressure at the expansion"))
    return out


def _exchange_candidates(finding: dict, chip: dict, protocol: dict, step: dict) -> list[Candidate]:
    """More volume, or more washes: both are protocol-only."""
    out: list[Candidate] = []
    value, target = finding.get("value"), target_value(finding)
    vol = step.get("volume_ul")
    if isinstance(vol, (int, float)) and vol and isinstance(value, (int, float)) and value and target:
        # Washout is exponential, so the volume needed for a target fraction goes as
        # ln(1 - f). transport.py owns the real curve; this is the first guess only.
        try:
            scale = math.log(1 - min(target, 0.999)) / math.log(1 - min(value, 0.999))
        except (ValueError, ZeroDivisionError):
            scale = target / value
        out.append(Candidate(
            finding["id"], "more_volume", step.get("target") or "",
            {"step": _step_ref(protocol, step), "protocol_step": step.get("step"), "volume_ul": round(vol * max(scale, 1.0), 4)}, "flow",
            f"exchange reached {value:g} of a {finding.get('limit')} target; a well-mixed "
            f"washout needs ln(1-f) more volume, about {max(scale, 1.0):.2f}x",
            scaling="exchange fraction f = 1 - exp(-V/V_chamber)", knob="volume_ul"))
    washes = step.get("washes")
    out.append(Candidate(
        finding["id"], "more_washes", step.get("target") or "",
        {"step": _step_ref(protocol, step), "protocol_step": step.get("step"), "washes": (washes or 1) + 1}, "flow",
        f"one more wash on step {step.get('step')}: "
        f"{(washes or 1)} -> {(washes or 1) + 1}, which clears the interior faster than one "
        f"long flush at the same total volume",
        scaling="contracts: wash.washes_per_exchange, exchange.interior_target_fraction"))
    return out


def shear_flow_ceiling(chip: dict, protocol: dict, step: dict, findings: list[dict]) -> dict:
    """The largest flow the shear ceilings allow on this step, for the sediment trade-off.

    Derived from the chip and the contract, never from a literal: tau = 6 mu Q / (w h^2) so
    Q_max = tau_max w h^2 / (6 mu), taken over every shear-bounded chamber on the step.
    """
    mu, _ = viscosity_for_step(step)
    best: float | None = None
    worst_feature = None
    for chamber in chip.get("chambers", []):
        klass = shear_class_for(chip, chamber, step)
        ceiling = contracts.shear_ceiling_for(klass).ceiling_dyn_cm2
        if ceiling is None:
            continue
        q = _network.threshold_flow(chamber["size"][1], chamber["size"][2], ceiling, mu)
        if best is None or q < best:
            best, worst_feature = q, chamber["id"]
    if best is None:
        return {}
    return {"max_flow_ul_min": best * FIX_HEADROOM, "set_by": worst_feature}


GENERATORS: dict[str, Callable[..., list[Candidate]]] = {
    "shear": _shear_candidates,
    "leak": _leak_candidates,
    "clog": _clog_candidates,
    "trap": _trap_candidates,
    "dead_end": _dead_end_candidates,
    "bubble": _dead_end_candidates,
    "sediment": _sediment_candidates,
    "exchange": _exchange_candidates,
    "retention": _retention_candidates,
}

BAND_METRICS = ("working_gap_band_um",)
"""Metrics whose value is a WINDOW, not a number.

particles.py files the weir sweep as class `leak` because a gap that is too wide leaks
capsules - but the thing that is wrong is a tolerance window, so it is routed to the
`retention` generator and gets a spec change rather than a flow cut. Checking the metric
rather than the class keeps that decision in one place instead of second-guessing
analyse.py's taxonomy."""


def effective_class(finding: dict) -> str:
    """The catalogue key for a finding: its class, aliased, and band metrics re-routed."""
    klass = CLASS_ALIASES.get(finding.get("class", ""), finding.get("class", ""))
    metric = str(finding.get("metric") or "")
    if metric in BAND_METRICS or isinstance(finding.get("value"), list):
        return "retention"
    return klass


def candidates_for(finding: dict, chip: dict, protocol: dict, sol: Solution | None = None,
                   step: dict | None = None) -> list[Candidate]:
    """Every candidate for one finding, in the order the catalogue tries them."""
    klass = effective_class(finding)
    gen = GENERATORS.get(klass)
    if gen is None:
        return []
    step = step if step is not None else (_step(protocol, finding.get("step")) or {})
    kwargs: dict[str, Any] = {}
    if klass in ("leak", "retention"):
        kwargs["sol"] = sol
    if klass == "sediment":
        kwargs["ceiling"] = shear_flow_ceiling(chip, protocol, step, [finding])
        kwargs["sol"] = sol
    built = gen(finding, chip, protocol, step, **kwargs)
    order = {a: i for i, a in enumerate(ACTIONS.get(klass, ()))}
    return sorted(built, key=lambda c: order.get(c.action, len(order)))


# --- applying a candidate to a COPY --------------------------------------------------------------

class FixError(ValueError):
    """A candidate that cannot be expressed as a parameter change on the spec."""


def apply_candidate(chip: dict, protocol: dict, cand: Candidate,
                    knob_value: float | None = None) -> tuple[dict, dict]:
    """Return (chip, protocol) COPIES with the candidate applied. Inputs are never mutated."""
    chip, protocol = copy.deepcopy(chip), copy.deepcopy(protocol)
    params = dict(cand.params)
    if knob_value is not None and cand.knob:
        params[cand.knob] = knob_value
    action = cand.action

    if action in ("set_flow", "raise_flow"):
        step = _step(protocol, params.get("step"))
        if step is None:
            raise FixError(f"{action}: protocol has no step {params.get('step')!r}")
        old = step.get("flow_ul_min")
        step["flow_ul_min"] = round(float(params["flow_ul_min"]), 4)
        _restate_duration(step, old)
        _note(step, f"flow changed from {old:g} to {step['flow_ul_min']:g} uL/min by "
                    f"assay2chip/cfd/fix.py ({cand.finding_id})")
        return chip, protocol

    if action == "more_volume":
        step = _step(protocol, params.get("step"))
        if step is None:
            raise FixError("more_volume: step not found")
        old = step.get("volume_ul")
        step["volume_ul"] = round(float(params["volume_ul"]), 4)
        _restate_duration(step, None)
        _note(step, f"volume raised from {old} to {step['volume_ul']:g} uL ({cand.finding_id})")
        return chip, protocol

    if action == "more_washes":
        step = _step(protocol, params.get("step"))
        if step is None:
            raise FixError("more_washes: step not found")
        old = step.get("washes")
        step["washes"] = int(params["washes"])
        _note(step, f"washes raised from {old} to {step['washes']} ({cand.finding_id})")
        return chip, protocol

    if action == "widen_channel":
        ch = _channel(chip, cand.target)
        if ch is None:
            raise FixError(f"widen_channel: no channel {cand.target!r}")
        width, capped = _clamp_width(chip, float(params["width_mm"]))
        if width <= ch["width_mm"]:
            raise FixError(
                f"widen_channel: {cand.target} is already {ch['width_mm']:g} mm and the "
                f"board holds at most {_max_width_mm(chip):g} mm, so there is nothing to "
                f"widen into - this needs a bigger envelope or a different fix")
        ch["width_mm"] = round(width, 4)
        if capped:
            ch["width_capped_by_envelope_mm"] = round(_max_width_mm(chip), 4)
        return chip, protocol

    if action == "widen_chamber":
        chamber = _chamber(chip, cand.target)
        if chamber is None:
            raise FixError(f"widen_chamber: no chamber {cand.target!r}")
        old_w = chamber["size"][1]
        width, capped = _clamp_width(chip, float(params["width_mm"]))
        if width <= old_w:
            raise FixError(
                f"widen_chamber: {cand.target} is already {old_w:g} mm across the flow and "
                f"the board holds at most {_max_width_mm(chip):g} mm, so there is nothing "
                f"to widen into - this needs a bigger envelope or a different fix")
        chamber["size"][1] = round(width, 4)
        if capped:
            chamber["width_capped_by_envelope_mm"] = round(_max_width_mm(chip), 4)
        _rescale_volume(chamber, old_w, protocol, cand)
        return chip, protocol

    if action == "change_aspect_ratio":
        chamber = _chamber(chip, cand.target)
        if chamber is None:
            raise FixError(f"change_aspect_ratio: no chamber {cand.target!r}")
        old_w = chamber["size"][1]
        chamber["size"][0] = round(float(params["size_mm"][0]), 4)
        chamber["size"][1] = round(float(params["size_mm"][1]), 4)
        _rescale_volume(chamber, old_w, protocol, cand)
        return chip, protocol

    if action == "move_outlet":
        port = _port(chip, cand.target)
        if port is None or not params.get("toward"):
            raise FixError(f"move_outlet: no port {cand.target!r} or no target face")
        x, y = params["toward"][:2]
        bore = contracts.floor("min_port_mm")
        margin = bore / 2 + contracts.floor("min_wall_mm")
        fx, fy = chip["footprint"]["x"], chip["footprint"]["y"]
        port["position"] = [round(min(max(x, margin), fx - margin), 3),
                            round(min(max(y, margin), fy - margin), 3),
                            port["position"][2]]
        return chip, protocol

    if action == "shorten_line":
        scale = params.get("length_scale")
        if not isinstance(scale, (int, float)) or not 0 < scale < 1:
            raise FixError(f"shorten_line: needs a length_scale in (0,1), got {scale!r}")
        ch = _channel(chip, cand.target)
        chamber = _chamber(chip, cand.target)
        if ch is None and chamber is not None:
            # A channel-like block stored as a chamber - a serpentine, a herringbone mixer -
            # is a DUCT at its lane width (mesh.py's rule), and its lane length comes from
            # the bounding box. So shortening it IS a resize of the box along the flow, and
            # the volume follows, which is why `_rescale_volume` is called on it.
            if not any(w in str(chamber.get("block", "")).lower()
                       for w in ("channel", "mixer", "serpentine", "herringbone")):
                raise FixError(
                    f"shorten_line: {cand.target!r} is a chamber, not a line. Shrinking a "
                    f"chamber changes what it holds, so the fix is S6 re-sizing it from the "
                    f"physics, not a length scale here")
            old_w = chamber["size"][1]
            chamber["size"][0] = round(chamber["size"][0] * scale, 4)
            _rescale_volume(chamber, old_w, protocol, cand)
            return chip, protocol
        if ch is None:
            raise FixError(f"shorten_line: no channel or channel-like chamber "
                           f"{cand.target!r}")
        if ch.get("path"):
            raise FixError("shorten_line: this channel has an explicit routed path, so its "
                           "length belongs to S7's router, not to a scale factor here")
        # No explicit path: the length is the distance between the parts, so shortening it
        # means moving the free end. Only a PORT may move - dragging a chamber would change
        # its volume's place in the layout, which is S6's and S7's job, not a local edit.
        free = _port(chip, ch["from"]) or _port(chip, ch["to"])
        other = (_chamber(chip, ch["to"]) or _port(chip, ch["to"])
                 if free and free["id"] == ch["from"]
                 else _chamber(chip, ch["from"]) or _port(chip, ch["from"]))
        if free is None or other is None:
            raise FixError("shorten_line: neither end of this channel is a port, so the "
                           "length is set by where two parts sit - an S7 re-route")
        anchor = (other.get("center") or other.get("position"))[:2]
        x0, y0 = free["position"][:2]
        free["position"] = [round(anchor[0] + (x0 - anchor[0]) * scale, 3),
                            round(anchor[1] + (y0 - anchor[1]) * scale, 3),
                            free["position"][2]]
        return chip, protocol

    if action == "add_vent":
        spec = params.get("port")
        if not spec:
            raise FixError("add_vent: no legal port position (see params.why_blocked)")
        chip.setdefault("ports", []).append(copy.deepcopy(spec))
        chip.setdefault("channels", []).append({
            "id": f"ch_{spec['id']}", "from": cand.target, "to": spec["id"],
            "width_mm": float(params.get("channel_width_mm",
                                         contracts.floor("min_open_channel_mm"))),
            "depth_mm": contracts.max_depth_mm(), "block": "straight_channel",
            "carries": "signals"})
        return chip, protocol

    if action in SPEC_ACTIONS:
        raise FixError(
            f"{action} is a SPEC change, not a chip or protocol edit: it belongs in "
            f"{SPEC_HOME.get(action, 'a process document')} and this module must not "
            f"apply it. See _predict_spec.")
    raise FixError(f"{action}: {NEEDS_SIBLING.get(action, 'no applier')} must land before "
                   f"this can be expressed as a parameter change")


def _note(step: dict, text: str) -> None:
    step["note"] = f"{step.get('note', '').rstrip()} [{text}]".strip()


def _restate_duration(step: dict, _old_flow: float | None) -> None:
    """Keep duration_s consistent with volume_ul / flow_ul_min.

    A flow change that leaves the old duration in place is a protocol that no longer
    describes itself - and the side_effects gate needs the new duration to judge whether a
    kinetically timed step has been stretched.
    """
    vol, flow = step.get("volume_ul"), step.get("flow_ul_min")
    if isinstance(vol, (int, float)) and isinstance(flow, (int, float)) and flow:
        washes = step.get("washes") or 1
        step["duration_s"] = round(vol * washes / flow * 60.0, 3)


def _rescale_volume(chamber: dict, old_width_mm: float, protocol: dict,
                    cand: Candidate) -> None:
    """A chamber that gets wider holds more, and an exchange step sized from its volume has
    to carry more liquid. Not rescaling is how a "fix" silently under-washes a chamber."""
    new = _box_volume_ul(chamber)
    stated = chamber.get("volume_ul")
    old_box = old_width_mm * chamber["size"][0] * chamber["size"][2]
    if isinstance(stated, (int, float)) and old_box and abs(stated - old_box) / old_box < 0.05:
        chamber["volume_ul"] = round(new, 4)
        factor = new / stated if stated else 1.0
        for step in protocol.get("steps", []):
            if step.get("target") == chamber["id"] and isinstance(step.get("volume_ul"), (int, float)):
                step["volume_ul"] = round(step["volume_ul"] * factor, 4)
                _restate_duration(step, None)
                _note(step, f"volume scaled x{factor:.3g} with {chamber['id']} "
                            f"({cand.finding_id})")
    else:
        chamber["volume_ul_note"] = ("volume_ul was not recomputed: it does not match the "
                                     "bounding box, so something else derived it")


# --- the six gates ---------------------------------------------------------------------------------

def _floor_errors(chip: dict) -> list[str]:
    """Fabrication-floor errors, from tools/check_chip_spec.py rather than a second copy.

    Port overlap is measured against the PATCHED SPEC: port_overlaps() would otherwise find
    the chip's exported DXF, which is the geometry before the fix.
    """
    f = _spec.Findings()
    _spec.check_blocks(chip, contracts.blocks(), f)
    polys = _spec._spec_outlines(chip)
    for r in _spec.port_overlaps(chip, contracts.blocks(), polys=polys,
                                 source="the patched spec"):
        if r["overlap_mm2"] + r["tolerance_mm2"] < r["need_mm2"]:
            f.error(f"port overlap: '{r['port']}' {r['side_mm']:g} mm hole opens onto "
                    f"{r['overlap_mm2']:.2f} mm2 (needs >= {r['need_mm2']:.2f} mm2)")
    return f.errors


def _planar(chip: dict) -> tuple[list[str], float]:
    """cad/make_figures.py's collision check. This module has no second implementation."""
    msgs, tightest = _figures.geometry_warnings(chip)
    return [m for m in msgs if m.startswith("ERROR")], tightest[0]


def gate_fabrication(before: dict, after: dict) -> Gate:
    was, now = _floor_errors(before), _floor_errors(after)
    new = [m for m in now if m not in was]
    if new:
        return Gate("fabrication", False, f"{len(new)} new floor error(s): {new[0]}")
    return Gate("fabrication", True,
                f"every fabrication floor kept ({len(was)} pre-existing error(s) unchanged)")


def gate_planar(before: dict, after: dict) -> Gate:
    was, tight_was = _planar(before)
    now, tight_now = _planar(after)
    new = [m for m in now if m not in was]
    if new:
        return Gate("planar", False, f"new planar collision: {new[0]}")
    wall = contracts.floor("min_wall_mm")
    if tight_now < min(wall, tight_was) - 1e-9:
        return Gate("planar", False,
                    f"tightest rib falls to {tight_now:.3f} mm, under the {wall:g} mm "
                    f"minimum wall (was {tight_was:.3f} mm)")
    return Gate("planar", True,
                f"no new crossing; tightest clearance {tight_now:.3f} mm "
                f"(was {tight_was:.3f} mm, min wall {wall:g} mm)")


def gate_measurable(before: float | None, after: float | None, cand: Candidate,
                    finding: dict | None = None) -> Gate:
    if before is None or after is None:
        owner = NEEDS_SIBLING.get(cand.action, "the metric's owning module")
        return Gate("measurable", False,
                    f"the re-solve cannot measure this metric yet: {owner} owns it")
    stated = _scalar((finding or {}).get("value"))
    if stated is not None and stated != 0.0:
        drift = abs(before - stated) / abs(stated)
        if drift > BEFORE_AGREEMENT:
            return Gate("measurable", False,
                        f"the re-solve reads {before:.4g} where the finding states "
                        f"{stated:.4g} ({drift:.0%} apart): this is not the state the "
                        f"finding describes, so nothing measured on it can be trusted - "
                        f"check which step the finding's `step` key refers to")
    if before == 0.0 and stated not in (None, 0.0):
        return Gate("measurable", False,
                    f"the re-solve reads zero where the finding states {stated:.4g}: the "
                    f"feature carries no flow in the step that was solved")
    return Gate("measurable", True, f"re-solved: {before:.4g} -> {after:.4g}")


def gate_cleared(finding: dict, after: float | None) -> Gate:
    if after is None:
        return Gate("cleared", False, "no after value")
    if within_limit(finding, after):
        return Gate("cleared", True,
                    f"{after:.4g} is inside the {finding.get('limit')} "
                    f"{finding.get('units', '')} limit at {FIX_HEADROOM:g} headroom")
    return Gate("cleared", False,
                f"{after:.4g} still misses the {finding.get('limit')} "
                f"{finding.get('units', '')} limit at {FIX_HEADROOM:g} headroom")


def gate_no_new_blocker(before: list[dict], after: list[dict], scope: Any = None) -> Gate:
    where = ("the whole protocol" if scope is None else
             "the worst-case step per target" if scope == WORST else f"step {scope}")

    def key(f: dict) -> tuple:
        return (f.get("class"), f.get("feature_id"), f.get("step"))

    was = {key(f) for f in before}
    new = [f for f in after if f.get("severity") == "blocker" and key(f) not in was]
    if new:
        return Gate("no_new_blocker", False,
                    f"the re-run over {where} raises a new blocker: {new[0].get('class')} on "
                    f"{new[0].get('feature_id')} ({str(new[0].get('message', ''))[:80]})")
    opened = [f for f in after if key(f) not in was]
    return Gate("no_new_blocker", True,
                f"no new blocker over {where} ({len(opened)} new non-blocking finding(s))")


RESIDENCE_BLOCKS = ("mixer", "serpentine", "herringbone", "incubat", "centipede", "spiral")
"""Features whose whole purpose is TIME in the stream. Speeding the flow through one of
these trades settling for mixing, which is a different experiment, not a fix."""


def gate_side_effects(cand: Candidate, protocol_before: dict, protocol_after: dict,
                      sol_before: Solution, sol_after: Solution, chip: dict) -> Gate:
    """The ways a numerically correct fix still breaks the step it fixes."""
    if cand.action == "raise_flow":
        # 1. A fix must not take the chip out of the model that predicted it. solve.py gives
        #    Reynolds, and above 1 the Stokes field this whole prediction rests on stops
        #    being the right physics - so a raise that crosses it is not a verified fix, it
        #    is an extrapolation. (Module 6's quench already lives up there; that is a
        #    finding, not something to create more of.)
        if not sol_after.reynolds:
            return Gate("side_effects", False,
                        "raising a flow needs Reynolds checked and this solve returned none "
                        "(the network fallback has no fluid density): never automatic")
        re_before = max(sol_before.reynolds.values(), default=0.0)
        re_after = max(sol_after.reynolds.values(), default=0.0)
        if re_after > 1.0 >= re_before:
            worst = max(sol_after.reynolds.items(), key=lambda kv: kv[1])[0]
            return Gate("side_effects", False,
                        f"the raise takes Re from {re_before:.3g} to {re_after:.3g} at "
                        f"{worst}: above 1 the Stokes model that predicted this fix is out "
                        f"of its validity, so the prediction cannot vouch for itself")
        # 2. Faster flow through a mixer is less mixing. Trading one finding for another.
        target = _chamber(chip, cand.target) or _channel(chip, cand.target)
        blocks = f"{(target or {}).get('block')} {(target or {}).get('type')}".lower()
        if any(word in blocks for word in RESIDENCE_BLOCKS):
            return Gate("side_effects", False,
                        f"{cand.target} is a residence-time feature ({(target or {}).get('block')}): "
                        f"raising the flow to beat settling cuts the time the stream spends "
                        f"in it, which is what the feature is for. Mixing time vs particle "
                        f"loss is a protocol decision")
    number = cand.params.get("step")
    was, now = _step(protocol_before, number), _step(protocol_after, number)
    if was and now:
        d0, d1 = was.get("duration_s"), now.get("duration_s")
        if isinstance(d0, (int, float)) and isinstance(d1, (int, float)) and d1 > d0 * 1.05:
            if d0 < KINETIC_STEP_S:
                return Gate("side_effects", False,
                            f"step {number} is {d0:g} s, under the {KINETIC_STEP_S:g} s a "
                            f"kinetically timed step takes; this change stretches it to "
                            f"{d1:g} s, which is a chemistry decision, not a flow decision")
    return Gate("side_effects", True,
                "no step stretched past a kinetic limit, no residence-time feature rushed, "
                "Reynolds still inside the Stokes model")


# --- the fallback analyser (analyse.py owns the real one) -------------------------------------------

LYSE_WORDS = ("lyse", "lysis", "quench", "solubilis", "denatur", "gitc", "extract")


def shear_class_for(chip: dict, chamber: dict, step: dict) -> str:
    """Which of the three ceilings bounds this chamber on this step.

    HEURISTIC, and labelled as one. The authority is unitops.json's `shear_class` carried
    through analyse.py; this stands in only when analyse.py is not loaded. Getting the SCOPE
    wrong is the bug the contract warns about: the 0.01 culture ceiling is for adherent cells
    and nothing else.
    """
    text = " ".join(str(x).lower() for x in
                    (step.get("reagent"), step.get("note"), step.get("operation")) if x)
    if any(w in text for w in LYSE_WORDS):
        return "prelysis_rinse"
    looks_cultured = any(w in f"{chamber.get('type')} {chamber.get('block')}".lower()
                         for w in ("culture",))
    if looks_cultured or any(w in text for w in _spec.CULTURE_WORDS):
        return "adherent_cells" if looks_cultured else "none"
    if chamber.get("retention") or "capsule" in f"{chamber.get('block')}".lower():
        return "capsules"
    return "none"


def _fallback_findings(chip: dict, protocol: dict, sim: Simulator) -> tuple[list[dict], list[dict]]:
    """The subset of findings this module can see on its own, in the findings.json shape.

    TODO(cfd): delete when assay2chip/cfd/analyse.py lands. It covers shear, port pressure,
    the capsule channel-width rule and graph reachability - NOT traps, residence time,
    bubbles, sedimentation or Re, which need mesh.py, transport.py and particles.py.
    """
    findings: list[dict] = []
    unresolved: list[dict] = [
        {"question": "contracts/materials.json states no bond pressure for a hot-embossed "
                     "0.3 mm bond, so the second leak test in assay2chip/cfd/README.md "
                     "cannot be run; FLAGGED rather than assumed", "assumption": True},
        {"question": "trap, bubble, sediment, exchange and Re findings are not produced "
                     "here: mesh.py, transport.py, particles.py and analyse.py own them"},
    ]
    chip_id = chip.get("id", "")
    max_p = contracts.max_working_pressure_kpa()
    rule_um = contracts.capsule_diameter_um() * contracts.param("capsule_channel.width_factor_low")

    for ch in chip.get("channels", []):
        if ch.get("carries") in ("liquid", "oil", "signals") or ch.get("capsule_passable") is False:
            continue
        width_um = ch["width_mm"] * 1000
        if width_um < rule_um:
            findings.append({
                "id": f"{chip_id}:clog:{ch['id']}", "class": "clog", "severity": "high",
                "chip_id": chip_id, "feature_id": ch["id"], "step": None,
                "metric": "width_um", "value": round(width_um, 3), "limit": round(rule_um, 3),
                "units": "um", "direction": "min",
                "message": f"channel {ch['id']} is {width_um:g} um, under "
                           f"{contracts.param('capsule_channel.width_factor_low'):g} x the "
                           f"{contracts.capsule_diameter_um():g} um capsule",
                "fix_hint": "widen to the particle rule"})

    ports = {p["id"] for p in chip.get("ports", [])}
    for chamber in chip.get("chambers", []):
        if _nearest(chip, chamber["id"], ports) is None:
            findings.append({
                "id": f"{chip_id}:dead_end:{chamber['id']}", "class": "dead_end",
                "severity": "blocker", "chip_id": chip_id, "feature_id": chamber["id"],
                "step": None, "metric": "paths_to_a_port", "value": 0, "limit": 1,
                "units": "paths", "direction": "min",
                "message": f"{chamber['id']} has no path to any port",
                "fix_hint": "add a vent or route it to an outlet"})

    for step in protocol.get("steps", []):
        if not step.get("flow_ul_min"):
            continue
        sol = sim.run(chip, step)
        for chamber in chip.get("chambers", []):
            klass = shear_class_for(chip, chamber, step)
            ceiling = contracts.shear_ceiling_for(klass)
            tau = sol.shear_dyn_cm2.get(chamber["id"])
            if ceiling.ceiling_dyn_cm2 is None or tau is None:
                continue
            if tau > ceiling.ceiling_dyn_cm2:
                findings.append({
                    "id": f"{chip_id}:shear:{chamber['id']}:{step['step']}", "class": "shear",
                    "severity": "blocker", "chip_id": chip_id, "feature_id": chamber["id"],
                    "step": step["step"], "metric": "wall_shear_dyn_cm2",
                    "value": round(tau, 6), "limit": ceiling.ceiling_dyn_cm2,
                    "units": "dyn/cm2", "direction": "max", "shear_class": klass,
                    "message": f"{chamber['id']} sees {tau:.4g} dyn/cm2 on step "
                               f"{step['step']} at {step['flow_ul_min']:g} uL/min, "
                               f"{tau / ceiling.ceiling_dyn_cm2:.3g}x the {klass} ceiling "
                               f"({ceiling.ceiling_dyn_cm2:g})",
                    "fix_hint": "lower the flow to the ceiling at headroom, or widen",
                    "scope": ceiling.scope, "source": ceiling.source,
                    "analyser": "fix.py fallback (assay2chip/cfd/analyse.py not loaded)"})
        for pid in ports:
            p_kpa = sol.pressure_kpa.get(pid)
            if p_kpa is not None and p_kpa > max_p:
                findings.append({
                    "id": f"{chip_id}:leak:{pid}:{step['step']}", "class": "leak",
                    "severity": "blocker", "chip_id": chip_id, "feature_id": pid,
                    "step": step["step"], "metric": "pressure_kpa", "value": round(p_kpa, 6),
                    "limit": round(max_p, 6), "units": "kPa", "direction": "max",
                    "message": f"{pid} reaches {p_kpa:.4g} kPa on step {step['step']}, over "
                               f"escape pressure x margin ({max_p:.4g} kPa)",
                    "fix_hint": "lower the flow or widen the restricting channel",
                    "assumption": contracts.is_assumption("capsule.escape_pressure_kpa"),
                    "analyser": "fix.py fallback (assay2chip/cfd/analyse.py not loaded)"})
    return findings, unresolved


UNRESOLVED_HERE: tuple[dict, ...] = (
    {"question": "particles.py's sediment and clog checks read chip/<id>.json from disk, so "
                 "they are not re-run on a patched copy here; those fixes stay proposals "
                 "until particles.py can take an in-memory chip"},
    {"question": "the `trap` and `exchange` classes rest on transport.py's washout curve, "
                 "which costs ~10 s per step; the no_new_blocker gate therefore re-analyses "
                 "one step for a flow fix and the worst-case step per target for a geometry "
                 "or spec fix, never all 104 of Module 1's steps", "assumption": False},
    {"question": "a spec change (z tolerance, capsule diameter, vertical mounting) cannot be "
                 "verified by re-solving: its before/after is arithmetic on particles.py's "
                 "swept window, and the coupon run that would confirm it is a bench job"},
)
"""Gaps this module knows it has. Written into fixes.json so a reader does not have to
reverse-engineer them from what is missing."""


WORST = "worst"
"""Scope: one step per target at the highest flow the protocol ever sends it.

`transport.mean_age` is an advection-diffusion solve on the whole mesh and costs about
10 s a step, so analysing all 104 of Module 1's flow steps to judge one candidate takes
17 minutes and tells us nothing the worst case did not. Wall shear, pressure and settling
are all monotone in flow, so the highest-flow step per target BOUNDS the rest - which is
the same worst-case construction `scenario.build_worst_cases` builds for the findings
themselves. Every gate verdict records the scope it used.
"""


def _scope_steps(views: list, only_step: Any) -> list:
    """Which step views one analysis covers, for `only_step` = None | WORST | a step key."""
    flowing = [v for v in views if v.has_flow]
    if only_step is None:
        return flowing
    if only_step != WORST:
        return [v for v in flowing if v.index == only_step]
    best: dict[tuple, Any] = {}
    for view in flowing:
        key = (tuple(sorted(view.targets)), tuple(sorted(view.feeds)), view.operation)
        current = best.get(key)
        if current is None or (view.flow_ul_min or 0) > (current.flow_ul_min or 0):
            best[key] = view
    return list(best.values())


def _bundle(chip: dict, protocol: dict, sim: Simulator, only_step: Any = None):
    """analyse.py's Bundle for an IN-MEMORY chip + protocol.

    One StepBundle per step in scope: the solved Field, the Scenario it came from, and
    transport.py's washout/dead-zone output. particles.py is left out on purpose - its
    `analyse_chip(chip_id, protocol_id)` reads `chip/<id>.json` off disk, which is the one
    thing a patched copy must not be confused with; `sediment_after()` re-runs its
    `sediment_check` on this module's own re-solved velocities instead.
    """
    mod = sim._analyse
    mesh = sim.mesh_of(chip)
    steps = []
    for view in _scope_steps(mod.step_views(protocol), only_step):
        raw = dict(view.raw)
        sol = sim.run(chip, raw)
        if sol.field is None:
            continue
        transport = None
        if sim._transport is not None:
            try:
                transport = sim._transport.analyse(mesh, sol.field, step=raw)
            except Exception:
                transport = None
        steps.append(mod.StepBundle(view=view, field=sol.field,
                                    scenario=sim.scenario_for(chip, raw), transport=transport))
    return mod.Bundle(chip=chip, protocol=protocol, mesh=mesh, steps=steps,
                      chip_id=chip.get("id"))


def analyse(chip: dict, protocol: dict, sim: Simulator,
            only_step: Any = None) -> tuple[list[dict], list[dict]]:
    """findings.json for a chip + protocol, through the simulator's cache."""
    return sim.findings(chip, protocol, only_step)


def _analyse_uncached(chip: dict, protocol: dict, sim: Simulator,
                      only_step: Any = None) -> tuple[list[dict], list[dict]]:
    """analyse.py when it is loaded, else this module's stand-in."""
    if sim.backend == "cfd" and sim._analyse is not None:
        try:
            found = sim._analyse.findings_json(
                sim._analyse.analyse_chip(_bundle(chip, protocol, sim, only_step)))
            return list(found), list(UNRESOLVED_HERE)
        except Exception as exc:                      # a sibling mid-build must not break us
            found, gaps = _fallback_findings(chip, protocol, sim)
            return found, gaps + [{"question": f"assay2chip/cfd/analyse.py raised {exc!r}; "
                                               f"used the fallback analyser instead"}]
    return _fallback_findings(chip, protocol, sim)


def group_findings(findings: list[dict], sol_of: Callable[[dict], Solution | None],
                   limit: int | None = None) -> list[dict]:
    """One representative finding per (class, spec feature), worst case first.

    analyse.py reports per mesh element per step - 3614 findings on module6_v1 - but a fix
    is ONE parameter change on ONE spec feature, and the step that puts that feature furthest
    over its limit is the step that sets the parameter. Fixing the worst case fixes the rest,
    so grouping here is not a shortcut; solving 3614 near-duplicates separately would be.
    """
    buckets: dict[tuple, dict] = {}
    for finding in findings:
        sol = sol_of(finding)
        spec = sol.spec_feature(finding.get("feature_id")) if sol else finding.get("feature_id")
        key = (effective_class(finding), spec)
        enriched = dict(finding)
        enriched["spec_feature_id"] = spec
        enriched.setdefault("mesh_feature_id", finding.get("feature_id"))
        best = buckets.get(key)
        if best is None or _over_by(enriched) > _over_by(best):
            enriched["grouped"] = (best or {}).get("grouped", 0) + 1
            buckets[key] = enriched
        else:
            best["grouped"] = best.get("grouped", 1) + 1
    order = {"blocker": 0, "high": 1, "medium": 2, "low": 3}
    out = sorted(buckets.values(),
                 key=lambda f: (order.get(f.get("severity"), 9), -_over_by(f)))
    return out if limit is None else out[:limit]


def _over_by(finding: dict) -> float:
    """How far past its limit the finding is, as a ratio. 0 when it cannot be told.

    A limit of 0 has no ratio, so a non-zero value against it ranks by severity alone and
    gets 1.0 - "over, by an amount this function cannot express" - rather than inf, which
    would sort every settling finding above everything else for a spurious reason.
    """
    value, limit = _scalar(finding.get("value")), _scalar(finding.get("limit"))
    if value is None or limit is None:
        return 0.0
    if limit == 0.0:
        return 1.0 if value != 0.0 else 0.0
    return value / limit if direction(finding) == "max" else limit / max(abs(value), 1e-12)


# --- predicting one candidate -----------------------------------------------------------------------

@dataclass
class Prediction:
    candidate: Candidate
    before: float | None
    after: float | None
    chip: dict
    protocol: dict
    gates: list[Gate]
    error: str | None = None
    knob_value: float | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and all(g.ok for g in self.gates)

    @property
    def blocked_by(self) -> list[str]:
        return [g.name for g in self.gates if not g.ok]

    @property
    def improvement(self) -> float:
        """How far the metric moved toward its limit, as a fraction of `before`."""
        if self.before in (None, 0) or self.after is None:
            return 0.0
        delta = (self.before - self.after) / abs(self.before)
        return delta if self.before > (self.after or 0) else -abs(delta)


def _predict_spec(finding: dict, cand: Candidate, chip: dict, protocol: dict,
                  sol: Solution) -> Prediction:
    """A spec change's before/after, and the gate that keeps it out of the automatic path.

    There is nothing to re-solve: the chip and the protocol are unchanged, and what changes
    is a process window or a reagent. So the before/after is stated on the quantity the
    change actually moves - for the weir, what FRACTION OF THE Z TOLERANCE still retains,
    which is arithmetic on the band particles.py swept. It is a real number with a real
    denominator, and it is labelled as swept rather than solved.

    `measurable` deliberately fails. A fix this module cannot re-run is a fix this module
    must not apply, and saying so is the whole point of having a third kind.
    """
    before = after = None
    band = finding.get("value")
    if isinstance(band, list) and len(band) >= 2 and all(
            isinstance(v, (int, float)) for v in band[:2]):
        lo, hi = float(band[0]), float(band[1])
        z_tol = _scalar(finding.get("z_tolerance_um")) or _z_tolerance_um(sol)
        if z_tol:
            before = min((hi - lo) / (2.0 * z_tol), 1.0)
        if cand.action == "tighten_z_tolerance":
            new_tol = _scalar(cand.params.get("z_tolerance_um"))
            after = min((hi - lo) / (2.0 * new_tol), 1.0) if new_tol else None
        elif cand.action == "larger_capsule":
            # The retention limit is gap_factor_high x diameter, so the whole band scales
            # with the capsule and the window it covers scales with it too.
            need = _scalar(cand.params.get("diameter_um_needed"))
            have = contracts.capsule_diameter_um()
            if need and have and z_tol:
                after = min(((hi - lo) * need / have) / (2.0 * z_tol), 1.0)
    gates = [
        Gate("measurable", False,
             f"a spec change is not a chip or protocol edit, so there is nothing to "
             f"re-solve: it lands in {cand.params.get('where', 'a process document')} and "
             f"its before/after is swept, not solved"),
        Gate("cleared", after is not None and after >= 1.0 - 1e-9,
             f"covers {after:.0%} of the tolerance window (was {before:.0%})"
             if before is not None and after is not None else "not quantified"),
        Gate("fabrication", True, "no geometry changes, so no floor can be broken"),
        Gate("planar", True, "no geometry changes, so no channel can start crossing"),
        Gate("side_effects", True,
             "a spec change has side effects a solver cannot see (yield, cost, a coupon "
             "run), which is why it is for a human"),
        Gate("no_new_blocker", True, "not re-analysed: nothing in the chip or protocol moved"),
    ]
    return Prediction(cand, before, after, chip, protocol, gates)


def _saturated(finding: dict, value: float) -> bool:
    """True when the metric is pinned at the end of its own range and carries no gradient.

    A `fraction` is the case that matters: `settled_fraction` is exactly 1.0 when the whole
    population drops out, and no amount of extra flow changes a 1.0 into a smaller 1.0, so
    the ratio the secant computes is meaningless until the first guess breaks saturation.
    """
    units = str(finding.get("units") or "").lower()
    if "fraction" not in units:
        return False
    return value >= 1.0 - 1e-9 or value <= 1e-12


def _z_tolerance_um(sol: Solution) -> float | None:
    """The weir z tolerance, from particles.py, which parses it out of the contract prose
    rather than retyping the number."""
    mod = _sibling("particles")
    if mod is None:
        return None
    try:
        return float(mod.z_tolerance_um())
    except Exception:
        try:
            return float(mod.capsule_size_for_full_tolerance().get("z_tolerance_um"))
        except Exception:
            return None


def anchor_step(finding: dict, chip: dict, protocol: dict, sim: Simulator) -> Any:
    """Which step to judge this finding on: the one where its feature is actually worst.

    `group_findings` keeps one representative per (class, feature), and analyse.py's own
    deduplication can attach a value seen on one lane to a finding whose `step` drives a
    different lane. On module4_v1 that produced three shear findings all stating
    2.17 dyn/cm2 whose nominated step left the named chamber with no flow at all.

    So: if the finding's own step does not reproduce its number, look for a step that does,
    and take the one where the metric is worst. Monotone in flow, so the worst step is the
    one that sets the fix - the same reasoning as `WORST`. Returns the original key when
    nothing matches, and the `measurable` gate then refuses the fix rather than inventing
    agreement.
    """
    stated = _scalar(finding.get("value"))
    original = finding.get("step")
    if stated in (None, 0.0):
        return original
    step0 = _step(protocol, original)
    if step0 is not None:
        try:
            here = measure(finding, sim.run(chip, step0), chip)
        except Exception:
            here = None
        if here is not None and abs(here - stated) <= BEFORE_AGREEMENT * abs(stated):
            return original
    best_key, best_value = original, None
    for index, step in enumerate(protocol.get("steps", [])):
        if not step.get("flow_ul_min"):
            continue
        try:
            value = measure(finding, sim.run(chip, step), chip)
        except Exception:
            continue
        if value is None:
            continue
        if best_value is None or abs(value) > abs(best_value):
            best_key, best_value = index, value
    if best_value is not None and abs(best_value - stated) <= BEFORE_AGREEMENT * abs(stated):
        return best_key
    return original


def predict(finding: dict, cand: Candidate, chip: dict, protocol: dict,
            sim: Simulator) -> Prediction:
    """Re-mesh, re-solve, re-analyse the modified copy, and run all six gates.

    `cand.params` is only the first guess. Up to MAX_REFINEMENTS secant steps correct the
    knob against the solver, because the scaling law that guessed it ignores the duct
    correction term and the rest of the network.
    """
    step_no = cand.params.get("step", finding.get("step"))
    step0 = _step(protocol, step_no) or _step(protocol, finding.get("step")) or {}
    try:
        base_sol = sim.run(chip, step0)
    except Exception as exc:
        return Prediction(cand, None, None, chip, protocol, [], error=f"baseline solve: {exc}")
    if cand.kind == "spec":
        return _predict_spec(finding, cand, chip, protocol, base_sol)
    before = measure(finding, base_sol, chip)
    if before is None and _scalar(finding.get("value")) is not None:
        before = _scalar(finding["value"])    # the analyser saw it even if we cannot re-read it

    target = target_value(finding)
    knob = cand.params.get(cand.knob) if cand.knob else None
    new_chip, new_proto, after, used = chip, protocol, None, knob
    err: str | None = None

    for attempt in range(MAX_REFINEMENTS + 1):
        try:
            new_chip, new_proto = apply_candidate(chip, protocol, cand, used)
        except FixError as exc:
            err = str(exc)
            break
        step1 = _step(new_proto, step_no) or step0
        try:
            sol = sim.run(new_chip, step1)
        except Exception as exc:
            err = f"re-solve: {exc}"
            break
        after = measure(finding, sol, new_chip)
        if (after is None or target is None or used is None or not cand.exponent
                or attempt == MAX_REFINEMENTS or not after):
            break
        if within_limit(finding, after) and abs(after - target) / max(target, 1e-12) < 0.25:
            break
        if _saturated(finding, after):
            # The metric is pinned at the end of its own range - `settled_fraction` is 1.0
            # when EVERYTHING drops out - so it carries no gradient and the secant has
            # nothing to follow. Refining anyway is how a flow fix once "converged" from
            # 3.92 to 0.00096 uL/min: the ratio said 100x less every time because the
            # measurement could not get worse. Stop and report what the first guess did.
            break
        # Secant on the log-log slope the scaling law claims: metric ~ knob ** exponent.
        factor = (target / after) ** (1.0 / cand.exponent)
        used = used * min(max(factor, 1.0 / SECANT_CLAMP), SECANT_CLAMP)

    if err:
        return Prediction(cand, before, after, new_chip, new_proto, [], error=err, knob_value=used)

    step1 = _step(new_proto, step_no) or step0
    sol = sim.run(new_chip, step1)
    gates = [gate_measurable(before, after, cand, finding),
             gate_cleared(finding, after)]
    if cand.kind == "geometry":
        gates += [gate_fabrication(chip, new_chip), gate_planar(chip, new_chip)]
    gates.append(gate_side_effects(cand, protocol, new_proto, base_sol, sol, new_chip))
    if all(g.ok for g in gates):
        # Scope: a flow change on step N cannot move step M, so comparing step N is the whole
        # comparison. A geometry or spec change can move every step, so that one is judged
        # against the worst-case step for every target, which bounds the rest.
        scope = step_no if cand.kind == "flow" else WORST
        gates.append(gate_no_new_blocker(analyse(chip, protocol, sim, scope)[0],
                                         analyse(new_chip, new_proto, sim, scope)[0], scope))
    else:
        gates.append(Gate("no_new_blocker", True,
                          "not re-analysed: an earlier gate already blocked this candidate"))
    return Prediction(cand, before, after, new_chip, new_proto, gates, knob_value=used)


# --- the bounded loop -------------------------------------------------------------------------------

def _fix_record(finding: dict, pred: Prediction, applied: bool, mode: str,
                verified_after: float | None, assumptions: list[str]) -> dict:
    cand = pred.candidate
    return {
        "finding_id": finding.get("id"),
        "finding_class": finding.get("class"),
        "catalogue_class": effective_class(finding),
        "finding_feature_id": _feature_id(finding),
        "action": cand.action,
        "target": cand.target,
        "params": dict(cand.params) | ({cand.knob: pred.knob_value}
                                       if cand.knob and pred.knob_value is not None else {}),
        "predicted": {"metric": finding.get("metric"), "units": finding.get("units"),
                      "before": pred.before, "after": pred.after,
                      "limit": finding.get("limit"),
                      "target_at_headroom": target_value(finding)},
        "auto_applicable": pred.ok,
        "applied": applied,
        "verified_after": verified_after,
        "kind": cand.kind,
        "rationale": cand.rationale,
        "scaling": cand.scaling,
        "gates": [g.as_json() for g in pred.gates],
        "blocked_by": pred.blocked_by or ([] if pred.error is None else ["applier"]),
        "error": pred.error,
        "rerun_from": cand.stage if cand.kind == "geometry" and mode == "pipeline" else None,
        "needs": NEEDS_SIBLING.get(cand.action),
        "assumptions": assumptions,
        "for_human": not applied,
        "spec_change": ({"where": SPEC_HOME.get(cand.action),
                         "what": cand.params,
                         "who": "fabrication / wet lab, not this pipeline"}
                        if cand.kind == "spec" else None),
    }


def propose(findings: list[dict], chip: dict, protocol: dict, *, mode: str = "standalone",
            sim: Simulator | None = None) -> FixRun:
    """findings -> fixes, applying only what passes all six gates. Bounded and logged.

    Inputs are never mutated: the applied fixes accumulate on COPIES, which is what
    `run.patched_chip` and `run.patched_protocol` hold at the end.
    """
    if mode not in ("standalone", "pipeline"):
        raise ValueError(f"mode must be 'standalone' or 'pipeline', not {mode!r}")
    sim = sim or Simulator()
    run = FixRun(chip_id=chip.get("id", ""), mode=mode, backend=sim.backend,
                 assumptions=process_assumptions())
    cur_chip, cur_proto = copy.deepcopy(chip), copy.deepcopy(protocol)
    grouped = group_findings(findings, lambda f: _safe_sol(sim, chip, protocol, f),
                             limit=MAX_FINDINGS_PER_RUN)
    run.log.append({"iteration": 0, "event": "grouped",
                    "findings_in": len(findings), "groups": len(grouped),
                    "cap": MAX_FINDINGS_PER_RUN,
                    "reason": "one representative per (class, spec feature), worst case "
                              "first: a fix is one parameter change, so the step that puts a "
                              "feature furthest over its limit is the step that sets it"})
    open_findings = {f.get("id"): f for f in grouped}

    for iteration in range(1, MAX_FIX_ITERATIONS + 1):
        if not open_findings:
            run.log.append({"iteration": iteration, "event": "stop",
                            "reason": "no findings left to fix"})
            break
        progressed = False
        for fid, finding in list(open_findings.items()):
            # Anchor the finding to a step that reproduces its own number before building
            # any candidate, so the candidate's flow and the gate's measurement agree.
            anchored = anchor_step(finding, cur_chip, cur_proto, sim)
            if anchored != finding.get("step"):
                run.log.append({"iteration": iteration, "finding_id": fid,
                                "event": "re_anchored",
                                "from_step": finding.get("step"), "to_step": anchored,
                                "reason": "the finding's own step did not reproduce its "
                                          "value; using the step where this feature is "
                                          "worst"})
                finding = dict(finding, step=anchored)
                open_findings[fid] = finding
            cands = candidates_for(finding, cur_chip, cur_proto,
                                   sol=_safe_sol(sim, cur_chip, cur_proto, finding))
            if not cands:
                klass = effective_class(finding)
                run.log.append({
                    "iteration": iteration, "finding_id": fid, "class": klass,
                    "event": "no_candidate",
                    "reason": f"the catalogue offers no action for class {klass!r}: "
                              f"{'reported with a fix_hint only' if klass in ACTIONS else 'unknown class'}"})
                open_findings.pop(fid, None)
                continue
            preds = []
            for cand in cands:
                pred = predict(finding, cand, cur_chip, cur_proto, sim)
                preds.append(pred)
                run.log.append({
                    "iteration": iteration, "finding_id": fid, "action": cand.action,
                    "target": cand.target, "before": pred.before, "after": pred.after,
                    "auto_applicable": pred.ok, "blocked_by": pred.blocked_by,
                    "error": pred.error,
                    "event": "candidate_predicted"})
                if pred.ok:
                    break
            winner = next((p for p in preds if p.ok), None)
            assumptions = _assumptions_for(cur_proto, finding)
            if winner is not None:
                cur_chip, cur_proto = winner.chip, winner.protocol
                step = _step(cur_proto, winner.candidate.params.get("step", finding.get("step")))
                verified = measure(finding, sim.run(cur_chip, step or {}), cur_chip)
                run.fixes.append(_fix_record(finding, winner, True, mode, verified, assumptions))
                run.log.append({"iteration": iteration, "finding_id": fid, "event": "applied",
                                "action": winner.candidate.action,
                                "before": winner.before, "after": verified})
                if winner.candidate.kind == "geometry" and mode == "pipeline":
                    run.rerun_requests.append(_rerun_request(winner, finding, chip))
                open_findings.pop(fid, None)
                progressed = True
            else:
                best = max(preds, key=lambda p: p.improvement, default=None)
                for pred in preds:
                    run.fixes.append(_fix_record(finding, pred, False, mode, None, assumptions))
                    if pred.candidate.kind == "geometry" and mode == "pipeline":
                        run.rerun_requests.append(_rerun_request(pred, finding, chip))
                run.log.append({
                    "iteration": iteration, "finding_id": fid, "event": "proposed_for_human",
                    "candidates": len(preds),
                    "blocked_by": sorted({g for p in preds for g in p.blocked_by}),
                    "best_improvement": round(best.improvement, 4) if best else 0.0,
                    "note": "every candidate is in fixes.json with the gate that stopped it"})
                open_findings.pop(fid, None)
        if not progressed:
            run.log.append({"iteration": iteration, "event": "stop",
                            "reason": "no improvement: nothing passed every gate this "
                                      "iteration, so the loop stops rather than churn"})
            break
        # Anything the applied fixes newly opened gets one more pass, within the bound. The
        # rescan is scoped the same way the no_new_blocker gate is: a geometry fix can move
        # any step, a flow fix only its own.
        applied_now = [f for f in run.fixes if f["applied"]]
        geometry_applied = any(f["kind"] == "geometry" for f in applied_now)
        scopes: list[Any] = [WORST] if geometry_applied else sorted(
            {f["params"].get("step") for f in applied_now
             if f["params"].get("step") is not None}, key=str)
        # Keyed on the FINDING's class and feature, not on the candidate's target. A flow
        # fix names the step's chamber while the finding is about a duct inside it, so
        # keying on the target let the rescan reopen the same finding every iteration.
        handled = {(f["catalogue_class"], f["finding_feature_id"]) for f in run.fixes}
        fresh: list[dict] = []
        for scope in scopes or [None]:
            fresh += analyse(cur_chip, cur_proto, sim, scope)[0]
        reopened = [f for f in group_findings(
            fresh, lambda f: _safe_sol(sim, cur_chip, cur_proto, f),
            limit=MAX_FINDINGS_PER_RUN)
            if (effective_class(f), _feature_id(f)) not in handled]
        open_findings = {f.get("id"): f for f in reopened}
        run.log.append({"iteration": iteration, "event": "iteration_end",
                        "applied_so_far": len(run.applied),
                        "rescan_scope": "worst-case step per target" if geometry_applied
                                        else f"steps {scopes}",
                        "reopened": [f"{f.get('class')}:{_feature_id(f)}"
                                     for f in reopened]})
    else:
        run.log.append({"iteration": MAX_FIX_ITERATIONS, "event": "stop",
                        "reason": f"MAX_FIX_ITERATIONS ({MAX_FIX_ITERATIONS}) reached"})

    run.patched_chip = cur_chip if cur_chip != chip else None
    run.patched_protocol = cur_proto if cur_proto != protocol else None
    run.unresolved = list(UNRESOLVED_HERE)
    run.log.append({"event": "cost", "solves": sim.solves, "analyses": sim.analyses,
                    "backend": sim.backend})
    return run


def _safe_sol(sim: Simulator, chip: dict, protocol: dict, finding: dict) -> Solution | None:
    try:
        return sim.run(chip, _step(protocol, finding.get("step")) or {})
    except Exception:
        return None


def _assumptions_for(protocol: dict, finding: dict) -> list[str]:
    out = [a["name"] for a in process_assumptions()]
    step = _step(protocol, finding.get("step")) or {}
    out += [g["question"] for g in viscosity_for_step(step)[1]]
    if finding.get("assumption"):
        out.append(f"the finding's own limit is a contract assumption "
                   f"({finding.get('metric')})")
    return out


def _rerun_request(pred: Prediction, finding: dict, chip: dict) -> dict:
    """The `{"rerun_from": "s6", "reason": ...}` shape from assay2chip/README.md, carrying
    the parameters S6/S7 need - never a patched repo chip."""
    cand = pred.candidate
    return {
        "rerun_from": cand.stage,
        "reason": f"S10 CFD: {finding.get('class')} on {_feature_id(finding)} "
                  f"({finding.get('message', '')[:120]}); {cand.rationale}",
        "chip_id": chip.get("id"),
        "finding_id": finding.get("id"),
        "action": cand.action,
        "target": cand.target,
        "params": dict(cand.params) | ({cand.knob: pred.knob_value}
                                       if cand.knob and pred.knob_value is not None else {}),
        "predicted": {"metric": finding.get("metric"), "before": pred.before,
                      "after": pred.after, "limit": finding.get("limit")},
        "auto_applicable": pred.ok,
        "blocked_by": pred.blocked_by,
        "max_reruns": MAX_FIX_ITERATIONS,
    }


# --- writing, and the guard that keeps us out of chip/ ------------------------------------------------

PROTECTED = ("chip", "protocols", "cad", "contracts", "runs")


def _assert_not_repo(path: Path) -> Path:
    """Refuse to write anywhere a hand-designed artefact lives.

    assay2chip/cfd/README.md: "Never modify a hand-designed repo chip." This is the
    mechanical version of that sentence - an exception, not a comment.
    """
    resolved = Path(path).resolve()
    for name in PROTECTED:
        guarded = (ROOT / name).resolve()
        if resolved == guarded or guarded in resolved.parents:
            raise PermissionError(
                f"assay2chip/cfd/fix.py refuses to write {resolved}: {name}/ holds "
                f"hand-designed artefacts. A fix is a patched COPY in the work dir, or a "
                f"rerun_from request to S6/S7.")
    return resolved


def _rel(path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def _write_json(path: Path, data: Any) -> Path:
    path = _assert_not_repo(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def write(run: FixRun, work_dir: Path) -> FixRun:
    """fixes.json, plus the patched copies. Never touches chip/ or protocols/."""
    work_dir = Path(work_dir)
    if run.patched_protocol is not None:
        run.artefacts["patched_protocol"] = _rel(_write_json(
            work_dir / "cfd" / "patched" /
            f"{run.patched_protocol.get('id', 'protocol')}.protocol.json",
            run.patched_protocol))
    if run.patched_chip is not None:
        if run.mode == "pipeline":
            run.artefacts["patched_chip"] = ("not written: in pipeline mode a geometry fix is "
                                             "a rerun_from request to S6/S7")
        else:
            run.artefacts["patched_chip"] = _rel(_write_json(
                work_dir / "cfd" / "patched" /
                f"{run.patched_chip.get('id', 'chip')}.patched.json", run.patched_chip))
    if run.rerun_requests:
        run.artefacts["rerun_requests"] = _rel(
            _write_json(work_dir / "cfd" / "rerun_requests.json", run.rerun_requests))
    run.artefacts["fixes"] = _rel(work_dir / "cfd" / "fixes.json")
    # Rewrite fixes.json so it carries the artefact paths it just produced.
    _write_json(work_dir / "cfd" / "fixes.json", run.as_json())
    return run


# --- CLI -----------------------------------------------------------------------------------------------

def _load(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _default_protocol(chip_id: str) -> Path | None:
    p = ROOT / "protocols" / f"{chip_id}.json"
    return p if p.is_file() else None


def summarise(run: FixRun) -> str:
    lines = [f"{run.chip_id}  mode={run.mode}  solver={run.backend}  "
             f"max_iterations={MAX_FIX_ITERATIONS}"]
    for fix in run.fixes:
        pred = fix["predicted"]
        before = f"{pred['before']:.4g}" if isinstance(pred["before"], (int, float)) else "?"
        after = f"{pred['after']:.4g}" if isinstance(pred["after"], (int, float)) else "?"
        mark = "APPLIED " if fix["applied"] else "PROPOSED"
        lines.append(f"  {mark} {fix['finding_class']:9s} {fix['action']:20s} "
                     f"{fix['target']:12s} {pred['metric']}: {before} -> {after} "
                     f"(limit {pred['limit']} {pred['units'] or ''})")
        if not fix["applied"]:
            for gate in fix["gates"]:
                if not gate["ok"]:
                    lines.append(f"           blocked by {gate['gate']}: {gate['detail']}")
            if fix["error"]:
                lines.append(f"           applier: {fix['error']}")
    for entry in run.log:
        if entry.get("event") == "stop":
            lines.append(f"  loop stopped: {entry['reason']}")
    for gap in run.unresolved:
        lines.append(f"  UNRESOLVED {gap['question']}")
    for name, path in run.artefacts.items():
        lines.append(f"  wrote {name}: {path}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--chip", required=True, help="chip spec JSON")
    ap.add_argument("--protocol", help="protocol JSON (default: protocols/<chip_id>.json)")
    ap.add_argument("--findings", help="cfd/findings.json (default: analyse in memory)")
    ap.add_argument("--mode", default="standalone", choices=("standalone", "pipeline"))
    ap.add_argument("--work-dir", help="where patched copies go "
                                       "(default: assay2chip/work/<chip_id>)")
    ap.add_argument("--backend", choices=("cfd", "network"), help="force a solver")
    ap.add_argument("--dry-run", action="store_true", help="print, write nothing")
    args = ap.parse_args(argv)

    chip = _load(args.chip)
    proto_path = Path(args.protocol) if args.protocol else _default_protocol(chip.get("id", ""))
    protocol = _load(proto_path) if proto_path else {"id": chip.get("id"), "steps": []}
    sim = Simulator(args.backend)
    if args.findings:
        payload = _load(args.findings)
        findings = payload.get("findings", payload) if isinstance(payload, dict) else payload
    else:
        findings = analyse(chip, protocol, sim)[0]
    run = propose(findings, chip, protocol, mode=args.mode, sim=sim)
    if not args.dry_run:
        work = Path(args.work_dir) if args.work_dir else ROOT / "assay2chip" / "work" / chip["id"]
        write(run, work)
    print(summarise(run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

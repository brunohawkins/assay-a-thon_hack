"""S10 couple: join the chips end to end and solve the handoff lines.

This is the module that makes S10 a WHOLE-PIPELINE simulation rather than a stack of
per-chip ones. `solve.py` tells you whether one chip flows; this tells you whether chip A
can actually hand its contents to chip B through the tubing, the selector valve and the
two drilled ports that sit between them - and what chip B's back-pressure does to chip A's
weir while it tries.

Reads the chain from `protocols/pipeline_mvp.json` (a `kind: "pipeline"` file) or from a
stage-S4 `partition.json`, then for every handoff computes, with the two numbers the
README's finding table wants:

  pressure      the pressure needed at the upstream outlet to drive the downstream inlet
                flow, against `capsule.escape_pressure_kpa x control.pressure_margin`
  dead volume   pi r^2 L of connector + tubing + valve, against the volume carried
  settling      particle transit time in the line vs Stokes settling across its bore
  continuity    what chip A releases vs what chip B draws (the audit found 77 uL released
                into a stage that draws 9.6 uL)
  particles     the SWOLLEN capsule (capsule.diameter_um x capsule.swell_factor) against
                the channels, filters and ports on BOTH sides
  joint point   chip A + line + chip B composed into ONE resistance network and solved, so
                back-pressure from chip B appears at chip A's weir

Off-chip stages - the bought droplet kit for encapsulation and barcoding, SPRI and QC - are
BOUNDARIES, not holes: the interface is modelled (line, dead volume, what the kit must
accept) and `automated: false` is recorded wherever a human carries the sample.

Writes `cfd/pipeline.field.json` (assay2chip/cfd/README.md).

    python assay2chip/cfd/couple.py --pipeline protocols/pipeline_mvp.json
    python assay2chip/cfd/couple.py --partition assay2chip/work/<id>/partition.json

FLAGGED ASSUMPTIONS. The line's bore and length, the valve's swept volume, the medium's
density and each particle's excess density are CONTRACT keys - `tubing.inner_diameter_mm`,
`tubing.length_mm`, `selector_valve.internal_volume_ul`, `fluid.density_37c_kg_m3`,
`cell|capsule|bead.excess_density_kg_m3` - read through `contracts.param()`. This module
keeps no copy of any of them. They were local here once, and that was the bug: five
modules each carried their own, so this one settled every particle at 50 kg/m3 while the
parts registry used 60 for a cell and solve.py used 993 for the fluid against this one's
1000. The contract still marks them `assumption: true`, so every result that leans on one
says so, in the output's `assumptions` and `unresolved` lists.

DENSITY IS PER PARTICLE KIND, AND THE END OF THE BAND IS A CHOICE. A cell, a capsule and a
magnetic bead are 60, 30 and 700 kg/m3 above medium; one shared figure made them settle
identically. Settling is LINEAR in excess density, so the contract's note asks for the
conservative end for the question: a handoff line LOSING particles takes `max` (the
densest drops out soonest), something RELYING on settling takes `min`. Every settling
result here names the end it used and reports the verdict across the whole band, so a
verdict that flips between min and max is visible as contract uncertainty rather than
presented as a fact about the chip.

Only two numbers are not contract keys: the connector (port bore and lid thickness, read
off the chip spec, so not an assumption at all) and how many valves sit in one line, which
no rig layout states and which stays in `ASSUMPTIONS`.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "operator")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from assay2chip.lib import contracts  # noqa: E402
from assay2chip.lib import io as stage_io  # noqa: E402

import network as opnetwork  # operator/network.py  # noqa: E402

# model.py owns the shapes, mesh.py discretises a chip and solve.py solves one. When all
# three are present the joint network is composed out of two real Meshes and handed to
# solve.py, so chip B's back-pressure lands on the actual weir CELLS rather than on a
# lumped chamber node. When they are not, this falls back to operator/network.py - which
# the README names as the independent implementation S10 must agree with anyway, and which
# tests/test_cfd_couple.py pins the local solver against. Whichever ran is reported in the
# output under `solver`, so no reader has to guess.
try:
    from assay2chip.cfd import model as cfd_model  # type: ignore
except Exception:  # noqa: BLE001
    cfd_model = None
try:
    from assay2chip.cfd import mesh as cfd_mesh  # type: ignore
except Exception:  # noqa: BLE001
    cfd_mesh = None
try:
    from assay2chip.cfd import solve as cfd_solve  # type: ignore
except Exception:  # noqa: BLE001
    cfd_solve = None
try:
    from assay2chip.cfd import particles as cfd_particles  # type: ignore
except Exception:  # noqa: BLE001
    cfd_particles = None
try:
    from assay2chip.cfd import validate as cfd_validate  # type: ignore
except Exception:  # noqa: BLE001
    cfd_validate = None

MESH_AVAILABLE = cfd_model is not None and cfd_mesh is not None and cfd_solve is not None
PARTICLES_AVAILABLE = cfd_particles is not None

UL_MIN_TO_M3_S = 1e-9 / 60.0
M3_S_TO_UL_MIN = 1.0 / UL_MIN_TO_M3_S
DYN_CM2_PER_PA = 10.0
G_M_S2 = 9.80665


class CoupleError(ValueError):
    """The chain cannot be coupled: a stage, port or protocol the handoff names is absent."""


# --- flagged assumptions ------------------------------------------------------


@dataclass(frozen=True)
class Assumption:
    """A number the repo does not state, with where it came from and why it is allowed.

    `flagged` is always True. There is no such thing as an unflagged assumption here: if a
    value has a contract it is read from `assay2chip.lib.contracts` instead and never
    appears in this table.
    """

    key: str
    value: float
    units: str
    why: str
    source: str
    flagged: bool = True

    def as_json(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "units": self.units,
            "why": self.why,
            "source": self.source,
            "flagged": self.flagged,
        }


# The line's dimensions and every density are CONTRACT keys now. They used to live here as
# local assumptions, and that was the bug: five modules each carried their own, so this
# module settled every particle at 50 kg/m3 while the parts registry used 60 for a cell and
# solve.py used 993 for the fluid against this module's 1000 - three answers to "does this
# capsule reach the next chip?". They are read through contracts.param() and nothing below
# keeps a copy. They are still marked `assumption: true` in the contract, so results that
# rest on them still say so.
CONTRACT_KEYS = (
    "tubing.inner_diameter_mm",
    "tubing.length_mm",
    "selector_valve.internal_volume_ul",
    "fluid.density_37c_kg_m3",
)

# One excess density PER PARTICLE KIND, keyed by a handoff's `carries`. A shared particle
# density made a 140 um capsule and a 12 um cell settle as if they were the same thing.
EXCESS_DENSITY_KEY: dict[str, str] = {
    "cells": "cell.excess_density_kg_m3",
    "capsules": "capsule.excess_density_kg_m3",
    "beads": "bead.excess_density_kg_m3",
}

ASSUMPTIONS: dict[str, Assumption] = {
    "valve.count_per_line": Assumption(
        "valve.count_per_line",
        1.0,
        "valves",
        "Every chip in this chain merged ports behind off-chip selector valves, so each "
        "handoff line is switched at least once. One is the floor, not a measurement. The "
        "valve's VOLUME is a contract key (selector_valve.internal_volume_ul); only how "
        "many of them sit in a line is still local, because no rig layout exists.",
        "chip/module3_v1_translated.json description (port merges 13 -> 6, 'isolation "
        "moves to off-chip rotary or selector valves'); chip/module6_v1.json port notes",
    ),
}


def assumption(key: str) -> float:
    """One LOCAL flagged assumption. Anything with a contract key goes through contracts.param."""
    if key in ASSUMPTIONS:
        return ASSUMPTIONS[key].value
    if key in CONTRACT_KEYS or key in EXCESS_DENSITY_KEY.values():
        raise CoupleError(
            f"{key!r} is a CONTRACT key: read it with contracts.param({key!r}), not from "
            f"couple.ASSUMPTIONS. Local copies of contract numbers are what made five "
            f"modules disagree about whether the same capsule settles."
        )
    raise CoupleError(
        f"no local assumption {key!r} in couple.ASSUMPTIONS; it has "
        f"{', '.join(sorted(ASSUMPTIONS))}"
    )


# Settling is LINEAR in excess density, so the band's end is a choice, not a detail, and
# the contract's note says which end answers which question. particles.py encodes exactly
# the same rule as an INTENT, so the two names are mapped here rather than reimplemented:
# a handoff line is particles.py's 'loss' intent, which takes the max end.
LOSS_END = "max"       # a line LOSING particles: the densest one drops out soonest
RETENTION_END = "min"  # a trap RELYING on settling: the lightest one is the hardest to keep
END_TO_INTENT = {"max": "loss", "min": "desired", "value": "parked"}

# A handoff line is a conveying path, so particles.py's `role` for it is 'feed': "its job
# is to DELIVER, so a particle that settles in it is lost before it arrives".
LINE_ROLE = "feed"


def particle_class_for(carries: str):
    """particles.py's ParticleClass for what a line carries, or None.

    Capsules map to `capsule_swollen`, not `capsule`: a capsule in culture is twice the
    size the chip specs quote, and it is the swollen one that has to get down the tubing.
    """
    if not PARTICLES_AVAILABLE:
        return None
    name = {"cells": "cells", "capsules": "capsule_swollen", "beads": "bead"}.get(carries)
    if name is None:
        return None
    return cfd_particles.particle_classes(contracts.viscosity_37c_pa_s().pa_s).get(name)


def excess_density(carries: str, end: str = LOSS_END) -> dict:
    """The excess density for what a line carries, at the conservative end for the question.

    THIS IS NOT A LOCAL TABLE. It reads `cell|capsule|bead.excess_density_kg_m3` through
    `particles.excess_density()`, which reads `contracts.param()` - so this module and the
    particle module cannot disagree about how fast the same capsule falls, which is the
    whole reason the keys moved into the contract. `end` is 'max' for a loss question (a
    handoff line: the densest particle drops out soonest, and if it survives they all do),
    'min' when a result RELIES on settling, 'value' for the nominal. The end used comes
    back with the number so every result can say which it was.
    """
    key = EXCESS_DENSITY_KEY.get(carries)
    if key is None:
        return {
            "key": None,
            "kg_m3": None,
            "end_used": None,
            "band": None,
            "why": f"no contract gives an excess density for carries={carries!r}; "
            f"the kinds with one are {', '.join(sorted(EXCESS_DENSITY_KEY))}",
        }
    if end not in END_TO_INTENT:
        raise CoupleError(
            f"no {end!r} end of a contract band: the three are "
            f"{', '.join(sorted(END_TO_INTENT))}"
        )
    if PARTICLES_AVAILABLE:
        rho = cfd_particles.excess_density(key)
        band = {"min": rho.min_kg_m3, "value": rho.value, "max": rho.max_kg_m3}
        chosen, named_end = rho.for_intent(END_TO_INTENT[end])
        is_assumption = rho.assumption
        via = f"assay2chip/cfd/particles.py excess_density({key!r}).for_intent({END_TO_INTENT[end]!r})"
    else:
        lo, hi = contracts.param_range(key)
        band = {"min": lo, "value": contracts.param(key), "max": hi}
        chosen, named_end = band[end], end
        is_assumption = contracts.is_assumption(key)
        via = f"assay2chip/lib/contracts.py param({key!r}) + param_range (particles.py absent)"
    if chosen is None:
        raise CoupleError(
            f"contracts/parameters.json {key!r} has no {end!r} end: it has "
            f"{', '.join(k for k, v in band.items() if v is not None)}"
        )
    why = {
        "max": "a handoff line LOSES particles to settling, so the conservative end is "
        "max: the densest particle in the band drops out soonest, and if it survives the "
        "line they all do",
        "min": "this result RELIES on settling, so the conservative end is min: the "
        "lightest particle in the band is the hardest to settle out",
        "value": "the contract's typical figure, for comparison only - not a conservative "
        "bound either way",
    }[end]
    return {
        "key": key,
        "kg_m3": float(chosen),
        "end_used": end,
        "named_end": named_end,
        "intent": END_TO_INTENT[end],
        "band": band,
        "why": why,
        "assumption": is_assumption,
        "read_through": via,
    }


def fluid_density_kg_m3() -> float:
    """Medium at 37 C (fluid.density_37c_kg_m3). Within 1% either way, so it never decides
    a verdict on its own - but it is one number for every module now, not five."""
    return float(contracts.param("fluid.density_37c_kg_m3"))


_VALIDATION: dict | None = None


def validation(force: bool = False) -> dict:
    """validate.py's verdict, cached. "A solver nobody has checked is worse than no solver."

    The README makes this gating: nothing here is trusted until the benchmarks pass. The
    joint network below is solved by the same solve.py those benchmarks exercise, so its
    verdict applies to these numbers too and belongs in the file rather than in a separate
    report nobody reads next to it.
    """
    global _VALIDATION
    if cfd_validate is None:
        return {
            "ran": False,
            "trusted": False,
            "why": "assay2chip/cfd/validate.py is not importable, so nothing has checked "
            "the solver these numbers came out of. Treat every one of them as untrusted.",
        }
    if _VALIDATION is None or force:
        report = cfd_validate.run()
        benchmarks = report.get("benchmarks") or []
        passed = [b for b in benchmarks if (b.get("status") or b.get("verdict")) == "pass"]
        _VALIDATION = {
            "ran": True,
            "trusted": bool(report.get("trusted")),
            "status": report.get("status"),
            "benchmarks": len(benchmarks),
            "benchmarks_passed": len(passed),
            "untrusted_reason": report.get("untrusted_reason") or None,
            "solver": report.get("solver"),
            "grid_mm": report.get("grid_mm"),
            "source": "assay2chip/cfd/validate.py run()",
            "why": "the joint chip + line + chip network is solved by the same solve.py "
            "these benchmarks exercise, so their verdict applies to the handoff numbers "
            "in this file",
        }
    return _VALIDATION


def contract_assumptions() -> list[dict]:
    """Every contract key this module leans on that the CONTRACT itself marks an assumption.

    Reported in the same shape as a local one, with `contract: true` and the band, so
    run.py's assumption roll-up gets them and a reader can see that "flagged" now means
    "flagged in one place for everybody" rather than "this module made it up".
    """
    out = []
    for key in list(CONTRACT_KEYS) + sorted(set(EXCESS_DENSITY_KEY.values())):
        try:
            entry = contracts.param_entry(key)
        except contracts.ContractError as exc:
            out.append({"key": key, "value": None, "units": "?", "flagged": True,
                        "contract": True, "source": "contracts/parameters.json",
                        "why": f"MISSING FROM THE CONTRACT: {exc}"})
            continue
        lo, hi = contracts.param_range(key)
        out.append(
            {
                "key": key,
                "value": entry.get("value"),
                "min": lo,
                "max": hi,
                "units": entry.get("units", ""),
                "flagged": bool(entry.get("assumption")),
                "contract": True,
                "source": f"contracts/parameters.json {key}",
                "why": entry.get("note", ""),
            }
        )
    return out


# --- line physics -------------------------------------------------------------


def round_tube_resistance_pa_s_per_m3(length_mm: float, bore_mm: float, mu_pa_s: float) -> float:
    """Poiseuille for a round tube: R = 128 mu L / (pi d^4), in Pa s / m^3.

    The chips are rectangular ducts (solve.py uses the exact rectangular form); the tubing,
    the valve bore and the drilled port hole are round, so they get the round law.
    """
    d = bore_mm / 1000.0
    return 128.0 * mu_pa_s * (length_mm / 1000.0) / (math.pi * d ** 4)


def tube_volume_ul(length_mm: float, bore_mm: float) -> float:
    """pi r^2 L. mm^3 and uL are the same thing, so no conversion is needed."""
    return math.pi * (bore_mm / 2.0) ** 2 * length_mm


def tube_wall_shear_dyn_cm2(flow_ul_min: float, bore_mm: float, mu_pa_s: float) -> float:
    """Round tube: tau = 4 mu Q / (pi r^3), x10 for dyn/cm2."""
    q = abs(flow_ul_min) * UL_MIN_TO_M3_S
    r = bore_mm / 2.0 / 1000.0
    return 4.0 * mu_pa_s * q / (math.pi * r ** 3) * DYN_CM2_PER_PA


def tube_reynolds(flow_ul_min: float, bore_mm: float, mu_pa_s: float) -> float:
    """Re = rho u d / mu, with the contract's fluid density. Above 1 the README says report
    it as outside the model rather than trusting the number."""
    d = bore_mm / 1000.0
    area = math.pi * (d / 2.0) ** 2
    u = abs(flow_ul_min) * UL_MIN_TO_M3_S / area
    return fluid_density_kg_m3() * u * d / mu_pa_s


def stokes_settling_mm_s(diameter_um: float, mu_pa_s: float, excess_density_kg_m3: float) -> float:
    """Stokes terminal velocity, mm/s: v = d_rho g d^2 / (18 mu).

    Delegates to `particles.stokes_settling_m_s` when that module is importable, so the
    two cannot drift apart; the local line is the identical formula and exists only so
    couple.py still runs if particles.py does not import.

    `excess_density_kg_m3` is PER PARTICLE KIND and comes from the contract - a cell, a
    capsule and a magnetic bead are 60, 30 and 700 kg/m3 above medium and do not settle
    alike. Settling is linear in it, so the caller must pick the end of the band that is
    conservative for its question (see `excess_density`) and say which it used.
    """
    d = diameter_um / 1e6
    if PARTICLES_AVAILABLE:
        return cfd_particles.stokes_settling_m_s(excess_density_kg_m3, d, mu_pa_s) * 1000.0
    return excess_density_kg_m3 * G_M_S2 * d * d / (18.0 * mu_pa_s) * 1000.0


@dataclass(frozen=True)
class Segment:
    """One piece of a handoff line, treated as a round tube in series with the others."""

    id: str
    kind: str  # connector | tubing | valve
    bore_mm: float
    length_mm: float
    volume_ul: float
    resistance_pa_s_per_m3: float
    source: str
    flagged: bool

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "bore_mm": round(self.bore_mm, 4),
            "length_mm": round(self.length_mm, 4),
            "dead_volume_ul": round(self.volume_ul, 4),
            "resistance_pa_s_per_m3": self.resistance_pa_s_per_m3,
            "resistance_kpa_per_ul_min": self.resistance_pa_s_per_m3 * UL_MIN_TO_M3_S / 1000.0,
            "source": self.source,
            "flagged": self.flagged,
        }


@dataclass(frozen=True)
class Line:
    """The whole off-chip path between two chips: port hole, tubing, valve, port hole."""

    id: str
    segments: tuple[Segment, ...]
    mu_pa_s: float
    mu_key: str

    @property
    def resistance_pa_s_per_m3(self) -> float:
        return sum(s.resistance_pa_s_per_m3 for s in self.segments)

    @property
    def dead_volume_ul(self) -> float:
        return sum(s.volume_ul for s in self.segments)

    @property
    def narrowest_bore_mm(self) -> float:
        return min(s.bore_mm for s in self.segments)

    @property
    def length_mm(self) -> float:
        return sum(s.length_mm for s in self.segments)

    def pressure_drop_kpa(self, flow_ul_min: float) -> float:
        return self.resistance_pa_s_per_m3 * flow_ul_min * UL_MIN_TO_M3_S / 1000.0

    def as_json(self) -> dict:
        return {
            "id": self.id,
            "segments": [s.as_json() for s in self.segments],
            "length_mm": round(self.length_mm, 4),
            "narrowest_bore_mm": round(self.narrowest_bore_mm, 4),
            "dead_volume_ul": round(self.dead_volume_ul, 4),
            "resistance_pa_s_per_m3": self.resistance_pa_s_per_m3,
            "resistance_kpa_per_ul_min": self.resistance_pa_s_per_m3 * UL_MIN_TO_M3_S / 1000.0,
            "viscosity_pa_s": self.mu_pa_s,
            "viscosity_key": self.mu_key,
            "all_dimensions_assumed": all(s.flagged for s in self.segments if s.kind != "connector"),
        }


def build_line(
    line_id: str,
    mu: contracts.Viscosity,
    from_connector: Segment | None,
    to_connector: Segment | None,
    valves: int = 1,
) -> Line:
    """Connector + assumed tubing + assumed valve + connector, in series.

    The valve's hydraulic length is derived from its assumed swept volume and the assumed
    bore (L = V / pi r^2), so the valve adds no assumption beyond the two already flagged.
    """
    bore = float(contracts.param("tubing.inner_diameter_mm"))
    length = float(contracts.param("tubing.length_mm"))
    segments: list[Segment] = []
    if from_connector is not None:
        segments.append(from_connector)
    segments.append(
        Segment(
            f"{line_id}/tubing",
            "tubing",
            bore,
            length,
            tube_volume_ul(length, bore),
            round_tube_resistance_pa_s_per_m3(length, bore, mu.pa_s),
            "contracts/parameters.json tubing.inner_diameter_mm + tubing.length_mm "
            "(both assumption: true)",
            contracts.is_assumption("tubing.inner_diameter_mm"),
        )
    )
    v_ul = float(contracts.param("selector_valve.internal_volume_ul"))
    valve_len = v_ul / (math.pi * (bore / 2.0) ** 2)  # mm, derived from the swept volume
    for i in range(max(int(valves), 0)):
        segments.append(
            Segment(
                f"{line_id}/valve{i + 1}",
                "valve",
                bore,
                valve_len,
                v_ul,
                round_tube_resistance_pa_s_per_m3(valve_len, bore, mu.pa_s),
                "contracts/parameters.json selector_valve.internal_volume_ul "
                "(assumption: true); hydraulic length derived from it and the bore",
                contracts.is_assumption("selector_valve.internal_volume_ul"),
            )
        )
    if to_connector is not None:
        segments.append(to_connector)
    return Line(line_id, tuple(segments), mu.pa_s, mu.key)


def connector_segment(
    seg_id: str, chip: dict | None, port_id: str | None, mu: contracts.Viscosity
) -> Segment | None:
    """The drilled hole through the lid, as a round tube.

    Bore: the port's own `width_um` when the chip states one, else the contract floor
    `fabrication_floors.min_port_mm` - never a literal. Length: the thickness of the chip's
    lid layer, which is what the hole is drilled through. Both come off the chip spec, so
    this is the ONE part of the line that is not an assumption.
    """
    if chip is None or not port_id:
        return None
    port = next((p for p in chip.get("ports", []) if p.get("id") == port_id), None)
    if port is None:
        return None
    bore_mm = float(port["width_um"]) / 1000.0 if port.get("width_um") else contracts.floor("min_port_mm")
    bore_source = (
        f"chip spec port '{port_id}'.width_um"
        if port.get("width_um")
        else "contracts/blocks.json fabrication_floors.min_port_mm"
    )
    lid = next((l for l in chip.get("layers", []) if l.get("id") == "lid"), None)
    if lid is None:
        lid = (chip.get("layers") or [{}])[-1]
    length_mm = float(lid.get("thickness_mm") or 0.0)
    flagged = not bool(lid.get("thickness_mm"))
    return Segment(
        seg_id,
        "connector",
        bore_mm,
        length_mm,
        tube_volume_ul(length_mm, bore_mm),
        round_tube_resistance_pa_s_per_m3(length_mm, bore_mm, mu.pa_s) if length_mm else 0.0,
        f"{bore_source}; length = chip spec layer '{lid.get('id', '?')}'.thickness_mm",
        flagged,
    )


# --- nodal solver -------------------------------------------------------------


def solve_nodal(
    nodes: Sequence[str],
    edges: Sequence[dict],
    vented: Iterable[str],
    feeds: dict[str, float],
) -> tuple[dict[str, float], dict[str, float]]:
    """A p = b for nodal pressures, then per-edge flow. The README's network form.

    `edges` are dicts with `id`, `from`, `to` and `R` in Pa s / m^3. `vented` nodes are
    held at 0 Pa; `feeds` inject uL/min. Returns (pressure in Pa, flow in uL/min signed
    from -> to). A component with a feed and no vent has no steady solution and raises,
    the same way operator/network.py does; a component with neither is left at 0.
    """
    import numpy as np

    idx = {n: i for i, n in enumerate(nodes)}
    parent = list(range(len(nodes)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for e in edges:
        parent[find(idx[e["from"]])] = find(idx[e["to"]])
    comps: dict[int, list[str]] = {}
    for n in nodes:
        comps.setdefault(find(idx[n]), []).append(n)

    fixed = {n: 0.0 for n in vented}
    for members in comps.values():
        has_vent = any(n in fixed for n in members)
        fed = [n for n in members if feeds.get(n, 0.0)]
        if not has_vent:
            if fed:
                raise CoupleError(
                    f"{', '.join(fed)} fed but nothing downstream is vented: the liquid has "
                    f"nowhere to go"
                )
            for n in members:
                fixed[n] = 0.0

    unknown = [n for n in nodes if n not in fixed]
    u = {n: i for i, n in enumerate(unknown)}
    m = len(unknown)
    A = np.zeros((m, m))
    b = np.zeros(m)
    for n in unknown:
        b[u[n]] += feeds.get(n, 0.0) * UL_MIN_TO_M3_S
    for e in edges:
        g = 1.0 / e["R"]
        for x, y in ((e["from"], e["to"]), (e["to"], e["from"])):
            if x in u:
                A[u[x]][u[x]] += g
                if y in u:
                    A[u[x]][u[y]] -= g
                else:
                    b[u[x]] += g * fixed[y]
    pressure = dict(fixed)
    if m:
        for n, val in zip(unknown, np.linalg.solve(A, b)):
            pressure[n] = float(val)
    flows = {}
    for e in edges:
        q = (pressure[e["from"]] - pressure[e["to"]]) / e["R"] * M3_S_TO_UL_MIN
        flows[e["id"]] = 0.0 if abs(q) < 1e-12 else q
    return pressure, flows


def chip_edges(chip: dict, mu_pa_s: float, prefix: str) -> tuple[list[str], list[dict]]:
    """One chip's resistance network, ids prefixed so two chips can share a system.

    Built with operator/network.py, which already cuts channel centrelines from the spec's
    path (or the trimmed straight line) and adds the slab across each chamber it enters.
    When cfd/solve.py lands this should use the Mesh it produces instead; the composition
    below does not care which produced the edges.
    """
    net = opnetwork.Network(chip, mu=mu_pa_s)
    nodes = [prefix + n for n in net.nodes]
    edges = [
        {"id": prefix + e["id"], "from": prefix + e["from"], "to": prefix + e["to"], "R": e["R"]}
        for e in net.edges
    ]
    return nodes, edges


def protocol_port_states(step: dict, ports: Iterable[str]) -> dict[str, str]:
    """feed / vent / cap per port, from a protocol step's `ports` block.

    Steps that predate the `ports` block (module1_v1 has none) give nothing, and the caller
    records that rather than guessing which ports were open.
    """
    block = step.get("ports") or {}
    out: dict[str, str] = {}
    for kind in ("feed", "vent", "cap"):
        for pid in block.get(kind) or []:
            out[pid] = kind
    for pid in ports:
        out.setdefault(pid, "cap")
    return out


# --- the chain ----------------------------------------------------------------


@dataclass(frozen=True)
class Stage:
    """One stage of the chain: a chip, or an off-chip boundary with no chip at all."""

    key: str
    name: str
    status: str
    chip_path: str | None
    protocol_path: str | None
    off_chip_reason: str | None = None
    kind: str = "module"  # module | roadmap

    @property
    def on_chip(self) -> bool:
        return self.chip_path is not None

    @property
    def pass_through(self) -> bool:
        """A dropped placeholder MODULE: no chip, no protocol, nothing happens to the sample.

        The sample crosses it unchanged, so the chip behind it is the one that really drives
        the next transfer. A `roadmap` stage is the opposite - SPRI and QC are real bench
        work that changes the volume - so it is never treated as pass-through.
        """
        return self.kind == "module" and self.chip_path is None and self.protocol_path is None


@dataclass(frozen=True)
class Handoff:
    """One join, as the chain states it. Numbers are computed later, never read from prose."""

    id: str
    carries: str
    from_stage: str
    from_ports: tuple[str, ...]
    to_stage: str
    to_ports: tuple[str, ...]
    after_step: int | None
    connector: dict
    note: str
    stated_volume_ul: float | None = None
    stated_particle_um: float | None = None
    stated_automated: bool | None = None
    stated_how: str | None = None
    stated_dead_volume_ul: float | None = None


@dataclass
class Chain:
    """A pipeline or partition file, read into stages and handoffs."""

    id: str
    source: str
    kind: str
    stages: dict[str, Stage]
    handoffs: list[Handoff]
    unresolved: list[dict] = field(default_factory=list)


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _tbc(port: Any) -> tuple[str, ...]:
    """Port ids a handoff names, dropping the chain's TBC placeholder."""
    if isinstance(port, list):
        return tuple(p for p in port if isinstance(p, str) and p and p != "TBC")
    if isinstance(port, str) and port and port != "TBC":
        return (port,)
    return ()


def read_pipeline(path: Path | str) -> Chain:
    """A `kind: "pipeline"` file, e.g. protocols/pipeline_mvp.json."""
    path = Path(path)
    doc = stage_io.load_json(path)
    stages: dict[str, Stage] = {}
    unresolved: list[dict] = []
    for mod in doc.get("modules", []):
        key = mod["module"]
        chip = _str_or_none(mod.get("chip"))
        proto = _str_or_none(mod.get("protocol"))
        reason = None
        if chip is None:
            reason = (
                f"{mod.get('status', 'unknown')}: {mod.get('status_note') or mod.get('note') or ''}"
            ).strip()
            unresolved.append(
                {
                    "stage": key,
                    "question": f"stage '{key}' has no chip, so it is a boundary, not a modelled "
                    f"chip: {reason}",
                }
            )
        stages[key] = Stage(key, mod.get("name", key), mod.get("status", "unknown"), chip, proto, reason)
    for road in doc.get("roadmap", []):
        stages[road["id"]] = Stage(
            road["id"],
            road.get("name", road["id"]),
            "off_chip",
            None,
            None,
            road.get("note") or "off-chip roadmap stage, no protocol",
            kind="roadmap",
        )
    handoffs = []
    for h in doc.get("handoffs", []):
        src, dst = h.get("from", {}), h.get("to", {})
        from_key = src.get("module") or src.get("roadmap")
        to_key = dst.get("module") or dst.get("roadmap")
        if from_key is None or to_key is None:
            raise CoupleError(f"handoff {h.get('id')!r} names neither a module nor a roadmap stage")
        for key in (from_key, to_key):
            stages.setdefault(key, Stage(key, key, "off_chip", None, None, "named only by a handoff"))
        handoffs.append(
            Handoff(
                id=h["id"],
                carries=h.get("carries", "unknown"),
                from_stage=from_key,
                from_ports=_tbc(src.get("port") or src.get("ports")),
                to_stage=to_key,
                to_ports=_tbc(dst.get("port") or dst.get("ports")),
                after_step=h.get("after_step"),
                connector=h.get("connector") or {},
                note=" ".join(
                    x for x in (h.get("note"), src.get("note"), dst.get("note")) if x
                ),
            )
        )
    return Chain(doc.get("id", path.stem), path.as_posix(), "pipeline", stages, handoffs, unresolved)


def read_partition(path: Path | str) -> Chain:
    """A stage-S4 partition.json (contracts/schemas/partition.schema.json)."""
    path = Path(path)
    doc = stage_io.load_json(path)
    stage_io.check("partition", doc, path)
    stages: dict[str, Stage] = {}
    unresolved = [dict(u) for u in doc.get("unresolved", [])]
    for chip in doc.get("chips", []):
        cid = chip["chip_id"]
        chip_file = ROOT / "chip" / f"{cid}.json"
        proto_file = ROOT / "protocols" / f"{cid}.json"
        if not chip_file.exists():
            unresolved.append(
                {
                    "chip_id": cid,
                    "question": f"chip/{cid}.json does not exist yet, so this stage is modelled "
                    f"as a boundary (S6/S7 have not built it)",
                }
            )
        stages[cid] = Stage(
            cid,
            cid,
            chip.get("note", "partition chip"),
            chip_file.relative_to(ROOT).as_posix() if chip_file.exists() else None,
            proto_file.relative_to(ROOT).as_posix() if proto_file.exists() else None,
            None if chip_file.exists() else "chip not built yet",
        )
    handoffs = []
    for i, h in enumerate(doc.get("handoffs", []), start=1):
        for key in (h["from"], h["to"]):
            stages.setdefault(key, Stage(key, key, "off_chip", None, None, "named only by a handoff"))
        handoffs.append(
            Handoff(
                id=f"{h['from']}_to_{h['to']}",
                carries=h.get("carries", "unknown"),
                from_stage=h["from"],
                from_ports=_tbc(h.get("from_port")),
                to_stage=h["to"],
                to_ports=_tbc(h.get("to_port")),
                after_step=None,
                connector={},
                note=" ".join(x for x in (h.get("how"), h.get("note")) if x),
                stated_volume_ul=h.get("volume_ul"),
                stated_particle_um=h.get("particle_um"),
                stated_automated=h.get("automated"),
                stated_how=h.get("how"),
                stated_dead_volume_ul=h.get("dead_volume_ul"),
            )
        )
    return Chain(doc.get("id", path.stem), path.as_posix(), "partition", stages, handoffs, unresolved)


def read_chain(path: Path | str) -> Chain:
    """Read either shape; the `kind` field decides."""
    path = Path(path)
    doc = stage_io.load_json(path)
    if doc.get("kind") == "pipeline" or "modules" in doc:
        return read_pipeline(path)
    return read_partition(path)


# --- automation ---------------------------------------------------------------

# A handoff is automated only if a machine can do it unattended. The partition schema is
# blunt about this: "never set it true to make the number look better". When the chain does
# not state it, these phrases in the handoff's own note decide, and the phrase that decided
# is reported, so a reader can check the call instead of trusting it.
MANUAL_PHRASES = (
    "at the bench",
    "by hand",
    "bench step",
    "manual",
    "pipette",
    "in a tube",
    "into tubes",
    "one spin",
    "centrifuge",
    "off-chip",
    "off chip",
)


def automation_for(handoff: Handoff, from_stage: Stage, to_stage: Stage) -> tuple[bool, str]:
    """(automated, why). Every `false` is a human in a pipeline whose goal is none."""
    if handoff.stated_automated is not None:
        return bool(handoff.stated_automated), f"stated in the chain file ({handoff.stated_how or 'how not given'})"
    text = handoff.note.lower()
    hit = next((p for p in MANUAL_PHRASES if p in text), None)
    if hit:
        return False, f"the chain's own note says {hit!r}, which is a human carrying the sample"
    if not from_stage.on_chip or not to_stage.on_chip:
        missing = from_stage.key if not from_stage.on_chip else to_stage.key
        return False, f"stage '{missing}' has no chip, so the sample is handled off-chip by a human"
    if not handoff.from_ports or not handoff.to_ports:
        side = "upstream" if not handoff.from_ports else "downstream"
        return False, f"the {side} port is TBC, so no machine route exists to check"
    return True, "tubing and a selector valve between two named ports on two built chips"


# --- particles ----------------------------------------------------------------


def particle_for(handoff: Handoff, chips: dict[str, dict]) -> dict:
    """The diameter the line and both chips actually have to pass.

    Capsules use the SWOLLEN diameter - `capsule.diameter_um x capsule.swell_factor` from
    the contract - because a capsule in culture is twice the size the chip specs quote and
    sizing on the unswollen number is how the audit's clog findings got missed. Cells have
    no diameter in any contract, so the bound used is `cell_filter.gap_um`: after the
    strainer the chain documents, nothing larger than the gap is in the line. That is a
    bound, not a measurement, and it is flagged as one.
    """
    if handoff.stated_particle_um:
        return {
            "diameter_um": float(handoff.stated_particle_um),
            "source": "stated in the chain file (partition handoff particle_um)",
            "swollen": True,
            "flagged": False,
        }
    if handoff.carries == "capsules":
        return {
            "diameter_um": contracts.swollen_capsule_diameter_um(),
            "unswollen_um": contracts.capsule_diameter_um(),
            "swell_factor": contracts.capsule_swell_factor(),
            "source": "contracts/parameters.json capsule.diameter_um x capsule.swell_factor",
            "swollen": True,
            "flagged": contracts.is_assumption("capsule.swell_factor"),
        }
    if handoff.carries == "cells":
        gap = contracts.param("cell_filter.gap_um")
        return {
            "diameter_um": float(gap),
            "source": "contracts/parameters.json cell_filter.gap_um, used as an UPPER BOUND: "
            "after the strainer the chain documents, nothing larger is in the line",
            "swollen": False,
            "flagged": True,
        }
    return {
        "diameter_um": None,
        "source": f"no contract gives a particle size for carries={handoff.carries!r}",
        "swollen": False,
        "flagged": True,
    }


def port_constrictions(chip: dict | None, port_id: str | None, carries: str) -> list[dict]:
    """Every opening the sample must pass on a chip, walking out from one port.

    The port hole, each channel on it, and the filter or retention gap of each chamber
    those channels reach. A `filter.gap_um` is a clog risk; a `retention.opening_um` is a
    weir that is MEANT to be smaller than the particle, so it is reported as retention and
    never as a clog.
    """
    if chip is None or not port_id:
        return []
    out: list[dict] = []
    port = next((p for p in chip.get("ports", []) if p.get("id") == port_id), None)
    if port is not None:
        bore = float(port["width_um"]) / 1000.0 if port.get("width_um") else contracts.floor("min_port_mm")
        out.append({"feature_id": port_id, "kind": "port", "opening_mm": bore, "by_design": False})
    chambers = {c["id"]: c for c in chip.get("chambers", [])}
    reached: list[str] = []
    for ch in chip.get("channels", []):
        if port_id not in (ch.get("from"), ch.get("to")):
            continue
        if carries == "capsules" and ch.get("capsule_passable") is False:
            continue
        out.append(
            {
                "feature_id": ch["id"],
                "kind": "channel",
                "opening_mm": min(float(ch["width_mm"]), float(ch["depth_mm"])),
                "width_mm": float(ch["width_mm"]),
                "depth_mm": float(ch["depth_mm"]),
                "by_design": False,
            }
        )
        other = ch["to"] if ch.get("from") == port_id else ch.get("from")
        if other in chambers:
            reached.append(other)
    for cid in reached:
        c = chambers[cid]
        if c.get("filter", {}).get("gap_um"):
            out.append(
                {
                    "feature_id": cid,
                    "kind": "filter",
                    "opening_mm": float(c["filter"]["gap_um"]) / 1000.0,
                    "by_design": False,
                    "purpose": c["filter"].get("purpose"),
                }
            )
        if c.get("retention", {}).get("opening_um"):
            out.append(
                {
                    "feature_id": cid,
                    "kind": "retention",
                    "opening_mm": float(c["retention"]["opening_um"]) / 1000.0,
                    "by_design": True,
                    "purpose": "retains the bed; meant to be smaller than the particle",
                }
            )
    return out


# --- volumes ------------------------------------------------------------------


def _chamber_volume(chip: dict | None, target: str | None) -> float | None:
    if chip is None or not target:
        return None
    c = next((c for c in chip.get("chambers", []) if c.get("id") == target), None)
    return float(c["volume_ul"]) if c and c.get("volume_ul") else None


# Operations that put the sample INTO the line. An `exchange` or a `mix` that happens to
# vent the transfer port is pushing waste through it, not handing the sample over, so it is
# not a release and must never be read as the volume carried.
RELEASE_OPS = ("release", "collect", "pool")


def _step_volume_ul(step: dict, chip: dict | None) -> dict | None:
    """One step's volume, from `volume_ul x washes` or - FLAGGED - from its note."""
    washes = max(int(step.get("washes") or 1), 1)
    if step.get("volume_ul"):
        return {
            "volume_ul": float(step["volume_ul"]) * washes,
            "source": f"step {step['step']} volume_ul x washes ({step['volume_ul']} x {washes})",
            "flagged": False,
        }
    import re

    m = re.search(r"(\d+)\s+chamber volumes?", step.get("note") or "")
    chamber = _chamber_volume(chip, step.get("target"))
    if m and chamber:
        return {
            "volume_ul": int(m.group(1)) * chamber,
            "source": f"step {step['step']} has volume_ul: null; its NOTE says '{m.group(0)}' "
            f"and {step.get('target')} holds {chamber} uL",
            "flagged": True,
        }
    return None


def released_volume_ul(
    protocol: dict | None,
    step_no: int | None,
    chip: dict | None,
    from_ports: Sequence[str] = (),
) -> dict:
    """What the upstream chip actually puts into the line.

    First choice is the step the chain names in `after_step`. When that step states no
    volume - or the chain names no step at all, which several MVP handoffs do not - the
    fallback is the last step that VENTS this handoff's outlet with a release-class
    operation. That is how module 3's release is found: `volume_ul` is null and the only
    place the volume exists is the step note, "2 chamber volumes of buffer carry the bed
    out", against a 38.443 uL trap. A number parsed out of prose is FLAGGED and recorded in
    `unresolved`: it is not the same as a structured field.
    """
    if protocol is None:
        return {"volume_ul": None, "source": "the upstream stage has no protocol", "flagged": True}
    steps = protocol.get("steps", [])
    tried: list[str] = []
    if step_no is not None:
        step = next((s for s in steps if s.get("step") == step_no), None)
        if step is None:
            tried.append(f"step {step_no} is not in the upstream protocol")
        else:
            got = _step_volume_ul(step, chip)
            if got:
                return {**got, "source": f"protocol {got['source']} (the chain's after_step)",
                        "operation": step.get("operation")}
            tried.append(f"the chain's after_step {step_no} ('{step.get('operation')}') states no volume")
    if from_ports:
        releases = [
            s
            for s in steps
            if set((s.get("ports") or {}).get("vent") or []) & set(from_ports)
            and s.get("operation") in RELEASE_OPS
        ]
        for step in reversed(releases):
            got = _step_volume_ul(step, chip)
            if got:
                return {
                    **got,
                    "source": f"protocol {got['source']} - the last release-class step venting "
                    f"{', '.join(from_ports)}",
                    "flagged": True,
                    "operation": step.get("operation"),
                }
        tried.append(
            f"no release-class step venting {', '.join(from_ports)} states a volume"
            if releases
            else f"no release-class step vents {', '.join(from_ports)}"
        )
    return {
        "volume_ul": None,
        "source": "; ".join(tried) or "the chain names no step and no outlet port",
        "flagged": True,
    }


def drawn_volume_ul(protocol: dict | None, to_ports: Sequence[str]) -> dict:
    """What the downstream chip draws through the inlet(s) this handoff feeds.

    The first step feeding one of those ports names the reagent; every step feeding one of
    them with that same reagent is part of the same draw. For module 4 that is 16 pulses of
    0.6 uL through four inlets behind one rotary valve, which is the 9.6 uL the audit found.
    """
    if protocol is None or not to_ports:
        return {"volume_ul": None, "source": "no downstream protocol or no named inlet", "flagged": True}
    hits = [
        s
        for s in protocol.get("steps", [])
        if set((s.get("ports") or {}).get("feed") or []) & set(to_ports) and s.get("reagent")
    ]
    if not hits:
        return {
            "volume_ul": None,
            "source": f"no step in the downstream protocol feeds {', '.join(to_ports)} with a reagent",
            "flagged": True,
        }
    reagent = hits[0]["reagent"]
    same = [s for s in hits if s.get("reagent") == reagent and s.get("volume_ul")]
    if not same:
        return {
            "volume_ul": None,
            "reagent": reagent,
            "source": f"the steps feeding {', '.join(to_ports)} with '{reagent}' state no volume_ul",
            "flagged": True,
        }
    per_step = [float(s["volume_ul"]) * max(int(s.get("washes") or 1), 1) for s in same]
    total = sum(per_step)
    return {
        "volume_ul": total,
        "per_transfer_ul": max(per_step),
        "reagent": reagent,
        "steps": [s["step"] for s in same],
        "source": f"{len(same)} downstream step(s) feeding {', '.join(to_ports)} with "
        f"'{reagent}', volume_ul x washes summed",
        "flagged": False,
    }


def inlet_flow_ul_min(
    protocol: dict | None, to_ports: Sequence[str], chip: dict | None = None
) -> dict:
    """The flow the downstream inlet is driven at, from its own protocol step.

    One contract fallback: an encapsulator's cell inlet. `spc_demo_v1`'s encapsulate step
    states no flow because the three streams are set by ratio, and the contract states the
    core-stream rate outright, so it is read rather than left blank. It is still flagged -
    the contract's rate is for the published kit, not necessarily for this chip.
    """
    if protocol is not None and to_ports:
        for s in protocol.get("steps", []):
            if set((s.get("ports") or {}).get("feed") or []) & set(to_ports) and s.get("flow_ul_min"):
                return {
                    "flow_ul_min": float(s["flow_ul_min"]),
                    "step": s["step"],
                    "source": f"downstream protocol step {s['step']} flow_ul_min",
                }
    if chip is not None and chip.get("encapsulation") and to_ports:
        roles = {
            p["id"]: p.get("role") for p in chip.get("ports", []) if p.get("id") in set(to_ports)
        }
        if "cells" in roles.values():
            return {
                "flow_ul_min": float(contracts.param("encapsulation.cell_flow_ul_h")) / 60.0,
                "source": "FLAGGED: the downstream protocol states no flow for this inlet, so "
                "contracts/parameters.json encapsulation.cell_flow_ul_h / 60 is used - the "
                "chip declares an `encapsulation` block and this port's role is 'cells'",
                "flagged": True,
            }
    if protocol is None or not to_ports:
        return {"flow_ul_min": None, "source": "no downstream protocol or no named inlet"}
    return {"flow_ul_min": None, "source": f"no step feeding {', '.join(to_ports)} states flow_ul_min"}


def outlet_flow_ul_min(protocol: dict | None, step_no: int | None) -> dict:
    """The flow the upstream chip pushes the sample out at."""
    if protocol is None or step_no is None:
        return {"flow_ul_min": None, "source": "no upstream protocol or no after_step"}
    step = next((s for s in protocol.get("steps", []) if s.get("step") == step_no), None)
    if step and step.get("flow_ul_min"):
        return {
            "flow_ul_min": float(step["flow_ul_min"]),
            "step": step_no,
            "source": f"upstream protocol step {step_no} flow_ul_min",
        }
    return {"flow_ul_min": None, "source": f"upstream step {step_no} states no flow_ul_min"}


# --- the joint operating point ------------------------------------------------


def _port_chamber(chip: dict, port_id: str) -> str | None:
    for ch in chip.get("channels", []):
        if ch.get("from") == port_id:
            return ch.get("to")
        if ch.get("to") == port_id:
            return ch.get("from")
    return None


def infer_drive(chip: dict, from_port: str) -> dict:
    """What pushes the sample out, when no protocol step says.

    S4 writes partition.json long before S8 writes a protocol, so a handoff often has no
    step to read port states from. The inlet that reaches the SAME chamber as the transfer
    outlet is the one that can push the chamber's contents through it - module 3's
    `backflush_in` and `reagent_in` both reach `main_trap`, which `to_split_tree` drains.
    The choice is reported so a reader can override it, and it is never used when a
    protocol step DOES state `ports.feed`: an empty feed list is a statement (step 4 of
    spc_demo_v1 collects into a tube), not a gap.
    """
    chamber = _port_chamber(chip, from_port)
    inlets = [p["id"] for p in chip.get("ports", []) if p.get("type") == "inlet"]
    same = [p for p in inlets if _port_chamber(chip, p) == chamber]
    vents = [
        p["id"]
        for p in chip.get("ports", [])
        if p.get("type") == "outlet" and p["id"] != from_port
    ]
    if not same:
        return {"feed": None, "why": f"no inlet on this chip reaches {chamber or from_port}"}
    return {
        "feed": same[0],
        "vents": vents,
        "chamber": chamber,
        "why": f"INFERRED, no protocol step: '{same[0]}' is the inlet that reaches "
        f"'{chamber}', the chamber '{from_port}' drains, so it is what can push the "
        f"contents out. Other candidates: {', '.join(same[1:]) or 'none'}.",
    }


def inlet_resistance(chip: dict, to_port: str, vent: str, mu_pa_s: float) -> dict:
    """Effective resistance from a chip's inlet to its vent, by solving the chip alone.

    1 uL/min in at the inlet, vent at 0 Pa: the inlet pressure IS the resistance. This is
    what the upstream outlet has to push against on top of the line.
    """
    nodes, edges = chip_edges(chip, mu_pa_s, "")
    pressure, _ = solve_nodal(nodes, edges, [vent], {to_port: 1.0})
    return {
        "resistance_pa_s_per_m3": pressure[to_port] / UL_MIN_TO_M3_S,
        "resistance_kpa_per_ul_min": pressure[to_port] / 1000.0,
        "vent": vent,
    }


def line_edge_g_geom_m3(line: Line) -> float:
    """The line's viscosity-free conductance, in m^3, which is model.Edge's contract.

    g_geom = conductance x mu = mu / R. For the whole line in series that is simply
    mu / sum(R), and because every segment's R is linear in mu the result has no mu in it.
    """
    return line.mu_pa_s / line.resistance_pa_s_per_m3


_MESH_CACHE: dict[tuple[str, float], Any] = {}


def chip_mesh(chip: dict, mu_pa_s: float):
    """mesh.py's Mesh for one chip, cached.

    Meshing a 30 x 20 mm chip at the mesher's grid is tens of thousands of cells, and a
    pipeline run composes the same chip into several handoffs, so this is cached per
    (chip id, viscosity). The Mesh is only ever read - `_prefixed_mesh` copies before
    renaming - so sharing it is safe.
    """
    key = (str(chip.get("id") or ""), float(mu_pa_s))
    if not key[0]:
        return cfd_mesh.build_mesh(chip, mu_pa_s=mu_pa_s)
    if key not in _MESH_CACHE:
        _MESH_CACHE[key] = cfd_mesh.build_mesh(chip, mu_pa_s=mu_pa_s)
    return _MESH_CACHE[key]


def _prefixed_mesh(mesh, prefix: str):
    """A copy of a Mesh with every node, edge and port id prefixed, so two can be merged."""
    from dataclasses import replace

    nodes = [replace(n, id=prefix + n.id) for n in mesh.nodes]
    edges = [replace(e, id=prefix + e.id, a=prefix + e.a, b=prefix + e.b) for e in mesh.edges]
    ports = {prefix + pid: prefix + nid for pid, nid in mesh.ports.items()}
    return cfd_model.Mesh(nodes=nodes, edges=edges, ports=ports, meta=dict(mesh.meta))


def compose_meshes(mesh_a, mesh_b, from_port: str, to_port: str, line: Line):
    """Two chip meshes joined by one line edge: the pipeline network, as a single Mesh.

    The line is a model.Edge with its `g_geom_m3` set from Poiseuille rather than from the
    rectangular-duct law, because it is a round tube. Its width and depth are set so that
    `width x depth` equals the true circular cross-section, which is what solve.py uses for
    velocity and the Reynolds number; the resistance itself comes from `g_geom_m3` and is
    exact. Returns (mesh, line_edge_id).
    """
    a, b = _prefixed_mesh(mesh_a, "A/"), _prefixed_mesh(mesh_b, "B/")
    side_mm = line.narrowest_bore_mm * math.sqrt(math.pi / 4.0)  # equal-area square
    link = cfd_model.Edge(
        id=f"line/{from_port}_to_{to_port}",
        a=a.ports[f"A/{from_port}"],
        b=b.ports[f"B/{to_port}"],
        kind="duct",
        g_geom_m3=line_edge_g_geom_m3(line),
        conductance_m3_per_pa_s=1.0 / line.resistance_pa_s_per_m3,
        length_mm=line.length_mm,
        width_mm=side_mm,
        depth_mm=side_mm,
        feature_id=line.id,
        note="off-chip handoff line: connector + tubing + valve, Poiseuille for a round "
        "tube. Width and depth are an equal-area square so velocity and Re are right; the "
        "resistance is g_geom_m3 and is exact.",
    )
    meta = {
        "chip_id": f"{mesh_a.chip_id}+{mesh_b.chip_id}",
        "mu_pa_s": line.mu_pa_s,
        "composed_by": "assay2chip/cfd/couple.py",
        "chips": [mesh_a.chip_id, mesh_b.chip_id],
        "line": line.as_json(),
    }
    mesh = cfd_model.Mesh(
        nodes=a.nodes + b.nodes,
        edges=a.edges + b.edges + [link],
        ports={**a.ports, **b.ports},
        meta=meta,
    )
    return mesh, link.id


def _scenario_for(mesh, states_a: dict[str, str], states_b: dict[str, str],
                  from_port: str, to_port: str, drive_ul_min: float, mu_pa_s: float):
    """Both chips' protocol port states as one Scenario over the composed mesh.

    The transfer ports are deliberately left out: they are joined by the line edge now, so
    neither is a boundary any more. Everything else keeps what its own step said.
    """
    PortState = cfd_model.PortState
    ports: dict[str, Any] = {}
    for prefix, states, skip in (("A/", states_a, from_port), ("B/", states_b, to_port)):
        for pid, kind in states.items():
            if pid == skip:
                continue
            key = prefix + pid
            if key not in mesh.ports:
                continue
            if kind == "feed":
                ports[key] = PortState.flow(drive_ul_min)
            elif kind == "vent":
                ports[key] = PortState.vent()
            else:
                ports[key] = PortState("closed")
    return cfd_model.Scenario(
        step=f"handoff/{from_port}->{to_port}",
        ports=ports,
        fluid="handoff transfer",
        temp_c=None,
        mu_pa_s=mu_pa_s,
        chip_id=mesh.chip_id,
        flow_ul_min=drive_ul_min,
        note="composed by assay2chip/cfd/couple.py: chip A + line + chip B",
    ).for_mesh(mesh)


def joint_operating_point_mesh(
    chip_a: dict,
    chip_b: dict,
    from_port: str,
    to_port: str,
    line: Line,
    mu_pa_s: float,
    drive_ul_min: float,
    a_states: dict[str, str],
    b_states: dict[str, str],
) -> dict:
    """The joint operating point through mesh.py + solve.py: the preferred path.

    Both chips are meshed properly - chambers discretised across their width, weirs as
    slit edges - joined by the line and solved as one system. That matters for the weir:
    on a lumped model the trap is one node, so "the pressure at the weir" is an average,
    while on the mesh it is the pressure on the cells actually sitting against the crest.
    """
    # The transfer has to be driven from UPSTREAM. Chip B's own pumps may well be running
    # (module 2's oil and shell streams are, during a core-inlet transfer) and they belong
    # in the network - but they are not what pushes the sample down the line, and solving a
    # network driven only from the receiving side reports a line flow of zero as if that
    # were a result.
    if not [p for p, k in a_states.items() if k == "feed"]:
        raise CoupleError(
            f"nothing drives {from_port}'s chip: its transfer step names no feed port, so "
            f"there is no upstream pump to push the sample through the line"
        )
    mesh_a = chip_mesh(chip_a, mu_pa_s)
    mesh_b = chip_mesh(chip_b, mu_pa_s)
    for mesh, port in ((mesh_a, from_port), (mesh_b, to_port)):
        if port not in mesh.ports:
            raise CoupleError(
                f"{mesh.chip_id} has no meshed port {port!r}; it has "
                f"{', '.join(sorted(mesh.ports))}"
            )
    joint_mesh, link_id = compose_meshes(mesh_a, mesh_b, from_port, to_port, line)
    scenario = _scenario_for(joint_mesh, a_states, b_states, from_port, to_port, drive_ul_min, mu_pa_s)
    if not scenario.driven():
        raise CoupleError("no port in the composed scenario is driven")
    if not scenario.fixed():
        raise CoupleError("no port in the composed scenario is open, so there is no outlet")
    field = cfd_solve.solve_step(joint_mesh, scenario)

    # Chip A alone, same drive, with the transfer port vented instead of piped: the
    # difference between the two weir pressures IS chip B's back-pressure.
    alone_ports = {
        pid: cfd_model.PortState.flow(drive_ul_min)
        if kind == "feed"
        else cfd_model.PortState.vent()
        if kind == "vent"
        else cfd_model.PortState("closed")
        for pid, kind in a_states.items()
        if pid in mesh_a.ports
    }
    alone_ports[from_port] = cfd_model.PortState.vent()
    alone = cfd_solve.solve_step(
        mesh_a,
        cfd_model.Scenario(
            step=f"{from_port}/alone", ports=alone_ports, mu_pa_s=mu_pa_s, chip_id=mesh_a.chip_id
        ).for_mesh(mesh_a),
    )

    weir_feature = _port_chamber(chip_a, from_port)
    chamber = next((c for c in chip_a.get("chambers", []) if c.get("id") == weir_feature), {}) or {}
    cells = [n.id for n in mesh_a.nodes_of(weir_feature or "")] if weir_feature else []
    p_alone = max((alone.p_pa[c] for c in cells if c in alone.p_pa), default=None)
    p_joint = max((field.p_pa.get(f"A/{c}") for c in cells if f"A/{c}" in field.p_pa), default=None)
    q_line = field.q_ul_min(link_id)
    limit_kpa = contracts.max_working_pressure_kpa()
    over_re = [e for e in field.over_re(1.0)]
    return {
        "solved": True,
        "solver": "assay2chip/cfd/mesh.py + solve.py, composed here into one Mesh",
        "mesh": {
            "nodes": len(joint_mesh.nodes),
            "edges": len(joint_mesh.edges),
            "weir_cells_on_chip_a": len(cells),
            "chips": joint_mesh.meta["chips"],
        },
        "drive_ul_min": drive_ul_min,
        "link_edge": link_id,
        "upstream_outlet": from_port,
        "downstream_inlet": to_port,
        "weir": {
            "feature_id": weir_feature,
            "is_retention": bool(chamber.get("retention")),
            "retention_opening_um": (chamber.get("retention") or {}).get("opening_um"),
            "p_alone_kpa": None if p_alone is None else p_alone / 1000.0,
            "p_joint_kpa": None if p_joint is None else p_joint / 1000.0,
            "back_pressure_from_chip_b_kpa": None
            if p_joint is None or p_alone is None
            else (p_joint - p_alone) / 1000.0,
            "limit_kpa": limit_kpa,
            "limit_source": "contracts/parameters.json capsule.escape_pressure_kpa x "
            "control.pressure_margin",
            "over_limit": None if p_joint is None else p_joint / 1000.0 > limit_kpa,
            "measured_on": "the chamber's own mesh cells, worst case",
        },
        "p_upstream_outlet_kpa": field.p_pa[joint_mesh.ports[f"A/{from_port}"]] / 1000.0,
        "p_downstream_inlet_kpa": field.p_pa[joint_mesh.ports[f"B/{to_port}"]] / 1000.0,
        "q_through_line_ul_min": q_line,
        "line_dp_kpa": line.pressure_drop_kpa(abs(q_line)),
        "re_in_line": field.re.get(link_id),
        "unmodelled_edges_re_over_1": over_re[:10] or None,
        "residual": field.residual.to_json(),
    }


def joint_operating_point(
    chip_a: dict,
    chip_b: dict,
    from_port: str,
    to_port: str,
    line: Line,
    mu_pa_s: float,
    drive_ul_min: float,
    a_states: dict[str, str],
    b_states: dict[str, str],
) -> dict:
    """Fallback joint operating point, on lumped-chamber edges from operator/network.py.

    This is the point of the module. Solved alone with its transfer port vented, chip A's
    weir sits at one pressure; with chip B's inlet path and the line hung off that port it
    sits at another, and the difference is the back-pressure chip B imposes on the weir.
    The weir is what the capsules are held on, so that difference is the number that
    decides whether the bed survives the transfer.
    """
    a_feeds = {f"A/{p}": drive_ul_min for p, k in a_states.items() if k == "feed"}
    if not a_feeds:
        return {
            "solved": False,
            "why": "the upstream transfer step names no feed port, so nothing is driving "
            "chip A: this is not a coupled flow, it is a collection into a tube. "
            "`ports.feed: []` is a statement (spc_demo_v1 step 4 really does collect off "
            "chip), so it is taken at its word; a step with no `ports` block at all gets a "
            "drive inferred instead (see infer_drive). Name the feed port in the protocol "
            "step to get a joint operating point.",
        }
    # The transfer port is no longer a free outlet: it is the line. Everything else keeps
    # the state its own protocol step gave it.
    a_vents = [f"A/{p}" for p, k in a_states.items() if k == "vent" and p != from_port]
    b_vents = [f"B/{p}" for p, k in b_states.items() if k == "vent" and p != to_port]
    if not b_vents:
        return {
            "solved": False,
            "why": f"the downstream step feeding {to_port} vents nothing, so chip B has no "
            f"outlet and the joint network has no steady solution",
        }

    a_nodes, a_edges = chip_edges(chip_a, mu_pa_s, "A/")
    b_nodes, b_edges = chip_edges(chip_b, mu_pa_s, "B/")
    link = {
        "id": f"line/{from_port}_to_{to_port}",
        "from": f"A/{from_port}",
        "to": f"B/{to_port}",
        "R": line.resistance_pa_s_per_m3,
    }
    nodes = a_nodes + b_nodes
    edges = a_edges + b_edges + [link]

    weir_a = _port_chamber(chip_a, from_port)
    joint_p, joint_q = solve_nodal(nodes, edges, a_vents + b_vents, a_feeds)
    alone_p, alone_q = solve_nodal(a_nodes, a_edges, a_vents + [f"A/{from_port}"], a_feeds)

    delivered = joint_q[link["id"]]
    limit_kpa = contracts.max_working_pressure_kpa()
    weir_joint = joint_p.get(f"A/{weir_a}") if weir_a else None
    weir_alone = alone_p.get(f"A/{weir_a}") if weir_a else None
    chamber = next((c for c in chip_a.get("chambers", []) if c.get("id") == weir_a), {}) or {}
    return {
        "solved": True,
        "solver": "operator/network.py edges, coupled and solved here (lumped chambers)",
        "drive_ul_min": drive_ul_min,
        "link_edge": link["id"],
        "upstream_outlet": from_port,
        "downstream_inlet": to_port,
        "weir": {
            "feature_id": weir_a,
            "is_retention": bool(chamber.get("retention")),
            "retention_opening_um": (chamber.get("retention") or {}).get("opening_um"),
            "p_alone_kpa": None if weir_alone is None else weir_alone / 1000.0,
            "p_joint_kpa": None if weir_joint is None else weir_joint / 1000.0,
            "back_pressure_from_chip_b_kpa": None
            if weir_joint is None or weir_alone is None
            else (weir_joint - weir_alone) / 1000.0,
            "limit_kpa": limit_kpa,
            "limit_source": "contracts/parameters.json capsule.escape_pressure_kpa x "
            "control.pressure_margin",
            "over_limit": None if weir_joint is None else weir_joint / 1000.0 > limit_kpa,
        },
        "p_upstream_outlet_kpa": joint_p[f"A/{from_port}"] / 1000.0,
        "p_downstream_inlet_kpa": joint_p[f"B/{to_port}"] / 1000.0,
        "q_through_line_ul_min": delivered,
        "line_dp_kpa": line.pressure_drop_kpa(abs(delivered)),
        "vents": {"chip_a": a_vents, "chip_b": b_vents},
    }


# --- per-handoff analysis -----------------------------------------------------

SEVERITY_ORDER = {"blocker": 3, "high": 2, "medium": 1, "low": 0, "none": -1}


def _worst(*severities: str) -> str:
    return max(severities, key=lambda s: SEVERITY_ORDER.get(s, -1)) if severities else "none"


def severity_score(parts: dict[str, str]) -> int:
    """Rank handoffs that share a top severity. Weighted so TWO blockers beat one.

    Without this, every handoff with a blocker ties and `worst_handoff` is whichever came
    first in the file, which is not a finding.
    """
    return sum(4 ** SEVERITY_ORDER.get(v, 0) for v in parts.values())


def analyse_handoff(
    handoff: Handoff,
    chain: Chain,
    chips: dict[str, dict],
    protocols: dict[str, dict],
    mu: contracts.Viscosity,
    upstream_fallback: Handoff | None = None,
) -> dict:
    """Every number for one handoff, with the limit each is checked against."""
    from_stage = chain.stages[handoff.from_stage]
    to_stage = chain.stages[handoff.to_stage]
    chip_a = chips.get(handoff.from_stage)
    chip_b = chips.get(handoff.to_stage)
    proto_a = protocols.get(handoff.from_stage)
    proto_b = protocols.get(handoff.to_stage)
    boundary = not from_stage.on_chip or not to_stage.on_chip
    unresolved: list[dict] = []
    notes: list[str] = []

    automated, automated_why = automation_for(handoff, from_stage, to_stage)

    # --- do the ports this handoff names actually exist? -----------------
    # A TBC port on a chip that HAS been built is the hardest kind of hole: module 4's
    # in_s1-in_s4 are inlets and p_vent cannot pass a capsule, so the sublibrary has no way
    # out and no amount of tubing fixes it. A TBC port on a placeholder is expected.
    ports_check: dict[str, Any] = {"sides": {}}
    pr_sev = "low"
    for side, stage, chip, named in (
        ("from", from_stage, chip_a, handoff.from_ports),
        ("to", to_stage, chip_b, handoff.to_ports),
    ):
        have = {p["id"] for p in (chip or {}).get("ports", [])}
        if not named:
            verdict = "tbc_on_built_chip" if chip is not None else "tbc_on_boundary"
            pr_sev = _worst(pr_sev, "blocker" if chip is not None else "high")
        elif chip is None:
            verdict = "named_but_no_chip_to_check"
            pr_sev = _worst(pr_sev, "high")
        elif set(named) - have:
            verdict = f"named ports not on the chip: {', '.join(sorted(set(named) - have))}"
            pr_sev = "blocker"
        else:
            verdict = "ok"
        ports_check["sides"][side] = {
            "stage": stage.key,
            "named": list(named),
            "on_chip_ports": sorted(have) or None,
            "verdict": verdict,
        }
        if verdict != "ok":
            unresolved.append(
                {
                    "handoff": handoff.id,
                    "field": f"{side}_port",
                    "question": f"{stage.key}: {verdict}"
                    + (
                        " - the chip is built, so this is a missing port, not a missing chip"
                        if chip is not None and not named
                        else ""
                    ),
                }
            )
    ports_check["severity"] = pr_sev

    line = build_line(
        handoff.id,
        mu,
        connector_segment(f"{handoff.id}/port_out", chip_a, handoff.from_ports[0] if handoff.from_ports else None, mu),
        connector_segment(f"{handoff.id}/port_in", chip_b, handoff.to_ports[0] if handoff.to_ports else None, mu),
        valves=int(assumption("valve.count_per_line")),
    )

    # --- flows -----------------------------------------------------------
    down = inlet_flow_ul_min(proto_b, handoff.to_ports, chip_b)
    up = outlet_flow_ul_min(proto_a, handoff.after_step)
    drive = down["flow_ul_min"] or up["flow_ul_min"]
    drive_source = (
        down["source"] if down["flow_ul_min"] else up["source"] if up["flow_ul_min"] else None
    )
    if drive is None:
        unresolved.append(
            {
                "handoff": handoff.id,
                "field": "flow_ul_min",
                "question": "neither side states a flow for this transfer, so no pressure, "
                "transit time or Reynolds number can be computed",
            }
        )

    # --- pressure to drive the downstream inlet --------------------------
    limit_kpa = contracts.max_working_pressure_kpa()
    pressure: dict[str, Any] = {
        "downstream_flow_ul_min": down["flow_ul_min"],
        "downstream_flow_source": down["source"],
        "limit_kpa": limit_kpa,
        "limit_source": "contracts/parameters.json capsule.escape_pressure_kpa x "
        "control.pressure_margin",
        "bond_limit_kpa": None,
        "bond_limit_source": "FLAGGED UNKNOWN: contracts/materials.json states no bond or "
        "burst pressure for a hot-embossed 0.3 mm bond, so the README's second leak test "
        "cannot be evaluated",
    }
    q = down["flow_ul_min"]
    if q:
        line_dp = line.pressure_drop_kpa(q)
        chip_b_dp = None
        if chip_b is not None and handoff.to_ports:
            vent = next(
                (
                    p
                    for p, k in protocol_port_states(
                        next(
                            (
                                s
                                for s in (proto_b or {}).get("steps", [])
                                if set((s.get("ports") or {}).get("feed") or []) & set(handoff.to_ports)
                            ),
                            {},
                        ),
                        [p["id"] for p in chip_b.get("ports", [])],
                    ).items()
                    if k == "vent"
                ),
                None,
            )
            if vent:
                try:
                    r = inlet_resistance(chip_b, handoff.to_ports[0], vent, mu.pa_s)
                    chip_b_dp = r["resistance_kpa_per_ul_min"] * q
                    pressure["downstream_chip_resistance_kpa_per_ul_min"] = r["resistance_kpa_per_ul_min"]
                    pressure["downstream_vent"] = vent
                except (CoupleError, KeyError, ValueError) as exc:  # noqa: PERF203
                    notes.append(f"chip B inlet resistance not solved: {exc}")
        total = line_dp + (chip_b_dp or 0.0)
        pressure.update(
            {
                "line_dp_kpa": line_dp,
                "downstream_chip_dp_kpa": chip_b_dp,
                "p_upstream_outlet_needed_kpa": total,
                "fraction_of_limit": total / limit_kpa if limit_kpa else None,
                "over_limit": total > limit_kpa,
                "line_share_of_total": line_dp / total if total else None,
                "wall_shear_in_line_dyn_cm2": tube_wall_shear_dyn_cm2(q, line.narrowest_bore_mm, mu.pa_s),
                "reynolds_in_line": tube_reynolds(q, line.narrowest_bore_mm, mu.pa_s),
            }
        )
        pressure["unmodelled_re_above_1"] = pressure["reynolds_in_line"] > 1.0
    p_sev = "high" if pressure.get("over_limit") else "low" if q else "medium"

    # --- dead volume vs volume carried -----------------------------------
    released = released_volume_ul(proto_a, handoff.after_step, chip_a, handoff.from_ports)
    if released["volume_ul"] is None and from_stage.pass_through and upstream_fallback is not None:
        back_stage = chain.stages[upstream_fallback.from_stage]
        released = released_volume_ul(
            protocols.get(upstream_fallback.from_stage),
            upstream_fallback.after_step,
            chips.get(upstream_fallback.from_stage),
            upstream_fallback.from_ports,
        )
        if released["volume_ul"] is not None:
            released["source"] = (
                f"{back_stage.key}: {released['source']} - the nearest upstream stage with a "
                f"protocol; the stages between it and here are placeholders that carry the "
                f"volume through unchanged"
            )
            released["flagged"] = True
    if released["flagged"]:
        unresolved.append(
            {
                "handoff": handoff.id,
                "field": "released_volume_ul",
                "question": f"the volume leaving upstream is not a structured field: {released['source']}",
            }
        )
    drawn = drawn_volume_ul(proto_b, handoff.to_ports)
    # What the line's dead volume is measured against: the volume released if we know it,
    # otherwise the volume the downstream chip draws, which is the next best statement of
    # how much sample is in play.
    carried = handoff.stated_volume_ul or released["volume_ul"] or drawn["volume_ul"]
    carried_source = (
        "stated in the chain file"
        if handoff.stated_volume_ul
        else released["source"]
        if released["volume_ul"]
        else f"the volume released upstream is unknown ({released['source']}), so this is "
        f"what the DOWNSTREAM chip draws: {drawn['source']}"
        if drawn["volume_ul"]
        else released["source"]
    )
    dead = line.dead_volume_ul
    dead_volume: dict[str, Any] = {
        "dead_volume_ul": dead,
        "by_segment": {s.id: round(s.volume_ul, 4) for s in line.segments},
        "volume_carried_ul": carried,
        "volume_carried_source": carried_source,
    }
    if drawn.get("per_transfer_ul"):
        dead_volume["volume_per_transfer_ul"] = drawn["per_transfer_ul"]
        dead_volume["line_volumes_per_transfer"] = dead / drawn["per_transfer_ul"]
        dead_volume["transfers"] = len(drawn.get("steps") or [])
    if carried:
        frac = dead / carried
        dead_volume.update(
            {
                "fraction_lost_in_line": frac,
                "dilution_if_line_primed": (carried + dead) / carried,
                "note": "two readings of the same number: 'fraction_lost' is the sample left "
                "behind when the flow stops, 'dilution' is what happens instead if the line "
                "was primed with buffer that joins the plug. The line is primed once and "
                "reused, so a repeated transfer pays the dilution on the first pass only.",
            }
        )
        # The per-transfer figure is the sharper one when a handoff is repeated: module 4
        # sends one sublibrary per pass, so each pass has to clear the whole line again.
        per = dead_volume.get("line_volumes_per_transfer") or frac
        d_sev = "blocker" if per >= 1.0 else "high" if per > 0.5 else "medium" if per > 0.1 else "low"
        if per >= 1.0:
            one = dead_volume.get("volume_per_transfer_ul") or carried
            dead_volume["message"] = (
                f"the line holds {dead:.4g} uL, {per:.3g}x the {one:.4g} uL of a single "
                f"transfer: on one pass nothing arrives"
            )
    else:
        d_sev = "medium"

    # --- transit vs settling ---------------------------------------------
    # A handoff line is a LOSS question - does the particle reach the next chip? - so the
    # conservative end of the excess-density band is max. The whole band is reported so the
    # verdict's robustness is visible: a verdict that flips between min and max is a
    # statement about the contract's uncertainty, not about the chip.
    particle = particle_for(handoff, chips)
    rho = excess_density(handoff.carries, LOSS_END)
    settling: dict[str, Any] = {
        "particle_um": particle["diameter_um"],
        "particle_source": particle["source"],
        "viscosity_pa_s": mu.pa_s,
        "viscosity_key": mu.key,
        "fluid_density_kg_m3": fluid_density_kg_m3(),
        "fluid_density_key": "fluid.density_37c_kg_m3",
        "excess_density_key": rho["key"],
        "excess_density_kg_m3": rho["kg_m3"],
        "excess_density_end_used": rho["end_used"],
        "excess_density_band": rho["band"],
        "excess_density_why_this_end": rho["why"],
        "excess_density_is_contract_assumption": rho.get("assumption"),
        "bore_mm": line.narrowest_bore_mm,
    }

    def _at(d_rho: float | None) -> dict | None:
        """Settling against transit at one excess density."""
        if not (particle["diameter_um"] and d_rho):
            return None
        v = stokes_settling_mm_s(particle["diameter_um"], mu.pa_s, d_rho)
        t_settle = line.narrowest_bore_mm / v if v else math.inf
        out = {
            "excess_density_kg_m3": d_rho,
            "settling_velocity_mm_s": v,
            "time_to_settle_across_bore_s": t_settle,
        }
        if q:
            t_transit = line.dead_volume_ul / q * 60.0
            out.update(
                {
                    "transit_time_in_line_s": t_transit,
                    "transit_over_settle": t_transit / t_settle if t_settle else None,
                    "settles_before_it_arrives": t_transit > t_settle,
                }
            )
        return out

    # The verdict itself comes from particles.py, given the line as a feed-role feature:
    # same code, same contract bands, same intent rule as the on-chip channels, so a
    # disagreement between this module and that one is impossible rather than unlikely.
    pclass = particle_class_for(handoff.carries)
    if pclass is not None and q:
        t_transit = line.dead_volume_ul / q * 60.0
        area_mm2 = math.pi * (line.narrowest_bore_mm / 2.0) ** 2
        u_mm_s = (q / 60.0) / area_mm2 if area_mm2 else 0.0
        feature = {
            "id": line.id,
            "type": "line",
            "block": "straight_channel",
            "carries": handoff.carries,
            "note": "off-chip handoff line: connector + tubing + selector valve",
        }
        verdict = cfd_particles.sediment_check(
            feature,
            element_id=f"{line.id}/tubing",
            element="edge",
            particle=pclass,
            depth_mm=line.narrowest_bore_mm,
            transit_time_s=t_transit,
            u_mm_s=u_mm_s,
            mu_pa_s=mu.pa_s,
            role=LINE_ROLE,
        )
        settling["particles_module_verdict"] = verdict.as_json()
        settling["particles_module"] = (
            "assay2chip/cfd/particles.py sediment_check, role='feed' - the same check it "
            "runs on Module 4's on-chip feed channels, so the two agree by construction"
        )
        settling["mean_velocity_in_line_mm_s"] = u_mm_s
        settling["diameter_used_um"] = verdict.diameter_um
        settling["diameter_end_used"] = verdict.diameter_end
        settling["diameter_band_um"] = [pclass.d_min_um, pclass.d_max_um]

    s_sev = "medium"
    if particle["diameter_um"] and rho["kg_m3"]:
        chosen = _at(rho["kg_m3"])
        settling.update({k: v for k, v in chosen.items() if k != "excess_density_kg_m3"})
        band = {end: _at(val) for end, val in (rho["band"] or {}).items()}
        settling["across_the_band"] = band
        verdicts = {
            end: r["settles_before_it_arrives"]
            for end, r in band.items()
            if r and "settles_before_it_arrives" in r
        }
        settling["verdict_across_the_band"] = verdicts or None
        settling["verdict_holds_across_the_whole_band"] = (
            len(set(verdicts.values())) == 1 if verdicts else None
        )
        if q:
            # particles.py's severity when it ran (it grades on the settled FRACTION, not
            # just a yes/no), else the local yes/no.
            pv = settling.get("particles_module_verdict") or {}
            s_sev = pv.get("severity") or (
                "high" if chosen["settles_before_it_arrives"] else "low"
            )
        else:
            settling["note"] = "no flow stated, so there is no transit time to compare it with"
    elif not rho["kg_m3"]:
        settling["note"] = rho["why"]
        unresolved.append(
            {"handoff": handoff.id, "field": "excess_density", "question": rho["why"]}
        )

    # --- volume continuity -----------------------------------------------
    continuity: dict[str, Any] = {
        "released_ul": released["volume_ul"],
        "released_source": released["source"],
        "drawn_ul": drawn["volume_ul"],
        "drawn_source": drawn["source"],
        "drawn_reagent": drawn.get("reagent"),
    }
    c_sev = "medium"
    if released["volume_ul"] and drawn["volume_ul"]:
        ratio = released["volume_ul"] / drawn["volume_ul"]
        continuity.update(
            {
                "released_over_drawn": ratio,
                "surplus_ul": released["volume_ul"] - drawn["volume_ul"],
                "balanced": 0.8 <= ratio <= 1.25,
                "message": f"{released['volume_ul']:.4g} uL released into a stage that draws "
                f"{drawn['volume_ul']:.4g} uL ({ratio:.3g}x)",
            }
        )
        c_sev = "blocker" if ratio > 4 or ratio < 0.25 else "high" if not continuity["balanced"] else "low"
        if not continuity["balanced"]:
            continuity["missing_step"] = (
                "a concentration step" if ratio > 1 else "a dilution or a second pass"
            ) + " is needed between the two chips and the chain documents none"
    if drawn.get("reagent") and handoff.carries not in (drawn["reagent"] or ""):
        continuity["reagent_mismatch"] = (
            f"the downstream step draws '{drawn['reagent']}' through "
            f"{', '.join(handoff.to_ports) or 'no named port'}, but this handoff carries "
            f"{handoff.carries}: check that the inlet is really the one the sample arrives at"
        )

    # --- particle vs the channels on both sides --------------------------
    factor = contracts.param("capsule_channel.width_factor_low")
    up_feats = port_constrictions(chip_a, handoff.from_ports[0] if handoff.from_ports else None, handoff.carries)
    down_feats = port_constrictions(
        chip_b, handoff.to_ports[0] if handoff.to_ports else None, handoff.carries
    )
    particles: dict[str, Any] = {
        **particle,
        "clog_factor": factor,
        "clog_factor_source": "contracts/parameters.json capsule_channel.width_factor_low",
        "required_opening_mm": (particle["diameter_um"] / 1000.0 * factor) if particle["diameter_um"] else None,
        "line_narrowest_bore_mm": line.narrowest_bore_mm,
        "upstream_features": up_feats,
        "downstream_features": down_feats,
    }
    pa_sev = "medium"
    if particle["diameter_um"]:
        need = particle["diameter_um"] / 1000.0 * factor
        d_mm = particle["diameter_um"] / 1000.0
        checked = []
        for side, feats in (("upstream", up_feats), ("downstream", down_feats)):
            for f in feats:
                checked.append(
                    {
                        "side": side,
                        **f,
                        "margin": f["opening_mm"] / d_mm,
                        "clog": (not f["by_design"]) and f["opening_mm"] < need,
                        "blocks": (not f["by_design"]) and f["opening_mm"] < d_mm,
                    }
                )
        checked.append(
            {
                "side": "line",
                "feature_id": line.id,
                "kind": "tubing",
                "opening_mm": line.narrowest_bore_mm,
                "by_design": False,
                "margin": line.narrowest_bore_mm / d_mm,
                "clog": line.narrowest_bore_mm < need,
                "blocks": line.narrowest_bore_mm < d_mm,
            }
        )
        particles["checked"] = checked
        particles["clogs"] = [c["feature_id"] for c in checked if c["clog"]]
        particles["blocks"] = [c["feature_id"] for c in checked if c["blocks"]]
        particles["retention_on_path"] = [c["feature_id"] for c in checked if c["by_design"]]
        pa_sev = "blocker" if particles["blocks"] else "high" if particles["clogs"] else "low"
        if not up_feats or not down_feats:
            pa_sev = _worst(pa_sev, "medium")
            unresolved.append(
                {
                    "handoff": handoff.id,
                    "field": "particle_vs_channels",
                    "question": "one side of this handoff has no chip or no named port, so the "
                    "particle could only be checked against the other side and the line",
                }
            )
    else:
        unresolved.append(
            {
                "handoff": handoff.id,
                "field": "particle_um",
                "question": particle["source"],
            }
        )

    # --- joint operating point -------------------------------------------
    # For a boundary handoff the upstream chip is the nearest one BEHIND the placeholders:
    # module 3 is what physically hands capsules to module 4, because barcoding and hairpin
    # have no chip to hand anything through. Composing module 3 + line + module 4 is the
    # real joint network, and saying so is better than refusing to solve it.
    joint_a, joint_a_port, joint_a_proto, joint_a_label = chip_a, (handoff.from_ports[0] if handoff.from_ports else None), proto_a, None
    joint_a_step = handoff.after_step
    if chip_a is None and from_stage.pass_through and upstream_fallback is not None:
        sub = chain.stages[upstream_fallback.from_stage]
        if sub.chip_path and upstream_fallback.from_ports:
            joint_a = chips.get(sub.key)
            joint_a_port = upstream_fallback.from_ports[0]
            joint_a_proto = protocols.get(sub.key)
            joint_a_step = upstream_fallback.after_step
            joint_a_label = (
                f"{sub.key} ({joint_a_port}) stands in for '{handoff.from_stage}': the "
                f"stage(s) between them are placeholders with no chip, so this is the chip "
                f"that physically drives the transfer"
            )

    joint: dict[str, Any]
    if joint_a is None or chip_b is None:
        missing = []
        if joint_a is None:
            missing.append(f"'{from_stage.key}' has no chip and no pass-through chip behind it")
        if chip_b is None:
            missing.append(f"'{to_stage.key}' has no chip")
        joint = {
            "solved": False,
            "why": "boundary handoff: "
            + " and ".join(missing)
            + ". The interface is modelled (line, dead volume, what the next stage must "
            "accept) but there is no network to compose.",
        }
    elif not joint_a_port or not handoff.to_ports:
        side = "upstream" if not joint_a_port else "downstream"
        joint = {
            "solved": False,
            "why": f"the {side} port is TBC in the chain, so there is no node to attach the "
            f"line to. This is a real gap in the chain, not a limitation here.",
        }
    elif drive is None:
        joint = {"solved": False, "why": "neither side states a flow, so there is nothing to drive"}
    else:
        step_a = next(
            (s for s in (joint_a_proto or {}).get("steps", []) if s.get("step") == joint_a_step), {}
        )
        step_b = next(
            (
                s
                for s in (proto_b or {}).get("steps", [])
                if set((s.get("ports") or {}).get("feed") or []) & set(handoff.to_ports)
            ),
            {},
        )
        if not step_a and joint_a_proto:
            step_a = next(
                (
                    s
                    for s in reversed(joint_a_proto.get("steps", []))
                    if joint_a_port in ((s.get("ports") or {}).get("vent") or [])
                    and s.get("operation") in RELEASE_OPS
                ),
                {},
            )
        a_states = protocol_port_states(step_a, [p["id"] for p in joint_a.get("ports", [])])
        b_states = protocol_port_states(step_b, [p["id"] for p in chip_b.get("ports", [])])
        inferred = None
        if not (step_a.get("ports") or {}):
            inferred = infer_drive(joint_a, joint_a_port)
            if inferred["feed"]:
                a_states[inferred["feed"]] = "feed"
                for vent in inferred.get("vents") or []:
                    a_states.setdefault(vent, "cap")
            unresolved.append(
                {
                    "handoff": handoff.id,
                    "field": "upstream_port_states",
                    "question": f"the upstream transfer step states no open ports, so what "
                    f"drives this transfer is not written down. {inferred['why']}",
                }
            )
        upstream_drive = (
            outlet_flow_ul_min(joint_a_proto, step_a.get("step"))["flow_ul_min"] or drive
        )
        args = (joint_a, chip_b, joint_a_port, handoff.to_ports[0], line, mu.pa_s,
                upstream_drive, a_states, b_states)
        joint, fell_back = {}, None
        if MESH_AVAILABLE:
            try:
                joint = joint_operating_point_mesh(*args)
            except Exception as exc:  # noqa: BLE001 - mesh.py may not handle every chip yet
                fell_back = f"mesh.py/solve.py could not do it ({type(exc).__name__}: {exc})"
        try:
            if not joint:
                joint = joint_operating_point(*args)
                if fell_back:
                    joint["fell_back_from_mesh"] = fell_back
            joint["drive_source"] = (
                f"upstream step {step_a.get('step')} flow_ul_min" if step_a.get("flow_ul_min") else drive_source
            )
            joint["upstream_step"] = step_a.get("step")
            joint["downstream_step"] = step_b.get("step")
            if inferred:
                joint["drive_port_inferred"] = inferred["why"]
            if joint_a_label:
                joint["composed_from"] = joint_a_label
        except (CoupleError, KeyError, ValueError, ZeroDivisionError) as exc:
            joint = {"solved": False, "why": f"the joint network did not solve: {exc}"}

    j_sev = "high" if joint.get("solved") and (joint.get("weir") or {}).get("over_limit") else "low"
    if not joint.get("solved"):
        j_sev = "medium"

    parts = {
        "ports": pr_sev,
        "pressure": p_sev,
        "dead_volume": d_sev,
        "settling": s_sev,
        "continuity": c_sev,
        "particles": pa_sev,
        "joint": j_sev,
    }
    severity = _worst(*parts.values())

    # Flat keys analyse.py's handoff checks look for (`from_chip`, `narrowest_passable_um`,
    # `released_volume_ul`, ...). The nested blocks below are the full picture; these are
    # the same numbers hoisted so a consumer does not have to know this module's layout.
    passable_um = None
    if particles.get("checked"):
        openings = [c["opening_mm"] for c in particles["checked"] if not c["by_design"]]
        passable_um = min(openings) * 1000.0 if openings else None

    return {
        "id": handoff.id,
        "carries": handoff.carries,
        "from_chip": handoff.from_stage,
        "to_chip": handoff.to_stage,
        "from_port": handoff.from_ports[0] if handoff.from_ports else None,
        "to_port": handoff.to_ports[0] if handoff.to_ports else None,
        "after_step": handoff.after_step,
        "particle_um": particle["diameter_um"],
        "narrowest_passable_um": passable_um,
        "released_volume_ul": released["volume_ul"],
        "drawn_volume_ul": drawn["volume_ul"],
        "dead_volume_ul": line.dead_volume_ul,
        "from": {
            "stage": handoff.from_stage,
            "name": from_stage.name,
            "chip": from_stage.chip_path,
            "protocol": from_stage.protocol_path,
            "ports": list(handoff.from_ports),
            "after_step": handoff.after_step,
            "on_chip": from_stage.on_chip,
            "off_chip_reason": from_stage.off_chip_reason,
        },
        "to": {
            "stage": handoff.to_stage,
            "name": to_stage.name,
            "chip": to_stage.chip_path,
            "protocol": to_stage.protocol_path,
            "ports": list(handoff.to_ports),
            "on_chip": to_stage.on_chip,
            "off_chip_reason": to_stage.off_chip_reason,
        },
        "boundary": boundary,
        "automated": automated,
        "automated_why": automated_why,
        "connector": handoff.connector,
        "ports_check": ports_check,
        "line": line.as_json(),
        "pressure": pressure,
        "dead_volume": dead_volume,
        "settling": settling,
        "continuity": continuity,
        "particles": particles,
        "joint": joint,
        "severity": severity,
        "severity_parts": parts,
        "severity_score": severity_score(parts),
        "notes": notes,
        "chain_note": handoff.note,
        "unresolved": unresolved,
    }


# --- top level ----------------------------------------------------------------


def couple(chain: Chain, temp_c: float | None = None, validated: bool = False) -> dict:
    """Every handoff in the chain, as `pipeline.field.json`.

    `temp_c` picks the viscosity through `contracts.viscosity_for`; None means the 37 C
    value, which is what the real device runs at. Water at 20 C belongs to the dye demo and
    is never used for a transfer.

    `validated` runs validate.py's benchmarks and records the verdict. It is off by default
    because it is seconds of work and this function is called repeatedly; `run()` - the
    path that writes the file anyone reads - turns it on, because a written finding with no
    statement about whether the solver has been checked is the thing the README forbids.
    """
    mu = contracts.viscosity_for(temp_c)
    chips: dict[str, dict] = {}
    protocols: dict[str, dict] = {}
    unresolved = [dict(u) for u in chain.unresolved]
    for key, stage in chain.stages.items():
        if stage.chip_path:
            path = ROOT / stage.chip_path
            if path.exists():
                chips[key] = stage_io.load_json(path)
            else:
                unresolved.append({"stage": key, "question": f"{stage.chip_path} does not exist"})
        if stage.protocol_path:
            path = ROOT / stage.protocol_path
            if path.exists():
                protocols[key] = stage_io.load_json(path)
            else:
                unresolved.append({"stage": key, "question": f"{stage.protocol_path} does not exist"})

    # For a boundary whose upstream stage is a placeholder, the volume still comes from the
    # last stage that had a protocol: the placeholders carry it through unchanged.
    fallbacks: dict[str, Handoff | None] = {}
    for i, h in enumerate(chain.handoffs):
        back = None
        for prev in reversed(chain.handoffs[:i]):
            if chain.stages[prev.from_stage].protocol_path and prev.from_ports:
                back = prev
                break
        fallbacks[h.id] = back

    results = [
        analyse_handoff(h, chain, chips, protocols, mu, fallbacks.get(h.id)) for h in chain.handoffs
    ]
    manual = [r for r in results if not r["automated"]]
    worst = max(
        results,
        key=lambda r: (SEVERITY_ORDER.get(r["severity"], -1), r["severity_score"]),
        default=None,
    )
    for r in results:
        unresolved.extend(r["unresolved"])

    return {
        "kind": "cfd_pipeline_field",
        "schema_note": "assay2chip/cfd/README.md, couple.py row. Units are in every key "
        "name: mm, uL, uL/min, kPa, Pa, s, dyn/cm2, um.",
        "pipeline_id": chain.id,
        "source": chain.source,
        "source_kind": chain.kind,
        "generated_by": "assay2chip/cfd/couple.py",
        "validation": validation()
        if validated
        else {
            "ran": False,
            "trusted": None,
            "why": "not run on this call (couple(validated=False)); run() and the CLI run "
            "it, and assay2chip/cfd/validate.py can be run on its own",
        },
        "settling_delegated_to": "assay2chip/cfd/particles.py"
        if PARTICLES_AVAILABLE
        else "local Stokes line (particles.py not importable)",
        "solver": {
            "preferred": "assay2chip/cfd/mesh.py + solve.py, composed into one Mesh here",
            "fallback": "operator/network.py edges (lumped chambers), solved here",
            "line": "Poiseuille, round tube, as model.Edge.g_geom_m3",
            "composition": "both chips and the line in one system, so chip B's "
            "back-pressure reaches chip A's weir",
            "model_py": cfd_model is not None,
            "mesh_py": cfd_mesh is not None,
            "solve_py": cfd_solve is not None,
            "mesh_path_available": MESH_AVAILABLE,
            "in_use": sorted(
                {
                    h["joint"].get("solver", "")
                    for h in results
                    if h["joint"].get("solved")
                }
            )
            or None,
            "todo": None
            if MESH_AVAILABLE
            else "mesh.py/solve.py are not importable, so the chip side comes from "
            "operator/network.py - which the README names as the independent "
            "implementation S10 must agree with, so the numbers are usable, but chambers "
            "are lumped and a weir pressure is a chamber average.",
        },
        "contracts": {
            "viscosity_pa_s": mu.pa_s,
            "viscosity_key": mu.key,
            "viscosity_scope": mu.scope,
            "capsule_diameter_um": contracts.capsule_diameter_um(),
            "capsule_swell_factor": contracts.capsule_swell_factor(),
            "swollen_capsule_diameter_um": contracts.swollen_capsule_diameter_um(),
            "escape_pressure_kpa": contracts.escape_pressure_kpa(),
            "pressure_margin": contracts.pressure_margin(),
            "max_working_pressure_kpa": contracts.max_working_pressure_kpa(),
            "min_port_mm": contracts.floor("min_port_mm"),
            "fingerprint": contracts.fingerprint(),
        },
        "assumptions": contract_assumptions() + [a.as_json() for a in ASSUMPTIONS.values()],
        "assumptions_note": "The line's dimensions and every density are CONTRACT keys "
        "(contracts/parameters.json), read through contracts.param(); this module keeps no "
        "copy. They are still marked assumption: true there - nobody has measured the "
        "tubing or the densities - so results that rest on them say so. Only the connector "
        "(port bore and lid thickness, off the chip spec) and the number of valves in a "
        "line are not assumptions and not contract keys respectively. Settling is LINEAR "
        "in excess density, so each settling result names the end of the band it used and "
        "reports the verdict across the whole band.",
        "stages": {
            k: {
                "name": s.name,
                "status": s.status,
                "chip": s.chip_path,
                "protocol": s.protocol_path,
                "on_chip": s.on_chip,
                "boundary": not s.on_chip,
                "off_chip_reason": s.off_chip_reason,
            }
            for k, s in chain.stages.items()
        },
        "handoffs": results,
        "automation": {
            "handoffs": len(results),
            "automated": len(results) - len(manual),
            "manual": len(manual),
            "manual_ids": [r["id"] for r in manual],
            "note": "`manual` is the honest answer to 'does this run without a human?' "
            "(contracts/schemas/partition.schema.json, handoffs.automated).",
        },
        "worst_handoff": None
        if worst is None
        else {
            "id": worst["id"],
            "severity": worst["severity"],
            "parts": worst["severity_parts"],
            "why": "; ".join(
                m
                for m in (
                    next(
                        (
                            f"{s['stage']} {k} port: {s['verdict']}"
                            for k, s in worst["ports_check"]["sides"].items()
                            if s["verdict"] != "ok"
                        ),
                        None,
                    ),
                    worst["continuity"].get("message"),
                    f"blocked by {', '.join(worst['particles'].get('blocks') or [])}"
                    if worst["particles"].get("blocks")
                    else None,
                    f"clogs at {', '.join(worst['particles'].get('clogs') or [])}"
                    if worst["particles"].get("clogs")
                    else None,
                    worst["dead_volume"].get("message")
                    or (
                        f"{worst['dead_volume']['dead_volume_ul']:.4g} uL dead volume"
                        if worst["dead_volume"].get("fraction_lost_in_line", 0) > 0.1
                        else None
                    ),
                )
                if m
            ),
        },
        "unresolved": unresolved,
    }


def run(
    path: Path | str,
    out: Path | str | None = None,
    temp_c: float | None = None,
    validated: bool = True,
) -> tuple[dict, Path]:
    """Couple a chain and write `cfd/pipeline.field.json`. Returns (payload, path)."""
    chain = read_chain(path)
    payload = couple(chain, temp_c, validated=validated)
    if out is None:
        out = stage_io.work_dir(chain.id, create=True) / "cfd" / "pipeline.field.json"
    stage_io.write_json(out, payload)
    return payload, Path(out)


# --- CLI ----------------------------------------------------------------------


def _fmt(value: Any, spec: str = ".4g") -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return format(value, spec)
    return str(value)


def summarise(payload: dict) -> str:
    """One block per handoff, with every number, for the terminal and the report."""
    lines = [
        f"{payload['pipeline_id']}  ({payload['source']})",
        f"viscosity {payload['contracts']['viscosity_pa_s']} Pa s "
        f"({payload['contracts']['viscosity_key']});  swollen capsule "
        f"{payload['contracts']['swollen_capsule_diameter_um']} um;  pressure limit "
        f"{payload['contracts']['max_working_pressure_kpa']} kPa",
        "",
        "FLAGGED ASSUMPTIONS (contract-level unless marked local):",
    ]
    for a in payload["assumptions"]:
        band = (
            f"  ({a['min']}-{a['max']})"
            if a.get("min") is not None and a.get("max") is not None
            else ""
        )
        where = "contract" if a.get("contract") else "LOCAL to couple.py"
        lines.append(f"  {a['key']} = {a['value']} {a['units']}{band}   [{where}]")
        lines.append(f"      {a['source']}")
    lines.append("")
    for h in payload["handoffs"]:
        lines.append(
            f"--- {h['id']}  [{h['severity'].upper()}]  carries {h['carries']}"
            + ("  BOUNDARY" if h["boundary"] else "")
        )
        lines.append(
            f"    {h['from']['stage']}:{','.join(h['from']['ports']) or 'TBC'} -> "
            f"{h['to']['stage']}:{','.join(h['to']['ports']) or 'TBC'}"
            f"   automated: {'yes' if h['automated'] else 'NO'} ({h['automated_why']})"
        )
        for side, chk in h["ports_check"]["sides"].items():
            if chk["verdict"] != "ok":
                lines.append(f"    ports     {side} ({chk['stage']}): {chk['verdict']}")
        ln, p, dv, st, co, pa, jo = (
            h["line"], h["pressure"], h["dead_volume"], h["settling"], h["continuity"],
            h["particles"], h["joint"],
        )
        lines.append(
            f"    line      bore {_fmt(ln['narrowest_bore_mm'])} mm, {_fmt(ln['length_mm'])} mm long, "
            f"R {_fmt(ln['resistance_kpa_per_ul_min'])} kPa per uL/min, dead "
            f"{_fmt(ln['dead_volume_ul'])} uL"
        )
        lines.append(
            f"    pressure  {_fmt(p.get('p_upstream_outlet_needed_kpa'))} kPa needed at the "
            f"upstream outlet to drive {_fmt(p.get('downstream_flow_ul_min'))} uL/min "
            f"(limit {_fmt(p['limit_kpa'])} kPa, "
            f"{_fmt(p.get('fraction_of_limit'))}x);  Re {_fmt(p.get('reynolds_in_line'))}"
        )
        lines.append(
            f"    dead vol  {_fmt(dv['dead_volume_ul'])} uL in the line vs "
            f"{_fmt(dv['volume_carried_ul'])} uL carried -> "
            f"{_fmt(dv.get('fraction_lost_in_line'))} lost, or "
            f"{_fmt(dv.get('dilution_if_line_primed'))}x dilution if primed"
            + (
                f";  {_fmt(dv['line_volumes_per_transfer'])} line volumes per "
                f"{_fmt(dv['volume_per_transfer_ul'])} uL transfer x{dv.get('transfers')}"
                if dv.get("line_volumes_per_transfer")
                else ""
            )
        )
        lines.append(
            f"    settling  {_fmt(st.get('particle_um'))} um particle at "
            f"{_fmt(st.get('excess_density_kg_m3'))} kg/m3 excess "
            f"({st.get('excess_density_key') or 'no contract key'}, "
            f"{st.get('excess_density_end_used') or '-'} of the band) settles "
            f"{_fmt(st.get('settling_velocity_mm_s'))} mm/s -> "
            f"{_fmt(st.get('time_to_settle_across_bore_s'))} s across the bore vs "
            f"{_fmt(st.get('transit_time_in_line_s'))} s transit "
            f"({'SETTLES FIRST' if st.get('settles_before_it_arrives') else 'arrives first'})"
        )
        if st.get("particles_module_verdict"):
            pv = st["particles_module_verdict"]
            lines.append(
                f"              particles.py [{pv['intent']}] {pv['verdict'].upper()} "
                f"({pv['severity']}): {pv['diameter_um']:.4g} um ({pv['diameter_end']} end) "
                f"at {pv['excess_density_kg_m3']:.4g} kg/m3 ({pv['density_end']} end) falls "
                f"{pv['depth_um']:.0f} um in {pv['fall_time_s']:.4g} s vs "
                f"{pv['transit_time_s']:.4g} s transit -> "
                f"{pv['settled_fraction'] * 100:.0f}% drops out"
            )
        if st.get("verdict_across_the_band"):
            band = st["across_the_band"]
            lines.append(
                "              across the band: "
                + ";  ".join(
                    f"{end} {_fmt(band[end]['excess_density_kg_m3'])} kg/m3 -> "
                    f"{_fmt(band[end]['time_to_settle_across_bore_s'])} s "
                    f"({'settles' if band[end].get('settles_before_it_arrives') else 'arrives'})"
                    for end in ("min", "value", "max")
                    if band.get(end)
                )
                + f"  [verdict holds throughout: "
                f"{'yes' if st.get('verdict_holds_across_the_whole_band') else 'NO'}]"
            )
        lines.append(
            f"    continuity {_fmt(co.get('released_ul'))} uL released vs "
            f"{_fmt(co.get('drawn_ul'))} uL drawn"
            + (f" = {_fmt(co.get('released_over_drawn'))}x" if co.get("released_over_drawn") else "")
            + (f"  [{co['missing_step']}]" if co.get("missing_step") else "")
        )
        lines.append(
            f"    particles {_fmt(pa.get('diameter_um'))} um needs "
            f"{_fmt(pa.get('required_opening_mm'))} mm; clogs: "
            f"{', '.join(pa.get('clogs') or []) or 'none'}; blocked by: "
            f"{', '.join(pa.get('blocks') or []) or 'none'}; retention on path: "
            f"{', '.join(pa.get('retention_on_path') or []) or 'none'}"
        )
        if jo.get("solved"):
            w = jo["weir"]
            lines.append(
                f"    joint     {w['feature_id']} weir {_fmt(w['p_alone_kpa'])} kPa alone -> "
                f"{_fmt(w['p_joint_kpa'])} kPa coupled (chip B adds "
                f"{_fmt(w['back_pressure_from_chip_b_kpa'])} kPa, limit "
                f"{_fmt(w['limit_kpa'])} kPa); line carries "
                f"{_fmt(jo['q_through_line_ul_min'])} uL/min at "
                f"{_fmt(jo['drive_ul_min'])} uL/min drive"
            )
            if jo.get("mesh"):
                lines.append(
                    f"              one network of {jo['mesh']['nodes']} nodes / "
                    f"{jo['mesh']['edges']} edges over "
                    f"{' + '.join(jo['mesh']['chips'])} + the line "
                    f"({jo['mesh']['weir_cells_on_chip_a']} weir cells); mass imbalance "
                    f"{_fmt(jo['residual']['mass_imbalance_max'], '.2e')} m3/s"
                )
            if jo.get("composed_from"):
                lines.append(f"              {jo['composed_from']}")
        else:
            lines.append(f"    joint     not solved: {jo.get('why')}")
        lines.append("")
    a = payload["automation"]
    lines.append(
        f"{a['automated']} of {a['handoffs']} handoffs are automated; {a['manual']} need a "
        f"human: {', '.join(a['manual_ids'])}"
    )
    if payload.get("worst_handoff"):
        lines.append(
            f"worst: {payload['worst_handoff']['id']} ({payload['worst_handoff']['severity']}) "
            f"- {payload['worst_handoff']['why']}"
        )
    lines.append(f"{len(payload['unresolved'])} unresolved question(s)")
    v = payload.get("validation") or {}
    if v.get("ran"):
        lines.append(
            f"solver: {'VALIDATED' if v['trusted'] else 'UNTRUSTED'} "
            f"({v['benchmarks_passed']}/{v['benchmarks']} benchmarks, validate.py)"
            + (f" - {v['untrusted_reason']}" if v.get("untrusted_reason") else "")
        )
    else:
        lines.append(f"solver: NOT CHECKED on this run - {v.get('why', '')}")
    lines.append(f"settling: {payload.get('settling_delegated_to')}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pipeline", help="a kind:'pipeline' file, e.g. protocols/pipeline_mvp.json")
    src.add_argument("--partition", help="a stage-S4 partition.json")
    ap.add_argument("-o", "--out", help="where to write pipeline.field.json")
    ap.add_argument("--temp-c", type=float, default=None, help="step temperature for viscosity_for")
    ap.add_argument("--json", action="store_true", help="print the payload instead of the summary")
    ap.add_argument("--no-validate", action="store_true",
                    help="skip validate.py's benchmarks (the file then says it is untrusted)")
    args = ap.parse_args(argv)
    payload, out = run(
        args.pipeline or args.partition, args.out, args.temp_c, validated=not args.no_validate
    )
    print(json.dumps(payload, indent=2) if args.json else summarise(payload))
    if not args.json:
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""What happens to cells, capsules and beads in the flow.

Module 6 of the CFD package (see `assay2chip/cfd/README.md`, "Module contracts"):

    particles.py | Mesh + Field + particle class | settling vs transit per edge/cell,
                 | clog margin per edge, weir escape check

Four questions, and the first one is the one this repo keeps getting wrong:

1. **Sedimentation.** Stokes settling `v_s = drho g d^2 / (18 mu)`, compared per edge and
   per cell against the transit time. The chip lies FLAT, so gravity is normal to the
   Hele-Shaw plane: settling does not bend an in-plane streamline, it *removes* the
   particle from the stream after it has fallen the local depth. The number alone is
   meaningless, because **settling is a desired mechanism in one feature and a loss in the
   next one along**. An audit found capsules settling in Module 4's feed lines and cells
   settling in cell_prep's mixer - both losses - while the very same velocity in Module 3's
   trap or a weir chamber is the point of the feature. `sediment_intent()` decides which
   from the block id and the retention data and records *why* in every finding; nothing
   here reports a bare settling number without its intent.

2. **Clogging.** Smallest channel dimension vs `capsule_channel.width_factor_low` x the
   capsule diameter - and capsules SWELL by `capsule.swell_factor`, so the diameter that
   matters depends on whether the line carries fresh or cultured capsules.

3. **Weir escape.** Pressure across each weir slit vs `capsule.escape_pressure_kpa` x
   `control.pressure_margin`, plus a sweep of the gap over the +/-20 um z tolerance the
   contract warns about. Slit flow scales as gap^3, and retention fails outright once the
   gap reaches `trap.gap_factor_high` x D. `weir_tolerance_band()` reports the band of
   gaps - and so of z offsets - in which the weir still works.

4. **Tracking.** Particles are traced from each feed through the reconstructed cell
   velocity field (RK4) with settling, and the fractions that reach each outlet, settle
   where intended, or are lost are reported per class.

Every contract number comes from `assay2chip/lib/contracts.py`. Nothing is hard-coded.
Where the contracts have no value - and they have NO density or density-difference key at
all - the number is a `Assumption`, carried into every finding that rests on it, exactly as
`contracts.is_assumption()` does for the parameters that are flagged in the file itself.

    python assay2chip/cfd/particles.py --chip module3_v1_translated
    python assay2chip/cfd/particles.py --all
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402

# --- sibling modules ---------------------------------------------------------
# model.py owns the shapes and solve.py owns Field (README, "Module contracts"). When they
# are present this module uses THEM - the dataclasses below are only a fallback for the
# window in which they do not exist yet, and they carry exactly the README's field names
# so that nothing downstream can tell the difference.

try:
    from assay2chip.cfd import model as _model  # type: ignore
except Exception:  # noqa: BLE001  # pragma: no cover
    _model = None

try:
    from assay2chip.cfd import mesh as _mesh_mod  # type: ignore
except Exception:  # noqa: BLE001  # pragma: no cover
    _mesh_mod = None

try:
    from assay2chip.cfd import scenario as _scenario_mod  # type: ignore
except Exception:  # noqa: BLE001  # pragma: no cover
    _scenario_mod = None

try:
    from assay2chip.cfd import solve as _solve_mod  # type: ignore
except Exception:  # noqa: BLE001  # pragma: no cover
    _solve_mod = None

USING_FALLBACK_SHAPES = _model is None
HAVE_REAL_PIPELINE = None not in (_mesh_mod, _scenario_mod, _solve_mod, _model)

G_M_S2 = 9.80665  # standard gravity: a physical constant, not a contract value
TICKS = None  # unused here; time in this module is seconds

SEC_PER_MIN = 60.0
UL_PER_M3 = 1e9
PA_PER_KPA = 1000.0
DYN_CM2_PER_PA = 10.0


# --- shapes (README field names) ---------------------------------------------


@dataclass
class Node:
    """A mesh node. `kind` is 'cell', 'junction' or 'port' (README mesh.py row)."""

    id: str
    kind: str
    x_mm: float
    y_mm: float
    area_mm2: float
    depth_mm: float
    feature_id: str | None = None


@dataclass
class Edge:
    """A mesh edge. `kind` is 'duct', 'hs_face' or 'mouth'; 'slit' is a weir face."""

    id: str
    a: str
    b: str
    kind: str
    conductance_m3_per_pa_s: float
    length_mm: float
    width_mm: float
    depth_mm: float
    feature_id: str | None = None


@dataclass
class Mesh:
    nodes: list[Node]
    edges: list[Edge]
    ports: dict[str, str]
    meta: dict[str, Any]

    def node(self, node_id: str) -> Node:
        return self._by_id()[node_id]

    def _by_id(self) -> dict[str, Node]:
        if not hasattr(self, "_idx") or len(self._idx) != len(self.nodes):
            self._idx = {n.id: n for n in self.nodes}
        return self._idx

    def edge(self, edge_id: str) -> Edge:
        for e in self.edges:
            if e.id == edge_id:
                return e
        raise KeyError(edge_id)


@dataclass
class Field:
    """solve.py's Field (README solve.py row). `u_mm_s` is a vector per cell."""

    p_pa: dict[str, float] = field(default_factory=dict)
    q_m3_s: dict[str, float] = field(default_factory=dict)
    u_mm_s: dict[str, tuple[float, float]] = field(default_factory=dict)
    shear_dyn_cm2: dict[str, float] = field(default_factory=dict)
    re: dict[str, float] = field(default_factory=dict)
    residual: dict[str, float] = field(default_factory=dict)


if _model is not None:  # prefer the real shapes over the fallbacks above
    Node = _model.Node          # type: ignore[misc,assignment]
    Edge = _model.Edge          # type: ignore[misc,assignment]
    Mesh = _model.Mesh          # type: ignore[misc,assignment]
    Field = _model.Field        # type: ignore[misc,assignment]


def _residual_value(residual: Any, key: str) -> float:
    """`Field.residual` is a dict in the fallback and a `model.Residual` in the real one."""
    if isinstance(residual, dict):
        return float(residual.get(key, 0.0))
    return float(getattr(residual, key, 0.0))


# --- assumptions -------------------------------------------------------------


@dataclass(frozen=True)
class Assumption:
    """A number this module needs that the contracts do not carry.

    `contracts.param()` refuses to default, which is right: a silently defaulted contract
    value is indistinguishable from a guess. So anything the contracts do not have is
    recorded here instead, with where the figure came from and what it changes, and is
    carried into every finding that rests on it.
    """

    key: str
    value: float
    units: str
    why: str
    source: str
    derived_from: str | None = None

    def as_json(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "units": self.units,
            "why": self.why,
            "source": self.source,
            "derived_from": self.derived_from,
            "assumption": True,
        }


# The contracts have NO density key of any kind (grep 'densit' over contracts/ finds one
# prose mention of adherent cells per microlitre and nothing else), so every density
# difference below is flagged. Two of the three are DERIVED from figures the contracts do
# carry, which is better provenance than a textbook number, and both say so.

CARRIER_DENSITY_KEY = "fluid.density_37c_kg_m3"
EXCESS_DENSITY_KEYS = {
    "cells": "cell.excess_density_kg_m3",
    "capsule": "capsule.excess_density_kg_m3",
    "capsule_swollen": "capsule.excess_density_kg_m3",
    "bead": "bead.excess_density_kg_m3",
}


@dataclass(frozen=True)
class ExcessDensity:
    """A particle's density minus the medium's, from the contracts, WITH its band.

    The band is not decoration. Stokes settling is LINEAR in this number, and the
    contract notes say so explicitly: "use max when predicting cells LOST by settling in
    a line or mixer, min when RELYING on settling to retain cells in a chamber, and
    report which end you used". That is the same rule this module already applies to
    diameter, and for the same reason - the conservative end depends on the question, so
    one number read two ways is a wrong answer half the time.

    `for_intent` is the single place that choice is made, so no caller can forget it.
    """

    key: str
    value: float
    min_kg_m3: float
    max_kg_m3: float
    assumption: bool
    note: str

    def for_intent(self, intent: str) -> tuple[float, str]:
        """(excess density, which end) for this intent. The contract's rule, in code."""
        if intent == "loss":
            # Densest plausible particle settles fastest, so it loses the most.
            return self.max_kg_m3, "max"
        if intent == "desired":
            # Least dense plausible particle settles slowest, so it is the one the trap
            # fails to catch.
            return self.min_kg_m3, "min"
        # 'parked': the verdict does not turn on it, so the nominal is honest.
        return self.value, "nominal"

    def span_ratio(self) -> float:
        """How much the band alone moves a settling time. 4x for capsules."""
        return self.max_kg_m3 / self.min_kg_m3 if self.min_kg_m3 else math.inf

    def as_assumption(self) -> Assumption:
        return Assumption(
            key=self.key,
            value=self.value,
            units="kg/m3",
            why=("Contract value, carried here because it is marked assumption: true. "
                 "Settling is linear in it, so the min/max band matters as much as the "
                 f"value: {self.min_kg_m3}-{self.max_kg_m3} kg/m3 spans "
                 f"{self.span_ratio():.1f}x in settling time. " + self.note),
            source=f"contracts/parameters.json {self.key}",
        )


def excess_density(key: str) -> ExcessDensity:
    """One particle's excess density from contracts/parameters.json, with its band."""
    entry = contracts.param_entry(key)
    lo, hi = contracts.param_range(key)
    value = float(contracts.param(key))
    return ExcessDensity(
        key=key,
        value=value,
        min_kg_m3=float(lo if lo is not None else value),
        max_kg_m3=float(hi if hi is not None else value),
        assumption=bool(contracts.is_assumption(key)),
        note=str(entry.get("note", "")),
    )


def carrier_density() -> ExcessDensity:
    """Medium density (`fluid.density_37c_kg_m3`).

    Only Reynolds numbers need it - Stokes settling uses the DIFFERENCE, which the
    contract now gives directly as the `*.excess_density_kg_m3` keys, so the carrier is
    no longer subtracted from anything here. `solve.py` reads the same key for its own
    Reynolds numbers, so the two cannot disagree.
    """
    return excess_density(CARRIER_DENSITY_KEY)


# The carrier density, read once. Only Reynolds numbers use it: Stokes settling takes the
# contract's *.excess_density_kg_m3 difference directly.
MEDIUM_DENSITY = carrier_density()


def capsule_sump_band_crosscheck(mu_pa_s: float | None = None) -> dict:
    """Does the contract's capsule excess density agree with its own sump settling band?

    `contracts/blocks.json` retention.alternatives_ranked offers a "Sedimentation sump:
    keep upward velocity below the ~50-210 um/s settling speed". That is the contract
    stating how fast a capsule falls, so it is an independent check on
    `capsule.excess_density_kg_m3` - two parts of the contract that must agree. They
    nearly do: the band inverts to about 15 kg/m3, which sits just above the stated
    minimum of 12. Reported, not used: the parameter is authoritative.
    """
    mu = mu_pa_s or contracts.viscosity_37c_pa_s().pa_s
    text = ""
    for entry in contracts.block("retention.alternatives_ranked"):
        if "sediment" in str(entry).lower():
            text = str(entry)
            break
    band = _prose_numbers(text, r"~?(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*um/s")
    ex = excess_density(EXCESS_DENSITY_KEYS["capsule"])
    if len(band) != 2:  # pragma: no cover - only if the contract prose changes
        return {"agrees": None, "why": "could not parse the sump band"}
    d_small = contracts.capsule_diameter_um() * 1e-6
    d_large = contracts.swollen_capsule_diameter_um() * 1e-6
    implied_lo = 18.0 * mu * (band[0] * 1e-6) / (G_M_S2 * d_small**2)
    implied_hi = 18.0 * mu * (band[1] * 1e-6) / (G_M_S2 * d_large**2)
    implied = (implied_lo + implied_hi) / 2.0
    inside = ex.min_kg_m3 <= implied <= ex.max_kg_m3
    return {
        "sump_band_um_s": band,
        "implied_excess_density_kg_m3": round(implied, 2),
        "contract_band_kg_m3": [ex.min_kg_m3, ex.max_kg_m3],
        "contract_value_kg_m3": ex.value,
        "agrees": inside,
        "why": (
            f"blocks.json's {band[0]:.0f}-{band[1]:.0f} um/s sump band inverts through "
            f"Stokes to about {implied:.1f} kg/m3 at the unswollen and swollen diameters. "
            f"The parameter says {ex.value} ({ex.min_kg_m3}-{ex.max_kg_m3}), so the two "
            + ("agree: the sump figure sits inside the band, near its low end."
               if inside else
               "DISAGREE, and the report must say which is wrong.")
        ),
    }


RESUSPENSION_SHEAR_DYN_CM2 = Assumption(
    key="assumed.resuspension_shear_dyn_cm2",
    value=0.002,
    units="dyn/cm2",
    why=(
        "Floor shear above which a settled particle is swept up again, so a 'settled' "
        "verdict is not permanent. retention_audit.md estimates 0.002-0.007 dyn/cm2 from "
        "an *assumed* Shields theta of 0.03-0.1 and notes its own floor shear (0.008) "
        "sits above the band. The low end is used, so resuspension is reported "
        "optimistically and a loss finding is never hidden behind it."
    ),
    source="protocols/retention_audit.md (*assumed* Shields theta 0.03-0.1)",
)

START_HEIGHT_FRACTION = Assumption(
    key="assumed.start_height_fraction",
    value=1.0,
    units="fraction of local depth",
    why=(
        "How far a particle has to fall before it is out of the stream. A particle "
        "entering at a uniformly random height falls depth/2 on average, so a HALF-depth "
        "test gives the mean loss and a FULL-depth test gives the last particle to drop "
        "out. The full depth is used for the pass/fail verdict - matching "
        "retention_audit.md's 'A 300 um fall takes 50 s' - and the half-depth figure is "
        "reported alongside it as the mean."
    ),
    source="geometric; retention_audit.md uses the full depth",
)


def _prose_numbers(text: str, pattern: str) -> list[float]:
    m = re.search(pattern, text)
    if not m:
        return []
    return [float(g) for g in m.groups() if g is not None]


def cell_diameter_range_um() -> tuple[float, float, Assumption]:
    """Bare-cell diameter, read out of the contract's own prose.

    `contracts/blocks.json` retention._cells_caveat says the weir "does NOT retain a bare
    cell of 10-15 um". That is the contract's figure, so it is parsed from there rather
    than retyped - but it lives in prose and not in a numbered key, which is itself worth
    flagging.
    """
    caveat = str(contracts.block("retention._cells_caveat"))
    got = _prose_numbers(caveat, r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*um")
    note = Assumption(
        key="assumed.cell_diameter_um",
        value=got[1] if len(got) == 2 else 15.0,
        units="um",
        why=(
            "Cell diameter is NOT a numbered contract key. It appears only as prose in "
            "contracts/blocks.json retention._cells_caveat ('a bare cell of 10-15 um'), "
            "which this function parses, so the number traces to the contract even though "
            "the contract cannot be read for it programmatically. Add a cell.diameter_um "
            "key and this flag goes away."
        ),
        source="contracts/blocks.json retention._cells_caveat (prose, not a key)",
    )
    if len(got) != 2:
        return 10.0, 15.0, note
    return got[0], got[1], note


def z_tolerance_um() -> tuple[float, Assumption]:
    """The +/-20 um z tolerance, parsed from the contract's own warning."""
    warn = str(contracts.block("retention.weir_geometry.z_tolerance_warning"))
    got = _prose_numbers(warn, r"\+/-\s*(\d+(?:\.\d+)?)\s*um")
    value = got[0] if got else 20.0
    return value, Assumption(
        key="assumed.z_tolerance_um",
        value=value,
        units="um",
        why=(
            "Weir gap tolerance. Not a numbered key: it is prose in "
            "contracts/blocks.json retention.weir_geometry.z_tolerance_warning "
            "('around +/-20 um'), which that same warning calls THE BIGGEST UNKNOWN IN "
            "THE RETENTION SCHEME. Parsed rather than retyped."
        ),
        source="contracts/blocks.json retention.weir_geometry.z_tolerance_warning (prose)",
    )


# --- particle classes --------------------------------------------------------


@dataclass(frozen=True)
class ParticleClass:
    """One kind of thing carried by the flow.

    `diameter_um` is the nominal; `d_min_um`/`d_max_um` bracket it where the source gives a
    range. The bracket is not decoration: for a LOSS the worst case is the LARGEST
    particle (it falls fastest), and for a DESIRED capture the worst case is the SMALLEST
    (it is the one that fails to drop out, or slips the gap). `conservative_diameter_um()`
    picks the right end from the intent instead of leaving one number to be read both ways.
    """

    name: str
    diameter_um: float
    d_min_um: float
    d_max_um: float
    excess: ExcessDensity
    carries: tuple[str, ...]
    source: str
    assumptions: tuple[Assumption, ...] = ()
    swollen_from: str | None = None

    @property
    def delta_rho_kg_m3(self) -> float:
        """The nominal excess density. Use `worst_case()` for a verdict, not this."""
        return self.excess.value

    def conservative_diameter_um(self, intent: str) -> float:
        """Worst-case diameter for this intent: smallest if we want it to drop out."""
        return self.d_min_um if intent == "desired" else self.d_max_um

    def worst_case(self, intent: str) -> tuple[float, float, str, str]:
        """(diameter_um, excess_density, diameter_end, density_end) for this intent.

        Both terms move the same way, because v_s goes as drho x d^2: for a LOSS take the
        biggest, densest plausible particle, and for a DESIRED capture the smallest,
        lightest one. Choosing them independently would be how a verdict ends up resting
        on a particle that cannot exist.
        """
        d = self.conservative_diameter_um(intent)
        rho, end = self.excess.for_intent(intent)
        d_end = {"desired": "min", "loss": "max"}.get(intent, "max")
        return d, rho, d_end, end

    def settling_m_s(self, mu_pa_s: float, diameter_um: float | None = None,
                     excess_kg_m3: float | None = None) -> float:
        d = (self.diameter_um if diameter_um is None else diameter_um) * 1e-6
        rho = self.excess.value if excess_kg_m3 is None else excess_kg_m3
        return stokes_settling_m_s(rho, d, mu_pa_s)

    def settling_for_intent(self, mu_pa_s: float, intent: str) -> tuple[float, dict]:
        """Settling velocity for an intent, plus exactly which ends were used."""
        d, rho, d_end, rho_end = self.worst_case(intent)
        v = self.settling_m_s(mu_pa_s, d, rho)
        return v, {"diameter_um": d, "diameter_end": d_end,
                   "excess_density_kg_m3": rho, "density_end": rho_end,
                   "density_key": self.excess.key}

    def as_json(self, mu_pa_s: float) -> dict:
        lo = self.settling_m_s(mu_pa_s, self.d_min_um, self.excess.min_kg_m3)
        hi = self.settling_m_s(mu_pa_s, self.d_max_um, self.excess.max_kg_m3)
        return {
            "name": self.name,
            "diameter_um": self.diameter_um,
            "diameter_range_um": [self.d_min_um, self.d_max_um],
            "excess_density_key": self.excess.key,
            "excess_density_kg_m3": self.excess.value,
            "excess_density_range_kg_m3": [self.excess.min_kg_m3, self.excess.max_kg_m3],
            "excess_density_is_assumption": self.excess.assumption,
            "settling_um_s": round(self.settling_m_s(mu_pa_s) * 1e6, 3),
            # The full span of the two contract bands together: this is the honest
            # uncertainty on every settling verdict below.
            "settling_um_s_range": [round(lo * 1e6, 3), round(hi * 1e6, 3)],
            "settling_span_ratio": round(hi / lo, 2) if lo else None,
            "settling_um_s_if_loss": round(
                self.settling_for_intent(mu_pa_s, "loss")[0] * 1e6, 3),
            "settling_um_s_if_desired": round(
                self.settling_for_intent(mu_pa_s, "desired")[0] * 1e6, 3),
            "carries": list(self.carries),
            "source": self.source,
            "swollen_from": self.swollen_from,
            "assumptions": [a.key for a in self.assumptions],
        }


def _grid_views(mesh: Mesh) -> dict[str, dict]:
    """Normalise a Mesh's chamber-grid metadata into one shape.

    mesh.py records `dx_mm`/`dy_mm` and a centre; the fallback builder here records
    `px`/`py` and an origin. Everything downstream wants origin plus pitch, so the
    difference is absorbed in one place instead of being tested for at each use.
    """
    out: dict[str, dict] = {}
    for fid, g in (mesh.meta.get("grids") or {}).items():
        if g.get("kind") != "grid":
            out[fid] = dict(g)
            continue
        px = g.get("px") or g.get("dx_mm")
        py = g.get("py") or g.get("dy_mm")
        size = list(g.get("size") or [0.0, 0.0])
        center = list(g.get("center") or [0.0, 0.0])
        x0 = g.get("x0", center[0] - size[0] / 2.0)
        y0 = g.get("y0", center[1] - size[1] / 2.0)
        out[fid] = {**g, "px": float(px), "py": float(py), "x0": float(x0),
                    "y0": float(y0), "size": size, "center": center,
                    "nx": int(g["nx"]), "ny": int(g["ny"]), "ids": g["ids"],
                    "depth_mm": float(g["depth_mm"]), "kind": "grid"}
    return out


def _boundary_exits(mesh: Mesh) -> dict[str, tuple[tuple[str, str, Edge], ...]]:
    """Per feature, the non-`hs_face` edges that cross its boundary: (near, far, edge).

    Computed once. A real mesh of module3 has 26,000 edges, so rescanning them inside the
    particle loop turned a millisecond analysis into a minute-long one.
    """
    by_id = {n.id: n for n in mesh.nodes}
    acc: dict[str, list[tuple[str, str, Edge]]] = {}
    for e in mesh.edges:
        if e.kind == "hs_face":
            continue
        fa = by_id[e.a].feature_id if e.a in by_id else None
        fb = by_id[e.b].feature_id if e.b in by_id else None
        if fa == fb:
            continue
        if fa:
            acc.setdefault(fa, []).append((e.a, e.b, e))
        if fb:
            acc.setdefault(fb, []).append((e.b, e.a, e))
    return {k: tuple(v) for k, v in acc.items()}


def _slit_gap_um(edge: Edge) -> float:
    """A slit's gap. model.Edge states it as `gap_um`; the fallback encodes it as depth."""
    gap = getattr(edge, "gap_um", None)
    return float(gap) if gap else float(edge.depth_mm) * 1000.0


def stokes_settling_m_s(delta_rho_kg_m3: float, diameter_m: float, mu_pa_s: float) -> float:
    """Stokes settling velocity, `v_s = drho g d^2 / (18 mu)`, in m/s.

    Valid while the particle Reynolds number stays well under 1, which it does by a wide
    margin here: the fastest thing in this module is a swollen capsule at ~200 um/s, which
    is Re_p ~ 4e-5. `settling_reynolds()` is reported anyway, on the same principle as the
    README's "where Stokes is not enough, say so rather than pretending".
    """
    return delta_rho_kg_m3 * G_M_S2 * diameter_m**2 / (18.0 * mu_pa_s)


def settling_reynolds(v_s_m_s: float, diameter_m: float, mu_pa_s: float,
                      rho_kg_m3: float | None = None) -> float:
    rho = MEDIUM_DENSITY.value if rho_kg_m3 is None else rho_kg_m3
    return rho * v_s_m_s * diameter_m / mu_pa_s


def particle_classes(mu_pa_s: float | None = None) -> dict[str, ParticleClass]:
    """The four classes this pipeline actually carries, from the contracts.

    cells            bare dissociated cells, 10-15 um (blocks.json retention prose)
    capsule          fresh capsule at capsule.diameter_um, min/max from the contract range
    capsule_swollen  the same capsule after culture: x capsule.swell_factor. This is the
                     one that sets channel widths and it is the one the repo forgets.
    bead             clean-up bead; far denser than medium, so it settles in seconds

    Every density is now a contract key (`*.excess_density_kg_m3`), read with its band.
    """
    carrier = carrier_density()
    c_lo, c_hi, cell_note = cell_diameter_range_um()
    d_cap = contracts.capsule_diameter_um()
    cap_lo, cap_hi = contracts.param_range("capsule.diameter_um")
    cap_lo = float(cap_lo if cap_lo is not None else d_cap)
    cap_hi = float(cap_hi if cap_hi is not None else d_cap)
    swell = contracts.capsule_swell_factor()

    cell_rho = excess_density(EXCESS_DENSITY_KEYS["cells"])
    cap_rho = excess_density(EXCESS_DENSITY_KEYS["capsule"])
    bead_rho = excess_density(EXCESS_DENSITY_KEYS["bead"])
    carrier_note = carrier.as_assumption()
    return {
        "cells": ParticleClass(
            name="cells",
            diameter_um=(c_lo + c_hi) / 2.0,
            d_min_um=c_lo,
            d_max_um=c_hi,
            excess=cell_rho,
            carries=("cells", "liquid"),
            source="contracts/blocks.json retention._cells_caveat + "
                   + EXCESS_DENSITY_KEYS["cells"],
            assumptions=(cell_note, cell_rho.as_assumption(), carrier_note),
        ),
        "capsule": ParticleClass(
            name="capsule",
            diameter_um=d_cap,
            d_min_um=cap_lo,
            d_max_um=cap_hi,
            excess=cap_rho,
            carries=("capsules",),
            source="contracts/parameters.json capsule.diameter_um + "
                   + EXCESS_DENSITY_KEYS["capsule"],
            assumptions=(cap_rho.as_assumption(), carrier_note),
        ),
        "capsule_swollen": ParticleClass(
            name="capsule_swollen",
            diameter_um=contracts.swollen_capsule_diameter_um(),
            d_min_um=cap_lo * swell,
            d_max_um=cap_hi * swell,
            excess=cap_rho,
            carries=("capsules",),
            source="contracts/parameters.json capsule.diameter_um x capsule.swell_factor "
                   "+ " + EXCESS_DENSITY_KEYS["capsule"],
            assumptions=(cap_rho.as_assumption(), carrier_note),
            swollen_from="capsule",
        ),
        "bead": ParticleClass(
            name="bead",
            diameter_um=(c_lo + c_hi) / 2.0,
            d_min_um=c_lo,
            d_max_um=c_hi,
            excess=bead_rho,
            carries=("beads", "liquid"),
            source="protocols/retention_audit.md coupon + "
                   + EXCESS_DENSITY_KEYS["bead"],
            assumptions=(bead_rho.as_assumption(), carrier_note),
        ),
    }


# --- intent: is settling the mechanism or the loss? -------------------------

# Blocks whose whole job is to hold a particle still. Settling INTO one of these helps.
CAPTURE_BLOCKS = frozenset({
    "trap_chamber", "trap_channel", "pillar_funnel_trap", "lock_and_shift_trap",
    "merging_trap", "metering_trap", "wells_array",
})

# Blocks whose job needs the particle to STAY SUSPENDED. Settling in one of these is the
# loss the audit found: a mixer that has dropped its cells has mixed buffer, not cells.
MIXER_BLOCKS = frozenset({
    "herringbone_mixer", "asymmetric_serpentine", "round_maze", "square_maze",
    "serpentine_channel", "snake_channel", "spiral_channel", "flow_focusing",
    "flow_focusing_funnel", "gradient_generator",
})

CAPTURE_CHAMBER_TYPES = frozenset({"trap", "exchange", "sump", "settling", "filter"})
SUSPENSION_CHAMBER_TYPES = frozenset({"mixer", "mix"})
# A chamber that is filled, then capped and reacted in place. A particle settling here has
# arrived where it was going and the chamber is sealed afterwards, so it is not lost.
PARKED_CHAMBER_TYPES = frozenset({"split", "reaction", "pcr", "qc_window", "culture",
                                  "organoid", "incubation"})
# Step operations that mean "get the particles OUT of here". During one of these, a
# chamber that normally holds particles on purpose is a chamber they must leave.
HARVEST_OPERATIONS = frozenset({"transfer", "harvest", "release", "collect", "elute",
                                "mix", "load"})


@dataclass(frozen=True)
class Intent:
    """What settling MEANS in one feature. `kind` is the whole point of this module.

    desired  the feature is meant to hold the particle still: a trap, a weir chamber, a
             sedimentation sump. Settling fast enough is a PASS; too slow is the finding.
    parked   the particle settles in the chamber it was being delivered to, and that
             chamber is capped and reacted in place. Neither a win nor a loss - but it
             must not be reported as a loss, which is the mistake in one direction.
    loss     the feature is a conveying path or needs the particle suspended: a feed line,
             a drain, a mixer, a counting window under flow. Settling here is the finding
             - which is the mistake in the other direction, reported as a win.
    """

    kind: str
    why: str
    scope: str


def sediment_intent(feature: dict, role: str = "chamber", destination: bool = False,
                    particle_d_um: float | None = None,
                    operation: str | None = None) -> Intent:
    """Decide what settling means here, from the block id and the retention data.

    The order matters, and it is data before block id, because the chip spec's own
    `retention` / `filter` / `mixer` keys say what the designer meant and a block id is
    only a shape. `role` is 'chamber', 'feed', 'drain', 'slit' or 'duct'.

    A retention feature is NOT automatically a reason to want this particle to stop, and
    getting that backwards is the same class of error as the two the audit found. The
    opening decides: module3's `capsule_prefilter` is a filter, but its 100 um gap is
    there to catch AGGREGATES and to PASS the 70 um capsules, so a capsule settling in it
    is a loss of payload, not a capture. Compare the opening with the particle before
    calling it desired.
    """
    fid = feature.get("id", "?")
    block = feature.get("block") or ""
    ftype = feature.get("type") or ""

    if role == "slit":
        return Intent("desired", f"{fid} is the weir slit itself: holding particles back "
                                 f"is the feature", "edge")

    # 1. The designer put a retention or filter feature here - but it only wants THIS
    #    particle stopped if the opening is smaller than the particle.
    if feature.get("retention") or feature.get("filter"):
        what = "retention" if feature.get("retention") else "filter"
        spec = feature.get(what) or {}
        opening = spec.get("opening_um") or spec.get("gap_um")
        if opening is not None and particle_d_um is not None and float(opening) >= particle_d_um:
            return Intent(
                "loss",
                f"{fid} has a {what} feature, but its {float(opening):.0f} um opening is "
                f"at or above the {particle_d_um:.0f} um particle, so this particle is "
                f"meant to PASS "
                + (f"(purpose: {spec.get('purpose')}) " if spec.get("purpose") else "")
                + f"- settling here loses payload and is NOT the capture the {what} "
                  f"feature exists for",
                "cell",
            )
        return Intent(
            "desired",
            f"{fid} carries an explicit {what} feature in the chip spec "
            f"({json.dumps(spec)})"
            + (f", whose {float(opening):.0f} um opening is below the "
               f"{particle_d_um:.0f} um particle" if opening is not None
               and particle_d_um is not None else "")
            + ", so stopping this particle is its job; settling adds to that retention",
            "cell",
        )

    # 2. A mixer. Mixing a suspension requires the suspension. This is checked BEFORE the
    #    conveying-path rule because a serpentine mixer is meshed as a duct (the README
    #    requires it), so both rules fire - and "its job is to MIX" is the reason that
    #    belongs in the report, not "its job is to deliver".
    if ftype in SUSPENSION_CHAMBER_TYPES or feature.get("mixer") or \
            (block in MIXER_BLOCKS and role != "feed"):
        why = f"{fid} is a mixer"
        if feature.get("mixer"):
            why += f" ({feature['mixer'].get('kind')}, chip spec 'mixer' key)"
        elif block in MIXER_BLOCKS:
            why += f" (block {block})"
        return Intent(
            "loss",
            why + ": mixing needs the particle to stay in the stream, so settling here is "
                  "a loss - this is the failure the audit found in cell_prep's mixer",
            "cell",
        )

    # 3. A conveying path. A particle that drops out of a line never arrives, whatever is
    #    at the far end. This is the Module 4 feed-line case.
    if role in ("feed", "drain", "duct"):
        carries = feature.get("carries")
        kind = {"feed": "feed line", "drain": "drain", "duct": "channel"}[role]
        detail = f" (carries={carries!r})" if carries else ""
        return Intent(
            "loss",
            f"{fid} is a {kind}{detail}: its job is to DELIVER, so a particle that "
            f"settles in it is lost before it arrives - this is the failure the audit "
            f"found in Module 4's feed lines",
            "edge",
        )

    # 4. A trap by block id or chamber type, with no explicit retention data.
    if block in CAPTURE_BLOCKS or ftype in CAPTURE_CHAMBER_TYPES:
        return Intent(
            "desired",
            f"{fid} is a capture feature (type={ftype!r}, block={block!r}) with no "
            f"explicit retention key, so settling is the retention mechanism available",
            "cell",
        )

    # 5. A chamber that is filled and then sealed.
    if ftype in PARKED_CHAMBER_TYPES or block in ("polygon_chamber", "culture_chamber"):
        extra = ""
        if ftype == "culture":
            # Which way round this goes depends on what the STEP is doing, and this is the
            # third place intent can be read backwards. While the cells are being cultured
            # or rinsed, sitting on the floor is the whole point. During a harvest they
            # have just been detached on purpose and have to leave: settling back down
            # there is exactly the loss the chip exists to avoid.
            if operation in HARVEST_OPERATIONS:
                return Intent(
                    "loss",
                    f"{fid} is a culture chamber, but this step's operation is "
                    f"{operation!r}: the cells have just been detached deliberately and "
                    f"must now leave, so settling back onto the floor is a LOSS of the "
                    f"harvest - the opposite of what the same geometry means during "
                    f"culture",
                    "cell",
                )
            extra = (" and these are ADHERENT cells, for which adhesion IS the retention "
                     "(retention_audit.md: 'Adherent culture needs no retention'); this "
                     f"step's operation is {operation!r}, which is not a harvest")
        return Intent(
            "parked",
            f"{fid} is a fill-then-seal chamber (type={ftype!r}, block={block!r}): a "
            f"particle settling here has reached the chamber it was being delivered to "
            f"and the chamber is capped afterwards, so it is neither captured nor lost"
            + extra,
            "cell",
        )

    return Intent(
        "loss",
        f"{fid} (type={ftype!r}, block={block!r}) matches no capture or fill-then-seal "
        f"rule, so settling is treated as a loss - the safe default, and a reason to add "
        f"a rule rather than accept the finding",
        "cell" if role == "chamber" else "edge",
    )


# --- sedimentation per edge / per cell --------------------------------------


@dataclass
class SedimentVerdict:
    feature_id: str
    element_id: str
    element: str  # 'edge' | 'cell'
    particle: str
    intent: str
    intent_why: str
    v_s_um_s: float
    depth_um: float
    fall_time_s: float
    fall_time_half_s: float
    transit_time_s: float
    u_mm_s: float
    settled_fraction: float
    verdict: str
    severity: str
    message: str
    floor_shear_dyn_cm2: float | None = None
    resuspends: bool | None = None
    shear_class: str | None = None
    bound_by: str | None = None
    diameter_um: float | None = None
    diameter_end: str | None = None
    excess_density_kg_m3: float | None = None
    density_end: str | None = None
    density_key: str | None = None

    def as_json(self) -> dict:
        d = dict(self.__dict__)
        for k in ("v_s_um_s", "fall_time_s", "fall_time_half_s", "transit_time_s",
                  "u_mm_s", "settled_fraction", "depth_um"):
            if d[k] is not None:
                d[k] = round(d[k], 4)
        return d


def sediment_check(feature: dict, element_id: str, element: str, particle: ParticleClass,
                   depth_mm: float, transit_time_s: float, u_mm_s: float, mu_pa_s: float,
                   role: str = "chamber", floor_shear_dyn_cm2: float | None = None,
                   destination: bool = False,
                   operation: str | None = None) -> SedimentVerdict:
    """Compare transit time with the time to fall the local depth, and read it per intent.

    The ratio `transit / fall` is the fraction of the depth a particle crosses while it
    crosses the feature, so it doubles as the settled fraction for a stream filling the
    depth uniformly (clipped at 1). What that fraction MEANS is `sediment_intent`'s call:
    0.9 in a trap is the feature working and 0.9 in a feed line is 90% of the payload on
    the floor of a tube.
    """
    # The intent is decided on the NOMINAL diameter (it is a property of the feature and
    # the class, not of which end of the size range we are being careful about), and only
    # then does the intent choose which end of BOTH contract bands is the worst case -
    # diameter and excess density, per the rule in the contract notes.
    intent = sediment_intent(feature, role=role, destination=destination,
                             particle_d_um=particle.diameter_um, operation=operation)
    v_s, used = particle.settling_for_intent(mu_pa_s, intent.kind)
    d_um = used["diameter_um"]
    depth_um = depth_mm * 1000.0
    fall_m = depth_mm * 1e-3 * START_HEIGHT_FRACTION.value
    fall_time = fall_m / v_s if v_s > 0 else math.inf
    fall_half = fall_time / 2.0
    settled = 0.0 if fall_time <= 0 else min(1.0, transit_time_s / fall_time)

    shear_class = shear_class_for(particle, feature)
    ceiling = contracts.shear_ceiling_for(shear_class)
    resuspends = None
    if floor_shear_dyn_cm2 is not None:
        resuspends = floor_shear_dyn_cm2 > RESUSPENSION_SHEAR_DYN_CM2.value

    if intent.kind == "desired":
        ok = settled >= 1.0
        verdict = "captured" if ok else "escapes"
        severity = "low" if ok else "high"
        if ok and resuspends:
            verdict, severity = "captured_but_resuspends", "medium"
        message = (
            f"{particle.name} in {feature.get('id')}: settling is the DESIRED mechanism "
            f"({intent.why}). {_density_note(used)}. "
            f"Falls {depth_um:.0f} um in {fall_time:.1f} s; transit is "
            f"{transit_time_s:.1f} s, so {settled * 100:.0f}% of the depth is cleared"
            + ("." if ok else
               f" - NOT captured, the flow carries it out first. Cap the flow at "
               f"{_cap_flow_hint(transit_time_s, fall_time, u_mm_s)}.")
        )
        if resuspends:
            message += (
                f" Floor shear {floor_shear_dyn_cm2:.4f} dyn/cm2 is above the assumed "
                f"{RESUSPENSION_SHEAR_DYN_CM2.value} resuspension threshold, so settled "
                f"particles may not stay down. Note the ceiling that applies here is "
                f"'{shear_class}', bound by {ceiling.bound_by}"
                + (f" at {ceiling.pressure_ceiling_kpa:.2f} kPa - there is NO shear "
                   f"ceiling for this content"
                   if ceiling.ceiling_dyn_cm2 is None
                   else f" at {ceiling.ceiling_dyn_cm2} dyn/cm2")
                + ", so this is a resuspension note and not a shear finding.")
    elif intent.kind == "parked":
        verdict = "parked"
        severity = "low"
        message = (
            f"{particle.name} in {feature.get('id')}: settles in place, which is neither "
            f"a win nor a loss ({intent.why}). {_density_note(used)}. "
            f"{settled * 100:.0f}% of the depth cleared in a {transit_time_s:.1f} s "
            f"transit. Do NOT report this as a loss."
        )
    else:
        lost = settled
        if lost >= 0.5:
            verdict, severity = "lost", "blocker" if lost >= 0.9 else "high"
        elif lost >= 0.1:
            verdict, severity = "partial_loss", "medium"
        elif lost > 0.01:
            verdict, severity = "partial_loss", "low"
        else:
            verdict, severity = "carried", "low"
        message = (
            f"{particle.name} in {feature.get('id')}: settling is a LOSS here "
            f"({intent.why}). {_density_note(used)}. "
            f"v_s {v_s * 1e6:.1f} um/s falls the {depth_um:.0f} um depth "
            f"in {fall_time:.1f} s while transit takes {transit_time_s:.1f} s, so about "
            f"{lost * 100:.0f}% drops out before it arrives "
            f"({settled and min(1.0, (transit_time_s / fall_half)) * 100:.0f}% on the "
            f"half-depth mean)."
        )
        if verdict != "carried":
            message += (f" Needs u above {depth_mm * 1e-3 / fall_time * 1e3:.3f} mm/s or a "
                        f"shorter path.")
    return SedimentVerdict(
        feature_id=str(feature.get("id")),
        element_id=element_id,
        element=element,
        particle=particle.name,
        intent=intent.kind,
        intent_why=intent.why,
        v_s_um_s=v_s * 1e6,
        depth_um=depth_um,
        fall_time_s=fall_time,
        fall_time_half_s=fall_half,
        transit_time_s=transit_time_s,
        u_mm_s=u_mm_s,
        settled_fraction=settled,
        verdict=verdict,
        severity=severity,
        message=message,
        floor_shear_dyn_cm2=floor_shear_dyn_cm2,
        resuspends=resuspends,
        shear_class=shear_class,
        bound_by=ceiling.bound_by,
        diameter_um=d_um,
        diameter_end=used["diameter_end"],
        excess_density_kg_m3=used["excess_density_kg_m3"],
        density_end=used["density_end"],
        density_key=used["density_key"],
    )


def _density_note(used: dict) -> str:
    """State which end of each contract band a verdict used. The contracts require it."""
    return (f"using the {used['density_end']} of {used['density_key']} "
            f"({used['excess_density_kg_m3']:g} kg/m3) and the "
            f"{used['diameter_end']} diameter ({used['diameter_um']:g} um), the "
            f"conservative end for this intent")


def _cap_flow_hint(transit_s: float, fall_s: float, u_mm_s: float) -> str:
    if transit_s <= 0 or fall_s <= 0 or u_mm_s <= 0:
        return "a lower flow"
    return f"{u_mm_s * transit_s / fall_s:.3f} mm/s or below"


def shear_class_for(particle: ParticleClass, feature: dict) -> str:
    """Which of the THREE ceilings bounds this feature, for `shear_ceiling_for()`.

    Scoping these wrongly has caused real bugs in this repo (the 0.01 culture ceiling was
    copied into six files and mis-scoped in five), so a settled-particle verdict never
    quotes a bare shear number: it quotes the ceiling that actually applies.

    A capsule has NO shear ceiling - capsule handling is bounded by escape pressure, and
    `contracts.shear_ceiling_for('capsules')` returns `ceiling_dyn_cm2=None` with
    `pressure_ceiling_kpa` set, which is exactly the distinction that matters here.
    """
    if particle.name.startswith("capsule"):
        return "capsules"
    if particle.name == "bead":
        return "none"
    if (feature.get("type") or "") == "culture" or \
            (feature.get("block") or "") == "culture_chamber":
        return "adherent_cells"
    text = json.dumps(feature).lower()
    if "lys" in text or "quench" in text:
        return "prelysis_rinse"
    return "none"


# --- clogging ---------------------------------------------------------------


@dataclass
class ClogVerdict:
    feature_id: str
    particle: str
    min_dimension_um: float
    required_um: float
    margin: float
    passes: bool
    severity: str
    message: str
    below_fab_floor: bool = False
    fab_floor_um: float | None = None

    def as_json(self) -> dict:
        d = dict(self.__dict__)
        for k in ("min_dimension_um", "required_um", "margin"):
            d[k] = round(d[k], 3)
        return d


def clog_check(feature: dict, particle: ParticleClass,
               width_mm: float | None = None, depth_mm: float | None = None) -> ClogVerdict:
    """Smallest channel dimension vs `capsule_channel.width_factor_low` x diameter.

    The smallest dimension is `min(width, depth)`, not the width: a 0.6 mm wide, 0.3 mm
    deep duct passes what a 0.3 mm slot passes. The diameter is the LARGEST in the class's
    range, because a clog is caused by the biggest particle in the population and the
    contract's own capsule range runs to 76.5 um - 9% above the nominal 70.
    """
    factor = float(contracts.param("capsule_channel.width_factor_low"))
    w = feature.get("width_mm") if width_mm is None else width_mm
    h = feature.get("depth_mm") if depth_mm is None else depth_mm
    dims = [float(x) * 1000.0 for x in (w, h) if x is not None]
    # A port or a filter states its own passage instead of a width/depth.
    if feature.get("width_um") is not None:
        dims.append(float(feature["width_um"]))
    gap = (feature.get("filter") or {}).get("gap_um") or (feature.get("retention") or {}).get("opening_um")
    if gap is not None and feature.get("filter"):
        dims.append(float(gap))
    min_dim = min(dims) if dims else math.inf
    required = factor * particle.d_max_um
    margin = min_dim / required if required > 0 else math.inf
    passes = min_dim >= required
    # An in-plane dimension below the fabrication floor is not a clog risk, it is an
    # unbuildable feature - a different and more serious thing, so it is reported as such
    # rather than folded into the margin. module3's 100 um prefilter gap is this case.
    fab_floor_um = contracts.floor("min_open_channel_mm") * 1000.0
    below_floor = min_dim < fab_floor_um and math.isfinite(min_dim)
    if passes:
        severity = "low"
    elif min_dim >= particle.d_max_um:
        severity = "high"   # fits, but below the anti-clog rule of thumb
    else:
        severity = "blocker"  # physically smaller than the particle
    message = (
        f"{feature.get('id')}: smallest dimension {min_dim:.0f} um vs "
        f"{factor} x {particle.d_max_um:.0f} um = {required:.0f} um needed for "
        f"{particle.name} ({margin:.2f}x). "
        + ("Clear." if passes else
           ("BELOW the particle itself - it cannot pass at all."
            if min_dim < particle.d_max_um else
            f"Passes a single particle but breaks the "
            f"capsule_channel.width_factor_low anti-clog rule; widen to {required:.0f} um."))
    )
    if below_floor:
        message += (f" Separately, {min_dim:.0f} um is below the "
                    f"{fab_floor_um:.0f} um min_open_channel fabrication floor, so this "
                    f"dimension cannot be printed at all - fix the geometry, not the flow.")
    if required < fab_floor_um and not passes:
        message += (f" (The {required:.0f} um fix is itself below the "
                    f"{fab_floor_um:.0f} um floor, so widening is free here.)")
    return ClogVerdict(str(feature.get("id")), particle.name, min_dim, required, margin,
                       passes, severity, message, below_floor, fab_floor_um)


# --- weir escape and the z-tolerance sweep ----------------------------------


def duct_resistance(mu_pa_s: float, length_m: float, width_m: float,
                    depth_m: float) -> float:
    """`R = 12 mu L / (w h^3 (1 - 0.63 h/w))`, the README's exact rectangular duct.

    `h` is always taken as the SMALLER of the two cross-section dimensions, because the
    0.63 correction is only valid for h <= w. A 25 um weir gap across an 11 mm chamber is
    the extreme case of that and gets it right; swapping them silently would not.
    """
    h, w = sorted((abs(depth_m), abs(width_m)))
    if h <= 0 or w <= 0:
        return math.inf
    return 12.0 * mu_pa_s * length_m / (w * h**3 * (1.0 - 0.63 * h / w))


def slit_pressure_pa(q_m3_s: float, gap_um: float, width_mm: float, mu_pa_s: float,
                     crest_thickness_um: float | None = None) -> float:
    """Pressure across a weir slit at a given gap. Scales as gap^-3, which is the point."""
    if crest_thickness_um is None:
        crest_thickness_um = float(contracts.block("retention.weir_geometry.crest_thickness_um"))
    r = duct_resistance(mu_pa_s, crest_thickness_um * 1e-6, width_mm * 1e-3, gap_um * 1e-6)
    return abs(q_m3_s) * r


@dataclass
class WeirBand:
    feature_id: str
    nominal_gap_um: float
    particle: str
    diameter_um: float
    retention_limit_um: float
    nominal_retention_limit_um: float
    contract_low_um: float
    pressure_limit_kpa: float
    q_ul_min: float
    gap_lo_um: float
    gap_hi_um: float
    z_offset_lo_um: float
    z_offset_hi_um: float
    z_tolerance_um: float
    covered_fraction: float
    nominal_dp_kpa: float
    works_at_nominal: bool
    limited_by: tuple[str, str]
    sweep: list[dict]
    message: str
    assumptions: list[dict] = field(default_factory=list)

    def as_json(self) -> dict:
        d = dict(self.__dict__)
        d["limited_by"] = list(self.limited_by)
        return d


def weir_tolerance_band(chamber: dict, particle: ParticleClass, q_m3_s: float,
                        mu_pa_s: float, width_mm: float | None = None,
                        nominal_gap_um: float | None = None,
                        step_um: float = 0.05) -> WeirBand:
    """Sweep the weir gap over the +/-20 um z tolerance and report where it still works.

    Two failure modes bound the band, and they pull in opposite directions:

    * **too open** - retention fails once the gap reaches `trap.gap_factor_high` x D. The
      contract's rule is strict ("gap must be STRICTLY below ... tools/check_chip_spec.py
      enforces the upper bound with >=, so a gap equal to it fails"), so `>=` fails here
      too. The SMALLEST capsule in the class sets this, because it is the first to slip.
    * **too tight** - slit flow goes as gap^3, so the pressure across the crest rises as
      gap^-3 and eventually passes `capsule.escape_pressure_kpa` x `control.pressure_margin`,
      at which point the capsules are squeezed through the gap they were being held by.

    The contract also states a lower bound of `trap.gap_factor_low` x D for the gap. That
    is an unexplained rule of thumb, not a physical limit, so it is reported as a third
    bound rather than folded into the band - and on these chips it is the one that bites.
    """
    retention = chamber.get("retention") or {}
    if nominal_gap_um is None:
        nominal_gap_um = float(retention.get("opening_um") or
                               contracts.block("retention.weir_geometry.gap_um"))
    if width_mm is None:
        size = chamber.get("size") or [1.0, 1.0, 0.3]
        width_mm = float(size[1])

    # Two limits, and the report needs both. The contract's rule and
    # tools/check_chip_spec.py are written against the NOMINAL capsule.diameter_um, which
    # gives the familiar 0.4 x 70 = 28 um. But the contract's own range runs down to
    # 67 um, and the smallest capsule is the first one to slip the gap, so the honest
    # limit is a little tighter. Quoting only the nominal would overstate the band.
    d_small = particle.d_min_um
    hi_factor = float(contracts.param("trap.gap_factor_high"))
    lo_factor = float(contracts.param("trap.gap_factor_low"))
    retention_limit = hi_factor * d_small
    nominal_retention_limit = hi_factor * particle.diameter_um
    contract_low = lo_factor * d_small
    p_limit_kpa = contracts.max_working_pressure_kpa()
    tol, tol_note = z_tolerance_um()

    sweep: list[dict] = []
    gaps = np.arange(max(step_um, nominal_gap_um - tol), nominal_gap_um + tol + step_um / 2,
                     step_um)
    ok_gaps: list[float] = []
    for gap in gaps:
        dp_kpa = slit_pressure_pa(q_m3_s, float(gap), width_mm, mu_pa_s) / PA_PER_KPA
        retains = gap < retention_limit
        pressure_ok = dp_kpa <= p_limit_kpa
        works = retains and pressure_ok
        if works:
            ok_gaps.append(float(gap))
        sweep.append({
            "gap_um": round(float(gap), 2),
            "z_offset_um": round(float(gap) - nominal_gap_um, 2),
            "dp_kpa": round(dp_kpa, 4),
            "retains": bool(retains),
            "pressure_ok": bool(pressure_ok),
            "works": bool(works),
            "in_contract_band": bool(contract_low <= gap < retention_limit),
        })

    if ok_gaps:
        gap_lo, gap_hi = min(ok_gaps), max(ok_gaps)
    else:
        gap_lo = gap_hi = math.nan
    covered = (len(ok_gaps) / len(gaps)) if len(gaps) else 0.0
    nominal_dp = slit_pressure_pa(q_m3_s, nominal_gap_um, width_mm, mu_pa_s) / PA_PER_KPA
    works_nominal = (nominal_gap_um < retention_limit) and (nominal_dp <= p_limit_kpa)

    lo_reason = ("pressure: gap^-3 drives dP past "
                 f"{p_limit_kpa:.2f} kPa (escape {contracts.escape_pressure_kpa()} kPa x "
                 f"margin {contracts.pressure_margin()})")
    hi_reason = (f"retention: gap >= trap.gap_factor_high {hi_factor} x "
                 f"{d_small:.0f} um = {retention_limit:.1f} um lets the capsule through")
    if not math.isnan(gap_lo) and gap_lo <= gaps[0] + step_um:
        lo_reason = (f"the -{tol:.0f} um end of the z tolerance; pressure would not bite "
                     f"until a much tighter gap")

    q_ul_min = abs(q_m3_s) * UL_PER_M3 * SEC_PER_MIN
    if math.isnan(gap_lo):
        message = (f"{chamber.get('id')}: NO gap inside the +/-{tol:.0f} um tolerance both "
                   f"retains {particle.name} and stays under {p_limit_kpa:.2f} kPa.")
    else:
        message = (
            f"{chamber.get('id')} weir, nominal {nominal_gap_um:.0f} um across "
            f"{width_mm:.2f} mm at {q_ul_min:.2f} uL/min: works for gaps "
            f"{gap_lo:.1f}-{gap_hi:.1f} um, i.e. z offsets {gap_lo - nominal_gap_um:+.1f} "
            f"to {gap_hi - nominal_gap_um:+.1f} um. That is {covered * 100:.0f}% of the "
            f"+/-{tol:.0f} um tolerance window. dP at nominal is {nominal_dp * 1000:.2f} Pa, "
            f"{nominal_dp / p_limit_kpa * 100:.2f}% of the {p_limit_kpa:.2f} kPa ceiling, so "
            f"pressure is NOT the binding constraint - retention is: anything at or above "
            f"{retention_limit:.1f} um passes the smallest ({d_small:.0f} um) capsule in "
            f"the contract's range, and anything at or above "
            f"{nominal_retention_limit:.1f} um passes the nominal "
            f"{particle.diameter_um:.0f} um one, which is the limit the contract rule and "
            f"tools/check_chip_spec.py state. The contract's "
            f"trap.gap_factor_low lower bound ({contract_low:.1f} um) is tighter than the "
            f"pressure bound and would cut the working band to "
            f"{contract_low:.1f}-{retention_limit:.1f} um, "
            f"only {retention_limit - contract_low:.1f} um wide against a "
            f"+/-{tol:.0f} um tolerance."
        )

    return WeirBand(
        feature_id=str(chamber.get("id")),
        nominal_gap_um=nominal_gap_um,
        particle=particle.name,
        diameter_um=d_small,
        retention_limit_um=retention_limit,
        nominal_retention_limit_um=nominal_retention_limit,
        contract_low_um=contract_low,
        pressure_limit_kpa=p_limit_kpa,
        q_ul_min=q_ul_min,
        gap_lo_um=gap_lo,
        gap_hi_um=gap_hi,
        z_offset_lo_um=gap_lo - nominal_gap_um if not math.isnan(gap_lo) else math.nan,
        z_offset_hi_um=gap_hi - nominal_gap_um if not math.isnan(gap_hi) else math.nan,
        z_tolerance_um=tol,
        covered_fraction=covered,
        nominal_dp_kpa=nominal_dp,
        works_at_nominal=works_nominal,
        limited_by=(lo_reason, hi_reason),
        sweep=sweep,
        message=message,
        assumptions=[tol_note.as_json()],
    )


def capsule_size_for_full_tolerance(mu_pa_s: float | None = None) -> dict:
    """What capsule diameter would make the weir survive the whole +/-20 um tolerance.

    The contract's own escape hatch: "If it fails, the lever is capsule size: capsules are
    tunable 35-260 um, and a larger capsule allows a proportionally larger, more printable
    gap." This works out whether that lever is actually long enough, which it is only if
    the unexplained `trap.gap_factor_low` bound is dropped.
    """
    tol, _ = z_tolerance_um()
    hi = float(contracts.param("trap.gap_factor_high"))
    lo = float(contracts.param("trap.gap_factor_low"))
    d_lo, d_hi = contracts.param_range("capsule.diameter_tunable_um")
    d_lo, d_hi = float(d_lo), float(d_hi)

    # Full contract band [lo*D, hi*D) must be at least 2*tol wide to hold +/-tol.
    d_needed_band = 2.0 * tol / (hi - lo)
    # Retention bound only: need nominal + tol < hi*D and nominal - tol > pressure bound.
    # The pressure bound is a couple of um on these chips, so take nominal = tol + 2 um.
    nominal = tol + 2.0
    d_needed_retention = (nominal + tol) / hi
    return {
        "z_tolerance_um": tol,
        "with_contract_band": {
            "diameter_um_needed": round(d_needed_band, 1),
            "tunable_max_um": d_hi,
            "achievable": d_needed_band <= d_hi,
            "why": (
                f"the contract band [{lo} D, {hi} D) is only {hi - lo:.1f} D wide, so it "
                f"holds +/-{tol:.0f} um only at D >= {d_needed_band:.0f} um, beyond the "
                f"{d_hi:.0f} um tunable maximum. Even at D = {d_hi:.0f} um the band is "
                f"{(hi - lo) * d_hi:.0f} um wide, needing +/-{(hi - lo) * d_hi / 2:.0f} um."
            ),
        },
        "retention_bound_only": {
            "diameter_um_needed": round(d_needed_retention, 1),
            "nominal_gap_um": nominal,
            "achievable": d_lo <= d_needed_retention <= d_hi,
            "why": (
                f"dropping the unexplained trap.gap_factor_low bound leaves only "
                f"'gap < {hi} D'. A {nominal:.0f} um nominal gap survives +{tol:.0f} um at "
                f"D >= {d_needed_retention:.0f} um, which IS inside the tunable "
                f"{d_lo:.0f}-{d_hi:.0f} um range. This is the cheap fix, and it needs the "
                f"0.3 D bound justified or dropped."
            ),
        },
    }


# --- provisional mesh and solve ---------------------------------------------
# TODO(cfd): everything from here to `_solve_provisional` belongs to mesh.py and solve.py
# and must be DELETED when they land - at which point `analyse_chip` takes a Mesh and a
# Field instead of building them. It exists so the particle physics above can be verified
# against the three real chips today instead of waiting. It follows the README's
# non-negotiable meshing rules (channel-like blocks are ducts at their lane width; a weir
# chamber gets a slit edge across its full width) but it is NOT a substitute for mesh.py:
# no path following beyond straight segments, no Re, no shear on hs_faces.


def _is_duct_like(chamber: dict) -> bool:
    """README: any `*_channel` block is a DUCT at its real lane width and path length."""
    block = str(chamber.get("block") or "")
    return block.endswith("_channel") or block in MIXER_BLOCKS - {"flow_focusing",
                                                                  "flow_focusing_funnel",
                                                                  "gradient_generator"}


def _path_length_mm(path: Sequence[Sequence[float]] | None,
                    fallback: tuple[tuple[float, float], tuple[float, float]]) -> float:
    pts = list(path) if path else [list(fallback[0]), list(fallback[1])]
    total = 0.0
    for p, q in zip(pts, pts[1:]):
        total += math.dist(p[:2], q[:2])
    return total or math.dist(fallback[0], fallback[1])


def _retention_side(chamber: dict, channels: Sequence[dict]) -> tuple[str | None, str]:
    """Which chamber edge the weir sits on, derived from the chip's own channel data.

    The chip spec does not state the side as a field (module3 states it only in a note),
    so it is inferred: the side where the majority of NON capsule-passable channels meet
    the chamber is the retention side, because those are exactly the lines that must have
    liquid without capsules. On module3 this returns 'right', which is what the chamber
    note says in prose.
    """
    size = chamber.get("size") or [0, 0, 0]
    cx, cy = (chamber.get("center") or [0, 0, 0])[:2]
    x0, x1 = cx - size[0] / 2, cx + size[0] / 2
    y0, y1 = cy - size[1] / 2, cy + size[1] / 2
    tally: dict[str, list[str]] = {}
    for ch in channels:
        if ch.get("capsule_passable", True):
            continue
        if chamber["id"] not in (ch.get("from"), ch.get("to")):
            continue
        pt = _chamber_contact(ch, chamber)
        if pt is None:
            continue
        dists = {
            "left": abs(pt[0] - x0), "right": abs(pt[0] - x1),
            "bottom": abs(pt[1] - y0), "top": abs(pt[1] - y1),
        }
        tally.setdefault(min(dists, key=dists.get), []).append(ch["id"])
    if not tally:
        return None, "no non-capsule-passable channel touches this chamber"
    side = max(tally, key=lambda s: len(tally[s]))
    why = ("retention side inferred as " + side + ": non-capsule-passable channels meet "
           "the chamber at " + ", ".join(f"{s} ({', '.join(ids)})"
                                         for s, ids in sorted(tally.items())))
    return side, why


def _chamber_contact(channel: dict, chamber: dict) -> tuple[float, float] | None:
    path = channel.get("path")
    if not path:
        return None
    return tuple(path[-1][:2]) if channel.get("to") == chamber["id"] else tuple(path[0][:2])


def _on_side(pt: tuple[float, float], chamber: dict, side: str, tol_mm: float = 0.3) -> bool:
    size = chamber.get("size") or [0, 0, 0]
    cx, cy = (chamber.get("center") or [0, 0, 0])[:2]
    bounds = {
        "left": (0, cx - size[0] / 2), "right": (0, cx + size[0] / 2),
        "bottom": (1, cy - size[1] / 2), "top": (1, cy + size[1] / 2),
    }
    axis, value = bounds[side]
    return abs(pt[axis] - value) <= tol_mm


def _feature_pos(feature_id: str, ports: dict[str, str], grids: dict[str, dict],
                 by_id: dict[str, Node]) -> tuple[float, float] | None:
    """Centre position of a port or chamber, used to aim a path-less channel's mouths."""
    if feature_id in ports:
        nd = by_id[ports[feature_id]]
        return (nd.x_mm, nd.y_mm)
    g = grids.get(feature_id)
    if not g:
        return None
    if g["kind"] == "duct":
        nd = by_id[g["nodes"][0]]
        return (nd.x_mm, nd.y_mm)
    return tuple(g["center"])


def _port_capsule_passable(chip: dict, channel: dict) -> bool | None:
    """A channel inherits `capsule_passable` from the port it terminates at.

    module4's vent channels say nothing, but the `p_vent` port they all run to is
    explicitly `capsule_passable: false` - it is a capillary stop that passes air and
    halts liquid. Ignoring that produced eight findings about capsules settling in vents
    they can never enter, which crowd out the one real finding on that chip.
    """
    if channel.get("capsule_passable") is not None:
        return channel["capsule_passable"]
    ports = {p["id"]: p for p in chip.get("ports", [])}
    for end in (channel.get("to"), channel.get("from")):
        if end in ports and ports[end].get("capsule_passable") is False:
            return False
    return None


def _build_provisional_mesh(chip: dict, mu_pa_s: float, cells_per_side: int = 7) -> Mesh:
    nodes: list[Node] = []
    edges: list[Edge] = []
    ports: dict[str, str] = {}
    grids: dict[str, dict] = {}
    channels = chip.get("channels", [])

    for p in chip.get("ports", []):
        nid = f"port:{p['id']}"
        pos = p.get("position") or [0, 0, 0]
        nodes.append(Node(nid, "port", float(pos[0]), float(pos[1]), 0.0,
                          float(chip.get("layers", [{}]) and 0.3), p["id"]))
        ports[p["id"]] = nid

    for ch in chip.get("chambers", []):
        size = ch.get("size") or [1.0, 1.0, 0.3]
        cx, cy = (ch.get("center") or [0, 0, 0])[:2]
        depth = float(size[2]) if len(size) > 2 else 0.3
        if _is_duct_like(ch):
            # README: a serpentine is a DUCT at its lane width and path length, not a 2D
            # chamber. Meshing it as a chamber is the 43x shear error the audit found.
            mix = ch.get("mixer") or {}
            lane_w = float(mix.get("channel_width_mm") or 0.2)
            lane_l = float(mix.get("length_mm") or max(size[0], size[1]))
            a = Node(f"cell:{ch['id']}:in", "cell", cx - size[0] / 2, cy, 0.0, depth, ch["id"])
            b = Node(f"cell:{ch['id']}:out", "cell", cx + size[0] / 2, cy, 0.0, depth, ch["id"])
            nodes += [a, b]
            r = duct_resistance(mu_pa_s, lane_l * 1e-3, lane_w * 1e-3, depth * 1e-3)
            edges.append(Edge(f"duct:{ch['id']}", a.id, b.id, "duct", 1.0 / r,
                              lane_l, lane_w, depth, ch["id"]))
            grids[ch["id"]] = {"kind": "duct", "nodes": [a.id, b.id], "depth_mm": depth,
                               "width_mm": lane_w, "length_mm": lane_l}
            continue

        nx = ny = max(3, cells_per_side)
        px, py = size[0] / nx, size[1] / ny
        ids = [[f"cell:{ch['id']}:{i}:{j}" for j in range(ny)] for i in range(nx)]
        for i in range(nx):
            for j in range(ny):
                nodes.append(Node(ids[i][j], "cell",
                                  cx - size[0] / 2 + (i + 0.5) * px,
                                  cy - size[1] / 2 + (j + 0.5) * py,
                                  px * py, depth, ch["id"]))
        for i in range(nx):
            for j in range(ny):
                if i + 1 < nx:
                    r = duct_resistance(mu_pa_s, px * 1e-3, py * 1e-3, depth * 1e-3)
                    edges.append(Edge(f"hs:{ch['id']}:x:{i}:{j}", ids[i][j], ids[i + 1][j],
                                      "hs_face", 1.0 / r, px, py, depth, ch["id"]))
                if j + 1 < ny:
                    r = duct_resistance(mu_pa_s, py * 1e-3, px * 1e-3, depth * 1e-3)
                    edges.append(Edge(f"hs:{ch['id']}:y:{i}:{j}", ids[i][j], ids[i][j + 1],
                                      "hs_face", 1.0 / r, py, px, depth, ch["id"]))
        grids[ch["id"]] = {"kind": "grid", "ids": ids, "nx": nx, "ny": ny,
                           "px": px, "py": py, "depth_mm": depth,
                           "x0": cx - size[0] / 2, "y0": cy - size[1] / 2,
                           "size": size, "center": [cx, cy]}

    by_id = {n.id: n for n in nodes}

    def _attach(feature_id: str, pt: tuple[float, float] | None,
                toward: tuple[float, float] | None = None,
                entering: bool | None = None) -> str:
        """Pick the node a channel meets this feature at.

        `pt` is the stated contact point when the channel has a `path`. Many chips (all of
        cell_prep's channels) have no path, and then BOTH ends would land on the same
        default node - which silently short-circuits the feature: cell_prep's serpentine
        mixer was bypassed entirely, so its residence time and its cell loss both vanished.
        So with no path we use `entering` (does the channel flow into this feature?) for a
        duct, and the direction of the other endpoint for a chamber.
        """
        if feature_id in ports:
            return ports[feature_id]
        g = grids.get(feature_id)
        if g is None:
            raise KeyError(f"channel endpoint {feature_id!r} is neither a port nor a chamber")
        if g["kind"] == "duct":
            if pt is None:
                # Into the duct -> its inlet; out of the duct -> its outlet. Never both
                # on the same node, or the duct carries no flow.
                return g["nodes"][0] if entering else g["nodes"][1]
            a, b = by_id[g["nodes"][0]], by_id[g["nodes"][1]]
            return g["nodes"][0] if math.dist(pt, (a.x_mm, a.y_mm)) <= \
                math.dist(pt, (b.x_mm, b.y_mm)) else g["nodes"][1]
        probe = pt or toward
        if probe is None:
            return g["ids"][g["nx"] // 2][g["ny"] // 2]
        # Nearest cell to the probe, clamped into the chamber. With `toward` this puts the
        # mouth on the side the channel actually comes from, so the flow crosses the
        # chamber instead of entering and leaving at its centre.
        i = min(g["nx"] - 1, max(0, int((probe[0] - g["x0"]) / g["px"])))
        j = min(g["ny"] - 1, max(0, int((probe[1] - g["y0"]) / g["py"])))
        return g["ids"][i][j]

    weir_sides = {}
    for ch in chip.get("chambers", []):
        if ch.get("retention"):
            weir_sides[ch["id"]] = _retention_side(ch, channels)

    crest_um = float(contracts.block("retention.weir_geometry.crest_thickness_um"))

    for c in channels:
        depth = float(c.get("depth_mm") or 0.3)
        width = float(c.get("width_mm") or 0.2)
        a_feat, b_feat = c["from"], c["to"]
        path = c.get("path")
        pa = tuple(path[0][:2]) if path else None
        pb = tuple(path[-1][:2]) if path else None
        # With no path, aim each end at the other end's position so the mouths land on
        # opposite sides of a chamber rather than both at its centre.
        pos_a = _feature_pos(a_feat, ports, grids, by_id)
        pos_b = _feature_pos(b_feat, ports, grids, by_id)
        a = _attach(a_feat, pa, toward=pos_b, entering=False)
        b = _attach(b_feat, pb, toward=pos_a, entering=True)
        length = _path_length_mm(path, ((by_id[a].x_mm, by_id[a].y_mm),
                                       (by_id[b].x_mm, by_id[b].y_mm)))
        r_duct = duct_resistance(mu_pa_s, length * 1e-3, width * 1e-3, depth * 1e-3)

        # Does this channel cross a weir? Only if it is not capsule-passable AND it meets
        # the chamber on the inferred retention side.
        slit_for = None
        for feat, pt in ((a_feat, pa), (b_feat, pb)):
            side, _ = weir_sides.get(feat, (None, ""))
            chamber = next((x for x in chip["chambers"] if x["id"] == feat), None)
            if side and chamber and not c.get("capsule_passable", True) and pt is not None \
                    and _on_side(pt, chamber, side):
                slit_for = (feat, chamber, pt)
        if slit_for is None:
            edges.append(Edge(f"duct:{c['id']}", a, b, "duct", 1.0 / r_duct,
                              length, width, depth, c["id"]))
            continue

        feat, chamber, pt = slit_for
        gap_um = float((chamber["retention"] or {}).get("opening_um"))
        slit_w = float((chamber.get("size") or [1, 1, 0.3])[1])
        jn = Node(f"weir:{c['id']}", "junction", pt[0], pt[1], 0.0, gap_um / 1000.0, feat)
        nodes.append(jn)
        by_id[jn.id] = jn
        r_slit = duct_resistance(mu_pa_s, crest_um * 1e-6, slit_w * 1e-3, gap_um * 1e-6)
        inner = a if by_id[a].feature_id == feat else b
        outer = b if inner == a else a
        edges.append(Edge(f"slit:{c['id']}", inner, jn.id, "slit", 1.0 / r_slit,
                          crest_um / 1000.0, slit_w, gap_um / 1000.0, feat))
        edges.append(Edge(f"duct:{c['id']}", jn.id, outer, "duct", 1.0 / r_duct,
                          length, width, depth, c["id"]))

    return Mesh(nodes, edges, ports, {
        "chip_id": chip.get("id"),
        "mu_pa_s": mu_pa_s,
        "grid_mm": None,
        "grids": grids,
        "weir_sides": {k: v[1] for k, v in weir_sides.items()},
        "provisional": True,
        "provisional_why": "mesh.py not yet present; see TODO(cfd) in particles.py",
    })


def _solve_provisional(mesh: Mesh, feeds: dict[str, float], vents: Sequence[str]) -> Field:
    """Minimal `A p = b` conductance solve, enough to get a velocity field for tracking."""
    ids = [n.id for n in mesh.nodes]
    idx = {nid: k for k, nid in enumerate(ids)}
    n = len(ids)
    A = np.zeros((n, n))
    b = np.zeros(n)
    for e in mesh.edges:
        if not math.isfinite(e.conductance_m3_per_pa_s):
            continue
        i, j = idx[e.a], idx[e.b]
        c = e.conductance_m3_per_pa_s
        A[i, i] += c
        A[j, j] += c
        A[i, j] -= c
        A[j, i] -= c
    for port_id, q in feeds.items():
        b[idx[mesh.ports[port_id]]] += q
    for port_id in vents:
        k = idx[mesh.ports[port_id]]
        A[k, :] = 0.0
        A[k, k] = 1.0
        b[k] = 0.0
    # Drop nodes that no finite edge reaches (a fully closed weir isolates its junction).
    live = np.abs(A).sum(axis=1) > 0
    p = np.zeros(n)
    p[live] = np.linalg.solve(A[np.ix_(live, live)], b[live])

    fld = Field()
    fld.p_pa = {nid: float(p[idx[nid]]) for nid in ids}
    for e in mesh.edges:
        c = e.conductance_m3_per_pa_s
        fld.q_m3_s[e.id] = (0.0 if not math.isfinite(c)
                            else float(c * (p[idx[e.a]] - p[idx[e.b]])))
        area = e.width_mm * e.depth_mm * 1e-6
        if area > 0 and e.kind in ("duct", "slit"):
            v = abs(fld.q_m3_s[e.id]) / area
            fld.shear_dyn_cm2[e.id] = (
                6.0 * mesh.meta["mu_pa_s"] * abs(fld.q_m3_s[e.id])
                / (e.width_mm * 1e-3 * (e.depth_mm * 1e-3) ** 2) * DYN_CM2_PER_PA)
            fld.re[e.id] = MEDIUM_DENSITY.value * v * (e.depth_mm * 1e-3) / mesh.meta["mu_pa_s"]

    # Cell velocities from the hs_face flows around each cell.
    acc: dict[str, list[float]] = {}
    for e in mesh.edges:
        if e.kind != "hs_face":
            continue
        a, bb = mesh.node(e.a), mesh.node(e.b)
        area = e.width_mm * e.depth_mm * 1e-6
        if area <= 0:
            continue
        v = fld.q_m3_s[e.id] / area * 1e3  # mm/s, positive a->b
        dx, dy = bb.x_mm - a.x_mm, bb.y_mm - a.y_mm
        norm = math.hypot(dx, dy) or 1.0
        ux, uy = v * dx / norm, v * dy / norm
        for nid in (e.a, e.b):
            acc.setdefault(nid, [0.0, 0.0, 0.0, 0.0])
            slot = 0 if abs(dx) > abs(dy) else 2
            acc[nid][slot] += ux if slot == 0 else uy
            acc[nid][slot + 1] += 1
    for nid, (sx, cx, sy, cy) in acc.items():
        fld.u_mm_s[nid] = (sx / cx if cx else 0.0, sy / cy if cy else 0.0)
    for e in mesh.edges:
        if e.kind in ("duct", "slit"):
            area = e.width_mm * e.depth_mm * 1e-6
            if area > 0:
                fld.u_mm_s.setdefault(e.id, (abs(fld.q_m3_s[e.id]) / area * 1e3, 0.0))

    q_in = sum(max(0.0, q) for q in feeds.values())
    q_out = sum(abs(fld.q_m3_s[e.id]) for e in mesh.edges
                if mesh.node(e.a).kind == "port" and mesh.node(e.a).feature_id in vents
                or mesh.node(e.b).kind == "port" and mesh.node(e.b).feature_id in vents)
    imbalance = 0.0
    for nd in mesh.nodes:
        if nd.kind == "port":
            continue
        net = 0.0
        for e in mesh.edges:
            if e.a == nd.id:
                net -= fld.q_m3_s[e.id]
            elif e.b == nd.id:
                net += fld.q_m3_s[e.id]
        imbalance = max(imbalance, abs(net))
    fld.residual = {"mass_imbalance_max": imbalance, "q_in": q_in, "q_out": q_out}
    return fld


# --- tracking ---------------------------------------------------------------


@dataclass
class TrackResult:
    particle: str
    launched: int
    outlets: dict[str, int]
    settled_desired: dict[str, int]
    settled_parked: dict[str, int]
    lost: dict[str, int]
    stalled: int
    mean_transit_s: float
    message: str
    seeded: str = ""
    density_ends: tuple[str, ...] = ()

    def fractions(self) -> dict[str, float]:
        n = max(1, self.launched)
        return {
            "reached_outlet": sum(self.outlets.values()) / n,
            "settled_as_intended": sum(self.settled_desired.values()) / n,
            "parked": sum(self.settled_parked.values()) / n,
            "lost": sum(self.lost.values()) / n,
            "stalled": self.stalled / n,
        }

    def as_json(self) -> dict:
        d = dict(self.__dict__)
        d["fractions"] = {k: round(v, 4) for k, v in self.fractions().items()}
        d["mean_transit_s"] = round(self.mean_transit_s, 2)
        return d


def _u_at(mesh: Mesh, fld: Field, grid: dict, x: float, y: float) -> tuple[float, float]:
    """Bilinear velocity at (x, y) in a chamber grid, in mm/s."""
    fi = (x - grid["x0"]) / grid["px"] - 0.5
    fj = (y - grid["y0"]) / grid["py"] - 0.5
    i0 = min(grid["nx"] - 1, max(0, int(math.floor(fi))))
    j0 = min(grid["ny"] - 1, max(0, int(math.floor(fj))))
    i1, j1 = min(grid["nx"] - 1, i0 + 1), min(grid["ny"] - 1, j0 + 1)
    tx, ty = min(1.0, max(0.0, fi - i0)), min(1.0, max(0.0, fj - j0))
    ux = uy = 0.0
    for (i, j, w) in ((i0, j0, (1 - tx) * (1 - ty)), (i1, j0, tx * (1 - ty)),
                      (i0, j1, (1 - tx) * ty), (i1, j1, tx * ty)):
        u = fld.u_mm_s.get(grid["ids"][i][j], (0.0, 0.0))
        ux += w * u[0]
        uy += w * u[1]
    return ux, uy


def _seed_nodes(chip: dict, mesh: Mesh, particle: ParticleClass, feeds: Sequence[str],
                source: dict | None) -> tuple[list[str], str]:
    """Where the particles of this class actually ARE at the start of this step.

    Launching every class from every feed port is wrong and flattering: module3's release
    step feeds `backflush_in`, a buffer line that never sees a capsule, so seeding
    capsules there reports a 100% loss in a line that by design carries only liquid. The
    capsules are already sitting in the trap. `_particle_sources` works out which it is.
    """
    source = source or {"ports": [], "chambers": [], "why": []}
    fed = [f for f in feeds if f in (source.get("ports") or []) and f in mesh.ports]
    if fed:
        return ([mesh.ports[f] for f in fed],
                f"seeded at feed port(s) {', '.join(fed)}: " +
                "; ".join(source.get("why") or []))

    views = _grid_views(mesh)
    for cid in (source.get("chambers") or []):
        grid = views.get(cid, {})
        if grid.get("kind") == "grid":
            ids = [grid["ids"][i][j] for i in range(grid["nx"]) for j in range(grid["ny"])]
            return (ids, f"this step's feed {list(feeds)} carries no {particle.name}, so "
                         f"the particles present are the resident ones: seeded across "
                         f"{cid}. " + "; ".join(source.get("why") or []))
        if grid.get("kind") == "duct":
            return (list(grid["nodes"]),
                    f"resident in {cid} (meshed as a duct)")
    return ([], f"no source for {particle.name} in this step: nothing to track")


def track(chip: dict, mesh: Mesh, fld: Field, particle: ParticleClass, mu_pa_s: float,
          feeds: Sequence[str], n_particles: int = 60, max_time_s: float = 1e5,
          rk4_steps: int = 4000, source: dict | None = None,
          operation: str | None = None, grids: dict | None = None,
          boundary: dict | None = None) -> TrackResult:
    """Trace particles from each feed through the field with settling.

    In a duct the path is one-dimensional, so the transit is analytic and settling is a
    depletion along it. In a chamber the in-plane trajectory is integrated with RK4 on the
    reconstructed cell velocities. Gravity is NORMAL to the plane, so it never appears in
    the RK4 right-hand side: it is a separate fall budget, and the particle leaves the
    stream when the cumulative fall reaches the local depth.
    """
    grids = grids if grids is not None else _grid_views(mesh)
    boundary = boundary if boundary is not None else _boundary_exits(mesh)
    chambers = {c["id"]: c for c in chip.get("chambers", [])}
    channels = {c["id"]: c for c in chip.get("channels", [])}
    density_ends: set[str] = set()
    outlets: dict[str, int] = {}
    settled_desired: dict[str, int] = {}
    settled_parked: dict[str, int] = {}
    lost: dict[str, int] = {}
    stalled = 0
    transits: list[float] = []

    out_edges: dict[str, list[Edge]] = {}
    for e in mesh.edges:
        out_edges.setdefault(e.a, []).append(e)
        out_edges.setdefault(e.b, []).append(e)

    def _flow_out(nid: str) -> list[tuple[Edge, str, float]]:
        out = []
        for e in out_edges.get(nid, []):
            q = fld.q_m3_s.get(e.id, 0.0)
            if e.a == nid and q > 0:
                out.append((e, e.b, q))
            elif e.b == nid and q < 0:
                out.append((e, e.a, -q))
        return out

    seeds, seed_why = _seed_nodes(chip, mesh, particle, feeds, source)
    if not seeds:
        return TrackResult(particle.name, 0, {}, {}, {}, {}, 0, 0.0,
                           f"{particle.name}: not present in this step.", seed_why)
    per_feed = max(1, n_particles // max(1, len(seeds)))
    for start in seeds:
        for k in range(per_feed):
            nid = start
            t = 0.0
            fallen_um = 0.0
            # Stagger the launch height so the cohort spans the depth, which is what makes
            # a partial loss a fraction rather than an all-or-nothing verdict.
            budget_frac = (k + 0.5) / per_feed
            where = None
            outcome = None
            hops = 0
            while outcome is None and hops < 400 and t < max_time_s:
                hops += 1
                opts = _flow_out(nid)
                if not opts:
                    outcome, where = "stalled", mesh.node(nid).feature_id
                    break
                # Follow the largest outflow; the cohort is split by launch height, not by
                # random branching, so this stays deterministic and reproducible.
                opts.sort(key=lambda o: -o[2])
                e, nxt, q = opts[0] if per_feed == 1 else opts[min(k % len(opts),
                                                                   len(opts) - 1)]
                node_a, node_b = mesh.node(e.a), mesh.node(e.b)

                if e.kind == "slit":
                    # Size exclusion, not settling: this is what the weir is for.
                    gap_um = _slit_gap_um(e)
                    if particle.d_min_um > gap_um:
                        outcome, where = "settled_desired", str(e.feature_id)
                        break
                    # Small enough to pass the gap - so the weir is not retaining it.
                    nid = nxt
                    continue

                if e.kind in ("duct", "mouth"):
                    area = e.width_mm * e.depth_mm * 1e-6
                    v = abs(q) / area if area > 0 else 0.0
                    dt = (e.length_mm * 1e-3) / v if v > 0 else math.inf
                    depth_um = e.depth_mm * 1000.0
                    feature = channels.get(str(e.feature_id)) or \
                        chambers.get(str(e.feature_id)) or {"id": e.feature_id}
                    if any(ch["id"] == e.feature_id
                           for ch in chip.get("chambers", [])):
                        role = "chamber"
                    else:
                        role = ("feed" if node_a.kind == "port" else
                                "drain" if node_b.kind == "port" else "duct")
                    intent = sediment_intent(feature, role=role,
                                             particle_d_um=particle.diameter_um,
                                             operation=operation)
                    v_s, used = particle.settling_for_intent(mu_pa_s, intent.kind)
                    density_ends.add(used["density_end"])
                    fall_um = v_s * dt * 1e6 if math.isfinite(dt) else math.inf
                    if fallen_um + fall_um >= depth_um * budget_frac:
                        t += dt * max(0.0, (depth_um * budget_frac - fallen_um)) / max(
                            fall_um, 1e-12)
                        outcome = {"desired": "settled_desired", "parked": "parked"}.get(
                            intent.kind, "lost")
                        where = str(e.feature_id)
                        break
                    fallen_um += fall_um
                    t += dt
                    nid = nxt
                    continue

                # hs_face: we are inside a chamber. Integrate it with RK4 in one go.
                feat = str(node_a.feature_id)
                grid = grids.get(feat)
                chamber = chambers.get(feat, {"id": feat})
                intent = sediment_intent(chamber, role="chamber",
                                         particle_d_um=particle.diameter_um,
                                         operation=operation)
                v_s, used = particle.settling_for_intent(mu_pa_s, intent.kind)
                density_ends.add(used["density_end"])
                depth_um = grid["depth_mm"] * 1000.0
                x, y = node_a.x_mm, node_a.y_mm
                exit_nodes = {}
                for near, far, ee in boundary.get(feat, ()):  # precomputed once
                    q_e = fld.q_m3_s.get(ee.id, 0.0)
                    if (q_e > 0 and ee.a == near) or (q_e < 0 and ee.b == near):
                        exit_nodes[near] = (ee, far)
                speed = max(1e-9, math.hypot(*_u_at(mesh, fld, grid, x, y)))
                dt = min(grid["px"], grid["py"]) / (speed * 4.0)
                reached = None
                for _ in range(rk4_steps):
                    def f(px: float, py: float) -> tuple[float, float]:
                        return _u_at(mesh, fld, grid, px, py)
                    k1 = f(x, y)
                    k2 = f(x + dt * k1[0] / 2, y + dt * k1[1] / 2)
                    k3 = f(x + dt * k2[0] / 2, y + dt * k2[1] / 2)
                    k4 = f(x + dt * k3[0], y + dt * k3[1])
                    nx_ = x + dt / 6 * (k1[0] + 2 * k2[0] + 2 * k3[0] + k4[0])
                    ny_ = y + dt / 6 * (k1[1] + 2 * k2[1] + 2 * k3[1] + k4[1])
                    step_fall = v_s * dt * 1e6
                    if fallen_um + step_fall >= depth_um * budget_frac:
                        outcome = {"desired": "settled_desired",
                                   "parked": "parked"}.get(intent.kind, "lost")
                        where = feat
                        t += dt
                        break
                    fallen_um += step_fall
                    t += dt
                    x, y = nx_, ny_
                    for near, (ee, far) in exit_nodes.items():
                        nn = mesh.node(near)
                        if math.dist((x, y), (nn.x_mm, nn.y_mm)) <= \
                                0.75 * max(grid["px"], grid["py"]):
                            reached = near
                            break
                    if reached:
                        break
                    if math.hypot(*f(x, y)) < 1e-9:
                        break
                if outcome:
                    break
                if reached:
                    nid = reached
                    continue
                # Never found an exit: a low-velocity zone, which the README says is what
                # a trap IS in Hele-Shaw flow. Charge it to the chamber's intent.
                outcome = {"desired": "settled_desired", "parked": "parked"}.get(
                    intent.kind, "lost")
                where = feat
                break

            if outcome is None:
                nd = mesh.node(nid)
                if nd.kind == "port":
                    outlets[str(nd.feature_id)] = outlets.get(str(nd.feature_id), 0) + 1
                    transits.append(t)
                else:
                    stalled += 1
                continue
            if outcome == "stalled":
                nd = mesh.node(nid)
                if nd.kind == "port" and nd.feature_id not in feeds:
                    outlets[str(nd.feature_id)] = outlets.get(str(nd.feature_id), 0) + 1
                    transits.append(t)
                else:
                    stalled += 1
            elif outcome == "settled_desired":
                settled_desired[str(where)] = settled_desired.get(str(where), 0) + 1
            elif outcome == "parked":
                settled_parked[str(where)] = settled_parked.get(str(where), 0) + 1
            else:
                lost[str(where)] = lost.get(str(where), 0) + 1

    launched = per_feed * len(seeds)
    res = TrackResult(particle.name, launched, outlets, settled_desired, settled_parked,
                      lost, stalled, sum(transits) / len(transits) if transits else 0.0,
                      "", seed_why, tuple(sorted(density_ends)))
    fr = res.fractions()
    res.message = (
        f"{particle.name}: {fr['reached_outlet'] * 100:.0f}% reach an outlet "
        f"({', '.join(f'{k} {v}' for k, v in sorted(outlets.items())) or 'none'}), "
        f"{fr['settled_as_intended'] * 100:.0f}% settle where intended "
        f"({', '.join(sorted(settled_desired)) or '-'}), "
        f"{fr['parked'] * 100:.0f}% park in a fill-then-seal chamber "
        f"({', '.join(sorted(settled_parked)) or '-'}), "
        f"{fr['lost'] * 100:.0f}% are LOST "
        f"({', '.join(f'{k} {v}' for k, v in sorted(lost.items())) or '-'}). "
        f"Excess density taken at the {'/'.join(sorted(density_ends)) or 'nominal'} end "
        f"of {particle.excess.key}, per feature intent."
    )
    return res


# --- chip-level analysis ----------------------------------------------------


def _mesh_for(chip: dict, mu_pa_s: float) -> tuple[Mesh, str]:
    """A Mesh for this chip: mesh.py's when it is importable, the local one otherwise."""
    if _mesh_mod is not None and hasattr(_mesh_mod, "build_mesh"):
        try:
            return _mesh_mod.build_mesh(chip, mu_pa_s=mu_pa_s), "assay2chip/cfd/mesh.py"
        except Exception as exc:  # noqa: BLE001  # pragma: no cover
            return (_build_provisional_mesh(chip, mu_pa_s),
                    f"provisional (mesh.py raised {type(exc).__name__}: {exc})")
    return _build_provisional_mesh(chip, mu_pa_s), "provisional (mesh.py not importable)"


def _solve_for(mesh: Mesh, feeds: Sequence[str], vents: Sequence[str], q_ul_min: float,
               mu_pa_s: float, step: dict) -> Field:
    """A Field for one step: solve.py's solver when available, the local one otherwise.

    The scenario is built here rather than taken from scenario.py's protocol walk because
    this module needs a field for the step's OWN feed/vent set, and scenario.py's records
    carry inference and worst-case variants that are its business, not ours. The port
    states use model.PortState so solve.py sees exactly what it expects.
    """
    if _solve_mod is not None and _model is not None and hasattr(_solve_mod, "solve_step"):
        states: dict[str, Any] = {}
        share = float(q_ul_min) / max(1, len(feeds))
        for pid in mesh.ports:
            if pid in feeds:
                states[pid] = _model.PortState(type="flow", q_ul_min=share)
            elif pid in vents:
                states[pid] = _model.PortState(type="pressure", p_pa=0.0)
            else:
                states[pid] = _model.PortState(type="closed")
        scen = _model.Scenario(
            step=step.get("step"), ports=states,
            fluid="aqueous", temp_c=step.get("temp_c"), mu_pa_s=mu_pa_s,
            chip_id=str(mesh.meta.get("chip_id") or ""),
            flow_ul_min=float(q_ul_min),
            note="built by particles.py for a per-step particle analysis",
        )
        return _solve_mod.solve_step(mesh, scen)
    q = float(q_ul_min) * 1e-9 / SEC_PER_MIN
    return _solve_provisional(mesh, {f: q / len(feeds) for f in feeds}, list(vents))


def _finding(fid: str, cls: str, severity: str, chip_id: str, feature_id: str,
             step: Any, metric: str, value: Any, limit: Any, units: str, message: str,
             fix_hint: str | None = None, **extra) -> dict:
    """analyse.py's finding shape (README analyse.py row), so it can consume these."""
    out = {
        "id": fid, "class": cls, "severity": severity, "chip_id": chip_id,
        "feature_id": feature_id, "step": step, "metric": metric, "value": value,
        "limit": limit, "units": units, "message": message, "fix_hint": fix_hint,
    }
    out.update(extra)
    return out


def _resolve_protocol(chip_id: str, protocol_id: str | None = None) -> tuple[dict | None, str | None]:
    """Find the protocol that drives this chip.

    Not always the same name: `chip/module3_v1_translated.json` is driven by
    `protocols/module3_v1.json`, and its `translated_from` field points at another CHIP,
    not at the protocol. Without this the translated chip silently gets no steps, no
    flows and therefore no sedimentation or tracking at all - which looked like a clean
    bill of health and was really an empty run.
    """
    candidates = [protocol_id] if protocol_id else []
    candidates.append(chip_id)
    if chip_id.endswith("_translated"):
        candidates.append(chip_id[: -len("_translated")])
    for cand in candidates:
        if not cand:
            continue
        path = ROOT / "protocols" / f"{cand}.json"
        if path.exists():
            data = json.loads(path.read_text())
            if data.get("steps"):
                return data, cand
    return None, None


def _dedupe_findings(findings: list[dict]) -> list[dict]:
    """Collapse the same finding repeated across identical protocol steps.

    Module 4 runs the same fill 24 times and module 3 washes three times, so an undeduped
    list reports one real problem a dozen times and buries the others. The steps it also
    applies to are kept in `also_steps` rather than thrown away.
    """
    out: list[dict] = []
    seen: dict[tuple, dict] = {}
    for f in findings:
        key = (f["class"], f["feature_id"], f["metric"], str(f["value"]),
               f.get("intent"), f["message"])
        if key in seen:
            seen[key].setdefault("also_steps", []).append(f["step"])
            continue
        seen[key] = f
        out.append(f)
    return out


def _classes_for(feature: dict, classes: dict[str, ParticleClass]) -> list[ParticleClass]:
    """Which particle classes this feature actually carries, from its own tags.

    `capsule_passable: false` is a design statement that the line must never see a
    capsule - the waste lines behind module3's weir are the case - so it is not clog
    checked against one. If a capsule reaches it, that is a retention finding about the
    weir, not a clog finding about the channel, and the two must not be conflated.
    """
    carries = str(feature.get("carries") or "").lower()
    if feature.get("capsule_passable") is False:
        return [classes["cells"]] if "cell" in carries else []
    if "capsule" in carries:
        # Both, because the contract's own anti-clog rule is written against a diameter
        # that CHANGES: a fresh capsule and the same capsule after culture differ by
        # capsule.swell_factor, and only the swollen one is marginal in a 0.2 mm lane.
        return [classes["capsule"], classes["capsule_swollen"]]
    if "cell" in carries:
        return [classes["cells"]]
    return []


def _classes_or_default(feature: dict, classes: dict[str, ParticleClass],
                        default: Sequence[ParticleClass]) -> list[ParticleClass]:
    """`_classes_for`, falling back to the chip's classes only when the feature is SILENT.

    An explicit `capsule_passable: false` is an answer, not a missing tag, so it must not
    be overwritten by the chip-wide default. module4's channels carry no tag at all and do
    fall back - which is how its feed lines get checked against capsules.
    """
    tagged = _classes_for(feature, classes)
    if tagged:
        return tagged
    if feature.get("capsule_passable") is False or feature.get("carries"):
        return tagged
    return list(default)


def _reagent_class(reagent: str | None) -> str | None:
    """Which particle class a step's `reagent` string is a suspension of.

    The protocol names its own content - 'capsule_suspension',
    'pooled_capsule_suspension', 'wash_buffer', 'trypsin' - so the content of a feed is
    data, not something to infer from the chip's prose. Matching the chip JSON text
    instead is how cell_prep_v1 got checked against capsules: its QC note mentions
    'single-capsule occupancy' while the chip never sees a capsule.
    """
    if not reagent:
        return None
    r = reagent.lower()
    if "capsule" in r:
        return "capsule"
    if "bead" in r:
        return "bead"
    if "cell" in r and "cell_free" not in r:
        return "cells"
    return None


def _particle_sources(chip: dict, protocol: dict | None,
                      classes: dict[str, ParticleClass]) -> dict[str, dict]:
    """Where each class ENTERS or already SITS on this chip, with the evidence.

    Without this, every class gets checked on every edge, which produces two kinds of
    nonsense: capsules in a chip that never sees one, and cells settling in the enzyme
    feed line of a module whose cells are adherent in a chamber downstream of it. A
    finding about a line that cannot contain the particle is worse than no finding,
    because it crowds out the real ones.
    """
    out: dict[str, dict] = {}
    steps = (protocol or {}).get("steps", [])

    # 1. Fed in by a step whose reagent is a suspension of this class.
    for si, step in enumerate(steps):
        name = _reagent_class(step.get("reagent"))
        if not name:
            continue
        feeds = (step.get("ports") or {}).get("feed") or []
        for f in feeds:
            entry = out.setdefault(name, {"ports": [], "chambers": [], "why": []})
            if f not in entry["ports"]:
                entry["ports"].append(f)
                entry["why"].append(
                    f"step {si} feeds {f} with {step.get('reagent')!r}, which the "
                    f"protocol names as a {name} suspension")

    # 2. Already resident: capsules held on a retention feature, cells adherent in a
    #    culture chamber. Both are sources for every later step, including the ones whose
    #    feed is pure buffer - that is what a wash or a backflush acts on.
    for ch in chip.get("chambers", []):
        if ch.get("retention") and "capsule" in out:
            e = out["capsule"]
            if ch["id"] not in e["chambers"]:
                e["chambers"].append(ch["id"])
                e["why"].append(f"{ch['id']} retains capsules, so they are resident there "
                                f"for every later wash, lysis and release step")
        if (ch.get("type") == "culture" or ch.get("block") == "culture_chamber"):
            e = out.setdefault("cells", {"ports": [], "chambers": [], "why": []})
            if ch["id"] not in e["chambers"]:
                e["chambers"].append(ch["id"])
                e["why"].append(
                    f"{ch['id']} is a culture chamber, so cells ORIGINATE there and exist "
                    f"only downstream of it - not in the reagent lines feeding it")

    if "capsule" in out:
        out["capsule_swollen"] = dict(out["capsule"])
        out["capsule_swollen"]["why"] = [
            w + " (checked again at the swollen diameter, which is the one that sets "
                "channel widths)" for w in out["capsule"]["why"]]
    return out


def _chip_particle_classes(chip: dict, protocol: dict | None,
                           classes: dict[str, ParticleClass]) -> list[ParticleClass]:
    sources = _particle_sources(chip, protocol, classes)
    return [classes[n] for n in ("capsule", "capsule_swollen", "cells", "bead")
            if n in sources and n in classes]


def _reachable_edges(mesh: Mesh, fld: Field, seeds: Sequence[str],
                     q_floor: float) -> set[str]:
    """Edge ids a particle released at `seeds` can actually reach, following the flow.

    A particle cannot settle in a line it never enters. Following the solved flow
    direction is what turns "every edge on the chip" into "the edges this particle
    traverses", and it is what separates cell_prep's real mixer loss from six invented
    findings in its reagent feeds.
    """
    adj: dict[str, list[tuple[str, str]]] = {}
    for e in mesh.edges:
        q = fld.q_m3_s.get(e.id, 0.0)
        if abs(q) < q_floor:
            continue
        a, b = (e.a, e.b) if q > 0 else (e.b, e.a)
        adj.setdefault(a, []).append((b, e.id))
    seen_nodes = set(seeds)
    stack = list(seeds)
    edges: set[str] = set()
    while stack:
        nid = stack.pop()
        for nxt, eid in adj.get(nid, []):
            edges.add(eid)
            if nxt not in seen_nodes:
                seen_nodes.add(nxt)
                stack.append(nxt)
    return edges


def analyse_chip(chip_id: str, protocol_id: str | None = None,
                 n_particles: int = 60) -> dict:
    """Run every particle check on one repo chip and return findings plus the detail.

    Takes ids rather than a Mesh and a Field only because mesh.py and solve.py are not
    there yet; when they are, this becomes `analyse_chip(mesh, field, scenario)` and the
    provisional builders above are deleted. The checks themselves already take Mesh/Field.
    """
    chip = json.loads((ROOT / "chip" / f"{chip_id}.json").read_text())
    protocol, pid = _resolve_protocol(chip_id, protocol_id)

    temps = [s.get("temp_c") for s in (protocol or {}).get("steps", [])
             if s.get("temp_c") is not None]
    visc = contracts.viscosity_for(max(temps) if temps else None)
    mu = visc.pa_s
    classes = particle_classes(mu)
    sources = _particle_sources(chip, protocol, classes)
    chip_classes = _chip_particle_classes(chip, protocol, classes)

    findings: list[dict] = []
    assumptions: dict[str, dict] = {}
    for pc in chip_classes:
        for a in pc.assumptions:
            assumptions[a.key] = a.as_json()
    for a in (START_HEIGHT_FRACTION, RESUSPENSION_SHEAR_DYN_CM2):
        assumptions[a.key] = a.as_json()

    # --- clog margins, per channel and per class it carries
    clogs: list[ClogVerdict] = []
    for c in chip.get("channels", []):
        passable = _port_capsule_passable(chip, c)
        cc = c if passable is None else {**c, "capsule_passable": passable}
        for pc in _classes_or_default(cc, classes, chip_classes):
            v = clog_check(c, pc)
            clogs.append(v)
            if not v.passes:
                findings.append(_finding(
                    f"clog:{chip_id}:{c['id']}:{pc.name}", "clog", v.severity, chip_id,
                    c["id"], None, "min_dimension_um", round(v.min_dimension_um, 1),
                    round(v.required_um, 1), "um", v.message,
                    f"widen {c['id']} to {v.required_um:.0f} um, or carry "
                    f"{pc.name} only before it swells"))
    for ch in chip.get("chambers", []):
        if ch.get("filter") or ch.get("retention"):
            for pc in chip_classes:
                if pc.name != "capsule":
                    continue
                v = clog_check(ch, pc)
                clogs.append(v)
                if not v.passes and ch.get("filter"):
                    findings.append(_finding(
                        f"clog:{chip_id}:{ch['id']}:{pc.name}", "clog", v.severity,
                        chip_id, ch["id"], None, "min_dimension_um",
                        round(v.min_dimension_um, 1), round(v.required_um, 1), "um",
                        v.message,
                        f"a filter gap below {v.required_um:.0f} um will blind on the "
                        f"capsules it is meant to pass"))

    # --- weir escape and the z sweep
    weirs: list[WeirBand] = []
    flows = [s.get("flow_ul_min") for s in (protocol or {}).get("steps", [])
             if s.get("flow_ul_min")]
    q_max = (max(flows) if flows else 100.0) * 1e-9 / SEC_PER_MIN
    for ch in chip.get("chambers", []):
        if not ch.get("retention"):
            continue
        for pc in (classes["capsule"], classes["capsule_swollen"]):
            band = weir_tolerance_band(ch, pc, q_max, mu)
            band.assumptions += [a.as_json() for a in pc.assumptions if a.key.endswith("rho_kg_m3")]
            weirs.append(band)
            if not band.works_at_nominal or band.covered_fraction < 1.0:
                sev = "blocker" if not band.works_at_nominal else "high"
                findings.append(_finding(
                    f"leak:{chip_id}:{ch['id']}:weir:{pc.name}", "leak", sev, chip_id,
                    ch["id"], None, "working_gap_band_um",
                    [round(band.gap_lo_um, 1), round(band.gap_hi_um, 1)],
                    [round(band.contract_low_um, 1), round(band.retention_limit_um, 1)],
                    "um", band.message,
                    "hold the plus side of the z tolerance to "
                    f"{band.z_offset_hi_um:+.0f} um, or use the contract's own lever and "
                    "raise the capsule diameter (see capsule_size_for_full_tolerance)",
                    covered_fraction=round(band.covered_fraction, 3),
                    nominal_dp_kpa=round(band.nominal_dp_kpa, 5)))
            # The contract's own reference pressure, checked rather than trusted.
            ref = float(contracts.block("retention.weir_geometry.pressure_drop_pa_at_100_ul_min"))
            mine = slit_pressure_pa(100e-9 / SEC_PER_MIN, band.nominal_gap_um,
                                    float((ch.get("size") or [1, 1])[1]), mu)
            if ref > 0 and not (0.5 <= mine / ref <= 2.0):
                findings.append(_finding(
                    f"unmodelled:{chip_id}:{ch['id']}:weir_dp_ref", "unmodelled", "medium",
                    chip_id, ch["id"], None, "weir_dp_pa_at_100_ul_min", round(mine, 2),
                    ref, "Pa",
                    f"contracts/blocks.json retention.weir_geometry says the weir drops "
                    f"{ref:.0f} Pa at 100 uL/min; the exact rectangular-duct form over a "
                    f"{band.nominal_gap_um:.0f} um gap x "
                    f"{float((ch.get('size') or [1, 1])[1]):.2f} mm width x "
                    f"{contracts.block('retention.weir_geometry.crest_thickness_um'):.0f} um "
                    f"crest gives {mine:.1f} Pa, {mine / ref:.1f}x the contract figure. "
                    f"One of the two is wrong and the report must say which; both are far "
                    f"below the {contracts.max_working_pressure_kpa():.2f} kPa ceiling, so "
                    f"it changes no verdict here - but it will the moment a gap narrows.",
                    "re-derive retention.weir_geometry.pressure_drop_pa_at_100_ul_min "
                    "from the per-chip slit width, or state the geometry it assumes"))
            break  # the gap band is set by the unswollen capsule; swollen only widens it

    # --- sedimentation and tracking, per protocol step
    sediments: list[SedimentVerdict] = []
    tracks: list[dict] = []
    grid_views: dict[str, dict] = {}
    boundary: dict[str, tuple] = {}
    steps = (protocol or {}).get("steps", [])
    seen_step: set[tuple] = set()
    mesh, mesh_source = _mesh_for(chip, mu)
    grid_views = _grid_views(mesh)
    boundary = _boundary_exits(mesh)
    for si, step in enumerate(steps):
        q_ul_min = step.get("flow_ul_min")
        operation = step.get("operation")
        feeds = (step.get("ports") or {}).get("feed") or []
        vents = (step.get("ports") or {}).get("vent") or []
        if not q_ul_min or not feeds or not vents:
            continue  # a no-flow hold: transport.py owns the soak, not this module
        feeds = [f for f in feeds if f in mesh.ports]
        vents = [v for v in vents if v in mesh.ports]
        if not feeds or not vents:
            continue
        sig = (tuple(feeds), tuple(vents), q_ul_min, operation)
        if sig in seen_step:
            continue
        seen_step.add(sig)
        q = q_ul_min * 1e-9 / SEC_PER_MIN
        try:
            fld = _solve_for(mesh, feeds, vents, q_ul_min, mu, step)
        except Exception:  # noqa: BLE001  # a step solve.py rejects is its finding to make
            continue

        # Which edges can each class actually reach this step? A particle cannot settle
        # in a line it never enters.
        reach: dict[str, set[str]] = {}
        for pc in chip_classes:
            seeds, _ = _seed_nodes(chip, mesh, pc, feeds, sources.get(pc.name))
            reach[pc.name] = _reachable_edges(mesh, fld, seeds, 1e-6 * q) if seeds else set()

        for e in mesh.edges:
            if e.kind == "hs_face":
                continue
            if e.kind == "slit":
                # A weir slit excludes by SIZE, not by settling: a 70 um capsule never
                # enters a 25 um gap, so "does it settle while crossing" is the wrong
                # question and would report a spurious escape. weir_tolerance_band() is
                # the check that belongs here.
                continue
            if e.kind == "mouth":
                # A mouth is a zero-length connector between a chamber and a channel, not
                # a conveying path. Its transit is ~0 s, so a settled fraction computed
                # over it is meaningless; the chamber's own cell check covers that volume.
                continue
            feature = next((c for c in chip.get("channels", []) if c["id"] == e.feature_id),
                           None) or \
                next((c for c in chip.get("chambers", []) if c["id"] == e.feature_id),
                     None) or {"id": e.feature_id}
            if feature.get("width_mm") is not None:  # a channel: inherit the port's rule
                passable = _port_capsule_passable(chip, feature)
                if passable is not None:
                    feature = {**feature, "capsule_passable": passable}
            area = e.width_mm * e.depth_mm * 1e-6
            qq = abs(fld.q_m3_s.get(e.id, 0.0))
            if area <= 0 or qq <= 0:
                continue
            # A capped line carries a numerically tiny leakage flow, which would give an
            # astronomical transit time and a bogus 100%-settled finding. A line with no
            # flow has no transport: the no-flow soak is transport.py's, not ours.
            if qq < 1e-6 * q:
                continue
            v = qq / area
            transit = (e.length_mm * 1e-3) / v
            node_a, node_b = mesh.node(e.a), mesh.node(e.b)
            is_chamber = any(ch["id"] == e.feature_id for ch in chip.get("chambers", []))
            if is_chamber:
                # A chamber meshed as a duct (a serpentine) is still a chamber: its intent
                # comes from what it is for, not from the node kinds either side of it.
                role = "chamber"
            else:
                role = ("feed" if node_a.kind == "port" else
                        "drain" if node_b.kind == "port" else "duct")
            for pc in _classes_or_default(feature, classes, chip_classes):
                if e.id not in reach.get(pc.name, set()):
                    continue
                sv = sediment_check(feature, e.id, "edge", pc, e.depth_mm, transit,
                                    v * 1e3, mu, role=role,
                                    floor_shear_dyn_cm2=fld.shear_dyn_cm2.get(e.id),
                                    operation=operation)
                sv_json = sv.as_json()
                sv_json["step"] = si
                sediments.append(sv)
                if sv.severity in ("high", "blocker") or sv.verdict == "partial_loss":
                    findings.append(_finding(
                        f"sediment:{chip_id}:{si}:{e.id}:{pc.name}", "sediment",
                        sv.severity, chip_id, str(e.feature_id), si,
                        "settled_fraction", round(sv.settled_fraction, 3),
                        0.0 if sv.intent == "loss" else 1.0, "fraction", sv.message,
                        ("raise the flow or shorten the line" if sv.intent == "loss"
                         else "lower the flow or lengthen the hold"),
                        intent=sv.intent, intent_why=sv.intent_why))

        for cid, grid in grid_views.items():
            if grid["kind"] != "grid":
                continue
            chamber = next(c for c in chip["chambers"] if c["id"] == cid)
            # Same guard as the edges: a chamber on a capped branch carries a numerically
            # tiny leakage flow, and dividing by it manufactures a multi-million-year
            # transit and a confident "100% settled" blocker out of nothing.
            throughput = sum(abs(fld.q_m3_s.get(ee.id, 0.0))
                             for _, _, ee in boundary.get(cid, ()))
            if throughput < 1e-6 * q:
                continue
            speeds = [math.hypot(*fld.u_mm_s.get(nid, (0, 0)))
                      for row in grid["ids"] for nid in row]
            mean_u = sum(speeds) / len(speeds) if speeds else 0.0
            if mean_u <= 0:
                continue
            span_mm = max(grid["size"][0], grid["size"][1])
            transit = (span_mm * 1e-3) / (mean_u * 1e-3)
            for pc in chip_classes:
                if not any(ee.id in reach.get(pc.name, set())
                           for _, _, ee in boundary.get(cid, ())):
                    continue
                sv = sediment_check(chamber, cid, "cell", pc, grid["depth_mm"], transit,
                                    mean_u, mu, role="chamber", operation=operation)
                sediments.append(sv)
                if sv.severity in ("high", "blocker"):
                    findings.append(_finding(
                        f"sediment:{chip_id}:{si}:{cid}:{pc.name}", "sediment",
                        sv.severity, chip_id, cid, si, "settled_fraction",
                        round(sv.settled_fraction, 3),
                        0.0 if sv.intent == "loss" else 1.0, "fraction", sv.message,
                        None, intent=sv.intent, intent_why=sv.intent_why))

        for pc in chip_classes:
            tr = track(chip, mesh, fld, pc, mu, feeds, n_particles=n_particles,
                       source=sources.get(pc.name), operation=operation,
                       grids=grid_views, boundary=boundary)
            if tr.launched:
                tracks.append({"step": si, "feeds": feeds, "flow_ul_min": q_ul_min,
                               "operation": operation, **tr.as_json()})

    findings = _dedupe_findings(findings)
    return {
        "chip_id": chip_id,
        "protocol_id": pid if protocol else None,
        "viscosity": {"pa_s": mu, "key": visc.key, "scope": visc.scope},
        "particle_classes": {pc.name: pc.as_json(mu) for pc in chip_classes},
        "weir_sides": mesh.meta.get("weir_sides") or {},
        "mesh_source": mesh_source,
        "findings": findings,
        "clog": [c.as_json() for c in clogs],
        "weir": [w.as_json() for w in weirs],
        "sediment": [s.as_json() for s in sediments],
        "tracks": tracks,
        "assumptions": list(assumptions.values()),
        "provisional_mesh": bool(mesh.meta.get("provisional", False)),
        "using_fallback_shapes": USING_FALLBACK_SHAPES,
        "have_real_pipeline": HAVE_REAL_PIPELINE,
    }


# --- CLI --------------------------------------------------------------------

TARGET_CHIPS = ("module3_v1_translated", "module4_v1", "cell_prep_v1")


def _summarise(result: dict, verbose: bool = False) -> str:
    out = [f"=== {result['chip_id']}  (mu {result['viscosity']['pa_s']} Pa s, "
           f"{result['viscosity']['key']})"]
    out.append("  particle classes:")
    for name, pc in result["particle_classes"].items():
        out.append(f"    {name:<16} d {pc['diameter_um']:>6.1f} um  "
                   f"drho {pc['delta_rho_kg_m3']:>6.2f} kg/m3  "
                   f"v_s {pc['settling_um_s']:>7.2f} um/s "
                   f"(range {pc['settling_um_s_range'][0]:.2f}-"
                   f"{pc['settling_um_s_range'][1]:.2f})")
    for k, why in (result["weir_sides"] or {}).items():
        if isinstance(why, str):
            out.append(f"  weir side: {why}")
        else:
            walls = sorted({w.get("wall", "?") for w in why})
            out.append(f"  weir {k}: {len(why)} slit(s) on {', '.join(walls)}")
    for w in result["weir"]:
        out.append(f"  WEIR {w['feature_id']} [{w['particle']}]: {w['message']}")
    for t in result["tracks"]:
        out.append(f"  TRACK step {t['step']} feeds {t['feeds']} @ "
                   f"{t['flow_ul_min']} uL/min -> {t['message']}")
    sev_order = {"blocker": 0, "high": 1, "medium": 2, "low": 3}
    out.append(f"  findings ({len(result['findings'])}):")
    for f in sorted(result["findings"], key=lambda f: sev_order.get(f["severity"], 9)):
        out.append(f"    [{f['severity']:<7}] {f['class']:<10} {f['feature_id']}: "
                   f"{f['message']}")
    if verbose:
        for s in result["sediment"]:
            out.append(f"    sed {s['element_id']:<28} {s['particle']:<16} "
                       f"{s['intent']:<7} {s['verdict']:<22} "
                       f"settled {s['settled_fraction']:.3f}")
    out.append("  assumptions flagged:")
    for a in result["assumptions"]:
        out.append(f"    {a['key']} = {a['value']} {a['units']}  [{a['source']}]")
    return "\n".join(out)


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chip", action="append", default=[])
    ap.add_argument("--all", action="store_true", help="the three verification chips")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--particles", type=int, default=60)
    args = ap.parse_args(argv)

    chips = list(args.chip) or (list(TARGET_CHIPS) if args.all else [TARGET_CHIPS[0]])
    results = [analyse_chip(c, n_particles=args.particles) for c in chips]
    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        for r in results:
            print(_summarise(r, args.verbose))
            print()
        band = capsule_size_for_full_tolerance()
        print("=== can the capsule-size lever buy the full z tolerance?")
        for k in ("with_contract_band", "retention_bound_only"):
            b = band[k]
            print(f"  {k}: D >= {b['diameter_um_needed']} um, "
                  f"achievable={b['achievable']}\n    {b['why']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

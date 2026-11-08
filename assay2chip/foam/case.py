"""assay2chip/foam/case.py: OpenFOAM dictionaries out of plain Python.

    python -m unittest tests.test_foam_case -v

Two layers, and you can stop at either one.

LAYER 1 - the writer. Any OpenFOAM dictionary is a nested mapping, so write one:

    from assay2chip.foam import case

    case.write_dict(path, {"nu": case.Dimensioned(1e-6, case.DIM_NU)},
                    class_name="dictionary", object_name="transportProperties")

    case.dumps({"a": 1, "b": {"c": (0, 0, -9.81)}})      # -> the text, no file

Mapping, in both directions, is boring on purpose: dict -> `{ }` block, list/tuple ->
`( )`, `Dimensioned` -> `[0 2 -1 0 0 0 0] 1e-06`, bool -> `true`/`false`, str -> written
through untouched so `uniform (0 0 0)` and `$internalField` work. `Verbatim` is an escape
hatch for `#include` and `codeStream`. Key order is the order you wrote it in and floats
go through one formatter, so the SAME INPUT ALWAYS PRODUCES THE SAME BYTES - which is
what lets a run manifest carry a hash of the case.

LAYER 2 - a complete laminar incompressible case for low Re, which is every chip in this
repo (assay2chip/cfd/README.md: "Re well under 1 almost everywhere"):

    fluid = case.fluid_for(37.0)                       # from the contracts, never water
    patches = {
        "p_in":  case.PatchBC("feed", q_m3_s=8.3e-10),  # 0.05 uL/min
        "p_out": case.PatchBC("vent"),                  # p = 0, zeroGradient U
        "p_mid": case.PatchBC("cap"),                   # noSlip
        "walls": case.PatchBC("wall"),
    }
    built = case.build_case(case_dir, fluid=fluid, patches=patches,
                            functions=case.standard_functions(patches))
    built.to_dict()          # straight into the run manifest

`build_case` writes `constant/transportProperties`, the laminar momentum-transport file,
`system/{controlDict,fvSchemes,fvSolution,decomposeParDict}` and `0/{U,p}`. It does NOT
write a mesh - `mesh.py` owns `constant/polyMesh`.

PRESSURE IS KINEMATIC. simpleFoam and pimpleFoam solve p/rho, so `0/p` is in m2/s2 and
every pressure that comes back out of a case has to be multiplied by rho before it is a
Pascal. `fluid.pressure_pa(p_kinematic)` does it, and `wall_shear_pa()` does the same for
`wallShearStress`, which is also kinematic for these solvers. Getting this wrong is a
silent factor of ~1000, so neither number is ever read raw.

FLUID PROPERTIES COME FROM THE CONTRACTS (assay2chip/foam/README.md):
nu = contracts.viscosity_for(temp_c) / fluid.density_37c_kg_m3. A fluid the contracts do
not cover is a FLAGGED assumption carried in `Fluid.assumption` and `CaseBuild.unresolved`
- never silently water. `fluid_from_spec()` takes the `FluidSpec` that
`assay2chip/cfd/scenario.py` already built for the step, assumption flags and all.

THE LAMINAR MODEL FILE DIFFERS BETWEEN DISTRIBUTIONS. ESI (openfoam.com, what we run)
wants `constant/momentumTransport` with `simulationType laminar;`; older ESI versions and
the Foundation builds spell it differently. `laminar_model_file()` picks it from the
installed version through `runner.py`, falls back to the ESI name, and records which it
used - it is never hard-coded at a call site.
"""

from __future__ import annotations

import hashlib
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402

FOAM_FORMAT_VERSION = "2.0"
DENSITY_KEY = "fluid.density_37c_kg_m3"

# The banner every OpenFOAM dictionary opens with. Cosmetic, but `foamDictionary` and a
# human reading a failed case both expect it, and leaving it out makes diffs noisy.
BANNER = r"""/*--------------------------------*- C++ -*----------------------------------*\
| =========                 |                                                 |
| \\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox           |
|  \\    /   O peration     |                                                 |
|   \\  /    A nd           | Written by assay2chip/foam/case.py              |
|    \\/     M anipulation  |                                                 |
\*---------------------------------------------------------------------------*/"""

RULE = "// " + "*" * 73 + " //"
FOOTER = "// " + "*" * 29 + " End of file " + "*" * 29 + " //"


class CaseError(RuntimeError):
    """A case could not be written, and the message says exactly what is missing.

    Never raised for anything a caller could not have fixed: a bad patch role names the
    four roles, a missing contract key names the key and the file.
    """


# --- dimensions --------------------------------------------------------------

@dataclass(frozen=True)
class Dimensions:
    """OpenFOAM's seven exponents: [kg m s K kmol A cd]."""

    mass: float = 0.0
    length: float = 0.0
    time: float = 0.0
    temperature: float = 0.0
    moles: float = 0.0
    current: float = 0.0
    luminous: float = 0.0

    def __str__(self) -> str:
        return "[" + " ".join(
            _number(v) for v in (self.mass, self.length, self.time, self.temperature,
                                 self.moles, self.current, self.luminous)
        ) + "]"


DIM_NONE = Dimensions()
DIM_LENGTH = Dimensions(length=1)
DIM_VELOCITY = Dimensions(length=1, time=-1)
DIM_NU = Dimensions(length=2, time=-1)                 # kinematic viscosity, m2/s
DIM_P_KINEMATIC = Dimensions(length=2, time=-2)        # p/rho, m2/s2 - see module docstring
DIM_PRESSURE = Dimensions(mass=1, length=-1, time=-2)  # Pa, for the record only
DIM_VOLUME_FLOW = Dimensions(length=3, time=-1)        # m3/s
DIM_DENSITY = Dimensions(mass=1, length=-3)
DIM_TIME = Dimensions(time=1)


@dataclass(frozen=True)
class Dimensioned:
    """`nu [0 2 -1 0 0 0 0] 1e-06` - a value that carries its units."""

    value: Any
    dimensions: Dimensions = DIM_NONE
    name: str | None = None

    def __str__(self) -> str:
        head = f"{self.name} " if self.name else ""
        return f"{head}{self.dimensions} {format_value(self.value)}"


@dataclass(frozen=True)
class Verbatim:
    """Text copied into the dictionary exactly, with no trailing semicolon.

    For `#include`, `#includeEtc`, `codeStream` blocks and anything else that is OpenFOAM
    syntax rather than a value.
    """

    text: str

    def __str__(self) -> str:
        return self.text


@dataclass(frozen=True)
class Function1:
    """An OpenFOAM `Function1`: a value that may vary with time. `constant 1.6e-11`.

    NOT a dimensioned scalar, and this is a real trap - `flowRateInletVelocity` takes a
    Function1, so writing `volumetricFlowRate constant [0 3 -1 0 0 0 0] 1.6e-11` fails
    with "Wrong token type - expected scalar value, found punctuation '['". Units are
    implied (m3/s here), not written.

    `Function1("table", [(0, 0), (1, 1e-9)])` gives a ramp, which is what a sibling
    writing a transient priming case will want.
    """

    kind: str = "constant"
    value: Any = 0.0

    def __str__(self) -> str:
        if self.kind == "constant":
            return f"constant {format_value(self.value)}"
        if isinstance(self.value, (list, tuple)):
            rows = " ".join(f"({format_value(a)} {format_value(b)})" for a, b in self.value)
            return f"{self.kind} ({rows})"
        return f"{self.kind} {format_value(self.value)}"


@dataclass(frozen=True)
class NamedBlocks:
    """`( name { ... } name { ... } )` - blockMeshDict's `boundary`, and nothing else yet."""

    items: Sequence[tuple[str, Mapping[str, Any]]]


@dataclass(frozen=True)
class Table:
    """A list written one entry per line, e.g. blockMeshDict `vertices`."""

    rows: Sequence[Any]


# --- formatting --------------------------------------------------------------

def _number(value: float | int) -> str:
    """One formatter for every number in this package, so output is byte-stable.

    Integral floats lose the `.0` (OpenFOAM reads `1` as a scalar perfectly well and the
    tutorials write it that way); everything else gets 12 significant digits, which is
    more than a double carries into a solver and fewer than repr's noise.
    """
    if isinstance(value, bool):  # bool is an int subclass; never let one through here
        raise CaseError("bool reached the number formatter - format_value handles it")
    if isinstance(value, int):
        return str(value)
    value = float(value)
    if math.isnan(value) or math.isinf(value):
        raise CaseError(f"{value!r} is not a number OpenFOAM can read")
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    text = f"{value:.12g}"
    return text


def format_value(value: Any) -> str:
    """One value, as OpenFOAM spells it. Strings pass through untouched."""
    if isinstance(value, (Dimensioned, Dimensions, Verbatim, Function1)):
        return str(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _number(value)
    if isinstance(value, str):
        return value
    if isinstance(value, Path):
        return f'"{value.as_posix()}"'
    if isinstance(value, (list, tuple)):
        return "(" + " ".join(format_value(v) for v in value) + ")"
    if value is None:
        raise CaseError(
            "None is not an OpenFOAM value: leave the key out, or write the word you mean "
            "(e.g. 'none' for a divScheme default)"
        )
    raise CaseError(f"don't know how to write {type(value).__name__} into an OpenFOAM dictionary")


def _entry(key: str, value: Any, indent: int) -> list[str]:
    pad = " " * indent
    if key.startswith("#"):  # #include "x", #includeEtc "y" - no semicolon
        return [f"{pad}{key} {format_value(value)}".rstrip()]
    if isinstance(value, Mapping):
        out = [f"{pad}{key}", f"{pad}{{"]
        out += _entries(value, indent + 4)
        out.append(f"{pad}}}")
        return out
    if isinstance(value, Verbatim):
        return [f"{pad}{key}" if not value.text else f"{pad}{key} {value.text}"]
    if isinstance(value, NamedBlocks):
        out = [f"{pad}{key}", f"{pad}("]
        for name, block in value.items:
            out.append(f"{pad}    {name}")
            out.append(f"{pad}    {{")
            out += _entries(block, indent + 8)
            out.append(f"{pad}    }}")
        out.append(f"{pad});")
        return out
    if isinstance(value, Table):
        out = [f"{pad}{key}", f"{pad}("]
        out += [f"{pad}    {format_value(row)}" for row in value.rows]
        out.append(f"{pad});")
        return out
    return [f"{pad}{key} {format_value(value)};"]


def _entries(mapping: Mapping[str, Any], indent: int) -> list[str]:
    out: list[str] = []
    for key, value in mapping.items():
        out += _entry(str(key), value, indent)
    return out


def foam_header(
    class_name: str = "dictionary",
    object_name: str = "",
    location: str | None = None,
    *,
    note: str = "",
) -> str:
    """The `FoamFile` block. `location` is the case-relative directory, e.g. `"system"`."""
    body: dict[str, Any] = {
        "version": FOAM_FORMAT_VERSION,
        "format": "ascii",
        "class": class_name,
    }
    if note:
        body["note"] = f'"{note}"'
    if location:
        body["location"] = f'"{location}"'
    body["object"] = object_name
    lines = [BANNER, "FoamFile", "{"] + _entries(body, 4) + ["}", RULE, ""]
    return "\n".join(lines)


def dumps(
    body: Mapping[str, Any],
    class_name: str = "dictionary",
    object_name: str = "",
    location: str | None = None,
    *,
    note: str = "",
) -> str:
    """A whole dictionary file as text: header, entries, footer."""
    lines = [foam_header(class_name, object_name, location, note=note)]
    lines += _entries(body, 0)
    lines += ["", FOOTER, ""]
    return "\n".join(lines)


def write_dict(
    path: Path | str,
    body: Mapping[str, Any],
    class_name: str = "dictionary",
    object_name: str | None = None,
    location: str | None = None,
    *,
    note: str = "",
) -> Path:
    """Write one dictionary. `object_name` and `location` default to the path itself."""
    path = Path(path)
    object_name = object_name if object_name is not None else path.name
    if location is None:
        location = path.parent.name
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dumps(body, class_name, object_name, location, note=note)
    path.write_text(text, encoding="utf-8")
    return path


def read_entry(path: Path | str, key: str) -> str | None:
    """The raw text of one top-level `key value;` entry. For tests and quick checks only.

    This is NOT a dictionary parser - it does not descend into blocks and it does not
    expand macros. Anything that needs real parsing should call `foamDictionary` through
    `runner.run()`.
    """
    text = Path(path).read_text(encoding="utf-8")
    match = re.search(rf"^\s*{re.escape(key)}\s+([^;]+);", text, re.M)
    return match.group(1).strip() if match else None


def case_digest(case_dir: Path | str) -> str:
    """sha256 over every dictionary in the case, so a manifest can prove what was run.

    Time directories and `processor*` are skipped: they are solver OUTPUT, and including
    them would make the digest of a case change just by running it.
    """
    case_dir = Path(case_dir)
    digest = hashlib.sha256()
    for path in sorted(p for p in case_dir.rglob("*") if p.is_file()):
        rel = path.relative_to(case_dir)
        if rel.parts and (rel.parts[0].startswith("processor") or _is_time_dir(rel.parts[0])):
            continue
        digest.update(rel.as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _is_time_dir(name: str) -> bool:
    if name == "0":
        return False  # 0/ is input, not output
    try:
        float(name)
    except ValueError:
        return False
    return True


# --- fluid -------------------------------------------------------------------

@dataclass(frozen=True)
class Fluid:
    """What the solver needs about the liquid, and how sure we are of it.

    `nu_m2_s` is the only number OpenFOAM reads. `rho_kg_m3` never reaches the solver -
    incompressible OpenFOAM divides it out - but every result has to be multiplied back by
    it, so it travels with the case and is written into `transportProperties` as a comment
    entry (`rhoInf`) for anyone reading the case by hand.
    """

    name: str
    nu_m2_s: float
    mu_pa_s: float
    rho_kg_m3: float
    temp_c: float | None
    source: str
    contract_key: str | None = None
    fluid_class: str = "aqueous"
    assumption: bool = False
    why: str = ""

    def pressure_pa(self, p_kinematic: float) -> float:
        """m2/s2 out of the solver -> Pa. simpleFoam's `p` is p/rho."""
        return float(p_kinematic) * self.rho_kg_m3

    def wall_shear_pa(self, tau_kinematic: float) -> float:
        """`wallShearStress` out of an incompressible solver is tau/rho (m2/s2) -> Pa."""
        return float(tau_kinematic) * self.rho_kg_m3

    def wall_shear_dyn_cm2(self, tau_kinematic: float) -> float:
        """The unit the shear ceilings are stated in: Pa x 10 (assay2chip/cfd/README.md)."""
        return self.wall_shear_pa(tau_kinematic) * 10.0

    def reynolds(self, u_m_s: float, length_m: float) -> float:
        return abs(float(u_m_s)) * float(length_m) / self.nu_m2_s

    def to_dict(self) -> dict:
        out = {
            "name": self.name,
            "class": self.fluid_class,
            "nu_m2_s": self.nu_m2_s,
            "mu_pa_s": self.mu_pa_s,
            "rho_kg_m3": self.rho_kg_m3,
            "temp_c": self.temp_c,
            "source": self.source,
            "assumption": self.assumption,
        }
        if self.contract_key:
            out["contract_key"] = self.contract_key
        if self.why:
            out["why"] = self.why
        return out


def density_kg_m3() -> float:
    """`fluid.density_37c_kg_m3` from the contracts. Never written down here."""
    return float(contracts.param(DENSITY_KEY))


def fluid_for(temp_c: float | None, *, name: str = "aqueous") -> Fluid:
    """The contract fluid at a temperature: nu = viscosity_for(temp) / density.

    `contracts.viscosity_for` already chooses between the 37 C value and water at 20 C and
    explains the choice, so this adds only the density division. Anything the contracts do
    not cover must come through `fluid_from_spec` instead, which keeps its assumption flag.
    """
    visc = contracts.viscosity_for(temp_c)
    rho = density_kg_m3()
    return Fluid(
        name=name,
        nu_m2_s=visc.pa_s / rho,
        mu_pa_s=visc.pa_s,
        rho_kg_m3=rho,
        temp_c=temp_c if temp_c is not None else visc.temp_c,
        source=f"{visc.key} / {DENSITY_KEY}",
        contract_key=visc.key,
        why=visc.scope,
    )


def fluid_from_spec(spec: Any) -> Fluid:
    """A `Fluid` out of `assay2chip.cfd.scenario.FluidSpec`, assumption flags intact.

    The scenario module has ALREADY decided between the two contract viscosities and the
    flagged assumptions for oils, cold solvents and viscous core buffers, and recorded why
    in the scenario's `unresolved`. Re-deciding it here would be a second opinion that
    nobody asked for, so this only divides by the density.

    Anything with `assumption=True` arrives here still flagged and leaves still flagged;
    `build_case` copies that into `CaseBuild.unresolved` so the render and the summary say
    the case rests on an assumed property.
    """
    for attr in ("name", "viscosity_pa_s", "source"):
        if not hasattr(spec, attr):
            raise CaseError(
                f"fluid_from_spec() wants a scenario.FluidSpec (or anything with .name, "
                f".viscosity_pa_s, .source); got {type(spec).__name__} with no .{attr}"
            )
    rho = density_kg_m3()
    mu = float(spec.viscosity_pa_s)
    if not mu > 0:
        raise CaseError(f"fluid {spec.name!r} has viscosity {mu!r}: a case cannot be built from it")
    return Fluid(
        name=str(spec.name),
        nu_m2_s=mu / rho,
        mu_pa_s=mu,
        rho_kg_m3=rho,
        temp_c=getattr(spec, "temp_c", None),
        source=f"{spec.source} / {DENSITY_KEY}",
        contract_key=getattr(spec, "contract_key", None),
        fluid_class=getattr(spec, "fluid_class", "aqueous"),
        assumption=bool(getattr(spec, "assumption", False)),
        why=getattr(spec, "why", ""),
    )


# --- boundary conditions -----------------------------------------------------

ROLES = ("feed", "vent", "cap", "wall")

#: What each role means, as `assay2chip/cfd/scenario.py` defines it. Kept as prose next to
#: the code that implements it because the three-way feed/vent/cap split is the contract
#: between the protocol reader and every solver in the repo.
ROLE_MEANING = {
    "feed": "driven inlet: flowRateInletVelocity at the step's volumetric flow rate, p zeroGradient",
    "vent": "open to atmosphere: p fixed at 0, U zeroGradient",
    "cap": "a plugged port, which is just a wall: noSlip, p zeroGradient",
    "wall": "the chip itself: noSlip, p zeroGradient",
}


@dataclass(frozen=True)
class PatchBC:
    """What one mesh patch does for one step.

    `q_m3_s` is SIGNED and positive means INTO the chip, which is what
    `flowRateInletVelocity` wants. A negative rate on a feed is a withdrawing pump and is
    allowed; zero is not, because a feed that drives nothing is a `cap` and saying so
    makes the case readable.
    """

    role: str
    q_m3_s: float | None = None
    p_pa: float = 0.0
    note: str = ""

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise CaseError(
                f"unknown patch role {self.role!r}: the four are {', '.join(ROLES)} "
                f"({'; '.join(f'{k} = {v}' for k, v in ROLE_MEANING.items())})"
            )
        if self.role == "feed":
            if self.q_m3_s is None:
                raise CaseError(
                    "a 'feed' patch needs q_m3_s (volumetric flow INTO the chip, m3/s). "
                    "Use scenario.ul_min_to_m3_s() to convert the protocol's uL/min, or "
                    "make it a 'cap' if nothing drives it."
                )
            if float(self.q_m3_s) == 0.0:
                raise CaseError(
                    "a 'feed' patch with q_m3_s = 0 drives nothing: use role='cap' so the "
                    "case says what it means"
                )

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"role": self.role}
        if self.role == "feed":
            out["q_m3_s"] = self.q_m3_s
            out["q_ul_min"] = (self.q_m3_s or 0.0) * 6e10
        if self.role == "vent":
            out["p_pa"] = self.p_pa
        if self.note:
            out["note"] = self.note
        return out


def patches_from_scenario(record: Any, *, wall_patch: str = "walls") -> dict[str, PatchBC]:
    """Turn one `scenario.ScenarioRecord` (or `model.Scenario`) into a patch map.

    flow -> feed, pressure -> vent, closed -> cap, plus the single `walls` patch the mesher
    always writes. The port ids become patch names unchanged, which is the rule the mesher
    and the renderer both rely on (assay2chip/foam/README.md: "One patch per port (named by
    port id) plus `walls`").
    """
    ports = getattr(record, "ports", None)
    if not ports:
        raise CaseError(
            f"{type(record).__name__} has no `ports`: patches_from_scenario() wants a "
            f"scenario.ScenarioRecord or a model.Scenario"
        )
    out: dict[str, PatchBC] = {}
    for port, state in ports.items():
        kind = getattr(state, "type", None)
        why = getattr(state, "why", "") or getattr(state, "note", "") or ""
        if kind == "flow":
            q_ul_min = getattr(state, "q_ul_min", None)
            if q_ul_min is None:
                raise CaseError(f"port {port!r} is a flow boundary with no q_ul_min")
            if float(q_ul_min) == 0.0:
                out[port] = PatchBC("cap", note=f"flow boundary at 0 uL/min. {why}".strip())
                continue
            out[port] = PatchBC("feed", q_m3_s=float(q_ul_min) / 6e10, note=why)
        elif kind == "pressure":
            out[port] = PatchBC("vent", p_pa=float(getattr(state, "p_pa", 0.0) or 0.0), note=why)
        else:
            out[port] = PatchBC("cap", note=why)
    out[wall_patch] = PatchBC("wall", note="every chip surface that is not a port")
    return out


def _check_patch(name: str, bc: PatchBC) -> None:
    if not name or not re.match(r"^[A-Za-z_][A-Za-z0-9_.-]*$", name):
        raise CaseError(
            f"{name!r} is not a usable patch name: OpenFOAM patch names (and so port ids) "
            f"must start with a letter or underscore and hold only letters, digits, "
            f"underscore, dot or hyphen"
        )
    if not isinstance(bc, PatchBC):
        raise CaseError(
            f"patch {name!r} maps to {type(bc).__name__}, not a case.PatchBC - build it "
            f"with case.PatchBC(role, q_m3_s=...)"
        )


def validate_patches(patches: Mapping[str, PatchBC]) -> list[str]:
    """Check a patch map and return the warnings; raise on anything unsolvable.

    Unsolvable: no patches at all, or a patch that is not a PatchBC. A case with feeds but
    no vent has nowhere for the liquid to go and an incompressible solver will simply fail
    to converge, so that is an error too - and a far better message than the solver's.
    A case with NO feed is only a warning: a pure pressure-driven case is legitimate.
    """
    if not patches:
        raise CaseError("a case needs at least one patch: pass the mesh's patch map")
    for name, bc in patches.items():
        _check_patch(name, bc)
    feeds = [n for n, b in patches.items() if b.role == "feed"]
    vents = [n for n, b in patches.items() if b.role == "vent"]
    warnings: list[str] = []
    if feeds and not vents:
        raise CaseError(
            f"feed patch(es) {', '.join(feeds)} drive liquid into a case with NO vent: an "
            f"incompressible solver has nowhere to put it and will not converge. Open one "
            f"port as role='vent' (p = 0) - in the protocol that is the `ports.vent` entry."
        )
    if not feeds:
        warnings.append(
            "no feed patch: this case is driven by pressure differences alone (or by "
            "nothing at all, if every vent sits at the same pressure)"
        )
    net = sum(b.q_m3_s or 0.0 for b in patches.values() if b.role == "feed")
    if feeds and net <= 0:
        warnings.append(
            f"the feed patches sum to {net:.3g} m3/s (not positive): the case withdraws "
            f"liquid through its vents"
        )
    if not any(b.role == "wall" for b in patches.values()):
        warnings.append(
            "no patch has role='wall': the chip surfaces are unconstrained unless the mesh "
            "names them differently"
        )
    return warnings


# --- the laminar momentum-transport file ------------------------------------

#: ESI OpenCFD OpenFOAM - what `assay2chip/foam/README.md` pins and what we run. VERIFIED
#: against the v2606 install: `$FOAM_TUTORIALS/incompressible/simpleFoam/pitzDaily/constant/`
#: holds `turbulenceProperties`, and `momentumTransport` appears nowhere in the tree. The
#: Foundation line (openfoam.org, OpenFOAM 8+) is the one that renamed it to
#: `momentumTransport`; mixing them up writes a file the solver silently ignores, which is
#: why this is a named constant with a test and not a literal at a call site.
ESI_LAMINAR_FILE = "turbulenceProperties"
#: The Foundation (openfoam.org) spelling. We do not run it - it does not build natively on
#: macOS - but the name is here so `laminar_model_file()` can answer honestly for one.
FOUNDATION_LAMINAR_FILE = "momentumTransport"

#: The laminar sub-dictionary keyword, also verified against v2606's own tutorials, which
#: write `laminar { model Stokes; }` (ESI uses `model`; the Foundation uses `laminarModel`).
ESI_LAMINAR_MODEL_KEY = "model"
FOUNDATION_LAMINAR_MODEL_KEY = "laminarModel"


def _is_foundation(version: str | None) -> bool:
    """A Foundation version is a small integer (8, 9, ... 13); ESI's is a date (2606)."""
    if not version:
        return False
    digits = re.sub(r"\D", "", str(version))
    return bool(digits) and len(digits) <= 2


def laminar_model_file(version: str | None = None) -> str:
    """Which file in `constant/` holds `simulationType laminar;` for this OpenFOAM.

    The spelling differs between distributions - exactly the kind of detail that silently
    produces a case the solver ignores - so it is resolved once, here, from the installed
    version rather than written at a call site. With no install the ESI name is used,
    because `assay2chip/foam/README.md` pins ESI.

    `build_case` records the answer in `CaseBuild.momentum_transport_file`.
    """
    if version is None:
        try:
            from assay2chip.foam import runner  # local import: case.py must work with no OpenFOAM
            version = runner.version()
        except Exception:  # pragma: no cover - runner is optional at write time
            version = None
    return FOUNDATION_LAMINAR_FILE if _is_foundation(version) else ESI_LAMINAR_FILE


# --- the dictionaries --------------------------------------------------------

def transport_properties(fluid: Fluid) -> dict:
    """`constant/transportProperties`: one number the solver reads, nu."""
    return {
        "transportModel": "Newtonian",
        "nu": Dimensioned(fluid.nu_m2_s, DIM_NU),
        # Not read by an incompressible solver. Written so that a case on disk carries the
        # density every pressure and shear number out of it has to be multiplied by.
        "rhoInf": Dimensioned(fluid.rho_kg_m3, DIM_DENSITY),
    }


def momentum_transport(version: str | None = None) -> dict:
    """Laminar, Newtonian. Re << 1 in this repo (assay2chip/cfd/README.md).

    `Stokes` is OpenFOAM's name for the plain Newtonian laminar stress model - it is NOT
    the Stokes-flow approximation and does not drop the convective term. Convection stays
    in the equations and is simply negligible at the Re the chips run at; where it is not
    (Module 6's quench at Re 13-23) the same model is still right.
    """
    key = FOUNDATION_LAMINAR_MODEL_KEY if _is_foundation(version) else ESI_LAMINAR_MODEL_KEY
    return {"simulationType": "laminar", "laminar": {key: "Stokes"}}


def control_dict(
    *,
    application: str = "simpleFoam",
    end_time: float = 2000,
    delta_t: float = 1,
    write_interval: float = 500,
    start_from: str = "startTime",
    start_time: float = 0,
    write_control: str = "timeStep",
    purge_write: int = 2,
    write_format: str = "ascii",
    write_precision: int = 8,
    functions: Mapping[str, Any] | None = None,
    libs: Sequence[str] = (),
    extra: Mapping[str, Any] | None = None,
) -> dict:
    """`system/controlDict`.

    For a steady `simpleFoam` run `end_time` is an ITERATION COUNT and `delta_t` is 1:
    pseudo-time has no physical meaning and the run stops on the `residualControl` in
    fvSolution long before it. For `pimpleFoam` they are real seconds.
    """
    body: dict[str, Any] = {
        "application": application,
        "startFrom": start_from,
        "startTime": start_time,
        "stopAt": "endTime",
        "endTime": end_time,
        "deltaT": delta_t,
        "writeControl": write_control,
        "writeInterval": write_interval,
        "purgeWrite": purge_write,
        "writeFormat": write_format,
        "writePrecision": write_precision,
        "writeCompression": "off",
        "timeFormat": "general",
        "timePrecision": 6,
        "runTimeModifiable": True,
    }
    if libs:
        body["libs"] = list(libs)
    if extra:
        body.update(extra)
    if functions:
        body["functions"] = dict(functions)
    return body


def fv_schemes(*, steady: bool = True, scalars: Sequence[str] = ()) -> dict:
    """`system/fvSchemes`. Second order where it is safe, bounded upwind on convection.

    `div(phi,U)` is `bounded Gauss linearUpwind grad(U)`: second-order accurate, and the
    `bounded` prefix subtracts the (zero at convergence) continuity error, which is what
    keeps a steady low-Re case from drifting. At Re << 1 convection barely matters and
    this choice costs nothing; at the Re 13-23 the audit found in Module 6 it is the
    difference between a converged answer and an oscillating one.
    """
    div: dict[str, Any] = {"default": "none"}
    div["div(phi,U)"] = ("bounded " if steady else "") + "Gauss linearUpwind grad(U)"
    for name in scalars:
        div[f"div(phi,{name})"] = ("bounded " if steady else "") + "Gauss limitedLinear 1"
    div["div((nuEff*dev2(T(grad(U)))))"] = "Gauss linear"
    return {
        "ddtSchemes": {"default": "steadyState" if steady else "Euler"},
        "gradSchemes": {"default": "Gauss linear", "limited": "cellLimited Gauss linear 1"},
        "divSchemes": div,
        "laplacianSchemes": {"default": "Gauss linear corrected"},
        "interpolationSchemes": {"default": "linear"},
        "snGradSchemes": {"default": "corrected"},
        "wallDist": {"method": "meshWave"},
    }


def fv_solution(
    *,
    steady: bool = True,
    p_tolerance: float = 1e-8,
    u_tolerance: float = 1e-9,
    residual_p: float = 1e-5,
    residual_u: float = 1e-6,
    n_non_orthogonal_correctors: int = 0,
    n_correctors: int = 2,
    relaxation_u: float = 0.9,
    relaxation_p: float = 0.9,
    scalars: Sequence[str] = (),
) -> dict:
    """`system/fvSolution`. GAMG on pressure, which is where all the work is.

    GAMG (geometric-algebraic multigrid) is the right pressure solver for a structured hex
    mesh of this size: its cost is near-linear in cell count where PCG's is not.

    `consistent yes` is SIMPLEC, which lets the relaxation factors go to ~0.9 instead of
    the 0.3/0.7 a plain SIMPLE run needs, and converges a Stokes-regime case in a few
    hundred iterations rather than a few thousand.

    `residualControl` is what actually stops the run - `endTime` in controlDict is only a
    backstop. `runner.py` reads BOTH out of the log and refuses to call a run converged
    just because it ran out of iterations.
    """
    solvers: dict[str, Any] = {
        "p": {
            "solver": "GAMG",
            "smoother": "GaussSeidel",
            "tolerance": p_tolerance,
            "relTol": 0.01 if steady else 0.01,
            "nPreSweeps": 0,
            "nPostSweeps": 2,
            "cacheAgglomeration": True,
            "agglomerator": "faceAreaPair",
            "nCellsInCoarsestLevel": 100,
            "mergeLevels": 1,
        },
        # `(Final)?` is not decoration. On the last PIMPLE corrector the solvers are
        # looked up as `UFinal`, `ageFinal` and so on, and a regex without it does not
        # match them - so a transient run dies at the first time step with
        # `Entry 'UFinal' not found in dictionary "system/fvSolution/solvers"`. It took
        # out pimpleFoam on spc_demo_v1 step 1 (and would take out every interFoam and
        # every transient scalar transport case in this package the same way).
        '"(U|k|epsilon|omega|T|s|age)(Final)?"': {
            "solver": "smoothSolver",
            "smoother": "symGaussSeidel",
            "tolerance": u_tolerance,
            "relTol": 0.01 if steady else 0,
        },
    }
    if not steady:
        final = dict(solvers["p"])
        final["relTol"] = 0
        solvers["pFinal"] = final
    body: dict[str, Any] = {"solvers": solvers}
    if steady:
        control = {
            "nNonOrthogonalCorrectors": n_non_orthogonal_correctors,
            "consistent": True,
            "residualControl": {"p": residual_p, "U": residual_u},
        }
        for name in scalars:
            control["residualControl"][name] = residual_u
        body["SIMPLE"] = control
        body["relaxationFactors"] = {
            "equations": {"U": relaxation_u, '".*"': relaxation_p},
        }
    else:
        body["PIMPLE"] = {
            "nOuterCorrectors": 1,
            "nCorrectors": n_correctors,
            "nNonOrthogonalCorrectors": n_non_orthogonal_correctors,
        }
    return body


def decompose_par_dict(n: int, method: str = "scotch") -> dict:
    """`system/decomposeParDict`. scotch needs no hints and balances a chip mesh well."""
    if n < 1:
        raise CaseError(f"decomposeParDict needs at least 1 subdomain, got {n}")
    body: dict[str, Any] = {"numberOfSubdomains": n, "method": method}
    if method == "simple":
        body["coeffs"] = {"n": (n, 1, 1)}
    return body


# --- fields ------------------------------------------------------------------

def u_boundary(name: str, bc: PatchBC) -> dict:
    """One patch's entry in `0/U`."""
    if bc.role == "feed":
        return {
            "type": "flowRateInletVelocity",
            # `volumetricFlowRate` is m3/s and POSITIVE IS INTO THE DOMAIN. The velocity
            # is spread over the patch area by OpenFOAM, so the mesh's port area - not a
            # number written here - sets the inlet speed.
            # It is a Function1, so NO dimensions bracket: see case.Function1.
            "volumetricFlowRate": Function1("constant", float(bc.q_m3_s or 0.0)),
            "value": "uniform (0 0 0)",
        }
    if bc.role == "vent":
        return {"type": "zeroGradient"}
    return {"type": "noSlip"}


def p_boundary(name: str, bc: PatchBC, fluid: Fluid) -> dict:
    """One patch's entry in `0/p`. KINEMATIC: Pa / rho (see the module docstring)."""
    if bc.role == "vent":
        return {"type": "fixedValue", "value": f"uniform {_number(bc.p_pa / fluid.rho_kg_m3)}"}
    return {"type": "zeroGradient"}


def field_u(patches: Mapping[str, PatchBC]) -> dict:
    return {
        "dimensions": DIM_VELOCITY,
        "internalField": "uniform (0 0 0)",
        "boundaryField": {name: u_boundary(name, bc) for name, bc in patches.items()},
    }


def field_p(patches: Mapping[str, PatchBC], fluid: Fluid) -> dict:
    return {
        "dimensions": DIM_P_KINEMATIC,
        "internalField": "uniform 0",
        "boundaryField": {name: p_boundary(name, bc, fluid) for name, bc in patches.items()},
    }


# --- function objects --------------------------------------------------------

FIELD_FUNCTION_LIBS = ("fieldFunctionObjects",)


def wall_shear_stress(patches: Sequence[str] = (), *, write_control: str = "writeTime") -> dict:
    """`wallShearStress`. KINEMATIC for an incompressible solver: tau/rho, m2/s2.

    Multiply by rho - `Fluid.wall_shear_pa()` - before comparing with any ceiling. The
    ceilings in `contracts` are in dyn/cm^2, which is Pa x 10.
    """
    body: dict[str, Any] = {
        "type": "wallShearStress",
        "libs": list(FIELD_FUNCTION_LIBS),
        "writeControl": write_control,
        "executeControl": write_control,
    }
    if patches:
        body["patches"] = list(patches)
    return body


def patch_flow_rate(patch: str, *, write_interval: int = 50) -> dict:
    """Volumetric flow through one patch, m3/s (sum of phi). Negative = out of the chip.

    This is the number that proves the mesh and the BCs agree: the sum over every patch
    must come back to zero, and the split between vents is what `flow.py` cross-checks
    against the reduced model.
    """
    return {
        "type": "surfaceFieldValue",
        "libs": list(FIELD_FUNCTION_LIBS),
        "regionType": "patch",
        "name": patch,
        "operation": "sum",
        "fields": ["phi"],
        "writeFields": False,
        "writeControl": "timeStep",
        "writeInterval": write_interval,
        "log": True,
    }


def patch_mean_pressure(patch: str, *, write_interval: int = 50) -> dict:
    """Area-averaged KINEMATIC pressure on one patch, m2/s2. x rho for Pa."""
    return {
        "type": "surfaceFieldValue",
        "libs": list(FIELD_FUNCTION_LIBS),
        "regionType": "patch",
        "name": patch,
        "operation": "areaAverage",
        "fields": ["p"],
        "writeFields": False,
        "writeControl": "timeStep",
        "writeInterval": write_interval,
        "log": True,
    }


def field_min_max(fields: Sequence[str] = ("U", "p"), *, write_interval: int = 50) -> dict:
    """Min and max of each field with the cell it happens in. Cheap sanity check."""
    return {
        "type": "fieldMinMax",
        "libs": list(FIELD_FUNCTION_LIBS),
        "fields": list(fields),
        "location": True,
        "writeControl": "timeStep",
        "writeInterval": write_interval,
        "log": True,
    }


def age_function(*, n_corr: int = 5, write_control: str = "writeTime", schemes_file: bool = True) -> dict:
    """Mean age of fluid, seconds: how long the liquid in each cell has been in the chip.

    THIS IS THE DEAD-ZONE TEST in 3D. Hele-Shaw has no closed streamlines, so the reduced
    model finds traps by residence time (assay2chip/cfd/README.md); `age` is the same idea
    solved on the real 3D field - it integrates a transport equation for age with a unit
    source, so a corner the flow never reaches shows an age far above the chamber mean.

    `age` needs its own `div(phi,age)` scheme and an `age` solver entry; `fv_schemes` and
    `fv_solution` take `scalars=("age",)` for exactly this. AVAILABILITY: `age` is an ESI
    fieldFunctionObject - `runner.has_function_object("age")` says whether the installed
    version really has it, and `standard_functions()` asks before including it.
    """
    return {
        "type": "age",
        "libs": list(FIELD_FUNCTION_LIBS),
        "nCorr": n_corr,
        "schemesField": "age" if schemes_file else "U",
        "writeControl": write_control,
        "executeControl": write_control,
    }


#: Function objects that solve a transport equation of their own, and the field each one
#: introduces. Such a field needs a `div(phi,<field>)` scheme and a solver entry, or the
#: run dies on its first iteration.
SOLVING_FUNCTION_OBJECTS = {"age": "age"}


def solved_fields(functions: Mapping[str, Any]) -> list[str]:
    """Fields that the given function objects will solve for, in order.

    Looks at each entry's `type`, not its key, because a caller is free to name the entry
    anything (`functions = {"meanAge": case.age_function()}`).
    """
    out: list[str] = []
    for body in functions.values():
        if not isinstance(body, Mapping):
            continue
        field_name = SOLVING_FUNCTION_OBJECTS.get(str(body.get("type", "")))
        if field_name and field_name not in out:
            out.append(field_name)
    return out


def standard_functions(
    patches: Mapping[str, PatchBC],
    *,
    include_age: bool | None = None,
    write_interval: int = 50,
) -> dict:
    """The function objects every chip case gets, keyed so siblings can add or drop one.

    * `wallShearStress` on every wall and cap patch - the shear findings;
    * `flow_<patch>` on every port - the flow split, and the mass-balance check;
    * `p_<patch>` on every port - the pressure drop, for the cross-check against
      `assay2chip/cfd`;
    * `fieldMinMax` on U and p - a cheap "did this blow up" signal;
    * `age`, when the installed OpenFOAM has it (pass `include_age` to decide yourself).
    """
    walls = [n for n, b in patches.items() if b.role in ("wall", "cap")]
    ports = [n for n, b in patches.items() if b.role in ("feed", "vent")]
    out: dict[str, Any] = {"wallShearStress": wall_shear_stress(walls)}
    for port in ports:
        out[f"flow_{port}"] = patch_flow_rate(port, write_interval=write_interval)
        out[f"p_{port}"] = patch_mean_pressure(port, write_interval=write_interval)
    out["fieldMinMax"] = field_min_max(write_interval=write_interval)
    if include_age is None:
        try:
            from assay2chip.foam import runner
            include_age = runner.has_function_object("age")
        except Exception:  # pragma: no cover
            include_age = False
    if include_age:
        out["age"] = age_function()
    return out


# --- the whole case ----------------------------------------------------------

@dataclass
class CaseBuild:
    """What `build_case` wrote, for the run manifest and the summary JSON."""

    case_dir: Path
    solver: str
    fluid: Fluid
    patches: dict[str, PatchBC]
    files: list[str] = field(default_factory=list)
    momentum_transport_file: str = ESI_LAMINAR_FILE
    foam_version: str | None = None
    functions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    steady: bool = True
    n_cores: int = 1

    @property
    def digest(self) -> str:
        return case_digest(self.case_dir)

    def to_dict(self) -> dict:
        return {
            "case_dir": self.case_dir.as_posix(),
            "solver": self.solver,
            "steady": self.steady,
            "fluid": self.fluid.to_dict(),
            "patches": {k: v.to_dict() for k, v in self.patches.items()},
            "files": sorted(self.files),
            "momentum_transport_file": self.momentum_transport_file,
            "foam_version": self.foam_version,
            "functions": sorted(self.functions),
            "n_cores": self.n_cores,
            "warnings": self.warnings,
            "unresolved": self.unresolved,
            "digest": self.digest,
        }


def build_case(
    case_dir: Path | str,
    *,
    fluid: Fluid,
    patches: Mapping[str, PatchBC],
    solver: str = "simpleFoam",
    steady: bool | None = None,
    end_time: float | None = None,
    delta_t: float | None = None,
    write_interval: float | None = None,
    n_cores: int = 1,
    decompose_method: str = "scotch",
    functions: Mapping[str, Any] | None = None,
    scalars: Sequence[str] = (),
    schemes: Mapping[str, Any] | None = None,
    solution: Mapping[str, Any] | None = None,
    control: Mapping[str, Any] | None = None,
    foam_version: str | None = None,
) -> CaseBuild:
    """Write a complete laminar incompressible case, minus the mesh.

    Writes `constant/transportProperties`, `constant/<momentum transport file>`,
    `system/{controlDict,fvSchemes,fvSolution,decomposeParDict}` and `0/{U,p}`. It does NOT
    touch `constant/polyMesh` - `mesh.py` owns that and may run before or after this.

    `schemes`, `solution` and `control` replace the generated dictionary wholesale when
    given, so a sibling that needs `interFoam` or `scalarTransportFoam` can reuse
    everything else.
    """
    case_dir = Path(case_dir)
    steady = (solver in ("simpleFoam", "potentialFoam")) if steady is None else steady
    warnings = validate_patches(patches)
    patches = dict(patches)

    if end_time is None:
        end_time = 2000 if steady else 1.0
    if delta_t is None:
        delta_t = 1 if steady else 1e-3
    if write_interval is None:
        write_interval = 500 if steady else max(end_time / 20.0, delta_t)

    mt_file = laminar_model_file(foam_version)
    functions = dict(functions) if functions is not None else standard_functions(patches)
    # A function object that SOLVES a field needs that field in fvSchemes and fvSolution.
    # `age` is the one here, and forgetting it kills the solver on its first iteration
    # with "Entry 'div(phi,age)' not found" - after the mesh and the decomposition. So the
    # scalar list is derived from the function objects rather than left to the caller to
    # remember.
    scalars = list(dict.fromkeys(list(scalars) + solved_fields(functions)))

    files: list[str] = []

    def _w(rel: str, body: Mapping[str, Any], class_name: str = "dictionary", **kw) -> None:
        write_dict(case_dir / rel, body, class_name, object_name=Path(rel).name,
                   location=Path(rel).parent.as_posix(), **kw)
        files.append(rel)

    _w("constant/transportProperties", transport_properties(fluid),
       note=f"{fluid.name}: {fluid.source}")
    _w(f"constant/{mt_file}", momentum_transport(foam_version),
       note="laminar: Re << 1 in every chip channel (assay2chip/cfd/README.md)")
    _w("system/controlDict",
       dict(control) if control is not None else control_dict(
           application=solver, end_time=end_time, delta_t=delta_t,
           write_interval=write_interval, functions=functions))
    _w("system/fvSchemes",
       dict(schemes) if schemes is not None else fv_schemes(steady=steady, scalars=scalars))
    _w("system/fvSolution",
       dict(solution) if solution is not None else fv_solution(steady=steady, scalars=scalars))
    _w("system/decomposeParDict", decompose_par_dict(max(int(n_cores), 1), decompose_method))
    _w("0/U", field_u(patches), class_name="volVectorField")
    _w("0/p", field_p(patches, fluid), class_name="volScalarField")

    unresolved: list[dict] = []
    if fluid.assumption:
        unresolved.append({
            "kind": "assumed_property",
            "key": fluid.contract_key or DENSITY_KEY,
            "message": (
                f"the OpenFOAM case in {case_dir.name} uses nu = {fluid.nu_m2_s:.4g} m2/s for "
                f"'{fluid.name}' ({fluid.fluid_class}), which is an ASSUMPTION, not a "
                f"contract value: {fluid.source}. {fluid.why}".strip()
            ),
            "wanted_in": "contracts/parameters.json",
        })
        warnings.append(f"fluid '{fluid.name}' is an assumed property: {fluid.source}")

    return CaseBuild(
        case_dir=case_dir,
        solver=solver,
        fluid=fluid,
        patches=patches,
        files=files,
        momentum_transport_file=mt_file,
        foam_version=foam_version,
        functions=list(functions),
        warnings=warnings,
        unresolved=unresolved,
        steady=steady,
        n_cores=max(int(n_cores), 1),
    )


# --- WHERE A CASE LIVES: one convention, used by every module ----------------
#
# This existed in four places and they did not agree, which cost a real OpenFOAM job:
# `mesh.py` was handed `foam/<chip>/1/` while `flow.py` wrote its case to
# `foam/foam/<chip>/step_1/`, so the mesh and the dictionaries landed in different
# directories and `checkMesh` failed on an empty one. The doubled `foam/` came from
# joining a run directory that was already called `foam` to a literal `foam`; the
# different step name came from one module writing `<step>` and another `step_<N>`.
#
# So there is now exactly ONE answer, here, and mesh, flow, transport, particles, vof and
# render all ask for it rather than building a path:
#
#     <out>/<chip_id>/step_<N>/            the case, with its mesh at constant/polyMesh
#     <out> = assay2chip/work/<run>/foam   by default
#
# `out_dir()` is idempotent about the trailing `foam`, so passing it either a run
# directory or an already-resolved output directory gives the same answer and the doubled
# path cannot come back.

#: The run name `assay2chip/work/<run>/foam` falls back to.
DEFAULT_RUN = "foam"

#: A case directory for a whole chip rather than one step (`run.py --no-step`).
ALL_STEPS = "all"


def out_dir(run: str = DEFAULT_RUN, *, work: Path | str | None = None) -> Path:
    """`assay2chip/work/<run>/foam` - the directory every case of one run lives under.

    THE DOUBLED PATH IS MADE UNREPRESENTABLE HERE, not merely avoided. Whatever a caller
    hands over - a run name, a run directory, an output directory, or an output directory
    that has already had `foam` appended twice by somebody else's string join - collapses
    onto the same place:

        out_dir()                               -> .../work/foam
        out_dir("e2e")                          -> .../work/e2e/foam
        out_dir(work=".../work/e2e")            -> .../work/e2e/foam
        out_dir(work=".../work/e2e/foam")       -> .../work/e2e/foam
        out_dir(work=".../work/foam/foam")      -> .../work/foam

    The last two lines are the point. `<out>` is `work/<run>/foam`, so the DEFAULT run -
    which is itself called `foam` - would read `work/foam/foam`, and a caller that
    appends `foam` to a directory already called `foam` lands in the same place. Both are
    the bug that put a mesh in `foam/<chip>/1/` and its case in `foam/foam/<chip>/step_1/`
    and failed `checkMesh` on the empty one. A run that genuinely wants a nested `foam`
    directory cannot have one, and that is the right trade.
    """
    base = Path(work) if work is not None else (ROOT / "assay2chip" / "work" / run)
    if base.name != "foam":
        base = base / "foam"
    while base.name == "foam" and base.parent.name == "foam":
        base = base.parent
    return base


def case_dir(out: Path | str | None, chip_id: str, step: Any = None) -> Path:
    """THE case directory for one chip and one protocol step.

        case_dir(None, "spc_demo_v1", 1)        -> .../work/foam/spc_demo_v1/step_1
        case_dir(out, "module1_v1", "4b")       -> <out>/module1_v1/step_4b
        case_dir(out, "module1_v1", None)       -> <out>/module1_v1/all

    The mesh goes INSIDE it, at `constant/polyMesh` - `mesh.write_polymesh(chip, case_dir)`
    - and the dictionaries beside it in `system/`, `constant/` and `0/`. Nothing may write
    a mesh for a step into a different directory from that step's case.

    `out` may be the output directory, the run directory, or None; see `out_dir`.
    """
    base = out_dir(work=out) if out is not None else out_dir()
    return base / _safe_name(chip_id) / step_name(step)


def step_name(step: Any = None) -> str:
    """`step_<N>` for a step, `all` for a whole-chip case.

    `render.py` already looks for `step_<N>` first, so this is that spelling and not a
    new one. A step id out of a protocol can be `4b` or `s7`, so it is used as written
    rather than coerced to an int.
    """
    if step is None or step == "" or step == ALL_STEPS:
        return ALL_STEPS
    text = str(step)
    return text if text.startswith("step_") else f"step_{_safe_name(text)}"


def _safe_name(text: str) -> str:
    """A path component: no separators, no surprises."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(text)).strip("_")
    if not cleaned or cleaned in (".", ".."):
        raise CaseError(f"{text!r} is not usable as a directory name")
    return cleaned


# --- blockMesh, for tests and simple ducts -----------------------------------

def block_mesh_dict(
    vertices: Sequence[Sequence[float]],
    blocks: Sequence[str],
    boundary: Sequence[tuple[str, Mapping[str, Any]]],
    *,
    scale: float = 1.0,
    edges: Sequence[Any] = (),
    merge_patch_pairs: Sequence[Any] = (),
) -> dict:
    """`system/blockMeshDict`. `mesh.py` writes real chips; this is for ducts and tests."""
    return {
        "scale": scale,
        "vertices": Table([tuple(float(c) for c in v) for v in vertices]),
        "blocks": Table([Verbatim(b) for b in blocks]),
        "edges": Table(list(edges)),
        "boundary": NamedBlocks(list(boundary)),
        "mergePatchPairs": Table(list(merge_patch_pairs)),
    }


def duct_block_mesh(
    length_m: float,
    width_m: float,
    height_m: float,
    *,
    nx: int = 60,
    ny: int = 16,
    nz: int = 16,
    inlet: str = "inlet",
    outlet: str = "outlet",
    walls: str = "walls",
) -> dict:
    """A straight rectangular duct along x: the benchmark the analytic law is exact for.

    Flow enters at x = 0 and leaves at x = L; the four long faces are one `walls` patch.
    `R = 12 mu L / (w h^3 (1 - 0.63 h/w))` holds for this geometry (and only once the flow
    is fully developed), which is why the integration test uses it.
    """
    half_w, half_h = width_m / 2.0, height_m / 2.0
    v = [
        (0.0, -half_w, -half_h), (length_m, -half_w, -half_h),
        (length_m, half_w, -half_h), (0.0, half_w, -half_h),
        (0.0, -half_w, half_h), (length_m, -half_w, half_h),
        (length_m, half_w, half_h), (0.0, half_w, half_h),
    ]
    return block_mesh_dict(
        v,
        [f"hex (0 1 2 3 4 5 6 7) ({nx} {ny} {nz}) simpleGrading (1 1 1)"],
        [
            (inlet, {"type": "patch", "faces": Table([Verbatim("(0 4 7 3)")])}),
            (outlet, {"type": "patch", "faces": Table([Verbatim("(1 2 6 5)")])}),
            (walls, {"type": "wall", "faces": Table([
                Verbatim("(0 1 5 4)"), Verbatim("(3 7 6 2)"),
                Verbatim("(0 3 2 1)"), Verbatim("(4 5 6 7)"),
            ])}),
        ],
    )


def duct_resistance_pa_s_m3(mu_pa_s: float, length_m: float, width_m: float, height_m: float) -> float:
    """R = 12 mu L / (w h^3 (1 - 0.63 h/w)), the shallow rectangular duct law.

    The same expression `operator/flow_calc.py` and `assay2chip/cfd` use, stated once more
    here so the OpenFOAM test has a reference that does not import the model it validates.
    h is the SHORT side.

    This is the standard ENGINEERING APPROXIMATION, not the exact solution: the 0.63 term
    truncates a Fourier series and is about 0.3% low at h/w = 0.6 and a few percent off as
    the duct approaches square. `duct_resistance_exact_pa_s_m3` is the series itself, and
    the integration test checks OpenFOAM against THAT - comparing one approximation with
    another would hide a real error in either.
    """
    h, w = sorted((float(width_m), float(height_m)))
    return 12.0 * float(mu_pa_s) * float(length_m) / (w * h ** 3 * (1.0 - 0.63 * h / w))


def duct_resistance_exact_pa_s_m3(
    mu_pa_s: float, length_m: float, width_m: float, height_m: float, *, terms: int = 200
) -> float:
    """The EXACT rectangular-duct resistance: the full Fourier series, not the 0.63 fit.

    For fully developed flow in a duct of half-width a (long side) and half-height b,

        Q = (4 G a b^3) / (3 mu) * [ 1 - (192 b) / (pi^5 a)
                                       * sum_{n = 1,3,5,...} tanh(n pi a / 2b) / n^5 ]

    with G = dP/dx. R = G L / Q. The series converges geometrically - 200 odd terms is far
    past double precision - so this is exact to the last bit, and it is what the OpenFOAM
    integration test is held to.
    """
    b, a = (x / 2.0 for x in sorted((float(width_m), float(height_m))))
    mu, length = float(mu_pa_s), float(length_m)
    series = 0.0
    for n in range(1, 2 * int(terms), 2):
        series += math.tanh(n * math.pi * a / (2.0 * b)) / n ** 5
    shape = 1.0 - (192.0 * b) / (math.pi ** 5 * a) * series
    # Q = G * (4 a b^3 / (3 mu)) * shape, and R = G L / Q
    return 3.0 * mu * length / (4.0 * a * b ** 3 * shape)


def duct_pressure_drop_pa(
    q_m3_s: float, mu_pa_s: float, length_m: float, width_m: float, height_m: float
) -> float:
    """The exact fully-developed pressure drop along a rectangular duct, Pa."""
    return float(q_m3_s) * duct_resistance_exact_pa_s_m3(mu_pa_s, length_m, width_m, height_m)


def main(argv: Sequence[str]) -> int:
    """python -m assay2chip.foam.case - print a demo case to stdout, write nothing."""
    import json

    fluid = fluid_for(37.0)
    patches = {
        "inlet": PatchBC("feed", q_m3_s=0.05 / 6e10),
        "outlet": PatchBC("vent"),
        "walls": PatchBC("wall"),
    }
    print(dumps(transport_properties(fluid), "dictionary", "transportProperties", "constant"))
    print(dumps(field_u(patches), "volVectorField", "U", "0"))
    print(json.dumps({"fluid": fluid.to_dict(),
                      "momentum_transport_file": laminar_model_file(),
                      "warnings": validate_patches(patches)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""assay2chip/foam/flow.py - the 3D flow solve for one chip and one protocol step.

    python assay2chip/foam/flow.py --chip module1_v1 --step 1
    python assay2chip/foam/flow.py --chip module3_v1_translated --all-flowing
    from assay2chip.foam import flow
    summary = flow.run_step(chip_path, scenario_record)      # needs OpenFOAM
    summary = flow.summarise(raw_fields, plan)               # pure, needs nothing

One step in, one summary JSON out (`schema: foam-flow/1`), plus `Finding`s in
`assay2chip/cfd/model.py`'s shape tagged `source: "openfoam"`. The pipeline is the one
`assay2chip/foam/README.md` fixes:

    cfd/scenario.py  -> Scenario           what the step does to every port
    foam/mesh.py     -> constant/polyMesh  structured hex, one patch per port, cellZones
    foam/case.py     -> system/constant/0  dictionaries, laminar, BCs, function objects
    foam/runner.py   -> the solver          THE ONLY launcher; parallel; logs
    flow.py          -> this summary        + the cross-check in validate_foam.py

WHICH SOLVER, AND WHY - the decision is physics, not taste.

Momentum reaches steady state in a shallow duct in the viscous diffusion time across the
depth, `t_visc = h^2 / nu`: 0.11 s for a 0.3 mm etch in water. So a step whose boundary
conditions hold for far longer than that, at Re << 1, is genuinely steady and
`simpleFoam` is the right solver - a transient run would spend its whole budget
re-deriving a field that stopped changing in the first tenth of a second.

`pimpleFoam` is used when one of three things is true, and the reason is recorded in
`plan.transient_reason` so nobody has to guess why a case is transient:

  * **Re > 1.** Inertia is then part of the answer, and the steady SIMPLE loop can still
    converge to a field whose stability nobody checked. Module 6's quench flood runs
    Re 13-23 and the droplet nozzle Re ~9 (`assay2chip/cfd/README.md`); module3's capsule
    load reaches Re ~5 in the reduced model, so it is transient here too.
  * **the step is short next to `t_visc`** - the field never settles inside the step.
  * **the step names a transient event**: a flood, a quench, a prime, a droplet or an
    encapsulation. Those are transient by construction, whatever Re says.

A steady run that reports Re > 1 from its OWN field is not trusted: `run_step` escalates
it to `pimpleFoam` and re-runs, and the summary keeps both (`escalated_from`).

UNITS, AND THE ONE MULTIPLICATION EVERYONE FORGETS. OpenFOAM's incompressible solvers
divide through by density: `p` is kinematic pressure in m^2/s^2 and `wallShearStress` is
kinematic shear, also m^2/s^2, NOT Pa. Every number this module reads out of a case is
therefore multiplied by `rho` from the contracts - `assay2chip.cfd.solve.density()`, which
says whether that density is a contract value or an assumption - and the summary records
`rho_kg_m3`, `rho_source` and `kinematic_inputs: true` so the conversion is auditable
rather than invisible. Shear is reported in BOTH Pa and dyn/cm^2 (x10), because the
contract ceilings are in dyn/cm^2 and the solver is in Pa.

WHAT A CHAMBER HOLDS IS NOT DECIDED HERE. The three shear ceilings are not
interchangeable - 0.01 dyn/cm^2 for ADHERENT CELLS only, 0.3 for a rinse immediately
before a deliberate lyse, capsules bounded by escape pressure instead - and this repo has
scoped them wrongly at least six times. So `assay2chip.cfd.analyse.shear_scope()` is
IMPORTED and called, never reimplemented, and its `ScopeCall.evidence` travels into the
finding's `audit`. A 3D solver that invented its own ceilings would just be a seventh
wrong answer with better streamlines.

HOW THE NUMBERS COME OUT OF THE CASE. Two independent routes, so a missing function
object degrades instead of failing:

  * `postProcessing/<name>/<time>/<file>.dat` from `surfaceFieldValue` (patch flow and
    pressure), `fieldMinMax` (global extrema) and `volFieldValue` (per-cellZone extrema).
    `read_dat` parses those files, including `(x y z)` vector columns.
  * the written fields and `constant/polyMesh` themselves. Per-cellZone MAX WALL SHEAR has
    to come this way: the README gives the mesh ONE `walls` patch, so no patch-wise
    function object can separate the culture chamber's walls from the waste line's. The
    boundary field of `wallShearStress` is read face by face and each wall face is
    attributed to the cellZone of its OWNER cell (`polyMesh/owner`, `polyMesh/boundary`,
    `polyMesh/cellZones`). That is exact, needs nothing from the case dictionaries, and is
    testable offline on a hand-written four-cell mesh. If mesh.py grows per-zone wall
    patches (`walls_<feature>`) they are used instead and `wall_shear_source` says so.

Every parser and every post-processing function in this module is PURE: it takes paths or
dataclasses and returns dataclasses or dicts. `tests/test_foam_flow.py` exercises all of
them on synthetic fields with no OpenFOAM installed, which is what the README requires.

SI internally; `mm`, `uL/min`, `Pa`, `kPa`, `dyn/cm^2` at the JSON boundary with the unit
in the key name. Contract values come from `assay2chip.lib.contracts` and are never
written here.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.cfd import analyse as cfd_analyse  # noqa: E402
from assay2chip.cfd import mesh as cfd_mesh  # noqa: E402
from assay2chip.cfd import scenario as cfd_scenario  # noqa: E402
from assay2chip.cfd import solve as cfd_solve  # noqa: E402
from assay2chip.cfd.model import (  # noqa: E402
    DYN_CM2_PER_PA,
    M3_S_TO_UL_MIN,
    Field,
    Finding,
    Mesh,
)
from assay2chip.lib import contracts  # noqa: E402

SCHEMA = "foam-flow/1"

#: Above this Reynolds number inertia is part of the answer and the steady SIMPLE loop is
#: no longer the right question. Same limit `assay2chip/cfd/solve.py` uses to declare the
#: reduced model out of validity, deliberately: the two models must disagree about the
#: same edges.
RE_STEADY_LIMIT = 1.0

#: A step is long enough to be steady when it lasts this many viscous diffusion times
#: `h^2 / nu`. Ten is ~e^-10 of the slowest decaying mode, i.e. settled to 5e-5.
VISC_SETTLE_FACTOR = 10.0

#: The steady solvers, so a forced solver can be recognised as steady or not.
STEADY_SOLVERS = ("simpleFoam", "potentialFoam")

#: Transient by construction, whatever Re says.
TRANSIENT_TERMS = (
    "flood", "quench", "prime", "priming", "droplet", "encapsulat", "emulsif",
    "back-flush", "backflush", "back flush", "purge", "burst", "shock",
)

#: The patch-flow sign convention. OpenFOAM's `phi` on a boundary is positive OUT of the
#: domain, while `Field.meta["port_flow_ul_min"]` and `PortState.q_ul_min` are positive
#: INTO the chip. One minus sign, in one place.
PHI_IS_OUTWARD = True

#: Roache's two-grid safety factor for the Grid Convergence Index. 1.25 is for three
#: grids with an OBSERVED order; with two grids the order has to be assumed, and 3.0 is
#: the conservative factor that assumption earns.
GCI_SAFETY_TWO_GRID = 3.0
#: Formal order of a second-order finite-volume scheme on a structured hex mesh.
ASSUMED_ORDER = 2.0

DEFAULT_WORK = ROOT / "assay2chip" / "work"


class FlowError(RuntimeError):
    """A case cannot be built, run or read, and saying so beats returning a number."""


# ======================================================================================
# the sibling stack: mesh.py (F2), case.py and runner.py (F1)
# ======================================================================================


@dataclass
class FoamStack:
    """`foam/mesh.py`, `foam/case.py` and `foam/runner.py`, imported for real.

    Those three are written in parallel with this file, so a half-built sibling has to be
    a SKIPPED run with a reason, never a crash and never a silent fake number. Same
    pattern as `assay2chip.cfd.validate.discover_cfd`.

    The entry points this module calls. `case.py` and `runner.py` have landed and are
    called by their real signatures:

        case.fluid_from_spec(record.fluid) / case.fluid_for(temp_c) -> case.Fluid
        case.patches_from_scenario(record)                          -> {patch: PatchBC}
        case.standard_functions(patches, write_interval=)           -> function objects
        case.build_case(case_dir, fluid=, patches=, solver=, end_time=, delta_t=,
                        write_interval=, n_cores=, functions=)      -> case.CaseBuild
        runner.run(case_dir, command, parallel=, check=False)       -> runner.RunResult
        runner.available() / runner.has_solver(name)                -> bool

    `mesh.py` (F2) is not here yet, so its entry point is still probed by name and
    `bound["mesh"]` records which one answered:

        mesh.write_mesh(chip, case_dir, spacing_mm=, z_cells=) -> info
            writes `case_dir/constant/polyMesh`: structured hex, one patch per port named
            by port id plus `walls`, cellZones named by feature id. `info` should carry
            `cells`, `patches` {port_id: patch_name} and `zones` {feature_id: zone_name}.

    TODO(F2): replace the probing in `build_case` with the agreed signature once
    `assay2chip/foam/mesh.py` lands. It is the only sibling call left that guesses.
    """

    mesh: Any = None
    case: Any = None
    runner: Any = None
    reason: str = ""
    bound: Dict[str, str] = dc_field(default_factory=dict)

    @property
    def available(self) -> bool:
        return None not in (self.mesh, self.case, self.runner)

    def openfoam_available(self) -> bool:
        """Does the runner say OpenFOAM itself is installed? Absent runner -> no."""
        if self.runner is None:
            return False
        for name in ("available", "is_available", "have_openfoam", "openfoam_available"):
            fn = getattr(self.runner, name, None)
            if callable(fn):
                try:
                    return bool(fn())
                except Exception:
                    return False
        return True

    def entry(self, module: str, *names: str):
        """The first callable among `names` on one sibling module, or None."""
        mod = getattr(self, module, None)
        if mod is None:
            return None
        for name in names:
            fn = getattr(mod, name, None)
            if callable(fn):
                self.bound[module] = name
                return fn
        return None

    def as_json(self) -> dict:
        return {
            "available": self.available,
            "openfoam": self.openfoam_available(),
            "reason": self.reason,
            "bound": dict(self.bound),
            "modules": {
                name: getattr(getattr(self, name), "__file__", None)
                for name in ("mesh", "case", "runner")
                if getattr(self, name) is not None
            },
        }


def discover_foam() -> FoamStack:
    """Import the three siblings, or report exactly which one is not there yet."""
    stack = FoamStack()
    missing: List[str] = []
    for name in ("mesh", "case", "runner"):
        try:
            setattr(stack, name, __import__(f"assay2chip.foam.{name}", fromlist=["*"]))
        except Exception as exc:
            missing.append(f"assay2chip/foam/{name}.py: {type(exc).__name__}: {exc}")
    if missing:
        stack.reason = "; ".join(missing)
    return stack


# ======================================================================================
# OpenFOAM ASCII readers - pure, offline, tested on synthetic files
# ======================================================================================

_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_COMMENT_LINE = re.compile(r"//[^\n]*")


def strip_comments(text: str) -> str:
    """OpenFOAM files are C++-commented, including the banner. Remove both forms."""
    return _COMMENT_LINE.sub("", _COMMENT_BLOCK.sub("", text))


@dataclass
class DatTable:
    """One `postProcessing/.../*.dat` file: the header's column names and the rows.

    A function object appends a row per write, so the LAST row is the converged (steady)
    or final (transient) value and `last()` is what a summary wants. A vector column
    `(x y z)` becomes three columns `name_x`, `name_y`, `name_z` plus a magnitude
    `name_mag`, because that is the number every check here actually asks for.

    A cell may be a STRING: `fieldMinMax` writes one row per field with the field's NAME
    in a column, so `U` and `p` arrive in the same file and `select("field", "U")` is the
    only way to tell them apart. Coercing that to a float would silently misalign every
    column after it.
    """

    columns: List[str] = dc_field(default_factory=list)
    rows: List[List[Any]] = dc_field(default_factory=list)
    path: str = ""
    #: The `# Key : value` lines a `surfaceFieldValue` writes above its header - `Faces`,
    #: `Area`, `Scale factor`, `Region type`. The patch AREA comes from here and nowhere
    #: else, and it is what turns a flow rate into a mean velocity.
    meta: Dict[str, str] = dc_field(default_factory=dict)
    #: Where each `(x y z)` group began in the RAW row, before the magnitudes were
    #: inserted. `group_anchor` needs these; nothing else should.
    vector_starts: List[int] = dc_field(default_factory=list)

    def meta_float(self, key: str) -> Optional[float]:
        try:
            return float(self.meta[key])
        except (KeyError, ValueError):
            return None

    def group_anchor(self, n: int) -> Optional[float]:
        """The scalar immediately before the n-th `(x y z)` group of the final row.

        This exists because `fieldMinMax` writes

            Time  field  min  location(min)  [processor]  max  location(max)  [processor]

        and the `processor` column EXISTS ONLY IN A PARALLEL RUN and is not named in the
        header. Looking up the column called `max` therefore returns the processor id of
        the minimum on any parallel case - a silent, plausible-looking wrong answer. The
        scalar immediately before each location group is the min and the max whatever the
        layout, so that is what is read.
        """
        if n >= len(self.vector_starts):
            return None
        raw = self.vector_starts[n] - 1
        if raw < 0:
            return None
        # Each group that STARTS before `raw` has had one magnitude inserted ahead of it.
        shift = sum(1 for s in self.vector_starts if s < raw)
        row = self.rows[-1] if self.rows else []
        index = raw + shift
        value = row[index] if 0 <= index < len(row) else None
        return value if isinstance(value, float) else None

    def column(self, name: str) -> List[Any]:
        try:
            i = self.columns.index(name)
        except ValueError:
            raise FlowError(
                f"{self.path or 'dat'}: no column {name!r}; it has {', '.join(self.columns)}"
            ) from None
        return [row[i] if i < len(row) else None for row in self.rows]

    def has(self, name: str) -> bool:
        return name in self.columns

    def select(self, column: str, value: str) -> "DatTable":
        """The rows whose `column` holds `value` - how one field is picked out of
        `fieldMinMax.dat`, which interleaves a row per field per write."""
        if column not in self.columns:
            return DatTable(columns=list(self.columns), rows=[], path=self.path,
                            meta=dict(self.meta), vector_starts=list(self.vector_starts))
        i = self.columns.index(column)
        rows = [r for r in self.rows if i < len(r) and str(r[i]) == value]
        return DatTable(columns=list(self.columns), rows=rows, path=self.path,
                        meta=dict(self.meta), vector_starts=list(self.vector_starts))

    def last(self) -> Dict[str, Any]:
        """The final row as {column: value}. Empty dict when the file has no data."""
        if not self.rows:
            return {}
        return dict(zip(self.columns, self.rows[-1]))

    def value(self, *names: str, default: Optional[float] = None) -> Optional[float]:
        """The final NUMERIC value of the first column that exists, by any of its names.

        Function-object column names drift between OpenFOAM versions
        (`sum(phi)` / `sum(phi)_inlet`, `max` / `max(mag(U))`), so a reader that insists
        on one spelling is a reader that breaks on the next release. Exact names are tried
        before substring matches so `max` never beats `max(mag(U))` by accident.
        """
        row = self.last()
        for name in names:
            if isinstance(row.get(name), float):
                return row[name]
        for name in names:
            for col, val in row.items():
                if isinstance(val, float) and (col.startswith(name) or name in col):
                    return val
        return default


def _tokenise_header(line: str) -> List[str]:
    """Split a `#`-header line into column names, keeping `(...)` groups together.

    `# Time sum(phi) areaAverage(p)` -> `['Time', 'sum(phi)', 'areaAverage(p)']`.
    Splitting on whitespace alone would shred every function object's own names.
    """
    out: List[str] = []
    depth = 0
    token = ""
    for ch in line:
        if ch == "(":
            depth += 1
            token += ch
        elif ch == ")":
            depth = max(0, depth - 1)
            token += ch
        elif ch.isspace() and depth == 0:
            if token:
                out.append(token)
                token = ""
        else:
            token += ch
    if token:
        out.append(token)
    return out


def _parse_row(line: str) -> Tuple[List[Any], List[int]]:
    """A data row to floats, keeping non-numeric tokens as strings.

    Returns (values, indices that started a `(x y z)` group). The group indices let the
    caller expand a vector column's name into `_x _y _z _mag` without re-parsing.

    THE PARENTHESES MEAN TWO DIFFERENT THINGS AND THEY HAVE TO BE TOLD APART. In

        50   mag(U)   0   (0.0158 0.0027 0.0003)   0.000547   (0.0174 0.0082 0.00025)

    the ones in `mag(U)` are part of a LABEL and the others delimit a vector. Splitting on
    `(` and `)` turns the field name into three tokens and shifts every column after it,
    which is how a reader ends up quoting a position as a velocity. A token whose
    parentheses are BALANCED and which does not open with one is therefore a label and is
    kept whole; only the unbalanced tokens - `(0.0158` and `0.0003)` - delimit a group.
    """
    values: List[Any] = []
    starts: List[int] = []
    depth = 0
    for token in line.split():
        if not token.startswith("(") and token.count("(") == token.count(")"):
            try:
                values.append(float(token))
            except ValueError:
                values.append(token)
            continue
        opens = 0
        while token.startswith("("):
            opens += 1
            token = token[1:]
        closes = 0
        while token.endswith(")"):
            closes += 1
            token = token[:-1]
        if opens:
            if depth == 0:
                starts.append(len(values))
            depth += opens
        if token:
            try:
                values.append(float(token))
            except ValueError:
                values.append(token)
        if closes:
            depth = max(0, depth - closes)
    return values, starts


def parse_dat(text: str, path: str = "") -> DatTable:
    """Parse the text of an OpenFOAM function-object `.dat` file."""
    header: List[str] = []
    rows: List[List[Any]] = []
    vector_starts: List[int] = []
    meta: Dict[str, str] = {}
    seen_data = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            body = line.lstrip("#").strip()
            tokens = _tokenise_header(body)
            if tokens and tokens[0].lower().startswith("time"):
                header = tokens
            elif ":" in body:
                key, _, value = body.partition(":")
                meta[key.strip()] = value.strip()
            continue
        values, starts = _parse_row(line)
        if not values:
            continue
        if not seen_data:
            vector_starts = starts
            seen_data = True
        rows.append(_expand_row(values, vector_starts))
    width = max((len(r) for r in rows), default=len(header))
    return DatTable(columns=_columns_for(header, vector_starts, width), rows=rows,
                    path=path, meta=meta, vector_starts=list(vector_starts))


def _columns_for(header: Sequence[str], vector_starts: Sequence[int],
                 width: int) -> List[str]:
    """Column names for one expanded row.

    `vector_starts` index the RAW row, so each name that lands on one consumes three raw
    columns and produces four: `_x`, `_y`, `_z` and the `_mag` that `_expand_row` inserted.
    """
    names: List[str] = []
    raw = 0
    starts = set(vector_starts)
    for name in header:
        if raw in starts:
            names.extend([f"{name}_x", f"{name}_y", f"{name}_z", f"{name}_mag"])
            raw += 3
            continue
        names.append(name)
        raw += 1
    while len(names) < width:
        names.append(f"col{len(names)}")
    return names


def _expand_row(values: List[Any], vector_starts: Sequence[int]) -> List[Any]:
    """Insert `|v|` after each `(x y z)` group so a magnitude is always available."""
    if not vector_starts:
        return values
    out: List[Any] = []
    i = 0
    starts = set(vector_starts)
    while i < len(values):
        if i in starts and i + 2 < len(values):
            x, y, z = values[i], values[i + 1], values[i + 2]
            mag = (math.sqrt(x * x + y * y + z * z)
                   if all(isinstance(c, float) for c in (x, y, z)) else math.nan)
            out.extend([x, y, z, mag])
            i += 3
            continue
        out.append(values[i])
        i += 1
    return out


def read_dat(path: Path) -> DatTable:
    return parse_dat(Path(path).read_text(encoding="utf-8", errors="replace"), str(path))


_DAT_SUFFIX = re.compile(r"_\d+$")


def find_dat_files(case_dir: Path) -> Dict[str, Path]:
    """`postProcessing/<name>/<time>/<file>.dat` -> {"<name>/<file>": the NEWEST path}.

    Two kinds of duplicate have to be resolved here, and getting either wrong means
    quoting a previous run's numbers as this one's - which is worse than reporting
    nothing, because it looks fine.

      * SEVERAL TIME DIRECTORIES, from a restart or from the steady -> transient
        escalation. The highest time wins.
      * `name.dat` AND `name_0.dat` IN THE SAME DIRECTORY. OpenFOAM does not overwrite an
        existing function-object file: it writes the new run to `name_0.dat` and leaves
        the old `name.dat` in place. So the suffixed file is the NEWER one, which is the
        opposite of what the names suggest. They are grouped under one key and the newest
        modification time wins.

    `run_step` also cleans a case before re-running it, so this is the second line of
    defence rather than the first.
    """
    root = Path(case_dir) / "postProcessing"
    out: Dict[str, Path] = {}
    if not root.is_dir():
        return out
    for dat in sorted(root.rglob("*.dat")):
        rel = dat.relative_to(root)
        if len(rel.parts) < 2:
            continue
        key = f"{rel.parts[0]}/{_DAT_SUFFIX.sub('', dat.stem)}"
        best = out.get(key)
        if best is None or _freshness(dat) > _freshness(best):
            out[key] = dat
    return out


def _freshness(dat: Path) -> Tuple[float, float]:
    try:
        mtime = dat.stat().st_mtime
    except OSError:
        mtime = -math.inf
    return (_time_of(dat.parent), mtime)


def _time_of(directory: Path) -> float:
    try:
        return float(directory.name)
    except ValueError:
        return -math.inf


# --- polyMesh and field files ---------------------------------------------------------


def _body_after_count(text: str) -> str:
    """The `( ... )` body of an OpenFOAM list, skipping the FoamFile dict and the count."""
    text = strip_comments(text)
    # drop the FoamFile header dictionary
    head = text.find("FoamFile")
    if head >= 0:
        brace = text.find("{", head)
        if brace >= 0:
            depth = 0
            for i in range(brace, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        text = text[i + 1:]
                        break
    start = text.find("(")
    end = text.rfind(")")
    if start < 0 or end < start:
        return ""
    return text[start + 1:end]


def read_label_list(path: Path) -> List[int]:
    """`polyMesh/owner` or `neighbour`: a list of labels, one per face."""
    body = _body_after_count(Path(path).read_text(encoding="utf-8", errors="replace"))
    return [int(tok) for tok in body.split() if _is_int(tok)]


def _is_int(token: str) -> bool:
    try:
        int(token)
        return True
    except ValueError:
        return False


def read_boundary(path: Path) -> Dict[str, Dict[str, Any]]:
    """`polyMesh/boundary` -> {patch: {type, nFaces, startFace}}.

    Deliberately a small hand-written parser rather than a general OpenFOAM dictionary
    reader: this is the only dictionary flow.py has to understand, and a 40-line parser
    that is obviously right beats a general one that is not.
    """
    text = strip_comments(Path(path).read_text(encoding="utf-8", errors="replace"))
    body = _body_after_count(text)
    out: Dict[str, Dict[str, Any]] = {}
    i = 0
    while i < len(body):
        match = re.compile(r"([A-Za-z_][\w.\-]*)\s*\{").search(body, i)
        if match is None:
            break
        name = match.group(1)
        depth = 1
        j = match.end()
        while j < len(body) and depth:
            if body[j] == "{":
                depth += 1
            elif body[j] == "}":
                depth -= 1
            j += 1
        inner = body[match.end():j - 1]
        entry: Dict[str, Any] = {}
        for key, value in re.findall(r"(\w+)\s+([^;{}]+);", inner):
            value = value.strip()
            entry[key] = int(value) if _is_int(value) else value
        out[name] = {
            "type": str(entry.get("type", "")),
            "nFaces": int(entry.get("nFaces", 0) or 0),
            "startFace": int(entry.get("startFace", 0) or 0),
        }
        i = j
    return out


def read_cell_zones(path: Path) -> Dict[str, List[int]]:
    """`polyMesh/cellZones` -> {zone name: cell labels}. Missing file -> {}."""
    path = Path(path)
    if not path.exists():
        return {}
    text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
    body = _body_after_count(text)
    out: Dict[str, List[int]] = {}
    i = 0
    while i < len(body):
        match = re.compile(r"([A-Za-z_][\w.\-]*)\s*\{").search(body, i)
        if match is None:
            break
        name = match.group(1)
        depth = 1
        j = match.end()
        while j < len(body) and depth:
            if body[j] == "{":
                depth += 1
            elif body[j] == "}":
                depth -= 1
            j += 1
        inner = body[match.end():j - 1]
        start = inner.find("(")
        end = inner.rfind(")")
        cells: List[int] = []
        if 0 <= start < end:
            cells = [int(t) for t in inner[start + 1:end].split() if _is_int(t)]
        out[name] = cells
        i = j
    return out


@dataclass
class VolField:
    """A written volume field: the internal values and the per-patch boundary values.

    `components` is 1 for a volScalarField and 3 for a volVectorField; the values are
    stored flat and `mag()` folds a vector field to magnitudes. A `uniform` entry is
    expanded to the right length only when the length is known from `boundary`, and
    otherwise kept as a single value - which is enough for a max.
    """

    name: str = ""
    components: int = 1
    internal: List[float] = dc_field(default_factory=list)
    boundary: Dict[str, List[float]] = dc_field(default_factory=dict)

    def mag(self, values: Sequence[float]) -> List[float]:
        if self.components == 1:
            return [abs(v) for v in values]
        out = []
        for i in range(0, len(values) - self.components + 1, self.components):
            chunk = values[i:i + self.components]
            out.append(math.sqrt(sum(c * c for c in chunk)))
        return out

    def patch_mag(self, patch: str) -> List[float]:
        return self.mag(self.boundary.get(patch, []))

    def face_count(self, patch: str) -> int:
        vals = self.boundary.get(patch, [])
        return len(vals) // max(1, self.components)


_NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def _numbers(text: str) -> List[float]:
    return [float(m.group(0)) for m in _NUMBER.finditer(text)]


def parse_vol_field(text: str, name: str = "") -> VolField:
    """Parse an ASCII `volScalarField` / `volVectorField` into a `VolField`.

    Only the two forms OpenFOAM actually writes are handled - `nonuniform List<...>` and
    `uniform (...)`. A binary-format field is refused loudly: silently reading garbage out
    of one would be worse than not reading it.
    """
    text = strip_comments(text)
    if re.search(r"format\s+binary", text):
        raise FlowError(
            f"field {name or '?'} is in OpenFOAM BINARY format; set writeFormat ascii in "
            f"system/controlDict (case.py) so the summary can be read without a solver"
        )
    cls = "volScalarField"
    m = re.search(r"class\s+(\w+)", text)
    if m:
        cls = m.group(1)
    components = 3 if "Vector" in cls else (6 if "SymmTensor" in cls else 1)
    out = VolField(name=name or "", components=components)

    internal = re.search(r"internalField\s+([^;]*?);", text, re.S)
    if internal:
        out.internal = _field_values(internal.group(1), components)

    bstart = text.find("boundaryField")
    if bstart >= 0:
        brace = text.find("{", bstart)
        body, _ = _balanced(text, brace)
        i = 0
        while i < len(body):
            match = re.compile(r"([A-Za-z_][\w.\-]*)\s*\{").search(body, i)
            if match is None:
                break
            patch = match.group(1)
            inner, j = _balanced(body, match.end() - 1)
            value = re.search(r"\bvalue\s+([^;]*?);", inner, re.S)
            if value is None:
                value = re.search(r"\b(?:uniformValue|gradient)\s+([^;]*?);", inner, re.S)
            out.boundary[patch] = _field_values(value.group(1), components) if value else []
            i = j
    return out


def _balanced(text: str, brace_index: int) -> Tuple[str, int]:
    """The text inside the `{...}` starting at `brace_index`, and the index past it."""
    if brace_index < 0 or brace_index >= len(text) or text[brace_index] != "{":
        return "", len(text)
    depth = 0
    for i in range(brace_index, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[brace_index + 1:i], i + 1
    return text[brace_index + 1:], len(text)


def _field_values(blob: str, components: int) -> List[float]:
    blob = blob.strip()
    if blob.startswith("uniform"):
        return _numbers(blob[len("uniform"):])
    start = blob.find("(")
    end = blob.rfind(")")
    if 0 <= start < end:
        return _numbers(blob[start + 1:end])
    return _numbers(blob)


def read_vol_field(path: Path) -> VolField:
    path = Path(path)
    return parse_vol_field(path.read_text(encoding="utf-8", errors="replace"), path.name)


def latest_time_dir(case_dir: Path) -> Optional[Path]:
    """The highest numeric time directory in a case, or None."""
    case_dir = Path(case_dir)
    times = []
    for child in case_dir.iterdir() if case_dir.is_dir() else []:
        if not child.is_dir():
            continue
        try:
            times.append((float(child.name), child))
        except ValueError:
            continue
    if not times:
        return None
    return max(times, key=lambda t: t[0])[1]


def zone_internal_max(
    case_dir: Path,
    field_name: str,
    *,
    time_dir: Optional[Path] = None,
    zones: Optional[Mapping[str, Sequence[int]]] = None,
) -> Dict[str, float]:
    """Max |field| over the CELLS of each cellZone, from the written internal field.

    This is how a per-zone Reynolds number becomes a measurement instead of a bound.
    `case.standard_functions` writes `fieldMinMax` over the whole chip and nothing per
    zone - there is no `volFieldValue` per cellZone - so the alternative would be to give
    every zone the chip-wide maximum |U|, which turns the quietest chamber's Re into the
    busiest channel's. The internal field is indexed by cell and `polyMesh/cellZones`
    lists each zone's cells, so the real per-zone maximum is one pass over both.
    """
    case_dir = Path(case_dir)
    time_dir = time_dir or latest_time_dir(case_dir)
    if time_dir is None:
        return {}
    path = time_dir / field_name
    if not path.exists():
        return {}
    vf = read_vol_field(path)
    values = vf.mag(vf.internal)
    if not values:
        return {}
    if zones is None:
        zones = read_cell_zones(case_dir / "constant" / "polyMesh" / "cellZones")
    out: Dict[str, float] = {}
    for zone, cells in (zones or {}).items():
        best = 0.0
        seen = False
        for cell in cells:
            if 0 <= cell < len(values):
                best = max(best, values[cell])
                seen = True
        if seen:
            out[zone] = best
    out["__chip__"] = max(values)
    return out


def wall_shear_by_zone(
    case_dir: Path,
    *,
    time_dir: Optional[Path] = None,
    wall_patches: Optional[Sequence[str]] = None,
) -> Tuple[Dict[str, float], str, List[str]]:
    """Max |wallShearStress| per cellZone, in OpenFOAM's KINEMATIC units (m^2/s^2).

    Returns (per-zone max, how it was obtained, notes).

    The mesh has one `walls` patch (`assay2chip/foam/README.md`), so the only way to tell
    the culture chamber's walls from the waste line's is to look at which cell owns each
    wall face: `polyMesh/owner[startFace + i]` for the i-th face of the patch, then
    `polyMesh/cellZones` for that cell's zone. Exact, and it needs nothing from the case
    dictionaries - which is why it also works on a case built by a future case.py.

    When mesh.py names its wall patches per feature (`walls_<feature_id>`), the patch name
    is used directly and the owner lookup is skipped: `how` says which happened.
    """
    case_dir = Path(case_dir)
    notes: List[str] = []
    time_dir = time_dir or latest_time_dir(case_dir)
    if time_dir is None:
        raise FlowError(f"{case_dir}: no time directory, so the solver wrote no fields")
    field_path = time_dir / "wallShearStress"
    if not field_path.exists():
        raise FlowError(
            f"{field_path} is missing: add the `wallShearStress` function object with "
            f"`writeFields true` in case.py, or shear cannot be reported at all"
        )
    wss = read_vol_field(field_path)
    poly = case_dir / "constant" / "polyMesh"
    boundary = read_boundary(poly / "boundary") if (poly / "boundary").exists() else {}
    patches = list(wall_patches or [p for p in wss.boundary if p.startswith("walls")]) or \
        [p for p, info in boundary.items() if info["type"] in ("wall", "patch")
         and p.startswith("walls")]

    per_zone_patches = [p for p in patches if p != "walls" and p.startswith("walls_")]
    out: Dict[str, float] = {}
    if per_zone_patches:
        for patch in per_zone_patches:
            zone = patch[len("walls_"):]
            values = wss.patch_mag(patch)
            if values:
                out[zone] = max(out.get(zone, 0.0), max(values))
        return out, "per_zone_wall_patches", notes

    if "walls" not in wss.boundary:
        raise FlowError(
            f"{field_path} has no `walls` patch (it has "
            f"{', '.join(sorted(wss.boundary)) or 'nothing'}): mesh.py must name the wall "
            f"patch `walls` or `walls_<feature_id>`"
        )
    owner = read_label_list(poly / "owner") if (poly / "owner").exists() else []
    zones = read_cell_zones(poly / "cellZones")
    if not owner or not zones or "walls" not in boundary:
        notes.append(
            "no polyMesh/owner, cellZones or boundary entry for `walls`, so wall shear "
            "could only be resolved for the WHOLE chip, not per feature"
        )
        values = wss.patch_mag("walls")
        return ({"__chip__": max(values)} if values else {}), "chip_global_only", notes

    zone_of: Dict[int, str] = {}
    for zone, cells in zones.items():
        for cell in cells:
            zone_of[cell] = zone
    start = int(boundary["walls"]["startFace"])
    values = wss.patch_mag("walls")
    unzoned = 0
    for i, value in enumerate(values):
        face = start + i
        if face >= len(owner):
            unzoned += 1
            continue
        zone = zone_of.get(owner[face])
        if zone is None:
            unzoned += 1
            continue
        out[zone] = max(out.get(zone, 0.0), value)
    if values:
        out["__chip__"] = max(values)
    if unzoned:
        notes.append(
            f"{unzoned} of {len(values)} wall faces belong to no cellZone, so their shear "
            f"is in the chip-wide maximum but in no feature's: mesh.py should put every "
            f"cell in a zone named by feature id"
        )
    return out, "owner_cell_to_cellzone", notes


# ======================================================================================
# the samples a summary is built from - the offline-testable boundary
# ======================================================================================


@dataclass
class PatchSample:
    """One boundary patch, as the function objects report it.

    `flow_m3_s` is positive INTO the chip (see `PHI_IS_OUTWARD`). `p_*_kinematic` are
    m^2/s^2 straight out of an incompressible solver and become Pa only through `rho`.
    """

    port: str
    patch: str = ""
    role: str = ""                       # feed | vent | cap | wall
    area_m2: float = 0.0
    flow_m3_s: float = 0.0
    p_mean_kinematic: float = 0.0
    p_max_kinematic: float = 0.0
    u_mean_m_s: float = 0.0

    def p_mean_pa(self, rho: float) -> float:
        return self.p_mean_kinematic * rho

    def p_max_pa(self, rho: float) -> float:
        return self.p_max_kinematic * rho

    @property
    def flow_ul_min(self) -> float:
        return self.flow_m3_s * M3_S_TO_UL_MIN


@dataclass
class ZoneSample:
    """One cellZone - i.e. one chip feature - as the function objects report it.

    `wall_shear_max_kinematic` is `max |wallShearStress|` over the wall faces owned by
    this zone, in m^2/s^2: the README's warning in numbers, because an incompressible
    solver's wallShearStress is tau/rho and reporting it as Pa under-reads by 1000x.

    `width_m` / `depth_m` are the zone's NARROWEST flow cross-section, taken from the
    reduced model's mesh (the repo's authoritative geometry), and they are what the local
    hydraulic diameter and therefore the local Reynolds number are built from.
    """

    zone: str
    cells: int = 0
    volume_m3: float = 0.0
    p_max_kinematic: float = 0.0
    p_min_kinematic: float = 0.0
    u_max_m_s: float = 0.0
    wall_shear_max_kinematic: float = math.nan
    wall_shear_source: str = ""
    width_m: float = math.nan
    depth_m: float = math.nan

    @property
    def hydraulic_diameter_m(self) -> float:
        if not (self.width_m > 0 and self.depth_m > 0):
            return math.nan
        return cfd_solve.hydraulic_diameter_m(self.width_m, self.depth_m)

    def wall_shear_pa(self, rho: float) -> float:
        return self.wall_shear_max_kinematic * rho

    def reynolds(self, rho: float, mu: float) -> float:
        d_h = self.hydraulic_diameter_m
        if not (d_h > 0) or not (mu > 0):
            return math.nan
        return rho * abs(self.u_max_m_s) * d_h / mu


@dataclass
class RawFields:
    """Everything read out of one finished case, before any physics is applied.

    Deliberately a plain container: `summarise()` takes one of these and nothing else
    from the filesystem, so every post-processing rule in this module is testable on a
    hand-written `RawFields` with no OpenFOAM anywhere. `kinematic` records the thing
    that is easiest to get wrong - whether `p` and `wallShearStress` still have to be
    multiplied by density.
    """

    patches: List[PatchSample] = dc_field(default_factory=list)
    zones: List[ZoneSample] = dc_field(default_factory=list)
    kinematic: bool = True
    p_max_kinematic: float = math.nan
    u_max_m_s: float = math.nan
    wall_shear_max_kinematic: float = math.nan
    wall_shear_source: str = ""
    final_time_s: Optional[float] = None
    converged: Optional[bool] = None
    initial_residuals: Dict[str, float] = dc_field(default_factory=dict)
    cells: int = 0
    notes: List[str] = dc_field(default_factory=list)

    def patch(self, port: str) -> Optional[PatchSample]:
        for p in self.patches:
            if p.port == port or p.patch == port:
                return p
        return None

    def zone(self, name: str) -> Optional[ZoneSample]:
        for z in self.zones:
            if z.zone == name:
                return z
        return None


# ======================================================================================
# post-processing: pure functions on RawFields
# ======================================================================================


def mass_balance(raw: RawFields) -> dict:
    """Flow in, flow out and the imbalance across every patch, in uL/min.

    A patch-wise mass balance is the one check that catches a bad mesh, a bad BC and a
    half-converged solve at once, so it is reported on every run whether or not it is
    good. `closure` is |in - out| / in: 0 is perfect.
    """
    q_in = sum(p.flow_m3_s for p in raw.patches if p.flow_m3_s > 0)
    q_out = -sum(p.flow_m3_s for p in raw.patches if p.flow_m3_s < 0)
    net = sum(p.flow_m3_s for p in raw.patches)
    return {
        "q_in_ul_min": q_in * M3_S_TO_UL_MIN,
        "q_out_ul_min": q_out * M3_S_TO_UL_MIN,
        "net_ul_min": net * M3_S_TO_UL_MIN,
        "imbalance_ul_min": abs(net) * M3_S_TO_UL_MIN,
        "closure": (abs(q_in - q_out) / q_in) if q_in > 0 else None,
        "per_patch_ul_min": {p.port: p.flow_ul_min for p in sorted(raw.patches,
                                                                   key=lambda s: s.port)},
    }


def outlet_split(raw: RawFields) -> dict:
    """What fraction of the outflow leaves through each outlet patch.

    Only patches that actually carry flow OUT are counted, so a vent that happens to be
    drawing in (which is a finding in itself, not a split) does not dilute the fractions.
    """
    out = {p.port: -p.flow_m3_s for p in raw.patches if p.flow_m3_s < 0}
    total = sum(out.values())
    return {
        "total_out_ul_min": total * M3_S_TO_UL_MIN,
        "fraction": {k: (v / total if total > 0 else None) for k, v in sorted(out.items())},
        "ul_min": {k: v * M3_S_TO_UL_MIN for k, v in sorted(out.items())},
    }


def pressure_summary(raw: RawFields, rho: float, *, feeds: Sequence[str] = (),
                     vents: Sequence[str] = ()) -> dict:
    """Peak pressure and the drop from each feed to each vent, in Pa and kPa.

    Peak pressure is taken from the whole field (`fieldMinMax`) when it is there and from
    the patches otherwise, and `peak_source` says which: a peak read only from the ports
    misses a pressure maximum inside a narrow lane, which is exactly where the bond fails.
    """
    peak = raw.p_max_kinematic
    source = "fieldMinMax(p)"
    if not (isinstance(peak, float) and math.isfinite(peak)):
        candidates = [p.p_max_kinematic for p in raw.patches
                      if math.isfinite(p.p_max_kinematic)]
        peak = max(candidates) if candidates else math.nan
        source = "patch maxima only (no fieldMinMax(p) in the case)"
    drops: Dict[str, float] = {}
    for feed in feeds:
        fp = raw.patch(feed)
        if fp is None:
            continue
        for vent in vents:
            vp = raw.patch(vent)
            if vp is None:
                continue
            drops[f"{feed}->{vent}"] = (fp.p_mean_kinematic - vp.p_mean_kinematic) * rho
    return {
        "peak_pa": peak * rho if math.isfinite(peak) else None,
        "peak_kpa": peak * rho / 1000.0 if math.isfinite(peak) else None,
        "peak_source": source,
        "drop_pa": drops,
        "drop_kpa": {k: v / 1000.0 for k, v in drops.items()},
        "patch_mean_pa": {p.port: p.p_mean_pa(rho) for p in sorted(raw.patches,
                                                                   key=lambda s: s.port)},
    }


def zone_shear(raw: RawFields, rho: float) -> dict:
    """Max wall shear per cellZone in Pa AND dyn/cm^2 (x10), plus the chip-wide max.

    The multiplication by `rho` is the whole point of this function: `wallShearStress` in
    an incompressible solver is tau/rho, so reporting it raw would under-read by three
    orders of magnitude and every shear finding would silently disappear.
    """
    per_zone: Dict[str, dict] = {}
    for z in sorted(raw.zones, key=lambda s: s.zone):
        if not math.isfinite(z.wall_shear_max_kinematic):
            continue
        pa = z.wall_shear_pa(rho)
        per_zone[z.zone] = {
            "max_pa": pa,
            "max_dyn_cm2": pa * DYN_CM2_PER_PA,
            "source": z.wall_shear_source or raw.wall_shear_source,
        }
    chip_pa = (raw.wall_shear_max_kinematic * rho
               if math.isfinite(raw.wall_shear_max_kinematic) else None)
    if chip_pa is None and per_zone:
        chip_pa = max(v["max_pa"] for v in per_zone.values())
    return {
        "per_zone": per_zone,
        "chip_max_pa": chip_pa,
        "chip_max_dyn_cm2": chip_pa * DYN_CM2_PER_PA if chip_pa is not None else None,
        "kinematic_to_pa": rho,
        "note": (
            "wallShearStress from an incompressible OpenFOAM solver is KINEMATIC "
            "(tau/rho, m^2/s^2); every value here is that number times the density "
            "below, then x10 for dyn/cm^2"
        ),
    }


def zone_reynolds(raw: RawFields, rho: float, mu: float) -> dict:
    """Max Re per zone from the zone's own max |U| and its local hydraulic diameter."""
    out: Dict[str, dict] = {}
    for z in sorted(raw.zones, key=lambda s: s.zone):
        re = z.reynolds(rho, mu)
        out[z.zone] = {
            "re_max": re if math.isfinite(re) else None,
            "u_max_mm_s": z.u_max_m_s * 1e3,
            "hydraulic_diameter_mm": (z.hydraulic_diameter_m * 1e3
                                      if math.isfinite(z.hydraulic_diameter_m) else None),
            "width_mm": z.width_m * 1e3 if math.isfinite(z.width_m) else None,
            "depth_mm": z.depth_m * 1e3 if math.isfinite(z.depth_m) else None,
        }
    values = [v["re_max"] for v in out.values() if v["re_max"] is not None]
    return {
        "per_zone": out,
        "chip_max": max(values) if values else None,
        "over_limit": sorted((z for z, v in out.items()
                              if v["re_max"] is not None and v["re_max"] > RE_STEADY_LIMIT),
                             key=lambda z: -(out[z]["re_max"] or 0.0)),
        "limit": RE_STEADY_LIMIT,
    }


# ======================================================================================
# the plan: which solver, at what resolution, driven how
# ======================================================================================


@dataclass
class CasePlan:
    """Everything decided before a case is written, with the reason for each decision."""

    chip_id: str
    step: Any
    solver: str = "simpleFoam"
    transient: bool = False
    transient_reason: str = ""
    steady_reason: str = ""
    #: Non-empty when a caller overrode the solver choice, with the consequence spelled
    #: out. It travels into the summary so no reader mistakes a forced steady field at
    #: Re > 1 for the full answer.
    solver_forced_reason: str = ""
    end_time_s: float = 0.0
    write_interval_s: float = 0.0
    #: What a caller ASKED for; `None` means mesh.py chooses, which is the default and the
    #: only way the README's "6 cells across the narrowest channel" rule stays in one
    #: place. `spacing_mm_actual` is what mesh.py actually used.
    spacing_mm: Optional[float] = None
    spacing_mm_actual: Optional[float] = None
    #: mesh.py's own measurements, so nothing here forms a second opinion about what the
    #: narrowest channel is or how many cells crossed it.
    min_channel_mm: Optional[float] = None
    narrowest_feature: str = ""
    cells_across_narrowest: Optional[float] = None
    z_cells_actual: Optional[int] = None
    mesh_cells: Optional[int] = None
    zones: List[str] = dc_field(default_factory=list)
    mesh_unresolved: List[dict] = dc_field(default_factory=list)
    z_cells: int = 8
    n_proc: int = 1
    feeds: List[str] = dc_field(default_factory=list)
    vents: List[str] = dc_field(default_factory=list)
    capped: List[str] = dc_field(default_factory=list)
    q_in_ul_min: float = 0.0
    mu_pa_s: float = 0.0
    rho_kg_m3: float = 0.0
    nu_m2_s: float = 0.0
    mu_source: str = ""
    rho_source: str = ""
    rho_is_assumption: bool = False
    re_estimate: Optional[float] = None
    re_source: str = ""
    t_visc_s: Optional[float] = None
    duration_s: Optional[float] = None
    depth_mm: Optional[float] = None
    case_dir: str = ""
    assumptions: List[str] = dc_field(default_factory=list)

    def to_json(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def _narrowest_cross_section_m(mesh: Mesh, feature_id: str) -> Tuple[float, float]:
    """(width, depth) in m of the tightest flow cross-section in one feature.

    Re is largest where the area is smallest, so the tightest cross-section is the one a
    per-zone Reynolds number has to be built from. Ducts, mouths and slits carry a real
    width and depth; a chamber meshed only as Hele-Shaw cells has none, so its in-plane
    width is taken as the square root of its footprint area - a characteristic width, not
    a measurement, and flagged as such wherever it is used.
    """
    best: Optional[Tuple[float, float, float]] = None
    for edge in mesh.edges_of(feature_id):
        if edge.kind == "hs_face":
            continue
        w, h = cfd_solve.edge_cross_section_m(edge)
        if not (w > 0 and h > 0):
            continue
        area = w * h
        if best is None or area < best[0]:
            best = (area, w, h)
    if best is not None:
        return best[1], best[2]
    cells = mesh.cells_of(feature_id)
    if cells:
        area_mm2 = sum(n.area_mm2 for n in cells)
        depth_mm = max((n.depth_mm for n in cells), default=0.0)
        if area_mm2 > 0 and depth_mm > 0:
            return math.sqrt(area_mm2) * 1e-3, depth_mm * 1e-3
    return math.nan, math.nan


def _cells_across(mesh: Optional[Mesh], plan: CasePlan) -> Optional[float]:
    """How many cells the hex mesh puts across the narrowest channel.

    The README's floor is 6. Below it a staircased wall is both narrower and rougher than
    the real one, which is the first thing the cross-validation blames a disagreement on.
    """
    spacing = plan.spacing_mm_actual or plan.spacing_mm
    if not spacing:
        return None
    narrowest = plan.min_channel_mm
    if narrowest is None:
        if mesh is None:
            return None
        narrowest = _min_channel_mm(mesh)
    return round(narrowest / spacing, 2) if narrowest > 0 else None


def _chip_depth_mm(mesh: Mesh) -> float:
    depths = [n.depth_mm for n in mesh.nodes if n.depth_mm > 0]
    return max(depths) if depths else 0.0


def _min_channel_mm(mesh: Mesh) -> float:
    """The narrowest REAL channel in the reduced model's mesh, mm.

    `duct` only, deliberately. A `mouth` edge's width is the span of the raster cells the
    opening happens to cover, so it tracks `grid_mm` rather than the part - on module1_v1
    it reads 0.1 mm where the narrowest channel the chip actually has is the 0.2 mm
    `ch_in_n`. Counting it would halve every "cells across the narrowest channel" figure
    and make the cross-validation blame a staircase that is not there. `hs_face` is
    excluded for the same reason, and a `slit` is a weir gap in z, which is counted
    separately because it is resolved by the z grading and not by the xy spacing.

    `build_case` prefers mesh.py's own `xy.narrowest_feature_mm` over this whenever the
    3D mesher has reported it; this is the fallback, and the two agree on ducts.
    """
    widths = [e.width_mm for e in mesh.edges if e.kind == "duct" and e.width_mm > 0]
    return min(widths) if widths else 0.0


def transient_reason(record: Any, re_estimate: Optional[float], t_visc_s: Optional[float],
                     duration_s: Optional[float]) -> str:
    """Why this step must be run transient, or "" when steady is the right physics."""
    blob = " ".join(
        str(x) for x in (
            getattr(record, "operation", "") or "",
            getattr(record, "note", "") or "",
            getattr(getattr(record, "fluid", None), "name", "") or "",
            getattr(record, "exchange_kind", "") or "",
            " ".join(getattr(record, "targets", ()) or ()),
        )
    ).lower()
    hit = next((t for t in TRANSIENT_TERMS if t in blob), "")
    if hit:
        return (f"the step names a transient event ({hit!r}): a flood, quench, prime or "
                f"droplet formation is transient by construction")
    if re_estimate is not None and re_estimate > RE_STEADY_LIMIT:
        return (f"Re = {re_estimate:.3g} > {RE_STEADY_LIMIT:g}: inertia is part of the "
                f"answer, so a steady SIMPLE loop would converge to a field whose "
                f"stability nobody checked")
    if (duration_s is not None and t_visc_s is not None
            and duration_s < VISC_SETTLE_FACTOR * t_visc_s):
        return (f"the step lasts {duration_s:.3g} s, under {VISC_SETTLE_FACTOR:g} x the "
                f"viscous settling time h^2/nu = {t_visc_s:.3g} s, so the field never "
                f"reaches the steady state simpleFoam would solve for")
    return ""


def plan_case(
    chip: Mapping,
    record: Any,
    mesh: Mesh,
    *,
    spacing_mm: Optional[float] = None,
    z_cells: int = 8,
    n_proc: int = 1,
    reduced: Optional[Field] = None,
    work_dir: Optional[Path] = None,
    run_name: str = "foam",
    force_solver: str = "",
) -> CasePlan:
    """Decide the solver, the end time and the fluid for one step, with reasons.

    `reduced` is the reduced-order `Field` for the same step when it is available: its
    `re` is the cheapest honest pre-estimate of whether this step is inertial, and using
    it means the choice of solver is made from physics rather than from the step's name.
    It is a PRE-estimate only - `run_step` re-checks Re against the 3D field and escalates
    a steady run that turns out to be inertial.
    """
    scenario = record.scenario if hasattr(record, "scenario") else record
    # The reduced model's own fluid resolution, which knows how to read a `Scenario` and
    # flags an assumed density. The OpenFOAM case is built from `case.Fluid` instead
    # (`build_case` below), and `_check_fluid_agreement` refuses to run if the two ever
    # disagree - a cross-validation where the two models ran different liquids would be
    # worthless, and that is exactly the kind of mismatch nobody notices.
    fluid = cfd_solve.fluid_for(scenario)
    nu = fluid.mu_pa_s / fluid.rho_kg_m3
    depth_mm = _chip_depth_mm(mesh)
    t_visc = ((depth_mm * 1e-3) ** 2 / nu) if (depth_mm > 0 and nu > 0) else None

    re_est, re_src = None, ""
    if reduced is not None and reduced.re:
        re_est, re_src = max(reduced.re.values()), "reduced-order solver (cfd/solve.py)"

    duration = getattr(record, "duration_s", None)
    reason = transient_reason(record, re_est, t_visc, duration)
    forced = ""
    if force_solver:
        steady_forced = force_solver in STEADY_SOLVERS
        if steady_forced and reason:
            forced = (
                f"the caller forced {force_solver}, overriding the transient choice "
                f"({reason}). The field this produces is the STEADY LIMIT of an inertial "
                f"step: its pressure drop and outlet split are the right numbers to "
                f"cross-validate against a steady reduced-order solver, and its wall shear "
                f"is NOT the whole answer - a transient run is still required before any "
                f"shear or stability finding from this step is believed."
            )
        elif not steady_forced and not reason:
            forced = (f"the caller forced {force_solver} on a step this module judged "
                      f"steady ({reason or 'no inertial or transient evidence'})")
        else:
            forced = f"the caller forced {force_solver}, which is what would have been chosen anyway"
    chip_id = str(chip.get("id") or mesh.chip_id or "chip")
    step = getattr(record, "step", None)
    if step is None:
        step = getattr(scenario, "step", None)

    feeds = sorted(k for k, v in getattr(scenario, "ports", {}).items() if v.is_flow)
    vents = sorted(k for k, v in getattr(scenario, "ports", {}).items() if v.is_pressure)
    capped = sorted(k for k, v in getattr(scenario, "ports", {}).items() if v.is_closed)

    # How long a transient run has to be: long enough to settle momentum AND to move a
    # chip volume, so the field that is reported is the one the step actually runs in.
    q_in = float(getattr(record, "q_in_ul_min", 0.0) or 0.0)
    flush_s = (mesh.volume_ul / q_in * 60.0) if q_in > 0 else None
    end_time = 0.0
    write_interval = 0.0
    if reason:
        wanted = [v for v in (
            (VISC_SETTLE_FACTOR * t_visc) if t_visc else None,
            flush_s,
        ) if v]
        end_time = min(duration, max(wanted)) if (duration and wanted) else (
            max(wanted) if wanted else (duration or 1.0))
        end_time = float(f"{end_time:.3g}")
        write_interval = end_time / 20.0

    work = Path(work_dir) if work_dir else (DEFAULT_WORK / run_name)
    case_dir = work / "foam" / chip_id / f"step_{step}"

    assumptions = list(fluid.notes)
    if fluid.rho_is_assumption:
        assumptions.append(
            f"density {fluid.rho_kg_m3} kg/m^3 is an assumption ({fluid.rho_source}), and "
            f"EVERY pressure and shear here is linear in it because OpenFOAM's "
            f"incompressible p and wallShearStress are kinematic"
        )
    return CasePlan(
        chip_id=chip_id,
        step=step,
        solver=force_solver or ("pimpleFoam" if reason else "simpleFoam"),
        transient=(force_solver not in STEADY_SOLVERS if force_solver else bool(reason)),
        transient_reason=reason,
        solver_forced_reason=forced,
        steady_reason=(
            "" if reason else
            (f"Re = {re_est:.3g} << 1 and the step holds for "
             f"{duration if duration is not None else 'longer than'} s against a viscous "
             f"settling time h^2/nu = {t_visc:.3g} s, so momentum is steady"
             if (re_est is not None and t_visc) else
             "no inertial or transient evidence in the step; steady laminar")
        ),
        end_time_s=end_time,
        write_interval_s=write_interval,
        spacing_mm=spacing_mm,
        z_cells=z_cells,
        n_proc=n_proc,
        feeds=feeds,
        vents=vents,
        capped=capped,
        q_in_ul_min=q_in,
        mu_pa_s=fluid.mu_pa_s,
        rho_kg_m3=fluid.rho_kg_m3,
        nu_m2_s=nu,
        mu_source=fluid.mu_source,
        rho_source=fluid.rho_source,
        rho_is_assumption=fluid.rho_is_assumption,
        re_estimate=re_est,
        re_source=re_src,
        t_visc_s=t_visc,
        duration_s=duration,
        depth_mm=depth_mm or None,
        case_dir=str(case_dir),
        assumptions=assumptions,
    )


# ======================================================================================
# the summary
# ======================================================================================


def summarise(raw: RawFields, plan: CasePlan, *, mesh: Optional[Mesh] = None,
              extra: Optional[Mapping] = None) -> dict:
    """One finished case -> the summary JSON. Pure: no filesystem, no OpenFOAM.

    Everything the README asks a flow run to report: peak pressure and the feed -> vent
    drop, the outlet flow split, wall shear per cellZone in Pa and dyn/cm^2, max Re per
    zone from U and the local hydraulic diameter, and the patch mass balance.
    """
    rho = plan.rho_kg_m3
    mu = plan.mu_pa_s
    pressure = pressure_summary(raw, rho, feeds=plan.feeds, vents=plan.vents)
    balance = mass_balance(raw)
    shear = zone_shear(raw, rho)
    reynolds = zone_reynolds(raw, rho, mu)
    split = outlet_split(raw)

    # The prescribed inflow is the ground truth: if the solve did not deliver it, nothing
    # downstream is worth reading, and that is a numerical finding not a design one.
    delivered = balance["q_in_ul_min"]
    prescribed = plan.q_in_ul_min
    flow_error = (abs(delivered - prescribed) / prescribed) if prescribed else None

    out = {
        "schema": SCHEMA,
        "source": "openfoam",
        "chip_id": plan.chip_id,
        "step": plan.step,
        "solver": plan.solver,
        "transient": plan.transient,
        "transient_reason": plan.transient_reason,
        "steady_reason": plan.steady_reason,
        "solver_forced_reason": plan.solver_forced_reason,
        "case_dir": plan.case_dir,
        "ports": {
            "feed": list(plan.feeds),
            "vent": list(plan.vents),
            "cap": list(plan.capped),
            "prescribed_q_in_ul_min": prescribed,
        },
        "fluid": {
            "mu_pa_s": mu,
            "mu_source": plan.mu_source,
            "rho_kg_m3": rho,
            "rho_source": plan.rho_source,
            "rho_is_assumption": plan.rho_is_assumption,
            "nu_m2_s": plan.nu_m2_s,
            "kinematic_inputs": raw.kinematic,
        },
        "pressure": pressure,
        "flow": {
            "mass_balance": balance,
            "outlet_split": split,
            "delivered_vs_prescribed": flow_error,
        },
        "shear": shear,
        "reynolds": reynolds,
        "grid": {
            "cells": raw.cells or plan.mesh_cells,
            "spacing_mm_requested": plan.spacing_mm,
            "spacing_mm": plan.spacing_mm_actual or plan.spacing_mm,
            "spacing_chosen_by": ("mesh.py (6 cells across the narrowest channel, inside "
                                  "its cell budget)" if plan.spacing_mm is None
                                  else "the caller, for a resolution sweep"),
            "z_cells": plan.z_cells_actual or plan.z_cells,
            "z_cells_requested": plan.z_cells,
            "zones": list(plan.zones),
            "min_channel_mm": plan.min_channel_mm if plan.min_channel_mm is not None
            else (_min_channel_mm(mesh) if mesh is not None else None),
            "narrowest_feature": plan.narrowest_feature,
            "cells_across_narrowest": (plan.cells_across_narrowest
                                       if plan.cells_across_narrowest is not None
                                       else _cells_across(mesh, plan)),
            "measured_by": ("mesh.py" if plan.cells_across_narrowest is not None
                            else "flow.py from the reduced model's duct widths"),
        },
        "convergence": {
            "final_time_s": raw.final_time_s,
            "converged": raw.converged,
            "initial_residuals": dict(raw.initial_residuals),
        },
        "assumptions": list(plan.assumptions),
        "mesh_unresolved": list(plan.mesh_unresolved),
        "notes": list(raw.notes),
        "labels": ["SIMULATED"],
    }
    if extra:
        out.update(dict(extra))
    return out


# ======================================================================================
# findings
# ======================================================================================


def _fid(chip_id: str, step: Any, kind: str, where: str) -> str:
    return f"foam_{kind}_{chip_id}_{step}_{where}".replace(" ", "_")


def findings_for(
    summary: Mapping,
    chip: Optional[Mapping] = None,
    protocol: Optional[Mapping] = None,
    *,
    step_index: Optional[int] = None,
) -> List[Finding]:
    """Findings from one 3D flow summary, in `cfd/model.Finding`'s shape.

    `source` is `"openfoam"` throughout, so `analyse.py`, `fix.py` and the report consume
    them unchanged alongside the reduced model's own.

    SHEAR IS SCOPED BY `assay2chip.cfd.analyse.shear_scope`, IMPORTED. There are three
    ceilings, they are not interchangeable, and this repo has picked the wrong one at
    least six times: 0.01 dyn/cm^2 applies to ADHERENT CELLS only, 0.3 to a rinse
    immediately before a deliberate lyse, and a capsule chamber has no shear ceiling at
    all because it is bounded by escape pressure. Reimplementing any of that here would
    be the seventh mistake, so the scope call, its evidence and its `unknown` verdict all
    come from analyse.py and the evidence is carried into `Finding.audit`.
    """
    chip_id = str(summary.get("chip_id", ""))
    step = summary.get("step")
    out: List[Finding] = []

    # --- numerical quality first: nothing below is worth reading if this fails ---------
    balance = dict(summary.get("flow", {}).get("mass_balance", {}) or {})
    closure = balance.get("closure")
    delivered = summary.get("flow", {}).get("delivered_vs_prescribed")
    if closure is not None and closure > 1e-3:
        out.append(Finding(
            id=_fid(chip_id, step, "mass", "patches"),
            cls="unmodelled", severity="high", chip_id=chip_id, step=step,
            metric="patch_mass_closure",
            value=float(closure), limit=1e-3, units="fraction",
            message=(
                f"the 3D solve does not conserve mass across its patches: "
                f"{balance.get('q_in_ul_min', 0.0):.4g} uL/min in against "
                f"{balance.get('q_out_ul_min', 0.0):.4g} out, closure {closure:.3g}. "
                f"Every pressure, shear and Reynolds number from this case is suspect "
                f"until it closes."
            ),
            fix_hint="tighten the solver tolerances or check the outlet BC is p = 0 and not a wall",
            source="openfoam",
        ))
    if delivered is not None and delivered > 1e-3:
        out.append(Finding(
            id=_fid(chip_id, step, "inflow", "delivered"),
            cls="unmodelled", severity="high", chip_id=chip_id, step=step,
            metric="delivered_vs_prescribed_inflow",
            value=float(delivered), limit=1e-3, units="fraction",
            message=(
                f"the case delivered {balance.get('q_in_ul_min', 0.0):.4g} uL/min where the "
                f"step prescribes {summary.get('ports', {}).get('prescribed_q_in_ul_min')} "
                f"uL/min: the flowRateInletVelocity BC or the inlet patch area is wrong, "
                f"so the whole field is at the wrong flow rate."
            ),
            fix_hint="check case.py's flowRateInletVelocity volumetricFlowRate against the step",
            source="openfoam",
        ))
    if summary.get("convergence", {}).get("converged") is False:
        out.append(Finding(
            id=_fid(chip_id, step, "converge", "solver"),
            cls="unmodelled", severity="high", chip_id=chip_id, step=step,
            metric="solver_converged", value=0.0, limit=1.0, units="bool",
            message=(
                f"{summary.get('solver')} did not reach its convergence criteria on "
                f"{chip_id} step {step}; an unconverged field reported as a result is how "
                f"a CFD package lies."
            ),
            fix_hint="more iterations, a better initial field, or a finer mesh",
            source="openfoam",
        ))

    # --- Re > 1: the thing the reduced model could not do -----------------------------
    reynolds = dict(summary.get("reynolds", {}) or {})
    for zone in reynolds.get("over_limit", []) or []:
        rec = dict((reynolds.get("per_zone", {}) or {}).get(zone, {}) or {})
        value = rec.get("re_max")
        if value is None:
            continue
        out.append(Finding(
            id=_fid(chip_id, step, "re", zone),
            cls="unmodelled",
            severity=("high" if value > 10 * RE_STEADY_LIMIT else "medium"),
            chip_id=chip_id, feature_id=zone, step=step,
            metric="reynolds_vs_stokes_validity",
            value=float(value), limit=RE_STEADY_LIMIT, units="dimensionless",
            message=(
                f"Re = {value:.3g} in {zone} (U_max {rec.get('u_max_mm_s', 0.0):.3g} mm/s, "
                f"D_h {rec.get('hydraulic_diameter_mm')} mm): the reduced Stokes model in "
                f"assay2chip/cfd is outside its validity here and must not be trusted on "
                f"this feature. This 3D {summary.get('solver')} run is the number to use, "
                f"and it says the peak wall shear in {zone} is "
                f"{(summary.get('shear', {}).get('per_zone', {}).get(zone, {}) or {}).get('max_dyn_cm2')} "
                f"dyn/cm^2."
            ),
            fix_hint=(
                "nothing to fix if the design intends it - this finding replaces the "
                "reduced model's number rather than reporting a fault"
            ),
            source="openfoam",
            audit={"solver": summary.get("solver"),
                   "transient_reason": summary.get("transient_reason")},
        ))

    # --- shear, scoped by analyse.py ---------------------------------------------------
    per_zone = dict((summary.get("shear", {}) or {}).get("per_zone", {}) or {})
    if per_zone and chip is not None:
        chambers = cfd_analyse.chamber_views(chip)
        steps = cfd_analyse.step_views(protocol)
        view = _step_view_for(steps, step, step_index)
        capsule_chip, capsule_evidence = cfd_analyse.is_capsule_chip(chip, protocol)
        unknown_seen: set = set()
        for zone, rec in sorted(per_zone.items()):
            if zone.startswith("__"):
                continue
            value = rec.get("max_dyn_cm2")
            if value is None or view is None:
                continue
            chamber = chambers.get(zone)
            if chamber is None:
                chamber = next((chambers[t] for t in view.targets if t in chambers), None)
            if chamber is None:
                continue
            scope = cfd_analyse.shear_scope(
                chamber, view, steps,
                capsule_chip=capsule_chip, capsule_evidence=capsule_evidence,
            )
            if scope.is_unknown:
                if chamber.id in unknown_seen:
                    continue
                unknown_seen.add(chamber.id)
                out.append(Finding(
                    id=_fid(chip_id, step, "shearscope", chamber.id),
                    cls="shear", severity="medium", chip_id=chip_id,
                    feature_id=chamber.id, step=step,
                    metric="shear_scope_unknown",
                    value=float(value), limit=None, units="dyn/cm2",
                    message=(
                        f"the 3D solve measures a peak wall shear of {value:.3g} dyn/cm2 in "
                        f"{chamber.id} (step {step}) but cannot tell what the chamber holds, "
                        f"so no ceiling applies and the number is UNJUDGED. Looked at: "
                        f"{'; '.join(scope.evidence)}. The three ceilings are not "
                        f"interchangeable, so picking the strictest would invent a blocker "
                        f"and the loosest would hide one."
                    ),
                    fix_hint=(
                        "state `contents` (adherent_cells | capsules | none) on the chamber "
                        "in the chip spec, or name the reagent and operation in the protocol"
                    ),
                    source="openfoam",
                    audit={"scope_evidence": list(scope.evidence),
                           "shear_source": rec.get("source")},
                ))
                continue
            if scope.ceiling_dyn_cm2 is None:
                # capsules, or nothing shear-sensitive: pressure governs and the leak /
                # weir_escape checks own that. Reporting shear here would BE the scoping
                # error this module refuses to make.
                continue
            ratio = value / scope.ceiling_dyn_cm2 if scope.ceiling_dyn_cm2 else None
            if ratio is None or ratio < cfd_analyse.LADDER[-1][0]:
                continue
            estimated = str(rec.get("source", "")).startswith("estimate")
            out.append(Finding(
                id=_fid(chip_id, step, "shear", zone),
                cls="shear", severity=cfd_analyse.severity_for_ratio(ratio),
                chip_id=chip_id, feature_id=zone, step=step,
                metric=f"wall_shear_vs_{scope.shear_class}_ceiling",
                value=float(value), limit=float(scope.ceiling_dyn_cm2), units="dyn/cm2",
                message=(
                    f"3D wall shear {value:.3g} dyn/cm2 at {zone} in step {step} against the "
                    f"{scope.shear_class} ceiling {scope.ceiling_dyn_cm2:g} dyn/cm2, "
                    f"{ratio:.3g}x over. Measured on the resolved 3D field by "
                    f"{summary.get('solver')}, so it includes the side-wall peak the "
                    f"depth-averaged model cannot see. Scope: "
                    f"{'; '.join(scope.evidence[:2])}."
                ),
                fix_hint=(
                    f"wall shear is linear in Q: drop the step flow to "
                    f"{(summary.get('ports', {}).get('prescribed_q_in_ul_min') or 0.0) / ratio:.3g} "
                    f"uL/min, or widen the lane (tau goes as 1/w h^2)"
                ),
                source="openfoam",
                assumption=estimated,
                audit={"scope_evidence": list(scope.evidence),
                       "shear_source": rec.get("source"),
                       "max_pa": rec.get("max_pa"),
                       "solver": summary.get("solver")},
            ))

    # --- peak pressure against the published working limit -----------------------------
    peak_kpa = (summary.get("pressure", {}) or {}).get("peak_kpa")
    if peak_kpa is not None:
        try:
            limit = float(contracts.max_working_pressure_kpa())
        except Exception:
            limit = None
        if limit:
            ratio = peak_kpa / limit
            if ratio >= cfd_analyse.LADDER[-1][0]:
                out.append(Finding(
                    id=_fid(chip_id, step, "pressure", "peak"),
                    cls="leak", severity=cfd_analyse.severity_for_ratio(ratio),
                    chip_id=chip_id, step=step,
                    metric="peak_pressure_vs_max_working",
                    value=float(peak_kpa), limit=limit, units="kPa",
                    message=(
                        f"3D peak pressure {peak_kpa:.3g} kPa on {chip_id} step {step} "
                        f"against the contract's safe working pressure {limit:g} kPa "
                        f"({ratio:.3g}x). Read from {(summary.get('pressure') or {}).get('peak_source')}."
                    ),
                    fix_hint="drop the step flow, widen the tightest lane, or open a second vent",
                    source="openfoam",
                    assumption=bool((summary.get("fluid", {}) or {}).get("rho_is_assumption")),
                ))
    return out


def _step_view_for(steps: Sequence[Any], step: Any, step_index: Optional[int]) -> Optional[Any]:
    """The `analyse.StepView` this summary belongs to.

    `shear_scope` walks the protocol by `index`, so the view handed to it has to be the
    one from THIS protocol's list and not a lookalike - that is the bug that let a quench
    be judged against a culture ceiling.
    """
    if not steps:
        return None
    if step_index is not None and 0 <= step_index < len(steps):
        return steps[step_index]
    for view in steps:
        if view.label == step or str(view.label) == str(step):
            return view
    return None


# ======================================================================================
# orchestration: mesh -> case -> run -> read -> summarise
# ======================================================================================


def _call(fn, *args, **kwargs):
    """Call a sibling entry point, dropping keyword arguments it does not accept.

    The three siblings are being written right now, so their signatures are not frozen.
    Passing an unexpected keyword would turn "F1 has not added `z_cells` yet" into a
    crash; dropping it turns the same thing into a default, which is what a parallel
    build needs. Whatever was dropped is returned so the summary can say so.
    """
    import inspect
    dropped: List[str] = []
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(*args, **kwargs), dropped
    if any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()):
        return fn(*args, **kwargs), dropped
    allowed = {name for name, p in sig.parameters.items()
               if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    kept = {k: v for k, v in kwargs.items() if k in allowed}
    dropped = sorted(set(kwargs) - set(kept))
    return fn(*args, **kept), dropped


def build_case(chip: Mapping, record: Any, mesh: Mesh, plan: CasePlan,
               stack: Optional[FoamStack] = None) -> dict:
    """Write `polyMesh` (mesh.py) and the dictionaries (case.py) for one step.

    This function is the ONLY place that calls a sibling, and it does not launch anything:
    `runner.py` is the only launcher, per `assay2chip/foam/README.md`.
    """
    stack = stack or discover_foam()
    if not stack.available:
        raise FlowError(f"the foam stack is not complete: {stack.reason}")
    case_dir = Path(plan.case_dir)
    if case_dir.is_dir():
        # A re-used case directory is a trap: OpenFOAM does not overwrite an existing
        # function-object file, it writes `name_0.dat` beside it, and the old time
        # directories stay too. Clearing the OUTPUT (runner.clean leaves 0/, constant/ and
        # system/ alone) is the only way to be sure the summary is this run's.
        try:
            stack.runner.clean(case_dir, keep_zero=False)
        except Exception as exc:                       # pragma: no cover - best effort
            raise FlowError(
                f"could not clean the old case in {case_dir} before re-running it: {exc}. "
                f"Delete it by hand - reading a stale postProcessing file would report the "
                f"previous run's numbers as this one's."
            ) from exc
    case_dir.mkdir(parents=True, exist_ok=True)

    write_mesh = stack.entry("mesh", "write_polymesh", "write_mesh", "build_mesh", "build")
    if write_mesh is None:
        raise FlowError(
            "assay2chip/foam/mesh.py has no write_polymesh entry point: flow.py cannot "
            "write a polyMesh itself - that would be the fourth geometry implementation "
            "the README forbids"
        )
    # `spacing_mm = None` means "mesh.py decides", which is the right default: it owns the
    # README's rule of at least 6 cells across the narrowest channel inside a cell budget,
    # and second-guessing it here would either break that rule or duplicate it. A spacing
    # is passed only when a caller is deliberately sweeping resolution (the grid study).
    mesh_info, mesh_dropped = _call(
        write_mesh, chip, case_dir,
        dxy_mm=plan.spacing_mm, min_cells_z=plan.z_cells,
    )
    meta = dict(getattr(mesh_info, "meta", None) or {})
    xy = dict(meta.get("xy") or {})
    plan.spacing_mm_actual = xy.get("dxy_mm")
    # mesh.py measures the narrowest feature and the cells across it while it is choosing
    # the spacing, so those are ITS numbers and not a second opinion formed here.
    plan.min_channel_mm = xy.get("narrowest_feature_mm")
    plan.narrowest_feature = str(xy.get("narrowest_feature") or "")
    plan.cells_across_narrowest = xy.get("cells_across_narrowest")
    plan.z_cells_actual = (meta.get("z") or {}).get("cells")
    plan.mesh_cells = meta.get("cells")
    plan.zones = sorted((meta.get("cell_zones") or {}))
    plan.mesh_unresolved = list(meta.get("unresolved") or [])
    for item in list(meta.get("synthesised") or []) + list(meta.get("unrepresented") or []):
        plan.assumptions.append(f"mesh.py: {item}")

    foam_case = stack.case
    fluid = case_fluid(record, foam_case)
    _check_fluid_agreement(fluid, plan)
    patches = foam_case.patches_from_scenario(record)
    if plan.transient:
        # A transient run in SECONDS. deltaT has to resolve the viscous settling time or
        # the first step is a shock; 1/200 of `t_visc` is the convention used here and the
        # run is written 20 times, which is what render.py needs for an animation.
        delta_t = max(min((plan.t_visc_s or plan.end_time_s) / 200.0,
                          plan.end_time_s / 100.0), 1e-9)
        end_time = plan.end_time_s
        write_interval = plan.write_interval_s or end_time / 20.0
    else:
        # simpleFoam: endTime is an ITERATION COUNT (case.py's own convention) and the run
        # stops on fvSolution's residualControl long before it.
        delta_t, end_time, write_interval = 1, 2000, 500
    # `include_age=False`: mean age is `transport.py`'s answer, not a flow run's
    # (`assay2chip/foam/README.md` gives washout and mean age to transport.py), and
    # solving an extra transport equation on every flow case costs time this module has no
    # use for. It is also the only thing in `standard_functions` that has crashed here: on
    # module3_v1_translated's 1.55M cells the `age` object brought simpleFoam down with
    # SIGILL *after* the flow had converged, which turned a good solve into a failed run.
    functions = foam_case.standard_functions(patches, write_interval=50,
                                             include_age=False)
    built = foam_case.build_case(
        case_dir,
        fluid=fluid,
        patches=patches,
        solver=plan.solver,
        steady=not plan.transient,
        end_time=end_time,
        delta_t=delta_t,
        write_interval=write_interval,
        n_cores=plan.n_proc,
        functions=functions,
    )
    return {
        "case_dir": str(case_dir),
        "mesh_info": _plain(mesh_info),
        "case_info": _plain(built),
        "dropped_kwargs": {"mesh": mesh_dropped},
        "bound": dict(stack.bound),
    }


def _check_fluid_agreement(fluid: Any, plan: CasePlan, rtol: float = 1e-9) -> None:
    """The OpenFOAM case and the reduced model must run the SAME liquid.

    `case.py` resolves viscosity and density from the contracts, and so does
    `cfd/solve.py`; they agree today and the cross-validation in validate_foam.py is only
    meaningful while they do. A 1% drift in either would show up as a 1% "disagreement
    between the models" that no amount of grid refinement would ever explain, so it is
    caught here instead of being chased there.
    """
    for name, theirs, ours in (("mu_pa_s", fluid.mu_pa_s, plan.mu_pa_s),
                               ("rho_kg_m3", fluid.rho_kg_m3, plan.rho_kg_m3)):
        if not ours:
            continue
        if abs(theirs - ours) > rtol * max(abs(theirs), abs(ours)):
            raise FlowError(
                f"case.py and cfd/solve.py disagree about {name} for {plan.chip_id} step "
                f"{plan.step}: {theirs!r} vs {ours!r}. Fix the contract lookup in one of "
                f"them before trusting any cross-validation - the two models would be "
                f"solving different liquids."
            )


def case_fluid(record: Any, foam_case: Any) -> Any:
    """The `case.Fluid` this step runs in: the scenario's own FluidSpec wherever there is
    one, so the assumption flags scenario.py attached survive into the case.

    `case.fluid_from_spec` only divides the viscosity by the density; it does not re-decide
    the fluid, which scenario.py has already done and recorded. Falling back to
    `fluid_for(temp_c)` is for a bare `model.Scenario` with no provenance.
    """
    spec = getattr(record, "fluid", None)
    if spec is not None and hasattr(spec, "viscosity_pa_s"):
        return foam_case.fluid_from_spec(spec)
    temp_c = getattr(record, "temp_c", None)
    name = str(spec) if isinstance(spec, str) and spec else "aqueous"
    return foam_case.fluid_for(temp_c, name=name)


def run_case(case_dir: Path, plan: CasePlan, stack: Optional[FoamStack] = None) -> dict:
    """Ask runner.py to run `checkMesh` and then the solver, in parallel when asked.

    `runner.run` is the ONLY launcher in this package (`assay2chip/foam/README.md`), so
    this function builds no command line of its own. `check=False` because a failed solve
    still has a log worth reporting, and a `checkMesh` verdict belongs in the summary
    rather than in a traceback.
    """
    stack = stack or discover_foam()
    foam_runner = stack.runner
    if foam_runner is None:
        raise FlowError("assay2chip/foam/runner.py is not importable")
    case_dir = Path(case_dir)
    steps: List[dict] = []

    mesh_check = foam_runner.run(case_dir, "checkMesh", check=False)
    steps.append({"command": "checkMesh", **_plain(mesh_check.to_dict())})
    if not mesh_check.ok:
        return {"ok": False, "stage": "checkMesh", "steps": steps,
                "result": _plain(mesh_check.to_dict())}

    result = foam_runner.run(case_dir, plan.solver,
                             parallel=(plan.n_proc if plan.n_proc > 1 else None),
                             check=False)
    steps.append({"command": plan.solver, **_plain(result.to_dict())})
    return {
        "ok": bool(result.ok),
        "stage": plan.solver,
        "converged": result.converged,
        "steps": steps,
        "result": _plain(result.to_dict()),
    }


def collect(case_dir: Path, plan: CasePlan, mesh: Optional[Mesh] = None) -> RawFields:
    """Read one finished case into `RawFields`. No physics here, only parsing.

    Function-object `.dat` files first, because they are cheap and are what `case.py`'s
    `surfaceFieldValue` / `fieldMinMax` exist for; the written fields and `polyMesh`
    second, for the per-cellZone wall shear no patch-wise function object can give.
    """
    case_dir = Path(case_dir)
    dats = find_dat_files(case_dir)
    notes: List[str] = []
    patches: List[PatchSample] = []
    role_of = {}
    for port in plan.feeds:
        role_of[port] = "feed"
    for port in plan.vents:
        role_of[port] = "vent"
    for port in plan.capped:
        role_of[port] = "cap"

    # `case.standard_functions` writes TWO objects per port - `flow_<port>` (sum of phi)
    # and `p_<port>` (areaAverage of p) - in separate directories. They have to merge into
    # one PatchSample, so each file contributes whatever it has and neither is skipped for
    # lacking the other's column. Reading only the file with `sum(phi)` is how a summary
    # ends up reporting a pressure drop of exactly zero.
    for key, path in sorted(dats.items()):
        name = key.split("/")[0]
        port = _port_from_fo_name(name, role_of)
        if port is None:
            continue
        table = read_dat(path)
        existing = next((p for p in patches if p.port == port), None)
        sample = existing or PatchSample(port=port, patch=port, role=role_of.get(port, ""))
        if existing is None:
            patches.append(sample)
        phi = table.value("sum(phi)", "sum(phi)_", default=None)
        if phi is not None:
            sample.flow_m3_s = -phi if PHI_IS_OUTWARD else phi
        p_mean = table.value("areaAverage(p)", "average(p)", "weightedAreaAverage(p)",
                             default=None)
        if p_mean is not None:
            sample.p_mean_kinematic = p_mean
        p_max = table.value("max(p)", default=None)
        if p_max is not None:
            sample.p_max_kinematic = p_max
        area = table.meta_float("Area") or table.value("areaNormalIntegrate(1)", default=None)
        if area:
            sample.area_m2 = abs(area)
    for sample in patches:
        if sample.area_m2 > 0:
            sample.u_mean_m_s = abs(sample.flow_m3_s) / sample.area_m2

    # ports the case never reported: a capped port carries no flow, which is a fact and
    # not a gap, so it is added explicitly rather than left out of the mass balance.
    for port in plan.capped:
        if not any(p.port == port for p in patches):
            patches.append(PatchSample(port=port, role="cap", flow_m3_s=0.0))
    missing = [p for p in list(plan.feeds) + list(plan.vents)
               if not any(s.port == p for s in patches)]
    if missing:
        notes.append(
            f"no surfaceFieldValue output for {', '.join(missing)}: the mass balance and "
            f"the outlet split are incomplete. case.py should add one per port patch."
        )

    # field extrema
    p_max = _fo_extreme(dats, "p", "max")
    u_max = _fo_extreme(dats, "U", "max")
    wss_global = _fo_extreme(dats, "wallShearStress", "max")

    # per-zone wall shear and per-zone extrema
    zone_shear_max: Dict[str, float] = {}
    shear_source = ""
    try:
        zone_shear_max, shear_source, shear_notes = wall_shear_by_zone(case_dir)
        notes.extend(shear_notes)
    except FlowError as exc:
        notes.append(f"per-zone wall shear unavailable: {exc}")
    if wss_global is None:
        wss_global = zone_shear_max.get("__chip__")

    cell_zones = read_cell_zones(case_dir / "constant" / "polyMesh" / "cellZones")
    zone_u_max = zone_internal_max(case_dir, "U", zones=cell_zones)
    zone_p_max = zone_internal_max(case_dir, "p", zones=cell_zones)

    zones: List[ZoneSample] = []
    u_fallback: List[str] = []
    zone_names = sorted(set(k for k in zone_shear_max if not k.startswith("__"))
                        | set(k for k in cell_zones))
    if not zone_names and mesh is not None:
        zone_names = mesh.features()
    for name in zone_names:
        sample = ZoneSample(zone=name)
        sample.wall_shear_max_kinematic = zone_shear_max.get(name, math.nan)
        sample.wall_shear_source = shear_source
        sample.cells = len(cell_zones.get(name, ()))
        zone_u = zone_u_max.get(name)
        if zone_u is None:
            zone_u = _fo_zone_value(dats, name, ("max(mag(U))", "max(U)_mag", "max"))
        if zone_u is not None:
            sample.u_max_m_s = zone_u
        else:
            # Neither the written field nor a per-zone function object: the chip-wide
            # maximum is the only bound there is, so this zone's Re is an UPPER BOUND and
            # the note below says so rather than letting it pass for a measurement.
            sample.u_max_m_s = u_max if u_max is not None else math.nan
            u_fallback.append(name)
        zone_p = zone_p_max.get(name)
        if zone_p is None:
            zone_p = _fo_zone_value(dats, name, ("max(p)",))
        if zone_p is not None:
            sample.p_max_kinematic = zone_p
        if mesh is not None:
            sample.width_m, sample.depth_m = _narrowest_cross_section_m(mesh, name)
        zones.append(sample)
    if u_fallback:
        notes.append(
            f"no per-zone velocity for "
            f"{', '.join(u_fallback[:6])}{'...' if len(u_fallback) > 6 else ''}: their "
            f"Reynolds numbers use the CHIP-WIDE max |U| and are therefore UPPER BOUNDS, "
            f"not measurements. The written U field or a per-cellZone volFieldValue would "
            f"fix it."
        )

    converged, residuals, final_time = _convergence(case_dir, dats)
    cells = _cell_count(case_dir)
    return RawFields(
        patches=patches,
        zones=zones,
        kinematic=True,
        p_max_kinematic=p_max if p_max is not None else math.nan,
        u_max_m_s=u_max if u_max is not None else math.nan,
        wall_shear_max_kinematic=wss_global if wss_global is not None else math.nan,
        wall_shear_source=shear_source,
        final_time_s=final_time,
        converged=converged,
        initial_residuals=residuals,
        cells=cells,
        notes=notes,
    )


#: `case.standard_functions` names its port function objects `flow_<port>` and `p_<port>`.
#: `flow_` and `patch_` are unambiguous, but `p_` is NOT: this repo's ports are called
#: `p_in`, `p_mid`, `p_a_out`, so the pressure object for `p_in` is `p_p_in` and the one
#: for a hypothetical port `a_in` would be `p_a_in` - which is itself a real port id
#: elsewhere. So the unambiguous prefixes and an exact port-id match are both tried
#: BEFORE `p_` is stripped.
_FO_PREFIXES_SAFE = ("flow_", "patch_", "Q_")
_FO_PREFIXES_LAST = ("p_", "U_", "pressure_")


def _port_from_fo_name(name: str, roles: Mapping[str, str]) -> Optional[str]:
    """Which port a function object's name is about, or None if it is about no port.

    A summary that silently drops an outlet reports a clean mass balance on half the chip,
    so this has to resolve every spelling `case.py` might use - and must not mis-resolve
    one port's object onto another, which is why the order below matters.
    """
    for prefix in _FO_PREFIXES_SAFE:
        if name.startswith(prefix) and name[len(prefix):] in roles:
            return name[len(prefix):]
    if name in roles:
        return name
    for prefix in _FO_PREFIXES_LAST:
        if name.startswith(prefix) and name[len(prefix):] in roles:
            return name[len(prefix):]
    for port in sorted(roles, key=len, reverse=True):
        if name.endswith(f"_{port}"):
            return port
    return None


def _fo_extreme(dats: Mapping[str, Path], field_name: str, op: str) -> Optional[float]:
    """`fieldMinMax` output for one field, as a magnitude.

    ESI's `fieldMinMax` writes ONE file holding every field it was given, one row per
    field per write: `# Time field min location(min) max location(max)`. So the field is
    selected by its name in the `field` column, and only then is the final row read.
    Older and single-field layouts (one file per field, no `field` column) fall back to
    reading the column straight off.
    """
    for key, path in sorted(dats.items()):
        if "minmax" not in key.lower():
            continue
        table = read_dat(path)
        if table.has("field"):
            # ESI writes `mag(U)` in the field column for a vector and `p` for a scalar,
            # so both spellings are tried; the value itself comes from `group_anchor`
            # rather than from a named column, because the parallel layout adds an unnamed
            # processor column that would otherwise be read as the extremum.
            for spelling in (field_name, f"mag({field_name})"):
                picked = table.select("field", spelling)
                if not picked.rows:
                    continue
                value = picked.group_anchor(0 if op == "min" else 1)
                if value is None:
                    value = picked.value(op, default=None)
                if value is not None:
                    return abs(value)
            continue
        value = table.value(f"{op}(mag({field_name}))", f"{op}({field_name})", default=None)
        if value is not None:
            return abs(value)
        if field_name.lower() in key.lower():
            value = table.value(op, f"{op}_mag", default=None)
            if value is not None:
                return abs(value)
    return None


def _fo_zone_value(dats: Mapping[str, Path], zone: str,
                   columns: Sequence[str]) -> Optional[float]:
    """A `volFieldValue` restricted to one cellZone, by any of several column names."""
    for key, path in sorted(dats.items()):
        name = key.split("/")[0]
        if zone not in name:
            continue
        table = read_dat(path)
        for col in columns:
            value = table.value(col, default=None)
            if value is not None:
                return abs(value)
    return None


def _convergence(case_dir: Path, dats: Mapping[str, Path]) -> Tuple[Optional[bool],
                                                                   Dict[str, float],
                                                                   Optional[float]]:
    """Did it converge, and on what residuals? Read from runner.py's log if it is there.

    The runner owns the log, so this reads whatever file it left rather than running
    anything. A missing log is `None` - unknown - never `True`.
    """
    residuals: Dict[str, float] = {}
    final_time: Optional[float] = None
    logs = sorted(Path(case_dir).glob("log.*")) + sorted(Path(case_dir).glob("*.log"))
    converged: Optional[bool] = None
    for log in logs:
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "SIMPLE solution converged" in text or "solution converged" in text:
            converged = True
        if re.search(r"\bEnd\b\s*$", text.strip()[-40:]):
            converged = True if converged is None else converged
        for m in re.finditer(r"Solving for (\w+), Initial residual = ([\deE.+-]+)", text):
            try:
                residuals[m.group(1)] = float(m.group(2))
            except ValueError:
                continue
        times = re.findall(r"^Time = ([\deE.+-]+)", text, re.M)
        if times:
            try:
                final_time = float(times[-1])
            except ValueError:
                pass
    latest = latest_time_dir(case_dir)
    if final_time is None and latest is not None:
        try:
            final_time = float(latest.name)
        except ValueError:
            final_time = None
    return converged, residuals, final_time


def _cell_count(case_dir: Path) -> int:
    owner = Path(case_dir) / "constant" / "polyMesh" / "owner"
    if not owner.exists():
        return 0
    try:
        labels = read_label_list(owner)
    except Exception:
        return 0
    return (max(labels) + 1) if labels else 0


def _plain(value: Any) -> Any:
    """Anything a sibling returns, reduced to something json.dumps will accept."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_plain(v) for v in value]
    if hasattr(value, "to_json"):
        try:
            return _plain(value.to_json())
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        return {k: _plain(v) for k, v in vars(value).items() if not k.startswith("_")}
    return str(value)


def run_step(
    chip_path: Any,
    record: Any,
    *,
    mesh: Optional[Mesh] = None,
    protocol: Optional[Mapping] = None,
    spacing_mm: Optional[float] = None,
    z_cells: int = 8,
    n_proc: int = 1,
    reduced: Optional[Field] = None,
    work_dir: Optional[Path] = None,
    run_name: str = "foam",
    stack: Optional[FoamStack] = None,
    escalate: bool = True,
    step_index: Optional[int] = None,
    force_solver: str = "",
) -> dict:
    """One chip, one protocol step: mesh, case, run, read, summarise, judge.

    The summary carries its own findings (`summary["findings"]`) so a caller gets one
    object per step. A steady run whose OWN field reports Re > 1 is re-run transient and
    the superseded summary is kept under `escalated_from`: a steady answer that the
    answer itself invalidates is not a result.
    """
    chip = json.loads(Path(chip_path).read_text(encoding="utf-8"))
    mesh = mesh if mesh is not None else cfd_mesh.build_mesh(chip, grid_mm=0.25)
    plan = plan_case(chip, record, mesh, spacing_mm=spacing_mm, z_cells=z_cells,
                     n_proc=n_proc, reduced=reduced, work_dir=work_dir, run_name=run_name,
                     force_solver=force_solver)
    stack = stack or discover_foam()
    built = build_case(chip, record, mesh, plan, stack)
    run = run_case(Path(plan.case_dir), plan, stack)
    if not run["ok"]:
        raise FlowError(
            f"{plan.solver} failed on {plan.chip_id} step {plan.step}; the runner's log is "
            f"in {plan.case_dir}. Result: {run['result']}"
        )
    raw = collect(Path(plan.case_dir), plan, mesh)
    if run.get("converged") is not None:
        raw.converged = bool(run["converged"])
    summary = summarise(raw, plan, mesh=mesh,
                        extra={"case": built, "run": run, "stack": stack.as_json()})

    re_max = (summary.get("reynolds", {}) or {}).get("chip_max")
    if (escalate and not force_solver and not plan.transient and re_max is not None
            and re_max > RE_STEADY_LIMIT):
        superseded = summary
        plan.transient = True
        plan.solver = "pimpleFoam"
        plan.transient_reason = (
            f"the steady run's OWN field reports Re = {re_max:.3g} > {RE_STEADY_LIMIT:g}, "
            f"so the steady assumption is contradicted by its own answer and the case was "
            f"re-run transient"
        )
        if plan.end_time_s <= 0:
            flush = (mesh.volume_ul / plan.q_in_ul_min * 60.0) if plan.q_in_ul_min else 1.0
            plan.end_time_s = float(f"{max(flush, VISC_SETTLE_FACTOR * (plan.t_visc_s or 0.1)):.3g}")
            plan.write_interval_s = plan.end_time_s / 20.0
        built = build_case(chip, record, mesh, plan, stack)
        run = run_case(Path(plan.case_dir), plan, stack)
        if not run["ok"]:
            raise FlowError(f"pimpleFoam escalation failed on {plan.chip_id} step {plan.step}")
        raw = collect(Path(plan.case_dir), plan, mesh)
        if run.get("converged") is not None:
            raw.converged = bool(run["converged"])
        summary = summarise(raw, plan, mesh=mesh,
                            extra={"case": built, "run": run, "stack": stack.as_json()})
        summary["escalated_from"] = {
            "solver": superseded.get("solver"),
            "reynolds_chip_max": re_max,
            "why": plan.transient_reason,
            "summary": superseded,
        }

    summary["plan"] = plan.to_json()
    summary["findings"] = [
        f.to_json() for f in findings_for(summary, chip, protocol, step_index=step_index)
    ]
    return summary


def first_flowing_step(run: Any) -> Optional[Any]:
    """The first step of a protocol that actually moves liquid.

    `no_flow` steps are kept by scenario.py on purpose (a capped hold is where bubbles
    grow), but there is no flow field to solve in one, so a flow run skips to the first
    step with a driven feed.
    """
    for rec in getattr(run, "step_scenarios", []):
        if rec.kind == "step" and rec.q_in_ul_min > 0 and rec.mappable:
            return rec
    return None


def flowing_steps(run: Any) -> List[Any]:
    return [r for r in getattr(run, "step_scenarios", [])
            if r.kind == "step" and r.q_in_ul_min > 0 and r.mappable]


# --- grid sensitivity ------------------------------------------------------------------


QOI_PATHS: Dict[str, Tuple[str, ...]] = {
    "peak_pressure_pa": ("pressure", "peak_pa"),
    "shear_chip_max_dyn_cm2": ("shear", "chip_max_dyn_cm2"),
    "reynolds_chip_max": ("reynolds", "chip_max"),
}


def _dig(summary: Mapping, path: Sequence[str]) -> Optional[float]:
    node: Any = summary
    for key in path:
        if not isinstance(node, Mapping) or key not in node:
            return None
        node = node[key]
    return float(node) if isinstance(node, (int, float)) else None


def grid_sensitivity(summaries: Sequence[Mapping], spacings: Sequence[float]) -> dict:
    """Two (or more) resolutions of the same step -> an error bar on every number.

    With two grids the convergence ORDER cannot be measured, only assumed, so this reports
    a Grid Convergence Index at the formal second order with Roache's conservative
    two-grid safety factor of 3: `GCI = Fs |(f_coarse - f_fine)/f_fine| / (r^p - 1)`.
    That is the honest form of "this number is 2.1% +/- 0.8%" - a discretisation error
    bar, not a claim that the fine grid is right.

    `extrapolated` is the Richardson value the two grids point at; the fine-grid number
    plus the GCI is what a finding should quote.
    """
    pairs = sorted(zip(spacings, summaries), key=lambda t: -t[0])
    if len(pairs) < 2:
        return {"status": "incomplete", "reason": "grid sensitivity needs two resolutions"}
    (h_coarse, coarse), (h_fine, fine) = pairs[0], pairs[-1]
    ratio = h_coarse / h_fine if h_fine > 0 else math.nan
    out: Dict[str, Any] = {
        "status": "ok" if ratio > 1 else "incomplete",
        "spacing_mm": {"coarse": h_coarse, "fine": h_fine},
        "refinement_ratio": ratio,
        "assumed_order": ASSUMED_ORDER,
        "safety_factor": GCI_SAFETY_TWO_GRID,
        "cells": {"coarse": _dig(coarse, ("grid", "cells")),
                  "fine": _dig(fine, ("grid", "cells"))},
        "quantities": {},
        "note": (
            "two grids cannot measure the convergence order, so it is ASSUMED to be the "
            "formal 2 and Roache's two-grid safety factor 3 is applied. Quote the fine-grid "
            "value with the GCI as its error bar."
        ),
    }
    worst = 0.0
    for name, path in QOI_PATHS.items():
        f_c, f_f = _dig(coarse, path), _dig(fine, path)
        if f_c is None or f_f is None or not f_f:
            out["quantities"][name] = {"coarse": f_c, "fine": f_f, "gci": None,
                                       "reason": "not reported at both resolutions"}
            continue
        rel = abs((f_c - f_f) / f_f)
        denom = (ratio ** ASSUMED_ORDER - 1.0) if ratio > 1 else math.nan
        gci = GCI_SAFETY_TWO_GRID * rel / denom if denom and math.isfinite(denom) else None
        extrapolated = (f_f + (f_f - f_c) / denom) if denom and math.isfinite(denom) else None
        out["quantities"][name] = {
            "coarse": f_c, "fine": f_f,
            "relative_change": rel,
            "gci": gci,
            "gci_percent": gci * 100.0 if gci is not None else None,
            "extrapolated": extrapolated,
        }
        if gci:
            worst = max(worst, gci)
    out["worst_gci"] = worst or None
    out["worst_gci_percent"] = worst * 100.0 if worst else None
    return out


# ======================================================================================
# CLI
# ======================================================================================


def _load_protocol_for(chip_id: str) -> Tuple[Optional[Path], Optional[dict]]:
    path = ROOT / "protocols" / f"{chip_id}.json"
    if path.exists():
        return path, json.loads(path.read_text(encoding="utf-8"))
    for candidate in sorted((ROOT / "protocols").glob("*.json")):
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("chip_id") == chip_id or data.get("chip") == chip_id:
            return candidate, data
    return None, None


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chip", default="module1_v1", help="chip id in chip/<id>.json")
    ap.add_argument("--protocol", default=None, help="protocols/<id>.json (default: by chip)")
    ap.add_argument("--step", default=None, help="protocol step number (default: first flowing)")
    ap.add_argument("--all-flowing", action="store_true", help="every step that moves liquid")
    ap.add_argument("--spacing-mm", type=float, default=None,
                    help="xy cell size; omit to let mesh.py choose (6 cells across the narrowest channel)")
    ap.add_argument("--z-cells", type=int, default=8)
    ap.add_argument("--np", type=int, default=1, dest="n_proc")
    ap.add_argument("--grid-sensitivity", nargs=2, type=float, default=None,
                    metavar=("COARSE_MM", "FINE_MM"))
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    chip_path = ROOT / "chip" / f"{args.chip}.json"
    if not chip_path.exists():
        print(f"no chip spec at {chip_path}", file=sys.stderr)
        return 2
    protocol_path = Path(args.protocol) if args.protocol else None
    if protocol_path is None:
        protocol_path, _ = _load_protocol_for(args.chip)
    if protocol_path is None:
        print(f"no protocol found for chip {args.chip}", file=sys.stderr)
        return 2
    protocol = json.loads(Path(protocol_path).read_text(encoding="utf-8"))
    run = cfd_scenario.build_protocol(protocol_path, chip_path, worst_cases=False)

    records = flowing_steps(run)
    if args.step is not None:
        records = [r for r in records if str(r.step) == str(args.step)]
    elif not args.all_flowing:
        records = records[:1]
    if not records:
        print("no flowing step matched", file=sys.stderr)
        return 2

    stack = discover_foam()
    if not stack.available or not stack.openfoam_available():
        print(json.dumps({
            "schema": SCHEMA, "status": "skipped",
            "reason": stack.reason or "OpenFOAM is not installed (runner.available() is False)",
            "stack": stack.as_json(),
            "planned": [_plan_only(chip_path, r, protocol) for r in records],
        }, indent=2))
        return 0

    chip = json.loads(chip_path.read_text(encoding="utf-8"))
    mesh = cfd_mesh.build_mesh(chip, grid_mm=0.25)
    reduced = cfd_solve.solve_steps(mesh, [r.scenario.for_mesh(mesh) for r in records])
    summaries = []
    for record, red in zip(records, reduced):
        if args.grid_sensitivity:
            per_grid = [
                run_step(chip_path, record, mesh=mesh, protocol=protocol,
                         spacing_mm=h, z_cells=args.z_cells, n_proc=args.n_proc,
                         reduced=red, stack=stack)
                for h in args.grid_sensitivity
            ]
            summary = per_grid[-1]
            summary["grid_sensitivity"] = grid_sensitivity(per_grid, args.grid_sensitivity)
        else:
            summary = run_step(chip_path, record, mesh=mesh, protocol=protocol,
                               spacing_mm=args.spacing_mm, z_cells=args.z_cells,
                               n_proc=args.n_proc, reduced=red, stack=stack)
        summaries.append(summary)
        out = args.out or (Path(summary["plan"]["case_dir"]).parent / f"step_{record.step}.json")
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(summaries, indent=2))
    else:
        for s in summaries:
            print(format_summary(s))
    return 0


def _plan_only(chip_path: Path, record: Any, protocol: Mapping) -> dict:
    """What WOULD be run, for a dry run with no OpenFOAM: the plan and nothing else."""
    chip = json.loads(Path(chip_path).read_text(encoding="utf-8"))
    mesh = cfd_mesh.build_mesh(chip, grid_mm=0.25)
    red = cfd_solve.solve_step(mesh, record.scenario.for_mesh(mesh))
    return plan_case(chip, record, mesh, reduced=red).to_json()


def format_summary(summary: Mapping) -> str:
    """The summary as a few readable lines - what a human checks before reading the JSON."""
    lines = [
        f"{summary.get('chip_id')} step {summary.get('step')}  "
        f"{summary.get('solver')}{' (transient)' if summary.get('transient') else ''}  "
        f"SIMULATED",
    ]
    if summary.get("transient_reason"):
        lines.append(f"  why transient: {summary['transient_reason']}")
    if summary.get("solver_forced_reason"):
        lines.append(f"  SOLVER FORCED: {summary['solver_forced_reason']}")
    pressure = summary.get("pressure", {}) or {}
    lines.append(f"  peak pressure   {_fmt(pressure.get('peak_pa'))} Pa")
    for key, value in sorted((pressure.get("drop_pa") or {}).items()):
        lines.append(f"  dP {key:<24} {_fmt(value)} Pa")
    split = (summary.get("flow", {}) or {}).get("outlet_split", {}) or {}
    for port, frac in sorted((split.get("fraction") or {}).items()):
        lines.append(f"  out {port:<22} {_fmt(frac, 4)}  "
                     f"({_fmt((split.get('ul_min') or {}).get(port))} uL/min)")
    balance = (summary.get("flow", {}) or {}).get("mass_balance", {}) or {}
    lines.append(f"  mass closure    {_fmt(balance.get('closure'), 3)}")
    for zone, rec in sorted((summary.get("shear", {}) or {}).get("per_zone", {}).items()):
        lines.append(f"  shear {zone:<20} {_fmt(rec.get('max_pa'), 4)} Pa = "
                     f"{_fmt(rec.get('max_dyn_cm2'), 4)} dyn/cm2")
    reynolds = summary.get("reynolds", {}) or {}
    lines.append(f"  Re max          {_fmt(reynolds.get('chip_max'), 3)}"
                 + (f"  over 1 in: {', '.join(reynolds.get('over_limit') or [])}"
                    if reynolds.get("over_limit") else ""))
    for f in summary.get("findings", []) or []:
        lines.append(f"  [{f.get('severity'):<7}] {f.get('class')}: {f.get('message','')[:110]}")
    for note in summary.get("assumptions", []) or []:
        lines.append(f"  ASSUMED: {note[:110]}")
    return "\n".join(lines)


def _fmt(value: Any, digits: int = 5) -> str:
    if value is None:
        return "-"
    if isinstance(value, float) and not math.isfinite(value):
        return "-"
    try:
        return f"{float(value):.{digits}g}"
    except (TypeError, ValueError):
        return str(value)


if __name__ == "__main__":
    raise SystemExit(main())

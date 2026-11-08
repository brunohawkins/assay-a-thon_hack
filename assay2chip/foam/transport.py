"""assay2chip/foam/transport.py - washout and mean age in 3D, on a converged OpenFOAM field.

    python assay2chip/foam/transport.py --chip module3_v1_translated --step 2
    python assay2chip/foam/transport.py --chip module1_v1 --fidelity high --json

    from assay2chip.foam import transport
    result = transport.check_step("module3_v1_translated", step=2)   # needs OpenFOAM
    result["findings"], result["cross_validation"], result["wall_time_s"]

The 3D counterpart of `assay2chip/cfd/transport.py`, which it cross-validates against on the
same chip and the same step. Three things, all on ONE converged steady `simpleFoam` field:

1. MEAN AGE AND DEAD ZONES. The steady mean-age equation `div(phi age) = 1` (age 0 where
   liquid enters), upwind, on the converged 3D face fluxes - the equation the `age`
   function object solves. Per cellZone: the volume-weighted mean age and the dead-zone
   volume fraction, judged with the reduced model's own thresholds
   (`cfd.transport.DEAD_ZONE_AGE_FACTOR`, `DEAD_ZONE_SPEED_FRACTION`), once per 3D CELL and
   once per depth-averaged PLAN COLUMN. The column number is the like-for-like comparison
   with Hele-Shaw; the cell number minus the column number is what the no-slip floor, lid
   and side walls add.

   WHY THE ASSEMBLY IS DONE HERE AND NOT IN THE `age` FUNCTION OBJECT. v2606's `age` object
   is usable only in a running solver (in `-postProcess` it looks up `phi` before the
   solver creates it), and on these chips its linear solve dies: DILU and GAMG raise
   sigFpe in `calcReciprocalD`, and unpreconditioned PBiCGStab diverges. The cause is
   physical - a capped port leaves a dead-end line whose face fluxes are ~1e-26 m^3/s,
   which is a zero diagonal in an upwind age matrix. So the same discrete equation is
   assembled from OpenFOAM's own `phi`, owner and neighbour and handed to the reduced
   model's exact flow-ordered sweep (`cfd.transport.mean_age`), which reports those
   dead-end cells as UNREACHABLE (infinite age) instead of dividing by zero. Set
   `age_engine="function_object"` to try the function object anyway.

2. WASHOUT. `scalarTransportFoam` on the FROZEN converged U and phi: old liquid T = 1 in the
   step's target chamber(s), fresh liquid T = 0 at every feed, the contract's molecular
   diffusivity (flagged, as cfd/transport.py flags it). The exchanged fraction is
   1 - volume-average T over the chamber's cellZone, sampled every step by a
   `volFieldValue` function object, and is judged against the step's `exchange_kind`
   exactly as `cfd.transport.exchange_intent` judges it (sweep or absent ->
   `exchange.target_fraction`, partial -> the stated fraction, fill -> full) - the intent
   logic is imported, not restated.

3. CROSS-VALIDATION. The reduced model runs on the same chip and step with the SAME
   diffusivity, and every pair of numbers (mean age, dead fraction, fraction reached,
   chamber volumes required) is reported side by side with the difference and the reason
   believed for it. A disagreement is never averaged away.

THE PHYSICS THE TWO MODELS DISAGREE ABOUT, stated up front so the numbers can be read:
Hele-Shaw is depth-averaged plug flow in z. The real flow is parabolic across the 0.3 mm
depth, so liquid next to the floor and lid moves far slower than the mean, and the old
liquid there can only leave by diffusing into the faster core (h^2/D is minutes for a
small molecule). That is Taylor-Aris dispersion, and it makes the 3D tail of every washout
LONGER than the reduced model's: more chamber volumes to reach 99%. Side walls do the same
in plan wherever a chamber is narrow. Recirculation needs Re >> 1 and is not expected on
these chips except in Module 6's quench (Re 13-23); a closed 3D flow loop is counted and
reported (`flow_loops`).

FIDELITY. `fast` (the default) caps the mesh at FAST_CELL_BUDGET cells and takes 16 time
steps per chamber volume, and finishes in a few minutes per case on a 6-core Mac. `high`
uses mesh.py's own budget (6 cells across the narrowest channel) and 32 steps per chamber
volume. Every result carries the cell count, the spacing and the wall time of each stage.

Findings are `assay2chip.cfd.model.Finding`s with `source: "openfoam"`. The six printed
demo chips are FROZEN, so every fix hint is a FULL-SPEC design improvement and says so.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.cfd import transport as cfd_transport  # noqa: E402
from assay2chip.cfd.model import Finding  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402

SCHEMA = "foam-transport/1"
SOURCE = "openfoam"
DEFAULT_WORK = ROOT / "assay2chip" / "work"

#: The six demo chips that are printed. Findings on them are reported; fixes on them are
#: framed as full-spec design improvements, never as edits to the printed part.
FROZEN_CHIPS = frozenset({"module1_v1", "spc_demo_v1", "module3_v1_translated",
                          "module4_v1", "module5_v1", "module6_v1"})

#: Mesh cap for the default `fast` fidelity. mesh.py's own default (2M) puts module3 at
#: 1.55M cells; 600k keeps every repo chip under ~2 minutes of simpleFoam on 6 cores and
#: still resolves the weir gap with 3 cells (the z grading does not depend on the budget).
FAST_CELL_BUDGET = 600_000

M3_PER_UL = 1e-9
S_PER_MIN = 60.0


@dataclass(frozen=True)
class Fidelity:
    """Everything that trades accuracy for wall time, in one place."""

    name: str
    cell_budget: int
    washout_steps_per_cv: int
    washout_cv_cap: float
    parcels_per_injector: int
    note: str

    def to_json(self) -> dict:
        return dict(self.__dict__)


def fidelity(name: str = "fast") -> Fidelity:
    from assay2chip.foam import mesh as foam_mesh

    if name == "fast":
        return Fidelity("fast", FAST_CELL_BUDGET, 8, 8.0, 200,
                        f"mesh capped at {FAST_CELL_BUDGET} cells; 8 Crank-Nicolson steps "
                        f"per chamber volume (time error ~(1/8)^2/12 = 0.1%); washout "
                        f"marched to at most 8 chamber volumes")
    if name == "high":
        return Fidelity("high", foam_mesh.DEFAULT_CELL_BUDGET,
                        cfd_transport.WASHOUT_STEPS_PER_CV,
                        cfd_transport.WASHOUT_CV_SEARCH_CAP, 1000,
                        "mesh.py's own budget (6 cells across the narrowest channel); "
                        "the reduced model's 32 steps per chamber volume and 25-volume cap")
    raise ValueError(f"unknown fidelity {name!r}: 'fast' or 'high'")


class TransportError(RuntimeError):
    """A 3D transport case could not be built, run or read."""


# ======================================================================================
# reading OpenFOAM ascii files fast (numpy, no per-entry Python)
# ======================================================================================


def _list_block(text: str, start: int) -> tuple[np.ndarray, int]:
    """Numbers of the `N ( ... )` list whose count begins at or after `start`."""
    m = re.compile(r"(\d+)\s*\(").search(text, start)
    if m is None:
        raise TransportError("no list found where one was expected")
    n = int(m.group(1))
    body_start = m.end()
    end = text.find("\n)", body_start)
    if end < 0:
        end = text.rfind(")")
    body = text[body_start:end].replace("(", " ").replace(")", " ")
    arr = np.array(body.split(), dtype=float) if body.strip() else np.zeros(0)
    if n == 0:
        arr = np.zeros(0)
    return arr, end + 2


def read_label_list(path: Path) -> np.ndarray:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    head = text.find("FoamFile")
    start = text.find("}", head) + 1 if head >= 0 else 0
    arr, _ = _list_block(_strip_comments(text[start:]), 0)
    return arr.astype(np.int64)


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _value_entry(text: str, at: int, n: int, ncomp: int) -> np.ndarray:
    """`uniform X` / `uniform (x y z)` / `nonuniform List<..> N (...)` starting at `at`."""
    m = re.compile(r"\s*(nonuniform|uniform)").match(text, at)
    if m is None:
        raise TransportError("expected uniform/nonuniform")
    if m.group(1) == "uniform":
        tail = text[m.end():text.find(";", m.end())].replace("(", " ").replace(")", " ")
        vals = np.array(tail.split(), dtype=float)
        return np.tile(vals, (n, 1)).reshape(n, ncomp) if ncomp > 1 else np.full(n, vals[0])
    arr, _ = _list_block(text, m.end())
    return arr.reshape(-1, ncomp) if ncomp > 1 else arr


def read_internal_field(path: Path, ncomp: int = 1, n_cells: int | None = None) -> np.ndarray:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    at = text.find("internalField")
    if at < 0:
        raise TransportError(f"{path}: no internalField")
    if n_cells is None:
        m = re.compile(r"\s*nonuniform[^\n]*\n?\s*(\d+)").match(text, at + len("internalField"))
        n_cells = int(m.group(1)) if m else 1
    return _value_entry(text, at + len("internalField"), n_cells, ncomp)


def read_patch_values(path: Path | str, patch: str, n_faces: int, ncomp: int = 1, *,
                      text: str | None = None) -> np.ndarray | None:
    """The `value` entry of one boundary patch, or None when the patch writes no value.

    Pass `text` (the file already read) when reading several patches of one field: phi on
    a 600k-cell mesh is 50 MB, and reading it once per patch was most of the run time.
    """
    if text is None:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    bf = text.find("boundaryField")
    m = re.compile(rf"\n\s*{re.escape(patch)}\s*\n\s*\{{").search(text, bf)
    if m is None:
        return None
    end = text.find("\n    }", m.end())
    v = re.compile(r"\n\s*value\s").search(text, m.end(), end if end > 0 else len(text))
    if v is None:
        return None
    return _value_entry(text, v.end(), n_faces, ncomp)


def read_boundary(path: Path) -> list[dict]:
    """`polyMesh/boundary` -> [{name, type, nFaces, startFace}] in face order."""
    text = _strip_comments(Path(path).read_text(encoding="utf-8", errors="replace"))
    text = text[text.find("}") + 1:]
    out = []
    for m in re.finditer(r"([A-Za-z_][\w.\-]*)\s*\{([^{}]*)\}", text):
        body = m.group(2)
        get = lambda k: re.search(rf"\b{k}\s+([^;]+);", body)  # noqa: E731
        out.append({"name": m.group(1), "type": get("type").group(1).strip(),
                    "nFaces": int(get("nFaces").group(1)),
                    "startFace": int(get("startFace").group(1))})
    return out


def read_cell_zones(path: Path, n_cells: int) -> tuple[list[str], np.ndarray]:
    """`polyMesh/cellZones` -> (names, zone index per cell; -1 for no zone)."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    text = text[text.find("}", text.find("FoamFile")) + 1:]
    zone = np.full(n_cells, -1, dtype=np.int32)
    names: list[str] = []
    for m in re.finditer(r"\n([A-Za-z_][\w.\-]*)\s*\n\{", text):
        labels, _ = _list_block(text, text.find("cellLabels", m.end()))
        zone[labels.astype(np.int64)] = len(names)
        names.append(m.group(1))
    return names, zone


def latest_time(case_dir: Path) -> Path | None:
    best, best_t = None, -1.0
    for d in Path(case_dir).iterdir():
        try:
            t = float(d.name)
        except ValueError:
            continue
        if d.is_dir() and t > best_t and (d / "U").exists():
            best, best_t = d, t
    return best


# ======================================================================================
# the protocol step
# ======================================================================================


def load_step(chip_id: str, step: int | str | None = None, *,
              want: str = "exchange") -> tuple[dict, Path, dict, Any, dict | None]:
    """(chip, chip_path, protocol, ScenarioRecord, raw protocol step) for one step.

    `step=None` takes the first flowing step whose operation is `want` (an exchange, for
    a washout), else the first flowing step. The RAW protocol step travels too, because it
    is the only place that still knows whether `exchange_kind` was stated or defaulted -
    `cfd.transport.exchange_intent` cares.
    """
    from assay2chip.cfd import scenario as cfd_scenario
    from assay2chip.foam import flow

    chip_path = ROOT / "chip" / f"{chip_id}.json"
    if not chip_path.exists():
        raise TransportError(f"no chip spec at {chip_path}")
    protocol_path, protocol = flow._load_protocol_for(chip_id)
    if protocol_path is None:
        raise TransportError(f"no protocol found for chip {chip_id}")
    run = cfd_scenario.build_protocol(protocol_path, chip_path, worst_cases=False)
    records = flow.flowing_steps(run)
    if not records:
        raise TransportError(f"{chip_id}: no step moves liquid")
    if step is not None:
        picked = [r for r in records if str(r.step) == str(step)]
        if not picked:
            raise TransportError(f"{chip_id}: step {step} is not a flowing step "
                                 f"(flowing: {[r.step for r in records][:20]}...)")
        record = picked[0]
    else:
        record = next((r for r in records if r.operation == want), records[0])
    raw = next((s for s in (protocol or {}).get("steps", [])
                if str(s.get("step")) == str(record.step)), None)
    chip = json.loads(chip_path.read_text(encoding="utf-8"))
    return chip, chip_path, protocol or {}, record, raw


# ======================================================================================
# the converged steady field
# ======================================================================================


@dataclass
class SteadyCase:
    chip_id: str
    step: Any
    case_dir: Path
    time_dir: Path
    mesh_meta: dict
    fluid: Any
    patches: dict
    feeds: list[str]
    vents: list[str]
    wall_time_s: dict[str, float]
    run: dict
    notes: list[str] = dc_field(default_factory=list)

    def summary(self) -> dict:
        xy = self.mesh_meta.get("xy", {})
        return {
            "case_dir": str(self.case_dir),
            "cells": self.mesh_meta.get("cells"),
            "dxy_mm": xy.get("dxy_mm"),
            "cells_across_narrowest": xy.get("cells_across_narrowest"),
            "z_cells": (self.mesh_meta.get("z") or {}).get("cells"),
            "weirs": [{k: w.get(k) for k in ("chamber", "wall", "gap_um", "gap_cells",
                                             "gap_mm_meshed")}
                      for w in self.mesh_meta.get("weirs", [])],
            "budget_note": xy.get("budget_note"),
            "fluid": self.fluid.to_dict() if hasattr(self.fluid, "to_dict") else str(self.fluid),
            "feeds": self.feeds,
            "vents": self.vents,
            "simpleFoam": self.run,
            "wall_time_s": self.wall_time_s,
            "notes": self.notes,
        }


def _regions_reference(chip: dict, **mesh_kw: Any) -> int:
    from assay2chip.foam import mesh as foam_mesh

    meta = foam_mesh.write_polymesh(chip, None, **mesh_kw).meta
    return int(meta["connectivity"]["regions"])


def solve_steady(chip: dict, record: Any, case_dir: Path, *, fid: Fidelity,
                 n_proc: int = 6, gap_um: float | None = None, gap_delta_um: float = 0.0,
                 timeout_s: float = 1800.0, reuse: bool = False) -> SteadyCase:
    """Mesh (mesh.py), case (case.py), simpleFoam (runner.py), cell volumes and centres.

    A capped mesh must not change the TOPOLOGY: coarsening can close a 0.2 mm channel
    (spc_demo_v1 splits into three pieces at 250k cells). So the region count is checked
    against mesh.py's full-budget mesh and the budget doubled until they agree.
    """
    from assay2chip.foam import case as foam_case
    from assay2chip.foam import flow
    from assay2chip.foam import mesh as foam_mesh
    from assay2chip.foam import runner

    if not runner.available():
        raise TransportError(runner.why_not())
    case_dir = Path(case_dir)
    key = {"budget": fid.cell_budget, "gap_um": gap_um, "gap_delta_um": gap_delta_um,
           "step": str(record.step), "chip": chip.get("id"),
           "contracts": contracts.fingerprint()}
    if reuse:
        cached = _reuse_steady(case_dir, key, record)
        if cached is not None:
            return cached
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)
    walls: dict[str, float] = {}
    notes: list[str] = []
    mesh_kw = {"gap_um": gap_um, "gap_delta_um": gap_delta_um}

    t0 = time.perf_counter()
    budget = fid.cell_budget
    want_regions = _regions_reference(chip, **mesh_kw)
    while True:
        built = foam_mesh.write_polymesh(chip, case_dir, cell_budget=budget, **mesh_kw)
        regions = int(built.meta["connectivity"]["regions"])
        if regions <= want_regions or budget >= foam_mesh.DEFAULT_CELL_BUDGET:
            break
        notes.append(f"a {budget}-cell mesh split the fluid into {regions} pieces against "
                     f"{want_regions} at full resolution; budget doubled")
        budget = min(budget * 2, foam_mesh.DEFAULT_CELL_BUDGET)
    meta = built.meta
    walls["mesh"] = time.perf_counter() - t0

    fluid = flow.case_fluid(record, foam_case)
    patches = foam_case.patches_from_scenario(record)
    have = {p["name"] for p in meta["patches"]}
    for port in sorted(set(patches) - have):
        if patches[port].role in ("feed", "vent"):
            notes.append(f"port {port} ({patches[port].role}) has no patch in the mesh "
                         f"(mesh.py: no fluid under its bore), so it is not driven")
        patches.pop(port)
    if not any(b.role == "feed" for b in patches.values()):
        raise TransportError(f"{meta['chip_id']} step {record.step}: no feed port has a patch")
    functions = {}
    for name, bc in patches.items():
        if bc.role in ("feed", "vent"):
            functions[f"flow_{name}"] = foam_case.patch_flow_rate(name, write_interval=50)
            functions[f"p_{name}"] = foam_case.patch_mean_pressure(name, write_interval=50)
    functions["fieldMinMax"] = foam_case.field_min_max(write_interval=50)
    foam_case.build_case(case_dir, fluid=fluid, patches=patches, n_cores=n_proc,
                         functions=functions)

    t0 = time.perf_counter()
    res = runner.run(case_dir, "simpleFoam", parallel=(n_proc if n_proc > 1 else None),
                     timeout_s=timeout_s, check=False)
    walls["simpleFoam"] = time.perf_counter() - t0
    if not res.ok:
        raise TransportError(f"simpleFoam failed in {case_dir}: {res.verdict}\n{res.tail(20)}")
    tdir = latest_time(case_dir)
    if tdir is None or tdir.name == "0":
        raise TransportError(f"simpleFoam wrote no solution in {case_dir}")

    t0 = time.perf_counter()
    for util in ("writeCellVolumes", "writeCellCentres"):
        runner.run(case_dir, "postProcess", args=["-func", util, "-time", tdir.name],
                   log_name=f"log.{util}", timeout_s=timeout_s, steady=False)
    walls["postProcess"] = time.perf_counter() - t0
    for p in case_dir.glob("processor*"):
        shutil.rmtree(p, ignore_errors=True)
    out = SteadyCase(
        chip_id=str(meta["chip_id"]), step=record.step, case_dir=case_dir, time_dir=tdir,
        mesh_meta=meta, fluid=fluid, patches=patches,
        feeds=sorted(k for k, b in patches.items() if b.role == "feed"),
        vents=sorted(k for k, b in patches.items() if b.role == "vent"),
        wall_time_s=walls,
        run={"ok": res.ok, "converged": res.converged, "iterations": res.iterations,
             "verdict": res.verdict, "cores": res.parallel},
        notes=notes,
    )
    (case_dir / STEADY_STAMP).write_text(json.dumps({
        "key": key, "meta": meta, "wall_time_s": walls, "run": out.run, "notes": notes,
        "time_dir": tdir.name}, default=str), encoding="utf-8")
    return out


STEADY_STAMP = "steady_case.json"


def _reuse_steady(case_dir: Path, key: dict, record: Any) -> SteadyCase | None:
    """A converged case from an earlier call with the SAME mesh, step and contracts."""
    from assay2chip.foam import case as foam_case
    from assay2chip.foam import flow

    stamp = case_dir / STEADY_STAMP
    if not stamp.exists():
        return None
    try:
        saved = json.loads(stamp.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if saved.get("key") != json.loads(json.dumps(key, default=str)):
        return None
    tdir = case_dir / saved["time_dir"]
    if not all((tdir / n).exists() for n in ("U", "phi", "V", "C")):
        return None
    patches = foam_case.patches_from_scenario(record)
    have = {p["name"] for p in saved["meta"]["patches"]}
    patches = {k: v for k, v in patches.items() if k in have}
    return SteadyCase(
        chip_id=str(saved["meta"]["chip_id"]), step=record.step, case_dir=case_dir,
        time_dir=tdir, mesh_meta=saved["meta"], fluid=flow.case_fluid(record, foam_case),
        patches=patches, feeds=sorted(k for k, b in patches.items() if b.role == "feed"),
        vents=sorted(k for k, b in patches.items() if b.role == "vent"),
        wall_time_s={k: v for k, v in saved["wall_time_s"].items()},
        run={**saved["run"], "reused": True}, notes=list(saved.get("notes", [])))


@dataclass
class Fields3D:
    """The converged field as arrays: everything the post-processing needs, read once."""

    n_cells: int
    owner: np.ndarray
    neighbour: np.ndarray
    phi: np.ndarray                     # internal faces, m^3/s, owner -> neighbour
    boundary: list[dict]
    phi_b: dict[str, np.ndarray]        # per patch, m^3/s, positive OUT of the domain
    U: np.ndarray                       # (n, 3) m/s
    V: np.ndarray                       # m^3
    C: np.ndarray                       # (n, 3) m
    zone_names: list[str]
    zone: np.ndarray
    plan_ij: np.ndarray                 # (n, 2) plan column of each cell
    depth_m: float

    def zone_mask(self, name: str) -> np.ndarray:
        if name not in self.zone_names:
            return np.zeros(self.n_cells, dtype=bool)
        return self.zone == self.zone_names.index(name)

    def boundary_owner(self, patch: dict) -> np.ndarray:
        s, n = patch["startFace"], patch["nFaces"]
        return self.owner[s:s + n]


def read_fields(steady: SteadyCase) -> Fields3D:
    poly = steady.case_dir / "constant" / "polyMesh"
    owner = read_label_list(poly / "owner")
    neighbour = read_label_list(poly / "neighbour")
    n_cells = int(owner.max()) + 1
    n_int = neighbour.size
    td = steady.time_dir
    phi_all = read_internal_field(td / "phi", 1)
    phi = phi_all[:n_int]
    boundary = read_boundary(poly / "boundary")
    phi_b = {}
    phi_text = (td / "phi").read_text(encoding="utf-8", errors="replace")
    for p in boundary:
        vals = read_patch_values(td / "phi", p["name"], p["nFaces"], 1, text=phi_text)
        phi_b[p["name"]] = np.zeros(p["nFaces"]) if vals is None else vals
    U = read_internal_field(td / "U", 3, n_cells)
    V = read_internal_field(td / "V", 1, n_cells)
    C = read_internal_field(td / "C", 3, n_cells)
    names, zone = read_cell_zones(poly / "cellZones", n_cells)
    xy = steady.mesh_meta["xy"]
    dxy = float(xy["dxy_mm"]) * 1e-3
    ox, oy = (float(v) * 1e-3 for v in xy["origin_mm"])
    ij = np.stack([np.floor((C[:, 0] - ox) / dxy), np.floor((C[:, 1] - oy) / dxy)],
                  axis=1).astype(np.int64)
    return Fields3D(n_cells, owner, neighbour, phi, boundary, phi_b, U, V, C, names, zone,
                    ij, float(steady.mesh_meta["z"]["depth_mm"]) * 1e-3)


# ======================================================================================
# 1. mean age and dead zones
# ======================================================================================


@dataclass
class AgeResult:
    age_s: np.ndarray
    engine: str
    theoretical_mean_age_s: float | None
    outlet_mean_age_s: float | None
    consistency_error: float | None
    unreachable_volume_fraction: float
    flow_loops: int
    wall_time_s: float
    notes: list[str]


#: Relative flux, as a fraction of the receiving cell's own outflow, below which a face is
#: dropped to BREAK a closed loop in the flow-ordered sweep. Tried in order; each level is
#: only reached if the previous one left cells in a loop. ASSUMPTION, reported.
LOOP_BREAK_LEVELS = (1e-12, 1e-9, 1e-6, 1e-4, 1e-3, 1e-2)

#: Faces below this multiple of the converged field's largest per-cell continuity error are
#: treated as carrying no flow (see `mean_age_3d`). ASSUMPTION, reported with the result.
CONTINUITY_NOISE_FACTOR = 10.0


def age_sweep(n: int, src: np.ndarray, dst: np.ndarray, w: np.ndarray, volume: np.ndarray,
              outflow_boundary: np.ndarray, *, qtol: float) -> tuple[np.ndarray, dict]:
    """Exact upwind mean age, `out_i a_i - sum_in w_ji a_j = V_i`, by flow-ordered sweep.

    The same forward substitution as `cfd.transport._age_by_sweep` (Kahn's algorithm on the
    flow digraph), vectorised over each FRONTIER so that 600k 3D cells take seconds, not
    minutes. Liquid enters at age 0, so boundary inflow adds nothing to the source.

    A 3D Stokes field has closed loops of two kinds: round-off (faces carrying 1e-26 m^3/s
    in a dead-end line, with either sign) and Moffatt corner eddies, which are real but
    carry a vanishing fraction of the flow. Either stalls a sweep. They are broken by
    dropping the faces whose flux is below a fraction of the receiving cell's outflow
    (LOOP_BREAK_LEVELS), smallest first, and the dropped flux is REPORTED; a cell left
    with no outflow at all is UNREACHABLE (infinite age), which is the dead_end class.
    """
    keep = w > qtol
    src, dst, w = src[keep], dst[keep], w[keep]
    out = np.bincount(src, weights=w, minlength=n) + outflow_boundary
    age = np.full(n, np.nan)
    acc = volume.astype(float).copy()
    done = np.zeros(n, dtype=bool)
    alive = np.ones(src.size, dtype=bool)
    info = {"dropped_faces": 0, "dropped_flux_fraction": 0.0, "loop_break_level": None,
            "rounds": 0}
    total_w = float(w.sum()) or 1.0

    def sweep() -> None:
        order = np.argsort(src[alive], kind="stable")
        e_src, e_dst, e_w = src[alive][order], dst[alive][order], w[alive][order]
        ptr = np.concatenate([[0], np.cumsum(np.bincount(e_src, minlength=n))])
        # an edge from a cell already done has delivered its age into `acc` already
        pending = np.bincount(e_dst[~done[e_src]], minlength=n)
        pending[done] = 0
        frontier = np.flatnonzero((pending == 0) & ~done)
        while frontier.size:
            info["rounds"] += 1
            o = out[frontier]
            with np.errstate(divide="ignore", invalid="ignore"):
                age[frontier] = np.where(o > qtol, acc[frontier] / o, np.inf)
            done[frontier] = True
            starts, ends = ptr[frontier], ptr[frontier + 1]
            cnt = ends - starts
            if not cnt.sum():
                break
            idx = np.repeat(starts - np.cumsum(np.concatenate([[0], cnt[:-1]])), cnt) + \
                np.arange(int(cnt.sum()))
            d = e_dst[idx]
            val = np.repeat(np.where(np.isfinite(age[frontier]), age[frontier], 0.0), cnt)
            np.add.at(acc, d, e_w[idx] * val)
            np.subtract.at(pending, d, 1)
            cand = np.unique(d)
            frontier = cand[(pending[cand] == 0) & ~done[cand]]

    sweep()
    for level in LOOP_BREAK_LEVELS:
        if done.all():
            break
        # faces INTO a still-pending cell from another still-pending cell, small relative
        # to that cell's outflow, are the loop closers.
        stuck = ~done
        rel = w / np.maximum(out[dst], qtol)
        drop = alive & stuck[dst] & stuck[src] & (rel < level)
        if not drop.any():
            continue
        alive &= ~drop
        info["dropped_faces"] += int(drop.sum())
        info["dropped_flux_fraction"] += float(w[drop].sum() / total_w)
        info["loop_break_level"] = level
        sweep()
    info["cells_left_in_loops"] = int((~done).sum())
    if not done.all():
        # whatever is left is a genuine circulation carrying more than 1% of a cell's
        # outflow. Gauss-Jacobi on the remainder, the done cells held fixed.
        rest = ~done & (out > qtol)
        age[~done & (out <= qtol)] = np.inf
        a = np.where(done, age, 0.0)
        a[~np.isfinite(a)] = 0.0
        for it in range(20000):
            inflow = np.bincount(dst[alive], weights=w[alive] * a[src[alive]], minlength=n)
            new = np.where(rest, (volume + inflow) / np.maximum(out, qtol), a)
            change = float(np.max(np.abs(new - a)[rest])) if rest.any() else 0.0
            a = new
            if change <= 1e-10 * max(float(np.max(a[rest])) if rest.any() else 1.0, 1.0):
                break
        info["jacobi_iterations"] = it + 1
        age[rest] = a[rest]
    return age, info


def mean_age_3d(f: Fields3D) -> AgeResult:
    """Mean age per 3D cell from OpenFOAM's own face fluxes (see the module docstring)."""
    t0 = time.perf_counter()
    n_int = f.phi.size
    own, nei, q = f.owner[:n_int], f.neighbour, f.phi
    src = np.where(q > 0, own, nei)
    dst = np.where(q > 0, nei, own)
    w = np.abs(q)
    out_b = np.zeros(f.n_cells)
    q_in = q_out = 0.0
    exit_cells, exit_q = [], []
    for p in f.boundary:
        qb = f.phi_b[p["name"]]
        own_b = f.boundary_owner(p)
        np.add.at(out_b, own_b, np.maximum(qb, 0.0))
        q_in += float(np.sum(np.maximum(-qb, 0.0)))
        q_out += float(np.sum(np.maximum(qb, 0.0)))
        pos = qb > 0
        exit_cells.append(own_b[pos])
        exit_q.append(qb[pos])
    # A face carrying less than the field's own continuity error is numerically zero: the
    # converged simpleFoam field balances each cell only to about 1e-7 of the largest face
    # flux, so a flux below CONTINUITY_NOISE_FACTOR x that imbalance has no direction.
    net = np.bincount(own, weights=q, minlength=f.n_cells) - np.bincount(nei, weights=q, minlength=f.n_cells)
    for p in f.boundary:
        np.add.at(net, f.boundary_owner(p), f.phi_b[p["name"]])
    qmax = max(float(np.max(w)) if w.size else 0.0, q_in)
    qtol = max(qmax * cfd_transport._QTOL_REL, CONTINUITY_NOISE_FACTOR * float(np.max(np.abs(net))))
    age, info = age_sweep(f.n_cells, src, dst, w, f.V, out_b, qtol=qtol)
    info["qtol_m3_s"] = qtol
    info["qtol_rel"] = qtol / qmax if qmax else None
    vol = float(f.V.sum())
    reach = np.isfinite(age)
    # The exact identity: flux-weighted outlet age = REACHED volume / throughput. A capped
    # dead-end line holds volume no liquid ever leaves, so it is excluded from the check
    # and reported separately as unreachable.
    theo = float(f.V[reach].sum()) / q_in if q_in > 0 else None
    ec, eq = np.concatenate(exit_cells), np.concatenate(exit_q)
    ea = age[ec]
    good = np.isfinite(ea)
    outlet = float(np.sum(eq[good] * ea[good]) / np.sum(eq[good])) if good.any() else None
    unreach = float(f.V[~np.isfinite(age)].sum() / vol)
    notes = []
    if info["dropped_faces"]:
        notes.append(
            f"{info['dropped_faces']} face(s) carrying {info['dropped_flux_fraction']:.2e} of "
            f"the total face flux were dropped to break closed flow loops (relative level "
            f"{info['loop_break_level']:g}); see LOOP_BREAK_LEVELS")
    if info["cells_left_in_loops"]:
        notes.append(f"{info['cells_left_in_loops']} cells sit in a real recirculation and "
                     f"were solved by Jacobi iteration")
    if unreach > 0:
        notes.append(f"{unreach * 100:.2f}% of the wetted volume has no convective path out "
                     f"(capped dead-end lines): infinite age, the dead_end class")
    return AgeResult(
        age_s=age, engine="upwind age on OpenFOAM phi, flow-ordered sweep (numpy)",
        theoretical_mean_age_s=theo, outlet_mean_age_s=outlet,
        consistency_error=(abs(outlet / theo - 1.0) if (outlet and theo) else None),
        unreachable_volume_fraction=unreach,
        flow_loops=int(info["cells_left_in_loops"]), wall_time_s=time.perf_counter() - t0,
        notes=notes + [f"sweep: {info}"],
    )


def write_age_field(steady: SteadyCase, age: np.ndarray) -> Path:
    """`<time>/age` as an OpenFOAM volScalarField, so render.py can draw it."""
    from assay2chip.foam import case as foam_case

    finite = np.where(np.isfinite(age), age, -1.0)
    body = "\n".join(f"{v:.6g}" for v in finite)
    bf = {p["name"]: {"type": "zeroGradient"} for p in read_boundary(
        steady.case_dir / "constant" / "polyMesh" / "boundary")}
    path = steady.time_dir / "age"
    foam_case.write_dict(path, {
        "dimensions": foam_case.DIM_TIME,
        "internalField": foam_case.Verbatim(
            f"nonuniform List<scalar>\n{age.size}\n(\n{body}\n);"),
        "boundaryField": bf,
    }, "volScalarField", note="mean age, s; -1 = unreachable (no convective path out)")
    return path


def dead_zones_3d(f: Fields3D, age: np.ndarray, zone: str, *,
                  age_factor: float = cfd_transport.DEAD_ZONE_AGE_FACTOR,
                  speed_fraction: float = cfd_transport.DEAD_ZONE_SPEED_FRACTION) -> dict:
    """Dead-zone volume fraction of one cellZone, per 3D cell AND per plan column.

    Same thresholds as `cfd.transport.dead_zones` (relative to the zone's own
    volume-weighted mean). The COLUMN version depth-averages age (volume-weighted) and
    speed first, which is what Hele-Shaw resolves; the CELL version is the full 3D answer.
    `near_wall_share` is the part of the dead CELL volume that sits in the floor/lid
    layers (bottom or top sixth of the depth): the no-slip contribution the reduced model
    cannot see.
    """
    m = f.zone_mask(zone)
    if not m.any():
        return {"zone": zone, "cells": 0}
    V, a = f.V[m], age[m]
    sp = np.linalg.norm(f.U[m], axis=1)
    fin = np.isfinite(a)
    vt = float(V.sum())
    mean_age = float(np.sum(V[fin] * a[fin]) / V[fin].sum()) if fin.any() else math.inf
    mean_sp = float(np.sum(V * sp) / vt)
    long_age = (~fin) | (a > age_factor * mean_age)
    slow = sp < speed_fraction * mean_sp
    dead = long_age | slow
    z = f.C[m, 2] / f.depth_m
    wall_layer = (z < 1 / 6) | (z > 5 / 6)

    # depth-averaged plan columns
    key = f.plan_ij[m, 0] * 1_000_003 + f.plan_ij[m, 1]
    uniq, inv = np.unique(key, return_inverse=True)
    cv = np.bincount(inv, weights=V)
    a_fin = np.where(fin, a, 0.0)
    col_inf = np.bincount(inv, weights=(~fin).astype(float)) > 0
    col_age = np.bincount(inv, weights=V * a_fin) / cv
    col_age[col_inf] = math.inf
    col_u = np.stack([np.bincount(inv, weights=V * f.U[m, k]) for k in range(2)], 1) / cv[:, None]
    col_sp = np.linalg.norm(col_u, axis=1)
    cfin = np.isfinite(col_age)
    col_mean_age = float(np.sum(cv[cfin] * col_age[cfin]) / cv[cfin].sum()) if cfin.any() else math.inf
    col_mean_sp = float(np.sum(cv * col_sp) / cv.sum())
    col_dead = (~cfin) | (col_age > age_factor * col_mean_age) | (col_sp < speed_fraction * col_mean_sp)
    order = np.argsort(np.where(fin, a, np.inf))
    cum = np.cumsum(V[order]) / vt
    return {
        "zone": zone,
        "cells": int(m.sum()),
        "volume_ul": vt / M3_PER_UL,
        "mean_age_s": mean_age,
        "p95_age_s": float(a[order][min(int(np.searchsorted(cum, 0.95)), a.size - 1)]),
        "max_finite_age_s": float(a[fin].max()) if fin.any() else None,
        "mean_speed_mm_s": mean_sp * 1e3,
        "dead_volume_fraction_cells": float(V[dead].sum() / vt),
        "long_age_volume_fraction_cells": float(V[long_age].sum() / vt),
        "low_speed_volume_fraction_cells": float(V[slow].sum() / vt),
        "unreachable_volume_fraction": float(V[~fin].sum() / vt),
        "dead_volume_fraction_columns": float(cv[col_dead].sum() / cv.sum()),
        "near_wall_share_of_dead": (float(V[dead & wall_layer].sum() / V[dead].sum())
                                    if dead.any() else 0.0),
        "thresholds": {"age_factor": age_factor, "speed_fraction": speed_fraction,
                       "source": "cfd.transport.DEAD_ZONE_* (ASSUMPTIONS, not contract values)"},
    }


def zone_inflow_m3_s(f: Fields3D, zone: str) -> float:
    """Volumetric flow ENTERING one cellZone across its faces (internal and patch)."""
    m = f.zone_mask(zone)
    o_in, n_in = m[f.owner[:f.phi.size]], m[f.neighbour]
    cross = o_in ^ n_in
    into = np.where(n_in[cross], f.phi[cross], -f.phi[cross])
    total = float(np.sum(np.maximum(into, 0.0)))
    for p in f.boundary:
        own = f.boundary_owner(p)
        inside = m[own]
        total += float(np.sum(np.maximum(-f.phi_b[p["name"]][inside], 0.0)))
    return total


# ======================================================================================
# 2. washout with scalarTransportFoam on the frozen field
# ======================================================================================


@dataclass
class Washout3D:
    zone: str
    volume_ul: float
    q_ul_min: float
    t_s: np.ndarray
    fraction: np.ndarray
    cv: np.ndarray
    diffusivity_m2_s: float
    steps: int
    dt_s: float
    wall_time_s: float
    case_dir: str

    def fraction_at_cv(self, cv: float) -> float:
        return float(np.interp(cv, self.cv, self.fraction))

    def cv_to_reach(self, target: float) -> float | None:
        hit = np.flatnonzero(self.fraction >= target)
        if not hit.size:
            return None
        k = int(hit[0])
        if k == 0:
            return float(self.cv[0])
        x0, x1, y0, y1 = self.cv[k - 1], self.cv[k], self.fraction[k - 1], self.fraction[k]
        return float(x0 + (target - y0) * (x1 - x0) / (y1 - y0)) if y1 > y0 else float(x1)


def run_washout(steady: SteadyCase, f: Fields3D, zones: Sequence[str], *, t_end_s: float,
                steps: int, diffusivity_m2_s: float, n_proc: int = 6,
                timeout_s: float = 3600.0) -> dict[str, Washout3D]:
    """March old liquid (T = 1 in `zones`) out with scalarTransportFoam on frozen U, phi."""
    from assay2chip.foam import case as foam_case
    from assay2chip.foam import flow
    from assay2chip.foam import runner

    wd = steady.case_dir.parent / f"{steady.case_dir.name}_washout"
    if wd.exists():
        shutil.rmtree(wd)
    (wd / "0").mkdir(parents=True)
    shutil.copytree(steady.case_dir / "constant" / "polyMesh", wd / "constant" / "polyMesh")
    for name in ("U", "phi"):
        shutil.copy(steady.time_dir / name, wd / "0" / name)
    mask = np.zeros(f.n_cells, dtype=bool)
    for z in zones:
        mask |= f.zone_mask(z)
    body = "\n".join("1" if x else "0" for x in mask)
    bf: dict[str, Any] = {}
    for p in f.boundary:
        role = steady.patches.get(p["name"])
        if role is not None and role.role == "feed":
            bf[p["name"]] = {"type": "fixedValue", "value": "uniform 0"}
        elif role is not None and role.role == "vent":
            bf[p["name"]] = {"type": "inletOutlet", "inletValue": "uniform 0",
                             "value": "uniform 0"}
        else:
            bf[p["name"]] = {"type": "zeroGradient"}
    foam_case.write_dict(wd / "0" / "T", {
        "dimensions": foam_case.DIM_NONE,
        "internalField": foam_case.Verbatim(f"nonuniform List<scalar>\n{mask.size}\n(\n{body}\n);"),
        "boundaryField": bf,
    }, "volScalarField", note="old liquid = 1 in the target chamber(s), fresh = 0")
    foam_case.write_dict(wd / "constant" / "transportProperties", {
        "DT": foam_case.Dimensioned(diffusivity_m2_s, foam_case.DIM_NU)})
    dt = t_end_s / steps
    functions = {
        f"T_{z}": {"type": "volFieldValue", "libs": ["fieldFunctionObjects"],
                   "regionType": "cellZone", "name": z, "operation": "volAverage",
                   "fields": ["T"], "writeFields": False, "writeControl": "timeStep",
                   "writeInterval": 1, "log": False}
        for z in zones
    }
    foam_case.write_dict(wd / "system" / "controlDict", foam_case.control_dict(
        application="scalarTransportFoam", end_time=t_end_s, delta_t=dt,
        write_interval=steps, functions=functions, purge_write=1))
    foam_case.write_dict(wd / "system" / "fvSchemes", {
        "ddtSchemes": {"default": "CrankNicolson 0.9"},
        "gradSchemes": {"default": "Gauss linear"},
        "divSchemes": {"default": "none", "div(phi,T)": "Gauss limitedLinear01 1"},
        "laplacianSchemes": {"default": "Gauss linear corrected"},
        "interpolationSchemes": {"default": "linear"},
        "snGradSchemes": {"default": "corrected"},
    })
    foam_case.write_dict(wd / "system" / "fvSolution", {
        "solvers": {"T": {"solver": "PBiCGStab", "preconditioner": "DILU",
                          "tolerance": 1e-8, "relTol": 0}},
        "SIMPLE": {"nNonOrthogonalCorrectors": 0},
    })
    foam_case.write_dict(wd / "system" / "decomposePar" "Dict",
                         foam_case.decompose_par_dict(max(n_proc, 1)))
    t0 = time.perf_counter()
    res = runner.run(wd, "scalarTransportFoam", parallel=(n_proc if n_proc > 1 else None),
                     timeout_s=timeout_s, check=False, steady=False)
    wall = time.perf_counter() - t0
    if not res.ok:
        raise TransportError(f"scalarTransportFoam failed in {wd}: {res.verdict}\n{res.tail(20)}")
    out: dict[str, Washout3D] = {}
    dats = flow.find_dat_files(wd)
    for z in zones:
        path = next((p for k, p in dats.items() if k.split("/")[0] == f"T_{z}"), None)
        if path is None:
            raise TransportError(f"no volFieldValue output for zone {z} in {wd}")
        table = flow.read_dat(path)
        col = next(c for c in table.columns if c.startswith("volAverage"))
        t = np.array(table.column(table.columns[0]), dtype=float)
        tbar = np.array(table.column(col), dtype=float)
        m = f.zone_mask(z)
        v = float(f.V[m].sum())
        q = zone_inflow_m3_s(f, z)
        t = np.concatenate([[0.0], t])
        frac = np.concatenate([[0.0], 1.0 - tbar])
        out[z] = Washout3D(z, v / M3_PER_UL, q / M3_PER_UL * S_PER_MIN, t, frac,
                           t * q / v if v > 0 else t * 0, diffusivity_m2_s, steps, dt, wall,
                           str(wd))
    for p in wd.glob("processor*"):
        shutil.rmtree(p, ignore_errors=True)
    return out


# ======================================================================================
# 3. the reduced model on the same chip and step, and the comparison
# ======================================================================================


def reduced_counterpart(chip: dict, record: Any, raw_step: Any, chambers: Sequence[str],
                        diffusivity_m2_s: float) -> dict:
    """cfd/transport.py on the same chip, step and diffusivity."""
    from assay2chip.cfd import mesh as cfd_mesh
    from assay2chip.cfd import solve as cfd_solve

    t0 = time.perf_counter()
    mesh = cfd_mesh.build_mesh(chip, grid_mm=0.25)
    fld = cfd_solve.solve_step(mesh, record.scenario.for_mesh(mesh))
    topo = cfd_transport.build_topology(mesh, fld)
    age = cfd_transport.mean_age(mesh, fld, topology=topo)
    out: dict[str, Any] = {"age": age.to_json(), "chambers": {}}
    for cid in chambers:
        entry: dict[str, Any] = {}
        try:
            entry["dead_zones"] = cfd_transport.dead_zones(mesh, fld, age, cid,
                                                           topology=topo).to_json()
        except ValueError as exc:
            entry["dead_zones"] = {"error": str(exc)}
        if raw_step is not None or record is not None:
            try:
                chk = cfd_transport.check_exchange(
                    mesh, fld, raw_step if raw_step is not None else record, cid,
                    diffusivity_m2_s=diffusivity_m2_s, chip_id=chip.get("id"), topology=topo)
                entry["exchange"] = chk.to_json()
            except ValueError as exc:
                entry["exchange"] = {"error": str(exc)}
        out["chambers"][cid] = entry
    out["wall_time_s"] = time.perf_counter() - t0
    out["chamber_ids"] = cfd_transport.chamber_ids(mesh)
    return out


def _explain(d_cv: float | None, dz3: dict, dz_red: dict, re_max: float | None) -> str:
    """Why the 3D and reduced numbers differ, from the evidence in hand."""
    parts = []
    nw = dz3.get("near_wall_share_of_dead")
    cells = dz3.get("dead_volume_fraction_cells")
    cols = dz3.get("dead_volume_fraction_columns")
    if d_cv is not None and d_cv > 0.05:
        parts.append(
            f"the 3D chamber needs {d_cv * 100:.0f}% more volume: the velocity profile is "
            f"parabolic across the depth, so liquid at the floor and lid moves far slower "
            f"than the depth-averaged Hele-Shaw speed and leaves only by diffusing into the "
            f"core (Taylor-Aris dispersion). The reduced model is plug flow in z and cannot "
            f"see that tail")
    elif d_cv is not None and d_cv < -0.05:
        parts.append(
            f"the 3D chamber clears with {-d_cv * 100:.0f}% LESS volume than the reduced "
            f"model predicts: cross-depth shear spreads the front and diffusion then mixes "
            f"the plan-view corners that a 0.25 mm Hele-Shaw grid treats as stagnant")
    if cells is not None and cols is not None and nw is not None and cells > cols + 0.01:
        parts.append(
            f"{(cells - cols) * 100:.1f} points of the 3D dead volume exist only per cell, not "
            f"per depth-averaged column, and {nw * 100:.0f}% of the dead cells sit in the "
            f"floor/lid layers: that is the no-slip wall effect, absent by construction "
            f"from a depth-averaged model")
    red = dz_red.get("dead_volume_fraction")
    if cols is not None and red is not None and abs(cols - red) > 0.05:
        parts.append(
            f"in plan the 3D column dead fraction ({cols * 100:.1f}%) and the reduced "
            f"model's ({red * 100:.1f}%) differ by more than 5 points: side walls (the 3D "
            f"field resolves the no-slip boundary layer of a narrow chamber, Hele-Shaw "
            f"takes plug flow up to the wall) and the two grids (0.25 mm reduced, "
            f"{''}3D at the hex spacing) both contribute")
    if re_max is not None and re_max > 1:
        parts.append(f"Re reaches {re_max:.3g} > 1 in 3D, so inertia and real recirculation "
                     f"are present that the reduced model cannot represent")
    return "; ".join(parts) or ("the two agree within 5%: at Re << 1 in a chamber swept end "
                                "to end, depth-averaging costs little")


# ======================================================================================
# the whole check for one chip and one step
# ======================================================================================


def _fix_frame(chip_id: str) -> str:
    if chip_id in FROZEN_CHIPS:
        return (f"{chip_id} is printed and FROZEN; this is a FULL-SPEC design improvement "
                f"for the next revision, not a change to the printed chip: ")
    return "design change: "


def _finding(chip_id: str, step: Any, cls: str, severity: str, feature: str, metric: str,
             value: float | None, limit: float | None, units: str, message: str,
             fix: str, audit: dict, assumption: bool = False) -> Finding:
    return Finding(
        id=f"openfoam:{cls}:{chip_id}:{step}:{feature}:{metric}", cls=cls, severity=severity,
        chip_id=chip_id, feature_id=feature, step=step, metric=metric,
        value=None if value is None or not math.isfinite(value) else float(value),
        limit=limit, units=units, message=message, fix_hint=_fix_frame(chip_id) + fix,
        source=SOURCE, assumption=assumption, audit=audit,
    )


def check_step(chip_id: str, step: int | str | None = None, *, fidelity_name: str = "fast",
               n_proc: int = 6, work_dir: Path | None = None, species: str | None = None,
               washout: bool = True, timeout_s: float = 3600.0, reuse: bool = False) -> dict:
    """Mean age, dead zones and washout in 3D for one step, cross-validated."""
    t_all = time.perf_counter()
    fid = fidelity(fidelity_name)
    chip, _, protocol, record, raw = load_step(chip_id, step)
    work = Path(work_dir) if work_dir else DEFAULT_WORK / "foam_transport"
    steady = solve_steady(chip, record, work / chip_id / f"step_{record.step}", fid=fid,
                          n_proc=n_proc, timeout_s=timeout_s, reuse=reuse)
    t0 = time.perf_counter()
    f = read_fields(steady)
    steady.wall_time_s["read_fields"] = time.perf_counter() - t0
    age = mean_age_3d(f)
    steady.wall_time_s["mean_age"] = age.wall_time_s
    write_age_field(steady, age.age_s)

    species = species or (raw or {}).get("reagent") or "dye"
    diff = cfd_transport.diffusivity("dye" if species == "dye" else str(species))
    targets = [t for t in (record.targets or []) if t in f.zone_names]
    red = reduced_counterpart(chip, record, raw if record.operation == "exchange" else None,
                              targets, diff.m2_s)
    chambers = [c for c in red["chamber_ids"] if c in f.zone_names]
    dz = {c: dead_zones_3d(f, age.age_s, c) for c in chambers}

    findings: list[Finding] = []
    cross: dict[str, Any] = {"mean_age": {}, "dead_zones": {}, "washout": {}}
    u_max = float(np.max(np.linalg.norm(f.U, axis=1)))
    re_max = steady.fluid.reynolds(u_max, f.depth_m) if hasattr(steady.fluid, "reynolds") else None

    for c in chambers:
        rz = (red["chambers"].get(c) or {}).get("dead_zones") or {}
        cross["dead_zones"][c] = {
            "dead_fraction_3d_cells": dz[c].get("dead_volume_fraction_cells"),
            "dead_fraction_3d_columns": dz[c].get("dead_volume_fraction_columns"),
            "dead_fraction_reduced": rz.get("dead_volume_fraction"),
            "mean_age_3d_s": dz[c].get("mean_age_s"),
            "mean_age_reduced_s": rz.get("mean_age_s"),
            "near_wall_share_of_dead_3d": dz[c].get("near_wall_share_of_dead"),
            "flushed_this_step": (dz[c].get("unreachable_volume_fraction") or 0) < 0.999,
        }
        frac = dz[c].get("dead_volume_fraction_columns") or 0.0
        if frac >= 0.05 and math.isfinite(dz[c].get("mean_age_s") or math.inf):
            sev = "high" if frac >= 0.2 else "medium"
            in_target = c in targets
            findings.append(_finding(
                steady.chip_id, record.step, "trap", sev if in_target else "low", c,
                "dead_zone_volume_fraction", frac,
                0.05, "fraction of chamber volume",
                f"{frac * 100:.1f}% of {c} (depth-averaged columns; {dz[c]['dead_volume_fraction_cells'] * 100:.1f}% "
                f"of its 3D cells) is a long-age or low-speed zone in step {record.step} "
                f"({record.operation}), SIMULATED in 3D (OpenFOAM simpleFoam, "
                f"{steady.mesh_meta['cells']} cells). Mean age {dz[c]['mean_age_s']:.3g} s; "
                f"the reduced model puts it at "
                f"{(rz.get('dead_volume_fraction') or 0) * 100:.1f}%.",
                "feed the chamber from a distributor or across its full width, or move the "
                "outlet to the far corner so the slow zone is on the through-path",
                {"dead_zones_3d": dz[c], "reduced": rz}, assumption=True))
    cross["mean_age"] = {
        "reached_volume_over_Q_s": age.theoretical_mean_age_s,
        "outlet_mean_age_3d_s": age.outlet_mean_age_s,
        "consistency_error_3d": age.consistency_error,
        "flow_loops_3d": age.flow_loops,
        "unreachable_volume_fraction_3d": age.unreachable_volume_fraction,
        "reduced": red["age"],
    }

    washouts: dict[str, Any] = {}
    if washout and record.operation == "exchange" and targets:
        for c in targets:
            chk = (red["chambers"].get(c) or {}).get("exchange") or {}
            intent = chk.get("intent") or {}
            target = intent.get("target_fraction")
            cv_dose = intent.get("chamber_volumes_delivered")
            if cv_dose is None:
                continue
            if intent.get("bounded_above_by_chamber_volume"):
                cv_dose = min(cv_dose, 1.0)
            red_w = chk.get("washout") or {}
            cv_red = red_w.get("chamber_volumes_required_cfd")
            # 3D tails are LONGER than Hele-Shaw's (module4 sub_1: 3.05 vs 1.84), so march
            # well past the reduced requirement before calling the target unreached
            march = max(cv_dose, 2.5 * cv_red if cv_red else fid.washout_cv_cap)
            march = min(max(march, cv_dose), max(fid.washout_cv_cap, cv_dose))
            q = zone_inflow_m3_s(f, c)
            v = float(f.V[f.zone_mask(c)].sum())
            if q <= 0:
                continue
            t_end = march * v / q
            n_steps = max(int(math.ceil(march * fid.washout_steps_per_cv)), 20)
            w = run_washout(steady, f, [c], t_end_s=t_end, steps=n_steps,
                            diffusivity_m2_s=diff.m2_s, n_proc=n_proc,
                            timeout_s=timeout_s)[c]
            steady.wall_time_s[f"washout_{c}"] = w.wall_time_s
            reached = w.fraction_at_cv(cv_dose)
            cv3 = w.cv_to_reach(target) if target is not None else None
            d_cv = (cv3 / cv_red - 1.0) if (cv3 and cv_red) else (
                (march / cv_red - 1.0) if cv_red else None)   # a lower bound when unreached
            zm = f.zone_mask(c)
            re_zone = steady.fluid.reynolds(float(np.max(np.linalg.norm(f.U[zm], axis=1))),
                                            f.depth_m)
            why = _explain(d_cv, dz.get(c, {}), (red["chambers"].get(c) or {}).get("dead_zones") or {},
                           re_zone)
            washouts[c] = {
                "chamber_volume_ul_3d": w.volume_ul,
                "chamber_volume_ul_reduced": red_w.get("chamber_volume_ul"),
                "q_into_chamber_ul_min_3d": w.q_ul_min,
                "q_into_chamber_ul_min_reduced": red_w.get("flow_ul_min"),
                "chamber_volumes_delivered": cv_dose,
                "exchange_kind": intent.get("exchange_kind"),
                "exchange_kind_source": intent.get("exchange_kind_source"),
                "target_fraction": target,
                "target_source": intent.get("target_fraction_source"),
                "fraction_reached_3d": reached,
                "fraction_reached_reduced": red_w.get("fraction_reached_cfd"),
                "fraction_reached_well_mixed": red_w.get("fraction_reached_well_mixed"),
                "chamber_volumes_required_3d": cv3,
                "chamber_volumes_required_reduced": cv_red,
                "chamber_volumes_required_well_mixed": red_w.get("chamber_volumes_required_well_mixed"),
                "marched_to_chamber_volumes": march,
                "relative_difference_cv_required": d_cv,
                "why_they_differ": why,
                "model_believed": ("3D (OpenFOAM): it resolves the depth profile and the side "
                                   "walls the reduced model averages away"
                                   if d_cv is None or abs(d_cv) > 0.05 else "both"),
                "diffusivity": diff.to_json(),
                "steps": w.steps, "dt_s": w.dt_s,
                "curve_cv_fraction": [[round(float(a), 4), round(float(b), 6)]
                                      for a, b in zip(w.cv[::max(1, w.cv.size // 48)],
                                                      w.fraction[::max(1, w.cv.size // 48)])],
                "case_dir": w.case_dir,
            }
            if target is not None and reached + 1e-9 < target:
                short = target - reached
                need = f"{cv3:.2f}" if cv3 else f"> {march:.1f}"
                sev = "blocker" if (intent.get("exchange_kind") == "sweep" and cv_dose < 1) else (
                    "high" if short > 0.05 else "medium")
                findings.append(_finding(
                    steady.chip_id, record.step, "exchange", sev, c,
                    "exchange_fraction_reached", reached, target, "fraction",
                    f"step {record.step} ({intent.get('exchange_kind')}) sweeps {cv_dose:.2f} "
                    f"chamber volumes through {c} and reaches {reached * 100:.2f}% in 3D "
                    f"(SIMULATED, scalarTransportFoam on the converged field), short of "
                    f"{target * 100:.1f}%. This chamber needs {need} chamber volumes in 3D "
                    f"against {cv_red if cv_red is None else round(cv_red, 2)} in the reduced "
                    f"model and {red_w.get('chamber_volumes_required_well_mixed') or 0:.2f} well "
                    f"mixed. Why the models differ: {why}.",
                    (f"sweep at least {cv3:.2f} chamber volumes ({cv3 * w.volume_ul:.1f} uL)"
                     if cv3 else f"sweep more than {march:.1f} chamber volumes")
                    + " or reshape the chamber so the feed and drain sit at opposite ends "
                      "of its long axis",
                    {"washout": washouts[c]}, assumption=diff.assumption))
    cross["washout"] = washouts
    total = time.perf_counter() - t_all
    unresolved = [diff.unresolved_note] if diff.unresolved_note else []
    return {
        "schema": SCHEMA,
        "chip_id": steady.chip_id,
        "step": record.step,
        "operation": record.operation,
        "targets": targets,
        "label": "SIMULATED" + (" - DEMO SCALE" if chip.get("scale", "demo") == "demo" else ""),
        "fidelity": fid.to_json(),
        "steady": steady.summary(),
        "reynolds_max_3d": re_max,
        "age": {k: v for k, v in age.__dict__.items() if k != "age_s"},
        "dead_zones_3d": dz,
        "reduced": {k: v for k, v in red.items() if k != "chamber_ids"},
        "cross_validation": cross,
        "findings": [x.to_json() for x in findings],
        "assumptions": {**cfd_transport.ASSUMPTIONS, "diffusivity": diff.to_json()},
        "unresolved": unresolved,
        "wall_time_s": {**{k: round(v, 2) for k, v in steady.wall_time_s.items()},
                        "reduced_model": round(red.get("wall_time_s", 0.0), 2),
                        "total": round(total, 2)},
    }


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chip", required=True)
    ap.add_argument("--step", default=None)
    ap.add_argument("--fidelity", choices=("fast", "high"), default="fast")
    ap.add_argument("--np", type=int, default=6, dest="n_proc")
    ap.add_argument("--no-washout", action="store_true")
    ap.add_argument("--work", type=Path, default=None)
    ap.add_argument("--reuse", action="store_true",
                    help="reuse a converged steady case with the same mesh, step and contracts")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    res = check_step(args.chip, args.step, fidelity_name=args.fidelity, n_proc=args.n_proc,
                     work_dir=args.work, washout=not args.no_washout, reuse=args.reuse)
    out = (Path(res["steady"]["case_dir"]).parent / f"transport_step_{res['step']}.json")
    out.write_text(json.dumps(res, indent=2, default=str) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        print(f"{res['chip_id']} step {res['step']} ({res['operation']})  {res['label']}  "
              f"{res['steady']['cells']} cells  wall {res['wall_time_s']}")
        for c, w in res["cross_validation"]["washout"].items():
            print(f"  washout {c}: 3D {w['fraction_reached_3d']:.4f} vs reduced "
                  f"{w['fraction_reached_reduced']} at {w['chamber_volumes_delivered']:.2f} CV; "
                  f"CV to target 3D {w['chamber_volumes_required_3d']} vs reduced "
                  f"{w['chamber_volumes_required_reduced']}  -- {w['why_they_differ']}")
        for c, d in res["cross_validation"]["dead_zones"].items():
            print(f"  dead {c}: 3D cells {d['dead_fraction_3d_cells']:.3f} columns "
                  f"{d['dead_fraction_3d_columns']:.3f} reduced {d['dead_fraction_reduced']}")
        for x in res["findings"]:
            print(f"  [{x['severity']}] {x['class']} {x['feature_id']}: {x['message'][:300]}")
        print(f"  json: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

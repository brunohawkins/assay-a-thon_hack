"""assay2chip/foam/particles.py - cells, capsules and beads as Lagrangian parcels in 3D.

    python assay2chip/foam/particles.py --chip module4_v1 --step 1
    python assay2chip/foam/particles.py --crossval            # the three reduced-order findings
    python assay2chip/foam/particles.py --weir-sweep          # module3_v1_translated's gap band

    from assay2chip.foam import particles
    result = particles.analyse_step("module4_v1", step=1)      # needs OpenFOAM

The 3D counterpart of `assay2chip/cfd/particles.py`. Parcels are ONE-WAY coupled to the
frozen, converged `simpleFoam` field (`transport.solve_steady`) and integrated by
`icoUncoupledKinematicParcelFoam` with Stokes-regime drag (`sphereDrag`, which is
Schiller-Naumann and reduces to Stokes at the particle Re ~1e-4 here) and gravity along -z
(`gravity`, which is buoyancy-corrected: `m g (1 - rho_c / rho_p)`). The chip lies flat, so
gravity is normal to the plane, exactly as the reduced model assumes - but here the parcel
falls through the REAL parabolic depth profile instead of a depth-averaged one.

WHAT IS IMPORTED, NOT RESTATED. Everything that decides what a result MEANS comes from the
reduced model: the particle classes and their diameter bands
(`cfd.particles.particle_classes`), the excess-density bands from the contracts
(`cell. / capsule. / bead.excess_density_kg_m3`, read through `contracts.param_range` by
`cfd.particles.excess_density`), which class is where in which step
(`_particle_sources`), what settling means in each feature (`sediment_intent`), and the
weir retention rule (`trap.gap_factor_high`). This module only adds the 3D trajectories.

THE CONSERVATIVE END, PER QUESTION. Each class is run TWICE:
  loss       the largest, densest plausible particle (diameter max, excess density MAX):
             what a LOSS verdict - settling in a feed line, a drain, a mixer - rests on.
  retention  the smallest, lightest one (diameter min, excess density MIN): what a
             RETENTION verdict - capture in a trap, a weir that must hold - rests on.
Every per-zone verdict is read from the run whose ends match the zone's intent, and every
result names the run, the ends and the contract key it used.

OUTCOMES. Per patch: parcels that reached each outlet (and each feed, if carried
backwards). Per cellZone: parcels that settled on a wall in it (with whether on the floor,
the lid or a side wall/crest), parcels still in flight when the step ended, and the
settled fraction of the parcels that ENTERED the zone - the like-for-like number for the
reduced model's per-edge `settled_fraction`. A parcel that leaves through an outlet that
sits behind a weir (`capsule_passable: false` on the line or its port, a `retention`
chamber on the other end) has crossed the crest; whether that is an ESCAPE is the
contract's size rule (`gap < trap.gap_factor_high x D` retains), because a point parcel
cannot be excluded by size - this is said in every weir result.

FIDELITY AND TIME. `fast` meshes at transport.FAST_CELL_BUDGET and launches 200 parcels
per injector; `high` uses mesh.py's own budget and 1000. A Lagrangian run itself takes
seconds to a minute; the steady flow solve dominates and is shared (`reuse=True`).

The six printed demo chips are FROZEN: every fix here is a FULL-SPEC design improvement.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.cfd import particles as cfd_particles  # noqa: E402
from assay2chip.foam import transport as ft  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402

SCHEMA = "foam-particles/1"
SOURCE = ft.SOURCE
G_M_S2 = cfd_particles.G_M_S2
DEFAULT_WORK = ft.DEFAULT_WORK

#: Fluid time steps per Lagrangian run. The parcels sub-cycle inside each step on the
#: cloud's own Courant limit (`maxCo`), so this sets only the output resolution.
STEPS_PER_RUN = {"fast": 400, "high": 1000}
#: Parcel Courant number: the fraction of a cell a parcel may cross per sub-step.
PARCEL_MAX_CO = 0.3
#: A stuck parcel within this fraction of the depth of z = 0 is on the floor (and of
#: z = depth, on the lid); anything else touched a side wall or a weir crest.
FLOOR_BAND = 0.05
#: Patch injection is spread over this fraction of the run (capped at 1 s), so the cohort
#: samples the inlet face without smearing the arrival time.
INJECT_FRACTION = 0.05

RUNS = ("loss", "retention")
INTENT_FOR_RUN = {"loss": "loss", "retention": "desired"}


@dataclass
class ParcelRun:
    particle: str
    run: str                  # loss | retention
    diameter_um: float
    diameter_end: str
    excess_density_kg_m3: float
    density_end: str
    density_key: str
    rho_particle_kg_m3: float
    stokes_settling_um_s: float
    injectors: list[dict]
    end_time_s: float
    case_dir: str
    wall_time_s: float = 0.0
    injected: int = 0
    reached: dict[str, int] = dc_field(default_factory=dict)
    settled: dict[str, dict] = dc_field(default_factory=dict)
    in_flight: dict[str, int] = dc_field(default_factory=dict)
    landing_distance_mm: dict[str, float] = dc_field(default_factory=dict)
    notes: list[str] = dc_field(default_factory=list)

    def to_json(self) -> dict:
        d = dict(self.__dict__)
        n = max(self.injected, 1)
        d["fractions"] = {
            "reached_outlet": sum(self.reached.values()) / n,
            "settled": sum(v["total"] for v in self.settled.values()) / n,
            "in_flight_at_end": sum(self.in_flight.values()) / n,
        }
        return d


# ======================================================================================
# the OpenFOAM case
# ======================================================================================


def _size(d_m: float) -> dict:
    return {"type": "fixedValue", "fixedValueDistribution": {"value": d_m}}


def patch_injector(patch: str, n: int, duration_s: float, d_m: float) -> dict:
    return {"type": "patchInjection", "SOI": 0, "duration": duration_s,
            "parcelBasisType": "fixed", "nParticle": 1, "massTotal": 1e-9,
            "patch": patch, "parcelsPerSecond": n / duration_s,
            "velocityType": "patchValue", "flowRateProfile": "constant 1",
            "sizeDistribution": _size(d_m)}


def zone_injector(zone: str, n: int, zone_volume_m3: float, d_m: float) -> dict:
    return {"type": "cellZoneInjection", "SOI": 0, "parcelBasisType": "fixed",
            "nParticle": 1, "massTotal": 1e-9, "cellZone": zone,
            "numberDensity": n / zone_volume_m3, "U0": (0, 0, 0),
            "sizeDistribution": _size(d_m)}


def write_parcel_case(steady: ft.SteadyCase, wd: Path, *, rho_p: float,
                      injectors: dict[str, dict], interactions: dict[str, str],
                      end_time_s: float, steps: int) -> None:
    """One `icoUncoupledKinematicParcelFoam` case on the frozen steady U and phi."""
    from assay2chip.foam import case as foam_case

    if wd.exists():
        shutil.rmtree(wd)
    (wd / "0").mkdir(parents=True)
    shutil.copytree(steady.case_dir / "constant" / "polyMesh", wd / "constant" / "polyMesh")
    for name in ("U", "phi"):
        shutil.copy(steady.time_dir / name, wd / "0" / name)
    fluid = steady.fluid
    foam_case.write_dict(wd / "constant" / "transportProperties", {
        "transportModel": "Newtonian",
        "nu": foam_case.Dimensioned(fluid.nu_m2_s, foam_case.DIM_NU),
        "rhoInf": foam_case.Dimensioned(fluid.rho_kg_m3, foam_case.DIM_DENSITY),
    }, note=f"{fluid.name}: {fluid.source}")
    foam_case.write_dict(wd / "constant" / foam_case.laminar_model_file(),
                         foam_case.momentum_transport())
    foam_case.write_dict(wd / "constant" / "g", {
        "dimensions": foam_case.Dimensions(length=1, time=-2),
        "value": (0, 0, -G_M_S2),
    }, "uniformDimensionedVectorField", note="the chip lies flat: gravity is -z")
    rules = "\n".join(f'        {name} {{ type {kind}; }}' for name, kind in interactions.items())
    cloud = {
        "solution": {
            "active": True, "coupled": False, "transient": True,
            "cellValueSourceCorrection": False, "maxCo": PARCEL_MAX_CO,
            "sourceTerms": {"schemes": {}},
            "interpolationSchemes": {"rho": "cell", "U": "cellPoint", "mu": "cell"},
            "integrationSchemes": {"U": "analytical"},
        },
        "constantProperties": {"rho0": rho_p, "youngsModulus": 1e9, "poissonsRatio": 0.35},
        "subModels": {
            "particleForces": {"sphereDrag": {}, "gravity": {}},
            "injectionModels": injectors,
            "dispersionModel": "none",
            "patchInteractionModel": "localInteraction",
            "localInteractionCoeffs": {
                "patches": foam_case.Verbatim(f"(\n{rules}\n    );"),
                "writeToFile": True,
            },
            "surfaceFilmModel": "none",
            "stochasticCollisionModel": "none",
            "collisionModel": "none",
        },
        "cloudFunctions": {
            "hits": {"type": "particlePostProcessing", "maxStoredParcels": 1_000_000,
                     "patches": [p for p, k in interactions.items() if k == "escape"]},
        },
    }
    foam_case.write_dict(wd / "constant" / "kinematicCloudProperties", cloud)
    foam_case.write_dict(wd / "system" / "controlDict", foam_case.control_dict(
        application="icoUncoupledKinematicParcelFoam", end_time=end_time_s,
        delta_t=end_time_s / steps, write_interval=steps, purge_write=0))
    foam_case.write_dict(wd / "system" / "fvSchemes", foam_case.fv_schemes(steady=False))
    foam_case.write_dict(wd / "system" / "fvSolution", {
        "solvers": {}, "PIMPLE": {"nOuterCorrectors": 1, "nCorrectors": 1,
                                  "nNonOrthogonalCorrectors": 0}})


def _read_cloud(tdir: Path) -> dict[str, np.ndarray]:
    """The parcels still in the cloud at a written time: position, cell, active, age."""
    cdir = tdir / "lagrangian" / "kinematicCloud"
    if not (cdir / "positions").exists():
        return {"xyz": np.zeros((0, 3)), "cell": np.zeros(0, dtype=np.int64),
                "active": np.zeros(0, dtype=bool), "age": np.zeros(0)}
    text = (cdir / "positions").read_text(encoding="utf-8", errors="replace")
    start = text.find("(", text.find("}", text.find("FoamFile")))
    rows = text[start + 1:text.rfind(")")].replace("(", " ").replace(")", " ").split()
    arr = np.array(rows, dtype=float).reshape(-1, 4) if rows else np.zeros((0, 4))

    def _scalar(name: str, default: float) -> np.ndarray:
        p = cdir / name
        if not p.exists():
            return np.full(arr.shape[0], default)
        t = p.read_text(encoding="utf-8", errors="replace")
        body = t[t.find("}", t.find("FoamFile")) + 1:]
        if "{" in body:   # uniform list: N{value}
            n, v = body.strip().split("{")
            return np.full(int(n.strip().split()[-1]), float(v.split("}")[0]))
        vals, _ = ft._list_block(ft._strip_comments(body), 0)
        return vals

    return {"xyz": arr[:, :3], "cell": arr[:, 3].astype(np.int64),
            "active": _scalar("active", 1.0) > 0.5, "age": _scalar("age", math.nan)}


def _escapes(wd: Path, patches: Sequence[str]) -> dict[str, int]:
    """Parcels that left through each patch, from the particlePostProcessing records."""
    out = {p: 0 for p in patches}
    base = wd / "postProcessing" / "lagrangian" / "kinematicCloud" / "hits"
    for p in patches:
        for f in base.glob(f"*/{p}.dat"):
            out[p] += sum(1 for line in f.read_text(errors="replace").splitlines()
                          if line.strip() and not line.startswith("#"))
    return out


def _injected(wd: Path) -> int:
    """Parcels injected, from the last `Injector <name>: - parcels added = N` per injector."""
    import re

    log = (wd / "log.icoUncoupledKinematicParcelFoam").read_text(errors="replace")
    last: dict[str, int] = {}
    for name, n in re.findall(r"Injector (\S+):\s*\n\s*- parcels added\s*=\s*(\d+)", log):
        last[name] = int(n)
    return sum(last.values())


# ======================================================================================
# zones, roles and intent
# ======================================================================================


def _features(chip: dict) -> dict[str, dict]:
    out = {c["id"]: c for c in chip.get("channels", [])}
    out.update({c["id"]: c for c in chip.get("chambers", [])})
    return out


def zone_role(chip: dict, zone: str, feeds: Sequence[str]) -> str:
    ports = {p["id"] for p in chip.get("ports", [])}
    chans = {c["id"]: c for c in chip.get("channels", [])}
    if zone in chans:
        c = chans[zone]
        if c.get("from") in ports and c.get("from") in feeds:
            return "feed"
        if c.get("to") in ports:
            return "drain"
        if c.get("from") in ports:
            return "feed"
        return "duct"
    return "chamber"


def zone_intent(chip: dict, zone: str, particle: Any, feeds: Sequence[str],
                operation: str | None) -> Any:
    feat = dict(_features(chip).get(zone) or {"id": zone})
    if feat.get("width_mm") is not None:
        passable = cfd_particles._port_capsule_passable(chip, feat)
        if passable is not None:
            feat["capsule_passable"] = passable
    return cfd_particles.sediment_intent(feat, role=zone_role(chip, zone, feeds),
                                         particle_d_um=particle.diameter_um,
                                         operation=operation)


def behind_weir_outlets(chip: dict) -> dict[str, str]:
    """{outlet port: weir chamber} for every port whose line leaves a retention chamber
    through a crest (`capsule_passable: false` on the line or the port)."""
    chambers = {c["id"]: c for c in chip.get("chambers", []) if c.get("retention")}
    ports = {p["id"] for p in chip.get("ports", [])}
    out = {}
    for c in chip.get("channels", []):
        if cfd_particles._port_capsule_passable(chip, c) is not False:
            continue
        ends = (c.get("from"), c.get("to"))
        ch = next((e for e in ends if e in chambers), None)
        port = next((e for e in ends if e in ports), None)
        if ch and port:
            out[port] = ch
    return out


def zone_flow_graph(f: ft.Fields3D, q_floor_m3_s: float) -> dict[str, set[str]]:
    """Zone -> zones its liquid flows into (net face flux above a floor)."""
    n_int = f.phi.size
    zo, zn = f.zone[f.owner[:n_int]], f.zone[f.neighbour]
    cross = zo != zn
    a, b, q = zo[cross], zn[cross], f.phi[cross]
    src = np.where(q > 0, a, b)
    dst = np.where(q > 0, b, a)
    key = src.astype(np.int64) * 100_000 + dst
    uniq, inv = np.unique(key, return_inverse=True)
    flux = np.bincount(inv, weights=np.abs(q))
    g: dict[str, set[str]] = {}
    for k, w in zip(uniq, flux):
        s, d = int(k // 100_000), int(k % 100_000)
        if w > q_floor_m3_s and s >= 0 and d >= 0:
            g.setdefault(f.zone_names[s], set()).add(f.zone_names[d])
    return g


def downstream(graph: dict[str, set[str]], zone: str) -> set[str]:
    seen, stack = {zone}, [zone]
    while stack:
        for nxt in graph.get(stack.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def outlet_zones(f: ft.Fields3D, patch: str) -> set[str]:
    p = next((b for b in f.boundary if b["name"] == patch), None)
    if p is None:
        return set()
    return {f.zone_names[z] for z in np.unique(f.zone[f.boundary_owner(p)]) if z >= 0}


# ======================================================================================
# one class, one end, one step
# ======================================================================================


def run_class(steady: ft.SteadyCase, f: ft.Fields3D, particle: Any, run: str, *,
              seeds: dict, n_parcels: int, end_time_s: float, steps: int,
              timeout_s: float = 3600.0) -> ParcelRun:
    """Inject one particle class at one conservative end and track it to the step's end."""
    from assay2chip.foam import runner

    mu = steady.fluid.mu_pa_s
    intent = INTENT_FOR_RUN[run]
    v_s, used = particle.settling_for_intent(mu, intent)
    rho_p = steady.fluid.rho_kg_m3 + used["excess_density_kg_m3"]
    d_m = used["diameter_um"] * 1e-6
    injectors: dict[str, dict] = {}
    duration = min(1.0, INJECT_FRACTION * end_time_s)
    for port in seeds.get("ports", []):
        injectors[f"feed_{port}"] = patch_injector(port, n_parcels, duration, d_m)
    for zone in seeds.get("zones", []):
        vol = float(f.V[f.zone_mask(zone)].sum())
        if vol > 0:
            injectors[f"zone_{zone}"] = zone_injector(zone, n_parcels, vol, d_m)
    interactions = {}
    for p in f.boundary:
        bc = steady.patches.get(p["name"])
        interactions[p["name"]] = ("escape" if bc is not None and bc.role in ("feed", "vent")
                                   else "stick")
    wd = steady.case_dir.parent / f"{steady.case_dir.name}_parcels_{particle.name}_{run}"
    write_parcel_case(steady, wd, rho_p=rho_p, injectors=injectors,
                      interactions=interactions, end_time_s=end_time_s, steps=steps)
    t0 = time.perf_counter()
    res = runner.run(wd, "icoUncoupledKinematicParcelFoam", timeout_s=timeout_s,
                     check=False, steady=False)
    out = ParcelRun(
        particle=particle.name, run=run, diameter_um=used["diameter_um"],
        diameter_end=used["diameter_end"],
        excess_density_kg_m3=used["excess_density_kg_m3"], density_end=used["density_end"],
        density_key=used["density_key"], rho_particle_kg_m3=rho_p,
        stokes_settling_um_s=v_s * 1e6,
        injectors=[{"name": k, "type": v["type"], "where": v.get("patch") or v.get("cellZone")}
                   for k, v in injectors.items()],
        end_time_s=end_time_s, case_dir=str(wd), wall_time_s=time.perf_counter() - t0,
    )
    if not res.ok:
        raise ft.TransportError(f"icoUncoupledKinematicParcelFoam failed in {wd}:\n{res.tail(30)}")
    out.injected = _injected(wd)
    escape_patches = [p for p, k in interactions.items() if k == "escape"]
    out.reached = {p: n for p, n in _escapes(wd, escape_patches).items() if n}
    tdir = ft.latest_time(wd) or max((d for d in wd.iterdir() if d.name.replace(".", "").isdigit()),
                                     key=lambda d: float(d.name))
    cloud = _read_cloud(tdir)
    zone_of = np.array(f.zone_names + ["(none)"], dtype=object)[
        np.where(f.zone[cloud["cell"]] >= 0, f.zone[cloud["cell"]], len(f.zone_names))] \
        if cloud["cell"].size else np.zeros(0, dtype=object)
    z = cloud["xyz"][:, 2] / f.depth_m if cloud["cell"].size else np.zeros(0)
    stuck = ~cloud["active"]
    # where each injecting port is, in plan: the mean of its patch's owner-cell centres.
    # (A stuck parcel's `age` keeps counting after it lands, so it cannot time the fall;
    # how far from the port it landed can, given the line speed.)
    port_xy = []
    for port in seeds.get("ports", []):
        pb = next((b for b in f.boundary if b["name"] == port), None)
        if pb is not None:
            cc = f.C[f.boundary_owner(pb)]
            port_xy.append((float(cc[:, 0].mean()), float(cc[:, 1].mean())))
    for name in sorted(set(zone_of.tolist())):
        sel = zone_of == name
        s = sel & stuck
        if s.any():
            out.settled[name] = {
                "total": int(s.sum()),
                "floor": int((s & (z < FLOOR_BAND)).sum()),
                "lid": int((s & (z > 1 - FLOOR_BAND)).sum()),
                "side_wall_or_crest": int((s & (z >= FLOOR_BAND) & (z <= 1 - FLOOR_BAND)).sum()),
            }
            if port_xy:
                d = np.min([np.hypot(cloud["xyz"][s, 0] - x, cloud["xyz"][s, 1] - y)
                            for x, y in port_xy], axis=0)
                out.landing_distance_mm[name] = float(np.median(d) * 1e3)
        fl = sel & ~stuck
        if fl.any():
            out.in_flight[name] = int(fl.sum())
    accounted = sum(out.reached.values()) + int(stuck.sum()) + int((~stuck).sum())
    if out.injected and accounted != out.injected:
        out.notes.append(f"{out.injected} parcels injected but {accounted} accounted for "
                         f"(outlets + settled + in flight); the difference left through a "
                         f"patch with no record")
    return out


def entered(run: ParcelRun, graph: dict[str, set[str]], zone: str,
            outlet_zone: dict[str, set[str]]) -> int:
    """Parcels that ENTERED `zone`: settled or in flight in it or anywhere downstream of
    it, plus those that left by an outlet downstream of it. The path through a chip is
    serial along the flow, so this is exact wherever the zone graph has no bypass."""
    down = downstream(graph, zone)
    n = sum(v["total"] for k, v in run.settled.items() if k in down)
    n += sum(v for k, v in run.in_flight.items() if k in down)
    n += sum(c for p, c in run.reached.items() if outlet_zone.get(p, set()) & down)
    return n


# ======================================================================================
# a whole step
# ======================================================================================


def _end_time(record: Any, f: ft.Fields3D, steady: ft.SteadyCase, slowest_v_s: float,
              cap_s: float | None) -> float:
    """How long to track: the step's own duration (the flow stops when the step does),
    but at least 1.5 full-depth fall times of the slowest particle so that "in flight at
    the end" never stands in for "had no time to fall", and never past 3 chip residence
    times, by which point every parcel has left or landed."""
    q = sum(max(-float(np.sum(f.phi_b[p])), 0.0) for p in steady.feeds if p in f.phi_b)
    vol = float(f.V.sum())
    flush = 3.0 * vol / q if q > 0 else 600.0
    fall = 1.5 * f.depth_m / slowest_v_s if slowest_v_s > 0 else 0.0
    t = max(flush, fall)
    if record.duration_s:
        t = min(t, max(float(record.duration_s), fall))
    if cap_s:
        t = min(t, cap_s)
    return float(f"{t:.4g}")


def analyse_step(chip_id: str, step: int | str | None = None, *,
                 classes: Sequence[str] | None = None, fidelity_name: str = "fast",
                 n_proc: int = 6, work_dir: Path | None = None, reuse: bool = True,
                 gap_um: float | None = None, gap_delta_um: float = 0.0,
                 seed_zones: dict[str, list[str]] | None = None, runs: Sequence[str] = RUNS,
                 end_time_cap_s: float | None = None, timeout_s: float = 3600.0) -> dict:
    """Every particle class present in one step, at both conservative ends, in 3D.

    `seed_zones` overrides where a class starts ({class: [cellZone, ...]}): the reduced
    model's per-edge check asks what happens to a particle ENTERING a feature, which a
    3D run answers best by seeding the line just upstream of it.
    """
    t_all = time.perf_counter()
    fid = ft.fidelity(fidelity_name)
    chip, _, protocol, record, raw = ft.load_step(chip_id, step, want="load")
    work = Path(work_dir) if work_dir else DEFAULT_WORK / "foam_particles"
    tag = "" if (gap_um is None and not gap_delta_um) else f"_gap{gap_um or ''}{gap_delta_um:+g}"
    steady = ft.solve_steady(chip, record, work / chip_id / f"step_{record.step}{tag}",
                             fid=fid, n_proc=n_proc, gap_um=gap_um,
                             gap_delta_um=gap_delta_um, timeout_s=timeout_s, reuse=reuse)
    f = ft.read_fields(steady)
    mu = steady.fluid.mu_pa_s
    all_classes = cfd_particles.particle_classes(mu)
    sources = cfd_particles._particle_sources(chip, protocol, all_classes)
    wanted = list(classes) if classes else [n for n in ("capsule", "capsule_swollen", "cells",
                                                        "bead") if n in sources]
    q_in = sum(max(-float(np.sum(f.phi_b[p])), 0.0) for p in steady.feeds if p in f.phi_b)
    graph = zone_flow_graph(f, 1e-6 * q_in)
    outlet_zone = {p["name"]: outlet_zones(f, p["name"]) for p in f.boundary}
    weir_out = behind_weir_outlets(chip)
    weirs = steady.mesh_meta.get("weirs", [])
    gap_meshed = {w["chamber"]: float(w.get("gap_mm_meshed", 0)) * 1000 for w in weirs}
    gap_factor = float(contracts.param("trap.gap_factor_high"))

    results: dict[str, Any] = {}
    for name in wanted:
        particle = all_classes[name]
        src = sources.get(name, {"ports": [], "chambers": []})
        seeds = {"ports": [p for p in steady.feeds if p in (src.get("ports") or [])],
                 "zones": []}
        why = "fed at this step's feed port(s), as the protocol's reagent names it"
        if seed_zones and name in seed_zones:
            seeds = {"ports": [], "zones": list(seed_zones[name])}
            why = f"seeded uniformly in {seed_zones[name]} (the line entering the feature)"
        elif not seeds["ports"]:
            seeds["zones"] = [c for c in (src.get("chambers") or []) if c in f.zone_names]
            why = (f"this step's feed carries no {name}; the resident ones are seeded "
                   f"uniformly through {seeds['zones']}")
        if not seeds["ports"] and not seeds["zones"]:
            continue
        v_slow = particle.settling_for_intent(mu, "desired")[0]
        t_end = _end_time(record, f, steady, v_slow, end_time_cap_s)
        per_run: dict[str, Any] = {}
        for run in runs:
            pr = run_class(steady, f, particle, run, seeds=seeds,
                           n_parcels=fid.parcels_per_injector, end_time_s=t_end,
                           steps=STEPS_PER_RUN[fid.name], timeout_s=timeout_s)
            zones: dict[str, Any] = {}
            for zone in sorted(set(pr.settled) | set(pr.in_flight)):
                n_in = entered(pr, graph, zone, outlet_zone)
                s = pr.settled.get(zone, {}).get("total", 0)
                zones[zone] = {"entered": n_in, "settled": s,
                               "settled_fraction_of_entered": (s / n_in) if n_in else None,
                               "in_flight_at_end": pr.in_flight.get(zone, 0),
                               "median_landing_distance_from_port_mm":
                                   pr.landing_distance_mm.get(zone),
                               "where": pr.settled.get(zone)}
            weir = {}
            for port, chamber in weir_out.items():
                n = pr.reached.get(port, 0)
                gap = gap_meshed.get(chamber)
                limit = gap_factor * pr.diameter_um
                weir[port] = {
                    "chamber": chamber, "crossed_crest": n,
                    "crossed_fraction": n / max(pr.injected, 1),
                    "gap_um_meshed": gap, "retention_limit_um": limit,
                    "size_rule_retains": (gap is not None and gap < limit),
                    "escaped": n if (gap is not None and gap >= limit) else 0,
                    "rule": f"trap.gap_factor_high {gap_factor} x {pr.diameter_um:g} um "
                            f"({pr.diameter_end} diameter): a point parcel cannot be "
                            f"excluded by size, so the contract rule decides",
                }
            per_run[run] = {**pr.to_json(), "zones": zones, "weir": weir}
        results[name] = {"seeded": why, "seeds": seeds, "end_time_s": t_end,
                         "runs": per_run}
    return {
        "schema": SCHEMA,
        "chip_id": steady.chip_id,
        "step": record.step,
        "operation": record.operation,
        "label": "SIMULATED" + (" - DEMO SCALE" if chip.get("scale", "demo") == "demo" else ""),
        "fidelity": fid.to_json(),
        "steady": steady.summary(),
        "zone_graph": {k: sorted(v) for k, v in graph.items()},
        "behind_weir_outlets": weir_out,
        "classes": results,
        "dp_feed_to_vent_pa_3d": _p_drop_pa(steady),
        "wall_time_s": {**{k: round(v, 2) for k, v in steady.wall_time_s.items()},
                        "total": round(time.perf_counter() - t_all, 2)},
        "_chip": chip, "_record": record, "_feeds": steady.feeds, "_mu": mu,
    }


# ======================================================================================
# verdicts and findings
# ======================================================================================


def _severity_loss(frac: float) -> str | None:
    if frac >= 0.9:
        return "blocker"
    if frac >= 0.5:
        return "high"
    if frac >= 0.1:
        return "medium"
    if frac > 0.01:
        return "low"
    return None


def findings_for(result: dict) -> list[dict]:
    """Per zone: the verdict from the run whose ends match the zone's intent."""
    chip, record, feeds = result["_chip"], result["_record"], result["_feeds"]
    chip_id, step = result["chip_id"], result["step"]
    out = []
    classes = cfd_particles.particle_classes(result.get("_mu"))
    for name, cls in result["classes"].items():
        particle = classes[name]
        for run, data in cls["runs"].items():
            for zone, z in data["zones"].items():
                intent = zone_intent(chip, zone, particle, feeds, record.operation)
                if INTENT_FOR_RUN[run] != intent.kind and not (
                        intent.kind == "parked" and run == "loss"):
                    continue
                frac = z["settled_fraction_of_entered"]
                if frac is None:
                    continue
                ends = (f"{data['run']} run: {data['diameter_end']} diameter "
                        f"{data['diameter_um']:g} um, {data['density_end']} of "
                        f"{data['density_key']} ({data['excess_density_kg_m3']:g} kg/m3)")
                if intent.kind == "loss":
                    sev = _severity_loss(frac)
                    if sev is None:
                        continue
                    msg = (f"{name} settle in {zone} in 3D: {frac * 100:.0f}% of the "
                           f"{z['entered']} parcels that entered it reach a wall there"
                           + (f" (median {z['median_landing_distance_from_port_mm']:.2f} mm "
                              f"from the port)" if z.get('median_landing_distance_from_port_mm')
                              else "")
                           + ", SIMULATED with icoUncoupledKinematicParcelFoam on "
                           f"the converged field. Settling is a LOSS here: {intent.why}. "
                           f"Uses the {ends}.")
                    fix = ("raise the line velocity above the settling rate over its "
                           "length (narrower or shallower line at the same flow), shorten the "
                           "line, or keep the suspension agitated at the source")
                    metric, limit = "settled_fraction_3d", 0.0
                elif intent.kind == "desired":
                    sev = None if frac >= 0.99 else ("high" if frac < 0.9 else "medium")
                    if sev is None:
                        continue
                    msg = (f"{name} are only {frac * 100:.0f}% captured in {zone} in 3D "
                           f"(the rest are carried on), where capture is the point: "
                           f"{intent.why}. Uses the {ends}.")
                    fix = "lower the flow through the feature, or lengthen it"
                    metric, limit = "captured_fraction_3d", 1.0
                else:
                    continue
                out.append(ft._finding(
                    chip_id, step, "sediment", sev, zone, metric, frac, limit, "fraction",
                    msg, fix, {"zone": z, "run": {k: v for k, v in data.items()
                                                  if k not in ("zones", "weir")},
                               "intent": intent.kind, "intent_why": intent.why},
                    assumption=True).to_json())
            for port, w in data.get("weir", {}).items():
                if run != "retention" or not w["crossed_crest"]:
                    continue
                sev = "blocker" if w["escaped"] else "medium"
                out.append(ft._finding(
                    chip_id, step, "leak", sev, w["chamber"], "parcels_crossing_weir_fraction",
                    w["crossed_fraction"], 0.0, "fraction",
                    f"{w['crossed_fraction'] * 100:.1f}% of the {name} parcels (retention "
                    f"run: smallest, lightest) are carried over {w['chamber']}'s crest to "
                    f"{port} in 3D at a meshed gap of {w['gap_um_meshed']:.1f} um. The "
                    f"contract rule ({w['rule']}) "
                    + ("RETAINS them by size at this gap, so they are held at the crest, "
                       "not lost - but it is the crest, not settling, that holds them."
                       if w["size_rule_retains"] else
                       "does NOT retain them at this gap: they ESCAPE."),
                    "keep the crest gap below the retention limit across the whole z "
                    "tolerance (a larger capsule allows a larger gap), and slow the load "
                    "so capsules settle before they reach the crest",
                    {"weir": w}, assumption=True).to_json())
    return out


def clean(result: dict) -> dict:
    return {k: v for k, v in result.items() if not k.startswith("_")}


# ======================================================================================
# the three reduced-order findings, cross-validated
# ======================================================================================


def _reduced(chip_id: str) -> dict:
    t0 = time.perf_counter()
    r = cfd_particles.analyse_chip(chip_id)
    r["_wall_s"] = time.perf_counter() - t0
    return r


def _reduced_sediment(red: dict, feature: str, particle: str, intent: str = "loss") -> dict | None:
    for s in red.get("sediment", []):
        if s["feature_id"] == feature and s["particle"] == particle and s["intent"] == intent:
            return s
    return None


def crossval_module4(**kw: Any) -> dict:
    """Capsules settling in module4_v1's feed lines (reduced: 100%, 10.3 s vs 1.5 s)."""
    res = analyse_step("module4_v1", 1, classes=["capsule"], runs=("loss",), **kw)
    red = _reduced("module4_v1")
    rs = _reduced_sediment(red, "ch_in_1", "capsule")
    z = res["classes"]["capsule"]["runs"]["loss"]["zones"].get("ch_in_1", {})
    frac = z.get("settled_fraction_of_entered")
    land = z.get("median_landing_distance_from_port_mm")
    line_u = (rs["u_mm_s"] if rs else None)
    t3 = (land / line_u) if (land and line_u) else None
    agree = frac is not None and rs is not None and abs(frac - rs["settled_fraction"]) <= 0.1
    return {
        "question": "capsules settling in module4_v1's feed line ch_in_1 (step 1 load)",
        "reduced": {"settled_fraction": rs and rs["settled_fraction"],
                    "transit_s": rs and rs["transit_time_s"], "fall_s": rs and rs["fall_time_s"],
                    "ends": rs and f"{rs['diameter_end']} d {rs['diameter_um']} um, "
                                   f"{rs['density_end']} {rs['excess_density_kg_m3']} kg/m3"},
        "openfoam": {"settled_fraction_of_entered": frac,
                     "median_landing_distance_from_port_mm": land,
                     "implied_time_to_land_s": t3,
                     "entered": z.get("entered"), "where": z.get("where"),
                     "ends": "loss run: max diameter, max excess density"},
        "agree": agree,
        "why": (f"both put the capsules on the floor of the feed line: at the loss end the "
                f"Stokes fall through the depth takes {rs and rs['fall_time_s']:.2f} s against a "
                f"{rs and rs['transit_time_s']:.1f} s transit. 3D adds WHERE: the median "
                f"capsule lands {land or 0:.2f} mm from the port, i.e. within the first "
                f"{t3 or 0:.1f} s at the line's mean speed, so the line is fouled at its "
                f"inlet end, not along its length" if rs else "no reduced-order entry"),
        "result": clean(res), "findings": findings_for(res),
        "wall_time_s": {"openfoam": res["wall_time_s"], "reduced": round(red["_wall_s"], 2)},
    }


def crossval_cell_prep(**kw: Any) -> dict:
    """Cells settling in cell_prep_v1's mixer mix_1 (reduced: 100%, 46 s vs 13 s)."""
    record_step = 4
    res = analyse_step("cell_prep_v1", record_step, classes=["cells"], runs=("loss",),
                       seed_zones={"cells": ["ch_cult_mix"]}, **kw)
    red = _reduced("cell_prep_v1")
    rs = _reduced_sediment(red, "mix_1", "cells")
    z = res["classes"]["cells"]["runs"]["loss"]["zones"].get("mix_1", {})
    frac = z.get("settled_fraction_of_entered")
    return {
        "question": "cells settling in cell_prep_v1's serpentine mixer mix_1 (step 4 transfer)",
        "reduced": {"settled_fraction": rs and rs["settled_fraction"],
                    "transit_s": rs and rs["transit_time_s"], "fall_s": rs and rs["fall_time_s"]},
        "openfoam": {"settled_fraction_of_entered": frac, "entered": z.get("entered"),
                     "zones": res["classes"]["cells"]["runs"]["loss"]["zones"],
                     "ends": "loss run: 15 um, max cell.excess_density_kg_m3 (75)"},
        "agree": frac is not None and rs is not None and abs(frac - rs["settled_fraction"]) <= 0.1,
        "result": clean(res), "findings": findings_for(res),
        "wall_time_s": {"openfoam": res["wall_time_s"], "reduced": round(red["_wall_s"], 2)},
    }


def _p_drop_pa(steady: ft.SteadyCase) -> float | None:
    from assay2chip.foam import flow

    dats = flow.find_dat_files(steady.case_dir)
    p = {}
    for key, path in dats.items():
        name = key.split("/")[0]
        if name.startswith("p_"):
            v = flow.read_dat(path).value("areaAverage(p)", default=None)
            if v is not None:
                p[name[2:]] = v * steady.fluid.rho_kg_m3
    feeds = [p[k] for k in steady.feeds if k in p]
    vents = [p[k] for k in steady.vents if k in p]
    return (max(feeds) - min(vents)) if feeds and vents else None


def weir_gap_sweep(chip_id: str = "module3_v1_translated", step: int | str = 1,
                   offsets_um: Sequence[float] | None = None, fidelity_name: str = "fast",
                   **kw: Any) -> dict:
    """Sweep the weir gap with mesh.py's override and find where the weir still works.

    Works = the 3D feed-to-vent pressure (an UPPER bound on the crest drop) stays under
    `contracts.max_working_pressure_kpa()` AND either the size rule retains the smallest
    capsule or no capsule reaches the crest. The reduced model's band (-20 to +1.8 um on
    module3) is the pressure-free size rule alone; this checks the pressure side in 3D and
    measures how much of the load actually arrives at the crest when the rule fails.
    """
    if offsets_um is None:
        offsets_um = (-20.0, 0.0, 2.0, 20.0) if fidelity_name == "fast" else (
            -20.0, -15.0, -10.0, -5.0, 0.0, 1.0, 1.7, 1.9, 3.0, 5.0, 10.0, 20.0)
    p_limit_pa = contracts.max_working_pressure_kpa() * 1000.0
    rows = []
    walls = {}
    for off in offsets_um:
        res = analyse_step(chip_id, step, classes=["capsule"], runs=("retention",),
                           gap_delta_um=off, fidelity_name=fidelity_name, **kw)
        run = res["classes"]["capsule"]["runs"]["retention"]
        dp = res["dp_feed_to_vent_pa_3d"]
        weir = next(iter(run["weir"].values()), {})
        crossed = weir.get("crossed_fraction", 0.0)
        retains = bool(weir.get("size_rule_retains"))
        p_ok = dp is not None and dp <= p_limit_pa
        rows.append({
            "z_offset_um": off, "gap_um_meshed": weir.get("gap_um_meshed"),
            "dp_feed_to_vent_pa_3d": dp, "pressure_limit_pa": p_limit_pa,
            "pressure_ok": p_ok, "size_rule_retains": retains,
            "retention_limit_um": weir.get("retention_limit_um"),
            "capsules_reaching_crest_fraction": crossed,
            "settled_in_trap": run["zones"].get(weir.get("chamber", ""), {}),
            "works": p_ok and (retains or crossed == 0),
            "works_by_rule_alone": p_ok and retains,
        })
        walls[f"{off:+g}"] = res["wall_time_s"]
    ok = [r["z_offset_um"] for r in rows if r["works_by_rule_alone"]]
    red = _reduced(chip_id)
    band = next((w for w in red.get("weir", []) if w["particle"] == "capsule"), {})
    limit = rows[0]["retention_limit_um"] if rows else None
    nominal = (rows[0]["gap_um_meshed"] - rows[0]["z_offset_um"]) if rows else None
    hi = (limit - nominal) if (limit is not None and nominal is not None) else None
    return {
        "question": f"{chip_id} weir working band over the +/-20 um z tolerance",
        "rows": rows,
        "openfoam_band_z_offset_um": [min(ok) if ok else None, hi],
        "openfoam_band_note": ("lower end: the most negative sampled offset at which the 3D "
                               "pressure stays under the limit; upper end: the size rule "
                               "(gap < trap.gap_factor_high x d_min), which 3D does not move"),
        "reduced_band_z_offset_um": [band.get("z_offset_lo_um"), band.get("z_offset_hi_um")],
        "reduced_nominal_dp_pa": (band.get("nominal_dp_kpa") or 0) * 1000,
        "wall_time_s": walls,
    }


# ======================================================================================
# CLI
# ======================================================================================


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--chip")
    ap.add_argument("--step", default=None)
    ap.add_argument("--class", dest="classes", action="append", default=None)
    ap.add_argument("--fidelity", choices=("fast", "high"), default="fast")
    ap.add_argument("--np", type=int, default=6, dest="n_proc")
    ap.add_argument("--work", type=Path, default=None)
    ap.add_argument("--no-reuse", action="store_true")
    ap.add_argument("--crossval", action="store_true",
                    help="the reduced model's three findings: module4 feed lines, cell_prep "
                         "mixer, module3 weir band")
    ap.add_argument("--weir-sweep", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)
    kw = {"fidelity_name": args.fidelity, "n_proc": args.n_proc, "work_dir": args.work,
          "reuse": not args.no_reuse}
    if args.crossval or args.weir_sweep:
        out: dict[str, Any] = {}
        if args.crossval:
            out["module4_feed_lines"] = crossval_module4(**kw)
            out["cell_prep_mixer"] = crossval_cell_prep(**kw)
        out["module3_weir_band"] = weir_gap_sweep(**kw)
    else:
        if not args.chip:
            ap.error("--chip is required unless --crossval or --weir-sweep")
        res = analyse_step(args.chip, args.step, classes=args.classes, **kw)
        out = {**clean(res), "findings": findings_for(res)}
    text = json.dumps(out, indent=2, default=str)
    path = args.out or ((args.work or DEFAULT_WORK / "foam_particles") / "particles_result.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    print(text[:20000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

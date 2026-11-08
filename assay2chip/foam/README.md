# foam: full 3D CFD with OpenFOAM, and a rendered end-to-end pipeline

The reduced-order solver in `assay2chip/cfd/` is fast enough to run inside the automatic
fix loop, and correct wherever Re << 1. This package is its 3D counterpart: it models
everything the reduced model cannot, checks it, and renders the whole pipeline.

| The reduced model cannot | OpenFOAM case here |
|---|---|
| Re > 1 (Module 6 quench flood Re 13-23, the droplet nozzle Re ~9) | `flow.py`: steady `simpleFoam` or transient `pimpleFoam`, laminar, full 3D |
| recirculation and real 3D dead zones | `flow.py` + `transport.py`: the 3D field itself, mean age per cell |
| accuracy near side walls in narrow chambers | resolved by the 3D mesh |
| two-phase flow: droplets, priming, trapped air | `vof.py`: `interFoam` (VOF) |
| settling and transport of cells, capsules and beads | `particles.py`: Lagrangian parcels with gravity and drag |
| washout and exchange efficiency in 3D | `transport.py`: `scalarTransportFoam` |

## Which OpenFOAM

**ESI OpenCFD OpenFOAM (v2412 or newer)**, native on Apple Silicon through the
`gerlero/openfoam` Homebrew tap. It is free and open source, needs no Docker, and its
solver names are what this package calls: `simpleFoam`, `pimpleFoam`,
`scalarTransportFoam`, `icoUncoupledKinematicParcelFoam`, `interFoam`, `checkMesh`,
`decomposePar`, `reconstructPar`, `foamToVTK`, `postProcess`.
`runner.py` is the ONLY place that knows how to launch it. Everything else builds a case
directory and asks the runner to execute a command in it.

Rendering uses **PyVista (VTK)**, off-screen, from `requirements-foam.txt`, plus ffmpeg
for video. Core `requirements.txt` stays lightweight: the rest of the repo, and every
test that does not need OpenFOAM, must run without either.

## Pipeline

```
chip/<id>.json + protocols/<id>.json + (pipeline_mvp.json | partition.json)
   │
 mesh.py      -> case/constant/polyMesh            structured hex, graded in z, checkMesh clean
 case.py      -> case/system, constant, 0          templates, per-step fluid and BCs
 runner.py    -> runs the solver (parallel)        logs, convergence, exit status
 flow.py      -> p, U, wall shear, patch flows     + cross-check against assay2chip/cfd
 transport.py -> washout, mean age                 dead zones in 3D
 particles.py -> parcel tracks with settling       sediment, escape, loss
 vof.py       -> priming (air->liquid), droplets   trapped air, droplet size
 render.py    -> per-chip PNG / MP4                slices, streamlines, shear, age, tracks
 scene.py     -> the WHOLE PIPELINE in one scene   chips in chain order, tubing, animated
 run.py       -> CLI                               --pipeline protocols/pipeline_mvp.json
```

Output goes to `assay2chip/work/<run>/foam/<chip_id>/<step>/` (case) and
`assay2chip/work/<run>/foam/render/` (images, video, glTF). Case directories are LARGE
and are git-ignored; only the summary JSON and a small set of keyframe images are kept.

## Rules (the same physics discipline as the rest of the repo)

- **Mesh.** The chip is one fluidic layer of constant depth, so the fluid domain is an
  EXTRUSION of the plan view: rasterise it (reuse the plan geometry in
  `assay2chip/cfd/mesh.py` and `cad/make_figures.py` - never a fourth geometry
  implementation) and write a structured hex `polyMesh` directly. No STL booleans, so no
  snappyHexMesh failure modes. xy spacing: at least 6 cells across the narrowest channel,
  within a cell budget. z: at least 8 cells across the depth, GRADED so a 25 um weir gap
  gets at least 3 cells. One patch per port (named by port id) plus `walls`. `checkMesh`
  must pass.
- **Fluid properties from contracts.** Kinematic viscosity = `contracts.viscosity_for(temp)`
  / `fluid.density_37c_kg_m3`. A fluid the contract does not cover (cold organic solvent,
  oil, viscous core buffer) is a FLAGGED assumption, never silently water.
- **Boundary conditions from the protocol step** through `assay2chip/cfd/scenario.py`:
  feed -> volumetric flow inlet, vent -> fixed pressure 0, cap -> wall.
- **Shear is judged against the right ceiling for what the chamber holds**, using the same
  rules as `assay2chip/cfd/analyse.py`: 0.01 dyn/cm^2 for adherent cells only, 0.3 for a
  rinse before a deliberate lyse, capsules by escape pressure. Report wall shear in Pa and
  dyn/cm^2 (x10).
- **Findings use the same `Finding` shape** (`assay2chip/cfd/model.py`), with
  `source: "openfoam"`, so analyse, fix and the report consume them unchanged.
- **Cross-validate both ways.** On every chip and step both models can run, compare
  pressure drop and outlet flow split. Agreement within a few percent where Re << 1 is
  the evidence that both are right. A disagreement is reported with the model believed
  correct and why - never averaged away.
- **Honest labels.** Every render says SIMULATED; every 30 x 20 mm chip says DEMO SCALE;
  any assumed property is listed on the render and in the summary.
- **Tests** that need OpenFOAM are skipped cleanly when it is absent. Case generation and
  mesh writing are tested offline.

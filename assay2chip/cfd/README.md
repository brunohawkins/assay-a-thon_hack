# CFD: does the whole chip set actually flow?

Stage S10 (`assay2chip/stages/s10_cfd.py`). Takes the built chips, their protocols and the
handoffs between them and simulates the **entire pipeline end to end**, then reports leaks,
traps and anything else that would stop the protocol running - with fixes.

It must also run on chips this pipeline did NOT make: the hand-designed MVP chain in
`protocols/pipeline_mvp.json` (module1 -> cellprep -> module2 -> module3 -> barcoding ->
module4 -> module5) is the first real target, and every repo chip in `chip/*.json` is a
test case. `python assay2chip/cfd/run.py --pipeline protocols/pipeline_mvp.json`.

**Physics note that changes what "trap" means.** Depth-averaged Hele-Shaw flow is a
potential flow in plane: it has NO recirculation eddies, so a "closed streamline" test will
never fire in a chamber and must not be relied on. In this regime a trap is a
**low-velocity or long-residence-time zone** (a corner, a region behind a rib, the far side
of a wide chamber fed from one point), found by residence-time and velocity-magnitude
analysis. Real 3D recirculation needs Re well above 1, which is itself a finding here.

```
chip/<chip_id>.json  ×N  +  partition.json handoffs
        │
  mesh      ──►  cfd/<chip_id>.mesh.json        depth-averaged cells + connectivity
        │
  solve     ──►  cfd/<chip_id>.field.json       p, u per cell; Q, dP per edge
        │
  couple    ──►  cfd/pipeline.field.json        chips joined through tubing models
        │
  analyse   ──►  cfd/findings.json              leaks, traps, bubbles, shear, particles
        │
  fix       ──►  cfd/fixes.json                 proposed geometry/flow changes, re-run
        │
  report    ──►  report.md + cfd/figures/*.svg
```

## Why a network solver, not a general CFD package

Every channel is a shallow rectangular duct, 0.2-2 mm wide and 0.3 mm deep, and the audit
measured **Re well under 1 almost everywhere** - so this is Stokes flow and the
Hele-Shaw / resistance-network form is not an approximation of convenience, it is the
correct physics. It also runs in milliseconds, which is what makes the automatic
fix-and-re-run loop possible.

Use the exact rectangular-duct resistance, which `operator/flow_calc.py` already has:

```
R = 12 mu L / (w h^3 (1 - 0.63 h/w))        Pa s / m^3
tau_wall = 6 mu Q / (w h^2)                  Pa, x10 for dyn/cm^2
```

Solve the linear system `A p = b` for nodal pressures with a conductance matrix, then
recover per-edge flow. Chambers are not single nodes: discretise them across their width,
or a wide chamber will look like a pipe and hide exactly the dead zones we are hunting.

**Where Stokes is not enough, say so rather than pretending.** The audit found genuine
exceptions: Module 6's quench flood runs Re 13-23 in the comb spine and feed line, and a
droplet nozzle runs Re ~9. Flag any edge above Re 1 as outside the model's validity and
report it as a finding, not a number.

## Findings the analysis must produce

Each finding carries `severity` (`blocker|high|medium|low`), the chip and feature, the two
numbers that disagree, and a `fix` where one exists.

| Class | Test | Source of the limit |
|---|---|---|
| **leak** | pressure at any port or seal above the safe margin | `capsule.escape_pressure_kpa` x `control.pressure_margin` |
| **leak** | pressure anywhere above what a hot-embossed 0.3 mm bond holds | `contracts/materials.json`, flag if unknown |
| **trap** | a cell whose velocity is below a threshold fraction of the chamber mean (NOT a closed streamline - Hele-Shaw has none) | low-velocity zone in the field |
| **trap** | residence-time outlier: part of the chamber flushes far slower than the bulk | exchange target in `contracts/parameters.json` |
| **dead_end** | a region with no path from an inlet to an outlet | graph reachability |
| **bubble** | a dead end with no vent, or a sudden expansion that will not prime | capillary pressure vs drive pressure |
| **shear** | wall shear above the ceiling **for that chamber's contents** | three ceilings, correctly scoped: `shear.culture_ceiling_dyn_cm2` 0.01 for ADHERENT CELLS only, `shear.prelysis_rinse_ceiling_dyn_cm2` 0.3 for a rinse before a deliberate lyse, capsule chambers bounded by escape pressure instead |
| **sediment** | settling velocity beats transit: a particle drops out before it crosses | Stokes settling vs local velocity and depth |
| **clog** | channel narrower than ~1.5x what it must carry | `capsule_channel.width_factor_low`; capsules swell |
| **handoff** | volume or particle size mismatch between two chips, or dead volume in the line | `partition.json` handoffs |
| **unmodelled** | Re > 1 on any edge | report, do not silently trust |

## Fixes

`fixes.json` proposes concrete, checkable changes - widen this channel to X, add a vent
here, drop this flow to Y, split this chamber, add a bypass - each with the finding it
addresses and the predicted new value. A fix is **applied automatically only if** it keeps
every fabrication floor, introduces no channel crossing, and the re-run shows the finding
gone and no new blocker. Anything else is reported for a human. Record both the before and
after numbers; never silently rewrite geometry.

## Module contracts (authoritative - ten agents build these in parallel)

All code in `assay2chip/cfd/`. Shapes live as dataclasses in `assay2chip/cfd/model.py`
(owned by the mesh module) and serialise to JSON. SI internally; mm, uL/min, Pa and
dyn/cm^2 at the JSON boundary, with the unit in every key name.

| Module | Reads | Writes |
|---|---|---|
| `mesh.py` | `chip/<id>.json` | `Mesh`: `nodes[{id, kind: cell\|junction\|port, x_mm, y_mm, area_mm2, depth_mm, feature_id}]`, `edges[{id, a, b, kind: duct\|hs_face\|mouth, conductance_m3_per_pa_s, length_mm, width_mm, depth_mm, feature_id}]`, `ports{port_id: node_id}`, `meta{chip_id, grid_mm, mu_pa_s}` |
| `scenario.py` | `protocols/<id>.json` + `Mesh` | `Scenario` per protocol step: `{step, ports: {port_id: {type: flow, q_ul_min} \| {type: pressure, p_pa} \| {type: closed}}, fluid, temp_c}` from `ports.feed/vent/cap` and `flow_ul_min` |
| `solve.py` | `Mesh` + `Scenario` | `Field`: `p_pa[node]`, `q_m3_s[edge]` (signed a->b), `u_mm_s[cell]` (vector), `shear_dyn_cm2[edge\|cell]`, `re[edge]`, `residual{mass_imbalance_max, q_in, q_out}` |
| `transport.py` | `Mesh` + `Field` | residence time per cell, washout curve per chamber, exchange fraction reached by the step's volume |
| `particles.py` | `Mesh` + `Field` + particle class | settling vs transit per edge/cell, clog margin per edge, weir escape check |
| `couple.py` | per-chip `Field`s + handoffs | `pipeline.field.json`: tubing edges, dead volume, pressure budget per handoff |
| `analyse.py` | all of the above | `findings.json`: `[{id, class, severity, chip_id, feature_id, step, metric, value, limit, units, message, fix_hint}]` |
| `fix.py` | findings + chip + protocol | `fixes.json`: `[{finding_id, action, target, params, predicted{metric, before, after}, auto_applicable, applied, verified_after}]` |
| `validate.py` | - | benchmark results; must pass before any finding is trusted |
| `figures.py`, `report.py`, `run.py` | everything | SVG heatmaps, report section, CLI; `assay2chip/stages/s10_cfd.py` |

**Meshing rules that the audits make non-negotiable.**
- A feature whose block is channel-like - `serpentine_channel`, `herringbone_mixer`,
  `spiral_channel`, `centipede_channel` and every `*_channel` - is a DUCT at its real lane
  width and path length, even when the chip spec stores it as a chamber with a bounding
  box. Meshing a serpentine as a 2D chamber reproduces the 43x shear error the audit found.
- Channels with a `path` follow that path. Straight lines between endpoints are wrong for
  routed channels.
- A weir chamber has a slit edge of gap `retention.opening_um` across its full width; flow
  through a slit scales with gap^3, so the +/-20 um z tolerance must be swept, not ignored.
- Viscosity: `fluid.viscosity_37c_pa_s` for 37 C steps, `fluid.viscosity_pa_s` (water,
  20 C) for the dye demo, and a FLAGGED assumption for any fluid the contract does not
  cover (cold organic solvent, viscous core buffer). Never silently use water.

**Never modify a hand-designed repo chip.** In pipeline mode a fix becomes a `rerun_from`
request to S6/S7. In standalone mode on `chip/*.json`, fixes are proposals plus a patched
copy in the work dir.

## Validation

A solver nobody has checked is worse than no solver. Required:

- Analytic single straight channel: match `R = 12 mu L / (w h^3 (1 - 0.63 h/w))`.
- Series and parallel networks: match hand-computed resistances.
- Agreement with `operator/network.py`, which already solves Module 1's network - it is an
  independent implementation, so the two must agree within a few percent on the same chip.
  If they disagree, one is wrong and the report must say which.
- Mass conservation at every node, and total in equals total out.

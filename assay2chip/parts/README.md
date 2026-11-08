# assay2chip/parts: the parametric part registry

S6 asks this package for geometry. It answers with a printable part or a reason.

```python
from assay2chip.parts import registry, Need

registry.list()                     # all 52 block ids: sized | refused | not_sized
part = registry.size("trap_chamber", Need(
    volume_ul=20, flow_ul_min=10, particles="capsules", shear_class="capsules"))
```

```
python assay2chip/parts/registry.py               # the whole registry, as JSON
python assay2chip/parts/registry.py --flowmatter  # the catalogue mapping and its gaps
python -m unittest tests.test_assay2chip_parts -v
```

## Why it exists

**The library parts do not fit.** `chip/components/catalogue.json` is a SHAPE reference.
All 21 of its parts are 1.0–2.0 mm deep against a 0.3 mm layer, 10 of them are larger than
the whole 30 × 20 mm envelope, and `spiral_channel` alone is 104.8 × 99.8 mm. Placing them
as they are is what produced a 317 × 116 mm, 1 mm deep, 12-port chip that failed the
contract outright.

**The blocks with internal geometry had no geometry.** A block audit found that every one
of them — the weir crest, the comb teeth, the serpentine length, the droplet nozzle — was
specified only in English prose, so nothing downstream could build it from data.

Both fixes are the same fix: size every part from the physics, against the contract, and
emit the internal features as `Feature` records and chip-spec entries.

## The shape

`Need` carries what the op requires: `load` (preferred) or `volume_ul`, `residence_s`,
`flow_ul_min`, `particles` + `particle_um`, `shear_class`, `depth_mm` (the chip's one etch
depth), `temp_c`, `scale`, and the few block-specific fields `registry.REQUIRES` publishes
(`branches`, `feed_edge_mm`, `pressure_drop_pa`, `length_mm`, `mix`).

`Load` is the `unitops.json` load object, and it is what a part should really be sized
from — **bench volumes do not transfer** (`assay2chip/README.md`). "Wash the pellet in
500 µL" describes a *tube*: a 500 µL chamber is wrong, and so is refusing the 0.012 µL
that is left once the tube is dropped. The four bases:

| `load.basis` | sizes from |
|---|---|
| `adherent_area` | floor area = `cells / surface_density_per_mm2` (contract default `culture.surface_density_per_mm2`, flagged `contract_default`) |
| `packed_volume` | a bed, from count, diameter and `capsule.packing_fraction` |
| `suspension_volume` | `cells / suspension_density_per_ul` |
| `flow_through` | nothing held: `V = Q·t` from flow and `residence_s` |

`volume_ul` still works unchanged, and is what a load resolves into.

**A volume below the smallest buildable chamber is rounded UP, never refused.** The floor
is `min_open_channel_mm²  ×  depth` = 0.012 µL at the demo depth, and the same clamp
applies when a shear-forced wide lane drives the flow path sub-floor. The part reports
`volume_requested_ul`, `volume_rounded_up`, `volume_rounded_up_factor`,
`smallest_buildable_volume_ul` and `volume_set_by`, and raises a risk saying reagent
*ratios* must be recomputed on the built volume. Refusal is reserved for what the floors
genuinely cannot meet: a sub-200 µm in-plane feature, a crest taller than its layer.

`PartGeometry` comes back with `footprint_mm`, `depth_mm`, `volume_ul`, `openings`
(named boundary attachment points with position, direction and real lane width, in
part-local mm — the router joins these), `features` (what a mould actually cuts),
`entries` (schema-valid chip-spec chamber/channel entries, each with `block`, its
`volume_ul` and the formula that produced it), `floors` (every fabrication floor checked,
tagged `in_plane` / `z` / `process`), `physics`, `risks`, `assumptions` and `provenance`.

Volume is always by the right formula — box, `pi/4 · dx · dy · z`, `length · width · depth`
for a folded run, box minus crest for a weir chamber, spine plus teeth for a comb, box
minus pillars where the lid is supported. A bounding-box volume on a serpentine overstates
it by the fold ratio, about 3×, and a protocol that trusts it flushes a third of what it
should.

## Rules it will not bend

- **Every contract value is read, never retyped.** `assay2chip.lib.contracts` for the keys,
  `derived.py` for the values `blocks.json` states only in prose (the ±20 µm z tolerance,
  the 5–10 mm lid-support pitch band, the DLD worked point, the 50–210 µm/s capsule
  settling band). `physics.py` holds **no material constants at all** — it is pure
  arithmetic and every density and viscosity arrives as an argument. A test parses the
  package with `ast` and fails on any contract number appearing as a literal.
- **Settling picks the conservative end of the density band, and says which.** Stokes
  settling is linear in excess density and the contract bands it per particle kind
  (`cell` / `capsule` / `bead.excess_density_kg_m3`, all `assumption: true`). A sizer
  *relying* on settling to retain takes the band's **min** (slowest, so retention is never
  over-promised); one *checking for loss* in a line or mixer takes the **max** (fastest, so
  a depositing line is never missed). `physics["excess_density_end"]`,
  `_key`, `_band_kg_m3`, `_purpose` and `_why_this_end` record the choice on every part.
- **Shear is on the real lane width**, never a bounding box, and `physics["shear_measured_on"]`
  says which dimension it used.
- **z is not in-plane.** The 25 µm weir gap, the crest height and the herringbone grooves
  are depth features; `Floors.z()` records them and deliberately does not test them
  against the 200 µm lateral floor. That is the whole reason the retention scheme works.
- **A plain serpentine does not mix.** `mix=True` on a folded run raises and names
  `herringbone_mixer`; Re here is order 1e-2.
- **`phase_guide` is a capillary stop, `flow_restrictor` sets resistance.** An audit found
  these two swapped, so each part names the other in `physics["not_to_be_confused_with"]`.
- **It raises rather than fudge.** A missing `Need` field raises naming the field, so S6
  records it in `unresolved`. A part that cannot meet a floor raises with the floor, the
  number it reached and the number it needed.

## What it finds

Two refusals are results, not omissions:

- `tesla_valve` is refused — diodicity is exactly 1.0 in Stokes flow, per the contract.
- `pillar_funnel_trap` is refused for retention, and `pillar_array` says on every part it
  returns that it cannot retain: its throat is 4.6 mm against a 70 µm capsule.
- `flow_focusing` refuses the contract's own 40 µm `encapsulation.nozzle_um`, because a
  nozzle is an IN-PLANE width and the floor is 200 µm. The levers are both in the
  contracts: bigger capsules (`capsule.diameter_tunable_um` is 35–260 µm, and a 200 µm
  nozzle lands inside that band) or off-chip encapsulation. Depth is not the problem.

## Flowmatter mapping

17 of the 52 block ids have a Flowmatter shape; **35 do not**, including every block the
audit flagged. Four catalogue parts map to no block: `mesh_body_a` / `mesh_body_b` (role
unknown, and a mesh is the one retention route the contract calls *bought*) and the two
valves (a chip spec files valves under `valves`, not as a block). Seven of the 21 names are
the catalogue's own guesses: `flow_manifold`, `flow_distributor` and `port` at medium
confidence, the two mesh bodies and the two valves at low.

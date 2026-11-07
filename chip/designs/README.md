# designs: paper → library parts → chip

```
paper / protocol table
   │  Claude groups the steps into modules, one library part each
   ▼
chip/designs/<id>.json
   │  python chip/components/build_chip.py chip/designs/<id>.json
   ▼
chip/<id>.json             chip spec (contract; checked by tools/validate.py)
cad/exports/<id>/          whole chip + one STL per placed part, mm, chip origin
cad/fusion/<id>_build.py   Fusion 360 script that imports the placed parts
```

The parts come from the Flowmatter block sheet. One-time setup, and again whenever that file changes:

```
python chip/components/build_library.py     # -> cad/library/*.stl + chip/components/catalogue.json
```

## Design file

```json
{
  "id": "capsule_prep_v1",
  "name": "Cell prep and encapsulation v1",
  "source": "where the steps come from",
  "placeholder": true,
  "modules": [
    { "id": "culture_1", "part": "culture_chamber", "steps": [1, 2, 3],
      "inlets": ["medium", "dissociation_enzyme"], "does": "one sentence" }
  ],
  "off_chip":      [ { "steps": [8], "why": "..." } ],
  "missing_parts": [ { "steps": [7], "need": "pillar filter, ~30 um gap", "why": "..." } ]
}
```

- `modules` are in **flow order**: the chip is `inlet_1 → module → module → … → outlet_1`.
- `part` is a `part` name from `chip/components/catalogue.json`.
- `steps` are the protocol step numbers the module does. Every step should end up in a
  module, `off_chip` or `missing_parts`. The build prints a step-by-step table so gaps show.
- `inlets` adds a reagent port (`<name>_in`) feeding that module. Names must be unique on the chip.
- Optional: `mesh_pore_um` on a module, `material`, `description`.

## How steps are grouped (rules Claude follows)

1. **One module per place the cells or capsules sit.** Steps that happen to the same
   cells in the same spot become one chamber: culture + dissociation; oil removal +
   curing + capsule wash in one trap.
2. **A reagent added to a flowing stream** is a merge part (`t_junction`, `y_junction`),
   then a mixer (`serpentine_channel`, or `snake_channel` for viscous liquids).
3. **Centrifuge washes** become a chamber flush (capsules on a mesh), or a
   `spiral_channel` for loose cells.
4. **Timed waits in flow** (gelation, incubation) are a delay line: pick a part whose
   `volume_ul` ÷ flow rate gives the residence time.
5. **Split-and-pool**: a `split` part (`flow_manifold`, `flow_distributor`,
   `fork_junction`), chambers, then `ring_junction` to pool.
6. Imaging, QC and existing kits go in `off_chip`. If no part fits, put the step in `missing_parts`
   with what's needed. Don't force a wrong part.

## Limits (v1, kept simple)

- Parts are placed in a row, unrotated, 5 mm apart. Their openings are **not** lined up
  or joined. `channels` in the chip spec record which part feeds which; draw the joining
  channels in Fusion.
- Every part's top-down shape is right, but these names are guesses: manifold vs
  distributor, normally-open vs normally-closed valve, port, and the two mesh bodies.
  See `name_confidence` in the catalogue and fix them in `build_library.py`.
- Parts that aren't chambers (mixers, junctions, spirals) are typed `exchange` (or
  `pool`/`split`) in the chip spec, because the contract has no other types.

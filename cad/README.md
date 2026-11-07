# cad/ (owner: Ayrton)

CAD for the laser-cut and 3D-printed chip parts, plus the exported geometry the
digital twin uses.

## Export rules (the twin depends on these)

1. **One STL per part**, saved in `cad/exports/`.
2. **Name each file exactly by its chamber or port ID** from the chip spec:
   `exchange_1.stl`, `split_1.stl`, `split_2.stl`, `split_3.stl`, `split_4.stl`,
   `pool_1.stl`, `inlet_1.stl`, `outlet_1.stl`. Lowercase, underscore, no spaces.
   The current IDs are in `chip/placeholder_chip_spec.json`.
3. **Millimetres.**
4. **Same origin as the chip spec:** the bottom-left corner of the 75 × 25 mm
   footprint is (0, 0, 0), x along the slide, y across it, **z up**. Export each part
   *in place* (don't move it to the origin), so `exchange_1.stl` sits where the chip
   spec says `exchange_1` is.
5. Binary or ASCII STL both work.
6. **One chip owns the flat folder; every other chip gets a subfolder.** Part files are
   named by bare ID and IDs repeat across chips (`inlet_1` is not unique), so
   `cad/exports/inlet_1.stl` can only mean one chip. `twin/main.js` loads
   `cad/exports/` flat, so that namespace belongs to the chip the twin shows, and
   `cad/make_train_cad.py` writes any other chip to `cad/exports/<chip_id>/`.
   `chip.loadStls()` already takes a base path, so pointing the twin at a subfolder is
   a one-line change whenever someone wants the second chip in it. `module1_v1` is in
   `cad/exports/module1_v1/`.
7. **`module1_v1` is laser-cut only.** It has no feature under 0.3 mm, so it needs no
   photolithography, cast layer, PDMS or glass, and no second fabrication route.

If your CAD can export **GLB with named parts**, put that in `cad/exports/` as well
(e.g. `chip_v1.glb`), with each part named by its ID. The twin doesn't load GLB yet,
but it keeps colours and names together and is the easiest upgrade path.

## Checking your export

```
python tools/validate.py
```

warns about any STL whose name doesn't match a chamber or port ID. Then open the twin
(see the main README): matching STLs replace the placeholder boxes automatically, and
a "Using CAD for …" message appears. A missing STL just keeps the box, so you can
export parts one at a time.

If a part shows up in the wrong place, the usual cause is a different origin or
units (inches, or centred on the part). Re-export in place in mm.

## Hello world plate (MVP)

`exports/hello_world_plate.stl` is Ayrton's hello world MVP: a **positive mould**,
15 × 10 mm, with two 1 mm wells joined by a 0.5 × 0.2 mm channel (5 mm long) raised
0.2 mm on a 0.5 mm base. Casting against it gives two open-top wells joined by an
open-top channel. It's exported in **µm with the plate centred on 0,0**; the dims file
records that, so the file doesn't need re-exporting.

- **Edit dimensions only in `cad/hello_world_dims.json`.** Values were measured from the STL.
  `stl_units`, `stl_origin` and `positive_mould` describe how the STL was exported. Names in
  `todo` are still unknown.
- Then run `python cad/hello_world_spec.py`. It regenerates `cad/hello_world_plate_spec.json`
  (`well_1` and `well_2` of type `well`, plus `channel_1`, all `open_top: true`); don't edit
  that spec by hand. It then checks the STL: the whole plate's bounding box, and for a
  positive mould, the bounding box of the raised wells + channel. It warns about anything
  more than 10% off and never changes the STL or the dims.
- A new version can be one piece (`hello_world_plate.stl`) or separate parts (`well_1.stl`,
  `channel_1.stl`, `well_2.stl`). If the export settings change, update `stl_units` /
  `stl_origin` to match.

## Real scale vs display model

- `cad/*_spec.json` (and `chip/*.json`) are **real-scale** chip specs: the working dimensions; the twin and `tools/check_chip_spec.py` use these.
- `cad/display/<chip_id>/` is a **display model**: the same chip scaled up by `display_scale` (`cad/fabrication.json`, default 10×) so people can see it. It's a show piece, not a working chip.
- Make one with `python cad/export_display_model.py cad/wash_mvp_spec.json`, then print `cad/display/<chip_id>/<chip_id>_display_print.stl` (one file, all parts in place; per-part STLs are alongside if you want colours).

## Design figures (`cad/figures/`)

```
python cad/make_figures.py                      # every spec in chip/
python cad/make_figures.py --chip chip/<spec>.json
```

Writes SVG, which GitHub renders inside markdown, so a drawing can sit in a README or
a PR. Per chip: `_layout` (plan view, to scale), `_stack` (the layer stack and which
process makes each layer), `_features` (every feature size against the process
floors), plus `_barrier` and `_schedule` where the chip has them. **Generated — don't
edit the SVGs by hand.** Channel geometry is imported from `make_train_cad.py` rather
than re-derived, so a drawing and the DXF cut path cannot disagree.

It also **checks the plan for collisions**, which is most of why it is worth running.
This is a single-layer chip with no vias, so two channels that touch in plan view are
one channel, and a port drilled on top of a channel it doesn't belong to is a short
circuit. Neither shows up in a JSON spec and neither is checked by
`tools/check_chip_spec.py`. It exits non-zero if it finds one, and always prints the
tightest clearance between unconnected features.

This found four real short circuits in an earlier chip on its first run.

## What goes in this folder

- `exports/`: STL/GLB files the twin reads (commit these)
- source CAD files: fine to commit if small; large files belong in a shared drive, linked here
- **no** vendor quotes, supplier part files or third-party designs (see CONFIDENTIALITY.md)

First STL hand-off: **Saturday 17:00**.

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

## What goes in this folder

- `exports/`: STL/GLB files the twin reads (commit these)
- source CAD files: fine to commit if small; large files belong in a shared drive, linked here
- **no** vendor quotes, supplier part files or third-party designs (see CONFIDENTIALITY.md)

First STL hand-off: **Saturday 17:00**.

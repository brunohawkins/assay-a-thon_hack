# chip/ (owners: Yuvraj + Szonja)

Natural language → chip spec. Someone describes the assay; this part designs the
capsule-processor chip for it as a `chip_spec.json`.

The format is explained, with examples, in [`contracts/README.md`](../contracts/README.md#2-chip_specjson-yuvraj--szonja).

## Files

| Path | What |
|---|---|
| `placeholder_chip_spec.json` | Placeholder chip: inlet → exchange chamber → 4 split chambers → pool → outlet on a 75 × 25 mm slide. Everyone builds against this until the real spec lands. |
| `ipsc_to_mgl_chip_v1.json` | **Real.** The iPSC-to-microglia train, laser-cut. |
| `module1_v1.json` | **Real.** Module 1: three culture chambers (neurons, glia, astrocytes) in a Y on 140 × 75 mm, routed by which port is driven, vented or capped — driven by syringe pumps, not pressure. No valves, no barriers; every feature ≥ 0.3 mm, so it is laser-cut only: no photolithography, cast layer, PDMS or glass. |
| `nl_to_spec/` | The NL → spec code. A stub for now that returns the placeholder. See its README. |

## Replacing the placeholder

1. Generate or write the new spec, e.g. `chip/chip_v1.json`, with a new `id` (`chip_v1`).
2. **Keep the ID convention**: `exchange_1`, `split_1` … `split_4`, `pool_1`, `inlet_1`,
   `outlet_1`. Protocols, STL files and the twin all refer to parts by these IDs. If
   you rename or add parts, tell MJ/Kilian (protocol targets) and Ayrton (STL names).
3. Connect parts with `channels` (`from` → `to`). Split and pool follow the channels.
4. Check it: `python tools/validate.py` (catches bad IDs, channels to nowhere, parts
   outside the footprint, volumes that don't fit the box).
5. See it: `python operator/run.py --all --chip chip/chip_v1.json`, then open the twin.

Hand-off: `chip_spec.json` **Saturday 16:00**.

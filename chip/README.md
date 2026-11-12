# chip/ (owners: Yuvraj + Szonja)

Natural language → chip spec. Someone describes the assay; this part designs the
capsule-processor chip for it as a `chip_spec.json`.

The format is explained, with examples, in [`contracts/README.md`](../contracts/README.md#2-chip_specjson-yuvraj--szonja).

## Scale: these are DEMO chips, not the real device

`contracts/blocks.json` records **three** scales, and only one of them is a maximum:

| Scale | Size | What it is |
|---|---|---|
| **DEMO** | 30 × 20 mm | What every spec in `chip/` is built to, and what we can print and emboss today |
| **TARGET** | 127.76 × 85.48 mm | The design target for the real device: the ANSI/SLAS 1-2004 microplate footprint |
| **CEILING** | 290 × 190 mm | The agreed absolute maximum chip size. Nothing in the design needs it |

Every spec in this folder is **DEMO SCALE**: 30 × 20 mm, one fluidic layer, resin-printed
and hot-embossed. Most were scaled down from a larger drawing by `tools/fit_chip.py`, and
each spec's `description` says by how much and what capability that cost. **None of them is
the real device** — the real device is designed at TARGET scale, where the 6-port demo limit
lifts and lanes get dedicated feeds instead of sharing off-chip selector valves.

## Retention is a fabricated depth weir

The 200 µm fabrication floor is **in plane only**; z is unconstrained. So capsule retention
is a **depth weir fabricated on the same single layer**: a crest about 275 µm tall under a
300 µm lid leaves a 25 µm gap in z. Every chip on disk that retains capsules records
`retention: {type: weir, opening_um: 25, source: fabricated}`.

A bought mesh insert and sedimentation are **fallbacks** if the weir coupon fails, not the
answer. (Run a 40 / 60 / 80 µm gap coupon first: the weir is designed, not yet tested.)

## Files

| Path | What |
|---|---|
| `placeholder_chip_spec.json` | **Placeholder, not a real design.** Generic development layout: inlet → exchange chamber → 4 split chambers → pool → outlet, 30 × 20 mm (scaled 0.389 from the original 75 × 25 mm drawing). Everyone builds against this when they just need *a* chip. |
| `module1_v1.json` | **Real.** Module 1, pipeline stage 1: three culture chambers (neurons, glia, astrocytes) in a Y, routed by which port is driven, vented or capped — syringe pumps, not pressure. No valves, no barriers. |
| `cell_prep_v1.json` | **Real.** Stage 2: cell prep — dissociate, quench, wash, filter, count, resuspend. |
| `spc_demo_v1.json` | **Real.** Stage 3: encapsulation. Authored directly at demo scale (scale factor 1.0), and the only encapsulation geometry we can actually fabricate. |
| `module3_v1_translated.json` | Stage 4: the main capsule trap — load, lysis, wash, RT, wash, release. Translated from the frozen source record in `modules/` (see below); still marked `placeholder`. |
| `module4_v1.json` | **Real.** Stage 5: splits the pooled capsules into four sublibraries, indexes each, thermocycles in place. |
| `module5_v1.json` | **Real.** Stage 6: opens the capsule shells with a matrix-digesting enzyme at 37 °C, captures the DNA on magnetic beads, washes and elutes a library. |
| `module6_v1.json` | **Real.** The bulk arm: metabolome *and* proteome from one cold extraction of adherent cells, three lanes that never share a wetted path. Not part of the single-cell chain. |
| `sctip_spc_v1.json` | **Partly retired.** Stage A duplicated `spc_demo_v1` and is gone; stages B and C duplicate `module3_v1_translated`. Don't build against it. |
| `ipsc_to_mgl_chip_v1.json` | **Real, but a separate experiment.** The iPSC-to-microglia train, end to end. Not in the pipeline chain. |
| `modules/` | **READ-ONLY frozen source record, NOT a chip spec.** See below. |
| `fusion/` | Fusion 360 script that builds the positive embossing mould (one STL per spec) from `chip/*.json`. |
| `nl_to_spec/` | The NL → spec code. A stub for now that returns the placeholder. See its README. |

Which chip does which job, which are retired and where the joins are broken:
[`protocols/pipeline_status.md`](../protocols/pipeline_status.md).

## `modules/` is not a spec directory

`chip/modules/` holds a **frozen source record** of the original Step 3 sizing work
(`module3_step1_3.json` plus its SVGs). It is **not** a chip spec, it is not in the
`chip_spec.json` format, and nothing is built from it directly — `tools/convert_module3.py`
translates it into `module3_v1_translated.json`, and that translated spec is the chip.

`tools/validate.py` and `tools/check_chip_spec.py` glob `chip/*.json`, which does **not**
recurse, so `modules/` is **deliberately excluded** from validation. Treat it as history:
don't edit it to change a design, and don't treat it as a chip.

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

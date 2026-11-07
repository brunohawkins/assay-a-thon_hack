# Prompt to chip: cached result (Yuvraj's tool)

Read-only copies of what Yuvraj's prompt-to-chip tool produced on branch
`chip/flowmatter-paper-to-chip` (not merged), so the presenter's first step runs offline.
Refresh with `python tools/cache_prompt_demo.py` (git fetch first); `check.json` records the
commit it came from.

| File | From the branch | What it is |
|---|---|---|
| `design.json` | `chip/designs/capsule_prep_v1.json` | Steps 1–15 of the cell prep and encapsulation table grouped into Flowmatter library parts (Claude does the grouping, following `chip/designs/README.md`) |
| `chip_spec.json` | `chip/capsule_prep_v1.json` | The chip spec `chip/components/build_chip.py` built from it: our contract format |
| `stl/<module>.stl` | `cad/exports/capsule_prep_v1/` | Each placed library part, mm, chip origin |
| `check.json` | (ours) | Our schema check and `tools/check_chip_spec.py` on that spec |

## What the tool does

1. `chip/components/build_library.py` splits the Flowmatter block sheet (`cad/BP_assayathon_chips.stl`)
   into one STL per block (`cad/library/`) and writes `chip/components/catalogue.json`: each
   part's size, volume, which chip-spec chamber type it maps to, and how sure the part name is.
2. A design file (`chip/designs/<id>.json`) lists the modules in flow order: each module is
   one library part, the protocol steps it covers, and any reagent inlets; steps that stay off
   the chip or need a part the library lacks are listed separately. Claude writes this from a
   paper or protocol table, following the grouping rules in `chip/designs/README.md`; there is
   no LLM call in the code.
3. `chip/components/build_chip.py <design>` places the parts in a row 5 mm apart, writes
   `chip/<id>.json` (validated against our chip-spec schema before writing), one placed STL
   per part plus a whole-chip STL in `cad/exports/<id>/`, and a Fusion 360 script that imports
   the parts, and prints which protocol step went where.

Limits it states itself: parts are not rotated or joined by drawn channels (the spec's channels
say which part feeds which; joining them is a manual CAD step), some part names are guesses,
and non-chamber parts (mixers, junctions, spirals) are typed `exchange` / `pool` / `split`.

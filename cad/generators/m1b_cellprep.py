"""Module 1b, automated cell prep (REAL scale, DESIGNED, SIMULATED: not printed this weekend).

    python cad/generators/m1b_cellprep.py          # writes cad/m1b_cellprep_spec.json

The bridge between Module 1 and Module 2: cells harvested from m1_out_1/2/3 come in at the
same heights, are quenched, washed, filtered, counted and resuspended in dextran buffer at
the concentration Module 2 needs, then leave for Module 2's core inlet.

Top view (75 x 25 mm; x positions from the design brief):

  cells_in_1 (4,19) --+        quench_in        wash_in                       buffer_in
  cells_in_2 (4,12.5)-merge -> [T + serpentine] -> [cell wash] -> [filter] -> [QC] -> [resuspend] -> to_module2 (71,12.5)
  cells_in_3 (4,6) ---+  (8)      x 10-22           x 25-37       x 40-48   x 51-57    x 60-67
                                                      |
                                                    waste

The filter gap must be smaller than Module 2's nozzle: tools/check_chip_spec.py checks it
across the hand-off in protocols/pipeline_mvp.json. The QC window has a simulated counter
(operator/cellprep.py); the operator sets the dilution at m1b_resuspend_1 from it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genlib  # noqa: E402
from genlib import r  # noqa: E402

NAME = "m1b_cellprep"
OUT = genlib.ROOT / "cad" / "m1b_cellprep_spec.json"
BASE_MM, FLUIDIC_MM, LID_MM = 1.0, 0.5, 1.0
PORT_UM = 500
CY = 12.5
LANES = [(1, 19.0), (2, 12.5), (3, 6.0)]                 # same heights as m1_out_1/2/3
STAGES = {                                                 # id: (x0, x1) from the design brief
    "m1b_mix_1": (10.0, 22.0), "m1b_cellwash_1": (25.0, 37.0), "m1b_filter_1": (40.0, 48.0),
    "m1b_qc_window_1": (51.0, 57.0), "m1b_resuspend_1": (60.0, 67.0),
}


def build(p: dict) -> dict:
    w, d = p["channel_width_mm"], p["channel_depth_mm"]
    top = BASE_MM + FLUIDIC_MM + LID_MM
    zc = lambda depth: r(BASE_MM + depth / 2)  # noqa: E731

    def box(cid, typ, height_y, depth, **extra):
        x0, x1 = STAGES[cid]
        out = {"id": cid, "type": typ, "center": [r((x0 + x1) / 2, 3), CY, zc(depth)], "size": [r(x1 - x0, 3), height_y, r(depth)],
               "volume_ul": r((x1 - x0) * height_y * depth, 4)}
        out.update(extra)
        return out

    def port(pid, typ, xy, role, note=None):
        out = {"id": pid, "type": typ, "position": [xy[0], xy[1], top], "role": role, "width_um": PORT_UM}
        if note:
            out["note"] = note
        return out

    def chan(cid, a, b, carries, path):
        return {"id": cid, "from": a, "to": b, "width_mm": w, "depth_mm": d, "carries": carries,
                "path": [[r(x, 3), r(y, 3)] for x, y in path]}

    mx0, mx1 = STAGES["m1b_mix_1"]
    serp_len = p["mixer_turns"] * 4.0 + (mx1 - mx0)
    chambers = [
        {"id": "m1b_merge_1", "type": "junction", "center": [8.0, CY, zc(d)], "size": [0.4, 0.4, d], "volume_ul": r(0.4 * 0.4 * d, 4),
         "note": "The three Module 1 lanes merge."},
        box("m1b_mix_1", "mixer", 5.0, d, volume_ul=r(serp_len * p["mixer_width_mm"] * d, 4),
            mixer={"kind": "serpentine", "channel_width_mm": p["mixer_width_mm"], "turns": p["mixer_turns"], "length_mm": r(serp_len, 2)},
            note="Quench: the quench medium joins at a T-junction, then a serpentine mixes it in."),
        box("m1b_cellwash_1", "cell_wash", 6.0, p["cellwash_depth_mm"],
            note="Cell wash block, METHOD TBC (sheet: DLD array, spiral inertial channel or density cushion)."),
        box("m1b_filter_1", "filter", 3.0, d, filter={"type": "pillars", "gap_um": p["filter_gap_um"], "purpose": "cells"},
            note="Pillar filter strains out cell clumps before encapsulation."),
        box("m1b_qc_window_1", "qc_window", 1.0, d,
            note="Optical window: a counter reads cell concentration and viability (simulated: operator/cellprep.py)."),
        box("m1b_resuspend_1", "mixer", p["resuspend_width_mm"], 0.2,
            mixer={"kind": "wide_short", "channel_width_mm": p["resuspend_width_mm"]},
            note="Wide, short mixer: resuspends the cells in dextran buffer at the dilution the operator sets."),
    ]
    ports = [port(f"m1b_cells_in_{n}", "inlet", (4.0, y), "cells", f"From m1_out_{n} (Module 1).") for n, y in LANES]
    ports += [
        port("m1b_quench_in_1", "inlet", (10.5, 19.5), "reagent", "Quench medium (neutralises the detach reagent)."),
        port("m1b_wash_in_1", "inlet", (31.0, 20.0), "reagent", "Cell wash buffer."),
        port("m1b_waste_1", "outlet", (31.0, 5.0), "waste"),
        port("m1b_buffer_in_1", "inlet", (61.0, 18.0), "reagent", "Dextran buffer (encapsulation buffer)."),
        port("m1b_to_module2_1", "outlet", (71.0, CY), "transfer", "To Module 2's core inlet (m2_core_in_1)."),
    ]
    channels = []
    for n, y in LANES:
        path = [(4.0, y), (8.0, y)] if y == CY else [(4.0, y), (8.0, y), (8.0, CY)]
        channels.append(chan(f"m1b_ch_in_{n}", f"m1b_cells_in_{n}", "m1b_merge_1", "cells", path))
    seq = ["m1b_merge_1", "m1b_mix_1", "m1b_cellwash_1", "m1b_filter_1", "m1b_qc_window_1", "m1b_resuspend_1"]
    names = ["mix", "cellwash", "filter", "qc", "resuspend"]
    for (a, b), nm in zip(zip(seq[:-1], seq[1:]), names):
        xa = 8.0 if a == "m1b_merge_1" else STAGES[a][1]
        channels.append(chan(f"m1b_ch_to_{nm}", a, b, "cells", [(xa, CY), (STAGES[b][0], CY)]))
    channels += [
        chan("m1b_ch_out", "m1b_resuspend_1", "m1b_to_module2_1", "cells", [(STAGES["m1b_resuspend_1"][1], CY), (71.0, CY)]),
        chan("m1b_ch_quench", "m1b_quench_in_1", "m1b_mix_1", "liquid", [(10.5, 19.5), (10.5, CY + 2.5)]),
        chan("m1b_ch_wash_in", "m1b_wash_in_1", "m1b_cellwash_1", "liquid", [(31.0, 20.0), (31.0, CY + 3.0)]),
        chan("m1b_ch_waste", "m1b_cellwash_1", "m1b_waste_1", "liquid", [(31.0, CY - 3.0), (31.0, 5.0)]),
        chan("m1b_ch_buffer", "m1b_buffer_in_1", "m1b_resuspend_1", "liquid", [(61.0, 18.0), (61.0, CY + p["resuspend_width_mm"] / 2)]),
    ]
    return {
        "id": "m1b_cellprep_v1",
        "name": "Module 1b: cell prep (real scale, simulated)",
        "description": ("Automated bridge from Module 1 to Module 2: merge the three lanes, quench (T-junction + serpentine), "
                        "wash the cells (method TBC), filter out clumps, count at a QC window, resuspend in dextran buffer "
                        "at the dilution the operator sets, and send the cells to Module 2's core inlet. Designed and "
                        "simulated; not printed this weekend. GENERATED by cad/generators/m1b_cellprep.py."),
        "placeholder": True,
        "module": {"id": "m1b", "name": "Cell prep", "status": "designed", "printed": False},
        "generator": {"script": "cad/generators/m1b_cellprep.py",
                      "params_schema": "contracts/schemas/generators/m1b_cellprep.schema.json", "params": p},
        "units": "mm",
        "footprint": {"x": p["footprint_x_mm"], "y": p["footprint_y_mm"]},
        "layers": [
            {"id": "m1b_base", "material": "pmma", "thickness_mm": BASE_MM},
            {"id": "m1b_fluidic", "material": "pmma", "thickness_mm": FLUIDIC_MM},
            {"id": "m1b_lid", "material": "pmma", "thickness_mm": LID_MM},
        ],
        "cell_filter": {"gap_um": p["filter_gap_um"]},
        "chambers": chambers,
        "channels": channels,
        "ports": ports,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        params, args = genlib.parse_params(NAME, argv, "Module 1b cell prep -> cad/m1b_cellprep_spec.json")
        spec = build(params)
        out = args.out or OUT
        genlib.write_spec(spec, out)
    except genlib.ParamError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"wrote {genlib.rel(out)}: {len(spec['chambers'])} stages, {len(spec['channels'])} channels, {len(spec['ports'])} ports")
    return genlib.finish(out, summary(spec), args)


def summary(spec: dict) -> list[str]:
    import safety
    import schema_io

    m2 = schema_io.load_json(genlib.ROOT / "cad" / "m2_encapsulation_spec.json")
    t = safety.cell_prep_target(m2)
    return [
        "Module 1b summary (cells, not capsules):",
        "  stages                      " + " -> ".join(c["id"].removeprefix("m1b_") for c in spec["chambers"]),
        f"  cell filter                 {spec['cell_filter']['gap_um']:g} um (Module 2 nozzle: "
        f"{m2['encapsulation']['nozzle_um']:g} um)",
        f"  Module 2 needs              ~{t['target_cells_per_ul']:,.0f} cells/uL for {t['target_mean']:g} cells per capsule "
        f"({t['capsules_per_hour']:,.0f} capsules/h from {t['core_flow_ul_h']:g} uL/h of core stream); accepted "
        f"{t['mean_low']:.3f}-{t['mean_high']:.3f}; viability >= {t['viability_min']:.0%}",
        "  runs                        python operator/run.py --scenario scenarios/m1b_cellprep_demo.json (simulated counter)",
    ]


if __name__ == "__main__":
    raise SystemExit(main())

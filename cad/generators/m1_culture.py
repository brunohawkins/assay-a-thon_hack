"""Module 1, culture chip (REAL scale, DESIGNED).

    python cad/generators/m1_culture.py            # writes cad/m1_culture_spec.json

One unit per chip, printed twice: "control" and "interaction" are two instances of this
same spec, chosen per run by the run parameter "variant" (operator/run.py --variant).
The safety checker blocks opening any pair valve in the control variant.

Top view (75 x 25 mm, origin bottom-left; not to scale):

  ctrl_1          ctrl_5  bt_1(medium_in_1)
    |               |       |
    |  factor_1     |    [neuron_1]------------------------------------> out_1 (71,19)
    |  +---v-----...-------' / |  \\
  [ipsc]-split-valve_branch    |   valve_pair_2      bt_2(medium_in_2)
    (8,12.5) (14) (17)  (21)  sig_1  \\              |
           +--v-----...------------------> [astrocyte_1]--------> out_2 (71,12.5)
           |  factor_2          |   /
           +--v-----...--- [microglia_1]---------------------------> out_3 (71,6)
              factor_3          bt_3(medium_in_3)

  Lanes, numbered top to bottom like the outlets: 1 = neuron (y 19), 2 = astrocyte
  (y 12.5), 3 = microglia (y 6). Each lane: seed channel (with a normally-closed branch
  valve at x 17), a factor port joining at x 21 (m1_join_n), a medium inlet over a bubble
  trap, the culture chamber, and an outlet at x 71.

  Pair links (signals-only microgrooves, each behind a pair valve):
    m1_sig_1 neuron-microglia, m1_sig_2 neuron-astrocyte, m1_sig_3 microglia-astrocyte.
  m1_sig_1 has to cross lane 2's feed channel: tools/check_chip_spec.py reports it.

Positions come from the Module 1 design brief; parameters only resize parts.
Don't hand-edit the JSON: change parameters and re-run.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genlib  # noqa: E402
from genlib import edge_toward, r  # noqa: E402

NAME = "m1_culture"
OUT = genlib.ROOT / "cad" / "m1_culture_spec.json"

BASE_MM, CONTROL_MM = 1.0, 1.0          # PMMA base; control layer (valve lines, gas window) on top
PORT_UM = 500
SEED_XY, SPLIT_XY = (8.0, 12.5), (14.0, 12.5)
BRANCH_X, JOIN_X, OUT_X = 17.0, 21.0, 71.0
LANES = [  # n, chamber id, centre, bubble trap centre, valve control port for the branch valve
    (1, "m1_neuron_1", (38.0, 19.0), (38.0, 23.0), (17.0, 23.5)),
    (2, "m1_astrocyte_1", (50.0, 12.5), (50.0, 17.0), (24.0, 1.5)),
    (3, "m1_microglia_1", (38.0, 6.0), (38.0, 2.0), (17.0, 1.5)),
]
PAIRS = [  # n, chamber a, chamber b, control port
    (1, "m1_neuron_1", "m1_microglia_1", (31.0, 1.5)),
    (2, "m1_neuron_1", "m1_astrocyte_1", (44.0, 23.5)),
    (3, "m1_microglia_1", "m1_astrocyte_1", (44.0, 1.5)),
]


def build(p: dict) -> dict:
    depth, ch_w, ch_d = p["chamber_depth_mm"], p["channel_width_mm"], p["channel_depth_mm"]
    top = BASE_MM + depth + CONTROL_MM
    cz = r(BASE_MM + depth / 2)
    jz = r(BASE_MM + ch_d / 2)
    rc, rs, rb = p["culture_chamber_diameter_mm"] / 2, p["seed_chamber_diameter_mm"] / 2, p["bubble_trap_diameter_mm"] / 2
    centers = {cid: c for _, cid, c, _, _ in LANES}

    def cyl(cid, typ, xy, rad, note=None):
        c = {"id": cid, "type": typ, "shape": "cylinder", "center": [xy[0], xy[1], cz],
             "size": [2 * rad, 2 * rad, depth], "volume_ul": r(math.pi * rad * rad * depth, 3)}
        if note:
            c["note"] = note
        return c

    def junction(cid, xy):
        return {"id": cid, "type": "junction", "center": [xy[0], xy[1], jz], "size": [0.4, 0.4, ch_d],
                "volume_ul": r(0.4 * 0.4 * ch_d, 4)}

    def port(pid, typ, xy, role, note=None):
        out = {"id": pid, "type": typ, "position": [xy[0], xy[1], top], "role": role, "width_um": PORT_UM}
        if note:
            out["note"] = note
        return out

    def chan(cid, a, b, carries, path=None, w=ch_w, d=ch_d, **extra):
        out = {"id": cid, "from": a, "to": b, "width_mm": w, "depth_mm": d, "carries": carries}
        if path:
            out["path"] = [[r(x, 3), r(y, 3)] for x, y in path]
        out.update(extra)
        return out

    chambers = [cyl("m1_ipsc_1", "inlet", SEED_XY, rs, "Cells (expanded off-chip) are loaded here, then split three ways."),
                junction("m1_split_1", SPLIT_XY)]
    ports = [port("m1_ipsc_in_1", "inlet", SEED_XY, "cells")]
    channels = [chan("m1_ch_ipsc_in", "m1_ipsc_in_1", "m1_ipsc_1", "cells"),
                chan("m1_ch_ipsc", "m1_ipsc_1", "m1_split_1", "cells", [(SEED_XY[0] + rs, 12.5), SPLIT_XY])]
    valves = []
    for n, cid, (x, y), bt, ctrl in LANES:
        chambers.append(cyl(cid, "culture", (x, y), rc))
        chambers.append(junction(f"m1_join_{n}", (JOIN_X, y)))
        chambers.append(cyl(f"m1_bt_{n}", "bubble_trap", bt, rb, "Bubble trap: bubbles in the medium line rise and stay here."))
        ports += [port(f"m1_factor_{n}", "inlet", (JOIN_X, y), "factor"),
                  port(f"m1_medium_in_{n}", "inlet", bt, "medium"),
                  port(f"m1_out_{n}", "outlet", (OUT_X, y), "harvest")]
        seed_path = [SPLIT_XY, (JOIN_X, y)] if y == SPLIT_XY[1] else [SPLIT_XY, (SPLIT_XY[0], y), (JOIN_X, y)]
        west = (x - rc, y)
        channels += [
            chan(f"m1_ch_seed_{n}", "m1_split_1", f"m1_join_{n}", "cells", seed_path),
            chan(f"m1_ch_factor_{n}", f"m1_factor_{n}", f"m1_join_{n}", "liquid"),
            chan(f"m1_ch_feed_{n}", f"m1_join_{n}", cid, "cells", [(JOIN_X, y), west]),
            chan(f"m1_ch_medium_in_{n}", f"m1_medium_in_{n}", f"m1_bt_{n}", "liquid"),
            chan(f"m1_ch_medium_{n}", f"m1_bt_{n}", cid, "liquid",
                 [(bt[0], bt[1] - rb if bt[1] > y else bt[1] + rb), (x, y + rc if bt[1] > y else y - rc)]),
            chan(f"m1_ch_out_{n}", cid, f"m1_out_{n}", "cells", [(x + rc, y), (OUT_X, y)]),
        ]
        ports.append(port(f"m1_ctrl_{n}", "control", ctrl, "control"))
        valves.append({"id": f"m1_valve_branch_{n}", "type": "normally_closed", "kind": "branch",
                       "channel": f"m1_ch_seed_{n}", "position": [BRANCH_X, y, BASE_MM + depth],
                       "control_port": f"m1_ctrl_{n}",
                       "note": "Open while seeding; closed afterwards so each lane is separate."})
    groove_note = ("Signals-only microgrooves (soluble factors, vesicles, axons; not cells): too small to fabricate "
                   "here. Size is a placeholder.")
    for n, a, b, ctrl in PAIRS:
        pa, pb = edge_toward(centers[a], rc, centers[b]), edge_toward(centers[b], rc, centers[a])
        channels.append(chan(f"m1_sig_{n}", a, b, "signals", [pa, pb], w=r(p["microgroove_width_um"] / 1000),
                             d=r(p["microgroove_depth_um"] / 1000), capsule_passable=False, fabricable=False,
                             note=groove_note))
        mid = [(pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2]
        if n == 1:
            mid[1] += 2.1  # keep the valve off the point where m1_sig_1 crosses lane 2
        ports.append(port(f"m1_ctrl_{3 + n}", "control", ctrl, "control"))
        valves.append({"id": f"m1_valve_pair_{n}", "type": "normally_closed", "kind": "pair",
                       "channel": f"m1_sig_{n}", "position": [r(mid[0], 3), r(mid[1], 3), BASE_MM + depth],
                       "control_port": f"m1_ctrl_{3 + n}", "connects": [a, b],
                       "note": "Opened only in the interaction variant; the safety checker blocks it in control."})

    culture_ids = [cid for _, cid, _, _, _ in LANES]
    fx, fy = p["footprint_x_mm"], p["footprint_y_mm"]
    return {
        "id": "m1_culture_v1",
        "name": "Module 1: culture chip (real scale)",
        "description": ("One culture unit per chip, printed twice (control and interaction). Cells are loaded into "
                        "m1_ipsc_1, split into three lanes, differentiated in three chambers with their own media "
                        "and factors, optionally linked through signals-only microgrooves behind pair valves, then "
                        "harvested through m1_out_1/2/3. GENERATED by cad/generators/m1_culture.py."),
        "placeholder": True,
        "module": {"id": "m1", "name": "Culture", "status": "designed", "printed": True},
        "generator": {"script": "cad/generators/m1_culture.py",
                      "params_schema": "contracts/schemas/generators/m1_culture.schema.json", "params": p},
        "units": "mm",
        "footprint": {"x": fx, "y": fy},
        "layers": [
            {"id": "m1_base", "material": "pmma", "thickness_mm": BASE_MM},
            {"id": "m1_fluidic", "material": "pmma", "thickness_mm": depth},
            {"id": "m1_control", "material": "pmma", "thickness_mm": CONTROL_MM},
        ],
        "run_parameters": {"variant": {
            "values": ["control", "interaction"], "default": "control",
            "description": "Same chip, two instances: control keeps every pair valve closed; interaction opens them "
                           "so the three cell types can signal to each other."}},
        "chambers": chambers,
        "channels": channels,
        "ports": ports,
        "valves": valves,
        "features": [
            {"id": "m1_gas_window_1", "type": "gas_window", "center": [44.0, 12.5, r(top - 0.05)],
             "size": [21.0, 22.0, 0.1], "covers": culture_ids,
             "note": "Gas-permeable window in the control layer over the culture chambers."},
            {"id": "m1_heat_zone_1", "type": "heat_zone", "center": [29.0, 12.5, 0.05], "size": [52.0, 24.0, 0.1],
             "temp_c": p["culture_temp_c"], "covers": ["m1_ipsc_1"] + culture_ids,
             "note": "Heater under the seeding and culture chambers."},
        ],
        "thermal_zones": [{"id": "m1_heat_zone_1", "chambers": ["m1_ipsc_1"] + culture_ids,
                           "max_temp_c": p["culture_temp_c"], "min_temp_c": p["culture_temp_c"], "purpose": "culture"}],
    }


def main(argv: list[str] | None = None) -> int:
    try:
        params, args = genlib.parse_params(NAME, argv, "Module 1 culture chip -> cad/m1_culture_spec.json")
        spec = build(params)
        out = args.out or OUT
        genlib.write_spec(spec, out)
    except genlib.ParamError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"wrote {genlib.rel(out)}: {len(spec['chambers'])} chambers, {len(spec['channels'])} channels, "
          f"{len(spec['ports'])} ports, {len(spec['valves'])} valves, {len(spec['features'])} features")
    return genlib.finish(out, summary(spec), args, [(genlib.ROOT / "protocols" / "m1_culture.json", OUT)])


def summary(spec: dict) -> list[str]:
    ch = {c["id"]: c for c in spec["chambers"]}
    cult = [ch[cid] for _, cid, _, _, _ in LANES]
    return [
        "Module 1 summary (cells, not capsules: no capsule sizing):",
        f"  seeding chamber             m1_ipsc_1, {ch['m1_ipsc_1']['volume_ul']:g} uL",
        "  culture chambers            " + ", ".join(f"{c['id']} {c['volume_ul']:g} uL" for c in cult),
        f"  bubble traps                {ch['m1_bt_1']['volume_ul']:g} uL each",
        f"  valves                      {sum(v['kind'] == 'branch' for v in spec['valves'])} branch + "
        f"{sum(v['kind'] == 'pair' for v in spec['valves'])} pair (normally closed); variants: "
        f"{', '.join(spec['run_parameters']['variant']['values'])} (pair valves blocked in control)",
        f"  heat zone                   {spec['features'][1]['temp_c']:g} C under {len(spec['features'][1]['covers'])} chambers",
    ]


if __name__ == "__main__":
    raise SystemExit(main())

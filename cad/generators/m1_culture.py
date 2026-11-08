"""Module 1, culture chip (DEMO SCALE: 30 x 20 mm, one fluidic layer, one depth).

    python cad/generators/m1_culture.py            # writes cad/m1_culture_spec.json and protocols/m1_culture.json

One unit per chip, printed twice: "control" and "interaction" are two instances of this
same spec, chosen per run by the run parameter "variant" (operator/run.py --variant).
The chip has no valves (a membrane valve needs a second layer), so the variant only
changes how the lane inlets are driven; see the protocol's step ports.

Fabrication rules come from contracts/blocks.json: ONE fluidic layer at ONE depth (every
chamber, junction and channel is the envelope depth, 0.3 mm), channels at least the open
channel floor wide (narrower requests are clamped UP), at least the minimum wall between
features, ports at the minimum port size and pitch, and at most the maximum port count.
Every chamber and channel names the CAD block it is built from.

Top view (30 x 20 mm, origin bottom-left; not to scale). Lanes are numbered top to bottom:
1 = neuron, 2 = astrocyte, 3 = microglia.

                 factor_1 (port)
                    |
   +--(seed trunk)--J1--[bt_1]--(neuron_1)--+
   |                                        |
 ipsc_in -(ipsc_1)-S   factor_2 -J2--[bt_2]--(astrocyte_1)--CJ--- out_1 (port)
   |                    (port)              |
   +--(seed trunk)--J3--[bt_3]--(microglia_1)+
                    |
                 factor_3 (port)

S splits the seeded cells three ways, each lane has a T-junction (J) where its reagent port
joins, an in-line bubble trap (bt), a culture chamber, and a collector (CJ) that drains all
three lanes to the one outlet. Five ports in all.

The wall shear in the chamber that a protocol step targets is held at or below
SHEAR_USE x 0.01 dyn/cm2 (tau = 6 mu Q / (w h^2), w = chamber size[1], h = size[2]) and the
step volumes are the chamber volumes, so protocols/m1_culture.json is derived from the
geometry here. Don't hand-edit the JSON: change parameters and re-run.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genlib  # noqa: E402
import schema_io  # noqa: E402
from genlib import r  # noqa: E402

NAME = "m1_culture"
OUT = genlib.ROOT / "cad" / "m1_culture_spec.json"
PROTOCOL = genlib.ROOT / "protocols" / "m1_culture.json"

BLOCKS = schema_io.load_json(genlib.ROOT / "contracts" / "blocks.json")
ENV, FLOORS = BLOCKS["envelope"], BLOCKS["fabrication_floors"]
DEPTH_MM = ENV["max_depth_mm"]                  # the ONE etch depth of every chamber and channel
MIN_CHANNEL_MM = FLOORS["min_open_channel_mm"]
WALL_MM = FLOORS["min_wall_mm"]
PORT_UM = FLOORS["min_port_mm"] * 1000
PORT_PITCH_MM = FLOORS["min_port_pitch_mm"]
MAX_PORTS = FLOORS["max_ports_per_chip"]

BASE_MM, LID_MM = 1.0, 1.0                      # PMMA base and lid around the one fluidic layer
MU_PA_S = 0.8e-3                                # medium viscosity
SHEAR_LIMIT_DYN_CM2 = 0.01
SHEAR_USE = 0.8                                 # run at this fraction of the limit
LANE_EDGE_MM, LANE_GAP_MM = 3.2, 3.2            # room for the end lanes' ports; gap between lane chambers
BT_IN_MM, BT_OUT_MM = 2.5, 3.5                  # channel run into a bubble trap, and from it to the chamber
X_MARGIN_MM = 1.2                               # inlet port centre from the left edge
# Hold times that are not set by flow (incubations, culture periods). A step takes the longer of this
# and the time to push its volume at the shear-limited flow.
NOMINAL_S = {1: 120, 2: 300, 3: 1209600, 4: 1800, 5: 600, 6: 600, 7: 600, 8: 259200, 9: 259200,
             10: 600, 11: 600, 12: 600, 13: 900, 14: 300}

LANES = [(1, "m1_neuron_1"), (2, "m1_astrocyte_1"), (3, "m1_microglia_1")]


def block_for(path) -> str:
    """CAD block for a channel run: straight, one right-angle corner, or a folded run."""
    bends = sum(1 for a, b, c in zip(path, path[1:], path[2:])
                if (b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0]))
    return "straight_channel" if bends == 0 else "channel_corner" if bends == 1 else "serpentine_channel"


def shear_dyn_cm2(flow_ul_min: float, width_mm: float, depth_mm: float) -> float:
    """tau = 6 mu Q / (w h^2) of a wide slot, in dyn/cm2 (1 Pa = 10 dyn/cm2)."""
    q = flow_ul_min * 1e-9 / 60
    return 10 * 6 * MU_PA_S * q / (width_mm * 1e-3 * (depth_mm * 1e-3) ** 2)


def flow_limit_ul_min(chamber: dict) -> float:
    """Flow at which the chamber's wall shear reaches SHEAR_USE x the limit (serpentines: their own channel)."""
    widths = [chamber["size"][1]] + ([chamber["mixer"]["channel_width_mm"]] if chamber.get("mixer") else [])
    h = chamber["size"][2]
    q = SHEAR_USE * SHEAR_LIMIT_DYN_CM2 / 10 * min(widths) * 1e-3 * (h * 1e-3) ** 2 / (6 * MU_PA_S)
    return math.floor(q * 1e9 * 60 * 1000) / 1000


def build(p: dict) -> dict:
    fx, fy = min(p["footprint_x_mm"], ENV["max_x_mm"]), min(p["footprint_y_mm"], ENV["max_y_mm"])
    ch_w = max(p["channel_width_mm"], MIN_CHANNEL_MM)
    d_cap = (fy - 2 * LANE_EDGE_MM) / 2 - LANE_GAP_MM   # biggest round chamber the lane pitch allows
    rs = min(p["seed_chamber_diameter_mm"], d_cap) / 2
    rc = min(p["culture_chamber_diameter_mm"], d_cap) / 2
    rb = min(p["bubble_trap_diameter_mm"], d_cap) / 2
    junction_mm = max(ch_w, MIN_CHANNEL_MM)
    cz = r(BASE_MM + DEPTH_MM / 2)
    top = BASE_MM + DEPTH_MM + LID_MM

    y2 = fy / 2
    pitch = (fy - 2 * LANE_EDGE_MM) / 2
    lane_y = {1: y2 + pitch, 2: y2, 3: y2 - pitch}
    p0x = X_MARGIN_MM
    c0x = p0x + 0.4 + 1.6 + rs                       # seeding chamber
    sx = c0x + rs + 1.4                              # split junction
    jx = sx + 3.5                                    # lane T-junctions (reagent ports join here)
    btx = jx + BT_IN_MM + rb
    cx = btx + rb + BT_OUT_MM + rc                   # culture chambers
    cjx = cx + rc + 2.8                              # collector junction
    ox = fx - 1.5                                    # outlet port
    if ox - cjx < PORT_UM / 2000 + WALL_MM:
        raise genlib.ParamError("the lanes do not fit the footprint at these diameters: reduce them")

    def cyl(cid, typ, x, y, rad, block, note=None):
        c = {"id": cid, "type": typ, "shape": "cylinder", "center": [r(x, 3), r(y, 3), cz],
             "size": [r(2 * rad, 3), r(2 * rad, 3), DEPTH_MM], "volume_ul": r(math.pi * rad * rad * DEPTH_MM, 3),
             "block": block}
        if note:
            c["note"] = note
        return c

    def junction(cid, x, y, block, note=None):
        c = {"id": cid, "type": "junction", "center": [r(x, 3), r(y, 3), cz],
             "size": [junction_mm, junction_mm, DEPTH_MM], "volume_ul": r(junction_mm * junction_mm * DEPTH_MM, 4),
             "block": block}
        if note:
            c["note"] = note
        return c

    def port(pid, typ, x, y, role, note=None):
        out = {"id": pid, "type": typ, "position": [r(x, 3), r(y, 3), r(top)], "role": role, "width_um": PORT_UM}
        if note:
            out["note"] = note
        return out

    def chan(cid, a, b, carries, path):
        return {"id": cid, "from": a, "to": b, "width_mm": ch_w, "depth_mm": DEPTH_MM, "carries": carries,
                "path": [[r(x, 3), r(y, 3)] for x, y in path], "block": block_for(path)}

    chambers = [cyl("m1_ipsc_1", "inlet", c0x, y2, rs, "polygon_chamber",
                    "Cells (expanded off-chip) are loaded here, then split three ways."),
                junction("m1_split_1", sx, y2, "cross_junction"),
                junction("m1_collect_1", cjx, y2, "cross_junction",
                         "The three lane outlets merge here and drain to the one outlet port (m1_out_1).")]
    ports = [port("m1_ipsc_in_1", "inlet", p0x, y2, "cells",
                  "Shared inlet: cells, detach reagent and harvest buffer, picked by an off-chip selector valve.")]
    channels = [chan("m1_ch_ipsc_in", "m1_ipsc_in_1", "m1_ipsc_1", "cells", [(p0x, y2), (c0x - rs, y2)]),
                chan("m1_ch_ipsc", "m1_ipsc_1", "m1_split_1", "cells", [(c0x + rs, y2), (sx, y2)]),
                chan("m1_ch_out", "m1_collect_1", "m1_out_1", "cells", [(cjx, y2), (ox, y2)])]
    ports.append(port("m1_out_1", "outlet", ox, y2, "harvest",
                      "The one outlet: harvest, waste and supernatant from all three lanes."))
    for n, cid in LANES:
        y = lane_y[n]
        chambers += [
            junction(f"m1_join_{n}", jx, y, "t_junction"),
            cyl(f"m1_bt_{n}", "bubble_trap", btx, y, rb, "polygon_chamber",
                "Bubble trap, in-line on the lane between the reagent junction and the culture chamber: "
                "bubbles in the lane line rise and stay here."),
            cyl(cid, "culture", cx, y, rc, "culture_chamber"),
        ]
        fy_port = y + 2.2 if n == 1 else y - 2.2 if n == 3 else y - pitch / 2
        ports.append(port(f"m1_factor_{n}", "inlet", jx, fy_port, "factor",
                          "Lane reagent inlet: factor, medium and label for this lane, picked by an off-chip "
                          "selector valve."))
        seed = [(sx, y2), (jx, y)] if n == 2 else [(sx, y2), (sx, y), (jx, y)]
        out = [(cx + rc, y), (cjx, y2)] if n == 2 else [(cx + rc, y), (cjx, y), (cjx, y2)]
        channels += [
            chan(f"m1_ch_seed_{n}", "m1_split_1", f"m1_join_{n}", "cells", seed),
            chan(f"m1_ch_factor_{n}", f"m1_factor_{n}", f"m1_join_{n}", "liquid", [(jx, fy_port), (jx, y)]),
            chan(f"m1_ch_feed_{n}", f"m1_join_{n}", f"m1_bt_{n}", "cells", [(jx, y), (btx - rb, y)]),
            chan(f"m1_ch_medium_{n}", f"m1_bt_{n}", cid, "cells", [(btx + rb, y), (cx - rc, y)]),
            chan(f"m1_ch_out_{n}", cid, "m1_collect_1", "cells", out),
        ]
    culture_ids = [cid for _, cid in LANES]
    if len(ports) > MAX_PORTS:
        raise genlib.ParamError(f"{len(ports)} ports > {MAX_PORTS}")
    for i, a in enumerate(ports):
        for b in ports[i + 1:]:
            if math.dist(a["position"][:2], b["position"][:2]) < PORT_PITCH_MM:
                raise genlib.ParamError(f"ports {a['id']} and {b['id']} are closer than {PORT_PITCH_MM:g} mm")
    return {
        "id": "m1_culture_v1",
        "name": "Module 1: culture chip (demo scale)",
        "description": (f"DEMO SCALE: {fx:g} x {fy:g} mm, one fluidic layer at ONE depth ({DEPTH_MM:g} mm: every chamber, "
                        f"junction and channel), resin print + hot emboss. Channels are {ch_w:g} mm wide (clamped up to "
                        f"the {MIN_CHANNEL_MM:g} mm open-channel floor), ports are {PORT_UM:g} um at >= {PORT_PITCH_MM:g} mm "
                        f"pitch ({len(ports)} of {MAX_PORTS}), and every chamber and channel names its CAD block "
                        "(contracts/blocks.json). Round chambers are capped to what the lane pitch allows (seeding "
                        f"{2 * rs:g} mm, culture {2 * rc:g} mm, bubble trap {2 * rb:g} mm). There is no retention "
                        "or mesh on this chip: cells stay in a lane by flow control, not by a filter. Cells load at "
                        "m1_ipsc_in_1 (shared inlet, also detach reagent and harvest buffer), split three ways, each "
                        "lane gets its own reagent at a T-junction, flows through an in-line bubble trap into its "
                        "culture chamber and drains to the shared outlet. No valves, control ports, gas window or "
                        "signals-only microgrooves: those need a second layer or sub-200 um features. GENERATED by "
                        "cad/generators/m1_culture.py."),
        "placeholder": True,
        "module": {"id": "m1", "name": "Culture (brief draft)", "status": "superseded", "printed": False,
                   "superseded_by": "chip/module1_v1.json",
                   "note": "Superseded by MJ's Module 1 v1; kept for reference, out of the pipeline."},
        "generator": {"script": "cad/generators/m1_culture.py",
                      "params_schema": "contracts/schemas/generators/m1_culture.schema.json", "params": p},
        "units": "mm",
        "footprint": {"x": fx, "y": fy},
        "layers": [
            {"id": "m1_base", "material": "pmma", "thickness_mm": BASE_MM},
            {"id": "m1_fluidic", "material": "pmma", "thickness_mm": DEPTH_MM},
            {"id": "m1_lid", "material": "pmma", "thickness_mm": LID_MM},
        ],
        "run_parameters": {"variant": {
            "values": ["control", "interaction"], "default": "control",
            "description": "Same chip, two instances. Control drives every lane inlet so the lanes stay isolated; "
                           "interaction drives one lane inlet with the others vented, so conditioned medium crosses "
                           "the shared outlet junction."}},
        "chambers": chambers,
        "channels": channels,
        "ports": ports,
        "thermal_zones": [{"id": "m1_heat_zone_1", "chambers": ["m1_ipsc_1"] + culture_ids,
                           "max_temp_c": p["culture_temp_c"], "min_temp_c": p["culture_temp_c"], "purpose": "culture"}],
    }


def targets_of(step: dict) -> list[str]:
    return ([step["target"]] if step.get("target") else []) + list(step.get("targets") or [])


def derive_protocol(protocol: dict, spec: dict) -> dict:
    """Volumes, flows and durations of every step from the chip geometry (volume = one chamber volume;
    flow = the wall-shear-limited flow of the least tolerant target; time = the longer of the nominal hold
    and the time to push the volume)."""
    chambers = {c["id"]: c for c in spec["chambers"]}
    for step in protocol["steps"]:
        targets = targets_of(step)
        missing = [t for t in targets if t not in chambers]
        if missing or not targets:
            raise genlib.ParamError(f"step {step['step']} targets {', '.join(missing)}, not on the chip")
        vol = min(chambers[t]["volume_ul"] for t in targets)
        if step.get("volume_ul") is not None:
            step["volume_ul"] = vol
        if step.get("flow_ul_min") is not None:
            q = min(flow_limit_ul_min(chambers[t]) for t in targets)
            step["flow_ul_min"] = q
            push_s = math.ceil((step.get("washes") or 1) * vol / q * 60)
            if step.get("exchange_interval_s") and push_s > step["exchange_interval_s"]:
                raise genlib.ParamError(f"step {step['step']}: one exchange takes {push_s} s, longer than its interval")
            step["duration_s"] = max(NOMINAL_S[step["step"]], push_s)
    return protocol


def main(argv: list[str] | None = None) -> int:
    try:
        params, args = genlib.parse_params(NAME, argv, "Module 1 culture chip -> cad/m1_culture_spec.json")
        spec = build(params)
        out = args.out or OUT
        genlib.write_spec(spec, out)
        if not args.out:
            schema_io.write_json(PROTOCOL, derive_protocol(schema_io.load_json(PROTOCOL), spec))
    except genlib.ParamError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"wrote {genlib.rel(out)}: {len(spec['chambers'])} chambers, {len(spec['channels'])} channels, "
          f"{len(spec['ports'])} ports")
    return genlib.finish(out, summary(spec), args, [(PROTOCOL, OUT)])


def summary(spec: dict) -> list[str]:
    ch = {c["id"]: c for c in spec["chambers"]}
    cult = [ch[cid] for _, cid in LANES]
    return [
        "Module 1 summary (cells, not capsules: no capsule sizing):",
        f"  seeding chamber             m1_ipsc_1, {ch['m1_ipsc_1']['volume_ul']:g} uL",
        "  culture chambers            " + ", ".join(f"{c['id']} {c['volume_ul']:g} uL" for c in cult),
        f"  bubble traps                {ch['m1_bt_1']['volume_ul']:g} uL each",
        f"  depth                       {DEPTH_MM:g} mm everywhere; shear-limited culture flow "
        f"{flow_limit_ul_min(cult[0]):g} uL/min per chamber",
        "  variants                    " + ", ".join(spec["run_parameters"]["variant"]["values"]),
    ]


if __name__ == "__main__":
    raise SystemExit(main())

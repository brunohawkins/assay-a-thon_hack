"""Translate Szonja's Module 3 file into the chip-spec contract format.

    python tools/convert_module3.py            # writes chip/module3_v1_translated.json
    python tools/convert_module3.py --check    # also runs tools/check_chip_spec.py on it

Reads chip/modules/module3_step1_3.json (Step 3, substeps 1–3: load, lysis, wash; owner
Szonja) and writes chip/module3_v1_translated.json in contracts/schemas/chip_spec.schema.json
format, so the validator, the checker, the pipeline and the twin can use it. The source
file is never modified: edit it and re-run this script.

What comes from her file (unchanged): every id she names (main_trap, capsule_prefilter,
capsule_in, lysis_buffer, wash_buffer, waste, lysis_waste, to_split_tree and the seven
valves), the trap size and height, its volume and capacity, the 25 um retention gap, the
100 um pre-filter gap, the 140 x 150 um channels, which ports pass capsules, the 4–37 C zone
and the capsule size and count.

What her file does not give, so this script infers it and marks it "placeholder": true:
footprint, layer stack and material, where each chamber and port sits, the channel ids and
routes (one channel per port, straight into the trap edge; the back-flush channel runs round
the bottom to the outlet side), and how each valve is built (normally closed, isolation, on
its port's channel, with its own control port). The questions this raises are printed at
the end and listed in the spec's description.

Standard library only (plus jsonschema through operator/schema_io for writing).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))

import schema_io  # noqa: E402

SOURCE = "chip/modules/module3_step1_3.json"
OUT = "chip/module3_v1_translated.json"

# ---- inferred layout (mm). Flow runs left to right: inlets on the left, the retention gap on
# the trap's right (outlet) side, waste outlets on the right. to_split_tree leaves from the
# trap's top edge on the capsule side, so back-flushed capsules can get out.
FOOT = (64.0, 48.0)
TRAP_C = (32.0, 24.0)
PORT_X_IN, PORT_X_OUT = 5.0, 59.0
BASE_MM, LID_MM = 1.0, 1.0
PORT_Y = {"capsule_in": 34.0, "lysis_buffer": 26.0, "wash_buffer": 18.0, "waste": 32.0, "lysis_waste": 22.0}
SPLIT_PORT = (24.0, 44.0)
PREFILTER = {"center": (11.0, 34.0), "size": (3.0, 2.0)}
BACKFLUSH_Y, BACKFLUSH_X, BACKFLUSH_ENTRY_Y = 5.0, 52.0, 14.0

ROLE = {"capsule_in": "load", "lysis_buffer": "reagent", "wash_buffer": "reagent",
        "waste": "waste", "lysis_waste": "waste", "to_split_tree": "release"}


def r2(x: float) -> float:
    return round(x, 3)


def translate(src: dict) -> tuple[dict, list[str]]:
    geo = src["geometry"]
    trap = geo["main_trap"]
    ret = trap["retention"]
    pre = geo["capsule_filter"]
    chan = geo["channels"]["capsule_channel"]
    rchan = geo["channels"]["reagent_channel"]
    w, d = chan["width_um"] / 1000, chan["depth_um"] / 1000
    rw, rd = rchan["width_um"] / 1000, rchan["depth_um"] / 1000
    h = trap["height_um"] / 1000
    tx, ty = trap["width_mm"], trap["length_mm"]
    left, right = TRAP_C[0] - tx / 2, TRAP_C[0] + tx / 2
    top = TRAP_C[1] + ty / 2
    z_top = BASE_MM + h + LID_MM
    inlets = {p["id"]: p for p in src["inlets"]}
    outlets = {p["id"]: p for p in src["outlets"]}

    def port(pid: str, ptype: str, xy: tuple[float, float], spec: dict) -> dict:
        out = {"id": pid, "type": ptype, "position": [r2(xy[0]), r2(xy[1]), r2(z_top)], "role": ROLE[pid]}
        if "capsule_passable" in spec:
            out["capsule_passable"] = spec["capsule_passable"]
        out["note"] = spec["role"] + (f" Source: {spec['source']}." if spec.get("source") else "") + \
            (f" {spec['safety']}." if spec.get("safety") else "") + " Position inferred."
        out["placeholder"] = True
        return out

    ports = [
        port("capsule_in", "inlet", (PORT_X_IN, PORT_Y["capsule_in"]), inlets["capsule_in"]),
        port("lysis_buffer", "inlet", (PORT_X_IN, PORT_Y["lysis_buffer"]), inlets["lysis_buffer"]),
        port("wash_buffer", "inlet", (PORT_X_IN, PORT_Y["wash_buffer"]), inlets["wash_buffer"]),
        port("waste", "outlet", (PORT_X_OUT, PORT_Y["waste"]), outlets["waste"]),
        port("lysis_waste", "outlet", (PORT_X_OUT, PORT_Y["lysis_waste"]), outlets["lysis_waste"]),
        port("to_split_tree", "outlet", SPLIT_PORT, outlets["to_split_tree"]),
    ]

    chambers = [
        {"id": "main_trap", "type": "exchange", "shape": "box",
         "center": [r2(TRAP_C[0]), r2(TRAP_C[1]), r2(BASE_MM + h / 2)], "size": [tx, ty, h],
         "height_um": trap["height_um"], "volume_ul": trap["chamber_volume_ul"],
         "retention": {"type": "pillars", "opening_um": ret["gap_um"], "source": "fabricated"},
         "note": (f"{trap['shape']}; holds {trap['capacity_capsules']:,} capsules in ~{trap['stack_layers']} layers "
                  f"(packed volume {trap['packed_capsule_volume_ul']} uL, fill {trap['fill_fraction']}). Retention "
                  f"{ret['gap_um']} um ({ret['type']}; allowed {ret['allowed_range_um'][0]}–{ret['allowed_range_um'][1]} um) "
                  f"on the outlet (right) side: {ret['note']}. {trap['flow_distribution']}. {trap['material_note']}. "
                  "Size, height, volume and gap from the source; position inferred; retention type: the source says "
                  "'pillar row or mesh', pillars assumed here."),
         "placeholder": True},
        {"id": pre["id"], "type": "filter", "shape": "box",
         "center": [r2(PREFILTER["center"][0]), r2(PREFILTER["center"][1]), r2(BASE_MM + d / 2)],
         "size": [PREFILTER["size"][0], PREFILTER["size"][1], d],
         "volume_ul": r2(PREFILTER["size"][0] * PREFILTER["size"][1] * d),
         "filter": {"type": "pillars", "gap_um": pre["gap_um"], "purpose": "capsule_aggregates"},
         "note": f"{pre['position']}; {pre['purpose']}. Gap from the source; size and position inferred.",
         "placeholder": True},
    ]

    def ch(cid: str, a: str, b: str, path: list[tuple[float, float]], carries: str, passable: bool, note: str,
           width: float = w, depth: float = d) -> dict:
        return {"id": cid, "from": a, "to": b, "width_mm": width, "depth_mm": depth, "carries": carries,
                "capsule_passable": passable, "path": [[r2(x), r2(y)] for x, y in path],
                "note": note + " Width and depth from the source; id and route inferred.", "placeholder": True}

    py = PORT_Y
    pf_left = PREFILTER["center"][0] - PREFILTER["size"][0] / 2
    pf_right = PREFILTER["center"][0] + PREFILTER["size"][0] / 2
    channels = [
        ch("ch_capsule_in", "capsule_in", pre["id"], [(PORT_X_IN, py["capsule_in"]), (pf_left, py["capsule_in"])],
           "capsules", True, "Capsules from encapsulation to the pre-filter."),
        ch("ch_prefilter_trap", pre["id"], "main_trap", [(pf_right, py["capsule_in"]), (left, py["capsule_in"])],
           "capsules", True, "Filtered capsules into the trap (inlet side)."),
        ch("ch_lysis_buffer", "lysis_buffer", "main_trap", [(PORT_X_IN, py["lysis_buffer"]), (left, py["lysis_buffer"])],
           "liquid", False, "Lysis buffer into the trap (inlet side).", rw, rd),
        ch("ch_wash_buffer", "wash_buffer", "main_trap", [(PORT_X_IN, py["wash_buffer"]), (left, py["wash_buffer"])],
           "liquid", False, "Wash buffer into the trap (inlet side).", rw, rd),
        ch("ch_backflush", "wash_buffer", "main_trap",
           [(PORT_X_IN, py["wash_buffer"]), (PORT_X_IN, BACKFLUSH_Y), (BACKFLUSH_X, BACKFLUSH_Y),
            (BACKFLUSH_X, BACKFLUSH_ENTRY_Y), (right, BACKFLUSH_ENTRY_Y)],
           "liquid", False, "Wash buffer routed to the outlet side of the retention gap (v_backflush), to lift the bed.", rw, rd),
        ch("ch_waste", "main_trap", "waste", [(right, py["waste"]), (PORT_X_OUT, py["waste"])],
           "liquid", False, "Wash waste, through the retention gap.", rw, rd),
        ch("ch_lysis_waste", "main_trap", "lysis_waste", [(right, py["lysis_waste"]), (PORT_X_OUT, py["lysis_waste"])],
           "liquid", False, "Lysis waste, through the retention gap; kept apart from the general waste.", rw, rd),
        ch("ch_to_split_tree", "main_trap", "to_split_tree", [(SPLIT_PORT[0], top), SPLIT_PORT],
           "capsules", True, "Resuspended capsules out to the barcoding split tree (closed during steps 1–3)."),
    ]

    on = {"capsule_in": "ch_capsule_in", "lysis_buffer": "ch_lysis_buffer", "wash_buffer": "ch_wash_buffer",
          "waste": "ch_waste", "lysis_waste": "ch_lysis_waste", "to_split_tree": "ch_to_split_tree"}
    by_id = {c["id"]: c for c in channels}
    valves, ctrl_ports = [], []
    for k, v in enumerate(src["valves"]):
        cid = "ch_backflush" if v["id"] == "v_backflush" else on[v["controls"]]
        (x0, y0), (x1, y1) = by_id[cid]["path"][:2] if cid != "ch_backflush" else by_id[cid]["path"][1:3]
        mid = ((x0 + x1) / 2, (y0 + y1) / 2)
        cp = f"ctrl_{v['id']}"
        ctrl_ports.append({"id": cp, "type": "control", "position": [r2(4.0 + 8.0 * k), 46.0, r2(z_top)], "role": "control",
                           "note": f"Control line for {v['id']}. Inferred: the source lists the valve but not how it is driven.",
                           "placeholder": True})
        valves.append({"id": v["id"], "type": "normally_closed", "kind": "isolation", "channel": cid,
                       "position": [r2(mid[0]), r2(mid[1]), r2(BASE_MM + d)], "control_port": cp,
                       "note": f"Controls {v['controls']} (source). Valve type, position and control port inferred.",
                       "placeholder": True})

    zone = trap["thermal_zone"]
    spec = {
        "id": "module3_v1_translated",
        "name": "Module 3: load, lysis, wash (translated from Szonja's file)",
        "description": (
            f"TRANSLATED by tools/convert_module3.py from {SOURCE} ({src['meta']['name']}; inputs: "
            f"{src['meta']['inputs_from']}). Do not edit by hand: edit the source and re-run the converter. "
            "Ids, trap, gaps, channel size and capsule figures are the source's; layout, footprint, layers, "
            "channel routes and valve construction are inferred and marked placeholder. Open questions for the "
            "owner: (1) the design uses seven valves, and the MVP rule is no valves; (2) the 25 um retention gap "
            "and 140 um channels are far below what the laser cutter makes (Module 1's cut features are 0.8 mm), "
            "so how is it fabricated (bought mesh, embossing)? (3) footprint, material and port positions."),
        "placeholder": True,
        "translated_from": SOURCE,
        "units": "mm",
        "footprint": {"x": FOOT[0], "y": FOOT[1], "placeholder": True,
                      "note": "Inferred: the source gives the trap (28.3 mm square) but no board size."},
        "layers": [
            {"id": "base", "material": "tbc", "thickness_mm": BASE_MM, "placeholder": True,
             "note": f"Material not in the source; {trap['material_note']}."},
            {"id": "fluidic", "material": "tbc", "thickness_mm": h, "placeholder": True,
             "note": f"Trap height {trap['height_um']} um from the source; the 150 um channels need a second, shallower depth."},
            {"id": "lid", "material": "tbc", "thickness_mm": LID_MM, "placeholder": True, "note": "Inferred."},
        ],
        "capsule": {"diameter_um": src["design_inputs"]["capsule_diameter_um"], "count": src["design_inputs"]["capsule_count"]},
        "trap": {"gap_um": ret["gap_um"]},
        "capsule_filter": {"gap_um": pre["gap_um"]},
        "chambers": chambers,
        "channels": channels,
        "ports": ports + ctrl_ports,
        "valves": valves,
        "thermal_zones": [{"id": zone["id"], "chambers": ["main_trap"], "min_temp_c": zone["temp_c_range"][0],
                           "max_temp_c": zone["temp_c_range"][1], "purpose": "wash"}],
    }
    questions = [
        f"Valves: the design uses {len(valves)} valves ({', '.join(v['id'] for v in valves)}); the MVP rule is no valves. "
        "Can the routing be done by capping and opening ports instead (as Module 1 does), or by off-chip pinch valves?",
        f"Retention gap {ret['gap_um']} um (allowed {ret['allowed_range_um'][0]}–{ret['allowed_range_um'][1]} um) is not "
        "laser-cuttable (Module 1's narrowest cut feature is 800 um). Pillar row or bought mesh? If mesh: which, and how is it bonded?",
        f"Channels {chan['width_um']} x {chan['depth_um']} um are also below laser-cut size, and the trap is "
        f"{trap['height_um']} um deep: two depths in one layer. Which process (embossing, milling, printing)?",
        "Footprint, layer material (must tolerate 4 M GITC) and port positions are not in the file: confirm or supply them.",
        "Back-flush route: 'wash_buffer routed to the outlet side of the retention gap'. Is a second channel from the "
        "wash_buffer port (as translated) what you meant, or a separate port?",
        "to_split_tree must sit on the capsule side of the retention gap to let resuspended capsules out; translated on "
        "the trap's top edge near the inlet side. Is that right?",
    ]
    return spec, questions


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="run tools/check_chip_spec.py on the result")
    ap.add_argument("--force", action="store_true", help="overwrite a translation that has since been rescaled by hand")
    args = ap.parse_args(argv)
    src = schema_io.load_json(ROOT / SOURCE)
    spec, questions = translate(src)
    errs = schema_io.schema_errors("chip_spec", spec)
    if errs:
        for e in errs:
            print(f"ERROR {schema_io.path_str(e)}: {schema_io.describe(e)}", file=sys.stderr)
        return 1
    existing = ROOT / OUT
    if existing.is_file() and not args.force:
        old = schema_io.load_json(existing)
        if old.get("footprint") != spec["footprint"]:
            print(f"{OUT} has been rescaled since it was translated (footprint {old.get('footprint')}, this would write "
                  f"{spec['footprint']}): MJ fitted it to the 30 x 20 mm envelope. Not overwritten; --force to replace it.",
                  file=sys.stderr)
            return 1
    schema_io.write_json(ROOT / OUT, spec)
    n_ph = sum(1 for g in ("chambers", "channels", "ports", "valves", "layers") for x in spec[g] if x.get("placeholder"))
    print(f"wrote {OUT} from {SOURCE}: {len(spec['chambers'])} chambers, {len(spec['channels'])} channels, "
          f"{len(spec['ports'])} ports, {len(spec['valves'])} valves; {n_ph} parts marked placeholder")
    print("Questions for the owner:")
    for k, q in enumerate(questions, 1):
        print(f"  {k}. {q}")
    if args.check:
        return subprocess.call([sys.executable, str(ROOT / "tools" / "check_chip_spec.py"), str(ROOT / OUT)])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

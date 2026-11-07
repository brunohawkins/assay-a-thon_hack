"""Build protocols/module1_v1.json.

    python protocols/build_module1.py           # write the protocol
    python protocols/build_module1.py --report  # schedule, port table, shear table

Three wild-type differentiations run in the three culture chambers of chip/module1_v1.json
(n_cx neurons, g_cx glia, a_cx astrocytes), interact once all three are mature, then get a
per-lane barcode and are pooled into one suspension. j_mid is a junction at the centre of
the Y, where the three channels meet and p_mid leaves. It is never seeded or cultured in.

The chip has no valves. Routing is set by what each port is doing: FEED (liquid pushed in),
VENT (open to drain) or CAP (physically closed). Only a capped port is a true block; a
fed port is a fixed-pressure boundary and flow reverses into it if the node it feeds is
higher. So a mode's isolation holds only if its CAP ports are actually capped.

  1. SEED (fallback only). p_in feeds cells, p_mid capped, p_g_out and p_a_out vented.
  2. SEPARATE GROWTH. p_in, p_g_in, p_a_in feed three media, only p_mid vented. The three
     streams meet at j_mid, outside every chamber, and leave through p_mid. Holds for weeks.
  3. INTERACTION. One chamber's inlet feeds, p_mid capped; its medium goes chamber ->
     j_mid -> the other two, which are vented. Run N to G+A, G to N+A, A to N+G. For G and A
     the neuron chamber's only exit is p_in, so p_in is the vent.
  Also used: IDLE (all capped, for attachment) and COLLECT (back-flush from p_g_out and
  p_a_out plus a feed at p_in, vented at p_mid, to sweep cells to the junction).

Seeding: each chamber through its own inlet on its own day (0, 40, 45), so all mature on
day 80. Seeding all three on day 0 would park iPSCs 40-45 days in chambers with no passage
path. Mode 1 is a fallback only: a 12 um cell settles 0.3 mm in about 43 s, against a
14 min residence time in n_cx, so very little reaches the far chambers.

Limits found in review:
  - Drive the chip with SYRINGE PUMPS (flow control), not pressure control. Operating
    pressures are Pa-scale (about 0.04-0.22 mbar): below what commercial controllers hold
    stable (about 7 mbar), and below a 1 mm reservoir head (0.098 mbar) or a port meniscus
    (about 1.4 mbar). With a fixed flow at each inlet every stream can only leave by its own
    route, so the pressure-matching problem disappears.
  - Shear: the 0.01 dyn/cm2 ceiling is applied to the CHAMBERS (met up to about 13.5
    uL/min), where the cells are. The 0.8 x 0.3 mm CHANNELS exceed it above about 0.9
    uL/min; they are transit, not culture.
  - Each lineage has one chamber, so the passages in the published methods are not done.

Flow rates come from chamber geometry as in build_ipsc_to_mgl.py: tau = 6*mu*Q/(w*h^2),
w being the chamber width across the flow. Where a mode splits a stream in two the split is
taken as equal. Reagent names are generic: see CONFIDENTIALITY.md.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "module1_v1.json"
CHIP = Path(__file__).resolve().parents[1] / "chip" / "module1_v1.json"

SOURCE = (
    "Neural lane: Shi Y, Kirwan P, Livesey FJ. Directed differentiation of human pluripotent "
    "stem cells to cerebral cortex neurons and neural networks. Nature Protocols 2012;7:1836, "
    "doi:10.1038/nprot.2012.116 (dual-SMAD cortical induction, 80 days). "
    "Microglia lane: McQuade A et al. Molecular Neurodegeneration 2018;13:67, "
    "doi:10.1186/s13024-018-0297-x (40 days). "
    "Astrocyte lane: Schuurmans IME et al. Glia 2026, doi:10.1002/glia.70202 (35 days). "
    "Per-lane barcoding before pooling: McGinnis CS et al. Nature Methods 2019;16:619, "
    "doi:10.1038/s41592-019-0433-8 (lipid-anchored oligos)."
)

DAY = 86400.0
MU_PA_S = 0.8e-3
SHEAR_CEILING = 0.01
MARGIN = 1.4

PORTS = ["p_in", "p_g_in", "p_a_in", "p_mid", "p_g_out", "p_a_out"]
FEED, CAP, VENT = "FEED", "CAP", "VENT"

# mode -> (label, port states, fraction of the feed that passes through n_cx, g_cx, a_cx)
MODES = {
    "seed": ("1  SEED (shared inlet, fallback)",
             dict(p_in=FEED, p_g_in=CAP, p_a_in=CAP, p_mid=CAP, p_g_out=VENT, p_a_out=VENT),
             dict(n_cx=1.0, g_cx=0.5, a_cx=0.5)),
    "grow": ("2  SEPARATE GROWTH",
             dict(p_in=FEED, p_g_in=FEED, p_a_in=FEED, p_mid=VENT, p_g_out=CAP, p_a_out=CAP),
             dict(n_cx=1.0, g_cx=1.0, a_cx=1.0)),
    "int_n": ("3a INTERACTION N -> G+A",
              dict(p_in=FEED, p_g_in=CAP, p_a_in=CAP, p_mid=CAP, p_g_out=VENT, p_a_out=VENT),
              dict(n_cx=1.0, g_cx=0.5, a_cx=0.5)),
    "int_g": ("3b INTERACTION G -> N+A",
              dict(p_in=VENT, p_g_in=FEED, p_a_in=CAP, p_mid=CAP, p_g_out=CAP, p_a_out=VENT),
              dict(n_cx=0.5, g_cx=1.0, a_cx=0.5)),
    "int_a": ("3c INTERACTION A -> N+G",
              dict(p_in=VENT, p_g_in=CAP, p_a_in=FEED, p_mid=CAP, p_g_out=VENT, p_a_out=CAP),
              dict(n_cx=0.5, g_cx=0.5, a_cx=1.0)),
    "collect": ("   COLLECT (back-flush, sweep)",
                dict(p_in=FEED, p_g_in=CAP, p_a_in=CAP, p_mid=VENT, p_g_out=FEED, p_a_out=FEED),
                dict(n_cx=1.0, g_cx=1.0, a_cx=1.0)),
    "idle": ("   IDLE (attach, between feeds)",
             dict(p_in=CAP, p_g_in=CAP, p_a_in=CAP, p_mid=CAP, p_g_out=CAP, p_a_out=CAP),
             dict(n_cx=0.0, g_cx=0.0, a_cx=0.0)),
}

TAG = {
    "seed": "Mode 1 (seed, shared inlet, fallback)",
    "grow": "Mode 2 (separate growth)",
    "int_n": "Mode 3, neurons to glia and astrocytes",
    "int_g": "Mode 3, glia to neurons and astrocytes",
    "int_a": "Mode 3, astrocytes to neurons and glia",
    "collect": "Collect (back-flush)",
    "idle": "Idle (all ports capped)",
}

# lane -> chamber, inlet port, seed day, switch day, media before and after the switch
LANES = {
    "neural": dict(chamber="n_cx", inlet="p_in", seed=0, switch=12,
                   media=("neural_induction_medium", "neural_maturation_medium")),
    "glia": dict(chamber="g_cx", inlet="p_g_in", seed=40, switch=52,
                 media=("hematopoietic_medium", "microglia_maturation_medium")),
    "astrocyte": dict(chamber="a_cx", inlet="p_a_in", seed=45, switch=None,
                      media=("astro_medium", "astro_medium")),
}
MATURE_DAY = 80      # all three lanes ready together
INTERACT_DAYS = 7    # interaction window before harvest
INTERACTIONS = [     # (mode, source lane, run day)
    ("int_n", "neural", 81),
    ("int_g", "glia", 83),
    ("int_a", "astrocyte", 85),
]
EXITS = {"int_n": "p_g_out and p_a_out", "int_g": "p_in and p_a_out", "int_a": "p_in and p_g_out"}
BARCODES = {"n_cx": "barcode_1", "g_cx": "barcode_2", "a_cx": "barcode_3"}
LANE_OF = {v["chamber"]: k for k, v in LANES.items()}


def max_flow(width_mm: float, height_mm: float, ceiling: float = SHEAR_CEILING) -> float:
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (ceiling / 10.0) * w * h * h / (6.0 * MU_PA_S) * 60.0 * 1e9


def shear(flow_ul_min: float, width_mm: float, height_mm: float) -> float:
    q = flow_ul_min * 1e-9 / 60.0
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (6.0 * MU_PA_S * q / (w * h * h)) * 10.0


CH = {c["id"]: c for c in json.loads(CHIP.read_text())["chambers"]}
# Highest flow each chamber can carry under the ceiling, rounded down with a little headroom.
CULTURE = ["n_cx", "g_cx", "a_cx"]
CAP_FLOW = {cid: max(1.0, float(int(max_flow(CH[cid]["size"][1], CH[cid]["size"][2]))) - 2.0)
            for cid in CULTURE}


def feed_flow(mode: str) -> float:
    """Largest feed (uL/min) for a mode that keeps every chamber it crosses under the ceiling."""
    share = MODES[mode][2]
    limits = [CAP_FLOW[c] / f for c, f in share.items() if f > 0]
    return float(math.floor(min(limits))) if limits else 0.0


def events() -> list[tuple[float, dict]]:
    """Every scheduled operation on a real calendar, as (day, partial step)."""
    ev: list[tuple[float, dict]] = []

    def add(day, operation, target, note, mode, reagent=None, fraction=None, washes=1,
            temp_c=None, duration_s=None, until=None):
        ev.append((day, dict(operation=operation, target=target, note=f"{TAG[mode]}: {note}", mode=mode,
                             reagent=reagent, fraction=fraction, washes=washes, temp_c=temp_c,
                             duration_s=duration_s, until=until)))

    for lane, L in LANES.items():
        c, inlet, seed, switch = L["chamber"], L["inlet"], L["seed"], L["switch"]
        med1, med2 = L["media"]
        add(seed - 0.5, "exchange", c,
            f"Coat {c} through {inlet} alone with coating solution; the other inlets stay capped so "
            f"the {lane} chamber is the only one wetted.", "grow", "coating_solution", 1.0)
        add(seed, "load", c,
            f"Seed iPSCs into the {lane} chamber through {inlet} on its own day, with p_mid vented. "
            f"The other two chambers are untouched, which is why the shared-inlet seed (mode 1) is not "
            f"used for a staggered schedule.", "grow")
        add(seed + 0.02, "hold_temp", c,
            f"All ports capped for 24 hours while the {lane} cells attach.", "idle",
            temp_c=37, until=seed + 1 - 0.005)
        day, switched = seed + 1, False
        while day < MATURE_DAY:
            use_med2 = switch is not None and day >= switch
            medium = med2 if use_med2 else med1
            n = day - seed
            if use_med2 and not switched and med1 != med2:
                add(day, "exchange", c,
                    f"Day {n:g} of the {lane} lane: full change into {medium} through {inlet}, p_mid vented. "
                    f"The stream meets the others only at the junction j_mid, outside every chamber.",
                    "grow", medium, 1.0)
                switched = True
            else:
                add(day, "exchange", c,
                    f"Day {n:g} of the {lane} lane: half change of {medium} through {inlet}, p_mid vented. "
                    f"The three media converge at the junction j_mid, outside every chamber, and leave through p_mid.",
                    "grow", medium, 0.5)
            day += 2

    # --- interaction window: one direction at a time, each preceded by a separate-growth reset ---
    for mode, lane, day in INTERACTIONS:
        L = LANES[lane]
        c, inlet = L["chamber"], L["inlet"]
        medium = L["media"][1]
        others = [x["chamber"] for k, x in LANES.items() if k != lane]
        for k, other in LANES.items():
            add(day - 0.1, "exchange", other["chamber"],
                f"Reset before the {lane}-source direction: half change of {other['media'][1]} so every chamber "
                f"starts from its own medium.", "grow", other["media"][1], 0.5)
        add(day, "exchange", c,
            f"Push {medium} through {c} from {inlet} with p_mid capped. The conditioned medium goes "
            f"{c} to the junction j_mid, then splits to {others[0]} and {others[1]} and leaves by "
            f"{EXITS[mode]} (vented). Twice the chamber volume goes through, so the other two see this "
            f"lane's secretome.",
            mode, medium, 1.0, washes=2)
        add(day + 0.5, "hold_temp", c,
            f"All ports capped so the {lane} secretome acts on the other two chambers; imaging and "
            f"supernatant sampling measure the response.", "idle", temp_c=37, until=day + 1.9)

    # --- per-lane barcode, then pool ---------------------------------------------------
    h = MATURE_DAY + INTERACT_DAYS
    add(h, "hold_temp", "n_cx",
        "Cool the whole chip to 4 C. Cold handling limits activation and stops the lipid-anchored "
        "barcodes swapping between cells.", "idle", temp_c=4, duration_s=1200)
    for chamber, tag in BARCODES.items():
        lane = LANE_OF[chamber]
        add(h + 0.01, "exchange", chamber,
            f"Feed {tag} into {chamber} through {LANES[lane]['inlet']}, p_mid vented. A lipid-anchored oligo "
            f"inserts into the membrane with no antibody and no spin, so each chamber carries its own label "
            f"before anything is mixed.", "grow", tag, 0.5, duration_s=600)
    for chamber in BARCODES:
        add(h + 0.02, "exchange", chamber,
            f"Wash unbound barcode out of {chamber} through {LANES[LANE_OF[chamber]]['inlet']}. Carryover causes "
            f"background, so this decides whether the labels can be told apart.",
            "grow", "wash_buffer", 0.5, washes=2)
    for chamber in BARCODES:
        add(h + 0.03, "exchange", chamber,
            f"Lift the cells in {chamber} with a gentle, enzyme-free reagent at 4 C.",
            "grow", "dissociation_reagent", 0.5, duration_s=900)
    add(h + 0.05, "exchange", "n_cx",
        "Quench with a carrier-protein buffer through p_in, so released cells do not stick to the walls; "
        "it sweeps them on to the junction.",
        "grow", "quench_buffer", 0.5)
    for chamber in ("g_cx", "a_cx"):
        port = LANES[LANE_OF[chamber]]["inlet"].replace("_in", "_out")
        add(h + 0.055, "exchange", chamber,
            f"Back-flush {chamber} from {port} with quench buffer, which lifts the cells off its mesh and "
            f"sweeps them into the junction j_mid.",
            "collect", "quench_buffer", 0.5)
    add(h + 0.06, "pool", "j_mid",
        "Pool all three barcoded lanes in the junction j_mid. They are safe to mix now because each carries its own barcode: "
        "lane identity survives in the sequencing data, and so does any cell that crossed between chambers.",
        "collect")
    add(h + 0.08, "release", "j_mid",
        "Deliver one counted, barcoded suspension to the outlet for encapsulation.", "collect",
        reagent="release_buffer")
    return sorted(ev, key=lambda e: e[0])


def build() -> dict:
    steps: list[dict] = []
    t = 0.0
    evs = events()
    start = min(d for d, _ in evs)

    def emit(operation, target, note, duration_s, reagent=None, volume_ul=None,
             temp_c=None, washes=None, flow_ul_min=None):
        nonlocal t
        steps.append({"step": len(steps) + 1, "operation": operation, "target": target,
                      "reagent": reagent, "volume_ul": volume_ul, "duration_s": round(duration_s),
                      "temp_c": temp_c, "washes": washes, "flow_ul_min": flow_ul_min, "note": note})
        t += round(duration_s)

    for day, e in evs:
        gap = (day - start) * DAY - t
        if gap > 1 and e["until"] is None:
            emit("hold_temp", e["target"],
                 "Idle (all ports capped): hold at 37 C until the next scheduled operation. Each lane is "
                 "incubating on its own; the chip acts on one chamber at a time.", gap, temp_c=37)
        op, target = e["operation"], e["target"]
        if op == "exchange":
            volume = round(CH[target]["volume_ul"] * (e["fraction"] or 1.0), 1)
            flow = feed_flow(e["mode"])
            minimum = e["washes"] * volume / flow * 60.0
            emit("exchange", target, e["note"], e["duration_s"] or minimum * MARGIN,
                 reagent=e["reagent"], volume_ul=volume, washes=e["washes"], flow_ul_min=flow)
        elif op == "hold_temp":
            duration = e["duration_s"]
            if e["until"] is not None:
                duration = max(1.0, (e["until"] - start) * DAY - t)
            emit("hold_temp", target, e["note"], duration, temp_c=e["temp_c"])
        elif op == "load":
            emit("load", target, e["note"], 900, volume_ul=CH[target]["volume_ul"])
        elif op == "pool":
            emit("pool", target, e["note"], 1800)
        elif op == "release":
            emit("release", target, e["note"], 900, reagent=e["reagent"])

    return {
        "id": "module1_v1",
        "name": "Three-lineage differentiation, interaction and barcoded collection (valve-free chip)",
        "source": SOURCE,
        "placeholder": False,
        "description": (
            "Three wild-type iPSC differentiations (neurons, microglia, astrocytes) run in the three "
            "chambers of one valve-free chip joined at a central junction. Routing is set by which ports "
            "feed, vent or are capped; the chip is meant to be driven by syringe pumps. Seeding is per "
            "chamber through its own inlet on its own day (0, 40 and 45) so all three mature on day 80; weeks of growth use "
            "separate-growth mode, where three media flow inward to one drain; an interaction window then "
            "sends each chamber's conditioned medium across to the other two in turn; each lane is then "
            "barcoded and the three are pooled into one suspension for encapsulation. Runs about "
            f"{MATURE_DAY + INTERACT_DAYS} days. Reagent names are generic; see the source citation. "
            "Flow rates are computed from chamber geometry to keep wall shear at or below 0.01 dyn/cm2."
        ),
        "steps": steps,
    }


def report() -> None:
    print("Lane schedule (run day):")
    for lane, L in LANES.items():
        print(f"  {lane:<10} {L['chamber']}  seed {L['seed']:>3}  mature {MATURE_DAY}  "
              f"= {MATURE_DAY - L['seed']} days of differentiation")
    print(f"  interaction window day {MATURE_DAY} to {MATURE_DAY + INTERACT_DAYS}: "
          + ", ".join(f"{m.replace('int_', '').upper()}-source day {d}" for m, _, d in INTERACTIONS)
          + f"; barcode, pool and release day {MATURE_DAY + INTERACT_DAYS}\n")

    print("Port state by mode  (FEED = liquid pushed in, VENT = open to drain, CAP = physically closed)")
    print(f"{'mode':<34}" + "".join(f"{p:>9}" for p in PORTS))
    for key, (label, states, _) in MODES.items():
        print(f"{label:<34}" + "".join(f"{states[p]:>9}" for p in PORTS))
    print("  Only CAP is a true block. Mode 2: an inlet not due a feed is capped; steps feed one lane at a time.\n")

    print(f"{'mode':<34}{'feed':>8}  " + "".join(f"{c:>20}" for c in CULTURE))
    for key, (label, _, share) in MODES.items():
        q = feed_flow(key)
        cells = ""
        for c in CULTURE:
            w, h = CH[c]["size"][1], CH[c]["size"][2]
            cells += f"{q * share[c]:.0f} uL/min {shear(q * share[c], w, h):.4f}".rjust(20)
        print(f"{label:<34}{q:>8.0f}  {cells}" if q > 0 else f"{label:<34}{'-':>8}  no flow")
    print(f"  shear in dyn/cm2, ceiling {SHEAR_CEILING}; chamber ceiling flow: "
          + ", ".join(f"{c} {max_flow(CH[c]['size'][1], CH[c]['size'][2]):.1f} uL/min" for c in CULTURE))
    ch = json.loads(CHIP.read_text())["channels"][0]
    print(f"  channels ({ch['width_mm']} x {ch['depth_mm']} mm) pass the ceiling above "
          f"{max_flow(ch['width_mm'], ch['depth_mm']):.1f} uL/min: transit, not culture")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    protocol = build()
    OUT.write_text(json.dumps(protocol, indent=2) + "\n")
    total = sum(s["duration_s"] for s in protocol["steps"])
    print(f"\nwrote {OUT.name}: {len(protocol['steps'])} steps, {total / DAY:.1f} days")

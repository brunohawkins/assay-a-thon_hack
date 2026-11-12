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
  - Shear: the 0.01 dyn/cm2 ceiling is applied to the CHAMBERS (derived from chamber
    geometry, about 2.3 uL/min at the 30 x 20 mm scale and the contract viscosity), where
    the cells are. The 0.2 x 0.3 mm CHANNELS exceed it above about 0.18 uL/min; they are
    transit, not culture.
  - Each lineage has one chamber, so the passages in the published methods are not done.

Flow rates come from chamber geometry as in build_ipsc_to_mgl.py: tau = 6*mu*Q/(w*h^2),
w being the chamber width across the flow. Where a mode splits a stream in two the split is
taken as equal. Reagent names are generic: see CONFIDENTIALITY.md.

mu is read from contracts/parameters.json, NOT hard-coded, and it is the 37 C culture-medium
value fluid.viscosity_37c_pa_s (0.0008 Pa s), which the contract says to use for "EVERY
CULTURE AND PROTOCOL FLOW OR SHEAR CALCULATION". The other key, fluid.viscosity_pa_s
(0.001), is water at 20 C - the dye-demo value - and this file was pointed at it for a
while: shear goes as mu, so every flow derived here as "85% of the shear-ceiling flow" came
out 20% lower than the chips can actually run. The original hard-coded 0.8e-3 was the right
physics with the wrong provenance; reading the 37 C key is the right physics with the right
provenance.

HOW MUCH LIQUID AN EXCHANGE MOVES. Every exchange step declares what it is FOR, because
that, not habit, sets the volume (see exchange_cv). The declaration is written into the step
as the DATA field `exchange_kind` ("sweep", "partial" or "fill"), not only into the note: it
used to live in English prose alone, so a checker could not tell a deliberate half medium
change from a wash that was too small to work, and the two rules contradicted each other.
The note still says the same thing in words, for the twin's narration.
  sweep   - a wash, rinse, reset or clear-out. A chamber is well mixed, so what is left
            after V chamber volumes is exp(-V): reaching exchange.target_fraction takes
            SWEEP_CV chamber volumes in total, split across the washes. HALF a chamber
            volume cannot clear a chamber however many times it is repeated, which is what
            96 steps of this protocol used to ask for.
  partial - a routine culture medium half-change. Deliberately does NOT clear the chamber:
            the conditioned medium left behind is the point. Stays at PARTIAL_CV.
  fill    - put a reagent in. One chamber volume displaces the bulk of what was there.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "module1_v1.json"
CHIP = Path(__file__).resolve().parents[1] / "chip" / "module1_v1.json"
CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "parameters.json"

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


def parameter(name: str) -> float:
    """One value from contracts/parameters.json. No fallback: a missing contract is a bug,
    and a literal copied here is how mu lost its provenance last time."""
    return float(json.loads(CONTRACT.read_text())["parameters"][name]["value"])


MU_PA_S = parameter("fluid.viscosity_37c_pa_s")
SHEAR_CEILING = parameter("shear.culture_ceiling_dyn_cm2")
EXCHANGE_TARGET = parameter("exchange.target_fraction")
SWEEP_CV = -math.log(1.0 - EXCHANGE_TARGET)   # chamber volumes a sweep needs to hit the goal
PARTIAL_CV = 0.5                              # a culture half-change, on purpose
MARGIN = 1.4

# Appended to every exchange note, so the file always says what fraction of a chamber volume
# the step moves and whether it is trying to clear the chamber.
CV_NOTE = {
    "sweep": ("SWEEP: {total:.1f} chamber volumes in total, which is what the {goal:.0%} exchange "
              "goal needs in a well-mixed chamber (what is left after V chamber volumes is exp(-V), "
              "so half a volume cannot clear it however often it is repeated)."),
    "partial": ("PARTIAL change: {total:.1f} of a chamber volume. This is NOT trying to clear the "
                "chamber - the conditioned medium left behind is wanted, as in any bench half-change."),
    "fill": ("FILL: {total:.1f} chamber volume(s) to put the reagent in, not a sweep to get the old "
             "liquid out; about {left:.0%} of what was there stays behind."),
}


def exchange_cv(kind: str, washes: int) -> float:
    """Chamber volumes PER WASH for an exchange of this kind. See the module docstring."""
    if kind == "sweep":
        return SWEEP_CV / washes
    if kind == "partial":
        return PARTIAL_CV
    return 1.0

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
# Highest flow each chamber can carry under the ceiling: 15% headroom, rounded down to 0.1 uL/min.
# No floor: if a chamber is so small the result is 0, that is a design problem to surface, not hide.
CULTURE = ["n_cx", "g_cx", "a_cx"]
HEADROOM = 0.85
CAP_FLOW = {cid: math.floor(max_flow(CH[cid]["size"][1], CH[cid]["size"][2]) * HEADROOM * 10) / 10
            for cid in CULTURE}


def feed_flow(mode: str) -> float:
    """Largest feed (uL/min) for a mode that keeps every chamber it crosses under the ceiling."""
    share = MODES[mode][2]
    limits = [CAP_FLOW[c] / f for c, f in share.items() if f > 0]
    if not limits:
        return 0.0
    flow = math.floor(min(limits) * 10) / 10
    if flow <= 0:
        raise SystemExit("chamber geometry leaves no flow under the shear ceiling")
    return flow


def events() -> list[tuple[float, dict]]:
    """Every scheduled operation on a real calendar, as (day, partial step)."""
    ev: list[tuple[float, dict]] = []

    def add(day, operation, target, note, mode, reagent=None, kind=None, washes=1,
            temp_c=None, duration_s=None, until=None):
        ev.append((day, dict(operation=operation, target=target, note=f"{TAG[mode]}: {note}", mode=mode,
                             reagent=reagent, kind=kind, washes=washes, temp_c=temp_c,
                             duration_s=duration_s, until=until)))

    for lane, L in LANES.items():
        c, inlet, seed, switch = L["chamber"], L["inlet"], L["seed"], L["switch"]
        med1, med2 = L["media"]
        add(seed - 0.5, "exchange", c,
            f"Coat {c} through {inlet} alone with coating solution; the other inlets stay capped so "
            f"the {lane} chamber is the only one wetted.", "grow", "coating_solution", "fill")
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
                    f"Day {n:g} of the {lane} lane: change into {medium} through {inlet}, p_mid vented. "
                    f"This one IS meant to clear the chamber - the previous medium drives a different "
                    f"stage and carrying it over would blur the switch - so it sweeps rather than half-changes. "
                    f"The stream meets the others only at the junction j_mid, outside every chamber.",
                    "grow", medium, "sweep")
                switched = True
            else:
                add(day, "exchange", c,
                    f"Day {n:g} of the {lane} lane: half change of {medium} through {inlet}, p_mid vented. "
                    f"The three media converge at the junction j_mid, outside every chamber, and leave through p_mid.",
                    "grow", medium, "partial")
            day += 2

    # --- interaction window: one direction at a time, each preceded by a separate-growth reset ---
    for mode, lane, day in INTERACTIONS:
        L = LANES[lane]
        c, inlet = L["chamber"], L["inlet"]
        medium = L["media"][1]
        others = [x["chamber"] for k, x in LANES.items() if k != lane]
        for k, other in LANES.items():
            add(day - 0.1, "exchange", other["chamber"],
                f"Reset before the {lane}-source direction: clear {other['chamber']} back to {other['media'][1]} "
                f"so every chamber starts from its own medium. The previous direction left another lane's "
                f"conditioned medium in here, and carrying it over would confound the comparison, so this one "
                f"has to actually clear - a half change would leave most of it.",
                "grow", other["media"][1], "sweep")
        add(day, "exchange", c,
            f"Push {medium} through {c} from {inlet} with p_mid capped. The conditioned medium goes "
            f"{c} to the junction j_mid, then splits to {others[0]} and {others[1]} and leaves by "
            f"{EXITS[mode]} (vented), so the other two see this lane's secretome. Deliberately a delivery "
            f"and not a wash: what matters is what arrives downstream, not clearing {c}.",
            mode, medium, "fill", washes=2)
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
            f"before anything is mixed. The leftover medium is the same share in all three chambers, so it "
            f"does not bias which lane is which.", "grow", tag, "fill", duration_s=600)
    for chamber in BARCODES:
        add(h + 0.02, "exchange", chamber,
            f"Wash unbound barcode out of {chamber} through {LANES[LANE_OF[chamber]]['inlet']}. Carryover causes "
            f"background, so this decides whether the labels can be told apart, and it is the one step here that "
            f"MUST reach its exchange goal.",
            "grow", "wash_buffer", "sweep", washes=2)
    for chamber in BARCODES:
        add(h + 0.03, "exchange", chamber,
            f"Lift the cells in {chamber} with a gentle, enzyme-free reagent at 4 C. The wash before this left "
            f"wash buffer, not medium, in the chamber, so one volume of reagent is enough to act.",
            "grow", "dissociation_reagent", "fill", duration_s=900)
    add(h + 0.05, "exchange", "n_cx",
        "Quench with a carrier-protein buffer through p_in, so released cells do not stick to the walls, and "
        "sweep them on to the junction. Recovery is the whole point here, so this is sized to clear the chamber, "
        "not to top it up.",
        "grow", "quench_buffer", "sweep")
    for chamber in ("g_cx", "a_cx"):
        port = LANES[LANE_OF[chamber]]["inlet"].replace("_in", "_out")
        add(h + 0.055, "exchange", chamber,
            f"Back-flush {chamber} from {port} with quench buffer, which lifts the cells off its weir and "
            f"sweeps them into the junction j_mid. Sized to clear the chamber: cells left behind are cells lost.",
            "collect", "quench_buffer", "sweep")
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
             temp_c=None, washes=None, flow_ul_min=None, exchange_kind=None):
        nonlocal t
        step = {"step": len(steps) + 1, "operation": operation, "target": target,
                "reagent": reagent, "volume_ul": volume_ul, "duration_s": round(duration_s),
                "temp_c": temp_c, "washes": washes, "flow_ul_min": flow_ul_min, "note": note}
        if exchange_kind is not None:
            step["exchange_kind"] = exchange_kind
        steps.append(step)
        t += round(duration_s)

    for day, e in evs:
        gap = (day - start) * DAY - t
        if gap > 1 and e["until"] is None:
            emit("hold_temp", e["target"],
                 "Idle (all ports capped): hold at 37 C until the next scheduled operation. Each lane is "
                 "incubating on its own; the chip acts on one chamber at a time.", gap, temp_c=37)
        op, target = e["operation"], e["target"]
        if op == "exchange":
            kind, washes = e["kind"] or "fill", e["washes"]
            per_wash = exchange_cv(kind, washes)
            # A SWEEP is rounded UP, never down. Rounding 4.6 chamber volumes down to one
            # decimal place puts the step a hair BELOW the exchange goal it was derived
            # from, so the file would claim a goal its own numbers miss.
            raw = CH[target]["volume_ul"] * per_wash
            volume = math.ceil(raw * 10) / 10 if kind == "sweep" else round(raw, 1)
            total_cv = per_wash * washes
            note = f"{e['note']} " + CV_NOTE[kind].format(
                total=total_cv, goal=EXCHANGE_TARGET, left=math.exp(-total_cv))
            flow = feed_flow(e["mode"])
            minimum = washes * volume / flow * 60.0
            emit("exchange", target, note, e["duration_s"] or minimum * MARGIN,
                 reagent=e["reagent"], volume_ul=volume, washes=washes, flow_ul_min=flow,
                 exchange_kind=kind)
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
            f"Flow rates are computed from chamber geometry to keep wall shear at or below "
            f"{SHEAR_CEILING:g} dyn/cm2 (tau = 6*mu*Q/(w*h^2), mu = {MU_PA_S:g} Pa s read from "
            f"contracts/parameters.json, not hard-coded). Every exchange says what it is for in its "
            f"`exchange_kind` field as well as in its note, so a checker can read it: a SWEEP "
            f"(wash, rinse, reset) moves {SWEEP_CV:.1f} chamber volumes, which is what the "
            f"{EXCHANGE_TARGET:.0%} exchange goal needs in a well-mixed chamber; a PARTIAL culture "
            f"half-change moves {PARTIAL_CV:g} and deliberately leaves conditioned medium behind, so the "
            f"one-chamber-volume floor does not apply to it; a FILL "
            f"moves one volume to put a reagent in. The 96 steps that used to move half a volume whatever "
            f"their purpose are now sized by purpose."
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
            cells += f"{q * share[c]:.1f} uL/min {shear(q * share[c], w, h):.4f}".rjust(20)
        print(f"{label:<34}{q:>8.1f}  {cells}" if q > 0 else f"{label:<34}{'-':>8}  no flow")
    print(f"  shear in dyn/cm2, ceiling {SHEAR_CEILING} at mu = {MU_PA_S:g} Pa s "
          f"(contracts/parameters.json fluid.viscosity_37c_pa_s); chamber ceiling flow: "
          + ", ".join(f"{c} {max_flow(CH[c]['size'][1], CH[c]['size'][2]):.2f} uL/min" for c in CULTURE))
    ch = json.loads(CHIP.read_text())["channels"][0]
    print(f"  channels ({ch['width_mm']} x {ch['depth_mm']} mm) pass the ceiling above "
          f"{max_flow(ch['width_mm'], ch['depth_mm']):.2f} uL/min: transit, not culture")

    print(f"\nExchange sizing (chamber volumes), goal {EXCHANGE_TARGET:.0%}:")
    for kind, washes in (("sweep", 1), ("sweep", 2), ("partial", 1), ("fill", 1), ("fill", 2)):
        per = exchange_cv(kind, washes)
        print(f"  {kind:<8}{washes} wash(es): {per:.2f} CV each, {per * washes:.2f} CV total, "
              f"leaves {math.exp(-per * washes):.1%} of the old liquid")
    proto = build()
    kinds = {"sweep": 0, "partial": 0, "fill": 0}
    for s in proto["steps"]:
        if s["operation"] == "exchange":
            kinds[s["exchange_kind"]] += 1
    print("  steps by purpose (read from the exchange_kind field, not from the prose): "
          + ", ".join(f"{k} {v}" for k, v in kinds.items()))


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    protocol = build()
    OUT.write_text(json.dumps(protocol, indent=2) + "\n")
    total = sum(s["duration_s"] for s in protocol["steps"])
    print(f"\nwrote {OUT.name}: {len(protocol['steps'])} steps, {total / DAY:.1f} days")

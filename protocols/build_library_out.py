"""Build protocols/library_out_v1.json.

    python protocols/build_library_out.py           # write the protocol
    python protocols/build_library_out.py --report  # fill order, thermal profile, dead volumes, shear

Steps 15 to 17 of the library prep, across TWO chips. The input is the pooled, hairpin-ligated
capsule suspension handed over from step 14. The output is a sequencing-ready library.

  15. SUBLIBRARY SPLIT + INDEX PCR, chip/module4_v1.json. Four chambers sub_1..sub_4 are filled
      one at a time by an off-chip 4-position rotary valve, in an INTERLEAVED PALINDROMIC order
      (A B C D D C B A, repeated; 8 to 16 pulses, 16 used here). That is what keeps the four
      sublibraries even. A symmetric manifold would give a CV of 30-100% (sedimentation plus the
      Zweifach-Fung effect at each bifurcation); interleaved time-division gives 3-8%. In the
      palindrome every chamber is served at the same mean position (4.5 of 8), so a steady drift
      in the reservoir (capsules settling, the suspension thinning) cancels between chambers.
      Then one index primer per chamber, the PCR mix, and thermocycling: about 98 C denature,
      60 C anneal, 72 C extend, 8-14 cycles (12 here), at 2-4 C/s (3 here). Each PCR cycle is
      three hold_temp steps so the run file shows the real thermal profile. The four chambers
      share one heater, so each hold_temp names sub_1 and its note says the whole chip follows.
  16. RELEASE + CLEANUP, chip/module5_v1.json. Collagenase digest at 37 C in react_cx, NOT
      proteinase K, which leaves these capsules intact. Then magnetic bead capture in bead_cx,
      buffer exchange and elution. Module 5 holds 8.1 uL in react_cx while the four sublibraries
      add up to far more, so it runs FOUR PASSES, one per sublibrary. The indices differ, so the
      four eluates can be mixed for sequencing in the tube at the end.
  17. LIBRARY OUT through out_lib, then off-chip QC.

Operations. The contract allows only load, exchange, hold_temp, split, pool and release, so:
a fill pulse, a bead load and a sublibrary transfer are `load`; adding primer, PCR mix, dilution,
wash and elution buffer are `exchange` (flow-through, nothing is retained, there is no mesh);
the collagenase digest and the library hand-offs are `release`; the PCR and the digest are
`hold_temp`. There is no `split` step: the split is done by the valve, not by the chip, and the
planner's split reads channel topology that module 4 deliberately does not have.

Flow and shear. Same maths as build_module1.py: tau = 6*mu*Q/(w*h^2), w the width across the
flow, tau in dyn/cm2 (1 Pa = 10 dyn/cm2), and duration = washes*volume/flow*60 times a 1.4
margin. THE 0.01 dyn/cm2 CEILING DOES NOT APPLY HERE. That ceiling comes from cultured cells:
it is the shear below which adherent, mechanosensitive cells stay unactivated over weeks. These
chambers hold crosslinked hydrogel capsules, which do not adhere, have no activation state to
protect and are in the chip for minutes, not weeks. What matters is that they stay suspended
and intact, so the design limit is set on the narrowest feature of each route at a handling
shear of 1 dyn/cm2 (an ASSUMPTION, to be replaced by a measured capsule deformation limit). The
flows actually used are lower still: fills at 10 uL/min, and capture and elution at 1-3 uL/min
because bead binding kinetics, not shear, set the pace.

THE DILUTION PROBLEM. Once the shell dissolves it contributes roughly 10,000 times more polymer
mass than there is DNA. SPRI binding depends on the crowding reagent holding DNA on the bead
surface; that much dissolved polymer competes with it and binding fails. So the digest must be
diluted before it meets the beads, and it is an explicit exchange step (dilution_buffer pushed
through react_cx into bead_cx). Honest limit: there is no mixer on chip, so dilution is by
dispersion in react_cx as the buffer sweeps the digest out, and the leading plug is the least
diluted. How far the dilution has to go for binding to work is not measured; DILUTION_UL is a
placeholder to be fitted on real eluates.

NO ETHANOL WASH ON CHIP. SPRI normally washes with ethanol and dries the pellet. A drying
air-liquid meniscus pulls on a bead about 100,000 times harder than the magnet, so it smears
and destroys the pellet. On chip there is capture, buffer exchange (aqueous, never dried) and
elution only. Ratio-based size selection to remove primer dimers is an OFF-CHIP TUBE step,
recorded as a release step that hands off at out_lib, not as an on-chip step. Collagenase needs
no chelator: carried-over EDTA would inhibit it, so the buffers on this path are chelator-free.

CONTESTED ORDER. The team's protocol sheet runs index PCR (15) before release (16), and this
file is built in that order. Published practice for these capsules is release FIRST, then PCR.
The reason is that fragments under about 300 bp leak out of the shell during thermocycling and
can prime neighbouring capsules, creating chimeric molecules that carry the wrong barcodes. If
the DNA is already free, there are no capsules to cross-prime. Module 5 supports the alternative:
with the magnet off, react_cx drains through bead_cx to out_waste, so it can run before module 4
as well as after it; only the order of passes changes, not the geometry. What this file does not
settle is where the digest goes between the two chips in that order (the collection route is
not specified) or how cleanup would be done after PCR, which would then be a tube step. MJ and
the protocol owners need to decide before this is built as the final run.

Port states, module 5 (no valves; a capped port is the only true block): digest hold, in_lib and
in_prot and in_bead and both outlets capped. Dilution and capture, in_prot feeds, out_waste
vented, in_bead and out_lib capped, magnet ON. Buffer exchange, same. Elution, in_bead feeds,
out_lib vented, out_waste capped, magnet ON until the eluate has left. Reagent names are generic
and nothing here is a concentration or a recipe: see CONFIDENTIALITY.md. Setpoints, cycle count
and volumes are round placeholders to be fitted.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT_M4 = HERE / "module4_v1.json"
OUT_M5 = HERE / "module5_v1.json"
CHIPS = HERE.parent / "chip"
M4 = json.loads((CHIPS / "module4_v1.json").read_text())
M5 = json.loads((CHIPS / "module5_v1.json").read_text())

SOURCE = (
    "Capsule workflow: Baronas et al., Science 391, 1138 (2026), doi:10.1126/science.ady7227 "
    "(CapSeq). Split-pool barcoding with a final indexing PCR: Rosenberg AB et al., Science "
    "2018;360:176, doi:10.1126/science.aam8999 (SPLiT-seq). Chip geometry, the interleaved "
    "fill order, the dead-volume accounting and the flows are ours."
)

MU_PA_S = 0.8e-3
HANDLING_SHEAR = 1.0   # dyn/cm2, assumption; the 0.01 culture ceiling does not apply, see docstring
MARGIN = 1.4

# --- split and PCR (module 4) ---
PALINDROME = ["sub_1", "sub_2", "sub_3", "sub_4", "sub_4", "sub_3", "sub_2", "sub_1"]
PALINDROMES = 2                # 8 pulses per palindrome; 8-16 pulses allowed
FILL_ORDER = PALINDROME * PALINDROMES
SUBS = ["sub_1", "sub_2", "sub_3", "sub_4"]
VALVE_POS = {s: i + 1 for i, s in enumerate(SUBS)}
LETTER = {s: "ABCD"[i] for i, s in enumerate(SUBS)}
PULSE_UL = 0.6                 # per pulse; four pulses per chamber
SUSP_UL = PULSE_UL * FILL_ORDER.count("sub_1")
PRIMER_UL = 0.6
FILL_FLOW = 10.0               # uL/min
SWITCH_S = 5                   # valve switch and settle between pulses, assumption
CYCLES = 12                    # 8-14
RAMP_C_S = 3.0                 # 2-4 C/s
INITIAL = (98, 60)             # setpoint C, dwell s
CYCLE = [("denature", 98, 10), ("anneal", 60, 30), ("extend", 72, 30)]
FINAL = (72, 120)
COLD_C = 4
COLD_S = 300
AMBIENT_C = 25

# --- release and cleanup (module 5) ---
PASSES = SUBS
DIGEST_C, DIGEST_S = 37, 1800
COLLAGENASE_UL = 1.2
BEADS_UL = 3.0
DILUTION_UL = 40.0             # placeholder, see THE DILUTION PROBLEM
DILUTION_FLOW = 3.0            # kinetics-limited, uL/min
EXCHANGE_UL, EXCHANGE_WASHES, EXCHANGE_FLOW = 6.0, 2, 3.0
ELUTE_UL, ELUTE_FLOW = 3.0, 1.0
BEAD_FLOW = 5.0
LOAD_FLOW = 5.0

# --- dead volumes that are not geometry (assumptions) ---
OFFCHIP_LINE_UL = 0.5          # tubing and valve stem per transfer
HOLDUP_UL = 0.2                # film and corner liquid left when a chamber is drained
OFFCHIP_S = 20                 # tubing flush between chips

CH4 = {c["id"]: c for c in M4["chambers"]} | {c["id"]: c for c in M4["channels"]}
CH5 = {c["id"]: c for c in M5["chambers"]} | {c["id"]: c for c in M5["channels"]}
PORT4 = {p["id"]: p for p in M4["ports"]}
PORT5 = {p["id"]: p for p in M5["ports"]}


def max_flow(width_mm: float, height_mm: float, ceiling: float = HANDLING_SHEAR) -> float:
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (ceiling / 10.0) * w * h * h / (6.0 * MU_PA_S) * 60.0 * 1e9


def shear(flow_ul_min: float, width_mm: float, height_mm: float) -> float:
    q = flow_ul_min * 1e-9 / 60.0
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (6.0 * MU_PA_S * q / (w * h * h)) * 10.0


def _rect(chamber: dict) -> tuple[float, float, float, float]:
    (cx, cy, _), (sx, sy, _) = chamber["center"], chamber["size"]
    return cx - sx / 2, cx + sx / 2, cy - sy / 2, cy + sy / 2


def _gap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    dx = max(a[0] - b[1], b[0] - a[1], 0.0)
    dy = max(a[2] - b[3], b[2] - a[3], 0.0)
    return math.hypot(dx, dy)


def channel_ul(chip: dict, channel_id: str) -> float:
    """Volume of a channel (1 mm^3 = 1 uL). Length is the straight-line gap between its two ends,
    a lower bound: real routing is longer."""
    chambers = {c["id"]: _rect(c) for c in chip["chambers"]}
    ports = {p["id"]: (p["position"][0],) * 2 + (p["position"][1],) * 2 for p in chip["ports"]}
    ch = next(c for c in chip["channels"] if c["id"] == channel_id)
    a = chambers.get(ch["from"]) or ports[ch["from"]]
    b = chambers.get(ch["to"]) or ports[ch["to"]]
    return _gap(a, b) * ch["width_mm"] * ch["depth_mm"]


def route_flow(chip: dict, features: list[str], nominal: float) -> float:
    """Nominal flow, held under the handling ceiling (with margin) of the narrowest feature on the route."""
    dims = {c["id"]: c for c in chip["channels"]}
    caps = []
    for f in features:
        if f in dims:
            caps.append(max_flow(dims[f]["width_mm"], dims[f]["depth_mm"]))
        else:
            c = next(c for c in chip["chambers"] if c["id"] == f)
            caps.append(max_flow(c["size"][1], c["size"][2]))
    return float(min(nominal, math.floor(min(caps) / MARGIN)))


def exchange_s(washes: int, volume_ul: float, flow: float) -> int:
    return math.ceil(washes * volume_ul / flow * 60.0 * MARGIN)


# --- derived quantities -------------------------------------------------------------------

LINE_IN = {s: channel_ul(M4, f"ch_in_{s[-1]}") for s in SUBS}
LINE4 = max(LINE_IN.values())
MIX_UL = round(SUSP_UL + LINE4, 1)     # the tail left in the fill line is mix, so top it up
CONTENT_UL = SUSP_UL + PRIMER_UL + MIX_UL - LINE4   # what actually reaches the chamber
LINE_LIB = channel_ul(M5, "ch_in_lib")
LINE_PROT = channel_ul(M5, "ch_in_prot")
LINE_BEAD = channel_ul(M5, "ch_in_bead")
LINE_REACT_BEAD = channel_ul(M5, "ch_react_bead")
LINE_OUT = channel_ul(M5, "ch_out_lib")
TRANSFER_LOSS = HOLDUP_UL + OFFCHIP_LINE_UL + LINE_LIB
DELIVERED_UL = round(CONTENT_UL - TRANSFER_LOSS, 1)   # reaches react_cx
PRODUCT_UL = round(ELUTE_UL - HOLDUP_UL - LINE_OUT, 2)

F_FILL = route_flow(M4, ["ch_in_1"] + ["sub_1"], FILL_FLOW)
F_LOAD = route_flow(M5, ["ch_in_lib", "react_cx"], LOAD_FLOW)
F_DIG = route_flow(M5, ["ch_in_prot", "react_cx"], BEAD_FLOW)
F_DIL = route_flow(M5, ["ch_in_prot", "react_cx", "ch_react_bead", "bead_cx", "ch_waste"], DILUTION_FLOW)
F_BEADLOAD = route_flow(M5, ["ch_in_bead", "bead_cx"], BEAD_FLOW)
F_EXCH = route_flow(M5, ["ch_in_bead", "bead_cx", "ch_waste"], EXCHANGE_FLOW)
F_ELUTE = route_flow(M5, ["ch_in_bead", "bead_cx", "ch_out_lib"], ELUTE_FLOW)


def ramp_s(a: float, b: float) -> float:
    return abs(b - a) / RAMP_C_S


def profile() -> list[tuple[str, float, float, float]]:
    """The thermal profile as (label, temp C, dwell s, ramp s into it)."""
    prev = AMBIENT_C
    seq = [("initial denature", *INITIAL)]
    for n in range(1, CYCLES + 1):
        seq += [(f"cycle {n} {name}", t, d) for name, t, d in CYCLE]
    seq += [("final extension", *FINAL), ("hold", COLD_C, COLD_S)]
    out = []
    for label, t, d in seq:
        out.append((label, float(t), float(d), ramp_s(prev, t)))
        prev = t
    return out


# --- protocol -------------------------------------------------------------------------------


def build() -> dict:
    steps: list[dict] = []

    def emit(operation, target, note, duration_s, reagent=None, volume_ul=None, temp_c=None,
             washes=None, flow_ul_min=None):
        steps.append({"step": len(steps) + 1, "operation": operation, "target": target,
                      "reagent": reagent, "volume_ul": volume_ul, "duration_s": round(duration_s),
                      "temp_c": temp_c, "washes": washes, "flow_ul_min": flow_ul_min, "note": note})

    # step 15: interleaved fill
    pulse_s = math.ceil(PULSE_UL / F_FILL * 60.0 * MARGIN) + SWITCH_S
    for i, sub in enumerate(FILL_ORDER, start=1):
        emit("load", sub,
             f"Step 15, fill pulse {i} of {len(FILL_ORDER)}: the rotary valve is at position "
             f"{VALVE_POS[sub]} and sends one {PULSE_UL} uL pulse of the pooled capsule suspension to "
             f"{sub} (sublibrary {LETTER[sub]}). The palindromic order serves every chamber at the same "
             f"mean position, so settling in the reservoir averages out between the four.",
             pulse_s, reagent="pooled_capsule_suspension", volume_ul=PULSE_UL, flow_ul_min=F_FILL)
    for sub in SUBS:
        n = sub[-1]
        emit("exchange", sub,
             f"Step 15: one index primer into {sub} through in_s{n}. Each chamber gets its own, which is "
             f"what tells the four sublibraries apart after sequencing. It also pushes the capsules left in "
             f"the fill line into the chamber.",
             exchange_s(1, PRIMER_UL, F_FILL), reagent=f"index_primer_{n}", volume_ul=PRIMER_UL, washes=1,
             flow_ul_min=F_FILL)
    for sub in SUBS:
        emit("exchange", sub,
             f"Step 15: PCR mix into {sub}. The fill line keeps its own volume as dead space, so the mix "
             f"volume includes it. Capillary-stop vents let air out and hold the liquid, so the four "
             f"reactions stay isolated.",
             exchange_s(1, MIX_UL, F_FILL), reagent="pcr_mix", volume_ul=MIX_UL, washes=1, flow_ul_min=F_FILL)

    for label, temp, dwell, ramp in profile():
        what = "cool down and hold" if label == "hold" else label
        emit("hold_temp", "sub_1",
             f"Step 15, thermocycling, {what}: the whole module 4 chip, all four chambers, goes to {temp:g} C "
             f"({dwell:g} s dwell plus {ramp:.1f} s ramp at {RAMP_C_S:g} C/s). The chambers share one heater, "
             f"so one step stands for all four.",
             dwell + ramp, temp_c=temp)

    # steps 16 and 17: four passes through module 5
    for sub in PASSES:
        n = sub[-1]
        who = f"sublibrary {n} ({sub})"
        emit("load", "react_cx",
             f"Step 16, pass {n}: move {who} from module 4 to module 5 through in_lib, with tubing between "
             f"the chips. Four sublibraries will not fit react_cx together, so each gets its own pass and "
             f"keeps its own index.",
             math.ceil(DELIVERED_UL / F_LOAD * 60.0 * MARGIN) + OFFCHIP_S, reagent="pcr_product_capsules",
             volume_ul=DELIVERED_UL, flow_ul_min=F_LOAD)
        emit("load", "bead_cx",
             f"Step 16, pass {n}: load magnetic beads into bead_cx through in_bead with the magnet ON. "
             f"Retention is magnetic only: the beads are about 1 um and no feature can be that small.",
             math.ceil((BEADS_UL + LINE_BEAD) / F_BEADLOAD * 60.0 * MARGIN), reagent="bead_suspension",
             volume_ul=round(BEADS_UL + LINE_BEAD, 1), flow_ul_min=F_BEADLOAD)
        emit("release", "react_cx",
             f"Step 16, pass {n}: collagenase into react_cx through in_prot, all ports capped. It digests the "
             f"gelatin between crosslinks until the capsule dissolves. Proteinase K would leave these "
             f"capsules intact, and a chelator would stop collagenase, so none is carried in.",
             math.ceil((COLLAGENASE_UL + LINE_PROT) / F_DIG * 60.0 * MARGIN), reagent="collagenase_solution")
        emit("hold_temp", "react_cx",
             f"Step 16, pass {n}: hold react_cx at {DIGEST_C} C while the capsules dissolve and the DNA is "
             f"released into solution.",
             DIGEST_S, temp_c=DIGEST_C)
        emit("exchange", "react_cx",
             f"Step 16, pass {n}: DILUTE, then capture. Dilution buffer is pushed through react_cx into "
             f"bead_cx at low flow. The dissolved shell carries about 10,000 times more polymer than DNA, "
             f"and at that level bead binding fails, so the digest must be diluted before it meets the "
             f"beads. The DNA binds to the magnet-held beads as it passes; the liquid leaves by out_waste.",
             exchange_s(1, DILUTION_UL, F_DIL), reagent="dilution_buffer", volume_ul=DILUTION_UL, washes=1,
             flow_ul_min=F_DIL)
        emit("exchange", "bead_cx",
             f"Step 16, pass {n}: buffer exchange on the bead pellet with an aqueous, ethanol-free buffer, "
             f"magnet ON. There is no ethanol wash and no drying on chip: a drying meniscus pulls a bead "
             f"about 100,000 times harder than the magnet and wrecks the pellet.",
             exchange_s(EXCHANGE_WASHES, EXCHANGE_UL, F_EXCH), reagent="exchange_buffer",
             volume_ul=EXCHANGE_UL, washes=EXCHANGE_WASHES, flow_ul_min=F_EXCH)
        emit("exchange", "bead_cx",
             f"Step 16, pass {n}: elute in a small volume through in_bead, out_lib vented and out_waste "
             f"capped, magnet still ON so the beads stay put.",
             exchange_s(1, ELUTE_UL, F_ELUTE), reagent="elution_buffer", volume_ul=ELUTE_UL, washes=1,
             flow_ul_min=F_ELUTE)
        emit("release", "bead_cx",
             f"Step 17, pass {n}: library {n} leaves through out_lib into a tube, chased with air to recover "
             f"the liquid left in the short out_lib channel.",
             300, reagent="elution_buffer")

    emit("release", "bead_cx",
         "Step 17, hand-off to the bench: mix the four libraries in a tube and do the ratio-based size "
         "selection that removes primer dimers there. It is off chip because it needs an ethanol wash and "
         "dry, which a chip cannot do.",
         900, reagent="ratio_size_selection")
    emit("release", "bead_cx",
         "Step 17, hand-off to the bench through out_lib: off-chip QC on the finished library (size "
         "distribution and quantity) before sequencing.",
         1800, reagent="off_chip_qc")

    return {
        "id": "library_out_v1",
        "name": "Sublibrary split, index PCR, release and cleanup, library out (steps 15-17)",
        "source": SOURCE,
        "placeholder": False,
        "description": (
            "Runs across two chips: module4_v1 for the interleaved split and index PCR, then module5_v1 "
            "for the collagenase release, the bead cleanup and the library outlet. Built in the protocol "
            "sheet's order (PCR before release); published practice releases first, so that order is "
            "contested and the reasoning is in protocols/build_library_out.py. Four chambers are filled "
            "one at a time by an off-chip rotary valve in the order A B C D D C B A, twice, to keep the "
            "sublibraries even. The PCR is written out cycle by cycle. Module 5 runs one pass per "
            "sublibrary, with an explicit dilution before the beads, no ethanol wash on chip, and "
            "ratio-based size selection left as an off-chip tube step. Reagent names are generic and the "
            "volumes, setpoints and cycle count are round placeholders to be fitted. Flows are computed "
            "from chamber geometry against a capsule-handling shear of 1 dyn/cm2; the 0.01 dyn/cm2 "
            "culture ceiling does not apply to capsules."
        ),
        "steps": steps,
    }


def report() -> None:
    print("Interleaved fill order (rotary valve position, chamber, letter), pulse of "
          f"{PULSE_UL} uL at {F_FILL:g} uL/min:")
    print("  " + " ".join(LETTER[s] for s in FILL_ORDER[:8]) + "   "
          + " ".join(LETTER[s] for s in FILL_ORDER[8:]))
    for i, s in enumerate(FILL_ORDER, start=1):
        print(f"  pulse {i:>2}  valve pos {VALVE_POS[s]}  {s}  ({LETTER[s]})")
    for s in SUBS:
        slots = [i for i, x in enumerate(FILL_ORDER[:8], start=1) if x == s]
        print(f"  {s}: slots {slots} in each palindrome, mean position {sum(slots) / len(slots):.1f}, "
              f"{FILL_ORDER.count(s)} pulses = {FILL_ORDER.count(s) * PULSE_UL:.1f} uL")
    print(f"  {len(FILL_ORDER)} pulses ({PALINDROMES} palindromes; 8-16 allowed). "
          f"Expected CV 3-8% interleaved, against 30-100% for a symmetric manifold.\n")

    print(f"Thermal profile, {CYCLES} cycles (8-14 allowed), ramp {RAMP_C_S:g} C/s (2-4 allowed):")
    print(f"  {'segment':<20}{'temp C':>8}{'dwell s':>9}{'ramp s':>8}{'step s':>8}{'total s':>9}")
    total = 0.0
    shown = profile()
    for label, t, d, r in shown:
        total += d + r
        if label.startswith("cycle") and not label.startswith(("cycle 1 ", f"cycle {CYCLES} ")):
            if label == "cycle 2 denature":
                print(f"  ... cycles 2-{CYCLES - 1} repeat the same three steps")
            continue
        print(f"  {label:<20}{t:>8.0f}{d:>9.0f}{r:>8.1f}{d + r:>8.0f}{total:>9.0f}")
    print(f"  total {total:.0f} s = {total / 60:.1f} min over {len(shown)} hold_temp steps\n")

    print("Dead-volume budget (uL). Straight-line channel lengths are a lower bound; off-chip tubing and "
          "chamber hold-up are assumptions.")
    rows = [
        ("fill line in_s -> sub (x4 each, primed once)", LINE4,
         "kept between pulses, pushed into the chamber by the primer and mix, so the mix volume covers it"),
        ("rotary valve stem + tubing (one-off)", OFFCHIP_LINE_UL, "assumption; stays primed after the last pulse"),
        ("transfer: sub chamber hold-up", HOLDUP_UL, "assumption; film and corners"),
        ("transfer: off-chip tubing", OFFCHIP_LINE_UL, "assumption"),
        ("transfer: ch_in_lib", LINE_LIB, "fills with sample and stays"),
        ("dilution flush: react_cx hold-up", HOLDUP_UL, "assumption; DNA left behind"),
        ("dilution flush: ch_react_bead", LINE_REACT_BEAD, "carries sample past the beads, no loss if bound"),
        ("elution: bead_cx hold-up", HOLDUP_UL, "assumption"),
        ("elution: ch_out_lib", LINE_OUT, "product left in the line unless chased with air"),
        ("reagent only: ch_in_prot", LINE_PROT, "collagenase and dilution line, no product"),
        ("reagent only: ch_in_bead", LINE_BEAD, "bead and elution line, no product"),
    ]
    print(f"  {'where':<46}{'uL':>7}  note")
    for name, v, note in rows:
        print(f"  {name:<46}{v:>7.2f}  {note}")
    print()
    print(f"  per sublibrary, uL: in chamber {CONTENT_UL:.2f} -> transfer loss {TRANSFER_LOSS:.2f} -> "
          f"delivered {DELIVERED_UL:.1f} -> eluted {ELUTE_UL:.1f}")
    print(f"  product per sublibrary {ELUTE_UL:.1f} - {HOLDUP_UL:.1f} hold-up - {LINE_OUT:.2f} line = "
          f"{PRODUCT_UL:.2f} uL; four sublibraries {4 * PRODUCT_UL:.1f} uL")
    lost = TRANSFER_LOSS + HOLDUP_UL * 2 + LINE_OUT
    print(f"  sample-path dead volume per sublibrary {lost:.2f} uL against {PRODUCT_UL:.2f} uL of product "
          f"({lost / PRODUCT_UL:.1f}x)\n")

    print(f"Shear (dyn/cm2). Handling ceiling {HANDLING_SHEAR:g}, an assumption; the 0.01 culture ceiling does not apply.")
    print(f"  {'route':<34}{'flow':>6}  {'chamber':>8}  {'narrowest channel':>18}")
    rows = [
        ("fill, module 4", M4, F_FILL, "sub_1", "ch_in_1"),
        ("load, module 5", M5, F_LOAD, "react_cx", "ch_in_lib"),
        ("dilution and capture", M5, F_DIL, "bead_cx", "ch_in_prot"),
        ("buffer exchange", M5, F_EXCH, "bead_cx", "ch_in_bead"),
        ("elution", M5, F_ELUTE, "bead_cx", "ch_out_lib"),
    ]
    for name, chip, q, cid, chid in rows:
        c = next(x for x in chip["chambers"] if x["id"] == cid)
        ch = next(x for x in chip["channels"] if x["id"] == chid)
        print(f"  {name:<34}{q:>6.1f}  {shear(q, c['size'][1], c['size'][2]):>8.4f}  "
              f"{shear(q, ch['width_mm'], ch['depth_mm']):>18.4f}")


def split_by_chip(protocol: dict) -> list[tuple]:
    """One protocol file per chip.

    tools/validate.py checks a protocol against a single chip, which is right: a chip
    cannot run a step that targets a chamber it does not have. This run spans two chips,
    so it is written out as two protocols that share one generator and run back to back.
    """
    m4 = {c["id"] for c in M4["chambers"]} | {p["id"] for p in M4["ports"]}
    parts = {"module4_v1": [], "module5_v1": []}
    for st in protocol["steps"]:
        parts["module4_v1" if st["target"] in m4 else "module5_v1"].append(st)
    out = []
    for cid, path, half in (("module4_v1", OUT_M4, "steps 15: interleaved split and index PCR"),
                            ("module5_v1", OUT_M5, "steps 16-17: release, cleanup, library out")):
        steps = [dict(s, step=i + 1) for i, s in enumerate(parts[cid])]
        out.append((path, {**protocol, "id": cid,
                           "name": f"{protocol['name']} - {cid}",
                           "description": f"{half.capitalize()}, on chip/{cid}.json. "
                                          f"Second half of a two-chip run; see "
                                          f"protocols/build_library_out.py. "
                                          + protocol["description"],
                           "steps": steps}))
    return out


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    protocol = build()
    for path, part in split_by_chip(protocol):
        path.write_text(json.dumps(part, indent=2) + "\n")
        total = sum(s["duration_s"] for s in part["steps"])
        print(f"wrote {path.name}: {len(part['steps'])} steps, {total / 3600:.1f} hours")

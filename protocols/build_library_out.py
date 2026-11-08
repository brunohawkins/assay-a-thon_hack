"""Build protocols/library_out_v1.json.

    python protocols/build_library_out.py           # write the protocol
    python protocols/build_library_out.py --report  # fill order, thermal profile, dead volumes, shear

Steps 15 to 17 of the library prep, across TWO chips. The input is the pooled, barcoded
capsule suspension handed over from stage 4b (per-cell barcoding in oil, protocols/sctip_spc_v1.json). The output is a sequencing-ready library.

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
  15b. COLLECTION, still on chip/module4_v1.json. The amplified sublibraries leave through
      out_sub, the chip's capsule-passable product outlet, ONE AT A TIME along the shared
      collection spine (node_1..node_4). Each chamber taps the spine through a 0.2 mm
      constriction that opens into a 0.6 mm node: that step is a capillary stop, so the spine
      stays dry through filling and through the whole thermal profile, and the four reactions
      only ever meet the spine after their PCR is over. Collection is driven from the chamber's
      own fill port with AIR and p_vent CAPPED, so the spine is one-way toward out_sub and the
      sublibrary is never diluted; what stays behind is the wetted film, not the bore. Order is
      sub_1 first (farthest from out_sub, so it crosses a virgin spine) to sub_4 last (one
      segment). CARRY_UL below is the worst-case film one pass can hand the next, straight from
      the geometry; --report prints it as a fraction of the sublibrary. A sublibrary can never
      be contaminated by one still sealed behind its own stop, only by one already collected.
  16. RELEASE + CLEANUP, chip/module5_v1.json. Each sublibrary arrives from module 4's out_sub,
      through tubing, at module 5's in_lib: that is the module4_to_module5 hand-off, and it is
      now a named port on both sides. Collagenase digest at 37 C in react_cx, NOT proteinase K,
      which leaves these capsules intact. Then magnetic bead capture in bead_cx, buffer exchange
      and elution. Module 5 holds 8.1 uL in react_cx while the four sublibraries add up to far
      more, so it runs FOUR PASSES, one per sublibrary, in the same order they were collected.
      The indices differ, so the four eluates can be mixed for sequencing in the tube at the end.
  17. LIBRARY OUT through out_lib, then off-chip QC.

Operations. A fill pulse, a bead load and a sublibrary transfer are `load`; adding primer, PCR mix,
dilution, wash and elution buffer are `exchange` (flow-through, nothing is retained, there is no
mesh); the collagenase digest, the sublibrary collection through out_sub and the library hand-offs
are `release`; the PCR and the digest are `hold_temp`. There is no `split` step: the split is done
by the valve, not by the chip, and the
planner's split reads channel topology that module 4 deliberately does not have. The PCR is written
out as one `hold_temp` per segment, not with the `cycles` field: the operator sends the step's
temp_c and duration_s only, and the twin reads the cycle number from each step's narration, so a
single `cycles` step would drop the thermal profile from the run file. Each step also records its
port state (`ports`): the chips route by port, and only a capped port is a true block.

Volumes and flow. Nothing numeric about liquid is typed in here. Each volume is a fraction of a
chamber's volume_ul (SUSP_FRACTION, PRIMER_FRACTION, BEADS_FRACTION, ...), and `volume_ul` of an
exchange is per wash. Duration = washes*volume/flow*60 times a 1.4 margin.

WHICH CEILING APPLIES TO WHICH CHAMBER. This file used to say: "Capsules do not adhere and would
tolerate more, but the project applies the 0.01 ceiling to every chamber, so it is applied here."
contracts/parameters.json now says the opposite in as many words: shear.culture_ceiling_dyn_cm2
"applies to chambers holding ADHERENT CELLS... It does NOT apply to chambers holding CAPSULES: a
capsule is not attached to anything and the limit there is capsule.escape_pressure_kpa". Neither
chip here holds a cell attached to anything, so the culture ceiling applies to NOTHING on this
path, and there are two different bounds instead:

  sub_1..sub_4, and react_cx while the capsules are still intact (the fill pulses, the primer and
  mix additions, the collection, the arrival at module 5 and the collagenase in) are CAPSULE
  steps. The bound is pressure across a packed capsule bed, kept under control.pressure_margin of
  capsule.escape_pressure_kpa, with the bed resistance from contracts/parameters.json
  (mesh.bed_resistance_kpa_per_ul_min). See capsule_flow(). That is about 59.5 uL/min rather than
  the 2.9-3.4 uL/min the culture ceiling allowed, and it is why these steps used to take so long
  for no cell's benefit. The capillary stops are untouched by it: driving 59.5 uL/min through a
  0.2 mm tap costs a few Pa against a stop that holds of order a kPa.

  bead_cx (and the dilution pass that crosses it) holds about 1 um magnetic beads held by a
  magnet. No cell and no capsule, so neither bound applies: what sets the pace is SPRI binding and
  elution kinetics. Those flows are left exactly as they were, as kinetics PLACEHOLDERS to be
  fitted on real eluates, and route_flow() is kept for them alone with that caveat attached. They
  are not shear limits and must not be read as any.

Narrow channels see more shear at these flows (--report prints them); no chamber here is held to a
shear ceiling at all.

WHAT AN EXCHANGE IS FOR IS A FIELD, NOT PROSE. Every exchange carries `exchange_kind`. The index
primer and the PCR mix are FILLs: they go into a part-air-filled chamber behind a capillary stop,
nothing drains, and they are bounded ABOVE by the chamber's volume_ul (together with the capsule
suspension they bring it to REACTION_FILL). They are arguably not exchanges at all - but the
instruction set's `load` carries no reagent (operator/commands.py: load(target, volume_ul)) and the
model draws the capsule carrier for it, so calling them `load` would throw away the index identity
that is the entire point of module 4. `exchange` with exchange_kind "fill" is what says it. The
dilution and the bead buffer exchange are SWEEPs, sized from exchange.target_fraction; the elution
is a PARTIAL, deliberately half a bead_cx volume, because a small eluate is the point.

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

Port states, module 4 (no valves either): filling, priming and the two reagent additions feed the
chamber's own in_s port with p_vent vented and out_sub CAPPED, so the capillary stop at the node
is the thing holding the liquid. Thermocycling caps everything. Collection feeds the chamber's own
in_s port with out_sub vented and p_vent CAPPED, which is what makes the spine one-way.

Port states, module 5 (no valves; a capped port is the only true block): digest hold, in_lib and
in_prot and in_bead and both outlets capped. Dilution and capture, in_prot feeds, out_waste
vented, in_bead and out_lib capped, magnet ON. Buffer exchange, in_bead feeds, out_waste vented.
Elution, in_bead feeds, out_lib vented, out_waste capped, magnet ON until the eluate has left.
Reagent names are generic and nothing here is a concentration or a recipe: see CONFIDENTIALITY.md.
Setpoints, cycle count and the fractions are round placeholders to be fitted.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT_M4 = HERE / "module4_v1.json"
OUT_M5 = HERE / "module5_v1.json"
CHIPS = ROOT / "chip"
CONTRACT = ROOT / "contracts" / "parameters.json"
M4 = json.loads((CHIPS / "module4_v1.json").read_text())
M5 = json.loads((CHIPS / "module5_v1.json").read_text())

SOURCE = (
    "Capsule workflow: Baronas et al., Science 391, 1138 (2026), doi:10.1126/science.ady7227 "
    "(CapSeq). Split-pool barcoding with a final indexing PCR: Rosenberg AB et al., Science "
    "2018;360:176, doi:10.1126/science.aam8999 (SPLiT-seq). Chip geometry, the interleaved "
    "fill order, the dead-volume accounting and the flows are ours."
)


def parameter(name: str) -> float:
    """One value from contracts/parameters.json. No fallback: a missing contract is a bug,
    and a literal copied here is how mu lost its provenance last time."""
    return float(json.loads(CONTRACT.read_text())["parameters"][name]["value"])


# mu is the 37 C culture-medium value the contract tells every protocol calculation to use,
# READ from the contract. This file used to carry a literal 0.8e-3: the right physics with no
# provenance, and nothing to stop it drifting from the contract again.
MU_PA_S = parameter("fluid.viscosity_37c_pa_s")
SHEAR_CEILING = parameter("shear.culture_ceiling_dyn_cm2")   # ADHERENT CELLS ONLY; see the docstring
ESCAPE_KPA = parameter("capsule.escape_pressure_kpa")
PRESSURE_MARGIN = parameter("control.pressure_margin")
BED_RESISTANCE = parameter("mesh.bed_resistance_kpa_per_ul_min")
EXCHANGE_TARGET = parameter("exchange.target_fraction")
SWEEP_CV = -math.log(1.0 - EXCHANGE_TARGET)   # chamber volumes a sweep needs to hit the goal
FLOW_FRACTION = 0.85   # run at this fraction of whichever bound applies
MARGIN = 1.4

# --- split and PCR (module 4) ---
PALINDROME = ["sub_1", "sub_2", "sub_3", "sub_4", "sub_4", "sub_3", "sub_2", "sub_1"]
PALINDROMES = 2                # 8 pulses per palindrome; 8-16 pulses allowed
FILL_ORDER = PALINDROME * PALINDROMES
SUBS = ["sub_1", "sub_2", "sub_3", "sub_4"]
VALVE_POS = {s: i + 1 for i, s in enumerate(SUBS)}
LETTER = {s: "ABCD"[i] for i, s in enumerate(SUBS)}
SUSP_FRACTION = 0.3            # of a sub chamber's volume, in capsule suspension over all pulses
PRIMER_FRACTION = 0.075        # of a sub chamber's volume
REACTION_FILL = 0.7            # of a sub chamber's volume once suspension, primer and mix are in
SWITCH_S = 5                   # valve switch and settle between pulses, assumption
COLLECT_ORDER = SUBS           # south to north: sub_1 crosses a virgin spine, sub_4 one segment
AIR_CHASE_S = 60               # air held on after the liquid has left, to clear the route; assumption
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
COLLAGENASE_FRACTION = 0.15    # of react_cx's volume
BEADS_FRACTION = 0.5           # of bead_cx's volume
# Dilution buffer pushed through react_cx, in react_cx volumes IN TOTAL. The fold is a
# chemistry placeholder to be fitted (see THE DILUTION PROBLEM); the SPLIT into washes is not
# free, because volume_ul is per wash and the planner can only multiply the stated per-wash
# volume by a small number of washes. Four washes of 1.25 chamber volumes keeps every wash
# above one chamber volume and the total above SWEEP_CV, so the step both dilutes as intended
# and provably clears react_cx.
DILUTION_FOLD, DILUTION_WASHES = 5, 4
# Bead buffer exchange: a SWEEP, so its total is the exchange goal and not a round number.
# It used to be 2 washes of one bead_cx volume = 2 chamber volumes, which leaves exp(-2) = 14%
# of the binding buffer behind: 86% exchanged against a 99% goal, i.e. a step that could not do
# what it said. Three washes keeps each one above a chamber volume.
EXCHANGE_WASHES = 3
EXCHANGE_FRACTION = SWEEP_CV / EXCHANGE_WASHES   # of bead_cx's volume per wash
ELUTE_FRACTION = 0.5           # of bead_cx's volume: a PARTIAL, a small eluate on purpose
assert DILUTION_FOLD >= SWEEP_CV, "the dilution sweep would not reach exchange.target_fraction"
assert DILUTION_FOLD / DILUTION_WASHES >= 1.0 and EXCHANGE_FRACTION >= 1.0, \
    "a sweep's per-wash volume must be at least one chamber volume"
CAPTURE_SLOWDOWN = 0.5         # kinetics-limited steps run at this fraction of the shear-ceiling flow
ELUTE_SLOWDOWN = 0.25

# --- dead volumes that are not chip geometry (assumptions) ---
TUBE_ID_MM, TUBE_LEN_MM = 0.5, 2.5   # off-chip tubing and valve stem per transfer
OFFCHIP_LINE_UL = math.pi / 4 * TUBE_ID_MM ** 2 * TUBE_LEN_MM
HOLDUP_FRACTION = 0.025        # of a chamber's volume: film and corner liquid left when it is drained
OFFCHIP_S = 20                 # tubing flush between chips

CH4 = {c["id"]: c for c in M4["chambers"]} | {c["id"]: c for c in M4["channels"]}
CH5 = {c["id"]: c for c in M5["chambers"]} | {c["id"]: c for c in M5["channels"]}
PORT4 = {p["id"]: p for p in M4["ports"]}
# Module 4 has two outlets and they are not interchangeable: p_vent is the air leg behind the
# capillary stops, OUT4 is the capsule-passable product outlet the sublibraries actually leave by.
VENT4 = next(p["id"] for p in M4["ports"]
             if p["type"] == "outlet" and p.get("capsule_passable") is False)
OUT4 = next(p["id"] for p in M4["ports"]
            if p["type"] == "outlet" and p.get("capsule_passable") is True)
PORT5 = {p["id"]: p for p in M5["ports"]}
WASTE5, LIB5 = "out_waste", "out_lib"
assert WASTE5 in PORT5 and LIB5 in PORT5, "module 5 lost a port this protocol routes through"


def max_flow(width_mm: float, height_mm: float, ceiling: float = SHEAR_CEILING) -> float:
    """Highest flow in uL/min that keeps wall shear at or below the ceiling."""
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


def capsule_flow() -> float:
    """FLOW_FRACTION of the flow that keeps the pressure across a PACKED CAPSULE BED under
    control.pressure_margin of capsule.escape_pressure_kpa, floored to 0.01 uL/min.

    This is the bound for a chamber holding capsules, and it is the only bound the contract
    gives for one: the risk is a soft capsule being squeezed through whatever constriction it
    is packed against (here the 0.2 mm tap out of a sub chamber, or ch_react_bead out of
    react_cx), not a cell being detached from a surface it is stuck to. The bed resistance is
    the contract's mesh.bed_resistance_kpa_per_ul_min at full cover, which is the same number
    operator/model.py and operator/safety.py judge a run against, so a flow derived here and a
    flow approved there cannot disagree. Geometry does not enter: a packed bed, not the
    chamber's cross-section, is what the pressure falls across.
    """
    return math.floor(FLOW_FRACTION * PRESSURE_MARGIN * ESCAPE_KPA / BED_RESISTANCE * 100) / 100


def route_flow(chip: dict, chambers: list[str], slowdown: float = 1.0) -> float:
    """KINETICS PLACEHOLDER for the bead steps only, to be fitted on real eluates.

    bead_cx holds about 1 um magnetic beads on a magnet: no adherent cell and no capsule, so
    neither shear.culture_ceiling_dyn_cm2 nor capsule.escape_pressure_kpa bounds it, and what
    sets the pace is SPRI binding and elution kinetics. The numbers this returns are the ones
    the file has always used - FLOW_FRACTION of the flow that would give SHEAR_CEILING in the
    tightest chamber on the route, times `slowdown` - kept unchanged so the bead residence
    times do not move. They are NOT a shear limit and must not be read as one; they are a
    stand-in for a binding time nobody has measured yet.
    """
    caps = []
    for f in chambers:
        c = next(c for c in chip["chambers"] if c["id"] == f)
        caps.append(max_flow(c["size"][1], c["size"][2]))
    return math.floor(FLOW_FRACTION * slowdown * min(caps) * 100) / 100


def exchange_s(washes: int, volume_ul: float, flow: float) -> int:
    return math.ceil(washes * volume_ul / flow * 60.0 * MARGIN)


# --- derived quantities -------------------------------------------------------------------

def vol(chip: dict, chamber_id: str) -> float:
    return next(c for c in chip["chambers"] if c["id"] == chamber_id)["volume_ul"]


V_SUB = min(vol(M4, x) for x in SUBS)      # the four chambers are identical; take the smallest
V_REACT, V_BEAD = vol(M5, "react_cx"), vol(M5, "bead_cx")

PULSE_UL = round(SUSP_FRACTION * V_SUB / FILL_ORDER.count("sub_1"), 3)   # per pulse, four pulses per chamber
SUSP_UL = PULSE_UL * FILL_ORDER.count("sub_1")
PRIMER_UL = round(PRIMER_FRACTION * V_SUB, 3)
COLLAGENASE_UL = round(COLLAGENASE_FRACTION * V_REACT, 3)
BEADS_UL = round(BEADS_FRACTION * V_BEAD, 3)
DILUTION_UL = DILUTION_FOLD * V_REACT      # in total, over DILUTION_WASHES washes
DILUTION_PER_WASH_UL = round(DILUTION_FOLD / DILUTION_WASHES * V_REACT, 3)
# rounded UP, never down: a sweep rounded down sits below the goal it was derived from
EXCHANGE_UL = math.ceil(EXCHANGE_FRACTION * V_BEAD * 1000) / 1000
ELUTE_UL = round(ELUTE_FRACTION * V_BEAD, 3)
HOLDUP_SUB, HOLDUP_REACT, HOLDUP_BEAD = (round(HOLDUP_FRACTION * v, 3) for v in (V_SUB, V_REACT, V_BEAD))

LINE_IN = {s: channel_ul(M4, f"ch_in_{s[-1]}") for s in SUBS}
LINE4 = max(LINE_IN.values())
# the tail left in the fill line is mix, so the mix is topped up to bring the chamber to REACTION_FILL
MIX_UL = round(REACTION_FILL * V_SUB - SUSP_UL - PRIMER_UL + LINE4, 3)
CONTENT_UL = SUSP_UL + PRIMER_UL + MIX_UL - LINE4   # what actually reaches the chamber

# --- the shared collection spine on module 4 ------------------------------------------------
# One spine, reached in sequence. A sublibrary's route out is its own tap, then every spine
# segment between its node and node_4, then ch_out. Nothing else is in the route, because the
# spine only ever flows toward OUT4: p_vent is capped for every collection pass.
SPINE = ["ch_spine_12", "ch_spine_23", "ch_spine_34"]


def collect_path(sub: str) -> list[str]:
    return [f"ch_tap_{sub[-1]}"] + SPINE[SUBS.index(sub):] + ["ch_out"]


PATH_UL = {s: round(sum(channel_ul(M4, c) for c in collect_path(s)), 3) for s in SUBS}


def shared_with_earlier(sub: str) -> float:
    """Channel volume on this sublibrary's route that an earlier pass has already wetted."""
    i = COLLECT_ORDER.index(sub)
    earlier = {c for s in COLLECT_ORDER[:i] for c in collect_path(s)}
    return sum(channel_ul(M4, c) for c in collect_path(sub) if c in earlier)


# What a pass leaves in the spine (lost), and the worst case it can hand the next pass. The air
# chase means the loss is the wetted film, not the bore: HOLDUP_FRACTION of the route.
SPINE_HOLDUP = {s: round(HOLDUP_FRACTION * PATH_UL[s], 3) for s in SUBS}
COLLECT_LOSS = max(SPINE_HOLDUP.values())
CARRY_UL = round(HOLDUP_FRACTION * max(shared_with_earlier(s) for s in SUBS), 4)
LINE_LIB = channel_ul(M5, "ch_in_lib")
LINE_PROT = channel_ul(M5, "ch_in_prot")
LINE_BEAD = channel_ul(M5, "ch_in_bead")
LINE_REACT_BEAD = channel_ul(M5, "ch_react_bead")
LINE_OUT = channel_ul(M5, "ch_out_lib")
TRANSFER_LOSS = HOLDUP_SUB + COLLECT_LOSS + OFFCHIP_LINE_UL + LINE_LIB
DELIVERED_UL = round(CONTENT_UL - TRANSFER_LOSS, 2)   # reaches react_cx
CARRY_FRACTION = CARRY_UL / DELIVERED_UL              # worst-case index cross-talk, pass to pass
PRODUCT_UL = round(ELUTE_UL - HOLDUP_BEAD - LINE_OUT, 2)
BEAD_LOAD_UL = round(BEADS_UL + LINE_BEAD, 3)

assert MIX_UL <= V_SUB and PRIMER_UL <= V_SUB and PULSE_UL <= V_SUB, "a module 4 step exceeds sub chamber volume"
assert DELIVERED_UL + COLLAGENASE_UL <= V_REACT, "digest does not fit react_cx"
assert BEAD_LOAD_UL <= V_BEAD, "bead load does not fit bead_cx"

# Capsule steps: bounded by escape pressure across a packed bed, not by a cell ceiling.
F_FILL = capsule_flow()     # sub_* fill pulses, primer, mix and collection
F_LOAD = capsule_flow()     # intact capsules arriving in react_cx
F_DIG = capsule_flow()      # collagenase into react_cx, capsules still intact
# Bead steps: kinetics placeholders, unchanged (see route_flow).
F_DIL = route_flow(M5, ["react_cx", "bead_cx"], CAPTURE_SLOWDOWN)
F_BEADLOAD = route_flow(M5, ["bead_cx"])
F_EXCH = route_flow(M5, ["bead_cx"], CAPTURE_SLOWDOWN)
F_ELUTE = route_flow(M5, ["bead_cx"], ELUTE_SLOWDOWN)
CULTURE_CEILING_FLOWS = {   # what the (inapplicable) culture ceiling would have allowed
    "sub_*": route_flow(M4, ["sub_1"]),
    "react_cx": route_flow(M5, ["react_cx"]),
}


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


def ports_for(chip: dict, feed: list[str] = (), vent: list[str] = ()) -> dict:
    """Port state for a step: every port not fed or vented is capped."""
    feed, vent = list(feed), list(vent)
    return {"feed": feed, "vent": vent,
            "cap": [p["id"] for p in chip["ports"] if p["id"] not in feed + vent]}


def build() -> dict:
    steps: list[dict] = []

    def emit(operation, target, note, duration_s, reagent=None, volume_ul=None, temp_c=None,
             washes=None, flow_ul_min=None, ports=None, exchange_kind=None):
        if operation == "exchange" and exchange_kind is None:
            raise ValueError(f"exchange on {target} must declare exchange_kind ('sweep', 'partial' "
                             f"or 'fill'); an absent one means the strict sweep rule, so it is "
                             f"stated here rather than left to a default")
        step = {"step": len(steps) + 1, "operation": operation, "target": target,
                "reagent": reagent, "volume_ul": volume_ul, "duration_s": round(duration_s),
                "temp_c": temp_c, "washes": washes, "flow_ul_min": flow_ul_min, "note": note}
        if exchange_kind is not None:
            step["exchange_kind"] = exchange_kind
        if ports is not None:
            step["ports"] = ports
        steps.append(step)

    # step 15: interleaved fill
    pulse_s = math.ceil(PULSE_UL / F_FILL * 60.0 * MARGIN) + SWITCH_S
    for i, sub in enumerate(FILL_ORDER, start=1):
        emit("load", sub,
             f"Step 15, fill pulse {i} of {len(FILL_ORDER)}: the rotary valve is at position "
             f"{VALVE_POS[sub]} and sends one {PULSE_UL} uL pulse of the pooled capsule suspension to "
             f"{sub} (sublibrary {LETTER[sub]}). The palindromic order serves every chamber at the same "
             f"mean position, so settling in the reservoir averages out between the four.",
             pulse_s, reagent="pooled_capsule_suspension", volume_ul=PULSE_UL, flow_ul_min=F_FILL,
             ports=ports_for(M4, [f"in_s{sub[-1]}"], [VENT4]))
    for sub in SUBS:
        n = sub[-1]
        emit("exchange", sub,
             f"Step 15: one index primer into {sub} through in_s{n}. Each chamber gets its own, which is "
             f"what tells the four sublibraries apart after sequencing. It also pushes the capsules left in "
             f"the fill line into the chamber. FILL, not a sweep: {sub} is part air at this point, behind "
             f"its capillary stop, and nothing drains - this adds {PRIMER_FRACTION:.1%} of the chamber's "
             f"{V_SUB:g} uL on top of the suspension already in it, on the way to "
             f"{REACTION_FILL:.0%} full. A fill is bounded ABOVE by the chamber, never below: sweeping a "
             f"chamber you are still filling would wash the index straight out of it.",
             exchange_s(1, PRIMER_UL, F_FILL), reagent=f"index_primer_{n}", volume_ul=PRIMER_UL, washes=1,
             flow_ul_min=F_FILL, exchange_kind="fill", ports=ports_for(M4, [f"in_s{n}"], [VENT4]))
    for sub in SUBS:
        emit("exchange", sub,
             f"Step 15: PCR mix into {sub}. The fill line keeps its own volume as dead space, so the mix "
             f"volume includes it. Capillary-stop vents let air out and hold the liquid, so the four "
             f"reactions stay isolated. FILL: this is the last of the three additions and it brings the "
             f"chamber to {REACTION_FILL:.0%} of its {V_SUB:g} uL. Nothing leaves, so there is nothing to "
             f"sweep; the bound is the chamber volume above, not the exchange goal below.",
             exchange_s(1, MIX_UL, F_FILL), reagent="pcr_mix", volume_ul=MIX_UL, washes=1, flow_ul_min=F_FILL,
             exchange_kind="fill", ports=ports_for(M4, [f"in_s{sub[-1]}"], [VENT4]))

    for label, temp, dwell, ramp in profile():
        what = "cool down and hold" if label == "hold" else label
        emit("hold_temp", "sub_1",
             f"Step 15, thermocycling, {what}: the whole module 4 chip, all four chambers, goes to {temp:g} C "
             f"({dwell:g} s dwell plus {ramp:.1f} s ramp at {RAMP_C_S:g} C/s). The chambers share one heater, "
             f"so one step stands for all four.",
             dwell + ramp, temp_c=temp, ports=ports_for(M4))

    # step 15b: collect the four sublibraries, one at a time, along the shared spine
    for i, sub in enumerate(COLLECT_ORDER, start=1):
        n = sub[-1]
        route = " -> ".join(collect_path(sub))
        emit("release", sub,
             f"Step 15b, collection {i} of {len(COLLECT_ORDER)}: send sublibrary {LETTER[sub]} out of {sub} "
             f"through out_sub, driven by AIR at in_s{n} with out_sub vented and p_vent CAPPED. Air, not a "
             f"chase liquid: the sublibrary is not diluted, and it arrives at module 5 at the volume it left "
             f"at. The push bursts this chamber's capillary stop and runs {route} ({PATH_UL[sub]} uL of "
             f"channel); the other three stops are untouched and dry, so the other three sublibraries stay "
             f"sealed in their chambers, and the spine only ever flows toward out_sub. The spine was dry for "
             f"the whole thermal profile, so no index met another while there was anything to cross-prime. "
             f"What stays behind is the wetted film, not the bore: about {SPINE_HOLDUP[sub]} uL lost to the "
             f"spine, and at most {CARRY_UL:g} uL of this pass handed to the next ({CARRY_FRACTION:.1%} of a "
             f"sublibrary). That is the cross-contamination bound; it shows up as index hopping at that rate "
             f"and is measurable in the reads rather than invisible. Hold the air on for "
             f"{AIR_CHASE_S:g} s after the liquid has left to clear the route.",
             math.ceil((CONTENT_UL + PATH_UL[sub]) / F_FILL * 60.0 * MARGIN) + AIR_CHASE_S,
             reagent="air_chase", volume_ul=CONTENT_UL, flow_ul_min=F_FILL,
             ports=ports_for(M4, [f"in_s{n}"], [OUT4]))

    # steps 16 and 17: four passes through module 5, in the order they were collected
    for sub in PASSES:
        n = sub[-1]
        who = f"sublibrary {n} ({sub})"
        emit("load", "react_cx",
             f"Step 16, pass {n}: {who} arrives from module 4's out_sub, through tubing, at module 5's in_lib. "
             f"That is the module4_to_module5 hand-off: out_sub is capsule-passable and in_lib is the load "
             f"inlet. Four sublibraries will not fit react_cx together, so each gets its own pass and "
             f"keeps its own index.",
             math.ceil(DELIVERED_UL / F_LOAD * 60.0 * MARGIN) + OFFCHIP_S, reagent="pcr_product_capsules",
             volume_ul=DELIVERED_UL, flow_ul_min=F_LOAD,
             ports=ports_for(M5, ["in_lib"], [WASTE5]))
        emit("load", "bead_cx",
             f"Step 16, pass {n}: load magnetic beads into bead_cx through in_bead with the magnet ON. "
             f"Retention is magnetic only: the beads are about 1 um and no feature can be that small.",
             math.ceil(BEAD_LOAD_UL / F_BEADLOAD * 60.0 * MARGIN), reagent="bead_suspension",
             volume_ul=BEAD_LOAD_UL, flow_ul_min=F_BEADLOAD, ports=ports_for(M5, ["in_bead"], [WASTE5]))
        emit("release", "react_cx",
             f"Step 16, pass {n}: collagenase into react_cx through in_prot, all ports capped. It digests the "
             f"gelatin between crosslinks until the capsule dissolves. Proteinase K would leave these "
             f"capsules intact, and a chelator would stop collagenase, so none is carried in.",
             math.ceil((COLLAGENASE_UL + LINE_PROT) / F_DIG * 60.0 * MARGIN), reagent="collagenase_solution",
             ports=ports_for(M5, ["in_prot"]))
        emit("hold_temp", "react_cx",
             f"Step 16, pass {n}: hold react_cx at {DIGEST_C} C while the capsules dissolve and the DNA is "
             f"released into solution.",
             DIGEST_S, temp_c=DIGEST_C, ports=ports_for(M5))
        emit("exchange", "react_cx",
             f"Step 16, pass {n}: DILUTE, then capture. Dilution buffer is pushed through react_cx into "
             f"bead_cx at low flow, {DILUTION_WASHES} washes of {DILUTION_FOLD / DILUTION_WASHES:g} "
             f"react_cx volumes = {DILUTION_FOLD:g} in total. The dissolved shell carries about 10,000 "
             f"times more polymer than DNA, "
             f"and at that level bead binding fails, so the digest must be diluted before it meets the "
             f"beads. The DNA binds to the magnet-held beads as it passes; the liquid leaves by out_waste. "
             f"SWEEP: {DILUTION_FOLD:g} chamber volumes is past the {SWEEP_CV:.1f} the "
             f"{EXCHANGE_TARGET:.0%} exchange goal needs, so react_cx is provably cleared into the bead "
             f"chamber and no digest is left behind. The flow is a bead-binding kinetics placeholder, not "
             f"a shear limit.",
             exchange_s(DILUTION_WASHES, DILUTION_PER_WASH_UL, F_DIL), reagent="dilution_buffer",
             volume_ul=DILUTION_PER_WASH_UL, washes=DILUTION_WASHES, flow_ul_min=F_DIL,
             exchange_kind="sweep", ports=ports_for(M5, ["in_prot"], [WASTE5]))
        emit("exchange", "bead_cx",
             f"Step 16, pass {n}: buffer exchange on the bead pellet with an aqueous, ethanol-free buffer, "
             f"magnet ON. There is no ethanol wash and no drying on chip: a drying meniscus pulls a bead "
             f"about 100,000 times harder than the magnet and wrecks the pellet. SWEEP: "
             f"{EXCHANGE_WASHES} washes of {EXCHANGE_FRACTION:.2f} chamber volumes = {SWEEP_CV:.1f} in "
             f"total, which is what the {EXCHANGE_TARGET:.0%} exchange goal needs in a well-mixed "
             f"chamber. It used to be 2 volumes, which leaves {math.exp(-2):.0%} of the binding buffer on "
             f"the pellet and carries it into the eluate. The flow is a binding-kinetics placeholder, not "
             f"a shear limit.",
             exchange_s(EXCHANGE_WASHES, EXCHANGE_UL, F_EXCH), reagent="exchange_buffer",
             volume_ul=EXCHANGE_UL, washes=EXCHANGE_WASHES, flow_ul_min=F_EXCH,
             exchange_kind="sweep", ports=ports_for(M5, ["in_bead"], [WASTE5]))
        emit("exchange", "bead_cx",
             f"Step 16, pass {n}: elute in a small volume through in_bead, out_lib vented and out_waste "
             f"capped, magnet still ON so the beads stay put. PARTIAL: {ELUTE_FRACTION:.0%} of bead_cx's "
             f"{V_BEAD:g} uL, replaced on purpose and not a sweep. A small eluate is the whole point of "
             f"eluting - concentration is what the sequencer wants - so the one-chamber-volume floor does "
             f"not apply here; sweeping the chamber would dilute the library it is trying to recover.",
             exchange_s(1, ELUTE_UL, F_ELUTE), reagent="elution_buffer", volume_ul=ELUTE_UL, washes=1,
             flow_ul_min=F_ELUTE, exchange_kind="partial", ports=ports_for(M5, ["in_bead"], [LIB5]))
        emit("release", "bead_cx",
             f"Step 17, pass {n}: library {n} leaves through out_lib into a tube, chased with air to recover "
             f"the liquid left in the short out_lib channel.",
             300, reagent="elution_buffer", ports=ports_for(M5, ["in_bead"], [LIB5]))

    emit("release", "bead_cx",
         "Step 17, hand-off to the bench: mix the four libraries in a tube and do the ratio-based size "
         "selection that removes primer dimers there. It is off chip because it needs an ethanol wash and "
         "dry, which a chip cannot do.",
         900, reagent="ratio_size_selection", ports=ports_for(M5, [], [LIB5]))
    emit("release", "bead_cx",
         "Step 17, hand-off to the bench through out_lib: off-chip QC on the finished library (size "
         "distribution and quantity) before sequencing.",
         1800, reagent="off_chip_qc", ports=ports_for(M5, [], [LIB5]))

    return {
        "id": "library_out_v1",
        "name": "Sublibrary split, index PCR, release and cleanup, library out (steps 15-17)",
        "source": SOURCE,
        "placeholder": False,
        "description": (
            "Runs across two chips: module4_v1 for the interleaved split, the index PCR and the collection "
            "of the four sublibraries, then module5_v1 for the collagenase release, the bead cleanup and the "
            "library outlet. Built in the protocol "
            "sheet's order (PCR before release); published practice releases first, so that order is "
            "contested and the reasoning is in protocols/build_library_out.py. Four chambers are filled "
            "one at a time by an off-chip rotary valve in the order A B C D D C B A, twice, to keep the "
            "sublibraries even. The PCR is written out cycle by cycle. The amplified sublibraries then leave "
            "module 4 one at a time through out_sub along a shared collection spine that stays dry until "
            "amplification is over, each pushed by air rather than a chase liquid so nothing is diluted; "
            "p_vent is capped so the spine is one-way, and the worst-case carry-over from one pass to the next is "
            f"{CARRY_UL:g} uL, {CARRY_FRACTION:.1%} of a sublibrary. Each one then crosses to module 5's "
            "in_lib by tubing. Module 5 runs one pass per "
            "sublibrary, with an explicit dilution before the beads, no ethanol wash on chip, and "
            "ratio-based size selection left as an off-chip tube step. Reagent names are generic and the "
            "volumes, setpoints and cycle count are round placeholders to be fitted. Every volume is a "
            "fraction of its chamber's volume_ul, and every exchange declares what it is FOR in its "
            "exchange_kind field: the index primer and the PCR mix are FILLs into a part-air-filled "
            "chamber behind a capillary stop and are bounded above by the chamber, the dilution and the "
            "bead buffer exchange are SWEEPs sized from exchange.target_fraction, and the elution is a "
            "PARTIAL because a small eluate is the point. Neither chip holds a cell attached to anything, "
            "so shear.culture_ceiling_dyn_cm2 - which contracts/parameters.json reserves for chambers of "
            "adherent cells - applies to nothing here: the capsule steps are bounded by "
            "capsule.escape_pressure_kpa across a packed bed "
            f"({F_FILL:g} uL/min, against the {CULTURE_CEILING_FLOWS['sub_*']:g} uL/min the culture "
            "ceiling used to impose), and the bead steps run at kinetics placeholders to be fitted on "
            "real eluates."
        ),
        "steps": steps,
    }


def report() -> None:
    print("Interleaved fill order (rotary valve position, chamber, letter), pulse of "
          f"{PULSE_UL:g} uL at {F_FILL:g} uL/min:")
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

    print(f"Collection along the shared spine, in order, out through {OUT4} with {VENT4} capped:")
    print(f"  {'pass':<6}{'route':<52}{'route uL':>9}{'film uL':>9}{'from earlier uL':>17}")
    for i, s in enumerate(COLLECT_ORDER, start=1):
        print(f"  {i:<6}{' '.join(collect_path(s)):<52}{PATH_UL[s]:>9.3f}{SPINE_HOLDUP[s]:>9.3f}"
              f"{HOLDUP_FRACTION * shared_with_earlier(s):>17.3f}")
    print(f"  worst-case carry-over one pass hands the next: {CARRY_UL:g} uL against a "
          f"{DELIVERED_UL:.2f} uL sublibrary = {CARRY_FRACTION:.1%}. One-way spine, so a sublibrary can "
          f"only pick up\n  from one already collected, never from one still behind its own dry "
          f"capillary stop.\n")

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
        ("transfer: sub chamber hold-up", HOLDUP_SUB, "assumption; film and corners"),
        ("collection: spine film, worst pass (sub_1)", COLLECT_LOSS,
         f"air-chased, so the film not the bore of a {max(PATH_UL.values()):.2f} uL route"),
        ("transfer: off-chip tubing", OFFCHIP_LINE_UL, "assumption"),
        ("transfer: ch_in_lib", LINE_LIB, "fills with sample and stays"),
        ("dilution flush: react_cx hold-up", HOLDUP_REACT, "assumption; DNA left behind"),
        ("dilution flush: ch_react_bead", LINE_REACT_BEAD, "carries sample past the beads, no loss if bound"),
        ("elution: bead_cx hold-up", HOLDUP_BEAD, "assumption"),
        ("elution: ch_out_lib", LINE_OUT, "product left in the line unless chased with air"),
        ("reagent only: ch_in_prot", LINE_PROT, "collagenase and dilution line, no product"),
        ("reagent only: ch_in_bead", LINE_BEAD, "bead and elution line, no product"),
    ]
    print(f"  {'where':<46}{'uL':>7}  note")
    for name, v, note in rows:
        print(f"  {name:<46}{v:>7.2f}  {note}")
    print()
    print(f"  per sublibrary, uL: in chamber {CONTENT_UL:.2f} -> transfer loss {TRANSFER_LOSS:.2f} -> "
          f"delivered {DELIVERED_UL:.2f} -> eluted {ELUTE_UL:.1f}")
    print(f"  product per sublibrary {ELUTE_UL:.1f} - {HOLDUP_BEAD:.2f} hold-up - {LINE_OUT:.2f} line = "
          f"{PRODUCT_UL:.2f} uL; four sublibraries {4 * PRODUCT_UL:.1f} uL")
    lost = TRANSFER_LOSS + HOLDUP_REACT + HOLDUP_BEAD + LINE_OUT
    print(f"  sample-path dead volume per sublibrary {lost:.2f} uL against {PRODUCT_UL:.2f} uL of product "
          f"({lost / PRODUCT_UL:.1f}x)\n")

    print(f"Which bound applies to which chamber (mu = {MU_PA_S:g} Pa s, contracts/parameters.json "
          f"fluid.viscosity_37c_pa_s).")
    print(f"  No chamber on this path holds an adherent cell, so the {SHEAR_CEILING:g} dyn/cm2 culture "
          f"ceiling applies to none of them.")
    print(f"  {'route':<26}{'holds':<10}{'bound':<22}{'flow':>7}{'was':>7}  "
          f"{'chamber tau':>11}{'channel tau':>12}")
    rows = [
        ("fill, module 4", "capsules", "escape pressure", M4, F_FILL,
         CULTURE_CEILING_FLOWS["sub_*"], "sub_1", "ch_in_1"),
        ("collection, module 4", "capsules", "escape pressure", M4, F_FILL,
         CULTURE_CEILING_FLOWS["sub_*"], "sub_1", "ch_tap_1"),
        ("load, module 5", "capsules", "escape pressure", M5, F_LOAD,
         CULTURE_CEILING_FLOWS["react_cx"], "react_cx", "ch_in_lib"),
        ("collagenase in", "capsules", "escape pressure", M5, F_DIG,
         CULTURE_CEILING_FLOWS["react_cx"], "react_cx", "ch_in_prot"),
        ("dilution and capture", "beads", "binding kinetics (TODO)", M5, F_DIL, F_DIL,
         "bead_cx", "ch_in_prot"),
        ("buffer exchange", "beads", "binding kinetics (TODO)", M5, F_EXCH, F_EXCH,
         "bead_cx", "ch_in_bead"),
        ("elution", "beads", "elution kinetics (TODO)", M5, F_ELUTE, F_ELUTE,
         "bead_cx", "ch_out_lib"),
    ]
    for name, holds, bound, chip, q, was, cid, chid in rows:
        c = next(x for x in chip["chambers"] if x["id"] == cid)
        ch = next(x for x in chip["channels"] if x["id"] == chid)
        print(f"  {name:<26}{holds:<10}{bound:<22}{q:>7.2f}{was:>7.2f}  "
              f"{shear(q, c['size'][1], c['size'][2]):>11.4f}{shear(q, ch['width_mm'], ch['depth_mm']):>12.4f}")
    print(f"  escape-pressure bound: {FLOW_FRACTION:.0%} of {PRESSURE_MARGIN:.0%} of {ESCAPE_KPA:g} kPa "
          f"across a packed bed at {BED_RESISTANCE:g} kPa per uL/min = {F_FILL:g} uL/min. The 'was' column "
          f"is what the\n  inapplicable culture ceiling imposed; tau is printed for information only, not "
          f"as a limit.\n")
    print("Exchange sizing, by what each step is FOR:")
    for part in (build(),):
        for s in part["steps"]:
            if s["operation"] != "exchange":
                continue
            vc = next((c["volume_ul"] for c in M4["chambers"] + M5["chambers"] if c["id"] == s["target"]), None)
            total = s["volume_ul"] * (s["washes"] or 1)
            print(f"  step {s['step']:>2}  {s['exchange_kind']:<8}{s['target']:<10}{s['reagent']:<18}"
                  f"{s['volume_ul']:>7.2f} uL x{s['washes']}  = {total / vc:>5.2f} CV  "
                  f"leaves {math.exp(-total / vc):>7.2%}")
            if s["step"] > 40:
                break


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

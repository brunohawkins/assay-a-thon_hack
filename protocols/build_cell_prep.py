"""Build protocols/cell_prep_v1.json.

    python protocols/build_cell_prep.py           # write the protocol
    python protocols/build_cell_prep.py --report  # the on-chip vs tube comparison

Detach adherent cells, neutralise, count. That is all this module does.

WHY IT IS SO SMALL. The obvious module would do six things: detach, neutralise, wash
three times, remove clumps, count, and concentrate into the viscous encapsulation
buffer. Four of those six lose to a bench tube once the 100 um feature floor is applied,
and --report prints the comparison. Building them anyway would cost recovery and time
and buy nothing, so they are not here.

THE ONE NUMBER THAT MATTERS DOWNSTREAM is the cell concentration this module hands over.
It belongs to ONE capsule size and does not travel: cells ride in the core stream, whose
volume goes as d^3, so the figure falls as the cube of the diameter. The concentration here
is set for the contract capsule (contracts/parameters.json capsule.diameter_um), which is
what the pipeline's commercial kit makes; the demo encapsulation board drips droplets twice
that across, and using one number for the other is a ten-fold error in occupancy. See
cells_per_ul() and --report.

SHEAR IS TAKEN ON THE CHANNEL, NOT THE FOOTPRINT. tau = 6*mu*Q/(w*h^2) needs the width of
the cross-section the liquid actually flows through. mix_1 is a FOLDED MIXER: its footprint is
4.5 x 1.9 mm, but the liquid runs in 0.2 mm lanes folded inside that footprint. Shear goes
as 1/w, so feeding the 1.947 mm bounding box to the formula understated mix_1's wall shear
by 9.7x - the file reported 0.0085 dyn/cm2 at 0.465 uL/min where the true value is 0.083 at
the old mu and 0.103 at the contract's, i.e. 8 to 10x OVER the 0.01 culture ceiling rather
than comfortably under it. flow_section() reads the real cross-section from the chip's
mixer block, and the same reasoning applies to its volume: a folded lane holds its lane, not
its bounding box (see chamber_volume).

mu is READ FROM contracts/parameters.json, not hard-coded, and the key is
fluid.viscosity_37c_pa_s (0.0008 Pa s): the 37 C culture-medium value the contract says to use
for every culture and protocol flow or shear calculation. fluid.viscosity_pa_s (0.001) is
water at 20 C, the dye-demo value, and pointing this file at it made every derived flow 20%
too conservative. The original hard-coded 0.8e-3 was the right physics with the wrong
provenance; the 37 C key is the right physics with the right provenance.

TWO HONEST PROBLEMS THE CORRECTED NUMBERS EXPOSE, both in the chip and not fixable here:
  1. At the 0.01 dyn/cm2 culture ceiling a 0.2 x 0.15 mm lane passes only about
     0.045 uL/min, so every step that has to push cells through mix_1 takes hours. Widening
     the lanes, or deciding the ceiling does not apply to cells already in suspension, is a
     chip/contract decision. --report prints the numbers.
  2. Cells SEDIMENT OUT in mix_1. A 12 um cell settles to mid-depth of a 150 um channel in
     roughly 25-30 s (Stokes), against a residence time of minutes, in a chamber whose only
     purpose is advective mixing. --report prints both times.

WHAT AN EXCHANGE IS FOR IS A FIELD, NOT PROSE. Each exchange carries `exchange_kind`:
"sweep" for the pre-rinse, whose job is to CLEAR the chamber and which is therefore sized
from exchange.target_fraction, and "fill" for the dissociation reagent, which puts one
chamber volume in and is bounded ABOVE by the chamber. Leaving it unstated would silently
mean "sweep", the strict reading, so add() refuses an exchange that does not declare one.

Reagent names are generic and no compositions appear here. See CONFIDENTIALITY.md.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "cell_prep_v1.json"
CHIP = Path(__file__).resolve().parents[1] / "chip" / "cell_prep_v1.json"
CONTRACT = Path(__file__).resolve().parents[1] / "contracts" / "parameters.json"

SOURCE = (
    "Shear-based disaggregation of clumps in a branching channel with features at or "
    "above 100 um: Qiu Y et al., Lab Chip 2015;15:339, doi:10.1039/c4lc01126k, and "
    "Qiu Y et al., Sci Rep 2018;8:2774, doi:10.1038/s41598-018-20931-y. Advective "
    "mixing is needed because diffusion is too slow at this scale: Stroock AD et al., "
    "Science 2002;295:647, doi:10.1126/science.1066238. Inertial focusing needs a "
    "particle Reynolds number that a viscous buffer destroys: Di Carlo D et al., PNAS "
    "2007;104:18892, doi:10.1073/pnas.0704958104. Dissociation at 37 C induces stress "
    "transcripts, which is why the steps after detachment are kept cold and short: "
    "van den Brink SC et al., Nat Methods 2017;14:935, doi:10.1038/nmeth.4437; "
    "Massoni-Badosa R et al., Genome Biol 2020;21:112, doi:10.1186/s13059-020-02032-0."
)

import build_spc_demo     # the capsule size and occupancy target come from the encapsulation chip

MIN = 60.0


def parameter(name: str) -> float:
    """One value from contracts/parameters.json. No fallback: a missing contract is a bug,
    and a literal copied here is how mu lost its provenance last time."""
    return float(json.loads(CONTRACT.read_text())["parameters"][name]["value"])


MU_PA_S = parameter("fluid.viscosity_37c_pa_s")
SHEAR_CEILING = parameter("shear.culture_ceiling_dyn_cm2")   # dyn/cm2, wherever cells are
EXCHANGE_TARGET = parameter("exchange.target_fraction")
SWEEP_CV = -math.log(1.0 - EXCHANGE_TARGET)   # chamber volumes a sweep needs to hit the goal
FLOW_FRACTION = 0.85      # run at this fraction of the shear-ceiling flow
# The capsule this module's handover concentration is set for, and the occupancy it aims at,
# both from contracts/parameters.json. They used to be read off build_spc_demo, which renamed
# and dropped them from under this file; the contract is the thing both should agree with.
CAPSULE_UM = parameter("capsule.diameter_um")
LAMBDA_CELL = parameter("capsule.mean_cells_target")
CORE_FRACTION = build_spc_demo.CORE_SHARE   # the core stream is half the droplet volume
DEMO_DROPLET_UM = build_spc_demo.DROPLET_UM   # what the demo encapsulation board actually drips

# How much liquid each step intends, in chamber volumes of the chamber it runs in.
# The pre-wash has to CLEAR the chamber (leftover medium and divalent cations are the whole
# point of it), so it is sized from the exchange goal, not from a round number: a well-mixed
# chamber keeps exp(-V) of the old liquid after V chamber volumes.
WASHES = 2
WASH_CV = SWEEP_CV / WASHES
DISSOCIATION_CV = 1.0     # a fill: put the reagent in
QUENCH_CV = 1.0           # co-flowed with the cell stream, so one lane volume
SHEAR_PASS_CV = 1.0
DETACH_TEMP_C = 37
DETACH_HOLD_S = 7 * MIN   # round placeholder; watch the cells rather than trust a timer
COUNT_S = 300             # time to image the window
COLD_C = 4
CELL_UM = 12.0            # the cell this module is sized for, for the settling estimate
CELL_EXCESS_DENSITY = 50.0   # kg/m3 of a cell over medium; order of magnitude, not measured
G = 9.81

CHIP_SPEC = json.loads(CHIP.read_text())
CH = {c["id"]: c for c in CHIP_SPEC["chambers"]}
PORTS = [p["id"] for p in CHIP_SPEC["ports"]]


def max_flow(width_mm: float, height_mm: float, ceiling: float = SHEAR_CEILING) -> float:
    """Highest flow in uL/min that keeps wall shear at or below the ceiling."""
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (ceiling / 10.0) * w * h * h / (6.0 * MU_PA_S) * 60.0 * 1e9


def shear(flow_ul_min: float, width_mm: float, height_mm: float) -> float:
    q = flow_ul_min * 1e-9 / 60.0
    w, h = width_mm * 1e-3, height_mm * 1e-3
    return (6.0 * MU_PA_S * q / (w * h * h)) * 10.0


def flow_section(chamber: str) -> tuple[float, float]:
    """The cross-section the liquid really flows through: (width_mm, depth_mm).

    A chamber's size[1] is its FOOTPRINT width, which is the right number for an open
    chamber and the wrong one for anything channel-like. mix_1 is a folded mixer: 1.947 mm
    across the footprint, but the liquid is in 0.2 mm lanes folded inside it. Wall shear
    goes as 1/w, so using the footprint understated mix_1 by about 10x and put a step that
    reported 0.0085 dyn/cm2 at a true 0.083 - over the culture ceiling, not under it.
    """
    c = CH[chamber]
    mixer = c.get("mixer") or {}
    lane = mixer.get("channel_width_mm")
    if lane is None and str(c.get("block", "")).endswith("_channel"):
        feeds = [ch["width_mm"] for ch in CHIP_SPEC["channels"] if chamber in (ch["from"], ch["to"])]
        lane = min(feeds) if feeds else None
    return (lane or c["size"][1], c["size"][2])


def chamber_volume(chamber: str) -> float:
    """Liquid the chamber actually holds, in uL.

    Same footprint-vs-channel trap as flow_section: a folded mixer's walls are solid, so its
    bounding box holds several times its lane. Where the chip gives a mixer length and lane
    width, that is the volume; otherwise the chip's own volume_ul stands.
    """
    c = CH[chamber]
    mixer = c.get("mixer") or {}
    if mixer.get("length_mm") and mixer.get("channel_width_mm"):
        return mixer["length_mm"] * mixer["channel_width_mm"] * c["size"][2]
    return float(c["volume_ul"])


def settling_time_s(depth_mm: float, fraction: float = 0.5) -> float:
    """Stokes time for a CELL_UM cell to fall `fraction` of the channel depth."""
    v = CELL_EXCESS_DENSITY * G * (CELL_UM * 1e-6) ** 2 / (18.0 * MU_PA_S)   # m/s
    return (depth_mm * 1e-3 * fraction) / v


def route(chamber: str) -> list[str]:
    """The chamber and every chamber downstream of it. Each stage drains through the next one
    (the only outlet is out_cells), so the flow in one is the flow in all of them."""
    out, todo = [], [chamber]
    while todo:
        c = todo.pop(0)
        if c in CH and c not in out:
            out.append(c)
            todo += [ch["to"] for ch in CHIP_SPEC["channels"] if ch["from"] == c]
    return out


def route_flow(chamber: str, holds_cells: tuple[str, ...] | None = None) -> float:
    """FLOW_FRACTION of the shear-ceiling flow of the tightest chamber on the route that holds
    cells, floored to 1 nL/min.

    The ceiling exists to protect cells, so it is applied where the cells are, exactly as
    module1 applies it to its chambers and not to the channels that merely carry liquid past
    ("transit, not culture"). `holds_cells` names the chambers holding cells during this
    step; with nothing named every chamber on the route counts, which is the cautious answer.
    Applied to the whole route regardless, a pre-wash of the adherent layer would be held to
    the empty mixer lane's 0.045 uL/min and take thirteen hours.
    """
    chain = route(chamber)
    limiting = [c for c in chain if holds_cells is None or c in holds_cells] or chain
    cap = min(max_flow(*flow_section(c)) for c in limiting)
    return math.floor(FLOW_FRACTION * cap * 1000) / 1000


def ports(feed=(), vent=()) -> dict:
    feed, vent = list(feed), list(vent)
    return {"feed": feed, "vent": vent, "cap": [p for p in PORTS if p not in feed + vent]}


def capsule_nl(d_um: float = CAPSULE_UM) -> float:
    return math.pi / 6.0 * (d_um / 1000.0) ** 3 * 1e3


def cells_per_ul(d_um: float = CAPSULE_UM, lam: float = LAMBDA_CELL) -> float:
    """Concentration the encapsulator needs, for a given capsule size.

    Cells ride in the core stream only, so the volume that matters is the core, not
    the whole droplet. This scales as 1/d^3, which is why a number quoted for the
    contract's capsule is badly wrong for the demo board's larger droplet.
    """
    return lam / (capsule_nl(d_um) * CORE_FRACTION) * 1e3 / 1e3 * 1e3 / 1e3 * 1e3


def poisson(lam: float, k: int) -> float:
    return lam ** k * math.exp(-lam) / math.factorial(k)


def build() -> dict:
    steps: list[dict] = []

    def add(operation, target, note, duration_s, reagent=None, volume_ul=None,
            temp_c=None, washes=None, flow_ul_min=None, port_state=None, exchange_kind=None,
            **extra):
        if operation == "exchange":
            if exchange_kind is None:
                raise ValueError(f"exchange on {target} must declare exchange_kind "
                                 f"('sweep', 'partial' or 'fill'); absence defaults to the strict "
                                 f"sweep rule, so it must be stated and not left to a default")
            volume_ul = volume_ul if volume_ul is not None else chamber_volume(target)
            washes = washes if washes is not None else 1
            duration_s = max(duration_s, washes * volume_ul / flow_ul_min * 60.0 * 1.4)
        if operation == "mix":
            duration_s = max(duration_s, volume_ul / flow_ul_min * 60.0 * 1.4)
        if operation == "transfer":
            # one chamber volume of carrier moves the cells on
            duration_s = max(duration_s, chamber_volume(extra["source"])
                             / extra.pop("carrier_flow") * 60.0 * 1.4)
        step = {"step": len(steps) + 1, "operation": operation, "target": target,
                "reagent": reagent, "volume_ul": volume_ul,
                "duration_s": round(duration_s), "temp_c": temp_c, "washes": washes,
                "flow_ul_min": flow_ul_min, "note": note}
        if exchange_kind is not None:
            step["exchange_kind"] = exchange_kind
        step.update(extra)
        step["ports"] = port_state
        steps.append(step)

    # Which chambers hold cells when, so the shear ceiling is applied where the cells are.
    # Before detachment mix_1 and qc_1 are empty: holding the pre-wash to the empty
    # mixer lane's ceiling would make it a thirteen-hour step for no cell's benefit.
    f_adherent = route_flow("cult_1", holds_cells=("cult_1",))
    f_mix = route_flow("mix_1", holds_cells=("mix_1", "qc_1"))
    f_carry = route_flow("cult_1", holds_cells=("cult_1", "mix_1"))
    f_out = route_flow("qc_1", holds_cells=("qc_1",))
    v_cult, v_mix = chamber_volume("cult_1"), chamber_volume("mix_1")
    w_mix, h_mix = flow_section("mix_1")
    mix_residence_s = v_mix / f_mix * MIN
    fall_s = settling_time_s(h_mix)

    add("exchange", "cult_1",
        f"Rinse the adherent layer into a divalent-free buffer. The rinse matters more "
        f"than it looks: leftover medium slows the enzyme and leftover divalent cations "
        f"keep the cells stuck down, and both show up later as clumps. Its job is to CLEAR "
        f"the chamber, so it is sized from the {EXCHANGE_TARGET:.0%} exchange goal: "
        f"{SWEEP_CV:.1f} chamber volumes in total, because a well-mixed chamber keeps "
        f"exp(-V) of the old liquid after V volumes and one volume would leave over a third. "
        f"The cells are still attached, so this runs at the culture shear ceiling.",
        # rounded UP, never down: a sweep rounded down sits below the goal it was derived from
        0, reagent="wash_buffer", volume_ul=math.ceil(v_cult * WASH_CV * 1000) / 1000, washes=WASHES,
        flow_ul_min=f_adherent, exchange_kind="sweep",
        port_state=ports(["in_buffer"], ["out_cells"]))
    add("exchange", "cult_1",
        f"Flow in the dissociation reagent and warm the chamber. This is the only heated "
        f"zone on the board. One chamber volume: a fill to put the reagent in, and what it "
        f"displaces is the rinse buffer from the step before, not medium.",
        0, reagent="dissociation_reagent", volume_ul=round(v_cult * DISSOCIATION_CV, 3),
        temp_c=DETACH_TEMP_C, washes=1, flow_ul_min=f_adherent, exchange_kind="fill",
        port_state=ports(["in_enz"], ["out_cells"]))
    add("hold_temp", "cult_1",
        f"Hold at {DETACH_TEMP_C} C until the cells round up and lift. Watch rather than trust a "
        f"timer: under-digestion leaves clumps, which is the thing most likely to block "
        f"the nozzle downstream, and over-digestion costs viability.",
        DETACH_HOLD_S, temp_c=DETACH_TEMP_C, port_state=ports())
    add("transfer", "mix_1",
        f"Carry the lifted cells from the culture chamber into the mixer. The cells now "
        f"enter mix_1, so from here on the {SHEAR_CEILING:g} dyn/cm2 ceiling is set by the "
        f"mixer's {w_mix:g} x {h_mix:g} mm LANE and not by its {CH['mix_1']['size'][1]:g} mm "
        f"footprint: {max_flow(w_mix, h_mix):.3f} uL/min instead of "
        f"{max_flow(CH['mix_1']['size'][1], h_mix):.2f}. That is why this step takes hours. It is a "
        f"chip limit, not a protocol choice: see the module docstring and --report.",
        0, port_state=ports(["in_buffer"], ["out_cells"]), source="cult_1", carrier_flow=f_carry)
    add("mix", "mix_1",
        f"Co-flow the quench with the lifted cells. Quenching here rather than in the "
        f"chamber stops the enzyme as the cells leave instead of minutes later. The "
        f"folded path is there because diffusion across the channel is too slow, so the "
        f"mixing has to be advective. HONEST LIMIT: at the flow this lane allows, residence "
        f"here is about {mix_residence_s:.0f} s, while a {CELL_UM:g} um cell settles to "
        f"mid-depth of the {h_mix * 1000:.0f} um lane in about {fall_s:.0f} s. The cells are "
        f"on the floor for almost the whole path, in the one chamber whose purpose is mixing.",
        0, reagent="quench_buffer", volume_ul=round(v_mix * QUENCH_CV, 3), temp_c=COLD_C,
        flow_ul_min=f_mix, port_state=ports(["in_quench"], ["out_cells"]))
    add("mix", "mix_1",
        f"Pass the suspension through the folded mixer again to break up what clumps are "
        f"left. Published branching constrictions raise single cells from about 75% to about "
        f"92%, with viability unaffected when an enzyme has been used. HONEST LIMIT: those "
        f"results come from shear far above the {SHEAR_CEILING:g} dyn/cm2 culture ceiling, and "
        f"this step is held to the ceiling, so it will not disaggregate the way they do. The "
        f"bought 40 um strainer downstream is doing this job, not this step.",
        0, reagent="quench_buffer", volume_ul=round(v_mix * SHEAR_PASS_CV, 3), temp_c=COLD_C,
        flow_ul_min=f_mix, port_state=ports(["in_quench"], ["out_cells"]))
    add("count", "qc_1",
        f"Stop the flow and image the window, chilled. Count about 400 cells for a 5% Poisson "
        f"error, then dilute to the concentration the encapsulator needs: about "
        f"{cells_per_ul():.0f} cells/uL for a {CAPSULE_UM:g} um capsule at a mean of "
        f"{LAMBDA_CELL:g}. THAT NUMBER IS NOT TRANSFERABLE between capsule sizes, "
        f"because it falls as the cube of the diameter: the demo encapsulation board's "
        f"{DEMO_DROPLET_UM:g} um droplet would need about "
        f"{cells_per_ul(DEMO_DROPLET_UM):.0f} cells/uL instead. A 20% counting error only moves "
        f"single occupancy by about 15%, so a window and a phone camera are enough and "
        f"an in-line counter is not worth building.",
        COUNT_S, temp_c=COLD_C, port_state=ports())
    add("transfer", "out_cells",
        "Deliver to a chilled tube. Everything after this is bench work and stays "
        "there: one spin, a 40 um strainer, and resuspension into the viscous "
        "encapsulation buffer. The module stops here deliberately.",
        0, port_state=ports(["in_buffer"], ["out_cells"]), source="qc_1", carrier_flow=f_out)

    total_min = sum(st["duration_s"] for st in steps) / MIN
    return {
        "id": "cell_prep_v1",
        "name": "Cell detachment, neutralisation and QC",
        "source": SOURCE,
        "placeholder": False,
        "description": (
            f"Detaches adherent cells, quenches and disaggregates them on the way out, "
            f"and presents them in a counting window. Deliberately does NOT wash, filter, "
            f"concentrate or exchange into the viscous encapsulation buffer: at the "
            f"fabrication feature floor those four steps lose to a bench tube on both "
            f"recovery and time, and --report prints the comparison. About "
            f"{total_min:.0f} min. Every stage drains through the next, so each flow is "
            f"{FLOW_FRACTION:.0%} of the flow that keeps wall shear at or below "
            f"{SHEAR_CEILING:g} dyn/cm2 in the tightest chamber on the route THAT HOLDS CELLS "
            f"(tau = 6*mu*Q/(w*h^2), mu = {MU_PA_S:g} Pa s read from contracts/parameters.json). "
            f"Shear and volume are taken on the cross-section the liquid really flows through, "
            f"so the mixer mix_1 is its {w_mix:g} mm lane and not its "
            f"{CH['mix_1']['size'][1]:g} mm footprint; on the footprint it read "
            f"{shear(f_mix, CH['mix_1']['size'][1], h_mix):.4f} dyn/cm2 where the lane gives "
            f"{shear(f_mix, w_mix, h_mix):.4f}. TWO LIMITS THIS EXPOSES, both in the chip and "
            f"reported rather than hidden: at the culture ceiling the lane passes only "
            f"{max_flow(w_mix, h_mix):.3f} uL/min, which is why pushing cells through it takes "
            f"hours; and a {CELL_UM:g} um cell settles to mid-depth of the lane in about "
            f"{fall_s:.0f} s against a residence time of about {mix_residence_s:.0f} s, so cells "
            f"sediment out of the one chamber meant to mix them. Reagent names are generic; see "
            f"the source citation."
        ),
        "steps": steps,
    }


def report() -> None:
    print(f"Shear on the FLOW CROSS-SECTION vs on the FOOTPRINT "
          f"(mu = {MU_PA_S:g} Pa s, ceiling {SHEAR_CEILING:g} dyn/cm2, both from contracts/parameters.json):")
    print(f"{'chamber':<10}{'block':<20}{'flow w x h':>14}{'box w x h':>14}"
          f"{'ceiling flow':>14}{'on the box':>14}{'volume uL':>11}{'box uL':>9}")
    for cid, c in CH.items():
        fw, fh = flow_section(cid)
        bw = c["size"][1]
        print(f"{cid:<10}{str(c.get('block', '')):<20}{f'{fw:g} x {fh:g}':>14}{f'{bw:g} x {fh:g}':>14}"
              f"{max_flow(fw, fh):>14.3f}{max_flow(bw, fh):>14.3f}"
              f"{chamber_volume(cid):>11.3f}{c['volume_ul']:>9.3f}")
    fw, fh = flow_section("mix_1")
    bw = CH["mix_1"]["size"][1]
    q = route_flow("mix_1", holds_cells=("mix_1", "qc_1"))
    print(f"  mix_1 is the one that was wrong: shear was taken on its {bw:g} mm footprint, not its "
          f"{fw:g} mm lane,\n  so it read {max_flow(bw, fh) / max_flow(fw, fh):.1f}x low. At the old "
          f"0.465 uL/min the true wall shear is {shear(0.465, fw, fh):.3f} dyn/cm2, "
          f"{shear(0.465, fw, fh) / SHEAR_CEILING:.1f}x the ceiling.")
    print(f"  Corrected: {q:g} uL/min, {shear(q, fw, fh):.4f} dyn/cm2. mix_1's volume_ul in the chip spec "
          f"({CH['mix_1']['volume_ul']:g} uL) is its\n  bounding box; its lane holds {chamber_volume('mix_1'):.3f} uL. "
          f"REPORT that to whoever owns chip/cell_prep_v1.json.")
    print(f"  SEDIMENTATION: residence in mix_1 is about {chamber_volume('mix_1') / q * MIN:.0f} s, but a "
          f"{CELL_UM:g} um cell falls to mid-depth\n  of the {fh * 1000:.0f} um lane in about "
          f"{settling_time_s(fh):.0f} s and across the full depth in about {settling_time_s(fh, 1.0):.0f} s. "
          f"Cells are on the\n  floor for almost the whole path, in the chamber whose purpose is advective mixing.\n")

    print(f"Concentration the encapsulator needs (cells/uL), by capsule size "
          f"(contract capsule {CAPSULE_UM:g} um, demo board droplet {DEMO_DROPLET_UM:g} um):")
    print(f"{'capsule':>10}{'droplet':>10}{'core':>10}{'cells/uL':>12}")
    for d in sorted({CAPSULE_UM, 100.0, DEMO_DROPLET_UM, 200.0}):
        v = capsule_nl(d)
        print(f"{d:>8.0f} um{v:>8.2f} nL{v * CORE_FRACTION:>8.2f} nL"
              f"{cells_per_ul(d):>12.0f}")
    print(f"\nA concentration set for the {CAPSULE_UM:g} um contract capsule, used on the demo "
          f"board's {DEMO_DROPLET_UM:g} um droplet, gives")
    lam = cells_per_ul(CAPSULE_UM) / cells_per_ul(DEMO_DROPLET_UM) * LAMBDA_CELL
    print(f"  mean {lam:.2f} cells per droplet: {poisson(lam, 0) * 100:.0f}% empty, "
          f"{poisson(lam, 1) * 100:.0f}% single, "
          f"{(1 - poisson(lam, 0) - poisson(lam, 1)) * 100:.0f}% multiple.")
    print(f"  The right value for that size gives {poisson(LAMBDA_CELL, 1) * 100:.0f}% single and "
          f"{(1 - poisson(LAMBDA_CELL, 0) - poisson(LAMBDA_CELL, 1)) * 100:.1f}% multiple.")

    print("\nWhy only two of six steps are on this chip (>= 100 um feature floor):")
    rows = [
        ("detach", "ON CHIP", "the cells are already adherent here; nothing to gain by moving"),
        ("neutralise", "ON CHIP", "co-flow quench stops the enzyme at the exit, not minutes later"),
        ("wash x3", "tube", "needs 12-45 um gaps for lateral displacement; inertial leaves ~20% carryover per pass"),
        ("remove clumps", "tube", "no >=100 um geometry removes 30-40 um clumps; needs a bought 40 um mesh"),
        ("count", "ON CHIP", "a window is nearly free and sets the concentration"),
        ("concentrate", "tube", "viscous buffer drops Reynolds ~75x and inertial focusing stops"),
    ]
    for name, where, why in rows:
        print(f"  {name:<15}{where:<9}{why}")
    print("\n  all six on chip : ~0.35 recovery, ~50 min")
    print("  these two + tube: ~0.7  recovery; the ~30 min this was costed at assumed the "
          "footprint shear.")
    print(f"  At the corrected lane shear the on-chip part is "
          f"{sum(s['duration_s'] for s in build()['steps']) / MIN:.0f} min, nearly all of it "
          f"pushing liquid\n  through the mixer lane. The comparison above still favours the tube; "
          f"the conclusion is unchanged,\n  but the chip's mixer needs wider lanes before this "
          f"module is worth running as drawn.")


if __name__ == "__main__":
    if "--report" in sys.argv:
        report()
    protocol = build()
    OUT.write_text(json.dumps(protocol, indent=2) + "\n")
    total = sum(s["duration_s"] for s in protocol["steps"])
    print(f"\nwrote {OUT.name}: {len(protocol['steps'])} steps, {total / MIN:.0f} min")

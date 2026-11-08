"""One sizer per block id in contracts/blocks.json. This is where the prose became data.

A block audit found that every block with real internal geometry - the weir crest, the
comb teeth, the serpentine length, the droplet nozzle - was specified only in English, so
nothing downstream could build it. Each sizer below derives that geometry from the Need
and the contracts, emits it as `Feature` records and chip-spec entries, and raises rather
than return a part that breaks a floor.

Four things the sizers are careful about, because each one has already caused a bug:

1. SHEAR IS ON THE LANE WIDTH. Every call to physics.wall_shear_dyn_cm2 passes the real
   wetted width across the flow, and `physics["shear_measured_on"]` says which dimension
   that was, so a reviewer can check it.
2. A PLAIN SERPENTINE DOES NOT MIX. At Re ~ 1e-2 there is no secondary flow; folding a
   channel multiplies residence time, not contact. need.mix=True on a serpentine raises
   and names herringbone_mixer.
3. phase_guide IS A CAPILLARY STOP, flow_restrictor SETS RESISTANCE. An audit found the
   two swapped. The phase guide sizer returns no resistance and `stops_flow: True`; the
   restrictor returns a resistance and `stops_flow: False`. Neither can stand in for the
   other.
4. z IS NOT IN-PLANE. The weir gap, the crest height and the herringbone grooves are
   depth features and are recorded through Floors.z(), which deliberately does not test
   them against the 200 um lateral floor.
"""

from __future__ import annotations

import math
from typing import Callable

from assay2chip.lib import contracts
from assay2chip.parts import derived, physics
from assay2chip.parts.geometry import (
    Ctx, Expansion, Feature, Load, Need, Opening, PartSizingError, VolumeRecord,
    box_volume_ul, chamber_entry, channel_entry, cylinder_volume_ul, fold_into_lanes,
    min_chamber_volume_ul, min_span_mm, pillared_volume_ul, r, r_up, serpentine_volume_ul,
    snake, weir_chamber_volume_ul,
)

# Geometry taken from a built precedent in this repo rather than from a contract. Not a
# contract value and not a guess: cited, and carried into PartGeometry.provenance.
PRECEDENT = {
    "comb_tooth_length_mm": (1.5, "chip/module6_v1.json comb_in_n (teeth 0.2 x 1.5 mm)"),
    "shm_groove_depth_fraction": (
        0.25, "Stroock 2002 Science staggered herringbone mixer: groove depth 0.17-0.3 x "
              "channel height, grooves at 45 deg, 6 per half cycle, groove pitch ~ w/2"),
    "shm_grooves_per_half_cycle": (6, "Stroock 2002 Science"),
}


# --- shared helpers ----------------------------------------------------------

def lane_width_mm(ctx: Ctx, what: str) -> tuple[float, list[str]]:
    """The narrowest legal lane for this need, and every reason it is that wide.

    Three independent lower bounds, and the widest wins: the in-plane fabrication floor,
    the cargo (1.5 x capsule diameter, or the cell-outlet minimum), and the shear ceiling
    inverted at this flow and depth. Returning the reasons means a 3 mm wide chamber can
    be explained instead of just trusted.
    """
    width = contracts.floor("min_open_channel_mm")
    why = [f"min_open_channel_mm {width:g} mm (in-plane floor)"]
    ctx.cite("contracts/blocks.json fabrication_floors.min_open_channel_mm")

    cargo = ctx.particle_floor_mm()
    if cargo > width + 1e-12:
        width = cargo
        why.append(f"{ctx.need.particles} cargo needs {cargo:.4g} mm")

    ceiling = ctx.ceiling.ceiling_dyn_cm2
    if ceiling is not None:
        ctx.need.require("flow_ul_min")
        shear_w = physics.lane_width_for_shear_mm(
            ctx.need.flow_ul_min, ctx.depth_mm, ctx.mu, ceiling)
        ctx.cite(ctx.ceiling.source)
        if shear_w > width + 1e-12:
            width = shear_w
            why.append(f"{ctx.ceiling.shear_class} shear ceiling {ceiling:g} dyn/cm2 "
                       f"needs a {shear_w:.4g} mm lane at {ctx.need.flow_ul_min:g} uL/min")
            # Rounded AWAY from the ceiling: nearest-rounding can land a hair narrow and
            # put the shear fractionally over a limit this width was derived from.
            return r_up(width), why
    return r(width), why


def shear_report(ctx: Ctx, lane_mm: float, what: str) -> dict:
    """Wall shear on the real lane width, checked against whichever ceiling applies."""
    out: dict = {"shear_measured_on": what, "lane_width_mm": r(lane_mm),
                 "shear_class": ctx.ceiling.shear_class,
                 "bound_by": ctx.ceiling.bound_by,
                 "viscosity_pa_s": ctx.mu, "viscosity_key": ctx.viscosity.key}
    if ctx.need.flow_ul_min is None:
        out["wall_shear_dyn_cm2"] = None
        out["note"] = "no flow given, so shear could not be evaluated"
        return out
    tau = physics.wall_shear_dyn_cm2(ctx.need.flow_ul_min, lane_mm, ctx.depth_mm, ctx.mu)
    out["flow_ul_min"] = ctx.need.flow_ul_min
    out["wall_shear_dyn_cm2"] = round(tau, 6)
    out["ceiling_dyn_cm2"] = ctx.ceiling.ceiling_dyn_cm2
    out["mean_velocity_mm_s"] = round(
        physics.mean_velocity_mm_s(ctx.need.flow_ul_min, lane_mm, ctx.depth_mm), 4)
    out["reynolds"] = round(
        physics.reynolds(ctx.need.flow_ul_min, lane_mm, ctx.depth_mm, ctx.mu,
                         derived.fluid_density_kg_m3()), 5)
    out["fluid_density_kg_m3"] = derived.fluid_density_kg_m3()
    ctx.assume(derived.fluid_density_assumption())
    ctx.cite(f"contracts/parameters.json {derived.FLUID_DENSITY_KEY}")
    if ctx.ceiling.ceiling_dyn_cm2 is not None and tau > ctx.ceiling.ceiling_dyn_cm2 * (1 + 1e-9):
        raise PartSizingError(
            ctx.block,
            f"wall shear on the {what} is {tau:.4g} dyn/cm2 at {ctx.need.flow_ul_min:g} "
            f"uL/min, above the {ctx.ceiling.shear_class} ceiling "
            f"{ctx.ceiling.ceiling_dyn_cm2:g} dyn/cm2. Widen the lane to at least "
            f"{physics.lane_width_for_shear_mm(ctx.need.flow_ul_min, ctx.depth_mm, ctx.mu, ctx.ceiling.ceiling_dyn_cm2):.4g} "
            f"mm or drop the flow.")
    if ctx.ceiling.pressure_ceiling_kpa is not None:
        out["pressure_ceiling_kpa"] = ctx.ceiling.pressure_ceiling_kpa
        out["pressure_ceiling_note"] = (
            "capsules are bounded by escape pressure, not by wall shear")
    return out


def _resolve_load(ctx: Ctx) -> tuple[float, float | None, str, str, list[str]]:
    """Turn a unitops.json `load` into a volume (and a floor area, when that is the point).

    Returns (volume_ul, area_mm2 or None, basis, source, why). Nothing here invents a
    number: where the load is short of one, the contract default is used and SAID so, via
    source='contract_default', which is what S6 turns into an `unresolved` question.
    """
    need = ctx.need
    load = need.load
    if load is None or load.basis == "none":
        if need.volume_ul is not None:
            return (float(need.volume_ul), None, "volume_ul", "derived",
                    ["Need.volume_ul as given"])
        if need.residence_s is not None and need.flow_ul_min is not None:
            ctx.cite("volume from residence time: V = Q t")
            return (physics.residence_volume_ul(need.flow_ul_min, need.residence_s), None,
                    "flow_through", "derived",
                    [f"V = Q t at {need.flow_ul_min:g} uL/min for {need.residence_s:g} s"])
        raise PartSizingError(
            ctx.block,
            "needs a load (contracts/schemas/unitops.schema.json), or volume_ul, or "
            "residence_s together with flow_ul_min (V = Q t). The paper's tube volume is "
            "NOT a substitute - record the gap in `unresolved` instead "
            "(assay2chip/README.md, 'Bench volumes do not transfer')")

    why: list[str] = []
    source = load.source

    if load.basis == "adherent_area":
        area = load.area_mm2
        if area is None:
            if load.cells is None:
                raise PartSizingError(
                    ctx.block,
                    "load.basis is adherent_area but neither area_mm2 nor cells is given, "
                    "and cell count is the one thing that does survive from the paper. "
                    "Raise it as an `unresolved` question rather than sizing from a tube "
                    "volume.")
            density = load.surface_density_per_mm2
            if density is None:
                density = float(contracts.param("culture.surface_density_per_mm2"))
                source = "contract_default"
                ctx.cite("contracts/parameters.json culture.surface_density_per_mm2")
                lo, hi = contracts.param_range("culture.surface_density_per_mm2")
                ctx.assume(
                    f"culture.surface_density_per_mm2 = {density:g} cells/mm2 (band "
                    f"{lo:g}-{hi:g}), marked assumption: true. The paper gave no seeding "
                    f"density, so the floor area rests on a contract default and the area "
                    f"moves {hi / lo:g}x across the band.")
                why.append(f"seeding density defaulted to {density:g} cells/mm2 "
                           f"(contract default, flagged)")
            area = load.cells / density
            why.append(f"{load.cells:g} cells / {density:g} cells/mm2 = {area:.4g} mm2 "
                       f"of floor")
        else:
            why.append(f"{area:.4g} mm2 of floor as given")
        # Floor area is the target; the volume is whatever that area holds at this depth,
        # and the solver keeps the FREE area on target when pillars displace some of it.
        return (area * ctx.depth_mm, area, "adherent_area", source, why)

    if load.basis == "packed_volume":
        packed = load.packed_volume_ul
        if packed is None:
            if load.cells is None:
                raise PartSizingError(
                    ctx.block,
                    "load.basis is packed_volume but neither packed_volume_ul nor a count "
                    "is given; a bed volume cannot be guessed from a tube volume")
            d_um = ctx.particle_um or contracts.capsule_diameter_um()
            fraction = float(contracts.param("capsule.packing_fraction"))
            ctx.cite("contracts/parameters.json capsule.packing_fraction",
                     "contracts/parameters.json capsule.diameter_um")
            one_ul = math.pi / 6.0 * (d_um / 1000.0) ** 3
            packed = load.cells * one_ul / fraction
            why.append(f"{load.cells:g} x {d_um:g} um spheres at packing fraction "
                       f"{fraction:g} = {packed:.4g} uL of bed")
        else:
            why.append(f"{packed:.4g} uL of bed as given")
        return (packed, None, "packed_volume", source, why)

    if load.basis == "suspension_volume":
        if load.cells is None or load.suspension_density_per_ul is None:
            raise PartSizingError(
                ctx.block,
                "load.basis is suspension_volume but cells and suspension_density_per_ul "
                "are not both given; the suspension density is what converts a count into "
                "a volume, and the paper's tube volume does not")
        vol = load.cells / load.suspension_density_per_ul
        why.append(f"{load.cells:g} cells / {load.suspension_density_per_ul:g} per uL = "
                   f"{vol:.4g} uL of suspension")
        return (vol, None, "suspension_volume", source, why)

    # flow_through: nothing is held, so the stream itself sets the size.
    residence = load.residence_s if load.residence_s is not None else ctx.need.residence_s
    if residence is None or ctx.need.flow_ul_min is None:
        raise PartSizingError(
            ctx.block,
            "load.basis is flow_through, so this part is sized from flow and residence "
            "time - it needs load.residence_s (or Need.residence_s) and flow_ul_min")
    ctx.cite("volume from residence time: V = Q t")
    why.append(f"V = Q t at {ctx.need.flow_ul_min:g} uL/min for {residence:g} s")
    return (physics.residence_volume_ul(ctx.need.flow_ul_min, residence), None,
            "flow_through", source, why)


def required_volume_ul(ctx: Ctx) -> float:
    """What this part must hold, from the load (preferred) or from volume_ul.

    A volume BELOW the smallest buildable chamber is ROUNDED UP to it, with a note, and
    the built volume is what comes back and what the part reports. It is never refused:
    "wash in 500 uL" describes a tube, and dropping the tube can leave 0.012 uL, which is
    a real step that needs a real chamber. Refusal is reserved for a need the floors
    genuinely cannot meet - a sub-200 um in-plane feature, a crest taller than its layer.
    """
    volume, area, basis, source, why = _resolve_load(ctx)
    requested = volume
    floor_volume = min_chamber_volume_ul(ctx.depth_mm)
    rounded = False
    if volume < floor_volume - 1e-12:
        rounded = True
        span = min_span_mm()
        why.append(
            f"ROUNDED UP from {requested:.4g} uL to {floor_volume:.4g} uL, the smallest "
            f"chamber the floors allow ({span:g} x {span:g} mm at {ctx.depth_mm:g} mm). "
            f"The assay's number is kept in volume_requested_ul.")
        ctx.risk(
            f"the load resolved to {requested:.4g} uL, below the smallest buildable "
            f"chamber ({floor_volume:.4g} uL at the {span * 1000:g} um in-plane floor), so "
            f"the part holds {floor_volume / requested:.0f}x what was asked. Dilution and "
            f"reagent RATIOS must be recomputed for the built volume, not the paper's "
            f"(assay2chip/README.md, 'Bench volumes do not transfer').")
        volume = floor_volume
        if area is not None:
            area = volume / ctx.depth_mm
    ctx.volume = VolumeRecord(volume, requested, basis, source, rounded, floor_volume,
                              area, tuple(why))
    return volume


def check_particle_depth(ctx: Ctx, height_mm: float, what: str, *, culture: bool = False) -> None:
    """Depth against the cargo. A capsule taller than the layer does not go in it.

    For anything where culture happens the diameter that matters is the SWOLLEN one, which
    is the same rule tools/check_chip_spec.py applies.
    """
    kind = ctx.need.particles
    if kind == "none":
        return
    d_um = ctx.particle_um or contracts.capsule_diameter_um()
    # Swelling is a GelMA capsule property (capsule.swell_factor). Beads and cells do not
    # swell, so applying it to them would refuse parts for a reason that is not real.
    if culture and kind == "capsules":
        d_um = d_um * contracts.capsule_swell_factor()
        ctx.cite("contracts/parameters.json capsule.swell_factor")
    if height_mm * 1000.0 < d_um:
        raise PartSizingError(
            ctx.block,
            f"the {what} is {height_mm * 1000:g} um deep but a {'swollen ' if culture else ''}"
            f"{kind[:-1] if kind.endswith('s') else kind} is {d_um:g} um. The etch depth is "
            f"capped at {contracts.max_depth_mm() * 1000:g} um, so the lever is particle "
            f"size, not depth.")
    if height_mm * 1000.0 < 1.5 * d_um:
        ctx.risk(f"the {what} is only {height_mm * 1000 / d_um:.2g} particle diameters "
                 f"deep, so the cargo runs as a near-monolayer and can bridge")


def sedimentation_report(ctx: Ctx, residence_s: float | None, what: str, *,
                         intended: bool = False) -> dict:
    """Does the cargo reach the floor before it reaches the far end?

    A 12 um cell falls the full 0.3 mm in about 51 s at 37 C, so any particle-carrying
    LINE held longer than that is a line that deposits its cargo - that is a risk, and the
    one the brief asks for. In a chamber that is meant to hold the cargo (a trap, a culture
    field, a metering pocket) the same arithmetic is a design fact, so `intended=True`
    reports the number without crying wolf.

    Excess density is LINEAR in the answer and the contract bands it per particle kind, so
    the END OF THE BAND is chosen by the question and recorded in the output: `intended`
    means this part relies on settling to retain, which takes the min (slowest settling,
    so retention is never over-promised); otherwise it is a loss check, which takes the
    max (fastest settling, so a depositing line is never missed).

    For capsules the NOMINAL estimate is also cross-checked against the 50-210 um/s band
    blocks.json states for a sedimentation sump, since every excess density in the
    contract is still marked assumption: true.
    """
    if ctx.need.particles == "none" or ctx.particle_um is None:
        return {}
    purpose = "retain" if intended else "loss"
    dens = derived.excess_density(ctx.need.particles, purpose)
    t_settle = physics.settling_time_s(ctx.depth_mm, ctx.particle_um, ctx.mu, dens.value)
    v_settle = physics.settling_velocity_um_s(ctx.particle_um, ctx.mu, dens.value)
    ctx.assume(dens.assumption)
    ctx.cite(dens.source)
    out = {
        "particle_um": ctx.particle_um,
        "excess_density_kg_m3": dens.value,
        "excess_density_key": dens.key,
        "excess_density_end": dens.end,
        "excess_density_band_kg_m3": [dens.band[0], dens.band[1]],
        "excess_density_purpose": purpose,
        "excess_density_why_this_end": (
            "relying on settling to retain, so the SLOWEST settling in the band"
            if purpose == "retain" else
            "checking for loss by settling, so the FASTEST settling in the band"),
        "settling_velocity_um_s": round(v_settle, 4),
        "settles_full_depth_s": round(t_settle, 2),
        "residence_s": None if residence_s is None else round(residence_s, 2),
        "settling_intended_here": intended,
    }
    if ctx.need.particles == "capsules":
        lo, hi = derived.capsule_settling_band_um_s()
        ctx.cite("contracts/blocks.json retention.alternatives_ranked "
                 "(sedimentation sump, 50-210 um/s)")
        v_nominal = physics.settling_velocity_um_s(ctx.particle_um, ctx.mu, dens.nominal)
        out["contract_settling_band_um_s"] = [lo, hi]
        out["settling_velocity_at_nominal_um_s"] = round(v_nominal, 4)
        out["nominal_inside_contract_band"] = lo <= v_nominal <= hi
        if not out["nominal_inside_contract_band"]:
            ctx.risk(f"at the nominal {dens.key} ({dens.nominal:g} kg/m3) a "
                     f"{ctx.particle_um:g} um capsule settles at {v_nominal:.0f} um/s, "
                     f"outside the {lo:g}-{hi:g} um/s band blocks.json states for capsule "
                     f"settling. Two contract values disagree; resolve that before either "
                     f"number is used for anything.")
    if residence_s is not None and residence_s > t_settle:
        out["settles_before_exit"] = True
        if intended:
            out["settling_note"] = (
                f"the cargo reaches the floor in {t_settle:.3g} s against a "
                f"{residence_s:.0f} s hold, which is what this chamber is for")
        else:
            ctx.risk(
                f"SEDIMENTATION: a {ctx.particle_um:g} um {ctx.need.particles[:-1]} falls "
                f"the full {ctx.depth_mm * 1000:g} um in {t_settle:.3g} s but the residence "
                f"time in the {what} is {residence_s:.0f} s, so the cargo reaches the floor "
                f"inside this part. Either keep the line moving, shorten the hold, or make "
                f"the hold a chamber where settling is intended.")
    elif residence_s is not None:
        out["settles_before_exit"] = False
    return out


def refuse_mixing(ctx: Ctx, re_number: float | None = None) -> None:
    """Only a chaotic-advection mixer mixes here. Folding a channel does not."""
    if not ctx.need.mix:
        return
    d = float(contracts.param("dye.diffusivity_m2_s"))
    ctx.cite("contracts/parameters.json dye.diffusivity_m2_s")
    extra = ""
    if ctx.need.flow_ul_min is not None:
        pe = physics.peclet(ctx.need.flow_ul_min, ctx.depth_mm, d)
        extra = f" (Pe = {pe:.0f}, Re = {re_number:.3g})" if re_number is not None else \
                f" (Pe = {pe:.0f})"
    raise PartSizingError(
        ctx.block,
        f"this block cannot mix at our flow rates{extra}. Re is order 1e-2, so there is no "
        f"secondary flow and no chaotic advection: folding a channel multiplies RESIDENCE "
        f"TIME, not contact. Streams side by side here mix only by diffusion across the "
        f"lane, which runs to metres. Use herringbone_mixer for mixing and keep this block "
        f"for residence time or resistance.")


def pillars_for_span(ctx: Ctx, x_mm: float, y_mm: float) -> tuple[int, float, float, list[str]]:
    """Lid support. A 0.3 mm chamber wider than the contract's pitch band collapses.

    Returns (count, pitch, pillar diameter, notes). Support is only added once a span
    passes the LOOSE end of the contract's 5-10 mm band; when it is added the pitch is the
    TIGHT end, which is the conservative choice inside the band rather than outside it.
    """
    tight, loose = derived.lid_support_pitch_mm()
    ctx.cite("contracts/blocks.json target_envelope.design_rules_at_this_scale "
             "(pillar_array lid support pitch)")
    span = max(x_mm, y_mm)
    if span <= loose + 1e-9:
        return 0, loose, 0.0, [f"unsupported span {span:.3g} mm is within the contract's "
                               f"{loose:g} mm pitch, so no lid support is needed"]
    dia = max(contracts.floor("min_wall_mm"), contracts.floor("min_open_channel_mm"))
    nx = max(1, int(math.floor(x_mm / tight)) - 1) if x_mm > tight else 0
    ny = max(1, int(math.floor(y_mm / tight)) - 1) if y_mm > tight else 0
    count = nx * ny
    gap = tight - dia
    if gap < contracts.floor("min_open_channel_mm"):
        raise PartSizingError(
            ctx.block, f"lid-support pillars at {tight:g} mm pitch leave a {gap:.3g} mm "
                       f"gap, below the in-plane floor")
    if ctx.need.particles in ("capsules", "beads"):
        cargo = ctx.particle_floor_mm()
        if gap < cargo:
            raise PartSizingError(
                ctx.block, f"lid-support pillars leave a {gap:.3g} mm gap but the cargo "
                           f"needs {cargo:.3g} mm to pass")
    return count, tight, dia, [
        f"span {span:.3g} mm exceeds the contract's {loose:g} mm lid-support pitch, so a "
        f"pillar_array at the band's tight end ({tight:g} mm) holds the lid flat: "
        f"{count} pillars of {dia:g} mm"]


def solve_box_chamber(ctx: Ctx, volume_ul: float, lane_mm: float,
                      displaced_ul_fn: Callable[[float, float], tuple[float, int, float, float]],
                      ) -> tuple[float, float, int, float, float]:
    """Grow a chamber until its FLUID volume (box minus solids) equals what is needed.

    `displaced_ul_fn(x, y)` returns (displaced uL, pillar count, pitch, diameter) for a
    candidate box, so crests and lid-support pillars are paid for in area rather than
    quietly eating into the volume the protocol will try to flush. Fixed point, capped,
    deterministic.

    The solved length is CLAMPED UP to the in-plane floor. A small volume behind a wide
    lane - a 0.012 uL load in a chamber the shear ceiling has already forced 8 mm wide -
    solves to a sub-floor length, and the answer to that is the shortest legal chamber,
    not a refusal. The part then holds more than was asked, and the VolumeRecord on ctx
    says so.
    """
    x = volume_ul / (lane_mm * ctx.depth_mm)
    count = 0
    pitch = dia = 0.0
    for _ in range(24):
        displaced, count, pitch, dia = displaced_ul_fn(x, lane_mm)
        new_x = (volume_ul + displaced) / (lane_mm * ctx.depth_mm)
        if abs(new_x - x) < 1e-9:
            x = new_x
            break
        x = new_x
    if x < min_span_mm() - 1e-12:
        floor_x = min_span_mm()
        built = floor_x * lane_mm * ctx.depth_mm - displaced_ul_fn(floor_x, lane_mm)[0]
        ctx.risk(
            f"a {volume_ul:.4g} uL load across a {lane_mm:.4g} mm lane solves to a "
            f"{x:.4g} mm flow path, under the {min_span_mm() * 1000:g} um in-plane floor, "
            f"so the chamber was lengthened to the floor and holds {built:.4g} uL - "
            f"{built / max(volume_ul, 1e-12):.0f}x the load. Recompute reagent ratios on "
            f"the built volume.")
        if ctx.volume is not None:
            ctx.volume = VolumeRecord(
                built, ctx.volume.requested_ul, ctx.volume.basis, ctx.volume.source, True,
                ctx.volume.floor_volume_ul,
                None if ctx.volume.area_mm2 is None else built / ctx.depth_mm,
                ctx.volume.why + (
                    f"flow path clamped up to the {min_span_mm():g} mm in-plane floor "
                    f"behind a {lane_mm:.4g} mm lane",))
        x = floor_x
    displaced, count, pitch, dia = displaced_ul_fn(x, lane_mm)
    return r(x), r(lane_mm), count, pitch, dia


# --- inline channel family ---------------------------------------------------

def _inline(ctx: Ctx, *, block: str, length_mm: float, type_hint: str,
            openings: tuple[Opening, Opening], footprint: tuple[float, float],
            path: list[list[float]], extra_features: tuple[Feature, ...] = (),
            lane_mm: float, why: list[str], physics_extra: dict | None = None,
            note: str = "") -> tuple:
    """Shared tail for every plain run: floors, entry, volume, physics."""
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane(f"{block} lane width", lane_mm)
    f.enclosed(f"{block} lane", lane_mm)
    f.fits(*footprint)
    check_particle_depth(ctx, ctx.depth_mm, "channel")
    vol = serpentine_volume_ul(length_mm, lane_mm, ctx.depth_mm)
    pid = ctx.prefix(block)
    entry = channel_entry(snake(pid), block, snake(pid, "in"), snake(pid, "out"),
                          lane_mm, ctx.depth_mm, carries=ctx.need.carries, path=path,
                          note=note or None)
    phys = {"length_mm": r(length_mm), "lane_width_mm": lane_mm,
            "width_set_by": why, **shear_report(ctx, lane_mm, "channel lane width")}
    if ctx.need.flow_ul_min is not None:
        phys["pressure_drop_pa"] = round(physics.pressure_drop_pa(
            length_mm, lane_mm, ctx.depth_mm, ctx.mu, ctx.need.flow_ul_min), 4)
        phys["residence_s"] = round(physics.residence_time_s(vol, ctx.need.flow_ul_min), 3)
        phys.update(sedimentation_report(ctx, phys["residence_s"], block))
    if physics_extra:
        phys.update(physics_extra)
    return (footprint, vol, openings, extra_features,
            (Expansion("channel", entry, r(vol), "length x lane width x depth"),), phys)


def _run_length(ctx: Ctx) -> float:
    """Length of a plain run: given, or whatever the asked-for volume implies."""
    if ctx.need.length_mm is not None:
        return float(ctx.need.length_mm)
    if ctx.need.volume_ul is not None or (
            ctx.need.residence_s is not None and ctx.need.flow_ul_min is not None):
        return required_volume_ul(ctx)  # converted to length by the caller
    raise PartSizingError(ctx.block, "needs length_mm, or a volume to turn into length")


def size_straight_channel(ctx: Ctx):
    lane, why = lane_width_mm(ctx, "lane")
    if ctx.need.length_mm is not None:
        length = float(ctx.need.length_mm)
    else:
        length = required_volume_ul(ctx) / (lane * ctx.depth_mm)
    refuse_mixing(ctx)
    half = lane / 2.0
    ops = (Opening("inlet", (0.0, half), "-x", lane, "inlet", ctx.need.carries),
           Opening("outlet", (r(length), half), "+x", lane, "outlet", ctx.need.carries))
    return _inline(ctx, block="straight_channel", length_mm=length, type_hint="junction",
                   openings=ops, footprint=(r(length), lane),
                   path=[[0.0, r(half)], [r(length), r(half)]], lane_mm=lane, why=why)


def size_curved_channel(ctx: Ctx):
    """Constant-radius bend. The arc IS the length: L = R * angle."""
    lane, why = lane_width_mm(ctx, "lane")
    refuse_mixing(ctx)
    if ctx.need.length_mm is not None:
        length = float(ctx.need.length_mm)
    else:
        length = required_volume_ul(ctx) / (lane * ctx.depth_mm)
    radius = length / (math.pi / 2.0)  # a quarter turn
    if radius < lane:
        raise PartSizingError(
            "curved_channel",
            f"a quarter turn of {length:.3g} mm needs a {radius:.3g} mm centreline radius, "
            f"tighter than the {lane:g} mm lane itself: use channel_corner instead")
    side = r(radius + lane / 2.0)
    half = lane / 2.0
    ops = (Opening("inlet", (0.0, r(radius)), "-x", lane, "inlet", ctx.need.carries),
           Opening("outlet", (r(radius), 0.0), "-y", lane, "outlet", ctx.need.carries))
    # The arc itself, so the centreline length in the spec is the real path and not a
    # straight line between the two ends.
    path = [[r(radius * (1.0 - math.cos(t))), r(radius * (1.0 - math.sin(t)))]
            for t in (0.0, math.pi / 8, math.pi / 4, 3 * math.pi / 8, math.pi / 2)]
    return _inline(ctx, block="curved_channel", length_mm=length, type_hint="junction",
                   openings=ops, footprint=(side, side), path=path, lane_mm=lane, why=why,
                   physics_extra={"centreline_radius_mm": r(radius), "sweep_deg": 90},
                   note=f"quarter turn, {r(radius)} mm centreline radius")


def size_channel_corner(ctx: Ctx):
    """Right-angle corner. Shorter than a bend and higher shear at the inside wall."""
    lane, why = lane_width_mm(ctx, "lane")
    refuse_mixing(ctx)
    arm = max(lane, contracts.floor("min_open_channel_mm"))
    length = 2 * arm
    side = r(arm + lane / 2.0)
    ops = (Opening("inlet", (0.0, r(arm)), "-x", lane, "inlet", ctx.need.carries),
           Opening("outlet", (r(arm), 0.0), "-y", lane, "outlet", ctx.need.carries))
    ctx.risk("a square corner has a stagnant inside pocket and a local shear peak; "
             "curved_channel avoids both where the cargo is shear-sensitive")
    return _inline(ctx, block="channel_corner", length_mm=length, type_hint="junction",
                   openings=ops, footprint=(side, side),
                   path=[[0.0, r(arm)], [r(arm), r(arm)], [r(arm), 0.0]],
                   lane_mm=lane, why=why)


def _folded(ctx: Ctx, block: str, *, rib_mm: float, note: str,
            mixing_allowed: bool = False):
    """Serpentine / snake / asymmetric serpentine: residence time or resistance by length.

    The lane width comes from shear and cargo, the length from the volume the residence
    time implies, and the fold from the envelope. Volume is length x width x depth - never
    the folded bounding box, which overstates it by roughly the fold ratio.
    """
    lane, why = lane_width_mm(ctx, "lane")
    volume = required_volume_ul(ctx)
    length = volume / (lane * ctx.depth_mm)
    re_number = None
    if ctx.need.flow_ul_min is not None:
        re_number = physics.reynolds(ctx.need.flow_ul_min, lane, ctx.depth_mm, ctx.mu,
                                     derived.fluid_density_kg_m3())
    if not mixing_allowed:
        refuse_mixing(ctx, re_number)
    elif ctx.need.mix:
        if re_number is None or re_number < 1.0:
            raise PartSizingError(
                block,
                f"unequal arms only mix once inertia exists, and Re here is "
                f"{0.0 if re_number is None else re_number:.3g} (<< 1). In Stokes flow the "
                f"streamlines are reversible, so the arms just rejoin unmixed. Use "
                f"herringbone_mixer.")
    fold = fold_into_lanes(length, lane, rib_mm, ctx.usable_x_mm, block)
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane(f"{block} lane width", lane)
    f.wall(f"{block} rib between lanes", rib_mm)
    f.enclosed(f"{block} lane", lane)
    f.fits(*fold.footprint_mm)
    check_particle_depth(ctx, ctx.depth_mm, "folded lane")
    vol = serpentine_volume_ul(fold.length_mm, lane, ctx.depth_mm)
    pid = ctx.prefix(block)
    # A folded run is a chip-spec MIXER chamber: that object is the only place the schema
    # lets a centreline length and a channel width live together with a volume.
    entry = chamber_entry(
        snake(pid), "mixer", block, (fold.footprint_mm[0], fold.footprint_mm[1], ctx.depth_mm),
        vol, mixer={"kind": "serpentine", "channel_width_mm": lane,
                    "turns": fold.turns, "length_mm": fold.length_mm},
        height_um=r(ctx.depth_mm * 1000), note=note)
    phys = {
        "centreline_length_mm": fold.length_mm, "lanes": fold.lanes,
        "lane_run_mm": fold.run_mm, "rib_mm": fold.rib_mm, "turns": fold.turns,
        "width_set_by": why, "fold_ratio": round(fold.length_mm / max(fold.run_mm, 1e-9), 2),
        "bounding_box_volume_ul": r(box_volume_ul(*fold.footprint_mm, ctx.depth_mm)),
        **shear_report(ctx, lane, "folded lane width"),
    }
    if ctx.need.flow_ul_min is not None:
        phys["residence_s"] = round(physics.residence_time_s(vol, ctx.need.flow_ul_min), 2)
        phys["pressure_drop_pa"] = round(physics.pressure_drop_pa(
            fold.length_mm, lane, ctx.depth_mm, ctx.mu, ctx.need.flow_ul_min), 3)
        phys["resistance_pa_s_per_m3"] = f"{physics.rect_resistance_pa_s_per_m3(fold.length_mm, lane, ctx.depth_mm, ctx.mu):.4g}"
        phys["diffusive_mixing_length_mm"] = round(physics.diffusive_mixing_length_mm(
            ctx.need.flow_ul_min, lane, ctx.depth_mm,
            float(contracts.param("dye.diffusivity_m2_s"))), 1)
        phys.update(sedimentation_report(ctx, phys["residence_s"], "delay line"))
    ctx.risk("this is a delay line, not a mixer: at Re << 1 folding adds residence time "
             "and nothing else (blocks.json herringbone_mixer is the mixing block)")
    openings = (
        Opening("inlet", (0.0, r(lane / 2.0)), "-x", lane, "inlet", ctx.need.carries),
        Opening("outlet",
                (r(fold.run_mm) if (fold.lanes - 1) % 2 == 0 else 0.0,
                 r(lane / 2.0 + (fold.lanes - 1) * (lane + rib_mm))),
                "+x" if (fold.lanes - 1) % 2 == 0 else "-x", lane, "outlet",
                ctx.need.carries),
    )
    features = (Feature("fold_turn", fold.turns, (lane, lane, ctx.depth_mm),
                        lane + rib_mm, lane,
                        "180 deg turn between lanes; its length is counted in the "
                        "centreline, so the residence time is right"),)
    return (fold.footprint_mm, vol, openings, features,
            (Expansion("chamber", entry, r(vol), "length x lane width x depth"),), phys)


def size_serpentine_channel(ctx: Ctx):
    return _folded(ctx, "serpentine_channel",
                   rib_mm=contracts.floor("min_wall_mm"),
                   note="delay line: residence time by centreline length. Not a mixer.")


def size_snake_channel(ctx: Ctx):
    """Tighter-pitch fold: same lane, the rib held exactly at the wall floor."""
    return _folded(ctx, "snake_channel", rib_mm=contracts.floor("min_wall_mm"),
                   note="tight-pitch delay line; the rib sits exactly on min_wall_mm, so "
                        "there is no margin left for wall thinning in the mould")


def size_asymmetric_serpentine(ctx: Ctx):
    return _folded(ctx, "asymmetric_serpentine",
                   rib_mm=contracts.floor("min_wall_mm"), mixing_allowed=True,
                   note="unequal-arm fold: resistance and residence time. Mixing only "
                        "once Re >= 1, which our flows do not reach.")


def size_spiral_channel(ctx: Ctx):
    """Archimedean spiral: a long path in a round footprint.

    Catalogue note: the Flowmatter spiral is 104.8 x 99.8 mm, which alone is 17x the demo
    envelope. Sized here from the length actually needed instead.
    """
    lane, why = lane_width_mm(ctx, "lane")
    volume = required_volume_ul(ctx)
    length = volume / (lane * ctx.depth_mm)
    rib = contracts.floor("min_wall_mm")
    pitch = lane + rib
    # L = pi * n * (d_in + d_out) / 2 for n turns between an inner and outer diameter.
    d_in = max(4 * pitch, 2 * lane)
    turns = 1.0
    for _ in range(64):
        d_out = d_in + 2 * turns * pitch
        got = math.pi * turns * (d_in + d_out) / 2.0
        if got >= length:
            break
        turns += 0.5
    else:
        raise PartSizingError("spiral_channel", f"cannot reach {length:.3g} mm of path")
    d_out = d_in + 2 * turns * pitch
    side = r(d_out + lane)
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane("spiral lane width", lane)
    f.wall("spiral rib", rib)
    f.enclosed("spiral lane", lane)
    f.fits(side, side)
    check_particle_depth(ctx, ctx.depth_mm, "spiral lane")
    vol = serpentine_volume_ul(length, lane, ctx.depth_mm)
    pid = ctx.prefix("spiral")
    entry = chamber_entry(snake(pid), "mixer", "spiral_channel", (side, side, ctx.depth_mm),
                          vol, shape="cylinder",
                          mixer={"kind": "serpentine", "channel_width_mm": lane,
                                 "turns": int(math.ceil(turns)), "length_mm": r(length)},
                          note="Archimedean spiral; long path in a round footprint")
    phys = {"centreline_length_mm": r(length), "turns": turns,
            "inner_diameter_mm": r(d_in), "outer_diameter_mm": r(d_out),
            "width_set_by": why, **shear_report(ctx, lane, "spiral lane width")}
    if ctx.need.flow_ul_min is not None:
        phys["residence_s"] = round(physics.residence_time_s(vol, ctx.need.flow_ul_min), 2)
        phys["dean_number_note"] = ("inertial focusing needs Dean numbers we do not reach "
                                    "at Re ~ 1e-2; treat this purely as a long path")
        phys.update(sedimentation_report(ctx, phys["residence_s"], "spiral"))
    ops = (Opening("inlet", (0.0, r(side / 2.0)), "-x", lane, "inlet", ctx.need.carries),
           Opening("outlet", (r(side / 2.0), 0.0), "-y", lane, "outlet", ctx.need.carries))
    feats = (Feature("spiral_turn", int(math.ceil(turns)), (side, side, ctx.depth_mm),
                     r(pitch), lane, "constant-pitch Archimedean turns"),)
    return ((side, side), vol, ops, feats,
            (Expansion("chamber", entry, r(vol), "length x lane width x depth"),), phys)


# --- the special blocks ------------------------------------------------------

def size_trap_chamber(ctx: Ctx):
    """Chamber with a DEPTH WEIR at its outlet. The retention scheme, built from data.

    The 200 um in-plane floor applies to WIDTH, not to z, so a crest that stops short of
    the lid leaves a gap in z that retains a capsule while liquid passes. Everything here
    is derived:

      gap          scaled from the contract's agreed 25 um for a 70 um capsule, and then
                   checked against the [0.3 D, 0.4 D) band - upper bound STRICT, because
                   tools/check_chip_spec.py fails a gap equal to it.
      crest height THAT CHIP's etch depth minus the gap. There is deliberately no fixed
                   number for it anywhere in the contract.
      crest thick  the in-plane floor, 200 um - a thickness, never a height.
      volume       box MINUS the crest, which is solid.
    """
    need = ctx.need
    if need.particles == "none":
        raise PartSizingError(
            "trap_chamber", "a trap chamber with nothing to retain is a polygon_chamber")
    d_um = ctx.particle_um or contracts.capsule_diameter_um()
    agreed_gap = derived.weir_gap_um()
    agreed_retains = derived.weir_retains_um()
    low, high = derived.weir_gap_band_um(d_um)
    ctx.cite("contracts/blocks.json retention.weir_geometry (gap_um, crest_thickness_um, "
             "crest_height_rule, retains_um, z_tolerance_warning)",
             "contracts/parameters.json trap.gap_factor_low / trap.gap_factor_high")

    # Hold the contract's own gap-to-diameter ratio, so the band is respected at any D.
    gap_um = agreed_gap * d_um / agreed_retains
    if not (low - 1e-9 <= gap_um < high):
        raise PartSizingError(
            "trap_chamber",
            f"a {gap_um:.3g} um gap for a {d_um:g} um particle is outside the contract's "
            f"[{low:g}, {high:g}) um band")
    depth_um = ctx.depth_mm * 1000.0
    crest_um = depth_um - gap_um
    if crest_um <= 0:
        raise PartSizingError(
            "trap_chamber",
            f"crest height is etch depth minus gap = {depth_um:g} - {gap_um:.3g} = "
            f"{crest_um:.3g} um: a crest taller than its layer cannot exist. This chip is "
            f"too shallow to carry a weir for a {d_um:g} um particle.",
            floor="crest_height_rule")
    crest_thick_um = derived.weir_crest_thickness_um()

    lane, why = lane_width_mm(ctx, "chamber width across flow")
    volume = required_volume_ul(ctx)
    crest_h_mm = crest_um / 1000.0
    crest_t_mm = crest_thick_um / 1000.0

    def displaced(x_mm: float, y_mm: float):
        crest = crest_t_mm * y_mm * crest_h_mm
        count, pitch, dia, _ = pillars_for_span(ctx, x_mm, y_mm)
        return (crest + count * cylinder_volume_ul(dia, dia, ctx.depth_mm),
                count, pitch, dia)

    # Square-ish chamber: start from a lane width that makes it roughly so, but never
    # below whatever shear and cargo already demand.
    lane = max(lane, r(math.sqrt(max(volume / ctx.depth_mm, 1e-9))))
    x, y, n_pillars, pitch, dia = solve_box_chamber(ctx, volume, lane, displaced)
    _, _, _, notes = pillars_for_span(ctx, x, y)

    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane("weir crest thickness (along the flow)", crest_t_mm,
               "crest_thickness_um IS the in-plane floor; it is not a height")
    f.in_plane("chamber width across the flow", y)
    f.z("weir gap (crest to lid)", gap_um / 1000.0)
    f.z("weir crest height above the floor", crest_h_mm,
        "etch depth minus gap, derived per chip (retention.weir_geometry.crest_height_rule)")
    f.enclosed("trap chamber", y)
    f.fits(x, y)
    check_particle_depth(ctx, ctx.depth_mm, "trap chamber", culture=False)

    fluid = weir_chamber_volume_ul(x, y, ctx.depth_mm, crest_t_mm, crest_h_mm) - \
        n_pillars * cylinder_volume_ul(dia, dia, ctx.depth_mm)
    z_tol = derived.z_tolerance_um()
    ctx.risk(
        f"Z TOLERANCE: the gap is {gap_um:.3g} um and published z tolerance on these "
        f"processes is +/-{z_tol:g} um, which is +/-{100 * z_tol / gap_um:.0f}% of it, so "
        f"the weir could close or leak outright. blocks.json calls this the biggest unknown "
        f"in the retention scheme: coupon {low:.0f}, {(low + high) / 2:.0f} and "
        f"{high - 1:.0f} um gaps before committing. If it fails, the lever is capsule size "
        f"(tunable 35-260 um), which buys a proportionally larger gap.",
        f"flow distribution across a wide weir is explicitly NOT scale-free "
        f"(blocks.json target_envelope.not_scale_free); this crest is {y:.3g} mm wide")
    if ctx.need.particles == "cells":
        ctx.risk("this weir retains CAPSULES, not bare cells: a 10-15 um cell passes a "
                 f"{gap_um:.3g} um gap (blocks.json retention._cells_caveat)")

    pid = ctx.prefix("trap")
    entry = chamber_entry(
        snake(pid), "exchange", "trap_chamber", (x, y, ctx.depth_mm), fluid,
        height_um=r(depth_um),
        retention={"type": "weir", "opening_um": round(gap_um, 2), "source": "fabricated",
                   "crest_height_um": round(crest_um, 2),
                   "crest_thickness_um": round(crest_thick_um, 2),
                   "retains_um": d_um},
        note=f"depth weir at the outlet: {gap_um:.3g} um gap in z under the lid, "
             f"{crest_um:.3g} um crest ({depth_um:g} um etch depth minus the gap), "
             f"{crest_thick_um:g} um thick in plane. Volume is the box minus the crest"
             + (f" and {n_pillars} lid-support pillars" if n_pillars else "") + ".")
    phys = {
        "weir_gap_um": round(gap_um, 3), "crest_height_um": round(crest_um, 3),
        "crest_thickness_um": crest_thick_um, "retains_um": d_um,
        "gap_band_um": [round(low, 2), round(high, 2)], "gap_band_upper_is_strict": True,
        "gap_over_diameter": round(gap_um / d_um, 4),
        "z_tolerance_um": z_tol,
        "z_tolerance_percent_of_gap": round(100 * z_tol / gap_um, 1),
        "width_set_by": why, "lid_support": notes,
        **shear_report(ctx, y, "chamber width across the flow"),
    }
    if ctx.need.flow_ul_min is not None:
        dp = derived.weir_pressure_drop_pa_at_100_ul_min() * ctx.need.flow_ul_min / 100.0
        phys["weir_pressure_drop_pa"] = round(dp, 4)
        phys["escape_pressure_margin_kpa"] = contracts.max_working_pressure_kpa()
        if dp / 1000.0 > contracts.max_working_pressure_kpa():
            raise PartSizingError(
                "trap_chamber",
                f"the weir costs {dp / 1000:.3g} kPa at {ctx.need.flow_ul_min:g} uL/min, "
                f"above the working ceiling {contracts.max_working_pressure_kpa():.3g} kPa "
                f"(escape pressure x margin): capsules would squeeze through")
        phys["residence_s"] = round(physics.residence_time_s(fluid, ctx.need.flow_ul_min), 2)
        phys.update(sedimentation_report(ctx, phys["residence_s"], "trap chamber", intended=True))
    feats = [Feature("weir_crest", 1, (crest_t_mm, y, crest_h_mm), None, crest_t_mm,
                     f"standing ridge across the full {y:.3g} mm outlet, {crest_h_mm * 1000:.3g} "
                     f"um tall, leaving a {gap_um:.3g} um gap in z under the lid. Relief in "
                     f"the same mould, so still ONE fluidic layer.")]
    if n_pillars:
        feats.append(Feature("lid_support_pillar", n_pillars, (dia, dia, ctx.depth_mm),
                             pitch, dia, notes[0]))
    ops = (
        Opening("inlet", (0.0, r(y / 2.0)), "-x", y, "inlet", ctx.need.carries,
                "full-width feed, so the weir sees an even front"),
        Opening("filtrate", (r(x), r(y / 2.0)), "+x", y, "outlet", "liquid",
                "behind the weir: liquid leaves, the cargo does not"),
        Opening("backflush", (r(x / 2.0), 0.0), "-y", r(y / 2.0), "release",
                ctx.need.carries, "reverse flow lifts the retained cargo off the crest"),
    )
    return ((x, y), fluid, ops, tuple(feats),
            (Expansion("chamber", entry, r(fluid), "box minus crest minus pillars"),), phys)


def size_centipede_channel(ctx: Ctx):
    """Comb distribution: a spine with many short teeth. The Module 6 precedent.

    Module 6 feeds a 1.2 x 8 mm culture field from its long edge through a 0.8 mm spine
    with 13 teeth of 0.2 x 1.5 mm at 0.6 mm pitch. Those numbers are not arbitrary: the
    tooth width IS the in-plane floor, the rib IS the wall floor, and the spine IS the
    port floor, so the comb is the tightest legal distribution at this scale. This sizer
    re-derives them from the floors and asserts they still match the precedent.
    """
    ctx.need.require("feed_edge_mm")
    edge = float(ctx.need.feed_edge_mm)
    tooth_w = contracts.floor("min_open_channel_mm")
    rib = contracts.floor("min_wall_mm")
    pitch = tooth_w + rib
    spine_w = contracts.floor("min_port_mm")
    tooth_len, tooth_src = PRECEDENT["comb_tooth_length_mm"]
    ctx.cite("chip/module6_v1.json centipede_channel comb (teeth 0.2 x 1.5 mm at 0.6 mm "
             "pitch, 0.4 mm ribs, 0.8 mm spine)", tooth_src,
             "contracts/blocks.json fabrication_floors.min_open_channel_mm / min_wall_mm "
             "/ min_port_mm")

    teeth = int(math.floor(edge / pitch))
    if teeth < 2:
        raise PartSizingError(
            "centipede_channel",
            f"a {edge:g} mm edge fits {teeth} teeth at the floor pitch {pitch:g} mm "
            f"(0.2 mm tooth + 0.4 mm rib); a comb needs at least 2. Feed this edge with a "
            f"single straight_channel instead.")
    # The spine must also carry the total flow without exceeding the applicable ceiling.
    if ctx.ceiling.ceiling_dyn_cm2 is not None and ctx.need.flow_ul_min is not None:
        spine_w = max(spine_w, physics.lane_width_for_shear_mm(
            ctx.need.flow_ul_min, ctx.depth_mm, ctx.mu, ctx.ceiling.ceiling_dyn_cm2))
    spine_w = r(spine_w)

    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane("comb tooth width", tooth_w)
    f.in_plane("comb spine width", spine_w)
    f.wall("comb rib between teeth", rib)
    f.port("comb spine (must accept a port)", spine_w)
    f.enclosed("comb tooth", tooth_w)
    x = r(spine_w + tooth_len)
    y = r(edge)
    f.fits(x, y)
    check_particle_depth(ctx, ctx.depth_mm, "comb tooth")
    if ctx.need.particles != "none":
        cargo = ctx.particle_floor_mm()
        if tooth_w < cargo:
            raise PartSizingError(
                "centipede_channel",
                f"a tooth at the {tooth_w:g} mm in-plane floor cannot pass "
                f"{ctx.need.particles} needing {cargo:.3g} mm. Feed the cargo through the "
                f"spine and keep the comb for liquid, as Module 6 does.")

    spine_vol = serpentine_volume_ul(edge, spine_w, ctx.depth_mm)
    teeth_vol = teeth * serpentine_volume_ul(tooth_len, tooth_w, ctx.depth_mm)
    vol = spine_vol + teeth_vol
    pid = ctx.prefix("comb")
    per_tooth = None if ctx.need.flow_ul_min is None else ctx.need.flow_ul_min / teeth
    entry = channel_entry(
        snake(pid), "centipede_channel", snake(pid, "in"), snake(pid, "out"),
        spine_w, ctx.depth_mm, carries=ctx.need.carries,
        path=[[r(spine_w / 2.0), 0.0], [r(spine_w / 2.0), y]],
        note=f"{spine_w:g} mm spine along the {edge:g} mm edge with {teeth} teeth "
             f"{tooth_w:g} x {tooth_len:g} mm at {pitch:g} mm pitch ({rib:g} mm ribs), "
             f"opening into the chamber. Volume {r(vol)} uL = spine {r(spine_vol)} + "
             f"{teeth} teeth {r(teeth_vol)}.")
    phys = {
        "teeth": teeth, "tooth_width_mm": tooth_w, "tooth_length_mm": tooth_len,
        "rib_mm": rib, "pitch_mm": r(pitch), "spine_width_mm": spine_w,
        "edge_fed_mm": y, "flow_per_tooth_ul_min": None if per_tooth is None else round(per_tooth, 4),
        "spine_volume_ul": r(spine_vol), "teeth_volume_ul": r(teeth_vol),
        **shear_report(ctx, spine_w, "comb spine width"),
    }
    if per_tooth is not None:
        phys["tooth_wall_shear_dyn_cm2"] = round(physics.wall_shear_dyn_cm2(
            per_tooth, tooth_w, ctx.depth_mm, ctx.mu), 6)
        phys["tooth_pressure_drop_pa"] = round(physics.pressure_drop_pa(
            tooth_len, tooth_w, ctx.depth_mm, ctx.mu, per_tooth), 4)
        spine_dp = physics.pressure_drop_pa(edge, spine_w, ctx.depth_mm, ctx.mu,
                                            ctx.need.flow_ul_min)
        phys["spine_pressure_drop_pa"] = round(spine_dp, 4)
        ratio = phys["tooth_pressure_drop_pa"] / max(spine_dp, 1e-12)
        phys["tooth_to_spine_resistance_ratio"] = round(ratio, 2)
        if ratio < 5:
            ctx.risk(
                f"the teeth cost only {ratio:.2g}x the spine, so flow favours the near "
                f"teeth and the far end of the {edge:g} mm edge is starved. Even "
                f"distribution wants the teeth to dominate the resistance (>= 5x); "
                f"lengthen the teeth or widen the spine.")
    ops = tuple(
        [Opening("spine_in", (r(spine_w / 2.0), 0.0), "-y", spine_w, "inlet",
                 ctx.need.carries, "the port end of the spine")] +
        [Opening("chamber_edge", (x, r(y / 2.0)), "+x", y, "outlet", "liquid",
                 f"the whole {y:g} mm toothed edge attaches to the chamber")])
    feats = (
        Feature("comb_tooth", teeth, (tooth_len, tooth_w, ctx.depth_mm), r(pitch), tooth_w,
                f"{teeth} real teeth, {tooth_w:g} x {tooth_len:g} mm, at {pitch:g} mm pitch; "
                f"the tooth width is the in-plane floor and the rib is the wall floor"),
        Feature("comb_rib", max(0, teeth - 1), (tooth_len, rib, ctx.depth_mm), r(pitch), rib,
                "solid rib between teeth, exactly min_wall_mm"),
    )
    return ((x, y), vol, ops, feats,
            (Expansion("channel", entry, r(vol), "spine + every tooth"),), phys)


def size_herringbone_mixer(ctx: Ctx):
    """The ONLY block that mixes here: chevron grooves, chaotic advection at Re << 1.

    Two things fall out of our own floors and are reported rather than hidden:

      The published SHM has ~50 um grooves at ~100 um pitch. Our in-plane floor is 200 um,
      so the grooves must be 4x the published size and the channel 4x as wide to keep the
      published proportions (groove pitch ~ w/2). This is a scaled-up, untested variant.

      The grooves are RELIEF IN Z below the channel floor, exactly like the weir crest, so
      the channel height is the etch depth minus the groove depth - not the full depth.
    """
    ctx.need.require("flow_ul_min")
    groove_w = contracts.floor("min_open_channel_mm")
    ridge_w = contracts.floor("min_wall_mm")
    groove_pitch = groove_w + ridge_w
    frac, frac_src = PRECEDENT["shm_groove_depth_fraction"]
    per_half, half_src = PRECEDENT["shm_grooves_per_half_cycle"]
    ctx.cite(frac_src, half_src,
             "contracts/parameters.json dye.diffusivity_m2_s")

    # Published proportion: groove pitch is about half the channel width.
    lane_floor, why = lane_width_mm(ctx, "lane")
    lane = r(max(lane_floor, 2.0 * groove_pitch))
    if lane > lane_floor:
        why.append(f"published SHM proportion groove pitch ~ w/2 needs {lane:g} mm")

    channel_h = ctx.depth_mm / (1.0 + frac)
    groove_h = ctx.depth_mm - channel_h
    d_mol = float(contracts.param("dye.diffusivity_m2_s"))
    pe = physics.peclet(ctx.need.flow_ul_min, channel_h, d_mol)
    cycles = physics.herringbone_cycles(pe)
    cycle_len = 2.0 * per_half * groove_pitch
    length = cycles * cycle_len
    rib = contracts.floor("min_wall_mm")
    fold = fold_into_lanes(length, lane, rib, ctx.usable_x_mm, "herringbone_mixer")

    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane("herringbone groove width", groove_w)
    f.in_plane("mixer channel width", lane)
    f.wall("ridge between grooves", ridge_w)
    f.wall("rib between folded lanes", rib)
    f.enclosed("mixer channel", lane)
    f.z("channel height above the groove floor", channel_h)
    f.z("groove depth below the channel floor", groove_h)
    f.fits(*fold.footprint_mm)
    check_particle_depth(ctx, channel_h, "mixer channel")

    n_grooves = cycles * 2 * per_half
    groove_run = lane * math.sqrt(2.0)  # 45 degrees across the channel
    vol = serpentine_volume_ul(fold.length_mm, lane, channel_h) + \
        n_grooves * groove_w * groove_run * groove_h
    z_tol = derived.z_tolerance_um()
    ctx.risk(
        f"the published staggered herringbone mixer uses ~50 um grooves; our in-plane "
        f"floor forces {groove_w * 1000:g} um grooves in a {lane:g} mm channel, so this is "
        f"a 4x scale-up of a published geometry and its mixing has NOT been measured here",
        f"the grooves are {groove_h * 1000:.3g} um of relief in z and process z tolerance is "
        f"+/-{z_tol:g} um ({100 * z_tol / (groove_h * 1000):.0f}% of the groove), so groove "
        f"depth is the dominant fabrication risk in this part",
        f"the {fold.turns} fold turns are not grooved, so about "
        f"{100 * fold.turns * (lane + rib) / fold.length_mm:.0f}% of the path does not mix")
    pid = ctx.prefix("hbm")
    entry = chamber_entry(
        snake(pid), "mixer", "herringbone_mixer",
        (fold.footprint_mm[0], fold.footprint_mm[1], ctx.depth_mm), vol,
        height_um=r(channel_h * 1000),
        note=f"staggered herringbone: {cycles} cycles, {n_grooves} chevron grooves "
             f"{groove_w:g} mm wide at {groove_pitch:g} mm pitch, {groove_h * 1000:.3g} um "
             f"deep below a {channel_h * 1000:.3g} um channel, folded into {fold.lanes} "
             f"lanes. Chaotic advection, not diffusion: Pe {pe:.0f} needs {cycles} cycles.")
    phys = {
        "peclet": round(pe, 1), "cycles": cycles, "grooves": n_grooves,
        "cycle_length_mm": r(cycle_len), "centreline_length_mm": fold.length_mm,
        "channel_height_um": round(channel_h * 1000, 2),
        "groove_depth_um": round(groove_h * 1000, 2),
        "groove_width_mm": groove_w, "groove_pitch_mm": r(groove_pitch),
        "lanes": fold.lanes, "width_set_by": why,
        "diffusive_mixing_length_mm": round(physics.diffusive_mixing_length_mm(
            ctx.need.flow_ul_min, lane, channel_h, d_mol), 1),
        "why_not_a_serpentine": (
            "mixing by diffusion alone would need the diffusive_mixing_length_mm above; "
            "chaotic advection needs only log(Pe) cycles"),
        **shear_report(ctx, lane, "mixer channel width"),
    }
    phys["residence_s"] = round(physics.residence_time_s(vol, ctx.need.flow_ul_min), 2)
    phys.update(sedimentation_report(ctx, phys["residence_s"], "mixer"))
    ops = (
        Opening("inlet_a", (0.0, r(lane / 4.0)), "-x", r(lane / 2.0), "inlet",
                ctx.need.carries, "one of two streams entering side by side"),
        Opening("inlet_b", (0.0, r(3 * lane / 4.0)), "-x", r(lane / 2.0), "inlet",
                ctx.need.carries, "the other stream"),
        Opening("outlet",
                (r(fold.run_mm) if (fold.lanes - 1) % 2 == 0 else 0.0,
                 r(lane / 2.0 + (fold.lanes - 1) * (lane + rib))),
                "+x" if (fold.lanes - 1) % 2 == 0 else "-x", lane, "outlet",
                ctx.need.carries),
    )
    feats = (
        Feature("herringbone_groove", n_grooves, (groove_w, r(groove_run), r(groove_h)),
                r(groove_pitch), groove_w,
                f"chevron at 45 deg, {per_half} per half cycle, asymmetric so each half "
                f"cycle shifts the rotation centre; relief in z below the channel floor, "
                f"so still ONE fluidic layer"),
        Feature("fold_turn", fold.turns, (lane, lane, ctx.depth_mm), r(lane + rib), lane,
                "ungrooved turn between lanes"),
    )
    return (fold.footprint_mm, vol, ops, feats,
            (Expansion("chamber", entry, r(vol), "channel + every groove"),), phys)


def size_phase_guide(ctx: Ctx):
    """A CAPILLARY STOP. It pins a liquid front at a line; it does not set resistance.

    An audit found phase_guide and flow_restrictor swapped. They are opposites:
    a phase guide's job is to STOP a front until a burst pressure is exceeded, and it is
    deliberately as cheap in resistance as a ridge can be. If you want resistance, the
    block is flow_restrictor.

    Burst pressure needs surface tension and a contact angle, and NEITHER is in
    contracts/parameters.json - so this returns None for it and names the gap instead of
    inventing a number.
    """
    lane, why = lane_width_mm(ctx, "channel at the guide")
    ridge_t = contracts.floor("min_open_channel_mm")
    # A pinning ridge is relief in z: it must leave the front something to pin ON, so the
    # ridge stands proud of the floor but stops well short of the lid.
    gap_frac = 0.5
    ridge_h = ctx.depth_mm * gap_frac
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane("phase guide ridge thickness", ridge_t)
    f.in_plane("channel at the guide", lane)
    f.z("phase guide ridge height", ridge_h,
        "a pinning ridge is relief in z, like the weir crest: not an in-plane feature")
    f.enclosed("channel at the guide", lane)
    x = r(ridge_t + 2 * lane)
    f.fits(x, lane)
    check_particle_depth(ctx, ctx.depth_mm - ridge_h, "gap over the phase guide")
    vol = serpentine_volume_ul(x, lane, ctx.depth_mm) - ridge_t * lane * ridge_h
    pid = ctx.prefix("guide")
    entry = channel_entry(
        snake(pid), "phase_guide", snake(pid, "in"), snake(pid, "out"), lane, ctx.depth_mm,
        carries=ctx.need.carries, path=[[0.0, r(lane / 2.0)], [x, r(lane / 2.0)]],
        note=f"CAPILLARY STOP: a {ridge_t:g} mm thick ridge standing "
             f"{ridge_h * 1000:.3g} um proud of the floor, pinning the advancing front at "
             f"this line until the burst pressure is exceeded. Not a resistance element.")
    phys = {
        "function": "capillary_stop", "stops_flow": True,
        "ridge_thickness_mm": ridge_t, "ridge_height_um": round(ridge_h * 1000, 2),
        "clearance_over_ridge_um": round((ctx.depth_mm - ridge_h) * 1000, 2),
        "burst_pressure_pa": None,
        "burst_pressure_gap": (
            "surface tension and contact angle are not in contracts/parameters.json, so "
            "the burst pressure cannot be computed. Record it in `unresolved` and measure "
            "it on a coupon; do not assume one."),
        "sets_resistance": False,
        "not_to_be_confused_with": (
            "flow_restrictor, which sets RESISTANCE. These two were found swapped in an "
            "audit: a phase guide holds a front, a restrictor meters a flow."),
        **shear_report(ctx, lane, "channel width at the guide"),
    }
    ctx.risk("a phase guide only works while the front is advancing by capillarity; "
             "under pump pressure above its (unmeasured) burst pressure it does nothing")
    ops = (Opening("inlet", (0.0, r(lane / 2.0)), "-x", lane, "inlet", ctx.need.carries),
           Opening("outlet", (x, r(lane / 2.0)), "+x", lane, "outlet", ctx.need.carries))
    feats = (Feature("pinning_ridge", 1, (ridge_t, lane, r(ridge_h)), None, ridge_t,
                     "ridge across the full channel; the front pins on its upstream edge"),)
    return ((x, lane), vol, ops, feats,
            (Expansion("channel", entry, r(vol), "channel minus the ridge"),), phys)


def size_flow_restrictor(ctx: Ctx):
    """Sets RESISTANCE. A deliberate local constriction, sized to a pressure drop.

    The other half of the pair the audit found swapped: this one does NOT stop a front,
    and a phase_guide cannot be used to meter a flow.
    """
    ctx.need.require("flow_ul_min")
    throat = contracts.floor("min_open_channel_mm")
    cargo = ctx.particle_floor_mm()
    if cargo > throat:
        throat = cargo
        ctx.risk(f"the throat had to open to {throat:.3g} mm to pass "
                 f"{ctx.need.particles}, which costs resistance: a restrictor that the "
                 f"cargo must traverse is a weak restrictor")
    throat = r(throat)
    if ctx.need.pressure_drop_pa is not None:
        length = physics.length_for_pressure_drop_mm(
            float(ctx.need.pressure_drop_pa), throat, ctx.depth_mm, ctx.mu,
            ctx.need.flow_ul_min)
    elif ctx.need.length_mm is not None:
        length = float(ctx.need.length_mm)
    else:
        raise PartSizingError(
            "flow_restrictor",
            "needs a target pressure_drop_pa (or an explicit length_mm): a restrictor "
            "with no resistance target is not a design, it is a narrowing")
    if length < throat:
        raise PartSizingError(
            "flow_restrictor",
            f"the target needs only {length:.3g} mm of throat, shorter than the "
            f"{throat:g} mm throat itself, so it would be an orifice and not a duct: at "
            f"Re << 1 an abrupt area change costs almost nothing (blocks.json "
            f"sudden_loss_element). Ask for more pressure drop or a narrower depth.")
    lane, why = lane_width_mm(ctx, "approach")
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane("restrictor throat width", throat)
    f.enclosed("restrictor throat", throat)
    # Resistance is linear in length, so a demanding target runs long: 200 Pa at 5 uL/min
    # in a 0.2 x 0.3 mm throat is 348 mm of it. Fold it rather than refuse, exactly as a
    # delay line is folded - the resistance is the same whether the duct is straight.
    fold = None
    if length + 2 * lane > ctx.usable_x_mm:
        fold = fold_into_lanes(length, throat, contracts.floor("min_wall_mm"),
                               ctx.usable_x_mm, "flow_restrictor")
        f.wall("rib between folded throat lanes", fold.rib_mm)
        x, y = fold.footprint_mm
        why.append(f"throat folded into {fold.lanes} lanes: {r(length)} mm of throat does "
                   f"not fit the {r(ctx.usable_x_mm)} mm available straight")
    else:
        x = r(length + 2 * lane)
        y = r(max(lane, throat))
    f.fits(x, y)
    check_particle_depth(ctx, ctx.depth_mm, "throat")
    vol = serpentine_volume_ul(length, throat, ctx.depth_mm)
    if fold is None:
        vol += 2 * serpentine_volume_ul(lane, lane, ctx.depth_mm)
    dp = physics.pressure_drop_pa(length, throat, ctx.depth_mm, ctx.mu, ctx.need.flow_ul_min)
    res = physics.rect_resistance_pa_s_per_m3(length, throat, ctx.depth_mm, ctx.mu)
    pid = ctx.prefix("restrictor")
    entry = channel_entry(
        snake(pid), "flow_restrictor", snake(pid, "in"), snake(pid, "out"), throat,
        ctx.depth_mm, carries=ctx.need.carries,
        path=fold.path() if fold else [[0.0, r(y / 2.0)], [x, r(y / 2.0)]],
        note=f"RESISTANCE element: {throat:g} mm throat over {r(length)} mm, "
             f"{dp:.3g} Pa at {ctx.need.flow_ul_min:g} uL/min. Does not stop a front - "
             f"that is phase_guide.")
    phys = {
        "function": "resistance", "stops_flow": False,
        "throat_width_mm": throat, "throat_length_mm": r(length),
        "folded_lanes": None if fold is None else fold.lanes,
        "pressure_drop_pa": round(dp, 4),
        "resistance_pa_s_per_m3": f"{res:.4g}",
        "target_pressure_drop_pa": ctx.need.pressure_drop_pa,
        "not_to_be_confused_with": (
            "phase_guide, which is a CAPILLARY STOP and sets no resistance"),
        **shear_report(ctx, throat, "throat width"),
    }
    if fold is None:
        ops = (Opening("inlet", (0.0, r(y / 2.0)), "-x", lane, "inlet", ctx.need.carries),
               Opening("outlet", (x, r(y / 2.0)), "+x", lane, "outlet", ctx.need.carries))
    else:
        last = fold.lanes - 1
        ops = (Opening("inlet", (0.0, r(throat / 2.0)), "-x", throat, "inlet",
                       ctx.need.carries),
               Opening("outlet",
                       (fold.run_mm if last % 2 == 0 else 0.0,
                        r(throat / 2.0 + last * (throat + fold.rib_mm))),
                       "+x" if last % 2 == 0 else "-x", throat, "outlet",
                       ctx.need.carries))
    feats = (Feature("throat", 1, (r(length), throat, ctx.depth_mm), None, throat,
                     "the constriction itself; resistance scales as 1/w and 1/h^3, so "
                     "depth is the strong lever and we have none left"),)
    return ((x, y), vol, ops, feats,
            (Expansion("channel", entry, r(vol), "throat + approaches"),), phys)


def size_metering_trap(ctx: Ctx):
    """Captures a FIXED VOLUME: the block for metered splitting.

    A pocket of exactly the metered volume, a capillary-stop vent so it fills and stops,
    and a drain. The honest output here is the TOLERANCE: the volume is set by an etch
    depth with a +/-20 um process tolerance, so a 0.3 mm pocket meters to about +/-7%
    whatever its area. That is the number a protocol should carry, not the nominal.
    """
    volume = required_volume_ul(ctx)
    lane, why = lane_width_mm(ctx, "pocket width")
    side = math.sqrt(max(volume / ctx.depth_mm, 1e-9))
    y = r(max(lane, side))
    x = r(volume / (y * ctx.depth_mm))
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane("metering pocket width", y)
    f.in_plane("metering pocket length", x)
    f.enclosed("metering pocket", y)
    f.fits(x, y)
    check_particle_depth(ctx, ctx.depth_mm, "metering pocket")
    vol = box_volume_ul(x, y, ctx.depth_mm)
    z_tol = derived.z_tolerance_um()
    tol_pct = 100.0 * z_tol / (ctx.depth_mm * 1000.0)
    ctx.cite("contracts/blocks.json retention.weir_geometry.z_tolerance_warning")
    ctx.risk(
        f"the metered volume is set by the etch depth, so process z tolerance "
        f"+/-{z_tol:g} um on a {ctx.depth_mm * 1000:g} um layer makes it "
        f"+/-{tol_pct:.1f}% ({r(vol * tol_pct / 100)} uL on {r(vol)} uL) whatever the "
        f"area. Calibrate the pocket by weight before trusting the nominal volume.")
    n_p, pitch, dia, notes = pillars_for_span(ctx, x, y)
    if n_p:
        vol = pillared_volume_ul(x, y, ctx.depth_mm, n_p, dia)
    pid = ctx.prefix("meter")
    entry = chamber_entry(
        snake(pid), "split", "metering_trap", (x, y, ctx.depth_mm), vol,
        height_um=r(ctx.depth_mm * 1000),
        note=f"fixed-volume pocket, {r(vol)} uL +/-{tol_pct:.1f}% from z tolerance. Fills "
             f"until the vent's capillary stop holds the front, then the excess bypasses.")
    phys = {
        "metered_volume_ul": r(vol), "nominal_volume_ul": r(volume),
        "volume_tolerance_percent": round(tol_pct, 2),
        "volume_tolerance_ul": r(vol * tol_pct / 100.0),
        "tolerance_source": "etch depth z tolerance; area is in-plane and far more precise",
        "width_set_by": why, "lid_support": notes,
        **shear_report(ctx, y, "pocket width"),
    }
    if ctx.need.flow_ul_min is not None:
        phys["fill_time_s"] = round(physics.residence_time_s(vol, ctx.need.flow_ul_min), 2)
        phys.update(sedimentation_report(ctx, phys["fill_time_s"], "metering pocket", intended=True))
    ops = (
        Opening("inlet", (0.0, r(y / 2.0)), "-x", lane, "inlet", ctx.need.carries),
        Opening("outlet", (x, r(y / 2.0)), "+x", lane, "outlet", ctx.need.carries),
        Opening("vent", (r(x / 2.0), y), "+y", contracts.floor("min_open_channel_mm"),
                "control", "liquid",
                "capillary stop (phase_guide) sits here: it defines where the fill stops, "
                "which is what makes the volume fixed"),
    )
    feats = [Feature("metering_pocket", 1, (x, y, ctx.depth_mm), None, min(x, y),
                     "the metered volume itself; its accuracy is a z accuracy")]
    if n_p:
        feats.append(Feature("lid_support_pillar", n_p, (dia, dia, ctx.depth_mm), pitch,
                             dia, notes[0]))
    return ((x, y), vol, ops, tuple(feats),
            (Expansion("chamber", entry, r(vol), "box minus pillars"),), phys)


def size_pillar_array(ctx: Ctx):
    """Regular pillars: lid support, or coarse filtration. NOT capsule retention.

    blocks.json is explicit: pillar_array "cannot retain by size exclusion at a 200 um
    throat". A 70 um capsule, swollen or not, goes straight through. That is the whole
    reason the depth weir exists, so this sizer says so on every part it returns.
    """
    volume = required_volume_ul(ctx)
    lane, why = lane_width_mm(ctx, "region width")
    side = math.sqrt(max(volume / ctx.depth_mm, 1e-9))
    y = r(max(lane, side))
    tight, loose = derived.lid_support_pitch_mm()
    dia = max(contracts.floor("min_wall_mm"), contracts.floor("min_open_channel_mm"))

    def displaced(x_mm: float, y_mm: float):
        nx = max(0, int(math.floor(x_mm / tight)))
        ny = max(0, int(math.floor(y_mm / tight)))
        n = nx * ny
        return n * cylinder_volume_ul(dia, dia, ctx.depth_mm), n, tight, dia

    x, y, n_p, pitch, dia = solve_box_chamber(ctx, volume, y, displaced)
    gap = r(pitch - dia)
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane("gap between pillars", gap)
    f.wall("pillar diameter", dia)
    f.enclosed("pillared region", gap)
    f.fits(x, y)
    check_particle_depth(ctx, ctx.depth_mm, "pillared region")
    vol = pillared_volume_ul(x, y, ctx.depth_mm, n_p, dia)
    ctx.cite("contracts/blocks.json retention.not_viable", "contracts/blocks.json "
             "target_envelope.design_rules_at_this_scale")
    ctx.risk(
        f"this array supports the lid and strains coarse debris; it CANNOT retain by size "
        f"exclusion. Its throat is {gap:g} mm = {gap * 1000:g} um and a capsule is "
        f"{contracts.capsule_diameter_um():g} um ({contracts.swollen_capsule_diameter_um():g} "
        f"um swollen), so everything passes (blocks.json retention.not_viable). Use "
        f"trap_chamber with a depth weir to retain.")
    pid = ctx.prefix("pillars")
    entry = chamber_entry(
        snake(pid), "filter", "pillar_array", (x, y, ctx.depth_mm), vol,
        height_um=r(ctx.depth_mm * 1000),
        filter={"type": "pillars", "gap_um": r(gap * 1000), "purpose": "capsule_aggregates"},
        note=f"{n_p} pillars of {dia:g} mm at {pitch:g} mm pitch holding a "
             f"{ctx.depth_mm:g} mm lid flat over {x:g} x {y:g} mm. Volume is the box minus "
             f"the pillars. Not a retention feature.")
    phys = {"pillars": n_p, "pitch_mm": pitch, "pillar_diameter_mm": dia,
            "gap_mm": gap, "gap_um": r(gap * 1000),
            "support_pitch_band_mm": [tight, loose],
            "retains_capsules": False, "width_set_by": why,
            "open_area_fraction": round(vol / box_volume_ul(x, y, ctx.depth_mm), 4),
            **shear_report(ctx, y, "region width across the flow")}
    ops = (Opening("inlet", (0.0, r(y / 2.0)), "-x", y, "inlet", ctx.need.carries),
           Opening("outlet", (x, r(y / 2.0)), "+x", y, "outlet", ctx.need.carries))
    feats = (Feature("pillar", n_p, (dia, dia, ctx.depth_mm), pitch, dia,
                     f"cylindrical pillar, full depth, floor to lid; {gap:g} mm gaps"),)
    return ((x, y), vol, ops, feats,
            (Expansion("chamber", entry, r(vol), "box minus every pillar"),), phys)


def size_dld_array(ctx: Ctx):
    """Deterministic lateral displacement, as a CONTINUOUS WASH and not as retention.

    blocks.json ranks it first among alternatives for exactly that: at a 200 um gap and a
    0.125 row shift the critical diameter is about 105 um, so a swollen capsule displaces
    and an unswollen one does not. This computes the critical diameter from the Davis
    correlation and reports the contract's worked point beside it, so neither is trusted
    alone.
    """
    ref = derived.dld_reference()
    ctx.cite(ref.source)
    gap = max(contracts.floor("min_open_channel_mm"), ref.gap_um / 1000.0)
    dc = physics.dld_critical_diameter_um(gap * 1000.0, ref.epsilon)
    volume = required_volume_ul(ctx)
    lane, why = lane_width_mm(ctx, "array width")
    post = max(contracts.floor("min_wall_mm"), contracts.floor("min_open_channel_mm"))
    pitch = gap + post
    side = math.sqrt(max(volume / ctx.depth_mm, 1e-9))
    y = r(max(lane, side))
    x = r(volume / (y * ctx.depth_mm))
    rows = max(1, int(math.floor(x / pitch)))
    cols = max(1, int(math.floor(y / pitch)))
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane("DLD gap", gap)
    f.wall("DLD post", post)
    f.enclosed("DLD gap", gap)
    f.fits(x, y)
    check_particle_depth(ctx, ctx.depth_mm, "DLD array")
    n_posts = rows * cols
    vol = pillared_volume_ul(x, y, ctx.depth_mm, n_posts, post)
    disagreement = abs(dc - ref.critical_diameter_um) / ref.critical_diameter_um
    ctx.risk(
        f"DLD needs +/-{ref.gap_tolerance_um:g} um gap tolerance and process z tolerance "
        f"alone is +/-{derived.z_tolerance_um():g} um; the in-plane gap is more forgiving "
        f"than the depth, but this is a tight part",
        f"it SORTS rather than retains: particles below the {dc:.0f} um critical diameter "
        f"follow the flow and leave. Use it to wash continuously, never to hold.")
    if disagreement > 0.15:
        ctx.risk(f"the Davis correlation gives {dc:.0f} um here but blocks.json's worked "
                 f"point says ~{ref.critical_diameter_um:g} um at the same gap and row "
                 f"shift: a {100 * disagreement:.0f}% disagreement worth resolving before "
                 f"committing")
    pid = ctx.prefix("dld")
    entry = chamber_entry(
        snake(pid), "cell_wash", "dld_array", (x, y, ctx.depth_mm), vol,
        height_um=r(ctx.depth_mm * 1000),
        filter={"type": "pillars", "gap_um": r(gap * 1000), "purpose": "capsule_aggregates"},
        note=f"DLD continuous wash: {rows} x {cols} posts of {post:g} mm, {gap:g} mm gaps, "
             f"row shift {ref.epsilon:g}; critical diameter {dc:.0f} um (contract's worked "
             f"point ~{ref.critical_diameter_um:g} um).")
    phys = {"critical_diameter_um": round(dc, 1),
            "contract_critical_diameter_um": ref.critical_diameter_um,
            "correlation": "Davis: Dc = 1.4 g eps^0.48",
            "gap_mm": r(gap), "post_mm": r(post), "row_shift": ref.epsilon,
            "rows": rows, "columns": cols, "posts": n_posts,
            "gap_tolerance_um": ref.gap_tolerance_um,
            "function": "continuous_wash", "retains": False, "width_set_by": why,
            **shear_report(ctx, y, "array width across the flow")}
    ops = (Opening("sample_in", (0.0, r(y / 4.0)), "-x", r(y / 2.0), "inlet",
                   ctx.need.carries),
           Opening("buffer_in", (0.0, r(3 * y / 4.0)), "-x", r(y / 2.0), "inlet", "liquid"),
           Opening("displaced_out", (x, r(3 * y / 4.0)), "+x", r(y / 2.0), "outlet",
                   ctx.need.carries, f"everything above {dc:.0f} um"),
           Opening("zigzag_out", (x, r(y / 4.0)), "+x", r(y / 2.0), "waste", "liquid",
                   f"everything below {dc:.0f} um, with the old buffer"))
    feats = (Feature("dld_post", n_posts, (post, post, ctx.depth_mm), r(pitch), post,
                     f"post array with a {ref.epsilon:g} row shift; {gap:g} mm gaps"),)
    return ((x, y), vol, ops, feats,
            (Expansion("chamber", entry, r(vol), "box minus every post"),), phys)


# --- chambers ----------------------------------------------------------------

def _plain_chamber(ctx: Ctx, block: str, chamber_type: str, note: str, *,
                   culture: bool = False, shape: str = "box"):
    """A chamber whose only job is to hold a volume at a legal shear.

    The lane width is the dimension ACROSS the flow, and that is what shear is computed
    on. For an adherent-cell chamber that inverts: the ceiling fixes the width and the
    volume then fixes the flow path length - which is exactly how Module 6's 1.2 x 8 mm
    field comes out, flow crossing the 1.2 mm dimension.
    """
    volume = required_volume_ul(ctx)
    lane, why = lane_width_mm(ctx, "chamber width across flow")
    # WIDE AND SHORT, never narrow and long. Residence time is V/Q whatever the shape, so
    # the aspect ratio is free - and the contract spends it on width: a wider lane is a
    # lower velocity, lower shear and a shorter path, and the design rule is to replicate
    # chambers rather than lengthen them. A square is the shortest path for the area.
    square = r(math.sqrt(max(volume / ctx.depth_mm, 1e-9)))
    if square > lane:
        lane = max(lane, square)
        why.append(f"square-ish footprint ({square:g} mm): shortest flow path for this "
                   f"volume, which is what the contract's replicate-don't-lengthen rule "
                   f"wants")

    def displaced(x_mm: float, y_mm: float):
        count, pitch, dia, _ = pillars_for_span(ctx, x_mm, y_mm)
        return count * cylinder_volume_ul(dia, dia, ctx.depth_mm), count, pitch, dia

    x, y, n_p, pitch, dia = solve_box_chamber(ctx, volume, lane, displaced)
    _, _, _, notes = pillars_for_span(ctx, x, y)
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane(f"{block} width across the flow", y)
    f.in_plane(f"{block} flow path length", x)
    f.enclosed(block, y)
    f.fits(x, y)
    if culture:
        h_min = float(contracts.param("culture.chamber_height_min_um")) / 1000.0
        ctx.cite("contracts/parameters.json culture.chamber_height_min_um")
        if ctx.depth_mm < h_min - 1e-9:
            raise PartSizingError(
                block, f"culture needs at least {h_min * 1000:g} um of height and this chip "
                       f"is {ctx.depth_mm * 1000:g} um deep")
    check_particle_depth(ctx, ctx.depth_mm, f"{block} height", culture=culture)
    if shape == "cylinder":
        vol = cylinder_volume_ul(x, y, ctx.depth_mm) - \
            n_p * cylinder_volume_ul(dia, dia, ctx.depth_mm)
        formula = "pi/4 x dx x dy x depth minus pillars"
    else:
        vol = pillared_volume_ul(x, y, ctx.depth_mm, n_p, dia)
        formula = "box minus pillars"
    pid = ctx.prefix(block)
    entry = chamber_entry(
        snake(pid), chamber_type, block, (x, y, ctx.depth_mm), vol, shape=shape,
        height_um=r(ctx.depth_mm * 1000), note=note + (
            f" {n_p} lid-support pillars at {pitch:g} mm pitch." if n_p else ""))
    phys = {"flow_path_mm": x, "width_across_flow_mm": y, "width_set_by": why,
            "lid_support": notes, "pillars": n_p,
            **shear_report(ctx, y, "chamber width across the flow")}
    if ctx.need.flow_ul_min is not None:
        phys["residence_s"] = round(physics.residence_time_s(vol, ctx.need.flow_ul_min), 2)
        phys["pressure_drop_pa"] = round(physics.pressure_drop_pa(
            x, y, ctx.depth_mm, ctx.mu, ctx.need.flow_ul_min), 4)
        phys.update(sedimentation_report(ctx, phys["residence_s"], block, intended=True))
    if culture:
        ctx.cite("contracts/blocks.json target_envelope.design_rules_at_this_scale "
                 "(keep chamber length constant and replicate)")
        ctx.risk("residence time is NOT scale-free: at fixed shear the velocity is fixed, "
                 "so a longer chamber holds fluid proportionally longer. To scale up, "
                 "REPLICATE this chamber rather than lengthening it.")
    ops = (
        Opening("inlet", (0.0, r(y / 2.0)), "-x", y, "inlet", ctx.need.carries,
                f"full-width feed across the {y:g} mm edge: a point feed would channel"),
        Opening("outlet", (x, r(y / 2.0)), "+x", y, "outlet", ctx.need.carries),
    )
    feats = tuple(
        [Feature("lid_support_pillar", n_p, (dia, dia, ctx.depth_mm), pitch, dia, notes[0])]
        if n_p else [])
    return ((x, y), vol, ops, feats,
            (Expansion("chamber", entry, r(vol), formula),), phys)


def size_polygon_chamber(ctx: Ctx):
    return _plain_chamber(ctx, "polygon_chamber", "exchange",
                          "general-purpose chamber, straight walls.")


def size_culture_chamber(ctx: Ctx):
    return _plain_chamber(
        ctx, "culture_chamber", "culture",
        "culture field. The shear ceiling fixes the width across the flow and the volume "
        "then fixes the flow path, so flow crosses the SHORT dimension (Module 6).",
        culture=True)


def size_organoid_chamber(ctx: Ctx):
    if ctx.particle_um is None:
        raise PartSizingError(
            "organoid_chamber",
            "needs particle_um: the whole point of this block is being sized for a "
            "specific 3D construct, and no construct diameter is in the contracts")
    return _plain_chamber(
        ctx, "organoid_chamber", "culture",
        f"chamber sized for a {ctx.particle_um:g} um 3D construct.", culture=True,
        shape="cylinder")


def size_wells_array(ctx: Ctx):
    """An array of discrete wells, each holding the need's volume."""
    ctx.need.require("volume_ul", "branches")
    n = int(ctx.need.branches)
    per = float(ctx.need.volume_ul)
    d = math.sqrt(per / ctx.depth_mm * 4.0 / math.pi)
    wall = contracts.floor("min_wall_mm")
    pitch = d + wall
    cols = max(1, int(math.floor(math.sqrt(n))))
    rows = int(math.ceil(n / cols))
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane("well diameter", r(d))
    f.wall("wall between wells", wall)
    x, y = r(cols * pitch), r(rows * pitch)
    f.fits(x, y)
    check_particle_depth(ctx, ctx.depth_mm, "well")
    vol = n * cylinder_volume_ul(d, d, ctx.depth_mm)
    pid = ctx.prefix("wells")
    entries = tuple(
        Expansion("chamber", chamber_entry(
            snake(pid, i + 1), "well", "wells_array", (r(d), r(d), ctx.depth_mm),
            cylinder_volume_ul(d, d, ctx.depth_mm), shape="cylinder",
            center=(r(pitch * (i % cols) + pitch / 2), r(pitch * (i // cols) + pitch / 2),
                    r(ctx.depth_mm / 2)),
            note=f"well {i + 1} of {n}, {r(per)} uL"),
            r(cylinder_volume_ul(d, d, ctx.depth_mm)), "pi/4 x d x d x depth")
        for i in range(n))
    phys = {"wells": n, "well_diameter_mm": r(d), "pitch_mm": r(pitch),
            "rows": rows, "columns": cols, "volume_per_well_ul": r(per),
            **shear_report(ctx, r(d), "well diameter")}
    ops = tuple(Opening(f"well_{i + 1}", (r(pitch * (i % cols) + pitch / 2), 0.0), "-y",
                        r(d), "inlet", ctx.need.carries) for i in range(min(n, 6)))
    feats = (Feature("well", n, (r(d), r(d), ctx.depth_mm), r(pitch), r(d),
                     "discrete well, full depth"),)
    return ((x, y), vol, ops, feats, entries, phys)


# --- junctions and distribution ---------------------------------------------

def _junction(ctx: Ctx, block: str, arms: tuple[tuple[str, str, str], ...], *,
              stub_factor: float = 1.0, note: str, split_axis: bool = False):
    """A node with stubs. Junction geometry is routing, not precision: the sizer's job is
    to get the lane width, the shear and the floors right, and to publish openings the
    router can reach.
    """
    lane, why = lane_width_mm(ctx, "arm")
    refuse_mixing(ctx)
    stub = r(max(lane * stub_factor, contracts.floor("min_open_channel_mm")))
    wall = contracts.floor("min_wall_mm")
    dirs = {d for _, d, _ in arms}
    span_x = lane + stub * (("-x" in dirs) + ("+x" in dirs))
    span_y = lane + stub * (("-y" in dirs) + ("+y" in dirs))
    if split_axis:
        span_y = max(span_y, 2 * lane + wall)
    span_x, span_y = r(span_x), r(span_y)
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane(f"{block} arm width", lane)
    if split_axis:
        f.wall(f"{block} wall between the split arms", wall)
    f.enclosed(f"{block} arm", lane)
    f.fits(span_x, span_y)
    check_particle_depth(ctx, ctx.depth_mm, "junction arm")
    node = serpentine_volume_ul(lane, lane, ctx.depth_mm)
    vol = node + len(arms) * serpentine_volume_ul(stub, lane, ctx.depth_mm)
    pid = ctx.prefix(block)
    entry = chamber_entry(snake(pid), "junction", block, (span_x, span_y, ctx.depth_mm),
                          vol, height_um=r(ctx.depth_mm * 1000), note=note)
    phys = {"arms": len(arms), "arm_width_mm": lane, "stub_mm": stub,
            "width_set_by": why, **shear_report(ctx, lane, "arm width")}
    if ctx.need.flow_ul_min is not None:
        phys.update(sedimentation_report(ctx, round(physics.residence_time_s(
            vol, ctx.need.flow_ul_min), 3), block))
    ops = []
    for name, d, role in arms:
        if d == "-x":
            pos = (0.0, r(span_y / 2.0))
        elif d == "+x":
            pos = (span_x, r(span_y / 2.0))
        elif d == "-y":
            pos = (r(span_x / 2.0), 0.0)
        else:
            pos = (r(span_x / 2.0), span_y)
        ops.append(Opening(name, pos, d, lane, role, ctx.need.carries))
    if split_axis:
        ops = []
        ops.append(Opening("inlet", (0.0, r(span_y / 2.0)), "-x", lane, "inlet",
                           ctx.need.carries))
        for i, (name, _, role) in enumerate(a for a in arms if a[2] != "inlet"):
            y = r(lane / 2.0 + i * (lane + wall))
            ops.append(Opening(name, (span_x, y), "+x", lane, role, ctx.need.carries))
    return ((span_x, span_y), vol, tuple(ops), (),
            (Expansion("chamber", entry, r(vol), "node + every stub"),), phys)


def size_y_junction(ctx: Ctx):
    return _junction(ctx, "y_junction",
                     (("inlet", "-x", "inlet"), ("branch_1", "+x", "outlet"),
                      ("branch_2", "+x", "outlet")), split_axis=True,
                     note="two into one or one into two. A sharp Y has a shear peak at "
                          "the apex; curved_y_junction is the gentler form.")


def size_curved_y_junction(ctx: Ctx):
    ctx.risk("swept arms lower the apex shear, which is why this block exists; it costs "
             "roughly twice the footprint of a sharp y_junction")
    return _junction(ctx, "curved_y_junction",
                     (("inlet", "-x", "inlet"), ("branch_1", "+x", "outlet"),
                      ("branch_2", "+x", "outlet")), stub_factor=2.0, split_axis=True,
                     note="Y with swept arms: lower shear at the apex than a sharp Y.")


def size_fork_junction(ctx: Ctx):
    return _junction(ctx, "fork_junction",
                     (("inlet", "-x", "inlet"), ("branch_1", "+x", "outlet"),
                      ("branch_2", "+x", "outlet")), split_axis=True,
                     note="symmetric 1-to-2 split: equal arms, so equal flow by symmetry.")


def size_t_junction(ctx: Ctx):
    return _junction(ctx, "t_junction",
                     (("inlet", "-x", "inlet"), ("side", "-y", "reagent"),
                      ("outlet", "+x", "outlet")),
                     note="three-way tee: add a reagent to a flowing stream. The streams "
                          "leave side by side and do NOT mix here - follow with "
                          "herringbone_mixer if mixing is required.")


def size_cross_junction(ctx: Ctx):
    return _junction(ctx, "cross_junction",
                     (("inlet", "-x", "inlet"), ("sheath_1", "-y", "reagent"),
                      ("sheath_2", "+y", "reagent"), ("outlet", "+x", "outlet")),
                     note="four-way cross. With the side arms as sheath it focuses the "
                          "centre stream; see flow_focusing for droplet generation.")


def size_network_node(ctx: Ctx):
    return _junction(ctx, "network_node",
                     (("arm_1", "-x", "inlet"), ("arm_2", "+x", "outlet"),
                      ("arm_3", "+y", "outlet")),
                     note="three-arm routing node. On one fluidic layer this is how a "
                          "path branches without a crossing.")


def size_ring_junction(ctx: Ctx):
    """Closed loop with taps: recirculation. Volume is the annulus, not the disc."""
    volume = required_volume_ul(ctx)
    lane, why = lane_width_mm(ctx, "ring channel")
    refuse_mixing(ctx)
    d_mean = volume / (math.pi * lane * ctx.depth_mm)
    if d_mean < 4 * lane:
        raise PartSizingError(
            "ring_junction",
            f"a {volume:g} uL ring at a {lane:g} mm channel comes out {d_mean:.3g} mm "
            f"across, under four channel widths: the taps would overlap. Use a "
            f"polygon_chamber for a volume this small.")
    outer = r(d_mean + lane)
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane("ring channel width", lane)
    f.enclosed("ring channel", lane)
    f.fits(outer, outer)
    check_particle_depth(ctx, ctx.depth_mm, "ring channel")
    length = math.pi * d_mean
    vol = serpentine_volume_ul(length, lane, ctx.depth_mm)
    pid = ctx.prefix("ring")
    entry = chamber_entry(snake(pid), "pool", "ring_junction",
                          (outer, outer, ctx.depth_mm), vol, shape="cylinder",
                          height_um=r(ctx.depth_mm * 1000),
                          mixer={"kind": "serpentine", "channel_width_mm": lane,
                                 "turns": 1, "length_mm": r(length)},
                          note=f"closed loop, {r(d_mean)} mm mean diameter, {r(length)} mm "
                               f"circumference. Volume is the annulus.")
    phys = {"mean_diameter_mm": r(d_mean), "circumference_mm": r(length),
            "channel_width_mm": lane, "width_set_by": why,
            **shear_report(ctx, lane, "ring channel width")}
    if ctx.need.flow_ul_min is not None:
        phys["lap_time_s"] = round(physics.residence_time_s(vol, ctx.need.flow_ul_min), 2)
        phys.update(sedimentation_report(ctx, phys["lap_time_s"], "ring"))
    ops = (Opening("tap_w", (0.0, r(outer / 2)), "-x", lane, "inlet", ctx.need.carries),
           Opening("tap_e", (outer, r(outer / 2)), "+x", lane, "outlet", ctx.need.carries),
           Opening("tap_s", (r(outer / 2), 0.0), "-y", lane, "inlet", ctx.need.carries),
           Opening("tap_n", (r(outer / 2), outer), "+y", lane, "outlet", ctx.need.carries))
    return ((outer, outer), vol, ops, (),
            (Expansion("chamber", entry, r(vol), "circumference x width x depth"),), phys)


def _header(ctx: Ctx, block: str, *, matched: bool, note: str, fractions: bool = False):
    """One-to-many: flow_manifold (header), flow_distributor (matched paths), flow_divider.

    The distinction matters hydraulically. A header's branches all hang off one spine, so
    the near branch sees less resistance and takes more flow unless the branches dominate
    the spine. Matched path lengths (an H-tree) make the split equal by construction,
    which is why flow_distributor exists as a separate block.
    """
    ctx.need.require("branches")
    n = int(ctx.need.branches)
    if n < 2:
        raise PartSizingError(block, f"{n} branches is not a split")
    lane, why = lane_width_mm(ctx, "branch")
    wall = contracts.floor("min_wall_mm")
    pitch = lane + wall
    total_flow = ctx.need.flow_ul_min
    spine = lane
    if ctx.ceiling.ceiling_dyn_cm2 is not None and total_flow is not None:
        spine = max(spine, physics.lane_width_for_shear_mm(
            total_flow, ctx.depth_mm, ctx.mu, ctx.ceiling.ceiling_dyn_cm2))
    spine = r(max(spine, contracts.floor("min_port_mm")))
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane(f"{block} branch width", lane)
    f.in_plane(f"{block} spine width", spine)
    f.wall(f"{block} wall between branches", wall)
    f.enclosed(f"{block} branch", lane)
    check_particle_depth(ctx, ctx.depth_mm, "branch")

    y = r(n * pitch)
    if matched:
        levels = int(math.log2(n))
        if 2 ** levels != n:
            raise PartSizingError(
                block, f"matched path lengths come from a binary tree, so the branch count "
                       f"must be a power of two; {n} is not. Use flow_manifold for an "
                       f"arbitrary count and accept the uneven split, or ask for "
                       f"{2 ** levels} or {2 ** (levels + 1)} branches.")
        stage = r(max(2 * pitch, 2 * lane))
        x = r(levels * stage + lane)
        seg = 0.0
        for k in range(levels):
            seg += 2 ** k * (stage + y / (2 ** (k + 1)))
        length = seg
        branches_note = (f"H-tree, {levels} levels, every path the same length, so the "
                         f"split is equal by construction")
    else:
        x = r(spine + lane * 3)
        length = y + n * lane * 3
        branches_note = (f"header: {n} branches off one {spine:g} mm spine")
    f.fits(x, y)
    vol = serpentine_volume_ul(length, lane, ctx.depth_mm)
    pid = ctx.prefix(block)
    entry = channel_entry(
        snake(pid), block, snake(pid, "in"), snake(pid, "out"), spine, ctx.depth_mm,
        carries=ctx.need.carries, path=[[0.0, r(y / 2)], [x, r(y / 2)]],
        note=note + " " + branches_note + ".")
    phys = {"branches": n, "branch_width_mm": lane, "spine_width_mm": spine,
            "pitch_mm": r(pitch), "matched_path_lengths": matched,
            "total_path_mm": r(length), "width_set_by": why,
            **shear_report(ctx, spine, "spine width")}
    if total_flow is not None:
        per = total_flow / n
        phys["flow_per_branch_ul_min"] = round(per, 4)
        phys["branch_wall_shear_dyn_cm2"] = round(physics.wall_shear_dyn_cm2(
            per, lane, ctx.depth_mm, ctx.mu), 6)
        if not matched:
            b_dp = physics.pressure_drop_pa(lane * 3, lane, ctx.depth_mm, ctx.mu, per)
            s_dp = physics.pressure_drop_pa(y, spine, ctx.depth_mm, ctx.mu, total_flow)
            ratio = b_dp / max(s_dp, 1e-12)
            phys["branch_to_spine_resistance_ratio"] = round(ratio, 2)
            if ratio < 5:
                ctx.risk(f"branches cost only {ratio:.2g}x the spine, so the near branch "
                         f"takes more flow than the far one. flow_distributor's matched "
                         f"paths fix this by construction; a header does not.")
        phys.update(sedimentation_report(ctx, round(physics.residence_time_s(
            vol, total_flow), 3), block))
    if fractions:
        phys["fractions"] = "equal as drawn; unequal fractions come from branch LENGTH " \
                            "ratios, since resistance is linear in length"
    ops = tuple([Opening("inlet", (0.0, r(y / 2)), "-x", spine, "inlet", ctx.need.carries)] +
                [Opening(f"branch_{i + 1}", (x, r(pitch * i + pitch / 2)), "+x", lane,
                         "outlet", ctx.need.carries) for i in range(n)])
    feats = (Feature("branch", n, (r(x - spine), lane, ctx.depth_mm), r(pitch), lane,
                     branches_note),)
    return ((x, y), vol, ops, feats,
            (Expansion("channel", entry, r(vol), "spine + every branch"),), phys)


def size_flow_manifold(ctx: Ctx):
    return _header(ctx, "flow_manifold", matched=False, note="one-to-many header.")


def size_flow_distributor(ctx: Ctx):
    return _header(ctx, "flow_distributor", matched=True,
                   note="one-to-many with MATCHED path lengths.")


def size_flow_divider(ctx: Ctx):
    return _header(ctx, "flow_divider", matched=False, fractions=True,
                   note="splits a stream into set fractions.")


def size_flow_focusing(ctx: Ctx):
    """Sheath-flow focusing junction: droplet generation.

    THIS IS WHERE THE ENCAPSULATION NOZZLE MEETS THE FABRICATION FLOOR, and they do not
    fit. contracts/parameters.json puts the nozzle at 40 um; the in-plane floor is 200 um.
    A nozzle is an in-plane width, so a 40 um nozzle is not printable here - and since a
    flow-focused droplet comes out roughly the size of its nozzle, the smallest capsule
    this process can make is about 200 um, not 70.

    So this sizer refuses the contract nozzle and says what the levers are, rather than
    emitting a part nobody can print.
    """
    floor_mm = contracts.floor("min_open_channel_mm")
    want_um = float(contracts.param("encapsulation.nozzle_um"))
    ctx.cite("contracts/parameters.json encapsulation.nozzle_um",
             "contracts/blocks.json fabrication_floors.min_open_channel_mm",
             "contracts/parameters.json capsule.diameter_tunable_um")
    target_um = ctx.particle_um if ctx.particle_um is not None else want_um
    if target_um < floor_mm * 1000.0:
        lo, hi = contracts.param_range("capsule.diameter_tunable_um")
        raise PartSizingError(
            "flow_focusing",
            f"a nozzle for a {target_um:g} um droplet is an IN-PLANE width of about "
            f"{target_um:g} um, below the {floor_mm * 1000:g} um in-plane floor, so it "
            f"cannot be printed or embossed here. The contract's own "
            f"encapsulation.nozzle_um is {want_um:g} um and hits the same wall. Two levers, "
            f"both in the contracts: make bigger capsules (capsule.diameter_tunable_um is "
            f"{lo:g}-{hi:g} um, and a {floor_mm * 1000:g} um nozzle gives roughly "
            f"{floor_mm * 1000:g} um capsules, inside that band), or keep encapsulation "
            f"off-chip. Note the depth is NOT the problem: z is free below "
            f"{contracts.max_depth_mm() * 1000:g} um.",
            floor="min_open_channel_mm", required=floor_mm * 1000.0, actual=target_um)
    nozzle = r(max(floor_mm, target_um / 1000.0))
    lane = r(max(nozzle * 3, contracts.floor("min_open_channel_mm")))
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane("focusing nozzle width", nozzle)
    f.in_plane("sheath arm width", lane)
    f.enclosed("nozzle", nozzle)
    x = r(lane * 2 + nozzle * 6)
    y = r(lane * 2 + nozzle)
    f.fits(x, y)
    vol = serpentine_volume_ul(nozzle * 6, nozzle, ctx.depth_mm) + \
        3 * serpentine_volume_ul(lane, lane, ctx.depth_mm)
    pid = ctx.prefix("focus")
    entry = chamber_entry(snake(pid), "junction", "flow_focusing", (x, y, ctx.depth_mm),
                          vol, height_um=r(ctx.depth_mm * 1000),
                          note=f"sheath-flow focusing: {nozzle * 1000:g} um nozzle, "
                               f"droplets about {nozzle * 1000:g} um.")
    phys = {"nozzle_um": r(nozzle * 1000), "droplet_um_approx": r(nozzle * 1000),
            "contract_nozzle_um": want_um,
            "in_plane_floor_um": r(floor_mm * 1000),
            "depth_is_not_the_limit": (
                "z is free below the layer depth; the nozzle is an IN-PLANE width"),
            **shear_report(ctx, nozzle, "nozzle width")}
    ctx.risk(f"this nozzle makes about {nozzle * 1000:g} um droplets, not the "
             f"{want_um:g} um the contract's encapsulation parameters assume; every "
             f"downstream dimension keyed to capsule diameter must be re-derived")
    ops = (Opening("core", (0.0, r(y / 2)), "-x", lane, "inlet", ctx.need.carries),
           Opening("sheath_1", (r(x / 2), 0.0), "-y", lane, "oil", "oil"),
           Opening("sheath_2", (r(x / 2), y), "+y", lane, "oil", "oil"),
           Opening("outlet", (x, r(y / 2)), "+x", r(nozzle * 2), "outlet", "droplets"))
    feats = (Feature("nozzle_throat", 1, (r(nozzle * 6), nozzle, ctx.depth_mm), None,
                     nozzle, "the throat the core stream is pinched through"),)
    return ((x, y), vol, ops, feats,
            (Expansion("chamber", entry, r(vol), "throat + three arms"),), phys)


def size_flow_focusing_funnel(ctx: Ctx):
    """Tapered funnel into a narrow throat. Hydrodynamic focusing, no droplets."""
    lane, why = lane_width_mm(ctx, "funnel mouth")
    throat = r(max(contracts.floor("min_open_channel_mm"), ctx.particle_floor_mm()))
    mouth = r(max(lane, throat * 4))
    taper = r(max(mouth, (mouth - throat) * 3))  # about 10 deg half angle
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane("funnel throat width", throat)
    f.in_plane("funnel mouth width", mouth)
    f.enclosed("funnel throat", throat)
    x = r(taper + throat * 3)
    f.fits(x, mouth)
    check_particle_depth(ctx, ctx.depth_mm, "throat")
    vol = (mouth + throat) / 2.0 * taper * ctx.depth_mm + \
        serpentine_volume_ul(throat * 3, throat, ctx.depth_mm)
    pid = ctx.prefix("funnel")
    entry = chamber_entry(snake(pid), "junction", "flow_focusing_funnel",
                          (x, mouth, ctx.depth_mm), vol,
                          height_um=r(ctx.depth_mm * 1000),
                          note=f"taper from {mouth:g} mm to a {throat:g} mm throat over "
                               f"{taper:g} mm. Volume is the trapezoid plus the throat.")
    phys = {"mouth_mm": mouth, "throat_mm": throat, "taper_length_mm": taper,
            "area_ratio": round(mouth / throat, 2), "width_set_by": why,
            "throat_wall_shear_dyn_cm2": None if ctx.need.flow_ul_min is None else round(
                physics.wall_shear_dyn_cm2(ctx.need.flow_ul_min, throat, ctx.depth_mm,
                                           ctx.mu), 6),
            **shear_report(ctx, throat, "throat width (the worst case in the part)")}
    ctx.risk(f"the throat is the shear maximum of the part: {mouth / throat:.1f}x the "
             f"mouth. A shear ceiling must be applied THERE, not at the mouth.")
    ops = (Opening("inlet", (0.0, r(mouth / 2)), "-x", mouth, "inlet", ctx.need.carries),
           Opening("outlet", (x, r(mouth / 2)), "+x", throat, "outlet", ctx.need.carries))
    feats = (Feature("taper", 1, (taper, mouth, ctx.depth_mm), None, throat,
                     f"straight taper, about 10 deg half angle, to avoid a separated "
                     f"corner where the cargo would stagnate"),
             Feature("throat", 1, (r(throat * 3), throat, ctx.depth_mm), None, throat, ""))
    return ((x, mouth), vol, ops, feats,
            (Expansion("chamber", entry, r(vol), "trapezoid + throat"),), phys)


def size_port(ctx: Ctx):
    """Through-hole to tubing, with the pad that stops it failing the port-overlap rule.

    assay2chip/README.md: check_chip_spec.py's newer rule is that a 0.8 mm port hole must
    open onto enough cut channel, and 60 pre-existing errors on main come from ports that
    do not. So a port here is the hole PLUS a pad under it, and the pad is what the
    channel joins.
    """
    hole = contracts.floor("min_port_mm")
    cargo = ctx.particle_floor_mm()
    if cargo > hole:
        hole = cargo
        ctx.risk(f"the port had to open to {hole:.3g} mm to pass {ctx.need.particles}")
    hole = r(hole)
    pad = r(hole * 1.5)
    lane, why = lane_width_mm(ctx, "stub")
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.port("port hole", hole)
    f.in_plane("port pad", pad)
    f.port_pitch("pad to the next port (minimum achievable)",
                 contracts.floor("min_port_pitch_mm"))
    x = r(pad + lane)
    f.fits(x, pad)
    vol = cylinder_volume_ul(pad, pad, ctx.depth_mm) + \
        serpentine_volume_ul(lane, lane, ctx.depth_mm)
    pid = ctx.prefix("port")
    entry = chamber_entry(
        snake(pid, "pad"), "inlet", "port", (pad, pad, ctx.depth_mm), vol, shape="cylinder",
        height_um=r(ctx.depth_mm * 1000),
        note=f"{hole:g} mm through-hole on a {pad:g} mm pad. The pad exists so the hole "
             f"opens onto cut channel, which is the port-overlap rule "
             f"tools/check_chip_spec.py enforces.")
    phys = {"hole_mm": hole, "pad_mm": pad,
            "min_port_pitch_mm": contracts.floor("min_port_pitch_mm"),
            "ports_per_chip_demo": contracts.max_ports_per_chip("demo"),
            "ports_per_chip_at_scale": contracts.max_ports_per_chip(ctx.need.scale),
            "width_set_by": why, **shear_report(ctx, lane, "stub width")}
    ops = (Opening("stub", (x, r(pad / 2)), "+x", lane, "outlet", ctx.need.carries,
                   "the only side a channel attaches: the rest of the pad is the hole"),)
    feats = (Feature("through_hole", 1, (hole, hole, ctx.depth_mm), None, hole,
                     "pierces every layer; the pad under it keeps it over cut channel"),)
    return ((x, pad), vol, ops, feats,
            (Expansion("chamber", entry, r(vol), "pi/4 x pad x pad x depth + stub"),), phys)


def size_text(ctx: Ctx):
    """Engraved label. Non-fluidic, so it expands to nothing and holds no volume."""
    ctx.need.require("length_mm")
    height = r(max(contracts.floor("min_open_channel_mm") * 5,
                   contracts.floor("min_wall_mm") * 4))
    stroke = contracts.floor("min_open_channel_mm")
    f = ctx.floors
    f.depth(ctx.depth_mm)
    f.in_plane("engraved stroke width", stroke)
    x = r(float(ctx.need.length_mm))
    f.fits(x, height)
    ops: tuple = ()
    feats = (Feature("engraved_stroke", 1, (x, height, ctx.depth_mm), None, stroke,
                     f"label relief at the {stroke:g} mm in-plane floor; non-fluidic, so "
                     f"it carries no volume and joins nothing"),)
    return ((x, height), 0.0, ops, feats, (),
            {"fluidic": False, "stroke_width_mm": stroke, "cap_height_mm": height})


# --- refusals ----------------------------------------------------------------

REFUSED: dict[str, str] = {
    "tesla_valve": (
        "a passive flow diode is USELESS here: blocks.json states diodicity is exactly 1.0 "
        "in Stokes flow, and our Re is order 1e-2. It would add resistance and rectify "
        "nothing. For one-way behaviour use an off-chip check valve or a switched port."),
    "pillar_funnel_trap": (
        "blocks.json retention.not_viable: a funnel into a pillar gap cannot retain by size "
        "exclusion at a 200 um throat, and the published stagnation-point traps it imitates "
        "were for 1-15 um beads with active feedback. Use trap_chamber with a depth weir."),
}

# Blocks in the contract library with no sizer yet. Listed rather than silently absent, so
# S6 can emit a `missing_part` placement with a real reason instead of guessing geometry.
NOT_SIZED: dict[str, str] = {
    "shape_array": "no rule yet for what shape, at what pitch, for what purpose",
    "sudden_loss_element": "at Re << 1 an abrupt area change costs almost nothing; use "
                           "flow_restrictor, which is a duct and does",
    "trap_channel": "inline trap geometry depends on the retention scheme; the only one "
                    "that works here is the depth weir, which is trap_chamber",
    "lock_and_shift_trap": "needs a measured release threshold on flow reversal",
    "merging_trap": "needs a pairing rule nothing upstream provides yet",
    "round_sorting_junction": "inertial sorting needs Re we do not reach",
    "square_sorting_junction": "inertial sorting needs Re we do not reach",
    "square_crenel_array": "no functional requirement expressed in a Need maps to it yet",
    "round_crenel_array": "no functional requirement expressed in a Need maps to it yet",
    "honeycomb_array": "surface-area target not expressed in a Need yet",
    "round_maze": "use serpentine_channel or spiral_channel for a long path; a maze adds "
                  "stagnant corners and no length per area",
    "square_maze": "as round_maze, with sharper corners",
    "side_port_aperture": "a sidewall aperture is a routing detail the router owns",
    "wagon_wheel_port": "spoke count and collapse margin need a bonding-pressure number "
                        "no contract records",
    "gradient_generator": "needs the concentration profile asked for, which no Need field "
                          "carries yet",
    "branching_arbor": "use flow_distributor for a matched tree; an arbor adds generations "
                       "without a stated purpose",
    "angiogenesis": "post spacing comes from the biology of the sprouting assay, which is "
                    "not in any contract here",
    "culture_chamber_open": "not a block id; open wells are chamber.open_top",
}

SIZERS: dict[str, Callable[[Ctx], tuple]] = {
    "straight_channel": size_straight_channel,
    "curved_channel": size_curved_channel,
    "channel_corner": size_channel_corner,
    "serpentine_channel": size_serpentine_channel,
    "snake_channel": size_snake_channel,
    "asymmetric_serpentine": size_asymmetric_serpentine,
    "spiral_channel": size_spiral_channel,
    "ring_junction": size_ring_junction,
    "port": size_port,
    "t_junction": size_t_junction,
    "cross_junction": size_cross_junction,
    "y_junction": size_y_junction,
    "curved_y_junction": size_curved_y_junction,
    "fork_junction": size_fork_junction,
    "network_node": size_network_node,
    "polygon_chamber": size_polygon_chamber,
    "culture_chamber": size_culture_chamber,
    "trap_chamber": size_trap_chamber,
    "flow_focusing_funnel": size_flow_focusing_funnel,
    "flow_restrictor": size_flow_restrictor,
    "flow_manifold": size_flow_manifold,
    "flow_divider": size_flow_divider,
    "flow_distributor": size_flow_distributor,
    "text": size_text,
    "pillar_array": size_pillar_array,
    "dld_array": size_dld_array,
    "wells_array": size_wells_array,
    "metering_trap": size_metering_trap,
    "centipede_channel": size_centipede_channel,
    "herringbone_mixer": size_herringbone_mixer,
    "flow_focusing": size_flow_focusing,
    "phase_guide": size_phase_guide,
    "organoid_chamber": size_organoid_chamber,
}

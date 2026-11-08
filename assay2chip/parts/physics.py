"""The flow physics every part is sized from. SI inside, repo units at the edges.

All of it is first-principles Stokes flow in a shallow rectangular duct, which is what a
0.3 mm deep chip at microlitres per minute actually is (Re is order 1e-2).

WALL SHEAR is the one that decides most geometry:

    tau = 6 * mu * Q / (w * h^2)        SI, Pa          x 10 -> dyn/cm2

and `w` IS THE REAL LANE WIDTH - the dimension the fluid is spread across, measured
perpendicular to the flow. It is NEVER the part's bounding box. Module 6's culture field
is 1.2 x 8 mm and the flow crosses the 1.2 mm dimension, so w is 8 mm, not 1.2 and not 8
x 1.2. Getting this wrong understates shear by whatever the aspect ratio happens to be,
which is how an adherent-cell chamber silently ends up washing its cells off the floor.

Viscosity always comes from assay2chip.lib.contracts.viscosity_for(temp_c) - 0.0008 Pa s
at 37 C for the real assay, not the 20 C water value that belongs to the dye demo.

THERE ARE NO MATERIAL CONSTANTS IN THIS MODULE. It is pure arithmetic: every density,
viscosity and dimension arrives as an argument. Densities used to live here as literals
(a water density and a cell excess density) back when the contracts had neither; they do
now, so the callers read them through `assay2chip/parts/derived.py`:

    derived.fluid_density_kg_m3()                 fluid.density_37c_kg_m3
    derived.excess_density(particles, purpose)    cell / capsule / bead
                                                  .excess_density_kg_m3

and `purpose` matters, because Stokes settling is LINEAR in excess density: a sizer
relying on settling to RETAIN takes the band's min, one checking for LOSS by settling
takes its max. See derived.PURPOSE_END.
"""

from __future__ import annotations

import math

GRAVITY_M_S2 = 9.80665

MM3_PER_UL = 1.0
PA_PER_DYN_CM2 = 0.1


# --- unit bridges ------------------------------------------------------------

def ul_min_to_m3_s(flow_ul_min: float) -> float:
    """1 uL = 1e-9 m3, per minute."""
    return flow_ul_min * 1e-9 / 60.0


def ul_min_to_mm3_s(flow_ul_min: float) -> float:
    return flow_ul_min / 60.0


def dyn_cm2_to_pa(tau_dyn_cm2: float) -> float:
    return tau_dyn_cm2 * PA_PER_DYN_CM2


def pa_to_dyn_cm2(tau_pa: float) -> float:
    return tau_pa * 10.0


# --- wall shear --------------------------------------------------------------

def wall_shear_dyn_cm2(flow_ul_min: float, lane_width_mm: float, depth_mm: float,
                       mu_pa_s: float) -> float:
    """tau = 6 mu Q / (w h^2), in dyn/cm2.

    lane_width_mm is the REAL lane width: the wetted width across the flow. Pass the
    bounding box and the answer is wrong.
    """
    if lane_width_mm <= 0 or depth_mm <= 0:
        raise ValueError("lane width and depth must be positive")
    q = ul_min_to_m3_s(flow_ul_min)
    w = lane_width_mm * 1e-3
    h = depth_mm * 1e-3
    return pa_to_dyn_cm2(6.0 * mu_pa_s * q / (w * h * h))


def lane_width_for_shear_mm(flow_ul_min: float, depth_mm: float, mu_pa_s: float,
                            ceiling_dyn_cm2: float) -> float:
    """The narrowest lane that keeps wall shear at or below a ceiling.

    Inverted straight out of the shear law: w = 6 mu Q / (tau h^2). The only lever at
    fixed depth and fixed flow is width, which is why wide chambers exist at all.
    """
    if ceiling_dyn_cm2 <= 0:
        raise ValueError("shear ceiling must be positive")
    q = ul_min_to_m3_s(flow_ul_min)
    h = depth_mm * 1e-3
    tau = dyn_cm2_to_pa(ceiling_dyn_cm2)
    return 6.0 * mu_pa_s * q / (tau * h * h) * 1e3


# --- velocity, resistance, Reynolds -----------------------------------------

def mean_velocity_mm_s(flow_ul_min: float, lane_width_mm: float, depth_mm: float) -> float:
    return ul_min_to_mm3_s(flow_ul_min) / (lane_width_mm * depth_mm)


def hydraulic_diameter_mm(width_mm: float, depth_mm: float) -> float:
    return 2.0 * width_mm * depth_mm / (width_mm + depth_mm)


def reynolds(flow_ul_min: float, width_mm: float, depth_mm: float, mu_pa_s: float,
             rho_kg_m3: float) -> float:
    """Re = rho U Dh / mu. Order 1e-2 here, which is why inertia buys us nothing.

    It is the reason tesla_valve is refused (diodicity is exactly 1 in Stokes flow) and
    the reason a plain serpentine does not mix. `rho_kg_m3` is required: pass
    derived.fluid_density_kg_m3().
    """
    u = mean_velocity_mm_s(flow_ul_min, width_mm, depth_mm) * 1e-3
    dh = hydraulic_diameter_mm(width_mm, depth_mm) * 1e-3
    return rho_kg_m3 * u * dh / mu_pa_s


def rect_resistance_pa_s_per_m3(length_mm: float, width_mm: float, depth_mm: float,
                                mu_pa_s: float) -> float:
    """Hydraulic resistance of a rectangular duct (Bruus):

        R = 12 mu L / (a b^3 (1 - 0.63 b/a)),  a = long side, b = short side.

    This is what flow_restrictor is for and what phase_guide is NOT for.
    """
    a = max(width_mm, depth_mm) * 1e-3
    b = min(width_mm, depth_mm) * 1e-3
    return 12.0 * mu_pa_s * (length_mm * 1e-3) / (a * b ** 3 * (1.0 - 0.63 * b / a))


def pressure_drop_pa(length_mm: float, width_mm: float, depth_mm: float, mu_pa_s: float,
                     flow_ul_min: float) -> float:
    return rect_resistance_pa_s_per_m3(length_mm, width_mm, depth_mm, mu_pa_s) * \
        ul_min_to_m3_s(flow_ul_min)


def length_for_pressure_drop_mm(target_pa: float, width_mm: float, depth_mm: float,
                                mu_pa_s: float, flow_ul_min: float) -> float:
    """How long a restrictor has to be to cost a given pressure at a given flow."""
    unit = pressure_drop_pa(1.0, width_mm, depth_mm, mu_pa_s, flow_ul_min)
    if unit <= 0:
        raise ValueError("zero flow has no pressure drop to size against")
    return target_pa / unit


# --- residence and sedimentation --------------------------------------------

def residence_volume_ul(flow_ul_min: float, residence_s: float) -> float:
    """Swept volume a residence time needs: V = Q t. This is what sets a delay line."""
    return flow_ul_min * residence_s / 60.0


def residence_time_s(volume_ul: float, flow_ul_min: float) -> float:
    return volume_ul / flow_ul_min * 60.0


def settling_velocity_um_s(diameter_um: float, mu_pa_s: float,
                           excess_density_kg_m3: float) -> float:
    """Stokes terminal velocity: v = dRho g d^2 / (18 mu). Microns per second.

    LINEAR in excess density and QUADRATIC in diameter, which is why the contract bands
    one excess density per particle kind and why the end of the band a sizer picks has to
    be recorded: across cell.excess_density_kg_m3's 40-75 band the answer moves 1.9x.
    """
    d = diameter_um * 1e-6
    v = excess_density_kg_m3 * GRAVITY_M_S2 * d * d / (18.0 * mu_pa_s)
    return v * 1e6


def settling_time_s(depth_mm: float, diameter_um: float, mu_pa_s: float,
                    excess_density_kg_m3: float) -> float:
    """How long a particle takes to fall the full depth of the layer and sit on the floor.

    At the nominal cell excess density a 12 um cell in a 0.3 mm layer at 37 C takes about
    51 s. Any particle-carrying line whose residence time beats that is a line that
    deposits its cargo on the floor - and that question takes the band's MAX, which makes
    it about 41 s.
    """
    v = settling_velocity_um_s(diameter_um, mu_pa_s, excess_density_kg_m3)
    return depth_mm * 1000.0 / v


# --- mixing ------------------------------------------------------------------

def peclet(flow_ul_min: float, depth_mm: float, diffusivity_m2_s: float) -> float:
    """Pe = U w / D, which for a duct reduces to Q / (h D): width cancels.

    So you cannot buy mixing by widening a channel, only by stirring it.
    """
    return ul_min_to_m3_s(flow_ul_min) / (depth_mm * 1e-3 * diffusivity_m2_s)


def diffusive_mixing_length_mm(flow_ul_min: float, width_mm: float, depth_mm: float,
                               diffusivity_m2_s: float) -> float:
    """How far two streams must travel side by side to mix by diffusion alone.

    L = U w^2 / D. This is the number that makes a plain serpentine useless as a mixer:
    at 0.8 mm wide it runs to metres.
    """
    u = mean_velocity_mm_s(flow_ul_min, width_mm, depth_mm) * 1e-3
    w = width_mm * 1e-3
    return u * w * w / diffusivity_m2_s * 1e3


def herringbone_cycles(peclet_number: float) -> int:
    """Staggered-herringbone cycles needed, from the log(Pe) scaling (Stroock 2002).

    Chaotic advection stretches and folds, so mixing length grows with log(Pe) instead of
    Pe. Three cycles is the published practical minimum.
    """
    return max(3, math.ceil(math.log(max(peclet_number, math.e))))


# --- deterministic lateral displacement -------------------------------------

def dld_critical_diameter_um(gap_um: float, epsilon: float) -> float:
    """Davis correlation: Dc = 1.4 g eps^0.48.

    Checked against the one worked point blocks.json states (gap 200 um, eps 0.125,
    ~105 um) by assay2chip/parts/sizers.py, which reports both numbers rather than
    trusting either on its own.
    """
    return 1.4 * gap_um * epsilon ** 0.48

"""Flow maths for the process model.

CLOSED channels   hydraulic resistance of a rectangular microchannel (laminar flow).
MESH              a clean woven mesh treated as parallel circular openings (Sampson flow
                  through an orifice). For the wash MVP this is tiny: the capsule bed sitting
                  on the mesh dominates, which is why that resistance is a parameter the
                  pressure sensor refits (contracts/parameters.json, mesh.bed_resistance_*).
OPEN channels     APPROXIMATE. Diffusion-only transfer between two wells at the same level:
                  t ~ L^2 / (2 D). Gravity-driven open-channel flow is still TODO.

Units in: mm, um, uL/min, Pa s. Resistances out: kPa per (uL/min), so pressure_kpa = R * flow.
Standard library only.
"""

from __future__ import annotations

import math

UL_MIN_TO_M3_S = 1e-9 / 60.0


def rect_channel_resistance_pa_s_m3(length_mm: float, width_mm: float, depth_mm: float, viscosity_pa_s: float) -> float:
    """R = 12 mu L / (w h^3 (1 - 0.63 h / w)), h the smaller side. Good to ~10% for h/w <= 1."""
    w, h = max(width_mm, depth_mm) / 1000, min(width_mm, depth_mm) / 1000
    return 12 * viscosity_pa_s * (length_mm / 1000) / (w * h ** 3 * (1 - 0.63 * h / w))


def to_kpa_per_ul_min(r_pa_s_m3: float) -> float:
    return r_pa_s_m3 * UL_MIN_TO_M3_S / 1000.0


def channel_resistance(length_mm: float, width_mm: float, depth_mm: float, viscosity_pa_s: float) -> float:
    """Closed rectangular channel, in kPa per uL/min."""
    return to_kpa_per_ul_min(rect_channel_resistance_pa_s_m3(length_mm, width_mm, depth_mm, viscosity_pa_s))


def mesh_clean_resistance(opening_um: float, area_mm2: float, open_fraction: float, viscosity_pa_s: float) -> float:
    """Clean mesh as N parallel openings, each a Sampson orifice (dP = 3 mu Q / a^3),
    in kPa per uL/min. Square openings are approximated by circles of the same width."""
    a = opening_um / 2 / 1e6                              # opening radius, m
    pores = open_fraction * area_mm2 / (opening_um / 1000) ** 2
    per_pore = 3 * viscosity_pa_s / a ** 3                # Pa s / m^3
    return to_kpa_per_ul_min(per_pore / max(pores, 1.0))


def pressure_kpa(resistance_kpa_per_ul_min: float, flow_ul_min: float) -> float:
    return resistance_kpa_per_ul_min * flow_ul_min


def open_channel_diffusion_time_s(length_mm: float, diffusivity_m2_s: float = 4e-10) -> float:
    """APPROXIMATE. Time for dye to diffuse along an open channel between two wells at
    the same level: t ~ L^2 / (2 D). Visible transfer needs a level difference or mixing."""
    return (length_mm / 1000) ** 2 / (2 * diffusivity_m2_s)


if __name__ == "__main__":
    # Quick look at the wash MVP numbers.
    mu = 1e-3
    print(f"140 x 150 um channel, 1.25 mm: {channel_resistance(1.25, 0.14, 0.15, mu):.2e} kPa per uL/min")
    print(f"clean 25 um mesh, 49 mm2, 35% open: {mesh_clean_resistance(25, 49, 0.35, mu):.2e} kPa per uL/min")
    t = open_channel_diffusion_time_s(5.0)
    print(f"APPROXIMATE: diffusion along a 5 mm open channel ~ {t:.0f} s = {t / 3600:.1f} h")

"""Flow maths for the process model.

CLOSED channels   hydraulic resistance of a rectangular microchannel (laminar flow), from the
                  EXACT series for Poiseuille flow in a rectangle. The 1 - 0.63 h/w shortcut
                  this used to apply is 14% off for a square duct (w = h), and every channel
                  on the demo chips is 0.2 x 0.3 mm, close to square.
WEIRS             a weir crest as a thin slit across the chamber: R = 12 mu L / (W g^3), so
                  flow goes as the gap cubed.
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

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UL_MIN_TO_M3_S = 1e-9 / 60.0


def _parameter(name: str) -> float:
    """One value from contracts/parameters.json, with no local fallback copy."""
    params = json.loads((ROOT / "contracts" / "parameters.json").read_text())["parameters"]
    return float(params[name]["value"])


DYE_DIFFUSIVITY_M2_S = _parameter("dye.diffusivity_m2_s")
# Default for chip calculations: the 37 C medium value. fluid.viscosity_pa_s is water at
# 20 C and belongs to the room-temperature dye demo only.
VISCOSITY_PA_S = _parameter("fluid.viscosity_37c_pa_s")
VISCOSITY_WATER_20C_PA_S = _parameter("fluid.viscosity_pa_s")


def duct_shape_factor(width_mm: float, depth_mm: float, terms: int = 50) -> float:
    """f in R = 12 mu L / (w h^3 f), w the longer and h the shorter side of the rectangle.

    The exact series for laminar flow in a rectangular duct (White, Viscous Fluid Flow;
    Bruus, Theoretical Microfluidics eq. 3.62 in this form):
        f = 1 - (192 h / (pi^5 w)) * sum over odd n of tanh(n pi w / (2 h)) / n^5
    f -> 1 for an infinitely wide slot and 0.4217 for a square, where the old
    1 - 0.63 h/w gives 0.37: the approximation over-states a square duct's resistance by
    14%. 50 odd terms converge far past double precision (the n^-5 tail is < 1e-11).
    """
    w, h = max(width_mm, depth_mm), min(width_mm, depth_mm)
    total = sum(math.tanh(n * math.pi * w / (2 * h)) / n ** 5 for n in range(1, 2 * terms, 2))
    return 1 - 192 * h / (math.pi ** 5 * w) * total


def rect_channel_resistance_pa_s_m3(length_mm: float, width_mm: float, depth_mm: float, viscosity_pa_s: float) -> float:
    """R = 12 mu L / (w h^3 f), h the smaller side and f the exact series (duct_shape_factor)."""
    w, h = max(width_mm, depth_mm) / 1000, min(width_mm, depth_mm) / 1000
    return 12 * viscosity_pa_s * (length_mm / 1000) / (w * h ** 3 * duct_shape_factor(width_mm, depth_mm))


def slit_resistance_pa_s_m3(width_mm: float, gap_um: float, length_mm: float, viscosity_pa_s: float) -> float:
    """A weir crest: a slit `gap_um` high, `width_mm` wide (the full chamber wall it spans) and
    `length_mm` long along the flow (the crest thickness). R = 12 mu L / (W g^3), the
    wide-slit limit - a 25 um gap under a 2.5 mm crest is 100:1, so the side walls the
    exact series would add change it by under 1%. Flow scales with gap^3, which is why a
    +/-20 um z tolerance on a 25 um gap moves this by a factor of several."""
    return 12 * viscosity_pa_s * (length_mm / 1000) / ((width_mm / 1000) * (gap_um * 1e-6) ** 3)


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


def open_channel_diffusion_time_s(length_mm: float, diffusivity_m2_s: float = DYE_DIFFUSIVITY_M2_S) -> float:
    """APPROXIMATE. Time for dye to diffuse along an open channel between two wells at
    the same level: t ~ L^2 / (2 D). Visible transfer needs a level difference or mixing."""
    return (length_mm / 1000) ** 2 / (2 * diffusivity_m2_s)


if __name__ == "__main__":
    # Quick look at the wash MVP numbers.
    mu = VISCOSITY_PA_S
    print(f"140 x 150 um channel, 1.25 mm: {channel_resistance(1.25, 0.14, 0.15, mu):.2e} kPa per uL/min")
    print(f"clean 25 um mesh, 49 mm2, 35% open: {mesh_clean_resistance(25, 49, 0.35, mu):.2e} kPa per uL/min")
    t = open_channel_diffusion_time_s(5.0)
    print(f"APPROXIMATE: diffusion along a 5 mm open channel ~ {t:.0f} s = {t / 3600:.1f} h")

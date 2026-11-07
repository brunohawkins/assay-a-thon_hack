"""Chip sizing calculator: a Python port of the "Sizing Calculator" tab in
contracts/sources/chip_geometry_requirements.xlsx (Szonja). Same inputs, same defaults,
same formulas; the sheet cell each one comes from is noted next to it.

Two input modes:
  capsules  you give the capsule count (the sheet's B6) and get the cells it holds.
  cells     you give a TARGET CELL COUNT; the capsule count needed is derived as
            cells / mean cells per capsule (the sheet's B62), and everything else is
            sized from that. Most capsules are empty (~0.08 cells per capsule), so
            "100,000 cells" needs about 1.3 million capsules, not 100,000.

    python operator/sizing.py                         # sheet defaults: capsule mode next to cell mode
    python operator/sizing.py mvp cells:100000        # one column per scenario
    python operator/sizing.py capsules:25000 --set split_chambers_per_round=12

Scenarios: default, mvp, capseq, capsules:<N>, cells:<N>, or preset+mode (mvp+cells:2000).
Standard library only.
"""

from __future__ import annotations

import argparse
import math
import sys

DEFAULTS = {
    # Inputs (step 1 / encapsulation)
    "capsule_diameter_um": 70.0,       # B5  CapSeq 70 +/- 3 um; SPC methods 75 +/- 1.5 um
    "capsule_count": 100000.0,         # B6
    "packing_fraction": 0.64,          # B7  random close packing (assumption)
    "swell_factor": 2.0,               # B8  CapSeq: >2x during culture
    "channel_width_factor_low": 1.5,   # B9  assumption
    "channel_width_factor_high": 2.0,  # B10 assumption
    "trap_gap_factor_low": 0.3,        # B11 assumption
    "trap_gap_factor_high": 0.4,       # B12 assumption
    "split_chambers_per_round": 8,     # B13 MVP suggestion; CapSeq uses 96
    "barcoding_rounds": 3,             # B14 CapSeq
    "gelma_flow_ul_h": 250.0,          # B15 SPC methods, 40 um nozzle
    "cell_flow_ul_h": 100.0,           # B16
    "oil_flow_ul_h": 700.0,            # B17
    "cell_conc_per_ul": 1500.0,        # B18
    "nozzle_width_um": 40.0,           # B19
    "cell_filter_gap_um": 30.0,        # B20
    # Inputs (step 3)
    "common_reagent_lines": 8,         # B47
    "sublibraries": 4,                 # B48 assumption
    "chamber_volumes_per_wash": 5.0,   # B49 assumption
    "washes_per_exchange": 3,          # B50 assumption
    "exchange_steps_per_run": 6,       # B51 assumption
    # Input (target cells)
    "target_cells": 2000.0,            # B66 demo target (assumption)
}

PRESETS = {
    "default": {},
    "mvp": {"capsule_count": 10000, "split_chambers_per_round": 8, "barcoding_rounds": 3},
    "capseq": {"split_chambers_per_round": 96, "barcoding_rounds": 3},
}

COLLISION_WARN = 0.05  # warn above 5% of cells sharing a barcode


def _iferror(fn, fallback=0.0):
    """Excel IFERROR: any arithmetic error gives the fallback."""
    try:
        return fn()
    except (ZeroDivisionError, OverflowError, ValueError):
        return fallback


def compute(inputs: dict | None = None, mode: str = "capsules") -> dict:
    """All sheet outputs for one scenario. mode is "capsules" or "cells"."""
    if mode not in ("capsules", "cells"):
        raise ValueError("mode must be 'capsules' or 'cells'")
    unknown = set(inputs or {}) - set(DEFAULTS)
    if unknown:
        raise ValueError(f"unknown input(s): {', '.join(sorted(unknown))}")
    p = {**DEFAULTS, **(inputs or {})}
    d = p["capsule_diameter_um"]

    capsule_volume_nl = 4 / 3 * math.pi * (d / 2) ** 3 / 1_000_000          # B23
    aqueous_flow_ul_h = p["gelma_flow_ul_h"] + p["cell_flow_ul_h"]          # B34
    capsules_per_hour = aqueous_flow_ul_h / capsule_volume_nl * 1000        # B35
    cells_per_hour = p["cell_flow_ul_h"] * p["cell_conc_per_ul"]            # B37
    mean = _iferror(lambda: cells_per_hour / capsules_per_hour)             # B38

    if mode == "cells":
        target = p["target_cells"]
        count = _iferror(lambda: target / mean)                             # B62
    else:
        count = p["capsule_count"]                                          # B6

    hours = _iferror(lambda: count / capsules_per_hour)                     # B41
    cells = cells_per_hour * hours                                          # B42 (= count x mean)
    chambers, rounds, subs = p["split_chambers_per_round"], p["barcoding_rounds"], p["sublibraries"]
    packed = capsule_volume_nl * count / p["packing_fraction"] / 1000       # B24
    combos = chambers ** rounds                                             # B31
    combos_sub = chambers ** rounds * subs                                  # B61
    wash_per_exchange = packed * p["chamber_volumes_per_wash"] * p["washes_per_exchange"]  # B59

    out = {
        "mode": mode,
        "inputs": p,
        "capsule_count": count,
        "cells": cells,
        # capsule handling
        "capsule_volume_nl": capsule_volume_nl,                              # B23
        "packed_volume_ul": packed,                                          # B24
        "monolayer_area_cm2": count * d ** 2 / 100_000_000,                  # B25
        "trap_gap_low_um": d * p["trap_gap_factor_low"],                     # B26
        "trap_gap_high_um": d * p["trap_gap_factor_high"],                   # B27
        "channel_width_low_um": d * p["channel_width_factor_low"],           # B28
        "channel_width_high_um": d * p["channel_width_factor_high"],         # B29
        "swollen_diameter_um": d * p["swell_factor"],                        # B30
        "barcode_combinations": combos,                                      # B31
        # step 1 / encapsulation
        "aqueous_flow_ul_h": aqueous_flow_ul_h,                              # B34
        "capsules_per_hour": capsules_per_hour,                              # B35
        "generation_rate_hz": capsules_per_hour / 3600,                      # B36
        "cells_per_hour": cells_per_hour,                                    # B37
        "mean_cells_per_capsule": mean,                                      # B38
        "fraction_single_cell": mean * math.exp(-mean),                      # B39
        "fraction_multi_of_occupied": _iferror(                              # B40
            lambda: (1 - math.exp(-mean) - mean * math.exp(-mean)) / (1 - math.exp(-mean))),
        "hours_to_encapsulate": hours,                                       # B41
        "cells_captured": cells,                                             # B42
        "filter_check": ("OK" if p["cell_filter_gap_um"] <= p["nozzle_width_um"]  # B43
                         else "Filter gap too large: clumps can reach nozzle"),
        # step 3
        "barcode_reagent_lines": chambers * rounds,                          # B54
        "total_reagent_inlets": p["common_reagent_lines"] + chambers * rounds + subs,  # B55
        "valves": p["common_reagent_lines"] + chambers * rounds + subs,      # B56
        "capsules_per_split_chamber": count / chambers,                      # B57
        "packed_volume_per_split_chamber_ul": packed / chambers,             # B58
        "wash_volume_per_exchange_ul": wash_per_exchange,                    # B59
        "total_wash_buffer_ml": wash_per_exchange * p["exchange_steps_per_run"] / 1000,  # B60
        "barcode_combinations_with_sublibraries": combos_sub,                # B61
        "capsules_needed_for_target_cells": _iferror(lambda: p["target_cells"] / mean),  # B62
    }
    # Not on the sheet: expected fraction of cells sharing their full barcode with another
    # cell. Approximation 1 - e^(-N/C); C includes the sublibrary index (B61), which is
    # equivalent to N/S cells per sublibrary over chambers^rounds barcodes.
    out["barcode_collision_fraction"] = _iferror(lambda: 1 - math.exp(-cells / combos_sub))
    out["warnings"] = warnings(out)
    return out


def warnings(r: dict) -> list[str]:
    w = []
    if r["barcode_collision_fraction"] > COLLISION_WARN:
        p = r["inputs"]
        w.append(f"barcode collisions: about {r['barcode_collision_fraction']:.0%} of {r['cells']:,.0f} cells share a "
                 f"barcode with another cell ({p['split_chambers_per_round']} chambers x {p['barcoding_rounds']} rounds "
                 f"x {p['sublibraries']} sublibraries = {r['barcode_combinations_with_sublibraries']:,} combinations); "
                 f"add chambers per round, rounds or sublibraries, or load fewer cells")
    if r["filter_check"] != "OK":
        w.append(r["filter_check"])
    return w


# --- scenarios and printing ------------------------------------------------------------


def scenario(spec: str, overrides: dict | None = None) -> tuple[str, dict]:
    """'mvp', 'cells:100000', 'capsules:25000', 'capseq+cells:100000' -> (label, result)."""
    inputs: dict = {}
    mode = "capsules"
    for part in spec.split("+"):
        if part in PRESETS:
            inputs.update(PRESETS[part])
        elif part.startswith("cells:"):
            mode = "cells"
            inputs["target_cells"] = float(part.split(":", 1)[1].replace(",", "").replace("_", ""))
        elif part.startswith("capsules:"):
            mode = "capsules"
            inputs["capsule_count"] = float(part.split(":", 1)[1].replace(",", "").replace("_", ""))
        else:
            raise ValueError(f"unknown scenario part {part!r}; use {', '.join(PRESETS)}, cells:<N> or capsules:<N>")
    inputs.update(overrides or {})
    return spec, compute(inputs, mode)


ROWS = [
    ("Mode", lambda r: "cells -> capsules" if r["mode"] == "cells" else "capsules -> cells"),
    ("Capsule count", lambda r: f"{math.ceil(r['capsule_count'] - 1e-9):,}"),
    ("Cells", lambda r: f"{r['cells']:,.0f}"),
    ("Capsule diameter (um)", lambda r: f"{r['inputs']['capsule_diameter_um']:g}"),
    ("Split chambers x rounds", lambda r: f"{r['inputs']['split_chambers_per_round']} x {r['inputs']['barcoding_rounds']}"),
    ("-- capsule handling", None),
    ("Single capsule volume (nL)", lambda r: f"{r['capsule_volume_nl']:.3f}"),
    ("Packed chamber volume (uL)", lambda r: f"{r['packed_volume_ul']:.2f}"),
    ("Monolayer trap area (cm2)", lambda r: f"{r['monolayer_area_cm2']:.3f}"),
    ("  = square side (mm)", lambda r: f"{math.sqrt(r['monolayer_area_cm2'] * 100):.2f}"),
    ("Trap gap (um)", lambda r: f"{r['trap_gap_low_um']:g}-{r['trap_gap_high_um']:g}"),
    ("Capsule channel width (um)", lambda r: f"{r['channel_width_low_um']:g}-{r['channel_width_high_um']:g}"),
    ("Swollen capsule diameter (um)", lambda r: f"{r['swollen_diameter_um']:g}"),
    ("-- encapsulation", None),
    ("Capsules made per hour", lambda r: f"{r['capsules_per_hour']:,.0f}"),
    ("Generation rate (Hz)", lambda r: f"{r['generation_rate_hz']:.0f}"),
    ("Mean cells per capsule", lambda r: f"{r['mean_cells_per_capsule']:.4f}"),
    ("Capsules with exactly 1 cell", lambda r: f"{r['fraction_single_cell']:.1%}"),
    ("Occupied capsules with >1 cell", lambda r: f"{r['fraction_multi_of_occupied']:.1%}"),
    ("Hours to encapsulate", lambda r: f"{r['hours_to_encapsulate']:.3f}"),
    ("Cell filter vs nozzle", lambda r: r["filter_check"]),
    ("-- step 3", None),
    ("Reagent inlets / valves", lambda r: f"{r['total_reagent_inlets']} / {r['valves']}"),
    ("Capsules per split chamber", lambda r: f"{r['capsules_per_split_chamber']:,.0f}"),
    ("Packed vol per split chamber (uL)", lambda r: f"{r['packed_volume_per_split_chamber_ul']:.3f}"),
    ("Wash volume per exchange (uL)", lambda r: f"{r['wash_volume_per_exchange_ul']:.1f}"),
    ("Total wash buffer per run (mL)", lambda r: f"{r['total_wash_buffer_ml']:.2f}"),
    ("-- barcodes", None),
    ("Combinations (chambers^rounds)", lambda r: f"{r['barcode_combinations']:,}"),
    ("  x sublibraries", lambda r: f"{r['barcode_combinations_with_sublibraries']:,}"),
    ("Cells sharing a barcode (approx)", lambda r: f"{r['barcode_collision_fraction']:.1%}"),
]


def format_table(results: list[tuple[str, dict]]) -> str:
    width = max(len(label) for label, _ in ROWS) + 2
    col = max(18, *(len(name) + 2 for name, _ in results))
    lines = ["".ljust(width) + "".join(name.rjust(col) for name, _ in results)]
    for label, fn in ROWS:
        if fn is None:
            lines.append(label)
            continue
        lines.append(label.ljust(width) + "".join(fn(r).rjust(col) for _, r in results))
    for name, r in results:
        for w in r["warnings"]:
            lines.append(f"WARN [{name}] {w}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Chip sizing calculator (port of Szonja's sheet).")
    ap.add_argument("scenarios", nargs="*", default=["default", "cells:100000"],
                    help="default, mvp, capseq, capsules:<N>, cells:<N>, or combined like mvp+cells:2000")
    ap.add_argument("--set", action="append", default=[], metavar="NAME=VALUE",
                    help=f"override an input for every scenario ({', '.join(DEFAULTS)})")
    args = ap.parse_args(argv)
    overrides = {}
    for item in args.set:
        name, _, value = item.partition("=")
        if name not in DEFAULTS:
            ap.error(f"unknown input {name!r}")
        overrides[name] = float(value)
    try:
        results = [scenario(s, overrides) for s in args.scenarios]
    except ValueError as e:
        ap.error(str(e))
    print(format_table(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())

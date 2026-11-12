"""The CFD's headline findings on the hand-designed chips, one command each, recomputed live.

    python tools/demo_findings.py settling     # capsules settle out of Module 4's feed line
    python tools/demo_findings.py retention    # Module 3's 25 um retention gap has ~2 um of headroom
    python tools/demo_findings.py sweep        # a wash step that does not reach its target
    python tools/demo_findings.py all

Nothing is read from a cached report: each command calls the CFD modules on the chip spec
and protocol in this checkout and prints the TWO NUMBERS THAT DISAGREE, plus where each one
comes from. Exit code 0 means the finding reproduced; 1 means it did not (the chip or the
model has changed - say so, do not hand-wave).

Note: these come from the reduced-order solver. Its validation suite currently reports
failed benchmarks (python assay2chip/cfd/validate.py), so the pipeline labels every finding
UNTRUSTED. The settling and retention numbers below do not depend on the 2D flow field
those benchmarks are about: settling is Stokes' law against the transit time through one
duct, and the retention band is the capsule diameter against the fabricated gap.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BAR = "-" * 78


def _head(title: str, chip: str) -> None:
    print(f"\n{BAR}\n{title}\n  chip: chip/{chip}.json   protocol: protocols/{chip}.json\n{BAR}")


def settling(chip: str = "module4_v1", feature: str = "ch_in_1") -> bool:
    from assay2chip.cfd import particles as P

    _head("FINDING 1 - capsules settle out in a feed line before they arrive", chip)
    t0 = time.monotonic()
    r = P.analyse_chip(chip, n_particles=8)
    rows = [s for s in r.get("sediment") or [] if s.get("feature_id") == feature]
    if not rows:
        print(f"  NOT REPRODUCED: no sediment record for {feature}")
        return False
    worst = max(rows, key=lambda s: s.get("settled_fraction") or 0)
    print(f"  feature           {feature} (a feed line: its job is to deliver)")
    print(f"  particle          {worst['particle']}, {worst['diameter_um']:g} um, "
          f"excess density {worst['excess_density_kg_m3']:g} kg/m3 ({worst['density_key']})")
    print(f"  settling velocity {worst['v_s_um_s']:.0f} um/s (Stokes) over a "
          f"{worst['depth_um']:.0f} um deep channel")
    print(f"\n  >> time to reach the floor   {worst['fall_time_s']:.2f} s")
    print(f"  >> time to cross the line    {worst['transit_time_s']:.2f} s   "
          f"(at {worst['u_mm_s']:.2f} mm/s)")
    print(f"  => {worst['settled_fraction'] * 100:.0f}% of them land before they arrive "
          f"(allowed: 0%).  verdict: {worst['verdict']}, {worst['severity']}")
    print(f"  ({time.monotonic() - t0:.0f} s)")
    return worst["transit_time_s"] > worst["fall_time_s"]


def retention(chip: str = "module3_v1_translated", feature: str = "main_trap") -> bool:
    from assay2chip.cfd import particles as P

    _head("FINDING 2 - the retention gap works only within a narrow tolerance", chip)
    t0 = time.monotonic()
    r = P.analyse_chip(chip, n_particles=8)
    band = next((w for w in r.get("weir") or [] if w.get("feature_id") == feature), None)
    if band is None:
        print(f"  NOT REPRODUCED: no weir band for {feature}")
        return False
    headroom = band["gap_hi_um"] - band["nominal_gap_um"]
    print(f"  feature           {feature}: a fabricated depth weir, nominal gap "
          f"{band['nominal_gap_um']:g} um")
    print(f"  capsule           {band['diameter_um']:g} um; it escapes once the gap reaches "
          f"0.4 x D = {band['retention_limit_um']:.1f} um (trap.gap_factor_high)")
    print(f"\n  >> headroom before capsules escape   +{headroom:.1f} um")
    print(f"  >> fabrication z tolerance           +/-{band['z_tolerance_um']:g} um "
          f"(contracts/blocks.json, parsed by particles.z_tolerance_um)")
    print(f"  => the gap works over {band['covered_fraction'] * 100:.0f}% of the tolerance "
          f"window; a print {headroom:.1f} um too deep loses the capsules.")
    fix = P.capsule_size_for_full_tolerance()["retention_bound_only"]
    print(f"  proposed fix (fix.py): {fix['why']}")
    print(f"  ({time.monotonic() - t0:.0f} s)")
    return headroom < band["z_tolerance_um"]


def sweep(chip: str = "module3_v1_translated") -> bool:
    """Exchange reached vs target, from transport.py on the solved field of each step."""
    from assay2chip.cfd import run as cfd

    _head("FINDING 3 - a wash that does not reach its exchange target", chip)
    t0 = time.monotonic()
    cfd.bootstrap()
    chip_path = ROOT / "chip" / f"{chip}.json"
    protocol_path = ROOT / "protocols" / f"{chip}.json"
    run = cfd.Run(f"demo_findings_{chip}", ROOT / "assay2chip" / "work" / "demo_findings" /
                  chip, "chip", max_steps=8, n_particles=8)
    run.validation = {"status": "skipped", "trusted": False,
                      "detail": "not run by tools/demo_findings.py"}
    run.simulate_chip(cfd.load_json(chip_path), cfd.load_json(protocol_path), chip_path,
                      protocol_path, chip)
    from assay2chip.cfd import analyse

    work = run.works[-1]
    rows = []
    for record, result in zip(work.records, work.transport):
        if result is None:
            continue
        for chamber, check in analyse._exchange_from_transport(result).items():
            if check.get("reached") is not None and check.get("target") is not None:
                rows.append((cfd.record_label(record), chamber, check["reached"],
                             check["target"]))
    if not rows:
        print("  NOT REPRODUCED: transport.py reported no exchange with a target")
        print(f"  ({time.monotonic() - t0:.0f} s)")
        return False
    for label, chamber, reached, target in rows:
        mark = "  <== SHORT" if reached < target else ""
        print(f"  step {label:<34} {chamber:<16} reached {reached * 100:6.1f}%  "
              f"target {target * 100:5.1f}%{mark}")
    worst = min(rows, key=lambda r: r[2] - r[3])
    print(f"\n  >> worst exchange reached   {worst[2] * 100:.1f}%  ({worst[1]}, step {worst[0]})")
    print(f"  >> exchange target          {worst[3] * 100:.1f}%  (exchange.target_fraction)")
    print(f"  ({time.monotonic() - t0:.0f} s)")
    return worst[2] < worst[3]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("which", choices=("settling", "retention", "sweep", "all"))
    ap.add_argument("--chip", help="override the chip the finding is shown on")
    args = ap.parse_args(argv)
    todo = ["settling", "retention", "sweep"] if args.which == "all" else [args.which]
    ok = True
    for name in todo:
        fn = globals()[name]
        got = fn(args.chip) if args.chip else fn()
        print(f"  {'REPRODUCED' if got else 'NOT REPRODUCED'}: {name}")
        ok &= bool(got)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

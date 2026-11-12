"""One command: a protocol in, chips + protocols + a flow verdict + print files out.

    python tools/demo_pipeline.py                       # demo profile, the small fixture
    python tools/demo_pipeline.py --profile full        # same chain at full fidelity
    python tools/demo_pipeline.py --paper my.md --id my_run

Runs assay2chip S0 -> S12 through the real orchestrator (assay2chip/run.py), offline
(LLM stub), from clean: earlier outputs of THIS id are removed first, and nothing else is
touched. Then prints what came out, read back from disk, and which profile ran.
See assay2chip/stages/_profile.py for exactly what the demo profile changes.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "demo_bead_wash.md"
OUTPUT_DIRS = ("chip", "chip/designs", "protocols", "runs", "cad/exports", "cad/figures")


def clean(assay_id: str) -> int:
    """Remove a previous run of this id: its work dir and files named <id>_c<N>."""
    removed = 0
    prefix = f"{assay_id}_c"
    for d in OUTPUT_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in base.iterdir():
            if p.name.startswith(prefix) and p.name[len(prefix):len(prefix) + 1].isdigit():
                shutil.rmtree(p) if p.is_dir() else p.unlink()
                removed += 1
    work = ROOT / "assay2chip" / "work" / assay_id
    if work.is_dir():
        shutil.rmtree(work)
        removed += 1
    return removed


def cpu_s() -> float:
    me = resource.getrusage(resource.RUSAGE_SELF)
    kids = resource.getrusage(resource.RUSAGE_CHILDREN)
    return me.ru_utime + me.ru_stime + kids.ru_utime + kids.ru_stime


def summary(assay_id: str) -> dict:
    work = ROOT / "assay2chip" / "work" / assay_id
    load = lambda p: json.loads(p.read_text()) if p.is_file() else None  # noqa: E731
    partition = load(work / "partition.json") or {}
    chips = [c["chip_id"] for c in partition.get("chips", [])]
    verify = load(work / "verify.json") or {}
    findings = load(work / "cfd" / "findings.json") or []
    validation = load(work / "cfd" / "validation.json") or {}
    sev: dict[str, int] = {}
    for f in findings if isinstance(findings, list) else []:
        sev[f.get("severity", "?")] = sev.get(f.get("severity", "?"), 0) + 1
    prints = sorted(p.relative_to(ROOT).as_posix() for c in chips
                    for p in (ROOT / "cad" / "exports" / c / "print").glob("*") if p.is_file())
    return {
        "chips": chips,
        "chip_specs": [f"chip/{c}.json" for c in chips if (ROOT / "chip" / f"{c}.json").is_file()],
        "protocols": [f"protocols/{c}.json" for c in chips
                      if (ROOT / "protocols" / f"{c}.json").is_file()],
        "handoffs": len(partition.get("handoffs", [])),
        "verify_ok": verify.get("ok"),
        "verify_reason": (verify.get("verdict") or {}).get("reason"),
        "cfd_findings": sev,
        "cfd_trusted": validation.get("trusted"),
        "cfd_validation": validation.get("detail"),
        "print_files": prints,
        "report": (work / "report.md").relative_to(ROOT).as_posix()
        if (work / "report.md").is_file() else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--paper", default=str(DEFAULT_FIXTURE))
    ap.add_argument("--id", default="demo_small")
    ap.add_argument("--profile", choices=("demo", "full"), default="demo")
    ap.add_argument("--keep", action="store_true", help="do not clean this id first")
    ap.add_argument("--json", action="store_true", help="also print the summary as JSON")
    args = ap.parse_args(argv)

    os.environ["ASSAY2CHIP_PROFILE"] = args.profile
    from assay2chip import run as orchestrator
    from assay2chip.lib import llm
    os.environ[llm.MODE_ENV] = llm.STUB
    from assay2chip.stages import _profile

    print("=" * 78)
    print(_profile.banner())
    print(f"input: {Path(args.paper).resolve().relative_to(ROOT) if Path(args.paper).resolve().is_relative_to(ROOT) else args.paper}"
          f"   id: {args.id}   llm: stub (offline)")
    print("=" * 78)
    if not args.keep:
        print(f"cleaned {clean(args.id)} earlier output(s) of '{args.id}'\n")

    wall0, cpu0 = time.monotonic(), cpu_s()
    manifest = orchestrator.execute(args.id, paper=args.paper, llm_mode=llm.STUB)
    wall, cpu = time.monotonic() - wall0, cpu_s() - cpu0

    s = summary(args.id)
    print("\n" + "=" * 78)
    print(f"WHAT CAME OUT  ({_profile.banner().split(' - ')[0]})")
    print(f"  chips           {len(s['chips'])}: {', '.join(s['chips'])}  ({s['handoffs']} hand-off(s))")
    print(f"  chip specs      {len(s['chip_specs'])}  e.g. {s['chip_specs'][:1]}")
    print(f"  protocols       {len(s['protocols'])}  e.g. {s['protocols'][:1]}")
    print(f"  S9 verify       {'PASS' if s['verify_ok'] else 'FAIL'}: {s['verify_reason']}")
    print(f"  S10 flow        findings by severity {s['cfd_findings'] or '{}'}; solver "
          f"{'TRUSTED' if s['cfd_trusted'] else 'UNTRUSTED'}"
          + ("" if s["cfd_trusted"] else f" ({s['cfd_validation']})"))
    print(f"  S11 print files {len(s['print_files'])}  e.g. {s['print_files'][:3]}")
    print(f"  S12 report      {s['report']}")
    reruns = manifest.get("reruns") or []
    print(f"  re-run loops    {len(reruns)}"
          + "".join(f"\n    {r['requested_by']} -> {r['rerun_from']}: {r['reason'][:110]}" for r in reruns))
    print(f"  stages          {manifest.get('summary')}")
    print(f"  time            {wall:.0f} s wall, {cpu:.0f} s CPU "
          f"(load average now {os.getloadavg()[0]:.0f} on {os.cpu_count()} cores)")
    print("=" * 78)
    if args.json:
        print(json.dumps({"profile": args.profile, "wall_s": wall, "cpu_s": cpu, **s}, indent=2))
    return 0 if manifest.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""S12 report: everything the run produced, in one short document.

    python assay2chip/stages/s12_report.py --id my_assay
    python assay2chip/stages/s12_report.py --id my_assay --stdout     # print, write nothing

Writes `assay2chip/work/<id>/report.md` and `report.json`. Same numbers in both, because
both come out of `assay2chip/report/model.collect()` - see `assay2chip/report/README.md`.

What it answers, and nothing else:

  * where every assay step ended up: `chip` | `op` | `off_chip` | `missing`
  * the headline numbers: chips, on-chip fraction of steps, **NON-AUTOMATED hand-offs**
    (the stated goal is no human intervention, so that count is the honest answer),
    unresolved questions from S1, CFD findings by severity with fixes applied and
    proposed, and the fabrication bundle paths
  * an HONEST LIMITS section, always present, saying what was simulated rather than
    measured

This stage NEVER fails a run. A report that refuses to exist because an earlier stage did
not run is worse than useless: the report is how you find out what did not run. Missing
inputs are listed in `missing` and shown in the document, and the exit code is 0 unless
the report itself could not be written.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip import report as report_pkg  # noqa: E402
from assay2chip.lib import io as stage_io  # noqa: E402

# Both outputs are inside assay2chip/work/<id>/, and only paths outside it need declaring
# (assay2chip/stages/__init__.py). report.json goes through write_stage, because 'report'
# is a known kind; it has no schema yet, so it is written unvalidated and says so.
WRITES: list[str] = []


def build(assay_id: str) -> tuple[dict, str]:
    """(report.json payload, report.md text). Reads nothing twice, writes nothing."""
    data = report_pkg.collect(assay_id)
    return data, report_pkg.render(data)


def write(assay_id: str) -> tuple[dict, Path, Path]:
    """Build and write both files. Returns (payload, json path, md path)."""
    data, text = build(assay_id)
    work = stage_io.work_dir(assay_id, create=True)
    json_path = stage_io.write_stage(assay_id, "report", data)
    md_path = stage_io.write_text(work / "report.md", text)
    return data, json_path, md_path


def run(assay_id: str = "", *, id: str = "", **_ignored) -> dict:
    """Stage entry point for assay2chip/run.py. Never raises on a missing earlier stage."""
    data, json_path, md_path = write(assay_id or id)
    return {
        "report": data["headline"],
        "wrote": [p.resolve().relative_to(ROOT).as_posix() for p in (md_path, json_path)],
        "missing": data["missing"],
    }


def render_console(data: dict) -> str:
    """The one-screen version, for the end of a run."""
    head = data["headline"]
    lines = [f"s12_report: {data['id']}"]
    pairs = [
        ("chips", head["chips"]),
        ("steps on chip", f"{head['steps_on_chip']}/{head['assay_steps']}"
                          + (f" ({head['on_chip_fraction']:.0%})"
                             if head["on_chip_fraction"] is not None else "")),
        ("hand-offs needing a human", f"{head['handoffs_not_automated']}/{head['handoffs']}"),
        ("unresolved from S1", head["unresolved_from_s1"]),
        ("CFD", ", ".join(f"{n} {s}" for s, n in head["cfd_by_severity"].items() if n)
                or "none"),
        ("CFD fixes", f"{head['cfd_fixes_applied']} applied, "
                      f"{head['cfd_fixes_proposed']} proposed"),
        ("S9 verify", {True: "PASSED", False: "FAILED", None: "did not run"}[head["verify_ok"]]),
        ("print bundles", f"{head['bundles_ready']}/{head['chips']}"),
    ]
    lines += [f"  {name:<26} {value}" for name, value in pairs]
    for line in data["missing"]:
        lines.append(f"  not run: {line}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="s12_report",
        description="S12: write assay2chip/work/<id>/report.md and report.json")
    parser.add_argument("--id", required=True, help="work id: assay2chip/work/<id>/")
    parser.add_argument("--stdout", action="store_true",
                        help="print report.md and write nothing")
    parser.add_argument("--json", action="store_true", help="print report.json instead")
    args = parser.parse_args(argv)

    if args.stdout or args.json:
        data, text = build(args.id)
        print(json.dumps(data, indent=2) if args.json else text)
        return 0

    data, json_path, md_path = write(args.id)
    print(render_console(data))
    for path in (md_path, json_path):
        print(f"  wrote {path.resolve().relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""A fast stand-in for the slow job kinds, used ONLY by the deployment smoke test.

    python deploy/local/fake_stage.py design|foam|fabricate|report RUN_ID [CHIP STEP OUT]

It proves the queue, the fan-out, retries, cancellation and artefact storage without
spending minutes in S5-S10 or hours in OpenFOAM. Every file it writes says FAKE. It runs
inside a run's workspace (a copy of the source tree), never in the checkout.

Test hooks, read from the run's own raw.json (so they apply per run):
    SLOW_FAKE_DESIGN      design sleeps 60 s   (to cancel it mid-flight)
    SLOWISH_FAKE_DESIGN   design sleeps 4 s    (to stop the service mid-job)
and from the environment:
    A2C_TEST_FAIL_ONCE=foam   the first attempt of each foam job exits 3 (to prove retry)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

LABEL = "FAKE (deploy smoke test) - not a pipeline result"
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082")


def work(run_id: str) -> Path:
    return Path("assay2chip") / "work" / run_id


def write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, (bytes, bytearray)):
        path.write_bytes(data)
    elif isinstance(data, str):
        path.write_text(data)
    else:
        path.write_text(json.dumps(data, indent=2) + "\n")


def manifest(run_id: str, stages) -> None:
    write(work(run_id) / "run_manifest.json", {
        "id": run_id, "fake": True, "note": LABEL, "status": "ok",
        "stages": [{"stage": s, "name": "fake", "status": "ok", "duration_s": 0.0}
                   for s in stages]})


def design(run_id: str) -> None:
    raw = (work(run_id) / "raw.json").read_text()
    if "SLOWISH_FAKE_DESIGN" in raw:
        time.sleep(4)
    elif "SLOW_FAKE_DESIGN" in raw:
        time.sleep(60)
    chips = [c["chip_id"] for c in
             json.loads((work(run_id) / "partition.json").read_text())["chips"]]
    for chip in chips:
        write(Path("chip") / ("%s.json" % chip), {"id": chip, "placeholder": True,
                                                  "note": LABEL})
        write(Path("protocols") / ("%s.json" % chip), {
            "id": chip, "placeholder": True, "note": LABEL,
            "steps": [{"step": 1, "operation": "prime", "flow_ul_min": 10},
                      {"step": 2, "operation": "wash", "flow_ul_min": 20},
                      {"step": 3, "operation": "incubate", "flow_ul_min": 0}]})
    write(work(run_id) / "cfd" / "findings.json", {"note": LABEL, "findings": []})
    manifest(run_id, ["s5", "s6", "s7", "s8", "s9", "s10"])
    print("  s5..s10 FAKE design: %d chip(s)" % len(chips))


def foam(run_id: str, chip: str, step: str, out: str) -> None:
    out_dir = Path(out)
    marker = out_dir / ".failed_once"
    if os.environ.get("A2C_TEST_FAIL_ONCE") == "foam" and not marker.exists():
        write(marker, "x")
        print("FAKE foam: failing the first attempt on purpose")
        sys.exit(3)
    render = out_dir / "foam" / "render"
    write(render / ("%s.render.json" % chip), {"chip": chip, "step": step, "note": LABEL})
    write(render / ("%s_step%s.png" % (chip, step)), PNG_1X1)
    # Bulk that must NOT be uploaded: a mesh and a time directory.
    write(out_dir / "foam" / chip / "all" / "constant" / "polyMesh" / "points", "(0 0 0)")
    write(out_dir / "foam" / chip / "all" / "0.5" / "U", "uniform (0 0 0)")
    print("  FAKE foam %s step %s" % (chip, step))


def fabricate(run_id: str) -> None:
    for path in sorted(Path("chip").glob("%s_*.json" % run_id)):
        chip = path.stem
        write(Path("cad") / "exports" / chip / ("%s.stl" % chip),
              "solid fake\nendsolid fake\n")
    manifest(run_id, ["s11"])


def report(run_id: str) -> None:
    summary = json.loads((work(run_id) / "foam" / "summary.json").read_text())
    write(work(run_id) / "report.md", "# %s\n\n%s\n\nOpenFOAM cases: %s\n" % (
        run_id, LABEL, json.dumps(summary["counts"])))
    manifest(run_id, ["s12"])


def main(argv) -> int:
    kind, run_id = argv[0], argv[1]
    if kind == "design":
        design(run_id)
    elif kind == "foam":
        foam(run_id, argv[2], argv[3], argv[4])
    elif kind == "fabricate":
        fabricate(run_id)
    elif kind == "report":
        report(run_id)
    else:
        print("unknown kind %r" % kind)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Rebuild the two assay variants from MJ's pipeline chain.

    python tools/build_assay_variants.py --base <MJ's pipeline file>    # writes the two variants
    python tools/choose_assay.py capseq                                 # then point pipeline_mvp.json at one

MJ maintains the chain of chips (which chip does which job, the ports, the build blocks). The two
assays are derived from it:

  protocols/pipeline_capseq.json  CapSeq, RNA: MJ's chain as it is. Barcoding is not a stage of its
      own (MJ, fcc6ef2): the cells carry a per-lane barcode from Module 1, reverse transcription in
      Module 3's capsule trap adds none, and Module 4's index PCR adds the sublibrary index.
  protocols/pipeline_tipseq.json  TIP-seq, chromatin DNA, an ALTERNATIVE to MJ's plan: culture -> permeabilise + tagment (a bench
      PLACEHOLDER) -> encapsulation (spc_demo_v1) -> barcoding in oil (sctip_spc_v1) -> release
      (module5) -> index PCR (module4), release before PCR as TIP-seq does. Module entries are
      copied from MJ's chain; sctip_spc_v1 is used read-only.

When MJ edits protocols/pipeline_mvp.json directly, tools/validate.py reports that it no longer
matches its variant: run this with --base protocols/pipeline_mvp.json, then choose_assay.py.
Standard library + jsonschema.
"""

from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "tools"))

import schema_io  # noqa: E402

DRILL = "drilled_0_8mm_5mm_pitch"


def unknown(a: str, b: str, why: str) -> dict:
    return {"from": a, "to": b, "status": "unknown", "warning": why}


def capseq(base: dict) -> dict:
    """MJ's chain as it is. Barcoding is not a stage of its own (MJ, fcc6ef2): the cells carry a
    per-lane barcode from Module 1, reverse transcription in Module 3's trap adds none, and Module
    4's index PCR adds the sublibrary index."""
    if any(m["module"] in ("barcoding", "hairpin") for m in base["modules"]):
        raise SystemExit("this base still has the old barcoding / hairpin placeholder stages; build from MJ's chain: "
                         "git show origin/mj/ipsc-to-mgl-end-to-end:protocols/pipeline_mvp.json > mj.json, then --base mj.json")
    out = copy.deepcopy(base)
    out.pop("same_as", None)
    out.update(id="pipeline_capseq", variant="capseq", name="CapSeq (RNA): " + base["name"].split(": ", 1)[-1])
    out["description"] = (base.get("description", "") + " CAPSEQ VARIANT: MJ's chain as it is. Barcoding is not a stage of its "
        "own (fcc6ef2): the cells carry a per-lane barcode from Module 1, reverse transcription in Module 3's capsule trap adds "
        "none, and Module 4's index PCR adds the sublibrary index, so the data is lane-level, not single-cell "
        "(protocols/pipeline_status.md).").strip()
    return out


def tipseq(base: dict) -> dict:
    by = {m["module"]: copy.deepcopy(m) for m in base["modules"]}
    enc = by["module2"]
    enc["note"] = "Capsules for the barcoding chip: sctip_spc_v1's own encapsulator (stage A) is retired."
    m5 = by["module5"]
    m5["note"] = "Release first: in TIP-seq the DNA leaves the capsules before index PCR."
    m4 = by["module4"]
    m4["note"] = "Index PCR after release, as TIP-seq does."
    road = copy.deepcopy(base.get("roadmap", []))
    out = {
        "kind": "pipeline", "id": "pipeline_tipseq", "variant": "tipseq",
        "name": "TIP-seq (chromatin DNA): culture -> permeabilise + tagment (bench) -> capsules -> barcoding in oil -> release -> index PCR",
        "placeholder": True,
        "description": ("An alternative to MJ's plan, kept for comparison. TIP-seq, chromatin DNA. The bench step (permeabilise + tagment) is a PLACEHOLDER. sctip_spc_v1 is MJ's capsule + "
            "single-copy barcoding chip (read-only); its own encapsulator (stage A) is retired, so its capsules come from spc_demo_v1, "
            "the only encapsulation geometry that can be printed. Release (module5_v1) comes BEFORE index PCR (module4_v1), as TIP-seq "
            "does; module5_v1's design note says either order works. protocols/pipeline_status.md lists sctip_spc_v1 as retired in the "
            "CapSeq plan; it is used here because this variant needs its barcoding in oil."),
        "modules": [
            by["module1"],
            {"module": "bench", "name": "Permeabilise + tagment (bench)", "status": "bench", "status_note": "placeholder · bench step",
             "owner": "TBC", "build": {"method": "bench, in tubes", "connector": "TBC"}, "built_for_demo": False,
             "note": "Placeholder for TIP-seq's steps on the harvested cells before encapsulation: permeabilise, then tagment with Tn5 at "
                     "the bench. No chip; the conditions come from the TIP-seq paper once MJ decides on the assay."},
            enc,
            {"module": "sctip", "name": "Barcoding in oil (sctip_spc_v1)", "status": "draft",
             "status_note": "MJ's sctip chip; listed as retired in the CapSeq plan", "owner": "MJ",
             "chip": "chip/sctip_spc_v1.json", "protocol": "protocols/sctip_spc_v1.json",
             "build": copy.deepcopy(enc.get("build", {"method": "TBC", "connector": DRILL})), "built_for_demo": False,
             "note": "Packs the capsules, adds the amplification mix and single-copy barcode, drops one capsule per droplet for the "
                     "off-chip thermocycler, breaks the emulsion and washes the capsules in its trap."},
            m5,
            m4,
        ],
        "handoffs": [
            {"id": "module1_to_bench", "carries": "cells", "from": {"module": "module1", "port": "p_mid"}, "to": {"module": "bench", "port": "TBC"},
             "connector": unknown(DRILL, "TBC", "Bench step: tubing into a tube, no chip port."), "note": "Harvested cells to the bench."},
            {"id": "bench_to_encapsulation", "carries": "cells", "from": {"module": "bench", "port": "TBC"},
             "to": {"module": "module2", "port": "in_core"},
             "connector": unknown("TBC", DRILL, "From a tube into spc_demo_v1's 0.8 mm drilled core inlet."),
             "note": "Permeabilised, tagmented cells into the core stream."},
            {"id": "encapsulation_to_sctip", "carries": "capsules", "from": {"module": "module2", "port": "out_caps"},
             "to": {"module": "sctip", "port": "in_load"}, "connector": {"from": DRILL, "to": DRILL, "status": "match"},
             "note": "Cured capsules (curing is off-chip), close-packed, through in_load's selector valve."},
            {"id": "sctip_to_release", "carries": "capsules",
             "from": {"module": "sctip", "port": "out_waste", "note": "The recovery line of its selector valve: the bed is back-flushed out."},
             "to": {"module": "module5", "port": "in_lib"}, "connector": {"from": DRILL, "to": DRILL, "status": "match"},
             "note": "Barcoded capsules to release."},
            {"id": "release_to_index_pcr", "carries": "library", "from": {"module": "module5", "port": "out_lib"},
             "to": {"module": "module4", "ports": ["in_s1", "in_s2", "in_s3", "in_s4"]},
             "connector": {"from": DRILL, "to": DRILL, "status": "match"}, "note": "Released DNA split four ways for index PCR."},
            {"id": "index_pcr_to_roadmap", "carries": "library",
             "from": {"module": "module4", "port": "TBC", "note": "Module 4 has no product outlet; which port each sublibrary leaves through is not stated."},
             "to": {"roadmap": road[0]["id"]} if road else {"roadmap": "library_out"}, "note": "Indexed sublibraries on, off-chip."},
        ] + [copy.deepcopy(h) for h in base["handoffs"] if "roadmap" in h["from"]],
        "roadmap": road,
    }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", type=Path, required=True, help="MJ's pipeline chain (a pipeline JSON)")
    args = ap.parse_args(argv)
    import validate  # noqa: E402  (after sys.path is set)
    base = schema_io.load_json(args.base)
    bad = 0
    for data in (capseq(base), tipseq(base)):
        report = validate.Report()
        validate.check_pipeline(data["id"], data, report)
        for e in report.errors:
            print(f"ERROR {data['id']}: {e}", file=sys.stderr)
        bad += len(report.errors)
        schema_io.write_json(ROOT / "protocols" / f"{data['id']}.json", data)
        print(f"wrote protocols/{data['id']}.json: {' -> '.join(m['module'] for m in data['modules'])} "
              f"({len(report.errors)} errors, {len(report.warnings)} warnings)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

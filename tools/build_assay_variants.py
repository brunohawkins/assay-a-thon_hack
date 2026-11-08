"""Rebuild the two assay variants from MJ's pipeline chain.

    python tools/build_assay_variants.py --base <MJ's pipeline file>    # writes the two variants
    python tools/choose_assay.py capseq                                 # then point pipeline_mvp.json at one

MJ maintains the chain of chips (which chip does which job, the ports, the build blocks). The two
assays are derived from it:

  protocols/pipeline_capseq.json  CapSeq, RNA: MJ's chain as it stands. His chain now carries a REAL
      single-cell barcoding stage between the capsule trap (module3) and the sublibrary split
      (module4) - one clonal barcode per capsule, generated in its own oil droplet (stage 4b in
      protocols/pipeline_status.md) - so nothing is injected. Only if a base ever arrives WITHOUT a
      barcoding stage does this fall back to the old split-and-pool + hairpin PLACEHOLDERS, which is
      how the dropped plate-based scheme used to be represented.
  protocols/pipeline_tipseq.json  TIP-seq, chromatin DNA: culture -> permeabilise + tagment (a bench
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


def core_name(name: str) -> str:
    """The chain's own name, without the prefixes earlier rebuilds and choose_assay.py put on it
    ("MVP pipeline (capseq): ", "CapSeq (RNA): "), so a rebuild never stacks them."""
    while True:
        head, sep, rest = name.partition(": ")
        if sep and (head.startswith("MVP pipeline") or head in ("CapSeq (RNA)", "TIP-seq (chromatin DNA)")):
            name = rest
        else:
            return name


def real_barcoding(base: dict) -> dict | None:
    """MJ's chain's own barcoding stage, if it has one with a chip behind it."""
    for m in base["modules"]:
        if m["module"] == "barcoding" and m.get("status") not in (None, "missing") and m.get("chip"):
            return m
    return None


def capseq(base: dict) -> dict:
    if real_barcoding(base) is not None:
        return capseq_as_is(base)
    return capseq_with_placeholders(base)


def capseq_as_is(base: dict) -> dict:
    """MJ's chain already barcodes per cell: copy it, add the variant fields, inject nothing."""
    out = copy.deepcopy(base)
    out.pop("same_as", None)
    out.update(id="pipeline_capseq", variant="capseq",
               name="CapSeq (RNA): " + core_name(base["name"]))
    bc = real_barcoding(out)
    out["description"] = (base.get("description", "") + " CAPSEQ VARIANT: this is MJ's chain unchanged. The barcoding stage "
        f"between the capsule trap and the sublibrary split is REAL ({bc.get('chip')}, {bc.get('protocol')}): one clonal "
        "barcode per capsule, generated in that capsule's own oil droplet, so the data is single-cell and not lane-level. The "
        "chemistry is ours; the droplet handling it needs is below the 200 um in-plane floor and runs on bought hardware, which "
        "is why the stage has a design and a protocol but no printable chip of ours (protocols/pipeline_status.md, stage 4b). "
        "The old plate-based split-and-pool rounds and their hairpin ligation stay dropped (fcc6ef2); they are NOT what makes "
        "this assay single-cell.").strip()
    return out


def capseq_with_placeholders(base: dict) -> dict:
    out = copy.deepcopy(base)
    out.pop("same_as", None)
    out.update(id="pipeline_capseq", variant="capseq",
               name="CapSeq (RNA): " + core_name(base["name"]) + " (+ barcoding and hairpin placeholders)")
    out["description"] = (base.get("description", "") + " CAPSEQ VARIANT: split-and-pool barcoding (BC2, BC3) and the hairpin "
        "ligation with its pooled wash sit between the capsule trap and the sublibrary split as PLACEHOLDERS (no chip): MJ dropped "
        "them in fcc6ef2, so the cells keep only their per-lane barcode and the data is lane-level; they come back only if the team "
        "wants single-cell resolution (protocols/pipeline_status.md).").strip()
    mods = [m for m in out["modules"] if m["module"] not in ("barcoding", "hairpin")]
    i = next(k for k, m in enumerate(mods) if m["module"] == "module3")
    mods[i + 1:i + 1] = [
        {"module": "barcoding", "name": "Barcoding: split and pool (BC2, BC3)", "status": "missing",
         "status_note": "placeholder · dropped by MJ for now", "owner": "TBC", "build": {"method": "TBC", "connector": "TBC"},
         "built_for_demo": False,
         "note": "Placeholder. MJ dropped split-and-pool barcoding (fcc6ef2): the cells keep only the per-lane barcode they get in "
                 "Module 1, so the data is lane-level, not single-cell. This stage comes back only if the team decides single-cell "
                 "resolution is needed (open question in protocols/pipeline_status.md)."},
        {"module": "hairpin", "name": "Hairpin ligation + pooled wash", "status": "missing",
         "status_note": "placeholder · dropped with the barcoding", "owner": "TBC", "build": {"method": "TBC", "connector": "TBC"},
         "built_for_demo": False,
         "note": "Placeholder: the hairpin ligation after the last barcode round and the wash of the pooled capsules. Dropped together "
                 "with split-and-pool barcoding (fcc6ef2)."},
    ]
    out["modules"] = mods
    nxt = mods[i + 3]["module"]  # what followed module3 in MJ's chain
    hands = []
    for h in out["handoffs"]:
        if h["from"].get("module") in ("barcoding", "hairpin") or h["to"].get("module") in ("barcoding", "hairpin"):
            continue  # an earlier placeholder chain
        if h["from"].get("module") == "module3" and h["to"].get("module") == nxt:
            to_end = {k: v for k, v in h["to"].items()}
            hands.append({"id": "module3_to_barcoding", "carries": h["carries"], "from": copy.deepcopy(h["from"]),
                          "to": {"module": "barcoding", "port": "TBC"},
                          "connector": unknown(DRILL, "TBC", "Barcoding is a placeholder: no chip, so no port style yet."),
                          "note": "Washed capsules (after RT in the trap) into the split tree."})
            hands.append({"id": "barcoding_to_hairpin", "carries": "capsules", "from": {"module": "barcoding", "port": "TBC"},
                          "to": {"module": "hairpin", "port": "TBC"}, "connector": unknown("TBC", "TBC", "Both are placeholders."),
                          "note": "Pooled capsules after the last barcode round."})
            hands.append({"id": f"hairpin_to_{nxt}", "carries": h["carries"], "from": {"module": "hairpin", "port": "TBC"},
                          "to": to_end, "connector": unknown("TBC", DRILL, "The hairpin step is a placeholder; Module 4 takes 0.8 mm "
                          "drilled ports. The rotary valve sits between them, so this is tubing either way."),
                          "note": h.get("note", "")})
            continue
        hands.append(copy.deepcopy(h))
    out["handoffs"] = hands
    return out


def m4_outlet(base: dict) -> dict:
    """Which port the indexed sublibraries leave Module 4 through.

    Read from the chip, because it changed: Module 4 had no product outlet at all and the
    indexed library was sealed in the chip, which this hand-off recorded as a flat TBC. The
    chain's own module4 -> module5 hand-off now names the real one, so take it from there
    rather than keeping a second, staler copy of the same fact here.
    """
    for h in base.get("handoffs", []):
        if h["from"].get("module") == "module4" and h["from"].get("port"):
            return copy.deepcopy(h["from"])
    return {"module": "module4", "port": "TBC",
            "note": "Module 4 has no product outlet; which port each sublibrary leaves through is not stated."}


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
        "description": ("An alternative to the chosen assay, kept for comparison. TIP-seq, chromatin DNA. The bench step (permeabilise + tagment) is a PLACEHOLDER. sctip_spc_v1 is MJ's capsule + "
            "single-copy barcoding chip (read-only); its own encapsulator (stage A) is retired, so its capsules come from spc_demo_v1, "
            "the only encapsulation geometry that can be printed. Release (module5_v1) comes BEFORE index PCR (module4_v1), as TIP-seq "
            "does; module5_v1's design note says either order works. Only stage A of sctip_spc_v1 is retired (spc_demo_v1 replaces it); "
            "stages B and C are the barcoding in oil this variant runs, and the same stage CapSeq calls 4b "
            "(protocols/pipeline_status.md). Steps 17-19 of its protocol - the gap-fill and the overnight linear amplification - are the "
            "chromatin tail, so they DO run here and are skipped on the RNA path."),
        "modules": [
            by["module1"],
            {"module": "bench", "name": "Permeabilise + tagment (bench)", "status": "bench", "status_note": "placeholder · bench step",
             "owner": "TBC", "build": {"method": "bench, in tubes", "connector": "TBC"}, "built_for_demo": False,
             "note": "Placeholder for TIP-seq's steps on the harvested cells before encapsulation: permeabilise, then tagment at the "
                     "bench with a transposase (CONFIDENTIALITY.md: no enzyme names). No chip; the conditions come from the published "
                     "method once MJ decides on the assay."},
            enc,
            {"module": "sctip", "name": "Barcoding in oil (sctip_spc_v1)", "status": "draft",
             "status_note": "MJ's sctip chip; stages B + C are also CapSeq's stage 4b", "owner": "MJ",
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
             "from": m4_outlet(base),
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

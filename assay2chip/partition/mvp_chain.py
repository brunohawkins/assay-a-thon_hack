"""The repo's own MVP chain, written as a unitops.json, so S4 can be held to it.

``protocols/pipeline_mvp.json`` and ``protocols/pipeline_status.md`` describe a chain that
real people designed by hand:

    module1 (culture) | cellprep | encapsulation (off-chip cure) | module3 (load, lysis,
    wash, RT) | barcoding (off-chip kit) | module4 (sublibrary split, index PCR) |
    module5 (release, bead clean-up) | SPRI and QC (off-chip)

If S4 is any good it should land on the same number of boards from the ops alone, and
every place it differs should be defensible. ``tests/test_assay2chip_partition.py`` runs
exactly that comparison, so this fixture is part of the verification rather than a demo.

**Where the numbers come from.** Nothing here is invented and nothing is copied from a
contract:

* Every op's ``load`` - what its place must HOLD - is the chambers' own geometry out of
  ``chip/*.json``, with 1 mm3 = 1 uL: module1_v1 ``n_cx`` 4.171 x 2.503 mm of floor per
  lane and three lanes; cell_prep_v1 ``cult_1`` 6.489 x 4.867 mm of floor;
  module3_v1_translated ``main_trap`` 11.32 x 11.32 x 0.3 mm of bed; module4_v1 four
  ``sub_n`` of 6.5 x 4.1 x 0.3; module5_v1 ``react_cx`` 9 x 3 x 0.3 and ``bead_cx``
  8 x 2.5 x 0.3. The junctions, the mixer and the encapsulation delay line are
  ``flow_through``: they carry a stream and hold nothing.
* ``fluid.volume_ul`` is the bench number and is traceability ONLY - the two washes carry
  192 uL, which is ``wash.chamber_volumes_per_wash`` sweeps of the trap and exactly the
  kind of tube figure that used to push every chip to target scale. Nothing sizes a board
  from it (``assay2chip/README.md``, "Bench volumes do not transfer").
* The two volumes at the module3 -> module4 join are the ones
  ``protocols/pipeline_status.md`` records as a broken join: "Module3 releases 76.9 uL;
  module4 draws 9.6 uL at near-packed density. Concentration step undocumented." They are
  in here on purpose, because a partition that does not notice a 77 uL stage feeding a
  9.6 uL one is not doing its job.
* Temperatures are ``protocols/thermal_map.md``'s full map.
* Capsule diameter is left to the caller, which passes
  ``contracts.capsule_diameter_um()``; the swollen size is derived from
  ``capsule.swell_factor`` by ``partition/ops.py``, never written down here.
* ``placement.part`` values are block ids from ``contracts/blocks.json``, matching the
  block each real chip uses.

The off-chip steps are the ones the repo itself marks OFF-CHIP or HOLE: the spin, strainer
and resuspension between cell prep and encapsulation; breaking the emulsion, washing the
oil and curing; the split-and-pool barcoding kit; and the SPRI and QC at the end.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from assay2chip.lib import contracts

CHAIN_SOURCE = "protocols/pipeline_mvp.json + protocols/pipeline_status.md"

#: What the hand-built chain actually is, for the test to compare against.
GROUND_TRUTH = {
    "chips": 6,
    "modules": ("module1", "cellprep", "module2", "module3", "module4", "module5"),
    "ports": {
        "module1_v1": 6, "cell_prep_v1": 4, "spc_demo_v1": 4,
        "module3_v1_translated": 6, "module4_v1": 5, "module5_v1": 5,
    },
    "off_chip": ("spin, 40 um strainer and resuspension",
                 "break the emulsion, wash the oil, cure",
                 "split-and-pool barcoding kit",
                 "pool, SPRI size selection, QC"),
}


def _op(number: int, kind: str, part: Optional[str], where: str, why: str,
        volume_ul: Optional[float] = None, temp_c: Optional[float] = None,
        particles: str = "none", diameter_um: Optional[float] = None,
        duration_s: Optional[float] = None, shear: str = "none",
        viscosity: str = "aqueous", note: str = "",
        need: str = "", load: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "op": number,
        "from_steps": [number],
        "kind": kind,
        "fluid": {"volume_ul": volume_ul, "viscosity_class": viscosity},
        "particles": {"kind": particles},
        "duration_s": duration_s,
        "temp_c": temp_c,
        "shear_class": shear,
        "placement": {"where": where, "why": why},
    }
    if load is not None:
        entry["load"] = load
    if particles != "none":
        entry["particles"]["diameter_um"] = diameter_um
    if part:
        entry["placement"]["part"] = part
    if need:
        entry["placement"]["need"] = need
    if note:
        entry["note"] = note
    return entry


def unitops(assay_id: str = "mvp_chain") -> Dict[str, Any]:
    """The MVP chain as a schema-valid unitops.json payload."""
    capsule = contracts.capsule_diameter_um()
    ops: List[Dict[str, Any]] = [
        # --- module1_v1: culture, lane barcode, harvest ----------------------
        _op(1, "culture", "culture_chamber", "on_chip",
            "an adherent monolayer grows in a culture chamber on chip",
            volume_ul=3.13 * 3, temp_c=37.0, particles="cells", diameter_um=15.0,
            duration_s=7 * 24 * 3600, shear="adherent_cells", viscosity="aqueous",
            note="module1_v1: three lanes of n_cx, 4.171 x 2.503 x 0.3 mm each, perfused "
                 "with culture media in a 37 C 5% CO2 incubator",
            load={"basis": "adherent_area", "area_mm2": 3 * 4.171 * 2.503,
                  "source": "derived",
                  "note": "three n_cx lanes of 4.171 x 2.503 mm of floor; an adherent "
                          "layer is an area and does not care how much medium sits on it"}),
        _op(2, "pool", "cross_junction", "on_chip",
            "the three lanes are back-flushed and pooled at the cross junction",
            volume_ul=3.13 * 3, temp_c=37.0, particles="cells", diameter_um=15.0,
            shear="adherent_cells",
            note="module1_v1 steps 179-182: back-flush, pool and release through p_mid",
            load={"basis": "flow_through", "source": "derived",
                  "note": "j_mid is a junction: it carries the pooled stream, holds nothing"}),

        # --- cell_prep_v1: detach, quench, count -----------------------------
        _op(3, "mix", "culture_chamber", "on_chip",
            "trypsin is added to the cells in the culture chamber",
            volume_ul=4.74, temp_c=37.0, particles="cells", diameter_um=15.0,
            shear="prelysis_rinse",
            note="cell_prep_v1 cult_1, 6.489 x 4.867 x 0.15 mm; trypsin_edta enzyme in",
            load={"basis": "adherent_area", "area_mm2": 6.489 * 4.867,
                  "source": "derived",
                  "note": "the same adherent layer, now in cult_1's floor area"}),
        _op(4, "incubate", "culture_chamber", "on_chip",
            "the dissociation is held at 37 C in the same chamber",
            volume_ul=4.74, temp_c=37.0, particles="cells", diameter_um=15.0,
            duration_s=600.0, shear="prelysis_rinse",
            note="dissociation.temp_c and dissociation.time_min",
            load={"basis": "adherent_area", "area_mm2": 6.489 * 4.867,
                  "source": "derived", "note": "the same floor, held at 37 C"}),
        _op(5, "mix", "serpentine_channel", "on_chip",
            "quench joins downstream in the serpentine mixer, a different place from the "
            "chamber, so it needs its own feed",
            volume_ul=9.5, temp_c=37.0, particles="cells", diameter_um=15.0,
            note="cell_prep_v1 mix_1; culture_media quench",
            load={"basis": "flow_through", "residence_s": 10.0, "source": "derived",
                  "note": "mix_1 is a serpentine mixer: a stream, not a chamber"}),
        _op(6, "collect", "polygon_chamber", "on_chip",
            "the counted suspension is collected in the QC chamber",
            volume_ul=9.5, temp_c=22.0, particles="cells", diameter_um=15.0,
            note="cell_prep_v1 qc_1",
            load={"basis": "suspension_volume", "cells": 2000.0,
                  "suspension_density_per_ul": 1500.0, "source": "paper",
                  "note": "target_cells at encapsulation.cell_conc_per_ul; the count and "
                          "the density are what the source states, so the volume follows"}),

        # --- OFF CHIP: spin, strainer, resuspend (pipeline_status 2b) --------
        _op(7, "transfer", None, "off_chip",
            "a spin, a bought 40 um strainer and resuspension in the viscous buffer "
            "happen in a tube at the bench; no block does any of them",
            volume_ul=9.5, temp_c=22.0, particles="cells", diameter_um=15.0,
            note="pipeline_status.md row 2b, OFF-CHIP: manual pipette, spin at "
                 "cell_wash.pellet_g, 40 um strainer, resuspend"),

        # --- spc_demo_v1: encapsulation --------------------------------------
        _op(8, "encapsulate", "flow_focusing", "on_chip",
            "a flow-focusing junction makes the GelMA capsules with oil and shell feeds",
            volume_ul=21.45, temp_c=37.0, particles="capsules", diameter_um=capsule,
            viscosity="viscous",
            note="spc_demo_v1 xj_1 and delay_1, 11 x 13 x 0.15 mm; fluorinated oil "
                 "continuous phase, gelma shell, cells as the core",
            load={"basis": "flow_through", "residence_s": 60.0, "source": "derived",
                  "note": "a flow-focusing junction and its delay line carry a stream; "
                          "S6 turns the residence time into the delay length"}),

        # --- OFF CHIP: break emulsion, wash oil, cure (status 3b, HOLE) ------
        _op(9, "cure", None, "off_chip",
            "gelation at 4 C, breaking the emulsion, washing the oil off and the 405 nm "
            "crosslink all happen in a tube on a cold block",
            volume_ul=21.45, temp_c=4.0, particles="capsules", diameter_um=capsule,
            duration_s=1200.0,
            note="pipeline_status.md row 3b, OFF-CHIP and a documented HOLE: manual, on "
                 "ice, no protocol for the emulsion break"),

        # --- module3_v1_translated: load, lyse, wash, RT, wash, release ------
        _op(10, "load", "trap_chamber", "on_chip",
            "the capsule bed is loaded into the trap and its carrier is displaced to waste",
            volume_ul=38.4, temp_c=22.0, particles="capsules", diameter_um=capsule,
            shear="capsules",
            note="module3_v1_translated main_trap, 11.32 x 11.32 x 0.3 mm, behind a "
                 "bought mesh insert",
            load={"basis": "packed_volume", "packed_volume_ul": 11.32 * 11.32 * 0.3,
                  "source": "derived",
                  "note": "the bed main_trap holds; loading is what decides the chamber"}),
        _op(11, "lyse", "trap_chamber", "on_chip",
            "chaotropic lysis runs in the trap and leaves through its own outlet",
            volume_ul=38.4, temp_c=22.0, particles="capsules", diameter_um=capsule,
            duration_s=900.0, shear="capsules",
            note="guanidine thiocyanate lysis buffer at lysis.gitc_molar; the lysate must "
                 "not meet the later enzyme path, hence lysis_waste",
            load={"basis": "packed_volume", "packed_volume_ul": 11.32 * 11.32 * 0.3,
                  "source": "derived", "note": "the same bed, now being lysed in place"}),
        _op(12, "wash", "trap_chamber", "on_chip",
            "the bed is washed in place; the wash still carries chaotrope",
            volume_ul=192.0, temp_c=22.0, particles="capsules", diameter_um=capsule,
            shear="capsules", note="wash_buffer through the same reagent line",
            load={"basis": "none", "source": "derived",
                  "note": "a wash is a sweep of main_trap: wash.chamber_volumes_per_wash "
                          "chamber volumes through a chamber that already exists, so it "
                          "adds no area"}),
        _op(13, "incubate", "trap_chamber", "on_chip",
            "reverse transcription is one isothermal hold inside the same trap",
            volume_ul=38.4, temp_c=42.0, particles="capsules", diameter_um=capsule,
            duration_s=3600.0, shear="capsules",
            note="rt.temp_c; reverse transcriptase rt_mix, plain poly(dT), no barcode "
                 "added here",
            load={"basis": "packed_volume", "packed_volume_ul": 11.32 * 11.32 * 0.3,
                  "source": "derived", "note": "the same bed, held at 42 C"}),
        _op(14, "wash", "trap_chamber", "on_chip",
            "a second wash clears the RT mix",
            volume_ul=192.0, temp_c=22.0, particles="capsules", diameter_um=capsule,
            shear="capsules", note="wash_buffer; enzyme carryover goes to the main waste",
            load={"basis": "none", "source": "derived",
                  "note": "another sweep of the same chamber"}),
        _op(15, "transfer", "trap_chamber", "on_chip",
            "the bed is back-flushed and leaves as one suspension",
            volume_ul=76.9, temp_c=22.0, particles="capsules", diameter_um=capsule,
            shear="capsules",
            note="module3_v1.json step 9 releases 76.9 uL through to_split_tree, driven "
                 "by a syringe pump",
            load={"basis": "flow_through", "source": "derived",
                  "note": "the bed leaves; nothing new is held"}),

        # --- OFF CHIP: split-and-pool barcoding kit --------------------------
        _op(16, "merge", None, "off_chip",
            "split-and-pool barcoding is a bench kit with no chip and no protocol in this "
            "repo, so it is off_chip by default",
            volume_ul=76.9, temp_c=22.0, particles="capsules", diameter_um=capsule,
            note="pipeline_mvp.json barcoding module: placeholder, dropped by MJ; manual "
                 "pipetting in plates if it comes back"),

        # --- module4_v1: sublibrary split + index PCR ------------------------
        _op(17, "split", "polygon_chamber", "on_chip",
            "an off-chip rotary valve fills the four sublibrary chambers one at a time",
            volume_ul=9.6, temp_c=22.0, particles="capsules", diameter_um=capsule,
            note="module4_v1 sub_1..sub_4, 6.5 x 4.1 x 0.3 mm each; the valve fills them "
                 "in the palindromic order A B C D D C B A; 16 x 0.6 uL = 9.6 uL drawn",
            load={"basis": "packed_volume",
                  "packed_volume_ul": 4 * 6.5 * 4.1 * 0.3, "source": "derived",
                  "note": "four sublibrary chambers of 6.5 x 4.1 x 0.3 mm"}),
        _op(18, "thermocycle", "polygon_chamber", "on_chip",
            "index PCR is the only true thermocycling in the whole chain",
            volume_ul=9.6, temp_c=98.0, particles="capsules", diameter_um=capsule,
            duration_s=3600.0,
            note="pcr.max_temp_c; 8-14 cycles of 98/60/72 C with amp_mix, on COC",
            load={"basis": "packed_volume",
                  "packed_volume_ul": 4 * 6.5 * 4.1 * 0.3, "source": "derived",
                  "note": "the same four chambers, cycled"}),

        # --- module5_v1: release + bead clean-up -----------------------------
        # Volumes here are the sublibrary that ARRIVES (9.6 uL, one pass), not the
        # chambers' capacity: react_cx holds 8.1 uL and bead_cx 6.0 uL, which is the
        # headroom the protocol works within rather than the volume moving between chips.
        _op(19, "mix", "polygon_chamber", "on_chip",
            "collagenase release buffer is added in the reaction chamber",
            volume_ul=9.6, temp_c=37.0, particles="capsules", diameter_um=capsule,
            note="module5_v1 react_cx, 9 x 3 x 0.3 mm = 8.1 uL of capacity; collagenase "
                 "cuts the gelatin backbone at 37 C, EDTA must be absent",
            load={"basis": "packed_volume", "packed_volume_ul": 9 * 3 * 0.3,
                  "source": "derived", "note": "react_cx holds one sublibrary"}),
        _op(20, "incubate", "polygon_chamber", "on_chip",
            "the release is held at 37 C in the same chamber",
            volume_ul=9.6, temp_c=37.0, particles="capsules", diameter_um=capsule,
            duration_s=1800.0, note="thermal_map.md step 16a",
            load={"basis": "packed_volume", "packed_volume_ul": 9 * 3 * 0.3,
                  "source": "derived", "note": "the same chamber, held at 37 C"}),
        _op(21, "separate", "polygon_chamber", "on_chip",
            "magnetic bead capture is a different bed in a different chamber",
            volume_ul=9.6, temp_c=22.0, particles="beads", diameter_um=1.0,
            note="module5_v1 bead_cx, 8 x 2.5 x 0.3 mm; magnetic bead capture with "
                 "ethanol washes, one sublibrary per pass",
            load={"basis": "packed_volume", "packed_volume_ul": 8 * 2.5 * 0.3,
                  "source": "derived", "note": "bead_cx holds the bead bed"}),

        # --- OFF CHIP: pool, SPRI, QC ----------------------------------------
        _op(22, "separate", None, "off_chip",
            "pooling and SPRI size selection are done by hand with ethanol; no protocol "
            "exists",
            volume_ul=9.6, temp_c=22.0, particles="none",
            note="pipeline_status.md row 7, OFF-CHIP and a HOLE: manual pipetting, "
                 "ethanol size selection"),
        _op(23, "image", None, "off_chip",
            "QC is Qubit, TapeStation and qPCR on bench instruments",
            volume_ul=9.6, temp_c=22.0, particles="none",
            note="pipeline_status.md row 7: qubit, tapestation, qpcr, by hand"),
    ]

    return {
        "id": assay_id,
        "source": {"kind": "md", "ref": "protocols/pipeline_status.md", "sha256": None},
        "placeholder": True,
        "ops": ops,
        "unresolved": [
            {"op": 9, "field": "duration_s",
             "question": "no protocol exists for breaking the emulsion and washing the "
                         "oil off (pipeline_status.md calls it a HOLE)",
             "assumed_from": "contracts/parameters.json gelation.time_min"},
            {"op": 17, "field": "fluid.volume_ul",
             "question": "module3 releases 76.9 uL and module4 draws 9.6 uL; the "
                         "concentration step between them is undocumented",
             "assumed_from": "protocols/pipeline_status.md broken join 4 -> 5"},
        ],
        "note": "The MVP chain of %s, written as unit operations so S4 can be compared "
                "with the boards people built by hand." % CHAIN_SOURCE,
    }

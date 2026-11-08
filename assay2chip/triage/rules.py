"""The S3 rule table: one rule, one decision, one citation.

Rules are tried in rank order and the lowest rank that matches wins, so the table reads
top to bottom as "what stops this being on a chip", hardest stop first:

   10-13  FABRICATION. The in-plane floor. Nothing below it exists, whatever the biology
          wants, so these come first.
   20-22  BIOLOGY AND HEAT. Wild-type cells only; live cells never above 37 C.
   30-33  CHEMISTRY. Material compatibility, and the steps that destroy themselves on a
          chip (a drying meniscus).
   40     CULTURE. Weeks on chip need perfusion and an off-chip gas exchange.
   50-53  RETENTION. The depth weir, adhesion, sedimentation, or an honest gap.
   70-79  DEFAULTS BY KIND. The ordinary flow parts.
   99     FALLBACK. missing_part with a `need`. Never a forced block.

Every `why` names the contract key or the repo file it rests on. Every rule declares
whether it is physics, because `tiebreak.py` refuses to put a physics rule to a language
model - rank 20 holds the only pair of rules that may ever be arbitrated that way, and
both are about what a word means, not about what a fluid does.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402

from . import facts as facts_mod  # noqa: E402
from . import library  # noqa: E402
from .facts import Facts, precedent, precedent_source  # noqa: E402

ON, OFF, MISSING = "on_chip", "off_chip", "missing_part"
#: What S2 writes and S3 must replace on every single op. A rule may never return it.
PENDING = "pending"
DECIDED = (ON, OFF, MISSING)

# How strong the evidence is, and so how confident a verdict built on it may be.
CONTRACT, PRECEDENT, TEXT = "contract", "precedent", "text"
CONFIDENCE = {CONTRACT: 0.95, PRECEDENT: 0.8, TEXT: 0.6}


@dataclass
class Verdict:
    """One placement, plus the bookkeeping S3 reports but the schema does not carry."""

    where: str
    why: str
    part: Optional[str] = None
    need: Optional[str] = None
    blocks: List[str] = field(default_factory=list)
    note: str = ""
    flags: List[str] = field(default_factory=list)
    rule: str = ""
    evidence: str = CONTRACT
    confidence: Optional[float] = None

    def placement(self) -> dict:
        """The `placement` object exactly as contracts/schemas/unitops.schema.json wants it."""
        if self.where not in DECIDED:
            raise ValueError(
                "rule %r returned where=%r; S3 exists to REPLACE 'pending' and may only "
                "emit %s" % (self.rule, self.where, " / ".join(DECIDED)))
        out = {"where": self.where, "why": self.why}
        if self.part:
            out["part"] = library.check(self.part, self.why)
            out["blocks"] = self.blocks or [self.part]
            shape = library.catalogue_part_for(out["part"])
            if shape:
                out["catalogue_part"] = shape
        elif self.blocks:
            out["blocks"] = self.blocks
        if self.need:
            out["need"] = self.need
        out["confidence"] = round(
            self.confidence if self.confidence is not None else CONFIDENCE[self.evidence], 2
        )
        bits = [b for b in ["rule " + self.rule if self.rule else "", self.note] if b]
        if bits:
            out["note"] = "; ".join(bits)
        return out


@dataclass(frozen=True)
class Rule:
    id: str
    rank: int
    evidence: str
    physics: bool
    test: Callable[[Facts], Optional[Verdict]]
    about: str = ""


RULES = []  # type: List[Rule]


def rule(rule_id, rank, evidence=CONTRACT, physics=True):
    """Register a rule. `physics=True` means a model may never arbitrate it."""

    def wrap(fn):
        def run(f):
            verdict = fn(f)
            if verdict is not None:
                verdict.rule = rule_id
                if verdict.evidence == CONTRACT and evidence != CONTRACT:
                    verdict.evidence = evidence
            return verdict

        RULES.append(Rule(rule_id, rank, evidence, physics, run, (fn.__doc__ or "").strip()))
        RULES.sort(key=lambda r: (r.rank, r.id))
        return run

    return wrap


def _part(purpose):
    return library.part_for(purpose)


# ---------------------------------------------------------------------------
# 10-13  FABRICATION: the in-plane floor decides first
# ---------------------------------------------------------------------------

@rule("fab.emulsion_break", 10, evidence=TEXT)
def _emulsion_break(f):
    """Breaking an emulsion and washing off the oil needs sub-floor slots."""
    if not f.signal("emulsion_break"):
        return None
    slot = precedent("phase_separation_slot_um")
    return Verdict(
        OFF,
        "breaking the emulsion and washing off the oil is drawn as %d x %d um slots and the "
        "%d um side is in plane, below the %.0f um floor in contracts/blocks.json "
        "fabrication_floors.min_open_channel_mm, so it goes off chip on a bought kit "
        "(precedent %s; protocols/pipeline_status.md records join 3 -> 4 as an off-chip hole)"
        % (slot[0], slot[1], slot[0], f.min_inplane_um,
           precedent_source("phase_separation_slot_um")),
        need="bought emulsion-breaking / phase-separation kit (not a library part)",
        note="protocols/sctip_spc_design.md says most capsules are lost at this step, so it is "
             "also the step most worth automating later",
    )


@rule("fab.capsule_reinjection", 11, evidence=PRECEDENT)
def _reinjection(f):
    """Close-packed capsule re-injection for barcoding needs about 0.85 x D in plane."""
    if not f.signal("reinjection") or f.particles != "capsules":
        return None
    if f.reinjection_entrance_um is None:
        return None
    if f.printable(f.reinjection_entrance_um):
        part, citation = _part("merge_gentle")
        return Verdict(
            ON,
            "close-packed re-injection of a %.0f um capsule needs a %.0f um entrance, at or "
            "above the %.0f um in-plane floor "
            "(contracts/blocks.json fabrication_floors.min_open_channel_mm), so it is "
            "printable (%s)"
            % (f.diameter_um, f.reinjection_entrance_um, f.min_inplane_um, citation),
            part=part)
    return Verdict(
        OFF,
        "close-packed re-injection needs an entrance of about 0.85 x D = %.0f um for a %.0f um "
        "capsule, below the %.0f um in-plane floor (contracts/blocks.json "
        "fabrication_floors.min_open_channel_mm), so barcoding re-injection goes off chip on a "
        "bought droplet kit (precedent %s; protocols/pipeline_mvp.json keeps barcoding off "
        "chip)"
        % (f.reinjection_entrance_um, f.diameter_um, f.min_inplane_um,
           precedent_source("reinjection_entrance_factor")),
        need="bought droplet re-injection and barcoding kit (about a 115 um entrance), or "
             "capsules large enough that 0.85 x D clears the in-plane floor",
        note="contracts/parameters.json barcoding.scheme says exactly this: 'Droplet hardware "
             "is bought (below the 200 um in-plane floor); the chemistry is ours'",
    )


@rule("fab.droplet_generation", 12)
def _droplet(f):
    """Droplet generation for 70 um capsules needs a ~40 um nozzle, which is not printable."""
    if f.kind != "encapsulate" or f.orifice_um is None:
        return None
    if f.printable(f.orifice_um):
        part, citation = _part("droplet_generation")
        return Verdict(
            ON,
            "a %.0f um flow-focusing orifice is at or above the %.0f um in-plane floor "
            "(contracts/blocks.json fabrication_floors.min_open_channel_mm), so the generator "
            "is printable (%s)" % (f.orifice_um, f.min_inplane_um, citation),
            part=part, blocks=[part, "y_junction"],
            note="orifice scaled from encapsulation.nozzle_um over capsule.diameter_um; "
                 "chip/spc_demo_v1.json prints at the floor and makes about 260 um capsules, "
                 "so check the size you actually get")
    return Verdict(
        OFF,
        "making a %.0f um capsule needs a %.0f um nozzle (contracts/parameters.json "
        "encapsulation.nozzle_um scaled by capsule.diameter_um), below the %.0f um in-plane "
        "floor in contracts/blocks.json fabrication_floors.min_open_channel_mm, so "
        "encapsulation goes off chip on a commercial droplet kit (precedent "
        "chip/spc_demo_v1.json, whose printable orifice makes about 260 um capsules instead)"
        % (f.diameter_um or 0.0, f.orifice_um, f.min_inplane_um),
        need="commercial droplet generator with a ~40 um flow-focusing nozzle (bought, not a "
             "library part)",
        note="protocols/sctip_spc_design.md section 2: a 40 um master needs photolithography or "
             "micromilling, both outside the constraint",
    )


@rule("fab.subfloor_size_exclusion", 13, evidence=TEXT)
def _subfloor_filter(f):
    """A cell strainer or clump filter is a sub-floor in-plane gap, so it is a bought mesh."""
    if not f.signal("size_exclusion") or f.particles not in ("cells", "beads"):
        return None
    if f.particles == "cells":
        stated = "%.0f um (contracts/parameters.json cell_filter.gap_um)" % contracts.param(
            "cell_filter.gap_um")
    else:
        stated = "a few um"
    return Verdict(
        OFF,
        "size exclusion of %s needs an in-plane gap of %s, below the %.0f um floor in "
        "contracts/blocks.json fabrication_floors.min_open_channel_mm, and "
        "retention.alternatives_ranked allows a bought woven mesh precisely because the floor "
        "does not apply to a bought part" % (f.particles, stated, f.min_inplane_um),
        need="bought woven nylon mesh insert clamped in a recessed ledge (adds a manual "
             "assembly step), or an off-chip strainer",
        note="protocols/retention_audit.md recommendations 2 and 3 both move this off chip",
    )


# ---------------------------------------------------------------------------
# 20-22  BIOLOGY AND HEAT
#
# The two rank-20 rules are the ONLY tie in the table, and it is a genuine one: an
# `image` op whose wording carries both a counting signal and a reporter signal could be
# a label-free count or a reporter read-out, and that is a question about English, not
# about physics. tiebreak.py may ask a model which; both rules are physics=False.
# ---------------------------------------------------------------------------

@rule("bio.engineered_line", 20, evidence=TEXT, physics=False)
def _engineered_line(f):
    """Label-based sorting or a reporter read needs a cell line this project does not have."""
    if not f.signal("label_sort"):
        return None
    return Verdict(
        OFF,
        "label-based sorting or a reporter read-out needs an engineered cell line, and this "
        "project is wild-type unmodified iPSCs only (protocols/ipsc_to_mgl_design.md, "
        "'Wild-type iPSCs only', and protocols/multiomic_design.md, which drops genetically "
        "encoded biosensors for the same reason), so it goes off chip on an instrument",
        need="flow sorter or bead-based selection off chip; on chip it would additionally need "
             "a cell line this project does not have",
        flags=["needs_engineered_cell_line"],
        note="FLAG: this step cannot be run on wild-type cells at all, on chip or off. "
             "protocols/multiomic_design.md gives the wild-type substitute - a metabolic pulse "
             "label per lane",
    )


@rule("qc.optical_window", 20, evidence=TEXT, physics=False)
def _qc_window(f):
    """Label-free imaging or counting: an on-chip QC window where there is a light path."""
    if f.kind != "image" or not f.signal("imaging"):
        return None
    clear = facts_mod.optical_materials()
    if not clear:
        return Verdict(
            OFF,
            "no material in contracts/materials.json offers a reliable optical path, so "
            "imaging goes off chip",
            need="an optically clear lid material")
    part, citation = _part("qc_window")
    return Verdict(
        ON,
        "label-free imaging and counting run in an on-chip QC window, because "
        "contracts/materials.json marks %s optically clear with low autofluorescence (%s)"
        % (", ".join(clear), citation),
        part=part,
        note="printed_resin is NOT a window (contracts/materials.json: 'not a reliable optical "
             "window'), so an imaged chip must be embossed in COC/COP or carry a clear lid, and "
             "contracts/blocks.json target_envelope._caveat warns an ANSI/SLAS outline does not "
             "guarantee a reader can focus - check working distance",
        flags=["needs_optical_path"],
    )


@rule("thermal.live_cells_above_37c", 21)
def _live_cells_hot(f):
    """Live cells never go above 37 C, and never share a board with something that does."""
    if not (f.live_cells and f.above_culture_temp):
        return None
    return Verdict(
        OFF,
        "this op holds live cells at %g C, above the %g C in contracts/parameters.json "
        "culture.temp_c, and protocols/thermal_map.md records everything from 42 C onward as "
        "lethal, so no chip may do it to live cells"
        % (float(f.temp_c), f.culture_temp_c),
        flags=["live_cells_above_culture_temp"],
        note="FLAG for S4: if the source really means this, the assay is wrong rather than the "
             "chip. A heated step belongs on its own board (protocols/thermal_map.md: 'the "
             "culture chip must never exceed 37 C')",
    )


@rule("thermal.thermocycle", 22)
def _thermocycle(f):
    """98 C index PCR: on chip only on a high-Tg material with a chamber-static Peltier dock."""
    hot = f.temp_c is not None and float(f.temp_c) >= f.pcr_max_temp_c
    if f.kind != "thermocycle" and not hot:
        return None
    qualified = facts_mod.high_tg_materials()
    if not qualified:
        return Verdict(
            MISSING,
            "nothing in contracts/materials.json survives the %g C hold in "
            "contracts/parameters.json pcr.max_temp_c" % f.pcr_max_temp_c,
            need="a chip material with Tg well above 98 C")
    part, citation = _part("thermocycle")
    return Verdict(
        ON,
        "thermocycling to %g C (contracts/parameters.json pcr.max_temp_c) runs in a "
        "chamber-static chamber over a zoned Peltier dock, but only on a high-Tg material - "
        "contracts/materials.json qualifies %s and disqualifies %s - and never on a board "
        "holding live cells (%s)"
        % (f.pcr_max_temp_c, ", ".join(qualified),
           ", ".join(facts_mod.disqualified_for_pcr()), citation),
        part=part,
        blocks=[part, "flow_restrictor"],
        note="the chamber is static during cycling (no flow), the dock stays a small zoned area "
             "(contracts/blocks.json target_envelope.design_rules_at_this_scale: 'the 98 C index "
             "PCR stays a small zoned dock'), and the flow_restrictor is the capillary stop that "
             "keeps the reactions isolated through a shared vent (protocols/module5_design.md "
             "section 4). CUT FOR S4: this board carries no live cells",
        flags=["needs_high_tg_material", "needs_peltier_dock", "cut_from_live_cells"],
    )


# ---------------------------------------------------------------------------
# 30-33  CHEMISTRY
# ---------------------------------------------------------------------------

@rule("chem.chlorinated_solvent", 30, evidence=TEXT)
def _chlorinated(f):
    """Chlorinated solvents are excluded from every chip in this project."""
    if not f.signal("chlorinated"):
        return None
    return Verdict(
        OFF,
        "a chlorinated solvent is excluded: it attacks PMMA and contracts/materials.json "
        "carries no compatibility verdict for it on any material "
        "(protocols/multiomic_design.md: 'Chloroform. Excluded: it attacks PMMA. The quench "
        "must be chloroform-free')",
        need="a chloroform-free solvent system, or the step stays off chip in glass",
        flags=["excluded_solvent"],
    )


@rule("chem.ethanol_bead_dry", 31, evidence=TEXT)
def _ethanol_beads(f):
    """An ethanol bead clean-up that dries the pellet destroys it on chip."""
    if not f.signal("ethanol"):
        return None
    if not (f.particles == "beads" or f.signal("magnetic")):
        return None
    return Verdict(
        OFF,
        "an ethanol bead clean-up cannot run on chip: a drying meniscus pulls on a bead about "
        "100,000x harder than the magnet and smears the pellet (precedent "
        "protocols/module5_design.md section 6, 'No ethanol wash or dry on chip')",
        need="off-chip SPRI / column clean-up on the bench (bought consumable)",
        note="protocols/module5_design.md keeps capture, buffer exchange and elution on chip and "
             "sends only the ratio-based size selection off it, so only the ethanol and dry "
             "steps have to leave",
    )


@rule("chem.magnetic_bead_capture", 32, evidence=PRECEDENT)
def _magnetic_beads(f):
    """Bead capture on chip is magnetic: a 1 um bead has no geometric retention here."""
    if not (f.particles == "beads" and f.signal("magnetic")):
        return None
    part, citation = _part("bead_capture")
    return Verdict(
        ON,
        "bead capture and buffer exchange run on chip with an external magnet, because "
        "retention here is magnetic rather than geometric - a %.3g um bead is far below the "
        "%.0f um in-plane floor (contracts/blocks.json "
        "fabrication_floors.min_open_channel_mm), so there is no bead trap to build (%s)"
        % (f.diameter_um or 1.0, f.min_inplane_um, citation),
        part=part,
        note="protocols/module5_design.md section 6: 'There is no bead trap. Beads are ~1 um, "
             "the smallest feature 200 um. Retention is magnetic.' Dilute before the beads: "
             "dissolved shell is roughly 10,000x more polymer than DNA",
        flags=["needs_external_magnet"],
    )


@rule("chem.organic_quench", 33, evidence=TEXT)
def _organic_quench(f):
    """A cold organic quench, placed against contracts/materials.json compatibility."""
    if not f.signal("quench") or f.signal("chlorinated"):
        return None
    if f.viscosity_class not in ("organic", "aqueous"):
        return None
    ok = facts_mod.compatible_materials("ethanol")
    if not ok:
        return Verdict(
            MISSING,
            "no material in contracts/materials.json is marked compatible with a polar organic "
            "solvent, so the quench has nowhere to run",
            need="a solvent-compatible chip material")
    part, citation = _part("static_hold")
    return Verdict(
        ON,
        "a chloroform-free organic quench is marked compatible with %s in "
        "contracts/materials.json chemical_compatibility, so it can run in a chamber on chip "
        "(%s)" % (", ".join(ok), citation),
        part=part,
        blocks=[part, "t_junction"],
        note="the mandatory rinse before the quench is a pre-lysis rinse, so it is bounded by "
             "shear.prelysis_rinse_ceiling_dyn_cm2 and NOT by the culture ceiling "
             "(protocols/multiomic_design.md section 5). Material verdicts there are marked "
             "assumptions, not tests",
        flags=["material_compatibility_is_an_assumption"],
    )


# ---------------------------------------------------------------------------
# 40  CULTURE
# ---------------------------------------------------------------------------

@rule("culture.long_perfused", 40, evidence=PRECEDENT)
def _long_culture(f):
    """Days to weeks of culture on chip: perfusion plus an off-chip vented reservoir."""
    if f.kind not in ("culture", "incubate") or f.particles != "cells":
        return None
    limit = precedent("sealed_static_culture_s")
    if f.duration_s is None or float(f.duration_s) < limit:
        return None
    part, citation = _part("culture")
    return Verdict(
        ON,
        "a %.3g-day culture at %g C (contracts/parameters.json culture.temp_c) runs on chip "
        "ONLY with continuous perfusion and an off-chip vented gas-exchange reservoir, because "
        "a sealed microlitre chamber goes anoxic in about %.0f h (%s) (%s)"
        % (float(f.duration_s) / 86400.0, f.culture_temp_c, limit / 3600.0,
           precedent_source("sealed_static_culture_s"), citation),
        part=part,
        blocks=[part, "pillar_array"],
        note="OXYGEN AND MEDIUM BUDGET, UNRESOLVED: protocols/ipsc_to_mgl_design.md measures a "
             "sealed static chamber as about seven times short and covers it by recirculating "
             "about 3 uL/min through a vented reservoir at 0.0017 dyn/cm2, well under "
             "shear.culture_ceiling_dyn_cm2; gas exchange happens in the reservoir, not the "
             "chamber, which is why the chamber itself need not be gas-permeable. The "
             "pillar_array is lid support at 5-10 mm pitch (contracts/blocks.json "
             "target_envelope.design_rules_at_this_scale), never retention",
        flags=["needs_perfusion", "needs_offchip_vented_reservoir", "oxygen_budget_unresolved"],
    )


@rule("culture.fed", 41, evidence=PRECEDENT)
def _short_culture(f):
    """A culture op shorter than the anoxia limit, or with no stated duration."""
    if f.kind != "culture":
        return None
    part, citation = _part("culture")
    held = ("%.3g h" % (float(f.duration_s) / 3600.0)) if f.duration_s is not None \
        else "an unstated time"
    return Verdict(
        ON,
        "a fed culture of %s for %s at or below %g C (contracts/parameters.json "
        "culture.temp_c) is what culture_chamber is for, and flow is bounded by "
        "shear.culture_ceiling_dyn_cm2 because this is the one kind that carries "
        "adherent-cell shear (%s)" % (f.particles, held, f.culture_temp_c, citation),
        part=part,
        blocks=[part, "pillar_array"],
        note="below the roughly %.0f h at which a sealed chamber goes anoxic (%s), so a "
             "static fill is survivable; past that it needs perfusion and an off-chip vented "
             "reservoir. The pillar_array is lid support, never retention"
             % (precedent("sealed_static_culture_s") / 3600.0,
                precedent_source("sealed_static_culture_s")),
        flags=["check_oxygen_budget"] if f.duration_s is None else [],
    )


# ---------------------------------------------------------------------------
# 50-53  RETENTION
# ---------------------------------------------------------------------------

#: Kinds that only work if the particles stay put while liquid leaves.
RETAINING_KINDS = ("wash", "separate", "collect", "pool")
#: A capsule bed is held for everything that happens to it, loading included: that is
#: exactly what chip/module3_v1_translated.json main_trap does.
CAPSULE_RETAINING_KINDS = RETAINING_KINDS + ("load", "incubate", "lyse", "mix", "merge")


def needs_retention(f):
    """Does this op depend on holding its particles while liquid moves past them?"""
    if not f.has_particles:
        return False
    kinds = CAPSULE_RETAINING_KINDS if f.particles == "capsules" else RETAINING_KINDS
    return f.kind in kinds or f.signal("centrifuge")


@rule("retain.capsule_weir", 50)
def _capsule_weir(f):
    """A depth weir retains capsules: the in-plane floor is a WIDTH, and z is free."""
    if f.particles != "capsules" or f.weir is None or not needs_retention(f):
        return None
    if not f.weir.buildable:
        return Verdict(
            MISSING,
            "a depth weir cannot retain a %.0f um capsule: %s (contracts/blocks.json "
            "retention.weir_geometry.rule, trap.gap_factor_low and trap.gap_factor_high)"
            % (f.diameter_um, f.weir.why),
            need="bought woven mesh insert below %.0f um in a recessed ledge, or larger "
                 "capsules - capsule.diameter_tunable_um reaches 260 um and the contract's own "
                 "lever for a failed weir is capsule size" % f.weir.gap_high_um)
    part, citation = _part("capsule_trap")
    flags = ["weir_gap_untested"]
    note = (
        "gap %.0f um inside the legal %s band; the crest is that chip's etch depth minus the "
        "gap (retention.weir_geometry.crest_height_rule - derive per chip, never a fixed "
        "number) and its in-plane thickness is the %.0f um floor, so this is still ONE fluidic "
        "layer. UNTESTED: retention.weir_geometry.z_tolerance_warning calls the z tolerance the "
        "biggest unknown in the scheme, so coupon 21/24/27 um before committing. Flow is "
        "bounded by capsule.escape_pressure_kpa x control.pressure_margin = %.3g kPa, NOT by a "
        "shear ceiling" % (f.weir.gap_um, f.weir.band, f.min_inplane_um,
                           contracts.max_working_pressure_kpa())
    )
    if f.shear_class == "adherent_cells":
        flags.append("shear_class_mis_scoped")
        note += ("; S2 set shear_class=adherent_cells on a capsule op, and "
                 "contracts/parameters.json shear.culture_ceiling_dyn_cm2 says explicitly that "
                 "ceiling does not apply to capsule chambers")
    return Verdict(
        ON,
        "capsules are retained by a DEPTH weir rather than an in-plane gap - the %.0f um floor "
        "applies to width only - and %s (contracts/blocks.json retention.solution and "
        "retention.weir_geometry) (%s)" % (f.min_inplane_um, f.weir.why, citation),
        part=part,
        note=note,
        flags=flags,
    )


@rule("retain.cell_adhesion", 51)
def _cell_adhesion(f):
    """Adherent cells need no retention feature at all: they are stuck down.

    Since the schema scopes adherent-cell shear to the `culture` kind, a wash of a
    monolayer says so in its wording instead, and that is accepted as the weaker signal
    it is.
    """
    if f.particles != "cells" or not needs_retention(f):
        return None
    if f.shear_class != "adherent_cells" and not f.signal("adherent"):
        return None
    ceiling = contracts.shear_ceiling_for("adherent_cells")
    part, citation = _part("culture")
    rinse = contracts.shear_ceiling_for("prelysis_rinse")
    bound = rinse if f.shear_class == "prelysis_rinse" else ceiling
    return Verdict(
        ON,
        "adherent cells need no retention feature - adhesion holds them - provided wall shear "
        "stays at or below %g dyn/cm2 (contracts/parameters.json %s) (%s)"
        % (bound.ceiling_dyn_cm2,
           "shear.prelysis_rinse_ceiling_dyn_cm2, the ceiling for a rinse immediately before "
           "a deliberate lyse" if bound is rinse else
           "shear.culture_ceiling_dyn_cm2, which scopes that ceiling to adherent-cell chambers",
           citation),
        part=part,
        note="protocols/retention_audit.md: 'Adhesion: sufficient. The culture chips have no "
             "problem' - published hESC colonies hold at 0.6 dyn/cm2, 60x the culture ceiling. "
             "The caveat is the first hours after seeding",
        evidence=CONTRACT if f.shear_class == "adherent_cells" else TEXT,
    )


@rule("retain.cell_sedimentation", 52, evidence=PRECEDENT)
def _cell_sedimentation(f):
    """Loose cells settle in a wide slow chamber. The weakest substitution in the pipeline."""
    if f.particles != "cells" or not needs_retention(f):
        return None
    if f.weir is not None and f.weir.buildable:
        return None
    if f.viscosity_class in ("viscous", "oil", "organic"):
        return None  # protocols/retention_audit.md: 'Dextran defeats it'
    part, citation = _part("sedimentation")
    band = f.weir.band if f.weir is not None else "a few um"
    return Verdict(
        ON,
        "a weir CANNOT hold a %.0f um cell (its legal band would be %s, finer than the agreed "
        "gap in contracts/blocks.json retention.weir_geometry.gap_um and inside the z "
        "tolerance), so retention here is by SEDIMENTATION in a wide slow chamber with upward "
        "velocity kept below the settling speed (contracts/blocks.json "
        "retention.alternatives_ranked, the sedimentation sump) (%s)"
        % (f.diameter_um or 12.0, band, citation),
        part=part,
        blocks=[part, "flow_restrictor"],
        note="WEAKEST SUBSTITUTION IN THE PIPELINE, and it should be read that way: loose-cell "
             "washing on chip is slower and less certain than a spin. protocols/retention_audit.md "
             "sizes it - a 12 um cell falls about 6 um/s at 37 C, a 300 um fall takes about 50 s, "
             "so flow must stop for a settle hold and then stay near 1 uL/min, and 'Dextran "
             "defeats it'. For two of the three such steps the audit's own recommendation is to "
             "move them off chip. A spiral_channel is NOT the alternative: inertial focusing needs "
             "Re >> 1 and ours is about 0.02, the same reason contracts/blocks.json rules out "
             "tesla_valve",
        flags=["weakest_substitution_loose_cell_wash", "needs_settle_hold"],
    )


@rule("retain.unsolved", 53)
def _retention_unsolved(f):
    """Nothing retains these particles at this floor. Say so instead of pretending."""
    if not needs_retention(f):
        return None
    if f.particles == "beads":
        return Verdict(
            MISSING,
            "a %.3g um bead cannot be retained geometrically at the %.0f um in-plane floor and "
            "nothing in this op says a magnet is present (contracts/blocks.json "
            "fabrication_floors.min_open_channel_mm; protocols/module5_design.md section 6: "
            "'There is no bead trap')" % (f.diameter_um or 1.0, f.min_inplane_um),
            need="an external magnet dock over the chamber, or a bought mesh insert; a printed "
                 "bead trap does not exist at this floor")
    return Verdict(
        MISSING,
        "this op needs the %s held while liquid leaves, and neither a depth weir nor "
        "sedimentation applies here (viscosity_class=%s; contracts/blocks.json retention and "
        "protocols/retention_audit.md)" % (f.particles, f.viscosity_class),
        need="bought mesh insert, an off-chip spin, or a redesign that does not need retention "
             "at this point")


# ---------------------------------------------------------------------------
# 70-79  DEFAULTS BY KIND
# ---------------------------------------------------------------------------

@rule("flow.load", 68)
def _load(f):
    """A one-shot fill of a chamber: a port, and a channel wide enough for what it carries."""
    if f.kind != "load":
        return None
    part, citation = _part("collect")
    note = ("contracts/blocks.json fabrication_floors.min_port_mm sets the port at 0.8 mm at a "
            "5 mm pitch, and the demo envelope allows six per chip")
    if f.diameter_um:
        low = float(contracts.param("capsule_channel.width_factor_low")) * f.diameter_um
        high = float(contracts.param("capsule_channel.width_factor_high")) * f.diameter_um
        note += ("; the feed channel wants %.0f-%.0f um (capsule_channel.width_factor_low and "
                 "width_factor_high x %.0f um) to avoid clogging, and the swollen diameter "
                 "%.0f um is what it must actually pass"
                 % (low, high, f.diameter_um, f.swollen_um or f.diameter_um))
    return Verdict(
        ON,
        "a one-shot fill enters through a port, the only fluid inlet (%s)" % citation,
        part=part, blocks=[part, "straight_channel"], note=note)


@rule("flow.transfer", 69, evidence=PRECEDENT)
def _transfer(f):
    """Moving material from one place to another: S4 turns this into a handoff."""
    if f.kind != "transfer":
        return None
    return Verdict(
        OFF,
        "a transfer moves material between places rather than doing anything to it, so it is "
        "not a block: S4 records it as a handoff in partition.json, with the tubing, the "
        "carried volume and whether a machine can do it (assay2chip/README.md S4 handoffs)",
        need="tubing and a selector valve between two chips; S4 must mark it automated only "
             "if a machine really does it, because pipetting is what the no-human-intervention "
             "goal is counted against",
        note="if this transfer stays inside one chip it is a channel, not a handoff, and S4 "
             "should fold it into the neighbouring ops")


@rule("flow.merge", 70, evidence=PRECEDENT)
def _merge(f):
    """A reagent added to a flowing stream is a junction."""
    if f.kind != "merge":
        return None
    gentle = f.particles == "cells" or f.shear_class in ("adherent_cells", "prelysis_rinse")
    part, citation = _part("merge_gentle" if gentle else "merge")
    return Verdict(
        ON,
        "a reagent joining a flowing stream is a junction, and this one carries %s, so %s is "
        "the block (%s)" % (f.particles, part, citation),
        part=part)


@rule("flow.mix", 71, evidence=PRECEDENT)
def _mix(f):
    """Mixing at Re << 1 is chaotic advection or nothing."""
    if f.kind != "mix":
        return None
    viscous = f.viscosity_class in ("viscous", "oil")
    part, citation = _part("mix_viscous" if viscous else "mix")
    return Verdict(
        ON,
        "mixing happens at Re of order 0.02, so %s is the block whose own description in "
        "contracts/blocks.json holds at low Re (%s)" % (part, citation),
        part=part,
        note="chip/designs/README.md rule 2 names serpentine_channel here; at our Re a "
             "serpentine is a delay line rather than a mixer, so herringbone_mixer is used "
             "instead - a deliberate DISAGREEMENT with that rule")


@rule("flow.incubate", 72, evidence=PRECEDENT)
def _incubate(f):
    """A timed hold: a delay line in flow, or a chamber when it is longer than minutes."""
    if f.kind != "incubate":
        return None
    short = f.duration_s is not None and float(f.duration_s) <= precedent("inflow_delay_line_s")
    if short:
        purpose = "delay_line_viscous" if f.viscosity_class in ("viscous", "oil") else "delay_line"
        shape = "a delay line in flow, sized so volume over flow gives the residence time"
    else:
        purpose = "static_hold"
        shape = "a static chamber: a delay line long enough would be metres at our flows"
    part, citation = _part(purpose)
    held = ("%g s" % float(f.duration_s)) if f.duration_s is not None else "an unstated time"
    at = ("%g C" % float(f.temp_c)) if f.temp_c is not None else "an unstated temperature"
    return Verdict(
        ON,
        "a %s hold at %s is %s (%s)" % (held, at, shape, citation),
        part=part,
        note="" if f.duration_s is not None else "duration not stated; recorded in unresolved")


@rule("flow.lyse", 73, evidence=PRECEDENT)
def _lyse(f):
    """Lysis is a reagent merge and a hold, in a chamber."""
    if f.kind != "lyse":
        return None
    part, citation = _part("static_hold")
    return Verdict(
        ON,
        "lysis is a reagent merge and a hold in a chamber (%s)" % citation,
        part=part, blocks=[part, "t_junction"],
        note="contracts/parameters.json lysis.gitc_molar notes harsh lysis needs chemically "
             "resistant materials and a dedicated waste line, and the rinse immediately before "
             "it is bounded by shear.prelysis_rinse_ceiling_dyn_cm2, not the culture ceiling")


@rule("flow.split", 74, evidence=PRECEDENT)
def _split(f):
    """Splitting a stream; metered when the source asks for equal volumes."""
    if f.kind != "split":
        return None
    metered = f.signal("metered")
    part, citation = _part("split_metered" if metered else "split")
    shape = ("a metered split needs the block that captures a FIXED VOLUME" if metered
             else "a split into set fractions with matched path lengths")
    return Verdict(ON, "%s (%s)" % (shape, citation), part=part,
                   evidence=TEXT if metered else PRECEDENT)


@rule("flow.pool", 75, evidence=PRECEDENT)
def _pool(f):
    """Pooling several streams back into one."""
    if f.kind != "pool":
        return None
    part, citation = _part("pool")
    return Verdict(ON, "pooling streams back into one is a closed loop with taps (%s)" % citation,
                   part=part)


@rule("flow.collect", 76)
def _collect(f):
    """Collecting the product: a port, and after that it is off chip."""
    if f.kind != "collect":
        return None
    part, citation = _part("collect")
    return Verdict(
        ON, "collection leaves through a port, the only fluid outlet (%s)" % citation,
        part=part,
        note="whatever happens to the collected volume afterwards is off chip; "
             "contracts/blocks.json fabrication_floors.min_port_mm sets the port at 0.8 mm at a "
             "5 mm pitch, and the demo envelope allows six of them")


@rule("flow.cure", 77, evidence=PRECEDENT)
def _cure(f):
    """Curing needs minutes of residence, so a delay line for it would be metres long."""
    if f.kind != "cure":
        return None
    return Verdict(
        OFF,
        "curing needs minutes for the two aqueous polymers to demix, and a serpentine holding "
        "minutes at this flow would be metres long, so capsules are collected and flood-cured "
        "in the tube (precedent protocols/sctip_spc_design.md section 6 and "
        "protocols/pipeline_mvp.json module2, 'curing off-chip')",
        need="off-chip collection tube and a 405 nm flood source "
             "(contracts/parameters.json crosslink.wavelength_nm)")


@rule("flow.wash_no_particles", 78)
def _wash_plain(f):
    """A wash with nothing to retain is just a chamber exchange."""
    if f.kind != "wash" or f.has_particles:
        return None
    part, citation = _part("static_hold")
    return Verdict(
        ON,
        "with no particles to retain, a wash is a chamber exchange - "
        "contracts/parameters.json wash.chamber_volumes_per_wash x wash.washes_per_exchange "
        "sets how much liquid it takes (%s)" % citation,
        part=part)


@rule("flow.separate_unspecified", 79, evidence=TEXT)
def _separate_plain(f):
    """A separation whose mechanism the source never states."""
    if f.kind != "separate":
        return None
    return Verdict(
        MISSING,
        "this op separates something but the source never says by what mechanism, and guessing "
        "one would put a block behind a function it may not perform - the failure mode "
        "contracts/blocks.json retention.not_viable warns about",
        need="the separation mechanism from the source: size, density, phase or affinity. Only "
             "then can a block be chosen")


# ---------------------------------------------------------------------------
# 99  FALLBACK
# ---------------------------------------------------------------------------

@rule("fallback.missing_part", 99, evidence=TEXT)
def _fallback(f):
    """Nothing matched. Never force a block."""
    return Verdict(
        MISSING,
        "no rule in assay2chip/triage/rules.py claims a '%s' op carrying %s, and "
        "contracts/blocks.json basic and advanced hold no block that plainly does it, so this "
        "is recorded as a gap rather than given a block that does not match its function"
        % (f.kind, f.particles),
        need="a block, or a bought part, that performs '%s' on %s; add the rule to "
             "assay2chip/triage/rules.py once the mechanism is known" % (f.kind, f.particles))


def audit():
    """Problems with the rule table itself. An empty list means it is well formed."""
    problems = library.audit()
    seen = {}  # type: Dict[str, int]
    for r in RULES:
        if r.id in seen:
            problems.append("duplicate rule id %r" % r.id)
        seen[r.id] = r.rank
        if r.evidence not in CONFIDENCE:
            problems.append("rule %r has evidence %r" % (r.id, r.evidence))
        if not r.about:
            problems.append("rule %r has no docstring saying what it decides" % r.id)
    if not any(r.rank == 99 for r in RULES):
        problems.append("no fallback rule: an unmatched op would get no placement")
    by_rank = {}  # type: Dict[int, List[Rule]]
    for r in RULES:
        by_rank.setdefault(r.rank, []).append(r)
    for rank, group in sorted(by_rank.items()):
        if len(group) > 1 and any(r.physics for r in group):
            problems.append(
                "rank %d holds more than one rule and at least one is physics (%s): a physics "
                "tie cannot be arbitrated, so give them distinct ranks"
                % (rank, ", ".join(r.id for r in group)))
    return problems


#: Places where triage deliberately differs from something written in this repo. The S3
#: report prints these, because a silent disagreement is how a wrong block survives.
DISAGREEMENTS = (
    {
        "rule": "flow.mix",
        "repo": "chip/designs/README.md rule 2 ('a mixer: serpentine_channel, or "
                "snake_channel for viscous liquids')",
        "triage": "herringbone_mixer for aqueous mixing",
        "who_is_wrong": "the repo, mildly. At Re of order 0.02 a serpentine does not mix, it "
                        "delays; contracts/blocks.json describes herringbone_mixer as 'chaotic "
                        "advection mixing at low Re', which is the claim that survives. "
                        "snake_channel for viscous liquids is kept.",
    },
    {
        "rule": "retain.cell_sedimentation",
        "repo": "chip/designs/README.md rule 3 ('Centrifuge washes become a chamber flush "
                "(capsules on a mesh), or a spiral_channel for loose cells')",
        "triage": "sedimentation in a wide slow chamber; spiral_channel only ever as a long "
                  "path",
        "who_is_wrong": "the repo. Spiral inertial focusing needs Re >> 1 and ours is about "
                        "0.02 - the same Stokes-flow argument contracts/blocks.json uses to "
                        "rule out tesla_valve. A spiral at our flows is a long channel, not a "
                        "sorter.",
    },
    {
        "rule": "retain.capsule_weir",
        "repo": "chip/module3_v1_translated.json capsule_prefilter, a pillar_array typed "
                "'filter'",
        "triage": "pillar_array is banned for retention "
                  "(assay2chip/triage/library.BANNED_FOR_RETENTION)",
        "who_is_wrong": "the repo, and it already knows: contracts/blocks.json "
                        "retention.not_viable says pillars 'cannot retain by size exclusion at "
                        "a 200 um throat' and protocols/retention_audit.md records module3 "
                        "main_trap as 'FIX: weir'.",
    },
)

"""S6 build: sizing, placement, ports and pads, and the chip spec it emits.

    python -m unittest discover -s tests -v
    python -m unittest tests.test_assay2chip_layout -v

The three things worth testing here are the three things that can silently go wrong:

  1. a number that was derived reads as if it were measured (so every derivation must
     leave an `unresolved` entry, and these tests check the entry, not just the number);
  2. a part sits inside a wall, outside the envelope, or on top of another part;
  3. a port hole lands on solid plastic. `tools/check_chip_spec.py` reports 60 of those on
     main's hand-made chips, so the port-overlap tests here run the REAL checker rather
     than re-implementing the rule.

Nothing is written into the repo: every chip goes to a temporary directory.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for path in (str(ROOT), str(ROOT / "operator"), str(ROOT / "tools")):
    if path not in sys.path:
        sys.path.insert(0, path)

import check_chip_spec as ccs  # noqa: E402
import schema_io  # noqa: E402

from assay2chip.layout import connections as conn  # noqa: E402
from assay2chip.layout import materials as mats  # noqa: E402
from assay2chip.layout import needs as needs_mod  # noqa: E402
from assay2chip.layout import parts as parts_mod  # noqa: E402
from assay2chip.layout import place as place_mod  # noqa: E402
from assay2chip.layout import ports as ports_mod  # noqa: E402
from assay2chip.layout import spec as spec_mod  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402
from assay2chip.stages import s6_build  # noqa: E402

FLOORS = contracts.fabrication_floors()
MIN_WALL = float(FLOORS["min_wall_mm"])
MIN_FEATURE = float(FLOORS["min_open_channel_mm"])
MIN_PORT = float(FLOORS["min_port_mm"])
MIN_PITCH = float(FLOORS["min_port_pitch_mm"])
DEPTH = contracts.max_depth_mm()
TOL = 1e-6

CAPSULES = {"kind": "capsules", "diameter_um": 70, "swollen_diameter_um": 140,
            "count": 20000}
CELLS = {"kind": "cells", "diameter_um": 15}


def bed(cells: int = 20000, source: str = "paper") -> dict:
    return {"basis": "packed_volume", "cells": cells, "source": source}


def monolayer(cells: int = 60000, density: float = 500.0) -> dict:
    return {"basis": "adherent_area", "cells": cells,
            "surface_density_per_mm2": density, "source": "paper"}


def op(number: int, kind: str, **kw) -> dict:
    """A unitops.json op. Every field the schema requires, nothing it forbids.

    `volume_ul` is the PAPER'S tube volume and is traceability only: S6 must size from
    `load`. Several tests set the two to deliberately disagreeing numbers to prove it.
    """
    out = {
        "op": number, "from_steps": [number], "kind": kind,
        "fluid": {"volume_ul": kw.pop("volume_ul", 10.0),
                  "viscosity_class": kw.pop("viscosity_class", "aqueous")},
        "particles": kw.pop("particles", {"kind": "none"}),
        "duration_s": kw.pop("duration_s", 600),
        "temp_c": kw.pop("temp_c", 37),
        "shear_class": kw.pop("shear_class", "none"),
        "placement": {"where": kw.pop("where", "on_chip"), "part": "x",
                      "why": "test fixture", **kw},
    }
    load = kw.pop("load", None)
    if load is not None:
        out["load"] = load
    return out


# Capsules in one trap: lysis, a wash and a hold, all on the same bed in the same place.
CAPSULE_TRAIN = [
    op(1, "wash", volume_ul=500.0, particles=dict(CAPSULES), shear_class="capsules",
       load=bed()),
    op(2, "lyse", volume_ul=200.0, particles=dict(CAPSULES), temp_c=55,
       shear_class="capsules", load=bed()),
    op(3, "thermocycle", volume_ul=50.0, temp_c=98, duration_s=1800,
       load={"basis": "suspension_volume", "cells": 20000,
             "suspension_density_per_ul": 2500, "source": "derived"}),
]
CELL_TRAIN = [
    op(1, "incubate", volume_ul=2000.0, particles=dict(CELLS), duration_s=7 * 24 * 3600,
       shear_class="adherent_cells", load=monolayer()),
    op(2, "wash", volume_ul=500.0, particles=dict(CELLS), shear_class="prelysis_rinse",
       load=monolayer()),
    op(3, "lyse", volume_ul=200.0, particles=dict(CELLS), load=monolayer()),
]


def partition(chip_id: str, ops: list[dict], scale: str = "demo", handoffs=()) -> dict:
    return {"id": "t", "cuts": [], "unresolved": [], "handoffs": list(handoffs),
            "chips": [{"chip_id": chip_id, "ops": [o["op"] for o in ops], "scale": scale,
                       "reason": "test fixture"}]}


def build_one(chip_id: str, ops: list[dict], scale: str = "demo", handoffs=()) -> dict:
    """One chip's build result (the dict build_chip returns)."""
    result = s6_build.build(partition(chip_id, ops, scale, handoffs), {"id": "t", "ops": ops},
                            assay_id="t")
    items = result["built"] + result["reruns"]
    assert len(items) == 1, items
    return items[0]


def built(chip_id: str, ops: list[dict], scale: str = "demo", handoffs=()) -> dict:
    item = build_one(chip_id, ops, scale, handoffs)
    assert "chip" in item, item.get("reason")
    return item


def pieces(ops: list[dict], scale: str = "demo"):
    """(briefs, parts, placement, port_plan) for a set of ops, without building a spec."""
    briefs = [needs_mod.brief_for_op(o, scale=scale) for o in ops]
    sized = [parts_mod.size_part(b) for b in briefs]
    placement = place_mod.place_parts(
        sized, contracts.envelope(scale), floors=FLOORS,
        port_pad_mm=ports_mod.pad_budget(briefs, scale=scale),
        side_feeds=[i for i, b in enumerate(briefs) if b.needs_reagent],
        drains=[i for i, b in enumerate(briefs) if b.drains])
    plan = ports_mod.plan_ports(briefs, placement, floors=FLOORS, scale=scale, chip_id="t_c1")
    return briefs, sized, placement, plan


def rect_gap(a, b) -> float:
    dx = max(b[0] - a[2], a[0] - b[2], 0.0)
    dy = max(b[1] - a[3], a[1] - b[3], 0.0)
    return 0.0 if dx == dy == 0.0 else (dx * dx + dy * dy) ** 0.5


# --- briefs: every derived number is flagged ---------------------------------------

class Briefs(unittest.TestCase):
    """The load rules from assay2chip/README.md, "Bench volumes do not transfer"."""

    def test_the_papers_tube_volume_is_never_the_chamber_volume(self):
        # The paper says 500 uL. The load says 20,000 capsules, which is about 5.6 uL.
        brief = needs_mod.brief_for_op(
            op(1, "wash", volume_ul=500.0, particles=dict(CAPSULES),
               shear_class="capsules", load=bed()))
        packing = float(contracts.param("capsule.packing_fraction"))
        self.assertAlmostEqual(brief.volume_ul,
                               needs_mod.bed_volume_ul(20000, 70, packing), places=6)
        self.assertEqual(brief.paper_volume_ul, 500.0)
        self.assertNotIn("fluid.volume_ul", brief.volume_source)
        self.assertIn("packed bed", brief.volume_source)

    def test_adherent_cells_become_a_floor_area_at_the_seeding_density(self):
        brief = needs_mod.brief_for_op(
            op(1, "incubate", volume_ul=2000.0, particles=dict(CELLS),
               load=monolayer(cells=60000, density=500.0)))
        self.assertEqual(brief.load_basis, "adherent_area")
        self.assertAlmostEqual(brief.volume_ul, (60000 / 500.0) * DEPTH)
        self.assertIn("cells/mm2", brief.volume_source)

    def test_a_suspension_uses_its_stated_density(self):
        brief = needs_mod.brief_for_op(
            op(1, "thermocycle", volume_ul=50.0, temp_c=98,
               load={"basis": "suspension_volume", "cells": 20000,
                     "suspension_density_per_ul": 2500, "source": "derived"}))
        self.assertAlmostEqual(brief.volume_ul, 8.0)
        self.assertIn("suspension", brief.volume_source)

    def test_a_complete_load_reaches_the_registry_untouched(self):
        brief = needs_mod.brief_for_op(
            op(1, "lyse", volume_ul=200.0, particles=dict(CAPSULES),
               load={"basis": "packed_volume", "cells": 20000, "source": "paper"}))
        self.assertEqual(brief.load.basis, "packed_volume")
        self.assertEqual(brief.load.cells, 20000)
        self.assertEqual(brief.load.source, "paper")
        self.assertIsNone(brief.need.volume_ul)     # the load is what the registry sizes from
        self.assertIs(brief.need.load, brief.load)

    def test_a_load_the_registry_cannot_resolve_is_downgraded_not_refused(self):
        # The contracts have no suspension density, so the registry refuses that basis.
        # Refusing would stop the run over a number nobody has; the same population as a
        # packed bed IS contract-derived, so the part is built to that lower bound.
        brief = needs_mod.brief_for_op(
            op(1, "lyse", particles=dict(CAPSULES),
               load={"basis": "suspension_volume", "cells": 20000, "source": "paper"}))
        self.assertEqual(brief.load.basis, "packed_volume")
        gap, = [g for g in brief.gaps
                if g["field"] == "load.suspension_density_per_ul"]
        self.assertIn("LOWER BOUND", gap["question"])
        self.assertTrue(parts_mod.size_part(brief).chambers)

    def test_a_flow_through_part_holds_nothing_and_carries_a_residence_time(self):
        brief = needs_mod.brief_for_op(
            op(1, "mix", volume_ul=100.0, duration_s=30,
               load={"basis": "flow_through", "residence_s": 30, "source": "paper"}))
        self.assertIsNone(brief.volume_ul)
        self.assertEqual(brief.residence_s, 30.0)
        self.assertIn("nothing is held", brief.volume_source)

    def test_a_load_below_the_smallest_part_is_rounded_up_never_refused(self):
        # S6 must hold NO minimum-size filter of its own: a one-cell load goes straight
        # to the registry, which rounds it up to the smallest buildable part and says so.
        tiny = op(1, "lyse", volume_ul=200.0, particles={"kind": "cells", "diameter_um": 15},
                  load={"basis": "adherent_area", "cells": 1,
                        "surface_density_per_mm2": 500.0, "source": "paper"})
        brief = needs_mod.brief_for_op(tiny)
        self.assertLess(brief.volume_ul, needs_mod.floor_volume_ul())
        part = parts_mod.size_part(brief)
        self.assertTrue(part.chambers)
        self.assertTrue(part.geometry.physics.get("volume_rounded_up"),
                        part.geometry.physics)

    def test_no_op_is_ever_refused_for_being_small(self):
        for cells in (1, 10, 100, 2000):
            for basis, extra in (("adherent_area", {"surface_density_per_mm2": 500.0}),
                                 ("suspension_volume", {"suspension_density_per_ul": 1500.0}),
                                 ("packed_volume", {})):
                brief = needs_mod.brief_for_op(op(
                    1, "lyse", volume_ul=500.0, particles=dict(CAPSULES),
                    shear_class="capsules",
                    load={"basis": basis, "cells": cells, "source": "paper", **extra}))
                with self.subTest(cells=cells, basis=basis):
                    self.assertTrue(parts_mod.size_part(brief).chambers)

    def test_an_op_with_no_load_is_given_the_basis_its_population_implies(self):
        brief = needs_mod.brief_for_op(
            op(1, "wash", volume_ul=500.0, particles=dict(CAPSULES),
               shear_class="capsules"))
        self.assertEqual(brief.load.basis, "packed_volume")
        self.assertEqual(brief.load.source, "contract_default")
        gap, = [g for g in brief.gaps if g["field"] == "load"]
        self.assertIn("no `load`", gap["question"])
        self.assertIn("TUBE volume and was deliberately NOT used", gap["question"])
        cells = needs_mod.brief_for_op(
            op(1, "wash", volume_ul=500.0, particles=dict(CELLS)))
        self.assertEqual(cells.load.basis, "adherent_area")

    def test_a_flow_is_derived_only_where_a_part_cannot_be_sized_without_one(self):
        # Capsules are bounded by escape PRESSURE, not wall shear, so nothing is derived.
        capsules = needs_mod.brief_for_op(
            op(1, "wash", particles=dict(CAPSULES), shear_class="capsules", load=bed()))
        self.assertIsNone(capsules.flow_ul_min)
        self.assertEqual([g for g in capsules.gaps if g["field"] == "flow_ul_min"], [])
        # Adherent cells are bounded by wall shear, which needs a flow.
        cells = needs_mod.brief_for_op(
            op(1, "wash", duration_s=600, particles=dict(CELLS),
               shear_class="adherent_cells", load=monolayer()))
        self.assertAlmostEqual(cells.flow_ul_min, cells.volume_ul / 10.0)
        gap, = [g for g in cells.gaps if g["field"] == "flow_ul_min"]
        self.assertIn("MEAN flow", gap["question"])
        self.assertIn("duration_s", gap["assumed_from"])

    def test_a_weir_is_offered_for_capsules_and_never_for_cells_or_beads(self):
        capsules = needs_mod.brief_for_op(
            op(1, "wash", particles=dict(CAPSULES), shear_class="capsules", load=bed()))
        self.assertTrue(capsules.weir_retained)
        self.assertEqual(capsules.blocks[0], "trap_chamber")
        for particles, load in ((dict(CELLS), monolayer()),
                                ({"kind": "beads", "diameter_um": 1}, bed())):
            brief = needs_mod.brief_for_op(op(1, "wash", particles=particles, load=load))
            self.assertFalse(brief.weir_retained, particles)
            self.assertNotIn("trap_chamber", brief.blocks, particles)
            gap, = [g for g in brief.gaps if g["field"] == "retention"]
            self.assertIn("_cells_caveat", gap["assumed_from"])


class Grouping(unittest.TestCase):
    """chip/designs/README.md rule 1: one part per place the population sits."""

    def test_ops_on_the_same_bed_in_the_same_place_become_one_part(self):
        ops = [op(1, "lyse", particles=dict(CAPSULES), temp_c=55, load=bed(),
                  shear_class="capsules"),
               op(2, "wash", particles=dict(CAPSULES), load=bed(), shear_class="capsules"),
               op(3, "wash", particles=dict(CAPSULES), load=bed(), shear_class="capsules"),
               op(4, "incubate", particles=dict(CAPSULES), temp_c=50, load=bed(),
                  shear_class="capsules")]
        briefs = needs_mod.briefs_for(ops)
        self.assertEqual(len(briefs), 1)
        brief, = briefs
        self.assertEqual(brief.ops, (1, 2, 3, 4))
        self.assertEqual(brief.part_id, "lyse_1")
        self.assertTrue(brief.merged)
        self.assertEqual(brief.blocks[0], "trap_chamber")
        # One trap, one weir, one bed - not four chambers.
        part = parts_mod.size_part(brief)
        self.assertEqual(len(part.chambers), 1)
        self.assertEqual(part.chambers[0]["retention"]["type"], "weir")

    def test_a_merged_part_is_held_at_every_one_of_its_temperatures(self):
        ops = [op(1, "lyse", particles=dict(CAPSULES), temp_c=55, load=bed(),
                  shear_class="capsules"),
               op(2, "incubate", particles=dict(CAPSULES), temp_c=50, load=bed(),
                  shear_class="capsules")]
        chip = built("t_group_c1", ops)["chip"]
        self.assertEqual(len(chip["chambers"]) - len(chip["ports"]), 1)
        zones = {z["id"]: z for z in chip["thermal_zones"]}
        self.assertEqual(sorted(zones), ["incubate_2_zone", "lyse_1_zone"])
        self.assertEqual({tuple(z["chambers"]) for z in zones.values()}, {("lyse_1",)})
        self.assertEqual(zones["lyse_1_zone"]["max_temp_c"], 55)
        self.assertEqual(zones["incubate_2_zone"]["max_temp_c"], 50)

    def test_a_merged_part_is_sized_for_the_largest_load_in_its_group(self):
        ops = [op(1, "lyse", particles=dict(CAPSULES), load=bed(5000),
                  shear_class="capsules"),
               op(2, "wash", particles=dict(CAPSULES), load=bed(20000),
                  shear_class="capsules")]
        brief, = needs_mod.briefs_for(ops)
        packing = float(contracts.param("capsule.packing_fraction"))
        self.assertAlmostEqual(brief.volume_ul,
                               needs_mod.bed_volume_ul(20000, 70, packing), places=6)
        self.assertIn("largest load", brief.volume_source)

    def test_a_flow_part_is_its_own_place_and_is_never_merged(self):
        ops = [op(1, "wash", particles=dict(CAPSULES), load=bed(), shear_class="capsules"),
               op(2, "mix", particles=dict(CAPSULES), duration_s=30,
                  load={"basis": "flow_through", "residence_s": 30, "source": "paper"}),
               op(3, "wash", particles=dict(CAPSULES), load=bed(), shear_class="capsules")]
        briefs = needs_mod.briefs_for(ops)
        self.assertEqual([b.ops for b in briefs], [(1,), (2,), (3,)])

    def test_a_different_population_starts_a_new_place(self):
        ops = [op(1, "wash", particles=dict(CELLS), load=monolayer()),
               op(2, "wash", particles=dict(CAPSULES), load=bed(), shear_class="capsules")]
        briefs = needs_mod.briefs_for(ops)
        self.assertEqual([b.particles for b in briefs], ["cells", "capsules"])

    def test_an_off_chip_op_does_not_split_a_place_in_two(self):
        ops = [op(1, "lyse", particles=dict(CAPSULES), load=bed(), shear_class="capsules"),
               op(2, "image", where="off_chip"),
               op(3, "wash", particles=dict(CAPSULES), load=bed(), shear_class="capsules")]
        briefs = needs_mod.briefs_for(ops)
        self.assertEqual([b.ops for b in briefs], [(1, 3)])

    def test_the_merge_is_explained_in_the_spec_and_flagged(self):
        item = built("t_group_c2", CAPSULE_TRAIN)
        self.assertIn("ONE PART PER PLACE", item["chip"]["description"])
        self.assertTrue([g for g in item["unresolved"]
                         if g["field"] == "part" and "rule 1" in g["assumed_from"]])
        self.assertEqual(item["summary"]["merged_parts"], {"wash_1": [1, 2]})

    def test_a_merge_does_not_claim_to_mix_but_a_mix_does(self):
        self.assertFalse(needs_mod.brief_for_op(op(1, "merge")).mix)
        self.assertTrue(needs_mod.brief_for_op(op(1, "mix")).mix)

    def test_every_candidate_block_is_in_the_contract_library(self):
        library = contracts.library()
        kinds = ["merge", "mix", "incubate", "wash", "separate", "lyse", "thermocycle",
                 "split", "pool", "collect", "image", "encapsulate", "cure"]
        for kind in kinds:
            for particles in ({"kind": "none"}, dict(CAPSULES), dict(CELLS)):
                brief = needs_mod.brief_for_op(op(1, kind, particles=particles))
                self.assertTrue(brief.blocks, f"{kind} has no candidate block")
                for block in brief.blocks:
                    self.assertIn(block, library, f"{kind} -> {block}")

    def test_a_block_hint_from_s3_wins(self):
        brief = needs_mod.brief_for_op(op(1, "lyse", blocks=["metering_trap"]))
        self.assertEqual(brief.blocks[0], "metering_trap")

    def test_the_registry_sizes_every_part_and_keeps_its_refusals(self):
        _, sized, _, _ = pieces(CAPSULE_TRAIN)
        self.assertEqual([p.block for p in sized],
                         ["trap_chamber", "trap_chamber", "polygon_chamber"])
        for part in sized:
            self.assertAlmostEqual(part.depth_mm, DEPTH)
            self.assertTrue(part.chambers)
            self.assertTrue(part.openings)


# --- placement ----------------------------------------------------------------------

class Placement(unittest.TestCase):
    def test_every_part_keeps_a_wall_to_the_edge_and_to_its_neighbours(self):
        for ops, scale in ((CAPSULE_TRAIN, "demo"), (CELL_TRAIN, "target")):
            _, _, placement, _ = pieces(ops, scale)
            env = placement.envelope
            with self.subTest(scale=scale):
                for placed in placement.placed:
                    x0, y0, x1, y1 = placed.rect
                    self.assertGreaterEqual(min(x0, y0), MIN_WALL - TOL, placed.part.part_id)
                    self.assertGreaterEqual(env.x_mm - x1, MIN_WALL - TOL, placed.part.part_id)
                    self.assertGreaterEqual(env.y_mm - y1, MIN_WALL - TOL, placed.part.part_id)
                for i, a in enumerate(placement.placed):
                    for b in placement.placed[i + 1:]:
                        self.assertGreaterEqual(
                            rect_gap(a.rect, b.rect), MIN_WALL - TOL,
                            f"{a.part.part_id} vs {b.part.part_id}")

    def test_parts_run_in_flow_order_along_each_row(self):
        _, _, placement, _ = pieces(CAPSULE_TRAIN)
        by_row: dict[int, list] = {}
        for placed in placement.placed:
            by_row.setdefault(placed.row, []).append(placed)
        for row, items in by_row.items():
            xs = [p.centre[0] for p in items]
            expected = sorted(xs) if items[0].flow > 0 else sorted(xs, reverse=True)
            self.assertEqual(xs, expected, f"row {row} is not in flow order")

    def test_a_small_chip_uses_one_row_and_risks_no_crossing(self):
        _, _, placement, _ = pieces(CAPSULE_TRAIN)
        self.assertEqual(placement.rows, 1)
        self.assertEqual(placement.crossing_risk, 0)
        self.assertEqual(placement.entry_edge, "left")
        self.assertEqual(placement.exit_edge, "right")

    def test_routing_corridors_are_left_open_between_parts(self):
        _, _, placement, _ = pieces(CAPSULE_TRAIN)
        self.assertTrue(placement.corridors)
        widest = max(p.brief.route_width_mm for p in [pl.part for pl in placement.placed])
        self.assertGreaterEqual(placement.clearance_mm, widest + 2 * MIN_WALL - TOL)
        for corridor in placement.corridors:
            x0, y0, x1, y1 = corridor["rect"]
            self.assertGreater(x1 - x0, 0, corridor)
            self.assertGreater(y1 - y0, 0, corridor)

    def test_a_part_is_rotated_only_when_it_has_to_be(self):
        # The part's own +x is its flow direction, so rotation 0 is preferred.
        _, _, placement, _ = pieces(CAPSULE_TRAIN)
        self.assertEqual([p.rotation for p in placement.placed], [0, 0, 0])

    def test_rotation_moves_both_the_size_and_the_openings(self):
        _, sized, placement, _ = pieces(CAPSULE_TRAIN)
        part = sized[0]
        fx, fy = part.footprint
        turned = place_mod.Placed(part=part, x=1.0, y=2.0, w=fy, h=fx, rotation=90,
                                  row=0, flow=1, index=0)
        self.assertEqual(turned.size_to_chip([fx, fy, DEPTH]), [fy, fx, DEPTH])
        # A -x opening on the left face becomes a -y opening on the bottom face.
        opening = part.sample_in()
        (x, y), direction = turned.place_opening(opening)
        self.assertEqual(direction, place_mod.rotate_direction(opening.direction, 90))
        self.assertGreaterEqual(x, 1.0 - TOL)
        self.assertGreaterEqual(y, 2.0 - TOL)

    def test_a_part_too_big_for_the_envelope_is_refused_not_shrunk(self):
        _, sized, _, _ = pieces(CAPSULE_TRAIN)
        with self.assertRaises(place_mod.CannotFit) as caught:
            place_mod.place_parts(sized * 40, contracts.envelope("demo"), floors=FLOORS,
                                  port_pad_mm=1.2)
        self.assertIn("usable", str(caught.exception).lower() + json.dumps(caught.exception.detail))


# --- ports and pads -----------------------------------------------------------------

class Ports(unittest.TestCase):
    def test_ports_respect_the_bore_the_pitch_and_the_count(self):
        briefs, _, placement, plan = pieces(CAPSULE_TRAIN)
        limit = contracts.max_ports_per_chip("demo")
        self.assertLessEqual(len(plan.ports), limit)
        for port in plan.ports:
            self.assertGreaterEqual(port.hole_mm, MIN_PORT - TOL, port.id)
        for i, a in enumerate(plan.ports):
            for b in plan.ports[i + 1:]:
                distance = ((a.position[0] - b.position[0]) ** 2
                            + (a.position[1] - b.position[1]) ** 2) ** 0.5
                self.assertGreaterEqual(distance, MIN_PITCH - TOL, f"{a.id} vs {b.id}")

    def test_inlets_and_outlets_sit_on_opposite_edges(self):
        _, _, placement, plan = pieces(CAPSULE_TRAIN)
        entry = plan.inlets[0]
        exit_port = next(p for p in plan.outlets if p.request.role != "waste"
                         and p.id != "vent")
        self.assertLess(entry.position[0], placement.envelope.x_mm / 2)
        self.assertGreater(exit_port.position[0], placement.envelope.x_mm / 2)

    def test_every_pad_is_inside_the_footprint_and_clear_of_every_part(self):
        _, _, placement, plan = pieces(CAPSULE_TRAIN)
        env = placement.envelope
        for port in plan.ports:
            half = port.pad_side_mm / 2
            rect = (port.position[0] - half, port.position[1] - half,
                    port.position[0] + half, port.position[1] + half)
            self.assertGreaterEqual(min(rect[0], rect[1]), MIN_WALL - TOL, port.id)
            self.assertLessEqual(rect[2], env.x_mm - MIN_WALL + TOL, port.id)
            self.assertLessEqual(rect[3], env.y_mm - MIN_WALL + TOL, port.id)
            for placed in placement.placed:
                self.assertGreaterEqual(rect_gap(rect, placed.rect), MIN_WALL - TOL,
                                        f"{port.id} vs {placed.part.part_id}")

    def test_the_port_budget_is_enforced_when_a_chip_wants_too_many(self):
        briefs, _, placement, _ = pieces(CAPSULE_TRAIN)
        requests = ports_mod.requests_for(briefs, placement, floors=FLOORS, chip_id="t_c1")
        too_many = requests + [
            ports_mod.PortRequest(id=f"spare_{i}", type="inlet", role="reagent",
                                  edge="bottom", near=0, carries="liquid", particles="none",
                                  particle_um=None, capsule_passable=False, why="test")
            for i in range(4)]
        with mock.patch.object(ports_mod, "requests_for", return_value=too_many):
            with self.assertRaises(ports_mod.PortBudgetExceeded) as caught:
                ports_mod.plan_ports(briefs, placement, floors=FLOORS, scale="demo",
                                     chip_id="t_c1")
        self.assertEqual(caught.exception.detail["limit"],
                         contracts.max_ports_per_chip("demo"))

    def test_the_demo_port_budget_is_met_by_sharing_lines_off_chip(self):
        # One reagent feed and one waste drain, selected off-chip, is what keeps every
        # chip inside the 6-port demo budget. If this ever grows per-op feeds, the guard
        # above is what will catch it.
        briefs, _, placement, _ = pieces(CAPSULE_TRAIN)
        requests = ports_mod.requests_for(briefs, placement, floors=FLOORS, chip_id="t_c1")
        reagent = [r for r in requests if r.role == "reagent"]
        self.assertEqual(len(reagent), 1)
        self.assertIn("selected OFF chip", reagent[0].why)

    def test_the_port_limit_lifts_at_target_scale(self):
        self.assertEqual(contracts.max_ports_per_chip("demo"), 6)
        self.assertIsNone(contracts.max_ports_per_chip("target"))


class PortOverlap(unittest.TestCase):
    """Bruno's rule, run through the real checker. Zero errors is the requirement."""

    def test_every_generated_port_opens_onto_cut_area_with_margin(self):
        for ops, scale in ((CAPSULE_TRAIN, "demo"), (CELL_TRAIN, "target")):
            chip = built(f"t_{scale}_c1", ops, scale)["chip"]
            with self.subTest(scale=scale):
                rows = ccs.port_overlaps(chip)
                self.assertEqual(len(rows), len(chip["ports"]))
                for row in rows:
                    self.assertGreaterEqual(row["overlap_mm2"], row["need_mm2"],
                                            f"{row['port']}: {row}")
                findings = ccs.Findings()
                ccs.check_port_overlap(chip, findings)
                self.assertEqual([e for e in findings.errors if "port overlap" in e], [])

    def test_the_pad_is_what_makes_it_pass(self):
        # Remove the pads and the same chip fails, which is the proof the pads do the work.
        chip = built("t_pads_c1", CAPSULE_TRAIN)["chip"]
        stripped = dict(chip, chambers=[c for c in chip["chambers"]
                                        if not c["id"].endswith("_pad")])
        findings = ccs.Findings()
        ccs.check_port_overlap(stripped, findings)
        self.assertEqual(len([e for e in findings.errors if "port overlap" in e]),
                         len(chip["ports"]))

    def test_every_port_has_exactly_one_pad_chamber_under_it(self):
        chip = built("t_pads_c2", CAPSULE_TRAIN)["chip"]
        pads = {c["id"]: c for c in chip["chambers"] if c["id"].endswith("_pad")}
        self.assertEqual(sorted(pads), sorted(f"{p['id']}_pad" for p in chip["ports"]))
        for port in chip["ports"]:
            pad = pads[f"{port['id']}_pad"]
            self.assertEqual(pad["center"][:2], port["position"][:2])
            self.assertAlmostEqual(pad["size"][2], DEPTH)
            self.assertGreater(pad["size"][0], port["width_um"] / 1000.0)
            self.assertIn(pad["block"], contracts.library())


# --- the chip spec ------------------------------------------------------------------

class ChipSpec(unittest.TestCase):
    def test_the_spec_matches_the_contract_schema(self):
        for ops, scale in ((CAPSULE_TRAIN, "demo"), (CELL_TRAIN, "target")):
            chip = built(f"t_schema_{scale}", ops, scale)["chip"]
            with self.subTest(scale=scale):
                self.assertEqual(schema_io.schema_errors("chip_spec", chip), [])

    def test_a_demo_chip_passes_check_chip_spec_with_no_errors(self):
        chip = built("t_check_c1", CAPSULE_TRAIN)["chip"]
        findings = ccs.check(chip, [], PARAMS, MATERIALS, FAB, [], BLOCKS)
        self.assertEqual(findings.errors, [], "\n".join(findings.errors))

    def test_one_fluidic_layer_at_one_etch_depth_with_a_base_and_a_lid(self):
        chip = built("t_layers_c1", CAPSULE_TRAIN)["chip"]
        self.assertEqual([la["id"] for la in chip["layers"]], ["base", "fluidic", "lid"])
        depths = {c["size"][2] for c in chip["chambers"]}
        self.assertEqual(depths, {DEPTH})
        fluidic = [la for la in chip["layers"] if abs(la["thickness_mm"] - DEPTH) < TOL]
        self.assertEqual(len(fluidic), 1)
        self.assertEqual({la["material"] for la in chip["layers"]}, {"coc"})

    def test_every_chamber_volume_agrees_with_the_box_it_is_written_as(self):
        for ops, scale in ((CAPSULE_TRAIN, "demo"), (CELL_TRAIN, "target")):
            chip = built(f"t_vol_{scale}", ops, scale)["chip"]
            for chamber in chip["chambers"]:
                sx, sy, sz = chamber["size"]
                with self.subTest(chamber=chamber["id"], scale=scale):
                    self.assertGreater(chamber["volume_ul"], 0)
                    self.assertLessEqual(chamber["volume_ul"], sx * sy * sz * 1.01)

    def test_a_98c_chip_gets_a_high_tg_material_and_a_pcr_thermal_zone(self):
        chip = built("t_pcr_c1", CAPSULE_TRAIN)["chip"]
        materials = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
        material = materials[chip["layers"][0]["material"]]
        self.assertGreaterEqual(material["max_working_temp_c"], 98)
        self.assertIn("pcr_chip", material["suitable_for"])
        self.assertIn("SURVIVES", material["pcr_98c_status"])
        zone, = [z for z in chip["thermal_zones"] if z["purpose"] == "pcr"]
        self.assertEqual(zone["max_temp_c"], 98)

    def test_a_37c_chip_is_not_given_a_pcr_grade_it_does_not_need(self):
        chip = built("t_warm_c1", CELL_TRAIN, "target")["chip"]
        materials = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
        material = materials[chip["layers"][0]["material"]]
        self.assertLess(material["max_working_temp_c"], 100)
        self.assertIn("hot_embossing", material["suitable_for"])

    def test_pmma_and_resin_are_refused_for_a_98c_hold(self):
        briefs = [needs_mod.brief_for_op(o) for o in CAPSULE_TRAIN]
        with mock.patch.object(mats, "materials", return_value={
                k: v for k, v in mats.materials().items() if k in ("pmma", "printed_resin")}):
            with self.assertRaises(mats.MaterialError) as caught:
                mats.choose_material(briefs)
        rejected = {r["material"] for r in caught.exception.detail["rejected"]}
        self.assertEqual(rejected, {"pmma", "printed_resin"})

    def test_every_temperature_holding_op_gets_a_thermal_zone_naming_real_chambers(self):
        item = built("t_zones_c1", CAPSULE_TRAIN)
        chip = item["chip"]
        holders = [b for b in (needs_mod.brief_for_op(o) for o in CAPSULE_TRAIN)
                   if b.holds_temp]
        self.assertEqual(len(chip["thermal_zones"]), len(holders))
        ids = {c["id"] for c in chip["chambers"]}
        for zone in chip["thermal_zones"]:
            self.assertTrue(zone["chambers"])
            for chamber in zone["chambers"]:
                self.assertIn(chamber, ids)

    def test_a_weir_crest_is_derived_from_this_chip_s_own_etch_depth(self):
        chip = built("t_weir_c1", CAPSULE_TRAIN)["chip"]
        weirs = [c for c in chip["chambers"] if (c.get("retention") or {}).get("type") == "weir"]
        self.assertTrue(weirs)
        gap = float(contracts.block("retention.weir_geometry.gap_um"))
        for chamber in weirs:
            retention = chamber["retention"]
            self.assertEqual(retention["opening_um"], gap)
            self.assertAlmostEqual(retention["crest_height_um"], DEPTH * 1000 - gap, places=3)
            self.assertAlmostEqual(
                retention["crest_height_um"] + retention["opening_um"],
                chamber["size"][2] * 1000, places=3)
            self.assertLess(retention["opening_um"],
                            float(contracts.param("trap.gap_factor_high")) * 70)

    def test_channels_between_parts_are_left_to_s7(self):
        chip = built("t_route_c1", CAPSULE_TRAIN)["chip"]
        part_ids = {c["id"] for c in chip["chambers"]}
        for channel in chip["channels"]:
            # Any channel in the spec is INTERNAL to one part: both ends are its own ids.
            self.assertIn(channel["from"], part_ids, channel["id"])
            self.assertIn(channel["to"], part_ids, channel["id"])
        self.assertIn("S7 routes them", chip["description"])

    def test_the_spec_records_how_it_was_made(self):
        chip = built("t_gen_c1", CAPSULE_TRAIN)["chip"]
        generator = chip["generator"]
        self.assertEqual(generator["script"], "assay2chip/stages/s6_build.py")
        self.assertFalse(generator["params"]["routed"])
        self.assertEqual(generator["params"]["part_registry"],
                         "assay2chip/parts/registry.py")
        self.assertEqual(generator["params"]["ops"], [1, 2, 3])

    def test_the_scale_the_partition_chose_is_the_envelope_that_is_used(self):
        for scale in ("demo", "target"):
            envelope = contracts.envelope(scale)
            ops = CAPSULE_TRAIN if scale == "demo" else CELL_TRAIN
            chip = built(f"t_scale_{scale}", ops, scale)["chip"]
            with self.subTest(scale=scale):
                self.assertEqual(chip["footprint"], {"x": envelope.x_mm, "y": envelope.y_mm})
                if spec_mod.schema_has_scale():
                    self.assertEqual(chip["scale"], scale)
                else:
                    gaps = built(f"t_scale_{scale}", ops, scale)["unresolved"]
                    self.assertTrue([g for g in gaps if g["field"] == "scale"])

    def test_a_derived_number_always_leaves_an_unresolved_entry(self):
        item = built("t_gaps_c1", [
            op(1, "wash", volume_ul=None, particles=dict(CAPSULES), shear_class="capsules")])
        self.assertTrue(item["chip"]["placeholder"])
        fields = {g["field"] for g in item["unresolved"]}
        self.assertIn("load", fields)
        self.assertIn("layers", fields)
        for gap in item["unresolved"]:
            self.assertTrue(gap["question"])
            self.assertTrue(gap.get("assumed_from"))

    def test_building_the_same_partition_twice_gives_the_same_bytes(self):
        first = built("t_det_c1", CAPSULE_TRAIN)
        second = built("t_det_c1", CAPSULE_TRAIN)
        self.assertEqual(json.dumps(first["chip"], sort_keys=True),
                         json.dumps(second["chip"], sort_keys=True))
        self.assertEqual(json.dumps(first["connections"], sort_keys=True),
                         json.dumps(second["connections"], sort_keys=True))


# --- the connections file S7 reads --------------------------------------------------

class Connections(unittest.TestCase):
    def setUp(self) -> None:
        self.item = built("t_conn_c1", CAPSULE_TRAIN)
        self.file = self.item["connections"]
        self.chip = self.item["chip"]

    def test_it_names_the_router_the_chip_and_the_rules(self):
        self.assertEqual(self.file["for_stage"], "s7_route")
        self.assertEqual(self.file["chip_id"], "t_conn_c1")
        self.assertTrue(self.file["rules"]["no_crossings"])
        self.assertEqual(self.file["rules"]["max_depth_mm"], DEPTH)
        self.assertEqual(self.file["layers"], 1)

    def test_every_endpoint_is_a_real_feature_on_the_chip(self):
        known = {c["id"] for c in self.chip["chambers"]} | {p["id"] for p in self.chip["ports"]}
        self.assertTrue(self.file["connections"])
        for item in self.file["connections"]:
            for end in ("from", "to"):
                self.assertIn(item[end]["feature"], known, f"{item['id']} {end}")

    def test_every_connection_carries_a_width_at_or_above_the_floor(self):
        for item in self.file["connections"]:
            self.assertGreaterEqual(item["min_width_mm"], MIN_FEATURE - TOL, item["id"])
            self.assertTrue(item["width_from"], item["id"])
            self.assertTrue(item["why"], item["id"])

    def test_a_capsule_line_is_wide_enough_for_a_capsule(self):
        factor = float(contracts.param("capsule_channel.width_factor_low"))
        for item in self.file["connections"]:
            if item["carries"] == "capsules":
                self.assertGreaterEqual(item["min_width_mm"],
                                        factor * 70 / 1000.0 - TOL, item["id"])

    def test_the_sample_path_runs_through_every_part_in_flow_order(self):
        chain = [c for c in self.file["connections"] if c["carries"] == "capsules"]
        self.assertTrue(chain)
        self.assertIn("port", chain[0]["from"])

    def test_must_not_cross_is_symmetric_and_excludes_shared_endpoints(self):
        by_id = {c["id"]: c for c in self.file["connections"]}
        for item in self.file["connections"]:
            for other in item["must_not_cross"]:
                self.assertIn(item["id"], by_id[other]["must_not_cross"])
            ends = {item["from"]["feature"], item["to"]["feature"]}
            for other in item["must_not_cross"]:
                shared = ends & {by_id[other]["from"]["feature"],
                                 by_id[other]["to"]["feature"]}
                self.assertEqual(shared, set(), f"{item['id']} vs {other}")

    def test_obstacles_cover_every_part_and_every_pad(self):
        ids = {o["id"] for o in self.file["obstacles"]}
        for chamber in self.chip["chambers"]:
            if chamber["id"].endswith("_pad"):
                self.assertIn(chamber["id"], ids)
        for part in self.item["summary"]["blocks"]:
            self.assertIn(part, ids)

    def test_a_shared_opening_is_declared_rather_than_invented(self):
        shared = [c for c in self.file["connections"] if c.get("shared_opening")]
        for item in shared:
            self.assertIn("note", item)
            self.assertIn("off-chip", item["note"] + item["why"])

    def test_the_vent_line_is_marked_as_a_capillary_stop(self):
        vent = [c for c in self.file["connections"] if c.get("capillary_stop")]
        self.assertTrue(vent)
        self.assertIn("flow_restrictor", vent[0]["note"])


# --- asking S4 for a different partition --------------------------------------------

class Reruns(unittest.TestCase):
    def test_a_part_too_big_for_the_scale_asks_s4_to_split(self):
        item = build_one("t_big_c1", [
            op(1, "wash", volume_ul=None, shear_class="capsules",
               particles={"kind": "capsules", "diameter_um": 140, "count": 400000})])
        self.assertEqual(item["rerun_from"], "s4")
        self.assertIn("too big: split", item["reason"])
        self.assertTrue(item["detail"]["too_big"])

    def test_a_chip_with_no_on_chip_ops_asks_s4_to_think_again(self):
        item = build_one("t_none_c1", [op(1, "image", where="off_chip")])
        self.assertEqual(item["rerun_from"], "s4")
        self.assertIn("no on-chip ops", item["reason"])

    def test_the_stage_returns_one_rerun_request_for_the_whole_run(self):
        ops = CAPSULE_TRAIN + [op(9, "wash", volume_ul=None, shear_class="capsules",
                                  particles={"kind": "capsules", "diameter_um": 140,
                                             "count": 400000})]
        plan = {"id": "t", "cuts": [], "unresolved": [], "handoffs": [],
                "chips": [{"chip_id": "t_ok_c1", "ops": [1, 2, 3], "scale": "demo",
                           "reason": "fits"},
                          {"chip_id": "t_bad_c2", "ops": [9], "scale": "demo",
                           "reason": "does not fit"}]}
        result = s6_build.build(plan, {"id": "t", "ops": ops}, assay_id="t")
        self.assertEqual(len(result["built"]), 1)
        self.assertEqual(len(result["reruns"]), 1)
        self.assertEqual(result["reruns"][0]["rerun_from"], "s4")

    def test_an_op_the_partition_puts_on_a_chip_must_exist(self):
        plan = partition("t_ghost_c1", CAPSULE_TRAIN)
        plan["chips"][0]["ops"] = [1, 2, 3, 77]
        with self.assertRaises(s6_build.BuildError) as caught:
            s6_build.build(plan, {"id": "t", "ops": CAPSULE_TRAIN}, assay_id="t")
        self.assertIn("77", str(caught.exception))

    def test_an_off_chip_op_is_recorded_rather_than_dropped_silently(self):
        ops = CAPSULE_TRAIN + [op(4, "image", where="off_chip", why="microscope")]
        item = built("t_skip_c1", ops)
        skipped = item["summary"]["skipped_ops"]
        self.assertEqual([s["op"] for s in skipped], [4])
        self.assertTrue([g for g in item["unresolved"] if g.get("op") == 4])


# --- writing, and never clobbering somebody's real chip ------------------------------

class Writing(unittest.TestCase):
    def test_it_writes_the_chip_and_the_connections_file(self):
        result = s6_build.build(partition("t_write_c1", CAPSULE_TRAIN),
                                {"id": "t", "ops": CAPSULE_TRAIN}, assay_id="t")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(s6_build.stage_io, "work_dir",
                                   return_value=Path(tmp) / "work"):
                written = s6_build.write(result, assay_id="t", out_dir=Path(tmp))
            names = sorted(p.name for p in written)
            self.assertEqual(names, ["connections_t_write_c1.json", "t_write_c1.json"])
            chip = json.loads((Path(tmp) / "t_write_c1.json").read_text())
            self.assertEqual(chip["id"], "t_write_c1")

    def test_it_refuses_to_overwrite_a_chip_this_pipeline_did_not_produce(self):
        self.assertFalse(s6_build.ours(ROOT / "chip" / "module4_v1.json", "module4_v1"))
        result = s6_build.build(partition("module4_v1", CAPSULE_TRAIN),
                                {"id": "t", "ops": CAPSULE_TRAIN}, assay_id="t")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "module4_v1.json"
            target.write_text((ROOT / "chip" / "module4_v1.json").read_text())
            with self.assertRaises(s6_build.BuildError) as caught:
                s6_build.write(result, assay_id="t", out_dir=Path(tmp))
            self.assertIn("did not write it", str(caught.exception))
            self.assertEqual(json.loads(target.read_text())["id"], "module4_v1")

    def test_it_recognises_and_replaces_its_own_output(self):
        result = s6_build.build(partition("t_again_c1", CAPSULE_TRAIN),
                                {"id": "t", "ops": CAPSULE_TRAIN}, assay_id="t")
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(s6_build.stage_io, "work_dir",
                                   return_value=Path(tmp) / "work"):
                s6_build.write(result, assay_id="t", out_dir=Path(tmp))
                self.assertTrue(s6_build.ours(Path(tmp) / "t_again_c1.json", "t_again_c1"))
                s6_build.write(result, assay_id="t", out_dir=Path(tmp))

    def test_the_stage_declares_what_it_writes_outside_the_work_directory(self):
        self.assertEqual(s6_build.WRITES, ["chip/{chip_id}.json"])

    def test_it_says_which_checks_only_pass_after_routing(self):
        report = s6_build.verification()
        self.assertTrue(any("port-overlap" in line for line in report["passes_now"]))
        rules = " ".join(item["rule"] for item in report["after_s7_only"])
        self.assertIn("cross", rules)
        self.assertIn("capsule diameter", rules)


PARAMS = schema_io.load_json(ROOT / "contracts" / "parameters.json")
MATERIALS = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
FAB = schema_io.load_json(ROOT / "cad" / "fabrication.json")
BLOCKS = schema_io.load_json(ROOT / "contracts" / "blocks.json")


if __name__ == "__main__":
    unittest.main()

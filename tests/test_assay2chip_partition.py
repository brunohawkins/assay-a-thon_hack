"""S4 partition: the planarity test, every cut constraint, and the MVP-chain comparison.

    python -m unittest tests.test_assay2chip_partition -v

Runs offline and touches nothing outside a temporary directory. Three groups of tests:

**The planarity engine** is checked against graphs whose answer is a mathematical fact
(K5, K3,3 and their subdivisions are not planar; the cube and the octahedron are; K4 is
planar but not outerplanar) and then cross-checked on random small graphs against a
brute-force Kuratowski/Wagner minor oracle written independently in this file. A planarity
test that is subtly wrong would quietly approve a chip with a short circuit in it, so it
is verified rather than trusted.

**Each constraint** gets a test that makes it, and only it, fire.

**The ground truth** is the chain in ``protocols/pipeline_mvp.json``: module1, cellprep,
encapsulation, module3, barcoding, module4, module5, SPRI/QC. Those boards are all
30 x 20 mm, so the comparison pins S4 to the demo envelope and checks it lands on the same
six chips, the same four manual handoffs, and the same port count on the four chips whose
port list is not disputed in the repo's own notes.
"""

from __future__ import annotations

import itertools
import json
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts, io as stage_io  # noqa: E402
from assay2chip.partition import (  # noqa: E402
    area, chemistry, constraints, graph, handoffs, mvp_chain, ops as ops_mod, ports,
    segment, thermal,
)
from assay2chip.stages import s4_partition  # noqa: E402


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------

PARTS = ["polygon_chamber", "serpentine_channel", "trap_chamber", "culture_chamber",
         "ring_junction", "snake_channel", "square_maze", "round_maze", "wells_array"]


def op(number, kind, part="polygon_chamber", where="on_chip", volume_ul=5.0,
       temp_c=22.0, particles="none", diameter_um=None, duration_s=None,
       shear="none", viscosity="aqueous", note="", why="test fixture", need="",
       load=None):
    entry = {
        "op": number, "from_steps": [number], "kind": kind,
        "fluid": {"volume_ul": volume_ul, "viscosity_class": viscosity},
        "particles": {"kind": particles},
        "duration_s": duration_s, "temp_c": temp_c, "shear_class": shear,
        "placement": {"where": where, "why": why},
    }
    if particles != "none":
        entry["particles"]["diameter_um"] = diameter_um
    if part and where == "on_chip":
        entry["placement"]["part"] = part
    if need:
        entry["placement"]["need"] = need
    if load is not None:
        entry["load"] = load
    if note:
        entry["note"] = note
    return entry


def held(area_mm2=None, packed_ul=None, cells=None, density_per_ul=None,
         surface_density=None, basis=None, residence_s=None, source="derived"):
    """A `load` block: what the place has to HOLD, which is what sizes a part."""
    entry = {"basis": basis or ("adherent_area" if (area_mm2 or surface_density)
                                else "packed_volume" if packed_ul is not None
                                else "suspension_volume" if density_per_ul
                                else "flow_through"),
             "source": source}
    for key, value in (("area_mm2", area_mm2), ("packed_volume_ul", packed_ul),
                       ("cells", cells), ("suspension_density_per_ul", density_per_ul),
                       ("surface_density_per_mm2", surface_density),
                       ("residence_s", residence_s)):
        if value is not None:
            entry[key] = value
    return entry


def board_filling_load(scale):
    """A packed bed that just fills one envelope, so `fits` is about to flip."""
    env = contracts.envelope(scale)
    return env.area_mm2 * contracts.max_depth_mm()


def ops_of(*entries):
    return [ops_mod.Op.from_dict(e) for e in entries]


def unitops_of(*entries, assay_id="t"):
    return {"id": assay_id, "ops": list(entries), "unresolved": []}


# --- independent planarity oracle, for cross-checking ---------------------------------

def brute_force_planar(adjacency, vertices):
    """Planar iff no K5 and no K3,3 MINOR (Wagner). Exhaustive, so only for tiny graphs."""
    count = len(vertices)

    def connected(group):
        group = set(group)
        if not group:
            return False
        start = next(iter(group))
        seen, stack = {start}, [start]
        while stack:
            vertex = stack.pop()
            for other in adjacency[vertex] & group:
                if other not in seen:
                    seen.add(other)
                    stack.append(other)
        return seen == group

    def has_minor(pattern_size, pattern_edges):
        for size in range(pattern_size, count + 1):
            for subset in itertools.combinations(vertices, size):
                for assignment in itertools.product(range(pattern_size), repeat=size):
                    parts = [set() for _ in range(pattern_size)]
                    for vertex, slot in zip(subset, assignment):
                        parts[slot].add(vertex)
                    if any(not part for part in parts):
                        continue
                    if not all(connected(part) for part in parts):
                        continue
                    if all(any(w in parts[j] for v in parts[i] for w in adjacency[v])
                           for i, j in pattern_edges):
                        return True
        return False

    k5 = [(i, j) for i in range(5) for j in range(i + 1, 5)]
    if count >= 5 and has_minor(5, k5):
        return False
    k33 = [(i, 3 + j) for i in range(3) for j in range(3)]
    if count >= 6 and has_minor(6, k33):
        return False
    return True


def complete(n):
    names = ["v%d" % i for i in range(n)]
    return graph.undirected({a: [b for b in names if b != a] for a in names})


def bipartite(m, n):
    left = ["a%d" % i for i in range(m)]
    right = ["b%d" % j for j in range(n)]
    return graph.undirected({a: right for a in left})


def cycle(n):
    names = ["c%d" % i for i in range(n)]
    return graph.undirected({names[i]: [names[(i + 1) % n]] for i in range(n)})


def cube():
    names = ["".join(bits) for bits in itertools.product("01", repeat=3)]
    return graph.undirected({v: [w for w in names
                                 if sum(x != y for x, y in zip(v, w)) == 1]
                             for v in names})


def petersen():
    outer = ["o%d" % i for i in range(5)]
    inner = ["i%d" % i for i in range(5)]
    adjacency = {}
    for i in range(5):
        adjacency.setdefault(outer[i], []).extend([outer[(i + 1) % 5], inner[i]])
        adjacency.setdefault(inner[i], []).append(inner[(i + 2) % 5])
    return graph.undirected(adjacency)


def without_an_edge(g, which=0):
    g = {k: set(v) for k, v in g.items()}
    a, b = graph.edges(g)[which]
    g[a].discard(b)
    g[b].discard(a)
    return g


def subdivided(g, times=1):
    g = {k: set(v) for k, v in g.items()}
    for step in range(times):
        a, b = graph.edges(g)[step % len(graph.edges(g))]
        middle = "sub%d" % step
        g[a].discard(b)
        g[b].discard(a)
        g[middle] = {a, b}
        g[a].add(middle)
        g[b].add(middle)
    return g


# --------------------------------------------------------------------------------------
# the planarity engine
# --------------------------------------------------------------------------------------

class Planarity(unittest.TestCase):
    """The one constraint a bigger envelope cannot fix, so it must be exact."""

    def test_known_graphs(self):
        cases = [
            ("K4", complete(4), True),
            ("K5", complete(5), False),
            ("K5 minus an edge", without_an_edge(complete(5)), True),
            ("K5 subdivided", subdivided(complete(5), 3), False),
            ("K3,3", bipartite(3, 3), False),
            ("K3,3 minus an edge", without_an_edge(bipartite(3, 3)), True),
            ("K3,3 subdivided", subdivided(bipartite(3, 3), 4), False),
            ("K2,3", bipartite(2, 3), True),
            ("K3,4", bipartite(3, 4), False),
            ("K7", complete(7), False),
            ("the 3-cube", cube(), True),
            ("the Petersen graph", petersen(), False),
            ("C7", cycle(7), True),
        ]
        for name, g, expected in cases:
            with self.subTest(graph=name):
                self.assertEqual(graph.is_planar(g).planar, expected)

    def test_non_planar_result_names_the_parts_involved(self):
        result = graph.is_planar(bipartite(3, 3))
        self.assertFalse(result.planar)
        self.assertTrue(result.reason)
        self.assertTrue(result.witness or result.attachments,
                        "a non-planar verdict must name the vertices, so a cut can "
                        "explain itself")

    def test_a_disconnected_or_tree_graph_is_planar(self):
        forest = graph.undirected({"a": ["b"], "c": ["d", "e"], "e": ["f"]})
        self.assertTrue(graph.is_planar(forest).planar)

    def test_two_non_planar_pieces_joined_by_a_bridge_are_still_non_planar(self):
        g = complete(5)
        second = graph.undirected({"y%d" % i: ["y%d" % j for j in range(5) if j != i]
                                   for i in range(5)})
        g.update(second)
        graph.add_edge(g, "v0", "y0")
        self.assertFalse(graph.is_planar(g).planar)

    def test_ports_on_the_boundary_is_stricter_than_planarity(self):
        """K4 is planar; K4 with all four vertices on the outer face is K5, which is not.

        This is the whole reason S4 uses fits_one_layer and not is_planar: a port is a
        through-hole that tubing has to reach, and blocks.json gives no second layer to
        reach it from underneath.
        """
        self.assertTrue(graph.is_planar(complete(4)).planar)
        self.assertFalse(graph.is_outerplanar(complete(4)).planar)

    def test_outerplanarity_matches_its_two_forbidden_minors(self):
        for name, g, expected in [("C5", cycle(5), True),
                                  ("a tree", graph.undirected({"r": ["a", "b", "c"],
                                                               "a": ["d", "e"]}), True),
                                  ("K4", complete(4), False),
                                  ("K2,3", bipartite(2, 3), False)]:
            with self.subTest(graph=name):
                self.assertEqual(graph.is_outerplanar(g).planar, expected)

    def test_cross_checked_against_a_brute_force_minor_oracle(self):
        """200 random graphs, against an independent Wagner-minor implementation."""
        random.seed(20261107)
        checked = 0
        for _ in range(200):
            count = random.randint(4, 6)
            names = ["n%d" % i for i in range(count)]
            adjacency = {v: set() for v in names}
            for a, b in itertools.combinations(names, 2):
                if random.random() < 0.55:
                    adjacency[a].add(b)
                    adjacency[b].add(a)
            mine = graph.is_planar({k: set(v) for k, v in adjacency.items()}).planar
            truth = brute_force_planar(adjacency, names)
            self.assertEqual(mine, truth,
                             "disagreed with the oracle on %s" % graph.edges(adjacency))
            checked += 1
        self.assertEqual(checked, 200)

    def test_the_single_layer_rule_really_is_in_the_contract(self):
        self.assertEqual(contracts.envelope("demo").layers, 1)
        self.assertTrue(contracts.single_layer())
        self.assertIn("crossing", contracts.block("excluded.multilayer"))


# --------------------------------------------------------------------------------------
# the port model, calibrated against the chips in this repo
# --------------------------------------------------------------------------------------

class PortModel(unittest.TestCase):
    """The port count is the limit that cuts most often, so it is held to real chips."""

    def ports_for(self, entries, scale="demo"):
        return ports.plan(ops_of(*entries), scale)

    def test_module3_needs_exactly_the_six_ports_module3_v1_has(self):
        """load, lyse, wash, RT, wash, release in ONE trap: 6 ports, as built."""
        capsule = contracts.capsule_diameter_um()
        entries = [
            op(1, "load", "trap_chamber", particles="capsules", diameter_um=capsule),
            op(2, "lyse", "trap_chamber", particles="capsules", diameter_um=capsule,
               note="guanidine thiocyanate lysis buffer"),
            op(3, "wash", "trap_chamber", particles="capsules", diameter_um=capsule),
            op(4, "incubate", "trap_chamber", particles="capsules", diameter_um=capsule,
               temp_c=contracts.param("rt.temp_c"), note="reverse transcriptase rt mix"),
            op(5, "wash", "trap_chamber", particles="capsules", diameter_um=capsule),
            op(6, "transfer", "trap_chamber", particles="capsules", diameter_um=capsule),
        ]
        plan = self.ports_for(entries)
        self.assertEqual(plan.count, 6, plan.tally())
        roles = sorted(p.role for p in plan.ports)
        self.assertEqual(roles, ["backflush", "feed", "inlet", "outlet", "waste", "waste"])
        self.assertEqual(len(plan.runs), 1,
                         "six ops in one chamber are one place, not six")

    def test_module4_counts_the_split_paths_and_the_vent(self):
        """A 4-way sublibrary split plus the 98 C PCR: inlet, 3 split paths, vent, outlet."""
        capsule = contracts.capsule_diameter_um()
        entries = [
            op(1, "split", "polygon_chamber", particles="capsules", diameter_um=capsule),
            op(2, "thermocycle", "polygon_chamber", particles="capsules",
               diameter_um=capsule, temp_c=contracts.param("pcr.max_temp_c")),
        ]
        plan = self.ports_for(entries)
        self.assertEqual(len(plan.by_role("split")),
                         int(contracts.param("sublibraries.count")) - 1)
        self.assertEqual(len(plan.by_role("vent")), 1)
        self.assertEqual(len(plan.by_role("feed")), 0,
                         "the amplification mix arrives with the sample, as on module4_v1")
        # module4_v1 has 5 ports and pipeline_status.md records the missing 6th as a bug:
        # "Module4 has no capsule outlet ... Port stays TBC".
        self.assertEqual(plan.count, 6, plan.tally())

    def test_module5_keeps_two_feeds_because_the_beads_are_a_different_bed(self):
        """Both chambers are `polygon_chamber`, so only the new particles separate them."""
        capsule = contracts.capsule_diameter_um()
        entries = [
            op(1, "mix", "polygon_chamber", particles="capsules", diameter_um=capsule,
               temp_c=37.0, note="collagenase release buffer"),
            op(2, "incubate", "polygon_chamber", particles="capsules",
               diameter_um=capsule, temp_c=37.0),
            op(3, "separate", "polygon_chamber", particles="beads", diameter_um=1.0,
               note="magnetic bead capture with ethanol washes"),
        ]
        plan = self.ports_for(entries)
        self.assertEqual(len(plan.runs), 2,
                         "the bead chamber is a second place even on the same block id")
        self.assertEqual(len(plan.by_role("feed")), 2)
        self.assertEqual(plan.count, 5, plan.tally())

    def test_cell_prep_gives_the_quench_its_own_feed(self):
        """Enzyme at the chamber, quench downstream at the mixer: two places, two feeds."""
        entries = [
            op(1, "mix", "culture_chamber", particles="cells", diameter_um=15.0,
               temp_c=37.0, note="trypsin edta"),
            op(2, "incubate", "culture_chamber", particles="cells", diameter_um=15.0,
               temp_c=37.0),
            op(3, "mix", "serpentine_channel", particles="cells", diameter_um=15.0,
               temp_c=37.0, note="culture media quench"),
            op(4, "collect", "polygon_chamber", particles="cells", diameter_um=15.0),
        ]
        plan = self.ports_for(entries)
        self.assertEqual(len(plan.by_role("feed")), 2)
        self.assertEqual(plan.count, 4, plan.tally())

    def test_encapsulation_has_two_feeds_and_no_vent(self):
        """spc_demo_v1: in_oil, in_shell, in_core, out_caps - four ports, no vent."""
        entries = [op(1, "encapsulate", "flow_focusing", particles="capsules",
                      diameter_um=contracts.capsule_diameter_um(), temp_c=37.0,
                      viscosity="viscous", note="fluorinated oil and gelma shell")]
        plan = self.ports_for(entries)
        self.assertEqual(len(plan.by_role("feed")), 2)
        self.assertEqual(len(plan.by_role("vent")), 0,
                         "the oil leaves with the capsules; a droplet maker needs no vent")
        self.assertEqual(plan.count, 4, plan.tally())

    def test_every_port_says_why_it_exists(self):
        plan = self.ports_for([op(1, "wash", "trap_chamber", particles="capsules",
                                  diameter_um=contracts.capsule_diameter_um())])
        for port in plan.ports:
            self.assertTrue(port.why, "port %s has no reason" % port.id)
            self.assertRegex(port.id, r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")

    def test_target_scale_gives_dedicated_feeds_as_the_contract_says_it_does(self):
        entries = [
            op(1, "wash", "trap_chamber"),
            op(2, "lyse", "trap_chamber", note="guanidine thiocyanate"),
            op(3, "wash", "trap_chamber"),
        ]
        demo = self.ports_for(entries, "demo")
        target = self.ports_for(entries, "target")
        self.assertEqual(len(demo.by_role("feed")), 1,
                         "one shared line through an off-chip selector at demo scale")
        self.assertEqual(len(target.by_role("feed")), 3,
                         "dedicated feeds at target scale, per design_rules_at_this_scale")
        self.assertIsNone(ports.limit_for("target"),
                          "the contract does not cap ports at target scale")
        self.assertEqual(ports.limit_for("demo"),
                         int(contracts.floor("max_ports_per_chip")))


# --------------------------------------------------------------------------------------
# every constraint that can force a cut
# --------------------------------------------------------------------------------------

class Constraints(unittest.TestCase):

    def assert_cut_for(self, entries, constraint, forced_by=None, scale="demo"):
        """`scale` defaults to demo: the port cap and the whole-board thermal ramp are
        demo-envelope constraints, and the contract says both change at target scale."""
        verdict = constraints.feasible(ops_of(*entries), scale)
        self.assertFalse(verdict.ok, "expected these ops to need a cut")
        names = [v.constraint for v in verdict.violations]
        self.assertIn(constraint, names, "violations were %s" % names)
        found = next(v for v in verdict.violations if v.constraint == constraint)
        if forced_by:
            self.assertEqual(found.forced_by, forced_by)
        self.assertTrue(found.keys, "a cut must name the contract key that forced it")
        return found

    def test_live_cells_cannot_share_a_board_with_the_rt_hold(self):
        found = self.assert_cut_for([
            op(1, "culture", "culture_chamber", particles="cells", diameter_um=15.0,
               temp_c=contracts.param("culture.temp_c"), shear="adherent_cells"),
            op(2, "incubate", "culture_chamber", particles="cells", diameter_um=15.0,
               temp_c=contracts.param("rt.temp_c")),
        ], "temperature", "temperature")
        self.assertIn("rt.temp_c", " ".join(found.keys))

    def test_live_cells_cannot_share_a_board_with_the_98c_pcr(self):
        self.assert_cut_for([
            op(1, "culture", "culture_chamber", particles="cells", diameter_um=15.0,
               temp_c=contracts.param("culture.temp_c"), shear="adherent_cells"),
            op(2, "thermocycle", "polygon_chamber",
               temp_c=contracts.param("pcr.max_temp_c")),
        ], "temperature")

    def test_the_98c_dock_cannot_share_a_demo_board_with_a_held_setpoint(self):
        found = self.assert_cut_for([
            op(1, "thermocycle", "polygon_chamber",
               temp_c=contracts.param("pcr.max_temp_c")),
            op(2, "incubate", "polygon_chamber", temp_c=37.0),
        ], "temperature")
        self.assertIn("design_rules_at_this_scale", " ".join(found.keys))
        # At target scale the same contract line allows a zoned dock, so it is legal.
        self.assertTrue(constraints.feasible(ops_of(
            op(1, "thermocycle", "polygon_chamber",
               temp_c=contracts.param("pcr.max_temp_c")),
            op(2, "incubate", "polygon_chamber", temp_c=37.0),
        ), "target").ok)

    def test_but_the_98c_dock_may_carry_the_split_that_feeds_it(self):
        """module4_v1 does exactly this, so the rule must not forbid it."""
        verdict = constraints.feasible(ops_of(
            op(1, "split", "polygon_chamber"),
            op(2, "thermocycle", "polygon_chamber",
               temp_c=contracts.param("pcr.max_temp_c")),
        ), "demo")
        self.assertTrue(verdict.ok, [v.why for v in verdict.violations])

    def test_the_port_limit_forces_a_cut_and_quotes_the_contract(self):
        entries = [op(i + 1, "wash", PARTS[i]) for i in range(6)]
        found = self.assert_cut_for(entries, "ports", "ports")
        self.assertIn("max_ports_per_chip", " ".join(found.keys))
        self.assertIn(str(int(contracts.floor("max_ports_per_chip"))), found.why)
        # ... and it does not cut at target scale, because the contract lifts it there.
        self.assertNotIn("ports", [v.constraint for v in
                                   constraints.feasible(ops_of(*entries), "target")
                                   .violations])

    def test_area_forces_a_cut_when_the_load_does_not_fit(self):
        """A bed three demo boards deep cannot sit on one, and `load` is what says so."""
        too_big = board_filling_load("demo") * 3
        found = self.assert_cut_for(
            [op(1, "incubate", "polygon_chamber", load=held(packed_ul=too_big))],
            "area", "area")
        self.assertIn("envelope", " ".join(found.keys))
        # Two beds of 60% of the target plate do not fit it either.
        each = board_filling_load("target") * 0.6
        self.assert_cut_for([op(1, "incubate", "polygon_chamber",
                                load=held(packed_ul=each)),
                             op(2, "incubate", "serpentine_channel",
                                load=held(packed_ul=each))],
                            "area", "area", scale="target")

    def test_a_bench_sized_wash_volume_no_longer_forces_a_bigger_board(self):
        """The regression that failed the first end-to-end run at S6.

        A 200-500 uL wash is 667-1,667 mm2 at the etch depth, so it used to push every
        chip past the 600 mm2 demo envelope on its own.
        """
        entries = [op(1, "load", "trap_chamber", load=held(packed_ul=30.0)),
                   op(2, "wash", "trap_chamber", volume_ul=500.0),
                   op(3, "lyse", "trap_chamber", volume_ul=200.0,
                      load=held(packed_ul=30.0))]
        verdict = constraints.feasible(ops_of(*entries))
        self.assertTrue(verdict.ok, [v.why for v in verdict.violations])
        self.assertEqual(verdict.scale, "demo")
        naive = sum(e["fluid"]["volume_ul"] or 0 for e in entries) / \
            contracts.max_depth_mm()
        self.assertGreater(naive, contracts.envelope("demo").area_mm2,
                           "the paper's volumes really would not have fitted")
        self.assertLess(verdict.area.parts_mm2, naive / 10)

    def test_planarity_forces_a_cut_that_no_envelope_can_fix(self):
        """Two interleaved waste paths across five parts cannot be cut in one layer."""
        entries = []
        for index in range(5):
            if index % 2 == 0:
                entries.append(op(index + 1, "lyse", PARTS[index],
                                  note="guanidine thiocyanate lysis buffer"))
            else:
                entries.append(op(index + 1, "wash", PARTS[index],
                                  note="polymerase amplification mix"))
        found = self.assert_cut_for(entries, "planarity", "other")
        self.assertIn("multilayer", " ".join(found.keys))
        # And it is not merely the port count: it fails at target scale too, where the
        # contract caps nothing.
        verdict = constraints.feasible(ops_of(*entries), "target")
        self.assertFalse(verdict.ok)
        self.assertIn("planarity", [v.constraint for v in verdict.violations])

    def test_incompatible_chemistry_cannot_share_a_board_at_all(self):
        found = self.assert_cut_for([
            op(1, "lyse", "trap_chamber", note="guanidine thiocyanate lysis buffer"),
            op(2, "wash", "trap_chamber", note="hypochlorite decontamination flush"),
        ], "contamination", "contamination")
        self.assertIn("cyanide", found.why)

    def test_chaotropic_waste_keeps_its_own_outlet(self):
        entries = ops_of(
            op(1, "lyse", "trap_chamber", note="guanidine thiocyanate lysis buffer"),
            op(2, "wash", "trap_chamber"),
            op(3, "incubate", "trap_chamber", temp_c=contracts.param("rt.temp_c"),
               note="reverse transcriptase rt mix"),
            op(4, "wash", "trap_chamber"),
        )
        paths = chemistry.waste_paths(entries)
        self.assertEqual(len(paths), 2,
                         "module3_v1_translated's waste and lysis_waste, derived: %s"
                         % paths)
        self.assertTrue(any("chaotropic" in group for group, _ in paths))

    def test_shear_classes_that_are_bounded_differently_cannot_share_a_board(self):
        found = self.assert_cut_for([
            op(1, "culture", "culture_chamber", particles="cells", diameter_um=15.0,
               temp_c=37.0, shear="adherent_cells"),
            op(2, "wash", "trap_chamber", particles="capsules", temp_c=37.0,
               diameter_um=contracts.capsule_diameter_um(), shear="capsules"),
        ], "shear", "shear")
        self.assertIn("escape_pressure", " ".join(found.keys))

    def test_material_is_chosen_from_the_contract_not_assumed(self):
        """COC for the chaotropic and 98 C boards; the simplest polymer for plain culture."""
        hot = thermal.choose_material(ops_of(
            op(1, "thermocycle", "polygon_chamber",
               temp_c=contracts.param("pcr.max_temp_c"))))
        self.assertTrue(hot.material_id)
        self.assertGreaterEqual(hot.max_working_temp_c, contracts.param("pcr.max_temp_c"))
        entry = thermal.materials()[hot.material_id]
        self.assertFalse(str(entry["pcr_98c_status"]).upper().startswith("DISQUALIFIED"))

        cool = thermal.choose_material(ops_of(
            op(1, "culture", "culture_chamber", particles="cells", diameter_um=15.0,
               temp_c=contracts.param("culture.temp_c"), shear="adherent_cells")))
        self.assertTrue(cool.material_id)
        self.assertLessEqual(cool.max_working_temp_c, hot.max_working_temp_c,
                             "a 37 C board should not need the hottest-rated polymer")

    def test_ops_needing_different_materials_are_reported_as_a_split(self):
        culture = ops_of(op(1, "culture", "culture_chamber", particles="cells",
                            diameter_um=15.0, temp_c=37.0, shear="adherent_cells"))
        pcr = ops_of(op(2, "thermocycle", "polygon_chamber",
                        temp_c=contracts.param("pcr.max_temp_c")))
        reason = thermal.material_split(culture, pcr)
        self.assertIsNotNone(reason)
        self.assertIn("materials.json", reason)

    def test_a_feasible_chip_reports_scale_ports_area_and_material(self):
        verdict = constraints.feasible(ops_of(
            op(1, "wash", "trap_chamber", particles="capsules",
               diameter_um=contracts.capsule_diameter_um(), shear="capsules")))
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.scale, "demo", "prefer what can be printed today")
        self.assertGreater(verdict.port_count, 0)
        self.assertIsNotNone(verdict.area)
        self.assertTrue(verdict.area.fits)

    def test_demo_is_preferred_and_target_is_used_only_when_demo_fails(self):
        target = contracts.envelope("target")
        bed = board_filling_load("demo") * 1.5        # too big for demo, fine for target
        verdict = constraints.feasible(ops_of(op(1, "incubate", "polygon_chamber",
                                                 load=held(packed_ul=bed))))
        self.assertTrue(verdict.ok, [v.why for v in verdict.violations])
        self.assertEqual(verdict.scale, "target")
        self.assertLess(verdict.area.total_mm2, target.area_mm2)
        # And a bed that fits demo stays on demo.
        self.assertEqual(constraints.feasible(ops_of(
            op(1, "incubate", "polygon_chamber",
               load=held(packed_ul=board_filling_load("demo") * 0.1)))).scale, "demo")

    def test_the_ceiling_is_never_offered_as_a_scale(self):
        self.assertNotIn("ceiling", area.SCALES)
        schema = stage_io.schemas()["partition"]
        allowed = schema["properties"]["chips"]["items"]["properties"]["scale"]["enum"]
        self.assertEqual(sorted(allowed), ["demo", "target"])


# --------------------------------------------------------------------------------------
# area terms
# --------------------------------------------------------------------------------------

class Area(unittest.TestCase):

    def test_the_papers_volume_never_sizes_a_part(self):
        """The bug that failed the first end-to-end run at S6, pinned down.

        A 500 uL bench wash is 1,667 mm2 at the 0.3 mm etch depth - nearly three demo
        boards. It must change nothing.
        """
        small = area.part_area(ops_of(
            op(1, "incubate", "polygon_chamber", volume_ul=0.5,
               load=held(packed_ul=10.0))))
        huge = area.part_area(ops_of(
            op(1, "incubate", "polygon_chamber", volume_ul=500.0,
               load=held(packed_ul=10.0))))
        self.assertEqual(small.area_mm2, huge.area_mm2)
        self.assertAlmostEqual(huge.area_mm2, 10.0 / contracts.max_depth_mm(), places=6)

    def test_a_packed_bed_is_its_volume_over_the_etch_depth(self):
        depth = contracts.max_depth_mm()
        got = area.part_area(ops_of(op(1, "incubate", "polygon_chamber", volume_ul=999.0,
                                       load=held(packed_ul=100.0))))
        self.assertAlmostEqual(got.area_mm2, 100.0 / depth, places=6)
        self.assertEqual(got.basis, "packed_volume")
        self.assertIn("etch depth", got.driver)

    def test_an_adherent_layer_is_a_floor_area_not_a_volume(self):
        direct = area.part_area(ops_of(op(1, "culture", "culture_chamber", volume_ul=400.0,
                                          load=held(area_mm2=31.3))))
        self.assertAlmostEqual(direct.area_mm2, 31.3, places=6)
        self.assertEqual(direct.basis, "adherent_area")
        seeded = area.part_area(ops_of(op(1, "culture", "culture_chamber",
                                          load=held(cells=60000.0, surface_density=2000.0,
                                                    basis="adherent_area"))))
        self.assertAlmostEqual(seeded.area_mm2, 30.0, places=6)
        self.assertIn("cells at", seeded.driver)

    def test_a_suspension_is_the_count_over_the_stated_density(self):
        got = area.part_area(ops_of(op(1, "collect", "polygon_chamber", volume_ul=300.0,
                                       load=held(cells=3000.0, density_per_ul=1500.0))))
        self.assertAlmostEqual(got.area_mm2, (3000.0 / 1500.0) / contracts.max_depth_mm(),
                               places=6)

    def test_a_bed_can_be_derived_from_the_count_and_the_packing_fraction(self):
        capsule = contracts.capsule_diameter_um()
        swollen = contracts.swollen_capsule_diameter_um()
        count = 10000.0
        got = area.part_area(ops_of(
            op(1, "load", "trap_chamber", particles="capsules", diameter_um=capsule,
               load=held(cells=count, basis="packed_volume"))))
        one_ul = 4.0 / 3.0 * 3.141592653589793 * (swollen / 2000.0) ** 3
        expected = count * one_ul / contracts.param("capsule.packing_fraction")
        self.assertAlmostEqual(got.area_mm2, expected / contracts.max_depth_mm(), places=6)
        self.assertIn("packing fraction", got.driver)

    def test_a_wash_adds_no_area_because_it_sweeps_a_chamber_that_exists(self):
        """README: a wash volume is "dropped - becomes a SWEEP of the chamber"."""
        trap = op(1, "load", "trap_chamber", load=held(packed_ul=30.0))
        wash = op(2, "wash", "trap_chamber", volume_ul=500.0,
                  load=held(packed_ul=500.0))
        alone = area.part_area(ops_of(trap))
        with_wash = area.part_area(ops_of(trap, wash))
        self.assertEqual(alone.area_mm2, with_wash.area_mm2)

    def test_a_run_takes_the_largest_load_not_the_sum(self):
        """The ops happen one after another in the same place."""
        first = op(1, "load", "trap_chamber", load=held(packed_ul=30.0))
        second = op(2, "lyse", "trap_chamber", load=held(packed_ul=30.0))
        third = op(3, "incubate", "trap_chamber", load=held(packed_ul=45.0))
        got = area.part_area(ops_of(first, second, third))
        self.assertAlmostEqual(got.area_mm2, 45.0 / contracts.max_depth_mm(), places=6)

    def test_flow_through_is_a_channel_not_a_chamber(self):
        got = area.part_area(ops_of(op(1, "mix", "serpentine_channel", volume_ul=250.0,
                                       load=held(residence_s=10.0,
                                                 basis="flow_through"))))
        self.assertIsNone(got.area_mm2)
        self.assertIn("holds nothing", got.driver)

    def test_a_part_is_at_least_a_capsule_passing_channel_wide(self):
        swollen = contracts.swollen_capsule_diameter_um()
        factor = contracts.param("capsule_channel.width_factor_low")
        got = area.part_area(ops_of(
            op(1, "wash", "trap_chamber", volume_ul=0.0001, particles="capsules",
               diameter_um=contracts.capsule_diameter_um())))
        self.assertAlmostEqual(got.side_mm, factor * swollen / 1000.0, places=6)

    def test_port_cells_use_the_contract_pitch_and_are_a_real_share_of_the_board(self):
        pitch = contracts.floor("min_port_pitch_mm")
        limit = int(contracts.floor("max_ports_per_chip"))
        estimate = area.estimate(ops_of(op(1, "wash", "trap_chamber", volume_ul=1.0)),
                                 limit, "demo")
        self.assertAlmostEqual(estimate.ports_mm2, limit * pitch * pitch, places=6)
        self.assertGreater(estimate.ports_mm2 / estimate.envelope_mm2, 0.2,
                           "six ports at a 5 mm pitch claim a quarter of the demo board")

    def test_an_op_with_no_load_is_reported_not_guessed(self):
        estimate = area.estimate(ops_of(op(1, "incubate", "polygon_chamber",
                                           volume_ul=250.0)), 4, "demo")
        self.assertEqual(estimate.unknown_loads, (1,),
                         "no `load` means the area is a floor, and that must be said")

    def test_a_wash_without_a_load_is_not_reported_as_a_gap(self):
        estimate = area.estimate(ops_of(op(1, "load", "trap_chamber",
                                           load=held(packed_ul=30.0)),
                                        op(2, "wash", "trap_chamber", volume_ul=500.0)),
                                 4, "demo")
        self.assertEqual(estimate.unknown_loads, (),
                         "a wash needs no load: it sweeps a chamber that already exists")

    def test_the_envelope_numbers_come_from_the_contract(self):
        demo = contracts.envelope("demo")
        self.assertEqual((demo.x_mm, demo.y_mm),
                         (float(contracts.block("envelope.max_x_mm")),
                          float(contracts.block("envelope.max_y_mm"))))
        target = contracts.envelope("target")
        self.assertEqual((target.x_mm, target.y_mm),
                         (float(contracts.block("target_envelope.x_mm")),
                          float(contracts.block("target_envelope.y_mm"))))


# --------------------------------------------------------------------------------------
# handoffs and their computed risks
# --------------------------------------------------------------------------------------

class Handoffs(unittest.TestCase):

    def capsule_handoff(self, release=40.0, draw=40.0):
        capsule = contracts.capsule_diameter_um()
        upstream = ops_of(op(1, "transfer", "trap_chamber", volume_ul=release,
                             particles="capsules", diameter_um=capsule))
        downstream = ops_of(op(2, "load", "trap_chamber", volume_ul=draw,
                               particles="capsules", diameter_um=capsule))
        return handoffs.build("c1", "outlet_1", "c2", "inlet_1", upstream, downstream)

    def test_the_particle_in_the_line_is_the_swollen_capsule(self):
        got = self.capsule_handoff()
        self.assertAlmostEqual(got.data["particle_um"],
                               contracts.swollen_capsule_diameter_um(), places=6)
        self.assertGreater(got.data["particle_um"], contracts.capsule_diameter_um(),
                           "a capsule swells by capsule.swell_factor before it travels")

    def test_dead_volume_is_computed_and_compared_with_what_is_carried(self):
        small = self.capsule_handoff(release=5.0, draw=5.0)
        large = self.capsule_handoff(release=900.0, draw=900.0)
        self.assertGreater(small.data["dead_volume_ul"], 0)
        self.assertEqual(small.data["dead_volume_ul"], large.data["dead_volume_ul"])
        self.assertTrue(any("above the" in r for r in small.data["risks"]),
                        "a 5 uL transfer through this line is mostly dead volume")
        self.assertTrue(any("inside control.error_threshold" in r
                            for r in large.data["risks"]))

    def test_settling_is_stokes_at_37c_and_lands_in_the_contracts_own_band(self):
        """blocks.json's sedimentation sump quotes ~50-210 um/s for a capsule as made.

        That is the independent check on this calculation: the same Stokes expression,
        with the same 37 C viscosity, has to reproduce the band the contract already
        quotes for an unswollen capsule.
        """
        diameter = contracts.capsule_diameter_um()
        velocity, key = handoffs.settling_velocity_m_s(diameter, "capsules", 37.0)
        self.assertEqual(key, contracts.VISCOSITY_37C_KEY,
                         "37 C work uses fluid.viscosity_37c_pa_s, not 20 C water")
        in_um_s = velocity * 1e6
        self.assertTrue(50.0 <= in_um_s <= 210.0,
                        "%.0f um/s is outside the 50-210 um/s band in blocks.json "
                        "retention.alternatives_ranked" % in_um_s)
        # Swelling doubles the diameter, and Stokes goes as the square of it, so a
        # swollen capsule settles about four times faster. That is the number a handoff
        # has to use, and it is well outside the sump's band.
        swollen, _ = handoffs.settling_velocity_m_s(
            contracts.swollen_capsule_diameter_um(), "capsules", 37.0)
        self.assertAlmostEqual(swollen / velocity,
                               contracts.capsule_swell_factor() ** 2, places=6)
        self.assertGreater(swollen * 1e6, 210.0)
        # Water at 20 C would understate it: viscosity is in the denominator.
        cold, cold_key = handoffs.settling_velocity_m_s(diameter, "capsules", 20.0)
        self.assertEqual(cold_key, contracts.VISCOSITY_20C_KEY)
        self.assertLess(cold, velocity)

    def test_a_settling_risk_names_a_flow_rate_an_operator_can_set(self):
        got = self.capsule_handoff()
        settling = [r for r in got.data["risks"] if "sedimentation" in r]
        self.assertEqual(len(settling), 1)
        self.assertIn("uL/min", settling[0])
        self.assertIn("Stokes", settling[0])

    def test_the_repos_own_77_to_9_6_mismatch_is_caught_and_made_manual(self):
        got = self.capsule_handoff(release=76.9, draw=9.6)
        mismatch = [r for r in got.data["risks"] if "volume mismatch" in r]
        self.assertEqual(len(mismatch), 1, got.data["risks"])
        self.assertIn("76.9", mismatch[0])
        self.assertIn("9.6", mismatch[0])
        self.assertFalse(got.automated,
                         "a chip releasing 77 uL into one drawing 9.6 uL needs a spin")
        self.assertTrue(any("concentration" in str(g.get("question", ""))
                            for g in got.gaps))

    def test_a_mismatch_inside_the_control_tolerance_is_not_a_risk(self):
        threshold = contracts.param("control.error_threshold")
        got = self.capsule_handoff(release=40.0, draw=40.0 * (1 - threshold / 2))
        self.assertFalse([r for r in got.data["risks"] if "volume mismatch" in r])
        self.assertTrue(got.automated)

    def test_off_chip_work_in_the_gap_is_never_automated(self):
        gap = ops_of(op(2, "transfer", None, where="off_chip", volume_ul=40.0,
                        why="a spin and a bought strainer at the bench",
                        note="manual pipette, spin, 40 um strainer"))
        got = handoffs.build("c1", "outlet_1", "c2", "inlet_1",
                             ops_of(op(1, "collect", "polygon_chamber", volume_ul=40.0)),
                             ops_of(op(3, "load", "trap_chamber", volume_ul=40.0)),
                             gap)
        self.assertFalse(got.automated)
        self.assertIn("bench", got.data["how"])

    def test_a_machine_named_in_the_gap_may_be_automated(self):
        gap = ops_of(op(2, "transfer", None, where="off_chip", volume_ul=40.0,
                        why="routed by the rotary valve between the two boards",
                        note="rotary valve under software control"))
        got = handoffs.build("c1", "outlet_1", "c2", "inlet_1",
                             ops_of(op(1, "collect", "polygon_chamber", volume_ul=40.0)),
                             ops_of(op(3, "load", "trap_chamber", volume_ul=40.0)),
                             gap)
        self.assertTrue(got.automated)
        self.assertIn("rotary valve", got.data["how"])

    def test_a_transfer_crossing_a_cut_becomes_the_handoff(self):
        upstream = ops_of(op(1, "transfer", "trap_chamber", volume_ul=40.0,
                             note="released through to_split_tree by a syringe pump"))
        got = handoffs.build("c1", "outlet_1", "c2", "inlet_1", upstream,
                             ops_of(op(2, "load", "trap_chamber", volume_ul=40.0)))
        self.assertTrue(got.automated)
        self.assertIn("op 1 (transfer)", got.data["how"])
        self.assertIn("crosses this cut", got.data["note"])

    def test_a_manual_transfer_stays_manual(self):
        upstream = ops_of(op(1, "transfer", "trap_chamber", volume_ul=40.0,
                             note="moved by hand with a pipette into a tube"))
        got = handoffs.build("c1", "outlet_1", "c2", "inlet_1", upstream,
                             ops_of(op(2, "load", "trap_chamber", volume_ul=40.0)))
        self.assertFalse(got.automated)

    def test_carries_is_one_of_the_four_the_schema_allows(self):
        allowed = set(stage_io.schemas()["partition"]["properties"]["handoffs"]["items"]
                      ["properties"]["carries"]["enum"])
        capsule = contracts.capsule_diameter_um()
        cases = [
            (ops_of(op(1, "culture", "culture_chamber", particles="cells",
                       diameter_um=15.0)), "cells"),
            (ops_of(op(1, "load", "trap_chamber", particles="capsules",
                       diameter_um=capsule)), "capsules"),
            (ops_of(op(1, "thermocycle", "polygon_chamber", temp_c=98.0)), "library"),
            (ops_of(op(1, "wash", "polygon_chamber")), "supernatant"),
        ]
        for upstream, expected in cases:
            with self.subTest(expected=expected):
                got = handoffs.carries_for(upstream, [])
                self.assertIn(got, allowed)
                self.assertEqual(got, expected)

    def test_the_density_band_is_read_at_its_loss_end_and_says_which_end(self):
        """Settling is linear in excess density, so a loss question takes the max."""
        for carries, key in [("cells", "cell.excess_density_kg_m3"),
                             ("capsules", "capsule.excess_density_kg_m3"),
                             ("beads", "bead.excess_density_kg_m3"),
                             ("library", "bead.excess_density_kg_m3")]:
            with self.subTest(carries=carries):
                value, got_key, end = handoffs.excess_density(carries)
                self.assertEqual(got_key, key)
                self.assertEqual(end, "max")
                self.assertEqual(value, contracts.param_range(key)[1])
                self.assertGreater(value, contracts.param(key),
                                   "the max of the band is above the central value")

    def test_each_particle_kind_settles_at_its_own_rate(self):
        """One shared density used to make cells, capsules and beads settle identically."""
        speeds = {kind: handoffs.settling_velocity_m_s(70.0, kind, 37.0)[0]
                  for kind in ("capsules", "cells", "beads")}
        self.assertLess(speeds["capsules"], speeds["cells"])
        self.assertLess(speeds["cells"], speeds["beads"])
        self.assertGreater(speeds["beads"] / speeds["capsules"], 10,
                           "magnetic beads settle in seconds, which is why the bead "
                           "chamber uses a magnet and not flow")

    def test_the_settling_risk_records_the_end_of_the_band_it_used(self):
        got = self.capsule_handoff()
        settling = next(r for r in got.data["risks"] if "sedimentation" in r)
        self.assertIn("capsule.excess_density_kg_m3", settling)
        self.assertIn("max of its band", settling)
        self.assertIn(handoffs.CARRIER_DENSITY_KEY, settling)
        gap = next(g for g in got.gaps
                   if g["field"] == "capsule.excess_density_kg_m3")
        self.assertIn("LOST", gap["question"])
        self.assertIn("max of the band", gap["assumed_from"])

    def test_the_line_comes_from_the_tubing_keys_not_from_this_module(self):
        line = handoffs.line_for(None)
        self.assertEqual(line.bore_mm, contracts.param("tubing.inner_diameter_mm"))
        self.assertEqual(line.length_mm, contracts.param("tubing.length_mm"))
        self.assertEqual(line.valve_ul, contracts.param("selector_valve.internal_volume_ul"))
        self.assertFalse(hasattr(handoffs, "DENSITY_DIFFERENCE_KG_M3"),
                         "the local density table must be gone, not shadowed")
        self.assertFalse(hasattr(handoffs, "LINE_LENGTH_MM"))

    def test_dead_volume_is_the_tube_plus_the_valve(self):
        line = handoffs.line_for(None)
        tube = 3.141592653589793 / 4 * line.bore_mm ** 2 * line.length_mm
        self.assertAlmostEqual(line.dead_volume_ul,
                               tube + contracts.param("selector_valve.internal_volume_ul"),
                               places=9)
        self.assertGreater(line.dead_volume_ul, tube,
                           "the selector valve's swept volume is part of the loss")

    def test_a_particle_too_big_for_the_contract_tubing_widens_the_bore_and_says_so(self):
        bore = contracts.param("tubing.inner_diameter_mm")
        factor = contracts.param("capsule_channel.width_factor_low")
        too_big_um = (bore / factor) * 1000.0 * 2
        line = handoffs.line_for(too_big_um)
        self.assertTrue(line.widened)
        self.assertGreater(line.bore_mm, bore)
        got = handoffs.build(
            "c1", "outlet_1", "c2", "inlet_1",
            ops_of(op(1, "transfer", "trap_chamber", volume_ul=40.0,
                      particles="capsules", diameter_um=too_big_um)),
            ops_of(op(2, "load", "trap_chamber", volume_ul=40.0)))
        self.assertTrue(any(g["field"] == "tubing.inner_diameter_mm"
                            and "which tubing is actually used" in g["question"]
                            for g in got.gaps))

    def test_the_dead_volume_risk_names_the_tubing_keys(self):
        got = self.capsule_handoff(release=5.0, draw=5.0)
        dead = next(r for r in got.data["risks"] if "dead volume" in r)
        self.assertIn("tubing.inner_diameter_mm", dead)
        self.assertIn("tubing.length_mm", dead)
        self.assertIn("selector_valve.internal_volume_ul", dead)

    def test_every_assumed_contract_value_is_recorded_with_its_key(self):
        got = self.capsule_handoff()
        for key in ("tubing.inner_diameter_mm", "tubing.length_mm",
                    "selector_valve.internal_volume_ul"):
            with self.subTest(key=key):
                self.assertTrue(contracts.is_assumption(key))
                gap = next(g for g in got.gaps if g["field"] == key)
                self.assertEqual(gap["assumed_from"], key)
                self.assertEqual(gap["assumed"], contracts.param(key))

    def test_every_risk_is_a_computed_sentence_with_a_number_in_it(self):
        got = self.capsule_handoff(release=76.9, draw=9.6)
        self.assertTrue(got.data["risks"])
        for risk in got.data["risks"]:
            self.assertRegex(risk, r"\d", "generic risk with no number: %r" % risk)


# --------------------------------------------------------------------------------------
# the stage
# --------------------------------------------------------------------------------------

class Stage(unittest.TestCase):

    def test_a_pending_placement_is_refused_and_the_ops_are_named(self):
        payload = unitops_of(
            op(1, "wash", "trap_chamber"),
            op(2, "lyse", None, where="pending", why="S2 has not decided"),
            op(3, "collect", None, where="pending", why="S2 has not decided"),
        )
        with self.assertRaises(ops_mod.PendingPlacement) as caught:
            s4_partition.plan_for("t", payload)
        message = str(caught.exception)
        self.assertIn("op 2", message)
        self.assertIn("op 3", message)
        self.assertIn("S3", message)

    def test_the_cli_exits_2_on_a_pending_placement(self):
        with tempfile.TemporaryDirectory() as folder:
            original = stage_io.WORK
            try:
                stage_io.WORK = Path(folder)
                stage_io.write_stage("pend", "unitops", unitops_of(
                    op(1, "wash", "trap_chamber"),
                    op(2, "lyse", None, where="pending", why="not decided"),
                    assay_id="pend"))
                self.assertEqual(s4_partition.main(["--id", "pend"]), 2)
            finally:
                stage_io.WORK = original

    def test_output_matches_the_partition_schema(self):
        payload = s4_partition.run(assay_id="t", unitops=self.simple(), write=False)
        self.assertEqual(stage_io.problems("partition", payload), [])

    def test_it_writes_a_validated_file_through_lib_io(self):
        with tempfile.TemporaryDirectory() as folder:
            original = stage_io.WORK
            try:
                stage_io.WORK = Path(folder)
                payload = s4_partition.run(assay_id="t", unitops=self.simple())
                path = Path(folder) / "t" / "partition.json"
                self.assertTrue(path.exists())
                self.assertEqual(json.loads(path.read_text()), payload)
            finally:
                stage_io.WORK = original

    def test_it_is_deterministic(self):
        first = s4_partition.run(assay_id="t", unitops=self.simple(), write=False)
        second = s4_partition.run(assay_id="t", unitops=self.simple(), write=False)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_every_on_chip_op_lands_on_exactly_one_chip(self):
        payload = s4_partition.run(assay_id="mvp", unitops=mvp_chain.unitops("mvp"),
                                   write=False)
        placed = [n for chip in payload["chips"] for n in chip["ops"]]
        self.assertEqual(len(placed), len(set(placed)), "an op is on two chips")
        expected = sorted(entry["op"] for entry in mvp_chain.unitops("mvp")["ops"]
                          if entry["placement"]["where"] == "on_chip")
        self.assertEqual(sorted(placed), expected)

    def test_no_off_chip_op_is_on_a_chip(self):
        source = mvp_chain.unitops("mvp")
        payload = s4_partition.run(assay_id="mvp", unitops=source, write=False)
        placed = {n for chip in payload["chips"] for n in chip["ops"]}
        for entry in source["ops"]:
            if entry["placement"]["where"] != "on_chip":
                self.assertNotIn(entry["op"], placed)

    def test_every_cut_carries_a_reason_and_a_constraint(self):
        payload = s4_partition.run(assay_id="mvp", unitops=mvp_chain.unitops("mvp"),
                                   write=False)
        self.assertTrue(payload["cuts"])
        allowed = set(stage_io.schemas()["partition"]["properties"]["cuts"]["items"]
                      ["properties"]["forced_by"]["enum"])
        for cut in payload["cuts"]:
            self.assertTrue(cut["why"])
            self.assertIn(cut.get("forced_by"), allowed)
            self.assertIn("constraint:", cut.get("note", ""))

    def test_the_headline_manual_handoff_count_is_in_the_note(self):
        payload = s4_partition.run(assay_id="mvp", unitops=mvp_chain.unitops("mvp"),
                                   write=False)
        manual = sum(1 for h in payload["handoffs"] if not h["automated"])
        self.assertIn("%d of them still needing a human" % manual, payload["note"])

    def test_a_chain_with_no_on_chip_op_does_not_pretend_to_build_a_chip(self):
        payload = unitops_of(
            op(1, "image", None, where="off_chip", why="done on a bench instrument"),
            op(2, "separate", None, where="missing_part", why="no block does it",
               need="a size-selection block"))
        plan = s4_partition.plan_for("t", payload)
        self.assertEqual(plan.chips, [])

    def test_a_missing_part_op_is_a_cut_that_says_so(self):
        payload = unitops_of(
            op(1, "wash", "trap_chamber"),
            op(2, "separate", None, where="missing_part",
               why="no block can size-select at this floor",
               need="a size-selection block"),
            op(3, "collect", "polygon_chamber"))
        result = s4_partition.run(assay_id="t", unitops=payload, write=False)
        self.assertEqual(len(result["chips"]), 2)
        self.assertEqual([c["forced_by"] for c in result["cuts"]], ["missing_part"])
        self.assertIn("size-selection block", result["cuts"][0]["why"])

    def test_an_unbuildable_single_op_is_kept_and_reported_not_dropped(self):
        """A board no envelope satisfies still has to appear, with the problem recorded."""
        huge = board_filling_load("target") * 2
        payload = unitops_of(op(1, "incubate", "polygon_chamber",
                                load=held(packed_ul=huge)))
        result = s4_partition.run(assay_id="t", unitops=payload, write=False)
        self.assertEqual(len(result["chips"]), 1)
        self.assertEqual(result["chips"][0]["ops"], [1])
        self.assertIn("UNSATISFIED", result["chips"][0]["note"])
        self.assertTrue(any("cannot be built as planned" in u["question"]
                            for u in result["unresolved"]))

    def test_unresolved_is_carried_forward_and_added_to(self):
        source = mvp_chain.unitops("mvp")
        payload = s4_partition.run(assay_id="mvp", unitops=source, write=False)
        carried = {entry["question"] for entry in source["unresolved"]}
        got = {entry["question"] for entry in payload["unresolved"]}
        self.assertTrue(carried <= got, "unitops unresolved entries must survive S4")
        self.assertGreater(len(got), len(carried))
        for entry in payload["unresolved"]:
            if "assumed" in entry:
                self.assertIn("assumed_from", entry,
                              "a value used in place of a real one must name its source")

    def test_a_block_id_outside_the_library_is_reported(self):
        payload = unitops_of(op(1, "wash", "not_a_real_block"))
        result = s4_partition.run(assay_id="t", unitops=payload, write=False)
        self.assertTrue(any("not_a_real_block" in u["question"]
                            for u in result["unresolved"]))

    def test_the_stage_exposes_the_interface_run_py_expects(self):
        self.assertTrue(callable(s4_partition.run))
        self.assertIsInstance(s4_partition.WRITES, list)
        self.assertEqual(s4_partition.WRITES, [],
                         "S4 writes only inside assay2chip/work/<id>/")

    def test_llm_provenance_says_no_model_was_involved(self):
        payload = s4_partition.run(assay_id="t", unitops=self.simple(), write=False)
        self.assertEqual(payload["llm"]["mode"], "none")
        self.assertEqual(payload["llm"]["calls"], 0)

    @staticmethod
    def simple():
        capsule = contracts.capsule_diameter_um()
        return unitops_of(
            op(1, "load", "trap_chamber", particles="capsules", diameter_um=capsule),
            op(2, "wash", "trap_chamber", particles="capsules", diameter_um=capsule),
            op(3, "transfer", "trap_chamber", particles="capsules", diameter_um=capsule),
        )


# --------------------------------------------------------------------------------------
# nothing is hard-coded: change a contract, the answer changes
# --------------------------------------------------------------------------------------

class ReadsTheContracts(unittest.TestCase):
    """The repo has been bitten by copied contract values; this catches a new one."""

    def setUp(self):
        self.blocks = json.loads(json.dumps(contracts.blocks()))
        self.parameters = json.loads(json.dumps(contracts.parameters()))

    def tearDown(self):
        contracts._cache["blocks"] = self.blocks
        contracts._cache["parameters"] = self.parameters

    def test_lowering_max_ports_per_chip_forces_more_cuts(self):
        """Four washes on four parts need 7 ports; the limit decides how they split."""
        entries = [op(i + 1, "wash", PARTS[i]) for i in range(4)]
        before = segment.partition("t", ops_of(*entries), "demo")
        self.assertEqual(len(before.chips), 2)
        patched = json.loads(json.dumps(self.blocks))
        patched["fabrication_floors"]["max_ports_per_chip"] = 4
        contracts._cache["blocks"] = patched
        after = segment.partition("t", ops_of(*entries), "demo")
        self.assertEqual(len(after.chips), 4,
                         "at a 4-port limit each wash needs its own board")
        self.assertGreater(len(after.chips), len(before.chips),
                           "the port limit is read from the contract, not written in code")
        self.assertFalse(after.infeasible,
                         "and the result still satisfies the patched limit")

    def test_shrinking_the_demo_envelope_forces_target_scale(self):
        entries = [op(1, "incubate", "polygon_chamber", volume_ul=20.0)]
        self.assertEqual(constraints.feasible(ops_of(*entries)).scale, "demo")
        patched = json.loads(json.dumps(self.blocks))
        patched["envelope"]["max_x_mm"] = 3
        patched["envelope"]["max_y_mm"] = 2
        contracts._cache["blocks"] = patched
        self.assertEqual(constraints.feasible(ops_of(*entries)).scale, "target")

    def test_changing_the_swell_factor_changes_the_line_sizing(self):
        capsule = contracts.capsule_diameter_um()
        entries = ops_of(op(1, "transfer", "trap_chamber", volume_ul=40.0,
                            particles="capsules", diameter_um=capsule))
        before = handoffs.build("c1", "outlet_1", "c2", "inlet_1", entries, entries)
        patched = json.loads(json.dumps(self.parameters))
        patched["capsule.swell_factor"]["value"] = \
            self.parameters["capsule.swell_factor"]["value"] * 2
        contracts._cache["parameters"] = patched
        after = handoffs.build("c1", "outlet_1", "c2", "inlet_1",
                               ops_of(op(1, "transfer", "trap_chamber", volume_ul=40.0,
                                         particles="capsules", diameter_um=capsule)),
                               entries)
        self.assertGreater(after.data["particle_um"], before.data["particle_um"])

    def test_lengthening_the_tubing_increases_the_dead_volume(self):
        before = handoffs.line_for(None).dead_volume_ul
        patched = json.loads(json.dumps(self.parameters))
        patched["tubing.length_mm"]["value"] = \
            self.parameters["tubing.length_mm"]["value"] * 3
        contracts._cache["parameters"] = patched
        after = handoffs.line_for(None).dead_volume_ul
        self.assertGreater(after, before)
        self.assertAlmostEqual(
            after - contracts.param("selector_valve.internal_volume_ul"),
            3 * (before - contracts.param("selector_valve.internal_volume_ul")),
            places=6)

    def test_widening_the_density_band_changes_the_settling_estimate(self):
        before, _ = handoffs.settling_velocity_m_s(70.0, "capsules", 37.0)
        patched = json.loads(json.dumps(self.parameters))
        patched["capsule.excess_density_kg_m3"]["max"] = \
            self.parameters["capsule.excess_density_kg_m3"]["max"] * 4
        contracts._cache["parameters"] = patched
        after, _ = handoffs.settling_velocity_m_s(70.0, "capsules", 37.0)
        self.assertAlmostEqual(after / before, 4.0, places=6,
                               msg="settling is linear in the excess density, read from "
                                   "the contract band rather than from code")

    def test_the_37c_viscosity_is_the_one_used_for_warm_work(self):
        self.assertEqual(contracts.viscosity_for(37.0).key, contracts.VISCOSITY_37C_KEY)
        self.assertEqual(contracts.viscosity_for(42.0).key, contracts.VISCOSITY_37C_KEY)
        self.assertNotEqual(contracts.viscosity_for(37.0).pa_s,
                            contracts.viscosity_for(20.0).pa_s)

    def test_no_contract_key_this_stage_needs_is_missing(self):
        self.assertEqual(contracts.missing_keys(), [])
        for key in ("culture.temp_c", "rt.temp_c", "pcr.max_temp_c",
                    "control.error_threshold", "sublibraries.count",
                    "capsule_channel.width_factor_low", "gelation.temp_c"):
            with self.subTest(key=key):
                self.assertIsNotNone(contracts.param(key))


# --------------------------------------------------------------------------------------
# the ground truth: the repo's own MVP chain
# --------------------------------------------------------------------------------------

class GroundTruth(unittest.TestCase):
    """S4 against the boards people built by hand (protocols/pipeline_mvp.json).

    The hand-built chain is six chips, all 30 x 20 mm, with four joins the repo itself
    marks OFF-CHIP: the spin and strainer before encapsulation, breaking the emulsion and
    curing, the split-and-pool barcoding kit, and the pooling, SPRI and QC at the end.
    """

    @classmethod
    def setUpClass(cls):
        cls.source = mvp_chain.unitops("mvp_chain")
        cls.demo = s4_partition.run(assay_id="mvp_chain", unitops=cls.source,
                                    scale="demo", write=False)
        cls.free = s4_partition.run(assay_id="mvp_chain", unitops=cls.source, write=False)

    def test_at_demo_scale_it_lands_on_the_same_six_chips(self):
        self.assertEqual(len(self.demo["chips"]), mvp_chain.GROUND_TRUTH["chips"])
        self.assertTrue(all(chip["scale"] == "demo" for chip in self.demo["chips"]),
                        "every chip in chip/ is 30 x 20 mm")

    def test_the_four_module_boundaries_that_are_not_in_dispute_match_exactly(self):
        """Encapsulation, module3, module4 and module5 come out as their own boards."""
        grouped = {chip["chip_id"]: chip["ops"] for chip in self.demo["chips"]}
        found = list(grouped.values())
        self.assertIn([8], found, "encapsulation is its own board (spc_demo_v1)")
        self.assertIn([10, 11, 12, 13, 14, 15], found,
                      "load, lysis, washes, RT and release are one trap "
                      "(module3_v1_translated)")
        self.assertIn([17, 18], found, "the split and the index PCR are one board "
                                       "(module4_v1)")
        self.assertIn([19, 20, 21], found, "release and bead clean-up are one board "
                                           "(module5_v1)")

    def test_the_culture_and_cell_prep_boundary_differs_and_the_difference_is_justified(self):
        """Two boards cover ops 1-6, as in the repo, but the cut sits one op later.

        The hand-built chain cuts at the module1 / cell_prep_v1 join. S4 cuts where the
        port budget runs out, which is after the dissociation hold. Both give two boards
        and one automated handoff, and the repo's own notes say its boundary is wrong
        there: pipeline_status.md lists join 1 -> 2 as broken because "Module 1 already
        lifts and quenches cells. cell_prep_v1 steps 1-3 expect an adherent layer and
        dissociate again". A cut that does not duplicate the dissociation is the better
        of the two.
        """
        covering = [chip for chip in self.demo["chips"]
                    if set(chip["ops"]) & {1, 2, 3, 4, 5, 6}]
        self.assertEqual(len(covering), 2)
        self.assertEqual(sorted(n for chip in covering for n in chip["ops"]),
                         [1, 2, 3, 4, 5, 6])
        cut = next(c for c in self.demo["cuts"] if c["between"][0] in (2, 4))
        self.assertEqual(cut["forced_by"], "ports")

    def test_the_port_counts_match_the_chips_as_built(self):
        by_ops = {tuple(chip["ops"]): chip for chip in self.demo["chips"]}
        expected = {
            (8,): ("spc_demo_v1", 4),
            (10, 11, 12, 13, 14, 15): ("module3_v1_translated", 6),
            (19, 20, 21): ("module5_v1", 5),
        }
        for key, (chip_name, count) in expected.items():
            with self.subTest(chip=chip_name):
                self.assertEqual(by_ops[key]["ports"], count)
                self.assertEqual(mvp_chain.GROUND_TRUTH["ports"][chip_name], count)

    def test_module4_comes_out_one_port_bigger_and_that_port_is_a_known_bug(self):
        """module4_v1 has 5 ports; S4 says 6, and the 6th is the outlet it is missing.

        pipeline_status.md: "Module4 has no capsule outlet: in_s1-in_s4 are inlets,
        p_vent blocks capsules. Port stays TBC." S4 counting a product outlet is the
        right answer, not a disagreement.
        """
        chip = next(c for c in self.demo["chips"] if c["ops"] == [17, 18])
        self.assertEqual(chip["ports"], mvp_chain.GROUND_TRUTH["ports"]["module4_v1"] + 1)
        self.assertIn("outlet_1", chip["note"])

    def test_it_finds_the_same_four_joins_that_still_need_a_human(self):
        manual = [h for h in self.demo["handoffs"] if not h["automated"]]
        self.assertEqual(len(manual), len(mvp_chain.GROUND_TRUTH["off_chip"]))
        self.assertEqual(len(manual), 4)

    def test_it_catches_the_broken_join_the_repo_documents(self):
        """module3 releases 76.9 uL and module4 draws 9.6 uL, with no step between."""
        mismatch = [h for h in self.demo["handoffs"]
                    if any("volume mismatch" in r for r in h.get("risks", []))]
        self.assertEqual(len(mismatch), 1)
        self.assertEqual(mismatch[0]["volume_ul"], 76.9)
        self.assertFalse(mismatch[0]["automated"])

    def test_the_98c_pcr_is_never_on_a_board_with_live_cells_or_a_held_setpoint(self):
        for payload in (self.demo, self.free):
            cycled = next(chip for chip in payload["chips"] if 18 in chip["ops"])
            self.assertNotIn(1, cycled["ops"], "culture on the 98 C board")
            self.assertNotIn(2, cycled["ops"])
            if payload is self.demo:
                self.assertEqual(cycled["ops"], [17, 18])

    def test_the_capsule_trap_board_is_a_polymer_that_tolerates_the_lysate(self):
        chip = next(c for c in self.demo["chips"]
                    if c["ops"] == [10, 11, 12, 13, 14, 15])
        self.assertIn("material", chip["note"])
        material = thermal.choose_material(
            [o for o in ops_mod.load_ops(self.source) if o.number in range(10, 16)])
        entry = thermal.materials()[material.material_id]
        compat = entry["chemical_compatibility"]
        self.assertEqual(compat.get("guanidine_thiocyanate"), "compatible",
                         "the lysis board must not be a polymer with a TODO against GITC")

    def test_left_free_to_choose_a_scale_it_still_picks_demo_throughout(self):
        """Sizing from `load` means this chain needs nothing bigger than 30 x 20 mm.

        Before the `load` field existed the area came from the paper's tube volumes, and
        this chain came out as four boards, two of them at ANSI/SLAS scale, which S6
        could not build. Now the free-scale answer and the demo-pinned answer agree.
        """
        self.assertTrue(all(chip["scale"] == "demo" for chip in self.free["chips"]),
                        [(c["chip_id"], c["scale"]) for c in self.free["chips"]])
        self.assertEqual([c["ops"] for c in self.free["chips"]],
                         [c["ops"] for c in self.demo["chips"]])
        demo_manual = sum(1 for h in self.demo["handoffs"] if not h["automated"])
        free_manual = sum(1 for h in self.free["handoffs"] if not h["automated"])
        self.assertEqual(free_manual, demo_manual)

    def test_every_board_is_well_inside_the_demo_envelope(self):
        limit = contracts.envelope("demo").area_mm2
        for chip in self.demo["chips"]:
            with self.subTest(chip=chip["chip_id"]):
                self.assertLess(chip["area_mm2"], limit)

    def test_both_plans_are_schema_valid_and_internally_consistent(self):
        for payload in (self.demo, self.free):
            self.assertEqual(stage_io.problems("partition", payload), [])
            chip_ids = {chip["chip_id"] for chip in payload["chips"]}
            for handoff in payload["handoffs"]:
                ends = {handoff["from"], handoff["to"]}
                self.assertTrue(ends & chip_ids, "a handoff joins no chip: %s" % ends)
                for end in ends - chip_ids:
                    self.assertTrue(end.endswith("_bench"),
                                    "%s is neither a chip nor the bench" % end)
            # One cut per chip boundary.
            self.assertEqual(len(payload["cuts"]), len(payload["chips"]) - 1)

    def test_the_chain_fixture_really_is_the_repos_chain(self):
        """Guard the fixture itself: it must stay a valid unitops file."""
        self.assertEqual(stage_io.problems("unitops", self.source), [])
        kinds = {entry["kind"] for entry in self.source["ops"]}
        self.assertIn("culture", kinds)
        self.assertIn("encapsulate", kinds)
        self.assertIn("thermocycle", kinds)
        self.assertIn("transfer", kinds)
        off_chip = [e for e in self.source["ops"]
                    if e["placement"]["where"] == "off_chip"]
        self.assertEqual(len(off_chip), 5,
                         "the spin/strainer, the cure, the barcoding kit, SPRI and QC")


if __name__ == "__main__":
    unittest.main()

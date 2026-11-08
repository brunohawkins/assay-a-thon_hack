"""S7 route: planar channels on ONE fluidic layer, no crossings, ever.

    python -m unittest tests.test_assay2chip_route -v

Offline: no model, no network, nothing written outside a temp directory. Three hand-built
placed chips carry most of the weight:

  feed_chip    one inlet, a header, three lanes at different distances, a collector and an
               outlet. Routes in one pass, and the three lane feeds carry a balance group,
               so it is also the length-matching case.
  pinch_chip   THE TIGHT ONE. A divider with two gaps: 1.3 mm and 1.1 mm. A 0.22 mm buffer
               line fits either; a 0.42 mm capsule line fits only the 1.3 mm gap. The
               buffer line is shorter, so it is routed first and takes the wide gap - which
               leaves the capsule line nowhere to go. Routing is only possible if the
               router rips the buffer line up and sends it through the narrow gap. The test
               asserts the rip-up HAPPENED, not merely that the chip came out planar.
  crossed_chip a chip with a deliberate crossing, to prove the checker fails it.

The last group of tests is the cross-check the brief demands: whether cad/make_train_cad.py
and cad/make_figures.py read a channel's `path` in THIS worktree, and that S7 says so
loudly when they do not.
"""

from __future__ import annotations

import json
import math
import random
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "operator", ROOT / "tools", ROOT / "cad"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

import check_chip_spec as ccs                              # noqa: E402
import make_figures                                        # noqa: E402
import schema_io                                           # noqa: E402

from assay2chip.lib import contracts                       # noqa: E402
from assay2chip.route import connections as conn           # noqa: E402
from assay2chip.route import geometry as geo               # noqa: E402
from assay2chip.route import meander, obstacles, planarity, router   # noqa: E402
from assay2chip.stages import s7_route                     # noqa: E402

MIN_WALL = contracts.floor("min_wall_mm")
MIN_OPEN = contracts.floor("min_open_channel_mm")
MIN_PORT = contracts.floor("min_port_mm")
WIDTH_FACTOR = float(contracts.param("capsule_channel.width_factor_low"))
SWELL = contracts.capsule_swell_factor()


# --- fixtures ---------------------------------------------------------------------------

def layers():
    return [{"id": "base", "material": "acrylic", "thickness_mm": 1.0},
            {"id": "fluidic", "material": "acrylic", "thickness_mm": 0.3},
            {"id": "lid", "material": "acrylic", "thickness_mm": 1.0}]


def chamber(cid, cx, cy, sx, sy, ctype="well", block="polygon_chamber"):
    return {"id": cid, "type": ctype, "block": block, "center": [cx, cy, 1.15],
            "size": [sx, sy, 0.3], "volume_ul": round(sx * sy * 0.3, 4)}


def port(pid, x, y, ptype="inlet", **kw):
    out = {"id": pid, "type": ptype, "position": [x, y, 2.3]}
    out.update(kw)
    return out


def chip(cid, chambers, ports, channels=()):
    return {"id": cid, "name": f"S7 test fixture {cid}", "placeholder": True, "units": "mm",
            "scale": "demo", "footprint": {"x": 30, "y": 20}, "layers": layers(),
            "chambers": list(chambers), "channels": list(channels), "ports": list(ports)}


def feed_chip():
    """One feed, a header, three parallel lanes STAGGERED IN X, one drain port each.

    The stagger is the point. lane_2 sits 3 mm further from the header than lanes 1 and 3,
    so the three feeds come out 13.5, 10.5 and 10.5 mm long, and at one width that is a
    29% resistance spread - the near lanes would take more than their share of the flow.
    The three feeds are therefore a balance group, and the only lever left (width is fixed
    by what they carry, depth by the one etch depth) is length. This is the case the
    meander exists for.
    """
    return chip("s7_feed_fixture", [
        chamber("header", 5.5, 10.0, 2.0, 12.0),                        # x 4.5 .. 6.5
        chamber("lane_1", 20.0, 15.5, 6.0, 3.0, "culture", "culture_chamber"),   # x 17 .. 23
        chamber("lane_2", 23.0, 10.0, 6.0, 3.0, "culture", "culture_chamber"),   # x 20 .. 26
        chamber("lane_3", 20.0, 4.5, 6.0, 3.0, "culture", "culture_chamber"),    # x 17 .. 23
    ], [port("feed", 2.0, 10.0, "inlet", role="medium"),
        port("drain_1", 28.0, 15.5, "outlet", role="harvest"),
        port("drain_2", 28.0, 10.0, "outlet", role="harvest"),
        port("drain_3", 28.0, 4.5, "outlet", role="harvest")])


def feed_connections():
    out = [{"id": "c_feed", "from": "feed", "to": "header", "carries": "liquid"}]
    for n, lane in enumerate(("lane_1", "lane_2", "lane_3"), start=1):
        out.append({"id": f"c_to_{lane}", "from": {"part": "header"}, "to": {"part": lane},
                    "carries": "liquid", "balance_group": "lane_feeds",
                    "note": "balanced feed into a parallel chamber"})
        out.append({"id": f"c_from_{lane}", "from": lane, "to": f"drain_{n}",
                    "carries": "liquid"})
    return out


def feed_chip_with_stub_channels():
    """The feed chip as S6 might hand it over with channels named but not drawn.

    from / to / width / depth and no `path`: exactly what connections.from_chip() reads
    when there is no connections file.
    """
    spec = feed_chip()
    spec["channels"] = [
        {"id": c["id"], "from": c["from"] if isinstance(c["from"], str) else c["from"]["part"],
         "to": c["to"] if isinstance(c["to"], str) else c["to"]["part"],
         "width_mm": MIN_OPEN, "depth_mm": 0.3, "carries": c["carries"],
         "block": "straight_channel"}
        for c in feed_connections()]
    return spec


def pinch_chip():
    """THE TIGHT ONE. A divider with a 1.3 mm gap and a 1.1 mm gap, and nothing else.

    A 0.22 mm line needs 0.22 + 2 x 0.4 = 1.02 mm of gap; a 0.42 mm capsule line needs
    1.22 mm. So the wide gap takes either and the narrow gap takes only the thin one. The
    thin line is also the shorter net, so it is routed first and grabs the wide gap. The
    board is routable, but ONLY the other way round.
    """
    return chip("s7_pinch_fixture", [
        chamber("div_s", 15.0, 3.70, 1.0, 7.40),     # y 0.00 .. 7.40
        chamber("div_m", 15.0, 10.80, 1.0, 4.20),    # y 8.70 .. 12.90   gap 7.40-8.70 = 1.30
        chamber("div_n", 15.0, 17.00, 1.0, 6.00),    # y 14.00 .. 20.00  gap 12.90-14.00 = 1.10
    ], [port("buf_in", 9.0, 8.05, "inlet", role="reagent"),
        port("buf_out", 21.0, 8.05, "outlet", role="waste"),
        port("caps_in", 4.0, 2.50, "inlet", role="load", capsule_passable=True,
             width_um=MIN_PORT * 1000),
        port("caps_out", 26.0, 2.50, "outlet", role="transfer", capsule_passable=True,
             width_um=MIN_PORT * 1000)])


def pinch_connections():
    return [
        # Short span (12 mm), so the router reaches it first. Straight through the wide gap.
        {"id": "c_buffer", "from": "buf_in", "to": "buf_out", "carries": "liquid",
         "note": "buffer line; fits either gap"},
        # Long span (23.5 mm) and 0.42 mm wide, so ONLY the 1.3 mm gap will take it.
        {"id": "c_capsules", "from": "caps_in", "to": "caps_out", "carries": "capsules",
         "particle_um": 140, "note": "capsule line; only the wide gap is wide enough"},
    ]


def crossed_chip():
    """Two channels drawn transversally across each other: what S7 must never emit.

    They cross once, at (12, 15), properly inside both polylines. Note the crossing has to
    be TRANSVERSAL to count: two channels that share a collinear run are a different fault
    (overlapping, caught by the clearance rule), and `proper_intersection` returns nothing
    for parallel segments - which is exactly what the first version of this fixture did.
    """
    c = chip("s7_crossed_fixture", [
        chamber("nw", 6.0, 15.0, 3.0, 3.0), chamber("se", 24.0, 5.0, 3.0, 3.0),
        chamber("sw", 6.0, 5.0, 3.0, 3.0), chamber("ne", 24.0, 18.0, 3.0, 3.0),
    ], [])
    c["channels"] = [
        {"id": "ch_a", "from": "nw", "to": "se", "width_mm": 0.3, "depth_mm": 0.3,
         "block": "channel_corner", "carries": "liquid",
         "path": [[7.5, 15.0], [15.0, 15.0], [15.0, 5.0], [22.5, 5.0]]},
        {"id": "ch_b", "from": "sw", "to": "ne", "width_mm": 0.3, "depth_mm": 0.3,
         "block": "channel_corner", "carries": "liquid",
         "path": [[7.5, 5.0], [12.0, 5.0], [12.0, 18.0], [22.5, 18.0]]},
    ]
    return c


def crosses_line_x(path, x):
    """Every y where a polyline crosses the vertical line x = `x`.

    Asking which gap a route went through by looking for a VERTEX inside the gap is wrong:
    a channel crossing a 1 mm throat in one straight segment has no vertex there at all.
    """
    out = []
    for (x0, y0), (x1, y1) in zip(path, path[1:]):
        if (x0 - x) * (x1 - x) <= 0 and abs(x1 - x0) > 1e-12:
            out.append(y0 + (y1 - y0) * (x - x0) / (x1 - x0))
        elif abs(x0 - x) < 1e-12 and abs(x1 - x) < 1e-12:
            out += [y0, y1]
    return out


def route(spec, connection_dicts, **kw):
    nets = conn.normalise({"connections": list(connection_dicts)})
    return s7_route.route_chip(spec, nets, connections_source="the test fixture", **kw)


# --- 1. the geometry agrees with the tools that will judge the chip ---------------------

class GeometryAgreesWithTheRepo(unittest.TestCase):
    """assay2chip/route/geometry.py restates cad/make_figures.py's segment maths.

    It is written out there rather than imported, so this test is what stops the two
    drifting. A drift would mean S7 passes a chip that make_figures fails, or the reverse.
    """

    def test_point_to_segment_matches_make_figures(self):
        rng = random.Random(7)
        for _ in range(400):
            p = (rng.uniform(0, 30), rng.uniform(0, 20))
            a = (rng.uniform(0, 30), rng.uniform(0, 20))
            b = (rng.uniform(0, 30), rng.uniform(0, 20))
            self.assertAlmostEqual(geo.dist_point_seg(p, a, b),
                                   make_figures._point_seg(p, a, b), places=9)

    def test_segment_to_segment_matches_make_figures(self):
        rng = random.Random(11)
        for _ in range(400):
            pts = [(rng.uniform(0, 30), rng.uniform(0, 20)) for _ in range(4)]
            self.assertAlmostEqual(geo.dist_seg_seg(*pts),
                                   make_figures._seg_seg(*pts), places=9)

    def test_segment_to_rect_matches_make_figures(self):
        rng = random.Random(13)
        for _ in range(300):
            a = (rng.uniform(0, 30), rng.uniform(0, 20))
            b = (rng.uniform(0, 30), rng.uniform(0, 20))
            cx, cy = rng.uniform(4, 26), rng.uniform(4, 16)
            c = {"center": [cx, cy, 0], "size": [rng.uniform(1, 6), rng.uniform(1, 6), 0.3]}
            rect = make_figures._chamber_rect(c)
            self.assertAlmostEqual(geo.dist_seg_rect(a, b, rect),
                                   make_figures._seg_rect(a, b, rect), places=9)

    def test_crossing_matches_check_chip_spec(self):
        rng = random.Random(17)
        for _ in range(400):
            pts = [(rng.uniform(0, 30), rng.uniform(0, 20)) for _ in range(4)]
            mine = geo.proper_intersection(*pts)
            theirs = ccs._intersect(*pts)
            self.assertEqual(mine is None, theirs is None)
            if mine is not None:
                self.assertAlmostEqual(mine[0], theirs[0], places=3)
                self.assertAlmostEqual(mine[1], theirs[1], places=3)

    def test_simplify_keeps_only_real_corners(self):
        path = [(0, 0), (1, 0), (2, 0), (2, 1), (2, 2), (3, 2)]
        self.assertEqual(geo.simplify(path), [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (3.0, 2.0)])
        self.assertEqual(geo.corners(path), 2)


# --- 2. contract values are read, never written down -----------------------------------

class NoHardCodedContractValues(unittest.TestCase):
    def test_every_floor_comes_from_the_contract(self):
        r = router.Router(feed_chip(), conn.normalise({"connections": feed_connections()}))
        self.assertEqual(r.min_wall, contracts.floor("min_wall_mm"))
        self.assertEqual(r.min_open, contracts.floor("min_open_channel_mm"))
        self.assertEqual(r.width_factor, contracts.param("capsule_channel.width_factor_low"))
        self.assertEqual(r.swell, contracts.capsule_swell_factor())
        self.assertLessEqual(r.depth_mm, contracts.max_depth_mm())

    def test_the_source_files_contain_no_contract_literals(self):
        """A literal that duplicates a contract value is the bug this repo keeps hitting."""
        banned = {"0.4": "min_wall_mm", "0.8": "min_port_mm", "1.5": "width_factor_low"}
        for path in sorted((ROOT / "assay2chip" / "route").glob("*.py")):
            src = path.read_text()
            for line_no, line in enumerate(src.splitlines(), start=1):
                code = line.split("#", 1)[0]
                if '"""' in line or line.strip().startswith(("#", "*")):
                    continue
                for literal, name in banned.items():
                    # The literal has to END there: `1.5e-4` is a tolerance in millimetres,
                    # not the capsule width factor, and matching it as a substring is how
                    # this test first cried wolf.
                    hit = re.search(rf"=\s*{re.escape(literal)}(?![0-9eE.])", code)
                    self.assertIsNone(
                        hit,
                        f"{path.name}:{line_no} assigns the literal {literal}, which is the "
                        f"contract value {name}: read it through assay2chip.lib.contracts")

    def test_a_capsule_line_is_wide_enough_for_the_swollen_capsule(self):
        nets = conn.normalise({"connections": pinch_connections()})
        r = router.Router(pinch_chip(), nets)
        caps = next(n for n in nets if n.id == "c_capsules")
        want = WIDTH_FACTOR * caps.particle_um * SWELL / 1000.0
        self.assertAlmostEqual(r.width_for(caps), want, places=6)
        self.assertGreater(want, WIDTH_FACTOR * caps.particle_um / 1000.0,
                           "the rule must use the SWOLLEN diameter, not the dry one")

    def test_a_liquid_line_is_at_least_the_minimum_open_channel(self):
        nets = conn.normalise({"connections": [
            {"id": "x", "from": "header", "to": "lane_2", "carries": "liquid"}]})
        r = router.Router(feed_chip(), nets)
        self.assertGreaterEqual(r.width_for(nets[0]), MIN_OPEN)


# --- 3. the ordinary case: it routes, and the result is planar -------------------------

class FeedChipRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = route(feed_chip(), feed_connections())
        cls.chip = cls.result["chip"]
        cls.report = cls.result["report"]

    def test_it_routed(self):
        self.assertTrue(self.result["ok"], self.report.get("result"))
        self.assertEqual(len(self.chip["channels"]), len(feed_connections()))

    def test_every_channel_has_an_explicit_path(self):
        self.assertEqual(planarity.routed_paths_only(self.chip), [])
        for ch in self.chip["channels"]:
            self.assertGreaterEqual(len(ch["path"]), 2)
            self.assertTrue(geo.is_rectilinear(ch["path"]), ch["id"])

    def test_the_path_aware_planar_check_is_clean(self):
        conflicts = planarity.check(self.chip)
        self.assertEqual([str(c) for c in planarity.errors(conflicts)], [])

    def test_no_crossings_by_the_repos_own_rule(self):
        self.assertEqual(ccs.crossings(self.chip), [])

    def test_every_block_is_in_the_cad_library(self):
        library = contracts.library()
        for ch in self.chip["channels"]:
            self.assertIn(ch["block"], library, ch["id"])
            self.assertIn(ch["block"],
                          {"straight_channel", "channel_corner", "serpentine_channel"})

    def test_a_straight_channel_really_is_straight(self):
        for ch in self.chip["channels"]:
            if ch["block"] == "straight_channel":
                self.assertEqual(geo.corners(ch["path"]), 0, ch["id"])
            if ch["block"] == "channel_corner":
                self.assertGreater(geo.corners(ch["path"]), 0, ch["id"])

    def test_mouths_land_on_the_part_outline_at_a_right_angle(self):
        feats = obstacles.features_of(self.chip)
        for ch in self.chip["channels"]:
            for end, point, nxt in (("from", ch["path"][0], ch["path"][1]),
                                    ("to", ch["path"][-1], ch["path"][-2])):
                f = feats[ch[end]]
                if f.kind == "port":
                    self.assertLessEqual(geo.dist_point_rect(point, f.rect), 1e-6,
                                         f"{ch['id']} does not reach port {f.id}")
                    continue
                self.assertLessEqual(geo.dist_point_rect(point, f.rect), 1e-6,
                                     f"{ch['id']} {end} is off the outline of {f.id}")
                vertical = min(abs(point[0] - f.rect[0]), abs(point[0] - f.rect[2])) < 1e-6
                self.assertTrue(abs(nxt[1] - point[1]) < 1e-6 if vertical
                                else abs(nxt[0] - point[0]) < 1e-6,
                                f"{ch['id']} meets {f.id} at an angle")

    def test_clearance_to_every_unconnected_feature(self):
        feats = obstacles.features_of(self.chip)
        chans = [(c, c["path"], c["width_mm"]) for c in self.chip["channels"]]
        for ch, path, w in chans:
            for f in feats.values():
                if f.id in (ch["from"], ch["to"]):
                    continue
                gap = (geo.dist_path_rect(path, f.rect) if f.kind == "chamber"
                       else geo.dist_point_path(f.centre, path) - f.port_side_mm / 2) - w / 2
                self.assertGreaterEqual(gap, MIN_WALL - 1e-9,
                                        f"{ch['id']} is {gap:.3f} mm from {f.kind} {f.id}")

    def test_never_over_or_beside_an_unconnected_port(self):
        """The port-overlap rule's other half: a hole may not break into a foreign line."""
        for p in self.chip["ports"]:
            for ch in self.chip["channels"]:
                if p["id"] in (ch["from"], ch["to"]):
                    continue
                gap = geo.dist_point_path(p["position"][:2], ch["path"]) \
                    - MIN_PORT / 2 - ch["width_mm"] / 2
                self.assertGreaterEqual(gap, MIN_WALL - 1e-9,
                                        f"port {p['id']} sits {gap:.3f} mm from {ch['id']}")

    def test_port_overlap_rule_passes_on_the_routed_geometry(self):
        """check_chip_spec's own rule, the one with 60 pre-existing failures on main."""
        rows = ccs.port_overlaps(self.chip, polys=ccs._spec_outlines(self.chip), source="spec")
        self.assertTrue(rows)
        for r in rows:
            self.assertGreaterEqual(r["overlap_mm2"] + r["tolerance_mm2"], r["need_mm2"],
                                    f"port {r['port']} opens onto {r['overlap_mm2']:.3f} mm2")

    def test_the_chip_still_matches_the_chip_spec_schema(self):
        self.assertEqual(schema_io.schema_errors("chip_spec", self.chip), [])

    def test_one_etch_depth(self):
        depths = {ch["depth_mm"] for ch in self.chip["channels"]}
        self.assertEqual(len(depths), 1)
        self.assertLessEqual(max(depths), contracts.max_depth_mm())

    def test_check_chip_spec_finds_no_errors(self):
        params = schema_io.load_json(ROOT / "contracts" / "parameters.json")
        materials = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
        fab = schema_io.load_json(ROOT / "cad" / "fabrication.json")
        blocks = schema_io.load_json(ROOT / "contracts" / "blocks.json")
        f = ccs.check(self.chip, [], params, materials, fab, [], blocks)
        self.assertEqual(f.errors, [])


# --- 4. length targets ------------------------------------------------------------------

class LengthTargets(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = route(feed_chip(), feed_connections())
        cls.report = cls.result["report"]
        cls.rows = cls.report["length_targets"]

    def test_the_balance_group_got_a_target_each(self):
        names = {r["net"] for r in self.rows}
        self.assertEqual(names, {"c_to_lane_1", "c_to_lane_2", "c_to_lane_3"})
        for r in self.rows:
            self.assertEqual(r["balance_group"], "lane_feeds")

    def test_achieved_is_reported_against_target_for_every_one(self):
        for r in self.rows:
            for key in ("target_length_mm", "achieved_length_mm", "error_mm",
                        "error_percent", "folds", "within_tolerance"):
                self.assertIn(key, r)

    def test_the_group_ends_up_hydraulically_balanced(self):
        rs = [r["resistance_pa_s_per_m3"] for r in self.rows]
        spread = (max(rs) - min(rs)) / max(rs)
        self.assertLess(spread, 0.02,
                        f"resistances still differ by {spread:.1%}: the feeds are not balanced")

    def test_the_short_branches_were_actually_meandered(self):
        self.assertTrue(any(r["folds"] > 0 for r in self.rows),
                        "nothing was folded, so nothing was length-matched")

    def test_a_meandered_channel_is_a_serpentine_block(self):
        folded = {r["channel"] for r in self.rows if r["folds"] > 0}
        for ch in self.result["chip"]["channels"]:
            if ch["id"] in folded:
                self.assertEqual(ch["block"], "serpentine_channel")

    def test_a_fold_never_breaks_planarity(self):
        self.assertEqual(planarity.errors(planarity.check(self.result["chip"])), [])
        self.assertEqual(ccs.crossings(self.result["chip"]), [])

    def test_the_serpentine_keeps_a_minimum_wall_between_its_own_legs(self):
        folded = {r["channel"]: r for r in self.rows if r["folds"] > 0}
        for ch in self.result["chip"]["channels"]:
            if ch["id"] not in folded:
                continue
            self.assertGreaterEqual(folded[ch["id"]]["pitch_mm"],
                                    ch["width_mm"] + MIN_WALL - 1e-9)
            self.assertEqual(geo.self_crossings(ch["path"]), [])

    def test_an_impossible_target_is_reported_not_faked(self):
        """A target with nowhere to fold comes back short, with a reason, and still planar."""
        spec = chip("s7_tight_target", [chamber("a", 3.0, 10.0, 2.0, 2.0),
                                        chamber("b", 7.0, 10.0, 2.0, 2.0)], [])
        res = route(spec, [{"id": "c1", "from": "a", "to": "b", "carries": "liquid",
                            "target_length_mm": 500.0}])
        self.assertTrue(res["ok"])
        row = res["report"]["length_targets"][0]
        self.assertFalse(row["within_tolerance"])
        self.assertLess(row["achieved_length_mm"], row["target_length_mm"])
        self.assertTrue(row["why"], "a missed target must say why")
        self.assertEqual(planarity.errors(planarity.check(res["chip"])), [])

    def test_resistance_targets_use_networks_own_formula(self):
        mu = contracts.viscosity_for(37.0).pa_s
        group = [("a", 5.0, 0.22, 0.3), ("b", 9.0, 0.22, 0.3)]
        want = meander.resistance_targets(group, mu)
        self.assertAlmostEqual(want["a"], 9.0, places=6)
        self.assertAlmostEqual(want["b"], 9.0, places=6)
        import network
        self.assertAlmostEqual(meander.rect_resistance(9.0, 0.22, 0.3, mu),
                               network.rect_resistance(9.0, 0.22, 0.3, mu), places=6)


# --- 5. THE TIGHT CASE: rip-up and reroute ---------------------------------------------

class RipUpAndReroute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.result = route(pinch_chip(), pinch_connections())
        cls.report = cls.result["report"]

    def test_the_greedy_order_really_does_fail_first(self):
        """Proof the fixture is tight: with rip-up switched off it cannot be routed."""
        nets = conn.normalise({"connections": pinch_connections()})
        r = router.Router(pinch_chip(), nets, max_passes=1)
        outcome = r.run()
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.failures[0].net.id, "c_capsules")
        self.assertEqual(outcome.failures[0].reason, "fenced in")

    def test_the_narrow_gap_cannot_take_the_capsule_line(self):
        """The arithmetic the fixture rests on, stated rather than assumed."""
        nets = conn.normalise({"connections": pinch_connections()})
        r = router.Router(pinch_chip(), nets)
        thin = r.width_for(next(n for n in nets if n.id == "c_buffer"))
        fat = r.width_for(next(n for n in nets if n.id == "c_capsules"))
        self.assertLessEqual(thin + 2 * MIN_WALL, 1.10 + 1e-9)
        self.assertLessEqual(fat + 2 * MIN_WALL, 1.30 + 1e-9)
        self.assertGreater(fat + 2 * MIN_WALL, 1.10)

    def test_it_routes_after_ripping_up(self):
        self.assertTrue(self.result["ok"], self.report.get("result"))
        self.assertGreater(len(self.report["ripups"]), 0, "no rip-up happened")
        self.assertGreater(self.report["passes"], 1)

    def test_the_rip_up_is_reported_with_its_victim(self):
        rip = self.report["ripups"][0]
        self.assertEqual(rip["net"], "c_capsules")
        self.assertIn("c_buffer", rip["ripped_up"])

    def test_the_capsule_line_ended_up_in_the_wide_gap(self):
        paths = {c["id"]: c["path"] for c in self.result["chip"]["channels"]}
        ys = crosses_line_x(paths["ch_c_capsules"], 15.0)
        self.assertTrue(ys, "the capsule line never crosses the divider")
        self.assertTrue(all(7.4 < y < 8.7 for y in ys),
                        f"the capsule line crossed at y={ys}, not through the 1.3 mm gap")

    def test_the_buffer_line_took_the_narrow_gap(self):
        paths = {c["id"]: c["path"] for c in self.result["chip"]["channels"]}
        ys = crosses_line_x(paths["ch_c_buffer"], 15.0)
        self.assertTrue(ys, "the buffer line never crosses the divider")
        self.assertTrue(all(12.9 < y < 14.0 for y in ys),
                        f"the buffer line crossed at y={ys}, not through the 1.1 mm gap")

    def test_the_tight_result_is_still_planar(self):
        self.assertEqual([str(c) for c in planarity.errors(planarity.check(self.result["chip"]))],
                         [])
        self.assertEqual(ccs.crossings(self.result["chip"]), [])

    def test_the_tight_result_passes_the_schema_and_check_chip_spec(self):
        self.assertEqual(schema_io.schema_errors("chip_spec", self.result["chip"]), [])
        params = schema_io.load_json(ROOT / "contracts" / "parameters.json")
        materials = schema_io.load_json(ROOT / "contracts" / "materials.json")["materials"]
        fab = schema_io.load_json(ROOT / "cad" / "fabrication.json")
        blocks = schema_io.load_json(ROOT / "contracts" / "blocks.json")
        f = ccs.check(self.result["chip"], [], params, materials, fab, [], blocks)
        self.assertEqual(f.errors, [])


# --- 6. a crossing is never accepted ----------------------------------------------------

class CrossingsAreRejected(unittest.TestCase):
    def test_the_checker_catches_a_hand_drawn_crossing(self):
        spec = crossed_chip()
        conflicts = planarity.check(spec)
        crossing = [c for c in conflicts if c.rule == "crossing"]
        self.assertEqual(len(crossing), 1)
        self.assertEqual(crossing[0].severity, "error")
        self.assertAlmostEqual(crossing[0].at[0], 12.0, places=3)
        self.assertAlmostEqual(crossing[0].at[1], 15.0, places=3)
        self.assertEqual(len(ccs.crossings(spec)), 1)

    def test_a_self_crossing_is_caught_too(self):
        spec = crossed_chip()
        spec["channels"] = [spec["channels"][0]]
        spec["channels"][0]["path"] = [[7.5, 15.0], [20.0, 15.0], [20.0, 10.0],
                                       [15.0, 10.0], [15.0, 18.0], [22.5, 18.0]]
        spec["channels"][0]["to"] = "ne"
        self.assertTrue([c for c in planarity.check(spec) if c.rule == "self_crossing"])

    def test_the_stage_refuses_to_emit_a_chip_that_fails_its_own_check(self):
        """If anything ever slipped through, S7 throws the layout away, not the rule."""
        spec = feed_chip()
        original = planarity.check
        try:
            planarity.check = lambda *a, **k: [planarity.Conflict(
                "crossing", "error", "injected", "a deliberately injected crossing")]
            result = route(spec, feed_connections())
        finally:
            planarity.check = original
        self.assertFalse(result["ok"])
        self.assertEqual(result["rerun_from"], "s6")
        self.assertIn("planar check", result["reason"])
        self.assertIsNone(result["chip"])


# --- 7. unroutable: the right rerun_from, with the conflict named -----------------------

class UnroutableReportsTheConflict(unittest.TestCase):
    def test_a_fenced_in_net_asks_s6_to_re_place(self):
        """A full-height divider with no gap at all: no route exists, as placed."""
        spec = chip("s7_sealed_fixture", [
            chamber("wall", 15.0, 10.0, 1.0, 20.0),
            chamber("west", 6.0, 10.0, 4.0, 4.0),
            chamber("east", 24.0, 10.0, 4.0, 4.0)], [])
        result = route(spec, [{"id": "c1", "from": "west", "to": "east", "carries": "liquid"}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["rerun_from"], "s6")
        self.assertIn("c1", result["reason"])
        self.assertIn("wall", " ".join(result["report"]["conflict"]["blockers"]))
        self.assertIn("fenced in", result["reason"])
        self.assertIsNone(result["chip"])

    def test_a_part_with_no_room_for_a_mouth_names_the_part(self):
        spec = chip("s7_boxed_fixture", [
            chamber("tiny", 15.0, 10.0, 0.5, 0.5),
            chamber("ring_n", 15.0, 11.0, 4.0, 1.0),
            chamber("ring_s", 15.0, 9.0, 4.0, 1.0),
            chamber("ring_w", 13.5, 10.0, 1.0, 1.0),
            chamber("ring_e", 16.5, 10.0, 1.0, 1.0),
            chamber("far", 25.0, 10.0, 3.0, 3.0)], [])
        result = route(spec, [{"id": "c1", "from": "tiny", "to": "far", "carries": "liquid"}])
        self.assertFalse(result["ok"])
        self.assertEqual(result["rerun_from"], "s6")
        self.assertIn("no legal opening", result["reason"])
        self.assertIn("tiny", result["reason"])

    def test_a_non_planar_connection_graph_asks_s4_to_re_partition(self):
        """E > 3V - 6 can never be drawn on one layer, so re-placing cannot help."""
        parts = [f"n{i}" for i in range(5)]
        spec = chip("s7_k5_fixture",
                    [chamber(p, 4.0 + 5 * i, 10.0, 2.0, 2.0) for i, p in enumerate(parts)], [])
        links = [{"id": f"c{i}_{j}", "from": a, "to": b, "carries": "liquid"}
                 for i, a in enumerate(parts) for j, b in enumerate(parts) if i < j]
        result = route(spec, links)
        self.assertFalse(result["ok"])
        self.assertEqual(result["rerun_from"], "s4")
        self.assertIn("3V - 6", result["reason"])

    def test_the_graph_rule_is_only_a_necessary_condition(self):
        overfull, _ = router.connection_graph_is_overfull(
            conn.normalise({"connections": feed_connections()}))
        self.assertFalse(overfull)

    def test_an_unknown_part_is_named_rather_than_crashing(self):
        result = route(feed_chip(), [{"id": "c1", "from": "header", "to": "nowhere",
                                      "carries": "liquid"}])
        self.assertFalse(result["ok"])
        self.assertIn("nowhere", result["reason"])


# --- 8. the connections file -------------------------------------------------------------

class ConnectionsInput(unittest.TestCase):
    def test_the_documented_shape_round_trips(self):
        nets = conn.normalise({"id": "a", "chip_id": "c", "connections": [
            {"id": "n1", "from": {"part": "a", "opening": "east"},
             "to": {"part": "b", "opening": "west"}, "carries": "capsules",
             "min_width_mm": 0.3, "particle_um": 70, "target_length_mm": 9.0,
             "balance_group": "g"}]})
        n = nets[0]
        self.assertEqual((n.a, n.a_sides, n.b, n.b_sides), ("a", ("east",), "b", ("west",)))
        self.assertEqual((n.carries, n.min_width_mm, n.target_length_mm, n.balance_group),
                         ("capsules", 0.3, 9.0, "g"))
        self.assertEqual(n.emit_id, "ch_n1")

    def test_the_tolerated_variations_all_mean_the_same_thing(self):
        a = conn.normalise({"nets": [{"id": "n", "from": "x", "to": "y", "width_um": 300,
                                      "length_target_mm": 9.0, "group": "g"}]})[0]
        b = conn.normalise({"links": [{"id": "n", "from": {"id": "x"}, "to": {"chamber": "y"},
                                       "min_width_mm": 0.3, "target_length_um": 9000,
                                       "balance": "g"}]})[0]
        self.assertEqual((a.min_width_mm, a.target_length_mm, a.balance_group),
                         (b.min_width_mm, b.target_length_mm, b.balance_group))

    def test_an_omitted_carries_takes_the_strict_default(self):
        n = conn.normalise({"connections": [{"id": "n", "from": "x", "to": "y"}]})[0]
        self.assertEqual(n.carries, "capsules")

    def test_a_bad_file_fails_loudly(self):
        for bad, needle in (
            ({"connections": [{"id": "n", "from": "x"}]}, "'from' and 'to'"),
            ({"connections": [{"id": "n", "from": "x", "to": "x"}]}, "to itself"),
            ({"connections": [{"id": "n", "from": "x", "to": "y", "carries": "lava"}]}, "carries"),
            ({"connections": [{"id": "n", "from": "x", "to": "y", "opening": "up"}]}, None),
            ({"wrong": []}, "no connection list"),
            ({"connections": [{"id": "n", "from": "x", "to": "y"},
                              {"id": "n", "from": "p", "to": "q"}]}, "duplicate"),
        ):
            if needle is None:
                continue
            with self.assertRaises(conn.ConnectionError_) as cm:
                conn.normalise(bad)
            self.assertIn(needle, str(cm.exception))

    def test_a_requested_opening_is_honoured(self):
        """S6 can name the wall. The router then picks the route, not the wall.

        A chip with no ports, because an unwired port is an error in its own right (its lid
        hole would open onto nothing) and that is not what this test is about.
        """
        spec = chip("s7_opening_fixture", [chamber("header", 5.5, 10.0, 2.0, 12.0),
                                           chamber("lane_2", 23.0, 10.0, 6.0, 3.0)], [])
        result = route(spec, [{"id": "c1", "from": {"part": "header", "opening": "west"},
                               "to": {"part": "lane_2", "opening": "south"},
                               "carries": "liquid"}])
        self.assertTrue(result["ok"], result["report"].get("result"))
        net = result["report"]["nets"][0]
        self.assertEqual((net["from_side"], net["to_side"]), ("west", "south"))
        path = result["chip"]["channels"][0]["path"]
        self.assertAlmostEqual(path[0][0], 4.5, places=6)    # header's west wall
        self.assertAlmostEqual(path[-1][1], 8.5, places=6)   # lane_2's south wall

    def test_an_unwired_port_is_an_error_not_a_shrug(self):
        """A lid hole that opens onto nothing is a hole to nowhere: check_chip_spec's rule."""
        spec = feed_chip()
        result = route(spec, [{"id": "c1", "from": "header", "to": "lane_2",
                               "carries": "liquid"}])
        self.assertFalse(result["ok"])
        rules = {c["rule"] for c in
                 result["report"]["checks"]["path_aware_planarity"]["errors"]}
        self.assertEqual(rules, {"port_overlap"})
        self.assertEqual(result["rerun_from"], "s6")

    def test_the_fallback_reads_the_chips_own_channels(self):
        spec = feed_chip()
        spec["channels"] = [{"id": "ch_x", "from": "header", "to": "lane_2",
                             "width_mm": 0.5, "depth_mm": 0.3, "carries": "liquid"}]
        nets = conn.from_chip(spec)
        self.assertEqual(len(nets), 1)
        self.assertEqual((nets[0].a, nets[0].b, nets[0].min_width_mm), ("header", "lane_2", 0.5))
        self.assertEqual(nets[0].emit_id, "ch_x")

    def test_a_missing_connections_file_says_which_stage_owes_it(self):
        with self.assertRaises(conn.ConnectionError_) as cm:
            conn.load("no_such_assay_id", "no_such_chip")
        self.assertIn("S6", str(cm.exception))


# --- 8b. S6's real connections file -----------------------------------------------------

def s6_endpoint(feature, direction="none", opening="pad", position=(1.0, 6.75),
                width=1.2, port=None):
    out = {"feature": feature, "opening": opening, "position": list(position),
           "direction": direction, "opening_width_mm": width}
    if port:
        out["port"] = port
    return out


def padded_chip():
    """A chip in S6's own idiom: every port drilled into a pad, two lines sharing a waste.

    Three things here are S6's, not mine, and S7 has to cope with all of them: part walls
    on coordinates like 1.99995 mm, a port hole inside a pad chamber, and two connections
    landing on one pad.
    """
    return chip("s7_padded_fixture", [
        chamber("mix_27", 6.7333, 6.6, 9.4667, 9.2, "mixer", "herringbone_mixer"),
        chamber("trap_28", 20.0, 6.6, 4.0, 4.0, "exchange", "trap_chamber"),
        chamber("inlet_1_pad", 1.0, 6.6, 1.2, 1.2, "inlet"),
        chamber("waste_pad", 15.0, 18.0, 1.2, 1.2, "outlet"),
    ], [port("inlet_1", 1.0, 6.6, "inlet", width_um=MIN_PORT * 1000),
        port("waste", 15.0, 18.0, "outlet", width_um=MIN_PORT * 1000)])


def padded_connections():
    return [
        {"id": "conn_1", "order": 1,
         "from": s6_endpoint("inlet_1_pad", "none", "pad", (1.0, 6.6), 1.2, "inlet_1"),
         "to": s6_endpoint("mix_27", "-x", "inlet_a", (1.99995, 6.6), 1.2),
         "carries": "capsules", "min_width_mm": 0.2, "why": "the sample enters here"},
        {"id": "conn_2", "order": 2,
         "from": s6_endpoint("mix_27", "+x", "outlet", (11.46665, 6.6), 1.2),
         "to": s6_endpoint("trap_28", "-x", "inlet", (18.0, 6.6), 2.0),
         "carries": "capsules", "min_width_mm": 0.2, "must_not_cross": ["conn_3"]},
        {"id": "conn_3", "order": 3,
         "from": s6_endpoint("mix_27", "+y", "filtrate", (6.7333, 11.2), 1.2),
         "to": s6_endpoint("waste_pad", "none", "pad", (15.0, 18.0), 1.2, "waste"),
         "carries": "liquid", "min_width_mm": 0.2},
        {"id": "conn_4", "order": 4,
         "from": s6_endpoint("trap_28", "+y", "filtrate", (20.0, 8.6), 1.2),
         "to": s6_endpoint("waste_pad", "none", "pad", (15.0, 18.0), 1.2, "waste"),
         "carries": "liquid", "min_width_mm": 0.2},
    ]


class S6ConnectionsFile(unittest.TestCase):
    """S6's actual shape, which is not the shape this module first guessed at."""

    @classmethod
    def setUpClass(cls):
        cls.nets = conn.normalise({"connections": padded_connections()})
        cls.result = route(padded_chip(), padded_connections())

    def test_feature_is_the_routable_id_not_part_or_port(self):
        """For a port endpoint the channel meets the PAD, never the hole."""
        self.assertEqual(self.nets[0].a, "inlet_1_pad")
        self.assertEqual(self.nets[0].a_end.port, "inlet_1")

    def test_direction_is_an_outward_normal_and_fixes_the_wall(self):
        for net, want_a, want_b in ((self.nets[0], (), ("west",)),
                                    (self.nets[1], ("east",), ("west",)),
                                    (self.nets[2], ("north",), ())):
            self.assertEqual(net.a_sides, want_a, net.id)
            self.assertEqual(net.b_sides, want_b, net.id)

    def test_an_opening_name_is_not_a_compass_word_and_is_not_an_error(self):
        for name in ("pad", "inlet_a", "filtrate", "backflush", "inlet_b"):
            nets = conn.normalise({"connections": [
                {"id": "n", "from": {"feature": "a", "opening": name},
                 "to": {"feature": "b", "opening": name}}]})
            self.assertEqual(nets[0].a_sides, ())
        with self.assertRaises(conn.ConnectionError_):
            conn.normalise({"connections": [
                {"id": "n", "from": {"feature": "a", "direction": "sideways"},
                 "to": "b"}]})

    def test_s6s_order_and_must_not_cross_are_carried_into_the_report(self):
        row = next(c for c in self.result["report"]["connections"]["nets"]
                   if c["id"] == "conn_2")
        self.assertEqual(row["must_not_cross"], ["conn_3"])
        self.assertEqual(self.nets[1].order, 2)

    def test_it_routes_a_padded_chip(self):
        self.assertTrue(self.result["ok"], self.report_result())
        self.assertEqual(len(self.result["chip"]["channels"]), 4)
        self.assertEqual(planarity.errors(planarity.check(self.result["chip"])), [])
        self.assertEqual(ccs.crossings(self.result["chip"]), [])

    def report_result(self):
        rep = self.result["report"]
        return rep.get("reason") or rep.get("result") or json.dumps(
            rep["checks"]["path_aware_planarity"]["errors"][:2])

    def test_a_port_and_its_pad_are_one_feature(self):
        feats = obstacles.features_of(padded_chip())
        self.assertEqual(obstacles.drilled_into(feats),
                         {"inlet_1": "inlet_1_pad", "waste": "waste_pad"})
        self.assertEqual(obstacles.one_feature_with({"inlet_1_pad"}, feats),
                         {"inlet_1_pad", "inlet_1"})

    def test_a_pad_is_a_place_streams_may_meet(self):
        """Two waste lines on one 1.2 mm pad is a tee, not a collision.

        A 1.2 mm wall has 1.2 - 2 x (w/2 + 0.4) = 0.2 mm of legal mouth left, so without
        this the second line on a shared waste port is told the pad is fenced in.
        """
        feats = obstacles.features_of(padded_chip())
        self.assertTrue(feats["waste_pad"].is_meeting)
        self.assertFalse(feats["mix_27"].is_meeting)
        shared = [c for c in self.result["chip"]["channels"]
                  if "waste_pad" in (c["from"], c["to"])]
        self.assertEqual(len(shared), 2)

    def test_the_mouth_tolerance_matches_the_dxf_this_repo_writes(self):
        """S6 walls land on 1.99995 mm; the DXF carries 4 decimals. Both must agree."""
        self.assertEqual(planarity.PATH_DECIMALS, 4)
        self.assertGreaterEqual(planarity.MOUTH_TOL_MM, 10 ** -planarity.PATH_DECIMALS)
        self.assertLess(planarity.MOUTH_TOL_MM, MIN_OPEN / 100)
        on_wall = [c for c in self.result["chip"]["channels"] if c["to"] == "mix_27"]
        self.assertTrue(on_wall)
        for ch in on_wall:
            self.assertAlmostEqual(ch["path"][-1][0], 2.0, places=3)

    def test_the_mouth_fidelity_is_reported_for_every_net(self):
        for net in self.result["report"]["nets"]:
            for which in ("from", "to"):
                self.assertIn(net["mouth_fidelity"][which], ("point", "wall", "any"))

    def test_a_fenced_wall_falls_back_to_another_wall_and_says_so(self):
        """The cascade's last step. It must widen, and it must not do it quietly."""
        spec = chip("s7_fenced_wall", [
            chamber("src", 10.0, 10.0, 3.0, 3.0),
            chamber("fence", 10.0, 13.0, 9.0, 2.0),     # 0.5 mm above src's north wall
            chamber("dst", 24.0, 10.0, 3.0, 3.0)], [])
        result = route(spec, [{"id": "c1",
                               "from": s6_endpoint("src", "+y", "outlet", (10.0, 11.5), 2.0),
                               "to": s6_endpoint("dst", "-x", "inlet", (22.5, 10.0), 2.0),
                               "carries": "liquid"}])
        self.assertTrue(result["ok"], result["report"].get("result"))
        net = result["report"]["nets"][0]
        self.assertEqual(net["mouth_fidelity"]["from"], "any")
        self.assertNotEqual(net["from_side"], "north")
        moved = [u for u in result["report"]["unresolved"]
                 if "could not stay on the wall" in u["what"]]
        self.assertEqual(len(moved), 1)
        self.assertEqual(moved[0]["openings"][0]["s6_wall"], ["north"])

    def test_the_mouth_budget_allows_exactly_what_geometry_allows(self):
        """A 0.83 mm square takes FOUR 0.2 mm channels, one per wall, and no more.

        Four is right, not generous: two mouths on adjacent walls leave 0.445 mm between
        their channels at the corner, which clears the 0.4 mm wall. So the bound is tight,
        and the fifth connection is the one that cannot be placed.
        """
        spec = chip("s7_budget", [
            chamber("tiny", 15.0, 10.0, 0.83, 0.83),
            *(chamber(f"f{i}", 3.0 + 5 * i, 3.0, 2.0, 2.0) for i in range(5))], [])

        def nets_for(n):
            return conn.normalise({"connections": [
                {"id": f"c{i}", "from": f"f{i}", "to": "tiny", "carries": "liquid"}
                for i in range(n)]})

        self.assertEqual(router.Router(spec, nets_for(4)).mouth_budget(), [])
        over = router.Router(spec, nets_for(5)).mouth_budget()
        self.assertEqual(len(over), 1)
        self.assertEqual(over[0].reason, "part over-subscribed")
        self.assertIn("0.83 x 0.83 mm", over[0].detail)
        self.assertIn("5 connection(s)", over[0].detail)
        self.assertIn("at most 4 mouth(s)", over[0].detail)

    def test_an_over_subscribed_part_is_arithmetic_not_a_rip_up_marathon(self):
        """Say so up front; do not burn eight rip-up passes rediscovering it."""
        spec = chip("s7_oversubscribed", [
            chamber("tiny", 15.0, 10.0, 0.83, 0.83),
            *(chamber(f"f{i}", 3.0 + 5 * i, 3.0, 2.0, 2.0) for i in range(5))], [])
        nets = conn.normalise({"connections": [
            {"id": f"c{i}", "from": f"f{i}", "to": "tiny", "carries": "liquid"}
            for i in range(5)]})
        result = route(spec, [n.as_dict() | {"from": n.a, "to": n.b} for n in nets])
        self.assertFalse(result["ok"])
        self.assertEqual(result["rerun_from"], "s6")
        self.assertEqual(result["report"]["passes"], 0, "it must not search at all")

    def test_a_no_legal_opening_failure_names_the_wall_and_the_blocker(self):
        spec = chip("s7_boxed_in", [
            chamber("src", 10.0, 10.0, 2.0, 2.0),
            chamber("n", 10.0, 11.5, 4.0, 0.6), chamber("s", 10.0, 8.5, 4.0, 0.6),
            chamber("w", 8.5, 10.0, 0.6, 4.0), chamber("e", 11.5, 10.0, 0.6, 4.0),
            chamber("dst", 24.0, 10.0, 3.0, 3.0)], [])
        result = route(spec, [{"id": "c1", "from": "src", "to": "dst", "carries": "liquid"}])
        self.assertFalse(result["ok"])
        detail = result["report"]["conflict"]["detail"]
        self.assertIn("2.00 x 2.00 mm", detail)
        for side in ("east", "north", "west", "south"):
            self.assertIn(f"{side}: blocked by", detail)

    def test_make_figures_and_check_chip_spec_disagree_about_port_pads(self):
        """A rule contradiction S7 can only report. Attribution matters, so it is tested.

        tools/check_chip_spec.py REQUIRES a port hole to open onto cut area; a port pad is
        how S6 provides it. cad/make_figures.py errors on ANY port touching a chamber. No
        chip can satisfy both, so S7 separates that complaint from the `path` one and names
        the owner instead of counting it against the route.
        """
        msgs, _ = make_figures.geometry_warnings(self.result["chip"])
        pad_errors = [m for m in msgs
                      if m.startswith("ERROR") and "is drilled into a chamber" in m]
        rows = ccs.port_overlaps(self.result["chip"],
                                 polys=ccs._spec_outlines(self.result["chip"]), source="spec")
        for r in rows:       # the pads satisfy the rule that the pads exist for
            self.assertGreaterEqual(r["overlap_mm2"] + r["tolerance_mm2"], r["need_mm2"])
        if not pad_errors:
            return           # make_figures has been taught about pads: nothing to excuse
        self.assertEqual(len(pad_errors), 2)
        entry = [u for u in self.result["report"]["unresolved"]
                 if "port pad" in u["what"]]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0]["lines"], pad_errors)
        self.assertIn("contradict each other", entry[0]["owner"])


# --- 9. the grid, the mask and the search -----------------------------------------------

class GridAndMask(unittest.TestCase):
    def test_the_grid_is_no_coarser_than_the_brief_allows(self):
        self.assertLessEqual(router.DEFAULT_GRID_MM, 0.05)
        with self.assertRaises(ValueError):
            router.Router(feed_chip(), conn.normalise({"connections": feed_connections()}),
                          grid_mm=0.1)

    def test_a_chamber_blocks_its_interior_and_its_wall(self):
        spec = feed_chip()
        feats = obstacles.features_of(spec)
        grid = obstacles.Grid(30, 20, 0.05)
        mask = obstacles.blocked_mask(grid, obstacles.static_obstacles(feats), 0.22, MIN_WALL)
        inside = grid.cell_containing(23.0, 10.0)            # middle of lane_2
        self.assertTrue(mask[grid.index(*inside)])
        beside = grid.cell_containing(23.0, 11.5 + MIN_WALL + 0.11 + 0.05)
        self.assertFalse(mask[grid.index(*beside)])
        too_close = grid.cell_containing(23.0, 11.5 + 0.11)
        self.assertTrue(mask[grid.index(*too_close)])

    def test_an_exempt_part_still_blocks_its_own_interior(self):
        """Otherwise the search would cut through a chamber and leave by the far wall."""
        spec = feed_chip()
        feats = obstacles.features_of(spec)
        grid = obstacles.Grid(30, 20, 0.05)
        mask = obstacles.blocked_mask(grid, obstacles.static_obstacles(feats), 0.22, MIN_WALL,
                                      exempt={"lane_2"})
        self.assertTrue(mask[grid.index(*grid.cell_containing(23.0, 10.0))])
        self.assertFalse(mask[grid.index(*grid.cell_containing(23.0, 11.52))])

    def test_the_board_rim_is_a_wall(self):
        grid = obstacles.Grid(30, 20, 0.05)
        mask = obstacles.blocked_mask(grid, [], 0.22, MIN_WALL)
        self.assertTrue(mask[grid.index(*grid.cell_containing(0.2, 10.0))])
        self.assertFalse(mask[grid.index(*grid.cell_containing(0.6, 10.0))])

    def test_a_routed_net_is_an_obstacle_to_the_next_one(self):
        grid = obstacles.Grid(30, 20, 0.05)
        obs = [obstacles.net_obstacle("n1", [(5.0, 10.0), (25.0, 10.0)], 0.3)]
        mask = obstacles.blocked_mask(grid, obs, 0.3, MIN_WALL)
        self.assertTrue(mask[grid.index(*grid.cell_containing(15.0, 10.0))])
        self.assertTrue(mask[grid.index(*grid.cell_containing(15.0, 10.0 + 0.29))])
        self.assertFalse(mask[grid.index(*grid.cell_containing(15.0, 10.0 + 0.75))])

    def test_a_small_part_may_take_a_mouth_it_cannot_margin_on_both_sides(self):
        """A 0.8 mm wall taking a 0.44 mm channel: allowed, but it may not overhang."""
        grid = obstacles.Grid(30, 20, 0.05)
        f = obstacles.Feature("f", "chamber", (10.0, 10.0, 11.2, 10.8), (10.6, 10.4), False)
        wide = obstacles.chamber_terminals(grid, f, 0.44, MIN_WALL, ["east"])
        self.assertEqual(len(wide), 1)
        self.assertLessEqual(abs(wide[0].anchor[1] - 10.4), (0.8 - 0.44) / 2 + 1e-9)
        self.assertEqual(obstacles.chamber_terminals(grid, f, 1.0, MIN_WALL, ["east"]), [])

    def test_a_port_mouth_crosses_the_whole_bore(self):
        grid = obstacles.Grid(30, 20, 0.05)
        f = obstacles.Feature("p", "port", geo.square_at((10.0, 10.0), MIN_PORT),
                              (10.0, 10.0), True, MIN_PORT)
        east = [t for t in obstacles.port_terminals(grid, f, 0.22) if t.side == "east"][0]
        self.assertAlmostEqual(east.anchor[0], 10.0 - MIN_PORT / 2, places=6)
        self.assertGreaterEqual(east.lead[-1][0], 10.0 + MIN_PORT / 2)


# --- 10. THE CROSS-CHECK: does cad/ honour `path` in this worktree? ---------------------

class CadPathSupport(unittest.TestCase):
    """The audit finding, re-tested here rather than taken on trust.

    This test does NOT assert that cad/ is broken - that would fail the day someone fixes
    it. It asserts the probe agrees with what the files actually do, and that S7 shouts
    whenever they are path-blind.
    """

    def setUp(self):
        self.support = planarity.cad_path_support()

    def test_the_probe_agrees_with_make_figures_behaviour(self):
        spec = chip("s7_probe", [chamber("a", 4.0, 10.0, 2.0, 2.0),
                                 chamber("b", 20.0, 10.0, 2.0, 2.0)], [])
        spec["channels"] = [{"id": "ch", "from": "a", "to": "b", "width_mm": 0.3,
                             "depth_mm": 0.3, "block": "channel_corner", "carries": "liquid",
                             "path": [[5.0, 10.0], [12.0, 10.0], [12.0, 3.0],
                                      [19.0, 3.0], [19.0, 10.0]]}]
        segs = make_figures.segments_of(spec)
        really_honours = len(segs) > 1
        self.assertEqual(self.support["make_figures"]["honours_path"], really_honours)

    def test_network_py_is_the_one_that_does_read_path(self):
        import network
        spec = chip("s7_probe2", [chamber("a", 4.0, 10.0, 2.0, 2.0),
                                  chamber("b", 20.0, 10.0, 2.0, 2.0)], [])
        spec["channels"] = [{"id": "ch", "from": "a", "to": "b", "width_mm": 0.3,
                             "depth_mm": 0.3, "carries": "liquid",
                             "path": [[5.0, 10.0], [12.0, 10.0], [12.0, 3.0],
                                      [19.0, 3.0], [19.0, 10.0]]}]
        g = network.channel_geometry(spec)["ch"]
        self.assertEqual(g["source"], "spec path")
        self.assertAlmostEqual(g["length_mm"], 7 + 7 + 7 + 7, places=6)
        self.assertTrue(self.support["network"]["honours_path"])

    def test_the_report_carries_the_finding_either_way(self):
        result = route(feed_chip(), feed_connections())
        support = result["report"]["cad_path_support"]
        self.assertIn("verdict", support)
        self.assertEqual(set(support["path_blind"]) <= {"make_figures", "make_train_cad",
                                                        "network"}, True)
        if support["path_blind"]:
            self.assertIn("PATH-BLIND", support["verdict"])
            lines = s7_route.path_support_warning(support)
            self.assertTrue(lines)
            self.assertTrue(any("NOT sufficient" in line for line in lines),
                            "the warning must say make_figures' verdict is not sufficient")
            self.assertTrue(any("IGNORES `path`" in line for line in lines))
        else:
            self.assertEqual(s7_route.path_support_warning(support), [])

    def test_the_straight_line_proxy_is_labelled_and_kept_separate(self):
        result = route(feed_chip(), feed_connections())
        proxy = result["report"]["checks"]["straight_line_proxy"]
        self.assertTrue(proxy["available"])
        self.assertIn("ignores `path`", proxy["geometry"])
        authority = result["report"]["checks"]["path_aware_planarity"]
        self.assertIn("as routed", authority["geometry"])

    def test_make_figures_reports_no_errors_on_a_deliverable_routed_chip(self):
        """The brief's acceptance check: 0 ERROR lines out of cad/make_figures.py."""
        result = route(feed_chip(), feed_connections())
        self.assertTrue(result["ok"], result["report"].get("result"))
        msgs, _ = make_figures.geometry_warnings(result["chip"])
        self.assertEqual([m for m in msgs if m.startswith("ERROR")], [],
                         "make_figures found collisions on the routed feed chip")
        self.assertEqual(result["report"]["checks"]["straight_line_proxy"]["errors"], [])
        self.assertEqual(result["report"]["unresolved"], [])

    def test_a_detour_is_exactly_where_make_figures_stops_being_able_to_judge(self):
        """The audit finding, demonstrated rather than asserted.

        The pinch fixture CANNOT give make_figures 0 ERRORs while cad/ is path-blind, and
        that is not a defect in the route - it is the whole bug. The capsule line is routed
        the long way round the divider; make_figures measures the straight line between its
        two ports, which runs through `div_s`. So the test pins down three things:

          * the chip IS planar on the geometry that is actually on it,
          * make_figures' complaints are all about the straight line, which is proved by
            re-deriving each one from the straight-line geometry alone,
          * S7 does not swallow them: every one is carried into report['unresolved'] with
            the reason, so S9 and the S12 report cannot mistake a clean chip for a dirty
            one or the reverse.

        When cad/ learns to read `path` (the fix on the other branch), path_blind goes
        empty and this test asserts the opposite - 0 errors - without being edited.
        """
        result = route(pinch_chip(), pinch_connections())
        self.assertTrue(result["ok"], result["report"].get("result"))
        self.assertEqual(planarity.errors(planarity.check(result["chip"])), [])
        self.assertEqual(ccs.crossings(result["chip"]), [])

        msgs, _ = make_figures.geometry_warnings(result["chip"])
        found = [m for m in msgs if m.startswith("ERROR")]
        if not self.support["path_blind"]:
            self.assertEqual(found, [], "cad/ reads `path` now, so there is nothing to excuse")
            return
        self.assertTrue(found, "the fixture is meant to detour; a detour cannot be straight")
        # Every complaint is reproducible from the straight-line geometry, with the routed
        # polylines deleted: that is the proof they are not about this chip.
        straight = json.loads(json.dumps(result["chip"]))
        for ch in straight["channels"]:
            ch.pop("path", None)
        straight_msgs, _ = make_figures.geometry_warnings(straight)
        self.assertEqual(found, [m for m in straight_msgs if m.startswith("ERROR")])
        unresolved = result["report"]["unresolved"]
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["count"], len(found))
        self.assertEqual(unresolved[0]["lines"], found)
        self.assertIn("not the routed `path`", unresolved[0]["why"])


# --- 11. the stage: files, rerun_from, idempotence ---------------------------------------

class StageBehaviour(unittest.TestCase):
    def test_writes_declares_the_chip_spec(self):
        self.assertIn("chip/{chip_id}.json", s7_route.WRITES)

    def test_run_writes_the_chip_and_the_report(self):
        """With no --id there is no connections file, so the chip's own channels are the
        input - the documented fallback, and how an S10 fix-up re-enters this stage."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s7_feed_fixture.json"
            path.write_text(json.dumps(feed_chip_with_stub_channels()))
            lines = []
            rerun = s7_route.run(chip_file=str(path), loud=False, log=lines.append)
            self.assertIsNone(rerun, "\n".join(lines))
            out = json.loads(path.read_text())
            self.assertEqual(len(out["channels"]), len(feed_connections()))
            self.assertTrue(all(ch.get("path") for ch in out["channels"]))
            self.assertEqual(schema_io.schema_errors("chip_spec", out), [])
            self.assertEqual(planarity.errors(planarity.check(out)), [])

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s7_feed_fixture.json"
            before = json.dumps(feed_chip_with_stub_channels())
            path.write_text(before)
            s7_route.run(chip_file=str(path), dry_run=True, loud=False, log=lambda _l: None)
            self.assertEqual(path.read_text(), before)

    def test_routing_twice_gives_the_same_answer(self):
        a = route(feed_chip(), feed_connections())["chip"]
        b = route(feed_chip(), feed_connections())["chip"]
        self.assertEqual(a["channels"], b["channels"])

    def test_the_stage_never_moves_a_part(self):
        before = feed_chip()
        result = route(before, feed_connections())
        for key in ("chambers", "ports", "footprint", "layers"):
            self.assertEqual(result["chip"][key], before[key],
                             f"S7 changed {key}: placement is S6's, not S7's")

    def test_run_returns_rerun_from_for_an_unroutable_board(self):
        spec = chip("s7_sealed_fixture", [
            chamber("wall", 15.0, 10.0, 1.0, 20.0),
            chamber("west", 6.0, 10.0, 4.0, 4.0),
            chamber("east", 24.0, 10.0, 4.0, 4.0)], [])
        spec["channels"] = [{"id": "ch_x", "from": "west", "to": "east", "width_mm": 0.22,
                             "depth_mm": 0.3, "carries": "liquid"}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s7_sealed_fixture.json"
            path.write_text(json.dumps(spec))
            rerun = s7_route.run(chip_file=str(path), loud=False, log=lambda _l: None)
            self.assertEqual(rerun["rerun_from"], "s6")
            self.assertIn("ch_x", rerun["reason"])
            self.assertEqual(json.loads(path.read_text())["channels"], spec["channels"],
                             "a failed route must not half-write the chip")

    def test_a_chip_with_no_connections_is_routed_as_empty(self):
        result = route(feed_chip(), [])
        self.assertTrue(result["ok"])
        self.assertEqual(result["chip"]["channels"], [])

    def test_the_report_records_the_algorithm_and_the_contract_fingerprint(self):
        rep = route(feed_chip(), feed_connections())["report"]
        self.assertIn("A*", rep["algorithm"]["name"])
        self.assertIn("visibility", rep["algorithm"]["why_not_visibility_graph"])
        self.assertEqual(rep["contracts"]["min_wall_mm"], MIN_WALL)
        self.assertEqual(set(rep["contracts"]["fingerprint"]),
                         set(contracts.fingerprint()))


if __name__ == "__main__":
    unittest.main()

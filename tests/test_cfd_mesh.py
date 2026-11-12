"""assay2chip/cfd/mesh.py and model.py: the CFD mesh, against the analytic answers.

    python -m unittest discover -s tests -v
    python -m unittest tests.test_cfd_mesh -v

A mesh nobody has checked is worse than no mesh, so the two checks that matter most are
done against closed-form results rather than against a previous run:

  * a straight duct, and two in series, reproduce R = 12 mu L / (w h^3 (1 - 0.63 h/w));
  * a chamber fed across its full width reproduces the Hele-Shaw slab R = 12 mu L/(h^3 W),
    which is what says the raster, the faces and the mouths are wired up right;
  * every chamber's cell areas add up to its footprint area to better than 1%.

The little dense solver below belongs to the tests: solve.py is another module's job, and
a mesh check that depended on it would not be a mesh check. numpy only, no scipy.
"""

from __future__ import annotations

import json
import math
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "operator")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import flow_calc  # noqa: E402
import network as nw  # noqa: E402
import schema_io  # noqa: E402

from assay2chip.lib import contracts  # noqa: E402
from assay2chip.cfd import mesh as cfd_mesh  # noqa: E402
from assay2chip.cfd.model import (  # noqa: E402
    Field,
    Finding,
    Mesh,
    PortState,
    Scenario,
    UL_MIN_TO_M3_S,
)

MU = contracts.viscosity_for(None).pa_s          # 37 C: the real device, never water
CHIPS = cfd_mesh.repo_chips()
#: Specs that cannot be meshed because the SPEC is broken, with the reason. They are
#: listed, not skipped silently, and `Unmeshable` below asserts the reason is still that
#: one - so a mesher regression cannot hide in here.
BROKEN_SPECS = {
    "e2e_messy_wash_and_lyse_assay_c1.json":
        "its only channel names load_1_in / load_1_out, which are neither a chamber nor "
        "a port in the spec (an S6 generator bug, not a mesh one)",
}
class Vanished(Exception):
    """The spec was deleted while the tests ran. S6 regenerates its e2e_* chips
    continuously, so a per-chip loop must survive one disappearing under it."""


_CACHE: dict[str, object] = {}
_SPECS: dict[Path, object] = {}


def chip_of(path: Path) -> dict:
    """The spec, read ONCE per run - a second read may find it gone or half-written."""
    if path not in _SPECS:
        try:
            _SPECS[path] = schema_io.load_json(path)
        except (OSError, ValueError) as exc:
            _SPECS[path] = Vanished(f"{path.name}: {exc}")
    got = _SPECS[path]
    if isinstance(got, Exception):
        raise got
    return got


def mesh_attempt(path: Path, **kwargs) -> object:
    """One mesh per chip per run, successful or not: chip/ holds ~38 specs, some of them
    12k cells, so re-meshing them all in every test costs minutes."""
    key = f"{path}|{sorted(kwargs.items())}"
    if key not in _CACHE:
        try:
            _CACHE[key] = cfd_mesh.build_mesh(chip_of(path), strict=False, **kwargs)
        except Exception as exc:                            # kept, not swallowed
            _CACHE[key] = exc
    return _CACHE[key]


def meshed(path: Path, **kwargs) -> Mesh:
    got = mesh_attempt(path, **kwargs)
    if isinstance(got, Exception):
        raise got
    return got


def meshable_chips() -> list[Path]:
    return [p for p in CHIPS if p.name not in BROKEN_SPECS]


def each_meshed_chip():
    """(path, chip, mesh) for every spec that is still on disk and meshes."""
    for path in meshable_chips():
        try:
            yield path, chip_of(path), meshed(path)
        except Vanished:
            continue


# --- helpers --------------------------------------------------------------------------


def chip_spec(chambers: list[dict], channels: list[dict], ports: list[dict],
              chip_id: str = "mesh_test_v1") -> dict:
    """A minimal chip spec that passes contracts/schemas/chip_spec.schema.json."""
    return {
        "id": chip_id, "placeholder": True, "units": "mm",
        "footprint": {"x": 30, "y": 20},
        "layers": [{"id": "spacer", "material": "acrylic", "thickness_mm": 0.3}],
        "chambers": chambers, "channels": channels, "ports": ports,
    }


def box(cid: str, cx: float, cy: float, sx: float, sy: float, depth: float = 0.3,
        **extra) -> dict:
    return {"id": cid, "type": "exchange", "center": [cx, cy, depth / 2],
            "size": [sx, sy, depth], "volume_ul": sx * sy * depth, **extra}


def duct(cid: str, src: str, dst: str, width: float = 0.2, depth: float = 0.3,
         **extra) -> dict:
    return {"id": cid, "from": src, "to": dst, "width_mm": width, "depth_mm": depth,
            **extra}


def port(pid: str, x: float, y: float, kind: str = "inlet") -> dict:
    return {"id": pid, "type": kind, "position": [x, y, 0.3]}


def port_to_port_resistance(mesh: Mesh, inlet: str, outlet: str) -> float:
    """dP / Q between two ports, in Pa s / m^3, by solving the conductance network.

    Unit flow into the inlet node, the outlet node held at 0 Pa: the inlet pressure that
    comes back IS the resistance. Dense and slow, which is fine on a test-sized mesh.
    """
    ids = [n.id for n in mesh.nodes]
    idx = {nid: i for i, nid in enumerate(ids)}
    a = np.zeros((len(ids), len(ids)))
    b = np.zeros(len(ids))
    for edge in mesh.edges:
        g = edge.conductance_m3_per_pa_s
        i, j = idx[edge.a], idx[edge.b]
        a[i, i] += g
        a[j, j] += g
        a[i, j] -= g
        a[j, i] -= g
    source, sink = idx[mesh.ports[inlet]], idx[mesh.ports[outlet]]
    b[source] = 1.0
    a[sink, :] = 0.0
    a[sink, sink] = 1.0
    b[sink] = 0.0
    return float(np.linalg.solve(a, b)[source])


def analytic_duct_r(length_mm: float, width_mm: float, depth_mm: float,
                    mu: float = MU) -> float:
    return flow_calc.rect_channel_resistance_pa_s_m3(length_mm, width_mm, depth_mm, mu)


def slab_r(length_mm: float, width_mm: float, depth_mm: float, mu: float = MU) -> float:
    """Hele-Shaw slab: R = 12 mu L / (h^3 W), the depth-averaged plane-Stokes answer."""
    return 12 * mu * (length_mm / 1000) / ((depth_mm / 1000) ** 3 * (width_mm / 1000))


# --- the formula ----------------------------------------------------------------------


class Formulas(unittest.TestCase):
    def test_duct_edge_matches_operator_flow_calc(self):
        for length, width, depth in ((10, 0.8, 0.3), (1.25, 0.14, 0.15), (3.4, 0.2, 0.3)):
            g = cfd_mesh.duct_g_geom_m3(length, width, depth)
            self.assertAlmostEqual(
                (1.0 / (g / MU)) / analytic_duct_r(length, width, depth), 1.0, places=12,
                msg=f"{length} x {width} x {depth} mm",
            )

    def test_h_is_the_smaller_side_whichever_way_round_the_spec_writes_it(self):
        # 0.2 wide x 0.3 deep and 0.3 wide x 0.2 deep are the same duct.
        self.assertAlmostEqual(cfd_mesh.duct_g_geom_m3(5, 0.2, 0.3),
                               cfd_mesh.duct_g_geom_m3(5, 0.3, 0.2), places=18)
        # ... and it is NOT what you get by taking width as w regardless.
        wrong = 12 * 5e-3 / (0.2e-3 * (0.3e-3) ** 3 * (1 - 0.63 * 0.3 / 0.2))
        self.assertNotAlmostEqual(1.0 / cfd_mesh.duct_g_geom_m3(5, 0.2, 0.3), wrong,
                                  delta=1.0)

    def test_hele_shaw_face_is_h_cubed_over_twelve_times_width_over_spacing(self):
        g = cfd_mesh.hs_g_geom_m3(0.3, 0.1, 0.1)
        self.assertAlmostEqual(g, (0.3e-3) ** 3 * 0.1e-3 / (12 * 0.1e-3), places=18)

    def test_every_edge_carries_both_conductances_and_rescales_exactly(self):
        mesh = cfd_mesh.mesh_chip(ROOT / "chip" / "module1_v1.json")
        self.assertAlmostEqual(mesh.mu_pa_s, MU, places=12)
        for edge in mesh.edges:
            self.assertAlmostEqual(edge.conductance_m3_per_pa_s * mesh.mu_pa_s,
                                   edge.g_geom_m3, places=18, msg=edge.id)
        cold = mesh.at_viscosity(2.5e-3)          # cold organic solvent
        for before, after in zip(mesh.edges, cold.edges):
            self.assertEqual(before.g_geom_m3, after.g_geom_m3)
            self.assertAlmostEqual(after.conductance_m3_per_pa_s,
                                   before.conductance_m3_per_pa_s * MU / 2.5e-3,
                                   places=18, msg=before.id)
        self.assertEqual(cold.meta["mu_pa_s"], 2.5e-3)


# --- the two analytic checks the README demands ---------------------------------------


class StraightDuct(unittest.TestCase):
    def test_a_straight_duct_mesh_reproduces_the_analytic_resistance(self):
        spec = chip_spec(
            [box("j_1", 15, 10, 0.3, 0.3)],        # a point feature: no resistance of its own
            [duct("ch_a", "p_in", "j_1"), duct("ch_b", "j_1", "p_out")],
            [port("p_in", 5, 10), port("p_out", 25, 10, "outlet")],
        )
        self.assertEqual(schema_io.schema_errors("chip_spec", spec), [])
        mesh = cfd_mesh.build_mesh(spec)
        r_mesh = port_to_port_resistance(mesh, "p_in", "p_out")
        expect = sum(analytic_duct_r(mesh.edge(f"d:{c}").length_mm, 0.2, 0.3)
                     for c in ("ch_a", "ch_b"))
        self.assertAlmostEqual(r_mesh / expect, 1.0, places=9)
        # and the lengths are the trimmed centrelines, not centre-to-centre
        self.assertAlmostEqual(mesh.edge("d:ch_a").length_mm, 15 - 0.15 - (5 + 0.4), places=9)

    def test_two_ducts_in_parallel_halve_the_resistance(self):
        spec = chip_spec(
            [box("j_1", 10, 10, 0.3, 0.3), box("j_2", 20, 10, 0.3, 0.3)],
            [duct("ch_in", "p_in", "j_1"),
             duct("ch_p1", "j_1", "j_2"), duct("ch_p2", "j_1", "j_2"),
             duct("ch_out", "j_2", "p_out")],
            [port("p_in", 5, 10), port("p_out", 25, 10, "outlet")],
        )
        mesh = cfd_mesh.build_mesh(spec)
        r = port_to_port_resistance(mesh, "p_in", "p_out")
        legs = [analytic_duct_r(mesh.edge(f"d:{c}").length_mm, 0.2, 0.3)
                for c in ("ch_in", "ch_out")]
        pair = analytic_duct_r(mesh.edge("d:ch_p1").length_mm, 0.2, 0.3)
        self.assertAlmostEqual(r / (sum(legs) + pair / 2), 1.0, places=9)

    def test_channel_lengths_agree_with_operator_network(self):
        """operator/network.py is an independent implementation of the same trim rule."""
        chip = schema_io.load_json(ROOT / "chip" / "module1_v1.json")
        mesh = cfd_mesh.build_mesh(chip)
        geometry = nw.channel_geometry(chip)       # no DXF: spec, straight, trimmed
        for channel in chip["channels"]:
            mine = mesh.edge(f"d:{channel['id']}")
            theirs = geometry[channel["id"]]
            self.assertAlmostEqual(mine.length_mm, theirs["length_mm"], places=9,
                                   msg=channel["id"])
            self.assertAlmostEqual(
                mine.resistance_pa_s_m3,
                nw.rect_resistance(theirs["length_mm"], theirs["width_mm"],
                                   theirs["depth_mm"], MU),
                delta=1.0, msg=channel["id"])


class HeleShawChamber(unittest.TestCase):
    def test_a_chamber_fed_across_its_full_width_reproduces_the_slab_resistance(self):
        # Channels as wide as the wall they meet, so the feed is uniform and the answer
        # is the textbook slab: R = 12 mu L / (h^3 W), here L = 2 mm, W = 1 mm.
        spec = chip_spec(
            [box("cx_1", 15, 10, 2.0, 1.0)],
            [duct("ch_in", "p_in", "cx_1", width=1.0),
             duct("ch_out", "cx_1", "p_out", width=1.0)],
            [port("p_in", 10, 10), port("p_out", 20, 10, "outlet")],
        )
        mesh = cfd_mesh.build_mesh(spec)
        ducts = sum(analytic_duct_r(mesh.edge(f"d:{c}").length_mm, 1.0, 0.3)
                    for c in ("ch_in", "ch_out"))
        r_chamber = port_to_port_resistance(mesh, "p_in", "p_out") - ducts
        self.assertAlmostEqual(r_chamber / slab_r(2.0, 1.0, 0.3), 1.0, places=6)

    def test_cell_areas_add_up_to_the_footprint_area(self):
        spec = chip_spec(
            [box("cx_1", 10, 10, 4.171, 2.503),
             {"id": "round_1", "type": "well", "shape": "cylinder",
              "center": [20, 10, 0.15], "size": [2.0, 2.0, 0.3], "volume_ul": 0.942}],
            [duct("ch_in", "p_in", "cx_1"), duct("ch_mid", "cx_1", "round_1"),
             duct("ch_out", "round_1", "p_out")],
            [port("p_in", 5, 10), port("p_out", 25, 10, "outlet")],
        )
        mesh = cfd_mesh.build_mesh(spec)
        self.assertAlmostEqual(mesh.area_mm2("cx_1"), 4.171 * 2.503, places=9)
        self.assertAlmostEqual(mesh.area_mm2("round_1") / (math.pi * 1.0 ** 2), 1.0,
                               delta=0.01)

    def test_a_narrow_chamber_is_refined_to_at_least_eight_cells_across(self):
        spec = chip_spec(
            [box("slot_1", 15, 10, 6.0, 0.5)],     # 0.5 mm narrow: coarser than 8 x 0.1
            [duct("ch_in", "p_in", "slot_1"), duct("ch_out", "slot_1", "p_out")],
            [port("p_in", 5, 10), port("p_out", 25, 10, "outlet")],
        )
        mesh = cfd_mesh.build_mesh(spec)
        note = next(f for f in mesh.meta["features"] if f["feature_id"] == "slot_1")
        self.assertGreaterEqual(note["ny"], cfd_mesh.MIN_CELLS_ACROSS)
        self.assertLessEqual(note["grid_mm"][1], cfd_mesh.DEFAULT_GRID_MM)
        self.assertAlmostEqual(mesh.area_mm2("slot_1"), 3.0, places=9)

    def test_a_chamber_is_not_one_node(self):
        spec = chip_spec(
            [box("cx_1", 15, 10, 4.0, 2.5)],
            [duct("ch_in", "p_in", "cx_1"), duct("ch_out", "cx_1", "p_out")],
            [port("p_in", 5, 10), port("p_out", 25, 10, "outlet")],
        )
        mesh = cfd_mesh.build_mesh(spec)
        self.assertGreater(len(mesh.cells_of("cx_1")), 100)


# --- channel-like blocks: the 43x error -----------------------------------------------


class ChannelLikeBlocks(unittest.TestCase):
    def test_a_serpentine_chamber_is_a_duct_at_its_lane_width(self):
        mesh = cfd_mesh.mesh_chip(ROOT / "chip" / "cell_prep_v1.json")
        self.assertEqual(mesh.cells_of("mix_1"), [])          # not a 2D chamber
        edge = mesh.edge("d:mix_1")
        self.assertEqual(edge.kind, "duct")
        self.assertAlmostEqual(edge.width_mm, 0.2, places=9)   # mixer.channel_width_mm
        self.assertAlmostEqual(edge.length_mm, 12.0, places=9)  # mixer.length_mm
        # the bounding box would have been 1.947 mm wide: ~10x, and shear goes with it
        note = next(f for f in mesh.meta["features"] if f["feature_id"] == "mix_1")
        self.assertGreater(note["width_error_if_boxed"], 5)

    def test_every_channel_like_block_in_the_repo_is_a_duct(self):
        seen = 0
        for path, chip, mesh in each_meshed_chip():
            for chamber in chip["chambers"]:
                block = str(chamber.get("block", ""))
                if not (block.endswith("_channel")
                        or block in cfd_mesh.CHANNEL_LIKE_BLOCKS):
                    continue
                if block in cfd_mesh.NOT_CHANNEL_LIKE_BLOCKS:
                    continue
                seen += 1
                self.assertEqual(mesh.cells_of(chamber["id"]), [], msg=chamber["id"])
                self.assertEqual(mesh.edge(f"d:{chamber['id']}").kind, "duct")
        # cell_prep mix_1 (serpentine_channel) and spc_demo delay_1 (serpentine_channel)
        self.assertGreaterEqual(seen, 2)

    def test_a_stated_lane_is_used_as_stated_and_needs_no_assumption(self):
        """spc_demo_v1 now STATES delay_1's lane (0.3 mm x 104 mm, volume 4.68 uL), so the
        mesher must read it rather than fold the bounding box - and must stop flagging."""
        mesh = cfd_mesh.mesh_chip(ROOT / "chip" / "spc_demo_v1.json")
        chamber = next(c for c in schema_io.load_json(ROOT / "chip" / "spc_demo_v1.json")
                       ["chambers"] if c["id"] == "delay_1")
        edge = mesh.edge("d:delay_1")
        self.assertAlmostEqual(edge.width_mm, chamber["mixer"]["channel_width_mm"], places=9)
        self.assertAlmostEqual(edge.length_mm, chamber["mixer"]["length_mm"], places=9)
        self.assertLess(edge.width_mm, min(chamber["size"][:2]))    # not the box
        note = next(f for f in mesh.meta["features"] if f["feature_id"] == "delay_1")
        self.assertIn("spec mixer", note["lane_source"])
        self.assertGreater(note["width_error_if_boxed"], 30)        # the 43x class of error
        self.assertEqual(
            [u for u in mesh.meta["unresolved"]
             if u["feature_id"] == "delay_1" and "lane" in u["question"]], [])

    def test_an_assumed_lane_width_is_flagged_not_silently_used(self):
        """The fallback path, on a spec that states no lane: fold at the fabrication
        floors, and say so. (No repo chip needs this now that spc_demo_v1 is fixed, which
        is exactly why it is kept alive on a fixture.)"""
        spec = chip_spec(
            [box("ser_1", 15, 10, 11.0, 13.0, depth=0.15, block="serpentine_channel")],
            [duct("ch_in", "p_in", "ser_1"), duct("ch_out", "ser_1", "p_out")],
            [port("p_in", 5, 10), port("p_out", 25, 10, "outlet")],
        )
        mesh = cfd_mesh.build_mesh(spec)
        edge = mesh.edge("d:ser_1")
        self.assertLessEqual(edge.width_mm, contracts.floor("min_open_channel_mm"))
        self.assertGreater(edge.length_mm, 50.0)   # a folded path, not an 11 mm box
        flags = [u for u in mesh.meta["unresolved"] if u["feature_id"] == "ser_1"]
        self.assertTrue(flags, "an assumed lane width must be recorded in unresolved")
        self.assertIn("bounding-box", flags[0]["question"])

    def test_a_routed_channel_follows_its_path(self):
        chip = schema_io.load_json(ROOT / "chip" / "sctip_spc_v1.json")
        mesh = cfd_mesh.build_mesh(chip, strict=False)
        for channel in chip["channels"]:
            if not channel.get("path"):
                continue
            want = cfd_mesh.polyline_length_mm(channel["path"])
            self.assertAlmostEqual(mesh.edge(f"d:{channel['id']}").length_mm, want,
                                   places=9, msg=channel["id"])
            straight = math.dist(tuple(channel["path"][0]), tuple(channel["path"][-1]))
            if len(channel["path"]) > 2:
                self.assertGreater(want, straight)   # a straight line would be wrong


# --- routing: a feature in series must be meshed in series ----------------------------


def port_flows(mesh: Mesh, feeds: dict[str, float], vent: str) -> dict[str, float]:
    """Edge flows (m^3/s, signed a->b) with `feeds` injected and `vent` held at 0 Pa."""
    ids = [n.id for n in mesh.nodes]
    idx = {nid: i for i, nid in enumerate(ids)}
    a = np.zeros((len(ids), len(ids)))
    b = np.zeros(len(ids))
    for edge in mesh.edges:
        g = edge.conductance_m3_per_pa_s
        i, j = idx[edge.a], idx[edge.b]
        a[i, i] += g
        a[j, j] += g
        a[i, j] -= g
        a[j, i] -= g
    for pid, q_ul_min in feeds.items():
        b[idx[mesh.ports[pid]]] += q_ul_min * UL_MIN_TO_M3_S
    sink = idx[mesh.ports[vent]]
    a[sink, :] = 0.0
    a[sink, sink] = 1.0
    b[sink] = 0.0
    p = np.linalg.solve(a, b)
    return {e.id: (p[idx[e.a]] - p[idx[e.b]]) * e.conductance_m3_per_pa_s
            for e in mesh.edges}


class Routing(unittest.TestCase):
    """The delay-line bug: `ch_xj_delay` and `ch_delay_out` both attached to
    `j:delay_1:a`, so the 234 mm serpentine hung off one node as a dead-end stub, carried
    1.8e-19 of the through-flow, and spc_demo_v1's 1194 Pa (duct law) / 1136 Pa (OpenFOAM)
    came out of the reduced model as 39 Pa."""

    def test_no_repo_chip_has_a_bypassed_feature(self):
        for path, _chip, mesh in each_meshed_chip():
            with self.subTest(chip=path.name):
                self.assertEqual(cfd_mesh.routing_problems(mesh), [])

    def test_a_feature_the_spec_puts_in_series_is_meshed_in_series(self):
        for path, _chip, mesh in each_meshed_chip():
            for feature, info in mesh.meta["routing"].items():
                if len(info["neighbours"]) < 2:
                    continue
                with self.subTest(chip=mesh.chip_id, feature=feature):
                    self.assertTrue(info["in_series"])
                    # each end carries its own neighbours: nothing shares an end with
                    # the channel on the other side of the run
                    ends = list(info["by_end"].values())
                    self.assertTrue(all(ends))
                    self.assertEqual(set(ends[0]) & set(ends[1]), set())

    def test_the_spc_demo_delay_line_carries_the_whole_through_flow(self):
        mesh = cfd_mesh.mesh_chip(ROOT / "chip" / "spc_demo_v1.json")
        feed = 10.0
        flows = port_flows(mesh, {"in_oil": feed / 3, "in_shell": feed / 3,
                                  "in_core": feed / 3}, "out_caps")
        edge = mesh.edge("d:delay_1")
        q = abs(flows[edge.id])
        self.assertAlmostEqual(q / (feed * UL_MIN_TO_M3_S), 1.0, places=9)
        # ... and the pressure drop is now the duct law, not 1/30th of it
        self.assertAlmostEqual(
            q * edge.resistance_pa_s_m3,
            q * analytic_duct_r(edge.length_mm, edge.width_mm, edge.depth_mm),
            places=9)
        self.assertNotEqual(mesh.edge("d:ch_xj_delay").b, mesh.edge("d:ch_delay_out").a)

    def test_a_serpentine_in_series_carries_everything_that_crosses_it(self):
        mesh = cfd_mesh.mesh_chip(ROOT / "chip" / "cell_prep_v1.json")
        flows = port_flows(mesh, {"in_buffer": 5.0}, "out_cells")
        self.assertAlmostEqual(abs(flows["d:mix_1"]) / (5.0 * UL_MIN_TO_M3_S), 1.0,
                               places=9)

    def test_two_channels_on_a_square_run_do_not_share_an_end(self):
        # The geometry that caused it: a square-ish footprint whose two neighbours are
        # exactly equidistant from both long-axis extremes, so a nearest-end rule ties
        # and sends both to the same node.
        spec = chip_spec(
            [box("ser_1", 15, 10, 6.0, 6.0, block="serpentine_channel",
                 mixer={"kind": "serpentine", "channel_width_mm": 0.2, "length_mm": 80.0})],
            [duct("ch_in", "p_in", "ser_1"), duct("ch_out", "ser_1", "p_out")],
            [port("p_in", 5, 10), port("p_out", 25, 10, "outlet")],
        )
        mesh = cfd_mesh.build_mesh(spec)           # strict: would raise if it stubbed
        self.assertEqual(cfd_mesh.routing_problems(mesh), [])
        self.assertNotEqual(mesh.edge("d:ch_in").b, mesh.edge("d:ch_out").a)
        flows = port_flows(mesh, {"p_in": 3.0}, "p_out")
        self.assertAlmostEqual(abs(flows["d:ser_1"]) / (3.0 * UL_MIN_TO_M3_S), 1.0,
                               places=9)
        # the run really is the 80 mm lane, and it dominates the two 0.2 mm feeds
        self.assertAlmostEqual(mesh.edge("d:ser_1").length_mm, 80.0, places=9)

    def test_the_check_fires_on_a_stub_and_build_mesh_refuses_it(self):
        """The check itself, on a mesh deliberately wired the broken way."""
        mesh = cfd_mesh.mesh_chip(ROOT / "chip" / "spc_demo_v1.json")
        broken = Mesh.from_json(json.loads(json.dumps(mesh.to_json())))
        stub = broken.edge("d:ch_delay_out")
        stub.a = broken.edge("d:ch_xj_delay").b         # both on j:delay_1:a again
        problems = cfd_mesh.routing_problems(broken)
        self.assertTrue(problems)
        self.assertIn("DEAD-END STUB", problems[0])
        self.assertIn("delay_1", problems[0])
        self.assertFalse(cfd_mesh.routing(broken)["delay_1"]["in_series"])


# --- weirs ----------------------------------------------------------------------------


class Weirs(unittest.TestCase):
    def setUp(self):
        self.mesh = cfd_mesh.mesh_chip(ROOT / "chip" / "module1_v1.json")

    def test_a_weir_chamber_gets_a_slit_at_the_spec_gap_across_its_full_width(self):
        slits = self.mesh.slit_edges()
        self.assertTrue(slits)
        widths = {round(s.width_mm, 4) for s in slits}
        self.assertEqual(widths, {2.503, 4.171})   # the two wall lengths of g_cx / a_cx
        for slit in slits:
            self.assertEqual(slit.gap_um, 25.0)    # retention.opening_um
            self.assertAlmostEqual(
                slit.length_mm,
                contracts.block("retention.weir_geometry.crest_thickness_um") / 1000.0,
                places=9)
            self.assertIn(slit.feature_id, ("g_cx", "a_cx"))

    def test_a_chamber_with_no_retention_has_no_slit(self):
        self.assertEqual([s for s in self.mesh.slit_edges() if s.feature_id == "n_cx"], [])

    def test_slit_flow_scales_with_the_cube_of_the_gap(self):
        """+/-20 um on a 25 um gap is +/-80%, which is a factor 6 in flow either way.

        The sweep rebuilds the duct expression rather than cubing by hand, so the ratio is
        the cube to within the (1 - 0.63 h/w) correction - a few parts in a thousand on a
        crest this wide, and exactly the expression for the gap, which is also checked.
        """
        for delta in (-20.0, -5.0, 5.0, 20.0):
            swept = self.mesh.with_slit_gap_delta(delta)
            for before, after in zip(self.mesh.slit_edges(), swept.slit_edges()):
                self.assertAlmostEqual(after.gap_um, before.gap_um + delta, places=9)
                ratio = after.conductance_m3_per_pa_s / before.conductance_m3_per_pa_s
                cube = ((25.0 + delta) / 25.0) ** 3
                self.assertAlmostEqual(ratio, cube, delta=0.01 * cube)
                self.assertAlmostEqual(
                    after.g_geom_m3,
                    cfd_mesh.slit_g_geom_m3(after.width_mm, after.gap_um, after.length_mm),
                    places=24)

    def test_a_tolerance_that_closes_the_crest_shuts_the_edge(self):
        shut = self.mesh.with_slit_gap_delta(-25.0)
        for slit in shut.slit_edges():
            self.assertEqual(slit.conductance_m3_per_pa_s, 0.0)
            self.assertIn("SHUT", slit.note)

    def test_the_crest_thickness_fallback_is_recorded(self):
        flags = [u for u in self.mesh.meta["unresolved"] if "crest_thickness" in u["question"]]
        self.assertTrue(flags)
        self.assertIn("blocks.json", flags[0]["assumed"])


# --- mouths ---------------------------------------------------------------------------


class Mouths(unittest.TestCase):
    def test_a_head_on_channel_spreads_over_its_own_width(self):
        mesh = cfd_mesh.mesh_chip(ROOT / "chip" / "module1_v1.json")
        mouths = [e for e in mesh.edges if e.kind == "mouth" and e.id.startswith("m:ch_in_n:")]
        self.assertTrue(mouths)
        self.assertAlmostEqual(sum(e.width_mm for e in mouths), 0.2, places=6)

    def test_a_spine_running_along_a_wall_feeds_the_whole_wall(self):
        # Module 6's comb spine is parallel to the 8 mm culture field edge: a point mouth
        # there would invent a dead zone over the whole field.
        mesh = cfd_mesh.mesh_chip(ROOT / "chip" / "module6_v1.json")
        mouths = [e for e in mesh.edges
                  if e.kind == "mouth" and e.id.startswith("m:comb_in_n:")]
        self.assertAlmostEqual(sum(e.width_mm for e in mouths), 8.0, places=6)
        self.assertEqual(len(mouths), len({e.b for e in mouths}))
        self.assertGreaterEqual(len(mouths), 80)

    def test_a_weir_opening_covers_the_whole_wall_even_for_a_narrow_channel(self):
        mesh = cfd_mesh.mesh_chip(ROOT / "chip" / "module1_v1.json")
        mouths = [e for e in mesh.edges
                  if e.kind == "mouth" and e.id.startswith("m:ch_g_out:")]
        self.assertAlmostEqual(sum(e.width_mm for e in mouths), 2.503, places=6)


# --- ports ----------------------------------------------------------------------------


class Ports(unittest.TestCase):
    def test_every_port_in_every_repo_chip_is_a_node_and_is_connected(self):
        for path, chip, mesh in each_meshed_chip():
            for p in chip["ports"]:
                self.assertIn(p["id"], mesh.ports, msg=f"{path.name}: {p['id']}")
                node = mesh.ports[p["id"]]
                self.assertTrue(mesh.edges_at(node), msg=f"{path.name}: {p['id']} orphan")

    def test_a_port_with_no_channel_is_joined_to_what_its_bore_opens_onto(self):
        spec = chip_spec(
            [box("cx_1", 15, 10, 4.0, 2.5)],
            [duct("ch_out", "cx_1", "p_out")],
            [port("p_top", 15, 10), port("p_out", 25, 10, "outlet")],
        )
        mesh = cfd_mesh.build_mesh(spec)
        bore = [e for e in mesh.edges_at(mesh.ports["p_top"])]
        self.assertEqual(len(bore), 1)
        self.assertEqual(bore[0].kind, "mouth")
        self.assertEqual(bore[0].feature_id, "p_top")
        self.assertEqual(mesh.unreachable(), [])

    def test_the_port_bore_comes_from_the_contract(self):
        spec = chip_spec([box("j_1", 15, 10, 0.3, 0.3)],
                         [duct("ch_a", "p_in", "j_1"), duct("ch_b", "j_1", "p_out")],
                         [port("p_in", 5, 10), port("p_out", 25, 10, "outlet")])
        mesh = cfd_mesh.build_mesh(spec)
        bore = contracts.floor("min_port_mm")
        self.assertAlmostEqual(mesh.node(mesh.ports["p_in"]).area_mm2,
                               math.pi * (bore / 2) ** 2, places=12)


# --- every repo chip ------------------------------------------------------------------


class Unmeshable(unittest.TestCase):
    """The specs that cannot be meshed, and the reason, pinned so a mesher regression
    cannot quietly join the list - and so a fixed spec drops off it."""

    def test_exactly_these_specs_are_unmeshable_and_the_reason_is_the_spec(self):
        failed = {p.name: str(got) for p in CHIPS
                  if isinstance(got := mesh_attempt(p), Exception)
                  and not isinstance(got, Vanished)}
        self.assertEqual(sorted(failed), sorted(BROKEN_SPECS), msg=str(failed))
        for name, reason in failed.items():
            # the message must name the chip and what is missing, not just fail
            self.assertIn("neither a chamber nor a port", reason, msg=name)
            self.assertIn(name.removesuffix(".json"), reason)

    def test_a_channel_naming_a_missing_feature_is_refused_up_front(self):
        spec = chip_spec(
            [box("cx_1", 15, 10, 4.0, 2.5)],
            [duct("ch_x", "ghost_in", "ghost_out", path=[[2.0, 2.0], [6.0, 2.0]])],
            [port("p_in", 5, 10), port("p_out", 25, 10, "outlet")],
        )
        with self.assertRaises(cfd_mesh.MeshError) as caught:
            cfd_mesh.build_mesh(spec)
        self.assertIn("ghost_in", str(caught.exception))
        self.assertIn("ghost_out", str(caught.exception))


class RepoChips(unittest.TestCase):
    def test_there_is_a_chip_to_mesh(self):
        self.assertGreaterEqual(len(CHIPS), 9)

    def test_every_repo_chip_meshes_and_the_mesh_is_sound(self):
        """Sound means the MESHER wired the spec up, which is not the same as the DESIGN
        being connected: a chamber with no channel on it (chip/s6_selftest_c*.json have
        none at all) really is unreachable, and graph reachability is exactly how the
        README's `dead_end` findings are made. So a node no port can reach is allowed
        only when the SPEC explains it - and the mesh must have written it down."""
        for path, chip, mesh in each_meshed_chip():
            with self.subTest(chip=path.name):
                # An orphan node is a mesher fault unless the SPEC joins its feature to
                # nothing at all, where a lone point feature honestly has no edge.
                isolated = set(mesh.meta["isolated_features"])
                for nid in mesh.orphans():
                    self.assertIn(mesh.node(nid).feature_id, isolated, msg=f"{path.name}")
                for feature in isolated:
                    self.assertNotIn(feature,
                                     {c["from"] for c in chip["channels"]}
                                     | {c["to"] for c in chip["channels"]},
                                     msg=f"{path.name}: {feature} has a channel on it")
                    self.assertTrue([u for u in mesh.meta["unresolved"]
                                     if u["feature_id"] == feature], msg=feature)
                # the mesher's own verdict: empty means strict mode accepts this chip,
                # so a design dead end never gets a sound chip refused
                self.assertEqual(mesh.meta["structural_problems"], [])
                self.assertGreater(len(mesh.nodes), len(mesh.ports))
                self.assertEqual(mesh.meta["mu_pa_s"], MU)

                attached = ({c["from"] for c in chip["channels"]}
                            | {c["to"] for c in chip["channels"]})
                dead = sorted({mesh.node(n).feature_id for n in mesh.unreachable()})
                self.assertEqual(dead, mesh.meta["dead_end_features"])
                for feature in dead:
                    self.assertNotIn(
                        feature, attached,
                        msg=f"{path.name}: {feature} has a channel on it, so a port not "
                            f"reaching it is a mesher bug, not a design dead end")
                    self.assertTrue(
                        [u for u in mesh.meta["unresolved"]
                         if u["feature_id"] == feature and "no path" in u["question"]],
                        msg=f"{path.name}: {feature} is unreachable and unrecorded")
                # every port itself must still be joined to something
                for p in chip["ports"]:
                    self.assertTrue(mesh.edges_at(mesh.ports[p["id"]]))

    def test_every_chamber_cell_area_is_within_one_percent_of_its_footprint(self):
        for path, _chip, mesh in each_meshed_chip():
            for note in mesh.meta["features"]:
                if note["model"] != "hele_shaw":
                    continue
                with self.subTest(chip=mesh.chip_id, feature=note["feature_id"]):
                    self.assertAlmostEqual(note["area_ratio"], 1.0, delta=0.01)
                    self.assertAlmostEqual(
                        mesh.area_mm2(note["feature_id"]) / note["footprint_area_mm2"],
                        1.0, delta=0.01)

    def test_meshing_is_deterministic(self):
        for path in meshable_chips()[:3]:
            chip = chip_of(path)
            one = cfd_mesh.build_mesh(chip, strict=False).to_json()
            two = cfd_mesh.build_mesh(chip, strict=False).to_json()
            self.assertEqual(json.dumps(one, sort_keys=True), json.dumps(two, sort_keys=True),
                             msg=path.name)

    def test_a_coarser_grid_still_tiles_the_same_footprint(self):
        fine = cfd_mesh.mesh_chip(ROOT / "chip" / "module1_v1.json")
        coarse = cfd_mesh.mesh_chip(ROOT / "chip" / "module1_v1.json", grid_mm=0.25)
        self.assertLess(len(coarse.nodes), len(fine.nodes))
        self.assertAlmostEqual(coarse.area_mm2("n_cx"), fine.area_mm2("n_cx"), places=6)

    def test_what_could_not_be_meshed_is_written_down(self):
        for path, _chip, mesh in each_meshed_chip():
            for entry in mesh.meta["unresolved"]:
                self.assertIn("feature_id", entry)
                self.assertIn("question", entry)
        # the ones we know about, so a regression that silently drops them is caught
        m6 = cfd_mesh.mesh_chip(ROOT / "chip" / "module6_v1.json")
        self.assertTrue(any("centipede" in u["question"] for u in m6.meta["unresolved"]))
        m3 = cfd_mesh.mesh_chip(ROOT / "chip" / "module3_v1_translated.json")
        self.assertTrue(any("filter" in u["question"] for u in m3.meta["unresolved"]))

    def test_the_per_feature_grids_the_other_modules_read_are_published(self):
        """meta.grids: particles.py and figures.py walk a chamber through this."""
        for path, _chip, mesh in each_meshed_chip():
            grids = mesh.meta["grids"]
            for note in mesh.meta["features"]:
                cid = note["feature_id"]
                with self.subTest(chip=mesh.chip_id, feature=cid):
                    grid = grids[cid]
                    self.assertIn(grid["kind"], ("grid", "duct", "junction"))
                    self.assertTrue(grid["nodes"])
                    for nid in grid["nodes"]:
                        self.assertEqual(mesh.node(nid).feature_id, cid)
                    if grid["kind"] == "grid":
                        self.assertEqual(len(grid["ids"]), grid["nx"])
                        self.assertEqual(len(grid["ids"][0]), grid["ny"])
                        self.assertEqual(len(grid["nodes"]),
                                         len(mesh.cells_of(cid)))
                        self.assertEqual(grid["depth_mm"], note["depth_mm"])

    def test_where_every_weir_crest_sits_is_published(self):
        """meta.weir_sides: which wall, which opening, what gap - for particles.py."""
        mesh = cfd_mesh.mesh_chip(ROOT / "chip" / "module1_v1.json")
        sides = mesh.meta["weir_sides"]
        self.assertEqual(sorted(sides), ["a_cx", "g_cx"])
        slits = {s.id for s in mesh.slit_edges()}
        for cid, openings in sides.items():
            for opening in openings:
                self.assertIn(opening["slit_edge"], slits)
                self.assertEqual(opening["gap_um"], 25.0)
                self.assertIn(opening["wall"], ("x_min", "x_max", "y_min", "y_max", "rim"))
        self.assertEqual(mesh.meta["weir_sides"].get("n_cx"), None)

    def test_the_mesh_round_trips_through_json(self):
        mesh = cfd_mesh.mesh_chip(ROOT / "chip" / "module1_v1.json")
        again = Mesh.from_json(json.loads(json.dumps(mesh.to_json())))
        self.assertEqual(len(again.nodes), len(mesh.nodes))
        self.assertEqual(again.ports, mesh.ports)
        self.assertEqual(again.check(), [])
        self.assertEqual(again.slit_edges()[0].gap_um, mesh.slit_edges()[0].gap_um)
        self.assertAlmostEqual(again.edges[0].conductance_m3_per_pa_s,
                               mesh.edges[0].conductance_m3_per_pa_s, places=18)


# --- the shapes the other nine modules code to ----------------------------------------


class ModelShapes(unittest.TestCase):
    def test_a_mesh_knows_what_it_is(self):
        mesh = cfd_mesh.mesh_chip(ROOT / "chip" / "module1_v1.json")
        stats = mesh.stats()
        self.assertEqual(stats["chip_id"], "module1_v1")
        self.assertEqual(stats["nodes"], len(mesh.nodes))
        self.assertEqual(sum(stats["nodes_by_kind"].values()), len(mesh.nodes))
        self.assertEqual(sum(stats["edges_by_kind"].values()), len(mesh.edges))
        self.assertGreater(stats["wetted_volume_ul"], 0)

    def test_a_scenario_is_checked_against_the_mesh_it_drives(self):
        mesh = cfd_mesh.mesh_chip(ROOT / "chip" / "module1_v1.json")
        scenario = Scenario(step=1, ports={"p_in": PortState.flow(11.0),
                                           "p_mid": PortState.vent()},
                            fluid="medium", temp_c=37.0).for_mesh(mesh)
        self.assertEqual(len(scenario.ports), len(mesh.ports))
        self.assertEqual(scenario.closed(), ["p_a_in", "p_a_out", "p_g_in", "p_g_out"])
        self.assertAlmostEqual(scenario.q_in_m3_s, 11.0 * UL_MIN_TO_M3_S, places=18)
        with self.assertRaises(Exception):
            Scenario(step=2, ports={"nope": PortState.vent()}).for_mesh(mesh)

    def test_a_scenario_accepts_plain_json_from_a_protocol(self):
        scenario = Scenario.from_json({
            "step": 3, "fluid": "cold rinse", "temp_c": 4,
            "ports": {"p_in": {"type": "flow", "q_ul_min": 2.0},
                      "p_mid": {"type": "pressure", "p_pa": 0},
                      "p_g_in": {"type": "closed"}},
        })
        self.assertEqual(scenario.ports["p_in"].q_ul_min, 2.0)
        self.assertTrue(scenario.ports["p_g_in"].is_closed)
        self.assertEqual(Scenario.from_json(scenario.to_json()).to_json(),
                         scenario.to_json())

    def test_a_field_round_trips_and_reports_its_own_validity(self):
        field = Field(p_pa={"a": 120.0}, q_m3_s={"d:ch": 1.0e-9},
                      u_mm_s={"c:x:0:0": [1.0, -2.0]}, re={"d:ch": 13.0, "d:ok": 0.2},
                      residual={"mass_imbalance_max": 1e-18, "q_in": 1e-9, "q_out": 1e-9},
                      meta={"chip_id": "module6_v1", "step": 4})
        self.assertEqual(field.over_re(), ["d:ch"])
        self.assertAlmostEqual(field.speed_mm_s("c:x:0:0"), math.hypot(1.0, 2.0), places=12)
        self.assertEqual(field.residual.closure, 0.0)
        self.assertEqual(Field.from_json(field.to_json()).to_json(), field.to_json())

    def test_a_finding_writes_class_not_cls(self):
        finding = Finding(id="shear_1", cls="shear", severity="blocker", chip_id="module1_v1",
                          feature_id="n_cx", metric="wall_shear", value=0.43, limit=0.01,
                          units="dyn/cm2", message="43x the culture ceiling")
        data = finding.to_json()
        self.assertEqual(data["class"], "shear")
        self.assertNotIn("cls", data)
        self.assertEqual(Finding.from_json(data).cls, "shear")
        self.assertAlmostEqual(finding.over_by, 43.0, places=9)
        with self.assertRaises(Exception):
            Finding(id="x", cls="shear", severity="catastrophic")

    def test_the_viscosity_is_the_contract_s_and_never_a_literal(self):
        mesh = cfd_mesh.mesh_chip(ROOT / "chip" / "module1_v1.json", temp_c=20.0)
        self.assertEqual(mesh.meta["mu_pa_s"], contracts.viscosity_water_20c_pa_s().pa_s)
        self.assertIn("fluid.viscosity_pa_s", mesh.meta["mu_source"])
        warm = cfd_mesh.mesh_chip(ROOT / "chip" / "module1_v1.json", temp_c=37.0)
        self.assertEqual(warm.meta["mu_pa_s"], contracts.viscosity_37c_pa_s().pa_s)
        self.assertNotEqual(warm.meta["mu_pa_s"], mesh.meta["mu_pa_s"])


if __name__ == "__main__":
    unittest.main()

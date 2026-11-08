"""Port pads, channel paths and the frozen printed chips.

Every tool used to clip a channel at the edge of its port's 0.8 mm bore square, so the lid
hole opened onto 0 mm2 of channel. A port may now declare a `pad` (chip schema): a cut
feature in the fluidic layer under the bore that its channels run into. These tests hold
cad/make_train_cad.py, cad/make_figures.py, tools/check_chip_spec.py and operator/network.py
to ONE geometry for it, and to one centreline for a channel with a `path`; and they prove the
six printed chips' exports are untouched.

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("cad", "tools", "operator"):
    sys.path.insert(0, str(ROOT / sub))

import check_chip_spec as ccs  # noqa: E402
import make_figures as mf  # noqa: E402
import make_train_cad as mtc  # noqa: E402
import network as nw  # noqa: E402

BLOCKS = json.loads((ROOT / "contracts" / "blocks.json").read_text())
WALL = BLOCKS["fabrication_floors"]["min_wall_mm"]
PRINTED = ["module1_v1", "spc_demo_v1", "module3_v1_translated", "module4_v1", "module5_v1", "module6_v1"]
PATHED = {"module3_v1_translated", "module6_v1"}     # their channels declare a non-straight path


def fixture(pad_p=None, pad_q=None, chip_id="pad_fixture"):
    """30 x 20 mm, one chamber between two ports, one 0.2 mm channel each side."""
    def port(pid, x, pad):
        p = {"id": pid, "type": "inlet", "position": [x, 10.0, 6.3]}
        if pad:
            p["pad"] = pad
        return p
    return {
        "id": chip_id, "name": "pad fixture", "footprint": {"x": 30, "y": 20},
        "layers": [{"id": "base", "material": "coc", "thickness_mm": 3.0},
                   {"id": "spacer", "material": "coc", "thickness_mm": 0.3},
                   {"id": "lid", "material": "coc", "thickness_mm": 3.0}],
        "chambers": [{"id": "c", "type": "exchange", "center": [15.0, 10.0, 3.15], "size": [4.0, 4.0, 0.3],
                      "volume_ul": 4.8}],
        "ports": [port("p", 5.0, pad_p), port("q", 25.0, pad_q)],
        "channels": [{"id": "ch_p", "from": "p", "to": "c", "width_mm": 0.2, "depth_mm": 0.3, "carries": "liquid"},
                     {"id": "ch_q", "from": "c", "to": "q", "width_mm": 0.2, "depth_mm": 0.3, "carries": "liquid"}],
    }


SQUARE = {"shape": "square", "size_mm": 1.2}
ROUND = {"shape": "round", "size_mm": 1.4}


def quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*a, **k)


def build_into(chip: dict, tmp: Path) -> Path:
    spec = tmp / f"{chip['id']}.json"
    spec.write_text(json.dumps(chip))
    code = quiet(mtc.build, spec, exports=tmp / "exports")
    assert code == 0, code
    return tmp / "exports" / chip["id"]


def closed_polys(path: Path) -> dict[str, list]:
    tokens = path.read_text().split("\n")
    out, cur, layer = {}, None, None
    for code, val in zip(tokens[0::2], tokens[1::2]):
        if code == "0":
            if cur is not None:
                out.setdefault(layer, []).append(cur)
            cur, layer = ([] if val == "LWPOLYLINE" else None), None
        elif cur is not None and code == "8":
            layer = val
        elif cur is not None and code == "10":
            cur.append([float(val), None])
        elif cur is not None and code == "20":
            cur[-1][1] = float(val)
    return out


class PadGeometry(unittest.TestCase):
    def test_square_and_round_pads(self):
        sq = mtc.pad_polygon({"position": [5, 10, 0], "pad": SQUARE})
        self.assertEqual(len(sq), 4)
        self.assertAlmostEqual(mtc.polygon_area(sq), 1.44)
        rd = mtc.pad_polygon({"position": [5, 10, 0], "pad": ROUND})
        self.assertEqual(len(rd), mtc.PAD_ROUND_SEGMENTS)
        self.assertTrue(all(abs(math.dist(v, (5, 10)) - 0.7) < 1e-12 for v in rd))
        self.assertIsNone(mtc.pad_polygon({"position": [5, 10, 0]}))

    def test_a_padded_channel_stops_on_the_pad_boundary(self):
        chip = fixture(SQUARE, ROUND)
        lines = dict((ch["id"], pts) for ch, pts in mtc.centrelines(chip))
        self.assertAlmostEqual(lines["ch_p"][0][0], 5.0 + 0.6)          # square pad edge
        self.assertAlmostEqual(lines["ch_q"][-1][0], 25.0 - 0.7)        # round pad vertex at angle pi
        bare = dict((ch["id"], pts) for ch, pts in mtc.centrelines(fixture()))
        self.assertAlmostEqual(bare["ch_p"][0][0], 5.0 + mtc.PORT_SIZE_MM / 2)   # unchanged without a pad


class CadExport(unittest.TestCase):
    def test_pads_are_cut_in_the_fluidic_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = build_into(fixture(SQUARE, ROUND), Path(tmp))
            self.assertTrue((out / "p_pad.stl").is_file() and (out / "q_pad.stl").is_file())
            stl = (out / "p_pad.stl").read_text()
            zs = {float(l.split()[3]) for l in stl.splitlines() if l.strip().startswith("vertex")}
            self.assertEqual(zs, {3.0, 3.3})                 # the spacer's slice of the stack
            ref = (out / "pad_fixture.dxf").read_text()
            self.assertIn("PADS", ref)
            polys = closed_polys(out / "pad_fixture_closed.dxf")
            self.assertEqual(len(polys["PADS"]), 2)
            self.assertEqual(mtc.outline_crossings({k: [[tuple(v) for v in p] for p in ps]
                                                    for k, ps in polys.items()}), [])

    def test_the_cut_channel_meets_the_pad_edge_to_edge(self):
        chip = fixture(SQUARE, ROUND)
        pads, anchors = mtc.port_pads(chip), mtc.anchors_of(chip)
        for ch, pts in mtc.centrelines(chip):
            _, cut = mtc.channel_cut(ch, pts, {"c"}, pads, anchors)
            pid = ch["from"] if ch["from"] in pads else ch["to"]
            pad = pads[pid]
            on = [v for v in cut if self._on_boundary(v, pad)]
            self.assertGreaterEqual(len(on), 2, ch["id"])     # the whole cap lies on the pad's boundary
            for v in cut:
                self.assertFalse(mtc.inside_convex(v, pad, -1e-9), f"{ch['id']} cuts into its pad at {v}")

    @staticmethod
    def _on_boundary(v, pad):
        return any(abs((b[0] - a[0]) * (v[1] - a[1]) - (b[1] - a[1]) * (v[0] - a[0])) < 1e-9
                   and min(a[0], b[0]) - 1e-9 <= v[0] <= max(a[0], b[0]) + 1e-9
                   and min(a[1], b[1]) - 1e-9 <= v[1] <= max(a[1], b[1]) + 1e-9
                   for a, b in mtc.polygon_lines(pad))

    def test_the_bore_opens_onto_the_pad(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = build_into(fixture(SQUARE, ROUND), Path(tmp))
            polys, src = None, None
            ccs_dxf = out / "pad_fixture.dxf"
            # Read the exported reference drawing the way the overlap rule does.
            orig = ccs.ROOT
            try:
                ccs.ROOT = Path(tmp)
                (Path(tmp) / "cad").mkdir()
                (Path(tmp) / "cad" / "exports").symlink_to(out.parent)
                polys, src = ccs._dxf_outlines("pad_fixture")
            finally:
                ccs.ROOT = orig
            self.assertTrue(src.endswith("pad_fixture.dxf") and ccs_dxf.exists())
            rows = {r["port"]: r for r in ccs.port_overlaps(fixture(SQUARE, ROUND), polys=polys, source=src)}
            for pid in ("p", "q"):
                self.assertAlmostEqual(rows[pid]["overlap_mm2"], 0.64, delta=0.01)
            bare = {r["port"]: r for r in ccs.port_overlaps(fixture(), polys=ccs._spec_outlines(fixture()))}
            self.assertLess(bare["p"]["overlap_mm2"], bare["p"]["need_mm2"])


class Figures(unittest.TestCase):
    def errors(self, chip):
        return [m for m in mf.geometry_warnings(chip)[0] if m.startswith("ERROR")]

    def test_a_port_over_its_own_pad_is_clean(self):
        self.assertEqual(self.errors(fixture(SQUARE, ROUND)), [])

    def test_a_pad_still_may_not_touch_an_unconnected_chamber_or_channel(self):
        chip = fixture({"shape": "square", "size_mm": 1.2})
        chip["chambers"].append({"id": "d", "type": "exchange", "center": [5.0, 11.0, 3.15],
                                 "size": [1.0, 1.0, 0.3], "volume_ul": 0.3})
        self.assertTrue(any("pad is cut into a chamber" in e for e in self.errors(chip)))
        chip = fixture(SQUARE)
        chip["channels"].append({"id": "ch_x", "from": "c", "to": "q", "width_mm": 0.2, "depth_mm": 0.3,
                                 "path": [[15.0, 10.0], [15.0, 10.5], [5.0, 10.5], [5.0, 18.0], [25.0, 18.0],
                                          [25.0, 10.0]]})
        self.assertTrue(any("'p' and channel 'ch_x'" in e for e in self.errors(chip)))

    def test_without_a_pad_every_check_is_as_before(self):
        chip = fixture()
        chip["chambers"].append({"id": "d", "type": "exchange", "center": [5.0, 10.9, 3.15],
                                 "size": [1.0, 1.0, 0.3], "volume_ul": 0.3})
        self.assertTrue(any("drilled into a chamber" in e for e in self.errors(chip)))


class Paths(unittest.TestCase):
    def detour(self):
        """A channel routed AROUND chamber 'block' (an L), whose straight run would cross it."""
        chip = fixture(SQUARE, ROUND)
        chip["chambers"].append({"id": "block", "type": "exchange", "center": [10.0, 10.0, 3.15],
                                 "size": [1.0, 1.0, 0.3], "volume_ul": 0.3})
        chip["channels"][0]["path"] = [[5.0, 10.0], [5.0, 14.0], [15.0, 14.0], [15.0, 10.0]]
        return chip

    def test_every_tool_follows_the_path(self):
        chip = self.detour()
        pts = dict((ch["id"], p) for ch, p in mtc.centrelines(chip))["ch_p"]
        self.assertEqual(len(pts), 4)
        self.assertAlmostEqual(pts[0][1], 10.6)                           # leaves the pad upwards
        self.assertAlmostEqual(pts[-1][1], 12.0)                          # stops on the chamber wall
        drawn = [(a, b) for ch, a, b, _ in mf.segments_of(chip) if ch["id"] == "ch_p"]
        self.assertEqual(drawn, list(zip(pts, pts[1:])))
        self.assertEqual(ccs.channel_segments(chip)["ch_p"], [(tuple(a), tuple(b)) for a, b in zip(pts, pts[1:])])
        g = nw.channel_geometry(chip)["ch_p"]
        self.assertAlmostEqual(g["length_mm"], sum(math.dist(a, b) for a, b in zip(pts, pts[1:])))
        # The detour misses 'block'; the straight run it replaced would have cut through it.
        self.assertFalse(any("'block'" in m for m in mf.geometry_warnings(chip)[0] if m.startswith("ERROR")))
        straight = self.detour()
        del straight["channels"][0]["path"]
        self.assertTrue(any("'block'" in m for m in mf.geometry_warnings(straight)[0] if m.startswith("ERROR")))

    def test_a_pathed_channel_is_one_closed_outline(self):
        chip = self.detour()
        with tempfile.TemporaryDirectory() as tmp:
            out = build_into(chip, Path(tmp))
            polys = closed_polys(out / "pad_fixture_closed.dxf")
            self.assertEqual(len(polys["CHANNELS"]), 2)
            self.assertEqual(mtc.outline_crossings({k: [[tuple(v) for v in p] for p in ps]
                                                    for k, ps in polys.items()}), [])
            # Reference drawing: one 4-LINE rectangle per segment, 3 for the L plus 1.
            self.assertEqual(len(nw.dxf_channel_segments(out / "pad_fixture.dxf")), 4)


class CheckChipSpec(unittest.TestCase):
    def run_check(self, chip):
        f = ccs.Findings()
        ccs.check_port_pads(chip, f, BLOCKS)
        return f.errors

    def test_a_good_pad_passes(self):
        self.assertEqual(self.run_check(fixture(SQUARE, ROUND)), [])

    def test_a_pad_must_cover_the_bore(self):
        errs = self.run_check(fixture({"shape": "square", "size_mm": 0.6}, {"shape": "round", "size_mm": 1.0}))
        self.assertEqual(sum("does not cover" in e for e in errs), 2)

    def test_a_pad_must_clear_unconnected_features_by_the_min_wall(self):
        chip = fixture(SQUARE)
        chip["chambers"].append({"id": "d", "type": "exchange", "center": [5.0, 10.6 + (WALL - 0.1) + 0.5, 3.15],
                                 "size": [1.0, 1.0, 0.3], "volume_ul": 0.3})
        self.assertTrue(any("under the" in e and "chamber 'd'" in e for e in self.run_check(chip)))
        chip["chambers"][-1]["center"][1] = 10.6 + (WALL + 0.1) + 0.5
        self.assertEqual(self.run_check(chip), [])

    def test_a_pad_no_channel_reaches_is_an_error(self):
        chip = fixture(SQUARE)
        chip["channels"] = chip["channels"][1:]
        self.assertTrue(any("no channel runs into it" in e for e in self.run_check(chip)))


class Network(unittest.TestCase):
    def test_the_port_connects_through_its_pad(self):
        chip = fixture(SQUARE, ROUND)
        net = nw.Network(chip)
        self.assertIn("p", net.nodes)
        self.assertIn("q", net.nodes)
        sol = net.solve({"p": ("FEED", 5.0), "q": "VENT"})
        self.assertAlmostEqual(sol["ports"]["q"]["into_chip_ul_min"], -5.0, places=6)
        g = nw.channel_geometry(chip)
        self.assertAlmostEqual(g["ch_p"]["length_mm"], 13.0 - 5.6)


class TargetChip(unittest.TestCase):
    """chip/module1_target_v1.json, the full-spec Module 1: padded through its generator."""
    CHIP = json.loads((ROOT / "chip" / "module1_target_v1.json").read_text())

    def test_every_port_has_a_pad(self):
        for p in self.CHIP["ports"]:
            self.assertEqual(p.get("pad", {}).get("shape"), "square", p["id"])

    def test_zero_errors_on_every_tool(self):
        params = json.loads((ROOT / "contracts" / "parameters.json").read_text())
        materials = json.loads((ROOT / "contracts" / "materials.json").read_text())["materials"]
        fab = json.loads((ROOT / "cad" / "fabrication.json").read_text())
        f = ccs.check(self.CHIP, [], params, materials, fab, ccs.downstream_nozzles(self.CHIP), BLOCKS)
        self.assertEqual(f.errors, [])
        self.assertEqual(len([o for o in f.ok if o.startswith("port overlap")]), len(self.CHIP["ports"]))
        self.assertEqual([m for m in mf.geometry_warnings(self.CHIP)[0] if m.startswith("ERROR")], [])
        net = nw.Network(self.CHIP, dxf=ROOT / "cad" / "exports" / "module1_target_v1" / "module1_target_v1.dxf")
        self.assertEqual({g["source"] for g in net.geometry.values()}, {"DXF"})
        self.assertTrue(set(p["id"] for p in self.CHIP["ports"]) <= set(net.nodes))

    def test_the_committed_exports_are_what_the_generator_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = ROOT / "chip" / "module1_target_v1.json"
            quiet(mtc.build, spec, exports=Path(tmp))
            fresh = Path(tmp) / "module1_target_v1"
            disk = ROOT / "cad" / "exports" / "module1_target_v1"
            names = sorted(p.name for p in fresh.iterdir() if p.suffix in (".stl", ".dxf"))
            self.assertEqual(names, sorted(p.name for p in disk.iterdir() if p.suffix in (".stl", ".dxf")))
            for n in names:
                self.assertEqual((fresh / n).read_bytes(), (disk / n).read_bytes(), n)


class FrozenChips(unittest.TestCase):
    def test_the_printed_chips_are_the_frozen_list(self):
        self.assertEqual(sorted(mtc.frozen_chips()), sorted(PRINTED))

    def test_the_real_exports_are_refused_without_force(self):
        before = {p: p.read_bytes() for p in (ROOT / "cad" / "exports" / "module1_v1").glob("*.dxf")}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = mtc.build(ROOT / "chip" / "module1_v1.json")
        self.assertEqual(code, 3)
        self.assertIn("FROZEN", buf.getvalue())
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_regenerating_a_printed_chip_is_byte_identical(self):
        """No printed chip declares a pad, so the pad work cannot touch them: regenerated into a
        scratch folder, every export is byte-for-byte what is on disk. module3_v1_translated and
        module6_v1 declare channel paths, which the generator now honours, so their two DXFs
        differ - which is exactly why the real folder is refused - while every STL matches."""
        for cid in PRINTED:
            with self.subTest(chip=cid), tempfile.TemporaryDirectory() as tmp:
                chip = json.loads((ROOT / "chip" / f"{cid}.json").read_text())
                self.assertFalse(any("pad" in p for p in chip["ports"]))
                quiet(mtc.build, ROOT / "chip" / f"{cid}.json", exports=Path(tmp))
                fresh, disk = Path(tmp) / cid, ROOT / "cad" / "exports" / cid
                for p in sorted(disk.iterdir()):
                    if p.suffix not in (".stl", ".dxf") or p.name.endswith("_asbuilt.dxf"):
                        continue
                    same = hashlib.sha256(p.read_bytes()).digest() == hashlib.sha256((fresh / p.name).read_bytes()).digest()
                    if cid in PATHED and p.suffix == ".dxf":
                        self.assertFalse(same, f"{p.name}: expected the path to change it")
                    else:
                        self.assertTrue(same, p.name)


if __name__ == "__main__":
    unittest.main()

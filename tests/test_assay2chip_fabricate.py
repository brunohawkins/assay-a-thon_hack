"""S11 fabricate: the print bundle, and the guarantees it is allowed to claim.

    python -m unittest tests.test_assay2chip_fabricate -v

Offline and deterministic: no network, no Fusion, no CAD kernel, no timestamps. Every
chip spec already in chip/ is exercised, so a new chip is covered automatically, the same
way tests/test_envelope.py does it.

What is actually being pinned down here, in order of how much it would cost to get wrong:

  1. Both STLs are closed solids at the right size and in the right place. A mesh with a
     naked edge slices into a part with holes in it, and a 25.4x unit error prints a chip
     the size of a paperback.
  2. A routed channel is built along its `path`. Straightening a bend is silent: the file
     opens, it prints, and the chip is wrong.
  3. The mould is the body's inverse. A tool that is not the complement of the part
     presses the wrong chip.
  4. The DXF declares millimetres and is closed contours, which is the repo-wide fault
     this stage was asked to stop repeating.
  5. A chip that fails a fabrication floor does not quietly become an STL.
"""

from __future__ import annotations

import json
import py_compile
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "chip" / "components"), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import stl_io  # noqa: E402
from assay2chip.fabricate import (  # noqa: E402
    body as body_mod, bundle, checks, dxf, geometry, mesh, render,
)
from assay2chip.fabricate import mould as mould_mod  # noqa: E402
from assay2chip.lib import contracts  # noqa: E402
from assay2chip.stages import s11_fabricate  # noqa: E402

SPECS = {p.stem: json.loads(p.read_text()) for p in sorted((ROOT / "chip").glob("*.json"))
         if {"id", "footprint", "chambers", "channels", "ports", "layers"}
         <= set(json.loads(p.read_text()))}
MAIN = "module6_v1"          # small, routed, three lanes: the quick one to work against
TOL = 1e-6


def geom_of(chip_id):
    return geometry.plan_geometry(SPECS[chip_id])


def l_shaped_chip():
    """A chip whose one channel is routed round a corner, with nothing on the chord.

    The bend is what proves S11 follows `path`: a builder that drew a straight line
    between the endpoints would cut the middle of the chip and leave the corner empty,
    which is the opposite of this geometry.
    """
    return {
        "id": "l_route_test", "units": "mm",
        "footprint": {"x": 30, "y": 20},
        "layers": [{"id": "base", "material": "coc", "thickness_mm": 0.5},
                   {"id": "fluidic", "material": "coc", "thickness_mm": 0.3},
                   {"id": "lid", "material": "coc", "thickness_mm": 1.0}],
        "chambers": [{"id": "ch_a", "type": "culture", "center": [24, 4, 0.65],
                      "size": [4, 4, 0.3], "volume_ul": 4.8, "block": "polygon_chamber"}],
        "channels": [{"id": "ch_bend", "from": "p_in", "to": "ch_a",
                      "width_mm": 0.6, "depth_mm": 0.3, "block": "channel_corner",
                      "path": [[4, 4], [4, 16], [24, 16], [24, 6]]}],
        "ports": [{"id": "p_in", "type": "inlet", "position": [4, 4, 1.8], "width_um": 800},
                  {"id": "p_out", "type": "outlet", "position": [24, 4, 1.8],
                   "width_um": 800}],
    }


# --- 1. the STLs ------------------------------------------------------------------------

class Meshes(unittest.TestCase):
    """Every chip on disk, body and mould, against the five STL guarantees."""

    def test_every_chip_body_and_mould_are_closed_solids(self):
        self.assertTrue(SPECS, "no chip specs found")
        for chip_id, spec in SPECS.items():
            with self.subTest(chip=chip_id):
                geom = geometry.plan_geometry(spec)
                mgeom = mould_mod.geometry_for(spec, geom)
                built = body_mod.build(spec, geom)
                tool = mould_mod.build(spec, mgeom)
                for name, part, z_top in (("body", built, built["z_top_mm"]),
                                          ("mould", tool, tool["z_top_mm"])):
                    report = mesh.validate(part["triangles"], spec["footprint"],
                                           part["z0_mm"], z_top, f"{chip_id}_{name}")
                    bad = {k: v["detail"] for k, v in report["checks"].items()
                           if not v["passed"]}
                    self.assertEqual(bad, {}, f"{chip_id} {name}")

    def test_bounding_box_is_the_spec_footprint_in_millimetres(self):
        spec = SPECS[MAIN]
        built = body_mod.build(spec, geom_of(MAIN))
        lo, hi = mesh.bbox(built["triangles"])
        self.assertAlmostEqual(hi[0] - lo[0], float(spec["footprint"]["x"]), delta=TOL)
        self.assertAlmostEqual(hi[1] - lo[1], float(spec["footprint"]["y"]), delta=TOL)
        self.assertEqual([round(v, 9) for v in lo[:2]], [0.0, 0.0], "not at the spec origin")
        self.assertAlmostEqual(lo[2], 0.0, delta=TOL)
        self.assertAlmostEqual(hi[2], built["z_top_mm"], delta=TOL)

    def test_body_thickness_is_the_specs_own_layer_stack(self):
        spec = SPECS[MAIN]
        built = body_mod.build(spec, geom_of(MAIN))
        base = next(l for l in spec["layers"] if l["id"] == "base")["thickness_mm"]
        fluidic = next(l for l in spec["layers"] if l["thickness_mm"] == 0.3)
        self.assertAlmostEqual(built["z_top_mm"], base + fluidic["thickness_mm"], delta=TOL)
        self.assertAlmostEqual(built["recess_floor_mm"], base, delta=TOL)
        self.assertEqual(built["stack"]["gaps"], [])

    def test_validator_catches_a_hole_and_an_inside_out_mesh(self):
        """The acceptance test has to be able to fail, or it is decoration."""
        spec = SPECS[MAIN]
        tris = body_mod.build(spec, geom_of(MAIN))["triangles"]

        holed = mesh.validate(tris[:-1], spec["footprint"], 0.0, name="holed")
        self.assertFalse(holed["checks"]["watertight"]["passed"])
        self.assertFalse(holed["passed"])

        flipped = [(t[0], t[2], t[1]) for t in tris]
        report = mesh.validate(flipped, spec["footprint"], 0.0, name="flipped")
        self.assertFalse(report["checks"]["outward_normals"]["passed"])

    def test_a_failing_mesh_is_never_written(self):
        spec = SPECS[MAIN]
        tris = body_mod.build(spec, geom_of(MAIN))["triangles"][:-1]
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "broken.stl"
            with self.assertRaises(mesh.MeshNotPrintable):
                mesh.write_validated(dest, tris, spec["footprint"], 0.0)
            self.assertFalse(dest.exists(), "a mesh that failed validation left a file")

    def test_stl_survives_the_round_trip_through_the_file(self):
        """Validate what a printer will read, not only what was in memory."""
        spec = SPECS[MAIN]
        built = body_mod.build(spec, geom_of(MAIN))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / f"{MAIN}_body.stl"
            mesh.write_validated(dest, built["triangles"], spec["footprint"], 0.0,
                                 built["z_top_mm"])
            back = stl_io.read_stl(dest)
            self.assertEqual(len(back), len(built["triangles"]))
            report = mesh.validate(back, spec["footprint"], 0.0, built["z_top_mm"])
            self.assertTrue(report["passed"], report["checks"])


# --- 2. routed paths --------------------------------------------------------------------

class RoutedPaths(unittest.TestCase):

    def test_a_bent_channel_is_cut_at_its_bend_and_not_across_the_chord(self):
        spec = l_shaped_chip()
        geom = geometry.plan_geometry(spec)
        for x, y, what in ((4, 10, "the first leg"), (14, 16, "the top leg"),
                           (24, 10, "the last leg")):
            self.assertGreater(geometry.depth_at(geom, x, y), 0,
                               f"{what} of the routed path was not cut at ({x}, {y})")
        for x, y in ((14, 9), (10, 7), (18, 11)):
            self.assertEqual(geometry.depth_at(geom, x, y), 0.0,
                             f"({x}, {y}) is on the straight line between the channel's "
                             f"endpoints and was cut: the route was straightened")

    def test_every_run_of_every_repo_path_is_built(self):
        for chip_id, spec in SPECS.items():
            routed = [c for c in spec["channels"] if c.get("path")]
            if not routed:
                continue
            with self.subTest(chip=chip_id):
                geom = geometry.plan_geometry(spec)
                for channel in routed:
                    pts = channel["path"]
                    for a, b in zip(pts, pts[1:]):
                        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
                        self.assertGreater(
                            geometry.depth_at(geom, mx, my), 0,
                            f"{chip_id} channel '{channel['id']}': the run "
                            f"{a} -> {b} is not cut at its midpoint")


# --- 3. the mould -----------------------------------------------------------------------

class Mould(unittest.TestCase):

    def test_mould_is_the_inverse_of_the_body_for_every_chip(self):
        for chip_id, spec in SPECS.items():
            with self.subTest(chip=chip_id):
                geom = geometry.plan_geometry(spec)
                result = mould_mod.is_inverse_of(geom, mould_mod.geometry_for(spec, geom))
                self.assertTrue(result["passed"], result["detail"])

    def test_mould_is_raised_where_the_body_is_cut(self):
        spec = SPECS[MAIN]
        geom = geometry.plan_geometry(spec)
        mgeom = mould_mod.geometry_for(spec, geom)
        built = body_mod.build(spec, geom)
        tool = mould_mod.build(spec, mgeom)
        self.assertLess(built["recess_floor_mm"], built["z_top_mm"], "the body is not cut")
        self.assertGreater(tool["z_top_mm"], tool["plate_mm"], "the mould has no relief")
        self.assertAlmostEqual(tool["relief_mm"], geom["max_depth_mm"], delta=TOL)
        self.assertTrue(tool["mirrored"], "chip_from_spec.MIRROR_MOULD says to mirror it")

    def test_the_inverse_check_can_fail(self):
        spec = SPECS[MAIN]
        geom = geometry.plan_geometry(spec)
        mgeom = mould_mod.geometry_for(spec, geom)
        mgeom["depths"][0][0] = 99.0
        self.assertFalse(mould_mod.is_inverse_of(geom, mgeom)["passed"])


# --- 4. the DXF -------------------------------------------------------------------------

def dxf_pairs(text):
    tokens = text.split("\n")
    return list(zip((t.strip() for t in tokens[0::2]), (t.strip() for t in tokens[1::2])))


class Dxf(unittest.TestCase):

    def setUp(self):
        self.spec = SPECS[MAIN]
        self.geom = geom_of(MAIN)
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / f"{MAIN}.dxf"
        self.report = dxf.write_for(self.path, self.spec, self.geom)
        self.text = self.path.read_text()
        self.pairs = dxf_pairs(self.text)
        self.addCleanup(self.tmp.cleanup)

    def test_header_declares_millimetres(self):
        """The whole point: an unstated $INSUNITS lets an importer scale the part 25.4x."""
        self.assertIn(("2", "HEADER"), self.pairs, "there is no HEADER section at all")
        at = self.pairs.index(("9", "$INSUNITS"))
        self.assertEqual(self.pairs[at + 1], ("70", "4"), "$INSUNITS must be 4 = mm")
        self.assertLess(at, self.pairs.index(("2", "ENTITIES")),
                        "$INSUNITS must be in the HEADER, before the entities")
        self.assertEqual(self.pairs[self.pairs.index(("9", "$MEASUREMENT")) + 1],
                         ("70", "1"), "$MEASUREMENT must be 1 = metric")
        self.assertEqual(self.report["insunits"], dxf.INSUNITS_MM)

    def test_every_outline_is_one_closed_polyline_and_no_loose_lines(self):
        kinds = [v for k, v in self.pairs if k == "0"]
        self.assertNotIn("LINE", kinds, "channels came out as loose LINE entities again")
        self.assertGreater(kinds.count("LWPOLYLINE"), 0)
        closed = [self.pairs[i + 1] for i, (k, v) in enumerate(self.pairs)
                  if k == "90"]
        self.assertTrue(all(c == ("70", "1") for c in closed),
                        "every LWPOLYLINE must carry the closed flag 70 = 1")
        self.assertEqual(kinds.count("LWPOLYLINE"), self.report["polylines"])

    def test_extents_and_layers(self):
        at = self.pairs.index(("9", "$EXTMAX"))
        self.assertEqual(self.pairs[at + 1], ("10", "30.0000"))
        self.assertEqual(self.pairs[at + 2], ("20", "20.0000"))
        for layer in ("OUTLINE", "CUT", "CHAMBERS", "CHANNELS", "PORTS"):
            self.assertIn(layer, self.report["by_layer"], f"no {layer} layer")

    def test_cut_layer_is_the_traced_region_not_per_feature_boxes(self):
        """Overlapping features must merge, or the cutter runs a line through a pocket."""
        loops = dxf.loops_for(self.spec, self.geom)
        features = len(self.geom["plan"]["features"])
        self.assertLess(len(loops["CUT"]), features,
                        f"CUT has one loop per feature ({features}), so nothing merged: a "
                        f"channel running into a chamber would be cut across the pocket")
        # The contour encloses exactly the area the height field says is cut: the outline
        # and the solid are the same geometry, measured two different ways.
        traced = sum(geometry.loop_area(l) for l in loops["CUT"])
        self.assertAlmostEqual(traced, self.geom["cut_area_mm2"], places=6)


# --- 5. the checks ----------------------------------------------------------------------

class Checks(unittest.TestCase):

    def test_scale_never_lets_a_demo_chip_read_as_the_device(self):
        demo = checks.scale_of({"x": 30, "y": 20})
        self.assertEqual(demo["name"], "demo")
        self.assertFalse(demo["is_target_device"])
        self.assertIn("NOT the device", demo["warning"])
        target = contracts.envelope("target")
        self.assertIn(f"{target.x_mm:g}", demo["warning"],
                      "the warning must say what the real device's footprint is")

        real = checks.scale_of({"x": target.x_mm, "y": target.y_mm})
        self.assertEqual(real["name"], "target")
        self.assertTrue(real["is_target_device"])

    def test_floors_come_from_the_contract_not_from_literals(self):
        geom = geom_of(MAIN)
        mgeom = mould_mod.geometry_for(SPECS[MAIN], geom)
        results = {c["check"]: c for c in checks.run_all(SPECS[MAIN], geom, mgeom)}
        self.assertIn(f"{contracts.floor('min_open_channel_mm') * 1000:g} um",
                      results["min_open_channel"]["detail"])
        self.assertIn(f"{contracts.floor('min_wall_mm') * 1000:g} um",
                      results["min_wall"]["detail"])
        self.assertIn(f"{contracts.floor('min_port_mm'):g} mm",
                      results["min_port_bore"]["detail"])

    def test_walls_are_measured_between_separate_pockets_only(self):
        """An L-shaped route must not be measured against its own other leg."""
        spec = l_shaped_chip()
        wall = checks.narrowest_wall_mm(geometry.plan_geometry(spec))
        self.assertGreaterEqual(wall["mm"], contracts.floor("min_wall_mm") - TOL,
                                f"a self-measurement came back as a wall: {wall}")

    def test_every_repo_chip_clears_the_wall_floor(self):
        floor = contracts.floor("min_wall_mm")
        for chip_id, spec in SPECS.items():
            with self.subTest(chip=chip_id):
                wall = checks.narrowest_wall_mm(geometry.plan_geometry(spec))
                self.assertGreaterEqual(wall["mm"], floor - TOL, wall["where"])

    def test_a_port_below_the_bore_floor_fails(self):
        spec = json.loads(json.dumps(SPECS[MAIN]))
        spec["ports"][0]["width_um"] = contracts.floor("min_port_mm") * 1000 / 2
        geom = geometry.plan_geometry(spec)
        results = {c["check"]: c for c in
                   checks.run_all(spec, geom, mould_mod.geometry_for(spec, geom))}
        self.assertFalse(results["min_port_bore"]["passed"])
        self.assertFalse(results["min_port_bore"]["advisory"])

    def test_an_advisory_is_recorded_but_does_not_block(self):
        geom = geom_of(MAIN)
        mgeom = mould_mod.geometry_for(SPECS[MAIN], geom)
        advisory = [c for c in checks.run_all(SPECS[MAIN], geom, mgeom) if c["advisory"]]
        self.assertTrue(advisory, "no check is marked advisory")
        self.assertIn("port_reached_by_routed_channel", [c["check"] for c in advisory])


# --- 6. the bundle ----------------------------------------------------------------------

class Bundle(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.spec = SPECS[MAIN]
        cls.doc = bundle.write(cls.spec, exports_root=Path(cls.tmp.name))
        cls.out = Path(cls.tmp.name) / MAIN / "print"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_writes_into_a_print_subfolder_and_nowhere_else(self):
        """cad/exports/<chip_id>/ belongs to cad/make_train_cad.py, marker file and all."""
        self.assertEqual(self.out.name, "print")
        siblings = {p.name for p in self.out.parent.iterdir()}
        self.assertEqual(siblings, {"print"},
                         "S11 wrote outside print/, into make_train_cad's folder")

    def test_every_promised_file_is_there_and_hashes_match(self):
        expected = {f"{MAIN}_body.stl", f"{MAIN}_mould.stl", f"{MAIN}.dxf",
                    f"{MAIN}_fusion_rebuild.py", "render_iso.svg", "render_top.svg",
                    "manifest.json", "README.md"}
        self.assertEqual({p.name for p in self.out.iterdir()}, expected)
        from assay2chip.lib.io import sha256_file
        for record in self.doc["files"]:
            path = self.out / record["file"]
            self.assertEqual(record["sha256"], sha256_file(path), record["file"])
            self.assertEqual(record["bytes"], path.stat().st_size)

    def test_manifest_answers_everything_s11_was_asked_for(self):
        m = json.loads((self.out / "manifest.json").read_text())
        self.assertEqual(m["units"], "mm")
        self.assertEqual(m["footprint_mm"], {"x": 30.0, "y": 20.0})
        self.assertEqual(m["depth"]["etch_depth_um"], 300.0)
        self.assertEqual(m["material"]["chip"], "coc")
        self.assertEqual(m["features"]["chambers"], len(self.spec["chambers"]))
        self.assertEqual(m["features"]["channels"], len(self.spec["channels"]))
        self.assertEqual(m["features"]["ports"], len(self.spec["ports"]))
        self.assertFalse(m["scale"]["is_target_device"])
        self.assertEqual(m["scale"]["name"], "demo")
        self.assertEqual(m["checks_failed"], [])
        self.assertTrue(m["all_checks_passed"])
        for route in ("resin_print", "hot_emboss"):
            self.assertIn(route, m["fabrication_routes"])
        self.assertEqual(m["fabrication_routes"]["process"],
                         json.loads((ROOT / "cad" / "fabrication.json").read_text())["process"])
        names = {c["check"] for c in m["checks"]}
        for required in ("envelope_fits", "single_etch_depth", "min_open_channel",
                         "min_wall", "min_port_bore", "min_port_pitch", "port_count",
                         "no_channel_crossings", "mould_is_inverse_of_body",
                         "stl_watertight", "stl_outward_normals", "stl_bbox_is_footprint"):
            self.assertIn(required, names, f"manifest does not record {required}")

    def test_manifest_is_deterministic(self):
        """Same spec in, same bytes out: a diff must mean the geometry changed."""
        first = (self.out / "manifest.json").read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            bundle.write(self.spec, exports_root=Path(tmp))
            again = (Path(tmp) / MAIN / "print" / "manifest.json").read_bytes()
        self.assertEqual(first.replace(str(self.out).encode(), b""),
                         again.replace(str(self.out).encode(), b""))

    def test_the_fusion_script_is_valid_python_and_rebuilds_both_solids(self):
        script = self.out / f"{MAIN}_fusion_rebuild.py"
        py_compile.compile(str(script), doraise=True)
        text = script.read_text()
        self.assertIn("def run(context=None):", text)
        self.assertIn("TemporaryBRepManager", text)
        self.assertIn("DifferenceBooleanType", text)       # the body: pockets cut
        self.assertIn("UnionBooleanType", text)            # the mould: features raised
        self.assertIn("bRepBodies.add", text)
        self.assertNotIn("meshBodies.add", text,
                         "a rebuild must make solids, not import the STLs back as meshes")
        self.assertIn('"dir"', text, "a routed run must keep its direction")
        self.assertIn(self.doc["scale"]["warning"], text,
                      "the rebuild script must carry the scale warning")

    def test_renders_are_well_formed_and_say_what_they_are(self):
        for name, must_say in (("render_top.svg", "top view"),
                               ("render_iso.svg", "isometric")):
            path = self.out / name
            root = ET.fromstring(path.read_text())        # raises if malformed
            self.assertTrue(root.tag.endswith("svg"))
            text = path.read_text()
            self.assertIn(must_say, text)
            self.assertIn("mm", text)
            self.assertIn("DEMO SCALE", text, f"{name} does not carry the scale banner")
            self.assertGreater(text.count("<polygon") + text.count("<path"), 0,
                               f"{name} draws nothing")

    def test_the_top_view_is_true_scale_and_the_iso_says_it_is_not(self):
        top, iso = self.doc["renders"]
        self.assertTrue(top["true_scale"])
        self.assertFalse(iso["true_scale"])
        self.assertGreater(iso["z_exaggeration"], 1.0)
        self.assertIn("EXAGGERATED", (self.out / "render_iso.svg").read_text())
        for report in (top, iso):
            self.assertGreater(report["scale_bar_mm"], 0, "no scale bar")

    def test_the_iso_never_exaggerates_a_pocket_deeper_than_it_is_wide(self):
        """Over-stretching z hides the floor of every narrow channel; that was the bug."""
        geom = geom_of(MAIN)
        exag = render.z_exaggeration_for(geom)
        self.assertLessEqual(exag * geom["max_depth_mm"],
                             render.narrowest_cut_mm(geom) + TOL)
        self.assertLessEqual(exag, render.display_scale())

    def test_readme_names_both_routes_and_the_scale(self):
        text = (self.out / "README.md").read_text()
        self.assertIn("positive embossing mould", text)
        self.assertIn("$INSUNITS = 4", text)
        self.assertIn("DEMO SCALE", text)


# --- 7. the stage -----------------------------------------------------------------------

class Stage(unittest.TestCase):

    def test_writes_list_covers_what_the_bundle_writes(self):
        named = {Path(w.format(chip_id=MAIN)).name for w in s11_fabricate.WRITES}
        with tempfile.TemporaryDirectory() as tmp:
            doc = bundle.write(SPECS[MAIN], exports_root=Path(tmp))
            on_disk = {p.name for p in (Path(tmp) / MAIN / "print").iterdir()}
        self.assertEqual(on_disk - named, set(), "the stage writes files WRITES omits")
        self.assertEqual(doc["chip_id"], MAIN)

    def test_run_against_spec_files_returns_a_bundle_per_chip(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = s11_fabricate.run(chip=[ROOT / "chip" / f"{MAIN}.json"],
                                       exports_root=Path(tmp))
        self.assertEqual([b["chip_id"] for b in result["bundles"]], [MAIN])
        self.assertEqual(result["bundles"][0]["checks_failed"], [])
        self.assertFalse(result["bundles"][0]["is_target_device"])
        self.assertIn("no chip was treated as verified", result["verification_source"])

    def test_a_chip_that_breaks_a_floor_is_refused_unless_forced(self):
        spec = json.loads(json.dumps(SPECS[MAIN]))
        spec["id"] = "too_small_a_port"
        spec["ports"][0]["width_um"] = 100
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(spec))
            with self.assertRaises(s11_fabricate.FabricateError) as caught:
                s11_fabricate.fabricate_one(path, exports_root=Path(tmp))
            self.assertIn("min_port_bore", str(caught.exception))
            forced = s11_fabricate.fabricate_one(path, force=True, exports_root=Path(tmp))
            self.assertIn("min_port_bore", forced["checks_failed"])

    def test_s9s_verdict_is_read_defensively_and_never_assumed(self):
        read = s11_fabricate.verification
        self.assertEqual(read(None), {}, "no assay id must mean no verdict, not a pass")
        self.assertEqual(read("a_run_that_does_not_exist"), {})

    def test_an_unverified_chip_is_skipped_rather_than_fabricated(self):
        from assay2chip.lib import io as stage_io
        work = stage_io.work_dir("s11_unit_test", create=True)
        verify = work / "verify.json"
        verify.write_text(json.dumps(
            {"chips": [{"chip_id": MAIN, "ok": False, "errors": ["a made-up S9 error"]}]}))
        self.addCleanup(verify.unlink)
        try:
            verdicts = s11_fabricate.verification("s11_unit_test")
            self.assertFalse(verdicts[MAIN]["ok"])
        finally:
            pass


if __name__ == "__main__":
    unittest.main()

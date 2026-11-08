"""assay2chip/foam: the pipeline scene's layout, its labels, and the renderer's rules.

    python -m unittest discover -s tests -v
    python -m unittest tests.test_foam_render -v

TWO HALVES, AND THE FIRST ONE NEEDS NOTHING INSTALLED.

Everything that decides WHAT the picture says - the chain order, where each stage sits on
the bench, which stages are placeholders, which badge they wear, where a handoff tube
starts and ends, how long the video will be, and the words SIMULATED / DEMO SCALE /
reduced-order (2.5D) - is pure data in `scene.py` and `render.py`. It is tested here with
no PyVista, no VTK, no OpenFOAM and no ffmpeg, because those tests are the ones that must
keep running in the core repo (foam/README.md: "every test that does not need OpenFOAM
must run without either").

The second half renders real pixels and is skipped cleanly when PyVista is absent.

THE SCENE IS DATA-DRIVEN, AND THAT IS WHAT THESE TESTS PIN. The corrected pipeline file is
coming in a merge - barcoding back between Module 3 and Module 4, and Module 6 - so the
layout is tested against a SYNTHETIC chain as well as the repo's own, and the synthetic
one inserts a stage in the middle to prove the order follows the handoffs rather than a
list anyone has to maintain.
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.foam import render as foam_render  # noqa: E402
from assay2chip.foam import run as foam_run  # noqa: E402
from assay2chip.foam import scene as foam_scene  # noqa: E402

PIPELINE = ROOT / "protocols" / "pipeline_mvp.json"
CHIP = ROOT / "chip" / "module1_v1.json"
PROTOCOL = ROOT / "protocols" / "module1_v1.json"

HAS_PYVISTA = foam_render.have_pyvista()
HAS_FFMPEG = foam_render.ffmpeg() is not None


def _synthetic_chain(tmp: Path, *, with_barcoding: bool,
                     built_for_demo: bool | None = None) -> Path:
    """A minimal pipeline file, optionally with a stage inserted in the middle.

    Deliberately NOT a copy of the repo's: the point is that scene.py reads a chain, not
    `pipeline_mvp.json` in particular.
    """
    modules = [
        {"module": "a", "name": "Stage A", "status": "final",
         "chip": "chip/module1_v1.json", "protocol": "protocols/module1_v1.json"},
        {"module": "b", "name": "Stage B", "status": "final",
         "chip": "chip/cell_prep_v1.json", "protocol": "protocols/cell_prep_v1.json"},
    ]
    if built_for_demo is not None:
        for mod in modules:
            mod["built_for_demo"] = built_for_demo
    handoffs = [{"id": "a_to_b", "carries": "cells",
                 "from": {"module": "a", "port": "p_mid"},
                 "to": {"module": "b", "port": "in_buffer"}}]
    if with_barcoding:
        modules.insert(1, {"module": "mid", "name": "Barcoding: split and pool",
                           "status": "missing",
                           "status_note": "bought kit, done at the bench"})
        handoffs = [
            {"id": "a_to_mid", "carries": "cells",
             "from": {"module": "a", "port": "p_mid"},
             "to": {"module": "mid", "port": "TBC"}},
            {"id": "mid_to_b", "carries": "cells",
             "from": {"module": "mid", "port": "TBC"},
             "to": {"module": "b", "port": "in_buffer"}},
        ]
    doc = {"kind": "pipeline", "id": "synthetic", "modules": modules,
           "handoffs": handoffs,
           "roadmap": [{"id": "out", "name": "Library out", "note": "off-chip"}]}
    path = tmp / "synthetic.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


class LayoutFromTheChainFile(unittest.TestCase):
    """The bench comes from the pipeline file, and only from the pipeline file."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.layout, cls.chips = foam_scene.layout_from_file(PIPELINE)

    def test_every_stage_of_the_chain_is_placed_exactly_once(self) -> None:
        chain = foam_scene.cfd_couple.read_chain(PIPELINE)
        self.assertEqual(sorted(self.layout.stages), sorted(chain.stages))
        self.assertEqual(len(self.layout.order), len(set(self.layout.order)))

    def test_chain_order_follows_the_handoffs(self) -> None:
        """Every handoff points forwards in the order, which is what "chain order" means."""
        chain = foam_scene.cfd_couple.read_chain(PIPELINE)
        rank = {k: i for i, k in enumerate(self.layout.order)}
        for h in chain.handoffs:
            self.assertLess(rank[h.from_stage], rank[h.to_stage],
                            f"{h.id} runs backwards in the laid-out order")

    def test_a_stage_inserted_by_a_merge_is_picked_up_with_no_code_change(self) -> None:
        """The corrected pipeline inserts barcoding; the order must follow the handoffs."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            before, _ = foam_scene.layout_from_file(_synthetic_chain(tmp, with_barcoding=False))
            after, _ = foam_scene.layout_from_file(_synthetic_chain(tmp, with_barcoding=True))
        self.assertEqual([k for k in before.order if k in ("a", "b")], ["a", "b"])
        self.assertEqual([k for k in after.order if k in ("a", "mid", "b")],
                         ["a", "mid", "b"],
                         "a stage inserted between two others must land between them")
        self.assertEqual(len(after.tubes), 2)

    def test_stages_do_not_overlap_on_the_bench(self) -> None:
        places = self.layout.placements()
        for i, p in enumerate(places):
            for q in places[i + 1:]:
                ax0, ax1, ay0, ay1 = p.bounds
                bx0, bx1, by0, by1 = q.bounds
                overlap = (ax0 < bx1 and bx0 < ax1) and (ay0 < by1 and by0 < ay1)
                self.assertFalse(overlap, f"{p.key} overlaps {q.key} on the bench")

    def test_a_chip_block_is_its_real_footprint(self) -> None:
        for place in self.layout.chips():
            chip = foam_render.load_json(ROOT / place.chip_path)
            fx, fy = foam_render.footprint_mm(chip)
            self.assertAlmostEqual(place.w_mm, fx, places=6)
            self.assertAlmostEqual(place.h_mm, fy, places=6)

    def test_every_block_is_inside_the_bench(self) -> None:
        bw, bh = self.layout.bench_mm
        for place in self.layout.placements():
            x0, x1, y0, y1 = place.bounds
            self.assertGreaterEqual(x0, 0.0)
            self.assertGreaterEqual(y0, 0.0)
            self.assertLessEqual(x1, bw + 1e-6, f"{place.key} runs off the bench in x")
            self.assertLessEqual(y1, bh + 1e-6, f"{place.key} runs off the bench in y")


class OffChipStagesAreLabelledPlaceholders(unittest.TestCase):
    """A stage with no chip is a block that says so, not a chip nobody built."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.layout, _ = foam_scene.layout_from_file(PIPELINE)

    def test_a_stage_with_no_chip_is_a_placeholder(self) -> None:
        chain = foam_scene.cfd_couple.read_chain(PIPELINE)
        for key, place in self.layout.stages.items():
            expected = "chip" if chain.stages[key].chip_path else "placeholder"
            self.assertEqual(place.kind, expected, key)

    def test_every_placeholder_is_badged_off_chip(self) -> None:
        placeholders = self.layout.placeholders()
        self.assertTrue(placeholders, "the MVP chain has off-chip stages")
        for place in placeholders:
            self.assertIn(foam_render.OFF_CHIP, place.badge, place.key)

    def test_no_chip_stage_carries_an_off_chip_badge(self) -> None:
        for place in self.layout.chips():
            self.assertEqual(place.badge, "", place.key)

    def test_real_off_chip_work_is_badged_bought_kit(self) -> None:
        """SPRI/QC is work done with bought kit; a DROPPED placeholder is not."""
        spri = next((p for p in self.layout.placeholders() if "spri" in p.key), None)
        self.assertIsNotNone(spri, "the chain has an SPRI/QC roadmap stage")
        self.assertIn(foam_render.BOUGHT_KIT, spri.badge)

    def test_a_dropped_placeholder_is_off_chip_but_not_a_bought_kit(self) -> None:
        chain = foam_scene.cfd_couple.read_chain(PIPELINE)
        dropped = [k for k, s in chain.stages.items() if s.pass_through]
        self.assertTrue(dropped, "the current chain drops barcoding and hairpin")
        for key in dropped:
            badge = self.layout.stages[key].badge
            self.assertEqual(badge, foam_render.OFF_CHIP,
                             f"{key} does nothing to the sample, so it is not a kit")

    def test_the_badge_follows_the_chain_when_a_stage_becomes_real_work(self) -> None:
        """Re-describe the same stage as a bought kit and the badge changes by itself."""
        with tempfile.TemporaryDirectory() as tmp:
            layout, _ = foam_scene.layout_from_file(
                _synthetic_chain(Path(tmp), with_barcoding=True))
        self.assertIn(foam_render.BOUGHT_KIT, layout.stages["mid"].badge)


class ChipsOnThePrinterSaySo(unittest.TestCase):
    """Six demo chips are being printed. Their cards say PRINTED FOR THE DEMO, and a
    render never captions a suggested change to one: improvements belong to the full-spec
    design, not to a chip that is already on the printer."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.layout, _ = foam_scene.layout_from_file(PIPELINE)

    def test_the_print_queue_is_read_from_the_repo_not_listed_here(self) -> None:
        table = foam_render.printed_chips()
        self.assertEqual(len(table), 6, "six demo chips are being printed")
        catalog = (ROOT / "tools" / "module_catalog.py").read_text(encoding="utf-8")
        for chip_rel in table:
            self.assertIn(chip_rel, catalog,
                          "the print queue must come from tools/module_catalog.py")
        self.assertIn("chip/module6_v1.json", table)

    def test_a_printed_chip_is_badged_and_a_twin_only_chip_is_not(self) -> None:
        printed = foam_render.Case(chip=foam_render.load_json(CHIP),
                                   chip_id="module1_v1", chip_path="chip/module1_v1.json")
        self.assertTrue(printed.printed)
        self.assertEqual(printed.printed_slot, "Module 1")
        self.assertIn(foam_render.PRINTED_FOR_DEMO, printed.badges())

        twin = foam_render.Case(chip=foam_render.load_json(ROOT / "chip/cell_prep_v1.json"),
                                chip_id="cell_prep_v1",
                                chip_path="chip/cell_prep_v1.json")
        self.assertFalse(twin.printed)
        self.assertNotIn(foam_render.PRINTED_FOR_DEMO, twin.badges())

    def test_five_of_the_six_are_in_the_mvp_chain_and_are_marked(self) -> None:
        marked = {p.key: p.printed_slot for p in self.layout.chips() if p.printed}
        self.assertEqual(sorted(marked.values()),
                         ["Module 1", "Module 2", "Module 3", "Module 4", "Module 5"])
        self.assertNotIn("module6", self.layout.stages,
                         "Module 6 is not in this chain yet; it arrives with the merge")

    def test_a_printed_chip_never_carries_a_suggested_change(self) -> None:
        case = foam_render.Case(
            chip=foam_render.load_json(CHIP), chip_id="module1_v1",
            chip_path="chip/module1_v1.json",
            findings=[{"severity": "high", "message": "shear is over the ceiling",
                       "fix_hint": "widen ch_in_n to 0.4 mm"}])
        self.assertNotIn("fix_hint", case.findings[0],
                         "a chip on the printer gets findings as built, not change requests")
        self.assertNotIn("widen", case.headline())
        self.assertIn("shear is over the ceiling", case.headline())

    def test_a_printed_card_says_findings_are_as_built(self) -> None:
        place = next(p for p in self.layout.chips() if p.printed)
        case = foam_render.Case(chip=foam_render.load_json(ROOT / place.chip_path),
                                chip_id=place.key, chip_path=place.chip_path, step=1)
        text = "\n".join(foam_scene.caption_for(place, case))
        self.assertIn(foam_render.PRINTED_FOR_DEMO, text)
        self.assertIn("AS BUILT", text)
        self.assertIn("full-spec design", text)

    def test_the_chain_file_can_say_so_itself_and_wins(self) -> None:
        """`built_for_demo` is what a merge updates, so it beats the frozen list."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _synthetic_chain(Path(tmp), with_barcoding=False, built_for_demo=True)
            layout, _ = foam_scene.layout_from_file(path)
        # Stage B is cell_prep_v1, which is NOT on the printer - the chain file says it is.
        self.assertTrue(layout.stages["b"].printed)
        self.assertEqual(layout.stages["b"].printed_slot, "Stage B")

    def test_an_off_chip_stage_is_never_printed(self) -> None:
        for place in self.layout.placeholders():
            self.assertFalse(place.printed, place.key)


class TheSpecIsRenderedAsWritten(unittest.TestCase):
    """Module 6's comb teeth are prose, not geometry. Render the gap, caption it."""

    def test_module6_combs_do_not_reach_their_culture_fields(self) -> None:
        chip = foam_render.load_json(ROOT / "chip" / "module6_v1.json")
        gaps = foam_render.spec_gaps(chip)
        self.assertEqual(len(gaps), 6, "three lanes, an inlet and an outlet comb each")
        combs = sorted(g["feature_id"] for g in gaps)
        self.assertEqual(combs, ["comb_in_a", "comb_in_g", "comb_in_n",
                                 "comb_out_a", "comb_out_g", "comb_out_n"])
        for gap in gaps:
            self.assertAlmostEqual(gap["value"], 1.90, places=2)
            self.assertEqual(gap["class"], "dead_end")
            self.assertIn("not in the geometry", gap["message"])
            self.assertIn("centipede_channel", gap["message"])

    def test_the_note_claims_teeth_that_the_path_does_not_have(self) -> None:
        """The evidence for the finding, straight out of the chip file."""
        chip = foam_render.load_json(ROOT / "chip" / "module6_v1.json")
        comb = next(c for c in chip["channels"] if c["id"] == "comb_in_n")
        self.assertIn("teeth", comb["note"])
        self.assertEqual(len(comb["path"]), 2,
                         "the path is the spine only: two points, no teeth")

    def test_module6_is_the_only_chip_in_the_chain_or_on_the_printer_with_a_gap(self) -> None:
        """No false positives where it matters: the live chain and the six printed chips.

        It does fire elsewhere - `chip/sctip_spc_v1.json` is retired and has twelve - and
        that is the check working, not a bug, so retired chips are not asserted on here.
        """
        layout, _ = foam_scene.layout_from_file(PIPELINE)
        paths = {ROOT / p.chip_path for p in layout.chips()}
        paths |= {ROOT / rel for rel in foam_render.printed_chips()}
        gappy = {}
        for path in sorted(paths):
            gaps = foam_render.spec_gaps(foam_render.load_json(path))
            if gaps:
                gappy[path.stem] = [g["feature_id"] for g in gaps]
        self.assertEqual(sorted(gappy), ["module6_v1"], f"unexpected gaps: {gappy}")

    def test_the_picture_does_not_draw_the_bridge_the_mesher_adds(self) -> None:
        """The caption says the comb stops short, so the frame must not join it up.

        The mesher redraws `comb_in_n` as a diagonal from the real spine start to the
        culture field's edge. The spec's own path is a vertical spine at x = 2.5.
        """
        from assay2chip.cfd import mesh as cfd_mesh
        chip = foam_render.load_json(ROOT / "chip" / "module6_v1.json")
        mesh = cfd_mesh.build_mesh(chip)

        redirected = next(e for e in foam_render.duct_edges(mesh, chip)
                          if e.feature_id == "comb_in_n")
        ends = (mesh.node(redirected.a), mesh.node(redirected.b))
        self.assertNotAlmostEqual(ends[0].x_mm, ends[1].x_mm, places=2,
                                  msg="the mesh edge is the diagonal, as expected")

        self.assertEqual(foam_render._shape_edges(mesh, chip), [],
                         "every duct here belongs to a channel, so the spec's paths "
                         "shape the picture and no mesh edge does")
        for (x0, y0), (x1, y1), _w, feature in foam_render._segments(chip, mesh):
            if feature == "comb_in_n":
                self.assertAlmostEqual(x0, 2.5, places=3)
                self.assertAlmostEqual(x1, 2.5, places=3)

    def test_the_culture_field_is_drawn_unconnected(self) -> None:
        """Fluid exists in the combs and in the field, with wall in between."""
        import numpy as np
        if not HAS_PYVISTA:
            self.skipTest("PyVista is not installed")
        case = foam_render.reduced_case(ROOT / "chip" / "module6_v1.json",
                                        ROOT / "protocols" / "module6_v1.json",
                                        max_steps=1, cells_budget=120_000, nz=5)[0]
        grid = case.volume.grid
        nx, ny, nz = grid.dimensions
        fluid = np.asarray(grid["fluid"]).reshape((nx, ny, nz), order="F")[:, :, nz // 2]
        spacing = case.volume.spacing_mm
        row = int(round(10.0 / spacing))              # y = 10 mm, across lane n
        # x = 2.5 is the inlet spine, 5.0 the culture field, 3.5 the gap between them.
        at = lambda x_mm: bool(fluid[int(round(x_mm / spacing)), row])
        self.assertTrue(at(2.5), "the inlet comb spine is fluid")
        self.assertTrue(at(5.0), "the culture field is fluid")
        self.assertTrue(at(7.5), "the outlet comb spine is fluid")
        self.assertFalse(at(3.5), "the 1.9 mm gap the spec leaves must stay wall")
        self.assertFalse(at(6.5), "the outlet side gap must stay wall too")

    def test_the_frame_says_the_numbers_are_provisional(self) -> None:
        if not HAS_PYVISTA:
            self.skipTest("PyVista is not installed")
        case = foam_render.reduced_case(ROOT / "chip" / "module6_v1.json",
                                        ROOT / "protocols" / "module6_v1.json",
                                        max_steps=1, cells_budget=120_000, nz=5)[0]
        self.assertIn("stops 1.90 mm short", case.headline(),
                      "a structural gap leads over a shear number of the same severity")
        self.assertTrue(any("provisional" in n for n in case.notes), case.notes)
        self.assertIn(foam_render.PRINTED_FOR_DEMO, case.badges())

    def test_the_gap_is_read_from_the_spec_not_from_the_forgiving_mesh(self) -> None:
        """cfd/mesh.py attaches a loose channel end to the nearest feature so the network
        still solves. That is right for a solver and wrong for a picture."""
        from assay2chip.cfd import mesh as cfd_mesh
        chip = foam_render.load_json(ROOT / "chip" / "module6_v1.json")
        mesh = cfd_mesh.build_mesh(chip)
        self.assertEqual(mesh.unreachable(), [],
                         "the mesher bridges the gap, which is why spec_gaps reads the "
                         "spec instead")
        self.assertTrue(foam_render.spec_gaps(chip))


class OneCaseDirectoryConvention(unittest.TestCase):
    """A job failed because the mesh and the case went to two different folders. There is
    one convention now: <out>/<chip_id>/step_<N>/, mesh inside at constant/polyMesh."""

    def test_the_case_directory_is_chip_then_step_n(self) -> None:
        got = foam_render.case_dir("module1_v1", 3, out=Path("/w/pipeline_e2e/foam"))
        self.assertEqual(got, Path("/w/pipeline_e2e/foam/module1_v1/step_3"))

    def test_the_mesh_lives_inside_the_case(self) -> None:
        case = foam_render.case_dir("module3_v1_translated", 9,
                                    out=Path("/w/run/foam"))
        self.assertEqual((case / "constant" / "polyMesh").parent.parent, case)

    def test_out_defaults_to_work_run_foam(self) -> None:
        got = foam_render.case_dir("module6_v1", 1, run="demo")
        self.assertEqual(got.parts[-4:], ("demo", "foam", "module6_v1", "step_1"))

    def test_there_is_never_a_second_foam_level(self) -> None:
        once = foam_render.foam_out_dir(Path("/w/run"))
        twice = foam_render.foam_out_dir(once)
        self.assertEqual(once, Path("/w/run/foam"))
        self.assertEqual(twice, once, "foam/foam is the bug that broke a real job")
        for out in (Path("/w/run"), Path("/w/run/foam")):
            self.assertEqual(foam_render.case_dir("c", 1, out=out),
                             Path("/w/run/foam/c/step_1"))

    def test_the_convention_is_case_pys_and_is_not_reimplemented_here(self) -> None:
        """`case.py` owns it; render.py passes through. Coordinate, do not duplicate."""
        from assay2chip.foam import case as foam_case
        for chip_id, step in (("module1_v1", 3), ("module6_v1", "4b"),
                              ("spc_demo_v1", None)):
            self.assertEqual(
                foam_render.case_dir(chip_id, step, out=Path("/w/run")),
                foam_case.case_dir(foam_case.out_dir(work=Path("/w/run")), chip_id, step),
                f"{chip_id} step {step}")
        self.assertEqual(foam_render.foam_out_dir(Path("/w/run")),
                         foam_case.out_dir(work=Path("/w/run")))

    def test_a_whole_chip_case_and_an_odd_step_id_follow_case_py(self) -> None:
        out = Path("/w/run/foam")
        self.assertEqual(foam_render.case_dir("m", None, out=out).name, "all")
        self.assertEqual(foam_render.case_dir("m", "4b", out=out).name, "step_4b")

    def test_nothing_builds_a_case_path_by_hand(self) -> None:
        import re
        for name in ("render.py", "scene.py", "run.py"):
            text = (ROOT / "assay2chip" / "foam" / name).read_text(encoding="utf-8")
            body = "\n".join(line for line in text.splitlines()
                             if not line.lstrip().startswith("#"))
            self.assertIsNone(
                re.search(r'f"step_\{', body),
                f"{name} formats a step directory itself instead of calling case_dir()")

    def test_a_case_written_under_the_convention_is_found_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = foam_render.foam_out_dir(Path(tmp))
            case = foam_render.case_dir("module1_v1", 7, out=out)
            (case / "constant" / "polyMesh").mkdir(parents=True)
            (case / "summary.json").write_text(
                json.dumps({"schema": "foam-flow/1", "step": 7}), encoding="utf-8")
            found = foam_render.foam_summary(Path(tmp), "module1_v1", 7)
            self.assertIsNotNone(found)
            self.assertEqual(found["step"], 7)


class KinematicOutputIsConvertedOnce(unittest.TestCase):
    """OpenFOAM's incompressible solvers divide through by density: p and
    wallShearStress are m^2/s^2, not Pa. Getting rho wrong is a factor of a thousand."""

    def test_the_density_comes_from_the_contract_not_a_literal(self) -> None:
        from assay2chip.foam import case as foam_case
        self.assertAlmostEqual(foam_render.foam_density(),
                               foam_case.fluid_for(37.0).rho_kg_m3, places=6)

    def test_the_cases_own_density_wins_when_it_states_one(self) -> None:
        self.assertEqual(foam_render.foam_density({"rho_kg_m3": 993.0}), 993.0)

    def test_kinematic_shear_becomes_dyn_cm2(self) -> None:
        """tau[Pa] = rho * tau_kinematic[m2/s2]; dyn/cm2 is ten times that."""
        from assay2chip.cfd.model import DYN_CM2_PER_PA
        rho = foam_render.foam_density()
        kinematic = 1e-5
        self.assertAlmostEqual(kinematic * rho * DYN_CM2_PER_PA, kinematic * rho * 10.0)
        self.assertEqual(DYN_CM2_PER_PA, 10.0)


class HandoffTubesJoinRealPorts(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.layout, cls.chips = foam_scene.layout_from_file(PIPELINE)

    def test_there_is_a_tube_for_every_handoff_between_placed_stages(self) -> None:
        chain = foam_scene.cfd_couple.read_chain(PIPELINE)
        self.assertEqual(len(self.layout.tubes), len(chain.handoffs))

    def test_a_named_port_puts_the_tube_end_on_that_port(self) -> None:
        hits = 0
        for tube in self.layout.tubes:
            place = self.layout.stages[tube.from_key]
            chip = self.chips.get(tube.from_key)
            if not tube.from_port or chip is None:
                continue
            want = place.port_xy(tube.from_port, chip)
            self.assertIsNotNone(want)
            self.assertAlmostEqual(tube.a[0], want[0], places=6)
            self.assertAlmostEqual(tube.a[1], want[1], places=6)
            hits += 1
        self.assertGreater(hits, 0, "the MVP chain names real ports on several handoffs")

    def test_an_unnamed_port_is_marked_tbc_rather_than_invented(self) -> None:
        tbc = [t for t in self.layout.tubes if t.tbc]
        self.assertTrue(tbc, "the MVP chain has handoffs with a TBC port")
        for tube in tbc:
            self.assertTrue(not tube.from_port or not tube.to_port)
            self.assertIn("TBC", tube.label())

    def test_a_tube_path_starts_and_ends_on_its_ports_and_lifts_between(self) -> None:
        for tube in self.layout.tubes:
            path = tube.path(24)
            self.assertEqual(path.shape, (24, 3))
            for want, got in zip(tube.a, path[0]):
                self.assertAlmostEqual(want, got, places=6)
            for want, got in zip(tube.b, path[-1]):
                self.assertAlmostEqual(want, got, places=6)
            self.assertGreater(path[:, 2].max(), max(tube.a[2], tube.b[2]),
                               f"{tube.id} does not lift clear of the bench")


class TheWordsOnEveryFrame(unittest.TestCase):
    """foam/README.md: every render says SIMULATED, every 30 x 20 mm chip says DEMO SCALE."""

    def test_a_demo_scale_chip_is_badged_simulated_and_demo_scale(self) -> None:
        chip = foam_render.load_json(CHIP)
        case = foam_render.Case(chip=chip, chip_id="module1_v1")
        self.assertTrue(case.demo_scale)
        self.assertIn(foam_render.SIMULATED, case.badges())
        self.assertIn(foam_render.DEMO_SCALE, case.badges())

    def test_a_chip_bigger_than_the_demo_envelope_is_not_badged_demo_scale(self) -> None:
        chip = {"id": "big", "footprint": {"x": 290, "y": 190}, "chambers": [], "ports": []}
        case = foam_render.Case(chip=chip, chip_id="big")
        self.assertFalse(case.demo_scale)
        self.assertIn(foam_render.SIMULATED, case.badges())
        self.assertNotIn(foam_render.DEMO_SCALE, case.badges())

    def test_the_fallback_says_reduced_order_and_the_foam_source_does_not(self) -> None:
        chip = foam_render.load_json(CHIP)
        reduced = foam_render.Case(chip=chip, chip_id="module1_v1",
                                   source=foam_render.SOURCE_REDUCED)
        foam = foam_render.Case(chip=chip, chip_id="module1_v1",
                                source=foam_render.SOURCE_FOAM)
        self.assertIn("reduced-order (2.5D)", reduced.badges())
        self.assertTrue(reduced.reduced)
        self.assertIn("openfoam", foam.badges())
        self.assertFalse(foam.reduced)
        self.assertNotIn("reduced-order (2.5D)", foam.badges())

    def test_a_case_with_no_finding_says_so_rather_than_going_quiet(self) -> None:
        case = foam_render.Case(chip={"id": "x"}, chip_id="x")
        self.assertIn("no finding", case.headline())

    def test_the_headline_is_the_worst_finding(self) -> None:
        case = foam_render.Case(chip={"id": "x"}, chip_id="x", findings=[
            {"severity": "low", "message": "a small thing"},
            {"severity": "blocker", "message": "the capsules cannot get out"},
            {"severity": "high", "message": "shear is over the ceiling"},
        ])
        self.assertIn("BLOCKER", case.headline())
        self.assertIn("capsules cannot get out", case.headline())

    def test_port_labels_name_the_port_and_what_the_step_does_with_it(self) -> None:
        self.assertIn("feed", foam_render.PortMark("p_in", 1, 2, role="feed",
                                                   q_ul_min=2.5).label())
        self.assertIn("2.5", foam_render.PortMark("p_in", 1, 2, role="feed",
                                                  q_ul_min=2.5).label())
        self.assertIn("vent", foam_render.PortMark("p_out", 1, 2, role="vent").label())
        self.assertIn("capped", foam_render.PortMark("p_x", 1, 2, role="cap").label())


class CaptionsSayStageStepFlowAndFinding(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.layout, _ = foam_scene.layout_from_file(PIPELINE)

    def test_a_chip_caption_carries_all_four(self) -> None:
        place = self.layout.chips()[0]
        case = foam_render.Case(
            chip=foam_render.load_json(ROOT / place.chip_path), chip_id=place.key,
            step=7, operation="wash", q_ul_min=3.0, fluid="PBS",
            source=foam_render.SOURCE_REDUCED,
            findings=[{"severity": "high", "message": "shear over the ceiling"}])
        text = "\n".join(foam_scene.caption_for(place, case))
        self.assertIn(place.name, text)
        self.assertIn("step 7", text)
        self.assertIn("wash", text)
        self.assertIn("3 uL/min", text)
        self.assertIn("shear over the ceiling", text)
        self.assertIn(foam_render.SOURCE_REDUCED, text)

    def test_a_placeholder_caption_says_there_is_no_chip(self) -> None:
        place = self.layout.placeholders()[0]
        text = "\n".join(foam_scene.caption_for(place, None))
        self.assertIn(place.badge, text)
        self.assertIn("no chip", text)

    def test_a_tbc_handoff_caption_calls_the_gap_a_gap(self) -> None:
        tube = next(t for t in self.layout.tubes if t.tbc)
        place = self.layout.stages[tube.to_key]
        text = "\n".join(foam_scene.caption_for(place, None, phase="handoff", tube=tube))
        self.assertIn("TBC", text)
        self.assertIn(tube.id, text)


class TheVideoIsTheLengthTheBriefAsksFor(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.layout, _ = foam_scene.layout_from_file(PIPELINE)

    def test_the_storyboard_visits_every_stage_in_chain_order(self) -> None:
        shots = foam_scene.storyboard(self.layout)
        visited = [s["key"] for s in shots if s["kind"] == "stage"]
        self.assertEqual(visited, self.layout.order)

    def test_a_handoff_shot_sits_between_the_two_stages_it_joins(self) -> None:
        shots = foam_scene.storyboard(self.layout)
        kinds = [(s["kind"], s.get("key"), s.get("tube")) for s in shots]
        for i, (kind, key, tube) in enumerate(kinds):
            if kind != "handoff":
                continue
            following = next(k for k in kinds[i + 1:] if k[0] == "stage")
            self.assertEqual(following[1], key)

    def test_a_handoff_is_framed_so_both_ends_of_its_tube_are_on_screen(self) -> None:
        tube = self.layout.tubes[0]
        a = self.layout.stages[tube.from_key]
        b = self.layout.stages[tube.to_key]
        view = foam_scene._pair_view(a, b, (1920, 1080))
        half_h = view["scale"]
        half_w = half_h * 1920.0 / 1080.0
        fx, fy, _ = view["focal"]
        for place in (a, b):
            x0, x1, y0, y1 = place.bounds
            self.assertLessEqual(max(abs(x0 - fx), abs(x1 - fx)), half_w + 1e-6,
                                 f"{place.key} falls outside the handoff framing in x")
            self.assertLessEqual(max(abs(y0 - fy), abs(y1 - fy)), half_h + 1e-6,
                                 f"{place.key} falls outside the handoff framing in y")

    def test_the_length_lands_in_the_sixty_to_one_twenty_second_window(self) -> None:
        plan = foam_scene.fit_to_window(self.layout)
        self.assertGreaterEqual(plan["seconds"], 59.0)
        self.assertLessEqual(plan["seconds"], 121.0)

    def test_a_very_short_chain_is_stretched_not_left_at_ten_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            layout, _ = foam_scene.layout_from_file(
                _synthetic_chain(Path(tmp), with_barcoding=False))
        plan = foam_scene.fit_to_window(layout)
        self.assertGreaterEqual(plan["seconds"], 59.0)


class RenderRules(unittest.TestCase):
    """The decisions render.py makes before a pixel is drawn."""

    def test_resolution_strings(self) -> None:
        self.assertEqual(foam_render.resolution_of("1080p"), (1920, 1080))
        self.assertEqual(foam_render.resolution_of("720p"), (1280, 720))
        self.assertEqual(foam_render.resolution_of("1281x721"), (1282, 722))
        self.assertEqual(foam_render.resolution_of((640, 480)), (640, 480))
        with self.assertRaises(foam_render.RenderError):
            foam_render.resolution_of("enormous")

    def test_h264_needs_even_dimensions(self) -> None:
        for spec in ("1080p", "1281x721", (999, 501)):
            w, h = foam_render.resolution_of(spec)
            self.assertEqual(w % 2, 0)
            self.assertEqual(h % 2, 0)

    def test_the_render_grid_obeys_the_cells_budget(self) -> None:
        chip = foam_render.load_json(CHIP)
        fx, fy = foam_render.footprint_mm(chip)
        for budget in (50_000, 400_000, 2_000_000):
            spacing = foam_render.grid_spacing_for(chip, None, budget, nz=9)
            cells = (fx / spacing) * (fy / spacing) * 9
            self.assertLessEqual(cells, budget * 1.05, f"budget {budget} overshot")

    def test_the_fluid_depth_is_the_fluidic_layer_not_the_whole_stack(self) -> None:
        chip = foam_render.load_json(CHIP)
        depth = foam_render.fluid_depth_mm(chip)
        self.assertAlmostEqual(depth, 0.3, places=6)
        thickest = max(l["thickness_mm"] for l in chip["layers"])
        self.assertLess(depth, thickest)

    def test_a_capsule_chamber_gets_no_wall_shear_ceiling(self) -> None:
        """Three ceilings, correctly scoped: a capsule chamber is pressure-bounded."""
        chip = foam_render.load_json(CHIP)
        table = foam_render.chamber_ceiling_table(chip, {"operation": "exchange"}, None)
        capsule = [cid for cid, rec in table.items() if rec["shear_class"] == "capsules"]
        self.assertTrue(capsule, "module1's trap chambers are capsule-scoped")
        for cid in capsule:
            self.assertIsNone(table[cid]["ceiling_dyn_cm2"],
                              f"{cid} must not borrow a cell-culture ceiling")

    def test_a_channel_inherits_the_stricter_of_the_chambers_it_joins(self) -> None:
        chip = foam_render.load_json(CHIP)
        table = foam_render.chamber_ceiling_table(chip, {"operation": "exchange"}, None)
        chambers = [rec["ceiling_dyn_cm2"] for cid, rec in table.items()
                    if any(c["id"] == cid for c in chip["chambers"])
                    and rec["ceiling_dyn_cm2"]]
        for ch in chip["channels"]:
            rec = table.get(ch["id"])
            if rec is None or not rec.get("ceiling_dyn_cm2"):
                continue
            self.assertIn(rec["ceiling_dyn_cm2"], chambers)
            self.assertIn("stricter", rec["why"])

    def test_the_flood_fill_does_not_cross_a_wall(self) -> None:
        """Two fluid strips with a wall between them must not share values."""
        import numpy as np
        fluid = np.zeros((9, 3), dtype=bool)
        fluid[0:4, :] = True
        fluid[5:9, :] = True              # row 4 is the wall
        values = np.full((9, 3), np.nan)
        values[0, :] = 1.0
        values[8, :] = 100.0
        foam_render._flood(fluid, [values])
        self.assertTrue(np.allclose(values[0:4, :], 1.0))
        self.assertTrue(np.allclose(values[5:9, :], 100.0))

    def test_a_limit_is_taken_whole_never_averaged_across_a_wall(self) -> None:
        import numpy as np
        fluid = np.ones((5, 1), dtype=bool)
        ceilings = np.full((5, 1), np.nan)
        ceilings[0, 0] = 0.01
        ceilings[4, 0] = 0.3
        foam_render._flood(fluid, [ceilings], how="nearest")
        for value in ceilings.ravel():
            self.assertIn(round(float(value), 6), (0.01, 0.3),
                          "a ceiling halfway between two real ones is a number nothing "
                          "was ever held to")

    def test_a_weir_slit_is_not_painted_as_a_chamber_wide_band(self) -> None:
        """A slit spans the chamber it crosses; drawing it at its own width inflates it."""
        from assay2chip.cfd import mesh as cfd_mesh
        chip = foam_render.load_json(CHIP)
        mesh = cfd_mesh.build_mesh(chip)
        self.assertTrue(mesh.slit_edges(), "module1's trap chambers have weir crests")
        drawn = {e.id for e in foam_render.duct_edges(mesh)}
        for slit in mesh.slit_edges():
            self.assertNotIn(slit.id, drawn)

    def test_the_glb_is_a_real_binary_container(self) -> None:
        import struct

        import numpy as np
        points = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float32)
        tris = np.array([[0, 1, 2]], dtype=np.uint32)
        part = foam_render.GlbPart("t", points, tris)
        with tempfile.TemporaryDirectory() as tmp:
            path = foam_render.write_glb([part], Path(tmp) / "x.glb")
            raw = path.read_bytes()
        self.assertEqual(raw[:4], b"glTF")
        magic, version, length = struct.unpack("<III", raw[:12])
        self.assertEqual(version, 2)
        self.assertEqual(length, len(raw))
        json_len, json_type = struct.unpack("<II", raw[12:20])
        self.assertEqual(json_type, foam_render.GLTF_JSON_CHUNK)
        doc = json.loads(raw[20:20 + json_len].decode("utf-8"))
        self.assertEqual(doc["asset"]["version"], "2.0")
        self.assertEqual(len(doc["buffers"]), 1)
        self.assertNotIn("uri", doc["buffers"][0], "a GLB buffer lives in the BIN chunk")
        self.assertNotIn("images", doc, "no texture, so no empty image URI to break a loader")

    def test_the_glb_labels_sidecar_carries_the_words(self) -> None:
        layout, _ = foam_scene.layout_from_file(PIPELINE)
        stages = {p.key: foam_scene.SceneStage(place=p, source=foam_render.SOURCE_REDUCED)
                  for p in layout.placements()}
        with tempfile.TemporaryDirectory() as tmp:
            path = foam_scene.write_labels(layout, stages, Path(tmp) / "labels.json")
            doc = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(doc["simulated"], foam_render.SIMULATED)
        self.assertEqual(len(doc["stages"]), len(layout.order))
        self.assertTrue(any(s["demo_scale"] for s in doc["stages"]))
        for stage in doc["stages"]:
            if stage["kind"] == "placeholder":
                self.assertIn(foam_render.OFF_CHIP, stage["badge"])


class OpenFoamIsPreferredAndItsAbsenceIsHonest(unittest.TestCase):

    def test_no_case_directory_means_no_openfoam_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(foam_render.foam_summary(Path(tmp), "module1_v1"))
            self.assertIsNone(foam_render.foam_vtk(Path(tmp), "module1_v1"))
            self.assertIsNone(foam_render.foam_case(Path(tmp), CHIP))

    def test_a_summary_with_no_field_is_not_dressed_up_as_openfoam(self) -> None:
        """A frame labelled `openfoam` must actually be one."""
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "foam" / "module1_v1" / "step_1"
            case_dir.mkdir(parents=True)
            (case_dir / "summary.json").write_text(
                json.dumps({"schema": "foam-flow/1", "source": "openfoam", "step": 1}),
                encoding="utf-8")
            self.assertIsNotNone(foam_render.foam_summary(Path(tmp), "module1_v1", 1))
            self.assertIsNone(foam_render.foam_case(Path(tmp), CHIP, 1),
                              "a summary without a field is not a renderable case")

    def test_a_json_that_is_not_a_flow_summary_is_left_alone(self) -> None:
        """flow.py stamps `schema: foam-flow/1`; another agent's file is not ours to read."""
        with tempfile.TemporaryDirectory() as tmp:
            case_dir = Path(tmp) / "foam" / "module1_v1"
            case_dir.mkdir(parents=True)
            (case_dir / "summary.json").write_text(
                json.dumps({"something": "else"}), encoding="utf-8")
            self.assertIsNone(foam_render.foam_summary(Path(tmp), "module1_v1"))


class TheOrchestratorReportsMissingSiblings(unittest.TestCase):

    def test_a_module_that_does_not_exist_yet_is_reported_not_faked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reports = foam_run.solver_stages(CHIP, PROTOCOL, Path(tmp), log=lambda *_: None)
        self.assertEqual([r.name for r in reports],
                         [name for name, _, _ in foam_run.SOLVER_STAGES])
        for report in reports:
            self.assertIn(report.status,
                          ("ok", "skipped", "not written yet", "no entry point",
                           "signature mismatch", "errored"))
            self.assertTrue(report.detail)

    def test_launching_openfoam_is_opt_in_and_the_skip_says_why(self) -> None:
        """A minutes-long solve is not something a render command should start by itself."""
        with tempfile.TemporaryDirectory() as tmp:
            reports = foam_run.solver_stages(CHIP, PROTOCOL, Path(tmp), solve=False,
                                             log=lambda *_: None)
        by_name = {r.name: r for r in reports}
        for name in foam_run.SOLVING_STAGES:
            self.assertEqual(by_name[name].status, "skipped", name)
            self.assertIn("--solve", by_name[name].detail)
        self.assertNotIn("mesh", foam_run.SOLVING_STAGES,
                         "writing a polyMesh launches nothing, so it always runs")

    def test_the_orchestrator_never_calls_a_siblings_cli(self) -> None:
        for _, names, _ in foam_run.SOLVER_STAGES:
            self.assertNotIn("main", names,
                             "calling another agent's main() with guessed arguments is "
                             "how one module breaks another")

    def test_render_only_skips_every_solver_stage(self) -> None:
        parser = foam_run.build_parser()
        args = parser.parse_args(["--pipeline", "protocols/pipeline_mvp.json",
                                  "--render-only"])
        self.assertTrue(args.render_only)
        self.assertIsNone(args.chip)

    def test_the_cli_takes_the_flags_the_brief_names(self) -> None:
        parser = foam_run.build_parser()
        args = parser.parse_args(["--chip", "chip/module1_v1.json",
                                  "--protocol", "protocols/module1_v1.json",
                                  "--step", "3", "--resolution", "720p",
                                  "--cells-budget", "50000"])
        self.assertEqual(args.chip, "chip/module1_v1.json")
        self.assertEqual(args.step, "3")
        self.assertEqual(args.resolution, "720p")
        self.assertEqual(args.cells_budget, 50000)


@unittest.skipUnless(HAS_PYVISTA, "PyVista is not installed (requirements-foam.txt)")
class ItActuallyDraws(unittest.TestCase):
    """The pixels. Skipped cleanly without PyVista, as foam/README.md requires."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.case = foam_render.reduced_case(CHIP, PROTOCOL, max_steps=1,
                                            cells_budget=60_000, nz=5)[0]

    def test_the_fallback_builds_a_volume_and_labels_itself(self) -> None:
        self.assertEqual(self.case.source, foam_render.SOURCE_REDUCED)
        self.assertIsNotNone(self.case.volume)
        self.assertGreater(self.case.volume.grid.n_points, 1000)

    def test_the_volume_has_the_fields_a_frame_needs(self) -> None:
        for name in ("fluid", "U_mm_s", "speed_mm_s", "shear_dyn_cm2", "shear_frac",
                     "age_s"):
            self.assertIn(name, self.case.volume.grid.point_data, name)

    def test_the_depth_averaged_profile_averages_to_the_solved_mean(self) -> None:
        """The 2.5D extrusion must not change the answer, only its shape in z."""
        import numpy as np
        grid = self.case.volume.grid
        nx, ny, nz = grid.dimensions
        speed = np.asarray(grid["speed_mm_s"]).reshape((nx, ny, nz), order="F")
        fluid = np.asarray(grid["fluid"]).reshape((nx, ny, nz), order="F") > 0.5
        zeta = np.linspace(-1.0, 1.0, nz)
        profile = 1.5 * (1.0 - zeta ** 2)
        profile[0] = profile[-1] = 0.0
        mid = speed[:, :, nz // 2][fluid[:, :, nz // 2]]
        mean = speed[:, :, :].mean(axis=2)[fluid[:, :, nz // 2]]
        peak = profile[nz // 2]
        good = mid > 0
        self.assertTrue(good.any())
        ratio = (mean[good] * peak / np.maximum(mid[good], 1e-12))
        self.assertAlmostEqual(float(np.median(ratio)), float(profile.mean()), places=2)

    def test_streamlines_start_at_the_feed_ports(self) -> None:
        import numpy as np
        lines = foam_render._streamlines(self.case.volume, self.case)
        self.assertIsNotNone(lines)
        self.assertGreater(lines.n_points, 10)
        feeds = np.asarray([[p.x_mm, p.y_mm] for p in self.case.feeds])
        self.assertTrue(len(feeds), "step 1 drives at least one feed port")
        starts = np.asarray(lines.points)[:, :2]
        nearest = min(float(np.min(np.linalg.norm(starts - f, axis=1))) for f in feeds)
        self.assertLess(nearest, 1.0, "no streamline begins at a feed port")

    def test_the_floor_layer_is_not_empty(self) -> None:
        floor = foam_render._layer(self.case.volume, "floor")
        self.assertGreater(floor.n_points, 100,
                           "a cutting plane laid on the boundary loses the shear frame")

    def test_a_dataset_that_lost_its_field_is_coloured_flat_not_crashed(self) -> None:
        """A filter can return points with no point data; VTK then raises
        `Data array (speed_mm_s) not present` and takes a 20-minute run with it."""
        import numpy as np
        pv = foam_render.pyvista()
        bare = pv.lines_from_points(np.array([[0.0, 0, 0], [1, 1, 0]]))
        flat = foam_render._paint(bare, "speed_mm_s")
        self.assertNotIn("scalars", flat)
        self.assertIn("color", flat)
        self.assertNotIn("show_scalar_bar", flat,
                         "it must not collide with an explicit show_scalar_bar")
        painted = bare.copy()
        painted["speed_mm_s"] = np.array([1.0, 2.0])
        self.assertEqual(foam_render._paint(painted, "speed_mm_s"),
                         {"scalars": "speed_mm_s"})
        self.assertEqual(foam_render._paint(None, "speed_mm_s").get("scalars"), None)

    def test_every_frame_kind_renders_on_every_solved_step(self) -> None:
        """The regression: step 5 of cell_prep_v1 crashed the whole pipeline run."""
        cases = foam_render.reduced_case(
            ROOT / "chip" / "cell_prep_v1.json", ROOT / "protocols" / "cell_prep_v1.json",
            max_steps=6, cells_budget=60_000, nz=5)
        self.assertGreater(len(cases), 1)
        with tempfile.TemporaryDirectory() as tmp:
            for case in cases:
                lines = foam_render._streamlines(case.volume, case)
                for kind in ("speed", "shear", "streamlines", "tracers"):
                    png = foam_render.render_frame(
                        case, kind, (320, 240), Path(tmp) / f"{case.step}_{kind}.png",
                        lines=lines)
                    self.assertTrue(png.exists(), f"step {case.step} {kind}")

    def test_a_frame_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            png = foam_render.render_frame(self.case, "speed", (480, 320),
                                           Path(tmp) / "f.png")
            self.assertTrue(png.exists())
            self.assertGreater(png.stat().st_size, 4000)

    def test_the_scene_exports_a_glb_a_viewer_can_open(self) -> None:
        import struct
        layout, _ = foam_scene.layout_from_file(PIPELINE)
        place = layout.chips()[0]
        stage = foam_scene.SceneStage(place=place, source=foam_render.SOURCE_REDUCED)
        stage.body = foam_render.pyvista().Box(bounds=(0, 30, 0, 20, 0, 0.3))
        with tempfile.TemporaryDirectory() as tmp:
            path = foam_scene.export_glb(layout, {place.key: stage},
                                         Path(tmp) / "scene.glb")
            raw = path.read_bytes()
        self.assertEqual(raw[:4], b"glTF")
        self.assertEqual(struct.unpack("<I", raw[8:12])[0], len(raw))


@unittest.skipUnless(HAS_PYVISTA and HAS_FFMPEG, "needs PyVista and ffmpeg")
class ItEncodesVideo(unittest.TestCase):

    def test_the_mp4_is_h264_yuv420p(self) -> None:
        import shutil
        import subprocess
        case = foam_render.reduced_case(CHIP, PROTOCOL, max_steps=1,
                                        cells_budget=40_000, nz=5)[0]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            frames = [foam_render.render_frame(case, "speed", (320, 240),
                                               tmp / f"f{i}.png") for i in range(3)]
            mp4 = foam_render.encode_mp4(frames, tmp / "out.mp4", fps=2)
            self.assertTrue(mp4.exists())
            probe = shutil.which("ffprobe")
            if probe:
                out = subprocess.run(
                    [probe, "-v", "error", "-select_streams", "v:0", "-show_entries",
                     "stream=codec_name,pix_fmt", "-of", "csv=p=0", str(mp4)],
                    capture_output=True, text=True).stdout
                self.assertIn("h264", out)
                self.assertIn("yuv420p", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

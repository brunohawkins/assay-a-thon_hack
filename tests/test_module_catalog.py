"""The module catalogue (tools/module_catalog.py), video slots and the video publisher. Offline.

    python -m unittest tests.test_module_catalog -v
"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import module_catalog as mc  # noqa: E402
import publish_video as pv  # noqa: E402


class Catalogue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cat = mc.build_catalog(ROOT)
        cls.pipe = json.loads((ROOT / mc.PIPELINE).read_text())

    def test_every_pipeline_stage_in_chain_order(self):
        keys = [s["stage"] for s in self.cat["stages"]]
        expected = [m["module"] for m in self.pipe["modules"]] + [r["id"] for r in self.pipe.get("roadmap", [])]
        self.assertEqual([k for k in keys if k in expected], expected)
        self.assertEqual([s["order"] for s in self.cat["stages"]], list(range(1, len(keys) + 1)))

    def test_off_chip_stages_are_marked(self):
        for r in self.pipe.get("roadmap", []):
            stage = next(s for s in self.cat["stages"] if s["stage"] == r["id"])
            # A branch filed under roadmap carries its real chip; everything else is off chip.
            self.assertEqual(stage["kind"], "CHIP" if stage.get("module") else "OFF-CHIP")
        md = (ROOT / mc.OUT_MD).read_text() if (ROOT / mc.OUT_MD).is_file() else mc.render_md(self.cat)
        self.assertIn("OFF-CHIP", md)

    def test_every_printed_chip_is_catalogued_and_flagged(self):
        mods = {s["module"]["chip"]: s["module"] for s in self.cat["stages"] + self.cat["not_in_pipeline"]
                if s.get("module")}
        for p in mc.PRINTED:
            if not (ROOT / p["chip"]).is_file():
                continue
            self.assertIn(p["chip"], mods)
            m = mods[p["chip"]]
            self.assertTrue(m["printed_for_demo"])
            self.assertEqual(m["slot"], p["slot"])
            self.assertTrue(m["function"], f"{p['chip']} has no protocol steps")
        for chip, m in mods.items():
            if chip not in mc.PRINTED_BY_CHIP:
                self.assertFalse(m["printed_for_demo"])

    def test_function_steps_carry_the_exact_fields(self):
        fields = {"step", "operation", "target", "reagent", "volume_ul", "flow_ul_min", "duration_s",
                  "temp_c", "exchange_kind"}
        for s in self.cat["stages"]:
            for st in (s.get("module") or {}).get("function", []):
                self.assertLessEqual(fields, set(st))

    def test_every_printed_module_has_a_video_slot(self):
        manifest = mc.build_video_manifest(self.cat, None)
        slots = {v["chip_id"]: v for v in manifest["videos"]}
        for p in mc.PRINTED:
            v = slots[Path(p["chip"]).stem]
            self.assertEqual(v["module"], p["slot"])
            self.assertTrue(v["shot"])
            for k in ("title", "url", "release_tag", "recorded", "duration_s"):
                self.assertIn(k, v)
            self.assertIsNone(v["url"])
            self.assertFalse(v["recorded"])

    def test_recorded_urls_survive_regeneration(self):
        old = {"videos": [{"chip_id": "module4_v1", "url": "https://example.invalid/m4.mp4",
                           "release_tag": "demo-videos", "recorded": True, "duration_s": 30}]}
        v = next(x for x in mc.build_video_manifest(self.cat, old)["videos"] if x["chip_id"] == "module4_v1")
        self.assertEqual((v["url"], v["recorded"], v["duration_s"]), ("https://example.invalid/m4.mp4", True, 30))

    def test_deterministic(self):
        a, b = mc.generate(ROOT), mc.generate(ROOT)
        self.assertEqual(a, b)
        self.assertEqual(set(a), {mc.OUT_MD, mc.OUT_JSON, mc.VIDEO_MANIFEST})

    def test_degrades_without_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = mc.generate(Path(tmp))  # no pipeline, no chips, no status, no demo plan
            cat = json.loads(out[mc.OUT_JSON])
            self.assertEqual(cat["stages"], [])
            self.assertEqual(len(json.loads(out[mc.VIDEO_MANIFEST])["videos"]), len(mc.PRINTED))

    def test_demo_plan_is_used_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            for rel in ("chip/module4_v1.json", "protocols/module4_v1.json", "runs/index.json"):
                (t / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / rel, t / rel)
            (t / "docs").mkdir()
            (t / "docs" / "demo.md").write_text(
                "# Demo\n\n## Module 4 (module4_v1)\n\n1. Prime with water.\n2. Film the fill A B C D.\n")
            cat = mc.build_catalog(t)
            m4 = next(s["module"] for s in cat["not_in_pipeline"] if s["module"]["chip_id"] == "module4_v1")
            self.assertEqual(m4["demo"]["source"], "docs/demo.md")
            self.assertEqual(m4["demo"]["procedure"], ["Prime with water.", "Film the fill A B C D."])
            v = next(x for x in mc.build_video_manifest(cat, None)["videos"] if x["chip_id"] == "module4_v1")
            self.assertEqual(v["shot"], "Film the fill A B C D.")


class AfterTheMerge(unittest.TestCase):
    """The merge brings the real pipeline (barcoding stage, Module 6), a demo plan and the Module 1
    target design: a plain re-run must pick all of it up, with no code change."""

    def test_plain_rerun_picks_up_merged_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            for d in ("chip", "protocols", "runs", "cad/figures"):
                shutil.copytree(ROOT / d, t / d)
            pipe = json.loads((t / mc.PIPELINE).read_text())
            pipe["modules"].append({"module": "barcode_kit", "name": "Barcoding: bought kit", "kind": "off_chip",
                                    "build": {"method": "bought kit, at the bench"}})
            pipe["modules"].append({"module": "module6", "name": "Module 6: bulk omics", "status": "final",
                                    "chip": "chip/module6_v1.json", "protocol": "protocols/module6_v1.json"})
            pipe["handoffs"].append({"id": "x", "carries": "cells", "from": "module1",
                                     "to": {"module": "module6", "port": "in_n"}, "volume_ul": "TBC"})
            (t / mc.PIPELINE).write_text(json.dumps(pipe))
            target = json.loads((t / "chip/module1_v1.json").read_text())
            target.update(id="module1_target_v1", footprint={"x": 140, "y": 75})
            (t / "chip/module1_target_v1.json").write_text(json.dumps(target))
            (t / "tools").mkdir()
            (t / "tools/demo_plan.py").write_text(
                "def plan():\n"
                "    return {'modules': [{'chip_id': 'module6_v1', 'steps': ['Seed dye', {'action': 'Flush'}],\n"
                "                         'shot': 'Film three lanes filling'}]}\n"
                "if __name__ == '__main__':\n    raise SystemExit(plan())\n")

            self.assertEqual(mc.main([], root=t), 0)
            self.assertEqual(mc.main(["--check"], root=t), 0)  # second run: nothing changes
            cat = json.loads((t / mc.OUT_JSON).read_text())
            keys = [s["stage"] for s in cat["stages"]]
            self.assertIn("module6", keys)
            self.assertEqual(cat["not_in_pipeline"], [])
            kit = next(s for s in cat["stages"] if s["stage"] == "barcode_kit")
            self.assertEqual(kit["kind"], "OFF-CHIP")
            m6 = next(s["module"] for s in cat["stages"] if s["stage"] == "module6")
            self.assertEqual(m6["demo"]["procedure"], ["Seed dye", "Flush"])
            m1 = next(s["module"] for s in cat["stages"] if s["stage"] == "module1")
            self.assertEqual(m1["full_spec"]["chip"], "chip/module1_target_v1.json")
            self.assertTrue(m1["full_spec"]["scale"].startswith("TARGET 140 x 75"))
            manifest = json.loads((t / mc.VIDEO_MANIFEST).read_text())
            v6 = next(v for v in manifest["videos"] if v["chip_id"] == "module6_v1")
            self.assertEqual((v6["shot"], v6["shot_source"]), ("Film three lanes filling", "tools/demo_plan.py"))
            self.assertIn("FULL SPEC", (t / mc.OUT_MD).read_text())

    def test_a_broken_demo_plan_does_not_break_the_catalogue(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            (t / "tools").mkdir()
            (t / "tools/demo_plan.py").write_text("import sys\nsys.exit(3)\n")
            self.assertEqual(mc.load_demo(t), {})


class FakeGh:
    """Stands in for subprocess.run: records gh calls, never touches the network."""

    def __init__(self, release_exists=False):
        self.calls = []
        self.release_exists = release_exists

    def __call__(self, cmd, capture_output=True, text=True):
        self.calls.append(cmd)
        assert cmd[0] == "gh"
        ok = lambda out="": subprocess.CompletedProcess(cmd, 0, out, "")  # noqa: E731
        if cmd[1:3] == ["release", "view"] and "--json" in cmd:
            return ok(json.dumps({"assets": [{"name": "module1_v1.mp4",
                                              "url": "https://github.com/o/r/releases/download/demo-videos/module1_v1.mp4"}]}))
        if cmd[1:3] == ["release", "view"]:
            return ok() if self.release_exists else subprocess.CompletedProcess(cmd, 1, "", "release not found")
        if cmd[1:3] == ["release", "create"]:
            self.release_exists = True
        return ok()


class PublishVideo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cat = mc.build_catalog(ROOT)
        (self.root / "docs" / "videos").mkdir(parents=True)
        (self.root / mc.VIDEO_MANIFEST).write_text(json.dumps(mc.build_video_manifest(cat, None)))
        self.video = self.root / "take.mp4"
        self.video.write_bytes(b"\x00" * 64)

    def tearDown(self):
        self.tmp.cleanup()

    def manifest(self):
        return json.loads((self.root / mc.VIDEO_MANIFEST).read_text())

    def test_requires_yes(self):
        gh = FakeGh()
        rc = pv.publish(self.video, "module1_v1", yes=False, root=self.root, run=gh, regenerate=False, echo=lambda *_: None)
        self.assertEqual(rc, 2)
        self.assertEqual(gh.calls, [])
        self.assertIsNone(pv.find_slot(self.manifest(), "module1_v1")["url"])

    def test_uploads_to_release_and_fills_url(self):
        gh = FakeGh(release_exists=False)
        rc = pv.publish(self.video, "Module 1", yes=True, root=self.root, run=gh, duration_s=12,
                        regenerate=False, echo=lambda *_: None)
        self.assertEqual(rc, 0)
        verbs = [c[1:3] for c in gh.calls]
        self.assertIn(["release", "create"], verbs)
        upload = next(c for c in gh.calls if c[1:3] == ["release", "upload"])
        self.assertEqual(upload[3], "demo-videos")
        self.assertTrue(upload[4].endswith("module1_v1.mp4"))
        slot = pv.find_slot(self.manifest(), "module1_v1")
        self.assertEqual(slot["url"], "https://github.com/o/r/releases/download/demo-videos/module1_v1.mp4")
        self.assertEqual((slot["release_tag"], slot["recorded"], slot["duration_s"]), ("demo-videos", True, 12))

    def test_existing_release_is_not_recreated(self):
        gh = FakeGh(release_exists=True)
        pv.publish(self.video, "module1_v1", yes=True, root=self.root, run=gh, regenerate=False, echo=lambda *_: None)
        self.assertNotIn(["release", "create"], [c[1:3] for c in gh.calls])

    def test_rejects_unknown_slot_and_non_video(self):
        gh = FakeGh()
        self.assertEqual(pv.publish(self.video, "nope", yes=True, root=self.root, run=gh, echo=lambda *_: None), 1)
        doc = self.root / "notes.pdf"
        doc.write_bytes(b"%PDF-")
        self.assertEqual(pv.publish(doc, "module1_v1", yes=True, root=self.root, run=gh, echo=lambda *_: None), 1)
        self.assertEqual(gh.calls, [])


if __name__ == "__main__":
    unittest.main()

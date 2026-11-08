"""The public twin bundle (tools/build_twin_site.py): allow-list and confidentiality scan. Offline.

    python -m unittest tests.test_twin_site -v
"""

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_twin_site as bts  # noqa: E402

quiet = lambda *_: None  # noqa: E731


def _fake_repo(t: Path) -> None:
    """A minimal repo: a twin that fetches two data files, plus things that must not ship."""
    (t / "twin").mkdir()
    (t / "twin" / "index.html").write_text('<script type="module" src="main.js"></script>')
    (t / "twin" / "main.js").write_text(
        "const ROOT = '../';\nconst P = 'protocols/pipeline_mvp.json';\n"
        "fetch(ROOT + P); fetch(`${ROOT}runs/index.json`);\n")
    (t / "protocols").mkdir()
    (t / "protocols" / "pipeline_mvp.json").write_text(json.dumps(
        {"kind": "pipeline", "modules": [{"module": "m", "chip": "chip/a_v1.json"}]}))
    (t / "protocols" / "secret_protocol.json").write_text("{}")
    (t / "runs").mkdir()
    (t / "runs" / "index.json").write_text(json.dumps({"runs": [{"file": "r.json", "chip_spec": "chip/a_v1.json"}]}))
    (t / "runs" / "r.json").write_text("{}")
    (t / "runs" / "unlisted.json").write_text("{}")
    (t / "chip").mkdir()
    (t / "chip" / "a_v1.json").write_text(json.dumps({"id": "a_v1", "chambers": []}))
    (t / "chip" / "b_v1.json").write_text("{}")
    (t / "docs").mkdir()
    (t / "docs" / "modules.json").write_text("{}")
    (t / "contracts" / "sources").mkdir(parents=True)
    (t / "contracts" / "sources" / "sheet.xlsx").write_bytes(b"PK\x03\x04")


class AllowList(unittest.TestCase):
    def test_real_repo_bundle_stays_inside_the_fence(self):
        files = bts.discover(ROOT)
        self.assertIn("twin/index.html", files)
        self.assertIn("docs/modules.json", files) if (ROOT / "docs/modules.json").is_file() else None
        self.assertIn("runs/index.json", files)
        for rel in files:
            self.assertTrue(rel.startswith(bts.ALLOWED_PREFIXES), rel)
            self.assertFalse(rel.startswith(bts.NEVER_PREFIXES), rel)
            self.assertNotIn(Path(rel).suffix, bts.BLOCKED_EXTS)

    def test_copies_nothing_outside_the_allow_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            _fake_repo(t)
            allowed = bts.discover(t)
            self.assertEqual(set(allowed), {
                "twin/index.html", "twin/main.js", "protocols/pipeline_mvp.json", "runs/index.json",
                "runs/r.json", "chip/a_v1.json", "docs/modules.json"})  # no video manifest in this repo
            out = t / "_site"
            self.assertEqual(bts.build(t, out, echo=quiet), 0)
            written = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
            self.assertEqual(written - set(allowed), {"index.html", ".nojekyll", bts.MARKER})

    def test_reduced_files_keep_only_what_the_twin_reads(self):
        for rel, rule in bts.REDUCE.items():
            if rule.get("chip"):
                continue
            reader = (ROOT / rule["read_by"]).read_text()
            for path in rule["keep"]:
                for name in re.findall(r"\w+", path):
                    self.assertIn(name, reader, f"{rule['read_by']} no longer reads {name} of {rel}")
        if (ROOT / "contracts/blocks.json").is_file():
            doc = json.loads(bts.payload(ROOT, "contracts/blocks.json"))
            self.assertEqual(set(doc), {"fabrication_floors", "_published"})
            self.assertEqual(set(doc["fabrication_floors"]), {"min_port_mm"})

    def test_module3_files_publish_no_source_file_name(self):
        for rel in ("chip/module3_v1_translated.json", "chip/modules/module3_step1_3.json"):
            if not (ROOT / rel).is_file():
                continue
            self.assertIn(rel, bts.REDUCE)
            text = bts.payload(ROOT, rel).decode()
            self.assertNotRegex(text, r"\.xlsx|contracts/sources")
            self.assertEqual(bts.scan(ROOT, [rel]), [])
        spec = json.loads(bts.payload(ROOT, "chip/module3_v1_translated.json"))
        for key in ("id", "chambers", "channels", "ports", "footprint", "layers"):
            self.assertIn(key, spec)
        self.assertEqual(spec["description"].count(". "), 0)  # one sentence

    def test_whole_real_bundle_passes_the_scan(self):
        self.assertEqual(bts.scan(ROOT, bts.discover(ROOT)), [])

    def test_refuses_to_wipe_a_foreign_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            _fake_repo(t)
            out = t / "elsewhere"
            out.mkdir()
            (out / "keep.txt").write_text("mine")
            self.assertEqual(bts.build(t, out, echo=quiet), 1)
            self.assertTrue((out / "keep.txt").is_file())


class Videos(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        _fake_repo(self.t)
        (self.t / "docs" / "videos").mkdir()
        (self.t / bts.VIDEO_MANIFEST).write_text(json.dumps({"videos": [
            {"module": "Module 1", "chip_id": "module1_v1", "recorded": True, "release_tag": "demo-videos",
             "url": "https://github.com/owner/private-repo/releases/download/demo-videos/module1_v1.mp4"},
            {"module": "Module 2", "chip_id": "spc_demo_v1", "recorded": True, "release_tag": "demo-videos",
             "url": "https://github.com/owner/private-repo/releases/download/demo-videos/spc_demo_v1.mp4"}]}))
        (self.t / "docs" / "modules.json").write_text(json.dumps({"stages": [{"module": {"video": {
            "chip_id": "module1_v1", "url": "https://github.com/owner/private-repo/x.mp4"}}}]}))
        self.dl = self.t / "downloaded"
        self.dl.mkdir()
        (self.dl / "module1_v1.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
        (self.dl / "stray.mp4").write_bytes(b"\x00")
        (self.dl / "module1_v1.xlsx").write_bytes(b"PK\x03\x04")

    def tearDown(self):
        self.tmp.cleanup()

    def test_videos_are_served_from_the_site_and_private_urls_never_ship(self):
        out = self.t / "_site"
        self.assertEqual(bts.build(self.t, out, echo=quiet, video_dir=self.dl), 0)
        self.assertTrue((out / "videos" / "module1_v1.mp4").is_file())
        self.assertEqual(sorted(p.name for p in (out / "videos").iterdir()), ["module1_v1.mp4"])
        slots = {v["chip_id"]: v for v in json.loads((out / bts.VIDEO_MANIFEST).read_text())["videos"]}
        self.assertEqual(slots["module1_v1"]["url"], "videos/module1_v1.mp4")
        self.assertIsNone(slots["spc_demo_v1"]["url"])  # not downloaded: no private url instead
        self.assertFalse(slots["spc_demo_v1"]["recorded"])
        catalog = json.loads((out / "docs" / "modules.json").read_text())
        self.assertEqual(catalog["stages"][0]["module"]["video"]["url"], "videos/module1_v1.mp4")
        for p in out.rglob("*.json"):
            self.assertNotIn("private-repo", p.read_text(), p)

    def test_without_videos_every_url_is_null(self):
        out = self.t / "_site"
        self.assertEqual(bts.build(self.t, out, echo=quiet), 0)
        self.assertFalse((out / "videos").exists())
        for v in json.loads((out / bts.VIDEO_MANIFEST).read_text())["videos"]:
            self.assertIsNone(v["url"])

    def test_video_dir_inside_the_output_is_refused(self):
        out = self.t / "_site"
        (out / "videos").mkdir(parents=True)
        (out / bts.MARKER).write_text("")
        self.assertEqual(bts.build(self.t, out, echo=quiet, video_dir=out / "videos"), 1)


class Scan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        _fake_repo(self.t)

    def tearDown(self):
        self.tmp.cleanup()

    def _refused(self, rel, content):
        p = self.t / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if isinstance(content, bytes) else content.encode())
        return bts.scan(self.t, [rel])

    def test_clean_bundle_passes(self):
        self.assertEqual(bts.scan(self.t, bts.discover(self.t)), [])

    def test_rejects_planted_xlsx(self):
        self.assertTrue(self._refused("runs/data.xlsx", b"PK\x03\x04"))
        self.assertTrue(self._refused("runs/data.json", b"PK\x03\x04 a zip in disguise"))

    def test_rejects_absolute_and_user_paths(self):
        self.assertTrue(self._refused("runs/a.json", '{"note": "see /Users/someone/Desktop/run.json"}'))
        self.assertTrue(self._refused("runs/b.json", '{"note": "C:\\\\Users\\\\lab\\\\x"}'))
        self.assertTrue(self._refused("runs/c.json", '{"note": "/home/lab/data"}'))

    def test_rejects_price_and_vendor(self):
        self.assertTrue(self._refused("runs/p.json", '{"note": "kit at £450 per box"}'))
        self.assertTrue(self._refused("runs/q.json", '{"note": "costs 1,200 USD"}'))
        self.assertTrue(self._refused("runs/v.json", '{"note": "bought from Thermo Fisher"}'))

    def test_rejects_spreadsheet_file_names(self):
        self.assertTrue(self._refused("runs/n.json", '{"source": "the requirements.xlsx sheet"}'))
        (self.t / "contracts" / "sources" / "plain_name.txt").write_text("x")
        self.assertTrue(self._refused("runs/m.json", '{"source": "see plain_name.txt"}'))

    def test_rejects_contracts_sources(self):
        self.assertTrue(bts.scan(self.t, ["contracts/sources/sheet.xlsx"]))
        self.assertTrue(self._refused("runs/s.json", '{"source": "contracts/sources/sheet.xlsx"}'))

    def test_refusal_writes_nothing(self):
        (self.t / "runs" / "r.json").write_text('{"note": "/Users/someone/secret"}')
        out = self.t / "_site"
        self.assertEqual(bts.build(self.t, out, echo=quiet), 1)
        self.assertFalse(out.exists())

    def test_twin_code_is_not_flagged(self):
        twin_files = [r for r in bts.discover(ROOT) if r.startswith("twin/")]
        self.assertEqual(bts.scan(ROOT, twin_files), [])


if __name__ == "__main__":
    unittest.main()

"""tools/check_asbuilt.py: noticing an as-built file in a chip's export folder and comparing it.

    python -m unittest discover -s tests -v
"""

import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "tools"))

import check_asbuilt  # noqa: E402

CHIP_PATH = ROOT / "chip" / "module1_v1.json"
CHIP = json.loads(CHIP_PATH.read_text())


def rect_lines(layer, a, b, w):
    """A channel or pad as 4 LINEs: the rectangle of width w along a -> b."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy) or 1
    px, py = -dy / n * w / 2, dx / n * w / 2
    c = [(a[0] + px, a[1] + py), (b[0] + px, b[1] + py), (b[0] - px, b[1] - py), (a[0] - px, a[1] - py)]
    out = []
    for i in range(4):
        (x1, y1), (x2, y2) = c[i], c[(i + 1) % 4]
        out += ["0", "LINE", "8", layer, "10", f"{x1:.4f}", "20", f"{y1:.4f}", "11", f"{x2:.4f}", "21", f"{y2:.4f}"]
    return out


def ayrton_dxf(path, channel_width=0.3, pads=True):
    """A fake as-built file: the spec's chambers, every channel at channel_width, 2 mm pads under the ports."""
    at = {c["id"]: c for c in CHIP["chambers"]}
    pos = {p["id"]: p["position"][:2] for p in CHIP["ports"]} | {c["id"]: c["center"][:2] for c in CHIP["chambers"]}
    out = ["0", "SECTION", "2", "ENTITIES"]
    for c in CHIP["chambers"]:
        (x, y), (sx, sy) = c["center"][:2], c["size"][:2]
        out += rect_lines("CUT", (x - sx / 2, y), (x + sx / 2, y), sy)
    for ch in CHIP["channels"]:
        a, b = pos[ch["from"]], pos[ch["to"]]
        # trimmed at the chamber walls, as a cutter file would be
        def trim(p, q, cid):
            if cid not in at:
                return p
            c = at[cid]
            hx, hy = c["size"][0] / 2, c["size"][1] / 2
            t = min(hx / abs(q[0] - p[0]) if q[0] != p[0] else 1e9, hy / abs(q[1] - p[1]) if q[1] != p[1] else 1e9, 1.0)
            return (p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t)
        out += rect_lines("CUT", trim(a, b, ch["from"]), trim(b, a, ch["to"]), channel_width)
    if pads:
        for p in CHIP["ports"]:
            x, y = p["position"][:2]
            out += rect_lines("CUT", (x - 1, y), (x + 1, y), 2.0)
    out += ["0", "ENDSEC", "0", "EOF", ""]
    Path(path).write_text("\n".join(out))


class AsBuilt(unittest.TestCase):
    def test_nothing_new_in_the_real_folder(self):
        r = check_asbuilt.check(CHIP_PATH)
        self.assertEqual(r["incoming"], [])
        self.assertIsNone(r["dxf"])

    def test_a_new_file_with_different_widths_and_pads_is_reported_and_normalised(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "module1_v1"
            shutil.copytree(ROOT / "cad" / "exports" / "module1_v1", folder)
            ayrton_dxf(folder / "module1_v1_lasercut_ayrton.dxf")
            r = check_asbuilt.check(CHIP_PATH, folder)
            self.assertEqual([(i["file"], i["why"]) for i in r["incoming"]], [("module1_v1_lasercut_ayrton.dxf", "new")])
            text = " | ".join(r["differences"])
            self.assertIn("0.300 mm wide as built", text)
            self.assertIn("match no spec part", text)        # the six port pads
            self.assertNotIn("port p_in: hole", text)        # with a pad, the hole opens onto the cut
            self.assertEqual(r["dxf"], "module1_v1_asbuilt.dxf")
            saved = json.loads((folder / "asbuilt.json").read_text())
            self.assertEqual(saved["source_dxf"], "module1_v1_lasercut_ayrton.dxf")
            norm = (folder / "module1_v1_asbuilt.dxf").read_text()
            self.assertEqual(norm.count("CHANNELS"), 4 * len(CHIP["channels"]))
            self.assertEqual(norm.count("CHAMBERS"), 4 * len(CHIP["chambers"]))

    def test_a_replaced_generated_file_counts_as_incoming(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "module1_v1"
            shutil.copytree(ROOT / "cad" / "exports" / "module1_v1", folder)
            ayrton_dxf(folder / "module1_v1.dxf", channel_width=0.2, pads=False)   # same name as ours, his content
            r = check_asbuilt.check(CHIP_PATH, folder)
            self.assertIn(("module1_v1.dxf", "replaced"), [(i["file"], i["why"]) for i in r["incoming"]])


if __name__ == "__main__":
    unittest.main()

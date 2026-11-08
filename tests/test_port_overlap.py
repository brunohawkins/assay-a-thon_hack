"""The port-overlap rule in tools/check_chip_spec.py: a lid hole must open onto the cut area under it.

    python -m unittest discover -s tests -v
"""

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "tools"))

import check_chip_spec as ccs  # noqa: E402

MODULE1 = json.loads((ROOT / "chip" / "module1_v1.json").read_text())


def tiny(port_xy=(2.0, 5.0), channel_to=(10.0, 5.0), width=0.2, port_um=None):
    """A chip with one port, one channel and one chamber, no DXF (spec geometry)."""
    port = {"id": "p", "type": "inlet", "position": [*port_xy, 2.0]}
    if port_um:
        port["width_um"] = port_um
    return {"id": "overlap_fixture_no_dxf", "ports": [port],
            "chambers": [{"id": "c", "type": "exchange", "center": [12.0, 5.0, 0.5], "size": [4.0, 4.0, 0.3], "volume_ul": 4.8}],
            "channels": [{"id": "ch", "from": "p", "to": "c", "width_mm": width, "depth_mm": 0.3,
                          "path": [list(port_xy), list(channel_to)]}]}


class PortOverlap(unittest.TestCase):
    def test_module1_fails_on_every_port_as_cut(self):
        # Known and expected (MJ / Ayrton are aware): the exported DXF stops each channel at the
        # edge of its port square, so no 0.8 mm lid hole opens onto a channel.
        rows = ccs.port_overlaps(MODULE1)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(r["source"].endswith("module1_v1.dxf") for r in rows))
        self.assertTrue(all(r["overlap_mm2"] < 0.01 and r["need_mm2"] > 0 for r in rows))
        f = ccs.Findings()
        ccs.check_port_overlap(MODULE1, f)
        self.assertEqual(len([e for e in f.errors if e.startswith("port overlap:")]), 6)

    def test_a_channel_ending_at_the_hole_centre_is_not_enough_for_a_small_bore(self):
        r, = ccs.port_overlaps(tiny(channel_to=(10.0, 5.0)))   # starts at the port centre: half the bore
        self.assertAlmostEqual(r["overlap_mm2"], 0.2 * 0.4, delta=0.01)
        self.assertLess(r["overlap_mm2"], r["need_mm2"])

    def test_a_channel_across_the_whole_bore_passes(self):
        chip = tiny()
        chip["channels"][0]["path"] = [[1.4, 5.0], [10.0, 5.0]]   # starts beyond the hole's far edge
        r, = ccs.port_overlaps(chip)
        self.assertGreaterEqual(r["overlap_mm2"] + r["tolerance_mm2"], r["need_mm2"])

    def test_a_2_mm_hole_needs_the_full_1_mm2(self):
        chip = tiny(port_um=2000, width=0.8)
        r, = ccs.port_overlaps(chip)
        self.assertEqual(r["need_mm2"], 1.0)
        self.assertLess(r["overlap_mm2"], 1.0)                    # 0.8 x 1.0 mm from the centre
        chip["channels"][0]["path"] = [[0.5, 5.0], [10.0, 5.0]]   # across the whole hole: 0.8 x 2 mm
        r, = ccs.port_overlaps(chip)
        self.assertGreaterEqual(r["overlap_mm2"], 1.0)

    def test_a_port_over_a_chamber_passes(self):
        chip = tiny(port_xy=(12.0, 5.0), channel_to=(12.0, 9.0))
        r, = ccs.port_overlaps(chip)
        self.assertAlmostEqual(r["overlap_mm2"], 0.64, delta=0.01)


if __name__ == "__main__":
    unittest.main()

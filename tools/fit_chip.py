"""Fit a chip spec into the 30 x 20 mm build envelope.

    python tools/fit_chip.py chip/<spec>.json          # report only
    python tools/fit_chip.py chip/<spec>.json --write   # rewrite it in place

We can only print 30 x 20 mm, one fluidic layer, 0.3 mm deep. Several chips were drawn
at 150 x 100 mm. This scales a chip down to fit, UNIFORMLY in x and y so nothing is
distorted, then repairs what pure scaling would break:

  * DEPTH IS NEVER SCALED. 0.3 mm is a fabrication limit, not a drawing choice.
  * Channel widths and chamber sides are CLAMPED at the fabrication floors in
    contracts/blocks.json, so a scaled channel can never fall below what we can make.
    A clamped feature is reported, because clamping changes the aspect ratio.
  * volume_ul is RECOMPUTED from the new geometry, as a box or, for shape "cylinder",
    as pi/4 of its bounding box. A scaled chamber holds far less, and a stale volume
    would silently corrupt every protocol step that exchanges it.
  * A cylinder's two in-plane sides are one diameter, so they are clamped together.
  * height_um is rewritten whenever the depth is clamped: check_chip_spec.py reads it in
    preference to size[2].
  * Channel path waypoints move with everything else, so channels still meet the
    chambers and ports they join.
  * Ports keep their z, which is set by the layer stack, not by the footprint.

The point of one shared tool is that everyone's chips come out on the same scale with
the same floors, so modules still line up with each other afterwards.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BLOCKS = json.loads((ROOT / "contracts" / "blocks.json").read_text())
ENV = BLOCKS["envelope"]
FLOOR = BLOCKS["fabrication_floors"]

MAX_X, MAX_Y = ENV["max_x_mm"], ENV["max_y_mm"]
MAX_D = ENV["max_depth_mm"]
MIN_W = FLOOR["min_open_channel_mm"]
MIN_WALL = FLOOR["min_wall_mm"]
MARGIN = MIN_WALL


def fit(chip: dict) -> tuple[dict, list[str], float]:
    fx, fy = chip["footprint"]["x"], chip["footprint"]["y"]
    usable_x, usable_y = MAX_X - 2 * MARGIN, MAX_Y - 2 * MARGIN
    s = min(usable_x / fx, usable_y / fy, 1.0)
    notes: list[str] = []
    if s >= 1.0:
        return chip, ["already inside the envelope; unchanged"], 1.0

    def px(v):
        return round(MARGIN + v * s, 3)

    for c in chip.get("chambers", []):
        c["center"][0], c["center"][1] = px(c["center"][0]), px(c["center"][1])
        cylinder = c.get("shape") == "cylinder"
        if cylinder:
            # size is [diameter, diameter, depth]: the two in-plane axes are one number.
            # Clamping them independently can leave them unequal, which breaks the
            # cylinder contract in contracts/schemas/chip_spec.schema.json.
            want = max(c["size"][0], c["size"][1]) * s
            if want < MIN_W:
                notes.append(f"chamber '{c['id']}' diameter {want:.3f} -> clamped to {MIN_W} mm")
                want = MIN_W
            c["size"][0] = c["size"][1] = round(want, 3)
        else:
            for i in (0, 1):
                want = c["size"][i] * s
                if want < MIN_W:
                    notes.append(f"chamber '{c['id']}' side {want:.3f} -> clamped to {MIN_W} mm")
                    want = MIN_W
                c["size"][i] = round(want, 3)
        depth = min(c["size"][2], MAX_D)
        if depth != c["size"][2] and "height_um" in c:
            # check_chip_spec.py reads height_um in preference to size[2]; leaving it alone
            # would have it checking a depth this chip no longer has.
            notes.append(f"chamber '{c['id']}' height_um {c['height_um']:g} -> {depth * 1000:g} (depth clamped)")
            c["height_um"] = round(depth * 1000, 3)
        c["size"][2] = depth
        # A cylinder is pi/4 of its bounding box. Calling it a box inflated it by 27.3%.
        area = math.pi / 4 * c["size"][0] * c["size"][1] if cylinder else c["size"][0] * c["size"][1]
        c["volume_ul"] = round(area * c["size"][2], 3)

    for ch in chip.get("channels", []):
        want = ch["width_mm"] * s
        if want < MIN_W:
            notes.append(f"channel '{ch['id']}' width {want:.3f} -> clamped to {MIN_W} mm")
            want = MIN_W
        ch["width_mm"] = round(want, 3)
        ch["depth_mm"] = min(ch["depth_mm"], MAX_D)
        # Waypoints are geometry too. Unscaled, they kept the old 150 x 100 mm board's
        # coordinates while the chambers and ports they join moved, so a fitted chip's
        # channels ran nowhere near its own features.
        for pt in ch.get("path") or []:
            pt[0], pt[1] = px(pt[0]), px(pt[1])

    for p in chip.get("ports", []):
        p["position"][0], p["position"][1] = px(p["position"][0]), px(p["position"][1])

    chip["footprint"] = {"x": MAX_X, "y": MAX_Y}
    return chip, notes, s


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("spec", type=Path)
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    chip = json.loads(a.spec.read_text())
    before = (chip["footprint"]["x"], chip["footprint"]["y"])
    chip, notes, s = fit(chip)
    print(f"{a.spec.name}: {before[0]} x {before[1]} mm -> "
          f"{chip['footprint']['x']} x {chip['footprint']['y']} mm  (scale {s:.3f})")
    for n in notes:
        print(f"  {n}")
    if a.write and s < 1.0:
        a.spec.write_text(json.dumps(chip, indent=2) + "\n")
        print(f"  wrote {a.spec}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

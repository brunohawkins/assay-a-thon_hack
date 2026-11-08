"""Has the as-built CAD for a chip arrived (Ayrton's file), and does it match the chip spec?

    python tools/check_asbuilt.py                        # Module 1: chip/module1_v1.json
    python tools/check_asbuilt.py --chip chip/<id>.json

Looks in the chip's export folder (cad/exports/<chip_id>/) for anything our scripts did not
write: a file cad/make_train_cad.py or cad/dxf_closed.py would not produce, or one of theirs
whose content differs from a fresh run (replaced by hand). Each such DXF (LINE loops and closed
LWPOLYLINEs, any layer) or STL is compared with the chip spec:
  - chambers: centre and size
  - channels: found or not, width, and where it ends
  - depth: an STL's height against the spec's chamber / channel depth
  - ports: how much each lid hole opens onto the cut area (the check_chip_spec.py rule)

If the as-built geometry differs from the spec, it writes
  cad/exports/<chip_id>/asbuilt.json            what came in, every difference, which DXF to use
  cad/exports/<chip_id>/<chip_id>_asbuilt.dxf   the as-built outlines in our DXF convention
and the twin's Module 1 view then draws and solves the flow on the as-built channels, since that
is what was printed. Exit 0: nothing new, or it matches; 1: differences (listed). Never edits
the as-built file itself. Standard library + jsonschema.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "operator"))
sys.path.insert(0, str(ROOT / "tools"))

import check_chip_spec as ccs  # noqa: E402

TOL_MM = 0.05          # position and size tolerance
TOL_DEPTH_MM = 0.02
OURS_EXTRA = {".chip_id", "asbuilt.json"}


# --------------------------------------------------------------------------- what we generate

def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def regenerate(chip_path: Path, chip_id: str) -> dict[str, bytes]:
    """What our scripts write for this chip, generated fresh in a scratch folder: name -> bytes."""
    with tempfile.TemporaryDirectory() as tmp:
        mtc = _load(ROOT / "cad" / "make_train_cad.py", "_mtc_asbuilt")
        mtc.EXPORTS = Path(tmp)
        argv = sys.argv
        try:
            sys.argv = ["make_train_cad.py", "--chip", str(chip_path)]
            import contextlib
            import io
            with contextlib.redirect_stdout(io.StringIO()):
                mtc.main()
        finally:
            sys.argv = argv
        folder = Path(tmp) / chip_id
        if not folder.is_dir():
            folder = Path(tmp)
        # Both DXFs come out of make_train_cad.py in one pass now: <chip>.dxf (LINE
        # centrelines, the reference drawing) and <chip>_closed.dxf (closed polylines, the file
        # to machine). There is no separate cad/dxf_closed.py step to run afterwards.
        return {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}


def incoming(folder: Path, fresh: dict[str, bytes]) -> list[tuple[Path, str]]:
    """Files in the export folder that our scripts did not write ('new') or that differ ('replaced')."""
    found = []
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.name in OURS_EXTRA or p.name.endswith("_asbuilt.dxf"):
            continue
        if p.name not in fresh:
            found.append((p, "new"))
        elif hashlib.sha256(p.read_bytes()).digest() != hashlib.sha256(fresh[p.name]).digest():
            found.append((p, "replaced"))
    return found


# --------------------------------------------------------------------------- reading CAD

def dxf_loops(path: Path) -> list[dict]:
    """Closed outlines of a DXF: LINE chains and closed LWPOLYLINEs, any layer."""
    tokens = path.read_text(errors="replace").split("\n")
    lines, polys = [], []
    cur, kind = None, None
    for code, val in zip(tokens[0::2], tokens[1::2]):
        code, val = code.strip(), val.strip()
        if code == "0":
            if kind == "LINE" and cur and all(k in cur for k in ("10", "20", "11", "21")):
                lines.append((cur.get("8", ""), (cur["10"], cur["20"]), (cur["11"], cur["21"])))
            elif kind == "LWPOLYLINE" and cur and cur.get("pts"):
                polys.append({"layer": cur.get("8", ""), "pts": cur["pts"]})
            kind = val
            cur = {"pts": []} if val in ("LINE", "LWPOLYLINE") else None
        elif cur is not None:
            if code == "8":
                cur["8"] = val
            elif kind == "LWPOLYLINE" and code == "10":
                cur["pts"].append([float(val), None])
            elif kind == "LWPOLYLINE" and code == "20" and cur["pts"]:
                cur["pts"][-1][1] = float(val)
            elif kind == "LINE" and code in ("10", "20", "11", "21"):
                cur[code] = float(val)
    by_layer: dict[str, list] = {}
    for layer, a, b in lines:
        by_layer.setdefault(layer, []).append((a, b))
    for layer, segs in by_layer.items():
        left = list(segs)
        while left:
            a, b = left.pop(0)
            loop = [a, b]
            while math.dist(loop[-1], loop[0]) > 1e-3:
                nxt = next((i for i, (p, q) in enumerate(left) if math.dist(p, loop[-1]) <= 1e-3 or math.dist(q, loop[-1]) <= 1e-3), None)
                if nxt is None:
                    break
                p, q = left.pop(nxt)
                loop.append(q if math.dist(p, loop[-1]) <= 1e-3 else p)
            if math.dist(loop[-1], loop[0]) <= 1e-3:
                polys.append({"layer": layer, "pts": [tuple(x) for x in loop[:-1]]})
    for p in polys:
        p["pts"] = [tuple(x) for x in p["pts"]]
        p.update(_shape(p["pts"]))
    return polys


def _shape(pts: list[tuple[float, float]]) -> dict:
    """Centre, the two side lengths of the outline's best rectangle, its long-axis ends."""
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
    best = None
    for i in range(len(pts)):                      # rotate to each edge's direction, keep the tightest box
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % len(pts)]
        ang = math.atan2(y2 - y1, x2 - x1)
        c, s = math.cos(ang), math.sin(ang)
        us = [(x - cx) * c + (y - cy) * s for x, y in pts]
        vs = [-(x - cx) * s + (y - cy) * c for x, y in pts]
        w, h = max(us) - min(us), max(vs) - min(vs)
        if best is None or w * h < best[0] * best[1] - 1e-12:
            ucen, vcen = (max(us) + min(us)) / 2, (max(vs) + min(vs)) / 2
            best = (w, h, ang, cx + ucen * c - vcen * s, cy + ucen * s + vcen * c)
    w, h, ang, x0, y0 = best
    long, short = (w, h) if w >= h else (h, w)
    axis = ang if w >= h else ang + math.pi / 2
    dx, dy = math.cos(axis) * long / 2, math.sin(axis) * long / 2
    return {"center": (x0, y0), "long": long, "short": short, "ends": [(x0 - dx, y0 - dy), (x0 + dx, y0 + dy)]}


def stl_extent(path: Path) -> tuple[list[float], list[float]]:
    data = path.read_bytes()
    if data[:5].lower() == b"solid" and b"facet" in data[:400]:
        pts = [list(map(float, line.split()[1:4])) for line in data.decode(errors="replace").splitlines() if line.strip().startswith("vertex")]
    else:
        n = struct.unpack("<I", data[80:84])[0]
        pts = [struct.unpack("<3f", data[84 + i * 50 + 12 + k * 12: 84 + i * 50 + 24 + k * 12]) for i in range(n) for k in range(3)]
    lo = [min(p[i] for p in pts) for i in range(3)]
    hi = [max(p[i] for p in pts) for i in range(3)]
    return lo, hi


# --------------------------------------------------------------------------- comparing

def compare_dxf(chip: dict, loops: list[dict]) -> tuple[list[str], dict]:
    """Differences between as-built outlines and the spec; also how each outline was matched."""
    diffs, used, match = [], set(), {"chambers": {}, "channels": {}}
    at = {c["id"]: c["center"][:2] for c in chip["chambers"]} | {p["id"]: p["position"][:2] for p in chip["ports"]}
    for c in chip["chambers"]:
        cands = [(math.dist(lp["center"], c["center"][:2]), k) for k, lp in enumerate(loops) if k not in used and lp["short"] > min(c["size"][:2]) * 0.5]
        if not cands or min(cands)[0] > max(c["size"][:2]):
            diffs.append(f"chamber {c['id']}: not found in the as-built outline")
            continue
        d, k = min(cands)
        used.add(k)
        lp = loops[k]
        match["chambers"][c["id"]] = k
        size = sorted(c["size"][:2], reverse=True)
        if d > TOL_MM:
            diffs.append(f"chamber {c['id']}: centre moved {d:.2f} mm, to ({lp['center'][0]:.2f}, {lp['center'][1]:.2f})")
        if abs(lp["long"] - size[0]) > TOL_MM or abs(lp["short"] - size[1]) > TOL_MM:
            diffs.append(f"chamber {c['id']}: {lp['long']:.2f} x {lp['short']:.2f} mm as built, {size[0]:g} x {size[1]:g} in the spec")
    for ch in chip["channels"]:
        a, b = at.get(ch["from"]), at.get(ch["to"])
        if a is None or b is None:
            continue
        def score(lp):
            e1, e2 = lp["ends"]
            # the outline's axis must lie along the a-b segment: distance of its ends from that line
            return max(_dist_to_segment(e1, a, b), _dist_to_segment(e2, a, b))
        cands = [(score(lp), k) for k, lp in enumerate(loops) if k not in used and lp["short"] < 3 * ch["width_mm"] + 0.5]
        if not cands or min(cands)[0] > max(0.5, 2 * ch["width_mm"]):
            diffs.append(f"channel {ch['id']} ({ch['from']} -> {ch['to']}): not found in the as-built outline")
            continue
        _, k = min(cands)
        used.add(k)
        lp = loops[k]
        match["channels"][ch["id"]] = k
        if abs(lp["short"] - ch["width_mm"]) > TOL_MM:
            diffs.append(f"channel {ch['id']}: {lp['short']:.3f} mm wide as built, {ch['width_mm']:g} in the spec")
    extra = [k for k in range(len(loops)) if k not in used]
    if extra:
        diffs.append(f"{len(extra)} outline(s) in the as-built file match no spec part (e.g. port pads): "
                     + ", ".join(f"{loops[k]['long']:.2f} x {loops[k]['short']:.2f} mm at ({loops[k]['center'][0]:.2f}, {loops[k]['center'][1]:.2f})" for k in extra[:6]))
    return diffs, match


def _dist_to_segment(p, a, b) -> float:
    ax, ay, bx, by = *a, *b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / (dx * dx + dy * dy or 1)))
    return math.dist(p, (ax + t * dx, ay + t * dy))


def write_normalised_dxf(path: Path, loops: list[dict], match: dict) -> None:
    """The as-built outlines in our convention: one 4-LINE rectangle per part, CHAMBERS / CHANNELS layers."""
    out = ["0", "SECTION", "2", "HEADER", "9", "$INSUNITS", "70", "4", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]
    for layer, ids in (("CHAMBERS", match["chambers"]), ("CHANNELS", match["channels"])):
        for k in ids.values():
            lp = loops[k]
            (x1, y1), (x2, y2) = lp["ends"]
            ux, uy = (x2 - x1) / (lp["long"] or 1), (y2 - y1) / (lp["long"] or 1)
            px, py = -uy * lp["short"] / 2, ux * lp["short"] / 2
            corners = [(x1 + px, y1 + py), (x2 + px, y2 + py), (x2 - px, y2 - py), (x1 - px, y1 - py)]
            for i in range(4):
                (ax, ay), (bx, by) = corners[i], corners[(i + 1) % 4]
                out += ["0", "LINE", "8", layer, "10", f"{ax:.4f}", "20", f"{ay:.4f}", "30", "0.0", "11", f"{bx:.4f}", "21", f"{by:.4f}", "31", "0.0"]
    out += ["0", "ENDSEC", "0", "EOF", ""]
    path.write_text("\n".join(out))


def check(chip_path: Path, folder: Path | None = None) -> dict:
    chip = json.loads(chip_path.read_text())
    folder = folder or ROOT / "cad" / "exports" / chip["id"]
    fresh = regenerate(chip_path, chip["id"])
    found = incoming(folder, fresh)
    report = {"chip": chip["id"], "folder": str(folder), "incoming": [], "differences": [], "dxf": None}
    best_dxf = None
    for path, why in found:
        item = {"file": path.name, "why": why}
        if path.suffix.lower() == ".dxf":
            loops = dxf_loops(path)
            diffs, match = compare_dxf(chip, loops)
            rows = ccs.port_overlaps(chip, polys=[lp["pts"] for lp in loops], source=path.name) if loops else []
            short = [r for r in rows if r["overlap_mm2"] + r["tolerance_mm2"] < r["need_mm2"]]
            diffs += [f"port {r['port']}: hole opens onto {r['overlap_mm2']:.2f} mm2 (needs {r['need_mm2']:.2f})" for r in short]
            item.update(outlines=len(loops), differences=diffs)
            if loops and (best_dxf is None or len(match["channels"]) > best_dxf[2]):
                best_dxf = (path, (loops, match), len(match["channels"]))
        elif path.suffix.lower() == ".stl":
            lo, hi = stl_extent(path)
            part = next((x for x in chip["chambers"] + chip["ports"] if x["id"] == path.stem), None)
            diffs = []
            if part and "size" in part:
                if abs((hi[2] - lo[2]) - part["size"][2]) > TOL_DEPTH_MM:
                    diffs.append(f"{part['id']}: {hi[2] - lo[2]:.3f} mm deep as built, {part['size'][2]:g} in the spec")
                for i, axis in enumerate("xy"):
                    if abs((hi[i] - lo[i]) - part["size"][i]) > TOL_MM:
                        diffs.append(f"{part['id']}: {hi[i] - lo[i]:.2f} mm in {axis} as built, {part['size'][i]:g} in the spec")
            item.update(extent_mm=[round(hi[i] - lo[i], 3) for i in range(3)], differences=diffs)
        else:
            item["differences"] = []
        report["incoming"].append(item)
        report["differences"] += [f"{path.name}: {d}" for d in item["differences"]]
    if best_dxf and report["differences"]:
        path, (loops, match), _ = best_dxf
        out = folder / f"{chip['id']}_asbuilt.dxf"
        write_normalised_dxf(out, loops, match)
        report["dxf"] = out.name
        report["source_dxf"] = path.name
        (folder / "asbuilt.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--chip", type=Path, default=ROOT / "chip" / "module1_v1.json")
    args = ap.parse_args(argv)
    r = check(args.chip)
    if not r["incoming"]:
        print(f"{r['chip']}: nothing new in {Path(r['folder']).relative_to(ROOT)}: every file there is one our scripts write, unchanged.")
        return 0
    for item in r["incoming"]:
        print(f"{item['why'].upper():9s} {item['file']}" + (f" ({item.get('outlines')} outlines)" if "outlines" in item else ""))
        for d in item["differences"]:
            print(f"   - {d}")
    if r["dxf"]:
        print(f"as-built geometry differs from the spec: wrote asbuilt.json and {r['dxf']}; the twin's Module 1 view uses it.")
        return 1
    print("the incoming files match the spec.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

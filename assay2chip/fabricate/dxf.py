"""A DXF a CAM importer cannot misread: closed polylines, and a HEADER saying millimetres.

Two faults were found in every DXF in this repo, and both are fixed here rather than in
cad/, because cad/make_train_cad.py is not in S11's scope:

 1. NO UNITS HEADER. `cad/make_train_cad.py`'s `write_dxf()` opens straight at
    `SECTION/ENTITIES` with no HEADER at all. DXF's default for an unstated $INSUNITS is
    0, "unitless", and a CAM or laser importer that assumes inches then scales a 30 mm
    chip to 762 mm - 25.4x - and the operator has no way to tell from the file. So this
    writer emits a HEADER with **$INSUNITS = 4 (millimetres)**, plus $MEASUREMENT = 1
    (metric) and $EXTMIN/$EXTMAX, which together leave an importer nothing to guess.

 2. LOOSE LINES, NOT CONTOURS. make_train_cad writes each outline as four separate LINE
    entities. They meet end to end, but software that does not auto-join (LightBurn
    without it, for one) treats four LINEs as four open paths: it will not offset them,
    will not tell inside from outside, and may cut them as four strokes. Everything here
    is a **closed LWPOLYLINE** (group 70 = 1).

`cad/dxf_closed.py` DOES already do both, but it is a post-processor you have to remember
to run on a file make_train_cad has already written wrong, and it can only close outlines
that were drawn separately - it cannot merge a channel into the chamber it runs into.
S11 writes the contour directly from the cut region, so overlapping features come out as
ONE loop with no line buried inside the pocket. The header codes and the 70 = 1 closed
flag follow that file, so the two agree.

Layers:
    CUT         what the tool removes: the traced boundary of the whole cut region, outer
                loops counter-clockwise and islands clockwise. THIS is the cutting path.
    OUTLINE     the footprint, for alignment and stock size.
    CHAMBERS    one closed loop per chamber, for reference
    CHANNELS    one closed loop per routed channel RUN (so an L-shaped channel is two)
    PORTS       one closed loop per port hole, at its own bore
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.lib import contracts  # noqa: E402
from assay2chip.lib.io import write_text  # noqa: E402  atomic: a crash leaves no half DXF

from . import geometry  # noqa: E402

INSUNITS_MM = 4          # DXF $INSUNITS code for millimetres
MEASUREMENT_METRIC = 1   # DXF $MEASUREMENT: 0 imperial, 1 metric
PORT_CIRCLE_SEGMENTS = 48

LAYER_ORDER = ("OUTLINE", "CUT", "CHAMBERS", "CHANNELS", "PORTS")


def _header(lo: tuple[float, float], hi: tuple[float, float]) -> list[str]:
    """The header that was missing. $INSUNITS = 4 is the whole point of this module."""
    return [
        "0", "SECTION", "2", "HEADER",
        "9", "$ACADVER", "1", "AC1015",
        "9", "$INSUNITS", "70", str(INSUNITS_MM),
        "9", "$MEASUREMENT", "70", str(MEASUREMENT_METRIC),
        "9", "$LUNITS", "70", "2",
        "9", "$EXTMIN", "10", f"{lo[0]:.4f}", "20", f"{lo[1]:.4f}", "30", "0.0",
        "9", "$EXTMAX", "10", f"{hi[0]:.4f}", "20", f"{hi[1]:.4f}", "30", "0.0",
        "9", "$LIMMIN", "10", f"{lo[0]:.4f}", "20", f"{lo[1]:.4f}",
        "9", "$LIMMAX", "10", f"{hi[0]:.4f}", "20", f"{hi[1]:.4f}",
        "0", "ENDSEC",
    ]


def _tables(layers: list[str]) -> list[str]:
    """A LAYER table, so each layer exists by name even in an importer that will not
    invent one from an entity."""
    out = ["0", "SECTION", "2", "TABLES",
           "0", "TABLE", "2", "LAYER", "70", str(len(layers))]
    for n, name in enumerate(layers, start=1):
        out += ["0", "LAYER", "2", name, "70", "0", "62", str(n % 7 + 1), "6", "CONTINUOUS"]
    out += ["0", "ENDTAB", "0", "ENDSEC"]
    return out


def write(path: Path, loops_by_layer: dict[str, list[list[tuple[float, float]]]],
          extents: tuple[tuple[float, float], tuple[float, float]]) -> dict:
    """Write closed LWPOLYLINEs, grouped by layer, with the units header. Refuses a loop
    with fewer than three points: that is not a contour."""
    present = [name for name in LAYER_ORDER if loops_by_layer.get(name)]
    present += [k for k in loops_by_layer if k not in LAYER_ORDER and loops_by_layer[k]]
    out = _header(*extents) + _tables(present)
    out += ["0", "SECTION", "2", "ENTITIES"]
    counts: dict[str, int] = {}
    for layer in present:
        for loop in loops_by_layer[layer]:
            if len(loop) < 3:
                raise ValueError(f"{path.name}: {layer} loop has {len(loop)} points, "
                                 f"which cannot be a closed contour")
            out += ["0", "LWPOLYLINE", "8", layer, "100", "AcDbEntity",
                    "100", "AcDbPolyline", "90", str(len(loop)), "70", "1"]
            for x, y in loop:
                out += ["10", f"{x:.4f}", "20", f"{y:.4f}"]
            counts[layer] = counts.get(layer, 0) + 1
    out += ["0", "ENDSEC", "0", "EOF", ""]
    write_text(path, "\n".join(out))
    return {"polylines": sum(counts.values()), "by_layer": counts,
            "insunits": INSUNITS_MM, "units": "mm", "all_closed": True}


# --- reference loops straight from the spec ---------------------------------------------

def _rect(cx: float, cy: float, sx: float, sy: float) -> list[tuple[float, float]]:
    hx, hy = sx / 2, sy / 2
    return [(cx - hx, cy - hy), (cx + hx, cy - hy), (cx + hx, cy + hy), (cx - hx, cy + hy)]


def _oriented_rect(f: dict) -> list[tuple[float, float]]:
    ux, uy = f["dir"]
    hl, hw = f["len"] / 2, f["wid"] / 2
    return [(f["x"] + ux * sl * hl - uy * sw * hw, f["y"] + uy * sl * hl + ux * sw * hw)
            for sl, sw in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def _circle(cx: float, cy: float, r: float, n: int = PORT_CIRCLE_SEGMENTS):
    return [(cx + r * math.cos(2 * math.pi * k / n), cy + r * math.sin(2 * math.pi * k / n))
            for k in range(n)]


def loops_for(spec: dict, geom: dict) -> dict[str, list[list[tuple[float, float]]]]:
    """Every layer's loops. CUT is traced from the cut region, so overlapping features are
    one contour; the rest are per-feature reference outlines from the same plan, which is
    why a routed channel appears run by run and never as a straight chord."""
    p = geom["plan"]
    fx, fy = p["fx"], p["fy"]
    chamber_ids = {c["id"] for c in spec.get("chambers", [])}
    port_ids = {q["id"] for q in spec.get("ports", [])}

    chambers, channels, ports = [], [], []
    for f in p["features"]:
        if f["kind"] == "cyl":
            loop = _circle(f["x"], f["y"], f["r"])
            (ports if f["id"] in port_ids else chambers).append(loop)
        else:
            loop = _oriented_rect(f)
            (chambers if f["id"] in chamber_ids else channels).append(loop)

    return {
        "OUTLINE": [_rect(fx / 2, fy / 2, fx, fy)],
        "CUT": geometry.trace_loops(geom["xs"], geom["ys"], geom["depths"]),
        "CHAMBERS": chambers,
        "CHANNELS": channels,
        "PORTS": ports,
    }


def write_for(path: Path, spec: dict, geom: dict) -> dict:
    """The one call S11 makes: spec + geometry -> one closed, millimetre-declared DXF."""
    loops = loops_for(spec, geom)
    fx, fy = geom["plan"]["fx"], geom["plan"]["fy"]
    report = write(path, loops, ((0.0, 0.0), (fx, fy)))
    report["cut_loops"] = len(loops["CUT"])
    report["cut_islands"] = sum(1 for l in loops["CUT"] if geometry.loop_area(l) < 0)
    report["min_feature_floor_mm"] = contracts.floor("min_open_channel_mm")
    report["note"] = (
        f"$INSUNITS = {INSUNITS_MM} (mm) and $MEASUREMENT = {MEASUREMENT_METRIC} are in the "
        f"HEADER, so no importer can scale this 25.4x. Every entity is a closed LWPOLYLINE; "
        f"cut on the CUT layer, which is the traced boundary of the cut region and so has no "
        f"line running through the inside of a pocket."
    )
    return report

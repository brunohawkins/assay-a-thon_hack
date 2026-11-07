"""Write a copy of a LINE-only DXF in which every outline is ONE closed polyline.

    python cad/dxf_closed.py cad/exports/module1_v1/module1_v1.dxf
    # -> cad/exports/module1_v1/module1_v1_closed.dxf

cad/make_train_cad.py writes each chamber and channel outline as four separate LINE
entities. They do meet end to end, but some laser software (LightBurn without auto-join,
for instance) treats separate LINEs as open paths. This chains the LINEs of each layer into
loops and writes each loop as a closed LWPOLYLINE, on the same layer, with the same
vertices: the geometry does not change. It also declares the units as millimetres
($INSUNITS = 4), which the original file leaves unstated.

Refuses to write anything if a loop doesn't close (gap > 1 um). Standard library only.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

TOL = 1e-3  # mm


def read_lines(path: Path) -> list[tuple[str, tuple[float, float], tuple[float, float]]]:
    tokens = path.read_text().split("\n")
    out, cur, layer = [], None, None
    for code, val in zip(tokens[0::2], tokens[1::2]):
        code, val = code.strip(), val.strip()
        if code == "0":
            if cur is not None:
                out.append((layer, (cur["10"], cur["20"]), (cur["11"], cur["21"])))
            cur, layer = ({} if val == "LINE" else None), None
        elif cur is not None and code == "8":
            layer = val
        elif cur is not None and code in ("10", "20", "11", "21"):
            cur[code] = float(val)
    return out


def chain(segments: list[tuple[tuple[float, float], tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    """Group segments into closed loops by joining matching end points."""
    left = list(segments)
    loops = []
    while left:
        a, b = left.pop(0)
        loop = [a, b]
        while math.dist(loop[-1], loop[0]) > TOL:
            nxt = next((i for i, (p, q) in enumerate(left) if math.dist(p, loop[-1]) <= TOL or math.dist(q, loop[-1]) <= TOL), None)
            if nxt is None:
                raise ValueError(f"outline starting at {loop[0]} does not close (open end at {loop[-1]})")
            p, q = left.pop(nxt)
            loop.append(q if math.dist(p, loop[-1]) <= TOL else p)
        loops.append(loop[:-1])  # last point repeats the first
    return loops


def write_closed(path: Path, loops_by_layer: dict[str, list[list[tuple[float, float]]]]) -> None:
    out = ["0", "SECTION", "2", "HEADER", "9", "$INSUNITS", "70", "4", "0", "ENDSEC",
           "0", "SECTION", "2", "ENTITIES"]
    for layer, loops in loops_by_layer.items():
        for loop in loops:
            out += ["0", "LWPOLYLINE", "8", layer, "90", str(len(loop)), "70", "1"]   # 70 = 1: closed
            for x, y in loop:
                out += ["10", f"{x:.4f}", "20", f"{y:.4f}"]
    out += ["0", "ENDSEC", "0", "EOF", ""]
    path.write_text("\n".join(out))


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    src = Path(args[0])
    lines = read_lines(src)
    by_layer: dict[str, list] = {}
    for layer, a, b in lines:
        by_layer.setdefault(layer, []).append((a, b))
    try:
        loops = {layer: chain(segs) for layer, segs in by_layer.items()}
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    # Same geometry: every original LINE is an edge of exactly one loop.
    edges = {(lay, frozenset((tuple(round(c, 4) for c in p), tuple(round(c, 4) for c in q))))
             for lay, ls in loops.items() for loop in ls for p, q in zip(loop, loop[1:] + loop[:1])}
    orig = {(lay, frozenset((tuple(round(c, 4) for c in a), tuple(round(c, 4) for c in b)))) for lay, a, b in lines}
    if edges != orig:
        print("ERROR: the polylines would not reproduce the original lines exactly", file=sys.stderr)
        return 1
    dest = src.with_name(src.stem + "_closed.dxf")
    write_closed(dest, loops)
    n = sum(len(v) for v in loops.values())
    print(f"wrote {dest}: {n} closed polylines ({', '.join(f'{k} {len(v)}' for k, v in loops.items())}) "
          f"from {len(lines)} LINEs; same vertices, units mm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

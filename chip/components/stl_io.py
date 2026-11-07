"""Tiny STL helpers (standard library only): read, write, split into shells, measure."""

from __future__ import annotations

import struct
from collections import defaultdict
from pathlib import Path

Tri = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]


def read_stl(path: Path) -> list[Tri]:
    data = path.read_bytes()
    n = struct.unpack("<I", data[80:84])[0]
    if 84 + 50 * n == len(data):  # binary
        out = []
        for i in range(n):
            v = struct.unpack("<12f", data[84 + 50 * i: 84 + 50 * i + 48])
            out.append((v[3:6], v[6:9], v[9:12]))
        return out
    verts = [tuple(float(x) for x in line.split()[1:4])
             for line in data.decode("ascii", "replace").splitlines() if line.strip().startswith("vertex")]
    return [tuple(verts[i:i + 3]) for i in range(0, len(verts), 3)]


def write_stl(path: Path, tris: list[Tri], name: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    out = bytearray(name.encode()[:80].ljust(80, b" "))
    out += struct.pack("<I", len(tris))
    for a, b, c in tris:
        u = [b[i] - a[i] for i in range(3)]
        v = [c[i] - a[i] for i in range(3)]
        n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])
        out += struct.pack("<12fH", *n, *a, *b, *c, 0)
    path.write_bytes(bytes(out))


def shells(tris: list[Tri]) -> list[list[Tri]]:
    """Split a mesh into its separate connected pieces (one per part)."""
    parent: dict = {}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def key(p):
        return tuple(round(c, 3) for c in p)

    for t in tris:
        ks = [key(p) for p in t]
        for k in ks:
            parent.setdefault(k, k)
        for k in ks[1:]:
            ra, rb = find(ks[0]), find(k)
            if ra != rb:
                parent[ra] = rb
    groups = defaultdict(list)
    for t in tris:
        groups[find(key(t[0]))].append(t)
    return list(groups.values())


def bbox(tris: list[Tri]) -> tuple[list[float], list[float]]:
    pts = [p for t in tris for p in t]
    return [min(p[i] for p in pts) for i in range(3)], [max(p[i] for p in pts) for i in range(3)]


def volume(tris: list[Tri]) -> float:
    """Enclosed volume of a closed mesh (signed tetrahedra), in the mesh's units cubed."""
    v = 0.0
    for a, b, c in tris:
        v += (a[0] * (b[1] * c[2] - b[2] * c[1]) - a[1] * (b[0] * c[2] - b[2] * c[0])
              + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6
    return abs(v)


def transform(tris: list[Tri], scale: float, offset: tuple[float, float, float]) -> list[Tri]:
    return [tuple(tuple(p[i] * scale + offset[i] for i in range(3)) for p in t) for t in tris]

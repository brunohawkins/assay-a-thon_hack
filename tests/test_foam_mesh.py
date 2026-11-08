"""assay2chip/foam/mesh.py: write a polyMesh, parse it back, and check the geometry.

    python -m unittest tests.test_foam_mesh -v
    python -m unittest discover -s tests

Everything here is OFFLINE: the mesh is written, read back with the little polyMesh
reader below and checked against first principles - every cell closed and
positive-volume, every internal face normal pointing from owner to neighbour, every
boundary face normal pointing out, patch areas against bore areas, and the total volume
against the sum of the features' volumes within the staircase error the mesh reports for
itself. `checkMesh` itself is a separate test and skips cleanly when there is no runner or
no OpenFOAM (assay2chip/foam/README.md).
"""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "operator"), str(ROOT / "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from assay2chip.foam import mesh as fm  # noqa: E402

#: Cases go OUTSIDE the repo: a 1.5 M cell polyMesh is several hundred megabytes and
#: `assay2chip/work/` is not git-ignored in this worktree. FOAM_MESH_TEST_DIR overrides.
TMP = Path(os.environ.get("FOAM_MESH_TEST_DIR")
           or Path(tempfile.gettempdir()) / "a2c_foam_mesh_tests")
#: The one artefact that is small enough to keep: the per-chip report.
REPORT = ROOT / "assay2chip" / "work" / "foam" / "mesh_report.json"
MM_TO_M = fm.MM_TO_M


# --- a polyMesh reader, so the test believes the FILES and not the writer -------------


def _tokens(path: Path) -> list[str]:
    """The file's body tokens: the FoamFile header and comments stripped."""
    text = path.read_text()
    out, i = [], 0
    while i < len(text):
        c = text[i]
        if c == "/" and text[i:i + 2] == "/*":
            i = text.index("*/", i) + 2
            continue
        if c == "/" and text[i:i + 2] == "//":
            j = text.find("\n", i)
            i = len(text) if j < 0 else j + 1
            continue
        if c == "{" and out and out[-1] == "FoamFile":    # skip the FoamFile header dict
            depth = 0
            while i < len(text):
                depth += (text[i] == "{") - (text[i] == "}")
                i += 1
                if depth == 0:
                    break
            out.pop()
            continue
        if c in "{}":
            out.append(c)
            i += 1
            continue
        if c in "()":
            out.append(c)
            i += 1
            continue
        if c.isspace():
            i += 1
            continue
        j = i
        while j < len(text) and not text[j].isspace() and text[j] not in "(){}":
            j += 1
        out.append(text[i:j])
        i = j
    return out


class PolyMesh:
    """A polyMesh parsed back off disk, with the geometry derived from the points."""

    def __init__(self, case_dir: Path):
        pm = Path(case_dir) / "constant" / "polyMesh"
        self.dir = pm
        self.points = self._points(pm / "points")
        self.faces = self._faces(pm / "faces")
        self.owner = self._labels(pm / "owner")
        self.neighbour = self._labels(pm / "neighbour")
        self.boundary = self._boundary(pm / "boundary")
        self.zones = self._zones(pm / "cellZones")
        self.n_cells = int(max(self.owner.max(), self.neighbour.max()) + 1)
        self.n_internal = self.neighbour.size

    # -- files ------------------------------------------------------------------

    @staticmethod
    def _points(path: Path) -> np.ndarray:
        tok = _tokens(path)
        n = int(tok[0])
        vals = [t for t in tok[1:] if t not in "()"]
        arr = np.asarray(vals[:3 * n], dtype=float).reshape(n, 3)
        return arr

    @staticmethod
    def _faces(path: Path) -> list[list[int]]:
        tok = _tokens(path)
        n = int(tok[0])
        out: list[list[int]] = []
        i = 2                                    # past the count and the opening '('
        while len(out) < n:
            size = int(tok[i])
            assert tok[i + 1] == "("
            out.append([int(v) for v in tok[i + 2:i + 2 + size]])
            assert tok[i + 2 + size] == ")"
            i += size + 3
        return out

    @staticmethod
    def _labels(path: Path) -> np.ndarray:
        tok = _tokens(path)
        n = int(tok[0])
        vals = [t for t in tok[1:] if t not in "()"]
        return np.asarray(vals[:n], dtype=np.int64)

    @staticmethod
    def _boundary(path: Path) -> list[dict]:
        text = path.read_text()
        body = text[text.index("\n(", text.index("object      boundary")):]
        out, cur = [], None
        for raw in body.splitlines():
            line = raw.strip().rstrip(";")
            if not line or line in "()":
                continue
            if line == "{":
                continue
            if line == "}":
                out.append(cur)
                cur = None
                continue
            if cur is None:
                cur = {"name": line}
            else:
                key, _, val = line.partition(" ")
                cur[key] = val.strip()
        return [{"name": p["name"], "type": p["type"], "nFaces": int(p["nFaces"]),
                 "startFace": int(p["startFace"])} for p in out]

    @staticmethod
    def _zones(path: Path) -> dict[str, np.ndarray]:
        if not path.is_file():
            return {}
        tok = _tokens(path)
        n = int(tok[0])
        out: dict[str, np.ndarray] = {}
        i = 2
        for _ in range(n):
            name = tok[i]
            assert tok[i + 1] == "{", tok[i:i + 4]
            # type cellZone ; cellLabels List<label>
            j = tok.index("cellLabels", i)
            size = int(tok[j + 2])
            assert tok[j + 3] == "("
            out[name] = np.asarray(tok[j + 4:j + 4 + size], dtype=np.int64)
            i = j + 4 + size
            while tok[i] != "}":
                i += 1
            i += 1
        return out

    # -- geometry ---------------------------------------------------------------

    def face_geometry(self) -> tuple[np.ndarray, np.ndarray]:
        """(area vector, centroid) of every face, by fan triangulation."""
        areas = np.zeros((len(self.faces), 3))
        centres = np.zeros((len(self.faces), 3))
        for fi, nodes in enumerate(self.faces):
            p = self.points[nodes]
            c0 = p.mean(axis=0)
            a_tot = np.zeros(3)
            c_tot = np.zeros(3)
            for k in range(len(nodes)):
                a = np.cross(p[k] - c0, p[(k + 1) % len(nodes)] - c0) / 2.0
                a_tot += a
                c_tot += np.linalg.norm(a) * (c0 + p[k] + p[(k + 1) % len(nodes)]) / 3.0
            mag = np.linalg.norm(a_tot)
            areas[fi] = a_tot
            centres[fi] = c_tot / mag if mag > 0 else c0
        return areas, centres

    def cell_geometry(self):
        """(volume, centroid, closure residual, face count) of every cell, from its faces."""
        areas, centres = self.face_geometry()
        vol = np.zeros(self.n_cells)
        mom = np.zeros((self.n_cells, 3))
        close = np.zeros((self.n_cells, 3))
        nf = np.zeros(self.n_cells, dtype=int)
        for fi in range(len(self.faces)):
            a, c = areas[fi], centres[fi]
            o = int(self.owner[fi])
            v = float(np.dot(c, a)) / 3.0
            vol[o] += v
            mom[o] += v * c * 0.75
            close[o] += a
            nf[o] += 1
            if fi < self.n_internal:
                n = int(self.neighbour[fi])
                vol[n] -= v
                mom[n] -= v * c * 0.75
                close[n] -= a
                nf[n] += 1
        with np.errstate(invalid="ignore", divide="ignore"):
            centroid = mom / vol[:, None]
        return vol, centroid, close, nf

    def patch(self, name: str) -> dict:
        return next(p for p in self.boundary if p["name"] == name)


def write(chip: dict, name: str, **kwargs) -> tuple[fm.FoamMesh, PolyMesh]:
    case = TMP / name
    out = fm.write_polymesh(chip, case, **kwargs)
    return out, PolyMesh(case)


# --- fixtures --------------------------------------------------------------------------


def one_chamber_chip(**over) -> dict:
    """One box chamber, an inlet and an outlet, a channel at each end."""
    chip = {
        "id": "foam_fixture_box", "units": "mm", "footprint": {"x": 10, "y": 6},
        "layers": [{"id": "base", "material": "acrylic", "thickness_mm": 1.0},
                   {"id": "spacer", "material": "acrylic", "thickness_mm": 0.3},
                   {"id": "lid", "material": "acrylic", "thickness_mm": 1.0}],
        "chambers": [{"id": "cx", "type": "exchange", "center": [5.0, 3.0, 1.15],
                      "size": [2.0, 1.2, 0.3], "volume_ul": 0.72,
                      "block": "culture_chamber"}],
        "channels": [
            {"id": "ch_in", "from": "p_in", "to": "cx", "width_mm": 0.2,
             "depth_mm": 0.3, "block": "straight_channel"},
            {"id": "ch_out", "from": "cx", "to": "p_out", "width_mm": 0.2,
             "depth_mm": 0.3, "block": "straight_channel"},
        ],
        "ports": [
            {"id": "p_in", "type": "inlet", "position": [1.5, 3.0, 2.3]},
            {"id": "p_out", "type": "outlet", "position": [8.5, 3.0, 2.3]},
        ],
    }
    chip.update(over)
    return chip


def weir_chip() -> dict:
    chip = one_chamber_chip(id="foam_fixture_weir")
    chip["chambers"][0]["block"] = "trap_chamber"
    chip["chambers"][0]["retention"] = {"type": "weir", "opening_um": 25,
                                        "source": "fabricated",
                                        "crest_thickness_um": 200}
    return chip


def cylinder_chip() -> dict:
    chip = one_chamber_chip(id="foam_fixture_round")
    chip["chambers"][0]["shape"] = "cylinder"
    chip["chambers"][0]["size"] = [2.0, 2.0, 0.3]
    chip["chambers"][0]["volume_ul"] = round(math.pi * 1.0 ** 2 * 0.3, 4)
    return chip


def serpentine_chip() -> dict:
    chip = one_chamber_chip(id="foam_fixture_serpentine")
    chip["chambers"][0].update({
        "id": "mix", "block": "serpentine_channel", "type": "mixer",
        "center": [5.0, 3.0, 1.15], "size": [3.0, 2.0, 0.3], "volume_ul": 1.8,
        "mixer": {"kind": "serpentine", "channel_width_mm": 0.2, "turns": 2,
                  "length_mm": 8.0},
    })
    for ch in chip["channels"]:
        ch["from"] = "p_in" if ch["id"] == "ch_in" else "mix"
        ch["to"] = "mix" if ch["id"] == "ch_in" else "p_out"
    return chip


COARSE = dict(min_cells_across=3, min_cells_z=4, cell_budget=400_000)


# --- the tests ------------------------------------------------------------------------


class ZColumn(unittest.TestCase):
    def test_no_weir_is_uniform_and_at_least_eight_cells(self):
        levels = fm.z_levels(0.3, [])
        self.assertEqual(levels.size - 1, fm.MIN_CELLS_Z)
        dz = np.diff(levels)
        self.assertAlmostEqual(float(dz.max() - dz.min()), 0.0, places=12)
        self.assertAlmostEqual(float(levels[-1]), 0.3, places=12)

    def test_a_weir_gap_gets_three_cells_and_the_crest_top_is_exact(self):
        crest_top = 0.3 - 0.025
        levels = fm.z_levels(0.3, [crest_top])
        self.assertIn(round(crest_top, 12), [round(v, 12) for v in levels.tolist()])
        above = [v for v in levels.tolist() if v > crest_top + 1e-12]
        self.assertGreaterEqual(len(above), fm.MIN_CELLS_GAP)
        self.assertGreaterEqual(levels.size - 1, fm.MIN_CELLS_Z)

    def test_the_column_is_graded_within_the_expansion_limit(self):
        dz = np.diff(fm.z_levels(0.3, [0.275]))
        ratios = dz[1:] / dz[:-1]
        self.assertLessEqual(float(np.max(ratios)), fm.MAX_Z_EXPANSION + 1e-9)
        self.assertLessEqual(float(np.max(1.0 / ratios)), fm.MAX_Z_EXPANSION + 1e-9)
        self.assertGreater(float(dz.max() / dz.min()), 2.0)      # it really is graded

    def test_a_swept_gap_moves_the_interface_and_nothing_else_breaks(self):
        for delta in (-20.0, 0.0, +20.0):
            levels = fm.z_levels(0.3, [0.3 - (25.0 + delta) / 1000.0])
            top = [v for v in levels.tolist() if v > 0.3 - (25.0 + delta) / 1000 + 1e-12]
            self.assertGreaterEqual(len(top), fm.MIN_CELLS_GAP, delta)


class TopologyAndGeometry(unittest.TestCase):
    """Write a simple chip and check the FILES from first principles."""

    @classmethod
    def setUpClass(cls):
        cls.out, cls.mesh = write(one_chamber_chip(), "box", **COARSE)

    def test_the_file_counts_agree_with_each_other_and_with_the_metadata(self):
        m, meta = self.mesh, self.out.meta
        self.assertEqual(len(m.faces), meta["faces"])
        self.assertEqual(m.owner.size, meta["faces"])
        self.assertEqual(m.neighbour.size, meta["internal_faces"])
        self.assertEqual(m.points.shape[0], meta["points"])
        self.assertEqual(m.n_cells, meta["cells"])
        self.assertTrue(all(len(f) == 4 for f in m.faces))      # all hexahedra

    def test_internal_faces_are_upper_triangular_and_owner_is_always_lower(self):
        m = self.mesh
        own, nei = m.owner[:m.n_internal], m.neighbour
        self.assertTrue(bool((own < nei).all()), "owner must be the lower cell label")
        key = own * (m.n_cells + 1) + nei
        self.assertTrue(bool((np.diff(key) > 0).all()),
                        "internal faces must be in upper-triangular order")
        self.assertTrue(bool((m.owner[m.n_internal:] >= 0).all()))

    def test_every_cell_is_a_closed_positive_hexahedron(self):
        vol, _c, close, nf = self.mesh.cell_geometry()
        self.assertTrue(bool((nf == 6).all()), "a hex cell has exactly six faces")
        self.assertGreater(float(vol.min()), 0.0, "every cell volume must be positive")
        scale = float(vol.mean()) ** (2.0 / 3.0)
        self.assertLess(float(np.abs(close).max()) / scale, 1e-9,
                        "the face area vectors of a closed cell must sum to zero")

    def test_internal_normals_point_from_owner_to_neighbour(self):
        m = self.mesh
        areas, _ = m.face_geometry()
        _vol, centroid, _c, _nf = m.cell_geometry()
        d = centroid[m.neighbour] - centroid[m.owner[:m.n_internal]]
        dot = (areas[:m.n_internal] * d).sum(axis=1)
        self.assertGreater(float(dot.min()), 0.0)

    def test_boundary_normals_point_out_of_the_mesh(self):
        m = self.mesh
        areas, centres = m.face_geometry()
        _vol, centroid, _c, _nf = m.cell_geometry()
        sl = slice(m.n_internal, None)
        d = centres[sl] - centroid[m.owner[sl]]
        dot = (areas[sl] * d).sum(axis=1)
        self.assertGreater(float(dot.min()), 0.0)

    def test_the_patches_tile_the_boundary_exactly_once(self):
        m = self.mesh
        self.assertEqual(m.boundary[0]["startFace"], m.n_internal)
        at = m.n_internal
        for p in m.boundary:
            self.assertEqual(p["startFace"], at, p["name"])
            at += p["nFaces"]
        self.assertEqual(at, len(m.faces))
        self.assertEqual(m.boundary[-1]["name"], "walls")
        self.assertEqual(m.boundary[-1]["type"], "wall")

    def test_the_total_volume_is_the_sum_of_the_features_within_the_staircase_error(self):
        vol, _c, _cl, _nf = self.mesh.cell_geometry()
        total_m3 = float(vol.sum())
        total_ul = total_m3 / (MM_TO_M ** 3)              # 1 mm^3 = 1 uL
        meta = self.out.meta
        self.assertAlmostEqual(total_ul, meta["volume"]["meshed_total_ul"], places=6)
        self.assertAlmostEqual(total_ul, sum(f["meshed_volume_ul"]
                                             for f in meta["features"]), places=6)
        staircase = abs(meta["volume"]["staircase_error_vs_nominal"])
        self.assertLess(staircase, 0.05)
        self.assertAlmostEqual(total_ul, meta["volume"]["nominal_total_ul"],
                               delta=meta["volume"]["nominal_total_ul"] * (staircase + 1e-9))

    def test_cellzones_are_named_by_feature_and_hold_every_cell_once(self):
        m, meta = self.mesh, self.out.meta
        self.assertEqual(sorted(m.zones), sorted(f["feature_id"] for f in meta["features"]))
        every = np.concatenate([v for v in m.zones.values()])
        self.assertEqual(every.size, m.n_cells)
        self.assertEqual(np.unique(every).size, m.n_cells)
        for f in meta["features"]:
            self.assertEqual(m.zones[f["feature_id"]].size, f["cells"], f["feature_id"])

    def test_the_mesh_is_byte_identical_when_meshed_twice(self):
        again, _ = write(one_chamber_chip(), "box_again", **COARSE)
        for name in ("points", "faces", "owner", "neighbour", "boundary", "cellZones"):
            a = (TMP / "box" / "constant" / "polyMesh" / name).read_bytes()
            b = (TMP / "box_again" / "constant" / "polyMesh" / name).read_bytes()
            self.assertEqual(a, b, name)


class Ports(unittest.TestCase):
    def test_one_patch_per_port_on_the_lid_inside_the_bore(self):
        out, m = write(one_chamber_chip(), "ports", **COARSE)
        names = [p["name"] for p in m.boundary]
        self.assertEqual(names, ["p_in", "p_out", "walls"])
        areas, centres = m.face_geometry()
        depth = out.meta["z"]["depth_mm"] * MM_TO_M
        for row in out.meta["ports"]:
            p = m.patch(row["port"])
            sl = slice(p["startFace"], p["startFace"] + p["nFaces"])
            self.assertEqual(p["nFaces"], row["patch_faces"])
            # on the lid, normal straight up
            self.assertTrue(np.allclose(centres[sl][:, 2], depth))
            unit = areas[sl] / np.linalg.norm(areas[sl], axis=1)[:, None]
            self.assertTrue(np.allclose(unit, np.array([0.0, 0.0, 1.0])))
            # inside the bore circle, and the area is what the metadata claims
            pos = next(q["position"] for q in one_chamber_chip()["ports"]
                       if q["id"] == row["port"])
            r = np.hypot(centres[sl][:, 0] / MM_TO_M - pos[0],
                         centres[sl][:, 1] / MM_TO_M - pos[1])
            self.assertLessEqual(float(r.max()), row["bore_mm"] / 2 + 1e-9)
            area_mm2 = float(np.linalg.norm(areas[sl], axis=1).sum()) / (MM_TO_M ** 2)
            self.assertAlmostEqual(area_mm2, row["patch_area_mm2"], places=6)
            self.assertLess(area_mm2, row["bore_area_mm2"])

    def test_a_bore_with_too_little_fluid_under_it_is_reported_not_invented(self):
        out, _m = write(one_chamber_chip(), "ports_low", **COARSE)
        rows = {r["port"]: r for r in out.meta["ports"]}
        # a 0.2 mm channel reaching the hole centre covers 0.08 mm2 of a 0.8 mm bore,
        # against the 0.25 x area the port rule wants: exactly tests/test_port_overlap.py
        self.assertFalse(rows["p_in"]["enough_fluid"])
        self.assertAlmostEqual(rows["p_in"]["patch_area_mm2"], 0.08, delta=0.02)
        self.assertEqual(out.meta["ports_under_the_rule"], ["p_in", "p_out"])
        self.assertIn("port rule", rows["p_in"]["problem"])

    def test_a_bore_over_no_fluid_at_all_gets_no_patch_and_says_so(self):
        chip = one_chamber_chip(id="foam_fixture_orphan")
        chip["ports"].append({"id": "p_nowhere", "type": "inlet",
                              "position": [9.6, 0.4, 2.3]})
        out, m = write(chip, "ports_orphan", **COARSE)
        self.assertEqual(out.meta["ports_without_fluid"], ["p_nowhere"])
        self.assertNotIn("p_nowhere", [p["name"] for p in m.boundary])
        row = next(r for r in out.meta["ports"] if r["port"] == "p_nowhere")
        self.assertIn("NO fluid under it", row["problem"])

    def test_a_port_pad_under_the_bore_satisfies_the_rule(self):
        chip = one_chamber_chip(id="foam_fixture_pad")
        chip["chambers"].append({"id": "p_in_pad", "type": "inlet", "shape": "cylinder",
                                 "center": [1.5, 3.0, 1.15], "size": [1.2, 1.2, 0.3],
                                 "volume_ul": 0.3393, "block": "port"})
        out, _m = write(chip, "ports_pad", **COARSE)
        row = next(r for r in out.meta["ports"] if r["port"] == "p_in")
        self.assertTrue(row["enough_fluid"], row)
        self.assertGreater(row["patch_area_mm2"], row["need_mm2"])


class Weirs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out, cls.mesh = write(weir_chip(), "weir", **COARSE)

    def test_the_crest_is_solid_from_the_floor_to_depth_minus_the_gap(self):
        meta = self.out.meta
        depth = meta["z"]["depth_mm"]
        self.assertTrue(meta["weirs"])
        for w in meta["weirs"]:
            self.assertAlmostEqual(w["crest_top_mm"], depth - w["gap_um"] / 1000.0,
                                   places=9)
            self.assertAlmostEqual(w["gap_mm_meshed"], w["gap_um"] / 1000.0, places=9)
            self.assertGreaterEqual(w["gap_cells"], fm.MIN_CELLS_GAP)
            self.assertGreater(w["crest_plan_cells"], 0)
            self.assertGreaterEqual(w["crest_cells_across"], COARSE["min_cells_across"])

    def test_there_is_no_fluid_inside_the_crest_but_there_is_a_slit_above_it(self):
        m, meta = self.mesh, self.out.meta
        w = meta["weirs"][0]
        _vol, centroid, _cl, _nf = m.cell_geometry()
        z_mm = centroid[:, 2] / MM_TO_M
        crest_top = w["crest_top_mm"]
        x_mm = centroid[:, 0] / MM_TO_M
        # the x_min crest strip of a 2.0 x 1.2 mm chamber centred on (5, 3)
        strip = (x_mm > 4.0 + 1e-6) & (x_mm < 4.0 + w["crest_thickness_mm"] - 1e-6)
        y_mm = centroid[:, 1] / MM_TO_M
        strip &= (y_mm > 2.4 + 1e-6) & (y_mm < 3.6 - 1e-6)
        self.assertTrue(strip.any(), "the crest strip should carry the slit cells")
        self.assertGreater(float(z_mm[strip].min()), crest_top,
                           "no fluid cell may sit inside the solid crest")
        layers = np.unique(np.round(z_mm[strip], 9))
        self.assertGreaterEqual(layers.size, fm.MIN_CELLS_GAP)

    def test_the_crest_volume_leaves_the_chamber_and_the_books_balance(self):
        meta = self.out.meta
        cx = next(f for f in meta["features"] if f["feature_id"] == "cx")
        self.assertGreater(cx["weir_crest_solid_ul"], 0.0)
        self.assertAlmostEqual(cx["meshed_volume_ul"] + cx["weir_crest_solid_ul"],
                               cx["nominal_volume_ul"],
                               delta=abs(cx["staircase_error_vs_nominal"])
                               * cx["nominal_volume_ul"] + 1e-9)
        vol, _c, _cl, _nf = self.mesh.cell_geometry()
        self.assertAlmostEqual(float(vol.sum()) / (MM_TO_M ** 3),
                               meta["volume"]["meshed_total_ul"], places=6)

    def test_the_gap_can_be_swept_for_the_z_tolerance(self):
        gaps, vols = {}, {}
        for delta in (-20.0, 0.0, +20.0):
            out, _m = write(weir_chip(), f"weir{int(delta):+d}", gap_delta_um=delta,
                            **COARSE)
            w = out.meta["weirs"][0]
            gaps[delta] = w["gap_um"]
            vols[delta] = out.meta["volume"]["meshed_total_ul"]
            self.assertAlmostEqual(w["gap_mm_meshed"], (25.0 + delta) / 1000.0, places=9)
            self.assertGreaterEqual(w["gap_cells"], fm.MIN_CELLS_GAP)
            self.assertEqual(out.meta["weir_gap_override"]["gap_delta_um"], delta)
        self.assertEqual([gaps[-20.0], gaps[0.0], gaps[20.0]], [5.0, 25.0, 45.0])
        self.assertLess(vols[-20.0], vols[0.0])        # a lower crest holds more fluid
        self.assertLess(vols[0.0], vols[20.0])

    def test_an_outright_gap_override_wins(self):
        out, _m = write(weir_chip(), "weir_abs", gap_um=50.0, **COARSE)
        self.assertEqual(out.meta["weirs"][0]["gap_um"], 50.0)
        self.assertEqual(out.meta["weirs"][0]["gap_um_spec"], 25.0)

    def test_a_gap_swept_past_the_layer_is_an_error_not_a_silent_mesh(self):
        with self.assertRaises(fm.FoamMeshError):
            fm.write_polymesh(weir_chip(), None, gap_um=0.0, **COARSE)


class Shapes(unittest.TestCase):
    def test_a_cylinder_chamber_is_round_and_not_its_bounding_box(self):
        chip = cylinder_chip()
        out, _m = write(chip, "round", **COARSE)
        f = next(r for r in out.meta["features"] if r["feature_id"] == "cx")
        exact = math.pi * 1.0 ** 2 * 0.3
        self.assertAlmostEqual(f["meshed_volume_ul"], exact, delta=0.04 * exact)
        self.assertLess(f["meshed_volume_ul"], 2.0 * 2.0 * 0.3 * 0.85)   # not the box

    def test_a_serpentine_is_the_lane_and_the_fold_is_recorded_as_synthesised(self):
        chip = serpentine_chip()
        out, _m = write(chip, "serp", **COARSE)
        f = next(r for r in out.meta["features"] if r["feature_id"] == "mix")
        syn = next(s for s in out.meta["synthesised"] if s["feature_id"] == "mix")
        self.assertEqual(syn["lanes"], 3)                 # mixer.turns = 2 -> 3 lanes
        self.assertEqual(syn["turns_from_spec"], 2)
        self.assertAlmostEqual(syn["meshed_length_mm"], 8.0, places=6)
        self.assertEqual(syn["lane_width_mm"], 0.2)
        self.assertTrue(f["synthesised"])
        lane_volume = 8.0 * 0.2 * 0.3
        self.assertAlmostEqual(f["meshed_volume_ul"], lane_volume,
                               delta=0.12 * lane_volume)
        self.assertLess(f["meshed_volume_ul"], 0.4 * (3.0 * 2.0 * 0.3))   # not the box
        self.assertEqual(f["spec_volume_ul"], 1.8)                        # the box volume

    def test_the_synthesised_fold_is_deterministic(self):
        a = fm.serpentine_lane(fm.Footprint(5, 3, 3.0, 2.0), 0.2, 8.0, 2, 0.4)
        b = fm.serpentine_lane(fm.Footprint(5, 3, 3.0, 2.0), 0.2, 8.0, 2, 0.4)
        self.assertEqual(a, b)
        self.assertAlmostEqual(fm.polyline_length(a["points"]), 8.0, places=9)

    def test_a_lane_longer_than_its_box_is_clamped_and_reported(self):
        fold = fm.serpentine_lane(fm.Footprint(5, 3, 3.0, 2.0), 0.2, 500.0, 2, 0.4)
        self.assertTrue(fold["length_clamped_to_box"])
        self.assertLess(fold["meshed_length_mm"], 500.0)
        self.assertEqual(fold["requested_length_mm"], 500.0)

    def test_a_lane_is_joined_to_its_channels_and_not_left_an_island(self):
        # The spec trims a channel at the serpentine's BOUNDING BOX, but the lane is set
        # in from those walls: without carrying the channel through to the lane end the
        # mixer is a separate mesh region and checkMesh says so.
        out, _m = write(serpentine_chip(), "serp_conn", **COARSE)
        self.assertEqual(out.meta["connectivity"]["regions"], 1,
                         out.meta["connectivity"]["detail"])
        region = out.meta["connectivity"]["detail"][0]
        self.assertEqual(sorted(region["features"]), ["ch_in", "ch_out", "mix"])
        self.assertEqual(region["ports"], ["p_in", "p_out"])
        notes = [f["note"] for f in out.meta["features"] if f["kind"] == "channel"]
        self.assertTrue(any("lane end of mix" in n for n in notes), notes)

    def test_a_chip_whose_geometry_does_not_join_up_is_reported(self):
        chip = one_chamber_chip(id="foam_fixture_island")
        chip["chambers"].append({"id": "island", "type": "pool",
                                 "center": [5.0, 5.4, 1.15], "size": [1.0, 0.8, 0.3],
                                 "volume_ul": 0.24, "block": "polygon_chamber"})
        out, _m = write(chip, "island", **COARSE)
        self.assertEqual(out.meta["connectivity"]["regions"], 2)
        lonely = out.meta["connectivity"]["detail"][1]
        self.assertEqual(lonely["features"], ["island"])
        self.assertEqual(lonely["ports"], [])
        self.assertTrue(any("disconnected" in u["question"]
                            for u in out.meta["unresolved"]))

    def test_herringbone_grooves_are_meshed_as_a_lane_and_declared_unrepresented(self):
        chip = serpentine_chip()
        chip["id"] = "foam_fixture_hb"
        chip["chambers"][0]["block"] = "herringbone_mixer"
        out, _m = write(chip, "hb", **COARSE)
        hit = [u for u in out.meta["unrepresented"] if u["feature_id"] == "mix"]
        self.assertTrue(hit)
        self.assertIn("groove", hit[0]["what"])
        self.assertIn("second", hit[0]["why"].lower())


class Resolution(unittest.TestCase):
    def test_six_cells_across_the_narrowest_channel_by_default(self):
        out = fm.write_polymesh(one_chamber_chip(), None)
        xy = out.meta["xy"]
        self.assertEqual(xy["narrowest_feature_mm"], 0.2)
        self.assertGreaterEqual(xy["cells_across_narrowest"], fm.MIN_CELLS_ACROSS)
        self.assertGreaterEqual(xy["min_cells_across_measured"], fm.MIN_CELLS_ACROSS)
        self.assertGreaterEqual(out.meta["z"]["cells"], fm.MIN_CELLS_Z)

    def test_the_cell_budget_is_a_parameter_and_coarsening_is_declared(self):
        tight = fm.write_polymesh(one_chamber_chip(), None, cell_budget=20_000)
        self.assertLessEqual(tight.meta["cells"], 30_000)
        self.assertIn("relaxed", tight.meta["xy"]["budget_note"])
        self.assertLess(tight.meta["xy"]["cells_across_narrowest"], fm.MIN_CELLS_ACROSS)
        loose = fm.write_polymesh(one_chamber_chip(), None)
        self.assertGreater(loose.meta["cells"], tight.meta["cells"])
        self.assertIn("fitted", loose.meta["xy"]["budget_note"])

    def test_the_weir_crest_thickness_is_resolved_too(self):
        out = fm.write_polymesh(weir_chip(), None)
        w = out.meta["weirs"][0]
        self.assertGreaterEqual(w["crest_cells_across"], fm.MIN_CELLS_ACROSS)


class RepoChips(unittest.TestCase):
    """Mesh every chip in chip/ and report cells and volume error for each."""

    def test_every_repo_chip_meshes(self):
        rows, failed = [], {}
        for path in fm.repo_chips():
            try:
                out = fm.mesh_chip(path)          # metadata only, nothing written
            except FileNotFoundError:
                continue                          # a scratch spec removed mid-run
            except Exception as exc:
                failed[path.name] = f"{type(exc).__name__}: {exc}"
                continue
            meta = out.meta
            rows.append({
                "chip": meta["chip_id"], "cells": meta["cells"],
                "dxy_um": round(meta["xy"]["dxy_mm"] * 1000, 2),
                "nz": meta["z"]["cells"],
                "staircase_error": meta["volume"]["staircase_error_vs_nominal"],
                "error_vs_spec": meta["volume"]["error_vs_spec_total"],
                "ports_without_fluid": meta["ports_without_fluid"],
                "regions": meta["connectivity"]["regions"],
                "seconds": meta["build_seconds"],
            })
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps({"meshed": rows, "failed": failed}, indent=2) + "\n")
        self.assertEqual(failed, {}, f"see {REPORT}")
        self.assertTrue(rows)
        for r in rows:
            self.assertLessEqual(abs(r["staircase_error"]), 0.05, r)
            self.assertLessEqual(r["cells"], fm.DEFAULT_CELL_BUDGET, r)

    def test_module1_meshes_in_well_under_a_minute(self):
        case = TMP / "module1_v1"
        try:
            t = time.perf_counter()
            out = fm.mesh_chip(ROOT / "chip" / "module1_v1.json", case)
            elapsed = time.perf_counter() - t
            self.assertLess(elapsed, 30.0,
                            f"{elapsed:.1f}s to write {out.meta['cells']} cells")
            self.assertEqual(sorted(out.meta["cell_zones"]),
                             sorted(f["feature_id"] for f in out.meta["features"]))
            self.assertEqual(len(out.meta["weirs"]), 6)    # g_cx and a_cx, 3 walls each
        finally:
            shutil.rmtree(case, ignore_errors=True)


class CheckMesh(unittest.TestCase):
    """checkMesh must pass. Skipped cleanly without a runner or without OpenFOAM."""

    CHIPS = ("module1_v1", "module3_v1_translated")

    def test_checkmesh_passes(self):
        results = {}
        for chip_id in self.CHIPS:
            case = TMP / f"check_{chip_id}"
            try:
                fm.mesh_chip(ROOT / "chip" / f"{chip_id}.json", case)
                results[chip_id] = fm.checkmesh(case)
            finally:
                shutil.rmtree(case, ignore_errors=True)   # hundreds of MB per case
        skipped = [r["reason"] for r in results.values() if not r["ran"]]
        if skipped:
            self.skipTest(f"checkMesh not run: {skipped[0]}")
        for chip_id, r in results.items():
            self.assertTrue(r["ok"], f"{chip_id}: {r['failed_checks']}\n{r.get('tail')}")
            self.assertEqual(r["failed_checks"], [], chip_id)


if __name__ == "__main__":
    unittest.main()

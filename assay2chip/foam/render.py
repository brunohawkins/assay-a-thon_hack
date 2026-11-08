"""Per-chip 3D renders: PNG keyframes and an MP4, off-screen, with honest labels.

`assay2chip/foam/README.md` is authoritative; this is its `render.py` row. One chip, one
protocol step, one set of frames:

    velocity magnitude on a mid-depth slice     streamlines seeded at every feed port
    wall shear on the floor as a FRACTION of    mean age, where transport could solve it
      that chamber's own ceiling                particle/tracer paths on the solved field

TWO SOURCES, ONE RENDERER. A frame is drawn from whichever field exists:

    openfoam              `assay2chip/work/<run>/foam/<chip_id>/<step>/` (flow.py, F3)
    reduced-order (2.5D)  `assay2chip/cfd` depth-averaged Hele-Shaw, extruded for the
                          picture with the parabolic duct profile it assumes

The fallback is the point: a chip with no OpenFOAM case yet still gets a render, and the
frame SAYS `reduced-order (2.5D)` so nobody mistakes it for a 3D solve. Nothing here
invents physics: the 2.5D volume is the solved depth-averaged field times the z profile
that model already assumes, and the renderer never fabricates a vertical velocity.

HONEST LABELS, on every single frame (foam/README.md, "Honest labels"):

    SIMULATED            always
    DEMO SCALE           every chip on the 30 x 20 mm demo envelope
    PRINTED FOR THE DEMO every chip on the print queue in `tools/module_catalog.py`, or
                         that its pipeline file marks `built_for_demo`
    the source           `openfoam` or `reduced-order (2.5D)`
    z exaggerated xN     a 0.3 mm layer under a 30 mm footprint is invisible at true
                         scale, so z is stretched and the factor is written down
    assumptions          anything the field rests on but did not read from a contract

A PRINTED CHIP GETS NO CHANGE REQUESTS. Its findings are a record of what was made, so
`fix_hint` is stripped from them and the frame says so; improvements belong to the
full-spec design beside it, which is a different card and a different render.

AND THE SPEC IS DRAWN AS WRITTEN. The chip file owns the geometry and the mesh owns the
numbers, which are not the same thing: `assay2chip/cfd/mesh.py` will move a loose channel
end onto the chamber it names so the network solves at all, and drawing that would put a
connection on screen the chip does not have. Module 6's comb spines stop 1.9 mm short of
their culture fields - the teeth are in the prose, not in the path - so they are rendered
short, `spec_gaps()` says so in the caption, and every number on that chip is marked
provisional.

GEOMETRY IS NEVER RE-IMPLEMENTED HERE. The plan view comes from the chip spec through
`assay2chip.cfd.figures` (`footprint`, `channel_path`, `anchors`, `is_demo_scale`) and
from the cfd `Mesh`, which is the rasterised plan already. The render grid is a mask built
from those two, so there is no fourth geometry implementation to drift.

SHEAR IS A FRACTION OF THE RIGHT CEILING, per chamber, through
`assay2chip.cfd.figures.chamber_ceilings` - the same three correctly scoped ceilings the
rest of the repo uses. A capsule chamber has NO wall-shear ceiling (it is bounded by
escape pressure), so its floor is drawn grey and the frame says so rather than borrowing
a cell-culture limit it was never held to.

UNITS. mm, mm/s, dyn/cm^2, s and Pa on screen, with the unit in every colour-bar title.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # running the file directly
    sys.path.insert(0, str(ROOT))

from assay2chip.cfd import figures as cfd_figures  # noqa: E402  plan geometry + ceilings
from assay2chip.cfd.model import DYN_CM2_PER_PA, Field, Mesh  # noqa: E402

# --- the words that must appear ------------------------------------------------------

SIMULATED = "SIMULATED"
DEMO_SCALE = "DEMO SCALE"
SOURCE_FOAM = "openfoam"
SOURCE_REDUCED = "reduced-order (2.5D)"
OFF_CHIP = "OFF-CHIP"
BOUGHT_KIT = "BOUGHT KIT"
PRINTED_FOR_DEMO = "PRINTED FOR THE DEMO"

#: What a render may say about a chip that is already on the printer. A finding on a
#: printed chip is a measurement of what was made; it is NOT a change request, and this
#: renderer never captions one. Improvements belong to the full-spec design beside it
#: (`chip/<base>_target_v*.json`), which is a different card and a different render.
AS_BUILT_NOTE = ("printed for the demo: findings here are recorded AS BUILT, not proposed "
                 "changes - improvements belong to the full-spec design")

#: A 0.3 mm fluidic layer under a 30 mm footprint is one pixel tall at true scale, so z is
#: stretched for the picture. Every frame prints the factor.
Z_EXAGGERATION = 14.0

#: Grid points in the render volume, before `--cells-budget` overrides it. This is a
#: PICTURE grid, not a solve grid: the ">= 6 cells across the narrowest channel" rule in
#: foam/README.md governs the hex mesher (F2), not this.
DEFAULT_CELLS_BUDGET = 1_400_000
DEFAULT_NZ = 9
DEFAULT_RESOLUTION = (1920, 1080)
DEFAULT_FPS = 24

#: One per-chip MP4 should walk the protocol, not every one of 109 steps.
DEFAULT_MAX_STEPS = 6
#: Frames per protocol step in the per-chip MP4: enough for the tracers to cross the chip.
FRAMES_PER_STEP = 36

FLOOR_CMAP = "inferno"
SPEED_CMAP = "viridis"
AGE_CMAP = "magma"

BENCH_BG = "#101418"
INK = "white"
WARN = "#ffcc55"


class RenderError(RuntimeError):
    """Something the renderer cannot do: no PyVista, no ffmpeg, no field to draw."""


# --- optional dependencies -----------------------------------------------------------


def pyvista(require: bool = True):
    """PyVista, configured for off-screen rendering, or None.

    `requirements-foam.txt` carries it; the rest of the repo must import this module and
    its pure-data helpers (layout, labels, scale bars) without it.
    """
    try:
        import pyvista as pv
    except Exception as exc:  # pragma: no cover - exercised by the skip path
        if require:
            raise RenderError(
                "PyVista is not installed, so nothing can be rendered. "
                "./.venv/bin/python -m pip install -r requirements-foam.txt"
            ) from exc
        return None
    pv.OFF_SCREEN = True
    try:
        pv.global_theme.background = BENCH_BG
        pv.global_theme.font.color = INK
        pv.global_theme.allow_empty_mesh = True
    except Exception:
        pass
    return pv


def have_pyvista() -> bool:
    return pyvista(require=False) is not None


def ffmpeg() -> str | None:
    return shutil.which("ffmpeg") or (
        "/opt/homebrew/bin/ffmpeg" if Path("/opt/homebrew/bin/ffmpeg").exists() else None
    )


# --- small helpers -------------------------------------------------------------------


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(out) or math.isinf(out) else out


def load_json(path: Path | str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def even(n: int) -> int:
    """H.264 yuv420p needs even dimensions."""
    n = int(n)
    return n if n % 2 == 0 else n + 1


def resolution_of(spec: Any, default: tuple[int, int] = DEFAULT_RESOLUTION) -> tuple[int, int]:
    """`1080p`, `720p`, `1920x1080`, `(1920, 1080)` -> an even (w, h)."""
    named = {"480p": (854, 480), "720p": (1280, 720), "1080p": (1920, 1080),
             "1440p": (2560, 1440), "2160p": (3840, 2160), "4k": (3840, 2160)}
    if spec is None:
        w, h = default
    elif isinstance(spec, (tuple, list)) and len(spec) == 2:
        w, h = int(spec[0]), int(spec[1])
    else:
        text = str(spec).strip().lower()
        if text in named:
            w, h = named[text]
        elif "x" in text:
            a, b = text.split("x", 1)
            w, h = int(float(a)), int(float(b))
        else:
            raise RenderError(f"resolution {spec!r} is not WIDTHxHEIGHT or one of {sorted(named)}")
    return even(w), even(h)


def nice_bar_mm(span_mm: float) -> float:
    """A round scale-bar length under about a fifth of the span."""
    for candidate in (0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0):
        if candidate >= span_mm / 5.0:
            return candidate
    return 100.0


def fluid_depth_mm(chip: dict) -> float:
    """The depth of the ONE fluidic layer: chambers first, then the thinnest layer."""
    depths = [
        _f(c.get("size", [0, 0, 0])[2]) for c in (chip or {}).get("chambers") or []
        if len(c.get("size") or []) > 2 and _f(c.get("size")[2]) > 0
    ]
    depths += [_f(c.get("depth_mm")) for c in (chip or {}).get("channels") or []
               if _f(c.get("depth_mm")) > 0]
    if depths:
        return max(depths)
    layers = [(_f(l.get("thickness_mm")), l.get("id", "")) for l in (chip or {}).get("layers") or []]
    layers = [(t, i) for t, i in layers if t > 0]
    if layers:
        return min(layers)[0]
    return 0.3


def footprint_mm(chip: dict) -> tuple[float, float]:
    return cfd_figures.footprint(chip)


def printed_chips() -> dict[str, str]:
    """`chip/<id>.json` -> the pipeline slot it is being printed for. Read, never listed.

    The frozen print queue lives in `tools/module_catalog.py` (`PRINTED`), which is the
    one place in the repo that says which chips are on the printer, and it is explicit
    that it is there only to label them. It is loaded by path - `tools/` is not a package
    and has no `__init__.py` - and its import is side-effect free.

    A pipeline file may also say so per module (`built_for_demo` in
    `contracts/schemas/pipeline.schema.json`); `scene.py` prefers that when it is set,
    because the chain file is the thing a merge updates.
    """
    import importlib.util

    path = ROOT / "tools" / "module_catalog.py"
    if not path.is_file():
        return {}
    try:
        spec = importlib.util.spec_from_file_location("_module_catalog", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        table = dict(getattr(module, "PRINTED_BY_CHIP", {}) or {})
    except Exception:
        return {}
    return {str(k): str(v) for k, v in table.items()}


def is_printed(chip_path: Path | str | None, chip: dict | None = None) -> str | None:
    """The slot this chip is printed for, or None. Matches on path and on chip id."""
    table = printed_chips()
    if chip_path:
        rel = Path(chip_path)
        try:
            rel = rel.relative_to(ROOT)
        except ValueError:
            pass
        slot = table.get(rel.as_posix())
        if slot:
            return slot
    chip_id = str((chip or {}).get("id") or (Path(chip_path).stem if chip_path else ""))
    for key, slot in table.items():
        if Path(key).stem == chip_id:
            return slot
    return None


def narrowest_mm(chip: dict) -> float:
    widths = [_f(c.get("width_mm")) for c in (chip or {}).get("channels") or []]
    widths = [w for w in widths if w > 0]
    return min(widths) if widths else 0.2


def _box_distance(point: Sequence[float], chamber: dict) -> float:
    """Distance in plan from a point to a chamber's footprint, 0 inside it."""
    cx, cy = _f(chamber["center"][0]), _f(chamber["center"][1])
    sx, sy = _f(chamber["size"][0]), _f(chamber["size"][1])
    dx = max(abs(_f(point[0]) - cx) - sx / 2.0, 0.0)
    dy = max(abs(_f(point[1]) - cy) - sy / 2.0, 0.0)
    return math.hypot(dx, dy)


def spec_gaps(chip: dict) -> list[dict]:
    """Channels whose GEOMETRY does not reach the chamber their own spec names.

    A chip spec can say two different things at once: `from`/`to` claim a connection and
    the `note` describes the teeth that would make it, while the `path` stops short. That
    is exactly Module 6: each lane's inlet and outlet combs are `centipede_channel`
    blocks whose notes describe "13 side teeth 0.2 x 1.5 mm", but the path in the file is
    the SPINE only, ending 1.9 mm from the culture field it feeds. The teeth are prose.

    This renders what the spec says and says what is missing. It is deliberately NOT read
    off the mesh: `assay2chip/cfd/mesh.py` attaches a loose channel end to the nearest
    feature so the network still solves, which is the right call for a solver and the
    wrong one for a picture - it would quietly draw a connection the chip does not have.

    One finding per channel end, in the `Finding` JSON shape, class `dead_end`.
    """
    chambers = {str(c.get("id")): c for c in (chip or {}).get("chambers") or []
                if c.get("center") and c.get("size")}
    table = cfd_figures.anchors(chip)
    chip_id = str((chip or {}).get("id") or "")
    out: list[dict] = []
    for ch in (chip or {}).get("channels") or []:
        pts = cfd_figures.channel_path(chip, ch, table)
        if len(pts) < 2:
            continue
        width = _f(ch.get("width_mm"), 0.2) or 0.2
        tol = max(width, 0.1)
        for point, target in ((pts[0], ch.get("from")), (pts[-1], ch.get("to"))):
            chamber = chambers.get(str(target))
            if chamber is None:
                continue
            gap = _box_distance(point, chamber)
            if gap <= tol:
                continue
            out.append({
                "class": "dead_end", "severity": "high", "chip_id": chip_id,
                "feature_id": str(ch.get("id")), "step": None,
                "metric": "gap between the channel path and the chamber it names",
                "value": round(gap, 3), "limit": round(tol, 3), "units": "mm",
                "message": (
                    f"{ch.get('id')} ({ch.get('block') or 'channel'}) stops {gap:.2f} mm "
                    f"short of {target}, which its own spec says it connects to: the "
                    f"openings are described in the note but are not in the geometry, so "
                    f"the field is drawn with the gap the file has"),
                "source": f"chip/{chip_id}.json: channel path vs chamber footprint",
            })
    return out


# --- what a frame needs --------------------------------------------------------------


@dataclass
class PortMark:
    """One port, where it is and what the step is doing with it."""

    id: str
    x_mm: float
    y_mm: float
    kind: str = "port"          # inlet | outlet | vent | port, from the chip spec
    role: str = "idle"          # feed | vent | cap, from the scenario
    q_ul_min: float | None = None

    @property
    def is_feed(self) -> bool:
        return self.role == "feed"

    def label(self) -> str:
        if self.role == "feed":
            rate = f" {self.q_ul_min:g} uL/min" if self.q_ul_min else ""
            return f"{self.id}  (feed{rate})"
        if self.role == "vent":
            return f"{self.id}  (vent)"
        if self.role == "cap":
            return f"{self.id}  (capped)"
        return self.id


@dataclass
class Volume:
    """The 3D field a frame is drawn from, plus the mask that says where the fluid is.

    `grid` is a `pyvista.ImageData` over the chip's bounding box with point arrays:

        fluid         1.0 inside the fluid domain, 0.0 in the wall
        U_mm_s        velocity, mm/s, zero outside the fluid so a streamline stops there
        speed_mm_s    |U|
        shear_frac    wall shear / this chamber's own ceiling, NaN where there is none
        shear_dyn_cm2 the shear itself
        age_s         mean age, NaN when transport could not solve it
    """

    grid: Any
    spacing_mm: float
    nz: int
    depth_mm: float
    footprint_mm: tuple[float, float]
    has_age: bool = False
    #: V/Q for this step: one pass of the wetted volume, the 100% of the mean-age bar.
    age_scale_s: float | None = None
    has_shear_ceiling: bool = False
    notes: list[str] = dc_field(default_factory=list)

    @property
    def mid_z_mm(self) -> float:
        return self.depth_mm / 2.0


@dataclass
class Case:
    """One chip, one protocol step, ready to draw. The renderer reads nothing else."""

    chip: dict
    chip_id: str
    chip_path: str = ""
    step: Any = None
    label: str = ""
    operation: str = ""
    fluid: str = ""
    temp_c: float | None = None
    q_ul_min: float | None = None
    source: str = SOURCE_REDUCED
    volume: Volume | None = None
    ports: list[PortMark] = dc_field(default_factory=list)
    findings: list[dict] = dc_field(default_factory=list)
    assumptions: list[str] = dc_field(default_factory=list)
    notes: list[str] = dc_field(default_factory=list)
    tracks: list[np.ndarray] = dc_field(default_factory=list)
    track_source: str = ""
    mesh: Mesh | None = None
    field: Field | None = None
    #: The pipeline slot this chip is being printed for, or "" when it is twin-only.
    printed_slot: str = ""

    def __post_init__(self) -> None:
        if not self.printed_slot:
            self.printed_slot = is_printed(self.chip_path or None, self.chip) or ""
        if self.printed:
            # A printed chip's findings are a record of what was MADE. Strip the fix hints
            # so no caption downstream can turn one into a change request on a chip that
            # is already on the printer.
            self.findings = [{k: v for k, v in f.items() if k != "fix_hint"}
                             for f in self.findings]

    @property
    def printed(self) -> bool:
        return bool(self.printed_slot)

    @property
    def demo_scale(self) -> bool:
        return bool(cfd_figures.is_demo_scale(self.chip))

    @property
    def feeds(self) -> list[PortMark]:
        return [p for p in self.ports if p.is_feed]

    @property
    def reduced(self) -> bool:
        return self.source != SOURCE_FOAM

    def headline(self) -> str:
        """The one finding a caption leads with, worst first, or the honest absence.

        Severity decides, and a STRUCTURAL finding breaks the tie: a channel that does not
        reach the chamber it names, or a region nothing can prime, invalidates every
        number measured on that geometry, so it leads over a shear or pressure figure of
        the same severity rather than queueing behind it.
        """
        if not self.findings:
            return "no finding on this step"
        order = {"blocker": 0, "high": 1, "medium": 2, "low": 3}
        structural = {"dead_end": 0, "bubble": 1, "clog": 2}
        worst = sorted(self.findings,
                       key=lambda f: (order.get(str(f.get("severity")), 9),
                                      structural.get(str(f.get("class")), 5)))[0]
        msg = str(worst.get("message") or worst.get("metric") or worst.get("class") or "")
        return f"{str(worst.get('severity', '?')).upper()}: {msg}"[:160]

    def badges(self) -> list[str]:
        out = [SIMULATED]
        if self.demo_scale:
            out.append(DEMO_SCALE)
        if self.printed:
            out.append(PRINTED_FOR_DEMO)
        out.append(self.source)
        return out


# --- the plan view, rasterised (from the cfd Mesh and the chip spec) -----------------


def _segments(chip: dict, mesh: Mesh | None) -> list[tuple[tuple[float, float], tuple[float, float], float, str]]:
    """Every duct-like run of the plan as (p0, p1, width_mm, feature_id).

    Channels follow their own `path` through `cfd.figures.channel_path` - a straight line
    between the endpoints is the wrong length and the wrong shear (cfd/README.md). A
    channel-LIKE chamber (a serpentine stored as a bounding box) has no Hele-Shaw cells in
    the mesh, so its own duct edges supply the real lane.
    """
    out: list[tuple[tuple[float, float], tuple[float, float], float, str]] = []
    table = cfd_figures.anchors(chip)
    for ch in (chip or {}).get("channels") or []:
        pts = cfd_figures.channel_path(chip, ch, table)
        width = _f(ch.get("width_mm"), 0.2) or 0.2
        for a, b in zip(pts, pts[1:]):
            out.append((tuple(a), tuple(b), width, str(ch.get("id", ""))))
    if mesh is None:
        return out
    nodes = {n.id: n for n in mesh.nodes}
    for e in _shape_edges(mesh, chip):
        a, b = nodes[e.a], nodes[e.b]
        width = e.width_mm if e.width_mm > 0 else 0.2
        out.append(((a.x_mm, a.y_mm), (b.x_mm, b.y_mm), width, e.feature_id or e.id))
    return out


def duct_edges(mesh: Mesh, chip: dict | None = None) -> list:
    """The mesh edges that carry a NUMBER this renderer draws: every duct outside a
    chamber that is already rasterised into cells.

    Not every edge is one. A `slit` is a weir crest INSIDE a chamber whose cells are
    already rasterised, and it is as wide as the chamber it crosses - painting it as a
    band of its own width inflates that chamber into a blob the chip does not have. A
    `hs_face` is a face between two cells of the same chamber, and a `mouth` is the
    opening a channel already covers.

    THIS IS NOT THE SAME QUESTION AS "where is there fluid" - see `_shape_edges`.
    """
    nodes = {n.id: n for n in mesh.nodes}
    celled = {n.feature_id for n in mesh.nodes if n.kind == "cell" and n.feature_id}
    out = []
    for e in mesh.edges:
        if e.kind != "duct":
            continue
        if e.feature_id and e.feature_id in celled:
            continue  # the chamber is meshed as cells; its tiles already cover it
        if e.a not in nodes or e.b not in nodes:
            continue
        out.append(e)
    return out


def _shape_edges(mesh: Mesh, chip: dict | None) -> list:
    """The duct edges that may CREATE fluid in the picture, as opposed to colour it.

    The chip spec owns the geometry; the mesh owns the numbers. The mesher is allowed to
    move a loose channel end onto the chamber it names so the network solves at all - on
    Module 6 it redraws each comb spine as a diagonal running from the real spine start
    to the culture field's edge - and drawing that would put a connection on screen that
    the chip does not have, directly contradicting the caption next to it.

    So an edge whose `feature_id` is a CHANNEL in the spec never shapes the picture: that
    channel's own `path` does, through `cfd.figures.channel_path`. What is left is the
    channel-LIKE chambers (a serpentine stored as a bounding box), whose real lane only
    the mesher knows, and they are spec features in their own right.
    """
    if chip is None:
        return duct_edges(mesh)
    channel_ids = {str(c.get("id")) for c in (chip.get("channels") or [])}
    return [e for e in duct_edges(mesh) if e.feature_id not in channel_ids]


def _paint_segment(mask: np.ndarray, p0, p1, width_mm: float, spacing: float,
                   setter=None, grow: bool = True) -> None:
    """Grid points within `width_mm`/2 of the segment: mark them, and/or set values.

    `grow=False` colours only points that are ALREADY fluid. That is how a mesh edge
    supplies numbers without being allowed to invent geometry the chip spec does not have.
    """
    nx, ny = mask.shape
    half = max(width_mm / 2.0, spacing * 0.6)
    x0, y0 = float(p0[0]), float(p0[1])
    x1, y1 = float(p1[0]), float(p1[1])
    lo_i = max(0, int(math.floor((min(x0, x1) - half) / spacing)))
    hi_i = min(nx - 1, int(math.ceil((max(x0, x1) + half) / spacing)))
    lo_j = max(0, int(math.floor((min(y0, y1) - half) / spacing)))
    hi_j = min(ny - 1, int(math.ceil((max(y0, y1) + half) / spacing)))
    if lo_i > hi_i or lo_j > hi_j:
        return
    xs = np.arange(lo_i, hi_i + 1) * spacing
    ys = np.arange(lo_j, hi_j + 1) * spacing
    gx, gy = np.meshgrid(xs, ys, indexing="ij")
    dx, dy = x1 - x0, y1 - y0
    length2 = dx * dx + dy * dy
    if length2 <= 1e-18:
        t = np.zeros_like(gx)
    else:
        t = np.clip(((gx - x0) * dx + (gy - y0) * dy) / length2, 0.0, 1.0)
    d = np.hypot(gx - (x0 + t * dx), gy - (y0 + t * dy))
    hit = d <= half
    sub = (slice(lo_i, hi_i + 1), slice(lo_j, hi_j + 1))
    if grow:
        if not hit.any():
            return
        mask[sub] |= hit
    else:
        hit &= mask[sub]
        if not hit.any():
            return
    if setter is not None:
        setter(sub, hit, t)


def _flood(fluid: np.ndarray, arrays: Sequence[np.ndarray], cap: int = 400,
           how: str = "mean") -> int:
    """Fill unknown (NaN) fluid points from their known neighbours, THROUGH the fluid.

    Grid-constrained, so a value never jumps a wall into the chamber next door - which a
    Euclidean nearest-neighbour fill would happily do. Returns the number of passes.

    `how`:
      mean     average the known neighbours. Right for a continuous field (velocity, shear).
      nearest  take one neighbour's value whole. Right for a LIMIT, which is a property of
               the chamber and must not be averaged with the chamber next door: a shear
               ceiling halfway between 0.01 and 0.3 dyn/cm^2 is a number nothing was ever
               held to.
    """
    if not len(arrays):
        return 0
    rolls = ((1, 0), (-1, 0), (0, 1), (0, -1))
    for step in range(cap):
        known = fluid & np.isfinite(arrays[0])
        todo = fluid & ~known
        if not todo.any():
            return step
        count = np.zeros(fluid.shape, dtype=np.float64)
        sums = [np.zeros(fluid.shape, dtype=np.float64) for _ in arrays]
        taken = np.zeros(fluid.shape, dtype=bool)
        for di, dj in rolls:
            nb_known = np.roll(known, (di, dj), axis=(0, 1))
            if di:
                (nb_known[:di] if di > 0 else nb_known[di:])[...] = False
            if dj:
                (nb_known[:, :dj] if dj > 0 else nb_known[:, dj:])[...] = False
            if how == "nearest":
                first = nb_known & todo & ~taken
                for s, a in zip(sums, arrays):
                    nb = np.roll(np.nan_to_num(a), (di, dj), axis=(0, 1))
                    s[first] = nb[first]
                taken |= first
                count += first
                continue
            count += nb_known
            for s, a in zip(sums, arrays):
                nb = np.roll(np.nan_to_num(a), (di, dj), axis=(0, 1))
                s += np.where(nb_known, nb, 0.0)
        fill = todo & (count > 0)
        if not fill.any():
            for a in arrays:              # fluid islands no source can reach
                a[todo] = 0.0
            return step
        for s, a in zip(sums, arrays):
            a[fill] = s[fill] if how == "nearest" else s[fill] / count[fill]
    return cap


def _smooth(fluid: np.ndarray, arrays: Sequence[np.ndarray], passes: int = 2) -> None:
    """A light 4-neighbour average inside the fluid: smoother streamlines, same field."""
    rolls = ((1, 0), (-1, 0), (0, 1), (0, -1))
    for _ in range(max(0, passes)):
        for a in arrays:
            acc = np.zeros(a.shape)
            cnt = np.zeros(a.shape)
            for di, dj in rolls:
                nb = np.roll(a, (di, dj), axis=(0, 1))
                nb_in = np.roll(fluid, (di, dj), axis=(0, 1))
                if di:
                    (nb_in[:di] if di > 0 else nb_in[di:])[...] = False
                if dj:
                    (nb_in[:, :dj] if dj > 0 else nb_in[:, dj:])[...] = False
                acc += np.where(nb_in, nb, 0.0)
                cnt += nb_in
            avg = np.divide(acc, np.maximum(cnt, 1.0))
            a[fluid] = 0.5 * a[fluid] + 0.5 * avg[fluid]


def grid_spacing_for(chip: dict, mesh: Mesh | None, cells_budget: int = DEFAULT_CELLS_BUDGET,
                     nz: int = DEFAULT_NZ) -> float:
    """The picture grid: as fine as the mesh, no finer than the budget allows."""
    fx, fy = footprint_mm(chip)
    want = min(narrowest_mm(chip) / 3.0,
               _f((mesh.meta.get("grid_mm") if mesh else None), 0.1) or 0.1)
    want = max(want, 1e-3)
    budget = max(int(cells_budget), 20_000)
    # nx*ny*nz <= budget  with nx ~ fx/s, ny ~ fy/s
    floor_s = math.sqrt(fx * fy * nz / budget)
    return max(want, floor_s)


def chamber_ceiling_table(chip: dict, step: dict | None, mesh: Mesh | None) -> dict[str, dict]:
    """Ceilings per chamber, extended to the channels between them.

    `cfd.figures.chamber_ceilings` is the one place that decides what 100% of the shear
    scale means, and it decides per CHAMBER. A channel carrying cells or capsules between
    two chambers is governed by the stricter of the two it joins, and the reason travels
    with it so the frame can say which ceiling it used.
    """
    table = dict(cfd_figures.chamber_ceilings(chip, step))
    byid = {c.get("id"): c for c in (chip or {}).get("chambers") or []}
    for ch in (chip or {}).get("channels") or []:
        ends = [table.get(ch.get("from")), table.get(ch.get("to"))]
        ends = [e for e in ends if e and e.get("ceiling_dyn_cm2")]
        if not ends:
            continue
        strict = min(ends, key=lambda e: e["ceiling_dyn_cm2"])
        table[str(ch.get("id"))] = {
            **strict,
            "why": f"channel {ch.get('id')!r} joins {ch.get('from')} and {ch.get('to')}; the "
                   f"stricter of the two ceilings governs what passes through it "
                   f"({strict.get('shear_class')})",
        }
    for cid in byid:
        table.setdefault(str(cid), {"shear_class": "none", "ceiling_dyn_cm2": None,
                                    "why": "no chamber record", "source": ""})
    return table


def build_volume(chip: dict, mesh: Mesh, field: Field, *, step: dict | None = None,
                 age_s: dict[str, float] | None = None, age_scale_s: float | None = None,
                 cells_budget: int = DEFAULT_CELLS_BUDGET, nz: int = DEFAULT_NZ) -> Volume:
    """The reduced-order field, extruded into the picture volume it already assumes.

    The depth-averaged model IS a 3D field with one shape imposed in z: a parabolic
    rectangular-duct profile whose depth average is the solved mean. So

        u(x, y, z) = u_bar(x, y) x 1.5 x (1 - (2 z / h - 1)^2)

    whose mean over the depth is exactly `u_bar` - no new physics, just the profile the
    model was derived with. The vertical component stays zero, because Hele-Shaw has none,
    and the frame says `reduced-order (2.5D)` so the picture never over-claims.
    """
    pv = pyvista()
    fx, fy = footprint_mm(chip)
    depth = fluid_depth_mm(chip)
    spacing = grid_spacing_for(chip, mesh, cells_budget, nz)
    nx, ny = int(fx / spacing) + 1, int(fy / spacing) + 1
    notes: list[str] = []

    fluid = np.zeros((nx, ny), dtype=bool)
    ux = np.full((nx, ny), np.nan)
    uy = np.full((nx, ny), np.nan)
    shear = np.full((nx, ny), np.nan)
    age = np.full((nx, ny), np.nan)
    #: The ceiling in force at each point, 0.0 where NOTHING holds this chamber to a wall
    #: shear (a capsule chamber is bounded by escape pressure instead). Never averaged
    #: across a wall - see `_flood(how="nearest")`.
    ceil = np.full((nx, ny), np.nan)

    ceilings = chamber_ceiling_table(chip, step, mesh)
    grid_mm = _f(mesh.meta.get("grid_mm"), spacing) or spacing
    nodes = {n.id: n for n in mesh.nodes}

    # 1. SHAPE, from the chip spec: channel paths and any channel-like chamber's lane.
    #    A chamber's own cells overwrite these in pass 2 where the two meet.
    for p0, p1, width, feature in _segments(chip, mesh):
        _paint_segment(fluid, p0, p1, width, spacing)

    # 2. VALUES, from the mesh, into fluid the spec already put there (`grow=False`).
    #    The mesher may redirect a loose channel end onto the chamber it names; that is
    #    the right call for a solver and would be a lie in a picture.
    for e in duct_edges(mesh, chip):
        a, b = nodes[e.a], nodes[e.b]
        area_mm2 = max(e.area_mm2, 1e-9)
        q_ul_min = _f(field.q_m3_s.get(e.id)) * 60.0 * 1e9      # m^3/s -> uL/min
        speed = (q_ul_min / 60.0) / area_mm2                    # mm^3/s / mm^2 = mm/s
        dx, dy = b.x_mm - a.x_mm, b.y_mm - a.y_mm
        length = math.hypot(dx, dy) or 1.0
        vx, vy = speed * dx / length, speed * dy / length
        tau = _f(field.shear_dyn_cm2.get(e.id), float("nan"))
        ceiling = (ceilings.get(e.feature_id) or {}).get("ceiling_dyn_cm2")
        age_a = _f((age_s or {}).get(e.a), float("nan"))
        age_b = _f((age_s or {}).get(e.b), float("nan"))

        def setter(sub, hit, t, vx=vx, vy=vy, tau=tau, ceiling=ceiling, age_a=age_a, age_b=age_b):
            np.copyto(ux[sub], vx, where=hit)
            np.copyto(uy[sub], vy, where=hit)
            np.copyto(shear[sub], tau, where=hit)
            np.copyto(ceil[sub], float(ceiling or 0.0), where=hit)
            if math.isfinite(age_a) or math.isfinite(age_b):
                blend = np.where(np.isfinite(age_a) & np.isfinite(age_b),
                                 age_a + t * (age_b - age_a),
                                 age_b if math.isfinite(age_b) else age_a)
                np.copyto(age[sub], blend, where=hit)

        _paint_segment(fluid, (a.x_mm, a.y_mm), (b.x_mm, b.y_mm),
                       e.width_mm if e.width_mm > 0 else 0.2, spacing, setter, grow=False)

    # 3. the Hele-Shaw chambers: one tile per cell node, the mesher's own raster.
    half = grid_mm / 2.0
    for n in mesh.nodes:
        if n.kind != "cell":
            continue
        i0 = max(0, int(math.ceil((n.x_mm - half) / spacing)))
        i1 = min(nx - 1, int(math.floor((n.x_mm + half) / spacing)))
        j0 = max(0, int(math.ceil((n.y_mm - half) / spacing)))
        j1 = min(ny - 1, int(math.floor((n.y_mm + half) / spacing)))
        if i0 > i1:
            i0 = i1 = min(max(int(round(n.x_mm / spacing)), 0), nx - 1)
        if j0 > j1:
            j0 = j1 = min(max(int(round(n.y_mm / spacing)), 0), ny - 1)
        sub = (slice(i0, i1 + 1), slice(j0, j1 + 1))
        vec = field.u_mm_s.get(n.id) or (0.0, 0.0)
        fluid[sub] = True
        ux[sub] = _f(vec[0])
        uy[sub] = _f(vec[1])
        tau = _f(field.shear_dyn_cm2.get(n.id), float("nan"))
        shear[sub] = tau
        ceil[sub] = float((ceilings.get(n.feature_id) or {}).get("ceiling_dyn_cm2") or 0.0)
        if age_s is not None:
            a_val = _f(age_s.get(n.id), float("nan"))
            age[sub] = a_val if math.isfinite(a_val) else np.nan

    if not fluid.any():
        raise RenderError(f"{chip.get('id')}: the plan rasterised to nothing - no fluid to draw")

    passes = _flood(fluid, [ux, uy, shear])
    if age_s is not None:
        _flood(fluid, [age])
    _flood(fluid, [ceil], how="nearest")
    with np.errstate(divide="ignore", invalid="ignore"):
        frac = np.where(np.isfinite(ceil) & (ceil > 0), shear / np.where(ceil > 0, ceil, 1.0),
                        np.nan)
    _smooth(fluid, [ux, uy], passes=2)
    if passes >= 400:
        notes.append("the fill did not reach every fluid point; some cells are drawn flat")

    has_ceiling = bool(np.isfinite(frac[fluid]).any())
    if not has_ceiling:
        notes.append("no chamber on this chip has a wall-shear ceiling "
                     "(capsule chambers are bounded by escape pressure), so the floor is grey")
    no_ceiling_names = sorted({
        str(cid) for cid, rec in ceilings.items()
        if rec.get("shear_class") == "capsules"
    })
    if no_ceiling_names:
        notes.append("escape-pressure bounded, no shear ceiling: " + ", ".join(no_ceiling_names))

    # 4. extrude: the parabolic profile the depth-averaged model already assumes.
    dz = depth / max(nz - 1, 1)
    grid = pv.ImageData(dimensions=(nx, ny, nz), spacing=(spacing, spacing, dz),
                        origin=(0.0, 0.0, 0.0))
    zeta = np.linspace(-1.0, 1.0, nz)
    profile = 1.5 * (1.0 - zeta ** 2)
    profile[0] = profile[-1] = 0.0                      # no slip on floor and lid

    fl3 = np.repeat(fluid[:, :, None], nz, axis=2)
    ux3 = np.nan_to_num(ux)[:, :, None] * profile[None, None, :]
    uy3 = np.nan_to_num(uy)[:, :, None] * profile[None, None, :]
    ux3 = np.where(fl3, ux3, 0.0)
    uy3 = np.where(fl3, uy3, 0.0)
    vec3 = np.stack([ux3, uy3, np.zeros_like(ux3)], axis=-1)

    order = "F"
    grid["fluid"] = fl3.astype(np.float32).ravel(order=order)
    grid["U_mm_s"] = vec3.reshape(-1, 3, order=order).astype(np.float32)
    grid["speed_mm_s"] = np.hypot(ux3, uy3).ravel(order=order).astype(np.float32)
    shear3 = np.where(fl3, np.repeat(shear[:, :, None], nz, axis=2), np.nan)
    frac3 = np.where(fl3, np.repeat(frac[:, :, None], nz, axis=2), np.nan)
    age3 = np.where(fl3, np.repeat(age[:, :, None], nz, axis=2), np.nan)
    grid["shear_dyn_cm2"] = shear3.ravel(order=order).astype(np.float32)
    grid["shear_frac"] = frac3.ravel(order=order).astype(np.float32)
    grid["age_s"] = age3.ravel(order=order).astype(np.float32)

    return Volume(grid=grid, spacing_mm=spacing, nz=nz, depth_mm=depth,
                  footprint_mm=(fx, fy), has_age=bool(age_s), age_scale_s=age_scale_s,
                  has_shear_ceiling=has_ceiling, notes=notes)


# --- the reduced-order source (always available) -------------------------------------


def _step_records(protocol_path: Path | str, chip_path: Path | str, max_steps: int) -> list[Any]:
    """Representative solved steps: distinct (operation, feeds, rate), in protocol order."""
    from assay2chip.cfd import scenario as cfd_scenario

    run = cfd_scenario.build_protocol(protocol_path, chip_path, worst_cases=False)
    seen: set[tuple] = set()
    picked = []
    for rec in run.scenarios:
        if rec.kind != "step" or rec.q_in_ul_min <= 0 or not rec.mappable:
            continue
        key = (rec.operation, tuple(sorted(rec.feeds)), tuple(sorted(rec.vents)),
               round(rec.q_in_ul_min, 4))
        if key in seen:
            continue
        seen.add(key)
        picked.append(rec)
        if len(picked) >= max_steps:
            break
    return picked


def _ports_for(chip: dict, record: Any) -> list[PortMark]:
    marks = []
    states = getattr(record, "ports", {}) or {}
    for p in (chip or {}).get("ports") or []:
        pid = str(p.get("id"))
        pos = p.get("position") or [0, 0, 0]
        st = states.get(pid)
        role = getattr(st, "role", None) or (
            {"flow": "feed", "pressure": "vent", "closed": "cap"}.get(getattr(st, "type", ""), "idle")
        )
        marks.append(PortMark(id=pid, x_mm=_f(pos[0]), y_mm=_f(pos[1]),
                              kind=str(p.get("type", "port")), role=str(role),
                              q_ul_min=_f(getattr(st, "q_ul_min", None), 0.0) or None))
    return marks


def _field_findings(chip_id: str, mesh: Mesh, field: Field, volume: Volume | None,
                    step: Any, chip: dict | None = None) -> list[dict]:
    """What this field itself says, so a frame always has an honest headline.

    These are measurements, not a replacement for `analyse.py`: peak pressure, the worst
    shear as a fraction of its own ceiling, any edge where Re > 1 and the Stokes model is
    out of its validity (`unmodelled` in cfd/README.md), and any channel whose geometry
    does not reach the chamber its own spec names (`spec_gaps`).
    """
    out: list[dict] = []
    over = field.over_re(1.0)
    if over:
        worst = max(_f(field.re.get(e)) for e in over)
        out.append({
            "class": "unmodelled", "severity": "high", "chip_id": chip_id, "step": step,
            "feature_id": mesh.edge(over[0]).feature_id if over else "",
            "metric": "reynolds", "value": round(worst, 2), "limit": 1.0, "units": "-",
            "message": f"Re reaches {worst:.1f} on {len(over)} edge(s): the Stokes/Hele-Shaw "
                       f"model is out of its validity there, so this needs the 3D solve",
            "source": "assay2chip/cfd/README.md (unmodelled: Re > 1)",
        })
    if volume is not None and volume.has_shear_ceiling:
        frac = volume.grid["shear_frac"]
        frac = frac[np.isfinite(frac)]
        if frac.size:
            peak = float(np.max(frac))
            out.append({
                "class": "shear",
                "severity": "high" if peak > 1.0 else ("medium" if peak > 0.5 else "low"),
                "chip_id": chip_id, "step": step, "feature_id": "", "metric": "wall shear",
                "value": round(peak, 4), "limit": 1.0, "units": "fraction of ceiling",
                "message": (f"peak wall shear is "
                            f"{'under 1' if peak < 0.01 else format(peak * 100, '.0f')}% of "
                            f"the ceiling that governs the chamber it is in"),
                "source": "assay2chip/cfd/figures.py chamber_ceilings()",
            })
    out.extend(spec_gaps(chip) if chip else [])
    peak_pa = field.max_pressure_pa()
    if peak_pa:
        out.append({
            "class": "leak", "severity": "low", "chip_id": chip_id, "step": step,
            "feature_id": "", "metric": "peak pressure", "value": round(peak_pa / 1000.0, 3),
            "limit": None, "units": "kPa",
            "message": f"peak gauge pressure {peak_pa / 1000.0:.2f} kPa",
            "source": "assay2chip/cfd/solve.py",
        })
    return out


def reduced_case(chip_path: Path | str, protocol_path: Path | str | None = None, *,
                 step: Any = None, cells_budget: int = DEFAULT_CELLS_BUDGET,
                 nz: int = DEFAULT_NZ, with_age: bool = True,
                 max_steps: int = DEFAULT_MAX_STEPS,
                 findings: Sequence[dict] = ()) -> list[Case]:
    """One Case per representative step, from `assay2chip/cfd` - the honest fallback.

    This is item 3 of the brief: a chip with no OpenFOAM result still renders, from the
    depth-averaged field, labelled `reduced-order (2.5D)` on screen.
    """
    from assay2chip.cfd import mesh as cfd_mesh, solve as cfd_solve

    chip_path = Path(chip_path)
    chip = load_json(chip_path)
    chip_id = str(chip.get("id") or chip_path.stem)
    base = cfd_mesh.build_mesh(chip)

    records: list[Any] = []
    if protocol_path:
        try:
            records = _step_records(protocol_path, chip_path, max_steps)
        except Exception as exc:
            records = []
            fallback_note = f"scenario build failed ({type(exc).__name__}: {exc}); " \
                            f"falling back to a nominal 1 uL/min on the first inlet"
        else:
            fallback_note = ""
    else:
        fallback_note = "no protocol given, so a nominal 1 uL/min on the first inlet is drawn"

    if step is not None:
        want = str(step)
        records = [r for r in records if str(getattr(r, "step", "")) == want] or records[:1]

    cases: list[Case] = []
    if not records:
        cases.append(_nominal_case(chip, chip_id, chip_path, base, cells_budget, nz,
                                   fallback_note, findings))
        return cases

    for rec in records:
        sc = rec.scenario
        mu = sc.mu_pa_s or base.mu_pa_s
        mesh = base.at_viscosity(mu)
        try:
            fld = cfd_solve.solve_step(mesh, sc.for_mesh(mesh))
        except Exception as exc:
            continue
        age_s, age_scale = (_age_or_none(mesh, fld) if with_age else (None, None))
        step_dict = {"operation": rec.operation, "reagent": getattr(rec.fluid, "name", ""),
                     "note": rec.note, "target": ", ".join(rec.targets)}
        volume = build_volume(chip, mesh, fld, step=step_dict, age_s=age_s,
                              age_scale_s=age_scale, cells_budget=cells_budget, nz=nz)
        notes = list(volume.notes)
        if fallback_note:
            notes.append(fallback_note)
        gaps = spec_gaps(chip)
        if gaps:
            # Say it on the frame: the mesher bridged a gap the chip does not have, so
            # every number on this render is for a connection the spec has not drawn.
            notes.append(
                f"{len(gaps)} channel end(s) do not reach the chamber they name; the "
                f"mesher bridges them to solve at all, so these numbers are provisional "
                f"until the geometry is completed")
        assumptions = [u.get("message", "") or u.get("question", "")
                       for u in (sc.unresolved or [])]
        mine = _field_findings(chip_id, mesh, fld, volume, rec.step, chip)
        cases.append(Case(
            chip=chip, chip_id=chip_id, chip_path=chip_path.as_posix(), step=rec.step,
            label=_label_for(rec), operation=rec.operation,
            fluid=getattr(rec.fluid, "name", "") or "", temp_c=rec.temp_c,
            q_ul_min=rec.q_in_ul_min, source=SOURCE_REDUCED, volume=volume,
            ports=_ports_for(chip, rec),
            findings=[*_for_chip(findings, chip_id, rec.step), *mine],
            assumptions=[a for a in assumptions if a], notes=notes, mesh=mesh, field=fld,
        ))
    if not cases:
        cases.append(_nominal_case(chip, chip_id, chip_path, base, cells_budget, nz,
                                   "every protocol step failed to solve", findings))
    return cases


def _label_for(record: Any) -> str:
    bits = [f"step {getattr(record, 'step', '?')}", getattr(record, "operation", "") or ""]
    targets = getattr(record, "targets", None) or []
    if targets:
        bits.append("-> " + ", ".join(str(t) for t in targets))
    q = getattr(record, "q_in_ul_min", 0.0)
    if q:
        bits.append(f"{q:g} uL/min")
    return "  ·  ".join(b for b in bits if b)


def _for_chip(findings: Iterable[dict], chip_id: str, step: Any) -> list[dict]:
    out = []
    for f in findings or ():
        if str(f.get("chip_id") or "") not in (chip_id, ""):
            continue
        fstep = f.get("step")
        if fstep is None or str(fstep) == str(step):
            out.append(dict(f))
    return out


def _age_or_none(mesh: Mesh, fld: Field) -> tuple[dict[str, float] | None, float | None]:
    """(age per node, V/Q) - the theoretical mean age is the honest 100% of the colour bar.

    A capped, dead chamber's mean age runs to 10^12 s, so a linear scale fitted to the
    data paints the whole live chip black. V/Q is what the step's own volume and flow say
    one pass takes; anything older than a few of those is a dead zone, and the frame says
    that rather than rescaling until it looks fine.
    """
    try:
        from assay2chip.cfd import transport as cfd_transport
        age = cfd_transport.mean_age(mesh, fld, diffusivity_m2_s=0.0)
        return ({k: v for k, v in age.age_s.items() if math.isfinite(v)},
                age.theoretical_mean_age_s)
    except Exception:
        return None, None


def _nominal_case(chip: dict, chip_id: str, chip_path: Path, base: Mesh,
                  cells_budget: int, nz: int, note: str,
                  findings: Sequence[dict]) -> Case:
    """No usable protocol step: drive the first inlet at 1 uL/min and SAY so."""
    from assay2chip.cfd import solve as cfd_solve
    from assay2chip.cfd.model import PortState, Scenario

    ports = (chip or {}).get("ports") or []
    inlets = [p for p in ports if str(p.get("type")) == "inlet"] or ports[:1]
    outlets = [p for p in ports if str(p.get("type")) == "outlet"] or ports[-1:]
    states: dict[str, Any] = {}
    if inlets:
        states[str(inlets[0]["id"])] = PortState.flow(1.0)
    for p in outlets:
        states[str(p["id"])] = PortState.vent()
    sc = Scenario(step="nominal", ports=states, fluid="water (assumed)", temp_c=20.0,
                  mu_pa_s=base.mu_pa_s, chip_id=chip_id)
    fld = cfd_solve.solve_step(base, sc.for_mesh(base))
    nominal_age, nominal_scale = _age_or_none(base, fld)
    volume = build_volume(chip, base, fld, step=None, age_s=nominal_age,
                          age_scale_s=nominal_scale, cells_budget=cells_budget, nz=nz)
    marks = []
    for p in ports:
        pid = str(p.get("id"))
        pos = p.get("position") or [0, 0, 0]
        st = states.get(pid)
        role = "cap" if st is None else ("feed" if st.is_flow else "vent")
        marks.append(PortMark(pid, _f(pos[0]), _f(pos[1]), str(p.get("type", "port")), role,
                              st.q_ul_min if st is not None else None))
    return Case(
        chip=chip, chip_id=chip_id, chip_path=chip_path.as_posix(), step="nominal",
        label="nominal 1 uL/min (NOT a protocol step)", operation="nominal",
        fluid="water (assumed)", temp_c=20.0, q_ul_min=1.0, source=SOURCE_REDUCED,
        volume=volume, ports=marks,
        findings=[*_for_chip(findings, chip_id, None),
                  *_field_findings(chip_id, base, fld, volume, "nominal", chip)],
        assumptions=["flow and fluid are ASSUMED, not read from a protocol: " + note],
        notes=[note, *volume.notes], mesh=base, field=fld,
    )


# --- the OpenFOAM source (F3's output, read tolerantly) ------------------------------

#: `flow.py` stamps its summary with this. A JSON blob that carries it is a flow summary
#: wherever it was filed; one that does not is somebody else's file.
FOAM_SCHEMA_PREFIX = "foam-flow"
FOAM_SUMMARY_NAMES = ("summary.json", "flow.json", "foam.json", "post.json", "result.json")


# ONE case-directory convention, and `assay2chip/foam/case.py` owns it:
#
#     case.out_dir(run=..., work=...)       -> assay2chip/work/<run>/foam
#     case.case_dir(out, chip_id, step)     -> <out>/<chip_id>/step_<N>/
#
# with the mesh INSIDE the case at `constant/polyMesh`. A real job failed because the mesh
# went to `foam/<chip>/1/` and the case to `foam/foam/<chip>/step_1/`, so `case.out_dir`
# makes the doubled path unrepresentable rather than merely avoided. Nothing in render.py,
# scene.py or run.py builds either path any other way; these two are thin passes through
# to it, kept only so the rest of this package has one import to reach for.


def foam_out_dir(run_dir: Path | str | None = None) -> Path:
    """`<out>`: `assay2chip/work/<run>/foam`, through `case.out_dir`.

    A caller may hand over the run directory or the foam directory; both collapse onto
    the same place.
    """
    from assay2chip.foam import case as foam_case
    return foam_case.out_dir(work=run_dir) if run_dir is not None else foam_case.out_dir()


def case_dir(chip_id: str, step: Any = None, *, out: Path | str | None = None,
             run: str | None = None) -> Path:
    """THE case directory for one chip and one step, through `case.case_dir`.

    `step=None` means a whole-chip case and `case.py` names it `all`; a step id that is
    not an integer (`4b`, `s7`) is used as written.
    """
    from assay2chip.foam import case as foam_case
    if out is not None:
        base = foam_out_dir(out)
    else:
        base = foam_case.out_dir(run) if run else foam_case.out_dir()
    return foam_case.case_dir(base, chip_id, step)


def foam_run_dir(run_dir: Path | str) -> Path:
    """Deprecated spelling of `foam_out_dir`, kept so nothing breaks mid-merge."""
    return foam_out_dir(run_dir)


def foam_density(summary: dict | None = None) -> float:
    """The density that turns OpenFOAM's KINEMATIC output into Pa, from the contracts.

    The incompressible solvers divide through by density, so `p` is m^2/s^2 and
    `wallShearStress` is m^2/s^2 as well - NOT Pa. Multiplying by the wrong rho is a
    silent error of a factor of a thousand in a number this renderer colours against a
    contract ceiling, so it comes from the case's own `rho_kg_m3` when the summary states
    it, and otherwise from `assay2chip.foam.case`, which reads the contract. It is never
    a literal in this file.
    """
    stated = _f((summary or {}).get("rho_kg_m3"), 0.0)
    if stated > 0:
        return stated
    try:
        from assay2chip.foam import case as foam_case
        temp = (summary or {}).get("temp_c")
        fluid = foam_case.fluid_for(_f(temp, 37.0) if temp is not None else 37.0)
        return float(fluid.rho_kg_m3)
    except Exception:
        from assay2chip.cfd import solve as cfd_solve
        return float(cfd_solve.density()[0])


def _is_flow_summary(doc: Any) -> bool:
    if not isinstance(doc, dict):
        return False
    return (str(doc.get("schema", "")).startswith(FOAM_SCHEMA_PREFIX)
            or doc.get("source") == SOURCE_FOAM)


def _step_dirs(out: Path, chip_id: str, step: Any) -> list[Path]:
    """Where this chip/step's case could be, newest convention first.

    `case_dir()` is the one convention. The bare `<chip>/<N>/` form is the layout that
    caused the mesh and the case to land in two different folders; it is still READ so a
    half-finished run is not silently ignored, and never written.
    """
    if step is None:
        return []
    return [case_dir(chip_id, step, out=out), out / str(chip_id) / str(step)]


def foam_summary(run_dir: Path | str, chip_id: str, step: Any = None) -> dict | None:
    """flow.py's per chip/step summary JSON, wherever it filed it.

    `flow.run_step()` RETURNS the summary (schema `foam-flow/1`) rather than insisting on
    a filename, so this does not insist on one either: it looks in the case directory for
    this step, then the chip's directory, then the run's, and accepts any JSON that stamps
    itself a flow summary. A file that does not is left alone.
    """
    out = foam_out_dir(run_dir)
    folders = _step_dirs(out, chip_id, step) + [out / chip_id, out]
    candidates: list[Path] = []
    for folder in folders:
        candidates.extend(folder / name for name in FOAM_SUMMARY_NAMES)
        candidates.append(folder / f"{chip_id}.summary.json")
        candidates.append(folder / f"{chip_id}.flow.json")
    for folder in folders:                 # anything else that stamps itself one
        if folder.is_dir():
            candidates.extend(sorted(folder.glob("*.json")))
    for path in candidates:
        if not path.is_file():
            continue
        try:
            doc = load_json(path)
        except Exception:
            continue
        if _is_flow_summary(doc):
            doc.setdefault("_path", path.as_posix())
            return doc
    return None


def foam_vtk(run_dir: Path | str, chip_id: str, step: Any = None) -> Path | None:
    """The newest VTK dataset written for this chip/step, or None.

    `foamToVTK` leaves `case/VTK/...`; a case that has not been converted has no field
    this renderer can draw, and None is the honest answer.
    """
    out = foam_out_dir(run_dir)
    base = out / chip_id
    for folder in _step_dirs(out, chip_id, step):
        if folder.is_dir():
            base = folder
            break
    if not base.is_dir():
        return None
    hits: list[Path] = []
    for pattern in ("**/*.vtm", "**/*.vtu", "**/*.vtk", "**/*.vti"):
        hits.extend(base.glob(pattern))
    if not hits:
        return None
    return sorted(hits, key=lambda p: (p.stat().st_mtime, p.name))[-1]


def foam_case(run_dir: Path | str, chip_path: Path | str, step: Any = None, *,
              findings: Sequence[dict] = ()) -> Case | None:
    """A Case from an OpenFOAM result, or None when there is not one yet.

    The summary (`flow.py`, schema `foam-flow/1`) supplies the words - step, solver,
    fluid, flow, findings, assumptions - and the VTK dataset supplies the field. BOTH are
    needed: a frame labelled `openfoam` must actually be one, so a summary with no
    converted field returns None and the reduced-order fallback draws the chip instead.

    TODO(F3): `foamToVTK` is not part of `run_step()` yet, so there is no VTK to read even
    when a case has run. The moment `case/VTK/*.vtm` appears this path takes over with no
    change here - `tests/test_foam_render.py` pins that a summary alone is not enough.

    OpenFOAM's incompressible solvers are kinematic: `U` is m/s and `wallShearStress` is
    m^2/s^2, not Pa. `U` is converted to mm/s here; shear is taken from the summary's
    already-converted `shear` block rather than being multiplied by rho a second time.
    """
    chip_path = Path(chip_path)
    chip = load_json(chip_path)
    chip_id = str(chip.get("id") or chip_path.stem)
    summary = foam_summary(run_dir, chip_id, step)
    vtk_path = foam_vtk(run_dir, chip_id, step)
    if summary is None and vtk_path is None:
        return None
    pv = pyvista(require=False)
    if pv is None or vtk_path is None:
        return None
    try:
        data = pv.read(vtk_path)
    except Exception:
        return None
    if isinstance(data, pv.MultiBlock):
        data = data.combine()
    names = {str(k).lower(): str(k) for k in data.point_data.keys()}
    u_key = names.get("u_mm_s") or names.get("u")
    if u_key is None:
        return None
    vectors = np.asarray(data.point_data[u_key], dtype=float)
    if vectors.ndim != 2 or vectors.shape[1] != 3:
        return None
    if u_key.lower() == "u":                 # OpenFOAM writes m/s
        vectors = vectors * 1000.0
    data["U_mm_s"] = vectors.astype(np.float32)
    data["speed_mm_s"] = np.linalg.norm(vectors, axis=1).astype(np.float32)
    data["fluid"] = np.ones(data.n_points, dtype=np.float32)
    rho = foam_density(summary)
    for want, keys in (("shear_dyn_cm2", ("shear_dyn_cm2", "wallshearstress", "tau")),
                       ("age_s", ("age_s", "age", "meanage"))):
        key = next((names[k] for k in keys if k in names), None)
        if key is None:
            data[want] = np.full(data.n_points, np.nan, dtype=np.float32)
            continue
        raw = np.asarray(data.point_data[key], dtype=float)
        if raw.ndim == 2:
            raw = np.linalg.norm(raw, axis=1)
        if want == "shear_dyn_cm2" and key.lower() == "wallshearstress":
            raw = raw * rho * DYN_CM2_PER_PA        # kinematic m^2/s^2 -> Pa -> dyn/cm^2
        data[want] = raw.astype(np.float32)
    if "shear_frac" not in data.point_data:
        data["shear_frac"] = np.full(data.n_points, np.nan, dtype=np.float32)

    fx, fy = footprint_mm(chip)
    volume = Volume(grid=data, spacing_mm=_f((summary or {}).get("grid_mm"), 0.05) or 0.05,
                    nz=int(_f((summary or {}).get("nz"), DEFAULT_NZ) or DEFAULT_NZ),
                    depth_mm=fluid_depth_mm(chip), footprint_mm=(fx, fy),
                    has_age=bool(np.isfinite(np.asarray(data["age_s"])).any()),
                    has_shear_ceiling=bool(np.isfinite(np.asarray(data["shear_frac"])).any()),
                    notes=[f"read from {vtk_path.relative_to(ROOT).as_posix()}"])
    plan = (summary or {}).get("plan") or {}
    roles: dict[str, str] = {}
    for key, role in (("feeds", "feed"), ("vents", "vent"), ("capped", "cap")):
        for pid in (plan.get(key) or (summary or {}).get(key) or []):
            roles[str(pid)] = role
    marks = []
    for p in (chip or {}).get("ports") or []:
        pos = p.get("position") or [0, 0, 0]
        pid = str(p.get("id"))
        marks.append(PortMark(pid, _f(pos[0]), _f(pos[1]), str(p.get("type", "port")),
                              roles.get(pid, "idle")))
    return Case(
        chip=chip, chip_id=chip_id, chip_path=chip_path.as_posix(),
        step=(summary or {}).get("step", step), label=str((summary or {}).get("label", "") or
                                                          f"step {step}"),
        operation=str((summary or {}).get("operation", "")),
        fluid=str((summary or {}).get("fluid", "")),
        temp_c=(summary or {}).get("temp_c"),
        q_ul_min=((summary or {}).get("q_in_ul_min")
                  or ((summary or {}).get("plan") or {}).get("q_in_ul_min")),
        source=SOURCE_FOAM, volume=volume, ports=marks,
        findings=[*_for_chip((summary or {}).get("findings") or findings, chip_id, step)],
        assumptions=[str(a) for a in ((summary or {}).get("assumptions") or [])],
        notes=list(volume.notes),
    )


def cases_for_chip(chip_path: Path | str, protocol_path: Path | str | None = None, *,
                   run_dir: Path | str | None = None, step: Any = None,
                   cells_budget: int = DEFAULT_CELLS_BUDGET, nz: int = DEFAULT_NZ,
                   max_steps: int = DEFAULT_MAX_STEPS,
                   findings: Sequence[dict] = ()) -> list[Case]:
    """OpenFOAM where it exists, the reduced-order fallback where it does not."""
    out: list[Case] = []
    if run_dir is not None:
        case = foam_case(run_dir, chip_path, step, findings=findings)
        if case is not None:
            out.append(case)
    if out:
        return out
    cases = reduced_case(chip_path, protocol_path, step=step, cells_budget=cells_budget,
                         nz=nz, max_steps=max_steps, findings=findings)
    return cases[:max_steps]


# --- drawing -------------------------------------------------------------------------


def chip_body(chip: dict, origin: tuple[float, float, float] = (0.0, 0.0, 0.0)):
    """The chip as a semi-transparent slab: the fluidic layer, in place, see-through."""
    pv = pyvista()
    fx, fy = footprint_mm(chip)
    depth = fluid_depth_mm(chip)
    ox, oy, oz = origin
    return pv.Box(bounds=(ox, ox + fx, oy, oy + fy, oz - depth * 0.15, oz + depth * 1.15))


def _scale_bar(plotter, chip: dict, z_mm: float, origin=(0.0, 0.0, 0.0), colour=INK):
    """A real bar of a round number of mm, in the plane the footprint lives in."""
    pv = pyvista()
    fx, fy = footprint_mm(chip)
    length = nice_bar_mm(fx)
    ox, oy, oz = origin
    y = oy + fy * 1.06                       # above the chip, clear of the caption block
    x0 = ox + fx * 0.02
    bar = pv.Line((x0, y, z_mm), (x0 + length, y, z_mm))
    plotter.add_mesh(bar, color=colour, line_width=5)
    for x in (x0, x0 + length):
        plotter.add_mesh(pv.Line((x, y - fy * 0.015, z_mm), (x, y + fy * 0.015, z_mm)),
                         color=colour, line_width=4)
    plotter.add_point_labels(
        np.array([[x0 + length / 2.0, y + fy * 0.05, z_mm]]), [f"{length:g} mm (x, y)"],
        font_size=11, text_color=colour, shape=None, show_points=False,
        always_visible=True,
    )
    return length


PORT_COLOURS = {"feed": "#54d98c", "vent": "#ff7a7a", "cap": "#8a8f98", "idle": "#8fd6ff"}


def _port_labels(plotter, case: Case, z_mm: float, origin=(0.0, 0.0, 0.0),
                 font_size: int = 10):
    """Every port named, coloured by what the step does with it.

    The markers are screen-space points, not spheres: z is exaggerated for the picture and
    a sphere drawn in that space comes out an egg.
    """
    if not case.ports:
        return
    ox, oy, _ = origin
    pts = np.asarray([[ox + p.x_mm, oy + p.y_mm, z_mm] for p in case.ports], dtype=float)
    plotter.add_point_labels(pts, [p.label() for p in case.ports], font_size=font_size,
                             text_color=INK, shape_opacity=0.5, shape_color="#0b1016",
                             show_points=False, always_visible=True)
    for role in sorted({p.role for p in case.ports}):
        sel = np.asarray([[ox + p.x_mm, oy + p.y_mm, z_mm]
                          for p in case.ports if p.role == role], dtype=float)
        plotter.add_points(sel, color=PORT_COLOURS.get(role, "#8fd6ff"), point_size=13,
                           render_points_as_spheres=True)


def _chrome(plotter, case: Case, title: str, size: tuple[int, int], *,
            field_label: str = "", extra: Sequence[str] = ()) -> None:
    """Every word foam/README.md requires, on this frame."""
    w, h = size
    pad = max(14, int(w / 90))
    plotter.add_text(title, position=(pad, h - int(h / 26)), font_size=max(10, int(h / 78)),
                     color=INK, name="title")
    sub = case.label or ""
    if field_label:
        sub = f"{field_label}   |   {sub}" if sub else field_label
    if sub:
        plotter.add_text(_wrap(sub, 104), position=(pad, h - int(h / 9.2)),
                         font_size=max(8, int(h / 112)), color="#c9d4e0", name="subtitle")

    plotter.add_text("   ".join(case.badges()), position=(pad, h - int(h / 6.0)),
                     font_size=max(9, int(h / 86)), color=WARN, name="badges")

    left = [f"z exaggerated x{Z_EXAGGERATION:g} (the scale bar is x, y only)"]
    if case.volume is not None:
        left.append(f"render grid {case.volume.spacing_mm * 1000:.0f} um, "
                    f"{case.volume.nz} cells across the {case.volume.depth_mm * 1000:.0f} um depth")
    if case.fluid:
        left.append(f"fluid: {case.fluid}" + (f" at {case.temp_c:g} C" if case.temp_c else ""))
    if case.q_ul_min:
        left.append(f"flow in: {case.q_ul_min:g} uL/min")
    plotter.add_text("\n".join(left), position=(pad, pad), font_size=max(7, int(h / 135)),
                     color="#9fb0c0", name="left")

    lines = [f"finding: {_wrap(case.headline(), 86)}"]
    if case.printed:
        lines.append(_wrap(f"{case.printed_slot}: {AS_BUILT_NOTE}", 86))
    for note in list(extra)[:2] + list(case.notes)[:2]:
        if note:
            lines.append(_wrap(str(note), 86))
    for a in case.assumptions[:1]:
        lines.append("assumed: " + _wrap(str(a), 80))
    plotter.add_text("\n".join(lines[:6]), position=(int(w * 0.36), pad),
                     font_size=max(7, int(h / 142)), color="#d8e2ec", name="caption")


def _wrap(text: str, width: int) -> str:
    words, line, out = str(text).split(), "", []
    for word in words:
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    out.append(line)
    return "\n".join(out[:3])


def _camera(plotter, chip: dict, depth: float, origin=(0.0, 0.0, 0.0),
            zoom: float = 1.15) -> None:
    """A low three-quarter view, parallel projection, fitted to what is on screen."""
    fx, fy = footprint_mm(chip)
    ox, oy, _ = origin
    cx, cy = ox + fx / 2.0, oy + fy / 2.0
    plotter.camera_position = [
        (cx - fx * 0.18, cy - fy * 1.30, depth * Z_EXAGGERATION + max(fx, fy) * 0.80),
        (cx, cy, depth * Z_EXAGGERATION * 0.5),
        (0.0, 0.0, 1.0),
    ]
    plotter.reset_camera()
    plotter.camera.zoom(zoom)


def _fluid(volume: Volume):
    """Only the wetted cells: the walls are not part of the picture."""
    grid = volume.grid
    if "fluid" in grid.point_data:
        try:
            return grid.threshold(0.5, scalars="fluid")
        except Exception:
            pass
    return grid


def _layer(volume: Volume, where: str):
    """A horizontal sheet of the volume: `floor` (z = 0) or `mid` (z = depth/2).

    The floor is taken as the k = 0 layer rather than a slice at z = 0 exactly: a cutting
    plane laid on the boundary of an image grid returns nothing, which silently loses the
    wall-shear frame.
    """
    grid = volume.grid
    if where == "floor" and hasattr(grid, "extract_subset") and hasattr(grid, "dimensions"):
        nx, ny, _ = grid.dimensions
        sheet = grid.extract_subset((0, nx - 1, 0, ny - 1, 0, 0))
    else:
        z = 0.0 if where == "floor" else volume.mid_z_mm
        sheet = grid.slice(normal="z", origin=(0, 0, z))
    try:
        return sheet.threshold(0.5, scalars="fluid")
    except Exception:
        return sheet


def _streamlines(volume: Volume, case: Case, *, seeds_per_port: int = 14):
    """Streamlines seeded at EVERY feed port of this step."""
    pv = pyvista()
    feeds = case.feeds or [p for p in case.ports if p.kind == "inlet"]
    if not feeds:
        return None
    fx, fy = volume.footprint_mm
    radius = max(fx * 0.012, volume.spacing_mm * 2.0)
    blocks = []
    for p in feeds:
        disc = pv.Disc(center=(p.x_mm, p.y_mm, volume.mid_z_mm), inner=0.0, outer=radius,
                       normal=(0, 0, 1), r_res=2, c_res=max(seeds_per_port, 6))
        blocks.append(disc)
    seed = blocks[0] if len(blocks) == 1 else pv.merge(blocks)
    speeds = np.asarray(volume.grid["speed_mm_s"], dtype=float)
    typical = float(np.percentile(speeds[speeds > 0], 60)) if (speeds > 0).any() else 1.0
    span = math.hypot(fx, fy)
    try:
        return volume.grid.streamlines_from_source(
            seed, vectors="U_mm_s", integration_direction="forward",
            max_time=float(span / max(typical, 1e-6) * 3.0),
            max_steps=6000, initial_step_length=0.2, terminal_speed=typical * 1e-4,
        )
    except Exception:
        return None


def _tracers(lines, phase: float, count: int = 240):
    """Dye tracers riding the streamlines: `phase` in [0, 1) walks them downstream."""
    pv = pyvista()
    if lines is None or lines.n_points == 0:
        return None
    pts = []
    try:
        strips = lines.lines
    except Exception:
        strips = None
    polys = []
    if strips is not None and len(strips):
        i = 0
        arr = np.asarray(strips)
        while i < len(arr):
            n = int(arr[i])
            polys.append(arr[i + 1:i + 1 + n])
            i += 1 + n
    if not polys:
        polys = [np.arange(lines.n_points)]
    stride = max(1, len(polys) // max(count // 3, 1))
    for ids in polys[::stride]:
        if len(ids) < 2:
            continue
        xyz = np.asarray(lines.points)[ids]
        d = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(xyz, axis=0), axis=1))]
        if d[-1] <= 0:
            continue
        for k in range(3):
            t = ((phase + k / 3.0) % 1.0) * d[-1]
            pts.append(xyz[int(np.searchsorted(d, t))])
    if not pts:
        return None
    return pv.PolyData(np.asarray(pts, dtype=float))


def _front(volume: Volume, lines, fraction: float):
    """A washout/dye front: everything the inlet fluid has reached by `fraction`.

    Drawn from the mean-age field where transport solved one (age below a moving
    threshold IS the swept volume), otherwise from distance along the streamlines.
    """
    grid = volume.grid
    age = np.asarray(grid["age_s"], dtype=float) if "age_s" in grid.point_data else None
    if age is None or not np.isfinite(age).any():
        return None
    good = age[np.isfinite(age)]
    hi = float(np.percentile(good, 97))
    cut = max(hi * max(fraction, 1e-3), 1e-9)
    tmp = grid.copy()
    tmp["front"] = np.where(np.isfinite(age) & (age <= cut), 1.0, 0.0).astype(np.float32)
    try:
        plane = tmp.slice(normal="z", origin=(0, 0, volume.mid_z_mm))
        return plane.threshold(0.5, scalars="front")
    except Exception:
        return None


def render_frame(case: Case, kind: str, size: tuple[int, int], out_path: Path | str, *,
                 phase: float = 0.0, lines=None, show_body: bool = True) -> Path:
    """One PNG. `kind` picks the field: speed | shear | age | streamlines | tracers."""
    pv = pyvista()
    volume = case.volume
    if volume is None:
        raise RenderError(f"{case.chip_id}: no volume to render")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    plotter = pv.Plotter(off_screen=True, window_size=list(size))
    plotter.enable_parallel_projection()          # so the scale bar means something
    plotter.set_background(BENCH_BG)
    if show_body:
        plotter.add_mesh(chip_body(case.chip), color="#7f97ad", opacity=0.09,
                         show_edges=True, edge_color="#5a7a95", line_width=1)

    field_label = ""
    extra: list[str] = []
    fluid = _fluid(volume)

    if kind == "speed":
        mid = _layer(volume, "mid")
        clim, note = _clim(volume.grid, "speed_mm_s", 99.5)
        plotter.add_mesh(mid, cmap=SPEED_CMAP, nan_opacity=0.0, clim=clim,
                         scalar_bar_args=_bar("|u| [mm/s]", size),
                         **_paint(mid, "speed_mm_s"))
        field_label = f"velocity magnitude, mid-depth slice (z = {volume.mid_z_mm * 1000:.0f} um)"
        if note:
            extra.append(note)
    elif kind == "shear":
        floor = _layer(volume, "floor")
        plotter.add_mesh(floor, cmap=FLOOR_CMAP, clim=(0.0, 1.0),
                         nan_color="#4b5058", nan_opacity=1.0, above_color="#ff2d55",
                         scalar_bar_args=_bar("wall shear / this chamber's ceiling [-]", size),
                         **_paint(floor, "shear_frac", "#4b5058"))
        field_label = "wall shear on the floor, as a fraction of the ceiling that governs " \
                      "each chamber"
        extra.append("grey = no wall-shear ceiling applies (capsules: bounded by escape "
                     "pressure); red = over the ceiling")
    elif kind == "age":
        if not volume.has_age:
            raise RenderError("no mean-age field")
        scale = volume.age_scale_s
        if scale and scale > 0:
            clim = (0.0, scale * 3.0)
            note = (f"white = older than 3 x V/Q ({scale:.0f} s per pass of the wetted "
                    f"volume at this step's flow): a dead zone, not a colour-scale artefact")
        else:
            clim, note = _clim(volume.grid, "age_s", 99.0)
        plotter.add_mesh(fluid, cmap=AGE_CMAP, nan_opacity=0.0, clim=clim,
                         above_color="#ffffff", opacity=0.75,
                         scalar_bar_args=_bar("mean age [s]", size),
                         **_paint(fluid, "age_s"))
        field_label = "mean age (residence time): the bright corners are the dead zones"
        if note:
            extra.append(note)
    elif kind in ("streamlines", "tracers"):
        mid = _layer(volume, "mid")
        plotter.add_mesh(mid, cmap=SPEED_CMAP, opacity=0.35, nan_opacity=0.0,
                         scalar_bar_args=_bar("|u| [mm/s]", size),
                         **_paint(mid, "speed_mm_s"))
        if lines is None:
            lines = _streamlines(volume, case)
        if lines is not None and lines.n_points:
            fx, _ = volume.footprint_mm
            tubes = lines.tube(radius=max(fx * 0.0012, volume.spacing_mm * 0.35))
            plotter.add_mesh(tubes, cmap=SPEED_CMAP, show_scalar_bar=False,
                             **_paint(tubes, "speed_mm_s", "#9fd4ff"))
        else:
            extra.append("no streamline could be seeded: every feed port is capped in this step")
        field_label = "streamlines seeded at every feed port"
        if kind == "tracers":
            tr = _tracers(lines, phase)
            if tr is not None:
                plotter.add_mesh(tr.glyph(scale=False, geom=pv.Sphere(
                    radius=max(volume.footprint_mm[0] * 0.004, volume.spacing_mm))),
                    color="#ffe08a", smooth_shading=True)
            front = _front(volume, lines, 0.15 + 0.85 * phase)
            if front is not None and front.n_points:
                plotter.add_mesh(front, color="#39b6ff", opacity=0.45, show_scalar_bar=False)
            field_label = "dye front and tracers on the solved field, seeded at the feed ports"
            if case.track_source:
                extra.append(f"particle tracks: {case.track_source}")
            else:
                extra.append("tracers are advected on the solved field, not Lagrangian "
                             "parcels: no parcel track file for this chip yet")
    else:
        raise RenderError(f"unknown frame kind {kind!r}")

    for track in case.tracks[:400]:
        plotter.add_mesh(pv.lines_from_points(np.asarray(track, dtype=float)),
                         color="#ff9ecb", line_width=2, show_scalar_bar=False)

    _port_labels(plotter, case, volume.depth_mm * 1.4)
    _scale_bar(plotter, case.chip, 0.0)
    _chrome(plotter, case, f"{case.chip_id}", size, field_label=field_label, extra=extra)
    plotter.set_scale(zscale=Z_EXAGGERATION)
    _camera(plotter, case.chip, volume.depth_mm, zoom=1.35)
    plotter.screenshot(str(out_path))
    plotter.close()
    return out_path


def _clim(grid, name: str, hi_pct: float = 99.0) -> tuple[tuple[float, float] | None, str]:
    """A colour range a single near-stagnant cell cannot blow out, and the caveat for it.

    A dead corner's mean age runs to 10^12 s, which on a linear scale paints every real
    number black. The scale is clipped and the frame says it is clipped - the outlier is a
    finding (`trap`), not a reason to hide the field.
    """
    if name not in grid.point_data:
        return None, ""
    vals = np.asarray(grid.point_data[name], dtype=float)
    vals = vals[np.isfinite(vals)]
    vals = vals[vals > 0]
    if vals.size == 0:
        return None, ""
    hi = float(np.percentile(vals, hi_pct))
    top = float(vals.max())
    if hi <= 0:
        return None, ""
    note = ""
    if top > hi * 1.5:
        over = int((vals > hi).sum())
        note = (f"colour scale clipped at the {hi_pct:g}th percentile ({hi:.3g}); "
                f"{over} cell(s) run to {top:.3g} and are a trap finding, not a colour")
    return (0.0, hi), note


def _paint(mesh, name: str, fallback: str = "#8fa6bb") -> dict:
    """`add_mesh` kwargs that colour by `name` if the mesh really carries it.

    A filter can return a dataset that has points but has dropped the point data: a
    streamline tracer that seeded nothing useful, or a threshold that kept no cells. VTK
    then raises `Data array (speed_mm_s) not present`, which killed a twenty-minute run
    on one frame of one step. Colouring flat is the right answer - the field is not there
    to show - and the caller says so on the frame.
    """
    try:
        has = mesh is not None and name in mesh.point_data
    except Exception:
        has = False
    return {"scalars": name} if has else {"color": fallback}


def _bar(title: str, size: tuple[int, int]) -> dict:
    w, h = size
    return {
        "title": f"{title}\n", "vertical": True, "position_x": 0.905, "position_y": 0.30,
        "width": 0.028, "height": 0.46, "title_font_size": max(11, int(h / 72)),
        "label_font_size": max(9, int(h / 95)), "color": INK, "n_labels": 5,
        "fmt": "%.3g",
    }


# --- video ---------------------------------------------------------------------------


def encode_mp4(frames: Sequence[Path | str], out_path: Path | str, fps: int = DEFAULT_FPS) -> Path:
    """H.264 / yuv420p, from a list of PNGs, through ffmpeg."""
    exe = ffmpeg()
    if exe is None:
        raise RenderError("ffmpeg is not on PATH, so no MP4 can be written "
                          "(brew install ffmpeg)")
    frames = [Path(f) for f in frames]
    if not frames:
        raise RenderError("no frames to encode")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        for i, src in enumerate(frames):
            os.link(src, Path(tmp) / f"f{i:05d}.png") if src.exists() else None
        cmd = [exe, "-y", "-loglevel", "error", "-framerate", str(int(fps)),
               "-i", str(Path(tmp) / "f%05d.png"),
               "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
               "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
               "-movflags", "+faststart", str(out_path)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not out_path.exists():
        raise RenderError(f"ffmpeg failed: {proc.stderr.strip()[:400]}")
    return out_path


def video_duration_s(path: Path | str) -> float | None:
    exe = shutil.which("ffprobe")
    if exe is None:
        return None
    proc = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                           "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return round(float(proc.stdout.strip()), 2)
    except ValueError:
        return None


# --- a minimal binary glTF writer ----------------------------------------------------
#
# VTK's own glTF exporter writes JSON with a texture whose `uri` is empty, which no
# WebGL loader will accept, and never a real GLB container. The scene we need is simple -
# coloured triangles and tubes - so it is written directly: POSITION, COLOR_0 and
# indices, one buffer, one BIN chunk. three.js GLTFLoader reads it as-is.

GLB_MAGIC = 0x46546C67
GLTF_JSON_CHUNK = 0x4E4F534A
GLTF_BIN_CHUNK = 0x004E4942


@dataclass
class GlbPart:
    """One drawable in the GLB: triangles, optional per-vertex colour, one material."""

    name: str
    points: np.ndarray                  # (n, 3) float32, metres
    triangles: np.ndarray               # (m, 3) uint32
    colors: np.ndarray | None = None    # (n, 4) uint8
    base_color: tuple[float, float, float, float] = (0.8, 0.85, 0.9, 1.0)
    opacity: float = 1.0


def _pad4(buf: bytearray, fill: bytes = b"\x00") -> None:
    while len(buf) % 4:
        buf += fill


def write_glb(parts: Sequence[GlbPart], path: Path | str, *,
              extras: dict | None = None) -> Path:
    """A real binary .glb: header, JSON chunk, BIN chunk. No textures, no data URIs."""
    import struct

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bin_buf = bytearray()
    accessors: list[dict] = []
    views: list[dict] = []
    meshes: list[dict] = []
    nodes: list[dict] = []
    materials: list[dict] = []

    def add_view(data: bytes, target: int | None) -> int:
        _pad4(bin_buf)
        offset = len(bin_buf)
        bin_buf.extend(data)
        view = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target is not None:
            view["target"] = target
        views.append(view)
        return len(views) - 1

    for part in parts:
        pts = np.ascontiguousarray(np.asarray(part.points, dtype=np.float32))
        tris = np.ascontiguousarray(np.asarray(part.triangles, dtype=np.uint32).reshape(-1))
        if pts.size == 0 or tris.size == 0:
            continue
        pos_view = add_view(pts.tobytes(), 34962)
        accessors.append({"bufferView": pos_view, "componentType": 5126, "count": int(len(pts)),
                          "type": "VEC3",
                          "min": [float(v) for v in pts.min(axis=0)],
                          "max": [float(v) for v in pts.max(axis=0)]})
        pos_acc = len(accessors) - 1
        attributes = {"POSITION": pos_acc}
        if part.colors is not None:
            cols = np.ascontiguousarray(np.asarray(part.colors, dtype=np.uint8))
            col_view = add_view(cols.tobytes(), 34962)
            accessors.append({"bufferView": col_view, "componentType": 5121, "normalized": True,
                              "count": int(len(cols)), "type": "VEC4"})
            attributes["COLOR_0"] = len(accessors) - 1
        idx_view = add_view(tris.tobytes(), 34963)
        accessors.append({"bufferView": idx_view, "componentType": 5125,
                          "count": int(tris.size), "type": "SCALAR"})
        idx_acc = len(accessors) - 1
        r, g, b, a = part.base_color
        materials.append({
            "name": f"{part.name}_mat",
            "pbrMetallicRoughness": {"baseColorFactor": [r, g, b, a * part.opacity],
                                     "metallicFactor": 0.0, "roughnessFactor": 0.75},
            "doubleSided": True,
            **({"alphaMode": "BLEND"} if (a * part.opacity) < 0.999 else {}),
        })
        meshes.append({"name": part.name,
                       "primitives": [{"attributes": attributes, "indices": idx_acc,
                                       "mode": 4, "material": len(materials) - 1}]})
        nodes.append({"name": part.name, "mesh": len(meshes) - 1})

    if not meshes:
        raise RenderError("write_glb: nothing to write")
    _pad4(bin_buf)
    gltf = {
        "asset": {"version": "2.0", "generator": "assay2chip/foam/render.py"},
        "scene": 0,
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes, "meshes": meshes, "materials": materials,
        "accessors": accessors, "bufferViews": views,
        "buffers": [{"byteLength": len(bin_buf)}],
    }
    if extras:
        gltf["extras"] = extras
    json_bytes = bytearray(json.dumps(gltf, separators=(",", ":")).encode("utf-8"))
    _pad4(json_bytes, b" ")
    total = 12 + 8 + len(json_bytes) + 8 + len(bin_buf)
    with path.open("wb") as fh:
        fh.write(struct.pack("<III", GLB_MAGIC, 2, total))
        fh.write(struct.pack("<II", len(json_bytes), GLTF_JSON_CHUNK))
        fh.write(bytes(json_bytes))
        fh.write(struct.pack("<II", len(bin_buf), GLTF_BIN_CHUNK))
        fh.write(bytes(bin_buf))
    return path


def part_from_mesh(mesh, name: str, *, scalars: str | None = None, cmap: str = SPEED_CMAP,
                   clim: tuple[float, float] | None = None,
                   base_color: tuple[float, float, float, float] = (0.8, 0.85, 0.9, 1.0),
                   opacity: float = 1.0, decimate: float = 0.0,
                   scale: float = 1e-3) -> GlbPart | None:
    """A PyVista surface as a GLB part: triangulated, decimated, vertex-coloured.

    `scale` turns mm into metres, which is what a glTF viewer expects.
    """
    pv = pyvista()
    if mesh is None or mesh.n_points == 0:
        return None
    surf = mesh.extract_surface() if hasattr(mesh, "extract_surface") else mesh
    surf = surf.triangulate()
    if decimate and 0.0 < decimate < 0.95 and surf.n_cells > 4000:
        try:
            surf = surf.decimate(decimate)
        except Exception:
            pass
    faces = np.asarray(surf.faces).reshape(-1, 4)[:, 1:] if surf.n_cells else np.zeros((0, 3))
    if not len(faces):
        return None
    colors = None
    if scalars and scalars in surf.point_data:
        from matplotlib import cm, colors as mcolors
        vals = np.asarray(surf.point_data[scalars], dtype=float)
        good = np.isfinite(vals)
        lo, hi = (clim if clim else (float(np.nanmin(vals[good])) if good.any() else 0.0,
                                     float(np.nanmax(vals[good])) if good.any() else 1.0))
        norm = mcolors.Normalize(vmin=lo, vmax=max(hi, lo + 1e-9))
        rgba = cm.get_cmap(cmap)(norm(np.nan_to_num(vals)))
        rgba[~good] = (0.3, 0.32, 0.35, 1.0)
        colors = (rgba * 255).astype(np.uint8)
    return GlbPart(name=name, points=np.asarray(surf.points, dtype=np.float32) * scale,
                   triangles=faces.astype(np.uint32), colors=colors,
                   base_color=base_color, opacity=opacity)


# --- the per-chip entry point --------------------------------------------------------


def render_chip(chip_path: Path | str, protocol_path: Path | str | None = None, *,
                out_dir: Path | str, run_dir: Path | str | None = None, step: Any = None,
                resolution: Any = DEFAULT_RESOLUTION,
                cells_budget: int = DEFAULT_CELLS_BUDGET, nz: int = DEFAULT_NZ,
                max_steps: int = DEFAULT_MAX_STEPS, fps: int = DEFAULT_FPS,
                video: bool = True, findings: Sequence[dict] = (),
                log=print) -> dict:
    """PNG keyframes plus an MP4 for one chip. Returns a manifest dict.

    The MP4 walks the protocol steps, with the tracers moving inside each one, so it is a
    transient picture of a sequence of steady fields - and the caption on every frame says
    which step it is.
    """
    size = resolution_of(resolution)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = cases_for_chip(chip_path, protocol_path, run_dir=run_dir, step=step,
                           cells_budget=cells_budget, nz=nz, max_steps=max_steps,
                           findings=findings)
    if not cases:
        raise RenderError(f"{chip_path}: nothing to render")
    chip_id = cases[0].chip_id
    manifest: dict[str, Any] = {
        "chip_id": chip_id, "chip": Path(chip_path).as_posix(),
        "protocol": Path(protocol_path).as_posix() if protocol_path else None,
        "source": cases[0].source, "resolution": list(size),
        "steps": [], "keyframes": [], "video": None, "labels": cases[0].badges(),
        "notes": [],
    }
    keyframes: list[Path] = []
    movie: list[Path] = []

    for case in cases:
        volume = case.volume
        lines = _streamlines(volume, case)
        kinds = ["speed", "shear", "streamlines"]
        if volume.has_age:
            kinds.append("age")
        for kind in kinds:
            png = out_dir / f"{chip_id}_s{case.step}_{kind}.png"
            try:
                render_frame(case, kind, size, png, lines=lines)
            except Exception as exc:
                manifest["notes"].append(
                    f"{chip_id} step {case.step} {kind}: {type(exc).__name__}: {exc}")
                log(f"    skip {kind}: {type(exc).__name__}: {exc}")
                continue
            keyframes.append(png)
        tmp = out_dir / "frames"
        tmp.mkdir(exist_ok=True)
        for i in range(FRAMES_PER_STEP if video else 1):
            frame = tmp / f"{chip_id}_s{case.step}_t{i:03d}.png"
            try:
                render_frame(case, "tracers", size, frame, phase=i / FRAMES_PER_STEP,
                             lines=lines)
            except Exception as exc:
                # One unhappy frame must not throw away the other several hundred.
                manifest["notes"].append(
                    f"{chip_id} step {case.step} frame {i}: {type(exc).__name__}: {exc}")
                log(f"    frame {i} failed: {type(exc).__name__}: {exc}")
                break
            movie.append(frame)
        manifest["steps"].append({
            "step": case.step, "label": case.label, "source": case.source,
            "operation": case.operation, "fluid": case.fluid, "temp_c": case.temp_c,
            "q_in_ul_min": case.q_ul_min, "headline": case.headline(),
            "mean_age": bool(volume.has_age),
            "shear_ceiling": bool(volume.has_shear_ceiling),
            "notes": list(volume.notes), "assumptions": list(case.assumptions),
        })
        log(f"    {chip_id} step {case.step}: {case.source}, {len(kinds)} keyframes")

    if video and movie:
        try:
            mp4 = encode_mp4(movie, out_dir / f"{chip_id}.mp4", fps=fps)
            manifest["video"] = mp4.as_posix()
            manifest["video_seconds"] = video_duration_s(mp4) or round(len(movie) / fps, 2)
            # The intermediate frames are hundreds of megabytes and say nothing the MP4
            # and the keyframes do not. foam/README.md keeps "only the summary JSON and a
            # small set of keyframe images".
            shutil.rmtree(out_dir / "frames", ignore_errors=True)
        except RenderError as exc:
            manifest["notes"].append(str(exc))
    manifest["keyframes"] = [p.as_posix() for p in keyframes]
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m assay2chip.foam.render chip/module1_v1.json` - a quick single-chip check."""
    import argparse

    ap = argparse.ArgumentParser(description="render one chip's flow field")
    ap.add_argument("chip")
    ap.add_argument("--protocol")
    ap.add_argument("--step")
    ap.add_argument("--out", default=None)
    ap.add_argument("--resolution", default="1080p")
    ap.add_argument("--cells-budget", type=int, default=DEFAULT_CELLS_BUDGET)
    ap.add_argument("--no-video", action="store_true")
    args = ap.parse_args(list(argv) if argv is not None else None)
    out = Path(args.out) if args.out else ROOT / "assay2chip/work/foam_render/foam/render"
    man = render_chip(args.chip, args.protocol, out_dir=out, step=args.step,
                      resolution=args.resolution, cells_budget=args.cells_budget,
                      video=not args.no_video)
    print(json.dumps(man, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

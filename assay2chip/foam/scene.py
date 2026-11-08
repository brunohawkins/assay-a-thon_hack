"""The WHOLE PIPELINE in one scene: every chip on one bench, in chain order, animated.

`assay2chip/foam/README.md` is authoritative; this is its `scene.py` row, and it is the
thing the project owner actually asked for - "a 3D rendered flow sim of the full
end-to-end pipeline", made for OUR pipeline, not a stock animation.

DATA, NEVER A HARD-CODED CHAIN. The stages, their order, the ports a handoff joins and
which stages have no chip all come from `assay2chip.cfd.couple.read_chain`, which reads
either `protocols/pipeline_mvp.json` or a stage-S4 `partition.json`. Nothing in this file
names module1, barcoding or SPRI. When the corrected pipeline lands - barcoding back
between Module 3 and Module 4, and Module 6 - a re-run picks it up with no edit here, and
the only thing that changes is the picture.

WHAT IS DRAWN

    a chip stage        the real footprint, semi-transparent, its solved flow field on a
                        mid-depth slice, streamlines from its feed ports, tracers and a
                        dye front that move
    a stage with no     a labelled placeholder block, badged OFF-CHIP, and OFF-CHIP /
    chip                BOUGHT KIT when the chain's own words say a bought kit does the
                        job. The sample still crosses it, so the chain stays continuous.
    a handoff           a 3D tube between the ACTUAL port positions of the two stages,
                        with material travelling through it. A handoff the chain marks
                        TBC is drawn amber and labelled: an unnamed port is a real gap in
                        the design, not something to tidy away.

SIX CHIPS ARE ON THE PRINTER, AND THEIR CARDS SAY SO. A stage whose chip is on the print
queue - `tools/module_catalog.py`, or `built_for_demo` in the pipeline file, whichever
the data says - is badged PRINTED FOR THE DEMO, and its caption records its findings AS
BUILT. The scene never captions a suggested change to one: improvements belong to the
full-spec design, not to a chip that is already being made.

THE WALK. The camera opens on the whole bench, then moves stage to stage in chain order.
On each stage its field animates; between stages the handoff tube carries a slug of
material. A caption on every frame gives the stage, the protocol step, the flow and the
headline finding. SIMULATED is on every frame, DEMO SCALE whenever the stage on screen is
a 30 x 20 mm chip, and the source of each field (`openfoam` or `reduced-order (2.5D)`) is
named on the stage it belongs to.

OUTPUT (into `assay2chip/work/<run>/foam/render/`)

    pipeline_e2e.mp4        1080p, H.264 / yuv420p, about 60-120 s
    pipeline_e2e.glb        decimated coloured surfaces plus streamlines, for the web
    pipeline_e2e.labels.json  label text and world positions, so the WebGL viewer can
                            draw the captions as HTML instead of baking text into the mesh
    keyframes/*.png         one per stage and per handoff
    pipeline_e2e.json       the manifest: what was drawn, from which source, and why
"""

from __future__ import annotations

import json
import math
import shutil
import sys
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Iterable, Sequence

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.cfd import couple as cfd_couple  # noqa: E402  read_chain, Stage, Handoff
from assay2chip.foam import render as R  # noqa: E402

# --- bench geometry (mm) --------------------------------------------------------------

#: A placeholder block stands in for a stage with no chip. It is deliberately NOT the size
#: of a chip: nothing about it is a design, so it must not look like one.
PLACEHOLDER_MM = (26.0, 15.0)
GAP_X_MM = 16.0
GAP_Y_MM = 26.0
STAGES_PER_ROW = 4
MARGIN_MM = 14.0
#: How high above the bench a handoff tube arcs, in real mm (z is exaggerated on screen).
TUBE_RISE_MM = 1.6
TUBE_BORE_MM = 0.8

#: z is EXAGGERATED IN THE GEOMETRY, not by the renderer's own scale transform, and that
#: is not a detail. A 0.3 mm layer under a 30 mm footprint is invisible at true scale, so
#: it has to be stretched; but stretching the whole scene with `Plotter.set_scale` stretches
#: the handoff TUBES too, and a round 0.8 mm tube comes out an 11 mm ribbon. Building the
#: geometry at `z x Z` instead means a tube is tubed AFTER the stretch and stays round,
#: while the chips still read as solid objects. Every frame prints the factor.
Z = R.Z_EXAGGERATION
#: The arc height of a handoff tube, as it appears on screen (display mm, so not stretched).
TUBE_RISE_DISPLAY_MM = 7.0

#: Words in the chain's OWN text that say a bought item does this stage's job, used as an
#: override when the chain is explicit. Otherwise `badge_for` decides from the chain's
#: structure, not from a list of stage names - see that function.
KIT_WORDS = ("bought", "buy ", " kit", "kit ", "commercial", "off-the-shelf", "vendor",
             "catalogue")

CHIP_COLOUR = "#8aa6bd"
PLACEHOLDER_COLOUR = "#6b5f45"
TUBE_COLOUR = "#9ad0ff"
TUBE_TBC_COLOUR = "#ffb347"
BENCH_COLOUR = "#1b2026"

# --- timing ---------------------------------------------------------------------------

DWELL_S = 5.0            # how long the camera holds on one stage
FLY_S = 1.4              # stage to stage
HANDOFF_S = 2.2          # material crossing one tube
OPENING_S = 3.0          # the whole bench, at the start
CLOSING_S = 2.5


class SceneError(RuntimeError):
    pass


def _rel(path: Path) -> str:
    """Repo-relative when it is inside the repo, absolute when a caller sent it elsewhere."""
    try:
        return Path(path).relative_to(ROOT).as_posix()
    except ValueError:
        return Path(path).as_posix()


# --- the layout, as pure data (no PyVista, so it tests offline) ----------------------


@dataclass(frozen=True)
class Placement:
    """One stage, where it sits on the bench and what it is."""

    key: str
    name: str
    kind: str                 # chip | placeholder
    badge: str                # "" | OFF-CHIP | OFF-CHIP / BOUGHT KIT
    status: str
    chip_path: str | None
    protocol_path: str | None
    x_mm: float
    y_mm: float
    w_mm: float
    h_mm: float
    note: str = ""
    stage_kind: str = "module"      # module | roadmap, straight from the chain
    #: The slot this chip is being printed for, or "" when it exists in the twin only.
    printed_slot: str = ""

    @property
    def on_chip(self) -> bool:
        return self.kind == "chip"

    @property
    def printed(self) -> bool:
        return bool(self.printed_slot)

    @property
    def center(self) -> tuple[float, float]:
        return (self.x_mm + self.w_mm / 2.0, self.y_mm + self.h_mm / 2.0)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return (self.x_mm, self.x_mm + self.w_mm, self.y_mm, self.y_mm + self.h_mm)

    def port_xy(self, port_id: str, chip: dict | None) -> tuple[float, float] | None:
        """A named port in bench coordinates, or None when this stage has no such port."""
        if chip is None or not port_id:
            return None
        for p in chip.get("ports") or []:
            if str(p.get("id")) == str(port_id):
                pos = p.get("position") or [0, 0, 0]
                return (self.x_mm + R._f(pos[0]), self.y_mm + R._f(pos[1]))
        return None

    def dock(self, towards: tuple[float, float]) -> tuple[float, float]:
        """Where a tube meets this block when no port is named: the edge facing `towards`."""
        cx, cy = self.center
        dx, dy = towards[0] - cx, towards[1] - cy
        if abs(dx) < 1e-9 and abs(dy) < 1e-9:
            return (cx, cy)
        tx = (self.w_mm / 2.0) / abs(dx) if abs(dx) > 1e-9 else math.inf
        ty = (self.h_mm / 2.0) / abs(dy) if abs(dy) > 1e-9 else math.inf
        t = min(tx, ty)
        return (cx + dx * t, cy + dy * t)


@dataclass(frozen=True)
class Tube:
    """One handoff: a real 3D tube between two points on the bench."""

    id: str
    carries: str
    from_key: str
    to_key: str
    from_port: str
    to_port: str
    a: tuple[float, float, float]
    b: tuple[float, float, float]
    tbc: bool
    after_step: int | None
    note: str = ""

    def label(self) -> str:
        src = self.from_port or "port TBC"
        dst = self.to_port or "port TBC"
        return f"{self.id}: {src} -> {dst}  ({self.carries})"

    def path(self, samples: int = 48) -> np.ndarray:
        """A quadratic arc from a to b, lifted clear of the bench."""
        a, b = np.asarray(self.a, dtype=float), np.asarray(self.b, dtype=float)
        mid = (a + b) / 2.0
        mid[2] = max(a[2], b[2]) + TUBE_RISE_MM
        t = np.linspace(0.0, 1.0, samples)[:, None]
        return (1 - t) ** 2 * a + 2 * (1 - t) * t * mid + t ** 2 * b


@dataclass
class Layout:
    """The whole bench, as data. Everything the renderer needs and nothing it does not."""

    id: str
    source: str
    kind: str
    order: list[str]
    stages: dict[str, Placement]
    tubes: list[Tube]
    bench_mm: tuple[float, float]
    unresolved: list[dict] = dc_field(default_factory=list)

    def placements(self) -> list[Placement]:
        return [self.stages[k] for k in self.order if k in self.stages]

    def chips(self) -> list[Placement]:
        return [p for p in self.placements() if p.on_chip]

    def placeholders(self) -> list[Placement]:
        return [p for p in self.placements() if not p.on_chip]

    def tube_for(self, from_key: str) -> Tube | None:
        return next((t for t in self.tubes if t.from_key == from_key), None)

    def to_json(self) -> dict:
        return {
            "id": self.id, "source": self.source, "kind": self.kind,
            "order": list(self.order),
            "bench_mm": [round(v, 2) for v in self.bench_mm],
            "stages": [
                {"key": p.key, "name": p.name, "kind": p.kind, "badge": p.badge,
                 "printed_for_demo": p.printed, "printed_slot": p.printed_slot,
                 "status": p.status, "chip": p.chip_path, "protocol": p.protocol_path,
                 "x_mm": round(p.x_mm, 3), "y_mm": round(p.y_mm, 3),
                 "w_mm": round(p.w_mm, 3), "h_mm": round(p.h_mm, 3),
                 "stage_kind": p.stage_kind}
                for p in self.placements()
            ],
            "tubes": [
                {"id": t.id, "carries": t.carries, "from": t.from_key, "to": t.to_key,
                 "from_port": t.from_port, "to_port": t.to_port, "tbc": t.tbc,
                 "after_step": t.after_step,
                 "a": [round(v, 3) for v in t.a], "b": [round(v, 3) for v in t.b]}
                for t in self.tubes
            ],
            "unresolved": list(self.unresolved),
        }


def chain_order(chain: Any) -> list[str]:
    """Chain order from the HANDOFFS, with the file's own order breaking every tie.

    A pipeline file lists its modules in order and then its roadmap stages, but the
    handoffs are what actually say what follows what - and a merge that inserts a stage
    (barcoding between Module 3 and Module 4) changes the handoffs. Kahn's algorithm over
    the handoff edges, seeded in file order, therefore follows the merge without an edit
    here. Anything a cycle makes unorderable keeps its file position and is reported.
    """
    keys = list(chain.stages.keys())
    rank = {k: i for i, k in enumerate(keys)}
    edges: dict[str, set[str]] = {k: set() for k in keys}
    indeg: dict[str, int] = {k: 0 for k in keys}
    for h in chain.handoffs:
        a, b = h.from_stage, h.to_stage
        if a not in edges or b not in edges or a == b or b in edges[a]:
            continue
        edges[a].add(b)
        indeg[b] += 1
    ready = sorted([k for k in keys if indeg[k] == 0], key=lambda k: rank[k])
    out: list[str] = []
    while ready:
        here = ready.pop(0)
        out.append(here)
        for nxt in sorted(edges[here], key=lambda k: rank[k]):
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
        ready.sort(key=lambda k: rank[k])
    for k in keys:                      # a cycle: keep it, in file order, and say so
        if k not in out:
            out.append(k)
    return out


def badge_for(stage: Any) -> str:
    """The badge a stage with no chip wears, decided by the CHAIN, not by its name.

    Two different things look alike in a pipeline file and must not read alike on screen:

      a DROPPED placeholder    no chip and no protocol: nothing happens to the sample, it
                               just crosses the stage (`Stage.pass_through` in
                               `assay2chip/cfd/couple.py`). Badge: OFF-CHIP.
      real off-chip work       a stage that does something to the sample with bought
                               reagents and instruments - the droplet kit, SPRI, QC,
                               sequencing. Badge: OFF-CHIP / BOUGHT KIT.

    So when the corrected pipeline brings barcoding back as a real off-chip kit between
    Module 3 and Module 4, the badge changes by itself: the chain stops calling it a
    dropped placeholder and starts giving it work.
    """
    if stage.chip_path:
        return ""
    text = " ".join(str(x or "").lower()
                    for x in (stage.name, stage.status, stage.off_chip_reason))
    if any(word in text for word in KIT_WORDS):
        return f"{R.OFF_CHIP} / {R.BOUGHT_KIT}"
    if getattr(stage, "pass_through", False):
        return R.OFF_CHIP
    return f"{R.OFF_CHIP} / {R.BOUGHT_KIT}"


def built_for_demo_map(path: Path | str) -> dict[str, bool]:
    """`{module key: built_for_demo}` straight out of a pipeline file, if it says.

    `built_for_demo` is in `contracts/schemas/pipeline.schema.json` but is not part of
    `couple.Stage`, so it is read here rather than guessed. A partition file has no such
    key and gets an empty map.
    """
    try:
        doc = R.load_json(path)
    except Exception:
        return {}
    out: dict[str, bool] = {}
    for mod in (doc.get("modules") or []) if isinstance(doc, dict) else []:
        key = mod.get("module")
        if key is not None and "built_for_demo" in mod:
            out[str(key)] = bool(mod["built_for_demo"])
    return out


def printed_slot_for(stage: Any, built_for_demo: bool | None = None) -> str:
    """Which slot this stage's chip is printed for, or "" - from the data, in order.

    1. the chain file's own `built_for_demo` for this module. It is what a merge updates,
       so it wins when it is set true.
    2. the frozen print queue in `tools/module_catalog.py`, which is the one place that
       currently says which six chips are on the printer.

    Nothing is listed here, so when the corrected pipeline arrives with Module 6 - and
    whenever `built_for_demo` is turned on - the cards change by themselves.
    """
    if not getattr(stage, "chip_path", None):
        return ""
    if built_for_demo:
        return str(getattr(stage, "name", "") or stage.key)
    return R.is_printed(stage.chip_path) or ""


def _chip_size(chip_path: str | None) -> tuple[float, float, dict | None]:
    if not chip_path:
        return (*PLACEHOLDER_MM, None)
    path = ROOT / chip_path if not Path(chip_path).is_absolute() else Path(chip_path)
    if not path.exists():
        return (*PLACEHOLDER_MM, None)
    try:
        chip = R.load_json(path)
    except Exception:
        return (*PLACEHOLDER_MM, None)
    fx, fy = R.footprint_mm(chip)
    return fx, fy, chip


def layout_for(chain: Any, *, per_row: int = STAGES_PER_ROW,
               built_for_demo: dict[str, bool] | None = None
               ) -> tuple[Layout, dict[str, dict]]:
    """Place every stage of the chain on one bench, in chain order. Pure data.

    The rows SERPENTINE - left to right, then right to left - so two consecutive stages
    are always neighbours and a handoff tube never has to cross the whole bench.
    """
    order = chain_order(chain)
    sizes: dict[str, tuple[float, float]] = {}
    chips: dict[str, dict] = {}
    for key in order:
        stage = chain.stages[key]
        w, h, chip = _chip_size(stage.chip_path)
        sizes[key] = (w, h)
        if chip is not None:
            chips[key] = chip

    rows: list[list[str]] = [order[i:i + per_row] for i in range(0, len(order), per_row)]
    placements: dict[str, Placement] = {}
    y = MARGIN_MM
    bench_w = MARGIN_MM
    row_tops: list[float] = []
    for r, row in enumerate(rows):
        row_h = max((sizes[k][1] for k in row), default=PLACEHOLDER_MM[1])
        keys = row if r % 2 == 0 else list(reversed(row))
        x = MARGIN_MM
        spans: dict[str, tuple[float, float]] = {}
        for key in keys:
            w, h = sizes[key]
            spans[key] = (x, w)
            x += w + GAP_X_MM
        bench_w = max(bench_w, x - GAP_X_MM + MARGIN_MM)
        for key in row:
            stage = chain.stages[key]
            w, h = sizes[key]
            x0, _ = spans[key]
            placements[key] = Placement(
                key=key, name=stage.name,
                kind="chip" if (stage.chip_path and key in chips) else "placeholder",
                badge=badge_for(stage), status=stage.status,
                chip_path=stage.chip_path, protocol_path=stage.protocol_path,
                x_mm=x0, y_mm=y + (row_h - h) / 2.0, w_mm=w, h_mm=h,
                note=stage.off_chip_reason or "", stage_kind=stage.kind,
                printed_slot=printed_slot_for(stage, (built_for_demo or {}).get(key)),
            )
        row_tops.append(y + row_h)
        y += row_h + GAP_Y_MM
    bench_h = (row_tops[-1] if row_tops else MARGIN_MM) + MARGIN_MM

    tubes: list[Tube] = []
    for h in chain.handoffs:
        src = placements.get(h.from_stage)
        dst = placements.get(h.to_stage)
        if src is None or dst is None:
            continue
        from_port = h.from_ports[0] if h.from_ports else ""
        to_port = h.to_ports[0] if h.to_ports else ""
        a = src.port_xy(from_port, chips.get(h.from_stage)) or src.dock(dst.center)
        b = dst.port_xy(to_port, chips.get(h.to_stage)) or dst.dock(src.center)
        named_a = src.port_xy(from_port, chips.get(h.from_stage)) is not None
        named_b = dst.port_xy(to_port, chips.get(h.to_stage)) is not None
        depth_a = R.fluid_depth_mm(chips.get(h.from_stage) or {})
        depth_b = R.fluid_depth_mm(chips.get(h.to_stage) or {})
        tubes.append(Tube(
            id=h.id, carries=h.carries, from_key=h.from_stage, to_key=h.to_stage,
            from_port=from_port if named_a else "", to_port=to_port if named_b else "",
            a=(a[0], a[1], depth_a), b=(b[0], b[1], depth_b),
            tbc=not (named_a and named_b), after_step=h.after_step, note=h.note,
        ))

    layout = Layout(
        id=getattr(chain, "id", "pipeline"), source=getattr(chain, "source", ""),
        kind=getattr(chain, "kind", "pipeline"), order=order, stages=placements,
        tubes=tubes, bench_mm=(bench_w, bench_h),
        unresolved=[dict(u) for u in getattr(chain, "unresolved", []) or []],
    )
    return layout, chips


def layout_from_file(path: Path | str, *, per_row: int = STAGES_PER_ROW
                     ) -> tuple[Layout, dict[str, dict]]:
    """`protocols/pipeline_mvp.json` or a `partition.json` -> a bench. Offline, no VTK."""
    return layout_for(cfd_couple.read_chain(path), per_row=per_row,
                      built_for_demo=built_for_demo_map(path))


# --- captions (pure text, so the wording is testable without rendering) --------------


def caption_for(place: Placement, case: Any | None, *, phase: str = "stage",
                tube: Tube | None = None) -> list[str]:
    """Stage, step, flow and the headline finding - the four lines the brief asks for."""
    head = f"{place.name}"
    if place.badge:
        head = f"{head}    [{place.badge}]"
    if place.printed:
        head = f"{head}    [{R.PRINTED_FOR_DEMO}]"
    lines = [head]
    if phase == "handoff" and tube is not None:
        src = tube.from_port or "an unnamed port (TBC)"
        dst = tube.to_port or "an unnamed port (TBC)"
        lines.append(f"handoff {tube.id}: {tube.carries} from {src} to {dst}"
                     + (f", after step {tube.after_step}" if tube.after_step else ""))
        lines.append("TUBING AND PORT NOT NAMED IN THE CHAIN - a real gap, drawn amber"
                     if tube.tbc else "connector match: both ends are 0.8 mm drilled ports"
                     if "match" in (tube.note or "").lower() else
                     R._wrap(tube.note or "by tubing between the two stages", 96))
        return lines
    if not place.on_chip:
        lines.append(f"no chip: {R._wrap(place.note or place.status, 96)}")
        lines.append("the sample still crosses this stage, so the chain is drawn through it")
        return lines
    if case is None:
        lines.append("no solved field for this chip yet")
        return lines
    step = f"step {case.step}" if case.step is not None else "no step"
    flow = f"{case.q_ul_min:g} uL/min" if case.q_ul_min else "no flow in this step"
    lines.append(f"{step}  ·  {case.operation or '-'}  ·  {flow}"
                 + (f"  ·  {case.fluid}" if case.fluid else "")
                 + (f" at {case.temp_c:g} C" if case.temp_c else ""))
    lines.append(f"finding: {R._wrap(case.headline(), 96)}")
    if place.printed or case.printed:
        lines.append(R._wrap(R.AS_BUILT_NOTE, 96))
    lines.append(f"field: {case.source}")
    return lines


def storyboard(layout: Layout, *, fps: int = R.DEFAULT_FPS, dwell_s: float = DWELL_S,
               fly_s: float = FLY_S, handoff_s: float = HANDOFF_S,
               opening_s: float = OPENING_S, closing_s: float = CLOSING_S) -> list[dict]:
    """The shot list: every segment of the walk, with its frame count. Pure data.

    `kind` is `opening` | `fly` | `stage` | `handoff` | `closing`; `key` names the stage
    and `tube` the handoff being crossed. The renderer just plays this back, which is what
    makes the length of the video a number this module can state before it renders a pixel.
    """
    shots: list[dict] = [{"kind": "opening", "key": None, "tube": None,
                          "frames": max(1, int(opening_s * fps))}]
    previous: str | None = None
    for key in layout.order:
        place = layout.stages.get(key)
        if place is None:
            continue
        if previous is not None:
            tube = next((t for t in layout.tubes
                         if t.from_key == previous and t.to_key == key), None)
            shots.append({"kind": "fly", "key": key, "from": previous, "tube": None,
                          "frames": max(1, int(fly_s * fps))})
            if tube is not None:
                shots.append({"kind": "handoff", "key": key, "from": previous,
                              "tube": tube.id, "frames": max(1, int(handoff_s * fps))})
        shots.append({"kind": "stage", "key": key, "tube": None,
                      "frames": max(1, int(dwell_s * fps))})
        previous = key
    shots.append({"kind": "closing", "key": None, "tube": None,
                  "frames": max(1, int(closing_s * fps))})
    return shots


def storyboard_seconds(shots: Sequence[dict], fps: int = R.DEFAULT_FPS) -> float:
    return round(sum(int(s["frames"]) for s in shots) / float(fps), 2)


def fit_to_window(layout: Layout, *, fps: int = R.DEFAULT_FPS, low_s: float = 60.0,
                  high_s: float = 120.0) -> dict:
    """Dwell times scaled so the finished video lands in the 60-120 s the brief asks for.

    Ten stages at five seconds each overshoots; three stages undershoots. The shot lengths
    are therefore scaled by one factor until the total is inside the window, rather than
    the video being whatever length the chain happens to make it.
    """
    base = storyboard(layout, fps=fps)
    total = storyboard_seconds(base, fps)
    if total <= 0:
        return {"scale": 1.0, "seconds": 0.0, "shots": base}
    scale = 1.0
    if total > high_s:
        scale = high_s / total
    elif total < low_s:
        scale = low_s / total
    shots = storyboard(layout, fps=fps, dwell_s=DWELL_S * scale, fly_s=FLY_S * scale,
                       handoff_s=HANDOFF_S * scale, opening_s=OPENING_S * scale,
                       closing_s=CLOSING_S * scale)
    return {"scale": round(scale, 4), "seconds": storyboard_seconds(shots, fps),
            "shots": shots}


# --- the drawable form of one stage --------------------------------------------------


@dataclass
class StepView:
    """One protocol step of one chip, reduced to the surfaces the bench draws."""

    case: Any
    slice_mid: Any = None
    floor: Any = None
    streams: Any = None
    paths: list[np.ndarray] = dc_field(default_factory=list)
    arclen: list[np.ndarray] = dc_field(default_factory=list)
    speed_clim: tuple[float, float] | None = None
    age_scale_s: float | None = None


@dataclass
class SceneStage:
    """What a stage contributes to the bench: surfaces, paths and the words on it.

    A stage holds SEVERAL steps, because the brief asks the animation to walk the
    protocol: while the camera is on a chip the scene cuts from one of its steps to the
    next, and the caption follows.
    """

    place: Placement
    steps: list[StepView] = dc_field(default_factory=list)
    body: Any = None
    source: str = ""

    @property
    def key(self) -> str:
        return self.place.key

    @property
    def case(self) -> Any | None:
        return self.steps[0].case if self.steps else None

    def index_at(self, phase: float) -> int:
        """Which step is on screen `phase` of the way through this stage's dwell."""
        if not self.steps:
            return -1
        return min(int(max(phase, 0.0) * len(self.steps)), len(self.steps) - 1)

    def view_at(self, phase: float) -> StepView | None:
        i = self.index_at(phase)
        return self.steps[i] if i >= 0 else None

    @property
    def shown(self) -> int:
        return getattr(self, "_shown", 0)


def _place(mesh, dx: float, dy: float):
    """A chip-local mesh moved onto the bench, with z stretched into display space."""
    out = mesh.copy()
    pts = np.asarray(out.points, dtype=float).copy()
    pts[:, 0] += dx
    pts[:, 1] += dy
    pts[:, 2] *= Z
    out.points = pts
    return out


def display_path(tube: Tube, samples: int = 48) -> np.ndarray:
    """The arc a handoff tube follows ON SCREEN: ports in display z, a fixed arc height.

    `Tube.path` is the data-space form and is what the tests pin; this is the same curve
    drawn where the exaggerated chips actually are.
    """
    a = np.array([tube.a[0], tube.a[1], tube.a[2] * Z], dtype=float)
    b = np.array([tube.b[0], tube.b[1], tube.b[2] * Z], dtype=float)
    mid = (a + b) / 2.0
    mid[2] = max(a[2], b[2]) + TUBE_RISE_DISPLAY_MM
    t = np.linspace(0.0, 1.0, samples)[:, None]
    return (1 - t) ** 2 * a + 2 * (1 - t) * t * mid + t ** 2 * b


def _paths_of(lines) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Streamline polylines plus their cumulative arc length, for riding tracers along."""
    if lines is None or lines.n_points == 0:
        return [], []
    arr = np.asarray(lines.lines)
    pts = np.asarray(lines.points, dtype=float)
    polys, lens = [], []
    i = 0
    while i < len(arr):
        n = int(arr[i])
        ids = arr[i + 1:i + 1 + n]
        i += 1 + n
        if n < 2:
            continue
        xyz = pts[ids]
        d = np.r_[0.0, np.cumsum(np.linalg.norm(np.diff(xyz, axis=0), axis=1))]
        if d[-1] <= 0:
            continue
        polys.append(xyz)
        lens.append(d)
    return polys, lens


STEPS_PER_STAGE = 3


def build_stage(place: Placement, *, protocol_path: str | None = None,
                run_dir: Path | str | None = None, cells_budget: int,
                max_steps: int = STEPS_PER_STAGE,
                findings: Sequence[dict] = (), log=print) -> SceneStage:
    """Solve (or read) one stage's fields and reduce them to the surfaces the scene draws.

    The volume itself is dropped once the slices and streamlines are out of it: eleven
    stages x three steps of full 3D grid will not fit in memory at once, and the bench
    only ever shows surfaces.
    """
    stage = SceneStage(place=place)
    stage.body = R.pyvista().Box(bounds=(place.x_mm, place.x_mm + place.w_mm,
                                         place.y_mm, place.y_mm + place.h_mm,
                                         -0.05 * Z, 0.35 * Z))
    if not place.on_chip:
        stage.source = R.OFF_CHIP
        return stage

    chip_path = ROOT / place.chip_path
    proto = protocol_path or place.protocol_path
    proto_path = (ROOT / proto) if proto else None
    try:
        cases = R.cases_for_chip(chip_path, proto_path, run_dir=run_dir,
                                 cells_budget=cells_budget, nz=7, max_steps=max_steps,
                                 findings=findings)
    except Exception as exc:
        log(f"    {place.key}: no field ({type(exc).__name__}: {exc})")
        stage.source = "no field"
        return stage
    if not cases:
        stage.source = "no field"
        return stage
    stage.source = cases[0].source
    dx, dy = place.x_mm, place.y_mm
    for case in cases:
        volume = case.volume
        if volume is None:
            continue
        view = StepView(case=case, age_scale_s=volume.age_scale_s)
        stage.body = R.pyvista().Box(bounds=(dx, dx + place.w_mm, dy, dy + place.h_mm,
                                             -volume.depth_mm * 0.2 * Z,
                                             volume.depth_mm * 1.2 * Z))
        view.slice_mid = _place(R._layer(volume, "mid"), dx, dy)
        view.floor = _place(R._layer(volume, "floor"), dx, dy)
        view.speed_clim, _ = R._clim(volume.grid, "speed_mm_s", 99.0)
        lines = R._streamlines(volume, case)
        if lines is not None and lines.n_points:
            # Stretch FIRST, tube SECOND, or every streamline comes out a flat ribbon.
            moved = _place(lines, dx, dy)
            try:
                view.streams = moved.tube(radius=max(place.w_mm * 0.004,
                                                     volume.spacing_mm * 0.6))
            except Exception:
                view.streams = moved
            view.paths, view.arclen = _paths_of(moved)
        case.volume = None                   # the grid is large; the surfaces are enough
        stage.steps.append(view)
    return stage


# --- the animation -------------------------------------------------------------------


def _ease(t: float) -> float:
    t = min(max(t, 0.0), 1.0)
    return t * t * (3.0 - 2.0 * t)


def _view_for(place: Placement | None, layout: Layout,
              size: tuple[int, int] = (1920, 1080)) -> dict:
    """Camera for one stage, or for the whole bench when `place` is None.

    Parallel projection throughout, so `scale` is literally half the world height on
    screen: it is computed from the thing being framed AND the frame's aspect ratio, not
    guessed, which is what stops a chip ending up a third of the picture.
    """
    w_px, h_px = size
    aspect = h_px / float(w_px)
    if place is None:
        bw, bh = layout.bench_mm
        return {"focal": (bw / 2.0, bh / 2.0, 0.0),
                "offset": (-bw * 0.05, -bh * 0.90, max(bw, bh) * 0.62),
                "scale": max(bh, bw * aspect) * 0.60}
    cx, cy = place.center
    return {"focal": (cx, cy, 0.0),
            "offset": (-place.w_mm * 0.10, -place.h_mm * 1.15, max(place.w_mm,
                                                                  place.h_mm) * 0.70),
            "scale": max(place.h_mm, place.w_mm * aspect) * 0.72}


def _pair_view(a: Placement, b: Placement, size: tuple[int, int] = (1920, 1080)) -> dict:
    """A view that holds BOTH ends of a handoff, so the tube is never half off screen."""
    w_px, h_px = size
    aspect = h_px / float(w_px)
    x0 = min(a.bounds[0], b.bounds[0])
    x1 = max(a.bounds[1], b.bounds[1])
    y0 = min(a.bounds[2], b.bounds[2])
    y1 = max(a.bounds[3], b.bounds[3])
    w, h = x1 - x0, y1 - y0
    return {"focal": ((x0 + x1) / 2.0, (y0 + y1) / 2.0, 0.0),
            "offset": (-w * 0.08, -h * 1.05, max(w, h) * 0.62),
            "scale": max(h, w * aspect) * 0.70}


def _blend(a: dict, b: dict, t: float) -> dict:
    t = _ease(t)
    return {
        "focal": tuple(a["focal"][i] + (b["focal"][i] - a["focal"][i]) * t for i in range(3)),
        "offset": tuple(a["offset"][i] + (b["offset"][i] - a["offset"][i]) * t for i in range(3)),
        "scale": a["scale"] + (b["scale"] - a["scale"]) * t,
    }


def _apply_view(plotter, view: dict) -> None:
    fx, fy, fz = view["focal"]
    ox, oy, oz = view["offset"]
    plotter.camera_position = [(fx + ox, fy + oy, fz + oz), (fx, fy, fz), (0.0, 0.0, 1.0)]
    plotter.camera.parallel_scale = max(view["scale"], 1e-3)


def _tracer_points(view: StepView | None, phase: float,
                   per_path: int = 3) -> np.ndarray | None:
    if view is None or not view.paths:
        return None
    pts = []
    stride = max(1, len(view.paths) // 60)
    for xyz, d in list(zip(view.paths, view.arclen))[::stride]:
        for k in range(per_path):
            t = ((phase + k / per_path) % 1.0) * d[-1]
            pts.append(xyz[int(np.searchsorted(d, t))])
    return np.asarray(pts, dtype=float) if pts else None


def _slug(tube: Tube, phase: float, samples: int = 10) -> np.ndarray:
    """The slug of material part way along a handoff tube."""
    path = display_path(tube, 96)
    n = len(path)
    start = int(min(max(phase, 0.0), 1.0) * (n - samples - 1))
    return path[start:start + samples]


def render_pipeline(chain_path: Path | str, *, out_dir: Path | str,
                    run_dir: Path | str | None = None,
                    resolution: Any = "1080p", fps: int = R.DEFAULT_FPS,
                    cells_budget: int = 320_000, per_row: int = STAGES_PER_ROW,
                    steps_per_stage: int = STEPS_PER_STAGE,
                    video: bool = True, glb: bool = True,
                    findings: Sequence[dict] = (), log=print) -> dict:
    """The whole thing: bench, walk, MP4, keyframes and GLB. Returns the manifest."""
    pv = R.pyvista()
    size = R.resolution_of(resolution)
    out_dir = Path(out_dir)
    keys_dir = out_dir / "keyframes"
    frames_dir = out_dir / "frames"
    for d in (out_dir, keys_dir, frames_dir):
        d.mkdir(parents=True, exist_ok=True)

    layout, chips = layout_from_file(chain_path, per_row=per_row)
    log(f"  bench {layout.bench_mm[0]:.0f} x {layout.bench_mm[1]:.0f} mm, "
        f"{len(layout.order)} stages, {len(layout.tubes)} handoffs")

    stages: dict[str, SceneStage] = {}
    for place in layout.placements():
        log(f"  stage {place.key} ({place.kind})")
        stages[place.key] = build_stage(place, run_dir=run_dir, cells_budget=cells_budget,
                                        max_steps=steps_per_stage, findings=findings,
                                        log=log)
        got = stages[place.key]
        if got.steps:
            log(f"    {len(got.steps)} step(s), {got.source}: "
                f"{', '.join(str(v.case.step) for v in got.steps)}")

    plan = fit_to_window(layout, fps=fps)
    shots = plan["shots"]
    log(f"  storyboard: {len(shots)} shots, {plan['seconds']:.1f} s at {fps} fps "
        f"(timing scaled x{plan['scale']})")

    plotter = pv.Plotter(off_screen=True, window_size=list(size))
    plotter.enable_parallel_projection()
    plotter.set_background(R.BENCH_BG)
    _draw_bench(plotter, layout, stages, chips, size)

    frames: list[Path] = []
    keyframes: list[dict] = []
    index = 0
    previous_view = _view_for(None, layout, size)
    # Each shot eases from where the last one left the camera, so the walk is continuous:
    # a handoff frames BOTH ends of its tube and the dwell that follows settles onto the
    # chip over the first part of its own time rather than cutting to it.
    MOVE_FRACTION = {"fly": 1.0, "closing": 1.0, "handoff": 0.5, "stage": 0.3,
                     "opening": 0.0}
    for shot in shots:
        key = shot.get("key")
        place = layout.stages.get(key) if key else None
        stage = stages.get(key) if key else None
        tube = next((t for t in layout.tubes if t.id == shot.get("tube")), None)
        if shot["kind"] in ("opening", "closing") or place is None:
            target = _view_for(None, layout, size)
        elif shot["kind"] == "handoff" and tube is not None:
            target = _pair_view(layout.stages[tube.from_key], layout.stages[tube.to_key],
                                size)
        else:
            target = _view_for(place, layout, size)
        move = MOVE_FRACTION.get(shot["kind"], 1.0)
        n = int(shot["frames"])
        for i in range(n):
            t = (i + 1) / n
            view = target if move <= 0 else _blend(previous_view, target,
                                                   min(t / move, 1.0))
            _apply_view(plotter, view)
            phase = t
            _update_dynamic(plotter, layout, stages, shot, tube, phase, size)
            frame = frames_dir / f"f{index:05d}.png"
            plotter.screenshot(str(frame))
            frames.append(frame)
            index += 1
            if i == n // 2 and shot["kind"] in ("stage", "handoff", "opening"):
                name = (f"{shot['kind']}_{key or layout.id}"
                        f"{'_' + tube.id if tube else ''}.png")
                shot_png = keys_dir / name
                plotter.screenshot(str(shot_png))
                keyframes.append({
                    "file": shot_png.as_posix(), "kind": shot["kind"], "stage": key,
                    "tube": tube.id if tube else None,
                    "shows": _describe(layout, stage, place, tube, shot["kind"]),
                })
        previous_view = target
    plotter.close()

    manifest: dict[str, Any] = {
        "pipeline": Path(chain_path).as_posix(),
        "layout": layout.to_json(),
        "resolution": list(size), "fps": fps,
        "storyboard": {"shots": len(shots), "seconds": plan["seconds"],
                       "timing_scale": plan["scale"]},
        "stages": [
            {"key": s.key, "name": s.place.name, "kind": s.place.kind,
             "badge": s.place.badge, "source": s.source,
             "printed_for_demo": bool(s.place.printed),
             "printed_slot": s.place.printed_slot,
             "steps": [v.case.step for v in s.steps],
             "headline": s.case.headline() if s.case else None,
             "chip": s.place.chip_path}
            for s in (stages[k] for k in layout.order if k in stages)
        ],
        "keyframes": keyframes,
        "labels": R.SIMULATED,
        "notes": [],
    }

    if video and frames:
        try:
            mp4 = R.encode_mp4(frames, out_dir / "pipeline_e2e.mp4", fps=fps)
            manifest["video"] = mp4.as_posix()
            manifest["video_seconds"] = R.video_duration_s(mp4) or plan["seconds"]
            log(f"  wrote {_rel(mp4)} ({manifest['video_seconds']} s)")
            # Thousands of intermediate PNGs say nothing the MP4 and the keyframes do not
            # (foam/README.md: keep "only the summary JSON and a small set of keyframes").
            shutil.rmtree(frames_dir, ignore_errors=True)
        except R.RenderError as exc:
            manifest["notes"].append(str(exc))
            log(f"  no MP4: {exc}")

    if glb:
        try:
            path = export_glb(layout, stages, out_dir / "pipeline_e2e.glb")
            manifest["glb"] = path.as_posix()
            manifest["glb_bytes"] = path.stat().st_size
            labels = write_labels(layout, stages, out_dir / "pipeline_e2e.labels.json")
            manifest["labels_json"] = labels.as_posix()
            log(f"  wrote {_rel(path)} ({path.stat().st_size // 1024} kB)")
            manifest["notes"].append(
                "pipeline_e2e.glb is not in .gitignore's keep-list for the work dir "
                "(it excepts *.gltf, not *.glb). VTK's exporter only writes JSON glTF "
                "with an empty texture URI, which no WebGL loader accepts, so this is a "
                "real binary GLB - add '!assay2chip/work/**/foam/**/*.glb' if the web "
                "viewer should serve it from the repo.")
        except Exception as exc:
            manifest["notes"].append(f"no GLB: {type(exc).__name__}: {exc}")
            log(f"  no GLB: {exc}")

    (out_dir / "pipeline_e2e.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def _describe(layout: Layout, stage: SceneStage | None, place: Placement | None,
              tube: Tube | None, kind: str) -> str:
    """What this keyframe shows, in a sentence, for the manifest and the hand-off note."""
    if kind == "opening":
        return (f"the whole bench: {len(layout.chips())} chips and "
                f"{len(layout.placeholders())} off-chip stages in chain order, "
                f"{len(layout.tubes)} handoff tubes")
    if kind == "handoff" and tube is not None:
        return (f"{tube.carries} moving through {tube.id} from "
                f"{tube.from_port or 'an unnamed port (TBC)'} to "
                f"{tube.to_port or 'an unnamed port (TBC)'}")
    if place is None:
        return "the bench"
    if not place.on_chip:
        return (f"{place.name}, a placeholder block badged {place.badge}; "
                f"{place.note or place.status}")
    printed = f" [{R.PRINTED_FOR_DEMO}]" if place.printed else ""
    case = shown_case(stage)
    if case is None:
        return f"{place.name}, no solved field"
    return (f"{place.name}{printed}: {case.source} field, step {case.step} "
            f"({case.operation}), {case.q_ul_min or 0:g} uL/min, streamlines from "
            f"{len(case.feeds)} feed port(s); {case.headline()}")


def shown_case(stage: SceneStage | None) -> Any | None:
    """The Case currently on the bench for this stage (the step the camera is watching)."""
    if stage is None or not stage.steps:
        return None
    return stage.steps[min(stage.shown, len(stage.steps) - 1)].case


def show_step(plotter, stage: SceneStage, index: int) -> int:
    """Put step `index` of this stage on the bench, replacing whatever was there.

    Actors are replaced by NAME, so swapping a chip from one protocol step to the next is
    two mapper swaps rather than a rebuilt scene - which is what makes a 90 s walk through
    eleven stages render in minutes instead of hours.
    """
    if not stage.steps:
        return -1
    index = min(max(index, 0), len(stage.steps) - 1)
    if getattr(stage, "_shown", None) == index:
        return index
    view = stage.steps[index]
    key = stage.key
    if view.slice_mid is not None and view.slice_mid.n_points:
        plotter.add_mesh(view.slice_mid, scalars="speed_mm_s", cmap=R.SPEED_CMAP,
                         clim=view.speed_clim, show_scalar_bar=False, name=f"mid_{key}")
    if view.streams is not None and view.streams.n_points:
        plotter.add_mesh(view.streams, scalars="speed_mm_s", cmap=R.SPEED_CMAP,
                         clim=view.speed_clim, show_scalar_bar=False,
                         name=f"streams_{key}")
    else:
        plotter.remove_actor(f"streams_{key}", render=False)
    stage._shown = index
    return index


def _draw_bench(plotter, layout: Layout, stages: dict[str, SceneStage],
                chips: dict[str, dict], size: tuple[int, int]) -> None:
    """Everything that does not move: bench, bodies, fields, tubes, names."""
    pv = R.pyvista()
    bw, bh = layout.bench_mm
    plotter.add_mesh(pv.Plane(center=(bw / 2, bh / 2, -0.1 * Z), direction=(0, 0, 1),
                              i_size=bw * 1.04, j_size=bh * 1.08),
                     color=BENCH_COLOUR, opacity=1.0)

    for key in layout.order:
        stage = stages.get(key)
        if stage is None:
            continue
        place = stage.place
        colour = CHIP_COLOUR if place.on_chip else PLACEHOLDER_COLOUR
        plotter.add_mesh(stage.body, color=colour,
                         opacity=0.12 if place.on_chip else 0.38,
                         show_edges=True, edge_color="#6d8299", line_width=1,
                         name=f"body_{key}")
        show_step(plotter, stage, 0)
        if place.on_chip and key in chips:
            case = stage.case
            marks = case.ports if case else []
            for role in sorted({p.role for p in marks}) or []:
                pts = np.asarray([[place.x_mm + p.x_mm, place.y_mm + p.y_mm, 0.4 * Z]
                                  for p in marks if p.role == role], dtype=float)
                if len(pts):
                    plotter.add_points(pts, color=R.PORT_COLOURS.get(role, "#8fd6ff"),
                                       point_size=9, render_points_as_spheres=True,
                                       name=f"ports_{key}_{role}")
        label = place.name if len(place.name) < 46 else place.name[:44] + "..."
        text = [label]
        if place.badge:
            text.append(place.badge)
        if place.printed:
            text.append(R.PRINTED_FOR_DEMO)
        if place.on_chip:
            text.append(stage.source)
        plotter.add_point_labels(
            np.asarray([[place.x_mm + place.w_mm / 2.0, place.y_mm - 2.2, 0.3 * Z]]),
            ["\n".join(text)], font_size=max(9, int(size[1] / 105)), text_color=R.INK,
            shape_color="#0b1016", shape_opacity=0.55, show_points=False,
            always_visible=True, name=f"name_{key}")

    for tube in layout.tubes:
        path = display_path(tube)
        poly = pv.lines_from_points(path).tube(radius=TUBE_BORE_MM / 2.0, n_sides=12)
        plotter.add_mesh(poly, color=TUBE_TBC_COLOUR if tube.tbc else TUBE_COLOUR,
                         opacity=0.55, name=f"tube_{tube.id}")
        mid = path[len(path) // 2]
        plotter.add_point_labels(
            np.asarray([mid + np.array([0.0, 0.0, 1.2])]),
            [tube.label() + ("  [TBC]" if tube.tbc else "")],
            font_size=max(7, int(size[1] / 135)),
            text_color=TUBE_TBC_COLOUR if tube.tbc else "#cfe6ff",
            shape_color="#0b1016", shape_opacity=0.5, show_points=False,
            always_visible=True, name=f"tubelabel_{tube.id}")


def _update_dynamic(plotter, layout: Layout, stages: dict[str, SceneStage], shot: dict,
                    tube: Tube | None, phase: float, size: tuple[int, int]) -> None:
    """The moving parts: tracers on every chip, the slug in the tube, and the caption."""
    pv = R.pyvista()
    w, h = size
    focus = shot.get("key")
    for key, stage in stages.items():
        # The chip the camera is on walks its protocol; the rest keep their first step.
        if key == focus and shot["kind"] == "stage" and stage.steps:
            show_step(plotter, stage, stage.index_at(phase))
        pts = _tracer_points(stage.steps[stage.shown] if stage.steps else None, phase)
        if pts is None or not len(pts):
            continue
        plotter.add_points(pts, color="#ffe08a", point_size=7,
                           render_points_as_spheres=True, name=f"tracers_{key}")
    if tube is not None:
        slug = _slug(tube, phase)
        plotter.add_mesh(pv.lines_from_points(slug).tube(radius=TUBE_BORE_MM * 0.42,
                                                         n_sides=10),
                         color="#ffe08a", name="slug")
    else:
        plotter.remove_actor("slug", render=False)

    key = shot.get("key")
    place = layout.stages.get(key) if key else None
    stage = stages.get(key) if key else None
    if shot["kind"] in ("opening", "closing") or place is None:
        lines = [f"{layout.id}  ·  {len(layout.chips())} chips, "
                 f"{len(layout.placeholders())} off-chip stages, "
                 f"{len(layout.tubes)} handoffs",
                 f"chain order read from {_rel(Path(layout.source))}",
                 "every field below is simulated; no chip in this chain has been built"]
    else:
        lines = caption_for(place, shown_case(stage),
                            phase="handoff" if shot["kind"] == "handoff" else "stage",
                            tube=tube)
    plotter.add_text("\n".join(lines), position=(int(w / 70), int(h / 38)),
                     font_size=max(9, int(h / 95)), color="#e6eef6", name="caption")

    badges = [R.SIMULATED]
    if place is not None and place.on_chip and place.w_mm <= 30.5 and place.h_mm <= 20.5:
        badges.append(R.DEMO_SCALE)
    if place is not None and place.printed:
        badges.append(R.PRINTED_FOR_DEMO)
    if stage is not None and stage.source and place is not None and place.on_chip:
        badges.append(stage.source)
    plotter.add_text("   ".join(badges), position=(int(w / 70), h - int(h / 16)),
                     font_size=max(10, int(h / 72)), color=R.WARN, name="badges")
    plotter.add_text(f"z exaggerated x{R.Z_EXAGGERATION:g}",
                     position=(int(w * 0.80), int(h / 38)),
                     font_size=max(7, int(h / 140)), color="#9fb0c0", name="zscale")


# --- the web export ------------------------------------------------------------------


def export_glb(layout: Layout, stages: dict[str, SceneStage], path: Path | str) -> Path:
    """A decimated, coloured surface plus streamlines, for the website's WebGL viewer."""
    pv = R.pyvista()
    parts: list[R.GlbPart] = []
    for key in layout.order:
        stage = stages.get(key)
        if stage is None:
            continue
        place = stage.place
        body = R.part_from_mesh(
            stage.body, f"{key}_body",
            base_color=(0.54, 0.65, 0.74, 1.0) if place.on_chip else (0.42, 0.37, 0.27, 1.0),
            opacity=0.18 if place.on_chip else 0.55)
        if body is not None:
            parts.append(body)
        view = stage.steps[0] if stage.steps else None
        if view is None:
            continue
        if view.slice_mid is not None and view.slice_mid.n_points:
            part = R.part_from_mesh(view.slice_mid, f"{key}_field", scalars="speed_mm_s",
                                    cmap=R.SPEED_CMAP, clim=view.speed_clim,
                                    decimate=0.88)
            if part is not None:
                parts.append(part)
        if view.streams is not None and view.streams.n_points:
            part = R.part_from_mesh(view.streams, f"{key}_streamlines",
                                    scalars="speed_mm_s", cmap=R.SPEED_CMAP,
                                    clim=view.speed_clim, decimate=0.75)
            if part is not None:
                parts.append(part)
    for tube in layout.tubes:
        poly = pv.lines_from_points(display_path(tube, 40)).tube(
            radius=TUBE_BORE_MM / 2.0, n_sides=8)
        colour = (1.0, 0.70, 0.28, 1.0) if tube.tbc else (0.60, 0.82, 1.0, 1.0)
        part = R.part_from_mesh(poly, f"tube_{tube.id}", base_color=colour, opacity=0.8)
        if part is not None:
            parts.append(part)
    return R.write_glb(parts, path, extras={
        "simulated": True,
        "pipeline": layout.id,
        "source": _rel(Path(layout.source)),
        "units": "metres (chip coordinates are mm, scaled by 1e-3)",
        "z_exaggeration": Z,
        "z_note": f"z is stretched x{Z:g} in the GEOMETRY so a 0.3 mm fluidic layer is "
                  f"visible under a 30 mm footprint; divide z by {Z:g} for true scale",
        "note": f"{R.SIMULATED}. Every 30 x 20 mm stage is {R.DEMO_SCALE}. Fields are "
                f"labelled per stage in pipeline_e2e.labels.json.",
    })


def write_labels(layout: Layout, stages: dict[str, SceneStage], path: Path | str) -> Path:
    """Label text and world positions (metres), so the viewer draws captions as HTML.

    Text baked into a mesh cannot be read at an angle and cannot be translated; the GLB
    therefore carries geometry only and this file carries every word that must appear.
    """
    out = {
        "simulated": R.SIMULATED,
        "pipeline": layout.id,
        "source": _rel(Path(layout.source)),
        "scale": 1e-3,
        "z_exaggeration": Z,
        "stages": [],
        "tubes": [],
    }
    for key in layout.order:
        stage = stages.get(key)
        if stage is None:
            continue
        place = stage.place
        cx, cy = place.center
        case = stage.case
        out["stages"].append({
            "key": key, "name": place.name, "badge": place.badge,
            "kind": place.kind, "source": stage.source,
            "printed_for_demo": bool(place.printed), "printed_slot": place.printed_slot,
            "as_built_note": R.AS_BUILT_NOTE if place.printed else "",
            "position_m": [cx * 1e-3, (place.y_mm - 2.0) * 1e-3, 0.0],
            "demo_scale": bool(place.on_chip and place.w_mm <= 30.5 and place.h_mm <= 20.5),
            "caption": caption_for(place, case),
        })
    for tube in layout.tubes:
        mid = display_path(tube, 9)[4]
        out["tubes"].append({
            "id": tube.id, "label": tube.label(), "tbc": bool(tube.tbc),
            "carries": tube.carries,
            "position_m": [float(mid[0]) * 1e-3, float(mid[1]) * 1e-3, float(mid[2]) * 1e-3],
        })
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """`python -m assay2chip.foam.scene protocols/pipeline_mvp.json`."""
    import argparse

    ap = argparse.ArgumentParser(description="render the whole pipeline on one bench")
    ap.add_argument("pipeline", nargs="?", default="protocols/pipeline_mvp.json")
    ap.add_argument("--out", default=None)
    ap.add_argument("--resolution", default="1080p")
    ap.add_argument("--cells-budget", type=int, default=320_000)
    ap.add_argument("--layout-only", action="store_true",
                    help="print the bench layout as JSON and stop (no VTK needed)")
    args = ap.parse_args(list(argv) if argv is not None else None)
    if args.layout_only:
        layout, _ = layout_from_file(args.pipeline)
        print(json.dumps(layout.to_json(), indent=2))
        return 0
    out = Path(args.out) if args.out else ROOT / "assay2chip/work/pipeline_e2e/foam/render"
    man = render_pipeline(args.pipeline, out_dir=out, resolution=args.resolution,
                          cells_budget=args.cells_budget)
    print(json.dumps({k: v for k, v in man.items() if k != "layout"}, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

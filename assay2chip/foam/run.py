"""CLI for the foam package: mesh -> case -> solve -> post -> render, for a chip or a chain.

`assay2chip/foam/README.md` is authoritative; this is its `run.py` row.

    python -m assay2chip.foam.run --chip chip/module1_v1.json [--protocol ...] [--step N]
    python -m assay2chip.foam.run --pipeline protocols/pipeline_mvp.json
    python -m assay2chip.foam.run --pipeline protocols/pipeline_mvp.json --render-only
    ... [--solve] [--resolution 1080p] [--cells-budget 1400000] [--out DIR] [--run NAME]

IT ORCHESTRATES, IT DOES NOT SOLVE. Every physics step belongs to a sibling module and is
called through `stage()`, which looks the module up at run time:

    mesh.py     structured hex polyMesh      F2
    case.py     system / constant / 0        F2
    runner.py   launches OpenFOAM            F1
    flow.py     simpleFoam / pimpleFoam      F3
    transport.py, particles.py, vof.py       the 3D post-processing
    render.py, scene.py                      this agent

A module that is not written yet is REPORTED, not faked: the stage is marked
`not written yet` in the manifest and the run carries on to the renderer, which falls back
to the reduced-order field from `assay2chip/cfd` and labels every frame
`reduced-order (2.5D)`. That is the whole point of the fallback - the pipeline always
produces an honest picture, and the picture says which model drew it.

LAUNCHING OPENFOAM IS OPT-IN (`--solve`), AND THAT IS A DECISION, NOT A GAP. A simpleFoam
run is minutes per chip per step, and the renderer does not need one to produce an honest
picture: without `--solve` the stages that would start a solver are reported `skipped`
with that reason, the mesh is still written, and every frame is drawn from the
reduced-order field and says `reduced-order (2.5D)` on its face. `--render-only` goes
further and skips the mesh too.

WHERE THINGS GO, AND THERE IS ONLY ONE ANSWER. The case directory is
`<out>/<chip_id>/step_<N>/` with the mesh inside it at `constant/polyMesh`, `<out>`
defaulting to `assay2chip/work/<run>/foam`. It comes from ONE helper -
`render.case_dir()`, which defers to `assay2chip/foam/case.py` - and nothing here builds
that path itself. A job has already failed because the mesh and the case were written to
two different folders (`foam/<chip>/1/` against `foam/foam/<chip>/step_1/`), so there is
no second convention and no extra `foam/` level. Images, video and glTF go under
`<out>/render/`.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from dataclasses import dataclass, field as dc_field
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from assay2chip.foam import render as R  # noqa: E402

DEFAULT_RUN = "pipeline_e2e"

#: The sibling modules, in the order foam/README.md lists them, with the entry points each
#: is expected to publish. The first name that exists is called; when none does, the stage
#: is recorded as "not written yet" and the run goes on.
#: `main` is deliberately NOT a candidate anywhere: calling a sibling's CLI entry point
#: with guessed arguments is how one agent breaks another's module.
SOLVER_STAGES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("mesh", ("write_mesh", "build_mesh", "mesh_chip", "hex_mesh"),
     "structured hex polyMesh under case/constant/polyMesh"),
    ("case", ("write_case", "build_case", "make_case"),
     "case/system, case/constant and case/0 from the protocol step"),
    ("runner", ("run_solver", "run_case", "run"),
     "launches the OpenFOAM solver and collects its log"),
    ("flow", ("run_step", "solve_chip", "solve_steps", "solve"),
     "p, U, wall shear and patch flows, plus the cross-check against assay2chip/cfd"),
    ("transport", ("solve_transport", "washout", "mean_age"),
     "3D washout and mean age"),
    ("particles", ("track_parcels", "track"), "Lagrangian parcels with settling"),
)

#: Stages that LAUNCH OpenFOAM. They are skipped unless `--solve` is given, because a
#: simpleFoam run is minutes per chip per step and the renderer does not need one to
#: produce an honest picture - it falls back to the reduced-order field and says so. This
#: is a deliberate default, not a missing feature: `--solve` turns them on.
SOLVING_STAGES = ("runner", "flow", "transport", "particles")


@dataclass
class StageReport:
    """What one orchestrated stage did, or honestly why it did nothing."""

    name: str
    status: str                 # ok | skipped | not written yet | errored
    detail: str = ""
    entry_point: str | None = None
    seconds: float = 0.0

    def to_json(self) -> dict:
        return {"stage": self.name, "status": self.status, "detail": self.detail,
                "entry_point": self.entry_point, "seconds": round(self.seconds, 3)}


def sibling(name: str):
    """A module of `assay2chip.foam` if it exists yet, else None. Never raises."""
    try:
        return importlib.import_module(f"assay2chip.foam.{name}")
    except ModuleNotFoundError:
        return None
    except Exception:
        return None


def entry_point(module, names: Sequence[str]) -> tuple[str | None, Callable | None]:
    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            return name, fn
    return None, None


def first_flowing_record(chip_path: Path, protocol_path: Path | None, step: Any = None):
    """The ScenarioRecord `flow.run_step()` wants: a step that actually moves liquid.

    Built through `assay2chip.cfd.scenario`, which is the one place that turns a protocol
    step into boundary conditions - the 3D solver and the reduced one must be given the
    SAME step, or the cross-check between them compares two different experiments.
    """
    if protocol_path is None:
        return None
    try:
        from assay2chip.cfd import scenario as cfd_scenario
        run = cfd_scenario.build_protocol(protocol_path, chip_path, worst_cases=False)
    except Exception:
        return None
    flowing = [r for r in run.scenarios
               if r.kind == "step" and r.q_in_ul_min > 0 and r.mappable]
    if step is not None:
        want = [r for r in flowing if str(r.step) == str(step)]
        if want:
            return want[0]
    return flowing[0] if flowing else None


def solve_chip(chip_path: Path, protocol_path: Path | None, foam_dir: Path, chip_id: str,
               *, step: Any = None, cells_budget: int = R.DEFAULT_CELLS_BUDGET,
               solve: bool = False, log=print) -> list[StageReport]:
    """Run the solver chain for one chip into THE case directory for its step.

    One convention, one helper: `render.case_dir(chip_id, step, out=foam_dir)` gives
    `<out>/<chip_id>/step_<N>/` and the mesh goes inside it at `constant/polyMesh`. The
    step is the chip's first flowing one, because a case directory named for no step is
    how the mesh and the case ended up in two different folders.
    """
    record = first_flowing_record(chip_path, protocol_path, step)
    use_step = step if step is not None else getattr(record, "step", None)
    if use_step is None:
        return [StageReport(name, "skipped",
                            "no protocol step on this chip moves liquid, so there is no "
                            "case to write")
                for name, _, _ in SOLVER_STAGES]
    case = R.case_dir(chip_id, use_step, out=foam_dir)
    case.mkdir(parents=True, exist_ok=True)
    log(f"    case {case.relative_to(ROOT) if case.is_absolute() else case}")
    return solver_stages(chip_path, protocol_path, case, step=use_step,
                         cells_budget=cells_budget, solve=solve, log=log)


def solver_stages(chip_path: Path, protocol_path: Path | None, out_dir: Path,
                  step: Any = None, cells_budget: int = R.DEFAULT_CELLS_BUDGET,
                  solve: bool = False, log=print) -> list[StageReport]:
    """Walk the OpenFOAM chain through the siblings' modules, reporting each.

    The call signature a sibling wants is its own, so the arguments are matched against
    the function's declared parameters (`inspect.signature`) rather than sprayed at it:
    a module gets exactly the names it asked for, and anything it needs that is not on
    offer is reported as a mismatch instead of being guessed at.
    """
    import inspect

    record = first_flowing_record(chip_path, protocol_path, step) if solve else None
    reports: list[StageReport] = []
    for name, names, what in SOLVER_STAGES:
        if name in SOLVING_STAGES and not solve:
            reports.append(StageReport(
                name, "skipped",
                f"{what} - launching OpenFOAM is minutes per chip and the renderer does "
                f"not need it to be honest; pass --solve to run it"))
            continue
        module = sibling(name)
        if module is None:
            reports.append(StageReport(name, "not written yet",
                                       f"assay2chip/foam/{name}.py does not exist yet - "
                                       f"{what}"))
            continue
        found, fn = entry_point(module, names)
        if fn is None:
            reports.append(StageReport(
                name, "no entry point",
                f"assay2chip/foam/{name}.py exists but exposes none of "
                f"{', '.join(names)}: rename one, or tell the orchestrator which to call"))
            continue
        on_offer = {
            "chip": chip_path, "chip_path": chip_path, "path": chip_path,
            "protocol": protocol_path, "protocol_path": protocol_path,
            "out_dir": out_dir, "case_dir": out_dir, "case": out_dir, "out": out_dir,
            "step": step, "cells_budget": cells_budget, "record": record,
            # `work_dir` means `<out>` - the foam directory the case tree lives under -
            # which is this case directory's grandparent (<out>/<chip_id>/step_<N>).
            "work_dir": out_dir.parent.parent,
        }
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = {}
        kwargs = {k: v for k, v in on_offer.items() if k in params}
        missing = [n for n, p in params.items()
                   if p.default is inspect.Parameter.empty
                   and p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
                   and n not in kwargs]
        if missing:
            reports.append(StageReport(
                name, "signature mismatch",
                f"{found}() needs {', '.join(missing)}, which the orchestrator does not "
                f"have; it can supply {', '.join(sorted(on_offer))}", found))
            log(f"    {name}: {found}() needs {', '.join(missing)}")
            continue
        started = time.time()
        try:
            fn(**kwargs)
        except Exception as exc:
            reports.append(StageReport(name, "errored",
                                       f"{found}() raised {type(exc).__name__}: {exc}",
                                       found, time.time() - started))
            log(f"    {name}: {type(exc).__name__}: {exc}")
            continue
        reports.append(StageReport(name, "ok", what, found, time.time() - started))
        log(f"    {name}: ok via {found}()")
    return reports


def findings_for(run_dir: Path) -> list[dict]:
    """Findings the rest of the repo already wrote for this run, if any.

    Both shapes are read: `analyse.py`'s `findings.json` and whatever F3's foam summary
    carries. A render with no findings file still gets a headline, because `render.py`
    measures one off the field itself.
    """
    out: list[dict] = []
    for name in ("cfd/findings.json", "foam/findings.json", "findings.json"):
        path = run_dir / name
        if not path.is_file():
            continue
        try:
            doc = R.load_json(path)
        except Exception:
            continue
        rows = doc.get("findings") if isinstance(doc, dict) else doc
        if isinstance(rows, list):
            out.extend(r for r in rows if isinstance(r, dict))
    return out


def protocol_for(chip_path: Path) -> Path | None:
    """The protocol a chip is paired with, through the cfd scenario module's own rule."""
    try:
        from assay2chip.cfd import scenario as cfd_scenario
        chip = R.load_json(chip_path)
        chip_id = str(chip.get("id") or chip_path.stem)
        for proto in sorted((ROOT / "protocols").glob("*.json")):
            doc = R.load_json(proto)
            if doc.get("kind") == "pipeline":
                continue
            guess, _ = cfd_scenario.chip_for_protocol(doc, {chip_id: chip})
            if guess == chip_id:
                return proto
    except Exception:
        pass
    direct = ROOT / "protocols" / f"{chip_path.stem}.json"
    return direct if direct.exists() else None


def run_chip(chip_path: Path | str, protocol_path: Path | str | None = None, *,
             run: str = "foam_chip", out_dir: Path | str | None = None,
             step: Any = None, resolution: Any = "1080p",
             cells_budget: int = R.DEFAULT_CELLS_BUDGET, render_only: bool = False,
             solve: bool = False, video: bool = True,
             max_steps: int = R.DEFAULT_MAX_STEPS, log=print) -> dict:
    """One chip: solve where the siblings can, then render whatever exists."""
    chip_path = Path(chip_path) if Path(chip_path).is_absolute() else ROOT / chip_path
    chip = R.load_json(chip_path)
    chip_id = str(chip.get("id") or chip_path.stem)
    if protocol_path is None:
        protocol_path = protocol_for(chip_path)
    elif not Path(protocol_path).is_absolute():
        protocol_path = ROOT / protocol_path
    run_dir = Path(out_dir) if out_dir else ROOT / "assay2chip/work" / run
    foam_dir = R.foam_out_dir(run_dir)
    render_dir = foam_dir / "render"
    render_dir.mkdir(parents=True, exist_ok=True)

    log(f"  {chip_id}  (protocol: "
        f"{Path(protocol_path).name if protocol_path else 'none found'})")
    stages: list[StageReport] = []
    if render_only:
        stages = [StageReport(name, "skipped", "--render-only")
                  for name, _, _ in SOLVER_STAGES]
    else:
        stages = solve_chip(chip_path, protocol_path, foam_dir, chip_id, step=step,
                            cells_budget=cells_budget, solve=solve, log=log)

    manifest = R.render_chip(
        chip_path, protocol_path, out_dir=render_dir, run_dir=foam_dir, step=step,
        resolution=resolution, cells_budget=cells_budget, max_steps=max_steps,
        video=video, findings=findings_for(run_dir), log=log)
    manifest["solver_stages"] = [s.to_json() for s in stages]
    manifest["run"] = run
    manifest["out_dir"] = render_dir.as_posix()
    out_json = render_dir / f"{chip_id}.render.json"
    out_json.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def run_pipeline(pipeline_path: Path | str, *, run: str = DEFAULT_RUN,
                 out_dir: Path | str | None = None, resolution: Any = "1080p",
                 cells_budget: int = 320_000, render_only: bool = False,
                 solve: bool = False, per_chip: bool = False, video: bool = True,
                 glb: bool = True, steps_per_stage: int = 3, log=print) -> dict:
    """The whole chain: every stage, in chain order, on one bench.

    `--pipeline` is the headline case. The chain file is the only source of what is in the
    pipeline and what order it runs in, so the corrected pipeline (barcoding back between
    Module 3 and Module 4, and Module 6) needs no change here.
    """
    from assay2chip.foam import scene as S

    pipeline_path = (Path(pipeline_path) if Path(pipeline_path).is_absolute()
                     else ROOT / pipeline_path)
    run_dir = Path(out_dir) if out_dir else ROOT / "assay2chip/work" / run
    foam_dir = R.foam_out_dir(run_dir)
    render_dir = foam_dir / "render"
    render_dir.mkdir(parents=True, exist_ok=True)
    findings = findings_for(run_dir)

    layout, _ = S.layout_from_file(pipeline_path)
    log(f"chain {layout.id}: {len(layout.chips())} chips, "
        f"{len(layout.placeholders())} off-chip stages, {len(layout.tubes)} handoffs")

    solver: dict[str, list[dict]] = {}
    if not render_only:
        for place in layout.chips():
            chip_path = ROOT / place.chip_path
            chip_id = str(R.load_json(chip_path).get("id") or chip_path.stem)
            log(f"  solver stages for {place.key}")
            solver[place.key] = [
                s.to_json() for s in solve_chip(
                    chip_path,
                    (ROOT / place.protocol_path) if place.protocol_path else None,
                    foam_dir, chip_id, cells_budget=cells_budget, solve=solve, log=log)
            ]

    per_chip_manifests: list[dict] = []
    if per_chip:
        for place in layout.chips():
            per_chip_manifests.append(R.render_chip(
                ROOT / place.chip_path,
                (ROOT / place.protocol_path) if place.protocol_path else None,
                out_dir=render_dir / "chips", run_dir=foam_dir, resolution=resolution,
                cells_budget=cells_budget, video=video, findings=findings, log=log))

    manifest = S.render_pipeline(
        pipeline_path, out_dir=render_dir, run_dir=foam_dir, resolution=resolution,
        cells_budget=cells_budget, steps_per_stage=steps_per_stage, video=video, glb=glb,
        findings=findings, log=log)
    manifest["run"] = run
    manifest["render_only"] = bool(render_only)
    manifest["solver_stages"] = solver
    if per_chip_manifests:
        manifest["per_chip"] = per_chip_manifests
    (render_dir / "pipeline_e2e.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest


def summarise(manifest: dict) -> str:
    """The three facts a reader wants: where it is, how long it is, and from which model."""
    lines: list[str] = []
    if "layout" in manifest:
        lines.append(f"pipeline: {manifest.get('pipeline')}")
        lines.append(f"video:    {manifest.get('video')} "
                     f"({manifest.get('video_seconds')} s, "
                     f"{'x'.join(str(v) for v in manifest.get('resolution', []))})")
        lines.append(f"glb:      {manifest.get('glb')} "
                     f"({(manifest.get('glb_bytes') or 0) // 1024} kB)")
        lines.append(f"keyframes: {len(manifest.get('keyframes', []))}")
        lines.append("stages:")
        for s in manifest.get("stages", []):
            mark = s.get("source") or "-"
            lines.append(f"  {s['key']:<13} {s['kind']:<11} {s.get('badge') or '':<24} "
                         f"{mark}")
    else:
        lines.append(f"chip:     {manifest.get('chip_id')}")
        lines.append(f"source:   {manifest.get('source')}")
        lines.append(f"video:    {manifest.get('video')} "
                     f"({manifest.get('video_seconds')} s)")
        lines.append(f"keyframes: {len(manifest.get('keyframes', []))}")
    for note in manifest.get("notes", []):
        lines.append(f"note: {note}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m assay2chip.foam.run",
        description="3D CFD and rendering for one chip or the whole pipeline")
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument("--chip", help="chip/<id>.json")
    what.add_argument("--pipeline", help="protocols/pipeline_mvp.json or a partition.json")
    ap.add_argument("--protocol", help="protocols/<id>.json (default: the chip's own)")
    ap.add_argument("--step", help="one protocol step number")
    ap.add_argument("--render-only", action="store_true",
                    help="skip the solver stages and draw the fields that already exist")
    ap.add_argument("--solve", action="store_true",
                    help="actually launch OpenFOAM (minutes per chip per step). Without "
                         "it the renderer falls back to the reduced-order field and "
                         "labels every frame 'reduced-order (2.5D)'")
    ap.add_argument("--resolution", default="1080p", help="1080p, 720p or WIDTHxHEIGHT")
    ap.add_argument("--cells-budget", type=int, default=None,
                    help="grid points in the render volume (default 1.4M per chip, "
                         "320k per stage of the pipeline scene)")
    ap.add_argument("--max-steps", type=int, default=R.DEFAULT_MAX_STEPS,
                    help="protocol steps to render per chip")
    ap.add_argument("--steps-per-stage", type=int, default=3,
                    help="protocol steps each chip walks in the pipeline animation")
    ap.add_argument("--per-chip", action="store_true",
                    help="also render every chip of the pipeline on its own")
    ap.add_argument("--no-video", action="store_true")
    ap.add_argument("--no-glb", action="store_true")
    ap.add_argument("--run", default=None, help="work directory name under assay2chip/work")
    ap.add_argument("--out", default=None, help="write somewhere else entirely")
    ap.add_argument("--json", action="store_true", help="print the manifest as JSON")
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if not R.have_pyvista():
        print("PyVista is not installed: ./.venv/bin/python -m pip install "
              "-r requirements-foam.txt", file=sys.stderr)
        return 2
    if args.pipeline:
        manifest = run_pipeline(
            args.pipeline, run=args.run or DEFAULT_RUN, out_dir=args.out,
            resolution=args.resolution, cells_budget=args.cells_budget or 320_000,
            render_only=args.render_only, solve=args.solve, per_chip=args.per_chip,
            video=not args.no_video, glb=not args.no_glb,
            steps_per_stage=args.steps_per_stage)
    else:
        manifest = run_chip(
            args.chip, args.protocol, run=args.run or "foam_chip", out_dir=args.out,
            step=args.step, resolution=args.resolution,
            cells_budget=args.cells_budget or R.DEFAULT_CELLS_BUDGET,
            render_only=args.render_only, solve=args.solve,
            video=not args.no_video, max_steps=args.max_steps)
    print(json.dumps(manifest, indent=2) if args.json else summarise(manifest))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

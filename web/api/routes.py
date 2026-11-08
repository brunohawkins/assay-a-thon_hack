"""The routes from web/README.md, which is authoritative. Nothing here is renamed.

| GET  /api/health              | POST /api/runs                 | GET /api/chips      |
| GET  /api/stages              | GET  /api/runs/<id>            | GET /api/chips/<id> |
| GET  /api/runs                | GET  /api/runs/<id>/events     | POST /api/cfd       |
| GET  /api/runs/<id>/artifacts | GET  /api/runs/<id>/file       |
| GET  /api/runs/<id>/report    | POST /api/runs/<id>/rerun      |

Every handler takes a Request and returns a Response. Errors are raised as ApiError and
become `{"error": ..., "detail": ...}` with a real status: 400 for a bad request, 404 for
something that is not there, 409 for a run already in flight, 413 for an oversized
upload, 503 for a stage script this checkout does not have yet.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import List, Optional

from . import chips, jobs, llm, manifest, paths, sandbox, stages
from .http import (ApiError, Request, Response, Route, SSE_HEADERS, file_response,
                   json_response, parse_multipart)

VERSION = "0.1.0"
SCALES = ("auto", "demo", "target")
#: Seconds between SSE heartbeat comments. Comfortably under the 60 s a proxy usually
#: allows an idle connection, so a run that sits in a slow stage is not dropped.
HEARTBEAT_SECONDS = 15.0


# --- health and stages -------------------------------------------------------

def health(req: Request) -> Response:
    rows = stages.listing()
    mode = llm.report()
    run_script = paths.RUN_SCRIPT.is_file()
    cfd_script = paths.CFD_SCRIPT.is_file()
    return json_response({
        "ok": True,
        "version": VERSION,
        "stages": [row["label"] for row in rows],
        "llm": mode["mode"],
        "llm_detail": mode,
        "stage_count": len(rows),
        "stages_implemented": sum(1 for row in rows if row["implemented"]),
        "scripts": {
            "run": {"path": paths.repo_rel(paths.RUN_SCRIPT), "available": run_script},
            "cfd": {"path": paths.repo_rel(paths.CFD_SCRIPT), "available": cfd_script},
        },
        "queue": req.app.manager.queue_state(),
        "runs": len(manifest.known_run_ids()),
        "chips": len(chips.listing()),
        "root": str(paths.ROOT),
        "static": {"path": paths.repo_rel(paths.STATIC), "available": paths.STATIC.is_dir()},
        "docs": {"path": "docs", "route": "/docs/<path>",
                 "available": (paths.ROOT / "docs").is_dir()},
        "twin": {"path": "twin", "route": "/twin/",
                 "available": (paths.ROOT / "twin" / "index.html").is_file(),
                 "files": len(sandbox.twin_files()),
                 "allowlist": sandbox.twin_source()},
        "python": jobs.python_executable(),
    })


def stage_list(req: Request) -> Response:
    return json_response(stages.listing())


# --- runs --------------------------------------------------------------------

def list_runs(req: Request) -> Response:
    rows = []
    seen = set()
    for job in req.app.manager.all():
        rows.append(_run_row(job.run_id, job))
        seen.add(job.run_id)
    for run_id in manifest.known_run_ids():
        if run_id not in seen:
            rows.append(_run_row(run_id, None))
    return json_response(rows)


def _run_row(run_id: str, job) -> dict:
    data = manifest.load(run_id)
    view = job.view if (job is not None and job.view is not None) else manifest.view_for(
        run_id, data)
    rows = view.list() if view is not None else (job.rows() if job else [])
    findings = job.findings if job is not None and job.findings else manifest.findings(run_id)
    status = job.status if job is not None else manifest.run_status(data, rows)
    mode = job.llm if job is not None else _manifest_llm(data)
    return {
        "run_id": run_id,
        "title": job.title if job is not None else manifest.title(run_id, data),
        "created": (job.created if job is not None else None) or manifest.created(run_id, data),
        "status": status,
        "chips": manifest.chip_ids(run_id, data),
        "findings_by_severity": manifest.findings_by_severity(run_id, findings),
        "kind": job.kind if job is not None else "run",
        "llm": mode.get("mode"),
        "llm_detail": mode,
        "stage_counts": _counts(rows),
        "queued_behind": job.queued_behind if job is not None else 0,
        "has_manifest": data is not None,
        "has_report": (manifest.work_dir(run_id) / paths.REPORT_MD).is_file(),
    }


def _counts(rows) -> dict:
    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return counts


def _manifest_llm(data: Optional[dict]) -> dict:
    """What the manifest says the model was. `stub` is never dressed up as real."""
    block = (data or {}).get("llm") or {}
    mode = block.get("mode") if block.get("mode") in (llm.STUB, llm.REAL) else None
    if mode is None:
        return {"mode": llm.STUB, "note": "no manifest yet, so nothing has been extracted",
                "requested": None}
    out = {"mode": mode, "requested": mode, "model": block.get("model"),
           "reported_by": paths.MANIFEST_NAME}
    if mode == llm.STUB:
        out["note"] = llm.STUB_NOTE
    return out


def get_run(req: Request) -> Response:
    run_id = _run_id(req)
    job = req.app.manager.get(run_id)
    data = manifest.load(run_id)
    if job is None and data is None and not manifest.work_dir(run_id).is_dir():
        raise ApiError(404, "unknown_run",
                       "no run called %r: nothing in %s" % (run_id, paths.repo_rel(paths.WORK)))
    if job is not None:
        body = job.summary()
    else:
        view = manifest.view_for(run_id, data)
        rows = view.list()
        mode = _manifest_llm(data)
        body = {
            "run_id": run_id,
            "job_id": None,
            "kind": "run",
            "title": manifest.title(run_id, data),
            "created": manifest.created(run_id, data),
            "started": (data or {}).get("created_at"),
            "finished": (data or {}).get("finished_at"),
            "status": manifest.run_status(data, rows),
            "returncode": None,
            "llm": mode.get("mode"),
            "llm_detail": mode,
            "scale": None,
            "command": (data or {}).get("command"),
            "stages": rows,
            "stage_counts": _counts(rows),
            "findings_by_severity": manifest.findings_by_severity(run_id),
            "findings_total": len(manifest.findings(run_id)),
            "chips": manifest.chip_ids(run_id, data),
            "message": "" if data else (
                "this run has no %s: either it has not run yet or it never got that far"
                % paths.MANIFEST_NAME),
        }
    body["manifest"] = {
        "path": paths.repo_rel(manifest.manifest_path(run_id)),
        "exists": data is not None,
    }
    body["reruns"] = (data or {}).get("reruns") or []
    body["rerun_cap_reached"] = bool((data or {}).get("rerun_cap_reached"))
    body["summary"] = (data or {}).get("summary") or {}
    body["missing_contract_keys"] = (data or {}).get("missing_contract_keys") or []
    body["events"] = "/api/runs/%s/events" % run_id
    return json_response(body)


def artifacts(req: Request) -> Response:
    run_id = _run_id(req)
    _require_run(req, run_id)
    return json_response(manifest.artifacts(run_id))


def run_file(req: Request) -> Response:
    run_id = _run_id(req, allow_repo=True)
    if run_id != paths.REPO_SCOPE:
        _require_run(req, run_id)
    path = _resolved(lambda: sandbox.resolve(run_id, req.get("path")))
    download = str(req.get("download", "")).lower() in ("1", "true", "yes")
    return file_response(path, download=download, range_header=req.headers.get("Range"))


def _resolved(resolve):
    """Run a sandbox resolver and turn a refusal into the right status.

    403 for "the sandbox does not allow that", 404 for "it would be allowed but there is
    no such file". The two are kept apart because they mean different things to whoever
    is debugging, and neither leaks anything about what is on disk outside the sandbox.
    """
    try:
        return resolve()
    except sandbox.Denied as denied:
        not_found = str(denied.message).startswith("not found")
        raise ApiError(404 if not_found else 403,
                       "not_found" if not_found else "forbidden_path",
                       denied.message, hint=denied.detail) from None


def report(req: Request) -> Response:
    run_id = _run_id(req)
    _require_run(req, run_id)
    directory = manifest.work_dir(run_id)
    markdown_path = directory / paths.REPORT_MD
    json_path = directory / paths.REPORT_JSON
    markdown = None
    if markdown_path.is_file():
        try:
            markdown = markdown_path.read_text(encoding="utf-8", errors="replace")
        except OSError as problem:
            raise ApiError(500, "unreadable", str(problem)) from None
    report_json = manifest.read_json(json_path) if json_path.is_file() else None
    if markdown is None and report_json is None:
        row = _stage_row(run_id, "s12")
        status = row.get("status") if row else "pending"
        raise ApiError(
            404, "no_report",
            "S12 report has not produced %s for run %r (its status is %s)"
            % (paths.REPORT_MD, run_id, status),
            stage="s12", stage_status=status)
    return json_response({
        "run_id": run_id,
        "markdown": markdown,
        "json": report_json,
        "paths": {
            "markdown": paths.repo_rel(markdown_path) if markdown is not None else None,
            "json": paths.repo_rel(json_path) if report_json is not None else None,
        },
    })


def _stage_row(run_id: str, token: str) -> dict:
    for row in manifest.view_for(run_id).list():
        if row["stage"] == token:
            return row
    return {}


# --- starting a run ----------------------------------------------------------

def create_run(req: Request) -> Response:
    fields, upload = _read_submission(req)
    options = fields.get("options") or {}
    run_id = _new_run_id(fields, upload)
    from_value = _stage_value(options.get("from"), "from")
    to_value = _stage_value(options.get("to"), "to")
    scale = _scale_value(options.get("scale"))
    mode = _llm_choice(options.get("llm"))
    plan = [row["stage"] for row in stages.plan(from_value, to_value)]
    if not plan:
        raise ApiError(400, "empty_plan",
                       "--to %r comes before --from %r" % (to_value, from_value))

    script = req.app.manager.require_script(
        paths.RUN_SCRIPT, "a run cannot be started",
        "It is being written in parallel; GET /api/stages shows which stages exist.")

    source, source_kind = _store_source(run_id, fields, upload)

    argv = [jobs.python_executable(), str(script), "--id", run_id]
    if source:
        argv += ["--paper", source]
    if from_value:
        argv += ["--from", from_value]
    if to_value:
        argv += ["--to", to_value]
    argv += ["--llm", mode["mode"], "--verbose"]
    environment = {"ASSAY2CHIP_LLM": mode["mode"]}
    if scale != "auto":
        # run.py has no --scale flag yet (its CLI is --paper/--id/--from/--to/--llm), so
        # the choice is passed as an environment variable until it grows one. Said out
        # loud in the response rather than silently dropped.
        if _supports_flag(script, "--scale"):
            argv += ["--scale", scale]
        else:
            environment["ASSAY2CHIP_SCALE"] = scale

    job = jobs.Job(run_id, "run", argv, run_id=run_id, llm_mode=mode, scale=scale,
                   plan=plan, env=environment,
                   request={"source": source, "source_kind": source_kind,
                            "from": from_value, "to": to_value})
    req.app.manager.submit(job)
    body = {
        "run_id": run_id,
        "job_id": job.id,
        "status": job.status,
        "queued_behind": job.queued_behind,
        "llm": mode["mode"],
        "llm_detail": mode,
        "scale": scale,
        "scale_passed_as": "flag" if "--scale" in argv else (
            "ASSAY2CHIP_SCALE environment variable (run.py has no --scale flag yet)"
            if scale != "auto" else None),
        "source": source,
        "source_kind": source_kind,
        "plan": plan,
        "command": " ".join(argv),
        "events": "/api/runs/%s/events" % run_id,
        "message": job.message or "started",
    }
    return json_response(body, 202, [("Location", "/api/runs/%s" % run_id)])


def rerun(req: Request) -> Response:
    run_id = _run_id(req)
    if not manifest.work_dir(run_id).is_dir():
        raise ApiError(404, "unknown_run", "no run called %r to re-run" % run_id)
    body = req.json()
    from_value = _stage_value(body.get("from"), "from")
    if not from_value:
        raise ApiError(400, "missing_from",
                       "say which stage to re-run from, e.g. {\"from\": \"s6\"}")
    to_value = _stage_value(body.get("to"), "to")
    mode = _llm_choice(body.get("llm") or (body.get("options") or {}).get("llm"))
    plan = [row["stage"] for row in stages.plan(from_value, to_value)]
    script = req.app.manager.require_script(
        paths.RUN_SCRIPT, "the run cannot be re-run",
        "It is being written in parallel; GET /api/stages shows which stages exist.")
    argv = [jobs.python_executable(), str(script), "--id", run_id, "--from", from_value]
    if to_value:
        argv += ["--to", to_value]
    argv += ["--llm", mode["mode"], "--verbose"]
    job = jobs.Job(run_id, "rerun", argv, run_id=run_id, llm_mode=mode, plan=plan,
                   env={"ASSAY2CHIP_LLM": mode["mode"]},
                   request={"from": from_value, "to": to_value})
    req.app.manager.submit(job)
    return json_response({
        "run_id": run_id,
        "job_id": job.id,
        "status": job.status,
        "from": from_value,
        "to": to_value,
        "queued_behind": job.queued_behind,
        "llm": mode["mode"],
        "llm_detail": mode,
        "plan": plan,
        "command": " ".join(argv),
        "events": "/api/runs/%s/events" % run_id,
        "message": job.message or "re-running from %s" % from_value,
    }, 202, [("Location", "/api/runs/%s" % run_id)])


def cfd(req: Request) -> Response:
    body = req.json()
    pipeline = body.get("pipeline")
    chip = body.get("chip")
    if bool(pipeline) == bool(chip):
        raise ApiError(400, "bad_request",
                       'give exactly one of {"pipeline": "protocols/pipeline_mvp.json"} '
                       'or {"chip": "<chip_id>"}')
    script = req.app.manager.require_script(
        paths.CFD_SCRIPT, "CFD cannot run",
        "The CFD modules are being written in parallel (assay2chip/cfd/README.md); "
        "GET /api/health shows when the CLI lands.")

    if pipeline:
        try:
            target = sandbox.resolve(paths.REPO_SCOPE, pipeline)
        except sandbox.Denied as denied:
            raise ApiError(400, "bad_pipeline", denied.message, hint=denied.detail) from None
        if not paths.repo_rel(target).startswith("protocols/"):
            raise ApiError(400, "bad_pipeline",
                           "a pipeline must be a protocols/*.json file, not %s"
                           % paths.repo_rel(target))
        subject = Path(paths.repo_rel(target)).stem
        argument = ["--pipeline", paths.repo_rel(target)]
        request = {"pipeline": paths.repo_rel(target)}
    else:
        found = chips.find(str(chip))
        if found is None:
            raise ApiError(404, "unknown_chip",
                           "no chip called %r; GET /api/chips lists them" % chip)
        subject = found["row"]["id"]
        # The API takes a chip id; the CLI takes the spec's path (chip/<id>.json).
        argument = ["--chip", found["row"]["path"]]
        request = {"chip": subject, "chip_path": found["row"]["path"]}

    job_id = req.app.manager.cfd_job_id(subject)
    argv = [jobs.python_executable(), str(script)] + argument
    if _supports_flag(script, "--id"):
        # Gives the job its own work/<id>/cfd directory, which is what /file and the
        # finding events then read.
        argv += ["--id", job_id]
    job = jobs.Job(job_id, "cfd", argv, run_id=job_id, title="CFD: %s" % subject,
                   request=request,
                   # The solver is physics, not a model. Saying "stub" here would imply
                   # an extraction that never happens in CFD.
                   llm_mode={"mode": "none",
                             "note": "the CFD solver calls no language model"})
    req.app.manager.submit(job)
    return json_response({
        "job_id": job.id,
        "run_id": job.run_id,
        "status": job.status,
        "queued_behind": job.queued_behind,
        "subject": request,
        "command": " ".join(argv),
        "events": "/api/runs/%s/events" % job.id,
        "message": job.message or "started",
    }, 202, [("Location", "/api/runs/%s" % job.id)])


# --- chips -------------------------------------------------------------------

def list_chips(req: Request) -> Response:
    return json_response(chips.listing())


def get_chip(req: Request) -> Response:
    chip_id = str(req.params.get("id") or "")
    found = chips.find(chip_id)
    if found is None:
        raise ApiError(404, "unknown_chip",
                       "no chip called %r; GET /api/chips lists every chip in the repo"
                       % chip_id)
    spec = dict(found["spec"])
    spec["_meta"] = found["row"]
    return json_response(spec)


# --- documentation, media and the digital twin -------------------------------

def docs_file(req: Request) -> Response:
    """GET /docs/<path>: docs/, the 3D renders and the CFD figures. Sandboxed.

    The path in the URL is the repo-relative path, so `/docs/videos/manifest.json` is
    `docs/videos/manifest.json` and a render the manifest points at is requested at its
    own repo path. Range requests are honoured, which is what lets a <video> seek.
    """
    rel = str(req.params.get("path") or "")
    if req.path.startswith("/api/docs/") or req.path.startswith("/docs/"):
        rel = "docs/" + rel if not rel.startswith(("docs/", "assay2chip/")) else rel
    path = _resolved(lambda: sandbox.resolve_doc(rel))
    return file_response(path, range_header=req.headers.get("Range"))


def repo_media(req: Request) -> Response:
    """The render and CFD-figure paths, requested at their own repo path.

    docs/modules.json points at `assay2chip/work/<id>/foam/render/...` and
    `assay2chip/work/<id>/cfd/figures/...`; the front end also reaches those through
    /api/runs/<id>/file, and both go through the same allowlist.
    """
    path = _resolved(lambda: sandbox.resolve_doc(str(req.params.get("path") or "")))
    return file_response(path, range_header=req.headers.get("Range"))


def root_file(req: Request) -> Response:
    """A repo-root-relative URL that is not an API route and not a front-end file.

    Two things arrive here: the `../<path>` fetches the twin makes from /twin/, and the
    render or figure media a document points at. Which allowlist applies is decided
    before resolving, so the refusal names the right one.
    """
    rel = req.path.lstrip("/")
    if rel in sandbox.twin_files():
        return file_response(_resolved(lambda: sandbox.resolve_twin(rel)),
                             range_header=req.headers.get("Range"))
    if any(pattern.match(rel) for pattern, _, _ in sandbox.DOC_RULES):
        return file_response(_resolved(lambda: sandbox.resolve_doc(rel)),
                             range_header=req.headers.get("Range"))
    raise ApiError(404, "not_found",
                   "%s is not served: it is not a front-end file, not on the digital "
                   "twin's allowlist and not under docs/" % req.path,
                   hint="GET /api/health, /twin/ or /docs/<path>")


def twin_file(req: Request) -> Response:
    """GET /twin/ and /twin/<path>, plus the repo files the twin fetches by relative path.

    The twin is served as static files from `twin/`, and it reaches chip specs,
    protocols, runs, contracts and cad/ with `../<path>` - which arrives here as a
    root-relative URL. The allowlist is derived from the twin's own code, so exactly what
    it asks for is served and nothing else.
    """
    if req.path == "/twin":
        # Without the trailing slash the twin's own relative URLs (main.js, style.css,
        # ../runs/index.json) would resolve against the root and all miss.
        return Response(301, b"", [("Location", "/twin/"),
                                   ("Content-Type", "text/plain; charset=utf-8")])
    rel = str(req.params.get("path") or "").strip()
    if rel in ("", "/"):
        rel = "twin/index.html"
    elif req.path.startswith("/twin/"):
        rel = "twin/" + rel
    path = _resolved(lambda: sandbox.resolve_twin(rel))
    return file_response(path, range_header=req.headers.get("Range"))


# --- server-sent events ------------------------------------------------------

def events(req: Request) -> Response:
    """A real SSE stream: `log` per output line, `stage` on every status change,
    `finding` as CFD findings appear, `done` at the end, and a comment heartbeat so a
    proxy does not drop a connection during a slow stage."""
    run_id = _run_id(req)
    job = req.app.manager.get(run_id)
    if job is None:
        data = manifest.load(run_id)
        if data is None and not manifest.work_dir(run_id).is_dir():
            raise ApiError(404, "unknown_run", "no run called %r to stream" % run_id)
        return Response(200, b"", SSE_HEADERS, stream=_replay_stream(run_id, data))
    return Response(200, b"", SSE_HEADERS, stream=_job_stream(job))


def _frame(name: str, data) -> bytes:
    body = json.dumps(data, default=str)
    return ("event: %s\ndata: %s\n\n" % (name, body)).encode("utf-8")


def _job_stream(job):
    def stream(write):
        write(b": stream open for %s\n\n" % job.id.encode("ascii", "replace"))
        replay, handle = job.subscribe()
        try:
            seen_done = False
            for event in replay:
                write(_frame(event.name, event.data))
                seen_done = seen_done or event.name == "done"
            if seen_done:
                return
            import queue as _queue
            while True:
                try:
                    event = handle[1].get(timeout=HEARTBEAT_SECONDS)
                except _queue.Empty:
                    write(b": heartbeat\n\n")
                    continue
                write(_frame(event.name, event.data))
                if event.name == "done":
                    return
        finally:
            job.unsubscribe(handle)
    return stream


def _replay_stream(run_id: str, data):
    """A run with no live job: replay the manifest as stage events, then done."""
    def stream(write):
        write(b": replaying a finished run from its manifest\n\n")
        view = manifest.view_for(run_id, data)
        rows = view.list()
        for row in rows:
            write(_frame("stage", row))
        findings = manifest.findings(run_id)
        for index, item in enumerate(findings):
            write(_frame("finding", dict(item, id=item.get("id",
                                                           manifest.finding_id(item, index)))))
        mode = _manifest_llm(data)
        write(_frame("done", {
            "run_id": run_id,
            "status": manifest.run_status(data, rows),
            "returncode": None,
            "stages": rows,
            "stage_counts": _counts(rows),
            "findings_by_severity": manifest.findings_by_severity(run_id, findings),
            "findings_total": len(findings),
            "chips": manifest.chip_ids(run_id, data),
            "llm": mode.get("mode"),
            "llm_detail": mode,
            "live": False,
            "message": "replayed from %s; this run is not in flight"
                       % paths.MANIFEST_NAME if data else
                       "no manifest for this run yet",
        }))
    return stream


# --- request helpers ---------------------------------------------------------

def _run_id(req: Request, allow_repo: bool = False) -> str:
    raw = str(req.params.get("id") or "")
    if allow_repo and raw == paths.REPO_SCOPE:
        return paths.REPO_SCOPE
    try:
        safe = paths.sanitise_id(raw, fallback="")
    except paths.BadId as problem:
        raise ApiError(400, "bad_run_id", str(problem)) from None
    if safe != raw:
        raise ApiError(400, "bad_run_id",
                       "%r is not a run id; ids are lowercase letters, digits and "
                       "underscores (this one would be %r)" % (raw, safe))
    return safe


def _require_run(req: Request, run_id: str) -> None:
    if req.app.manager.get(run_id) is None and not manifest.work_dir(run_id).is_dir():
        raise ApiError(404, "unknown_run", "no run called %r" % run_id)


def _read_submission(req: Request):
    """({text, doi, url, id, options}, (filename, bytes) or None) from either body form."""
    content_type = req.content_type
    if "multipart/form-data" in content_type.lower():
        body = req.body(paths.MAX_UPLOAD_BYTES)
        fields, files = parse_multipart(body, content_type)
        options = {}
        if fields.get("options"):
            try:
                parsed = json.loads(fields["options"])
                options = parsed if isinstance(parsed, dict) else {}
            except ValueError:
                raise ApiError(400, "bad_options",
                               "the options field is not JSON") from None
        for key in ("llm", "scale", "from", "to"):
            if fields.get(key):
                options[key] = fields[key]
        data = {key: fields.get(key) for key in ("text", "doi", "url", "id")}
        data["options"] = options
        upload = files.get("file") or files.get("paper") or None
        if files and upload is None:
            raise ApiError(400, "bad_upload",
                           "the uploaded file must be in the 'file' field")
        if upload is not None and not upload[1]:
            raise ApiError(400, "empty_upload", "%r is empty" % upload[0])
        return data, upload
    if content_type and "application/json" not in content_type.lower():
        raise ApiError(415, "unsupported_media_type",
                       "send multipart/form-data with a 'file', or application/json with "
                       "text, doi or url")
    body = req.json()
    options = body.get("options") or {}
    if not isinstance(options, dict):
        raise ApiError(400, "bad_options", "options must be an object")
    return {"text": body.get("text"), "doi": body.get("doi"), "url": body.get("url"),
            "id": body.get("id"), "options": options}, None


def _new_run_id(fields, upload) -> str:
    explicit = fields.get("id")
    if explicit:
        safe = paths.sanitise_id(explicit, fallback="")
        if safe != str(explicit).strip().lower():
            raise ApiError(400, "bad_run_id",
                           "id %r must be snake_case: lowercase letters, digits and "
                           "underscores, starting with a letter" % explicit)
        return safe
    seed = None
    if upload is not None:
        seed = Path(paths.safe_filename(upload[0])).stem
    elif fields.get("doi"):
        seed = str(fields["doi"])
    elif fields.get("url"):
        seed = re.sub(r"^https?://", "", str(fields["url"]))[:60]
    elif fields.get("text"):
        seed = "pasted_" + time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    try:
        return paths.sanitise_id(seed, fallback="run_" + time.strftime("%Y%m%d_%H%M%S",
                                                                      time.gmtime()))
    except paths.BadId as problem:
        raise ApiError(400, "bad_run_id", str(problem)) from None


def _store_source(run_id: str, fields, upload):
    """Put the submitted document where S0 will read it. Uploads go under
    assay2chip/work/<id>/upload/ and nowhere else."""
    if upload is not None:
        filename, blob = upload
        if len(blob) > paths.MAX_UPLOAD_BYTES:
            raise ApiError(413, "too_large",
                           "%s is %d bytes; the limit is %d"
                           % (filename, len(blob), paths.MAX_UPLOAD_BYTES),
                           limit_bytes=paths.MAX_UPLOAD_BYTES)
        try:
            target = sandbox.upload_target(run_id, filename)
        except sandbox.Denied as denied:
            raise ApiError(400, "bad_upload", denied.message) from None
        target.write_bytes(blob)
        return paths.repo_rel(target), "upload"
    text = fields.get("text")
    if text and str(text).strip():
        if len(str(text).encode("utf-8")) > paths.MAX_UPLOAD_BYTES:
            raise ApiError(413, "too_large", "the pasted text is over the %d byte limit"
                           % paths.MAX_UPLOAD_BYTES, limit_bytes=paths.MAX_UPLOAD_BYTES)
        target = sandbox.upload_target(run_id, "%s.md" % run_id)
        target.write_text(str(text), encoding="utf-8")
        return paths.repo_rel(target), "text"
    for kind in ("doi", "url"):
        value = fields.get(kind)
        if value and str(value).strip():
            reference = str(value).strip()
            if reference.startswith("-"):
                raise ApiError(400, "bad_source",
                               "a %s cannot start with '-'" % kind)
            if "\n" in reference or "\x00" in reference or len(reference) > 2048:
                raise ApiError(400, "bad_source", "that %s is not usable" % kind)
            if kind == "url" and not re.match(r"^https?://", reference):
                raise ApiError(400, "bad_source", "a url must start with http:// or https://")
            return reference, kind
    raise ApiError(400, "no_source",
                   "give a file upload, or JSON with text, doi or url")


def _stage_value(value, name) -> Optional[str]:
    if value is None or str(value).strip() == "":
        return None
    index = stages.resolve(value)
    if index is None:
        raise ApiError(400, "unknown_stage",
                       "%r is not a stage; they are %s" % (value, ", ".join(stages.tokens())),
                       field=name)
    number = stages.SPINE[index][0]
    return stages.token(number)


def _scale_value(value) -> str:
    if value is None or str(value).strip() == "":
        return "auto"
    scale = str(value).strip().lower()
    if scale not in SCALES:
        raise ApiError(400, "bad_scale", "scale must be one of %s" % ", ".join(SCALES))
    return scale


def _llm_choice(value) -> dict:
    try:
        mode = llm.report(value)
    except ValueError as problem:
        raise ApiError(400, "bad_llm", str(problem)) from None
    if mode["requested"] == llm.REAL and mode["mode"] != llm.REAL:
        raise ApiError(
            503, "llm_unavailable",
            "a real model was asked for but %s is not set in the server's environment. "
            "Run with the stub, or start the server with the key; a stubbed extraction "
            "is never reported as a real one." % llm.KEY_ENV,
            llm=mode)
    return mode


_FLAG_CACHE = {}


def _supports_flag(script: Path, flag: str) -> bool:
    """Whether a script's CLI mentions a flag, so a new one is used as soon as it lands."""
    try:
        stamp = script.stat().st_mtime_ns
    except OSError:
        return False
    key = (str(script), flag)
    cached = _FLAG_CACHE.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        text = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    found = ('"%s"' % flag) in text or ("'%s'" % flag) in text
    _FLAG_CACHE[key] = (stamp, found)
    return found


# --- the table ---------------------------------------------------------------

ID = r"(?P<id>[^/]+)"

ROUTES: List[Route] = [
    Route("GET", re.compile(r"^/api/health$"), health),
    Route("GET", re.compile(r"^/api/stages$"), stage_list),
    Route("GET", re.compile(r"^/api/runs$"), list_runs),
    Route("POST", re.compile(r"^/api/runs$"), create_run),
    Route("GET", re.compile(r"^/api/runs/%s$" % ID), get_run),
    Route("GET", re.compile(r"^/api/runs/%s/events$" % ID), events),
    Route("GET", re.compile(r"^/api/runs/%s/artifacts$" % ID), artifacts),
    Route("GET", re.compile(r"^/api/runs/%s/file$" % ID), run_file),
    Route("GET", re.compile(r"^/api/runs/%s/report$" % ID), report),
    Route("POST", re.compile(r"^/api/runs/%s/rerun$" % ID), rerun),
    Route("GET", re.compile(r"^/api/chips$"), list_chips),
    Route("GET", re.compile(r"^/api/chips/%s$" % ID), get_chip),
    Route("POST", re.compile(r"^/api/cfd$"), cfd),
    # The front end probes /docs/<path> and then /api/docs/<path>; both are the same
    # sandbox, so the second one answers rather than 404ing for no reason.
    Route("GET", re.compile(r"^/api/docs/(?P<path>.+)$"), docs_file),
]

#: Pages and files outside /api/, matched before web/static/ is tried.
PAGE_ROUTES: List[Route] = [
    Route("GET", re.compile(r"^/docs/(?P<path>.+)$"), docs_file),
    Route("GET", re.compile(r"^/twin/?$"), twin_file),
    Route("GET", re.compile(r"^/twin/(?P<path>.+)$"), twin_file),
]

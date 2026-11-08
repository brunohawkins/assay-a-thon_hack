"""The queue API, mounted into web/server.py's server under /api/queue/.

The launcher (local.py) builds web/server.py's own App and Server and puts these routes in
front of the existing ones, so ONE port serves the GUI, the existing /api/* and the durable
queue. Nothing in web/ is modified.

    GET  /api/queue/status                    machine, pool plan, slots, queue depth
    GET  /api/queue/runs                      queued runs, newest first
    POST /api/queue/runs                      multipart `file`, or JSON {text|url|doi, id?,
                                              options: {llm, foam, foam_max_steps}}
                                              (header Idempotency-Key: repeat-safe) -> 202
    GET  /api/queue/runs/<id>                 the run and every job
    GET  /api/queue/runs/<id>/events          Server-Sent Events (Last-Event-ID resumes)
    GET  /api/queue/runs/<id>/artifacts       every artefact with a signed download URL
    POST /api/queue/runs/<id>/cancel          stop it; running jobs are killed
    POST /api/queue/runs/<id>/retry           requeue a failed or cancelled run's failures
    GET  /api/queue/dl/<key>?exp=&sig=        a signed download (HMAC, expires)
    GET  /api/queue/ui  (and /queue)          a small page: submit, watch, download
"""

from __future__ import annotations

import json
import mimetypes
import re
import secrets
import time
import unicodedata
from pathlib import PurePosixPath
from typing import List

from web.api import http as H

from .queue import TERMINAL

ID = r"(?P<id>[a-z][a-z0-9_]{0,80})"
ALLOWED_UPLOADS = (".pdf", ".md", ".markdown", ".txt")
SSE_MAX_S = 30 * 60
DOWNLOAD_HEADERS = [
    ("X-Content-Type-Options", "nosniff"),
    # An artefact is never allowed to run script in the GUI's origin (an SVG or HTML file
    # from an uploaded document could carry some).
    ("Content-Security-Policy", "default-src 'none'; img-src 'self' data:; "
                                "style-src 'unsafe-inline'; media-src 'self'; sandbox"),
]


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    text = re.sub(r"_+", "_", text)[:40].strip("_")
    if not text or not text[0].isalpha():
        text = "assay_" + text if text else "assay"
    return text.strip("_")


def new_run_id(base: str) -> str:
    return "%s_%s" % (slug(base), secrets.token_hex(3))


class QueueApi:
    def __init__(self, ctx):
        """ctx: settings, q, store, orch, signer, worker, plan, machine."""
        self.ctx = ctx

    # --- helpers ------------------------------------------------------------------

    def _run_or_404(self, run_id: str) -> dict:
        run = self.ctx.q.run(run_id)
        if run is None:
            raise H.ApiError(404, "no_such_run", "no queued run %r" % run_id)
        return run

    def _signed(self, key: str) -> str:
        s = self.ctx.signer.sign(key, self.ctx.settings.url_ttl_s)
        return "/api/queue/dl/%s?exp=%d&sig=%s" % (key, s["exp"], s["sig"])

    @staticmethod
    def _job_view(job: dict) -> dict:
        keep = ("id", "kind", "key", "resource", "status", "attempts", "max_attempts",
                "created", "started", "finished", "exit_code", "error", "result", "payload")
        return {k: job.get(k) for k in keep}

    def _run_view(self, run: dict, jobs: bool = False) -> dict:
        all_jobs = self.ctx.q.jobs(run["id"])
        counts = {}
        for j in all_jobs:
            counts[j["status"]] = counts.get(j["status"], 0) + 1
        out = {"run_id": run["id"], "title": run["title"], "status": run["status"],
               "created": run["created"], "updated": run["updated"], "error": run["error"],
               "options": run["options"], "jobs_by_status": counts}
        if jobs:
            out["jobs"] = [self._job_view(j) for j in all_jobs]
        return out

    # --- routes -------------------------------------------------------------------

    def status(self, req):
        c = self.ctx
        return H.json_response({
            "ok": True, "url": "http://%s:%d/" % (c.settings.host, c.settings.port),
            "machine": c.machine.to_json(), "plan": c.plan.to_json(),
            "store": c.store.describe(), "settings": c.settings.public(),
            "queue": c.q.counts(), "slots": c.worker.status() if c.worker else [],
            "active_runs": c.q.active_run_ids()})

    def list_runs(self, req):
        return H.json_response([self._run_view(r) for r in self.ctx.q.runs()])

    def submit(self, req):
        c = self.ctx
        limit = c.settings.max_upload_mb * 1024 * 1024
        ctype = req.content_type.lower()
        fields, files = {}, {}
        if ctype.startswith("application/json"):
            body = req.json(limit)
        else:
            fields, files = H.parse_multipart(req.body(limit), req.content_type)
            body = dict(fields)
            if "options" in fields:
                try:
                    body["options"] = json.loads(fields["options"])
                except ValueError:
                    raise H.ApiError(400, "bad_options", "options must be JSON") from None
        options = body.get("options") or {}
        if not isinstance(options, dict):
            raise H.ApiError(400, "bad_options", "options must be an object")
        llm = options.get("llm") or c.settings.llm_mode
        if llm not in ("stub", "real"):
            raise H.ApiError(400, "bad_llm", "options.llm must be 'stub' or 'real'")
        clean = {"llm": llm, "foam": bool(options.get("foam", True))}
        if options.get("foam_max_steps") is not None:
            try:
                clean["foam_max_steps"] = max(0, int(options["foam_max_steps"]))
            except (TypeError, ValueError):
                raise H.ApiError(400, "bad_options", "foam_max_steps must be an integer") \
                    from None

        upload = None
        if "file" in files:
            name, data = files["file"]
            safe = PurePosixPath(name.replace("\\", "/")).name
            stem, dot, ext = safe.rpartition(".")
            if ("." + ext.lower()) not in ALLOWED_UPLOADS or not data:
                raise H.ApiError(400, "bad_upload", "upload a non-empty %s file"
                                 % "/".join(ALLOWED_UPLOADS))
            upload = (slug(stem or "upload") + "." + ext.lower(), data)
            base = body.get("id") or stem or "assay"
        elif body.get("text"):
            upload = ("protocol.md", str(body["text"]).encode("utf-8"))
            base = body.get("id") or body.get("title") or "pasted"
        elif body.get("url") or body.get("doi"):
            base = body.get("id") or "fetched"
        else:
            raise H.ApiError(400, "nothing_to_run", "send a file, text, url or doi")

        run_id = new_run_id(str(base))
        if upload is not None:
            rel = "assay2chip/work/%s/upload/%s" % (run_id, upload[0])
            paper = rel
        else:
            rel, paper = None, str(body.get("url") or body.get("doi"))
        run = c.q.create_run(run_id, title=str(body.get("title") or base)[:200], paper=paper,
                             options=clean, idem_key=req.headers.get("Idempotency-Key"))
        if run.get("existing"):
            return H.json_response({"run_id": run["id"], "existing": True}, 200)
        if upload is not None:
            import hashlib
            key = "runs/%s/%s" % (run_id, rel)
            c.store.put_bytes(key, upload[1])
            c.q.record_artifacts(run_id, [{"path": rel, "key": key, "bytes": len(upload[1]),
                                           "sha256": hashlib.sha256(upload[1]).hexdigest(),
                                           "kind": "upload", "job_id": None}])
        c.orch.advance(run_id)
        return H.json_response({"run_id": run_id, "existing": False,
                                "events": "/api/queue/runs/%s/events" % run_id}, 202)

    def get_run(self, req):
        return H.json_response(self._run_view(self._run_or_404(req.params["id"]), jobs=True))

    def artifacts(self, req):
        run = self._run_or_404(req.params["id"])
        rows = [{"path": a["path"], "kind": a["kind"], "bytes": a["bytes"],
                 "sha256": a["sha256"], "url": self._signed(a["key"])}
                for a in self.ctx.q.artifacts(run["id"])]
        return H.json_response(rows)

    def cancel(self, req):
        run = self._run_or_404(req.params["id"])
        self.ctx.q.cancel_run(run["id"])
        self.ctx.orch.advance(run["id"])
        return H.json_response(self._run_view(self.ctx.q.run(run["id"])), 202)

    def retry(self, req):
        run = self._run_or_404(req.params["id"])
        n = self.ctx.q.retry_run(run["id"])
        self.ctx.orch.advance(run["id"])
        return H.json_response({"run_id": run["id"], "requeued": n}, 202)

    def events(self, req):
        run = self._run_or_404(req.params["id"])
        try:
            after = int(req.headers.get("Last-Event-ID") or req.get("after", "0") or 0)
        except ValueError:
            after = 0
        q = self.ctx.q

        def stream(write):
            last, started, ping = after, time.time(), time.time()
            write(b"retry: 3000\n\n")
            while time.time() - started < SSE_MAX_S:
                batch = q.events(run["id"], last)
                for e in batch:
                    last = e["seq"]
                    write(("id: %d\nevent: %s\ndata: %s\n\n" % (
                        e["seq"], e["name"], json.dumps(e["data"], default=str))
                    ).encode("utf-8"))
                if not batch:
                    current = q.run(run["id"])
                    if current is None or current["status"] in TERMINAL:
                        write(("event: end\ndata: %s\n\n" % json.dumps(
                            {"status": current and current["status"]})).encode("utf-8"))
                        return
                    if time.time() - ping > 15:
                        ping = time.time()
                        write(b": ping\n\n")
                    time.sleep(0.5)

        return H.Response(200, b"", [("Content-Type", "text/event-stream"),
                                     ("Cache-Control", "no-store"),
                                     ("X-Accel-Buffering", "no")], stream=stream)

    def download(self, req):
        key = req.params["key"]
        if not self.ctx.signer.verify(key, req.get("exp"), req.get("sig")):
            raise H.ApiError(403, "bad_signature", "this link is invalid or has expired")
        try:
            data = self.ctx.store.get_bytes(key)
        except Exception:
            raise H.ApiError(404, "not_found", "no such artefact") from None
        name = PurePosixPath(key).name
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        inline = ctype.startswith(("image/png", "image/jpeg", "video/", "text/plain",
                                   "application/json"))
        return H.Response(200, data, [
            ("Content-Type", ctype),
            ("Content-Disposition", '%s; filename="%s"' % (
                "inline" if inline else "attachment", name.replace('"', "")))
        ] + DOWNLOAD_HEADERS)

    def ui(self, req):
        return H.Response(200, UI_HTML.encode("utf-8"),
                          [("Content-Type", "text/html; charset=utf-8")] + [
                              ("Content-Security-Policy",
                               "default-src 'self'; script-src 'unsafe-inline'; "
                               "style-src 'unsafe-inline'")])

    def routes(self) -> List[H.Route]:
        p = "/api/queue"
        return [
            H.Route("GET", re.compile(r"^%s/status$" % p), self.status),
            H.Route("GET", re.compile(r"^%s/runs$" % p), self.list_runs),
            H.Route("POST", re.compile(r"^%s/runs$" % p), self.submit),
            H.Route("GET", re.compile(r"^%s/runs/%s$" % (p, ID)), self.get_run),
            H.Route("GET", re.compile(r"^%s/runs/%s/events$" % (p, ID)), self.events),
            H.Route("GET", re.compile(r"^%s/runs/%s/artifacts$" % (p, ID)), self.artifacts),
            H.Route("POST", re.compile(r"^%s/runs/%s/cancel$" % (p, ID)), self.cancel),
            H.Route("POST", re.compile(r"^%s/runs/%s/retry$" % (p, ID)), self.retry),
            H.Route("GET", re.compile(r"^%s/dl/(?P<key>runs/.+)$" % p), self.download),
            H.Route("GET", re.compile(r"^%s/ui/?$" % p), self.ui),
        ]

    def pages(self) -> List[H.Route]:
        return [H.Route("GET", re.compile(r"^/queue/?$"), self.ui)]


UI_HTML = r"""<!doctype html>
<html lang="en"><meta charset="utf-8"><title>assay2chip - local queue</title>
<style>
body{font:15px/1.5 system-ui,sans-serif;margin:2rem auto;max-width:72rem;padding:0 1rem;color:#222}
table{border-collapse:collapse;width:100%;margin:.5rem 0}td,th{border-bottom:1px solid #ddd;padding:.3rem .5rem;text-align:left;font-size:14px}
code{background:#f3f3f3;padding:.1em .3em;border-radius:3px}.muted{color:#777}
.done{color:#176f2c}.failed,.cancelled{color:#a11}.running{color:#1a4fa0}.done_with_errors{color:#a66b00}
fieldset{border:1px solid #ddd;padding:1rem}button{padding:.3rem .8rem}
</style>
<h1>assay2chip <span class="muted">local queue</span></h1>
<p class="muted" id="plan"></p>
<fieldset><legend>Submit an assay</legend>
<form id="f"><input type="file" name="file" accept=".pdf,.md,.markdown,.txt">
 <label>LLM <select name="llm"><option>stub</option><option>real</option></select></label>
 <label><input type="checkbox" name="foam" checked> OpenFOAM 3D</label>
 <button>Submit</button> <span id="msg"></span></form>
<p class="muted">Stub mode is offline and deterministic. Real mode sends the document's text
to the language-model API: never submit lab-confidential material in real mode.</p></fieldset>
<h2>Runs</h2><table id="runs"><thead><tr><th>run</th><th>status</th><th>jobs</th><th></th></tr></thead><tbody></tbody></table>
<div id="detail"></div>
<script>
const $=s=>document.querySelector(s), esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function j(u,o){const r=await fetch(u,o);return r.json()}
async function plan(){const s=await j('/api/queue/status');$('#plan').textContent=
 `${s.machine.cores} cores, ${s.machine.mem_gb} GB - ${s.plan.light_slots} light slot(s), ${s.plan.foam_slots} OpenFOAM case(s) at once on ${s.plan.foam_cores_per_case} cores - store ${s.store}`}
async function runs(){const rs=await j('/api/queue/runs');$('#runs tbody').innerHTML=rs.map(r=>
 `<tr><td><a href="#" data-r="${esc(r.run_id)}">${esc(r.run_id)}</a></td><td class="${esc(r.status)}">${esc(r.status)}</td>
 <td class="muted">${esc(Object.entries(r.jobs_by_status).map(([k,v])=>k+' '+v).join(', '))}</td>
 <td>${['queued','running','cancelling'].includes(r.status)?`<button data-c="${esc(r.run_id)}">cancel</button>`:''}</td></tr>`).join('')}
async function detail(id){const r=await j('/api/queue/runs/'+id), a=await j('/api/queue/runs/'+id+'/artifacts');
 $('#detail').innerHTML=`<h2>${esc(id)} <span class="${esc(r.status)}">${esc(r.status)}</span></h2>${r.error?`<p class="failed">${esc(r.error)}</p>`:''}
 <table><tr><th>job</th><th>status</th><th>attempts</th><th>error</th></tr>${r.jobs.map(x=>`<tr><td>${esc(x.key)}</td><td class="${esc(x.status)}">${esc(x.status)}</td><td>${x.attempts}/${x.max_attempts}</td><td class="muted">${esc(x.error)}</td></tr>`).join('')}</table>
 <h3>Artefacts (${a.length})</h3><table>${a.map(x=>`<tr><td><a href="${esc(x.url)}">${esc(x.path)}</a></td><td class="muted">${x.bytes} B</td></tr>`).join('')}</table>`}
document.addEventListener('click',async e=>{const t=e.target;if(t.dataset.r){e.preventDefault();detail(t.dataset.r)}
 if(t.dataset.c){await fetch('/api/queue/runs/'+t.dataset.c+'/cancel',{method:'POST'});runs()}});
$('#f').addEventListener('submit',async e=>{e.preventDefault();const fd=new FormData(e.target);
 fd.append('options',JSON.stringify({llm:fd.get('llm'),foam:!!fd.get('foam')}));fd.delete('llm');fd.delete('foam');
 const r=await fetch('/api/queue/runs',{method:'POST',body:fd});const b=await r.json();$('#msg').textContent=b.run_id||b.detail||b.error;runs()});
plan();runs();setInterval(runs,3000);
</script></html>
"""

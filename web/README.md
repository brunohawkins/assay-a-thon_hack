# web: run the whole pipeline from a browser

```
python web/server.py            # http://127.0.0.1:8765
```

Standard library only (`http.server` + threads) - no new dependencies, no build step.
Binds to **127.0.0.1 by default**: the repo is private and runs can contain an ingested
paper, so exposing it on a network is an explicit `--host` choice, never the default.

## API (authoritative - backend and frontend are built in parallel against this)

All JSON. Errors are `{"error": "...", "detail": "..."}` with a real HTTP status.

| Method | Path | Body / query | Returns |
|---|---|---|---|
| GET | `/api/health` | | `{ok, version, stages:[...], llm: "real"\|"stub"}` |
| GET | `/api/stages` | | ordered stage list with one-line descriptions |
| GET | `/api/runs` | | `[{run_id, title, created, status, chips, findings_by_severity}]` |
| POST | `/api/runs` | multipart `file` **or** JSON `{text\|doi\|url, id?, options:{llm, scale: auto\|demo\|target, from?, to?}}` | `{run_id}` (202) |
| GET | `/api/runs/<id>` | | manifest: per-stage `{status: pending\|running\|done\|failed\|skipped\|not_implemented, started, seconds, outputs[], error?}` |
| GET | `/api/runs/<id>/events` | | **Server-Sent Events**: `stage`, `log`, `finding`, `done` |
| GET | `/api/runs/<id>/artifacts` | | `[{path, kind: json\|svg\|stl\|dxf\|md\|png, stage, bytes}]` |
| GET | `/api/runs/<id>/file` | `?path=` | the file, correct content type |
| GET | `/api/runs/<id>/report` | | `{markdown, json}` |
| POST | `/api/runs/<id>/rerun` | `{from}` | `{run_id}` |
| GET | `/api/chips` | | every repo chip: `[{id, path, footprint, scale, figures[]}]` |
| GET | `/api/chips/<id>` | | the chip spec |
| POST | `/api/cfd` | `{pipeline: "protocols/pipeline_mvp.json"}` or `{chip: "<id>"}` | `{job_id}` - progress on `/api/runs/<job_id>/events` |

**`/file` is sandboxed.** It serves only paths the run's manifest lists, plus
`cad/figures/`, `cad/exports/<chip_id>/` for that run's chips, and `chip/` / `protocols/`
specs. Resolve and compare real paths; reject `..`, symlinks out, and absolute paths. A
path-traversal hole in a tool that holds unpublished lab work is a confidentiality breach.

Runs execute `assay2chip/run.py` in a worker thread, one at a time by default (several
stages write into shared `chip/` and `protocols/`). Uploads go to
`assay2chip/work/<id>/upload/` and nowhere else.

## Views

1. **New run** - drop a PDF / markdown / text, or paste a DOI or URL; choose scale and
   LLM mode (stub is labelled as such, everywhere it appears).
2. **Run** - the stage timeline with live logs; click a stage to see its output file.
3. **Steps** - every assay step and where it ended up: which chip, off chip, or missing part.
4. **Chips** - plan-view SVGs, the 3D renders from S11, fabrication manifest and downloads.
5. **CFD** - pressure / speed / shear / residence-time maps per chip and protocol step,
   the findings table by severity, and each fix with its before and after numbers.
6. **Report** - `report.md`, rendered.

Every simulated number is labelled simulated. A demo-scale (30 x 20 mm) chip is labelled
DEMO SCALE wherever it appears, so it is never mistaken for the real device.

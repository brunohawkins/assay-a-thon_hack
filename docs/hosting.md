# Hosting: the pipeline as a local service on this Mac

One command starts everything on **127.0.0.1**: the GUI and API (`web/server.py`), a
durable job queue, a worker pool, and artefact storage. It uses the repo's `.venv` and
the Homebrew OpenFOAM. Nothing is exposed to the network, and nothing leaves the machine
unless you choose `llm: real` (see Confidentiality below).

```
deploy/local/a2c start            # foreground; Ctrl-C stops it cleanly
deploy/local/a2c start --detach   # or in the background
deploy/local/a2c status           # up/down, pool, what each slot is doing, queue depth
deploy/local/a2c stop             # graceful; running jobs go back in the queue
deploy/local/a2c logs [-f]        # the one service log;  logs RUN_ID  for a run's job logs
```

Open http://127.0.0.1:8765/ for the GUI and **http://127.0.0.1:8765/queue** to submit to
the queue and download artefacts. From the shell:

```
deploy/local/a2c submit assay2chip/fixtures/bead_capture_assay.md --wait
deploy/local/a2c submit paper.pdf --no-foam          # skip OpenFOAM for a quick run
deploy/local/a2c submit https://doi.org/... --url
```

## What runs where

```
 browser / a2c CLI ──► 127.0.0.1:8765  one process (assay2chip/hosting/local.py)
                         ├─ web/server.py routes   GUI, /api/*  (unchanged)
                         ├─ /api/queue/*            submit, status, events (SSE), cancel,
                         │                          retry, artefacts with signed URLs
                         ├─ SQLite queue (WAL)      runs, jobs, events, artefacts
                         └─ worker pool             subprocesses, one workspace per run
                              light x2  ingest s0 · extract s1 · plan s2-s4 · design s5-s10
                                        · fabricate s11 · collect · report s12
                              foam  x1  python -m assay2chip.foam.run per chip x step
                                        (6 cores; OpenFOAM only via foam/runner.py)
                         artefacts ──► local store (or any S3 bucket, by configuration)
```

A run: ingest → extract → plan → design, then one OpenFOAM job per chip × flowing
protocol step (highest flow first, at most 4 per chip) in parallel with fabricate, then
collect (`foam/summary.json`) and the report. Design (s5-s10) is one job because S10's fix
loop back to S6/S7 only works inside one `run.py` call. Each run gets its own copy of the
source tree, so runs never write into the checkout and never collide in `chip/`.

Retries: a crash, timeout or unexpected exit is retried (ingest/plan/design/foam 2 tries,
extract 3, with backoff); a stage error inside a segment is deterministic, recorded as
`done_with_errors`, and the run continues so the report can say what failed. Repeat-safe
submission: send an `Idempotency-Key` header. Cancel kills the job's whole process group.

Durability: jobs are rows in SQLite. A clean stop hands running jobs back without spending
a retry; after a crash the next start requeues them. `a2c start` resumes unfinished runs.

## Configuration (environment variables or `start` flags)

| Variable | Flag | Default |
|---|---|---|
| `A2C_DATA_DIR` | `--data` | `~/Library/Application Support/assay2chip` (queue, store, workspaces, logs) |
| `A2C_PORT` | `--port` | 8765 (always bound to 127.0.0.1) |
| `A2C_LIGHT_SLOTS` | `--light-slots` | 2 |
| `A2C_FOAM_MAX_PARALLEL` | `--foam-parallel` | 1 OpenFOAM case at a time |
| `A2C_FOAM_CORES_PER_CASE` | `--foam-cores` | min(6, cores − light slots) → 6 here |
| `A2C_FOAM_MAX_STEPS_PER_CHIP` | | 4 |
| `A2C_FOAM_SOLVE` / `A2C_FOAM_VIDEO` | | on / off (`--solve` launches OpenFOAM; off = reduced-order renders) |
| `A2C_LLM` | `--llm` | `stub` |
| `A2C_STORE` | | `local`; `s3` + `A2C_S3_ENDPOINT/BUCKET/REGION/ACCESS_KEY_ID/SECRET_ACCESS_KEY` |
| `A2C_WORKSPACE_TTL_DAYS` | | 7 (old workspaces, incl. OpenFOAM scratch, are removed) |

`a2c status` prints the pool it chose for this machine.

## Private by default

Bound to 127.0.0.1 only; any other host is refused. Every request must carry a loopback
`Host` (defeats DNS rebinding) and browser POSTs must come from the same origin (defeats
cross-site form posts). Downloads are HMAC-signed, expiring links; the key is generated
into the data directory (mode 0700). Artefacts are served with a sandboxing CSP.

## Confidentiality and the language model

The data directory is **outside the repo**, so uploads and results can never be committed
by accident. Nothing from the lab's Labstep or OneDrive belongs in a run that leaves this
machine. With `llm: real`, the extract and design jobs (only those) receive the model API
key and send document text to the model provider; stub mode is offline. Job subprocesses
get a minimal environment, never the storage keys.

## Proving it

```
.venv/bin/python deploy/local/smoke.py      # ~1 min; fakes stand in for s5-s12 and OpenFOAM
.venv/bin/python -m unittest tests.test_deploy_queue tests.test_deploy_storage tests.test_deploy_local
```

The smoke test starts the real service, submits the bead-capture fixture, and checks the
fan-out, a retry, a cancel, signed downloads, a stop-and-restart that resumes a job, and a
clean stop. `smoke.py --real` runs the real pipeline and OpenFOAM instead (slow).

## Cloud: a later option

The queue, worker, storage and signed URLs are platform-neutral. Moving artefacts to a
bucket is configuration (`A2C_STORE=s3`; SigV4 is built in and checked against AWS's
published examples, and OCI Object Storage, MinIO and R2 speak the same API). A hosted
version would put the same process on a bigger machine behind a TLS reverse proxy with
authentication; never expose the bare server.

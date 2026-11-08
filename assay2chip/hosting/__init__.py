"""assay2chip.hosting: run the pipeline as a hosted service on one machine.

Platform-neutral pieces, standard library only (Python 3.9+):

    config.py      settings from environment variables; CPU / RAM / disk detection and
                   the worker-pool plan sized from them
    sigv4.py       AWS Signature V4, for any S3-compatible store (OCI Object Storage's
                   S3 compatibility API, MinIO, AWS S3)
    storage.py     ObjectStore: LocalStore (a directory) and S3Store; HMAC-signed URLs
    queue.py       the durable job queue and run state, in one SQLite file (WAL)
    orchestrator.py  the run as a state machine: segments, the OpenFOAM fan-out per chip
                   and step, the fan-in, the report. Idempotent; safe to call twice
    workspace.py   one isolated copy of the source tree per run, and artefact sync
    commands.py    what each job kind runs (assay2chip/run.py slices, foam/run.py)
    worker.py      the slot pool that leases jobs and runs them
    api.py         the hosted HTTP API (behind the reverse proxy, never bare)
    local.py       `python -m assay2chip.hosting.local`: everything in plain processes

See assay2chip/hosting/README.md and docs/hosting.md. Nothing in the existing pipeline
imports this package; it drives the pipeline through its CLIs.
"""

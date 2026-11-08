# deploy

Running the pipeline as a service. See [docs/hosting.md](../docs/hosting.md).

| Path | What |
|---|---|
| `local/a2c` | the one command: `start [--detach]`, `stop`, `status`, `logs`, `submit` |
| `local/smoke.py` | end-to-end proof of the local service (no Docker) |
| `local/fake_stage.py` | fast fakes the smoke test stands in for the slow stages; every file it writes says FAKE |

The service itself is `assay2chip/hosting/` (queue, worker, storage, API). Cloud hosting is
parked; the storage layer already speaks S3, so it is a configuration change later.

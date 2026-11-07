# operator/ (owner: Bruno)

Runs a protocol on a chip and records what happened as a run file for the twin.

```
python operator/run.py --protocol protocols/placeholder_scrnaseq.json --chip chip/placeholder_chip_spec.json
python operator/run.py --protocol protocols/placeholder_scrnaseq.json --chip chip/placeholder_chip_spec.json --llm
python operator/run.py --all [--chip chip/<spec>.json]   # every protocol (default chip: the placeholder)
python operator/run.py --reindex                          # just rebuild runs/index.json
```

## Pieces

| File | What |
|---|---|
| `commands.py` | The six commands as functions, with validation against `contracts/schemas/commands.schema.json` and the chip. Also builds the Claude API tool definitions from that schema. |
| `planner.py` | Deterministic operator: one command per protocol step, timing from `duration_s`, narration from `note`. Split/pool fan-out comes from the chip's channels. |
| `llm_operator.py` | Claude operates the chip through the six commands as tools and explains each decision in one sentence (the narration). **Stub/TODO: written but not yet run against the live API.** |
| `adapters/sim.py` | Default adapter: validates and records commands into a run file. |
| `adapters/serial_pump.py` | Same interface, for real pumps later. Raises `NotImplementedError`; the docstring describes the planned serial protocol. |
| `run.py` | CLI. Writes `runs/<protocol_id>__<chip_id>.json` (LLM runs: `…__llm.json`) and rebuilds `runs/index.json`. |
| `schema_io.py` | Loads the contract schemas; shared with `tools/validate.py`. |

Every adapter has `send(command, args, **meta) -> result`.

## LLM operator

- Needs `ANTHROPIC_API_KEY` in your environment (`export ANTHROPIC_API_KEY=...`).
  **Never commit a key**; `.env` and `*.key` are git-ignored.
- With no key, or if the API call fails, `run.py --llm` prints why and falls back to the planner.
- Model: `claude-opus-5-5` (override with `CAPSULE_LLM_MODEL`). Server-side refusal fallback
  is switched on (`FALLBACK_BETA` / `FALLBACKS` in `llm_operator.py`); set `FALLBACKS = None` to turn it off.
- Invalid tool calls go back to the model as errors so it can correct itself; nothing invalid reaches a run file.
- Once a good LLM run exists, commit it so the demo works offline.

Note: `operator/` can't be imported as a Python package (it would shadow the standard
library's `operator` module). Scripts here import each other directly; run them by path
as shown above.

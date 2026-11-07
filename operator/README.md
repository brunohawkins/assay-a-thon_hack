# operator/ (owner: Bruno)

The automated management system: it plans, checks and runs protocols on the chip in a
closed loop, with an AI operator on top, and writes the run files the twin plays.

> **SIMULATED HARDWARE.** There is no physical pump, valves or sensors this weekend.
> Closed-loop runs use a virtual chip (`adapters/virtual_chip.py`). The hardware drivers
> are written and ready to plug in, but are **UNTESTED – no hardware available**.

## How it works

```
protocol step ─▶ optimizer ─▶ safety ─▶ executor ─▶ adapter (virtual chip / hardware)
                  (simulates   (approves  (time         │
                   candidates   or blocks)  slices)     ▼
                   with model)             ◀── sensors: pressure, absorbance, pump, temp, counter
                       ▲                        │
                       └── model refit + re-plan ◀┘   monitor ─▶ alerts ─▶ operator recovery
```

- **The AI proposes, the model simulates, the safety checker approves.** Safety rules are
  deterministic code (`safety.py`), never the LLM.
- **Model-predictive control:** the optimizer predicts candidate actions with the process
  model, picks the best safe one, the executor runs it in slices, measures, and corrects.
- **Hardware abstraction:** every adapter implements `adapters/base.py`, so the same
  controller drives the virtual chip now and real hardware later.

## Pieces

| File | What |
|---|---|
| `state.py` | `ChipState`: liquid, reagent, exchange %, capsules (on mesh / suspended / in channel), temperature, pressure, reservoirs, waste, collection, clock. Two copies at runtime: **believed** and **true** (simulation only). |
| `model.py` | Process model: well-mixed exchange + diffusion lag into capsules, mesh pressure, capsule escape risk, back-flush lift, first-order heating, volumes. Constants from `contracts/parameters.json` (TODO placeholders marked there). |
| `flow_calc.py` | Channel and mesh flow resistance; open-channel diffusion time (APPROXIMATE). |
| `safety.py` | `check(command, args, believed, chip)` -> approved / warning / blocked. Unit-tested rules. |
| `optimizer.py` | Simulates candidate flows / volumes / washes / ramps, drops unsafe ones, picks least time then reagent; returns the comparison table. |
| `controller.py` | The closed loop: plan, approve, execute in slices, measure, refit (mesh resistance, chamber volume), re-plan, log. Open loop when there are no sensors. |
| `faults.py` | Injectable faults (clog, reagent_low, capsule_loss, sensor_fault) and the alert monitor. |
| `operators.py` | `planner` (deterministic + recovery table) and `scripted` (replays a transcript). |
| `llm_operator.py` | Claude tool use over the controller (plan_step, execute_plan, back_flush, get_state, get_sensors, get_alerts, pause_for_human, distrust_sensor). Not yet run against the live API. |
| `report.py` | Builds the run record and the Markdown audit report. |
| `run.py` | CLI (below). |
| `adapters/virtual_chip.py` | **SIMULATED HARDWARE** (default): model physics with different parameters, seeded sensor noise, switchable sensors, faults. |
| `adapters/dryrun.py` | Prints the exact hardware actions with timestamps; "measurements" = model predictions. |
| `adapters/syringe_pump.py` | RS-232/USB syringe pump driver; command strings in `contracts/hardware/pump_<model>.json`. **UNTESTED – no hardware available.** |
| `adapters/valves.py`, `adapters/sensors.py` | Documented stubs (`NotImplementedError`). **UNTESTED – no hardware available.** |
| `planner.py`, `commands.py`, `adapters/sim.py` | Simple runs without a scenario (one command per step, no simulation). |

## Running

```bash
python operator/run.py --scenario scenarios/wash_mvp_clog.json                       # closed loop, planner
python operator/run.py --scenario scenarios/wash_mvp_clog.json --operator llm        # needs ANTHROPIC_API_KEY
python operator/run.py --scenario scenarios/wash_mvp_clog.json --operator scripted \
    --transcript runs/transcripts/placeholder_capsule_wash_mvp__wash_mvp_v1__wash_mvp_clog.json
python operator/run.py --scenario scenarios/wash_mvp_closed_loop.json --adapter dryrun
python operator/run.py --all          # regenerate every planner run
```

Each closed-loop run writes `runs/<protocol>__<chip>__<scenario>[__operator].json`, the
audit report `…_report.md`, and the operator's tool-call transcript in `runs/transcripts/`.

Scenarios (`scenarios/`): `wash_mvp_open_loop` (no sensors), `wash_mvp_closed_loop`
(pressure + absorbance), `wash_mvp_clog` (clog from wash 2), `wash_mvp_clog_open_loop`
(the same clog, no sensors), `wash_mvp_sensor_fault` (pressure sensor sticks mid-run).
A scenario sets the seed, which sensors exist, how reality differs from the model, and
which faults to inject.

## LLM operator

- `ANTHROPIC_API_KEY` from the environment only; **never commit a key**. No key: falls back
  to the planner with a notice. Tool calls are capped (`--max-tool-calls`, default 60).
- Model `claude-opus-5-5` (override with `CAPSULE_LLM_MODEL`), server-side refusal fallback on.
- Every action passes `safety.py`; blocked actions come back to the model with the reason.
- The transcript is saved so the run can be replayed offline with `--operator scripted`.
  The transcripts committed now come from the **planner** (labelled as such), because no
  API key was available to record an LLM run.

`operator/` can't be imported as a Python package (it would shadow the standard library's
`operator` module); run the scripts by path as shown.

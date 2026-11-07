"""LLM operator: Claude runs the protocol on the (SIMULATED) chip through the controller.

The AI PROPOSES, the model SIMULATES, the safety checker APPROVES. Claude never touches
hardware directly: its tools are the controller's primitives, and every action that
moves liquid passes the deterministic safety checker (operator/safety.py). Blocked
actions come back to Claude with the reason.

Tools: plan_step, execute_plan, back_flush, get_state (believed), get_sensors,
get_alerts, pause_for_human, distrust_sensor. Every tool takes a one-sentence `reason`,
which becomes the narration and the explanation in the run file.

API key: read from ANTHROPIC_API_KEY only; never commit it. With no key, run.py falls back
to the planner and prints a notice. Tool calls per run are capped (LLM_TOOL_CALL_CAP or
--max-tool-calls). The transcript of calls is saved so the run can be replayed offline
by the scripted operator.

Standard library only (urllib). STATUS: not yet run against the live API (no key here);
the loop is covered offline by tests/test_operators.py with a fake API.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Callable

from operators import dispatch

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
MODEL = os.environ.get("CAPSULE_LLM_MODEL", "claude-opus-5-5")
# Server-side refusal fallback: if the model declines, the API retries on a fallback model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACKS = "default"
MAX_TOKENS = 16000
DEFAULT_TOOL_CALL_CAP = int(os.environ.get("LLM_TOOL_CALL_CAP", "60"))

SYSTEM_PROMPT = """\
You operate a microfluidic capsule-processor chip through a controller. The hardware is \
SIMULATED (a virtual chip); act exactly as you would on real hardware.

How the system works:
- plan_step asks the optimizer to plan one protocol step from the believed state; it \
simulates candidate settings and returns the chosen plan.
- execute_plan runs that plan in small time slices, measuring after each slice and \
correcting the model. It returns "done", or "interrupted" with alerts, or "blocked" with \
the safety checker's reason.
- back_flush lifts the capsules off the mesh (use it when pressure is high, then re-plan).
- get_state, get_sensors and get_alerts let you look before acting.
- pause_for_human stops the run for a person (e.g. a reservoir needs refilling).
- distrust_sensor stops using a sensor that disagrees with the model.
A deterministic safety checker approves every action that moves liquid. You cannot \
override it; if it blocks something, choose a different action.

Rules:
- Carry out the protocol steps in order: plan_step then execute_plan for each.
- Protect the capsules first, then finish the protocol, then save time and reagent.
- In every tool call, explain the decision in one plain sentence in `reason`.
- When every step is done (or you have paused or stopped), reply with one short sentence \
and no tool call."""

ApiCall = Callable[[dict, str], dict]


class LLMError(RuntimeError):
    """The LLM run could not finish."""


def _tool(name: str, description: str, props: dict | None = None) -> dict:
    props = dict(props or {})
    props["reason"] = {"type": "string", "description": "One plain sentence explaining this decision."}
    return {"name": name, "description": description, "strict": True,
            "input_schema": {"type": "object", "additionalProperties": False, "properties": props,
                             "required": list(props)}}


STEP = {"step": {"type": "integer", "description": "Protocol step number."}}
TOOLS = [
    _tool("plan_step", "Plan one protocol step with the optimizer (simulates candidates from the believed state).", STEP),
    _tool("execute_plan", "Execute the current plan for a step in closed loop. Returns done / interrupted (with alerts) / blocked.", STEP),
    _tool("back_flush", "Back-flush a chamber: flow up through the mesh to lift the capsules and clear it.",
          {"chamber": {"type": "string", "description": "Chamber id, e.g. trap_1."}}),
    _tool("get_state", "The believed chip state (what the model thinks)."),
    _tool("get_sensors", "The latest readings from trusted sensors."),
    _tool("get_alerts", "Active alerts with their evidence."),
    _tool("pause_for_human", "Stop the run and ask a person to act (e.g. refill a reservoir)."),
    _tool("distrust_sensor", "Stop using a sensor that disagrees with the model; fall back to the model for it.",
          {"sensor": {"type": "string", "enum": ["pump_volume_ul", "pressure_kpa", "outlet_absorbance",
                                                 "temperature_c", "capsules_counted"]}}),
]


def call_messages_api(body: dict, api_key: str) -> dict:
    headers = {"content-type": "application/json", "x-api-key": api_key, "anthropic-version": API_VERSION}
    if body.get("fallbacks"):
        headers["anthropic-beta"] = FALLBACK_BETA
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise LLMError(f"Claude API returned HTTP {e.code}: {e.read().decode('utf-8', errors='replace')[:500]}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"could not reach the Claude API: {e.reason}") from e


def chip_summary(chip: dict) -> dict:
    return {"id": chip["id"], "capsule": chip.get("capsule"),
            "chambers": [{k: c.get(k) for k in ("id", "type", "volume_ul", "height_um", "retention")} for c in chip["chambers"]],
            "ports": [{k: p.get(k) for k in ("id", "type", "role", "capsule_passable")} for p in chip["ports"]],
            "channels": [{k: c.get(k) for k in ("id", "from", "to", "width_mm")} for c in chip["channels"]]}


def run_llm(ctl, api_key: str, model: str = MODEL, call_api: ApiCall = call_messages_api,
            max_tool_calls: int = DEFAULT_TOOL_CALL_CAP) -> None:
    first = (
        "SIMULATED HARDWARE. Protocol to run:\n"
        f"<protocol>\n{json.dumps(ctl.protocol, indent=2)}\n</protocol>\n\n"
        f"Chip:\n<chip>\n{json.dumps(chip_summary(ctl.chip), indent=2)}\n</chip>\n\n"
        f"Believed state:\n<state>\n{json.dumps(ctl.get_state())}\n</state>\n\n"
        f"Active alerts: {json.dumps(ctl.get_alerts())}\n\nRun the protocol."
    )
    messages: list[dict] = [{"role": "user", "content": first}]
    body = {"model": model, "max_tokens": MAX_TOKENS, "system": SYSTEM_PROMPT, "tools": TOOLS,
            "tool_choice": {"type": "auto"}, "output_config": {"effort": "medium"}}
    if FALLBACKS:
        body["fallbacks"] = FALLBACKS
    calls = 0
    while True:
        resp = call_api({**body, "messages": messages}, api_key)
        stop = resp.get("stop_reason")
        content = resp.get("content") or []
        messages.append({"role": "assistant", "content": content})   # keep thinking blocks as returned
        if stop == "refusal":
            raise LLMError("the model declined this request (stop_reason=refusal)")
        if stop == "max_tokens":
            raise LLMError("response hit max_tokens; not executing possibly truncated tool calls")
        uses = [b for b in content if b.get("type") == "tool_use"]
        if not uses:
            break
        results = []
        for block in uses:
            calls += 1
            inp = dict(block.get("input") or {})
            reason = str(inp.pop("reason", "")).strip()
            if calls > max_tool_calls:
                results.append({"type": "tool_result", "tool_use_id": block["id"], "is_error": True,
                                "content": f"tool-call cap ({max_tool_calls}) reached; finish now"})
                continue
            try:
                out = dispatch(ctl, block["name"], inp, reason)
                results.append({"type": "tool_result", "tool_use_id": block["id"], "content": json.dumps(out, default=str)[:6000]})
            except (KeyError, ValueError, TypeError) as e:
                results.append({"type": "tool_result", "tool_use_id": block["id"], "is_error": True, "content": f"Error: {e}"})
        messages.append({"role": "user", "content": results})
        if calls > max_tool_calls:
            ctl.stop(f"LLM tool-call cap of {max_tool_calls} reached")
            break
    if ctl.status == "running":
        unfinished = [n for n in ctl.steps if n not in ctl.step_results]
        ctl.status = "done" if not unfinished else "stopped"
        if unfinished:
            ctl.status_reason = f"the LLM finished without completing step(s) {unfinished}"

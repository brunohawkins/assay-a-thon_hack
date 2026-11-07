"""LLM operator: Claude runs the protocol by calling the six commands as tools.

STATUS: TODO. The loop below is written against the Claude Messages API but has NOT
been run against the live API yet. Before relying on it for the demo:
  - TODO run it once with a key and check the run file in the twin
  - TODO decide if narration should come from `reason` (current) or the model's text
  - TODO add retry/backoff on HTTP 429/5xx if we hit rate limits
  - TODO record a good LLM run and commit it, so the demo works offline

How it works
  - Tools: the six commands from commands.tool_definitions() (built from
    contracts/schemas/commands.schema.json), each with a `reason` field for narration.
  - System prompt: "you operate this chip, follow the protocol, explain each decision
    in one sentence."
  - Loop: send the protocol + chip spec, execute every tool call through the sim
    adapter, return results (or validation errors so the model can correct itself),
    and stop when the model ends its turn.
  - Each executed tool call becomes a run event, narration = the model's reason.

API key: read from the ANTHROPIC_API_KEY environment variable. Never commit it;
`.env` and `*.key` are in .gitignore. With no key set, run.py falls back to the
deterministic planner and says so.

Standard library only (urllib), per the repo rule of stdlib + jsonschema.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Callable

import commands
from adapters.sim import SimAdapter

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2026-10-21"
MODEL = os.environ.get("CAPSULE_LLM_MODEL", "claude-opus-5-5")
# Server-side fallback: if the model declines a request, the API retries it on a
# fallback model inside the same call. Remove both lines to opt out.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACKS = "default"
MAX_TURNS = 40
MAX_TOKENS = 16000

SYSTEM_PROMPT = """\
You operate a microfluidic capsule-processor chip. Cells sit inside small capsules; \
the capsules sit on mesh floors inside chambers while liquids are exchanged around them.

Your job: carry out the protocol you are given, step by step, on the chip you are given, \
using only the six tools (load, exchange, hold_temp, split, pool, release).

Rules:
- Follow the protocol steps in order. Use one tool call per protocol step and set \
protocol_step to that step's number.
- Use chamber ids exactly as they appear in the chip spec.
- For split, send capsules from the protocol's target chamber to the split chambers \
connected to it. For pool, bring capsules from the chambers connected to the pool chamber.
- In `reason`, explain the decision in one plain-English sentence for a non-expert audience.
- If a tool returns an error, fix the call and try again.
- When every step is done, reply with one short sentence and no tool call."""

ApiCall = Callable[[dict, str], dict]


class LLMError(RuntimeError):
    """The LLM run could not finish. run.py falls back to the planner on this."""


def call_messages_api(body: dict, api_key: str) -> dict:
    """POST one Messages API request and return the parsed JSON response."""
    headers = {
        "content-type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": API_VERSION,
    }
    if body.get("fallbacks"):
        headers["anthropic-beta"] = FALLBACK_BETA
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:500]
        raise LLMError(f"Claude API returned HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise LLMError(f"could not reach the Claude API: {e.reason}") from e


def _user_prompt(protocol: dict, chip: dict) -> str:
    return (
        "Here is the chip spec (millimetres, microlitres, seconds, degrees C, uL/min):\n"
        f"<chip_spec>\n{json.dumps(chip, indent=2)}\n</chip_spec>\n\n"
        "Here is the protocol to run:\n"
        f"<protocol>\n{json.dumps(protocol, indent=2)}\n</protocol>\n\n"
        "Run the protocol now, one tool call per step."
    )


def run_llm(protocol: dict, chip: dict, *, api_key: str, model: str = MODEL,
            call_api: ApiCall = call_messages_api) -> SimAdapter:
    """Let Claude operate the chip. Returns the sim adapter holding the recorded events."""
    adapter = SimAdapter(chip)
    step_duration = {s["step"]: s["duration_s"] for s in protocol["steps"]}
    messages: list[dict] = [{"role": "user", "content": _user_prompt(protocol, chip)}]
    body_base = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "tools": commands.tool_definitions(),
        "tool_choice": {"type": "auto"},
        "output_config": {"effort": "medium"},
    }
    if FALLBACKS:
        body_base["fallbacks"] = FALLBACKS

    for _turn in range(MAX_TURNS):
        response = call_api({**body_base, "messages": messages}, api_key)
        stop = response.get("stop_reason")
        content = response.get("content") or []
        # Keep the assistant turn exactly as returned (including thinking blocks).
        messages.append({"role": "assistant", "content": content})

        if stop == "refusal":
            raise LLMError("the model declined this request (stop_reason=refusal)")
        if stop == "max_tokens":
            raise LLMError("response hit max_tokens; tool calls may be truncated, not executing them")

        tool_uses = [b for b in content if b.get("type") == "tool_use"]
        if not tool_uses:
            if stop == "end_turn":
                break
            raise LLMError(f"unexpected stop_reason {stop!r} with no tool calls")

        text = " ".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        results = []
        # Execute in the order the model asked, then return all results in one message.
        for block in tool_uses:
            args = dict(block.get("input") or {})
            reason = str(args.pop("reason", "") or text)
            step = args.pop("protocol_step", None)
            try:
                result = adapter.send(block["name"], args, narration=reason,
                                      protocol_step=step if isinstance(step, int) else None,
                                      duration_s=step_duration.get(step))
                results.append({"type": "tool_result", "tool_use_id": block["id"],
                                "content": json.dumps(result)})
            except commands.CommandError as e:
                results.append({"type": "tool_result", "tool_use_id": block["id"],
                                "content": f"Error: {e}", "is_error": True})
        messages.append({"role": "user", "content": results})
    else:
        raise LLMError(f"stopped after {MAX_TURNS} turns without finishing")

    if not adapter.events:
        raise LLMError("the model finished without sending any commands")
    return adapter


if __name__ == "__main__":
    import run

    raise SystemExit(run.main(["--llm", *sys.argv[1:]]))

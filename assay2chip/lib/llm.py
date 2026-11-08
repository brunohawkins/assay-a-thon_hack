"""The ONE interface for language-model calls in assay2chip.

Two modes, and which one ran is recorded in the stage file so a stubbed result can never
be mistaken for a real extraction:

  stub  (the default)  No network, ever. The caller's own deterministic fallback is
                       returned instead of a model answer. This is what tests run.
  real  (opt in)       A real model call. Chosen only by ASSAY2CHIP_LLM=real or
                       `assay2chip/run.py --llm real`, never automatically, and it fails
                       loudly if no client or key is available rather than quietly
                       falling back to the stub.

Every call needs a `stub=` fallback. That is deliberate: a stage that cannot work without
a model does not have a deterministic path, and assay2chip/README.md requires one.

    from assay2chip.lib import llm

    answer = llm.complete_json(prompt, task="s1_extract_steps", stub={"steps": []})
    data["llm"] = llm.provenance()      # goes straight into the file's `llm` block

`provenance()` matches the `llm` object in contracts/schemas/{assay,unitops,partition}.
schema.json: mode, model, calls, tasks, prompt_sha256.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

MODE_ENV = "ASSAY2CHIP_LLM"
MODEL_ENV = "ASSAY2CHIP_LLM_MODEL"
DEFAULT_MODEL = "claude-sonnet-4-5"
STUB, REAL = "stub", "real"
MODES = (STUB, REAL)


class LLMUnavailable(RuntimeError):
    """Real mode was asked for and no model could be reached.

    Never caught and turned into a stub inside this module: a stage that silently
    degraded from real to stub would put model-shaped guesses in a file labelled `real`.
    """


@dataclass
class Result:
    """One model answer, with how it was produced."""

    text: str
    mode: str
    task: str
    model: str | None
    prompt_sha256: str
    stubbed: bool

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"{self.mode} answer for task '{self.task}' is not JSON "
                f"(line {e.lineno}, column {e.colno}: {e.msg})"
            ) from None


@dataclass
class _State:
    mode: str | None = None
    model: str | None = None
    calls: int = 0
    stub_calls: int = 0
    tasks: list[str] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)


_state = _State()


def configure(mode: str | None = None, model: str | None = None) -> str:
    """Set the mode for this process (run.py calls it once, from --llm). Returns the mode."""
    if mode is not None:
        if mode not in MODES:
            raise ValueError(f"llm mode must be one of {MODES}, not {mode!r}")
        _state.mode = mode
    if model is not None:
        _state.model = model
    return current_mode()


def current_mode() -> str:
    """'stub' or 'real'. Stub unless something explicitly asked for real."""
    if _state.mode is not None:
        return _state.mode
    env = os.environ.get(MODE_ENV, "").strip().lower()
    if env in MODES:
        return env
    if env in ("", "off", "none", "offline", "0", "false"):
        return STUB
    raise ValueError(f"{MODE_ENV}={env!r} is not understood; use 'stub' or 'real'")


def is_stub() -> bool:
    return current_mode() == STUB


def model_name() -> str:
    return _state.model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL


def reset() -> None:
    """Forget the call ledger (not the mode). Call between stages."""
    _state.calls = 0
    _state.stub_calls = 0
    _state.tasks = []
    _state.prompts = []


def prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def stub_text(prompt: str, task: str) -> str:
    """The deterministic marker a stub answer carries when the caller gave no fallback.

    Same prompt, same string, every machine, no network.
    """
    return f"[stub:{task}:{prompt_sha256(prompt)[:12]}]"


def complete(
    prompt: str,
    *,
    task: str,
    stub: Any,
    system: str | None = None,
    model: str | None = None,
    max_tokens: int = 4096,
) -> Result:
    """Ask the model, or return the deterministic fallback in stub mode.

    task    short name for this prompt, e.g. 's1_extract_steps'. It goes in the
            provenance and into the stub marker, so keep it stable.
    stub    the fallback: a string, a JSON-able object, or a zero-argument callable
            returning either. REQUIRED - there is no model-only path.
    """
    if not task:
        raise ValueError("every llm call needs a task name, for the provenance")
    mode = current_mode()
    sha = prompt_sha256((system or "") + "\n" + prompt)
    _state.calls += 1
    _state.tasks.append(task)
    _state.prompts.append(sha)

    if mode == STUB:
        _state.stub_calls += 1
        value = stub() if callable(stub) else stub
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return Result(text, STUB, task, None, sha, True)

    name = model or model_name()
    text = _call_real(prompt, system=system, model=name, max_tokens=max_tokens, task=task)
    return Result(text, REAL, task, name, sha, False)


def complete_json(prompt: str, *, task: str, stub: Any, **kwargs) -> Any:
    """`complete` plus JSON parsing. In stub mode `stub` comes back unchanged."""
    result = complete(prompt, task=task, stub=stub, **kwargs)
    if result.stubbed and not isinstance(stub, str):
        return stub() if callable(stub) else stub
    return result.json()


def provenance(note: str | None = None) -> dict:
    """The `llm` block for a stage file. Put it in every file a model touched.

    mode is 'none' when no call was made at all, 'stub' when any call was stubbed, and
    'real' only when every call in this stage really went to a model.
    """
    if _state.calls == 0:
        mode = "none"
    elif _state.stub_calls:
        mode = STUB
    else:
        mode = REAL
    out: dict[str, Any] = {
        "mode": mode,
        "model": model_name() if mode == REAL else None,
        "calls": _state.calls,
        "tasks": sorted(set(_state.tasks)),
        "prompt_sha256": _state.prompts[-1] if _state.prompts else None,
    }
    if mode == STUB:
        out["note"] = note or (
            "Deterministic offline stub, no model was called: treat every number here as "
            "made up and do not cite it."
        )
    elif note:
        out["note"] = note
    return out


def _call_real(prompt: str, *, system: str | None, model: str, max_tokens: int, task: str) -> str:
    """The only place that touches the network. Unreachable in stub mode."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise LLMUnavailable(
            f"task '{task}' asked for a real model call but ANTHROPIC_API_KEY is not set. "
            f"Set it, or run in the default stub mode ({MODE_ENV}=stub)."
        )
    try:
        import anthropic  # noqa: PLC0415 - imported only in real mode, so tests never need it
    except ImportError:
        raise LLMUnavailable(
            f"task '{task}' asked for a real model call but the 'anthropic' package is not "
            f"installed (pip install anthropic). Stub mode needs no package."
        ) from None
    client = anthropic.Anthropic(api_key=key)
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system or "",
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(part.text for part in message.content if getattr(part, "type", "") == "text")

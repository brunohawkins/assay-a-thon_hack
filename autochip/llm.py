"""One way for every stage to ask an agent for JSON, check it, and send it back to fix.

Backends (pick with --backend, default auto):
  api        Claude Messages API over urllib. Needs ANTHROPIC_API_KEY. Model from --model or AUTOCHIP_MODEL.
  claude-cli The Claude Code CLI (`claude -p`), using whoever is logged in on this machine. No API key.
  rules      No agent. ask_json() returns None and each stage falls back to its built-in rules.
  replay     Read saved answers from a folder (<replay_dir>/<stage>[__<key>].json), e.g. a previous run's
             agent/ folder. Lets the demo run offline with the exact output a real agent produced.
  auto       api if ANTHROPIC_API_KEY is set, else claude-cli if `claude` is on PATH, else rules.

The loop: the stage gives a system prompt, a user prompt and a validate(obj) -> [errors] function.
The agent answers with one JSON object; if validate() finds problems they go back to the agent
with its own answer ("fix these"), up to max_repairs times. Every exchange is written to
<log_dir>/<stage>[__<key>].log.json, and the accepted answer to <log_dir>/<stage>[__<key>].json
(which is exactly what the replay backend reads). When no valid answer comes back, the stale
accepted answer of an earlier run is removed so a later replay can't pick it up.

Standard library only, like operator/llm_operator.py.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = os.environ.get("AUTOCHIP_MODEL", "claude-opus-5-5")
MAX_TOKENS = 32000
BACKENDS = ("auto", "api", "claude-cli", "rules", "replay")
RETRY_STATUS = (429, 500, 502, 503, 504, 529)      # API: overloaded / rate limited / transient
RETRY_DELAYS_S = (5, 20)                            # waits before the 2nd and 3rd try
TRUNCATED = ("your reply was cut off at the output limit before the JSON object was complete: reply with the "
             "complete JSON object again, shorter (trim free-text fields such as why / notes)")

Validator = Callable[[dict], list]


class AgentError(RuntimeError):
    pass


_FENCE_OPEN = re.compile(r"```[ \t]*[A-Za-z]*[ \t]*\r?\n?[ \t]*$")


def _json_objects(text: str) -> list[tuple[int, int, dict]]:
    """Every top-level JSON object in text as (start, end, obj). Braces inside JSON strings are handled
    by the decoder, so `{"a": "x}"}` is one object; objects nested in a found one are not listed."""
    dec = json.JSONDecoder()
    found, i = [], text.find("{")
    while i != -1:
        try:
            obj, end = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
            continue
        if isinstance(obj, dict):
            found.append((i, end, obj))
        i = text.find("{", end)
    return found


def extract_json(text: str) -> dict:
    """The answer object in a reply. Prose before or after, ``` fences (json or not) and braces inside
    strings are all fine. With several objects, one that opens a ``` fence wins over unfenced ones, and
    the largest wins among those (a short example in the prose doesn't beat the answer)."""
    if not text or not text.strip():
        raise AgentError("the reply was empty: reply with the JSON object")
    found = _json_objects(text)
    if not found:
        hint = "it starts with '{' but is not complete or not valid JSON" if "{" in text else "no '{' in it"
        raise AgentError(f"no JSON object in the reply ({hint}): reply with one complete JSON object")
    fenced = [f for f in found if _FENCE_OPEN.search(text, 0, f[0])]
    return max(fenced or found, key=lambda f: f[1] - f[0])[2]


class Agent:
    def __init__(self, backend: str = "auto", model: str | None = None, log_dir: Path | None = None,
                 replay_dir: Path | None = None, max_repairs: int = 2, timeout_s: int = 900, quiet: bool = False):
        if backend not in BACKENDS:
            raise ValueError(f"backend must be one of {BACKENDS}")
        if backend == "auto":
            if os.environ.get("ANTHROPIC_API_KEY"):
                backend = "api"
            elif shutil.which("claude"):
                backend = "claude-cli"
            else:
                backend = "rules"
        if backend == "replay":
            if not replay_dir:
                raise ValueError("the replay backend needs a folder of saved answers (--replay <earlier out>/agent)")
            if not Path(replay_dir).is_dir():
                raise ValueError(f"replay folder {replay_dir} does not exist")
        self.backend = backend
        # --model applies to both live backends; the CLI falls back to AUTOCHIP_CLI_MODEL, then its own default.
        self.model = model or DEFAULT_MODEL
        self.cli_model = model or os.environ.get("AUTOCHIP_CLI_MODEL") or None
        self.log_dir = Path(log_dir) if log_dir else None
        self.replay_dir = Path(replay_dir) if replay_dir else None
        self.max_repairs = max_repairs
        self.timeout_s = timeout_s
        self.quiet = quiet
        self._meta: dict = {}           # facts about the last call (stop reason, model, cost) for the log
        if self.log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    @property
    def live(self) -> bool:
        return self.backend in ("api", "claude-cli")

    @property
    def model_used(self) -> str | None:
        return {"api": self.model, "claude-cli": self.cli_model}.get(self.backend)

    def describe(self) -> str:
        return {"api": f"Claude API ({self.model})",
                "claude-cli": "Claude Code CLI (claude -p" + (f", {self.cli_model})" if self.cli_model else ")"),
                "rules": "built-in rules (no agent)", "replay": f"replay of {self.replay_dir}"}[self.backend]

    # ------------------------------------------------------------------ calls
    def _say(self, msg: str) -> None:
        if not self.quiet:
            print(f"  [agent] {msg}", flush=True)

    def _call_api(self, system: str, messages: list[dict]) -> str:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise AgentError("ANTHROPIC_API_KEY is not set")
        body = {"model": self.model, "max_tokens": MAX_TOKENS, "system": system, "messages": messages}
        req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST", headers={
            "content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"})
        for attempt in range(len(RETRY_DELAYS_S) + 1):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                    raw = r.read()
                break
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                if e.code in RETRY_STATUS and attempt < len(RETRY_DELAYS_S):
                    wait = RETRY_DELAYS_S[attempt]
                    try:
                        wait = min(max(wait, float(e.headers.get("retry-after") or 0)), 60)
                    except (TypeError, ValueError):
                        pass
                    self._say(f"API error {e.code}, trying again in {wait:g}s")
                    time.sleep(wait)
                    continue
                raise AgentError(f"API error {e.code}: {detail}") from e
            except urllib.error.URLError as e:
                if attempt < len(RETRY_DELAYS_S) and not isinstance(e.reason, TimeoutError):
                    self._say(f"API unreachable ({e.reason}), trying again in {RETRY_DELAYS_S[attempt]}s")
                    time.sleep(RETRY_DELAYS_S[attempt])
                    continue
                raise AgentError(f"API unreachable: {e.reason}") from e
            except TimeoutError as e:       # the read timed out (urlopen only wraps connect errors in URLError)
                raise AgentError(f"API call timed out after {self.timeout_s}s") from e
            except OSError as e:
                raise AgentError(f"API connection failed: {e}") from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise AgentError(f"API sent something that is not JSON: {raw[:300]!r}") from e
        self._meta = {"stop_reason": data.get("stop_reason"), "model": data.get("model"), "usage": data.get("usage")}
        if data.get("stop_reason") == "refusal":
            raise AgentError("the model declined this request")
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

    def _cli_cmd(self, system: str) -> list[str]:
        # --system-prompt replaces Claude Code's own (coding-agent) system prompt, so the agent sees what the
        # log says it saw, as with the API; --tools "" gives it no tools (it only has to answer).
        cmd = ["claude", "-p", "--output-format", "json", "--system-prompt", system, "--tools", "",
               "--no-session-persistence"]
        if self.cli_model:
            cmd += ["--model", self.cli_model]
        return cmd

    def _call_cli(self, system: str, messages: list[dict]) -> str:
        # The CLI is stateless per call here: fold the conversation into one prompt.
        parts = []
        for m in messages:
            who = "REQUEST" if m["role"] == "user" else "YOUR PREVIOUS ANSWER"
            parts.append(f"=== {who} ===\n{m['content']}")
        prompt = "\n\n".join(parts) + "\n\nReply with the JSON object only."
        # Run in an empty folder: the CLI would otherwise read the CLAUDE.md files of wherever autochip was
        # started into the conversation.
        with tempfile.TemporaryDirectory(prefix="autochip_cli_") as cwd:
            try:
                r = subprocess.run(self._cli_cmd(system), input=prompt, capture_output=True, text=True,
                                   timeout=self.timeout_s, cwd=cwd)
            except FileNotFoundError as e:
                raise AgentError("the claude CLI is not on PATH (install Claude Code, or use --backend api / rules)") from e
            except subprocess.TimeoutExpired as e:
                raise AgentError(f"claude CLI timed out after {self.timeout_s}s") from e
        out = _cli_result(r.stdout)
        if r.returncode != 0 or out is None or out.get("is_error"):
            msg = (out or {}).get("result") or r.stderr or r.stdout
            raise AgentError(f"claude CLI failed (exit {r.returncode}"
                             + (f", {out.get('subtype')}" if out and out.get("subtype") else "") + f"): {str(msg).strip()[:500]}")
        self._meta = {"stop_reason": out.get("stop_reason"), "model": self.cli_model,
                      "cost_usd": out.get("total_cost_usd"), "usage": out.get("usage")}
        return out.get("result") or ""

    def _call(self, system: str, messages: list[dict]) -> str:
        self._meta = {}
        return self._call_api(system, messages) if self.backend == "api" else self._call_cli(system, messages)

    # ------------------------------------------------------------------ public
    def ask_json(self, stage: str, system: str, user: str, validate: Validator | None = None,
                 key: str | None = None) -> dict | None:
        """Returns the accepted JSON object, or None for the rules backend (caller uses its fallback).
        Raises AgentError if the agent can't produce a valid answer."""
        name = stage + (f"__{key}" if key else "")
        if self.backend == "rules":
            return None
        if self.backend == "replay":
            return self._replay(name, validate)

        messages = [{"role": "user", "content": user}]
        log = []
        last_errors: list = []
        for attempt in range(self.max_repairs + 1):
            t0 = time.time()
            self._say(f"{name}: asking {self.describe()}" + (f" (repair {attempt})" if attempt else ""))
            try:
                text = self._call(system, messages)
            except AgentError as e:
                log.append({"attempt": attempt, "seconds": round(time.time() - t0, 1), "error": str(e)})
                self._save(name, None, log, system=system, user=user)
                raise
            entry = {"attempt": attempt, "seconds": round(time.time() - t0, 1), "reply": text}
            entry.update({k: v for k, v in self._meta.items() if v is not None})
            obj, errs = None, []
            if self._meta.get("stop_reason") == "max_tokens":
                errs = [TRUNCATED]          # any object found in a cut-off reply is a fragment of the answer
            else:
                try:
                    obj = extract_json(text)
                except AgentError as e:
                    errs = [str(e)]
            if obj is not None and validate:
                errs = _run_validator(validate, obj)
            entry["errors"] = [str(e) for e in errs]
            log.append(entry)
            if not errs:
                self._save(name, obj, log, system=system, user=user)
                return obj
            last_errors = errs
            self._say(f"{name}: {len(errs)} problem(s), sending back: {str(errs[0])[:120]}")
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": "Your answer has these problems. Fix all of them and reply with the "
                 "complete corrected JSON object only:\n- " + "\n- ".join(str(e) for e in errs[:30])}]
        self._save(name, None, log, system=system, user=user)
        raise AgentError(f"{name}: no valid answer after {self.max_repairs + 1} tries: {last_errors[:5]}")

    def _replay(self, name: str, validate: Validator | None) -> dict:
        path = self.replay_dir / f"{name}.json"
        if not path.exists():
            raise AgentError(f"replay: no saved answer {path}")
        try:
            obj = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            raise AgentError(f"replay: saved answer {path} is not valid JSON ({e})") from e
        if not isinstance(obj, dict):
            raise AgentError(f"replay: saved answer {path} is not a JSON object")
        errs = _run_validator(validate, obj) if validate else []
        if errs:
            raise AgentError(f"replay: saved answer {path} fails validation: {errs[:5]}")
        if self.log_dir and self.log_dir.resolve() != self.replay_dir.resolve():
            # Copy the answer and the original exchange (what the agent saw and said), marked as replayed.
            (self.log_dir / f"{name}.json").write_text(json.dumps(obj, indent=2))
            src_log = self.replay_dir / f"{name}.log.json"
            try:
                rec = json.loads(src_log.read_text()) if src_log.exists() else {}
            except json.JSONDecodeError:
                rec = {}
            rec = rec if isinstance(rec, dict) else {}
            rec.setdefault("exchanges", [])
            rec["replayed_from"] = str(path)
            (self.log_dir / f"{name}.log.json").write_text(json.dumps(rec, indent=2))
        # Replaying a folder into itself: answer and original log are already there; leave them alone.
        return obj

    def _save(self, name: str, obj, log: list, system: str | None = None, user: str | None = None) -> None:
        if not self.log_dir:
            return
        answer = self.log_dir / f"{name}.json"
        if obj is not None:
            answer.write_text(json.dumps(obj, indent=2))
        elif answer.exists():
            answer.unlink()         # an earlier run's answer would no longer match this log or this run
        (self.log_dir / f"{name}.log.json").write_text(json.dumps(
            {"backend": self.backend, "model": self.model_used, "accepted": obj is not None,
             "system": system, "user": user, "exchanges": log}, indent=2))


def _run_validator(validate: Validator, obj: dict) -> list:
    """validate(obj), with a crash turned into an error the agent can act on (it usually means a field is
    missing or has the wrong type) instead of an exception that loses the exchange."""
    try:
        return list(validate(obj) or [])
    except Exception as e:  # noqa: BLE001 - any validator bug or shape surprise goes back as a problem
        return [f"the answer could not be checked ({type(e).__name__}: {str(e)[:200]}): it probably misses a "
                f"required field or has a value of the wrong type; follow the schema exactly"]


def _cli_result(stdout: str) -> dict | None:
    """The result record of `claude -p --output-format json`: one JSON object, or (with --verbose / older
    versions) a list of messages ending in it; anything printed around it is skipped."""
    for line in reversed([ln for ln in (stdout or "").splitlines() if ln.strip()] + [stdout or ""]):
        try:
            out = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(out, list):
            out = next((m for m in reversed(out) if isinstance(m, dict) and m.get("type") == "result"), None)
        if isinstance(out, dict) and ("result" in out or out.get("type") == "result" or "is_error" in out):
            return out
    return None


def schema_errors(obj: dict, schema: dict) -> list[str]:
    """Readable jsonschema errors (path: message), for validate() functions."""
    import jsonschema
    v = jsonschema.Draft202012Validator(schema)
    out = []
    for e in sorted(v.iter_errors(obj), key=lambda e: list(e.path)):
        path = "/".join(str(p) for p in e.path) or "(top)"
        out.append(f"{path}: {e.message[:300]}")
    return out

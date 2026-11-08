"""Which language-model mode the server is in, reported honestly.

`assay2chip/lib/llm.py` is the pipeline's interface and the authority; this is the
read-only view of it the API needs, reimplemented rather than imported because the server
is standard-library only and must answer /api/health even when the pipeline's
dependencies are not installed. It agrees with that module on the names: ASSAY2CHIP_LLM,
the two modes `stub` and `real`, and real mode needing ANTHROPIC_API_KEY.

The rule this module exists to enforce: a stubbed answer is never presented as a real
extraction. `real` is reported only when a real call could actually be made - mode asked
for AND a key present. Anything else reports `stub`, with a note saying why.
"""

from __future__ import annotations

import os
from typing import Optional

MODE_ENV = "ASSAY2CHIP_LLM"
KEY_ENV = "ANTHROPIC_API_KEY"
MODEL_ENV = "ASSAY2CHIP_LLM_MODEL"
STUB, REAL = "stub", "real"
MODES = (STUB, REAL)

STUB_NOTE = ("deterministic offline stub: no model is called, so every extracted value is "
             "a fallback, not something read out of the source")


def requested(value: Optional[str] = None) -> str:
    """The mode asked for: the request's `options.llm`, else the environment, else stub."""
    if value is not None and str(value).strip():
        mode = str(value).strip().lower()
        if mode not in MODES:
            raise ValueError("llm must be one of %s, not %r" % (", ".join(MODES), value))
        return mode
    env = os.environ.get(MODE_ENV, "").strip().lower()
    return env if env in MODES else STUB


def key_present() -> bool:
    return bool(os.environ.get(KEY_ENV, "").strip())


def report(value: Optional[str] = None) -> dict:
    """{mode, requested, api_key_present, model, note} - what /api/health and a run show.

    `mode` is the honest one: `real` only if a real call is actually possible.
    """
    asked = requested(value)
    real_possible = asked == REAL and key_present()
    mode = REAL if real_possible else STUB
    out = {
        "mode": mode,
        "requested": asked,
        "api_key_present": key_present(),
        "model": os.environ.get(MODEL_ENV) if real_possible else None,
    }
    if mode == STUB:
        out["note"] = STUB_NOTE if asked == STUB else (
            "%s was asked for but %s is not set, so this would run stubbed"
            % (REAL, KEY_ENV))
    return out


def mode(value: Optional[str] = None) -> str:
    return report(value)["mode"]

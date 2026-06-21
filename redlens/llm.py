"""One small LLM completion over raw HTTP.

redlens talks to any OpenAI-compatible chat-completions endpoint — OpenAI by
default, or whatever ``[llm] base_url`` points at (another hosted provider, a
local server, a gateway). One wire format means one code path and no
provider-specific branching. Stdlib-only by design, so no SDK.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any

from redlens import config, constants
from redlens.errors import RedlensError


def model_name() -> str:
    """The model a completion will use — ``[llm] model`` or the default.
    Exposed so callers can report which model produced an output."""
    return config.load_config().get("llm", {}).get("model") or constants.DEFAULT_LLM_MODEL


def complete(prompt: str, api_key: str, *,
             max_tokens: int = constants.LLM_MAX_TOKENS,
             json_object: bool = False) -> str:
    """Return the text of one completion for ``prompt``.

    ``json_object`` turns on the API's JSON mode (``response_format``), which
    guarantees the reply is a syntactically valid JSON object — supported by
    gpt-4o-mini and most OpenAI-compatible servers, and requires the prompt to
    mention "json" (our JSON prompts do). Callers still parse defensively, so an
    endpoint that ignores the field degrades rather than breaks.
    """
    settings = config.load_config().get("llm", {})
    url = settings.get("base_url") or constants.LLM_API_URL
    body: dict[str, Any] = {
        "model": settings.get("model") or constants.DEFAULT_LLM_MODEL,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if json_object:
        body["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=constants.HTTP_TIMEOUT_S) as r:
            data = json.loads(r.read())
    except Exception as exc:
        raise RedlensError(f"LLM request failed: {exc}") from exc
    return str(data["choices"][0]["message"]["content"])

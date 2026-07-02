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
    choice = data["choices"][0]
    # JSON mode guarantees valid syntax only if the reply wasn't cut off by
    # max_tokens; a length truncation yields half an object. Flag it clearly
    # (callers degrade on RedlensError) instead of a downstream "invalid JSON".
    if json_object and choice.get("finish_reason") == "length":
        raise RedlensError(
            f"LLM reply truncated at max_tokens={max_tokens}; raise the limit")
    return str(choice["message"]["content"])


def parse_json(raw: str) -> dict[str, Any]:
    """The JSON object from a completion, tolerant of markdown fences/prose
    around it (we take the outermost ``{...}``)."""
    i, j = raw.find("{"), raw.rfind("}")
    if i == -1 or j <= i:
        raise RedlensError("LLM did not return a JSON object")
    try:
        obj = json.loads(raw[i:j + 1])
    except json.JSONDecodeError as exc:
        raise RedlensError(f"LLM returned invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise RedlensError("LLM JSON was not an object")
    return obj


def complete_json(prompt: str, api_key: str, *,
                  max_tokens: int = constants.SUMMARY_MAX_TOKENS) -> dict[str, Any]:
    """One JSON-mode completion, parsed to a dict — the call every structured
    extractor shares (same token budget and defensive parse)."""
    raw = complete(prompt, api_key, max_tokens=max_tokens, json_object=True)
    return parse_json(raw)

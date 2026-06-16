"""One small LLM completion over raw HTTP.

This package is stdlib-only by design — no provider SDKs — so a completion is
a single ``urllib`` POST. Provider is chosen by key shape (``sk-ant`` →
Anthropic Messages API, else an OpenAI-compatible chat-completions endpoint),
overridable via ``[llm] provider``/``model`` in ``config.toml``. Both the
discovery subreddit-suggester and the profile summarizer call through here.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any

from redlens import config, constants
from redlens.errors import RedlensError


def provider_and_model(api_key: str) -> tuple[str, str]:
    """The (provider, model) a completion will use for ``api_key``, honoring
    the ``[llm]`` config overrides. Exposed so callers can record which model
    produced an output (the summary table stores it)."""
    settings = config.load_config().get("llm", {})
    provider = settings.get("provider") or (
        "anthropic" if api_key.startswith("sk-ant") else "openai"
    )
    default = (constants.DEFAULT_ANTHROPIC_MODEL if provider == "anthropic"
               else constants.DEFAULT_OPENAI_MODEL)
    return provider, settings.get("model") or default


def complete(prompt: str, api_key: str, *,
             max_tokens: int = constants.LLM_MAX_TOKENS) -> str:
    """Return the text of one completion for ``prompt``."""
    provider, model = provider_and_model(api_key)
    if provider == "anthropic":
        url = constants.ANTHROPIC_URL
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    else:
        url = constants.OPENAI_URL
        headers = {"Authorization": f"Bearer {api_key}"}
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=constants.HTTP_TIMEOUT_S) as r:
            data = json.loads(r.read())
    except Exception as exc:
        raise RedlensError(f"LLM {provider} request failed: {exc}") from exc
    if provider == "anthropic":
        return "".join(
            block.get("text", "") for block in data.get("content", [])
            if block.get("type") == "text"
        )
    return str(data["choices"][0]["message"]["content"])

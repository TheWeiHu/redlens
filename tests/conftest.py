"""Shared, hermetic-by-default test setup.

redlens reads an LLM API key from ``REDLENS_LLM_API_KEY`` / ``OPENAI_API_KEY`` or
a config file, and several features (``summarize``, and now ``track``'s relevance
filter) change behavior — and make real network calls — when one is present. CI
runs keyless, so the suite must too: a developer with a key in their environment or
config shouldn't get different, network-touching test runs.

This autouse fixture neutralizes both sources for every test — env vars cleared and
``REDLENS_CONFIG`` pointed at a nonexistent file — so ``llm_api_key()`` returns
``None`` unless a test explicitly opts in with ``monkeypatch.setenv(...)`` (which
runs after this fixture and so wins). Tests that exercise an LLM path set a dummy
key and monkeypatch ``llm.complete``.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _hermetic_llm_env(monkeypatch, tmp_path):
    monkeypatch.setenv("REDLENS_CONFIG", str(tmp_path / "no-such-config.toml"))
    for var in ("REDLENS_LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)

"""Shared test setup: isolate the suite from the developer's environment.

A shell exporting OPENAI_API_KEY would make the relevance filter (and any
summary path) issue REAL LLM calls mid-test — slow, paid, and nondeterministic.
Likewise REDLENS_DB/REDLENS_CONFIG would point tests at a developer's live
database or config. Delete them all up front; tests that need one set it
explicitly with monkeypatch.setenv, which runs after this autouse delenv and
therefore wins.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OPENAI_API_KEY", "REDLENS_LLM_API_KEY",
                "REDLENS_DB", "REDLENS_CONFIG"):
        monkeypatch.delenv(var, raising=False)

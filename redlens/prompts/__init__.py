"""Prompt templates passed to the LLM, kept out of the code that builds them.

Each ``<name>.txt`` here is a full prompt with ``$placeholder`` slots
(:class:`string.Template` syntax). Callers fill the slots with
:func:`render`; the data assembly stays in the feature module, the wording
lives here so it can be read and tuned in one place.
"""
from __future__ import annotations

from pathlib import Path
from string import Template

_DIR = Path(__file__).resolve().parent


def load(name: str) -> str:
    """The raw template text for ``<name>.txt``."""
    return (_DIR / f"{name}.txt").read_text(encoding="utf-8")


def render(name: str, /, **values: str) -> str:
    """``<name>.txt`` with its ``$placeholders`` filled. Uses safe substitution
    so a stray ``$`` in injected user content can't blow up rendering."""
    return Template(load(name)).safe_substitute(values)

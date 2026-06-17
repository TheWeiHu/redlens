"""First-run setup: optionally collect an API key, never ask twice.

The key is optional and nothing requires it: arctic-shift needs no key at all.
An LLM key adds AI profile summaries and the ``llm`` discovery source.
``redlens setup`` runs the wizard explicitly; the first interactive run offers
it once, and either way leaves a config file behind so the offer never repeats.
Non-interactive runs (pipes, cron, CI) are never prompted.
"""
from __future__ import annotations

import getpass
import sys
from typing import Any

from redlens.config import config_path, save_config

# The LLM key is live today — `summarize` and the `llm` discovery source both
# read it. While False, the first-run offer is silent and the `setup`
# subcommand is not registered.
ENABLED = True

FIRST_RUN_MARKER = (
    "# redlens configuration — see `redlens setup` to add or change\n"
    "# an optional LLM API key for AI summaries.\n"
)


def run_wizard() -> int:
    print("redlens setup — the LLM key is optional; Enter skips.\n")

    updates: dict[str, dict[str, Any]] = {}

    print("LLM API key — AI profile summaries (OpenAI or any compatible API).")
    llm_key = getpass.getpass("  API key (Enter to skip): ").strip()
    if llm_key:
        updates["llm"] = {"api_key": llm_key}

    path = save_config(updates)
    what = "LLM key saved" if "llm" in updates else "no key saved (that's fine)"
    print(f"\n{what} → {path}")
    return 0


def offer_setup_on_first_run() -> None:
    """On the very first interactive run, offer the wizard — exactly once.

    The config file's existence is the "already asked" marker, so declining
    writes a comment-only file. Skipped entirely when stdin/stdout is not a
    terminal.
    """
    if not ENABLED:
        return
    if config_path().exists():
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    answer = input(
        "First run! Add an optional LLM API key for AI summaries? [y/N] "
    ).strip().lower()
    if answer in ("y", "yes"):
        run_wizard()
    else:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.write_text(FIRST_RUN_MARKER, encoding="utf-8")
        print("ok — run `redlens setup` anytime to add it\n")

"""First-run setup: optionally collect API keys, never ask twice.

Both keys are optional and nothing requires them: arctic-shift needs no key
at all. A Reddit API key adds fresh data via the official API; an LLM key
adds AI profile summaries. ``redlens setup`` runs the wizard explicitly;
the first interactive run offers it once, and either way leaves a config
file behind so the offer never repeats. Non-interactive runs (pipes, cron,
CI) are never prompted.
"""
from __future__ import annotations

import getpass
import sys
from typing import Any

from redlens.config import config_path, save_config

# The LLM key is live today — `summarize` and the `llm` discovery source both
# read it. The Reddit key is collected ahead of the forthcoming Reddit provider
# (queue task 0001); it is stored but not consumed until that lands. Enabled now
# that there's a real payoff. While False, the first-run offer is silent and the
# `setup` subcommand is not registered.
ENABLED = True

FIRST_RUN_MARKER = (
    "# redlens configuration — see `redlens setup` to add or change\n"
    "# optional API keys (Reddit for fresh data, LLM for AI summaries).\n"
)


def run_wizard() -> int:
    print("redlens setup — both keys are optional; Enter skips.\n")

    updates: dict[str, dict[str, Any]] = {}

    print("Reddit API key — fresh data via Reddit's official API.")
    print("Create a 'script' app at https://www.reddit.com/prefs/apps")
    client_id = input("  client id (Enter to skip): ").strip()
    if client_id:
        client_secret = getpass.getpass("  client secret: ").strip()
        if client_secret:
            updates["reddit"] = {
                "client_id": client_id, "client_secret": client_secret,
            }
        else:
            print("  no secret given — skipping the Reddit key")

    print("\nLLM API key — AI profile summaries (OpenAI or any compatible API).")
    llm_key = getpass.getpass("  API key (Enter to skip): ").strip()
    if llm_key:
        updates["llm"] = {"api_key": llm_key}

    path = save_config(updates)
    saved = [s for s in ("reddit", "llm") if s in updates]
    what = " + ".join(saved) + " key saved" if saved else "no keys saved (that's fine)"
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
        "First run! Add optional API keys for fresh data / AI summaries? [y/N] "
    ).strip().lower()
    if answer in ("y", "yes"):
        run_wizard()
    else:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)
        path.write_text(FIRST_RUN_MARKER, encoding="utf-8")
        print("ok — run `redlens setup` anytime to add them\n")

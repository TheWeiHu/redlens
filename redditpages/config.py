"""Where the database and configuration live.

The DB path is resolved with this precedence (first hit wins):

1. ``--db`` flag
2. ``REDDITPAGES_DB`` env var
3. ``db`` under ``[storage]`` in the config file
4. the per-user data directory (e.g. ``~/.local/share/redditpages/`` on
   Linux, ``~/Library/Application Support/redditpages/`` on macOS)

The config file is ``config.toml`` in the per-user config directory
(override the file location with ``REDDITPAGES_CONFIG``). It is optional —
everything works with no config at all. Recognized so far:

    [storage]
    db = "/path/to/redditpages.db"

    [reddit]                  # optional: fresh data via Reddit's official API
    client_id = "..."
    client_secret = "..."

    [llm]                     # optional: AI profile summaries
    api_key = "..."

API keys can also come from the environment, which always wins over the
file: ``REDDITPAGES_REDDIT_CLIENT_ID`` / ``REDDITPAGES_REDDIT_CLIENT_SECRET``
and ``REDDITPAGES_LLM_API_KEY`` (falling back to ``ANTHROPIC_API_KEY`` /
``OPENAI_API_KEY``).
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir, user_data_dir

from redditpages.errors import RedditPagesError

APP_NAME = "redditpages"


def config_path() -> Path:
    env = os.environ.get("REDDITPAGES_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path(user_config_dir(APP_NAME)) / "config.toml"


def default_db_path() -> Path:
    return Path(user_data_dir(APP_NAME)) / "redditpages.db"


def load_config() -> dict[str, Any]:
    """The parsed config file, or {} if there is none."""
    path = config_path()
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise RedditPagesError(f"bad config {path}: {exc}") from exc


def resolve_db(flag: str | None = None) -> Path:
    if flag:
        return Path(flag).expanduser()
    env = os.environ.get("REDDITPAGES_DB")
    if env:
        return Path(env).expanduser()
    configured = load_config().get("storage", {}).get("db")
    if configured:
        return Path(str(configured)).expanduser()
    return default_db_path()


def _toml_dump(data: dict[str, dict[str, Any]]) -> str:
    """Serialize sections of scalar values. The stdlib can read TOML but not
    write it, and our config is flat enough that a real writer dependency
    is not worth it."""
    lines = []
    for section, values in data.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            if isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")
            elif isinstance(value, int | float):
                lines.append(f"{key} = {value}")
            else:
                escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{key} = "{escaped}"')
        lines.append("")
    return "\n".join(lines)


def save_config(updates: dict[str, dict[str, Any]]) -> Path:
    """Merge ``updates`` into the config file and write it with mode 600."""
    merged = load_config()
    for section, values in updates.items():
        merged.setdefault(section, {}).update(values)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.write_text(_toml_dump(merged))
    path.chmod(0o600)  # also tighten files that predate us
    return path


def reddit_credentials() -> tuple[str, str] | None:
    """(client_id, client_secret) for Reddit's official API, or None."""
    cid = os.environ.get("REDDITPAGES_REDDIT_CLIENT_ID")
    secret = os.environ.get("REDDITPAGES_REDDIT_CLIENT_SECRET")
    if not (cid and secret):
        section = load_config().get("reddit", {})
        cid = cid or section.get("client_id")
        secret = secret or section.get("client_secret")
    if cid and secret:
        return str(cid), str(secret)
    return None


def llm_api_key() -> str | None:
    """API key for AI summaries, from env or the config file."""
    for var in ("REDDITPAGES_LLM_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        if os.environ.get(var):
            return os.environ[var]
    key = load_config().get("llm", {}).get("api_key")
    return str(key) if key else None

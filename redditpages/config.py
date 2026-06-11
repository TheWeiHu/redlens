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

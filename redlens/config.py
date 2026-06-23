"""Where the database and configuration live.

The DB path is resolved with this precedence (first hit wins):

1. ``--db`` flag
2. ``REDLENS_DB`` env var
3. ``db`` under ``[storage]`` in the config file
4. the per-user data directory (e.g. ``~/.local/share/redlens/`` on
   Linux, ``~/Library/Application Support/redlens/`` on macOS)

The config file is ``config.toml`` in the per-user config directory
(override the file location with ``REDLENS_CONFIG``). It is optional —
everything works with no config at all. Recognized so far:

    [storage]
    db = "/path/to/redlens.db"

    [llm]                     # optional: AI profile summaries
    api_key = "..."
    model = "..."             # default: gpt-4o-mini
    base_url = "..."          # default: OpenAI; any OpenAI-compatible endpoint

The LLM key can also come from the environment, which always wins over the
file: ``REDLENS_LLM_API_KEY`` (falling back to ``OPENAI_API_KEY``).

``--project NAME`` (or ``REDLENS_PROJECT``) moves the db, config, and reports
into a self-contained ``projects/<NAME>/`` dir, isolating one client's data. No
project = the defaults above, unchanged. The explicit overrides still win. The
env LLM key is environment-wide so it serves every project; a key saved in a
config file is scoped to that project's config.
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from platformdirs import user_config_dir, user_data_dir

from redlens.errors import MissingKey, RedlensError

APP_NAME = "redlens"


def _validate_project_name(name: str) -> str:
    """A project name becomes a directory under ``projects/``, so it must be a
    single safe path segment. Reject separators, parent refs, and dotfiles."""
    cleaned = name.strip()
    if (not cleaned or cleaned.startswith(".") or "/" in cleaned
            or "\\" in cleaned or os.sep in cleaned
            or (os.altsep and os.altsep in cleaned)):
        raise RedlensError(
            f"invalid project name {name!r}: use a single path segment "
            "(no slashes, no '..', no leading dot)")
    return cleaned


def active_project() -> str | None:
    """The selected project (from ``--project``, surfaced as ``REDLENS_PROJECT``),
    or ``None`` for the default top-level location. Validated — it names a dir."""
    raw = os.environ.get("REDLENS_PROJECT")
    if raw is None or not raw.strip():
        return None
    return _validate_project_name(raw)


def project_dir(name: str) -> Path:
    """The self-contained directory for a project — its own ``config.toml``,
    ``redlens.db``, and ``reports/`` all live here."""
    return Path(user_data_dir(APP_NAME)) / "projects" / _validate_project_name(name)


def config_path() -> Path:
    """The active config file. An explicit ``REDLENS_CONFIG`` always wins; a
    selected project otherwise repoints this into its own directory."""
    env = os.environ.get("REDLENS_CONFIG")
    if env:
        return Path(env).expanduser()
    project = active_project()
    if project:
        return project_dir(project) / "config.toml"
    return Path(user_config_dir(APP_NAME)) / "config.toml"


def default_db_path() -> Path:
    project = active_project()
    if project:
        return project_dir(project) / "redlens.db"
    return Path(user_data_dir(APP_NAME)) / "redlens.db"


def default_report_dir() -> Path:
    """Where ``page --all`` writes the index plus per-topic pages by default —
    a ``reports`` folder under the per-user data dir (or the project's dir),
    kept apart from the DB."""
    project = active_project()
    base = project_dir(project) if project else Path(user_data_dir(APP_NAME))
    return base / "reports"


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise RedlensError(f"bad config {path}: {exc}") from exc


def load_config() -> dict[str, Any]:
    """The parsed active config file (the project's, if one is selected), or {}."""
    return _load_toml(config_path())


def resolve_db_source(flag: str | None = None) -> tuple[Path, str]:
    """Resolve the DB path and report which source in the precedence chain won.

    The label is for humans (``redlens doctor`` shows which knob is active);
    :func:`resolve_db` is the thin wrapper most callers want.
    """
    if flag:
        return Path(flag).expanduser(), "--db flag"
    env = os.environ.get("REDLENS_DB")
    if env:
        return Path(env).expanduser(), "REDLENS_DB env"
    configured = load_config().get("storage", {}).get("db")
    if configured:
        return Path(str(configured)).expanduser(), "config.toml [storage].db"
    return default_db_path(), "default data dir"


def resolve_db(flag: str | None = None) -> Path:
    return resolve_db_source(flag)[0]


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
    path.write_text(_toml_dump(merged), encoding="utf-8")
    path.chmod(0o600)  # also tighten files that predate us
    return path


def llm_api_key() -> str | None:
    """API key for AI summaries, from env or the active config file. The env key
    (``REDLENS_LLM_API_KEY`` / ``OPENAI_API_KEY``) is environment-wide, so it
    serves every project; a key saved in a config file is scoped to that config."""
    for var in ("REDLENS_LLM_API_KEY", "OPENAI_API_KEY"):
        if os.environ.get(var):
            return os.environ[var]
    key = load_config().get("llm", {}).get("api_key")
    return str(key) if key else None


def require_llm_key() -> str:
    """The configured LLM key, or raise :class:`MissingKey` with setup guidance.

    For features that *need* a key; use :func:`llm_api_key` where absence is OK.
    """
    key = llm_api_key()
    if not key:
        raise MissingKey(
            "no LLM API key — run `redlens setup` or set "
            "OPENAI_API_KEY / REDLENS_LLM_API_KEY"
        )
    return key

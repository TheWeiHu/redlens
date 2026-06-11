from pathlib import Path

import pytest

from redlens import config
from redlens.errors import RedlensError


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch, tmp_path):
    """Keep tests away from the developer's real env and config file."""
    monkeypatch.delenv("REDLENS_DB", raising=False)
    monkeypatch.setenv("REDLENS_CONFIG", str(tmp_path / "absent.toml"))


def test_flag_wins_over_everything(monkeypatch, tmp_path):
    monkeypatch.setenv("REDLENS_DB", str(tmp_path / "env.db"))
    assert config.resolve_db("flag.db") == Path("flag.db")


def test_env_beats_config_file(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    # as_posix: backslashes in TOML basic strings are escapes, so raw
    # Windows paths would be invalid TOML
    cfg.write_text(f'[storage]\ndb = "{(tmp_path / "cfg.db").as_posix()}"\n')
    monkeypatch.setenv("REDLENS_CONFIG", str(cfg))
    monkeypatch.setenv("REDLENS_DB", str(tmp_path / "env.db"))
    assert config.resolve_db() == tmp_path / "env.db"


def test_config_file_beats_default(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(f'[storage]\ndb = "{(tmp_path / "cfg.db").as_posix()}"\n')
    monkeypatch.setenv("REDLENS_CONFIG", str(cfg))
    assert config.resolve_db() == tmp_path / "cfg.db"


def test_default_is_per_user_data_dir():
    resolved = config.resolve_db()
    assert resolved == config.default_db_path()
    assert resolved.name == "redlens.db"


def test_malformed_config_is_a_clear_error(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[storage\n")
    monkeypatch.setenv("REDLENS_CONFIG", str(cfg))
    with pytest.raises(RedlensError, match="bad config"):
        config.resolve_db()

from pathlib import Path

import pytest

from redlens import config
from redlens.errors import RedlensError


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch, tmp_path):
    """Keep tests away from the developer's real env and config file."""
    monkeypatch.delenv("REDLENS_DB", raising=False)
    monkeypatch.delenv("REDLENS_PROJECT", raising=False)
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


def test_project_repoints_db_config_and_reports(monkeypatch):
    # An explicit REDLENS_CONFIG would override the project's config location, so
    # clear the one the autouse fixture set to exercise the project repoint.
    monkeypatch.delenv("REDLENS_CONFIG", raising=False)
    monkeypatch.setenv("REDLENS_PROJECT", "acme")
    pdir = config.project_dir("acme")
    assert config.default_db_path() == pdir / "redlens.db"
    assert config.config_path() == pdir / "config.toml"
    assert config.default_report_dir() == pdir / "reports"
    assert config.resolve_db() == pdir / "redlens.db"


def test_no_project_keeps_default_paths(monkeypatch):
    # Back-compat: with no project the paths are exactly the top-level defaults.
    monkeypatch.delenv("REDLENS_CONFIG", raising=False)
    assert config.active_project() is None
    assert config.default_db_path().name == "redlens.db"
    assert "projects" not in config.default_db_path().parts


def test_explicit_db_overrides_project_default(monkeypatch):
    monkeypatch.setenv("REDLENS_PROJECT", "acme")
    assert config.resolve_db("flag.db") == Path("flag.db")
    monkeypatch.setenv("REDLENS_DB", str(Path("/tmp/env.db")))
    assert config.resolve_db() == Path("/tmp/env.db")


def test_blank_project_means_default(monkeypatch):
    monkeypatch.setenv("REDLENS_PROJECT", "   ")
    assert config.active_project() is None


@pytest.mark.parametrize("bad", ["../evil", "a/b", ".", "..", "x\\y"])
def test_invalid_project_names_rejected(monkeypatch, bad):
    monkeypatch.setenv("REDLENS_PROJECT", bad)
    with pytest.raises(RedlensError, match="invalid project name"):
        config.active_project()

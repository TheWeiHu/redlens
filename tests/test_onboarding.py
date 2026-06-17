import os
import tomllib

import pytest

from redlens import config, onboarding


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch, tmp_path):
    monkeypatch.delenv("REDLENS_DB", raising=False)
    for var in ("REDLENS_LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REDLENS_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setattr(onboarding, "ENABLED", True)


def test_first_run_silent_while_gated(monkeypatch):
    monkeypatch.setattr(onboarding, "ENABLED", False)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr(
        "builtins.input",
        lambda _: pytest.fail("must not prompt while the wizard is gated"),
    )
    onboarding.offer_setup_on_first_run()
    assert not config.config_path().exists()


def test_save_config_merges_and_restricts_permissions(tmp_path):
    config.save_config({"storage": {"db": "/tmp/a.db"}})
    path = config.save_config({"llm": {"api_key": 'k"ey\\x'}})
    if os.name == "posix":  # Windows has no POSIX modes; chmod is a no-op there
        assert path.stat().st_mode & 0o777 == 0o600
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["storage"]["db"] == "/tmp/a.db"     # earlier write survived
    assert parsed["llm"]["api_key"] == 'k"ey\\x'      # quoting round-trips


def test_llm_key_prefers_env(monkeypatch):
    config.save_config({"llm": {"api_key": "file-key"}})
    assert config.llm_api_key() == "file-key"

    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    assert config.llm_api_key() == "env-key"


def test_llm_key_none_when_unset():
    assert config.llm_api_key() is None


def test_wizard_saves_llm_key(monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda _: "sk-test-xyz")
    assert onboarding.run_wizard() == 0
    assert config.llm_api_key() == "sk-test-xyz"


def test_wizard_skipped_still_writes_config(monkeypatch):
    monkeypatch.setattr("getpass.getpass", lambda _: "")
    assert onboarding.run_wizard() == 0
    assert config.config_path().exists()
    assert config.llm_api_key() is None


def test_first_run_not_prompted_when_not_a_tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr(
        "builtins.input",
        lambda _: pytest.fail("must not prompt without a TTY"),
    )
    onboarding.offer_setup_on_first_run()
    assert not config.config_path().exists()  # still eligible next time


def test_first_run_decline_writes_marker_and_never_asks_again(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    onboarding.offer_setup_on_first_run()
    assert config.config_path().exists()
    assert config.load_config() == {}  # comment-only marker

    monkeypatch.setattr(
        "builtins.input",
        lambda _: pytest.fail("must not ask twice"),
    )
    onboarding.offer_setup_on_first_run()


def test_first_run_accept_runs_wizard(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    monkeypatch.setattr("getpass.getpass", lambda _: "sk-wizard-key")
    onboarding.offer_setup_on_first_run()
    assert config.llm_api_key() == "sk-wizard-key"

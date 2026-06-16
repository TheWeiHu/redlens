import os
import tomllib

import pytest

from redlens import config, onboarding


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch, tmp_path):
    monkeypatch.delenv("REDLENS_DB", raising=False)
    for var in ("REDLENS_REDDIT_CLIENT_ID", "REDLENS_REDDIT_CLIENT_SECRET",
                "REDLENS_LLM_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REDLENS_CONFIG", str(tmp_path / "config.toml"))
    # Most tests exercise the wizard as it will behave once keys are wired up.
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


def test_key_getters_prefer_env(monkeypatch):
    config.save_config({
        "reddit": {"client_id": "file-id", "client_secret": "file-secret"},
        "llm": {"api_key": "file-key"},
    })
    assert config.reddit_credentials() == ("file-id", "file-secret")
    assert config.llm_api_key() == "file-key"

    monkeypatch.setenv("REDLENS_REDDIT_CLIENT_ID", "env-id")
    monkeypatch.setenv("REDLENS_REDDIT_CLIENT_SECRET", "env-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    assert config.reddit_credentials() == ("env-id", "env-secret")
    assert config.llm_api_key() == "env-key"


def test_key_getters_none_when_unset():
    assert config.reddit_credentials() is None
    assert config.llm_api_key() is None


def test_wizard_saves_both_keys(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "my-client-id")
    secrets = iter(["my-secret", "sk-ant-xyz"])
    monkeypatch.setattr("getpass.getpass", lambda _: next(secrets))
    assert onboarding.run_wizard() == 0
    assert config.reddit_credentials() == ("my-client-id", "my-secret")
    assert config.llm_api_key() == "sk-ant-xyz"


def test_wizard_all_skipped_still_writes_config(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "")
    monkeypatch.setattr("getpass.getpass", lambda _: "")
    assert onboarding.run_wizard() == 0
    assert config.config_path().exists()
    assert config.reddit_credentials() is None
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
    answers = iter(["y", "wizard-id"])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    secrets = iter(["wizard-secret", ""])
    monkeypatch.setattr("getpass.getpass", lambda _: next(secrets))
    onboarding.offer_setup_on_first_run()
    assert config.reddit_credentials() == ("wizard-id", "wizard-secret")

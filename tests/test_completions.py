"""Shell completion generation — guards against verb/flag drift."""

from __future__ import annotations

import argparse

import pytest

from redlens import completions
from redlens.cli import _resolve_sources, build_parser, main


def test_init_is_listed_in_help() -> None:
    """`init` must be discoverable in --help — doctor and schema errors tell
    users to run it, so it can't be a hidden verb."""
    help_text = build_parser().format_help()
    assert "init" in help_text
    assert "create or migrate the database" in help_text


def test_sources_none_runs_no_discovery() -> None:
    """`--sources none`/`skip` is the non-interactive 'only --subreddits' path:
    it resolves to an empty discovery list."""
    assert _resolve_sources("none", assume_yes=True) == []
    assert _resolve_sources("skip", assume_yes=True) == []
    assert _resolve_sources("name", assume_yes=True) == ["name"]


def _subcommands() -> list[str]:
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            # Internal helper verbs (``__``-prefixed, e.g. __complete) are kept
            # out of the generated scripts on purpose, so don't require them.
            return [n for n in action.choices if not n.startswith("__")]
    raise AssertionError("parser has no subcommands")


@pytest.mark.parametrize("shell", completions.SHELLS)
def test_script_mentions_every_subcommand(shell: str) -> None:
    script = completions.generate(shell, build_parser())
    for verb in _subcommands():
        assert verb in script, f"{shell} completion is missing subcommand {verb!r}"


@pytest.mark.parametrize("shell", completions.SHELLS)
def test_script_mentions_a_known_flag(shell: str) -> None:
    # `--full` is registered on `sync`; if flag introspection breaks this trips.
    # fish renders long options as `-l full`, so check the bare option name.
    script = completions.generate(shell, build_parser())
    expected = "-l full" if shell == "fish" else "--full"
    assert expected in script


def test_unknown_shell_rejected() -> None:
    from redlens.errors import RedlensError

    with pytest.raises(RedlensError):
        completions.generate("powershell", build_parser())


@pytest.mark.parametrize("shell", completions.SHELLS)
def test_cli_completions_verb_prints_script(shell: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["completions", shell])
    assert rc == 0
    out = capsys.readouterr().out
    assert "redlens" in out
    assert "completions" in out  # the verb completes itself


@pytest.mark.parametrize("shell", completions.SHELLS)
def test_helper_verb_stays_out_of_scripts(shell: str) -> None:
    # The internal helper is invoked *by* the scripts but must never be offered
    # to the user as a completion candidate.
    script = completions.generate(shell, build_parser())
    assert completions.HELPER_VERB in script  # the scripts call it...
    assert f"-a {completions.HELPER_VERB}" not in script  # ...but never list it (fish)
    assert f" {completions.HELPER_VERB})" not in script  # ...nor as a case label (bash/zsh)


@pytest.mark.parametrize("shell", completions.SHELLS)
def test_scripts_wire_db_value_completion(shell: str) -> None:
    script = completions.generate(shell, build_parser())
    # positional username completion (show/export/summarize) and topic
    # completion (page positional + --topic value) both shell out to the helper.
    assert completions.HELPER_VERB in script
    # bash routes each kind through the literal reader __redlens_values <kind>;
    # zsh/fish call `__complete <kind>` directly.
    token = "__redlens_values " if shell == "bash" else f"{completions.HELPER_VERB} "
    assert f"{token}users" in script
    assert f"{token}topics" in script


def test_untrack_completes_topic_names() -> None:
    # 0021 follow-up: `untrack <topic>` must complete topic names, like
    # `page` and `--topic` already do.
    assert completions.POSITIONAL_KIND.get("untrack") == "topics"
    bash = completions.generate("bash", build_parser())
    assert 'untrack) __redlens_values topics "$cur"; return ;;' in bash
    fish = completions.generate("fish", build_parser())
    assert "__fish_seen_subcommand_from page untrack" in fish  # both topic verbs


def test_bash_value_completion_is_injection_safe_and_newline_safe() -> None:
    # DB values (topic/user names) must NEVER reach `compgen -W "$(...)"`, which
    # expands each word and would execute a name like "$(rm -rf ~)" at TAB time.
    # They flow through __redlens_values, which reads them literally (IFS= read
    # -r), so a multi-word name like "dua lipa" also stays one candidate.
    script = completions.generate("bash", build_parser())
    assert "__redlens_values()" in script
    assert "while IFS= read -r line" in script
    # no DB value is ever piped into compgen -W
    assert f'compgen -W "$({completions._HELPER}' not in script
    # the value cases delegate to the safe literal reader
    assert '--topic) __redlens_values topics "$cur"; return ;;' in script


def test_help_hides_internal_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    # The `analytics` deprecation alias and the `__complete` helper are
    # registered without help=, so a `<command>` metavar keeps them out of the
    # usage line's choices brace too — they must not surface in `--help`.
    with pytest.raises(SystemExit):
        main(["--help"])
    out = capsys.readouterr().out
    assert "analytics" not in out
    assert "__complete" not in out
    assert "<command>" in out          # public verbs grouped under the metavar
    assert "doctor" in out             # ...real verbs still documented


def test_complete_lists_usernames_and_topics(tmp_path) -> None:
    from redlens.db import connect, init_schema, session
    from redlens.models import Topic, User

    db = tmp_path / "redlens.db"
    engine = connect(db)
    init_schema(engine)
    with session(engine) as s:
        s.add(User(username="alice"))
        s.add(User(username="bob"))
        s.add(Topic(name="ubi"))
        s.commit()

    assert completions.complete("users", db) == ["alice", "bob"]
    assert completions.complete("topics", db) == ["ubi"]


def test_complete_returns_shell_metachar_names_verbatim(tmp_path) -> None:
    # A topic name with shell metacharacters comes back unmodified; the bash
    # script (see the injection test) reads it literally so it is never executed.
    from redlens.db import connect, init_schema, session
    from redlens.models import Topic

    db = tmp_path / "redlens.db"
    engine = connect(db)
    init_schema(engine)
    with session(engine) as s:
        s.add(Topic(name="$(touch pwned)"))
        s.commit()
    assert completions.complete("topics", db) == ["$(touch pwned)"]


def test_complete_silent_when_db_missing(tmp_path) -> None:
    missing = tmp_path / "nope.db"
    assert completions.complete("users", missing) == []
    assert completions.complete("topics", missing) == []
    assert not missing.exists()  # completion must not create a DB


def test_cli_complete_verb_prints_names(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    from redlens.db import connect, init_schema, session
    from redlens.models import User

    db = tmp_path / "redlens.db"
    engine = connect(db)
    init_schema(engine)
    with session(engine) as s:
        s.add(User(username="carol"))
        s.commit()

    rc = main(["--db", str(db), completions.HELPER_VERB, "users"])
    assert rc == 0
    assert capsys.readouterr().out.splitlines() == ["carol"]

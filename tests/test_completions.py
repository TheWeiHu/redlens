"""Shell completion generation — guards against verb/flag drift."""

from __future__ import annotations

import argparse

import pytest

from redlens import completions
from redlens.cli import build_parser, main


def _subcommands() -> list[str]:
    parser = build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return list(action.choices)
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

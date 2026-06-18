"""Generate shell completion scripts straight from the argparse parser.

Stdlib only: we walk the built parser (subcommands + their option strings)
so the completions never drift from the real CLI — adding a verb or flag in
``cli.build_parser`` is automatically reflected here, and a test asserts every
registered subcommand shows up in each generated script.
"""

from __future__ import annotations

import argparse

from redlens.errors import RedlensError

SHELLS = ("bash", "zsh", "fish")


def _walk(parser: argparse.ArgumentParser) -> tuple[list[str], dict[str, list[str]]]:
    """Return (global option strings, {subcommand: its option strings}).

    Subcommands keep their registration order; ``--help``/``-h`` is included
    because argparse adds it to every (sub)parser.
    """
    global_flags: list[str] = []
    subcommands: dict[str, list[str]] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, subparser in action.choices.items():
                flags: list[str] = []
                for sub_action in subparser._actions:
                    flags.extend(sub_action.option_strings)
                subcommands[name] = flags
        else:
            global_flags.extend(action.option_strings)
    return global_flags, subcommands


def _bash(global_flags: list[str], subcommands: dict[str, list[str]]) -> str:
    verbs = " ".join(subcommands)
    globals_ = " ".join(global_flags)
    cases = "\n".join(
        f"        {name}) opts=\"{' '.join(flags)}\" ;;" for name, flags in subcommands.items()
    )
    return f"""\
# redlens bash completion — source this file or drop it in a bash-completion.d dir.
_redlens_completions() {{
    local cur cmd i
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    cmd=""
    for ((i = 1; i < COMP_CWORD; i++)); do
        case "${{COMP_WORDS[i]}}" in
            {verbs.replace(' ', '|')}) cmd="${{COMP_WORDS[i]}}"; break ;;
        esac
    done
    if [ -z "$cmd" ]; then
        COMPREPLY=( $(compgen -W "{verbs} {globals_}" -- "$cur") )
        return
    fi
    local opts=""
    case "$cmd" in
{cases}
    esac
    COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
}}
complete -F _redlens_completions redlens
"""


def _zsh(global_flags: list[str], subcommands: dict[str, list[str]]) -> str:
    verbs = " ".join(subcommands)
    globals_ = " ".join(global_flags)
    cases = "\n".join(
        f"        {name}) compadd -- {' '.join(flags)} ;;" for name, flags in subcommands.items()
    )
    return f"""\
#compdef redlens
# redlens zsh completion — source this file or place it on your $fpath.
_redlens() {{
    if (( CURRENT == 2 )); then
        compadd -- {verbs} {globals_}
        return
    fi
    case "${{words[2]}}" in
{cases}
    esac
}}
compdef _redlens redlens
"""


def _fish(global_flags: list[str], subcommands: dict[str, list[str]]) -> str:
    lines = ["# redlens fish completion — source this file or put it in ~/.config/fish/completions/redlens.fish"]
    for name in subcommands:
        lines.append(
            f"complete -c redlens -n __fish_use_subcommand -f -a {name}"
        )
    for name, flags in subcommands.items():
        for flag in flags:
            opt = ("-l " + flag[2:]) if flag.startswith("--") else ("-s " + flag.lstrip("-"))
            lines.append(
                f'complete -c redlens -n "__fish_seen_subcommand_from {name}" {opt}'
            )
    for flag in global_flags:
        opt = ("-l " + flag[2:]) if flag.startswith("--") else ("-s " + flag.lstrip("-"))
        lines.append(f"complete -c redlens {opt}")
    return "\n".join(lines) + "\n"


def generate(shell: str, parser: argparse.ArgumentParser) -> str:
    """Render a completion script for ``shell`` ('bash' | 'zsh' | 'fish')."""
    if shell not in SHELLS:
        raise RedlensError(f"unknown shell {shell!r} (choose from {', '.join(SHELLS)})")
    global_flags, subcommands = _walk(parser)
    return {"bash": _bash, "zsh": _zsh, "fish": _fish}[shell](global_flags, subcommands)

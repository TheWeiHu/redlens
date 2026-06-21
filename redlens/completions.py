"""Generate shell completion scripts straight from the argparse parser.

Stdlib only for *script generation*: we walk the built parser (subcommands +
their option strings) so the completions never drift from the real CLI —
adding a verb or flag in ``cli.build_parser`` is automatically reflected here,
and a test asserts every registered subcommand shows up in each generated
script.

Argparse can't know that a positional names a *username* or a *topic*, so the
generated scripts call a hidden helper verb — ``redlens __complete users`` /
``redlens __complete topics`` — to complete those values from the local DB.
:func:`complete` is the read-only, best-effort backend for that helper.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from redlens.errors import RedlensError

SHELLS = ("bash", "zsh", "fish")

# Hidden verb the scripts shell out to for DB-backed value completion. The
# ``__`` prefix marks it internal: it is registered on the parser but kept out
# of the generated completion scripts (see ``_walk``).
HELPER_VERB = "__complete"
_HELPER = f"redlens {HELPER_VERB}"

# Positional arguments that name a DB entity, by subcommand → entity kind.
# argparse can't infer this, so this small map drives positional value
# completion. Keep it in sync with ``cli.build_parser``.
POSITIONAL_KIND: dict[str, str] = {
    "show": "users",
    "export": "users",
    "summarize": "users",
    "page": "topics",
    "untrack": "topics",
}
# Flags whose VALUE names a DB entity (the flag name itself still completes via
# the normal flag list); flag → entity kind.
FLAG_VALUE_KIND: dict[str, str] = {
    "--topic": "topics",
}


def _walk(parser: argparse.ArgumentParser) -> tuple[list[str], dict[str, list[str]]]:
    """Return (global option strings, {subcommand: its option strings}).

    Subcommands keep their registration order; ``--help``/``-h`` is included
    because argparse adds it to every (sub)parser. Internal helper verbs (those
    whose name starts with ``__``, e.g. ``__complete``) are skipped so they
    never surface as user-facing completions.
    """
    global_flags: list[str] = []
    subcommands: dict[str, list[str]] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, subparser in action.choices.items():
                if name.startswith("__"):
                    continue
                flags: list[str] = []
                for sub_action in subparser._actions:
                    flags.extend(sub_action.option_strings)
                subcommands[name] = flags
        else:
            global_flags.extend(action.option_strings)
    return global_flags, subcommands


def _call(kind: str) -> str:
    """The shell snippet that lists DB values, silenced if the helper errors."""
    return f"{_HELPER} {kind} 2>/dev/null"


def _bash(global_flags: list[str], subcommands: dict[str, list[str]]) -> str:
    verbs = " ".join(subcommands)
    globals_ = " ".join(global_flags)
    flag_cases = "\n".join(
        f"        {name}) opts=\"{' '.join(flags)}\" ;;" for name, flags in subcommands.items()
    )
    # Value completion goes through __redlens_values (defined in the script),
    # which reads DB values LITERALLY — never via `compgen -W "$(...)"`, which
    # expands each word and would execute a topic named e.g. "$(rm -rf ~)".
    flag_value_cases = "\n".join(
        f'        {flag}) __redlens_values {kind} "$cur"; return ;;'
        for flag, kind in FLAG_VALUE_KIND.items()
    )
    pos_cases = "\n".join(
        f'            {verb}) __redlens_values {kind} "$cur"; return ;;'
        for verb, kind in POSITIONAL_KIND.items()
        if verb in subcommands
    )
    return f"""\
# redlens bash completion — source this file or drop it in a bash-completion.d dir.
# DB values are read line-by-line and matched literally; they are NEVER passed
# through `compgen -W`, which expands each word and would execute a value like
# "$(...)" at completion time.
__redlens_values() {{  # $1 = users|topics, $2 = current word
    local line
    COMPREPLY=()
    while IFS= read -r line; do
        [[ -n $line && $line == "$2"* ]] && COMPREPLY+=("$line")
    done < <({_HELPER} "$1" 2>/dev/null)
}}
_redlens_completions() {{
    local cur prev cmd i
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"
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
    # complete the VALUE after a flag that names a DB entity (e.g. --topic)
    case "$prev" in
{flag_value_cases}
    esac
    # complete a positional that names a DB entity (skip while typing a flag)
    if [[ "$cur" != -* ]]; then
        case "$cmd" in
{pos_cases}
        esac
    fi
    local opts=""
    case "$cmd" in
{flag_cases}
    esac
    COMPREPLY=( $(compgen -W "$opts" -- "$cur") )
}}
complete -F _redlens_completions redlens
"""


def _zsh(global_flags: list[str], subcommands: dict[str, list[str]]) -> str:
    verbs = " ".join(subcommands)
    globals_ = " ".join(global_flags)
    flag_cases = "\n".join(
        f"        {name}) compadd -- {' '.join(flags)} ;;" for name, flags in subcommands.items()
    )
    flag_value_cases = "\n".join(
        f'        {flag}) compadd -- ${{(f)"$({_call(kind)})"}}; return ;;'
        for flag, kind in FLAG_VALUE_KIND.items()
    )
    pos_cases = "\n".join(
        f'            {verb}) compadd -- ${{(f)"$({_call(kind)})"}}; return ;;'
        for verb, kind in POSITIONAL_KIND.items()
        if verb in subcommands
    )
    return f"""\
#compdef redlens
# redlens zsh completion — source this file or place it on your $fpath.
_redlens() {{
    if (( CURRENT == 2 )); then
        compadd -- {verbs} {globals_}
        return
    fi
    # complete the VALUE after a flag that names a DB entity (e.g. --topic)
    case "${{words[CURRENT-1]}}" in
{flag_value_cases}
    esac
    # complete a positional that names a DB entity (skip while typing a flag)
    if [[ "${{words[CURRENT]}}" != -* ]]; then
        case "${{words[2]}}" in
{pos_cases}
        esac
    fi
    case "${{words[2]}}" in
{flag_cases}
    esac
}}
compdef _redlens redlens
"""


def _fish_opt(flag: str) -> str:
    return ("-l " + flag[2:]) if flag.startswith("--") else ("-s " + flag.lstrip("-"))


def _fish(global_flags: list[str], subcommands: dict[str, list[str]]) -> str:
    lines = ["# redlens fish completion — source this file or put it in ~/.config/fish/completions/redlens.fish"]
    for name in subcommands:
        lines.append(
            f"complete -c redlens -n __fish_use_subcommand -f -a {name}"
        )
    for name, flags in subcommands.items():
        for flag in flags:
            lines.append(
                f'complete -c redlens -n "__fish_seen_subcommand_from {name}" {_fish_opt(flag)}'
            )
    for flag in global_flags:
        lines.append(f"complete -c redlens {_fish_opt(flag)}")
    # DB-backed value completion: a flag whose value names a topic, e.g. --topic
    for flag, kind in FLAG_VALUE_KIND.items():
        verbs_with = [v for v, flags in subcommands.items() if flag in flags]
        if verbs_with:
            cond = "__fish_seen_subcommand_from " + " ".join(verbs_with)
            lines.append(
                f'complete -c redlens -n "{cond}" {_fish_opt(flag)} -r -f -a "({_call(kind)})"'
            )
    # DB-backed value completion: positionals that name a username/topic.
    by_kind: dict[str, list[str]] = {}
    for verb, kind in POSITIONAL_KIND.items():
        if verb in subcommands:
            by_kind.setdefault(kind, []).append(verb)
    for kind, verbs_list in by_kind.items():
        cond = "__fish_seen_subcommand_from " + " ".join(verbs_list)
        lines.append(f'complete -c redlens -n "{cond}" -f -a "({_call(kind)})"')
    return "\n".join(lines) + "\n"


def generate(shell: str, parser: argparse.ArgumentParser) -> str:
    """Render a completion script for ``shell`` ('bash' | 'zsh' | 'fish')."""
    if shell not in SHELLS:
        raise RedlensError(f"unknown shell {shell!r} (choose from {', '.join(SHELLS)})")
    global_flags, subcommands = _walk(parser)
    return {"bash": _bash, "zsh": _zsh, "fish": _fish}[shell](global_flags, subcommands)


def complete(kind: str, db_path: str | Path) -> list[str]:
    """List DB values for shell value-completion: archived usernames or tracked
    topic names, sorted.

    Read-only and best-effort: returns ``[]`` (so completion stays silent) when
    the DB is missing or unreadable, and never creates or migrates a database —
    completion must not have side effects.
    """
    path = Path(db_path).expanduser()
    if kind not in ("users", "topics") or not path.exists():
        return []
    try:
        from sqlmodel import Session, create_engine, select

        from redlens.models import Topic, User

        engine = create_engine(f"sqlite:///{path}")
        with Session(engine) as s:
            if kind == "users":
                rows = s.exec(select(User.username).order_by(User.username)).all()
            else:
                rows = s.exec(select(Topic.name).order_by(Topic.name)).all()
        return [str(r) for r in rows]
    except Exception:
        return []

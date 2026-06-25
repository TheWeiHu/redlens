<div align="center">

# redlens

**A local lens on public Reddit history — archive users and topics, analyze them, own the data.**

[![CI](https://github.com/TheWeiHu/redlens/actions/workflows/ci.yml/badge.svg)](https://github.com/TheWeiHu/redlens/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](pyproject.toml)

[How it works](#how-it-works) · [Install](#install) · [Commands](#commands) · [Design](DESIGN.md)

</div>

## How it works

redlens pulls public Reddit history from [arctic-shift](https://arctic-shift.photon-reddit.com),
a free, keyless mirror, and stores it in **one SQLite file you own**. Archive a **user's** full
history, or **track a topic** — redlens builds a subreddit net (arctic has no global text search),
archives every match, and renders it as a standalone HTML report.

No API keys, no setup, no Reddit account. The schema is created and migrated automatically on first
use. Architecture and configuration live in [DESIGN.md](DESIGN.md).

## Install

```bash
pip install -e ".[dev]"
```

Two runtime dependencies (`platformdirs`, `sqlmodel`), all permissively licensed (MIT/BSD/PSF).

## Commands

| Command | What it does |
| --- | --- |
| `redlens sync <user>` | Archive a user's public post + comment history |
| `redlens show <user>` | Print a rollup (`--json` for machine output) |
| `redlens list` | Every archived user — counts, last activity |
| `redlens export <user>` | Dump posts + comments (`--format json\|csv\|jsonl`, `-o PATH`) |
| `redlens track <topic>` | Build a subreddit net and archive every matching post |
| `redlens topics` | Every tracked topic — keywords, net size, match count |
| `redlens page <topic>` | Render a standalone HTML report (`--all` for every topic + index) |
| `redlens explore` | Browse the DB in your browser (read-only, with a SQL console) |
| `redlens summarize <user>` | LLM-powered summary (needs an API key — see below) |
| `redlens setup` | Configure the optional LLM API key |
| `redlens completions <shell>` | Print a `bash\|zsh\|fish` completion script |

Run `redlens <command> --help` for flags. `track` takes `--query`/`--exclude` keyword steering,
`--sources` to pick the discovery net (`name`, `global`, `web`, `popular`, `llm`), `--comments` to
archive threads, and `--discover` to widen the net through matched authors. Re-running `track` is
incremental — the net is remembered and only new posts are pulled.

## Optional LLM key

An optional **OpenAI-compatible API key** powers `redlens summarize`, LLM-scored sentiment in
`page --summary`, and the `llm` discovery source. The first interactive run offers to collect it, or
run `redlens setup` anytime. It's stored mode-600 in your per-user config dir — details in
[DESIGN.md](DESIGN.md).

> **No Reddit official-API integration.** As of late 2025 Reddit gates its API behind pre-approval
> and no longer hands out keys on request, so redlens relies on the keyless arctic-shift mirror. Hold
> Reddit credentials and want fresh sync? [Open an issue](https://github.com/TheWeiHu/redlens/issues)
> and we'll build the provider around your key.

## More

- [DESIGN.md](DESIGN.md) — architecture, configuration, data layout
- [CONTRIBUTING.md](CONTRIBUTING.md) — dev setup, how releases are cut (tag-driven, PyPI trusted publishing)
- [CHANGELOG.md](CHANGELOG.md) — release history
- [SECURITY.md](SECURITY.md) — reporting vulnerabilities

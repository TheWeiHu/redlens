<h1 align="center">redlens</h1>

<p align="center">
  <strong>A local lens on public Reddit history — archive users and topics, analyze them, own the data.</strong>
</p>

<p align="center">
  <a href="https://github.com/TheWeiHu/redlens/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/TheWeiHu/redlens/ci.yml" alt="CI"></a>
  <a href="https://github.com/TheWeiHu/redlens/blob/main/LICENSE"><img src="https://img.shields.io/github/license/TheWeiHu/redlens" alt="MIT license"></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.11+-blue" alt="Python 3.11+"></a>
  <a href="https://arctic-shift.photon-reddit.com"><img src="https://img.shields.io/badge/data-arctic--shift-orange" alt="Data: arctic-shift"></a>
</p>

<p align="center">
  <a href="#how-it-works">How It Works</a>
  ·
  <a href="#install">Install</a>
  ·
  <a href="#commands">Commands</a>
  ·
  <a href="DESIGN.md">Design</a>
</p>

Archive and analyze public Reddit history, locally. redlens pulls from
[arctic-shift](https://arctic-shift.photon-reddit.com) — a free, keyless mirror — and keeps
everything in **one SQLite file you own**. No API keys, no setup, no Reddit account.

## How It Works

```
Archive    redlens sync <user>     a user's full public post + comment history
           redlens track <topic>   build a subreddit net, archive every match
Analyze    redlens show / topics   rollups, scores, per-subreddit totals
Render     redlens page <topic>    a standalone, dependency-free HTML report
```

arctic has no global text search, so a **topic** isn't a query — it's a *net*. `track` discovers
the subreddits a topic lives in, archives every matching post across them, and remembers the net so
re-runs pull only what's new. The schema is created and migrated automatically on first use. Full
architecture and configuration in [`DESIGN.md`](DESIGN.md).

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
archive threads, and `--discover` to widen the net through matched authors.

## Optional LLM Key

An optional **OpenAI-compatible API key** powers `redlens summarize`, LLM-scored sentiment in
`page --summary`, and the `llm` discovery source. The first interactive run offers to collect it, or
run `redlens setup` anytime. It's stored mode-600 in your per-user config dir — details in
[`DESIGN.md`](DESIGN.md).

> **No Reddit official-API integration.** As of late 2025 Reddit gates its API behind pre-approval
> and no longer hands out keys on request, so redlens relies on the keyless arctic-shift mirror. Hold
> Reddit credentials and want fresh sync? [Open an issue](https://github.com/TheWeiHu/redlens/issues)
> and we'll build the provider around your key.

## More

- [`DESIGN.md`](DESIGN.md) — architecture, configuration, data layout
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, releases (tag-driven, PyPI trusted publishing)
- [`CHANGELOG.md`](CHANGELOG.md) — release history
- [`SECURITY.md`](SECURITY.md) — reporting vulnerabilities

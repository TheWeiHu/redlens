# redlens

Archive and analyze public Reddit history, locally. Built on
[arctic-shift](https://arctic-shift.photon-reddit.com). Three tables
(users, posts, comments), one derived analytic, one fetch function, one
CLI. Your data stays in a local SQLite file you own.

*The name: a lens on public discussion. Today redlens archives and
examines users; tracking topics across public discussion is where it's
headed.*

## Install

```bash
pip install -e ".[dev]"
```

## Use

```bash
redlens sync KimJongFunk              # archive a user's public history
redlens analytics KimJongFunk         # print rollup
redlens analytics KimJongFunk --json  # or as JSON
redlens explore                       # browse the DB in your browser
```

Track a **topic** instead of a user, then render it as a page:

```bash
redlens track "dua lipa"   # discover a subreddit net, archive every match
redlens page  "dua lipa"   # render a standalone HTML report
```

`track` builds a subreddit net (arctic has no global text search) from
several discovery sources you pick from, plus a curating picker. Run
`redlens track --help` for the discovery sources and flags.

No setup needed — the schema is created (and migrated) automatically on
first use. No API keys are needed — arctic-shift is a free, open mirror.

## Optional API keys

Two keys unlock more. The first interactive run offers to collect them once,
or run the wizard anytime with `redlens setup`:

- **LLM API key** (OpenAI or any OpenAI-compatible endpoint) — powers
  `redlens summarize` and the `llm` discovery source in `track`.
- **Reddit API key** — fresh-data top-ups via Reddit's official API
  (collected now; used once the Reddit provider lands).

Keys are stored (mode 600) in your per-user config dir (`redlens setup` writes
it; `REDLENS_CONFIG` to override); environment variables override the file. See
[DESIGN.md](DESIGN.md) for the variable names.

## Data

Everything lands in one SQLite file you own, created automatically on first use
in your per-user data directory. Point elsewhere with the `--db` flag,
`REDLENS_DB`, or `[storage] db` in the config file — see [DESIGN.md](DESIGN.md).

## Explore

Browse the database in your browser — tables and row counts, schema, sortable
and searchable rows, and a read-only SQL console with preset analyses:

```bash
redlens explore                       # opens the default DB, pops a browser
redlens explore --port 9000 --no-browser
```

The DB is opened read-only, so nothing here can mutate it.

---

Architecture, configuration, and development: see [DESIGN.md](DESIGN.md).

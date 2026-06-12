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
redlens track "dua lipa" --days 180   # cast the net, archive every match
redlens track "dua lipa" --discover   # widen the net via who posts about it
redlens page  "dua lipa"              # render a standalone HTML report
```

Arctic has no global text search, so `track` fans the query out across a
subreddit net: guessed home subs, anything you add with
`--subreddits a,b,c`, and (with `--discover`) the other subreddits where
authors of matching posts also discuss the topic. Re-running is
incremental.

No setup needed — the schema is created (and migrated) automatically on
first use.

No API keys are needed — arctic-shift is a free, open mirror. (Optional
keys for fresh-data sync via Reddit's official API and for AI profile
summaries are coming; the `redlens setup` wizard ships disabled until
they do something.)

## Data

Everything lands in one SQLite file, by default in your per-user data
directory (`~/.local/share/redlens/redlens.db` on Linux,
`~/Library/Application Support/redlens/redlens.db` on macOS).

Point elsewhere with (in order of precedence):

```bash
redlens --db /path/to/other.db sync spez      # 1. the --db flag
export REDLENS_DB=/path/to/other.db           # 2. env var
```

or set it once in `~/.config/redlens/config.toml` (3.):

```toml
[storage]
db = "/path/to/other.db"
```

## Explore

Browse the database in your browser — tables and row counts, schema, sortable
and searchable rows, and a read-only SQL console with preset analyses:

```bash
redlens explore                       # opens the default DB, pops a browser
redlens explore --port 9000 --no-browser
```

The DB is opened read-only, so nothing here can mutate it.

## Layout

```
redlens/     models, db, config, arctic client, ingest, analytics,
                 explore (the DB browser), cli
tests/           pytest, in-memory sqlite, no network
```

## Test

```bash
pytest
```

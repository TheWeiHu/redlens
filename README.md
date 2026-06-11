# RedditPages

Reddit profile analytics built on [arctic-shift](https://arctic-shift.photon-reddit.com).
Three tables (users, posts, comments), one derived analytic, one fetch
function, one CLI. Your data stays in a local SQLite file you own.

## Install

```bash
pip install -e ".[dev]"
```

## Use

```bash
redditpages sync KimJongFunk              # pull from arctic
redditpages analytics KimJongFunk         # print rollup
redditpages analytics KimJongFunk --json  # or as JSON
redditpages explore                       # browse the DB in your browser
```

No setup needed — the schema is created (and migrated) automatically on
first use.

## Data

Everything lands in one SQLite file, by default in your per-user data
directory (`~/.local/share/redditpages/redditpages.db` on Linux,
`~/Library/Application Support/redditpages/redditpages.db` on macOS).

Point elsewhere with (in order of precedence):

```bash
redditpages --db /path/to/other.db sync spez      # 1. the --db flag
export REDDITPAGES_DB=/path/to/other.db           # 2. env var
```

or set it once in `~/.config/redditpages/config.toml` (3.):

```toml
[storage]
db = "/path/to/other.db"
```

## Explore

Browse the database in your browser — tables and row counts, schema, sortable
and searchable rows, and a read-only SQL console with preset analyses:

```bash
redditpages explore                       # opens the default DB, pops a browser
redditpages explore --port 9000 --no-browser
```

The DB is opened read-only, so nothing here can mutate it.

## Layout

```
redditpages/     models, db, config, arctic client, ingest, analytics,
                 explore (the DB browser), cli
tests/           pytest, in-memory sqlite, no network
```

## Test

```bash
pytest
```

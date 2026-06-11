# redthread

Archive and analyze public Reddit history, locally. Built on
[arctic-shift](https://arctic-shift.photon-reddit.com). Three tables
(users, posts, comments), one derived analytic, one fetch function, one
CLI. Your data stays in a local SQLite file you own.

*The name: "the red thread" is the common theme running through a story.
Today redthread archives users; tracing topics across public discussion is
where it's headed.*

## Install

```bash
pip install -e ".[dev]"
```

## Use

```bash
redthread sync KimJongFunk              # pull from arctic
redthread analytics KimJongFunk         # print rollup
redthread analytics KimJongFunk --json  # or as JSON
redthread explore                       # browse the DB in your browser
```

No setup needed — the schema is created (and migrated) automatically on
first use.

No API keys are needed — arctic-shift is a free, open mirror. (Optional
keys for fresh-data sync via Reddit's official API and for AI profile
summaries are coming; the `redthread setup` wizard ships disabled until
they do something.)

## Data

Everything lands in one SQLite file, by default in your per-user data
directory (`~/.local/share/redthread/redthread.db` on Linux,
`~/Library/Application Support/redthread/redthread.db` on macOS).

Point elsewhere with (in order of precedence):

```bash
redthread --db /path/to/other.db sync spez      # 1. the --db flag
export REDTHREAD_DB=/path/to/other.db           # 2. env var
```

or set it once in `~/.config/redthread/config.toml` (3.):

```toml
[storage]
db = "/path/to/other.db"
```

## Explore

Browse the database in your browser — tables and row counts, schema, sortable
and searchable rows, and a read-only SQL console with preset analyses:

```bash
redthread explore                       # opens the default DB, pops a browser
redthread explore --port 9000 --no-browser
```

The DB is opened read-only, so nothing here can mutate it.

## Layout

```
redthread/     models, db, config, arctic client, ingest, analytics,
                 explore (the DB browser), cli
tests/           pytest, in-memory sqlite, no network
```

## Test

```bash
pytest
```

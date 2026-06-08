# RedditPages

Reddit profile analytics built on [arctic-shift](https://arctic-shift.photon-reddit.com).
Three tables (users, posts, comments), one derived analytic, one fetch
function, one CLI. No runtime dependencies.

## Install

```bash
pip install -e ".[dev]"
```

## Use

```bash
redditpages init                          # create schema
redditpages sync KimJongFunk              # pull from arctic
redditpages analytics KimJongFunk         # print rollup
redditpages analytics KimJongFunk --json  # or as JSON
```

## Data

The synced SQLite database is a large, network-sourced artifact and lives
outside the checkout, in a sibling `data/` directory:

```
../data/redditpages.db     56 curated users (posts, comments, moderators)
```

`redditpages.db` is the default for the CLI and `scripts/`, so
`redditpages analytics spez` and `python scripts/build_rich_all.py` work with
no flags. Point elsewhere with `--db` or the `REDDITPAGES_DATA` env var:

```bash
export REDDITPAGES_DATA=/path/to/data   # overrides the sibling default
```

## Explore

Browse the database in your browser — tables and row counts, schema, sortable
and searchable rows, and a read-only SQL console with preset analyses:

```bash
python scripts/explore.py                 # opens ../data/redditpages.db, pops a browser
python scripts/explore.py --db other.db --port 9000 --no-browser
```

Pure standard library (no install, no dependencies); the DB is opened
read-only, so nothing here can mutate it.

## Layout

```
redditpages/        models, db, arctic client, ingest, analytics, cli
scripts/         build/sync tooling + explore.py (the DB browser)
tests/           pytest, in-memory sqlite, no network
notes/NOTES.md   schema, design calls, what we drop and why
```

## Test

```bash
pytest
```

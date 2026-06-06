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

## Layout

```
redditpages/        models, db, arctic client, ingest, analytics, cli
tests/           pytest, in-memory sqlite, no network
notes/NOTES.md   schema, design calls, what we drop and why
```

## Test

```bash
pytest
```

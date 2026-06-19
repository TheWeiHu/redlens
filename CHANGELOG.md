# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- **Sentiment over time** on the topic page — a new "Sentiment over time"
  section charts each week's sentiment (−1 to +1) as diverging bars (green
  positive / red negative), bucketed from the archive's post timestamps. With
  `page --summary` (LLM, key required) each week is scored by one LLM call that
  handles sarcasm and negation ("X no longer works" is negative); without a key
  it falls back to an offline, deterministic lexicon scorer
  (`redlens/sentiment.py`, valences from the VADER lexicon) — keyless but rough.
  `summarize.weekly_topic_sentiment()` does the LLM scoring in a single call
  over per-week title samples.
- `redlens page --all` — render every tracked topic plus a small `index.html`
  linking them, into a directory (`-o DIR`, default the per-user reports dir).
  Reuses the existing per-topic renderer; topics with zero matched posts are
  skipped and noted on the index.
- `redlens untrack <topic>` — stop tracking a topic and garbage-collect only
  the rows it alone kept: deletes the topic and its `topicpost` links, then
  drops a matched post (and its comments) only when no other topic still tags
  it and its author isn't a synced user. Confirms before deleting; `-y/--yes`
  skips the prompt (a non-interactive run without `-y` declines, never deletes
  by surprise).
- `redlens show --topic <topic>` — a topic's roll-up stats to the terminal:
  matched-post volume, total score, top subreddits, top authors, and date
  range, computed in SQL (the topic-side mirror of `show <user>`). `--json`
  emits the full ranked lists.
- `redlens export --topic <topic>` — dump a tracked topic's matched posts
  (and any pulled comments) in the existing export formats (json/jsonl/csv),
  the machine-readable counterpart to the HTML `page`. Mirrors
  `export <user>`; `username` and `--topic` are mutually exclusive.
- `redlens track <topic>` — follow a subject across public discussion: a
  full-text query fanned out over a subreddit net, with user-selectable
  discovery sources (name match via arctic, DuckDuckGo web search, a
  maintained top-100 popular list, and optional LLM suggestions — the
  first consumer of the LLM key), one curating picker, `--subreddits`,
  and one `--discover` round via authors of matching posts. Incremental
  on re-runs. (#13, #14)
- `redlens page <topic>` — render a tracked topic as a standalone HTML
  page: volume by subreddit, monthly timeline, top posts. (#13, #14)
- `redlens summarize <username>` — AI profile summaries via a bring-your-own
  LLM key: infers demographics (gender/age/location) and Big Five
  personality from a representative top-voted-and-recent sample of a user's
  posts, with a `--depth` knob and structured JSON output for deterministic
  rendering.
- New `topic` and `topicpost` tables; posts stay in the shared `post`
  table so user archives and topic archives coexist in one DB.
- New `sync_state` table (per-user, per-stream cursors): `redlens sync` is now
  incremental — re-syncing an unchanged user costs one request per kind and
  writes nothing, interrupted backfills resume from where they stopped instead
  of starting over, and `--full` forces a complete re-pull. (#6)

- `redlens list` — every archived user at a glance: post/comment counts,
  last activity, and when each was last synced; `--json` for scripting. (#8)
- `redlens topics` — the topic-surface parallel to `list`: every tracked
  topic with its keywords, subreddit-net size, matched-post count, and
  last-tracked date; `--json` for scripting. (#14)
- `redlens export <username>` — dump a user's posts and comments to stdout (or
  `-o PATH`) as `--format json|csv|jsonl`. (#8)

### Changed
- The AI topic summary (`summarize --topic`, and `page --summary`) is now more
  succinct: the prompt caps the overview at one or two sentences, each theme to
  one short sentence, and sentiment/viewpoints to a single sentence each.
- `redlens doctor`: an unreachable arctic-shift probe is now a "⚠" (exit 0)
  rather than a "✗" (exit 1) — a third party's transient downtime isn't a fault
  in your environment, and the exit code gates only on what you can fix (storage
  and config). New `--no-network` skips the probe entirely (reported as a "–
  skipped") so DB/config/LLM-key diagnosis still runs offline. (#17)
- `redlens analytics` is now `redlens show`. `analytics` is kept as a hidden
  alias for one release (it prints a deprecation note); switch to `show`. (#8)
- The first-run key-onboarding wizard (`redlens setup`) is now enabled. (#19)
- `upsert()` returns the net-new inserted rows. (#17)
- README gains a worked topic-tracking walkthrough (`track` → `topics` →
  `page`) with a real `--query`/`--exclude`/`--sources` example and expected
  output. (#18)

### Fixed
- Incremental `sync`: a top-up cut short by `MAX_ITEMS_PER_STREAM` no longer
  strands the items between the old cursor and the oldest one it fetched — the
  pull is marked an unfinished backfill so the next sync resumes downward and
  closes the gap (re-pulled rows dedup).
- `track`: the per-topic incremental cursor no longer advances when a subreddit
  in the net fails transiently, so that subreddit's older posts are still
  re-fetched on the next track instead of being silently skipped. When the
  failed run had *widened* the topic (new subreddit, changed keywords, longer
  window) the cursor is reset, forcing a full re-pull next time — otherwise the
  already-persisted wider net would mask the widening and skip the failed slice.
- Shell completions (bash): topic names containing spaces (e.g. `dua lipa`) are
  offered as a single completion instead of being split on whitespace.
- `page --all`: topic names that reduce to the same slug (e.g. `C++` and `C#`
  → `c`) no longer overwrite each other's page or share an index link —
  colliding slugs are suffixed (`c`, `c-2`).
- Shell completions now complete topic names for `untrack <topic>`.
- The `analytics` deprecation alias and the internal `__complete` helper no
  longer appear in `redlens --help` (they were leaking into the usage line).

### Removed
- The un-buildable Reddit official-API surface. (#21)

## [0.2.0] - 2026-06-11

First installable release — and a new name: **redlens** (formerly
redditpages). A lens on public discussion; tracking topics across it is
the roadmap.

### Added
- DB path resolution that works anywhere: `--db` flag > `REDLENS_DB`
  env var > `config.toml` > the per-user data directory (via platformdirs).
- Optional `~/.config/redlens/config.toml`.
- Schema versioning via SQLite's `PRAGMA user_version`, with automatic
  migrations on connect.
- `redlens explore` — the read-only browser DB explorer, now a
  first-class subcommand.
- A first-run key-onboarding wizard (`redlens setup`), shipped disabled
  until the keys it collects are consumed.
- MIT license, CI, and PyPI release workflow.

### Changed
- The schema is created and migrated automatically on first use; `init` is
  no longer required.

### Removed
- Personal research tooling (HTML site generation, curated-user scripts,
  moderator-snapshot loading) and the `moderator`/`subreddit` tables.

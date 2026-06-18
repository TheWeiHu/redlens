# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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
- `redlens analytics` is now `redlens show`. `analytics` is kept as a hidden
  alias for one release (it prints a deprecation note); switch to `show`. (#8)
- The first-run key-onboarding wizard (`redlens setup`) is now enabled. (#19)
- `upsert()` returns the net-new inserted rows. (#17)

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

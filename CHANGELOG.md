# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `redlens track <topic>` — follow a subject across public discussion: a
  full-text query fanned out over a subreddit net (guessed home subs,
  `--subreddits`, and one `--discover` round via authors of matching
  posts). Incremental on re-runs. (#13)
- `redlens page <topic>` — render a tracked topic as a standalone HTML
  page: volume by subreddit, monthly timeline, top posts. (#13)
- New `topic` and `topicpost` tables; posts stay in the shared `post`
  table so user archives and topic archives coexist in one DB.

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

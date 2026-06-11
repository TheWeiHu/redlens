# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-06-11

First installable release.

### Added
- DB path resolution that works anywhere: `--db` flag > `REDDITPAGES_DB`
  env var > `config.toml` > the per-user data directory (via platformdirs).
- Optional `~/.config/redditpages/config.toml`.
- Schema versioning via SQLite's `PRAGMA user_version`, with automatic
  migrations on connect.
- `redditpages explore` — the read-only browser DB explorer, now a
  first-class subcommand.
- A first-run key-onboarding wizard (`redditpages setup`), shipped disabled
  until the keys it collects are consumed.
- MIT license, CI, and PyPI release workflow.

### Changed
- The schema is created and migrated automatically on first use; `init` is
  no longer required.

### Removed
- Personal research tooling (HTML site generation, curated-user scripts,
  moderator-snapshot loading) and the `moderator`/`subreddit` tables.

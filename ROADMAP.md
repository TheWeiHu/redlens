# Roadmap

The roadmap lives in [GitHub issues](https://github.com/TheWeiHu/redlens/issues?q=is%3Aissue+is%3Aopen+label%3Aroadmap),
grouped by milestone — this file is just the map.

- **[v0.3 — the keys do something](https://github.com/TheWeiHu/redlens/milestone/1)**:
  fresh data via the Reddit API, AI profile summaries via an LLM key,
  incremental sync, `doctor`, and a rounder CLI (`show` / `list` / `export`).
- **[v1.0 — polish](https://github.com/TheWeiHu/redlens/milestone/2)**:
  SQL-backed analytics, shell completions, a Homebrew tap.
- **Beyond**: topic tracking — follow a subject across public discussion,
  not just a username. The reason this tool is called a lens.

## Picking up a task (humans and agents alike)

Issues labeled [`agent-ready`](https://github.com/TheWeiHu/redlens/issues?q=is%3Aissue+is%3Aopen+label%3Aagent-ready)
are self-contained: goal, agreed design, acceptance criteria, and file
pointers. To claim one:

1. Comment on the issue so work isn't duplicated.
2. Read `CLAUDE.md` for repo conventions (stdlib-first, minimal deps).
3. Open a PR with `Fixes #N` in the description.
4. Done means: acceptance criteria met, `pytest` / `ruff check .` /
   `mypy redlens` all green.

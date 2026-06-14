# Redlens

Reddit profile analytics on arctic-shift data. See `README.md` for usage.

## Working on this repo

- Install: `pip install -e ".[dev]"`
- Test: `pytest` (network-marked tests: `pytest -m integration`)
- Lint/type: `ruff check .` and `mypy redlens`
- Keep dependencies minimal: stdlib + sqlmodel. The arctic client and the
  DB explorer are deliberately stdlib-only.
- Schema lives in `redlens/models.py` (SQLModel, tables: user / post /
  comment / topic / topicpost). Writes go through `db.upsert` so re-syncs
  are idempotent.

## Shipping a PR

- Every PR that changes behavior must include a **terminal log demonstrating
  the feature working** (the real command + output — a `track`/`sync` run, a
  rendered page, a new flag, the fixed bug). Evidence it works, not a claim
  that it should. Docs-only/refactor PRs state "N/A — no behavior change".
- When addressing PR review comments, **reply on GitHub to each comment**
  saying how it was handled (`gh api repos/<owner>/<repo>/pulls/<n>/comments/
  <id>/replies -f body=...`), and **post one summary comment** on the PR
  (`gh pr comment <n>`). Don't just push the fixes silently.

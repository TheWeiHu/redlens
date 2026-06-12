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

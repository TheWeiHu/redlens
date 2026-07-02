# Design

Redlens archives and analyzes **public** Reddit history locally. Built on the
[arctic-shift](https://arctic-shift.photon-reddit.com) mirror, it pulls a
subject's public posts and comments into a SQLite file you own, then derives
analytics and renders standalone reports. See `README.md` for usage; this
document covers how it is built and why.

## Principles

- **Local-first, you own the data.** Everything lands in one SQLite file in
  your per-user data directory. Nothing is sent anywhere except the public
  read-only APIs the fetchers call.
- **Minimal dependencies.** Runtime deps are stdlib + SQLModel. The arctic
  client and the DB explorer are deliberately stdlib-only — no extra HTTP or
  web framework. Optional features (AI summaries) reach an OpenAI-compatible
  endpoint over `urllib`, not a vendor SDK.
- **Idempotent ingest.** Re-running a sync or track never duplicates rows.
  All writes go through `db.upsert`, which returns the rows actually inserted
  so callers can report net-new counts.
- **No global text search.** Arctic has no full-text search, so topic tracking
  builds a *subreddit net* and scans within it (see Discovery).

## Two subjects

- **Users** — `sync` archives a user's public history; `analytics` prints a
  rollup; `summarize` produces an AI profile (optional, BYO LLM key).
- **Topics** — `track` discovers a subreddit net and archives every matching
  post across it; `page` renders the tracked topic as a standalone HTML report.

## Data model (`redlens/models.py`)

SQLModel tables: `user`, `post`, `comment`, `topic`, `topicpost`. Rows are
mapped from arctic payloads via `from_arctic` classmethods so the wire shape is
isolated from the schema. The schema is created and migrated automatically on
first use — there is no separate migration step.

## Modules

| Module | Responsibility |
| --- | --- |
| `arctic.py` | stdlib client for the arctic-shift mirror: pagination, retry, 429 `Retry-After`, descriptive User-Agent |
| `ingest.py` | `sync_user` and the streaming fetch loop that feeds `db.upsert` |
| `discovery.py` | subreddit-net discovery sources for topic tracking |
| `topics.py` | topic tracking: build/extend the net, scan it, record matches |
| `analytics.py` | derived `UserAnalytics` rollup |
| `summarize.py` / `llm.py` / `prompts/` | optional AI profile summaries via a BYO OpenAI-compatible key |
| `reporting/` | standalone HTML report rendering for `page` |
| `explore.py` | read-only in-browser DB explorer (opened read-only; cannot mutate) |
| `serve.py` | `serve` — the local listening report; read-only stdlib server + vanilla-JS dashboard |
| `config.py` | DB-path resolution, `config.toml`, env vars, optional API-key getters |
| `onboarding.py` | first-run `setup` wizard for optional keys |
| `db.py` | engine, session, `upsert` |
| `constants.py` / `data/` | tunables and bundled data files (e.g. popular subreddits) |

## Discovery

Because arctic has no text search, `track` assembles a net of candidate
subreddits and archives posts whose keywords match within it. Sources:

- `name` — subreddits whose name matches the topic (keyless)
- `global` — subreddits whose posts match, via PullPush (keyless)
- `web` — subreddits surfaced by a web search (keyless, best-effort)
- `popular` — cast over the largest subreddits
- `llm` — one cheap LLM-suggested list (needs an LLM key)

Omitting `--sources` opens an interactive picker and a curating step; the net
is then remembered and re-tracking is incremental. `--discover` widens the net
one round by following authors of matching posts.

## Listening report (`serve`)

`serve` opens a localhost dashboard over an existing DB, framed as a
**coordinated network**: every account is one cohort and the report surfaces the
deterministic, keyless coordination signals between them as **matrices** that
share one account-column order — the account × account **network matrix**
(pairwise shared subs + co-commented threads, drawn as a heatmap), per-account
volume, **brand mentions** (a curated roster — `brands.csv` next to the DB or
`--brands PATH` — counted exactly; mined proper names as the keyless fallback),
the **shared-subreddit footprint** (subs ≥2 accounts touch), and the **threads
they co-comment in** (`link_id` seen by ≥2 accounts). **Every matrix cell is
clickable** — a drawer opens with the exact posts/comments (or shared units,
for a heatmap pair) behind that cell — and any account drills into its raw
history.

It reuses `explore.py`'s pattern: a stdlib `http.server` opening the DB
**read-only**, a JSON API, and one self-contained vanilla-JS page (no build
step, no framework, no LLM key) in the redlens report style (light, one
`constants.ACCENT` red). This is the first slice of the paid listening report;
per-account `gpt-4o-mini` profiles with a `coordinated?` flag, brand
share-of-voice, and view-time NL-plots are later slices, and the stdlib server
is a cheap swap for a hosted front door when that era arrives.

## Configuration

DB path resolves with this precedence: `--db` flag → `REDLENS_DB` env →
`[storage] db` in `config.toml` → the per-user data directory. An optional LLM
key (for summaries and the `llm` discovery source) lives in `config.toml`
(mode 600) or the environment, which always wins over the file. Everything
works with no config at all.

The file uses `[storage] db` and `[llm] api_key`; the matching env vars are
`REDLENS_DB` and `REDLENS_LLM_API_KEY` (falling back to `OPENAI_API_KEY`).

**Fresh data / Reddit's official API.** Not integrated: as of late 2025 Reddit
gates its API behind pre-approval and no longer issues keys on request, so the
keyless arctic-shift mirror is the only data source (it lags live Reddit by
weeks). A BYO-key fresh-data provider could be built if a user supplies working
Reddit credentials.

## Development

```bash
pip install -e ".[dev]"   # install with dev extras
pytest                    # tests run offline; network-marked: pytest -m integration
ruff check .              # lint
mypy redlens              # types
```

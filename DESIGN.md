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
- **Deterministic first, LLM optional.** Every analysis view is computed from
  the data with no key. LLM calls (summaries, canonicalization, the per-account
  read) are isolated behind pure prompt builders and `parse_*` parsers, so the
  network never depends on a model and each call is trivially testable offline.
- **No global text search.** Arctic has no full-text search, so topic tracking
  builds a *subreddit net* and scans within it (see Stage 1).

## Two subjects

- **Users** — `sync` archives a user's public history; `show`/`analytics`
  print a rollup; `summarize` produces an AI profile (optional, BYO LLM key).
- **Topics** — `track` discovers a subreddit net and archives every matching
  post across it; `page` renders the tracked topic as a standalone HTML report.

## The investigation pipeline

The package mirrors a six-stage flow: **archive** the raw history, **discover**
the coordinated network inside it, **expand** the network to undetected
members, **catalogue** the brands it pushes, **verify** which of those brands it
seeded, and **expose** the result. The `network/` package is one module per
stage; `redlens.network.Network` is a thin **facade** whose every method
delegates in one line to a stage module, all sharing one read-only `Store` (the
DB connection + low-level helpers). Each CLI verb lands on a stage:

| # | Stage | Modules | CLI verb |
| --- | --- | --- | --- |
| 1 | **Archive** | `arctic`, `ingest`, `discovery`, `topics`, `db`, `models` | `sync`, `track` |
| 2 | **Discover** the network | `network/coactivity` | `serve` (matrix) |
| 3 | **Expand** it | `network/leads` | `leads` |
| 4 | **Catalogue** its brands | `network/brands` | `brands` |
| 5 | **Verify** seeding | `network/seeding` | `seeding` |
| 6 | **Expose** | `serve`, `reporting/expose`, `reporting/` | `serve`, `report`, `page` |

### 1 · Archive (`arctic`, `ingest`, `discovery`, `topics`, `db`, `models`)

`arctic.py` is the stdlib client for the arctic-shift mirror (pagination, retry,
429 `Retry-After`, descriptive User-Agent). `ingest.sync_user` streams a user's
history into `db.upsert`. Because arctic has no text search, a **topic** isn't a
query — it's a *net*: `discovery` assembles candidate subreddits and `topics`
scans them, archiving posts whose keywords match. Sources:

- `name` — subreddits whose name matches the topic (keyless)
- `global` — subreddits whose posts match, via PullPush (keyless)
- `web` — subreddits surfaced by a web search (keyless, best-effort)
- `popular` — cast over the largest subreddits
- `llm` — one cheap LLM-suggested list (needs an LLM key)

Omitting `--sources` opens an interactive picker; the net is remembered and
re-tracking is incremental. `--discover` widens the net one round by following
authors of matching posts.

**Data model (`models.py`).** SQLModel tables: `user`, `post`, `comment`,
`topic`, `topicpost`. Rows map from arctic payloads via `from_arctic`
classmethods so the wire shape is isolated from the schema. The schema is
created and migrated automatically on first use — no separate migration step.

### 2 · Discover the network (`network/coactivity`)

With the history archived, the network is read as a **coordinated network**:
every account is one cohort and the deterministic, keyless coordination signals
between them become **matrices** sharing one account-column order. `coactivity`
computes who shares subreddits and threads with whom (`pairs`), the shared-sub
and co-commented-thread footprints, and the exact posts/comments behind any
matrix cell (`pair_evidence`). This is the account × account **network matrix**
drawn as a heatmap in `serve`.

### 3 · Expand it (`network/leads`)

`leads` grows the labeled cohort without an LLM. `suggested_coordinated`
surfaces unlabeled accounts pushing many distinct roster brands; `verify_leads`
scores those candidates for coordinated-block membership from three
deterministic signals and emits a promote-ready CSV — closing the **detect →
verify → promote** loop. The CSV feeds straight back into `serve --promote` /
`report --promote`.

### 4 · Catalogue its brands (`network/brands`)

`brands` builds the mention matrix — exact roster counting when a `brands.csv`
roster is given, mined proper names as the keyless fallback — and splits each
brand's Reddit conversation into coordinated vs organic (**share of voice**).
The `brands` verb mines the cohort's roster and merges it into a roster CSV,
LLM-canonicalizing the mined names when a key is set (the LLM call is a pure
builder/parser pair; without a key it merges raw).

### 5 · Verify seeding (`network/seeding`)

With ≥2 labeled cohorts, `seeding` judges each roster brand **seeded** (pushed
first by the coordinated network) vs adopted **organically**, from deterministic
signals. It also computes the multi-cohort views: seeding **waves** + the
coordination **raster** (brands arriving in a synchronized burst), the cohort
comparison / timeline / bridges, and the per-cohort outbound-domain catalogue.

### 6 · Expose (`serve`, `reporting/expose`, `reporting/`)

Two front doors over the same computations, plus the static topic report:

- **`serve`** — a localhost dashboard. It reuses `explore.py`'s pattern: a
  stdlib `http.server` opening the DB **read-only**, a JSON API, and one
  self-contained vanilla-JS SPA (`serve_assets/index.html`) in the redlens
  report style (light, one `constants.ACCENT` red). The landing page shows the
  stats, the heatmap, and the accounts table; the other matrices sit in
  collapsed sections. **Every matrix cell is clickable** (a drawer opens with
  the units behind it), and every account name opens a hash-routed **profile
  view** (`#/user/<name>`): identity stats, subreddit breakdown, top co-actors,
  brand mentions, and raw paginated activity (`network/profiles`). With a key,
  the profile view runs an on-demand **AI profile** — a cheap-model persona +
  `coordinated?` verdict grounded in sampled content and the deterministic
  signals, cached per server run. **Cohort labels** (`cohorts.csv` or
  `--cohorts`) group the matrices by cohort and scope them so the coordinated
  block reads as a block; a **Share of voice** section then ranks the brands the
  network most dominates.
- **`report`** (`reporting/expose`) — renders that exact dashboard as **one
  self-contained static HTML file**, no server. It builds the same `Network`,
  iterates `serve.ENDPOINTS` and calls each handler to pre-compute a snapshot of
  every payload the SPA would fetch, embeds it, and the SPA's single `getJSON`
  seam resolves against the snapshot instead of the network. A shareable exposé.
- **`page`** (`reporting/page`) — the standalone HTML report for a tracked
  topic. `reporting/html.py` holds the shared HTML primitives both exports use.

## Configuration

DB path resolves with this precedence: `--db` flag → `REDLENS_DB` env →
`[storage] db` in `config.toml` → the per-user data directory. An optional LLM
key (for summaries, the `llm` discovery source, and brand canonicalization)
lives in `config.toml` (mode 600) or the environment, which always wins over the
file. Everything works with no config at all.

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
make check                # ruff + mypy + pytest, exactly as CI runs them
make coverage             # coverage report (term-missing)
make coverage-gate        # the CI coverage floor — fails under the threshold
pytest -m integration     # opt into the network-marked arctic tests
```

CI (`.github/workflows/ci.yml`) runs ruff + mypy strict + pytest across
3 OS × 3 Python versions, collapsed into one required `ci-gate` check, plus a
single-cell `coverage` job that enforces the floor. `make check` mirrors it.

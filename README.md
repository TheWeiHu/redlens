# redlens

Archive and analyze public Reddit history, locally. Built on
[arctic-shift](https://arctic-shift.photon-reddit.com). Three tables
(users, posts, comments), one derived analytic, one fetch function, one
CLI. Your data stays in a local SQLite file you own.

*The name: a lens on public discussion. Today redlens archives and
examines users; tracking topics across public discussion is where it's
headed.*

## Install

```bash
pip install -e ".[dev]"
```

## Use

```bash
redlens sync KimJongFunk              # archive a user's public history
redlens show KimJongFunk              # print rollup
redlens show KimJongFunk --json       # or as JSON
redlens list                          # every archived user (counts, last activity)
redlens export KimJongFunk --format csv > kim.csv   # dump posts + comments
redlens explore                       # browse the DB in your browser
```

`show` was previously called `analytics`; the old name still works for one
release. `export` writes to stdout by default (pipeable) — pass `-o PATH` to
write a file, and `--format json|csv|jsonl` (default `json`) to pick the shape.

Track a **topic** instead of a user, then render it as a page:

```bash
redlens track "dua lipa"   # discover a subreddit net, archive every match
redlens topics             # every tracked topic (keywords, net, match count)
redlens page  "dua lipa"   # render a standalone HTML report
redlens page  --all        # render every topic + an index.html linking them
```

`track` builds a subreddit net (arctic has no global text search) from
several discovery sources you pick from, plus a curating picker. Run
`redlens track --help` for the discovery sources and flags.

No setup needed — the schema is created (and migrated) automatically on
first use. No API keys are needed — arctic-shift is a free, open mirror.

## Walkthrough: track a topic

A worked example — track an ambiguous topic, inspect it, then render a page.
We follow `ubi` (universal basic income), which collides with *Ubisoft* and the
game *Rainbow Six*, so we steer the keywords and drop the noise:

```bash
redlens track ubi \
  --query 'ubi, universal basic income' \
  --exclude 'ubisoft, rainbow six' \
  --sources name,global \
  --yes
```

`--query` terms are comma-separated and OR'd; `--exclude` drops any post
containing one of its terms; `--sources name,global` picks the discovery net
non-interactively (instead of the picker); `--yes` accepts the found subreddits
without curating. `track` first builds that subreddit *net* (arctic has no
global text search), then archives every matching post across it. Progress
streams to stderr, and the result line lands on stdout:

```
  r/BasicIncome: 412 new
  r/economy: 1209 new
  ...
'ubi': 1,847 new posts across 9 subreddits (keywords 'ubi, universal basic income', last 180 days)
next: redlens page 'ubi'
```

Inspect what's tracked — `topics` rolls up every topic (keywords, net size,
match count, last run):

```bash
redlens topics
```
```
ubi: 1,847 posts across 9 subreddits · keywords 'ubi, universal basic income' · tracked 2m ago
```

Re-running `track ubi` is incremental — the net is remembered and only new
posts are pulled. Add `--comments` to also archive the comment threads under
matched posts. Finally, render a standalone, dependency-free HTML report:

```bash
redlens page ubi          # writes ./ubi.html  (-o PATH to choose the path)
```
```
wrote ubi.html (84,210 bytes)
```

Tracking several topics? `redlens page --all` renders each one plus a small
`index.html` that links them, into a directory (`-o DIR`, default the per-user
reports dir); topics with no matched posts yet are skipped and noted on the
index.

See `redlens track --help` for every discovery source (`name`, `global`, `web`,
`popular`, `llm`) and `--discover`, which widens the net by following the
authors of matched posts.

## Optional API key

An optional **LLM API key** (OpenAI or any OpenAI-compatible endpoint) powers
`redlens summarize` and the `llm` discovery source in `track`. The first
interactive run offers to collect it once, or run the wizard anytime with
`redlens setup`. It's stored (mode 600) in your per-user config dir
(`REDLENS_CONFIG` to override; env vars override the file) — see
[DESIGN.md](DESIGN.md).

> **No Reddit official-API integration.** As of late 2025 Reddit gates its API
> behind a pre-approval process and no longer hands out keys on request, so
> redlens doesn't integrate it — the keyless arctic-shift mirror is the data
> source. If you already hold Reddit API credentials and want fresh-from-Reddit
> sync, [open an issue](https://github.com/TheWeiHu/redlens/issues) and we'll
> build the provider around your key.

## Data

Everything lands in one SQLite file you own, created automatically on first use
in your per-user data directory. Point elsewhere with the `--db` flag,
`REDLENS_DB`, or `[storage] db` in the config file — see [DESIGN.md](DESIGN.md).

## Explore

Browse the database in your browser — tables and row counts, schema, sortable
and searchable rows, and a read-only SQL console with preset analyses:

```bash
redlens explore                       # opens the default DB, pops a browser
redlens explore --port 9000 --no-browser
```

The DB is opened read-only, so nothing here can mutate it.

## Shell completions

`redlens completions {bash|zsh|fish}` prints a completion script for your shell —
generated from the CLI itself, so it never drifts from the real subcommands and
flags. Install the one-liner for your shell:

```bash
# bash — add to ~/.bashrc
redlens completions bash > ~/.local/share/bash-completion/completions/redlens

# zsh — drop on your $fpath (then restart the shell)
redlens completions zsh > "${fpath[1]}/_redlens"

# fish
redlens completions fish > ~/.config/fish/completions/redlens.fish
```

## Dependencies

redlens keeps its runtime footprint small — two direct dependencies, and the
whole transitive tree is permissively licensed (MIT/BSD/PSF), so embedding or
redistributing redlens carries no copyleft obligations.

| Package             | License  | Why it's here                                    |
| ------------------- | -------- | ------------------------------------------------ |
| `platformdirs`      | MIT      | per-user data/config dir (direct)                |
| `sqlmodel`          | MIT      | models + SQLite layer (direct)                   |
| ↳ `pydantic`        | MIT      | validation, via sqlmodel                         |
| ↳ `pydantic-core`   | MIT      | pydantic's compiled core                         |
| ↳ `annotated-types` | MIT      | pydantic constraint types                        |
| ↳ `typing-inspection` | MIT    | pydantic runtime typing helpers                  |
| ↳ `SQLAlchemy`      | MIT      | SQL engine, via sqlmodel                         |
| ↳ `greenlet`        | MIT      | SQLAlchemy async extra (platform-dependent)      |
| ↳ `typing-extensions` | PSF-2.0 | backported typing, used throughout              |

Dev-only extras (`pip install -e ".[dev]"`): `pytest`, `ruff`, `mypy` — all
permissively licensed and never shipped to users.

Bundled data in `redlens/data/` ships inside the package: the topic-modeling
stopword and popular-subreddit lists (first-party), plus `sentiment_lexicon.txt`
— the word valences behind the sentiment-over-time chart, derived from the
[VADER lexicon](https://github.com/cjhutto/vaderSentiment) (C.J. Hutto, MIT).

`sqlmodel` is still `0.0.x` and can break API on any patch bump, so it is pinned
`>=0.0.14,<0.1` to keep redlens off a future `0.1` that may move things.

## Contributing & releases

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup and how a new version is
cut and published (tag-driven, PyPI trusted publishing).

---

Architecture, configuration, and development: see [DESIGN.md](DESIGN.md).

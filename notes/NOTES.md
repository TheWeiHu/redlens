# RedditPages Notes

## Sources

- arctic-shift: `/api/users/search`, `/api/posts/search`, `/api/comments/search`
- All paginate by `created_utc` descending via `before=` cursor
- arctic is authoritative for archived data; Reddit's own API gates
  unauthenticated traffic, so it's not a viable backup

## Schema (3 tables)

### `users`
- `username` (PK, case-insensitive)
- `author_fullname` — Reddit's `t2_xxx` ID, stored bare
- `arctic_meta_json` — arctic's `_meta` envelope as JSON. Carries the
  counters (`num_posts`, `num_comments`, `*_karma`), the span
  (`earliest_*_at`, `last_*_at`), and arctic's own freshness markers
  (`post_stats_updated_at`, `comment_stats_updated_at`). Kept as JSON
  because it's a small, infrequently-queried envelope; promote individual
  fields to columns the first time a feature needs to filter on them.
- `fetched_at`

### `posts`
Arctic returns ~95 fields per post; we keep **9**:
`post_id, author_username, subreddit_name, created_utc, title, selftext, url, score, num_comments`
(plus `fetched_at`).

Index: `(author_username, created_utc DESC)`.

### `comments`
Arctic returns ~75; we keep **8**:
`comment_id, author_username, subreddit_name, link_id, parent_id, created_utc, body, score`
(plus `fetched_at`).

`link_id` is stored bare (no `t3_` prefix). `parent_id` is stored **with**
its prefix because the prefix is meaningful: `t1_xxx` = reply to comment,
`t3_xxx` = top-level on a post.

Index: `(author_username, created_utc DESC)`.

## What gets dropped from arctic

Most fields arctic returns are noise for our use case:

- **viewer state** — `likes`, `saved`, `clicked`, `visited`, `hidden`,
  `profile_*` (all null for unauthenticated reads)
- **moderator fields** — `approved_*`, `banned_*`, `mod_*`, `removal_*`,
  `*_reports` (always null for unauthenticated reads)
- **dead awards** — `all_awardings`, `gildings`, `total_awards_received`
  (Reddit removed the awards system in 2023)
- **flair UI metadata** — only `*_flair_text` is signal; `*_color`,
  `*_css_class`, `*_richtext`, `*_template_id` are styling
- **Reddit infrastructure** — `websocket_url`, `secure_media_embed`,
  `thumbnail_*`, `pwls`, `wls`, `is_robot_indexable`, etc.

The `from_arctic` classmethods on `Post` and `Comment` are the single
place we make this cut — extending the model is one diff.

### `subredditmoderator`
One row per **(subreddit, moderator)** — composite PK. Captures who moderates
the top-100 subreddits.

`subreddit_name, moderator_username, rank, as_of_date, as_of_utc,
snapshot_timestamp, source, list_complete` (plus `fetched_at`).

- `as_of_date` — **the date the row was actually accurate**, not when we
  fetched it. Reddit gated logged-out moderator lists in 2021, so almost all
  of this is read from Internet Archive snapshots (dates span 2019–2021).
  Always read mod data relative to `as_of_date`.
- `rank` — position in the list (1 = most senior).
- `source` — `about-page` (full list from archived `/about/moderators`),
  `front-page sidebar` (≤10-mod teams, complete), or
  `front-page sidebar (union)` (large teams rebuilt by unioning the capped
  10-mod sidebar across snapshots).
- `list_complete` — `False` when the sub's roster is partial (the front-page
  sidebar capped at 10 and some junior mods were never publicly archived:
  travel, stocks, AnimalsBeingDerps). r/ChatGPT has **no** rows — it was
  created after the 2021 gate, so no logged-out list ever existed.

Load: `python scripts/load_moderators.py --db redditpages.db --json mods.json`.

## Analytics

One model, one query per call: `UserAnalytics` (see `analytics.py`).

Fields: `total_posts`, `total_comments`, `post_karma`, `comment_karma`,
`total_karma`, `first_event_at`, `last_event_at`, `active_days`,
`distinct_subreddits`, `top_subreddit`, `top_subreddit_event_count`.

Not persisted. Recomputing is one CTE plus one tie-broken top-N query;
materializing would mean cache invalidation for no gain at v0.1 scale.

## Three clocks

| clock | source | answers |
|---|---|---|
| `created_utc` (per item) | Reddit | when did the user write this? |
| `arctic_meta._stats_updated_at` (per user) | arctic | when did arctic last reconcile counters? |
| `fetched_at` (per row) | us | when did we write this row? |

Keep all three. Conflating any two makes freshness debugging guesswork —
"reddit shows X, arctic says Y, we say Z" is only answerable when every
clock is stored.

## Design calls

1. **sqlite3 stdlib, no ORM.** SQL is inline in `analytics.py`. The query
   IS the analytics — adding a layer would hide that.
2. **Dataclasses, not Pydantic.** Zero runtime dependencies. If we ever
   need validation at a boundary, add it just there.
3. **Analytics is a query, not a table.** Cheap, no invalidation. Promote
   to a materialization the first time a query exceeds 100ms.
4. **Synchronous everything.** No async, no worker pool. Wrap
   `sync_user()` from outside when you need parallelism.
5. **SQLite for v0.1, Postgres-shaped DDL.** Swap is a connection-string
   change plus widening `INTEGER` → `BIGINT` and `INTEGER NOT NULL` →
   `BOOLEAN` for the flag columns.

## Not in scope

- Public-listing scrape (Reddit's `old.reddit.com` HTML) for
  visible-vs-hidden subreddit diffs
- HTML report rendering
- Cross-user analytics, comparisons
- Background sync / scheduling
- Subreddit dimension table, post comments tree

Each one is a natural follow-up; the schema doesn't need to change to add
any.

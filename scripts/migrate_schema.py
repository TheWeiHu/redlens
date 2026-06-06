"""One-off migration to the normalized schema.

What it does, atomically (SQLite DDL is transactional — a failure rolls the
whole thing back, so it is safe to re-run):

1. userstat   — split each user's flat ``arctic_meta_json`` blob into two rows
                (post / comment). For the one user arctic had no _meta for, the
                same stats are derived from their post/comment rows.
2. subreddit  — a stable dimension, one row per subreddit seen in any post,
                comment, or moderator list.
3. moderator  — copy of the old ``subredditmoderator`` table (snapshot columns
                kept on the rows, where they belong).
4. drop the old ``subredditmoderator`` table and ``user.arctic_meta_json``.

    python scripts/migrate_schema.py            # ../data/important.db
    python scripts/migrate_schema.py --db x.db
"""
from __future__ import annotations

import argparse
import sqlite3

from redditpages.db import connect, data_db, init_schema

NOW = "CAST(strftime('%s','now') AS INTEGER)"

USERSTAT_FROM_JSON = f"""
INSERT OR IGNORE INTO userstat
  (username, kind, event_count, karma, earliest_at, last_at, stats_updated_at, fetched_at)
SELECT username, 'post',
       json_extract(arctic_meta_json,'$.num_posts'),
       json_extract(arctic_meta_json,'$.post_karma'),
       json_extract(arctic_meta_json,'$.earliest_post_at'),
       json_extract(arctic_meta_json,'$.last_post_at'),
       json_extract(arctic_meta_json,'$.post_stats_updated_at'), {NOW}
FROM user WHERE arctic_meta_json IS NOT NULL
UNION ALL
SELECT username, 'comment',
       json_extract(arctic_meta_json,'$.num_comments'),
       json_extract(arctic_meta_json,'$.comment_karma'),
       json_extract(arctic_meta_json,'$.earliest_comment_at'),
       json_extract(arctic_meta_json,'$.last_comment_at'),
       json_extract(arctic_meta_json,'$.comment_stats_updated_at'), {NOW}
FROM user WHERE arctic_meta_json IS NOT NULL
"""

# For users arctic gave no _meta for, derive the same five measures from the
# post/comment rows we actually hold (stats_updated_at is left NULL — we did
# not get it from arctic, we computed it).
USERSTAT_DERIVED = f"""
INSERT OR IGNORE INTO userstat
  (username, kind, event_count, karma, earliest_at, last_at, stats_updated_at, fetched_at)
SELECT u.username, 'post',
  (SELECT count(*)               FROM post p WHERE p.author_username=u.username),
  (SELECT coalesce(sum(score),0) FROM post p WHERE p.author_username=u.username),
  (SELECT min(created_utc)       FROM post p WHERE p.author_username=u.username),
  (SELECT max(created_utc)       FROM post p WHERE p.author_username=u.username),
  NULL, {NOW}
FROM user u WHERE u.arctic_meta_json IS NULL
UNION ALL
SELECT u.username, 'comment',
  (SELECT count(*)               FROM comment c WHERE c.author_username=u.username),
  (SELECT coalesce(sum(score),0) FROM comment c WHERE c.author_username=u.username),
  (SELECT min(created_utc)       FROM comment c WHERE c.author_username=u.username),
  (SELECT max(created_utc)       FROM comment c WHERE c.author_username=u.username),
  NULL, {NOW}
FROM user u WHERE u.arctic_meta_json IS NULL
"""

SUBREDDIT_FILL = f"""
INSERT OR IGNORE INTO subreddit (name, fetched_at)
SELECT name, {NOW} FROM (
  SELECT subreddit_name AS name FROM post
  UNION SELECT subreddit_name FROM comment
  UNION SELECT subreddit_name FROM subredditmoderator
)
"""

MODERATOR_COPY = """
INSERT OR IGNORE INTO moderator
  (subreddit_name, moderator_username, rank, as_of_date, as_of_utc,
   snapshot_timestamp, source, list_complete, fetched_at)
SELECT subreddit_name, moderator_username, rank, as_of_date, as_of_utc,
       snapshot_timestamp, source, list_complete, fetched_at
FROM subredditmoderator
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=data_db("important.db"))
    args = ap.parse_args()

    # Create the new tables (userstat, subreddit, moderator) from the models.
    engine = connect(args.db)
    init_schema(engine)
    engine.dispose()  # release the connection before the raw DDL transaction

    con = sqlite3.connect(args.db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    user_cols = {r[1] for r in con.execute("PRAGMA table_info(user)")}
    has_old = "subredditmoderator" in tables
    has_meta = "arctic_meta_json" in user_cols
    if not has_old and not has_meta:
        print("already migrated — nothing to do")
        con.close()
        return 0

    try:
        con.execute("BEGIN")
        con.execute(USERSTAT_FROM_JSON)
        con.execute(USERSTAT_DERIVED)
        con.execute(SUBREDDIT_FILL)
        con.execute(MODERATOR_COPY)
        con.execute("DROP TABLE subredditmoderator")
        con.execute("ALTER TABLE user DROP COLUMN arctic_meta_json")
        con.commit()
    except Exception:
        con.rollback()
        raise

    def count(t: str) -> int:
        return con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]

    print(
        f"migrated → userstat={count('userstat')} rows, "
        f"subreddit={count('subreddit')} rows, moderator={count('moderator')} rows; "
        f"dropped subredditmoderator + user.arctic_meta_json"
    )
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

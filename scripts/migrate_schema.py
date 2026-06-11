"""One-off migration to the current schema. Idempotent and transactional
(SQLite DDL rolls back as a unit), so it is safe to re-run.

It brings an older ``redditpages.db`` to the shape the models now declare:

* user       — arctic activity stats flattened onto the row as plain columns
               (num_posts, post_karma, earliest_post_at, …). Backfilled from
               whichever older form the DB has: the ``arctic_meta_json`` blob,
               or the intermediate ``userstat`` table. For the one user arctic
               had no stats for, they are derived from their post/comment rows.
* subreddit  — a stable dimension, one row per subreddit seen in any post,
               comment, or moderator list.
* moderator  — renamed from ``subredditmoderator`` (snapshot columns kept).

Old artifacts (``userstat``, ``subredditmoderator``, ``user.arctic_meta_json``)
are dropped once their data has been copied across.

    python scripts/migrate_schema.py            # ../data/redditpages.db
    python scripts/migrate_schema.py --db x.db
"""
from __future__ import annotations

import argparse
import sqlite3

from redditpages.db import connect, data_db, init_schema

NOW = "CAST(strftime('%s','now') AS INTEGER)"

# Stat columns flattened onto user, paired with the arctic _meta key they map
# from. Order matters only for readability.
STAT_COLS = [
    "num_posts", "num_comments", "post_karma", "comment_karma",
    "earliest_post_at", "last_post_at", "earliest_comment_at", "last_comment_at",
    "post_stats_updated_at", "comment_stats_updated_at",
]

# user stat columns <- userstat rows (pivot kind into columns)
PIVOT_FROM_USERSTAT = """
UPDATE user SET
  num_posts          = (SELECT event_count      FROM userstat s WHERE s.username=user.username AND s.kind='post'),
  post_karma         = (SELECT karma            FROM userstat s WHERE s.username=user.username AND s.kind='post'),
  earliest_post_at   = (SELECT earliest_at       FROM userstat s WHERE s.username=user.username AND s.kind='post'),
  last_post_at       = (SELECT last_at           FROM userstat s WHERE s.username=user.username AND s.kind='post'),
  post_stats_updated_at = (SELECT stats_updated_at FROM userstat s WHERE s.username=user.username AND s.kind='post'),
  num_comments       = (SELECT event_count      FROM userstat s WHERE s.username=user.username AND s.kind='comment'),
  comment_karma      = (SELECT karma            FROM userstat s WHERE s.username=user.username AND s.kind='comment'),
  earliest_comment_at= (SELECT earliest_at       FROM userstat s WHERE s.username=user.username AND s.kind='comment'),
  last_comment_at    = (SELECT last_at           FROM userstat s WHERE s.username=user.username AND s.kind='comment'),
  comment_stats_updated_at = (SELECT stats_updated_at FROM userstat s WHERE s.username=user.username AND s.kind='comment')
"""

# user stat columns <- arctic_meta_json blob
FILL_FROM_JSON = """
UPDATE user SET
  num_posts          = json_extract(arctic_meta_json,'$.num_posts'),
  num_comments       = json_extract(arctic_meta_json,'$.num_comments'),
  post_karma         = json_extract(arctic_meta_json,'$.post_karma'),
  comment_karma      = json_extract(arctic_meta_json,'$.comment_karma'),
  earliest_post_at   = json_extract(arctic_meta_json,'$.earliest_post_at'),
  last_post_at       = json_extract(arctic_meta_json,'$.last_post_at'),
  earliest_comment_at= json_extract(arctic_meta_json,'$.earliest_comment_at'),
  last_comment_at    = json_extract(arctic_meta_json,'$.last_comment_at'),
  post_stats_updated_at = json_extract(arctic_meta_json,'$.post_stats_updated_at'),
  comment_stats_updated_at = json_extract(arctic_meta_json,'$.comment_stats_updated_at')
WHERE arctic_meta_json IS NOT NULL
"""

# For users still without stats, derive from the rows we hold. stats_updated_at
# stays null — we computed these, arctic did not report them.
DERIVE_FROM_ROWS = """
UPDATE user SET
  num_posts        = (SELECT count(*)               FROM post p WHERE p.author_username=user.username),
  post_karma       = (SELECT coalesce(sum(score),0) FROM post p WHERE p.author_username=user.username),
  earliest_post_at = (SELECT min(created_utc)        FROM post p WHERE p.author_username=user.username),
  last_post_at     = (SELECT max(created_utc)        FROM post p WHERE p.author_username=user.username),
  num_comments        = (SELECT count(*)               FROM comment c WHERE c.author_username=user.username),
  comment_karma       = (SELECT coalesce(sum(score),0) FROM comment c WHERE c.author_username=user.username),
  earliest_comment_at = (SELECT min(created_utc)        FROM comment c WHERE c.author_username=user.username),
  last_comment_at     = (SELECT max(created_utc)        FROM comment c WHERE c.author_username=user.username)
WHERE num_posts IS NULL AND num_comments IS NULL
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
    ap.add_argument("--db", default=data_db("redditpages.db"))
    args = ap.parse_args()

    # Create any missing tables the models declare (subreddit, moderator).
    engine = connect(args.db)
    init_schema(engine)
    engine.dispose()  # release the connection before the raw DDL transaction

    con = sqlite3.connect(args.db)
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    user_cols = {r[1] for r in con.execute("PRAGMA table_info(user)")}
    has_userstat = "userstat" in tables
    has_old_mod = "subredditmoderator" in tables
    has_meta = "arctic_meta_json" in user_cols
    missing_cols = [c for c in STAT_COLS if c not in user_cols]

    if not (has_userstat or has_old_mod or has_meta or missing_cols):
        print("already migrated — nothing to do")
        con.close()
        return 0

    # Source the subreddit dimension from whichever moderator table exists.
    mod_src = "subredditmoderator" if has_old_mod else "moderator"
    subreddit_fill = f"""
        INSERT OR IGNORE INTO subreddit (name, fetched_at)
        SELECT name, {NOW} FROM (
          SELECT subreddit_name AS name FROM post
          UNION SELECT subreddit_name FROM comment
          UNION SELECT subreddit_name FROM {mod_src}
        )
    """

    try:
        con.execute("BEGIN")
        for col in missing_cols:
            con.execute(f"ALTER TABLE user ADD COLUMN {col} INTEGER")
        if has_userstat:
            con.execute(PIVOT_FROM_USERSTAT)
        elif has_meta:
            con.execute(FILL_FROM_JSON)
        con.execute(DERIVE_FROM_ROWS)
        con.execute(subreddit_fill)
        if has_old_mod:
            con.execute(MODERATOR_COPY)
        if has_userstat:
            con.execute("DROP TABLE userstat")
        if has_old_mod:
            con.execute("DROP TABLE subredditmoderator")
        if has_meta:
            con.execute("ALTER TABLE user DROP COLUMN arctic_meta_json")
        con.commit()
    except Exception:
        con.rollback()
        raise

    def count(t: str) -> int:
        return con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]

    with_stats = con.execute("SELECT count(*) FROM user WHERE num_posts IS NOT NULL").fetchone()[0]
    print(
        f"migrated → user={count('user')} rows ({with_stats} with stats), "
        f"subreddit={count('subreddit')} rows, moderator={count('moderator')} rows; "
        f"dropped userstat / subredditmoderator / user.arctic_meta_json where present"
    )
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

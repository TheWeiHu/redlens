"""Incremental sync: the ``sync_state`` cursors that make re-syncs cheap.

These tests drive the real ``arctic._iter_kind`` pagination (including its
``MAX_ITEMS_PER_STREAM`` interruption hook) against a fake ``_get`` that serves
a fixed dataset offline, so the cursor bookkeeping in :mod:`redlens.ingest` is
exercised end to end without touching the network.
"""
from __future__ import annotations

from typing import Any

from sqlmodel import Session, SQLModel, func, select

from redlens import arctic
from redlens.db import SCHEMA_VERSION, connect, init_schema
from redlens.ingest import _get_sync_state, sync_user
from redlens.models import Comment, Post


def _posts(n: int, author: str = "alice", start: int = 1000) -> list[dict[str, Any]]:
    return [{"id": f"p{i}", "author": author, "subreddit": "python",
             "created_utc": start + i, "title": f"t{i}", "score": 1,
             "num_comments": 0} for i in range(n)]


def _comments(n: int, author: str = "alice", start: int = 5000) -> list[dict[str, Any]]:
    return [{"id": f"c{i}", "author": author, "subreddit": "python",
             "link_id": f"t3_x{i}", "created_utc": start + i, "body": "b",
             "score": 1} for i in range(n)]


class FakeArctic:
    """Stands in for ``arctic._get``: serves desc-sorted pages honoring the
    ``after``/``before`` window, and records each post/comment search."""

    def __init__(self, posts: list[dict[str, Any]], comments: list[dict[str, Any]],
                 meta: dict[str, Any] | None) -> None:
        self.posts = posts
        self.comments = comments
        self.meta = meta
        self.calls: list[tuple[str, int | None, int | None]] = []

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        if path == "/api/users/search":
            return {"data": [self.meta] if self.meta else []}
        kind = "posts" if "posts" in path else "comments"
        data = self.posts if kind == "posts" else self.comments
        after, before = params.get("after"), params.get("before")
        self.calls.append((kind, after, before))
        items = [d for d in data
                 if (after is None or d["created_utc"] > after)
                 and (before is None or d["created_utc"] < before)]
        items.sort(key=lambda d: int(d["created_utc"]), reverse=True)
        return {"data": items[:50]}  # arctic's auto page ~50


def _install(monkeypatch, fake: FakeArctic, cap: int | None = None) -> None:
    monkeypatch.setattr(arctic, "_get", fake.get)
    monkeypatch.setattr(arctic, "MAX_ITEMS_PER_STREAM", cap)


def _mem():
    engine = connect(":memory:")
    init_schema(engine)
    return engine


# --- migration -------------------------------------------------------------

def test_migration_upgrades_v3_through_latest(tmp_path):
    """A v3 database upgrades to the current schema: it gains the v4 sync_state
    table, the v5 topicpost relevance columns, and the v6 topic.about column.
    (Build the latest schema, then strip what v4/v5/v6 added so the starting DB
    faithfully looks like v3.)"""
    db = tmp_path / "v3.db"
    engine = connect(db)
    SQLModel.metadata.create_all(engine)
    with engine.begin() as con:
        con.exec_driver_sql("DROP TABLE sync_state")          # v4 added this
        for c in ("relevant", "relevance_confidence", "relevance_reason",
                  "relevance_model", "relevance_at"):         # v5 added these
            con.exec_driver_sql(f"ALTER TABLE topicpost DROP COLUMN {c}")
        con.exec_driver_sql("ALTER TABLE topic DROP COLUMN about")  # v6 added this
        con.exec_driver_sql("PRAGMA user_version = 3")

    init_schema(engine)

    with engine.begin() as con:
        version = int(con.exec_driver_sql("PRAGMA user_version").scalar())
        sync_state = con.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_state'"
        ).first()
        tp_cols = {r[1] for r in con.exec_driver_sql(
            "PRAGMA table_info(topicpost)")}
        topic_cols = {r[1] for r in con.exec_driver_sql(
            "PRAGMA table_info(topic)")}
    assert version == SCHEMA_VERSION == 6
    assert sync_state is not None
    assert {"relevant", "relevance_confidence", "relevance_reason",
            "relevance_model", "relevance_at"} <= tp_cols
    assert "about" in topic_cols


def test_migration_upgrades_pre_versioning_v1_db(tmp_path):
    """A pre-versioning database (tables present, user_version 0 -> treated as
    v1) upgrades cleanly to the latest schema. Regression: migration 3 DROPs
    topic/topicpost, so the v5/v6 column-adds must skip those tables (create_all
    rebuilds them) rather than ALTER a missing table and abort init_schema."""
    db = tmp_path / "v1.db"
    engine = connect(db)
    with engine.begin() as con:
        # The v0.2 baseline shape: topic with no exclude_terms/about, the old
        # 2-column topicpost, user_version 0 with tables present.
        con.exec_driver_sql(
            "CREATE TABLE topic (id INTEGER PRIMARY KEY, name VARCHAR, "
            "keywords VARCHAR, subreddits VARCHAR, days INTEGER, "
            "newest_seen_utc INTEGER, last_tracked_at INTEGER, fetched_at INTEGER)")
        con.exec_driver_sql(
            "CREATE TABLE topicpost (topic_id INTEGER, post_id VARCHAR, "
            "PRIMARY KEY (topic_id, post_id))")
        con.exec_driver_sql("CREATE TABLE user (username VARCHAR PRIMARY KEY)")
        con.exec_driver_sql("PRAGMA user_version = 0")

    init_schema(engine)   # must not raise

    with engine.begin() as con:
        version = int(con.exec_driver_sql("PRAGMA user_version").scalar())
        tp_cols = {r[1] for r in con.exec_driver_sql("PRAGMA table_info(topicpost)")}
        topic_cols = {r[1] for r in con.exec_driver_sql("PRAGMA table_info(topic)")}
    assert version == SCHEMA_VERSION == 6
    assert "relevant" in tp_cols and "about" in topic_cols


# --- incremental no-op -----------------------------------------------------

def test_second_sync_is_one_request_per_kind_and_writes_nothing(monkeypatch):
    fake = FakeArctic(_posts(120), _comments(80), meta={"author": "alice", "id": "t2_a"})
    _install(monkeypatch, fake)
    engine = _mem()

    r1 = sync_user("alice", engine)
    assert (r1.posts_written, r1.comments_written) == (120, 80)

    fake.calls.clear()
    r2 = sync_user("alice", engine)
    assert (r2.posts_written, r2.comments_written) == (0, 0)
    # At most one request per kind on an unchanged user (the `after` window is
    # empty, so the first page is the only page).
    assert sum(1 for c in fake.calls if c[0] == "posts") == 1
    assert sum(1 for c in fake.calls if c[0] == "comments") == 1
    # And every post-search was a forward (`after`) pull, not a re-walk.
    assert all(after is not None and before is None
               for kind, after, before in fake.calls if kind == "posts")


# --- backfill resume -------------------------------------------------------

def test_interrupted_backfill_resumes_without_refetch(monkeypatch):
    fake = FakeArctic(_posts(120), [], meta={"author": "alice", "id": "t2_a"})
    # Cap the stream at 30 items -> the backfill is cut off mid-history.
    _install(monkeypatch, fake, cap=30)
    engine = _mem()

    r1 = sync_user("alice", engine)
    assert r1.posts_written == 30
    with Session(engine) as s:
        st = _get_sync_state(s, "alice", "posts")
        assert st is not None
        assert st.completed_backfill is False
        assert st.newest_seen_utc == 1119          # newest item kept
        assert st.oldest_seen_utc == 1090          # 30 newest are 1090..1119

    # Resume with no cap: it must continue from the cursor, not start over.
    _install(monkeypatch, fake, cap=None)
    fake.calls.clear()
    r2 = sync_user("alice", engine)
    assert r2.posts_written == 90                   # the remaining 90 items
    # Resume only walked from the cursor down — nothing newer was re-fetched.
    # The cursor is padded +1 (before <= 1091) so the boundary second is
    # re-queried and any same-second sibling is recovered, not skipped.
    assert all(after is None and before is not None and before <= 1091
               for kind, after, before in fake.calls if kind == "posts")

    with Session(engine) as s:
        total = s.exec(select(func.count()).select_from(Post)).one()
        st = _get_sync_state(s, "alice", "posts")
    assert total == 120                             # whole history now stored
    assert st is not None
    assert st.completed_backfill is True
    assert st.oldest_seen_utc == 1000


# --- capped incremental top-up must not strand a forward gap ---------------

def test_capped_incremental_pull_does_not_lose_the_gap(monkeypatch):
    """A capped forward top-up fetches only the newest items; everything between
    the old cursor and the oldest item it reached must still be recoverable, not
    silently skipped on the next sync."""
    fake = FakeArctic(_posts(10, start=1000), [], meta={"author": "alice", "id": "t2_a"})
    _install(monkeypatch, fake)
    engine = _mem()

    # Baseline: the first 10 items (1000..1009), fully backfilled.
    assert sync_user("alice", engine).posts_written == 10
    with Session(engine) as s:
        st = _get_sync_state(s, "alice", "posts")
        assert st is not None and st.completed_backfill is True
        assert st.newest_seen_utc == 1009

    # 100 newer items arrive (1010..1109); a capped pull reaches only the top 30.
    fake.posts = _posts(110, start=1000)
    _install(monkeypatch, fake, cap=30)
    assert sync_user("alice", engine).posts_written == 30      # 1080..1109
    with Session(engine) as s:
        total = s.exec(select(func.count()).select_from(Post)).one()
        st = _get_sync_state(s, "alice", "posts")
    assert total == 40
    # The capped top-up left a gap (1010..1079) -> backfill is NOT complete, and
    # the cursor drops to this run's floor so the next sync resumes downward.
    assert st is not None and st.completed_backfill is False
    assert st.oldest_seen_utc == 1080

    # Next (uncapped) sync closes the gap instead of querying past it.
    _install(monkeypatch, fake, cap=None)
    sync_user("alice", engine)
    with Session(engine) as s:
        total = s.exec(select(func.count()).select_from(Post)).one()
        st = _get_sync_state(s, "alice", "posts")
    assert total == 110                                         # nothing lost
    assert st is not None and st.completed_backfill is True


# --- same-second sibling at the cursor boundary ----------------------------

def test_incremental_pull_recovers_same_second_sibling(monkeypatch):
    """A new item created in the *same wall-clock second* as the prior cursor
    must not be skipped. arctic's `after` is a strict >, so a naive
    `after=newest` would exclude it permanently — the -1s pad re-queries the
    boundary second and upsert dedups the already-stored twin."""
    fake = FakeArctic(_posts(10, start=1000), [], meta={"author": "alice", "id": "t2_a"})
    _install(monkeypatch, fake)
    engine = _mem()

    sync_user("alice", engine)                       # cursor settles at 1009
    with Session(engine) as s:
        st = _get_sync_state(s, "alice", "posts")
        assert st is not None and st.newest_seen_utc == 1009

    # A new post lands in the same second as the cursor (1009), plus a clearly
    # newer one. The same-second sibling is the one a strict `after` would drop.
    fake.posts = _posts(10, start=1000) + [
        {"id": "twin", "author": "alice", "subreddit": "python",
         "created_utc": 1009, "title": "same second", "score": 1, "num_comments": 0},
        {"id": "later", "author": "alice", "subreddit": "python",
         "created_utc": 1010, "title": "newer", "score": 1, "num_comments": 0},
    ]
    sync_user("alice", engine)

    with Session(engine) as s:
        ids = {p.post_id for p in s.exec(select(Post)).all()}
    assert "twin" in ids                              # the boundary sibling survived
    assert "later" in ids


# --- --full override -------------------------------------------------------

def test_full_flag_re_walks_the_whole_history(monkeypatch):
    fake = FakeArctic(_posts(60), [], meta={"author": "alice", "id": "t2_a"})
    _install(monkeypatch, fake)
    engine = _mem()

    sync_user("alice", engine)                      # establishes the cursor
    fake.calls.clear()
    sync_user("alice", engine, full=True)
    # A full pull ignores the cursor: it walks from the top (`after` is None).
    assert all(after is None for kind, after, before in fake.calls if kind == "posts")
    assert any(c[0] == "posts" for c in fake.calls)


# --- the user-meta-missing path still streams via the cursor ---------------

def test_sync_without_user_meta_peeks_then_archives(monkeypatch):
    fake = FakeArctic(_posts(10), _comments(5), meta=None)  # arctic has no user row
    _install(monkeypatch, fake)
    engine = _mem()

    r = sync_user("alice", engine)
    assert r.user.username == "alice"
    assert (r.posts_written, r.comments_written) == (10, 5)
    with Session(engine) as s:
        assert s.exec(select(func.count()).select_from(Comment)).one() == 5


# --- `sync --all`: re-sync every user already in the DB --------------------

def test_sync_all_iterates_users_skips_failures_and_summarizes(
        tmp_path, monkeypatch, capsys):
    """`sync --all` re-syncs every user in the DB; a single user that errors
    (NotFound / arctic network blips are both RedlensError) is skipped, not
    fatal; a roll-up line reports how many synced."""
    from redlens.cli import main
    from redlens.db import upsert
    from redlens.errors import NotFound
    from redlens.ingest import SyncResult
    from redlens.models import User

    path = tmp_path / "all.db"
    engine = connect(str(path))
    init_schema(engine)
    with Session(engine) as s:
        upsert(s, [User(username="alice"), User(username="bob")])
        s.commit()

    seen: list[tuple[str, bool]] = []

    def fake_sync(name, _engine, *, full=False):
        seen.append((name, full))
        if name == "bob":
            raise NotFound("u/bob not in arctic")
        return SyncResult(User(username=name), 3, 5)

    monkeypatch.setattr("redlens.cli.sync_user", fake_sync)

    assert main(["--db", str(path), "sync", "--all"]) == 0
    out = capsys.readouterr()
    assert sorted(n for n, _ in seen) == ["alice", "bob"]   # both attempted
    assert "u/alice: 3 posts, 5 comments" in out.out
    assert "bob skipped" in out.err                         # one failure, non-fatal
    assert "synced 1/2 user(s): 3 new posts, 5 new comments (1 skipped)" in out.out


def test_sync_all_forwards_full_flag(tmp_path, monkeypatch):
    from redlens.cli import main
    from redlens.db import upsert
    from redlens.ingest import SyncResult
    from redlens.models import User

    path = tmp_path / "full.db"
    engine = connect(str(path))
    init_schema(engine)
    with Session(engine) as s:
        upsert(s, [User(username="alice")])
        s.commit()

    captured: dict[str, bool] = {}

    def fake_sync(name, _engine, *, full=False):
        captured["full"] = full
        return SyncResult(User(username=name), 0, 0)

    monkeypatch.setattr("redlens.cli.sync_user", fake_sync)
    assert main(["--db", str(path), "sync", "--all", "--full"]) == 0
    assert captured["full"] is True


def test_sync_without_username_or_all_errors(tmp_path):
    from redlens.cli import main

    path = tmp_path / "e.db"
    init_schema(connect(str(path)))
    assert main(["--db", str(path), "sync"]) == 1   # RedlensError -> exit 1

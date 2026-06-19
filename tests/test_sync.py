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

def test_migration_adds_sync_state_table(tmp_path):
    """A v3 database (no sync_state) upgrades to v4 and gains the table."""
    db = tmp_path / "v3.db"
    engine = connect(db)
    SQLModel.metadata.create_all(engine)
    with engine.begin() as con:
        con.exec_driver_sql("DROP TABLE sync_state")
        con.exec_driver_sql("PRAGMA user_version = 3")

    init_schema(engine)

    with engine.begin() as con:
        version = int(con.exec_driver_sql("PRAGMA user_version").scalar())
        present = con.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sync_state'"
        ).first()
    assert version == SCHEMA_VERSION == 4
    assert present is not None


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
    # Resume only walked *older* than the cursor — nothing newer was re-fetched.
    assert all(after is None and before is not None and before <= 1090
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

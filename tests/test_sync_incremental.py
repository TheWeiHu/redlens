"""Incremental `sync`: the SyncState cursor that turns re-syncs from full
re-pulls into cheap, resumable fetches.

The contract pinned here:
  - a second sync of an unchanged user makes at most one request per kind and
    writes nothing (the head cursor short-circuits it);
  - a backfill cut short by MAX_ITEMS_PER_STREAM resumes from the tail cursor
    on the next run and finishes, without re-walking what's already stored;
  - `--full` ignores the cursors and re-pulls everything;
  - the v4 migration creates the syncstate table on fresh and pre-v4 databases.

Arctic is stubbed at `_get`, so the real `_iter_kind` pagination (and the
MAX_ITEMS_PER_STREAM cap) runs for real — only the network is faked.
"""
from sqlmodel import Session, func, select

from redlens import arctic
from redlens.db import SCHEMA_VERSION, connect, init_schema
from redlens.ingest import sync_user
from redlens.models import Comment, Post, SyncState


def _post(pid, ts):
    return {"id": pid, "author": "alice", "subreddit": "python",
            "created_utc": ts, "title": f"post {pid}", "score": 1, "num_comments": 0}


def _comment(cid, ts):
    return {"id": cid, "author": "alice", "subreddit": "python",
            "link_id": "t3_x", "created_utc": ts, "body": f"c {cid}", "score": 1}


def fake_get(posts, comments, calls=None):
    """Stub of ``arctic._get`` over fixed post/comment lists.

    Honors ``author``/``after``/``before`` and returns one newest-first page of
    up to 50, mirroring arctic so the real ``_iter_kind`` paginates against it.
    A user-search hit is always returned so sync skips the recovery peek.
    """
    def it(path, **params):
        if calls is not None:
            calls.append((path, params))
        if path == "/api/users/search":
            return {"data": [{"author": "alice", "id": "t2_alice", "_meta": {}}]}
        data = posts if path == "/api/posts/search" else comments
        rows = [r for r in data if r["author"] == params.get("author")]
        after, before = params.get("after"), params.get("before")
        if after is not None:
            rows = [r for r in rows if r["created_utc"] > int(after)]
        if before is not None:
            rows = [r for r in rows if r["created_utc"] < int(before)]
        rows.sort(key=lambda r: r["created_utc"], reverse=True)
        return {"data": rows[:50]}
    return it


def _engine(tmp_path):
    e = connect(tmp_path / "s.db")
    init_schema(e)
    return e


def _count(engine, model):
    with Session(engine) as s:
        return s.exec(select(func.count()).select_from(model)).one()


# --- migration -------------------------------------------------------------

def test_fresh_db_has_syncstate_and_latest_version(tmp_path):
    engine = _engine(tmp_path)
    with engine.begin() as con:
        version = con.exec_driver_sql("PRAGMA user_version").scalar()
        names = {r[0] for r in con.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert version == SCHEMA_VERSION == 4
    assert "syncstate" in names


def test_pre_v4_db_gains_syncstate_on_migration(tmp_path):
    engine = _engine(tmp_path)
    # Simulate a database from before v4: drop the table and roll the stamp back.
    with engine.begin() as con:
        con.exec_driver_sql("DROP TABLE syncstate")
        con.exec_driver_sql("PRAGMA user_version = 3")
    init_schema(engine)
    with engine.begin() as con:
        version = con.exec_driver_sql("PRAGMA user_version").scalar()
        names = {r[0] for r in con.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert version == 4
    assert "syncstate" in names


# --- incremental head ------------------------------------------------------

def test_second_sync_unchanged_is_cheap_and_writes_nothing(tmp_path, monkeypatch):
    posts = [_post("p1", 100), _post("p2", 200), _post("p3", 300)]
    comments = [_comment("c1", 150)]
    monkeypatch.setattr(arctic, "_get", fake_get(posts, comments))

    first = sync_user("alice", engine := _engine(tmp_path))
    assert (first.posts_written, first.comments_written) == (3, 1)

    # First sync reached the bottom, so the cursor is complete.
    with Session(engine) as s:
        st = s.get(SyncState, ("alice", "posts", "arctic"))
        assert st.completed_backfill is True
        assert st.newest_seen_utc == 300

    calls: list = []
    monkeypatch.setattr(arctic, "_get", fake_get(posts, comments, calls))
    second = sync_user("alice", engine)

    assert (second.posts_written, second.comments_written) == (0, 0)
    post_reqs = [c for c in calls if c[0] == "/api/posts/search"]
    comment_reqs = [c for c in calls if c[0] == "/api/comments/search"]
    assert len(post_reqs) <= 1 and len(comment_reqs) <= 1
    assert _count(engine, Post) == 3 and _count(engine, Comment) == 1


# --- resumable backfill ----------------------------------------------------

def test_interrupted_backfill_resumes_from_tail(tmp_path, monkeypatch):
    posts = [_post(f"p{i}", i * 100) for i in range(1, 13)]  # 12 posts, ts 100..1200
    monkeypatch.setattr(arctic, "_get", fake_get(posts, []))

    # Cut the first walk short after 5 items.
    monkeypatch.setattr(arctic, "MAX_ITEMS_PER_STREAM", 5)
    sync_user("alice", engine := _engine(tmp_path))
    assert _count(engine, Post) == 5  # the 5 newest (ts 1200..800)
    with Session(engine) as s:
        st = s.get(SyncState, ("alice", "posts", "arctic"))
        assert st.completed_backfill is False
        assert st.newest_seen_utc == 1200 and st.oldest_seen_utc == 800

    # Lift the cap and re-run: the rest backfills from the tail.
    monkeypatch.setattr(arctic, "MAX_ITEMS_PER_STREAM", None)
    calls: list = []
    monkeypatch.setattr(arctic, "_get", fake_get(posts, [], calls))
    res = sync_user("alice", engine)

    assert res.posts_written == 7
    assert _count(engine, Post) == 12
    with Session(engine) as s:
        st = s.get(SyncState, ("alice", "posts", "arctic"))
        assert st.completed_backfill is True
        assert st.oldest_seen_utc == 100

    # No request re-walked from the top: every page was bounded by after/before,
    # so the already-stored newest 5 were never re-fetched.
    post_reqs = [p for _, p in
                 ((c[0], c[1]) for c in calls) if _ == "/api/posts/search"]
    assert post_reqs  # it did make requests
    assert all(p.get("after") is not None or p.get("before") is not None
               for p in post_reqs)


# --- full override ---------------------------------------------------------

def test_full_flag_ignores_cursor_and_repulls(tmp_path, monkeypatch):
    posts = [_post("p1", 100), _post("p2", 200)]
    monkeypatch.setattr(arctic, "_get", fake_get(posts, []))

    sync_user("alice", engine := _engine(tmp_path))  # stores both, cursor complete

    calls: list = []
    monkeypatch.setattr(arctic, "_get", fake_get(posts, [], calls))
    res = sync_user("alice", engine, full=True)

    # full=True re-walks the whole history (after is None on the head request),
    # though upsert keeps net-new at 0 since the rows already exist.
    assert res.posts_written == 0
    head = [c for c in calls
            if c[0] == "/api/posts/search" and c[1].get("after") is None]
    assert head, "full sync should issue an unbounded (after=None) head request"
    assert _count(engine, Post) == 2

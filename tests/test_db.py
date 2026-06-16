from sqlmodel import Session

from redlens.db import SCHEMA_VERSION, connect, init_schema, upsert
from redlens.models import Post, Summary, TopicPost


def _post(pid: str, score: int = 0) -> Post:
    return Post(post_id=pid, author_username="alice",
                subreddit_name="python", created_utc=1700000000, score=score)


def _engine() -> object:
    engine = connect(":memory:")
    init_schema(engine)
    return engine


def test_upsert_returns_count_of_newly_inserted_rows():
    with Session(_engine()) as s:
        assert upsert(s, [_post("a"), _post("b"), _post("c")]) == 3


def test_upsert_of_existing_rows_returns_zero():
    engine = _engine()
    with Session(engine) as s:
        upsert(s, [_post("a"), _post("b")])
        s.commit()
        # Same primary keys again -> nothing new, even though they're refreshed.
        assert upsert(s, [_post("a"), _post("b")]) == 0


def test_upsert_counts_only_the_new_rows_in_a_mixed_batch():
    engine = _engine()
    with Session(engine) as s:
        upsert(s, [_post("a")])
        s.commit()
        # 'a' exists, 'b' and 'c' are new -> 2 net-new.
        assert upsert(s, [_post("a"), _post("b"), _post("c")]) == 2


def test_upsert_still_refreshes_existing_rows():
    engine = _engine()
    with Session(engine) as s:
        upsert(s, [_post("a", score=1)])
        s.commit()
        assert upsert(s, [_post("a", score=99)]) == 0  # not new...
        s.commit()
    with Session(engine) as s:
        assert s.get(Post, "a").score == 99            # ...but updated


def test_upsert_net_new_with_composite_primary_key():
    engine = _engine()
    with Session(engine) as s:
        first = upsert(s, [TopicPost(topic_id=1, post_id="a"),
                           TopicPost(topic_id=1, post_id="b")])
        s.commit()
        assert first == 2
        # One overlapping composite key, one new -> 1 net-new.
        again = upsert(s, [TopicPost(topic_id=1, post_id="b"),
                           TopicPost(topic_id=2, post_id="b")])
        assert again == 1


def test_v4_migration_adds_summary_table_to_a_pre_v4_db(tmp_path):
    """A database stamped at v3 (no `summary` table) gains it on init, and the
    schema stamp advances to the current version."""
    db = tmp_path / "old.db"
    engine = connect(db)
    # Build the v3 baseline: every table except `summary`, stamped at v3.
    with engine.begin() as con:
        con.exec_driver_sql(
            "CREATE TABLE user (username VARCHAR PRIMARY KEY)")
        con.exec_driver_sql("PRAGMA user_version = 3")

    init_schema(engine)

    with engine.begin() as con:
        assert int(con.exec_driver_sql("PRAGMA user_version").scalar()) == SCHEMA_VERSION
        has_summary = con.exec_driver_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='summary'"
        ).first() is not None
    assert has_summary
    # And the new table is usable.
    with Session(engine) as s:
        upsert(s, [Summary(username="alice", model="m", text="hi")])
        s.commit()
        assert s.get(Summary, "alice").text == "hi"

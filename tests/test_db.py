"""The `upsert` contract: it inserts new rows, refreshes existing ones in
place, and returns the count of rows that were *newly* inserted — including
for composite-key join tables. That net-new count is what lets sync/track
report how much fresh data arrived, so it's the behavior worth pinning down.
"""
from sqlmodel import Session

from redlens.db import connect, init_schema, upsert
from redlens.models import Post, TopicPost


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

import pytest
from sqlmodel import Session

from redditpages.analytics import compute_user_analytics
from redditpages.db import connect, init_schema, upsert
from redditpages.errors import NotFound
from redditpages.models import Comment, Post, User


@pytest.fixture
def db_session():
    engine = connect(":memory:")
    init_schema(engine)
    with Session(engine) as s:
        yield s


def _post(user, pid, *, sub="askreddit", score=10, ts=1_700_000_000):
    return Post(post_id=pid, author_username=user, subreddit_name=sub,
                created_utc=ts, score=score)


def _comment(user, cid, *, sub="askreddit", score=5, ts=1_700_000_001):
    return Comment(comment_id=cid, author_username=user, subreddit_name=sub,
                   link_id="abc", parent_id=None, created_utc=ts, score=score)


def test_not_found_for_unknown_user(db_session):
    with pytest.raises(NotFound):
        compute_user_analytics(db_session, "ghost")


def test_empty_user(db_session):
    upsert(db_session, [User(username="alice")])
    a = compute_user_analytics(db_session, "alice")
    assert a.total_posts == 0
    assert a.total_comments == 0
    assert a.total_karma == 0
    assert a.first_event_at is None
    assert a.active_days == 0
    assert a.top_subreddit is None


def test_rollup_sums_correctly(db_session):
    upsert(db_session, [User(username="alice")])
    upsert(db_session, [
        _post("alice", "p1", sub="news", score=50),
        _post("alice", "p2", sub="news", score=30),
        _post("alice", "p3", sub="cats", score=10),
    ])
    upsert(db_session, [
        _comment("alice", "c1", sub="news", score=4),
        _comment("alice", "c2", sub="cats", score=2),
        _comment("alice", "c3", sub="dogs", score=1),
    ])
    a = compute_user_analytics(db_session, "alice")
    assert a.total_posts == 3
    assert a.total_comments == 3
    assert a.post_karma == 90
    assert a.comment_karma == 7
    assert a.total_karma == 97
    assert a.distinct_subreddits == 3
    assert a.top_subreddit == "news"
    assert a.top_subreddit_event_count == 3


def test_active_days_counts_distinct_calendar_dates(db_session):
    upsert(db_session, [User(username="alice")])
    upsert(db_session, [
        _post("alice", "p1", ts=1_700_000_000),  # 2023-11-14
        _post("alice", "p2", ts=1_700_001_000),  # same day
        _post("alice", "p3", ts=1_700_100_000),  # 2023-11-15
    ])
    a = compute_user_analytics(db_session, "alice")
    assert a.active_days == 2
    assert a.first_event_at == 1_700_000_000
    assert a.last_event_at == 1_700_100_000


def test_does_not_leak_across_users(db_session):
    upsert(db_session, [User(username="alice"), User(username="bob")])
    upsert(db_session, [_post("alice", "pA", score=100), _post("bob", "pB", score=200)])
    upsert(db_session, [_comment("bob", "cB", score=20)])
    assert compute_user_analytics(db_session, "alice").total_karma == 100
    assert compute_user_analytics(db_session, "bob").total_karma == 220


def test_username_lookup_is_case_insensitive(db_session):
    upsert(db_session, [User(username="Alice")])
    upsert(db_session, [_post("Alice", "p1", score=10)])
    a = compute_user_analytics(db_session, "alice")
    assert a.username == "Alice"
    assert a.total_posts == 1

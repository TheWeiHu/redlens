import csv
import io
import json

import pytest
from sqlmodel import Session

from redlens.db import connect, init_schema, upsert
from redlens.errors import NotFound
from redlens.export import export_topic, export_user
from redlens.models import Comment, Post, Topic, TopicPost, User


@pytest.fixture
def db_session():
    engine = connect(":memory:")
    init_schema(engine)
    with Session(engine) as s:
        yield s


def _seed(s):
    upsert(s, [User(username="alice"), User(username="bob")])
    upsert(s, [
        Post(post_id="p1", author_username="alice", subreddit_name="news",
             created_utc=1_700_000_000, title="hello", score=10),
        Post(post_id="p2", author_username="bob", subreddit_name="cats",
             created_utc=1_700_000_500, title="other user", score=3),
    ])
    upsert(s, [
        Comment(comment_id="c1", author_username="alice", subreddit_name="news",
                link_id="p1", created_utc=1_700_000_100, body="nice", score=4),
    ])


def test_unknown_user_raises(db_session):
    with pytest.raises(NotFound):
        export_user(db_session, "ghost", "json", io.StringIO())


def test_json_groups_posts_and_comments(db_session):
    _seed(db_session)
    out = io.StringIO()
    n_posts, n_comments = export_user(db_session, "alice", "json", out)
    assert (n_posts, n_comments) == (1, 1)
    data = json.loads(out.getvalue())
    assert data["username"] == "alice"
    assert [p["post_id"] for p in data["posts"]] == ["p1"]
    assert [c["comment_id"] for c in data["comments"]] == ["c1"]


def test_jsonl_one_record_per_line_with_kind(db_session):
    _seed(db_session)
    out = io.StringIO()
    export_user(db_session, "alice", "jsonl", out)
    lines = [json.loads(line) for line in out.getvalue().splitlines()]
    assert [r["kind"] for r in lines] == ["post", "comment"]
    assert lines[0]["post_id"] == "p1"
    assert lines[1]["comment_id"] == "c1"


def test_csv_has_kind_column_and_one_row_per_record(db_session):
    _seed(db_session)
    out = io.StringIO()
    export_user(db_session, "alice", "csv", out)
    rows = list(csv.DictReader(out.getvalue().splitlines()))
    assert [r["kind"] for r in rows] == ["post", "comment"]
    assert rows[0]["post_id"] == "p1"
    assert rows[1]["comment_id"] == "c1"


def test_export_is_scoped_to_one_user(db_session):
    _seed(db_session)
    out = io.StringIO()
    n_posts, n_comments = export_user(db_session, "bob", "json", out)
    assert (n_posts, n_comments) == (1, 0)
    data = json.loads(out.getvalue())
    assert [p["post_id"] for p in data["posts"]] == ["p2"]


def test_username_match_is_case_insensitive(db_session):
    upsert(db_session, [User(username="Alice")])
    upsert(db_session, [Post(post_id="p1", author_username="Alice",
                             subreddit_name="news", created_utc=1, score=1)])
    out = io.StringIO()
    n_posts, _ = export_user(db_session, "alice", "json", out)
    assert n_posts == 1
    assert json.loads(out.getvalue())["username"] == "Alice"


def _seed_topic(s):
    # Two posts; only p1/p2 are matched to the topic, q1 is unrelated.
    upsert(s, [
        Post(post_id="p1", author_username="alice", subreddit_name="news",
             created_utc=1_700_000_000, title="ubi pilot", score=20),
        Post(post_id="p2", author_username="bob", subreddit_name="economics",
             created_utc=1_700_000_500, title="basic income", score=5),
        Post(post_id="q1", author_username="carol", subreddit_name="cats",
             created_utc=1_700_001_000, title="unrelated", score=99),
    ])
    upsert(s, [
        Comment(comment_id="c1", author_username="alice", subreddit_name="news",
                link_id="p1", created_utc=1_700_000_100, body="great", score=7),
    ])
    s.add(Topic(id=1, name="UBI", keywords='["ubi"]', subreddits='["news"]'))
    upsert(s, [TopicPost(topic_id=1, post_id="p1"),
               TopicPost(topic_id=1, post_id="p2")])
    s.commit()


def test_unknown_topic_raises(db_session):
    with pytest.raises(NotFound):
        export_topic(db_session, "ghost", "json", io.StringIO())


def test_topic_json_has_topic_key_and_matched_posts_only(db_session):
    _seed_topic(db_session)
    out = io.StringIO()
    n_posts, n_comments = export_topic(db_session, "ubi", "json", out)
    assert (n_posts, n_comments) == (2, 1)
    data = json.loads(out.getvalue())
    assert data["topic"] == "UBI"  # canonical name, case-insensitive lookup
    # highest score first (p1=20 before p2=5); q1 is excluded
    assert [p["post_id"] for p in data["posts"]] == ["p1", "p2"]
    assert [c["comment_id"] for c in data["comments"]] == ["c1"]


def test_topic_csv_tags_kind(db_session):
    _seed_topic(db_session)
    out = io.StringIO()
    export_topic(db_session, "UBI", "csv", out)
    rows = list(csv.DictReader(out.getvalue().splitlines()))
    assert [r["kind"] for r in rows] == ["post", "post", "comment"]

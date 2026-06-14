"""Core behavior of topic tracking and the topic page.

Kept deliberately small — the essential, stable contract:
  - track creates a topic (surrogate id + keyword list), dedupes posts,
    and tags them in topicpost by topic_id;
  - re-tracking is incremental when nothing widened the result set, and
    re-pulls the full window when the net grows or the window extends;
  - the page renders from a tracked topic and refuses an unknown one.
Arctic is stubbed; one integration test (network, weekly) is marked.
"""
import time

import pytest
from sqlmodel import Session, select

from redlens import arctic
from redlens.cli import main
from redlens.db import connect, init_schema
from redlens.errors import NotFound
from redlens.models import TopicPost
from redlens.reporting.page import render_topic_page
from redlens.topics import get_topic, track_topic

NOW = int(time.time())


def raw(pid, sub, *, ts=None, score=10, num_comments=2, title="about a topic"):
    return {"id": pid, "subreddit": sub, "author": "alice",
            "created_utc": ts or NOW - 3600, "title": title,
            "score": score, "num_comments": num_comments}


def fake_query(data, calls=None):
    def it(subreddit, query, after=None, before=None):
        if calls is not None:
            calls.append(after)
        yield from data.get(subreddit, [])
    return it


@pytest.fixture
def engine(tmp_path):
    e = connect(tmp_path / "t.db")
    init_schema(e)
    return e


def test_track_creates_topic_dedupes_and_tags(engine, monkeypatch):
    data = {"dualipa": [raw("p1", "dualipa"), raw("p2", "dualipa")],
            "dua_lipa": [raw("p1", "dua_lipa")],          # dup id across subs
            "DuaLipa": []}
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_query(data))

    res = track_topic(engine, "dua lipa", subreddits=["dualipa", "dua_lipa", "DuaLipa"])

    assert res.posts_new == 2                            # p1 deduped
    with Session(engine) as s:
        topic = get_topic(s, "DUA LIPA")                 # case-insensitive
        assert topic is not None and topic.id is not None
        assert topic.keyword_list == ["dua lipa"]
        tps = s.exec(select(TopicPost)).all()
        assert {t.post_id for t in tps} == {"p1", "p2"}
        assert {t.topic_id for t in tps} == {topic.id}


def test_retrack_incremental_vs_full_window(engine, monkeypatch):
    data = {"a": [raw("p1", "a", ts=NOW - 5000)]}
    calls: list = []
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_query(data, calls))
    track_topic(engine, "x", subreddits=["a"])           # fresh: full window

    calls.clear()
    track_topic(engine, "x")                             # unchanged: incremental
    assert calls and all(c == NOW - 5000 for c in calls)

    calls.clear()
    track_topic(engine, "x", subreddits=["b"])           # net grew: full window
    assert calls and all(c < NOW - 5000 for c in calls)


def test_page_renders_and_requires_tracking(engine, monkeypatch):
    with pytest.raises(NotFound):
        render_topic_page(engine, "missing")

    data = {"dualipa": [raw("p1", "dualipa", score=500, title="Wedding <eek>")]}
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_query(data))
    track_topic(engine, "dua lipa", subreddits=["dualipa"])

    doc = render_topic_page(engine, "dua lipa")
    assert "<!doctype html>" in doc
    assert "Wedding &lt;eek&gt;" in doc                  # escaped
    assert "https://reddit.com/comments/p1" in doc
    assert "r/dualipa" in doc
    assert doc == render_topic_page(engine, "dua lipa")  # byte-deterministic


def test_cli_track_then_page(engine, tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(arctic, "iter_subreddit_query",
                        fake_query({"dualipa": [raw("p1", "dualipa")]}))
    monkeypatch.chdir(tmp_path)
    assert main(["--db", str(db), "track", "dua lipa",
                 "--subreddits", "dualipa", "--yes"]) == 0
    assert main(["--db", str(db), "page", "dua lipa"]) == 0
    assert (tmp_path / "dua-lipa.html").exists()


@pytest.mark.integration
def test_track_against_real_arctic(tmp_path, monkeypatch):
    """Weekly canary: does arctic's scoped full-text search still answer the
    way track expects? Capped tiny so it costs a couple of requests."""
    monkeypatch.setattr(arctic, "MAX_ITEMS_PER_STREAM", 5)
    engine = connect(tmp_path / "live.db")
    init_schema(engine)
    res = track_topic(engine, "ozempic", subreddits=["Ozempic"], days=365)
    assert not res.failed and res.posts_new > 0
    assert "r/Ozempic" in render_topic_page(engine, "ozempic")

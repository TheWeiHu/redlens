"""Read-through caching of a topic's LLM renders (task 0032).

`summarize_topic` and `daily_topic_sentiment` are LLM-scored and used to recompute
from scratch on every render — re-paying the model for identical data. These tests
drive the real functions against a seeded SQLite DB with the one network call
(`llm.complete`) stubbed by a *counter*, so they assert the property that matters:
a re-render of an unchanged topic does NOT call the model, while a change to the
matched set (or an untrack) invalidates the cache and a recompute happens.
"""
import json

import pytest
from sqlmodel import Session, select

from redlens import llm
from redlens.db import connect, init_schema, upsert
from redlens.models import Post, Topic, TopicCache, TopicPost
from redlens.summarize import daily_topic_sentiment, summarize_topic
from redlens.topics import untrack_topic


@pytest.fixture
def db(tmp_path, monkeypatch):
    # Isolate config + keys so the real environment never leaks in.
    monkeypatch.setenv("REDLENS_CONFIG", str(tmp_path / "none.toml"))
    for var in ("REDLENS_LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "t.db"
    engine = connect(str(path))
    init_schema(engine)
    return path


def _seed(db, name="vpn", *, post_ids=("a", "b")):
    with Session(connect(str(db))) as s:
        topic = Topic(name=name, keywords=json.dumps([name]),
                      subreddits=json.dumps([name]), last_tracked_at=1_700_000_000)
        s.add(topic)
        s.flush()
        posts = [Post(post_id=p, author_username="x", subreddit_name=name,
                      created_utc=1_700_000_000, title=f"about {p}", score=5)
                 for p in post_ids]
        upsert(s, posts)
        upsert(s, [TopicPost(topic_id=topic.id, post_id=p.post_id) for p in posts])
        s.commit()


_SUMMARY_JSON = json.dumps({
    "overview": "o", "themes": [], "sentiment": "s", "viewpoints": "v"})
_SENTIMENT_JSON = json.dumps({"days": [{"day": "2023-11-14", "score": 20}]})


def _counter(monkeypatch, payload):
    calls = {"n": 0}

    def fake_complete(prompt, key, **kwargs):
        calls["n"] += 1
        return payload

    monkeypatch.setattr(llm, "complete", fake_complete)
    return calls


def test_summary_is_cached_after_first_render(db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed(db)
    calls = _counter(monkeypatch, _SUMMARY_JSON)

    with Session(connect(str(db))) as s:
        first = summarize_topic(s, "vpn")
    with Session(connect(str(db))) as s:
        second = summarize_topic(s, "vpn")

    assert calls["n"] == 1                       # second render hit the cache
    assert first.model_dump() == second.model_dump()


def test_sentiment_is_cached_after_first_render(db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed(db)
    calls = _counter(monkeypatch, _SENTIMENT_JSON)

    with Session(connect(str(db))) as s:
        first = daily_topic_sentiment(s, "vpn")
    with Session(connect(str(db))) as s:
        second = daily_topic_sentiment(s, "vpn")

    assert calls["n"] == 1
    assert [d.day for d in first] == [d.day for d in second]
    assert [d.mean for d in first] == [d.mean for d in second]


def test_cached_render_needs_no_llm_key(db, monkeypatch):
    """The whole point: a deterministic re-render of unchanged data must not
    touch the LLM — so it works even with the key removed afterward."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed(db)
    _counter(monkeypatch, _SUMMARY_JSON)
    with Session(connect(str(db))) as s:
        summarize_topic(s, "vpn")

    # Drop the key; a cached read must still succeed (no MissingKey).
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with Session(connect(str(db))) as s:
        again = summarize_topic(s, "vpn")
    assert again.overview == "o"


def test_new_matched_post_invalidates_the_cache(db, monkeypatch):
    """A `track` that adds a relevant post changes the data-version, so the
    next render recomputes instead of serving the stale summary."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed(db)
    calls = _counter(monkeypatch, _SUMMARY_JSON)

    with Session(connect(str(db))) as s:
        summarize_topic(s, "vpn")
    assert calls["n"] == 1

    # Simulate a track fetching a new matched post.
    with Session(connect(str(db))) as s:
        topic = s.exec(select(Topic).where(Topic.name == "vpn")).first()
        upsert(s, [Post(post_id="c", author_username="x", subreddit_name="vpn",
                        created_utc=1_700_000_001, title="about c", score=1)])
        upsert(s, [TopicPost(topic_id=topic.id, post_id="c")])
        s.commit()

    with Session(connect(str(db))) as s:
        summarize_topic(s, "vpn")
    assert calls["n"] == 2                       # data changed -> recomputed


def test_untrack_invalidates_cached_rows(db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed(db)
    _counter(monkeypatch, _SUMMARY_JSON)
    engine = connect(str(db))
    with Session(engine) as s:
        summarize_topic(s, "vpn")
        assert s.exec(select(TopicCache)).all()

    untrack_topic(engine, "vpn")

    with Session(connect(str(db))) as s:
        assert s.exec(select(TopicCache)).all() == []

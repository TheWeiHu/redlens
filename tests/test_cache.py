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
from redlens.summarize import (
    daily_topic_sentiment,
    identify_brands,
    label_themes,
    summarize_topic,
)
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


def test_keyword_change_invalidates_the_cache(db, monkeypatch):
    """Re-tracking with different keywords recomputes even when the matched id
    set is unchanged — keywords feed the summary prompt, so they're in the
    data-version."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed(db)
    calls = _counter(monkeypatch, _SUMMARY_JSON)

    with Session(connect(str(db))) as s:
        summarize_topic(s, "vpn")
    assert calls["n"] == 1

    # Same posts, different keywords (e.g. a re-track that broadened the query).
    with Session(connect(str(db))) as s:
        topic = s.exec(select(Topic).where(Topic.name == "vpn")).first()
        topic.keywords = json.dumps(["vpn", "wireguard"])
        s.add(topic)
        s.commit()

    with Session(connect(str(db))) as s:
        summarize_topic(s, "vpn")
    assert calls["n"] == 2                       # keywords changed -> recomputed


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


# --- task 0033: the recognizer (brands) and theme labels are cached too, so an
# unchanged --summary page re-renders with zero LLM calls. ---

_BRANDS_JSON = json.dumps({"brands": [
    {"name": "Tesla", "aliases": ["tesla", "tsla"]},
    {"name": "BYD", "aliases": []},
]})
_THEMES_JSON = json.dumps({"labels": ["Connection Problems", "Pricing"]})
_THEMES = [["server", "connection", "slow"], ["price", "deal", "refund"]]


def test_brands_are_cached_after_first_render(db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed(db)
    calls = _counter(monkeypatch, _BRANDS_JSON)

    with Session(connect(str(db))) as s:
        first = identify_brands(s, "vpn")
    with Session(connect(str(db))) as s:
        second = identify_brands(s, "vpn")

    assert calls["n"] == 1                       # recognizer ran once; 2nd was cached
    assert [b.name for b in first] == [b.name for b in second] == ["Tesla", "BYD"]
    assert [b.aliases for b in first] == [b.aliases for b in second]


def test_cached_brands_need_no_llm_key(db, monkeypatch):
    """Persisting the recognizer also pins the entity SET across renders (the
    bonus): a keyless re-render serves the identical, stable set."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed(db)
    _counter(monkeypatch, _BRANDS_JSON)
    with Session(connect(str(db))) as s:
        identify_brands(s, "vpn")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with Session(connect(str(db))) as s:
        again = identify_brands(s, "vpn")
    assert [b.name for b in again] == ["Tesla", "BYD"]


def test_new_matched_post_invalidates_brands(db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed(db)
    calls = _counter(monkeypatch, _BRANDS_JSON)

    with Session(connect(str(db))) as s:
        identify_brands(s, "vpn")
    assert calls["n"] == 1

    with Session(connect(str(db))) as s:
        topic = s.exec(select(Topic).where(Topic.name == "vpn")).first()
        upsert(s, [Post(post_id="c", author_username="x", subreddit_name="vpn",
                        created_utc=1_700_000_001, title="about c", score=1)])
        upsert(s, [TopicPost(topic_id=topic.id, post_id="c")])
        s.commit()

    with Session(connect(str(db))) as s:
        identify_brands(s, "vpn")
    assert calls["n"] == 2                       # data changed -> re-recognized


def test_theme_labels_are_cached_when_given_a_session(db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed(db)
    calls = _counter(monkeypatch, _THEMES_JSON)

    with Session(connect(str(db))) as s:
        first = label_themes("vpn", _THEMES, session=s)
    with Session(connect(str(db))) as s:
        second = label_themes("vpn", _THEMES, session=s)

    assert calls["n"] == 1                       # labeled once; 2nd hit the cache
    assert first == second == ["Connection Problems", "Pricing"]


def test_theme_labels_keyless_re_render(db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed(db)
    _counter(monkeypatch, _THEMES_JSON)
    with Session(connect(str(db))) as s:
        label_themes("vpn", _THEMES, session=s)

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with Session(connect(str(db))) as s:
        again = label_themes("vpn", _THEMES, session=s)
    assert again == ["Connection Problems", "Pricing"]


def test_theme_labels_without_session_always_recompute(db, monkeypatch):
    """The keyless callers (unit path) pass no session and never cache."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _seed(db)
    calls = _counter(monkeypatch, _THEMES_JSON)

    label_themes("vpn", _THEMES)
    label_themes("vpn", _THEMES)
    assert calls["n"] == 2                       # no session -> no caching

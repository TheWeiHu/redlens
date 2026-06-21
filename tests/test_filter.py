"""The LLM relevance filter: it classifies a topic's matched posts, persists a
soft verdict on the topicpost rows, and downstream surfaces hide only the posts
judged off-topic. ``llm.complete`` is mocked with canned verdicts (like
test_llm.py) — no network.
"""
from __future__ import annotations

import json
import time

import pytest
from sqlmodel import Session, select

from redlens import arctic, llm
from redlens.db import connect, init_schema, upsert
from redlens.filter import filter_topic
from redlens.models import Post, Topic, TopicPost
from redlens.topics import get_topic, topic_posts, track_topic

NOW = int(time.time())


@pytest.fixture
def engine(tmp_path):
    e = connect(tmp_path / "t.db")
    init_schema(e)
    return e


def _post(pid, sub="conductor", title="t", body=""):
    return Post(post_id=pid, author_username="bob", subreddit_name=sub,
                created_utc=NOW, title=title, selftext=body, score=5,
                num_comments=0)


def _seed(engine, topic_name, posts):
    """Create a topic and tag ``posts`` to it (unscored), as track would."""
    with Session(engine) as s:
        topic = Topic(name=topic_name, keywords=json.dumps([topic_name]))
        s.add(topic)
        s.flush()
        tid = topic.id
        upsert(s, posts)
        upsert(s, [TopicPost(topic_id=tid, post_id=p.post_id) for p in posts])
        s.commit()
    return tid


def _verdicts(mapping):
    """A fake llm.complete returning a verdict per id in ``mapping`` {id: bool}."""
    def fake(prompt, key, **kwargs):
        return json.dumps({"verdicts": [
            {"id": pid, "relevant": rel, "confidence": 0.9, "reason": "x"}
            for pid, rel in mapping.items()
        ]})
    return fake


def test_filter_persists_verdicts_and_hides_false_positives(engine, monkeypatch):
    posts = [_post("p1", title="Conductor the Mac app is great"),
             _post("p2", title="the train conductor checked tickets"),
             _post("p3", title="loving conductor for parallel agents")]
    _seed(engine, "conductor", posts)
    monkeypatch.setattr(llm, "complete",
                        _verdicts({"p1": True, "p2": False, "p3": True}))

    with Session(engine) as s:
        topic = get_topic(s, "conductor")
        res = filter_topic(s, topic, ["p1", "p2", "p3"], "sk-test")
        assert (res.scored, res.relevant, res.filtered) == (3, 2, 0 + 1)
        # Verdict persisted on the join rows.
        rows = {r.post_id: r for r in s.exec(select(TopicPost)).all()}
        assert rows["p2"].relevant is False
        assert rows["p1"].relevant is True
        assert rows["p2"].relevance_model == "gpt-4o-mini"
        # Downstream read hides the false positive, keeps the rest.
        kept = {p.post_id for p in topic_posts(s, "conductor")}
        assert kept == {"p1", "p3"}


def test_keep_when_unsure_for_omitted_ids(engine, monkeypatch):
    # The model only returns a verdict for p1; p2 is omitted -> kept (recall bias),
    # but still recorded so we don't re-pay the LLM next time.
    posts = [_post("p1"), _post("p2")]
    _seed(engine, "shell", posts)
    monkeypatch.setattr(llm, "complete", _verdicts({"p1": True}))

    with Session(engine) as s:
        topic = get_topic(s, "shell")
        res = filter_topic(s, topic, ["p1", "p2"], "sk-test")
        assert res.scored == 2 and res.filtered == 0
        rows = {r.post_id: r for r in s.exec(select(TopicPost)).all()}
        assert rows["p2"].relevant is True
        assert rows["p2"].relevance_confidence == 0.0  # the "unsure" sentinel


def test_llm_error_leaves_batch_unscored(engine, monkeypatch):
    from redlens.errors import RedlensError
    posts = [_post("p1"), _post("p2")]
    _seed(engine, "bolt", posts)

    def boom(prompt, key, **kwargs):
        raise RedlensError("LLM request failed")
    monkeypatch.setattr(llm, "complete", boom)

    with Session(engine) as s:
        topic = get_topic(s, "bolt")
        res = filter_topic(s, topic, ["p1", "p2"], "sk-test")
        assert res.scored == 0 and res.errored == 2
        # Nothing marked junk; both rows stay unscored (NULL) and thus kept.
        rows = s.exec(select(TopicPost)).all()
        assert all(r.relevant is None for r in rows)
        assert {p.post_id for p in topic_posts(s, "bolt")} == {"p1", "p2"}


def test_batching_makes_one_call_per_batch(engine, monkeypatch):
    posts = [_post(f"p{i}") for i in range(5)]
    _seed(engine, "arc", posts)
    calls = {"n": 0}

    def fake(prompt, key, **kwargs):
        calls["n"] += 1
        # echo back relevant for whatever ids appear in the prompt
        ids = [ln.split("id=", 1)[1].split(" ", 1)[0]
               for ln in prompt.splitlines() if "id=" in ln]
        return json.dumps({"verdicts": [
            {"id": pid, "relevant": True, "confidence": 0.5, "reason": "x"}
            for pid in ids]})
    monkeypatch.setattr(llm, "complete", fake)

    with Session(engine) as s:
        topic = get_topic(s, "arc")
        res = filter_topic(s, topic, [p.post_id for p in posts], "sk-test", batch=2)
    assert calls["n"] == 3          # 2 + 2 + 1
    assert res.scored == 5


# --- track integration: filter runs only with a key ------------------------

def _fake_query(data):
    def it(subreddit, query, after=None, before=None):
        yield from data.get(subreddit, [])
    return it


def _raw(pid, sub, title):
    return {"id": pid, "subreddit": sub, "author": "bob",
            "created_utc": NOW - 100, "title": title, "score": 3,
            "num_comments": 0}


def test_track_filters_when_key_present(engine, monkeypatch):
    data = {"conductor": [_raw("p1", "conductor", "Conductor app rocks"),
                          _raw("p2", "conductor", "orchestra conductor bows")]}
    monkeypatch.setattr(arctic, "iter_subreddit_query", _fake_query(data))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "complete", _verdicts({"p1": True, "p2": False}))

    res = track_topic(engine, "conductor", subreddits=["conductor"])
    assert res.posts_new == 2                       # both fetched + stored…
    assert res.relevance is not None
    assert res.relevance.filtered == 1              # …one flagged off-topic
    with Session(engine) as s:
        assert {p.post_id for p in topic_posts(s, "conductor")} == {"p1"}


def test_track_without_key_does_not_filter(engine, monkeypatch):
    data = {"conductor": [_raw("p1", "conductor", "x"), _raw("p2", "conductor", "y")]}
    monkeypatch.setattr(arctic, "iter_subreddit_query", _fake_query(data))
    # conftest already clears keys; assert no LLM call happens.
    monkeypatch.setattr(llm, "complete",
                        lambda *a, **k: pytest.fail("LLM called without a key"))

    res = track_topic(engine, "conductor", subreddits=["conductor"])
    assert res.relevance is None
    with Session(engine) as s:
        rows = s.exec(select(TopicPost)).all()
        assert all(r.relevant is None for r in rows)         # unscored
        assert {p.post_id for p in topic_posts(s, "conductor")} == {"p1", "p2"}

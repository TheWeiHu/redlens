"""End-to-end behavior of `redlens summarize`, with the network stubbed.

The only logic worth testing here is ours, not the model's, so we stub the one
network call (`llm.complete`) and check the parts redlens actually controls:

  - the no-key path is a clean exit 2 with a setup hint (no LLM involved);
  - the payload we hand the model is built from a *representative* sample of
    the archive — top-voted content across the user's whole history, not just
    their newest rows — and describes the person, not raw counts/karma;
  - the model's JSON is parsed into a structured Profile (and bad JSON fails
    cleanly);
  - an unknown --depth is rejected.

Each test drives the real `summarize_user` / CLI against a seeded SQLite DB,
so it exercises the user lookup, sampling, JSON parsing, and prompt assembly
together rather than mocking them apart.
"""
import json

import pytest
from sqlmodel import Session

from redlens import llm
from redlens.cli import main
from redlens.db import connect, init_schema, upsert
from redlens.errors import RedlensError
from redlens.models import Comment, Post, Topic, TopicPost, User


def _seed(session, user="Alice"):
    upsert(session, [User(username=user)])
    upsert(session, [
        Post(post_id="p1", author_username=user, subreddit_name="python",
             created_utc=1_700_000_000, title="how I learned async", score=12),
    ])
    # One defining, heavily-upvoted comment from long ago, buried under newer
    # low-score ones — the sample must surface the old one, not just the tail.
    upsert(session, [
        Comment(comment_id="old", author_username=user, subreddit_name="python",
                link_id="x", parent_id=None, created_utc=1_000, score=9999,
                body="DEFINING TAKE on language design"),
    ])
    upsert(session, [
        Comment(comment_id=f"r{i}", author_username=user, subreddit_name="python",
                link_id="x", parent_id=None, created_utc=2_000 + i, score=1,
                body=f"filler comment {i}")
        for i in range(40)
    ])
    session.commit()


@pytest.fixture
def db(tmp_path, monkeypatch):
    # Isolate config + keys so the real environment never leaks in.
    monkeypatch.setenv("REDLENS_CONFIG", str(tmp_path / "none.toml"))
    for var in ("REDLENS_LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "t.db"
    engine = connect(str(path))
    init_schema(engine)
    with Session(engine) as s:
        _seed(s)
    return path


def test_no_key_exits_2_with_setup_hint(db, capsys):
    assert main(["--db", str(db), "summarize", "alice"]) == 2
    assert "redlens setup" in capsys.readouterr().err


_STUB_JSON = """```json
{
  "demographics": {
    "gender": [{"label": "Female", "confidence": 55, "reason": "tone"}],
    "country": [{"label": "Canada", "confidence": 60, "reason": "spelling"}]
  },
  "big_five": {"openness": {"score": 88, "reason": "varied interests"}},
  "interests": "python and rust",
  "beliefs": "open source",
  "tone": "friendly"
}
```"""


def test_representative_payload_and_structured_profile(db, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    captured = {}

    def fake_complete(prompt, key, *, max_tokens):
        captured["prompt"] = prompt
        return _STUB_JSON                       # fenced JSON, as a model might return

    monkeypatch.setattr(llm, "complete", fake_complete)

    assert main(["--db", str(db), "summarize", "alice", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    # JSON (even fenced) parsed into the structured Profile; we set the metadata.
    assert out["username"] == "Alice" and out["model"] == "gpt-4o-mini"
    assert out["demographics"]["country"][0] == {
        "label": "Canada", "confidence": 60, "reason": "spelling"}
    assert out["big_five"]["openness"]["score"] == 88

    # The data we feed sits before the instruction block; check that half.
    data = captured["prompt"].split("Infer a profile", 1)[0]
    assert "r/python" in data                           # communities, by name
    assert "how I learned async" in data                # real content sampled
    assert "DEFINING TAKE on language design" in data   # top-voted, not recency
    assert "karma" not in data and "posts," not in data  # no raw stats fed in


def test_bad_json_fails_cleanly(db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "complete", lambda *a, **k: "sorry, I can't do that")
    from redlens.summarize import summarize_user
    with Session(connect(str(db))) as s, pytest.raises(RedlensError):
        summarize_user(s, "alice")


def test_unknown_depth_is_rejected(db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from redlens.summarize import summarize_user
    with Session(connect(str(db))) as s, pytest.raises(RedlensError):
        summarize_user(s, "alice", depth="exhaustive")


def _seed_topic(session, name="climate"):
    """A tracked topic with matched posts (via topicpost) and comments under
    them (via the link_id bridge), including one defining old top-voted comment
    buried under newer filler — the sample must surface it."""
    topic = Topic(name=name, keywords=json.dumps([name]),
                  subreddits=json.dumps(["climate", "science"]),
                  last_tracked_at=1_700_300_000)
    session.add(topic)
    session.flush()
    posts = [
        Post(post_id="t1", author_username="Bob", subreddit_name="climate",
             created_utc=1_700_000_000, title="carbon tax debate heats up",
             score=42),
        Post(post_id="t2", author_username="Cara", subreddit_name="science",
             created_utc=1_700_100_000, title="new solar efficiency record",
             score=8),
    ]
    upsert(session, posts)
    upsert(session, [TopicPost(topic_id=topic.id, post_id=p.post_id)
                     for p in posts])
    upsert(session, [
        Comment(comment_id="old", author_username="Bob", subreddit_name="climate",
                link_id="t1", parent_id=None, created_utc=1_000, score=9999,
                body="DEFINING ARGUMENT about policy tradeoffs"),
    ])
    upsert(session, [
        Comment(comment_id=f"c{i}", author_username="Cara",
                subreddit_name="science", link_id="t1", parent_id=None,
                created_utc=2_000 + i, score=1, body=f"filler reply {i}")
        for i in range(40)
    ])
    session.commit()
    return topic


@pytest.fixture
def topic_db(db):
    with Session(connect(str(db))) as s:
        _seed_topic(s)
    return db


_STUB_TOPIC_JSON = """```json
{
  "overview": "people argue about climate policy",
  "themes": [{"title": "carbon pricing", "summary": "taxes vs cap-and-trade"}],
  "sentiment": "concerned but engaged",
  "viewpoints": "market vs regulation"
}
```"""


def test_topic_no_key_exits_2_with_setup_hint(topic_db, capsys):
    assert main(["--db", str(topic_db), "summarize", "--topic", "climate"]) == 2
    assert "redlens setup" in capsys.readouterr().err


def test_topic_not_tracked_exits_2(db, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert main(["--db", str(db), "summarize", "--topic", "ghost"]) == 2
    assert "not found" in capsys.readouterr().err.lower()


def test_summarize_without_user_or_topic_errors(db, capsys):
    assert main(["--db", str(db), "summarize"]) == 1
    assert "username or --topic" in capsys.readouterr().err


def test_topic_representative_payload_and_structured_summary(
        topic_db, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    captured = {}

    def fake_complete(prompt, key, *, max_tokens):
        captured["prompt"] = prompt
        return _STUB_TOPIC_JSON

    monkeypatch.setattr(llm, "complete", fake_complete)

    assert main(
        ["--db", str(topic_db), "summarize", "--topic", "climate", "--json"]
    ) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["topic"] == "climate" and out["model"] == "gpt-4o-mini"
    assert out["themes"][0]["title"] == "carbon pricing"
    assert out["sentiment"] == "concerned but engaged"

    data = captured["prompt"].split("Summarize what", 1)[0]
    assert "climate" in data                              # the topic + keywords
    assert "r/climate" in data                            # communities, by name
    assert "carbon tax debate heats up" in data          # matched post sampled
    assert "DEFINING ARGUMENT about policy tradeoffs" in data  # top-voted comment
    assert "score" not in data and "posts," not in data   # no raw stats fed in

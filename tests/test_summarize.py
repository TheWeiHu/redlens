"""Profile summaries: payload build, caching, refresh, and the no-key path.

The LLM call is stubbed throughout — no network in the default test run.
"""
import json

import pytest
from sqlmodel import Session

from redlens import llm
from redlens.cli import main
from redlens.db import connect, init_schema, upsert
from redlens.errors import MissingKey, NotFound, RedlensError
from redlens.models import Comment, Post, Summary, User
from redlens.summarize import summarize_user


@pytest.fixture
def db_session(tmp_path, monkeypatch):
    # Isolate config so the real user config/keys never leak into the test.
    monkeypatch.setenv("REDLENS_CONFIG", str(tmp_path / "none.toml"))
    for var in ("REDLENS_LLM_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    engine = connect(":memory:")
    init_schema(engine)
    with Session(engine) as s:
        yield s


def _seed(session, user="Alice"):
    upsert(session, [User(username=user)])
    upsert(session, [
        Post(post_id="p1", author_username=user, subreddit_name="python",
             created_utc=1_700_000_000, title="how I learned async", score=12),
        Post(post_id="p2", author_username=user, subreddit_name="rust",
             created_utc=1_700_000_100, title="borrow checker tips", score=4),
    ])
    upsert(session, [
        Comment(comment_id="c1", author_username=user, subreddit_name="python",
                link_id="x", parent_id=None, created_utc=1_700_000_200,
                body="use asyncio.gather here", score=3),
    ])
    session.commit()


def test_missing_key_raises_missingkey(db_session):
    _seed(db_session)
    with pytest.raises(MissingKey):
        summarize_user(db_session, "alice")


def test_unknown_user_raises_notfound(db_session, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with pytest.raises(NotFound):
        summarize_user(db_session, "ghost")


def test_generates_persists_and_is_case_insensitive(db_session, monkeypatch):
    _seed(db_session, user="Alice")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    seen = {}

    def fake_complete(prompt, key, *, max_tokens):
        seen["prompt"] = prompt
        return "  Alice writes about Python and Rust.  "

    monkeypatch.setattr(llm, "complete", fake_complete)

    summ = summarize_user(db_session, "alice")           # lowercased input
    assert summ.username == "Alice"                       # canonical casing kept
    assert summ.text == "Alice writes about Python and Rust."   # stripped
    assert summ.model == "claude-haiku-4-5"
    # The payload is built from the archive, not the whole archive shipped raw.
    assert "r/python" in seen["prompt"]
    assert "how I learned async" in seen["prompt"]
    # Persisted.
    assert db_session.get(Summary, "Alice").text == summ.text


def test_rerun_reuses_cache_unless_refresh(db_session, monkeypatch):
    _seed(db_session)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    calls = []

    def fake_complete(prompt, key, *, max_tokens):
        calls.append(1)
        return f"summary #{len(calls)}"

    monkeypatch.setattr(llm, "complete", fake_complete)

    first = summarize_user(db_session, "alice")
    again = summarize_user(db_session, "alice")           # cached -> no 2nd call
    assert len(calls) == 1
    assert again.text == first.text == "summary #1"

    refreshed = summarize_user(db_session, "alice", refresh=True)
    assert len(calls) == 2
    assert refreshed.text == "summary #2"
    assert db_session.get(Summary, "Alice").text == "summary #2"  # overwritten


def test_cli_summarize_json_and_no_key(tmp_path, monkeypatch, capsys):
    db = tmp_path / "t.db"
    monkeypatch.setenv("REDLENS_CONFIG", str(tmp_path / "none.toml"))
    for var in ("REDLENS_LLM_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    engine = connect(str(db))
    init_schema(engine)
    with Session(engine) as s:
        _seed(s)

    # No key -> exit 2 with a setup hint.
    assert main(["--db", str(db), "summarize", "alice"]) == 2
    assert "redlens setup" in capsys.readouterr().err

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(llm, "complete",
                        lambda prompt, key, *, max_tokens: "a tidy summary")
    assert main(["--db", str(db), "summarize", "alice", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "username": "Alice", "model": "claude-haiku-4-5", "depth": "standard",
        "text": "a tidy summary", "created_at": out["created_at"],
    }
    assert isinstance(out["created_at"], int)


def test_sample_is_top_voted_not_recency_only(db_session, monkeypatch):
    """A high-score *old* comment beats a wall of recent low-score ones — the
    sample represents the whole history, not just the tail."""
    upsert(db_session, [User(username="alice")])
    # One defining, heavily-upvoted comment from long ago...
    upsert(db_session, [
        Comment(comment_id="old", author_username="alice", subreddit_name="python",
                link_id="x", parent_id=None, created_utc=1_000, score=9999,
                body="DEFINING TAKE on language design"),
    ])
    # ...buried under many recent, low-score ones.
    upsert(db_session, [
        Comment(comment_id=f"r{i}", author_username="alice", subreddit_name="python",
                link_id="x", parent_id=None, created_utc=2_000 + i, score=1,
                body=f"filler comment {i}")
        for i in range(30)
    ])
    db_session.commit()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    seen = {}
    monkeypatch.setattr(llm, "complete",
                        lambda prompt, key, *, max_tokens: seen.update(p=prompt) or "ok")

    # quick depth samples only ~20 comments — recency-only would drop the old one.
    summarize_user(db_session, "alice", depth="quick")
    assert "DEFINING TAKE on language design" in seen["p"]


def test_depth_is_stored_and_changing_it_regenerates(db_session, monkeypatch):
    _seed(db_session)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    calls = []
    monkeypatch.setattr(llm, "complete",
                        lambda prompt, key, *, max_tokens: calls.append(1) or f"s{len(calls)}")

    first = summarize_user(db_session, "alice", depth="quick")
    assert first.depth == "quick"
    # Same depth (and bare re-run) hit the cache; a different depth regenerates.
    summarize_user(db_session, "alice", depth="quick")
    summarize_user(db_session, "alice")                       # no depth -> cached
    assert len(calls) == 1
    deep = summarize_user(db_session, "alice", depth="deep")
    assert len(calls) == 2
    assert deep.depth == "deep"
    assert db_session.get(Summary, "Alice").depth == "deep"   # overwritten


def test_refresh_keeps_prior_depth(db_session, monkeypatch):
    _seed(db_session)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setattr(llm, "complete",
                        lambda prompt, key, *, max_tokens: "ok")
    summarize_user(db_session, "alice", depth="deep")
    refreshed = summarize_user(db_session, "alice", refresh=True)  # no depth given
    assert refreshed.depth == "deep"


def test_unknown_depth_raises(db_session):
    _seed(db_session)
    with pytest.raises(RedlensError):
        summarize_user(db_session, "alice", depth="exhaustive")

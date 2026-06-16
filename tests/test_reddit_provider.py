import json

import pytest
from sqlmodel import Session

from redlens import config, ingest
from redlens.db import connect, init_schema
from redlens.errors import RedlensError
from redlens.models import Comment, Post
from redlens.providers import reddit

# Listing-shaped fixtures (Reddit's official API), with noise fields that must
# be ignored. ``created_utc`` is a float, as Reddit returns it.
REDDIT_POST = {
    "id": "abc123", "author": "alice", "subreddit": "python",
    "created_utc": 1781000000.0, "title": "Fresh post", "selftext": "",
    "url": "https://www.reddit.com/r/python/abc123", "score": 12,
    "num_comments": 3, "over_18": False, "stickied": True,  # stickied = noise
}
REDDIT_COMMENT = {
    "id": "cmt1", "author": "alice", "subreddit": "python",
    "link_id": "t3_abc123", "parent_id": "t1_xyz", "created_utc": 1781000500.0,
    "body": "nice", "score": 5, "controversiality": 0,  # noise
}


def _listing(children: list[dict], after: str | None = None) -> dict:
    return {"data": {"after": after,
                     "children": [{"kind": "t3", "data": c} for c in children]}}


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._b = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._b

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *a: object) -> bool:
        return False


# --- field mapping ----------------------------------------------------------

def test_post_from_reddit_handles_float_ts_and_empty_selftext():
    p = Post.from_reddit(REDDIT_POST)
    assert p.post_id == "abc123"
    assert p.author_username == "alice"
    assert p.subreddit_name == "python"
    assert p.created_utc == 1781000000   # float coerced to int
    assert p.selftext is None            # "" normalized to None
    assert p.score == 12
    assert p.num_comments == 3


def test_comment_from_reddit_strips_link_prefix_keeps_parent():
    c = Comment.from_reddit(REDDIT_COMMENT)
    assert c.comment_id == "cmt1"
    assert c.link_id == "abc123"         # t3_ stripped
    assert c.parent_id == "t1_xyz"       # parent prefix preserved
    assert c.created_utc == 1781000500
    assert c.score == 5


# --- auth -------------------------------------------------------------------

def test_get_token_uses_basic_auth_and_ua(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["auth"] = req.headers.get("Authorization")
        captured["ua"] = req.headers.get("User-agent")  # urllib capitalizes
        captured["body"] = req.data
        return _FakeResp({"access_token": "tok123", "expires_in": 3600})

    monkeypatch.setattr(reddit.urllib.request, "urlopen", fake_urlopen)
    assert reddit.get_token("cid", "secret") == "tok123"
    assert captured["auth"].startswith("Basic ")
    assert b"grant_type=client_credentials" in captured["body"]
    assert "redlens/" in captured["ua"] and "github.com" in captured["ua"]


def test_get_token_without_access_token_raises(monkeypatch):
    monkeypatch.setattr(reddit.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp({"error": "unauthorized"}))
    with pytest.raises(RedlensError):
        reddit.get_token("cid", "secret")


# --- pagination -------------------------------------------------------------

def test_iter_listing_walks_after_cursor_until_exhausted(monkeypatch):
    pages = [
        _listing([REDDIT_POST], after="t3_abc123"),
        _listing([{**REDDIT_POST, "id": "def456"}], after=None),
    ]
    calls = []

    def fake_request(req, *, what):
        calls.append(req.full_url)
        return pages.pop(0)

    monkeypatch.setattr(reddit, "_request", fake_request)
    out = list(reddit.iter_submitted("tok", "alice"))
    assert [o["id"] for o in out] == ["abc123", "def456"]
    assert "/user/alice/submitted" in calls[0]
    assert "after=t3_abc123" in calls[1]   # second page followed the cursor


# --- ingest top-up ----------------------------------------------------------

def test_topup_is_noop_without_credentials(monkeypatch):
    monkeypatch.setattr(config, "reddit_credentials", lambda: None)
    engine = connect(":memory:")
    init_schema(engine)
    with Session(engine) as s:
        assert ingest._reddit_topup(s, "alice") == (0, 0)


def test_topup_upserts_fresh_items_when_credentials_present(monkeypatch):
    monkeypatch.setattr(config, "reddit_credentials", lambda: ("cid", "sec"))
    monkeypatch.setattr(reddit, "get_token", lambda *a: "tok")
    monkeypatch.setattr(reddit, "iter_submitted",
                        lambda t, u: iter([REDDIT_POST]))
    monkeypatch.setattr(reddit, "iter_comments",
                        lambda t, u: iter([REDDIT_COMMENT]))
    engine = connect(":memory:")
    init_schema(engine)
    with Session(engine) as s:
        assert ingest._reddit_topup(s, "alice") == (1, 1)
        s.commit()
        # Re-running is idempotent at the DB level (upsert on the same PKs).
        ingest._reddit_topup(s, "alice")
        s.commit()
    with Session(engine) as s:
        assert s.get(Post, "abc123") is not None
        assert s.get(Comment, "cmt1") is not None

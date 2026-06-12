import time

import pytest
from sqlmodel import Session, select

from redlens import arctic
from redlens.cli import _pick_subreddits, main
from redlens.db import connect, init_schema
from redlens.errors import NotFound
from redlens.models import Post, TopicPost
from redlens.page import render_topic_page
from redlens.topics import (
    SubredditCandidate,
    get_topic,
    guess_home_subreddits,
    search_subreddits,
    track_topic,
)

NOW = int(time.time())


def raw(pid, sub, *, ts=None, author="alice", score=10, title="about dua lipa"):
    return {
        "id": pid, "subreddit": sub, "author": author,
        "created_utc": ts or NOW - 3600, "title": title,
        "score": score, "num_comments": 2,
    }


def fake_subreddit_query(data, calls=None):
    """A stand-in for arctic.iter_subreddit_query serving canned posts."""
    def it(subreddit, query, after=None, before=None):
        if calls is not None:
            calls.append({"subreddit": subreddit, "query": query, "after": after})
        yield from data.get(subreddit, [])
    return it


@pytest.fixture
def engine(tmp_path):
    e = connect(tmp_path / "topics.db")
    init_schema(e)
    return e


def test_guess_home_subreddits():
    assert guess_home_subreddits("Dua Lipa") == ["DuaLipa", "dua_lipa", "dualipa"]
    assert guess_home_subreddits("") == []


def test_track_creates_topic_and_dedupes(engine, monkeypatch):
    data = {
        "dualipa": [raw("p1", "dualipa"), raw("p2", "dualipa")],
        "dua_lipa": [raw("p1", "dua_lipa")],          # duplicate id across subs
        "DuaLipa": [],
    }
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_subreddit_query(data))

    res = track_topic(engine, "dua lipa")

    assert res.posts_new == 2                          # p1 deduped
    assert res.subreddits_searched == 3                # the guessed home subs
    with Session(engine) as s:
        topic = get_topic(s, "DUA LIPA")               # lookup is case-insensitive
        assert topic is not None
        assert topic.query == "dua lipa"
        assert set(topic.subreddit_list) == {"DuaLipa", "dua_lipa", "dualipa"}
        assert topic.newest_seen_utc == NOW - 3600
        assert {t.post_id for t in s.exec(select(TopicPost))} == {"p1", "p2"}
        assert s.exec(select(Post)).all()              # posts in the shared table


def test_retrack_is_incremental_when_net_unchanged(engine, monkeypatch):
    data = {"dualipa": [raw("p1", "dualipa", ts=NOW - 5000)]}
    calls: list[dict] = []
    monkeypatch.setattr(
        arctic, "iter_subreddit_query", fake_subreddit_query(data, calls)
    )
    track_topic(engine, "x", subreddits=["dualipa"])
    calls.clear()

    track_topic(engine, "x")                           # same net: cursor applies
    assert all(c["after"] == NOW - 5000 for c in calls)

    calls.clear()
    track_topic(engine, "x", subreddits=["popheads"])  # net grew: full window
    assert all(c["after"] < NOW - 5000 for c in calls)


def test_discover_widens_the_net(engine, monkeypatch):
    seeds = {"dualipa": [raw("p1", "dualipa", author="superfan")]}
    elsewhere = {
        "superfan": [raw("p2", "popheads", author="superfan"),
                     raw("p3", "Fauxmoi", author="superfan"),
                     raw("p1", "dualipa", author="superfan"),    # already known
                     raw("p4", "u_superfan", author="superfan")],  # profile page
    }
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_subreddit_query(
        {**seeds, "popheads": [raw("p2", "popheads")], "Fauxmoi": [raw("p3", "Fauxmoi")],
         "dua_lipa": [], "DuaLipa": []}
    ))
    monkeypatch.setattr(
        arctic, "iter_author_query",
        lambda author, query, after=None, before=None: iter(elsewhere.get(author, [])),
    )

    res = track_topic(engine, "dua lipa", discover=True)
    assert set(res.discovered) == {"popheads", "Fauxmoi"}
    assert res.posts_new == 3
    with Session(engine) as s:
        topic = get_topic(s, "dua lipa")
        assert {"popheads", "Fauxmoi"} <= set(topic.subreddit_list)


def test_search_subreddits_dedupes_and_ranks(monkeypatch):
    def fake_search(prefix, limit=25):
        return {
            "dualipa": [
                {"display_name": "dualipa", "subscribers": 614_652,
                 "public_description": "Everything Dua Lipa", "over18": False},
                {"display_name": "DuaLipaGW", "subscribers": 92_482, "over18": True},
            ],
            "dua_lipa": [
                {"display_name": "DUALIPA", "subscribers": 614_652},  # dupe, case
                {"display_name": "dua_lipa", "subscribers": 50},
            ],
        }.get(prefix, [])

    monkeypatch.setattr(arctic, "search_subreddits", fake_search)
    found = search_subreddits("Dua Lipa")
    assert [c.name for c in found] == ["dualipa", "DuaLipaGW", "dua_lipa"]
    assert found[0].subscribers == 614_652
    assert found[1].over_18 is True


def _candidates(*names):
    return [SubredditCandidate(name=n, subscribers=10, description="", over_18=False)
            for n in names]


def test_picker_passthrough_when_not_interactive(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert _pick_subreddits(_candidates("a", "b"), assume_yes=False) == ["a", "b"]
    assert _pick_subreddits(_candidates("a"), assume_yes=True) == ["a"]


def test_picker_drop_and_add(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    lines = iter(["-2 +r/popheads", ""])
    monkeypatch.setattr("builtins.input", lambda: next(lines))
    picked = _pick_subreddits(_candidates("dualipa", "DuaLipaGW", "dua_lipa"),
                              assume_yes=False)
    assert picked == ["dualipa", "dua_lipa", "popheads"]


def test_one_bad_subreddit_does_not_sink_the_net(engine, monkeypatch):
    from redlens.errors import RedlensError

    def it(subreddit, query, after=None, before=None):
        if subreddit == "deadsub":
            raise RedlensError("arctic GET ...: HTTP Error 422")
        yield raw("p1", subreddit)

    monkeypatch.setattr(arctic, "iter_subreddit_query", it)
    res = track_topic(engine, "x", subreddits=["deadsub", "livesub"])
    assert res.posts_new == 1
    assert "deadsub" in res.failed
    assert res.per_subreddit.get("livesub") == 1


def test_page_renders_and_requires_tracking(engine, monkeypatch):
    with pytest.raises(NotFound):
        render_topic_page(engine, "nope")

    data = {"dualipa": [raw("p1", "dualipa", score=500, title="Wedding <megathread>"),
                        raw("p2", "dualipa", score=7)]}
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_subreddit_query(data))
    track_topic(engine, "dua lipa", subreddits=["dualipa"])

    doc = render_topic_page(engine, "dua lipa")
    assert "<!doctype html>" in doc
    assert "dua lipa" in doc
    assert "Wedding &lt;megathread&gt;" in doc         # escaped
    assert "https://reddit.com/comments/p1" in doc
    assert "r/dualipa" in doc
    assert "1 of 1" in doc                             # matches vs net searched
    assert "Posts per day" in doc and "Score per day" in doc
    assert '<svg class="chart"' in doc
    assert "peak: 507 points" in doc                   # 500 + 7, same day


@pytest.mark.integration
def test_track_against_real_arctic(tmp_path, monkeypatch):
    """Weekly canary: does arctic's scoped full-text search still answer the
    way track expects? Capped tiny so it costs a couple of requests."""
    monkeypatch.setattr(arctic, "MAX_ITEMS_PER_STREAM", 5)
    engine = connect(tmp_path / "live.db")
    init_schema(engine)
    res = track_topic(engine, "dua lipa", subreddits=["dualipa"], days=365)
    assert not res.failed
    assert res.posts_new > 0
    doc = render_topic_page(engine, "dua lipa")
    assert "r/dualipa" in doc


def test_cli_track_then_page(engine, tmp_path, monkeypatch):
    db = tmp_path / "topics.db"                        # same file as the fixture
    data = {"dualipa": [raw("p1", "dualipa")], "dua_lipa": [], "DuaLipa": []}
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_subreddit_query(data))
    searches: list[str] = []
    monkeypatch.setattr(
        arctic, "search_subreddits",
        lambda prefix, limit=25: searches.append(prefix) or [],
    )
    monkeypatch.chdir(tmp_path)

    assert main(["--db", str(db), "track", "dua lipa"]) == 0
    assert searches                                    # first run searches

    searches.clear()
    assert main(["--db", str(db), "track", "dua lipa"]) == 0
    assert not searches                                # re-track: stored net, no search

    assert main(["--db", str(db), "page", "dua lipa"]) == 0
    out = tmp_path / "dua-lipa.html"
    assert out.exists()
    assert "redlens" in out.read_text(encoding="utf-8")

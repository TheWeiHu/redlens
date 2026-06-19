"""Core behavior of topic tracking and the topic page.

Kept deliberately small — the essential, stable contract:
  - track creates a topic (surrogate id + keyword list), dedupes posts,
    and tags them in topicpost by topic_id;
  - re-tracking is incremental when nothing widened the result set, and
    re-pulls the full window when the net grows or the window extends;
  - the page renders from a tracked topic and refuses an unknown one.
Arctic is stubbed; one integration test (network, weekly) is marked.
"""
import json
import time

import pytest
from sqlmodel import Session, select

from redlens import arctic
from redlens.cli import main
from redlens.db import connect, init_schema
from redlens.errors import NotFound, RedlensError
from redlens.models import Comment, Post, TopicPost, User
from redlens.reporting.page import render_topic_page
from redlens.topics import get_topic, list_topics, track_topic, untrack_topic

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


def test_cli_page_open_launches_browser(engine, tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(arctic, "iter_subreddit_query",
                        fake_query({"dualipa": [raw("p1", "dualipa")]}))
    monkeypatch.chdir(tmp_path)
    assert main(["--db", str(db), "track", "dua lipa",
                 "--subreddits", "dualipa", "--yes"]) == 0

    opened: list[str] = []
    monkeypatch.setattr("redlens.cli.webbrowser.open", opened.append)

    # default: file written, no browser launched.
    assert main(["--db", str(db), "page", "dua lipa"]) == 0
    assert opened == []

    # --open launches the browser pointed at the written file's URI.
    assert main(["--db", str(db), "page", "dua lipa", "--open"]) == 0
    assert opened == [(tmp_path / "dua-lipa.html").resolve().as_uri()]

    # --no-browser suppresses the launch even with --open (scripts/CI).
    assert main(["--db", str(db), "page", "dua lipa", "--open",
                 "--no-browser"]) == 0
    assert len(opened) == 1


def test_cli_page_all_renders_index_and_skips_empty(engine, tmp_path,
                                                    monkeypatch):
    db = tmp_path / "t.db"
    # dua lipa matches a post; ozempic's net yields nothing → skipped.
    monkeypatch.setattr(arctic, "iter_subreddit_query",
                        fake_query({"dualipa": [raw("p1", "dualipa")]}))
    assert main(["--db", str(db), "track", "dua lipa",
                 "--subreddits", "dualipa", "--yes"]) == 0
    assert main(["--db", str(db), "track", "ozempic",
                 "--subreddits", "Ozempic", "--yes"]) == 0

    out = tmp_path / "reports"
    assert main(["--db", str(db), "page", "--all", "-o", str(out)]) == 0

    assert (out / "dua-lipa.html").exists()
    assert not (out / "ozempic.html").exists()        # zero matches: skipped
    index = (out / "index.html").read_text()
    assert "dua-lipa.html" in index                   # links the rendered page
    assert "ozempic" in index                          # but noted as skipped


def test_cli_page_needs_topic_or_all(engine, tmp_path):
    db = tmp_path / "t.db"
    assert main(["--db", str(db), "page"]) == 1        # neither topic nor --all


def test_cli_page_all_decollides_slugs(engine, tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    # "C++" and "C#" both slug() to "c" — render_all must not overwrite one
    # topic's page (and index link) with the other's.
    monkeypatch.setattr(arctic, "iter_subreddit_query",
                        fake_query({"cpp": [raw("p1", "cpp")],
                                    "csharp": [raw("p2", "csharp")]}))
    assert main(["--db", str(db), "track", "C++",
                 "--subreddits", "cpp", "--yes"]) == 0
    assert main(["--db", str(db), "track", "C#",
                 "--subreddits", "csharp", "--yes"]) == 0

    out = tmp_path / "reports"
    assert main(["--db", str(db), "page", "--all", "-o", str(out)]) == 0

    pages = sorted(p.name for p in out.glob("*.html") if p.name != "index.html")
    assert pages == ["c-2.html", "c.html"]             # distinct files, no clobber
    index = (out / "index.html").read_text()
    assert "c.html" in index and "c-2.html" in index   # both topics linked


def test_track_does_not_advance_cursor_when_a_subreddit_fails(engine, monkeypatch):
    # a succeeds with a recent post; b fails transiently. The net-wide cursor
    # must NOT advance past a's post, or b's older posts would never be
    # re-fetched (silent data loss).
    def flaky(subreddit, query, after=None, before=None):
        if subreddit == "b":
            raise RedlensError("r/b: rate limited")
        yield from {"a": [raw("p1", "a", ts=NOW - 1000)]}.get(subreddit, [])
    monkeypatch.setattr(arctic, "iter_subreddit_query", flaky)

    res = track_topic(engine, "x", subreddits=["a", "b"])
    assert "b" in res.failed and res.posts_new == 1
    with Session(engine) as s:
        topic = get_topic(s, "x")
        assert topic is not None and topic.newest_seen_utc is None   # not advanced

    # b recovers with an OLDER post; since the cursor never moved, the next track
    # re-queries the full window and still picks p2 up.
    def healthy(subreddit, query, after=None, before=None):
        yield from {"a": [raw("p1", "a", ts=NOW - 1000)],
                    "b": [raw("p2", "b", ts=NOW - 2000)]}.get(subreddit, [])
    monkeypatch.setattr(arctic, "iter_subreddit_query", healthy)
    track_topic(engine, "x")
    with Session(engine) as s:
        post_ids = {p.post_id for p in s.exec(select(Post)).all()}
    assert "p2" in post_ids                            # recovered, not lost


def test_track_widening_failure_does_not_strand_new_subreddit(engine, monkeypatch):
    # Phase 1: track over sub "a" — succeeds, cursor advances to p1.
    monkeypatch.setattr(arctic, "iter_subreddit_query",
                        fake_query({"a": [raw("p1", "a", ts=NOW - 1000)]}))
    track_topic(engine, "x", subreddits=["a"])
    with Session(engine) as s:
        assert get_topic(s, "x").newest_seen_utc == NOW - 1000

    # Phase 2: widen the net with sub "b", which fails transiently. The widened
    # net is persisted, so a *stale* cursor would make the next run go
    # incremental and skip b's older posts — the cursor must reset to force a
    # full re-pull instead.
    def flaky(subreddit, query, after=None, before=None):
        if subreddit == "b":
            raise RedlensError("r/b: rate limited")
        yield from {"a": [raw("p1", "a", ts=NOW - 1000)]}.get(subreddit, [])
    monkeypatch.setattr(arctic, "iter_subreddit_query", flaky)
    res = track_topic(engine, "x", subreddits=["a", "b"])
    assert "b" in res.failed
    with Session(engine) as s:
        assert get_topic(s, "x").newest_seen_utc is None    # reset, not stale

    # Phase 3: b recovers with an OLDER post; the forced full re-pull gets it.
    def healthy(subreddit, query, after=None, before=None):
        yield from {"a": [raw("p1", "a", ts=NOW - 1000)],
                    "b": [raw("p2", "b", ts=NOW - 9000)]}.get(subreddit, [])
    monkeypatch.setattr(arctic, "iter_subreddit_query", healthy)
    track_topic(engine, "x")
    with Session(engine) as s:
        post_ids = {p.post_id for p in s.exec(select(Post)).all()}
    assert "p2" in post_ids                                 # recovered, not lost


def test_list_topics_rollup_and_recency(engine, monkeypatch):
    data = {"dualipa": [raw("p1", "dualipa"), raw("p2", "dualipa")],
            "Ozempic": [raw("p3", "Ozempic")]}
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_query(data))
    track_topic(engine, "dua lipa", subreddits=["dualipa", "dua_lipa"])
    time.sleep(1.1)  # ensure a later last_tracked_at for the second topic
    track_topic(engine, "ozempic", query="ozempic, glp-1", subreddits=["Ozempic"])

    with Session(engine) as s:
        rows = list_topics(s)

    assert [r.name for r in rows] == ["ozempic", "dua lipa"]  # most-recent first
    by_name = {r.name: r for r in rows}
    assert by_name["dua lipa"].matched_posts == 2
    assert by_name["dua lipa"].subreddit_count == 2
    assert by_name["dua lipa"].keywords == ["dua lipa"]
    assert by_name["ozempic"].matched_posts == 1
    assert by_name["ozempic"].keywords == ["ozempic", "glp-1"]
    assert by_name["ozempic"].last_tracked_at is not None


def test_list_topics_empty_db(engine):
    with Session(engine) as s:
        assert list_topics(s) == []


def test_cli_topics_text_and_json(engine, tmp_path, monkeypatch, capsys):
    db = tmp_path / "t.db"
    monkeypatch.setattr(arctic, "iter_subreddit_query",
                        fake_query({"dualipa": [raw("p1", "dualipa")]}))
    assert main(["--db", str(db), "topics"]) == 0      # empty queue: no crash
    assert main(["--db", str(db), "track", "dua lipa",
                 "--subreddits", "dualipa", "--yes"]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "topics"]) == 0
    out = capsys.readouterr().out
    assert "dua lipa" in out and "1 posts" in out

    assert main(["--db", str(db), "topics", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["name"] == "dua lipa"
    assert payload[0]["matched_posts"] == 1


def test_cli_show_topic(engine, tmp_path, monkeypatch, capsys):
    db = tmp_path / "t.db"
    monkeypatch.setattr(arctic, "iter_subreddit_query",
                        fake_query({"dualipa": [raw("p1", "dualipa")]}))
    assert main(["--db", str(db), "track", "dua lipa",
                 "--subreddits", "dualipa", "--yes"]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "show", "--topic", "dua lipa", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["name"] == "dua lipa"
    assert out["matched_posts"] == 1


def test_cli_show_requires_user_or_topic(engine, tmp_path):
    db = tmp_path / "t.db"
    assert main(["--db", str(db), "show"]) == 1


def test_untrack_drops_topic_and_orphaned_matches(engine, monkeypatch):
    data = {"dualipa": [raw("p1", "dualipa"), raw("p2", "dualipa")]}
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_query(data))
    track_topic(engine, "dua lipa", subreddits=["dualipa"])
    # A comment riding under a matched post, by a non-synced author.
    with Session(engine) as s:
        s.add(Comment(comment_id="c1", author_username="bob",
                      subreddit_name="dualipa", link_id="p1", created_utc=NOW))
        s.commit()

    res = untrack_topic(engine, "dua lipa")

    assert (res.links_removed, res.posts_deleted, res.comments_deleted) == (2, 2, 1)
    with Session(engine) as s:
        assert get_topic(s, "dua lipa") is None
        assert s.exec(select(TopicPost)).all() == []
        assert s.exec(select(Post)).all() == []
        assert s.exec(select(Comment)).all() == []


def test_untrack_keeps_posts_other_topics_reference(engine, monkeypatch):
    data = {"dualipa": [raw("p1", "dualipa")], "popheads": [raw("p1", "popheads")]}
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_query(data))
    track_topic(engine, "dua lipa", subreddits=["dualipa"])
    track_topic(engine, "popheads", subreddits=["popheads"])   # also tags p1

    res = untrack_topic(engine, "dua lipa")

    assert res.posts_deleted == 0                              # p1 still tagged
    with Session(engine) as s:
        assert {p.post_id for p in s.exec(select(Post)).all()} == {"p1"}
        assert get_topic(s, "popheads") is not None


def test_untrack_keeps_synced_users_posts(engine, monkeypatch):
    data = {"dualipa": [raw("p1", "dualipa")]}                 # author is "alice"
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_query(data))
    track_topic(engine, "dua lipa", subreddits=["dualipa"])
    with Session(engine) as s:
        s.add(User(username="alice"))                          # alice is synced
        s.commit()

    res = untrack_topic(engine, "dua lipa")

    assert res.posts_deleted == 0                              # alice's post survives
    with Session(engine) as s:
        assert {p.post_id for p in s.exec(select(Post)).all()} == {"p1"}


def test_cli_untrack_yes_and_unknown(engine, tmp_path, monkeypatch, capsys):
    db = tmp_path / "t.db"
    monkeypatch.setattr(arctic, "iter_subreddit_query",
                        fake_query({"dualipa": [raw("p1", "dualipa")]}))
    assert main(["--db", str(db), "untrack", "ghost", "-y"]) == 2  # NotFound
    assert main(["--db", str(db), "track", "dua lipa",
                 "--subreddits", "dualipa", "--yes"]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "untrack", "dua lipa", "-y"]) == 0
    assert "untracked 'dua lipa'" in capsys.readouterr().out
    assert main(["--db", str(db), "topics", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


@pytest.mark.integration
def test_track_against_real_arctic(tmp_path, monkeypatch):
    """Weekly canary: does arctic's scoped full-text search still answer in
    the *shape* track expects? Asserts a well-formed response, not a non-zero
    count — a quiet window or arctic downtime is infra noise, not a redlens
    regression."""
    monkeypatch.setattr(arctic, "MAX_ITEMS_PER_STREAM", 5)
    engine = connect(tmp_path / "live.db")
    init_schema(engine)
    res = track_topic(engine, "ozempic", subreddits=["Ozempic"], days=365)
    assert res.subreddits_searched == 1            # the request was made
    assert "Ozempic" not in res.failed             # and it came back well-formed
    assert isinstance(res.posts_new, int)
    assert render_topic_page(engine, "ozempic").startswith("<!doctype html>")

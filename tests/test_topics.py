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


def raw(pid, sub, *, ts=None, author="alice", score=10, num_comments=2,
        title="about dua lipa"):
    return {
        "id": pid, "subreddit": sub, "author": author,
        "created_utc": ts or NOW - 3600, "title": title,
        "score": score, "num_comments": num_comments,
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
        assert topic.id is not None                    # surrogate id assigned
        assert topic.keyword_list == ["dua lipa"]
        assert set(topic.subreddit_list) == {"DuaLipa", "dua_lipa", "dualipa"}
        assert topic.newest_seen_utc == NOW - 3600
        assert {t.post_id for t in s.exec(select(TopicPost))} == {"p1", "p2"}
        assert {t.topic_id for t in s.exec(select(TopicPost))} == {topic.id}
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

    calls.clear()
    track_topic(engine, "x", days=365)                 # window extended: rewind
    # generous tolerance: NOW is import-time, the call is minutes later on
    # slow CI runners — the point is the rewind to ~365 days, not seconds
    assert all(abs(c["after"] - (NOW - 365 * 86400)) < 600 for c in calls)


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


def test_multi_term_query_ors_and_dedupes(engine, monkeypatch):
    calls = []

    def it(subreddit, query, after=None, before=None):
        calls.append(query)
        yield raw("shared", subreddit)                 # found by both terms
        if query == "universal basic income":
            yield raw("longform", subreddit)           # only the spelt-out term

    monkeypatch.setattr(arctic, "iter_subreddit_query", it)
    res = track_topic(engine, "ubi",
                      query="ubi, universal basic income",
                      subreddits=["BasicIncome"])
    assert calls == ["ubi", "universal basic income"]  # one search per term
    assert res.posts_new == 2                          # 'shared' deduped
    with Session(engine) as s:
        topic = get_topic(s, "ubi")
        assert topic.keyword_list == ["ubi", "universal basic income"]


def comment_raw(cid, link_id, *, body="ubi is great", ts=None, score=3):
    return {
        "id": cid, "author": "bob", "subreddit": "BasicIncome",
        "link_id": f"t3_{link_id}", "created_utc": ts or NOW - 100,
        "body": body, "score": score,
    }


def test_pull_topic_comments_via_link_bridge(engine, monkeypatch):
    from redlens.topics import pull_topic_comments, topic_comments
    posts = {"BasicIncome": [raw("p1", "BasicIncome", num_comments=2),
                             raw("p2", "BasicIncome", num_comments=0)]}
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_subreddit_query(posts))
    track_topic(engine, "ubi", subreddits=["BasicIncome"])

    threads = {"p1": [comment_raw("c1", "p1"), comment_raw("c2", "p1")]}

    def fake_post_comments(post_id):
        if post_id == "p2":
            raise AssertionError("must skip posts with num_comments=0")
        yield from threads.get(post_id, [])

    monkeypatch.setattr(arctic, "iter_post_comments", fake_post_comments)
    n = pull_topic_comments(engine, "ubi")
    assert n == 2
    with Session(engine) as s:
        got = topic_comments(s, "ubi")            # derived via link_id, no table
        assert {c.comment_id for c in got} == {"c1", "c2"}

    n2 = pull_topic_comments(engine, "ubi")        # idempotent
    assert n2 == 2
    with Session(engine) as s:
        assert len(topic_comments(s, "ubi")) == 2


def test_page_folds_comment_text_into_themes(engine, monkeypatch):
    from redlens.page import render_topic_page
    from redlens.topics import pull_topic_comments
    # posts say little; the signal lives in the comments
    posts = {"BasicIncome": [raw(f"p{i}", "BasicIncome", title="discussion",
                                 num_comments=1) for i in range(8)]}
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_subreddit_query(posts))
    track_topic(engine, "ubi", subreddits=["BasicIncome"])
    monkeypatch.setattr(
        arctic, "iter_post_comments",
        lambda pid: iter([comment_raw(f"{pid}c", pid,
                                      body="automation displaces workers")]),
    )
    pull_topic_comments(engine, "ubi")
    doc = render_topic_page(engine, "ubi")
    assert "comments analyzed" in doc
    assert "real times" in doc                     # punchcard uses comment ts
    assert "and comments" in doc                    # themes note


def test_exclude_terms_drop_homonym_noise(engine, monkeypatch):
    # Regression for the live UBI run: "ubi" is gamer slang for Ubisoft,
    # so gaming posts flooded the topic. --exclude is the textual defense.
    data = {"BasicIncome": [
        raw("policy", "BasicIncome", title="UBI pilot results are in"),
        raw("noise1", "BasicIncome", title="Ubisoft announces UBI... in a game"),
        raw("noise2", "BasicIncome", title="ok", ts=NOW - 60),
    ]}
    data["BasicIncome"][2] = raw("noise2", "BasicIncome", title="Rainbow Six ubi moment")
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_subreddit_query(data))

    res = track_topic(engine, "ubi", subreddits=["BasicIncome"],
                      exclude="ubisoft, rainbow six")
    assert res.posts_new == 1
    with Session(engine) as s:
        assert {t.post_id for t in s.exec(select(TopicPost))} == {"policy"}
        assert get_topic(s, "ubi").exclude_terms == "ubisoft, rainbow six"

    # the stored exclusions keep applying on re-tracks, without re-passing
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_subreddit_query(
        {"BasicIncome": [raw("noise3", "BasicIncome",
                             title="Ubisoft again", ts=NOW - 10)]}))
    res = track_topic(engine, "ubi")
    assert res.posts_new == 0


def test_influence_ranking_resists_bot_volume(engine, monkeypatch):
    # Regression for the live ozempic run: a news bot with 181 ignored
    # posts must rank below a sustained human with engaged posts.
    from redlens.page import _influential_users
    bots = [raw(f"b{i}", "news", author="NewsBot", score=1, num_comments=0,
                ts=NOW - i) for i in range(50)]
    humans = [raw(f"h{i}", "sub", author="human", score=40, num_comments=10,
                  ts=NOW - i) for i in range(3)]
    names = [label for label, _ in _influential_users(
        [Post.from_arctic(r) for r in bots + humans], top=5)]
    assert names and names[0].startswith("human")
    assert not any(n.startswith("NewsBot") for n in names)


def test_rerun_with_new_keywords_backfills_full_window(engine, monkeypatch):
    # Adding a keyword must backfill the whole window for the new term, not
    # just match newer-than-cursor — otherwise history under the new term is
    # silently missed.
    calls = []

    def it(subreddit, query, after=None, before=None):
        calls.append((query, after))
        yield raw(f"{query}-post", subreddit, ts=NOW - 100)   # recent → cursor ~NOW

    monkeypatch.setattr(arctic, "iter_subreddit_query", it)
    full_window = NOW - 180 * 86400
    track_topic(engine, "ubi", query="ubi", subreddits=["BasicIncome"])

    calls.clear()                                      # re-track, keywords unchanged
    track_topic(engine, "ubi")
    assert calls[0][1] == NOW - 100                    # incremental: cursor applied

    calls.clear()                                      # re-track with an added keyword
    track_topic(engine, "ubi", query="ubi, universal basic income")
    afters = {q: a for q, a in calls}
    assert afters["ubi"] == full_window                # changed set → all re-pulled
    assert afters["universal basic income"] == full_window
    with Session(engine) as s:
        topic = get_topic(s, "ubi")
        assert topic.keyword_list == ["ubi", "universal basic income"]
        assert len(s.exec(select(TopicPost)).all()) == 2   # additive union


def test_reset_clears_then_repulls(engine, monkeypatch):
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_subreddit_query(
        {"BasicIncome": [raw("old", "BasicIncome")]}))
    track_topic(engine, "ubi", query="ubi", subreddits=["BasicIncome"])
    with Session(engine) as s:
        assert {t.post_id for t in s.exec(select(TopicPost))} == {"old"}

    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_subreddit_query(
        {"BasicIncome": [raw("fresh", "BasicIncome")]}))
    track_topic(engine, "ubi", reset=True)
    with Session(engine) as s:
        # old match cleared, only the fresh pull remains
        assert {t.post_id for t in s.exec(select(TopicPost))} == {"fresh"}


def test_one_bad_subreddit_does_not_sink_the_net(engine, monkeypatch):
    from redlens.errors import RedlensError

    def it(subreddit, query, after=None, before=None):
        if subreddit == "deadsub":
            raise RedlensError("arctic GET ...: HTTP Error 503")
        yield raw("p1", subreddit)

    monkeypatch.setattr(arctic, "iter_subreddit_query", it)
    res = track_topic(engine, "x", subreddits=["deadsub", "livesub"])
    assert res.posts_new == 1
    assert "deadsub" in res.failed
    assert res.per_subreddit.get("livesub") == 1


def test_mid_pull_failure_keeps_partial_batch(engine, monkeypatch):
    from redlens.errors import RedlensError

    def it(subreddit, query, after=None, before=None):
        yield raw("kept1", subreddit)
        yield raw("kept2", subreddit)
        raise RedlensError("arctic GET ...: exhausted retries")

    monkeypatch.setattr(arctic, "iter_subreddit_query", it)
    res = track_topic(engine, "x", subreddits=["flaky"])
    assert res.posts_new == 2                          # fetched-before-failure saved
    assert "flaky" in res.failed
    with Session(engine) as s:
        assert {t.post_id for t in s.exec(select(TopicPost))} == {"kept1", "kept2"}


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
    assert "Most influential users" in doc and "u/alice" in doc
    assert "Themes" in doc                             # LDA section renders
    assert "busiest day" in doc                        # spike note links top post
    assert 'class="chart punch"' in doc                # weekday x hour grid
    assert doc == render_topic_page(engine, "dua lipa")  # byte-deterministic


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

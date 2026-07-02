"""Coordinated-network queries behind ``redlens serve``.

The server opens the DB read-only through its own ``sqlite3`` connection, so
these tests seed a real file (not ``:memory:``) via SQLModel, then point
``Network`` at that path.
"""
import pytest
from sqlmodel import Session

from redlens.db import connect, init_schema, upsert
from redlens.models import Comment, Post, User
from redlens.serve import Network


@pytest.fixture
def net(tmp_path):
    path = str(tmp_path / "redlens.db")
    engine = connect(path)
    init_schema(engine)
    with Session(engine) as s:
        upsert(s, [
            User(username="alice", post_karma=100, comment_karma=50),
            User(username="bob", post_karma=5, comment_karma=1),
            # carol has no User row — stats must degrade to null, not crash.
        ])
        upsert(s, [
            Post(post_id="p1", author_username="alice", subreddit_name="vpn",
                 created_utc=1_700_000_000, title="try nord", score=10),
            Post(post_id="p2", author_username="bob", subreddit_name="vpn",
                 created_utc=1_700_100_000, title="me too", score=2),
            Post(post_id="p3", author_username="alice", subreddit_name="solo",
                 created_utc=1_700_200_000, title="alone", score=1),
        ])
        upsert(s, [
            # alice, bob, carol all comment in thread p1 (co-activity); carol
            # and alice also share r/vpn.
            Comment(comment_id="c1", author_username="alice", subreddit_name="vpn",
                    link_id="p1", created_utc=1_700_000_100, body="a", score=3),
            Comment(comment_id="c2", author_username="bob", subreddit_name="vpn",
                    link_id="p1", created_utc=1_700_000_200, body="b", score=1),
            Comment(comment_id="c3", author_username="carol", subreddit_name="vpn",
                    link_id="p1", created_utc=1_700_000_300, body="c", score=0),
            Comment(comment_id="c4", author_username="carol", subreddit_name="cats",
                    link_id="z9", created_utc=1_700_000_400, body="meow", score=0),
        ])
        s.commit()
    return Network(path)


def test_overview_counts_the_whole_network(net):
    o = net.overview()
    assert o["accounts"] == 3          # alice, bob, carol (authors, not User rows)
    assert o["posts"] == 3
    assert o["comments"] == 4
    assert o["subreddits"] == 3        # vpn, solo, cats
    assert o["first_utc"] == 1_700_000_000
    assert o["last_utc"] == 1_700_200_000


def test_accounts_roll_up_volume_and_degrade_missing_stats(net):
    rows = {a["username"]: a for a in net.accounts()}
    assert set(rows) == {"alice", "bob", "carol"}
    alice = rows["alice"]
    assert (alice["posts"], alice["comments"], alice["total"]) == (2, 1, 3)
    assert alice["subreddits"] == 2            # vpn + solo
    assert alice["post_karma"] == 100
    assert alice["top_subreddit"] == "vpn"     # 2 vpn vs 1 solo
    assert rows["carol"]["post_karma"] is None  # no User row → null, not a crash


def test_accounts_sorted_by_total_desc(net):
    totals = [a["total"] for a in net.accounts()]
    assert totals == sorted(totals, reverse=True)


def test_shared_subreddits_need_two_accounts(net):
    res = net.subreddits()
    assert res["total"] == 1                    # only r/vpn is shared
    subs = {s["subreddit"]: s for s in res["rows"]}
    assert set(subs) == {"vpn"}                 # solo/cats are single-account
    vpn = subs["vpn"]
    assert vpn["accounts"] == 3
    assert vpn["members"] == ["alice", "bob", "carol"]
    assert vpn["posts"] == 2 and vpn["comments"] == 3


def test_threads_need_two_accounts_and_carry_title(net):
    res = net.threads()
    assert res["total"] == 1                    # only p1 has ≥2 authors
    assert len(res["rows"]) == 1
    t = res["rows"][0]
    assert t["link_id"] == "p1"
    assert t["accounts"] == 3
    assert t["comments"] == 3
    assert t["title"] == "try nord"            # resolved from the post
    assert t["members"] == ["alice", "bob", "carol"]


def test_content_drills_posts_and_comments_newest_first(net):
    posts = net.content("alice", "posts", limit=50, offset=0)
    assert posts["total"] == 2
    assert [p["title"] for p in posts["items"]] == ["alone", "try nord"]  # desc

    comments = net.content("alice", "comments", limit=50, offset=0)
    assert comments["total"] == 1
    assert comments["items"][0]["body"] == "a"


def test_content_paginates(net):
    page = net.content("alice", "posts", limit=1, offset=1)
    assert page["limit"] == 1 and page["offset"] == 1
    assert len(page["items"]) == 1
    assert page["items"][0]["title"] == "try nord"  # 2nd newest

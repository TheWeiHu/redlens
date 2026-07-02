"""Coordinated-network queries behind ``redlens serve``.

The server opens the DB read-only through its own ``sqlite3`` connection, so
these tests seed a real file (not ``:memory:``) via SQLModel, then point
``Network`` at that path.
"""
import pytest
from sqlmodel import Session

from redlens.db import connect, init_schema, upsert
from redlens.models import Comment, Post, User
from redlens.serve import Network, load_brands, load_cohorts


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
    # matrix cells: per-account activity (posts + comments) in the sub
    assert vpn["cells"] == {"alice": 2, "bob": 2, "carol": 1}
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
    # matrix cells: per-account comment counts in the thread
    assert t["cells"] == {"alice": 1, "bob": 1, "carol": 1}


def test_pairs_relate_every_entangled_account_pair(net):
    res = net.pairs()
    # column order: most active first (alice 3), ties broken by name
    assert res["accounts"] == ["alice", "bob", "carol"]
    assert res["total_accounts"] == 3
    pairs = {(p["a"], p["b"]): p for p in res["pairs"]}
    # all three share r/vpn and all three commented in thread p1
    assert set(pairs) == {("alice", "bob"), ("alice", "carol"),
                          ("bob", "carol")}
    assert all(p["subs"] == 1 and p["threads"] == 1 for p in pairs.values())


def test_mentions_surface_shared_names_not_prose(tmp_path):
    path = str(tmp_path / "brands.db")
    engine = connect(path)
    init_schema(engine)
    with Session(engine) as s:
        upsert(s, [
            Post(post_id="p1", author_username="alice", subreddit_name="vpn",
                 created_utc=1, title="NordVPN saved me",
                 selftext="I love NordVPN"),
            Post(post_id="p2", author_username="bob", subreddit_name="vpn",
                 created_utc=2, title="is NordVPN worth it"),
        ])
        upsert(s, [
            # "Great" is capitalized once but lowercase twice — prose, not a
            # name, so the capitalization-ratio gate must drop it.
            Comment(comment_id="c1", author_username="alice",
                    subreddit_name="vpn", link_id="p2", created_utc=3,
                    body="Great value, great speed, great support"),
            Comment(comment_id="c2", author_username="bob",
                    subreddit_name="vpn", link_id="p2", created_utc=4,
                    body="NordVPN it is"),
        ])
        s.commit()
    res = Network(path).mentions()
    terms = {r["term"]: r for r in res["rows"]}
    assert "NordVPN" in terms
    nord = terms["NordVPN"]
    assert nord["accounts"] == 2
    assert nord["cells"] == {"alice": 2, "bob": 2}   # matrix cells per account
    assert "Great" not in terms                       # prose word filtered


def test_profile_rolls_up_one_account(net):
    p = net.profile("alice")
    assert (p["posts"], p["comments"], p["subreddits"]) == (2, 1, 2)
    assert p["post_karma"] == 100
    assert p["first_utc"] == 1_700_000_000
    # vpn (post p1 + comment c1) outranks solo (post p3)
    assert [s["subreddit"] for s in p["top_subreddits"]] == ["vpn", "solo"]
    assert p["top_subreddits"][0]["posts"] == 1
    assert p["top_subreddits"][0]["comments"] == 1
    # bob and carol each share r/vpn and thread p1 with alice
    co = {c["account"]: c for c in p["coactors"]}
    assert set(co) == {"bob", "carol"}
    assert co["bob"] == {"account": "bob", "subs": 1, "threads": 1}


def test_profile_rejects_unknown_accounts(net):
    with pytest.raises(ValueError, match="unknown account"):
        net.profile("nobody")


def test_load_cohorts_maps_accounts_to_labels(tmp_path):
    p = tmp_path / "cohorts.csv"
    p.write_text(
        "# ground truth\n"
        "bob, coordinated\n"
        "carol, coordinated\n"
        "alice, organic\n"
        "dangling-line-without-label\n",
        encoding="utf-8")
    assert load_cohorts(p) == {
        "bob": "coordinated", "carol": "coordinated", "alice": "organic"}


def test_cohorts_group_the_matrix_and_tag_everything(net):
    labels = {"bob": "coordinated", "carol": "coordinated",
              "alice": "organic"}
    n = Network(net.path, cohorts=labels)
    # matrix order: cohorts in file order (coordinated first), activity
    # within — even though alice is the most active account overall
    assert n.pairs()["accounts"] == ["bob", "carol", "alice"]
    assert n.pairs()["cohorts"] == labels
    # overview counts per cohort, in the same order
    assert n.overview()["cohorts"] == [
        {"cohort": "coordinated", "accounts": 2},
        {"cohort": "organic", "accounts": 1}]
    # accounts and profiles carry the label ('' when unlabeled)
    rows = {a["username"]: a["cohort"] for a in n.accounts()}
    assert rows == {"alice": "organic", "bob": "coordinated",
                    "carol": "coordinated"}
    assert n.profile("bob")["cohort"] == "coordinated"
    assert "cohorts" not in net.overview()   # unlabeled DB: no cohort block


def test_load_brands_parses_names_aliases_and_comments(tmp_path):
    p = tmp_path / "brands.csv"
    p.write_text(
        "# roster\n"
        "\n"
        "NordVPN, nordvpn, nord vpn\n"
        "Shef\n",
        encoding="utf-8")
    assert load_brands(p) == [
        ("NordVPN", ["nordvpn", "nord vpn"]),  # aliases are the match terms
        ("Shef", ["Shef"]),                    # a bare name matches itself
    ]


def test_roster_mentions_match_case_insensitively(net):
    # the fixture's texts never capitalize "nord" — the roster still finds it
    roster = [("NordVPN", ["nord"]), ("Ghost", ["ghost"])]
    res = Network(net.path, roster=roster).mentions()
    assert res["source"] == "roster"
    assert res["total"] == 1                    # unmentioned brands drop out
    row = res["rows"][0]
    assert row["term"] == "NordVPN"
    assert row["cells"] == {"alice": 1}         # p1's title "try nord"
    assert row["accounts"] == 1                 # roster shows even 1-account brands


def test_mentions_fall_back_to_mining_without_roster(net):
    assert net.mentions()["source"] == "mined"


def test_pair_evidence_lists_the_shared_units(net):
    res = net.pairs()  # sanity: alice+bob are entangled
    assert any(p["a"] == "alice" and p["b"] == "bob" for p in res["pairs"])
    ev = net.pair_evidence("alice", "bob")
    assert [s["subreddit"] for s in ev["subs"]] == ["vpn"]
    # alice in r/vpn: post p1 + comment c1; bob: post p2 + comment c2
    assert (ev["subs"][0]["a_n"], ev["subs"][0]["b_n"]) == (2, 2)
    assert ev["threads"][0]["link_id"] == "p1"
    assert ev["threads"][0]["title"] == "try nord"
    assert (ev["threads"][0]["a_n"], ev["threads"][0]["b_n"]) == (1, 1)


def test_account_sub_items_merge_posts_and_comments(net):
    ev = net.account_sub_items("alice", "vpn")
    assert ev["total"] == 2                     # post p1 + comment c1
    kinds = {i["kind"] for i in ev["items"]}
    assert kinds == {"post", "comment"}


def test_account_thread_items_carry_the_thread_title(net):
    ev = net.account_thread_items("carol", "p1")
    assert ev["title"] == "try nord"
    assert ev["total"] == 1
    assert ev["items"][0]["selftext"] == "c"    # the comment body


def test_account_term_items_use_roster_terms(net):
    roster = [("NordVPN", ["nord"])]
    ev = Network(net.path, roster=roster).account_term_items("alice", "NordVPN")
    assert ev["total"] == 1
    assert ev["items"][0]["title"] == "try nord"


def test_pairs_handles_a_single_account(tmp_path):
    path = str(tmp_path / "solo.db")
    engine = connect(path)
    init_schema(engine)
    with Session(engine) as s:
        upsert(s, [Post(post_id="p1", author_username="alice",
                        subreddit_name="vpn", created_utc=1, title="hi")])
        s.commit()
    res = Network(path).pairs()
    assert res["accounts"] == ["alice"]
    assert res["pairs"] == []


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

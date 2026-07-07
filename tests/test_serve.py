"""Coordinated-network queries behind ``redlens serve``.

The server opens the DB read-only through its own ``sqlite3`` connection, so
these tests seed a real file (not ``:memory:``) via SQLModel, then point
``Network`` at that path.
"""
import pytest
from sqlmodel import Session

from redlens import serve as serve_mod
from redlens.db import connect, init_schema, upsert
from redlens.errors import MissingKey
from redlens.models import Comment, Post, Topic, TopicPost, User
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


def test_ai_profile_grounds_the_prompt_and_caches(net, monkeypatch):
    prompts_seen = []

    def fake_complete(prompt, key, **kw):
        prompts_seen.append(prompt)
        return {"persona": "a helpful techie", "promotion": "none observed",
                "coordinated": {"verdict": "organic", "confidence": 40,
                                "reason": "casual mentions only"}}

    monkeypatch.setattr(serve_mod.config, "require_llm_key", lambda: "k")
    monkeypatch.setattr(serve_mod.llm, "complete_json", fake_complete)
    n = Network(net.path, roster=[("NordVPN", ["nord"])])
    out = n.ai_profile("alice")
    assert out["coordinated"]["verdict"] == "organic"
    assert out["model"]
    prompt = prompts_seen[0]
    assert "u/alice" in prompt
    assert "try nord" in prompt                  # sampled post title
    assert "co-activity with u/bob" in prompt    # deterministic signal
    assert 'mentions "NordVPN"' in prompt        # roster signal
    # roster-breadth signal — the strongest cheap seeding tell
    assert "distinct brands from the tracked roster" in prompt
    # second call is served from the per-run cache — one LLM call total
    assert n.ai_profile("alice") is out
    assert len(prompts_seen) == 1


def test_ai_profile_stays_keyless_without_a_key(net, monkeypatch):
    monkeypatch.setattr(serve_mod.config, "llm_api_key", lambda: None)
    with pytest.raises(MissingKey):
        net.ai_profile("alice")


def test_promoted_accounts_join_the_coordinated_cohort(net):
    # carol was unlabeled (organic pool); promoting her folds her into the
    # coordinated cohort so scoping + share-of-voice pick her up, and she's
    # marked promoted.
    labels = {"alice": "coordinated", "bob": "coordinated",
              "carol": "coordinated"}
    n = Network(net.path, roster=[("Nord", ["nord"])], cohorts=labels,
                promoted={"carol"})
    o = n.overview()
    assert o["accounts"] == 3 and o["promoted"] == 1
    rows = {a["username"]: a for a in n.accounts()}
    assert rows["carol"]["promoted"] is True
    assert rows["alice"]["promoted"] is False
    assert "carol" in n._coordinated          # counts as coordinated now
    assert {a["username"] for a in n.accounts()} == {"alice", "bob", "carol"}


def test_network_view_scopes_to_labeled_cohort(net):
    # alice + bob are the curated cohort; carol is an unlabeled "organic"
    # author (as if pulled in by brand-tracking) and must drop out of the
    # network matrices and the headline count.
    labels = {"alice": "coordinated", "bob": "coordinated"}
    n = Network(net.path, cohorts=labels)
    assert {a["username"] for a in n.accounts()} == {"alice", "bob"}
    o = n.overview()
    assert o["accounts"] == 2 and o["organic_authors"] == 1
    assert all(a in labels for a in n.pairs()["accounts"])
    # unscoped (no labels) still sees every author
    assert len(Network(net.path).accounts()) == 3


def _sov_db(tmp_path):
    path = str(tmp_path / "sov.db")
    engine = connect(path)
    init_schema(engine)
    with Session(engine) as s:
        upsert(s, [
            Post(post_id="s1", author_username="seed", subreddit_name="saas",
                 created_utc=1, title="love Widget", selftext="Widget rocks"),
            Post(post_id="o1", author_username="org1", subreddit_name="saas",
                 created_utc=2, title="is Widget any good"),
        ])
        upsert(s, [
            Comment(comment_id="oc1", author_username="org2",
                    subreddit_name="saas", link_id="o1", created_utc=3,
                    body="Widget worked for me", score=1),
        ])
        s.commit()
    return path


def test_share_of_voice_splits_coordinated_vs_organic(tmp_path):
    # Gadget is mentioned ONLY by the seeder — without an organic baseline its
    # "100%" is an artifact of what was archived, so it must be flagged and
    # sorted after the measurable brands.
    n = Network(_sov_db(tmp_path),
                roster=[("Widget", ["widget"]), ("Gadget", ["widget rocks"])],
                cohorts={"seed": "coordinated"})
    sov = n.share_of_voice()
    assert sov["available"] and sov["coordinated_accounts"] == 1
    w, g = sov["rows"]
    assert w["term"] == "Widget" and w["baseline"] is True
    assert w["total"] == 3           # s1 title+selftext = 1 post match, o1, oc1
    assert w["coordinated"] == 1 and w["organic"] == 2
    assert w["coord_pct"] == 33
    assert w["coord_authors"] == 1 and w["organic_authors"] == 2
    assert set(w["top_organic"]) == {"org1", "org2"}
    assert g["term"] == "Gadget" and g["baseline"] is False
    assert g["coord_pct"] == 100     # only the seeder was archived


def test_cohort_views_need_more_than_one_cohort(net):
    one = Network(net.path, roster=[("Nord", ["nord"])],
                  cohorts={"alice": "coordinated", "bob": "coordinated"})
    assert one.multi_cohort is False
    assert one.cohort_comparison()["available"] is False
    assert one.cohort_bridges()["available"] is False
    assert one.seeding_waves()["available"] is False


def test_cohort_bridges_link_accounts_across_cohorts(net):
    # alice (coordinated) and carol (smartiflix) both comment in thread p1 —
    # a cross-cohort bridge; Nord is pushed only by alice's cohort.
    two = Network(net.path, roster=[("Nord", ["nord"])],
                  cohorts={"alice": "coordinated", "carol": "smartiflix"})
    assert two.multi_cohort is True
    edges = two.cohort_bridges()["edges"]
    assert any({e["a"], e["b"]} == {"alice", "carol"}
               and {e["coh_a"], e["coh_b"]} == {"coordinated", "smartiflix"}
               for e in edges)
    cc = two.cohort_comparison()
    assert cc["available"] and set(cc["cohorts"]) == {"coordinated", "smartiflix"}
    nord = next(r for r in cc["rows"] if r["brand"] == "Nord")
    assert nord["by"]["coordinated"] == 1 and nord["shared"] is False


def test_suggested_coordinated_flags_multi_brand_unlabeled_authors(tmp_path):
    path = str(tmp_path / "sus.db")
    engine = connect(path)
    init_schema(engine)
    with Session(engine) as s:
        upsert(s, [
            # 'hidden' is unlabeled but pushes 3 distinct roster brands → flag
            Post(post_id="h1", author_username="hidden", subreddit_name="x",
                 created_utc=1, title="love Alpha and Beta", selftext="Gamma too"),
            # 'real' is unlabeled but mentions one brand → genuine organic, skip
            Post(post_id="r1", author_username="real", subreddit_name="x",
                 created_utc=2, title="Alpha worked for me"),
            # 'seed' is already labeled coordinated → never a "suggestion"
            Post(post_id="s1", author_username="seed", subreddit_name="x",
                 created_utc=3, title="Alpha Beta Gamma Delta"),
        ])
        s.commit()
    roster = [("Alpha", ["alpha"]), ("Beta", ["beta"]),
              ("Gamma", ["gamma"]), ("Delta", ["delta"])]
    n = Network(path, roster=roster, cohorts={"seed": "coordinated"})
    res = n.suggested_coordinated()
    assert res["available"] and res["threshold"] == 3
    assert [r["account"] for r in res["rows"]] == ["hidden"]   # only the pusher
    hit = res["rows"][0]
    assert hit["brand_count"] == 3
    assert hit["brands"] == ["Alpha", "Beta", "Gamma"]


def test_suggested_coordinated_needs_roster_and_labels(net):
    assert net.suggested_coordinated()["available"] is False


def test_share_of_voice_needs_roster_and_labels(net):
    # no roster → unavailable; roster but no coordinated label → unavailable
    assert net.share_of_voice()["available"] is False
    assert Network(net.path, roster=[("X", ["x"])]).share_of_voice()[
        "available"] is False


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


def _topic_db(tmp_path, *, cohorts=False):
    """A DB with tracked topics + topicpost, for the listening layer."""
    path = str(tmp_path / "redlens.db")
    engine = connect(path)
    init_schema(engine)
    with Session(engine) as s:
        upsert(s, [User(username="alice"), User(username="bob")])
        upsert(s, [
            Post(post_id="p1", author_username="alice", subreddit_name="vpn",
                 created_utc=1_700_000_000, score=1),
            Post(post_id="p2", author_username="alice", subreddit_name="vpn",
                 created_utc=1_700_000_001, score=1),
            Post(post_id="p3", author_username="bob", subreddit_name="vpn",
                 created_utc=1_700_000_002, score=1),
            Post(post_id="p4", author_username="carol", subreddit_name="vpn",
                 created_utc=1_700_000_003, score=1),   # carol: not synced
        ])
        s.add(Topic(id=1, name="nordvpn"))
        s.add(Topic(id=2, name="protonvpn"))
        s.commit()
        # nordvpn: p1(alice)+p3(bob)+p4(carol) ; protonvpn: p2(alice)
        s.add_all([
            TopicPost(topic_id=1, post_id="p1"),
            TopicPost(topic_id=1, post_id="p3"),
            TopicPost(topic_id=1, post_id="p4"),
            TopicPost(topic_id=2, post_id="p2"),
        ])
        s.commit()
    return path


def test_listening_empty_for_network_only_db(net):
    # the base fixture has no tracked topics — section stays hidden
    assert net.listening() == {"topics": [], "crossings": []}


def test_listening_topics_share_of_voice(tmp_path):
    r = Network(_topic_db(tmp_path)).listening()
    names = [t["name"] for t in r["topics"]]
    assert names == ["nordvpn", "protonvpn"]        # by matched-post desc
    shares = {t["name"]: t["share"] for t in r["topics"]}
    assert shares == {"nordvpn": 75, "protonvpn": 25}  # 3 vs 1 of 4


def test_listening_crossings_scoped_to_watchlist(tmp_path):
    # no cohorts → scope is the synced `user` table (alice, bob); carol excluded
    r = Network(_topic_db(tmp_path)).listening()
    pairs = {(c["account"], c["topic"]): c["n"] for c in r["crossings"]}
    assert pairs == {("alice", "nordvpn"): 1, ("alice", "protonvpn"): 1,
                     ("bob", "nordvpn"): 1}
    assert not any(c["account"] == "carol" for c in r["crossings"])


def test_listening_crossings_scoped_to_cohort_when_labeled(tmp_path):
    # a cohort label narrows the scope to just the labeled account
    r = Network(_topic_db(tmp_path), cohorts={"alice": "coordinated"}).listening()
    assert {c["account"] for c in r["crossings"]} == {"alice"}


def test_listening_hides_irrelevance_filtered_matches(tmp_path):
    # a topicpost the relevance filter judged off-topic (relevant=False) must
    # not inflate topic volume or crossings — same rule the rest of redlens uses
    path = str(tmp_path / "redlens.db")
    engine = connect(path)
    init_schema(engine)
    with Session(engine) as s:
        upsert(s, [User(username="alice")])
        upsert(s, [
            Post(post_id="p1", author_username="alice", subreddit_name="vpn",
                 created_utc=1, score=1),
            Post(post_id="p2", author_username="alice", subreddit_name="vpn",
                 created_utc=2, score=1),
        ])
        s.add(Topic(id=1, name="nordvpn"))
        s.commit()
        s.add_all([
            TopicPost(topic_id=1, post_id="p1", relevant=True),
            TopicPost(topic_id=1, post_id="p2", relevant=False),  # off-topic
        ])
        s.commit()
    r = Network(path).listening()
    assert [t["matched"] for t in r["topics"]] == [1]          # p2 excluded
    assert {c["account"]: c["n"] for c in r["crossings"]} == {"alice": 1}

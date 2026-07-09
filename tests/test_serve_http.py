"""HTTP-layer characterization tests for ``redlens serve``.

``tests/test_serve.py`` pins the ``Network`` payloads; this file pins the
*route surface*: the real ``serve()`` wiring (sidecar CSVs, ``$TITLE``
substitution, ``BoundHandler`` + ``ThreadingHTTPServer``) is started once on
an ephemeral port and every ``Handler.do_GET`` path is exercised over real
HTTP. All fixture data is synthetic.

Quirks pinned on purpose (current behavior, not necessarily desired):
- every handler exception maps to 400 with a ``{"error": ...}`` JSON body,
  including a missing LLM key on ``/api/ai-profile``;
- the 404 body is JSON even for non-``/api`` paths;
- ``/api/content`` echoes an unknown ``kind`` back while serving posts;
- a non-numeric ``limit`` on ``/api/content`` is a 400 (``int()`` blows up).
"""
import json
import threading
import time
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
from sqlmodel import Session

from redlens import serve as serve_mod
from redlens.db import connect, init_schema, upsert
from redlens.models import Comment, Post, Topic, TopicPost, User

BASE_TS = 1_700_000_000  # 2023-11: keeps the cohort timeline to one month


def _seed(tmp) -> str:
    """A tiny two-cohort network: alice+bob (coordinated) and carol (rivalco)
    all push the roster brand within days of each other (a seeding wave) and
    co-comment thread p1 (bridges); alice and carol link the same domain."""
    path = str(tmp / "redlens.db")
    engine = connect(path)
    init_schema(engine)
    with Session(engine) as s:
        upsert(s, [
            User(username="alice", post_karma=100, comment_karma=50),
            User(username="bob", post_karma=5, comment_karma=1),
            # carol has no User row — karma must degrade to null over HTTP too
        ])
        upsert(s, [
            Post(post_id="p1", author_username="alice", subreddit_name="vpn",
                 created_utc=BASE_TS, title="try nord", score=10,
                 url="https://nordish-deals.com/offer"),
            Post(post_id="p2", author_username="bob", subreddit_name="vpn",
                 created_utc=BASE_TS + 100_000, title="nord works for me",
                 score=2),
            Post(post_id="p3", author_username="alice", subreddit_name="solo",
                 created_utc=BASE_TS + 200_000, title="alone", score=1),
            Post(post_id="p4", author_username="carol", subreddit_name="vpn",
                 created_utc=BASE_TS + 150_000, title="nord is fine",
                 selftext="see nordish-deals.com", score=1),
        ])
        upsert(s, [
            Comment(comment_id="c1", author_username="alice",
                    subreddit_name="vpn", link_id="p1",
                    created_utc=BASE_TS + 100, body="a", score=3),
            Comment(comment_id="c2", author_username="bob",
                    subreddit_name="vpn", link_id="p1",
                    created_utc=BASE_TS + 200, body="b", score=1),
            Comment(comment_id="c3", author_username="carol",
                    subreddit_name="vpn", link_id="p1",
                    created_utc=BASE_TS + 300, body="c", score=0),
        ])
        s.add(Topic(id=1, name="nordvpn"))
        s.commit()
        s.add_all([TopicPost(topic_id=1, post_id="p1"),
                   TopicPost(topic_id=1, post_id="p2")])
        s.commit()
    return path


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """The real ``serve()`` on 127.0.0.1:0 — the ephemeral port is recovered
    by capturing the ``ThreadingHTTPServer`` instance ``serve()`` builds."""
    tmp = tmp_path_factory.mktemp("serve_http")
    db = _seed(tmp)
    brands = tmp / "roster.csv"
    brands.write_text("# roster\nNordVPN, nord\nGhostShell, ghostshell\n",
                      encoding="utf-8")
    cohorts = tmp / "labels.csv"
    cohorts.write_text("alice, coordinated\nbob, coordinated\n"
                       "carol, rivalco\n", encoding="utf-8")

    mp = pytest.MonkeyPatch()
    captured: dict = {}

    class _Capture(serve_mod.ThreadingHTTPServer):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            captured["httpd"] = self

    mp.setattr(serve_mod, "ThreadingHTTPServer", _Capture)
    rc: dict = {}
    t = threading.Thread(
        target=lambda: rc.setdefault("code", serve_mod.serve(
            db, port=0, open_browser=False, brands=brands, cohorts=cohorts,
            title="net & ops")),
        daemon=True)
    t.start()
    deadline = time.monotonic() + 10
    while "httpd" not in captured and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "httpd" in captured, "server never started"
    port = captured["httpd"].server_address[1]
    assert port != 0
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        captured["httpd"].shutdown()
        t.join(timeout=10)
        assert not t.is_alive(), "server thread did not shut down"
        assert rc["code"] == 0  # serve() returns cleanly after shutdown
        mp.undo()


def _get(server: str, path: str):
    """(status, content-type, body-bytes) — 4xx/5xx included, not raised."""
    try:
        with urlopen(server + path, timeout=10) as r:
            return r.status, r.headers.get("Content-Type"), r.read()
    except HTTPError as e:
        body = e.read()
        e.close()
        return e.code, e.headers.get("Content-Type"), body


def _get_json(server: str, path: str, expect: int = 200):
    status, ctype, body = _get(server, path)
    assert status == expect, f"{path}: {status} != {expect}: {body[:200]!r}"
    assert ctype == "application/json"
    return json.loads(body)


# --------------------------------------------------------------------------- #
# the SPA page + error surface                                                 #
# --------------------------------------------------------------------------- #

def test_root_serves_the_spa_with_the_title_baked_in(server):
    status, ctype, body = _get(server, "/")
    assert status == 200
    assert ctype == "text/html; charset=utf-8"
    page = body.decode()
    assert "$TITLE" not in page                       # placeholder substituted
    assert "<title>net &amp; ops · redlens</title>" in page  # html-escaped
    assert "<h1>net &amp; ops</h1>" in page
    for tab in ("network", "brands", "cohorts", "footprint", "topics"):
        assert f'data-page="{tab}"' in page           # the nav tabs


def test_unknown_path_is_a_json_404(server):
    assert _get_json(server, "/nope", expect=404) == {"error": "not found"}


def test_unknown_api_path_is_a_json_404(server):
    assert _get_json(server, "/api/nope", expect=404) == {"error": "not found"}


# --------------------------------------------------------------------------- #
# unparameterized /api routes — status, JSON, and top-level payload keys       #
# --------------------------------------------------------------------------- #

def test_overview(server):
    o = _get_json(server, "/api/overview")
    assert set(o) == {"db", "posts", "comments", "subreddits", "first_utc",
                      "last_utc", "accounts", "organic_authors", "cohorts",
                      "promoted", "brands"}
    assert (o["accounts"], o["posts"], o["comments"]) == (3, 4, 3)
    assert o["organic_authors"] == 0                  # everyone is labeled
    assert o["cohorts"] == [{"cohort": "coordinated", "accounts": 2},
                            {"cohort": "rivalco", "accounts": 1}]
    assert o["brands"] == 2                           # roster size, not matches
    assert o["db"].endswith("redlens.db")             # handler adds the path


def test_accounts(server):
    rows = _get_json(server, "/api/accounts")
    assert set(rows) == {"accounts"}
    accounts = {a["username"]: a for a in rows["accounts"]}
    assert set(accounts) == {"alice", "bob", "carol"}
    assert set(accounts["alice"]) == {
        "username", "posts", "comments", "first_utc", "last_utc", "subreddits",
        "post_karma", "comment_karma", "total", "top_subreddit", "cohort",
        "promoted"}
    assert accounts["carol"]["post_karma"] is None    # no user row → null
    assert accounts["carol"]["cohort"] == "rivalco"


def test_pairs(server):
    p = _get_json(server, "/api/pairs")
    assert set(p) == {"accounts", "cohorts", "total_accounts", "pairs"}
    assert p["accounts"] == ["alice", "bob", "carol"]  # cohort order, then volume
    assert p["cohorts"]["carol"] == "rivalco"
    assert all(set(e) == {"a", "b", "subs", "threads"} for e in p["pairs"])


def test_mentions(server):
    m = _get_json(server, "/api/mentions")
    assert set(m) == {"source", "total", "rows"}
    assert m["source"] == "roster"
    assert m["total"] == 1                            # GhostShell: no mentions
    row = m["rows"][0]
    assert set(row) == {"term", "accounts", "uses", "cells"}
    assert row["term"] == "NordVPN" and row["accounts"] == 3


def test_share_of_voice(server):
    sov = _get_json(server, "/api/share-of-voice")
    assert set(sov) == {"available", "coordinated_accounts", "total", "rows"}
    assert sov["available"] is True and sov["coordinated_accounts"] == 2
    row = sov["rows"][0]
    assert set(row) == {"term", "baseline", "verdict", "total", "coordinated",
                        "organic", "coord_pct", "coord_authors",
                        "organic_authors", "top_coordinated", "top_organic"}
    # carol (rivalco) counts as organic for the coordinated cohort's share
    assert (row["coordinated"], row["organic"], row["coord_pct"]) == (2, 1, 67)


def test_listening(server):
    li = _get_json(server, "/api/listening")
    assert set(li) == {"topics", "crossings"}
    assert [set(t) for t in li["topics"]] == [{"name", "matched", "share"}]
    assert li["topics"][0] == {"name": "nordvpn", "matched": 2, "share": 100}
    assert {(c["account"], c["topic"], c["n"]) for c in li["crossings"]} == {
        ("alice", "nordvpn", 1), ("bob", "nordvpn", 1)}


def test_suggested_coordinated(server):
    s = _get_json(server, "/api/suggested-coordinated")
    assert set(s) == {"available", "threshold", "total", "rows"}
    assert s["available"] is True and s["threshold"] == 3
    assert s["rows"] == [] and s["total"] == 0        # everyone is labeled


def test_cohort_comparison(server):
    cc = _get_json(server, "/api/cohort-comparison")
    assert set(cc) == {"available", "cohorts", "shared", "rows"}
    assert cc["available"] is True
    assert cc["cohorts"] == ["coordinated", "rivalco"]
    row = cc["rows"][0]
    assert set(row) == {"brand", "by", "organic", "shared", "total"}
    assert row["brand"] == "NordVPN" and row["shared"] is True
    assert row["by"] == {"coordinated": 2, "rivalco": 1}


def test_cohort_timeline(server):
    tl = _get_json(server, "/api/cohort-timeline")
    assert set(tl) == {"available", "cohorts", "months", "series"}
    assert tl["available"] is True
    assert tl["months"] == ["2023-11"]
    assert set(tl["series"]) == {"coordinated", "rivalco"}


def test_cohort_bridges(server):
    br = _get_json(server, "/api/cohort-bridges")
    assert set(br) == {"available", "cohorts", "edges", "shared_subs"}
    assert br["available"] is True
    assert all(set(e) == {"a", "b", "coh_a", "coh_b", "shared"}
               for e in br["edges"])
    # thread p1 bridges the cohorts: alice–carol and bob–carol (never same-cohort)
    assert {frozenset((e["a"], e["b"])) for e in br["edges"]} == {
        frozenset(("alice", "carol")), frozenset(("bob", "carol"))}
    assert br["shared_subs"][0]["sub"] == "vpn"


def test_cohort_domains(server):
    d = _get_json(server, "/api/cohort-domains")
    assert set(d) == {"available", "cohorts", "rows"}
    assert d["available"] is True
    row = d["rows"][0]
    assert set(row) == {"domain", "by", "total"}
    assert row["domain"] == "nordish-deals.com"
    assert row["by"] == {"coordinated": 1, "rivalco": 1}


def test_seeding_waves(server):
    w = _get_json(server, "/api/seeding-waves")
    assert set(w) == {"available", "window_days", "rows"}
    assert w["available"] is True and w["window_days"] == 14
    wave = w["rows"][0]
    assert set(wave) == {"n", "start", "end", "accounts", "brand", "total",
                         "cohorts"}
    assert wave["brand"] == "NordVPN" and wave["n"] == 3
    assert wave["cohorts"] == ["coordinated", "rivalco"]


def test_coordination_raster(server):
    r = _get_json(server, "/api/coordination-raster")
    assert set(r) == {"available", "accounts", "brands", "events"}
    assert r["available"] is True
    assert r["brands"] == ["NordVPN"]
    assert all(set(a) == {"name", "cohort"} for a in r["accounts"])
    assert all(set(e) == {"a", "b", "ts"} for e in r["events"])
    assert len(r["events"]) == 3                      # one first-push each


def test_subreddits(server):
    s = _get_json(server, "/api/subreddits")
    assert set(s) == {"total", "rows"}
    assert s["total"] == 1                            # only r/vpn is shared
    row = s["rows"][0]
    assert set(row) == {"subreddit", "accounts", "posts", "comments", "cells"}
    assert row["subreddit"] == "vpn" and row["accounts"] == 3


def test_threads(server):
    t = _get_json(server, "/api/threads")
    assert set(t) == {"total", "rows"}
    row = t["rows"][0]
    assert set(row) == {"link_id", "subreddit", "accounts", "comments",
                        "cells", "title"}
    assert row["link_id"] == "p1" and row["title"] == "try nord"


# --------------------------------------------------------------------------- #
# parameterized routes                                                         #
# --------------------------------------------------------------------------- #

def test_profile(server):
    p = _get_json(server, "/api/profile?u=alice")
    assert set(p) == {"username", "cohort", "posts", "comments", "first_utc",
                      "last_utc", "subreddits", "post_karma", "comment_karma",
                      "top_subreddits", "coactors"}
    assert p["username"] == "alice" and p["cohort"] == "coordinated"
    assert (p["posts"], p["comments"]) == (2, 1)


def test_profile_unknown_account_is_a_400(server):
    err = _get_json(server, "/api/profile?u=nobody", expect=400)
    assert err == {"error": "unknown account: nobody"}


def test_profile_without_the_param_is_a_400(server):
    # `u` defaults to "" → unknown account; the handler folds it into 400
    err = _get_json(server, "/api/profile", expect=400)
    assert err == {"error": "unknown account: "}


def test_ai_profile_with_a_stubbed_llm(server, monkeypatch):
    def fake_complete(prompt, key, **kw):
        assert "u/alice" in prompt                    # grounded in the account
        return {"persona": "a helpful techie", "promotion": "none observed",
                "coordinated": {"verdict": "organic", "confidence": 40,
                                "reason": "casual mentions only"}}

    monkeypatch.setattr(serve_mod.config, "require_llm_key", lambda: "k")
    monkeypatch.setattr(serve_mod.llm, "complete_json", fake_complete)
    out = _get_json(server, "/api/ai-profile?u=alice")
    assert set(out) == {"username", "model", "persona", "promotion",
                        "coordinated"}
    assert set(out["coordinated"]) == {"verdict", "confidence", "reason"}
    assert out["coordinated"]["verdict"] == "organic"


def test_ai_profile_without_a_key_is_a_400(server, monkeypatch):
    # a different username than the stubbed test — the per-run cache would
    # otherwise serve alice without consulting the key at all
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("REDLENS_LLM_API_KEY", raising=False)
    monkeypatch.setattr(serve_mod.config, "llm_api_key", lambda: None)
    err = _get_json(server, "/api/ai-profile?u=bob", expect=400)
    assert "no LLM API key" in err["error"]


def test_evidence_pair(server):
    ev = _get_json(server, "/api/evidence?type=pair&a=alice&b=bob")
    assert set(ev) == {"subs", "threads"}
    assert [s["subreddit"] for s in ev["subs"]] == ["vpn"]
    assert ev["threads"][0]["link_id"] == "p1"
    assert ev["threads"][0]["title"] == "try nord"


def test_evidence_sub(server):
    ev = _get_json(server, "/api/evidence?type=sub&u=alice&sub=vpn")
    assert set(ev) == {"total", "items"}
    assert ev["total"] == 2                           # post p1 + comment c1
    assert {i["kind"] for i in ev["items"]} == {"post", "comment"}
    assert set(ev["items"][0]) == {"kind", "subreddit", "title", "selftext",
                                   "url", "score", "created_utc"}


def test_evidence_thread(server):
    ev = _get_json(server, "/api/evidence?type=thread&u=carol&link=p1")
    assert set(ev) == {"total", "items", "title"}
    assert ev["title"] == "try nord" and ev["total"] == 1


def test_evidence_mention(server):
    ev = _get_json(server, "/api/evidence?type=mention&u=alice&term=NordVPN")
    assert set(ev) == {"total", "items"}
    assert ev["total"] == 1
    assert ev["items"][0]["title"] == "try nord"


@pytest.mark.parametrize("query", ["type=bogus", ""])
def test_evidence_unknown_type_is_a_400(server, query):
    err = _get_json(server, f"/api/evidence?{query}", expect=400)
    assert err == {"error": "unknown evidence type"}


def test_content_defaults_to_posts(server):
    c = _get_json(server, "/api/content?u=alice")
    assert set(c) == {"kind", "total", "limit", "offset", "items"}
    assert (c["kind"], c["total"], c["limit"], c["offset"]) == \
        ("posts", 2, 50, 0)
    assert [p["title"] for p in c["items"]] == ["alone", "try nord"]  # newest
    assert set(c["items"][0]) == {"subreddit", "title", "selftext", "url",
                                  "score", "num_comments", "created_utc",
                                  "post_id"}


def test_content_comments_and_pagination(server):
    c = _get_json(server, "/api/content?u=alice&kind=comments")
    assert c["total"] == 1
    assert set(c["items"][0]) == {"subreddit", "body", "score", "created_utc",
                                  "link_id"}
    page = _get_json(server, "/api/content?u=alice&kind=posts&limit=1&offset=1")
    assert (page["limit"], page["offset"], len(page["items"])) == (1, 1, 1)
    assert page["items"][0]["title"] == "try nord"    # 2nd newest


def test_content_echoes_an_unknown_kind_but_serves_posts(server):
    c = _get_json(server, "/api/content?u=alice&kind=bogus")
    assert c["kind"] == "bogus"                       # echoed verbatim
    assert c["total"] == 2                            # …but these are posts


def test_content_non_numeric_limit_is_a_400(server):
    err = _get_json(server, "/api/content?u=alice&limit=abc", expect=400)
    assert "invalid literal" in err["error"]

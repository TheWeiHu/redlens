"""``redlens report`` — the static, self-contained dashboard export.

``render_report`` builds the same ``Network`` ``serve`` uses and snapshots
every parameterless ``ENDPOINTS`` payload (plus a bounded set of parameterized
calls) into one HTML file. These tests seed a real DB + sidecars, render, and
assert the file is self-contained, carries the expected snapshot keys and
getJSON shim, pre-bakes labeled-account profiles, and neutralizes a
``</script>`` breakout in an account name.
"""
from __future__ import annotations

import re

import pytest
from sqlmodel import Session

from redlens import serve
from redlens.cli import main
from redlens.db import connect, init_schema, upsert
from redlens.models import Comment, Post, User
from redlens.reporting import expose

# Parameterless routes (no query param) must all appear in the snapshot.
_PARAMETERLESS = [
    p for p, h in serve.ENDPOINTS.items() if h not in expose._PARAMETERIZED]


def _seed(path: str) -> None:
    engine = connect(path)
    init_schema(engine)
    with Session(engine) as s:
        upsert(s, [
            User(username="alice", post_karma=100, comment_karma=50),
            User(username="bob", post_karma=5, comment_karma=1),
        ])
        upsert(s, [
            Post(post_id="p1", author_username="alice", subreddit_name="vpn",
                 created_utc=1_700_000_000, title="try nordvpn", score=10),
            Post(post_id="p2", author_username="bob", subreddit_name="vpn",
                 created_utc=1_700_100_000, title="nordvpn too", score=2),
            Post(post_id="p3", author_username="carol", subreddit_name="vpn",
                 created_utc=1_700_200_000, title="me three", score=1),
        ])
        upsert(s, [
            Comment(comment_id="c1", author_username="alice",
                    subreddit_name="vpn", link_id="p1",
                    created_utc=1_700_000_100, body="a", score=3),
            Comment(comment_id="c2", author_username="bob",
                    subreddit_name="vpn", link_id="p1",
                    created_utc=1_700_000_200, body="b", score=1),
            Comment(comment_id="c3", author_username="carol",
                    subreddit_name="vpn", link_id="p1",
                    created_utc=1_700_000_300, body="c", score=0),
        ])
        s.commit()


@pytest.fixture
def seeded(tmp_path):
    db = str(tmp_path / "redlens.db")
    _seed(db)
    (tmp_path / "cohorts.csv").write_text(
        "alice, coordinated\nbob, coordinated\ncarol, organic\n",
        encoding="utf-8")
    (tmp_path / "brands.csv").write_text("NordVPN, nordvpn\n", encoding="utf-8")
    return tmp_path, db


def _render(tmp_path, db, **kw):
    out = expose.render_report(
        db, brands=tmp_path / "brands.csv", cohorts=tmp_path / "cohorts.csv",
        out=tmp_path / "report.html", **kw)
    return out, out.read_text(encoding="utf-8")


def test_writes_a_self_contained_file(seeded):
    tmp_path, db = seeded
    out, txt = _render(tmp_path, db)
    assert out.exists() and out.stat().st_size > 0
    # No external asset loads — every script/style/font is inline. (Outbound
    # reddit.com data links inside JS strings are fine; asset *loads* aren't.)
    assert not re.search(r'<script[^>]+\bsrc=', txt)
    assert not re.search(r'<link[^>]+\bhref=', txt)
    assert "@import" not in txt


def test_snapshot_has_every_parameterless_route(seeded):
    tmp_path, db = seeded
    _, txt = _render(tmp_path, db)
    assert "const SNAPSHOT" in txt
    for path in _PARAMETERLESS:
        assert f'"{path}"' in txt, f"missing snapshot key {path}"


def test_getjson_shim_present(seeded):
    tmp_path, db = seeded
    _, txt = _render(tmp_path, db)
    # The shim short-circuits getJSON to the embedded snapshot.
    assert "typeof SNAPSHOT !== 'undefined'" in txt
    assert "SNAPSHOT['_miss']" in txt
    assert "not captured in static export" in txt


def test_labeled_account_profile_is_prebaked(seeded):
    tmp_path, db = seeded
    _, txt = _render(tmp_path, db)
    # alice + bob are labeled (in cohorts.csv) → their profiles are baked.
    assert "/api/profile?u=alice" in txt
    assert "/api/profile?u=bob" in txt


def test_pair_evidence_is_prebaked(seeded):
    tmp_path, db = seeded
    _, txt = _render(tmp_path, db)
    # openPair fetches /api/evidence?type=pair&a=..&b=.. — at least one baked.
    assert "/api/evidence?type=pair&a=alice&b=bob" in txt


def test_key_encoding_matches_encodeuricomponent():
    # The baked key must be byte-identical to the URL the SPA builds with
    # encodeURIComponent, which leaves !'()*-._~ unescaped (Python's quote
    # would percent-encode !'()* by default → a lookup miss).
    assert expose._enc("a!'()*-._~b") == "a!'()*-._~b"
    assert expose._enc("a b/c") == "a%20b%2Fc"


def test_script_close_in_account_name_is_neutralized(tmp_path):
    db = str(tmp_path / "evil.db")
    engine = connect(db)
    init_schema(engine)
    evil = "</script><script>alert(1)</script>"
    with Session(engine) as s:
        upsert(s, [
            Post(post_id="p1", author_username=evil, subreddit_name="vpn",
                 created_utc=1, title="x", score=1),
            Post(post_id="p2", author_username="bob", subreddit_name="vpn",
                 created_utc=2, title="y", score=1),
        ])
        upsert(s, [
            Comment(comment_id="c1", author_username=evil,
                    subreddit_name="vpn", link_id="p1", created_utc=3,
                    body="a"),
            Comment(comment_id="c2", author_username="bob",
                    subreddit_name="vpn", link_id="p1", created_utc=4,
                    body="b"),
        ])
        s.commit()
    (tmp_path / "cohorts.csv").write_text(
        f"{evil}, coordinated\nbob, organic\n", encoding="utf-8")
    out = expose.render_report(
        db, cohorts=tmp_path / "cohorts.csv", out=tmp_path / "r.html")
    txt = out.read_text(encoding="utf-8")
    # The raw closing tag must NOT survive; the </ is escaped to <\/ so it
    # can't break out of the embedded <script>.
    assert "</script><script>alert(1)</script>" not in txt
    assert "<\\/script>" in txt


def test_report_cli_writes_file(tmp_path, capsys):
    db = str(tmp_path / "cli.db")
    _seed(db)
    out = tmp_path / "out.html"
    code = main(["--db", db, "report", "--title", "Exposé", "--out", str(out)])
    assert code == 0
    assert out.exists()
    assert f"wrote {out}" in capsys.readouterr().out
    txt = out.read_text(encoding="utf-8")
    assert "const SNAPSHOT" in txt

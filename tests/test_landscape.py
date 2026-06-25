"""Cross-topic comparison (``redlens landscape``/``compare``).

The contract that matters:
  - compares two or more topics by volume, deterministically and keyless;
  - the default window is the topics' *overlap* (so a long topic and a short
    one compare fairly — the matched-window gotcha);
  - ``--days`` forces an explicit common trailing window;
  - share-of-voice sums to 1 and the loudest topic sorts first.
"""
import time

import pytest
from sqlmodel import Session

from redlens.cli import main
from redlens.db import connect, init_schema
from redlens.errors import NotFound, RedlensError
from redlens.landscape import compare_topics
from redlens.models import Post, Topic, TopicPost

DAY = 86_400
NOW = (int(time.time()) // DAY) * DAY  # midnight-ish, stable day buckets


@pytest.fixture
def engine(tmp_path):
    e = connect(tmp_path / "t.db")
    init_schema(e)
    return e


def _topic(session: Session, name: str, posts: list[tuple[str, str, int]]) -> None:
    """Create ``name`` and tag ``posts`` = (post_id, subreddit, created_utc)."""
    topic = Topic(name=name)
    session.add(topic)
    session.flush()
    for pid, sub, ts in posts:
        session.add(Post(post_id=pid, author_username="alice", subreddit_name=sub,
                         created_utc=ts, title="t", score=1))
        session.add(TopicPost(topic_id=topic.id, post_id=pid))
    session.commit()


def test_compare_volume_and_share(engine):
    with Session(engine) as s:
        # All in the same day so the overlap window holds every post.
        _topic(s, "vpn", [("a1", "vpn", NOW), ("a2", "vpn", NOW),
                          ("a3", "vpn", NOW)])
        _topic(s, "nordvpn", [("b1", "nordvpn", NOW)])
        land = compare_topics(s, ["vpn", "nordvpn"])

    assert land.matched is True
    assert [t.name for t in land.topics] == ["vpn", "nordvpn"]  # loudest first
    assert land.total_posts == 4
    by = {t.name: t for t in land.topics}
    assert by["vpn"].posts == 3 and by["nordvpn"].posts == 1
    assert by["vpn"].share_of_voice == 0.75 and by["nordvpn"].share_of_voice == 0.25
    assert round(by["vpn"].share_of_voice + by["nordvpn"].share_of_voice, 6) == 1.0
    assert by["vpn"].top_subreddit == "vpn"


def test_matched_window_clips_the_longer_topic(engine):
    # vpn spans 100 days; nordvpn only the last day. The overlap window is that
    # last day, so vpn's old posts must be excluded from the comparison.
    with Session(engine) as s:
        _topic(s, "vpn", [("a1", "vpn", NOW - 100 * DAY), ("a2", "vpn", NOW)])
        _topic(s, "nordvpn", [("b1", "nordvpn", NOW)])
        land = compare_topics(s, ["vpn", "nordvpn"])

    assert land.window_start == NOW       # overlap starts at nordvpn's first post
    assert land.window_days == 1
    by = {t.name: t for t in land.topics}
    assert by["vpn"].posts == 1           # the 100-day-old post is clipped out


def test_days_flag_forces_a_common_window(engine):
    with Session(engine) as s:
        _topic(s, "vpn", [("a1", "vpn", NOW - 100 * DAY), ("a2", "vpn", NOW)])
        _topic(s, "nordvpn", [("b1", "nordvpn", NOW)])
        land = compare_topics(s, ["vpn", "nordvpn"], days=7)

    assert land.matched is False
    assert land.window_days == 7
    by = {t.name: t for t in land.topics}
    assert by["vpn"].posts == 1           # only the recent post falls in 7 days


def test_needs_two_topics_and_rejects_unknown(engine):
    with Session(engine) as s:
        _topic(s, "vpn", [("a1", "vpn", NOW)])
        with pytest.raises(RedlensError):
            compare_topics(s, ["vpn"])
        with pytest.raises(NotFound):
            compare_topics(s, ["vpn", "ghost"])


def test_cli_table_and_html(engine, tmp_path, capsys, monkeypatch):
    # main() resolves the DB from REDLENS_DB; point it at the seeded file.
    monkeypatch.setenv("REDLENS_DB", str(tmp_path / "t.db"))
    with Session(engine) as s:
        _topic(s, "vpn", [("a1", "vpn", NOW), ("a2", "vpn", NOW)])
        _topic(s, "nordvpn", [("b1", "nordvpn", NOW)])

    assert main(["landscape", "vpn", "nordvpn"]) == 0
    out = capsys.readouterr().out
    assert "landscape · 2 topics" in out and "vpn" in out and "share" in out

    page = tmp_path / "land.html"
    assert main(["compare", "vpn", "nordvpn", "-o", str(page)]) == 0
    html = page.read_text()
    assert "<!doctype html>" in html and "Share of voice" in html
    assert "near-disjoint" in html       # the brand-net caveat is surfaced

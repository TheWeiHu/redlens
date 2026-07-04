import pytest
from sqlmodel import Session

from redlens.db import connect, init_schema, upsert
from redlens.models import Post, Topic, TopicPost, User
from redlens.reporting.home import render_home


@pytest.fixture
def engine():
    eng = connect(":memory:")
    init_schema(eng)
    return eng


def _post(user, pid, *, sub="askreddit", ts=1_700_000_000):
    return Post(post_id=pid, author_username=user, subreddit_name=sub,
                created_utc=ts, score=1)


def _seed(engine):
    """A tiny world: 2 users, 2 topics, some posts, some tagged to topics."""
    with Session(engine) as s:
        upsert(s, [User(username="alice"), User(username="bob")])
        upsert(s, [
            _post("alice", "a1", sub="vpn"),
            _post("alice", "a2", sub="vpn"),
            _post("alice", "a3", sub="privacy"),
            _post("bob", "b1", sub="selfhosted"),
            _post("carol", "c1", sub="vpn"),   # carol not a synced user
        ])
        s.add(Topic(id=1, name="nordvpn"))
        s.add(Topic(id=2, name="protonvpn"))
        s.commit()
        # nordvpn: a1 (alice) + b1 (bob) + c1 (carol, unsynced)
        # protonvpn: a2 (alice)
        s.add_all([
            TopicPost(topic_id=1, post_id="a1"),
            TopicPost(topic_id=1, post_id="b1"),
            TopicPost(topic_id=1, post_id="c1"),
            TopicPost(topic_id=2, post_id="a2"),
        ])
        s.commit()


def test_renders_three_sections(engine):
    _seed(engine)
    doc = render_home(engine)
    assert doc.startswith("<!doctype html>")
    for h in ("Topics", "Users", "Crossings"):
        assert f">{h}</h2>" in doc
    assert "2 topics · 2 users" in doc


def test_topic_share_of_voice(engine):
    _seed(engine)
    doc = render_home(engine)
    # nordvpn has 3 matched posts, protonvpn 1 → 75% / 25%
    assert "75%" in doc
    assert "25%" in doc
    # topic links use the page --all slug convention
    assert 'href="nordvpn.html"' in doc


def test_users_show_main_sub_and_count(engine):
    _seed(engine)
    doc = render_home(engine)
    assert "r/vpn" in doc          # alice's most-posted sub
    assert 'href="https://www.reddit.com/user/alice"' in doc


def test_crossings_only_synced_users(engine):
    _seed(engine)
    doc = render_home(engine)
    # alice→nordvpn (1), alice→protonvpn (1), bob→nordvpn (1); carol excluded
    assert doc.count('class="x"') == 3
    assert "carol" not in doc


def test_empty_db(engine):
    doc = render_home(engine)
    assert "0 topics · 0 users" in doc
    assert "no matched topics yet" in doc
    assert "no synced users yet" in doc

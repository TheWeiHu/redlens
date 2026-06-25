"""End-to-end behavior of `redlens summarize`, with the network stubbed.

The only logic worth testing here is ours, not the model's, so we stub the one
network call (`llm.complete`) and check the parts redlens actually controls:

  - the no-key path is a clean exit 2 with a setup hint (no LLM involved);
  - the payload we hand the model is built from a *representative* sample of
    the archive — top-voted content across the user's whole history, not just
    their newest rows — and describes the person, not raw counts/karma;
  - the model's JSON is parsed into a structured Profile (and bad JSON fails
    cleanly);
  - an unknown --depth is rejected.

Each test drives the real `summarize_user` / CLI against a seeded SQLite DB,
so it exercises the user lookup, sampling, JSON parsing, and prompt assembly
together rather than mocking them apart.
"""
import json

import pytest
from sqlmodel import Session

from redlens import llm
from redlens.cli import main
from redlens.db import connect, init_schema, upsert
from redlens.errors import RedlensError
from redlens.models import Comment, Post, Topic, TopicPost, User


def _seed(session, user="Alice"):
    upsert(session, [User(username=user)])
    upsert(session, [
        Post(post_id="p1", author_username=user, subreddit_name="python",
             created_utc=1_700_000_000, title="how I learned async", score=12),
    ])
    # One defining, heavily-upvoted comment from long ago, buried under newer
    # low-score ones — the sample must surface the old one, not just the tail.
    upsert(session, [
        Comment(comment_id="old", author_username=user, subreddit_name="python",
                link_id="x", parent_id=None, created_utc=1_000, score=9999,
                body="DEFINING TAKE on language design"),
    ])
    upsert(session, [
        Comment(comment_id=f"r{i}", author_username=user, subreddit_name="python",
                link_id="x", parent_id=None, created_utc=2_000 + i, score=1,
                body=f"filler comment {i}")
        for i in range(40)
    ])
    session.commit()


@pytest.fixture
def db(tmp_path, monkeypatch):
    # Isolate config + keys so the real environment never leaks in.
    monkeypatch.setenv("REDLENS_CONFIG", str(tmp_path / "none.toml"))
    for var in ("REDLENS_LLM_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / "t.db"
    engine = connect(str(path))
    init_schema(engine)
    with Session(engine) as s:
        _seed(s)
    return path


def test_no_key_exits_2_with_setup_hint(db, capsys):
    assert main(["--db", str(db), "summarize", "alice"]) == 2
    assert "redlens setup" in capsys.readouterr().err


_STUB_JSON = """```json
{
  "demographics": {
    "gender": [{"label": "Female", "confidence": 55, "reason": "tone"}],
    "country": [{"label": "Canada", "confidence": 60, "reason": "spelling"}]
  },
  "big_five": {"openness": {"score": 88, "reason": "varied interests"}},
  "interests": "python and rust",
  "beliefs": "open source",
  "tone": "friendly"
}
```"""


def test_representative_payload_and_structured_profile(db, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    captured = {}

    def fake_complete(prompt, key, **kwargs):
        captured["prompt"] = prompt
        return _STUB_JSON                       # fenced JSON, as a model might return

    monkeypatch.setattr(llm, "complete", fake_complete)

    assert main(["--db", str(db), "summarize", "alice", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    # JSON (even fenced) parsed into the structured Profile; we set the metadata.
    assert out["username"] == "Alice" and out["model"] == "gpt-4o-mini"
    assert out["demographics"]["country"][0] == {
        "label": "Canada", "confidence": 60, "reason": "spelling"}
    assert out["big_five"]["openness"]["score"] == 88

    # The data we feed sits before the instruction block; check that half.
    data = captured["prompt"].split("Infer a profile", 1)[0]
    assert "r/python" in data                           # communities, by name
    assert "how I learned async" in data                # real content sampled
    assert "DEFINING TAKE on language design" in data   # top-voted, not recency
    assert "karma" not in data and "posts," not in data  # no raw stats fed in


def test_bad_json_fails_cleanly(db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(llm, "complete", lambda *a, **k: "sorry, I can't do that")
    from redlens.summarize import summarize_user
    with Session(connect(str(db))) as s, pytest.raises(RedlensError):
        summarize_user(s, "alice")


def test_unknown_depth_is_rejected(db, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    from redlens.summarize import summarize_user
    with Session(connect(str(db))) as s, pytest.raises(RedlensError):
        summarize_user(s, "alice", depth="exhaustive")


def _seed_topic(session, name="climate"):
    """A tracked topic with matched posts (via topicpost) and comments under
    them (via the link_id bridge), including one defining old top-voted comment
    buried under newer filler — the sample must surface it."""
    topic = Topic(name=name, keywords=json.dumps([name]),
                  subreddits=json.dumps(["climate", "science"]),
                  last_tracked_at=1_700_300_000)
    session.add(topic)
    session.flush()
    posts = [
        Post(post_id="t1", author_username="Bob", subreddit_name="climate",
             created_utc=1_700_000_000, title="carbon tax debate heats up",
             score=42),
        Post(post_id="t2", author_username="Cara", subreddit_name="science",
             created_utc=1_700_100_000, title="new solar efficiency record",
             score=8),
    ]
    upsert(session, posts)
    upsert(session, [TopicPost(topic_id=topic.id, post_id=p.post_id)
                     for p in posts])
    upsert(session, [
        Comment(comment_id="old", author_username="Bob", subreddit_name="climate",
                link_id="t1", parent_id=None, created_utc=1_000, score=9999,
                body="DEFINING ARGUMENT about policy tradeoffs"),
    ])
    upsert(session, [
        Comment(comment_id=f"c{i}", author_username="Cara",
                subreddit_name="science", link_id="t1", parent_id=None,
                created_utc=2_000 + i, score=1, body=f"filler reply {i}")
        for i in range(40)
    ])
    session.commit()
    return topic


@pytest.fixture
def topic_db(db):
    with Session(connect(str(db))) as s:
        _seed_topic(s)
    return db


_STUB_TOPIC_JSON = """```json
{
  "overview": "people argue about climate policy",
  "themes": [{"title": "carbon pricing", "summary": "taxes vs cap-and-trade"}],
  "sentiment": "concerned but engaged",
  "viewpoints": "market vs regulation"
}
```"""


def test_topic_no_key_exits_2_with_setup_hint(topic_db, capsys):
    assert main(["--db", str(topic_db), "summarize", "--topic", "climate"]) == 2
    assert "redlens setup" in capsys.readouterr().err


def test_topic_not_tracked_exits_2(db, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert main(["--db", str(db), "summarize", "--topic", "ghost"]) == 2
    assert "not found" in capsys.readouterr().err.lower()


def test_summarize_without_user_or_topic_errors(db, capsys):
    assert main(["--db", str(db), "summarize"]) == 1
    assert "username or --topic" in capsys.readouterr().err


def test_topic_representative_payload_and_structured_summary(
        topic_db, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    captured = {}

    def fake_complete(prompt, key, **kwargs):
        captured["prompt"] = prompt
        return _STUB_TOPIC_JSON

    monkeypatch.setattr(llm, "complete", fake_complete)

    assert main(
        ["--db", str(topic_db), "summarize", "--topic", "climate", "--json"]
    ) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["topic"] == "climate" and out["model"] == "gpt-4o-mini"
    assert out["themes"][0]["title"] == "carbon pricing"
    assert out["sentiment"] == "concerned but engaged"

    data = captured["prompt"].split("Summarize what", 1)[0]
    assert "climate" in data                              # the topic + keywords
    assert "r/climate" in data                            # communities, by name
    assert "carbon tax debate heats up" in data          # matched post sampled
    assert "DEFINING ARGUMENT about policy tradeoffs" in data  # top-voted comment


def test_daily_topic_sentiment_no_key_raises(topic_db):
    from redlens.errors import MissingKey
    from redlens.summarize import daily_topic_sentiment
    with Session(connect(str(topic_db))) as s, pytest.raises(MissingKey):
        daily_topic_sentiment(s, "climate")


def test_daily_topic_sentiment_buckets_llm_scores(db, monkeypatch):
    """Posts are bucketed by calendar day, the day's titles are handed to the
    model, and the returned -100..100 scores map to [-1,1] with gaps
    zero-filled."""
    from datetime import UTC, datetime

    from redlens.sentiment import _day_start
    from redlens.summarize import daily_topic_sentiment

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    t1 = int(datetime(2024, 1, 3, tzinfo=UTC).timestamp())   # day 2024-01-03
    t2 = int(datetime(2024, 1, 5, tzinfo=UTC).timestamp())   # day 2024-01-05
    d1, d2 = _day_start(t1), _day_start(t2)
    with Session(connect(str(db))) as s:
        topic = Topic(name="vpn", keywords=json.dumps(["vpn"]),
                      subreddits=json.dumps(["vpn"]), last_tracked_at=t2)
        s.add(topic)
        s.flush()
        posts = [
            Post(post_id="a", author_username="x", subreddit_name="vpn",
                 created_utc=t1, title="works great", score=5),
            Post(post_id="b", author_username="y", subreddit_name="vpn",
                 created_utc=t2, title="keeps crashing", score=9),
        ]
        upsert(s, posts)
        upsert(s, [TopicPost(topic_id=topic.id, post_id=p.post_id) for p in posts])
        # a comment under post "a" (bridged by link_id == post_id), day 1
        upsert(s, [Comment(comment_id="c1", author_username="z",
                           subreddit_name="vpn", link_id="a", parent_id=None,
                           created_utc=t1, score=7, body="totally agree, love it")])
        s.commit()

    seen = {}

    def fake_complete(prompt, key, **kwargs):
        seen["prompt"] = prompt
        return json.dumps({"days": [{"day": d1, "score": 80},
                                    {"day": d2, "score": -60}]})

    monkeypatch.setattr(llm, "complete", fake_complete)
    with Session(connect(str(db))) as s:
        days = daily_topic_sentiment(s, "vpn")

    assert [d.day for d in days] == ["2024-01-03", "2024-01-04", "2024-01-05"]
    assert days[0].mean == 0.8 and days[0].posts == 1 and days[0].comments == 1
    assert days[1].posts == 0 and days[1].mean is None      # gap -> unscored, not 0.0
    assert days[2].mean == -0.6 and days[2].posts == 1
    # both the post titles and the comment body were handed to the model
    assert "works great" in seen["prompt"] and "keeps crashing" in seen["prompt"]
    assert "totally agree, love it" in seen["prompt"] and "comments:" in seen["prompt"]


def _vpn_topic_with_three_days(db, ts):
    """Three posts across the given UTC timestamps under topic 'vpn'."""
    titles = ["ancient gripe", "works great", "keeps crashing"]
    last = max(ts)
    with Session(connect(str(db))) as s:
        topic = Topic(name="vpn", keywords=json.dumps(["vpn"]),
                      subreddits=json.dumps(["vpn"]), last_tracked_at=last)
        s.add(topic)
        s.flush()
        posts = [Post(post_id=pid, author_username="x", subreddit_name="vpn",
                      created_utc=t, title=ti, score=5)
                 for pid, t, ti in zip("abc", ts, titles, strict=True)]
        upsert(s, posts)
        upsert(s, [TopicPost(topic_id=topic.id, post_id=p.post_id) for p in posts])
        s.commit()


def test_daily_topic_sentiment_days_cap_trims_window(db, monkeypatch):
    """``days_cap`` keeps only the most recent N calendar days of activity —
    both the returned series and the LLM prompt are bounded, and the dropped
    earlier day never reaches the model."""
    from datetime import UTC, datetime

    from redlens.summarize import daily_topic_sentiment

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    ts = [int(datetime(2024, 1, d, tzinfo=UTC).timestamp()) for d in (1, 9, 10)]
    _vpn_topic_with_three_days(db, ts)

    seen = {}

    def fake_complete(prompt, key, **kwargs):
        seen["prompt"] = prompt
        return json.dumps({"days": [{"day": "2024-01-09", "score": 20},
                                    {"day": "2024-01-10", "score": 40}]})

    monkeypatch.setattr(llm, "complete", fake_complete)
    with Session(connect(str(db))) as s:
        days = daily_topic_sentiment(s, "vpn", days_cap=3)

    # cutoff is 2024-01-08 (3 days ending at the latest activity): the 01-01 day
    # is dropped, the span is the surviving active days (01-09..01-10).
    assert [d.day for d in days] == ["2024-01-09", "2024-01-10"]
    assert days[-1].mean == 0.4
    assert "works great" in seen["prompt"] and "keeps crashing" in seen["prompt"]
    assert "ancient gripe" not in seen["prompt"]    # the dropped old post


def test_daily_topic_sentiment_days_cap_separate_cache_variant(db, monkeypatch):
    """A capped render must not be served the uncapped cached series (or vice
    versa): the cap is part of the cache key, so each is computed once."""
    from datetime import UTC, datetime

    from redlens.summarize import daily_topic_sentiment

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    ts = [int(datetime(2024, 1, d, tzinfo=UTC).timestamp()) for d in (1, 9, 10)]
    _vpn_topic_with_three_days(db, ts)

    monkeypatch.setattr(llm, "complete",
                        lambda *a, **k: json.dumps({"days": []}))
    with Session(connect(str(db))) as s:
        full = daily_topic_sentiment(s, "vpn")           # uncapped, cached as ""
        capped = daily_topic_sentiment(s, "vpn", days_cap=3)  # cached as "d3"
    assert full[0].day == "2024-01-01"          # uncapped spans from the old day
    assert capped[0].day == "2024-01-09"        # capped window is independent


def _vpn_topic_with_two_days(db, t1, t2):
    """Two posts on two different days under topic 'vpn'; returns nothing,
    just seeds the DB. Shared by the robustness tests below."""
    with Session(connect(str(db))) as s:
        topic = Topic(name="vpn", keywords=json.dumps(["vpn"]),
                      subreddits=json.dumps(["vpn"]), last_tracked_at=t2)
        s.add(topic)
        s.flush()
        posts = [
            Post(post_id="a", author_username="x", subreddit_name="vpn",
                 created_utc=t1, title="works great", score=5),
            Post(post_id="b", author_username="y", subreddit_name="vpn",
                 created_utc=t2, title="keeps crashing", score=9),
        ]
        upsert(s, posts)
        upsert(s, [TopicPost(topic_id=topic.id, post_id=p.post_id) for p in posts])
        s.commit()


def test_daily_topic_sentiment_robust_to_bad_scores(db, monkeypatch):
    """A day the model omits, or scores out-of-range / non-numeric / bool, must
    not be laundered into a confident 0.0 — it stays unscored (mean is None)."""
    from datetime import UTC, datetime

    from redlens.sentiment import _day_start
    from redlens.summarize import daily_topic_sentiment

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    t1 = int(datetime(2024, 1, 3, tzinfo=UTC).timestamp())
    t2 = int(datetime(2024, 1, 5, tzinfo=UTC).timestamp())
    d1, d2 = _day_start(t1), _day_start(t2)
    _vpn_topic_with_two_days(db, t1, t2)

    def fake_complete(prompt, key, **kwargs):
        # d1 scored 150 (out of range -> clamped to 1.0); d2 OMITTED entirely;
        # plus a bool score and an invented day the code must ignore.
        return json.dumps({"days": [
            {"day": d1, "score": 150},
            {"day": "2024-02-05", "score": 50},    # not an active day -> ignored
            {"day": d2, "score": True},            # bool -> rejected
        ]})

    monkeypatch.setattr(llm, "complete", fake_complete)
    with Session(connect(str(db))) as s:
        days = daily_topic_sentiment(s, "vpn")

    by = {d.day: d for d in days}
    assert by[d1].mean == 1.0                       # 150/100 clamped to 1.0
    assert by[d2].mean is None and by[d2].posts == 1  # omitted+bool -> unscored, NOT 0.0
    assert "2024-02-05" not in by                   # invented day dropped


def test_daily_topic_sentiment_includes_comment_only_days(db, monkeypatch):
    """A day with comments but no posts is shown to the model and charted, not
    silently dropped or forced neutral."""
    from datetime import UTC, datetime

    from redlens.sentiment import _day_start
    from redlens.summarize import daily_topic_sentiment

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    t_post = int(datetime(2024, 1, 3, tzinfo=UTC).timestamp())     # day 2024-01-03
    t_comment = int(datetime(2024, 1, 5, tzinfo=UTC).timestamp())  # day 2024-01-05
    dp, dc = _day_start(t_post), _day_start(t_comment)
    with Session(connect(str(db))) as s:
        topic = Topic(name="vpn", keywords=json.dumps(["vpn"]),
                      subreddits=json.dumps(["vpn"]), last_tracked_at=t_comment)
        s.add(topic)
        s.flush()
        upsert(s, [Post(post_id="a", author_username="x", subreddit_name="vpn",
                        created_utc=t_post, title="works great", score=5)])
        upsert(s, [TopicPost(topic_id=topic.id, post_id="a")])
        # comment lands on a LATER day than any post (comment-only day)
        upsert(s, [Comment(comment_id="c1", author_username="z",
                           subreddit_name="vpn", link_id="a", parent_id=None,
                           created_utc=t_comment, score=7, body="this broke for me")])
        s.commit()

    seen = {}

    def fake_complete(prompt, key, **kwargs):
        seen["prompt"] = prompt
        return json.dumps({"days": [{"day": dp, "score": 40},
                                    {"day": dc, "score": -80}]})

    monkeypatch.setattr(llm, "complete", fake_complete)
    with Session(connect(str(db))) as s:
        days = daily_topic_sentiment(s, "vpn")

    by = {d.day: d for d in days}
    assert dc in by                                  # comment-only day present
    assert by[dc].posts == 0 and by[dc].comments == 1
    assert by[dc].mean == -0.8                       # scored, not forced 0.0
    assert "this broke for me" in seen["prompt"]     # comment shown to the model


def test_label_themes_empty_needs_no_key():
    from redlens.summarize import label_themes
    assert label_themes("vpn", []) == []


def test_label_themes_no_key_raises(db):
    from redlens.errors import MissingKey
    from redlens.summarize import label_themes
    with pytest.raises(MissingKey):
        label_themes("vpn", [["a", "b"]])


def test_label_themes_aligns_and_falls_back(db, monkeypatch):
    """Labels align to themes by position; a blank or missing label falls back
    to the cluster's own keywords, so the result is always full-length."""
    from redlens.summarize import label_themes
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    themes = [["server", "connection", "slow"],
              ["price", "deal", "refund"],
              ["app", "update", "ui"]]
    seen = {}

    def fake_complete(prompt, key, **kwargs):
        seen["prompt"] = prompt
        return json.dumps({"labels": ["Connection Problems", ""]})  # 2nd blank, 3rd missing

    monkeypatch.setattr(llm, "complete", fake_complete)
    labels = label_themes("vpn", themes)
    assert labels == ["Connection Problems", "price, deal, refund", "app, update, ui"]
    assert "server, connection, slow" in seen["prompt"]   # clusters handed to model


def test_identify_brands_no_key_raises(topic_db):
    from redlens.errors import MissingKey
    from redlens.summarize import identify_brands
    with Session(connect(str(topic_db))) as s, pytest.raises(MissingKey):
        identify_brands(s, "climate")


def test_identify_brands_parses_and_samples(topic_db, monkeypatch):
    """Brands are parsed into name + aliases; blank names dropped, empty aliases
    fall back to the name, and the sample handed to the model is the archive."""
    from redlens.summarize import identify_brands
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen = {}

    def fake_complete(prompt, key, **kwargs):
        seen["prompt"] = prompt
        return json.dumps({"brands": [
            {"name": "Tesla", "aliases": ["tesla", "tsla"]},
            {"name": "BYD", "aliases": []},          # empty -> name as alias
            {"name": "", "aliases": ["x"]},          # blank name -> dropped
        ]})

    monkeypatch.setattr(llm, "complete", fake_complete)
    with Session(connect(str(topic_db))) as s:
        brands = identify_brands(s, "climate")

    assert [b.name for b in brands] == ["Tesla", "BYD"]
    assert brands[0].aliases == ["tesla", "tsla"]
    assert brands[1].aliases == ["BYD"]                  # empty -> [name]
    assert "carbon tax debate heats up" in seen["prompt"]  # archive sampled


def test_pin_brands_parses_dedupes_and_drops_blanks():
    """`page --brands` builds a fixed, key-free list: split on commas, strip,
    drop blanks, collapse case-insensitive dupes (first spelling wins). Each
    name is its own whole-word alias so symbol-edged names ('C++') count."""
    from redlens.summarize import pin_brands

    brands = pin_brands(" C++ , .NET, Rust , c++ , ,Rust")
    assert [b.name for b in brands] == ["C++", ".NET", "Rust"]   # deduped, ordered
    assert all(b.aliases == [b.name] for b in brands)            # self as alias
    assert pin_brands("") == [] and pin_brands("  , ,") == []    # nothing to pin


def test_identify_brands_passes_about_to_exclude_own_products(db, monkeypatch):
    """A topic's `about` is threaded into the brands prompt as the authoritative
    sense, so the recognizer can tell the subject's own products from competitors;
    the exclusion rule covers the subject's own offerings, not just its name."""
    from redlens.summarize import identify_brands
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with Session(connect(str(db))) as s:
        t = _seed_topic(s, name="anthropic")
        t.about = "the AI company Anthropic"
        s.add(t)
        s.commit()
    seen = {}

    def fake_complete(prompt, key, **kwargs):
        seen["prompt"] = prompt
        return json.dumps({"brands": []})

    monkeypatch.setattr(llm, "complete", fake_complete)
    with Session(connect(str(db))) as s:
        identify_brands(s, "anthropic")

    assert "authoritatively: the AI company Anthropic" in seen["prompt"]
    assert "own products" in seen["prompt"]   # the strengthened exclusion rule


def test_identify_brands_omits_about_line_when_blank(topic_db, monkeypatch):
    """With no `about`, the prompt carries no authoritative-sense line (the
    template slot collapses) — the keyless/sparse path stays clean."""
    from redlens.summarize import identify_brands
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen = {}

    def fake_complete(prompt, key, **kwargs):
        seen["prompt"] = prompt
        return json.dumps({"brands": []})

    monkeypatch.setattr(llm, "complete", fake_complete)
    with Session(connect(str(topic_db))) as s:
        identify_brands(s, "climate")

    assert "authoritatively:" not in seen["prompt"]


def test_extract_categories_parses_complaints(topic_db, monkeypatch):
    """Complaints/use-cases go through the same core, reading the 'categories'
    list and 'phrases' terms; empty phrases fall back to the name."""
    from redlens.summarize import extract_categories
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    seen = {}

    def fake_complete(prompt, key, **kwargs):
        seen["prompt"] = prompt
        return json.dumps({"categories": [
            {"name": "Pricing", "phrases": ["too expensive", "price hike"]},
            {"name": "Outages", "phrases": []},   # empty -> name as term
        ]})

    monkeypatch.setattr(llm, "complete", fake_complete)
    with Session(connect(str(topic_db))) as s:
        cats = extract_categories(s, "climate", "complaints")

    assert [c.name for c in cats] == ["Pricing", "Outages"]
    assert cats[0].terms == ["too expensive", "price hike"]
    assert cats[1].terms == ["Outages"]
    assert "PROBLEMS" in seen["prompt"]   # the complaints prompt was used

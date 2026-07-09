"""Render smoke + escaping tests for the topic page renderer.

Pins the page's stable structure at the library layer — render_all /
render_index / render_topic_page, slug decollision, the doc-cap refusal —
before shared HTML primitives are extracted. The CLI wiring of the same
features (page --all, --limit, --force) lives in test_topics.py.

Escaping is the load-bearing part: every user-controlled string on the
page (topic name, post title, author, comment body) must appear only
HTML-escaped, never as live markup.
"""
import time

import pytest
from sqlmodel import Session

from redlens import arctic
from redlens.db import connect, init_schema
from redlens.models import Comment, MentionGroup
from redlens.reporting.page import (
    PageResult,
    Sections,
    _unique_slug,
    render_all,
    render_index,
    render_topic_page,
    slug,
)
from redlens.topics import track_topic

NOW = int(time.time())

SCRIPT_PAYLOAD = "<script>alert(1)</script>"
IMG_PAYLOAD = "<img src=x onerror=alert(1)>"


def raw(pid, sub, *, ts=None, score=100, num_comments=2,
        title="a post about the topic", author="alice"):
    return {"id": pid, "subreddit": sub, "author": author,
            "created_utc": ts or NOW - 3600, "title": title,
            "score": score, "num_comments": num_comments}


def fake_query(data):
    def it(subreddit, query, after=None, before=None):
        yield from data.get(subreddit, [])
    return it


@pytest.fixture
def engine(tmp_path):
    e = connect(tmp_path / "t.db")
    init_schema(e)
    return e


def track(engine, monkeypatch, name, data, subreddits=None):
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_query(data))
    track_topic(engine, name, subreddits=subreddits or sorted(data))


# --- smoke: page + index structure ------------------------------------------

def test_topic_page_smoke_sections(engine, monkeypatch):
    track(engine, monkeypatch, "vinyl",
          {"vinyl": [raw("p1", "vinyl"), raw("p2", "vinyl", score=30)]})

    doc = render_topic_page(engine, "vinyl")

    assert doc.startswith("<!doctype html>")
    assert "<title>vinyl · redlens</title>" in doc
    assert "<h1>vinyl</h1>" in doc
    for marker in (
        "<h2>Posts per day</h2>",
        "<h2>By weekday &amp; hour (UTC)</h2>",   # punchcard section
        "<h2>Subreddits</h2>",
        "<h2>Most influential</h2>",
        "<h2>Themes</h2>",
        "<h2>Links</h2>",
        "<h2>Top posts</h2>",
    ):
        assert marker in doc
    assert "<svg" in doc                          # day chart + punchcard drawn
    assert "r/vinyl" in doc


def test_topic_page_mentions_section(engine, monkeypatch):
    track(engine, monkeypatch, "vinyl",
          {"vinyl": [raw("p1", "vinyl", title="switched from AcmeCorp last week")]})

    sections = Sections(brands=[MentionGroup(name="AcmeCorp", terms=["acmecorp"]),
                                MentionGroup(name="Ghost", terms=["ghostbrand"])])
    doc = render_topic_page(engine, "vinyl", sections)

    assert "<h2>Other brands mentioned</h2>" in doc
    assert "AcmeCorp" in doc
    assert "Ghost" not in doc                     # zero mentions: row dropped


def test_render_all_writes_pages_and_index(engine, tmp_path, monkeypatch):
    track(engine, monkeypatch, "vinyl", {"vinyl": [raw("p1", "vinyl")]})
    track(engine, monkeypatch, "ghost", {}, subreddits=["ghosttown"])  # no posts

    out = tmp_path / "reports"
    results = render_all(engine, out)

    by_name = {r.name: r for r in results}
    assert by_name["vinyl"] == PageResult("vinyl", "vinyl", 1, written=True)
    assert by_name["ghost"] == PageResult("ghost", "ghost", 0, written=False)
    assert (out / "vinyl.html").exists()
    assert not (out / "ghost.html").exists()
    index = (out / "index.html").read_text()
    assert "<h1>tracked topics</h1>" in index
    assert "<a href='vinyl.html'>vinyl</a>" in index
    assert "skipped (no matched posts yet)" in index and "ghost" in index


def test_render_index_empty_and_rows():
    assert "no tracked topics with matched posts yet" in render_index([])

    doc = render_index([
        PageResult("vinyl", "vinyl", 3, written=True),
        PageResult("mega", "mega", 9, written=False, too_large=True),
    ])
    assert "1 report" in doc
    assert "<a href='vinyl.html'>vinyl</a>" in doc and "3 posts" in doc
    assert "too large to render" in doc and "mega" in doc and "--limit" in doc


# --- slugs -------------------------------------------------------------------

def test_slug_and_unique_slug():
    assert slug("Dua Lipa") == "dua-lipa"
    assert slug("C++") == "c"
    assert slug("***") == "topic"                 # nothing sluggable: fallback
    used: set[str] = set()
    assert _unique_slug("C++", used) == "c"
    assert _unique_slug("C#", used) == "c-2"      # collision: suffixed
    assert _unique_slug("c", used) == "c-3"
    assert used == {"c", "c-2", "c-3"}


def test_render_all_decollides_slugs_in_results(engine, tmp_path, monkeypatch):
    # "C++" and "C#" both slug() to "c"; render_all must hand back distinct
    # slugs so the index links don't both point at the last writer.
    track(engine, monkeypatch, "C++", {"cpp": [raw("p1", "cpp")]})
    track(engine, monkeypatch, "C#", {"csharp": [raw("p2", "csharp")]})

    out = tmp_path / "reports"
    results = render_all(engine, out)

    assert sorted(r.slug for r in results) == ["c", "c-2"]
    assert all(r.written and (out / f"{r.slug}.html").exists() for r in results)


# --- doc-cap refusal ----------------------------------------------------------

def test_render_all_doc_limit_marks_too_large(engine, tmp_path, monkeypatch):
    track(engine, monkeypatch, "big", {"a": [raw("p1", "a"), raw("p2", "a")]})
    track(engine, monkeypatch, "small", {"b": [raw("p3", "b")]})

    out = tmp_path / "reports"
    results = render_all(engine, out, doc_limit=1)

    by_name = {r.name: r for r in results}
    assert by_name["big"].too_large and not by_name["big"].written
    assert by_name["small"].written and not by_name["small"].too_large
    assert (out / "small.html").exists()
    assert not (out / "big.html").exists()
    index = (out / "index.html").read_text()
    assert "too large to render" in index and "big" in index


# --- escaping: user-controlled strings must never reach the page as markup ----

def test_topic_name_escaped_on_page_index_and_filename(engine, tmp_path, monkeypatch):
    track(engine, monkeypatch, SCRIPT_PAYLOAD, {"vinyl": [raw("p1", "vinyl")]})

    doc = render_topic_page(engine, SCRIPT_PAYLOAD)
    assert SCRIPT_PAYLOAD not in doc                       # never raw
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in doc  # title + h1, escaped
    assert "<script" not in doc                            # no live script at all

    out = tmp_path / "reports"
    results = render_all(engine, out)
    assert results[0].slug == "script-alert-1-script"      # filename sanitized
    index = (out / "index.html").read_text()
    assert SCRIPT_PAYLOAD not in index
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in index


def test_post_title_author_and_comment_body_escaped(engine, monkeypatch):
    track(engine, monkeypatch, "vinyl", {"vinyl": [
        raw("p1", "vinyl", score=500, title=f"review {IMG_PAYLOAD} inside",
            author=f"mallory{IMG_PAYLOAD}"),
    ]})
    with Session(engine) as s:
        s.add(Comment(comment_id="c1", author_username="carol",
                      subreddit_name="vinyl", link_id="p1", parent_id=None,
                      created_utc=NOW - 100, score=50,
                      body=f"hot take {IMG_PAYLOAD} here"))
        s.commit()

    doc = render_topic_page(engine, "vinyl")

    assert "<img" not in doc                               # the page has no <img> at all
    assert "&lt;img src=x onerror=alert(1)&gt;" in doc     # payload only escaped
    # each sink made it onto the page (escaped): title, influential author, comment
    assert "review &lt;img" in doc
    assert "mallory&lt;img" in doc
    assert "hot take &lt;img" in doc


def test_skipped_topic_name_escaped_on_index(engine, tmp_path, monkeypatch):
    track(engine, monkeypatch, SCRIPT_PAYLOAD, {}, subreddits=["ghosttown"])

    out = tmp_path / "reports"
    render_all(engine, out)

    index = (out / "index.html").read_text()
    assert SCRIPT_PAYLOAD not in index
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in index

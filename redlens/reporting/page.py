"""Render a tracked topic into a standalone HTML page.

One self-contained file — minimal inline CSS, no JavaScript, no external
assets. The goal is a functional report (counts, themes, who/where/what),
not a polished site.
"""
from __future__ import annotations

import html
import re
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from redlens import constants
from redlens.errors import NotFound
from redlens.models import Comment, Post, Topic, TopicPost
from redlens.reporting import lda
from redlens.topics import get_topic, topic_comments

_WORD_RE = re.compile(r"[a-z0-9']+")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_STOPWORDS = frozenset(constants.data_lines("stopwords.txt"))

_CSS = """
body { font-family: system-ui, sans-serif; max-width: 800px; margin: 2rem auto;
       padding: 0 1rem; line-height: 1.4; }
h2 { margin-top: 1.8rem; font-size: 1.1rem; border-bottom: 1px solid #ccc; }
.bar { display: grid; grid-template-columns: 16rem 1fr 4rem; gap: .5rem;
       align-items: center; }
.bar .t { background: #eee; } .bar .f { background: #888; height: 1rem; }
.bar .v { text-align: right; }
.muted { color: #666; font-size: .85rem; }
table { border-collapse: collapse; width: 100%; }
td { border-bottom: 1px solid #eee; padding: .3rem .5rem; vertical-align: top; }
td.n { text-align: right; white-space: nowrap; }
"""


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def _ranked(counts: Counter[str], top: int) -> list[tuple[str, int]]:
    """most_common with a deterministic tie-break (count desc, then name)."""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:top]


def _bars(rows: list[tuple[str, int]], prefix: str = "") -> str:
    peak = max((n for _, n in rows), default=1)
    return "\n".join(
        f'<div class="bar"><div>{html.escape(prefix + label)}</div>'
        f'<div class="t"><div class="f" style="width:{100 * n / peak:.0f}%"></div></div>'
        f'<div class="v">{n:,}</div></div>'
        for label, n in rows
    )


def _themes(posts: list[Post], keywords: str, comments: list[Comment]) -> str:
    """LDA themes over post text and, when pulled, comment bodies — one row
    per topic with its share of the corpus. Comments sharpen the themes."""
    skip = _STOPWORDS | set(_WORD_RE.findall(keywords.lower()))

    def tokens(text: str) -> list[str]:
        return [w for w in _WORD_RE.findall(text.lower())
                if len(w) > 2 and w not in skip]

    docs = [t for p in posts if (t := tokens(f"{p.title or ''} {p.selftext or ''}"))]
    docs += [t for c in comments if (t := tokens(c.body or ""))]
    found = lda.topics(docs)
    if not found:
        return '<div class="muted">not enough text for topic modeling</div>'
    return "\n".join(
        f'<div>{share:.0%} · {html.escape(", ".join(words))}</div>'
        for share, words in found
    )


def _influential(posts: list[Post]) -> list[tuple[str, int]]:
    """Authors ranked by an engagement index: per post sqrt(score + 2x
    comments), summed, counting only posts past a floor. Comments weigh
    double (discussion is the influence); the sqrt favors sustained voices
    over one fluke; the floor keeps zero-engagement bot volume out."""
    index: dict[str, float] = {}
    count: Counter[str] = Counter()
    for p in posts:
        if p.author_username.lower() in constants.NON_AUTHORS:
            continue
        engagement = max(p.score, 0) + 2 * p.num_comments
        if engagement < constants.MIN_POST_ENGAGEMENT:
            continue
        index[p.author_username] = index.get(p.author_username, 0.0) + engagement**0.5
        count[p.author_username] += 1
    ranked = sorted(index.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    return [(f"{name} ({count[name]})", round(pts))
            for name, pts in ranked[:constants.TOP_AUTHORS]]


def _link_domains(posts: list[Post]) -> Counter[str]:
    counts: Counter[str] = Counter()
    for p in posts:
        host = urlparse(p.url or "").netloc.lower().removeprefix("www.")
        if host and not host.endswith(("reddit.com", "redd.it")):
            counts[host] += 1
    return counts


def _timing(posts: list[Post], comments: list[Comment]) -> str:
    """Two plain-text lines: the busiest day (with its top post) and the
    busiest weekday-hour. Replaces the old SVG charts — same signal, no
    chart code."""
    if not posts:
        return ""
    by_day: Counter[str] = Counter()
    by_slot: Counter[tuple[int, int]] = Counter()
    for p in posts:
        dt = datetime.fromtimestamp(p.created_utc, tz=UTC)
        by_day[dt.date().isoformat()] += 1
        by_slot[(dt.weekday(), dt.hour)] += 1
    for c in comments:
        dt = datetime.fromtimestamp(c.created_utc, tz=UTC)
        by_slot[(dt.weekday(), dt.hour)] += 1

    peak_day = max(by_day.items(), key=lambda kv: (kv[1], kv[0]))
    on_day = [p for p in posts
              if _date(p.created_utc) == peak_day[0]]
    top = max(on_day, key=lambda p: (p.score, p.post_id))
    (wd, hr), slot_n = max(by_slot.items(), key=lambda kv: (kv[1], kv[0]))
    return (
        f'<div>Busiest day: {peak_day[0]} ({peak_day[1]:,} posts) — top: '
        f'<a href="https://reddit.com/comments/{top.post_id}">'
        f'{html.escape((top.title or "(untitled)")[:90])}</a> '
        f'({top.score:,} pts)</div>'
        f'<div>Busiest time: {_WEEKDAYS[wd]} ~{hr:02d}:00 UTC '
        f'({slot_n:,} {"posts+comments" if comments else "posts"})</div>'
    )


def render_topic_page(engine: Engine, name: str) -> str:
    with Session(engine) as session:
        topic = get_topic(session, name)
        if topic is None:
            raise NotFound(f"topic {name!r} not tracked yet — run `redlens track` first")
        posts = list(session.exec(
            select(Post)
            .join(TopicPost, TopicPost.post_id == Post.post_id)  # type: ignore[arg-type]
            .where(TopicPost.topic_id == topic.id)
            # post_id tie-break keeps the rendered page byte-deterministic
            .order_by(Post.score.desc(), Post.post_id)  # type: ignore[attr-defined]
        ))
        comments = topic_comments(session, topic.name)
        return _render(topic, posts, comments)


def _render(topic: Topic, posts: list[Post], comments: list[Comment]) -> str:
    subs = Counter(p.subreddit_name for p in posts)
    net = len(topic.subreddit_list)
    keywords = ", ".join(topic.keyword_list)
    timestamps = [p.created_utc for p in posts]
    span = f"{_date(min(timestamps))} – {_date(max(timestamps))}" if timestamps else "—"
    comment_count = len(comments) or sum(p.num_comments for p in posts)
    comment_label = "comments analyzed" if comments else "comments on posts"

    top_rows = "\n".join(
        f"<tr><td><a href='https://reddit.com/comments/{p.post_id}'>"
        f"{html.escape((p.title or '(untitled)')[:120])}</a><br>"
        f"<span class='muted'>r/{html.escape(p.subreddit_name)} · "
        f"{_date(p.created_utc)}</span></td>"
        f"<td class='n'>{p.score:,} pts</td>"
        f"<td class='n'>{p.num_comments:,} comm.</td></tr>"
        for p in posts[:constants.TOP_POSTS]
    )
    domains = _bars(_ranked(_link_domains(posts), constants.TOP_DOMAINS))

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(topic.name)} · redlens</title><style>{_CSS}</style></head><body>
<h1>redlens · {html.escape(topic.name)}</h1>
<p class="muted">Public Reddit discussion matching {html.escape(keywords)!r},
last {topic.days} days ({span}) · data via arctic-shift</p>
<p>{len(posts):,} posts · {sum(p.score for p in posts):,} combined score ·
{comment_count:,} {comment_label} · {len(subs):,} of {net:,} subreddits had matches</p>
<h2>When</h2>
{_timing(posts, comments)}
<h2>Where (subreddits)</h2>
{_bars(_ranked(subs, constants.TOP_SUBREDDITS), prefix="r/")}
<h2>Who (most influential)</h2>
{_bars(_influential(posts), prefix="u/")}
<p class="muted">engagement index: sqrt(score + 2x comments) per post, summed</p>
<h2>Themes (LDA)</h2>
{_themes(posts, keywords, comments)}
<h2>Where links point</h2>
{domains or '<div class="muted">no external links</div>'}
<h2>Top posts</h2>
<table>{top_rows}</table>
<p class="muted">Generated by redlens — github.com/TheWeiHu/redlens</p>
</body></html>"""

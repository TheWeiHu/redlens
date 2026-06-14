"""Render a tracked topic into a standalone HTML page.

One self-contained file — minimal inline CSS (a single red accent), a
couple of lightweight inline-SVG plots with hover tooltips, no JavaScript
and no external assets. Functional and minimalist, not a polished site.
"""
from __future__ import annotations

import html
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
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
_A = constants.ACCENT

_CSS = f"""
body {{ font-family: system-ui, sans-serif; max-width: 820px; margin: 2rem auto;
       padding: 0 1rem; line-height: 1.4; color: #222; }}
h1, h2 {{ font-weight: 600; }}
h2 {{ margin-top: 2rem; font-size: 1rem; text-transform: uppercase;
     letter-spacing: .05em; color: {_A}; border-bottom: 2px solid {_A};
     padding-bottom: .2rem; }}
a {{ color: {_A}; text-decoration: none; }}
a:hover {{ text-decoration: underline; }}
.bar {{ display: grid; grid-template-columns: 15rem 1fr 4rem; gap: .5rem;
       align-items: center; margin: .15rem 0; }}
.bar .t {{ background: #f0e7e3; }} .bar .f {{ background: {_A}; height: .9rem; }}
.bar .v {{ text-align: right; color: #666; }}
.muted {{ color: #888; font-size: .85rem; }}
svg {{ width: 100%; height: auto; }}
svg rect, svg circle {{ fill: {_A}; }}
svg text {{ fill: #888; font-size: 9px; }}
table {{ border-collapse: collapse; width: 100%; }}
td {{ border-bottom: 1px solid #eee; padding: .3rem .5rem; vertical-align: top; }}
td.n {{ text-align: right; white-space: nowrap; color: #666; }}
"""


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def _trunc(text: str) -> str:
    return html.escape(text[:constants.TITLE_MAX]
                       + ("…" if len(text) > constants.TITLE_MAX else ""))


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


def _daily(posts: list[Post]) -> list[tuple[str, int]]:
    """Posts per calendar day, gaps zero-filled so the axis is honest."""
    if not posts:
        return []
    totals: Counter[str] = Counter(_date(p.created_utc) for p in posts)
    days = sorted(totals)
    out, cur = [], datetime.fromisoformat(days[0]).date()
    end = datetime.fromisoformat(days[-1]).date()
    while cur <= end:
        out.append((cur.isoformat(), totals.get(cur.isoformat(), 0)))
        cur += timedelta(days=1)
    return out


def _day_chart(series: list[tuple[str, int]]) -> str:
    """Inline-SVG column chart, one bar per day, hover for the count."""
    if not series:
        return ""
    w, h = 600.0, 60.0
    peak = max(v for _, v in series) or 1
    bw = w / len(series)
    bars = "".join(
        f'<rect x="{i * bw:.1f}" y="{h - v / peak * h:.1f}" '
        f'width="{max(bw - 0.5, 0.4):.1f}" height="{v / peak * h:.1f}">'
        f"<title>{d}: {v:,} posts</title></rect>"
        for i, (d, v) in enumerate(series)
    )
    return (f'<svg viewBox="0 0 {w:.0f} {h + 12:.0f}">{bars}'
            f'<text x="0" y="{h + 10:.0f}">{series[0][0]}</text>'
            f'<text x="{w:.0f}" y="{h + 10:.0f}" text-anchor="end">'
            f'{series[-1][0]}</text></svg>')


def _punchcard(posts: list[Post], comments: list[Comment]) -> str:
    """Inline-SVG weekday x hour grid (UTC); dot area ~ volume, hover for
    the count. Comments land at their real time when pulled."""
    counts: Counter[tuple[int, int]] = Counter()
    for p in posts:
        dt = datetime.fromtimestamp(p.created_utc, tz=UTC)
        counts[(dt.weekday(), dt.hour)] += 1 if comments else 1 + p.num_comments
    for c in comments:
        dt = datetime.fromtimestamp(c.created_utc, tz=UTC)
        counts[(dt.weekday(), dt.hour)] += 1
    if not counts:
        return ""
    peak = max(counts.values())
    cell, left, top = 22, 32, 4
    w, h = left + 24 * cell, top + 7 * cell + 14
    dots = "".join(
        f'<circle cx="{left + hr * cell + cell / 2:.0f}" '
        f'cy="{top + d * cell + cell / 2:.0f}" r="{1 + 8 * (n / peak) ** 0.5:.1f}">'
        f"<title>{_WEEKDAYS[d]} {hr:02d}:00 UTC — {n:,}</title></circle>"
        for (d, hr), n in sorted(counts.items())
    )
    labels = "".join(
        f'<text x="{left - 5}" y="{top + i * cell + cell / 2 + 3}" '
        f'text-anchor="end">{wd}</text>' for i, wd in enumerate(_WEEKDAYS)
    ) + "".join(
        f'<text x="{left + hr * cell + cell / 2}" y="{h - 3}" '
        f'text-anchor="middle">{hr:02d}</text>' for hr in (0, 6, 12, 18)
    )
    return f'<svg viewBox="0 0 {w} {h}">{labels}{dots}</svg>'


def _themes(posts: list[Post], keywords: str, comments: list[Comment]) -> str:
    """LDA themes over post text and, when pulled, comment bodies."""
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
    """Authors by engagement index: per post sqrt(score + COMMENT_WEIGHT x
    comments), summed, counting only posts past a floor — sustained voices
    over one fluke, and zero-engagement bot volume excluded."""
    index: dict[str, float] = {}
    count: Counter[str] = Counter()
    for p in posts:
        if p.author_username.lower() in constants.NON_AUTHORS:
            continue
        engagement = max(p.score, 0) + constants.COMMENT_WEIGHT * p.num_comments
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
    ts = [p.created_utc for p in posts]
    span = f"{_date(min(ts))} – {_date(max(ts))}" if ts else "—"
    n_comments = len(comments) or sum(p.num_comments for p in posts)
    comment_label = "comments analyzed" if comments else "comments on posts"

    top_rows = "\n".join(
        f"<tr><td><a href='https://reddit.com/comments/{p.post_id}'>"
        f"{_trunc(p.title or '(untitled)')}</a><br>"
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
<h1>{html.escape(topic.name)}</h1>
<p class="muted">{html.escape(keywords)!r} · last {topic.days} days · {span}</p>
<p>{len(posts):,} posts · {sum(p.score for p in posts):,} score ·
{n_comments:,} {comment_label} · {len(subs):,}/{net:,} subreddits matched</p>
<h2>Posts per day</h2>
{_day_chart(_daily(posts))}
<h2>By weekday &amp; hour (UTC)</h2>
{_punchcard(posts, comments)}
<h2>Subreddits</h2>
{_bars(_ranked(subs, constants.TOP_SUBREDDITS), prefix="r/")}
<h2>Most influential</h2>
{_bars(_influential(posts), prefix="u/")}
<h2>Themes</h2>
{_themes(posts, keywords, comments)}
<h2>Links</h2>
{domains or '<div class="muted">no external links</div>'}
<h2>Top posts</h2>
<table>{top_rows}</table>
</body></html>"""

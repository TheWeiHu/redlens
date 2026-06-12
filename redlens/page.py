"""Render a tracked topic into a standalone HTML page.

One self-contained file — inline CSS, no JavaScript, no external assets —
so the output can be mailed, hosted, or opened from disk as-is.
"""
from __future__ import annotations

import html
import re
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from redlens.errors import NotFound
from redlens.models import Post, Topic, TopicPost
from redlens.topics import get_topic

TOP_POSTS = 25
TOP_SUBREDDITS = 15
TOP_AUTHORS = 10
TOP_WORDS = 12
TOP_DOMAINS = 8

_WORD_RE = re.compile(r"[a-z0-9']+")
_NON_AUTHORS = {"[deleted]", "automoderator"}
_STOPWORDS = frozenset("""
a about after all also am an and any are as at be because been before being
but by can could did do does doing down for from get got had has have he her
here hers him his how i if in into is it its just like me more most my new
no not now of off on one only or other our out over own re s so some such
t than that the their them then there these they this those through to too
under up very was we were what when where which while who why will with
would you your yours
""".split())

_CSS = """
:root { --fg:#1a1a1a; --mut:#6b7280; --line:#e5e7eb; --accent:#d93a00; --hl:#fff7f3; }
body { font-family: -apple-system, system-ui, sans-serif; margin: 40px auto;
       max-width: 880px; padding: 0 20px; color: var(--fg); }
h1 { font-size: 26px; margin-bottom: 2px; }
h1 .red { color: var(--accent); }
h2 { font-size: 15px; text-transform: uppercase; letter-spacing: .08em;
     color: var(--mut); margin: 36px 0 10px; }
.sub { color: var(--mut); margin-bottom: 28px; }
.cards { display: flex; gap: 14px; flex-wrap: wrap; }
.card { border: 1px solid var(--line); border-radius: 10px; padding: 14px 20px; }
.card .n { font-size: 24px; font-weight: 700; }
.card .k { font-size: 12px; color: var(--mut); }
.bar { display: grid; grid-template-columns: 170px 1fr 60px; gap: 8px;
       align-items: center; font-size: 13px; margin: 3px 0; }
.bar .track { background: var(--hl); border-radius: 4px; height: 16px; }
.bar .fill { background: var(--accent); border-radius: 4px; height: 16px; }
.bar .v { text-align: right; font-variant-numeric: tabular-nums; color: var(--mut); }
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
td, th { padding: 7px 10px; border-bottom: 1px solid var(--line);
         text-align: left; vertical-align: top; }
td.n { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.meta { color: var(--mut); font-size: 12px; }
svg.chart { width: 100%; display: block; }
svg.chart rect { fill: var(--accent); }
svg.chart text { font-size: 9px; fill: var(--mut); }
footer { margin: 40px 0 12px; color: var(--mut); font-size: 12px; }
"""


def _bars(counts: list[tuple[str, int]], prefix: str = "") -> str:
    peak = max((n for _, n in counts), default=1)
    rows = []
    for label, n in counts:
        rows.append(
            f'<div class="bar"><div>{html.escape(prefix + label)}</div>'
            f'<div class="track">'
            f'<div class="fill" style="width:{100 * n / peak:.1f}%"></div></div>'
            f'<div class="v">{n:,}</div></div>'
        )
    return "\n".join(rows)


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%b %d, %Y")


def _ranked(counts: Counter[str], top: int) -> list[tuple[str, int]]:
    """most_common with a deterministic tie-break (count desc, then name)."""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:top]


def _title_words(posts: list[Post], query: str) -> Counter[str]:
    """Word frequency across titles — fixed stopword list, and the query's
    own terms excluded (they trivially dominate)."""
    skip = _STOPWORDS | set(_WORD_RE.findall(query.lower()))
    counts: Counter[str] = Counter()
    for p in posts:
        counts.update(
            w for w in _WORD_RE.findall((p.title or "").lower())
            if len(w) > 2 and w not in skip
        )
    return counts


def _link_domains(posts: list[Post]) -> Counter[str]:
    """External domains posts link to; reddit self/media links excluded."""
    counts: Counter[str] = Counter()
    for p in posts:
        host = urlparse(p.url or "").netloc.lower().removeprefix("www.")
        if host and not host.endswith(("reddit.com", "redd.it")):
            counts[host] += 1
    return counts


def _spike_note(posts: list[Post], series: list[tuple[str, int]]) -> str:
    """One line explaining the busiest day: its top post, linked."""
    if not series:
        return ""
    peak_day = max(series, key=lambda dv: (dv[1], dv[0]))[0]
    on_day = [
        p for p in posts
        if datetime.fromtimestamp(p.created_utc, tz=UTC).date().isoformat() == peak_day
    ]
    if not on_day:
        return ""
    top = max(on_day, key=lambda p: (p.score, p.post_id))
    return (
        f'<div class="meta">busiest day, {peak_day} — top post: '
        f'<a href="https://reddit.com/comments/{top.post_id}">'
        f"{html.escape((top.title or '(untitled)')[:90])}</a> "
        f"({top.score:,} pts, r/{html.escape(top.subreddit_name)})</div>"
    )


def _daily(posts: list[Post], value: Callable[[Post], int]) -> list[tuple[str, int]]:
    """(ISO date, summed value) per calendar day, gaps filled with zeros so
    the time axis is honest."""
    if not posts:
        return []
    totals: dict[str, int] = {}
    for p in posts:
        day = datetime.fromtimestamp(p.created_utc, tz=UTC).date()
        totals[day.isoformat()] = totals.get(day.isoformat(), 0) + value(p)
    days = sorted(totals)
    first = datetime.fromisoformat(days[0]).date()
    last = datetime.fromisoformat(days[-1]).date()
    series = []
    cur = first
    while cur <= last:
        series.append((cur.isoformat(), totals.get(cur.isoformat(), 0)))
        cur += timedelta(days=1)
    return series


def _day_chart(series: list[tuple[str, int]], unit: str) -> str:
    """A simple inline-SVG column chart: one bar per day, no JavaScript."""
    if not series:
        return '<div class="meta">no data</div>'
    width, height, axis = 600.0, 64.0, 12.0
    peak = max(v for _, v in series) or 1
    peak_day = max(series, key=lambda dv: dv[1])[0]
    bar_w = width / len(series)
    bars = "".join(
        f'<rect x="{i * bar_w:.2f}" y="{height - v / peak * height:.2f}" '
        f'width="{max(bar_w - 0.6, 0.4):.2f}" height="{v / peak * height:.2f}">'
        f"<title>{day}: {v:,} {unit}</title></rect>"
        for i, (day, v) in enumerate(series)
    )
    return (
        f'<svg class="chart" viewBox="0 0 {width:.0f} {height + axis:.0f}" '
        f'role="img" aria-label="{unit} per day">{bars}'
        f'<text x="0" y="{height + axis - 2:.0f}">{series[0][0]}</text>'
        f'<text x="{width:.0f}" y="{height + axis - 2:.0f}" '
        f'text-anchor="end">{series[-1][0]}</text></svg>'
        f'<div class="meta">peak: {peak:,} {unit} on {peak_day}</div>'
    )


def render_topic_page(engine: Engine, name: str) -> str:
    with Session(engine) as session:
        topic = get_topic(session, name)
        if topic is None:
            raise NotFound(f"topic {name!r} not tracked yet — run `redlens track` first")
        posts = list(session.exec(
            select(Post)
            .join(TopicPost, TopicPost.post_id == Post.post_id)  # type: ignore[arg-type]
            .where(TopicPost.topic_name == topic.name)
            # post_id tie-break keeps the rendered page byte-deterministic
            .order_by(Post.score.desc(), Post.post_id)  # type: ignore[attr-defined]
        ))
        return _render(topic, posts)


def _render(topic: Topic, posts: list[Post]) -> str:
    subs = Counter(p.subreddit_name for p in posts)
    authors = Counter(
        p.author_username for p in posts
        if p.author_username.lower() not in _NON_AUTHORS
    )
    timestamps = [p.created_utc for p in posts]
    span = (
        f"{_date(min(timestamps))} – {_date(max(timestamps))}" if timestamps else "—"
    )

    top_rows = "\n".join(
        f"<tr><td><a href='https://reddit.com/comments/{p.post_id}'>"
        f"{html.escape((p.title or '(untitled)')[:120])}</a>"
        f"<div class='meta'>r/{html.escape(p.subreddit_name)} · "
        f"{_date(p.created_utc)}</div></td>"
        f"<td class='n'>{p.score:,} pts</td>"
        f"<td class='n'>{p.num_comments:,} comments</td></tr>"
        for p in posts[:TOP_POSTS]
    )

    name = html.escape(topic.name)
    net = len(topic.subreddit_list)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} · redlens</title><style>{_CSS}</style></head><body>
<h1><span class="red">red</span>lens · {name}</h1>
<div class="sub">Public Reddit discussion matching {html.escape(topic.query)!r},
last {topic.days} days ({span}) · data via arctic-shift</div>
<div class="cards">
  <div class="card"><div class="n">{len(posts):,}</div><div class="k">posts</div></div>
  <div class="card"><div class="n">{sum(p.score for p in posts):,}</div>
    <div class="k">combined score</div></div>
  <div class="card"><div class="n">{sum(p.num_comments for p in posts):,}</div>
    <div class="k">comments on them</div></div>
  <div class="card"><div class="n">{len(subs):,} of {net:,}</div>
    <div class="k">subreddits had matches</div></div>
</div>
<h2>Posts per day</h2>
{_day_chart(_daily(posts, lambda p: 1), "posts")}
{_spike_note(posts, _daily(posts, lambda p: 1))}
<h2>Score per day</h2>
{_day_chart(_daily(posts, lambda p: p.score), "points")}
<h2>Where the conversation happens</h2>
{_bars(_ranked(subs, TOP_SUBREDDITS), prefix="r/")}
<div class="meta" style="margin-top:6px">searched {net:,} subreddits;
{net - len(subs):,} had no matching posts in the window</div>
<h2>Who's talking</h2>
{_bars(_ranked(authors, TOP_AUTHORS), prefix="u/")}
<h2>What the titles say</h2>
{_bars(_ranked(_title_words(posts, topic.query), TOP_WORDS))}
<h2>Where links point</h2>
{_bars(_ranked(_link_domains(posts), TOP_DOMAINS)) or
 '<div class="meta">no external links</div>'}
<h2>Top posts</h2>
<table>{top_rows}</table>
<footer>Generated by <a href="https://github.com/TheWeiHu/redlens">redlens</a> ·
the open-source intelligent lens on public discussion</footer>
</body></html>"""

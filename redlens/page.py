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

from redlens import lda
from redlens.errors import NotFound
from redlens.models import Post, Topic, TopicPost
from redlens.topics import get_topic

TOP_POSTS = 25
TOP_SUBREDDITS = 15
TOP_AUTHORS = 10
TOP_DOMAINS = 8
MIN_POST_ENGAGEMENT = 5  # score + 2x comments below this = post didn't land

_WORD_RE = re.compile(r"[a-z0-9']+")
_NON_AUTHORS = {"[deleted]", "automoderator"}
# Conversational-filler stopwords, in the spirit of rhiever/reddit-analysis:
# aggressive on chatter ("really", "think", "anyone") so topic words surface,
# hands-off on anything that could be domain signal ("mg", "dose", "insurance").
_STOPWORDS = frozenset((
    "a", "able", "about", "actually", "after", "again", "ago", "all",
    "also", "am", "amp", "com", "gt", "http", "https",
    "que", "three", "two", "www", "x200b",
    "an", "and", "any", "anybody", "anyone", "anything", "are", "aren't",
    "around", "as", "at", "back", "bad", "be", "because", "been", "before",
    "being", "best", "better", "bit", "but", "by", "came", "can", "can't",
    "cant", "come", "could", "couldn't", "couldnt", "day", "days", "did",
    "didn't", "didnt", "do", "does", "doesn't", "doesnt", "doing", "don't",
    "dont", "down", "edit", "else", "even", "ever", "everyone", "everything",
    "feel", "feeling", "feels", "felt", "few", "first", "for", "from", "get",
    "gets", "getting", "go", "going", "good", "got", "had", "has", "hasn't",
    "have", "haven't", "he", "he's", "help", "her", "here", "hers", "him",
    "his", "how", "however", "i", "i'd", "i'll", "i'm", "i've", "id", "if",
    "ill", "im", "in", "into", "is", "isn't", "isnt", "it", "it's", "its",
    "ive", "just", "know", "last", "let's", "like", "long", "look",
    "looking", "looks", "lot", "made", "make", "makes", "making", "many",
    "maybe", "me", "month", "months", "more", "most", "much", "my", "need",
    "never", "new", "next", "no", "not", "nothing", "now", "of", "off",
    "on", "one", "only", "or", "other", "our", "out", "over", "own",
    "people", "post", "probably", "question", "re", "really", "right", "s",
    "said", "say", "says", "see", "seen", "she", "she's", "should", "since",
    "so", "some", "someone", "something", "started", "still", "such",
    "sure", "t", "take", "taking", "than", "thank", "thanks", "that",
    "that's", "thats", "the", "their", "them", "then", "there", "there's",
    "these", "they", "they're", "theyre", "thing", "things", "think",
    "this", "those", "though", "through", "to", "today", "too", "took",
    "tried", "try", "trying", "under", "up", "use", "used", "using",
    "very", "want", "was", "wasn't", "way", "we", "we're", "week", "weeks",
    "were", "what", "what's", "when", "where", "which", "while", "who",
    "why", "will", "with", "won't", "wont", "would", "wouldn't", "wouldnt",
    "yeah", "year", "years", "yes", "you", "you're", "your", "youre",
    "yours",
))

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
svg.punch { max-width: 640px; }
svg.punch circle { fill: var(--accent); opacity: .85; }
.theme { font-size: 14px; margin: 7px 0; }
.theme span { display: inline-block; width: 48px; color: var(--mut);
              font-variant-numeric: tabular-nums; }
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


def _themes(posts: list[Post], query: str) -> str:
    """LDA themes over titles + selftext — one row per topic, weighted by
    its share of the corpus."""
    skip = _STOPWORDS | set(_WORD_RE.findall(query.lower()))
    docs = []
    for p in posts:
        tokens = [
            w for w in _WORD_RE.findall(f"{p.title or ''} {p.selftext or ''}".lower())
            if len(w) > 2 and w not in skip
        ]
        if tokens:
            docs.append(tokens)
    found = lda.topics(docs)
    if not found:
        return '<div class="meta">not enough text for topic modeling</div>'
    return "\n".join(
        f'<div class="theme"><span>{share:.0%}</span>'
        f"{html.escape(', '.join(words))}</div>"
        for share, words in found
    )


def _influential_users(posts: list[Post], top: int) -> list[tuple[str, int]]:
    """Authors ranked by an engagement index: per post, sqrt(score + 2x
    comments), summed — but only posts clearing a minimum engagement bar
    count at all. Comments weigh double (a reply is a stronger act than a
    vote — in support communities discussion, not karma, is the
    influence); the square root means sustained voices beat one viral
    fluke; and the floor means firehosing ignored posts earns nothing."""
    index: dict[str, float] = {}
    count: Counter[str] = Counter()
    reach: dict[str, set[str]] = {}
    for p in posts:
        author = p.author_username
        if author.lower() in _NON_AUTHORS:
            continue
        engagement = max(p.score, 0) + 2 * p.num_comments
        if engagement < MIN_POST_ENGAGEMENT:
            continue
        index[author] = index.get(author, 0.0) + engagement**0.5
        count[author] += 1
        reach.setdefault(author, set()).add(p.subreddit_name)
    ranked = sorted(index.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:top]
    return [
        (f"{name} · {count[name]} post{'s' if count[name] != 1 else ''}"
         + (f" in {len(reach[name])} subs" if len(reach[name]) > 1 else ""),
         round(points))
        for name, points in ranked
    ]


def _punchcard(posts: list[Post]) -> str:
    """GitHub-style day-of-week x hour-of-day grid (UTC), dot area ~ volume."""
    counts: Counter[tuple[int, int]] = Counter()
    for p in posts:
        dt = datetime.fromtimestamp(p.created_utc, tz=UTC)
        counts[(dt.weekday(), dt.hour)] += 1
    if not counts:
        return '<div class="meta">no data</div>'
    peak = max(counts.values())
    cell, left, top = 24, 40, 6
    width, height = left + 24 * cell, top + 7 * cell + 18
    dots = "".join(
        f'<circle cx="{left + hr * cell + cell / 2:.0f}" '
        f'cy="{top + day * cell + cell / 2:.0f}" '
        f'r="{1.5 + 8.5 * (c / peak) ** 0.5:.1f}">'
        f"<title>{_WEEKDAYS[day]} {hr:02d}:00 UTC — {c:,} posts</title></circle>"
        for (day, hr), c in sorted(counts.items())
    )
    day_labels = "".join(
        f'<text x="{left - 6}" y="{top + i * cell + cell / 2 + 3}" '
        f'text-anchor="end">{d}</text>'
        for i, d in enumerate(_WEEKDAYS)
    )
    hour_labels = "".join(
        f'<text x="{left + h * cell + cell / 2}" y="{height - 5}" '
        f'text-anchor="middle">{h:02d}h</text>'
        for h in (0, 4, 8, 12, 16, 20)
    )
    return (
        f'<svg class="chart punch" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="posts by weekday and hour">{day_labels}{hour_labels}{dots}'
        f"</svg>"
        f'<div class="meta">post times in UTC — dot size is volume; '
        f"peak {peak:,} posts in one weekday-hour</div>"
    )


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


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _render(topic: Topic, posts: list[Post]) -> str:
    subs = Counter(p.subreddit_name for p in posts)
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
<h2>When the conversation happens</h2>
{_punchcard(posts)}
<h2>Where the conversation happens</h2>
{_bars(_ranked(subs, TOP_SUBREDDITS), prefix="r/")}
<div class="meta" style="margin-top:6px">searched {net:,} subreddits;
{net - len(subs):,} had no matching posts in the window</div>
<h2>Most influential users</h2>
{_bars(_influential_users(posts, TOP_AUTHORS), prefix="u/")}
<div class="meta" style="margin-top:6px">engagement index:
&radic;(score + 2&times;comments) per post, summed — sustained voices
outrank one-hit virality, discussion counts double</div>
<h2>Themes</h2>
{_themes(posts, topic.query)}
<div class="meta" style="margin-top:6px">topics via LDA (collapsed Gibbs
sampling) over titles and post text; % is each theme's share</div>
<h2>Where links point</h2>
{_bars(_ranked(_link_domains(posts), TOP_DOMAINS)) or
 '<div class="meta">no external links</div>'}
<h2>Top posts</h2>
<table>{top_rows}</table>
<footer>Generated by <a href="https://github.com/TheWeiHu/redlens">redlens</a> ·
the open-source intelligent lens on public discussion</footer>
</body></html>"""

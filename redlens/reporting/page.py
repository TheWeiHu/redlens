"""Render a tracked topic into a standalone HTML page.

One self-contained file — minimal inline CSS (a single red accent), a
couple of lightweight inline-SVG plots with hover tooltips, no JavaScript
and no external assets.

Every aggregate is explorable: the subreddit, author, and link-domain
bars are ``<details>`` disclosures (no JS) that expand to the underlying
Reddit posts behind the number, each linking straight to the thread — so
any claim on the page drills down to the evidence that makes it up.
"""
from __future__ import annotations

import html
import re
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from redlens import constants
from redlens.errors import NotFound
from redlens.models import Comment, Post, Topic, TopicPost, TopicSummary
from redlens.reporting import lda
from redlens.sentiment import WeekSentiment, weekly_sentiment
from redlens.topics import get_topic, list_topics, topic_comments

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
details > summary {{ list-style: none; cursor: pointer; }}
details > summary::-webkit-details-marker {{ display: none; }}
details[open] > summary .bar {{ background: #faf3f0; }}
details ul {{ margin: .2rem 0 .6rem 1rem; padding: 0; }}
details li {{ list-style: none; font-size: .9rem; margin: .1rem 0; }}
.muted {{ color: #888; font-size: .85rem; }}
svg {{ width: 100%; height: auto; }}
svg rect, svg circle {{ fill: {_A}; }}
svg rect.pos {{ fill: #2e8b57; }} svg rect.neg {{ fill: {_A}; }}
svg text {{ fill: #888; font-size: 9px; }}
table {{ border-collapse: collapse; width: 100%; }}
td {{ border-bottom: 1px solid #eee; padding: .3rem .5rem; vertical-align: top; }}
td.n {{ text-align: right; white-space: nowrap; color: #666; }}
.summary {{ background: #faf3f0; border-left: 3px solid {_A}; padding: .6rem .9rem;
           border-radius: 0 4px 4px 0; }}
.summary ul.themes {{ margin: .4rem 0 .4rem 1.1rem; padding: 0; }}
.summary ul.themes li {{ list-style: disc; font-size: .95rem; margin: .2rem 0; }}
.summary .lbl {{ color: {_A}; font-weight: 600; }}
.summary p {{ margin: .4rem 0; }}
"""


def _date(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def _trunc(text: str) -> str:
    return html.escape(text[:constants.TITLE_MAX]
                       + ("…" if len(text) > constants.TITLE_MAX else ""))


def _ranked(counts: Counter[str], top: int) -> list[tuple[str, int]]:
    """most_common with a deterministic tie-break (count desc, then name)."""
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))[:top]


def _bar(label: str, n: int, peak: int, prefix: str = "") -> str:
    return (f'<div class="bar"><div>{html.escape(prefix + label)}</div>'
            f'<div class="t"><div class="f" style="width:{100 * n / peak:.0f}%">'
            f'</div></div><div class="v">{n:,}</div></div>')


def _post_links(posts: list[Post]) -> str:
    """A clickable list of the underlying posts — every claim drills down to
    the real Reddit threads behind it."""
    return "<ul>" + "".join(
        f"<li><a href='https://reddit.com/comments/{p.post_id}'>"
        f"{_trunc(p.title or '(untitled)')}</a> "
        f"<span class='muted'>{p.score:,} pts · {p.num_comments:,} comm. · "
        f"r/{html.escape(p.subreddit_name)} · {_date(p.created_utc)}</span></li>"
        for p in posts[:constants.DRILL_POSTS]
    ) + "</ul>"


def _drill(rows: list[tuple[str, int]], groups: dict[str, list[Post]],
           prefix: str = "") -> str:
    """Bar rows that expand (no JS, via <details>) to the posts behind them,
    best first."""
    peak = max((n for _, n in rows), default=1)
    out = []
    for label, n in rows:
        posts = sorted(groups.get(label, []),
                       key=lambda p: (-p.score, p.post_id))
        out.append(
            f"<details><summary>{_bar(label, n, peak, prefix)}</summary>"
            f"{_post_links(posts)}</details>"
        )
    return "\n".join(out)


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


def _sentiment_chart(series: list[WeekSentiment]) -> str:
    """Inline-SVG diverging bar chart of weekly sentiment in [-1, 1]: bars rise
    (green) above a neutral baseline for positive weeks and fall (red) below for
    negative ones, height ~ magnitude; hover for the week's score and post
    count. Returns '' when there's no signal to show (every week neutral) — e.g.
    the lexicon fallback found no sentiment words."""
    if not series or all(w.mean == 0.0 for w in series):
        return ""
    width, half, pad = 600.0, 38.0, 14.0
    center, total_h = half, half * 2
    bw = width / len(series)
    bars = []
    for i, wk in enumerate(series):
        bh = abs(wk.mean) * half
        y = center - bh if wk.mean >= 0 else center
        cls = "pos" if wk.mean >= 0 else "neg"
        sign = "+" if wk.mean >= 0 else ""
        bars.append(
            f'<rect class="{cls}" x="{i * bw:.1f}" y="{y:.1f}" '
            f'width="{max(bw - 0.5, 0.4):.1f}" height="{max(bh, 0.6):.1f}">'
            f"<title>{wk.week}: {sign}{wk.mean:.2f} · "
            f"{wk.total:,} posts</title></rect>"
        )
    baseline = (f'<line x1="0" y1="{center:.1f}" x2="{width:.0f}" '
                f'y2="{center:.1f}" stroke="#ccc" stroke-width="0.5"/>')
    labels = (f'<text x="0" y="{total_h + pad - 2:.0f}">{series[0].week}</text>'
              f'<text x="{width:.0f}" y="{total_h + pad - 2:.0f}" '
              f'text-anchor="end">{series[-1].week}</text>')
    return (f'<svg viewBox="0 0 {width:.0f} {total_h + pad:.0f}">'
            f'{baseline}{"".join(bars)}{labels}</svg>')


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


def _influential(posts: list[Post]) -> list[tuple[str, int, str]]:
    """Authors by engagement index: per post sqrt(score + COMMENT_WEIGHT x
    comments), summed, counting only posts past a floor — sustained voices
    over one fluke, and zero-engagement bot volume excluded. Returns
    (display label, points, raw username) so the page can drill to their posts."""
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
    return [(f"{name} ({count[name]})", round(pts), name)
            for name, pts in ranked[:constants.TOP_AUTHORS]]


def _host(post: Post) -> str:
    """The external domain a post links to, or '' for self/Reddit posts."""
    host = urlparse(post.url or "").netloc.lower().removeprefix("www.")
    return "" if host.endswith(("reddit.com", "redd.it")) else host


def _link_domains(posts: list[Post]) -> Counter[str]:
    return Counter(h for p in posts if (h := _host(p)))


def _by(posts: list[Post], key: Callable[[Post], str]) -> dict[str, list[Post]]:
    """Group posts under a key function, dropping empty keys."""
    groups: dict[str, list[Post]] = defaultdict(list)
    for p in posts:
        if k := key(p):
            groups[k].append(p)
    return groups


def _summary_section(s: TopicSummary) -> str:
    """The LLM narrative from ``summarize --topic`` rendered inline — overview,
    themes, sentiment, viewpoints. Everything is escaped: model output is
    untrusted text. Returns '' when the summary carried no prose."""
    parts: list[str] = []
    if s.overview:
        parts.append(f"<p>{html.escape(s.overview)}</p>")
    if s.themes:
        items = "".join(
            f"<li><strong>{html.escape(t.title)}</strong>"
            + (f" — {html.escape(t.summary)}" if t.summary else "")
            + "</li>"
            for t in s.themes
        )
        parts.append(f'<ul class="themes">{items}</ul>')
    for label, body in (("Sentiment", s.sentiment), ("Viewpoints", s.viewpoints)):
        if body:
            parts.append(
                f'<p><span class="lbl">{label}</span> {html.escape(body)}</p>')
    if not parts:
        return ""
    note = (f'<p class="muted">AI summary · {html.escape(s.model)} · '
            f'{html.escape(s.depth)} depth</p>')
    return (f"<h2>AI summary</h2>\n{note}\n"
            f'<div class="summary">{"".join(parts)}</div>')


def slug(name: str) -> str:
    """A filesystem-safe, lowercase slug for a topic name (``Dua Lipa`` →
    ``dua-lipa``); the per-topic page filename derives from it."""
    return "-".join(re.findall(r"[a-z0-9]+", name.lower())) or "topic"


@dataclass(frozen=True)
class PageResult:
    """One topic's outcome from ``render_all``: ``written`` is False when the
    topic had zero matched posts and was skipped (noted on the index)."""
    name: str
    slug: str
    matched: int
    written: bool


def _unique_slug(name: str, used: set[str]) -> str:
    """``slug(name)`` made collision-free within ``used`` by appending ``-2``,
    ``-3``, … on conflict. Mutates ``used`` with the slug it hands back."""
    base = slug(name)
    s = base
    n = 1
    while s in used:
        n += 1
        s = f"{base}-{n}"
    used.add(s)
    return s


def render_all(engine: Engine, out_dir: Path,
               summarize: Callable[[str], TopicSummary | None] | None = None,
               sentiment: Callable[[str], list[WeekSentiment] | None] | None = None,
               ) -> list[PageResult]:
    """Render every tracked topic into ``out_dir`` plus an ``index.html`` that
    links them. Topics with zero matched posts are skipped (and noted on the
    index) since there is nothing to chart. Returns one result per topic,
    in the same most-recently-tracked-first order as the index.

    ``summarize`` is an optional per-topic AI-narrative provider (one LLM call
    each). ``sentiment`` is an optional per-topic LLM sentiment-trend provider;
    without it each page falls back to the offline lexicon. When given, every
    rendered page gets that section."""
    out_dir.mkdir(parents=True, exist_ok=True)
    with Session(engine) as session:
        listings = list_topics(session)
    results: list[PageResult] = []
    used: set[str] = set()
    for listing in listings:
        # slug() is lossy (keeps only [a-z0-9]), so distinct names can collide
        # ("C#"/"C++" -> "c"). Suffix dupes so each topic gets its own file and
        # the index links don't all point at the last writer.
        s = _unique_slug(listing.name, used)
        if listing.matched_posts == 0:
            results.append(PageResult(listing.name, s, 0, written=False))
            continue
        summary = summarize(listing.name) if summarize else None
        weeks = sentiment(listing.name) if sentiment else None
        doc = render_topic_page(engine, listing.name, summary=summary,
                                sentiment_weeks=weeks)
        (out_dir / f"{s}.html").write_text(doc, encoding="utf-8")
        results.append(
            PageResult(listing.name, s, listing.matched_posts, written=True))
    (out_dir / "index.html").write_text(
        render_index(results), encoding="utf-8")
    return results


def render_index(results: list[PageResult]) -> str:
    """A small overview page linking each rendered topic's report, with a note
    listing any topics skipped for having no matched posts."""
    written = [r for r in results if r.written]
    skipped = [r for r in results if not r.written]
    rows = "\n".join(
        f"<tr><td><a href='{r.slug}.html'>{html.escape(r.name)}</a></td>"
        f"<td class='n'>{r.matched:,} posts</td></tr>"
        for r in written
    )
    body = (f"<table>{rows}</table>" if written
            else '<p class="muted">no tracked topics with matched posts yet</p>')
    skip_note = ""
    if skipped:
        names = ", ".join(html.escape(r.name) for r in skipped)
        skip_note = (f'<p class="muted">skipped (no matched posts yet): '
                     f'{names}</p>')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>tracked topics · redlens</title><style>{_CSS}</style></head><body>
<h1>tracked topics</h1>
<p class="muted">{len(written):,} report{"" if len(written) == 1 else "s"}</p>
{body}
{skip_note}
</body></html>"""


def render_topic_page(engine: Engine, name: str,
                      summary: TopicSummary | None = None,
                      sentiment_weeks: list[WeekSentiment] | None = None) -> str:
    """Render one topic's page. ``summary`` (from ``summarize --topic``) is
    optional — when given, an AI-narrative section is added. ``sentiment_weeks``
    (from ``weekly_topic_sentiment``) is the LLM-scored sentiment trend; when
    omitted the page falls back to the offline lexicon, so it stays fully
    keyless without either."""
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
        is_llm = sentiment_weeks is not None
        if sentiment_weeks is None:
            sentiment_weeks = weekly_sentiment(
                (p.created_utc, f"{p.title or ''} {p.selftext or ''}")
                for p in posts)
        section = _sentiment_section(sentiment_weeks, is_llm=is_llm)
        return _render(topic, posts, comments, summary, section)


def _sentiment_section(series: list[WeekSentiment], *, is_llm: bool) -> str:
    """The 'Sentiment over time' heading + caption + chart, or '' when there's
    nothing to chart. ``is_llm`` only changes the caption's method note."""
    svg = _sentiment_chart(series)
    if not svg:
        return ""
    src = "LLM-scored" if is_llm else "offline lexicon (rough — sarcasm/negation can fool it)"
    return ('<h2>Sentiment over time</h2>\n'
            '<p class="muted">weekly sentiment, −1 (negative) to +1 (positive)'
            f' · {src}</p>\n{svg}')


def _render(topic: Topic, posts: list[Post], comments: list[Comment],
            summary: TopicSummary | None = None,
            sentiment_section: str = "") -> str:
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
    sub_groups = _by(posts, lambda p: p.subreddit_name)
    sub_rows = _ranked(subs, constants.TOP_SUBREDDITS)

    infl = _influential(posts)
    author_posts = _by(posts, lambda p: p.author_username)
    infl_rows = [(label, pts) for label, pts, _ in infl]
    infl_groups = {label: author_posts.get(author, [])
                   for label, _, author in infl}

    domain_groups = _by(posts, _host)
    domain_rows = _ranked(_link_domains(posts), constants.TOP_DOMAINS)
    domains = _drill(domain_rows, domain_groups) if domain_rows else ""

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(topic.name)} · redlens</title><style>{_CSS}</style></head><body>
<h1>{html.escape(topic.name)}</h1>
<p class="muted">{html.escape(keywords)!r} · last {topic.days} days · {span}</p>
<p>{len(posts):,} posts · {sum(p.score for p in posts):,} score ·
{n_comments:,} {comment_label} · {len(subs):,}/{net:,} subreddits matched</p>
{_summary_section(summary) if summary else ""}
<h2>Posts per day</h2>
{_day_chart(_daily(posts))}
{sentiment_section}
<h2>By weekday &amp; hour (UTC)</h2>
{_punchcard(posts, comments)}
<h2>Subreddits</h2>
<p class="muted">click any row to see the posts behind it</p>
{_drill(sub_rows, sub_groups, prefix="r/")}
<h2>Most influential</h2>
{_drill(infl_rows, infl_groups, prefix="u/")}
<h2>Themes</h2>
{_themes(posts, keywords, comments)}
<h2>Links</h2>
{domains or '<div class="muted">no external links</div>'}
<h2>Top posts</h2>
<table>{top_rows}</table>
</body></html>"""

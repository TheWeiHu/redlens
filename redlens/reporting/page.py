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
from typing import TypeVar
from urllib.parse import urlparse

from sqlalchemy.engine import Engine
from sqlmodel import Session

from redlens import constants
from redlens.models import (
    Brand,
    Category,
    Comment,
    Post,
    Topic,
    TopicSummary,
)
from redlens.reporting import lda
from redlens.sentiment import DaySentiment
from redlens.topics import (
    list_topics,
    require_topic,
    topic_comments,
    topic_drop_confidences,
    topic_posts,
)

_WORD_RE = re.compile(r"[a-z0-9']+")
_Item = TypeVar("_Item", Post, Comment)
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_STOPWORDS = frozenset(constants.data_lines("stopwords.txt"))
_A = constants.ACCENT

_CSS = f"""
body {{ font-family: system-ui, sans-serif; max-width: 820px; margin: 2rem auto;
       padding: 0 1rem; line-height: 1.4; color: #222; }}
h1, h2 {{ font-weight: 600; }}
h1 {{ text-align: center; }}
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
.cfslide {{ margin: .2rem 0 1.4rem; padding: .7rem .9rem; border: 1px solid #eee;
  border-radius: 10px; background: #fafafa; }}
.cfslide-top {{ text-align: center; margin-bottom: .55rem; }}
.cfslide-top output {{ color: {_A}; font-weight: 600; font-size: .95rem; }}
.cfslide-track {{ display: flex; align-items: center; gap: .7rem; }}
.cfslide-track input[type=range] {{ flex: 1; accent-color: {_A}; }}
.cfend {{ font-size: .72rem; color: #999; white-space: nowrap; }}
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


def _post_li(p: Post) -> str:
    return (f"<li><a href='https://reddit.com/comments/{p.post_id}'>"
            f"{_trunc(p.title or '(untitled)')}</a> "
            f"<span class='muted'>{p.score:,} pts · {p.num_comments:,} comm. · "
            f"r/{html.escape(p.subreddit_name)} · {_date(p.created_utc)}</span></li>")


def _comment_li(c: Comment) -> str:
    body = (c.body or "").strip().replace("\n", " ") or "(comment)"
    return (f"<li><a href='https://reddit.com/comments/{c.link_id}/_/{c.comment_id}'>"
            f"{_trunc(body)}</a> "
            f"<span class='muted'>{c.score:,} pts · comment · "
            f"r/{html.escape(c.subreddit_name)} · {_date(c.created_utc)}</span></li>")


def _post_links(posts: list[Post]) -> str:
    """A clickable list of the underlying posts — every claim drills down to
    the real Reddit threads behind it."""
    return "<ul>" + "".join(
        _post_li(p) for p in posts[:constants.DRILL_POSTS]) + "</ul>"


def _drill(rows: list[tuple[str, int]], groups: dict[str, list[Post]],
           prefix: str = "", comment_counts: Counter[str] | None = None) -> str:
    """Bar rows that expand (no JS, via <details>) to the posts behind them,
    best first. When ``comment_counts`` is given, each expansion also reports
    the total comments pulled under that row's posts."""
    peak = max((n for _, n in rows), default=1)
    out = []
    for label, n in rows:
        posts = sorted(groups.get(label, []),
                       key=lambda p: (-p.score, p.post_id))
        meta = ""
        if comment_counts is not None:
            meta = (f"<div class='muted'>{n:,} posts · "
                    f"{comment_counts.get(label, 0):,} comments</div>")
        out.append(
            f"<details><summary>{_bar(label, n, peak, prefix)}</summary>"
            f"{meta}{_post_links(posts)}</details>"
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


def _sentiment_chart(series: list[DaySentiment]) -> str:
    """Inline-SVG diverging bar chart of daily sentiment in [-1, 1]: bars rise
    (green) above a neutral baseline for positive days and fall (red) below for
    negative ones, height ~ magnitude; hover for the day's score and post
    count. Unscored days (``mean is None`` — gaps or days the model left out)
    draw no bar. Returns '' when no day carries a non-zero score."""
    scored = [d for d in series if d.mean is not None]
    if not scored or all(d.mean == 0.0 for d in scored):
        return ""
    width, half, pad = 600.0, 38.0, 14.0
    center, total_h = half, half * 2
    bw = width / len(series)
    bars = []
    for i, dy in enumerate(series):
        if dy.mean is None:
            continue
        bh = abs(dy.mean) * half
        y = center - bh if dy.mean >= 0 else center
        cls = "pos" if dy.mean >= 0 else "neg"
        sign = "+" if dy.mean >= 0 else ""
        counts = f"{dy.posts:,} posts" + (
            f", {dy.comments:,} comments" if dy.comments else "")
        bars.append(
            f'<rect class="{cls}" x="{i * bw:.1f}" y="{y:.1f}" '
            f'width="{max(bw - 0.5, 0.4):.1f}" height="{max(bh, 0.6):.1f}">'
            f"<title>{dy.day}: {sign}{dy.mean:.2f} · {counts}</title></rect>"
        )
    baseline = (f'<line x1="0" y1="{center:.1f}" x2="{width:.0f}" '
                f'y2="{center:.1f}" stroke="#ccc" stroke-width="0.5"/>')
    labels = (f'<text x="0" y="{total_h + pad - 2:.0f}">{series[0].day}</text>'
              f'<text x="{width:.0f}" y="{total_h + pad - 2:.0f}" '
              f'text-anchor="end">{series[-1].day}</text>')
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
    # Pulled comments sit at their real time; without them we fold each post's
    # reported count into the post's own hour — so label honestly per mode.
    unit = "posts + comments" if comments else "posts + reported comments"
    peak = max(counts.values())
    cell, left, top = 22, 32, 4
    w, h = left + 24 * cell, top + 7 * cell + 14
    dots = "".join(
        f'<circle cx="{left + hr * cell + cell / 2:.0f}" '
        f'cy="{top + d * cell + cell / 2:.0f}" r="{1 + 8 * (n / peak) ** 0.5:.1f}">'
        f"<title>{_WEEKDAYS[d]} {hr:02d}:00 UTC — {n:,} {unit}</title>"
        f"</circle>"
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


def _lda_themes(posts: list[Post], comments: list[Comment],
                keywords: str) -> list[tuple[float, list[str]]]:
    """LDA themes (share, keywords) over post text and, when pulled, comment
    bodies — the raw clusters, ready to render or hand to a labeler."""
    skip = _STOPWORDS | set(_WORD_RE.findall(keywords.lower()))

    def tokens(text: str) -> list[str]:
        return [w for w in _WORD_RE.findall(text.lower())
                if len(w) > 2 and w not in skip]

    docs = [t for p in posts if (t := tokens(f"{p.title or ''} {p.selftext or ''}"))]
    docs += [t for c in comments if (t := tokens(c.body or ""))]
    return lda.topics(docs)


def _themes_html(themes: list[tuple[float, list[str]]],
                 labels: list[str] | None = None) -> str:
    """Render LDA themes. With ``labels`` (one per theme, from the LLM) each row
    leads with the readable label and keeps the keywords as muted context;
    without them it shows the keywords alone."""
    if not themes:
        return '<div class="muted">not enough text for topic modeling</div>'
    out = []
    for i, (share, words) in enumerate(themes):
        wl = html.escape(", ".join(words))
        if labels and i < len(labels) and labels[i]:
            out.append(f"<div>{share:.0%} · <strong>{html.escape(labels[i])}</strong> "
                       f'<span class="muted">{wl}</span></div>')
        else:
            out.append(f"<div>{share:.0%} · {wl}</div>")
    return "\n".join(out)


def _count_mentions(
    named_terms: list[tuple[str, list[str]]],
    posts: list[Post], comments: list[Comment],
) -> list[tuple[str, int, list[Post], list[Comment]]]:
    """Count how many posts and comments mention each ``(name, terms)`` entry —
    deterministic, case-insensitive, whole-word over the entry's terms. Returns
    (name, mention count, matching posts, matching comments) for entries that
    appear at least once, most-mentioned first. The LLM recognized the entries;
    the frequency is counted here so it's exact. Shared by the brands, complaints
    and use-case sections."""
    rows: list[tuple[str, int, list[Post], list[Comment]]] = []
    for label, raw_terms in named_terms:
        terms = [t for t in raw_terms if t]
        if not terms:
            continue
        # (?<!\w)…(?!\w) instead of \b…\b: a plain \b needs a word char on the
        # boundary, so a symbol-edged term ("C++", ".NET", "C#") would never
        # match. Lookarounds assert only that the *adjacent* char isn't a word
        # char, so symbol-edged names count while "Go" still won't hit "Google".
        pat = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms)
                         + r")(?!\w)", re.IGNORECASE)
        mp = [p for p in posts if pat.search(f"{p.title or ''} {p.selftext or ''}")]
        mc = [c for c in comments if pat.search(c.body or "")]
        if mp or mc:
            rows.append((label, len(mp) + len(mc), mp, mc))
    rows.sort(key=lambda r: (-r[1], r[0].lower()))
    return rows


def _mentions_section(
    heading: str, caption: str,
    rows: list[tuple[str, int, list[Post], list[Comment]]],
) -> str:
    """Render a mention section: a count bar per entry that expands to the posts
    and comments behind it, best first. '' when there's nothing to show."""
    if not rows:
        return ""
    rows = rows[:constants.TOP_MENTIONS]
    peak = max(n for _, n, _, _ in rows)
    out = []
    for name, n, mp, mc in rows:
        aposts = sorted(mp, key=lambda p: (-p.score, p.post_id))[:constants.DRILL_POSTS]
        acomments = sorted(mc, key=lambda c: (-c.score, c.comment_id)
                           )[:constants.DRILL_POSTS]
        items = ("".join(_post_li(p) for p in aposts)
                 + "".join(_comment_li(c) for c in acomments))
        out.append(f"<details><summary>{_bar(name, n, peak)}</summary>"
                   f"<ul>{items}</ul></details>")
    return (f"<h2>{html.escape(heading)}</h2>\n"
            f'<p class="muted">{html.escape(caption)}</p>\n' + "\n".join(out))


def _influential(posts: list[Post],
                 comments: list[Comment]) -> list[tuple[str, int, str]]:
    """Authors by engagement index: per post sqrt(score + COMMENT_WEIGHT x
    comments) and per comment sqrt(score), summed over everything past a floor —
    so a prolific commenter counts, not just posters, and zero-engagement bot
    volume is excluded. Returns (display label, points, raw username) so the
    page can drill to that author's posts and comments."""
    index: dict[str, float] = {}
    posts_by: Counter[str] = Counter()
    comments_by: Counter[str] = Counter()
    for p in posts:
        if p.author_username.lower() in constants.NON_AUTHORS:
            continue
        engagement = max(p.score, 0) + constants.COMMENT_WEIGHT * p.num_comments
        if engagement < constants.MIN_POST_ENGAGEMENT:
            continue
        index[p.author_username] = index.get(p.author_username, 0.0) + engagement**0.5
        posts_by[p.author_username] += 1
    for c in comments:
        if c.author_username.lower() in constants.NON_AUTHORS:
            continue
        if max(c.score, 0) < constants.MIN_POST_ENGAGEMENT:
            continue
        index[c.author_username] = index.get(c.author_username, 0.0) + c.score**0.5
        comments_by[c.author_username] += 1

    def _label(name: str) -> str:
        parts = []
        if posts_by[name]:
            parts.append(f"{posts_by[name]} post{'' if posts_by[name] == 1 else 's'}")
        if comments_by[name]:
            parts.append(
                f"{comments_by[name]} comment{'' if comments_by[name] == 1 else 's'}")
        return f"{name} ({', '.join(parts)})"

    ranked = sorted(index.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    return [(_label(name), round(pts), name)
            for name, pts in ranked[:constants.TOP_AUTHORS]]


def _influence_drill(infl: list[tuple[str, int, str]],
                     posts_by_author: dict[str, list[Post]],
                     comments_by_author: dict[str, list[Comment]]) -> str:
    """Influence rows that expand (no JS) to the author's top posts and
    comments, best first — so an author's score drills to the activity behind
    it, whether they posted, commented, or both."""
    peak = max((pts for _, pts, _ in infl), default=1)
    out = []
    for label, pts, author in infl:
        aposts = sorted(posts_by_author.get(author, []),
                        key=lambda p: (-p.score, p.post_id))[:constants.DRILL_POSTS]
        acomments = sorted(comments_by_author.get(author, []),
                           key=lambda c: (-c.score, c.comment_id))[:constants.DRILL_POSTS]
        items = ("".join(_post_li(p) for p in aposts)
                 + "".join(_comment_li(c) for c in acomments))
        out.append(
            f'<details><summary>{_bar(label, pts, peak, "u/")}</summary>'
            f"<ul>{items}</ul></details>")
    return "\n".join(out)


def _host(post: Post) -> str:
    """The external domain a post links to, or '' for self/Reddit posts."""
    host = urlparse(post.url or "").netloc.lower().removeprefix("www.")
    return "" if host.endswith(("reddit.com", "redd.it")) else host


def _link_domains(posts: list[Post]) -> Counter[str]:
    return Counter(h for p in posts if (h := _host(p)))


def _by(items: list[_Item], key: Callable[[_Item], str]) -> dict[str, list[_Item]]:
    """Group items (posts or comments) under a key function, dropping empty keys."""
    groups: dict[str, list[_Item]] = defaultdict(list)
    for it in items:
        if k := key(it):
            groups[k].append(it)
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


def _html_shell(title: str, body: str) -> str:
    """The standalone-HTML wrapper (doctype, head, inline CSS) shared by the
    per-topic page and the --all index so the two can't drift. ``title`` is
    escaped and suffixed with ' · redlens'; ``body`` is the inner markup."""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f'<title>{html.escape(title)} · redlens</title>'
        f'<style>{_CSS}</style></head><body>\n{body}\n</body></html>')


def render_all(engine: Engine, out_dir: Path,
               summarize: Callable[[str], TopicSummary | None] | None = None,
               sentiment: Callable[[str], list[DaySentiment] | None] | None = None,
               theme_labeler: Callable[[Session, str, list[list[str]]],
                                       list[str]] | None = None,
               brands: Callable[[str], list[Brand] | None] | None = None,
               complaints: Callable[[str], list[Category] | None] | None = None,
               use_cases: Callable[[str], list[Category] | None] | None = None,
               ) -> list[PageResult]:
    """Render every tracked topic into ``out_dir`` plus an ``index.html`` that
    links them. Topics with zero matched posts are skipped (and noted on the
    index) since there is nothing to chart. Returns one result per topic,
    in the same most-recently-tracked-first order as the index.

    ``summarize`` is an optional per-topic AI-narrative provider (one LLM call
    each). ``sentiment`` is an optional per-topic LLM sentiment-trend provider;
    without it the page shows no sentiment chart. ``theme_labeler`` turns each
    page's LDA clusters into readable labels. ``brands`` surfaces the other
    brands named in each topic's discussion. When given, every rendered page
    gets those (each provider is best-effort: a per-topic LLM failure drops just
    that section, see the CLI guards)."""
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
        days = sentiment(listing.name) if sentiment else None
        found_brands = brands(listing.name) if brands else None
        found_complaints = complaints(listing.name) if complaints else None
        found_uses = use_cases(listing.name) if use_cases else None
        doc = render_topic_page(engine, listing.name, summary=summary,
                                sentiment_days=days, theme_labeler=theme_labeler,
                                brands=found_brands, complaints=found_complaints,
                                use_cases=found_uses)
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
    table = (f"<table>{rows}</table>" if written
             else '<p class="muted">no tracked topics with matched posts yet</p>')
    skip_note = ""
    if skipped:
        names = ", ".join(html.escape(r.name) for r in skipped)
        skip_note = (f'<p class="muted">skipped (no matched posts yet): '
                     f'{names}</p>')
    body = (f'<h1>tracked topics</h1>\n'
            f'<p class="muted">{len(written):,} '
            f'report{"" if len(written) == 1 else "s"}</p>\n{table}\n{skip_note}')
    return _html_shell("tracked topics", body)


def render_topic_page(engine: Engine, name: str,
                      summary: TopicSummary | None = None,
                      sentiment_days: list[DaySentiment] | None = None,
                      theme_labeler: Callable[[Session, str, list[list[str]]],
                                              list[str]] | None = None,
                      brands: list[Brand] | None = None,
                      complaints: list[Category] | None = None,
                      use_cases: list[Category] | None = None,
                      min_confidence: float = 0.0) -> str:
    """Render one topic's page. ``summary`` (from ``summarize --topic``) is
    optional — when given, an AI-narrative section is added. ``sentiment_days``
    (from ``daily_topic_sentiment``) is the LLM-scored sentiment trend; when
    omitted the page shows no sentiment chart. ``theme_labeler`` (from
    ``label_themes``) turns each LDA keyword cluster into a readable label; when
    omitted the themes show keywords only. The page stays fully keyless without
    any of them."""
    with Session(engine) as session:
        topic = require_topic(session, name)

        def build_view(min_conf: float, label_themes: bool) -> tuple[str, int]:
            # post_id tie-break keeps the rendered page byte-deterministic
            posts = topic_posts(session, topic.name, min_conf)
            comments = topic_comments(session, topic.name, min_conf)
            section = _sentiment_section(sentiment_days) if sentiment_days else ""
            themes = _lda_themes(posts, comments, ", ".join(topic.keyword_list))
            labels = (theme_labeler(session, topic.name,
                                    [words for _, words in themes])
                      if theme_labeler and themes and label_themes else None)
            themes_html = _themes_html(themes, labels)
            brands_html = _mentions_section(
                "Other brands mentioned",
                "competitors and alternatives named in the discussion, by posts + "
                "comments mentioning them · click to read",
                _count_mentions([(b.name, b.aliases) for b in brands], posts, comments),
            ) if brands else ""
            complaints_html = _mentions_section(
                "Top complaints",
                "recurring problems people raise, by posts + comments mentioning "
                "them · click to read",
                _count_mentions([(c.name, c.terms) for c in complaints], posts, comments),
            ) if complaints else ""
            use_cases_html = _mentions_section(
                "Use cases",
                "what people use it for, by posts + comments mentioning it · "
                "click to read",
                _count_mentions([(c.name, c.terms) for c in use_cases], posts, comments),
            ) if use_cases else ""
            return (_view(topic, posts, comments, summary, section, themes_html,
                          brands_html, complaints_html, use_cases_html), len(posts))

        # One full view per confidence breakpoint where the visible set changes
        # (drops with confidence < threshold reappear). No drops -> a single view.
        confs = topic_drop_confidences(session, topic.name)
        if not confs:
            return _html_shell(topic.name,
                               f"{_header(topic)}\n{build_view(min_confidence, True)[0]}")
        thresholds = [0.0] + [c + 1e-6 for c in confs]
        built = [build_view(t, label_themes=(i == 0)) for i, t in enumerate(thresholds)]
        return _slider_page(topic, [h for h, _ in built], [n for _, n in built],
                            [round(c * 100) for c in confs])


def _sentiment_section(series: list[DaySentiment]) -> str:
    """The 'Sentiment over time' heading + caption + chart, or '' when there's
    nothing to chart. The series is always LLM-scored (see
    ``daily_topic_sentiment``)."""
    svg = _sentiment_chart(series)
    if not svg:
        return ""
    return ('<h2>Sentiment over time</h2>\n'
            '<p class="muted">daily sentiment, −1 (negative) to +1 (positive)'
            f' · LLM-scored</p>\n{svg}')


def _view(topic: Topic, posts: list[Post], comments: list[Comment],
          summary: TopicSummary | None, sentiment_section: str, themes_html: str,
          brands_html: str, complaints_html: str, use_cases_html: str) -> str:
    """Everything from the headline counts down, computed for one post set — so the
    same body renders twice (relevant-only and all-matched) for the toggle to swap."""
    subs = Counter(p.subreddit_name for p in posts)
    net = len(topic.subreddit_list)
    ts = [p.created_utc for p in posts]
    span = f"{_date(min(ts))} – {_date(max(ts))}" if ts else "—"
    n_comments = len(comments) or sum(p.num_comments for p in posts)
    comment_label = "comments analyzed" if comments else "comments on posts"
    # Per-subreddit comment totals for the drill: real pulled comments when we
    # have them, else each subreddit's reported num_comments (mirrors the
    # headline's `len(comments) or sum(num_comments)`), so an un-pulled topic
    # shows true reported volume instead of a misleading 0.
    if comments:
        sub_comment_counts = Counter(c.subreddit_name for c in comments)
    else:
        sub_comment_counts = Counter()
        for p in posts:
            sub_comment_counts[p.subreddit_name] += p.num_comments

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
    infl_html = _influence_drill(
        _influential(posts, comments), _by(posts, lambda p: p.author_username),
        _by(comments, lambda c: c.author_username))
    domain_rows = _ranked(_link_domains(posts), constants.TOP_DOMAINS)
    domains = _drill(domain_rows, _by(posts, _host)) if domain_rows else ""
    score = sum(p.score for p in posts) + sum(c.score for c in comments)
    return f"""<p>{len(posts):,} posts · {score:,} score ·
{n_comments:,} {comment_label} · {len(subs):,}/{net:,} subreddits matched · {span}</p>
{_summary_section(summary) if summary else ""}
<h2>Posts per day</h2>
{_day_chart(_daily(posts))}
{sentiment_section}
<h2>By weekday &amp; hour (UTC)</h2>
<p class="muted">{"posts + comments" if comments else "posts (+ reported comment counts)"} · dot area ~ volume</p>
{_punchcard(posts, comments)}
<h2>Subreddits</h2>
<p class="muted">click any row to see the posts behind it</p>
{_drill(sub_rows, sub_groups, prefix="r/", comment_counts=sub_comment_counts)}
<h2>Most influential</h2>
{infl_html}
<h2>Themes</h2>
{themes_html}
{complaints_html}
{use_cases_html}
{brands_html}
<h2>Links</h2>
{domains or '<div class="muted">no external links</div>'}
<h2>Top posts</h2>
<table>{top_rows}</table>"""


def _header(topic: Topic) -> str:
    return f"<h1>{html.escape(topic.name)}</h1>"


def _slider_page(topic: Topic, views: list[str], counts: list[int],
                 breakpoints: list[int]) -> str:
    """A relevance-confidence slider near the top that recomputes the WHOLE page:
    each ``views[i]`` is a full render that reveals more off-topic matches, and the
    slider (drag right = require higher confidence to hide) selects which to show.

    Needs a little JS — the only spot the page isn't pure HTML/CSS — because a
    draggable range input can't drive sibling visibility in CSS alone. The model's
    confidence is coarse (near-bimodal), so the slider snaps to the few breakpoints
    where the visible set actually changes."""
    # views are ordered loosest→strictest (most posts → fewest). The strictest
    # (relevant-only) view is the default, shown on the RIGHT of the slider.
    total = max(counts)
    view_divs = "".join(
        f'<div class="cfview"{" hidden" if n != min(counts) else ""}>{v}</div>'
        for v, n in zip(views, counts, strict=True))
    slider = (
        '<div class="cfslide"><div class="cfslide-top"><output id="cflab"></output></div>'
        '<div class="cfslide-track"><span class="cfend">all matches</span>'
        '<input type="range" id="cf" min="0" max="100" step="1" value="100">'
        '<span class="cfend">relevant only</span></div></div>')
    js = (
        "<script>(function(){"
        f"var bp={breakpoints},counts={counts},total={total};"
        "var s=document.getElementById('cf'),lab=document.getElementById('cflab'),"
        "views=document.querySelectorAll('.cfview');"
        # slider right = strict: invert v so higher = hide more (fewer posts shown)
        "function u(){var v=+s.value,i=bp.filter(function(b){return b<(100-v);}).length;"
        "views.forEach(function(el,k){el.hidden=k!==i;});var shown=counts[i];"
        "lab.textContent=shown===total?'all '+total+' posts':"
        "shown.toLocaleString()+' of '+total.toLocaleString()+' posts';}"
        "s.addEventListener('input',u);u();})();</script>")
    return _html_shell(topic.name, f"{_header(topic)}\n{slider}{view_divs}{js}")

"""Render a tracked topic into a standalone HTML page.

One self-contained file — inline CSS, no JavaScript, no external assets —
so the output can be mailed, hosted, or opened from disk as-is.
"""
from __future__ import annotations

import html
from collections import Counter
from datetime import UTC, datetime

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from redlens.errors import NotFound
from redlens.models import Post, Topic, TopicPost
from redlens.topics import get_topic

TOP_POSTS = 25
TOP_SUBREDDITS = 15

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


def render_topic_page(engine: Engine, name: str) -> str:
    with Session(engine) as session:
        topic = get_topic(session, name)
        if topic is None:
            raise NotFound(f"topic {name!r} not tracked yet — run `redlens track` first")
        posts = list(session.exec(
            select(Post)
            .join(TopicPost, TopicPost.post_id == Post.post_id)  # type: ignore[arg-type]
            .where(TopicPost.topic_name == topic.name)
            .order_by(Post.score.desc())  # type: ignore[attr-defined]
        ))
        return _render(topic, posts)


def _render(topic: Topic, posts: list[Post]) -> str:
    subs = Counter(p.subreddit_name for p in posts)
    months = Counter(
        datetime.fromtimestamp(p.created_utc, tz=UTC).strftime("%Y-%m") for p in posts
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
<h2>Where the conversation happens</h2>
{_bars(subs.most_common(TOP_SUBREDDITS), prefix="r/")}
<div class="meta" style="margin-top:6px">searched {net:,} subreddits;
{net - len(subs):,} had no matching posts in the window</div>
<h2>Volume by month</h2>
{_bars(sorted(months.items()))}
<h2>Top posts</h2>
<table>{top_rows}</table>
<footer>Generated by <a href="https://github.com/TheWeiHu/redlens">redlens</a> ·
the open-source intelligent lens on public discussion</footer>
</body></html>"""

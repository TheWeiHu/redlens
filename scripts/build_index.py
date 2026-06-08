"""Generate a very basic HTML index for every user in the DB, plus a
per-user page with basic stats. Plain HTML — no JS, no fancy charts."""

from __future__ import annotations

import argparse
import html
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from redditpages.db import DATA_DIR, connect, data_db
from redditpages.models import Comment, Post, User

from scripts.sync_important_users import USERS as CURATED

CSS = """
body { font-family: ui-monospace, monospace; margin: 24px; max-width: 1100px; }
h1 { font-size: 18px; margin: 0 0 4px; }
h2 { font-size: 14px; margin: 24px 0 6px; color: #444;
     text-transform: uppercase; letter-spacing: .08em; }
.note { color: #666; font-size: 12px; margin-bottom: 16px; }
a { color: #06f; text-decoration: none; }
a:hover { text-decoration: underline; }
table { border-collapse: collapse; font-size: 12px; width: 100%; }
th, td { border: 1px solid #ddd; padding: 4px 8px; text-align: left;
         vertical-align: top; }
th { background: #f5f5f5; }
tr:nth-child(even) { background: #fafafa; }
td.n { text-align: right; font-variant-numeric: tabular-nums; }
td.d { color: #666; }
td.reason { color: #666; max-width: 280px; }
td.body { max-width: 600px; }
.kv { display: grid; grid-template-columns: 180px 1fr;
      gap: 4px 16px; font-size: 13px; }
.kv .k { color: #666; }
.kv .v { font-variant-numeric: tabular-nums; }
.back { font-size: 12px; }
"""


def fmt_n(n: int | None) -> str:
    return "—" if n is None else f"{n:,}"


def fmt_date(ts: int | None) -> str:
    return "—" if not ts else datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def fmt_ts(ts: int | None) -> str:
    return "—" if not ts else datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M")


def page(title: str, body: str) -> str:
    return (
        f"<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        f"<style>{CSS}</style></head><body>{body}</body></html>"
    )


def build_user_page(
    user: User, posts: list[Post], comments: list[Comment],
    cat: str, reason: str,
) -> str:
    """Render one user's basic stats page."""
    u = html.escape(user.username)
    n_posts = len(posts)
    n_comments = len(comments)
    pk = sum(p.score for p in posts)
    ck = sum(c.score for c in comments)
    timestamps = [p.created_utc for p in posts] + [c.created_utc for c in comments]
    first = min(timestamps, default=None)
    last = max(timestamps, default=None)
    days = {datetime.fromtimestamp(ts, tz=UTC).date() for ts in timestamps}

    sub_events: Counter[str] = Counter(p.subreddit_name for p in posts)
    sub_events.update(c.subreddit_name for c in comments)

    sub_karma: Counter[str] = Counter()
    for p in posts:
        sub_karma[p.subreddit_name] += p.score
    for c in comments:
        sub_karma[c.subreddit_name] += c.score

    # Top subs by event count
    top_subs = sub_events.most_common(15)
    sub_rows = "".join(
        f"<tr><td>{i + 1}</td>"
        f"<td><a href='https://reddit.com/r/{html.escape(sub)}' target='_blank'>"
        f"r/{html.escape(sub)}</a></td>"
        f"<td class='n'>{fmt_n(n)}</td>"
        f"<td class='n'>{fmt_n(sub_karma[sub])}</td></tr>"
        for i, (sub, n) in enumerate(top_subs)
    )

    # Recent 25 events
    items = []
    for p in posts:
        items.append((p.created_utc, "post", p.subreddit_name, p.score,
                      p.post_id, p.title or "", ""))
    for c in comments:
        items.append((c.created_utc, "comment", c.subreddit_name, c.score,
                      c.comment_id, "", (c.body or "")[:200].replace("\n", " ")))
    items.sort(key=lambda x: x[0], reverse=True)
    item_rows = "".join(
        f"<tr><td class='d'>{fmt_ts(ts)}</td><td>{typ}</td>"
        f"<td>r/{html.escape(sub)}</td>"
        f"<td class='n'>{score:+,}</td>"
        f"<td class='body'>{html.escape(title or body)}</td></tr>"
        for ts, typ, sub, score, _id, title, body in items[:25]
    )

    body_html = f"""
<div class="back"><a href="../index.html">← back to index</a></div>
<h1>u/{u}</h1>
<div class="note">{html.escape(cat)} &middot; {html.escape(reason)}</div>

<h2>Stats</h2>
<div class="kv">
<div class="k">total karma</div><div class="v">{fmt_n(pk + ck)}</div>
<div class="k">post karma</div><div class="v">{fmt_n(pk)}</div>
<div class="k">comment karma</div><div class="v">{fmt_n(ck)}</div>
<div class="k">posts</div><div class="v">{fmt_n(n_posts)}</div>
<div class="k">comments</div><div class="v">{fmt_n(n_comments)}</div>
<div class="k">active days</div><div class="v">{fmt_n(len(days))}</div>
<div class="k">distinct subreddits</div><div class="v">{fmt_n(len(sub_events))}</div>
<div class="k">first event</div><div class="v">{fmt_date(first)}</div>
<div class="k">last event</div><div class="v">{fmt_date(last)}</div>
<div class="k">on reddit</div><div class="v"><a href="https://reddit.com/user/{u}" target="_blank">u/{u} ↗</a></div>
</div>

<h2>Top {len(top_subs)} subreddits (by activity)</h2>
<table>
<thead><tr><th>#</th><th>subreddit</th><th>events</th><th>karma</th></tr></thead>
<tbody>{sub_rows}</tbody>
</table>

<h2>Most recent {min(25, len(items))} of {len(items)} events</h2>
<table>
<thead><tr><th>when</th><th>type</th><th>sub</th><th>score</th><th>text</th></tr></thead>
<tbody>{item_rows}</tbody>
</table>
"""
    return page(f"u/{user.username} · RedditPages", body_html)


def build_index(rows: list[dict], n_synced: int) -> str:
    rows = sorted(rows, key=lambda r: r.get("total_karma") or 0, reverse=True)
    table_rows = []
    for i, r in enumerate(rows, 1):
        u = html.escape(r["username"])
        cat = html.escape(r["category"])
        why = html.escape(r.get("reason", ""))
        table_rows.append(
            f"<tr>"
            f"<td>{i}</td>"
            f"<td><a href='u/{u}.html'>u/{u}</a></td>"
            f"<td>{cat}</td>"
            f"<td class='reason'>{why}</td>"
            f"<td class='n'>{fmt_n(r['total_karma'])}</td>"
            f"<td class='n'>{fmt_n(r['post_karma'])}</td>"
            f"<td class='n'>{fmt_n(r['comment_karma'])}</td>"
            f"<td class='n'>{fmt_n(r['total_posts'])}</td>"
            f"<td class='n'>{fmt_n(r['total_comments'])}</td>"
            f"<td class='n'>{fmt_n(r['active_days'])}</td>"
            f"<td>{('r/' + html.escape(r['top_subreddit'])) if r['top_subreddit'] else '—'}</td>"
            f"<td class='d'>{fmt_date(r['first_event_at'])}</td>"
            f"<td class='d'>{fmt_date(r['last_event_at'])}</td>"
            f"</tr>"
        )

    body = f"""
<h1>RedditPages — {n_synced} important users</h1>
<div class="note">
Sorted by total karma. Click a username for that user's basic stats page.
Generated {datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%MZ")}.
</div>
<table>
<thead><tr>
<th>#</th><th>user</th><th>cat</th><th>why important</th>
<th>karma</th><th>post k</th><th>cmt k</th>
<th>posts</th><th>cmts</th><th>days</th>
<th>top sub</th><th>first</th><th>last</th>
</tr></thead>
<tbody>{"".join(table_rows)}</tbody>
</table>
"""
    return page("RedditPages — important users", body)


def _user_stats(session: Session, user: User) -> tuple[list[Post], list[Comment]]:
    posts = list(session.exec(select(Post).where(
        Post.author_username == user.username)))
    comments = list(session.exec(select(Comment).where(
        Comment.author_username == user.username)))
    return posts, comments


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=data_db("redditpages.db"))
    p.add_argument("--out-dir", default=str(DATA_DIR / "important"))
    args = p.parse_args()

    out_dir = Path(args.out_dir).resolve()
    (out_dir / "u").mkdir(parents=True, exist_ok=True)

    cat_map = {u: (cat, why) for u, cat, why in CURATED}
    engine = connect(args.db)
    rows = []
    with Session(engine) as s:
        users = list(s.exec(select(User)))
        for u in users:
            posts, comments = _user_stats(s, u)
            cat, reason = cat_map.get(u.username, ("?", ""))

            # Write the per-user page
            user_html = build_user_page(u, posts, comments, cat, reason)
            (out_dir / "u" / f"{u.username}.html").write_text(user_html)

            # Stash data for the index row
            pk = sum(p.score for p in posts)
            ck = sum(c.score for c in comments)
            timestamps = [p.created_utc for p in posts] + [
                c.created_utc for c in comments
            ]
            days = {datetime.fromtimestamp(ts, tz=UTC).date()
                    for ts in timestamps}
            sub_events: Counter[str] = Counter(p.subreddit_name for p in posts)
            sub_events.update(c.subreddit_name for c in comments)
            top = sub_events.most_common(1)
            rows.append({
                "username": u.username,
                "category": cat, "reason": reason,
                "total_posts": len(posts),
                "total_comments": len(comments),
                "post_karma": pk,
                "comment_karma": ck,
                "total_karma": pk + ck,
                "first_event_at": min(timestamps, default=None),
                "last_event_at": max(timestamps, default=None),
                "active_days": len(days),
                "top_subreddit": top[0][0] if top else None,
            })

    index_html = build_index(rows, len(rows))
    (out_dir / "index.html").write_text(index_html)
    print(f"wrote {out_dir}/index.html  ({len(rows)} users)")
    print(f"wrote {len(rows)} per-user pages into {out_dir}/u/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

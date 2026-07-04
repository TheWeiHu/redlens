"""Render the unified ``redlens home`` dashboard as one standalone HTML page.

A single keyless overview tying the archive together: tracked TOPICS (by
share-of-voice), watchlist USERS (main subreddit + post count), and the
CROSSINGS between them — which synced users show up in which tracked topics.

Reuses the per-topic page's shell (``_html_shell``) and slug rules so the home
page can't drift from the rest of redlens, and links out with the same
``<slug>.html`` convention ``page --all`` writes, so a home rendered alongside
``page --all`` output has resolving topic links.
"""
from __future__ import annotations

import html

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, func, select

from redlens.analytics import list_users
from redlens.constants import ACCENT
from redlens.models import Post, Topic, TopicListing, TopicPost, UserListing
from redlens.reporting.page import _html_shell, _unique_slug
from redlens.topics import list_topics, relevant_clause

_REDDIT_USER = "https://www.reddit.com/user/{}"

# Home-specific layout, scoped to this page; the accent + typography come from
# the shared shell. Kept inline so the page stays self-contained and the shared
# style.css isn't touched for one surface.
_STYLE = f"""
.home-sub {{ text-align:center; color:#999; font-size:.82rem;
             margin:-.4rem 0 2rem; }}
.cols {{ display:grid; grid-template-columns:1fr 1fr; gap:2.4rem; }}
.trow {{ display:grid; grid-template-columns:7rem 1fr 2.8rem; gap:.6rem;
         align-items:center; margin:.4rem 0; }}
.track {{ background:#f0e7e3; border-radius:3px; height:.95rem; }}
.fill {{ background:{ACCENT}; height:.95rem; border-radius:3px; }}
.pct {{ text-align:right; color:#888; font-size:.85rem;
        font-variant-numeric:tabular-nums; }}
.u, .x {{ display:flex; justify-content:space-between; align-items:baseline;
          padding:.4rem 0; border-bottom:1px solid #f0f0f0; }}
.u .sub2 {{ color:#999; font-size:.8rem; }}
.u .ct, .x .ct {{ color:#888; font-size:.8rem; white-space:nowrap;
                  font-variant-numeric:tabular-nums; }}
.x .arr {{ color:#ccc; padding:0 .35rem; }}
.xwrap {{ margin-top:2.4rem; }}
.empty {{ color:#999; font-size:.85rem; }}
"""


def _topic_slugs(listings: list[TopicListing]) -> dict[str, str]:
    """Assign each topic the same ``<slug>.html`` filename ``page --all`` would,
    by walking ``list_topics`` order through ``_unique_slug`` — so home's topic
    links land on the files a sibling ``page --all`` writes."""
    used: set[str] = set()
    return {t.name: _unique_slug(t.name, used) for t in listings}


def _main_subreddits(session: Session, usernames: set[str]) -> dict[str, str]:
    """Each user's most-posted-in subreddit, from one grouped query."""
    rows = session.exec(
        select(Post.author_username, Post.subreddit_name, func.count())
        .group_by(Post.author_username, Post.subreddit_name)
    ).all()
    best: dict[str, tuple[int, str]] = {}
    for author, sub, n in rows:
        if author not in usernames:
            continue
        if n > best.get(author, (0, ""))[0]:
            best[author] = (n, sub)
    return {author: sub for author, (_, sub) in best.items()}


def _crossings(session: Session, usernames: set[str]) -> list[tuple[str, str, int]]:
    """(user, topic, count) for every synced user who authored a post tagged to
    a tracked topic — the one genuinely new query. Sorted count-desc."""
    if not usernames:
        return []
    rows = session.exec(
        select(Post.author_username, Topic.name, func.count())
        .join(TopicPost, col(TopicPost.post_id) == col(Post.post_id))
        .join(Topic, col(Topic.id) == col(TopicPost.topic_id))
        .where(relevant_clause())
        .where(col(Post.author_username).in_(usernames))
        .group_by(Post.author_username, Topic.name)
    ).all()
    out = [(a, t, n) for a, t, n in rows]
    out.sort(key=lambda r: r[2], reverse=True)
    return out


def _topics_panel(listings: list[TopicListing], slugs: dict[str, str]) -> str:
    ranked = sorted((t for t in listings if t.matched_posts > 0),
                    key=lambda t: t.matched_posts, reverse=True)
    total = sum(t.matched_posts for t in ranked)
    if not ranked:
        return '<h2>Topics</h2><p class="empty">no matched topics yet</p>'
    peak = ranked[0].matched_posts
    rows = []
    for t in ranked:
        share = t.matched_posts / total if total else 0
        width = 100 * t.matched_posts / peak
        rows.append(
            f'<div class="trow">'
            f'<a href="{slugs[t.name]}.html">{html.escape(t.name)}</a>'
            f'<div class="track"><div class="fill" style="width:{width:.0f}%">'
            f'</div></div><span class="pct">{share:.0%}</span></div>')
    return "<h2>Topics</h2>\n" + "\n".join(rows)


def _users_panel(users: list[UserListing], main_sub: dict[str, str]) -> str:
    if not users:
        return '<h2>Users</h2><p class="empty">no synced users yet</p>'
    rows = []
    for u in users:
        sub = main_sub.get(u.username)
        sub_html = f' <span class="sub2">r/{html.escape(sub)}</span>' if sub else ""
        rows.append(
            f'<div class="u"><span>'
            f'<a href="{_REDDIT_USER.format(html.escape(u.username))}">'
            f'{html.escape(u.username)}</a>{sub_html}</span>'
            f'<span class="ct">{u.total_posts:,}</span></div>')
    return "<h2>Users</h2>\n" + "\n".join(rows)


def _crossings_panel(crossings: list[tuple[str, str, int]],
                     slugs: dict[str, str]) -> str:
    if not crossings:
        return ('<section class="xwrap"><h2>Crossings</h2>'
                '<p class="empty">no synced user appears in a tracked topic yet'
                '</p></section>')
    rows = []
    for user, topic, n in crossings:
        # every crossing's topic is a tracked topic, so it's always in slugs
        rows.append(
            f'<div class="x"><span>'
            f'<a href="{_REDDIT_USER.format(html.escape(user))}">'
            f'{html.escape(user)}</a><span class="arr">→</span>'
            f'<a href="{slugs[topic]}.html">{html.escape(topic)}</a></span>'
            f'<span class="ct">{n:,}</span></div>')
    return ('<section class="xwrap"><h2>Crossings</h2>\n'
            + "\n".join(rows) + "</section>")


def render_home(engine: Engine) -> str:
    """Return the full standalone-HTML home dashboard for ``engine``'s DB."""
    with Session(engine) as session:
        topics = list_topics(session)
        users = list_users(session)
        usernames = {u.username for u in users}
        slugs = _topic_slugs(topics)
        main_sub = _main_subreddits(session, usernames)
        crossings = _crossings(session, usernames)

    n_topics = sum(1 for t in topics if t.matched_posts > 0)
    body = (
        "<h1>redlens</h1>\n"
        f'<div class="home-sub">{n_topics} topic'
        f'{"" if n_topics == 1 else "s"} · {len(users)} user'
        f'{"" if len(users) == 1 else "s"}</div>\n'
        f'<style>{_STYLE}</style>\n'
        '<div class="cols">\n'
        f"<section>{_topics_panel(topics, slugs)}</section>\n"
        f"<section>{_users_panel(users, main_sub)}</section>\n"
        "</div>\n"
        f"{_crossings_panel(crossings, slugs)}")
    return _html_shell("home", body)

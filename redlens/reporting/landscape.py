"""Render a :class:`~redlens.landscape.Landscape` as a standalone HTML page.

Reuses the per-topic page's shell (``_html_shell``) and bar markup (``_bar``) so
the cross-topic page can't drift from the per-topic look. Share-of-voice is the
headline: one bar per topic, longest = most discussed over the matched window.
"""
from __future__ import annotations

import html

from redlens.landscape import Landscape
from redlens.reporting.page import _bar, _date, _html_shell

_DISJOINT_NOTE = (
    "Volume is comparable across topics; brand mentions are not — brand nets are "
    "near-disjoint (a topic's own community rarely sits in another's net), so this "
    "page compares how much each topic is discussed, not which brands win.")


def render_landscape(land: Landscape) -> str:
    """Return a full standalone-HTML document for ``land``."""
    peak = max((t.posts for t in land.topics), default=1) or 1
    bars = "\n".join(
        _bar(t.name, t.posts, peak,
             prefix="")  # label is the topic; value column shows the count
        for t in land.topics
    )
    window = (f"{_date(land.window_start)} → {_date(land.window_end)} "
              f"({land.window_days} day{'' if land.window_days == 1 else 's'}, "
              f"{'matched overlap' if land.matched else 'forced window'})")

    rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(t.name)}</td>"
        f"<td>{t.posts:,}</td>"
        f"<td>{t.comments:,}</td>"
        f"<td>{t.posts_per_day:g}</td>"
        f"<td>{t.share_of_voice:.0%}</td>"
        f"<td>{html.escape('r/' + t.top_subreddit) if t.top_subreddit else '—'}</td>"
        "</tr>"
        for t in land.topics
    )
    table = (
        "<table><thead><tr><th>topic</th><th>posts</th><th>comments</th>"
        "<th>posts/day</th><th>share</th><th>top subreddit</th></tr></thead>"
        f"<tbody>\n{rows}\n</tbody></table>")

    body = (
        f"<h1>Landscape · {len(land.topics)} topics</h1>\n"
        f'<p class="meta">{html.escape(window)} · {land.total_posts:,} posts</p>\n'
        "<h2>Share of voice</h2>\n"
        f'<div class="bars">{bars}</div>\n'
        f"{table}\n"
        f'<p class="meta">{html.escape(_DISJOINT_NOTE)}</p>')
    return _html_shell("Landscape", body)

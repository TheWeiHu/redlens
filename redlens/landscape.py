"""Cross-topic comparison — the data side of ``redlens landscape``/``compare``.

redlens renders one page per topic; this is the first cross-topic synthesis. It
compares two or more tracked topics on *volume* — how much each is discussed —
which doubles as an empirical share-of-voice map across the set.

Two gotchas, learned the hard way, are baked in here (see brain
``llm-enrichment-architecture``):

* **Matched time windows.** Comparing a 30-day topic against a 90-day one
  invents fake paradoxes. So the default window is the *overlap* of every
  topic's data — the latest first-post across the set to the latest last-post —
  and ``--days N`` forces one explicit trailing window on all of them. Each
  topic's posts are clipped to that single window before anything is counted.
* **Brand nets are near-disjoint.** r/nordvpn isn't in the general "vpn" net, so
  cross-topic *brand* counts aren't comparable. This module deliberately does
  **not** compare brands — only volume, which is comparable. (Brand overlap is a
  deferred follow-up.)

Everything here is deterministic and keyless: no LLM call, exact counts.
"""
from __future__ import annotations

from collections import Counter

from pydantic import BaseModel
from sqlmodel import Session

from redlens.errors import RedlensError
from redlens.models import Comment, Post
from redlens.topics import require_topic, topic_comments, topic_posts

DAY = 86_400


class TopicStats(BaseModel):
    """One topic's volume within the matched window."""

    name: str
    posts: int
    comments: int
    active_days: int             # distinct UTC calendar days with a post
    posts_per_day: float         # posts / window_days (the matched window)
    top_subreddit: str | None    # the subreddit contributing the most posts
    share_of_voice: float        # posts / total posts across the compared set


class Landscape(BaseModel):
    """A cross-topic volume comparison over one matched time window."""

    window_start: int            # inclusive UTC second
    window_end: int             # inclusive UTC second
    window_days: int
    matched: bool                # True = window is the topics' overlap (default)
    total_posts: int
    topics: list[TopicStats]


def _day(ts: int) -> int:
    return ts // DAY


def compare_topics(session: Session, names: list[str],
                   days: int | None = None) -> Landscape:
    """Compare ``names`` (>= 2 tracked topics) on volume over one window.

    With ``days`` the window is the trailing ``days`` ending at the newest post
    across the set; without it the window is the topics' overlap (so every topic
    has data spanning it). Raises ``RedlensError`` if fewer than two topics are
    given, a topic has no matched posts, or the windows don't overlap.
    """
    if len(names) < 2:
        raise RedlensError("landscape: give at least two topics to compare")

    # Load each topic's archive once. require_topic raises NotFound on a typo.
    loaded: list[tuple[str, list[Post], list[Comment]]] = []
    for name in names:
        canonical = require_topic(session, name).name
        posts = topic_posts(session, canonical)
        comments = topic_comments(session, canonical)
        if not posts:
            raise RedlensError(
                f"landscape: topic {canonical!r} has no matched posts to compare")
        loaded.append((canonical, posts, comments))

    newest = max(p.created_utc for _, posts, _ in loaded for p in posts)
    if days is not None:
        if days < 1:
            raise RedlensError("landscape: --days must be at least 1")
        start, end = newest - days * DAY, newest
        window_days, matched = days, False
    else:
        # The overlap: every topic must cover the whole window, so clamp it to
        # the latest first-post .. earliest last-post across topics. Using the
        # global newest post as the end would count a shorter topic's missing
        # tail (and could call non-overlapping ranges a "matched" comparison).
        start = max(min(p.created_utc for p in posts) for _, posts, _ in loaded)
        end = min(max(p.created_utc for p in posts) for _, posts, _ in loaded)
        if start > end:
            raise RedlensError(
                "landscape: topics' date ranges don't overlap — pass --days to "
                "force a common trailing window")
        window_days = max(1, (end - start) // DAY + 1)
        matched = True

    def in_window(ts: int) -> bool:
        return start <= ts <= end

    stats: list[tuple[str, int, int, int, str | None]] = []
    for name, posts, comments in loaded:
        kept = [p for p in posts if in_window(p.created_utc)]
        ckept = [c for c in comments if in_window(c.created_utc)]
        active = len({_day(p.created_utc) for p in kept})
        subs = Counter(p.subreddit_name for p in kept)
        top = max(sorted(subs), key=lambda s: subs[s]) if subs else None
        stats.append((name, len(kept), len(ckept), active, top))

    total = sum(n for _, n, _, _, _ in stats)
    topics = [
        TopicStats(
            name=name, posts=n, comments=cn, active_days=active,
            posts_per_day=round(n / window_days, 2),
            top_subreddit=top,
            share_of_voice=round(n / total, 4) if total else 0.0,
        )
        for name, n, cn, active, top in stats
    ]
    # Loudest topic first; name as a stable tie-break.
    topics.sort(key=lambda t: (-t.posts, t.name.lower()))
    return Landscape(window_start=start, window_end=end, window_days=window_days,
                     matched=matched, total_posts=total, topics=topics)

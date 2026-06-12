"""Topic tracking: follow a subject across public discussion.

Arctic has no global full-text search — queries must be scoped to a
subreddit or an author — so tracking a topic means casting a net:

1. start from a subreddit list (guessed home subs, ``--subreddits``, or the
   list stored on the topic from previous runs),
2. optionally widen it by one discovery round (``--discover``): the authors
   of matching posts are queried author-scoped, and the other subreddits
   *their* matching posts live in join the net,
3. fan the query out per subreddit, dedupe by post id, upsert.

Empty or non-existent subreddits cost one request and return nothing, so
over-casting the net is cheap. Re-running with an unchanged net is
incremental via ``Topic.newest_seen_utc``.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from redlens import arctic
from redlens.db import upsert
from redlens.errors import RedlensError
from redlens.models import Post, Topic, TopicPost

# Discovery bounds: how many top posters to follow out of the seed subs, and
# how many new subreddits one round may add to the net.
DISCOVER_MAX_AUTHORS = 8
DISCOVER_MAX_NEW_SUBREDDITS = 12
# Authors that scope to noise rather than people.
_NON_AUTHORS = {"[deleted]", "AutoModerator"}


def _now() -> int:
    return int(time.time())


@dataclass
class TrackResult:
    topic: Topic
    posts_new: int
    subreddits_searched: int
    discovered: list[str] = field(default_factory=list)
    per_subreddit: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)  # subreddit -> error


def guess_home_subreddits(name: str) -> list[str]:
    """Candidate home subs from the topic name: 'dua lipa' -> dualipa,
    dua_lipa, DuaLipa. Wrong guesses return nothing and cost one request."""
    words = re.findall(r"[a-z0-9]+", name.lower())
    if not words:
        return []
    guesses = {
        "".join(words),
        "_".join(words),
        "".join(w.capitalize() for w in words),
    }
    return sorted(guesses)


@dataclass(frozen=True)
class SubredditCandidate:
    name: str
    subscribers: int
    description: str
    over_18: bool


def search_subreddits(topic: str, limit: int = 15) -> list[SubredditCandidate]:
    """Communities whose *name* matches the topic, via arctic's keyless
    subreddit search, largest first.

    This is the user-facing discovery step: the CLI shows the result as a
    pickable list. Name matching finds r/dualipa and r/DuaLipaDiscussion but
    not r/popheads — communities that merely *discuss* the topic come from
    the behavioral round (``--discover``) or the user's own additions.
    """
    prefixes = list(dict.fromkeys(
        s.lower() for s in guess_home_subreddits(topic)
    ))
    by_name: dict[str, SubredditCandidate] = {}
    for prefix in prefixes:
        try:
            found = arctic.search_subreddits(prefix, limit=limit)
        except RedlensError:
            continue  # discovery is best-effort; track still has fallbacks
        for s in found:
            name = s.get("display_name")
            if not name or name.lower() in by_name:
                continue
            by_name[name.lower()] = SubredditCandidate(
                name=name,
                subscribers=int(s.get("subscribers") or 0),
                description=" ".join((s.get("public_description") or "").split()),
                over_18=bool(s.get("over18")),
            )
    ranked = sorted(by_name.values(), key=lambda c: -c.subscribers)
    return ranked[:limit]


def get_topic(session: Session, name: str) -> Topic | None:
    return session.exec(
        select(Topic).where(func.lower(Topic.name) == name.lower())
    ).first()


def discover_subreddits(
    query: str,
    seeds: list[str],
    after: int,
    before: int,
) -> list[str]:
    """One bootstrap round: who posts about the topic in the seed subs, and
    where else do *they* post about it?"""
    authors: Counter[str] = Counter()
    for sub in seeds:
        try:
            for raw in arctic.iter_subreddit_query(
                sub, query, after=after, before=before
            ):
                author = raw.get("author") or ""
                if author and author not in _NON_AUTHORS:
                    authors[author] += 1
        except RedlensError:
            continue  # a dead seed shouldn't sink discovery

    found: Counter[str] = Counter()
    known = {s.lower() for s in seeds}
    for author, _ in authors.most_common(DISCOVER_MAX_AUTHORS):
        try:
            for raw in arctic.iter_author_query(
                author, query, after=after, before=before
            ):
                sub = raw.get("subreddit") or ""
                # u_* "subreddits" are user profile pages, not communities.
                if sub and sub.lower() not in known and not sub.startswith("u_"):
                    found[sub] += 1
        except RedlensError:
            continue
    return [s for s, _ in found.most_common(DISCOVER_MAX_NEW_SUBREDDITS)]


def track_topic(
    engine: Engine,
    name: str,
    *,
    query: str | None = None,
    subreddits: list[str] | None = None,
    days: int | None = None,
    discover: bool = False,
    on_progress: Callable[[str, int], None] | None = None,
) -> TrackResult:
    """Pull every post matching the topic's query across its subreddit net."""
    now = _now()
    with Session(engine, expire_on_commit=False) as session:
        topic = get_topic(session, name)
        if topic is None:
            topic = Topic(name=name, query=query or name, days=days or 180)
        if query:
            topic.query = query
        if days:
            topic.days = days

        net = list(dict.fromkeys(
            topic.subreddit_list + (subreddits or [])
        )) or guess_home_subreddits(name)

        window_start = now - topic.days * 86400
        discovered: list[str] = []
        if discover:
            discovered = discover_subreddits(topic.query, net, window_start, now)
            net = list(dict.fromkeys(net + discovered))

        # Incremental only when the net hasn't grown: new subreddits need the
        # full window, not just what's newer than the cursor.
        net_grew = set(s.lower() for s in net) != {
            s.lower() for s in topic.subreddit_list
        }
        after = window_start
        if topic.newest_seen_utc and not net_grew:
            after = max(window_start, topic.newest_seen_utc)

        seen: set[str] = set()
        newest = topic.newest_seen_utc or 0
        posts_new = 0
        per_subreddit: dict[str, int] = {}
        failed: dict[str, str] = {}
        for sub in net:
            batch: list[Post] = []
            try:
                for raw in arctic.iter_subreddit_query(
                    sub, topic.query, after=after, before=now
                ):
                    pid = raw.get("id")
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    post = Post.from_arctic(raw)
                    newest = max(newest, post.created_utc)
                    batch.append(post)
            except RedlensError as exc:
                # One bad subreddit (banned, renamed, transient 4xx) must not
                # sink the whole net; report it and keep casting.
                failed[sub] = str(exc)
                if on_progress:
                    on_progress(f"{sub} (failed)", 0)
                continue
            if batch:
                upsert(session, batch)
                upsert(session, [
                    TopicPost(topic_name=topic.name, post_id=p.post_id)
                    for p in batch
                ])
            per_subreddit[sub] = len(batch)
            posts_new += len(batch)
            if on_progress:
                on_progress(sub, len(batch))

        topic.subreddits = json.dumps(net)
        topic.newest_seen_utc = newest or None
        topic.last_tracked_at = now
        upsert(session, [topic])
        session.commit()

    return TrackResult(
        topic=topic,
        posts_new=posts_new,
        subreddits_searched=len(net),
        discovered=discovered,
        per_subreddit=per_subreddit,
        failed=failed,
    )

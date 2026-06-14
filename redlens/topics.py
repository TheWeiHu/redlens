"""Topic tracking: follow a subject across public discussion.

Arctic has no global full-text search — queries must be scoped to a
subreddit or an author — so tracking a topic means casting a *net* of
subreddits and fanning the keyword query across it. The net is assembled
from several optional, complementary sources (see :mod:`redlens.discovery`
for the first four; the CLI ``--sources`` flag selects them):

- **name** — subreddits whose name matches the topic (arctic, keyless).
- **global** — subreddits hosting matching posts anywhere on Reddit, via
  the keyless PullPush mirror (the only unscoped full-text search).
- **web** — subreddits mined from a DuckDuckGo result page (keyless,
  best-effort: DDG bot-walls automated queries).
- **popular** — the ~100 largest general subreddits, cast over wholesale.
- **llm** — one cheap LLM-suggested list, when an LLM key is configured.

On top of those, ``--discover`` does a behavioral round here: it finds the
authors of matching posts in the current net and queries them
author-scoped (arctic's one window onto unknown subreddits) to learn where
else they discuss the topic. The user also seeds/curates the net directly
(``--subreddits``, the interactive picker), and it's remembered per topic.

The pull then fans each keyword across each subreddit, dedupes by post id,
and upserts. Empty or non-existent subreddits cost one request and return
nothing, so over-casting is cheap. Re-running with an unchanged net and
keyword set is incremental via ``Topic.newest_seen_utc``.
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy import delete, func
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from redlens import arctic
from redlens.constants import (
    DISCOVER_MAX_AUTHORS,
    DISCOVER_MAX_NEW_SUBREDDITS,
    NON_AUTHORS,
)
from redlens.db import upsert
from redlens.errors import RedlensError
from redlens.models import Comment, Post, Topic, TopicPost


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


def query_terms(query: str) -> list[str]:
    """A topic's query is one or more comma-separated terms, OR'd by
    fanning out one arctic request per term — arctic ANDs all words within
    a query and has no OR operator, so 'ubi, universal basic income'
    must be two searches."""
    return [t.strip() for t in query.split(",") if t.strip()] or [query]


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
    source: str = "name"  # which discovery source(s) proposed it


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
                if author and author.lower() not in NON_AUTHORS:
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
    exclude: str | None = None,
    discover: bool = False,
    reset: bool = False,
    on_progress: Callable[[str, int], None] | None = None,
) -> TrackResult:
    """Pull every post matching the topic's keywords across its subreddit net."""
    now = _now()
    with Session(engine, expire_on_commit=False) as session:
        topic = get_topic(session, name)
        old_days = topic.days if topic else None
        old_keywords = set(topic.keyword_list) if topic else set()
        if topic is None:
            topic = Topic(name=name)
        if query:
            topic.keywords = json.dumps(query_terms(query))
        elif not topic.keyword_list:
            topic.keywords = json.dumps([name])
        if days:
            topic.days = days
        if exclude is not None:
            topic.exclude_terms = exclude
        # Persist now so topicpost rows can reference a stable topic id.
        session.add(topic)
        session.flush()
        topic_id = topic.id
        assert topic_id is not None

        terms = topic.keyword_list
        terms_changed = old_keywords != set(terms)
        if reset:
            session.execute(
                delete(TopicPost).where(TopicPost.topic_id == topic_id)  # type: ignore[arg-type]
            )
            topic.newest_seen_utc = None

        excluded = [t.lower() for t in query_terms(topic.exclude_terms)] \
            if topic.exclude_terms else []

        net = list(dict.fromkeys(
            topic.subreddit_list + (subreddits or [])
        )) or guess_home_subreddits(name)

        window_start = now - topic.days * 86400
        discovered: list[str] = []
        if discover:
            for term in terms:
                discovered += discover_subreddits(term, net, window_start, now)
            discovered = list(dict.fromkeys(discovered))
            net = list(dict.fromkeys(net + discovered))

        # Incremental only when nothing widened the result set: a grown net,
        # a longer window, or a changed keyword set each need the full window
        # so the new dimension backfills rather than only matching forward.
        net_grew = set(s.lower() for s in net) != {
            s.lower() for s in topic.subreddit_list
        }
        window_extended = old_days is not None and topic.days > old_days
        after = window_start
        if (topic.newest_seen_utc and not net_grew and not window_extended
                and not terms_changed and not reset):
            after = max(window_start, topic.newest_seen_utc)

        seen: set[str] = set()
        newest = topic.newest_seen_utc or 0
        posts_new = 0
        per_subreddit: dict[str, int] = {}
        failed: dict[str, str] = {}
        for sub in net:
            batch: list[Post] = []
            sub_failed = False
            try:
                for term in terms:
                    for raw in arctic.iter_subreddit_query(
                        sub, term, after=after, before=now
                    ):
                        pid = raw.get("id")
                        if not pid or pid in seen:
                            continue
                        seen.add(pid)
                        post = Post.from_arctic(raw)
                        newest = max(newest, post.created_utc)
                        if excluded:
                            text = f"{post.title or ''} {post.selftext or ''}".lower()
                            if any(t in text for t in excluded):
                                continue  # homonym noise, e.g. Ubisoft for "ubi"
                        batch.append(post)
            except RedlensError as exc:
                # One bad subreddit (banned, renamed, exhausted retries) must
                # not sink the whole net; keep what was fetched before the
                # failure, report, and keep casting.
                failed[sub] = str(exc)
                sub_failed = True
            if batch:
                upsert(session, batch)
                upsert(session, [
                    TopicPost(topic_id=topic_id, post_id=p.post_id)
                    for p in batch
                ])
            per_subreddit[sub] = len(batch)
            posts_new += len(batch)
            if on_progress:
                label = f"{sub} (failed, kept {len(batch)})" if sub_failed else sub
                on_progress(label, len(batch))

        topic.subreddits = json.dumps(net)
        topic.newest_seen_utc = newest or None
        topic.last_tracked_at = now
        session.add(topic)
        session.commit()

    return TrackResult(
        topic=topic,
        posts_new=posts_new,
        subreddits_searched=len(net),
        discovered=discovered,
        per_subreddit=per_subreddit,
        failed=failed,
    )


def pull_topic_comments(
    engine: Engine,
    name: str,
    *,
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """Fetch the comment threads under a topic's matched posts.

    Comments are reached through ``topicpost`` (post matched the topic) and
    stored in the shared ``comment`` table, linked by ``comment.link_id ==
    post.post_id`` — so topic-to-comment membership is derivable with no
    extra table. Idempotent: re-running upserts the same comments.
    """
    with Session(engine, expire_on_commit=False) as session:
        topic = get_topic(session, name)
        if topic is None:
            raise RedlensError(f"topic {name!r} not tracked yet")
        # Only posts that actually drew discussion are worth a request.
        post_ids = list(session.exec(
            select(Post.post_id)
            .join(TopicPost, TopicPost.post_id == Post.post_id)  # type: ignore[arg-type]
            .where(TopicPost.topic_id == topic.id, Post.num_comments > 0)
        ))
        written = 0
        for i, pid in enumerate(post_ids, 1):
            try:
                batch = [Comment.from_arctic(raw)
                         for raw in arctic.iter_post_comments(pid)]
            except RedlensError:
                continue  # one unreachable thread shouldn't sink the pull
            if batch:
                upsert(session, batch)
                written += len(batch)
            if on_progress and (i % 50 == 0 or i == len(post_ids)):
                on_progress(i, len(post_ids))
        session.commit()
    return written


def topic_comments(session: Session, name: str) -> list[Comment]:
    """Comments under a topic's matched posts (the link_id bridge)."""
    return list(session.exec(
        select(Comment)
        .join(TopicPost, TopicPost.post_id == Comment.link_id)  # type: ignore[arg-type]
        .join(Topic, Topic.id == TopicPost.topic_id)  # type: ignore[arg-type]
        .where(func.lower(Topic.name) == name.lower())
        .order_by(Comment.score.desc(), Comment.comment_id)  # type: ignore[attr-defined]
    ))

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

from sqlalchemy import ColumnElement, delete, func, or_
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from redlens import arctic
from redlens.config import llm_api_key
from redlens.constants import (
    COMMIT_BATCH,
    DISCOVER_MAX_AUTHORS,
    DISCOVER_MAX_NEW_SUBREDDITS,
    NON_AUTHORS,
)
from redlens.db import upsert
from redlens.errors import NotFound, RedlensError
from redlens.filter import FilterResult, filter_topic
from redlens.models import Comment, Post, Topic, TopicListing, TopicPost, User


def relevant_clause(min_confidence: float = 0.0) -> ColumnElement[bool]:
    """Predicate for ``topicpost`` rows a tracked topic's relevance filter kept.

    Tri-state ``relevant``: ``False`` is hidden (a judged false positive);
    ``None`` (unscored — no LLM key, or pre-filter rows) and ``True`` are kept. So
    ``IS NOT False`` keeps unscored rows, preserving keyless behavior exactly.
    Every surface that reads a topic's matched posts/comments ANDs this in so the
    soft flag is honored uniformly.

    ``min_confidence`` (0–1) makes hiding confidence-gated: a False row is hidden
    only when the model was at least that sure (``relevance_confidence >=
    min_confidence``); lower-confidence drops are kept visible. 0 (the default)
    hides every False, unchanged. Note the model is overconfident (see
    docs/relevance-filter-calibration.png), so this is a blunt knob — but confident
    drops are well-ordered, so a high threshold reliably keeps only the sure junk hidden."""
    kept = col(TopicPost.relevant).isnot(False)
    if min_confidence <= 0:
        return kept
    return or_(kept,
               col(TopicPost.relevance_confidence).is_(None),
               col(TopicPost.relevance_confidence) < min_confidence)


def _now() -> int:
    return int(time.time())


@dataclass
class UntrackResult:
    name: str
    links_removed: int       # topicpost rows deleted for this topic
    posts_deleted: int       # orphaned posts removed from the shared archive
    comments_deleted: int    # orphaned comments removed alongside those posts


@dataclass
class TrackResult:
    topic: Topic
    posts_new: int
    subreddits_searched: int
    discovered: list[str] = field(default_factory=list)
    per_subreddit: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)  # subreddit -> error
    relevance: FilterResult | None = None  # LLM relevance pass, when a key is set


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


def require_topic(session: Session, name: str) -> Topic:
    """The tracked topic ``name``, or raise :class:`NotFound` with the standard
    "run track first" guidance — the message every read path shares."""
    topic = get_topic(session, name)
    if topic is None:
        raise NotFound(f"topic {name!r} not tracked yet — run `redlens track` first")
    return topic


def list_topics(session: Session) -> list[TopicListing]:
    """Roll up every tracked topic: keywords, net size, matched-post count,
    and when each was last tracked. Sorted most-recently-tracked first.

    The matched-post count comes from one grouped ``topicpost`` query (not a
    per-topic scan) so this stays cheap as the archive grows; topics never
    yet pulled simply count zero.
    """
    topics = session.exec(select(Topic)).all()
    if not topics:
        return []

    counts: dict[int, int] = dict(session.exec(
        select(col(TopicPost.topic_id), func.count())
        .where(relevant_clause())
        .group_by(col(TopicPost.topic_id))
    ).all())

    listings = [
        TopicListing(
            name=t.name,
            keywords=t.keyword_list,
            subreddit_count=len(t.subreddit_list),
            matched_posts=counts.get(t.id, 0) if t.id is not None else 0,
            last_tracked_at=t.last_tracked_at,
        )
        for t in topics
    ]
    listings.sort(key=lambda r: (r.last_tracked_at or 0), reverse=True)
    return listings


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
    about: str | None = None,
    discover: bool = False,
    reset: bool = False,
    on_progress: Callable[[str, int], None] | None = None,
    on_filter: Callable[[int], None] | None = None,
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
        if about is not None:
            topic.about = about
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
        incremental = bool(
            topic.newest_seen_utc and not net_grew and not window_extended
            and not terms_changed and not reset)
        after = window_start
        if incremental:
            # -1 so the boundary second is re-queried (arctic's `after` is a
            # strict >); same-second siblings dedup on upsert rather than vanish.
            after = max(window_start, (topic.newest_seen_utc or 0) - 1)

        seen: set[str] = set()
        newest = topic.newest_seen_utc or 0
        posts_new = 0
        per_subreddit: dict[str, int] = {}
        failed: dict[str, str] = {}

        def flush(batch: list[Post]) -> None:
            """Persist a chunk of posts + their topic links and commit, so a
            single huge subreddit never balloons memory or the transaction."""
            if not batch:
                return
            upsert(session, batch)
            # update=False so re-linking an already-matched post on a full
            # re-pull keeps its stored relevance verdict instead of resetting it
            # to NULL (the verdict columns are TopicPost's only non-PK state).
            upsert(session, [TopicPost(topic_id=topic_id, post_id=p.post_id)
                             for p in batch], update=False)
            session.commit()
            batch.clear()

        for sub in net:
            batch: list[Post] = []
            kept = 0
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
                        kept += 1
                        if len(batch) >= COMMIT_BATCH:
                            flush(batch)
            except RedlensError as exc:
                # One bad subreddit (banned, renamed, exhausted retries) must
                # not sink the whole net; keep what was fetched before the
                # failure, report, and keep casting.
                failed[sub] = str(exc)
                sub_failed = True
            flush(batch)
            per_subreddit[sub] = kept
            posts_new += kept
            if on_progress:
                label = f"{sub} (failed, kept {kept})" if sub_failed else sub
                on_progress(label, kept)

        topic.subreddits = json.dumps(net)
        # The cursor is a single high-water mark across the whole net. Advance it
        # only when every subreddit succeeded — otherwise a transiently-failed
        # sub would be queried with after=<that mark> next time and never
        # re-fetch its older posts (silent loss).
        if not failed:
            topic.newest_seen_utc = newest or None
        elif not incremental:
            # A widened/full re-pull (grown net, changed keywords, longer window,
            # or reset) that partially failed: the widened net/keywords/days are
            # already persisted, so the next run won't see net_grew /
            # terms_changed / window_extended and would go incremental from the
            # stale cursor, skipping the failed slice's older posts. Drop the
            # cursor so the next run re-pulls the full window until a clean pass.
            topic.newest_seen_utc = None
        # else: incremental top-up with a transient failure — the failed sub was
        # already covered down to the old cursor, so keeping it is correct.
        topic.last_tracked_at = now
        session.add(topic)
        session.commit()

        # Relevance pass: when an LLM key is set, classify this topic's UNSCORED
        # matches (relevant IS NULL) and flag the false positives — the brand-name
        # homonym noise substring search can't avoid. Scoring the unscored set,
        # not just this run's new matches, also back-fills posts matched earlier
        # without a key (e.g. the user just added one); already-scored rows are
        # non-NULL so they're never re-paid. Skipped silently with no key, so
        # keyless `track` is unchanged. Runs after the commit above, so a filter
        # failure never loses the fetched archive.
        relevance: FilterResult | None = None
        key = llm_api_key()
        if key:
            unscored = list(session.exec(
                select(TopicPost.post_id).where(
                    TopicPost.topic_id == topic_id,
                    col(TopicPost.relevant).is_(None))))
            if unscored:
                # Heads-up before a potentially large (and paid) LLM pass: the
                # first keyed track of a big pre-existing topic back-fills every
                # unscored row at once, so report the count we're about to spend on.
                if on_filter:
                    on_filter(len(unscored))
                relevance = filter_topic(session, topic, unscored, key,
                                         about=topic.about)

    return TrackResult(
        topic=topic,
        posts_new=posts_new,
        subreddits_searched=len(net),
        discovered=discovered,
        per_subreddit=per_subreddit,
        failed=failed,
        relevance=relevance,
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
        # Skip false-positive posts: fetching comment threads for off-topic
        # matches wastes arctic requests (and stores junk). Unscored/on-topic
        # posts are still pulled, so keyless behavior is unchanged.
        post_ids = list(session.exec(
            select(Post.post_id)
            .join(TopicPost, TopicPost.post_id == Post.post_id)  # type: ignore[arg-type]
            .where(TopicPost.topic_id == topic.id, Post.num_comments > 0,
                   relevant_clause())
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


def topic_posts(session: Session, name: str, min_confidence: float = 0.0) -> list[Post]:
    """Posts matched to a topic, highest-scoring first (post_id tie-break
    keeps the order deterministic, matching the rendered page). ``min_confidence``
    keeps low-confidence drops visible (see :func:`relevant_clause`)."""
    return list(session.exec(
        select(Post)
        .join(TopicPost, TopicPost.post_id == Post.post_id)  # type: ignore[arg-type]
        .join(Topic, Topic.id == TopicPost.topic_id)  # type: ignore[arg-type]
        .where(func.lower(Topic.name) == name.lower(), relevant_clause(min_confidence))
        .order_by(Post.score.desc(), Post.post_id)  # type: ignore[attr-defined]
    ))


def topic_hidden_posts(session: Session, name: str,
                       min_confidence: float = 0.0) -> list[tuple[Post, float | None, str | None]]:
    """The matched posts the relevance filter is currently *hiding* (judged
    off-topic), each with the model's confidence + reason — for the page's
    "reveal hidden matches" toggle. Mirrors :func:`relevant_clause`: at
    ``min_confidence`` only the drops at/above that confidence are hidden, so this
    returns exactly the complement of what :func:`topic_posts` shows."""
    hidden: list[ColumnElement[bool]] = [col(TopicPost.relevant).is_(False)]
    if min_confidence > 0:
        hidden += [col(TopicPost.relevance_confidence).isnot(None),
                   col(TopicPost.relevance_confidence) >= min_confidence]
    rows = session.exec(
        select(Post, TopicPost.relevance_confidence, TopicPost.relevance_reason)
        .join(TopicPost, TopicPost.post_id == Post.post_id)  # type: ignore[arg-type]
        .join(Topic, Topic.id == TopicPost.topic_id)  # type: ignore[arg-type]
        .where(func.lower(Topic.name) == name.lower(), *hidden)
        .order_by(Post.score.desc(), Post.post_id)  # type: ignore[attr-defined]
    ).all()
    return [(p, conf, reason) for p, conf, reason in rows]


def topic_comments(session: Session, name: str, min_confidence: float = 0.0) -> list[Comment]:
    """Comments under a topic's matched posts (the link_id bridge)."""
    return list(session.exec(
        select(Comment)
        .join(TopicPost, TopicPost.post_id == Comment.link_id)  # type: ignore[arg-type]
        .join(Topic, Topic.id == TopicPost.topic_id)  # type: ignore[arg-type]
        .where(func.lower(Topic.name) == name.lower(), relevant_clause(min_confidence))
        .order_by(Comment.score.desc(), Comment.comment_id)  # type: ignore[attr-defined]
    ))


def _chunked(items: list[str], size: int = 400) -> list[list[str]]:
    """Split an id list into SQLite-IN-safe chunks (the variadic limit is
    ~999; 400 leaves headroom for other bound params)."""
    return [items[i:i + size] for i in range(0, len(items), size)]


def untrack_topic(engine: Engine, name: str) -> UntrackResult:
    """Remove a tracked topic and garbage-collect only the rows it alone kept.

    Deletes the ``Topic`` row and its ``topicpost`` links, then drops a matched
    post (and the comments riding under it via ``link_id``) only when this was
    the *sole* reason to keep it: no other topic still tags the post, and its
    author isn't a synced user. So posts shared across topics and posts/comments
    belonging to a user-sync archive survive — only orphaned matches are dropped.
    """
    with Session(engine, expire_on_commit=False) as session:
        topic = get_topic(session, name)
        if topic is None:
            raise NotFound(f"topic {name!r} is not tracked")
        topic_id = topic.id
        assert topic_id is not None
        topic_name = topic.name

        my_post_ids = set(session.exec(
            select(TopicPost.post_id).where(TopicPost.topic_id == topic_id)
        ).all())
        # One topicpost row per (topic, post), so the link count is exactly
        # how many posts this topic tagged.
        links_removed = len(my_post_ids)

        session.execute(
            delete(TopicPost).where(TopicPost.topic_id == topic_id)  # type: ignore[arg-type]
        )
        session.delete(topic)
        session.flush()

        posts_deleted = 0
        comments_deleted = 0
        if my_post_ids:
            # Posts still tagged by some *other* topic must stay; so must posts
            # whose author is a synced user (part of that user's archive).
            still_linked = set(session.exec(select(TopicPost.post_id)).all())
            synced = set(session.exec(select(User.username)).all())
            candidates = my_post_ids - still_linked

            orphan_posts: list[str] = []
            for chunk in _chunked(list(candidates)):
                for pid, author in session.exec(
                    select(Post.post_id, Post.author_username)
                    .where(col(Post.post_id).in_(chunk))
                ).all():
                    if author not in synced:
                        orphan_posts.append(pid)

            # A synced user may have *commented* under an orphan post; deleting
            # the post would leave their archived comment dangling (its link_id
            # pointing at a gone post). Keep any such post — and everything under
            # it — by dropping it from the orphan set.
            if orphan_posts:
                synced_commented: set[str] = set()
                for chunk in _chunked(orphan_posts):
                    for link_id, c_author in session.exec(
                        select(Comment.link_id, Comment.author_username)
                        .where(col(Comment.link_id).in_(chunk))
                    ).all():
                        if c_author in synced:
                            synced_commented.add(link_id)
                orphan_posts = [p for p in orphan_posts
                                if p not in synced_commented]

            for chunk in _chunked(orphan_posts):
                orphan_comments = [
                    cid for cid, author in session.exec(
                        select(Comment.comment_id, Comment.author_username)
                        .where(col(Comment.link_id).in_(chunk))
                    ).all() if author not in synced
                ]
                for cchunk in _chunked(orphan_comments):
                    session.execute(
                        delete(Comment).where(col(Comment.comment_id).in_(cchunk))
                    )
                    comments_deleted += len(cchunk)
                session.execute(
                    delete(Post).where(col(Post.post_id).in_(chunk))
                )
                posts_deleted += len(chunk)

        session.commit()

    return UntrackResult(
        name=topic_name,
        links_removed=links_removed,
        posts_deleted=posts_deleted,
        comments_deleted=comments_deleted,
    )

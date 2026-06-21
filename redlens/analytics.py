from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, literal, union_all
from sqlmodel import Session, col, select

from redlens import constants
from redlens.errors import NotFound
from redlens.models import (
    Comment,
    NameCount,
    Post,
    SyncState,
    TopicAnalytics,
    TopicPost,
    User,
    UserAnalytics,
    UserListing,
)
from redlens.topics import require_topic


def compute_user_analytics(session: Session, username: str) -> UserAnalytics:
    user = session.exec(
        select(User).where(func.lower(User.username) == username.lower())
    ).first()
    if user is None:
        raise NotFound(f"u/{username} not in DB — sync first")

    canon = user.username

    # One UNION ALL over (subreddit_name, created_utc, score, kind) lets every
    # measure be a SQL aggregate — nothing is loaded into Python. ``kind`` keeps
    # post and comment counts/karma separable without extra table scans.
    posts = select(
        *[
            col(Post.subreddit_name).label("subreddit_name"),
            col(Post.created_utc).label("created_utc"),
            col(Post.score).label("score"),
            literal("post").label("kind"),
        ]
    ).where(Post.author_username == canon)
    comments = select(
        *[
            col(Comment.subreddit_name).label("subreddit_name"),
            col(Comment.created_utc).label("created_utc"),
            col(Comment.score).label("score"),
            literal("comment").label("kind"),
        ]
    ).where(Comment.author_username == canon)
    events = union_all(posts, comments).subquery()

    is_post = events.c.kind == "post"
    row = session.execute(
        select(
            *[
                func.coalesce(func.sum(case((is_post, 1), else_=0)), 0).label("total_posts"),
                func.coalesce(func.sum(case((is_post, 0), else_=1)), 0).label("total_comments"),
                func.coalesce(
                    func.sum(case((is_post, events.c.score), else_=0)), 0
                ).label("post_karma"),
                func.coalesce(
                    func.sum(case((is_post, 0), else_=events.c.score)), 0
                ).label("comment_karma"),
                func.min(events.c.created_utc).label("first_event_at"),
                func.max(events.c.created_utc).label("last_event_at"),
                func.count(
                    func.distinct(func.date(events.c.created_utc, "unixepoch"))
                ).label("active_days"),
                func.count(func.distinct(events.c.subreddit_name)).label("distinct_subreddits"),
            ]
        )
    ).one()

    # Top subreddit by combined post+comment event count. Ties break on name so
    # the result is deterministic across runs.
    top = session.execute(
        select(events.c.subreddit_name, func.count().label("n"))
        .group_by(events.c.subreddit_name)
        .order_by(func.count().desc(), events.c.subreddit_name)
        .limit(1)
    ).first()

    return UserAnalytics(
        username=canon,
        total_posts=row.total_posts,
        total_comments=row.total_comments,
        post_karma=row.post_karma,
        comment_karma=row.comment_karma,
        total_karma=row.post_karma + row.comment_karma,
        first_event_at=row.first_event_at,
        last_event_at=row.last_event_at,
        active_days=row.active_days,
        distinct_subreddits=row.distinct_subreddits,
        top_subreddit=top.subreddit_name if top else None,
        top_subreddit_event_count=top.n if top else 0,
    )


def compute_topic_analytics(session: Session, name: str) -> TopicAnalytics:
    """Roll up a tracked topic's matched posts — the topic-side mirror of
    :func:`compute_user_analytics`. Every measure is a SQL aggregate over the
    ``topicpost`` join (the 0007 in-SQL convention); nothing is loaded into
    Python row-by-row.
    """
    topic = require_topic(session, name)

    # Every measure below is a SQL aggregate over the topic's matched posts
    # (the topicpost join), so nothing is loaded into Python row-by-row.
    def _scoped(*columns: Any) -> Any:
        return (
            select(*columns)
            .select_from(Post)
            .join(TopicPost, TopicPost.post_id == Post.post_id)
            .where(TopicPost.topic_id == topic.id)
        )

    def _top(column: Any, limit: int, extra_where: Any = None) -> list[Any]:
        q = _scoped(column, func.count().label("n"))
        if extra_where is not None:
            q = q.where(extra_where)
        return list(session.execute(
            q.group_by(column).order_by(func.count().desc(), column).limit(limit)
        ).all())

    row = session.execute(_scoped(
        func.count().label("matched_posts"),
        func.coalesce(func.sum(Post.score), 0).label("total_score"),
        func.min(Post.created_utc).label("first_post_at"),
        func.max(Post.created_utc).label("last_post_at"),
        func.count(func.distinct(Post.subreddit_name)).label("distinct_subreddits"),
    )).one()

    top_subreddits = _top(col(Post.subreddit_name), constants.TOP_SUBREDDITS)
    # Drop bot/placeholder names so "top authors" reflects real voices.
    top_authors = _top(
        col(Post.author_username), constants.TOP_AUTHORS,
        func.lower(Post.author_username).notin_(constants.NON_AUTHORS),
    )

    return TopicAnalytics(
        name=topic.name,
        keywords=topic.keyword_list,
        net_size=len(topic.subreddit_list),
        matched_posts=row.matched_posts,
        total_score=row.total_score,
        distinct_subreddits=row.distinct_subreddits,
        first_post_at=row.first_post_at,
        last_post_at=row.last_post_at,
        last_tracked_at=topic.last_tracked_at,
        top_subreddits=[NameCount(name=n, count=c) for n, c in top_subreddits],
        top_authors=[NameCount(name=a, count=c) for a, c in top_authors],
    )


def list_users(session: Session) -> list[UserListing]:
    """Roll up every user in the DB: post/comment counts, last activity, and
    when each was last synced. Sorted most-recently-active first.

    Counts and last-event come from grouped SQL (one query per stream) so this
    stays cheap as the archive grows; ``last_synced_at`` uses the newest
    ``sync_state`` row for the user, falling back to when the row was fetched.
    """
    users = session.exec(select(User)).all()
    if not users:
        return []

    def _agg(model: type[Post] | type[Comment]) -> dict[str, tuple[int, int | None]]:
        rows: list[tuple[str, int, int | None]] = list(session.exec(
            select(model.author_username, func.count(), func.max(model.created_utc))
            .group_by(model.author_username)
        ).all())
        return {author: (count, newest) for author, count, newest in rows}

    posts = _agg(Post)
    comments = _agg(Comment)
    synced_rows: list[tuple[str, int | None]] = list(session.exec(
        select(SyncState.username, func.max(SyncState.synced_at))
        .group_by(SyncState.username)
    ).all())
    synced = dict(synced_rows)

    listings = []
    for u in users:
        post_count, post_newest = posts.get(u.username, (0, None))
        comment_count, comment_newest = comments.get(u.username, (0, None))
        last_event = max((t for t in (post_newest, comment_newest) if t is not None),
                         default=None)
        listings.append(UserListing(
            username=u.username,
            total_posts=post_count,
            total_comments=comment_count,
            last_event_at=last_event,
            last_synced_at=synced.get(u.username) or u.fetched_at,
        ))
    listings.sort(key=lambda r: (r.last_event_at or 0), reverse=True)
    return listings

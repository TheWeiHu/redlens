from __future__ import annotations

from sqlalchemy import case, func, literal, union_all
from sqlmodel import Session, col, select

from redlens.errors import NotFound
from redlens.models import Comment, Post, User, UserAnalytics


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

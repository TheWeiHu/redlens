from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import func
from sqlmodel import Session, select

from redditpages.errors import NotFound
from redditpages.models import Comment, Post, User, UserAnalytics


def compute_user_analytics(session: Session, username: str) -> UserAnalytics:
    user = session.exec(
        select(User).where(func.lower(User.username) == username.lower())
    ).first()
    if user is None:
        raise NotFound(f"u/{username} not in DB — sync first")

    canon = user.username
    posts = session.exec(select(Post).where(Post.author_username == canon)).all()
    comments = session.exec(select(Comment).where(Comment.author_username == canon)).all()

    post_karma = sum(p.score for p in posts)
    comment_karma = sum(c.score for c in comments)
    timestamps = [p.created_utc for p in posts] + [c.created_utc for c in comments]
    days = {datetime.fromtimestamp(ts, tz=UTC).date() for ts in timestamps}

    sub_events: Counter[str] = Counter(p.subreddit_name for p in posts)
    sub_events.update(c.subreddit_name for c in comments)
    top = sub_events.most_common(1)

    return UserAnalytics(
        username=canon,
        total_posts=len(posts),
        total_comments=len(comments),
        post_karma=post_karma,
        comment_karma=comment_karma,
        total_karma=post_karma + comment_karma,
        first_event_at=min(timestamps, default=None),
        last_event_at=max(timestamps, default=None),
        active_days=len(days),
        distinct_subreddits=len(sub_events),
        top_subreddit=top[0][0] if top else None,
        top_subreddit_event_count=top[0][1] if top else 0,
    )

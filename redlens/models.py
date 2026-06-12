from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel
from sqlmodel import Field, SQLModel


def _now() -> int:
    return int(time.time())


class User(SQLModel, table=True):
    """A Reddit account plus its arctic activity stats.

    Arctic returns the stats as a flat ``_meta`` blob — the same five measures
    for posts and for comments. They are one-per-user, so they live as plain
    columns on this row (atomic, 1NF) rather than a JSON blob. ``total_karma``
    is dropped: it is just ``post_karma + comment_karma``. All stat columns are
    null when arctic has no ``_meta`` for the account.
    """

    username: str = Field(primary_key=True)
    author_fullname: str | None = None

    num_posts: int | None = None
    num_comments: int | None = None
    post_karma: int | None = None
    comment_karma: int | None = None
    earliest_post_at: int | None = None
    last_post_at: int | None = None
    earliest_comment_at: int | None = None
    last_comment_at: int | None = None
    post_stats_updated_at: int | None = None     # when arctic last recomputed post stats
    comment_stats_updated_at: int | None = None  # ditto for comment stats
    fetched_at: int = Field(default_factory=_now)

    @classmethod
    def from_arctic(cls, raw: dict[str, Any]) -> User:
        meta = raw.get("_meta") or {}
        return cls(
            username=raw["author"],
            author_fullname=raw.get("id"),
            num_posts=meta.get("num_posts"),
            num_comments=meta.get("num_comments"),
            post_karma=meta.get("post_karma"),
            comment_karma=meta.get("comment_karma"),
            earliest_post_at=meta.get("earliest_post_at"),
            last_post_at=meta.get("last_post_at"),
            earliest_comment_at=meta.get("earliest_comment_at"),
            last_comment_at=meta.get("last_comment_at"),
            post_stats_updated_at=meta.get("post_stats_updated_at"),
            comment_stats_updated_at=meta.get("comment_stats_updated_at"),
        )


class Post(SQLModel, table=True):
    post_id: str = Field(primary_key=True)
    author_username: str = Field(index=True)
    subreddit_name: str = Field(index=True)
    created_utc: int = Field(index=True)
    title: str | None = None
    selftext: str | None = None
    url: str | None = None
    score: int = 0
    num_comments: int = 0
    over_18: bool = False  # Reddit's NSFW flag, per post
    fetched_at: int = Field(default_factory=_now)

    @classmethod
    def from_arctic(cls, raw: dict[str, Any]) -> Post:
        return cls(
            post_id=raw["id"],
            author_username=raw["author"],
            subreddit_name=raw["subreddit"],
            created_utc=int(raw["created_utc"]),
            title=raw.get("title"),
            selftext=raw.get("selftext") or None,
            url=raw.get("url"),
            score=int(raw.get("score") or 0),
            num_comments=int(raw.get("num_comments") or 0),
            over_18=bool(raw.get("over_18", False)),
        )


class Comment(SQLModel, table=True):
    comment_id: str = Field(primary_key=True)
    author_username: str = Field(index=True)
    subreddit_name: str
    link_id: str
    parent_id: str | None = None
    created_utc: int
    body: str | None = None
    score: int = 0
    fetched_at: int = Field(default_factory=_now)

    @classmethod
    def from_arctic(cls, raw: dict[str, Any]) -> Comment:
        link_id = raw["link_id"]
        if link_id.startswith("t3_"):
            link_id = link_id[3:]
        return cls(
            comment_id=raw["id"],
            author_username=raw["author"],
            subreddit_name=raw["subreddit"],
            link_id=link_id,
            parent_id=raw.get("parent_id"),
            created_utc=int(raw["created_utc"]),
            body=raw.get("body"),
            score=int(raw.get("score") or 0),
        )


class Topic(SQLModel, table=True):
    """A tracked subject: a full-text query fanned out over a subreddit net.

    Arctic has no global text search (queries must be scoped to a subreddit
    or author), so each topic carries its own subreddit list — the net. The
    list is a JSON array; it grows via ``--subreddits`` or ``--discover``.
    ``newest_seen_utc`` is the incremental cursor: re-tracking with an
    unchanged net only fetches newer posts.
    """

    name: str = Field(primary_key=True)
    query: str
    subreddits: str = "[]"               # JSON array of subreddit names
    days: int = 180                       # trailing window for full pulls
    newest_seen_utc: int | None = None    # incremental cursor
    last_tracked_at: int | None = None
    fetched_at: int = Field(default_factory=_now)

    @property
    def subreddit_list(self) -> list[str]:
        return [str(s) for s in json.loads(self.subreddits)]


class TopicPost(SQLModel, table=True):
    """Join table tagging which posts belong to which tracked topic.

    Posts stay in the shared ``post`` table (a post can match several
    topics, and user-sync and topic-track share the same archive).
    """

    topic_name: str = Field(primary_key=True, index=True)
    post_id: str = Field(primary_key=True, index=True)


class UserAnalytics(BaseModel):
    username: str
    total_posts: int
    total_comments: int
    post_karma: int
    comment_karma: int
    total_karma: int
    first_event_at: int | None
    last_event_at: int | None
    active_days: int
    distinct_subreddits: int
    top_subreddit: str | None
    top_subreddit_event_count: int

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


class SyncState(SQLModel, table=True):
    """Per-user, per-stream cursors that make ``sync`` incremental.

    A full re-pull of a user's whole history every run is wasteful and impolite
    to arctic (a donation-funded mirror). This row records, for one
    ``(username, kind, provider)`` stream, how far sync has reached:

    - ``newest_seen_utc`` — the high-water mark; the next incremental sync asks
      arctic only for items created *after* it.
    - ``oldest_seen_utc`` — the low-water mark; if a backfill was interrupted
      mid-walk (the stream pages newest-first), the next run resumes from here
      instead of starting over.
    - ``completed_backfill`` — True once the backward walk reached the end of
      history. Until then sync keeps extending the backfill; after, it switches
      to cheap forward-only incremental pulls.

    ``provider`` is part of the key so a future second source can carry its own
    cursors without colliding with arctic's.
    """

    __tablename__ = "sync_state"

    username: str = Field(primary_key=True)
    kind: str = Field(primary_key=True)          # "posts" | "comments"
    provider: str = Field(primary_key=True, default="arctic")
    newest_seen_utc: int | None = None
    oldest_seen_utc: int | None = None
    completed_backfill: bool = False
    synced_at: int = Field(default_factory=_now)


class Topic(SQLModel, table=True):
    """A tracked subject: a full-text query fanned out over a subreddit net.

    Arctic has no global text search (queries must be scoped to a subreddit
    or author), so each topic carries its own subreddit list — the net. The
    list is a JSON array; it grows via ``--subreddits`` or ``--discover``.
    ``newest_seen_utc`` is the incremental cursor: re-tracking with an
    unchanged net only fetches newer posts.
    """

    id: int | None = Field(default=None, primary_key=True)
    name: str = Field(unique=True, index=True)   # the CLI handle
    keywords: str = "[]"                 # JSON array; terms OR'd at fetch time
    subreddits: str = "[]"               # JSON array of subreddit names
    days: int = 180                       # trailing window for full pulls
    exclude_terms: str = ""              # comma-separated; matching posts dropped
    newest_seen_utc: int | None = None    # incremental cursor
    last_tracked_at: int | None = None
    fetched_at: int = Field(default_factory=_now)

    @property
    def keyword_list(self) -> list[str]:
        return [str(k) for k in json.loads(self.keywords)]

    @property
    def subreddit_list(self) -> list[str]:
        return [str(s) for s in json.loads(self.subreddits)]


class TopicPost(SQLModel, table=True):
    """Join table tagging which posts belong to which tracked topic.

    Keyed on ``topic_id`` (not name) so a topic's name or keywords can
    change without orphaning its matches. Posts stay in the shared
    ``post`` table (a post can match several topics, and user-sync and
    topic-track share the same archive).
    """

    topic_id: int = Field(primary_key=True, index=True)
    post_id: str = Field(primary_key=True, index=True)


class Guess(BaseModel):
    """One ranked inference: a label, a 0-100 confidence, and the evidence."""
    label: str
    confidence: int = 0
    reason: str = ""


class Trait(BaseModel):
    """A Big Five trait: a 0-100 strength score and the evidence."""
    score: int = 0
    reason: str = ""


class Profile(BaseModel):
    """An AI-inferred profile, generated on demand and not persisted.

    The model returns this as structured JSON (not prose) so consumers — the
    CLI, ``--json``, an HTML view — render it deterministically instead of
    parsing freeform text. ``demographics`` maps a field (``gender``,
    ``age_range``, ``country``, ``state``, ``city``) to ranked guesses;
    ``big_five`` maps each OCEAN trait to its score. It's cheap to regenerate
    and depends on the changing archive, so nothing is cached in the DB.
    """

    username: str
    model: str   # which LLM produced it
    depth: str   # sampling preset used
    demographics: dict[str, list[Guess]] = {}
    big_five: dict[str, Trait] = {}
    interests: str = ""
    beliefs: str = ""
    tone: str = ""


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


class UserListing(BaseModel):
    """One row of ``redlens list`` — a lightweight per-user roll-up.

    Cheaper than ``UserAnalytics``: just the counts and the two timestamps a
    human scanning the archive cares about (when the account was last active,
    and when redlens last synced it).
    """

    username: str
    total_posts: int
    total_comments: int
    last_event_at: int | None    # newest post/comment created_utc
    last_synced_at: int | None   # most recent sync for this user


class TopicListing(BaseModel):
    """One row of ``redlens topics`` — a lightweight per-topic roll-up.

    The topic-surface parallel to :class:`UserListing`: the few facts a
    human scanning their tracked topics cares about — the keywords being
    queried, how wide the subreddit net is, how many posts have matched,
    and when it was last tracked.
    """

    name: str
    keywords: list[str]
    subreddit_count: int         # size of the topic's subreddit net
    matched_posts: int           # posts tagged to this topic in topicpost
    last_tracked_at: int | None  # when track last ran for this topic


class NameCount(BaseModel):
    """A named tally — one ranked (subreddit|author, count) pair."""
    name: str
    count: int


class TopicAnalytics(BaseModel):
    """A tracked topic's roll-up: the topic-side mirror of ``UserAnalytics``.

    Counts cover the topic's *matched* posts (the ``topicpost`` join), not the
    whole archive. ``net_size`` is how many subreddits the net casts over;
    ``distinct_subreddits`` is how many of them actually produced a match.
    """

    name: str
    keywords: list[str]
    net_size: int                       # subreddits in the cast net
    matched_posts: int
    total_score: int
    distinct_subreddits: int            # net subs with at least one match
    first_post_at: int | None
    last_post_at: int | None
    last_tracked_at: int | None
    top_subreddits: list[NameCount]
    top_authors: list[NameCount]

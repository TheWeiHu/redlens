from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel
from sqlmodel import Field, SQLModel


def _now() -> int:
    return int(time.time())


class User(SQLModel, table=True):
    username: str = Field(primary_key=True)
    author_fullname: str | None = None
    fetched_at: int = Field(default_factory=_now)

    @classmethod
    def from_arctic(cls, raw: dict[str, Any]) -> User:
        return cls(username=raw["author"], author_fullname=raw.get("id"))


class UserStat(SQLModel, table=True):
    """Per-kind activity stats for a user — one row per (username, kind) with
    ``kind`` in {'post', 'comment'}.

    Arctic hands these back as a single flat ``_meta`` blob in which the same
    five measures repeat once for posts and once for comments. Storing that
    repeating group as rows rather than a JSON column is what keeps the schema
    in first normal form. ``total_karma`` is intentionally dropped — it is just
    the sum of the two ``karma`` rows.
    """

    username: str = Field(primary_key=True, index=True)
    kind: str = Field(primary_key=True)          # 'post' | 'comment'
    event_count: int | None = None               # arctic num_posts / num_comments
    karma: int | None = None                     # arctic post_karma / comment_karma
    earliest_at: int | None = None               # epoch of first post / comment
    last_at: int | None = None                   # epoch of latest post / comment
    stats_updated_at: int | None = None          # when arctic last recomputed the above
    fetched_at: int = Field(default_factory=_now)

    @classmethod
    def rows_from_arctic(
        cls, username: str, meta: dict[str, Any] | None
    ) -> list[UserStat]:
        if not meta:
            return []
        out: list[UserStat] = []
        for kind, plural in (("post", "posts"), ("comment", "comments")):
            out.append(cls(
                username=username,
                kind=kind,
                event_count=meta.get(f"num_{plural}"),
                karma=meta.get(f"{kind}_karma"),
                earliest_at=meta.get(f"earliest_{kind}_at"),
                last_at=meta.get(f"last_{kind}_at"),
                stats_updated_at=meta.get(f"{kind}_stats_updated_at"),
            ))
        return out


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


class Subreddit(SQLModel, table=True):
    """One row per subreddit we have seen — in a post, comment, or mod list.

    A stable identity/dimension table: it does not carry moderator-scrape
    provenance (that belongs on the ``moderator`` rows), so re-scraping a mod
    list never churns these rows. Right now the only intrinsic fact we hold is
    the name; it exists so posts, comments, and moderators have a subreddit to
    reference.
    """

    name: str = Field(primary_key=True)
    fetched_at: int = Field(default_factory=_now)


class Moderator(SQLModel, table=True):
    """One row per (subreddit, moderator).

    Reddit gated logged-out moderator lists in 2021, so most of this data comes
    from Internet Archive snapshots. ``as_of_date`` records the date the row was
    actually accurate (the snapshot date) — not when we fetched it. That
    snapshot provenance rides with the moderator rows (it describes a scrape,
    not the subreddit itself).
    """

    subreddit_name: str = Field(primary_key=True, index=True)
    moderator_username: str = Field(primary_key=True, index=True)
    rank: int = 0                       # position in the list (1 = most senior)
    as_of_date: str | None = None       # YYYY-MM-DD the data was correct (snapshot date)
    as_of_utc: int | None = None        # epoch of that snapshot
    snapshot_timestamp: str | None = None   # raw Wayback 14-digit timestamp
    source: str | None = None           # e.g. "about-page", "front-page sidebar"
    list_complete: bool = True          # False if the sub's mod list is partial/capped
    fetched_at: int = Field(default_factory=_now)


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

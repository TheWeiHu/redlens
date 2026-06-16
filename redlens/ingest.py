from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from redlens import arctic, config
from redlens.db import upsert
from redlens.errors import NotFound
from redlens.models import Comment, Post, User
from redlens.providers import reddit

T = TypeVar("T", bound=SQLModel)
BATCH_SIZE = 500


@dataclass(frozen=True)
class SyncResult:
    user: User
    posts_written: int
    comments_written: int
    # Items seen in the optional Reddit top-up pass (0 when no credentials).
    reddit_posts: int = 0
    reddit_comments: int = 0


def sync_user(username: str, engine: Engine) -> SyncResult:
    raw = arctic.fetch_user_meta(username)
    if raw is not None:
        user = User.from_arctic(raw)
    else:
        # arctic's user-object index lags the content dumps — recent or
        # low-volume accounts often have posts/comments but no user entry.
        # Peek the first available item to recover canonical casing + fullname.
        first = next(arctic.iter_posts(username), None) \
             or next(arctic.iter_comments(username), None)
        if first is None:
            raise NotFound(f"u/{username} not in arctic")
        user = User(
            username=first.get("author") or username,
            author_fullname=first.get("author_fullname"),
        )

    with Session(engine) as session:
        upsert(session, [user])
        posts = _stream(session, arctic.iter_posts(user.username), Post.from_arctic)
        comments = _stream(session, arctic.iter_comments(user.username), Comment.from_arctic)
        r_posts, r_comments = _reddit_topup(session, user.username)
        session.commit()
    return SyncResult(user, posts, comments, r_posts, r_comments)


def _reddit_topup(session: Session, username: str) -> tuple[int, int]:
    """Pull fresh posts/comments from Reddit's official API when credentials
    are configured. A no-op (0, 0) otherwise — arctic-only behavior is
    unchanged. Upserts share the archive, so re-runs stay idempotent."""
    creds = config.reddit_credentials()
    if creds is None:
        return 0, 0
    token = reddit.get_token(*creds)
    posts = _stream(session, reddit.iter_submitted(token, username), Post.from_reddit)
    comments = _stream(session, reddit.iter_comments(token, username), Comment.from_reddit)
    return posts, comments


def _stream(
    session: Session,
    source: Iterator[dict[str, Any]],
    parse: Callable[[dict[str, Any]], T],
) -> int:
    batch: list[T] = []
    written = 0
    for raw in source:
        batch.append(parse(raw))
        if len(batch) >= BATCH_SIZE:
            written += upsert(session, batch)
            batch.clear()
    if batch:
        written += upsert(session, batch)
    return written

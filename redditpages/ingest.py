from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from redditpages import arctic
from redditpages.db import insert_ignore, upsert
from redditpages.errors import NotFound
from redditpages.models import Comment, Post, Subreddit, User

T = TypeVar("T", bound=SQLModel)
BATCH_SIZE = 500


@dataclass(frozen=True)
class SyncResult:
    user: User
    posts_written: int
    comments_written: int


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
        _register_subreddits(session, user.username)
        session.commit()
    return SyncResult(user, posts, comments)


def _register_subreddits(session: Session, username: str) -> None:
    """Add any subreddits this user touched to the stable subreddit dimension.

    Insert-only: existing rows are left untouched so the dimension does not
    churn on every re-sync.
    """
    names: set[str] = set()
    for model in (Post, Comment):
        names.update(session.exec(
            select(model.subreddit_name).where(model.author_username == username)
        ))
    insert_ignore(session, [Subreddit(name=n) for n in names])


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

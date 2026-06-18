from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel

from redlens import arctic
from redlens.db import upsert
from redlens.errors import NotFound
from redlens.models import Comment, Post, SyncState, User

T = TypeVar("T", bound=SQLModel)
BATCH_SIZE = 500


class _KindIter(Protocol):
    """An arctic stream of one kind (posts or comments) for a user, bounded by
    ``after``/``before`` epoch seconds — i.e. ``arctic.iter_posts`` /
    ``arctic.iter_comments``."""

    def __call__(
        self, username: str, after: int | None = ..., before: int | None = ...
    ) -> Iterator[dict[str, Any]]: ...


@dataclass(frozen=True)
class SyncResult:
    user: User
    posts_written: int
    comments_written: int


@dataclass(frozen=True)
class _StreamResult:
    written: int
    newest: int | None
    oldest: int | None
    exhausted: bool  # True = the source ended on its own; False = cut short


def sync_user(username: str, engine: Engine, *, full: bool = False) -> SyncResult:
    """Archive a user's posts and comments into ``engine``.

    By default this is *incremental*: a per-user, per-kind ``SyncState`` cursor
    lets a re-sync fetch only items newer than what's stored (and resume an
    interrupted backfill from the tail) instead of re-walking the whole history.
    Pass ``full=True`` to ignore the cursors and re-pull everything.
    """
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
        posts = _sync_kind(
            session, user.username, "posts", arctic.iter_posts, Post.from_arctic, full
        )
        comments = _sync_kind(
            session, user.username, "comments", arctic.iter_comments,
            Comment.from_arctic, full,
        )
        session.commit()
    return SyncResult(user, posts, comments)


def _sync_kind(
    session: Session,
    username: str,
    kind: str,
    iter_fn: _KindIter,
    parse: Callable[[dict[str, Any]], T],
    full: bool,
) -> int:
    """Sync one kind (posts or comments) for a user and update its cursor.

    Two phases, driven by the saved cursor:
      - *head*: fetch items newer than ``newest_seen_utc``. On a first/full
        sync there is no cursor, so this walks the entire history top-to-bottom
        and doubles as the backfill — whether it reached the bottom is what
        sets ``completed_backfill``.
      - *backfill*: if a previous backfill was cut short, resume downward from
        ``oldest_seen_utc`` so already-stored rows aren't re-fetched.
    Returns the number of net-new rows written.
    """
    state = session.get(SyncState, (username, kind, "arctic"))
    prev_newest = None if (full or state is None) else state.newest_seen_utc
    prev_oldest = None if (full or state is None) else state.oldest_seen_utc
    completed = False if (full or state is None) else state.completed_backfill

    written = 0

    # Head: everything newer than the cursor (whole history when it's None).
    head = _stream(session, iter_fn(username, after=prev_newest), parse)
    written += head.written
    new_newest = _max(prev_newest, head.newest)

    if prev_newest is None:
        # First/full pull: the head walk IS the backfill. Its oldest item is
        # the tail, and whether it ran to the end decides completion.
        new_oldest = head.oldest
        completed = head.exhausted
    else:
        new_oldest = prev_oldest
        if not completed and prev_oldest is not None:
            back = _stream(session, iter_fn(username, before=prev_oldest), parse)
            written += back.written
            new_oldest = _min(prev_oldest, back.oldest)
            completed = back.exhausted

    row = state or SyncState(username=username, kind=kind)
    row.newest_seen_utc = new_newest
    row.oldest_seen_utc = new_oldest
    row.completed_backfill = completed
    row.synced_at = int(time.time())
    upsert(session, [row])
    return written


def _stream(
    session: Session,
    source: Iterator[dict[str, Any]],
    parse: Callable[[dict[str, Any]], T],
) -> _StreamResult:
    batch: list[T] = []
    written = 0
    newest: int | None = None
    oldest: int | None = None
    count = 0
    for raw in source:
        obj = parse(raw)
        ts = getattr(obj, "created_utc", None)
        if ts is not None:
            newest = ts if newest is None else max(newest, ts)
            oldest = ts if oldest is None else min(oldest, ts)
        batch.append(obj)
        count += 1
        if len(batch) >= BATCH_SIZE:
            written += upsert(session, batch)
            batch.clear()
    if batch:
        written += upsert(session, batch)
    # A stream is "exhausted" (reached the end of the user's history) unless it
    # was truncated by the MAX_ITEMS_PER_STREAM safety cap — the same cap arctic
    # enforces while paginating, so hitting it means more items remain upstream.
    cap = arctic.MAX_ITEMS_PER_STREAM
    exhausted = cap is None or count < cap
    return _StreamResult(written, newest, oldest, exhausted)


def _max(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    return a if b is None else max(a, b)


def _min(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    return a if b is None else min(a, b)

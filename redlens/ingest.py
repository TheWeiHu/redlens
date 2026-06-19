from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, TypeVar

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, select

from redlens import arctic
from redlens.db import upsert
from redlens.errors import NotFound
from redlens.models import Comment, Post, SyncState, User

T = TypeVar("T", bound=SQLModel)
BATCH_SIZE = 500
PROVIDER = "arctic"


@dataclass(frozen=True)
class SyncResult:
    user: User
    posts_written: int
    comments_written: int


def sync_user(username: str, engine: Engine, *, full: bool = False) -> SyncResult:
    """Archive a user's posts and comments into ``engine``.

    Incremental by default: each stream resumes from the cursors in
    ``sync_state`` so an unchanged user costs one request per kind and writes
    nothing. ``full=True`` ignores the cursors and re-pulls the whole history.
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
        posts = _sync_kind(session, user.username, "posts",
                           arctic.iter_posts, Post.from_arctic, full)
        comments = _sync_kind(session, user.username, "comments",
                              arctic.iter_comments, Comment.from_arctic, full)
        session.commit()
    return SyncResult(user, posts, comments)


def _get_sync_state(session: Session, username: str, kind: str) -> SyncState | None:
    return session.exec(
        select(SyncState).where(
            SyncState.username == username,
            SyncState.kind == kind,
            SyncState.provider == PROVIDER,
        )
    ).first()


def _sync_kind(
    session: Session,
    username: str,
    kind: str,
    iter_fn: Callable[..., Iterator[dict[str, Any]]],
    parse: Callable[[dict[str, Any]], T],
    full: bool,
) -> int:
    """Stream one kind (posts|comments), advancing this user's cursor.

    Three modes, chosen from the stored state:
    - **full pull** — no state yet, or ``full=True``: walk the whole history.
    - **resume backfill** — a prior walk was cut off (``completed_backfill`` is
      False): continue further back from ``oldest_seen_utc``.
    - **incremental** — backfill done: fetch only items after ``newest_seen_utc``.
    """
    state = None if full else _get_sync_state(session, username, kind)

    after: int | None = None
    before: int | None = None
    if state is not None and not state.completed_backfill:
        before = state.oldest_seen_utc      # resume the interrupted backfill
    elif state is not None:
        after = state.newest_seen_utc       # cheap forward-only top-up

    incremental = after is not None
    newest = state.newest_seen_utc if state else None
    oldest = state.oldest_seen_utc if state else None

    batch: list[T] = []
    written = 0
    count = 0
    run_oldest: int | None = None   # oldest item fetched in THIS call only
    for raw in iter_fn(username, after=after, before=before):
        count += 1
        obj = parse(raw)
        cu: int = obj.created_utc  # type: ignore[attr-defined]
        newest = cu if newest is None else max(newest, cu)
        oldest = cu if oldest is None else min(oldest, cu)
        run_oldest = cu if run_oldest is None else min(run_oldest, cu)
        batch.append(obj)
        if len(batch) >= BATCH_SIZE:
            written += upsert(session, batch)
            batch.clear()
    if batch:
        written += upsert(session, batch)

    # A stream stops short of history only when the MAX_ITEMS_PER_STREAM cap
    # trips (the interruption hook used in tests — in production it is None, so
    # streams run to exhaustion). Streams are newest-first: a capped *backfill*
    # leaves the bottom unfinished, and a capped *incremental* top-up leaves a
    # gap between the old cursor and the oldest item it managed to fetch. Both
    # are unfinished backfills — drop oldest_seen_utc to this run's floor so the
    # next sync resumes downward from there and closes the gap (re-pulled rows
    # dedup on upsert). Only a stream that exhausted is "completed".
    cap = arctic.MAX_ITEMS_PER_STREAM
    capped = cap is not None and count >= cap
    if incremental and capped and run_oldest is not None:
        completed = False
        oldest = run_oldest
    else:
        completed = (state is not None and state.completed_backfill) or not capped

    upsert(session, [SyncState(
        username=username,
        kind=kind,
        provider=PROVIDER,
        newest_seen_utc=newest,
        oldest_seen_utc=oldest,
        completed_backfill=completed,
        synced_at=int(time.time()),
    )])
    return written

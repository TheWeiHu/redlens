from __future__ import annotations

import os
from pathlib import Path
from typing import TypeVar

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

# Importing models registers them with SQLModel.metadata.
from redditpages.models import (  # noqa: F401
    Comment,
    Moderator,
    Post,
    Subreddit,
    User,
    UserStat,
)

T = TypeVar("T", bound=SQLModel)

# The synced SQLite databases are large, network-sourced artifacts kept out of
# the repo. They live in a sibling ``data/`` directory next to the checkout
# (override with the ``REDDITPAGES_DATA`` env var).
DATA_DIR = Path(
    os.environ.get("REDDITPAGES_DATA")
    or Path(__file__).resolve().parents[2] / "data"
)


def data_db(name: str = "important.db") -> str:
    """Resolve a database file name against the shared data directory."""
    return str(DATA_DIR / name)


def connect(path: str | Path = "redditpages.db") -> Engine:
    return create_engine(f"sqlite:///{path}", echo=False)


def init_schema(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)


def session(engine: Engine) -> Session:
    return Session(engine)


def upsert(session: Session, items: list[T]) -> int:
    if not items:
        return 0
    table = type(items[0]).__table__  # type: ignore[attr-defined]
    pk_cols = {c.name for c in table.primary_key.columns}
    rows = [item.model_dump() for item in items]
    stmt = sqlite_insert(table).values(rows)
    update_cols = {
        c.name: stmt.excluded[c.name]
        for c in table.columns
        if c.name not in pk_cols
    }
    session.execute(stmt.on_conflict_do_update(
        index_elements=list(pk_cols),
        set_=update_cols,
    ))
    return len(items)


def insert_ignore(session: Session, items: list[T]) -> int:
    """Insert rows, skipping any whose primary key already exists.

    Unlike ``upsert``, this never rewrites an existing row — use it for stable
    dimension tables (e.g. ``subreddit``) that should not churn on re-sync.
    """
    if not items:
        return 0
    table = type(items[0]).__table__  # type: ignore[attr-defined]
    stmt = sqlite_insert(table).values([item.model_dump() for item in items])
    session.execute(stmt.on_conflict_do_nothing())
    return len(items)

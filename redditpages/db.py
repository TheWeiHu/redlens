from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

# Importing models registers them with SQLModel.metadata.
from redditpages.models import (  # noqa: F401
    Comment,
    Post,
    SubredditModerator,
    User,
)

T = TypeVar("T", bound=SQLModel)


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

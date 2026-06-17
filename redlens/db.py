from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import select, tuple_
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

# Import every table model so `init_schema` is self-sufficient: create_all
# only builds tables registered in SQLModel.metadata, so they must all be
# imported here regardless of what the caller happens to import.
from redlens.models import (  # noqa: F401
    Comment,
    Post,
    Topic,
    TopicPost,
    User,
)

T = TypeVar("T", bound=SQLModel)

# Schema versioning rides on SQLite's PRAGMA user_version. Each MIGRATIONS
# entry upgrades version N-1 -> N. Migrations run BEFORE
# ``SQLModel.metadata.create_all``, so an entry can ALTER an existing table,
# or DROP one whose shape changed fundamentally and let create_all rebuild it
# at the current ORM schema (brand-new tables also arrive via create_all).
# Fresh databases skip migrations and are built straight at the latest schema;
# databases from before versioning (user_version 0 with tables present) are
# treated as version 1, the v0.2 baseline.
SCHEMA_VERSION = 3
MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: ("ALTER TABLE topic ADD COLUMN exclude_terms VARCHAR NOT NULL DEFAULT ''",),
    # v3 gave topic a surrogate id + keyword list and rekeyed topicpost on
    # topic_id; both change shape, so drop and let create_all rebuild. Tracked
    # topics are re-created by the next `track` (posts/comments are preserved).
    3: ("DROP TABLE IF EXISTS topicpost", "DROP TABLE IF EXISTS topic"),
}


def connect(path: str | Path = "redlens.db") -> Engine:
    p = str(path)
    if p != ":memory:":
        Path(p).expanduser().parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{p}", echo=False)


def init_schema(engine: Engine) -> None:
    with engine.begin() as con:
        version = int(con.exec_driver_sql("PRAGMA user_version").scalar() or 0)
        if version == 0:
            existed = con.exec_driver_sql(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='user'"
            ).first() is not None
            # Tables but no version stamp = a DB from before versioning
            # existed; that schema is the v1 baseline. No tables = fresh:
            # create_all below builds the latest schema outright.
            version = 1 if existed else SCHEMA_VERSION
    # Migrations first: an entry may drop a reshaped table that create_all
    # then rebuilds at the current ORM schema.
    with engine.begin() as con:
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for stmt in MIGRATIONS[target]:
                con.exec_driver_sql(stmt)
    SQLModel.metadata.create_all(engine)
    with engine.begin() as con:
        con.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")


def session(engine: Engine) -> Session:
    return Session(engine)


def upsert(session: Session, items: list[T]) -> int:
    """Insert ``items``, refreshing any whose primary key already exists.

    Returns the number of rows **newly inserted** — rows that already existed
    are updated but not counted, so re-syncing unchanged data returns 0. This
    is what lets callers report net-new (how many rows a sync actually added)
    and is the foundation for incremental sync.
    """
    if not items:
        return 0
    table = type(items[0]).__table__  # type: ignore[attr-defined]
    pk_names = [c.name for c in table.primary_key.columns]
    rows = [item.model_dump() for item in items]
    new_count = _count_new(session, table, pk_names, rows)
    stmt = sqlite_insert(table).values(rows)
    update_cols = {
        c.name: stmt.excluded[c.name]
        for c in table.columns
        if c.name not in set(pk_names)
    }
    if update_cols:
        session.execute(stmt.on_conflict_do_update(
            index_elements=pk_names,
            set_=update_cols,
        ))
    else:
        # Every column is part of the key (pure join tables like topicpost):
        # nothing to rewrite on conflict.
        session.execute(stmt.on_conflict_do_nothing())
    return new_count


def _count_new(session: Session, table: Any, pk_names: list[str],
               rows: list[dict[str, Any]]) -> int:
    """How many of ``rows`` have a primary key not already in ``table``."""
    pk_cols = [table.c[name] for name in pk_names]
    keys = [tuple(r[name] for name in pk_names) for r in rows]
    if len(pk_cols) == 1:
        col = pk_cols[0]
        found = session.execute(select(col).where(col.in_([k[0] for k in keys])))
    else:
        found = session.execute(select(*pk_cols).where(tuple_(*pk_cols).in_(keys)))
    present = {tuple(row) for row in found}
    return sum(1 for k in keys if k not in present)

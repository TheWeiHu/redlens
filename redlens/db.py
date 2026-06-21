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
    SyncState,
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
SCHEMA_VERSION = 6
# Each migration step is a tuple of operations. An operation is either a raw SQL
# string (run as-is) or an ("addcol", table, column, decl) tuple — an additive
# column-add that is SKIPPED when the table is absent (a prior step dropped it
# and create_all will rebuild it at the current schema, the new column included)
# or the column already exists (idempotent). This guard matters because step 3
# DROPs topic/topicpost: without it, the later column-adds would ALTER missing
# tables and abort init_schema on any pre-v3 database (see _run_migration).
AddCol = tuple[str, str, str, str]
MIGRATIONS: dict[int, tuple[str | AddCol, ...]] = {
    2: (("addcol", "topic", "exclude_terms", "VARCHAR NOT NULL DEFAULT ''"),),
    # v3 gave topic a surrogate id + keyword list and rekeyed topicpost on
    # topic_id; both change shape, so drop and let create_all rebuild. Tracked
    # topics are re-created by the next `track` (posts/comments are preserved).
    3: ("DROP TABLE IF EXISTS topicpost", "DROP TABLE IF EXISTS topic"),
    # v4 adds the sync_state table (incremental-sync cursors). It is purely
    # additive, so create_all builds it on the spot — no DDL needed here; this
    # empty step only advances user_version past 3 so the stamp stays honest.
    4: (),
    # v5 adds the LLM relevance verdict to topicpost (additive, nullable):
    # which matched posts a tracked topic's filter judged on-topic. All-null on
    # existing rows means "unscored" — kept by default, so old archives read
    # exactly as before until the next keyed `track` fills them in.
    5: (
        ("addcol", "topicpost", "relevant", "BOOLEAN"),
        ("addcol", "topicpost", "relevance_confidence", "FLOAT"),
        ("addcol", "topicpost", "relevance_reason", "VARCHAR"),
        ("addcol", "topicpost", "relevance_model", "VARCHAR"),
        ("addcol", "topicpost", "relevance_at", "INTEGER"),
    ),
    # v6 adds topic.about (additive): a one-line definition of the intended sense
    # for the relevance filter (`track --about`), so the LLM pins the right
    # meaning of an ambiguous name. Empty default = infer, unchanged behavior.
    6: (("addcol", "topic", "about", "VARCHAR NOT NULL DEFAULT ''"),),
}


def _add_column(con: Any, table: str, column: str, decl: str) -> None:
    """``ALTER TABLE … ADD COLUMN``, but a no-op when the table is absent (a
    prior migration dropped it; create_all rebuilds it at the current schema with
    this column already present) or the column already exists (idempotent)."""
    has_table = con.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).first() is not None
    if not has_table:
        return
    cols = {r[1] for r in con.exec_driver_sql(f"PRAGMA table_info({table})")}
    if column in cols:
        return
    con.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _run_migration(con: Any, op: str | AddCol) -> None:
    if isinstance(op, tuple):
        _, table, column, decl = op
        _add_column(con, table, column, decl)
    else:
        con.exec_driver_sql(op)


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
            for op in MIGRATIONS[target]:
                _run_migration(con, op)
    SQLModel.metadata.create_all(engine)
    with engine.begin() as con:
        con.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")


def session(engine: Engine) -> Session:
    return Session(engine)


def upsert(session: Session, items: list[T], *, update: bool = True) -> int:
    """Insert ``items``, refreshing any whose primary key already exists.

    Returns the number of rows **newly inserted** — rows that already existed
    are updated but not counted, so re-syncing unchanged data returns 0. This
    is what lets callers report net-new (how many rows a sync actually added)
    and is the foundation for incremental sync.

    ``update=False`` makes an existing row untouched on conflict (insert-or-
    ignore). Use it when the non-PK columns carry state the caller must NOT
    clobber from the incoming object's defaults — e.g. ``topicpost`` rows hold a
    relevance verdict, so re-linking a post on a full re-pull must keep its
    verdict rather than reset it to NULL.
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
    } if update else {}
    if update_cols:
        session.execute(stmt.on_conflict_do_update(
            index_elements=pk_names,
            set_=update_cols,
        ))
    else:
        # Nothing to rewrite on conflict: a pure join table (every column is a
        # key), or update=False (keep the existing row's non-PK state).
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

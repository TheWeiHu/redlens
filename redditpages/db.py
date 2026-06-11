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
    User,
)

T = TypeVar("T", bound=SQLModel)

# Schema versioning rides on SQLite's PRAGMA user_version. Each MIGRATIONS
# entry upgrades version N-1 -> N and contains only ALTER/UPDATE statements
# for *existing* tables — brand-new tables arrive for free via
# ``SQLModel.metadata.create_all`` before migrations run. Fresh databases are
# created at the latest schema and stamped directly; databases from before
# versioning existed (user_version 0 with tables present) are treated as
# version 1, the v0.2 baseline.
SCHEMA_VERSION = 1
MIGRATIONS: dict[int, tuple[str, ...]] = {
    # 2: ("ALTER TABLE user ADD COLUMN ...",),
}


def connect(path: str | Path = "redditpages.db") -> Engine:
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
    SQLModel.metadata.create_all(engine)
    with engine.begin() as con:
        for target in range(version + 1, SCHEMA_VERSION + 1):
            for stmt in MIGRATIONS[target]:
                con.exec_driver_sql(stmt)
        con.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")


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

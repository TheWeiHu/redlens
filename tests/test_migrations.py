"""Mechanics of the PRAGMA user_version migration scheme in db.init_schema."""

from redlens import db as rdb
from redlens.db import connect, init_schema


def _user_version(engine) -> int:
    with engine.begin() as con:
        return int(con.exec_driver_sql("PRAGMA user_version").scalar() or 0)


def test_fresh_db_is_stamped_at_latest(tmp_path):
    engine = connect(tmp_path / "fresh.db")
    init_schema(engine)
    assert _user_version(engine) == rdb.SCHEMA_VERSION


def test_fresh_db_skips_migrations(tmp_path, monkeypatch):
    # This statement would blow up if executed — a fresh DB must never run
    # migrations because create_all already built the latest schema.
    monkeypatch.setattr(rdb, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(rdb, "MIGRATIONS", {2: ("INSERT INTO no_such_table VALUES (1)",)})
    engine = connect(tmp_path / "fresh.db")
    init_schema(engine)
    assert _user_version(engine) == 2


def test_outdated_db_runs_pending_migrations(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    engine = connect(path)
    init_schema(engine)
    engine.dispose()

    monkeypatch.setattr(rdb, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(
        rdb, "MIGRATIONS",
        {2: ('ALTER TABLE user ADD COLUMN migrated_marker INTEGER',)},
    )
    engine = connect(path)
    init_schema(engine)
    with engine.begin() as con:
        cols = [r[1] for r in con.exec_driver_sql('PRAGMA table_info("user")')]
    assert "migrated_marker" in cols
    assert _user_version(engine) == 2


def test_pre_versioning_db_treated_as_baseline(tmp_path, monkeypatch):
    # A DB with tables but user_version 0 predates versioning: it is the v1
    # baseline and must receive every migration after that.
    path = tmp_path / "legacy.db"
    engine = connect(path)
    init_schema(engine)
    with engine.begin() as con:
        con.exec_driver_sql("PRAGMA user_version = 0")
    engine.dispose()

    monkeypatch.setattr(rdb, "SCHEMA_VERSION", 2)
    monkeypatch.setattr(
        rdb, "MIGRATIONS",
        {2: ('ALTER TABLE user ADD COLUMN migrated_marker INTEGER',)},
    )
    engine = connect(path)
    init_schema(engine)
    with engine.begin() as con:
        cols = [r[1] for r in con.exec_driver_sql('PRAGMA table_info("user")')]
    assert "migrated_marker" in cols
    assert _user_version(engine) == 2


def test_init_schema_is_idempotent(tmp_path):
    engine = connect(tmp_path / "twice.db")
    init_schema(engine)
    init_schema(engine)
    assert _user_version(engine) == rdb.SCHEMA_VERSION

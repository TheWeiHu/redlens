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
    nxt = rdb.SCHEMA_VERSION + 1
    monkeypatch.setattr(rdb, "SCHEMA_VERSION", nxt)
    monkeypatch.setattr(rdb, "MIGRATIONS",
                        {nxt: ("INSERT INTO no_such_table VALUES (1)",)})
    engine = connect(tmp_path / "fresh.db")
    init_schema(engine)
    assert _user_version(engine) == nxt


def test_outdated_db_runs_pending_migrations(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    engine = connect(path)
    init_schema(engine)                                # stamped at current
    engine.dispose()

    nxt = rdb.SCHEMA_VERSION + 1
    monkeypatch.setattr(rdb, "SCHEMA_VERSION", nxt)
    monkeypatch.setattr(
        rdb, "MIGRATIONS",
        {nxt: ('ALTER TABLE user ADD COLUMN migrated_marker INTEGER',)},
    )
    engine = connect(path)
    init_schema(engine)
    with engine.begin() as con:
        cols = [r[1] for r in con.exec_driver_sql('PRAGMA table_info("user")')]
    assert "migrated_marker" in cols
    assert _user_version(engine) == nxt


def test_pre_versioning_db_treated_as_baseline(tmp_path, monkeypatch):
    # A DB with tables but user_version 0 predates versioning: it is the v1
    # baseline and must receive every migration after that.
    path = tmp_path / "legacy.db"
    engine = connect(path)
    init_schema(engine)
    with engine.begin() as con:
        con.exec_driver_sql("PRAGMA user_version = 0")
    engine.dispose()

    # benign no-ops for intermediate versions (the test DB was built with
    # the current schema, so the real intermediate ALTERs would collide)
    nxt = rdb.SCHEMA_VERSION + 1
    fakes: dict[int, tuple[str, ...]] = {v: () for v in range(2, nxt)}
    fakes[nxt] = ('ALTER TABLE user ADD COLUMN migrated_marker INTEGER',)
    monkeypatch.setattr(rdb, "SCHEMA_VERSION", nxt)
    monkeypatch.setattr(rdb, "MIGRATIONS", fakes)
    engine = connect(path)
    init_schema(engine)
    with engine.begin() as con:
        cols = [r[1] for r in con.exec_driver_sql('PRAGMA table_info("user")')]
    assert "migrated_marker" in cols
    assert _user_version(engine) == nxt


def test_v3_rebuilds_topic_with_id_and_keywords(tmp_path):
    # A genuine v1-era database: name-keyed topic with a `query` column and a
    # topic_name-keyed topicpost. v3 rebuilds both; posts are untouched.
    engine = connect(tmp_path / "v1.db")
    with engine.begin() as con:
        con.exec_driver_sql(
            "CREATE TABLE topic (name VARCHAR PRIMARY KEY, query VARCHAR, "
            "subreddits VARCHAR, days INTEGER, newest_seen_utc INTEGER, "
            "last_tracked_at INTEGER, fetched_at INTEGER)")
        con.exec_driver_sql(
            "CREATE TABLE topicpost (topic_name VARCHAR, post_id VARCHAR, "
            "PRIMARY KEY (topic_name, post_id))")
        con.exec_driver_sql(
            "CREATE TABLE post (post_id VARCHAR PRIMARY KEY, author_username "
            "VARCHAR, subreddit_name VARCHAR, created_utc INTEGER, score "
            "INTEGER, num_comments INTEGER, over_18 BOOLEAN, fetched_at INTEGER)")
        con.exec_driver_sql("INSERT INTO post (post_id) VALUES ('keepme')")
        con.exec_driver_sql("PRAGMA user_version = 1")
    init_schema(engine)
    with engine.begin() as con:
        topic_cols = [r[1] for r in con.exec_driver_sql('PRAGMA table_info("topic")')]
        tp_cols = [r[1] for r in con.exec_driver_sql('PRAGMA table_info("topicpost")')]
        posts = con.exec_driver_sql("SELECT post_id FROM post").scalars().all()
    assert "id" in topic_cols and "keywords" in topic_cols
    assert "query" not in topic_cols
    assert "topic_id" in tp_cols and "topic_name" not in tp_cols
    assert list(posts) == ["keepme"]               # post data preserved
    assert _user_version(engine) == rdb.SCHEMA_VERSION


def test_init_schema_is_idempotent(tmp_path):
    engine = connect(tmp_path / "twice.db")
    init_schema(engine)
    init_schema(engine)
    assert _user_version(engine) == rdb.SCHEMA_VERSION

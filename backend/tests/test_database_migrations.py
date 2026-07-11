import sqlite3

import pytest
from sqlalchemy import create_engine

from apps.database.migrations import Migration, run_migrations


def _engine(path):
    return create_engine(f"sqlite:///{path}", future=True)


def test_versioned_migration_upgrades_legacy_runs_and_creates_backup(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    engine = _engine(db_path)
    assert run_migrations(engine, db_path) == [1]
    assert run_migrations(engine, db_path) == []

    with engine.connect() as connection:
        columns = {
            row[1]
            for row in connection.exec_driver_sql('PRAGMA table_info("runs")')
        }
        versions = connection.exec_driver_sql(
            "SELECT version FROM schema_migrations"
        ).scalars().all()

    assert {
        "job_category",
        "job_outcome",
        "job_subject_ref",
        "job_correlation_id",
    }.issubset(columns)
    assert versions == [1]
    assert len(list(tmp_path.glob("legacy.db.pre-migration-*.bak"))) == 1


def test_failed_migration_rolls_back_and_keeps_recovery_backup(tmp_path):
    db_path = tmp_path / "rollback.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("CREATE TABLE sentinel (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()

    def fail(connection):
        connection.exec_driver_sql("CREATE TABLE should_rollback (id INTEGER)")
        raise RuntimeError("migration failed")

    engine = _engine(db_path)
    with pytest.raises(RuntimeError, match="migration failed"):
        run_migrations(
            engine,
            db_path,
            migrations=(Migration(99, "failing", fail),),
        )

    with engine.connect() as connection:
        tables = {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        versions = connection.exec_driver_sql(
            "SELECT version FROM schema_migrations"
        ).scalars().all()

    assert "should_rollback" not in tables
    assert versions == []
    assert len(list(tmp_path.glob("rollback.db.pre-migration-*.bak"))) == 1

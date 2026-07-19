"""Small versioned SQLite migration runner for persisted StreamFlow databases."""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from sqlalchemy.engine import Connection, Engine


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    upgrade: Callable[[Connection], None]


def _table_columns(connection: Connection, table_name: str) -> set[str]:
    return {
        row[1]
        for row in connection.exec_driver_sql(
            f'PRAGMA table_info("{table_name}")'
        ).fetchall()
    }


def _add_v6_run_history_columns(connection: Connection) -> None:
    tables = {
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "runs" not in tables:
        return

    columns = _table_columns(connection, "runs")
    required = {
        "job_category": "VARCHAR(50)",
        "job_outcome": "VARCHAR(50)",
        "job_subject_ref": "VARCHAR(100)",
        "job_correlation_id": "VARCHAR(100)",
    }
    for column_name, column_type in required.items():
        if column_name not in columns:
            connection.exec_driver_sql(
                f'ALTER TABLE "runs" ADD COLUMN "{column_name}" {column_type}'
            )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "add_v6_run_history_columns", _add_v6_run_history_columns),
)


def _backup_database(db_path: Path, *, retention: int = 3) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = db_path.with_name(f"{db_path.name}.pre-migration-{timestamp}.bak")
    descriptor, temp_name = tempfile.mkstemp(
        dir=str(db_path.parent),
        prefix=f".{db_path.name}.pre-migration-",
        suffix=".tmp",
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        source = sqlite3.connect(str(db_path))
        target = sqlite3.connect(str(temp_path))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, backup_path)
    finally:
        temp_path.unlink(missing_ok=True)

    backups = sorted(
        db_path.parent.glob(f"{db_path.name}.pre-migration-*.bak"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for stale in backups[max(1, retention):]:
        stale.unlink(missing_ok=True)
    return backup_path


def _restore_database_backup(engine: Engine, db_path: Path, backup_path: Path) -> None:
    engine.dispose()
    descriptor, temp_name = tempfile.mkstemp(
        dir=str(db_path.parent),
        prefix=f".{db_path.name}.restore-",
        suffix=".tmp",
    )
    os.close(descriptor)
    temp_path = Path(temp_name)
    try:
        shutil.copyfile(backup_path, temp_path)
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, db_path)
        db_path.with_name(f"{db_path.name}-wal").unlink(missing_ok=True)
        db_path.with_name(f"{db_path.name}-shm").unlink(missing_ok=True)
    finally:
        temp_path.unlink(missing_ok=True)


def run_migrations(
    engine: Engine,
    db_path: Path,
    *,
    migrations: Sequence[Migration] = MIGRATIONS,
) -> list[int]:
    """Apply pending migrations transactionally after a consistent DB backup."""
    if engine.dialect.name != "sqlite":
        return []

    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                applied_at DATETIME NOT NULL
            )
            """
        )
        applied = {
            int(row[0])
            for row in connection.exec_driver_sql(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }

    pending = sorted(
        (migration for migration in migrations if migration.version not in applied),
        key=lambda migration: migration.version,
    )
    if not pending:
        return []

    engine_database = engine.url.database
    backup_path = None
    actual_db_path = None
    if engine_database and engine_database != ":memory:":
        actual_db_path = Path(engine_database).resolve()
        backup_path = _backup_database(actual_db_path)

    completed = []
    try:
        for migration in pending:
            with engine.begin() as connection:
                migration.upgrade(connection)
                connection.exec_driver_sql(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                    (
                        migration.version,
                        migration.name,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            completed.append(migration.version)
        return completed
    except Exception:
        if backup_path is not None and actual_db_path is not None:
            _restore_database_backup(engine, actual_db_path, backup_path)
        raise

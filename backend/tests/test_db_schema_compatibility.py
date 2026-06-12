from apps.automation import automation_config_manager as acm
from apps.database.connection import get_session, init_db


def test_init_db_accepts_legacy_automation_periods_schema(clean_test_db):
    """init_db should still accept legacy automation_periods tables."""
    with clean_test_db.begin() as conn:
        conn.exec_driver_sql("DROP TABLE automation_periods")
        conn.exec_driver_sql(
            """
            CREATE TABLE automation_periods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                profile_id INTEGER NOT NULL,
                cron_schedule VARCHAR(100) NOT NULL,
                name VARCHAR(255) NOT NULL,
                enabled BOOLEAN,
                channel_regex VARCHAR(512),
                exclude_regex VARCHAR(512),
                matching_type VARCHAR(50),
                automation_type VARCHAR(50),
                extra_settings JSON,
                FOREIGN KEY(profile_id) REFERENCES automation_profiles (id) ON DELETE CASCADE
            )
            """
        )

    init_db()

    # Reset singleton for isolation and verify period creation works against the legacy schema.
    acm._automation_config_manager = None
    manager = acm.get_automation_config_manager()

    profile_id = manager.create_profile({"name": "Schema Compat Profile"})
    assert profile_id is not None

    period_id = manager.create_period(
        {
            "name": "Schema Compat Period",
            "profile_id": profile_id,
            "schedule": {"type": "interval", "value": "60"},
        }
    )
    assert period_id is not None


def test_init_db_adds_v6_run_history_columns_to_legacy_runs_schema(clean_test_db):
    """Existing SQLite DBs should gain V6 job-history columns on startup."""
    with clean_test_db.begin() as conn:
        conn.exec_driver_sql("DROP TABLE stream_telemetry")
        conn.exec_driver_sql("DROP TABLE channel_health")
        conn.exec_driver_sql("DROP TABLE runs")
        conn.exec_driver_sql(
            """
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME,
                duration_seconds FLOAT NOT NULL DEFAULT 0.0,
                total_channels INTEGER NOT NULL DEFAULT 0,
                total_streams INTEGER NOT NULL DEFAULT 0,
                global_dead_count INTEGER NOT NULL DEFAULT 0,
                global_revived_count INTEGER NOT NULL DEFAULT 0,
                run_type VARCHAR(50) NOT NULL DEFAULT 'automation_run',
                raw_details VARCHAR,
                raw_subentries VARCHAR
            )
            """
        )

    init_db()

    with clean_test_db.connect() as conn:
        columns = {
            row[1]
            for row in conn.exec_driver_sql('PRAGMA table_info("runs")').fetchall()
        }

    assert "job_category" in columns
    assert "job_outcome" in columns
    assert "job_subject_ref" in columns
    assert "job_correlation_id" in columns

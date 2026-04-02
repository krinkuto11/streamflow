from apps.automation import automation_config_manager as acm
from apps.database.connection import get_session, init_db


def test_init_db_reconciles_legacy_automation_periods_schema(clean_test_db):
    """init_db should add missing columns to legacy automation_periods tables."""
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

    session = get_session()
    try:
        columns = {
            row[1]
            for row in session.connection().exec_driver_sql(
                'PRAGMA table_info("automation_periods")'
            ).fetchall()
        }
    finally:
        session.close()

    assert 'enable_loop_detection' in columns

    # Reset singleton for isolation and verify period creation works against upgraded schema.
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

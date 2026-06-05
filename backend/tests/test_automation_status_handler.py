from datetime import datetime, timedelta

from apps.api.automation_handlers import get_recent_run_history_summary
from apps.database.connection import get_session
from apps.database.models import Run


def test_recent_run_history_summary_uses_automation_runs_only():
    session = get_session()
    try:
        base = datetime(2026, 6, 4, 12, 0, 0)
        session.add_all(
            [
                Run(
                    timestamp=base - timedelta(minutes=30),
                    run_type="automation_run",
                    duration_seconds=120,
                    total_channels=12,
                    total_streams=100,
                    global_dead_count=2,
                    global_revived_count=1,
                ),
                Run(
                    timestamp=base - timedelta(minutes=25),
                    run_type="automation_run",
                    duration_seconds=150,
                    total_channels=15,
                    total_streams=120,
                    global_dead_count=1,
                ),
                Run(
                    timestamp=base - timedelta(minutes=20),
                    run_type="single_channel_check",
                    duration_seconds=5,
                    total_channels=1,
                ),
                Run(
                    timestamp=base - timedelta(minutes=10),
                    run_type="automation_run",
                    duration_seconds=300,
                    total_channels=30,
                    total_streams=200,
                    global_dead_count=3,
                ),
            ]
        )
        session.commit()
    finally:
        session.close()

    summary = get_recent_run_history_summary(limit=5)

    assert summary["sample_count"] == 3
    assert summary["typical_duration_seconds"] == 150
    assert summary["average_duration_seconds"] == 190
    assert summary["typical_seconds_per_channel"] == 10
    assert summary["per_channel_sample_count"] == 3
    assert summary["per_channel_baseline_stable"] is True
    assert summary["latest"]["duration_seconds"] == 300
    assert summary["latest"]["total_channels"] == 30
    assert [run["duration_seconds"] for run in summary["runs"]] == [300, 150, 120]


def test_recent_run_history_summary_returns_empty_shape_without_runs():
    summary = get_recent_run_history_summary(limit=5)

    assert summary == {
        "sample_count": 0,
        "runs": [],
        "latest": None,
        "typical_duration_seconds": None,
        "average_duration_seconds": None,
        "typical_seconds_per_channel": None,
        "per_channel_sample_count": 0,
        "per_channel_baseline_stable": False,
    }

from datetime import datetime, timedelta

from apps.automation.automation_config_manager import AutomationConfigManager
from apps.automation.automated_stream_manager import AutomatedStreamManager


def _period(catch_up=False):
    return {
        "id": "period-1",
        "name": "Hourly",
        "schedule": {"type": "interval", "value": 60},
        "catch_up_missed_runs": catch_up,
    }


def test_period_without_last_run_waits_by_default():
    manager = AutomatedStreamManager()
    manager.period_last_run = {}

    assert manager._is_period_due("period-1", _period(catch_up=False)) is False
    assert "period-1" in manager.period_last_run


def test_period_without_last_run_can_startup_catch_up():
    manager = AutomatedStreamManager()
    manager.period_last_run = {}

    assert manager._is_period_due("period-1", _period(catch_up=True)) is True
    assert "period-1" not in manager.period_last_run


def test_period_with_old_last_run_still_runs_normally():
    manager = AutomatedStreamManager()
    manager.period_last_run = {
        "period-1": datetime.now() - timedelta(minutes=61),
    }

    assert manager._is_period_due("period-1", _period(catch_up=False)) is True


def test_period_catch_up_policy_persists_with_priority():
    manager = AutomationConfigManager()
    profile_id = manager.create_profile({"name": "Catch-Up Profile"})
    period_id = manager.create_period({
        "name": "Catch-Up Period",
        "schedule": {"type": "interval", "value": 60},
        "profile_id": profile_id,
        "priority": 7,
        "catch_up_missed_runs": True,
    })

    period = manager.get_period(period_id)
    assert period["priority"] == 7
    assert period["catch_up_missed_runs"] is True

    assert manager.update_period(period_id, {"catch_up_missed_runs": False}) is True
    period = manager.get_period(period_id)
    assert period["priority"] == 7
    assert period["catch_up_missed_runs"] is False

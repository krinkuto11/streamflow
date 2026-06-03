from datetime import datetime, timedelta

from apps.automation.automation_config_manager import AutomationConfigManager
from apps.automation.automated_stream_manager import AutomatedStreamManager


def _period(catch_up=False, grace_minutes=0):
    return {
        "id": "period-1",
        "name": "Hourly",
        "schedule": {"type": "interval", "value": 60},
        "catch_up_missed_runs": catch_up,
        "missed_run_grace_minutes": grace_minutes,
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


def test_period_within_missed_run_grace_runs_once():
    manager = AutomatedStreamManager()
    manager.period_last_run = {
        "period-1": datetime.now() - timedelta(minutes=70),
    }

    assert manager._is_period_due("period-1", _period(grace_minutes=15)) is True


def test_period_outside_missed_run_grace_is_skipped_and_rebased():
    manager = AutomatedStreamManager()
    original_last_run = datetime.now() - timedelta(minutes=90)
    manager.period_last_run = {
        "period-1": original_last_run,
    }

    assert manager._is_period_due("period-1", _period(grace_minutes=15)) is False
    assert manager.period_last_run["period-1"] > original_last_run
    assert datetime.now() - manager.period_last_run["period-1"] < timedelta(seconds=5)
    history = manager.get_period_skip_history("period-1")
    assert len(history) == 1
    assert history[0]["reason"] == "missed_run_grace_expired"
    assert history[0]["period_name"] == "Hourly"
    assert history[0]["grace_minutes"] == 15
    assert "due_at" in history[0]
    assert "skipped_at" in history[0]


def test_period_catch_up_policy_persists_with_priority():
    manager = AutomationConfigManager()
    profile_id = manager.create_profile({"name": "Catch-Up Profile"})
    period_id = manager.create_period({
        "name": "Catch-Up Period",
        "schedule": {"type": "interval", "value": 60},
        "profile_id": profile_id,
        "priority": 7,
        "catch_up_missed_runs": True,
        "missed_run_grace_minutes": 15,
    })

    period = manager.get_period(period_id)
    assert period["priority"] == 7
    assert period["catch_up_missed_runs"] is True
    assert period["missed_run_grace_minutes"] == 15

    assert manager.update_period(period_id, {"catch_up_missed_runs": False}) is True
    period = manager.get_period(period_id)
    assert period["priority"] == 7
    assert period["catch_up_missed_runs"] is False
    assert period["missed_run_grace_minutes"] == 15

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

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


def test_maintenance_window_policy_handles_same_day_and_overnight_windows():
    manager = AutomatedStreamManager()

    same_day = {
        "maintenance_window_enabled": True,
        "maintenance_window_start": "02:00",
        "maintenance_window_end": "04:00",
    }
    assert manager._is_maintenance_window_active(same_day, datetime(2026, 6, 3, 2, 30)) is True
    assert manager._is_maintenance_window_active(same_day, datetime(2026, 6, 3, 4, 1)) is False

    overnight = {
        "maintenance_window_enabled": True,
        "maintenance_window_start": "23:00",
        "maintenance_window_end": "02:00",
    }
    assert manager._is_maintenance_window_active(overnight, datetime(2026, 6, 3, 23, 30)) is True
    assert manager._is_maintenance_window_active(overnight, datetime(2026, 6, 4, 1, 30)) is True
    assert manager._is_maintenance_window_active(overnight, datetime(2026, 6, 4, 2, 1)) is False


def test_teamarr_event_window_policy_uses_cached_upcoming_events():
    manager = AutomatedStreamManager()
    current = datetime(2026, 6, 3, 20, 50, tzinfo=timezone.utc)
    service = Mock()
    service.get_status.return_value = {
        "upcoming_events": [
            {
                "event_name": "Team A vs Team B",
                "channel_name": "Event Channel",
                "dispatcharr_channel_id": 123,
                "state": "scheduled",
                "event_date": "2026-06-03T21:00:00+00:00",
            }
        ]
    }

    with patch(
        "apps.stream.teamarr_preflight_service.get_teamarr_preflight_service",
        return_value=service,
    ):
        active = manager._get_active_teamarr_event_window(
            {
                "teamarr_event_window_enabled": True,
                "teamarr_event_window_before_minutes": 15,
                "teamarr_event_window_after_minutes": 5,
            },
            now=current,
        )

    assert active is not None
    assert active["event_name"] == "Team A vs Team B"
    assert active["seconds_to_start"] == 600
    assert active["window_before_minutes"] == 15
    assert active["window_after_minutes"] == 5


def test_teamarr_event_window_policy_ignores_filtered_and_outside_window_events():
    manager = AutomatedStreamManager()
    current = datetime(2026, 6, 3, 20, 50, tzinfo=timezone.utc)
    service = Mock()
    service.get_status.return_value = {
        "upcoming_events": [
            {
                "event_name": "Filtered Event",
                "dispatcharr_channel_id": 123,
                "state": "filtered",
                "event_date": "2026-06-03T20:55:00+00:00",
            },
            {
                "event_name": "Outside Event",
                "dispatcharr_channel_id": 456,
                "state": "scheduled",
                "event_date": "2026-06-03T22:00:00+00:00",
            },
        ]
    }

    with patch(
        "apps.stream.teamarr_preflight_service.get_teamarr_preflight_service",
        return_value=service,
    ):
        active = manager._get_active_teamarr_event_window(
            {
                "teamarr_event_window_enabled": True,
                "teamarr_event_window_before_minutes": 15,
                "teamarr_event_window_after_minutes": 5,
            },
            now=current,
        )

    assert active is None


def test_run_all_due_periods_disabled_defers_extra_periods_by_default():
    manager = AutomatedStreamManager()
    manager._period_skip_history = {}
    manager._save_state = lambda: None

    active_periods = {
        ("1", "Highest"): {"priority": 20, "period_name": "Highest", "channels": [1]},
        ("2", "Lowest"): {"priority": 1, "period_name": "Lowest", "channels": [2]},
        ("3", "Middle"): {"priority": 10, "period_name": "Middle", "channels": [3]},
    }

    kept = manager._apply_global_catch_up_cap(
        active_periods,
        {"run_all_due_periods": False, "catch_up_max_periods_per_cycle": 0},
        forced=False,
    )

    assert set(kept) == {("1", "Highest")}
    assert manager.get_period_skip_history("2")[0]["reason"] == "run_all_due_disabled"
    assert manager.get_period_skip_history("3")[0]["reason"] == "run_all_due_disabled"


def test_global_catch_up_cap_defers_lower_priority_periods_when_run_all_is_enabled():
    manager = AutomatedStreamManager()
    manager._period_skip_history = {}
    manager._save_state = lambda: None

    active_periods = {
        ("1", "Highest"): {"priority": 20, "period_name": "Highest", "channels": [1]},
        ("2", "Lowest"): {"priority": 1, "period_name": "Lowest", "channels": [2]},
        ("3", "Middle"): {"priority": 10, "period_name": "Middle", "channels": [3]},
    }

    kept = manager._apply_global_catch_up_cap(
        active_periods,
        {"run_all_due_periods": True, "catch_up_max_periods_per_cycle": 2},
        forced=False,
    )

    assert set(kept) == {("1", "Highest"), ("3", "Middle")}
    history = manager.get_period_skip_history("2")
    assert history[0]["reason"] == "global_catch_up_cap"
    assert history[0]["period_name"] == "Lowest"


def test_run_all_due_periods_enabled_without_cap_keeps_every_due_period():
    manager = AutomatedStreamManager()
    manager._period_skip_history = {}
    manager._save_state = lambda: None

    active_periods = {
        ("1", "First"): {"priority": 1, "period_name": "First", "channels": [1]},
        ("2", "Second"): {"priority": 1, "period_name": "Second", "channels": [2]},
    }

    kept = manager._apply_global_catch_up_cap(
        active_periods,
        {"run_all_due_periods": True, "catch_up_max_periods_per_cycle": 0},
        forced=False,
    )

    assert kept == active_periods
    assert manager.get_period_skip_history("2") == []


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


def test_global_catch_up_and_maintenance_policy_persist():
    manager = AutomationConfigManager()

    assert manager.update_global_settings(settings={
        "catch_up_max_periods_per_cycle": "3",
        "run_all_due_periods": "true",
        "maintenance_window_enabled": "true",
        "maintenance_window_start": "23:15",
        "maintenance_window_end": "02:45",
        "teamarr_event_window_enabled": "true",
        "teamarr_event_window_before_minutes": "25",
        "teamarr_event_window_after_minutes": "7",
    }) is True

    settings = manager.get_global_settings()
    assert settings["catch_up_max_periods_per_cycle"] == 3
    assert settings["run_all_due_periods"] is True
    assert settings["maintenance_window_enabled"] is True
    assert settings["maintenance_window_start"] == "23:15"
    assert settings["maintenance_window_end"] == "02:45"
    assert settings["teamarr_event_window_enabled"] is True
    assert settings["teamarr_event_window_before_minutes"] == 25
    assert settings["teamarr_event_window_after_minutes"] == 7

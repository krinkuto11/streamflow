from flask import Flask

from apps.api.automation_handlers import (
    handle_automation_periods_response,
    handle_global_automation_settings_response,
)


class DummyConfigManager:
    def __init__(self):
        self.settings = {
            "regular_automation_enabled": False,
            "playlist_update_interval_minutes": {"type": "interval", "value": 5},
            "validate_existing_streams": False,
        }
        self.update_calls = []

    def get_global_settings(self):
        return dict(self.settings)

    def update_global_settings(self, regular_automation_enabled=None, settings=None):
        updates = settings or {}
        if isinstance(regular_automation_enabled, dict):
            updates.update(regular_automation_enabled)
        self.update_calls.append(dict(updates))
        self.settings.update(updates)
        return True


class DummyAutomationManager:
    M3U_REFRESH_WAIT_DEFAULTS = {
        "enabled": True,
        "timeout_seconds": 600,
        "poll_interval_seconds": 10,
        "stable_polls_required": 2,
        "min_wait_seconds": 0,
        "retry_failed_providers": False,
    }

    def __init__(self):
        self.config = {"enabled_m3u_accounts": []}
        self.automation_running = False
        self.start_called = False
        self.stop_called = False

    def update_config(self, updates):
        self.config.update(updates)

    def start_automation(self):
        self.start_called = True
        self.automation_running = True

    def stop_automation(self):
        self.stop_called = True
        self.automation_running = False


class DummyPeriodsConfigManager:
    def get_all_periods(self, search="", page=None, per_page=50):
        return [
            {
                "id": "period-1",
                "name": "Hourly",
                "schedule": {"type": "interval", "value": 60},
            }
        ]

    def get_period_channels(self, period_id):
        return [101, 102]


class DummyPeriodRuntimeManager:
    def get_period_skip_history(self, period_id, *, limit=10):
        return [
            {
                "reason": "missed_run_grace_expired",
                "period_id": str(period_id),
                "period_name": "Hourly",
                "due_at": "2026-06-03T18:00:00",
                "skipped_at": "2026-06-03T18:20:00",
                "grace_minutes": 15,
                "message": "Missed-run grace expired before the scheduler observed this period",
            }
        ]


def test_get_automation_config_includes_enabled_m3u_accounts():
    app = Flask(__name__)
    cfg = DummyConfigManager()
    manager = DummyAutomationManager()
    manager.config["enabled_m3u_accounts"] = [1, 4]

    with app.app_context():
        response, status_code = handle_global_automation_settings_response(
            method="GET",
            updates=None,
            get_automation_config_manager=lambda: cfg,
            check_wizard_complete=lambda: True,
            get_automation_manager=lambda: manager,
        )

    assert status_code == 200
    data = response.get_json()
    assert data["enabled_m3u_accounts"] == [1, 4]
    assert data["m3u_refresh_wait"]["retry_failed_providers"] is False


def test_get_automation_periods_includes_runtime_skip_history():
    app = Flask(__name__)
    cfg = DummyPeriodsConfigManager()
    manager = DummyPeriodRuntimeManager()

    with app.app_context():
        response, status_code = handle_automation_periods_response(
            method="GET",
            args={},
            payload=None,
            get_automation_config_manager=lambda: cfg,
            croniter_available=True,
            croniter_module=None,
            get_automation_manager=lambda: manager,
        )

    assert status_code == 200
    data = response.get_json()
    assert data[0]["channel_count"] == 2
    assert data[0]["last_skip"]["reason"] == "missed_run_grace_expired"
    assert data[0]["skip_history"][0]["grace_minutes"] == 15


def test_put_automation_config_updates_enabled_m3u_accounts():
    app = Flask(__name__)
    cfg = DummyConfigManager()
    manager = DummyAutomationManager()

    with app.app_context():
        response, status_code = handle_global_automation_settings_response(
            method="PUT",
            updates={"enabled_m3u_accounts": ["2", 3]},
            get_automation_config_manager=lambda: cfg,
            check_wizard_complete=lambda: True,
            get_automation_manager=lambda: manager,
        )

    assert status_code == 200
    data = response.get_json()
    assert manager.config["enabled_m3u_accounts"] == [2, 3]
    assert data["settings"]["enabled_m3u_accounts"] == [2, 3]
    assert cfg.update_calls == []


def test_put_automation_config_updates_teamarr_event_window_policy():
    app = Flask(__name__)
    cfg = DummyConfigManager()
    manager = DummyAutomationManager()

    with app.app_context():
        response, status_code = handle_global_automation_settings_response(
            method="PUT",
            updates={
                "teamarr_event_window_enabled": True,
                "teamarr_event_window_before_minutes": 45,
                "teamarr_event_window_after_minutes": 6,
            },
            get_automation_config_manager=lambda: cfg,
            check_wizard_complete=lambda: True,
            get_automation_manager=lambda: manager,
        )

    assert status_code == 200
    data = response.get_json()
    assert data["settings"]["teamarr_event_window_enabled"] is True
    assert data["settings"]["teamarr_event_window_before_minutes"] == 45
    assert data["settings"]["teamarr_event_window_after_minutes"] == 6
    assert cfg.update_calls == [{
        "teamarr_event_window_enabled": True,
        "teamarr_event_window_before_minutes": 45,
        "teamarr_event_window_after_minutes": 6,
    }]


def test_put_automation_config_updates_m3u_refresh_wait_retry_policy():
    app = Flask(__name__)
    cfg = DummyConfigManager()
    manager = DummyAutomationManager()

    with app.app_context():
        response, status_code = handle_global_automation_settings_response(
            method="PUT",
            updates={
                "m3u_refresh_wait": {
                    "retry_failed_providers": True,
                    "timeout_seconds": 900,
                },
            },
            get_automation_config_manager=lambda: cfg,
            check_wizard_complete=lambda: True,
            get_automation_manager=lambda: manager,
        )

    assert status_code == 200
    data = response.get_json()
    assert manager.config["m3u_refresh_wait"]["retry_failed_providers"] is True
    assert manager.config["m3u_refresh_wait"]["timeout_seconds"] == 900
    assert data["settings"]["m3u_refresh_wait"]["retry_failed_providers"] is True
    assert cfg.update_calls == []


def test_put_automation_config_updates_run_all_due_policy():
    app = Flask(__name__)
    cfg = DummyConfigManager()
    manager = DummyAutomationManager()

    with app.app_context():
        response, status_code = handle_global_automation_settings_response(
            method="PUT",
            updates={
                "run_all_due_periods": True,
                "catch_up_max_periods_per_cycle": 4,
            },
            get_automation_config_manager=lambda: cfg,
            check_wizard_complete=lambda: True,
            get_automation_manager=lambda: manager,
        )

    assert status_code == 200
    data = response.get_json()
    assert data["settings"]["run_all_due_periods"] is True
    assert data["settings"]["catch_up_max_periods_per_cycle"] == 4
    assert cfg.update_calls == [{
        "run_all_due_periods": True,
        "catch_up_max_periods_per_cycle": 4,
    }]


def test_put_automation_config_rejects_invalid_enabled_m3u_accounts_payload():
    app = Flask(__name__)
    cfg = DummyConfigManager()
    manager = DummyAutomationManager()

    with app.app_context():
        response, status_code = handle_global_automation_settings_response(
            method="PUT",
            updates={"enabled_m3u_accounts": "1,2,3"},
            get_automation_config_manager=lambda: cfg,
            check_wizard_complete=lambda: True,
            get_automation_manager=lambda: manager,
        )

    assert status_code == 400
    assert response.get_json()["error"] == "enabled_m3u_accounts must be a list"

from flask import Flask

from apps.api.automation_handlers import handle_global_automation_settings_response


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

from flask import Flask

from apps.api.automation_handlers import (
    get_upcoming_automation_events_response,
    handle_automation_periods_response,
)
from apps.api.stream_sessions_handlers import (
    get_playing_streams_response,
    get_proxy_status_response,
)
from apps.api.viewer_activity_handlers import get_viewer_activity_status_response


class InitializingUdi:
    def is_initialization_pending(self):
        return True

    def get_proxy_status(self):
        raise AssertionError("startup status handlers must not block on proxy status")

    def get_playing_stream_ids(self):
        raise AssertionError("startup playing-streams handler must not block on UDI")

    def get_channels(self):
        raise AssertionError("startup viewer activity handler must not block on channels")


class ShadowMonitorStub:
    def get_config(self):
        return {"watcher_user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0"}

    def get_status(self):
        return {"running": True, "enabled": True}


class AutomationEventsSchedulerStub:
    def get_cached_events_snapshot(self, hours_ahead, max_events):
        return {
            "events": [
                {
                    "time": "2026-06-03T22:00:00",
                    "period_id": "1",
                    "period_name": "Full Check",
                }
            ],
            "cached_at": "2026-06-03T08:55:00",
            "from_cache": True,
            "cache_only": True,
            "stale": True,
        }

    def get_cached_events(self, hours_ahead, max_events, force_refresh=False):
        raise AssertionError("startup upcoming events response must not recalculate events")


class AutomationConfigManagerStub:
    def get_global_settings(self):
        return {"regular_automation_enabled": True}


def test_proxy_status_returns_empty_while_udi_initializes():
    app = Flask(__name__)

    with app.app_context():
        response, status_code = get_proxy_status_response(get_udi_manager=lambda: InitializingUdi())

    assert status_code == 200
    assert response.get_json() == {}


def test_playing_streams_returns_empty_while_udi_initializes():
    app = Flask(__name__)

    with app.app_context():
        response, status_code = get_playing_streams_response(get_udi_manager=lambda: InitializingUdi())

    assert status_code == 200
    assert response.get_json() == {
        "playing_stream_ids": [],
        "count": 0,
        "initializing": True,
    }


def test_viewer_activity_returns_empty_summary_while_udi_initializes():
    app = Flask(__name__)

    with app.app_context():
        response, status_code = get_viewer_activity_status_response(
            get_udi_manager=lambda: InitializingUdi(),
            get_shadow_monitor_service=lambda: ShadowMonitorStub(),
        )

    assert status_code == 200
    payload = response.get_json()
    assert payload["initializing"] is True
    assert payload["active_channel_count"] == 0
    assert payload["channels"] == []
    assert payload["shadow_monitor_running"] is True
    assert payload["shadow_monitor_enabled"] is True


def test_automation_periods_returns_empty_page_while_udi_initializes():
    app = Flask(__name__)

    def get_automation_config_manager():
        raise AssertionError("startup periods response must not touch automation config")

    with app.app_context():
        response, status_code = handle_automation_periods_response(
            method="GET",
            args={"page": "1", "per_page": "200"},
            payload=None,
            get_automation_config_manager=get_automation_config_manager,
            croniter_available=True,
            croniter_module=None,
            get_udi_manager=lambda: InitializingUdi(),
        )

    assert status_code == 200
    assert response.get_json() == {
        "items": [],
        "total": 0,
        "page": 1,
        "per_page": 200,
        "total_pages": 1,
        "has_next": False,
        "has_prev": False,
        "initializing": True,
    }


def test_upcoming_events_returns_cache_snapshot_while_udi_initializes():
    app = Flask(__name__)

    with app.app_context():
        response, status_code = get_upcoming_automation_events_response(
            args={"hours": "24", "max_events": "100", "force_refresh": "true"},
            get_events_scheduler=lambda: AutomationEventsSchedulerStub(),
            get_automation_config_manager=lambda: AutomationConfigManagerStub(),
            get_udi_manager=lambda: InitializingUdi(),
        )

    assert status_code == 200
    payload = response.get_json()
    assert payload["initializing"] is True
    assert payload["from_cache"] is True
    assert payload["cache_only"] is True
    assert payload["automation_enabled"] is True
    assert payload["events"][0]["period_name"] == "Full Check"

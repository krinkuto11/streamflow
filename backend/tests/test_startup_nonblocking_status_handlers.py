from flask import Flask

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

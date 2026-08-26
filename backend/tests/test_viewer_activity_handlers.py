from apps.api.viewer_activity_handlers import (
    apply_shadow_monitor_context,
    build_viewer_activity_status,
)


def test_viewer_activity_splits_real_and_watcher_clients():
    status = build_viewer_activity_status(
        proxy_status={
            "uuid-1": {
                "state": "active",
                "channel_id": "uuid-1",
                "stream_id": 10,
                "clients": [
                    {"user_agent": "VLC", "username": "viewer"},
                    {"user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0"},
                ],
            },
            "uuid-2": {
                "state": "active",
                "channel_id": "uuid-2",
                "stream_id": 20,
                "clients": [{"user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0"}],
            },
        },
        channels=[
            {"id": 1, "uuid": "uuid-1", "name": "Channel One"},
            {"id": 2, "uuid": "uuid-2", "name": "Channel Two"},
        ],
        watcher_user_agent="StreamFlow-Shadow-Blank-Monitor/1.0",
    )

    assert status["active_channel_count"] == 2
    assert status["real_watched_count"] == 1
    assert status["watcher_only_count"] == 1
    assert status["total_real_clients"] == 1
    assert status["total_watcher_clients"] == 2
    assert status["channels"][0]["channel_name"] == "Channel One"
    assert status["channels"][0]["has_real_clients"] is True
    assert status["channels"][0]["channel_ref"].startswith("channel-")
    assert status["channels"][0]["stream_ref"].startswith("stream-")
    assert status["channels"][1]["watcher_only"] is True


def test_viewer_activity_falls_back_when_clients_are_not_listed():
    status = build_viewer_activity_status(
        proxy_status={
            "7": {
                "state": "active",
                "numeric_channel_id": 7,
                "stream_id": 70,
                "current_viewers": 3,
            }
        },
        channels=[{"id": 7, "uuid": "uuid-7", "name": "Channel Seven"}],
        watcher_user_agent="StreamFlow-Shadow-Blank-Monitor/1.0",
    )

    assert status["active_channel_count"] == 1
    assert status["real_watched_count"] == 1
    assert status["total_real_clients"] == 3
    assert status["total_watcher_clients"] == 0
    assert status["channels"][0]["channel_uuid"] == "uuid-7"


def test_viewer_activity_ignores_idle_or_empty_statuses():
    status = build_viewer_activity_status(
        proxy_status={
            "uuid-idle": {"state": "idle", "channel_id": "uuid-idle", "clients": []},
            "uuid-empty": {"state": "active", "channel_id": "uuid-empty", "clients": []},
        },
        channels=[],
        watcher_user_agent="StreamFlow-Shadow-Blank-Monitor/1.0",
    )

    assert status["active_channel_count"] == 0
    assert status["channels"] == []


def test_viewer_activity_can_attach_safe_shadow_epg_context():
    status = build_viewer_activity_status(
        proxy_status={
            "uuid-1": {
                "state": "waiting_for_clients",
                "channel_id": "uuid-1",
                "stream_id": 10,
                "clients": [{"user_agent": "VLC"}],
            },
        },
        channels=[{"id": 1, "uuid": "uuid-1", "name": "Channel One"}],
        watcher_user_agent="StreamFlow-Shadow-Blank-Monitor/1.0",
    )
    channel_ref = status["channels"][0]["channel_ref"]

    enriched = apply_shadow_monitor_context(
        status,
        {
            "watched_channels": [
                {
                    "channel_ref": channel_ref,
                    "current_program": {
                        "title": "Live: MLB",
                        "state": "current",
                        "start_time": "2026-06-05T18:00:00+00:00",
                        "end_time": "2026-06-05T21:00:00+00:00",
                        "private_provider": "should-not-leak",
                    },
                }
            ]
        },
    )

    assert enriched["channels"][0]["current_program"] == {
        "title": "Live: MLB",
        "state": "current",
        "start_time": "2026-06-05T18:00:00+00:00",
        "end_time": "2026-06-05T21:00:00+00:00",
    }
    assert "should-not-leak" not in repr(enriched)


def test_viewer_activity_can_attach_shadow_watcher_reconnect_context():
    status = build_viewer_activity_status(
        proxy_status={
            "uuid-1": {
                "state": "active",
                "channel_id": "uuid-1",
                "stream_id": 10,
                "clients": [{"user_agent": "VLC"}],
            },
        },
        channels=[{"id": 1, "uuid": "uuid-1", "name": "Channel One"}],
        watcher_user_agent="StreamFlow-Shadow-Blank-Monitor/1.0",
    )
    channel_ref = status["channels"][0]["channel_ref"]

    enriched = apply_shadow_monitor_context(
        status,
        {
            "watched_channels": [
                {
                    "channel_ref": channel_ref,
                    "watcher_state": "reconnecting",
                    "watcher_absent_seconds": 4,
                    "viewer_left_grace_active": True,
                    "viewer_left_grace_remaining_seconds": 26,
                    "proxy_status_gap": True,
                    "private_provider": "should-not-leak",
                }
            ]
        },
    )

    assert enriched["channels"][0]["watcher_state"] == "reconnecting"
    assert enriched["channels"][0]["watcher_absent_seconds"] == 4
    assert enriched["channels"][0]["viewer_left_grace_active"] is True
    assert enriched["channels"][0]["viewer_left_grace_remaining_seconds"] == 26
    assert enriched["channels"][0]["proxy_status_gap"] is True
    assert "should-not-leak" not in repr(enriched)

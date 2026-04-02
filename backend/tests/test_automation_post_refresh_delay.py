from unittest.mock import Mock

from apps.automation.automated_stream_manager import AutomatedStreamManager


def _build_manager(monkeypatch, *, m3u_enabled: bool) -> AutomatedStreamManager:
    manager = AutomatedStreamManager()

    manager._save_state = Mock()
    manager.validate_and_remove_non_matching_streams = Mock(return_value={"details": []})
    manager.discover_and_assign_streams = Mock(
        return_value={"assignment_details": [], "assigned_stream_ids": {}}
    )

    profile = {
        "id": "profile-1",
        "name": "Profile 1",
        "m3u_update": {"enabled": m3u_enabled, "playlists": []},
        "stream_matching": {"enabled": False},
        "stream_checking": {"enabled": False},
    }

    effective_config = {
        "periods": [
            {
                "id": "period-1",
                "name": "Period 1",
                "schedule": {"type": "interval", "value": 60},
                "profile_id": "profile-1",
                "profile": profile,
            }
        ],
        "profile": profile,
    }

    mock_automation_config = Mock()
    mock_automation_config.get_global_settings.return_value = {
        "regular_automation_enabled": True,
    }
    mock_automation_config.get_effective_configuration.return_value = effective_config
    mock_automation_config.get_profile.return_value = profile

    monkeypatch.setattr(
        "apps.automation.automation_config_manager.get_automation_config_manager",
        lambda: mock_automation_config,
    )

    mock_udi = Mock()
    mock_udi.get_channels.return_value = [{"id": 101, "name": "Channel 101", "streams": []}]
    mock_udi.get_channel_by_id.return_value = {"id": 101, "name": "Channel 101", "streams": []}
    monkeypatch.setattr("apps.automation.automated_stream_manager.get_udi_manager", lambda: mock_udi)

    mock_stream_checker = Mock()
    mock_stream_checker.get_status.return_value = {"stream_checking_mode": False}
    mock_stream_checker.check_channels_synchronously.return_value = {}
    monkeypatch.setattr(
        "apps.stream.stream_checker_service.get_stream_checker_service",
        lambda: mock_stream_checker,
    )

    return manager


def test_run_cycle_no_default_post_refresh_delay(monkeypatch):
    manager = _build_manager(monkeypatch, m3u_enabled=True)
    manager.refresh_playlists = Mock(return_value=(True, [{"id": 1, "name": "Account 1"}]))

    sleep_mock = Mock()
    monkeypatch.setattr("apps.automation.automated_stream_manager.time.sleep", sleep_mock)

    manager.run_automation_cycle(forced=True, forced_period_id="period-1")

    sleep_mock.assert_not_called()


def test_run_cycle_honors_configured_post_refresh_delay(monkeypatch):
    manager = _build_manager(monkeypatch, m3u_enabled=True)
    manager.config["post_refresh_delay_seconds"] = 0.25
    manager.refresh_playlists = Mock(return_value=(True, [{"id": 1, "name": "Account 1"}]))

    sleep_mock = Mock()
    monkeypatch.setattr("apps.automation.automated_stream_manager.time.sleep", sleep_mock)

    manager.run_automation_cycle(forced=True, forced_period_id="period-1")

    sleep_mock.assert_called_once_with(0.25)

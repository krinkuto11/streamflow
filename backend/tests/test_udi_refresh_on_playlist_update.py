#!/usr/bin/env python3
"""Tests for decoupled UDI refresh behavior in automation."""

from unittest.mock import Mock

from apps.automation.automated_stream_manager import AutomatedStreamManager


def _build_manager_for_cycle(monkeypatch, *, m3u_enabled: bool) -> AutomatedStreamManager:
    manager = AutomatedStreamManager.__new__(AutomatedStreamManager)
    manager.period_last_run = {}
    manager.last_playlist_update = None
    manager._m3u_accounts_cache = None
    manager.config = {"enabled_features": {"changelog_tracking": False}}
    manager.changelog = Mock()
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
    mock_automation_config.get_global_settings.return_value = {"regular_automation_enabled": True}
    mock_automation_config.get_effective_configuration.return_value = effective_config
    mock_automation_config.get_profile.return_value = profile
    monkeypatch.setattr(
        "apps.automation.automation_config_manager.get_automation_config_manager",
        lambda: mock_automation_config,
    )

    mock_stream_checker = Mock()
    mock_stream_checker.get_status.return_value = {"stream_checking_mode": False}
    mock_stream_checker.check_channels_synchronously.return_value = {}
    monkeypatch.setattr(
        "apps.stream.stream_checker_service.get_stream_checker_service",
        lambda: mock_stream_checker,
    )

    return manager


def test_refresh_playlists_does_not_refresh_udi(monkeypatch):
    import apps.automation.automated_stream_manager as asm

    manager = AutomatedStreamManager.__new__(AutomatedStreamManager)
    manager.config = {
        "enabled_features": {
            "auto_playlist_update": True,
            "changelog_tracking": True,
        },
        "enabled_m3u_accounts": [],
    }
    manager._m3u_accounts_cache = None
    manager.dead_streams_tracker = None
    manager.changelog = Mock()
    manager.last_playlist_update = None

    mock_refresh_m3u = Mock(return_value=Mock(status_code=200))
    mock_get_udi = Mock(return_value=Mock())

    monkeypatch.setattr(asm, "get_m3u_accounts", Mock(return_value=[]))
    monkeypatch.setattr(asm, "get_streams", Mock(return_value=[]))
    monkeypatch.setattr(asm, "refresh_m3u_playlists", mock_refresh_m3u)
    monkeypatch.setattr(asm, "get_udi_manager", mock_get_udi)

    mock_sched_service = Mock()
    monkeypatch.setattr(
        "apps.automation.scheduling_service.get_scheduling_service",
        lambda: mock_sched_service,
    )

    success, _accounts = manager.refresh_playlists(force=True)

    assert success is True
    mock_refresh_m3u.assert_called_once()
    mock_get_udi.assert_not_called()


def test_automation_cycle_refreshes_udi_without_m3u_refresh(monkeypatch):
    import apps.automation.automated_stream_manager as asm

    manager = _build_manager_for_cycle(monkeypatch, m3u_enabled=False)
    manager.refresh_playlists = Mock(return_value=(True, []))

    mock_udi = Mock()
    mock_udi.get_channels.return_value = [{"id": 101, "name": "Channel 101", "streams": []}]
    monkeypatch.setattr(asm, "get_udi_manager", Mock(return_value=mock_udi))
    monkeypatch.setattr(asm, "get_m3u_accounts", Mock(return_value=[]))
    monkeypatch.setattr(asm.time, "sleep", Mock())

    manager.run_automation_cycle(forced=True, forced_period_id="period-1")

    manager.refresh_playlists.assert_not_called()
    mock_udi.refresh_m3u_accounts.assert_called_once()
    mock_udi.refresh_streams.assert_called_once()
    mock_udi.refresh_channels.assert_called_once()
    mock_udi.refresh_channel_groups.assert_called_once()
    mock_udi.refresh_channel_profiles.assert_called_once()


def test_automation_cycle_refreshes_udi_once_with_m3u_refresh(monkeypatch):
    import apps.automation.automated_stream_manager as asm

    manager = _build_manager_for_cycle(monkeypatch, m3u_enabled=True)
    manager.refresh_playlists = Mock(return_value=(True, [{"id": 1, "name": "Account 1"}]))

    mock_udi = Mock()
    mock_udi.get_channels.return_value = [{"id": 101, "name": "Channel 101", "streams": []}]
    monkeypatch.setattr(asm, "get_udi_manager", Mock(return_value=mock_udi))
    monkeypatch.setattr(asm, "get_m3u_accounts", Mock(return_value=[]))
    monkeypatch.setattr(asm.time, "sleep", Mock())

    manager.run_automation_cycle(forced=True, forced_period_id="period-1")

    manager.refresh_playlists.assert_called_once()
    mock_udi.refresh_m3u_accounts.assert_called_once()
    mock_udi.refresh_streams.assert_called_once()
    mock_udi.refresh_channels.assert_called_once()
    mock_udi.refresh_channel_groups.assert_called_once()
    mock_udi.refresh_channel_profiles.assert_called_once()

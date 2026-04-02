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


def test_refresh_playlists_returns_false_on_404(monkeypatch):
    manager = AutomatedStreamManager()
    manager.config["enabled_features"]["changelog_tracking"] = False

    monkeypatch.setattr(
        "apps.automation.automated_stream_manager.get_m3u_accounts",
        lambda: [{"id": 9, "name": "Flaky Provider", "is_active": True}],
    )
    monkeypatch.setattr(
        "apps.automation.automated_stream_manager.get_streams",
        lambda log_result=False: [],
    )
    monkeypatch.setattr(
        "apps.automation.automated_stream_manager.refresh_m3u_playlists",
        lambda account_id=None: Mock(status_code=404),
    )

    mock_sched_service = Mock()
    monkeypatch.setattr(
        "apps.automation.scheduling_service.get_scheduling_service",
        lambda: mock_sched_service,
    )

    success, refreshed_accounts = manager.refresh_playlists(force=True)

    assert success is False
    assert refreshed_accounts == []


def test_cycle_aborts_on_suspicious_stream_pool_drop(monkeypatch):
    manager = _build_manager(monkeypatch, m3u_enabled=True)

    manager.refresh_playlists = Mock(return_value=(True, [{"id": 1, "name": "Account 1"}]))
    manager._refresh_udi_cache_for_automation_cycle = Mock(return_value=True)

    # Pre-refresh pool is healthy, post-refresh pool is poisoned/empty.
    stream_calls = {"count": 0}

    def _stream_side_effect(log_result=False):
        stream_calls["count"] += 1
        if stream_calls["count"] == 1:
            return [{"id": i} for i in range(1000)]
        return []

    monkeypatch.setattr(
        "apps.automation.automated_stream_manager.get_streams",
        _stream_side_effect,
    )

    monkeypatch.setattr("apps.automation.automated_stream_manager.time.sleep", Mock())

    manager.run_automation_cycle(forced=True, forced_period_id="period-1")

    # Safety gate should skip destructive operations.
    manager.validate_and_remove_non_matching_streams.assert_not_called()
    manager.discover_and_assign_streams.assert_not_called()

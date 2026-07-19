from unittest.mock import Mock
from datetime import datetime, timedelta
import threading

from apps.automation.automated_stream_manager import AutomatedStreamManager


def test_due_period_does_not_inherit_another_period_profile(monkeypatch):
    """A due period must use its own profile, not config['profile'] from another period."""
    manager = AutomatedStreamManager()

    # Keep the cycle lightweight and deterministic for this unit test.
    manager._save_state = Mock()
    manager.refresh_playlists = Mock(return_value=(True, []))
    manager.validate_and_remove_non_matching_streams = Mock(return_value={"details": []})
    manager.discover_and_assign_streams = Mock(
        return_value={"assignment_details": [], "assigned_stream_ids": {}}
    )
    monkeypatch.setattr("apps.automation.automated_stream_manager.time.sleep", lambda _seconds: None)

    # Only period-1 is due in this cycle.
    monkeypatch.setattr(
        manager,
        "_is_period_due",
        lambda period_id, _period_info: period_id == "period-1",
    )

    profile_match_only = {
        "id": "profile-1",
        "name": "Match Only",
        "m3u_update": {"enabled": False},
        "stream_matching": {"enabled": True},
        "stream_checking": {"enabled": False},
    }
    profile_checker_only = {
        "id": "profile-2",
        "name": "Checker Only",
        "m3u_update": {"enabled": False},
        "stream_matching": {"enabled": False},
        "stream_checking": {"enabled": True, "check_all_streams": False},
    }

    # config['profile'] intentionally points to the wrong profile to reproduce the bug.
    effective_config = {
        "periods": [
            {
                "id": "period-1",
                "name": "Every 60m",
                "schedule": {"type": "interval", "value": 60},
                "profile_id": "profile-1",
                "profile": profile_match_only,
            },
            {
                "id": "period-2",
                "name": "10:00 Daily",
                "schedule": {"type": "cron", "value": "0 10 * * *"},
                "profile_id": "profile-2",
                "profile": profile_checker_only,
            },
        ],
        "profile": profile_checker_only,
    }

    mock_automation_config = Mock()
    mock_automation_config.get_global_settings.return_value = {
        "regular_automation_enabled": True,
    }
    mock_automation_config.get_effective_configuration.return_value = effective_config
    mock_automation_config.get_profile.side_effect = lambda profile_id: {
        "profile-1": profile_match_only,
        "profile-2": profile_checker_only,
    }.get(str(profile_id))

    monkeypatch.setattr(
        "apps.automation.automated_stream_manager.get_automation_config_manager",
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

    manager.run_automation_cycle(forced=False)

    # Regression assertion: period-2 checker profile must not run during period-1 execution.
    mock_stream_checker.check_channels_synchronously.assert_not_called()


def test_connectivity_retry_resumes_quality_without_provider_refresh_or_matching(monkeypatch):
    manager = AutomatedStreamManager()
    manager.config["enabled_features"]["changelog_tracking"] = False
    manager._save_state = Mock()
    manager.refresh_playlists = Mock(return_value=(True, []))
    manager.validate_and_remove_non_matching_streams = Mock(return_value={"details": []})
    manager.discover_and_assign_streams = Mock(
        return_value={"assignment_details": [], "assigned_stream_ids": {}}
    )
    manager._scheduler_retry_state = {
        "period-1": {
            "period_name": "Full Check",
            "attempt": 1,
            "max_attempts": 3,
            "next_retry_at": (datetime.now() - timedelta(seconds=1)).isoformat(),
            "exhausted": False,
            "pending_channel_ids": [101],
            "completed_channel_ids": [100],
            "target_stream_ids": {},
            "check_all_stream_ids": [101],
            "skip_provider_refresh": True,
            "resume_stage": "quality_checking",
        },
    }
    manager.period_last_run = {"period-1": datetime.now() - timedelta(hours=12)}

    profile = {
        "id": "profile-1",
        "name": "Full Check",
        "m3u_update": {"enabled": True, "playlists": []},
        "stream_matching": {"enabled": True},
        "stream_checking": {"enabled": True, "check_all_streams": True},
    }
    effective_config = {
        "periods": [{
            "id": "period-1",
            "name": "Full Check",
            "schedule": {"type": "cron", "value": "0 21 * * *"},
            "profile_id": "profile-1",
            "profile": profile,
        }],
    }
    automation_config = Mock()
    automation_config.get_global_settings.return_value = {
        "regular_automation_enabled": True,
    }
    automation_config.get_effective_configuration.return_value = effective_config
    automation_config.get_profile.return_value = profile
    monkeypatch.setattr(
        "apps.automation.automated_stream_manager.get_automation_config_manager",
        lambda: automation_config,
    )
    monkeypatch.setattr(
        "apps.automation.automation_config_manager.get_automation_config_manager",
        lambda: automation_config,
    )

    udi = Mock()
    udi.get_channels.return_value = [
        {"id": 100, "name": "Already checked", "streams": [1]},
        {"id": 101, "name": "Pending", "streams": [2]},
    ]
    udi.get_channel_by_id.side_effect = lambda channel_id: {
        "id": channel_id,
        "name": "Pending" if int(channel_id) == 101 else "Already checked",
        "streams": [2] if int(channel_id) == 101 else [1],
    }
    udi.is_network_ready.return_value = False
    monkeypatch.setattr(
        "apps.automation.automated_stream_manager.get_udi_manager",
        lambda: udi,
    )

    stream_checker = Mock()
    stream_checker.get_status.return_value = {"stream_checking_mode": False}
    stream_checker._require_quality_check_connectivity.return_value = None
    stream_checker.config.get.side_effect = lambda key, default=None: default
    stream_checker.abort_current_check = threading.Event()
    stream_checker.check_channels_synchronously.return_value = {
        101: {
            "success": True,
            "channel_id": 101,
            "channel_name": "Pending",
            "stream_stats": [],
        },
    }
    monkeypatch.setattr(
        "apps.automation.automated_stream_manager.get_stream_checker_service",
        lambda: stream_checker,
    )
    monkeypatch.setattr(
        "apps.stream.stream_checker_service.get_stream_checker_service",
        lambda: stream_checker,
    )

    manager.run_automation_cycle(forced=False)

    manager.refresh_playlists.assert_not_called()
    manager.validate_and_remove_non_matching_streams.assert_not_called()
    manager.discover_and_assign_streams.assert_not_called()
    checked_ids = stream_checker.check_channels_synchronously.call_args.kwargs["channel_ids"]
    assert checked_ids == [101]
    assert "period-1" not in manager._scheduler_retry_state

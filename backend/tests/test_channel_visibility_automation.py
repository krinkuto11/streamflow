from datetime import datetime, timezone
from unittest.mock import Mock, patch

from apps.automation.channel_visibility_automation import (
    STATE_KEY,
    ChannelVisibilityAutomation,
    resolve_channel_visibility_config,
)


class FakeDb:
    def __init__(self, state=None):
        self.settings = {STATE_KEY: state or {}}

    def get_system_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_system_setting(self, key, value):
        self.settings[key] = value
        return True


class FakeResponse:
    status_code = 204


class FakeUdi:
    def __init__(self):
        self.updated = []

    def update_channel(self, channel_id, channel_data):
        self.updated.append((channel_id, channel_data))
        return True


def make_service(db, patch_request=None, udi=None):
    return ChannelVisibilityAutomation(
        db_provider=lambda: db,
        patch_request=patch_request or Mock(return_value=FakeResponse()),
        base_url_provider=lambda: "http://dispatcharr.test",
        udi_provider=lambda: udi or FakeUdi(),
        clock=lambda: datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc),
    )


def test_no_regex_hide_marks_channel_and_records_streamflow_state():
    db = FakeDb()
    patch = Mock(return_value=FakeResponse())
    udi = FakeUdi()
    service = make_service(db, patch_request=patch, udi=udi)

    result = service.handle_no_regex(
        {"id": 10, "name": "Test Channel", "hidden_from_output": False},
        config={"enabled": True, "hide_on_no_regex": True},
        details={"match_source": "regex"},
    )

    assert result["action"] == "hidden"
    assert result["changed"] is True
    assert result["channel_name"] == "Test Channel"
    patch.assert_called_once_with(
        "http://dispatcharr.test/api/channels/channels/10/",
        {"hidden_from_output": True},
    )
    state = db.settings[STATE_KEY]["10"]
    assert state["hidden_by"] == "streamflow"
    assert state["reason"] == "no_regex"
    assert state["channel_ref"] == "channel-10"
    assert state["channel_name"] == "Test Channel"
    assert udi.updated[0][1]["hidden_from_output"] is True


def test_manual_hidden_channel_is_not_claimed_by_no_regex_hide():
    db = FakeDb()
    patch = Mock(return_value=FakeResponse())
    service = make_service(db, patch_request=patch)

    result = service.handle_no_regex(
        {"id": 11, "name": "Manual Hidden", "hidden_from_output": True},
        config={"enabled": True, "hide_on_no_regex": True},
    )

    assert result["action"] == "manual_hidden_preserved"
    assert result["changed"] is False
    assert db.settings[STATE_KEY] == {}
    patch.assert_not_called()


def test_recovered_channel_unhides_only_streamflow_managed_state():
    db = FakeDb(state={"12": {"hidden_by": "streamflow", "reason": "all_failed"}})
    patch = Mock(return_value=FakeResponse())
    service = make_service(db, patch_request=patch)

    result = service.handle_quality_result(
        {"id": 12, "name": "Recovered", "hidden_from_output": True},
        good_streams_count=1,
        dead_streams_count=0,
        revived_streams_count=1,
        config={"enabled": True, "unhide_on_recovered": True},
    )

    assert result["action"] == "unhidden"
    assert result["changed"] is True
    patch.assert_called_once_with(
        "http://dispatcharr.test/api/channels/channels/12/",
        {"hidden_from_output": False},
    )
    assert "12" not in db.settings[STATE_KEY]


def test_recovered_manual_hidden_channel_stays_hidden():
    db = FakeDb()
    patch = Mock(return_value=FakeResponse())
    service = make_service(db, patch_request=patch)

    result = service.handle_quality_result(
        {"id": 13, "name": "Manual Hidden", "hidden_from_output": True},
        good_streams_count=2,
        dead_streams_count=0,
        config={"enabled": True, "unhide_on_recovered": True},
    )

    assert result["action"] == "manual_hidden_preserved"
    assert result["changed"] is False
    patch.assert_not_called()


def test_all_failed_hide_uses_quality_result_gate():
    db = FakeDb()
    patch = Mock(return_value=FakeResponse())
    service = make_service(db, patch_request=patch)

    result = service.handle_quality_result(
        {"id": 14, "name": "All Failed", "hidden_from_output": False},
        good_streams_count=0,
        dead_streams_count=3,
        config={"enabled": True, "hide_on_all_failed": True},
    )

    assert result["action"] == "hidden"
    assert result["reason"] == "all_failed"
    assert db.settings[STATE_KEY]["14"]["reason"] == "all_failed"


def test_no_streams_hide_uses_quality_result_total_streams_gate():
    db = FakeDb()
    patch = Mock(return_value=FakeResponse())
    service = make_service(db, patch_request=patch)

    result = service.handle_quality_result(
        {"id": 18, "name": "No Streams", "hidden_from_output": False},
        good_streams_count=0,
        dead_streams_count=0,
        config={"enabled": True, "hide_on_no_streams": True},
        details={"total_streams": 0},
    )

    assert result["action"] == "hidden"
    assert result["reason"] == "no_streams"
    assert db.settings[STATE_KEY]["18"]["reason"] == "no_streams"


def test_streams_recovered_unhides_only_no_streams_state():
    db = FakeDb(state={"19": {"hidden_by": "streamflow", "reason": "no_streams"}})
    patch = Mock(return_value=FakeResponse())
    service = make_service(db, patch_request=patch)

    result = service.handle_quality_result(
        {"id": 19, "name": "Streams Back", "hidden_from_output": True},
        good_streams_count=0,
        dead_streams_count=0,
        config={"enabled": True, "hide_on_no_streams": True, "unhide_on_recovered": True},
        details={"total_streams": 2},
    )

    assert result["action"] == "unhidden"
    assert result["reason"] == "streams_recovered"
    assert "19" not in db.settings[STATE_KEY]


def test_all_failed_wins_over_no_streams_recovery_after_visual_probe():
    db = FakeDb(state={"20": {"hidden_by": "streamflow", "reason": "no_streams"}})
    patch = Mock(return_value=FakeResponse())
    service = make_service(db, patch_request=patch)

    result = service.handle_quality_result(
        {"id": 20, "name": "NASA", "hidden_from_output": True},
        good_streams_count=0,
        dead_streams_count=2,
        revived_streams_count=2,
        config={
            "enabled": True,
            "hide_on_no_streams": True,
            "hide_on_all_failed": True,
            "unhide_on_recovered": True,
        },
        details={
            "total_streams": 2,
            "good_streams_count": 0,
            "dead_streams_count": 2,
            "revived_streams_count": 2,
        },
    )

    assert result["action"] == "hidden_already_managed"
    assert result["reason"] == "all_failed"
    assert result["changed"] is False
    assert db.settings[STATE_KEY]["20"]["reason"] == "all_failed"
    patch.assert_not_called()


def test_disabled_visibility_automation_is_read_only():
    db = FakeDb()
    patch = Mock(return_value=FakeResponse())
    service = make_service(db, patch_request=patch)

    result = service.handle_quality_result(
        {"id": 15, "hidden_from_output": False},
        good_streams_count=0,
        dead_streams_count=5,
        config={"enabled": False, "hide_on_all_failed": True},
    )

    assert result["action"] == "disabled"
    assert result["changed"] is False
    assert db.settings[STATE_KEY] == {}
    patch.assert_not_called()


def test_profile_visibility_override_can_disable_global_hides():
    global_config = {
        "enabled": True,
        "hide_on_no_streams": True,
        "hide_on_all_failed": True,
        "unhide_on_recovered": True,
    }
    profile = {
        "name": "Teamarr Event Preflight",
        "channel_visibility_automation": {
            "inherit_global": False,
            "enabled": False,
            "hide_on_no_streams": False,
            "hide_on_all_failed": False,
            "unhide_on_recovered": False,
        },
    }

    resolved = resolve_channel_visibility_config(global_config, profile)

    assert resolved["enabled"] is False
    assert resolved["hide_on_no_streams"] is False
    assert resolved["hide_on_all_failed"] is False
    assert resolved["unhide_on_recovered"] is False


def test_profile_visibility_ignores_legacy_global_inheritance_flag():
    global_config = {
        "enabled": True,
        "hide_on_no_streams": True,
        "hide_on_all_failed": True,
    }
    profile = {
        "name": "Full Run",
        "channel_visibility_automation": {
            "inherit_global": True,
            "enabled": False,
            "hide_on_no_streams": False,
            "hide_on_all_failed": False,
        },
    }

    resolved = resolve_channel_visibility_config(global_config, profile)

    assert resolved["enabled"] is False
    assert resolved["hide_on_no_streams"] is False
    assert resolved["hide_on_all_failed"] is False
    assert "inherit_global" not in resolved


def test_stream_checker_visibility_hook_uses_quality_counts():
    from apps.stream.stream_checker_service import StreamCheckerService

    service = StreamCheckerService.__new__(StreamCheckerService)
    service.config = {
        "channel_visibility_automation": {
            "enabled": True,
            "hide_on_all_failed": True,
            "unhide_on_recovered": True,
        }
    }
    service.channel_visibility_automation = Mock()
    service.channel_visibility_automation.handle_quality_result.return_value = {
        "action": "hidden",
        "changed": True,
        "reason": "all_failed",
        "channel_id": 16,
    }

    result = service._apply_channel_visibility_after_check(
        {"id": 16, "hidden_from_output": False},
        good_streams_count=0,
        dead_streams_count=4,
        revived_streams_count=0,
        total_streams=4,
    )

    assert result["action"] == "hidden"
    service.channel_visibility_automation.handle_quality_result.assert_called_once()
    kwargs = service.channel_visibility_automation.handle_quality_result.call_args.kwargs
    assert kwargs["good_streams_count"] == 0
    assert kwargs["dead_streams_count"] == 4
    assert kwargs["details"]["total_streams"] == 4


def test_stream_checker_visibility_hook_uses_profile_override():
    from apps.stream.stream_checker_service import StreamCheckerService

    service = StreamCheckerService.__new__(StreamCheckerService)
    service.config = {
        "channel_visibility_automation": {
            "enabled": True,
            "hide_on_all_failed": True,
        }
    }
    service.channel_visibility_automation = Mock()
    service.channel_visibility_automation.handle_quality_result.return_value = {
        "action": "disabled",
        "changed": False,
        "reason": "quality_result",
        "channel_id": 16,
    }

    result = service._apply_channel_visibility_after_check(
        {"id": 16, "hidden_from_output": False},
        good_streams_count=0,
        dead_streams_count=4,
        revived_streams_count=0,
        total_streams=4,
        profile={
            "channel_visibility_automation": {
                "inherit_global": False,
                "enabled": False,
                "hide_on_all_failed": False,
            }
        },
    )

    assert result["action"] == "disabled"
    kwargs = service.channel_visibility_automation.handle_quality_result.call_args.kwargs
    assert kwargs["config"]["enabled"] is False
    assert kwargs["config"]["hide_on_all_failed"] is False


def test_discovery_no_regex_visibility_event_does_not_queue_as_checking_only():
    from apps.automation.automated_stream_manager import AutomatedStreamManager

    channel = {
        "id": 17,
        "name": "No Regex",
        "channel_group_id": None,
        "group_id": None,
        "streams": [],
    }
    profile = {
        "name": "Matching Profile",
        "stream_matching": {"enabled": True},
        "stream_checking": {"enabled": True},
        "global_action": {"affected": False},
    }

    manager = AutomatedStreamManager.__new__(AutomatedStreamManager)
    manager.config = {
        "enabled_features": {
            "auto_stream_discovery": True,
            "changelog_tracking": False,
        },
        "enabled_m3u_accounts": [],
    }
    manager._m3u_accounts_cache = None
    manager.changelog = Mock()
    manager._update_run_progress = Mock()
    manager._mark_checking_only_channels = Mock()
    manager._filter_channels_by_profile = Mock(side_effect=lambda channels, _: channels)
    manager._get_channel_visibility_config = Mock(return_value={
        "enabled": True,
        "hide_on_no_regex": True,
    })
    manager.channel_visibility_automation = Mock()
    manager.channel_visibility_automation.handle_no_regex.return_value = {
        "action": "hidden",
        "changed": True,
        "reason": "no_regex",
        "channel_id": 17,
    }
    manager.regex_matcher = Mock()
    manager.regex_matcher.reload_patterns = Mock()
    manager.regex_matcher.has_regex_patterns.return_value = False
    manager.regex_matcher.get_match_by_tvg_id.return_value = False
    manager.regex_matcher._get_effective_channel_config.return_value = None

    udi = Mock()
    udi.get_channels.return_value = [channel]
    acm = Mock()
    acm.get_effective_configuration.return_value = {
        "profile": profile,
        "periods": [{"id": "p1", "profile": profile}],
    }

    with patch("apps.automation.automated_stream_manager.get_streams", return_value=[]), \
        patch("apps.automation.automated_stream_manager.get_m3u_accounts", return_value=[]), \
        patch("apps.automation.automated_stream_manager.get_udi_manager", return_value=udi), \
        patch("apps.automation.automated_stream_manager.get_automation_config_manager", return_value=acm):
        result = manager._discover_and_assign_streams_impl(force=True, skip_check_trigger=True)

    assert result["channel_visibility_events"][0]["action"] == "hidden"
    manager.channel_visibility_automation.handle_no_regex.assert_called_once()
    manager._mark_checking_only_channels.assert_called_once_with([], udi, True)

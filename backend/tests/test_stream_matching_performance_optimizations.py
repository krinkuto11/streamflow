import threading
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import event

from apps.automation.automated_stream_manager import (
    AutomatedStreamManager,
    RegexChannelMatcher,
    _compile_stream_search_regex,
)
from apps.database.manager import get_db_manager
from apps.stream.dead_streams_tracker import DeadStreamsTracker


def test_match_streams_batch_calls_matcher_once_per_stream_signature():
    manager = AutomatedStreamManager.__new__(AutomatedStreamManager)
    manager.regex_matcher = Mock()
    manager.regex_matcher.match_stream_to_channels.return_value = ["101"]
    manager.dead_streams_tracker = None

    streams = [
        {"id": "s1", "name": "Event Alpha", "url": "http://a", "m3u_account": 1, "tvg_id": "alpha"},
        {"id": "s2", "name": "Event Alpha", "url": "http://b", "m3u_account": 1, "tvg_id": "alpha"},
    ]

    assignments, details = manager._match_streams_batch(
        streams=streams,
        channel_streams={"101": set()},
        dead_stream_removal_enabled=False,
        channel_to_revive_enabled={},
        channel_tvg_map={},
        channel_to_match_priorities={},
        channel_to_group_map={},
        channel_name_map={},
    )

    assert manager.regex_matcher.match_stream_to_channels.call_count == 1
    assert assignments["101"] == ["s1", "s2"]
    assert len(details["101"]) == 2


def test_matching_uses_one_dead_snapshot_and_preserves_revival(clean_test_db):
    manager = AutomatedStreamManager.__new__(AutomatedStreamManager)
    manager.regex_matcher = Mock()
    manager.regex_matcher.match_stream_to_channels.return_value = ["101"]
    manager.dead_streams_tracker = DeadStreamsTracker()
    dead_url = "http://example.test/dead"
    get_db_manager().mark_stream_dead(dead_url, 1, "Dead", reason="offline")
    streams = [
        {"id": i, "name": f"Stream {i}", "url": dead_url if i == 1 else f"http://example.test/{i}"}
        for i in range(1, 201)
    ]
    queries = []

    def record(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT") and "dead_streams" in statement:
            queries.append(statement)

    event.listen(clean_test_db, "before_cursor_execute", record)
    try:
        dead_urls = frozenset(manager.dead_streams_tracker.get_dead_stream_reasons())
        assignments, _ = manager._match_streams_batch(
            streams, {"101": set()}, True, {"101": False}, dead_stream_urls=dead_urls
        )
        assert assignments["101"] == list(range(2, 201))
        assert len(queries) == 1

        revival_assignments, _ = manager._match_streams_batch(
            streams[:2], {"101": set()}, True, {"101": True}, dead_stream_urls=dead_urls
        )
        assert revival_assignments["101"] == [1, 2]
        assert len(queries) == 1

        allowed_assignments, _ = manager._match_streams_batch(
            streams[:2], {"101": set()}, False, {"101": False}
        )
        assert allowed_assignments["101"] == [1, 2]
        assert len(queries) == 1

        with pytest.raises(RuntimeError, match="Dead stream snapshot unavailable"):
            manager._match_streams_batch(
                streams[:2], {"101": set()}, True, {"101": False}
            )
        assert len(queries) == 1
    finally:
        event.remove(clean_test_db, "before_cursor_execute", record)


def test_discovery_shares_one_dead_snapshot_with_matching_workers():
    manager = AutomatedStreamManager.__new__(AutomatedStreamManager)
    manager.config = {
        "enabled_features": {"auto_stream_discovery": True, "changelog_tracking": False},
        "enabled_m3u_accounts": [],
    }
    manager._manual_stop_requested = threading.Event()
    manager._lock = threading.Lock()
    manager._m3u_accounts_cache = [{"id": 1, "name": "Provider", "is_active": True}]
    manager.regex_matcher = Mock()
    manager.regex_matcher.has_regex_patterns.return_value = True
    manager.regex_matcher.get_match_by_tvg_id.return_value = False
    manager.regex_matcher.match_stream_to_channels.return_value = ["10"]
    manager._filter_channels_by_profile = Mock(side_effect=lambda channels, _reason: channels)
    manager._record_channel_visibility_events = Mock()
    manager._is_dead_stream_removal_enabled = Mock(return_value=True)
    manager._update_run_progress = Mock()
    manager._get_channel_visibility_config = Mock(return_value={})
    manager.dead_streams_tracker = Mock()
    manager.dead_streams_tracker.get_dead_stream_reasons.return_value = {
        "http://example.test/dead": "offline"
    }
    manager.dead_streams_tracker.is_dead.side_effect = AssertionError("per-stream DB lookup")

    automation_config = Mock()
    automation_config.get_effective_configuration.return_value = {
        "profile": {
            "stream_matching": {"enabled": True, "match_priority_order": ["regex"]},
            "stream_checking": {"enabled": False},
        }
    }
    udi = Mock()
    udi.get_channel_streams.return_value = []
    session_manager = Mock()
    session_manager.get_channels_in_active_sessions.return_value = []

    with patch("apps.automation.automated_stream_manager.get_streams", return_value=[
            {"id": 1, "name": "Dead", "url": "http://example.test/dead", "m3u_account": 1}
        ]), patch("apps.automation.automated_stream_manager.get_channels", return_value=[
            {"id": 10, "name": "Channel 10"}
        ]), patch("apps.automation.automated_stream_manager.get_udi_manager", return_value=udi), \
         patch("apps.automation.automated_stream_manager.get_automation_config_manager", return_value=automation_config), \
         patch("apps.stream.stream_session_manager.get_session_manager", return_value=session_manager), \
         patch("apps.automation.automated_stream_manager.assign_streams_to_channel") as assign:
        result = manager._discover_and_assign_streams_impl(force=True, skip_check_trigger=True)
        assert result.get("success") is not False
        assert result["assignment_count"] == {}
        assert "allowed_channel_ids" not in manager.regex_matcher.match_stream_to_channels.call_args.kwargs
        manager.dead_streams_tracker.get_dead_stream_reasons.assert_called_once_with()
        manager.dead_streams_tracker.is_dead.assert_not_called()
        assign.assert_not_called()

        manager.regex_matcher.match_stream_to_channels.reset_mock()
        single_result = manager._discover_and_assign_streams_impl(
            force=True, skip_check_trigger=True, channel_id=10,
        )
        assert single_result.get("success") is not False
        assert manager.regex_matcher.match_stream_to_channels.call_args.kwargs["allowed_channel_ids"] == frozenset(("10",))

        manager.dead_streams_tracker.get_dead_stream_reasons.side_effect = RuntimeError(
            "injected database read failure"
        )
        with patch.object(manager, "_match_streams_batch", side_effect=AssertionError("worker started")) as worker:
            failed = manager._discover_and_assign_streams_impl(force=True, skip_check_trigger=True)
            worker.assert_not_called()

    assert failed["success"] is False
    assert "Dead stream state unavailable" in failed["error"]
    assert failed["assignment_count"] == {}
    assert failed["assigned_stream_ids"] == {}
    assign.assert_not_called()


def test_regex_compilation_cache_reuses_compiled_pattern(monkeypatch):
    monkeypatch.setattr(
        RegexChannelMatcher,
        "_load_patterns",
        lambda self: {
            "patterns": {
                "10": {
                    "name": "Sports Plus",
                    "enabled": True,
                    "match_by_tvg_id": False,
                    "regex_patterns": [{"pattern": "CHANNEL_NAME", "m3u_accounts": None}],
                }
            },
            "global_settings": {"case_sensitive": True},
        },
    )
    monkeypatch.setattr(RegexChannelMatcher, "_load_group_patterns", lambda self: {})

    _compile_stream_search_regex.cache_clear()
    matcher = RegexChannelMatcher()

    matches_first = matcher.match_stream_to_channels("Watch Sports Plus Live")
    matches_second = matcher.match_stream_to_channels("Watch Sports Plus Live")
    cache_info = _compile_stream_search_regex.cache_info()

    assert "10" in matches_first
    assert "10" in matches_second
    assert cache_info.misses == 1
    assert cache_info.hits >= 1


def test_group_pattern_lookup_uses_in_memory_cache(monkeypatch):
    load_group_calls = {"count": 0}

    monkeypatch.setattr(
        RegexChannelMatcher,
        "_load_patterns",
        lambda self: {
            "patterns": {},
            "global_settings": {"case_sensitive": True},
        },
    )

    def _fake_load_group_patterns(self):
        load_group_calls["count"] += 1
        return {
            "777": {
                "name": "Group Pattern",
                "enabled": True,
                "match_by_tvg_id": False,
                "regex_patterns": [{"pattern": "Group Event", "m3u_accounts": None}],
            }
        }

    monkeypatch.setattr(RegexChannelMatcher, "_load_group_patterns", _fake_load_group_patterns)

    matcher = RegexChannelMatcher()
    assert load_group_calls["count"] == 1

    for _ in range(5):
        matches = matcher.match_stream_to_channels(
            "Group Event HD",
            channel_to_group_map={"5001": "777"},
        )
        assert "5001" in matches

    assert load_group_calls["count"] == 1


def test_single_channel_scope_preserves_regex_tvg_group_and_provider_matches():
    matcher = RegexChannelMatcher.__new__(RegexChannelMatcher)
    matcher.lock = threading.RLock()
    matcher.channel_patterns = {
        "patterns": {
            "10": {
                "name": "Target",
                "enabled": True,
                "match_by_tvg_id": False,
                "regex_patterns": [{"pattern": "^Target", "m3u_accounts": [2]}],
            },
            "20": {
                "name": "Other",
                "enabled": True,
                "match_by_tvg_id": True,
                "regex_patterns": [{"pattern": "^Other", "m3u_accounts": None}],
            },
        },
        "global_settings": {"case_sensitive": True},
    }
    matcher.group_patterns = {
        "7": {
            "name": "Group",
            "enabled": True,
            "match_by_tvg_id": False,
            "regex_patterns": [{"pattern": "^Group", "m3u_accounts": None}],
        }
    }
    tvg_ids = {"10": "target.id", "20": "other.id", "30": "group.id"}
    priorities = {"10": ["regex", "tvg"], "20": ["tvg", "regex"], "30": ["regex", "tvg"]}
    groups = {"30": 7}

    for stream_name, account, tvg_id in (
        ("Target HD", 2, None),
        ("Target HD", 1, None),
        ("Other HD", 2, "other.id"),
        ("Unrelated", 2, "other.id"),
        ("Group HD", 2, None),
        ("Unrelated", 2, None),
    ):
        all_matches = matcher.match_stream_to_channels(
            stream_name, account, tvg_id, tvg_ids, priorities, groups,
        )
        for target in ("10", "20", "30"):
            scoped_matches = matcher.match_stream_to_channels(
                stream_name, account, tvg_id, tvg_ids, priorities, groups,
                allowed_channel_ids=frozenset((target,)),
            )
            assert scoped_matches == ([target] if target in all_matches else [])


def test_single_channel_scope_avoids_unrelated_channel_evaluations():
    matcher = RegexChannelMatcher.__new__(RegexChannelMatcher)
    matcher.lock = threading.RLock()
    matcher.channel_patterns = {
        "patterns": {
            str(i): {
                "name": f"Channel {i}",
                "enabled": True,
                "match_by_tvg_id": False,
                "regex_patterns": [{"pattern": f"^Channel {i}$", "m3u_accounts": None}],
            }
            for i in range(1, 101)
        },
        "global_settings": {"case_sensitive": True},
    }
    matcher.group_patterns = {}
    matcher._get_effective_channel_config = Mock(wraps=matcher._get_effective_channel_config)

    for i in range(40):
        matcher.match_stream_to_channels(f"Stream {i}", 1)
    assert matcher._get_effective_channel_config.call_count == 4000

    matcher._get_effective_channel_config.reset_mock()
    for i in range(40):
        matcher.match_stream_to_channels(
            f"Stream {i}", 1, allowed_channel_ids=frozenset(("10",)),
        )
    assert matcher._get_effective_channel_config.call_count == 40


def test_single_channel_batch_scope_preserves_dead_stream_revival():
    manager = AutomatedStreamManager.__new__(AutomatedStreamManager)
    manager.regex_matcher = RegexChannelMatcher.__new__(RegexChannelMatcher)
    manager.regex_matcher.lock = threading.RLock()
    manager.regex_matcher.channel_patterns = {
        "patterns": {
            "10": {
                "name": "Target",
                "enabled": True,
                "match_by_tvg_id": False,
                "regex_patterns": [{"pattern": "Target", "m3u_accounts": [2]}],
            },
            "20": {
                "name": "Other",
                "enabled": True,
                "match_by_tvg_id": False,
                "regex_patterns": [{"pattern": "Other", "m3u_accounts": None}],
            },
        },
        "global_settings": {"case_sensitive": True},
    }
    manager.regex_matcher.group_patterns = {}
    streams = [
        {"id": 1, "name": "Target", "m3u_account": 2, "url": "http://dead.test/1"},
        {"id": 2, "name": "Target", "m3u_account": 2, "url": "http://live.test/2"},
        {"id": 3, "name": "Other", "m3u_account": 2, "url": "http://live.test/3"},
        {"id": 4, "name": "Target", "m3u_account": 1, "url": "http://live.test/4"},
    ]
    for allow_revive, expected in ((False, [2]), (True, [1, 2])):
        full, _ = manager._match_streams_batch(
            streams, {"10": set()}, True, {"10": allow_revive},
            dead_stream_urls=frozenset(("http://dead.test/1",)),
        )
        scoped, _ = manager._match_streams_batch(
            streams, {"10": set()}, True, {"10": allow_revive},
            dead_stream_urls=frozenset(("http://dead.test/1",)),
            allowed_channel_ids=frozenset(("10",)),
        )
        assert scoped["10"] == full["10"] == expected

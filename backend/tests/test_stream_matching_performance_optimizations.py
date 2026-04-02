from unittest.mock import Mock

from apps.automation.automated_stream_manager import (
    AutomatedStreamManager,
    RegexChannelMatcher,
    _compile_stream_search_regex,
)


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

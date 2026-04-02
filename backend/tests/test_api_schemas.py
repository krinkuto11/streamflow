import pytest

from apps.api.schemas import (
    AutomationProfileCreateSchema,
    AutomationProfileUpdateSchema,
    AutoCreateRuleCreateSchema,
    BulkRegexPatternsSchema,
    ChannelMatchSettingsSchema,
    GroupRegexConfigSchema,
    RegexPatternCreateSchema,
    ScheduledEventCreateSchema,
    SessionSettingsUpdateSchema,
    StreamSessionCreateSchema,
)
from apps.core.exceptions import ValidationError


def test_regex_pattern_create_schema_valid_payload():
    payload = {
        "channel_id": 123,
        "name": "Sports HD",
        "regex": ["sports.*", "sport tv"],
        "enabled": True,
        "m3u_accounts": [1],
    }

    result = RegexPatternCreateSchema.from_payload(payload)

    assert result.channel_id == "123"
    assert result.name == "Sports HD"
    assert result.regex == ["sports.*", "sport tv"]
    assert result.enabled is True
    assert result.m3u_accounts == [1]


def test_regex_pattern_create_schema_requires_fields():
    with pytest.raises(ValidationError) as exc:
        RegexPatternCreateSchema.from_payload({"name": "Missing fields"})

    assert "Missing required fields" in str(exc.value)


def test_channel_match_settings_schema_rejects_invalid_value():
    with pytest.raises(ValidationError) as exc:
        ChannelMatchSettingsSchema.from_payload({"match_by_tvg_id": "not-bool"})

    assert "must be a boolean" in str(exc.value)


def test_group_regex_config_schema_supports_legacy_regex_key():
    result = GroupRegexConfigSchema.from_payload({
        "regex": ["movie.*"],
        "enabled": "true",
        "match_by_tvg_id": "false",
    })

    assert result.regex_patterns == ["movie.*"]
    assert result.enabled is True
    assert result.match_by_tvg_id is False


def test_bulk_regex_patterns_schema_normalizes_channel_ids():
    result = BulkRegexPatternsSchema.from_payload({
        "channel_ids": ["1", 2, "3"],
        "regex_patterns": ["pattern"],
    })

    assert result.channel_ids == [1, 2, 3]
    assert result.regex_patterns == ["pattern"]


def test_bulk_regex_patterns_schema_rejects_empty_lists():
    with pytest.raises(ValidationError) as exc:
        BulkRegexPatternsSchema.from_payload({"channel_ids": [], "regex_patterns": []})

    assert "channel_ids must be a non-empty list" in str(exc.value)


def test_stream_session_create_schema_normalizes_payload():
    parsed = StreamSessionCreateSchema.from_payload(
        {
            "channel_id": 55,
            "pre_event_minutes": "15",
            "stagger_ms": "250",
            "timeout_ms": "45000",
            "auto_created": "true",
            "enable_looping_detection": "false",
            "enable_logo_detection": True,
        }
    )

    assert parsed.channel_id == "55"
    assert parsed.pre_event_minutes == 15
    assert parsed.stagger_ms == 250
    assert parsed.timeout_ms == 45000
    assert parsed.auto_created is True
    assert parsed.enable_looping_detection is False
    assert parsed.enable_logo_detection is True


def test_session_settings_schema_requires_non_negative_values():
    with pytest.raises(ValidationError) as exc:
        SessionSettingsUpdateSchema.from_payload({"review_duration": -1})

    assert "review_duration must be greater than or equal to 0" in str(exc.value)


def test_scheduled_event_create_schema_requires_core_fields():
    with pytest.raises(ValidationError) as exc:
        ScheduledEventCreateSchema.from_payload({"channel_id": 7})

    assert "Missing required fields" in str(exc.value)


def test_auto_create_rule_schema_requires_channel_binding():
    with pytest.raises(ValidationError) as exc:
        AutoCreateRuleCreateSchema.from_payload({"name": "Rule", "regex_pattern": ".*"})

    assert "channel_id or channel_ids" in str(exc.value)


def test_automation_profile_schema_normalizes_remove_dead_streams_flag():
    parsed = AutomationProfileCreateSchema.from_payload(
        {
            "name": "Sports",
            "stream_checking": {
                "enabled": True,
                "remove_dead_streams": "false",
            },
        }
    )

    assert parsed.profile_data["stream_checking"]["remove_dead_streams"] is False


def test_automation_profile_schema_rejects_invalid_remove_dead_streams_flag():
    with pytest.raises(ValidationError) as exc:
        AutomationProfileUpdateSchema.from_payload(
            {
                "stream_checking": {
                    "remove_dead_streams": "maybe",
                },
            }
        )

    assert "stream_checking.remove_dead_streams must be a boolean" in str(exc.value)

from pathlib import Path
from unittest.mock import Mock
from datetime import datetime, timedelta, timezone

from apps.automation import scheduling_service
from apps.automation.scheduling_service import (
    AUTO_CREATE_QUEUE_PRIORITY,
    NoTvgIdError,
    SchedulingService,
)


def _make_service(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    scheduling_service.CONFIG_DIR = tmp_path
    scheduling_service.SCHEDULING_CONFIG_FILE = tmp_path / "scheduling_config.json"
    scheduling_service.SCHEDULED_EVENTS_FILE = tmp_path / "scheduled_events.json"
    scheduling_service.AUTO_CREATE_RULES_FILE = tmp_path / "auto_create_rules.json"
    scheduling_service.EXECUTED_EVENTS_FILE = tmp_path / "executed_events.json"
    return SchedulingService()


def test_auto_create_rule_preview_expands_group_channels(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)

    channels = {
        1: {"id": 1, "name": "No Match Channel", "tvg_id": "no-match"},
        2: {"id": 2, "name": "Match Channel", "tvg_id": "match"},
    }
    udi = Mock()
    udi.get_channel_group_by_id.return_value = {"id": 50, "name": "Event Group"}
    udi.get_channels_by_group.return_value = list(channels.values())
    udi.get_channel_by_id.side_effect = lambda channel_id: channels.get(int(channel_id))
    monkeypatch.setattr(scheduling_service, "get_udi_manager", lambda: udi)

    service._epg_cache = [
        {
            "title": "Regular Show",
            "start_time": (now + timedelta(hours=1)).isoformat(),
            "end_time": (now + timedelta(hours=2)).isoformat(),
            "tvg_id": "no-match",
        },
        {
            "title": "World Cup Friendly",
            "start_time": (now + timedelta(hours=1)).isoformat(),
            "end_time": (now + timedelta(hours=3)).isoformat(),
            "tvg_id": "match",
        },
    ]

    result = service.test_regex_against_epg_for_rule(
        channel_group_ids=[50],
        regex_pattern="Friendly",
    )

    assert result["matches"] == 1
    assert result["channels_tested"] == 2
    assert result["channels_with_matches"] == 1
    assert result["programs"][0]["channel_id"] == 2
    assert result["programs"][0]["channel_name"] == "Match Channel"


def test_auto_create_rule_preview_reports_why_group_channels_do_not_match(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)

    channels = {
        1: {"id": 1, "name": "No TVG Channel", "tvg_id": None},
        2: {"id": 2, "name": "No EPG Channel", "tvg_id": "no-epg"},
        3: {"id": 3, "name": "Wrong Title Channel", "tvg_id": "wrong-title"},
        4: {"id": 4, "name": "Match Channel", "tvg_id": "match"},
    }
    udi = Mock()
    udi.get_channel_group_by_id.return_value = {"id": 50, "name": "Team Channels"}
    udi.get_channels_by_group.return_value = list(channels.values())
    udi.get_channel_by_id.side_effect = lambda channel_id: channels.get(int(channel_id))
    monkeypatch.setattr(scheduling_service, "get_udi_manager", lambda: udi)

    service._epg_cache = [
        {
            "title": "Pregame Baseball",
            "start_time": (now + timedelta(hours=1)).isoformat(),
            "end_time": (now + timedelta(hours=2)).isoformat(),
            "tvg_id": "wrong-title",
        },
        {
            "title": "Live: Baseball",
            "start_time": (now + timedelta(hours=1)).isoformat(),
            "end_time": (now + timedelta(hours=2)).isoformat(),
            "tvg_id": "match",
        },
    ]

    result = service.test_regex_against_epg_for_rule(
        channel_group_ids=[50],
        regex_pattern="^Live:",
    )

    assert result["matches"] == 1
    assert result["channels_tested"] == 4
    assert result["channels_with_matches"] == 1
    assert [channel["id"] for channel in result["channels_without_tvg"]] == []
    assert [channel["id"] for channel in result["channels_without_programs"]] == [1, 2]
    assert result["channels_without_matches"][0]["id"] == 3
    assert result["channels_without_matches"][0]["sample_titles"] == ["Pregame Baseball"]


def test_auto_create_preview_reports_due_and_unscheduled_matches(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)

    channels = {
        10: {"id": 10, "name": "Due Team", "tvg_id": "due"},
        11: {"id": 11, "name": "Future Team", "tvg_id": "future"},
        12: {"id": 12, "name": "Ended Team", "tvg_id": "ended"},
        13: {"id": 13, "name": "Missing Time Team", "tvg_id": "missing"},
    }
    udi = Mock()
    udi.get_channel_group_by_id.return_value = {"id": 50, "name": "Team Channels"}
    udi.get_channels_by_group.return_value = list(channels.values())
    udi.get_channel_by_id.side_effect = lambda channel_id: channels.get(int(channel_id))
    monkeypatch.setattr(scheduling_service, "get_udi_manager", lambda: udi)

    service._epg_cache = [
        {
            "title": "Live: Baseball",
            "start_time": (now + timedelta(minutes=5)).isoformat(),
            "end_time": (now + timedelta(hours=2)).isoformat(),
            "tvg_id": "due",
        },
        {
            "title": "Live: Baseball",
            "start_time": (now + timedelta(hours=2)).isoformat(),
            "end_time": (now + timedelta(hours=4)).isoformat(),
            "tvg_id": "future",
        },
        {
            "title": "Live: Baseball",
            "start_time": (now - timedelta(hours=3)).isoformat(),
            "end_time": (now - timedelta(hours=1)).isoformat(),
            "tvg_id": "ended",
        },
        {
            "title": "Live: Baseball",
            "tvg_id": "missing",
        },
    ]

    result = service.test_regex_against_epg_for_rule(
        channel_group_ids=[50],
        regex_pattern="^Live:",
        minutes_before=10,
    )

    assert result["total_epg_matches"] == 4
    assert result["matches"] == 2
    assert result["due_now_matches"] == 1
    assert result["future_matches"] == 1
    assert result["ended_matches"] == 1
    assert result["missing_time_matches"] == 1
    assert {program["schedule_state"] for program in result["programs"]} == {"due_now", "future"}
    assert [channel["id"] for channel in result["channels_with_unscheduled_matches"]] == [12, 13]


def test_auto_create_preview_guardrail_blocks_massqueue(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)

    channels = {
        10: {"id": 10, "name": "Team A", "tvg_id": "team-a"},
        11: {"id": 11, "name": "Team B", "tvg_id": "team-b"},
        12: {"id": 12, "name": "Team C", "tvg_id": "team-c"},
    }
    udi = Mock()
    udi.get_channel_group_by_id.return_value = {"id": 50, "name": "Team Channels"}
    udi.get_channels_by_group.return_value = list(channels.values())
    udi.get_channel_by_id.side_effect = lambda channel_id: channels.get(int(channel_id))
    monkeypatch.setattr(scheduling_service, "get_udi_manager", lambda: udi)

    service._epg_cache = [
        {
            "title": "Live: Baseball",
            "start_time": (now + timedelta(minutes=5)).isoformat(),
            "end_time": (now + timedelta(hours=2)).isoformat(),
            "tvg_id": tvg_id,
        }
        for tvg_id in ["team-a", "team-b", "team-c"]
    ]

    result = service.test_regex_against_epg_for_rule(
        channel_group_ids=[50],
        regex_pattern="^Live:",
        minutes_before=10,
        max_events_per_run=2,
    )

    assert result["total_epg_matches"] == 3
    assert result["matches"] == 0
    assert result["guardrail"]["blocked"] is True
    assert result["guardrail"]["reason"] == "max_events_per_rule_run"
    assert result["guardrail"]["limit"] == 2
    assert result["guardrail_blocked_matches"] == 3
    assert len(result["guardrail_blocked_programs"]) == 3
    assert result["programs"] == []


def test_group_only_auto_create_rule_does_not_persist_group_channels_as_individuals(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)

    channels = {
        10: {"id": 10, "name": "Team A", "tvg_id": "team-a"},
        11: {"id": 11, "name": "Team B", "tvg_id": "team-b"},
    }
    udi = Mock()
    udi.get_channel_group_by_id.return_value = {"id": 50, "name": "MLB Teams"}
    udi.get_channels_by_group.return_value = list(channels.values())
    udi.get_channel_by_id.side_effect = lambda channel_id: channels.get(int(channel_id))
    monkeypatch.setattr(scheduling_service, "get_udi_manager", lambda: udi)

    rule = service.create_auto_create_rule({
        "name": "Live team checks",
        "channel_group_ids": [50],
        "regex_pattern": "^Live:",
        "minutes_before": 0,
    })

    assert rule["channel_ids"] == []
    assert rule["channel_group_ids"] == [50]
    assert rule["channel_groups_info"][0]["channel_count"] == 2
    assert len(rule["channels_info"]) == 2


def test_legacy_group_expanded_rule_returns_group_channels_as_groups_only(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)

    channels = {
        10: {"id": 10, "name": "Team A", "tvg_id": "team-a"},
        11: {"id": 11, "name": "Team B", "tvg_id": "team-b"},
    }
    udi = Mock()
    udi.get_channel_group_by_id.return_value = {"id": 50, "name": "MLB Teams"}
    udi.get_channels_by_group.return_value = list(channels.values())
    udi.get_channel_by_id.side_effect = lambda channel_id: channels.get(int(channel_id))
    monkeypatch.setattr(scheduling_service, "get_udi_manager", lambda: udi)

    service._auto_create_rules = [{
        "id": "legacy",
        "name": "Legacy group rule",
        "channel_ids": [10, 11],
        "channel_group_ids": [50],
        "channels_info": list(channels.values()),
        "regex_pattern": "^Live:",
        "minutes_before": 0,
    }]

    [rule] = service.get_auto_create_rules()

    assert rule["channel_ids"] == []
    assert rule["channel_group_ids"] == [50]
    assert len(rule["channels_info"]) == 2


def test_auto_create_schedules_currently_airing_programs(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)

    channels = {
        10: {"id": 10, "name": "Team A", "tvg_id": "team-a"},
        11: {"id": 11, "name": "Team B", "tvg_id": "team-b"},
        12: {"id": 12, "name": "Team C", "tvg_id": "team-c"},
    }
    udi = Mock()
    udi.get_channel_group_by_id.return_value = {"id": 50, "name": "Team Channels"}
    udi.get_channels_by_group.return_value = list(channels.values())
    udi.get_channel_by_id.side_effect = lambda channel_id: channels.get(int(channel_id))
    monkeypatch.setattr(scheduling_service, "get_udi_manager", lambda: udi)

    service._epg_cache = [
        {
            "title": "Live: Baseball",
            "start_time": (now - timedelta(minutes=5)).isoformat(),
            "end_time": (now + timedelta(hours=2)).isoformat(),
            "tvg_id": tvg_id,
        }
        for tvg_id in ["team-a", "team-b", "team-c"]
    ]

    rule = service.create_auto_create_rule({
        "name": "Live team checks",
        "channel_group_ids": [50],
        "regex_pattern": "^Live:",
        "minutes_before": 0,
    })

    assert rule["channel_ids"] == []
    events = service.get_scheduled_events()
    assert len(events) == 3
    assert {event["channel_id"] for event in events} == {10, 11, 12}


def test_auto_create_match_guardrail_blocks_massqueue_events(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)

    channels = {
        10: {"id": 10, "name": "Team A", "tvg_id": "team-a"},
        11: {"id": 11, "name": "Team B", "tvg_id": "team-b"},
        12: {"id": 12, "name": "Team C", "tvg_id": "team-c"},
    }
    udi = Mock()
    udi.get_channel_group_by_id.return_value = {"id": 50, "name": "Team Channels"}
    udi.get_channels_by_group.return_value = list(channels.values())
    udi.get_channel_by_id.side_effect = lambda channel_id: channels.get(int(channel_id))
    monkeypatch.setattr(scheduling_service, "get_udi_manager", lambda: udi)

    service._auto_create_rules = [{
        "id": "rule-wide",
        "name": "Wide live rule",
        "channel_group_ids": [50],
        "channel_ids": [],
        "regex_pattern": "^Live:",
        "minutes_before": 0,
        "max_events_per_run": 2,
    }]
    service._epg_cache = [
        {
            "title": "Live: Baseball",
            "start_time": (now - timedelta(minutes=5)).isoformat(),
            "end_time": (now + timedelta(hours=2)).isoformat(),
            "tvg_id": tvg_id,
        }
        for tvg_id in ["team-a", "team-b", "team-c"]
    ]

    result = service.match_programs_to_rules()

    assert result["matched"] == 3
    assert result["created"] == 0
    assert result["guardrail_blocked_matches"] == 3
    assert result["guardrail_blocked_rules"][0]["rule_id"] == "rule-wide"
    assert service.get_scheduled_events() == []


def test_auto_create_schedules_programs_with_guide_time_aliases(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)

    channels = {
        10: {
            "id": 10,
            "name": "San Francisco Giants",
            "tvg_id": "giants",
            "uuid": "channel-uuid-10",
        },
    }
    udi = Mock()
    udi.get_channel_group_by_id.return_value = {"id": 50, "name": "MLB Teams"}
    udi.get_channels_by_group.return_value = list(channels.values())
    udi.get_channel_by_id.side_effect = lambda channel_id: channels.get(int(channel_id))
    monkeypatch.setattr(scheduling_service, "get_udi_manager", lambda: udi)

    service._auto_create_rules = [{
        "id": "rule-1",
        "name": "Live team checks",
        "channel_group_ids": [50],
        "channel_ids": [],
        "regex_pattern": "^Live:",
        "minutes_before": 15,
    }]
    service._epg_cache = [{
        "sub_title": "Live: MLB",
        "start": (now + timedelta(hours=1)).isoformat(),
        "stop": (now + timedelta(hours=4)).isoformat(),
        "channel_uuid": "channel-uuid-10",
    }]

    result = service.match_programs_to_rules()

    events = service.get_scheduled_events()
    assert result["created"] == 1
    assert result["future_matches"] == 1
    assert len(events) == 1
    assert events[0]["channel_id"] == 10
    assert events[0]["program_title"] == "Live: MLB"
    assert events[0]["program_start_time"] == service._epg_cache[0]["start"]
    assert events[0]["program_end_time"] == service._epg_cache[0]["stop"]


def test_auto_create_scheduled_check_queues_with_event_priority(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    event = {
        "id": "event-1",
        "channel_id": 10,
        "channel_name": "Team A",
        "program_title": "Live: MLB",
        "program_start_time": (now + timedelta(minutes=5)).isoformat(),
        "program_end_time": (now + timedelta(hours=2)).isoformat(),
        "check_time": now.isoformat(),
        "minutes_before": 5,
        "schedule_type": "check",
        "auto_created": True,
        "auto_create_rule_id": "rule-1",
    }
    service._scheduled_events = [event]

    class Checker:
        def __init__(self):
            self.queued = []

        def queue_channel(self, *args, **kwargs):
            self.queued.append((args, kwargs))
            return True

        def check_single_channel(self, *args, **kwargs):
            raise AssertionError("auto-create checks should be queued")

    checker = Checker()

    assert service.execute_scheduled_check("event-1", checker) is True
    assert service.get_scheduled_events() == []
    assert len(checker.queued) == 1

    args, kwargs = checker.queued[0]
    assert args == (10,)
    assert kwargs["priority"] == AUTO_CREATE_QUEUE_PRIORITY
    assert kwargs["force_check"] is True
    assert kwargs["metadata"]["source"] == "auto_create"
    assert kwargs["metadata"]["program_name"] == "Live: MLB"
    assert kwargs["metadata"]["is_epg_scheduled"] is True
    assert kwargs["metadata"]["auto_create_rule_id"] == "rule-1"

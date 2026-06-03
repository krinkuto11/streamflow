from pathlib import Path
from unittest.mock import Mock

from apps.automation import scheduling_service
from apps.automation.scheduling_service import SchedulingService


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

    channels = {
        1: {"id": 1, "name": "No Match Channel", "tvg_id": "no-match"},
        2: {"id": 2, "name": "Match Channel", "tvg_id": "match"},
    }
    udi = Mock()
    udi.get_channel_group_by_id.return_value = {"id": 50, "name": "Event Group"}
    udi.get_channels_by_group.return_value = list(channels.values())
    udi.get_channel_by_id.side_effect = lambda channel_id: channels.get(int(channel_id))
    monkeypatch.setattr(scheduling_service, "get_udi_manager", lambda: udi)

    programs_by_channel = {
        1: [{"title": "Regular Show", "start_time": "2026-06-03T18:00:00Z"}],
        2: [{"title": "World Cup Friendly", "start_time": "2026-06-03T19:00:00Z"}],
    }
    monkeypatch.setattr(
        service,
        "get_programs_by_channel",
        lambda channel_id: programs_by_channel[int(channel_id)],
    )

    result = service.test_regex_against_epg_for_rule(
        channel_group_ids=[50],
        regex_pattern="Friendly",
    )

    assert result["matches"] == 1
    assert result["channels_tested"] == 2
    assert result["channels_with_matches"] == 1
    assert result["programs"][0]["channel_id"] == 2
    assert result["programs"][0]["channel_name"] == "Match Channel"

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit
from unittest.mock import Mock

import pytest
from flask import Flask

from apps.automation import epg_snapshot, scheduling_service
from apps.automation.epg_snapshot import EpgSnapshotIncompleteError, fetch_program_window
from apps.automation.scheduling_service import SchedulingService
from apps.api.scheduling_handlers import get_epg_refresh_processor_status_response

NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
BASE = "http://dispatcharr.test"


def test_window_search_keeps_current_long_programs_and_follows_all_pages():
    calls = []
    def fetch(url):
        calls.append(url)
        parsed = urlsplit(url)
        params = parse_qs(parsed.query)
        assert parsed.path == "/api/epg/programs/search/"
        assert datetime.fromisoformat(params["end_after"][0]) == NOW
        assert datetime.fromisoformat(params["start_before"][0]) == NOW + timedelta(hours=48)
        assert params["page_size"] == ["500"]
        if "page" not in params:
            return {"results": [{"id": 1, "start_time": (NOW - timedelta(hours=5)).isoformat(),
                                 "end_time": (NOW + timedelta(hours=1)).isoformat()}],
                    "next": "?page=2"}
        return {"results": [{"id": 2}], "next": None}
    result = fetch_program_window(BASE, fetch, now=NOW, hours_ahead=48)
    assert result.complete
    assert result.pages == len(calls) == 2
    assert [p["id"] for p in result.programs] == [1, 2]


@pytest.mark.parametrize("next_link,limit,reason", [
    ("?page=2", 1, "page_limit"),
    ("?page=1", 5, "pagination_loop"),
    ("http://foreign.test/api/epg/programs/search/", 5, "unsafe_next_link"),
    ("/api/epg/programs/", 5, "unsafe_next_link"),
])
def test_incomplete_pagination_is_explicit(next_link, limit, reason):
    fetch = Mock(return_value={"results": [{"id": 1}], "next": next_link})
    result = fetch_program_window(BASE, fetch, now=NOW, max_pages=limit)
    assert not result.complete
    assert result.reason == reason
    assert fetch.call_count <= 2


def test_exact_page_limit_is_complete_when_last_page_has_no_next():
    result = fetch_program_window(BASE, lambda _url: {"results": [], "next": None}, now=NOW, max_pages=1)
    assert result.complete


@pytest.mark.parametrize("data", [None, {}, {"results": "invalid"}, {"results": [None]}, {"results": [], "next": 7}])
def test_unreadable_page_is_never_a_complete_snapshot(data):
    assert not fetch_program_window(BASE, lambda _url: data, now=NOW).complete


def make_service(monkeypatch):
    service = SchedulingService()
    monkeypatch.setattr(service, "_get_base_url", lambda: BASE)
    monkeypatch.setattr(scheduling_service, "fetch_data_from_url", lambda _url: None)
    return service


def test_partial_snapshot_does_not_replace_or_extend_complete_cache(monkeypatch):
    service = make_service(monkeypatch)
    previous = {"time": datetime.now() - timedelta(hours=2), "by_tvg_id": {"test": [{"id": 9}]}}
    service._epg_all_programs_cache = previous
    assert service._fetch_and_cache_all_programs(force_refresh=True) == previous["by_tvg_id"]
    assert service._epg_all_programs_cache is previous
    assert service.get_epg_snapshot_status()["using_previous_snapshot"] is True
    assert not service.get_epg_snapshot_status()["complete"]


def test_first_partial_snapshot_blocks_auto_create_and_is_reported(monkeypatch):
    service = make_service(monkeypatch)
    with pytest.raises(EpgSnapshotIncompleteError):
        service._fetch_and_cache_all_programs(force_refresh=True)
    assert service._epg_all_programs_cache is None
    with Flask(__name__).app_context():
        response, code = get_epg_refresh_processor_status_response(
            epg_refresh_thread=None, epg_refresh_running=False, get_scheduling_service=lambda: service,
        )
        assert code == 200
        assert response.get_json()["snapshot"]["complete"] is False


def test_search_channel_mappings_are_available_for_epg_identity():
    service = SchedulingService()
    ids = service._program_epg_identifiers({"channels": [{"id": 17, "uuid": "channel-17"}]})
    assert "dispatcharr_channel:17" in ids
    assert "dispatcharr_channel_uuid:channel-17" in ids


def test_guide_timestamp_rounding_does_not_duplicate_search_programme():
    service = SchedulingService()
    rows, seen = [], set()
    program = {"id": 521, "tvg_id": "test", "title": "Live event",
               "start_time": "2026-10-24T12:00:00.123456Z", "end_time": "2026-10-24T13:00:00.123456Z"}
    rounded = dict(program, start_time="2026-10-24T12:00:00.123Z", end_time="2026-10-24T13:00:00.123Z")
    service._merge_program_sources(rows, seen, [program], source="programs")
    service._merge_program_sources(rows, seen, [rounded], source="grid")
    assert len(rows) == 1
    assert rows[0]["start_time"] == program["start_time"]

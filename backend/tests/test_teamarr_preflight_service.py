import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.stream.teamarr_preflight_service import TeamarrPreflightService, normalize_config


FIXED_NOW = 1780005600.0


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeChecker:
    def __init__(self):
        self.calls = []

    def get_status(self):
        return {"stream_checking_mode": False, "queue": {"queue_size": 0, "in_progress": 0}}

    def check_single_channel(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {"success": True, "stats": {"total_streams": 2}}


class FakeUdi:
    def __init__(self, streams=None):
        self.streams = streams if streams is not None else [{"id": 1}, {"id": 2}]

    def get_channel_streams(self, channel_id):
        return self.streams


def make_event(**overrides):
    event = {
        "id": 100,
        "event_id": "event-100",
        "event_name": "Home vs Away",
        "channel_name": "Managed Event",
        "dispatcharr_channel_id": 77,
        "dispatcharr_uuid": "uuid-77",
        "sport": "soccer",
        "league": "uefa.europa",
        "sync_status": "in_sync",
        "event_date": "2026-05-28T22:10:00+00:00",
    }
    event.update(overrides)
    return event


class TeamarrPreflightServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_file = Path(self.temp_dir.name) / "teamarr_preflight_config.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_service(self, events, *, checker=None, udi=None):
        checker = checker or FakeChecker()
        udi = udi or FakeUdi()
        http_get = Mock(return_value=FakeResponse(events))
        service = TeamarrPreflightService(
            config_file=self.config_file,
            http_get=http_get,
            udi_provider=lambda: udi,
            stream_checker_provider=lambda: checker,
            clock=lambda: FIXED_NOW,
        )
        service.update_config({
            "teamarr_base_url": "http://teamarr.test",
            "api_key": "secret",
            "api_key_header": "X-Teamarr-Key",
            "preflight_offset_minutes": 20,
            "retry_offsets_minutes": [10, 3],
        })
        return service, checker, http_get

    def test_config_redacts_api_key_and_normalizes_filters(self):
        config = normalize_config({
            "api_key": "secret",
            "retry_offsets_minutes": ["3", "10", "bad", "10"],
            "include_sports": [" Soccer ", ""],
            "exclude_leagues": "mlb",
        })

        self.assertEqual(config["retry_offsets_minutes"], [10, 3])
        self.assertEqual(config["include_sports"], ["soccer"])
        self.assertEqual(config["exclude_leagues"], ["mlb"])

        service, _, _ = self.make_service([])
        public_config = service.get_config()
        self.assertTrue(public_config["has_api_key"])
        self.assertEqual(public_config["api_key"], "")

    def test_due_event_launches_single_channel_check_with_epg_context(self):
        checker = FakeChecker()
        service, _, http_get = self.make_service([make_event()], checker=checker)

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["events_seen"], 1)
        self.assertEqual(result["launched"], 1)

        deadline = time.time() + 2
        while time.time() < deadline and not checker.calls:
            time.sleep(0.01)

        self.assertEqual(len(checker.calls), 1)
        args, kwargs = checker.calls[0]
        self.assertEqual(args[0], 77)
        self.assertEqual(kwargs["program_name"], "Home vs Away")
        self.assertTrue(kwargs["is_epg_scheduled"])
        http_get.assert_called_once()
        called_url = http_get.call_args[0][0]
        self.assertEqual(called_url, "http://teamarr.test/api/v1/channels/managed")
        self.assertEqual(http_get.call_args.kwargs["headers"]["X-Teamarr-Key"], "secret")

    def test_no_streams_records_no_streams_without_launching_check(self):
        checker = FakeChecker()
        service, _, _ = self.make_service([make_event()], checker=checker, udi=FakeUdi(streams=[]))

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 0)
        self.assertEqual(checker.calls, [])

        recent = service.get_status()["recent_events"]
        self.assertEqual(recent[0]["type"], "no_streams_yet")

    def test_include_filters_keep_non_matching_sports_out_of_due_set(self):
        service, _, _ = self.make_service([make_event(sport="basketball")])
        service.update_config({"include_sports": ["soccer"]})

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 0)

        upcoming = service.get_status()["upcoming_events"]
        self.assertEqual(upcoming[0]["state"], "filtered")


if __name__ == "__main__":
    unittest.main()

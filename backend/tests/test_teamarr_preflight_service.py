import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.stream.teamarr_preflight_service import (
    ATTEMPT_STATE_KEY,
    DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME,
    DEFAULT_TEAMARR_PREFLIGHT_VISIBILITY_POLICY,
    TEAMARR_PREFLIGHT_QUEUE_PRIORITY,
    TeamarrPreflightService,
    normalize_config,
)


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


class FakeDb:
    def __init__(self):
        self.settings = {}

    def get_system_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_system_setting(self, key, value):
        self.settings[key] = value
        return True


class RouteHttpGet:
    def __init__(self, routes):
        self.routes = dict(routes)
        self.calls = []

    def __call__(self, url, *args, **kwargs):
        path = str(url).replace("http://teamarr.test", "")
        self.calls.append(path)
        payload = self.routes.get(path, [])
        if isinstance(payload, Exception):
            raise payload
        return FakeResponse(payload)


class SlowTeamStatusHttpGet:
    def __init__(self, teams, *, delay=0.05, block_event=None):
        self.teams = [dict(team) for team in teams]
        self.delay = delay
        self.block_event = block_event
        self.entered = threading.Event()
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.timeouts = []

    def __call__(self, url, *args, **kwargs):
        path = str(url).replace("http://teamarr.test", "")
        if path == "/api/v1/teams?active_only=true":
            return FakeResponse(self.teams)
        if path.endswith("/channel-status"):
            with self.lock:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
                self.timeouts.append(kwargs.get("timeout"))
            self.entered.set()
            try:
                if self.block_event is not None:
                    self.block_event.wait(timeout=2)
                else:
                    time.sleep(self.delay)
                team_id = int(path.split("/")[-2])
                team = next(team for team in self.teams if int(team["id"]) == team_id)
                return FakeResponse(make_team_status(team=team))
            finally:
                with self.lock:
                    self.active -= 1
        if path == "/api/v1/sports-subscription":
            return FakeResponse({"leagues": []})
        if path == "/api/v1/cache/sports":
            return FakeResponse({"sports": {}})
        if path == "/api/v1/cache/leagues":
            return FakeResponse({"leagues": []})
        return FakeResponse([])


class FakeChecker:
    def __init__(self):
        self.calls = []
        self.queued = []
        self.gates = []
        self.running = False
        self.start_calls = 0

    def get_status(self):
        return {"stream_checking_mode": False, "queue": {"queue_size": 0, "in_progress": 0}}

    def start(self):
        self.start_calls += 1
        self.running = True

    def queue_channel(self, *args, **kwargs):
        self.queued.append((args, kwargs))
        return True

    def check_single_channel(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return {
            "success": True,
            "stats": {
                "total_streams": 2,
                "duration_seconds": 12,
                "stream_details": [
                    {"stream_name": "Private Stream", "m3u_account": "Private Provider"},
                ],
            },
        }

    def set_specialized_queue_gate(self, gate_name, active):
        self.gates.append((gate_name, active))


class SequencedChecker(FakeChecker):
    def __init__(self, results):
        super().__init__()
        self.results = list(results)

    def check_single_channel(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.results:
            return self.results.pop(0)
        return super().check_single_channel(*args, **kwargs)


class BusyChecker(FakeChecker):
    def __init__(self):
        super().__init__()
        self.running = True

    def get_status(self):
        return {"stream_checking_mode": True, "queue": {"queue_size": 0, "in_progress": 1}}


class QueueBackedChecker(FakeChecker):
    def __init__(self):
        super().__init__()
        self.check_queue = type("FakeQueue", (), {})()
        self.check_queue.queued_priorities = {77: TEAMARR_PREFLIGHT_QUEUE_PRIORITY}
        self.check_queue.queued_metadata = {
            77: {
                "source": "teamarr_preflight",
                "attempted_key": "id:queued:pre",
                "program_name": "Queued Match",
                "trigger_bucket": "pre",
                "event": {
                    "identity": "id:queued",
                    "event_name": "Queued Match",
                    "event_date": "2026-05-28T22:10:00+00:00",
                    "channel_name": "Queued Channel",
                    "dispatcharr_channel_id": 77,
                    "sport": "soccer",
                    "league": "fifa.friendly",
                    "seconds_to_start": 600,
                    "trigger_bucket": "pre",
                },
            }
        }
        self.check_queue.in_progress_metadata = {
            78: {
                "source": "teamarr_preflight",
                "attempted_key": "id:running:pre",
                "program_name": "Running Match",
                "event": {
                    "identity": "id:running",
                    "event_name": "Running Match",
                    "event_date": "2026-05-28T22:20:00+00:00",
                    "channel_name": "Running Channel",
                    "dispatcharr_channel_id": 78,
                },
            }
        }


class FakeUdi:
    def __init__(self, streams=None, streams_after_channel_refresh=None):
        self.streams = streams if streams is not None else [{"id": 1}, {"id": 2}]
        self.streams_after_channel_refresh = streams_after_channel_refresh
        self.refresh_channel_calls = []

    def get_channel_streams(self, channel_id):
        return self.streams

    def refresh_channel_by_id(self, channel_id):
        self.refresh_channel_calls.append(channel_id)
        if self.streams_after_channel_refresh is not None:
            self.streams = self.streams_after_channel_refresh
            return True
        return False


class FakeAutomationConfig:
    def __init__(self, profiles=None, next_id="42"):
        self.profiles = [dict(profile) for profile in (profiles or [])]
        self.next_id = str(next_id)
        self.created_profiles = []
        self.updated_profiles = []

    def get_all_profiles(self, *args, **kwargs):
        return [dict(profile) for profile in self.profiles]

    def get_profile(self, profile_id):
        profile_id = str(profile_id)
        for profile in self.profiles:
            if str(profile.get("id")) == profile_id:
                return dict(profile)
        return None

    def create_profile(self, profile_data):
        self.created_profiles.append(dict(profile_data))
        profile = dict(profile_data)
        profile["id"] = self.next_id
        self.profiles.append(profile)
        return self.next_id

    def update_profile(self, profile_id, profile_data):
        self.updated_profiles.append((str(profile_id), dict(profile_data)))
        for profile in self.profiles:
            if str(profile.get("id")) == str(profile_id):
                profile.update(dict(profile_data))
                return True
        return False


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


def make_team_status(**overrides):
    status = {
        "team": {
            "id": 501,
            "provider": "espn",
            "provider_team_id": "20",
            "primary_league": "mlb",
            "leagues": ["mlb"],
            "sport": "baseball",
            "team_name": "Static Test Team",
            "team_abbrev": "STT",
            "channel_id": "StaticTestTeam.mlb",
            "active": True,
        },
        "dispatcharr_channel": {
            "found": True,
            "id": 77,
            "uuid": "uuid-77",
            "name": "Static Team Channel",
            "tvg_id": "StaticTestTeam.mlb",
            "stream_count": 2,
            "streams": [1, 2],
        },
        "next_live_window": {
            "found": True,
            "start": "2026-05-28T22:20:00+00:00",
            "stop": "2026-05-29T01:20:00+00:00",
            "title": "Live: MLB",
            "sub_title": "Static Test Team at Fixture Opponent",
            "is_live": True,
            "source": "team_epg_xmltv",
        },
        "status": "ready",
        "missing": [],
        "xmltv_updated_at": "2026-05-28T21:00:00+00:00",
    }
    status.update(overrides)
    return status


class TeamarrPreflightServiceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_file = Path(self.temp_dir.name) / "teamarr_preflight_config.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_service(
        self,
        events,
        *,
        checker=None,
        udi=None,
        automation_config=None,
        automation_status=None,
        http_get=None,
        db=None,
        service_options=None,
    ):
        checker = checker or FakeChecker()
        udi = udi or FakeUdi()
        automation_config = automation_config or FakeAutomationConfig()
        automation_status = automation_status or {}
        http_get = http_get or Mock(return_value=FakeResponse(events))
        db = db or FakeDb()
        service = TeamarrPreflightService(
            config_file=self.config_file,
            http_get=http_get,
            udi_provider=lambda: udi,
            stream_checker_provider=lambda: checker,
            automation_config_provider=lambda: automation_config,
            automation_status_provider=lambda: automation_status,
            db_provider=lambda: db,
            clock=lambda: FIXED_NOW,
            **(service_options or {}),
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
            "post_start_offsets_minutes": ["2", "1", "bad", "2"],
            "include_sports": [" Soccer ", ""],
            "exclude_leagues": "mlb",
        })

        self.assertEqual(config["retry_offsets_minutes"], [10, 3])
        self.assertEqual(config["post_start_offsets_minutes"], [1, 2])
        self.assertEqual(config["include_sports"], ["soccer"])
        self.assertEqual(config["exclude_leagues"], ["mlb"])
        self.assertEqual(normalize_config({})["post_start_offsets_minutes"], [2, 4])
        self.assertEqual(normalize_config({"retry_offsets_minutes": "3"})["retry_offsets_minutes"], [3])
        self.assertEqual(normalize_config({"post_start_offsets_minutes": 2})["post_start_offsets_minutes"], [2])
        self.assertTrue(normalize_config({})["queue_during_active_checks"])
        self.assertFalse(normalize_config({"skip_during_quality_check": True})["queue_during_active_checks"])
        self.assertFalse(normalize_config({"defer_during_active_checks": True})["queue_during_active_checks"])
        self.assertTrue(normalize_config({"defer_during_active_checks": False})["queue_during_active_checks"])
        self.assertTrue(normalize_config({"queue_during_active_checks": True})["queue_during_active_checks"])

        service, _, _ = self.make_service([])
        public_config = service.get_config()
        self.assertTrue(public_config["has_api_key"])
        self.assertEqual(public_config["api_key"], "")
        self.assertTrue(public_config["queue_during_active_checks"])
        self.assertFalse(public_config["defer_during_active_checks"])
        self.assertFalse(public_config["skip_during_quality_check"])
        self.assertTrue(public_config["managed_event_preflight_enabled"])
        self.assertFalse(public_config["static_team_preflight_enabled"])

    def test_static_team_source_default_off_does_not_call_teams_api(self):
        http_get = RouteHttpGet({
            "/api/v1/channels/managed": [make_event()],
            "/api/v1/sports-subscription": {"leagues": []},
            "/api/v1/cache/sports": {"sports": {}},
            "/api/v1/cache/leagues": {"leagues": []},
        })
        service, _, _ = self.make_service([], http_get=http_get)

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["teams_seen"], 0)
        self.assertNotIn("/api/v1/teams?active_only=true", http_get.calls)
        status = service.get_status()
        self.assertEqual(status["upcoming_teams"], [])
        self.assertFalse(status["team_status"]["enabled"])

    def test_static_team_source_enabled_fetches_team_channel_status(self):
        team_status = make_team_status()
        http_get = RouteHttpGet({
            "/api/v1/teams?active_only=true": [team_status["team"]],
            "/api/v1/teams/501/channel-status": team_status,
            "/api/v1/sports-subscription": {"leagues": []},
            "/api/v1/cache/sports": {"sports": {}},
            "/api/v1/cache/leagues": {"leagues": []},
        })
        service, _, _ = self.make_service([], http_get=http_get)
        service.update_config({
            "managed_event_preflight_enabled": False,
            "static_team_preflight_enabled": True,
        })

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["events_seen"], 0)
        self.assertEqual(result["teams_seen"], 1)
        self.assertIn("/api/v1/teams?active_only=true", http_get.calls)
        self.assertIn("/api/v1/teams/501/channel-status", http_get.calls)
        status = service.get_status()
        self.assertEqual(status["team_status"]["ready"], 1)
        self.assertEqual(status["team_status"]["queueable"], 1)
        self.assertEqual(status["upcoming_teams"][0]["preflight_kind"], "team")
        self.assertEqual(status["upcoming_teams"][0]["teamarr_team_id"], 501)
        self.assertEqual(status["upcoming_teams"][0]["state"], "due")
        self.assertEqual(status["preflight_items"][0]["identity"], status["upcoming_teams"][0]["identity"])

    def test_static_team_status_requests_use_bounded_parallelism(self):
        base_team = make_team_status()["team"]
        teams = [
            {
                **base_team,
                "id": 600 + index,
                "provider_team_id": str(600 + index),
                "team_name": f"Parallel Team {index}",
                "channel_id": f"ParallelTeam{index}.mlb",
            }
            for index in range(8)
        ]
        http_get = SlowTeamStatusHttpGet(teams, delay=0.05)
        service, _, _ = self.make_service(
            [],
            http_get=http_get,
            service_options={
                "static_team_scan_workers": 4,
                "static_team_request_timeout_seconds": 0.5,
                "static_team_scan_budget_seconds": 2,
            },
        )
        service.update_config({
            "managed_event_preflight_enabled": False,
            "static_team_preflight_enabled": True,
        })

        started = time.monotonic()
        result = service.run_once(force=True)
        elapsed = time.monotonic() - started

        self.assertTrue(result["success"])
        self.assertFalse(result["partial"])
        self.assertEqual(result["teams_seen"], 8)
        self.assertEqual(http_get.max_active, 4)
        self.assertLess(elapsed, 0.35)
        self.assertTrue(all(timeout <= 0.5 for timeout in http_get.timeouts))
        scan_status = service.get_status()["scan_status"]
        self.assertEqual(scan_status["completed"], 8)
        self.assertEqual(scan_status["pending_requests"], 0)

    def test_static_team_scan_budget_returns_visible_partial_result(self):
        base_team = make_team_status()["team"]
        teams = [
            {
                **base_team,
                "id": 700 + index,
                "provider_team_id": str(700 + index),
                "team_name": f"Budget Team {index}",
            }
            for index in range(6)
        ]
        http_get = SlowTeamStatusHttpGet(teams, delay=0.2)
        service, _, _ = self.make_service(
            [],
            http_get=http_get,
            service_options={
                "static_team_scan_workers": 2,
                "static_team_request_timeout_seconds": 0.05,
                "static_team_scan_budget_seconds": 0.1,
            },
        )
        service.update_config({
            "managed_event_preflight_enabled": False,
            "static_team_preflight_enabled": True,
        })

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertTrue(result["partial"])
        self.assertTrue(result["degraded"])
        scan_status = service.get_status()["scan_status"]
        self.assertEqual(scan_status["state"], "degraded")
        self.assertTrue(scan_status["budget_exhausted"])
        self.assertEqual(scan_status["requested"], 6)
        self.assertGreater(scan_status["cancelled"], 0)
        self.assertTrue(service._static_requests_finished_event.wait(timeout=1))

    def test_stop_reports_stopping_until_blocked_team_request_finishes(self):
        release = threading.Event()
        team = make_team_status()["team"]
        http_get = SlowTeamStatusHttpGet([team], block_event=release)
        service, checker, _ = self.make_service(
            [],
            http_get=http_get,
            service_options={
                "static_team_scan_workers": 1,
                "static_team_request_timeout_seconds": 1,
                "static_team_scan_budget_seconds": 2,
                "stop_wait_seconds": 0.05,
            },
        )
        service.update_config({
            "managed_event_preflight_enabled": False,
            "static_team_preflight_enabled": True,
        })

        service.start(persist=True)
        self.assertTrue(http_get.entered.wait(timeout=1))
        stopped = service.stop(persist=False)

        self.assertFalse(stopped)
        status = service.get_status()
        self.assertTrue(status["stopping"])
        self.assertEqual(status["scan_status"]["state"], "stopping")
        self.assertGreater(status["scan_status"]["pending_requests"], 0)
        self.assertEqual(checker.calls, [])
        self.assertFalse(service.start(persist=False))

        release.set()
        self.assertTrue(service._static_requests_finished_event.wait(timeout=1))
        self.assertTrue(service._scan_finished_event.wait(timeout=1))
        deadline = time.time() + 1
        while time.time() < deadline and service.get_status()["running"]:
            time.sleep(0.01)
        final_status = service.get_status()
        self.assertFalse(final_status["running"])
        self.assertFalse(final_status["stopping"])
        self.assertEqual(final_status["scan_status"]["state"], "cancelled")
        self.assertEqual(checker.calls, [])

    def test_cancelled_manual_scan_does_not_report_success(self):
        release = threading.Event()
        http_get = SlowTeamStatusHttpGet([make_team_status()["team"]], block_event=release)
        service, checker, _ = self.make_service(
            [],
            http_get=http_get,
            service_options={
                "static_team_scan_workers": 1,
                "static_team_request_timeout_seconds": 1,
                "static_team_scan_budget_seconds": 2,
                "stop_wait_seconds": 0.05,
            },
        )
        service.update_config({
            "managed_event_preflight_enabled": False,
            "static_team_preflight_enabled": True,
        })
        result = {}

        scan_thread = threading.Thread(
            target=lambda: result.update(service.run_once(force=True)),
            daemon=True,
        )
        scan_thread.start()
        self.assertTrue(http_get.entered.wait(timeout=1))
        self.assertFalse(service.stop(persist=False))
        release.set()
        scan_thread.join(timeout=1)

        self.assertFalse(scan_thread.is_alive())
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "scan_cancelled")
        self.assertTrue(result["partial"])
        self.assertEqual(checker.calls, [])

    def test_incomplete_static_team_is_visible_but_not_queueable(self):
        incomplete = make_team_status(
            dispatcharr_channel={"found": True, "id": 77, "stream_count": 0, "streams": []},
            status="incomplete",
            missing=["next_live_window"],
            next_live_window={"found": False, "is_live": False, "source": "team_epg_xmltv"},
        )
        http_get = RouteHttpGet({
            "/api/v1/teams?active_only=true": [incomplete["team"]],
            "/api/v1/teams/501/channel-status": incomplete,
            "/api/v1/sports-subscription": {"leagues": []},
            "/api/v1/cache/sports": {"sports": {}},
            "/api/v1/cache/leagues": {"leagues": []},
        })
        checker = FakeChecker()
        service, _, _ = self.make_service([], checker=checker, http_get=http_get)
        service.update_config({
            "managed_event_preflight_enabled": False,
            "static_team_preflight_enabled": True,
        })

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 0)
        self.assertEqual(checker.calls, [])
        team = service.get_status()["upcoming_teams"][0]
        self.assertEqual(team["state"], "no_streams_yet")
        self.assertEqual(service.get_status()["team_status"]["queueable"], 0)

    def test_due_static_team_queues_single_channel_check_with_team_metadata(self):
        team_status = make_team_status()
        http_get = RouteHttpGet({
            "/api/v1/teams?active_only=true": [team_status["team"]],
            "/api/v1/teams/501/channel-status": team_status,
            "/api/v1/sports-subscription": {"leagues": []},
            "/api/v1/cache/sports": {"sports": {}},
            "/api/v1/cache/leagues": {"leagues": []},
        })
        checker = BusyChecker()
        service, _, _ = self.make_service([], checker=checker, http_get=http_get)
        service.update_config({
            "managed_event_preflight_enabled": False,
            "static_team_preflight_enabled": True,
        })

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 1)
        self.assertEqual(len(checker.queued), 1)
        args, kwargs = checker.queued[0]
        self.assertEqual(args[0], 77)
        self.assertTrue(kwargs["force_check"])
        metadata = kwargs["metadata"]
        self.assertEqual(metadata["source"], "teamarr_preflight")
        self.assertEqual(metadata["preflight_kind"], "team")
        self.assertEqual(metadata["forced_profile_id"], "42")
        self.assertEqual(metadata["quality_profile_id"], "42")
        self.assertEqual(metadata["quality_profile_name"], DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME)
        self.assertEqual(metadata["event"]["preflight_kind"], "team")
        self.assertEqual(metadata["event"]["teamarr_team_id"], 501)
        self.assertEqual(metadata["event"]["quality_profile_id"], "42")
        self.assertEqual(metadata["event"]["quality_profile_name"], DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME)
        self.assertFalse(metadata["event"]["may_start_full_run"])

        service.run_once(force=True)
        self.assertEqual(len(checker.queued), 1)

    def test_static_team_placeholder_window_is_visible_but_not_queueable(self):
        base_status = make_team_status()
        placeholder = make_team_status(
            team={
                **base_status["team"],
                "primary_league": "nfl",
                "leagues": ["nfl"],
                "sport": "football",
                "team_name": "Arizona Cardinals",
                "team_abbrev": "ARI",
                "channel_id": "ArizonaCardinals.nfl",
            },
            dispatcharr_channel={
                **base_status["dispatcharr_channel"],
                "id": 645,
                "name": "Arizona Cardinals",
                "tvg_id": "ArizonaCardinals.nfl",
                "stream_count": 1,
                "streams": [645001],
            },
            next_live_window={
                "found": True,
                "start": "2026-05-28T22:03:00+00:00",
                "stop": "2026-05-29T01:03:00+00:00",
                "title": "ARIZONA CARDINALS",
                "is_live": True,
                "source": "team_epg_xmltv",
            },
            status="ready",
            missing=[],
        )
        http_get = RouteHttpGet({
            "/api/v1/teams?active_only=true": [placeholder["team"]],
            "/api/v1/teams/501/channel-status": placeholder,
            "/api/v1/sports-subscription": {"leagues": []},
            "/api/v1/cache/sports": {"sports": {}},
            "/api/v1/cache/leagues": {"leagues": []},
        })
        checker = FakeChecker()
        service, _, _ = self.make_service([], checker=checker, http_get=http_get)
        service.update_config({
            "managed_event_preflight_enabled": False,
            "static_team_preflight_enabled": True,
        })

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 0)
        self.assertEqual(checker.calls, [])
        team = service.get_status()["upcoming_teams"][0]
        self.assertEqual(team["state"], "no_event_window")
        self.assertFalse(team["live_window_event_evidence"])
        self.assertIn("team_event_evidence", team["missing"])
        self.assertEqual(service.get_status()["team_status"]["ready"], 1)
        self.assertEqual(service.get_status()["team_status"]["queueable"], 0)

    def test_static_team_upcoming_matchup_preview_window_is_not_queueable(self):
        promo = make_team_status(
            next_live_window={
                "found": True,
                "start": "2026-05-28T22:03:00+00:00",
                "stop": "2026-05-29T01:03:00+00:00",
                "title": "Coming up Tonight: Static Test Team @ Fixture Opponent",
                "is_live": False,
                "source": "team_epg_xmltv",
            },
            status="ready",
            missing=[],
        )
        http_get = RouteHttpGet({
            "/api/v1/teams?active_only=true": [promo["team"]],
            "/api/v1/teams/501/channel-status": promo,
            "/api/v1/sports-subscription": {"leagues": []},
            "/api/v1/cache/sports": {"sports": {}},
            "/api/v1/cache/leagues": {"leagues": []},
        })
        checker = FakeChecker()
        service, _, _ = self.make_service([], checker=checker, http_get=http_get)
        service.update_config({
            "managed_event_preflight_enabled": False,
            "static_team_preflight_enabled": True,
        })

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 0)
        self.assertEqual(checker.calls, [])
        team = service.get_status()["upcoming_teams"][0]
        self.assertEqual(team["state"], "no_event_window")
        self.assertFalse(team["live_window_event_evidence"])
        self.assertIn("team_event_evidence", team["missing"])
        self.assertEqual(service.get_status()["team_status"]["queueable"], 0)

    def test_static_team_generic_upcoming_window_is_visible_but_not_queueable(self):
        promo = make_team_status(
            next_live_window={
                "found": True,
                "start": "2026-05-28T22:03:00+00:00",
                "stop": "2026-05-29T01:03:00+00:00",
                "title": "Coming up Tonight",
                "is_live": False,
                "source": "team_epg_xmltv",
            },
            status="ready",
            missing=[],
        )
        http_get = RouteHttpGet({
            "/api/v1/teams?active_only=true": [promo["team"]],
            "/api/v1/teams/501/channel-status": promo,
            "/api/v1/sports-subscription": {"leagues": []},
            "/api/v1/cache/sports": {"sports": {}},
            "/api/v1/cache/leagues": {"leagues": []},
        })
        checker = FakeChecker()
        service, _, _ = self.make_service([], checker=checker, http_get=http_get)
        service.update_config({
            "managed_event_preflight_enabled": False,
            "static_team_preflight_enabled": True,
        })

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 0)
        self.assertEqual(checker.calls, [])
        team = service.get_status()["upcoming_teams"][0]
        self.assertEqual(team["state"], "no_event_window")
        self.assertFalse(team["live_window_event_evidence"])

    def test_static_team_queue_uses_selected_preflight_profile(self):
        team_status = make_team_status()
        http_get = RouteHttpGet({
            "/api/v1/teams?active_only=true": [team_status["team"]],
            "/api/v1/teams/501/channel-status": team_status,
            "/api/v1/sports-subscription": {"leagues": []},
            "/api/v1/cache/sports": {"sports": {}},
            "/api/v1/cache/leagues": {"leagues": []},
        })
        checker = BusyChecker()
        automation_config = FakeAutomationConfig(
            profiles=[
                {"id": "7", "name": "Custom Team Preflight"},
                {"id": "8", "name": DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME},
            ]
        )
        service, _, _ = self.make_service(
            [],
            checker=checker,
            automation_config=automation_config,
            http_get=http_get,
        )
        service.update_config({
            "managed_event_preflight_enabled": False,
            "static_team_preflight_enabled": True,
            "forced_profile_id": "7",
        })

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 1)
        self.assertEqual(len(checker.queued), 1)
        _, kwargs = checker.queued[0]
        metadata = kwargs["metadata"]
        self.assertEqual(metadata["preflight_kind"], "team")
        self.assertEqual(metadata["forced_profile_id"], "7")
        self.assertEqual(metadata["quality_profile_id"], "7")
        self.assertEqual(metadata["quality_profile_name"], "Custom Team Preflight")
        self.assertEqual(metadata["event"]["preflight_kind"], "team")
        self.assertEqual(metadata["event"]["quality_profile_id"], "7")
        self.assertEqual(metadata["event"]["quality_profile_name"], "Custom Team Preflight")
        checker.check_queue = type("FakeQueue", (), {})()
        checker.check_queue.queued_priorities = {77: TEAMARR_PREFLIGHT_QUEUE_PRIORITY}
        checker.check_queue.queued_metadata = {77: metadata}
        checker.check_queue.in_progress_metadata = {}
        queued = service.get_status()["queued_checks"]
        self.assertEqual(queued[0]["preflight_kind"], "team")
        self.assertEqual(queued[0]["forced_profile_id"], "7")
        self.assertEqual(queued[0]["quality_profile_id"], "7")
        self.assertEqual(queued[0]["quality_profile_name"], "Custom Team Preflight")

    def test_manual_force_static_team_identity_runs_single_channel_check(self):
        team_status = make_team_status(
            next_live_window={
                "found": True,
                "start": "2026-05-28T23:00:00+00:00",
                "stop": "2026-05-29T02:00:00+00:00",
                "title": "Live: MLB",
                "sub_title": "Static Test Team at Fixture Opponent",
                "is_live": True,
                "source": "team_epg_xmltv",
            },
        )
        http_get = RouteHttpGet({
            "/api/v1/teams?active_only=true": [team_status["team"]],
            "/api/v1/teams/501/channel-status": team_status,
            "/api/v1/sports-subscription": {"leagues": []},
            "/api/v1/cache/sports": {"sports": {}},
            "/api/v1/cache/leagues": {"leagues": []},
        })
        checker = FakeChecker()
        service, _, _ = self.make_service([], checker=checker, http_get=http_get)
        service.update_config({
            "managed_event_preflight_enabled": False,
            "static_team_preflight_enabled": True,
        })
        service.run_once(force=True)
        identity = service.get_status()["upcoming_teams"][0]["identity"]

        result = service.force_check_event(identity)

        self.assertTrue(result["success"])
        self.assertTrue(result["launched"])
        deadline = time.time() + 2
        while time.time() < deadline and not checker.calls:
            time.sleep(0.01)
        args, kwargs = checker.calls[0]
        self.assertEqual(args[0], 77)
        self.assertEqual(kwargs["program_name"], "Static Test Team")
        self.assertTrue(kwargs["is_epg_scheduled"])
        self.assertEqual(kwargs["forced_profile_id"], "42")

    def test_static_team_endpoint_error_degrades_without_blocking_events(self):
        http_get = RouteHttpGet({
            "/api/v1/channels/managed": [make_event()],
            "/api/v1/teams?active_only=true": RuntimeError("HTTP 500"),
            "/api/v1/sports-subscription": {"leagues": []},
            "/api/v1/cache/sports": {"sports": {}},
            "/api/v1/cache/leagues": {"leagues": []},
        })
        checker = FakeChecker()
        service, _, _ = self.make_service([], checker=checker, http_get=http_get)
        service.update_config({"static_team_preflight_enabled": True})

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["events_seen"], 1)
        self.assertEqual(result["team_error"], "Teamarr static team endpoint did not complete the last scan")
        deadline = time.time() + 2
        while time.time() < deadline and not checker.calls:
            time.sleep(0.01)
        self.assertEqual(len(checker.calls), 1)
        status = service.get_status()
        self.assertEqual(status["team_status"]["last_error"], "Teamarr static team endpoint did not complete the last scan")
        self.assertEqual(status["upcoming_teams"], [])

    def test_default_profile_is_created_and_selected_for_preflight(self):
        automation_config = FakeAutomationConfig()
        service, _, _ = self.make_service([], automation_config=automation_config)

        config = service.get_config()
        self.assertEqual(config["forced_profile_id"], "42")
        self.assertEqual(config["default_profile_id"], "42")
        self.assertEqual(config["default_profile_name"], DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME)
        self.assertTrue(config["default_profile_available"])
        self.assertEqual(len(automation_config.created_profiles), 1)
        created_profile = automation_config.created_profiles[0]
        self.assertEqual(created_profile["name"], DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME)
        self.assertFalse(created_profile["m3u_update"]["enabled"])
        self.assertFalse(created_profile["stream_matching"]["enabled"])
        self.assertTrue(created_profile["stream_checking"]["enabled"])
        self.assertFalse(created_profile["stream_checking"]["remove_dead_streams"])
        self.assertEqual(
            created_profile["channel_visibility_automation"],
            DEFAULT_TEAMARR_PREFLIGHT_VISIBILITY_POLICY,
        )

    def test_existing_default_profile_is_updated_with_visibility_policy(self):
        automation_config = FakeAutomationConfig(
            profiles=[{"id": "42", "name": DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME}]
        )
        service, _, _ = self.make_service([], automation_config=automation_config)

        config = service.get_config()

        self.assertEqual(config["default_profile_id"], "42")
        self.assertEqual(len(automation_config.created_profiles), 0)
        self.assertEqual(len(automation_config.updated_profiles), 1)
        profile_id, payload = automation_config.updated_profiles[0]
        self.assertEqual(profile_id, "42")
        self.assertEqual(
            payload["channel_visibility_automation"],
            DEFAULT_TEAMARR_PREFLIGHT_VISIBILITY_POLICY,
        )

    def test_existing_forced_profile_is_preserved(self):
        automation_config = FakeAutomationConfig(
            profiles=[
                {"id": "7", "name": "Custom Event Profile"},
                {"id": "8", "name": DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME},
            ]
        )
        service, _, _ = self.make_service([], automation_config=automation_config)
        service.update_config({"forced_profile_id": "7"})

        config = service.get_config()
        self.assertEqual(config["forced_profile_id"], "7")
        self.assertEqual(config["default_profile_id"], "8")
        self.assertEqual(automation_config.created_profiles, [])

    def test_due_event_launches_single_channel_check_with_epg_context(self):
        checker = FakeChecker()
        service, _, http_get = self.make_service([make_event()], checker=checker)

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["events_seen"], 1)
        self.assertEqual(result["launched"], 1)
        upcoming = service.get_status()["upcoming_events"]
        self.assertEqual(upcoming[0]["match_evidence"]["source"], "teamarr_managed_event")
        self.assertFalse(upcoming[0]["match_evidence"]["may_start_full_run"])
        self.assertFalse(upcoming[0]["may_start_full_run"])

        deadline = time.time() + 2
        while time.time() < deadline and not checker.calls:
            time.sleep(0.01)

        self.assertEqual(len(checker.calls), 1)
        args, kwargs = checker.calls[0]
        self.assertEqual(args[0], 77)
        self.assertEqual(kwargs["program_name"], "Home vs Away")
        self.assertTrue(kwargs["is_epg_scheduled"])
        self.assertEqual(kwargs["forced_profile_id"], "42")
        managed_call = http_get.call_args_list[0]
        self.assertEqual(managed_call[0][0], "http://teamarr.test/api/v1/channels/managed")
        self.assertEqual(managed_call.kwargs["headers"]["X-Teamarr-Key"], "secret")

        deadline = time.time() + 2
        recent = []
        while time.time() < deadline:
            recent = service.get_status()["recent_events"]
            if recent and recent[0]["type"] == "preflight_completed":
                break
            time.sleep(0.01)
        self.assertEqual(recent[0]["type"], "preflight_completed")
        self.assertFalse(recent[0]["may_start_full_run"])
        self.assertFalse(recent[0]["details"]["may_start_full_run"])
        self.assertEqual(recent[0]["details"]["match_evidence"]["source"], "teamarr_managed_event")
        self.assertFalse(recent[0]["details"]["match_evidence"]["may_start_full_run"])
        public_stats = recent[0]["details"]["stats"]
        self.assertEqual(public_stats["total_streams"], 2)
        self.assertEqual(public_stats["duration_seconds"], 12)
        self.assertNotIn("stream_details", public_stats)
        self.assertEqual(recent[0]["details"]["quality_profile_id"], "42")
        self.assertEqual(recent[0]["details"]["quality_profile_name"], DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME)

    def test_status_keeps_large_managed_event_lists_visible(self):
        events = [
            make_event(
                id=1000 + index,
                event_id=f"event-{index}",
                dispatcharr_channel_id=7700 + index,
                event_date=f"2030-01-01T{index % 24:02d}:00:00+00:00",
            )
            for index in range(75)
        ]
        service, _, _ = self.make_service(events)

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        status = service.get_status()
        self.assertEqual(status["managed_events_seen"], 75)
        self.assertEqual(status["managed_candidates"], 75)
        self.assertEqual(status["managed_events_returned"], 75)
        self.assertFalse(status["managed_events_truncated"])
        self.assertEqual(len(status["upcoming_events"]), 75)
        self.assertEqual(status["teamarr_connector"]["state"], "connected")
        self.assertFalse(status["teamarr_connector"]["official_api_key_required"])

    def test_status_exposes_empty_teamarr_connector_state(self):
        service, _, _ = self.make_service([])

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        connector = service.get_status()["teamarr_connector"]
        self.assertEqual(connector["state"], "empty")
        self.assertEqual(connector["endpoint"], "/api/v1/channels/managed")

    def test_status_exposes_teamarr_connector_scan_error(self):
        def failing_http_get(*_args, **_kwargs):
            raise RuntimeError("network unavailable")

        service, _, _ = self.make_service([], http_get=failing_http_get)

        result = service.run_once(force=True)

        self.assertFalse(result["success"])
        connector = service.get_status()["teamarr_connector"]
        self.assertEqual(connector["state"], "error")
        self.assertEqual(connector["last_error"], "Teamarr preflight scan failed")

    def test_managed_event_accepts_alternate_start_time_and_channel_id_fields(self):
        event = make_event(
            event_date=None,
            start_time="2026-05-28T22:10:00+00:00",
            dispatcharr_channel_id=None,
            channel_id=91,
        )
        service, _, _ = self.make_service([event])

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        upcoming = service.get_status()["upcoming_events"]
        self.assertEqual(len(upcoming), 1)
        self.assertEqual(upcoming[0]["dispatcharr_channel_id"], 91)
        self.assertEqual(upcoming[0]["event_date"], "2026-05-28T22:10:00+00:00")

    def test_controlled_guard_skip_defers_preflight_bucket_for_retry(self):
        checker = SequencedChecker([
            {
                "success": True,
                "skipped": True,
                "reason": "active_viewers",
                "details": {"skip_reason": "active_viewers"},
            },
            {
                "success": True,
                "stats": {"total_streams": 1, "duration_seconds": 8},
            },
        ])
        service, _, _ = self.make_service([make_event()], checker=checker)

        first_result = service.run_once(force=True)
        self.assertTrue(first_result["success"])
        self.assertEqual(first_result["launched"], 1)

        deadline = time.time() + 2
        recent = []
        while time.time() < deadline:
            recent = service.get_status()["recent_events"]
            if recent and recent[0]["type"] == "preflight_deferred":
                break
            time.sleep(0.01)

        self.assertEqual(recent[0]["type"], "preflight_deferred")
        self.assertEqual(recent[0]["details"]["reason"], "active_viewers")

        second_result = service.run_once(force=True)
        self.assertTrue(second_result["success"])
        self.assertEqual(second_result["launched"], 1)

        deadline = time.time() + 2
        while time.time() < deadline:
            recent = service.get_status()["recent_events"]
            if recent and recent[0]["type"] == "preflight_completed":
                break
            time.sleep(0.01)

        self.assertEqual(len(checker.calls), 2)
        self.assertEqual(recent[0]["type"], "preflight_completed")
        self.assertEqual(recent[0]["details"]["stats"]["total_streams"], 1)

    def test_retry_offsets_do_not_fire_before_preflight_offset(self):
        checker = FakeChecker()
        service, _, _ = self.make_service(
            [make_event(event_date="2026-05-28T22:05:00+00:00")],
            checker=checker,
        )
        service.update_config({
            "preflight_offset_minutes": 1,
            "retry_offsets_minutes": [10, 3],
        })

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 0)
        self.assertEqual(checker.calls, [])

        upcoming = service.get_status()["upcoming_events"]
        self.assertEqual(upcoming[0]["state"], "scheduled")

    def test_scheduled_event_exposes_next_automatic_check(self):
        checker = FakeChecker()
        service, _, _ = self.make_service(
            [make_event(event_date="2026-05-28T22:45:00+00:00")],
            checker=checker,
        )

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 0)

        upcoming = service.get_status()["upcoming_events"]
        self.assertEqual(upcoming[0]["state"], "scheduled")
        self.assertEqual(upcoming[0]["next_automatic_check"], {
            "label": "Next auto check",
            "bucket": "-20m",
            "timestamp": "2026-05-28T22:25:00+00:00",
        })

    def test_due_bucket_is_limited_to_poll_window(self):
        checker = FakeChecker()
        service, _, _ = self.make_service(
            [make_event(event_date="2026-05-28T22:09:00+00:00")],
            checker=checker,
        )
        service.update_config({"poll_interval_seconds": 30})

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 0)
        self.assertEqual(checker.calls, [])
        upcoming = service.get_status()["upcoming_events"]
        self.assertEqual(upcoming[0]["state"], "scheduled")

    def test_pre_start_bucket_fires_inside_poll_window(self):
        checker = FakeChecker()
        service, _, _ = self.make_service(
            [make_event(event_date="2026-05-28T22:09:50+00:00")],
            checker=checker,
        )
        service.update_config({"poll_interval_seconds": 30})

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 1)

    def test_managed_events_sort_current_and_upcoming_before_past(self):
        checker = FakeChecker()
        events = [
            make_event(id=1, event_id="past", event_name="Past Match", event_date="2026-05-28T19:00:00+00:00"),
            make_event(id=2, event_id="future", event_name="Future Match", event_date="2026-05-28T22:45:00+00:00"),
            make_event(id=3, event_id="due", event_name="Due Match", event_date="2026-05-28T22:10:00+00:00"),
        ]
        service, _, _ = self.make_service(events, checker=checker)

        result = service.run_once(force=True)
        self.assertTrue(result["success"])

        upcoming = service.get_status()["upcoming_events"]
        self.assertEqual([event["event_name"] for event in upcoming], [
            "Due Match",
            "Future Match",
            "Past Match",
        ])

    def test_post_start_offset_launches_after_game_start(self):
        checker = FakeChecker()
        service, _, _ = self.make_service(
            [make_event(event_date="2026-05-28T21:58:00+00:00")],
            checker=checker,
        )
        service.update_config({"post_start_offsets_minutes": [2]})

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 1)

        deadline = time.time() + 2
        while time.time() < deadline and not checker.calls:
            time.sleep(0.01)

        self.assertEqual(len(checker.calls), 1)
        recent = service.get_status()["recent_events"]
        self.assertEqual(recent[0]["details"]["bucket"], "post+2m")

    def test_post_start_grace_must_cover_post_start_offset(self):
        checker = FakeChecker()
        service, _, _ = self.make_service(
            [make_event(event_date="2026-05-28T21:58:00+00:00")],
            checker=checker,
        )
        service.update_config({
            "post_start_offsets_minutes": [2],
            "post_start_grace_minutes": 1,
        })

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 0)
        self.assertEqual(checker.calls, [])

        upcoming = service.get_status()["upcoming_events"]
        self.assertEqual(upcoming[0]["state"], "past")

    def test_preflight_attempt_does_not_block_post_start_bucket(self):
        checker = FakeChecker()
        event = make_event(event_date="2026-05-28T21:58:00+00:00")
        service, _, _ = self.make_service([event], checker=checker)
        identity = "id:100:2026-05-28T21:58:00+00:00"
        service._attempted_buckets[f"{identity}:20m"] = FIXED_NOW

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 1)

        deadline = time.time() + 2
        while time.time() < deadline and not checker.calls:
            time.sleep(0.01)

        self.assertEqual(len(checker.calls), 1)
        recent = service.get_status()["recent_events"]
        self.assertEqual(recent[0]["details"]["bucket"], "post+2m")

    def test_managed_event_attempt_survives_service_restart(self):
        db = FakeDb()
        first_checker = FakeChecker()
        first, _, _ = self.make_service([make_event()], checker=first_checker, db=db)

        first_result = first.run_once(force=True)
        self.assertTrue(first_result["success"])
        self.assertEqual(first_result["launched"], 1)

        deadline = time.time() + 2
        while time.time() < deadline and not first_checker.calls:
            time.sleep(0.01)
        self.assertEqual(len(first_checker.calls), 1)
        self.assertIn(ATTEMPT_STATE_KEY, db.settings)

        second_checker = FakeChecker()
        second, _, _ = self.make_service([make_event()], checker=second_checker, db=db)
        second_result = second.run_once(force=True)

        self.assertTrue(second_result["success"])
        self.assertEqual(second_result["launched"], 0)
        self.assertEqual(second_checker.calls, [])
        self.assertEqual(second.get_status()["upcoming_events"][0]["state"], "already_attempted")

    def test_static_team_attempt_survives_service_restart(self):
        db = FakeDb()
        team_status = make_team_status()
        routes = {
            "/api/v1/teams?active_only=true": [team_status["team"]],
            "/api/v1/teams/501/channel-status": team_status,
            "/api/v1/sports-subscription": {"leagues": []},
            "/api/v1/cache/sports": {"sports": {}},
            "/api/v1/cache/leagues": {"leagues": []},
        }
        first_checker = FakeChecker()
        first, _, _ = self.make_service(
            [], checker=first_checker, db=db, http_get=RouteHttpGet(routes)
        )
        first.update_config({
            "managed_event_preflight_enabled": False,
            "static_team_preflight_enabled": True,
        })

        first_result = first.run_once(force=True)
        self.assertTrue(first_result["success"])
        self.assertEqual(first_result["launched"], 1)

        deadline = time.time() + 2
        while time.time() < deadline and not first_checker.calls:
            time.sleep(0.01)
        self.assertEqual(len(first_checker.calls), 1)

        second_checker = FakeChecker()
        second, _, _ = self.make_service(
            [], checker=second_checker, db=db, http_get=RouteHttpGet(routes)
        )
        second.update_config({
            "managed_event_preflight_enabled": False,
            "static_team_preflight_enabled": True,
        })
        second_result = second.run_once(force=True)

        self.assertTrue(second_result["success"])
        self.assertEqual(second_result["launched"], 0)
        self.assertEqual(second_checker.calls, [])
        self.assertEqual(second.get_status()["upcoming_teams"][0]["state"], "already_attempted")

    def test_queue_state_reconciles_missing_persisted_attempt_on_start(self):
        db = FakeDb()
        checker = QueueBackedChecker()
        service, _, _ = self.make_service([], checker=checker, db=db)

        service.start(persist=False)
        service.stop(persist=False)

        attempts = db.settings[ATTEMPT_STATE_KEY]["attempts"]
        self.assertIn("id:queued:pre", attempts)
        self.assertIn("id:running:pre", attempts)

    def test_second_post_start_bucket_runs_after_first_post_start_attempt(self):
        checker = FakeChecker()
        event = make_event(event_date="2026-05-28T21:56:00+00:00")
        service, _, _ = self.make_service([event], checker=checker)
        identity = "id:100:2026-05-28T21:56:00+00:00"
        service._attempted_buckets[f"{identity}:post+2m"] = FIXED_NOW

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 1)

        deadline = time.time() + 2
        while time.time() < deadline and not checker.calls:
            time.sleep(0.01)

        self.assertEqual(len(checker.calls), 1)
        recent = service.get_status()["recent_events"]
        self.assertEqual(recent[0]["details"]["bucket"], "post+4m")

    def test_missed_configured_post_start_bucket_catches_up_inside_grace(self):
        checker = FakeChecker()
        event = make_event(event_date="2026-05-28T21:55:00+00:00")
        service, _, _ = self.make_service([event], checker=checker)
        identity = "id:100:2026-05-28T21:55:00+00:00"
        service._attempted_buckets[f"{identity}:post+2m"] = FIXED_NOW

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 1)

        deadline = time.time() + 2
        while time.time() < deadline and not checker.calls:
            time.sleep(0.01)

        self.assertEqual(len(checker.calls), 1)
        recent = service.get_status()["recent_events"]
        self.assertEqual(recent[0]["details"]["bucket"], "post+4m")

    def test_post_start_catchup_does_not_invent_unconfigured_four_minute_bucket(self):
        checker = FakeChecker()
        event = make_event(event_date="2026-05-28T21:55:00+00:00")
        service, _, _ = self.make_service([event], checker=checker)
        service.update_config({"post_start_offsets_minutes": [2]})
        identity = "id:100:2026-05-28T21:55:00+00:00"
        service._attempted_buckets[f"{identity}:post+2m"] = FIXED_NOW

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 0)
        self.assertEqual(checker.calls, [])
        upcoming = service.get_status()["upcoming_events"]
        self.assertEqual(upcoming[0]["state"], "already_attempted")
        self.assertEqual(upcoming[0]["trigger_bucket"], "post+2m")

    def test_no_streams_records_no_streams_without_launching_check(self):
        checker = FakeChecker()
        service, _, _ = self.make_service([make_event()], checker=checker, udi=FakeUdi(streams=[]))

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 0)
        self.assertEqual(checker.calls, [])

        recent = service.get_status()["recent_events"]
        self.assertEqual(recent[0]["type"], "no_streams_yet")

    def test_no_streams_refreshes_channel_before_marking_bucket_attempted(self):
        checker = FakeChecker()
        udi = FakeUdi(streams=[], streams_after_channel_refresh=[{"id": 99}])
        service, _, _ = self.make_service([make_event()], checker=checker, udi=udi)

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 1)

        deadline = time.time() + 2
        while time.time() < deadline and not checker.calls:
            time.sleep(0.01)

        self.assertEqual(udi.refresh_channel_calls, [77])
        self.assertEqual(len(checker.calls), 1)
        recent = service.get_status()["recent_events"]
        self.assertEqual(recent[0]["type"], "preflight_completed")

    def test_active_automation_run_defers_preflight_without_marking_attempted(self):
        checker = FakeChecker()
        service, _, _ = self.make_service(
            [make_event()],
            checker=checker,
            automation_status={"active": True, "stage": "stream_matching"},
        )
        service.update_config({"queue_during_active_checks": False})

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(checker.calls, [])

        recent = service.get_status()["recent_events"]
        self.assertEqual(recent[0]["type"], "deferred_automation_active")
        upcoming = service.get_status()["upcoming_events"]
        self.assertEqual(upcoming[0]["state"], "due")

    def test_wrapped_public_automation_status_defers_preflight(self):
        checker = FakeChecker()
        service, _, _ = self.make_service(
            [make_event()],
            checker=checker,
            automation_status={
                "running": True,
                "thread_alive": True,
                "run_status": {"active": True, "state": "running", "stage": "quality_checking"},
            },
        )
        service.update_config({"queue_during_active_checks": False})

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 0)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(checker.calls, [])
        self.assertEqual(checker.queued, [])
        self.assertEqual(service.get_status()["recent_events"][0]["type"], "deferred_automation_active")

    def test_active_automation_run_queues_preflight_when_queue_setting_enabled(self):
        checker = FakeChecker()
        service, _, _ = self.make_service(
            [make_event()],
            checker=checker,
            automation_status={
                "running": True,
                "thread_alive": True,
                "run_status": {"active": True, "state": "running", "stage": "stream_matching"},
            },
        )
        service.update_config({"queue_during_active_checks": True})

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 1)
        self.assertEqual(checker.calls, [])
        self.assertEqual(len(checker.queued), 1)
        self.assertEqual(checker.queued[0][1]["metadata"]["source"], "teamarr_preflight")
        self.assertEqual(service.get_status()["recent_events"][0]["type"], "preflight_queued")
        self.assertIn(("teamarr_preflight_automation", True), checker.gates)
        self.assertEqual(checker.start_calls, 0)

    def test_automation_queue_gate_clears_when_no_run_active(self):
        checker = FakeChecker()
        automation_status = {
            "running": True,
            "thread_alive": True,
            "run_status": {"active": True, "state": "running", "stage": "stream_matching"},
        }
        service, _, _ = self.make_service(
            [make_event()],
            checker=checker,
            automation_status=automation_status,
        )

        service.run_once(force=True)
        automation_status["run_status"] = {"active": False, "state": "idle"}
        service.run_once(force=True)

        self.assertIn(("teamarr_preflight_automation", True), checker.gates)
        self.assertIn(("teamarr_preflight_automation", False), checker.gates)

    def test_scheduler_running_without_active_run_does_not_defer_preflight(self):
        checker = FakeChecker()
        service, _, _ = self.make_service(
            [make_event()],
            checker=checker,
            automation_status={
                "running": True,
                "thread_alive": True,
                "run_status": {"active": False, "state": "idle"},
            },
        )

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 1)
        self.assertEqual(result["skipped"], 0)

    def test_active_stream_checker_queues_teamarr_event_with_preflight_context(self):
        checker = BusyChecker()
        service, _, _ = self.make_service([make_event()], checker=checker)

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 1)
        self.assertEqual(checker.calls, [])
        self.assertEqual(len(checker.queued), 1)

        args, kwargs = checker.queued[0]
        self.assertEqual(args[0], 77)
        self.assertEqual(kwargs["priority"], TEAMARR_PREFLIGHT_QUEUE_PRIORITY)
        self.assertTrue(kwargs["force_check"])
        self.assertEqual(kwargs["metadata"]["source"], "teamarr_preflight")
        self.assertEqual(kwargs["metadata"]["program_name"], "Home vs Away")
        self.assertTrue(kwargs["metadata"]["is_epg_scheduled"])
        self.assertFalse(kwargs["metadata"]["may_start_full_run"])
        self.assertEqual(kwargs["metadata"]["match_evidence"]["source"], "teamarr_managed_event")
        self.assertFalse(kwargs["metadata"]["match_evidence"]["may_start_full_run"])
        self.assertEqual(kwargs["metadata"]["forced_profile_id"], "42")
        self.assertEqual(kwargs["metadata"]["quality_profile_id"], "42")
        self.assertEqual(kwargs["metadata"]["quality_profile_name"], DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME)
        self.assertEqual(kwargs["metadata"]["event"]["identity"], "id:100:2026-05-28T22:10:00+00:00")
        self.assertEqual(kwargs["metadata"]["event"]["event_name"], "Home vs Away")
        self.assertEqual(kwargs["metadata"]["event"]["quality_profile_id"], "42")
        self.assertEqual(kwargs["metadata"]["event"]["quality_profile_name"], DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME)
        self.assertFalse(kwargs["metadata"]["event"]["may_start_full_run"])

        recent = service.get_status()["recent_events"]
        self.assertEqual(recent[0]["type"], "preflight_queued")
        self.assertEqual(recent[0]["details"]["priority"], TEAMARR_PREFLIGHT_QUEUE_PRIORITY)
        self.assertEqual(recent[0]["details"]["match_evidence"]["source"], "teamarr_managed_event")
        self.assertEqual(recent[0]["details"]["quality_profile_id"], "42")
        self.assertEqual(recent[0]["details"]["quality_profile_name"], DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME)

        service.record_queued_check_result(
            kwargs["metadata"],
            {"success": True, "stats": {"total_streams": 2, "duration_seconds": 11}},
        )
        recent = service.get_status()["recent_events"]
        self.assertEqual(recent[0]["type"], "preflight_completed")
        self.assertEqual(recent[0]["event_name"], "Home vs Away")
        self.assertEqual(recent[0]["details"]["stats"]["total_streams"], 2)
        self.assertNotIn("priority", recent[0]["details"])
        self.assertFalse(recent[0]["details"]["may_start_full_run"])
        self.assertEqual(recent[0]["details"]["quality_profile_id"], "42")
        self.assertEqual(recent[0]["details"]["quality_profile_name"], DEFAULT_TEAMARR_PREFLIGHT_PROFILE_NAME)

    def test_duplicate_due_events_are_deduped_before_queueing(self):
        checker = BusyChecker()
        service, _, _ = self.make_service([make_event(), make_event()], checker=checker)

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(len(checker.queued), 1)
        recent = service.get_status()["recent_events"]
        self.assertEqual(recent[0]["type"], "duplicate_preflight_skipped")
        self.assertEqual(recent[1]["type"], "preflight_queued")
        self.assertFalse(recent[0]["details"]["may_start_full_run"])
        self.assertEqual(recent[0]["details"]["match_evidence"]["source"], "teamarr_managed_event")

    def test_status_exposes_teamarr_stream_checker_queue(self):
        checker = QueueBackedChecker()
        service, _, _ = self.make_service([], checker=checker)

        status = service.get_status()

        self.assertEqual(status["queued_checks_count"], 1)
        self.assertEqual(status["queued_checks"][0]["event_name"], "Queued Match")
        self.assertEqual(status["queued_checks"][0]["priority"], TEAMARR_PREFLIGHT_QUEUE_PRIORITY)
        self.assertEqual(status["queue_active_checks_count"], 1)
        self.assertEqual(status["queue_active_checks"][0]["event_name"], "Running Match")

    def test_direct_check_gates_specialized_queue_until_finished(self):
        checker = FakeChecker()
        service, _, _ = self.make_service([make_event()], checker=checker)

        result = service.run_once(force=True)
        self.assertTrue(result["success"])

        deadline = time.time() + 2
        while time.time() < deadline and len(checker.gates) < 2:
            time.sleep(0.01)

        self.assertIn(("teamarr_preflight_direct", True), checker.gates)
        self.assertEqual(checker.gates[-1], ("teamarr_preflight_direct", False))

    def test_direct_capacity_limit_queues_due_teamarr_events_instead_of_hiding_them(self):
        checker = FakeChecker()
        service, _, _ = self.make_service([make_event()], checker=checker)
        service._active_checks["busy"] = {
            "identity": "busy",
            "dispatcharr_channel_id": 1,
            "started_at": FIXED_NOW,
        }

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 1)
        self.assertEqual(checker.calls, [])
        self.assertEqual(len(checker.queued), 1)
        args, kwargs = checker.queued[0]
        self.assertEqual(args[0], 77)
        self.assertEqual(kwargs["metadata"]["source"], "teamarr_preflight")
        recent = service.get_status()["recent_events"]
        self.assertEqual(recent[0]["type"], "preflight_queued")
        self.assertEqual(checker.start_calls, 1)
        self.assertTrue(checker.running)

    def test_filter_options_use_teamarr_subscription_and_cache_catalogs(self):
        def http_get(url, **kwargs):
            if url.endswith("/api/v1/channels/managed"):
                return FakeResponse([make_event()])
            if url.endswith("/api/v1/sports-subscription"):
                return FakeResponse({"leagues": ["mlb", "nhl", "uefa.europa", "ufc"]})
            if url.endswith("/api/v1/cache/sports"):
                return FakeResponse({"sports": {
                    "baseball": "Baseball",
                    "hockey": "Hockey",
                    "mma": "MMA",
                    "soccer": "Soccer",
                }})
            if url.endswith("/api/v1/cache/leagues"):
                return FakeResponse({"leagues": [
                    {"slug": "mlb", "name": "Major League Baseball", "sport": "baseball"},
                    {"slug": "nhl", "name": "National Hockey League", "sport": "hockey"},
                    {"slug": "uefa.europa", "name": "UEFA Europa League", "sport": "soccer"},
                    {"slug": "ufc", "name": "UFC", "sport": "mma"},
                ]})
            raise AssertionError(f"Unexpected URL {url}")

        service, _, _ = self.make_service([make_event()], http_get=Mock(side_effect=http_get))

        result = service.run_once(force=True)
        self.assertTrue(result["success"])

        options = service.get_status()["filter_options"]
        self.assertEqual(options["source"], "teamarr_subscription")
        self.assertEqual(
            [item["value"] for item in options["sports"]],
            ["baseball", "hockey", "mma", "soccer"],
        )
        league_labels = {item["value"]: item["label"] for item in options["leagues"]}
        self.assertEqual(league_labels["mlb"], "Major League Baseball")
        self.assertEqual(league_labels["uefa.europa"], "UEFA Europa League")

    def test_filter_options_use_event_sport_when_subscription_league_catalog_is_unknown(self):
        event = make_event(sport="soccer", league="usa.usl.1")

        def http_get(url, **kwargs):
            if url.endswith("/api/v1/channels/managed"):
                return FakeResponse([event])
            if url.endswith("/api/v1/sports-subscription"):
                return FakeResponse({"leagues": ["usa.usl.1", "usa.usl.1.cup", "fifa.cwc", "uefa.europa_qual"]})
            if url.endswith("/api/v1/cache/sports"):
                return FakeResponse({"sports": {"soccer": "Soccer"}})
            if url.endswith("/api/v1/cache/leagues"):
                return FakeResponse({"leagues": [
                    {"slug": "usa.usl.1", "name": "USL League One", "sport": "unknown"},
                    {"slug": "usa.usl.1.cup", "name": "USL Cup", "sport": "unknown"},
                    {"slug": "fifa.cwc", "name": "FIFA Club World Cup", "sport": "unknown"},
                    {"slug": "uefa.europa_qual", "name": "UEFA Europa League Qualifying", "sport": "unknown"},
                ]})
            raise AssertionError(f"Unexpected URL {url}")

        service, _, _ = self.make_service([event], http_get=Mock(side_effect=http_get))

        result = service.run_once(force=True)
        self.assertTrue(result["success"])

        options = service.get_status()["filter_options"]
        self.assertEqual(options["source"], "teamarr_subscription")
        self.assertEqual(options["sports"], [{"value": "soccer", "label": "Soccer"}])
        league_sports = {item["value"]: item["sport"] for item in options["leagues"]}
        self.assertEqual(league_sports["usa.usl.1"], "soccer")
        self.assertEqual(league_sports["usa.usl.1.cup"], "soccer")
        self.assertEqual(league_sports["fifa.cwc"], "soccer")
        self.assertEqual(league_sports["uefa.europa_qual"], "soccer")

    def test_default_automation_status_provider_uses_running_main_module(self):
        class FakeAutomationManager:
            def get_run_status(self):
                return {"active": True, "stage": "stream_matching"}

        main_module = sys.modules["__main__"]
        sentinel = object()
        previous_main_manager = getattr(main_module, "automation_manager", sentinel)
        previous_api_module = sys.modules.pop("apps.api.web_api", None)
        previous_web_module = sys.modules.pop("web_api", None)

        try:
            main_module.automation_manager = FakeAutomationManager()
            status = TeamarrPreflightService._default_automation_status_provider()
        finally:
            if previous_main_manager is sentinel:
                delattr(main_module, "automation_manager")
            else:
                main_module.automation_manager = previous_main_manager
            if previous_api_module is not None:
                sys.modules["apps.api.web_api"] = previous_api_module
            if previous_web_module is not None:
                sys.modules["web_api"] = previous_web_module

        self.assertTrue(status["active"])
        self.assertEqual(status["stage"], "stream_matching")

    def test_default_automation_status_provider_uses_web_api_manager_factory(self):
        class FakeAutomationManager:
            def get_run_status(self):
                return {"active": True, "stage": "quality_checking"}

        class FakeWebApiModule:
            automation_manager = None

            @staticmethod
            def get_automation_manager():
                return FakeAutomationManager()

        sentinel = object()
        previous_api_module = sys.modules.get("apps.api.web_api", sentinel)

        try:
            sys.modules["apps.api.web_api"] = FakeWebApiModule()
            status = TeamarrPreflightService._default_automation_status_provider()
        finally:
            if previous_api_module is sentinel:
                sys.modules.pop("apps.api.web_api", None)
            else:
                sys.modules["apps.api.web_api"] = previous_api_module

        self.assertTrue(status["active"])
        self.assertEqual(status["stage"], "quality_checking")

    def test_default_automation_status_provider_prefers_active_duplicate_module(self):
        class InactiveAutomationManager:
            def get_run_status(self):
                return {"active": False, "stage": "idle"}

        class ActiveAutomationManager:
            def get_run_status(self):
                return {"active": True, "stage": "stream_matching"}

        class InactiveWebApiModule:
            automation_manager = InactiveAutomationManager()

        class ActiveWebApiModule:
            automation_manager = ActiveAutomationManager()

        sentinel = object()
        previous_api_module = sys.modules.get("apps.api.web_api", sentinel)
        previous_web_module = sys.modules.get("web_api", sentinel)

        try:
            sys.modules["apps.api.web_api"] = InactiveWebApiModule()
            sys.modules["web_api"] = ActiveWebApiModule()
            status = TeamarrPreflightService._default_automation_status_provider()
        finally:
            if previous_api_module is sentinel:
                sys.modules.pop("apps.api.web_api", None)
            else:
                sys.modules["apps.api.web_api"] = previous_api_module
            if previous_web_module is sentinel:
                sys.modules.pop("web_api", None)
            else:
                sys.modules["web_api"] = previous_web_module

        self.assertTrue(status["active"])
        self.assertEqual(status["stage"], "stream_matching")

    def test_include_filters_keep_non_matching_sports_out_of_due_set(self):
        service, _, _ = self.make_service([make_event(sport="basketball")])
        service.update_config({"include_sports": ["soccer"]})

        result = service.run_once(force=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 0)

        upcoming = service.get_status()["upcoming_events"]
        self.assertEqual(upcoming[0]["state"], "filtered")

    def test_unknown_managed_event_sport_uses_teamarr_league_catalog(self):
        event = make_event(sport="unknown", league="usa.usl.1")
        http_get = RouteHttpGet({
            "/api/v1/channels/managed": [event],
            "/api/v1/sports-subscription": {"leagues": []},
            "/api/v1/cache/sports": {"sports": {}},
            "/api/v1/cache/leagues": {
                "leagues": [
                    {"slug": "usa.usl.1", "sport": "soccer", "name": "USL Championship"},
                ],
            },
        })
        checker = FakeChecker()
        service, _, _ = self.make_service([], checker=checker, http_get=http_get)
        service.update_config({"include_sports": ["soccer"]})

        result = service.run_once(force=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["launched"], 1)
        deadline = time.time() + 2
        while time.time() < deadline and not checker.calls:
            time.sleep(0.01)
        self.assertEqual(len(checker.calls), 1)
        upcoming = service.get_status()["upcoming_events"]
        self.assertEqual(upcoming[0]["sport"], "soccer")
        self.assertNotEqual(upcoming[0]["state"], "filtered")

    def test_manual_force_launches_scheduled_event_with_manual_bucket(self):
        checker = FakeChecker()
        service, _, _ = self.make_service(
            [make_event(event_date="2030-01-01T00:00:00+00:00")],
            checker=checker,
        )

        scan_result = service.run_once(force=True)
        self.assertTrue(scan_result["success"])
        self.assertEqual(scan_result["launched"], 0)
        upcoming = service.get_status()["upcoming_events"]
        self.assertEqual(upcoming[0]["state"], "scheduled")

        result = service.force_check_event(upcoming[0]["identity"])
        self.assertTrue(result["success"])
        self.assertTrue(result["launched"])
        self.assertEqual(result["event"]["trigger_bucket"], "manual")

        deadline = time.time() + 2
        while time.time() < deadline and not checker.calls:
            time.sleep(0.01)

        self.assertEqual(len(checker.calls), 1)
        args, kwargs = checker.calls[0]
        self.assertEqual(args[0], 77)
        self.assertEqual(kwargs["program_name"], "Home vs Away")

        deadline = time.time() + 2
        recent = []
        while time.time() < deadline:
            recent = service.get_status()["recent_events"]
            if recent and recent[0]["type"] == "preflight_completed":
                break
            time.sleep(0.01)
        self.assertEqual(recent[0]["type"], "preflight_completed")
        self.assertEqual(recent[0]["details"]["bucket"], "manual")
        upcoming_after = service.get_status()["upcoming_events"]
        self.assertEqual(upcoming_after[0]["last_preflight_event"]["type"], "preflight_completed")
        self.assertEqual(upcoming_after[0]["last_preflight_event"]["details"]["bucket"], "manual")

    def test_upcoming_events_attach_recent_check_by_event_fingerprint(self):
        upcoming = [{
            "identity": "new-identity",
            "teamarr_id": "100",
            "event_id": "event-100",
            "event_name": "Home vs Away",
            "event_date": "2026-05-28T22:10:00+00:00",
            "dispatcharr_channel_id": 77,
        }]
        recent = [{
            "identity": "old-identity",
            "event_name": "Home vs Away",
            "event_date": "2026-05-28T22:10:00+00:00",
            "dispatcharr_channel_id": 77,
            "type": "preflight_completed",
            "details": {"bucket": "manual"},
        }]

        enriched = TeamarrPreflightService._attach_recent_events_to_upcoming(upcoming, recent)

        self.assertEqual(enriched[0]["last_preflight_event"]["type"], "preflight_completed")
        self.assertEqual(enriched[0]["last_preflight_event"]["identity"], "old-identity")

    def test_status_attaches_checks_beyond_recent_event_display_limit(self):
        event = make_event(
            id=100,
            event_date="2026-05-28T20:00:00+00:00",
            dispatcharr_channel_id=77,
        )
        service, _, _ = self.make_service([event])
        scan_result = service.run_once(force=True)
        self.assertTrue(scan_result["success"])
        upcoming = service.get_status()["upcoming_events"]
        target = upcoming[0]

        old_check = {
            "timestamp": FIXED_NOW - 3600,
            "type": "preflight_completed",
            "identity": target["identity"],
            "event_name": target["event_name"],
            "event_date": target["event_date"],
            "dispatcharr_channel_id": target["dispatcharr_channel_id"],
            "details": {"bucket": "20m"},
        }
        with service._lock:
            service._events.clear()
            for index in range(30):
                service._events.append({
                    "timestamp": FIXED_NOW + index,
                    "type": "preflight_completed",
                    "identity": f"other-{index}",
                    "event_name": f"Other {index}",
                    "event_date": "2026-05-28T22:00:00+00:00",
                    "dispatcharr_channel_id": 9000 + index,
                    "details": {"bucket": "manual"},
                })
            service._events.append(old_check)

        status = service.get_status()

        self.assertEqual(len(status["recent_events"]), 25)
        self.assertEqual(
            status["upcoming_events"][0]["last_preflight_event"]["type"],
            "preflight_completed",
        )
        self.assertEqual(
            status["upcoming_events"][0]["last_preflight_event"]["details"]["bucket"],
            "20m",
        )

    def test_manual_force_launches_past_event_as_manual_check(self):
        checker = FakeChecker()
        service, _, _ = self.make_service(
            [make_event(event_date="2026-05-28T20:00:00+00:00")],
            checker=checker,
        )

        scan_result = service.run_once(force=True)
        self.assertTrue(scan_result["success"])
        upcoming = service.get_status()["upcoming_events"]
        self.assertEqual(upcoming[0]["state"], "past")

        result = service.force_check_event(upcoming[0]["identity"])
        self.assertTrue(result["success"])
        self.assertTrue(result["launched"])
        self.assertEqual(result["event"]["trigger_bucket"], "manual")

        deadline = time.time() + 2
        while time.time() < deadline and not checker.calls:
            time.sleep(0.01)

        self.assertEqual(len(checker.calls), 1)
        args, kwargs = checker.calls[0]
        self.assertEqual(args[0], 77)
        self.assertEqual(kwargs["program_name"], "Home vs Away")

    def test_manual_force_rejects_filtered_event_without_launching_check(self):
        checker = FakeChecker()
        service, _, _ = self.make_service([make_event(sport="basketball")], checker=checker)
        service.update_config({"include_sports": ["soccer"]})

        scan_result = service.run_once(force=True)
        self.assertTrue(scan_result["success"])
        upcoming = service.get_status()["upcoming_events"]
        self.assertEqual(upcoming[0]["state"], "filtered")

        result = service.force_check_event(upcoming[0]["identity"])
        self.assertFalse(result["success"])
        self.assertEqual(result["code"], "filtered")
        self.assertEqual(checker.calls, [])

        recent = service.get_status()["recent_events"]
        self.assertEqual(recent[0]["type"], "manual_preflight_rejected")
        self.assertEqual(recent[0]["details"]["reason"], "filtered")


if __name__ == "__main__":
    unittest.main()

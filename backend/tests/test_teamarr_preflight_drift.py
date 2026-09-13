"""Tests for the Teamarr order-drift monitor.

The drift monitor watches Teamarr-managed channels and queues a re-sync check
when the Dispatcharr stream order for a channel drifts away from the ordered
baseline StreamFlow last wrote (checked_stream_ids).
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.core.atomic_json import atomic_write_json
from apps.stream.teamarr_preflight_service import (
    DRIFT_RESYNC_QUEUE_PRIORITY,
    TeamarrPreflightService,
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


class FakeHttp:
    """Returns the managed-channel payload for /api/v1/channels/managed."""

    def __init__(self, managed_channels):
        self.managed_channels = managed_channels

    def __call__(self, url, *args, **kwargs):
        if str(url).endswith("/api/v1/channels/managed"):
            return FakeResponse(self.managed_channels)
        return FakeResponse([])


class FakeUdi:
    """Returns a fixed per-channel stream order."""

    def __init__(self, channel_order):
        # channel_id -> [ordered stream dicts]
        self.channel_order = dict(channel_order)

    def get_channel_streams(self, channel_id):
        return self.channel_order.get(channel_id)


class FakeDb:
    def __init__(self):
        self.settings = {}

    def get_system_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_system_setting(self, key, value):
        self.settings[key] = value
        return True


class FakeChecker:
    def __init__(self, baselines):
        # channel_id -> ordered baseline stream ids (StreamFlow's last write)
        self.update_tracker = FakeUpdateTracker(baselines)
        self.queued = []

    def queue_channel(self, *args, **kwargs):
        self.queued.append((args, kwargs))
        return True


class FakeUpdateTracker:
    def __init__(self, baselines):
        self.updates = {
            "channels": {
                str(channel_id): {"checked_stream_ids": list(baseline)}
                for channel_id, baseline in baselines.items()
            }
        }


MANAGED_101 = {
    "dispatcharr_channel_id": 101,
    "event_name": "NFL | Team A @ Team B",
    "channel_name": "Sports 1",
}

MANAGED_202 = {
    "dispatcharr_channel_id": 202,
    "event_name": "MLB | X @ Y",
    "channel_name": "Sports 2",
}


class TeamarrDriftTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_file = Path(self.temp_dir.name) / "teamarr_preflight_config.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def make_service(self, managed_channels, channel_order, baselines):
        checker = FakeChecker(baselines)
        udi = FakeUdi(channel_order)
        # Seed the config file directly so the constructor loads enabled=True
        # WITHOUT the worker thread auto-starting (start() only happens via
        # update_config). This keeps the drift checks deterministic in tests.
        atomic_write_json(self.config_file, {
            "teamarr_base_url": "http://teamarr.test",
            "api_key": "secret",
            "enabled": True,
            "drift_enabled": True,
            "drift_poll_interval_seconds": 300,
        })
        service = TeamarrPreflightService(
            config_file=self.config_file,
            http_get=FakeHttp(managed_channels),
            udi_provider=lambda: udi,
            stream_checker_provider=lambda: checker,
            automation_config_provider=lambda: Mock(),
            automation_status_provider=lambda: {},
            db_provider=lambda: FakeDb(),
            clock=lambda: FIXED_NOW,
        )
        return service, checker, udi

    def test_no_drift_when_order_matches(self):
        service, checker, udi = self.make_service(
            managed_channels=[MANAGED_101],
            channel_order={101: [{"id": 1}, {"id": 2}, {"id": 3}]},
            baselines={101: [1, 2, 3]},
        )
        result = service._check_drift(service.get_config())
        self.assertEqual(result["drifted"], 0)
        self.assertEqual(result["channels_checked"], 1)
        self.assertEqual(checker.queued, [])

    def test_reordered_membership_same_is_drift(self):
        # Same stream set, different order -> external reorder.
        service, checker, udi = self.make_service(
            managed_channels=[MANAGED_101],
            channel_order={101: [{"id": 3}, {"id": 1}, {"id": 2}]},
            baselines={101: [1, 2, 3]},
        )
        result = service._check_drift(service.get_config())
        self.assertEqual(result["drifted"], 1)
        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["channels"]["101"]["kind"], "reordered")
        args, kwargs = checker.queued[0]
        self.assertEqual(args[0], 101)
        self.assertEqual(kwargs["priority"], DRIFT_RESYNC_QUEUE_PRIORITY)
        self.assertFalse(kwargs["force_check"])
        self.assertEqual(kwargs["metadata"]["source"], "stream_drift")

    def test_membership_change_is_drift(self):
        service, checker, udi = self.make_service(
            managed_channels=[MANAGED_101],
            channel_order={101: [{"id": 1}, {"id": 2}, {"id": 9}]},
            baselines={101: [1, 2, 3]},
        )
        result = service._check_drift(service.get_config())
        self.assertEqual(result["drifted"], 1)
        self.assertEqual(result["channels"]["101"]["kind"], "membership")
        self.assertEqual(len(checker.queued), 1)

    def test_channels_without_baseline_not_checked(self):
        # No checked_stream_ids baseline yet -> nothing to compare, skip.
        service, checker, udi = self.make_service(
            managed_channels=[MANAGED_101],
            channel_order={101: [{"id": 1}, {"id": 2}]},
            baselines={},  # no baseline at all
        )
        result = service._check_drift(service.get_config())
        self.assertEqual(result["channels_checked"], 0)
        self.assertEqual(checker.queued, [])

    def test_drift_disabled_is_noop(self):
        # Seed config with drift_disabled so the worker never auto-starts.
        checker = FakeChecker({101: [1, 2, 3]})
        udi = FakeUdi({101: [{"id": 3}, {"id": 1}, {"id": 2}]})
        atomic_write_json(self.config_file, {
            "teamarr_base_url": "http://teamarr.test",
            "api_key": "secret",
            "enabled": False,
            "drift_enabled": False,
            "drift_poll_interval_seconds": 300,
        })
        service = TeamarrPreflightService(
            config_file=self.config_file,
            http_get=FakeHttp([MANAGED_101]),
            udi_provider=lambda: udi,
            stream_checker_provider=lambda: checker,
            automation_config_provider=lambda: Mock(),
            automation_status_provider=lambda: {},
            db_provider=lambda: FakeDb(),
            clock=lambda: FIXED_NOW,
        )
        result = service._check_drift(service.get_config())
        self.assertEqual(result["drifted"], 0)
        self.assertEqual(checker.queued, [])

    def test_non_managed_channel_ignored(self):
        # 202 has drift but is not in managed channels, so it should be ignored.
        service, checker, udi = self.make_service(
            managed_channels=[MANAGED_101],
            channel_order={
                101: [{"id": 1}, {"id": 2}],
                202: [{"id": 5}, {"id": 4}],
            },
            baselines={101: [1, 2], 202: [4, 5]},
        )
        result = service._check_drift(service.get_config())
        self.assertEqual(result["channels_checked"], 1)
        self.assertEqual(result["drifted"], 0)
        self.assertEqual(checker.queued, [])

    def test_drift_baselines_from_checked_stream_ids(self):
        service, checker, udi = self.make_service(
            managed_channels=[MANAGED_101],
            channel_order={101: [{"id": 1}, {"id": 2}]},
            baselines={101: [1, 2]},
        )
        # Directly exercise the baseline reader to confirm it maps 101 -> ordered [1,2].
        baselines = service._drift_baselines()
        self.assertEqual(baselines[101], [1, 2])

    def test_status_exposes_drift(self):
        service, checker, udi = self.make_service(
            managed_channels=[MANAGED_101],
            channel_order={101: [{"id": 3}, {"id": 1}, {"id": 2}]},
            baselines={101: [1, 2, 3]},
        )
        service._check_drift(service.get_config())
        status = service.get_status()
        drift = status["drift_status"]
        self.assertTrue(drift["enabled"])
        self.assertEqual(drift["drifted"], 1)
        self.assertEqual(drift["queued"], 1)


if __name__ == "__main__":
    unittest.main()
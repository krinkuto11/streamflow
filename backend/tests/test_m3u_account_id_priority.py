#!/usr/bin/env python3
import os
import sys
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestM3uAccountIdPriority(unittest.TestCase):
    def test_stream_sort_key_uses_sql_m3u_account_id_for_absolute_priority(self):
        from apps.stream.stream_checker_service import StreamCheckerService

        service = StreamCheckerService.__new__(StreamCheckerService)
        udi = Mock()
        udi.get_stream_by_id.side_effect = lambda stream_id: {
            1: {"id": 1, "m3u_account_id": 20},
            2: {"id": 2, "m3u_account_id": 10},
        }.get(stream_id)

        low_res_priority_stream = {"stream_id": 1, "resolution": "1280x720", "score": 0.10}
        high_res_secondary_stream = {"stream_id": 2, "resolution": "3840x2160", "score": 1.00}

        with patch("apps.stream.stream_checker_service.get_udi_manager", return_value=udi):
            sorted_streams = sorted(
                [high_res_secondary_stream, low_res_priority_stream],
                key=lambda stream: service._generate_stream_sort_key(
                    stream,
                    priority_m3u_ids=["20", "10"],
                    priority_mode="absolute",
                ),
            )

        self.assertEqual([stream["stream_id"] for stream in sorted_streams], [1, 2])

    def test_stream_sort_key_uses_resolution_first_for_same_resolution_mode(self):
        from apps.stream.stream_checker_service import StreamCheckerService

        service = StreamCheckerService.__new__(StreamCheckerService)
        udi = Mock()
        udi.get_stream_by_id.side_effect = lambda stream_id: {
            1: {"id": 1, "m3u_account_id": 20},
            2: {"id": 2, "m3u_account_id": 10},
        }.get(stream_id)

        low_res_priority_stream = {"stream_id": 1, "resolution": "1280x720", "score": 0.10}
        high_res_secondary_stream = {"stream_id": 2, "resolution": "3840x2160", "score": 1.00}

        with patch("apps.stream.stream_checker_service.get_udi_manager", return_value=udi):
            sorted_streams = sorted(
                [low_res_priority_stream, high_res_secondary_stream],
                key=lambda stream: service._generate_stream_sort_key(
                    stream,
                    priority_m3u_ids=[20, 10],
                    priority_mode="same_resolution",
                ),
            )

        self.assertEqual([stream["stream_id"] for stream in sorted_streams], [2, 1])

    def test_stream_sort_key_uses_score_only_for_quality_mode(self):
        from apps.stream.stream_checker_service import StreamCheckerService

        service = StreamCheckerService.__new__(StreamCheckerService)
        udi = Mock()
        udi.get_stream_by_id.side_effect = lambda stream_id: {
            1: {"id": 1, "m3u_account_id": 20},
            2: {"id": 2, "m3u_account_id": 10},
        }.get(stream_id)

        low_score_priority_stream = {"stream_id": 1, "resolution": "3840x2160", "score": 0.10}
        high_score_secondary_stream = {"stream_id": 2, "resolution": "1280x720", "score": 1.00}

        with patch("apps.stream.stream_checker_service.get_udi_manager", return_value=udi):
            sorted_streams = sorted(
                [low_score_priority_stream, high_score_secondary_stream],
                key=lambda stream: service._generate_stream_sort_key(
                    stream,
                    priority_m3u_ids=[20, 10],
                    priority_mode="quality",
                ),
            )

        self.assertEqual([stream["stream_id"] for stream in sorted_streams], [2, 1])

    def test_stream_sort_key_uses_playlist_then_score_mode(self):
        from apps.stream.stream_checker_service import StreamCheckerService

        service = StreamCheckerService.__new__(StreamCheckerService)
        udi = Mock()
        udi.get_stream_by_id.side_effect = lambda stream_id: {
            1: {"id": 1, "m3u_account_id": 20},
            2: {"id": 2, "m3u_account_id": 10},
        }.get(stream_id)

        low_score_priority_stream = {"stream_id": 1, "resolution": "1280x720", "score": 0.10}
        high_score_secondary_stream = {"stream_id": 2, "resolution": "3840x2160", "score": 1.00}

        with patch("apps.stream.stream_checker_service.get_udi_manager", return_value=udi):
            sorted_streams = sorted(
                [high_score_secondary_stream, low_score_priority_stream],
                key=lambda stream: service._generate_stream_sort_key(
                    stream,
                    priority_m3u_ids=[20, 10],
                    priority_mode="playlist_score",
                ),
            )

        self.assertEqual([stream["stream_id"] for stream in sorted_streams], [1, 2])

    def test_stream_sort_key_uses_score_then_playlist_tiebreaker_mode(self):
        from apps.stream.stream_checker_service import StreamCheckerService

        service = StreamCheckerService.__new__(StreamCheckerService)
        udi = Mock()
        udi.get_stream_by_id.side_effect = lambda stream_id: {
            1: {"id": 1, "m3u_account_id": 20},
            2: {"id": 2, "m3u_account_id": 10},
            3: {"id": 3, "m3u_account_id": 10},
        }.get(stream_id)

        priority_tie_stream = {"stream_id": 1, "resolution": "1280x720", "score": 0.80}
        secondary_tie_stream = {"stream_id": 2, "resolution": "3840x2160", "score": 0.80}
        high_score_secondary_stream = {"stream_id": 3, "resolution": "1280x720", "score": 0.90}

        with patch("apps.stream.stream_checker_service.get_udi_manager", return_value=udi):
            sorted_streams = sorted(
                [secondary_tie_stream, high_score_secondary_stream, priority_tie_stream],
                key=lambda stream: service._generate_stream_sort_key(
                    stream,
                    priority_m3u_ids=[20, 10],
                    priority_mode="score_playlist",
                ),
            )

        self.assertEqual([stream["stream_id"] for stream in sorted_streams], [3, 1, 2])

    def test_udi_profile_lookup_accepts_m3u_account_id(self):
        from apps.udi.manager import UDIManager

        udi = UDIManager()
        udi._m3u_accounts_cache = [
            {
                "id": 5,
                "name": "Provider",
                "profiles": [
                    {"id": 50, "name": "Primary", "max_streams": 1, "is_active": True},
                    {"id": 51, "name": "Sibling", "max_streams": 1, "is_active": True},
                ],
            }
        ]
        udi._streams_cache = [{"id": 100, "m3u_account_id": "5", "url": "http://example/100"}]
        udi._initialized = True
        udi._build_indexes()
        udi.get_active_streams_count_per_profile = lambda account_id: {50: 1, 51: 0}

        profile = udi.find_available_profile_for_stream(
            {"id": 100, "m3u_account_id": "5", "url": "http://example/100"}
        )

        self.assertIsNotNone(profile)
        self.assertEqual(profile["id"], 51)
        self.assertEqual(udi._stream_account_id[100], 5)

    def test_udi_account_without_active_profiles_defers_to_account_limiter(self):
        from apps.udi.manager import UDIManager

        udi = UDIManager()
        udi._m3u_accounts_cache = [
            {
                "id": 5,
                "name": "Provider",
                "max_streams": 1,
                "profiles": [],
            }
        ]
        udi._streams_cache = [
            {"id": 100, "m3u_account_id": 5, "url": "http://example/100"}
        ]
        udi._initialized = True
        udi._build_indexes()

        can_run, reason = udi.check_stream_can_run(udi._streams_cache[0])

        self.assertTrue(can_run)
        self.assertIsNone(reason)

        udi._m3u_accounts_cache[0]["profiles"] = [
            {
                "id": 51,
                "name": "Broken alternate",
                "is_active": True,
                "is_default": False,
                "max_streams": 1,
            }
        ]
        can_run, reason = udi.check_stream_can_run(udi._streams_cache[0])

        self.assertFalse(can_run)
        self.assertIn("at capacity", reason)

    def test_profile_reservation_accepts_m3u_account_id(self):
        from apps.stream.concurrent_stream_limiter import AccountStreamLimiter

        udi = Mock()
        udi.get_m3u_account_by_id.return_value = {
            "id": 7,
            "name": "Provider",
            "profiles": [
                {"id": 70, "name": "Primary", "max_streams": 1, "is_active": True}
            ],
        }
        udi.get_active_streams_count_per_profile.return_value = {70: 0}
        limiter = AccountStreamLimiter(udi_manager=udi)

        acquired, reason, profile = limiter.reserve_profile_for_stream(
            {"id": 200, "url": "http://provider.test/live/200", "m3u_account_id": "7"}
        )

        self.assertTrue(acquired)
        self.assertEqual(reason, "acquired")
        self.assertEqual(profile["id"], 70)
        udi.get_m3u_account_by_id.assert_called_with(7)

    def test_profile_slot_snapshot_exposes_sibling_capacity(self):
        from apps.stream.concurrent_stream_limiter import AccountStreamLimiter

        udi = Mock()
        udi.get_m3u_account_by_id.return_value = {
            "id": 7,
            "name": "Provider",
            "profiles": [
                {
                    "id": 70,
                    "name": "Primary",
                    "max_streams": 2,
                    "is_active": True,
                    "is_default": True,
                },
                {
                    "id": 71,
                    "name": "Sibling",
                    "max_streams": 1,
                    "is_active": True,
                    "is_default": False,
                    "search_pattern": "primary-user",
                    "replace_pattern": "sibling-user",
                },
                {"id": 72, "name": "Inactive", "max_streams": 1, "is_active": False},
            ],
        }
        udi.get_active_streams_count_per_profile.return_value = {"70": 1, "71": 0}
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.profile_checking_counts[70] = 1

        snapshot = limiter.get_profile_slot_snapshot(7)

        self.assertEqual(
            snapshot,
            [
                {
                    "id": 70,
                    "name": "Primary",
                    "limit": 2,
                    "unlimited": False,
                    "active_viewers": 1,
                    "real_viewers": 1,
                    "shadow_watchers": 0,
                    "checking": 1,
                    "used": 2,
                    "available": 0,
                    "full": True,
                    "capacity_counted": True,
                },
                {
                    "id": 71,
                    "name": "Sibling",
                    "limit": 1,
                    "unlimited": False,
                    "active_viewers": 0,
                    "real_viewers": 0,
                    "shadow_watchers": 0,
                    "checking": 0,
                    "used": 0,
                    "available": 1,
                    "full": False,
                    "capacity_counted": True,
                },
            ],
        )

    def test_udi_profile_usage_context_splits_shadow_watcher_clients(self):
        from apps.udi.manager import UDIManager

        udi = UDIManager()
        udi._m3u_accounts_cache = [
            {
                "id": 7,
                "name": "Provider",
                "profiles": [
                    {"id": 70, "name": "Primary", "max_streams": 1, "is_active": True},
                ],
            }
        ]
        udi._initialized = True
        udi._build_indexes()
        udi._proxy_status_cache = {
            "channel-1": {
                "state": "active",
                "m3u_profile_id": 70,
                "clients": [
                    {"user_agent": "VLC", "username": "viewer"},
                    {"user_agent": "VLC", "username": "viewer-two"},
                    {"user_agent": "StreamFlow-Shadow-Blank-Monitor/1.0"},
                ],
            },
            "channel-2": {
                "state": "active",
                "m3u_profile_id": 70,
                "clients": [
                    {"user_agent": "VLC", "username": "viewer-three"},
                ],
            },
        }
        udi._proxy_status_last_fetch = time.time()

        context = udi.get_active_stream_context_per_profile(7)

        self.assertEqual(context[70]["active_streams"], 2)
        self.assertEqual(context[70]["real_viewers"], 3)
        self.assertEqual(context[70]["real_viewer_streams"], 2)
        self.assertEqual(context[70]["shadow_watchers"], 1)
        self.assertEqual(udi.get_active_streams_count_per_profile(7), {70: 2})

    def test_udi_retains_removed_profile_owner_for_open_proxy_session(self):
        """A refresh must not hide usage from a just-removed provider profile."""
        from apps.udi.manager import UDIManager

        udi = UDIManager()
        udi._m3u_accounts_cache = [
            {
                "id": 7,
                "name": "Provider",
                "profiles": [
                    {"id": 70, "name": "Primary", "is_active": True},
                    {"id": 71, "name": "Removed later", "is_active": True},
                ],
            }
        ]
        udi._initialized = True
        udi._build_indexes()

        # Simulate the account refresh completing while Dispatcharr still has an
        # upstream session on profile 71. The current account payload no longer
        # contains that profile, but its last-known owner remains authoritative
        # for capacity accounting until the process/session ends.
        udi._m3u_accounts_cache = [
            {
                "id": 7,
                "name": "Provider",
                "profiles": [{"id": 70, "name": "Primary", "is_active": True}],
            }
        ]
        udi._build_indexes()
        udi._proxy_status_cache = {
            "channel-removed-profile": {
                "state": "active",
                "m3u_profile_id": "71",
                "clients": [{"user_agent": "VLC", "username": "viewer"}],
            }
        }
        udi._proxy_status_last_fetch = time.time()

        context = udi.get_active_stream_context_per_profile(7)

        self.assertEqual(context[71]["active_streams"], 1)
        self.assertEqual(context[71]["real_viewer_streams"], 1)
        self.assertEqual(udi.get_active_streams_for_account(7), 1)

        # Fetcher failures are represented by an empty mapping, the same shape
        # as a valid idle status. They must not erase the retained owner and
        # make a later still-running removed-profile session invisible.
        udi._proxy_status_cache = {}
        udi._proxy_status_last_fetch = 0
        udi.fetcher.fetch_proxy_status = Mock(return_value={})
        self.assertEqual(udi.get_active_stream_context_per_profile(7), {})
        self.assertEqual(udi._profile_account_id_candidates[71], {7})

        udi._proxy_status_cache = {
            "channel-removed-profile": {
                "state": "active",
                "m3u_profile_id": "71",
                "clients": [{"user_agent": "VLC", "username": "viewer"}],
            }
        }
        udi._proxy_status_last_fetch = time.time()
        self.assertEqual(
            udi.get_active_stream_context_per_profile(7)[71]["active_streams"],
            1,
        )

        # If the numeric ID is reused, proxy status cannot identify its account
        # generation. Charge every observed owner for this process; an empty
        # status is also the fetcher's error fallback and cannot safely prune.
        udi._m3u_accounts_cache = [
            {"id": 7, "name": "Provider", "profiles": []},
            {
                "id": 8,
                "name": "New owner",
                "profiles": [{"id": 71, "name": "Reused ID", "is_active": True}],
            },
        ]
        udi._build_indexes()

        self.assertEqual(
            udi.get_active_stream_context_per_profile(7)[71]["active_streams"],
            1,
        )
        self.assertEqual(
            udi.get_active_stream_context_per_profile(8)[71]["active_streams"],
            1,
        )

        udi._proxy_status_cache = {
            "channel-idle": {"state": "idle", "m3u_profile_id": "71"}
        }
        udi._proxy_status_last_fetch = time.time()
        self.assertEqual(udi.get_active_stream_context_per_profile(7), {})
        self.assertEqual(udi.get_active_stream_context_per_profile(8), {})

        udi._proxy_status_cache = {
            "channel-reused-profile": {
                "state": "active",
                "m3u_profile_id": "71",
                "clients": [{"user_agent": "VLC", "username": "new-viewer"}],
            }
        }
        udi._proxy_status_last_fetch = time.time()
        self.assertEqual(
            udi.get_active_stream_context_per_profile(7)[71]["active_streams"],
            1,
        )
        self.assertEqual(
            udi.get_active_stream_context_per_profile(8)[71]["active_streams"],
            1,
        )

    def test_removed_profile_session_blocks_identical_active_route_end_to_end(self):
        """UDI history and limiter route history jointly prevent overbooking."""
        from apps.stream.concurrent_stream_limiter import AccountStreamLimiter
        from apps.udi.manager import UDIManager

        active_shared = {
            "id": 70,
            "name": "Active shared",
            "max_streams": 1,
            "is_active": True,
            "is_default": False,
            "search_pattern": r"^http://provider[.]test/a/(.+)$",
            "replace_pattern": r"http://shared.test/live/$1",
        }
        removed_shared = {
            "id": 71,
            "name": "Removed shared",
            "max_streams": 1,
            "is_active": True,
            "is_default": False,
            "search_pattern": r"^http://provider[.]test/b/(.+)$",
            "replace_pattern": r"http://shared.test/live/$1",
        }
        independent = {
            "id": 72,
            "name": "Independent",
            "max_streams": 1,
            "is_active": True,
            "is_default": False,
            "search_pattern": r"^http://provider[.]test/c/(.+)$",
            "replace_pattern": r"http://other.test/live/$1",
        }

        udi = UDIManager()
        original_profiles = [active_shared, removed_shared, independent]
        udi._m3u_accounts_cache = [
            {"id": 7, "name": "Provider", "max_streams": 1, "profiles": original_profiles}
        ]
        udi._initialized = True
        udi._build_indexes()
        limiter = AccountStreamLimiter(udi_manager=udi)
        limiter.set_account_limit(7, 1, profiles=original_profiles)

        current_profiles = [active_shared, independent]
        udi._m3u_accounts_cache = [
            {"id": 7, "name": "Provider", "max_streams": 1, "profiles": current_profiles}
        ]
        udi._build_indexes()
        limiter.set_account_limit(7, 1, profiles=current_profiles)
        udi._proxy_status_cache = {
            "channel-removed-profile": {
                "state": "active",
                "m3u_profile_id": "71",
                "clients": [{"user_agent": "VLC", "username": "viewer"}],
            }
        }
        udi._proxy_status_last_fetch = time.time()

        acquired, reason, reserved, resolved_url = (
            limiter.reserve_profile_for_stream_with_url(
                {
                    "id": 100,
                    "url": "http://provider.test/a/100",
                    "m3u_account_id": 7,
                }
            )
        )

        self.assertFalse(acquired)
        self.assertEqual(reason, "active_viewers")
        self.assertIsNone(reserved)
        self.assertEqual(resolved_url, "")
        snapshot = {
            item["id"]: item for item in limiter.get_profile_slot_snapshot(7)
        }
        self.assertEqual(snapshot[70]["active_viewers"], 1)
        self.assertTrue(snapshot[70]["full"])
        self.assertEqual(snapshot[72]["active_viewers"], 0)

    def test_profile_slot_snapshot_and_reservation_expose_shadow_context(self):
        from apps.stream.concurrent_stream_limiter import AccountStreamLimiter

        udi = Mock()
        udi.get_m3u_account_by_id.return_value = {
            "id": 7,
            "name": "Provider",
            "profiles": [
                {"id": 70, "name": "Primary", "max_streams": 1, "is_active": True},
            ],
        }
        udi.get_active_stream_context_per_profile.return_value = {
            70: {
                "active_streams": 1,
                "real_viewers": 0,
                "shadow_watchers": 1,
            }
        }
        udi.get_active_streams_count_per_profile.return_value = {70: 1}
        udi._find_account_for_profile.return_value = 7
        limiter = AccountStreamLimiter(udi_manager=udi)

        snapshot = limiter.get_profile_slot_snapshot(7)
        acquired, reason, profile = limiter.reserve_profile_for_stream(
            {"id": 200, "url": "http://provider.test/live/200", "m3u_account_id": "7"}
        )

        self.assertEqual(snapshot[0]["active_viewers"], 1)
        self.assertEqual(snapshot[0]["real_viewers"], 0)
        self.assertEqual(snapshot[0]["shadow_watchers"], 1)
        self.assertTrue(snapshot[0]["full"])
        self.assertFalse(acquired)
        self.assertEqual(reason, "shadow_watchers")
        self.assertIsNone(profile)
        self.assertFalse(
            limiter.should_preempt_profile_for_viewer(
                {"id": 70, "name": "Primary", "max_streams": 1}
            )
        )


if __name__ == "__main__":
    unittest.main()

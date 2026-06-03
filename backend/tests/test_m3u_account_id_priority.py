#!/usr/bin/env python3
import os
import sys
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
            {"id": 200, "m3u_account_id": "7"}
        )

        self.assertTrue(acquired)
        self.assertEqual(reason, "acquired")
        self.assertEqual(profile["id"], 70)
        udi.get_m3u_account_by_id.assert_called_with(7)


if __name__ == "__main__":
    unittest.main()

import os
import sys
import tempfile
import unittest
from unittest.mock import Mock

os.environ['CONFIG_DIR'] = tempfile.mkdtemp()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestPreflightPreservesChannelAssignments(unittest.TestCase):
    def test_fresh_dispatcharr_assignment_is_used_when_removal_disabled(self):
        from apps.stream.stream_checker_service import StreamCheckerService

        service = StreamCheckerService.__new__(StreamCheckerService)
        fresh_channel = {'id': 906, 'name': 'Event Channel', 'streams': [10, 11, 12, 13, 14, 15, 16]}
        stale_channel = {'id': 906, 'name': 'Event Channel', 'streams': [10, 11]}

        udi = Mock()
        udi.fetcher.fetch_channel_by_id.return_value = fresh_channel

        assignment_ids = service._get_channel_assignment_stream_ids(
            906,
            stale_channel,
            udi,
            fallback_stream_ids=[10, 11],
            refresh_from_dispatcharr=True,
        )

        self.assertEqual(assignment_ids, [10, 11, 12, 13, 14, 15, 16])
        udi.fetcher.fetch_channel_by_id.assert_called_once_with(906)
        udi.update_channel.assert_called_once_with(906, fresh_channel)

    def test_removal_disabled_write_back_keeps_all_assigned_stream_ids(self):
        from apps.stream.stream_checker_service import StreamCheckerService

        assigned_ids = [10, 11, 12, 13, 14, 15, 16]
        reordered_ids = [16, 15]

        missing_assigned_ids = StreamCheckerService._get_uncached_channel_stream_ids(
            assigned_ids,
            set(reordered_ids),
            dead_stream_removal_enabled=False,
            dead_stream_ids={12, 13},
        )
        reordered_ids.extend(missing_assigned_ids)

        self.assertEqual(reordered_ids, [16, 15, 10, 11, 12, 13, 14])

    def test_removal_disabled_valid_ids_include_assigned_cache_misses(self):
        from apps.stream.stream_checker_service import StreamCheckerService

        udi = Mock()
        udi.get_valid_stream_ids.return_value = {10, 11}

        valid_ids = StreamCheckerService._build_write_back_valid_stream_ids(
            udi,
            [10, 11, 12, 13, 14, 15, 16],
            dead_stream_removal_enabled=False,
        )

        self.assertEqual(valid_ids, {10, 11, 12, 13, 14, 15, 16})

    def test_removal_enabled_keeps_existing_dead_filtering_behavior(self):
        from apps.stream.stream_checker_service import StreamCheckerService

        missing_assigned_ids = StreamCheckerService._get_uncached_channel_stream_ids(
            [10, 11, 12, 13, 14],
            {10, 11},
            dead_stream_removal_enabled=True,
            dead_stream_ids={12, 13},
        )
        valid_ids = StreamCheckerService._build_write_back_valid_stream_ids(
            Mock(),
            [10, 11, 12, 13, 14],
            dead_stream_removal_enabled=True,
        )

        self.assertEqual(missing_assigned_ids, [14])
        self.assertIsNone(valid_ids)

    def test_multiple_channels_preserve_assignments_when_removal_disabled(self):
        from apps.stream.stream_checker_service import StreamCheckerService

        service = StreamCheckerService.__new__(StreamCheckerService)
        udi = Mock()
        udi.get_valid_stream_ids.return_value = {10, 11, 15, 16, 20, 22}

        cases = [
            {
                'channel_id': 906,
                'assigned': [10, 11, 12, 13, 14, 15, 16],
                'reordered': [16, 15],
                'dead': {12, 13},
                'expected_write_back': [16, 15, 10, 11, 12, 13, 14],
            },
            {
                'channel_id': 907,
                'assigned': [20, 21, 22, 23],
                'reordered': [22],
                'dead': {21},
                'expected_write_back': [22, 20, 21, 23],
            },
        ]

        for case in cases:
            with self.subTest(channel_id=case['channel_id']):
                write_back_ids = list(case['reordered'])
                write_back_ids.extend(
                    service._get_uncached_channel_stream_ids(
                        case['assigned'],
                        set(write_back_ids),
                        dead_stream_removal_enabled=False,
                        dead_stream_ids=case['dead'],
                    )
                )
                valid_ids = service._build_write_back_valid_stream_ids(
                    udi,
                    case['assigned'],
                    dead_stream_removal_enabled=False,
                )

                self.assertEqual(write_back_ids, case['expected_write_back'])
                self.assertTrue(set(case['assigned']).issubset(valid_ids))

    def test_multiple_channels_still_drop_dead_assignments_when_removal_enabled(self):
        from apps.stream.stream_checker_service import StreamCheckerService

        service = StreamCheckerService.__new__(StreamCheckerService)
        cases = [
            {
                'channel_id': 906,
                'assigned': [10, 11, 12, 13, 14, 15, 16],
                'reordered': [16, 15],
                'dead': {12, 13},
                'expected_write_back': [16, 15, 10, 11, 14],
            },
            {
                'channel_id': 907,
                'assigned': [20, 21, 22, 23],
                'reordered': [22],
                'dead': {21, 23},
                'expected_write_back': [22, 20],
            },
        ]

        for case in cases:
            with self.subTest(channel_id=case['channel_id']):
                write_back_ids = list(case['reordered'])
                write_back_ids.extend(
                    service._get_uncached_channel_stream_ids(
                        case['assigned'],
                        set(write_back_ids),
                        dead_stream_removal_enabled=True,
                        dead_stream_ids=case['dead'],
                    )
                )
                valid_ids = service._build_write_back_valid_stream_ids(
                    Mock(),
                    case['assigned'],
                    dead_stream_removal_enabled=True,
                )

                self.assertEqual(write_back_ids, case['expected_write_back'])
                self.assertIsNone(valid_ids)


if __name__ == '__main__':
    unittest.main()

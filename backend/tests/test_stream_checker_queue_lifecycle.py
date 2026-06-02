#!/usr/bin/env python3
"""Regression tests for stream-checker queue clear/abort lifecycle."""

import os
import sys
import threading
import unittest
from unittest.mock import Mock, patch

from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.api.stream_checker_handlers import (  # noqa: E402
    clear_stream_checker_queue_response,
    queue_all_channels_response,
)
from apps.stream.stream_checker_components import StreamCheckQueue  # noqa: E402
from apps.stream.stream_checker_service import StreamCheckerService  # noqa: E402


class TestStreamCheckQueueLifecycle(unittest.TestCase):
    def test_higher_priority_channels_are_checked_first(self):
        check_queue = StreamCheckQueue(max_size=10)

        self.assertTrue(check_queue.add_channel(201, priority=5, stream_count=1))
        self.assertTrue(check_queue.add_channel(202, priority=100, stream_count=1))
        self.assertTrue(check_queue.add_channel(203, priority=10, stream_count=1))

        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 202)
        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 203)
        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 201)

    def test_queue_entries_preserve_metadata_with_priority_ordering(self):
        check_queue = StreamCheckQueue(max_size=10)

        self.assertTrue(check_queue.add_channel(301, priority=5, stream_count=1))
        self.assertTrue(check_queue.add_channel(
            302,
            priority=100,
            stream_count=2,
            metadata={
                'source': 'teamarr_preflight',
                'program_name': 'Home vs Away',
                'is_epg_scheduled': True,
                'forced_profile_id': '42',
            },
        ))

        entry = check_queue.get_next_entry(timeout=0.1)
        self.assertEqual(entry['channel_id'], 302)
        self.assertEqual(entry['metadata']['source'], 'teamarr_preflight')
        self.assertEqual(entry['metadata']['program_name'], 'Home vs Away')
        self.assertTrue(entry['metadata']['is_epg_scheduled'])
        self.assertEqual(entry['metadata']['forced_profile_id'], '42')

        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 301)

    def test_equal_priority_channels_keep_fifo_order(self):
        check_queue = StreamCheckQueue(max_size=10)

        self.assertTrue(check_queue.add_channel(211, priority=10, stream_count=1))
        self.assertTrue(check_queue.add_channel(212, priority=10, stream_count=1))
        self.assertTrue(check_queue.add_channel(213, priority=10, stream_count=1))

        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 211)
        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 212)
        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 213)

    def test_clear_prevents_late_completion_from_repopulating_status(self):
        check_queue = StreamCheckQueue(max_size=10)
        self.assertTrue(check_queue.add_channel(101, priority=10, stream_count=3))
        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 101)

        cleared = check_queue.clear(reason='test_clear')

        self.assertEqual(cleared['in_progress'], 1)
        self.assertFalse(check_queue.mark_completed(101))

        status = check_queue.get_status()
        self.assertEqual(status['completed'], 0)
        self.assertEqual(status['total_completed'], 0)
        self.assertEqual(status['in_progress'], 0)
        self.assertEqual(status['state'], 'cleared')

    def test_clear_prevents_late_failure_from_repopulating_status(self):
        check_queue = StreamCheckQueue(max_size=10)
        self.assertTrue(check_queue.add_channel(102, priority=10, stream_count=2))
        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 102)

        check_queue.clear(reason='test_clear')
        self.assertFalse(check_queue.mark_failed(102, 'late failure'))

        status = check_queue.get_status()
        self.assertEqual(status['failed'], 0)
        self.assertEqual(status['total_failed'], 0)
        self.assertEqual(status['in_progress'], 0)

    def test_stale_queue_item_removed_by_clear_is_not_started(self):
        check_queue = StreamCheckQueue(max_size=10)
        self.assertTrue(check_queue.add_channel(104, priority=10, stream_count=4))

        with check_queue.lock:
            check_queue.queued.clear()

        self.assertIsNone(check_queue.get_next_channel(timeout=0.1))

        status = check_queue.get_status()
        self.assertEqual(status['in_progress'], 0)
        self.assertIsNone(status['current_channel'])
        self.assertEqual(status['state'], 'idle')

    def test_queue_status_exposes_batch_started_at(self):
        check_queue = StreamCheckQueue(max_size=10)

        self.assertTrue(check_queue.add_channel(105, priority=10, stream_count=2))
        queued_status = check_queue.get_status()
        self.assertIsNotNone(queued_status['started_at'])

        self.assertEqual(check_queue.get_next_channel(timeout=0.1), 105)
        active_status = check_queue.get_status()
        self.assertEqual(active_status['started_at'], queued_status['started_at'])

        check_queue.clear(reason='test_clear')
        self.assertIsNone(check_queue.get_status()['started_at'])

    def test_service_clear_queue_does_not_leave_abort_stuck_when_idle(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service.sync_batch_state = {'active': False}
        service.checking = False
        service.abort_current_check = threading.Event()
        service.progress = Mock()

        result = service.clear_queue()

        self.assertFalse(result['abort_requested'])
        self.assertFalse(service.abort_current_check.is_set())
        service.progress.clear.assert_called_once()

    def test_service_clear_queue_requests_abort_for_active_check(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service._sync_batch_generation = 0
        service.sync_batch_state = {'active': False}
        service.checking = True
        service.abort_current_check = threading.Event()
        service.progress = Mock()

        service.check_queue.add_channel(103, priority=10)
        service.check_queue.get_next_channel(timeout=0.1)

        result = service.clear_queue()

        self.assertTrue(result['abort_requested'])
        self.assertTrue(service.abort_current_check.is_set())
        self.assertEqual(service.check_queue.get_status()['state'], 'cleared')
        service.progress.clear.assert_called_once()

    def test_worker_keeps_abort_set_during_queue_handoff(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.running = True
        service.batch_start_time = None
        service.abort_current_check = threading.Event()
        service.check_queue = Mock()
        service._start_batch_changelog = Mock()
        service._finalize_batch_changelog = Mock()
        seen_abort_states = []

        def pull_entry(timeout):
            service.abort_current_check.set()
            return {'channel_id': 105, 'metadata': {}}

        def check_channel(channel_id):
            seen_abort_states.append(service.abort_current_check.is_set())
            service.running = False

        service.check_queue.get_next_entry.side_effect = pull_entry
        service._check_channel = check_channel

        service._worker_loop()

        self.assertEqual(seen_abort_states, [True])

    def test_worker_uses_single_channel_path_for_teamarr_queue_metadata(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.running = True
        service.batch_start_time = None
        service.abort_current_check = threading.Event()
        service.check_queue = Mock()
        service._start_batch_changelog = Mock()
        service._finalize_batch_changelog = Mock()
        service._check_channel = Mock()
        service.check_single_channel = Mock(return_value={'success': True})
        teamarr_service = Mock()

        def pull_entry(timeout):
            service.running = False
            return {
                'channel_id': 8441,
                'metadata': {
                    'source': 'teamarr_preflight',
                    'program_name': 'Home vs Away',
                    'is_epg_scheduled': True,
                    'forced_profile_id': '42',
                },
            }

        service.check_queue.get_next_entry.side_effect = pull_entry
        service.check_queue.mark_completed = Mock()
        service.check_queue.mark_failed = Mock()

        with patch(
            'apps.stream.teamarr_preflight_service.get_teamarr_preflight_service',
            return_value=teamarr_service,
        ):
            service._worker_loop()

        service._start_batch_changelog.assert_not_called()
        service._check_channel.assert_not_called()
        service.check_single_channel.assert_called_once_with(
            8441,
            program_name='Home vs Away',
            is_epg_scheduled=True,
            forced_profile_id='42',
        )
        teamarr_service.record_queued_check_result.assert_called_once()
        queued_metadata, result = teamarr_service.record_queued_check_result.call_args.args
        self.assertEqual(queued_metadata['source'], 'teamarr_preflight')
        self.assertEqual(result, {'success': True})
        service.check_queue.mark_completed.assert_called_once_with(8441)
        service.check_queue.mark_failed.assert_not_called()

    def test_clear_queue_resets_active_sync_batch_state(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service._sync_batch_generation = 4
        service.sync_batch_state = {
            'active': True,
            'total_channels': 5,
            'completed': 1,
            'failed': 0,
            'in_progress': 1,
            'queued_streams_count': 10,
            'in_progress_streams_count': 2,
            'generation': 4,
        }
        service.checking = True
        service.abort_current_check = threading.Event()
        service.progress = Mock()

        result = service.clear_queue()

        self.assertTrue(result['abort_requested'])
        self.assertTrue(service.abort_current_check.is_set())
        self.assertFalse(service.sync_batch_state['active'])
        self.assertEqual(service.sync_batch_state['total_channels'], 0)
        self.assertEqual(service.sync_batch_state['in_progress'], 0)
        self.assertEqual(service.sync_batch_state['queued_streams_count'], 0)
        self.assertEqual(service._sync_batch_generation, 5)
        self.assertFalse(service.checking)

    def test_get_status_does_not_show_cleared_state_for_active_sync_batch(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.check_queue.clear(reason='previous_manual_clear')
        service.lock = threading.Lock()
        service.sync_batch_state = {
            'active': True,
            'total_channels': 4,
            'completed': 1,
            'failed': 0,
            'in_progress': 1,
            'queued_streams_count': 6,
            'in_progress_streams_count': 2,
            'started_at': '2026-05-29T18:03:41',
            'generation': 1,
        }
        service.checking = True
        service.running = True
        service.config = Mock()
        service.config.get.side_effect = lambda key, default=None: default
        service.connectivity_guard_status = {'ok': True}
        service.progress = Mock()
        service.progress.get.return_value = {'channel_id': 200}
        service.update_tracker = Mock()
        service.update_tracker.get_last_global_check.return_value = None

        status = service.get_status()

        self.assertEqual(status['queue']['state'], 'checking')
        self.assertEqual(status['queue']['queued'], 2)
        self.assertEqual(status['queue']['in_progress'], 1)
        self.assertEqual(status['queue']['started_at'], '2026-05-29T18:03:41')
        self.assertTrue(status['stream_checking_mode'])

    def test_get_status_clears_stale_progress_when_no_check_is_active(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service.sync_batch_state = {'active': False}
        service.checking = False
        service.running = True
        service.config = Mock()
        service.config.get.side_effect = lambda key, default=None: default
        service.connectivity_guard_status = {'ok': True}
        service.progress = Mock()
        service.progress.get.return_value = {
            'status': 'analyzing',
            'channel_id': 1946,
            'streams_detail': [{'id': 1, 'status': 'checking'}],
        }
        service.update_tracker = Mock()
        service.update_tracker.get_last_global_check.return_value = None

        status = service.get_status()

        self.assertFalse(status['stream_checking_mode'])
        self.assertIsNone(status['progress'])
        service.progress.clear.assert_called_once()

    def test_sync_batch_invokes_progress_callback_after_each_channel(self):
        service = StreamCheckerService.__new__(StreamCheckerService)
        service.check_queue = StreamCheckQueue(max_size=10)
        service.lock = threading.Lock()
        service._sync_batch_generation = 0
        service.sync_batch_state = {'active': False}
        service.checking = False
        service.abort_current_check = threading.Event()
        service.update_tracker = Mock()
        service.config = Mock()
        service.config.get.side_effect = lambda key, default=None: True if key == 'concurrent_streams.enabled' else default
        service._require_quality_check_connectivity = Mock(return_value=None)
        service._check_channel_concurrent = Mock(side_effect=[
            {'success': True, 'channel_name': 'One'},
            {'success': True, 'channel_name': 'Two'},
        ])

        udi = Mock()
        udi.get_channel_by_id.side_effect = lambda channel_id: {'streams': [{'id': f'{channel_id}-a'}]}
        progress_events = []

        with patch('apps.udi.get_udi_manager', return_value=udi):
            result = service.check_channels_synchronously(
                [101, 102],
                progress_callback=lambda completed, total, payload: progress_events.append(
                    (completed, total, payload.get('channel_name'))
                ),
            )

        self.assertEqual(list(result.keys()), [101, 102])
        self.assertEqual(progress_events, [(1, 2, 'One'), (2, 2, 'Two')])
        self.assertFalse(service.sync_batch_state['active'])


class TestStreamCheckerQueueHandlers(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)

    def test_clear_queue_response_includes_abort_details(self):
        service = Mock()
        service.clear_queue.return_value = {
            'abort_requested': True,
            'cleared': {'queued': 2, 'in_progress': 1},
        }

        with self.app.app_context():
            response = clear_stream_checker_queue_response(
                get_stream_checker_service=lambda: service
            )

        data = response.get_json()
        self.assertEqual(data['message'], 'Queue cleared successfully')
        self.assertTrue(data['abort_requested'])
        self.assertEqual(data['cleared']['in_progress'], 1)

    def test_queue_all_channels_uses_service_queue_path(self):
        class UpdateTracker:
            def __init__(self):
                self.marked = None

            def mark_channels_updated(self, channel_ids):
                self.marked = list(channel_ids)

        class Service:
            def __init__(self):
                self.update_tracker = UpdateTracker()
                self.queued = None
                self.config = Mock()
                self.config.get.side_effect = lambda key, default=None: default

            def queue_channels(self, channel_ids, priority=10, force_check=False):
                self.queued = {
                    'channel_ids': list(channel_ids),
                    'priority': priority,
                    'force_check': force_check,
                }
                return 2

        service = Service()
        udi = Mock()
        udi.get_channels.return_value = [{'id': 1}, {'id': 2}, {'id': 3}]

        with self.app.app_context():
            response = queue_all_channels_response(
                get_stream_checker_service=lambda: service,
                get_udi_manager=lambda: udi,
            )

        data = response.get_json()
        self.assertEqual(service.update_tracker.marked, [1, 2, 3])
        self.assertEqual(service.queued['channel_ids'], [1, 2, 3])
        self.assertEqual(service.queued['priority'], 10)
        self.assertFalse(service.queued['force_check'])
        self.assertEqual(data['queued'], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Regression tests for stream-checker queue clear/abort lifecycle."""

import os
import sys
import threading
import unittest
from unittest.mock import Mock

from flask import Flask

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from apps.api.stream_checker_handlers import (  # noqa: E402
    clear_stream_checker_queue_response,
    queue_all_channels_response,
)
from apps.stream.stream_checker_components import StreamCheckQueue  # noqa: E402
from apps.stream.stream_checker_service import StreamCheckerService  # noqa: E402


class TestStreamCheckQueueLifecycle(unittest.TestCase):
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

        def pull_channel(timeout):
            service.abort_current_check.set()
            return 105

        def check_channel(channel_id):
            seen_abort_states.append(service.abort_current_check.is_set())
            service.running = False

        service.check_queue.get_next_channel.side_effect = pull_channel
        service._check_channel = check_channel

        service._worker_loop()

        self.assertEqual(seen_abort_states, [True])

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
